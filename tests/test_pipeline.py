"""The pipeline package: its records and the intake of events, '::step' lines and JUnit reports (#6112)."""

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from river import core
from river.core import RiverError
from river.pipeline import cdevents, define, junit, steps, store
from tests import timing


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "t.db")
        env = mock.patch.dict(os.environ, {"MAXPM_DB": self.path})
        env.start()
        self.addCleanup(env.stop)
        self.c = core.connect(self.path)
        self.addCleanup(self.c.close)

    def toolscaled(self):
        define.pipeline_add(self.c, "toolscaled", project="toolscaled", actor="mark")
        define.env_set(self.c, "toolscaled", "beta")
        define.env_set(self.c, "toolscaled", "prod", target="toolscaled-prod")
        define.stage_set(self.c, "toolscaled", "build", "build", run="make prod-release-prepare")
        define.stage_set(self.c, "toolscaled", "test", "test", run="make check-all-once")
        define.stage_set(self.c, "toolscaled", "beta", "deploy", env="beta", run="make deploy ENV=beta")
        define.stage_set(self.c, "toolscaled", "prod", "deploy", env="prod", run="make prod-release", gate="person")


class Definitions(Base):
    def test_the_tables_come_with_connect(self):
        names = {r[0] for r in self.c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertLessEqual({"pipelines", "pipeline_envs", "pipeline_stages", "pipeline_versions", "stage_runs",
                              "stage_steps", "test_suites", "test_results", "pipeline_deploys", "pipeline_events"},
                             names)

    def test_stages_in_order_with_their_environment_and_gate(self):
        self.toolscaled()
        define.stage_set(self.c, "toolscaled", "review", "review", pos=3)
        p = store.show(self.c, "toolscaled")
        self.assertEqual([s["name"] for s in p["stages"]], ["build", "test", "review", "beta", "prod"])
        prod = p["stages"][-1]
        self.assertEqual((prod["env"], prod["transition"]["mode"], prod["kind"]), ("prod", "yes", "deploy"))
        self.assertEqual([e["name"] for e in p["envs"]], ["beta", "prod"])
        self.assertEqual(p["envs"][1]["target"], "toolscaled-prod")

    def test_refusals(self):
        self.toolscaled()
        for call in (lambda: define.pipeline_add(self.c, "toolscaled"),
                     lambda: define.pipeline_add(self.c, "has space"),
                     lambda: define.stage_set(self.c, "toolscaled", "x", "dance"),
                     lambda: define.stage_set(self.c, "toolscaled", "x", "deploy"),
                     lambda: define.stage_set(self.c, "toolscaled", "x", "deploy", env="nowhere"),
                     lambda: define.stage_set(self.c, "toolscaled", "x", "build", gate="anyone"),
                     lambda: define.stage_set(self.c, "nope", "x", "build")):
            with self.assertRaises(RiverError):
                call()


class Commands(Base):
    """maxpm pipeline (#6113): add, env, stage, gate, show, export, history."""

    def setUp(self):
        super().setUp()
        core.register(self.c, "mark", human=True)
        core.register(self.c, "w1")

    def cli(self, *words, who="mark"):
        from river import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.dict(os.environ, {"MAXPM_DB": self.path, "MAXPM_QUIET": "1"}):
            code = cli.run(["--as", who, *words])
        return code, out.getvalue()

    def test_the_manager_writes_a_pipeline_with_commands(self):
        for words in (("add", "toolscaled", "--project", "toolscaled"),
                      ("env", "toolscaled", "beta"), ("env", "toolscaled", "prod", "--target", "toolscaled-prod"),
                      ("stage", "toolscaled", "build", "--kind", "build", "--run", "make prod-release-prepare"),
                      ("stage", "toolscaled", "test", "--kind", "test", "--run", "make check-all-once"),
                      ("stage", "toolscaled", "review", "--kind", "review"),
                      ("stage", "toolscaled", "beta", "--kind", "deploy", "--env", "beta", "--run", "make deploy ENV=beta"),
                      ("stage", "toolscaled", "prod", "--kind", "deploy", "--env", "prod", "--run", "make prod-release",
                       "--gate", "person mark"),
                      ("gate", "toolscaled", "prod", "auto", "after", "30m")):
            code, out = self.cli("pipeline", *words)
            self.assertEqual(code, 0, out)
        code, out = self.cli("pipeline", "show", "toolscaled")
        self.assertIn("5. prod  [deploy, env prod]  mode delay 30m  run: make prod-release", out)
        self.assertIn("env prod: no version yet  (target toolscaled-prod)", out)
        path = os.path.join(self.dir.name, "toolscaled-pipeline.json")
        self.cli("pipeline", "export", "toolscaled", "--file", path)
        with open(path) as f:
            exp = json.load(f)
        self.assertEqual([s["name"] for s in exp["stages"]], ["build", "test", "review", "beta", "prod"])
        self.assertEqual(exp["stages"][-1], {"name": "prod", "kind": "deploy", "run": "make prod-release",
                                             "workflow": None, "env": "prod", "junit": None, "waves": None,
                                             "check": None, "rollback": None, "wave_wait": None, "tier": None,
                                             "start": "auto", "after": None,
                                             "transition": {"mode": "delay 30m", "window": None, "freezes": [],
                                                            "fault_rule": False}})
        self.assertEqual(exp["environments"][1]["target"], "toolscaled-prod")
        code, out = self.cli("pipeline", "history", "toolscaled")
        lines = out.strip().split("\n")
        self.assertEqual(len(lines), 9)
        self.assertIn("mark: stage prod changed: mode yes mark -> delay 30m", lines[0])
        self.assertIn("mark: pipeline toolscaled added for project toolscaled", lines[-1])

    def test_an_agent_may_not_change_a_pipeline(self):
        define.pipeline_add(self.c, "web", actor="mark")
        with self.assertRaises(RiverError) as e:
            define.stage_set(self.c, "web", "build", "build", actor="w1")
        self.assertIn("only a person or the manager changes a pipeline", str(e.exception))
        with self.assertRaises(RiverError):  # the permission table, or define itself, refuses it
            self.cli("pipeline", "stage", "web", "build", "--kind", "build", who="w1")
        self.assertEqual(store.show(self.c, "web")["stages"], [])
        self.assertEqual(self.cli("pipeline", "show", "web", who="w1")[0], 0)  # it reads it
        core.register(self.c, "boss")
        self.c.execute("UPDATE agents SET role='manager' WHERE name='boss'")
        define.stage_set(self.c, "web", "build", "build", actor="boss")

    def test_stage_tiers_several_stages_may_share_one_and_a_trigger_names_one(self):
        """#6137: each stage may have a tier (alpha, beta, prod, or a name of the pipeline's own); two stages can be
        prod; the show, the export, the history and the trigger rules read it."""
        self.toolscaled()
        define.env_set(self.c, "toolscaled", "prod-b", actor="mark")
        define.stage_set(self.c, "toolscaled", "prod-b", "deploy", env="prod-b", tier="prod", actor="mark")
        for stage, tier in (("beta", "beta"), ("prod", "PROD"), ("test", "canary-1")):
            define.stage_set(self.c, "toolscaled", stage, tier=tier, actor="mark")
        tiers = {s["name"]: s["tier"] for s in store.show(self.c, "toolscaled")["stages"]}
        self.assertEqual(tiers, {"build": None, "test": "canary-1", "beta": "beta", "prod": "prod", "prod-b": "prod"})
        self.assertEqual(define.export(self.c, "toolscaled")["stages"][-1]["tier"], "prod")
        self.assertIn("stage prod changed: tier none -> prod", define.history(self.c, "toolscaled", 5)[1]["change"])
        for bad in ("Prod Region", "x" * 40, "-a"):
            with self.assertRaises(RiverError):
                define.stage_set(self.c, "toolscaled", "beta", tier=bad, actor="mark")
        define.stage_set(self.c, "toolscaled", "test", tier="", actor="mark")
        self.assertIsNone(store.show(self.c, "toolscaled")["stages"][1]["tier"])
        code, out = self.cli("pipeline", "stage", "toolscaled", "build", "--tier", "alpha")
        self.assertEqual(code, 0, out)
        code, out = self.cli("pipeline", "show", "toolscaled")
        self.assertIn("1. build  [build, tier alpha]", out)
        self.assertIn("5. prod-b  [deploy, env prod-b, tier prod]", out)
        from river.pipeline import triggers
        triggers.rule_add(self.c, "toolscaled", "stage-failed", does="find the cause", tier="prod", actor="mark")
        with self.assertRaises(RiverError) as e:  # no stage has it
            triggers.rule_add(self.c, "toolscaled", "stage-failed", does="x", tier="gamma", actor="mark")
        self.assertIn("its tiers: alpha, beta, prod", str(e.exception))
        self.assertEqual(triggers.rules(self.c, "toolscaled")["tiers"], ["alpha", "beta", "prod"])

    def test_change_move_remove_and_bring_back_a_stage(self):
        self.toolscaled()
        define.stage_set(self.c, "toolscaled", "test", pos=1, actor="mark")
        names = lambda: [s["name"] for s in store.show(self.c, "toolscaled")["stages"]]
        self.assertEqual(names(), ["test", "build", "beta", "prod"])
        define.stage_set(self.c, "toolscaled", "build", workflow="build.yml", actor="mark")
        s = store.show(self.c, "toolscaled")["stages"][1]
        self.assertEqual((s["run"], s["workflow"]), (None, "build.yml"))  # one or the other
        run = store.run_start(self.c, "toolscaled", "build")
        define.stage_set(self.c, "toolscaled", "build", remove=True, actor="mark")
        self.assertEqual(names(), ["test", "beta", "prod"])
        self.assertEqual([s["pos"] for s in store.show(self.c, "toolscaled")["stages"]], [1, 2, 3])
        self.assertEqual(store.run_show(self.c, run)["stage"], "build")  # its runs stay
        r = define.stage_set(self.c, "toolscaled", "build", pos=1, actor="mark")
        self.assertEqual((r["changed"], names()), ("added again", ["build", "test", "beta", "prod"]))
        with self.assertRaises(RiverError):  # nothing to change
            define.stage_set(self.c, "toolscaled", "build", actor="mark")
        with self.assertRaises(RiverError):  # a deploy stage keeps its environment
            define.stage_set(self.c, "toolscaled", "prod", env="", actor="mark")
        with self.assertRaises(RiverError):  # a stage deploys to it
            define.env_set(self.c, "toolscaled", "prod", remove=True, actor="mark")
        define.env_set(self.c, "toolscaled", "prod", url="https://toolscaled.com", actor="mark")
        self.assertEqual(store.show(self.c, "toolscaled")["envs"][1]["url"], "https://toolscaled.com")

    def test_modes_of_a_transition_and_the_words_of_the_old_gates(self):
        from river.pipeline import transitions as tr
        self.assertEqual([tr.parse_mode(t) for t in ("auto", "auto after 30m", "manager", "person", "person mark",
                                                     "yes", "yes mark", "yes manager", "delay 10m", "release",
                                                     "release every 2h", "forge")],
                         [("auto", None), ("delay", "30m"), ("yes", "manager"), ("yes", None), ("yes", "mark"),
                          ("yes", None), ("yes", "mark"), ("yes", "manager"), ("delay", "10m"), ("release", None),
                          ("release", "2h"), ("forge", None)])
        for bad in ("", "auto after", "auto after soon", "anyone", "person a b", "manager mark", "delay", "release 2h"):
            with self.assertRaises(RiverError):
                tr.parse_mode(bad)
        self.assertEqual([tr.mode_words(*m) for m in (("delay", "30m"), ("release", "2h"), ("yes", "mark"))],
                         ["delay 30m", "release every 2h", "yes mark"])


class Events(Base):
    def setUp(self):
        super().setUp()
        self.toolscaled()

    def ev(self, subject, predicate, sid, content=None, at=None, source="github", **maxpm):
        return cdevents.make(subject, predicate, sid, content, {"pipeline": "toolscaled", **maxpm}, source, at)

    def test_a_pipelinerun_makes_a_stage_run_of_a_version(self):
        r = store.ingest(self.c, self.ev("pipelinerun", "queued", "gh-run-7", {"pipelineName": "build"},
                                         at="2026-11-18T10:00:00Z", version="9c1e2a0"))
        store.ingest(self.c, self.ev("pipelinerun", "started", "gh-run-7", at="2026-11-18T10:01:00Z"))
        run = store.run_show(self.c, r["run"])
        self.assertEqual((run["stage"], run["state"], run["version"], run["started_at"]),
                         ("build", "running", "9c1e2a0", "2026-11-18T10:01:00Z"))
        store.ingest(self.c, self.ev("pipelinerun", "finished", "gh-run-7", {"outcome": "failure", "errors": "exit 2"},
                                     at="2026-11-18T10:09:30+00:00"))
        run = store.run_show(self.c, r["run"])
        self.assertEqual((run["state"], run["ended_at"], run["errors"]), ("failed", "2026-11-18T10:09:30Z", "exit 2"))
        # A late started does not undo the end.
        store.ingest(self.c, self.ev("pipelinerun", "started", "gh-run-7"))
        self.assertEqual(store.run_show(self.c, r["run"])["state"], "failed")

    def test_the_same_event_twice_changes_nothing(self):
        e = self.ev("pipelinerun", "started", "gh-run-8", {"pipelineName": "build"})
        a, b = store.ingest(self.c, e), store.ingest(self.c, json.dumps(e))
        self.assertTrue(b["duplicate"])
        self.assertEqual(a["run"], b["run"])
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM pipeline_events").fetchone()[0], 1)

    def test_a_stored_event_keeps_the_cdevents_form(self):
        store.ingest(self.c, self.ev("pipelinerun", "started", "gh-run-9", {"pipelineName": "build"}))
        e = store.events(self.c, pipeline="toolscaled")[0]
        self.assertEqual(e["type"], "dev.cdevents.pipelinerun.started.0.2.0")
        self.assertEqual(e["body"]["subject"]["type"], "pipelineRun")
        self.assertEqual(e["body"]["context"]["version"], cdevents.SPEC)
        self.assertEqual(store.events(self.c, after=e["id"]), [])

    def test_an_unknown_stage_is_added_by_the_event(self):
        r = store.ingest(self.c, self.ev("pipelinerun", "started", "x1", {"pipelineName": "lint"}, kind="check"))
        s = self.c.execute("SELECT * FROM pipeline_stages WHERE name='lint'").fetchone()
        self.assertEqual((s["kind"], s["created_by"], s["pos"]), ("check", "event", 5))
        self.assertIsNotNone(r["run"])

    def test_refusals_change_nothing(self):
        good = self.ev("pipelinerun", "started", "ok-1", {"pipelineName": "build"})
        bad = [{"context": {"type": "dev.cdevents.pipelinerun.started.0.2.0"}, "subject": {}},
               {"context": {"id": "1", "source": "s", "type": "com.example.thing"}, "subject": {}},
               "not json",
               cdevents.make("pipelinerun", "started", "x", {"pipelineName": "build"}, None, "s"),  # no pipeline
               self.ev("taskrun", "started", "t1", {"taskName": "x"}),  # no run
               self.ev("pipelinerun", "started", "x", {"pipelineName": "build"}, at="yesterday")]
        for b in bad:
            with self.assertRaises(RiverError):
                store.ingest(self.c, [good, b])
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM stage_runs").fetchone()[0], 0)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM pipeline_events").fetchone()[0], 0)

    def test_another_subject_is_kept_and_changes_no_record(self):
        r = store.ingest(self.c, self.ev("change", "merged", "pr-12", {"repository": {"id": "x"}}))
        self.assertIn("kept", r["note"])
        self.assertEqual(len(store.events(self.c)), 1)

    def test_taskruns_are_steps_of_their_run(self):
        run = store.run_start(self.c, "toolscaled", "build", version="9c1e2a0", key="gh-run-1",
                              at="2026-11-18T10:00:00Z")
        store.ingest(self.c, [
            self.ev("taskrun", "started", "job-1", {"taskName": "image build", "pipelineRun": {"id": "gh-run-1"}},
                    at="2026-11-18T10:00:05Z"),
            self.ev("taskrun", "finished", "job-1", {"taskName": "image build", "outcome": "success",
                                                     "pipelineRun": {"id": "gh-run-1"}}, at="2026-11-18T10:09:05Z")])
        s = store.run_show(self.c, run)["step_list"][0]
        self.assertEqual((s["name"], s["state"], s["seconds"]), ("image build", "passed", 540))

    def test_service_events_say_what_runs_where(self):
        store.ingest(self.c, self.ev("service", "deployed", "svc", {"environment": {"id": "prod"},
                                                                    "artifactId": "pkg:generic/toolscaled@a8fd2ca"},
                                     at="2026-11-17T14:41:00Z"))
        store.deployed(self.c, "toolscaled", "beta", "8d8d2fd", at="2026-11-18T13:20:00Z")
        store.deployed(self.c, "toolscaled", "beta", "8d8d2fd0123456789", kind="rolledback",
                       at="2026-11-18T12:00:00Z")  # older than the current one: history only
        envs = {e["name"]: e for e in store.show(self.c, "toolscaled")["envs"]}
        self.assertEqual((envs["prod"]["rev"], envs["prod"]["since"]), ("a8fd2ca", "2026-11-17T14:41:00Z"))
        # The short and the full id of one commit are one version, and the full id stays.
        self.assertEqual((envs["beta"]["rev"], envs["beta"]["since"]), ("8d8d2fd0123456789", "2026-11-18T13:20:00Z"))
        self.assertEqual([h["kind"] for h in envs["beta"]["history"]], ["rolledback", "deployed"])
        store.ingest(self.c, self.ev("environment", "created", "staging", {"name": "staging", "url": "https://s"}))
        self.assertIn("staging", [e["name"] for e in store.show(self.c, "toolscaled")["envs"]])

    def test_versions_in_each_stage(self):
        for stage, rev, state in (("build", "aaaaaaa", "passed"), ("test", "aaaaaaa", "failed"),
                                  ("build", "bbbbbbb", "passed")):
            rid = store.run_start(self.c, "toolscaled", stage, version=rev)
            store.run_finish(self.c, rid, state)
        vs = store.versions(self.c, "toolscaled")
        self.assertEqual([v["rev"] for v in vs], ["bbbbbbb", "aaaaaaa"])
        self.assertEqual({k: v["state"] for k, v in vs[1]["stages"].items()}, {"build": "passed", "test": "failed"})


class StepLines(Base):
    def setUp(self):
        super().setUp()
        self.toolscaled()
        self.run = store.run_start(self.c, "toolscaled", "prod", version="8d8d2fd", at="2026-11-18T14:40:00Z")

    def test_parse(self):
        self.assertIsNone(steps.parse("building images"))
        self.assertEqual(steps.parse("::step start deploy kind=deploy where=edge-1 parallel"),
                         {"op": "start", "name": "deploy", "kind": "deploy", "where": "edge-1", "parallel": True,
                          "state": None, "seconds": None, "at": None, "note": None})
        e = steps.parse("::step end image-build passed 9m02s")
        self.assertEqual((e["state"], e["seconds"]), ("passed", 542))
        e = steps.parse("::step end deploy failed readyz timed out")
        self.assertEqual((e["state"], e["note"]), ("failed", "readyz timed out"))
        self.assertEqual(steps.parse("::step end x")["state"], "passed")
        self.assertEqual(steps.parse("::step start x at=2026-11-18T14:41:00-08:00")["at"], "2026-11-18T22:41:00Z")

    def test_one_step_for_each_server_with_its_times_and_lines(self):
        out = ("release-20261118 8d8d2fd\n"
               "::step start deploy kind=deploy where=edge-1 at=2026-11-18T14:41:00Z\n"
               "pulling images\n"
               "::step start deploy kind=deploy where=cell-1 parallel at=2026-11-18T14:41:30Z\n"
               "cell-1 migrating\n"
               "::step end deploy where=edge-1 passed at=2026-11-18T14:45:00Z\n"
               "::step end deploy where=cell-1 failed readyz at=2026-11-18T14:50:00Z\n")
        self.assertEqual(steps.ingest_output(self.c, self.run, out), {"steps": 4, "lines": 3})
        st = {s["host"]: s for s in store.run_show(self.c, self.run)["step_list"]}
        self.assertEqual((st["edge-1"]["state"], st["edge-1"]["seconds"]), ("passed", 240))
        self.assertEqual((st["cell-1"]["state"], st["cell-1"]["seconds"], st["cell-1"]["note"], st["cell-1"]["parallel"]),
                         ("failed", 510, "readyz", 1))
        self.assertEqual(st["edge-1"]["output"], "pulling images")
        self.assertEqual(st["cell-1"]["output"], "cell-1 migrating")
        self.assertEqual(store.run_show(self.c, self.run)["output"].split("\n")[0], "release-20261118 8d8d2fd")
        types = [e["type"] for e in store.events(self.c)]
        self.assertEqual(types.count("dev.cdevents.taskrun.started.0.2.0"), 2)
        self.assertEqual(types.count("dev.cdevents.taskrun.finished.0.2.0"), 2)

    def test_output_in_pieces_and_an_open_step_when_the_run_ends(self):
        steps.ingest_output(self.c, self.run, "::step start build\n", at="2026-11-18T14:41:00Z")
        steps.ingest_output(self.c, self.run, "::step end build passed\n::step start smoke\n",
                            at="2026-11-18T14:43:00Z")
        store.run_finish(self.c, self.run, "failed", errors="exit 1", at="2026-11-18T14:44:00Z")
        st = {s["name"]: s for s in store.run_show(self.c, self.run)["step_list"]}
        self.assertEqual((st["build"]["state"], st["build"]["seconds"]), ("passed", 120))
        self.assertEqual((st["smoke"]["state"], st["smoke"]["seconds"]), ("stopped", 60))

    def test_an_end_line_with_no_start(self):
        steps.ingest_output(self.c, self.run, "::step end preview passed 1m31s", at="2026-11-18T14:45:00Z")
        s = store.run_show(self.c, self.run)["step_list"][0]
        self.assertEqual((s["started_at"], s["ended_at"], s["seconds"]),
                         ("2026-11-18T14:43:29Z", "2026-11-18T14:45:00Z", 91))

    def test_a_long_output_keeps_its_last_lines(self):
        steps.ingest_output(self.c, self.run, "\n".join(f"line {i}" for i in range(500)))
        out = store.run_show(self.c, self.run)["output"].split("\n")
        self.assertEqual((len(out), out[-1]), (store.OUTPUT_LINES, "line 499"))


def report(passed=51, failed=2, errors=1, skipped=0):
    """A JUnit report like pytest's, of passed + failed + errors + skipped tests in two suites."""
    cases = [f'<testcase classname="tests.test_api.Billing" name="test_ok_{i}" time="0.01"/>' for i in range(passed)]
    cases += [f'<testcase classname="tests.test_api.Webhooks" name="test_fail_{i}" time="1.5" file="tests/test_api.py">'
              f'<failure message="assert 2 == 3">Traceback\n  line {i}\nAssertionError</failure></testcase>'
              for i in range(failed)]
    cases += [f'<testcase classname="tests.test_api.Webhooks" name="test_error_{i}" time="0.2">'
              f'<error type="KeyError">KeyError: \'id\'</error></testcase>' for i in range(errors)]
    cases += [f'<testcase classname="tests.test_api.Billing" name="test_skip_{i}"><skipped message="needs stripe"/>'
              f'</testcase>' for i in range(skipped)]
    return ('<?xml version="1.0" encoding="utf-8"?>\n<!-- made by a test -->\n<testsuites name="pytest">'
            f'<testsuite name="pytest" tests="{len(cases)}" time="12.5">{"".join(cases)}</testsuite></testsuites>')


