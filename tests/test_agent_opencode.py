"""The OpenCode adapter, river/agents/opencode.py (#5171), against the files recorded from OpenCode 2.2.7 in
tests/fixtures/opencode."""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from river import agents, core, sessions
from river.agents import opencode
from tests import timing

FIX = Path(__file__).resolve().parent / "fixtures" / "opencode"
SESSION_ID = "ses_e366d1e13ffe0SgSDgPP6MNKXx"


def lines(name="session.jsonl"):
    return (FIX / name).read_text(encoding="utf-8").splitlines()


def screen(name):
    return (FIX / f"pane-{name}.txt").read_text(encoding="utf-8")


class Env(unittest.TestCase):
    """A temporary queue (MAXPM_DB), home, OpenCode config folder and database, so no test reads or writes the
    person's own files."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / "home").mkdir()
        env = mock.patch.dict(os.environ, {"MAXPM_DB": str(self.root / "t.db"), "HOME": str(self.root / "home"),
                                           "OPENCODE_CONFIG_DIR": str(self.root / "oc-config"),
                                           "OPENCODE_DB": str(self.root / "oc" / "opencode.db")})
        env.start()
        self.addCleanup(env.stop)
        for k in ("OPENCODE_CLI_CONFIG_CONTENT", "OPENCODE_CONFIG_CONTENT", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
            os.environ.pop(k, None)
        agents.load(refresh=True)
        self.addCleanup(lambda: agents.load(refresh=True))
        self.a = agents.get("opencode")


class Describe(Env):
    def test_it_is_a_built_in_adapter_with_every_part_but_effort(self):
        self.assertEqual(agents.check(opencode, "opencode"), [])
        self.assertEqual((self.a.source, self.a.missing()), ("builtin", []))
        self.assertIs(agents.get("open-code"), self.a)
        for part in ("describe", "settings", "launch", "identity", "chat", "state", "detect", "input", "resume",
                     "commands", "setup"):
            self.assertTrue(self.a.supports(part), part)
        # OpenCode's variant cannot be set at the start, and /effort takes no argument: the page hides effort.
        self.assertFalse(self.a.supports("effort"))
        self.assertIsNone(self.a.effort_name("high"))
        self.assertEqual(self.a.describe["family"], "multi")
        self.assertEqual(self.a.config_key("standalone"), "opencode_standalone")

    def test_the_default_model_is_a_free_model_of_opencodes_own_provider(self):
        self.assertEqual(self.a.describe["default_model"], "opencode/big-pickle")
        self.assertIn(self.a.describe["default_model"], self.a.describe["models"])
        self.assertTrue(all(m.startswith("opencode/") for m in self.a.describe["models"]))

    def test_a_command_inside_opencode_is_found_by_its_env_even_from_a_claude_code_shell(self):
        env = {"OPENCODE_SESSION_ID": SESSION_ID, "OPENCODE": "1", "AI_AGENT": "opencode"}
        self.assertIs(agents.from_env(env), self.a)
        self.assertIs(agents.from_env({**env, "CLAUDECODE": "1"}), self.a)
        self.assertIsNone(agents.from_env({"OPENCODE": "1"}))

    def test_read_models_reads_the_cli_list_once_and_keeps_only_model_ids(self):
        calls = []

        def runner():
            calls.append(1)
            return "opencode/big-pickle\nopencode/glm-5.3-flash-free\nopenrouter/meta/muse-spark-1.3-contributor\n" \
                   "a warning line\n"

        mock.patch.object(opencode, "MODELS_RUNNER", runner).start()
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(opencode._models, {"at": 0.0, "list": None}).start()
        got = self.a.read_models()
        self.assertEqual(got, ["opencode/big-pickle", "opencode/glm-5.3-flash-free",
                               "openrouter/meta/muse-spark-1.3-contributor"])
        self.assertEqual(self.a.read_models(), got)
        self.assertEqual(len(calls), 1)

    def test_a_page_build_never_waits_for_the_cli_and_an_old_list_stands_while_a_thread_reads(self):
        # #5638: `opencode models` took 470 ms of the page's build once in ten minutes.
        import threading
        go, calls = threading.Event(), []

        def runner():
            calls.append(1)
            go.wait(5)
            return f"opencode/model-{len(calls)}\n"
        mock.patch.object(opencode, "MODELS_RUNNER", runner).start()
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(opencode._models, {"at": 0.0, "list": None, "reader": None}).start()
        token = core.BUILD_MEMO.set({})
        try:
            t = time.monotonic()
            self.assertIsNone(opencode.read_models())  # no list yet: the build goes on with the described models
            self.assertEqual(self.a.read_models(), opencode.DESCRIBE["models"])
            self.assertLess(time.monotonic() - t, timing.limit(1, below=5))  # the runner waits 5 s
        finally:
            core.BUILD_MEMO.reset(token)
        go.set()
        self.assertEqual(opencode.read_models(), ["opencode/model-1"])  # a command waits for the read
        self.assertEqual(len(calls), 1)  # one read at a time
        opencode._models["at"] = 0.0  # ten minutes later: the old list, and a thread reads the new one
        self.assertEqual(opencode.read_models(), ["opencode/model-1"])
        reader = opencode._models["reader"]  # None when it is done already
        if reader is not None:
            reader.join(5)
        self.assertEqual(opencode.read_models(), ["opencode/model-2"])

    def test_the_tests_never_run_the_installed_opencode(self):
        # #5286: core.platform_models (the launch dialog, a launch) calls read_models; tests/__init__.py fakes it.
        mock.patch.dict(opencode._models, {"at": 0.0, "list": None}).start()
        self.addCleanup(mock.patch.stopall)
        with mock.patch("subprocess.run", side_effect=AssertionError("ran a CLI")):
            self.assertEqual(self.a.read_models(), opencode.DESCRIBE["models"])

    def test_a_running_session_s_settings_dialog_offers_both_toggles(self):
        # #5286: a restart applies them (flags), and the dialog reads them back from the command line.
        got = {s["key"]: s for s in self.a.session_specs()}
        self.assertEqual(set(got), {"model", "standalone", "auto"})
        self.assertEqual((got["auto"]["restart"], got["auto"]["choices"]), ("opt", ["on", "off"]))

    def test_with_no_cli_answer_the_described_models_stand(self):
        mock.patch.object(opencode, "MODELS_RUNNER", lambda: "").start()
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(opencode._models, {"at": 0.0, "list": None}).start()
        self.assertEqual(self.a.read_models(), opencode.DESCRIBE["models"])

    def test_the_launch_dialog_gets_the_described_models_first_and_finds_the_rest_by_typing(self):
        # #5238: `opencode models` lists about 500 models; the dialog offers the adapter's own ones (pick) first.
        picks = opencode.DESCRIBE["models"]
        others = [f"openrouter/vendor/model-{n}" for n in range(60)]
        cli = others[:30] + [picks[1]] + others[30:]  # the CLI knows a pick too, in its own place
        mock.patch.object(opencode, "MODELS_RUNNER", lambda: "\n".join(cli) + "\n").start()
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(opencode._models, {"at": 0.0, "list": None}).start()
        conn = core.connect()
        self.addCleanup(conn.close)
        ms = core.platform_models(conn, "opencode")
        self.assertEqual([m["name"] for m in ms], list(picks) + others)  # each model once, the picks first
        self.assertEqual([m["name"] for m in ms if m["pick"]], list(picks))
        if not shutil.which("node"):
            return
        js = Path(__file__).resolve().parent.parent / "river" / "static" / "components" / "modelPick.js"
        code = f"""
            const {{ modelChoices, LONG }} = await import({json.dumps(js.as_uri())});
            const ms = {json.dumps(ms)};
            const names = (r) => [r.shown.map(m => m.name), r.all.length];
            console.log(JSON.stringify({{
              long: names(modelChoices(ms, "")),
              current: names(modelChoices(ms, "openrouter/vendor/model-7")),
              short: names(modelChoices(ms.slice(0, LONG), "")),
              nopick: names(modelChoices(ms.map(m => ({{ ...m, pick: false }})), "")),
            }}));"""
        r = subprocess.run(["node", "--input-type=module"], input=code, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout)
        self.assertEqual(got["long"], [list(picks), len(ms)])
        self.assertEqual(got["current"], [list(picks) + ["openrouter/vendor/model-7"], len(ms)])
        self.assertEqual(got["short"], [[m["name"] for m in ms[:30]], 0])  # a short list: every model, no search
        self.assertEqual(got["nopick"][1], 0)  # no picks: the whole list as before

    def test_the_settings_dialog_of_a_session_gets_the_picks_and_shortens_the_list(self):
        # #5242: the Model row of a running OpenCode session lists the same picks first, as the launch dialog does.
        picks = opencode.DESCRIBE["models"]
        others = [f"openrouter/vendor/model-{n}" for n in range(60)]
        mock.patch.object(opencode, "MODELS_RUNNER", lambda: "\n".join(others) + "\n").start()
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(opencode._models, {"at": 0.0, "list": None}).start()
        conn = core.connect()
        self.addCleanup(conn.close)
        core.register(conn, "oc1")
        conn.execute("UPDATE agents SET agent_type='opencode', pid_cmd='opencode --model openrouter/vendor/model-7' "
                     "WHERE name='oc1'")
        row = next(s for s in sessions.session_settings(conn, "oc1")["settings"] if s["key"] == "model")
        self.assertEqual((row["kind"], len(row["choices"])), ("choice", len(picks) + 60))
        self.assertEqual(row["picks"], list(picks))
        if not shutil.which("node"):
            return
        js = Path(__file__).resolve().parent.parent / "river" / "static" / "components" / "modelPick.js"
        code = f"""
            const {{ nameChoices }} = await import({json.dumps(js.as_uri())});
            const r = {json.dumps(row)};
            console.log(JSON.stringify([nameChoices(r.choices, r.picks, "openrouter/vendor/model-7"),
                                        nameChoices(r.choices.slice(0, 5), r.picks, "")]));"""
        r = subprocess.run(["node", "--input-type=module"], input=code, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        long, short = json.loads(r.stdout)
        self.assertEqual(long["shown"], list(picks) + ["openrouter/vendor/model-7"])
        self.assertEqual(len(long["all"]), len(row["choices"]))  # "Other model…" finds every model
        self.assertEqual((short["shown"], short["all"]), (row["choices"][:5], []))
        text = (js.parent / "settingsDialog.js").read_text(encoding="utf-8")
        self.assertIn("nameChoices(s.choices, s.picks, val)", text)
        self.assertIn("Other model…", text)


class Launch(Env):
    def test_the_model_goes_in_by_the_config_of_a_standalone_server(self):
        argv = self.a.build_command("w1", "/work/demo", "opencode/big-pickle", "high", {}, "run maxpm go")
        self.assertEqual(argv, ["env", 'OPENCODE_CONFIG_CONTENT={"model":"opencode/big-pickle"}', "opencode",
                                "--standalone", "--prompt", "run maxpm go", "/work/demo"])
        self.assertEqual(core.shell_command(argv),
                         "env 'OPENCODE_CONFIG_CONTENT={\"model\":\"opencode/big-pickle\"}' opencode --standalone "
                         "--prompt 'run maxpm go' /work/demo")

    def test_without_its_own_server_no_model_goes_in(self):
        # The shared background service keeps the config it started with, so a model there would not apply.
        argv = self.a.build_command("w1", None, "opencode/big-pickle", None, {"standalone": "off"}, None)
        self.assertEqual(argv, ["opencode"])

    def test_auto_resume_and_args(self):
        argv = self.a.build_command(None, None, None, None, {"auto": "on", "resume": "ses_1", "args": "--log-level info"},
                                    "go on")
        self.assertEqual(argv, ["opencode", "--standalone", "--auto", "--session", "ses_1", "--log-level info",
                                "--prompt", "go on"])
        self.assertIsInstance(argv[5], agents.Raw)

    def test_resume_command_continues_the_session(self):
        self.assertEqual(self.a.resume_command(SESSION_ID),
                         ["opencode", "--standalone", "--session", SESSION_ID])
        self.assertEqual(self.a.resume_command(SESSION_ID, "opencode/glm-5.3-flash-free", None,
                                               {"prompt": "the answer came"}),
                         ["env", 'OPENCODE_CONFIG_CONTENT={"model":"opencode/glm-5.3-flash-free"}', "opencode",
                          "--standalone", "--session", SESSION_ID, "--prompt", "the answer came"])
        self.assertIsNone(self.a.resume_command(""))
        self.assertIsNone(self.a.resume_command("ses; rm -rf /"))

    def test_parse_command_reads_back_what_a_profile_builds(self):
        words = ["env", 'OPENCODE_CONFIG_CONTENT={"model":"{model}"}', "opencode", "--standalone", "--auto",
                 "--prompt", "run maxpm go"]
        self.assertEqual(self.a.parse_command(words),
                         {"settings": {"standalone": "on", "auto": "on"}, "prompt": "run maxpm go"})
        self.assertEqual(self.a.parse_command(["/opt/homebrew/bin/opencode", "--prompt", "hi"]),
                         {"settings": {"standalone": "off"}, "prompt": "hi"})
        for bad in (["opencode"], ["opencode", "run", "hi"], ["opencode", "--session", "x", "--prompt", "hi"],
                    ["env", 'OPENCODE_CONFIG_CONTENT={"model":"x/y"}', "opencode", "--prompt", "hi"],
                    ["claude", "--prompt", "hi"]):
            self.assertIsNone(self.a.parse_command(bad), bad)


class DefaultModel(Env):
    """#5284: a launch that names no model, of an item that names none, runs the adapter's DESCRIBE default_model
    (OpenCode: its free opencode/big-pickle), not whatever model OpenCode's TUI remembers."""

    def setUp(self):
        super().setUp()
        self.c = core.connect()
        self.addCleanup(self.c.close)
        (self.root / "shop").mkdir()
        core.project_add(self.c, "shop", path=str(self.root / "shop"))
        core.config_set(self.c, "launch_agents", "Claude Code=@claude-code; OpenCode=@opencode")

    def test_no_model_named_runs_the_adapters_default(self):
        x = core.item_add(self.c, "shop", "work")["id"]
        t = core.launch_target(self.c, agent="OpenCode", item=x)
        self.assertEqual((t["model"], t["env"].get("MAXPM_MODEL")), ("opencode/big-pickle", "opencode/big-pickle"))
        self.assertIn('"model":"opencode/big-pickle"', t["command"])
        self.assertIn("the default model of OpenCode", t["model_why"])
        # Any start path: the focused and manager starts build with no model too.
        cmd = core._launch_agent_cmd(self.c, core._project(self.c, "shop")["id"], "OpenCode")
        self.assertEqual((cmd["model"], cmd["adapter_default"]), ("opencode/big-pickle", True))

    def test_a_model_named_wins_and_an_agent_with_no_default_gets_none(self):
        x = core.item_add(self.c, "shop", "work")["id"]
        t = core.launch_target(self.c, agent="OpenCode", item=x, model="openrouter/qwen/qwen3-coder")
        self.assertEqual((t["model"], t["model_why"]), ("openrouter/qwen/qwen3-coder", "chosen for this launch"))
        t = core.launch_target(self.c, agent="Claude Code", item=x)
        self.assertEqual((t["model"], t["adapter_default"]), (None, False))  # Claude Code names no default
        self.assertNotIn("--model", t["command"])
        # The setting default_model applies to an item with no model (#5346), and the session runs it.
        core.config_set(self.c, "default_model", "opus")
        t = core.launch_target(self.c, agent="Claude Code", item=x)
        self.assertEqual((t["model"], t["model_why"]), ("opus", "the default model (global)"))
        self.assertIn("--model", t["command"])
        # The same for default_effort (#5346).
        core.config_set(self.c, "default_effort", "high")
        self.assertIn("high", core.launch_target(self.c, agent="Claude Code", item=x)["command"])
        core.config_unset(self.c, "default_effort")
        # Not of the agent's family: the agent's own default.
        self.assertEqual(core.launch_target(self.c, agent="OpenCode", item=x)["model"], "opencode/big-pickle")


