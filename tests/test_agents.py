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
    def test_the_built_in_adapters_pass_the_check_and_have_only_their_describe_part(self):
        for mid in agents.BUILTIN:
            a = agents.get(mid)
            self.assertEqual(agents.check(a.module, mid), [])
            self.assertEqual(a.source, "builtin")
            self.assertEqual(a.missing(), ["launch"])
            self.assertTrue(a.supports("describe"))
            self.assertFalse(any(a.supports(p) for p in agents.PARTS if p != "describe"))
            with self.assertRaisesRegex(RiverError, "no launch part"):
                a.build_command("w", "/tmp", "opus", None, {}, "go")
        self.assertEqual(agents.ids()[:2], ["claude", "codex"])

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
            LAUNCH={"session_name": True, "fork": False},
            build_command=lambda name, folder, model, effort, settings, prompt: ["pi", "--model", model, prompt],
            IDENTITY={"source": "env", "env": "PI_SESSION", "log_glob": None, "pane": None, "url": None,
                      "inside_env": ["PI_SESSION"]},
            CHAT={"compressed": False, "file_tools": ["write"]},
            chat_path=lambda ref, folder: f"/logs/{ref}.jsonl",
            parse_chat=lambda lines: ([{"at": None, "role": "assistant", "text": x} for x in lines], "idle"),
            STATE={"state": "detect", "session_id": "identity", "background": False},
            DETECT={"process": "pi", "idle": r"^> $", "working": r"esc to interrupt", "blocked": [r"Allow\?"],
                    "tell_instead": "No, and tell Pi what to do", "image_paste": "[image {n}]"},
            INPUT={"native": None},
            resume_command=lambda ref, model, effort, settings: ["pi", "--resume", ref],
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
        self.user_module("pi", """
            DESCRIBE = {"id": "pi", "label": "Pi", "exe": "pi", "aliases": ["pi-agent"], "family": "multi"}
            IDENTITY = {"source": "env", "inside_env": ["PI_SESSION"]}
        """)
        a = agents.get("pi-agent")
        self.assertEqual((a.id, a.source), ("pi", str(self.dir / "pi.py")))
        self.assertEqual(agents.ids(), ["claude", "codex", "pi"])
        self.assertIs(agents.from_env({"PI_SESSION": "1", "CLAUDECODE": "1"}), a)
        self.assertIs(agents.from_env({"MAXPM_AGENT_TYPE": "codex", "PI_SESSION": "1"}), agents.get("codex"))
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
        self.user_module("pi", "DESCRIBE = {'id': 'pi', 'label': 'Pi', 'exe': 'pi', 'family': 'multi'}\n")
        self.assertEqual(agents.get("pi").label, "Pi")
        f = self.dir / "pi.py"
        f.write_text("DESCRIBE = {'id': 'pi', 'label': 'Pi 2', 'exe': 'pi', 'family': 'multi'}\n")
        os.utime(f, ns=(1, 1))
        self.assertEqual(agents.get("pi").label, "Pi 2")
        f.unlink()
        self.assertIsNone(agents.find("pi"))


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


if __name__ == "__main__":
    unittest.main()
