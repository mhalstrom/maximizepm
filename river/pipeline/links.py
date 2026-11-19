"""Pipelines and the rest of MaximizePM (#6118): the few calls that the package makes into the queue.

- A failed stage run opens a fault item, as a failed check does (an operational item, which starts in a new session
  by itself): its context holds the prompt that analyzes the failure. A later failure of the same stage, while that
  item is open, joins it with a line in its history. A trigger rule of the pipeline does it (triggers.py, #6150;
  each pipeline starts with the rule 'any stage fails').
- The prompt of a failed test run adds a fix task, or adds it and starts a session on it (fix_task).
- The tasks of a version: the commits between the version before it and it that name #<id> (version_tasks).
- The versions of a task: which environments run a commit that names it, and which versions carry one
  (item_versions).
- The work under each stage (#6149, stage_work): the fault items of a stage and the sessions that work on them or on
  their fix tasks, and on a deploy target's release (its review and deploy items), for the Deploy tab's Agents view
  and the small marks on the stage cards.

The git reads run in the project's folder (projects.path), each with a short timeout.
"""

import os
import re
import subprocess

from .. import core
from . import audit, prompt, store

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


def _what(conn, r, does=None, rule=None):
    """The text of a failed run for its fault item: the test prompt, or its errors, failed steps and last lines;
    then what the agent does (the trigger rule's words, #6150) and the command that runs the stage again."""
    if any(t["state"] in ("failed", "error") for t in store.tests(conn, r["id"], limit=1)):
        text = prompt.failure_prompt(conn, r["id"])["text"]
    else:
        text = prompt.run_text(conn, r["id"])
    return text + (f"\n\nWhat to do (trigger rule {rule}): " if rule else "\n\n") + (does or (
        "Find the cause; say whether it is the code, the stage's command or the environment; fix it, then run the "
        "stage again.")) + f"\nRun the stage again: maxpm pipeline rerun {r['pipeline']} {r['stage']} --rev {r['rev']}"


def add_does(conn, item_id, rule, does, what):
    """A second trigger rule's words on a fault item that exists (inside the caller's tx): a line of its history,
    and its context, which the next session reads, when the context does not hold them yet."""
    core._event(conn, item_id, f"trigger {rule}", f"{what}; what to do: {does}")
    ctx = conn.execute("SELECT context FROM items WHERE id=?", (item_id,)).fetchone()["context"] or ""
    if does not in ctx:
        conn.execute("UPDATE items SET context=? WHERE id=?",
                     (ctx + f"\n\nWhat to do (trigger rule {rule}): {does}", item_id))


def fault(conn, run_id):
    """Open the fault item of a failed run, or join the open one of its stage. Returns the item's id, or None when
    the run did not fail or the pipeline has no project to hold it."""
    return fault_for(conn, run_id)[0]


def fault_for(conn, run_id, does=None, rule=None):
    """fault for the trigger rule named rule (triggers.py) with its words does. Returns (item id, 'opened' or
    'joined'), or (None, why) when there is no failed run or no project to hold the item."""
    r = _run(conn, run_id)
    if r is None or r["state"] != "failed":
        return None, "the run did not fail"
    if not r["project"] or not conn.execute("SELECT 1 FROM projects WHERE name=?", (r["project"],)).fetchone():
        return None, "no project to hold the task"
    old = r["fault_item"] or None
    if not old:
        row = conn.execute("SELECT x.fault_item FROM stage_runs x JOIN items i ON i.id=x.fault_item WHERE x.stage_id=? "
                           "AND i.status NOT IN ('done','dropped') ORDER BY x.id DESC LIMIT 1",
                           (r["stage_id"],)).fetchone()
        old = row["fault_item"] if row else None
    if old:
        with core.tx(conn):
            if not r["fault_item"]:
                conn.execute("UPDATE stage_runs SET fault_item=? WHERE id=?", (old, run_id))
                core._event(conn, old, "maxpm", f"pipeline {r['pipeline']}: stage {r['stage']} failed again for "
                                                f"{r['rev']} (run #{run_id}): {r['errors'] or 'failed'}")
            if does and rule:
                add_does(conn, old, rule, does, f"stage {r['stage']} failed for {r['rev']} (run #{run_id})")
        return old, "joined"
    item = core.item_add(conn, r["project"], f"pipeline:{r['pipeline']}:{r['stage']} failed for {(r['rev'] or '')[:12]}"
                         f": {(r['errors'] or 'failed')[:120]}", doer="ai", kind="operational", actor="maxpm", goals=[],
                         short=f"{r['stage']} stage failed", context=_what(conn, r, does, rule))
    with core.tx(conn):
        conn.execute("UPDATE stage_runs SET fault_item=? WHERE id=?", (item["id"], run_id))
    return item["id"], "opened"  # the rule that acted writes the line of the audit log (triggers._fire)


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


