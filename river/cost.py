"""The cost view (#5857, mark 2026-11-14, research theme 4: parallel agents use the plan limits fast, and most tools
show no cost). What each session, item and project used in a period: tokens from the agents' chat logs (the
adapters' chat_requests, as maxpm usage counts them), input-token equivalents (core.usage_eq), and dollars from the
setting model_prices by the session's model. A model call is charged to the item its session held at that time
(holds; when it held several, the one it took last), else to the session's project with no item.

The plan windows come from the agents that write them to their log (Codex: the 5-hour and the weekly window).

The setting cost_limit (empty by default: no limit, mark's rule) names an amount per period, globally or for one
project: the person gets one alert at 80% and one at 100% in each period. Nothing stops by itself."""

import re
import time
from datetime import datetime

from . import core
from .core import RiverError

PERIODS = ("today", "7d", "30d", "all")
_REQS = {}       # session id -> (read at, requests): a session that ended is read once
REREAD = 60.0    # seconds: a live session's log is read again after this
TOKEN_KEYS = ("input", "cache_read", "write_5m", "write_1h", "output")


def start_of(conn, period):
    """The start of a period as an aware UTC datetime, or None for 'all'. today: midnight in the setting timezone."""
    if period not in PERIODS:
        raise RiverError(f"the period is one of {', '.join(PERIODS)}, not {period!r}")
    if period == "all":
        return None
    if period == "today":
        zone = core.local_zone(core.setting(conn, "timezone"))
        local = datetime.now(zone)
        return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(core.timezone.utc)
    return core.now() - core.parse_duration(period)


def _requests(conn, agent, root=None):
    """[(time, usage)] of every model call of the agent's sessions, oldest first. A log is read again only when the
    agent was seen after the last read (or REREAD later)."""
    a = conn.execute("SELECT native_address, last_seen FROM agents WHERE name=?", (agent,)).fetchone()
    sids = [r[0] for r in conn.execute("SELECT session_id FROM agent_sessions WHERE agent=? ORDER BY first_seen",
                                       (agent,))]
    if a and a["native_address"] and a["native_address"] not in sids:
        sids.append(a["native_address"])
    seen = core.parse_iso(a["last_seen"]).timestamp() if a and a["last_seen"] else 0
    out = []
    for sid in sids:
        hit = _REQS.get(sid)
        if hit is None or (hit[0] < seen + 5 and time.time() - hit[0] > REREAD):
            got = core._session_requests(sid, root)
            hit = (time.time(), sorted(got[0].values(), key=lambda r: r[0]) if got else [])
            _REQS[sid] = hit
        out += hit[1]
    return sorted(out, key=lambda r: r[0])


def priced(conn, model, u):
    """{usd, output_priced}: the dollars of a usage sum at the model's model_prices, or usd None for no price."""
    p = core.model_price(conn, model, output=True)
    if not p:
        return {"usd": None, "output_priced": False}
    inp, read, w5, w1, out = p
    usd = (u["input"] * inp + u["cache_read"] * read + u["write_5m"] * w5 + u["write_1h"] * w1
           + (u["output"] * out if out is not None else 0)) / 1e6
    return {"usd": round(usd, 4), "output_priced": out is not None}


def _blank():
    return {"calls": 0, **dict.fromkeys(TOKEN_KEYS, 0), "eq": 0.0, "usd": 0.0, "unpriced": 0}


def _add(row, u, cost):
    row["calls"] += 1
    for k in TOKEN_KEYS:
        row[k] += u.get(k) or 0
    row["eq"] += core.usage_eq(u)
    if cost is None:
        row["unpriced"] += 1
    else:
        row["usd"] += cost


def _finish(row):
    row["eq"] = round(row["eq"])
    row["usd"] = round(row["usd"], 2)
    row["tokens"] = sum(row[k] for k in TOKEN_KEYS)
    return row


