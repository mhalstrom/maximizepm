"""Operations areas, playbooks, triggers and events (#6625, plan: canvas operations-tab).

A project has areas (its general area, each deploy target, an environment, a host). An area has triggers; each
trigger has one playbook of the project, and a playbook serves one or more triggers. A trigger starts work when a
check changes state (condition state: today's checks), when a time comes (time), or when an outside system reports
an event (event); MaximizePM polls its routine (arrival poll) or the outside system posts to it (push). Each event
has an id and a title; a repeat of an id is counted, not recorded again.

The tables are in core.SCHEMA (ops_areas, ops_playbooks, ops_playbook_proposals, triggers, trigger_events); the
checks of the fault path are the view core.checks over the state triggers that poll. This module holds the maxpm ops
area|playbook|trigger|events commands. The handling of an event (one item, one fresh session) is #6627; the routine
of a poll trigger is #6626; the address of a push trigger is #6628.
"""

from datetime import timedelta
import json
import os
import time

from . import core
from .core import RiverError, iso, now, tx, _event

AREA_FIELDS = ("description", "place", "target")
PLAYBOOK_TEXT = ("knowledge", "done_means")  # an agent proposes them; the manager accepts (#6641)
PLAYBOOK_PERSON = ("tools", "limits", "repair")  # only a person changes them (#6641)
PLAYBOOK_FIELDS = ("name",) + PLAYBOOK_TEXT + PLAYBOOK_PERSON + ("model", "effort", "workdir")
EVENT_MAX = 20000  # the characters of an event's data that MaximizePM keeps


def _who(conn, actor):
    """person, manager or agent: a name that is no agent (a person at the terminal, the page) is a person."""
    r = conn.execute("SELECT kind, role FROM agents WHERE name=?", (actor,)).fetchone() if actor else None
    if r is None or r["kind"] == "human":
        return "person"
    return "manager" if r["role"] == "manager" else "agent"


def _need(conn, actor, what):
    """Refuse an agent that is not the manager: areas and triggers decide what starts sessions."""
    if _who(conn, actor) == "agent":
        raise RiverError(f"{actor} is an agent; only a person or the manager {what}. Ask the manager: "
                         f"{core.as_cmd(actor)} ask manager \"<what and why>\"")


def _pid(conn, project):
    return core._project(conn, project)["id"]


def _area(conn, project, name):
    p = core._project(conn, project)
    r = conn.execute("SELECT * FROM ops_areas WHERE project_id=? AND name=?", (p["id"], name)).fetchone()
    if r is None:
        names = [x["name"] for x in conn.execute("SELECT name FROM ops_areas WHERE project_id=? ORDER BY name",
                                                 (p["id"],))]
        raise RiverError(f"{p['name']} has no area {name!r}; its areas: {', '.join(names) or 'none'} "
                         f"(maxpm ops area add {p['name']} {name})")
    return dict(r)


def _name(name, what):
    if not core._OPS_NAME.match(name or ""):
        raise RiverError(f"{what} names use lower-case letters, digits, '.', '_', '-'")
    return name


# ---- areas

def area_add(conn, project, name, description="", place=None, target=None, actor=None):
    """maxpm ops area add: a part of a project that triggers watch (decision 8): a name, and optionally a
    description, its folder or host, and the deploy target it is."""
    _name(name, "area")
    with tx(conn):
        _need(conn, actor, "adds an area")
        pid = _pid(conn, project)
        if conn.execute("SELECT 1 FROM ops_areas WHERE project_id=? AND name=?", (pid, name)).fetchone():
            raise RiverError(f"{project} has an area {name!r}; change it: maxpm ops area edit {project} {name} ...")
        if target:
            target = core._target(conn, target)["name"]
        core._area_insert(conn, pid, name, actor, description or "", place or None, target or None)
        _event(conn, None, actor, f"ops area {project}/{name} added" + (f" (deploy target {target})" if target else ""))
    return {"area": area_show(conn, project, name), "changed": "added"}


def area_edit(conn, project, name, fields, actor=None):
    """maxpm ops area edit: its description, its folder or host, its new name. Its deploy target stays: the scope of
    its triggers follows it."""
    with tx(conn):
        _need(conn, actor, "changes an area")
        a = _area(conn, project, name)
        sets = {k: v for k, v in fields.items() if v is not None and k in ("description", "place", "name")}
        if not sets:
            raise RiverError("give a change: --description, --place or --name")
        if "name" in sets:
            _name(sets["name"], "area")
            if sets["name"] != name and conn.execute("SELECT 1 FROM ops_areas WHERE project_id=? AND name=?",
                                                     (a["project_id"], sets["name"])).fetchone():
                raise RiverError(f"{project} has an area {sets['name']!r} already")
        if "place" in sets:
            sets["place"] = sets["place"].strip() or None
        conn.execute(f"UPDATE ops_areas SET {', '.join(k + '=?' for k in sets)}, changed_by=?, changed_at=? WHERE id=?",
                     (*sets.values(), actor, iso(now()), a["id"]))
        _event(conn, None, actor, f"ops area {project}/{name} changed ({', '.join(sets)})")
    return {"area": area_show(conn, project, sets.get("name", name)), "changed": "changed"}


def _area_row(conn, r, project=None):
    d = dict(r)
    d["project"] = project or core._project_name(conn, d["project_id"])
    d["triggers"] = conn.execute("SELECT COUNT(*) n FROM triggers WHERE area_id=?", (d["id"],)).fetchone()["n"]
    return d


def area_list(conn, project=None):
    """The areas of a project, or of every project, each with its count of triggers."""
    if project:
        p = core._project(conn, project)
        rows = conn.execute("SELECT * FROM ops_areas WHERE project_id=? ORDER BY name", (p["id"],))
    else:
        rows = conn.execute("SELECT a.* FROM ops_areas a JOIN projects p ON p.id=a.project_id ORDER BY p.rank, p.name, "
                            "a.name")
    return {"areas": [_area_row(conn, r) for r in rows]}


def area_show(conn, project, name):
    a = _area_row(conn, _area(conn, project, name), project)
    a["trigger_list"] = [_trigger_brief(conn, t) for t in conn.execute(
        "SELECT * FROM triggers WHERE area_id=? ORDER BY name", (a["id"],))]
    return a


# ---- playbooks

def _playbook(conn, project, name):
    pid = _pid(conn, project)
    r = conn.execute("SELECT * FROM ops_playbooks WHERE project_id=? AND name=?", (pid, name)).fetchone()
    if r is None:
        names = [x["name"] for x in conn.execute("SELECT name FROM ops_playbooks WHERE project_id=? ORDER BY name",
                                                 (pid,))]
        raise RiverError(f"{project} has no playbook {name!r}; its playbooks: {', '.join(names) or 'none'} "
                         f"(maxpm ops playbook list {project})")
    return dict(r)


def _playbook_rights(conn, actor, sets):
    """Who changes what of a playbook (mark 2026-11-21, #6641): a person all of it; the manager all but the tools,
    the limits and the repair; another agent nothing directly: it proposes the knowledge and done means."""
    who = _who(conn, actor)
    person = [k for k in sets if k in PLAYBOOK_PERSON]
    if person and who != "person":
        raise RiverError(f"{actor} is {'the manager' if who == 'manager' else 'an agent'}; only a person sets the "
                         f"{', '.join(person)} of a playbook (mark 2026-11-21). Ask the person: "
                         f"{core.as_cmd(actor)} ask <person> \"<the playbook, the change and why>\"")
    return who