class Detect(Env):
    """The screen rules against the screens recorded in tmux. The plugin is the one source of the state; these rules
    hold the pane's data, and must still say each recorded screen right."""

    def match(self, key, name):
        pats = opencode.DETECT[key] if isinstance(opencode.DETECT[key], list) else [opencode.DETECT[key]]
        return any(re.search(p, screen(name)) for p in pats)

    def test_each_recorded_screen_matches_its_own_rule_only(self):
        expect = {"idle": "idle", "idle-after-turn": "idle", "working": "working", "compacting": "working",
                  "blocked": "blocked"}
        for name, state in expect.items():
            got = [k for k in ("idle", "working", "blocked") if self.match(k, name)]
            self.assertEqual(got, [state], name)

    def test_the_permission_screen_has_the_tell_instead_option(self):
        self.assertIn(opencode.DETECT["tell_instead"], screen("blocked"))
        self.assertIn("Allow once", screen("blocked"))


class Card(Env):
    """#5281: the answer card of the Sessions tab reads OpenCode's one-line permission menu (DETECT['row']), and an
    answer moves in it with Right and Left."""

    def card(self):
        from river import server
        from tests.test_server import FakeTmux
        conn = core.connect()
        self.addCleanup(conn.close)
        core.register(conn, "oc-1")
        tmux = FakeTmux()
        tmux.session = True
        tmux._pane(name="oc-1")
        tmux.panes[0].update(agent="oc-1", running="opencode", screen=screen("blocked"))
        old = server.TMUX_RUNNER, core.PLATFORM, server.PROMPT_PAUSE
        server.TMUX_RUNNER, core.PLATFORM, server.PROMPT_PAUSE = tmux, "linux", 0
        self.addCleanup(lambda: (setattr(server, "TMUX_RUNNER", old[0]), setattr(core, "PLATFORM", old[1]),
                                 setattr(server, "PROMPT_PAUSE", old[2])))
        pane = tmux.panes[0]
        return server, conn, pane, server.pane_prompts(conn, {"oc-1": pane["id"]})["oc-1"]

    def test_the_card_has_the_question_and_the_options_of_the_one_line_menu(self):
        _, _, _, card = self.card()
        self.assertEqual(card["question"], "Permission required · Read .env · Path: .env")
        self.assertEqual(card["options"], ["Allow once", "Always allow", "Reject"])
        self.assertEqual((card["selected"], card["tell"], card["row"]), (0, 2, True))

    def test_an_answer_moves_with_right_and_the_text_goes_by_reject(self):
        server, conn, pane, card = self.card()
        with mock.patch.object(server, "agent_terminals", lambda c: {"oc-1": pane["id"]}):
            got = server.prompt_answer(conn, "oc-1", card["sig"], pick=1, who="mark")
            self.assertEqual((got["picked"], pane["keys"][-2:]), ("Always allow", ["Right", "Enter"]))
            got = server.prompt_answer(conn, "oc-1", card["sig"], other="read .env.example instead", who="mark")
        self.assertEqual(got["picked"], "Reject")
        self.assertEqual(pane["keys"][-5:], ["Right", "Right", "Enter", "read .env.example instead", "Enter"])

    def test_no_card_on_the_other_screens(self):
        from river import server
        lay, rx = server._layout(opencode.DETECT), re.compile("|".join(opencode.DETECT["blocked"]))
        for name in ("idle", "idle-after-turn", "working", "compacting"):
            self.assertIsNone(server._prompt_found(server._prompt_tail(screen(name)), rx, lay), name)


