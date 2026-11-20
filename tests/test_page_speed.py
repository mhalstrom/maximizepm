"""The page-speed budget during development (#6505; policy p147: every page action answers in under 100 ms).

mark 2026-11-20: 'the page-speed stuff needs to be run locally during dev so we don't make our system slow', and
'the speed tests should not be a blocker they are to ensure we don't make speed regressions while developing'.
So the budget is this test: it builds the page's state (GET /api/state, the main view and the done view) on a queue
of the size of a real one (tests/speed_queue.py) and fails when the p90 of the builds passes the budget, with the
slowest part of the build named. It runs in the local suite before each commit. GitHub's runners are many times
slower and shared, so the test skips there (CI set): a slow runner says nothing about this code. A watch check of a
release never fails, holds or rolls back a release (#6505).
"""
import math
import os
import shutil
import tempfile
import unittest

from river import core, server
from tests import speed_queue

BUILDS = 20  # each view, after one build that reads the whole item graph (live, that comes once per serve start)
DONE_BUILDS = 8


def _p90(xs):
    return sorted(xs)[max(0, math.ceil(0.9 * len(xs)) - 1)]


def _report(view, runs, budget):
    """The failure text: the p90, and the part of the build that took the most time (median over the builds)."""
    builds = [r["build_ms"] for r in runs]
    parts = {}
    for r in runs:
        for k, v in (r.get("parts") or {}).items():
            parts.setdefault(k, []).append(v)
    med = {k: sorted(v)[len(v) // 2] for k, v in parts.items()}
    slow = max(med, key=med.get) if med else "?"
    worst = max(runs, key=lambda r: r["build_ms"])
    return (f"the {view} page build is slow: p90 {_p90(builds)} ms over {len(builds)} builds, budget {budget} ms "
            f"(policy p147). The slowest part is '{slow}' (median {med.get(slow)} ms; the parts, median ms: {med}). "
            f"The slowest build: {worst['build_ms']} ms, its parts {worst.get('parts')}, CPU {worst.get('cpu_ms')} ms. "
            f"Profile it: python3 -m cProfile -s cumulative on server.page_state against tests/speed_queue.py")


@unittest.skipIf(os.environ.get("CI"), "the page-speed budget runs locally during development, not on GitHub's "
                                       "shared runners (#6505)")
class PageSpeed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="maxpm-speed-")
        cls.old_db = os.environ.get("MAXPM_DB")
        os.environ["MAXPM_DB"] = os.path.join(cls.dir, "speed.db")
        conn = core.connect()
        speed_queue.build(conn)
        conn.close()

    @classmethod
    def tearDownClass(cls):
        server.state_changed()
        if cls.old_db is None:
            os.environ.pop("MAXPM_DB", None)
        else:
            os.environ["MAXPM_DB"] = cls.old_db
        shutil.rmtree(cls.dir, True)

    def builds(self, closed, n):
        runs = []
        for k in range(n + 1):
            server.state_changed()  # as after an action: the next request builds the state again
            t = {}
            server.page_state(closed, t)
            self.assertIn("build_ms", t, "the state came from the cache: no build was measured")
            if k:
                runs.append(t)
        return runs

    def test_the_main_page_builds_within_the_budget(self):
        runs = self.builds(False, BUILDS)
        p90 = _p90([r["build_ms"] for r in runs])
        self.assertLessEqual(p90, core.PAGE_BUILD_WARN, _report("main", runs, core.PAGE_BUILD_WARN))

    def test_the_done_view_builds_within_the_budget(self):
        runs = self.builds(True, DONE_BUILDS)
        p90 = _p90([r["build_ms"] for r in runs])
        self.assertLessEqual(p90, core.PAGE_BUILD_WARN, _report("done view", runs, core.PAGE_BUILD_WARN))

    def test_the_report_names_the_slowest_part(self):
        runs = [{"build_ms": 150.0, "parts": {"start": 2.0, "state": 140.0, "goals": 8.0}, "cpu_ms": 120.0},
                {"build_ms": 90.0, "parts": {"start": 2.0, "state": 80.0, "goals": 8.0}, "cpu_ms": 80.0}]
        text = _report("main", runs, 100)
        self.assertIn("p90 150.0 ms", text)
        self.assertIn("slowest part is 'state'", text)


if __name__ == "__main__":
    unittest.main()
