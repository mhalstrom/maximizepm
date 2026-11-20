"""Each deploy target of today as a default pipeline (#6115), computed from the records MaximizePM keeps now.

A target owns its release flow. A read builds it from its deploy items (one release each), review and approval items,
hook runs, and the checks after each release. MaximizePM starts the hook on schedule or through Release now.
An attached GitHub source reports tests of the release branch as checks. Test results leave release controls and the
live version to the target. When the deploy hook starts an external build, its reported jobs belong to that release.

The stages, left to right, as a target has them:
    holding  (holding) the commits that wait for the next release; the release fixes the release's list (cut_at)
    build    (build)   the release-cut hook, when the target has one or ran one
    review   (review)  the review item, when the target reviews its releases
    approval (approve) the person's yes, when release_approval is person
    deploy   (deploy)  the deploy item, with the deploy hook's runs as its steps; its environment is the target
    watch    (check)   the target's checks after the release, until its last watch time (24h with none)
With release_approval draft (#6213, maximizepm-github) the person's yes comes after the deploy, and the stages are one
line: holding, deploy (the deploy hook's runs until it prepared the release and exited core.HOOK_YES: test, commit,
push main, the draft build of the downloads), release tag (#6516, mark 2026-11-20: 'still missing the release tag
stage'): the person's yes on the draft is the arrow into it, and its runs after the yes push the tag and publish the
GitHub release; its state says waits for your yes (with the draft and the yes item), tagged, published or failed),
watch. A target's stages never branch (mark 2026-11-19: 'this should not be a branching deploy workflow').

The result has the form of store.show (name, project, stages with last_run, envs) and of store.versions (releases),
with "default": true.

The holding stage (#6140; mark 2026-11-18: "a holding stage at the beginning that shows the commits, releases/ review
reports so we can see whats been reviewed, see the code changes"; "cut is not the right thing to call it. its more
like its the actual release"): the first stage of every pipeline, computed, not stored (holding). It lists the commits
of the project's folder since the last version that left it: for a pipeline, the newest version that reached its first
stage; for a deploy target, the pin of its last done release (or the last version tag). Each commit has its tasks
(#<id> in its message), the session that wrote it (its Claude-Session link), its changes, and its review: passed in or
in the review of a release that covers it (a cut release of the target), approved or sent back in the Changes view of
its task, else not reviewed. Review counts apply to flows that have review. The transition after holding is the
release: release() sends the commits on, a pipeline's
with maxpm pipeline start, a target's with its release cut (core.target_cut), pinned at the last commit it takes.
"""

import json
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
    elif _prepared(h):
        state = "passed"
    else:
        state = "passed" if h["exit_code"] == 0 and not h["timed_out"] else "failed"
    return {"name": h["event"], "state": state, "started_at": h["started_at"], "ended_at": h["ended_at"],
            "seconds": h["seconds"], "output": h["output"], "note": f"exit {h['exit_code']}" if state == "failed"
            else "prepared: waits for a person's yes" if _prepared(h) else None}


def _approved(h):
    """A run of the deploy hook after the person's yes on the prepared release (#6213, core._run_approved)."""
    return core._run_approved(dict(h))


def _prepared(h):
    """A run of the deploy hook that prepared the release and asked for the person's yes (exit core.HOOK_YES)."""
    return (h["event"] == "deploy" and h["ended_at"] is not None and h["exit_code"] == core.HOOK_YES
            and not h["timed_out"] and not _approved(h))


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


WATCH_FAILS = 5  # the newest failed runs that a check's row of the watch names; the rest open on request (watch_runs)


def _check_state(status):
    return "failed" if status in ("warning", "critical") else "passed" if status == "ok" else "skipped"