class Commands(Env):
    def test_the_built_in_list_is_the_recorded_slash_list(self):
        self.assertEqual([c["name"] for c in opencode.COMMANDS], [w.lstrip("/") for w in (FIX / "commands.txt").read_text(encoding="utf-8").split()])
        self.assertIn("compact", [c["name"] for c in self.a.read_commands(None)])

    def test_custom_commands_of_the_folder_up_to_git_and_of_the_global_config(self):
        work = self.root / "repo" / "sub"
        (self.root / "repo" / ".git").mkdir(parents=True)
        d = self.root / "repo" / ".opencode" / "commands" / "deep"
        d.mkdir(parents=True)
        (d.parent / "hello.md").write_text("---\ndescription: Say hello\nargument-hint: <name>\n---\nHi $ARGUMENTS\n", encoding="utf-8")
        (d / "x.md").write_text("Nested one\n", encoding="utf-8")
        work.mkdir(parents=True)
        g = self.root / "oc-config" / "command"
        g.mkdir(parents=True)
        (g / "hello.md").write_text("---\ndescription: the global one\n---\n", encoding="utf-8")
        (g / "ship.md").write_text("---\ndescription: Ship it\n---\n", encoding="utf-8")
        (g / "compact.md").write_text("a custom command cannot take a built-in name\n", encoding="utf-8")
        got = self.a.read_commands(str(work))
        names = [c["name"] for c in got]
        self.assertEqual(names[:len(opencode.COMMANDS)], [c["name"] for c in opencode.COMMANDS])
        custom = got[len(opencode.COMMANDS):]
        self.assertEqual(custom, [
            {"name": "deep/x", "description": "Nested one", "hint": ""},
            {"name": "hello", "description": "Say hello", "hint": "<name>"},
            {"name": "ship", "description": "Ship it", "hint": ""},
        ])


