"""The controls of a deploy target's pipeline (#6176): the Deploy tab's transitions, operators, emergency actions,
override, move back, triggers and audit log on a target that has no pipeline of its own (toolscaled-prod).

mark's goal: a person follows and controls every release in one Deploy tab. A target's pipeline is computed from its
release items (targets.py); its controls need rows, so the target gets one row of pipelines (name = the target,
pipelines.target set) the first time a control names it (store._pipeline calls ensure), with the stages of the target
less holding. That row holds what the other pipelines hold: the transitions into its stages, its operators and
'agents may be operators', its trigger rules, its audit log, a stop of the whole pipeline. It never has stage runs:
maxpm serve starts nothing for it, and its runs stay the target's items.

The release path stays the target's own (manager's decision, option A of #6156):
- A closed transition holds the release item after it, the way the release cadence does (an outside blocker set by
  HOLD_BY, cleared by the same sync when the transition opens; a blocker of someone else stays): the arrow after
  holding (the release) holds an uncut release and its review, and the cut (core.target_cut refuses); the arrow into
  review holds the review of a cut release; the arrow into approval holds the deploy item, so nobody is asked for the
  yes yet, and the open approval item; the arrow into deploy holds the deploy item, so the deploy hook does not start.
  The watch runs the target's checks after each deploy by itself: its arrow has no controls. A stop of the whole
  pipeline holds them all. A deploy that runs already goes on to its end.
- A run again, a move back or an override of the deploy stage makes a deploy item of the target pinned at that
  version (or reuses the open one pinned there): it waits on the person's approval item (release_approval person) and
  goes out through the deploy hook (MAXPM_REV), as every release does. No action runs the release command itself. Such
  an item was sent on purpose, so it passes the transitions (a stop still holds it); an open release of the target
  waits until it is done. A run again of review opens the release's review again; of build, runs its release-cut hook
  again.
- Who acts: as on every pipeline, a person or the manager, and the pipeline's operators on the Emergency tab (agents
  only with 'agents may be operators').
"""

from .. import core
from . import audit, store, transitions

HOLD_BY = "maxpm pipeline"  # items.blocked_set_by of a release item that a closed transition holds
PURPOSE_WORDS = {"rerun": "run again", "rollback": "moved back", "override": "override"}


def is_target(p):
    return bool(p is not None and "target" in p.keys() and p["target"])


def row(conn, target):
    """The stored row of a target's controls, or None (no write)."""
    return conn.execute("SELECT * FROM pipelines WHERE name=? AND target IS NOT NULL", (target,)).fetchone()


def _project(conn, target):
    r = conn.execute("SELECT name FROM projects WHERE target=? AND archived=0 AND name<>? ORDER BY rank, id LIMIT 1",
                     (target, f"deploy-{target}")).fetchone()
    return r["name"] if r else None


def _in_tx(conn, work):
    if conn.in_transaction:
        return work()
    with core.tx(conn):
        return work()


def ensure(conn, name):
    """The row of pipeline name when it is a deploy target with no pipeline of its own: made now when it has none, and
    its stages kept the same as the target's. None when name is no such target."""
    p = conn.execute("SELECT * FROM pipelines WHERE name=?", (name,)).fetchone()
    if p is not None and not is_target(p):
        return p
    if p is None and not conn.execute("SELECT 1 FROM targets WHERE name=?", (name,)).fetchone():
        return None

    def work():
        q = conn.execute("SELECT * FROM pipelines WHERE name=?", (name,)).fetchone()
        if q is None:
            pid = conn.execute("INSERT INTO pipelines(name, project, notes, created_by, created_at, target) VALUES "
                               "(?,?,?,?,?,?)", (name, _project(conn, name), f"the controls of deploy target {name}",
                                                 "maxpm", store._now(), name)).lastrowid
            audit.write(conn, pid, "maxpm", "define", f"deploy target {name} keeps the controls of its pipeline here "
                        "(its transitions, operators, triggers and audit log); its releases stay its own items",
                        kind="trigger")
            q = conn.execute("SELECT * FROM pipelines WHERE id=?", (pid,)).fetchone()
        _sync_stages(conn, q)
        return conn.execute("SELECT * FROM pipelines WHERE id=?", (q["id"],)).fetchone()
    if p is not None and _stages_match(conn, p):
        return p
    return _in_tx(conn, work)