class JUnit(Base):
    def setUp(self):
        super().setUp()
        self.toolscaled()
        self.run = store.run_start(self.c, "toolscaled", "test", version="9c1e2a0")

    def test_54_tests_give_54_results_failed_first(self):
        counts = junit.ingest_junit(self.c, self.run, xml=report())
        self.assertEqual(counts, {"suites": 1, "tests": 54, "passed": 51, "failed": 2, "error": 1, "skipped": 0})
        ts = store.tests(self.c, self.run)
        self.assertEqual(len(ts), 54)
        self.assertEqual([t["state"] for t in ts[:3]], ["error", "failed", "failed"])
        t = ts[1]
        self.assertEqual((t["suite"], t["name"], t["seconds"], t["message"], t["file"]),
                         ("tests.test_api.Webhooks", "test_fail_0", 1.5, "assert 2 == 3", "tests/test_api.py"))
        self.assertIn("AssertionError", t["detail"])
        self.assertEqual(ts[0]["message"], "KeyError")
        run = store.run_show(self.c, self.run)
        self.assertEqual(run["tests"], {"passed": 51, "failed": 2, "error": 1})
        s = run["suites"][0]
        self.assertEqual((s["name"], s["state"], s["tests"], s["failed"], s["seconds"]), ("pytest", "failed", 54, 3, 12.5))

    def test_test_events_are_applied_and_not_kept(self):
        """#6520: test_suites and test_results are the record of a test event; pipeline_events keeps no copy."""
        before = self.c.execute("SELECT COUNT(*) FROM pipeline_events").fetchone()[0]
        junit.ingest_junit(self.c, self.run, xml=report())
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM pipeline_events").fetchone()[0], before)
        self.assertEqual(len(store.tests(self.c, self.run)), 54)
        # The same test event twice (a sender that sends again) writes the same row once.
        suite = self.c.execute("SELECT key FROM test_suites WHERE run_id=?", (self.run,)).fetchone()[0]
        ev = cdevents.make("testcaserun", "finished", "again", {"testCase": {"id": "x", "name": "test_again"},
                                                                  "testSuiteRun": {"id": suite}, "outcome": "fail"},
                           {"pipeline": "toolscaled", "run": self.run, "suite": "s"}, "ci")
        a, b = store.ingest(self.c, ev), store.ingest(self.c, ev)
        self.assertEqual((a["event"], b["event"], a["run"], b["run"]), (None, None, self.run, self.run))
        self.assertEqual(len(store.tests(self.c, self.run)), 55)
        self.assertEqual(store.run_show(self.c, self.run)["tests"], {"passed": 51, "failed": 3, "error": 1})

    def test_a_file_nested_suites_and_skipped(self):
        xml = ('<testsuite name="outer"><testsuite name="inner"><testcase name="a"/>'
               '<testcase name="b"><skipped/></testcase></testsuite><testcase name="c" time="1,234.5"/></testsuite>')
        path = os.path.join(self.dir.name, "report.xml")
        with open(path, "w") as f:
            f.write(xml)
        counts = junit.ingest_junit(self.c, self.run, path=path)
        self.assertEqual((counts["suites"], counts["passed"], counts["skipped"]), (2, 2, 1))
        ts = {t["name"]: t for t in store.tests(self.c, self.run)}
        self.assertEqual((ts["a"]["suite"], ts["c"]["suite"], ts["c"]["seconds"]), ("inner", "outer", 1234.5))
        self.assertEqual(ts["b"]["state"], "skipped")

    def test_the_same_tests_again_replace_their_results(self):
        junit.ingest_junit(self.c, self.run, xml=report(failed=2))
        junit.ingest_junit(self.c, self.run, xml=report(passed=53, failed=0, errors=1))
        self.assertEqual({t["state"] for t in store.tests(self.c, self.run, state="failed")}, {"failed"})
        self.assertEqual(len(store.tests(self.c, self.run)), 56)  # test_fail_0 and _1 keep their old result

    def test_refusals(self):
        for xml in ("<html/>", "not xml <", '<?xml version="1.0"?>\n<!DOCTYPE x [<!ENTITY a "aaaa">]><testsuite/>'):
            with self.assertRaises(RiverError):
                junit.ingest_junit(self.c, self.run, xml=xml)
        with self.assertRaises(RiverError):
            junit.ingest_junit(self.c, self.run, path=os.path.join(self.dir.name, "missing.xml"))
        # A DOCTYPE word inside a failure's text is no DOCTYPE.
        ok = '<testsuite name="s"><testcase name="t"><failure>&lt;!DOCTYPE html&gt; <![CDATA[<!DOCTYPE x>]]></failure></testcase></testsuite>'
        self.assertEqual(junit.ingest_junit(self.c, self.run, xml=ok)["failed"], 1)

    def test_test_events_from_an_adapter(self):
        """An adapter sends its own testsuiterun and testcaserun events; the cases find their run by the suite."""
        mk = lambda *a, **m: cdevents.make(*a, maxpm={"pipeline": "toolscaled", **m}, source="gitlab")
        key = self.c.execute("SELECT key FROM stage_runs WHERE id=?", (self.run,)).fetchone()[0]
        store.ingest(self.c, [
            mk("testsuiterun", "started", "suite-1", {"testSuite": {"id": "unit", "name": "unit"}}, run_key=key),
            mk("testcaserun", "finished", "case-1", {"testCase": {"id": "t1", "name": "test_refund_partial"},
                                                     "testSuiteRun": {"id": "suite-1"}, "outcome": "fail",
                                                     "reason": "500 from Stripe"}),
            mk("testsuiterun", "finished", "suite-1", {"outcome": "fail"}, run_key=key)])
        t = store.tests(self.c, self.run)[0]
        self.assertEqual((t["suite"], t["name"], t["state"], t["message"]),
                         ("unit", "test_refund_partial", "failed", "500 from Stripe"))
        self.assertEqual(store.run_show(self.c, self.run)["suites"][0]["failed"], 1)


class RunnerBase(Base):
    """A fake pipeline of four stages whose commands print '::step' lines, for the runner's tests."""

    def setUp(self):
        super().setUp()
        from river.pipeline import runner
        self.runner = runner
        runner.SYNC["on"] = True
        self.addCleanup(runner.SYNC.update, on=False)
        self.folder = os.path.join(self.dir.name, "app")
        os.makedirs(self.folder)
        core.project_add(self.c, "app", path=self.folder)
        core.register(self.c, "mark", human=True)
        core.register(self.c, "w1")
        define.pipeline_add(self.c, "app", project="app")
        define.env_set(self.c, "app", "beta")
        define.env_set(self.c, "app", "prod")
        define.stage_set(self.c, "app", "build", "build", run=(
            "printf '::step start compile kind=build\\ncompiling for %s\\n::step end compile passed\\n' $MAXPM_REV"))
        define.stage_set(self.c, "app", "test", "test", junit="report.xml", run=(
            "printf '::step start unit kind=test\\n'; printf '<testsuite name=\"unit\"><testcase name=\"a\"/>"
            "<testcase name=\"b\"/></testsuite>' > report.xml; printf '::step end unit passed\\n'"))
        define.stage_set(self.c, "app", "beta", "deploy", env="beta", run="echo deploying $MAXPM_REV to $MAXPM_ENV")
        define.stage_set(self.c, "app", "prod", "deploy", env="prod", run="echo prod", gate="person mark")

    def passes(self, n=6):
        out = []
        for _ in range(n):
            out.append(self.runner.serve_pass(self.c))
        return out

    def runs(self):
        return {r["stage"]: r for r in self.c.execute(
            "SELECT r.*, s.name stage FROM stage_runs r JOIN pipeline_stages s ON s.id=r.stage_id ORDER BY r.id")}


class Runner(RunnerBase):
    """The no-forge runner (#6114)."""

    def test_a_version_goes_through_each_stage_and_waits_for_the_person(self):
        self.runner.start(self.c, "app", rev="abc1234")
        self.passes()
        runs = self.runs()
        self.assertEqual({k: v["state"] for k, v in runs.items()},
                         {"build": "passed", "test": "passed", "beta": "passed", "prod": "queued"})
        build = store.run_show(self.c, runs["build"]["id"])
        self.assertEqual([(s["name"], s["state"], s["kind"]) for s in build["step_list"]],
                         [("compile", "passed", "build")])
        self.assertEqual(build["step_list"][0]["output"], "compiling for abc1234")
        self.assertEqual(store.run_show(self.c, runs["test"]["id"])["tests"], {"passed": 2})
        self.assertIn("deploying abc1234 to beta", store.run_show(self.c, runs["beta"]["id"])["output"])
        envs = {e["name"]: e["rev"] for e in store.show(self.c, "app")["envs"]}
        self.assertEqual(envs, {"beta": "abc1234", "prod": None})
        # The person gate asked with an approval item; mark's done opens it.
        card = runs["prod"]["gate_item"]
        self.assertIn("waits for mark's yes on #", runs["prod"]["gate_note"])
        self.assertEqual(core.item_show(self.c, card)["doer"], "human")
        core.done(self.c, card, actor="mark")
        self.passes(1)
        runs = self.runs()
        self.assertEqual((runs["prod"]["state"], runs["prod"]["approved_by"]), ("passed", "mark"))
        self.assertEqual(store.show(self.c, "app")["envs"][1]["rev"], "abc1234")
        self.assertEqual(self.runner.start(self.c, "app", rev="abc1234")["stage"], "build")  # it went through: again
        with self.assertRaises(RiverError):  # but not twice at once
            self.runner.start(self.c, "app", rev="abc1234")

    def test_a_failed_stage_stops_the_version(self):
        define.stage_set(self.c, "app", "build", run="echo broken; exit 3")
        self.runner.start(self.c, "app", rev="bad0001")
        self.passes()
        runs = self.runs()
        self.assertEqual(list(runs), ["build"])
        self.assertEqual((runs["build"]["state"], runs["build"]["errors"]), ("failed", "exit 3"))

    def test_failed_tests_fail_the_stage(self):
        define.stage_set(self.c, "app", "test", run=(
            "printf '<testsuite name=\"u\"><testcase name=\"a\"><failure message=\"no\"/></testcase></testsuite>' "
            "> report.xml"))
        self.runner.start(self.c, "app", rev="bad0002")
        self.passes()
        t = self.runs()["test"]
        self.assertEqual((t["state"], t["errors"]), ("failed", "1 of 1 tests failed"))
        self.assertNotIn("beta", self.runs())

    def test_a_test_stage_with_no_report_fails(self):
        define.stage_set(self.c, "app", "test", run="true")
        self.runner.start(self.c, "app", rev="bad0003")
        self.passes()
        self.assertIn("no new JUnit report", self.runs()["test"]["errors"])

    @timing.local_only("live child process timeout")
    def test_stage_timeout_stops_the_command(self):
        core.config_set(self.c, "stage_timeout", "1s")
        define.stage_set(self.c, "app", "build", run="sleep 30")
        self.runner.start(self.c, "app", rev="slow001")
        t = time.monotonic()
        self.passes(1)
        self.assertLess(time.monotonic() - t, timing.limit(10, below=30))  # the command sleeps 30 s
        self.assertEqual(self.runs()["build"]["errors"], "stopped at stage_timeout (1s)")

    def test_the_person_declines(self):
        define.stage_set(self.c, "app", "build", gate="person")
        self.runner.start(self.c, "app", rev="nope001")
        self.passes(1)
        card = self.runs()["build"]["gate_item"]
        core.drop(self.c, card, "mark", note="not this one")
        self.passes(1)
        self.assertEqual(self.runs()["build"]["state"], "cancelled")

    def test_a_manager_gate_and_approve(self):
        define.stage_set(self.c, "app", "build", gate="manager")
        self.runner.start(self.c, "app", rev="mgr0001")
        out = self.passes(2)
        self.assertIn("waits for the manager: maxpm pipeline approve app build --rev mgr0001", out[-1]["waiting"][1])
        with self.assertRaises(RiverError):
            self.runner.approve(self.c, "app", "build", actor="w1")
        self.runner.approve(self.c, "app", "build", rev="mgr0001", actor="mark")
        self.passes(1)
        self.assertEqual(self.runs()["build"]["state"], "passed")

    def test_auto_after_a_time_while_the_checks_pass(self):
        define.stage_set(self.c, "app", "test", gate="auto after 30m")
        self.runner.start(self.c, "app", rev="wait001")
        self.passes(2)
        test = self.runs()["test"]
        self.assertEqual(test["state"], "queued")
        self.assertIn("(a delay of 30m) while the checks pass", test["gate_note"])
        # A failed check of the project since the build keeps it closed.
        cid = self.c.execute("INSERT INTO triggers(scope_kind, scope, name, form, config, every, fail_after, "
                             "recover_after, timeout, severity, created_at) VALUES ('project','app','up','http','{}',"
                             "'1m',1,1,'10s','item',?)", (core.iso(core.now()),)).lastrowid
        self.c.execute("INSERT INTO ops_runs(check_id, key, started_at, status) VALUES (?, 'ops:app:up', ?, "
                       "'critical')", (cid, core.iso(core.now())))
        self.c.execute("UPDATE stage_runs SET ended_at=? WHERE id=?",
                       (core.iso(core.now() - core.timedelta(minutes=31)), self.runs()["build"]["id"]))
        self.c.execute("UPDATE ops_runs SET started_at=?", (core.iso(core.now() - core.timedelta(minutes=5)),))
        self.passes(1)
        self.assertIn("check app/up failed", self.runs()["test"]["gate_note"])
        self.c.execute("DELETE FROM ops_runs")
        self.passes(1)
        self.assertEqual(self.runs()["test"]["state"], "passed")

    def test_a_run_that_serve_lost_fails(self):
        self.runner.start(self.c, "app", rev="lost001")
        rid = self.runs()["build"]["id"]
        self.c.execute("UPDATE stage_runs SET state='running', runner='1:x' WHERE id=?", (rid,))
        self.runner.serve_pass(self.c)
        self.assertEqual(self.runs()["build"]["errors"], "maxpm serve stopped while it ran")

    @timing.local_only("live child process timing")
    def test_stop_a_running_command_and_a_wait_at_the_gate(self):
        self.runner.SYNC["on"] = False
        define.stage_set(self.c, "app", "build", run="sleep 30")
        self.runner.start(self.c, "app", rev="stop001")
        self.runner.serve_pass(self.c)
        rid = self.runs()["build"]["id"]
        timing.wait_for(lambda: rid in self.runner.PROCS, what="the command started")
        self.assertEqual(self.runner.stop(self.c, rid, actor="mark")["stopped"], "its command")
        timing.wait_for(lambda: self.runs()["build"]["state"] == "failed", what="the run ended")
        self.assertNotIn(rid, self.runner.PROCS)
        define.stage_set(self.c, "app", "build", gate="manager")
        self.runner.start(self.c, "app", rev="stop002")
        rid = max(r["id"] for r in self.c.execute("SELECT id FROM stage_runs"))
        self.assertEqual(self.runner.stop(self.c, rid, actor="mark")["stopped"], "the wait at its gate")
        with self.assertRaises(RiverError):
            self.runner.stop(self.c, rid, actor="mark")  # cancelled already

    def test_restart_waits_for_a_running_stage(self):
        self.runner.RUNNING[99] = True
        self.addCleanup(self.runner.RUNNING.pop, 99, None)
        self.assertEqual(self.runner.running_text(), "1 pipeline stage(s) run")

    def test_cli_start_and_show(self):
        from river import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.dict(os.environ, {"MAXPM_DB": self.path, "MAXPM_QUIET": "1"}):
            cli.run(["--as", "mark", "pipeline", "start", "app", "--rev", "cli0001"])
            self.passes(4)
            cli.run(["--as", "mark", "pipeline", "show", "app"])
        text = out.getvalue()
        self.assertIn("version cli0001 waits at stage build", text)
        self.assertIn("at the transition: waits for mark's yes on #", text)


class Actions(RunnerBase):
    """Hold, end a hold, roll back, run the failed tests again (#6117); approve, run again and stop are in Runner."""

    def test_a_hold_of_a_stage_blocks_its_transition_and_a_hold_of_every_stage(self):
        self.runner.hold(self.c, "app", "beta", "the payment provider has an outage", actor="mark")
        self.runner.start(self.c, "app", rev="hold001")
        out = self.passes(4)
        beta = self.runs()["beta"]
        self.assertEqual(beta["state"], "queued")
        self.assertIn("blocked by mark: the payment provider has an outage", out[-1]["waiting"][beta["id"]])
        # A version let pass on purpose passes the block; a hold of every stage stops even that.
        self.runner.hold(self.c, "app", None, "release freeze", actor="mark")
        with self.assertRaises(RiverError):  # an automatic transition takes no yes: pass is the way
            self.runner.approve(self.c, "app", "beta", actor="mark")
        from river.pipeline import transitions as tr
        tr.let_pass(self.c, "app", "beta", reason="the outage does not touch beta", actor="mark")
        out = self.passes(1)
        self.assertIn("every stage is held by mark: release freeze", out[-1]["waiting"][beta["id"]])
        self.runner.release_hold(self.c, "app", None, actor="mark")
        self.passes(1)
        self.assertEqual(self.runs()["beta"]["state"], "passed")
        self.runner.release_hold(self.c, "app", "beta", actor="mark")
        with self.assertRaises(RiverError):
            self.runner.release_hold(self.c, "app", "beta", actor="mark")  # not blocked
        with self.assertRaises(RiverError):
            self.runner.hold(self.c, "app", "beta", "  ", actor="mark")  # no reason
        with self.assertRaises(RiverError):
            self.runner.hold(self.c, "app", "beta", "x", actor="w1")  # an agent
        changes = [h["change"] for h in define.history(self.c, "app")[:8]]
        self.assertIn("held every stage: release freeze", changes)
        self.assertIn("unblocked the transition test -> beta", changes)
        self.assertIn("let version hold001 pass the transition test -> beta once (run #3)", changes)

    def test_roll_back_an_environment(self):
        for rev in ("old0001", "new0002"):
            self.runner.start(self.c, "app", rev=rev)
            self.passes(4)
        self.assertEqual(store.show(self.c, "app")["envs"][0]["rev"], "new0002")
        prod_runs = lambda: self.c.execute("SELECT COUNT(*) FROM stage_runs r JOIN pipeline_stages s ON s.id=r.stage_id "
                                           "WHERE s.name='prod'").fetchone()[0]
        before = prod_runs()
        r = self.runner.rollback(self.c, "app", "beta", "old0001", reason="new0002 breaks sign-in", actor="mark")
        self.assertEqual((r["stage"], r["held"]), ("beta", "rolled back beta to old0001: new0002 breaks sign-in"))
        self.passes(2)
        env = store.show(self.c, "app")["envs"][0]
        self.assertEqual((env["rev"], env["history"][0]["kind"]), ("old0001", "rolledback"))
        self.assertEqual(prod_runs(), before)  # a roll back hands nothing on
        self.runner.start(self.c, "app", rev="new0003")
        out = self.passes(3)
        self.assertIn("rolled back beta to old0001", self.runs()["beta"]["gate_note"])  # the gate holds
        with self.assertRaises(RiverError):
            self.runner.rollback(self.c, "app", "nowhere", "old0001", actor="mark")

    def test_versions_in_a_stage_and_a_move_back_as_one_more_record(self):
        """#6141: Versions in <stage> lists each run of the stage, newest first, with what it was, who and why; a move
        back takes a version that passed there, runs the stage with it, and blocks the transition into the stage."""
        for rev in ("old0001", "new0002"):
            self.runner.start(self.c, "app", rev=rev)
            self.passes(4)
        runs = store.stage_versions(self.c, "app", "beta")["runs"]
        self.assertEqual([(r["rev"], r["what"], r["state"], r["live"]) for r in runs],
                         [("new0002", "deployed", "passed", True), ("old0001", "deployed", "passed", False)])
        self.assertEqual(runs[0]["why"], "the flow: the stage before passed")
        # A stage with no environment: its newest passed run is the version it has now.
        self.assertEqual([r["live"] for r in store.stage_versions(self.c, "app", "build")["runs"]], [True, False])
        with self.assertRaises(RiverError):  # never passed there
            self.runner.move_back(self.c, "app", "beta", "zzz9999", reason="x", actor="mark")
        with self.assertRaises(RiverError):  # an agent
            self.runner.move_back(self.c, "app", "beta", "old0001", reason="x", actor="w1")
        r = self.runner.move_back(self.c, "app", "beta", "old0001", reason="new0002 breaks sign-in", actor="mark")
        self.assertEqual(r["held"], "moved stage beta back to old0001: new0002 breaks sign-in")
        self.passes(2)
        runs = store.stage_versions(self.c, "app", "beta")["runs"]
        self.assertEqual([(x["rev"], x["what"], x["live"]) for x in runs][:2],
                         [("old0001", "moved back", True), ("new0002", "deployed", False)])
        self.assertEqual((runs[0]["by"], runs[0]["why"]), ("mark", "new0002 breaks sign-in"))
        sid = self.runs()["beta"]["stage_id"]
        from river.pipeline import transitions
        self.assertEqual(transitions.get(self.c, sid)["block_reason"],
                         "moved stage beta back to old0001: new0002 breaks sign-in")
        self.runner.run_again(self.c, "app", "beta", "new0002", actor="mark", reason="the sign-in fix is in")
        self.passes(1)
        top = store.stage_versions(self.c, "app", "beta")["runs"][0]
        self.assertEqual((top["rev"], top["what"], top["why"], top["live"]), ("new0002", "run again",
                                                                             "the sign-in fix is in", True))
        from river import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.dict(os.environ, {"MAXPM_DB": self.path, "MAXPM_QUIET": "1"}):
            self.assertEqual(cli.run(["--as", "w1", "pipeline", "runs", "app", "beta"]), 0)  # any session reads it
            self.assertEqual(cli.run(["--as", "mark", "pipeline", "move-back", "app", "beta", "--rev", "old0001",
                                      "--reason", "again"]), 0)
        text = out.getvalue()
        self.assertIn("Versions in beta (pipeline app), newest first:", text)
        self.assertIn("new0002  run again: passed", text)
        self.assertIn("(live)  by mark: the sign-in fix is in", text)
        self.assertIn("old0001  moved back: passed", text)
        self.assertIn("the transition into it is blocked until maxpm pipeline unblock app beta", text)

    def test_run_the_failed_tests_again(self):
        define.stage_set(self.c, "app", "test", run=(
            "printf '%s' \"$MAXPM_TESTS\" > asked.txt; printf '<testsuite name=\"u\"><testcase classname=\"u\" "
            "name=\"a\"/><testcase classname=\"u\" name=\"b\"><failure message=\"no\"/></testcase></testsuite>' "
            "> report.xml"))
        self.runner.start(self.c, "app", rev="tst0001")
        self.passes(2)
        first = self.runs()["test"]["id"]
        self.assertEqual(self.c.execute("SELECT state FROM stage_runs WHERE id=?", (first,)).fetchone()[0], "failed")
        r = self.runner.rerun_failed(self.c, first, actor="mark")
        self.passes(1)
        with open(os.path.join(self.folder, "asked.txt")) as f:
            self.assertEqual(f.read(), "u.b")
        self.assertEqual(self.c.execute("SELECT purpose FROM stage_runs WHERE id=?", (r["run"],)).fetchone()[0],
                         f"failed:{first}")
        build_runs = self.c.execute("SELECT COUNT(*) FROM stage_runs").fetchone()[0]
        with self.assertRaises(RiverError):
            self.runner.rerun_failed(self.c, self.runs()["build"]["id"], actor="mark")  # no failed test
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM stage_runs").fetchone()[0], build_runs)