def _watch(conn, t, dep, next_at, checks):
    """The watch of one release: the runs of the target's checks from its deploy until its last watch time (or
    the next release, when sooner), as one step for each check (#6181: a check that runs every few minutes gave
    hundreds of steps, one row each). A check's step has its last result in the watch, how many runs and how many
    failed, and the newest failed runs with their summary; watch_runs lists every run of one check on request."""
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
    by = {c["id"]: [] for c in checks}
    for r in rows:
        by[r["check_id"]].append(r)
    steps = []
    for c in checks:
        runs = by[c["id"]]
        fails = [r for r in runs if _check_state(r["status"]) == "failed"]
        last = runs[-1] if runs else None
        steps.append({"name": f"{c['scope']}/{c['name']}", "check": c["id"],
                      "state": _check_state(last["status"]) if last else "skipped",
                      "started_at": runs[0]["started_at"] if runs else None,
                      "ended_at": (last["ended_at"] or last["started_at"]) if last else None, "seconds": None,
                      "output": None, "note": (last["summary"] if last else "no run in the watch"),
                      "runs": len(runs), "failed": len(fails),
                      "fails": [{"at": r["started_at"], "summary": r["summary"]} for r in fails[-WATCH_FAILS:][::-1]]})
    steps.sort(key=lambda s: (not s["failed"], s["name"]))  # the checks that failed first
    bad = [s for s in steps if s["failed"]]
    # #6505 (mark 2026-11-20: 'these types of issues should not block deployment'): a check that fails in the watch
    # never fails the release. The watch runs until its end and then passed; each failed check is a note on the
    # Deploy page (errors) and its own fault item (ops_faults), not a failed stage, so no stage-failed rule acts.
    if core.now() < end:
        state = "running"
    else:
        state = "passed" if rows else "skipped"
    errors = None
    if bad:  # each check that failed in the watch, and whether its newest run (also after the watch) passes now
        now = {r["check_id"]: r["status"] for r in conn.execute(
            f"SELECT o.check_id, o.status FROM ops_runs o WHERE o.id IN (SELECT MAX(id) FROM ops_runs WHERE check_id "
            f"IN ({','.join('?' * len(bad))}) AND ended_at IS NOT NULL GROUP BY check_id)", [s["check"] for s in bad])}
        for s in bad:
            s["now"] = _check_state(now.get(s["check"]))
        errors = f"A note, not a failure of the release: {sum(s['failed'] for s in bad)} failed check run(s): " + \
            ", ".join(s["name"] + (" (passes now)" if s["now"] == "passed" else " (fails now)" if s["now"] == "failed"
                                   else "") for s in bad) + "; a check in fault has its own fix item"
    out = _run("watch", state, dep["closed_at"], core.iso(end) if state != "running" else None, steps=steps,
               errors=errors)
    out["watch_end"] = core.iso(end)  # the end of the window that watch_runs reads
    return out


WATCH_RUNS_MAX = 2000  # the runs of one check that watch_runs returns (a check every minute for 24h: 1440)


def watch_runs(conn, check_id, start, end):
    """Every run of one check in a release's watch, newest first (#6181): the list that a click on the check's row
    opens. start and end are the watch's started_at and watch_end."""
    c = conn.execute("SELECT id, scope, name FROM checks WHERE id=?", (check_id,)).fetchone()
    if not c:
        raise core.RiverError(f"no check with id {check_id}; the list: maxpm ops list")
    rows = conn.execute("SELECT started_at, ended_at, status, summary FROM ops_runs WHERE check_id=? AND started_at>=? "
                        "AND started_at<=? ORDER BY id DESC LIMIT ?", (check_id, start, end, WATCH_RUNS_MAX)).fetchall()
    return {"check": f"{c['scope']}/{c['name']}", "runs": [{"at": r["started_at"], "ended_at": r["ended_at"],
                                                             "state": _check_state(r["status"]),
                                                             "summary": r["summary"]} for r in rows]}


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
    if approval == "person":
        yes = core._approvals(conn, dep["id"])
        if yes:
            a = yes[0]
            r = out["approval"] = _run("approval", _ITEM_STATE.get(a["status"], "queued"), a["created_at"],
                                       a["closed_at"], item=a["id"])
            r["yes"] = a["id"] if r["state"] == "queued" else None  # the person's item while it waits for the yes
    deploy_hooks = [h for h in hooks if h["event"] in ("deploy", "deployed")]
    after = []
    if approval == "draft":  # #6213: the runs after the person's yes, and the deployed hook, are the stage publish
        after = [h for h in deploy_hooks if _approved(h) or h["event"] == "deployed"]
        deploy_hooks = [h for h in deploy_hooks if h not in after]
    deploy_steps = [_hook_run(h) for h in deploy_hooks]
    done = dep["status"] == "done"
    st = _ITEM_STATE.get(dep["status"], "queued")
    runs = [s for s in deploy_steps if s["name"] == "deploy"]
    ended = dep["closed_at"] if done else None
    last = next((h for h in reversed(deploy_hooks) if h["event"] == "deploy"), None)
    if last is not None and _prepared(last):  # #6213: it prepared the release; the yes and publish come after it
        st, ended = "passed", last["ended_at"]
    elif st == "queued" and runs and runs[-1]["state"] in ("running", "failed"):
        st = runs[-1]["state"]
    out["deploy"] = _run("deploy", st, dep["claimed_at"] or (runs[0]["started_at"] if runs else None), ended,
                         steps=_steps(conn, deploy_hooks), item=dep["id"],
                         errors=runs[-1]["note"] if runs and runs[-1]["state"] == "failed" else None)
    if approval == "draft":
        tag = _tag_run(conn, dep, hooks, after)
        if tag:
            out[TAG] = tag
    w = _watch(conn, t, dep, next_at, checks) if checks is not None else None
    if w:
        out["watch"] = w
    label = rev or f"deploy #{dep['id']}"
    for r in out.values():
        r["version"] = label
    return out


