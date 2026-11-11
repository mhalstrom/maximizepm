"""The agent adapter contract, its registry and user override folder, and maxpm report / maxpm release (#5167)."""
import contextlib
import io
import json
import os
import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from unittest import mock

from river import agents, cli, core, sessions
from river.core import RiverError


class Env(unittest.TestCase):
    """A queue in a temporary folder, named by MAXPM_DB, so the override folder (agents/ beside it) is temporary."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db = os.path.join(self.tmp.name, "t.db")
        env = mock.patch.dict(os.environ, {"MAXPM_DB": db})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MAXPM_AGENT", None)
        self.c = core.connect(db)
        self.addCleanup(self.c.close)
        self.dir = Path(self.tmp.name).resolve() / "agents"
        agents.load(refresh=True)
        self.addCleanup(lambda: agents.load(refresh=True))

    def user_module(self, mid, text):
        self.dir.mkdir(exist_ok=True)
        (self.dir / f"{mid}.py").write_text(textwrap.dedent(text))
        agents.load(refresh=True)


def module(**parts):
    return types.SimpleNamespace(**parts)


GOOD = {"id": "pi", "label": "Pi", "exe": "pi", "aliases": ["pi-agent"], "family": "multi",
        "models": ["anthropic/claude-opus"], "default_model": "", "model_ids": {}, "prefix": "pi_"}


class Contract(Env):
    def test_the_built_in_adapters_pass_the_check_and_have_every_required_part(self):
        for mid in agents.BUILTIN:
            a = agents.get(mid)
            self.assertEqual(agents.check(a.module, mid), [])
            self.assertEqual(a.source, "builtin")
            self.assertEqual(a.missing(), [])
            for part in ("describe", "launch", "identity", "resume", "chat") + (("effort",) if mid in ("claude", "codex") else ()):
                self.assertTrue(a.supports(part), (mid, part))
            for part in ("state", "detect", "input") if mid in ("claude", "codex") else ():  # #5183
                self.assertTrue(a.supports(part), (mid, part))
            if mid in ("claude", "codex"):
                self.assertTrue(a.supports("commands"), mid)  # #5186
            if mid in ("claude", "codex"):
                self.assertTrue(a.supports("setup"), mid)  # #5184
        self.assertEqual(agents.ids()[:2], ["claude", "codex"])

    def test_a_module_with_no_launch_part_loads_but_cannot_start(self):
        a = agents.Adapter(module(DESCRIBE=GOOD), "test")
        self.assertEqual(a.missing(), ["launch"])
        with self.assertRaisesRegex(RiverError, "agent pi has no launch part"):
            a.build_command("w", "/tmp", "opus", None, {}, "go")
        self.assertIsNone(a.resume_command("s1"))

    def test_the_built_in_describe_gives_the_platform_names_that_settings_store(self):
        # No platform table is left (#5185): the platform name (claude-code, codex) is the adapter's.
        for mid, platform in (("claude", "claude-code"), ("codex", "codex")):
            d, pl = agents.get(mid).describe, core.platform_spec(platform)
            self.assertEqual((d["label"], d["exe"], d["family"], d["prefix"]), (pl["label"], pl["exe"], pl["family"], pl["prefix"]))
            self.assertIs(agents.get(platform), agents.get(mid))
            self.assertEqual(core._platform_of(agents.get(mid)), platform)
        for name in ("LAUNCH_PLATFORMS", "AGENT_FAMILIES", "PLATFORM_EFFORTS"):
            self.assertFalse(hasattr(core, name), name)
        self.assertFalse(hasattr(sessions, "PLATFORMS") or hasattr(sessions, "PROVIDERS"))
        self.assertEqual(agents.check(module(DESCRIBE={**GOOD, "platform": "pi-agent"})), [])
        self.assertIn("must be the id or one of the aliases", " ".join(agents.check(module(DESCRIBE={**GOOD, "platform": "x"}))))
        ladder = core.parse_ladder(core.DEFAULT_SETTINGS["model_ladder"])
        self.assertEqual(agents.get("claude").describe["models"], ladder["claude"])
        self.assertEqual(agents.get("codex").describe["models"], ladder["openai"])
        ids = core.platform_spec("codex")["options"]["model_ids"]["default"]
        self.assertEqual(agents.get("codex").describe["model_ids"], dict(p.split("=") for p in ids.replace(" ", "").split(",")))

    def test_names_are_found_in_any_case_and_an_unknown_name_says_what_is_registered(self):
        self.assertIs(agents.find("Claude-Code"), agents.get("claude"))
        self.assertIsNone(agents.find(""))
        with self.assertRaisesRegex(RiverError, r"agent 'nosuch' is not registered.*claude, codex.*agents/<id>\.py"):
            agents.get("nosuch")

    def test_check_names_each_problem(self):
        self.assertEqual(agents.check(module(DESCRIBE=GOOD), "pi"), [])
        self.assertEqual(agents.check(module(), "x"), ["it has no DESCRIBE (the describe part is required)"])
        got = agents.check(module(DESCRIBE={**GOOD, "id": "pie", "family": "llama", "colour": "red"}), "pi")
        self.assertIn("DESCRIBE['id'] is 'pie'; it must be the file name, 'pi'", got)
        self.assertTrue(any("family" in p and "llama" in p for p in got))
        self.assertTrue(any("unknown key 'colour'" in p for p in got))
        self.assertIn("DESCRIBE has no 'exe'", agents.check(module(DESCRIBE={"id": "pi", "label": "Pi", "family": "multi"}), "pi"))
        bad = module(DESCRIBE=GOOD, SETTINGS=[{"key": "x", "kind": "slider", "read": "("}],
                     EFFORT={"map": {"huge": "max"}}, STATE={"state": "hook"}, DETECT={"idle": "> $", "working": "[",
                                                                                         "blocked": ["ok"]},
                     parse_chat=lambda lines: ([], None), LAUNCH={"teleport": True}, resume_command="no")
        got = "\n".join(agents.check(bad, "pi"))
        for want in ("'slider'", "SETTINGS[0]['read'] is no valid regex", "'huge', which is no MaximizePM effort",
                     "'hook', but the module has no install_hooks", "DETECT['working'] is no valid regex",
                     "chat_path and parse_chat together", "unknown capability 'teleport'", "resume_command is no function"):
            self.assertIn(want, got)

    def test_a_full_adapter_passes_and_its_functions_go_through_the_adapter(self):
        m = module(
            DESCRIBE=GOOD,
            SETTINGS=[{"key": "mode", "label": "Mode", "kind": "choice", "choices": ["a", "b"], "default": "",
                       "text": "", "flag": ["--mode", "{value}"], "live": None, "read": r"--mode (\S+)"}],
            EFFORT={"levels": ["low", "high"], "map": {"medium": "low", "xhigh": "high", "max": "high"},
                    "flag": ["--thinking", "{value}"], "live": None, "read": None},
            LAUNCH={"session_name": True, "fork": False, "background": True},
            build_command=lambda name, folder, model, effort, settings, prompt: ["pi", "--model", model, prompt],
            IDENTITY={"source": "env", "env": "PI_SESSION", "log_glob": None, "pane": None, "url": None,
                      "inside_env": ["PI_SESSION"]},
            CHAT={"compressed": False, "file_tools": ["write"]},
            chat_path=lambda ref, folder: f"/logs/{ref}.jsonl",
            parse_chat=lambda lines: ([{"at": None, "role": "assistant", "text": x} for x in lines], "idle"),
            STATE={"state": "detect", "session_id": "identity"},
            DETECT={"process": "pi", "idle": r"^> $", "working": r"esc to interrupt", "blocked": [r"Allow\?"],
                    "tell_instead": "No, and tell Pi what to do", "image_paste": "[image {n}]"},
            INPUT={"native": None},
            resume_command=lambda ref, model, effort, settings, name=None: ["pi", "--resume", ref],
            COMMANDS=[{"name": "compact", "description": "compact the chat", "hint": ""}],
            read_commands=lambda folder: [{"name": "mine", "description": "", "hint": "<x>"}],
            SETUP={"instructions": "AGENTS.md", "skills_dir": None, "voice": None, "open_link": None},
        )
        self.assertEqual(agents.check(m, "pi"), [])
        a = agents.Adapter(m, "test")
        self.assertEqual(a.missing(), [])
        self.assertTrue(all(a.supports(p) for p in agents.PARTS))
        self.assertEqual(a.build_command("w", "/f", "anthropic/claude-opus", None, {}, "go"),
                         ["pi", "--model", "anthropic/claude-opus", "go"])
        self.assertEqual(a.resume_command("s1"), ["pi", "--resume", "s1"])
        self.assertEqual(a.parse_chat(["hi"]), ([{"at": None, "role": "assistant", "text": "hi"}], "idle"))
        self.assertEqual([c["name"] for c in a.read_commands("/f")], ["compact", "mine"])
        self.assertEqual((a.effort_name("medium"), a.effort_name("low"), a.effort_name(None)), ("low", "low", None))
        self.assertEqual(a.config_key("mode"), "pi_mode")
        self.assertTrue(a.inside({"PI_SESSION": "x"}))
        self.assertIsNone(agents.Adapter(module(DESCRIBE=GOOD), "t").effort_name("high"))  # no effort: none


class Overrides(Env):
    def test_a_user_module_adds_an_agent(self):
        self.user_module("toy", """
            DESCRIBE = {"id": "toy", "label": "Toy", "exe": "toy", "aliases": ["toy-agent"], "family": "multi"}
            IDENTITY = {"source": "env", "inside_env": ["TOY_SESSION"]}
        """)
        a = agents.get("toy-agent")
        self.assertEqual((a.id, a.source), ("toy", str(self.dir / "toy.py")))
        self.assertEqual(agents.ids(), list(agents.BUILTIN) + ["toy"])
        self.assertIs(agents.from_env({"TOY_SESSION": "1", "CLAUDECODE": "1"}), a)
        self.assertIs(agents.from_env({"MAXPM_AGENT_TYPE": "codex", "TOY_SESSION": "1"}), agents.get("codex"))
        self.assertIsNone(agents.from_env({}))

    def test_a_user_module_replaces_the_built_in_one_of_its_id(self):
        self.user_module("codex", """
            DESCRIBE = {"id": "codex", "label": "My Codex", "exe": "/opt/codex", "family": "openai"}
            def build_command(name, folder, model, effort, settings, prompt):
                return ["/opt/codex", prompt]
        """)
        a = agents.get("codex")
        self.assertEqual((a.label, a.missing()), ("My Codex", []))
        self.assertEqual(a.build_command("w", "/f", "sol", None, {}, "go"), ["/opt/codex", "go"])
        self.assertEqual(agents.errors(), {})

    def test_a_broken_user_module_is_left_out_and_named_and_the_built_in_one_stays(self):
        self.user_module("claude", "DESCRIBE = {'id': 'claude', 'label': 'C', 'exe': 'c', 'family': 'claude'}\n1/0\n")
        self.user_module("nosuch", "DESCRIBE = {'id': 'nosuch', 'label': 'No such', 'exe': 'nosuch'}\n")
        self.user_module("Bad-Name", "DESCRIBE = {}\n")
        self.assertEqual(agents.get("claude").source, "builtin")
        err = agents.errors()
        self.assertIn("ZeroDivisionError", err["claude"])
        self.assertIn("DESCRIBE has no 'family'", err["nosuch"])
        self.assertIn("must be an id", err["Bad-Name"])
        with self.assertRaisesRegex(RiverError, r"agent 'nosuch' is not registered \(.*nosuch\.py: DESCRIBE has no 'family'"):
            agents.get("nosuch")

    def test_two_agents_with_one_alias_are_named(self):
        self.user_module("cc", "DESCRIBE = {'id': 'cc', 'label': 'CC', 'exe': 'cc', 'family': 'claude', 'aliases': ['claude-code']}\n")
        self.assertIn("also agent claude's", agents.errors()["cc"])

    def test_the_registry_reads_a_changed_file_again(self):
        self.user_module("toy", "DESCRIBE = {'id': 'toy', 'label': 'Toy', 'exe': 'toy', 'family': 'multi'}\n")
        self.assertEqual(agents.get("toy").label, "Toy")
        f = self.dir / "toy.py"
        f.write_text("DESCRIBE = {'id': 'toy', 'label': 'Toy 2', 'exe': 'toy', 'family': 'multi'}\n")
        os.utime(f, ns=(1, 1))
        self.assertEqual(agents.get("toy").label, "Toy 2")
        f.unlink()
        self.assertIsNone(agents.find("toy"))


def run(*words, env=None):
    out = io.StringIO()
    with mock.patch.dict(os.environ, env or {}), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        try:
            code = cli.run(list(words))
        except SystemExit as e:
            code = e.code
        except RiverError:
            code = 2
    return code, out.getvalue()


class Report(Env):
    def setUp(self):
        super().setUp()
        core.register(self.c, "w1")

    def row(self):
        return dict(self.c.execute("SELECT * FROM agents WHERE name='w1'").fetchone())

    def test_each_state_is_stored_where_the_sessions_tab_reads_it(self):
        core.report(self.c, "w1", "working")
        a = self.row()
        self.assertEqual((a["activity_event"], a["activity_prompt"]), ("report:working", None))
        self.assertEqual(sessions.state(self.c, a, None)[0], "working")
        core.report(self.c, "w1", "blocked", message="Allow rm -rf build?")
        a = self.row()
        self.assertEqual(json.loads(a["activity_prompt"]), {"message": "Allow rm -rf build?"})
        self.assertEqual(sessions.state(self.c, a, None)[0], "prompt")
        core.report(self.c, "w1", "idle")
        a = self.row()
        self.assertIsNone(a["activity_prompt"])
        self.assertEqual(sessions.state(self.c, a, None), ("idle", "idle: at its prompt"))

    def test_a_late_report_changes_nothing(self):
        self.assertTrue(core.report(self.c, "w1", "working", seq=5, session_id="s1")["stored"])
        late = core.report(self.c, "w1", "idle", seq=4)
        self.assertFalse(late["stored"])
        self.assertIn("seq 4 is not above 5", late["why"])
        self.assertFalse(core.report(self.c, "w1", "idle", seq=5, session_id="s1")["stored"])
        self.assertEqual(self.row()["activity_event"], "report:working")
        self.assertTrue(core.report(self.c, "w1", "idle")["stored"])  # no seq: always stored
        self.assertTrue(core.report(self.c, "w1", "blocked", seq=1, session_id="s2")["stored"])  # a new session counts anew
        self.assertEqual((self.row()["report_seq"], self.row()["report_session"]), (1, "s2"))

    def test_the_session_id_and_the_resume_command_are_recorded(self):
        core.report(self.c, "w1", "idle", session_id="abc", resume=["pi", "--resume", "abc"])
        self.assertEqual(json.loads(self.row()["resume_argv"]), ["pi", "--resume", "abc"])
        self.assertEqual(core.session_owner(self.c, "abc"), "w1")

    def test_report_refuses_what_is_no_agent_or_no_state(self):
        with self.assertRaisesRegex(RiverError, "--state is one of working, idle, blocked"):
            core.report(self.c, "w1", "sleeping")
        with self.assertRaisesRegex(RiverError, "needs the agent"):
            core.report(self.c, None, "idle")
        core.register(self.c, "mark", human=True)
        with self.assertRaisesRegex(RiverError, "mark is no registered agent"):
            core.report(self.c, "mark", "idle")

    def test_release_with_no_item_makes_the_agent_gone_until_its_next_command(self):
        self.assertEqual(core.agent_status(self.c, "w1")["state"], "active")
        core.report_release(self.c, "w1")
        self.assertEqual(core.agent_status(self.c, "w1")["state"], "gone")
        core.activity(self.c, "w1")  # any later command of the session: it runs again
        self.assertEqual(core.agent_status(self.c, "w1")["state"], "active")

    def test_a_report_started_before_the_release_does_not_undo_it(self):
        # #5231: a plugin starts each maxpm command and does not wait, so its last report (seq: a time in ms) can reach
        # maxpm after its release. 1_700_000_000_000 ms is 2023-11-14T22:13:20Z.
        t_ms = 1_700_000_000_000
        core.report(self.c, "w1", "working", session_id="s1", seq=t_ms)
        with mock.patch.object(core, "clock_ns", return_value=(t_ms + 500) * 10 ** 6):
            core.report_release(self.c, "w1")
        late = core.report(self.c, "w1", "idle", session_id="s1", seq=t_ms + 200)  # started before the release
        self.assertFalse(late["stored"])
        self.assertEqual(core.agent_status(self.c, "w1")["state"], "gone")
        again = core.report(self.c, "w1", "working", session_id="s1", seq=t_ms + 900)  # started after it: a resume
        self.assertTrue(again["stored"])
        self.assertNotEqual(core.agent_status(self.c, "w1")["state"], "gone")

    def test_the_release_seq_keeps_the_unit_of_the_agent_s_seq(self):
        ns = 1_700_000_000_123_456_789
        with mock.patch.object(core, "clock_ns", return_value=ns):
            self.assertEqual(core._release_seq(1_700_000_000_000), 1_700_000_000_123)  # ms (Pi, OpenCode)
            self.assertEqual(core._release_seq(1_700_000_000_000_000), 1_700_000_000_123_456)  # µs
            self.assertEqual(core._release_seq(1_700_000_000_000_000_000), ns)  # ns (Grok)
            self.assertEqual(core._release_seq(1_800_000_000_000), 1_800_000_000_000)  # a later seq stays
            self.assertEqual(core._release_seq(7), 7)  # a count: no time
            self.assertIsNone(core._release_seq(None))
        core.report(self.c, "w1", "working", session_id="s1", seq=3)
        core.report_release(self.c, "w1")
        self.assertTrue(core.report(self.c, "w1", "idle", session_id="s1", seq=4)["stored"])

    def test_the_cli_takes_report_and_release(self):
        code, out = run("--as", "w1", "report", "--state", "blocked", "--message", "trust this folder?", "--seq", "3",
                        "--session-id", "s9", "--", "claude", "--resume", "s9")
        self.assertEqual((code, out.strip()), (0, "w1: blocked"))
        self.assertEqual(json.loads(self.row()["resume_argv"]), ["claude", "--resume", "s9"])
        code, out = run("--as", "w1", "report", "--state", "idle", "--seq", "2")
        self.assertIn("not stored", out)
        code, _ = run("--as", "w1", "report", "--state", "idle", "stray")
        self.assertEqual(code, 2)
        code, out = run("--as", "w1", "release")
        self.assertEqual((code, out.strip()), (0, "w1: its session ended (gone)"))
        self.assertEqual(self.row()["activity_event"], core.REPORT_GONE)
        code, _ = run("--as", "w1", "release", "--note", "x")
        self.assertEqual(code, 2)

    def test_release_of_an_item_works_as_before(self):
        core.project_add(self.c, "p")
        it = core.item_add(self.c, "p", "a task")["id"]
        core.claim(self.c, it, "w1")
        code, _ = run("--as", "w1", "release", str(it), "--note", "later")
        self.assertEqual(code, 0)
        self.assertEqual(self.c.execute("SELECT status FROM items WHERE id=?", (it,)).fetchone()["status"], "open")
        self.assertNotEqual(self.row()["activity_event"], core.REPORT_GONE)


class StartPaths(Env):
    """#5181: launch, resume, profile and identity go through the adapters, with no change of behaviour."""

    def test_the_agent_type_comes_from_each_adapters_identity(self):
        self.assertEqual(core.agent_type_from_env({"CLAUDECODE": "1"}), "claude-code")
        self.assertEqual(core.agent_type_from_env({"CLAUDECODE": "1", "CODEX_THREAD_ID": "t"}), "codex")
        self.assertEqual(core.agent_type_from_env({"CODEX_SANDBOX": "seatbelt"}), "codex")
        self.assertEqual(core.agent_type_from_env({"MAXPM_AGENT_TYPE": "claude", "CODEX_THREAD_ID": "t"}), "claude-code")
        self.assertEqual(core.agent_type_from_env({"MAXPM_AGENT_TYPE": "nonsense", "CLAUDECODE": "1"}), "claude-code")
        self.assertIsNone(core.agent_type_from_env({}))

    def test_a_user_module_changes_what_core_builds(self):
        before = core.build_command("codex", {"prompt": "go"}, "gpt-6-luna")
        self.assertTrue(before.startswith("codex -m gpt-6-luna "), before)
        self.user_module("codex", """
            from river.agents import Raw
            DESCRIBE = {"id": "codex", "label": "Codex", "exe": "codex", "family": "openai"}
            LAUNCH = {"resume": True}
            def build_command(name, folder, model, effort, settings, prompt):
                return ["/opt/codex", "-m", model, Raw("--x 'a b'"), prompt]
            def resume_command(ref, model, effort, settings, name=None):
                return ["/opt/codex", "resume", ref, settings["prompt"]]
        """)
        self.assertEqual(core.build_command("codex", {"prompt": "run go now"}, "gpt-6-luna"),
                         "/opt/codex -m gpt-6-luna --x 'a b' 'run go now'")

    def test_profile_from_command_reads_through_each_adapter(self):
        self.assertEqual(core.profile_from_command("claude --model {model} --permission-mode plan go --remote-control"),
                         ("claude-code", {"remote_control": "on", "permission_mode": "plan", "prompt": "go"}))
        self.assertEqual(core.profile_from_command("codex -m {model} -s read-only 'run go'"),
                         ("codex", {"sandbox": "read-only", "prompt": "run go"}))
        self.assertIsNone(core.profile_from_command("claude --model opus go"))  # a fixed model stays custom
        self.assertIsNone(core.profile_from_command("gemini go"))

    def test_effort_flags_and_the_effort_read_back_are_the_adapters(self):
        cl, cx = agents.get("claude"), agents.get("codex")
        self.assertEqual(cl.effort["live"], "/effort {value}")
        self.assertIsNone(cx.effort["live"])
        self.assertIn("--effort high", core.build_command("claude-code", {"prompt": "go"}, "opus", "high"))
        self.assertIn("-c model_reasoning_effort=xhigh", core.build_command("codex", {"prompt": "go"}, "sol", "xhigh"))
        # A custom command's family and effort levels come from the agent whose program it runs (agents.by_exe).
        self.assertIs(agents.by_exe("/usr/local/bin/Claude"), cl)
        self.assertIs(agents.by_exe("codex"), cx)
        self.assertIsNone(agents.by_exe("nope"))
        import re
        self.assertEqual(re.search(cx.effort["read"], "codex -c model_reasoning_effort=low go").group(1), "low")
        self.assertEqual(re.search(cl.effort["read"], "claude --effort max go").group(1), "max")

    def test_only_an_agent_that_forks_gets_a_fork_base_and_only_one_with_background_commands_waits_there(self):
        item = {"goals": ["g"], "no_goal_context": False}
        self.assertIsNone(core.fork_base(self.c, item, self.tmp.name, "opus", "codex"))
        self.assertIsNone(core.fork_base(self.c, item, self.tmp.name, "opus", None))
        self.assertTrue(agents.get("claude").launch["background"])
        self.assertFalse(agents.get("codex").launch.get("background"))

    def test_the_web_link_of_an_agent_with_no_session_link_comes_from_its_identity(self):
        core.register(self.c, "cx")
        self.c.execute("UPDATE agents SET agent_type='codex' WHERE name='cx'")
        self.assertEqual(core.agent_web_url(self.c, "cx"), core.CODEX_WEB)
        self.c.execute("UPDATE agents SET agent_type='claude-code' WHERE name='cx'")
        self.assertIsNone(core.agent_web_url(self.c, "cx"))



