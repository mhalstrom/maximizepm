"""The log check plugin (#5463): the new lines of a log since its last run, against a pattern and limits."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from river import cli, core
from river.core import RiverError
from river.plugins import logs


class Logs(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        env = mock.patch.dict(os.environ, {"MAXPM_DB": os.path.join(self.dir.name, "t.db")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MAXPM_CHECK_KEY", None)
        self.log = os.path.join(self.dir.name, "app.log")
        self.write("old ERROR line\nok\n", "w")

    def write(self, text, mode="a"):
        with open(self.log, mode, encoding="utf-8") as f:
            f.write(text)

    def test_a_file_counts_only_the_lines_since_the_last_run_and_starts_again_after_a_rotation(self):
        res = logs.run("t", file=self.log)
        self.assertEqual(res["status"], 0)
        self.assertIn("the first read found the end of the log", res["summary"])
        self.write("INFO fine\nERROR db down\nTraceback (most recent call last)\nERROR noisy cache\n")
        res = logs.run("t", file=self.log, ignore="noisy", warning=1, critical=3)
        self.assertEqual((res["status"], res["count"], res["lines"]), (1, 2, 4))
        self.assertEqual(res["matches"], ["ERROR db down", "Traceback (most recent call last)"])
        out = logs.lines(res)
        self.assertEqual(out[0], f"LOGS WARNING - {self.log}: 2 of 4 new lines match; the limit is 1 | matches=2;1;3 lines=4")
        self.assertEqual(json.loads(out[1])["count"], 2)
        self.assertEqual(logs.run("t", file=self.log)["count"], 0)  # nothing new
        self.write("x\nERROR after rotation\n", "w")  # shorter: rotated
        res = logs.run("t", file=self.log)
        self.assertEqual((res["status"], res["matches"]), (2, ["ERROR after rotation"]))  # no limit: one line

    def test_the_noise_patterns_that_maxpm_serve_gives_are_not_counted(self):
        logs.run("t", file=self.log)
        self.write("ERROR BrokenPipeError: [Errno 32]\nERROR db down\nERROR ConnectionResetError\n")
        with mock.patch.dict(os.environ, {"MAXPM_NOISE": "BrokenPipeError\nConnectionResetError\n(bad"}):
            res = logs.run("t", file=self.log)
        self.assertEqual((res["count"], res["matches"]), (1, ["ERROR db down"]))
        self.assertIn("; 2 more are noise (maxpm ops noise list)", res["summary"])

    def test_from_start_reads_the_old_lines_and_a_large_read_skips_the_oldest_bytes(self):
        res = logs.run("t", file=self.log, from_start=True)
        self.assertEqual((res["status"], res["count"]), (2, 1))
        self.write("ERROR " + "y" * 200 + "\nERROR last\n")
        res = logs.run("t", file=self.log, max_read=20)
        self.assertEqual(res["matches"], ["ERROR last"])
        self.assertGreater(res["skipped_bytes"], 0)
        self.assertIn("older bytes skipped", res["summary"])

    def test_a_command_counts_the_lines_after_the_last_runs_last_lines_and_gets_since(self):
        seen = []
        out = {"text": "a\nb\nERROR old\n"}

        def runner(cmd, timeout):
            seen.append(cmd)
            return 0, out["text"].encode(), ""
        with mock.patch.object(logs, "RUNNER", runner):
            self.assertEqual(logs.run("c", command="docker logs app --since {since}")["status"], 0)
            out["text"] = "b\nERROR old\nc\nERROR new\n"  # the tail moved: only c and ERROR new are new
            res = logs.run("c", command="docker logs app --since {since}")
            self.assertEqual((res["count"], res["lines"], res["matches"]), (1, 2, ["ERROR new"]))
            out["text"] = "unrelated\n"  # none of the last lines: all new
            self.assertEqual(logs.run("c", command="journalctl --since @{since_epoch}")["lines"], 1)
        self.assertRegex(seen[1], r"^docker logs app --since \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertRegex(seen[2], r"^journalctl --since @\d+$")

    def test_a_remote_file_reads_through_ssh_in_batch_mode(self):
        calls = []
        answers = [(0, b"120\n", ""), (0, b"136\n120\nERROR remote\nok\n", ""), (255, b"", "Permission denied")]

        def runner(cmd, timeout):
            calls.append(cmd)
            return answers.pop(0)
        with mock.patch.object(logs, "RUNNER", runner):
            self.assertEqual(logs.run("r", file="/var/log/app.log", ssh="web1")["status"], 0)
            res = logs.run("r", file="/var/log/app.log", ssh="web1")
            self.assertEqual((res["status"], res["matches"]), (2, ["ERROR remote"]))
            res = logs.run("r", file="/var/log/app.log", ssh="web1")
            self.assertEqual(res["status"], 3)
            self.assertIn("Permission denied", res["summary"])
        self.assertEqual(calls[0][:5], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"])
        self.assertIn("o=120", calls[1][-1])
        self.assertEqual(json.loads((core.db_path().parent / "logs" / "r.json").read_text(encoding="utf-8"))["offset"], 136)

    def test_the_rate_per_minute_and_the_check_key_as_the_name(self):
        os.environ["MAXPM_CHECK_KEY"] = "ops:shop:errors"
        logs.run(file=self.log)
        later = core.now() + core.timedelta(minutes=2)
        self.write("ERROR 1\nERROR 2\nERROR 3\nERROR 4\n")
        with mock.patch.object(core, "now", lambda: later):
            res = logs.run(file=self.log, warning=1, critical=3, per_minute=True)
        self.assertEqual(res["status"], 1)  # 4 lines in 2 minutes: 2 a minute
        self.assertIn("(2.0 a minute)", res["summary"])
        self.assertTrue((core.db_path().parent / "logs" / "ops_shop_errors.json").exists())

    def test_bad_settings_are_refused_and_an_unreadable_log_is_unknown(self):
        for kw, text in (({}, "give one source"), ({"file": "a", "command": "b"}, "give one source"),
                         ({"command": "x", "ssh": "h"}, "--ssh reads a file"),
                         ({"file": "a", "pattern": "("}, "bad --pattern"), ({"file": "a", "warning": 0}, "more than 0")):
            with self.assertRaisesRegex(RiverError, text):
                logs.run("t", **kw)
        with self.assertRaisesRegex(RiverError, "name the log check"):
            logs.run(file=self.log)
        res = logs.run("t", file=os.path.join(self.dir.name, "nope.log"))
        self.assertEqual(res["status"], 3)

    def test_the_command_prints_the_plugin_lines_and_exits_with_the_status(self):
        def cli_run(*words):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.run(list(words))
            return code, out.getvalue()
        self.assertEqual(cli_run("ops", "logs", "--name", "t", "--file", self.log)[0], 0)
        self.write("FATAL boom\n")
        code, out = cli_run("ops", "logs", "--name", "t", "--file", self.log, "--critical", "1")
        self.assertEqual(code, 2)
        self.assertTrue(out.startswith("LOGS CRITICAL"))
        self.assertIn('"FATAL boom"', out)


if __name__ == "__main__":
    unittest.main()