class Chat(Env):
    def test_the_recorded_session_reads_as_shared_entries(self):
        entries, turn = self.a.parse_chat(lines())
        self.assertEqual(turn, "idle")
        self.assertEqual([e["role"] for e in entries], [
            "user", "tool", "result", "assistant",
            "user", "tool", "result", "assistant",
            "user", "tool", "result", "event",
            "user", "tool", "result", "tool", "result", "assistant",
            "divider", "summary"])
        tools = [(e["name"], e["detail"]) for e in entries if e["role"] == "tool"]
        self.assertEqual(tools, [("read", "a.txt"), ("read", "/private/tmp/claude-501/outside.txt"), ("read", ".env"),
                                 ("write", "b.txt"), ("edit", "b.txt")])
        rejected = entries[10]
        self.assertEqual((rejected["text"], rejected["error"], rejected["for"]),
                         ("The user declined this tool call", True, entries[9]["id"]))
        self.assertEqual(entries[11]["text"], "interrupted")
        self.assertEqual(entries[0]["text"], "Read a.txt and tell me its content in one line.")
        self.assertEqual(entries[0]["at"], "2026-11-11T07:46:49.000Z")
        self.assertEqual(entries[18]["kind"], "compacted")
        self.assertTrue(entries[19]["text"].startswith("## Objective"))
        self.assertTrue(set(opencode.CHAT["file_tools"]) <= {n for n, _ in tools})

    def test_a_failed_turn_says_its_error_once(self):
        entries, turn = self.a.parse_chat(lines("failed.jsonl"))
        self.assertEqual([e["role"] for e in entries], ["user", "event"])
        self.assertTrue(entries[1]["text"].startswith("error: Free usage limit reached."))
        self.assertEqual(turn, "idle")

    def test_the_turn_runs_until_the_idle_row(self):
        rows = lines()
        cut = next(i for i, ln in enumerate(rows) if '"seq": 58,' in ln)  # the write step of the last turn
        self.assertEqual(self.a.parse_chat(rows[:cut])[1], "working")  # the user's message is the last row
        self.assertEqual(self.a.parse_chat(rows[:cut + 1])[1], "working")  # a tool step, no idle row yet
        self.assertEqual(self.a.parse_chat(rows[:1])[1], None)

    def test_title_folder_usage_and_model(self):
        L = lines()
        self.assertEqual(opencode.chat_title(L), "Summarize a.txt in one line")
        self.assertEqual(opencode.chat_cwd(L), "/private/tmp/claude-501/oc-try")
        # OpenCode's own totals of the session, with the compaction step and the reasoning tokens.
        self.assertEqual(opencode.chat_usage(L), {"input": 19069, "output": 680 + 321, "cache_read": 59200,
                                                  "cache_write": 0})
        self.assertEqual(opencode.chat_usage(L[1:])["input"], 18091)  # no header: the sum of the steps
        self.assertEqual(opencode.chat_settings(L), {"model": "opencode/big-pickle"})
        self.assertIsNone(opencode.chat_title(lines("failed.jsonl")))