def _want(conn, target):
    from . import targets
    return [s for s in targets.shape(conn, target) if s != "holding"]


def _live(conn, pid):
    return conn.execute("SELECT * FROM pipeline_stages WHERE pipeline_id=? AND removed_at IS NULL ORDER BY pos, id",
                        (pid,)).fetchall()


def _stages_match(conn, p):
    return [s["name"] for s in _live(conn, p["id"])] == _want(conn, p["target"])


def _sync_stages(conn, p):
    """Inside a tx: the stored stages are the target's (less holding), in its order. A stage the target no longer has
    is removed (its transition row stays and comes back with it)."""
    from . import targets
    want = _want(conn, p["target"])
    have = {s["name"]: s for s in conn.execute("SELECT * FROM pipeline_stages WHERE pipeline_id=?", (p["id"],))}
    t = store._now()
    for pos, name in enumerate(want, 1):
        s = have.get(name)
        if s is None:
            conn.execute("INSERT INTO pipeline_stages(pipeline_id, name, pos, kind, created_by, created_at, tier) "
                         "VALUES (?,?,?,?,?,?,?)", (p["id"], name, pos, targets.KINDS[name], "maxpm", t,
                                                    "prod" if name in ("deploy", "watch") else None))
        elif s["removed_at"] or s["pos"] != pos:
            conn.execute("UPDATE pipeline_stages SET removed_at=NULL, pos=? WHERE id=?", (pos, s["id"]))
    for name, s in have.items():
        if name not in want and not s["removed_at"]:
            conn.execute("UPDATE pipeline_stages SET removed_at=? WHERE id=?", (t, s["id"]))


def refuse_define(p, what):
    """A deploy target's stages come from its releases: no stage, environment, gate, source or intake of ours."""
    if is_target(p):
        raise core.RiverError(f"{p['name']} is a deploy target: its stages come from its releases (its review, the "
                              f"person's yes, its deploy hook), so MaximizePM {what} there never. Its transitions, "
                              f"operators, triggers and emergency actions work: maxpm pipeline show {p['name']}")


# ---------------------------------------------------------------- the arrows

WATCH_FIXED = "the watch runs the target's checks after each deploy by itself: block the transition into deploy"


def fixed_stage(p, stage):
    """Refuse a control on the arrow into watch (it lets nothing in: the checks run after each deploy)."""
    if is_target(p) and stage == "watch":
        raise core.RiverError(f"the transition into watch of {p['name']} has no controls: {WATCH_FIXED}")


def arrow(conn, p, s, stage, mode, cache=None):
    """The arrow into a stage of a target's pipeline: its mode from the target (the release, the person's yes,
    automatic) and its controls from the stored row (none when the target has none yet)."""
    plain = transitions.plain(*mode)
    if stage == "watch":
        return dict(plain, fixed_why=WATCH_FIXED)
    plain["fixed"] = False
    if p is None or s is None:
        return plain
    d = transitions.describe(conn, s, p, cache)
    held = p["held_reason"]
    out = dict(d, mode=plain["mode"], arg=plain["arg"], words=plain["words"], text=plain["text"], fixed=False,
               mode_by=None, mode_at=None, waiting=[], **{"from": None, "join": None})
    if held:
        out.update(state="blocked", state_text=f"the pipeline is stopped by {p['held_by'] or 'a person'}: {held}",
                   set_by=p["held_by"])
    elif out["state"] == "open":
        out["state_text"] = plain["text"]
    out["holds"] = [dict(r) for r in conn.execute(
        "SELECT id, title, kind FROM items WHERE blocked_set_by=? AND blocked_reason LIKE ? AND status='open' ORDER BY "
        "id", (HOLD_BY, f"%the transition into {stage} %"))] if out["state"] != "open" else []
    return out


