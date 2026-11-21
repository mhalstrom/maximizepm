"""The Operations tab (#6629; canvas operations-tab, mark 2026-11-21): projects, their areas and each area's
triggers, the project's playbooks, and the forms that add and change them. The rules of who may change what are in
river/operations.py (a person all, the manager all but a playbook's tools, limits and repair).

GET: (test, handler) pairs that Handler.do_GET tries in order. OPS: page actions, merged into server.OPS."""

import json
from urllib.parse import parse_qs, urlsplit

from .. import core, operations
from ..core import RiverError


def tab(h, path):
    """GET /api/operations: every project's areas, triggers (each with its last event) and playbooks."""
    conn = core.connect()
    try:
        return h._send(200, operations.tab_view(conn))
    finally:
        conn.close()


def trigger(h, path):
    """GET /api/operations/trigger?scope=<kind:name>&name=<trigger>: one trigger, its playbook and its events."""
    q = {k: v[0] for k, v in parse_qs(urlsplit(h.path).query).items()}
    conn = core.connect()
    try:
        return h._send(200, operations.tab_trigger(conn, q.get("scope") or "", q.get("name") or ""))
    except (RiverError, ValueError) as e:
        return h._send(404, {"error": str(e)})
    finally:
        conn.close()


GET = [
    (lambda path: path == "/api/operations", tab),
    (lambda path: path == "/api/operations/trigger", trigger),
]

# The fields of the playbook form. The page sends only the fields the person changed, so a manager's change of the
# knowledge never names the tools (which only a person sets) and the history names what changed.
PLAYBOOK_FORM = ("knowledge", "done_means", "tools", "limits", "repair", "model", "effort", "workdir")


def _renamed(a):
    return {"name": a["new_name"]} if a.get("new_name") and a["new_name"] != a["name"] else {}


def _playbook_fields(a):
    return {k: str(a[k]) for k in PLAYBOOK_FORM if a.get(k) is not None}


def _priority(a):
    """'' or None: by its condition; else 0 to 4."""
    v = a.get("priority")
    return None if v in (None, "") else int(v)


def _trigger_add(c, a, who):
    return operations.trigger_add(
        c, a["project"], a["name"], a["condition"], a["arrival"], area=a.get("area") or None,
        playbook=a.get("playbook") or None, priority=_priority(a), command=a.get("command") or None,
        every=a.get("every") or None, cron=a.get("cron") or None, timeout=a.get("timeout") or None, actor=who,
        escalate_after=a.get("escalate_after") or None, title=a.get("title") or None)


def _trigger_edit(c, a, who):
    """The edit form sends every field it shows: a field equal to the trigger's value changes nothing. An empty
    priority means 'by its condition', an empty escalate-after 'off'."""
    t = operations._trigger(c, a["scope"], a["name"])
    cfg = json.loads(t["config"] or "{}")
    area = c.execute("SELECT name, project_id FROM ops_areas WHERE id=?", (t["area_id"],)).fetchone() \
        if t["area_id"] else None
    # A trigger that names no playbook uses its project's default one: the form shows that name, which is no change.
    book = c.execute("SELECT name FROM ops_playbooks WHERE id=?", (t["playbook_id"],)).fetchone() if t["playbook_id"] \
        else c.execute("SELECT name FROM ops_playbooks WHERE project_id=? AND is_default=1",
                       (area["project_id"],)).fetchone() if area else None

    def changed(key, old):
        v = a.get(key)
        if v is None:
            return None
        v = str(v).strip()
        return None if v == (old or "") else v

    prio = a.get("priority")
    if prio is not None:
        prio = -1 if prio == "" else int(prio)
        if prio == (-1 if t["priority"] is None else t["priority"]):
            prio = None
    esc = changed("escalate_after", t["escalate_after"])
    kw = dict(area=changed("area", area["name"] if area else None),
              playbook=changed("playbook", book["name"] if book else None), priority=prio,
              escalate_after="off" if esc == "" else esc)
    if t["condition"] == "time":
        kw["title"] = changed("title", cfg.get("title"))
    if t["condition"] == "event" and t["arrival"] == "poll":
        kw["command"] = changed("command", cfg.get("command"))
    if t["arrival"] == "poll" and t["condition"] != "state":
        kw.update(every=changed("every", t["every"]), cron=changed("cron", t["cron"]),
                  timeout=changed("timeout", t["timeout"]))
    if all(v is None for v in kw.values()):
        return {"changed": "nothing"}
    return operations.trigger_edit(c, a["scope"], a["name"], actor=who, **kw)


OPS = {
    "ops_area_add": lambda c, a, who: operations.area_add(c, a["project"], a["name"], a.get("description") or "",
                                                          a.get("place") or None, a.get("target") or None, who),
    "ops_area_edit": lambda c, a, who: operations.area_edit(
        c, a["project"], a["name"], {k: str(a[k]) for k in ("description", "place") if a.get(k) is not None}
        | _renamed(a), who),
    "ops_playbook_add": lambda c, a, who: operations.playbook_add(c, a["project"], a["name"], _playbook_fields(a), who),
    "ops_playbook_edit": lambda c, a, who: operations.playbook_edit(
        c, a["project"], a["name"], _playbook_fields(a) | _renamed(a), why=a.get("why") or None, actor=who),
    "ops_playbook_decide": lambda c, a, who: operations.playbook_decide(c, a["ref"], bool(a.get("accept")), who,
                                                                        a.get("note") or None),
    "ops_trigger_add": _trigger_add,
    "ops_trigger_edit": _trigger_edit,
    "ops_trigger_rotate": lambda c, a, who: operations.trigger_rotate(c, a["scope"], a["name"],
                                                                      bool(a.get("new_address")), who),
}
