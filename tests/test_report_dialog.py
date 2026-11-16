"""The review report of a release on the page (river/static/components/reportDialog.js, #6003), run in node."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "river" / "static"

REPORT = {
    "release": {"id": 5, "title": "Deploy web", "target": "web", "status": "open", "cut_at": "2026-11-16T00:00:00Z",
                "cut_rev": "4f2a9c1", "closed_at": None, "review": {"id": 6, "status": "open", "assignee": None, "output": None}},
    "summary": {"text": "One revert <b>here</b>.", "by": "boss", "at": "2026-11-16T00:00:00Z"},
    "numbers": {"tasks": 2, "done": 2, "dropped": 0, "open": 0, "faults": 0, "faults_open": 0, "fixes": 0, "decisions": 1,
                "usd": 1.5},
    "groups": [{"key": "risks", "name": "Risks", "risk": True, "items": [4], "line": "#4 a revert"},
               {"key": "goal:checkout", "name": "checkout", "risk": False, "items": [1], "line": "People can pay"}],
    "tasks": {"1": {"id": 1, "title": "Pay page", "short": None, "status": "done", "kind": "work", "project": "site",
                    "goals": ["checkout"], "changed": "You can pay by card", "output": "abc1234: done", "check": "",
                    "commits": ["abc1234"], "done_by": "dev", "closed_at": "2026-11-16T00:00:00Z", "usd": 1.5,
                    "decisions": [{"id": 9, "kind": "question", "from": "dev", "to": "mark", "body": "Card first?",
                                   "at": "2026-11-16T00:00:00Z", "answer": {"from": "mark", "body": "Yes", "at": "2026-11-16T00:00:00Z"}}],
                    "risk": None},
              "4": {"id": 4, "title": "Revert the menu", "short": None, "status": "done", "kind": "work", "project": "site",
                    "goals": [], "changed": "The old menu is back", "output": "", "check": "", "commits": [],
                    "done_by": "dev", "closed_at": None, "usd": None, "decisions": [], "risk": "a revert"}},
    "faults": []}


def run(expr):
    js = (f"const m = await import('./components/reportDialog.js'); const R = {json.dumps(REPORT)};"
          f" console.log(JSON.stringify({expr}));")
    out = subprocess.run(["node", "--input-type=module", "-e", js], cwd=STATIC, capture_output=True, text=True,
                         timeout=30, check=True)
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which("node"), "node is not on this machine")
class ReportDialog(unittest.TestCase):
    def test_the_left_side_has_the_scope_summary_numbers_and_groups_risks_first(self):
        html = run("m.leftHtml(R)")
        self.assertIn("#5</b> of web", html)
        self.assertIn("review <span class=\"link\" data-open=\"6\">#6</span>", html)
        self.assertIn("One revert &lt;b&gt;here&lt;/b&gt;.", html)  # escaped
        self.assertIn("decided by you", html)
        self.assertLess(html.index("Risks"), html.index("checkout"))
        self.assertIn('class="rep-g risk"', html)
        self.assertIn('data-rep-task="4"', html)

    def test_a_task_has_its_line_proof_and_decisions(self):
        html = run("m.taskHtml(R.tasks['1'])")
        self.assertIn("You can pay by card", html)
        self.assertIn("<code>abc1234</code>", html)
        self.assertIn("Card first?", html)
        self.assertIn("<b>mark:</b> Yes", html)
        self.assertIn('data-open="1"', html)
        self.assertIn("Pick a task", run("m.taskHtml(null)"))


if __name__ == "__main__":
    unittest.main()
