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

from river import agents, core
from river.agents import opencode

FIX = Path(__file__).resolve().parent / "fixtures" / "opencode"
SESSION_ID = "ses_e366d1e13ffe0SgSDgPP6MNKXx"


def lines(name="session.jsonl"):
    return (FIX / name).read_text().splitlines()


def screen(name):
    return (FIX / f"pane-{name}.txt").read_text()


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


class Commands(Env):
    def test_the_built_in_list_is_the_recorded_slash_list(self):
        self.assertEqual([c["name"] for c in opencode.COMMANDS], [w.lstrip("/") for w in (FIX / "commands.txt").read_text().split()])
        self.assertIn("compact", [c["name"] for c in self.a.read_commands(None)])

    def test_custom_commands_of_the_folder_up_to_git_and_of_the_global_config(self):
        work = self.root / "repo" / "sub"
        (self.root / "repo" / ".git").mkdir(parents=True)
        d = self.root / "repo" / ".opencode" / "commands" / "deep"
        d.mkdir(parents=True)
        (d.parent / "hello.md").write_text("---\ndescription: Say hello\nargument-hint: <name>\n---\nHi $ARGUMENTS\n")
        (d / "x.md").write_text("Nested one\n")
        work.mkdir(parents=True)
        g = self.root / "oc-config" / "command"
        g.mkdir(parents=True)
        (g / "hello.md").write_text("---\ndescription: the global one\n---\n")
        (g / "ship.md").write_text("---\ndescription: Ship it\n---\n")
        (g / "compact.md").write_text("a custom command cannot take a built-in name\n")
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
        got = [json.loads(ln) for ln in p.read_text().splitlines()]
        want = [json.loads(ln) for ln in lines()]
        self.assertEqual([{k: v for k, v in r.items() if k != "id"} for r in got],
                         [{k: v for k, v in r.items() if k != "id"} for r in want])
        self.assertEqual(self.a.parse_chat(p.read_text().splitlines())[0], self.a.parse_chat(lines())[0])
        before = p.stat().st_mtime_ns
        self.assertEqual(self.a.chat_path(SESSION_ID), p)
        self.assertEqual(p.stat().st_mtime_ns, before)  # the database did not change
        time.sleep(0.01)
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO session_message VALUES ('m9', ?, 'user', 99, 1, 1, ?)",
                     (SESSION_ID, json.dumps({"time": {"created": 1794383500000}, "text": "one more"})))
        conn.commit()
        conn.close()
        self.a.chat_path(SESSION_ID)
        entries, turn = self.a.parse_chat(p.read_text().splitlines())
        self.assertEqual((entries[-1]["text"], turn), ("one more", "working"))
        self.assertEqual(self.a.chat_path(str(p)), p)  # a path of such a file, as it is

    def test_an_unknown_session_or_no_database_has_no_file(self):
        self.assertIsNone(self.a.chat_path(SESSION_ID))
        self.make_db([("session.jsonl", SESSION_ID, None)])
        self.assertIsNone(self.a.chat_path("ses_unknown"))
        self.assertIsNone(self.a.chat_path("../x"))
        self.assertIsNone(self.a.chat_path(""))

    def test_the_chain_follows_forks_back_to_the_first_session(self):
        self.make_db([("session.jsonl", SESSION_ID, None), ("failed.jsonl", "ses_fork1", SESSION_ID)])
        p = self.a.chat_path("ses_fork1")
        self.assertEqual([q.name for q in opencode.chat_chain(p)], [f"{SESSION_ID}.jsonl", "ses_fork1.jsonl"])


class State(Env):
    def test_install_hooks_writes_the_plugin_beside_the_queue_and_returns_an_env_prefix(self):
        report = ["maxpm", "--as", "w1", "report"]
        words = self.a.install_hooks("/work/demo", report)
        d = Path(core.river_dir()) / "opencode" / opencode.PLUGIN_DIR
        self.assertEqual(words, ["env", 'OPENCODE_CLI_CONFIG_CONTENT={"plugins":["%s"]}' % d])
        text = (d / "tui.js").read_text()
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
                                      '  "theme": "dark", /* a note */\n}\n')
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
        """)
        env = {**os.environ, "MAXPM_AGENT": "oc1"}
        run = subprocess.run(["node", str(harness), plugin.as_uri(), str(FIX / "events.jsonl")], capture_output=True,
                             text=True, env=env, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        # The words after the stand-in's "sh", split as the stand-in prints them ("$*").
        calls = [" ".join(argv[4:]).split() for argv in json.loads(run.stdout)]
        real = [ln.split() for ln in (FIX / "reports.txt").read_text().splitlines()]

        def states(cs):
            return [c[c.index("--state") + 1] if "--state" in c else c[2] for c in cs]

        self.assertEqual(states(calls), states(real))
        self.assertEqual(states(real), ["idle", "working", "blocked", "working", "idle", "release"])
        self.assertEqual(calls[-1], ["--as", "oc1", "release"])
        reports = [c for c in calls if "--seq" in c]
        seqs = [int(c[c.index("--seq") + 1]) for c in reports]
        self.assertEqual(seqs, sorted(set(seqs)))  # each report's seq is above the last: a late one changes nothing
        for _ in range(100):  # each command runs in a process of its own, in no fixed order
            if out.exists() and len(out.read_text().splitlines()) >= len(calls):
                break
            time.sleep(0.1)
        self.assertEqual(sorted(ln.split() for ln in out.read_text().splitlines()), sorted(calls))
        blocked = next(c for c in reports if "blocked" in c)
        msg = blocked[blocked.index("--message") + 1: blocked.index("--session-id")]
        self.assertEqual(msg, ["permission:", "read", ".env"])  # 2.2.7 names the file in 'resources'
        sid = "ses_e365c17cbffe7O4FB3OeQtl7r8"
        for c in reports:
            self.assertEqual(c[c.index("--session-id") + 1], sid)
            self.assertEqual(c[-4:], ["opencode", "--standalone", "--session", sid])
        out2 = subprocess.run(["node", str(harness), plugin.as_uri(), str(FIX / "events.jsonl")], capture_output=True,
                              text=True, timeout=60, env={k: v for k, v in env.items() if k != "MAXPM_AGENT"})
        self.assertEqual(out2.returncode, 0, out2.stderr)
        self.assertEqual(json.loads(out2.stdout), [])
        time.sleep(0.3)
        self.assertEqual(len(out.read_text().splitlines()), len(calls))  # not started by MaximizePM: no report


if __name__ == "__main__":
    unittest.main()