def _closed(conn, p, s):
    if p["held_reason"]:
        return f"pipeline {p['name']} is stopped by {p['held_by'] or 'a person'}: {p['held_reason']}"
    c = transitions.closed(conn, transitions.get(conn, s["id"]), p)
    return f"the transition into {s['name']} of pipeline {p['name']} is {c['text']}" if c else None


def _items_at(conn, target, stage, first):
    """The open release items that the arrow into stage holds (see the module's text)."""
    if stage == first:
        deps = conn.execute("SELECT id FROM items WHERE kind='deploy' AND target=? AND status='open' AND cut_at IS NULL",
                            (target,)).fetchall()
        ids = [d["id"] for d in deps]
        return ids + _linked(conn, ids, "i.kind='review'")
    if stage == "review":
        ids = [d["id"] for d in conn.execute("SELECT id FROM items WHERE kind='deploy' AND target=? AND status IN "
                                             f"{core.OPEN_STATES} AND cut_at IS NOT NULL", (target,))]
        return _linked(conn, ids, "i.kind='review'")
    if stage == "approval":
        ids = [d["id"] for d in conn.execute("SELECT id FROM items WHERE kind='deploy' AND target=? AND status='open' "
                                             "AND cut_at IS NOT NULL", (target,))]
        return ids + _linked(conn, ids, "i.doer='human' AND i.title LIKE ?", core.APPROVE_TITLE + " %")
    if stage == "deploy":
        return [d["id"] for d in conn.execute("SELECT id FROM items WHERE kind='deploy' AND target=? AND status='open'",
                                              (target,))]
    if stage == "publish":  # #6213: the release that the person approved waits; the deploy hook does not publish it
        return [d["id"] for d in conn.execute("SELECT id FROM items WHERE kind='deploy' AND target=? AND status='open' "
                                              "AND cut_at IS NOT NULL", (target,)) if core._person_yes(conn, d["id"])]
    return []


def _linked(conn, ids, where, *args):
    if not ids:
        return []
    return [r["id"] for r in conn.execute(
        f"SELECT DISTINCT i.id FROM deps d JOIN items i ON i.id=d.blocked_by WHERE d.item_id IN "
        f"({','.join('?' * len(ids))}) AND i.status='open' AND {where}", (*ids, *args))]


def _sent(conn, pid):
    return {r["item_id"] for r in conn.execute("SELECT item_id FROM target_sends WHERE pipeline_id=?", (pid,))}


def holds(conn):
    """{item id: {why, stage}}: the release items that the closed transitions of the targets' pipelines hold now
    (stage: the first stage, left to right, whose closed transition holds it)."""
    want = {}
    for p in conn.execute(
            "SELECT p.* FROM pipelines p WHERE p.target IS NOT NULL "
            "AND (p.held_reason IS NOT NULL OR EXISTS (SELECT 1 "
            "FROM pipeline_transitions t WHERE t.pipeline_id=p.id AND (t.block_reason IS NOT NULL OR t.disabled_at IS "
            "NOT NULL OR t.delay_until IS NOT NULL OR (t.freezes IS NOT NULL AND t.freezes NOT IN ('', '[]')) OR "
            "t.time_window IS NOT NULL OR t.fault_rule=1)))").fetchall():
        stages = _live(conn, p["id"])
        if not stages:
            continue
        sent = _sent(conn, p["id"])
        for s in stages:
            why = _closed(conn, p, s)
            if not why:
                continue
            for i in _items_at(conn, p["target"], s["name"], stages[0]["name"]):
                if i in sent and not p["held_reason"]:
                    continue  # sent on purpose: it passes the transitions; a stop still holds it
                want.setdefault(i, {"why": why, "stage": s["name"]})
    return want


