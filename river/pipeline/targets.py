"""Each deploy target of today as a default pipeline (#6115), computed from the records MaximizePM keeps now.

Nothing is stored and nothing that works today changes: a read builds the pipeline of a target from its deploy items
(one release each), their review items, the person's approval items (release_approval person), the hook runs
(release-cut, deploy) and the runs of the target's checks after each release (the watch). So toolscaled-prod and
maximizepm-github show in the pipeline view before they move to a pipeline of their own.

The stages, left to right, as a target has them:
    cut      (build)   the release's list of items is fixed (cut_at)
    build    (build)   the release-cut hook, when the target has one or ran one
    review   (review)  the review item, when the target reviews its releases
    approval (approve) the person's yes, when release_approval is person
    deploy   (deploy)  the deploy item, with the deploy hook's runs as its steps; its environment is the target
    watch    (check)   the target's checks after the release, until its last watch time (24h with none)

The result has the form of store.show (name, project, stages with last_run, envs) and of store.versions (releases),
with "default": true.
"""

from datetime import timedelta

from .. import core
from . import transitions

RELEASES = 10  # the newest releases a read covers
WATCH_DEFAULT = timedelta(hours=24)

_ITEM_STATE = {"done": "passed", "in_progress": "running", "held": "running", "open": "queued",
               "dropped": "cancelled"}


def _targets(conn):
    return conn.execute("SELECT * FROM targets ORDER BY name").fetchall()


def _target(conn, name):
    t = conn.execute("SELECT * FROM targets WHERE name=?", (name,)).fetchone()
    if not t:
        raise core.RiverError(f"no pipeline or target named {name!r}; the list: maxpm pipeline list")
    return t


def _hook_run(h):
    if h["started_at"] is None:
        state = "queued"
    elif h["ended_at"] is None:
        state = "running"
    else:
        state = "passed" if h["exit_code"] == 0 and not h["timed_out"] else "failed"
    return {"name": h["event"], "state": state, "started_at": h["started_at"], "ended_at": h["ended_at"],
            "seconds": h["seconds"], "output": h["output"], "note": f"exit {h['exit_code']}" if state == "failed"
            else None}


def _steps(conn, hooks):
    """The steps of hook runs: the '::step' lines each one printed (hook_steps, #6126), or the run as one step when
    it printed none. A step still open when its hook ended shows as stopped."""
    out = []
    for h in hooks:
        rows = conn.execute("SELECT * FROM hook_steps WHERE hook_run=? ORDER BY COALESCE(started_at, ended_at), id",
                            (h["id"],)).fetchall()
        if not rows:
            out.append(_hook_run(h))
            continue
        for s in rows:
            state = "stopped" if s["state"] == "running" and h["ended_at"] else s["state"]
            out.append({"name": s["name"], "host": s["host"], "kind": s["kind"], "state": state,
                        "started_at": s["started_at"], "ended_at": s["ended_at"] or (h["ended_at"] if state == "stopped"
                                                                                       else None),
                        "seconds": s["seconds"], "output": None, "note": s["note"]})
    return out


def _run(name, state, started=None, ended=None, version=None, steps=None, item=None, errors=None):
    steps = steps or []
    counts = {}
    for s in steps:
        counts[s["state"]] = counts.get(s["state"], 0) + 1
    return {"stage": name, "state": state, "started_at": started, "ended_at": ended, "version": version,
            "steps": counts, "step_list": steps, "tests": {}, "item": item, "errors": errors}


def _checks(conn, target):
    """The checks of the target and of its projects."""
    return conn.execute("SELECT id, scope, name FROM checks WHERE (scope_kind='target' AND scope=?) OR "
                        "(scope_kind='project' AND scope IN (SELECT name FROM projects WHERE target=?))",
                        (target, target)).fetchall()


