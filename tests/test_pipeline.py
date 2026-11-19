"""The pipeline package: its records and the intake of events, '::step' lines and JUnit reports (#6112)."""

import json
import os
import tempfile
import time
import unittest
from unittest import mock

from river import core
from river.core import RiverError
from river.pipeline import cdevents, junit, steps, store
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
        store.pipeline_add(self.c, "toolscaled", project="toolscaled", actor="mark")
        store.env_add(self.c, "toolscaled", "beta")
        store.env_add(self.c, "toolscaled", "prod", target="toolscaled-prod")
        store.stage_add(self.c, "toolscaled", "build", "build", run="make prod-release-prepare")
        store.stage_add(self.c, "toolscaled", "test", "test", run="make check-all-once")
        store.stage_add(self.c, "toolscaled", "beta", "deploy", env="beta", run="make deploy ENV=beta")
        store.stage_add(self.c, "toolscaled", "prod", "deploy", env="prod", run="make prod-release", gate="person")


class Definitions(Base):
    def test_the_tables_come_with_connect(self):
        names = {r[0] for r in self.c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertLessEqual({"pipelines", "pipeline_envs", "pipeline_stages", "pipeline_versions", "stage_runs",
                              "stage_steps", "test_suites", "test_results", "pipeline_deploys", "pipeline_events"},
                             names)

    def test_stages_in_order_with_their_environment_and_gate(self):
        self.toolscaled()
        store.stage_add(self.c, "toolscaled", "review", "review", pos=3)
        p = store.show(self.c, "toolscaled")
        self.assertEqual([s["name"] for s in p["stages"]], ["build", "test", "review", "beta", "prod"])
        prod = p["stages"][-1]
        self.assertEqual((prod["env"], prod["gate"], prod["kind"]), ("prod", "person", "deploy"))
        self.assertEqual([e["name"] for e in p["envs"]], ["beta", "prod"])
        self.assertEqual(p["envs"][1]["target"], "toolscaled-prod")

    def test_refusals(self):
        self.toolscaled()
        for call in (lambda: store.pipeline_add(self.c, "toolscaled"),
                     lambda: store.pipeline_add(self.c, "has space"),
                     lambda: store.stage_add(self.c, "toolscaled", "x", "dance"),
                     lambda: store.stage_add(self.c, "toolscaled", "x", "deploy"),
                     lambda: store.stage_add(self.c, "toolscaled", "x", "deploy", env="nowhere"),
                     lambda: store.stage_add(self.c, "toolscaled", "x", "build", gate="anyone"),
                     lambda: store.stage_add(self.c, "nope", "x", "build")):
            with self.assertRaises(RiverError):
                call()


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
