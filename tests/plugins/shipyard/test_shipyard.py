"""The shipyard plugin against Shipyard's recorded answers (answers/), with no network.

python3 tests/plugins/shipyard/test_shipyard.py
"""

import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True  # no __pycache__ beside the plugin
sys.path.insert(0, HERE)
import plugin  # noqa: E402

ANSWERS = os.path.join(HERE, "answers")
BASE = "https://ci.example.com/api/projects/shop"


def answer(name):
    with open(os.path.join(ANSWERS, name), "rb") as f:
        return f.read()


class FakeShipyard:
    """Serves the recorded answers. After a start, 'after=7' lists deploy 103 with the tag that the start sent."""

    def __init__(self, key="good"):
        self.key, self.calls, self.tag = key, [], None

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if headers.get("X-Shipyard-Key") != "good":
            return 401, answer("error-401.json")
        path = url[len(BASE):]
        if method == "GET" and path == "/deploys?after=0":
            return 200, answer("deploys-after-0.json")
        if method == "GET" and path == "/deploys?after=7":
            if self.tag is None:
                return 200, answer("deploys-after-7.json")
            return 200, answer("deploys-after-7-started.json").replace(b"maxpm-run-REPLACED", self.tag.encode())
        if method == "GET" and path == "/deploys/101/results":
            return 200, answer("results-101.xml")
        if method == "GET" and path.endswith("/results"):
            return 404, b'{"problem": "no results"}'
        if method == "POST" and path == "/deploys":
            self.tag = json.loads(body)["tag"]
            return 200, answer("start-deploy.json")
        if method == "POST" and path.endswith("/stop"):
            return 200, answer("stop.json")
        if method == "POST" and path.endswith("/again"):
            return 200, b'{"id": 104}'
        return 404, b'{"problem": "not recorded"}'


PIPELINE = {"name": "shop", "project": "shop", "envs": [{"name": "prod", "target": None, "version": None}],
            "stages": [{"name": "build", "kind": "build", "pos": 1, "work": "build", "env": None,
                        "transition": "auto", "junit": None},
                       {"name": "deploy", "kind": "deploy", "pos": 2, "work": "deploy", "env": "prod",
                        "transition": "auto", "junit": None}]}


def request(call, state=None, runs=(), run=None, key="good"):
    req = {"contract": 1, "call": call, "pipeline": PIPELINE, "settings": {"url": "https://ci.example.com/",
           "project": "shop"}, "secrets": {"key": key}, "state": state, "runs": list(runs)}
    if run is not None:
        req["run"] = run
    return req


def types(events):
    return [(e["context"]["type"].split(".")[2] + "." + e["context"]["type"].split(".")[3], e["subject"]["id"])
            for e in events]


