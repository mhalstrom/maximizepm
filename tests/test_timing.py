"""#5705: a test that depends on the speed of the machine fails on GitHub's slow macOS runners. The guard below
refuses the two forms that failed there: a fixed sleep before a check, and an upper bound on the time a step took
that does not go through tests.timing.limit. A loop that polls (a while loop with a sleep in it), a fake that
sleeps to act slow (a function inside the test), and a line with a '# timing: <why>' comment pass."""
import ast
import os
import time
import unittest
from pathlib import Path

from tests import timing

HERE = Path(__file__).resolve().parent
CLOCKS = {"time", "monotonic", "perf_counter"}
UPPER = {"assertLess", "assertLessEqual"}


def _is_sleep(node):
    f = node.func
    return isinstance(f, ast.Attribute) and f.attr == "sleep" and isinstance(f.value, ast.Name) \
        and f.value.id in ("time", "_time")


def _reads_clock(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in CLOCKS \
                and isinstance(n.func.value, ast.Name) and n.func.value.id in ("time", "_time"):
            return True
    return False


def _scaled(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name == "limit":
                return True
    return False


def findings(path):
    """The lines of one test file that wait a fixed time before a check, or bound a time without limit()."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    tree = ast.parse(text)
    parents = {}
    for p in ast.walk(tree):
        for c in ast.iter_child_nodes(p):
            parents[c] = p
    out = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call) or "# timing:" in lines[n.lineno - 1]:
            continue
        if _is_sleep(n):
            p, polls = parents.get(n), False
            while p is not None and not isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                # A while loop, or a for loop that ends on the event (a break or a return in it), polls.
                polls = polls or isinstance(p, ast.While) or (isinstance(p, ast.For) and any(
                    isinstance(x, (ast.Break, ast.Return)) for x in ast.walk(p)))
                p = parents.get(p)
            if p is None or polls or isinstance(p, ast.Lambda):
                continue
            owner = parents.get(p)
            if not (isinstance(owner, ast.Module) or (isinstance(owner, ast.ClassDef)
                                                     and isinstance(parents.get(owner), ast.Module))):
                continue  # a function inside a test: a fake that acts slow, or a thread's work
            out.append(f"{path.name}:{n.lineno}: a fixed sleep: wait for the event with timing.wait_for")
        elif isinstance(n.func, ast.Attribute) and n.func.attr in UPPER and len(n.args) >= 2 \
                and _reads_clock(n.args[0]) and not _scaled(n.args[1]):
            out.append(f"{path.name}:{n.lineno}: a time bound: write it as timing.limit(<seconds on a fast machine>)")
    return out


class Guard(unittest.TestCase):
    def test_no_fixed_sleep_and_no_unscaled_time_bound(self):
        found = []
        for path in sorted(HERE.glob("*.py")):
            if path.name not in ("timing.py", "test_timing.py"):
                found += findings(path)
        self.assertEqual(found, [], "\n" + "\n".join(found))

    def test_the_guard_sees_both_forms(self):
        src = HERE / "fixtures" / "timing_bad.py.txt"
        found = findings(src)
        self.assertEqual([f.split(": ", 1)[1].split(":")[0] for f in found], ["a fixed sleep", "a time bound"], found)


class Helper(unittest.TestCase):
    def test_wait_for_returns_the_value_and_fails_with_the_event(self):
        n = [0]

        def check():
            n[0] += 1
            return n[0] >= 3 and "ready"
        self.assertEqual(timing.wait_for(check, 5, step=0.001), "ready")
        with self.assertRaisesRegex(AssertionError, "the frame did not happen within"):
            timing.wait_for(lambda: False, 0.01 / timing.SCALE, "the frame", step=0.001)

    def test_the_scale_is_larger_on_ci(self):
        old = {k: os.environ.get(k) for k in ("CI", "MAXPM_TEST_TIME_SCALE")}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in old.items()])
        os.environ.pop("MAXPM_TEST_TIME_SCALE", None)
        os.environ.pop("CI", None)
        self.assertEqual(timing._scale(), 1.0)
        os.environ["CI"] = "true"
        self.assertEqual(timing._scale(), 5.0)
        os.environ["MAXPM_TEST_TIME_SCALE"] = "8"
        self.assertEqual(timing._scale(), 8.0)
        t = time.monotonic()
        self.assertLess(time.monotonic() - t, timing.limit(1))


if __name__ == "__main__":
    unittest.main()