TAG = "release tag"  # #6516: the stage after the person's yes on a prepared release (release_approval draft)
_DRAFT = re.compile(r"the draft: (https?://\S+)")


def _tag_run(conn, dep, hooks, after):
    """The release tag stage of one release (release_approval draft, #6213, #6516), or None before the deploy hook
    prepared it. Its arrow is the person's yes on the draft: while that waits, the run is queued with the yes item
    (yes) and the draft's link (draft, from the last lines of the prepared run). After the yes, the deploy hook's runs
    push the tag and publish the release: word says tagged when the tag step passed and the publish has not, published
    when the release is out, failed with the reason."""
    prepared = [h for h in hooks if _prepared(h)]
    pruns = [h for h in after if h["event"] == "deploy"]
    # The newest run of the deploy hook: a preparation after a failed tag asks for a new yes (the release starts again).
    last = next((h for h in reversed(hooks) if h["event"] == "deploy"), None)
    again = last is not None and _prepared(last)
    if not prepared and not pruns:
        return None
    m = None
    for h in reversed(prepared):
        m = _DRAFT.search(h["output"] or "")
        if m:
            break
    draft = m.group(1) if m else None
    yes = core._approvals(conn, dep["id"])
    a = yes[0] if yes else None
    steps = _steps(conn, after)
    passed = {s["name"] for s in steps if s["state"] == "passed"}
    word = note = None
    waits = None
    if dep["status"] == "done":
        st, word = "passed", "published"
        started, ended = (_hook_run(pruns[0])["started_at"] if pruns else dep["closed_at"]), dep["closed_at"]
    elif dep["status"] == "dropped":
        st, started, ended = "cancelled", None, dep["closed_at"]
    elif pruns and (a is None or a["status"] == "done") and not again:
        r = _hook_run(pruns[-1])
        st, started, ended = r["state"], _hook_run(pruns[0])["started_at"], r["ended_at"] if r["state"] != "running" \
            else None
        if st == "queued":
            word = "approved: the tag goes out next"
        elif "publish" in passed or "published" in passed:
            word = "published" if st != "failed" else None
        elif "tag" in passed:
            word = "tagged"
        note = r["note"] if st == "failed" else None
    elif a is not None and a["status"] in core.OPEN_STATES:
        st, word, started, ended, waits = "queued", "waits for your yes", a["created_at"], None, a["id"]
    elif a is not None and a["status"] == "done":
        st, word, started, ended = "queued", "approved: the tag goes out next", a["closed_at"], None
    else:  # the yes was declined or dropped: the release waits for a new preparation
        st, started, ended = "cancelled", a["created_at"] if a else None, a["closed_at"] if a else None
        note = "the person declined the draft" if a else None
    out = _run(TAG, st, started, ended, steps=steps if pruns else [], item=waits or dep["id"], errors=note)
    out.update(word=word, yes=waits, approval=a["id"] if a else None, draft=draft)
    return out


KINDS = {"holding": "holding", "build": "build", "review": "review", "approval": "approve", "deploy": "deploy",
         TAG: "deploy", "watch": "check"}


def _deploys(conn, name, limit=RELEASES):
    return conn.execute("SELECT * FROM items WHERE kind='deploy' AND target=? ORDER BY id DESC LIMIT ?",
                        (name, limit)).fetchall()


def _has_reviews(conn, name, deps):
    return core._target_reviews(conn, name) or any(
        conn.execute("SELECT 1 FROM deps d JOIN items i ON i.id=d.blocked_by WHERE d.item_id=? AND i.kind='review'",
                     (d["id"],)).fetchone() for d in deps)


