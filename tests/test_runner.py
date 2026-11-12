"""maxpm commands that run in a runner of maxpm serve (#5501): river/client.py, river/runner.py, POST /api/run."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from river import client, core, runner, server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Client(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.db = os.path.join(self.dir.name, "q.db")
        self.env = {"MAXPM_DB": self.db, "PATH": os.environ.get("PATH", "")}

    def serve_file(self):
        with open(self.db + "-serve.json", "w") as f:
            json.dump({"port": 1, "token": "t"}, f)

    def test_commands_that_stay_in_their_own_process(self):
        self.serve_file()
        with mock.patch.object(client, "_post", side_effect=AssertionError("asked serve")):
            for words in (["serve"], ["wait"], ["review", "pass", "3"], ["canvas", "put", "f.html"],
                          ["goal", "handoff", "g", "--file", "notes.md"], ["add", "--from", "plan.md"],
                          ["inbox", "--wait"], ["manage", "--watch"], ["add", "x", "-"], ["artifacts", "show", "2"],
                          ["--version"], [], ["-h"]):
                self.assertIsNone(client.run_remote(words, self.env), words)
            self.assertIsNone(client.run_remote(["show", "3"], {**self.env, "MAXPM_LOCAL": "1"}))

    def test_no_serve_file_runs_here(self):
        with mock.patch.object(client, "_post", side_effect=AssertionError("asked serve")):
            self.assertIsNone(client.run_remote(["show", "3"], self.env))

    def test_an_answer_of_serve(self):
        self.serve_file()
        sent = {}

        def post(port, body, token, timeout):
            sent.update(json.loads(body), token=token)
            return 200, json.dumps({"code": 2, "out": "one\n", "err": "two\n"}).encode()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(client, "_post", post), mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", err):
            self.assertEqual(client.run_remote(["--as", "ag", "show", "3"], self.env, cwd="/somewhere"), 2)
        self.assertEqual((out.getvalue(), err.getvalue()), ("one\n", "two\n"))
        self.assertEqual((sent["argv"], sent["cwd"], sent["env"]["MAXPM_DB"], sent["token"]),
                         (["--as", "ag", "show", "3"], "/somewhere", self.db, "t"))
        self.assertEqual(sent["stamp"], client.code_stamp())

    def test_refused_runs_here_and_a_lost_answer_does_not(self):
        self.serve_file()
        for status in (None, 403, 404, 409, 503):
            with mock.patch.object(client, "_post", return_value=(status, b"{}")):
                self.assertIsNone(client.run_remote(["done", "3"], self.env), status)
        err = io.StringIO()
        with mock.patch.object(client, "_post", return_value=(599, b"")), mock.patch.object(sys, "stderr", err):
            self.assertEqual(client.run_remote(["done", "3"], self.env), 1)  # serve may have run it: not again
        self.assertIn("took the command", err.getvalue())


class LeanStart(unittest.TestCase):
    """The command's own start (#5525): the client uses the C parts of json and socket, and bin/maxpm runs Python
    with -S."""

    def test_the_json_of_the_c_parts_is_the_json_of_the_module(self):
        self.assertEqual(client._socket.__name__, "_socket")
        obj = {"argv": ["--as", "ag", "say", "caf\u00e9 \u2713"], "env": {"A": "1"}, "ppid": 7, "tty": False,
               "none": None, "n": [1.5, -2]}
        self.assertEqual(client._dumps(obj), json.dumps(obj))
        self.assertEqual(client._loads(json.dumps(obj)), obj)
        self.assertEqual(client._loads(b' {"out": "a\\nb", "code": 0} \n'), {"out": "a\nb", "code": 0})
        for bad in ("", "{} x", "NaN", "{"):
            with self.assertRaises(ValueError, msg=bad):
                client._loads(bad)
        with self.assertRaises(TypeError):
            client._dumps({"x": object()})

    def test_post_talks_http_over_a_local_socket(self):
        import socket
        import threading
        srv = socket.socket()
        try:
            srv.bind(("127.0.0.1", 0))
        except OSError:
            self.skipTest("this sandbox binds no port")
        srv.listen(1)
        self.addCleanup(srv.close)
        got = {}

        def answer():
            c, _ = srv.accept()
            with c:
                data = b""
                while b"\r\n\r\n" not in data or not data.endswith(b"}"):
                    data += c.recv(65536)
                got["req"] = data
                body = b'{"code": 3, "out": "x"}'
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
        t = threading.Thread(target=answer)
        t.start()
        status, rest = client._post(srv.getsockname()[1], b'{"argv": []}', "tok", 5)
        t.join(5)
        self.assertEqual((status, client._loads(rest)), (200, {"code": 3, "out": "x"}))
        self.assertIn(b"X-Maxpm-Token: tok", got["req"])
        self.assertEqual(client._post(1, b"{}", "t", 1), (None, None))  # nothing listens on port 1

    def test_main_runs_the_command_here_when_serve_does_not(self):
        ran = []
        with mock.patch.object(client, "run_remote", return_value=None), \
                mock.patch("river.cli.main", lambda: ran.append("local")):
            client.main()
        self.assertEqual(ran, ["local"])
        with mock.patch.object(client, "run_remote", return_value=4), self.assertRaises(SystemExit) as e:
            client.main()
        self.assertEqual(e.exception.code, 4)

    def test_bin_maxpm_starts_python_without_site(self):
        path = os.path.join(ROOT, "bin", "maxpm")
        with open(path) as f:
            self.assertEqual(f.readline().strip(), "#!/usr/bin/env -S python3 -S")
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        env = {**os.environ, "MAXPM_LOCAL": "1", "MAXPM_DB": os.path.join(d.name, "q.db")}
        r = subprocess.run([sys.executable, "-S", path, "--version"], capture_output=True, text=True, env=env,
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("maxpm "), r.stdout)


class Runner(unittest.TestCase):
    """A runner process over its pipes, as serve uses it."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.db = os.path.join(self.dir.name, "q.db")
        c = core.connect(self.db)
        core.project_add(c, "web")
        self.item = core.item_add(c, "web", "Build the cart")["id"]
        core.register(c, "ag")
        c.close()
        self.proc = subprocess.Popen([sys.executable, "-m", "river.runner", self.db], cwd=ROOT, text=True,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.addCleanup(self.stop)

    def stop(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)
        self.proc.stdout.close()

    def ask(self, argv, **more):
        env = {"MAXPM_DB": self.db, "PATH": os.environ.get("PATH", ""), "MAXPM_AGENT": "ag", **more.pop("env", {})}
        req = {"argv": argv, "cwd": self.dir.name, "env": env, "ppid": os.getppid(), "stamp": client.code_stamp(),
               **more}
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def test_commands_with_the_callers_words_folder_and_environment(self):
        a = self.ask(["-q", "show", str(self.item)])
        self.assertEqual(a["code"], 0)
        self.assertIn("Build the cart", a["out"])
        a = self.ask(["-q", "claim", str(self.item)])
        self.assertEqual(a["code"], 0, a)
        a = self.ask(["-q", "show", str(self.item)])  # the next command sees the write of the last
        self.assertIn("in_progress by ag", a["out"])
        a = self.ask(["-q", "show", "999"])
        self.assertEqual(a["code"], 2)
        self.assertIn("maxpm: ", a["err"])
        a = self.ask(["-q", "db", "path"], env={"MAXPM_AGENT": "other"})
        self.assertEqual(a["out"].strip(), str(core.Path(self.db)))
        a = self.ask(["shw"])  # a mistake of the words: the parser's own message, status 2
        self.assertEqual(a["code"], 2)
        self.assertIn("invalid choice", a["err"])

    def test_another_queue_or_older_code_is_refused(self):
        self.assertEqual(self.ask(["-q", "show", "1"], stamp=1), {"refused": "stale"})
        other = os.path.join(self.dir.name, "other.db")
        self.assertEqual(self.ask(["-q", "show", "1"], env={"MAXPM_DB": other}), {"refused": "another queue"})


class ServeRoute(unittest.TestCase):
    def handler(self, body, token=None):
        h, out = server.Handler.__new__(server.Handler), {}
        raw = json.dumps(body).encode()
        h.path = "/api/run"
        h.client_address = ("127.0.0.1", 5)
        h.headers = {"Host": "127.0.0.1:8765", "Content-Length": str(len(raw)),
                     "X-Maxpm-Token": server.RUN_TOKEN if token is None else token}
        h.rfile = io.BytesIO(raw)
        h._send = lambda code, b, ctype=None, headers=None: out.update(code=code, body=b)
        h._refuse = lambda: False
        return h, out

    def test_the_token_the_busy_runners_and_an_answer(self):
        h, out = self.handler({"argv": ["show", "1"]}, token="wrong")
        h.do_POST()
        self.assertEqual(out["code"], 403)
        for ans, code in (({"busy": True}, 503), ({"refused": "stale"}, 409), ({"error": "stopped"}, 500),
                          ({"code": 0, "out": "x", "err": ""}, 200)):
            h, out = self.handler({"argv": ["show", "1"]})
            with mock.patch.object(server, "run_command", return_value=ans):
                h.do_POST()
            self.assertEqual(out["code"], code, ans)
        self.assertEqual(out["body"]["out"], "x")

    def test_no_runner_is_free(self):
        r = server._Runner("x")
        r.lock.acquire()
        with mock.patch.object(server, "RUNNERS", [r]):
            self.assertEqual(server.run_command({"argv": []}), {"busy": True})
        r.lock.release()


if __name__ == "__main__":
    unittest.main()
