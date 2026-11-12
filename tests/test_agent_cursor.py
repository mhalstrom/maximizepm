"""The Cursor CLI adapter, river/agents/cursor.py (#5175), against the screens and transcripts recorded from
cursor-agent 2026.11.11-043ef28 in tests/fixtures/cursor."""
import hashlib
import json
import os
import re
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from river import agents, core, server
from river.agents import cursor

FIX = Path(__file__).resolve().parent / "fixtures" / "cursor"
CHAT_ID = "7a14bd07-35ce-499a-8a3a-9b8c2333855d"


def lines(name="transcript.jsonl"):
    return (FIX / name).read_text(encoding="utf-8").splitlines()


def screen(name):
    return server._prompt_tail((FIX / f"screen-{name}.txt").read_text(encoding="utf-8"))


class Env(unittest.TestCase):
    """A temporary queue and home, so no test reads or writes the person's own ~/.cursor."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / "home").mkdir()
        env = mock.patch.dict(os.environ, {"MAXPM_DB": str(self.root / "t.db"), "HOME": str(self.root / "home")})
        env.start()
        self.addCleanup(env.stop)
        agents.load(refresh=True)
        self.addCleanup(lambda: agents.load(refresh=True))
        self.a = agents.get("cursor")


class Describe(Env):
    def test_it_is_registered_and_valid(self):
        self.assertEqual(agents.check(cursor, "cursor"), [])
        self.assertIn("cursor", agents.ids())
        self.assertEqual(self.a.missing(), [])
        self.assertIs(agents.get("cursor-agent"), self.a)
        self.assertEqual(self.a.describe["family"], "multi")

    def test_no_name_of_another_agent(self):
        # ~/.local/bin/agent is the Grok CLI on this Mac: the adapter does not claim that name.
        self.assertNotIn("agent", self.a.names())

    def test_a_command_inside_cursor_finds_it(self):
        # Cursor sets CURSOR_AGENT=1 for the commands it runs; one started from a Claude Code shell keeps CLAUDECODE.
        self.assertIs(agents.from_env({"CURSOR_AGENT": "1", "CLAUDECODE": "1"}), self.a)
        self.assertIsNot(agents.from_env({"CLAUDECODE": "1"}), self.a)

    def test_state_from_the_screen_and_the_id_from_the_environment(self):
        self.assertEqual(self.a.state, {"state": "detect", "session_id": "identity"})
        self.assertEqual(self.a.identity["source"], "env")
        self.assertEqual(self.a.identity["env"], "CURSOR_CONVERSATION_ID")
        self.assertIsNone(self.a.install_hooks("/tmp", ["maxpm", "report"]))  # nothing goes into ~/.cursor


class Launch(Env):
    def test_a_plain_start(self):
        self.assertEqual(self.a.build_command("w1", "/w", None, None, {}, "run maxpm go"),
                         ["cursor-agent", "run maxpm go"])

    def test_the_effort_goes_into_the_model_id(self):
        argv = self.a.build_command("w1", "/w", "claude-opus-5-5", "high", {}, "go")
        self.assertEqual(argv[1:3], ["--model", "claude-opus-5-5-high"])
        self.assertEqual(cursor.model_word("gpt-5.6-sol", "low"), "gpt-5.6-sol-low")

    def test_no_effort_for_auto_an_id_with_one_or_no_model(self):
        self.assertEqual(cursor.model_word("auto", "high"), "auto")
        self.assertEqual(cursor.model_word("claude-opus-5-5-max", "low"), "claude-opus-5-5-max")
        self.assertEqual(cursor.model_word("gpt-5.6-sol-high-fast", "low"), "gpt-5.6-sol-high-fast")
        self.assertIsNone(cursor.model_word(None, "high"))
        self.assertNotIn("--model", self.a.build_command(None, None, None, "high", {}, "go"))

    def test_the_ladder_names_map_to_cursor_ids(self):
        ids = self.a.describe["model_ids"]
        self.assertEqual(ids["opus"], "claude-opus-5-5")
        for name in self.a.describe["models"]:
            self.assertRegex(ids.get(name, name), r"^[a-z0-9][a-z0-9.-]*$")

    def test_the_settings_go_on_by_their_flags(self):
        argv = self.a.build_command("w1", "/w", None, None, {"force": "on", "auto_review": "off", "sandbox": "enabled",
                                                              "mode": "plan", "trust": "on", "approve_mcps": "on",
                                                              "args": "--worktree x"}, "go")
        self.assertEqual(argv, ["cursor-agent", "--force", "--sandbox", "enabled", "--mode", "plan", "--trust",
                                "--approve-mcps", "--worktree x", "go"])
        self.assertIsInstance(argv[-2], agents.Raw)

    def test_each_setting_reads_back_from_the_command_line(self):
        on = {"force": "on", "auto_review": "on", "sandbox": "disabled", "mode": "ask", "trust": "on",
              "approve_mcps": "on"}
        line = " ".join(self.a.build_command("w1", "/w", None, None, on, "go"))
        for spec in cursor.SETTINGS:
            if spec["read"]:
                m = re.search(spec["read"], line)
                self.assertIsNotNone(m, spec["key"])
                self.assertIn(m.group(1), (on[spec["key"]], *spec["flag"]), spec["key"])

    def test_the_effort_reads_back_from_the_command_line(self):
        line = " ".join(self.a.build_command("w1", "/w", "claude-opus-5-5", "xhigh", {}, "go"))
        self.assertEqual(re.search(cursor.EFFORT["read"], line).group(1), "xhigh")
        self.assertIsNone(re.search(cursor.EFFORT["read"], "cursor-agent --model auto go"))

    def test_resume(self):
        self.assertEqual(self.a.resume_command(CHAT_ID, None, None, {"prompt": "go on", "force": "on"}),
                         ["cursor-agent", "--resume", CHAT_ID, "--force", "go on"])
        self.assertIsNone(self.a.resume_command("", None, None, {}))

    def test_parse_command(self):
        got = self.a.parse_command(shlex.split("cursor-agent --model {model} --yolo --sandbox enabled --plan 'run go'"))
        self.assertEqual(got, {"settings": {"force": "on", "sandbox": "enabled", "mode": "plan"}, "prompt": "run go"})
        self.assertIsNone(self.a.parse_command(["cursor-agent", "-p", "hi"]))  # print mode: no session
        self.assertIsNone(self.a.parse_command(["cursor-agent", "--model", "auto", "hi"]))  # a fixed model
        self.assertIsNone(self.a.parse_command(["agent", "hi"]))  # the Grok CLI's name

    def test_a_built_command_parses_back(self):
        s = {"force": "on", "sandbox": "enabled", "mode": "ask"}
        argv = self.a.build_command(None, None, None, None, s, "go")
        self.assertEqual(self.a.parse_command(argv), {"settings": s, "prompt": "go"})


class Chat(Env):
    def test_the_workspace_keys(self):
        self.assertEqual(cursor.workspace_key("/private/tmp/claude-501/-Users-x/a.b/ws"), "private-tmp-claude-501-Users-x-a-b-ws")
        self.assertEqual(cursor.chats_key("/"), hashlib.md5(b"/").hexdigest())

    def _transcript(self, folder):
        d = self.root / "projects" / cursor.workspace_key(folder) / "agent-transcripts" / CHAT_ID
        d.mkdir(parents=True)
        (d / f"{CHAT_ID}.jsonl").write_text((FIX / "transcript.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
        return d / f"{CHAT_ID}.jsonl"

    def test_chat_path_by_folder_and_by_id(self):
        p = self._transcript("/Users/person/work/app")
        self.assertEqual(self.a.chat_path(CHAT_ID, "/Users/person/work/app", root=self.root / "projects"), p)
        self.assertEqual(self.a.chat_path(CHAT_ID, None, root=self.root / "projects"), p)
        self.assertIsNone(self.a.chat_path("../../etc", None, root=self.root / "projects"))
        self.assertIsNone(self.a.chat_path("0000aaaa-0000", None, root=self.root / "projects"))

    def test_chat_path_in_the_home_folder(self):
        d = self.root / "home" / ".cursor" / "projects" / "Users-person-app" / "agent-transcripts" / CHAT_ID
        d.mkdir(parents=True)
        (d / f"{CHAT_ID}.jsonl").write_text("", encoding="utf-8")
        self.assertEqual(self.a.chat_path(CHAT_ID), d / f"{CHAT_ID}.jsonl")

    def test_the_recorded_chat(self):
        entries, turn = self.a.parse_chat(lines())
        self.assertEqual(turn, "idle")
        users = [e["text"] for e in entries if e["role"] == "user"]
        self.assertEqual(users, ["Run the shell command date, then tell me the year in one line.",
                                 "Write the word ok into a new file named out.txt",
                                 "What word did you write into out.txt? One line.",  # the prompt of the resume
                                 "Say the word blue. Nothing else."])
        tools = [(e["name"], e["detail"]) for e in entries if e["role"] == "tool"]
        self.assertEqual(tools, [("Shell", "date"), ("Write", "/Users/person/work/app/out.txt"),
                                 ("Read", "/Users/person/work/app/out.txt")])
        self.assertEqual(len({e["id"] for e in entries if e["role"] == "tool"}), 3)
        self.assertIn("I'll run `date` and report the year.", [e["text"] for e in entries if e["role"] == "assistant"])
        # Cursor's own line after a dropped connection is an event, not something the person typed.
        self.assertTrue(any(e["role"] == "event" and "interrupted" in e["text"] for e in entries))
        for e in entries:
            self.assertIn(e["role"], agents.ENTRY_ROLES)

    def test_a_turn_that_runs(self):
        _, turn = self.a.parse_chat(lines()[:2])
        self.assertEqual(turn, "working")
        self.assertEqual(self.a.parse_chat([]), ([], None))

    def test_a_turn_that_failed(self):
        entries, turn = self.a.parse_chat(lines("transcript-error.jsonl"))
        self.assertEqual(turn, "idle")
        self.assertEqual(entries[0]["role"], "event")
        self.assertIn("Model unavailable on Free", entries[0]["text"])

    def test_a_line_that_is_no_json_is_left_out(self):
        entries, _ = self.a.parse_chat(["{not json", lines()[0]])
        self.assertEqual([e["role"] for e in entries], ["user"])

    def test_meta(self):
        d = self.root / "home" / ".cursor" / "chats" / cursor.chats_key("/w") / CHAT_ID
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps({"title": "Date And Year", "cwd": "/w"}), encoding="utf-8")
        self.assertEqual(cursor.meta(CHAT_ID, "/w")["title"], "Date And Year")
        self.assertEqual(cursor.meta(CHAT_ID)["cwd"], "/w")
        self.assertIsNone(cursor.meta("ffffffff-0000"))


class Detect(Env):
    """The pane watcher's own readers (server._prompt_found, server._screen_state) on the recorded screens."""

    def look(self, name):
        tail = screen(name)
        parts = server._prompt_found(tail, server._AnyOf(cursor.DETECT["blocked"]), server._layout(cursor.DETECT))
        return ("blocked" if parts else server._screen_state(tail, cursor.DETECT)), parts

    def test_working(self):
        for name in ("working", "working-tool", "working-edit"):
            self.assertEqual(self.look(name)[0], "working", name)

    def test_idle(self):
        for name in ("idle", "idle-start"):
            self.assertEqual(self.look(name)[0], "idle", name)

    def test_a_permission_menu_is_blocked(self):
        state, parts = self.look("blocked-shell")
        self.assertEqual(state, "blocked")
        self.assertEqual(parts["question"], "Run this command?")
        self.assertEqual(parts["options"][0], ("Run (once) (y)", True))
        self.assertEqual(len(parts["options"]), 4)
        self.assertRegex(parts["options"][-1][0], cursor.DETECT["tell_instead"])

    def test_the_trust_question_is_blocked(self):
        state, parts = self.look("trust")
        self.assertEqual(state, "blocked")
        self.assertEqual(parts["question"], "Do you trust the contents of this directory?")
        self.assertEqual([o for o, _ in parts["options"]], ["[a] Trust this workspace", "[q] Quit"])

    def test_the_alert_names_the_question_and_keys(self):
        text, q, _ = server.prompt_alert("w1", "s:1", screen("blocked-shell"), layout=server._layout(cursor.DETECT))
        self.assertEqual(q, "Run this command?")
        self.assertIn("Enter = Run (once) (y) (selected now)", text)

    def test_reconnecting_after_a_turn_is_no_work(self):
        tail = ["  ok", " ⠘⠤ Reconnecting (attempt 1, 0s)", "  → Add a follow-up", "  Auto · 6.3%"]
        self.assertEqual(server._screen_state(tail, cursor.DETECT), "idle")


