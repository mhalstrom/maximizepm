"""The review report of a release (#6003; summaries-mockup v7, mark 2026-11-15: 'as a pre-release effort the review
stuff is good'). What a cut release ships, for the person to read before the deploy, one level at a time:

1. the summary: one paragraph (the manager writes it: maxpm target summary; until then MaximizePM writes a plain one
   from its records) and the numbers (tasks done, dropped and open, faults, reviews, the person's decisions, cost);
2. the groups: risks first (a revert, a fix that a review asked for, an open fault), then one group for each goal,
   then one for each project;
3. each task: what changed for a person (done --changed), the proof (its commits, its check), who and when, cost;
4. the decisions on the task: its questions and their answers, and the notes between sessions;
5. the code: the Changes view of the task (GET /api/diff?item=<id>, #5855).

It opens from the target's pipeline in Deploy (Plan > Releases until #6136), from the release's deploy item and from its review. When a release is cut, the manager
gets a notice that offers it (core._release_cut); the manager's summary sends the person one Read card."""

import re

from . import core
from .core import RiverError

_SHA = re.compile(r"\b[0-9a-f]{7,40}\b")
_REVERT = re.compile(r"\brevert", re.I)


def release_of(conn, item_id):
    """The deploy item of a release: the item itself, or the deploy that waits on this review."""
    it = core._item(conn, item_id)
    if it["kind"] == "deploy":
        return it
    if it["kind"] == "review":
        dep = core._review_deploy(conn, it["id"])
        if dep:
            return core._item(conn, dep)
    raise RiverError(f"#{item_id} is no release: name its deploy item or its review (the target's pipeline in Deploy lists them)")


def _review(conn, dep_id):
    r = conn.execute("SELECT i.* FROM deps d JOIN items i ON i.id=d.blocked_by WHERE d.item_id=? AND i.kind='review' "
                     "ORDER BY i.id DESC LIMIT 1", (dep_id,)).fetchone()
    return dict(r) if r else None


def _shipped(conn, dep_id, review):
    """The tasks of the release: what the deploy waits on (no review, no deploy), and the fixes its review asked for."""
    ids = [r[0] for r in conn.execute("SELECT d.blocked_by FROM deps d JOIN items i ON i.id=d.blocked_by WHERE "
                                      "d.item_id=? AND i.kind NOT IN ('review','deploy') ORDER BY d.blocked_by",
                                      (dep_id,))]
    if review:
        ids += [r[0] for r in conn.execute("SELECT d.blocked_by FROM deps d JOIN items i ON i.id=d.blocked_by WHERE "
                                           "d.item_id=? AND i.kind NOT IN ('review','deploy') ORDER BY d.blocked_by",
                                           (review["id"],)) if r[0] not in ids]
    return ids


def _usd(conn, item_id):
    """The dollars of a task, from its measured usage (item_usage) at the price of each session's model."""
    from . import cost
    total, priced = 0.0, False
    for r in conn.execute("SELECT u.*, a.model FROM item_usage u LEFT JOIN agents a ON a.name=u.agent "
                          "WHERE u.item_id=?", (item_id,)):
        got = cost.priced(conn, r["model"] or core.setting(conn, "default_model") or "", dict(r))
        if got["usd"] is not None:
            total += got["usd"]
            priced = True
    return round(total, 2) if priced else None


def _decisions(conn, item_id):
    """The questions on a task with their answers, and the notes between sessions, oldest first (level 4)."""
    out = []
    for m in conn.execute("SELECT * FROM messages WHERE item_id=? AND kind IN ('question','note','alert') "
                          "ORDER BY id", (item_id,)):
        if m["from_agent"] == "maxpm":
            continue
        a = conn.execute("SELECT from_agent, body, created_at FROM messages WHERE reply_to=? AND kind='answer' "
                         "ORDER BY id LIMIT 1", (m["id"],)).fetchone() if m["kind"] == "question" else None
        out.append({"id": m["id"], "kind": m["kind"], "from": m["from_agent"], "to": m["to_agent"],
                    "body": m["body"], "at": m["created_at"],
                    "answer": ({"from": a["from_agent"], "body": a["body"], "at": a["created_at"]} if a else None)})
    return out


def _risk(it, faults):
    """Why a task is a risk, or None: a revert, a fix that the review asked for, or an open fault found after it."""
    if _REVERT.search(it["title"] or "") or _REVERT.search(it["changed"] or ""):
        return "a revert"
    if it["kind"] == "fixes" or (it["found_during_kind"] == "review"):
        return "a fix the review asked for"
    if any(f["found_during"] == it["id"] and f["status"] in core.OPEN_STATES for f in faults):
        return "an open fault"
    return None