def _playbook_check(sets):
    for k in ("knowledge", "done_means", "name"):
        if k in sets and not (sets[k] or "").strip():
            raise RiverError(f"a playbook needs its {k.replace('_', ' ')}: it cannot be empty")
    if "name" in sets:
        _name(sets["name"], "playbook")
    for k in ("tools", "limits", "repair", "model", "effort", "workdir"):
        if k in sets and sets[k] is not None:
            sets[k] = sets[k].strip() or (None if k in ("repair", "model", "effort", "workdir") else "")
    return sets


def playbook_add(conn, project, name, fields, actor=None):
    """maxpm ops playbook add: a playbook of the project (decision 7). Required: the name, the knowledge and what
    done means. The rest has a default: no tools, the limits 'investigate and propose only', the model and effort of
    the queue's operational work, the area's folder (else the project's)."""
    sets = {k: v for k, v in fields.items() if v is not None and k in PLAYBOOK_FIELDS}
    sets["name"] = name
    for k in PLAYBOOK_TEXT:
        if not (sets.get(k) or "").strip():
            raise RiverError(f"a playbook needs --{k.replace('_', '-')}: "
                             + ("what the agent must know (where to look, what is normal)" if k == "knowledge" else
                                "when the work of an event is finished"))
    with tx(conn):
        _need(conn, actor, "adds a playbook (an agent proposes a change of one: maxpm ops playbook edit)")
        _playbook_rights(conn, actor, sets)
        _playbook_check(sets)
        pid = _pid(conn, project)
        if conn.execute("SELECT 1 FROM ops_playbooks WHERE project_id=? AND name=?", (pid, name)).fetchone():
            raise RiverError(f"{project} has a playbook {name!r}; change it: maxpm ops playbook edit {project} {name}")
        cols = list(sets)
        conn.execute(f"INSERT INTO ops_playbooks(project_id,{','.join(cols)},created_by,created_at) VALUES "
                     f"(?,{','.join('?' * len(cols))},?,?)", (pid, *sets.values(), actor, iso(now())))
        _event(conn, None, actor, f"ops playbook {project}/{name} added")
    return {"playbook": playbook_show(conn, project, name), "changed": "added"}


def playbook_edit(conn, project, name, fields, why=None, item=None, actor=None):
    """maxpm ops playbook edit. A person or the manager changes it at once (only a person the tools, the limits and
    the repair); another agent's change of the knowledge or done means is a proposal until a person or the manager
    accepts it (maxpm ops playbook accept p<id>)."""
    sets = {k: v for k, v in fields.items() if v is not None and k in PLAYBOOK_FIELDS}
    if not sets:
        raise RiverError("give a change: --knowledge, --done-means, --tools, --limits, --repair, --model, --effort, "
                         "--where or --name")
    with tx(conn):
        b = _playbook(conn, project, name)
        who = _playbook_rights(conn, actor, sets)
        _playbook_check(sets)
        if who == "agent":
            other = [k for k in sets if k not in PLAYBOOK_TEXT]
            if other:
                raise RiverError(f"{actor} is an agent; it proposes the knowledge and done means of a playbook, and "
                                 f"the manager or a person sets its {', '.join(other)}. Ask the manager: "
                                 f"{core.as_cmd(actor)} ask manager \"<the playbook, the change and why>\"")
            if not (why or "").strip():
                raise RiverError("say why it changes: --why \"<what the agent lacked, and the event that showed it>\"")
            if item is None:
                r = conn.execute("SELECT id FROM items WHERE assignee=? AND status IN ('in_progress','held') ORDER BY "
                                 "id LIMIT 1", (actor,)).fetchone()
                item = r["id"] if r else None
            conn.execute("UPDATE ops_playbook_proposals SET state='replaced', decided_at=? WHERE playbook_id=? AND "
                         "state='open' AND proposed_by=?", (iso(now()), b["id"], actor))
            pid_ = conn.execute("INSERT INTO ops_playbook_proposals(playbook_id,knowledge,done_means,why,item_id,"
                                "proposed_by,proposed_at) VALUES (?,?,?,?,?,?,?)",
                                (b["id"], sets.get("knowledge"), sets.get("done_means"), why.strip(), item, actor,
                                 iso(now()))).lastrowid
            _event(conn, item, actor, f"ops playbook {project}/{name}: change proposed (p{pid_}): {why.strip()[:300]}")
            m = core.active_manager(conn)
            if m:
                core._send(conn, "note", "maxpm", f"{actor} proposes a change of the playbook {project}/{name} "
                           f"({', '.join(sets)}): {why.strip()[:300]}. Accept: maxpm ops playbook accept p{pid_}; "
                           f"reject: maxpm ops playbook reject p{pid_} --note \"<why>\"", to=m, item_id=item)
            return {"proposal": proposal_show(conn, pid_), "changed": "proposed"}
        if "name" in sets and sets["name"] != name and conn.execute(
                "SELECT 1 FROM ops_playbooks WHERE project_id=? AND name=?", (b["project_id"], sets["name"])).fetchone():
            raise RiverError(f"{project} has a playbook {sets['name']!r} already")
        conn.execute(f"UPDATE ops_playbooks SET {', '.join(k + '=?' for k in sets)}, changed_by=?, changed_at=? "
                     f"WHERE id=?", (*sets.values(), actor, iso(now()), b["id"]))
        _event(conn, None, actor, f"ops playbook {project}/{name} changed ({', '.join(sets)})")
    return {"playbook": playbook_show(conn, project, sets.get("name", name)), "changed": "changed"}


def _proposal(conn, ref):
    try:
        pid = int(str(ref).lstrip("p"))
    except ValueError:
        raise RiverError(f"bad proposal {ref!r}: p<id> (maxpm ops playbook list --proposals)")
    r = conn.execute("SELECT * FROM ops_playbook_proposals WHERE id=?", (pid,)).fetchone()
    if r is None:
        raise RiverError(f"no playbook proposal p{pid}")
    return dict(r)


def proposal_show(conn, ref):
    p = _proposal(conn, ref)
    b = conn.execute("SELECT * FROM ops_playbooks WHERE id=?", (p["playbook_id"],)).fetchone()
    p["playbook"] = b["name"] if b else None
    p["project"] = core._project_name(conn, b["project_id"]) if b else None
    return p


def playbook_decide(conn, ref, accept, actor=None, note=None):
    """maxpm ops playbook accept|reject p<id>: a person or the manager decides an agent's proposed change."""
    with tx(conn):
        if _who(conn, actor) == "agent":
            raise RiverError(f"{actor} is an agent; a person or the manager accepts or rejects a playbook change")
        p = proposal_show(conn, ref)
        if p["state"] != "open":
            raise RiverError(f"the proposal p{p['id']} is {p['state']}")
        if not accept and not (note or "").strip():
            raise RiverError("say why: --note \"<why not>\"")
        if accept:
            sets = {k: p[k] for k in PLAYBOOK_TEXT if p[k] is not None}
            conn.execute(f"UPDATE ops_playbooks SET {', '.join(k + '=?' for k in sets)}, changed_by=?, changed_at=? "
                         f"WHERE id=?", (*sets.values(), actor, iso(now()), p["playbook_id"]))
        conn.execute("UPDATE ops_playbook_proposals SET state=?, decided_by=?, decided_at=?, note=? WHERE id=?",
                     ("accepted" if accept else "rejected", actor, iso(now()), note, p["id"]))
        _event(conn, p["item_id"], actor, f"ops playbook {p['project']}/{p['playbook']}: proposal p{p['id']} "
                                          f"{'accepted' if accept else 'rejected'}" + (f": {note}" if note else ""))
        if p["proposed_by"]:
            core._send(conn, "note", actor or "maxpm", f"your change of the playbook {p['project']}/{p['playbook']} "
                       f"(p{p['id']}) is {'accepted' if accept else 'rejected'}" + (f": {note}" if note else ""),
                       to=p["proposed_by"], item_id=p["item_id"])
    return {"proposal": proposal_show(conn, p["id"]), "changed": "accepted" if accept else "rejected"}


