"""Trigger rules of a pipeline (#6150): when something happens in the pipeline, an agent does what the rule says.

mark 2026-11-18 (canvas release-tracking, Plan 2 row E): each pipeline has its own rules ("same with triggers", as
its emergency actions), and "the generic componenets are what the triggers are hooked to". So a rule names one of the
generic events below, never an event of GitHub, GitLab or Pulumi Delivery. Each source turns its own events into
these: every source reports its runs through store.ingest (a script's '::step' lines and exit code through the
runner, a forge's runs through its adapter), so a stage run of any source has the same states and times, and this
module reads the generic events from those runs. A check that fails comes from the ops checks of the environment's
target or of the pipeline's project.

    stage-failed       a stage failed (a run of its own failed tests again does not count)
    check-failed       a check of the environment's target, or of the pipeline's project, became a fault within
                       a day after a version went to that environment (and before the next one did)
    transition-opened  a version went into a stage: its transition (#6139) let it in
    run-started        a stage run started
    run-ended          a stage run ended (passed, failed or stopped)

A rule names the event, the stage it watches (a stage, any stage of a tier, or any stage), what the agent does (the
instructions of its task), where the agent runs (this computer, or a runner that the pipeline ticks, #6151: a server
where the agent runs with no terminal and no prompt, river/runners.py; or github, #6152: a job in GitHub Actions that
GitHub starts when the stage's workflow fails, river/pipeline/agent_job.py), and the keys its task gets (credential sets, granted to that task alone until it closes). A person or the
manager adds, changes, turns off and removes rules; a rule that is off acts on nothing until it is on again, and then
only on what happens after that.

What a rule does when it acts: on stage-failed it opens the fault task of the stage, or joins the open one (#6118);
on check-failed it adds its words to the check's open fault task, or opens a task when the check has none; on the
other events it adds a task. Each task is operational: maxpm serve starts a new session on it by itself. Each act
is a row of pipeline_trigger_fires (an event acts once for each rule) and a line of the pipeline's audit log, whose
actor is the rule ('trigger t3').
"""

from .. import core
from . import links, store
from .schema import DEFAULT_RULE

EVENTS = {  # the generic events, in the words of the page and the briefing
    "stage-failed": "a stage failed",
    "check-failed": "a check failed after a deploy",
    "transition-opened": "a transition opened (a version went into a stage)",
    "run-started": "a run started",
    "run-ended": "a run ended",
}
THIS = "this computer"  # a rule's agent runs here, in a terminal, unless the rule names a runner (#6151)
WITHIN = 86400  # seconds: an event older than this when maxpm serve first sees it starts nothing
WATCH = 86400   # seconds: a check that fails this long after a deploy no longer counts as "after a deploy"
DEFAULT_DOES = DEFAULT_RULE[1]
CHECK_DOES = ("Find the cause of the failed check: the version that went out, or something else. When the version "
              "caused it, propose a move back of that environment as a card for the person.")


def _may(conn, actor):
    why = core._lead_refusal(conn, actor, "changes the trigger rules of a pipeline, because a rule starts agents "
                                          "and gives them keys")
    if why:
        raise core.RiverError(why)


def _rule(conn, pid, ref):
    ref = str(ref or "").strip().lower().lstrip("t")
    row = conn.execute("SELECT * FROM pipeline_triggers WHERE id=? AND pipeline_id=?",
                       (int(ref) if ref.isdigit() else -1, pid)).fetchone()
    if row is None:
        raise core.RiverError(f"this pipeline has no rule {ref!r}: maxpm pipeline triggers <pipeline> lists them")
    return row


def where_text(r):
    """Which stages a rule watches, in words."""
    if r["stage"]:
        return f"stage {r['stage']}"
    return f"any {r['tier']} stage" if r["tier"] else "any stage"


