"""The prompt that analyzes the failed tests of a stage run (#6116; the test view of canvas pipeline-mockup v2).

It names the version, the last version that passed the same stage, the commits between them (git log, when the
project has a folder), each failed test with its message and its history (new, flaky, or failed before too), and
where the full output is. The page shows it under the test view, with Copy; #6118 starts a session with it.
"""

import os
import subprocess

from . import store

HISTORY = 10  # the earlier runs of the stage that a test's history reads
LOG_MAX = 30  # the commits the prompt lists
DETAIL_LINES = 12


def _folder(conn, project):
    r = conn.execute("SELECT path FROM projects WHERE name=?", (project,)).fetchone() if project else None
    return r["path"] if r and r["path"] else None


def _git_log(folder, old, new):
    """The commits from old to new, one line each, or None when git cannot say."""
    try:
        out = subprocess.run(["git", "-C", os.path.expanduser(folder), "log", "--oneline", "--no-decorate",
                              f"{old}..{new}"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.splitlines() if out.returncode == 0 else None


def history(conn, run, test):
    """A failed test's history in the earlier runs of its stage: 'new' (it passed every earlier run, or never ran),
    'flaky' (it passed and failed), or how often it failed before."""
    rows = conn.execute("SELECT t.state FROM test_results t JOIN stage_runs r ON r.id=t.run_id WHERE r.stage_id=? "
                        "AND r.id<? AND t.suite=? AND t.name=? ORDER BY r.id DESC LIMIT ?",
                        (run["stage_id"], run["id"], test["suite"], test["name"], HISTORY)).fetchall()
    states = [r["state"] for r in rows]
    bad = sum(1 for s in states if s in ("failed", "error"))
    if not states:
        return "new test: no earlier run"
    if not bad:
        return f"new: passed the {len(states)} earlier run(s)"
    if states[0] in ("failed", "error") and bad == len(states):
        return f"failed in each of the {len(states)} earlier run(s)"
    return f"flaky: failed in {bad} of the {len(states)} earlier run(s)"


def failure_prompt(conn, run_id):
    """The prompt text for the failed tests of a stage run, and its parts: {text, version, passed, commits, failed}."""
    run = store.run_show(conn, run_id)
    failed = [t for t in store.tests(conn, run_id) if t["state"] in ("failed", "error")]
    total = sum(run["tests"].values())
    passed = conn.execute("SELECT v.rev FROM stage_runs x JOIN pipeline_versions v ON v.id=x.version_id WHERE "
                          "x.stage_id=? AND x.state='passed' AND x.id<? ORDER BY x.id DESC LIMIT 1",
                          (run["stage_id"], run_id)).fetchone()
    passed = passed["rev"] if passed else None
    project = conn.execute("SELECT project FROM pipelines WHERE id=?", (run["pipeline_id"],)).fetchone()["project"]
    stage = conn.execute("SELECT junit FROM pipeline_stages WHERE id=?", (run["stage_id"],)).fetchone()
    folder = _folder(conn, project)
    commits = _git_log(folder, passed, run["version"]) if folder and passed and run["version"] else None
    lines = [f"The {run['stage']} stage of pipeline {run['pipeline']} failed for version {run['version'] or 'unknown'}"
             f" (run #{run_id}): {len(failed)} of {total} tests failed."]
    if passed:
        lines.append(f"The last version that passed this stage: {passed}.")
        if commits is not None:
            more = f" (the first {LOG_MAX})" if len(commits) > LOG_MAX else ""
            lines.append(f"The commits between them: git log {passed}..{run['version']}, {len(commits)} commit(s){more}:")
            lines += [f"  {c}" for c in commits[:LOG_MAX]]
        else:
            lines.append(f"The commits between them: git log {passed}..{run['version']}")
    else:
        lines.append("No earlier version passed this stage.")
    lines += ["", "Failed tests:"]
    for n, t in enumerate(failed, 1):
        where = f" ({t['file']})" if t["file"] else ""
        lines.append(f"{n}. {t['suite']}.{t['name']}{where}: {t['message'] or t['state']}. {history(conn, run, t)}.")
        if t["detail"]:
            lines += ["   " + x for x in t["detail"].splitlines()[:DETAIL_LINES]]
    lines += ["", "The full output: maxpm pipeline run " + str(run_id)
              + (f"; the test report: {stage['junit']} in {folder or 'the project folder'}" if stage and stage["junit"] else "")
              + (f"; the run on the Git host: {run['url']}" if run.get("url") else "") + ".",
              "For each failure: find the cause, say whether it is in the code, the test or the environment, and "
              "propose the fix."]
    return {"text": "\n".join(lines), "version": run["version"], "passed": passed, "commits": commits,
            "failed": len(failed), "tests": total}


def run_text(conn, run_id):
    """maxpm pipeline run <id>: a stage run as text: its state, steps, failed tests and last lines."""
    r = store.run_show(conn, run_id)
    out = [f"run #{r['id']} of stage {r['stage']} ({r['kind']}), pipeline {r['pipeline']}: {r['state']}"
           + (f", version {r['version']}" if r["version"] else "") + (f": {r['errors']}" if r["errors"] else "")]
    if r.get("gate_note") and r["state"] == "queued":
        out.append(f"  at the transition: {r['gate_note']}")
    for s in r["step_list"]:
        out.append(f"  step {s['name']}" + (f" on {s['host']}" if s["host"] else "") + f": {s['state']}"
                   + (f", {s['seconds']:.0f}s" if s["seconds"] is not None else "") + (f" ({s['note']})" if s["note"] else ""))
    if r["tests"]:
        out.append("  tests: " + ", ".join(f"{n} {k}" for k, n in sorted(r["tests"].items())))
        for t in store.tests(conn, run_id):
            if t["state"] in ("failed", "error"):
                out.append(f"    {t['state']}: {t['suite']}.{t['name']}: {t['message'] or ''}")
    if r["output"]:
        out += ["  last lines:"] + ["    " + x for x in r["output"].splitlines()]
    return "\n".join(out)