class Commands(Env):
    def test_the_built_in_list(self):
        names = [c["name"] for c in self.a.commands]
        self.assertIn("model", names)
        self.assertIn("resume", names)
        self.assertEqual(len(names), len(set(names)))

    def test_custom_commands_and_skills(self):
        home, ws = self.root / "home", self.root / "ws"
        (ws / ".cursor" / "commands").mkdir(parents=True)
        (ws / ".cursor" / "commands" / "ship.md").write_text("---\ndescription: Ship it\nargument-hint: <what>\n---\nBody", encoding="utf-8")
        (home / ".claude" / "commands").mkdir(parents=True)
        (home / ".claude" / "commands" / "ship.md").write_text("# Another ship\n", encoding="utf-8")  # the folder's one wins
        (home / ".claude" / "commands" / "model.md").write_text("# Not the built-in\n", encoding="utf-8")
        (home / ".cursor" / "skills" / "tidy").mkdir(parents=True)
        (home / ".cursor" / "skills" / "tidy" / "SKILL.md").write_text("---\nname: tidy-up\ndescription: Tidy\n---\n", encoding="utf-8")
        got = cursor.read_commands(ws, home=home)
        self.assertEqual(got, [{"name": "ship", "description": "Ship it", "hint": "<what>"},
                               {"name": "tidy-up", "description": "Tidy", "hint": ""}])
        self.assertEqual(self.a.read_commands(ws)[-2:], got)  # ~ is HOME: the test's home

    def test_the_slash_list_takes_the_folder_from_the_chats_meta_when_the_log_names_none(self):
        # #5301: Cursor's transcript has no cwd; ~/.cursor/chats/<key>/<id>/meta.json has.
        from river import sessions
        ws = self.root / "ws"
        (ws / ".cursor" / "commands").mkdir(parents=True)
        (ws / ".cursor" / "commands" / "ship.md").write_text("---\ndescription: Ship it\n---\nBody", encoding="utf-8")
        chat = self.root / "home" / ".cursor" / "chats" / cursor.chats_key(ws) / CHAT_ID
        chat.mkdir(parents=True)
        (chat / "meta.json").write_text(json.dumps({"cwd": str(ws)}), encoding="utf-8")
        conn = core.connect()
        self.addCleanup(conn.close)
        core.register(conn, "cur1")
        conn.execute("UPDATE agents SET agent_type='cursor' WHERE name='cur1'")
        conn.execute("INSERT INTO agent_sessions (agent, session_id, first_seen) VALUES ('cur1', ?, '2026-11-11')",
                     (CHAT_ID,))
        conn.commit()
        with mock.patch.object(sessions, "transcript", return_value=("cursor", None)):
            got = sessions.session_commands(conn, "cur1")
        self.assertEqual((got["provider"], got["folder"]), ("cursor", str(ws)))
        self.assertIn("ship", [c["name"] for c in got["commands"]])


