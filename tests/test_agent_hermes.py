"""The Hermes adapter (river/agents/hermes.py, #5173), tested with what a real Hermes Agent v0.22.7 printed and stored
(tests/fixtures/hermes/README.md says how each file was recorded)."""

import json
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from river import core  # noqa: F401 - river.agents imports core; core first, as the CLI does
from river import agents
from river.agents import hermes

FIX = Path(__file__).parent / "fixtures" / "hermes"


def screen(name):
    return (FIX / name).read_text()


def lines(name):
    return (FIX / name).read_text().splitlines()


def state_of(text):
    """The state that the DETECT rules give a screen, in the order a pane watcher applies them: a screen that waits
    for a person first, then a turn, then the prompt."""
    d = hermes.DETECT
    if any(re.search(p, text) for p in d["blocked"]):
        return "blocked"
    if re.search(d["working"], text):
        return "working"
    if re.search(d["idle"], text):
        return "idle"
    return None


class HomeCase(unittest.TestCase):
    """A test with its own HERMES_HOME, so nothing reads or writes ~/.hermes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        patcher = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)


class Registry(unittest.TestCase):
    def test_it_is_a_valid_builtin_adapter_with_every_required_part(self):
        a = agents.get("hermes")
        self.assertEqual(a.source, "builtin")
        self.assertEqual(agents.check(a.module, "hermes"), [])
        self.assertEqual(a.missing(), [])
        self.assertIs(agents.find("Hermes-Agent"), a)
        self.assertEqual(a.describe["family"], "multi")
        self.assertEqual(a.describe["default_model"], "gpt-6.1-terra")
        for part in ("settings", "effort", "launch", "identity", "chat", "state", "detect", "resume", "commands",
                     "setup"):
            self.assertTrue(a.supports(part), part)

    def test_a_command_inside_hermes_is_known_by_its_session_id_variable(self):
        self.assertEqual(agents.from_env({"HERMES_SESSION_ID": "local-abc", "PATH": "/bin"}).id, "hermes")
        # A Hermes started from a Claude Code shell keeps CLAUDECODE: Hermes's own variable wins.
        self.assertEqual(agents.from_env({"HERMES_SESSION_ID": "local-abc", "CLAUDECODE": "1"}).id, "hermes")
        self.assertIsNone(agents.from_env({"PATH": "/bin"}))

    def test_the_settings_and_effort_fit_the_contract(self):
        a = agents.get("hermes")
        keys = [s["key"] for s in a.settings]
        self.assertEqual(keys, ["provider", "yolo", "checkpoints", "toolsets", "skills", "args", "prompt"])
        self.assertEqual(a.config_key("provider"), "hermes_provider")
        self.assertEqual([a.effort_name(x) for x in agents.MAXPM_EFFORTS], list(agents.MAXPM_EFFORTS))
        self.assertTrue(set(agents.MAXPM_EFFORTS) <= set(a.effort["levels"]))
        for s in a.settings:
            if s["read"]:
                re.compile(s["read"])


class Launch(unittest.TestCase):
    def test_the_default_start_runs_the_classic_cli_on_openai_codex(self):
        a = agents.get("hermes")
        got = a.build_command("w1", "/work", "gpt-6.1-terra", "high", {}, "run maxpm go")
        self.assertEqual(got, ["hermes", "chat", "--cli", "--provider", "openai-codex", "-m", "gpt-6.1-terra",
                               "--reasoning", "high", "-q", "run maxpm go"])

    def test_every_setting_goes_on_the_command_line(self):
        got = hermes.build_command("w1", "/work", "claude-opus-5-5", "max",
                                   {"provider": "anthropic", "yolo": "on", "checkpoints": "on",
                                    "toolsets": "file,terminal", "skills": "maxpm", "args": "--max-turns 40"}, "go")
        self.assertEqual(got, ["hermes", "chat", "--cli", "--provider", "anthropic", "-m", "claude-opus-5-5",
                               "--reasoning", "max", "--yolo", "--checkpoints", "--toolsets", "file,terminal",
                               "--skills", "maxpm", "--max-turns 40", "-q", "go"])
        self.assertIsInstance(got[-3], agents.Raw)  # the person's own shell text goes in as it is

    def test_an_empty_provider_leaves_hermes_its_own_and_no_model_or_effort_adds_nothing(self):
        got = hermes.build_command(None, None, None, None, {"provider": "", "yolo": "off"}, None)
        self.assertEqual(got, ["hermes", "chat", "--cli"])

    def test_resume_names_the_session_and_sends_the_prompt_after_it(self):
        a = agents.get("hermes")
        sid = "local-a52f877bc92e683b2568b21f70409c0805725b04c01d9d5597ef9de6585441b0"
        got = a.resume_command(sid, "gpt-6.1-terra", "low", {"provider": "openai-codex", "prompt": "go on"})
        self.assertEqual(got, ["hermes", "chat", "--cli", "--resume", sid, "--provider", "openai-codex", "-m",
                               "gpt-6.1-terra", "--reasoning", "low", "-q", "go on"])
        # The exit screen of the recorded session names the same id for a resume.
        self.assertIn(f"hermes --resume {sid}", screen("pane_exit.txt"))
        self.assertIsNone(a.resume_command("", None, None, {}))

    def test_parse_command_reads_back_what_a_profile_builds_and_nothing_else(self):
        got = hermes.parse_command(["hermes", "chat", "--cli", "--provider", "anthropic", "-m", "{model}",
                                    "--reasoning", "{effort}", "--yolo", "-q", "run maxpm go"])
        self.assertEqual(got, {"settings": {"provider": "anthropic", "yolo": "on"}, "prompt": "run maxpm go"})
        self.assertEqual(hermes.parse_command(["/usr/local/bin/hermes", "go"]),
                         {"settings": {"provider": ""}, "prompt": "go"})
        self.assertIsNone(hermes.parse_command(["hermes", "--unknown", "go"]))
        self.assertIsNone(hermes.parse_command(["codex", "go"]))
        self.assertIsNone(hermes.parse_command(["hermes", "chat", "--cli"]))  # no prompt


class Detect(unittest.TestCase):
    """The pane screens of the recorded session, each one read as the state it showed."""

    def test_each_recorded_screen_reads_as_its_state(self):
        expect = {"pane_idle_start.txt": "idle", "pane_idle.txt": "idle", "pane_working.txt": "working",
                  "pane_blocked_clarify.txt": "blocked", "pane_blocked_approval.txt": "blocked"}
        for name, state in expect.items():
            self.assertEqual(state_of(screen(name)), state, name)

    def test_the_rules_do_not_overlap_on_the_recorded_screens(self):
        d = hermes.DETECT
        self.assertIsNone(re.search(d["idle"], screen("pane_working.txt")))
        self.assertIsNone(re.search(d["working"], screen("pane_idle.txt")))
        for name in ("pane_blocked_clarify.txt", "pane_blocked_approval.txt"):
            self.assertIsNone(re.search(d["working"], screen(name)), name)
            self.assertIsNone(re.search(d["idle"], screen(name)), name)
        for name in ("pane_idle.txt", "pane_working.txt"):
            self.assertFalse(any(re.search(p, screen(name)) for p in d["blocked"]), name)

    def test_the_question_box_has_the_option_to_type_an_answer(self):
        self.assertTrue(re.search(hermes.DETECT["tell_instead"], screen("pane_blocked_clarify.txt")))
        self.assertIsNone(re.search(hermes.DETECT["tell_instead"], screen("pane_blocked_approval.txt")))


class Watcher(unittest.TestCase):
    """The pane watcher of maxpm serve (server.watch_prompts) reads each recorded screen with the adapter's DETECT:
    the state it reports, and the question and options it sends the person."""

    def read(self, name):
        from river import server
        d = agents.get("hermes").detect
        tail = server._prompt_tail(server._plain(screen(name)))
        layout = server._layout(d)
        parts = server._prompt_found(tail, server._AnyOf(d["blocked"]), layout)
        return ("blocked" if parts else server._screen_state(tail, d)), parts, layout

    def test_idle_and_working_screens(self):
        for name, state in (("pane_idle_start.txt", "idle"), ("pane_idle.txt", "idle"),
                            ("pane_working.txt", "working")):
            self.assertEqual(self.read(name)[:2], (state, None), name)

    def test_a_question_reads_with_its_options_and_the_option_to_type_an_answer(self):
        state, parts, layout = self.read("pane_blocked_clarify.txt")
        self.assertEqual(state, "blocked")
        self.assertIn("Which color do you prefer?", parts["question"])
        self.assertEqual(parts["options"], [("1. red (Recommended)", True), ("2. blue", False),
                                            ("3. Other (type your answer)", False)])
        self.assertEqual(parts["hint"], "↑/↓ to select, Enter to lock")
        self.assertEqual([o for o, _ in parts["options"] if layout["tell"].search(o)], ["3. Other (type your answer)"])

    def test_a_command_approval_reads_with_its_options(self):
        state, parts, _ = self.read("pane_blocked_approval.txt")
        self.assertEqual(state, "blocked")
        self.assertEqual([o for o, _ in parts["options"]], ["1. Allow once", "2. Allow for this session",
                                                            "3. Add to permanent allowlist", "4. Deny"])
        self.assertEqual(parts["hint"], "↑/↓ to select, Enter to confirm")


class Chat(unittest.TestCase):
    def test_the_recorded_chat_reads_as_prompts_tools_results_and_answers(self):
        entries, turn = hermes.parse_chat(lines("chat_approval.jsonl"))
        self.assertEqual(turn, "idle")
        self.assertEqual([e["role"] for e in entries],
                         ["user", "tool", "result", "tool", "result", "assistant",
                          "user", "tool", "result", "assistant",
                          "user", "tool", "result", "assistant"])
        tools = [e for e in entries if e["role"] == "tool"]
        self.assertEqual([(t["name"], t["detail"]) for t in tools],
                         [("terminal", "sleep 15; ls"), ("terminal", "rm -rf old"),
                          ("clarify", "Which color do you prefer?"),
                          ("terminal", "rm -rf /tmp/maxpm-hermes-probe /etc/maxpm-nonexistent-probe")])
        results = [e for e in entries if e["role"] == "result"]
        self.assertEqual([r["for"] for r in results], [t["id"] for t in tools])
        self.assertEqual(results[0]["text"], "notes.txt")
        self.assertEqual([r["error"] for r in results], [False, False, False, True])  # the person denied the last
        self.assertIn("denied by user", results[3]["text"])
        self.assertEqual(entries[-1]["text"], "The command was denied and did not run.")
        self.assertTrue(entries[0]["at"].endswith("Z"))
        for e in entries:
            self.assertIn(e["role"], agents.ENTRY_ROLES)

    def test_the_turn_is_working_until_the_answer_ends(self):
        rows = lines("chat_approval.jsonl")
        self.assertEqual(hermes.parse_chat(rows[:2])[1], "working")  # the session and the prompt
        self.assertEqual(hermes.parse_chat(rows[:3])[1], "working")  # a tool call
        self.assertEqual(hermes.parse_chat(rows[:4])[1], "working")  # its result
        self.assertEqual(hermes.parse_chat(rows[:7])[1], "idle")  # the answer
        self.assertEqual(hermes.parse_chat(rows[:1]), ([], None))  # the session row alone says nothing

    def test_a_file_write_and_a_resumed_turn(self):
        entries, turn = hermes.parse_chat(lines("chat_write_resume.jsonl"))
        self.assertEqual(turn, "idle")
        writes = [e for e in entries if e["role"] == "tool" and e["name"] in hermes.CHAT["file_tools"]]
        self.assertEqual([(w["name"], w["detail"]) for w in writes], [("write_file", "hello.txt")])
        users = [e["text"] for e in entries if e["role"] == "user"]
        self.assertEqual(users[-1], "What file did you write before? Answer in one line.")  # after the resume
        self.assertEqual(entries[-1], {**entries[-1], "role": "assistant", "text": "hello.txt"})

    def test_the_session_row_gives_title_folder_usage_settings_and_effort(self):
        rows = lines("chat_approval.jsonl")
        self.assertEqual(hermes.chat_title(rows), "Run shell commands and delete old")
        self.assertEqual(hermes.chat_cwd(rows), "/tmp/work/hermes-run")
        self.assertEqual(hermes.chat_usage(rows), {"input": 12911, "output": 174, "cache_read": 95104,
                                                   "cache_write": 0})
        self.assertEqual(hermes.chat_settings(rows), {"provider": "openai-codex"})
        self.assertIsNone(hermes.read_effort(rows))  # the session ran at Hermes's configured level
        high = [json.dumps({"type": "session", "model_config": json.dumps(
            {"reasoning_config": {"enabled": True, "effort": "high"}})})]
        self.assertEqual(hermes.read_effort(high), "high")

    def test_rows_the_recording_did_not_have(self):
        """Made-up rows in the shape of Hermes's messages table, for the kinds the recorded sessions did not show: a
        compaction summary, a rewound row, a hidden row, a failed turn, a finished background process, and a prompt
        with an image."""
        def row(i, role, content, **kw):
            return json.dumps({"type": "message", "id": i, "role": role, "content": content, "timestamp": 1794383000 + i,
                               "active": 1, "_compressed_summary": 0, "compacted": 0, **kw})
        got, turn = hermes.parse_chat([
            row(1, "user", "old prompt", compacted=1),
            row(2, "user", "Summary of the earlier turns", _compressed_summary=1),
            row(3, "user", "rewound", active=0),
            row(4, "assistant", "internal", display_kind="hidden"),
            row(5, "user", "background job done", display_kind="process_complete"),
            row(6, "user", json.dumps([{"type": "text", "text": "look"},
                                       {"type": "image_url", "image_url": {"url": "/tmp/a.png"}}])),
            row(7, "assistant", "", display_kind="failed_turn"),
        ])
        self.assertEqual([(e["role"], e.get("kind"), e["text"]) for e in got],
                         [("user", None, "old prompt"), ("divider", "compacted", "Compacted"),
                          ("summary", None, "Summary of the earlier turns"), ("event", None, "background job done"),
                          ("user", None, "look"), ("event", None, "The turn failed")])
        self.assertEqual(got[4]["images"], ["/tmp/a.png"])
        self.assertEqual(turn, "idle")


class Store(HomeCase):
    """chat_lines and chat_path read Hermes's state.db; here a database built from the recorded rows. The copies of
    the chats go beside a queue of the test (MAXPM_DB)."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"MAXPM_DB": str(self.home / "queue" / "maxpm.db")})
        patcher.start()
        self.addCleanup(patcher.stop)
        conn = sqlite3.connect(self.home / "state.db")
        conn.execute(f"CREATE TABLE sessions ({', '.join(hermes._SESSION_COLS)})")
        conn.execute(f"CREATE TABLE messages (session_id, {', '.join(hermes._MESSAGE_COLS)})")
        for name in ("chat_approval.jsonl", "chat_write_resume.jsonl"):
            sid = None
            for r in map(json.loads, lines(name)):
                t = r.pop("type")
                if t == "session":
                    sid = r["id"]
                    conn.execute(f"INSERT INTO sessions VALUES ({', '.join('?' * len(r))})", list(r.values()))
                else:
                    conn.execute(f"INSERT INTO messages VALUES (?, {', '.join('?' * len(r))})", [sid, *r.values()])
        conn.commit()
        conn.close()

    def test_the_lines_of_a_session_are_its_rows_in_order(self):
        sid = json.loads(lines("chat_approval.jsonl")[0])["id"]
        self.assertEqual(hermes.chat_lines(sid), lines("chat_approval.jsonl"))

    def test_the_chat_file_is_a_copy_of_the_rows_beside_the_queue(self):
        sid = json.loads(lines("chat_approval.jsonl")[0])["id"]
        path = agents.get("hermes").chat_path(sid)
        self.assertEqual(path, self.home / "queue" / "hermes" / f"{sid}.jsonl")
        self.assertEqual(path.read_text().splitlines(), lines("chat_approval.jsonl"))
        # The Sessions tab parses the file line by line.
        a = agents.get("hermes")
        entries = [e for x in path.read_text().splitlines() for e in a.parse_chat_line(json.loads(x), x)[0]]
        self.assertEqual(entries, hermes.parse_chat(lines("chat_approval.jsonl"))[0])

    def _db(self):
        conn = sqlite3.connect(self.home / "state.db", isolation_level=None)
        self.addCleanup(conn.close)
        return conn

    def test_a_new_row_is_appended_and_a_changed_session_row_too(self):
        sid = json.loads(lines("chat_approval.jsonl")[0])["id"]
        path = hermes.chat_path(sid)
        size = path.stat().st_size
        self.assertEqual(hermes.chat_path(sid).stat().st_size, size)  # nothing new: the file stays as it was
        with self._db() as c:
            c.execute("INSERT INTO messages (session_id, id, role, content, timestamp, active, _compressed_summary, "
                      "compacted) VALUES (?, 9999, 'user', 'one more', 1794383300, 1, 0, 0)", (sid,))
            c.execute("UPDATE sessions SET title='New title', output_tokens=200 WHERE id=?", (sid,))
        got = hermes.chat_path(sid).read_text().splitlines()
        self.assertEqual(got[:len(lines("chat_approval.jsonl"))], lines("chat_approval.jsonl"))  # appended only
        self.assertEqual([json.loads(x)["type"] for x in got[-2:]], ["session", "message"])
        self.assertEqual(hermes.chat_title(got), "New title")
        self.assertEqual(hermes.chat_usage(got)["output"], 200)

    def test_an_undo_writes_the_copy_again(self):
        sid = json.loads(lines("chat_approval.jsonl")[0])["id"]
        before = hermes.parse_chat(hermes.chat_path(sid).read_text().splitlines())[0]
        with self._db() as c:  # /undo of the last turn: Hermes marks its rows inactive
            c.execute("UPDATE messages SET active=0 WHERE session_id=? AND id >= 1348", (sid,))
        after = hermes.parse_chat(hermes.chat_path(sid).read_text().splitlines())[0]
        self.assertEqual(after, before[:-4])

    def test_texts_for_the_artifacts_tab(self):
        a = agents.get("hermes")
        got = [t for x in lines("chat_write_resume.jsonl") for t in a.chat_texts(x.encode())]
        self.assertEqual([t[0] for t in got if t[2] == "wrote"], ["hello.txt"])
        self.assertTrue(all(t[1] and t[1].endswith("Z") for t in got))
        self.assertTrue(any(t[0].startswith("Run this shell command") and t[2] == "named" for t in got))
        results = [x for x in lines("chat_write_resume.jsonl") if '"role": "tool"' in x]
        self.assertEqual([t for x in results for t in a.chat_texts(x.encode())], [])  # results are left out
        self.assertEqual(a.chat_texts(b"not json"), [])

    def test_the_first_hex_digits_of_an_id_find_the_session(self):
        # The banner shows "Session: 42f65cbc"; the id is "local-42f65cbc...".
        got = hermes.chat_lines("42f65cbc")
        self.assertEqual(json.loads(got[0])["title"], "Run shell commands and delete old")
        self.assertEqual(len(hermes.chat_lines("local-a52f877b")), len(lines("chat_write_resume.jsonl")))

    def test_after_reads_only_the_newer_rows(self):
        sid = json.loads(lines("chat_approval.jsonl")[0])["id"]
        last = json.loads(lines("chat_approval.jsonl")[-2])["id"]
        got = hermes.chat_lines(sid, after=last)
        self.assertEqual([json.loads(x)["type"] for x in got], ["session", "message"])

    def test_an_unknown_or_ambiguous_session_has_no_chat(self):
        self.assertEqual(hermes.chat_lines("local-ffff"), [])
        self.assertIsNone(hermes.chat_path("local-ffff"))
        self.assertIsNone(hermes.chat_path("local-"))  # the start of both ids
        self.assertIsNone(hermes.chat_path(""))
        self.assertFalse((self.home / "queue" / "hermes").exists())

    def test_no_database_means_no_chat(self):
        (self.home / "state.db").unlink()
        self.assertEqual(hermes.chat_lines("42f65cbc"), [])
        self.assertIsNone(hermes.chat_path("42f65cbc"))

    def test_root_names_the_folder_of_the_database(self):
        other = self.home / "other"
        other.mkdir()
        (self.home / "state.db").rename(other / "state.db")
        self.assertIsNone(hermes.chat_path("42f65cbc"))
        self.assertTrue(agents.get("hermes").chat_path("42f65cbc", None, other).is_file())