class Transitions(RunnerBase):
    """The transition into each stage (#6139): its mode, its time (a window, freezes), a block, the fault rule, a
    delay, disabled, approve and let one version pass; each change a line of the audit log with its reason."""

    def setUp(self):
        super().setUp()
        from river.pipeline import transitions
        self.tr = transitions

    def waits_at_beta(self, rev):
        self.runner.start(self.c, "app", rev=rev)
        out = self.passes(4)
        beta = self.runs()["beta"]
        self.assertEqual(beta["state"], "queued")
        return out[-1]["waiting"][beta["id"]]

    def span(self, a, b):
        """A daily window from now + a hours to now + b hours, in UTC."""
        t = core.now()
        return f"{(t + core.timedelta(hours=a)):%H:%M}-{(t + core.timedelta(hours=b)):%H:%M} UTC"

    def test_a_time_window_lets_versions_pass_only_inside_it(self):
        self.tr.change(self.c, "app", "beta", window=self.span(2, 3), reason="beta deploys by day", actor="mark")
        note = self.waits_at_beta("win0001")
        self.assertIn(f"outside its window ({self.span(2, 3)}): opens", note)
        arrow = store.show(self.c, "app")["stages"][2]["transition"]
        self.assertEqual((arrow["state"], arrow["window"]["open"], arrow["set_by"]), ("window", False, "mark"))
        self.assertEqual([w["rev"] for w in arrow["waiting"]], ["win0001"])
        self.tr.change(self.c, "app", "beta", window=self.span(-1, 1), actor="mark")
        self.passes(1)
        self.assertEqual(self.runs()["beta"]["state"], "passed")
        with self.assertRaises(RiverError):
            self.tr.change(self.c, "app", "beta", window="fri 9-17", actor="mark")  # no time span
        with self.assertRaises(RiverError):
            self.tr.change(self.c, "app", "beta", window="someday 09:00-17:00", actor="mark")
        self.tr.change(self.c, "app", "beta", window="none", actor="mark")
        self.assertIsNone(self.tr.get(self.c, store.show(self.c, "app")["stages"][2]["id"])["time_window"])

    def test_windows_by_day_and_over_midnight(self):
        from datetime import datetime, timezone
        w = self.tr.parse_window("mon-thu 09:00-17:00 UTC; sat 22:00-04:00 UTC")
        at = lambda *a: datetime(*a, tzinfo=timezone.utc)
        self.assertEqual(self.tr.window_state(w, at(2026, 11, 16, 10, 0)), (True, at(2026, 11, 16, 17, 0)))  # Mon
        self.assertEqual(self.tr.window_state(w, at(2026, 11, 20, 10, 0)), (False, at(2026, 11, 21, 22, 0)))  # Fri
        self.assertEqual(self.tr.window_state(w, at(2026, 11, 22, 3, 0)), (True, at(2026, 11, 22, 4, 0)))  # Sun 03:00
        self.assertEqual(self.tr.parse_window("weekdays 08:00-18:00")[0]["days"], [0, 1, 2, 3, 4])
        self.assertEqual(self.tr.parse_window("fri-mon 08:00-18:00")[0]["days"], [0, 4, 5, 6])

    def test_a_freeze_of_every_transition_and_its_end(self):
        r = self.tr.freeze(self.c, "app", None, end="2h", reason="the holidays", actor="mark")
        self.assertIn("froze every transition from", r["changed"])
        self.runner.start(self.c, "app", rev="frz0001")
        out = self.passes(1)
        self.assertIn("frozen until", out[-1]["waiting"][self.runs()["build"]["id"]])
        self.tr.freeze(self.c, "app", clear=True, actor="mark")
        self.passes(4)
        self.assertEqual(self.runs()["beta"]["state"], "passed")
        with self.assertRaises(RiverError):  # nothing left to remove
            self.tr.freeze(self.c, "app", clear=True, actor="mark")
        with self.assertRaises(RiverError):  # no reason
            self.tr.freeze(self.c, "app", "beta", end="2h", actor="mark")

    def test_a_block_until_a_time_or_an_item_ends_by_itself(self):
        self.tr.block(self.c, "app", "beta", "the payment provider is down", until="1h", actor="mark")
        self.assertIn("blocked by mark: the payment provider is down (until", self.waits_at_beta("blk0001"))
        sid = self.runs()["beta"]["stage_id"]
        self.c.execute("UPDATE pipeline_transitions SET block_until=? WHERE stage_id=?",
                       (core.iso(core.now() - core.timedelta(minutes=1)), sid))
        self.passes(1)
        self.assertEqual(self.runs()["beta"]["state"], "passed")
        self.assertIsNone(self.tr.get(self.c, sid)["block_reason"])
        self.assertIn("transition test -> beta: the block of mark ended (its time came)",
                      [h["change"] for h in define.history(self.c, "app")])
        # Until an item closes: a fault, a person's task.
        item = core.item_add(self.c, "app", "Find out why sign-in fails")["id"]
        self.tr.block(self.c, "app", "beta", "sign-in fails", until_done=item, actor="mark")
        self.assertIn(f"(until #{item} closes)", self.waits_at_beta("blk0002"))
        core.done(self.c, item, actor="mark")
        self.passes(1)
        self.assertEqual(self.runs()["beta"]["state"], "passed")
        line = self.c.execute("SELECT reason, until_at FROM pipeline_changes WHERE change LIKE 'blocked%' ORDER BY id "
                              "DESC").fetchone()
        self.assertEqual(tuple(line), ("sign-in fails", f"#{item}"))

    def test_the_fault_rule_blocks_while_a_fault_of_the_pipeline_is_open(self):
        self.tr.change(self.c, "app", "beta", fault_rule=True, actor="mark")
        fault = core.item_add(self.c, "app", "pipeline:app:watch failed for old0001: 1 of 8 checks fails",
                              kind="operational", doer="ai")["id"]
        self.assertIn(f"blocked while fault #{fault} is open", self.waits_at_beta("flt0001"))
        arrow = store.show(self.c, "app")["stages"][2]["transition"]
        self.assertEqual((arrow["state"], arrow["fault"]["id"], arrow["until"]), ("fault", fault, f"#{fault}"))
        core.drop(self.c, fault, "mark", note="a false alarm")
        self.passes(1)
        self.assertEqual(self.runs()["beta"]["state"], "passed")

    def test_disable_and_a_delay_hold_until_someone_ends_them(self):
        self.tr.disable(self.c, "app", "beta", "the new runner is not ready", actor="mark")
        self.assertIn("disabled by mark: the new runner is not ready", self.waits_at_beta("dis0001"))
        self.tr.delay(self.c, "app", "beta", "2h", reason="wait for the database copy", actor="mark")
        self.tr.enable(self.c, "app", "beta", actor="mark")
        out = self.passes(1)
        self.assertIn("delayed until", out[-1]["waiting"][self.runs()["beta"]["id"]])
        self.tr.delay(self.c, "app", "beta", "none", actor="mark")
        self.passes(1)
        self.assertEqual(self.runs()["beta"]["state"], "passed")
        for call in (lambda: self.tr.enable(self.c, "app", "beta", actor="mark"),  # not disabled
                     lambda: self.tr.disable(self.c, "app", "beta", "", actor="mark"),  # no reason
                     lambda: self.tr.disable(self.c, "app", "beta", "x", actor="w1"),  # an agent
                     lambda: self.tr.block(self.c, "app", "nope", "x", actor="mark")):  # no such stage
            with self.assertRaises(RiverError):
                call()

    def test_a_yes_while_blocked_waits_and_a_blocked_transition_asks_nobody(self):
        self.tr.block(self.c, "app", "prod", "fault #6141: backend errors", actor="mark")
        self.runner.start(self.c, "app", rev="yes0001")
        self.passes(5)
        prod = self.runs()["prod"]
        self.assertIsNone(prod["gate_item"])  # nobody is asked while it is blocked
        self.runner.approve(self.c, "app", "prod", actor="mark")
        out = self.passes(1)
        self.assertIn("blocked by mark: fault #6141: backend errors (until someone unblocks it); mark said yes",
                      out[-1]["waiting"][prod["id"]])
        self.tr.unblock(self.c, "app", "prod", actor="mark")
        self.passes(1)
        self.assertEqual(self.runs()["prod"]["state"], "passed")

    def test_let_one_version_pass_a_block_but_not_a_named_person_s_yes(self):
        core.register(self.c, "boss")
        self.c.execute("UPDATE agents SET role='manager' WHERE name='boss'")
        self.tr.block(self.c, "app", "prod", "release freeze", actor="mark")
        self.runner.start(self.c, "app", rev="pas0001")
        self.passes(5)
        r = self.tr.let_pass(self.c, "app", "prod", reason="the hotfix for checkout", actor="boss")
        self.assertTrue(r["waits_for_yes"])  # the transition takes mark's yes; the manager passes only the block
        self.passes(1)
        prod = self.runs()["prod"]
        self.assertEqual((prod["state"], prod["passed_by"]), ("queued", "boss"))
        self.assertIn("waits for mark's yes on #", prod["gate_note"])  # asked now: the block no longer holds it
        core.done(self.c, prod["gate_item"], actor="mark")
        self.passes(1)
        self.assertEqual(self.runs()["prod"]["state"], "passed")
        with self.assertRaises(RiverError):  # nothing waits there now
            self.tr.let_pass(self.c, "app", "prod", reason="again", actor="mark")

    def test_the_release_with_a_cadence(self):
        self.tr.change(self.c, "app", "beta", mode="release every 2h", actor="mark")
        self.runner.start(self.c, "app", rev="rel0001")
        self.passes(4)
        self.assertEqual(self.runs()["beta"]["state"], "passed")  # the first version: no release before it
        self.runner.start(self.c, "app", rev="rel0002")
        out = self.passes(4)
        beta = self.runs()["beta"]
        self.assertIn("waits for the release: the cadence lets the next version in at", out[-1]["waiting"][beta["id"]])
        self.runner.approve(self.c, "app", "beta", actor="mark")  # the manager or a person releases it now
        self.passes(1)
        self.assertEqual(self.runs()["beta"]["state"], "passed")
        with self.assertRaises(RiverError):
            self.tr.change(self.c, "app", "beta", mode="release every 2h", actor="mark")  # the same already

    def test_the_cli_shows_and_changes_a_transition(self):
        from river import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.dict(os.environ, {"MAXPM_DB": self.path, "MAXPM_QUIET": "1"}):
            for words in (("transition", "app", "prod", "--window", "mon-thu 09:00-17:00 America/Los_Angeles",
                           "--fault-rule", "on", "--reason", "mark: no prod deploys on Fridays"),
                          ("block", "app", "prod", "--reason", "fault #6141", "--until", "2h"),
                          ("freeze", "app", "prod", "--from", "2026-11-26T00:00", "--to", "2026-11-28T23:00",
                           "--reason", "Thanksgiving"),
                          ("transition", "app", "prod"), ("show", "app")):
                self.assertEqual(cli.run(["--as", "mark", "pipeline", *words]), 0)
        text = out.getvalue()
        self.assertIn("pipeline app: the transition into stage prod: window none -> mon-thu 09:00-17:00 "
                      "America/Los_Angeles; blocked while a fault of the pipeline is open", text)
        self.assertIn("  mode: yes mark, mark's yes", text)
        self.assertIn("  block: fault #6141 (set by mark", text)
        self.assertIn(": Thanksgiving (set by mark)", text)
        self.assertIn("  now: blocked by mark: fault #6141 (until", text)
        self.assertIn("4. prod  [deploy, env prod]  mode yes mark, window mon-thu 09:00-17:00 America/Los_Angeles, "
                      "fault rule  run: echo prod", text)
        exp = define.export(self.c, "app")["stages"][-1]["transition"]
        self.assertEqual((exp["mode"], exp["fault_rule"], [f["reason"] for f in exp["freezes"]]),
                         ("yes mark", True, ["Thanksgiving"]))

    def test_the_gates_and_holds_before_become_transitions(self):
        """A queue made before #6139: pipeline_stages had gate, gate_rule and held_*; the migration makes them the
        transition into each stage, once."""
        from river.pipeline import schema
        for c in ("gate TEXT NOT NULL DEFAULT 'auto'", "gate_rule TEXT", "held_reason TEXT", "held_by TEXT",
                  "held_at TEXT"):
            self.c.execute(f"ALTER TABLE pipeline_stages ADD COLUMN {c}")
        ids = {s["name"]: s["id"] for s in store.show(self.c, "app")["stages"]}
        self.c.execute("DELETE FROM pipeline_transitions")
        self.c.execute("UPDATE pipeline_stages SET gate='auto', gate_rule='30m' WHERE name='test'")
        self.c.execute("UPDATE pipeline_stages SET held_reason='an outage', held_by='mark' WHERE name='beta'")
        self.c.execute("UPDATE pipeline_stages SET gate='person', gate_rule='mark' WHERE name='prod'")
        self.c.execute("UPDATE pipeline_stages SET gate='manager' WHERE name='build'")
        self.c.execute("DELETE FROM meta WHERE key='pipeline_transitions_6139'")
        schema.migrate(self.c)
        got = {n: self.tr.get(self.c, i) for n, i in ids.items()}
        self.assertEqual({n: (t["mode"], t["mode_arg"], t["block_reason"]) for n, t in got.items()},
                         {"build": ("yes", "manager", None), "test": ("delay", "30m", None),
                          "beta": ("auto", None, "an outage"), "prod": ("yes", "mark", None)})
        self.c.execute("UPDATE pipeline_stages SET gate='forge'")
        schema.migrate(self.c)  # once only
        self.assertEqual(self.tr.get(self.c, ids["build"])["mode"], "yes")


class Branches(RunnerBase):
    """#6142 (Decision 9): after a stage the flow can split into branches, each with its own transitions, stages and
    versions; a join starts when a version passed every branch; a move back acts on one branch or on every branch."""

    def setUp(self):
        super().setUp()
        define.env_set(self.c, "app", "beta-b")
        define.stage_set(self.c, "app", "beta-b", "deploy", env="beta-b", pos=4, after="test",
                         run="test -f fail-b && exit 3 || echo region b")
        define.stage_set(self.c, "app", "prod", gate="auto", after="beta,beta-b")

    def rev_runs(self, stage):
        return [(r["rev"], r["state"]) for r in self.c.execute(
            "SELECT v.rev, r.state FROM stage_runs r JOIN pipeline_stages s ON s.id=r.stage_id JOIN pipeline_versions v "
            "ON v.id=r.version_id WHERE s.name=? ORDER BY r.id", (stage,))]

    def test_a_version_runs_in_every_branch_and_the_join_waits_for_all(self):
        rows, prev, nxt = store.stage_graph(self.c, store._pipeline(self.c, "app")["id"])
        self.assertEqual((prev["beta-b"], prev["prod"], nxt["test"]), (["test"], ["beta", "beta-b"], ["beta", "beta-b"]))
        self.runner.start(self.c, "app", rev="brn0001")
        self.passes(6)
        self.assertEqual(self.rev_runs("beta"), [("brn0001", "passed")])
        self.assertEqual(self.rev_runs("beta-b"), [("brn0001", "passed")])
        self.assertEqual(self.rev_runs("prod"), [("brn0001", "passed")])  # once, after both branches
        # Region b fails for the next version: the join lets it in only when it passed every branch.
        open(os.path.join(self.folder, "fail-b"), "w").close()
        self.runner.start(self.c, "app", rev="brn0002")
        self.passes(6)
        self.assertEqual(self.rev_runs("beta")[-1], ("brn0002", "passed"))
        self.assertEqual(self.rev_runs("beta-b")[-1], ("brn0002", "failed"))
        self.assertEqual(len(self.rev_runs("prod")), 1)
        s = {x["name"]: x for x in store.show(self.c, "app")["stages"]}
        self.assertEqual(s["prod"]["transition"]["join"], {"rev": "brn0002", "missing": ["beta-b"]})
        self.assertEqual(s["prod"]["prev"], ["beta", "beta-b"])
        self.assertEqual(s["beta-b"]["prev"], ["test"])
        # Region b passes when it runs again: the version then enters the join.
        os.remove(os.path.join(self.folder, "fail-b"))
        self.runner.run_again(self.c, "app", "beta-b", "brn0002", "mark")
        self.passes(4)
        self.assertEqual(self.rev_runs("prod")[-1], ("brn0002", "passed"))
        self.assertIsNone(store.show(self.c, "app")["stages"][4]["transition"]["join"])
        from river.pipeline import transitions
        self.assertEqual(transitions._arrow(self.c, store._pipeline(self.c, "app"), s["prod"]), "beta + beta-b -> prod")
        from river import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.dict(os.environ, {"MAXPM_DB": self.path, "MAXPM_QUIET": "1"}):
            cli.run(["--as", "mark", "pipeline", "show", "app"])
        self.assertIn("4. beta-b  [deploy, env beta-b, after test]", buf.getvalue())
        self.assertIn("5. prod  [deploy, env prod, after beta + beta-b]", buf.getvalue())

    def test_after_names_earlier_stages_only(self):
        for bad in ("prod", "beta-b", "nope", "test,test"):
            with self.assertRaises(RiverError):
                define.stage_set(self.c, "app", "beta-b", after=bad, actor="mark")
        define.stage_set(self.c, "app", "prod", after="", actor="mark")  # '' clears: the stage before it again
        _, prev, _ = store.stage_graph(self.c, store._pipeline(self.c, "app")["id"])
        self.assertEqual(prev["prod"], ["beta-b"])
        self.assertIn("after beta,beta-b -> none", define.history(self.c, "app", 1)[0]["change"])

    def test_a_move_back_acts_on_one_branch_or_on_every_branch(self):
        for rev in ("brn0001", "brn0002"):
            self.runner.start(self.c, "app", rev=rev)
            self.passes(6)
        p = store._pipeline(self.c, "app")
        self.assertEqual(store.branch_siblings(self.c, p["id"], "beta"), ["beta-b"])
        self.assertEqual(store.branch_siblings(self.c, p["id"], "test"), [])
        self.assertEqual(store.stage_versions(self.c, "app", "beta-b")["branches"], ["beta"])
        self.runner.move_back(self.c, "app", "beta-b", "brn0001", "region b only", "mark")
        self.passes(2)
        envs = {e["name"]: e["rev"] for e in store.show(self.c, "app")["envs"]}
        self.assertEqual((envs["beta"], envs["beta-b"]), ("brn0002", "brn0001"))
        r = self.runner.move_back_branches(self.c, "app", "beta", "brn0001", "a fault in every region", "mark")
        self.assertEqual((r["moved"], r["left"]), (["beta", "beta-b"], []))
        self.passes(2)
        envs = {e["name"]: e["rev"] for e in store.show(self.c, "app")["envs"]}
        self.assertEqual((envs["beta"], envs["beta-b"]), ("brn0001", "brn0001"))
        with self.assertRaises(RiverError):
            self.runner.move_back_branches(self.c, "app", "test", "brn0001", "x", "mark")  # on no branch


class Followed(RunnerBase):
    """#6167: a followed pipeline learns its stages from an outside system's events; none of our flow actions apply;
    its trigger rules and hooks still act on its events."""

    def setUp(self):
        super().setUp()
        from river.pipeline import follow, triggers
        self.follow, self.triggers = follow, triggers

    def ev(self, predicate, sid, content=None, **maxpm):
        return cdevents.make("pipelinerun", predicate, sid, content, {"pipeline": "partner", **maxpm}, "jenkins", None)

    def test_events_make_its_stages_and_our_flow_actions_refuse(self):
        r = self.follow.follow(self.c, "partner", project="app", actor="mark")
        self.assertEqual(r["followed"], True)
        for i, (stage, out) in enumerate((("build", "success"), ("deploy", "success")), 1):
            store.ingest(self.c, self.ev("queued", f"j-{i}", {"pipelineName": stage}, version="ptn0001"))
            res = store.ingest(self.c, self.ev("finished", f"j-{i}", {"outcome": out}))
            self.runner.hand_on_passed(self.c, [res])
        self.assertEqual([s["name"] for s in store.show(self.c, "partner")["stages"]], ["build", "deploy"])
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM stage_runs r JOIN pipelines p ON p.id=r.pipeline_id "
                                        "WHERE p.name='partner'").fetchone()[0], 2)  # we queued nothing after build
        from river.pipeline import operators, transitions
        for act in (lambda: self.runner.start(self.c, "partner", rev="x1", actor="mark"),
                    lambda: self.runner.run_again(self.c, "partner", "deploy", "ptn0001", "mark"),
                    lambda: self.runner.move_back(self.c, "partner", "deploy", "ptn0001", "x", "mark"),
                    lambda: self.runner.hold(self.c, "partner", reason="x", actor="mark"),
                    lambda: transitions.block(self.c, "partner", "deploy", "x", actor="mark"),
                    lambda: operators.stop(self.c, "partner", "x", actor="mark"),
                    lambda: operators.override(self.c, "partner", "deploy", "ptn0001", "x", actor="mark")):
            with self.assertRaises(RiverError) as e:
                act()
            self.assertIn("pipeline partner is followed", str(e.exception))
        self.assertTrue(operators.plan(self.c, "partner")["followed"])
        with self.assertRaises(RiverError):
            self.follow.follow(self.c, "partner", actor="w1")  # a person or the manager
        self.follow.follow(self.c, "partner", off=True, actor="mark")
        transitions.block(self.c, "partner", "deploy", "ours again", actor="mark")

    def test_its_rules_act_on_its_failures_an_agent_and_a_hook(self):
        self.follow.follow(self.c, "partner", project="app", actor="mark")
        store.ingest(self.c, self.ev("queued", "j-1", {"pipelineName": "deploy"}, version="ptn0002"))
        self.triggers.rule_add(self.c, "partner", "stage-failed", command="echo $MAXPM_EVENT $MAXPM_STAGE $MAXPM_REV "
                               "> hook.txt; echo done", actor="mark")
        with self.assertRaises(RiverError):
            self.triggers.rule_add(self.c, "partner", "stage-failed", command="x", keys="prod", actor="mark")
        store.ingest(self.c, self.ev("finished", "j-1", {"outcome": "failure", "errors": "exit 1"}))
        fires = self.triggers.serve_pass(self.c)
        results = sorted(f["result"] for f in self.c.execute("SELECT result FROM pipeline_trigger_fires"))
        self.assertEqual(len(fires), 2)  # the default rule's agent and the hook
        self.assertTrue(any(r.startswith("opened fault #") or "fault #" in r for r in results), results)
        self.assertIn("ran the hook `echo $MAXPM_EVENT $MAXPM_STAGE $MAXPM_REV > hook.txt; echo done`: exit 0: done",
                      results)
        with open(os.path.join(self.folder, "hook.txt")) as f:
            self.assertEqual(f.read().strip(), "stage-failed deploy ptn0002")
        line = self.c.execute("SELECT kind, change FROM pipeline_changes WHERE action='hook'").fetchone()
        self.assertEqual(line["kind"], "hook")
        self.assertEqual(self.triggers.export(self.c, store._pipeline(self.c, "partner")["id"])[-1]["command"],
                         "echo $MAXPM_EVENT $MAXPM_STAGE $MAXPM_REV > hook.txt; echo done")


class DefinitionVersions(RunnerBase):
    """#6143: each change of a pipeline's definition makes a numbered version (who, when, why); compare two; go back
    to one as one more version. Actions of a moment (a block, a hold) make none."""

    def setUp(self):
        super().setUp()
        from river.pipeline import versions
        self.v = versions

    def ns(self):
        return [x["n"] for x in self.v.listing(self.c, "app")["versions"]]

    def test_each_change_is_a_version_and_a_block_is_not(self):
        first = self.v.listing(self.c, "app")["versions"]
        self.assertEqual(first[-1]["why"], "pipeline app added for project app")
        n = first[0]["n"]
        from river.pipeline import transitions, triggers
        define.stage_set(self.c, "app", "beta", run="make beta", actor="mark")
        transitions.change(self.c, "app", "prod", window="22:00-04:00 UTC", reason="region b at night", actor="mark")
        triggers.rule_add(self.c, "app", "stage-failed", does="find the cause", actor="mark")
        transitions.block(self.c, "app", "beta", "a freeze", actor="mark")  # not a definition
        self.runner.hold(self.c, "app", reason="x", actor="mark")
        self.assertEqual(self.ns()[:4], [n + 3, n + 2, n + 1, n])
        top = self.v.listing(self.c, "app")["versions"][:3]
        self.assertEqual([x["actor"] for x in top], ["mark"] * 3)
        self.assertIn("window none -> 22:00-04:00 UTC: region b at night", top[1]["why"])
        diff = self.v.compare(self.c, "app", n, n + 2)["changes"]
        self.assertEqual([(c["what"], c["a"], c["b"]) for c in diff],
                         [("stage beta run", "echo deploying $MAXPM_REV to $MAXPM_ENV", "make beta"),
                          ("stage prod transition window", None, "22:00-04:00 UTC")])
        self.assertEqual(self.v.show(self.c, "app", n + 1)["definition"]["stages"][2]["run"], "make beta")

    def test_go_back_to_a_version_is_one_more_version(self):
        n = self.ns()[0]
        from river.pipeline import transitions, triggers
        define.env_set(self.c, "app", "beta-b", actor="mark")
        define.stage_set(self.c, "app", "beta-b", "deploy", env="beta-b", pos=4, after="test", run="echo b", actor="mark")
        define.stage_set(self.c, "app", "build", tier="alpha", start="manual", actor="mark")
        transitions.change(self.c, "app", "beta", window="01:00-02:00 UTC", actor="mark")
        triggers.rule_add(self.c, "app", "stage-failed", does="find the cause", actor="mark")
        define.stage_set(self.c, "app", "test", remove=True, actor="mark")
        with self.assertRaises(RiverError):
            self.v.restore(self.c, "app", n, " ", actor="mark")  # no reason
        with self.assertRaises(RiverError):
            self.v.restore(self.c, "app", n, "x", actor="w1")  # an agent that is not the manager
        r = self.v.restore(self.c, "app", n, "the split went wrong", actor="mark")
        self.assertEqual(r["back_to"], n)
        self.assertEqual(self.v.compare(self.c, "app", n, r["version"])["changes"], [])
        self.assertEqual(self.ns()[0], r["version"])
        self.assertEqual(self.v.listing(self.c, "app")["versions"][0]["why"], f"back to version {n}: the split went wrong")
        self.assertEqual([s["name"] for s in store.show(self.c, "app")["stages"]], ["build", "test", "beta", "prod"])
        # Back to the newer one again: the branch, the tier, the window and the rule come back.
        r2 = self.v.restore(self.c, "app", r["version"] - 1, "it was right", actor="mark")
        self.assertEqual(self.v.compare(self.c, "app", r["version"] - 1, r2["version"])["changes"], [])
        from river import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.dict(os.environ, {"MAXPM_DB": self.path, "MAXPM_QUIET": "1"}):
            cli.run(["--as", "mark", "pipeline", "versions", "app", "--compare", str(n), str(r2["version"])])
            cli.run(["--as", "mark", "pipeline", "versions", "app"])
        self.assertIn('stage build tier: null -> "alpha"', buf.getvalue())
        self.assertIn(f"  v{r2['version']}  ", buf.getvalue())


