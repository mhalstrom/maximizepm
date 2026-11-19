"""The audit log of pipelines (#6144): one list of every change to a pipeline and its flow.

mark 2026-11-18 (canvas release-tracking, task M): "we need an audit log and an emergency actions tab in the deploy
tab". Each line says who (a person, an agent session, a trigger, a hook, a forge), what (approve, decline, hold, end
a hold, roll back, run again, stop, start, a definition change, a stage's end, a deploy, a fault, a token or grant
change), on which pipeline and stage or transition, when, why, and the result.

Where the lines come from:
- pipeline_changes: a line is written when the thing happens, inside the same transaction: the definition (define.py),
  each action (runner.py), the end of each stage run and each deploy (store.py, from the events of the runner and of
  the forges), a fault item and a fix task (links.py). Triggers in the schema refuse an UPDATE or a DELETE of a line:
  a mistake gets a new line that corrects it (`correct`).
- secret_log and secret_grants (river/vault.py): the changes of each token that a pipeline names, and the grants of
  the sets that hold it. The vault keeps those itself; this module reads them, so a pipeline that names a token later
  also shows that token's earlier changes.

Readers: maxpm pipeline audit, GET /api/pipeline-audit (the Deploy tab's Audit log and each pipeline's own tab), the
export of the page.
"""

import json
from datetime import datetime, timezone

from .. import core

KINDS = ("person", "agent", "trigger", "hook", "forge")
MAX = 500  # the lines of one read
EXPORT_MAX = 10000  # the lines of one export
FORGE_NAMES = {"github": "GitHub", "gitlab": "GitLab", "gitea": "Gitea", "forgejo": "Forgejo",
               "pulumi": "Pulumi Delivery"}
# The token changes that go in the log (a use or a refusal is not a change).
TOKEN_ACTIONS = {"put": "saved the token", "replace": "replaced the token", "delete": "deleted the token",
                 "level": "changed who may use the token", "set-add": "added the token to a set",
                 "set-remove": "took the token out of a set", "ask": "changed how a grant of the token is asked"}

# The kind of a line's actor, for a line that does not keep one (written before #6144).
_KIND_SQL = ("COALESCE(c.kind, CASE WHEN c.actor IS NULL THEN 'person' WHEN c.actor='maxpm' THEN 'trigger' "
             "WHEN a.kind='ai' THEN 'agent' ELSE 'person' END)")


def kind_of(conn, actor):
    """person, agent or trigger: who an actor of a command or a page action is. None is a person at the terminal or
    the page; maxpm is MaximizePM itself (a rule that acts)."""
    if actor is None:
        return "person"
    if actor == "maxpm":
        return "trigger"
    r = conn.execute("SELECT kind FROM agents WHERE name=?", (actor,)).fetchone()
    return "agent" if r and r["kind"] == "ai" else "person"


def write(conn, pipeline_id, actor, action, change, kind=None, place=None, rev=None, run_id=None, reason=None,
          result=None, detail=None, corrects=None, until=None):
    """Add one line (inside the caller's tx). action is a short word (approve, hold, passed, deployed, ...); change
    is the line in words; place is what it acts on (stage prod, the transition into prod, environment beta, every
    stage); until: a block, delay or freeze ends then."""
    conn.execute("INSERT INTO pipeline_changes(pipeline_id, at, actor, change, detail, kind, action, place, rev, "
                 "run_id, reason, result, corrects, until_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (pipeline_id, core.iso(core.now()), actor, change,
                  json.dumps(detail, separators=(",", ":")) if detail else None, kind or kind_of(conn, actor), action,
                  place, rev, run_id, reason, result, corrects, until))


def run_end(conn, run_id, state, errors=None):
    """The end of a stage run as a line (inside the ingest's tx): who ran it (the stage's command here, or the forge),
    passed, failed or stopped, and for a deploy stage the version that now runs in its environment. A run that a
    person or a session stopped or declined has that line already, with its actor."""
    r = conn.execute("SELECT r.pipeline_id, r.source, r.purpose, s.name stage, s.kind, s.run cmd, e.name env, v.rev "
                     "FROM stage_runs r JOIN pipeline_stages s ON s.id=r.stage_id LEFT JOIN pipeline_envs e ON "
                     "e.id=s.env_id LEFT JOIN pipeline_versions v ON v.id=r.version_id WHERE r.id=?",
                     (run_id,)).fetchone()
    if r is None:
        return
    if state == "cancelled" and conn.execute("SELECT 1 FROM pipeline_changes WHERE run_id=? AND action IN "
                                             "('stop','decline')", (run_id,)).fetchone():
        return
    src = r["source"] or "maxpm"
    if src != "maxpm":
        actor, kind = FORGE_NAMES.get(src, src), "forge"
    elif state == "cancelled" or not r["cmd"]:
        actor, kind = "maxpm", "trigger"  # a newer version took its place, or a stage with no command
    else:
        cmd = r["cmd"].strip().splitlines()[0] if r["cmd"].strip() else r["cmd"]
        actor, kind = (cmd[:57] + "..." if len(cmd) > 60 else cmd), "hook"  # the stage's command ran it here
    rev = r["rev"] or ""
    if state == "passed" and r["kind"] == "deploy" and r["env"]:
        back = r["purpose"] == "rollback"
        action = "rolled back" if back else "deployed"
        change = f"{'put' if back else 'deployed'} {rev} {'back on' if back else 'to'} environment {r['env']}"
    else:
        action = {"passed": "passed", "failed": "failed", "cancelled": "stopped", "skipped": "skipped"}.get(state, state)
        change = f"stage {r['stage']} {action} for {rev}" + (f": {errors}" if errors else "")
    write(conn, r["pipeline_id"], actor, action, change, kind=kind, place=f"stage {r['stage']}", rev=rev or None,
          run_id=run_id, reason=errors)


