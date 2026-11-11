"""The Grok CLI adapter, river/agents/grok.py (#5172): its describe, settings, effort, launch, resume, identity, hooks
(state), commands and setup parts. The hook events follow the payloads of Grok's own hook guide
(~/.grok/docs/user-guide/10-hooks.md of Grok 1.0.5); the hook script runs for real, with a real maxpm report."""
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from river import agents, core

grok = importlib.import_module("river.agents.grok")
ROOT = Path(__file__).resolve().parents[1]

# The shape of ~/.grok/models_cache.json of Grok 1.0.5: two models, grok-4.5 without xhigh, and one hidden model.
MODELS_CACHE = {"fetched_at": "2026-08-27T22:17:56Z", "grok_version": "1.0.5", "models": {
    "grok-4.6": {"info": {"id": "grok-4.6", "name": "Grok 4.6", "hidden": False, "supports_reasoning_effort": True,
                          "reasoning_effort": "high", "reasoning_efforts": [
                              {"id": "xhigh", "value": "xhigh", "default": False},
                              {"id": "high", "value": "high", "default": True},
                              {"id": "medium", "value": "medium", "default": False},
                              {"id": "low", "value": "low", "default": False}]}},
    "grok-4.5": {"info": {"id": "grok-4.5", "name": "Grok 4.5", "hidden": False, "supports_reasoning_effort": True,
                          "reasoning_efforts": [{"id": "high"}, {"id": "medium"}, {"id": "low"}]}},
    "grok-fast": {"info": {"id": "grok-fast", "hidden": False, "supports_reasoning_effort": False}},
    "grok-internal": {"info": {"id": "grok-internal", "hidden": True}},
}}

REPORT = ["maxpm", "report"]


