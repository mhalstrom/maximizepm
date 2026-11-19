"""The operators of a pipeline and its emergency actions (#6145): the Emergency tab of each pipeline's page.

mark 2026-11-18 (canvas release-tracking, task N; Decisions 7 and 10): "the emergency actions are per pipeline", "the
emergency [actions] should be on the specific pipeline page", "operators can, we audit who does them", and on agents
as operators: "this is a pipeline by pipeline decision".

- The operators are a list on each pipeline (pipeline_operators): people, and agent sessions when the pipeline's
  setting 'agents may be operators' (pipelines.agents_operate) is on. Only a person changes the list and the setting.
  A pipeline with no operators named: a person or the manager, as for every other pipeline action.
- The emergency actions, each for that one pipeline, each with a reason and a line in the audit log that names the
  operator:
    stop       block every stage of it: nothing new starts, and a running stage stops after its present step
               (pipelines.held_* with stop_runs); a banner until an operator resumes it
    resume     end the stop
    agents     ask each agent session that works on it now (a fault or a fix task under its stages) to stop
    moveback   put a stage back on its last good version: the newest version that passed there before the one it has
               (runner.move_back: one more run, and the transition into the stage blocked after it)
    revoke     take a key away from it: the forge or plugin token it reads with, or a credential set its trigger rules
               give their tasks (and the grants those tasks hold now); a warning names each other pipeline that uses
               the same token
  plan() says what each one acts on before an operator presses it. The override (send a version straight to a later
  stage) is #6146.

While an operator's action runs, the checks for 'a person or the manager' inside it (runner, transitions) let that
operator act (vouched): an agent on the list moves a stage back as a person would.
"""

import contextlib
import json
import threading

from .. import core
from . import audit, store

_VOUCH = threading.local()


@contextlib.contextmanager
def _acting(actor):
    old = getattr(_VOUCH, "name", None)
    _VOUCH.name = actor or "a person"
    try:
        yield
    finally:
        _VOUCH.name = old


def vouched(actor):
    """True while an emergency action of this operator runs (runner._lead and transitions._lead ask it)."""
    return getattr(_VOUCH, "name", None) == (actor or "a person")


# ---------------------------------------------------------------- the operators

def _rows(conn, pid):
    return conn.execute("SELECT o.*, a.kind FROM pipeline_operators o LEFT JOIN agents a ON a.name=o.name WHERE "
                        "o.pipeline_id=? ORDER BY o.id", (pid,)).fetchall()


def listing(conn, pipeline):
    """The operators of a pipeline: each with its kind (person, agent) and whether it may act now (an agent only
    when the pipeline lets agents operate)."""
    p = store._pipeline(conn, pipeline)
    agents = bool(p["agents_operate"])
    ops = [{"name": r["name"], "kind": "agent" if r["kind"] == "ai" else "person", "added_by": r["added_by"],
            "added_at": r["added_at"], "may_act": r["kind"] != "ai" or agents} for r in _rows(conn, p["id"])]
    return {"pipeline": p["name"], "operators": ops, "agents": agents,
            "rule": "the operators named here" if ops else "a person or the manager (no operators named yet)"}


def refusal(conn, p, actor):
    """Why actor may not use the emergency actions of pipeline p, or None."""
    rows = _rows(conn, p["id"])
    if not rows:
        return core._lead_refusal(conn, actor, "uses the emergency actions of a pipeline")
    if actor is None:
        return None  # the person at the terminal
    names = [r["name"] for r in rows]
    if actor not in names:
        return (f"{actor} is not an operator of pipeline {p['name']} (its operators: {', '.join(names)}); a person "
                f"adds one: maxpm pipeline operators {p['name']} --add <name>")
    kind = next(r["kind"] for r in rows if r["name"] == actor)
    if kind == "ai" and not p["agents_operate"]:
        return (f"{actor} is an agent session, and pipeline {p['name']} lets no agent operate; a person turns it on: "
                f"maxpm pipeline operators {p['name']} --agents on")
    return None