# ---------------------------------------------------------------------------------------------------------------
# State through maxpm report and DETECT (#5183)
# ---------------------------------------------------------------------------------------------------------------

class HookState(Env):
    """Claude Code's hooks run maxpm report --hook claude; the adapter's read_hook says what each event means."""

    def setUp(self):
        super().setUp()
        core.register(self.c, "w1")

    def test_each_claude_code_event_is_a_hook_report(self):
        a = agents.get("claude")
        self.assertEqual(a.state["state"], "hook")
        self.assertIsNone(a.read_hook({"hook_event_name": "SessionStart"}))
        got = {ev: a.read_hook({"hook_event_name": ev, "session_id": "s1", **more}) for ev, more in [
            ("PostToolUse", {}), ("UserPromptSubmit", {"prompt": "go"}),
            ("PermissionRequest", {"tool_name": "Bash", "tool_input": {"command": "rm -rf build", "timeout": 5}}),
            ("Notification", {"notification_type": "permission_prompt", "message": "Claude needs your permission"}),
            ("Notification", {"notification_type": "idle_prompt"}),
            ("Stop", {"background_tasks": [{"type": "local_bash", "status": "running", "command": "make check"}],
                      "stop_hook_active": True})]}
        self.assertEqual({k: v["state"] for k, v in got.items()},
                         {"PostToolUse": "working", "UserPromptSubmit": "working", "PermissionRequest": "blocked",
                          "Notification": None, "Stop": "idle"})
        self.assertEqual(got["UserPromptSubmit"]["prompt"], "go")
        self.assertEqual(got["PermissionRequest"]["call"], {"tool": "Bash", "input": {"command": "rm -rf build"}})
        self.assertEqual((got["Stop"]["waits"], got["Stop"]["again"]), (["local_bash: make check"], True))
        self.assertIsNone(a.read_hook({"hook_event_name": "Stop"})["waits"])  # an older Claude Code: not said
        shows = a.read_hook({"hook_event_name": "Notification", "notification_type": "permission_prompt"})
        self.assertEqual((shows["state"], shows["shows"], shows["event"]), ("blocked", True, "Notification:permission_prompt"))
        self.assertTrue(all(set(r) == set(agents.HOOK_REPORT_KEYS) for r in got.values()))

    def test_the_hook_command_records_the_report_and_never_fails(self):
        hook = core.activity_hook_settings()["hooks"]["Stop"][0]["hooks"][0]["command"]
        self.assertTrue(hook.endswith(" report --hook claude"), hook)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"MAXPM_AGENT": "w1"}), mock.patch.object(core, "agent_process", lambda: (None, None)):
            for text in ('{"hook_event_name": "UserPromptSubmit", "prompt": "hi"}', "not json", ""):
                with mock.patch("sys.stdin", io.StringIO(text)), contextlib.redirect_stdout(out):
                    self.assertEqual(cli.run(["report", "--hook", "claude"]), 0)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(self.c.execute("SELECT activity_event FROM agents WHERE name='w1'").fetchone()[0], "report:working")
        # Another event that says no state keeps the state and moves only the time.
        core.hook_report(self.c, "claude", {"hook_event_name": "Notification", "notification_type": "auth_success"},
                         {"MAXPM_AGENT": "w1"})
        self.assertEqual(self.c.execute("SELECT activity_event FROM agents WHERE name='w1'").fetchone()[0], "report:working")
        with self.assertRaisesRegex(RiverError, "--state working"):
            cli.run(["--as", "w1", "report"])

    def test_the_defaults_are_the_adapters_data(self):
        """The prompt_pattern default reads the same screens as the built-in adapters' blocked words, and the
        native_message default is Codex's INPUT; B5 (#5185) removes the copies in the settings."""
        words = {w for m in ("claude", "codex") for w in agents.get(m).detect["blocked"]}
        self.assertEqual(set(core.DEFAULT_SETTINGS["prompt_pattern"].split("|")) - {"Do you (want to", "trust)"},
                         words - {"Do you (want to|trust)"})
        native = " ".join(agents.get("codex").input["native"]).replace("{text}", "{message}")
        self.assertTrue(core.DEFAULT_SETTINGS["native_message"].endswith(": " + native))
        self.assertEqual(agents.get("claude").detect["image_paste"].format(n=1), "[Image #1]")


