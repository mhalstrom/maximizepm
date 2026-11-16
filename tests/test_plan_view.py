"""The plan versions of a design document on the page (river/static/components/planView.js, #6014), run in node."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "river" / "static"
T = lambda i, part, status="open": {"id": i, "title": f"task {i}", "status": status, "part": part}
DATA = {"canvas": "phone", "id": 1, "title": "Phone", "plans": [
    {"plan_version": 1, "version": 2, "author": "ex", "note": "first", "added": [1, 2], "removed": [], "changed": [],
     "tasks": [T(1, "4.1 Shell"), T(2, "4.2 Pairing")]},
    {"plan_version": 2, "version": 5, "author": "ex", "note": "the prototype first", "added": [3], "removed": [2],
     "changed": [1], "tasks": [T(3, "4.1 Prototype"), T(1, "4.2 Shell", "done")]}]}


def run(expr):
    js = (f"const m = await import('./components/planView.js'); const D = {json.dumps(DATA)};"
          " const esc = (s) => String(s).replace(/[&<>\"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'})[c]);"
          f" console.log(JSON.stringify({expr}));")
    out = subprocess.run(["node", "--input-type=module", "-e", js], cwd=STATIC, capture_output=True, text=True,
                         timeout=30, check=True)
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which("node"), "node is not on this machine")
class PlanView(unittest.TestCase):
    def test_compare_shows_added_removed_and_moved_tasks_with_why(self):
        html = run("m.planHtml(D, 1, 2, esc)")
        self.assertIn('class="pl-t added"', html)
        self.assertIn('class="pl-t removed"', html)
        self.assertIn('class="pl-t changed"', html)
        self.assertIn("why: the prototype first", html)
        self.assertIn('data-open="2"', html)  # a removed task still opens
        self.assertIn("done · 4.2 Shell", html)
        self.assertIn('<option value="1" selected>v1</option>', html)

    def test_one_version_and_no_plan(self):
        one = run("m.planHtml({plans: [D.plans[0]]}, 1, 1, esc)")
        self.assertNotIn("Compare", one)
        self.assertIn("why: first", one)
        self.assertIn("No plan yet", run("m.planHtml({plans: []}, 0, 0, esc)"))


if __name__ == "__main__":
    unittest.main()