def check(conn, p, actor):
    why = refusal(conn, p, actor)
    if why:
        raise core.RiverError(why)


def change(conn, pipeline, add=(), remove=(), agents=None, actor=None):
    """maxpm pipeline operators <name> --add <name> --remove <name> --agents on|off: only a person changes who may
    press the emergency actions (Decision 10: whether agents may is a person's decision for each pipeline)."""
    if actor is not None and not core._is_person(conn, actor):
        raise core.RiverError(f"{actor} is not a person; only a person names the operators of a pipeline. Ask the "
                              "person: maxpm ask <person> \"...\"")
    add, remove = [a.strip() for a in add or () if a.strip()], [r.strip() for r in remove or () if r.strip()]
    if not add and not remove and agents is None:
        raise core.RiverError("give what to change: --add <name>, --remove <name> or --agents on|off")
    with core.tx(conn):
        p = store._pipeline(conn, pipeline)
        have = {r["name"] for r in _rows(conn, p["id"])}
        parts = []
        for name in add:
            if not conn.execute("SELECT 1 FROM agents WHERE name=?", (name,)).fetchone():
                raise core.RiverError(f"no person or session named {name} (maxpm who --all)")
            if name in have:
                raise core.RiverError(f"{name} is an operator of pipeline {p['name']} already")
            conn.execute("INSERT INTO pipeline_operators(pipeline_id, name, added_by, added_at) VALUES (?,?,?,?)",
                         (p["id"], name, actor, store._now()))
            parts.append(f"added operator {name}")
        for name in remove:
            if name not in have:
                raise core.RiverError(f"{name} is not an operator of pipeline {p['name']}")
            conn.execute("DELETE FROM pipeline_operators WHERE pipeline_id=? AND name=?", (p["id"], name))
            parts.append(f"removed operator {name}")
        if agents is not None and bool(agents) != bool(p["agents_operate"]):
            conn.execute("UPDATE pipelines SET agents_operate=? WHERE id=?", (1 if agents else 0, p["id"]))
            parts.append("agents may be operators" if agents else "agents may no longer be operators")
        if not parts:
            raise core.RiverError(f"pipeline {p['name']} has these operators already")
        audit.write(conn, p["id"], actor, "operators", f"{'; '.join(parts)} of pipeline {p['name']}",
                    place="operators")
    return dict(listing(conn, pipeline), changed="; ".join(parts))


# ---------------------------------------------------------------- what each action acts on

def _stages(conn, p):
    return conn.execute("SELECT * FROM pipeline_stages WHERE pipeline_id=? AND removed_at IS NULL ORDER BY pos, id",
                        (p["id"],)).fetchall()


def _agents(conn, p):
    """The agent sessions that work on the pipeline now: each one that holds an open fault or fix task under its
    stages (links.stage_work) and was not asked to stop already."""
    from . import links
    out = []
    for stage, w in links.stage_work(conn, pipeline=p["name"]).get(p["name"], {}).items():
        for a in w["agents"]:
            if a["st"] == "ok" or a["live"] == "gone" or any(x["name"] == a["name"] for x in out):
                continue
            row = conn.execute("SELECT stop_at FROM agents WHERE name=?", (a["name"],)).fetchone()
            if row and row["stop_at"]:
                continue
            out.append({"name": a["name"], "stage": stage, "item": a["item"], "title": a["title"], "state": a["state"]})
    return out


def _last_good(conn, p, stage):
    """(the version a stage has now, the last good version before it): the newest passed run, and the newest passed
    run of another version after it (store.stage_versions)."""
    runs = store.stage_versions(conn, p["name"], stage)["runs"]
    now = next((r["rev"] for r in runs if r["live"]), None)
    good = next((r for r in runs if r["state"] == "passed" and r["rev"] and r["rev"] != now), None)
    return now, good