def sync(conn):
    """Inside a tx (core._sweep, the deploy hook's start, the release pass of maxpm serve): the release items that a
    closed transition holds get an outside blocker by HOLD_BY; one that no transition holds any more loses it. A
    blocker that someone else set stays (a running deploy hook, the cadence, a person's)."""
    want = holds(conn)
    t = core.iso(core.now())
    for r in conn.execute("SELECT id FROM items WHERE blocked_set_by=?", (HOLD_BY,)).fetchall():
        if r["id"] not in want:
            conn.execute("UPDATE items SET blocked_reason=NULL, blocked_until=NULL, blocked_at=NULL, blocked_set_by=NULL "
                         "WHERE id=?", (r["id"],))
            core._event(conn, r["id"], "maxpm", "the pipeline's transition is open again: this release item waits no "
                                                "more")
    for item_id, h in want.items():
        why = h["why"]
        it = conn.execute("SELECT status, blocked_reason, blocked_set_by FROM items WHERE id=?", (item_id,)).fetchone()
        if not it or it["status"] != "open" or (it["blocked_reason"] and it["blocked_set_by"] != HOLD_BY):
            continue
        if it["blocked_set_by"] == HOLD_BY and it["blocked_reason"] == why:
            continue
        conn.execute("UPDATE items SET blocked_reason=?, blocked_until=NULL, blocked_set_by=?, "
                     "blocked_at=COALESCE(blocked_at, ?) WHERE id=?", (why, HOLD_BY, t, item_id))
        core._event(conn, item_id, "maxpm", f"held: {why}")
    return want


def cut_refusal(conn, target):
    """Why a release of target may not be cut now (the arrow after holding is closed, or the pipeline is stopped), or
    None."""
    p = row(conn, target)
    if p is None:
        return None
    stages = _live(conn, p["id"])
    why = _closed(conn, p, stages[0]) if stages else None
    if why:
        return (f"{why}; the release waits. To release anyway: open it on the Deploy tab, or maxpm pipeline unblock "
                f"{p['name']} {stages[0]['name']}")
    return None


# ---------------------------------------------------------------- the runs of a stage, and sending a version

def _rev_match(a, b):
    return bool(a and b) and (a == b or (len(b) >= 7 and a.startswith(b)) or (len(a) >= 7 and b.startswith(a)))


def stage_versions(conn, p, stage, limit=store.STAGE_VERSIONS):
    """'Versions in <stage>' of a target's pipeline, in the form of store.stage_versions: one row for each release that
    reached the stage, newest first; a version sent on purpose names who sent it and why."""
    from . import targets
    releases = targets.release_runs(conn, p["target"], targets._deploys(conn, p["target"], limit))
    if stage not in [s["name"] for s in _live(conn, p["id"])] + ["holding"]:
        raise core.RiverError(f"pipeline {p['name']} has no stage {stage}")
    sends = {}
    for r in conn.execute("SELECT * FROM target_sends WHERE pipeline_id=? ORDER BY id", (p["id"],)):
        sends.setdefault(r["item_id"], r)  # the send that made the item says why; a later one reused it
    # The release that runs in production: the newest done deploy, as the default pipeline's environment says
    # (#6182: the first passed review was marked live, 9783eb1, while production ran daa69ba).
    live = conn.execute("SELECT id FROM items WHERE kind='deploy' AND target=? AND status='done' AND closed_at IS NOT "
                        "NULL ORDER BY id DESC LIMIT 1", (p["target"],)).fetchone()
    out = []
    for rel in releases:
        r = rel["runs"].get(stage)
        if r is None:
            continue
        is_live = bool(live) and rel["deploy"] == live["id"]
        sent = sends.get(rel["deploy"]) or (sends.get(r["item"]) if r.get("item") else None)
        took = None
        if r["started_at"] and r["ended_at"]:
            took = (core.parse_iso(r["ended_at"]) - core.parse_iso(r["started_at"])).total_seconds()
        out.append({"run": None, "item": r.get("item") or rel["deploy"], "deploy": rel["deploy"],
                    "rev": rel["cut_rev"] or r["version"], "pinned": bool(rel["cut_rev"]),
                    "released": rel["status"] == "done",
                    "what": PURPOSE_WORDS.get(sent["purpose"]) if sent else None,  # #6182: a plain release says no 'ran'
                    "purpose": sent["purpose"] if sent else None, "by": sent["sent_by"] if sent else None,
                    "why": sent["reason"] if sent else None,  # #6182: not 'the release of deploy #N' on every row
                    "state": r["state"], "queued_at": r["started_at"], "started_at": r["started_at"],
                    "ended_at": r["ended_at"], "seconds": took, "errors": r["errors"], "live": is_live})
    return {"pipeline": p["name"], "stage": stage, "kind": targets.KINDS.get(stage, "deploy"), "env_id": None, "runs": out,
            "branches": [], "target": p["target"]}


