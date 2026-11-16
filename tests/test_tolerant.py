"""#5970: maxpm takes the positional and the named forms of its arguments, and names the nearest valid word."""

import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from river import cli, core


class Tolerant(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "t.db")
        self.c = core.connect(self.path)
        self.addCleanup(self.c.close)
        core.project_add(self.c, "shop")
        core.register(self.c, "w")

    def run_cli(self, *words):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.dict(os.environ, {"MAXPM_DB": self.path, "MAXPM_QUIET": "1"}):
            try:
                code = cli.run(list(words))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_tolerant_words_move_global_flags_and_read_a_priority(self):
        t = cli.tolerant_words
        self.assertEqual(t(["list", "-q", "--json"]), ["-q", "--json", "list"])
        self.assertEqual(t(["show", "12", "--as", "w"]), ["--as", "w", "show", "12"])
        self.assertEqual(t(["--as", "w", "add", "x", "-p", "P1"]), ["--as", "w", "add", "x", "-p", "1"])
        self.assertEqual(t(["add", "x", "--priority=p0"]), ["add", "x", "--priority=0"])
        self.assertEqual(t(["prio", "12", "P3"]), ["prio", "12", "3"])
        self.assertEqual(t(["add", "P1 report"]), ["add", "P1 report"])  # a title stays a title
        self.assertEqual(t(["help"]), ["-h"])
        self.assertEqual(t(["help", "add"]), ["add", "-h"])
        self.assertEqual(t(["db"]), ["db", "path"])
        self.assertEqual(t(["config"]), ["config", "get"])
        self.assertEqual(t(["config", "--project", "shop"]), ["config", "get", "--project", "shop"])
        self.assertEqual(t(["config", "set", "k", "v"]), ["config", "set", "k", "v"])
        self.assertEqual(t(["secret", "run", "x", "--", "gh", "--json"]), ["secret", "run", "x", "--", "gh", "--json"])

    def test_tolerant_commands_take_both_forms(self):
        code, out, _ = self.run_cli("add", "--project", "shop", "Fix the header", "-p", "P1", "--as", "w")
        self.assertEqual(code, 0, out)
        row = self.c.execute("SELECT id, priority FROM items WHERE title='Fix the header'").fetchone()
        self.assertEqual(row["priority"], 1)
        self.run_cli("--as", "w", "add", "shop", "--title", "Fix the footer")
        self.assertIsNotNone(self.c.execute("SELECT 1 FROM items WHERE title='Fix the footer'").fetchone())
        code, out, _ = self.run_cli("list", "-n", "1", "--json")
        self.assertEqual((code, out.count('"title"')), (0, 1))
        self.assertEqual(self.run_cli("prio", str(row["id"]), "P0")[0], 0)
        self.assertEqual(self.c.execute("SELECT priority FROM items WHERE id=?", (row["id"],)).fetchone()[0], 0)
        code, out, _ = self.run_cli("db")
        self.assertEqual((code, out.strip()), (0, self.path))
        self.assertEqual(self.run_cli("config")[0], 0)
        core.register(self.c, "gone-soon")
        self.assertEqual(self.run_cli("--as", "gone-soon", "unregister")[0], 0)
        self.assertIsNone(self.c.execute("SELECT 1 FROM agents WHERE name='gone-soon'").fetchone())

    def test_tolerant_errors_name_the_nearest_word(self):
        code, _, err = self.run_cli("lsit")
        self.assertEqual(code, 2)
        self.assertIn("Did you mean: list", err)
        code, _, err = self.run_cli("add", "x", "--prioirty", "1")
        self.assertEqual(code, 2)
        self.assertIn("Did you mean: --priority", err)


if __name__ == "__main__":
    unittest.main()
