"""The Sessions row of the session list (river/static/views/status.js), run in node (#5999)."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "river" / "static"


def run(expr):
    js = f"const m = await import('./views/status.js'); console.log(JSON.stringify({expr}));"
    out = subprocess.run(["node", "--input-type=module", "-e", js], cwd=STATIC, capture_output=True, text=True,
                         timeout=30, check=True)
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which("node"), "node is not on this machine")
class StatusRow(unittest.TestCase):
    def test_before_the_first_read_the_row_does_not_say_that_no_session_runs(self):
        # A page that loaded hidden reads the sessions only when it shows; until then its list is empty, and the row
        # said "No session runs" while five sessions ran (#5999).
        row = run("m.statusRowHtml([], false, 1, false)")
        self.assertIn("Reading the sessions", row)
        self.assertNotIn("No session runs", row)
        self.assertIn("1 browser", row)
        self.assertIn("Reading the sessions", run("m.statusHtml([], 'off', false)"))

    def test_after_a_read_an_empty_list_says_that_no_session_runs(self):
        self.assertIn("No session runs", run("m.statusRowHtml([], false, 0, true)"))
        self.assertIn("No session runs now", run("m.statusHtml([], 'off')"))
        row = run("m.statusRowHtml([{name: 'w1', status: 'working'}], false, 0, true)")
        self.assertIn("1 working", row)


if __name__ == "__main__":
    unittest.main()