def _has_build(conn, name, deps):
    """True when the target has a release-cut hook, or one of these releases ran one."""
    if conn.execute("SELECT 1 FROM hooks h JOIN targets x ON x.id=h.target_id WHERE x.name=? AND h.event='release-cut'",
                    (name,)).fetchone():
        return True
    ids = [d["id"] for d in deps]
    return bool(ids) and conn.execute(f"SELECT 1 FROM hook_runs WHERE event='release-cut' AND deploy_id IN "
                                      f"({','.join('?' * len(ids))}) LIMIT 1", ids).fetchone() is not None


def _approval_mode(conn, name, deps):
    mode = core.release_approval(conn, name)["approval"]
    if mode or not deps:
        return mode
    ids = [d["id"] for d in deps]
    prepared = conn.execute(
        f"SELECT 1 FROM hook_runs WHERE deploy_id IN ({','.join('?' * len(ids))}) AND event='deploy' "
        "AND ended_at IS NOT NULL AND exit_code=? AND NOT COALESCE(timed_out, 0) LIMIT 1",
        (*ids, core.HOOK_YES)).fetchone()
    return "draft" if prepared else ""


def shape(conn, name, deps=None):
    """The stages of target name, left to right, holding first: the names that its default pipeline shows (#6176:
    the stored pipeline of its controls has the same ones, less holding)."""
    deps = _deploys(conn, name) if deps is None else deps
    out = ["holding"]
    if _has_build(conn, name, deps):
        out.append("build")
    if _has_reviews(conn, name, deps):
        out.append("review")
    gate = _approval_mode(conn, name, deps)
    if gate == "person":
        out.append("approval")
    if gate == "draft":  # #6213: the deploy hook prepares the release; after the person's yes it tags and publishes it
        return out + ["deploy", TAG, "watch"]
    return out + ["deploy", "watch"]


def release_runs(conn, name, deps=None, watch_history=True):
    """The releases of target name, newest first: [{deploy, status, runs: {stage: run}}] (_release)."""
    t = _target(conn, name)
    deps = _deploys(conn, name) if deps is None else deps
    reviews = _has_reviews(conn, name, deps)
    approval = _approval_mode(conn, name, deps)
    checks = _checks(conn, name)
    done = [d for d in deps if d["status"] == "done" and d["closed_at"]]
    out = []
    newest_watch = done[0]["id"] if done else None
    for d in deps:
        later = [x["closed_at"] for x in done if x["closed_at"] > (d["closed_at"] or "")]
        watch_checks = checks if watch_history or d["id"] == newest_watch else None
        out.append({"deploy": d["id"], "status": d["status"], "closed_at": d["closed_at"], "cut_rev": d["cut_rev"],
                    "runs": _release(conn, t, d, min(later) if later else None, reviews, approval, watch_checks)})
    return out


def current_run(releases, stage):
    """The run of a stage that its card shows (#6516, mark 2026-11-20: the deploy card showed 'deploy #6498 cancelled'
    while release #6211 ran): the one that runs, else the newest one not cancelled, else the newest. releases are
    newest first."""
    runs = [r["runs"][stage] for r in releases if stage in r["runs"]]
    return (next((x for x in runs if x["state"] == "running"), None)
            or next((x for x in runs if x["state"] != "cancelled"), None) or (runs[0] if runs else None))