class Home(unittest.TestCase):
    """GROK_HOME in a temporary folder, and the home folder there too, so no test reads the person's own ~/.grok."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.gh = self.home / ".grok"
        self.gh.mkdir()
        env = mock.patch.dict(os.environ, {"GROK_HOME": str(self.gh), "HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        p = mock.patch.object(Path, "home", classmethod(lambda cls: self.home))
        p.start()
        self.addCleanup(p.stop)


class Contract(Home):
    def test_the_module_passes_the_check_and_gives_each_part(self):
        self.assertEqual(agents.check(grok, "grok"), [])
        a = agents.Adapter(grok, "builtin")
        self.assertEqual(a.missing(), [])
        for part in agents.PARTS:
            self.assertTrue(a.supports(part), part)
        self.assertEqual(a.names(), ["grok", "grok-cli", "grok-build"])
        self.assertTrue(a.inside({"GROK_SESSION_ID": "01a1c999"}))
        self.assertFalse(a.inside({"CLAUDECODE": "1"}))
        self.assertEqual(a.config_key("permission_mode"), "grok_permission_mode")
        self.assertEqual((a.effort_name("max"), a.effort_name("high")), ("xhigh", "high"))

    def test_models_and_effort_levels_come_from_grok_s_cache(self):
        self.assertIsNone(grok.read_models())
        self.assertIsNone(grok.read_effort_levels("grok-4.6"))
        (self.gh / "models_cache.json").write_text(json.dumps(MODELS_CACHE))
        self.assertEqual(grok.read_models(), ["grok-fast", "grok-4.6", "grok-4.5"])
        self.assertEqual(grok.read_effort_levels("grok-4.6"), ["low", "medium", "high", "xhigh"])
        self.assertEqual(grok.read_effort_levels("grok-4.5"), ["low", "medium", "high"])
        self.assertEqual(grok.read_effort_levels("grok-fast"), [])
        self.assertIsNone(grok.read_effort_levels("grok-9"))
        (self.gh / "models_cache.json").write_text("{not json")
        self.assertIsNone(grok.read_models())


class Launch(Home):
    def setUp(self):
        super().setUp()
        self.a = agents.Adapter(grok, "builtin")

    def test_the_command_has_the_model_effort_settings_args_and_prompt(self):
        self.assertEqual(self.a.build_command("w1", "/p", None, None, {}, "go"), ["grok", "go"])
        got = self.a.build_command("w1", "/p", "grok-4.6", "max",
                                   {"permission_mode": "bypassPermissions", "sandbox": "workspace",
                                    "args": "--no-subagents --rules 'be brief'", "prompt": "ignored here"},
                                   "run maxpm go")
        self.assertEqual(got, ["grok", "-m", "grok-4.6", "--reasoning-effort", "xhigh", "--permission-mode",
                               "bypassPermissions", "--sandbox", "workspace", "--no-subagents --rules 'be brief'",
                               "run maxpm go"])
        self.assertIsInstance(got[-2], agents.Raw)  # the person's own shell text, as it is

    def test_resume_and_fork(self):
        self.assertEqual(self.a.resume_command("019ff6bd-6753", "grok-4.5", "low", {"prompt": "go on"}),
                         ["grok", "--resume", "019ff6bd-6753", "-m", "grok-4.5", "--reasoning-effort", "low", "go on"])
        self.assertEqual(self.a.resume_command("abc", None, None, {}), ["grok", "--resume", "abc"])
        self.assertIsNone(self.a.resume_command("", None, None, {}))
        self.assertEqual(self.a.build_command(None, None, None, None, {"fork": "abc"}, "go"),
                         ["grok", "--resume", "abc", "--fork-session", "go"])

    def test_an_old_command_reads_back_when_a_profile_builds_it_exactly(self):
        p = self.a.parse_command(["grok", "-m", "{model}", "--effort", "{effort}", "--permission-mode", "plan",
                                  "--sandbox", "read-only", "run maxpm go"])
        self.assertEqual(p, {"settings": {"permission_mode": "plan", "sandbox": "read-only"}, "prompt": "run maxpm go"})
        self.assertEqual(self.a.parse_command(["/Users/x/.grok/bin/grok", "--yolo", "go"]),
                         {"settings": {"permission_mode": "bypassPermissions"}, "prompt": "go"})
        for words in (["grok", "-m", "grok-4.6", "go"], ["grok", "--continue", "go"], ["grok"], ["claude", "go"],
                      ["grok", "--permission-mode", "nonsense", "go"], ["grok", "go", "two"]):
            self.assertIsNone(self.a.parse_command(words), words)


class Hooks(Home):
    def test_install_writes_the_agent_file_and_the_script_once_and_nothing_in_grok_home(self):
        q = self.home / "queue"
        words = grok.install_hooks("/p", REPORT, python="/usr/bin/python3", where=q)
        agent, script = q / "grok-agent.md", q / "grok-hook.py"
        self.assertEqual(words, ["--agent", str(agent)])
        text = agent.read_text()
        self.assertTrue(text.startswith("---\nname: maxpm\n"))
        self.assertIn("prompt_mode: extend\nagents_md: true\nhooks:\n", text)
        for ev in grok.HOOK_EVENTS:
            self.assertIn(f"  {ev}:\n    - hooks:\n        - type: command\n"
                          f"          command: \"/usr/bin/python3 {script} {ev}\"\n          timeout: 10\n", text)
        self.assertIn(grok.AGENT_BODY, text)
        code = script.read_text()
        self.assertIn(repr(REPORT), code)
        self.assertIn(repr(str(ROOT)), code)
        self.assertIn(repr(str(q / "grok-hooks")), code)
        before = (script.stat().st_mtime_ns, agent.stat().st_mtime_ns)
        time.sleep(0.01)
        grok.install_hooks("/p", REPORT, python="/usr/bin/python3", where=q)
        self.assertEqual(before, (script.stat().st_mtime_ns, agent.stat().st_mtime_ns))
        self.assertEqual(list(self.gh.iterdir()), [])
        self.assertEqual(self.home_launch(words), ["grok", f"--agent {agent}", "go"])

    def home_launch(self, words):
        return agents.Adapter(grok, "builtin").build_command("w1", "/p", None, None, {"args": " ".join(words)}, "go")


class States(Home):
    """hook_main through a sequence of events, as Grok sends them; the one maxpm command each ends as is recorded,
    and a --hook command is read back with read_hook, as maxpm report --hook grok reads it."""

    def setUp(self):
        super().setUp()
        self.calls = []
        self.state = self.home / "state"
        self.env = {"MAXPM_AGENT": "w1", "GROK_SESSION_ID": "s1", "GROK_HOME": str(self.gh)}

    def hook(self, event, now=1000.0, env=None, **payload):
        self.calls.clear()
        rc = grok.hook_main([event], REPORT, stdin=io.StringIO(json.dumps(payload)), env=env or self.env, now=now,
                            run=lambda argv, data, env: self.calls.append((argv, data)), state_dir=self.state)
        self.assertEqual(rc, 0)
        return [said(argv, data) for argv, data in self.calls]

    def test_a_turn_with_a_permission_menu(self):
        self.assertEqual(self.hook("SessionStart", source="startup"), ["report idle s1 -- grok --resume s1"])
        self.assertEqual(self.hook("UserPromptSubmit", promptId="p1", prompt="go"), ["hook working (typed: go)"])
        self.assertEqual(self.hook("PostToolUse", now=1010, toolName="read_file"), [])  # spaced out
        self.assertEqual(self.hook("Notification", now=1015, notificationType="permission_prompt",
                                   message="Tool permission requested"),
                         ["hook blocked shows (Tool permission requested)"])  # no tool call seen before it
        self.assertEqual(self.hook("PreToolUse", now=1018, toolName="run_terminal_command",
                                   toolInput={"command": "git push origin main", "description": "Push", "n": 3}), [])
        self.assertEqual(self.hook("Notification", now=1020, notificationType="permission_prompt",
                                   message="Tool permission requested"),
                         ["hook blocked shows (Tool permission requested) call run_terminal_command: "
                          "{'command': 'git push origin main', 'description': 'Push'}"])
        self.assertEqual(self.hook("PostToolUse", now=1030, toolName="run_terminal_command"), ["hook working"])
        self.assertEqual(self.hook("PostToolUse", now=1095), ["hook working"])  # 60 s on: renewed
        self.assertEqual(self.hook("Stop", now=1100, promptId="p1", reason="end_turn", stopHookActive=False,
                                   backgroundTasks=[{"id": "b1", "type": "shell", "status": "running",
                                                     "command": "make  check"},
                                                    {"id": "b2", "type": "subagent", "status": "completed"}],
                                   sessionCrons=[{"id": "c1", "schedule": "every 5 minutes", "prompt": "x"}]),
                         ["hook idle waits ['shell: make check', 'wakeup every 5 minutes']"])
        self.assertEqual(self.hook("Notification", now=1160, notificationType="idle_prompt"), [])  # idle already
        self.assertEqual(self.hook("PostToolUse", now=1170, isBackgrounded=True), [])  # its turn ended
        self.assertEqual(self.hook("SessionEnd", reason="exit"), ["release"])
        self.assertFalse((self.state / "s1.state").exists())
        self.assertEqual(self.hook("Stop", reason="shutdown"), [])  # Grok runs it late, after SessionEnd
        self.assertEqual(list(self.state.iterdir()), [])

    def test_a_new_session_removes_the_state_of_sessions_that_ended_unseen(self):
        self.state.mkdir()
        old, fresh = self.state / "gone.state", self.state / "other.state"
        old.write_text("{}")
        fresh.write_text("{}")
        os.utime(old, (1, 1))
        self.hook("SessionStart", now=time.time())
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), ["other.state", "s1.state"])

    def test_a_prompt_whose_hook_runs_before_session_start_keeps_the_session_working(self):
        self.assertEqual(self.hook("UserPromptSubmit", promptId="p1", prompt="go"), ["hook working (typed: go)"])
        self.assertEqual(self.hook("SessionStart", source="startup"), ["report working s1 -- grok --resume s1"])

    def test_a_late_turn_end_of_an_older_prompt_changes_nothing(self):
        self.hook("UserPromptSubmit", promptId="p1")
        self.hook("UserPromptSubmit", promptId="p2")
        self.assertEqual(self.hook("StopCancelled", promptId="p1", reason="user_interrupt"), [])
        self.assertEqual(self.hook("StopFailure", promptId="p2", error="rate_limit"), ["hook idle"])
        self.hook("UserPromptSubmit", promptId="p3")
        self.assertEqual(self.hook("StopCancelled", promptId="bash-turn", reason="user_interrupt"),
                         ["hook idle"])  # never seen to start: idle

    def test_what_is_not_the_session_s_own_state_reports_nothing(self):
        self.hook("UserPromptSubmit", promptId="p1")
        self.assertEqual(self.hook("Stop", promptId="c1", subagentType="explore", reason="end_turn"), [])
        self.assertEqual(self.hook("Stop", reason="shutdown"), [])  # the session's end: SessionEnd follows
        self.assertEqual(self.hook("Notification", notificationType="task_complete"), [])
        self.assertEqual(self.hook("PreCompact"), [])
        self.assertEqual(self.hook("UserPromptSubmit", promptId="p2", env={"GROK_SESSION_ID": "s1"}), [])  # no agent
        self.assertEqual(self.hook("Stop", env={"MAXPM_AGENT": "w1", "GROK_SESSION_ID": "../x"}), [])

    def test_the_session_id_comes_from_the_payload_when_the_variable_is_missing(self):
        self.hook("SessionStart", env={"MAXPM_AGENT": "w1"}, sessionId="s7")
        self.assertEqual(self.calls[0][0][-1], "s7")

    def test_a_broken_payload_never_fails_the_hook(self):
        rc = grok.hook_main(["Stop"], REPORT, stdin=io.StringIO("{not json"), env=self.env, now=1,
                            run=lambda argv, data, env: self.calls.append(argv), state_dir=self.state)
        self.assertEqual((rc, len(self.calls)), (0, 1))
        rc = grok.hook_main(["UserPromptSubmit"], REPORT, stdin=io.StringIO("{}"), env=self.env, now=1,
                            run=lambda argv, data, env: 1 / 0, state_dir=self.state)
        self.assertEqual(rc, 0)

    def test_read_hook_alone_reads_grok_s_own_input(self):
        self.assertEqual(grok.read_hook({"hookEventName": "stop_cancelled", "sessionId": "s1"})["state"], "idle")
        self.assertEqual(grok.read_hook({"hookEventName": "user_prompt_submit", "prompt": "hi"})["prompt"], "hi")
        self.assertIsNone(grok.read_hook({"hookEventName": "pre_compact"}))
        self.assertIsNone(grok.read_hook({}))
        rep = agents.get("grok").read_hook({"maxpmEvent": "Notification", "notificationType": "permission_prompt"})
        self.assertEqual((rep["state"], rep["shows"], rep["call"], rep["event"]),
                         ("blocked", True, None, "Notification:permission_prompt"))


def said(argv, data):
    """One maxpm command of the hook, in a few words."""
    if argv[-1] == "release":
        return "release"
    if "--hook" in argv:
        self_check = argv[-2:] == ["--hook", "grok"] and argv[:2] == REPORT
        assert self_check, argv
        rep = agents.get("grok").read_hook(json.loads(data))
        out = f"hook {rep['state']}" + (" shows" if rep["shows"] else "")
        out += f" ({rep['message']})" if rep["message"] else ""
        out += f" call {rep['call']['tool']}: {rep['call']['input']}" if rep["call"] else ""
        out += f" waits {rep['waits']}" if rep["waits"] else ""
        out += f" (typed: {rep['prompt']})" if rep["prompt"] else ""
        return out
    i = argv.index("--seq")
    int(argv[i + 1])
    rest = argv[:i] + argv[i + 2:]
    assert rest[:3] == REPORT + ["--state"], rest
    return f"report {rest[3]} " + " ".join(rest[5:])


class HookScript(Home):
    """The installed script runs as Grok runs it: its own process, the event's JSON on stdin, the session's
    environment. It becomes maxpm report (the CLI of this tree), which writes to a real queue."""

    def test_the_script_reports_to_the_queue(self):
        db = str(self.home / "t.db")
        conn = core.connect(db)
        self.addCleanup(conn.close)
        core.register(conn, "w1")
        report = [sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv.pop(1)); from river.cli import main; "
                  "main()", str(ROOT), "report"]
        grok.install_hooks("/p", report, where=self.home)
        script = self.home / "grok-hook.py"
        env = {**os.environ, "MAXPM_DB": db, "MAXPM_AGENT": "w1", "GROK_SESSION_ID": "s1", "MAXPM_QUIET": "1"}

        def run(event, **payload):
            subprocess.run([sys.executable, str(script), event], input=json.dumps(payload), text=True, env=env,
                           timeout=60, check=True)
            return conn.execute("SELECT activity_event, activity_prompt, report_session, resume_argv FROM agents "
                                "WHERE name='w1'").fetchone()

        row = run("SessionStart", source="startup")
        self.assertEqual((row["activity_event"], row["report_session"]), ("report:idle", "s1"))
        self.assertEqual(json.loads(row["resume_argv"]), ["grok", "--resume", "s1"])
        self.assertEqual(run("UserPromptSubmit", promptId="p1", prompt="go")["activity_event"], "report:working")
        run("PreToolUse", toolName="run_terminal_command", toolInput={"command": "rm notes.txt"})
        row = run("Notification", notificationType="permission_prompt", message="Tool permission requested")
        self.assertEqual(row["activity_event"], "report:blocked")
        self.assertEqual(json.loads(row["activity_prompt"]),
                         {"tool": "run_terminal_command", "input": {"command": "rm notes.txt"}})
        self.assertEqual(run("Stop", promptId="p1", reason="end_turn")["activity_event"], "report:idle")
        self.assertEqual(run("SessionEnd", reason="exit")["activity_event"], core.REPORT_GONE)


FIX = ROOT / "tests" / "fixtures" / "grok"
SID = "01a1c999-4608-7093-917c-9c43a1592baa"


class Recorded(Home):
    """A real Grok 1.0.77 session (tests/fixtures/grok): four turns (a long command, a file write and git add, an
    rm behind a permission menu, a turn cancelled with Ctrl+C), /quit, then grok --resume with one more prompt. Its
    update log, the payloads of its hooks (from an agent file, as install_hooks writes it), and its pane screens."""

    def test_the_log_is_found_by_session_id_under_the_real_path_of_the_folder(self):
        d = self.gh / "sessions" / "%2Fwork%2Frepo" / SID
        d.mkdir(parents=True)
        (d / "updates.jsonl").write_text("")
        a = agents.Adapter(grok, "builtin")
        self.assertEqual(a.chat_path(SID), d / "updates.jsonl")
        with mock.patch("os.path.realpath", lambda p: "/work/repo"):
            self.assertEqual(a.chat_path(SID, "/tmp/link-to-repo"), d / "updates.jsonl")
        for bad in ("", "../x", ".hidden", "nope"):
            self.assertIsNone(a.chat_path(bad), bad)

    def test_the_chat_log_reads_into_the_shared_entries(self):
        lines = (FIX / "updates.jsonl").read_text().splitlines()
        entries, turn = agents.Adapter(grok, "builtin").parse_chat(lines)
        self.assertEqual(turn, "idle")  # the resumed turn ended
        for e in entries:
            self.assertIn(e["role"], agents.ENTRY_ROLES)
            self.assertRegex(e["at"], r"^2026-11-11T07:5\d:\d\dZ$")
        users = [e["text"] for e in entries if e["role"] == "user"]
        self.assertEqual(users, ["Run this shell command and tell me its output: env | grep -i -E '^(grok|agent|xai)' "
                                 "| cut -d= -f1 | sort; sleep 20; echo finished",
                                 "Create a file named notes.txt that contains the word hello, then run: git add notes.txt",
                                 "Delete notes.txt with the shell command: rm notes.txt",
                                 "Run the shell command: sleep 40; echo done", "Reply with the single word: pong"])
        tools = [(e["name"], e["detail"]) for e in entries if e["role"] == "tool"]
        self.assertEqual(tools[1:4], [("write", "/work/repo/notes.txt"),
                                      ("run_terminal_command", "git add notes.txt && git status --short notes.txt"),
                                      ("run_terminal_command", "rm notes.txt")])
        results = {e["for"]: e for e in entries if e["role"] == "result"}
        first = next(e for e in entries if e["role"] == "tool")
        self.assertEqual(results[first["id"]]["text"], "GROK_AGENT\nGROK_SESSION_ID\nGROKFX\nfinished\n")
        self.assertFalse(results[first["id"]]["error"])
        sleep = next(e for e in entries if e["role"] == "tool" and e["detail"] == "sleep 40; echo done")
        self.assertNotIn(sleep["id"], results)  # the cancelled command never completed
        events = [e["text"] for e in entries if e["role"] == "event"]
        self.assertEqual(events, ["mode: plan", "mode: default", "turn ended: cancelled"])
        said = [e["text"] for e in entries if e["role"] == "assistant"]
        self.assertTrue(said[1].startswith("```\nGROK_AGENT\n") and said[1].endswith("DONE"))
        self.assertTrue(all(not e["text"].startswith("The user wants") for e in entries))  # no thoughts
        cut = next(i for i, l in enumerate(lines) if '"user_message_chunk"' in l and "sleep 40" in l)
        self.assertEqual(grok.parse_chat(lines[:cut + 1])[1], "working")

    def test_usage_counts_the_turns(self):
        lines = (FIX / "updates.jsonl").read_text().splitlines()
        got = grok.chat_usage(lines)
        turns = [json.loads(l)["params"]["update"]["usage"] for l in lines if '"turn_completed"' in l]
        self.assertEqual(len(turns), 5)
        self.assertEqual(got["output"], sum(t["outputTokens"] for t in turns))
        self.assertEqual(got["cache_read"], sum(t["cachedReadTokens"] for t in turns))
        self.assertEqual(got["input"], sum(t["inputTokens"] - t["cachedReadTokens"] for t in turns))
        self.assertEqual(got["cache_write"], 0)

    def test_each_pane_screen_matches_its_state_only(self):
        det = grok.DETECT
        import re

        def state(text):
            if any(re.search(p, text) for p in det["blocked"]):
                return "blocked"
            if re.search(det["working"], text):
                return "working"
            return "idle" if re.search(det["idle"], text) else None

        want = {"pane_blocked_trust.txt": "blocked", "pane_blocked_permission.txt": "blocked",
                "pane_working_thinking.txt": "working", "pane_working_tool.txt": "working",
                "pane_idle.txt": "idle", "pane_idle_start.txt": "idle", "pane_slash.txt": "idle"}
        for name, s in want.items():
            self.assertEqual(state((FIX / name).read_text()), s, name)
        self.assertRegex((FIX / "pane_blocked_permission.txt").read_text(), det["tell_instead"])

    def test_the_recorded_hook_events_report_each_change_of_state(self):
        rows = [json.loads(l) for l in (FIX / "hooks.jsonl").read_text().splitlines()]
        self.assertEqual({r["env"]["GROK_SESSION_ID"] for r in rows}, {SID})
        self.assertTrue(all(r["env"]["MAXPM_AGENT"] for r in rows))  # the hook has the session's environment
        from datetime import datetime
        calls, state_dir = [], self.home / "state"
        for r in rows:
            now = datetime.fromisoformat(r["payload"]["timestamp"]).timestamp()
            grok.hook_main([r["event"]], REPORT, stdin=io.StringIO(json.dumps(r["payload"])), env=r["env"], now=now,
                           run=lambda argv, data, env: calls.append(said(argv, data)), state_dir=state_dir)
        resume = f"report idle {SID} -- grok --resume {SID}"
        self.assertEqual(calls, [
            resume,
            "hook working (typed: Run this shell command and tell me its output: env | grep -i -E "
            "'^(grok|agent|xai)' | cut -d= -f1 | sort; sleep 20; echo finished)",
            "hook idle",
            "hook working (typed: Create a file named notes.txt that contains the word hello, then run: "
            "git add notes.txt)",
            "hook idle",
            "hook working (typed: Delete notes.txt with the shell command: rm notes.txt)",
            "hook blocked shows (Tool permission requested) call run_terminal_command: "
            "{'command': 'rm notes.txt', 'description': 'Delete notes.txt with rm'}",
            "hook working", "hook idle",
            "hook working (typed: Run the shell command: sleep 40; echo done)", "hook idle",
            "release",
            resume,
            "hook working (typed: Reply with the single word: pong)", "hook idle"])


class Commands(Home):
    def test_built_in_commands_then_skills_and_command_files_nearest_first(self):
        proj = self.home / "work" / "app"
        (proj / ".grok" / "skills" / "ship").mkdir(parents=True)
        (proj / ".grok" / "skills" / "ship" / "SKILL.md").write_text(
            "---\nname: ship\ndescription: \"Ship the app\"\nargument-hint: <version>\n---\nbody\n")
        (proj / ".grok" / "skills" / "quiet").mkdir()
        (proj / ".grok" / "skills" / "quiet" / "SKILL.md").write_text("---\nname: quiet\nuser-invocable: false\n---\n")
        (self.home / "work" / ".claude" / "commands").mkdir(parents=True)
        (self.home / "work" / ".claude" / "commands" / "review.md").write_text("Review the diff.\n")
        (self.gh / "skills" / "ship").mkdir(parents=True)
        (self.gh / "skills" / "ship" / "SKILL.md").write_text("---\nname: ship\ndescription: the user's own\n---\n")
        (self.gh / "commands").mkdir()
        (self.gh / "commands" / "standup.md").write_text("---\ndescription: Notes\n---\n")
        got = agents.Adapter(grok, "builtin").read_commands(str(proj))
        names = [c["name"] for c in got]
        self.assertEqual(names[:len(grok.COMMANDS)], [c["name"] for c in grok.COMMANDS])
        custom = got[len(grok.COMMANDS):]
        self.assertEqual(custom, [{"name": "ship", "description": "Ship the app", "hint": "<version>"},
                                  {"name": "review", "description": "", "hint": ""},
                                  {"name": "standup", "description": "Notes", "hint": ""}])


if __name__ == "__main__":
    unittest.main()


def watch(text):
    """What maxpm serve's pane watcher (server.watch_prompts) reads from a screen with Grok's DETECT: (state,
    question, options). state: blocked, working, idle, or None (the screen says neither)."""
    from river import server
    tail = server._prompt_tail(server._plain(text))
    parts = server._prompt_found(tail, server._AnyOf(grok.DETECT["blocked"]), server._layout(grok.DETECT))
    if parts:
        return "blocked", parts["question"], parts["options"]
    return server._screen_state(tail, grok.DETECT), None, None


# The free usage limit, rebuilt from the lines that #5189 quoted (no capture of it was kept): a ┃ box with the
# sentence and a numbered menu, and the key hint under it; above it, the end of the chat.
USAGE_LIMIT = """
  I will add the test next.

  ┃
  ┃  You hit your free usage limit.
  ┃
  ┃  1 ({one}) Upgrade to SuperGrok
  ┃  2 ({two}) Wait until the limit resets
  ┃

  Tab:next answer  │  Esc:scrollback  │  Shift+x:dismiss