def override_plan(conn, p):
    """operators._override_plan of a target's pipeline: the pinned versions of its releases, with the stages each one
    passed, and the deploy stage (the one stage that an override may send a version to)."""
    from . import targets
    order = [s["name"] for s in _live(conn, p["id"])]
    versions, seen = [], set()
    for rel in targets.release_runs(conn, p["target"]):
        if not rel["cut_rev"] or rel["cut_rev"] in seen:
            continue
        seen.add(rel["cut_rev"])
        versions.append({"rev": rel["cut_rev"], "passed": [n for n in order if (rel["runs"].get(n) or {}).get("state")
                                                           == "passed"]})
    hook = core.deploy_hook(conn, p["target"])
    stages = [{"name": "deploy", "before": order[:order.index("deploy")] if "deploy" in order else [],
               "runs": (f"a deploy item of {p['target']}: after the person's yes, the deploy hook {hook}" if hook else
                        f"a deploy item of {p['target']}: the deployer takes it"), "check": None}]
    return {"versions": versions[:12], "stages": stages}


def _write_send(conn, p, stage, item_id, rev, purpose, actor, reason, text):
    conn.execute("INSERT INTO target_sends(pipeline_id, stage, item_id, rev, purpose, sent_by, reason, at) VALUES "
                 "(?,?,?,?,?,?,?,?)", (p["id"], stage, item_id, rev, purpose, actor or "a person", reason,
                                       store._now()))
    audit.write(conn, p["id"], actor, purpose, text, place=f"stage {stage}", rev=rev, reason=reason,
                detail={"item": item_id})


def send(conn, p, stage, rev, actor=None, purpose="rerun", note=None, reason=None):
    """A run again, a move back or an override of a stage of a target's pipeline (runner.run_again calls it): see the
    module's text. Returns the form of runner.run_again, with the item."""
    rev = (rev or "").strip()
    if not rev:
        raise core.RiverError("name the version: --rev <commit>")
    if stage == "deploy":
        return _deploy(conn, p, rev, actor, purpose, note, reason)
    rel = next((d for d in conn.execute(f"SELECT * FROM items WHERE kind='deploy' AND target=? AND status IN "
                                        f"{core.OPEN_STATES} AND cut_rev IS NOT NULL ORDER BY id DESC", (p["target"],))
                if _rev_match(d["cut_rev"], rev)), None)
    if stage in ("review", "build") and purpose == "rerun":
        if rel is None:
            raise core.RiverError(f"no open release of {p['target']} is pinned at {rev}: a run again of {stage} acts "
                                  f"on a release that is not out yet. To send {rev} out again: maxpm pipeline rerun "
                                  f"{p['name']} deploy --rev {rev}")
        if stage == "build":
            with core.tx(conn):
                core._hook(conn, "release-cut", p["target"], rel["id"], actor)
                _write_send(conn, p, stage, rel["id"], rel["cut_rev"], purpose, actor, reason,
                            note or f"ran the build (the release-cut hook) of release #{rel['id']} again for "
                                    f"{rel['cut_rev']}")
            return {"pipeline": p["name"], "stage": stage, "version": rel["cut_rev"], "run": None, "item": rel["id"]}
        rv = conn.execute("SELECT i.* FROM deps d JOIN items i ON i.id=d.blocked_by WHERE d.item_id=? AND "
                          "i.kind='review' ORDER BY i.id DESC LIMIT 1", (rel["id"],)).fetchone()
        if rv is None:
            raise core.RiverError(f"release #{rel['id']} of {p['target']} has no review")
        if rv["status"] in core.CLOSED_STATES:
            core.reopen(conn, rv["id"], actor)
        with core.tx(conn):
            _write_send(conn, p, stage, rv["id"], rel["cut_rev"], purpose, actor, reason,
                        note or f"opened review #{rv['id']} of release #{rel['id']} again for {rel['cut_rev']}")
        return {"pipeline": p["name"], "stage": stage, "version": rel["cut_rev"], "run": None, "item": rv["id"]}
    yes = " (the person's own approval item)" if stage == "approval" else ""
    raise core.RiverError(f"the stage {stage} of deploy target {p['target']} takes no {PURPOSE_WORDS.get(purpose, purpose)}"
                          f"{yes}: send a version to deploy (maxpm pipeline rerun {p['name']} deploy --rev <commit>), "
                          f"or run again review or build of an open release")