def when_text(r):
    words = {"stage-failed": "fails", "check-failed": "has a check that fails after a deploy",
             "transition-opened": "lets a version in", "run-started": "starts a run",
             "run-ended": "ends a run"}[r["event"]]
    return f"{where_text(r)} {words}".capitalize()


def _info(r):
    d = dict(r)
    d["name"] = f"t{r['id']}"
    d["when"] = when_text(r)
    d["keys"] = [k for k in (r["keys"] or "").split(",") if k]
    d["enabled"] = bool(r["enabled"])
    return d


def _check_fields(conn, p, event, stage, tier, runner, keys, actor):
    if event not in EVENTS:
        raise core.RiverError(f"the event {event!r} is not one of: {', '.join(EVENTS)}")
    if stage and tier:
        raise core.RiverError("name a stage or a tier, not both")
    if stage and not conn.execute("SELECT 1 FROM pipeline_stages WHERE pipeline_id=? AND name=? AND removed_at IS "
                                  "NULL", (p["id"], stage)).fetchone():
        names = [r["name"] for r in conn.execute("SELECT name FROM pipeline_stages WHERE pipeline_id=? AND removed_at "
                                                 "IS NULL ORDER BY pos", (p["id"],))]
        raise core.RiverError(f"pipeline {p['name']} has no stage {stage!r}; its stages: {', '.join(names) or 'none'}")
    if tier and not conn.execute("SELECT 1 FROM pipeline_stages WHERE pipeline_id=? AND tier=? AND removed_at IS NULL",
                                 (p["id"], tier)).fetchone():
        tiers = sorted({r["tier"] for r in conn.execute("SELECT tier FROM pipeline_stages WHERE pipeline_id=? AND tier "
                                                         "IS NOT NULL AND removed_at IS NULL", (p["id"],))})
        raise core.RiverError(f"no stage of pipeline {p['name']} has the tier {tier!r}; its tiers: "
                              f"{', '.join(tiers) or 'none yet (maxpm pipeline stage <name> <stage> --tier <tier>)'}")
    from .. import runners
    may = runners.choices(conn, p)
    if runner not in may:
        known = conn.execute("SELECT 1 FROM runners WHERE name=?", (runner,)).fetchone()
        raise core.RiverError(
            f"pipeline {p['name']} may not use runner {runner!r}: tick it first (maxpm runner allow {runner} "
            f"{p['name']})" if known else
            f"no runner {runner!r}: this pipeline's agents run on {', '.join(may)} (maxpm runner list)")
    from .. import vault
    for k in keys:
        vault._may_grant(conn, actor, vault._set(conn, k), "grant")  # the rule grants the set to each task later
    from . import keys as stage_keys
    stage_keys.rule_check(conn, p, keys, stage, tier, runner)  # #6153: a stage key only where its stages are


def _keys(text):
    if text is None:
        return None
    return [k.strip() for k in str(text).replace(" ", ",").split(",") if k.strip()]


def rule_add(conn, pipeline, event, does=None, stage=None, tier=None, runner=None, keys=None, actor=None):
    """maxpm pipeline trigger <pipeline> add: a new rule, on from now."""
    runner = runner or THIS
    keys = _keys(keys) or []
    does = (does or "").strip() or {"stage-failed": DEFAULT_DOES, "check-failed": CHECK_DOES}.get(event)
    if event in EVENTS and not does:
        raise core.RiverError(f"say what the agent does when {EVENTS[event]}: --does \"<instructions>\"")
    _may(conn, actor)
    p = store._pipeline(conn, pipeline)
    _check_fields(conn, p, event, stage or None, tier or None, runner, keys, actor)
    with core.tx(conn):
        at = store._now()
        rid = conn.execute("INSERT INTO pipeline_triggers(pipeline_id, event, stage, tier, does, runner, keys, keys_by, "
                           "since, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (p["id"], event, stage or None, tier or None, does, runner, ",".join(keys) or None,
                            (actor or "a person") if keys else None, at, actor, at)).lastrowid
        r = conn.execute("SELECT * FROM pipeline_triggers WHERE id=?", (rid,)).fetchone()
        _change(conn, p["id"], actor, f"trigger t{rid} added: {when_text(r)}: {does}" + _where_keys(r), r)
    return _info(r)