class ManualStart(RunnerBase):
    """#6138: a stage starts automatically when its transition lets a version in, or manually: the version waits in
    the stage until someone presses Run (maxpm pipeline run-stage)."""

    def test_a_manual_stage_waits_for_run_and_then_runs(self):
        define.stage_set(self.c, "app", "beta", start="manual", actor="mark")
        self.assertEqual(define.export(self.c, "app")["stages"][2]["start"], "manual")
        with self.assertRaises(RiverError):
            define.stage_set(self.c, "app", "beta", start="later", actor="mark")
        self.runner.start(self.c, "app", rev="man0001")
        out = self.passes(4)
        self.assertEqual(self.runs()["beta"]["state"], "queued")  # in the stage, not run
        self.assertIn("waits in stage beta for Run", list(out[-1]["waiting"].values())[0])
        s = {x["name"]: x for x in store.show(self.c, "app")["stages"]}
        self.assertEqual((s["beta"]["start"], s["beta"]["waits_for_run"]["rev"]), ("manual", "man0001"))
        self.assertIsNone(s["test"]["waits_for_run"])
        self.assertEqual(s["beta"]["transition"]["waiting"], [])  # in the stage, not at its arrow
        with self.assertRaises(RiverError) as e:
            self.runner.run_stage(self.c, "app", "test", actor="mark")
        self.assertIn("stage test starts by itself", str(e.exception))
        with self.assertRaises(RiverError):
            self.runner.run_stage(self.c, "app", "beta", actor="w1")  # an agent that is no operator
        r = self.runner.run_stage(self.c, "app", "beta", actor="mark")
        self.assertEqual(r["version"], "man0001")
        self.passes(2)
        self.assertEqual(self.runs()["beta"]["state"], "passed")
        lines = [x["change"] for x in self.c.execute("SELECT change FROM pipeline_changes WHERE action IN "
                                                     "('entered','run') ORDER BY id")]
        self.assertEqual(lines, ["version man0001 entered stage beta; it waits for Run (the stage starts manually)",
                                 f"pressed Run: version man0001 starts in stage beta (run #{r['run']})"])
        with self.assertRaises(RiverError):
            self.runner.run_stage(self.c, "app", "beta", actor="mark")  # nothing waits now
        # A run again starts at once: a person sent it.
        self.runner.run_again(self.c, "app", "beta", "man0001", "mark")
        self.passes(2)
        self.assertEqual(self.runs()["beta"]["state"], "passed")
        # An operator of the pipeline may press Run too, an agent when the pipeline lets agents operate.
        from river.pipeline import operators
        operators.change(self.c, "app", add=["w1"], agents=True, actor="mark")
        self.runner.start(self.c, "app", rev="man0002")
        self.passes(4)
        self.assertEqual(self.runner.run_stage(self.c, "app", "beta", actor="w1")["version"], "man0002")
        from river import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.dict(os.environ, {"MAXPM_DB": self.path, "MAXPM_QUIET": "1"}):
            cli.run(["--as", "mark", "pipeline", "show", "app"])
        self.assertIn("[deploy, env beta, starts manually]", out.getvalue())

    def test_a_held_pipeline_holds_a_pressed_run(self):
        define.stage_set(self.c, "app", "beta", start="manual", actor="mark")
        self.runner.start(self.c, "app", rev="hld0001")
        self.passes(4)
        self.runner.hold(self.c, "app", reason="a freeze", actor="mark")
        self.runner.run_stage(self.c, "app", "beta", actor="mark")
        out = self.passes(1)
        self.assertIn("every stage is held by mark", list(out[-1]["waiting"].values())[0])
        self.runner.release_hold(self.c, "app", actor="mark")
        self.passes(2)
        self.assertEqual(self.runs()["beta"]["state"], "passed")


class Emergency(RunnerBase):
    """The operators of a pipeline and its emergency actions (#6145): stop and resume, stop its agents, move a stage
    back to its last good version, revoke a key; each with a reason and an audit line that names the operator."""

    def setUp(self):
        super().setUp()
        from river.pipeline import operators
        self.ops = operators

    def audit(self, action):
        return [dict(r) for r in self.c.execute("SELECT actor, change, reason FROM pipeline_changes WHERE action=? "
                                                "ORDER BY id", (action,))]

    def test_operators_a_person_names_them_and_decides_whether_agents_may_act(self):
        p = store._pipeline(self.c, "app")
        self.assertIsNone(self.ops.refusal(self.c, p, "mark"))  # none named: a person or the manager
        self.assertIn("only a person or the manager", self.ops.refusal(self.c, p, "w1"))
        with self.assertRaises(RiverError):
            self.ops.change(self.c, "app", add=["w1"], actor="w1")  # an agent names no operator
        r = self.ops.change(self.c, "app", add=["mark", "w1"], actor="mark")
        self.assertEqual([(o["name"], o["kind"], o["may_act"]) for o in r["operators"]],
                         [("mark", "person", True), ("w1", "agent", False)])
        p = store._pipeline(self.c, "app")
        self.assertIn("lets no agent operate", self.ops.refusal(self.c, p, "w1"))
        self.ops.change(self.c, "app", agents=True, actor="mark")
        self.assertIsNone(self.ops.refusal(self.c, store._pipeline(self.c, "app"), "w1"))
        core.register(self.c, "boss")
        self.c.execute("UPDATE agents SET role='manager' WHERE name='boss'")
        self.assertIn("not an operator", self.ops.refusal(self.c, store._pipeline(self.c, "app"), "boss"))
        for bad in (dict(add=["nobody"]), dict(add=["mark"]), dict(remove=["boss"]), {}):
            with self.assertRaises(RiverError):
                self.ops.change(self.c, "app", actor="mark", **bad)
        self.assertEqual([x["change"] for x in self.audit("operators")][-1],
                         "agents may be operators of pipeline app")

    @timing.local_only("live child process timing")
    def test_stop_ends_a_running_stage_after_its_step_and_resume_lets_it_go_on(self):
        self.runner.SYNC["on"] = False
        define.stage_set(self.c, "app", "build", run="printf '::step start one\\n'; sleep 1; printf '::step end one\\n'; "
                         "sleep 30; echo never")
        self.runner.start(self.c, "app", rev="stp0001")
        self.runner.serve_pass(self.c)
        rid = self.runs()["build"]["id"]
        timing.wait_for(lambda: rid in self.runner.PROCS, what="the command started")
        r = self.ops.stop(self.c, "app", "the prod database is slow", actor="mark")
        self.assertEqual(r["running"], 1)
        t = time.monotonic()
        timing.wait_for(lambda: self.runs()["build"]["state"] == "cancelled", what="the stage stopped")
        self.assertLess(time.monotonic() - t, timing.limit(10, below=30))  # not the 30 s sleep
        self.assertIn("stopped after its present step: pipeline stopped by mark: the prod database is slow",
                      self.runs()["build"]["errors"])
        plan = self.ops.plan(self.c, "app")
        self.assertEqual(plan["stopped"]["reason"], "the prod database is slow")
        define.stage_set(self.c, "app", "build", run="true")
        self.runner.SYNC["on"] = True
        self.runner.start(self.c, "app", rev="stp0002")
        out = self.passes(1)
        self.assertIn("every stage is held by mark", list(out[-1]["waiting"].values())[0])
        self.ops.resume(self.c, "app", actor="mark")
        self.passes(4)
        self.assertEqual(self.runs()["beta"]["state"], "passed")
        self.assertEqual(self.audit("emergency-stop")[0]["reason"], "the prod database is slow")
        self.assertEqual(self.audit("resume")[0]["actor"], "mark")
        with self.assertRaises(RiverError):
            self.ops.resume(self.c, "app", actor="mark")  # not stopped
        with self.assertRaises(RiverError):
            self.ops.stop(self.c, "app", " ", actor="mark")  # no reason

    def test_stop_its_agents_and_move_a_stage_back_as_an_agent_operator(self):
        define.stage_set(self.c, "app", "test", run="exit 4")
        self.runner.start(self.c, "app", rev="bad0001")
        self.passes(2)  # the test stage fails: a fault item
        fault = self.runs()["test"]["fault_item"]
        self.assertTrue(fault)
        core.claim(self.c, fault, "w1")
        self.assertEqual([a["name"] for a in self.ops.plan(self.c, "app")["agents"]], ["w1"])
        r = self.ops.stop_agents(self.c, "app", "a bad release, stop now", actor="mark")
        self.assertEqual((r["asked"], r["refused"]), (["w1"], []))
        self.assertTrue(self.c.execute("SELECT stop_at FROM agents WHERE name='w1'").fetchone()[0])
        self.assertEqual(self.ops.plan(self.c, "app")["agents"], [])  # asked already
        # Two good versions through beta, then w1 (an operator, with agents on) moves beta back.
        define.stage_set(self.c, "app", "test", run=(
            "printf '<testsuite name=\"unit\"><testcase name=\"a\"/></testsuite>' > report.xml"))
        for rev in ("old0001", "new0002"):
            self.runner.start(self.c, "app", rev=rev)
            self.passes(4)
        core.register(self.c, "w2")
        self.ops.change(self.c, "app", add=["mark", "w2"], agents=True, actor="mark")
        back = {m["stage"]: (m["now"], m["good"]) for m in self.ops.plan(self.c, "app")["moveback"]}
        self.assertEqual(back["beta"], ("new0002", "old0001"))
        with self.assertRaises(RiverError):
            self.ops.move_back(self.c, "app", "beta", "x", actor="w1")  # not on the list
        r = self.ops.move_back(self.c, "app", "beta", "new0002 breaks sign-in", actor="w2")
        self.assertEqual((r["version"], r["previous"]), ("old0001", "new0002"))
        self.passes(2)
        self.assertEqual(store.show(self.c, "app")["envs"][0]["rev"], "old0001")
        with self.assertRaises(RiverError):
            self.ops.move_back(self.c, "app", "prod", "x", actor="mark")  # nothing passed prod

    def test_override_sends_a_version_straight_to_a_stage_and_a_whole_run_ends_there(self):
        """#6146: the override skips the stages and transitions between; prod runs its own command; prod shows 'not
        through the pipeline' until the whole run passes the skipped stages, which ends where the override put it."""
        self.runner.start(self.c, "app", rev="old0001")
        self.passes(4)  # build, test and beta pass; prod waits for mark's yes
        plan = self.ops.plan(self.c, "app")["override"]
        self.assertEqual([s["name"] for s in plan["stages"]], ["test", "beta", "prod"])
        self.assertEqual(plan["stages"][-1]["before"], ["build", "test", "beta"])
        with self.assertRaises(RiverError):
            self.ops.override(self.c, "app", "prod", "fix0001", "", actor="mark")  # no reason
        with self.assertRaises(RiverError):
            self.ops.override(self.c, "app", "build", "fix0001", "x", actor="mark")  # the first stage: start it
        with self.assertRaises(RiverError):
            self.ops.override(self.c, "app", "prod", "fix0001", "x", actor="w1")  # an agent that is no operator
        r = self.ops.override(self.c, "app", "prod", "fix0001", "hotfix: checkout down", actor="mark")
        self.assertEqual(r["skipped"], ["build", "test", "beta"])
        self.passes(2)
        runs = [(x["stage"], x["rev"], x["state"], x["purpose"]) for x in self.c.execute(
            "SELECT s.name stage, v.rev, r.state, r.purpose FROM stage_runs r JOIN pipeline_stages s ON s.id=r.stage_id "
            "JOIN pipeline_versions v ON v.id=r.version_id WHERE v.rev='fix0001' ORDER BY r.id")]
        self.assertEqual(runs, [("prod", "fix0001", "passed", "override")])  # the skipped stages do not run
        prod = {s["name"]: s for s in store.show(self.c, "app")["stages"]}["prod"]
        self.assertEqual(prod["override"]["skipped"], ["build", "test", "beta"])
        line = self.c.execute("SELECT actor, change, reason FROM pipeline_changes WHERE action='override'").fetchone()
        self.assertEqual(tuple(line), ("mark", "override: sent fix0001 straight to stage prod; skipped build, test, beta",
                                       "hotfix: checkout down"))
        w = self.ops.whole(self.c, "app", "fix0001", actor="mark")
        self.assertEqual(w["stage"], "build")
        self.passes(6)
        runs = [(x["stage"], x["state"]) for x in self.c.execute(
            "SELECT s.name stage, r.state FROM stage_runs r JOIN pipeline_stages s ON s.id=r.stage_id JOIN "
            "pipeline_versions v ON v.id=r.version_id WHERE v.rev='fix0001' ORDER BY r.id")]
        self.assertEqual(runs, [("prod", "passed"), ("build", "passed"), ("test", "passed"), ("beta", "passed")])
        self.assertIsNone({s["name"]: s for s in store.show(self.c, "app")["stages"]}["prod"]["override"])
        self.assertIn("whole pipeline run", [r["what"] for r in store.stage_versions(self.c, "app", "build")["runs"]])

    def test_revoke_a_token_warns_of_the_other_pipelines_that_use_it(self):
        define.forge_set(self.c, "app", "github", repo="o/app", secret="gh-tok", actor="mark")
        define.pipeline_add(self.c, "site", actor="mark")
        define.forge_set(self.c, "site", "github", repo="o/site", secret="gh-tok", actor="mark")
        keys = self.ops.plan(self.c, "app")["keys"]
        self.assertEqual([(k["key"], k["kind"], k["others"]) for k in keys], [("gh-tok", "token", ["site"])])
        with self.assertRaises(RiverError):
            self.ops.revoke(self.c, "app", "nope", "x", actor="mark")
        r = self.ops.revoke(self.c, "app", "gh-tok", "the token leaked in a log", actor="mark")
        self.assertEqual((r["others"], store._pipeline(self.c, "app")["secret"]), (["site"], None))
        self.assertEqual(store._pipeline(self.c, "site")["secret"], "gh-tok")  # the other keeps it
        line = self.audit("revoke")[0]
        self.assertEqual(line["reason"], "the token leaked in a log")
        self.assertIn("other pipelines still use it: site", line["change"])
        from river import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.dict(os.environ, {"MAXPM_DB": self.path, "MAXPM_QUIET": "1"}):
            self.assertEqual(cli.run(["--as", "mark", "pipeline", "emergency", "site"]), 0)
            self.assertEqual(cli.run(["--as", "mark", "pipeline", "emergency", "site", "stop", "--reason",
                                      "a leaked token"]), 0)
        text = out.getvalue()
        self.assertIn("pipeline site: running. The emergency actions take a person or the manager", text)
        self.assertIn("revoke --key gh-tok: the token that pipeline site reads its source with", text)
        self.assertIn("pipeline site stopped: nothing new starts. Resume: maxpm pipeline emergency site resume", text)


class Waves(RunnerBase):
    """A stage over many servers in waves (#6120): three fake servers (local commands), one fails its check."""

    def setUp(self):
        super().setUp()
        define.stage_set(self.c, "app", "prod", gate="auto", waves="web-1; web-2, web-3",
                         run='echo "deploy $MAXPM_REV to $MAXPM_HOST" >> deploys.log',
                         check='test "$MAXPM_HOST" != web-3 || { echo "readyz 503"; exit 1; }',
                         rollback='echo "back to $MAXPM_PREV_REV on $MAXPM_HOST" >> deploys.log')

    def log(self):
        with open(os.path.join(self.folder, "deploys.log")) as f:
            return f.read().splitlines()

    def test_a_failed_check_stops_the_waves_and_rolls_back_that_wave(self):
        store.deployed(self.c, "app", "prod", "old0001")
        self.runner.start(self.c, "app", rev="new0002")
        self.passes(5)
        prod = self.runs()["prod"]
        self.assertEqual(prod["state"], "failed")
        self.assertEqual(prod["errors"], "wave 2: web-3 failed its check (exit 1); rolled back web-2, web-3")
        self.assertEqual(self.log(), ["deploy new0002 to web-1", "deploy new0002 to web-2", "deploy new0002 to web-3",
                                      "back to old0001 on web-2", "back to old0001 on web-3"])
        steps_ = [(s["name"], s["host"], s["state"]) for s in store.run_show(self.c, prod["id"])["step_list"]]
        self.assertEqual(steps_, [("deploy", "web-1", "passed"), ("check", "web-1", "passed"),
                                  ("deploy", "web-2", "passed"), ("check", "web-2", "passed"),
                                  ("deploy", "web-3", "passed"), ("check", "web-3", "failed"),
                                  ("rollback", "web-2", "passed"), ("rollback", "web-3", "passed")])
        self.assertEqual(store.show(self.c, "app")["envs"][1]["rev"], "old0001")  # a failed stage records no deploy

    def test_a_first_wave_that_fails_stops_the_others(self):
        define.stage_set(self.c, "app", "prod", waves="web-3; web-1, web-2")
        self.runner.start(self.c, "app", rev="new0003")
        self.passes(5)
        self.assertEqual(self.runs()["prod"]["errors"], "wave 1: web-3 failed its check (exit 1); rolled back web-3; "
                                                        "2 server(s) of later waves not started")

    def test_all_waves_pass_and_the_definition(self):
        define.stage_set(self.c, "app", "prod", check="true", wave_wait="0s")
        self.runner.start(self.c, "app", rev="new0004")
        self.passes(5)
        self.assertEqual(self.runs()["prod"]["state"], "passed")
        self.assertEqual(store.show(self.c, "app")["envs"][1]["rev"], "new0004")
        stage = next(s for s in define.export(self.c, "app")["stages"] if s["name"] == "prod")
        self.assertEqual((stage["waves"], stage["wave_wait"]), ("web-1; web-2, web-3", "0s"))
        with self.assertRaises(RiverError):
            define.stage_set(self.c, "app", "prod", waves="web 1;$(rm)")