def _deploy(conn, p, rev, actor, purpose, note, reason):
    """A deploy item of the target pinned at rev: the open one pinned there, or a new one, cut at rev (its release-cut
    hook builds it), that waits on the person's yes and the deploy hook as every release does. The other open releases
    of the target wait until it is done."""
    target = p["target"]
    what = PURPOSE_WORDS.get(purpose, purpose)
    with core.tx(conn):
        same = next((d for d in conn.execute(f"SELECT * FROM items WHERE kind='deploy' AND target=? AND status IN "
                                             f"{core.OPEN_STATES} ORDER BY id", (target,)) if _rev_match(d["cut_rev"], rev)),
                    None)
        if same is not None:
            _write_send(conn, p, "deploy", same["id"], same["cut_rev"], purpose, actor, reason,
                        (note or f"{what}: {rev} to deploy") + f"; deploy #{same['id']} is pinned there already and "
                        "goes on with its release")
            return {"pipeline": p["name"], "stage": "deploy", "version": same["cut_rev"], "run": None,
                    "item": same["id"], "reused": True}
        pname = f"deploy-{target}"
        prow = conn.execute("SELECT id FROM projects WHERE name=?", (pname,)).fetchone()
        if prow is None:
            top = conn.execute("SELECT COALESCE(MAX(rank),0) m FROM projects WHERE archived=0").fetchone()["m"]
            conn.execute("INSERT INTO projects(name,rank,notes,target,created_at) VALUES (?,?,?,?,?)",
                         (pname, top + 1, f"Deploys to target {target}, made by maxpm ship. Only the target owner "
                          f"takes these items.", target, core.iso(core.now())))
            prow = conn.execute("SELECT id FROM projects WHERE name=?", (pname,)).fetchone()
        top = conn.execute("SELECT COALESCE(MAX(rank),0) m FROM items WHERE project_id=?", (prow["id"],)).fetchone()["m"]
        why = (note or f"{what}: {rev} to deploy") + (f": {reason}" if reason else "")
        dep_id = conn.execute(
            "INSERT INTO items(project_id,title,short,notes,priority,rank,doer,context,kind,target,created_at) VALUES "
            "(?,?,?,?,?,?,'any',?,'deploy',?,?)",
            (prow["id"], f"Deploy {target} {rev[:12]} ({what}, pipeline {p['name']})", f"Deploy {rev[:12]} ({what})",
             "Put the release id in the output.", 1, top + 1,
             f"Sent from the pipeline of deploy target {target} by {actor or 'a person'}: {why}. It goes out the way "
             f"every release of {target} does: after the person's yes when the target asks for one (release_approval "
             f"person), through its deploy hook with MAXPM_REV={rev}.", target, core.iso(core.now()))).lastrowid
        core._event(conn, dep_id, actor, f"deploy item for target {target} added by the pipeline: {why}")
        core._release_cut(conn, dep_id, actor, f"{what} from the pipeline of {target}", rev)
        waits = []
        for d in conn.execute("SELECT id FROM items WHERE kind='deploy' AND target=? AND status='open' AND id<>?",
                              (target, dep_id)).fetchall():
            core._dep_add(conn, d["id"], dep_id, actor, alert=False)
            core._event(conn, d["id"], actor, f"waits until deploy #{dep_id} ({what} of {rev}) is out")
            waits.append(d["id"])
        _write_send(conn, p, "deploy", dep_id, rev, purpose, actor, reason,
                    why + f": deploy #{dep_id} pinned at it" + (f"; release(s) {', '.join(f'#{w}' for w in waits)} "
                                                                f"wait until it is out" if waits else ""))
    core.sync_needs_you(conn)
    return {"pipeline": p["name"], "stage": "deploy", "version": rev, "run": None, "item": dep_id, "waits": waits}