def deploy_event(conn, pipeline_id, source, env, rev, kind):
    """A version that an outside system put in an environment with no stage run of ours (a forge's deployment)."""
    write(conn, pipeline_id, FORGE_NAMES.get(source, source), kind,
          f"{kind} {rev or 'nothing'} {'from' if kind == 'removed' else 'in'} environment {env}",
          kind="trigger" if source == "maxpm" else "forge", place=f"environment {env}", rev=rev)


# ---------------------------------------------------------------- corrections

def correct(conn, line_id, text, actor=None):
    """A correction of a line: a new line that names it (the old one stays). The actor of that line, a person or the
    manager may add it."""
    text = (text or "").strip()
    if not text:
        raise core.RiverError("say what is wrong in the line and what is right: maxpm pipeline correct <line> "
                              "\"<text>\"")
    with core.tx(conn):
        old = conn.execute("SELECT * FROM pipeline_changes WHERE id=?", (line_id,)).fetchone()
        if old is None:
            raise core.RiverError(f"no line {line_id} in the audit log: maxpm pipeline audit lists them")
        if actor != old["actor"] or actor is None:
            why = core._lead_refusal(conn, actor, "corrects a line of another actor in the audit log")
            if why:
                raise core.RiverError(why)
        write(conn, old["pipeline_id"], actor, "correct", f"corrects line {line_id}: {text}", place=old["place"],
              rev=old["rev"], reason=text, corrects=line_id)
        new = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return {"line": new, "corrects": line_id}


# ---------------------------------------------------------------- reading

def _since(conn, since):
    """The UTC time from which lines show: today (from midnight here), a duration back (7d), or a date."""
    if not since or since == "all":
        return None
    if since == "today":
        zone = core.local_zone(core.setting(conn, "timezone"))
        day = datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0)
        return core.iso(day.astimezone(timezone.utc))
    try:
        return core.iso(core.now() - core.parse_duration(since))
    except core.RiverError:
        pass
    try:
        return core.iso(core.parse_when(since, core.setting(conn, "timezone")))
    except (core.RiverError, ValueError) as e:
        raise core.RiverError(f"--since takes today, a time back (7d, 2h) or a date (2026-11-01), not {since!r}") from e


def _who(who):
    """(kinds, name): a list of kinds (person, agent, trigger, hook, forge; a comma list) or one actor's name."""
    if not who or who == "all":
        return None, None
    parts = [w.strip() for w in str(who).split(",") if w.strip()]
    if parts and all(p in KINDS for p in parts):
        return parts, None
    return None, str(who).strip()


def _result(row):
    """What came of a line: the state of the run it started or ended, now, else what the line itself says. An outside
    system's question (#6164) has its answer: yes or no."""
    if not row["run_state"] or row["action"] == "ask":
        return row["result"]
    out = row["run_state"]
    if row["run_started"] and row["run_ended"]:
        secs = int((core.parse_iso(row["run_ended"]) - core.parse_iso(row["run_started"])).total_seconds())
        out += f", {secs // 60}m" if secs >= 60 else f", {secs}s"
    if row["fault_item"]:
        out += f", fault #{row['fault_item']}"
    return out


def _matches(words, *texts):
    hay = " ".join(t for t in texts if t).lower()
    return all(w in hay for w in words)