class PaneState(Env):
    """The generic pane watcher reads each pane with its agent's DETECT; for an agent whose STATE is detect (Codex),
    it reports the state as maxpm report does (#5183)."""

    def setUp(self):
        super().setUp()
        from river import server
        from tests.test_server import FakeTmux
        self.server = server
        platform, core.PLATFORM = core.PLATFORM, "linux"
        self.addCleanup(setattr, core, "PLATFORM", platform)
        server.TMUX_RUNNER = FakeTmux()
        self.addCleanup(setattr, server, "TMUX_RUNNER", None)
        self.addCleanup(server.PROMPTS.clear)
        in_tmux = os.environ.pop("TMUX", None)
        if in_tmux is not None:
            self.addCleanup(os.environ.__setitem__, "TMUX", in_tmux)
        core.project_add(self.c, "shop", path=self.tmp.name)

    def start(self, agent_type):
        core.item_add(self.c, "shop", "work")
        name = self.server.launch_agent(self.c)["session_name"]
        core.register(self.c, name)
        core.set_agent_type(self.c, name, agent_type)
        return name, self.server.TMUX_RUNNER.panes[-1]

    def event(self, name):
        return self.c.execute("SELECT activity_event, activity_prompt FROM agents WHERE name=?", (name,)).fetchone()

    def test_a_codex_pane_reports_working_idle_and_blocked(self):
        name, pane = self.start("codex")
        pane["running"] = "codex"
        pane["screen"] = "• Running the tests\n\n◦ Working (12s • esc to interrupt)\n\n› Ask Codex to do anything"
        self.assertEqual(self.server.watch_prompts(self.c)["reported"], {name: "working"})
        self.assertNotIn("reported", self.server.watch_prompts(self.c))  # no change: nothing written
        pane["screen"] = "• Done.\n\n› Ask Codex to do anything\n\n  100% context left"
        self.assertEqual(self.server.watch_prompts(self.c)["reported"], {name: "idle"})
        pane["screen"] = ("Would you like to run the following command?\n\n  $ rm -rf build\n\n› 1. Yes, proceed\n"
                          "  2. No, and tell Codex what to do differently\n\n  Press enter to confirm or esc to cancel")
        got = self.server.watch_prompts(self.c)
        self.assertEqual((got["reported"], got["waiting"]),
                         ({name: "blocked"}, {name: "Would you like to run the following command?"}))
        r = self.event(name)
        self.assertEqual((r["activity_event"], json.loads(r["activity_prompt"])),
                         ("report:blocked", {"message": "Would you like to run the following command?"}))
        self.assertEqual(sessions.state(self.c, dict(self.c.execute("SELECT * FROM agents WHERE name=?", (name,))
                                                     .fetchone()), None)[0], "prompt")

    def test_a_claude_code_pane_gives_its_state_by_its_hooks_only(self):
        name, pane = self.start("claude-code")
        pane["screen"] = "⏺ Read 3 files\n✻ Pondering… (12s · esc to interrupt)"
        self.assertNotIn("reported", self.server.watch_prompts(self.c))
        self.assertIsNone(self.event(name)["activity_event"])

    def test_the_tell_instead_option_comes_from_the_adapter(self):
        name, pane = self.start("codex")
        pane["screen"] = ("Would you like to run the following command?\n\n  $ rm -rf build\n\n› 1. Yes, proceed\n"
                          "  2. No, and tell Codex what to do differently\n\n  Press enter to confirm or esc to cancel")
        p = self.server.pane_prompts(self.c, {name: pane["id"]})[name]
        with mock.patch.object(self.server, "PROMPT_PAUSE", 0):
            got = self.server.prompt_answer(self.c, name, p["sig"], other="run make clean instead", who="mark")
        self.assertEqual(got["picked"], "2. No, and tell Codex what to do differently")


