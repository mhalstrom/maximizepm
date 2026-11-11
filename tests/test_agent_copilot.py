"""The GitHub Copilot CLI adapter (river/agents/copilot.py, #5176), tested on fixtures recorded from the real CLI.

tests/fixtures/copilot was recorded from Copilot CLI 1.0.109 in a tmux pane on macOS, 2026-11-11, with a local model
(Ollama): events.jsonl is a session's chat log (two tool calls, a permission menu, an ask_user form, a resume and a
/compact; strings over 1200 characters cut), hooks.jsonl the stdin payloads of the plugin's hooks in a second session
(a permission menu, an ask_user form, /exit), and pane_*.txt the screens of each state. Home and temporary paths are
replaced by /Users/me and /tmp/proj. events_github.jsonl is one short chat with a GitHub-hosted model (Copilot Free).
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from river import agents, core
from river.agents import copilot

FIX = Path(__file__).resolve().parent / "fixtures" / "copilot"
MAXPM = [sys.executable, str(Path(__file__).resolve().parent.parent / "bin" / "maxpm")]


def fixture(name):
    return (FIX / name).read_text(encoding="utf-8")


def hook_calls():
    return [json.loads(line) for line in fixture("hooks.jsonl").splitlines() if line.strip()]


def hook_program():
    """The hook program's functions, as the plugin runs them (it imports nothing of river)."""
    ns = {"__name__": "maxpm_hook"}
    exec(compile(copilot.HOOK_SCRIPT, "maxpm_hook.py", "exec"), ns)
    return ns


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.db = str(self.root / "t.db")
        env = mock.patch.dict(os.environ, {"MAXPM_DB": self.db, "COPILOT_HOME": str(self.root / "copilot-home"),
                                           "HOME": str(self.root / "home")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MAXPM_AGENT", None)
        agents.load(refresh=True)
        self.addCleanup(lambda: agents.load(refresh=True))


class Contract(Env):
    def test_it_is_registered_and_passes_the_check_with_each_part(self):
        a = agents.get("copilot")
        self.assertEqual(agents.check(a.module, "copilot"), [])
        self.assertEqual((a.source, a.missing()), ("builtin", []))
        for part in ("describe", "settings", "effort", "launch", "identity", "chat", "state", "detect", "input",
                     "resume", "commands", "setup"):
            self.assertTrue(a.supports(part), part)
        self.assertIs(agents.get("GitHub-Copilot"), a)
        self.assertEqual(a.describe["family"], "multi")
        self.assertEqual(a.config_key("permissions"), "copilot_permissions")
        self.assertEqual(a.state, {"state": "hook", "session_id": "hook"})

    def test_no_choice_is_blank_since_the_page_adds_the_agents_own_option_itself(self):
        # #5235: the launch dialog and Settings > Setup put 'the agent's own' (value '') first; a '' in the choices
        # showed a second, blank option. The default '' still means the agent's own, and starts no flag.
        a = agents.get("copilot")
        for key in ("permissions", "mode"):
            spec = next(x for x in a.settings if x["key"] == key)
            self.assertNotIn("", spec["choices"], key)
            self.assertEqual(spec["default"], "", key)
        words = a.build_command("w1", "/p", None, None, {"permissions": "", "mode": ""}, "go")
        self.assertFalse([w for w in words if w in ("--mode",) or w.startswith(("--allow", "--assisted"))])

    def test_a_command_inside_copilot_is_copilots_also_when_it_was_started_from_claude_code(self):
        self.assertEqual(agents.from_env({"COPILOT_CLI": "1", "CLAUDECODE": "1"}).id, "copilot")
        self.assertEqual(agents.from_env({"CLAUDECODE": "1"}).id, "claude")
        self.assertIsNone(agents.from_env({}))


class Launch(Env):
    def test_a_new_session_and_a_resume(self):
        a = agents.get("copilot")
        q = core.river_dir()
        words = a.build_command("w1", "/p", "auto", "high", {"permissions": "allow-all-tools", "mode": "plan",
                                                              "args": "--no-color"}, "do #12")
        self.assertEqual(words, ["copilot", "--name", "w1", "--model", "auto", "--reasoning-effort", "high",
                                 "--allow-all-tools", "--mode", "plan", "--add-dir", q, "--no-color", "-i", "do #12"])
        self.assertIsInstance(words[-3], agents.Raw)
        self.assertEqual(a.build_command(None, None, None, None, {}, None), ["copilot", "--add-dir", q])
        # A resumed session keeps its name: no --name.
        self.assertEqual(a.resume_command("368ff8ca", None, None, {"prompt": "go on"}, name="w1"),
                         ["copilot", "--resume", "368ff8ca", "--add-dir", q, "-i", "go on"])
        self.assertIsNone(a.resume_command("", None, None, {}))
        # The start paths of core build the shell command through the adapter (#5181), with the prompt setting, and
        # load the hook plugin that reports the state (#5214).
        self.assertEqual(core.build_command("copilot", {"prompt": "go", "permissions": "allow-all-tools"}, "auto"),
                         f"copilot --plugin-dir {Path(q) / 'copilot-plugin'} --model auto --allow-all-tools "
                         f"--add-dir {q} -i go")

    def test_a_hand_written_command_is_read_back_only_when_a_profile_builds_it_exactly(self):
        a = agents.get("copilot")
        self.assertEqual(a.parse_command(["/opt/homebrew/bin/copilot", "--model", "{model}", "--reasoning-effort",
                                          "{effort}", "--allow-all", "--mode", "autopilot", "--add-dir",
                                          "{maxpm_dir}", "-i", "go"]),
                         {"settings": {"permissions": "allow-all", "mode": "autopilot"}, "prompt": "go"})
        self.assertIsNone(a.parse_command(["copilot", "--model", "gpt-5.6-sol", "-i", "go"]))  # a fixed model
        self.assertIsNone(a.parse_command(["copilot", "--yolo", "-i", "go"]))
        self.assertIsNone(a.parse_command(["copilot", "--mode", "plan"]))  # no prompt
        self.assertIsNone(a.parse_command(["claude", "-i", "go"]))

    def test_the_effort_flag_and_its_read_back(self):
        a = agents.get("copilot")
        self.assertEqual(a.effort_name("xhigh"), "xhigh")
        line = '{"type": "session.model_change", "data": {"newModel": "gpt-5.6-sol", "reasoningEffort": "high"}}'
        self.assertEqual(re.search(a.effort["read"], line).group(1), "high")


class Hooks(Env):
    """State and session id come from the plugin's hooks (STATE). The calls are the real ones of hooks.jsonl."""

    def test_each_recorded_hook_call_reports_its_state(self):
        prog, got = hook_program(), []
        for call in hook_calls():
            args = prog["report_args"](call["event"], call["payload"])
            got.append((call["event"], args and (args[0] if args[0] == "release" else args[2])))
        self.assertEqual(got, [
            ("sessionStart", "idle"), ("userPromptSubmitted", "working"), ("preToolUse", None),
            ("permissionRequest", None),  # it runs also when Copilot allows the call by itself: no signal
            ("notification", "blocked"), ("postToolUse", "working"), ("agentStop", "idle"),
            ("userPromptSubmitted", "working"), ("preToolUse", None), ("notification", "blocked"),
            ("postToolUse", "working"), ("agentStop", "idle"), ("sessionEnd", "release")])

    def test_a_report_carries_the_session_id_the_seq_and_on_start_the_resume_command(self):
        prog, calls = hook_program(), hook_calls()
        start = prog["report_args"]("sessionStart", calls[0]["payload"])
        sid = calls[0]["payload"]["sessionId"]
        self.assertEqual(start, ["report", "--state", "idle", "--session-id", sid, "--seq",
                                 str(calls[0]["payload"]["timestamp"]), "--", "copilot", "--resume", sid])
        menu = prog["report_args"]("notification", calls[4]["payload"])
        self.assertEqual(menu[:4], ["report", "--state", "blocked", "--message"])
        self.assertEqual(menu[4], "Run command: touch hooks.txt")
        # The chat log writes the same hook input with notificationType.
        self.assertEqual(prog["report_args"]("notification", {"notificationType": "elicitation_dialog",
                                                              "message": "Information requested"})[2], "blocked")
        self.assertIsNone(prog["report_args"]("notification", {"notification_type": "shell_completed"}))

    def test_install_hooks_writes_the_plugin_once_and_returns_the_words_that_load_it(self):
        a = agents.get("copilot")
        words = a.install_hooks("/p", MAXPM + ["report"])
        d = Path(core.river_dir()) / copilot.PLUGIN_DIR
        self.assertEqual(words, ["--plugin-dir", str(d)])
        self.assertEqual(json.loads((d / "plugin.json").read_text())["name"], "maxpm")
        hooks = json.loads((d / "hooks.json").read_text())
        self.assertEqual(hooks["version"], 1)
        self.assertEqual(sorted(hooks["hooks"]), sorted(copilot.HOOK_EVENTS))
        h = hooks["hooks"]["agentStop"][0]
        self.assertEqual((h["type"], h["exec"], h["args"]), ("command", sys.executable,
                                                             [str(d / "maxpm_hook.py"), "agentStop", *MAXPM]))
        before = {f.name: f.stat().st_mtime_ns for f in d.iterdir()}
        a.install_hooks("/p", MAXPM + ["report"])
        self.assertEqual({f.name: f.stat().st_mtime_ns for f in d.iterdir()}, before)

    def run_hook(self, plugin, call, env):
        return subprocess.run([sys.executable, str(plugin / "maxpm_hook.py"), call["event"], *MAXPM],
                              input=json.dumps(call["payload"]), capture_output=True, text=True, env=env, timeout=60)

    def test_the_written_hook_program_reports_the_recorded_session_to_maxpm(self):
        """The plugin's program, run as Copilot runs it, against a real queue: the state of the Sessions tab
        follows the recorded hooks, and it prints nothing (Copilot reads a hook's output as a decision)."""
        conn = core.connect(self.db)
        self.addCleanup(conn.close)
        core.register(conn, "cop-1")
        plugin = Path(agents.get("copilot").install_hooks("/p", MAXPM + ["report"])[1])
        env = {**os.environ, "MAXPM_AGENT": "cop-1", "MAXPM_QUIET": "1"}

        def state():
            r = conn.execute("SELECT activity_event, activity_prompt, report_session, resume_argv FROM agents "
                             "WHERE name='cop-1'").fetchone()
            return dict(r)

        calls = hook_calls()
        sid = calls[0]["payload"]["sessionId"]
        seen = []
        for call in calls:
            done = self.run_hook(plugin, call, env)
            self.assertEqual((done.returncode, done.stdout), (0, ""), done.stderr)
            seen.append(state()["activity_event"])
            if call["event"] == "notification" and call["payload"]["notification_type"] == "permission_prompt":
                self.assertEqual(json.loads(state()["activity_prompt"]), {"message": "Run command: touch hooks.txt"})
        self.assertEqual(seen, ["report:idle", "report:working", "report:working", "report:working",
                                "report:blocked", "report:working", "report:idle", "report:working",
                                "report:working", "report:blocked", "report:working", "report:idle", "report:gone"])
        s = state()
        self.assertEqual(s["report_session"], sid)
        self.assertEqual(json.loads(s["resume_argv"]), ["copilot", "--resume", sid])
        self.assertTrue(conn.execute("SELECT 1 FROM agent_sessions WHERE agent='cop-1' AND session_id=?",
                                     (sid,)).fetchone())

    def test_the_hook_program_does_nothing_outside_a_maxpm_session(self):
        conn = core.connect(self.db)
        self.addCleanup(conn.close)
        core.register(conn, "cop-2")
        plugin = Path(agents.get("copilot").install_hooks("/p", MAXPM)[1])
        env = {k: v for k, v in os.environ.items() if k != "MAXPM_AGENT"}
        done = self.run_hook(plugin, hook_calls()[1], env)
        self.assertEqual((done.returncode, done.stdout), (0, ""))
        self.assertIsNone(conn.execute("SELECT activity_event FROM agents WHERE name='cop-2'").fetchone()[0])


class Chat(Env):
    def lines(self):
        return fixture("events.jsonl").splitlines()

    def test_the_recorded_chat_log_becomes_the_shared_entries(self):
        entries, turn = agents.get("copilot").parse_chat(self.lines())
        self.assertEqual(turn, "idle")
        roles = [e["role"] for e in entries]
        self.assertEqual(roles.count("user"), 5)
        users = [e["text"] for e in entries if e["role"] == "user"]
        self.assertEqual(users[0], "Use the bash tool to run: cat notes.txt")
        self.assertEqual(users[-1], "Reply with the single word: done")
        tools = [(e["name"], e["detail"]) for e in entries if e["role"] == "tool"]
        self.assertIn(("bash", "touch made.txt"), tools)
        call = next(e for e in entries if e["role"] == "tool" and e["detail"] == "cat notes.txt")
        result = next(e for e in entries if e["role"] == "result" and e["for"] == call["id"])
        self.assertTrue(result["text"].startswith("hello\n"))
        self.assertFalse(result["error"])
        self.assertTrue(any(e["role"] == "result" and e["error"] for e in entries))  # the ask_user call it refused
        events = [e["text"] for e in entries if e["role"] == "event"]
        self.assertEqual(events, ["Asks permission: touch made.txt", 'Answered: {"color": "red"}'])
        self.assertEqual([e["kind"] for e in entries if e["role"] == "divider"], ["resumed", "compacted"])
        summary = next(e for e in entries if e["role"] == "summary")
        self.assertIn("made.txt", summary["text"])
        self.assertEqual(entries[-1], {"at": entries[-1]["at"], "role": "assistant", "text": "done"})
        self.assertTrue(all(e["role"] in agents.ENTRY_ROLES and "text" in e for e in entries))

    def test_a_chat_with_a_github_hosted_model_reads_the_same_way(self):
        """events_github.jsonl: Copilot Fusion (--model auto) on Copilot Free picked gpt-6-luna; one permission."""
        lines = fixture("events_github.jsonl").splitlines()
        entries, turn = agents.get("copilot").parse_chat(lines)
        self.assertEqual([(e["role"], e["text"]) for e in entries if e["role"] != "result"], [
            ("user", "Use the bash tool to run: touch gh.txt"), ("tool", "bash: Create gh.txt"),
            ("event", "Asks permission: touch gh.txt"), ("assistant", "Created `gh.txt`.")])
        call = next(e for e in entries if e["role"] == "tool")
        # The model's own description of the command, as the Sessions row says it; the command when opened.
        self.assertEqual((call["plain"], call["detail"]), ("Creates gh.txt", "touch gh.txt"))
        self.assertEqual(turn, "idle")
        self.assertEqual(copilot.chat_settings(lines), {"model": "auto"})
        # Copilot's own total at the end of the session: 6 input tokens apart from the 16500 read from the cache.
        shutdown = json.loads(next(l for l in lines if '"type": "session.shutdown"' in l))["data"]
        u = copilot.chat_usage(lines)
        self.assertEqual(u["input"], shutdown["accountingSnapshot"]["tokenDetails"]["input"]["tokenCount"])
        self.assertEqual(u["cache_read"], shutdown["accountingSnapshot"]["tokenDetails"]["cache_read"]["tokenCount"])

    def test_the_turn_is_working_from_the_prompt_until_a_reply_that_asks_for_no_tool(self):
        lines = self.lines()
        parse = agents.get("copilot").parse_chat
        first = next(i for i, l in enumerate(lines) if '"type": "user.message"' in l)
        self.assertEqual(parse(lines[:first + 1])[1], "working")
        # The first reply asks for a tool: its turn end is no end of the agent's work.
        tool_end = next(i for i, l in enumerate(lines) if '"type": "assistant.turn_end"' in l)
        self.assertEqual(parse(lines[:tool_end + 1])[1], "working")
        # The Sessions tab reads one line at a time: each line says its own turn state.
        line = agents.get("copilot").parse_chat_line
        self.assertEqual(line(json.loads(lines[first]))[1], "working")
        self.assertIsNone(line(json.loads(lines[tool_end]))[1])
        self.assertEqual(parse([])[1], None)
        self.assertEqual(parse(["not json", "{}"]), ([], None))

    def test_cwd_usage_and_settings_of_the_log(self):
        lines = self.lines()
        self.assertEqual(copilot.chat_cwd(lines), "/tmp/proj")
        self.assertEqual(copilot.chat_settings(lines), {"model": "qwen3:1.7b"})
        u = copilot.chat_usage(lines)
        self.assertEqual(set(u), {"input", "output", "cache_read", "cache_write"})
        records = [json.loads(l)["data"]["usage"] for l in lines if '"type": "session.usage_record"' in l]
        self.assertEqual(u["output"], sum(r["outputTokens"] for r in records))
        self.assertEqual(u["input"] + u["cache_read"], sum(r["inputTokens"] for r in records))

    def test_the_log_is_found_by_session_id_under_copilot_home(self):
        a = agents.get("copilot")
        sid = "368ff8ca-7a3b-4270-bb8e-5487d0e8dccc"
        self.assertIsNone(a.chat_path(sid))
        f = Path(os.environ["COPILOT_HOME"]) / "session-state" / sid / "events.jsonl"
        f.parent.mkdir(parents=True)
        f.write_text("")
        self.assertEqual(a.chat_path(sid), f)
        self.assertIsNone(a.chat_path("../" + sid))


class Screens(Env):
    """DETECT on the recorded panes: the prompt layout for the pane watcher (the state itself comes from hooks)."""

    def test_each_screen_matches_only_its_own_rule(self):
        d = agents.get("copilot").detect
        for name, want in (("idle", "idle"), ("working", "working"), ("waiting_model", "working"),
                           ("permission", "blocked"), ("trust", "blocked"), ("question", "blocked")):
            text = fixture(f"pane_{name}.txt")
            got = {k for k in ("idle", "working") if re.search(d[k], text)}
            if any(re.search(p, text) for p in d["blocked"]):
                got.add("blocked")
            self.assertEqual(got, {want}, name)
        self.assertIn(d["tell_instead"], fixture("pane_permission.txt"))


class Commands(Env):
    def test_the_built_in_list_and_the_folders_commands_and_skills(self):
        a = agents.get("copilot")
        names = [c["name"] for c in a.commands]
        for n in ("model", "compact", "resume", "exit", "plan"):
            self.assertIn(n, names)
        self.assertEqual(len(names), len(set(names)))
        proj = self.root / "proj"
        (proj / ".claude" / "commands").mkdir(parents=True)
        (proj / ".claude" / "commands" / "count-files.md").write_text(
            "---\ndescription: Count the files\nargument-hint: <folder>\n---\nCount files in $ARGUMENTS\n")
        (proj / ".github" / "skills" / "greet-me").mkdir(parents=True)
        (proj / ".github" / "skills" / "greet-me" / "SKILL.md").write_text(
            "---\nname: greet-me\ndescription: Say a greeting to the user\n---\nSay hello.\n")
        mine = Path(os.environ["COPILOT_HOME"]) / "skills" / "tidy"
        mine.mkdir(parents=True)
        (mine / "SKILL.md").write_text("---\ndescription: Tidy the folder\n---\n")
        claude_own = Path(os.environ["HOME"]) / ".claude" / "commands"  # Copilot does not read ~/.claude
        claude_own.mkdir(parents=True)
        (claude_own / "secret.md").write_text("x")
        got = a.read_commands(proj)[len(a.commands):]
        self.assertEqual(got, [
            {"name": "count-files", "description": "Count the files", "hint": "<folder>"},
            {"name": "greet-me", "description": "Say a greeting to the user", "hint": ""},
            {"name": "tidy", "description": "Tidy the folder", "hint": ""}])


class Setup(Env):
    def test_the_mcp_server_goes_into_copilots_list_and_other_servers_stay(self):
        f = Path(os.environ["COPILOT_HOME"]) / "mcp-config.json"
        f.parent.mkdir(parents=True)
        f.write_text(json.dumps({"mcpServers": {"other": {"type": "local", "command": "x"}}}))
        self.assertEqual(copilot.write_mcp_config("http://127.0.0.1:8765/mcp"), [str(f)])
        data = json.loads(f.read_text())
        self.assertEqual(data["mcpServers"]["maxpm"], {"type": "http", "url": "http://127.0.0.1:8765/mcp",
                                                       "tools": ["*"]})
        self.assertIn("other", data["mcpServers"])
        self.assertTrue(f.with_name("mcp-config.json.bak").exists())
        self.assertEqual(copilot.write_mcp_config("http://127.0.0.1:8765/mcp"), [])
        f.write_text("{not json")
        with self.assertRaises(core.RiverError):
            copilot.write_mcp_config("http://127.0.0.1:8765/mcp")


if __name__ == "__main__":
    unittest.main()