def report(conn, period="today", root=None, extras=True):
    """The cost of the period: {period, start, sessions, items, projects, total, output_priced, plans, limits}.
    Each row has calls, the tokens (input, cache_read, write_5m, write_1h, output, tokens), eq and usd; rows are
    sorted by usd, then eq. unpriced: model calls of a model with no model_prices family (their tokens count).
    extras False: no plan windows and no limits (a limit reads the use of its own period with it)."""
    start = start_of(conn, period)
    agents = conn.execute("SELECT * FROM agents WHERE kind='ai'" + (" AND last_seen>=?" if start else "")
                          + " ORDER BY name", ((core.iso(start),) if start else ())).fetchall()
    sessions, items, projects = {}, {}, {}
    end_now = core.now()
    out_priced = True
    for a in agents:
        reqs = [(t, u) for t, u in _requests(conn, a["name"], root) if start is None or t >= start]
        if not reqs:
            continue
        model = a["model"]
        holds = [(core.parse_iso(h["started_at"]), core.parse_iso(h["ended_at"]) if h["ended_at"] else end_now,
                  h["id"], h["item_id"]) for h in conn.execute("SELECT * FROM holds WHERE agent=? ORDER BY id",
                                                               (a["name"],))]
        s = sessions.setdefault(a["name"], {"agent": a["name"], "model": model, "state": core._agent_state(conn, a),
                                            "items": set(), **_blank()})
        home = _home_project(conn, a["name"])
        for t, u in reqs:
            m = u.get("model") or model  # the call's own model (Claude Code names it), else the session's
            if m and m != "<synthetic>" and not s["model"]:
                s["model"] = m
            got = priced(conn, m, {k: u.get(k) or 0 for k in TOKEN_KEYS})
            out_priced = out_priced and (got["usd"] is None or got["output_priced"])
            cost = got["usd"]
            _add(s, u, cost)
            active = [h for h in holds if h[0] <= t < h[1]]
            item_id = max(active, key=lambda h: (h[0], h[2]))[3] if active else None
            if item_id is not None:
                s["items"].add(item_id)
                it = items.get(item_id)
                if it is None:
                    r = conn.execute("SELECT i.id, i.title, i.status, p.name project FROM items i JOIN projects p "
                                     "ON p.id=i.project_id WHERE i.id=?", (item_id,)).fetchone()
                    it = items[item_id] = {"id": item_id, "title": r["title"] if r else "", "status": r["status"]
                                           if r else None, "project": r["project"] if r else None, **_blank()}
                _add(it, u, cost)
                proj = it["project"]
            else:
                proj = home
            pr = projects.setdefault(proj or "", {"project": proj, **_blank()})
            _add(pr, u, cost)
    for s in sessions.values():
        s["items"] = sorted(s["items"])
    key = lambda r: (-(r["usd"] or 0), -r["eq"])
    rows = lambda d: sorted((_finish(r) for r in d.values()), key=key)
    out = {"period": period, "start": core.iso(start) if start else None, "sessions": rows(sessions),
           "items": rows(items), "projects": rows(projects), "output_priced": out_priced}
    total = _blank()
    for r in out["sessions"]:
        for k in ("calls", *TOKEN_KEYS, "eq", "usd", "unpriced"):
            total[k] += r[k]
    out["total"] = _finish(total)
    if extras:
        out["plans"] = plans(conn, [a["name"] for a in agents
                                    if core._agent_state(conn, a) not in ("gone", "stopped")], root)
        out["limits"] = limits(conn, out, root)
    return out


def _home_project(conn, agent):
    """The project of a session with no item: the project of the folder it works in, else of its last held item."""
    from . import review
    try:
        f = review.folder_of(conn, agent)
    except Exception:  # noqa: BLE001 - a folder that cannot be read: the item's project
        f = None
    names = core.projects_for_dir(conn, f) if f else []
    if names:
        return names[0]
    r = conn.execute("SELECT p.name FROM holds h JOIN items i ON i.id=h.item_id JOIN projects p ON p.id=i.project_id "
                     "WHERE h.agent=? ORDER BY h.id DESC LIMIT 1", (agent,)).fetchone()
    return r[0] if r else None


def plans(conn, agents, root=None):
    """The plan windows that running agents write to their log (Codex): one row per window, the newest reading of
    each kind of agent. Agents whose log has none (Claude Code) are named, so the page can say where to look."""
    from . import sessions
    seen, rows, silent = set(), [], set()
    for name in agents:
        try:
            u = sessions.session_usage(conn, name, root)
        except RiverError:
            continue
        label = u.get("label") or "Agent"
        if u.get("limits"):  # [{name, used_percent, window_minutes, resets_at} or {name, text}] (codex.chat_limits)
            if label in seen:
                continue
            seen.add(label)
            rows += [{"agent": label, **w} for w in u["limits"]]
        else:
            silent.add(label)
    return {"windows": rows, "silent": sorted(silent - seen)}


# ---- the limit

LIMIT = re.compile(r"^\s*(\$)?([0-9.]+)\s*([kKmM]?)\s*/\s*(day|week|month)\s*$")