def _playbook_row(conn, r, project=None):
    d = dict(r)
    d["project"] = project or core._project_name(conn, d["project_id"])
    d["triggers"] = [f"{t['scope']}/{t['name']}" for t in conn.execute(
        "SELECT scope, name FROM triggers WHERE playbook_id=? ORDER BY scope, name", (d["id"],))]
    if d["is_default"]:  # the triggers that name none are served by it too
        d["triggers"] += [f"{t['scope']}/{t['name']}" for t in conn.execute(
            "SELECT t.scope, t.name FROM triggers t JOIN ops_areas a ON a.id=t.area_id WHERE t.playbook_id IS NULL "
            "AND a.project_id=? ORDER BY t.scope, t.name", (d["project_id"],))]
    d["proposals"] = [dict(p) for p in conn.execute(
        "SELECT * FROM ops_playbook_proposals WHERE playbook_id=? AND state='open' ORDER BY id", (d["id"],))]
    return d


def playbook_list(conn, project=None):
    if project:
        rows = conn.execute("SELECT * FROM ops_playbooks WHERE project_id=? ORDER BY is_default DESC, name",
                            (_pid(conn, project),))
    else:
        rows = conn.execute("SELECT b.* FROM ops_playbooks b JOIN projects p ON p.id=b.project_id ORDER BY p.rank, "
                            "p.name, b.is_default DESC, b.name")
    return {"playbooks": [_playbook_row(conn, r) for r in rows]}


def playbook_show(conn, project, name):
    return _playbook_row(conn, _playbook(conn, project, name), project)


def playbook_for(conn, t):
    """The playbook that serves a trigger, with each default filled in: what #6627 gives the agent of an event."""
    r = conn.execute("SELECT * FROM ops_playbooks WHERE id=?", (t["playbook_id"],)).fetchone() \
        if t.get("playbook_id") else None
    if r is None:
        with tx(conn):
            pid, _ = core._trigger_home(conn, t["scope_kind"], t["scope"], "maxpm")
            r = conn.execute("SELECT * FROM ops_playbooks WHERE id=?", (core._default_playbook(conn, pid, "maxpm"),)
                             ).fetchone()
    b = dict(r)
    b["knowledge"] = b["knowledge"] or core.OPS_DEFAULT_KNOWLEDGE
    b["tools"] = b["tools"] or core.OPS_NO_TOOLS
    b["limits"] = b["limits"] or core.OPS_NO_LIMITS
    if not b["workdir"]:
        a = conn.execute("SELECT place FROM ops_areas WHERE id=?", (t.get("area_id"),)).fetchone() \
            if t.get("area_id") else None
        p = conn.execute("SELECT path FROM projects WHERE id=?", (b["project_id"],)).fetchone()
        b["workdir"] = (a["place"] if a and a["place"] else None) or (p["path"] if p else None)
    return b


def playbook_has_custom_limits(conn, item=None, check=None):
    """True when the trigger's playbook has limits that a person explicitly wrote (#6645: rule g7 and ops_repair_max
    apply only when a playbook sets no limits)."""
    t = check
    if t is None and item is not None:
        iid = item["id"] if isinstance(item, dict) else item
        r = conn.execute("SELECT t.* FROM trigger_events e JOIN triggers t ON t.id=e.trigger_id WHERE e.item_id=? "
                         "ORDER BY e.id LIMIT 1", (iid,)).fetchone()
        if r is not None:
            t = dict(r)
        else:
            key = core.fault_key(item) if isinstance(item, dict) else None
            if key:
                parts = key.split(":")
                if len(parts) >= 3 and parts[2]:
                    r = conn.execute("SELECT * FROM triggers WHERE scope=? AND name=? LIMIT 1",
                                     (parts[1], parts[2])).fetchone()
                    if r is not None:
                        t = dict(r)
    if not t or not t.get("playbook_id"):
        return False
    r = conn.execute("SELECT limits FROM ops_playbooks WHERE id=?", (t["playbook_id"],)).fetchone()
    lim = (r["limits"] or "").strip() if r else ""
    return bool(lim and lim != core.OPS_NO_LIMITS)


def is_non_state_event_item(conn, item_id):
    """True when item_id was opened by a time or event trigger (not a state check)."""
    r = conn.execute("SELECT t.condition FROM trigger_events e JOIN triggers t ON t.id=e.trigger_id WHERE e.item_id=? "
                     "LIMIT 1", (item_id,)).fetchone()
    return bool(r and r["condition"] != "state")


def workdir_for_item(conn, item_id):
    """The playbook or area folder for an operations item, or None."""
    r = conn.execute("SELECT t.* FROM trigger_events e JOIN triggers t ON t.id=e.trigger_id WHERE e.item_id=? "
                     "ORDER BY e.id LIMIT 1", (item_id,)).fetchone()
    t = dict(r) if r is not None else None
    if t is None:
        it = conn.execute("SELECT id, title, kind FROM items WHERE id=?", (item_id,)).fetchone()
        key = core.fault_key(it) if it is not None else None
        if key:
            parts = key.split(":")
            if len(parts) >= 3 and parts[2]:
                r = conn.execute("SELECT * FROM triggers WHERE scope=? AND name=? LIMIT 1",
                                 (parts[1], parts[2])).fetchone()
                t = dict(r) if r is not None else None
    if t is None:
        return None
    return playbook_for(conn, t).get("workdir")


# ---- triggers

def _trigger(conn, scope, name):
    kind, scope = core._ops_scope(conn, scope)
    r = conn.execute("SELECT * FROM triggers WHERE scope_kind=? AND scope=? AND name=?", (kind, scope, name)).fetchone()
    if r is None:
        names = [x["name"] for x in conn.execute("SELECT name FROM triggers WHERE scope_kind=? AND scope=? ORDER BY "
                                                 "name", (kind, scope))]
        raise RiverError(f"{scope} has no trigger {name!r}; its triggers: {', '.join(names) or 'none'} "
                         f"(maxpm ops trigger list)")
    return dict(r)


def _trigger_brief(conn, r):
    t = dict(r)
    t["config"] = json.loads(t["config"] or "{}")
    a = conn.execute("SELECT a.name, p.name project FROM ops_areas a JOIN projects p ON p.id=a.project_id WHERE "
                     "a.id=?", (t["area_id"],)).fetchone() if t["area_id"] else None
    b = conn.execute("SELECT name FROM ops_playbooks WHERE id=?", (t["playbook_id"],)).fetchone() \
        if t["playbook_id"] else None
    t["area"] = a["name"] if a else None
    t["project"] = a["project"] if a else None
    t["playbook"] = b["name"] if b else None
    t["priority_used"] = core.trigger_priority(t)
    t["routine"] = t["config"].get("command") or (t["config"].get("url") if t["form"] == "url" else None)
    t["title"] = t["config"].get("title")
    ev = conn.execute("SELECT event_id, title, last_at FROM trigger_events WHERE trigger_id=? ORDER BY last_at DESC "
                      "LIMIT 1", (t["id"],)).fetchone()
    t["last_event"] = dict(ev) if ev else None
    t["events"] = conn.execute("SELECT COUNT(*) n FROM trigger_events WHERE trigger_id=?", (t["id"],)).fetchone()["n"]
    return t


