"""Events in from any deployment system (#6163): maxpm pipeline event, maxpm pipeline report, and
POST /api/pipeline-events with a token of the pipeline."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from river import cli, core, server
from river.core import RiverError
from river.pipeline import define, forge, intake, store

JUNIT = b"""<testsuite name="unit" tests="2" failures="1" time="0.3">
<testcase classname="shop.Cart" name="test_ok" time="0.1"/>
<testcase classname="shop.Cart" name="test_tax" time="0.2"><failure message="19 != 20">at cart.py:9</failure></testcase>
</testsuite>"""


def ev(n, predicate, stage, version, outcome=None):
    content = {"pipelineName": stage, **({"outcome": outcome} if outcome else {})}
    return {"context": {"version": "0.4.1", "id": f"jenkins-{n}", "source": "jenkins",
                        "type": f"dev.cdevents.pipelinerun.{predicate}.0.2.0",
                        "timestamp": f"2026-11-19T10:0{n}:00Z"},
            "subject": {"id": "build-42", "type": "pipelineRun", "content": content},
            "customData": {"maxpm": {"stage": stage, "version": version}}}


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
        forge._TOKENS.clear()
        self.addCleanup(forge._TOKENS.clear)
        get = mock.patch("river.vault.get", lambda conn, name, **kw: f"value-of-{name}".encode())
        get.start()
        self.addCleanup(get.stop)
        core.project_add(self.c, "app")
        core.register(self.c, "mark", human=True)
        define.pipeline_add(self.c, "app", project="app")
        define.env_set(self.c, "app", "prod")
        define.stage_set(self.c, "app", "build", "build", workflow="jenkins build")
        define.stage_set(self.c, "app", "deploy", "deploy", env="prod", workflow="jenkins deploy")

    def runs(self):
        return {r["stage"]: r for r in self.c.execute(
            "SELECT r.*, s.name stage FROM stage_runs r JOIN pipeline_stages s ON s.id=r.stage_id ORDER BY r.id")}

    def cli(self, words, stdin=""):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("sys.stdin", io.StringIO(stdin)):
            code = cli.run(["-q", *words])
        return code, out.getvalue()


class Command(Base):
    def test_events_on_stdin_make_a_run_and_hand_the_version_on_once(self):
        text = "\n".join(json.dumps(e) for e in (ev(1, "started", "build", "abc1234"),
                                                 ev(2, "finished", "build", "abc1234", "success")))
        code, out = self.cli(["pipeline", "event", "app", "--file", "-"], text)
        self.assertEqual(code or 0, 0, out)
        self.assertIn("pipeline app: 2 event(s) kept", out)
        runs = self.runs()
        self.assertEqual((runs["build"]["state"], runs["build"]["key"]), ("passed", "build-42"))
        self.assertEqual(runs["deploy"]["state"], "queued")  # the version went on to the next stage
        _code, out = self.cli(["pipeline", "event", "app", "--file", "-"], text)  # sent again: nothing changes
        self.assertIn("0 event(s) kept, 2 sent before", out)
        self.assertEqual(len(self.runs()), 2)
        line = self.c.execute("SELECT change FROM pipeline_changes WHERE action='report'").fetchall()
        self.assertEqual([r["change"] for r in line], ["2 event(s) in (maxpm pipeline event)"])

    def test_a_list_or_a_file_and_what_is_wrong(self):
        path = os.path.join(self.dir.name, "events.json")
        with open(path, "w") as f:
            json.dump([ev(1, "queued", "build", "abc1234")], f)
        self.assertIn("1 event(s) kept", self.cli(["pipeline", "event", "app", "--file", path])[1])
        with self.assertRaisesRegex(RiverError, "line 2 is not JSON"):
            intake.parse('{"a": 1}\nnot json')
        with self.assertRaisesRegex(RiverError, "no events"):
            intake.parse("  ")
        with self.assertRaisesRegex(RiverError, "no pipeline"):
            intake.take(self.c, "nope", [ev(1, "queued", "build", "x")])

    def test_steps_and_junit_reported_to_a_run(self):
        intake.take(self.c, "app", [ev(1, "started", "build", "abc1234")])
        run = self.runs()["build"]["id"]
        steps = os.path.join(self.dir.name, "out.txt")
        with open(steps, "w") as f:
            f.write("::step start compile kind=build\nbuilding\n::step end compile passed 45s\n")
        junit = os.path.join(self.dir.name, "junit.xml")
        with open(junit, "wb") as f:
            f.write(JUNIT)
        _code, out = self.cli(["pipeline", "report", str(run), "--steps", steps, "--junit", junit])
        self.assertIn(f"run #{run}: 2 step line(s), 1 other line(s), 2 test(s), 1 failed", out)
        step = self.c.execute("SELECT name, state FROM stage_steps WHERE run_id=?", (run,)).fetchone()
        self.assertEqual((step["name"], step["state"]), ("compile", "passed"))
        self.assertEqual(sorted(t["state"] for t in store.tests(self.c, run)), ["failed", "passed"])
        with self.assertRaisesRegex(RiverError, "nothing to report"):
            intake.report(self.c, run)

    def test_an_agent_may_not_send_events(self):
        core.register(self.c, "w1")
        with self.assertRaisesRegex(RiverError, "only a person or the manager"):
            intake.event_cmd(self.c, "app", json.dumps(ev(1, "queued", "build", "x")), actor="w1")
        with self.assertRaisesRegex(RiverError, "only a person or the manager"):
            intake.set_secret(self.c, "app", "ci-token", actor="w1")


class Http(Base):
    def post(self, body, token=None, headers=None):
        data = json.dumps(body).encode()
        h = server.Handler.__new__(server.Handler)
        h.path, h.client_address, h.rfile = "/api/pipeline-events", ("127.0.0.1", 5555), io.BytesIO(data)
        h.headers = {"Host": "127.0.0.1:8765", "Content-Length": str(len(data)),
                     **({"Authorization": f"Bearer {token}"} if token else {}), **(headers or {})}
        got = []
        h._send = lambda code, answer, ctype=None: got.append((code, answer))
        h.do_POST()
        return got[0]

    def test_the_token_of_the_pipeline_lets_events_in(self):
        body = {"pipeline": "app", "events": [ev(1, "started", "build", "abc1234"),
                                              ev(2, "finished", "build", "abc1234", "success")]}
        code, answer = self.post(body, "value-of-ci-token")
        self.assertEqual(code, 403)
        self.assertIn("maxpm pipeline intake app --secret", answer["error"])
        define_out = intake.set_secret(self.c, "app", "ci-token", actor="mark")
        self.assertEqual(define_out["secret"], "ci-token")
        self.assertEqual(self.post(body)[0], 401)
        self.assertEqual(self.post(body, "value-of-other")[0], 401)
        self.assertEqual(self.post(body, "value-of-ci-token", {"Origin": "http://127.0.0.1:8765"})[0], 403)
        self.assertEqual(self.post(body, "value-of-ci-token", {"Host": "evil.example.com"})[0], 403)
        code, answer = self.post(body, "value-of-ci-token")
        self.assertEqual(code, 200, answer)
        self.assertEqual(answer["result"]["events"], 2)
        self.c.execute("PRAGMA wal_checkpoint")
        runs = self.runs()
        self.assertEqual((runs["build"]["state"], runs["deploy"]["state"]), ("passed", "queued"))
        who = self.c.execute("SELECT actor, kind FROM pipeline_changes WHERE action='report'").fetchone()
        self.assertEqual((who["actor"], who["kind"]), ("token ci-token", "forge"))
        # Steps and JUnit of a run, by the run's id.
        code, answer = self.post({"run": runs["build"]["id"], "steps": "::step end smoke passed 3s\n",
                                  "junit": JUNIT.decode()}, "value-of-ci-token")
        self.assertEqual((code, answer["result"]["tests"]), (200, 2))
        self.assertEqual(self.post({"run": 999}, "value-of-ci-token")[0], 400)
        self.assertEqual(self.post({"pipeline": "nope", "events": []}, "value-of-ci-token")[0], 404)

    def test_a_token_reaches_its_own_pipeline_only(self):
        define.pipeline_add(self.c, "other", project="app")
        define.stage_set(self.c, "other", "build", "build", workflow="b")
        intake.set_secret(self.c, "app", "ci-token", actor="mark")
        intake.set_secret(self.c, "other", "other-token", actor="mark")
        body = {"pipeline": "other", "events": [ev(1, "queued", "build", "x")]}
        self.assertEqual(self.post(body, "value-of-ci-token")[0], 401)
        self.assertEqual(self.post(body, "value-of-other-token")[0], 200)
        intake.set_secret(self.c, "app", None, actor="mark")
        self.assertEqual(self.post({"pipeline": "app", "events": []}, "value-of-ci-token")[0], 403)


if __name__ == "__main__":
    unittest.main()
