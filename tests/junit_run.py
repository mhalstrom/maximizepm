"""Run the unit tests and write a JUnit XML report (#6119): the pipeline of MaximizePM reads it from the GitHub run's
artifact, so each test's result shows by name. Standard library only.

    python tests/junit_run.py report.xml [suite prefix]

It discovers the tests as `python -m unittest discover -s tests -t .` does, prints the usual summary, writes the
report, and exits 1 when a test failed or had an error. The prefix (py3.13) goes before each suite's name, so the
reports of several Python versions keep their own results."""

import sys
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


class Result(unittest.TextTestResult):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.cases, self._t0 = [], {}

    def startTest(self, test):
        self._t0[test.id()] = time.monotonic()
        super().startTest(test)

    def _add(self, test, state, err=None):
        seconds = time.monotonic() - self._t0.get(test.id(), time.monotonic())
        self.cases.append((test.id(), state, seconds, err))

    def addSuccess(self, test):
        super().addSuccess(test)
        self._add(test, "passed")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._add(test, "failure", self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self._add(test, "error", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._add(test, "skipped", reason)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._add(test, "passed")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._add(test, "failure", "unexpected success")


def write(cases, path, prefix=""):
    root = ET.Element("testsuites", name="maximizepm")
    suites = {}
    for tid, state, seconds, err in cases:
        cls, _, name = tid.rpartition(".")
        suite = (prefix + " " if prefix else "") + cls
        el = suites.get(suite)
        if el is None:
            el = suites[suite] = ET.SubElement(root, "testsuite", name=suite)
        case = ET.SubElement(el, "testcase", classname=suite, name=name, time=f"{seconds:.3f}")
        if state in ("failure", "error"):
            last = (err or "").strip().splitlines()[-1:] or [state]
            ET.SubElement(case, state, message=last[0][:300]).text = err
        elif state == "skipped":
            ET.SubElement(case, "skipped", message=str(err or "")[:300])
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def main(argv):
    if not argv:
        print(__doc__.split("\n\n")[1].strip(), file=sys.stderr)
        return 2
    top = Path(__file__).resolve().parent.parent
    suite = unittest.defaultTestLoader.discover(str(top / "tests"), top_level_dir=str(top))
    runner = unittest.TextTestRunner(resultclass=Result, verbosity=2)
    result = runner.run(suite)
    write(result.cases, argv[0], argv[1] if len(argv) > 1 else "")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
