"""Each deploy target of today as a default pipeline (#6115), computed from the records MaximizePM keeps now.

Nothing is stored and nothing that works today changes: a read builds the pipeline of a target from its deploy items
(one release each), their review items, the person's approval items (release_approval person), the hook runs
(release-cut, deploy) and the runs of the target's checks after each release (the watch). So toolscaled-prod and
maximizepm-github show in the pipeline view before they move to a pipeline of their own.

The stages, left to right, as a target has them:
    holding  (holding) the commits that wait for the next release; the release fixes the release's list (cut_at)
    build    (build)   the release-cut hook, when the target has one or ran one
    review   (review)  the review item, when the target reviews its releases
    approval (approve) the person's yes, when release_approval is person
    deploy   (deploy)  the deploy item, with the deploy hook's runs as its steps; its environment is the target
    watch    (check)   the target's checks after the release, until its last watch time (24h with none)

The result has the form of store.show (name, project, stages with last_run, envs) and of store.versions (releases),
with "default": true.

The holding stage (#6140; mark 2026-11-18: "a holding stage at the beginning that shows the commits, releases/ review
reports so we can see whats been reviewed, see the code changes"; "cut is not the right thing to call it. its more
like its the actual release"): the first stage of every pipeline, computed, not stored (holding). It lists the commits
of the project's folder since the last version that left it: for a pipeline, the newest version that reached its first
stage; for a deploy target, the pin of its last done release (or the last version tag). Each commit has its tasks
(#<id> in its message), the session that wrote it (its Claude-Session link), its changes, and its review: passed in or
in the review of a release that covers it (a cut release of the target), approved or sent back in the Changes view of
its task, else not reviewed. The transition after holding is the release: release() sends the commits on, a pipeline's
with maxpm pipeline start, a target's with its release cut (core.target_cut), pinned at the last commit it takes.
"""