def default_pipeline(conn, name, watch_history=True):
    """The default pipeline of target name, in the form of store.show, with its releases newest first. Its arrows
    have the controls of the target's stored pipeline (#6176, target_flow.py) when it has one, else none set yet."""
    from . import target_flow
    t = _target(conn, name)
    deps = _deploys(conn, name)
    releases = release_runs(conn, name, deps, watch_history=watch_history)
    done = [d for d in deps if d["status"] == "done" and d["closed_at"]]
    hook = {r["event"]: r["command"] for r in conn.execute(
        "SELECT h.event, h.command FROM hooks h JOIN targets x ON x.id=h.target_id WHERE x.name=?", (name,))}
    own = target_flow.row(conn, name)
    rows = {r["name"]: r for r in conn.execute("SELECT * FROM pipeline_stages WHERE pipeline_id=? AND removed_at IS "
                                               "NULL", (own["id"],))} if own else {}
    cache = {}
    stages = []
    names = shape(conn, name, deps)
    # The stage that puts a release in the target: deploy, or the release tag with release_approval draft (#6213: a
    # deploy that waits for the yes is a draft, not live).
    lands = TAG if TAG in names else "deploy"
    for stage in names:
        last = current_run(releases, stage)
        # The arrow after holding is the release (#6140): the manager or the cadence makes it. The arrow into approval,
        # and into the release tag (#6516), is the person's yes; the others are automatic. These modes come from the
        # target's settings; the controls (a block, a delay, disabled, a window, a freeze, the fault rule) from its
        # stored pipeline (#6176).
        mode = ("release", core.parse_cadence(t["cadence"])[0] or None) if len(stages) == 1 else \
            ("yes", None) if stage in ("approval", TAG) else ("auto", None)
        arrow = transitions.plain("auto") if stage == "holding" else \
            target_flow.arrow(conn, own, rows.get(stage), stage, mode, cache)
        stages.append({"name": stage, "pos": len(stages) + 1, "kind": KINDS[stage],
                       "run": hook.get("release-cut") if stage == "build" else hook.get("deploy")
                       if stage in ("deploy", TAG) else None, "workflow": None,
                       "env": name if stage == lands else None, "transition": arrow,
                       # #6137: the target is production
                       "tier": "prod" if stage in ("deploy", TAG, "watch") else None,
                       "junit": None, "last_run": last})
    live = done[0] if done else None
    env = {"name": name, "target": name, "url": None, "notes": t["description"],
           "rev": (live["cut_rev"] or f"deploy #{live['id']}") if live else None,
           "since": live["closed_at"] if live else None,
           "history": [{"kind": "deployed", "at": d["closed_at"], "run_id": None,
                        "rev": d["cut_rev"] or f"deploy #{d['id']}"} for d in done[:5]]}
    project = conn.execute("SELECT name FROM projects WHERE target=? AND archived=0 AND name<>? ORDER BY rank, id "
                           "LIMIT 1", (name, f"deploy-{name}")).fetchone()  # not the deploy project of the target
    checks = release_checks(conn, name)
    return {"name": name, "project": project["name"] if project else None, "default": True, "stages": stages,
            "checks": checks, "latest": latest_release(conn, name, releases, deps, checks),  # #6486
            "controls": True,  # #6176: its arrows, operators, emergency actions, triggers and audit log act
            "held_reason": own["held_reason"] if own else None, "held_by": own["held_by"] if own else None,
            "envs": [env], "releases": [{"deploy": r["deploy"], "status": r["status"],
                                         "version": next(iter(r["runs"].values()))["version"],
                                         "stages": {k: {"state": v["state"], "item": v["item"]}
                                                    for k, v in r["runs"].items()}} for r in releases]}


_GH_RELEASE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/releases/tag/[\w.+-]+")
_TAG = re.compile(r"\bv\d+\.\d+\.\d+\b")


def _release_link(conn, dep_id, checks):
    """The version tag and the GitHub release page of one release (#6486): a release page that the hook printed, else
    the newest tag vX.Y.Z it printed, on the repository of the target's GitHub checks."""
    texts = [r[0] or "" for r in conn.execute("SELECT output FROM hook_runs WHERE deploy_id=? ORDER BY id DESC",
                                              (dep_id,))]
    texts += [r[0] or "" for r in conn.execute(
        "SELECT s.note FROM hook_steps s JOIN hook_runs h ON h.id=s.hook_run WHERE h.deploy_id=? ORDER BY s.id DESC",
        (dep_id,))]
    texts.append((conn.execute("SELECT output FROM items WHERE id=?", (dep_id,)).fetchone() or [""])[0] or "")
    url = next((m.group(0) for x in texts for m in [_GH_RELEASE.search(x)] if m), None)
    tag = url.rsplit("/", 1)[1] if url else None
    if not tag or tag.startswith("untagged-"):  # #6516: a draft has no tag yet; its version is the one the hook printed
        tag = next((m.group(0) for x in texts for m in [_TAG.search(x)] if m), None)
    if tag and not url:
        repo = next((c.get("repo") for c in checks if c.get("repo")), None)
        url = f"https://github.com/{repo}/releases/tag/{tag}" if repo else None
    return tag, url