def trigger_add(conn, project, name, condition, arrival, area=None, playbook=None, priority=None, command=None,
                every=None, cron=None, timeout=None, actor=None, escalate_after=None, title=None):
    """maxpm ops trigger add: a time or an event trigger of an area (a state trigger is a check: maxpm ops add). A
    time trigger has no routine: the clock gives the event, with the trigger's fixed title (#6626). An event poll
    trigger has a routine, a script that prints events as JSON lines (decision 5); a push trigger has none (#6628)."""
    _name(name, "trigger")
    if condition not in core.TRIGGER_CONDITIONS:
        raise RiverError(f"no condition {condition!r}: " + "; ".join(f"{k} ({v})" for k, v in
                                                                    core.TRIGGER_CONDITIONS.items()))
    if arrival not in core.TRIGGER_ARRIVALS:
        raise RiverError(f"no arrival {arrival!r}: " + "; ".join(f"{k} ({v})" for k, v in core.TRIGGER_ARRIVALS.items()))
    if condition == "state":
        raise RiverError(f"a state trigger is a check: maxpm ops add {project} {name} <form> ... --area <area> "
                         f"--playbook <playbook>")
    if arrival == "push" and condition == "time":
        raise RiverError("a time trigger polls: MaximizePM runs it at its time (--arrival poll)")
    if arrival == "push" and (command or every or cron):
        raise RiverError("a push trigger has no routine: the outside system posts its events to it")
    if condition == "time" and (command or "").strip():
        raise RiverError("a time trigger has no routine: the clock gives the event (use --title for its fixed title)")
    if condition != "time" and (title or "").strip():
        raise RiverError("--title is only for a time trigger; an event trigger's events carry their own title")
    _priority(priority)
    esc = _escalate_after(escalate_after)
    with tx(conn):
        _need(conn, actor, "adds a trigger")
        p = core._project(conn, project)
        a = _area(conn, p["name"], area) if area else None
        kind, scope = ("target", a["target"]) if a and a["target"] else ("project", p["name"])
        if conn.execute("SELECT 1 FROM triggers WHERE scope_kind=? AND scope=? AND name=?", (kind, scope, name)
                        ).fetchone():
            raise RiverError(f"{scope} has a trigger {name!r}: maxpm ops trigger show {scope} {name}")
        col = {"form": "", "config": {}, "every": "5m", "timeout": "30s", "cron": None}
        if condition == "time":
            if not (every or cron):
                raise RiverError("a time trigger needs --cron (for example '0 9 * * mon') or --every")
            if cron:
                core.cron_parse(cron)
            if every:
                core.parse_duration(every)
            if timeout:
                core.parse_duration(timeout)
            cfg = {"title": title.strip()} if (title or "").strip() else {}
            col = {"form": "time", "config": cfg, "every": every or "5m", "timeout": timeout or "30s",
                   "cron": cron or None}
        elif arrival == "poll":
            if not (command or "").strip():
                raise RiverError("a poll trigger needs --command: the routine that MaximizePM runs")
            col = core._ops_build("cmd", {"command": command, "every": every, "cron": cron, "timeout": timeout})
        pid, area_id = core._trigger_home(conn, kind, scope, actor, a["name"] if a else None)
        tid = conn.execute(
            "INSERT INTO triggers(scope_kind,scope,name,area_id,condition,arrival,priority,escalate_after,form,config,"
            "every,timeout,cron,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (kind, scope, name, area_id, condition, arrival, priority, esc, col["form"], json.dumps(col["config"]),
             col["every"], col["timeout"], col.get("cron"), actor, iso(now()))).lastrowid
        core._trigger_playbook(conn, tid, pid, name, None, None, playbook, actor)
        _event(conn, None, actor, f"ops trigger {scope}/{name} added: {condition}, {arrival}"
               + (f", title {title.strip()}" if (title or "").strip() else "")
               + (f", routine {command}" if command else ""))
    return {"trigger": trigger_show(conn, f"{kind}:{scope}", name), "changed": "added"}


def _priority(priority):
    if priority is not None and priority not in range(5):
        raise RiverError("--priority is 0 to 4 (0 is the highest); none: by its condition")


def _escalate_after(value):
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in ("off", "none"):
        return None
    core.parse_duration(s)
    return s


def trigger_edit(conn, scope, name, area=None, playbook=None, priority=None, actor=None, escalate_after=None,
                 title=None, command=None, every=None, cron=None, timeout=None):
    """maxpm ops trigger edit: another area of the same scope, another playbook of the project, its own priority
    (-1: by its condition again), escalate-after ('off' clears it), or its title/routine/schedule. The routine of a
    check: maxpm ops edit."""
    if all(v is None for v in (area, playbook, priority, escalate_after, title, command, every, cron, timeout)):
        raise RiverError("give a change: --area, --playbook, --priority, --escalate-after, --title, --command, "
                         "--every, --cron or --timeout")
    if priority is not None and priority != -1:
        _priority(priority)
    esc = _escalate_after(escalate_after) if escalate_after is not None else None
    with tx(conn):
        _need(conn, actor, "changes a trigger")
        t = _trigger(conn, scope, name)
        pid, area_id = core._trigger_home(conn, t["scope_kind"], t["scope"], actor, area)
        changed = []
        if area is not None and area_id != t["area_id"]:
            conn.execute("UPDATE triggers SET area_id=? WHERE id=?", (area_id, t["id"]))
            changed.append("area")
        if playbook is not None:
            core._trigger_playbook(conn, t["id"], pid, name, None, None, playbook, actor)
            changed.append("playbook")
        if priority is not None:
            conn.execute("UPDATE triggers SET priority=? WHERE id=?", (None if priority == -1 else priority, t["id"]))
            changed.append("priority")
        if escalate_after is not None:
            conn.execute("UPDATE triggers SET escalate_after=? WHERE id=?", (esc, t["id"]))
            changed.append("escalate_after")
        cfg = json.loads(t["config"] or "{}")
        cfg_changed = False
        if title is not None:
            if t["condition"] != "time":
                raise RiverError("--title is only for a time trigger")
            if title.strip():
                cfg["title"] = title.strip()
            else:
                cfg.pop("title", None)
            cfg_changed = True
            changed.append("title")
        if command is not None:
            if t["condition"] != "event" or t["arrival"] != "poll":
                raise RiverError("--command is only for an event poll trigger (a state check uses maxpm ops edit)")
            if not command.strip():
                raise RiverError("--command cannot be empty for a poll trigger")
            cfg["command"] = command.strip()
            cfg_changed = True
            changed.append("command")
        if cfg_changed:
            conn.execute("UPDATE triggers SET config=? WHERE id=?", (json.dumps(cfg), t["id"]))
        if every is not None:
            if t["arrival"] != "poll":
                raise RiverError("--every is only for a poll or time trigger")
            core.parse_duration(every)
            conn.execute("UPDATE triggers SET every=? WHERE id=?", (every, t["id"]))
            changed.append("every")
        if cron is not None:
            if t["arrival"] != "poll":
                raise RiverError("--cron is only for a poll or time trigger")
            cval = cron.strip() or None
            if cval:
                core.cron_parse(cval)
            conn.execute("UPDATE triggers SET cron=? WHERE id=?", (cval, t["id"]))
            changed.append("cron")
        if timeout is not None:
            if t["arrival"] != "poll":
                raise RiverError("--timeout is only for a poll or time trigger")
            core.parse_duration(timeout)
            conn.execute("UPDATE triggers SET timeout=? WHERE id=?", (timeout, t["id"]))
            changed.append("timeout")
        conn.execute("UPDATE triggers SET changed_by=?, changed_at=? WHERE id=?", (actor, iso(now()), t["id"]))
        _event(conn, None, actor, f"ops trigger {t['scope']}/{name} changed ({', '.join(changed) or 'nothing'})")
    return {"trigger": trigger_show(conn, f"{t['scope_kind']}:{t['scope']}", name), "changed": "changed"}