class Database(Env):
    """chat_path copies a session's rows of OpenCode's database into a file beside the queue."""

    def make_db(self, sessions):
        db = Path(os.environ["OPENCODE_DB"])
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        cols = [c for c in opencode.SESSION_COLUMNS]
        conn.execute(f"CREATE TABLE session_v2 ({', '.join(cols)}, project_id, slug, version)")
        conn.execute("CREATE TABLE session_message (id, session_id, type, seq, time_created, time_updated, data)")
        conn.execute("CREATE TABLE credential (id, secret)")  # a table the adapter never reads
        for name, sid, fork in sessions:
            rows = [json.loads(ln) for ln in lines(name)]
            head = dict(rows[0], id=sid, fork_session_id=fork)
            head["model"] = json.dumps(head["model"])
            conn.execute(f"INSERT INTO session_v2 ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                         [head[c] for c in cols])
            for r in rows[1:]:
                conn.execute("INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                             (r["id"] + sid[-4:], sid, r["type"], r["seq"], r["time"], r["time"],
                              json.dumps(r["data"])))
        conn.commit()
        conn.close()
        return db

    def test_the_file_holds_the_recorded_lines_and_is_written_again_only_after_a_change(self):
        db = self.make_db([("session.jsonl", SESSION_ID, None)])
        p = self.a.chat_path(SESSION_ID)
        self.assertEqual(p, Path(core.river_dir()) / "opencode" / "chats" / f"{SESSION_ID}.jsonl")
        got = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines()]
        want = [json.loads(ln) for ln in lines()]
        self.assertEqual([{k: v for k, v in r.items() if k != "id"} for r in got],
                         [{k: v for k, v in r.items() if k != "id"} for r in want])
        self.assertEqual(self.a.parse_chat(p.read_text(encoding="utf-8").splitlines())[0], self.a.parse_chat(lines())[0])
        before = p.stat().st_mtime_ns
        self.assertEqual(self.a.chat_path(SESSION_ID), p)
        self.assertEqual(p.stat().st_mtime_ns, before)  # the database did not change
        time.sleep(0.01)  # timing: a later mtime for the next write; a slow machine only makes it later
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO session_message VALUES ('m9', ?, 'user', 99, 1, 1, ?)",
                     (SESSION_ID, json.dumps({"time": {"created": 1794383500000}, "text": "one more"})))
        conn.commit()
        conn.close()
        self.a.chat_path(SESSION_ID)
        entries, turn = self.a.parse_chat(p.read_text(encoding="utf-8").splitlines())
        self.assertEqual((entries[-1]["text"], turn), ("one more", "working"))
        self.assertEqual(self.a.chat_path(str(p)), p)  # a path of such a file, as it is

    def test_the_sessions_tab_finds_the_chat_by_the_agents_newest_session_id(self):
        # #5267: sessions.transcript asks each adapter whose CHAT['ref'] is 'session' with the agent's newest id.
        from river import sessions
        self.make_db([("session.jsonl", SESSION_ID, None)])
        conn = core.connect()
        self.addCleanup(conn.close)
        core.register(conn, "oc1")
        conn.execute("INSERT INTO agent_sessions (agent, session_id, first_seen) VALUES ('oc1', ?, '2026-11-11')",
                     (SESSION_ID,))
        conn.commit()
        kind, path = sessions.transcript(conn, "oc1")
        self.assertEqual((kind, Path(path).name), ("opencode", f"{SESSION_ID}.jsonl"))
        self.assertEqual(self.a.chat_ref(), "session")

    def test_the_slash_list_offers_the_custom_commands_of_the_sessions_folder(self):
        # #5275: sessions.last_cwd finds the folder by a "cwd" key; the header names it.
        from river import sessions
        work = self.root / "work"
        (work / ".opencode" / "commands").mkdir(parents=True)
        (work / ".opencode" / "commands" / "hello.md").write_text("---\ndescription: Say hello\n---\nhi\n", encoding="utf-8")
        db = self.make_db([("session.jsonl", SESSION_ID, None)])
        conn = sqlite3.connect(db)
        conn.execute("UPDATE session_v2 SET directory=?", (str(work),))
        conn.commit()
        conn.close()
        mconn = core.connect()
        self.addCleanup(mconn.close)
        core.register(mconn, "oc1")
        mconn.execute("UPDATE agents SET agent_type='opencode' WHERE name='oc1'")
        mconn.execute("INSERT INTO agent_sessions (agent, session_id, first_seen) VALUES ('oc1', ?, '2026-11-11')",
                      (SESSION_ID,))
        mconn.commit()
        got = sessions.session_commands(mconn, "oc1")
        self.assertEqual((got["provider"], got["folder"]), ("opencode", str(work)))
        self.assertIn("hello", [c["name"] for c in got["commands"]])

    def test_an_id_of_another_agent_is_no_opencode_id_and_reads_nothing(self):
        with mock.patch.object(opencode, "export_lines") as export:
            for other in ("7f3c2a10-5b9e-4c1d-9a2f-0e6d4b8c1a21", "T-12345678", "ses_x"):
                self.assertIsNone(self.a.chat_path(other))
            export.assert_not_called()

    def test_root_replaces_opencodes_own_database_folder(self):
        db = self.make_db([("session.jsonl", SESSION_ID, None)])
        moved = self.root / "elsewhere"
        db.parent.rename(moved)
        self.assertIsNone(self.a.chat_path(SESSION_ID))
        self.assertTrue(self.a.chat_path(SESSION_ID, None, str(moved)).is_file())

    def test_one_line_at_a_time_reads_as_the_whole_file(self):
        rows = [json.loads(ln) for ln in lines()]
        got = [e for r in rows for e in opencode.parse_chat_line(r)[0]]
        self.assertEqual(got, self.a.parse_chat(lines())[0])
        self.assertEqual(opencode.parse_chat_line(rows[0]), ([], None))  # the header
        self.assertEqual(opencode.parse_chat_line(rows[-1])[1], "idle")
        failed = [json.loads(ln) for ln in lines("failed.jsonl")]
        self.assertEqual([e["role"] for r in failed for e in opencode.parse_chat_line(r)[0]], ["user", "event"])

    def test_an_unknown_session_or_no_database_has_no_file(self):
        self.assertIsNone(self.a.chat_path(SESSION_ID))
        self.make_db([("session.jsonl", SESSION_ID, None)])
        self.assertIsNone(self.a.chat_path("ses_unknown00000"))
        self.assertIsNone(self.a.chat_path("../x"))
        self.assertIsNone(self.a.chat_path(""))

    def test_the_chain_follows_forks_back_to_the_first_session(self):
        self.make_db([("session.jsonl", SESSION_ID, None), ("failed.jsonl", "ses_fork00000001", SESSION_ID)])
        p = self.a.chat_path("ses_fork00000001")
        self.assertEqual([q.name for q in opencode.chat_chain(p)], [f"{SESSION_ID}.jsonl", "ses_fork00000001.jsonl"])


