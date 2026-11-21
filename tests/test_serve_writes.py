"""#5975: only maxpm serve writes the queue. A caller reads the files and stdin of a command and serve's runner gets
their text; a process that cannot write the queue file reads it and refuses a write with the next step."""
import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from river import cli, client, core

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ServeWrites(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.db = os.path.join(self.dir.name, "q.db")
        self.env = {"MAXPM_DB": self.db, "PATH": os.environ.get("PATH", "")}

    def test_serve_writes_the_caller_names_the_files_it_reads(self):
        r = client.reads
        self.assertEqual(r(["canvas", "put", "p.html", "--name", "n"]), [(2, "", "p.html")])
        self.assertEqual(r(["--as", "a", "canvas", "put", "--name", "n", "-q", "p.md"]), [(7, "", "p.md")])
        self.assertEqual(r(["canvas", "section", "doc", "Plan", "plan.md"]), [(4, "", "plan.md")])
        self.assertEqual(r(["canvas", "put", "--name", "n", "--text", "hi"]), [])
        self.assertEqual(r(["goal", "handoff", "g", "--file", "h.md"]), [(4, "", "h.md")])
        self.assertEqual(r(["add", "web", "--from=plan.md"]), [(2, "--from=", "plan.md")])
        self.assertEqual(r(["canvas", "section", "doc", "Plan", "-"]), [(4, "", "-")])
        self.assertEqual(r(["who", "--file", "river/core.py"]), [])  # a path as text, not a file it reads
        self.assertIsNone(r(["goal", "handoff", "g", "--file"]))

    def test_serve_writes_a_command_with_a_file_goes_to_serve_with_its_text(self):
        with open(self.db + "-serve.json", "w", encoding="utf-8") as f:
            json.dump({"port": 1, "token": "t"}, f)
        page = os.path.join(self.dir.name, "page.md")
        with open(page, "w", encoding="utf-8") as f:
            f.write("# Plan\n")
        sent = []

        def post(port, body, token, timeout):
            sent.append(json.loads(body))
            return 200, b'{"code": 0, "out": "ok\\n"}'
        with mock.patch.object(client, "_post", post), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(client.run_remote(["canvas", "put", page, "--name", "plan"], self.env), 0)
            with mock.patch.object(sys, "stdin", io.StringIO("from stdin")):
                self.assertEqual(client.run_remote(["canvas", "put", "-", "--name", "n"], self.env), 0)
        self.assertEqual(sent[0]["files"], [{"at": 2, "prefix": "", "name": "page.md", "text": "# Plan\n",
                                             "source": page}])
        self.assertEqual((sent[1]["files"], sent[1]["stdin"]), ([], "from stdin"))
        # Stays here: a file it cannot read, a page with a local picture, a canvas it writes to a file, stdin as a title.
        pic = os.path.join(self.dir.name, "pic.md")
        with open(pic, "w", encoding="utf-8") as f:
            f.write("![chart](chart.png) and ![web](https://x.test/a.png)\n")
        with mock.patch.object(client, "_post", side_effect=AssertionError("asked serve")):
            for words in (["canvas", "put", "nofile.html", "--name", "n"], ["canvas", "save", "plan", "p.md"],
                          ["canvas", "put", pic, "--name", "n"], ["add", "x", "-"]):
                self.assertIsNone(client.run_remote(words, self.env), words)
        # serve does not answer: the command here reads the text that the client read from stdin.
        with mock.patch.object(client, "_post", return_value=(None, None)), \
                mock.patch.object(sys, "stdin", io.StringIO("again")):
            self.assertIsNone(client.run_remote(["canvas", "put", "-", "--name", "n"], self.env))
            self.assertEqual(sys.stdin.read(), "again")

    def test_serve_writes_the_runner_reads_the_callers_text(self):
        c = core.connect(self.db)
        core.project_add(c, "web", path=self.dir.name)
        core.register(c, "ag")
        c.close()
        proc = subprocess.Popen([sys.executable, "-m", "river.runner", self.db], cwd=ROOT, text=True,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.addCleanup(lambda: (proc.stdin.close(), proc.wait(timeout=10), proc.stdout.close()))
        req = {"argv": ["-q", "canvas", "put", "/nowhere/plan.md", "--name", "plan", "--project", "web"],
               "cwd": self.dir.name, "stamp": client.code_stamp(), "ppid": os.getppid(),
               "env": {**self.env, "MAXPM_AGENT": "ag"},
               "files": [{"at": 3, "prefix": "", "name": "plan.md", "text": "# The plan\n"}]}
        proc.stdin.write(json.dumps(req) + "\n")
        proc.stdin.flush()
        ans = json.loads(proc.stdout.readline())
        self.assertEqual(ans["code"], 0, ans)
        c = core.connect(self.db)
        self.addCleanup(c.close)
        text = c.execute("SELECT v.body FROM canvas_versions v JOIN canvases k ON k.id=v.canvas_id "
                         "WHERE k.name='plan'").fetchone()[0]
        self.assertEqual(text, "# The plan\n")

    @unittest.skipIf(os.geteuid() == 0, "root ignores read-only file modes")
    def test_serve_writes_a_queue_this_process_cannot_write_reads_and_refuses_a_write(self):
        serve = core.connect(self.db)  # serve's connection keeps the queue open (its WAL files stay)
        self.addCleanup(serve.close)
        core.project_add(serve, "web")
        x = core.item_add(serve, "web", "Build the cart")["id"]
        files = [p for p in os.listdir(self.dir.name) if p.startswith("q.db")]
        for p in files:
            os.chmod(os.path.join(self.dir.name, p), stat.S_IRUSR)
        self.addCleanup(lambda: [os.chmod(os.path.join(self.dir.name, p), stat.S_IRUSR | stat.S_IWUSR) for p in files])

        def run(*words):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                    mock.patch.dict(os.environ, {"MAXPM_DB": self.db, "MAXPM_QUIET": "1"}):
                return cli.run(list(words)), out.getvalue()
        code, out = run("show", str(x))
        self.assertEqual(code, 0)
        self.assertIn("Build the cart", out)
        self.assertEqual(run("list")[0], 0)
        with self.assertRaisesRegex(core.RiverError, "only maxpm serve writes the queue"):
            run("add", "web", "Fix the header")
        self.assertEqual(serve.execute("SELECT count(*) FROM items").fetchone()[0], 1)


class ServeWritesCommands(unittest.TestCase):
    """#6009: the commands that wrote in their own process go to serve; review pass runs its command here and sends
    the record."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.db = os.path.join(self.dir.name, "q.db")
        self.env = {"MAXPM_DB": self.db, "PATH": os.environ.get("PATH", "")}

    def test_serve_writes_which_commands_go_to_serve(self):
        with open(self.db + "-serve.json", "w", encoding="utf-8") as f:
            json.dump({"port": 1, "token": "t"}, f)
        notes = os.path.join(self.dir.name, "notes.md")
        with open(notes, "w", encoding="utf-8") as f:
            f.write("what I learned\n")
        for words in (["--as", "boss", "scope", "set", "w1", "project:web"], ["pause", "--reason", "x"], ["resume"],
                      ["review", "fail", "3", "fix it"], ["expert", "request", "read it", "--project", "web"],
                      ["expert"], ["expert", "notes", "fab", "--put", notes], ["report", "--state", "idle"]):
            with mock.patch.object(client, "_post", return_value=(200, b'{"code": 0}')) as post:
                self.assertEqual(client.run_remote(words, self.env), 0, words)
            post.assert_called_once()
        with mock.patch.object(client, "_post", side_effect=AssertionError("asked serve")):
            for words in (["review", "pass", "3"], ["report", "--hook", "claude"], ["expert", "start", "fab"],
                          ["report", "--state", "idle", "--hook", "x"]):
                self.assertIsNone(client.run_remote(words, self.env), words)

    @unittest.skipIf(os.geteuid() == 0, "root ignores read-only file modes")
    def test_serve_writes_review_pass_runs_here_and_records_through_serve(self):
        serve = core.connect(self.db)
        self.addCleanup(serve.close)
        core.project_add(serve, "web", path=self.dir.name)
        for n in ("w1", "w2"):
            core.register(serve, n)
        core.target_add(serve, "site", "push")
        core.project_target(serve, "web", "site")
        core.config_set(serve, "review", "on")
        x = core.item_add(serve, "web", "Build the cart")["id"]
        core.claim(serve, x, "w1")
        core.done(serve, x, "built", "w1")
        core.ship(serve, x, "w1")
        rv = serve.execute("SELECT id FROM items WHERE kind='review'").fetchone()[0]
        core.claim(serve, rv, "w2")
        mark = os.path.join(self.dir.name, "ran")
        serve.execute("UPDATE items SET \"check\"=? WHERE id=?", (f"touch {mark}", rv))
        files = [p for p in os.listdir(self.dir.name) if p.startswith("q.db")]
        for p in files:
            os.chmod(os.path.join(self.dir.name, p), stat.S_IRUSR)
        self.addCleanup(lambda: [os.chmod(os.path.join(self.dir.name, p), stat.S_IRUSR | stat.S_IWUSR)
                                 for p in files])
        asked = []

        def ask(conn, op, args, actor=None, **kw):
            asked.append((op, args, actor))
            return {"id": rv, "status": "done"}
        with mock.patch.object(cli, "ask_server", ask), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.dict(os.environ, {"MAXPM_DB": self.db, "MAXPM_QUIET": "1"}):
            os.chdir(self.dir.name)
            self.addCleanup(os.chdir, ROOT)
            self.assertEqual(cli.run(["--as", "w2", "review", "pass", str(rv), "--output", "checked"]), 0)
        self.assertTrue(os.path.exists(mark))  # the review command ran here
        self.assertEqual([(op, a["id"], a["ran"], a["output"], who) for op, a, who in asked],
                         [("review_record", rv, f"touch {mark}", "checked", "w2")])
        # serve records it.
        for p in files:
            os.chmod(os.path.join(self.dir.name, p), stat.S_IRUSR | stat.S_IWUSR)
        from river import server
        res = server.OPS["review_record"](serve, asked[0][1], "w2")
        self.assertEqual(res["status"], "done")


class ServeWritesStale(unittest.TestCase):
    def test_serve_writes_a_stale_runner_is_asked_again_once(self):
        """#6018: serve stops a runner with older code, so the second request reaches one with the new code."""
        answers = [(409, b'{"error": "stale"}'), (200, b'{"code": 0, "out": "ok\\n"}')]
        with mock.patch.object(client, "_post", side_effect=answers) as post:
            self.assertEqual(client._post_fresh(1, b"{}", "t"), (200, b'{"code": 0, "out": "ok\\n"}'))
        self.assertEqual(post.call_count, 2)
        answers = [(409, b'{"error": "another queue"}')]
        with mock.patch.object(client, "_post", side_effect=answers) as post:
            self.assertEqual(client._post_fresh(1, b"{}", "t")[0], 409)
        self.assertEqual(post.call_count, 1)
        answers = [(409, b'{"error": "stale"}')] * 2
        with mock.patch.object(client, "_post", side_effect=answers) as post:
            self.assertEqual(client._post_fresh(1, b"{}", "t")[0], 409)  # once only
        self.assertEqual(post.call_count, 2)


class ServeWritesPolls(unittest.TestCase):
    """#6008: a wait runs as polls in serve; the caller sleeps between them and keeps the wait's state."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.db = os.path.join(self.dir.name, "q.db")
        self.c = core.connect(self.db)
        self.addCleanup(self.c.close)
        core.project_add(self.c, "web", path=self.dir.name)
        core.register(self.c, "w")
        self.addCleanup(setattr, core, "POLL", None)

    def test_serve_writes_a_wait_as_polls_keeps_its_token_and_its_clock(self):
        core.POLL = {"id": "abcd1234", "state": {}}
        self.assertIs(core.wait(self.c, self.dir.name, "w"), core.POLL_AGAIN_RESULT)
        state = dict(core.POLL["state"])
        a = core._agent(self.c, "w")
        self.assertEqual((a["wait_token"], a["role"], state["started"]), ("abcd1234", "waiting", a["wait_at"]))
        core.POLL = {"id": "abcd1234", "state": state}
        self.assertIs(core.wait(self.c, self.dir.name, "w"), core.POLL_AGAIN_RESULT)
        self.assertEqual(core._agent(self.c, "w")["wait_at"], state["started"])  # a poll does not start it again
        x = core.item_add(self.c, "web", "Build the cart")["id"]
        res = core.wait(self.c, self.dir.name, "w")
        self.assertEqual((res["result"], res["why"]), ("work", f"#{x} is ready: Build the cart"))
        # A newer wait of the session (another id) ends this one quietly.
        core.drop(self.c, x, "not needed", "w")
        core.POLL = {"id": "ffff0000", "state": {}}
        core.wait(self.c, self.dir.name, "w")
        core.POLL = {"id": "abcd1234", "state": state}
        self.assertEqual(core.wait(self.c, self.dir.name, "w")["result"], "left")
        # The wait's step counts from its first poll.
        core.POLL = {"id": "ffff0000", "state": {"started": core.iso(core.now() - core.timedelta(hours=1)),
                                                 "scope_at": None}}
        self.assertEqual(core.wait(self.c, self.dir.name, "w")["result"], "again")

    def test_serve_writes_an_inbox_wait_as_polls(self):
        core.POLL = {"id": "i1", "state": {}}
        self.assertIs(core.inbox_wait(self.c, "w"), core.POLL_AGAIN_RESULT)
        self.assertIn("since", core.POLL["state"])
        core.send(self.c, "note", "hello", to="w", actor="w2")
        res = core.inbox_wait(self.c, "w")
        self.assertEqual((res["result"], [m["body"] for m in res["messages"]]), ("messages", ["hello"]))
        core.POLL = {"id": "i1", "state": {"since": core.iso(core.now() - core.timedelta(hours=1))}}
        self.assertEqual(core.inbox_wait(self.c, "w")["result"], "timeout")

    def test_serve_writes_the_client_polls_and_sleeps_between(self):
        answers = [(200, b'{"code": 75, "out": "", "err": "", "poll": {"started": "t0"}}'), (503, b""),
                   (200, b'{"code": 75, "out": "", "err": "", "poll": {"started": "t0", "seen": {}}}'),
                   (200, b'{"code": 0, "out": "WORK: #3 is ready. Run now: maxpm --as w go\\n", "err": ""}')]
        sent, naps = [], []

        def post(port, body, token, timeout):
            sent.append(json.loads(body)["poll"])
            return answers[len(sent) - 1]
        out = io.StringIO()
        with mock.patch.object(client, "_post", post), contextlib.redirect_stdout(out):
            code = client._poll_loop(1, "t", {"argv": ["wait"]}, sleep=naps.append)
        self.assertEqual((code, out.getvalue()), (0, "WORK: #3 is ready. Run now: maxpm --as w go\n"))
        self.assertEqual([p["state"] for p in sent], [{}, {"started": "t0"}, {"started": "t0"},
                                                      {"started": "t0", "seen": {}}])
        self.assertEqual(len({p["id"] for p in sent}), 1)
        self.assertEqual(naps, [client.POLL_SLEEP] * 3)
        # serve does not answer the first poll: the wait runs here; it stops answering later: it waits, then here.
        with mock.patch.object(client, "_post", return_value=(None, None)):
            self.assertIsNone(client._poll_loop(1, "t", {"argv": ["wait"]}, sleep=naps.append))
        clock = iter(range(0, 1000, 60))
        answers[1:] = [(None, None)] * 10
        sent.clear()
        with mock.patch.object(client, "_post", post), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(client._poll_loop(1, "t", {"argv": ["wait"]}, sleep=lambda s: None,
                                                clock=lambda: next(clock)))
        self.assertIn("stopped answering during this wait", err.getvalue())
        self.assertTrue(client._pollable("wait", ["wait"]) and client._pollable("inbox", ["inbox", "--wait"]))
        self.assertFalse(client._pollable("inbox", ["inbox"]))

    def test_serve_writes_a_wait_polls_again_through_a_proxy_502(self):
        # #6030: while serve restarts, the sandbox's HTTP proxy answers 502 (or 504); serve never does. The wait
        # polls again, as when serve stops answering, and does not end with "took the command but gave no answer".
        answers = [(200, b'{"code": 75, "out": "", "err": "", "poll": {"started": "t0"}}'),
                   (502, b"<html>Bad Gateway</html>"), (504, b""),
                   (200, b'{"code": 0, "out": "WORK: #3 is ready.\\n", "err": ""}')]
        sent, naps = [], []

        def post(port, body, token, timeout):
            sent.append(1)
            return answers[len(sent) - 1]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(client, "_post", post), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = client._poll_loop(1, "t", {"argv": ["wait"]}, sleep=naps.append)
        self.assertEqual((code, out.getvalue(), err.getvalue()), (0, "WORK: #3 is ready.\n", ""))
        self.assertEqual(len(sent), 4)

    def test_serve_writes_a_wait_reloads_serve_token_and_stamp_after_restart(self):
        # #6475: a restarted serve writes a new RUN_TOKEN to serve.json and may have newer code. _poll_loop re-reads
        # serve.json and refreshes stamp so the wait goes back to serve instead of getting 403/409 for POLL_LOST.
        sfile = self.db + "-serve.json"
        with open(sfile, "w", encoding="utf-8") as f:
            json.dump({"port": 1, "token": "t1"}, f)
        stamp = [100]
        seen = []

        def post(port, body, token, timeout):
            req = json.loads(body)
            seen.append((token, req.get("stamp")))
            if len(seen) == 1:
                return 200, b'{"code": 75, "out": "", "err": "", "poll": {"started": "t0"}}'
            if len(seen) == 2:
                # serve restarted with a new token and newer code
                with open(sfile, "w", encoding="utf-8") as f:
                    json.dump({"port": 1, "token": "t2"}, f)
                stamp[0] = 200
                return 403, b'{"error": "a maxpm command with the token of the serve file only"}'
            if token == "t2" and req.get("stamp") == 200:
                return 200, b'{"code": 0, "out": "WORK: #9 is ready.\\n", "err": ""}'
            return 409, b'{"error": "stale"}'
        out, err, naps = io.StringIO(), io.StringIO(), []
        with mock.patch.object(client, "_post", post), mock.patch.object(client, "code_stamp", lambda: stamp[0]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = client._poll_loop(1, "t1", {"argv": ["wait"], "stamp": 100}, sleep=naps.append,
                                     serve_path=sfile, can_local=False)
        self.assertEqual((code, out.getvalue(), err.getvalue()), (0, "WORK: #9 is ready.\n", ""))
        self.assertEqual(seen, [("t1", 100), ("t1", 100), ("t2", 200)])
        self.assertEqual(naps, [client.POLL_SLEEP])  # 403 with rotated token retried immediately

    def test_serve_writes_a_wait_that_cannot_write_locally_keeps_polling_past_poll_lost(self):
        # #6475: when this process cannot write the queue locally, falling back after POLL_LOST always fails with
        # 'only maxpm serve writes the queue'. Keep polling until serve answers again.
        answers = [(200, b'{"code": 75, "out": "", "err": "", "poll": {"started": "t0"}}'),
                   (None, None), (None, None), (None, None),
                   (200, b'{"code": 0, "out": "WORK: #5 is ready.\\n", "err": ""}')]
        calls = [0]
        clock = iter(range(0, 1000, 90))

        def post(port, body, token, timeout):
            calls[0] += 1
            return answers[calls[0] - 1]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(client, "_post", post), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = client._poll_loop(1, "t", {"argv": ["wait"]}, sleep=lambda s: None,
                                     clock=lambda: next(clock), can_local=False)
        self.assertEqual((code, out.getvalue(), err.getvalue()), (0, "WORK: #5 is ready.\n", ""))
        self.assertEqual(calls[0], 5)

    def test_serve_writes_a_write_during_a_restart_is_retried_not_a_502(self):
        # #6475: a non-poll command (e.g. send note) that hits 502/504 or 503 restarting retries across the restart,
        # re-reading the new serve token, instead of failing with 'gave no answer (502)'.
        sfile = self.db + "-serve.json"
        with open(sfile, "w", encoding="utf-8") as f:
            json.dump({"port": 1, "token": "t1"}, f)
        env = {"MAXPM_DB": self.db, "PATH": os.environ.get("PATH", "")}
        tokens = []

        def post(port, body, token, timeout):
            tokens.append(token)
            if len(tokens) == 1:
                return 502, b"<html>Bad Gateway</html>"
            if len(tokens) == 2:
                return 503, b'{"error": "restarting"}'
            if len(tokens) == 3:
                with open(sfile, "w", encoding="utf-8") as f:
                    json.dump({"port": 1, "token": "t2"}, f)
                return 403, b'{"error": "token"}'
            return 200, b'{"code": 0, "out": "sent\\n"}'
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(client, "_post", post), mock.patch.object(client, "RETRY_SLEEP", 0), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = client.run_remote(["send", "note", "hi", "--to", "m"], env)
        self.assertEqual((code, out.getvalue(), err.getvalue()), (0, "sent\n", ""))
        self.assertEqual(tokens, ["t1", "t1", "t1", "t2"])

    @unittest.skipIf(os.geteuid() == 0, "root ignores read-only file modes")
    def test_serve_writes_a_write_on_a_readonly_db_retries_connection_refused(self):
        # #6475: inside a sandbox (read-only queue), a brief connection refused during serve exec retries instead of
        # falling back locally and failing with 'only maxpm serve writes the queue'.
        sfile = self.db + "-serve.json"
        with open(sfile, "w", encoding="utf-8") as f:
            json.dump({"port": 1, "token": "t1"}, f)
        os.chmod(self.db, stat.S_IRUSR)
        self.addCleanup(os.chmod, self.db, stat.S_IRUSR | stat.S_IWUSR)
        env = {"MAXPM_DB": self.db, "PATH": os.environ.get("PATH", "")}
        answers = [(None, None), (200, b'{"code": 0, "out": "ok\\n"}')]
        calls = [0]

        def post(port, body, token, timeout):
            calls[0] += 1
            return answers[calls[0] - 1]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(client, "_post", post), mock.patch.object(client, "RETRY_SLEEP", 0), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = client.run_remote(["send", "note", "hi"], env)
        self.assertEqual((code, out.getvalue(), err.getvalue()), (0, "ok\n", ""))
        self.assertEqual(calls[0], 2)

    def test_serve_writes_a_runner_answers_a_poll_with_its_state(self):
        proc = subprocess.Popen([sys.executable, "-m", "river.runner", self.db], cwd=ROOT, text=True,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.addCleanup(lambda: (proc.stdin.close(), proc.wait(timeout=10), proc.stdout.close()))
        req = {"argv": ["--as", "w", "wait"], "cwd": self.dir.name, "stamp": client.code_stamp(),
               "ppid": os.getppid(), "env": {"MAXPM_DB": self.db, "PATH": os.environ.get("PATH", "")},
               "poll": {"id": "abcd1234", "state": {}}}
        proc.stdin.write(json.dumps(req) + "\n")
        proc.stdin.flush()
        ans = json.loads(proc.stdout.readline())
        self.assertEqual((ans["code"], ans["out"]), (core.POLL_AGAIN, ""), ans)
        self.assertIn("started", ans["poll"])
        n = self.c.execute("SELECT count(*) FROM runs WHERE cmd='wait'").fetchone()[0]
        self.assertEqual(n, 0)  # a poll that found nothing writes no row of the run log


if __name__ == "__main__":
    unittest.main()