class Commands(HomeCase):
    def test_the_builtin_commands_fit_the_contract(self):
        names = [c["name"] for c in hermes.COMMANDS]
        self.assertEqual(len(names), len(set(names)))
        for c in hermes.COMMANDS:
            self.assertEqual(set(c), {"name", "description", "hint"})
            self.assertFalse(c["name"].startswith("/"))
        self.assertTrue({"new", "compress", "model", "reasoning", "resume", "quit"} <= set(names))
        self.assertEqual(hermes.EFFORT["live"].format(value="high"), "/reasoning high")

    def test_each_skill_is_a_slash_command(self):
        skills = self.home / "skills"
        (skills / "research" / "arxiv").mkdir(parents=True)
        (skills / "research" / "arxiv" / "SKILL.md").write_text(
            "---\nname: arxiv\ndescription: Search arXiv papers\n---\nbody\n")
        (skills / "plain").mkdir()
        (skills / "plain" / "SKILL.md").write_text("no front matter\n")
        (skills / "bad name").mkdir()
        (skills / "bad name" / "SKILL.md").write_text("---\nname: bad name\n---\n")
        got = agents.get("hermes").read_commands("/work")
        custom = got[len(hermes.COMMANDS):]
        self.assertEqual(custom, [{"name": "plain", "description": "", "hint": ""},
                                  {"name": "arxiv", "description": "Search arXiv papers", "hint": ""}])

    def test_no_skills_folder_gives_only_the_builtin_commands(self):
        self.assertEqual(hermes.read_commands("/work"), [])


class Models(HomeCase):
    def test_the_models_come_from_hermes_cache_of_the_default_provider(self):
        a = agents.get("hermes")
        self.assertEqual(a.read_models(), a.describe["models"])  # no cache: the built-in list
        (self.home / "provider_models_cache.json").write_text(json.dumps(
            {"openai-codex": {"models": ["gpt-6.1-sol", "gpt-6.1-terra"]}, "anthropic": {"models": ["x"]}}))
        self.assertEqual(a.read_models(), ["gpt-6.1-sol", "gpt-6.1-terra"])
        self.assertEqual(hermes.read_models("anthropic"), ["x"])


class Setup(unittest.TestCase):
    def test_the_passthrough_command_lists_maxpm_variables(self):
        got = hermes.env_passthrough_command()
        self.assertEqual(got[:4], ["hermes", "config", "set", "terminal.env_passthrough"])
        for name in ("MAXPM_AGENT", "MAXPM_FOCUS", "MAXPM_DB"):
            self.assertIn(name, got[4])
        self.assertEqual(hermes.SETUP["instructions"], "AGENTS.md")


if __name__ == "__main__":
    unittest.main()