def trigger_list(conn, project=None):
    """The triggers by project and area (the layout of the Operations tab: project, area, triggers)."""
    if project:
        p = core._project(conn, project)
        scopes = [("project", p["name"])] + ([("target", p["target"])] if p["target"] else [])
        rows = [r for k, s in scopes for r in conn.execute(
            "SELECT * FROM triggers WHERE scope_kind=? AND scope=? ORDER BY name", (k, s))]
    else:
        rows = conn.execute("SELECT * FROM triggers ORDER BY scope, name").fetchall()
    return {"triggers": [_trigger_brief(conn, r) for r in rows]}


def trigger_show(conn, scope, name, n=10):
    t = _trigger_brief(conn, _trigger(conn, scope, name))
    t["playbook_used"] = playbook_for(conn, t)
    t["event_list"] = [_event_row(r) for r in conn.execute(
        "SELECT * FROM trigger_events WHERE trigger_id=? ORDER BY last_at DESC, id DESC LIMIT ?", (t["id"], n))]
    return t


# ---- events

def _event_row(r):
    d = dict(r)
    d["data"] = json.loads(d["data"]) if d.get("data") else {}
    return d


def trigger_event(conn, trigger_id, event, at=None):
    """Record one event of a trigger (inside the caller's tx): the path that a poll (#6626), a push (#6628) and maxpm
    ops events add share (decision 6). event: a dict with id and title (required); the other fields are kept as they
    came. A repeat of an id is counted on its first row. Returns {"event": the row, "new": True on its first time}."""
    if not isinstance(event, dict):
        raise RiverError("an event is a JSON object with an id and a title")
    eid = str(event.get("id") if event.get("id") is not None else "").strip()
    title = str(event.get("title") or "").strip()
    if not eid or not title:
        raise RiverError("an event needs an id (repeats of it start nothing new) and a title (one line: what "
                         "happened)")
    data = {k: v for k, v in event.items() if k not in ("id", "title")}
    text = json.dumps(data, sort_keys=True) if data else None
    if text and len(text) > EVENT_MAX:
        text = json.dumps({"truncated": text[:EVENT_MAX]})
    at = iso(at or now())
    old = conn.execute("SELECT * FROM trigger_events WHERE trigger_id=? AND event_id=?", (trigger_id, eid)).fetchone()
    if old:
        conn.execute("UPDATE trigger_events SET repeats=repeats+1, last_at=? WHERE id=?", (at, old["id"]))
        return {"event": _event_row(conn.execute("SELECT * FROM trigger_events WHERE id=?", (old["id"],)).fetchone()),
                "new": False}
    rid = conn.execute("INSERT INTO trigger_events(trigger_id,event_id,title,data,received_at,last_at) VALUES "
                       "(?,?,?,?,?,?)", (trigger_id, eid, title[:500], text, at, at)).lastrowid
    return {"event": _event_row(conn.execute("SELECT * FROM trigger_events WHERE id=?", (rid,)).fetchone()), "new": True}


def _escalate(conn, item_id, text, actor="maxpm"):
    """Escalate an event item (#6649, mark 2026-11-21): alert the live agent holding it; when nobody holds it,
    alert the active manager. No outside tool, no phone."""
    it = conn.execute("SELECT id, status, assignee FROM items WHERE id=?", (item_id,)).fetchone()
    if it and it["assignee"] and it["status"] in ("in_progress", "held"):
        a = conn.execute("SELECT * FROM agents WHERE name=?", (it["assignee"],)).fetchone()
        if a is not None and core._agent_state(conn, a) not in ("gone", "stopped"):
            with tx(conn):
                core._send(conn, "alert", actor, text, to=it["assignee"], item_id=item_id)
            return it["assignee"]
    m = core.active_manager(conn)
    if m:
        with tx(conn):
            core._send(conn, "alert", actor, text, to=m, item_id=item_id, level=core.WAKE_LEVEL)
        return m
    return None


