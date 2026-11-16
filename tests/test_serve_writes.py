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
        # Stays here: a file it cannot read, a page with a local picture, a canvas it writes to a file, a wait.
        pic = os.path.join(self.dir.name, "pic.md")
        with open(pic, "w", encoding="utf-8") as f:
            f.write("![chart](chart.png) and ![web](https://x.test/a.png)\n")
        with mock.patch.object(client, "_post", side_effect=AssertionError("asked serve")):
            for words in (["canvas", "put", "nofile.html", "--name", "n"], ["canvas", "save", "plan", "p.md"],
                          ["canvas", "put", pic, "--name", "n"],
                          ["inbox", "--wait"], ["add", "x", "-"]):
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


if __name__ == "__main__":
    unittest.main()
