"""Runners (#6151): a server where a trigger rule's agent works with no person, terminal or prompt. Over SSH only
through fakes: host.SSH_RUNNER for the probe, the install and the stop; runners.SPAWN for the run's ssh process."""

import io
import json
import os
import unittest
from email.message import Message
from unittest import mock

from river import client, core, host, runners, server
from river.core import RiverError
from river.pipeline import define, triggers

from tests.test_pipeline import RunnerBase

PROBE = "Linux x86_64\n--\nmaxpm 0.0.1\n--\nclaude 2.1.328 (Claude Code)\ncodex codex-cli 0.159.1\n--\n100.64.0.7\n"


class FakeProc:
    def __init__(self, code=None):
        self.pid, self.code, self.terminated = 4242, code, False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = -15


class Base(RunnerBase):
    def setUp(self):
        super().setUp()
        self.ssh = []

        def ssh(argv, data, timeout):
            self.ssh.append(argv[-1])
            return (0, PROBE, "") if argv[-1].startswith("uname") else (0, "", "")
        host.SSH_RUNNER = ssh
        self.addCleanup(setattr, host, "SSH_RUNNER", None)
        patch = mock.patch.object(host, "install", lambda target: self.ssh.append("install " + target))
        patch.start()
        self.addCleanup(patch.stop)
        self.spawned = []

        def spawn(argv, script, path):
            self.spawned.append({"argv": argv, "script": script, "path": path})
            self.proc = FakeProc()
            return self.proc
        for name, value in (("SPAWN", spawn), ("GIT", lambda folder: "git@example.com:me/app.git"),
                            ("SERVE_PORT", 8765)):
            setattr(runners, name, value)
            self.addCleanup(setattr, runners, name, None)
        self.addCleanup(runners.PROCS.clear)

    def add(self):
        return runners.add(self.c, "ci-1", "ci@ci-1", actor="mark")

    def fault_on_runner(self):
        """A rule of pipeline app that runs its agent on ci-1, and a failed build: the fault task goes to ci-1."""
        self.add()
        runners.allow_pipeline(self.c, "app", "ci-1", actor="mark")
        rule = triggers.rules(self.c, "app")["rules"][0]
        triggers.rule_edit(self.c, "app", rule["name"], runner="ci-1", actor="mark")
        define.stage_set(self.c, "app", "build", run="exit 3")
        self.runner.start(self.c, "app", rev="bad0001")
        self.passes(2)
        run = self.c.execute("SELECT * FROM runner_runs").fetchone()
        return run


class List(Base):
    def test_add_checks_the_server_installs_maxpm_and_finds_its_agent_clis(self):
        res = self.add()
        r = res["runner"]
        self.assertEqual((r["state"], r["system"], r["tailnet"], r["cli"]), ("ok", "Linux x86_64", "100.64.0.7", "claude"))
        self.assertEqual(r["clis"], {"claude": "2.1.328 (Claude Code)", "codex": "codex-cli 0.159.1"})
        self.assertIn("install ci@ci-1", self.ssh)  # its maxpm had another version
        self.assertTrue(any("installed maxpm" in s for s in res["steps"]))
        with self.assertRaises(RiverError):  # a session that is not the manager adds no runner
            runners.add(self.c, "ci-2", "ci@ci-2", actor="w1")
        with self.assertRaises(RiverError):
            runners.add(self.c, "Bad Name", "ci@ci-2", actor="mark")
        listing = runners.listing(self.c)
        self.assertEqual([x["name"] for x in listing["runners"]], ["ci-1"])
        self.assertEqual(listing["this"]["rules"][0]["pipeline"], "app")  # the rule of before runs here

    def test_a_server_that_does_not_answer_is_marked_bad(self):
        self.add()
        host.SSH_RUNNER = lambda argv, data, timeout: (255, "", "Permission denied (publickey)")
        r = runners.check(self.c, "ci-1", actor="mark")
        self.assertEqual(r["state"], "bad")
        self.assertIn("Permission denied", r["error"])

    def test_a_pipeline_ticks_the_runners_its_rules_may_use(self):
        self.add()
        rule = triggers.rules(self.c, "app")["rules"][0]["name"]
        with self.assertRaises(RiverError) as e:
            triggers.rule_edit(self.c, "app", rule, runner="ci-1", actor="mark")
        self.assertIn("maxpm runner allow ci-1 app", str(e.exception))
        runners.allow_pipeline(self.c, "app", "ci-1", actor="mark")
        self.assertEqual(triggers.rules(self.c, "app")["runners"], ["this computer", "ci-1"])
        triggers.rule_edit(self.c, "app", rule, runner="ci-1", actor="mark")
        for refused in (lambda: runners.allow_pipeline(self.c, "app", "ci-1", on=False, actor="mark"),
                        lambda: runners.remove(self.c, "ci-1", actor="mark")):
            with self.assertRaises(RiverError):  # a rule names it
                refused()
        self.assertEqual(runners.listing(self.c)["runners"][0]["used_by"], ["app"])
        self.assertEqual(define.export(self.c, "app")["runners"], ["ci-1"])
        self.assertTrue(any("runner ci-1 may run" in h["change"] for h in define.history(self.c, "app")))