class Links(RunnerBase):
    """Pipelines and the rest of MaximizePM (#6118): fault items, fix tasks, the tasks of a version and back."""

    def git(self, *a):
        import subprocess
        return subprocess.run(["git", "-C", self.folder, *a], capture_output=True, text=True, check=True).stdout.strip()

    def commit(self, msg):
        self.git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def test_a_failed_stage_opens_one_fault_item(self):
        define.stage_set(self.c, "app", "build", run="echo broken; exit 3")
        self.runner.start(self.c, "app", rev="bad0001")
        self.passes(2)
        r = self.runs()["build"]
        it = core.item_show(self.c, r["fault_item"])
        self.assertEqual((it["kind"], it["project"]), ("operational", "app"))
        self.assertTrue(it["title"].startswith("pipeline:app:build failed for bad0001: exit 3"))
        self.assertIn("maxpm pipeline rerun app build --rev bad0001", it["context"])
        self.runner.start(self.c, "app", rev="bad0002")
        self.passes(2)
        second = self.c.execute("SELECT fault_item FROM stage_runs ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertEqual(second, r["fault_item"])  # it joins the open fault item
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM items WHERE kind='operational'").fetchone()[0], 1)

    def test_failed_tests_give_the_prompt_and_a_fix_task(self):
        define.stage_set(self.c, "app", "test", run=(
            "printf '<testsuite name=\"u\"><testcase classname=\"u\" name=\"b\"><failure message=\"no\"/></testcase>"
            "</testsuite>' > report.xml"))
        self.runner.start(self.c, "app", rev="tst0001")
        self.passes(3)
        run = self.runs()["test"]
        self.assertIn("Failed tests:", core.item_show(self.c, run["fault_item"])["context"])
        from river.pipeline import links
        from river import server
        with mock.patch.object(server, "dispatch_item", return_value={"agent": "w9"}) as d:
            out = links.fix_task(self.c, run["id"], actor="mark", start=True)
        d.assert_called_once()
        fix = core.item_show(self.c, out["item"])
        self.assertIn("1. u.b: no.", fix["context"])
        self.assertEqual(out["started"], {"agent": "w9"})

    def test_the_tasks_of_a_version_and_the_versions_of_a_task(self):
        from river.pipeline import links
        self.git("init", "-q")
        a = core.item_add(self.c, "app", "Header", actor="mark")["id"]
        b = core.item_add(self.c, "app", "Footer", actor="mark")["id"]
        v1 = self.commit("start")
        self.commit(f"Fix the header (#{a})")
        v2 = self.commit(f"Fix the footer (#{b}); see #99999")
        for rev in (v1, v2):
            self.runner.start(self.c, "app", rev=rev)
            self.passes(4)
        t = links.version_tasks(self.c, "app", v2)
        self.assertEqual((t["base"], [x["id"] for x in t["tasks"]]), (v1, [a, b]))
        where = links.item_versions(self.c, a)["pipelines"][0]
        self.assertEqual((where["envs"]["beta"]["carries"], where["envs"]["prod"]["carries"]), (True, False))
        self.assertEqual(where["versions"], [v2])


class Triggers(RunnerBase):
    """The trigger rules of a pipeline (#6150): generic events, what the agent does, where, which keys, on and off."""

    def setUp(self):
        super().setUp()
        from river.pipeline import triggers
        self.t = triggers

    def ops(self):
        return self.c.execute("SELECT * FROM items WHERE kind='operational' ORDER BY id").fetchall()

    def test_a_pipeline_starts_with_the_rule_of_before_and_it_can_go_off(self):
        rules = self.t.rules(self.c, "app")["rules"]
        self.assertEqual([(r["event"], r["stage"], r["runner"], r["enabled"]) for r in rules],
                         [("stage-failed", None, "this computer", True)])
        self.assertEqual(define.export(self.c, "app")["triggers"][0]["event"], "stage-failed")
        with self.assertRaises(RiverError):  # a reason is asked
            self.t.rule_switch(self.c, "app", rules[0]["name"], False, actor="mark")
        self.t.rule_switch(self.c, "app", rules[0]["name"], False, "a known flaky build", actor="mark")
        define.stage_set(self.c, "app", "build", run="exit 3")
        self.runner.start(self.c, "app", rev="bad0001")
        self.passes(2)
        self.assertEqual(self.ops(), [])  # off: no fault task
        self.assertIsNone(self.runs()["build"]["fault_item"])
        with core.tx(self.c):  # the failure happened while the rule was off (the times have whole seconds)
            self.c.execute("UPDATE stage_runs SET started_at=?, ended_at=?",
                           (core.iso(core.now() - core.timedelta(seconds=5)),) * 2)
        self.t.rule_switch(self.c, "app", "t" + str(rules[0]["id"]), True, actor="mark")
        self.passes(1)
        self.assertEqual(self.ops(), [])  # on again: it acts only on what happens from now
        self.runner.start(self.c, "app", rev="bad0002")
        self.passes(2)
        self.assertEqual(len(self.ops()), 1)
        audit = [h["change"] for h in define.history(self.c, "app")]
        self.assertTrue(any(h.startswith(f"trigger {rules[0]['name']} turned off: a known flaky build") for h in audit))
        last = self.c.execute("SELECT actor, change FROM pipeline_changes ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(last[0], f"trigger {rules[0]['name']}")  # the audit log names the rule
        self.assertIn("stage build failed for bad0002", last[1])

    def test_a_rule_for_one_stage_and_a_second_rule_on_the_same_failure(self):
        r = self.t.rule_add(self.c, "app", "stage-failed", "Propose a move back as a card.", stage="beta",
                            actor="mark")
        self.assertEqual(r["when"], "Stage beta fails")
        define.stage_set(self.c, "app", "beta", run="exit 4")
        self.runner.start(self.c, "app", rev="bad0003")
        self.passes(4)
        items = self.ops()
        self.assertEqual(len(items), 1)  # both rules act on one fault task
        ctx = items[0]["context"]
        self.assertIn("Find the cause", ctx)
        self.assertIn(f"What to do (trigger rule {r['name']}): Propose a move back as a card.", ctx)
        fired = self.t.rules(self.c, "app")["fired"]
        self.assertEqual(sorted(f["result"] for f in fired), [f"joined fault #{items[0]['id']}",
                                                               f"opened fault #{items[0]['id']}"])
        self.passes(2)
        self.assertEqual(len(self.t.rules(self.c, "app")["fired"]), 2)  # each event acts once for each rule

    def test_run_and_transition_events_add_a_task_with_the_rules_words(self):
        self.t.rule_add(self.c, "app", "run-ended", "Read the deploy log and note anything odd.", stage="beta",
                        actor="mark")
        self.t.rule_add(self.c, "app", "transition-opened", "Watch the prod deploy step by step.", stage="prod",
                        actor="mark")
        with self.assertRaises(RiverError):  # these events have no default words
            self.t.rule_add(self.c, "app", "run-started", actor="mark")
        self.runner.start(self.c, "app", rev="ok00001")
        self.passes(5)
        titles = [i["title"] for i in self.ops()]
        self.assertEqual(titles, ["pipeline:app:beta a run ended for ok00001: Read the deploy log and note anything "
                                  "odd."])
        core.done(self.c, self.runs()["prod"]["gate_item"], actor="mark")
        self.passes(2)
        it = self.ops()[-1]
        self.assertTrue(it["title"].startswith("pipeline:app:prod a transition opened"))
        self.assertIn("version ok00001 went into stage prod (let in by mark)", it["context"])
        self.assertIn("What to do: Watch the prod deploy step by step.", it["context"])

    def test_a_check_that_fails_after_a_deploy(self):
        core.ops_add(self.c, "app", "home", "url", {"url": "https://example.test/"}, actor="mark")
        check = self.c.execute("SELECT id FROM checks WHERE name='home'").fetchone()[0]
        self.t.rule_add(self.c, "app", "check-failed", stage="beta", actor="mark")
        self.runner.start(self.c, "app", rev="ok00002")
        self.passes(4)

        def fault():
            with core.tx(self.c):
                self.c.execute("INSERT INTO ops_events(check_id, key, state, at, note) VALUES (?,?,?,?,?)",
                               (check, "ops:app:home", "fault", core.iso(core.now()), "HTTP 500"))
        fault()
        self.passes(1)
        it = self.ops()[-1]
        self.assertTrue(it["title"].startswith("pipeline:app:beta a check failed after a deploy for ok00002"))
        self.assertIn("check app/home failed at", it["context"])
        self.assertIn("after version ok00002 went to beta", it["context"])
        # With the check's own fault task open, the rule adds its words there.
        own = core.item_add(self.c, "app", "ops:app:home failed: HTTP 500", kind="operational", actor="maxpm",
                            goals=[])["id"]
        fault()
        self.passes(1)
        self.assertIn("propose a move back", core.item_show(self.c, own)["context"])
        self.assertEqual(len(self.ops()), 2)

    def test_who_may_change_rules_and_the_keys_of_a_task(self):
        with self.assertRaises(RiverError):  # an agent that is not the manager
            self.t.rule_add(self.c, "app", "stage-failed", stage="build", actor="w1")
        with self.assertRaises(RiverError):  # no such set
            self.t.rule_add(self.c, "app", "stage-failed", stage="build", keys="logs", actor="mark")
        with self.assertRaises(RiverError):  # only this computer runs agents now
            self.t.rule_add(self.c, "app", "stage-failed", runner="ci-1", actor="mark")
        with self.assertRaises(RiverError):
            self.t.rule_add(self.c, "app", "stage-failed", stage="nope", actor="mark")
        with core.tx(self.c):
            self.c.execute("INSERT INTO secret_sets(name, created_at) VALUES ('logs', ?)", (core.iso(core.now()),))
        base = self.t.rules(self.c, "app")["rules"][0]["name"]
        r = self.t.rule_edit(self.c, "app", base, keys="logs", actor="mark")
        self.assertEqual(r["keys"], ["logs"])
        define.stage_set(self.c, "app", "build", run="exit 5")
        self.runner.start(self.c, "app", rev="bad0004")
        self.passes(2)
        item = self.ops()[0]["id"]
        g = self.c.execute("SELECT scope, value, granted_by FROM secret_grants").fetchone()
        self.assertEqual((g[0], g[1], g[2]), ("item", str(item), f"trigger {base} (set by mark)"))
        self.assertIn("; keys logs", self.t.rules(self.c, "app")["fired"][0]["result"])
        self.t.rule_remove(self.c, "app", base, actor="mark")
        self.assertEqual(self.t.rules(self.c, "app")["rules"], [])

    def test_the_commands(self):
        out = io.StringIO()
        from river import cli

        def run(*words):
            out.truncate(0); out.seek(0)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                    mock.patch.dict(os.environ, {"MAXPM_DB": self.path, "MAXPM_QUIET": "1"}):
                code = cli.run(["--as", "mark", *words])
            return code, out.getvalue()
        code, text = run("pipeline", "trigger", "app", "add", "--when", "run-started", "--stage", "prod",
                         "--does", "Watch it.")
        self.assertEqual(code, 0, text)
        self.assertIn("Stage prod starts a run", text)
        code, text = run("pipeline", "trigger", "app", "off", "t1", "--reason", "too many tasks")
        self.assertEqual(code, 0, text)
        code, text = run("pipeline", "triggers", "app")
        self.assertIn("t1  Any stage fails  [off: too many tasks]", text)
        self.assertIn("runs on this computer; no keys", text)

    def test_a_pipeline_of_before_gets_the_rule_once(self):
        with core.tx(self.c):
            self.c.execute("DELETE FROM pipeline_triggers")
            self.c.execute("DELETE FROM meta WHERE key='pipeline_default_triggers'")
            from river.pipeline import schema
            schema.migrate(self.c)
            schema.migrate(self.c)
        self.assertEqual(len(self.t.rules(self.c, "app")["rules"]), 1)


class DefaultPipelines(Base):
    """Each deploy target of today as a default pipeline, computed from its records (#6115)."""

    def setUp(self):
        super().setUp()
        from river.pipeline import targets
        self.targets = targets
        core.register(self.c, "mark", human=True)
        core.target_add(self.c, "web-prod", "deploys web", None)
        core.project_add(self.c, "web", target="web-prod")

    def release(self, rev, title):
        i = core.item_add(self.c, "web", title, actor="mark")["id"]
        core.done(self.c, i, output="done", actor="mark", ship_it=True)
        dep = self.c.execute("SELECT MAX(id) FROM items WHERE kind='deploy'").fetchone()[0]
        core.target_cut(self.c, "web-prod", rev=rev, actor=None)
        return dep

    def hook(self, dep, event, code, rev, at):
        self.c.execute("INSERT INTO hook_runs(target, event, command, deploy_id, rev, queued_at, started_at, ended_at, "
                       "exit_code, seconds, output) VALUES ('web-prod',?,'make it',?,?,?,?,?,?,60,'last line')",
                       (event, dep, rev, at, at, at, code))

    def test_a_draft_names_its_version_not_untagged(self):
        """#6516: GitHub names a draft release untagged-<id> until its tag is pushed; the release card names the
        version that the hook printed, and links the draft."""
        dep = self.release("aaaaaaa", "A change")
        at = core.iso(core.now())
        self.c.execute("INSERT INTO hook_runs(target, event, command, deploy_id, item_id, rev, queued_at, started_at, "
                       "ended_at, exit_code, seconds, output) VALUES ('web-prod','deploy','make it',?,?,'aaaaaaa',?,?,?,"
                       "?,60,?)", (dep, dep, at, at, at, core.HOOK_YES, "github-release: v1.5.0 is prepared\n  the "
                                   "draft: https://github.com/o/r/releases/tag/untagged-3f2a"))
        self.assertEqual(self.targets._release_link(self.c, dep, []),
                         ("v1.5.0", "https://github.com/o/r/releases/tag/untagged-3f2a"))
        latest = self.targets.default_pipeline(self.c, "web-prod")["latest"]
        self.assertEqual((latest["tag"], latest["url"]), ("v1.5.0", "https://github.com/o/r/releases/tag/untagged-3f2a"))
        self.c.execute("UPDATE items SET status='done', closed_at=? WHERE id=?", (at, dep))  # published
        latest = self.targets.default_pipeline(self.c, "web-prod")["latest"]
        self.assertEqual(latest["url"], "https://github.com/o/r/releases/tag/v1.5.0")

    def test_the_running_release_comes_before_newer_cancelled_ones(self):
        """#6516 (mark 2026-11-20): the deploy card showed 'deploy #6498 cancelled' while release #6211 ran, and three
        cancelled rows sat above it in Versions in deploy."""
        from river.pipeline import target_flow
        old = core.iso(core.now() - core.timedelta(days=1))
        live = self.release("aaaaaaa", "Live change")
        self.c.execute("UPDATE items SET status='done', closed_at=? WHERE id=?", (old, live))
        run = self.release("bbbbbbb", "Running change")
        self.c.execute("UPDATE items SET status='in_progress', claimed_at=? WHERE id=?", (old, run))
        gone = []
        for n in range(3):
            i = self.c.execute("INSERT INTO items(project_id, title, kind, target, status, rank, created_at) VALUES "
                               "((SELECT id FROM projects WHERE name='web'), ?, 'deploy', 'web-prod', 'dropped', 0, ?)",
                               (f"Deploy web-prod {n}", old)).lastrowid
            gone.append(i)
        p = self.targets.default_pipeline(self.c, "web-prod")
        deploy = next(s for s in p["stages"] if s["name"] == "deploy")["last_run"]
        self.assertEqual((deploy["state"], deploy["item"]), ("running", run))
        pipe = target_flow.ensure(self.c, "web-prod")
        rows = target_flow.stage_versions(self.c, pipe, "deploy")["runs"]
        self.assertEqual([(r["item"], r["state"]) for r in rows],
                         [(run, "running"), *[(i, "cancelled") for i in reversed(gone)], (live, "passed")])
        self.assertTrue(rows[-1]["live"])
        self.assertEqual(p["latest"]["deploy"], run)  # the release card of the target's page
        # A newer release, cut while this one runs: the cards still show the running one.
        newer = self.c.execute("INSERT INTO items(project_id, title, kind, target, status, rank, created_at, cut_at, "
                               "cut_rev) VALUES ((SELECT id FROM projects WHERE name='web'), 'Deploy web-prod new', "
                               "'deploy', 'web-prod', 'open', 0, ?, ?, 'ccccccc')", (old, core.iso(core.now()))).lastrowid
        p = self.targets.default_pipeline(self.c, "web-prod")
        self.assertEqual((p["latest"]["deploy"], next(s for s in p["stages"] if s["name"] == "deploy")["last_run"]
                          ["item"]), (run, run))
        self.c.execute("UPDATE items SET status='dropped' WHERE id=?", (newer,))
        # Nothing runs: the newest release that is not cancelled.
        self.c.execute("UPDATE items SET status='done', closed_at=? WHERE id=?", (core.iso(core.now()), run))
        deploy = next(s for s in self.targets.default_pipeline(self.c, "web-prod")["stages"]
                      if s["name"] == "deploy")["last_run"]
        self.assertEqual((deploy["state"], deploy["item"]), ("passed", run))

    def test_a_target_shows_its_releases_as_stage_runs(self):
        old = core.iso(core.now() - core.timedelta(days=2))
        d1 = self.release("aaaaaaa", "First change")
        self.hook(d1, "release-cut", 0, "aaaaaaa", old)
        self.hook(d1, "deploy", 2, "aaaaaaa", old)
        self.hook(d1, "deploy", 0, "aaaaaaa", old)
        self.c.execute("UPDATE items SET status='done', closed_at=? WHERE id=?", (old, d1))
        cid = self.c.execute("INSERT INTO triggers(scope_kind, scope, name, form, config, every, fail_after, "
                             "recover_after, timeout, severity, created_at) VALUES ('target','web-prod','up','http',"
                             "'{}','1m',1,1,'10s','item',?)", (old,)).lastrowid
        self.c.execute("INSERT INTO ops_runs(check_id, key, started_at, ended_at, status) VALUES (?, 'k', ?, ?, 'ok')",
                       (cid, old, old))
        d2 = self.release("bbbbbbb", "Second change")
        p = self.targets.default_pipeline(self.c, "web-prod")
        self.assertTrue(p["default"])
        self.assertEqual([s["name"] for s in p["stages"]], ["holding", "build", "deploy", "watch"])
        self.assertEqual(p["stages"][1]["transition"]["mode"], "release")  # #6140: the arrow after holding
        self.assertEqual(p["stages"][2]["env"], "web-prod")
        self.assertEqual(p["stages"][2]["run"], None)  # no deploy hook set now: the runs keep their command
        self.assertEqual([r["version"] for r in p["releases"]], ["bbbbbbb", "aaaaaaa"])
        self.assertEqual(p["releases"][0]["stages"], {"holding": {"state": "passed", "item": d2},
                                                      "deploy": {"state": "queued", "item": d2}})
        first = {k: v["state"] for k, v in p["releases"][1]["stages"].items()}
        self.assertEqual(first, {"holding": "passed", "build": "passed", "deploy": "passed", "watch": "passed"})
        deploy = next(s for s in p["stages"] if s["name"] == "deploy")["last_run"]
        self.assertEqual(deploy["state"], "queued")  # the newest release's
        env = p["envs"][0]
        self.assertEqual((env["rev"], env["since"]), ("aaaaaaa", old))
        summary = self.targets.default_pipeline(self.c, "web-prod", watch_history=False)
        self.assertEqual(summary["stages"], p["stages"])
        # The deploy hook's runs are the steps of the first release's deploy stage: one failed, then one passed.
        runs = self.targets._release(self.c, self.targets._target(self.c, "web-prod"),
                                     self.c.execute("SELECT * FROM items WHERE id=?", (d1,)).fetchone(), None, False,
                                     False, self.targets._checks(self.c, "web-prod"))
        self.assertEqual([s["state"] for s in runs["deploy"]["step_list"]], ["failed", "passed"])
        self.assertEqual(runs["watch"]["step_list"][0]["name"], "web-prod/up")

    def test_the_watch_has_one_row_for_each_check(self):
        """#6181: toolscaled-prod's watch listed every run of every check (hundreds of rows). It has one step for each
        check: its last result, its runs and failed runs, the newest failed runs; every run of one check on request."""
        start = core.now() - core.timedelta(hours=2)
        dep = self.release("aaaaaaa", "A change")
        self.c.execute("UPDATE items SET status='done', closed_at=? WHERE id=?", (core.iso(start), dep))
        ids = {}
        for name in ("up", "errors", "shell"):
            ids[name] = self.c.execute(
                "INSERT INTO triggers(scope_kind, scope, name, form, config, every, fail_after, recover_after, timeout, "
                "severity, created_at) VALUES ('target','web-prod',?,'http','{}','1m',1,1,'10s','item',?)",
                (name, core.iso(start))).lastrowid
        for n in range(100):
            at = core.iso(start + core.timedelta(minutes=n + 1))
            for name, cid in ids.items():
                bad = name == "errors" and n in (40, 41)
                self.c.execute("INSERT INTO ops_runs(check_id, key, started_at, ended_at, status, summary) VALUES "
                               "(?, 'k', ?, ?, ?, ?)", (cid, at, at, "critical" if bad else "ok",
                                                         f"5xx at {n}" if bad else "fine"))
        w = next(s for s in self.targets.default_pipeline(self.c, "web-prod")["stages"]
                 if s["name"] == "watch")["last_run"]
        # #6505: a check that failed in the watch never fails the release: the watch runs to its end (24h), and the
        # failure is a note.
        self.assertEqual(w["state"], "running")
        rows = w["step_list"]
        self.assertEqual([(s["name"], s["state"], s["runs"], s["failed"]) for s in rows],
                         [("web-prod/errors", "passed", 100, 2), ("web-prod/shell", "passed", 100, 0),
                          ("web-prod/up", "passed", 100, 0)])  # the checks that failed first
        self.assertEqual([f["summary"] for f in rows[0]["fails"]], ["5xx at 41", "5xx at 40"])  # newest first
        self.assertEqual(w["errors"], "A note, not a failure of the release: 2 failed check run(s): web-prod/errors "
                                      "(passes now); a check in fault has its own fix item")
        runs = self.targets.watch_runs(self.c, ids["errors"], w["started_at"], w["watch_end"])
        self.assertEqual((runs["check"], len(runs["runs"])), ("web-prod/errors", 100))
        self.assertEqual(runs["runs"][58]["state"], "failed")  # newest first: run 41 is the 59th
        # The route: GET /api/pipeline-check-runs.
        from river.routes import pipeline as routes
        from urllib.parse import quote
        got = routes.check_runs(self.c, f"/api/pipeline-check-runs?check={ids['up']}&from={quote(w['started_at'])}"
                                        f"&to={quote(w['watch_end'])}")
        self.assertEqual(len(got["runs"]), 100)
        with self.assertRaises(core.RiverError):
            routes.check_runs(self.c, "/api/pipeline-check-runs?check=1")
        # A check that still fails after the watch says so.
        self.c.execute("INSERT INTO ops_runs(check_id, key, started_at, ended_at, status) VALUES (?, 'k', ?, ?, "
                       "'critical')", (ids["errors"], core.iso(core.now()), core.iso(core.now())))
        w = next(s for s in self.targets.default_pipeline(self.c, "web-prod")["stages"]
                 if s["name"] == "watch")["last_run"]
        self.assertIn("web-prod/errors (fails now)", w["errors"])
        # After its end the watch passed, with the note still there.
        self.c.execute("UPDATE items SET closed_at=? WHERE id=?", (core.iso(start - core.timedelta(hours=23)), dep))
        w = next(s for s in self.targets.default_pipeline(self.c, "web-prod")["stages"]
                 if s["name"] == "watch")["last_run"]
        self.assertEqual(w["state"], "passed")
        self.assertIn("web-prod/errors (fails now)", w["errors"])

    def hook_run(self, event, command, dep):
        hid = self.c.execute("INSERT INTO hook_runs(target, event, command, deploy_id, rev, queued_at, started_at) "
                             "VALUES ('web-prod',?,?,?, 'aaaaaaa', ?, ?)",
                             (event, command, dep, core.iso(core.now()), core.iso(core.now()))).lastrowid
        return dict(self.c.execute("SELECT * FROM hook_runs WHERE id=?", (hid,)).fetchone())

    def test_a_hook_s_step_lines_are_the_steps_of_its_stage(self):
        """#6126: toolscaled-prod's release scripts print '::step' lines with at= times; the default pipeline lists
        them, one step for each server too."""
        dep = self.release("aaaaaaa", "A change")
        lines = ["::step start image-build kind=build at=2026-11-18T10:00:00Z", "building",
                 "::step end image-build passed at=2026-11-18T10:09:00Z",
                 "::step start activate kind=deploy where=edge-1 at=2026-11-18T10:10:00Z",
                 "::step start activate kind=deploy where=cell-1 at=2026-11-18T10:10:30Z",
                 "::step end activate where=edge-1 passed at=2026-11-18T10:12:00Z",
                 "::step end activate where=cell-1 failed readyz at=2026-11-18T10:14:00Z"]
        cmd = "printf '%s\\n' " + " ".join(f"'{x}'" for x in lines)
        core._run_hook(self.c, self.hook_run("deploy", cmd, dep), cwd=self.dir.name)
        p = self.targets.default_pipeline(self.c, "web-prod")
        steps = next(s for s in p["stages"] if s["name"] == "deploy")["last_run"]["step_list"]
        self.assertEqual([(s["name"], s["host"], s["state"], s["seconds"]) for s in steps],
                         [("image-build", None, "passed", 540), ("activate", "edge-1", "passed", 120),
                          ("activate", "cell-1", "failed", 210)])
        self.assertEqual(steps[2]["note"], "readyz")
        from river import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.dict(os.environ, {"MAXPM_DB": self.path, "MAXPM_QUIET": "1"}):
            cli.run(["--as", "mark", "pipeline", "show", "web-prod"])
        self.assertIn("step activate on cell-1: failed, 210s, ended 2026-11-18T10:14:00Z", out.getvalue())

    def test_a_target_s_stages_never_branch(self):
        """#6213 (mark 2026-11-19: 'this should not be a branching deploy workflow'): the steps that #6186 named
        downloads/<step> stay steps of the deploy stage, with their full names; no stage after deploy has a row of its
        own."""
        dep = self.release("aaaaaaa", "A change")
        link = "https://github.example/actions/runs/7/job/9"
        lines = ["::step start push kind=deploy at=2026-11-19T10:00:00Z",
                 "::step end push passed at=2026-11-19T10:00:05Z",
                 "::step start downloads/tag kind=deploy at=2026-11-19T10:00:06Z",
                 "::step end downloads/tag passed at=2026-11-19T10:00:08Z",
                 "::step start downloads/build-checks kind=test at=2026-11-19T10:01:00Z",
                 "::step end downloads/build-checks passed at=2026-11-19T10:07:00Z",
                 "::step start downloads/build-mac kind=build at=2026-11-19T10:07:05Z",
                 f"::step end downloads/build-mac failed at=2026-11-19T10:10:00Z {link}"]
        cmd = "printf '%s\\n' " + " ".join(f"'{x}'" for x in lines) + "; exit 1"
        core._run_hook(self.c, self.hook_run("deploy", cmd, dep), cwd=self.dir.name)
        p = self.targets.default_pipeline(self.c, "web-prod")
        self.assertEqual([(s["name"], s.get("prev")) for s in p["stages"]],
                         [("holding", None), ("deploy", None), ("watch", None)])
        run = next(s for s in p["stages"] if s["name"] == "deploy")["last_run"]
        self.assertEqual([(s["name"], s["state"]) for s in run["step_list"]],
                         [("push", "passed"), ("downloads/tag", "passed"), ("downloads/build-checks", "passed"),
                          ("downloads/build-mac", "failed")])
        self.assertEqual(run["state"], "failed")
        self.assertEqual(set(p["releases"][0]["stages"]), {"holding", "deploy"})
        from river.pipeline import store
        with self.assertRaisesRegex(core.RiverError, "has no stage downloads"):
            store.stage_versions(self.c, "web-prod", "downloads")

    @timing.local_only("live hook process timing")
    def test_the_steps_show_while_the_hook_runs(self):
        import threading
        dep = self.release("bbbbbbb", "Another change")
        run = self.hook_run("release-cut", "echo '::step start preview'; sleep 3; echo '::step end preview passed'", dep)
        done = []

        def go():
            c = core.connect(self.path)
            try:
                core._run_hook(c, run, cwd=self.dir.name)
            finally:
                c.close()
                done.append(1)
        threading.Thread(target=go, daemon=True).start()
        timing.wait_for(lambda: self.c.execute("SELECT state FROM hook_steps WHERE hook_run=?", (run["id"],)).fetchone(),
                        what="the step as it starts")
        self.assertFalse(done)  # it shows while the hook still runs
        self.assertEqual(self.c.execute("SELECT state FROM hook_steps").fetchone()[0], "running")
        timing.wait_for(lambda: done, timeout=20, what="the hook's end")
        self.assertEqual(self.c.execute("SELECT state FROM hook_steps").fetchone()[0], "passed")

    def test_review_and_the_person_s_yes_are_stages_when_the_target_has_them(self):
        core.config_set(self.c, "review", "on", project="web")
        core.config_set(self.c, "release_approval", "person", project="web")
        self.release("ccccccc", "A change")
        names = [s["name"] for s in self.targets.default_pipeline(self.c, "web-prod")["stages"]]
        self.assertEqual(names, ["holding", "review", "approval", "deploy", "watch"])
        approval = next(s for s in self.targets.default_pipeline(self.c, "web-prod")["stages"]
                        if s["name"] == "approval")
        self.assertEqual(approval["transition"]["mode"], "yes")

    def test_list_and_show_and_a_target_refuses_a_replacement_pipeline(self):
        from river import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.dict(os.environ, {"MAXPM_DB": self.path, "MAXPM_QUIET": "1"}):
            cli.run(["--as", "mark", "pipeline", "list"])
            cli.run(["--as", "mark", "pipeline", "show", "web-prod"])
        self.assertIn("web-prod  (a deploy target: its default pipeline)", out.getvalue())
        self.assertIn("(the default pipeline of deploy target web-prod, from its releases)", out.getvalue())
        define.pipeline_add(self.c, "web", project="web")
        with self.assertRaisesRegex(RiverError, "owns its release flow"):
            define.env_set(self.c, "web", "prod", target="web-prod")
        self.assertEqual([t["name"] for t in self.targets.default_list(self.c)], ["web-prod"])
        with self.assertRaises(RiverError):
            self.targets.default_pipeline(self.c, "nowhere")

    def test_the_hook_supplies_the_draft_build_and_approval_without_a_github_deploy_stage(self):
        from river.routes import pipeline
        core.target_hook(self.c, "web-prod", "deploy", "build the draft", actor="mark")
        dep = self.release("aaaaaaa", "A change")
        at = core.iso(core.now())
        self.hook(dep, "deploy", core.HOOK_YES, "aaaaaaa", at)
        run = self.c.execute("SELECT MAX(id) FROM hook_runs WHERE deploy_id=?", (dep,)).fetchone()[0]
        self.c.execute("INSERT INTO hook_steps(hook_run, name, kind, state, started_at, ended_at) "
                       "VALUES (?, 'build-mac', 'build', 'passed', ?, ?)", (run, at, at))
        approval = core._draft_yes(self.c, dep, "The draft is ready")
        d = pipeline.pipeline(self.c, "/api/pipeline/web-prod")
        self.assertEqual([s["name"] for s in d["stages"]], ["holding", "deploy", "release tag", "watch"])
        self.assertEqual(d["stages"][1]["run"], "build the draft")
        self.assertEqual(d["stages"][1]["last_run"]["step_list"][0]["name"], "build-mac")
        self.assertEqual((d["stages"][2]["last_run"]["yes"], d["stages"][2]["last_run"]["word"]),
                         (approval, "waits for your yes"))
        self.assertIsNone(d["envs"][0]["rev"])

    @timing.local_only("100 ms latency budget")
    def test_ten_releases_read_under_100_ms(self):
        for n in range(12):
            d = self.release(f"{n:07x}", f"Change {n}")
            self.c.execute("UPDATE items SET status='done', closed_at=? WHERE id=?",
                           (core.iso(core.now() - core.timedelta(hours=12 - n)), d))
        t = time.monotonic()
        p = self.targets.default_pipeline(self.c, "web-prod")
        self.assertLess(time.monotonic() - t, timing.limit(0.1))
        self.assertEqual(len(p["releases"]), self.targets.RELEASES)
        with mock.patch.object(self.targets, "_watch", wraps=self.targets._watch) as watch:
            summary = self.targets.default_pipeline(self.c, "web-prod", watch_history=False)
        self.assertEqual(watch.call_count, 1)
        self.assertEqual(summary["stages"], p["stages"])


class Prompt(Base):
    """The prompt that analyzes the failed tests of a stage run (#6116)."""

    def setUp(self):
        super().setUp()
        from river.pipeline import prompt
        self.prompt = prompt
        self.toolscaled()

    def run_with(self, rev, failing, state):
        run = store.run_start(self.c, "toolscaled", "test", version=rev)
        cases = "".join(f'<testcase classname="api" name="{n}">' + (f'<failure message="{n} broke">trace {n}</failure>'
                        if n in failing else "") + "</testcase>" for n in ("a", "b", "c"))
        junit.ingest_junit(self.c, run, xml=f'<testsuite name="s">{cases}</testsuite>')
        store.run_finish(self.c, run, state)
        return run

    def test_version_last_pass_history_and_where_the_output_is(self):
        self.run_with("1111111", [], "passed")
        self.run_with("2222222", ["b"], "failed")
        self.run_with("3333333", [], "passed")
        run = self.run_with("4444444", ["a", "b"], "failed")
        p = self.prompt.failure_prompt(self.c, run)
        text = p["text"]
        self.assertIn("failed for version 4444444 (run #%d): 2 of 3 tests failed." % run, text)
        self.assertIn("The last version that passed this stage: 3333333.", text)
        self.assertIn("1. api.a: a broke. new: passed the 3 earlier run(s).", text)
        self.assertIn("2. api.b: b broke. flaky: failed in 1 of the 3 earlier run(s).", text)
        self.assertIn("   trace a", text)
        self.assertIn(f"The full output: maxpm pipeline run {run}", text)
        self.assertEqual((p["failed"], p["tests"], p["passed"]), (2, 3, "3333333"))
        out = self.prompt.run_text(self.c, run)
        self.assertIn("failed: api.a: a broke", out)

    def test_the_commits_between_from_git(self):
        import subprocess
        folder = os.path.join(self.dir.name, "repo")
        os.makedirs(folder)
        git = lambda *a: subprocess.run(["git", "-C", folder, *a], capture_output=True, text=True, check=True).stdout
        git("init", "-q")
        revs = []
        for n in range(3):
            git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", f"change {n}")
            revs.append(git("rev-parse", "HEAD").strip())
        core.project_add(self.c, "toolscaled", path=folder)
        self.run_with(revs[0], [], "passed")
        run = self.run_with(revs[2], ["a"], "failed")
        text = self.prompt.failure_prompt(self.c, run)["text"]
        self.assertIn(f"git log {revs[0]}..{revs[2]}, 2 commit(s):", text)
        self.assertIn("change 2", text)


class FakeGitHub:
    """The GitHub REST API as the adapter calls it: answers by (method, path) and keeps each call."""

    def __init__(self):
        self.calls, self.routes = [], {}

    def __call__(self, method, url, headers, body):
        path = url.replace("https://api.github.com", "")
        self.calls.append((method, path, json.loads(body) if body else None))
        for (m, prefix), answer in sorted(self.routes.items(), key=lambda kv: -len(kv[0][1])):  # the longest path first
            if m == method and path.startswith(prefix):
                status, out = answer(path, body) if callable(answer) else answer
                return status, out if isinstance(out, bytes) else json.dumps(out).encode()
        return 404, b'{"message": "Not Found"}'


def gh_run(n, sha, status="completed", conclusion="success", title="Fix it", at="2026-11-18T10:00:00Z"):
    return {"id": n, "head_sha": sha, "head_branch": "main", "status": status, "conclusion": conclusion,
            "display_title": title, "html_url": f"https://github.com/o/r/actions/runs/{n}", "run_attempt": 1,
            "created_at": at, "run_started_at": at, "updated_at": at}


def junit_zip(xml):
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("report.xml", xml)
    return buf.getvalue()


def in_process_plugin(fetch):
    """A plugin.RUNNER that runs a shipped Python plugin in this process, with fetch as its HTTP: the recorded answers
    of a fake API (#6160). The plugin answers as its process would: (exit code, the JSON answer, stderr)."""
    import importlib.util
    mods = {}

    def run(argv, folder, request, timeout):
        if folder not in mods:
            spec = importlib.util.spec_from_file_location("maxpm_plugin_" + os.path.basename(folder),
                                                          os.path.join(folder, "plugin.py"))
            mods[folder] = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mods[folder])
        mod = mods[folder]
        try:
            out, code = mod.handle(json.loads(json.dumps(request)), fetch=fetch), 0
        except mod.Failed as e:
            out, code = {"error": str(e)}, 1
        return code, json.dumps(out).encode(), ""
    return run


def plugin_hooks(test, fetch):
    """The GitHub plugin (and GitLab, Pulumi adapters) with a fake API and the secret 'test-token'."""
    import types
    from river.pipeline import forge, plugin
    old = (plugin.RUNNER, forge.secret)
    plugin.RUNNER, forge.secret = in_process_plugin(fetch), lambda conn, name, use=None: "test-token"
    test.addCleanup(lambda: setattr(plugin, "RUNNER", old[0]) or setattr(forge, "secret", old[1]))
    return types.SimpleNamespace(poll=plugin.poll)


class GitHub(Base):
    """The GitHub source, the shipped plugin github (#6119, #6160): runs, jobs, JUnit artifacts and deployments read
    into events; a gated workflow starts after its gate; cancel. The plugin runs in this process against the fake API."""

    def setUp(self):
        super().setUp()
        from river.pipeline import runner
        self.runner = runner
        self.api = FakeGitHub()
        self.gh = plugin_hooks(self, self.api)
        runner.SYNC["on"] = True
        self.addCleanup(runner.SYNC.update, on=False)
        core.project_add(self.c, "maximizepm")
        core.register(self.c, "mark", human=True)
        define.pipeline_add(self.c, "mpm", project="maximizepm")
        define.env_set(self.c, "mpm", "beta")
        define.stage_set(self.c, "mpm", "tests", "test", workflow="tests.yml")
        define.stage_set(self.c, "mpm", "beta", "deploy", env="beta", workflow="desktop-release.yml", gate="person mark")
        define.forge_set(self.c, "mpm", "github", repo="o/r", secret="gh-token")
        self.p = lambda: self.c.execute("SELECT * FROM pipelines WHERE name='mpm'").fetchone()
        self.runs = {"tests.yml": [], "desktop-release.yml": []}
        wf = lambda name: lambda path, body: (200, {"workflow_runs": self.runs[name]})
        self.api.routes[("GET", "/repos/o/r/actions/workflows/tests.yml/runs")] = wf("tests.yml")
        self.api.routes[("GET", "/repos/o/r/actions/workflows/desktop-release.yml/runs")] = wf("desktop-release.yml")
        self.api.routes[("GET", "/repos/o/r/deployments")] = (200, [])

    def jobs(self, n, *jobs):
        self.api.routes[("GET", f"/repos/o/r/actions/runs/{n}/jobs")] = (200, {"jobs": [
            {"id": n * 10 + i, "name": name, "status": "completed", "conclusion": c, "started_at": "2026-11-18T10:01:00Z",
             "completed_at": "2026-11-18T10:04:00Z", "html_url": "u"} for i, (name, c) in enumerate(jobs)]})

    def stage_runs(self):
        return {r["stage"]: r for r in self.c.execute(
            "SELECT r.*, s.name stage FROM stage_runs r JOIN pipeline_stages s ON s.id=r.stage_id ORDER BY r.id")}

    def test_linked_github_checks_keep_one_target_flow_and_its_release_record(self):
        from river.pipeline import target_flow
        from river.routes import pipeline
        core.target_add(self.c, "site", "", None)
        core.project_add(self.c, "web", target="site")
        core.target_cadence(self.c, "site", "1w", "mark")
        i = core.item_add(self.c, "web", "First", actor="mark")["id"]
        core.done(self.c, i, output="done", actor="mark", ship_it=True)
        core.target_cut(self.c, "site", rev="1234567", actor="mark")
        old = core.iso(core.now() - core.timedelta(days=2))
        self.c.execute("UPDATE items SET status='done', closed_at=? WHERE kind='deploy' AND target='site'", (old,))
        i = core.item_add(self.c, "web", "Second", actor="mark")["id"]
        core.done(self.c, i, output="done", actor="mark", ship_it=True)

        store.deployed(self.c, "mpm", "beta", "f" * 40, source="github")
        with self.assertRaisesRegex(RiverError, "only GitHub test workflows"):
            define.env_set(self.c, "mpm", "beta", target="site", actor="mark")
        # A legacy link can already have a deploy stage. It contributes test runs only.
        self.c.execute("UPDATE pipeline_envs SET target='site' WHERE name='beta'")
        self.assertTrue(target_flow.ensure(self.c, "site"))
        with self.assertRaisesRegex(RiverError, "supplies GitHub checks"):
            self.runner.start(self.c, "mpm", rev="a" * 40, actor="mark")

        # A newer branch run was collected under the old model. It stays in history but never becomes the check.
        store.run_start(self.c, "mpm", "tests", "b" * 40, key="gh-run-999", source="github")
        self.runs["tests.yml"] = [
            dict(gh_run(104, "d" * 40), event="push", head_repository={"full_name": "fork/r"}),
            dict(gh_run(103, "c" * 40, conclusion="action_required"), event="pull_request"),
            dict(gh_run(102, "b" * 40, conclusion="failure"), event="push", head_branch="feature"),
            dict(gh_run(101, "a" * 40), event="push"),
        ]
        self.jobs(101, ("test", "success"))
        self.api.routes[("GET", "/repos/o/r/actions/runs/101/artifacts")] = (200, {"artifacts": []})
        self.assertEqual(self.gh.poll(self.c, self.p())["runs"], 1)
        self.assertTrue(any("branch=main&event=push" in call[1] for call in self.api.calls))
        self.assertFalse(any("desktop-release.yml/runs" in call[1] for call in self.api.calls))
        self.assertFalse(any("/deployments" in call[1] for call in self.api.calls))
        self.assertNotIn("beta", self.stage_runs())  # test success never schedules the desktop workflow
        payload = pipeline.deploy(self.c, "/api/deploy")
        self.assertEqual(payload["aliases"], {"mpm": "site"})
        self.assertEqual(pipeline.pipeline(self.c, "/api/pipeline/mpm")["name"], "site")
        d = payload["pipelines"]
        self.assertEqual([p["name"] for p in d], ["site"])
        self.assertEqual((d[0]["envs"][0]["rev"], d[0]["envs"][0]["since"]), ("1234567", old))
        self.assertEqual(d[0]["release"]["tasks"], 1)
        check, = d[0]["checks"]
        self.assertEqual((check["branch"], check["last_run"]["key"]), ("main", "gh-run-101"))
        define.stage_set(self.c, "mpm", "beta", remove=True, actor="mark")
        result = define.env_set(self.c, "mpm", "beta", target="site", actor="mark")
        self.assertIn("GitHub tests join the release flow of site", result["message"])

    def test_a_push_run_its_jobs_and_tests_then_the_gate_then_github_runs_the_next_stage(self):
        self.runs["tests.yml"] = [gh_run(101, "a" * 40)]
        self.jobs(101, ("test (3.10)", "success"), ("standalone", "success"))
        self.api.routes[("GET", "/repos/o/r/actions/runs/101/artifacts")] = (200, {"artifacts": [
            {"name": "junit-3.13", "expired": False, "archive_download_url": "https://api.github.com/download/1"}]})
        self.api.routes[("GET", "/download/1")] = (200, junit_zip(
            '<testsuite name="t"><testcase classname="c" name="a"/><testcase classname="c" name="b"/></testsuite>'))
        out = self.gh.poll(self.c, self.p())
        self.assertEqual((out["runs"], out["tests"]), (1, 1))
        runs = self.stage_runs()
        t = store.run_show(self.c, runs["tests"]["id"])
        self.assertEqual((t["state"], t["version"], t["url"]), ("passed", "a" * 40, "https://github.com/o/r/actions/runs/101"))
        self.assertEqual([s["name"] for s in t["step_list"]], ["test (3.10)", "standalone"])
        self.assertEqual(t["tests"], {"passed": 2})
        self.assertEqual(runs["beta"]["state"], "queued")  # handed on: it waits at its gate
        n = len(self.c.execute("SELECT * FROM pipeline_events").fetchall())
        self.gh.poll(self.c, self.p())  # read again: nothing new
        self.assertEqual(len(self.c.execute("SELECT * FROM pipeline_events").fetchall()), n)
        # The person's yes opens the gate; MaximizePM starts the workflow with the run's id and commit.
        self.runner.serve_pass(self.c)
        card = self.stage_runs()["beta"]["gate_item"]
        core.done(self.c, card, actor="mark")
        self.c.execute("UPDATE pipelines SET polled_at=? WHERE name='mpm'", (core.iso(core.now()),))  # no read now
        self.api.routes[("POST", "/repos/o/r/actions/workflows/desktop-release.yml/dispatches")] = (204, b"")
        self.runner.serve_pass(self.c)
        beta = self.stage_runs()["beta"]
        sent = [c for c in self.api.calls if c[0] == "POST"][-1]
        self.assertEqual(sent[2], {"ref": "main", "inputs": {"maxpm_run": str(beta["id"]), "maxpm_rev": "a" * 40}})
        self.assertEqual(beta["runner"], "forge:github")
        self.runner.serve_pass(self.c)  # it is started: not again
        self.assertEqual(len([c for c in self.api.calls if c[0] == "POST"]), 1)
        # Its run on GitHub names our run: the two are one; when it passes, beta runs that version.
        self.runs["desktop-release.yml"] = [gh_run(202, "a" * 40, title=f"maxpm run {beta['id']}")]
        self.jobs(202, ("mac", "success"))
        self.api.routes[("GET", "/repos/o/r/actions/runs/202/artifacts")] = (200, {"artifacts": []})
        self.gh.poll(self.c, self.p())
        beta = self.stage_runs()["beta"]
        self.assertEqual((beta["key"], beta["state"], beta["approved_by"]), ("gh-run-202", "passed", "mark"))
        self.assertEqual(store.show(self.c, "mpm")["envs"][0]["rev"], "a" * 40)

    def test_pull_request_runs_are_skipped_and_action_required_does_not_fail(self):
        # External fork PR runs come back with event 'pull_request' and conclusion 'action_required' (#6473):
        # the plugin skips pull-request runs and maps action_required to skipped rather than failed.
        self.runs["tests.yml"] = [
            dict(gh_run(103, "c" * 40, conclusion="action_required"), event="pull_request"),
            dict(gh_run(102, "b" * 40, conclusion="failure"), event="pull_request_target"),
            dict(gh_run(101, "a" * 40, conclusion="success"), event="push"),
        ]
        self.jobs(101, ("test (3.10)", "success"))
        self.api.routes[("GET", "/repos/o/r/actions/runs/101/artifacts")] = (200, {"artifacts": []})
        out = self.gh.poll(self.c, self.p())
        self.assertEqual(out["runs"], 1)
        listed = [c[1] for c in self.api.calls if c[1].startswith("/repos/o/r/actions/workflows/tests.yml/runs")]
        self.assertIn("exclude_pull_requests=true", listed[-1])
        keys = [r["key"] for r in self.c.execute(
            "SELECT key FROM stage_runs WHERE stage_id=(SELECT id FROM pipeline_stages WHERE name='tests')").fetchall()]
        self.assertEqual(keys, ["gh-run-101"])
        # A non-PR run held for approval (action_required) records as skipped, never failed.
        self.runs["tests.yml"] = [dict(gh_run(104, "d" * 40, conclusion="action_required"), event="workflow_dispatch")]
        self.jobs(104)
        self.api.routes[("GET", "/repos/o/r/actions/runs/104/artifacts")] = (200, {"artifacts": []})
        self.c.execute("UPDATE pipelines SET polled_at=NULL")
        self.gh.poll(self.c, self.p())
        r104 = self.c.execute("SELECT state, errors FROM stage_runs WHERE key='gh-run-104'").fetchone()
        self.assertEqual((r104["state"], r104["errors"]), ("skipped", "action_required"))

    def test_a_stored_held_pr_run_is_not_the_last_run_and_the_arrow_names_github(self):
        # #6482: a run stored before #6473 (an outside PR's run that GitHub held: errors action_required) is not the
        # stage's last run; the stage shows the run of main before it. The arrow names GitHub, never 'forge'.
        self.runs["tests.yml"] = [gh_run(101, "a" * 40)]
        self.jobs(101, ("test (3.10)", "success"))
        self.api.routes[("GET", "/repos/o/r/actions/runs/101/artifacts")] = (200, {"artifacts": []})
        self.gh.poll(self.c, self.p())
        tests_id = self.c.execute("SELECT id FROM pipeline_stages WHERE name='tests'").fetchone()["id"]
        self.c.execute("INSERT INTO stage_runs(pipeline_id, stage_id, key, state, ended_at, source, errors) "
                       "VALUES (?,?,?,?,?,?,?)", (self.p()["id"], tests_id, "gh-run-99", "failed",
                                                  "2026-11-20T20:36:27Z", "github", "action_required"))
        tests = next(s for s in store.show(self.c, "mpm")["stages"] if s["name"] == "tests")
        self.assertEqual((tests["last_run"]["state"], tests["last_run"]["version"]), ("passed", "a" * 40))
        define.stage_set(self.c, "mpm", "beta", gate="forge")
        beta = next(s for s in store.show(self.c, "mpm")["stages"] if s["name"] == "beta")["transition"]
        self.assertEqual((beta["host"], beta["text"]), ("GitHub", "GitHub starts it by itself (a push or a tag)"))
        self.assertNotIn("forge", json.dumps(beta["text"]) + beta["state_text"])
        from river.pipeline import transitions
        self.assertEqual(transitions.parse_mode("host"), ("forge", None))  # the page's word for the mode

    def test_a_refused_artifact_download_skips_its_report_and_never_reads_as_a_refused_token(self):
        # The storage host of the artifact answers 401 (#6459): the run is read, its report is skipped, and the
        # pipeline keeps no error that the token check reads as GitHub's refusal of the token.
        self.runs["tests.yml"] = [gh_run(101, "a" * 40)]
        self.jobs(101, ("test (3.10)", "success"))
        self.api.routes[("GET", "/repos/o/r/actions/runs/101/artifacts")] = (200, {"artifacts": [
            {"name": "junit-3.13", "expired": False, "archive_download_url": "https://api.github.com/download/1"}]})
        self.api.routes[("GET", "/download/1")] = (401, b"<Error><Code>InvalidAuthenticationInfo</Code></Error>")
        out = self.gh.poll(self.c, self.p())
        self.assertEqual((out["runs"], out["tests"]), (1, 0))
        self.assertFalse(self.p()["poll_error"])
        self.assertEqual(self.stage_runs()["tests"]["state"], "passed")

    def test_the_token_goes_only_to_the_host_it_was_sent_to(self):
        import importlib.util
        import urllib.request
        path = os.path.join(os.path.dirname(store.__file__), "plugins", "github", "plugin.py")
        spec = importlib.util.spec_from_file_location("maxpm_plugin_github_redirect", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        req = urllib.request.Request("https://api.github.com/repos/o/r/actions/artifacts/1/zip",
                                     headers={"Authorization": "Bearer t", "User-Agent": "maxpm-pipeline"})
        h = mod._Redirect()
        blob = h.redirect_request(req, None, 302, "Found", {}, "https://blob.core.windows.net/x?sig=s")
        self.assertNotIn("Authorization", blob.headers)
        self.assertEqual(blob.headers["User-agent"], "maxpm-pipeline")
        same = h.redirect_request(req, None, 302, "Found", {}, "https://api.github.com/repos/o/r/other")
        self.assertEqual(same.headers["Authorization"], "Bearer t")

    def test_a_forge_gate_waits_for_the_tag_run_and_takes_it(self):
        define.stage_set(self.c, "mpm", "beta", gate="forge")
        self.runs["tests.yml"] = [gh_run(101, "a" * 40)]
        self.jobs(101)
        self.api.routes[("GET", "/repos/o/r/actions/runs/101/artifacts")] = (200, {"artifacts": []})
        self.gh.poll(self.c, self.p())
        self.c.execute("UPDATE pipelines SET polled_at=?", (core.iso(core.now()),))
        out = self.runner.serve_pass(self.c)
        beta = self.stage_runs()["beta"]
        self.assertIn("GitHub starts it by itself", out["waiting"][beta["id"]])  # #6482: the host's own name
        self.assertFalse([c for c in self.api.calls if c[0] == "POST"])  # MaximizePM starts nothing
        self.runs["desktop-release.yml"] = [gh_run(404, "a" * 40, title="v0.9.0")]  # the manager's tag
        self.jobs(404)
        self.api.routes[("GET", "/repos/o/r/actions/runs/404/artifacts")] = (200, {"artifacts": []})
        self.gh.poll(self.c, self.p())
        runs = self.c.execute("SELECT id, key, state FROM stage_runs WHERE stage_id=(SELECT id FROM pipeline_stages "
                              "WHERE name='beta')").fetchall()
        self.assertEqual([(r["id"], r["key"], r["state"]) for r in runs], [(beta["id"], "gh-run-404", "passed")])

    def test_a_newer_version_replaces_an_older_one_at_the_gate(self):
        self.runs["tests.yml"] = [gh_run(102, "b" * 40, at="2026-11-18T11:00:00Z"), gh_run(101, "a" * 40)]
        for n in (101, 102):
            self.jobs(n)
            self.api.routes[("GET", f"/repos/o/r/actions/runs/{n}/artifacts")] = (200, {"artifacts": []})
        self.gh.poll(self.c, self.p())
        beta = self.c.execute("SELECT r.state, v.rev, r.errors FROM stage_runs r JOIN pipeline_stages s ON s.id=r.stage_id "
                              "JOIN pipeline_versions v ON v.id=r.version_id WHERE s.name='beta' ORDER BY r.id").fetchall()
        self.assertEqual([(b["state"], b["rev"][0]) for b in beta], [("cancelled", "a"), ("queued", "b")])
        self.assertIn("a newer version came", beta[0]["errors"])

    def test_a_refused_start_fails_the_run_and_a_read_error_stays_on_the_pipeline(self):
        define.stage_set(self.c, "mpm", "tests", gate="auto")
        self.api.routes[("POST", "/repos/o/r/actions/workflows/tests.yml/dispatches")] = (
            422, {"message": "Unexpected inputs provided: [\"maxpm_run\"]"})
        self.runner.start(self.c, "mpm", rev="c" * 40)
        self.api.routes[("GET", "/repos/o/r/actions/workflows/tests.yml/runs")] = (500, {"message": "boom"})
        self.runner.serve_pass(self.c)
        t = self.stage_runs()["tests"]
        self.assertEqual(t["state"], "failed")
        self.assertIn("Unexpected inputs provided", t["errors"])
        self.assertIn("GitHub answered 500", self.p()["poll_error"])

    def test_deployments_and_cancel(self):
        self.api.routes[("GET", "/repos/o/r/deployments")] = (200, [{"id": 7, "sha": "d" * 40}])
        self.api.routes[("GET", "/repos/o/r/deployments/7/statuses")] = (200, [{"id": 70, "state": "success",
                                                                                 "created_at": "2026-11-18T12:00:00Z"}])
        self.gh.poll(self.c, self.p())
        self.assertEqual(store.show(self.c, "mpm")["envs"][0]["rev"], "d" * 40)
        self.runs["tests.yml"] = [gh_run(303, "e" * 40, status="in_progress", conclusion=None)]
        self.jobs(303, ("test", None))
        self.c.execute("UPDATE pipelines SET polled_at=NULL")
        self.gh.poll(self.c, self.p())
        run = self.stage_runs()["tests"]
        self.assertEqual(run["state"], "running")
        self.api.routes[("POST", "/repos/o/r/actions/runs/303/cancel")] = (202, {})
        with self.assertRaises(RiverError):  # an agent may not
            core.register(self.c, "w1")
            self.runner.stop(self.c, run["id"], actor="w1")
        self.assertEqual(self.runner.stop(self.c, run["id"], actor="mark")["stopped"], "on GitHub")
        self.assertEqual(self.api.calls[-1][:2], ("POST", "/repos/o/r/actions/runs/303/cancel"))

    def test_run_again_an_older_commit_and_the_forge_command(self):
        r = self.runner.run_again(self.c, "mpm", "beta", "f" * 40, actor="mark")
        row = self.c.execute("SELECT approved_by FROM stage_runs WHERE id=?", (r["run"],)).fetchone()
        self.assertEqual(row["approved_by"], "mark")
        with self.assertRaises(RiverError):
            define.forge_set(self.c, "mpm", "github", repo="not a repo", secret="x")
        define.forge_set(self.c, "mpm", "none")
        self.assertIsNone(self.p()["forge"])
        self.assertIn("forge none", define.history(self.c, "mpm")[0]["change"])


class JUnitRunner(unittest.TestCase):
    def test_the_report_of_tests_junit_run_reads_back(self):
        """tests/junit_run.py writes the report that tests.yml uploads and the GitHub adapter reads (#6119)."""
        from tests import junit_run
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "r.xml")
            junit_run.write([("tests.test_x.A.test_ok", "passed", 0.5, None),
                             ("tests.test_x.A.test_bad", "failure", 1.0, "Traceback\nAssertionError: 2 != 3"),
                             ("tests.test_y.B.test_skip", "skipped", 0.0, "no Chrome")], path, "py3.13")
            with open(path, "rb") as f:
                suites = junit.parse(f.read())
        tests = {t["name"]: t for s in suites for t in s["tests"]}
        self.assertEqual((tests["test_bad"]["state"], tests["test_bad"]["message"], tests["test_bad"]["suite"]),
                         ("failed", "AssertionError: 2 != 3", "py3.13 tests.test_x.A"))
        self.assertEqual((tests["test_ok"]["state"], tests["test_skip"]["state"]), ("passed", "skipped"))


class FakeForge(FakeGitHub):
    """Gitea or Forgejo at https://git.example.com: the API under /api/v1, the token as 'token <value>'."""

    def __call__(self, method, url, headers, body):
        assert headers["Authorization"] == "token test-token"
        return super().__call__(method, url.replace("https://git.example.com/api/v1", "https://api.github.com"),
                                headers, body)


class GiteaForgejo(Base):
    """Gitea and Forgejo (#6122): the plugin github with their address, token and run forms. Gitea lists runs in
    GitHub's form; Forgejo in its own (title, commit_sha, one status word) and has no jobs, artifacts or cancel."""

    def setUp(self):
        super().setUp()
        from river.pipeline import runner
        self.runner = runner
        self.api = FakeForge()
        self.gh = plugin_hooks(self, self.api)
        runner.SYNC["on"] = True
        self.addCleanup(runner.SYNC.update, on=False)
        core.project_add(self.c, "shop")
        core.register(self.c, "mark", human=True)
        define.pipeline_add(self.c, "shop", project="shop")
        define.env_set(self.c, "shop", "prod")
        define.stage_set(self.c, "shop", "test", "test", workflow="test.yml")
        define.stage_set(self.c, "shop", "prod", "deploy", env="prod", workflow="deploy.yml", gate="person mark")
        self.runs = []
        self.api.routes[("GET", "/repos/o/shop/actions/runs?limit")] = lambda path, body: (
            200, {"workflow_runs": self.runs, "total_count": len(self.runs)})

    def forge(self, name):
        define.forge_set(self.c, "shop", name, repo="o/shop", secret="tok", url="https://git.example.com")

    def poll(self):
        self.gh.poll(self.c, self.c.execute("SELECT * FROM pipelines WHERE name='shop'").fetchone())

    def stage_runs(self):
        return {r["stage"]: r for r in self.c.execute(
            "SELECT r.*, s.name stage FROM stage_runs r JOIN pipeline_stages s ON s.id=r.stage_id ORDER BY r.id")}

    def test_gitea_runs_and_jobs_then_the_gate_starts_the_deploy_workflow(self):
        self.forge("gitea")
        self.runs = [dict(gh_run(7, "a" * 40), path=".gitea/workflows/test.yml@refs/heads/main", started_at=None),
                     dict(gh_run(8, "f" * 40), path=".gitea/workflows/other.yml@refs/heads/main")]
        self.api.routes[("GET", "/repos/o/shop/actions/runs/7/jobs")] = (200, {"jobs": [
            {"id": 70, "name": "unit", "status": "completed", "conclusion": "success",
             "started_at": "2026-11-18T10:01:00Z", "completed_at": "2026-11-18T10:03:00Z"}]})
        self.poll()
        runs = self.stage_runs()
        self.assertEqual((runs["test"]["key"], runs["test"]["state"]), ("gh-run-7", "passed"))
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM stage_runs").fetchone()[0], 2)  # other.yml is no stage
        self.assertEqual(self.c.execute("SELECT DISTINCT source FROM pipeline_events").fetchall()[0][0], "gitea")
        self.assertEqual([s["name"] for s in store.run_show(self.c, runs["test"]["id"])["step_list"]], ["unit"])
        self.c.execute("UPDATE pipelines SET polled_at=?", (core.iso(core.now()),))
        self.runner.serve_pass(self.c)  # the gate asks the person
        core.done(self.c, self.stage_runs()["prod"]["gate_item"], actor="mark")
        self.api.routes[("POST", "/repos/o/shop/actions/workflows/deploy.yml/dispatches")] = (204, b"")
        self.runner.serve_pass(self.c)
        self.assertEqual(self.api.calls[-1], ("POST", "/repos/o/shop/actions/workflows/deploy.yml/dispatches",
                                              {"ref": "main", "inputs": {"maxpm_run": str(runs["prod"]["id"]),
                                                                         "maxpm_rev": "a" * 40}}))
        self.assertEqual(self.stage_runs()["prod"]["runner"], "forge:gitea")

    def test_forgejo_runs_and_a_run_again_with_an_older_commit(self):
        self.forge("forgejo")
        fj = lambda n, sha, status: {"id": n, "title": f"maxpm run x {sha[:3]}", "commit_sha": sha, "status": status,
                                     "workflow_id": "test.yml", "prettyref": "main", "html_url": f"u/{n}",
                                     "created": "2026-11-18T10:00:00Z", "started": "2026-11-18T10:00:10Z",
                                     "stopped": "2026-11-18T10:02:00Z" if status != "running" else None}
        self.runs = [fj(3, "b" * 40, "running"), fj(2, "a" * 40, "failure")]
        self.poll()  # no jobs and no artifacts on this Forgejo: 404, and the read goes on
        states = dict(self.c.execute("SELECT key, state FROM stage_runs").fetchall())
        self.assertEqual(states, {"gh-run-2": "failed", "gh-run-3": "running"})
        # This Forgejo cannot stop a run by its API: the refusal says where to stop it.
        running = self.c.execute("SELECT id FROM stage_runs WHERE key='gh-run-3'").fetchone()[0]
        with self.assertRaisesRegex(RiverError, "cannot stop a run by its API; stop run 3 on its Actions page"):
            self.runner.stop(self.c, running, actor="mark")
        self.runs[0] = fj(3, "b" * 40, "success")
        self.poll()
        # Run the test stage again for the older commit: the forge's workflow_dispatch with that commit.
        r = self.runner.run_again(self.c, "shop", "test", "a" * 40, actor="mark")
        self.api.routes[("POST", "/repos/o/shop/actions/workflows/test.yml/dispatches")] = (204, b"")
        self.c.execute("UPDATE pipelines SET polled_at=?", (core.iso(core.now()),))
        self.runner.serve_pass(self.c)
        self.assertEqual(self.api.calls[-1][2]["inputs"], {"maxpm_run": str(r["run"]), "maxpm_rev": "a" * 40})
        self.assertEqual(self.c.execute("SELECT runner FROM stage_runs WHERE id=?", (r["run"],)).fetchone()[0],
                         "forge:forgejo")

    def test_the_forge_command_needs_the_address(self):
        with self.assertRaisesRegex(RiverError, "--url https://<host>"):
            define.forge_set(self.c, "shop", "forgejo", repo="o/shop", secret="tok")
        self.forge("forgejo")
        self.assertEqual(define.export(self.c, "shop")["forge"]["url"], "https://git.example.com")


class FakeGitLab(FakeGitHub):
    def __call__(self, method, url, headers, body):
        assert headers["PRIVATE-TOKEN"] == "test-token"
        return super().__call__(method, url.replace("https://gitlab.com/api/v4", "https://api.github.com"), headers,
                                body)


def gl_job(n, name, stage, status, at="2026-11-18T10:00:00Z"):
    done = status in ("success", "failed", "canceled")
    return {"id": n, "name": name, "stage": stage, "status": status, "allow_failure": False, "web_url": f"u/{n}",
            "created_at": at, "started_at": at if status not in ("manual", "created", "pending") else None,
            "finished_at": at.replace("10:00", "10:05") if done else None}


class GitLab(Base):
    """GitLab, the shipped plugin gitlab (#6121, #6162): a MaximizePM stage is a GitLab stage; its jobs, the test
    report and the environments become events; a gated stage plays its manual jobs; a roll back retries the jobs of an
    older pipeline. The plugin runs in this process against the fake API."""

    P = "/projects/grp%2Fshop"

    def setUp(self):
        super().setUp()
        from river.pipeline import plugin, runner
        self.runner, self.plugin = runner, plugin
        self.api = FakeGitLab()
        self.gl = plugin_hooks(self, self.api)
        runner.SYNC["on"] = True
        self.addCleanup(runner.SYNC.update, on=False)
        core.project_add(self.c, "shop")
        core.register(self.c, "mark", human=True)
        define.pipeline_add(self.c, "shop", project="shop")
        define.env_set(self.c, "shop", "production")
        define.stage_set(self.c, "shop", "test", "test", workflow="test", gate="forge")
        define.stage_set(self.c, "shop", "deploy", "deploy", env="production", workflow="deploy", gate="person mark")
        define.forge_set(self.c, "shop", "gitlab", repo="grp/shop", secret="gl-token")
        self.jobs = {}
        self.api.routes[("GET", f"{self.P}/pipelines?per_page")] = lambda path, body: (200, self.pls)
        self.api.routes[("GET", f"{self.P}/environments")] = (200, [])
        self.pls = []

    def pipeline(self, n, sha, jobs, report=None):
        self.pls.insert(0, {"id": n, "sha": sha, "ref": "main", "status": "running", "web_url": f"p/{n}"})
        self.jobs[n] = jobs
        self.api.routes[("GET", f"{self.P}/pipelines/{n}/jobs")] = lambda path, body: (200, self.jobs[n])
        self.api.routes[("GET", f"{self.P}/pipelines?sha={sha}")] = (200, [self.pls[0]])
        self.api.routes[("GET", f"{self.P}/pipelines/{n}/test_report")] = (200, report or {"test_suites": []})

    def poll(self):
        self.gl.poll(self.c, self.c.execute("SELECT * FROM pipelines WHERE name='shop'").fetchone())

    def runs(self):
        return {r["stage"]: r for r in self.c.execute(
            "SELECT r.*, s.name stage FROM stage_runs r JOIN pipeline_stages s ON s.id=r.stage_id ORDER BY r.id")}

    def test_jobs_tests_a_manual_deploy_after_the_gate_and_the_environment(self):
        self.pipeline(501, "a" * 40, [gl_job(1, "unit", "test", "success"), gl_job(2, "release", "deploy", "manual")],
                      {"test_suites": [{"name": "unit", "total_time": 3.5, "failed_count": 1, "test_cases": [
                          {"name": "test_ok", "classname": "shop.Cart", "status": "success", "execution_time": 0.1},
                          {"name": "test_tax", "classname": "shop.Cart", "status": "failed", "execution_time": 0.2,
                           "system_output": "AssertionError: 19 != 20\n  at cart.py:9"}]}]})
        self.pls[0]["status"] = "success"
        self.poll()
        runs = self.runs()
        self.assertEqual((runs["test"]["key"], runs["test"]["state"]), ("gl-501-test", "passed"))
        self.assertEqual((runs["deploy"]["key"], runs["deploy"]["state"]), ("gl-501-deploy", "queued"))
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM stage_runs").fetchone()[0], 2)  # one deploy run, not two
        tests = store.tests(self.c, runs["test"]["id"])
        self.assertEqual([(t["suite"], t["name"], t["state"]) for t in tests],
                         [("shop.Cart", "test_tax", "failed"), ("shop.Cart", "test_ok", "passed")])
        self.assertEqual(tests[0]["message"], "AssertionError: 19 != 20")
        # The person's yes plays the manual job.
        self.c.execute("UPDATE pipelines SET polled_at=?", (core.iso(core.now()),))
        self.runner.serve_pass(self.c)
        core.done(self.c, self.runs()["deploy"]["gate_item"], actor="mark")
        self.api.routes[("POST", "/projects/grp%2Fshop/jobs/2/play")] = (200, {})
        self.runner.serve_pass(self.c)
        self.assertEqual(self.api.calls[-1][:2], ("POST", f"{self.P}/jobs/2/play"))
        self.assertEqual(self.runs()["deploy"]["runner"], "forge:gitlab")
        self.jobs[501][1] = gl_job(2, "release", "deploy", "success")
        self.api.routes[("GET", f"{self.P}/environments")] = (200, [{"name": "production", "last_deployment": {
            "id": 77, "status": "success", "sha": "a" * 40, "created_at": "2026-11-18T10:06:00Z"}}])
        self.poll()
        self.assertEqual(self.runs()["deploy"]["state"], "passed")
        self.assertEqual(store.show(self.c, "shop")["envs"][0]["rev"], "a" * 40)

    def test_roll_back_retries_the_jobs_of_the_older_pipeline_and_cancel(self):
        self.pipeline(501, "a" * 40, [gl_job(1, "unit", "test", "success"), gl_job(2, "release", "deploy", "success")])
        self.pls[0]["status"] = "success"
        self.poll()
        old_deploy = self.runs()["deploy"]["id"]
        self.api.routes[("POST", f"{self.P}/jobs/2/retry")] = (201, {})
        r = self.runner.rollback(self.c, "shop", "production", "a" * 40, reason="b breaks", actor="mark")
        self.c.execute("UPDATE pipelines SET polled_at=?", (core.iso(core.now()),))
        self.runner.serve_pass(self.c)
        self.assertEqual(self.api.calls[-1][:2], ("POST", f"{self.P}/jobs/2/retry"))
        keys = dict(self.c.execute("SELECT id, key FROM stage_runs WHERE id IN (?, ?)", (old_deploy, r["run"])).fetchall())
        self.assertEqual(keys[r["run"]], "gl-501-deploy")  # the retried jobs belong to the roll back's run
        self.assertEqual(keys[old_deploy], f"gl-501-deploy~{old_deploy}")
        self.jobs[501][1] = gl_job(3, "release", "deploy", "running", at="2026-11-18T11:00:00Z")
        self.poll()
        self.assertEqual(self.c.execute("SELECT state FROM stage_runs WHERE id=?", (r["run"],)).fetchone()[0], "running")
        self.api.routes[("POST", f"{self.P}/jobs/3/cancel")] = (201, {})
        self.assertEqual(self.runner.stop(self.c, r["run"], actor="mark")["stopped"], "on GitLab")
        self.assertEqual(self.api.calls[-1][:2], ("POST", f"{self.P}/jobs/3/cancel"))

    def test_the_forge_command_for_gitlab(self):
        define.forge_set(self.c, "shop", "gitlab", repo="grp/sub/shop", secret="x", url="https://git.example.com",
                         actor="mark")
        urls = []
        self.plugin.RUNNER = in_process_plugin(lambda method, url, headers, body: urls.append(url) or (404, b"{}"))
        self.c.execute("UPDATE pipelines SET polled_at=NULL")
        self.assertIn("GitLab answered 404", self.plugin.poll_due(self.c)["shop"]["error"])
        self.assertTrue(urls[0].startswith("https://git.example.com/api/v4/projects/grp%2Fsub%2Fshop/pipelines"), urls)
        for bad in ({"repo": "shop"}, {"repo": "g/s", "url": "http://plain"}):
            with self.assertRaises(RiverError):
                define.forge_set(self.c, "shop", "gitlab", secret="x", **{"repo": None, **bad})


class FakePulumi(FakeGitHub):
    """The Pulumi Delivery API: paths after the pipeline stack's .../delivery, or the whole /api/... path."""

    BASE = "https://api.pulumi.com/api/preview/stacks/acme/shop-delivery/main/delivery"

    def __call__(self, method, url, headers, body):
        assert headers["Authorization"] == "token test-token"
        url = url.replace(self.BASE, "https://api.github.com").replace("https://api.pulumi.com", "https://api.github.com")
        return super().__call__(method, url, headers, body)


def pd_run(n, status, stages, release=None):
    """A Delivery run in the form that Pulumi's own gate hook reads: summary and stages."""
    return {"summary": {"number": n, "status": {"status": status}, **({"release": release} if release else {})},
            "stages": [{"name": name, "status": {"status": st}, **({"gate": gate} if gate else {})}
                       for name, st, gate in stages]}


class PulumiDelivery(Base):
    """Pulumi Delivery, the shipped plugin pulumi (#6133, #6161): a MaximizePM stage is a Delivery stage; runs are
    read by number; the yes at a MaximizePM gate continues the Delivery gate; retry, cancel; a roll back is refused.
    The plugin runs in this process against the fake API."""

    def setUp(self):
        super().setUp()
        from river.pipeline import plugin, runner
        self.runner, self.plugin = runner, plugin
        self.api = FakePulumi()
        self.pd = plugin_hooks(self, self.api)
        runner.SYNC["on"] = True
        self.addCleanup(runner.SYNC.update, on=False)
        core.project_add(self.c, "shop")
        core.register(self.c, "mark", human=True)
        define.pipeline_add(self.c, "shop", project="shop")
        define.env_set(self.c, "shop", "production")
        define.stage_set(self.c, "shop", "test", "test", workflow="checks")
        define.stage_set(self.c, "shop", "deploy", "deploy", env="production", workflow="prod", gate="person mark")
        define.forge_set(self.c, "shop", "pulumi", repo="acme/shop-delivery/main", secret="pulumi-token")

    def delivery(self, n, *args, **kw):
        self.api.routes[("GET", f"/runs/{n}")] = (200, pd_run(n, *args, **kw))

    def poll(self):
        return self.pd.poll(self.c, self.c.execute("SELECT * FROM pipelines WHERE name='shop'").fetchone())

    def pass_(self):
        self.c.execute("UPDATE pipelines SET polled_at=?", (core.iso(core.now()),))  # serve_pass reads no API
        self.runner.serve_pass(self.c)

    def runs(self):
        return {r["stage"]: r for r in self.c.execute(
            "SELECT r.*, s.name stage, v.rev FROM stage_runs r JOIN pipeline_stages s ON s.id=r.stage_id LEFT JOIN "
            "pipeline_versions v ON v.id=r.version_id ORDER BY r.id")}

    def ref(self):
        """The next Delivery run to read: the plugin's state (its 'next'), else the pipeline's --ref."""
        p = self.c.execute("SELECT ref, plugin_state FROM pipelines WHERE name='shop'").fetchone()
        state = json.loads(p["plugin_state"] or "null") or {}
        return str(state["next"]) if "next" in state else p["ref"]

    def test_a_run_its_gate_continued_after_the_persons_yes_and_the_environment(self):
        gate = {"id": "g-7", "stageName": "prod", "status": "open", "conditions": [
            {"name": "go", "kind": "manual", "state": "pending"},
            {"name": "lead", "kind": "approval", "state": "pending", "changeRequestId": "cr-9"}]}
        self.delivery(1, "in_progress", [("checks", "succeeded", None), ("prod", "pending", None)], release=7)
        self.poll()
        runs = self.runs()
        self.assertEqual((runs["test"]["key"], runs["test"]["state"], runs["test"]["rev"]),
                         ("pd-1-checks", "passed", "release 7"))
        self.assertEqual(runs["deploy"]["state"], "queued")
        self.assertFalse(runs["deploy"]["key"].startswith("pd-"))  # hand_on queued it; Delivery has not reached it
        self.assertIn(self.ref(), (None, "1"))  # run 1 has not ended: it is read again
        self.delivery(1, "waiting", [("checks", "succeeded", None), ("prod", "waiting", gate)], release=7)
        self.poll()
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM stage_runs").fetchone()[0], 2)  # one deploy run
        self.assertEqual(self.runs()["deploy"]["key"], "pd-1-prod")
        # The person's yes in MaximizePM continues the manual condition and approves the change request.
        self.pass_()
        core.done(self.c, self.runs()["deploy"]["gate_item"], actor="mark")
        self.api.routes[("POST", "/runs/1/gates/g-7/continue")] = (200, {})
        self.api.routes[("POST", "/api/change-requests/acme/cr-9/approve")] = (200, {})
        self.pass_()
        self.assertEqual([c for c in self.api.calls if c[0] == "POST"],
                         [("POST", "/runs/1/gates/g-7/continue", {"condition": "go"}),
                          ("POST", "/api/change-requests/acme/cr-9/approve", {})])
        self.assertEqual(self.runs()["deploy"]["runner"], "forge:pulumi")
        self.delivery(1, "succeeded", [("checks", "succeeded", None), ("prod", "succeeded", {**gate, "status": "passed"})],
                 release=7)
        self.poll()
        self.assertEqual(self.runs()["deploy"]["state"], "passed")
        self.assertEqual(store.show(self.c, "shop")["envs"][0]["rev"], "release 7")
        self.assertEqual(self.ref(), "2")  # run 1 ended: the next read starts at run 2

    def test_an_auto_gate_continues_only_manual_conditions(self):
        define.stage_set(self.c, "shop", "deploy", gate="auto")
        self.api.routes[("POST", "/runs/1/gates/g-1/continue")] = (409, {"message": "the gate is not open"})
        self.delivery(1, "waiting", [("checks", "succeeded", None), ("prod", "waiting", {"id": "g-1", "status": "open",
            "conditions": [{"name": "go", "kind": "manual", "state": "pending"},
                           {"name": "lead", "kind": "approval", "state": "pending", "changeRequestId": "cr-1"}]})])
        self.poll()
        self.pass_()
        self.assertNotIn("/api/change-requests/acme/cr-1/approve", [c[1] for c in self.api.calls])
        self.assertIn("a person decides: approval lead", self.runs()["deploy"]["gate_note"])

    def test_start_retry_cancel_and_no_roll_back(self):
        self.api.routes[("POST", "/dispatch")] = (200, {"runNumber": 40})
        self.runner.start(self.c, "shop", rev="a" * 40, actor="mark")
        self.pass_()
        self.assertEqual(self.api.calls[-1][:2], ("POST", "/dispatch"))
        self.assertEqual((self.runs()["test"]["key"], self.ref()), ("pd-40-checks", "40"))
        self.delivery(40, "failed", [("checks", "failed", None), ("prod", "pending", None)])
        self.poll()
        failed = self.runs()["test"]
        self.assertEqual((failed["state"], failed["rev"]), ("failed", "a" * 40))  # it keeps the version it was sent with
        self.assertIn("checks failed", failed["errors"])
        self.assertEqual(self.ref(), "41")
        # Run again: Delivery retries the failed run; the old end that it still shows is not the retry's end.
        self.api.routes[("POST", "/runs/40/retry")] = (200, {})
        again = self.runner.run_again(self.c, "shop", "test", "a" * 40, actor="mark")
        self.pass_()
        self.assertEqual(self.api.calls[-1][:2], ("POST", "/runs/40/retry"))
        keys = dict(self.c.execute("SELECT id, key FROM stage_runs WHERE stage_id=1").fetchall())
        self.assertEqual(keys, {failed["id"]: f"pd-40-checks~{failed['id']}", again["run"]: "pd-40-checks"})
        self.poll()
        self.assertEqual(self.c.execute("SELECT state FROM stage_runs WHERE id=?", (again["run"],)).fetchone()[0],
                         "queued")
        self.delivery(40, "in_progress", [("checks", "running", None), ("prod", "pending", None)])
        self.poll()  # run 40 is below the ref, and a stage run waits on it: it is read again
        self.assertEqual(self.c.execute("SELECT state FROM stage_runs WHERE id=?", (again["run"],)).fetchone()[0],
                         "running")
        self.api.routes[("POST", "/runs/40/cancel")] = (200, {})
        self.assertEqual(self.runner.stop(self.c, again["run"], actor="mark")["stopped"], "on Pulumi Delivery")
        self.assertEqual(self.api.calls[-1][:2], ("POST", "/runs/40/cancel"))
        self.delivery(40, "canceled", [("checks", "running", None), ("prod", "pending", None)])
        self.poll()
        r = self.c.execute("SELECT state, errors FROM stage_runs WHERE id=?", (again["run"],)).fetchone()
        self.assertEqual(r["state"], "cancelled")
        self.assertIn("the Delivery run ended (canceled)", r["errors"])
        # A roll back is Pulumi's page's: refused before the stage's gate holds (#6159).
        with self.assertRaises(RiverError) as e:
            self.runner.rollback(self.c, "shop", "production", "a" * 40, reason="b breaks", actor="mark")
        self.assertIn("rollback is not available for this source: MaximizePM cannot do it through the Pulumi "
                      "Delivery API", str(e.exception))
        self.assertFalse(self.c.execute("SELECT 1 FROM pipeline_transitions WHERE block_reason IS NOT NULL").fetchone())

    def test_the_forge_command_for_pulumi(self):
        for bad in ({"repo": "acme/shop"}, {"repo": "acme/shop-delivery/main", "ref": "main"}):
            with self.assertRaises(RiverError):
                define.forge_set(self.c, "shop", "pulumi", secret="x", **bad)
        define.forge_set(self.c, "shop", "pulumi", repo="acme/shop-delivery/main", secret="x", ref="120")
        self.assertIn("runs read from run 120 on", define.history(self.c, "shop", 1)[0]["change"])
        self.api.routes[("GET", "/runs/120")] = (500, {"message": "down"})
        self.c.execute("UPDATE pipelines SET polled_at=NULL")
        self.assertIn("Pulumi answered 500", self.plugin.poll_due(self.c)["shop"]["error"])
        self.assertEqual(self.api.calls[-1][:2], ("GET", "/runs/120"))  # under the stack's .../delivery address


class Speed(Base):
    @timing.local_only("100 ms latency budget")
    def test_each_read_under_100_ms(self):
        """Policy p147: a pipeline with 400 runs, 2000 steps and 10800 test results reads fast."""
        self.toolscaled()
        stages = ["build", "test", "beta", "prod"]
        with core.tx(self.c):
            for i in range(400):
                pid = 1
                sid = i % 4 + 1
                vid = store._version_id(self.c, pid, f"{i // 4:07x}")
                rid = self.c.execute("INSERT INTO stage_runs(pipeline_id, stage_id, version_id, key, state, started_at) "
                                     "VALUES (1,?,?,?, 'passed', '2026-11-18T10:00:00Z')",
                                     (sid, vid, f"r{i}")).lastrowid
                self.c.executemany("INSERT INTO stage_steps(run_id, key, name, state) VALUES (?,?,?, 'passed')",
                                   [(rid, f"s{j}", f"step {j}") for j in range(5)])
                if stages[i % 4] == "test" and i % 8 == 1:
                    self.c.executemany("INSERT INTO test_results(run_id, suite, name, state) VALUES (?,?,?,?)",
                                       [(rid, "suite", f"t{j}", "failed" if j % 50 == 0 else "passed")
                                        for j in range(216)])
        last_test = self.c.execute("SELECT MAX(run_id) FROM test_results").fetchone()[0]
        for read in (lambda: store.show(self.c, "toolscaled"), lambda: store.versions(self.c, "toolscaled"),
                     lambda: store.run_show(self.c, last_test), lambda: store.tests(self.c, last_test),
                     lambda: store.events(self.c)):
            t = time.monotonic()
            read()
            self.assertLess(time.monotonic() - t, timing.limit(0.1))


class Holding(RunnerBase):
    """#6140: holding, the first stage: the commits since the last version that left it, their review, the release."""

    def setUp(self):
        super().setUp()
        from river.pipeline import targets
        self.targets = targets
        targets._HOLD.clear()
        self.addCleanup(targets._HOLD.clear)
        self.git("init", "-q", "-b", "main")

    def git(self, *a):
        import subprocess
        return subprocess.run(["git", "-C", self.folder, *a], capture_output=True, text=True, check=True).stdout.strip()

    def commit(self, msg, name="f.txt"):
        with open(os.path.join(self.folder, name), "a", encoding="utf-8") as f:
            f.write(msg + "\n")
        self.git("add", name)
        self.git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def test_holding_lists_the_commits_since_the_last_version_and_releases_the_reviewed_ones(self):
        from river import review
        base = self.commit("Start")
        self.runner.start(self.c, "app", rev=base, actor="mark")  # a version left holding
        t1 = core.item_add(self.c, "app", "Round the total", actor="mark")["id"]
        t2 = core.item_add(self.c, "app", "Cache the prices", actor="mark")["id"]
        self.c.execute("UPDATE agents SET session_url='https://claude.ai/code/session_ABC' WHERE name='w1'")
        a = self.commit(f"Round the total (#{t1})\n\nClaude-Session: https://claude.ai/code/session_ABC")
        b = self.commit(f"Cache the prices (#{t2})")
        h = self.targets.holding(self.c, "app", fresh=True)
        self.assertEqual((h["base"], h["base_label"]), (base, "the last version that left holding"))
        self.assertEqual([c["sha"] for c in h["commits"]], [a, b])  # the oldest first
        self.assertEqual(([t["id"] for t in h["commits"][0]["tasks"]], h["commits"][0]["session"]), ([t1], "w1"))
        self.assertEqual((h["commits"][1]["session"], h["commits"][1]["by_session"]), ("t", False))  # git's author
        self.assertEqual((h["added"], h["counts"]["not_reviewed"], h["upto"]), (4, 2, None))  # 3 lines, then 1
        self.assertTrue(h["release"]["text"].startswith("a person or the manager releases"))
        with self.assertRaises(RiverError):  # nothing reviewed: name the commits
            self.targets.release(self.c, "app", actor="mark")
        review.mark(self.c, "approved", item=t1, actor="mark")
        h = self.targets.holding(self.c, "app", fresh=True)
        self.assertEqual((h["commits"][0]["review"]["text"], h["upto"]), ("approved in Changes", a))
        with self.assertRaises(RiverError):
            self.targets.release(self.c, "app", actor="w1")  # an agent: only a person or the manager releases
        out = self.targets.release(self.c, "app", actor="mark")
        self.assertEqual((out["rev"], out["released"], out["stage"]), (a, 1, "build"))
        h = self.targets.holding(self.c, "app", fresh=True)
        self.assertEqual(([c["sha"] for c in h["commits"]], h["base"]), ([b], a))
        d = self.targets.holding_diff(self.c, "app", b[:8])
        self.assertEqual(d["commits"][0]["files"][0]["path"], "f.txt")
        self.assertIn("all 1 commits", self.targets.holding_diff(self.c, "app")["commits"][0]["subject"])

    def test_a_target_holds_its_shipped_work_and_its_release_is_the_cut(self):
        core.target_add(self.c, "site", "", None)
        core.project_add(self.c, "web", path=self.folder, target="site")
        self.commit("Start")
        t = core.item_add(self.c, "web", "A change", actor="mark")["id"]
        sha = self.commit(f"A change (#{t})")
        h = self.targets.holding(self.c, "site", fresh=True)
        self.assertEqual(([c["sha"] for c in h["commits"]][-1], h["default"]), (sha, True))
        self.assertFalse(h["review_enabled"])
        self.assertEqual(h["counts"], {})
        self.assertTrue(all(c["review"] is None for c in h["commits"]))
        self.assertFalse(h["release"]["can"])  # nothing is collected yet
        core.claim(self.c, t, "w1")
        core.done(self.c, t, output="done", actor="w1", ship_it=True, force="test")
        self.assertTrue(self.targets.holding(self.c, "site", fresh=True)["release"]["can"])
        out = self.targets.release(self.c, "site", everything=True, actor="mark")
        dep = core.item_show(self.c, out["release"])
        self.assertEqual((dep["cut_rev"], out["rev"]), (sha, sha))
        stages = self.targets.default_pipeline(self.c, "site")["stages"]
        self.assertEqual((stages[0]["name"], stages[0]["last_run"]["state"]), ("holding", "passed"))
        # #6175: the cut release left holding: its commits are no longer there while it waits after it.
        h = self.targets.holding(self.c, "site", fresh=True)
        self.assertEqual((h["commits"], h["base"], h["base_label"]), ([], sha, f"the pin of release #{out['release']}"))
        self.assertEqual(self.targets.holding_stage(h)["last_run"]["state"], "none")
        later = self.commit("A later change")
        h = self.targets.holding(self.c, "site", fresh=True)
        self.assertEqual([c["sha"] for c in h["commits"]], [later])
        # A release dropped after its cut gives its commits back to holding.
        core.drop(self.c, out["release"], "mark", note="not this one")
        h = self.targets.holding(self.c, "site", fresh=True)
        self.assertEqual([c["sha"] for c in h["commits"]][-2:], [sha, later])


class AgentsOnStages(RunnerBase):
    """#6149: the faults of each stage and the sessions that work on them, for the Deploy tab's Agents view."""

    def test_a_fault_and_the_agents_on_it_and_its_fix_stand_under_their_stage(self):
        from river.pipeline import links
        define.stage_set(self.c, "app", "test", run="echo broken; exit 3")
        self.runner.start(self.c, "app", rev="bad0001")
        self.passes(2)
        fault = self.runs()["test"]["fault_item"]
        self.assertEqual(links.stage_work(self.c)["app"]["test"]["faults"][0]["nobody"], True)  # nobody works on it
        core.claim(self.c, fault, "w1")
        core.agent_now(self.c, "w1", "Reads the test output of the failed run")
        fix = links.fix_task(self.c, self.runs()["test"]["id"], actor="mark")["item"]
        core.register(self.c, "w2")
        core.claim(self.c, fix, "w2")
        core.send(self.c, "question", "Skip the flaky test until Monday?", to="mark", item=fix, actor="w2")
        w = links.stage_work(self.c)["app"]
        self.assertEqual(set(w), {"test"})
        f = w["test"]["faults"][0]
        self.assertEqual((f["id"], f["rev"], f["fixes"], f["nobody"]), (fault, "bad0001", [fix], False))
        A = {a["name"]: a for a in w["test"]["agents"]}
        self.assertEqual((A["w1"]["step"], A["w1"]["where"], A["w1"]["item"]),
                         ("Reads the test output of the failed run", "this computer", fault))
        self.assertIn("stage test failed for bad0001", A["w1"]["started_by"])
        self.assertEqual((A["w2"]["st"], A["w2"]["state"]), ("human", "waits for a yes"))
        self.assertEqual(A["w2"]["question"]["body"], "Skip the flaky test until Monday?")
        self.assertEqual(A["w2"]["started_by"], f"a fix task of fault #{fault}")
        core.done(self.c, fix, actor="w2", output="skipped the flaky test, commit abc", force="test")
        done = next(a for a in links.stage_work(self.c)["app"]["test"]["agents"] if a["name"] == "w2")
        self.assertEqual((done["st"], done["proof"]), ("ok", "skipped the flaky test, commit abc"))
        self.assertEqual(links.stage_work(self.c, "other"), {})


class AuditLog(RunnerBase):
    """The audit log of pipelines (#6144): every change to a pipeline and its flow, with who did it; no line changes."""

    def setUp(self):
        super().setUp()
        from river.pipeline import audit
        self.audit = audit

    def lines(self, **kw):
        return self.audit.lines(self.c, **kw)["lines"]

    def test_each_action_and_each_change_of_the_flow_is_a_line_with_its_actor(self):
        self.runner.start(self.c, "app", rev="abc1234", actor="w1")
        self.passes()
        core.done(self.c, self.runs()["prod"]["gate_item"], actor="mark")  # mark's yes on the card
        self.passes(1)
        self.runner.hold(self.c, "app", "prod", "the payment provider has an outage", actor="mark")
        got = [(l["actor"], l["kind"], l["action"], l["place"]) for l in reversed(self.lines()) if l["action"] != "define"]
        self.assertEqual(got[0], ("w1", "agent", "start", "stage build"))
        self.assertIn(("mark", "person", "approve", "the transition into prod"), got)
        self.assertIn(("mark", "person", "block", "the transition into prod"), got)  # a hold of a stage blocks it
        hooks = [g for g in got if g[1] == "hook"]
        self.assertEqual([g[2] for g in hooks], ["passed", "passed", "deployed", "deployed"])
        self.assertEqual(hooks[2][0], "echo deploying $MAXPM_REV to $MAXPM_ENV")  # the stage's command ran it
        approve = next(l for l in self.lines() if l["action"] == "approve")
        self.assertEqual((approve["rev"], approve["result"].split(",")[0]), ("abc1234", "passed"))  # the run's state now
        from river.pipeline import transitions
        transitions.delay(self.c, "app", "beta", "2h", "a quiet night first", actor="mark")
        d = self.lines()[0]
        self.assertEqual((d["action"], d["place"], d["reason"]), ("delay", "the transition into beta",
                                                                  "a quiet night first"))
        self.assertTrue(d["until"])
        hold = self.lines(who="person", search="outage")
        self.assertEqual([(l["action"], l["reason"]) for l in hold], [("block", "the payment provider has an outage")])
        self.assertEqual({l["kind"] for l in self.lines(who="trigger,hook")}, {"hook"})
        self.assertEqual([l["actor"] for l in self.lines(who="w1")], ["w1"])
        self.assertEqual(len(self.lines(since="today")), len(self.lines()))
        self.assertEqual(self.lines(search="nothing-like-this"), [])
        with self.assertRaises(RiverError):
            self.lines(since="someday")
        with self.assertRaises(RiverError):
            self.lines(pipeline="nope")

    def test_a_declined_card_a_stop_a_fault_and_a_forge_run_name_who_did_them(self):
        self.runner.start(self.c, "app", rev="abc1234")
        self.passes()
        core.drop(self.c, self.runs()["prod"]["gate_item"], "mark", note="not today")
        self.passes(1)
        decline = self.lines(search="declined")
        self.assertEqual([(l["actor"], l["action"]) for l in decline], [("mark", "decline")])
        self.assertEqual(self.lines(search="stopped"), [])  # the cancelled run adds no second line
        define.stage_set(self.c, "app", "build", run="echo broken; exit 3")
        self.runner.start(self.c, "app", rev="bad0001")
        self.passes(2)
        fault = self.lines(who="trigger")
        self.assertEqual(fault[0]["action"], "fault")
        self.assertIn(f"fault #{self.runs()['build']['fault_item']}", fault[0]["result"])
        failed = next(l for l in self.lines() if l["action"] == "failed")
        self.assertEqual((failed["kind"], failed["reason"]), ("hook", "exit 3"))
        r = store.run_start(self.c, "app", "test", version="f00d001", source="github", key="gh-run-9")
        store.run_finish(self.c, r, "passed", source="github")
        store.run_finish(self.c, r, "passed", source="github")  # the same end read again
        forge = self.lines(who="forge")
        self.assertEqual([(l["actor"], l["action"]) for l in forge], [("GitHub", "passed")])

    def test_no_line_is_changed_or_deleted_and_a_correction_is_a_new_line(self):
        import sqlite3
        self.runner.hold(self.c, "app", None, "release freez", actor="mark")
        line = self.lines()[0]["id"]
        for sql in ("UPDATE pipeline_changes SET change='x'", "DELETE FROM pipeline_changes"):
            with self.assertRaises(sqlite3.DatabaseError) as e:
                self.c.execute(sql)
            self.assertIn("maxpm pipeline correct", str(e.exception))
        with self.assertRaises(RiverError):
            self.audit.correct(self.c, line, "it says freeze", actor="w1")  # an agent, not the line's actor
        with self.assertRaises(RiverError):
            self.audit.correct(self.c, line, " ", actor="mark")
        out = self.audit.correct(self.c, line, "the reason is: release freeze", actor="mark")
        L = self.lines()
        self.assertEqual((L[0]["id"], L[0]["corrects"], L[0]["action"]), (out["line"], line, "correct"))
        self.assertEqual(L[1]["corrected_by"], [out["line"]])
        self.assertEqual(L[1]["change"], "held every stage: release freez")  # the old line stays as it was

    def test_a_token_and_its_grants_show_in_the_log_of_each_pipeline_that_names_it(self):
        from river import vault
        old = vault.BACKEND
        vault.BACKEND = vault.Files()
        self.addCleanup(setattr, vault, "BACKEND", old)
        define.pipeline_add(self.c, "gh", actor="mark")
        define.forge_set(self.c, "gh", "github", repo="o/r", secret="github-o", actor="mark")
        vault.put(self.c, "github-o", b"tok-1", "mark")
        vault.put(self.c, "github-o", b"tok-2", "mark")
        vault.put(self.c, "other", b"x", "mark")  # no pipeline names it
        vault.set_change(self.c, "ci", add=["github-o"], actor="mark")
        vault.grant(self.c, "ci", "project", "app", actor="mark")
        tok = [l["change"] for l in self.lines(pipeline="gh") if l["action"] in ("token", "grant")]
        self.assertTrue(any(t.startswith("granted the set ci (token github-o) to project") for t in tok), tok)
        self.assertIn("replaced the token github-o (stdin)", tok)
        self.assertIn("saved the token github-o (stdin)", tok)
        self.assertFalse(any("other" in t for t in tok))
        self.assertEqual([l for l in self.lines(pipeline="app") if l["action"] in ("token", "grant")], [])

    def test_the_commands_print_the_log_and_add_a_correction(self):
        from river import cli
        self.runner.hold(self.c, "app", "prod", "an outage", actor="mark")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.run(["--as", "mark", "pipeline", "audit", "app", "--who", "person", "--search",
                                      "outage"]), 0)
        text = out.getvalue()
        self.assertIn("mark (person): blocked the transition beta -> prod until someone unblocks it  why: an outage",
                      text)
        line = int(text.split()[0])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.run(["--as", "mark", "pipeline", "correct", str(line), "the outage was at Stripe"]), 0)
        self.assertIn(f"corrects line {line}", out.getvalue())