def latest_release(conn, name, releases, deps, checks):
    """The release that runs now, or the last one that ran (#6486, mark 2026-11-20: 'I can't figure out how to use
    it'): the newest release that is not dropped and has started (a hook ran, it was cut, or it is done), with its
    stage runs and their steps, its fix item while it waits on one, its version tag and the GitHub release page. The
    release card on top of a target's page reads it; None when no release ran yet."""
    by_id = {d["id"]: d for d in deps}
    # #6516: the release whose deploy runs comes first, also when a newer one was cut after it.
    for r in sorted(releases, key=lambda x: (x["runs"].get("deploy") or {}).get("state") != "running"):
        d = by_id.get(r["deploy"])
        if not d or d["status"] == "dropped":
            continue
        dep_run = r["runs"].get("deploy") or {}
        started = d["status"] == "done" or d["cut_at"] or dep_run.get("step_list") or dep_run.get("state") in (
            "running", "failed")
        if not started:
            continue
        fix = conn.execute("SELECT id, status, title FROM items WHERE found_during=? AND title LIKE 'Fix the failed "
                           "deploy%' ORDER BY id DESC LIMIT 1", (d["id"],)).fetchone()
        tag, url = _release_link(conn, d["id"], checks)
        if url and tag and d["status"] == "done" and "/releases/tag/untagged-" in url:
            url = url.rsplit("/", 1)[0] + "/" + tag  # #6516: published, the draft's page is the release of its tag
        return {"deploy": d["id"], "status": d["status"], "closed_at": d["closed_at"], "runs": r["runs"],
                "fix": dict(fix) if fix and fix["status"] not in core.CLOSED_STATES else None,
                "tag": tag, "url": url}
    return None


def default_list(conn):
    """Every deploy target owns its release flow."""
    return [{"name": t["name"], "project": None, "default": True} for t in _targets(conn)]


_CHECK_LINKS = ("FROM pipelines p JOIN pipeline_envs e ON e.pipeline_id=p.id "
                "JOIN targets t ON t.name=e.target "
                "WHERE p.target IS NULL AND p.forge='github' AND e.removed_at IS NULL")


def check_targets(conn, pipeline_id):
    """The release flows that read this pipeline's GitHub tests."""
    return [r[0] for r in conn.execute("SELECT DISTINCT t.name " + _CHECK_LINKS + " AND p.id=? ORDER BY t.name",
                                      (pipeline_id,))]


def check_source_ids(conn):
    return {r[0] for r in conn.execute("SELECT DISTINCT p.id " + _CHECK_LINKS)}


def check_aliases(conn):
    """Old pipeline links open the target when the source serves just one release flow."""
    return dict(conn.execute("SELECT p.name, MIN(t.name) " + _CHECK_LINKS
                             + " GROUP BY p.id HAVING COUNT(DISTINCT t.name)=1"))