def _token_lines(conn, pipeline, since, kinds, name, words, limit):
    """The changes of each token that a pipeline names, and the grants of the sets that hold it."""
    q, args = "SELECT name, secret FROM pipelines WHERE secret IS NOT NULL AND secret<>''", []
    if pipeline:
        q, args = q + " AND name=?", [pipeline]
    named = {}
    for p in conn.execute(q, args):
        named.setdefault(p["secret"], []).append(p["name"])
    if not named:
        return []
    marks = ",".join("?" * len(named))
    out = []
    for r in conn.execute(f"SELECT id, name, at, actor, action, detail FROM secret_log WHERE name IN ({marks}) AND "
                          f"action IN ({','.join('?' * len(TOKEN_ACTIONS))})" + (" AND at>=?" if since else "") +
                          " ORDER BY id DESC LIMIT ?", [*named, *TOKEN_ACTIONS, *([since] if since else []), limit]):
        actor = None if r["actor"] == "a person" else r["actor"]
        out.append({"id": f"s{r['id']}", "at": r["at"], "pipeline": ", ".join(named[r["name"]]),
                    "actor": r["actor"], "kind": kind_of(conn, actor), "action": "token",
                    "change": f"{TOKEN_ACTIONS[r['action']]} {r['name']}" + (f" ({r['detail']})" if r["detail"] else ""),
                    "place": f"token {r['name']}", "rev": None, "reason": None, "result": None, "run": None,
                    "corrects": None})
    grants = conn.execute(
        f"SELECT g.*, s.name set_name, m.name secret FROM secret_grants g JOIN secret_sets s ON s.id=g.set_id JOIN "
        f"secret_set_members m ON m.set_id=g.set_id WHERE m.name IN ({marks})", list(named)).fetchall()
    for g in grants:
        for at, by, verb in ((g["granted_at"], g["granted_by"], "granted"), (g["revoked_at"], g["revoked_by"], "revoked")):
            if not at or (since and at < since):
                continue
            out.append({"id": f"g{g['id']}{verb[0]}", "at": at, "pipeline": ", ".join(named[g["secret"]]),
                        "actor": by or "a person", "kind": kind_of(conn, by), "action": "grant",
                        "change": f"{verb} the set {g['set_name']} (token {g['secret']}) to {g['scope']} {g['value']}",
                        "place": f"token {g['secret']}", "rev": None, "reason": g["note"] if verb == "granted" else None,
                        "result": None, "run": None, "corrects": None})
    return [l for l in out if (not kinds or l["kind"] in kinds) and (not name or l["actor"] == name)
            and _matches(words, l["actor"], l["change"], l["place"], l["pipeline"], l["reason"])]


def lines(conn, pipeline=None, who=None, since=None, search=None, limit=MAX):
    """The lines of the audit log, newest first: of every pipeline or of one; of a kind of actor (person, agent,
    trigger, hook, forge; a comma list) or one actor's name; since a time (today, 7d, a date); with every word of
    search. Returns {"lines": [...], "more": True when there were more than limit}."""
    if pipeline:
        from . import store
        try:
            store._pipeline(conn, pipeline)  # #6176: a deploy target's controls have a row from their first use
        except core.RiverError:
            raise core.RiverError(f"no pipeline {pipeline}: maxpm pipeline list") from None
    limit = max(1, min(int(limit or MAX), EXPORT_MAX))
    at = _since(conn, since)
    kinds, name = _who(who)
    words = [w.lower() for w in str(search or "").split() if w]
    sql = (f"SELECT c.id, c.at, c.actor, {_KIND_SQL} kind, c.action, c.change, c.place, c.rev, c.reason, c.result, "
           "c.until_at, "
           "c.run_id, c.corrects, p.name pipeline, r.state run_state, r.started_at run_started, r.ended_at run_ended, "
           "r.fault_item FROM pipeline_changes c JOIN pipelines p ON p.id=c.pipeline_id LEFT JOIN agents a ON "
           "a.name=c.actor LEFT JOIN stage_runs r ON r.id=c.run_id WHERE 1=1")
    args = []
    if pipeline:
        sql, args = sql + " AND p.name=?", args + [pipeline]
    if at:
        sql, args = sql + " AND c.at>=?", args + [at]
    if kinds:
        sql, args = sql + f" AND {_KIND_SQL} IN ({','.join('?' * len(kinds))})", args + kinds
    if name:
        sql, args = sql + " AND c.actor=?", args + [name]
    for w in words:
        sql += (" AND lower(COALESCE(c.change,'') || ' ' || COALESCE(c.actor,'') || ' ' || COALESCE(c.place,'') || ' ' "
                "|| COALESCE(c.rev,'') || ' ' || COALESCE(c.reason,'') || ' ' || COALESCE(c.action,'') || ' ' || p.name) "
                "LIKE ?")
        args.append(f"%{w}%")
    rows = conn.execute(sql + " ORDER BY c.id DESC LIMIT ?", args + [limit + 1]).fetchall()
    out = [{"id": r["id"], "at": r["at"], "pipeline": r["pipeline"], "actor": r["actor"] or "a person",
            "kind": r["kind"], "action": r["action"], "change": r["change"], "place": r["place"], "rev": r["rev"],
            "reason": r["reason"], "result": _result(r), "run": r["run_id"], "corrects": r["corrects"],
            "until": r["until_at"]} for r in rows]
    out += _token_lines(conn, pipeline, at, kinds, name, words, limit + 1)
    out.sort(key=lambda l: (l["at"], str(l["id"]).isdigit(), l["id"] if isinstance(l["id"], int) else 0),
             reverse=True)
    more = len(out) > limit
    out = out[:limit]
    ids = [l["id"] for l in out if isinstance(l["id"], int)]
    if ids:
        fixed = {}
        for r in conn.execute(f"SELECT id, corrects FROM pipeline_changes WHERE corrects IN "
                              f"({','.join('?' * len(ids))})", ids):
            fixed.setdefault(r["corrects"], []).append(r["id"])
        for l in out:
            if l["id"] in fixed:
                l["corrected_by"] = fixed[l["id"]]
    return {"lines": out, "more": more}