class OutsideGates(Base):
    """#6164: an outside flow (Jenkins, a cron job) asks 'may version 42 enter prod?' and the transition answers: its
    mode and its controls, one audit line each time the answer changes; maxpm serve never starts its stage."""

    def setUp(self):
        super().setUp()
        from river.pipeline import audit, gates, runner, transitions
        self.g, self.runner, self.tr, self.audit = gates, runner, transitions, audit
        runner.SYNC["on"] = True
        self.addCleanup(runner.SYNC.update, on=False)
        core.project_add(self.c, "shop")
        core.register(self.c, "mark", human=True)
        define.pipeline_add(self.c, "shop", project="shop", actor="mark")
        define.env_set(self.c, "shop", "staging")
        define.env_set(self.c, "shop", "prod")
        define.stage_set(self.c, "shop", "build", "build")  # no --run: the outside system runs each stage
        define.stage_set(self.c, "shop", "staging", "deploy", env="staging")
        define.stage_set(self.c, "shop", "prod", "deploy", env="prod", gate="person mark")

    def run_at(self, stage, rev):
        return self.c.execute("SELECT r.* FROM stage_runs r JOIN pipeline_stages s ON s.id=r.stage_id JOIN "
                              "pipeline_versions v ON v.id=r.version_id WHERE s.name=? AND v.rev=? ORDER BY r.id DESC",
                              (stage, rev)).fetchone()

    def lines(self):
        return [(l["actor"], l["action"], l["result"]) for l in self.audit.lines(self.c, "shop")["lines"]
                if l["action"] == "ask"]

    def test_an_open_transition_says_yes_once_and_the_next_stage_ends_the_one_before(self):
        a = self.g.ask(self.c, "shop", "build", "42", asker="jenkins")
        self.assertEqual((a["may"], a["version"], a["asked_by"]), (True, "42", "jenkins"))
        self.assertEqual(self.run_at("build", "42")["state"], "running")
        self.assertTrue(self.g.ask(self.c, "shop", "build", "42", asker="jenkins")["may"])  # asked again: the same yes
        self.assertEqual(self.lines(), [("jenkins", "ask", "yes")])
        self.assertEqual(self.audit.lines(self.c, "shop", who="forge")["lines"][0]["actor"], "jenkins")
        self.assertTrue(self.g.ask(self.c, "shop", "staging", "42", asker="jenkins")["may"])
        self.assertEqual(self.run_at("build", "42")["state"], "passed")  # it asked at the next stage: it passed build
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM stage_runs").fetchone()[0], 2)
        self.g.ask(self.c, "shop", "prod", "42", asker="jenkins")  # a no (mark's yes), but staging passed
        envs = {e["name"]: e["rev"] for e in store.show(self.c, "shop")["envs"]}
        self.assertEqual(envs["staging"], "42")
        # An event of the outside system ends a run (maxpm pipeline event): the next stage waits for its question.
        self.assertTrue(self.g.ask(self.c, "shop", "build", "50", asker="jenkins")["may"])
        run = self.run_at("build", "50")["id"]
        store.run_finish(self.c, run, "passed", source="jenkins")
        self.runner.hand_on(self.c, run)
        self.runner.serve_pass(self.c)
        nxt = self.run_at("staging", "50")
        self.assertEqual((nxt["state"], nxt["outside"]), ("queued", "jenkins"))

    def test_a_persons_yes_and_maxpm_serve_never_starts_the_stage(self):
        no = self.g.ask(self.c, "shop", "prod", "43", asker="cron")
        self.assertFalse(no["may"])
        r = self.run_at("prod", "43")
        self.assertIn(f"waits for mark's yes on #{r['gate_item']}", no["why"])
        self.assertFalse(self.g.ask(self.c, "shop", "prod", "43", asker="cron")["may"])
        out = self.runner.serve_pass(self.c)
        self.assertEqual(self.run_at("prod", "43")["state"], "queued")  # never started here
        self.assertIn(r["id"], out["waiting"])
        arrow = store.show(self.c, "shop")["stages"][2]["transition"]
        self.assertEqual([(w["rev"], w["outside"]) for w in arrow["waiting"]], [("43", "cron")])
        self.assertEqual(self.lines(), [("cron", "ask", "no")])  # the same no again writes nothing
        core.done(self.c, r["gate_item"], actor="mark")
        yes = self.g.ask(self.c, "shop", "prod", "43", asker="cron")
        self.assertTrue(yes["may"])
        self.assertEqual(self.run_at("prod", "43")["approved_by"], "mark")
        self.assertEqual(self.lines(), [("cron", "ask", "yes"), ("cron", "ask", "no")])

    def test_a_block_says_until_when_and_a_declined_yes_stays_no(self):
        self.tr.block(self.c, "shop", "staging", "the database moves", until="2h", actor="mark")
        no = self.g.ask(self.c, "shop", "staging", "44", asker="argo")
        self.assertFalse(no["may"])
        self.assertIn("blocked by mark: the database moves", no["why"])
        self.assertTrue(no["until"])
        self.tr.unblock(self.c, "shop", "staging", actor="mark")
        self.assertTrue(self.g.ask(self.c, "shop", "staging", "44", asker="argo")["may"])
        self.g.ask(self.c, "shop", "prod", "45", asker="argo")
        core.drop(self.c, self.run_at("prod", "45")["gate_item"], actor="mark", note="not this one")
        for _ in range(2):
            self.assertIn("declined", self.g.ask(self.c, "shop", "prod", "45", asker="argo")["why"])

    def test_the_refusals_the_wait_the_exit_code_and_the_http_answer(self):
        define.stage_set(self.c, "shop", "smoke", "test", run="echo ok")
        with self.assertRaises(RiverError) as e:
            self.g.ask(self.c, "shop", "smoke", "46")
        self.assertIn("runs here", str(e.exception))
        with self.assertRaises(RiverError):
            self.g.ask(self.c, "shop", "build", "")
        with self.assertRaises(RiverError):
            self.g.ask(self.c, "shop", "nowhere", "46")
        self.tr.block(self.c, "shop", "staging", "a test", actor="mark")
        slept = []

        def sleep(s):
            slept.append(s)
            self.tr.unblock(self.c, "shop", "staging", actor="mark")
        res = self.g.may(self.c, "shop", "staging", "47", wait="1m", asker="jenkins", sleep=sleep)
        self.assertTrue(res["may"])
        self.assertEqual(slept, [self.g.POLL])
        self.tr.block(self.c, "shop", "staging", "a test", actor="mark")
        from river import cli
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.run(["pipeline", "may", "shop", "staging", "--rev", "48", "--from", "cron"]), 1)
        self.assertIn("no: version 48 may not enter stage staging of shop now", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.run(["pipeline", "may", "shop", "build", "--rev", "48"]), 0)
        from river.routes import pipeline as routes
        sent = []
        h = mock.Mock(path="/api/pipeline-gate?pipeline=shop&stage=staging&rev=49&from=github", _send=lambda s, b: sent.append((s, b)))
        routes._gate(h, "/api/pipeline-gate")
        self.assertEqual((sent[0][0], sent[0][1]["may"], sent[0][1]["asked_by"]), (409, False, "github"))
        self.tr.unblock(self.c, "shop", "staging", actor="mark")
        routes._gate(h, "/api/pipeline-gate")
        self.assertEqual((sent[1][0], sent[1][1]["may"]), (200, True))
        h.path = "/api/pipeline-gate?pipeline=shop&stage=smoke&rev=49"
        routes._gate(h, "/api/pipeline-gate")
        self.assertEqual(sent[2][0], 404)


if __name__ == "__main__":
    unittest.main()