def passed_deploy(conn, p, rev):
    """The pin of a done release of the target that matches rev (a move back takes a version that went out), or
    None."""
    for d in conn.execute("SELECT cut_rev FROM items WHERE kind='deploy' AND target=? AND status='done' AND cut_rev IS "
                          "NOT NULL ORDER BY closed_at DESC", (p["target"],)):
        if _rev_match(d["cut_rev"], rev):
            return d["cut_rev"]
    return None


def start(conn, p, rev, actor=None):
    """runner.start of a target's pipeline (Run the whole pipeline): the release from holding, cut at rev. Never a
    move of the pin of a release that is cut already (target_cut does that with --rev)."""
    plan = core.release_plan(conn, p["target"])
    if plan["deploy"] is None or not plan["ships"]:
        raise core.RiverError(f"nothing is collected for {p['target']}: the whole pipeline runs for a release from "
                              f"holding (maxpm pipeline release {p['name']}); a version that went past it already "
                              f"goes out with its own release")
    out = core.target_cut(conn, p["target"], rev=rev, actor=actor)
    with core.tx(conn):
        audit.write(conn, p["id"], actor, "start", f"cut release #{out['cut']['id']} of {p['target']} at {rev}",
                    place="the release", rev=rev)
    return {"pipeline": p["name"], "stage": "holding", "version": rev, "run": None, "item": out["cut"]["id"]}


# ---------------------------------------------------------------- the events of its trigger rules

def events(conn, p, since):
    """The generic events of a target's pipeline (triggers.events): a stage run of a release that started, ended or
    failed, from the target's items."""
    from . import targets
    stages = {s["name"]: dict(s) for s in _live(conn, p["id"])}
    out = []
    for rel in targets.release_runs(conn, p["target"]):
        for name, r in rel["runs"].items():
            s = stages.get(name)
            if s is None:
                continue
            base = {"run_id": None, "stage": name, "tier": s.get("tier"), "rev": rel["cut_rev"] or r["version"],
                    "item": r.get("item") or rel["deploy"]}
            key = f"{p['target']}:{rel['deploy']}:{name}"
            if r["started_at"] and r["started_at"] >= since:
                out.append(dict(base, event="run-started", key=f"run-started:{key}", at=r["started_at"],
                                text=f"stage {name} of release #{rel['deploy']} started"))
            # A deploy whose hook failed is open again (it runs again after a fix): its end is the failed step's.
            end = r["ended_at"] or next((s["ended_at"] for s in reversed(r["step_list"]) if s.get("ended_at")), None)
            if r["state"] in ("passed", "failed", "cancelled") and end and end >= since:
                key = f"{key}:{end}"  # a release can fail again after a fix
                out.append(dict(base, event="run-ended", key=f"run-ended:{key}", at=end, state=r["state"],
                                text=f"stage {name} of release #{rel['deploy']} ended {r['state']}"
                                     + (f": {r['errors']}" if r["errors"] else "")))
                if r["state"] == "failed":
                    out.append(dict(base, event="stage-failed", key=f"stage-failed:{key}", at=end,
                                    text=f"stage {name} of release #{rel['deploy']} failed: {r['errors'] or 'failed'}"))
    return out