def _uses(conn, secret, but=None):
    """The pipelines that read a source with that maxpm secret (their forge token or a plugin's secret)."""
    from . import plugin
    out = []
    for q in conn.execute("SELECT * FROM pipelines ORDER BY name").fetchall():
        if q["name"] != but and secret in plugin._config(q)["secrets"].values():
            out.append(q["name"])
    return out


def _keys(conn, p):
    """The keys of a pipeline: the token its source reads with (maxpm secrets) and the credential sets its trigger
    rules give their tasks, each with what it reaches and who else uses it."""
    from . import plugin
    out = []
    for name, secret in sorted(plugin._config(p)["secrets"].items()):
        if secret:
            out.append({"key": secret, "kind": "token", "what": f"the {name} that pipeline {p['name']} reads its "
                        "source with", "others": _uses(conn, secret, but=p["name"])})
    sets = {}
    for r in conn.execute("SELECT id, keys FROM pipeline_triggers WHERE pipeline_id=? AND keys IS NOT NULL",
                          (p["id"],)):
        for k in r["keys"].split(","):
            if k:
                sets.setdefault(k, []).append(f"t{r['id']}")
    from . import keys as stage_keys
    bound = stage_keys.bound(conn, p["id"])  # #6153: the sets its stages and tiers may read
    for k in sorted(set(sets) | set(bound)):
        rules = sets.get(k) or []
        held = len(_task_grants(conn, p, k))
        others = sorted({q["name"] for q in conn.execute(
            "SELECT DISTINCT pl.name FROM pipeline_triggers t JOIN pipelines pl ON pl.id=t.pipeline_id WHERE "
            "t.pipeline_id<>? AND (','||t.keys||',') LIKE ?", (p["id"], f"%,{k},%"))}
                        | {q["name"] for q in conn.execute(
                            "SELECT DISTINCT pl.name FROM pipeline_keys x JOIN pipelines pl ON pl.id=x.pipeline_id "
                            "WHERE x.pipeline_id<>? AND x.set_name=? AND x.revoked_at IS NULL", (p["id"], k))})
        what = ([f"the credential set that trigger rule(s) {', '.join(rules)} give their tasks"] if rules else []) + (
            [f"a key that stage(s) {', '.join(bound[k]) or '(none live)'} may read"] if k in bound else [])
        out.append({"key": k, "kind": "set", "what": "; ".join(what) + (f"; {held} task(s) hold it now" if held else ""),
                    "others": others})
    return out


def _task_grants(conn, p, set_name):
    """The grants of a set that this pipeline's trigger rules gave to their tasks, still in force."""
    items = [str(r["item"]) for r in conn.execute("SELECT DISTINCT item FROM pipeline_trigger_fires WHERE "
                                                  "pipeline_id=? AND item IS NOT NULL", (p["id"],))]
    if not items:
        return []
    return conn.execute("SELECT g.* FROM secret_grants g JOIN secret_sets s ON s.id=g.set_id WHERE s.name=? AND "
                        "g.scope='item' AND g.revoked_at IS NULL AND g.value IN (" + ",".join("?" * len(items)) + ")",
                        [set_name] + items).fetchall()


