"""The Sessions tab of maxpm serve (#4592): running sessions and a read-only view of their transcripts."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from river import core, server, sessions

SID = "11111111-2222-3333-4444-555555555555"
SID2 = "66666666-7777-8888-9999-000000000000"
TID = "01a14170-419d-7241-afac-300125038d9d"


def claude_line(kind, content, stop=None, at="2026-11-08T10:00:00Z", **extra):
    msg = {"role": kind, "content": content}
    if stop:
        msg["stop_reason"] = stop
    return json.dumps({"type": kind, "timestamp": at, "message": msg, **extra}) + "\n"


def codex_line(kind, payload, at="2026-11-08T10:00:00Z"):
    return json.dumps({"timestamp": at, "type": kind, "payload": payload}) + "\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name) / "claude"
        self.croot = Path(self.dir.name) / "codex"
        os.environ["MAXPM_DB"] = os.path.join(self.dir.name, "t.db")
        self.c = core.connect()
        core.project_add(self.c, "site")
        core.register(self.c, "ag")
        core.register(self.c, "mark", human=True)
        self.it = core.item_add(self.c, "site", "header")["id"]
        core.claim(self.c, self.it, "ag")
        core.record_claude_session(self.c, "ag", SID)
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET pid=?, host=?, session_url=? WHERE name='ag'",
                           (os.getpid(), core.this_host(), "https://claude.ai/code/session_x"))
        self.path = self.root / "-proj" / f"{SID}.jsonl"
        self.path.parent.mkdir(parents=True)
        self.write(claude_line("user", "Fix the header, please")
                   + claude_line("assistant", [{"type": "thinking", "thinking": "hm"},
                                               {"type": "text", "text": "I read the header first."},
                                               {"type": "tool_use", "name": "Bash",
                                                "input": {"command": "cat a.css", "description": "Read the styles"}}],
                                 "tool_use")
                   + claude_line("user", [{"type": "tool_result", "content": "body {}"}])
                   + claude_line("user", "<system-reminder>x</system-reminder>")
                   + claude_line("user", "a side question", isSidechain=True))

    def tearDown(self):
        self.c.close()
        os.environ.pop("MAXPM_DB", None)
        self.dir.cleanup()

    def write(self, text):
        with open(self.path, "a") as f:
            f.write(text)

    def cards(self):
        return sessions.views(self.c, {}, self.root, self.croot)


class Sessions(Base):
    def test_a_card_shows_the_agent_its_item_state_open_link_and_last_lines(self):
        (card,) = self.cards()
        self.assertEqual((card["name"], card["holds"][0]["id"], card["session_url"], card["transcript"]),
                         ("ag", self.it, "https://claude.ai/code/session_x", "claude"))
        self.assertEqual([(e["role"], e["text"]) for e in card["last"]],
                         [("user", "Fix the header, please"), ("assistant", "I read the header first."),
                          ("tool", "Bash: Read the styles")])
        self.assertEqual(card["state"], "working")  # a tool runs
        self.write(claude_line("assistant", [{"type": "text", "text": "Done."}], "end_turn"))
        (card,) = self.cards()
        self.assertEqual((card["state"], card["state_label"]), ("idle", "idle: at its prompt"))
        self.assertEqual(card["last"][-1]["text"], "Done.")

    def test_a_tool_call_carries_its_input_and_its_result_for_the_chat(self):
        # #4604: the chat folds each tool call to one line; opened, it shows the command and what came back.
        self.write(claude_line("assistant", [{"type": "tool_use", "id": "tu1", "name": "Bash",
                                              "input": {"command": "git status", "description": "Show the status",
                                                        "timeout": 60, "run_in_background": False}}], "tool_use")
                   + claude_line("user", [{"type": "tool_result", "tool_use_id": "tu1", "is_error": True,
                                           "content": [{"type": "text", "text": "fatal: not a repo"}]}]))
        got = sessions.view(self.c, "ag", None, self.root, self.croot)["entries"]
        tool, res = got[-2], got[-1]
        self.assertEqual((tool["role"], tool["text"], tool["id"], tool["detail"]),
                         ("tool", "Bash: Show the status", "tu1", "git status"))
        self.assertEqual((res["role"], res["for"], res["error"], res["text"]), ("result", "tu1", True, "fatal: not a repo"))
        (card,) = self.cards()
        self.assertNotIn("result", [e["role"] for e in card["last"]])  # a card's last lines leave results out

    def test_the_view_names_the_items_its_lines_refer_to(self):
        # #4609: the page links #<number> to the drawer only for items in the queue.
        self.write(claude_line("assistant", [{"type": "text", "text": f"Done with #{self.it}; see #99999 and a&#38;"}],
                               "end_turn"))
        refs = sessions.view(self.c, "ag", None, self.root, self.croot)["refs"]
        self.assertEqual(refs, {str(self.it): {"title": "header", "status": "in_progress", "assignee": "ag"}})

    def test_the_item_pop_up_names_its_session_also_when_nobody_holds_it(self):
        # #4622: the holder; after it is done, the last agent that held it, ended or running.
        got = sessions.item_session(self.c, self.it, self.root, self.croot)
        self.assertEqual((got["holder"], got["session"]["name"], got["session"]["running"], got["session"]["viewable"]),
                         ("ag", "ag", True, True))
        core.done(self.c, self.it, "c1", "ag")
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET pid=999999 WHERE name='ag'")  # its process ended
        got = sessions.item_session(self.c, self.it, self.root, self.croot)
        self.assertEqual((got["status"], got["holder"], got["session"]["name"], got["session"]["running"], got["session"]["why"]),
                         ("done", None, "ag", False, "finished it"))
        other = core.item_add(self.c, "site", "nobody took it")["id"]
        self.assertIsNone(sessions.item_session(self.c, other)["session"])
        # #4628: no holder: the goal's owner, then a running session on another item of the goal, then the history.
        with core.tx(self.c):
            self.c.execute("INSERT INTO goals (project_id, name, outcome, done_when, rank, status, owner, shared, created_at) "
                           "SELECT project_id, 'ship', '', '', 1, 'open', NULL, 1, ? FROM items WHERE id=?", (core.iso(core.now()), other))
            g = self.c.execute("SELECT id FROM goals WHERE name='ship'").fetchone()[0]
            self.c.execute("INSERT INTO item_goals VALUES (?, ?)", (other, g))
        got = sessions.item_session(self.c, other)
        self.assertEqual((got["session"], got["goal"]), (None, "ship"))
        core.register(self.c, "w2")
        busy = core.item_add(self.c, "site", "next part")["id"]
        with core.tx(self.c):
            self.c.execute("INSERT INTO item_goals VALUES (?, ?)", (busy, g))
        core.claim(self.c, busy, "w2")
        self.assertEqual((sessions.item_session(self.c, other)["session"]["name"],
                          sessions.item_session(self.c, other)["session"]["why"]), ("w2", "works on goal ship"))
        with core.tx(self.c):
            self.c.execute("UPDATE goals SET owner='lead', shared=0 WHERE id=?", (g,))
        self.assertEqual(sessions.item_session(self.c, other)["session"]["why"], "owns goal ship")
        with self.assertRaises(core.RiverError):
            sessions.item_session(self.c, 999999)

    def test_the_settings_a_session_runs_with(self):
        # #4625: the values come from the session's transcript first, then its command line, then the default.
        self.write(json.dumps({"type": "permission-mode", "permissionMode": "auto"}) + "\n"
                   + json.dumps({"type": "assistant", "timestamp": "2026-11-08T10:03:00Z", "effort": "xhigh",
                                 "message": {"role": "assistant", "model": "claude-opus-5-5", "content": []}}) + "\n")
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET pid_cmd='claude --name ag --remote-control --model opus' WHERE name='ag'")
        got = {x["key"]: x for x in sessions.session_settings(self.c, "ag", self.root, self.croot)["settings"]}
        self.assertEqual((got["model"]["value"], got["model"]["source"], got["model"]["live"]), ("claude-opus-5-5", "session", True))
        self.assertEqual((got["effort"]["value"], got["permission_mode"]["value"]), ("xhigh", "auto"))
        self.assertEqual((got["remote_control"]["value"], got["remote_control"]["source"]), ("on", "command line"))
        self.assertFalse(got["permission_mode"]["live"])
        self.assertEqual(got["permission_mode"]["default_key"], "claude_permission_mode")

    def test_the_items_a_session_took(self):
        # #4623: from the history by the agent's name, newest first, with what it did.
        other = core.item_add(self.c, "site", "footer")["id"]
        core.done(self.c, self.it, "c1", "ag")
        core.claim(self.c, other, "ag")
        core.release(self.c, other, "later", "ag")
        got = sessions.session_items(self.c, "ag")
        self.assertEqual([(x["id"], x["did"], x["status"]) for x in got],
                         [(other, ["claimed", "released"], "open"), (self.it, ["claimed", "finished"], "done")])
        self.assertEqual(sessions.session_items(self.c, "nobody"), [])

    def test_the_state_names_a_prompt_a_question_and_a_wait_for_work(self):
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET activity_prompt='{}' WHERE name='ag'")
        self.assertEqual(self.cards()[0]["state_label"], "waits at a prompt")
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET activity_prompt=NULL WHERE name='ag'")
        core.send(self.c, "question", "Push the header?", to="mark", item=self.it, actor="ag")
        self.assertEqual(self.cards()[0]["state_label"], "waits for an answer")
        core.done(self.c, self.it, "c1", "ag")
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET waiting_since=? WHERE name='ag'", (core.iso(core.now()),))
        self.assertEqual(self.cards()[0]["state_label"], "idle: waits for work")

    def test_the_large_view_reads_the_tail_then_only_new_lines(self):
        v = sessions.view(self.c, "ag", None, self.root, self.croot)
        self.assertEqual(len(v["entries"]), 3)
        self.write('{"type": "assistant", "message": {"content": [{"type": "text", "text": "half')  # still written
        again = sessions.view(self.c, "ag", v["cursor"], self.root, self.croot)
        self.assertEqual((again["entries"], again["state"]), ([], "working"))
        self.write(' a line"}], "stop_reason": "end_turn"}}\n')
        again = sessions.view(self.c, "ag", again["cursor"], self.root, self.croot)
        self.assertEqual(([e["text"] for e in again["entries"]], again["state"]), (["half a line"], "idle"))
        core.register(self.c, "nobody")
        v = sessions.view(self.c, "nobody", None, self.root, self.croot)
        self.assertEqual(v["entries"], [])
        self.assertIn("No transcript of nobody", v["note"])

    def test_a_first_read_of_a_long_transcript_starts_at_a_whole_line(self):
        self.write(claude_line("assistant", [{"type": "text", "text": "x" * 5000}], "tool_use") * 3)
        got = sessions.entries("claude", self.path, None, tail=12000)
        self.assertEqual([len(e["text"]) for e in got["entries"]], [sessions.TEXT_MAX] * 2)
        self.assertEqual(got["cursor"], self.path.stat().st_size)

    def test_one_card_for_a_session_that_ran_commands_under_two_names(self):
        core.register(self.c, "alias")
        core.record_claude_session(self.c, "alias", SID)
        (card,) = self.cards()
        self.assertEqual((card["name"], card["also"]), ("ag", ["alias"]))  # the name that holds the work

    def test_an_ended_process_has_no_card(self):
        core.register(self.c, "old")
        core.record_claude_session(self.c, "old", SID2)
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET pid=999999, host=? WHERE name='old'", (core.this_host(),))
        self.assertEqual([c["name"] for c in self.cards()], ["ag"])

    def test_a_codex_session_reads_its_log_with_no_open_link(self):
        core.register(self.c, "cx")
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET agent_type='codex', platform='Codex', native_address=? WHERE name='cx'",
                           (TID,))
        log = self.croot / "2026" / "11" / "08" / f"rollout-2026-11-08T10-00-00-{TID}.jsonl"
        log.parent.mkdir(parents=True)
        text = (codex_line("event_msg", {"type": "task_started"})
                + codex_line("response_item", {"type": "message", "role": "user",
                                               "content": [{"type": "input_text", "text": "# AGENTS.md instructions"}]})
                + codex_line("response_item", {"type": "message", "role": "user",
                                               "content": [{"type": "input_text", "text": "Fix the footer"}]})
                + codex_line("response_item", {"type": "function_call", "name": "shell",
                                               "arguments": json.dumps({"command": ["git", "status"]})})
                + codex_line("response_item", {"type": "message", "role": "assistant",
                                               "content": [{"type": "output_text", "text": "Fixed."}]})
                + codex_line("event_msg", {"type": "task_complete"}))
        log.write_text(text)
        card = next(c for c in self.cards() if c["name"] == "cx")
        self.assertEqual([(e["role"], e["text"]) for e in card["last"]],
                         [("user", "Fix the footer"), ("tool", "shell: git status"), ("assistant", "Fixed.")])
        self.assertEqual((card["state"], card["session_url"], card["transcript"]), ("idle", None, "codex"))
        self.assertEqual(card["provider"], {"id": "codex", "label": "Codex"})  # #4627
        self.assertEqual(sessions.provider(None, "claude"), {"id": "claude-code", "label": "Claude Code"})
        self.assertIsNone(sessions.provider(None, None))
        if sessions._zstd():  # Codex compresses a log when the session ends
            log.unlink()
            Path(str(log) + ".zst").write_bytes(sessions._zstd().compress(text.encode()))
            v = sessions.view(self.c, "cx", None, self.root, self.croot)
            self.assertEqual([e["text"] for e in v["entries"]][-1], "Fixed.")

    def test_the_page_routes(self):
        conf = Path(self.dir.name) / "config"
        os.environ["CLAUDE_CONFIG_DIR"] = str(conf)
        self.addCleanup(os.environ.pop, "CLAUDE_CONFIG_DIR", None)
        (conf / "projects" / "-proj").mkdir(parents=True)
        (conf / "projects" / "-proj" / f"{SID}.jsonl").write_text(self.path.read_text())

        def get(path):
            h = server.Handler.__new__(server.Handler)
            h.path, h.client_address, h.headers, out = path, ("127.0.0.1", 5555), {"Host": "127.0.0.1:8765"}, {}
            h._send = lambda code, body, ctype=None: out.update(code=code, body=body)
            h.do_GET()
            return out["code"], out["body"]

        code, body = get("/api/sessions")
        self.assertEqual((code, [c["name"] for c in body["sessions"]]), (200, ["ag"]))
        code, body = get("/api/session?agent=ag")
        self.assertEqual((code, len(body["entries"]), body["terminal"]), (200, 3, False))
        code, body = get(f"/api/session?agent=ag&cursor={body['cursor']}")
        self.assertEqual((code, body["entries"]), (200, []))
        self.assertEqual(get("/api/session?agent=ghost")[0], 409)


class Adopt(Base):
    """tmux_adopt (#4598): a session outside tmux moves into a tmux pane when it is quiet at its prompt."""

    def setUp(self):
        import subprocess
        from tests.test_server import FakeTmux
        super().setUp()
        self.platform, core.PLATFORM = core.PLATFORM, "linux"
        self.tmux = server.TMUX_RUNNER = FakeTmux()
        self.tmux_path, core.tmux_path = core.tmux_path, lambda: "/usr/bin/tmux"
        roots, core.transcripts_root = core.transcripts_root, lambda: self.root
        self.addCleanup(setattr, core, "transcripts_root", roots)
        self.proc = subprocess.Popen(["sleep", "60"])  # the agent CLI in its Terminal tab
        self.addCleanup(self.proc.wait)
        self.addCleanup(self.proc.kill)
        info = core.proc_info(self.proc.pid)
        if info is None:
            self.skipTest("no ps here (an agent sandbox)")
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET pid=?, pid_cmd=?, model='opus' WHERE name='ag'", (self.proc.pid, info[2]))
        self.write(claude_line("assistant", [{"type": "text", "text": "Done."}], "end_turn", cwd=self.dir.name))

    def tearDown(self):
        core.PLATFORM, server.TMUX_RUNNER, core.tmux_path = self.platform, None, self.tmux_path
        server.ADOPT["failed"].clear()
        super().tearDown()

    def quiet(self, seconds=600):
        t = time.time() - seconds
        os.utime(self.path, (t, t))

    def check(self):
        (card,) = sessions.views(self.c, {}, self.root, self.croot)
        return card["adopt"]["when"]

    def test_it_waits_for_a_quiet_prompt_and_tmux_adopt(self):
        self.assertEqual(self.check(), "later")  # it wrote just now
        self.quiet()
        self.assertEqual(self.check(), "now")
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET activity_prompt='{}' WHERE name='ag'")
        self.assertEqual(self.check(), "later")  # a permission prompt waits
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET activity_prompt=NULL WHERE name='ag'")
        core.config_set(self.c, "tmux_adopt", "off")
        self.assertEqual(self.check(), "never")
        with self.assertRaises(core.RiverError):
            core.config_set(self.c, "tmux_adopt", "maybe")

    def test_the_resume_commands(self):
        cmd = core.resume_command(self.c, "ag", "claude-code", SID)["command"]
        self.assertTrue(cmd.startswith("claude --name ag --model "), cmd)
        self.assertIn(f"--resume {SID} ", cmd)
        self.assertIn("MaximizePM moved this session into a tmux pane", cmd)
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET pid_cmd='codex -m x -c model_reasoning_effort=low' WHERE name='ag'")
        core.config_set(self.c, "launch_agents", "Claude Code=@claude-code; Codex=@codex")
        cmd = core.resume_command(self.c, "ag", "codex", TID)["command"]
        self.assertTrue(cmd.startswith(f"codex resume {TID} -m "), cmd)
        self.assertIn("model_reasoning_effort=low", cmd)

    def test_serve_moves_a_quiet_session_and_it_keeps_its_work(self):
        self.quiet()
        moved = server.adopt_sessions(self.c)
        self.assertEqual(list(moved), ["ag"])
        self.proc.wait(5)  # its old process ended
        pane = self.tmux.panes[0]
        self.assertEqual(pane["agent"], "ag")
        line = pane["keys"][0]
        self.assertIn(f"cd {self.dir.name} && ", line)
        self.assertIn("MAXPM_AGENT=ag ", line)
        self.assertIn(f"--resume {SID} ", line)
        a = core.agent_status(self.c, "ag")
        self.assertEqual((a["pid"], a["state"], [h["id"] for h in a["holds"]]), (None, "active", [self.it]))
        self.assertIn("moves into a tmux pane", self.c.execute("SELECT change FROM events ORDER BY id DESC").fetchone()[0])
        self.assertEqual(server.adopt_sessions(self.c), {})  # no process now: nothing more to move

    def test_restart_with_new_settings(self):
        # #4625: the same chat resumes with the changed settings; a live change goes into its pane as a command.
        with self.assertRaises(core.RiverError):
            server.session_restart(self.c, "ag", {"permission_mode": "sometimes"})
        got = server.session_restart(self.c, "ag", {"permission_mode": "plan", "effort": "low"}, make_default=True)
        self.assertEqual(got["defaults"], ["claude_permission_mode"])
        self.proc.wait(5)
        line = self.tmux.panes[0]["keys"][0]
        self.assertIn(f"--resume {SID} ", line)
        self.assertIn("--permission-mode plan", line)
        self.assertIn("--effort low", line)
        self.assertIn("restarted this session with new settings", line)
        self.assertEqual(core.setting(self.c, "claude_permission_mode"), "plan")
        with self.assertRaises(core.RiverError):  # its provider changes this only at a start
            server.session_setting_live(self.c, "ag", "permission_mode", "auto")
        server.session_setting_live(self.c, "ag", "model", "sonnet")
        self.assertIn("/model sonnet", " ".join(str(k) for k in self.tmux.panes[0]["keys"]))

    def test_a_reused_pid_is_not_ended(self):
        self.quiet()
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET pid_cmd='claude --other' WHERE name='ag'")
        self.assertEqual(server.adopt_sessions(self.c), {})
        self.assertIsNone(self.proc.poll())  # still runs
        self.assertIn("ag", server.ADOPT["failed"])


PERMISSION = """⏺ Bash(git push)
  ⎿  Running…

 Bash command

   git push origin main
   Push the branch

 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again for git push commands in /work
   3. No, and tell Claude what to do differently (esc)

 Esc to cancel · Tab to amend
"""


class TmuxBase(Base):
    """An agent in a fake tmux pane that shows a permission prompt."""

    def setUp(self):
        from tests.test_server import FakeTmux
        super().setUp()
        self.platform, core.PLATFORM = core.PLATFORM, "linux"
        self.tmux = server.TMUX_RUNNER = FakeTmux()
        self.tmux.session = True
        self.tmux._pane(name="ag")
        self.pane = self.tmux.panes[0]
        self.pane.update(agent="ag", screen=PERMISSION)
        self.pause, server.PROMPT_PAUSE = server.PROMPT_PAUSE, 0
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET pid=NULL WHERE name='ag'")

    def tearDown(self):
        core.PLATFORM, server.TMUX_RUNNER, server.PROMPT_PAUSE = self.platform, None, self.pause
        super().tearDown()

    def cards(self):
        t = server.agent_terminals(self.c)
        return sessions.views(self.c, t, self.root, self.croot, prompts=server.pane_prompts(self.c, t))


class Waiting(TmuxBase):
    """#4607: what waits for the person in a session, and the dialog's answers to a prompt in its pane."""

    def test_a_prompt_in_the_pane_and_an_open_question_wait_for_the_person(self):
        core.send(self.c, "question", "Push the header?", to="mark", item=self.it, actor="ag")
        (card,) = self.cards()
        prompt, question = card["waiting"]
        self.assertEqual((prompt["kind"], prompt["question"], prompt["selected"], prompt["command"]),
                         ("prompt", "Do you want to proceed?", 0, "git push origin main"))
        self.assertEqual(prompt["options"][2], "3. No, and tell Claude what to do differently (esc)")
        self.assertEqual((question["kind"], question["body"], question["to"]), ("question", "Push the header?", "mark"))
        self.assertEqual(card["state"], "prompt")
        self.pane["screen"] = "❯ \n"  # answered: the prompt is gone
        self.assertEqual([w["kind"] for w in self.cards()[0]["waiting"]], ["question"])

    def test_the_dialog_picks_an_option_or_tells_the_agent_what_to_do(self):
        sig = self.cards()[0]["waiting"][0]["sig"]
        r = server.OPS["prompt_answer"](self.c, {"agent": "ag", "sig": sig, "pick": 1}, "mark")
        self.assertEqual((r["picked"][:6], self.pane["keys"]), ("2. Yes", ["Down", "Enter"]))
        self.pane["keys"].clear()
        r = server.OPS["prompt_answer"](self.c, {"agent": "ag", "sig": sig, "other": "push to\na branch first"}, "mark")
        self.assertEqual(self.pane["keys"], ["Down", "Down", "Enter", "push to a branch first", "Enter"])
        # A prompt that changed since the person read it gets no keys.
        self.pane["keys"].clear()
        self.pane["screen"] = PERMISSION.replace("git push origin main", "rm -rf build")
        with self.assertRaisesRegex(core.RiverError, "changed or is gone"):
            server.prompt_answer(self.c, "ag", sig, 0, None, "mark")  # same options, another command
        sig = self.cards()[0]["waiting"][0]["sig"]
        with self.assertRaisesRegex(core.RiverError, "pick one of the prompt's options"):
            server.prompt_answer(self.c, "ag", sig, 7, None, "mark")
        core.register(self.c, "other")
        with self.assertRaisesRegex(core.RiverError, "only a person"):
            server.prompt_answer(self.c, "ag", sig, 0, None, "other")
        self.assertEqual(self.pane["keys"], [])
        self.assertIn("prompt_answer", server.TYPING_OPS)  # it needs the page's token


class Images(TmuxBase):
    """#4610: an image pasted into the message box goes to the session as a saved file, its path pasted."""

    def setUp(self):
        super().setUp()
        self.pane["screen"] = "❯ \n"

    def test_an_image_is_saved_and_its_path_pasted_with_the_text(self):
        import base64
        png = b"\x89PNG\r\n\x1a\n" + b"\0" * 20
        r = server.OPS["session_send"](self.c, {"agent": "ag", "text": "what is wrong here?",
                                                "images": [{"type": "image/png", "data": base64.b64encode(png).decode()}]}, "mark")
        (path,) = r["images"]
        self.assertTrue(path.startswith(str(server.uploads_dir() / "ag")) and path.endswith(".png"), path)
        self.assertEqual(Path(path).read_bytes(), png)
        # The path as a paste of its own (Claude Code attaches it as [Image #1]), then the text, then Enter (#4618).
        paste = ["paste-buffer", "-p", "-d", "-b", "maxpm-send", "-t", self.pane["id"]]
        self.assertEqual(self.tmux.calls[-5:], [["set-buffer", "-b", "maxpm-send", "--", path], paste,
                                                ["set-buffer", "-b", "maxpm-send", "--", " what is wrong here?"], paste,
                                                ["send-keys", "-t", self.pane["id"], "Enter"]])
        got = server.upload_file("ag/" + Path(path).name)
        self.assertEqual(got, (png, "image/png"))
        for bad in ("../t.db", "ag/../../t.db", "ag/.x.png", "ag"):
            self.assertIsNone(server.upload_file(bad), bad)
        # The person's turn in the transcript names the image: the chat shows its thumbnail.
        self.write(claude_line("user", f"{path} what is wrong here?"))
        last = sessions.view(self.c, "ag", None, self.root, self.croot)["entries"][-1]
        self.assertEqual(last["images"], ["ag/" + Path(path).name])
        # Old uploads go.
        old = time.time() - 8 * 86400
        os.utime(path, (old, old))
        self.assertEqual(server.tidy_uploads(), 1)
        self.assertFalse(Path(path).exists())

    def test_only_images_and_not_too_many(self):
        import base64
        for images, why in (([{"data": base64.b64encode(b"#!/bin/sh").decode()}], "only PNG"),
                            ([{"data": "not base64!"}], "base64"),
                            ([{"data": "AAAA"}] * 6, "at most 5")):
            with self.assertRaisesRegex(core.RiverError, why):
                server.session_send(self.c, "ag", "x", "mark", images)
        with self.assertRaisesRegex(core.RiverError, "only a person"):
            core.register(self.c, "bot")
            server.session_send(self.c, "ag", "x", "bot", [{"data": "AAAA"}])


class SideFiles(Base):
    """#4619: the side panel shows local files read only, with the page's token, only from the project folders,
    their worktrees and uploads, each in a sandbox of its own."""

    def setUp(self):
        super().setUp()
        self.proj = Path(self.dir.name) / "proj"
        (self.proj / "scratch").mkdir(parents=True)
        (self.proj / "scratch" / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (self.proj / "page.html").write_text("<script>fetch('/api/action')</script>")
        (self.proj / "notes.py").write_text("x = 1")
        (self.proj / ".env").mkdir()
        (self.proj / ".env" / "key.txt").write_text("secret")
        core.project_add(self.c, "web", path=str(self.proj))
        server._ROOTS["at"] = 0
        self.addCleanup(server._ROOTS.update, at=0)

    def get(self, path):
        h = server.Handler.__new__(server.Handler)
        h.path, h.client_address, h.headers, out = path, ("127.0.0.1", 5555), {"Host": "127.0.0.1:8765"}, {}
        h._send = lambda code, body, ctype=None, headers=None: out.update(code=code, body=body, ctype=ctype, headers=headers)
        h.do_GET()
        return out

    def test_a_file_under_a_project_comes_with_the_token_in_a_sandbox(self):
        from urllib.parse import quote
        shot = quote(str(self.proj / "scratch" / "shot.png"))
        self.assertEqual(self.get(f"/api/file?path={shot}")["code"], 403)  # no token
        out = self.get(f"/api/file?path={shot}&token={server.PAGE_TOKEN}")
        self.assertEqual((out["code"], out["ctype"]), (200, "image/png"))
        self.assertIn("sandbox", out["headers"]["Content-Security-Policy"])
        out = self.get(f"/api/file?path={quote(str(self.proj / 'page.html'))}&token={server.PAGE_TOKEN}")
        self.assertEqual((out["code"], out["headers"]["Content-Security-Policy"]), (200, server.FILE_CSP))
        for bad, why in ((self.proj / "notes.py", "does not show .py"), (self.proj / ".env" / "key.txt", "hidden"),
                         (Path(self.dir.name) / "t.db", "only from the project folders"), (Path("/etc/hosts"), "only from"),
                         (self.proj / "scratch" / ".." / ".." / "t.db", "only from")):
            out = self.get(f"/api/file?path={quote(str(bad))}&token={server.PAGE_TOKEN}")
            self.assertEqual(out["code"], 404, bad)
            self.assertIn(why, out["body"]["error"], bad)

    def test_full_screen_opens_one_file_by_a_signed_key(self):
        # #4620: a tab of its own cannot send the token; its address carries a key for that one file.
        from urllib.parse import parse_qs, urlsplit, quote
        shot = str(self.proj / "scratch" / "shot.png")
        out = self.get(f"/api/view-key?target={quote(shot)}&session=ag&item={self.it}&token={server.PAGE_TOKEN}")
        url = out["body"]["url"]
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        page = self.get(url)
        self.assertEqual((page["code"], page["ctype"]), (200, "text/html; charset=utf-8"))
        self.assertIn(b"shot.png", page["body"])
        self.assertIn(f"#{self.it}".encode(), page["body"])
        got = self.get(f"/api/file?path={quote(shot)}&e={q['e']}&k={q['k']}")
        self.assertEqual((got["code"], got["ctype"]), (200, "image/png"))
        # The key opens nothing else, and not after its end.
        other = quote(str(self.proj / "page.html"))
        self.assertEqual(self.get(f"/api/file?path={other}&e={q['e']}&k={q['k']}")["code"], 403)
        self.assertEqual(self.get(url.replace(q["k"], "0" * 40))["code"], 403)
        old_end = str(int(time.time()) - 5)
        self.assertEqual(self.get(f"/api/file?path={quote(shot)}&e={old_end}&k={server.view_key(self.c, shot, old_end)}")["code"], 403)
        self.assertEqual(self.get("/api/view-key?target=javascript:x&token=" + server.PAGE_TOKEN)["code"], 409)
        self.assertEqual(self.get(f"/api/view-key?target={quote(shot)}")["code"], 403)  # no token

    def test_the_artifacts_of_a_session(self):
        shot = str(self.proj / "scratch" / "shot.png")
        self.write(claude_line("assistant", [{"type": "tool_use", "id": "w1", "name": "Write",
                                              "input": {"file_path": str(self.proj / "page.html"), "content": "<p>"}}], "tool_use",
                               at="2026-11-08T10:01:00Z")
                   + claude_line("assistant", [{"type": "text", "text": f"Saved {shot}; see http://localhost:5173/app "
                                                "and https://claude.ai/artifact/ab12-cd and /etc/hosts.md"}], "end_turn",
                                 at="2026-11-08T10:02:00Z"))
        allowed = lambda f: self.get(f"/api/file?path={f}&token={server.PAGE_TOKEN}")["code"] == 200
        got = {a["target"]: a for a in sessions.artifacts(self.c, {}, allowed, self.root, self.croot)}
        self.assertEqual(got[str(self.proj / "page.html")]["how"], "wrote")
        self.assertEqual((got[shot]["kind"], got[shot]["session"]), ("file", "ag"))
        self.assertEqual(got["http://localhost:5173/app"]["kind"], "page")
        self.assertEqual(got["https://claude.ai/artifact/ab12-cd"]["kind"], "artifact")
        self.assertNotIn("/etc/hosts.md", got)  # outside the project folders

    def test_the_frame_check_needs_a_web_address(self):
        self.assertEqual(server.frame_check("javascript:alert(1)")["frame"], False)
        self.assertEqual(self.get("/api/frame-check?url=x")["code"], 403)


class NotConnected(TmuxBase):
    """#4621: a session that ran no maxpm command and wrote no transcript is not working."""

    def test_a_session_that_never_connected_is_starting_or_ended(self):
        core.register(self.c, "cx")
        self.tmux._pane(name="cx")
        p = self.tmux.panes[-1]
        p.update(agent="cx", screen="")
        t = server.agent_terminals(self.c)
        card = next(c for c in sessions.views(self.c, t, self.root, self.croot) if c["name"] == "cx")
        self.assertEqual(card["state"], "starting")
        card = next(c for c in sessions.views(self.c, t, self.root, self.croot, ended_panes={p["id"]}) if c["name"] == "cx")
        self.assertEqual((card["state"], card["state_label"]), ("ended", "ended before its first maxpm command"))

    def test_codex_gets_no_daemon_when_it_knows_the_flag(self):
        self.addCleanup(setattr, core, "CODEX_FLAGS", set())
        core.CODEX_FLAGS = {"--no-daemon", "--sandbox"}
        self.assertIn(" --no-daemon ", core.build_command("codex", {"prompt": "go"}) + " ")
        self.assertEqual(core.build_command("codex", {"prompt": "go", "args": "--no-daemon"}).count("--no-daemon"), 1)
        core.CODEX_FLAGS = set()
        self.assertNotIn("--no-daemon", core.build_command("codex", {"prompt": "go"}))


class OutsideTmux(unittest.TestCase):
    """#4599: go and manage say when a session runs outside tmux, where the Sessions page cannot type into it."""

    def test_tmux_is_known_by_term_program_too(self):
        cc = {"CLAUDECODE": "1"}
        self.assertFalse(core.outside_tmux({**cc, "TMUX": "/tmp/tmux-501/default,1,2", "TERM_PROGRAM": "tmux"}))
        self.assertFalse(core.outside_tmux({**cc, "TMUX": "/tmp/tmux-501/default,1,2"}))  # an old tmux sets no TERM_PROGRAM
        # A Terminal app started from a tmux pane passes TMUX to its tabs: such a tab is outside tmux.
        self.assertTrue(core.outside_tmux({**cc, "TMUX": "/tmp/tmux-501/default,1,2", "TERM_PROGRAM": "Apple_Terminal"}))
        self.assertTrue(core.outside_tmux({**cc, "TERM_PROGRAM": "iTerm.app"}))
        self.assertTrue(core.outside_tmux({"CODEX_THREAD_ID": TID}))
        self.assertFalse(core.outside_tmux({"TERM_PROGRAM": "Apple_Terminal"}))  # a person's shell, not an agent CLI
        self.assertFalse(core.outside_tmux({**cc, "MAXPM_CHAT": "1"}))  # a chat app has no terminal

    def test_the_go_briefing_says_so(self):
        from unittest import mock
        from river import cli
        line = "This session runs outside tmux"
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1", "TERM_PROGRAM": "Apple_Terminal"}):
            self.assertIn(line, cli._tmux_line()[0])
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1", "TERM_PROGRAM": "tmux", "TMUX": "x,1,2"}):
            self.assertEqual(cli._tmux_line(), [])


if __name__ == "__main__":
    unittest.main()