class ImportOrder(unittest.TestCase):
    def test_each_module_imports_first_in_a_new_interpreter(self):
        """#5207: core imports the built-in adapters while it loads, and they import Raw from river.agents. A user
        adapter module or a script that imports river.agents (or an adapter) before core must work."""
        import subprocess
        import sys
        root = str(Path(__file__).resolve().parent.parent)
        names = ["river.agents"] + [f"river.agents.{p.stem}" for p in sorted((Path(root) / "river" / "agents").glob("*.py"))
                                    if p.stem != "__init__"] + ["river.core", "river.cli"]
        for name in names:
            r = subprocess.run([sys.executable, "-c", f"import {name}; import river.agents as a; a.Raw; a.load()"],
                               cwd=root, capture_output=True, text=True, timeout=60,
                               env={**os.environ, "MAXPM_DB": os.path.join(tempfile.gettempdir(), "maxpm-5207-none.db")})
            self.assertEqual(r.returncode, 0, f"import {name} first: {r.stderr.strip().splitlines()[-1:]}")


class FirstPrompt(Env):
    """#5221: an agent with FIRST_PROMPT 'paste' (Aider) gets its first prompt pasted into its tmux pane by maxpm serve,
    once its idle prompt shows there."""

    def setUp(self):
        super().setUp()
        from river import server
        from tests.test_server import FakeTmux
        self.server = server
        self.platform, core.PLATFORM = core.PLATFORM, "linux"
        self.tmux = server.TMUX_RUNNER = FakeTmux()
        self.addCleanup(self._reset)
        core.project_add(self.c, "shop", path=self.tmp.name)

    def _reset(self):
        core.PLATFORM, self.server.TMUX_RUNNER = self.platform, None

    def start(self, agent):
        t = {**core._launch_agent_cmd(self.c, None, agent, "opus", None, None, "w1"), "path": self.tmp.name,
             "project": "shop", "session_title": "w1"}
        self.server._open_terminal(t, {"MAXPM_AGENT": "w1", **t["env"]})
        return t, next(p for p in self.tmux.panes if p["id"] == t["tmux_pane"])

    def pastes(self):
        return [c for c in self.tmux.calls if c[0] in ("set-buffer", "paste-buffer")]

    def test_the_contract_names_first_prompt(self):
        self.assertEqual((agents.get("aider").first_prompt, agents.get("claude").first_prompt), ("paste", "argv"))
        self.assertIn("FIRST_PROMPT is 'typed', not one of argv, paste",
                      agents.check(module(DESCRIBE=GOOD, FIRST_PROMPT="typed"), "pi"))
        self.assertIn("FIRST_PROMPT is 'paste', but the module has no DETECT: maxpm serve cannot see its idle prompt",
                      agents.check(module(DESCRIBE=GOOD, FIRST_PROMPT="paste"), "pi"))

    def test_the_prompt_is_pasted_once_when_the_idle_prompt_shows(self):
        t, pane = self.start("Aider")
        self.assertNotIn("run maxpm go", t["command"])  # Aider takes no prompt on its command line
        self.assertEqual(t["first_prompt"], "run maxpm go in this folder and follow the briefing")
        pane["running"], pane["screen"] = "Python", "Aider v0.86.2\nMain model: opus\n░█  Waiting for opus"
        self.assertEqual(self.server.paste_first_prompts(self.c), [])  # no idle prompt yet: it waits
        self.assertEqual(self.pastes(), [])
        pane["screen"] = "Aider v0.86.2\nMain model: opus\n\n>"
        self.assertEqual(self.server.paste_first_prompts(self.c), [pane["id"]])
        self.assertEqual(self.pastes()[0], ["set-buffer", "-b", "maxpm-send", "--", t["first_prompt"]])
        self.assertEqual(pane["keys"][-1], "Enter")
        self.assertEqual(self.server.paste_first_prompts(self.c), [])  # once
        self.assertEqual(len(self.pastes()), 2)

    def test_an_agent_that_takes_its_prompt_on_the_command_line_holds_none(self):
        t, _ = self.start("Claude Code")
        self.assertIsNone(t["first_prompt"])
        self.assertIsNone(self.c.execute("SELECT 1 FROM pane_prompts").fetchone())

    def test_a_pane_that_ended_drops_its_prompt(self):
        _, pane = self.start("Aider")
        self.tmux.panes.remove(pane)
        self.assertEqual(self.server.paste_first_prompts(self.c), [])
        self.assertIsNone(self.c.execute("SELECT 1 FROM pane_prompts").fetchone())
        self.assertEqual(self.pastes(), [])


