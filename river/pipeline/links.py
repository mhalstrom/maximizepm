"""Pipelines and the rest of MaximizePM (#6118): the few calls that the package makes into the queue.

- A failed stage run opens a fault item, as a failed check does (an operational item, which starts in a new session
  by itself): its context holds the prompt that analyzes the failure. A later failure of the same stage, while that
  item is open, joins it with a line in its history. serve_pass does it, from runner.serve_pass.
- The prompt of a failed test run adds a fix task, or adds it and starts a session on it (fix_task).
- The tasks of a version: the commits between the version before it and it that name #<id> (version_tasks).
- The versions of a task: which environments run a commit that names it, and which versions carry one
  (item_versions).

The git reads run in the project's folder (projects.path), each with a short timeout.
"""

import os
import re
import subprocess

from .. import core
from . import audit, prompt, store

FAULT_WITHIN = 86400  # seconds: a failed run older than this when serve first sees it opens no fault item
_REF = re.compile(r"#(\d+)\b")


def _folder(conn, project):
    r = conn.execute("SELECT path FROM projects WHERE name=?", (project,)).fetchone() if project else None
    return os.path.expanduser(r["path"]) if r and r["path"] else None


def _git(folder, *args, timeout=5):
    try:
        out = subprocess.run(["git", "-C", folder, *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return out if out.returncode in (0, 1) else None


def _run(conn, run_id):
    return conn.execute("SELECT r.*, s.name stage, s.kind, p.name pipeline, p.project, v.rev FROM stage_runs r JOIN "
                        "pipeline_stages s ON s.id=r.stage_id JOIN pipelines p ON p.id=r.pipeline_id LEFT JOIN "
                        "pipeline_versions v ON v.id=r.version_id WHERE r.id=?", (run_id,)).fetchone()


def _what(conn, r):
    """The text of a failed run for its fault item: the test prompt, or its errors, failed steps and last lines."""
    if any(t["state"] in ("failed", "error") for t in store.tests(conn, r["id"], limit=1)):
        return prompt.failure_prompt(conn, r["id"])["text"]
    return prompt.run_text(conn, r["id"]) + ("\n\nFind the cause; say whether it is the code, the stage's command or "
                                             "the environment; fix it, then run the stage again "
                                             f"(maxpm pipeline rerun {r['pipeline']} {r['stage']} --rev {r['rev']}).")


def fault(conn, run_id):
    """Open the fault item of a failed run, or join the open one of its stage. Returns the item's id, or None when
    the pipeline has no project to hold it."""
    r = _run(conn, run_id)
    if r is None or r["state"] != "failed" or r["fault_item"]:
        return r["fault_item"] if r else None
    if not r["project"] or not conn.execute("SELECT 1 FROM projects WHERE name=?", (r["project"],)).fetchone():
        return None
    old = conn.execute("SELECT x.fault_item FROM stage_runs x JOIN items i ON i.id=x.fault_item WHERE x.stage_id=? AND "
                       "i.status NOT IN ('done','dropped') ORDER BY x.id DESC LIMIT 1", (r["stage_id"],)).fetchone()
    if old:
        with core.tx(conn):
            conn.execute("UPDATE stage_runs SET fault_item=? WHERE id=?", (old["fault_item"], run_id))
            core._event(conn, old["fault_item"], "maxpm", f"pipeline {r['pipeline']}: stage {r['stage']} failed again "
                                                          f"for {r['rev']} (run #{run_id}): {r['errors'] or 'failed'}")
        return old["fault_item"]
    item = core.item_add(conn, r["project"], f"pipeline:{r['pipeline']}:{r['stage']} failed for {(r['rev'] or '')[:12]}"
                         f": {(r['errors'] or 'failed')[:120]}", doer="ai", kind="operational", actor="maxpm", goals=[],
                         short=f"{r['stage']} stage failed", context=_what(conn, r))
    with core.tx(conn):
        conn.execute("UPDATE stage_runs SET fault_item=? WHERE id=?", (item["id"], run_id))
        audit.write(conn, r["pipeline_id"], "the rule 'a stage fails'", "fault",
                    f"opened fault #{item['id']} for stage {r['stage']}: a new session takes it", kind="trigger",
                    place=f"stage {r['stage']}", rev=r["rev"], result=f"fault #{item['id']}")
    return item["id"]


def serve_pass(conn):
    """The failed runs that have no fault item yet get one (runs of some failed tests again do not)."""
    since = core.iso(core.now() - core.timedelta(seconds=FAULT_WITHIN))
    out = []
    for r in conn.execute("SELECT id FROM stage_runs WHERE state='failed' AND fault_item IS NULL AND ended_at>=? AND "
                          "(purpose IS NULL OR purpose NOT LIKE 'failed:%') ORDER BY id", (since,)).fetchall():
        item = fault(conn, r["id"])
        if item is None:  # no project to hold it: do not look again
            with core.tx(conn):
                conn.execute("UPDATE stage_runs SET fault_item=0 WHERE id=?", (r["id"],))
        out.append(item)
    return out


def fix_task(conn, run_id, actor=None, start=False):
    """The prompt of a failed run as a fix task (found during its fault item); start: and a session on it now."""
    r = _run(conn, run_id)
    if r is None:
        raise core.RiverError(f"no stage run {run_id}")
    if not r["project"]:
        raise core.RiverError(f"pipeline {r['pipeline']} names no project for the task")
    item = core.item_add(conn, r["project"], f"Fix the failure of the {r['stage']} stage of pipeline {r['pipeline']} "
                         f"for {(r['rev'] or '')[:12]}", doer="ai", actor=actor, context=_what(conn, r),
                         found_during=r["fault_item"] or None, short=f"Fix the {r['stage']} failure")
    out = {"item": item["id"], "started": None}
    if start:
        from .. import server
        out["started"] = server.dispatch_item(conn, item["id"], actor=actor)
    with core.tx(conn):
        audit.write(conn, r["pipeline_id"], actor, "fix", f"added fix task #{item['id']} for the failed {r['stage']} "
                    f"stage (run #{run_id})" + ("; a session starts on it" if start else ""), place=f"stage {r['stage']}",
                    rev=r["rev"], result=f"task #{item['id']}" + (", a session started" if out["started"] else ""))
    return out


def version_tasks(conn, pipeline, rev, base=None):
    """The tasks of a version: the items that the commits from base (the newest older version of the pipeline,
    when none) to rev name. [{id, title, status, commits}]"""
    p = store._pipeline(conn, pipeline)
    folder = _folder(conn, p["project"])
    if not folder:
        raise core.RiverError(f"pipeline {p['name']} has no project folder to read its commits")
    if base is None:
        for v in conn.execute("SELECT rev FROM pipeline_versions WHERE pipeline_id=? AND rev<>? ORDER BY id DESC "
                              "LIMIT 20", (p["id"], rev)).fetchall():
            ok = _git(folder, "merge-base", "--is-ancestor", v["rev"], rev)
            if ok is not None and ok.returncode == 0:
                base = v["rev"]
                break
    log = _git(folder, "log", "--format=%h %s", "-n", "200", f"{base}..{rev}" if base else rev)
    if log is None:
        raise core.RiverError(f"git cannot read the commits of {rev} in {folder}")
    found = {}
    for line in log.stdout.splitlines():
        for n in _REF.findall(line):
            found.setdefault(int(n), []).append(line.split(" ", 1)[0])
    rows = {r["id"]: r for r in conn.execute(
        f"SELECT id, title, status FROM items WHERE id IN ({','.join('?' * len(found)) or 'NULL'})", list(found))}
    return {"pipeline": p["name"], "rev": rev, "base": base,
            "tasks": [{"id": i, "title": rows[i]["title"], "status": rows[i]["status"], "commits": c}
                      for i, c in sorted(found.items()) if i in rows]}


def item_versions(conn, item_id):
    """Where a task runs: for each pipeline of its project, the environments whose version holds a commit that names
    it, and the newest versions that carry one."""
    it = conn.execute("SELECT i.id, p.name project FROM items i JOIN projects p ON p.id=i.project_id WHERE i.id=?",
                      (item_id,)).fetchone()
    if not it:
        raise core.RiverError(f"no item #{item_id}")
    folder = _folder(conn, it["project"])
    out = []
    pipes = conn.execute("SELECT * FROM pipelines WHERE project=?", (it["project"],)).fetchall()
    log = (_git(folder, "log", "--all", "--format=%H", "-n", "20", "-E", f"--grep=#{item_id}([^0-9]|$)")
           if folder and pipes else None)
    commits = log.stdout.split() if log else []

    def carries(rev):
        return any((_git(folder, "merge-base", "--is-ancestor", c, rev) or subprocess.CompletedProcess([], 1)).returncode
                   == 0 for c in commits)
    for p in pipes:
        envs = {e["name"]: {"rev": e["rev"], "carries": bool(commits and e["rev"] and carries(e["rev"]))}
                for e in conn.execute("SELECT e.name, v.rev FROM pipeline_envs e LEFT JOIN pipeline_versions v ON "
                                      "v.id=e.version_id WHERE e.pipeline_id=? AND e.removed_at IS NULL", (p["id"],))}
        versions = [v["rev"] for v in conn.execute("SELECT rev FROM pipeline_versions WHERE pipeline_id=? ORDER BY id "
                                                   "DESC LIMIT 10", (p["id"],)) if commits and carries(v["rev"])]
        out.append({"pipeline": p["name"], "envs": envs, "versions": versions})
    return {"item": item_id, "commits": commits, "pipelines": out}
