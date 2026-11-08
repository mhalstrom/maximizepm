"""The Sessions tab of maxpm serve (#4592): running sessions and a read-only view of their transcripts."""
import json
import os
import tempfile
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


class Sessions(unittest.TestCase):
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
