"""A JUnit XML test report as test events (#6112): each test's name, suite, state, time and message.

The form most test tools write (pytest --junitxml, Maven Surefire, Jest, Go's gotestsum): a <testsuites> root or a
<testsuite> root, <testsuite> elements that may nest, and <testcase name classname time file> elements with a
<failure>, <error> or <skipped> child, or none (passed). The suite of a test is its classname, or else the name of
the <testsuite> around it.

A report becomes, for each <testsuite> with tests, one testsuiterun.started, a testcaserun event for each test, and
one testsuiterun.finished, all kept in one transaction (store.ingest). Read with xml.etree (standard library); a
report with a DOCTYPE is refused, so no entity in it can grow without end.
"""

import re
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from .. import core
from . import cdevents, store


def _tag(el):
    return el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""


def _seconds(v):
    try:
        return float(str(v).replace(",", "")) if v not in (None, "") else None
    except ValueError:
        return None


# The prolog of an XML document: the declaration, processing instructions, comments and spaces before the root.
_PROLOG = re.compile(r"\A(?:\s+|<\?.*?\?>|<!--.*?-->)*", re.S)


def _has_doctype(xml):
    """A DOCTYPE, the only place where entities are declared, can stand only in the prolog: expat refuses one
    after the root element starts. So a look at the prolog finds every one."""
    if isinstance(xml, bytes):
        enc = "utf-16" if xml[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
        xml = xml.decode(enc, "replace")
    end = _PROLOG.match(xml).end()
    return xml[end:end + 9].upper() == "<!DOCTYPE"


def parse(xml):
    """The suites of a report: [{"name", "seconds", "tests": [{"suite", "name", "state", "seconds", "message",
    "detail", "file"}]}], in the report's order. Refuses text that is not a JUnit report."""
    if _has_doctype(xml):
        raise core.RiverError("the JUnit report has a DOCTYPE; MaximizePM reads reports without one")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise core.RiverError(f"the JUnit report is not XML ({e})") from None
    if _tag(root) not in ("testsuites", "testsuite"):
        raise core.RiverError(f"the report's root is <{_tag(root)}>, not <testsuites> or <testsuite>: not JUnit XML")
    suites = []

    def walk(el, outer):
        name = el.get("name") or outer or "tests"
        tests = []
        for child in el:
            t = _tag(child)
            if t == "testsuite":
                walk(child, name)
            elif t == "testcase":
                tests.append(_case(child, name))
        if tests:
            suites.append({"name": name, "seconds": _seconds(el.get("time")), "tests": tests})

    if _tag(root) == "testsuites":
        for child in root:
            if _tag(child) == "testsuite":
                walk(child, root.get("name"))
    else:
        walk(root, None)
    return suites


def _case(el, suite):
    state, message, detail = "passed", None, None
    for child in el:
        t = _tag(child)
        if t in ("failure", "error") and state in ("passed", "skipped"):
            state = "failed" if t == "failure" else "error"
            message = child.get("message") or child.get("type")
            detail = (child.text or "").strip() or None
        elif t == "skipped" and state == "passed":
            state, message = "skipped", child.get("message")
    if message is None and detail:
        message = detail.splitlines()[0]
    return {"suite": el.get("classname") or suite, "name": el.get("name") or "(no name)", "state": state,
            "seconds": _seconds(el.get("time")), "message": message, "detail": detail, "file": el.get("file")}


_TEST_WORD = {"passed": "pass", "failed": "fail", "error": "error"}


def events(run, suites, source="maxpm", at=None):
    """The test events of parsed suites for a stage run (a row with id, key and pipeline)."""
    out = []
    base = {"pipeline": run["pipeline"], "run": run["id"]}
    for s in suites:
        key = f"junit/{s['name']}/{uuid.uuid4().hex[:8]}"
        suite_ref = {"id": key, "source": source}
        out.append(cdevents.make("testsuiterun", "started", key, {"testSuite": {"id": s["name"], "name": s["name"]}},
                                 base, source, at))
        for t in s["tests"]:
            pred = "skipped" if t["state"] == "skipped" else "finished"
            content = {"testCase": {"id": f"{t['suite']}.{t['name']}", "name": t["name"]}, "testSuiteRun": suite_ref,
                       "outcome": _TEST_WORD.get(t["state"]), "reason": t["message"]}
            out.append(cdevents.make("testcaserun", pred, f"{key}/{t['suite']}.{t['name']}", content,
                                     {**base, "suite": t["suite"], "seconds": t["seconds"],
                                      "detail": store._cut(t["detail"], store.DETAIL_MAX), "file": t["file"]},
                                     source, at))
        failed = any(t["state"] in ("failed", "error") for t in s["tests"])
        out.append(cdevents.make("testsuiterun", "finished", key,
                                 {"testSuite": {"id": s["name"], "name": s["name"]},
                                  "outcome": "fail" if failed else "pass"},
                                 {**base, "seconds": s["seconds"]}, source, at))
    return out


def ingest_junit(conn, run_id, xml=None, path=None, source="maxpm", at=None):
    """Keep a JUnit report's results for a stage run: the report's text (xml) or its file (path). Returns the
    count of suites and of tests in each state."""
    if (xml is None) == (path is None):
        raise core.RiverError("give the JUnit report as its text or as its file, one of them")
    if path is not None:
        try:
            xml = Path(path).read_bytes()
        except OSError as e:
            raise core.RiverError(f"cannot read the JUnit report {path}: {e.strerror}") from None
    suites = parse(xml)
    run = conn.execute("SELECT r.id, r.key, p.name pipeline FROM stage_runs r JOIN pipelines p "
                       "ON p.id=r.pipeline_id WHERE r.id=?", (run_id,)).fetchone()
    if not run:
        raise core.RiverError(f"no stage run {run_id}")
    store.ingest(conn, events(run, suites, source, at))
    counts = {"suites": len(suites), "tests": 0, "passed": 0, "failed": 0, "error": 0, "skipped": 0}
    for s in suites:
        for t in s["tests"]:
            counts["tests"] += 1
            counts[t["state"]] += 1
    return counts