class Runs(Base):
    def test_a_rule_gives_its_fault_to_the_runner_and_serve_starts_it_there(self):
        run = self.fault_on_runner()
        item = self.c.execute("SELECT * FROM items WHERE id=?", (run["item"],)).fetchone()
        self.assertEqual((run["state"], run["agent"], item["reserved_for"]), ("queued", "ci-1-r1", "ci-1-r1"))
        self.assertIn("runs on ci-1", self.c.execute("SELECT result FROM pipeline_trigger_fires").fetchone()[0])
        self.assertEqual(server.auto_operational(self.c, runner=lambda line: None)["started"], {})  # none here
        out = runners.serve_pass(self.c)
        self.assertEqual(out["started"], [run["id"]])
        s = self.spawned[0]
        argv = " ".join(s["argv"])
        self.assertIn("-R 127.0.0.1:", argv)
        self.assertIn(":127.0.0.1:8765 ci@ci-1 sh -s", argv)
        self.assertNotIn("MAXPM_TOKEN", argv)  # the keys go on stdin, never into a command line
        self.assertIn("MAXPM_TOKEN=", s["script"])
        self.assertIn("git clone -q git@example.com:me/app.git", s["script"])
        self.assertIn("bad0001", s["script"])  # the worktree starts at the failed version
        self.assertIn("claude -p", s["script"])
        self.assertIn("--max-budget-usd 5", s["script"])
        self.assertIn("--permission-mode dontAsk", s["script"])
        run = self.c.execute("SELECT * FROM runner_runs").fetchone()
        self.assertEqual(run["state"], "running")
        tok = self.c.execute("SELECT host, scope FROM session_tokens WHERE agent=?", (run["agent"],)).fetchone()
        self.assertEqual(tok["host"], "runner:ci-1")
        self.assertIn(str(run["item"]), self.c.execute("SELECT scope FROM agents WHERE name=?",
                                                       (run["agent"],)).fetchone()[0])
        self.assertEqual(runners.serve_pass(self.c)["started"], [])  # one slot: busy

    def key_and_token(self):
        run = self.fault_on_runner()
        runners.serve_pass(self.c)
        script = self.spawned[0]["script"]
        key = script.split("MAXPM_RUNNER_KEY=", 1)[1].split()[0].strip("'")
        token = script.split("MAXPM_TOKEN=", 1)[1].split()[0].strip("'")
        return run, key, token

    def test_a_command_from_the_runner_needs_the_runs_key_and_its_sessions_token(self):
        run, key, token = self.key_and_token()
        req = {"argv": ["now", "reading the logs"], "cwd": "/home/ci/app", "stamp": 1, "ppid": 9,
               "env": {core.TOKEN_ENV: token, "AWS_SECRET_ACCESS_KEY": "x", "MAXPM_QUIET": "1"}}
        out = runners.request_for(self.c, key, req)
        self.assertEqual(out["cwd"], os.path.realpath(self.folder))  # the project's folder on this computer
        self.assertEqual(out["stamp"], client.code_stamp())
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", out["env"])  # nothing else of the runner's environment
        self.assertEqual((out["env"]["MAXPM_AGENT"], out["env"]["MAXPM_QUIET"]), (run["agent"], "1"))
        self.assertEqual(core.token_session(self.c, token, [(1, 0, "zsh", 0)])["agent"], run["agent"])
        for bad_key, bad_env in (("wrong", {core.TOKEN_ENV: token}), (key, {}), (key, {core.TOKEN_ENV: "mxt_x"})):
            with self.assertRaises(RiverError):
                runners.request_for(self.c, bad_key, dict(req, env=bad_env))
        got = {}

        def fake_run(r):
            got.update(r)
            return {"code": 0, "out": "ok\n", "err": ""}

        def call(headers):
            h = server.Handler.__new__(server.Handler)
            raw = json.dumps(req).encode()
            h.path, h.client_address, h.headers, out = "/api/runner-run", ("127.0.0.1", 5555), Message(), {}
            for k, v in {"Host": "127.0.0.1:41000", "Content-Length": str(len(raw)), **headers}.items():
                h.headers[k] = v
            h.rfile = io.BytesIO(raw)
            h._send = lambda code, body, ctype=None: out.update(code=code, body=body)
            h.do_POST()
            return out["code"], out["body"]
        with mock.patch.object(server, "run_command", fake_run):
            self.assertEqual(call({"X-Maxpm-Runner": key}), (200, {"code": 0, "out": "ok\n", "err": ""}))
            self.assertEqual(got["cwd"], os.path.realpath(self.folder))
            self.assertEqual(call({"X-Maxpm-Runner": "wrong"})[0], 403)
            self.assertEqual(call({"X-Maxpm-Token": key})[0], 403)  # /api/runner-run takes no serve token

    def test_the_runners_session_takes_reports_and_closes_its_task_through_serve(self):
        import contextlib
        from river import cli
        run, key, token = self.key_and_token()
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        outs = []
        for argv in (["go"], ["now", "reading the build log"],
                     ["done", str(run["item"]), "--output", "fixed the build", "--no-proof", "a test pipeline"]):
            req = runners.request_for(self.c, key, {"argv": argv, "env": {core.TOKEN_ENV: token}})
            out = io.StringIO()
            with mock.patch.dict(os.environ, req["env"], clear=True), contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(io.StringIO()):
                os.chdir(req["cwd"])
                self.assertEqual(cli.run(req["argv"]), 0)
            outs.append(out.getvalue())
        self.assertIn(f"You are MaximizePM agent {run['agent']}. Role: WORKER. (runner ci-1 started this session",
                      outs[0])
        self.assertNotIn("outside tmux", outs[0])
        item = self.c.execute("SELECT status, assignee FROM items WHERE id=?", (run["item"],)).fetchone()
        self.assertEqual(tuple(item), ("done", run["agent"]))

    def test_the_end_of_a_run_keeps_cost_and_last_words_and_frees_the_task(self):
        run, key, token = self.key_and_token()
        path = self.spawned[0]["path"]
        with open(path, "w") as f:
            f.write("maxpm: run 1 of task starts\n")
            f.write(json.dumps({"type": "system", "subtype": "init", "model": "claude-opus-5-5", "cwd": "/w"}) + "\n")
            f.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "The build fails on a missing file."},
                {"type": "tool_use", "name": "Bash", "input": {"command": "make prod-release-prepare"}}]}}) + "\n")
            f.write(json.dumps({"type": "result", "is_error": False, "result": "Released: needs a person.",
                                "total_cost_usd": 0.42}) + "\n")
        self.proc.code = 0
        out = runners.serve_pass(self.c)
        self.assertEqual(out["ended"][0]["state"], "ended")
        r = self.c.execute("SELECT * FROM runner_runs").fetchone()
        self.assertEqual((r["state"], r["cost"], r["result"]), ("ended", 0.42, "Released: needs a person."))
        item = self.c.execute("SELECT * FROM items WHERE id=?", (r["item"],)).fetchone()
        self.assertIsNone(item["reserved_for"])  # back to the queue
        with self.assertRaises(RiverError):  # the run's token and key are spent
            core.token_session(self.c, token, None)
        with self.assertRaises(RiverError):
            runners.request_for(self.c, key, {"argv": ["go"], "env": {core.TOKEN_ENV: token}})
        lines = runners.log(self.c, r["id"])["lines"]
        self.assertEqual([x["kind"] for x in lines], ["out", "start", "say", "tool", "end"])
        self.assertIn("make prod-release-prepare", lines[3]["text"])
        self.assertIn("$0.42", lines[4]["text"])

    def test_serve_stops_a_run_after_max_minutes(self):
        self.key_and_token()
        with core.tx(self.c):
            self.c.execute("UPDATE runner_runs SET started_at=?", (core.iso(core.now() - core.timedelta(minutes=61)),))
        self.assertEqual(runners.serve_pass(self.c)["stopped"], [1])
        self.assertTrue(self.proc.terminated)
        self.assertTrue(any("run1.pid" in c and "kill -TERM" in c for c in self.ssh))
        runners.serve_pass(self.c)
        r = self.c.execute("SELECT * FROM runner_runs").fetchone()
        self.assertEqual((r["state"], r["stopped_by"]), ("stopped", "maxpm"))
        self.assertIn("max_minutes", r["result"])

    def test_a_run_whose_project_has_no_origin_fails_with_the_reason(self):
        runners.GIT = lambda folder: None
        run = self.fault_on_runner()
        out = runners.serve_pass(self.c)
        self.assertEqual(out["ended"][0]["state"], "failed")
        self.assertIn("no git remote origin", self.c.execute("SELECT result FROM runner_runs WHERE id=?",
                                                             (run["id"],)).fetchone()[0])
        self.assertEqual(self.spawned, [])