def _watch(conn, t, dep, next_at, checks):
    """The watch of one release: the runs of the target's checks from its deploy until its last watch time (or
    the next release, when sooner)."""
    if dep["status"] != "done" or not dep["closed_at"]:
        return None
    if not checks:
        return _run("watch", "skipped", version=None, errors="the target has no checks")
    times = core.parse_watch_times(t["watch_times"])
    start = core.parse_iso(dep["closed_at"])
    end = start + (times[-1][1] if times else WATCH_DEFAULT)
    if next_at and core.parse_iso(next_at) < end:
        end = core.parse_iso(next_at)
    ids = [c["id"] for c in checks]
    marks = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT o.check_id, o.started_at, o.ended_at, o.status, o.summary FROM ops_runs o WHERE "
                        f"o.check_id IN ({marks}) AND o.started_at>=? AND o.started_at<=? ORDER BY o.id",
                        (*ids, dep["closed_at"], core.iso(end))).fetchall()
    names = {c["id"]: f"{c['scope']}/{c['name']}" for c in checks}
    steps = [{"name": names[r["check_id"]], "state": "failed" if r["status"] in ("warning", "critical") else
              "passed" if r["status"] == "ok" else "skipped", "started_at": r["started_at"],
              "ended_at": r["ended_at"], "seconds": None, "output": None, "note": r["summary"]} for r in rows]
    bad = [s for s in steps if s["state"] == "failed"]
    if bad:
        state = "failed"
    elif core.now() < end:
        state = "running"
    else:
        state = "passed" if steps else "skipped"
    return _run("watch", state, dep["closed_at"], core.iso(end) if state != "running" else None, steps=steps,
                errors=f"{len(bad)} failed check run(s): {bad[0]['name']}" if bad else None)


def _release(conn, t, dep, next_at, reviews, approval, checks):
    """The stage runs of one release (a deploy item): {stage name: run}."""
    from .. import report
    rev = dep["cut_rev"]
    hooks = conn.execute("SELECT * FROM hook_runs WHERE deploy_id=? ORDER BY id", (dep["id"],)).fetchall()
    if not rev:
        rev = next((h["rev"] for h in hooks if h["rev"]), None)
    out = {}
    out["cut"] = _run("cut", "passed" if dep["cut_at"] else "cancelled" if dep["status"] == "dropped" else "queued",
                      dep["created_at"], dep["cut_at"], item=dep["id"])
    cut_hooks = [_hook_run(h) for h in hooks if h["event"] == "release-cut"]
    if cut_hooks:
        last = cut_hooks[-1]
        out["build"] = _run("build", last["state"], cut_hooks[0]["started_at"], last["ended_at"],
                            steps=_steps(conn, [h for h in hooks if h["event"] == "release-cut"]), errors=last["note"])
    if reviews:
        rv = report._review(conn, dep["id"])
        if rv:
            st = _ITEM_STATE.get(rv["status"], "queued")
            failed = conn.execute("SELECT 1 FROM hook_runs WHERE deploy_id=? AND event='review-failed' LIMIT 1",
                                  (dep["id"],)).fetchone()
            if st == "queued" and failed:
                st = "failed"
            out["review"] = _run("review", st, rv["claimed_at"], rv["closed_at"], item=rv["id"],
                                 errors="the review sent the release back with fixes" if st == "failed" else None)
    if approval:
        yes = core._approvals(conn, dep["id"])
        if yes:
            a = yes[0]
            out["approval"] = _run("approval", _ITEM_STATE.get(a["status"], "queued"), a["created_at"], a["closed_at"],
                                   item=a["id"])
    deploy_steps = [_hook_run(h) for h in hooks if h["event"] in ("deploy", "deployed")]
    st = _ITEM_STATE.get(dep["status"], "queued")
    runs = [s for s in deploy_steps if s["name"] == "deploy"]
    if st == "queued" and runs and runs[-1]["state"] in ("running", "failed"):
        st = runs[-1]["state"]
    out["deploy"] = _run("deploy", st, dep["claimed_at"] or (runs[0]["started_at"] if runs else None),
                         dep["closed_at"] if dep["status"] == "done" else None,
                         steps=_steps(conn, [h for h in hooks if h["event"] in ("deploy", "deployed")]), item=dep["id"],
                         errors=runs[-1]["note"] if runs and runs[-1]["state"] == "failed" else None)
    w = _watch(conn, t, dep, next_at, checks)
    if w:
        out["watch"] = w
    label = rev or f"deploy #{dep['id']}"
    for r in out.values():
        r["version"] = label
    return out


