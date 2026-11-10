"""The Sessions tab of maxpm serve (#4592): running sessions and a read-only view of their transcripts. (#4656: the
tests of each view are in a file of their own, tests/test_view_<view>.py.)"""
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
        self.assertEqual((got["model"]["value"], got["model"]["source"], got["model"]["live"]), ("opus", "session", True))
        # #4633: the models of its own provider, the launch dialog's list; the id it reports stays shown.
        self.assertEqual((got["model"]["kind"], got["model"]["choices"], got["model"]["reported"]),
                         ("choice", ["haiku", "sonnet", "opus", "fable"], "claude-opus-5-5"))
        launch = next(o for o in core.launch_options(self.c) if o["platform"] == "claude-code")
        self.assertEqual([m["name"] for m in launch["models"]], got["model"]["choices"])
        cx = core.platform_models(self.c, "codex")
        self.assertEqual([m["name"] for m in cx], ["luna", "terra", "sol", "astra"])
        self.assertEqual(core.model_name_of(cx, cx[2]["id"]), "sol")
        self.assertIsNone(core.model_name_of(cx, "gpt-9"))
        with self.assertRaises(core.RiverError):
            sessions.check_value(sessions.setting_spec("codex", "model"), "opus", self.c, "codex")
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

    def test_a_question_asked_before_the_persons_last_prompt_does_not_wait(self):
        """#4995: a session with no hooks: the person answered its question in its chat, so the card neither shows the
        question in 'Waits for you' nor says 'waits for an answer'; a question asked after that prompt still waits."""
        q = core.send(self.c, "question", "Do you agree?", to="mark", item=self.it, actor="ag")["id"]
        (card,) = self.cards()
        self.assertEqual((card["state_label"], [w["id"] for w in card["waiting"]]), ("waits for an answer", [q]))
        later = (core.now() + core.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S.123Z")
        self.write(claude_line("user", "[maxpm note from boss] x", at=later))
        self.assertEqual(self.cards()[0]["state_label"], "waits for an answer")  # MaximizePM's message: no answer
        self.write(claude_line("user", "ok I agree", at=later))
        (card,) = self.cards()
        self.assertEqual((card["state_label"], card["waiting"]), ("working", []))
        with core.tx(self.c):
            self.c.execute("UPDATE messages SET created_at=? WHERE id=?",
                           (core.iso(core.now() + core.timedelta(minutes=2)), q))
        (card,) = self.cards()
        self.assertEqual((card["state_label"], [w["id"] for w in card["waiting"]]), ("waits for an answer", [q]))

    def test_an_old_question_is_marked_so_it_does_not_pop_up(self):
        """#5001 (mark: 'maybe the questions should just not open by default if they are old'): a question older than
        question_popup_max is marked old; the page opens the dialog by itself only for one that is not."""
        q = core.send(self.c, "question", "Do you agree?", to="mark", item=self.it, actor="ag")["id"]
        (card,) = self.cards()
        self.assertEqual([(w["id"], w["old"]) for w in card["waiting"]], [(q, False)])
        with core.tx(self.c):
            self.c.execute("UPDATE messages SET created_at=? WHERE id=?",
                           (core.iso(core.now() - core.timedelta(minutes=11)), q))
        (card,) = self.cards()
        self.assertEqual(([(w["id"], w["old"]) for w in card["waiting"]], card["state_label"]),
                         ([(q, True)], "waits for an answer"))  # still on the card, behind 'Answer it'
        core.config_set(self.c, "question_popup_max", "0s")
        self.assertFalse(self.cards()[0]["waiting"][0]["old"])
        js = (Path(sessions.__file__).parent / "static" / "views" / "sessions.js").read_text()
        self.assertIn("const popsUp = (s) => (s.waiting || []).filter(w => !w.old);", js)
        self.assertIn("if (!s || !popsUp(s).some(", js)

    def test_a_person_pins_a_session_to_the_top_until_it_ends(self):
        """#5007 (mark: 'a way to pin sessions to the top of the worker sessions area'): the pin is stored by maxpm for
        the person, shows on the card, in who and its --json, and goes when the session's agent row goes."""
        import contextlib
        import io
        from river import cli
        with self.assertRaisesRegex(core.RiverError, "a pin is a person's"):
            core.session_pin(self.c, "ag", "ag")
        self.assertEqual(server.OPS["session_pin"](self.c, {"agent": "ag"}, "mark")["pinned_by"], ["mark"])
        self.assertEqual(self.cards()[0]["pinned_by"], ["mark"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(["--as", "mark", "-q", "who"])
        self.assertIn("pinned by: mark (top of the Sessions tab)", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(["--as", "mark", "-q", "session", "unpin", "ag"])
        self.assertEqual(out.getvalue().strip(), "ag: unpinned for mark")
        self.assertEqual(self.cards()[0]["pinned_by"], [])
        with contextlib.redirect_stdout(io.StringIO()):
            cli.run(["--as", "mark", "-q", "session", "pin", "ag"])
        self.assertEqual(core.agent_status(self.c, "ag")["pinned_by"], ["mark"])
        # The session ends: its agent row goes, and the pin with it.
        core.release(self.c, self.it, "ag")
        core.unregister(self.c, "ag", "ag")
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM session_pins").fetchone()[0], 0)
        # The page: a pin button on worker rows only, and pinned workers first in their group.
        js = (Path(sessions.__file__).parent / "static" / "views" / "sessions.js").read_text()
        self.assertIn('if (s.role === "manager" || s.role === "expert") return "";', js)
        self.assertIn("workers.sort((a, b) => isPinned(b) - isPinned(a));", js)

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
        # #4948: a person talks with it while its wait runs in the background: a turn after the wait started.
        t = core.now()
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET wait_at=?, activity_at=? WHERE name='ag'",
                           (core.iso(t), core.iso(t + core.timedelta(seconds=5))))
        self.assertEqual(self.cards()[0]["state_label"], "working")
        with core.tx(self.c):  # its turn ends with a new background wait
            self.c.execute("UPDATE agents SET wait_at=? WHERE name='ag'", (core.iso(t + core.timedelta(seconds=9)),))
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
        self.assertEqual((card["provider"]["id"], card["provider"]["label"]), ("codex", "Codex"))  # #4627
        cc = sessions.provider(None, "claude")
        self.assertEqual((cc["id"], cc["label"]), ("claude-code", "Claude Code"))
        # #4626: both CLIs record their own computer's microphone, so the page dictates text for each.
        self.assertEqual((cc["voice"], card["provider"]["voice"]), ("dictate", "dictate"))
        self.assertIn("/voice", cc["voice_cli"])
        self.assertIsNone(sessions.provider("other-cli")["voice"])
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


@unittest.skipIf(os.name == "nt", "tmux adoption uses Unix terminals and signals")
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

    def test_a_process_in_a_tmux_pane_is_never_ended(self):
        """#5060: two agents had one pid (a command with --as for another agent wrote it), the map from pids to names
        kept one of them, and tmux_adopt took the other for a session outside tmux: it ended the manager in its pane.
        Each name of a pid maps to the pane, and the terminal device of the process decides before an end."""
        from unittest import mock
        self.quiet()
        self.tmux.session = True
        self.tmux._pane(name="manager")  # pane %0, on /dev/ttys000
        core.register(self.c, "mgr")
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET pid=?, host=? WHERE name IN ('ag', 'mgr')", (self.proc.pid, core.this_host()))
        server.PS_RUNNER = lambda pids: f" {self.proc.pid} ttys000\n"
        self.addCleanup(setattr, server, "PS_RUNNER", None)
        self.assertEqual(server.agent_terminals(self.c), {"ag": "%0", "mgr": "%0"})
        with mock.patch.object(server, "agent_terminals", lambda c: {}):  # the map loses it: the tty still decides
            self.assertEqual(server.adopt_sessions(self.c), {})
        self.assertIsNone(self.proc.poll())  # still runs

        def no_ps(pids):
            raise OSError("ps: not permitted")
        server.PS_RUNNER = no_ps  # no answer from ps: nothing moves
        self.assertEqual(server.adopt_sessions(self.c), {})
        self.assertIsNone(self.proc.poll())

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


class Uploads(TmuxBase):
    """#4610, #4629: images attach; arbitrary files go to the session as readable paths."""

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

    def test_files_keep_their_type_and_bytes_and_have_separate_paths(self):
        import base64
        for provider in ("claude-code", "codex"):
            with core.tx(self.c):
                self.c.execute("UPDATE agents SET agent_type=? WHERE name='ag'", (provider,))
            payloads = [("report.pdf", b"%PDF-1.4\n"), ("data.csv", b"a,b\n1,2"),
                        ("archive.zip", b"PK\x03\x04\x00\xff"), ("LICENSE", b""), ("shot.png", b"not an image")]
            r = server.OPS["session_send"](self.c, {"agent": "ag", "files": [
                {"name": name, "data": base64.b64encode(raw).decode()} for name, raw in payloads]}, "mark")
            self.assertEqual(r["images"], [])
            self.assertEqual(len(r["files"]), 5)
            self.assertEqual(r["text"], "Attached files:\n" + "\n".join(r["files"]))
            for path, (name, raw) in zip(r["files"], payloads):
                self.assertEqual(Path(path).read_bytes(), raw)
                self.assertTrue(path.endswith(name + (".bin" if name == "shot.png" else "")))
                self.assertEqual(server.upload_file("ag/" + Path(path).name), (raw, "application/octet-stream"))
            pastes = [c[-1] for c in self.tmux.calls if c[0] == "set-buffer"]
            self.assertEqual(pastes[-1], " " + r["text"])
            self.write(claude_line("user", r["text"]))
            last = sessions.view(self.c, "ag", None, self.root, self.croot)["entries"][-1]
            self.assertEqual([f["name"] for f in last["files"]], [n + (".bin" if n == "shot.png" else "") for n, _ in payloads])
            self.assertNotIn("images", last)

    def test_mixed_files_and_images_and_safe_names(self):
        import base64
        png = b"\x89PNG\r\n\x1a\n" + b"\0" * 20
        r = server.session_send(self.c, "ag", "compare", "mark", files=[
            {"name": "../unsafe/notes $(whoami)\n.txt", "data": base64.b64encode(b"notes").decode()},
            {"name": "screenshot.jpeg", "data": base64.b64encode(png).decode()}])
        self.assertTrue(Path(r["files"][0]).name.endswith("notes___whoami__.txt"))
        self.assertTrue(r["images"][0].endswith("screenshot.png"))
        self.assertEqual([c[-1] for c in self.tmux.calls if c[0] == "set-buffer"],
                         [r["images"][0], " " + r["text"]])
        self.assertIn(r["files"][0] + "\n\ncompare", r["text"])

    def test_bad_files_size_count_and_authorization(self):
        for files, why in (([{"data": "not base64!"}], "base64"),
                           ([{"data": None}], "base64"), ([None], "file needs"),
                           ([{"data": "", "name": 1}], "name must be text"),
                           ([{"data": "AAAA"}] * 6, "at most 5"), ({"data": "AAAA"}, "at most 5")):
            with self.assertRaisesRegex(core.RiverError, why):
                server.session_send(self.c, "ag", "x", "mark", files=files)
        from unittest.mock import patch
        with patch.object(server, "UPLOAD_MAX", 2):
            with self.assertRaisesRegex(core.RiverError, "at most"):
                server.session_send(self.c, "ag", "x", "mark", files=[{"data": "AAAA"}])
        with self.assertRaisesRegex(core.RiverError, "only a person"):
            core.register(self.c, "bot")
            server.session_send(self.c, "ag", "x", "bot", files=[{"data": "AAAA"}])
        # A bad second file leaves no orphan from the first and types nothing into the terminal.
        before = list(server.uploads_dir().glob("ag/*"))
        with self.assertRaisesRegex(core.RiverError, "base64"):
            server.session_send(self.c, "ag", "x", "mark", files=[{"data": "AAAA"}, {"data": "!"}])
        self.assertEqual(list(server.uploads_dir().glob("ag/*")), before)
        self.assertFalse(any(c[0] == "paste-buffer" for c in self.tmux.calls))

    def test_active_content_downloads_and_symlinks_are_refused(self):
        import base64
        raw = b"<script>alert(1)</script>"
        path = Path(server.save_upload("ag", base64.b64encode(raw).decode(), "page.html"))
        h = server.Handler.__new__(server.Handler)
        h.path, h.client_address, h.headers, out = "/api/upload/ag/" + path.name, ("127.0.0.1", 5555), {"Host": "localhost:8765"}, {}
        h._send = lambda code, body, ctype=None, headers=None: out.update(code=code, body=body, ctype=ctype, headers=headers)
        h.do_GET()
        self.assertEqual((out["code"], out["ctype"], out["body"]), (200, "application/octet-stream", raw))
        self.assertEqual(out["headers"]["Content-Disposition"], "attachment")
        self.assertEqual(out["headers"]["X-Content-Type-Options"], "nosniff")
        self.assertIn("sandbox", out["headers"]["Content-Security-Policy"])
        link = path.with_name("link.txt")
        link.symlink_to(Path(self.dir.name) / "t.db")
        self.assertIsNone(server.upload_file("ag/link.txt"))
        (server.uploads_dir() / "alias").symlink_to(path.parent, target_is_directory=True)
        self.assertIsNone(server.upload_file("alias/" + path.name))


class UploadComposer(TmuxBase):
    """Exercise paste, drop, the picker, limits, and the file-only Send in a real browser."""

    def test_generic_files_in_the_composer(self):
        import subprocess
        import threading
        from unittest.mock import patch
        from tests.test_server import _chrome
        if not _chrome():
            self.skipTest("no Chrome on this computer")
        driver = r'''
import { makeSessions } from "/views/sessions.js";
const q = s => document.querySelector(s);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const until = async (f, what) => { for (let i = 0; i < 150; i++) { if (f()) return; await sleep(100); } throw new Error(what); };
const check = (v, what) => { if (!v) throw new Error(what); };
const post = o => navigator.sendBeacon("/__result", JSON.stringify(o));
try {
  q("#actor").innerHTML = '<option value="mark">mark</option>';
  const ui = makeSessions({ isShown: () => true });
  ui.refresh();
  await until(() => q("#sessText").disabled === false && q("#sessTitle").textContent === "ag", "composer ready");
  check(q("#sessSend").disabled && !q("#sessAttach").disabled, "empty Send off, Attach available");
  const dt = new DataTransfer(); dt.items.add(new File(["a,b\n1,2"], "table.csv", { type: "text/csv" }));
  q("#sessText").dispatchEvent(new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true }));
  const drop = new DataTransfer(); drop.items.add(new File(["PK"], "archive.zip", { type: "application/zip" }));
  q("#sessBar").closest(".chat-foot").dispatchEvent(new DragEvent("drop", { dataTransfer: drop, bubbles: true, cancelable: true }));
  const pick = new DataTransfer(); pick.items.add(new File(["%PDF-1.4"], "report.pdf", { type: "application/pdf" }));
  q("#sessPickFiles").files = pick.files;
  q("#sessPickFiles").dispatchEvent(new Event("change", { bubbles: true }));
  await until(() => !q("#sessSend").disabled, "files read");
  check(q("#sessFiles").children.length === 3, "paste, drop, picker all accept files");
  check(!q("#sessPickFiles").hasAttribute("accept"), "picker accepts every type");
  q('#sessFiles [data-rm="1"]').click();
  check(q("#sessFiles").children.length === 2 && !q("#sessFiles").textContent.includes("archive.zip"), "remove file");
  const many = new DataTransfer();
  for (let i = 0; i < 6; i++) many.items.add(new File(["text"], "file" + i));
  q("#sessPickFiles").files = many.files; q("#sessPickFiles").dispatchEvent(new Event("change"));
  await until(() => !q("#sessSend").disabled, "many files read");
  check(q("#sessFiles").children.length === 5, "five-file limit includes reads in progress");
  while (q("#sessFiles").children.length > 2) q('#sessFiles [data-rm="2"]').click();
  const big = new DataTransfer(); big.items.add(new File([new Uint8Array(10 * 1024 * 1024 + 1)], "large.bin"));
  q("#sessPickFiles").files = big.files; q("#sessPickFiles").dispatchEvent(new Event("change"));
  check(q("#sessFiles").children.length === 2, "size limit");
  q("#sessBar").requestSubmit();
  await until(() => q(".msg-files a") && q("#sessFiles").children.length === 0, "files sent and chat links shown");
  const links = [...document.querySelectorAll(".msg-files a")];
  check(links.length === 2 && links[0].download === "table.csv" && links[1].download === "report.pdf", "sent filename links");
  const download = await fetch(links[0].href);
  check(await download.text() === "a,b\n1,2", "download keeps exact bytes");
  check(download.headers.get("Content-Disposition") === "attachment", "download response");
  check(q("#sessSend").disabled, "Send off after sending");
  post({ files: links.map(a => a.download) });
} catch (e) { post({ error: String(e.stack || e) }); }
'''
        page = server.page_html("/").replace(
            b'<script type="module" src="app.js" onerror="riverLoadFailed()"></script>',
            b'<script type="module" src="/__drive.js"></script>')
        got, result = threading.Event(), {}

        class H(server.Handler):
            def do_GET(self):
                if self.path == "/":
                    return self._send(200, page, "text/html; charset=utf-8")
                if self.path == "/__drive.js":
                    return self._send(200, driver.encode(), "text/javascript; charset=utf-8")
                return super().do_GET()

            def do_POST(self):
                if self.path == "/__result":
                    result.update(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                    got.set()
                    return self._send(204, b"")
                return super().do_POST()

        with patch.object(core, "transcripts_root", lambda: self.root):
            try:
                httpd = server._Server(("127.0.0.1", 0), H)
            except PermissionError:
                self.skipTest("no local port here")
            httpd.daemon_threads = True
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            chrome = subprocess.Popen([
                _chrome(), "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                "--disable-extensions", f"--user-data-dir={self.dir.name}/browser",
                f"http://127.0.0.1:{httpd.server_address[1]}/"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                ran = got.wait(30)
            finally:
                chrome.kill()
                chrome.wait()
                httpd.shutdown()
                httpd.server_close()
        self.assertTrue(ran, "browser test did not report")
        self.assertNotIn("error", result, result.get("error"))
        self.assertEqual(result["files"], ["table.csv", "report.pdf"])
        paths = list(server.uploads_dir().glob("ag/*"))
        self.assertEqual(len(paths), 2)
        self.assertEqual([c[0] for c in self.tmux.calls].count("paste-buffer"), 1)


class PromptHistory(TmuxBase):
    """#4855: the message box's Up and Down bring back the prompts sent to that session, newest first."""

    def setUp(self):
        super().setUp()
        self.pane["screen"] = "❯ \n"

    def prompts(self, agent="ag"):
        h = server.Handler.__new__(server.Handler)
        h.path, h.client_address, h.headers, out = (f"/api/session-prompts?agent={agent}", ("127.0.0.1", 5555),
                                                    {"Host": "localhost:8765"}, {})
        h._send = lambda code, body, ctype=None, headers=None: out.update(code=code, body=body)
        h.do_GET()
        self.assertEqual(out["code"], 200)
        return out["body"]["prompts"]

    def test_the_server_keeps_each_sent_prompt_for_its_session(self):
        import base64
        send = lambda text, **kw: server.OPS["session_send"](self.c, {"agent": "ag", "text": text, **kw}, "mark")
        send("first one")
        send("two\nlines")
        send("two\nlines")  # the same again: kept once
        send("", files=[{"name": "a.csv", "data": base64.b64encode(b"a,b").decode()}])  # files only: no prompt
        send("  with a file  ", files=[{"name": "b.csv", "data": base64.b64encode(b"a,b").decode()}])
        self.assertEqual(self.prompts(), ["with a file", "two\nlines", "first one"])  # the text only, newest first
        self.assertEqual(self.prompts("other"), [])  # one list for each session
        with self.assertRaises(core.RiverError):
            send("")  # refused: nothing kept
        old = sessions.PROMPTS_KEEP
        sessions.PROMPTS_KEEP = 2
        self.addCleanup(setattr, sessions, "PROMPTS_KEEP", old)
        send("third")
        self.assertEqual(self.prompts(), ["third", "with a file"])

    def test_up_and_down_in_the_message_box(self):
        for t in ("oldest", "line one\nline two", "newest"):
            sessions.record_prompt(self.c, "ag", t, "mark")
        core.register(self.c, "bee")
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET pid=NULL WHERE name='bee'")
        self.tmux._pane(name="bee")
        self.tmux.panes[-1].update(agent="bee", screen="❯ \n")
        sessions.record_prompt(self.c, "bee", "for bee", "mark")
        driver = DRIVE_HEAD + r'''
const box = () => q("#sessText");
const key = (k, at) => {
  if (at != null) box().setSelectionRange(at, at);
  const e = new KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true });
  box().dispatchEvent(e);
  return e.defaultPrevented;
};
const up = at => key("ArrowUp", at), down = at => key("ArrowDown", at);
try {
  await until(() => q("#sessTitle").textContent === "ag" && !box().disabled, "the box of ag");
  box().focus();
  box().value = "my draft"; box().dispatchEvent(new Event("input"));
  await until(() => up(), "Up brings the newest prompt");
  check(box().value === "newest", "newest first: " + box().value);
  check(up() && box().value === "line one\nline two", "then the one before");
  check(box().selectionStart === 8, "the caret stays on the first line: " + box().selectionStart);
  check(up() && box().value === "oldest", "Up on the first line goes on");
  check(!up() && box().value === "oldest", "no older prompt: the box stays");
  check(down() && box().value === "line one\nline two", "Down goes forward");
  check(!down(3), "Down with the caret on the first line of two moves the caret only");
  check(box().value === "line one\nline two", "the text stays");
  check(down(box().value.length) && box().value === "newest", "Down on the last line goes forward");
  check(down() && box().value === "my draft", "past the newest: the draft again");
  check(!down() && box().value === "my draft", "nothing after the draft");
  box().value = "a\nb"; box().dispatchEvent(new Event("input"));
  check(!up(3) && box().value === "a\nb", "Up on the second line moves the caret only");
  box().value = "sent now"; box().dispatchEvent(new Event("input"));
  q("#sessBar").requestSubmit();
  await until(() => box().value === "", "sent");
  check(up() && box().value === "sent now", "the prompt just sent comes first");
  check(down() && box().value === "", "past it: the empty box");
  q('[data-session="bee"] .srow-main').click();
  await until(() => q("#sessTitle").textContent === "bee" && !box().disabled, "the box of bee");
  await until(() => up(), "Up in bee's box");
  check(box().value === "for bee", "bee's own list: " + box().value);
  check(!up() && box().value === "for bee", "only bee's prompts");
  post({ ok: true });
} catch (e) { post({ error: String(e.stack || e) }); }
'''
        drive_page(self, driver)
        self.assertEqual(sessions.sent_prompts(self.c, "ag")[0], "sent now")


class RowLayout(unittest.TestCase):
    """#4675: a long state chip wraps inside the row's own column, never over the item column on the right."""

    def test_a_long_state_chip_stays_in_its_column(self):
        import subprocess
        import tempfile
        from tests.test_server import _chrome
        if not _chrome():
            self.skipTest("no Chrome on this computer")
        static = Path(server.__file__).parent / "static"
        page = f"""<!doctype html><meta charset="utf-8">
<link rel="stylesheet" href="{(static / 'app.css').as_uri()}"><link rel="stylesheet" href="{(static / 'views/sessions.css').as_uri()}">
<body style="width:300px;margin:8px"><div class="srow" id="row"><div class="srow-main"><div class="srow-h"><b>maximizepm-d450</b>
<span class="spacer"></span><span class="chip c-waiting" id="chip">not connected yet: it ran no maxpm command</span></div>
<div class="sub">last line</div></div><button class="srow-it" id="it"><b>#4675</b><span>A long item title</span></button></div>
<pre id="out"></pre><script>
const c = document.getElementById("chip").getBoundingClientRect(), i = document.getElementById("it").getBoundingClientRect();
document.getElementById("out").textContent = "RESULT " + JSON.stringify({{chipRight: c.right, itemLeft: i.left}});
</script>"""
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "row.html"
            f.write_text(page)
            try:
                dom = subprocess.run([_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
                                      "--allow-file-access-from-files", f"--user-data-dir={d}/browser",
                                      "--window-size=320,300", "--dump-dom", f.as_uri()],
                                     capture_output=True, text=True, timeout=60).stdout
            except (OSError, subprocess.TimeoutExpired) as e:
                self.skipTest(f"Chrome did not run here: {e}")
        if "RESULT " not in dom:
            self.skipTest("Chrome gave no page here")
        got = json.loads(dom.split("RESULT ", 1)[1].split("<", 1)[0].replace("&quot;", '"'))
        self.assertLessEqual(got["chipRight"], got["itemLeft"], got)


def drive_page(test, driver, wait=40, routes=None):
    """Load the real page at /sessions/ag in headless Chrome with driver (a module) after app.js, and return what
    the driver posts to /__result. Skips with no Chrome or no local port."""
    import subprocess
    import threading
    from unittest.mock import patch
    from tests.test_server import _chrome
    if not _chrome():
        test.skipTest("no Chrome on this computer")
    page = server.page_html("/sessions/ag").replace(b"</body>", b'<script type="module" src="/__drive.js"></script></body>')
    got, result = threading.Event(), {}

    class H(server.Handler):
        def do_GET(self):
            if self.path.split("?")[0] == "/sessions/ag":
                return self._send(200, page, "text/html; charset=utf-8")
            if self.path == "/__drive.js":
                return self._send(200, driver.encode(), "text/javascript; charset=utf-8")
            if self.path in (routes or {}):
                return self._send(200, routes[self.path]())
            return super().do_GET()

        def do_POST(self):
            if self.path == "/__result":
                result.update(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                got.set()
                return self._send(204, b"")
            return super().do_POST()

    with patch.object(core, "transcripts_root", lambda: test.root):
        try:
            httpd = server._Server(("127.0.0.1", 0), H)
        except PermissionError:
            test.skipTest("no local port here")
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        chrome = subprocess.Popen([
            _chrome(), "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
            "--disable-extensions", "--window-size=1500,900", f"--user-data-dir={test.dir.name}/browser",
            f"http://127.0.0.1:{httpd.server_address[1]}/sessions/ag?as=mark"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            ran = got.wait(wait)
        finally:
            chrome.kill()
            chrome.wait()
            httpd.shutdown()
            httpd.server_close()
    test.assertTrue(ran, "browser test did not report")
    test.assertNotIn("error", result, result.get("error"))
    return result


DRIVE_HEAD = r'''
const q = s => document.querySelector(s);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const until = async (f, what) => { for (let i = 0; i < 150; i++) { if (f()) return; await sleep(100); } throw new Error(what); };
const check = (v, what) => { if (!v) throw new Error(what); };
const post = o => navigator.sendBeacon("/__result", JSON.stringify(o));
'''


class PanelLists(Base):
    """#4685: the chat stays in sight; Items and Artifacts are lists in the side panel, and an item opens in a tab
    there (from the lists, from a #<id> in the chat), at /sessions/<session>/items/<id>; Edit in the drawer opens
    the drawer. Drives the real page in Chrome."""

    def test_items_and_artifacts_open_in_the_side_panel_beside_the_chat(self):
        self.write(claude_line("assistant", [{"type": "text", "text": f"Working on #{self.it} now."}], "end_turn",
                               at="2026-11-08T10:05:00Z"))
        driver = DRIVE_HEAD + r'''
const chat = () => q("#sessBody").offsetParent !== null && q("#sessCol").textContent.includes("I read the header first.");
try {
  await until(() => q("#sessTitle").textContent === "ag" && chat(), "the chat of ag");
  check(!q("[data-sview]"), "no Chat / Items / Artifacts tabs in the middle");
  q('[data-sp-open="items"]').click();
  await until(() => q('.sp-list[data-list="items"] .si-row [data-open]'), "the Items list in the panel");
  check(chat(), "the chat stays with the Items list");
  q('.sp-list[data-list="items"] .si-row [data-open]').click();
  await until(() => q(".sp-item h3") && q(".sp-item h3").textContent === "header", "the item in a panel tab");
  check(chat() && !q("#drawer").classList.contains("open"), "the chat stays, and the drawer stays closed");
  check(/\/sessions\/ag\/items\/\d+$/.test(location.pathname), "the address names the item: " + location.pathname);
  q('[data-sp-list="artifacts"]').click();
  await until(() => q('.sp-list[data-list="artifacts"]'), "the Artifacts list");
  const ref = [...document.querySelectorAll("#sessCol [data-open]")].pop();
  check(ref, "a #<id> link in the chat"); ref.click();
  await until(() => q(".sp-item h3"), "the chat's #<id> opens in the panel");
  check(!q("#drawer").classList.contains("open") && chat(), "not in the drawer");
  check(document.querySelectorAll(".sp-tab:not(.sp-tab-list)").length === 1, "one tab for the item, opened twice");
  q("[data-drawer-open]").click();
  await until(() => q("#drawer").classList.contains("open"), "Edit in the drawer opens the drawer");
  post({ path: location.pathname, tabs: [...document.querySelectorAll(".sp-tab")].map(t => t.textContent.replace("✕", "").trim()) });
} catch (e) { post({ error: String(e.stack || e) }); }
'''
        result = drive_page(self, driver)
        self.assertEqual(result["path"], f"/sessions/ag/items/{self.it}")
        self.assertEqual(result["tabs"], ["Items", "Artifacts", f"◇ #{self.it} header"])


class PanelOneTab(Base):
    """#5000: the same claude.ai artifact, opened from two links of different form and from another session's link,
    is one tab in the side panel; a second open shows that tab, and its label names who opened it last."""

    def test_one_tab_for_one_artifact(self):
        self.write(claude_line("assistant", [{"type": "text", "text": "Published [the page](https://claude.ai/artifact/AbC12?v=1) "
                                                                      "and see https://claude.ai/artifact/AbC12 too"}],
                               "end_turn", at="2026-11-08T10:05:00Z"))
        core.register(self.c, "boss")
        driver = DRIVE_HEAD + r'''
const links = () => [...document.querySelectorAll('#sessCol a[href*="claude.ai/artifact"]')];
const arts = () => [...document.querySelectorAll(".sp-tab:not(.sp-tab-list)")].filter(t => t.title.includes("claude.ai/artifact"));
const onArt = () => q(".sp-tab.on") && q(".sp-tab.on").title.includes("claude.ai/artifact");
try {
  await until(() => q("#sessTitle").textContent === "ag" && links().length >= 2, "two links to the artifact in the chat");
  links()[0].click();
  await until(() => arts().length === 1, "a tab for the artifact");
  window.riverOpenInPanel("ag", "#ITEM");
  await until(() => q(".sp-tab.on") && !onArt(), "another tab on top");
  links()[1].click();
  await until(onArt, "the second link shows the artifact's tab");
  check(arts().length === 1, "one tab for the artifact, opened from two links: " + arts().map(t => t.title));
  window.riverOpenInPanel("ag", "#ITEM");
  await until(() => q(".sp-tab.on") && !onArt(), "the item on top again");
  window.riverOpenInPanel(null, "http://Claude.AI/artifact/AbC12#x");
  await until(onArt, "a third address of it shows its tab");
  post({ arts: arts().map(t => t.title), by: q(".sp-tab.on .sp-tab-l").dataset.by || "",
         fits: document.documentElement.scrollWidth <= window.innerWidth });
} catch (e) { post({ error: String(e.stack || e) }); }
'''.replace("ITEM", str(self.it))
        result = drive_page(self, driver)
        self.assertEqual(result["arts"], ["https://claude.ai/artifact/AbC12"])
        self.assertEqual(result["by"], "ag")
        self.assertTrue(result["fits"])


class ExpertCache(Base):
    """#5023: an expert's prompt cache from its transcript: warm with the minutes left, or cold, and what the next
    message costs at the read or the write price of the lifetime in force."""

    def usage(self, inp, read, w1=0, w5=0, out=100):
        return {"input_tokens": inp, "cache_read_input_tokens": read, "cache_creation_input_tokens": w1 + w5,
                "cache_creation": {"ephemeral_1h_input_tokens": w1, "ephemeral_5m_input_tokens": w5}, "output_tokens": out}

    def call(self, mid, usage, at, model="claude-opus-5-5"):
        return json.dumps({"type": "assistant", "timestamp": at, "message": {
            "id": mid, "model": model, "role": "assistant", "usage": usage,
            "content": [{"type": "text", "text": "ok"}]}}) + "\n"

    def state(self, minutes, **k):
        at = core.parse_iso("2026-11-08T12:00:00Z") + core.timedelta(minutes=minutes)
        return sessions.cache_state(self.c, "ag", self.root, self.croot, at=at, **k)

    def test_warm_then_cold_with_the_cost_of_the_next_message(self):
        core.expert_make(self.c, "ag", "the pricing talk", "site", "mark")
        # The call starts when the person's prompt is sent (11:59:30), not when its answer is written.
        self.write(claude_line("user", "What about tiers?", at="2026-11-08T11:59:30Z")
                   + self.call("m1", self.usage(10, 150_000, w1=50_000), "2026-11-08T12:00:00Z")
                   + self.call("m1", self.usage(10, 150_000, w1=50_000, out=890), "2026-11-08T12:00:01Z"))
        c = self.state(30)
        ctx = 10 + 150_000 + 50_000 + 890
        self.assertEqual((c["state"], c["ttl"], c["ttl_from"], c["context"], c["model"]),
                         ("warm", "1h", "transcript", ctx, "claude-opus-5-5"))
        self.assertEqual(c["left"], 29 * 60 + 30)
        self.assertEqual(c["next_usd"], round(ctx * 0.2 / 1e6, 4))  # Opus 5.5 cache read: $0.20 per million
        self.assertFalse(c["fresh"])
        c = self.state(61)
        self.assertEqual((c["state"], c["left"], c["next_usd"], c["next_eq"]), ("cold", 0, round(ctx * 8 / 1e6, 4), ctx * 2))
        self.assertTrue(c["fresh"])  # cold, and 200k tokens is above expert_fresh_min (150k)
        core.config_set(self.c, "expert_fresh_min", "300k")
        self.assertFalse(self.state(61)["fresh"])
        # A later call that only reads keeps the lifetime of the entry it read; a 5-minute write (usage overage)
        # makes it 5m.
        self.write(claude_line("user", "And the free tier?", at="2026-11-08T12:10:00Z")
                   + self.call("m2", self.usage(5, 200_900, out=10), "2026-11-08T12:10:05Z"))
        self.assertEqual((self.state(69)["state"], self.state(71)["state"]), ("warm", "cold"))
        self.write(claude_line("user", "ok", at="2026-11-08T12:20:00Z")
                   + self.call("m3", self.usage(5, 200_000, w5=900, out=10), "2026-11-08T12:20:05Z"))
        c = self.state(24)
        self.assertEqual((c["ttl"], c["state"]), ("5m", "warm"))
        c = self.state(26)
        self.assertEqual((c["state"], c["next_usd"]), ("cold", round(c["context"] * 5 / 1e6, 4)))
        # The Sessions tab: the expert's card and its chat view carry it; a worker's do not.
        card = next(x for x in sessions.views(self.c, {}, self.root, self.croot) if x["name"] == "ag")
        self.assertEqual(card["cache"]["ttl"], "5m")
        self.assertEqual(sessions.view(self.c, "ag", root=self.root, croot=self.croot)["cache"]["ttl"], "5m")

    def test_a_model_of_no_price_family_and_a_codex_log(self):
        core.config_set(self.c, "model_prices", "gpt=1.25")
        self.write(claude_line("user", "hi", at="2026-11-08T11:59:30Z")
                   + self.call("m1", self.usage(10, 0, w1=1000), "2026-11-08T12:00:00Z", model="mystery-1"))
        c = self.state(70)
        self.assertEqual((c["next_usd"], c["next_eq"]), (None, round(c["context"] * 2)))
        tid = "01a14170-419d-7241-afac-300125038d9d"
        p = self.croot / "2026" / "11" / "08" / f"rollout-2026-11-08T12-00-00-{tid}.jsonl"
        p.parent.mkdir(parents=True)
        p.write_text(codex_line("turn_context", {"model": "gpt-6-astra", "cwd": "/x"}, at="2026-11-08T12:00:00Z")
                     + codex_line("event_msg", {"type": "token_count", "info": {"last_token_usage": {
                         "input_tokens": 40_000, "cached_input_tokens": 30_000, "output_tokens": 500}}},
                         at="2026-11-08T12:00:10Z"))
        c = sessions.cache_state(self.c, "cx", at=core.parse_iso("2026-11-08T12:03:00Z"), found=("codex", p))
        self.assertEqual((c["state"], c["ttl"], c["ttl_from"], c["context"], c["model"]),
                         ("warm", "5m", "setting", 40_500, "gpt-6-astra"))
        self.assertEqual(c["next_usd"], round(40_500 * 0.125 / 1e6, 4))
        c = sessions.cache_state(self.c, "cx", at=core.parse_iso("2026-11-08T12:06:00Z"), found=("codex", p))
        self.assertEqual((c["state"], c["next_usd"]), ("cold", round(40_500 * 1.25 / 1e6, 4)))
        self.assertIsNone(sessions.cache_state(self.c, "nobody", self.root, self.croot))

    def test_the_cli_line(self):
        from river import cli
        c = {"state": "cold", "until": "2026-11-08T13:00:00Z", "left": 0, "ttl": "1h", "context": 210_000,
             "next_usd": 1.68, "next_eq": 420_000, "fresh": True}
        self.assertIn("next message ~$1.68; a fresh expert from its handoff costs less: maxpm expert fresh ag",
                      cli.cache_text(c, "ag"))
        self.assertIn("cache: warm, 12m left (1h cache)", cli.cache_text(dict(c, state="warm", left=720, fresh=False)))


class ExpertCachePage(Base):
    """#5023 in Chrome: a cold expert with a large context shows 'cache cold' with the cost on its row and in its
    chat header, and a message to it first asks: a fresh expert from the handoff, Send anyway, or Cancel."""

    def test_the_badge_and_the_question_before_a_send(self):
        core.expert_make(self.c, "ag", "the pricing talk", "site", "mark")
        usage = {"input_tokens": 10, "cache_read_input_tokens": 150_000, "cache_creation_input_tokens": 50_000,
                 "cache_creation": {"ephemeral_1h_input_tokens": 50_000, "ephemeral_5m_input_tokens": 0}, "output_tokens": 90}
        self.write(json.dumps({"type": "assistant", "timestamp": "2026-11-08T12:00:00Z", "message": {
            "id": "m1", "model": "claude-opus-5-5", "role": "assistant", "usage": usage,
            "content": [{"type": "text", "text": "Three tiers it is."}]}}) + "\n")
        driver = DRIVE_HEAD + r'''
try {
  await until(() => q('#sessExpertRows .cache-b') && q("#sessItem .cache-b"), "the cache badges");
  const row = q('#sessExpertRows .cache-b'), head = q("#sessItem .cache-b");
  const box = q("#sessText"), btn = q("#sessSend");
  box.disabled = false; box.value = "And the free tier?"; btn.disabled = false;
  q("#sessBar").requestSubmit();
  await until(() => q("#coldDlg") && !q("#coldDlg").classList.contains("hidden"), "the question before the send");
  const why = q("#coldDlg .cold-why").textContent;
  q('#coldDlg [data-cold="cancel"]').click();
  await until(() => q("#coldDlg").classList.contains("hidden"), "Cancel closes it");
  post({ row: row.textContent, rowClass: row.className, head: head.textContent, title: head.title, why,
         kept: box.value });
} catch (e) { post({ error: String(e.stack || e) }); }
'''
        result = drive_page(self, driver)
        self.assertEqual(result["row"], "cache cold · $1.60 next")  # 200,100 tokens at the 1-hour write price, $8
        self.assertEqual(result["rowClass"], "cache-b cold big")
        self.assertEqual(result["head"], result["row"])
        self.assertIn("Context 200k tokens on claude-opus-5-5", result["title"])
        self.assertIn("ag holds 200k tokens of context", result["why"])
        self.assertEqual(result["kept"], "And the free tier?")


class EndSession(Base):
    """#5041 (mark: 'i would like a feature to end a session.'): End on a session row and in its chat header. One
    confirm dialog says what happens: a worker gets a stop request, an expert ends with its notes kept, and End now
    (kill) shows only for a process on this computer, behind a second confirm. The row then shows 'ending'."""

    def test_the_card_says_who_asked_it_to_stop_and_whether_it_can_end_now(self):
        (card,) = self.cards()
        self.assertEqual((card["can_kill"], card["pid"], card["stop_by"]), (True, os.getpid(), None))
        server.OPS["stop_agent"](self.c, {"agent": "ag", "reason": "the plan changed"}, "mark")
        (card,) = self.cards()
        self.assertEqual((card["state"], card["state_label"], card["stop_by"], card["stop_reason"]),
                         ("stopping", "ending: asked to stop", "mark", "the plan changed"))
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET host='elsewhere' WHERE name='ag'")
        self.assertFalse(sessions.views(self.c, {"ag": "%1"}, self.root, self.croot)[0]["can_kill"])

    def test_end_on_the_row_asks_then_the_row_shows_ending(self):
        driver = DRIVE_HEAD + r"""
try {
  await until(() => q('#sessions [data-session="ag"] .srow-end') && q('#sessOpen [data-end="ag"]'), "End on the row and in the header");
  const head = q('#sessOpen [data-end="ag"]').textContent;
  q('#sessions [data-session="ag"] .srow-end').click();
  await until(() => q("#endDlg") && !q("#endDlg").classList.contains("hidden"), "the dialog");
  const ask = q("#endDlg .box").textContent.replace(/\s+/g, " ");
  const picked = q("#sessTitle").textContent;
  q("#endDlg [data-end-reason]").value = "the plan changed";
  q('#endDlg [data-end-go="stop"]').click();
  await until(() => q('#sessions [data-session="ag"]').dataset.state === "stopping", "the row shows ending");
  const chip = q('#sessions [data-session="ag"] .chip').textContent;
  await until(() => q('#sessOpen [data-end="ag"]').textContent === "Ending…", "the header says Ending");
  q('#sessOpen [data-end="ag"]').click();
  await until(() => !q("#endDlg").classList.contains("hidden"), "the dialog again");
  const again = q("#endDlg .box").textContent.replace(/\s+/g, " ");
  const nowHidden = q("#endDlg .end-now").classList.contains("hidden");
  q("#endDlg [data-end-now-ask]").click();
  const nowShown = !q("#endDlg .end-now").classList.contains("hidden");
  q("#endDlg [data-end-cancel]").click();  // never End now here: the process is this test's own
  await until(() => q("#endDlg").classList.contains("hidden"), "Cancel closes it");
  const m = await import("/components/endSession.js");
  const ex = m.endHtml({ name: "fab", role: "expert", state: "expert", holds: [{ id: 7, title: "the pricing talk" }] });
  const none = m.endHtml({ name: "w", role: "worker", state: "idle", holds: [], can_kill: false });
  post({ head, ask, picked, chip, again, nowHidden, nowShown, ex, none,
         op: m.endOp({ name: "fab", role: "expert" }, { how: "expert", reason: "", done: true }) });
} catch (e) { post({ error: String(e.stack || e) }); }
"""
        r = drive_page(self, driver)
        self.assertNotIn("error", r, r.get("error"))
        self.assertEqual(r["head"], "End")
        self.assertIn("MaximizePM asks ag to stop. It commits its finished work, releases #%d header with a note" % self.it, r["ask"])
        self.assertIn("End now…", r["ask"])
        self.assertEqual(r["picked"], "ag")  # End on the row does not open another session
        self.assertEqual(r["chip"], "ending: asked to stop")
        self.assertEqual(core.stop_request(self.c, "ag")["stop_reason"], "the plan changed")
        self.assertIn("ag was asked to stop by mark: the plan changed", r["again"])
        self.assertNotIn("End session", r["again"])
        self.assertEqual((r["nowHidden"], r["nowShown"]), (True, True))
        self.assertIn("Its task #7 the pricing talk", r["ex"])
        self.assertIn("goes back to the queue", r["ex"])
        self.assertIn("The talk is done: close its task", r["ex"])
        self.assertIn('data-end-go="expert"', r["ex"])
        self.assertNotIn("end-now", r["none"])
        self.assertEqual(r["op"], ["expert_end", {"name": "fab", "done": True}])


class WorkNames(Base):
    """#4695: a session shows what it works on; its agent id stays its address."""

    def name(self, who="ag"):
        return sessions.work_name(self.c, core.agent_status(self.c, who), root=self.root, croot=self.croot)

    def test_the_order_of_the_names(self):
        from river import notify
        self.assertEqual(self.name(), "header")  # the item it holds
        card = self.cards()[0]
        self.assertEqual((card["name"], card["title"]), ("ag", "header"))
        q = core.send(self.c, "question", "Push it?", to="mark", actor="ag")["id"]
        self.assertEqual(notify.compose(self.c, [{"summary": "s", "item_id": None, "message_id": q, "human": "mark"}])[0],
                         "MaximizePM: header")
        core.done(self.c, self.it, output="x", actor="ag")
        core.goal_add(self.c, "site", "launch", "the site is live")
        core.goal_own(self.c, "launch", actor="ag")
        self.assertEqual(self.name(), "Goal: launch")
        core.goal_release(self.c, "launch", actor="ag", no_handoff="test")
        core.target_add(self.c, "prod")
        core.target_own(self.c, "prod", actor="ag")
        self.assertEqual(self.name(), "Deploys prod")
        core.target_release(self.c, "prod", actor="ag")
        self.assertEqual(self.name(), "last: header")
        with core.tx(self.c):
            self.c.execute("UPDATE items SET assignee=NULL")
        self.write(json.dumps({"type": "custom-title", "customTitle": "#12 Fix the \"cart\" page", "sessionId": SID}) + "\n")
        self.assertEqual(self.name(), 'Fix the "cart" page')  # the Claude Code session title, without its #id
        self.path.write_text("")
        core.register(self.c, "web-app-6c44")
        self.assertEqual(self.name("web-app-6c44"), "web-app")
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET role='manager' WHERE name='ag'")
        self.assertEqual(self.name(), "Manager")
        self.assertEqual(sessions._short_title("x" * 80), "x" * 59 + "…")


class NewCode(Base):
    """#4690, #4850: an open page sees new page code: a bar with Reload; it loads itself again only for a new state shape."""

    def test_page_build_follows_the_files(self):
        from unittest import mock
        f = Path(self.dir.name) / "app.js"
        f.write_text("1")
        server._PAGE_BUILD["at"] = 0
        self.addCleanup(server._PAGE_BUILD.update, at=0)
        with mock.patch.object(server, "_watched", lambda: [f]):
            first = server.page_build()
            os.utime(f, ns=(f.stat().st_atime_ns, f.stat().st_mtime_ns + 5_000_000_000))
            self.assertEqual(server.page_build(), first)  # read again after 2 seconds at most
            server._PAGE_BUILD["at"] = 0
            self.assertNotEqual(server.page_build(), first)

    def test_a_new_version_shows_the_bar_and_the_page_waits_for_a_click(self):
        """#4850: the served code moves at each maxpm merge; a reload by itself made the page flash and lose its place."""
        from unittest import mock
        build = {"n": 1}
        core.config_set(self.c, "setup_done", "on")  # the setup guide is a dialog: the page would wait for it
        driver = DRIVE_HEAD + r'''
const n = +(sessionStorage.getItem("loads") || 0) + 1;
sessionStorage.setItem("loads", String(n));
try {
  // The first state too: the Sessions tab shows before it (#4888), and the page reads its build from it.
  await until(() => q("#sessTitle").textContent === "ag" && /updated/.test(q("#stamp").textContent), "the page");
  if (n === 1) {
    await fetch("/__bump");
    await until(() => q("#newCodeBar"), "the bar");
    check(q("#newCodeBar").textContent.includes("MaximizePM was updated."), "the bar's text");
    await sleep(6000);
    check(+sessionStorage.getItem("loads") === 1, "no reload by itself");
    q("#newCodeBar button").click();
  } else {
    await sleep(3000);
    post({ loads: +sessionStorage.getItem("loads"), bar: !!q("#newCodeBar") });
  }
} catch (e) { post({ error: String(e.stack || e) }); }
'''
        with mock.patch.object(server, "page_build", lambda: str(build["n"])):
            got = drive_page(self, driver, routes={"/__bump": lambda: build.update(n=2) or {"ok": True}})
        self.assertEqual(got, {"loads": 2, "bar": False})

    def test_a_new_state_shape_reloads_once_then_only_the_bar(self):
        """#4850: a page whose code cannot read the server's state loads itself again, at most once in 10 minutes."""
        from unittest import mock
        core.config_set(self.c, "setup_done", "on")
        driver = DRIVE_HEAD + r'''
const n = +(sessionStorage.getItem("loads") || 0) + 1;
sessionStorage.setItem("loads", String(n));
try {
  if (n === 1) {
    await sleep(8000);
    post({ error: "no reload for another state shape" });
  } else {
    await until(() => q("#newCodeBar"), "the bar after the one reload");
    await sleep(5000);
    post({ loads: +sessionStorage.getItem("loads"), text: q("#newCodeBar span").textContent });
  }
} catch (e) { post({ error: String(e.stack || e) }); }
'''
        with mock.patch.object(server, "STATE_SHAPE", 99):
            got = drive_page(self, driver)
        self.assertEqual(got, {"loads": 2, "text": "MaximizePM was updated, and this page must load again."})

    def test_the_page_and_the_server_read_the_same_state_shape(self):
        js = (Path(server.__file__).parent / "static" / "app.js").read_text()
        self.assertIn(f"const PAGE_SHAPE = {server.STATE_SHAPE};", js)


class ArtifactLinks(Base):
    """#4689: the paths and links a session names open in its side panel: in the chat (a relative path, a full
    path, a file:// link), and in the ! dialog; a refused path says why on hover, an unknown name stays text."""

    def test_links_in_the_chat_and_the_dialog_open_in_the_side_panel(self):
        proj = Path(self.dir.name) / "proj"
        (proj / "site" / "img").mkdir(parents=True)
        (proj / "scratch").mkdir()
        png = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                            "1f15c4890000000d49444154789c6360f8cfc0f01f0005000201b06a2d"
                            "6b0000000049454e44ae426082")
        (proj / "site" / "img" / "board.png").write_bytes(png)
        (proj / "scratch" / "shot.png").write_bytes(png)
        core.project_add(self.c, "web", path=str(proj))
        server._ROOTS["at"] = 0
        self.addCleanup(server._ROOTS.update, at=0)
        shot = str(proj / "scratch" / "shot.png")
        self.write(json.dumps({"type": "user", "cwd": str(proj / "site"), "timestamp": "2026-11-08T10:04:00Z",
                               "message": {"role": "user", "content": "Show me the pictures"}}) + "\n"
                   + claude_line("assistant", [{"type": "text", "text": f"See img/board.png, `{shot}`, [the shot](file://{shot}), "
                                                "/etc/not-here.png and nothing.png. The page: http://localhost:5173/app"}],
                                 "end_turn", at="2026-11-08T10:05:00Z"))
        core.item_edit(self.c, self.it, context=f"The picture: {shot}", actor="mark")
        core.send(self.c, "question", f"Is {shot} right?", "mark", None, None, "ag")
        driver = DRIVE_HEAD + r'''
const files = () => [...document.querySelectorAll("#sessCol [data-file]")];
try {
  await until(() => q("#sessCol .fpath-no") && !q("#sessCol [data-file]:not([data-checked])"), "the chat's paths checked");
  const no = q("#sessCol .fpath-no");
  const plain = [...document.querySelectorAll("#sessCol .fpath, #sessCol .fpath-no")].some(x => x.textContent === "nothing.png");
  const out = { files: files().map(x => [x.textContent, x.dataset.file]), refused: [no.textContent, no.title], plain };
  files()[0].click();
  await until(() => q(".sp-tab.on") && q(".sp-tab.on").title.endsWith("board.png") && q("#spBody img.sp-img"), "board.png in a panel tab");
  q('#sessions [data-session="ag"] .srow-main').click();
  await until(() => !q("#waitDlg").classList.contains("hidden") && q("#waitList .wq [data-file][data-checked]"), "the ! dialog's link");
  q("#waitList .wq [data-file]").click();
  await until(() => q("#waitDlg").classList.contains("hidden") && q(".sp-tab.on") && q(".sp-tab.on").title.endsWith("shot.png"), "the dialog's link in the panel");
  q('#sessCol a[href^="http://localhost:5173"], #sessCol [data-url^="http://localhost:5173"]').click();
  await until(() => q(".sp-tab.on") && q(".sp-tab.on").title === "http://localhost:5173/app", "the local page in the panel");
  await until(() => q("#sessItem [data-open]"), "the session's item");  // drawn after the session's data comes (#4766)
  q("#sessItem [data-open]").click();
  await until(() => q(".sp-item [data-file][data-checked]"), "the item's path checked");
  q(".sp-item [data-file]").click();
  await until(() => q(".sp-tab.on") && q(".sp-tab.on").title.endsWith("shot.png"), "the item's path in the panel");
  out.tabs = [...document.querySelectorAll(".sp-tab:not(.sp-tab-list)")].map(t => t.title);
  post(out);
} catch (e) { post({ error: String(e.stack || e) }); }
'''
        got = drive_page(self, driver)
        board = str(proj / "site" / "img/board.png")
        self.assertEqual(got["files"], [["img/board.png", board], [shot, shot], ["the shot", shot]])
        self.assertEqual(got["refused"][0], "/etc/not-here.png")
        self.assertIn("cannot show this file: the side panel shows files only from", got["refused"][1])
        self.assertFalse(got["plain"])  # a name it finds nothing for is plain text
        self.assertEqual(got["tabs"], [board, shot, "http://localhost:5173/app", f"#{self.it} header"])


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
                         (Path(self.dir.name) / "t.db", "only from the project folders"), (Path(Path(self.dir.name).anchor) / "outside-maxpm-roots.txt", "only from"),
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

    def test_screenshots_in_playwright_mcp_and_the_session_scratchpad(self):
        # #4683: the two places where a session's screenshots land; every other hidden folder stays refused.
        import re
        from unittest import mock
        shots = self.proj / ".playwright-mcp"
        shots.mkdir()
        (shots / "home.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (self.proj / "sub" / ".playwright-mcp").mkdir(parents=True)
        (self.proj / "sub" / ".playwright-mcp" / "x.png").write_bytes(b"\x89PNG")
        (shots / ".git").mkdir()
        (shots / ".git" / "y.png").write_bytes(b"\x89PNG")
        tmp = Path(self.dir.name) / "claude-501"
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(self.proj.resolve()))
        pad = tmp / slug / "c3a5e255-f33c" / "scratchpad"
        (pad / "shots").mkdir(parents=True)
        (pad / "shots" / "index.html").write_text("<p>")
        (tmp / slug / "c3a5e255-f33c" / "tasks").mkdir()
        (tmp / slug / "c3a5e255-f33c" / "tasks" / "out.txt").write_text("log")
        (tmp / "-other-project" / "s1" / "scratchpad").mkdir(parents=True)
        (tmp / "-other-project" / "s1" / "scratchpad" / "a.png").write_bytes(b"\x89PNG")
        with mock.patch.object(server, "CLAUDE_TMP", str(tmp.resolve())):
            self.assertEqual(server.local_file(self.c, str(shots / "home.png"))[1], "image/png")
            self.assertEqual(server.local_file(self.c, str(pad / "shots" / "index.html"))[1], "text/html; charset=utf-8")
            for bad, why in ((self.proj / "sub" / ".playwright-mcp" / "x.png", "hidden"),  # only right under a project
                             (shots / ".git" / "y.png", "hidden"),
                             (tmp / slug / "c3a5e255-f33c" / "tasks" / "out.txt", "only from"),  # not its scratchpad
                             (tmp / "-other-project" / "s1" / "scratchpad" / "a.png", "only from")):  # no such project
                with self.assertRaisesRegex(core.RiverError, why):
                    server.local_file(self.c, str(bad))

    def test_the_paths_a_session_names_resolve_or_say_why(self):
        # #4689: a full path, a file:// link, a path relative to the session's folder, a bare name of a file it
        # named, and the reasons for the others.
        from unittest import mock
        (self.proj / "site" / "img").mkdir(parents=True)
        (self.proj / "site" / "img" / "board.png").write_bytes(b"\x89PNG")
        shot = str(self.proj / "scratch" / "shot.png")
        board = str(self.proj / "site" / "img" / "board.png")
        self.write(json.dumps({"type": "user", "cwd": str(self.proj / "site"), "timestamp": "2026-11-09T01:00:00Z",
                               "message": {"role": "user", "content": "go"}}) + "\n"
                   + claude_line("assistant", [{"type": "text", "text": f"Saved {board}"}], "end_turn"))
        with mock.patch.object(core, "transcripts_root", lambda: self.root):
            got = server.file_check(self.c, [shot, "file://" + shot, "file://localhost" + shot, "img/board.png",
                                             "board.png", "./img/board.png", "nothing.png",
                                             str(Path(self.dir.name) / "t.db"), "file://other/x.png", "notes.py"], agent="ag")
        ok = {p: g["target"] for p, g in got.items() if g["ok"]}
        self.assertEqual(ok, {shot: shot, "file://" + shot: shot, "file://localhost" + shot: shot,
                              "img/board.png": str(self.proj / "site" / "img/board.png"), "board.png": board,
                              "./img/board.png": str(self.proj / "site" / "./img/board.png")})
        self.assertIn("no file of this session", got["nothing.png"]["why"])
        self.assertIn("only from the project folders", got[str(Path(self.dir.name) / "t.db")]["why"])
        self.assertIn("another computer", got["file://other/x.png"]["why"])
        self.assertIn("no file of this session", got["notes.py"]["why"])  # not in its folder, never named
        self.assertEqual(self.get("/api/file-check?path=a.png")["code"], 403)  # the page's token
        out = self.get(f"/api/file-check?path={shot}&token={server.PAGE_TOKEN}")
        self.assertEqual(out["body"]["paths"][shot]["ok"], True)

    def test_a_notice_about_a_file_of_a_session_opens_it_in_the_side_panel(self):
        # #4689: notify_open maxpm, a question that names a file the session named: /sessions/<s>/artifacts/<id>.
        import shutil
        import subprocess
        from unittest import mock
        from river import notify
        shot = str(self.proj / "scratch" / "shot.png")
        self.write(claude_line("assistant", [{"type": "text", "text": f"I saved {shot}"}], "end_turn"))
        q = core.send(self.c, "question", f"Is {shot} right?", "mark", None, None, "ag")["id"]
        row = {"summary": f"question from ag: Is {shot} right?", "item_id": None, "message_id": q, "human": "mark"}
        with mock.patch.object(core, "transcripts_root", lambda: self.root):
            url = notify.compose(self.c, [row])[2]
            self.assertTrue(url.endswith(f"/sessions/ag/artifacts/{notify.art_id(shot)}?as=mark"), url)
            other = dict(row, summary="question from ag: which port?")
            self.assertTrue(notify.compose(self.c, [other])[2].endswith("/sessions/ag?as=mark"))
        node = shutil.which("node")
        if node:  # the same id as the page's artId (static/lib.js)
            lib = (Path(server.__file__).parent / "static" / "lib.js").as_uri()
            js = subprocess.run([node, "--input-type=module", "-e",
                                 f"import {{ artId }} from {json.dumps(lib)}; console.log(artId(process.argv[1]))", shot],
                                capture_output=True, text=True, timeout=30).stdout.strip()
            self.assertEqual(js, notify.art_id(shot))

    def test_the_scratchpad_folder_from_the_environment(self):
        from unittest import mock
        if not hasattr(os, "getuid"):
            self.skipTest("no scratchpads on Windows")
        own = f"claude-{os.getuid()}"
        d = Path(self.dir.name).resolve()
        for env, want in ((str(d / own), d / own), (str(d), d / own)):  # a session's own value, or its parent
            with mock.patch.dict(os.environ, {"CLAUDE_CODE_TMPDIR": env}):
                self.assertEqual(server.claude_tmps(), [(Path("/tmp") / own).resolve(), want])

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
        # #4630: one session only, also when it is not running and was not seen for days.
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET last_seen='2026-01-01T00:00:00Z', pid=NULL WHERE name='ag'")
        self.assertEqual(sessions.artifacts(self.c, {}, allowed, self.root, self.croot), [])
        one = sessions.artifacts(self.c, {}, allowed, self.root, self.croot, agent="ag")
        self.assertIn(shot, [a["target"] for a in one])
        self.assertEqual(sessions.artifacts(self.c, {}, allowed, self.root, self.croot, agent="nobody"), [])

    def test_one_row_for_one_claude_ai_artifact_with_each_session_that_named_it(self):
        # #5000: a worker publishes an artifact, the manager names it again in another form; one row, both sessions.
        import shutil
        import subprocess
        art = "https://claude.ai/artifact/9bimT1oWobMH8TBekRjK3q"
        self.write(claude_line("assistant", [{"type": "text", "text": f"Published: {art})."}], "end_turn",
                               at="2026-11-08T10:01:00Z"))
        core.register(self.c, "boss")
        sid2 = "22222222-2222-4222-8222-222222222222"
        core.record_claude_session(self.c, "boss", sid2)
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET pid=?, host=? WHERE name='boss'", (os.getpid(), core.this_host()))
        (self.root / "-boss").mkdir()
        (self.root / "-boss" / f"{sid2}.jsonl").write_text(
            claude_line("assistant", [{"type": "text", "text": "Look at http://Claude.AI/artifact/9bimT1oWobMH8TBekRjK3q?v=2 "
                                                               "and https://claude.ai/Code/Artifact/ab-12"}], "end_turn",
                        at="2026-11-08T10:05:00Z"))
        rows = [a for a in sessions.artifacts(self.c, {}, None, self.root, self.croot) if a["kind"] == "artifact"]
        self.assertEqual([(a["target"], a["session"], a["sessions"]) for a in rows],
                         [(art, "boss", ["boss", "ag"]), ("https://claude.ai/code/artifact/ab-12", "boss", ["boss"])])
        self.assertEqual(sessions.artifact_key("https://claude.ai/artifact/X1_y-2#top"), "https://claude.ai/artifact/X1_y-2")
        self.assertEqual(sessions.artifact_key("/a/b.md"), "/a/b.md")
        node = shutil.which("node")
        if node:  # the page's targetKey (static/lib.js) gives the same key, and one key for each thing it shows
            lib = (Path(server.__file__).parent / "static" / "lib.js").as_uri()
            cases = [art + ").", "http://claude.ai/artifact/9bimT1oWobMH8TBekRjK3q?x=1#y", "https://claude.ai/code/artifact/ab-12/",
                     "canvas:c3", "#12", "/a/b/", "http://LOCALHOST:5173", "http://localhost:5173/#/a"]
            js = subprocess.run([node, "--input-type=module", "-e",
                                 f"import {{ targetKey }} from {json.dumps(lib)}; "
                                 "console.log(JSON.stringify(JSON.parse(process.argv[1]).map(targetKey)))", json.dumps(cases)],
                                capture_output=True, text=True, timeout=30)
            self.assertEqual(json.loads(js.stdout), [art, art, "https://claude.ai/code/artifact/ab-12", "canvas:3", "item:12",
                                                     "/a/b", "http://localhost:5173/", "http://localhost:5173/#/a"], js.stderr)
            self.assertEqual([sessions.artifact_key(x) for x in cases[:3]], json.loads(js.stdout)[:3])

    def test_maxpm_artifacts_lists_and_shows_what_the_sessions_of_an_item_made(self):
        # #4838: a manager in the terminal: --item (what was made while the item was held), --since, show <n>, the
        # line in maxpm show, and the hint in the manager's findings for a Look at item.
        import contextlib
        import io
        from unittest import mock
        from river import cli, notify
        shot = str(self.proj / "scratch" / "shot.png")
        page = str(self.proj / "page.html")
        self.write(claude_line("assistant", [{"type": "tool_use", "id": "w1", "name": "Write",
                                              "input": {"file_path": page, "content": "<p>"}}], "tool_use",
                               at="2026-11-08T10:01:00Z")
                   + claude_line("assistant", [{"type": "text", "text": f"Saved {shot}; see https://claude.ai/artifact/ab12-cd"}],
                                 "end_turn", at="2026-11-08T10:02:00Z"))
        it2 = core.item_add(self.c, "site", "footer")["id"]
        with core.tx(self.c):
            self.c.execute("DELETE FROM holds")
            self.c.execute("INSERT INTO holds (item_id, agent, started_at, ended_at) VALUES (?, 'ag', "
                           "'2026-11-08T10:00:00Z', '2026-11-08T10:01:30Z'), (?, 'ag', '2026-11-08T10:01:30Z', NULL)",
                           (self.it, it2))
        allowed = lambda f: self.get(f"/api/file?path={f}&token={server.PAGE_TOKEN}")["code"] == 200
        arts = lambda **k: [(a["target"], a["item"]) for a in sessions.artifacts(self.c, {}, allowed, self.root, self.croot, **k)]
        self.assertEqual(arts(item=self.it), [(page, self.it)])
        self.assertEqual(sorted(arts(item=it2)), sorted([(shot, it2), ("https://claude.ai/artifact/ab12-cd", it2)]))
        self.assertEqual(arts(item=it2, agent="nobody"), [])
        self.assertNotIn(page, [t for t, _i in arts(agent="ag", since="2026-11-08T10:01:30Z")])
        core.canvas_put(self.c, "plan", "<p>The plan</p>", "ag", kind="html", item=it2)

        def run(*words):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), mock.patch.object(core, "transcripts_root", lambda: self.root):
                cli.run(["-q", "--as", "mark", *words])
            return out.getvalue()
        listed = run("artifacts", "--item", str(it2))
        self.assertIn('canvas:', listed.splitlines()[0])  # canvases first
        self.assertIn(shot, listed)
        self.assertIn(f"id {notify.art_id(shot)}", listed)
        self.assertIn(f"read one: maxpm artifacts show <n> --item {it2}", listed)
        self.assertIn("The plan", run("artifacts", "show", "1", "--item", str(it2)))
        self.assertIn(f"path: {shot}", run("artifacts", "show", notify.art_id(shot), "--item", str(it2)))
        self.assertIn("Artifact tool", run("artifacts", "show", "https://claude.ai/artifact/ab12-cd", "--item", str(it2)))
        self.assertIn("fetch('/api/action')", run("artifacts", "show", page, "--item", str(self.it)))
        with self.assertRaises(core.RiverError):
            cli.artifacts_cmd(self.c, cli.build_parser().parse_args(["artifacts", "show", "9", "--item", str(self.it)]))
        self.assertIn(f"artifacts: 1, maxpm artifacts --item {self.it}", run("show", str(self.it)))
        look = core.item_add(self.c, "site", "Look at the footer", doer="human", found_during=it2)["id"]
        self.assertIn(f"artifacts: 3 of #{it2}, where it was found", run("show", str(look)))
        lines = "\n".join(cli._findings_lines(core.manager_findings(self.c), "maxpm"))
        self.assertIn(f"#{look}: maxpm artifacts --item {it2}", lines)

    def test_the_frame_check_needs_a_web_address(self):
        self.assertEqual(server.frame_check("javascript:alert(1)")["frame"], False)
        self.assertEqual(self.get("/api/frame-check?url=x")["code"], 403)


class ArtifactScan(unittest.TestCase):
    """#4681: pictures in tool results fill a transcript fast; the files a session named before them still count
    (the first read takes the whole file), and a later read takes only the lines the session added."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.path = Path(d.name) / "t.jsonl"
        sessions._ART_CACHE.clear()
        self.addCleanup(sessions._ART_CACHE.clear)

    def line(self, kind, content):
        return json.dumps({"type": kind, "timestamp": "2026-11-08T10:00:00Z",
                           "message": {"role": kind, "content": content}}, separators=(",", ":")) + "\n"

    def targets(self):
        return sorted({h["target"] for h in sessions._artifact_hits(self.path)})

    def test_names_before_many_pictures_and_only_new_lines_later(self):
        from unittest import mock
        picture = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 1_000_000}}
        with open(self.path, "w") as f:
            f.write(self.line("assistant", [{"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": "/p/a.png"}}]))
            for n in range(6):  # 6 MB of pictures, more than the 4 MB tail of before
                f.write(self.line("user", [{"type": "tool_result", "tool_use_id": f"r{n}",
                                            "content": [picture, {"type": "text", "text": "/p/listing.png"}]}]))
            f.write(self.line("user", 'I typed "type":"tool_result" and /p/typed.png'))
            f.write(self.line("assistant", [{"type": "text", "text": "Saved /p/b.png"}]))
        self.assertEqual(self.targets(), ["/p/a.png", "/p/b.png", "/p/typed.png"])  # a tool result names none
        with open(self.path, "a") as f:
            f.write(self.line("assistant", [{"type": "text", "text": "And /p/c.png"}]))
            f.write('{"type":"assistant","message":{"content":[{"type":"text","text":"/p/half')  # still written
        lines = []
        real = sessions._line_hits
        with mock.patch.object(sessions, "_line_hits", lambda raw: lines.append(raw) or real(raw)):
            self.assertEqual(self.targets(), ["/p/a.png", "/p/b.png", "/p/c.png", "/p/typed.png"])
            with open(self.path, "a") as f:
                f.write('.png"}]}}\n')
            self.assertIn("/p/half.png", self.targets())
        self.assertEqual(len(lines), 2, lines)  # only the lines after the last read, each one once
        # A file written again, shorter: read again from its start.
        self.path.write_text(self.line("assistant", [{"type": "text", "text": "/p/new.png"}]))
        self.assertEqual(self.targets(), ["/p/new.png"])


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


class WindowsPaths(unittest.TestCase):
    def test_windows_artifacts_and_full_screen_paths(self):
        for path in (r"C:\Users\person\project\page.html", r"C:/Users/person/project/page.html",
                     r"\\computer\share\page.html"):
            with self.subTest(path=path):
                self.assertEqual(sessions._hits_in("Saved " + path + ";", None, "named"),
                                 [{"target": path, "kind": "file", "how": "named", "at": None}])
                self.assertTrue(server._file_target(path))
        self.assertFalse(server._file_target(r"C:relative.txt"))
        self.assertFalse(server._file_target("https://example.com/file.txt"))

    def test_uploads_in_backslash_paths_still_have_previews_and_downloads(self):
        from unittest.mock import patch
        with patch.object(core, "river_dir", lambda: "C:/Users/person/.maximizepm"):
            turn = sessions._user_turn(None, r"C:\Users\person\.maximizepm\uploads\ag\20261108-120000-12345678-shot.png"
                                       + "\n" + r"C:\Users\person\.maximizepm\uploads\ag\20261108-120000-12345678-report.pdf")
        self.assertEqual(turn["images"], ["ag/20261108-120000-12345678-shot.png"])
        self.assertEqual(turn["files"], [{"path": "ag/20261108-120000-12345678-report.pdf", "name": "report.pdf"}])


class EndProcess(unittest.TestCase):
    def test_windows_ends_the_process_tree_without_unix_signal_or_waitpid(self):
        from unittest.mock import patch
        with patch.object(os, "name", "nt"), patch("subprocess.run") as run, \
                patch.object(core, "pid_alive", return_value=False), patch.object(os, "kill") as kill:
            self.assertTrue(server._end_process(1234))
            run.assert_called_once_with(["taskkill", "/PID", "1234", "/T", "/F"], capture_output=True)
            kill.assert_not_called()
        with patch.object(os, "name", "nt"), patch("subprocess.run"), patch.object(core, "pid_alive", return_value=True):
            self.assertFalse(server._end_process(1234))


@unittest.skipIf(os.name == "nt", "Windows does not use tmux; outside_tmux intentionally returns false")
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