class Shipyard(unittest.TestCase):
    def test_describe_matches_manifest(self):
        out = plugin.handle({"contract": 1, "call": "describe"}, None)
        self.assertEqual(out, {"name": "shipyard", "version": "1.0.0", "level": "act",
                               "actions": ["start", "stop", "rerun"]})

    def test_first_poll(self):
        fake = FakeShipyard()
        out = plugin.handle(request("poll"), fake)
        self.assertNotIn("error", out)
        self.assertEqual(types(out["events"]), [
            ("pipelinerun.started", "101"), ("pipelinerun.finished", "101"),
            ("taskrun.started", "101/compile"), ("taskrun.finished", "101/compile"),
            ("taskrun.started", "101/unit tests"), ("taskrun.finished", "101/unit tests"),
            ("pipelinerun.started", "102"), ("taskrun.started", "102/compile")])
        finished = out["events"][1]
        self.assertEqual(finished["subject"]["content"]["outcome"], "success")
        self.assertEqual(finished["subject"]["content"]["url"], "https://ci.example.com/p/shop/deploys/101")
        self.assertEqual(finished["customData"]["maxpm"]["stage"], "build")
        self.assertTrue(finished["customData"]["maxpm"]["version"].startswith("3f2a9c1"))
        self.assertNotIn("run", finished["customData"]["maxpm"])  # a push started it: no MaximizePM run
        self.assertEqual(out["events"][2]["customData"]["maxpm"]["run_key"], "101")
        ids = [e["context"]["id"] for e in out["events"]]
        self.assertEqual(len(ids), len(set(ids)))
        # The results of 101 once; 102 still runs, so nobody asks for its results.
        self.assertEqual([r["run_key"] for r in out["reports"]], ["101"])
        self.assertIn('name="test_tax"', out["reports"][0]["xml"])
        self.assertEqual([c[1] for c in fake.calls if c[1].endswith("/results")], [BASE + "/deploys/101/results"])
        self.assertEqual(out["state"], {"cursor": 7})
        self.assertEqual(fake.calls[0][2]["X-Shipyard-Key"], "good")

    def test_second_poll_gives_nothing_new(self):
        fake = FakeShipyard()
        first = plugin.handle(request("poll"), fake)
        second = plugin.handle(request("poll", state=first["state"]), fake)
        self.assertEqual(second, {"events": [], "reports": [], "state": {"cursor": 7}})
        self.assertEqual(fake.calls[-1][1], BASE + "/deploys?after=7")

    def test_start_links_the_deploy_to_the_run(self):
        fake = FakeShipyard()
        run = {"id": 42, "stage": "deploy", "work": "deploy", "version": "3f2a9c1", "key": None, "state": "queued"}
        out = plugin.handle(request("start", state={"cursor": 7}, runs=[run], run=run), fake)
        self.assertEqual(out["key"], "103")
        method, url, headers, body = fake.calls[-1]
        self.assertEqual((method, url), ("POST", BASE + "/deploys"))
        self.assertEqual(json.loads(body), {"job": "deploy", "commit": "3f2a9c1", "tag": "maxpm-run-42"})
        # The next poll, before MaximizePM saved the key: the tag links deploy 103 to run 42.
        out = plugin.handle(request("poll", state={"cursor": 7}), fake)
        self.assertEqual(types(out["events"])[:2], [("pipelinerun.started", "103"), ("pipelinerun.finished", "103")])
        for e in out["events"][:2]:
            self.assertEqual(e["customData"]["maxpm"]["run"], 42)
            self.assertEqual(e["customData"]["maxpm"]["stage"], "deploy")
        self.assertEqual(out["reports"], [])  # 103 wrote no results (404)
        self.assertEqual(out["state"], {"cursor": 9})
        # And with the key saved on the run, the key links it too.
        keyed = dict(run, key="103", state="running")
        out = plugin.handle(request("poll", state={"cursor": 7}, runs=[keyed]), fake)
        self.assertEqual(out["events"][0]["customData"]["maxpm"]["run"], 42)

    def test_stop(self):
        fake = FakeShipyard()
        run = {"id": 43, "stage": "build", "work": "build", "version": "9b8c7d6", "key": "102", "state": "running"}
        out = plugin.handle(request("stop", run=run), fake)
        self.assertNotIn("error", out)
        self.assertEqual(fake.calls[-1][:2], ("POST", BASE + "/deploys/102/stop"))

    def test_rerun_runs_the_deploy_again(self):
        fake = FakeShipyard()
        run = {"id": 44, "stage": "build", "work": "build", "version": "3f2a9c1", "key": "101~40", "state": "failed"}
        out = plugin.handle(request("rerun", run=run), fake)
        self.assertEqual(out["key"], "104")
        self.assertEqual(fake.calls[-1][:2], ("POST", BASE + "/deploys/101/again"))

    def test_rollback_is_not_done(self):
        out = plugin.handle(request("rollback", run={"id": 1}), FakeShipyard())
        self.assertIn("no roll back", out["error"])

    def test_wrong_key(self):
        out = plugin.handle(request("poll", key="bad"), FakeShipyard())
        self.assertEqual(set(out), {"error"})
        self.assertIn("401", out["error"])
        self.assertIn("the key is not valid", out["error"])

    def test_main_exits_1_with_error(self):
        # The program as MaximizePM runs it: a request with no settings fails before any network call.
        req = json.dumps({"contract": 1, "call": "poll", "settings": {}, "secrets": {}, "state": None})
        p = subprocess.run([sys.executable, "plugin.py"], cwd=HERE, input=req, capture_output=True, text=True)
        self.assertEqual(p.returncode, 1)
        self.assertIn("url", json.loads(p.stdout)["error"])
        p = subprocess.run([sys.executable, "plugin.py"], cwd=HERE, input='{"contract": 1, "call": "describe"}',
                           capture_output=True, text=True)
        self.assertEqual((p.returncode, json.loads(p.stdout)["name"]), (0, "shipyard"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