def parse_limit(text):
    """'$40/day' -> {'usd': 40, 'per': 'day'}; '20M/week' -> {'eq': 20000000, 'per': 'week'}; '' -> None."""
    if not (text or "").strip():
        return None
    m = LIMIT.match(text)
    if not m:
        raise RiverError("cost_limit is an amount per day, week or month: $40/day (dollars) or 20M/week (input-token "
                         "equivalents); empty for no limit")
    n = float(m.group(2)) * {"": 1, "k": 1000, "m": 1_000_000}[m.group(3).lower()]
    return {"usd" if m.group(1) else "eq": n, "per": m.group(4)}


PER_PERIOD = {"day": "today", "week": "7d", "month": "30d"}


def limits(conn, rep=None, root=None):
    """Each cost_limit in force (global, and each project's own) with its use: [{project, text, per, kind, used,
    of, percent}]. The use is read for the limit's own period (day: today, week: 7d, month: 30d)."""
    out = []
    scopes = [(None, core.setting(conn, "cost_limit"))]
    for r in conn.execute("SELECT name, id FROM projects WHERE archived=0 ORDER BY rank, name"):
        own = conn.execute("SELECT value FROM settings WHERE scope=? AND key='cost_limit'", (f"project:{r['name']}",)) \
            .fetchone()
        if own and own[0].strip():
            scopes.append((r["name"], own[0]))
    cache = {}
    for project, text in scopes:
        lim = parse_limit(text)
        if not lim:
            continue
        period = PER_PERIOD[lim["per"]]
        if rep is not None and rep["period"] == period:
            got = rep
        else:
            got = cache.get(period) or cache.setdefault(period, report(conn, period, root, extras=False))
        rows = got["projects"] if project else [got["total"]]
        row = next((r for r in rows if not project or r["project"] == project), None)
        kind = "usd" if "usd" in lim else "eq"
        used = (row or {}).get(kind) or 0
        of = lim[kind]
        out.append({"project": project, "text": text.strip(), "per": lim["per"], "kind": kind, "used": used,
                    "of": of, "percent": round(100 * used / of, 1) if of else None})
    return out


WATCH_EVERY = 300.0  # seconds between two looks of the loop: a week's or a month's use reads many logs
_WATCHED = {"at": 0.0}


def watch(conn, force=False):
    """One look at the limits (the loop of maxpm serve, at most every WATCH_EVERY): the person gets one alert at 80%
    and one at 100% of each limit in each of its periods. Nothing runs when no cost_limit is set."""
    if not core.setting(conn, "cost_limit").strip() and not conn.execute(
            "SELECT 1 FROM settings WHERE key='cost_limit' AND TRIM(value)<>''").fetchone():
        return []
    if not force and time.time() - _WATCHED["at"] < WATCH_EVERY:
        return []
    _WATCHED["at"] = time.time()
    sent = []
    for lim in limits(conn):
        for mark in (100, 80):
            if (lim["percent"] or 0) < mark:
                continue
            key = f"cost_limit:{lim['project'] or '*'}:{lim['per']}:{mark}:{_period_key(conn, lim['per'])}"
            with core.tx(conn):
                if conn.execute("SELECT 1 FROM meta WHERE key=?", (key,)).fetchone():
                    break
                conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (key, core.iso(core.now())))
                people = [r[0] for r in conn.execute(
                    "SELECT name FROM agents WHERE kind='human' ORDER BY last_seen DESC")]
                if people:
                    used = f"${lim['used']:.2f}" if lim["kind"] == "usd" else f"{lim['used'] / 1e6:.1f}M"
                    core._send(conn, "alert", "maxpm", f"Cost: {used} of {lim['text']}"
                               + (f" in {lim['project']}" if lim["project"] else "") + f" ({lim['percent']:.0f}%). "
                               "Nothing stops by itself; the Cost view shows where it went.", to=people[0])
                sent.append(key)
            break
    return sent


def _period_key(conn, per):
    """The current day, week or month in the setting timezone: the key of one alert per period."""
    d = datetime.now(core.local_zone(core.setting(conn, "timezone")))
    return d.strftime("%Y-%m-%d") if per == "day" else d.strftime("%G-W%V") if per == "week" else d.strftime("%Y-%m")


def csv_text(rep, part="sessions"):
    """One table of a report as CSV text, for export."""
    import csv
    import io
    cols = {"sessions": ["agent", "model", "state"], "items": ["id", "title", "project", "status"],
            "projects": ["project"]}[part]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols + ["calls", *TOKEN_KEYS, "tokens", "eq", "usd"])
    for r in rep[part]:
        w.writerow([r.get(c) for c in cols] + [r[k] for k in ("calls", *TOKEN_KEYS, "tokens", "eq", "usd")])
    return buf.getvalue()