def default_pipeline(conn, name):
    """The default pipeline of target name, in the form of store.show, with its releases newest first."""
    t = _target(conn, name)
    deps = conn.execute("SELECT * FROM items WHERE kind='deploy' AND target=? ORDER BY id DESC LIMIT ?",
                        (name, RELEASES)).fetchall()
    reviews = core._target_reviews(conn, name) or any(
        conn.execute("SELECT 1 FROM deps d JOIN items i ON i.id=d.blocked_by WHERE d.item_id=? AND i.kind='review'",
                     (d["id"],)).fetchone() for d in deps)
    approval = core.release_approval(conn, name)["approval"] == "person"
    has_cut_hook = conn.execute("SELECT 1 FROM hooks h JOIN targets x ON x.id=h.target_id WHERE x.name=? AND "
                                "h.event='release-cut'", (name,)).fetchone()
    hook = {r["event"]: r["command"] for r in conn.execute(
        "SELECT h.event, h.command FROM hooks h JOIN targets x ON x.id=h.target_id WHERE x.name=?", (name,))}
    checks = _checks(conn, name)
    done = [d for d in deps if d["status"] == "done" and d["closed_at"]]
    releases = []
    for d in deps:
        later = [x["closed_at"] for x in done if x["closed_at"] > (d["closed_at"] or "")]
        releases.append({"deploy": d["id"], "status": d["status"],
                         "runs": _release(conn, t, d, min(later) if later else None, reviews, approval, checks)})
    shape = [("cut", "build", None, "auto"), ("build", "build", hook.get("release-cut"), "auto"),
             ("review", "review", None, "auto"), ("approval", "approve", None, "yes"),
             ("deploy", "deploy", hook.get("deploy"), "auto"), ("watch", "check", None, "auto")]
    stages = []
    for stage, kind, cmd, gate in shape:
        if stage == "build" and not (has_cut_hook or any("build" in r["runs"] for r in releases)):
            continue
        if stage == "review" and not reviews:
            continue
        if stage == "approval" and not approval:
            continue
        last = next((r["runs"][stage] for r in releases if stage in r["runs"]), None)
        stages.append({"name": stage, "pos": len(stages) + 1, "kind": kind, "run": cmd, "workflow": None,
                       "env": name if stage == "deploy" else None, "transition": transitions.plain(gate),
                       "tier": "prod" if stage in ("deploy", "watch") else None,  # #6137: the target is production
                       "junit": None, "last_run": last})
    live = done[0] if done else None
    env = {"name": name, "target": name, "url": None, "notes": t["description"],
           "rev": (live["cut_rev"] or f"deploy #{live['id']}") if live else None,
           "since": live["closed_at"] if live else None,
           "history": [{"kind": "deployed", "at": d["closed_at"], "run_id": None,
                        "rev": d["cut_rev"] or f"deploy #{d['id']}"} for d in done[:5]]}
    project = conn.execute("SELECT name FROM projects WHERE target=? AND archived=0 AND name<>? ORDER BY rank, id "
                           "LIMIT 1", (name, f"deploy-{name}")).fetchone()  # not the deploy project of the target
    return {"name": name, "project": project["name"] if project else None, "default": True, "stages": stages,
            "envs": [env], "releases": [{"deploy": r["deploy"], "status": r["status"],
                                         "version": next(iter(r["runs"].values()))["version"],
                                         "stages": {k: {"state": v["state"], "item": v["item"]}
                                                    for k, v in r["runs"].items()}} for r in releases]}


def default_list(conn):
    """The targets that no pipeline covers yet (no pipeline environment names them), as default pipelines."""
    covered = {r["target"] for r in conn.execute("SELECT DISTINCT target FROM pipeline_envs WHERE target IS NOT NULL "
                                                 "AND removed_at IS NULL")}
    return [{"name": t["name"], "project": None, "default": True} for t in _targets(conn) if t["name"] not in covered]