def plan(conn, pipeline):
    """What each emergency action of a pipeline acts on, in words, before an operator presses it: the Emergency tab
    and maxpm pipeline emergency <name> show it."""
    p = store._pipeline(conn, pipeline)
    stages = _stages(conn, p)
    running = conn.execute("SELECT r.id, s.name stage, v.rev FROM stage_runs r JOIN pipeline_stages s ON "
                           "s.id=r.stage_id LEFT JOIN pipeline_versions v ON v.id=r.version_id WHERE r.pipeline_id=? "
                           "AND r.state='running' ORDER BY r.id", (p["id"],)).fetchall()
    waiting = conn.execute("SELECT COUNT(*) FROM stage_runs WHERE pipeline_id=? AND state='queued'",
                           (p["id"],)).fetchone()[0]
    stop = [f"{len(stages)} stage(s) of {p['name']}: nothing new starts" + (f"; {waiting} waiting version(s) stay where "
                                                                             "they are" if waiting else "")]
    stop += [f"{r['stage']} runs {(r['rev'] or '')[:12]} now: it stops after its present step" for r in running]
    target = p["target"] if "target" in p.keys() else None
    if target:  # #6176: a deploy target's stop holds its release items
        stop = [f"the open release items of deploy target {target} (its review, the person's yes, its deploy item): "
                "each one is held; a deploy hook that runs now goes on to its end"]
        stages = [s for s in stages if s["name"] == "deploy"]  # a move back of a target acts on its deploy stage
    stop.append("Other pipelines go on")
    back = []
    for s in stages:
        now, good = _last_good(conn, p, s["name"])
        back.append({"stage": s["name"], "now": now, "good": good["rev"] if good else None,
                     "good_at": good["ended_at"] if good else None})
    return {"pipeline": p["name"], "stopped": {"reason": p["held_reason"], "by": p["held_by"], "at": p["held_at"],
                                                "stop_runs": bool(p["stop_runs"])} if p["held_reason"] else None,
            "operators": listing(conn, pipeline), "stop": stop, "agents": _agents(conn, p), "moveback": back,
            "keys": _keys(conn, p), "override": _override_plan(conn, p),
            # #6167: a followed pipeline: only Stop its agents and Revoke its keys act (on our side)
            "followed": (p["forge"] or "its outside system") if p["followed"] else None,
            "target": target}  # #6176: a deploy target's pipeline: a release still waits for the person's yes


# ---------------------------------------------------------------- the actions

def _reason(reason):
    reason = (reason or "").strip()
    if not reason:
        raise core.RiverError("say why: --reason \"<what happened>\" (the audit log keeps it)")
    return reason


def stop(conn, pipeline, reason, actor=None):
    """Stop this pipeline: every stage is held (nothing new starts) and a running stage stops after its present step;
    the page shows a banner until an operator resumes it."""
    reason = _reason(reason)
    p = store._pipeline(conn, pipeline)
    check(conn, p, actor)
    from . import follow
    follow.check(conn, p, "stops")  # #6167
    with core.tx(conn):
        conn.execute("UPDATE pipelines SET held_reason=?, held_by=?, held_at=?, stop_runs=1 WHERE id=?",
                     (reason, actor, store._now(), p["id"]))
        n = conn.execute("SELECT COUNT(*) FROM stage_runs WHERE pipeline_id=? AND state='running'",
                         (p["id"],)).fetchone()[0]
        audit.write(conn, p["id"], actor, "emergency-stop", f"stopped pipeline {p['name']}: nothing new starts"
                    + (f"; {n} running stage(s) stop after their present step" if n else ""), place="every stage",
                    reason=reason)
    return {"pipeline": p["name"], "stopped": reason, "running": n}


def resume(conn, pipeline, actor=None):
    """Resume a stopped (or held) pipeline."""
    p = store._pipeline(conn, pipeline)
    check(conn, p, actor)
    if not p["held_reason"]:
        raise core.RiverError(f"pipeline {p['name']} is not stopped")
    with core.tx(conn):
        conn.execute("UPDATE pipelines SET held_reason=NULL, held_by=NULL, held_at=NULL, stop_runs=NULL WHERE id=?",
                     (p["id"],))
        audit.write(conn, p["id"], actor, "resume", f"resumed pipeline {p['name']}", place="every stage")
    return {"pipeline": p["name"], "stopped": None}