if __name__ == "__main__":
    unittest.main()


class ChatLog(Env):
    """#5182: the chat log readers of the Sessions tab, the Artifacts tab, the prompt cache and the usage measurement
    go through each adapter's chat log part, so an agent with its own log format needs no change in sessions.py."""

    TOY = """
        import json, os
        from datetime import datetime, timezone
        from pathlib import Path
        DESCRIBE = {"id": "toy", "label": "Toy", "exe": "toy", "family": "multi"}
        CHAT = {"compressed": False, "file_tools": [], "ref": "session", "cache_write": False}

        def chat_path(ref, folder, root=None):
            p = Path(os.environ["TOY_LOGS"]) / f"{ref}.log"
            return p if p.is_file() else None

        def parse_chat(lines):  # no parse_chat_line: the Sessions tab gives it one line at a time
            out, turn = [], None
            for line in lines:
                d = json.loads(line)
                out.append({"at": d["t"], "role": "user" if d["who"] == "me" else "assistant", "text": d["text"]})
                turn = "working" if d["who"] == "me" else "idle"
            return out, turn

        def chat_title(lines):
            return "Toy talk"

        def chat_texts(raw):
            d = json.loads(raw)
            return [(d["text"], d["t"], "named")]

        def chat_cache_line(raw, st):
            d = json.loads(raw)
            if d["who"] == "toy":
                st["last"] = {"start": d["t"], "at": d["t"], "context": 1200}

        def chat_requests(ref, root=None):
            if not chat_path(ref, None):
                return None
            u = {"input": 10, "output": 5, "cache_read": 100, "cache_write": 0, "write_1h": 0, "write_5m": 0}
            return {"r1": (datetime(2026, 11, 11, tzinfo=timezone.utc), u)}, False
    """

    def test_the_built_in_chat_parts(self):
        claude, codex = agents.get("claude"), agents.get("codex")
        self.assertEqual((claude.chat_ref(), codex.chat_ref()), ("session", "address"))
        self.assertEqual(claude.chat["file_tools"], ["Write", "Edit", "MultiEdit", "NotebookEdit"])
        self.assertTrue(claude.chat["cache_write"])
        self.assertFalse(codex.chat["cache_write"])
        self.assertEqual(codex.chat_chain("/x/a.jsonl", ["s1"]), ["/x/a.jsonl"])  # no chain: the one file
        # Pi's log names its parent: its chat_chain takes the refs and needs none; its chat_usage sums a file.
        self.assertEqual(agents.get("pi").chat_chain("/no/such.jsonl", ["s1"]), [Path("/no/such.jsonl")])
        self.assertIsNone(agents.get("pi").chat_requests("s1"))
        self.assertIsNone(codex.chat_title(['{"type":"custom-title","customTitle":"t"}']))
        self.assertEqual(claude.chat_title(['{"type":"custom-title","customTitle":"t"}']), "t")
        bad = agents.check(module(DESCRIBE=GOOD, CHAT={"ref": "file", "cache_write": "yes"}), "pi")
        self.assertTrue(any("CHAT['ref'] is 'file'" in p for p in bad), bad)
        self.assertTrue(any("CHAT['cache_write'] is a str" in p for p in bad), bad)

    def test_an_agent_with_its_own_log_format_shows_in_every_reader(self):
        logs = Path(self.tmp.name) / "toy-logs"
        logs.mkdir()
        env = mock.patch.dict(os.environ, {"TOY_LOGS": str(logs)})
        env.start()
        self.addCleanup(env.stop)
        roots = mock.patch.object(core, "transcripts_root", lambda: Path(self.tmp.name) / "no-claude")
        roots.start()
        self.addCleanup(roots.stop)
        self.user_module("toy", self.TOY)
        core.register(self.c, "w")
        with core.tx(self.c):
            self.c.execute("INSERT INTO agent_sessions (agent, session_id, first_seen) VALUES ('w', 'p1', ?)",
                           (core.iso(core.now()),))
        lines = [{"t": "2026-11-11T10:00:00Z", "who": "me", "text": "draw /tmp/plan.png"},
                 {"t": "2026-11-11T10:00:05Z", "who": "toy", "text": "Done: /tmp/plan.png"}]
        (logs / "p1.log").write_text("".join(json.dumps(d) + "\n" for d in lines))
        self.assertEqual(sessions.transcript(self.c, "w")[0], "toy")
        v = sessions.view(self.c, "w")
        self.assertEqual([(e["role"], e["text"]) for e in v["entries"]],
                         [("user", "draw /tmp/plan.png"), ("assistant", "Done: /tmp/plan.png")])
        self.assertEqual(v["turn"], "idle")
        self.assertEqual(sessions.work_name(self.c, core.agent_status(self.c, "w")), "Toy talk")
        self.assertEqual([a["target"] for a in sessions.artifacts(self.c, agent="w")], ["/tmp/plan.png"])
        cache = sessions.cache_state(self.c, "w", at=core.parse_iso("2026-11-11T10:01:00Z"))
        self.assertEqual((cache["context"], cache["ttl_from"]), (1200, "setting"))
        reqs, fork = core._session_requests("p1")
        self.assertEqual((list(reqs), fork), (["r1"], False))


GEM = """
    DESCRIBE = {"id": "gem", "label": "Gem", "exe": "gem", "family": "multi", "model_ids": {"opus": "g/opus"}}
    SETTINGS = [{"key": "sandbox", "label": "Sandbox", "kind": "toggle", "default": "on", "text": "run in a box",
                 "flag": ["--sandbox"], "live": None, "read": None}]
    LAUNCH = {}
    def build_command(name, folder, model, effort, settings, prompt):
        return ["gem"] + (["-m", model] if model else []) + (["--sandbox"] if settings.get("sandbox") == "on" else []) \\
            + [prompt or ""]
    def resume_command(ref, model, effort, settings, name=None):
        return ["gem", "--resume", ref, "-m", model, settings["prompt"]]
"""