def _where_keys(r):
    return f" (runs on {r['runner']}" + (f"; keys {r['keys'].replace(',', ', ')}" if r["keys"] else "") + ")"


def _change(conn, pid, actor, text, r):
    from . import define
    define._change(conn, pid, actor, text, {k: r[k] for k in ("event", "stage", "tier", "does", "runner", "keys",
                                                              "enabled")} | {"rule": f"t{r['id']}"})


def rule_edit(conn, pipeline, ref, event=None, does=None, stage=None, tier=None, runner=None, keys=None, actor=None):
    """Change the fields given ('' clears the stage, the tier or the keys)."""
    _may(conn, actor)
    p = store._pipeline(conn, pipeline)
    r = _rule(conn, p["id"], ref)
    new = {"event": event or r["event"], "does": (does.strip() if does is not None else r["does"]),
           "stage": r["stage"] if stage is None else (stage or None), "tier": r["tier"] if tier is None else (tier or None),
           "runner": runner or r["runner"], "keys": r["keys"] if keys is None else (",".join(_keys(keys)) or None)}
    if stage:
        new["tier"] = None
    if tier:
        new["stage"] = None
    if not new["does"]:
        raise core.RiverError("say what the agent does: --does \"<instructions>\"")
    changed = {k: v for k, v in new.items() if v != r[k]}
    if not changed:
        raise core.RiverError(f"rule t{r['id']} has these values already; give what to change: --when, --stage, "
                              "--tier, --does, --runs-on or --keys")
    added = [k for k in _keys(new["keys"] or "") if k not in _keys(r["keys"] or "")]
    _check_fields(conn, p, new["event"], new["stage"], new["tier"], new["runner"], added, actor)
    from . import keys as stage_keys  # #6153: a new stage or runner must fit the keys the rule had too
    stage_keys.rule_check(conn, p, _keys(new["keys"] or ""), new["stage"], new["tier"], new["runner"])
    sets = dict(changed, keys_by=(actor or "a person") if new["keys"] else None) if "keys" in changed else changed
    with core.tx(conn):
        conn.execute("UPDATE pipeline_triggers SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?",
                     [*sets.values(), r["id"]])
        r = conn.execute("SELECT * FROM pipeline_triggers WHERE id=?", (r["id"],)).fetchone()
        _change(conn, p["id"], actor, f"trigger t{r['id']} changed: " + "; ".join(
            f"{k} {v or 'none'}" for k, v in changed.items()), r)
    return _info(r)


def rule_switch(conn, pipeline, ref, on, reason=None, actor=None):
    """Turn a rule off (it acts on nothing; a reason is asked) or on again (it acts on what happens from now)."""
    _may(conn, actor)
    p = store._pipeline(conn, pipeline)
    r = _rule(conn, p["id"], ref)
    if bool(r["enabled"]) == on:
        raise core.RiverError(f"rule t{r['id']} is {'on' if on else 'off'} already")
    if not on and not (reason or "").strip():
        raise core.RiverError("say why the rule goes off: --reason \"<why>\"")
    with core.tx(conn):
        at = store._now()
        if on:
            conn.execute("UPDATE pipeline_triggers SET enabled=1, since=?, off_reason=NULL, off_by=NULL, off_at=NULL "
                         "WHERE id=?", (at, r["id"]))
        else:
            conn.execute("UPDATE pipeline_triggers SET enabled=0, off_reason=?, off_by=?, off_at=? WHERE id=?",
                         (reason.strip(), actor, at, r["id"]))
        r = conn.execute("SELECT * FROM pipeline_triggers WHERE id=?", (r["id"],)).fetchone()
        _change(conn, p["id"], actor, f"trigger t{r['id']} turned {'on' if on else 'off'}"
                + ("" if on else f": {reason.strip()}"), r)
    return _info(r)