if __name__ == "__main__":
    unittest.main()


class Setup(Env):
    """write_mcp_config puts MaximizePM's MCP server into ~/.cursor/mcp.json (#5250); HOME is the test's home."""

    def file(self):
        return self.root / "home" / ".cursor" / "mcp.json"

    def test_it_adds_the_server_to_a_new_file_without_the_chat_flag(self):
        got = self.a.write_mcp_config()
        self.assertTrue(got.startswith(f"added MaximizePM to {self.file()}"), got)
        self.assertIn("cursor-agent mcp enable maxpm", got)
        entry = json.loads(self.file().read_text(encoding="utf-8"))["mcpServers"]["maxpm"]
        want = agents.setup_entry()
        self.assertEqual((entry["command"], entry["args"]), (want["command"], want["args"]))
        self.assertEqual(entry["env"], {"MAXPM_DB": str(self.root / "t.db")})  # a folder session, not a chat
        self.assertFalse(self.file().with_name("mcp.json.bak").exists())

    def test_the_other_servers_and_keys_stay_and_the_old_file_is_kept(self):
        self.file().parent.mkdir(parents=True)
        old = {"mcpServers": {"github": {"url": "https://example.test/mcp"}}, "other": 1}
        self.file().write_text(json.dumps(old), encoding="utf-8")
        self.a.write_mcp_config()
        new = json.loads(self.file().read_text(encoding="utf-8"))
        self.assertEqual(new["mcpServers"]["github"], old["mcpServers"]["github"])
        self.assertEqual(new["other"], 1)
        self.assertEqual(json.loads(self.file().with_name("mcp.json.bak").read_text(encoding="utf-8")), old)
        self.assertIn("nothing changed", self.a.write_mcp_config())  # the same entry again

    def test_remove_takes_only_maxpm_out(self):
        self.assertIn("has no MaximizePM entry", self.a.write_mcp_config(remove=True))
        self.file().parent.mkdir(parents=True)
        self.file().write_text(json.dumps({"mcpServers": {"github": {"url": "https://example.test/mcp"}}}), encoding="utf-8")
        self.a.write_mcp_config()
        self.assertIn("took MaximizePM out", self.a.write_mcp_config(remove=True))
        self.assertEqual(json.loads(self.file().read_text(encoding="utf-8")), {"mcpServers": {"github": {"url": "https://example.test/mcp"}}})

    def test_another_file_and_a_file_that_is_no_json(self):
        project = self.root / "proj" / ".cursor" / "mcp.json"
        self.a.write_mcp_config(str(project))
        self.assertIn("maxpm", json.loads(project.read_text(encoding="utf-8"))["mcpServers"])
        self.assertFalse(self.file().exists())
        project.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(core.RiverError):
            self.a.write_mcp_config(str(project))
        self.assertEqual(project.read_text(encoding="utf-8"), "{ not json")  # left as it was
        project.write_text(json.dumps({"mcpServers": []}), encoding="utf-8")
        with self.assertRaises(core.RiverError):
            self.a.write_mcp_config(str(project))