class RoleChoice(Env):
    """#5168: agent_for.<role> at the item, project and global scopes, before default_agent; default_agent,
    agent_rules, --agent and every start path take any registered adapter; expert and crash resume go through the
    adapter's resume_command."""

    def setUp(self):
        super().setUp()
        self.user_module("gem", GEM)
        self.folder = os.path.join(self.tmp.name, "shop")
        os.mkdir(self.folder)
        core.project_add(self.c, "shop", path=self.folder)
        core.project_add(self.c, "docs", path=self.folder)
        self.pid = core._project(self.c, "shop")["id"]

    def test_any_registered_adapter_is_an_agent_type(self):
        types = core.agent_types()  # the built-in adapters first, then the others by id
        self.assertEqual(types[:2], ["claude-code", "codex"])
        self.assertIn("gem", types)
        x = core.item_add(self.c, "shop", "work", models={"agent": "gem"})["id"]
        self.assertEqual(core._item(self.c, x)["agent"], "gem")
        core.config_set(self.c, "default_agent", "gem")
        core.config_set(self.c, "agent_rules", "gem: *.css")
        self.assertEqual(core.parse_agent_rules("gem: *.css"), [("gem", ["*.css"])])
        with self.assertRaisesRegex(RiverError, "one of claude-code, codex, .*gem"):
            core.config_set(self.c, "agent_for.worker", "nope")
        with self.assertRaisesRegex(RiverError, "unknown setting"):
            core.config_set(self.c, "agent_for.janitor", "gem")
        # An adapter with no launch part cannot be started, so it is no agent type.
        self.user_module("half", "DESCRIBE = {'id': 'half', 'label': 'Half', 'exe': 'half', 'family': 'multi'}\n")
        self.assertNotIn("half", core.agent_types())
        with self.assertRaisesRegex(RiverError, "an agent type is one of"):
            core.item_add(self.c, "shop", "other", models={"agent": "half"})
        # A session that runs it declares it by hand.
        self.assertEqual(core.agent_type_from_env({"MAXPM_AGENT_TYPE": "gem", "CLAUDECODE": "1"}), "gem")

    def test_a_registered_adapter_starts_with_no_launch_agents_entry(self):
        self.assertEqual(core.launch_entries(self.c)[-1], ("Gem", "@gem"))
        self.assertEqual(core.launch_entries(self.c)[0][1], "@claude-code")  # the default start stays the first
        x = core.item_add(self.c, "shop", "work", models={"agent": "gem"})["id"]
        t = core.launch_target(self.c, item=x, model="opus")
        self.assertEqual((t["agent"], t["platform"], t["command"].split()[:4]), ("Gem", "gem", ["gem", "-m", "g/opus", "--sandbox"]))
        # A profile in launch_agents names it and its options, and the implicit entry goes.
        core.config_set(self.c, "launch_agents", "Claude Code=@claude-code; My Gem=@gem sandbox=off")
        self.assertNotIn(("Gem", "@gem"), core.launch_entries(self.c))
        t = core.launch_target(self.c, item=x, model="opus")
        self.assertEqual((t["agent"], "--sandbox" in t["command"]), ("My Gem", False))

    def test_agent_for_a_role_comes_before_default_agent_of_its_scope(self):
        w = core.item_add(self.c, "shop", "work")["id"]
        r = core.item_add(self.c, "shop", "review it")["id"]
        with core.tx(self.c):  # a release review comes from the review setting; its kind is what counts here
            self.c.execute("UPDATE items SET kind='review' WHERE id=?", (r,))
        core.config_set(self.c, "default_agent", "codex")
        core.config_set(self.c, "agent_for.reviewer", "gem")
        ann = core.annotate(self.c)
        self.assertEqual((ann[w]["agent"], ann[r]["agent"]), ("codex", "gem"))
        self.assertEqual(ann[r]["agent_from"], "global agent_for.reviewer")
        # The most specific scope wins: a project's default_agent before the global agent_for.reviewer.
        core.config_set(self.c, "default_agent", "claude-code", project="shop")
        self.assertEqual(core.annotate(self.c)[r]["agent"], "claude-code")
        # An item scope before both; the item's own --agent before every setting.
        core.config_set(self.c, "agent_for.reviewer", "gem", item=r)
        self.assertEqual(core.annotate(self.c)[r]["agent"], "gem")
        core.item_edit(self.c, r, models={"agent": "codex"})
        self.assertEqual(core.annotate(self.c)[r]["agent"], "codex")
        # The role an item has: worker, reviewer, deployer, expert.
        self.assertEqual([core.item_role(core._item(self.c, i)) for i in (w, r)], ["worker", "reviewer"])

    def test_role_agent_for_a_session_without_an_item(self):
        self.assertIsNone(core.role_agent(self.c, "manager"))  # nothing set: the first launch_agents entry
        core.config_set(self.c, "default_agent", "codex")
        self.assertEqual(core.role_agent(self.c, "manager"), "Codex")
        core.config_set(self.c, "agent_for.manager", "gem")
        self.assertEqual(core.role_agent(self.c, "manager", self.pid), "Gem")
        core.config_set(self.c, "default_agent", "claude-code", project="shop")
        self.assertEqual(core.role_agent(self.c, "manager", self.pid), "Claude Code")
        self.assertEqual(core.role_agent(self.c, "manager", core._project(self.c, "docs")["id"]), "Gem")
        with self.assertRaisesRegex(RiverError, "role 'janitor'"):
            core.role_agent(self.c, "janitor")
        # An expert for a work item: the expert's agent, not the worker's.
        x = core.item_add(self.c, "docs", "talk")["id"]
        core.config_set(self.c, "agent_for.expert", "gem")
        self.assertEqual(core.role_agent(self.c, "expert", item_id=x), "Gem")
        self.assertEqual(core.role_agent(self.c, "worker", item_id=x), "Codex")

    def test_the_manager_and_an_expert_start_the_agent_of_their_role(self):
        from river import server
        core.config_set(self.c, "agent_for.manager", "gem")
        sent = []
        t = server.start_manager(self.c, runner=sent.append, model="opus")
        self.assertEqual(t["agent"], "Gem")
        self.assertTrue(t["command"].startswith("gem -m g/opus --sandbox"), t["command"])
        core.config_set(self.c, "agent_for.expert", "codex")
        t = server.start_expert(self.c, "fab", "a long talk", "shop", model="sol", runner=sent.append)
        self.assertEqual(t["agent"], "Codex")
        self.assertIn("codex", sent[-1])
        # A focused start (Deploy now, Review and deploy, help, unblock): the agent of its role.
        core.config_set(self.c, "agent_for.deployer", "gem")
        t = server._open_focused(self.c, core._project(self.c, "shop"), "deploy:web", sent.append, None, "opus")
        self.assertEqual(t["agent"], "Gem")
        t = server._open_focused(self.c, core._project(self.c, "shop"), "review:web", sent.append, None, "opus")
        self.assertEqual(t["agent"], "Claude Code")

    def test_expert_resume_goes_through_the_adapter_it_ran_in(self):
        from river import server
        sent = []
        t = server.start_expert(self.c, "fab", "a long talk", "shop", model="opus", runner=sent.append)
        core.set_agent_type(self.c, "fab", "gem")
        with core.tx(self.c):
            self.c.execute("INSERT INTO agent_sessions(agent, session_id, first_seen) VALUES ('fab', 'sess-9', ?)",
                           (core.iso(core.now()),))
        core.expert_end(self.c, "fab", None, "pause")
        t = server.start_expert(self.c, "fab", resume=True, runner=sent.append)
        self.assertEqual(t.get("resumed"), "sess-9")
        self.assertTrue(t["command"].startswith("gem --resume sess-9 -m g/opus"), t["command"])
        # An agent that cannot resume starts fresh and reads its notes.
        self.user_module("gem", GEM.replace("def resume_command", "def _no_resume"))
        core.expert_end(self.c, "fab", None, "pause")
        t = server.start_expert(self.c, "fab", resume=True, runner=sent.append)
        self.assertIsNone(t.get("resumed"))


# ---------------------------------------------------------------------------------------------------------------
# Settings and effort from the adapter (#5169)
# ---------------------------------------------------------------------------------------------------------------

XYZ = '''
    DESCRIBE = {"id": "xyz", "label": "Xyz CLI", "exe": "xyz", "family": "multi", "prefix": "xyz_",
                "models": ["acme/small", "acme/large"], "model_live": "/model {value}", "model_read": r"--model (\\S+)"}
    SETTINGS = [
        {"key": "mode", "label": "Mode", "kind": "choice", "choices": ["fast", "careful"], "default": "",
         "text": "how it works (--mode)", "flag": ["--mode", "{value}"], "read": r"--mode (\\S+)", "session": True},
        {"key": "trust", "kind": "toggle", "default": "on", "text": "trust the folder", "flag": ["--trust"]},
        {"key": "args", "kind": "text", "default": "", "text": "more arguments"},
        {"key": "yolo", "label": "Yolo", "kind": "toggle", "default": "off", "flag": ["--yolo"], "config": False,
         "session": True},
    ]
    EFFORT = {"levels": ["minimal", "deep"], "map": {"low": "minimal", "high": "deep", "max": "deep"},
              "flag": ["--think", "{value}"], "live": None, "read": None, "label": "Thinking"}

    def build_command(name, folder, model, effort, settings, prompt):
        return ["xyz"] + (["--model", model] if model else []) + [prompt]
'''