# ---------------------------------------------------------------- the work under each stage (#6149)

DONE_SHOWN = 86400  # seconds: a done fault or fix stays under its stage this long, with its proof and its cost


def _ago(at):
    secs = int((core.now() - core.parse_iso(at)).total_seconds())
    return f"{secs // 3600}h" if secs >= 3600 else f"{max(secs // 60, 0)}m"


def _cost(conn, agent):
    """The dollars of the session's model calls so far (river/cost.py reads its chat log, at most once a minute), or
    None when its log gives none or its model has no price."""
    from .. import cost
    try:
        reqs = cost._requests(conn, agent)
    except Exception:  # noqa: BLE001 - a log that cannot be read shows no cost; the view still draws
        return None
    a = conn.execute("SELECT model FROM agents WHERE name=?", (agent,)).fetchone()
    usd, any_price = 0.0, False
    for _t, u in reqs:
        got = cost.priced(conn, u.get("model") or (a["model"] if a else None),
                          {k: u.get(k) or 0 for k in cost.TOKEN_KEYS})
        if got["usd"] is not None:
            usd, any_price = usd + got["usd"], True
    return round(usd, 2) if any_price else None


def _where(conn, a):
    """Where a session runs and how: this computer or a host, and the agent CLI with its model."""
    from .. import agents as adapters
    t = a["agent_type"] or a["platform"]
    ad = adapters.find(t) if t else None
    label = (getattr(ad, "DESCRIBE", None) or {}).get("label") if ad else None
    host = "this computer" if not a["host"] or a["host"] == core.this_host() else a["host"]
    return host, " · ".join(x for x in (label or t, a["model"]) if x) or None


def _agent_card(conn, name, item, started_by, cache):
    """One agent under a stage: its state, present step, where it runs, its keys, what started it, its proof, its
    cost, and its open question (a yes or a no on the page, never a prompt in a terminal)."""
    if name in cache:
        a = cache[name]
    else:
        a = cache[name] = conn.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone()
    if a is None or a["kind"] != "ai":
        return None
    q = conn.execute("SELECT id, body, created_at FROM messages WHERE kind='question' AND state='open' AND "
                     "from_agent=? ORDER BY id DESC LIMIT 1", (name,)).fetchone()
    live = core._agent_state(conn, a)
    if item["status"] in core.CLOSED_STATES:
        st, state = "ok", f"{item['status']} {_ago(item['closed_at'])} ago" if item["closed_at"] else item["status"]
    elif q is not None:
        st, state = "human", "waits for a yes"
    elif live == "active":
        st, state = "run", "working" + (f" {_ago(item['claimed_at'])}" if item["claimed_at"] else "")
    elif live == "away":
        st, state = "wait", "at its prompt"
    else:
        st, state = "wait", live  # gone or stopped: its item goes back to the queue when its lease ends
    host, how = _where(conn, a)
    from .. import vault
    try:
        keys = sorted({g["set"] for g in vault.grants(conn, for_agent=name)["grants"]})
    except core.RiverError:
        keys = []
    return {"name": name, "item": item["id"], "title": item["short"] or item["title"], "st": st, "state": state,
            "live": live, "step": a["doing"], "where": host, "how": how, "keys": keys, "started_by": started_by,
            "proof": item["output"] if item["status"] == "done" else None, "cost": _cost(conn, name),
            "question": {"id": q["id"], "body": q["body"], "at": q["created_at"]} if q else None}