def handle_event(conn, trigger_id, event, at=None, actor=None):
    """Record and handle one event of a trigger (#6627): the shared path for poll (#6626), push (#6628) and
    maxpm ops events add.
    - Repeat of an id whose item is open: add a note on that item and escalate (alert holder, else manager).
    - Repeat of an id whose item is closed: count only; no second item.
    - Many new events of one trigger within ops_storm_window (> ops_storm_count): share one storm item.
    - Otherwise: open one operational item with the trigger's priority and playbook's model/effort.
    Returns {"event", "new", "item", "action", "escalated_to"}."""
    at_dt = at or now()
    t_row = conn.execute("SELECT * FROM triggers WHERE id=?", (trigger_id,)).fetchone()
    if t_row is None:
        raise RiverError(f"no trigger {trigger_id}")
    t = dict(t_row)
    with tx(conn):
        res = trigger_event(conn, trigger_id, event, at=at_dt)
        ev = res["event"]
        _event(conn, None, actor or "maxpm", f"ops trigger {t['scope']}/{t['name']}: event {ev['event_id']} "
               + ("received" if res["new"] else f"repeated ({ev['repeats']}x)") + f": {ev['title']}")
    if not res["new"]:
        it = conn.execute("SELECT id, status, title FROM items WHERE id=?", (ev["item_id"],)).fetchone() \
            if ev.get("item_id") else None
        if it and it["status"] in ("open", "in_progress", "held"):
            core._ops_note(conn, it["id"],
                           f"ops event {ev['event_id']} repeated ({ev['repeats']}x) at {core.show_time(iso(at_dt))}: "
                           f"{ev['title']}", at_dt, force=True)
            to = _escalate(conn, it["id"],
                           f"repeat of event {ev['event_id']} ({ev['repeats']}x) on {t['scope']}/{t['name']}: "
                           f"#{it['id']} {ev['title']}")
            return {"event": ev, "new": False, "item": it["id"], "action": "repeat-note", "escalated_to": to}
        return {"event": ev, "new": False, "item": ev.get("item_id"), "action": "repeat-closed", "escalated_to": None}
    project = core._ops_project(conn, t)
    pid = core._project(conn, project)["id"]
    pb = playbook_for(conn, t)
    key = f"ops:{t['scope']}:{t['name']}"
    models = {k: pb[k] for k in ("model", "effort") if pb.get(k)} or None
    win_str = core.setting(conn, "ops_storm_window", project_id=pid)
    window = core.parse_duration(win_str)
    storm_count = int(core.setting(conn, "ops_storm_count", project_id=pid) or 3)
    recent = conn.execute("SELECT id, event_id, title FROM trigger_events WHERE trigger_id=? AND received_at>=? "
                          "ORDER BY id", (trigger_id, iso(at_dt - window))).fetchall()
    if len(recent) > storm_count:
        storm_prefix = f"{key} storm:"
        st = conn.execute("SELECT id, title FROM items WHERE kind='operational' AND status IN ('open','in_progress',"
                          "'held') AND substr(title, 1, ?)=? ORDER BY id DESC LIMIT 1",
                          (len(storm_prefix), storm_prefix)).fetchone()
        if st:
            with tx(conn):
                conn.execute("UPDATE trigger_events SET item_id=? WHERE id=?", (st["id"], ev["id"]))
            ev["item_id"] = st["id"]
            core._ops_note(conn, st["id"],
                           f"ops storm event e{ev['id']} [{ev['event_id']}] at {core.show_time(iso(at_dt))}: "
                           f"{ev['title']}", at_dt, force=True)
            to = _escalate(conn, st["id"],
                           f"storm on {t['scope']}/{t['name']}: new event {ev['event_id']} ({ev['title']}) "
                           f"joined #{st['id']}")
            return {"event": ev, "new": True, "item": st["id"], "action": "storm-note", "escalated_to": to}
        ev_list = ", ".join(f"{r['event_id']}: {r['title'][:60]}" for r in recent[-10:])
        ctx = (f"{len(recent)} events arrived on trigger {t['scope']}/{t['name']} within {win_str} "
               f"(ops_storm_count {storm_count}), so one item holds them. Recent events: {ev_list}. "
               f"Playbook: {pb['name']}. Full playbook and event data are in the go briefing.")
        r = core.item_add(conn, project, f"{storm_prefix} {len(recent)} events in {win_str}"[:300],
                          priority=core.trigger_priority(t), doer="ai", actor="maxpm", context=ctx,
                          goals=[], kind="operational", short=f"{t['name']}: {len(recent)} events"[:60],
                          models=models)
        with tx(conn):
            conn.execute("UPDATE trigger_events SET item_id=? WHERE id=?", (r["id"], ev["id"]))
        ev["item_id"] = r["id"]
        to = _escalate(conn, r["id"],
                       f"storm on {t['scope']}/{t['name']}: {len(recent)} events in {win_str}: #{r['id']}")
        return {"event": ev, "new": True, "item": r["id"], "action": "storm", "escalated_to": to}
    ctx = (f"Trigger {t['scope']}/{t['name']} ({t['condition']}, {t['arrival']}), playbook {pb['name']}. "
           f"Event e{ev['id']} (id {ev['event_id']}): {ev['title']}. "
           f"Read the playbook and untrusted event data in the go briefing (or maxpm ops events show e{ev['id']}).")
    r = core.item_add(conn, project, f"{key} {ev['title']}"[:300],
                      priority=core.trigger_priority(t), doer="ai", actor="maxpm", context=ctx,
                      goals=[], kind="operational", short=f"{t['name']}: {ev['title']}"[:60],
                      models=models)
    with tx(conn):
        conn.execute("UPDATE trigger_events SET item_id=? WHERE id=?", (r["id"], ev["id"]))
    ev["item_id"] = r["id"]
    return {"event": ev, "new": True, "item": r["id"], "action": "opened", "escalated_to": None}


def escalate_due(conn, at=None):
    """Escalate open event items whose trigger's escalate_after duration has passed (#6649): alert the holder,
    else the active manager. Called from server.ops_watch."""
    at_dt = at or now()
    out = []
    seen = set()
    rows = conn.execute(
        "SELECT e.id, e.event_id, e.title, e.received_at, e.escalated_at, e.item_id, "
        "t.scope, t.name AS trigger_name, t.escalate_after, i.status, i.title AS item_title "
        "FROM trigger_events e JOIN triggers t ON t.id=e.trigger_id JOIN items i ON i.id=e.item_id "
        "WHERE t.escalate_after IS NOT NULL AND t.escalate_after != '' "
        "AND i.status IN ('open','in_progress','held') ORDER BY e.id").fetchall()
    for r in rows:
        if r["item_id"] in seen:
            continue
        limit = core.parse_duration(r["escalate_after"])
        if at_dt - core.parse_iso(r["received_at"]) < limit:
            continue
        if r["escalated_at"] and at_dt - core.parse_iso(r["escalated_at"]) < limit:
            continue
        seen.add(r["item_id"])
        age = core._short(at_dt - core.parse_iso(r["received_at"]))
        to = _escalate(conn, r["item_id"],
                       f"event e{r['id']} ({r['scope']}/{r['trigger_name']}) has been open for {age} "
                       f"(escalate_after {r['escalate_after']}): #{r['item_id']} {r['title']}")
        with tx(conn):
            conn.execute("UPDATE trigger_events SET escalated_at=? WHERE item_id=?", (iso(at_dt), r["item_id"]))
        out.append({"item": r["item_id"], "trigger": f"{r['scope']}/{r['trigger_name']}", "to": to})
    return out


def event_brief(conn, item):
    """The playbook and untrusted event data for the go briefing of a trigger event item (#6627)."""
    iid = item["id"] if isinstance(item, dict) else item
    rows = conn.execute("SELECT * FROM trigger_events WHERE item_id=? ORDER BY id", (iid,)).fetchall()
    if not rows:
        return None
    t_row = conn.execute("SELECT * FROM triggers WHERE id=?", (rows[0]["trigger_id"],)).fetchone()
    if t_row is None:
        return None
    t = dict(t_row)
    pb = playbook_for(conn, t)
    a = conn.execute("SELECT name, place FROM ops_areas WHERE id=?", (t.get("area_id"),)).fetchone() \
        if t.get("area_id") else None
    return {
        "trigger": f"{t['scope']}/{t['name']}",
        "condition": t["condition"],
        "arrival": t["arrival"],
        "area": a["name"] if a else None,
        "place": a["place"] if a else None,
        "playbook": {
            "name": pb["name"],
            "knowledge": pb["knowledge"],
            "done_means": pb["done_means"],
            "tools": pb["tools"],
            "limits": pb["limits"],
            "custom_limits": playbook_has_custom_limits(conn, check=t),
            "workdir": pb["workdir"],
            "repair": bool(pb.get("repair")),
        },
        "events": [_event_row(r) for r in rows],
    }


def events_add(conn, scope, name, event, actor=None):
    """maxpm ops events add: give a trigger one event by hand (a test of its playbook, an event a person saw)."""
    _need(conn, actor, "gives a trigger an event")
    t = _trigger(conn, scope, name)
    res = handle_event(conn, t["id"], event, actor=actor)
    return dict(res, trigger=f"{t['scope']}/{name}")


def events_list(conn, scope=None, name=None, n=20):
    if scope and name:
        t = _trigger(conn, scope, name)
        rows = conn.execute("SELECT e.*, ? AS trigger FROM trigger_events e WHERE trigger_id=? ORDER BY last_at DESC, "
                            "id DESC LIMIT ?", (f"{t['scope']}/{t['name']}", t["id"], n))
    else:
        rows = conn.execute("SELECT e.*, t.scope || '/' || t.name AS trigger FROM trigger_events e LEFT JOIN triggers t "
                            "ON t.id=e.trigger_id ORDER BY e.last_at DESC, e.id DESC LIMIT ?", (n,))
    return {"events": [_event_row(r) for r in rows]}