def release_checks(conn, target):
    """Read the latest eligible GitHub runs. The source retains all its original run history."""
    from . import plugin, store
    out = []
    sources = conn.execute("SELECT DISTINCT p.* " + _CHECK_LINKS + " AND t.name=? ORDER BY p.name", (target,))
    for p in sources:
        branch = plugin._config(p)["settings"].get("ref") or "main"
        observed = (json.loads(p["plugin_state"] or "null") or {}).get("release_checks") or {}
        for stage in conn.execute("SELECT id, name, workflow FROM pipeline_stages WHERE pipeline_id=? "
                                  "AND removed_at IS NULL AND kind='test' AND workflow IS NOT NULL ORDER BY pos, id",
                                  (p["id"],)):
            keys = observed.get(stage["name"]) or []
            run = None
            if keys:
                marks = ",".join("?" * len(keys))
                run = conn.execute(f"SELECT * FROM stage_runs WHERE stage_id=? AND key IN ({marks}) "
                                   "ORDER BY queued_at DESC, id DESC LIMIT 1", (stage["id"], *keys)).fetchone()
            out.append({"name": stage["name"], "source": "GitHub", "workflow": stage["workflow"],
                        "branch": branch, "repo": p["repo"] if p["forge"] == "github" else None,
                        "last_run": store._run_dict(conn, run), "error": p["poll_error"]})
    return out


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
    p = conn.execute("SELECT * FROM pipelines WHERE name=? AND target IS NULL", (name,)).fetchone()  # #6176: not
    if p:  # the stored row of a target's controls: its holding is the target's
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
    A target: the pin of its newest cut release (#6175: the release left holding at its cut, done or not; toolscaled-prod
    listed the 5 commits of cut release #6128 as queued in holding while they waited at approval), else of its last
    done release, else the last version tag. None when there is none (an empty start)."""
    if p is not None:
        r = conn.execute("SELECT v.rev FROM stage_runs r JOIN pipeline_versions v ON v.id=r.version_id JOIN "
                         "pipeline_stages s ON s.id=r.stage_id WHERE r.pipeline_id=? AND s.removed_at IS NULL AND "
                         "s.pos=(SELECT MIN(pos) FROM pipeline_stages WHERE pipeline_id=? AND removed_at IS NULL) "
                         "ORDER BY r.id DESC LIMIT 1", (p["id"], p["id"])).fetchone()
        if r and _ancestor(root, r["rev"], head):
            return r["rev"], "the last version that left holding"
        return None, None
    # The newest cut releases first (a pin that is not in head, from another branch, is passed over), then the done ones.
    for d in conn.execute("SELECT id, cut_rev FROM items WHERE kind='deploy' AND target=? AND cut_rev IS NOT NULL AND "
                          "cut_at IS NOT NULL AND status<>'dropped' ORDER BY cut_at DESC, id DESC LIMIT 5",
                          (target,)).fetchall():
        if _ancestor(root, d["cut_rev"], head):
            return d["cut_rev"], f"the pin of release #{d['id']}"
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
    review_enabled = p is not None or _has_reviews(conn, target, _deploys(conn, target))
    out = {"pipeline": name, "target": target, "project": project, "folder": root, "default": p is None,
           "base": None, "base_label": None, "head": None, "head_label": None, "commits": [], "added": 0,
           "removed": 0, "review_enabled": review_enabled,
           "counts": {"reviewed": 0, "in_review": 0, "not_reviewed": 0} if review_enabled else {},
           "upto": None, "report": None}
    head, head_label = review._branch_head(root) if root else (None, None)
    if root and head:
        head_sha = (_git(root, "rev-parse", head + "^{commit}") or "").strip()
        base, base_label = _base(conn, root, p, target, head)
        rows = _commits(conn, root, base, head)
        found = _reviews(conn, root, target, base) if review_enabled else {}
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
            status = None
            if review_enabled:
                rv = found.get(c["sha"])
                if rv is None:
                    marks = {review.state_of(conn, f"item:{t['id']}", "-") for t in tasks}
                    rv = ("bad", "changes requested in Changes", None) if "changes" in marks else \
                        ("ok", "approved in Changes", None) if tasks and marks == {"approved"} else \
                        ("wait", "not reviewed", None)
                state, text, dep = rv
                key = "reviewed" if state == "ok" else "in_review" if state == "run" else "not_reviewed"
                out["counts"][key] += 1
                open_run = open_run and state == "ok"
                if open_run:
                    upto = c["sha"]
                status = {"state": state, "text": text, "release": dep}
                out["report"] = out["report"] or dep
            session = next((who[x] for x in c["links"] if x in who), None)
            out["commits"].append({"sha": c["sha"], "subject": c["subject"], "tasks": tasks,
                                   "session": session or c["author"], "by_session": bool(session),
                                   "added": c["added"], "removed": c["removed"],
                                   "review": status})
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
    elif not h["review_enabled"]:
        rev = shas[-1]
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
    # #6189: the release from holding is a control at the top of the pipeline's page: whether it can release now, how
    # many commits the reviewed ones are (up to the first one not reviewed), and the commits each button sends.
    rel = h.get("release") or {}
    k = next((i + 1 for i, x in enumerate(h["commits"]) if x["sha"] == h.get("upto")), 0) if h.get("upto") else 0
    return {"name": "holding", "pos": 0, "kind": "holding", "run": None, "workflow": None, "env": None,
            "transition": None, "holding": {"commits": n, **c, "added": h["added"], "removed": h["removed"],
                                            "review_enabled": h.get("review_enabled", True),
                                            "base_label": h["base_label"], "folder": bool(h["folder"]),
                                            "reading": bool(h.get("reading")),
                                            "release": {"can": bool(rel.get("can")), "reviewed": k,
                                                        "upto": h.get("upto"),
                                                        "last": h["commits"][-1]["sha"] if n else None}},
            "last_run": {"state": "queued" if n else "none", "version": None, "started_at": None, "ended_at": None,
                         "id": None, "steps": {}}}
