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
            for part in ("describe", "effort", "launch", "identity", "resume", "chat"):
                self.assertTrue(a.supports(part), (mid, part))
            for part in ("state", "detect", "input") if mid in ("claude", "codex") else ():  # #5183
                self.assertTrue(a.supports(part), (mid, part))
        self.assertEqual(agents.ids()[:2], ["claude", "codex"])

    def test_a_module_with_no_launch_part_loads_but_cannot_start(self):
        a = agents.Adapter(module(DESCRIBE=GOOD), "test")
        self.assertEqual(a.missing(), ["launch"])
        with self.assertRaisesRegex(RiverError, "agent pi has no launch part"):
            a.build_command("w", "/tmp", "opus", None, {}, "go")
        self.assertIsNone(a.resume_command("s1"))

    def test_the_built_in_describe_matches_the_platform_tables_of_today(self):
        for mid, platform in (("claude", "claude-code"), ("codex", "codex")):
            d, pl = agents.get(mid).describe, core.LAUNCH_PLATFORMS[platform]
            self.assertEqual((d["label"], d["exe"], d["family"], d["prefix"]), (pl["label"], pl["exe"], pl["family"], pl["prefix"]))
            self.assertIs(agents.get(platform), agents.get(mid))
        ladder = core.parse_ladder(core.DEFAULT_SETTINGS["model_ladder"])
        self.assertEqual(agents.get("claude").describe["models"], ladder["claude"])
        self.assertEqual(agents.get("codex").describe["models"], ladder["openai"])
        ids = core.LAUNCH_PLATFORMS["codex"]["options"]["model_ids"]["default"]
        self.assertEqual(agents.get("codex").describe["model_ids"], dict(p.split("=") for p in ids.replace(" ", "").split(",")))

    def test_names_are_found_in_any_case_and_an_unknown_name_says_what_is_registered(self):
        self.assertIs(agents.find("Claude-Code"), agents.get("claude"))
        self.assertIsNone(agents.find(""))
        with self.assertRaisesRegex(RiverError, r"agent 'gemini' is not registered.*claude, codex.*agents/<id>\.py"):
            agents.get("gemini")

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
        self.user_module("gemini", "DESCRIBE = {'id': 'gemini', 'label': 'Gemini', 'exe': 'gemini'}\n")
        self.user_module("Bad-Name", "DESCRIBE = {}\n")
        self.assertEqual(agents.get("claude").source, "builtin")
        err = agents.errors()
        self.assertIn("ZeroDivisionError", err["claude"])
        self.assertIn("DESCRIBE has no 'family'", err["gemini"])
        self.assertIn("must be an id", err["Bad-Name"])
        with self.assertRaisesRegex(RiverError, r"agent 'gemini' is not registered \(.*gemini\.py: DESCRIBE has no 'family'"):
            agents.get("gemini")

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
        self.assertEqual(core.PLATFORM_EFFORTS, {"claude": cl.effort["levels"], "openai": cx.effort["levels"]})
        self.assertEqual(core.AGENT_FAMILIES, {"claude": "claude", "codex": "openai"})
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
