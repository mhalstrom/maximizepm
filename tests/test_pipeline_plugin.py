"""Pipeline plugins (#6159): the manifest, the process call (time limit, crash, bad output), poll, the actions, and
the actions a source does not list."""

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from river import cli, core
from river.core import RiverError
from river.pipeline import define, plugin, runner, store
from tests import timing

SHIPPED = os.path.join(os.path.dirname(plugin.__file__), "plugins")

# A plugin for the tests. Its setting 'mode' says how it behaves; each request is kept in requests.jsonl in its
# folder, so a test reads what MaximizePM sent (the secrets too: only a test plugin does this).
TEST_PLUGIN = r'''
import json, os, sys, time
req = json.load(sys.stdin)
with open("requests.jsonl", "a") as f:
    f.write(json.dumps({"req": req, "env": sorted(os.environ)}) + "\n")
mode = (req.get("settings") or {}).get("mode", "ok")
call = req["call"]
if call == "describe":
    m = json.load(open("plugin.json"))
    out = {k: m[k] for k in ("name", "version", "level", "actions")}
    if mode == "lies":
        out["version"] = "9.9"
    json.dump(out, sys.stdout); sys.exit(0)
if mode == "sleep":
    time.sleep(30)
if mode == "crash":
    sys.stderr.write("Traceback: the API said 401\n"); sys.exit(3)
if mode == "garbage":
    print("this is not json"); sys.exit(0)
if mode == "refuse":
    json.dump({"error": "the token is not valid"}, sys.stdout); sys.exit(1)
def ev(pred, run, stage, n, outcome=None, link=None):
    c = {"pipelineName": stage}
    if outcome:
        c["outcome"] = outcome
    m = {"stage": stage, "version": "abc1234"}
    if link is not None:
        m["run"] = link
    return {"context": {"version": "0.4.1", "id": f"e{n}", "source": "anything",
                        "type": f"dev.cdevents.pipelinerun.{pred}.0.2.0", "timestamp": "2026-11-18T10:00:0%dZ" % n},
            "subject": {"id": run, "type": "pipelineRun", "content": c}, "customData": {"maxpm": m}}
if call == "poll":
    n = (req.get("state") or {}).get("n", 0)
    events = []
    if n == 0:
        events = [ev("started", "x-1", "build", 1), ev("finished", "x-1", "build", 2, "success")]
    waiting = [r for r in req.get("runs", []) if r["state"] == "queued" and r["stage"] == "deploy"]
    if mode == "link" and waiting:
        events.append(ev("started", "x-2", "deploy", 3, link=waiting[0]["id"]))
    json.dump({"events": events, "state": {"n": n + 1}}, sys.stdout); sys.exit(0)
json.dump({"key": "x-run-" + str(req["run"]["id"]), "url": "https://ci.example/run", "note": call + " asked"},
          sys.stdout)
'''