def rule_remove(conn, pipeline, ref, actor=None):
    _may(conn, actor)
    p = store._pipeline(conn, pipeline)
    r = _rule(conn, p["id"], ref)
    with core.tx(conn):
        conn.execute("DELETE FROM pipeline_triggers WHERE id=?", (r["id"],))
        _change(conn, p["id"], actor, f"trigger t{r['id']} removed: {when_text(r)}: {r['does']}", r)
    return {"pipeline": p["name"], "removed": f"t{r['id']}"}


def add_default(conn, pid, actor=None):
    """The rule a new pipeline starts with (inside the caller's tx)."""
    at = store._now()
    conn.execute("INSERT INTO pipeline_triggers(pipeline_id, event, does, since, created_by, created_at) VALUES "
                 "(?,?,?,?,?,?)", (pid, *DEFAULT_RULE, at, actor or "maxpm", at))


def rules(conn, pipeline, fired=20):
    """The rules of a pipeline, what they did lately, and the choices of a rule's fields (the Triggers tab)."""
    from .. import runners
    p = store._pipeline(conn, pipeline)
    stages = conn.execute("SELECT * FROM pipeline_stages WHERE pipeline_id=? AND removed_at IS NULL ORDER BY pos, id",
                          (p["id"],)).fetchall()
    ticks = runners.ticked(p)
    every = [{"name": r["name"], "ssh": r["ssh"], "state": r["state"], "cli": r["cli"], "ticked": r["name"] in ticks}
             for r in conn.execute("SELECT name, ssh, state, cli FROM runners ORDER BY name")]
    from . import keys as stage_keys
    from . import permissions
    return {"pipeline": p["name"], "project": p["project"],
            "permissions": permissions.show(conn, p["name"]),  # #6154: what its agents may do with no prompt
            # key_list (#6153): each set with the stages that may read it (None: any), then the runner's sets
            "rules": [dict(_info(r), key_list=stage_keys.rule_keys(conn, p["id"], _keys(r["keys"] or ""), r["runner"]))
                      for r in conn.execute("SELECT * FROM pipeline_triggers WHERE pipeline_id=? ORDER BY id",
                                            (p["id"],))],
            "fired": [dict(f, rule=f"t{f['trigger_id']}") for f in conn.execute(
                "SELECT * FROM pipeline_trigger_fires WHERE pipeline_id=? ORDER BY id DESC LIMIT ?", (p["id"], fired))],
            "events": EVENTS, "runners": runners.choices(conn, p), "all_runners": every,
            "stages": [s["name"] for s in stages],
            "tiers": sorted({s["tier"] for s in stages if s["tier"]})}


def export(conn, pid):
    """The rules as the export of the pipeline keeps them."""
    return [export_one(r) for r in conn.execute("SELECT * FROM pipeline_triggers WHERE pipeline_id=? ORDER BY id",
                                                (pid,))]


def export_one(r):
    """One rule as the export keeps it (a version of the definition compares rules in this form, #6143)."""
    return ({k: r[k] for k in ("event", "stage", "tier", "does", "runner")}
            | {"keys": _keys(r["keys"] or ""), "on": bool(r["enabled"])})


# ---------------------------------------------------------------- the generic events