def events_show(conn, ref):
    try:
        rid = int(str(ref).lstrip("e"))
    except ValueError:
        raise RiverError(f"bad event {ref!r}: e<id> (maxpm ops events list)")
    r = conn.execute("SELECT e.*, t.scope || '/' || t.name AS trigger FROM trigger_events e LEFT JOIN triggers t ON "
                     "t.id=e.trigger_id WHERE e.id=?", (rid,)).fetchone()
    if r is None:
        raise RiverError(f"no event e{rid}: maxpm ops events list")
    return {"event": _event_row(r)}


# ---- poll and time triggers (#6626)

def _trigger_exec_row(r):
    t = dict(r)
    t["config"] = json.loads(t["config"] or "{}") if isinstance(t.get("config"), str) else dict(t.get("config") or {})
    t["key"] = f"ops:{t['scope']}:{t['name']}"
    return t


def _trigger_due(conn, t, at):
    """(due, fire_at) for a time or event poll trigger at `at`. Clears an expired pause, skips while a run is open."""
    if t.get("paused_until"):
        if core.parse_iso(t["paused_until"]) > at:
            return False, None
        with tx(conn):
            conn.execute("UPDATE triggers SET paused_until=NULL, state='new' WHERE id=? AND paused_until=?",
                         (t["id"], t["paused_until"]))
            conn.execute("INSERT INTO ops_events(check_id,key,state,at,actor,note) VALUES (?,?,?,?,?,?)",
                         (t["id"], t["key"], "resumed", iso(at), "maxpm", f"the pause until {t['paused_until']} ended"))
        t["paused_until"] = None
        t["state"] = "new"
    if conn.execute("SELECT 1 FROM ops_runs WHERE check_id=? AND ended_at IS NULL", (t["id"],)).fetchone():
        return False, None
    last = conn.execute("SELECT started_at FROM ops_runs WHERE check_id=? ORDER BY id DESC LIMIT 1",
                        (t["id"],)).fetchone()
    since = core.parse_iso(last["started_at"]) if last else None
    if t.get("cron"):
        spec, zone = core.cron_parse(t["cron"]), core._ops_zone(conn, t)
        fire = core.cron_prev(spec, at, zone)
        base = since or core.parse_iso(t["created_at"])
        return (fire is not None and fire > base), fire
    due = since is None or at - since >= core.parse_duration(t["every"])
    return due, at


def time_due(conn, at=None):
    """The time triggers due at `at`, as a list of (trigger_dict, scheduled_at)."""
    at = at or now()
    out = []
    for r in conn.execute("SELECT * FROM triggers WHERE condition='time' AND arrival='poll' ORDER BY id").fetchall():
        t = _trigger_exec_row(r)
        due, fire = _trigger_due(conn, t, at)
        if due:
            out.append((t, fire))
    return out


def time_fire(conn, t, at=None, scheduled_at=None, actor="maxpm"):
    """Fire one time trigger (#6626): no routine runs; the clock gives the event with the trigger's fixed title."""
    at = at or now()
    t = _trigger_exec_row(t)
    fire_dt = scheduled_at or at
    title = (t["config"].get("title") or "").strip() or t["name"]
    eid = f"time-{iso(fire_dt)}"
    with tx(conn):
        if conn.execute("SELECT 1 FROM ops_runs WHERE check_id=? AND ended_at IS NULL", (t["id"],)).fetchone():
            return {"run": None, "trigger": f"{t['scope']}/{t['name']}", "skipped": "already running"}
        run_id = conn.execute(
            "INSERT INTO ops_runs(check_id,key,started_at,ended_at,status,seconds,summary,runner) VALUES "
            "(?,?,?,?,?,?,?,?)",
            (t["id"], t["key"], iso(at), iso(at), "ok", 0.0, f"time event: {title}"[:300], core.OPS_PROCESS)).lastrowid
    res = handle_event(conn, t["id"], {"id": eid, "title": title, "scheduled_at": iso(fire_dt)}, at=at, actor=actor)
    return dict(res, run=run_id, trigger=f"{t['scope']}/{t['name']}")


def fire_due_time_triggers(conn, at=None):
    """Fire every due time trigger at `at` (called from server.ops_watch)."""
    at = at or now()
    return [time_fire(conn, t, at=at, scheduled_at=fire) for t, fire in time_due(conn, at)]


def poll_due(conn, at=None):
    """The event poll triggers due at `at`."""
    at = at or now()
    out = []
    for r in conn.execute("SELECT * FROM triggers WHERE condition='event' AND arrival='poll' ORDER BY id").fetchall():
        t = _trigger_exec_row(r)
        due, _ = _trigger_due(conn, t, at)
        if due:
            out.append(t)
    return out


def poll_start(conn, t, at=None, actor=None):
    """Open one run of an event poll trigger in ops_runs; returns None if a run is already open."""
    at = at or now()
    t = _trigger_exec_row(t)
    with tx(conn):
        if conn.execute("SELECT 1 FROM ops_runs WHERE check_id=? AND ended_at IS NULL", (t["id"],)).fetchone():
            return None
        rid = conn.execute("INSERT INTO ops_runs(check_id,key,started_at,runner) VALUES (?,?,?,?)",
                           (t["id"], t["key"], iso(at), core.OPS_PROCESS)).lastrowid
    folder = playbook_for(conn, t).get("workdir") or core._ops_folder(conn, t)
    return {"id": rid, "trigger": t, "folder": folder, "limit": core.parse_duration(t["timeout"]).total_seconds(),
            "actor": actor}