class SettingsAndEffort(Env):
    def test_the_built_in_launch_platforms_are_the_adapters_settings(self):
        for platform, mid in (("claude-code", "claude"), ("codex", "codex")):
            a = agents.get(mid)
            self.assertEqual(list(core.platform_spec(platform)["options"]), list(a.options()))
            for key, spec in a.options().items():
                self.assertEqual(core.DEFAULT_SETTINGS[a.config_key(key)], spec["default"])
        self.assertEqual(core.setting(self.c, "claude_remote_control"), "on")
        self.assertNotIn("codex_approve_for_me", core.DEFAULT_SETTINGS)  # a flag of one session, no config key

    def test_the_settings_dialog_of_claude_code_and_codex_comes_from_the_adapter(self):
        cc = {s["key"]: s for s in agents.get("claude-code").session_specs()}
        self.assertEqual(list(cc), ["model", "effort", "remote_control", "permission_mode"])
        self.assertEqual((cc["model"]["live"], cc["model"]["cmd"]), ("/model {value}", r"--model (\S+)"))
        self.assertEqual((cc["effort"]["live"], cc["effort"]["restart"]), ("/effort {value}", "effort"))
        self.assertEqual(cc["effort"]["choices"], ["low", "medium", "high", "xhigh", "max"])
        self.assertEqual((cc["remote_control"]["restart"], cc["remote_control"]["default"], cc["remote_control"]["flag"]),
                         ("opt", "claude_remote_control", "--remote-control"))
        self.assertEqual(cc["permission_mode"]["note"], "Shift+Tab in its terminal also cycles it")
        cx = {s["key"]: s for s in agents.get("codex").session_specs()}
        self.assertEqual(sorted(cx), ["approval", "approve_for_me", "effort", "model", "no_daemon", "sandbox"])
        self.assertIsNone(cx["model"]["live"])
        self.assertEqual(cx["effort"]["label"], "Reasoning effort")
        self.assertEqual((cx["no_daemon"]["restart"], cx["no_daemon"]["flag"], cx["no_daemon"]["default"]),
                         ("flag", "--no-daemon", None))
        self.assertIn("untrusted", cx["approval"]["choices"])
        self.assertIs(sessions.setting_spec("codex", "sandbox")["restart"], "opt")
        with self.assertRaisesRegex(RiverError, "Codex has no setting remote_control"):
            sessions.setting_spec("codex", "remote_control")

    def test_a_new_agent_s_settings_are_config_keys_launch_options_and_setup_rows(self):
        self.user_module("xyz", XYZ)
        self.assertEqual(core.setting(self.c, "xyz_trust"), "on")
        core.config_set(self.c, "xyz_mode", "careful", actor="mark")
        self.assertEqual(core.setting(self.c, "xyz_mode"), "careful")
        with self.assertRaisesRegex(RiverError, "xyz mode is one of fast, careful"):
            core.config_set(self.c, "xyz_mode", "slow", actor="mark")
        with self.assertRaisesRegex(RiverError, "unknown setting 'xyz_yolo'"):
            core.config_set(self.c, "xyz_yolo", "on", actor="mark")  # config False: no config key
        self.assertEqual(core.config_list(self.c)["defaults"]["xyz_mode"], "")
        core.config_set(self.c, "launch_agents", "Claude Code=@claude-code; Xyz=@xyz trust=off", actor="mark")
        opt = next(o for o in core.launch_options(self.c) if o["label"] == "Xyz")
        self.assertEqual(opt["platform"], "xyz")
        self.assertEqual([m["name"] for m in opt["models"]], ["acme/small", "acme/large"])
        self.assertEqual(opt["efforts"], ["low", "high", "max"])  # the levels whose Xyz name is one of its levels
        self.assertEqual(agents.get("pi").effort_choices(), list(agents.MAXPM_EFFORTS))  # map: only max -> xhigh
        self.assertTrue(opt["takes_effort"])
        self.assertEqual([(o["name"], o["label"], o["value"], o["fixed"]) for o in opt["options"]],
                         [("mode", "Mode", "careful", False), ("trust", "Trust", "off", True)])
        prof = next(p for p in core.launch_profiles(self.c) if p["platform"] == "xyz")
        self.assertEqual((prof["label"], [o["name"] for o in prof["options"]]), ("Xyz CLI", ["mode", "trust", "args", "prompt"]))
        self.assertEqual(core.setting(self.c, "xyz_prompt"), core.FIRST_PROMPT)  # #5168: every agent has a first prompt
        self.assertEqual(core.profile_options(self.c, "xyz", {"trust": "off"}, chosen={"mode": "fast"}),
                         {"mode": "fast", "trust": "off", "args": "", "prompt": core.FIRST_PROMPT})

    def test_an_agent_with_no_effort_offers_none(self):
        self.user_module("plain", '''
            DESCRIBE = {"id": "plain", "label": "Plain", "exe": "plain", "family": "multi", "prefix": "plain_"}
            def build_command(name, folder, model, effort, settings, prompt):
                return ["plain", prompt]
        ''')
        core.config_set(self.c, "launch_agents", "Plain=@plain", actor="mark")
        opt = core.launch_options(self.c)[0]
        self.assertEqual((opt["efforts"], opt["takes_effort"], opt["options"]), ([], False, []))
        self.assertEqual([s["key"] for s in agents.get("plain").session_specs()], ["model"])

    def test_the_settings_dialog_of_a_new_agent_reads_its_command_line(self):
        self.user_module("xyz", XYZ)
        core.register(self.c, "x1")
        self.c.execute("UPDATE agents SET agent_type='xyz', pid_cmd='xyz --model acme/large --mode fast --yolo' "
                       "WHERE name='x1'")
        got = {s["key"]: s for s in sessions.session_settings(self.c, "x1")["settings"]}
        self.assertEqual(list(got), ["model", "effort", "mode", "yolo"])
        self.assertEqual((got["model"]["value"], got["model"]["live"]), ("acme/large", True))
        self.assertEqual((got["effort"]["label"], got["effort"]["choices"]), ("Thinking", ["low", "high", "max"]))
        self.assertEqual((got["mode"]["value"], got["mode"]["source"], got["mode"]["default_key"]),
                         ("fast", "command line", "xyz_mode"))
        self.assertEqual((got["yolo"]["value"], got["yolo"]["default_key"]), ("on", None))
        self.assertEqual(sessions.check_value(sessions.setting_spec("xyz", "mode"), "careful"), "careful")

    def test_check_names_a_setting_spec_that_cannot_work(self):
        bad = module(DESCRIBE={**GOOD, "id": "x"}, SETTINGS=[
            {"key": "a", "kind": "toggle", "default": "maybe"},
            {"key": "b", "kind": "choice", "choices": ["x"], "default": "y"},
            {"key": "e", "kind": "choice", "choices": ["", "x"], "default": ""},
            {"key": "c", "kind": "text", "config": False, "session": True},
            {"key": "d", "kind": "toggle", "flag": ["--d"], "config": False},
            {"key": "model", "kind": "text"},
            {"key": "a", "kind": "text"},
        ])
        got = " | ".join(agents.check(bad, "x"))
        for part in ("a toggle is on or off", "not one of its choices", "such a setting is a toggle with a flag",
                     "nothing shows it", "come from DESCRIBE and EFFORT", "the key 'a' twice",
                     "SETTINGS[2]['choices'] holds ''"):
            self.assertIn(part, got)


class SessionFromEnv(Env):
    """#5212: each maxpm command records the session id of an agent whose adapter takes it from an environment
    variable (IDENTITY source 'env') and finds its chat log by it (CHAT ref 'session'), as it does for Claude Code."""

    HERMES = """
        import os
        from pathlib import Path
        DESCRIBE = {"id": "hermes", "label": "Hermes", "exe": "hermes", "family": "multi"}
        IDENTITY = {"source": "env", "env": "HERMES_SESSION_ID", "inside_env": ["HERMES_SESSION_ID"]}
        CHAT = {"compressed": False, "file_tools": [], "ref": "session", "cache_write": False}

        def chat_path(ref, folder, root=None):
            p = Path(os.environ["HERMES_LOGS"]) / f"{ref}.jsonl"
            return p if p.is_file() else None

        def parse_chat(lines):
            return [], None
    """

    def test_the_id_of_each_agent_comes_from_its_adapter(self):
        uuid = "44444444-4444-4444-8444-444444444444"
        self.assertEqual(core.session_from_env({"CLAUDE_CODE_SESSION_ID": uuid}), uuid)
        self.assertIsNone(core.session_from_env({"CLAUDE_CODE_SESSION_ID": "local-abc"}))  # Claude Code's are UUIDs
        self.assertIsNone(core.session_from_env({"CODEX_THREAD_ID": "t1"}))  # Codex's goes to native_address
        self.assertIsNone(core.session_from_env({}))
        self.assertEqual(core.session_from_env({"HERMES_SESSION_ID": "local-abc"}), "local-abc")  # the built-in Hermes
        self.user_module("hermes", self.HERMES)
        env = {"HERMES_SESSION_ID": "local-abc", "CLAUDE_CODE_SESSION_ID": uuid}  # started from a Claude Code shell
        self.assertEqual(core.session_from_env(env), "local-abc")
        for bad in ("../x", "*", "a/b", ""):
            self.assertIsNone(core.session_from_env({"HERMES_SESSION_ID": bad}), bad)

    def test_a_command_records_it_and_the_sessions_tab_finds_the_chat(self):
        logs = Path(self.tmp.name) / "hermes"
        logs.mkdir()
        (logs / "local-abc.jsonl").write_text("{}\n")
        self.user_module("hermes", self.HERMES)
        core.register(self.c, "w")
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "CODEX", "MAXPM_AGENT"))}
        with mock.patch.dict(os.environ, {**clean, "HERMES_SESSION_ID": "local-abc", "HERMES_LOGS": str(logs)},
                             clear=True), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            cli.run(["--as", "w", "who"])  # any maxpm command of the agent
        self.assertEqual([r[0] for r in self.c.execute("SELECT session_id FROM agent_sessions WHERE agent='w'")],
                         ["local-abc"])
        with mock.patch.dict(os.environ, {"HERMES_LOGS": str(logs)}):
            self.assertEqual(sessions.transcript(self.c, "w"), ("hermes", logs / "local-abc.jsonl"))