def _runs(conn, p, stages, since):
    """The events of the stage runs: each source's runs are rows of stage_runs with the same states (store.ingest)."""
    out = []
    for r in conn.execute("SELECT r.*, v.rev FROM stage_runs r LEFT JOIN pipeline_versions v ON v.id=r.version_id "
                          "WHERE r.pipeline_id=? AND (r.started_at>=? OR r.ended_at>=? OR r.approved_at>=? OR "
                          "r.passed_at>=?) ORDER BY r.id", (p["id"], since, since, since, since)):
        s = stages.get(r["stage_id"])
        if s is None:
            continue
        base = {"run_id": r["id"], "stage": s["name"], "tier": s.get("tier"), "rev": r["rev"]}
        # The transition let the version in: a yes (approved_at), a pass of its controls (passed_at, #6139), or the
        # run's start when the transition let it in by itself.
        entered = min(t for t in (r["approved_at"], r["passed_at"], r["started_at"]) if t) \
            if (r["approved_at"] or r["passed_at"] or r["started_at"]) else None
        who = r["approved_by"] or r["passed_by"]
        if entered and entered >= since:
            out.append(dict(base, event="transition-opened", key=f"transition-opened:{r['id']}", at=entered,
                            text=f"version {_short(r['rev'])} went into stage {s['name']}"
                                 + (f" (let in by {who})" if who else "")))
        if r["started_at"] and r["started_at"] >= since:
            out.append(dict(base, event="run-started", key=f"run-started:{r['id']}", at=r["started_at"],
                            text=f"run #{r['id']} of stage {s['name']} started for {_short(r['rev'])}"))
        if r["state"] in ("passed", "failed", "cancelled") and r["ended_at"] and r["ended_at"] >= since:
            out.append(dict(base, event="run-ended", key=f"run-ended:{r['id']}", at=r["ended_at"], state=r["state"],
                            text=f"run #{r['id']} of stage {s['name']} ended {r['state']} for {_short(r['rev'])}"
                                 + (f": {r['errors']}" if r["errors"] else "")))
            if r["state"] == "failed" and not (r["purpose"] or "").startswith("failed:"):
                out.append(dict(base, event="stage-failed", key=f"stage-failed:{r['id']}", at=r["ended_at"],
                                text=f"stage {s['name']} failed for {_short(r['rev'])}: {r['errors'] or 'failed'}"))
    return out


def _checks(conn, p, stages, since):
    """check-failed: a check of an environment's target, or of the pipeline's project, became a fault after a
    version went to that environment (within WATCH, and before the next version went there)."""
    envs = {e["id"]: e for e in conn.execute("SELECT * FROM pipeline_envs WHERE pipeline_id=? AND removed_at IS NULL",
                                             (p["id"],))}
    targets = sorted({e["target"] for e in envs.values() if e["target"]})
    if not envs or not (targets or p["project"]):
        return []
    q = ",".join("?" * len(targets)) or "NULL"
    faults = conn.execute(
        f"SELECT e.id, e.at, e.key, e.note, c.scope_kind, c.scope, c.name FROM ops_events e JOIN checks c ON "
        f"c.id=e.check_id WHERE e.state='fault' AND e.at>=? AND ((c.scope_kind='target' AND c.scope IN ({q})) OR "
        f"(c.scope_kind='project' AND c.scope=?)) ORDER BY e.id", (since, *targets, p["project"])).fetchall()
    out = []
    for f in faults:
        ids = [i for i, e in envs.items() if f["scope_kind"] == "project" or e["target"] == f["scope"]]
        start = core.iso(core.parse_iso(f["at"]) - core.timedelta(seconds=WATCH))
        d = conn.execute(f"SELECT d.*, v.rev FROM pipeline_deploys d LEFT JOIN pipeline_versions v ON "
                         f"v.id=d.version_id WHERE d.env_id IN ({','.join('?' * len(ids))}) AND d.at<=? AND d.at>=? "
                         f"AND d.kind<>'removed' ORDER BY d.at DESC, d.id DESC LIMIT 1",
                         (*ids, f["at"], start)).fetchone() if ids else None
        if d is None:
            continue
        later = conn.execute("SELECT 1 FROM pipeline_deploys WHERE env_id=? AND at>? AND at<=? AND id<>?",
                             (d["env_id"], d["at"], f["at"], d["id"])).fetchone()
        if later:
            continue
        s = None
        if d["run_id"]:
            r = conn.execute("SELECT stage_id FROM stage_runs WHERE id=?", (d["run_id"],)).fetchone()
            s = stages.get(r["stage_id"]) if r else None
        if s is None:
            s = next((x for x in stages.values() if x["env_id"] == d["env_id"]), None)
        if s is None:
            continue
        env = envs[d["env_id"]]["name"]
        out.append({"event": "check-failed", "key": f"check-failed:{f['id']}", "at": f["at"], "run_id": d["run_id"],
                    "stage": s["name"], "tier": s.get("tier"), "rev": d["rev"], "check": f["key"],
                    "text": f"check {f['scope']}/{f['name']} failed at {f['at']}, after version {_short(d['rev'])} "
                            f"went to {env} at {d['at']}" + (f": {f['note']}" if f["note"] else "")})
    return out