class ClientOnARunner(unittest.TestCase):
    """maxpm on a runner sends each command through the tunnel, and never runs one on a queue of its own."""

    def setUp(self):
        self.addCleanup(client.ROUTE.update, path="/api/run", header="X-Maxpm-Token")

    def test_commands_go_to_the_runner_route_and_a_refusal_ends_there(self):
        env = {"MAXPM_RUNNER_PORT": "41000", "MAXPM_RUNNER_KEY": "k1", "MAXPM_RUNNER": "ci-1", "MAXPM_LOCAL": "1"}
        seen = []

        def post(port, body, token, timeout):
            seen.append((port, token, dict(client.ROUTE)))
            return 403, b'{"error": "this key is not of a run that runs now"}'
        with mock.patch.object(client, "_post", post), mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(client.run_remote(["now", "reading"], env=env), 2)
        self.assertEqual(seen[0], (41000, "k1", {"path": "/api/runner-run", "header": "X-Maxpm-Runner"}))
        self.assertIn("this runner has no queue of its own", err.getvalue())
        self.assertIsNone(client.run_remote(["review", "pass", "12"], env=env))  # not one serve runs: main refuses it
        with mock.patch.dict(os.environ, env):
            self.assertIn("runs only on the computer of the queue", client.runner_refusal(["review", "pass", "12"]))


if __name__ == "__main__":
    unittest.main()