def _holder(conn, item):
    """The session that holds an item now, or that closed it."""
    if item["assignee"]:
        return item["assignee"]
    if item["status"] in core.CLOSED_STATES:
        r = conn.execute("SELECT actor FROM events WHERE item_id=? AND (change LIKE 'done%' OR change LIKE 'dropped%') "
                         "ORDER BY id DESC LIMIT 1", (item["id"],)).fetchone()
        return r["actor"] if r else None
    return None


def stage_work(conn, pipeline=None, defaults=None):
    """{pipeline: {stage: {"faults": [...], "agents": [...]}}}: the work under each stage now. A fault is an open
    fault item of the stage (or one closed in the last DONE_SHOWN); its agents are the sessions that hold it or one
    of its fix tasks (found during it), or closed one. A deploy target's release shows the sessions on its review
    and deploy items under those stages. pipeline: only that one. defaults: {target: its default pipeline} that the
    caller built already (targets.default_pipeline), so they are not built twice."""
    since = core.iso(core.now() - core.timedelta(seconds=DONE_SHOWN))
    out, cache = {}, {}

    def slot(p, s):
        return out.setdefault(p, {}).setdefault(s, {"faults": [], "agents": []})

    def add_agent(p, s, item, started_by):
        name = _holder(conn, item)
        if not name or any(x["name"] == name and x["item"] == item["id"] for x in slot(p, s)["agents"]):
            return
        card = _agent_card(conn, name, item, started_by, cache)
        if card:
            slot(p, s)["agents"].append(card)

    q = ("SELECT DISTINCT r.fault_item, s.name stage, p.name pipeline, MIN(r.ended_at) failed_at FROM stage_runs r "
         "JOIN pipeline_stages s ON s.id=r.stage_id JOIN pipelines p ON p.id=r.pipeline_id JOIN items i ON "
         "i.id=r.fault_item WHERE r.fault_item>0 AND (i.status NOT IN ('done','dropped') OR i.closed_at>=?)")
    args = [since]
    if pipeline:
        q, args = q + " AND p.name=?", args + [pipeline]
    for f in conn.execute(q + " GROUP BY r.fault_item, s.name, p.name ORDER BY r.fault_item", args).fetchall():
        it = conn.execute("SELECT * FROM items WHERE id=?", (f["fault_item"],)).fetchone()
        rev = conn.execute("SELECT v.rev FROM stage_runs r LEFT JOIN pipeline_versions v ON v.id=r.version_id WHERE "
                           "r.fault_item=? ORDER BY r.id DESC LIMIT 1", (it["id"],)).fetchone()["rev"]
        fixes = conn.execute("SELECT * FROM items WHERE found_during=? AND (status NOT IN ('done','dropped') OR "
                             "closed_at>=?) ORDER BY id", (it["id"], since)).fetchall()
        slot(f["pipeline"], f["stage"])["faults"].append(
            {"id": it["id"], "title": it["short"] or it["title"], "status": it["status"], "rev": rev,
             "at": f["failed_at"] or it["created_at"], "fixes": [x["id"] for x in fixes],
             "nobody": not _holder(conn, it) and not any(_holder(conn, x) for x in fixes)})
        add_agent(f["pipeline"], f["stage"], it,
                  f"stage {f['stage']} failed for {(rev or '')[:12]} (fault #{it['id']})")
        for x in fixes:
            add_agent(f["pipeline"], f["stage"], x, f"a fix task of fault #{it['id']}")
    from . import targets
    if defaults is None:
        names = [t["name"] for t in targets.default_list(conn)] if not pipeline else (
            [pipeline] if not conn.execute("SELECT 1 FROM pipelines WHERE name=?", (pipeline,)).fetchone() else [])
        defaults = {}
        for t in names:
            try:
                defaults[t] = targets.default_pipeline(conn, t)
            except core.RiverError:
                continue
    for t, d in defaults.items():
        rel = (d.get("releases") or [None])[0]
        for stage, run in ((rel or {}).get("stages") or {}).items():
            if stage not in ("review", "deploy") or not run.get("item"):
                continue
            it = conn.execute("SELECT * FROM items WHERE id=?", (run["item"],)).fetchone()
            if it and (it["status"] not in core.CLOSED_STATES or (it["closed_at"] or "") >= since):
                add_agent(t, stage, it, f"the {stage} of release #{rel['deploy']} of target {t}")
    return out