def _short(rev):
    return (rev or "?")[:12]


def events(conn, p, since):
    """Every generic event of one pipeline from since on, oldest first."""
    stages = {s["id"]: dict(s) for s in conn.execute("SELECT * FROM pipeline_stages WHERE pipeline_id=?", (p["id"],))}
    return sorted(_runs(conn, p, stages, since) + _checks(conn, p, stages, since), key=lambda e: (e["at"], e["key"]))


def _matches(r, ev):
    return (r["event"] == ev["event"] and ev["at"] >= r["since"] and (not r["stage"] or r["stage"] == ev["stage"])
            and (not r["tier"] or r["tier"] == ev.get("tier")))


# ---------------------------------------------------------------- acting

def serve_pass(conn):
    """One pass of maxpm serve (from runner.serve_pass): each rule that is on acts once on each new event it
    matches. Returns the rows of pipeline_trigger_fires it added."""
    floor = core.iso(core.now() - core.timedelta(seconds=WITHIN))
    out = []
    for p in conn.execute("SELECT * FROM pipelines WHERE id IN (SELECT pipeline_id FROM pipeline_triggers WHERE "
                          "enabled=1) ORDER BY id").fetchall():
        rs = conn.execute("SELECT * FROM pipeline_triggers WHERE pipeline_id=? AND enabled=1 ORDER BY id",
                          (p["id"],)).fetchall()
        since = max(floor, min(r["since"] for r in rs))
        for ev in events(conn, p, since):
            for r in rs:
                if _matches(r, ev):
                    fire = _fire(conn, p, r, ev)
                    if fire:
                        out.append(fire)
    return out


def _fire(conn, p, r, ev):
    """The rule r acts on the event ev, once: a row of pipeline_trigger_fires first (the claim), then the act."""
    with core.tx(conn):
        cur = conn.execute("INSERT OR IGNORE INTO pipeline_trigger_fires(trigger_id, pipeline_id, key, event, at, "
                           "fired_at, run_id, stage, rev) VALUES (?,?,?,?,?,?,?,?,?)",
                           (r["id"], p["id"], ev["key"], ev["event"], ev["at"], store._now(), ev.get("run_id"),
                            ev["stage"], ev.get("rev")))
        if not cur.rowcount:
            return None
        fid = cur.lastrowid
    try:
        item, result = _act(conn, p, r, ev)
    except core.RiverError as e:
        item, result = None, f"did nothing: {e}"
    if item and r["keys"]:
        result += _grant(conn, r, item, ev["stage"])
    if item and r["runner"] and r["runner"] != THIS:  # #6151: the task goes to a runner, not to a session here
        from .. import runners
        from . import agent_job
        try:
            if r["runner"] in agent_job.FORGES:  # #6152: the forge starts the agent in its own CI job
                result += agent_job.queue(conn, p, r, item, ev)
            else:
                result += runners.queue(conn, r["runner"], item, p["name"], f"t{r['id']}", ev.get("rev"))
        except core.RiverError as e:
            result += f"; not on runner {r['runner']}: {e}"
    with core.tx(conn):
        conn.execute("UPDATE pipeline_trigger_fires SET item=?, result=? WHERE id=?", (item, result, fid))
        from . import audit
        action = "fault" if ev["event"] in ("stage-failed", "check-failed") else "task"
        audit.write(conn, p["id"], f"trigger t{r['id']}", action, f"{ev['text']}: {result}", kind="trigger",
                    place=f"stage {ev['stage']}", rev=ev.get("rev"), run_id=ev.get("run_id"), result=result,
                    detail={"rule": f"t{r['id']}", "event": ev["event"], "key": ev["key"], "item": item})
    return dict(conn.execute("SELECT * FROM pipeline_trigger_fires WHERE id=?", (fid,)).fetchone())