def report(conn, item_id):
    """The review report of the release of a deploy item (or of its review)."""
    dep = release_of(conn, item_id)
    review = _review(conn, dep["id"])
    ids = _shipped(conn, dep["id"], review)
    marks = ",".join("?" * len(ids)) or "NULL"
    rows = {r["id"]: dict(r) for r in conn.execute(
        f"SELECT i.*, p.name project, f.kind found_during_kind FROM items i JOIN projects p ON p.id=i.project_id "
        f"LEFT JOIN items f ON f.id=i.found_during WHERE i.id IN ({marks})", ids)}
    faults = [dict(r) for r in conn.execute(
        f"SELECT id, title, status, found_during FROM items WHERE kind='operational' AND title LIKE 'Fault%' AND "
        f"(found_during=? OR found_during IN ({marks}))", [dep["id"], *ids])]
    goals = {}
    for r in conn.execute(f"SELECT ig.item_id, g.name, g.outcome FROM item_goals ig JOIN goals g ON g.id=ig.goal_id "
                          f"WHERE ig.item_id IN ({marks}) ORDER BY g.rank, g.id", ids):
        goals.setdefault(r["item_id"], []).append({"name": r["name"], "outcome": r["outcome"]})
    tasks = {}
    for i in ids:
        it = rows.get(i)
        if not it:
            continue
        decisions = _decisions(conn, i)
        tasks[i] = {"id": i, "title": it["title"], "short": it["short"], "status": it["status"], "kind": it["kind"],
                    "project": it["project"], "goals": [g["name"] for g in goals.get(i, [])],
                    "changed": it["changed"], "output": it["output"], "check": it["check"],
                    "commits": sorted(set(_SHA.findall(it["output"] or "")), key=(it["output"] or "").find),
                    "done_by": core._done_by(conn, i), "closed_at": it["closed_at"], "usd": _usd(conn, i),
                    "decisions": decisions, "risk": _risk(it, faults)}
    # Level 2: risks first, then each goal, then each project; a task is in one group.
    groups, seen = [], set()
    risky = [i for i, t in tasks.items() if t["risk"]]
    if risky:
        groups.append({"key": "risks", "name": "Risks", "risk": True, "items": risky,
                       "line": "; ".join(f"#{i} {tasks[i]['risk']}" for i in risky)})
        seen |= set(risky)
    by_goal, by_project = {}, {}
    for i, t in tasks.items():
        if i in seen:
            continue
        if t["goals"]:
            by_goal.setdefault(t["goals"][0], []).append(i)
        else:
            by_project.setdefault(t["project"], []).append(i)
    outcome = {g["name"]: g["outcome"] for gs in goals.values() for g in gs}
    for name, items in by_goal.items():
        groups.append({"key": "goal:" + name, "name": name, "risk": False, "items": items,
                       "line": outcome.get(name) or ""})
    for name, items in by_project.items():
        groups.append({"key": "project:" + name, "name": name, "risk": False, "items": items, "line": ""})
    done = [t for t in tasks.values() if t["status"] == "done"]
    usd = [t["usd"] for t in tasks.values() if t["usd"] is not None]
    numbers = {"tasks": len(tasks), "done": len(done),
               "dropped": sum(t["status"] == "dropped" for t in tasks.values()),
               "open": sum(t["status"] in core.OPEN_STATES for t in tasks.values()),
               "faults": len(faults), "faults_open": sum(f["status"] in core.OPEN_STATES for f in faults),
               "fixes": sum(t["kind"] == "fixes" or rows[t["id"]]["found_during_kind"] == "review"
                            for t in tasks.values()),
               "decisions": sum(1 for t in tasks.values() for d in t["decisions"]
                                if d["kind"] == "question" and _person(conn, d["to"])),
               "usd": round(sum(usd), 2) if usd else None}
    s = conn.execute("SELECT * FROM release_reports WHERE deploy_id=?", (dep["id"],)).fetchone()
    summary = ({"text": s["summary"], "by": s["by"], "at": s["at"]} if s
               else {"text": _plain_summary(dep, numbers, groups), "by": "maxpm", "at": None})
    code = _code(conn, dep["id"])
    if code:  # the size of the whole release, not only of its tasks (#6038)
        numbers.update(files=code["files"], added=code["added"], removed=code["removed"])
    return {"code": code,
            "release": {"id": dep["id"], "title": dep["title"], "target": dep["target"], "status": dep["status"],
                        "cut_at": dep["cut_at"], "cut_rev": dep["cut_rev"], "closed_at": dep["closed_at"],
                        "review": ({"id": review["id"], "status": review["status"], "assignee": review["assignee"],
                                    "output": review["output"]} if review else None)},
            "summary": summary, "numbers": numbers, "groups": groups, "tasks": tasks,
            "faults": faults}


def _code(conn, dep_id):
    """review.release_code; a report without git (no folder, no tag) still opens, with no code numbers."""
    from . import review as rv
    try:
        return rv.release_code(conn, dep_id)
    except (RiverError, OSError, ValueError):
        return None


def _person(conn, name):
    return bool(name) and conn.execute("SELECT 1 FROM agents WHERE name=? AND kind='human'", (name,)).fetchone() is not None