class State(Env):
    def test_install_hooks_writes_the_plugin_beside_the_queue_and_returns_an_env_prefix(self):
        report = ["maxpm", "--as", "w1", "report"]
        words = self.a.install_hooks("/work/demo", report)
        d = Path(core.river_dir()) / "opencode" / opencode.PLUGIN_DIR
        self.assertEqual(words, ["env", 'OPENCODE_CLI_CONFIG_CONTENT={"plugins":["%s"]}' % d])
        text = (d / "tui.js").read_text(encoding="utf-8")
        self.assertIn('const REPORT = ["maxpm", "--as", "w1", "report"];', text)
        self.assertIn('"opencode", "--standalone", "--session"]', text)
        before = (d / "tui.js").stat().st_mtime_ns
        self.assertEqual(self.a.install_hooks("/other", report), words)
        self.assertEqual((d / "tui.js").stat().st_mtime_ns, before)  # the same text is not written again
        self.assertFalse((self.root / "oc-config").exists())  # nothing in the person's own config folder
        self.assertFalse((self.root / "home" / ".config").exists())

    def test_the_persons_own_cli_plugins_stay_loaded(self):
        # The plugin list of the environment replaces that of cli.json, so the start names the person's own again.
        cfg = self.root / "oc-config"
        cfg.mkdir()
        (cfg / "cli.json").write_text('{\n  // my plugins\n  "plugins": ["./mine", "https://x.test/a//b"],\n'
                                      '  "theme": "dark", /* a note */\n}\n', encoding="utf-8")
        words = self.a.install_hooks("/work/demo", ["maxpm", "report"])
        got = json.loads(words[1].split("=", 1)[1])["plugins"]
        self.assertEqual(got[:2], ["./mine", "https://x.test/a//b"])
        self.assertTrue(got[2].endswith("opencode/maxpm-state"))

    @unittest.skipUnless(shutil.which("node"), "no node")
    def test_the_plugin_reports_the_recorded_run_as_the_real_one_did(self):
        """Run the plugin in node with a stand-in for OpenCode's TUI API, fire the events that a probe plugin recorded
        in a real run at it, and compare its maxpm commands with those of the real run (reports.txt). The plugin starts
        each command as a detached process and never waits for it, so the order in which they finish varies with
        the load (#5228): the harness records each spawn in the order the plugin makes it, and the file the stand-in
        maxpm writes shows only that every one of them ran."""
        out = self.root / "calls.txt"
        report = ["/bin/sh", "-c", f'printf "%s\\n" "$*" >> {out}', "sh", "--as", "oc1", "report"]
        self.a.install_hooks("/work/demo", report)
        plugin = Path(core.river_dir()) / "opencode" / opencode.PLUGIN_DIR / "tui.js"
        harness = self.root / "harness.mjs"
        harness.write_text("""
            import fs from "node:fs";
            import cp from "node:child_process";
            import { syncBuiltinESMExports } from "node:module";
            const issued = [], spawn = cp.spawn;
            cp.spawn = (cmd, args, opts) => { issued.push([cmd, ...args]); return spawn(cmd, args, opts); };
            syncBuiltinESMExports();  // the plugin's own import { spawn } sees the wrapper
            const plugin = (await import(process.argv[2])).default;
            const events = fs.readFileSync(process.argv[3], "utf8").trim().split("\\n").map((l) => JSON.parse(l));
            let route = { type: "home" }, status, listener;
            const api = {
              data: { listen: (fn) => { listener = fn; return () => { listener = undefined; }; },
                      session: { get: () => undefined, status: () => status } },
              ui: { router: { current: () => route } },
            };
            const dispose = plugin.setup(api);
            for (const e of events) {
              route = e.route ?? route;
              status = e.status;
              listener?.({ details: { type: e.type, data: e.data } });
              await new Promise((r) => setTimeout(r, 5));
            }
            dispose?.();
            console.log(JSON.stringify(issued));
        """, encoding="utf-8")
        env = {**os.environ, "MAXPM_AGENT": "oc1"}
        run = subprocess.run(["node", str(harness), plugin.as_uri(), str(FIX / "events.jsonl")], capture_output=True,
                             text=True, env=env, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        # The words after the stand-in's "sh", split as the stand-in prints them ("$*").
        calls = [" ".join(argv[4:]).split() for argv in json.loads(run.stdout)]
        real = [ln.split() for ln in (FIX / "reports.txt").read_text(encoding="utf-8").splitlines()]

        def states(cs):
            return [c[c.index("--state") + 1] if "--state" in c else c[2] for c in cs]

        self.assertEqual(states(calls), states(real))
        self.assertEqual(states(real), ["idle", "working", "blocked", "working", "idle", "release"])
        self.assertEqual(calls[-1], ["--as", "oc1", "release"])
        reports = [c for c in calls if "--seq" in c]
        seqs = [int(c[c.index("--seq") + 1]) for c in reports]
        self.assertEqual(seqs, sorted(set(seqs)))  # each report's seq is above the last: a late one changes nothing
        for _ in range(100):  # each command runs in a process of its own, in no fixed order
            if out.exists() and len(out.read_text(encoding="utf-8").splitlines()) >= len(calls):
                break
            time.sleep(0.1)
        self.assertEqual(sorted(ln.split() for ln in out.read_text(encoding="utf-8").splitlines()), sorted(calls))
        blocked = next(c for c in reports if "blocked" in c)
        msg = blocked[blocked.index("--message") + 1: blocked.index("--session-id")]
        self.assertEqual(msg, ["permission:", "read", ".env"])  # 2.2.7 names the file in 'resources'
        sid = "ses_e365c17cbffe7O4FB3OeQtl7r8"
        for c in reports:
            self.assertEqual(c[c.index("--session-id") + 1], sid)
            self.assertEqual(c[-4:], ["opencode", "--standalone", "--session", sid])
        # The session that the pane showed had a listener, which the dispose ended (#5702).
        self.assertTrue((core.wake_dir(sid) / "owner").exists())
        self.assertFalse((core.wake_dir(sid) / "alive").exists())
        out2 = subprocess.run(["node", str(harness), plugin.as_uri(), str(FIX / "events.jsonl")], capture_output=True,
                              text=True, timeout=60, env={k: v for k, v in env.items() if k != "MAXPM_AGENT"})
        self.assertEqual(out2.returncode, 0, out2.stderr)
        self.assertEqual(json.loads(out2.stdout), [])
        time.sleep(0.3)  # timing: a check that no report came; a slow machine can only make it pass
        self.assertEqual(len(out.read_text(encoding="utf-8").splitlines()), len(calls))  # not started by MaximizePM: no report

    @unittest.skipUnless(shutil.which("node"), "no node")
    def test_the_plugin_brings_a_message_into_the_session_that_the_pane_shows(self):
        """#5702: the plugin listens in the folder of the session that the pane shows (the hash of core.wake_dir); a
        message that a sender writes there goes to OpenCode's server with client.session.prompt, delivery steer."""
        # A maxpm that is not there: its failed start must not end the plugin's process.
        self.a.install_hooks("/work/demo", [os.path.join(self.root, "no-such-maxpm"), "report"])
        plugin = Path(core.river_dir()) / "opencode" / opencode.PLUGIN_DIR / "tui.js"
        harness = self.root / "listen.mjs"
        harness.write_text("""
            const plugin = (await import(process.argv[2])).default;
            const sent = [];
            const api = {
              data: { listen: () => () => {}, session: { get: () => undefined, status: () => "idle" } },
              ui: { router: { current: () => ({ type: "session", sessionID: "ses_1" }) } },
              client: { session: { prompt: (body) => { sent.push(body); return Promise.resolve({}); } } },
            };
            const dispose = plugin.setup(api);
            const t0 = Date.now();
            while (!sent.length && Date.now() - t0 < 20000) await new Promise((r) => setTimeout(r, 50));
            dispose?.();
            console.log(JSON.stringify(sent));
        """, encoding="utf-8")
        p = subprocess.Popen(["node", str(harness), plugin.as_uri()], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, env={**os.environ, "MAXPM_AGENT": "oc1"})
        self.addCleanup(lambda: p.poll() is None and p.kill())
        alive = core.wake_dir("ses_1") / "alive"
        for _ in range(200):
            if alive.exists() or p.poll() is not None:
                break
            time.sleep(0.05)
        self.assertTrue(alive.exists(), p.poll() is not None and p.stderr.read())
        self.assertEqual(core._wake_send("ses_1", "[maxpm alert from mark] the build is red"), (0, ""))
        out, err = p.communicate(timeout=30)
        self.assertEqual(json.loads(out.strip().splitlines()[-1]),
                         [{"sessionID": "ses_1", "text": "[maxpm alert from mark] the build is red", "delivery": "steer"}],
                         err)
        self.assertFalse(alive.exists())


if __name__ == "__main__":
    unittest.main()