import re
import threading
import time
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
    # Holding: the release's items wait there until its release (its cut, #6140) sends them on.
    out["holding"] = _run("holding", "passed" if dep["cut_at"] else "cancelled" if dep["status"] == "dropped"
                          else "queued", dep["created_at"], dep["cut_at"], item=dep["id"])
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
    shape = [("holding", "holding", None, "auto"), ("build", "build", hook.get("release-cut"), "auto"),
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
        # The arrow after holding is the release (#6140): the manager or the cadence makes it.
        arrow = transitions.plain("release", core.parse_cadence(t["cadence"])[0] or None) if len(stages) == 1 else \
            transitions.plain(gate)
        stages.append({"name": stage, "pos": len(stages) + 1, "kind": kind, "run": cmd, "workflow": None,
                       "env": name if stage == "deploy" else None, "transition": arrow,
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


# ---------------------------------------------------------------- the holding stage (#6140)

HOLD_MAX = 100   # the commits that holding lists (the oldest first: the release takes them in order)
HOLD_TTL = 20.0  # seconds: a pipeline's holding is read again after this (the Deploy tab asks every 3s; p147)
_HOLD = {}       # pipeline name -> (time.monotonic() of the read, the result)
_TRAILER = re.compile(core.SESSION_LINK)


def _git(root, *args):
    from .. import review
    return review.git(root, *args)


def _root_of(conn, project):
    from .. import review
    r = conn.execute("SELECT path FROM projects WHERE name=?", (project,)).fetchone() if project else None
    return review.repo_root(r["path"]) if r and r["path"] else None


def _where(conn, name):
    """(pipeline row or None, target name or None, project name): what holding reads for a pipeline or a target."""
    p = conn.execute("SELECT * FROM pipelines WHERE name=?", (name,)).fetchone()
    if p:
        env = conn.execute("SELECT target FROM pipeline_envs WHERE pipeline_id=? AND target IS NOT NULL AND removed_at "
                           "IS NULL ORDER BY id LIMIT 1", (p["id"],)).fetchone()
        proj = conn.execute("SELECT target FROM projects WHERE name=?", (p["project"],)).fetchone() if p["project"] else None
        return p, (env["target"] if env else None) or (proj["target"] if proj else None), p["project"]
    t = _target(conn, name)
    project = conn.execute("SELECT name FROM projects WHERE target=? AND archived=0 AND path IS NOT NULL AND path<>'' "
                           "ORDER BY rank, id LIMIT 1", (t["name"],)).fetchone()
    return None, t["name"], project["name"] if project else None


def _ancestor(root, a, b):
    return _git(root, "merge-base", "--is-ancestor", a, b) is not None


def _base(conn, root, p, target, head):
    """(rev, label): the last version that left holding. A pipeline: the newest version that reached its first stage.
    A target: the pin of its last done release, else the last version tag. None when there is none (an empty start)."""
    if p is not None:
        r = conn.execute("SELECT v.rev FROM stage_runs r JOIN pipeline_versions v ON v.id=r.version_id JOIN "
                         "pipeline_stages s ON s.id=r.stage_id WHERE r.pipeline_id=? AND s.removed_at IS NULL AND "
                         "s.pos=(SELECT MIN(pos) FROM pipeline_stages WHERE pipeline_id=? AND removed_at IS NULL) "
                         "ORDER BY r.id DESC LIMIT 1", (p["id"], p["id"])).fetchone()
        if r and _ancestor(root, r["rev"], head):
            return r["rev"], "the last version that left holding"
        return None, None
    d = conn.execute("SELECT id, cut_rev FROM items WHERE kind='deploy' AND target=? AND status='done' AND cut_rev IS "
                     "NOT NULL ORDER BY closed_at DESC LIMIT 1", (target,)).fetchone()
    if d and _ancestor(root, d["cut_rev"], head):
        return d["cut_rev"], f"the pin of release #{d['id']}"
    tag = (_git(root, "describe", "--tags", "--abbrev=0", head) or "").strip()
    return (tag, f"the tag {tag}") if tag else (None, None)


def _reviews(conn, root, target, base):
    """{sha: (state, text, deploy id)} for the commits that a cut release of the target covers and that is not done:
    passed in its review, in its review, sent back, or in it with no review."""
    from .. import report
    out = {}
    if not target:
        return out
    for d in conn.execute("SELECT id, cut_rev FROM items WHERE kind='deploy' AND target=? AND cut_at IS NOT NULL AND "
                          "status NOT IN ('done','dropped') AND cut_rev IS NOT NULL ORDER BY id", (target,)).fetchall():
        span = f"{base}..{d['cut_rev']}" if base else d["cut_rev"]
        shas = (_git(root, "rev-list", "-n", str(HOLD_MAX * 2), span) or "").split()
        rv = report._review(conn, d["id"])
        if rv and rv["status"] == "done":
            val = ("ok", f"passed in review #{rv['id']}", d["id"])
        elif rv and rv["status"] in core.OPEN_STATES:
            val = ("run", f"in review #{rv['id']}" + (f" ({rv['assignee']})" if rv["assignee"] else ""), d["id"])
        else:
            val = ("wait", f"in release #{d['id']}, which has no review", d["id"])
        for s in shas:
            out.setdefault(s, val)
    return out


def _commits(conn, root, base, head):
    """The commits from base to head, oldest first, each with its tasks, the session that wrote it and its changes."""
    span = f"{base}..{head}" if base else head
    text = _git(root, "log", "--no-merges", "-n", str(HOLD_MAX), "--shortstat",
                "--format=%x1e%H%x00%s%x00%an%x00%b%x00", span) or ""
    out = []
    for rec in text.split("\x1e")[1:]:
        parts = rec.split("\x00")
        if len(parts) < 5:
            continue
        sha, subject, author, body, stat = parts[0].strip(), parts[1], parts[2], parts[3], parts[4]
        add = re.search(r"(\d+) insertion", stat)
        rem = re.search(r"(\d+) deletion", stat)
        out.append({"sha": sha, "subject": subject, "author": author,
                    "tasks": sorted({int(n) for n in re.findall(r"#(\d{1,7})\b", subject + "\n" + body)}),
                    "links": _TRAILER.findall(body), "added": int(add.group(1)) if add else 0,
                    "removed": int(rem.group(1)) if rem else 0})
    return out[::-1]


_READING = set()  # the pipelines whose holding a thread reads now
_LOCK = threading.Lock()


def holding_soon(conn, name):
    """The holding of a pipeline for a read that must stay fast (the Deploy list, the stage cards; p147): the last
    one read, at once, and a new read in a thread of its own when it is older than HOLD_TTL. With none read yet: a
    holding with "reading": true and no commits."""
    hit = _HOLD.get(name)
    if hit and time.monotonic() - hit[0] < HOLD_TTL:
        return hit[1]
    with _LOCK:
        start = name not in _READING
        _READING.add(name)
    if start:
        def work():
            try:
                c = core.connect()
                try:
                    holding(c, name, fresh=True)
                finally:
                    c.close()
            except Exception:  # noqa: BLE001 - the next read tries again; the cards show the last holding
                pass
            finally:
                with _LOCK:
                    _READING.discard(name)
        if SYNC_HOLD["on"]:
            work()
            return _HOLD[name][1] if name in _HOLD else holding(conn, name)
        threading.Thread(target=work, daemon=True, name=f"maxpm-holding-{name}").start()
    if hit:
        return hit[1]
    return {"pipeline": name, "reading": True, "folder": None, "commits": [], "added": 0, "removed": 0,
            "counts": {"reviewed": 0, "in_review": 0, "not_reviewed": 0}, "base_label": None, "upto": None}


SYNC_HOLD = {"on": False}  # tests: holding_soon reads in the caller's thread


def holding(conn, name, fresh=False):
    """The holding stage of a pipeline or a deploy target: {pipeline, target, folder, base, base_label, head,
    head_label, commits: [{sha, subject, tasks: [{id, title, status}], session, added, removed, review: {state, text,
    release}}], counts {reviewed, in_review, not_reviewed}, added, removed, upto (the last commit of the reviewed run
    from the oldest), release: {mode, text, can, why}, report (a release whose report opens)}."""
    hit = _HOLD.get(name)
    if hit and not fresh and time.monotonic() - hit[0] < HOLD_TTL:
        return hit[1]
    from .. import review
    p, target, project = _where(conn, name)
    root = _root_of(conn, project)
    out = {"pipeline": name, "target": target, "project": project, "folder": root, "default": p is None,
           "base": None, "base_label": None, "head": None, "head_label": None, "commits": [], "added": 0,
           "removed": 0, "counts": {"reviewed": 0, "in_review": 0, "not_reviewed": 0}, "upto": None, "report": None}
    head, head_label = review._branch_head(root) if root else (None, None)
    if root and head:
        head_sha = (_git(root, "rev-parse", head + "^{commit}") or "").strip()
        base, base_label = _base(conn, root, p, target, head)
        rows = _commits(conn, root, base, head)
        found = _reviews(conn, root, target, base)
        ids = sorted({t for c in rows for t in c["tasks"]})
        items = {r["id"]: r for r in conn.execute(
            f"SELECT id, title, short, status FROM items WHERE id IN ({','.join('?' * len(ids)) or 'NULL'})", ids)}
        links = sorted({x for c in rows for x in c["links"]})
        who = {r["session_url"]: r["name"] for r in conn.execute(
            f"SELECT name, session_url FROM agents WHERE session_url IN ({','.join('?' * len(links)) or 'NULL'})",
            links)}
        upto, open_run = None, True
        for c in rows:
            tasks = [{"id": i, "title": items[i]["short"] or items[i]["title"], "status": items[i]["status"]}
                     for i in c["tasks"] if i in items]
            rv = found.get(c["sha"])
            if rv is None:
                marks = {review.state_of(conn, f"item:{t['id']}", "-") for t in tasks}
                rv = ("bad", "changes requested in Changes", None) if "changes" in marks else \
                    ("ok", "approved in Changes", None) if tasks and marks == {"approved"} else \
                    ("wait", "not reviewed", None)
            state, text, dep = rv
            key = "reviewed" if state == "ok" else "in_review" if state == "run" else "not_reviewed"
            out["counts"][key] += 1
            if state == "ok" and open_run:
                upto = c["sha"]
            else:
                open_run = False
            session = next((who[x] for x in c["links"] if x in who), None)
            out["commits"].append({"sha": c["sha"], "subject": c["subject"], "tasks": tasks,
                                   "session": session or c["author"], "by_session": bool(session),
                                   "added": c["added"], "removed": c["removed"],
                                   "review": {"state": state, "text": text, "release": dep}})
            out["report"] = out["report"] or dep
        out.update(base=base, base_label=base_label, head=head_sha or head, head_label=head_label, upto=upto,
                   added=sum(c["added"] for c in rows), removed=sum(c["removed"] for c in rows))
    out["release"] = _release_rule(conn, p, target)
    _HOLD[name] = (time.monotonic(), out)
    return out


def _release_rule(conn, p, target):
    """The transition after holding: {mode, text, can, why}. A pipeline: the transition into its first stage. A target:
    its release plan (the manager, or the cadence)."""
    if p is not None:
        s = conn.execute("SELECT id, name FROM pipeline_stages WHERE pipeline_id=? AND removed_at IS NULL ORDER BY pos, id "
                         "LIMIT 1", (p["id"],)).fetchone()
        if not s:
            return {"mode": None, "text": "the pipeline has no stage yet", "can": False,
                    "why": f"add a stage first: maxpm pipeline stage {p['name']} <stage> --kind build --run \"<command>\""}
        t = transitions.get(conn, s["id"])
        # Nothing leaves holding by itself: a release sends it on. The transition into the first stage holds it after.
        text = "a person or the manager releases (Release, or maxpm pipeline release)" + (
            "" if t["mode"] == "auto" else f"; then the transition into {s['name']}: "
                                           + transitions.mode_text(t["mode"], t["mode_arg"]))
        return {"mode": "release", "text": text, "can": True, "why": None}
    plan = core.release_plan(conn, target)
    cad = core.parse_cadence(core._target(conn, target)["cadence"])[0]
    why = None
    if plan["deploy"] is None or not plan["ships"]:
        why = f"nothing is collected for {target} yet: a task goes out with maxpm done <id> --ship"
    elif plan["waits"]:
        why = plan["text"]
    return {"mode": "release", "text": transitions.mode_text("release", cad or None), "can": why is None, "why": why,
            "plan": plan["text"] or None, "ships": [s["id"] for s in plan["ships"] or []]}


def release(conn, name, upto=None, everything=False, actor=None):
    """Release from holding (the arrow after it): the commits in order up to upto (a commit that holding lists), or,
    with everything, all of them; by default the reviewed ones up to the first one not reviewed. A pipeline: the
    version starts at its first stage (runner.start). A target: its release is cut, pinned at that commit
    (core.target_cut). Only a person or the manager releases."""
    why = core._lead_refusal(conn, actor, "releases from holding")
    if why:
        raise core.RiverError(why)
    h = holding(conn, name, fresh=True)
    if not h["commits"]:
        raise core.RiverError(f"nothing waits in holding of {name}" + ("" if h["folder"] else
                              ": its project has no git folder (maxpm project path <project> <folder>)"))
    shas = [c["sha"] for c in h["commits"]]
    if everything:
        rev = shas[-1]
    elif upto:
        rev = next((s for s in shas if s.startswith(upto)), None)
        if not rev:
            raise core.RiverError(f"{upto} is not a commit in holding of {name}: maxpm pipeline holding {name}")
    else:
        rev = h["upto"]
        if not rev:
            raise core.RiverError(f"no commit in holding of {name} is reviewed from the oldest on; release all "
                                  f"{len(shas)}: maxpm pipeline release {name} --all, or one: --rev <commit>")
    n = shas.index(rev) + 1
    _HOLD.pop(name, None)
    if not h["default"]:
        from . import runner
        out = runner.start(conn, name, rev=rev, actor=actor)
        return {**out, "released": n, "rev": rev}
    out = core.target_cut(conn, h["target"], rev=rev, actor=actor)
    return {"pipeline": name, "target": h["target"], "released": n, "rev": rev, "release": out["cut"]["id"]}


def holding_diff(conn, name, sha=None):
    """The Changes view of holding (GET /api/diff?holding=<name>[&sha=<commit>]): one commit, or all the code from the
    base to the head, in the form of review.item_diff."""
    from .. import review
    h = holding(conn, name)
    if not h["folder"] or not h["commits"]:
        raise core.RiverError(f"nothing waits in holding of {name}")
    budget = {"left": review.MAX_LINES}
    if sha:
        c = next((c for c in h["commits"] if c["sha"].startswith(sha)), None)
        if not c:
            raise core.RiverError(f"{sha} is not a commit in holding of {name}")
        text = _git(h["folder"], "show", "-M", "--no-color", "--no-ext-diff", "--format=", c["sha"]) or ""
        block = {"sha": c["sha"], "subject": c["subject"], "item": (c["tasks"] or [{}])[0].get("id"),
                 "root": h["folder"], "files": review.parse(text, budget)}
        title = c["subject"]
    else:
        span = f"{h['base']}..{h['head']}" if h["base"] else f"{h['commits'][0]['sha']}^..{h['head']}"
        text = _git(h["folder"], "diff", "-M", "--no-color", "--no-ext-diff", span) or ""
        block = {"sha": h["head"], "subject": f"all {len(h['commits'])} commits in holding",
                 "item": None, "root": h["folder"], "files": review.parse(text, budget)}
        title = f"Holding of {name}: {len(h['commits'])} commits since {h['base_label'] or 'the start'}"
    return {"item": None, "kind": "holding", "title": title, "items": [], "commits": [block],
            "cut": budget["left"] <= 0, "folder": h["folder"], "holding": name, "review_at": None, "waits": False,
            "state": None}


def holding_stage(h):
    """Holding as the first stage card of a pipeline (the routes put it in front, or in the place of a target's own
    holding stage): its last_run is the summary."""
    n = len(h["commits"])
    c = h["counts"]
    return {"name": "holding", "pos": 0, "kind": "holding", "run": None, "workflow": None, "env": None,
            "transition": None, "holding": {"commits": n, **c, "added": h["added"], "removed": h["removed"],
                                            "base_label": h["base_label"], "folder": bool(h["folder"]),
                                            "reading": bool(h.get("reading"))},
            "last_run": {"state": "queued" if n else "none", "version": None, "started_at": None, "ended_at": None,
                         "id": None, "steps": {}}}