def stop_agents(conn, pipeline, reason, actor=None):
    """Ask each agent session that works on the pipeline now to stop (core.stop_agent: a request; the session
    commits, hands its task back with a note, and ends). Returns who was asked and who could not be."""
    reason = _reason(reason)
    p = store._pipeline(conn, pipeline)
    check(conn, p, actor)
    asked, refused = [], []
    for a in _agents(conn, p):
        try:
            core.stop_agent(conn, a["name"], f"pipeline {p['name']} emergency: {reason}", actor)
            asked.append(a["name"])
        except core.RiverError as e:
            refused.append(f"{a['name']}: {e}")
    with core.tx(conn):
        audit.write(conn, p["id"], actor, "stop-agents", (f"asked {', '.join(asked)} to stop" if asked else
                                                          "no agent works on it now")
                    + (f"; not stopped: {'; '.join(refused)}" if refused else ""), place="its agents", reason=reason)
    return {"pipeline": p["name"], "asked": asked, "refused": refused}


def move_back(conn, pipeline, stage, reason, actor=None):
    """Put a stage back on its last good version (runner.move_back): one more run, and the transition into it
    blocked after it."""
    reason = _reason(reason)
    p = store._pipeline(conn, pipeline)
    check(conn, p, actor)
    now, good = _last_good(conn, p, stage)
    if not good:
        raise core.RiverError(f"stage {stage} of pipeline {p['name']} has no earlier version that passed there"
                              + (f" before {now}" if now else ""))
    from . import runner
    with _acting(actor):
        out = runner.move_back(conn, p["name"], stage, good["rev"], reason, actor,
                               words=f"emergency: moved stage {stage} back to {good['rev']}")
    return dict(out, previous=now)


def _override_plan(conn, p):
    """For the override (#6146): the versions an operator may send (newest first) with the stages each one passed,
    and each stage after the first with the stages before it and what runs in it."""
    from . import target_flow
    if target_flow.is_target(p):  # #6176: a deploy target: its pinned releases, and its deploy stage
        return target_flow.override_plan(conn, p)
    rows, prev, _ = store.stage_graph(conn, p["id"])
    order = [r["name"] for r in rows]
    versions = []
    for v in conn.execute("SELECT id, rev FROM pipeline_versions WHERE pipeline_id=? ORDER BY id DESC LIMIT 12",
                          (p["id"],)).fetchall():
        passed = sorted({x["name"] for x in conn.execute(
            "SELECT s.name FROM stage_runs r JOIN pipeline_stages s ON s.id=r.stage_id WHERE r.version_id=? AND "
            "r.state='passed'", (v["id"],))}, key=lambda n: order.index(n) if n in order else 0)
        versions.append({"rev": v["rev"], "passed": passed})
    stages = []
    for r in rows:
        before = [n for n in order if n in store.ancestors(prev, r["name"])]
        if not before:
            continue
        what = r["run"] or (f"the workflow {r['workflow']}" if r["workflow"] else "nothing to run")
        stages.append({"name": r["name"], "before": before, "runs": what, "check": r["check_cmd"]})
    return {"versions": versions, "stages": stages}


def override(conn, pipeline, stage, rev, reason, actor=None):
    """The override (#6146; mark 2026-11-18: 'we also need to be able to override to deploy directly to prod'): send
    version rev straight to a later stage, past the stages and the transitions between. The stage's own steps and
    checks run, and the flow goes on after it. The stages it skipped do not run by themselves (mark: 'no'); until the
    version passed them, the stage shows 'not through the pipeline', and whole() runs them later."""
    reason = _reason(reason)
    p = store._pipeline(conn, pipeline)
    check(conn, p, actor)
    plan = _override_plan(conn, p)
    s = next((x for x in plan["stages"] if x["name"] == stage), None)
    if s is None:
        raise core.RiverError(f"stage {stage} is not a later stage of pipeline {p['name']} (the first stage: maxpm "
                              f"pipeline start {p['name']} --rev <commit>)")
    if not (rev or "").strip():
        raise core.RiverError("name the version: --rev <commit>")
    v = next((x for x in plan["versions"] if x["rev"] == rev or (len(rev) >= 7 and x["rev"].startswith(rev))), None)
    skipped = [n for n in s["before"] if not v or n not in v["passed"]]
    from . import runner
    words = (f"override: sent {rev} straight to stage {stage}" + (f"; skipped {', '.join(skipped)}" if skipped
                                                                    else "; it had passed every stage before"))
    with _acting(actor):
        out = runner.run_again(conn, p["name"], stage, v["rev"] if v else rev, actor, purpose="override",
                               note=words, reason=reason)
    return dict(out, skipped=skipped)