def _parse_poll_lines(stdout):
    """Parse JSON lines printed by a polling routine (#6626). Each non-empty line must be a JSON object with non-empty
    'id' and 'title'; extra fields are kept as they are. Duplicate ids within the same run are dropped once here.
    Returns (events, errors)."""
    events = []
    errors = []
    seen = set()
    for lineno, raw in enumerate((stdout or "").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError as e:
            errors.append(f"line {lineno}: not valid JSON ({e})")
            continue
        if not isinstance(obj, dict):
            errors.append(f"line {lineno}: expected a JSON object")
            continue
        eid = str(obj.get("id") if obj.get("id") is not None else "").strip()
        title = str(obj.get("title") or "").strip()
        if not eid or not title:
            errors.append(f"line {lineno}: event needs non-empty id and title")
            continue
        if eid in seen:
            continue
        seen.add(eid)
        events.append(obj)
    return events, errors


def poll_execute(run):
    """Run an event poll trigger's routine script and return its raw result (writes nothing)."""
    t = run["trigger"]
    cmd = t["config"]["command"]
    env = {**os.environ, "MAXPM_TRIGGER": t["name"], "MAXPM_SCOPE": t["scope"], "MAXPM_TRIGGER_KEY": t["key"],
           "MAXPM_QUEUE_DIR": str(core.db_path().parent)}
    t0 = time.monotonic()
    error = None
    try:
        if run["folder"] and not os.path.isdir(run["folder"]):
            raise OSError(f"the folder {run['folder']} does not exist")
        r = core.OPS_RUNNER(cmd, run["folder"], run["limit"], env) if core.OPS_RUNNER else \
            core._run_command(cmd, run["folder"], run["limit"], None, env)
        code, timed_out = r.returncode, bool(getattr(r, "timed_out", False))
        stdout = r.stdout or ""
        stderr = r.stderr or ""
        text = (stdout.strip() + ("\n" + stderr.strip() if stderr.strip() else "")).strip()
    except OSError as e:
        code, timed_out, stdout, stderr, text = None, False, "", "", f"the command did not start: {e}"
        error = text
    return {"exit_code": code, "timed_out": timed_out, "stdout": stdout, "stderr": stderr,
            "output": core._ops_tail(text), "seconds": round(time.monotonic() - t0, 2), "error": error}


def _broken_trigger_item(conn, t):
    prefix = f"ops:{t['scope']}:{t['name']}: broken trigger"
    return conn.execute(
        "SELECT id, status, title FROM items WHERE status IN ('open','in_progress','held') "
        "AND substr(title, 1, ?)=? ORDER BY id DESC LIMIT 1", (len(prefix), prefix)).fetchone()


def _broken_trigger(conn, t, why, output=None, exit_code=None, timed_out=False, at=None):
    """Record a broken-trigger event for a failed poll routine (#6626, kinds.json rule 1): for the person or manager,
    never the trigger's playbook."""
    at = at or now()
    existing = _broken_trigger_item(conn, t)
    with tx(conn):
        conn.execute("INSERT INTO ops_events(check_id,key,state,at,actor,note) VALUES (?,?,?,?,?,?)",
                     (t["id"], t["key"], "broken-trigger", iso(at), "maxpm", why[:500]))
    if existing:
        core._ops_note(conn, existing["id"],
                       f"poll routine failed again at {core.show_time(iso(at))}: {why}", at, force=True)
        return {"item": existing["id"], "action": "broken-note"}
    project = core._ops_project(conn, t)
    prefix = f"ops:{t['scope']}:{t['name']}: broken trigger"
    cmd = t["config"].get("command") or ""
    details = [f"The polling routine for trigger {t['scope']}/{t['name']} failed ({why}).",
               "Broken-trigger event (kinds.json rule 1): fix the trigger's routine, command or access; "
               "the trigger's playbook did not run."]
    if cmd:
        details.append(f"Command: {cmd}")
    if exit_code is not None:
        details.append(f"Exit code: {exit_code}")
    if timed_out:
        details.append(f"Timed out after {t['timeout']}.")
    if output:
        details.append(f"Output:\n{output[:1500]}")
    r = core.item_add(conn, project, f"{prefix} — {why}"[:300],
                      priority=core.trigger_priority(t), doer="human", actor="maxpm",
                      context="\n".join(details), goals=[], kind="work",
                      short=f"Broken trigger {t['name']}"[:60])
    core._ops_tell_manager(conn, "alert",
                           f"broken trigger {t['scope']}/{t['name']}: {why} (#{r['id']})", r["id"])
    return {"item": r["id"], "action": "broken-opened"}


def poll_finish(conn, run, at=None):
    """Execute an opened poll trigger run, close ops_runs, and either emit its JSON-line events through handle_event
    or record a broken-trigger event for the person/manager (#6626)."""
    t = run["trigger"]
    res = poll_execute(run)
    at = at or now()
    why = None
    if res["timed_out"]:
        why = f"routine stopped at its timeout {t['timeout']}"
    elif res["error"] or res["exit_code"] is None:
        why = res["error"] or "routine did not start"
    elif res["exit_code"] != 0:
        first = ((res["output"] or "").splitlines() or [""])[0].strip()[:200]
        label = "routine exited UNKNOWN (3)" if res["exit_code"] == 3 else f"routine exited {res['exit_code']}"
        why = f"{label}: {first}" if first else label
    if why:
        with tx(conn):
            conn.execute(
                "UPDATE ops_runs SET ended_at=?, status='unknown', exit_code=?, timed_out=?, seconds=?, summary=?, "
                "output=? WHERE id=?",
                (iso(at), res["exit_code"], int(bool(res["timed_out"])), res["seconds"], why[:300],
                 res["output"], run["id"]))
        brk = _broken_trigger(conn, t, why, output=res["output"], exit_code=res["exit_code"],
                              timed_out=res["timed_out"], at=at)
        return {"run": run["id"], "trigger": f"{t['scope']}/{t['name']}", "status": "unknown", "broken": why,
                "events": [], **brk}
    events, parse_errors = _parse_poll_lines(res["stdout"])
    handled = [handle_event(conn, t["id"], ev, at=at, actor="maxpm") for ev in events]
    if parse_errors:
        why = f"routine printed invalid event line(s): {'; '.join(parse_errors[:3])}"
        with tx(conn):
            conn.execute(
                "UPDATE ops_runs SET ended_at=?, status='unknown', exit_code=0, timed_out=0, seconds=?, summary=?, "
                "output=? WHERE id=?",
                (iso(at), res["seconds"], why[:300], res["output"], run["id"]))
        brk = _broken_trigger(conn, t, why, output=res["output"], exit_code=0, timed_out=False, at=at)
        return {"run": run["id"], "trigger": f"{t['scope']}/{t['name']}", "status": "unknown", "broken": why,
                "events": handled, **brk}
    n_new = sum(1 for h in handled if h["new"])
    n_rep = len(handled) - n_new
    summary = f"{n_new} new, {n_rep} repeat" if handled else "0 events"
    with tx(conn):
        conn.execute(
            "UPDATE ops_runs SET ended_at=?, status='ok', exit_code=0, timed_out=0, seconds=?, summary=?, output=? "
            "WHERE id=?",
            (iso(at), res["seconds"], summary, res["output"] if handled else None, run["id"]))
    existing = _broken_trigger_item(conn, t)
    if existing:
        core._ops_note(conn, existing["id"],
                       f"poll routine succeeded at {core.show_time(iso(at))} ({summary})", at, force=True)
    return {"run": run["id"], "trigger": f"{t['scope']}/{t['name']}", "status": "ok", "summary": summary,
            "events": handled}


def close_lost_polls(conn, running=(), at=None):
    """Close event poll runs that a stopped maxpm serve left open: cut runs are deleted to retry; lost runs become
    a broken-trigger event (#6626)."""
    at = at or now()
    out = []
    rows = conn.execute(
        "SELECT r.id, r.check_id, r.started_at, r.runner, t.* FROM ops_runs r "
        "JOIN triggers t ON t.id=r.check_id WHERE t.condition='event' AND t.arrival='poll' AND r.ended_at IS NULL"
    ).fetchall()
    for r in rows:
        run_id = r["id"]
        if run_id in running:
            continue
        t = _trigger_exec_row(r)
        t["id"] = r["check_id"]
        if core._ops_cut(r["runner"]):
            with tx(conn):
                if conn.execute("DELETE FROM ops_runs WHERE id=? AND ended_at IS NULL", (run_id,)).rowcount:
                    _event(conn, None, "maxpm",
                           f"ops poll {t['scope']}/{t['name']}: a stop of maxpm serve cut run {run_id}; it runs again")
            out.append({"key": t["key"], "run": run_id, "status": "cut"})
            continue
        if at - core.parse_iso(r["started_at"]) < core.parse_duration(t["timeout"]) + timedelta(seconds=core.OPS_LOST_GRACE):
            continue
        why = "the run never ended: maxpm serve stopped while it ran"
        with tx(conn):
            conn.execute("UPDATE ops_runs SET ended_at=?, status='unknown', summary=? WHERE id=?",
                         (iso(at), why, run_id))
        brk = _broken_trigger(conn, t, why, at=at)
        out.append({"key": t["key"], "run": run_id, "status": "unknown", "broken": why, **brk})
    return out
