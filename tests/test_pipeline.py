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
        self.assertEqual((prod["env"], prod["gate"], prod["kind"]), ("prod", "person", "deploy"))
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
        self.assertIn("5. prod  [deploy, env prod]  gate auto after 30m  run: make prod-release", out)
        self.assertIn("env prod: no version yet  (target toolscaled-prod)", out)
        path = os.path.join(self.dir.name, "toolscaled-pipeline.json")
        self.cli("pipeline", "export", "toolscaled", "--file", path)
        with open(path) as f:
            exp = json.load(f)
        self.assertEqual([s["name"] for s in exp["stages"]], ["build", "test", "review", "beta", "prod"])
        self.assertEqual(exp["stages"][-1], {"name": "prod", "kind": "deploy", "run": "make prod-release",
                                             "workflow": None, "env": "prod", "gate": "auto after 30m",
                                             "junit": None})
        self.assertEqual(exp["environments"][1]["target"], "toolscaled-prod")
        code, out = self.cli("pipeline", "history", "toolscaled")
        lines = out.strip().split("\n")
        self.assertEqual(len(lines), 9)
        self.assertIn("mark: stage prod changed: gate person mark -> auto after 30m", lines[0])
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

    def test_gate_rules(self):
        self.assertEqual([define.parse_gate(t) for t in ("auto", "auto after 30m", "manager", "person", "person mark")],
                         [("auto", None), ("auto", "30m"), ("manager", None), ("person", None), ("person", "mark")])
        for bad in ("", "auto after", "auto after soon", "anyone", "person a b", "manager mark"):
            with self.assertRaises(RiverError):
                define.parse_gate(bad)
        self.assertEqual(store.gate_text("auto", "30m"), "auto after 30m")


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


class Runner(Base):
    """The no-forge runner (#6114): a fake pipeline of four stages whose commands print '::step' lines."""

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
        self.assertIn("(auto after 30m) while the checks pass", test["gate_note"])
        # A failed check of the project since the build keeps it closed.
        cid = self.c.execute("INSERT INTO checks(scope_kind, scope, name, form, config, every, fail_after, "
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
        self.assertIn("gate: waits for mark's yes on #", text)


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

    def test_a_target_shows_its_releases_as_stage_runs(self):
        old = core.iso(core.now() - core.timedelta(days=2))
        d1 = self.release("aaaaaaa", "First change")
        self.hook(d1, "release-cut", 0, "aaaaaaa", old)
        self.hook(d1, "deploy", 2, "aaaaaaa", old)
        self.hook(d1, "deploy", 0, "aaaaaaa", old)
        self.c.execute("UPDATE items SET status='done', closed_at=? WHERE id=?", (old, d1))
        cid = self.c.execute("INSERT INTO checks(scope_kind, scope, name, form, config, every, fail_after, "
                             "recover_after, timeout, severity, created_at) VALUES ('target','web-prod','up','http',"
                             "'{}','1m',1,1,'10s','item',?)", (old,)).lastrowid
        self.c.execute("INSERT INTO ops_runs(check_id, key, started_at, ended_at, status) VALUES (?, 'k', ?, ?, 'ok')",
                       (cid, old, old))
        d2 = self.release("bbbbbbb", "Second change")
        p = self.targets.default_pipeline(self.c, "web-prod")
        self.assertTrue(p["default"])
        self.assertEqual([s["name"] for s in p["stages"]], ["cut", "build", "deploy", "watch"])
        self.assertEqual(p["stages"][2]["env"], "web-prod")
        self.assertEqual(p["stages"][2]["run"], None)  # no deploy hook set now: the runs keep their command
        self.assertEqual([r["version"] for r in p["releases"]], ["bbbbbbb", "aaaaaaa"])
        self.assertEqual(p["releases"][0]["stages"], {"cut": {"state": "passed", "item": d2},
                                                      "deploy": {"state": "queued", "item": d2}})
        first = {k: v["state"] for k, v in p["releases"][1]["stages"].items()}
        self.assertEqual(first, {"cut": "passed", "build": "passed", "deploy": "passed", "watch": "passed"})
        deploy = next(s for s in p["stages"] if s["name"] == "deploy")["last_run"]
        self.assertEqual(deploy["state"], "queued")  # the newest release's
        env = p["envs"][0]
        self.assertEqual((env["rev"], env["since"]), ("aaaaaaa", old))
        # The deploy hook's runs are the steps of the first release's deploy stage: one failed, then one passed.
        runs = self.targets._release(self.c, self.targets._target(self.c, "web-prod"),
                                     self.c.execute("SELECT * FROM items WHERE id=?", (d1,)).fetchone(), None, False,
                                     False, self.targets._checks(self.c, "web-prod"))
        self.assertEqual([s["state"] for s in runs["deploy"]["step_list"]], ["failed", "passed"])
        self.assertEqual(runs["watch"]["step_list"][0]["name"], "web-prod/up")

    def test_review_and_the_person_s_yes_are_stages_when_the_target_has_them(self):
        core.config_set(self.c, "review", "on", project="web")
        core.config_set(self.c, "release_approval", "person", project="web")
        self.release("ccccccc", "A change")
        names = [s["name"] for s in self.targets.default_pipeline(self.c, "web-prod")["stages"]]
        self.assertEqual(names, ["cut", "review", "approval", "deploy", "watch"])
        approval = next(s for s in self.targets.default_pipeline(self.c, "web-prod")["stages"]
                        if s["name"] == "approval")
        self.assertEqual(approval["gate"], "person")

    def test_list_and_show_and_a_target_that_a_pipeline_covers(self):
        from river import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.dict(os.environ, {"MAXPM_DB": self.path, "MAXPM_QUIET": "1"}):
            cli.run(["--as", "mark", "pipeline", "list"])
            cli.run(["--as", "mark", "pipeline", "show", "web-prod"])
        self.assertIn("web-prod  (a deploy target: its default pipeline)", out.getvalue())
        self.assertIn("(the default pipeline of deploy target web-prod, from its releases)", out.getvalue())
        define.pipeline_add(self.c, "web", project="web")
        define.env_set(self.c, "web", "prod", target="web-prod")
        self.assertEqual(self.targets.default_list(self.c), [])
        with self.assertRaises(RiverError):
            self.targets.default_pipeline(self.c, "nowhere")

    def test_ten_releases_read_under_100_ms(self):
        for n in range(12):
            d = self.release(f"{n:07x}", f"Change {n}")
            self.c.execute("UPDATE items SET status='done', closed_at=? WHERE id=?",
                           (core.iso(core.now() - core.timedelta(hours=12 - n)), d))
        t = time.monotonic()
        p = self.targets.default_pipeline(self.c, "web-prod")
        self.assertLess(time.monotonic() - t, timing.limit(0.1))
        self.assertEqual(len(p["releases"]), self.targets.RELEASES)


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


class Speed(Base):
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


if __name__ == "__main__":
    unittest.main()