class Setup(Env):
    """#5184: the setup part. The MCP config writers, the instruction file names, the voice and the web link texts
    are in the adapters, and the page reads them from GET /api/agents instead of testing agent names."""

    def test_the_instruction_files_come_from_the_adapters(self):
        self.assertEqual(agents.get("claude").setup["instructions"], "CLAUDE.md")
        self.assertEqual(agents.get("codex").setup["instructions"], "AGENTS.md")
        self.assertEqual(agents.instruction_files(), ("CLAUDE.md", "AGENTS.md", "@AGENTS.md", "Claude Code"))
        self.assertEqual(agents.shared_readers()[0], "Codex")  # and the other agents that read AGENTS.md (Pi)
        self.assertTrue(cli._imports_agents("# rules\n@AGENTS.md\n"))
        self.assertIn("Codex and other agents read AGENTS.md", cli.move_hint())

    def test_a_user_adapter_with_its_own_file_name_changes_what_init_writes(self):
        # A user module that replaces claude with another file name: init and the layout follow it.
        self.user_module("claude", """
            DESCRIBE = {"id": "claude", "label": "Claude Code", "exe": "claude", "family": "claude"}
            SETUP = {"instructions": "CLAUDE.local.md", "import": "@{file}"}
        """)
        d = Path(self.tmp.name, "proj")
        d.mkdir()
        lines = cli.setup_instructions(d)
        self.assertEqual((d / "CLAUDE.local.md").read_text(), "@AGENTS.md\n")
        self.assertFalse((d / "CLAUDE.md").exists())
        self.assertTrue(any("CLAUDE.local.md: created" in x for x in lines))
        self.assertEqual(cli.instructions_layout(d), "shared")

    def test_the_voice_text_comes_from_the_adapter(self):
        self.assertEqual(sessions.provider("claude-code")["voice_cli"], agents.get("claude").setup["voice"])
        self.assertEqual(sessions.provider(None, "codex")["voice_cli"], agents.get("codex").setup["voice"])
        self.assertEqual(sessions.provider(None, "codex")["agent"], "codex")
        self.assertIsNone(sessions.provider("other-cli")["agent"])

    def test_setup_agent_writes_the_mcp_config_through_the_adapter(self):
        f = Path(self.tmp.name, "claude_desktop_config.json")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.run(["setup-agent", "--claude-desktop", "--config", str(f)])
        self.assertIn("added MaximizePM", out.getvalue())
        self.assertEqual(json.loads(f.read_text())["mcpServers"]["maxpm"], agents.setup_entry())
        self.assertFalse(hasattr(cli, "setup_claude_desktop"))  # moved, not copied
        with self.assertRaisesRegex(RiverError, "no MCP config writer"):
            agents.Adapter(module(DESCRIBE=GOOD), "builtin").write_mcp_config()

    def test_the_page_reads_every_agent_from_one_endpoint(self):
        from river import server
        h = server.Handler.__new__(server.Handler)
        h.path, h.client_address, h.headers, out = "/api/agents", ("127.0.0.1", 5555), {"Host": "127.0.0.1:8765"}, {}
        h._send = lambda code, body, ctype=None: out.update(code=code, body=body)
        h.do_GET()
        self.assertEqual(out["code"], 200)
        got = {a["id"]: a for a in out["body"]["agents"]}
        self.assertEqual(list(got)[:2], ["claude", "codex"])
        self.assertTrue(got["claude"]["main"])
        self.assertFalse(got["codex"]["main"])
        self.assertIn("claude-code", got["claude"]["names"])
        self.assertEqual(got["claude"]["setup"]["open_link"], "Open on claude.ai")
        self.assertIn("Codex session has no web link", got["codex"]["setup"]["no_link"])
        self.assertTrue(got["codex"]["parts"]["setup"])

    def test_the_sessions_view_tests_no_agent_name(self):
        js = (Path(cli.__file__).parent / "static" / "views" / "sessions.js").read_text(encoding="utf-8")
        for name in ("\"claude-code\"", "\"codex\"", "Open on claude.ai", "A Codex session"):
            self.assertNotIn(name, js)


class SharedGateway(Env):
    """#5247: a Hermes command runs under the background gateway that every Hermes session shares, so the session's
    own process is the ancestor only when that one runs on a terminal."""

    TREE = {100: (50, "zsh", "-zsh"), 50: (1, "python3", "python3 -I -c import os, re, sys")}

    def test_an_ancestor_with_no_terminal_is_no_session_s_own_process(self):
        with mock.patch.object(core, "proc_info", self.TREE.get):
            with mock.patch.object(core, "proc_tty", lambda pid: ""):
                self.assertEqual(core.agent_process(100, terminal=True), (None, None))
                self.assertEqual(core.agent_process(100)[0], 50)  # every other agent: the ancestor, as before
            with mock.patch.object(core, "proc_tty", lambda pid: "ttys030"):
                self.assertEqual(core.agent_process(100, terminal=True)[0], 50)

    def test_hermes_needs_a_terminal_and_the_others_do_not(self):
        self.assertEqual(agents.get("hermes").identity["process"], "terminal")
        self.assertTrue(core.process_on_terminal(self.c, "x", env={"HERMES_SESSION_ID": "20261111_0001"}))
        self.assertFalse(core.process_on_terminal(self.c, "x", env={"CLAUDECODE": "1"}))
        core.register(self.c, "h1")
        self.c.execute("UPDATE agents SET agent_type='hermes' WHERE name='h1'")
        self.assertTrue(core.process_on_terminal(self.c, "h1", env={}))  # by the agent's type
        bad = module(DESCRIBE={**GOOD, "id": "x"}, IDENTITY={"source": "env", "process": "daemon"})
        self.assertIn("IDENTITY['process'] is 'daemon'", " ".join(agents.check(bad, "x")))

    def test_a_recorded_gateway_is_forgotten_and_not_recorded_again(self):
        core.register(self.c, "h1")
        self.c.execute("UPDATE agents SET agent_type='hermes', pid=4242, pid_cmd='python3 -I -c gateway', host=? "
                       "WHERE name='h1'", (core.this_host(),))
        gateway = lambda terminal=False: (None, None) if terminal else (4242, "python3 -I -c gateway")  # noqa: E731
        env = {"MAXPM_AGENT": "h1", "HERMES_SESSION_ID": "20261111_0001"}
        with mock.patch.dict(os.environ, env), mock.patch.object(core, "agent_process", gateway), \
                mock.patch.object(core, "proc_tty", lambda pid: ""):
            cli._record_process(self.c, "h1")
        row = self.c.execute("SELECT pid, pid_cmd FROM agents WHERE name='h1'").fetchone()
        self.assertEqual((row["pid"], row["pid_cmd"]), (None, None))
        with mock.patch.dict(os.environ, env), mock.patch.object(core, "proc_tty", lambda pid: "ttys030"):
            self.c.execute("UPDATE agents SET pid=4243 WHERE name='h1'")
            self.assertFalse(core.drop_background_process(self.c, "h1"))  # a process on a terminal stays


class ChatPathType(Env):
    """#5260: Grok's chat_path returned a str, sessions.transcript called stat() on it, and the Sessions tab listed no
    session at all. Adapter.chat_path gives a Path for any module, and the built-in modules return one."""

    def test_a_str_from_a_user_module_becomes_a_path(self):
        log = Path(self.tmp.name) / "s1.log"
        log.write_text("{}\n")
        self.user_module("strlog", f"""
            DESCRIBE = {{"id": "strlog", "label": "Str", "exe": "strlog", "family": "multi"}}
            CHAT = {{"compressed": False, "file_tools": [], "ref": "session", "cache_write": False}}
            def chat_path(ref, folder, root=None):
                return {str(log)!r} if ref == "s1" else ""
            def parse_chat(lines):
                return [], None
        """)
        a = agents.get("strlog")
        self.assertEqual(a.chat_path("s1"), log)
        self.assertIsNone(a.chat_path("other"))  # "" is no file
        core.register(self.c, "w")
        with core.tx(self.c):
            self.c.execute("INSERT INTO agent_sessions(agent, session_id, first_seen) VALUES ('w', 's1', ?)",
                           (core.iso(core.now()),))
        self.assertEqual(sessions.transcript(self.c, "w"), ("strlog", log))

    def test_every_built_in_chat_path_returns_a_path_or_none(self):
        import inspect
        for mid in agents.BUILTIN:
            m = agents.load()[mid].module
            f = getattr(m, "chat_path", None)
            if f is None:
                continue
            src = inspect.getsource(f)
            self.assertNotRegex(src, r"return str\(", f"{mid}.chat_path returns a str")