def _act(conn, p, r, ev):
    """(item, result in words)."""
    name = f"t{r['id']}"
    if not p["project"] or not conn.execute("SELECT 1 FROM projects WHERE name=?", (p["project"],)).fetchone():
        return None, "no project to hold the task: the pipeline names none (maxpm pipeline add ... --project)"
    if ev["event"] == "stage-failed":
        item, how = links.fault_for(conn, ev["run_id"], r["does"], name)
        return item, f"{how} fault #{item}" if item else how
    if ev["event"] == "check-failed":
        it = core._ops_item(conn, ev["check"])
        if it:
            text = f"Trigger rule {name} of pipeline {p['name']}: {ev['text']}.\nWhat to do: {r['does']}"
            with core.tx(conn):
                links.add_does(conn, it["id"], name, r["does"], ev["text"])
                core._send(conn, "note", f"trigger {name}", text, item_id=it["id"])
            return it["id"], f"added its words to fault #{it['id']}"
    item = core.item_add(conn, p["project"], f"pipeline:{p['name']}:{ev['stage']} {EVENTS[ev['event']]} for "
                         f"{_short(ev.get('rev'))}: {r['does'][:100]}", doer="ai", kind="operational", actor="maxpm",
                         goals=[], short=_short_title(ev), context=_context(p, r, ev))
    return item["id"], f"added task #{item['id']}"


_SHORT = {"check-failed": "check failed after deploy", "transition-opened": "version let in",
          "run-started": "run started", "run-ended": "run ended"}


def _short_title(ev):
    """The short title of a task (at most 8 words and 50 characters): the stage and what happened."""
    text = f"{ev['stage']} {_SHORT[ev['event']]}"
    return text if len(text) <= 50 else _SHORT[ev["event"]].capitalize()


def _context(p, r, ev):
    run = f"maxpm pipeline run {ev['run_id']}" if ev.get("run_id") else f"maxpm pipeline show {p['name']}"
    return (f"Trigger rule t{r['id']} of pipeline {p['name']} ({when_text(r).lower()}): {ev['text']}.\n\n"
            f"What to do: {r['does']}\n\nThe run: {run}. The pipeline: maxpm pipeline show {p['name']}.")


def _grant(conn, r, item, stage=None):
    """The rule's keys for its task: each set granted to the task alone (it ends when the task closes). The person
    or the manager who named a set on the rule was checked then (vault._may_grant). A stage key of other stages of
    the pipeline only is held back (#6153): an agent on a stage gets only that stage's keys."""
    from .. import vault
    from . import keys as stage_keys
    got, missing, held = [], [], []
    for k in (r["keys"] or "").split(","):
        if not k:
            continue
        why = stage_keys.held_back(conn, r["pipeline_id"], k, stage)
        if why:
            held.append(why)
            continue
        try:
            vault.grant(conn, k, "item", str(item), note=f"trigger rule t{r['id']}", actor=f"trigger t{r['id']}",
                        checked_by=r["keys_by"] or "a person")
            got.append(k)
        except core.RiverError:
            missing.append(k)
    return (("; keys " + ", ".join(got) if got else "") + ("; no set " + ", ".join(missing) if missing else "")
            + ("; held back: " + "; ".join(held) if held else ""))