def manifest(name="tci", level="act", actions=("start", "stop"), **extra):
    m = {"contract": 1, "name": name, "version": "1.2.0", "level": level, "actions": list(actions),
         "run": ["{python}", "plugin.py"], "description": "a plugin for the tests",
         "settings": {"mode": {"help": "how it behaves", "default": "ok"}},
         "secrets": {"token": {"help": "the API token"}}, "timeout": 3}
    m.update(extra)
    return m


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "t.db")
        env = mock.patch.dict(os.environ, {"MAXPM_DB": self.path, "MAXPM_SECRET_FOR_TEST": "x"})
        env.start()
        self.addCleanup(env.stop)
        self.c = core.connect(self.path)
        self.addCleanup(self.c.close)
        self.plugins = os.path.join(self.dir.name, "plugins")
        os.makedirs(self.plugins)
        old = plugin.DIRS
        plugin.DIRS = [SHIPPED, self.plugins]
        self.addCleanup(setattr, plugin, "DIRS", old)
        plugin._SECRETS.clear()
        self.addCleanup(plugin._SECRETS.clear)
        get = mock.patch("river.vault.get", lambda conn, name, **kw: f"value-of-{name}".encode())
        get.start()
        self.addCleanup(get.stop)
        runner.SYNC["on"] = True
        self.addCleanup(runner.SYNC.update, on=False)
        core.project_add(self.c, "app")
        core.register(self.c, "mark", human=True)
        define.pipeline_add(self.c, "app", project="app")
        define.env_set(self.c, "app", "prod")
        define.stage_set(self.c, "app", "build", "build", workflow="build-job")
        define.stage_set(self.c, "app", "deploy", "deploy", env="prod", workflow="deploy-job")

    def install(self, m, code=TEST_PLUGIN, folder=None):
        folder = os.path.join(self.plugins, folder or m["name"])
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "plugin.json"), "w") as f:
            json.dump(m, f)
        with open(os.path.join(folder, "plugin.py"), "w") as f:
            f.write(code)
        return folder

    def requests(self, name="tci"):
        path = os.path.join(self.plugins, name, "requests.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(x) for x in f]

    def use(self, mode="ok", name="tci"):
        return define.forge_set(self.c, "app", name, settings={"mode": mode}, secrets=["token=ci-token"])

    def pipeline(self):
        return self.c.execute("SELECT * FROM pipelines WHERE name='app'").fetchone()

    def runs(self):
        return {r["stage"]: r for r in self.c.execute(
            "SELECT r.*, s.name stage FROM stage_runs r JOIN pipeline_stages s ON s.id=r.stage_id ORDER BY r.id")}

    def due(self):
        with core.tx(self.c):
            self.c.execute("UPDATE pipelines SET polled_at=NULL")


class Manifests(Base):
    def test_the_shipped_example_and_a_plugin_of_the_user(self):
        self.install(manifest())
        rows = {p["name"]: p for p in plugin.listing()}
        self.assertEqual(rows["jsonl"]["level"], "act")
        self.assertEqual(rows["jsonl"]["actions"], ["start", "stop"])
        self.assertEqual(rows["tci"]["secrets"], ["token"])
        out = plugin.describe("tci")
        self.assertEqual(out["describe"]["version"], "1.2.0")

    def test_bad_manifests_say_what_is_wrong(self):
        cases = [({"contract": 2}, "contract"), ({"name": "other"}, "name of its folder"),
                 ({"level": "watch"}, "level"), ({"actions": ["deploy"]}, "actions"),
                 ({"level": "report"}, "lists no actions"), ({"actions": []}, "lists the actions"),
                 ({"run": "python3 plugin.py"}, "list of words"), ({"timeout": 900}, "timeout")]
        for i, (change, words) in enumerate(cases):
            m = manifest(name=f"bad{i}")
            m.update(change)
            self.install(m, folder=f"bad{i}")
            with self.assertRaises(RiverError) as e:
                plugin.find(f"bad{i}")
            self.assertIn(words, str(e.exception))
        rows = {p["name"]: p for p in plugin.listing()}
        self.assertIn("error", rows["bad0"])
        with self.assertRaises(RiverError) as e:
            plugin.find("github")
        self.assertIn("no plugin", str(e.exception))

    def test_a_reserved_name_is_refused(self):
        m = manifest(name="maxpm")
        self.install(m)
        self.assertIsNone(plugin.find("maxpm", need=False))
        self.assertIn("MaximizePM's own", plugin.listing()[-1]["error"])

    def test_describe_must_match_the_manifest(self):
        self.install(manifest(), code=TEST_PLUGIN.replace('if mode == "lies":', 'if True:'))
        with self.assertRaises(RiverError) as e:
            define.forge_set(self.c, "app", "tci", secrets=["ci-token"])
        self.assertIn("plugin tci: its describe differs from its plugin.json in version", str(e.exception))


class Use(Base):
    def test_settings_and_secrets_are_checked_and_kept(self):
        self.install(manifest(settings={"repo": {"required": True, "help": "owner/name"}}))
        with self.assertRaises(RiverError) as e:
            define.forge_set(self.c, "app", "tci", secrets=["ci-token"])
        self.assertIn("--set repo=<owner/name>", str(e.exception))
        with self.assertRaises(RiverError) as e:
            define.forge_set(self.c, "app", "tci", settings={"colour": "red"}, repo="o/r", secrets=["ci-token"])
        self.assertIn("no setting colour", str(e.exception))
        with self.assertRaises(RiverError) as e:
            define.forge_set(self.c, "app", "tci", repo="o/r")
        self.assertIn("--secret token=<maxpm secret: the API token>", str(e.exception))
        res = define.forge_set(self.c, "app", "tci", repo="o/r", secrets=["ci-token"])
        self.assertEqual(res["secrets"], {"token": "ci-token"})
        p = self.pipeline()
        self.assertEqual((p["forge"], p["repo"], p["secret"]), ("tci", "o/r", "ci-token"))
        self.assertIn("source plugin tci 1.2.0 (act: start, stop), repo=o/r, secret token from ci-token",
                      define.history(self.c, "app")[0]["change"])
        self.assertEqual(define.export(self.c, "app")["forge"]["plugin"]["secrets"], {"token": "ci-token"})
        define.forge_set(self.c, "app", "none")
        self.assertIsNone(self.pipeline()["plugin_settings"])

    def test_an_agent_may_not_connect_a_plugin(self):
        self.install(manifest())
        core.register(self.c, "w1")
        with self.assertRaises(RiverError):
            define.forge_set(self.c, "app", "tci", secrets=["ci-token"], actor="w1")
        self.assertEqual(self.requests(), [])  # refused before the plugin ran

    def test_the_cli(self):
        self.install(manifest())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(["-q", "pipeline", "plugins"])
            cli.run(["-q", "pipeline", "forge", "app", "tci", "--set", "mode=ok", "--secret", "token=ci-token"])
            cli.run(["-q", "pipeline", "plugin", "jsonl"])
        text = out.getvalue()
        self.assertIn("jsonl 1.0.0  level act  actions: start, stop", text)
        self.assertIn("pipeline app: its runs come from plugin tci 1.2.0 (act; actions: start, stop)", text)
        self.assertIn("setting file (required): the path of the file of run lines", text)


class Calls(Base):
    def test_poll_takes_the_events_keeps_the_state_and_hands_the_version_on(self):
        self.install(manifest())
        self.use()
        res = runner.serve_pass(self.c)
        p = self.pipeline()
        self.assertIsNone(p["poll_error"])
        self.assertEqual(json.loads(p["plugin_state"]), {"n": 1})
        runs = self.runs()
        self.assertEqual(runs["build"]["state"], "passed")
        self.assertEqual(runs["build"]["source"], "tci")
        # Handed on, and its gate (auto) let the plugin start it; it runs when the plugin's events say so.
        self.assertEqual(runs["deploy"]["state"], "queued")
        self.assertEqual(runs["deploy"]["gate_note"], "start asked")
        self.assertEqual(runs["deploy"]["url"], "https://ci.example/run")
        self.assertEqual(runs["deploy"]["runner"], "forge:tci")
        self.assertEqual(runs["deploy"]["key"], f"x-run-{runs['deploy']['id']}")
        self.assertEqual(res["started"], [runs["deploy"]["id"]])
        ev = self.c.execute("SELECT source, event_id FROM pipeline_events ORDER BY id LIMIT 1").fetchone()
        self.assertEqual((ev["source"], ev["event_id"]), ("tci", "app:e1"))  # the plugin's own, once per pipeline
        reqs = [r["req"] for r in self.requests()]
        poll, start = reqs[1], reqs[2]  # describe at forge, then poll, then start
        self.assertEqual(poll["call"], "poll")
        self.assertEqual(poll["contract"], 1)
        self.assertEqual(poll["secrets"], {"token": "value-of-ci-token"})
        self.assertEqual([s["work"] for s in poll["pipeline"]["stages"]], ["build-job", "deploy-job"])
        self.assertEqual(start["call"], "start")
        self.assertEqual(start["run"]["stage"], "deploy")
        self.assertEqual(start["run"]["version"], "abc1234")
        env = self.requests()[1]["env"]
        self.assertNotIn("MAXPM_DB", env)
        self.assertNotIn("MAXPM_SECRET_FOR_TEST", env)
        self.assertIn("MAXPM_PLUGIN_CONTRACT", env)
        # The next pass polls only after poll_every; the same events again change nothing.
        n = len(self.requests())
        runner.serve_pass(self.c)
        self.assertEqual(len(self.requests()), n)
        self.due()
        runner.serve_pass(self.c)
        self.assertEqual(json.loads(self.pipeline()["plugin_state"]), {"n": 2})

    def test_a_report_plugin_links_a_waiting_run_and_starts_nothing(self):
        self.install(manifest(name="watch", level="report", actions=[]))
        define.forge_set(self.c, "app", "watch", settings={"mode": "link"}, secrets=["ci-token"])
        runner.serve_pass(self.c)
        runs = self.runs()
        self.assertEqual(runs["deploy"]["runner"], "forge:watch")
        self.assertIn("starts it in its own system", runs["deploy"]["gate_note"])
        self.assertNotIn("start", [r["req"]["call"] for r in self.requests("watch")])
        self.due()
        runner.serve_pass(self.c)
        self.assertEqual(self.runs()["deploy"]["state"], "running")  # its poll linked the run through maxpm.run
        self.assertEqual(self.runs()["deploy"]["key"], "x-2")

    def test_a_hang_a_crash_bad_output_and_a_refusal_stay_on_the_pipeline(self):
        self.install(manifest(timeout=1))
        for mode, words in (("sleep", "ran past its time limit of 1s"), ("crash", "exit 3 on poll: Traceback: the "
                            "API said 401"), ("garbage", "is not one JSON object"),
                            ("refuse", "plugin tci: the token is not valid")):
            self.use("ok")
            with core.tx(self.c):
                self.c.execute("UPDATE pipelines SET plugin_settings=? WHERE name='app'",
                               (json.dumps({"settings": {"mode": mode}, "secrets": {"token": "ci-token"}}),))
            self.due()
            t = time.monotonic()
            runner.serve_pass(self.c)  # never raises
            self.assertLess(time.monotonic() - t, timing.limit(5, below=30))  # the plugin's sleep is 30s
            self.assertIn(words, self.pipeline()["poll_error"], mode)

    def test_a_plugin_that_is_gone(self):
        folder = self.install(manifest())
        self.use()
        os.rename(folder, folder + "-old")
        self.due()
        runner.serve_pass(self.c)
        self.assertIn("no plugin 'tci' is installed", self.pipeline()["poll_error"])
        self.assertEqual(store.show(self.c, "app")["source"]["kind"], "missing")


class Actions(Base):
    def test_actions_the_plugin_does_not_list_are_not_available(self):
        self.install(manifest())
        self.use()
        src = store.show(self.c, "app")["source"]
        self.assertEqual((src["kind"], src["actions"], src["level"]), ("plugin", ["start", "stop"], "act"))
        for fn in (lambda: runner.run_again(self.c, "app", "build", "abc1234"),
                   lambda: runner.rollback(self.c, "app", "prod", "abc1234", reason="bad")):
            with self.assertRaises(RiverError) as e:
                fn()
            self.assertIn("is not available for this source: plugin tci lists only start, stop", str(e.exception))
        # Refused before the roll back blocks the transition into the deploy stage.
        self.assertFalse(self.c.execute("SELECT 1 FROM pipeline_transitions WHERE block_reason IS NOT NULL").fetchone())
        # A stage with a command of its own keeps every action.
        define.stage_set(self.c, "app", "build", run="make", workflow="")
        runner.run_again(self.c, "app", "build", "abc1234")

    def test_stop_goes_to_the_plugin(self):
        self.install(manifest())
        self.use()
        runner.serve_pass(self.c)
        run = self.runs()["deploy"]["id"]
        res = runner.stop(self.c, run)
        self.assertEqual(res["stopped"], "by plugin tci")
        stop = self.requests()[-1]["req"]
        self.assertEqual((stop["call"], stop["run"]["id"]), ("stop", run))
        self.assertIn("stopped run", define.history(self.c, "app")[0]["change"])

    def test_stop_is_refused_when_the_plugin_does_not_list_it(self):
        self.install(manifest(actions=["start"]))
        self.use()
        runner.serve_pass(self.c)
        with self.assertRaises(RiverError) as e:
            runner.stop(self.c, self.runs()["deploy"]["id"])
        self.assertIn("stop is not available for this source", str(e.exception))

    def test_built_in_adapters_and_commands_here(self):
        self.assertEqual(store.show(self.c, "app")["source"]["kind"], "here")
        define.forge_set(self.c, "app", "pulumi", repo="o/p/s", secret="pd")
        self.assertNotIn("rollback", store.show(self.c, "app")["source"]["actions"])


class Jsonl(Base):
    """The shipped example plugin, end to end, as a person sets it up."""

    def test_lines_become_runs_and_start_writes_a_request(self):
        lines = os.path.join(self.dir.name, "runs.jsonl")
        asks = os.path.join(self.dir.name, "requests.jsonl")
        with open(lines, "w") as f:
            f.write(json.dumps({"run": "n-17", "stage": "build", "version": "3f2a9c1", "state": "running",
                                "at": "2026-11-18T10:00:00Z"}) + "\n")
            f.write(json.dumps({"run": "n-17", "stage": "build", "version": "3f2a9c1", "state": "passed",
                                "at": "2026-11-18T10:04:00Z"}) + "\n")
        define.forge_set(self.c, "app", "jsonl", settings={"file": lines, "requests": asks})
        runner.serve_pass(self.c)
        self.assertIsNone(self.pipeline()["poll_error"])
        runs = self.runs()
        self.assertEqual((runs["build"]["state"], runs["build"]["key"]), ("passed", "n-17"))
        deploy = runs["deploy"]
        with open(asks) as f:
            ask = json.loads(f.readline())
        self.assertEqual((ask["request"], ask["maxpm_run"], ask["work"], ask["version"]),
                         ("start", deploy["id"], "deploy-job", "3f2a9c1"))
        with open(lines, "a") as f:
            f.write(json.dumps({"run": "d-1", "stage": "deploy", "version": "3f2a9c1", "state": "passed",
                                "maxpm_run": deploy["id"], "at": "2026-11-18T10:09:00Z"}) + "\n")
        self.due()
        runner.serve_pass(self.c)
        self.assertEqual(self.runs()["deploy"]["state"], "passed")
        self.assertEqual(store.show(self.c, "app")["envs"][0]["rev"], "3f2a9c1")  # a deploy stage records it


if __name__ == "__main__":
    unittest.main()
