"""The Goose adapter (river/agents/goose.py, #5178), tested with fixtures recorded from goose 1.57.0 in tmux
(tests/fixtures/goose: the pane screens of idle, working and blocked, and one session's rows of its sessions.db)."""
import json
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from river import core, server, sessions  # noqa: F401 - core first: river.agents imports it
from river import agents
from river.agents import goose

FIX = Path(__file__).parent / "fixtures" / "goose"


def pane(name):
    return (FIX / f"pane_{name}.txt").read_text()


def session_row():
    return json.loads((FIX / "session_row.json").read_text())


def message_rows():
    return [json.loads(line) for line in (FIX / "session_messages.jsonl").read_text().splitlines() if line.strip()]


def watch(screen):
    """What the pane watcher of maxpm serve (server.watch_prompts) reads from a screen with goose's DETECT:
    (state, the prompt's parts or None)."""
    ad = agents.get("goose")
    tail = server._prompt_tail(server._plain(screen))
    rx = server._AnyOf(ad.detect["blocked"])
    parts = server._prompt_found(tail, rx, server._layout(ad.detect))
    return ("blocked" if parts else server._screen_state(tail, ad.detect)), parts


class Root(unittest.TestCase):
    """A goose root (GOOSE_PATH_ROOT), a HOME and a queue (MAXPM_DB) in a temporary folder."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.root, self.home, self.queue = base / "goose", base / "home", base / "queue"
        self.home.mkdir()
        self.queue.mkdir()
        env = mock.patch.dict(os.environ, {"GOOSE_PATH_ROOT": str(self.root), "HOME": str(self.home),
                                           "MAXPM_DB": str(self.queue / "t.db")})
        env.start()
        self.addCleanup(env.stop)

    def make_db(self, sessions=(), messages=(), models=(), ledger=()):
        """A sessions.db with the tables and columns that the adapter reads, filled with the given rows."""
        db = self.root / "data" / "sessions" / "sessions.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(db)
        cols = goose._SESSION_COLS
        c.execute(f"CREATE TABLE IF NOT EXISTS sessions ({', '.join(cols)})")
        c.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, message_id, session_id, "
                  "role, content_json, created_timestamp, metadata_json)")
        c.execute("CREATE TABLE IF NOT EXISTS provider_inventory_entries (inventory_key, provider_id)")
        c.execute("CREATE TABLE IF NOT EXISTS provider_inventory_models (inventory_key, ordinal, model_id)")
        c.execute("CREATE TABLE IF NOT EXISTS usage_ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id, "
                  "created_timestamp, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)")
        for s in sessions:
            c.execute(f"INSERT INTO sessions VALUES ({','.join('?' * len(cols))})", [s.get(k) for k in cols])
        for m in messages:
            c.execute("INSERT INTO messages (message_id, session_id, role, content_json, created_timestamp, "
                      "metadata_json) VALUES (?,?,?,?,?,?)",
                      [m[k] for k in ("message_id", "session_id", "role", "content_json", "created_timestamp",
                                      "metadata_json")])
        for key, provider, ordinal, model in models:
            c.execute("INSERT INTO provider_inventory_entries VALUES (?,?)", (key, provider))
            c.execute("INSERT INTO provider_inventory_models VALUES (?,?,?)", (key, ordinal, model))
        for row in ledger:
            c.execute("INSERT INTO usage_ledger (session_id, created_timestamp, input_tokens, output_tokens, "
                      "cache_read_tokens, cache_write_tokens) VALUES (?,?,?,?,?,?)", row)
        c.commit()
        c.close()
        return db

    def recorded_lines(self):
        """The lines of the copy of the recorded session (chat_path from a database with its rows)."""
        self.make_db([session_row()], message_rows())
        return Path(goose.chat_path("20261111_1")).read_text().splitlines()


class Contract(unittest.TestCase):
    def test_goose_is_registered_and_has_its_parts(self):
        a = agents.get("goose")
        self.assertIs(a.module, goose)
        self.assertEqual(agents.check(goose, "goose"), [])
        self.assertEqual(a.missing(), [])
        for part in ("describe", "settings", "effort", "launch", "identity", "chat", "state", "detect", "input",
                     "resume", "commands", "setup"):
            self.assertTrue(a.supports(part), part)
        self.assertIs(agents.find("goose-cli"), a)
        self.assertEqual(a.config_key("mode"), "goose_mode")
        self.assertEqual(a.effort_name("xhigh"), "max")
        self.assertEqual(a.chat_ref(), "session")

    def test_a_command_inside_goose_is_known_by_its_session_variable(self):
        self.assertEqual(agents.from_env({"AGENT_SESSION_ID": "20261111_1"}).id, "goose")
        self.assertEqual(agents.from_env({"AGENT_SESSION_ID": "20261111_1", "CLAUDECODE": "1"}).id, "goose")
        self.assertEqual(agents.from_env({"CLAUDECODE": "1"}).id, "claude")


class Launch(unittest.TestCase):
    def test_a_start_with_a_prompt_runs_it_and_stays_interactive(self):
        argv = agents.get("goose").build_command("w-1", "/Users/me/work", "ollama/qwen2.5:3b", "xhigh",
                                                 {"mode": "approve", "max_turns": "40", "builtins": "todo",
                                                  "args": "--debug"}, "Run maxpm go")
        self.assertEqual(argv, ["env", "GOOSE_MODE=approve", "GOOSE_THINKING_EFFORT=max", "goose", "run",
                                "--interactive", "--name", "w-1", "--provider", "ollama", "--model", "qwen2.5:3b",
                                "--max-turns", "40", "--with-builtin", "todo", "--debug", "--text", "Run maxpm go"])
        self.assertIsInstance(argv[-3], agents.Raw)  # the person's own shell text goes in as it is

    def test_a_start_with_no_prompt_nor_settings_is_a_plain_session(self):
        self.assertEqual(goose.build_command("w-1", "/x", None, None, {}, None), ["goose", "session", "--name", "w-1"])
        self.assertEqual(goose.build_command(None, None, "gpt-5", None, {}, "")[-2:], ["--model", "gpt-5"])

    def test_resume_takes_an_id_or_a_name_and_a_prompt_after_it(self):
        self.assertEqual(goose.resume_command("20261111_1", None, None, {}),
                         ["goose", "session", "--resume", "--session-id", "20261111_1"])
        self.assertEqual(goose.resume_command("w-1", "ollama/qwen2.5:3b", "low", {"mode": "auto"}),
                         ["env", "GOOSE_MODE=auto", "GOOSE_THINKING_EFFORT=low", "goose", "session", "--resume",
                          "--name", "w-1", "--provider", "ollama", "--model", "qwen2.5:3b"])
        self.assertEqual(goose.resume_command("20261111_1", None, None, {"prompt": "go on"}, name="w-1"),
                         ["goose", "run", "--interactive", "--resume", "--session-id", "20261111_1", "--text", "go on"])
        self.assertIsNone(goose.resume_command("", None, None, {}))
        self.assertEqual(agents.get("goose").resume_command("20261111_1")[-1], "20261111_1")

    def test_parse_command_reads_a_command_that_a_profile_builds_exactly(self):
        words = ["env", "GOOSE_MODE=smart_approve", "GOOSE_THINKING_EFFORT={effort}", "goose", "run",
                 "--interactive", "--model", "{model}", "--max-turns", "40", "--with-builtin", "todo", "--text", "go"]
        self.assertEqual(goose.parse_command(words),
                         {"settings": {"mode": "smart_approve", "max_turns": "40", "builtins": "todo"},
                          "prompt": "go"})
        self.assertEqual(goose.parse_command(["/opt/homebrew/bin/goose", "run", "-s", "-t", "go"]),
                         {"settings": {}, "prompt": "go"})
        for bad in (["claude", "go"], ["goose", "session"], ["goose", "run", "--text", "go"],
                    ["goose", "run", "--interactive", "--model", "gpt-5", "--text", "go"],
                    ["env", "OPENAI_API_KEY=x", "goose", "run", "--interactive", "--text", "go"],
                    ["goose", "run", "--interactive", "--recipe", "r.yaml", "--text", "go"]):
            self.assertIsNone(goose.parse_command(bad), bad)

    def test_each_setting_is_read_back_from_its_command_line(self):
        line = " ".join(goose.build_command("w", "/x", None, None, {"mode": "approve", "max_turns": "7",
                                                                    "builtins": "todo"}, None))
        got = {s["key"]: re.search(s["read"], line).group(1) for s in goose.SETTINGS if s["read"]}
        self.assertEqual(got, {"mode": "approve", "max_turns": "7", "builtins": "todo"})


class Screens(unittest.TestCase):
    """The pane screens recorded in tmux (120 x 40, the blank lines at the end left out), read as maxpm serve reads
    them."""

    def test_idle_and_working_screens(self):
        for name, state in {"idle": "idle", "idle_after_turn": "idle", "resumed": "idle",
                            "working": "working"}.items():
            self.assertEqual(watch(pane(name)), (state, None), name)

    def test_screens_that_say_neither_keep_the_state(self):
        # The person types at the prompt; the first prompt of goose run --text runs with no spinner.
        for name in ("idle_typed", "working_first_prompt"):
            self.assertEqual(watch(pane(name)), (None, None), name)

    def test_the_compact_question_is_blocked_with_its_question(self):
        state, parts = watch(pane("blocked_compact"))
        self.assertEqual(state, "blocked")
        self.assertEqual(parts["question"], "◆  Are you sure you want to compact this conversation?")
        self.assertEqual(parts["options"][0][1], True)

    @unittest.expectedFailure  # #5209: the watcher reads no cliclack menu yet (○ marks each other option)
    def test_the_tool_approval_is_blocked_with_its_options(self):
        state, parts = watch(pane("blocked_tool"))
        self.assertEqual(state, "blocked")
        self.assertEqual(parts["options"], [("Allow (Allow the tool call once)", True), ("Always Allow", False),
                                            ("Deny", False), ("Cancel", False)])

    def test_the_blocked_words_find_the_open_menus_and_not_the_answered_one(self):
        rx = server._AnyOf(goose.DETECT["blocked"])
        for name, found in {"blocked_tool": True, "blocked_compact": True, "idle_after_turn": False,
                            "working": False}.items():
            tail = server._prompt_tail(pane(name))
            self.assertEqual(any(rx.search(x) for x in tail), found, name)
        self.assertIn("◇  Goose would like to call the above tool", pane("idle_after_turn"))

    def test_the_session_id_is_read_from_the_header_at_a_start_and_a_resume(self):
        for name, sid in {"idle": "20261111_1", "resumed": "20261111_1", "working": "20261111_1",
                          "working_first_prompt": "20261111_4"}.items():
            self.assertEqual(re.search(goose.IDENTITY["pane"], pane(name)).group(1), sid, name)


class Chat(Root):
    def test_the_recorded_session_reads_as_the_person_saw_it(self):
        entries, turn = goose.parse_chat(self.recorded_lines())
        self.assertEqual([(e["role"], e.get("name") or e.get("kind") or e["text"][:30]) for e in entries], [
            ("user", "Use the shell tool to run the "),
            ("tool", "shell"),
            ("event", "Asks to approve shell"),
            ("result", "hello-goose"),
            ("assistant", "The command `echo hello-goose`"),
            ("user", "/compact"),
            ("divider", "compacted"),
            ("summary", "# Conversation Summary\n\n## Use"),
        ])
        self.assertEqual(turn, "idle")
        tool, result = entries[1], entries[3]
        self.assertEqual((tool["id"], tool["detail"]), ("call_zaozjzd0", "echo hello-goose"))
        self.assertEqual((result["for"], result["error"]), ("call_zaozjzd0", False))
        self.assertEqual(entries[0]["at"], "2026-11-11T07:46:15Z")
        self.assertFalse(any("<turn-context>" in e["text"] for e in entries))

    def test_the_sessions_tab_reads_the_copy_line_by_line(self):
        lines = self.recorded_lines()
        path = goose.chat_path("maxpm-test")  # by its name too
        got = sessions.entries("goose", path)
        self.assertEqual([e["role"] for e in got["entries"]], [e["role"] for e in goose.parse_chat(lines)[0]])
        self.assertEqual(got["turn"], "idle")

    def test_the_turn_is_working_until_the_assistant_answers(self):
        lines = self.recorded_lines()  # a session line, then the 13 message rows
        turns = [goose.parse_chat(lines[:n])[1] for n in (2, 3, 6, 8, 9)]
        self.assertEqual(turns, [None, "working", "working", "working", "idle"])

    def test_the_copy_grows_with_the_database_and_starts_again_when_it_is_removed(self):
        rows = message_rows()
        db = self.make_db([session_row()], rows[:8])
        path = Path(goose.chat_path("20261111_1"))
        self.assertEqual(path.parent, Path(core.river_dir()) / "goose")
        first = path.read_text()
        self.assertEqual(len(first.splitlines()), 1 + 8)
        self.assertEqual(goose.chat_path("20261111_1"), path)
        self.assertEqual(path.read_text(), first)  # nothing new: nothing written
        self.make_db(messages=rows[8:])
        c = sqlite3.connect(db)
        c.execute("UPDATE sessions SET accumulated_input_tokens=99999 WHERE id='20261111_1'")
        c.commit()
        c.close()
        goose.chat_path("20261111_1")
        text = path.read_text()
        self.assertTrue(text.startswith(first))  # only appended: a cursor into the copy stays valid
        added = [json.loads(x) for x in text[len(first):].splitlines()]
        self.assertEqual(added[0]["session"]["accumulated_input_tokens"], 99999)
        self.assertEqual([d["row"] for d in added[1:]], [9, 10, 11, 12, 13])  # 13: the start row of the resume
        self.assertTrue(added[3]["summary"].startswith("# Conversation Summary"))
        self.assertEqual(goose.chat_usage(text.splitlines())["input"], 99999)
        path.unlink()
        self.assertEqual(len(Path(goose.chat_path("20261111_1")).read_text().splitlines()), 1 + 13)

    def test_no_database_or_no_session_is_no_chat(self):
        self.assertIsNone(goose.chat_path("20261111_1"))
        self.make_db([session_row()])
        self.assertIsNone(goose.chat_path("20261111_9"))
        self.assertIsNone(goose.chat_requests("20261111_9"))
        self.assertIsNone(agents.get("goose").chat_path(None))

    def test_an_image_and_an_error_are_entries(self):
        user = {"role": "user", "created_timestamp": 1794383175,
                "metadata_json": json.dumps({"userVisible": True, "agentVisible": True}),
                "content_json": json.dumps([{"type": "text", "text": "what is this"},
                                            {"type": "image", "data": "iVBO", "mimeType": "image/png"}])}
        bad = {"role": "assistant", "created_timestamp": 1794383176, "metadata_json": json.dumps({"userVisible": True}),
               "content_json": json.dumps([{"type": "toolResponse", "id": "c1", "toolResult": {
                   "status": "error", "error": "denied"}}, {"type": "error", "error": "rate limit"}])}
        entries, turn = goose.parse_chat([json.dumps(user), json.dumps(bad)])
        self.assertEqual(entries[0]["images"], ["data:image/png;base64,iVBO"])
        self.assertEqual((entries[1]["role"], entries[1]["error"], entries[1]["text"]), ("result", True, "denied"))
        self.assertEqual((entries[2]["role"], entries[2]["text"]), ("event", "rate limit"))
        self.assertEqual(turn, "idle")

    def test_title_folder_usage_settings_and_effort_come_from_the_session_line(self):
        lines = self.recorded_lines()
        self.assertEqual(goose.chat_title(lines), "maxpm-test")
        self.assertEqual(goose.chat_cwd(lines), "/Users/me/work")
        self.assertEqual(goose.chat_usage(lines), {"input": 11512, "output": 212, "cache_read": 5395,
                                                   "cache_write": 0})
        self.assertEqual(goose.chat_settings(lines), {"mode": "approve"})
        self.assertEqual(goose.chat_usage(lines[1:]), {"input": 5379 + 5421, "output": 22 + 23, "cache_read": 5392,
                                                       "cache_write": 0})
        self.assertIsNone(re.search(goose.EFFORT["read"], lines[0]))
        # The model_config_json of a session that goose run started with GOOSE_THINKING_EFFORT=low (recorded).
        row = dict(session_row(), model_config_json='{"model_name":"qwen2.5:3b","temperature":null,'
                   '"max_tokens":null,"toolshim":false,"toolshim_model":null,'
                   '"request_params":{"thinking_effort":"low"}}')
        self.assertEqual(re.search(goose.EFFORT["read"], json.dumps({"session": row})).group(1), "low")

    def test_the_usage_ledger_gives_each_model_call(self):
        self.make_db([session_row()], ledger=[("20261111_1", 1794383187, 5379, 22, 0, None),
                                              ("20261111_1", 1794383206, 5421, 23, 5392, None),
                                              ("other_1", 1794383206, 1, 1, 1, 1)])
        requests, fork = goose.chat_requests("maxpm-test")
        self.assertFalse(fork)
        self.assertEqual(list(requests.values()), [
            ("2026-11-11T07:46:27Z", {"input": 5379, "output": 22, "cache_read": 0, "cache_write": 0, "write_1h": 0,
                                      "write_5m": 0}),
            ("2026-11-11T07:46:46Z", {"input": 5421, "output": 23, "cache_read": 5392, "cache_write": 0,
                                      "write_1h": 0, "write_5m": 0})])

    def test_the_artifacts_tab_reads_the_texts_and_the_written_files(self):
        lines = self.recorded_lines()
        texts = [t for line in lines for t in goose.chat_texts(line.encode())]
        self.assertIn(('{"command": "echo hello-goose"}', "2026-11-11T07:46:27Z", "named"), texts)
        self.assertFalse(any(t[0] == "hello-goose" for t in texts))  # tool results are left out
        write = {"role": "assistant", "created_timestamp": 1794383187, "metadata_json": "{}", "content_json": json.dumps(
            [{"type": "toolRequest", "id": "c2", "toolCall": {"status": "success", "value": {
                "name": "write", "arguments": {"path": "/Users/me/work/notes.md", "content": "x"}}}}])}
        self.assertEqual(goose.chat_texts(json.dumps(write).encode()),
                         [("/Users/me/work/notes.md", "2026-11-11T07:46:27Z", "wrote")])


class Paths(Root):
    def test_the_paths_follow_xdg_without_a_root(self):
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/d", "XDG_CONFIG_HOME": "/c"}):
            os.environ.pop("GOOSE_PATH_ROOT")
            self.assertEqual(goose.sessions_db(), Path("/d/goose/sessions/sessions.db"))
            self.assertEqual(goose.config_dir(), Path("/c/goose"))
        with mock.patch.dict(os.environ, {"GOOSE_PATH_ROOT": "relative/root"}):
            for k in ("XDG_DATA_HOME", "XDG_CONFIG_HOME"):
                os.environ.pop(k, None)
            self.assertEqual(goose.sessions_db(), self.home / ".local/share/goose/sessions/sessions.db")

    def test_read_models_reads_goose_s_model_cache(self):
        self.assertIsNone(goose.read_models())
        self.make_db(models=[("k1", "ollama", 0, "qwen2.5:3b"), ("k2", "anthropic", 0, "claude-sonnet-4-5")])
        self.assertEqual(agents.get("goose").read_models(), ["anthropic/claude-sonnet-4-5", "ollama/qwen2.5:3b"])


class Commands(Root):
    def test_recipes_and_skills_are_custom_commands_and_a_built_in_name_is_left_out(self):
        cfg = self.root / "config"
        cfg.mkdir(parents=True)
        (cfg / "config.yaml").write_text("GOOSE_MODE: approve\nslash_commands:\n- command: release\n"
                                         "  recipe_path: /r/release.yaml\n- recipe_path: /r/c.yaml\n"
                                         "  command: compact\nplugins: {}\n")
        work = self.home / "work"
        for base, name, desc in ((work / ".agents" / "skills", "lint-all", "Lint every file"),
                                 (self.home / ".claude" / "skills", "Ship It", "Ship the release"),
                                 (self.root / ".agents" / "plugins" / "p1" / "skills", "plug", "From a plugin")):
            (base / name).mkdir(parents=True)
            (base / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: '{desc}'\n---\nbody\n")
        got = goose.read_commands(str(work))
        self.assertEqual([(c["name"], c["description"]) for c in got], [
            ("release", "Run the recipe /r/release.yaml"), ("lint-all", "Lint every file"),
            ("ship-it", "Ship the release"), ("plug", "From a plugin")])
        names = [c["name"] for c in agents.get("goose").read_commands(str(work))]
        self.assertEqual(names[:3], ["help", "exit", "mode"])
        self.assertEqual(names.count("compact"), 1)

    def test_no_config_and_no_skills_is_no_custom_command(self):
        self.assertEqual(goose.read_commands(str(self.home)), [])


if __name__ == "__main__":
    unittest.main()