"""


class Watcher(unittest.TestCase):
    """#5261: the pane watcher itself (not only the regexes) finds each blocked screen of Grok, with its question and
    its menu, and reads the other screens as working or idle."""

    def test_the_trust_question_is_blocked_above_its_version_line(self):
        self.assertEqual(watch((FIX / "pane_blocked_trust.txt").read_text()),
                         ("blocked", "Do you trust the contents of this directory?", []))

    def test_the_permission_menu_has_its_header_and_options_with_the_mark_after_the_number(self):
        state, question, options = watch((FIX / "pane_blocked_permission.txt").read_text())
        self.assertEqual((state, question), ("blocked", "Delete notes.txt with rm"))
        self.assertEqual(options, [("Yes, and don't ask again for anything (always-approve mode)", False),
                                   ("Always allow: rm notes.txt", False), ("Yes, proceed", True),
                                   ("No, reject (type to add feedback)", False), ("Never allow: rm notes.txt", False)])
        from river import server
        self.assertRegex(options[3][0], server._layout(grok.DETECT)["tell"])

    def test_the_free_usage_limit_is_blocked_with_or_without_a_selected_option(self):
        self.assertEqual(watch(USAGE_LIMIT.format(one="●", two="○")),
                         ("blocked", "You hit your free usage limit.",
                          [("Upgrade to SuperGrok", True), ("Wait until the limit resets", False)]))
        self.assertEqual(watch(USAGE_LIMIT.format(one="○", two="○"))[0], "blocked")

    def test_the_other_screens_and_quoted_words_are_not_blocked(self):
        want = {"pane_working_thinking.txt": "working", "pane_working_tool.txt": "working",
                "pane_idle.txt": "idle", "pane_idle_start.txt": "idle", "pane_slash.txt": "idle"}
        for name, s in want.items():
            self.assertEqual(watch((FIX / name).read_text()), (s, None, None), name)
        # The words of a blocked screen in the chat, with the input box under them: no prompt.
        idle = (FIX / "pane_idle.txt").read_text().splitlines()
        quoted = "\n".join(["  You hit your free usage limit. Do you trust the contents of this directory?"] + idle)
        self.assertEqual(watch(quoted)[0], "idle")

    def test_the_process_of_a_release_build_is_found_by_its_command_line(self):
        import re
        rx = grok.DETECT["process_args"]
        for line in ("grok-1.0.77-mac --agent /q/grok-agent.md", "/opt/homebrew/bin/grok", "grok --resume abc"):
            self.assertRegex(line, rx)
        for line in ("grokker run", "/x/grok-notes.txt", "python3 grok.py"):
            self.assertIsNone(re.search(rx, line), line)
