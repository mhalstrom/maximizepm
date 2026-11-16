"""The Sessions line of the session list and the state dot of a row (river/static/views/sessionsLine.js), run in node
(#5999, #6021)."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "river" / "static"


def run(expr):
    js = f"const m = await import('./views/sessionsLine.js'); console.log(JSON.stringify({expr}));"
    out = subprocess.run(["node", "--input-type=module", "-e", js], cwd=STATIC, capture_output=True, text=True,
                         timeout=30, check=True)
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which("node"), "node is not on this machine")
class SessionsLine(unittest.TestCase):
    def test_before_the_first_read_the_line_does_not_say_that_no_session_runs(self):
        # A page that loaded hidden reads the sessions only when it shows; until then its list is empty, and the line
        # said "No session runs" while five sessions ran (#5999).
        row = run("m.sessionsLine([], 1, false)")
        self.assertIn("Reading the sessions", row)
        self.assertNotIn("No session runs", row)
        self.assertIn("1 browser", row)

    def test_after_a_read_an_empty_list_says_that_no_session_runs(self):
        self.assertIn("No session runs", run("m.sessionsLine([], 0, true)"))

    def test_the_counts_are_the_dots_of_the_rows(self):
        # #6021: the line counted states that no row showed ('2 done'), and called idle a manager whose row said
        # '1 running'. Now each count is a dot of the rows: needs you, working (a turn, or its background work), waiting.
        rows = ("[{state: 'working'}, {state: 'idle', state_label: 'running: Run the full suite'},"
                " {state: 'idle', state_label: 'idle: at its prompt', shells: [{label: 'maxpm manage --watch'}]},"
                " {state: 'idle', state_label: 'idle: waits for work'}, {state: 'prompt'},"
                " {state: 'working', waiting: [{kind: 'question'}]}, {state: 'expert'}]")
        self.assertEqual(run(f"{rows}.map(m.dotOf)"), ["active", "active", "away", "away", "needs", "needs", "active"])
        self.assertEqual(run(f"m.countLine({rows})"), "2 need you · 3 working · 2 waiting")
        self.assertEqual(run("m.countLine([{state: 'prompt'}])"), "1 needs you")

    def test_the_line_is_a_heading_not_a_button(self):
        row = run("m.sessionsLine([{state: 'working'}], 0, true)")
        self.assertIn("1 working", row)
        self.assertNotIn("role=\"button\"", row)
        self.assertNotIn("data-status", row)


if __name__ == "__main__":
    unittest.main()