def whole(conn, pipeline, rev, actor=None):
    """'Run the whole pipeline for <version>' (#6146; mark: 'we can rerun the whole pipeline for the stuff we had to do
    an emergency release for'): the version starts at the first stage and goes through the flow; the run ends at a
    stage where an override put it already."""
    p = store._pipeline(conn, pipeline)
    check(conn, p, actor)
    from . import runner
    with _acting(actor):
        out = runner.start(conn, p["name"], rev=rev, actor=actor)
    with core.tx(conn):
        conn.execute("UPDATE stage_runs SET purpose='whole', sent_by=? WHERE id=?", (actor or "a person", out["run"]))
    return out


def revoke(conn, pipeline, key, reason, actor=None):
    """Take a key away from this pipeline: its source token (the pipeline no longer names it, so it reads nothing
    with it) or a credential set of its trigger rules (taken off the rules, and the grants their tasks hold now
    end). The secret itself stays: a person deletes or replaces it (maxpm secret put <name>). Other pipelines that use
    the same token keep it; the reply names them."""
    reason = _reason(reason)
    p = store._pipeline(conn, pipeline)
    check(conn, p, actor)
    keys = {k["key"]: k for k in _keys(conn, p)}
    if key not in keys:
        raise core.RiverError(f"pipeline {p['name']} has no key {key!r}; its keys: {', '.join(keys) or 'none'}")
    k, done = keys[key], []
    with core.tx(conn):
        if k["kind"] == "token":
            if p["secret"] == key:
                conn.execute("UPDATE pipelines SET secret=NULL WHERE id=?", (p["id"],))
            if p["plugin_settings"]:
                c = json.loads(p["plugin_settings"])
                c["secrets"] = {n: s for n, s in (c.get("secrets") or {}).items() if s != key}
                conn.execute("UPDATE pipelines SET plugin_settings=? WHERE id=?", (json.dumps(c), p["id"]))
            done.append(f"its source no longer reads with {key}")
        else:
            for r in conn.execute("SELECT id, keys FROM pipeline_triggers WHERE pipeline_id=? AND keys IS NOT NULL",
                                  (p["id"],)).fetchall():
                left = [x for x in r["keys"].split(",") if x and x != key]
                if len(left) != len([x for x in r["keys"].split(",") if x]):
                    conn.execute("UPDATE pipeline_triggers SET keys=? WHERE id=?", (",".join(left) or None, r["id"]))
                    done.append(f"rule t{r['id']} gives it no more")
    if k["kind"] == "set":
        from .. import vault
        for g in _task_grants(conn, p, key):
            with _acting(actor):
                vault.revoke(conn, grant_id=g["id"], actor=actor)
            done.append(f"the grant to task #{g['value']} ended")
        from . import keys as stage_keys  # #6153: its stages and tiers no longer read it
        for g in stage_keys.revoke(conn, set_name=key, actor=actor, reason=reason, vouched=True,
                                   pipeline_id=p["id"])["revoked"]:
            done.append(f"{g['covers']} no longer reads it ({g['grant']})")
    with core.tx(conn):
        audit.write(conn, p["id"], actor, "revoke", f"revoked {key} from pipeline {p['name']}: {'; '.join(done)}"
                    + (f"; other pipelines still use it: {', '.join(k['others'])}" if k["others"] else ""),
                    place=f"key {key}", reason=reason)
    return {"pipeline": p["name"], "key": key, "done": done, "others": k["others"]}