def _plain_summary(dep, n, groups):
    """MaximizePM's own paragraph, until the manager writes one: the counts and the risks, risks first."""
    risks = next((g for g in groups if g["risk"]), None)
    parts = []
    if risks:
        parts.append(f"Risks: {risks['line']}.")
    if n["faults_open"]:
        parts.append(f"{n['faults_open']} open fault{'s' if n['faults_open'] != 1 else ''}.")
    rest = [g["name"] for g in groups if not g["risk"]]
    parts.append(f"The release of {dep['target'] or 'its target'} ships {n['tasks']} task{'s' if n['tasks'] != 1 else ''}"
                 + (f" in {len(rest)} group{'s' if len(rest) != 1 else ''}: {', '.join(rest)}" if rest else "") + ".")
    if n["open"]:
        parts.append(f"{n['open']} {'is' if n['open'] == 1 else 'are'} not done yet.")
    return " ".join(parts) + " (MaximizePM wrote this from its records; the manager has not written a summary yet.)"


def summary_set(conn, item_id, text, actor=None):
    """The manager's paragraph of a release's report (maxpm target summary). The first one sends each person one Read
    card (a low alert) with the report: the manager offers it when the release is cut."""
    text = (text or "").strip()
    if not text:
        raise RiverError("write the summary: maxpm target summary <id> \"<one paragraph, risks first>\"")
    dep = release_of(conn, item_id)
    t = core.iso(core.now())
    with core.tx(conn):
        first = conn.execute("SELECT 1 FROM release_reports WHERE deploy_id=?", (dep["id"],)).fetchone() is None
        conn.execute("INSERT INTO release_reports (deploy_id, summary, by, at) VALUES (?,?,?,?) ON CONFLICT(deploy_id) "
                     "DO UPDATE SET summary=excluded.summary, by=excluded.by, at=excluded.at",
                     (dep["id"], text, actor or "maxpm", t))
        core._event(conn, dep["id"], actor, f"the summary of the release report: {text[:200]}")
    sent = []
    # A target that takes a person's yes for each release (release_approval person) shows the report on its approval
    # card: a Read card as well asked the person twice (#6108). A Read card closes with its release (core._close).
    on_approval = bool(dep["target"]) and core.release_approval(conn, dep["target"])["approval"] == "person"
    if first and not on_approval and dep["status"] not in core.CLOSED_STATES:
        for p in [r["name"] for r in conn.execute("SELECT name FROM agents WHERE kind='human' AND last_seen IS NOT NULL "
                                                    "ORDER BY name")]:
            m = core.send(conn, "alert", f"{core.REPORT_CARD} {dep['target'] or 'a target'} "
                          f"(#{dep['id']}) before it goes out: {text[:300]}", to=p, item=dep["id"], actor=actor,
                          level="low")
            sent.append(m["id"] if isinstance(m, dict) and "id" in m else p)
    return {"id": dep["id"], "summary": text, "read_cards": sent, "on_approval": on_approval}


def release_marks(conn, agent, days=14, limit=20):
    """The report blocks of a session's chat (#6031, mark 2026-11-16: 'this is not a rule this should be built into the
    platform'): each release that this session cut (core._release_cut) or started (it claimed the deploy item), and for
    the manager every cut. [{id, at, kind: cut|start, by, target, tasks}], oldest first; the page puts each one into
    the chat at its time and reads its numbers from GET /api/report/<id>. Only releases that are not out yet (the
    deploy item open): a release that went out has no block, so a new manager's chat starts with no report of the
    releases before it (#6180, mark 2026-11-19)."""
    me = conn.execute("SELECT role FROM agents WHERE name=?", (agent,)).fetchone()
    manager = bool(me and me["role"] == "manager")
    since = core.iso(core.now() - core.timedelta(days=days))
    states = core.OPEN_STATES
    rows = conn.execute(
        "SELECT e.item_id, e.at, e.actor, e.change, i.target FROM events e JOIN items i ON i.id=e.item_id "
        f"WHERE i.kind='deploy' AND i.status IN ({','.join('?' * len(states))}) AND e.at>=? "
        "AND (e.change LIKE 'release cut%' OR e.change LIKE 'claimed (%') "
        "AND (e.actor=? OR (? AND e.change LIKE 'release cut%')) ORDER BY e.id DESC LIMIT ?",
        (*states, since, agent, 1 if manager else 0, limit)).fetchall()
    out, seen = [], set()
    for r in rows:
        kind = "cut" if r["change"].startswith("release cut") else "start"
        if (r["item_id"], kind) in seen:
            continue
        seen.add((r["item_id"], kind))
        tasks = conn.execute("SELECT COUNT(*) FROM deps d JOIN items i ON i.id=d.blocked_by WHERE d.item_id=? "
                             "AND i.kind NOT IN ('review','deploy')", (r["item_id"],)).fetchone()[0]
        out.append({"id": r["item_id"], "at": r["at"], "kind": kind, "by": r["actor"], "target": r["target"],
                    "tasks": tasks})
    return list(reversed(out))
