"""The transition into each stage (#6139): the arrow between two stages, where a person controls the flow.

mark 2026-11-18 (canvas release-tracking, task J): "the transitions are where we need controls/ blockers", "the tests
are in stages. the transitions allow us to block/ delay, disable/ set time based transitions etc". The tests and checks
run inside a stage and are part of its result; a transition only decides when a version that passed the stage before
may enter the next one. It has:

- a mode, the normal way a version passes:
    auto                    at once, when the stage before passed
    yes [<name>|manager]    a person's yes (that person's; manager: the manager's or a person's)
    delay <time>            after that time, while the checks of the stage before pass (an old 'auto after 30m')
    release [every <time>]  the release: the manager or a person releases each version, or the cadence lets the next
                            one in after that time
    forge                   the forge starts the stage by itself (a push, a tag); MaximizePM only reads its run
- a time: a window ('mon-thu 09:00-17:00 America/Los_Angeles'; outside it no version passes) and freezes (from, to);
- a block (a reason, until a time, until an item closes, or until someone unblocks it), a rule that blocks it while a
  fault of the pipeline is open, a delay (no version before a time), or disabled (until a person or the manager
  enables it).

A waiting version passes when the mode lets it and no control holds it. Approve gives the yes of the mode only (a
blocked transition still holds an approved version); 'Let one version pass' takes that one version past every control
once. A stop of the whole pipeline (maxpm pipeline hold <name>) still stops even that.

Each change is a line of pipeline_changes with its actor, its reason and until when (the audit log). check() is what
the runner asks; describe() is what the arrow shows (mode, state, who set it, until when, the waiting versions).
"""

import json
import re
from datetime import datetime, timedelta, timezone

from .. import core
from . import audit, store

MODES = ("auto", "yes", "delay", "release", "forge")
_NAME = re.compile(r"^[\w.@-]{1,64}$")
_RANGE = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")
_DAY_SETS = {"weekdays": (0, 1, 2, 3, 4), "weekends": (5, 6), "daily": tuple(range(7)), "everyday": tuple(range(7))}
WAITING_SHOWN = 5  # the waiting versions that the arrow lists


# ---------------------------------------------------------------- the words of a mode

def parse_mode(text):
    """(mode, arg) from the words of a mode. The words of the gates before #6139 still work: auto after <time> is a
    delay, manager the manager's yes, person [<name>] a person's yes."""
    words = str(text or "").split()
    if words in (["auto"], ["forge"], ["yes"], ["release"], ["person"]):
        return ("yes", None) if words == ["person"] else (words[0], None)
    if words == ["manager"]:
        return "yes", "manager"
    if len(words) == 2 and words[0] in ("yes", "person") and _NAME.match(words[1]):
        return "yes", words[1]
    after = words[2:] if words[:2] in (["auto", "after"], ["release", "every"]) else words[1:] if words[:1] == ["delay"] \
        else None
    if after is not None and len(after) == 1:
        try:
            core.parse_duration(after[0])
        except (core.RiverError, ValueError):
            pass
        else:
            return ("release" if words[0] == "release" else "delay"), after[0]
    raise core.RiverError(f"the mode {text!r} is not one of: auto; yes; yes <person>; yes manager; delay <time> "
                          "(30m); release; release every <time> (2h); forge")


def mode_words(mode, arg):
    """The mode as maxpm pipeline transition --mode takes it: auto, yes mark, delay 30m, release every 2h."""
    if mode == "release" and arg:
        return f"release every {arg}"
    return f"{mode} {arg}" if arg else mode


def mode_text(mode, arg):
    """The mode in plain words, for the arrow and the briefing."""
    if mode == "yes":
        return "the manager's yes" if arg == "manager" else f"{arg}'s yes" if arg else "a person's yes"
    if mode == "delay":
        return f"after a delay of {arg}, while the checks pass"
    if mode == "release":
        return "the release" + (f", or every {arg}" if arg else "")
    if mode == "forge":
        return "the forge starts it (a push or a tag)"
    return "automatic"


# ---------------------------------------------------------------- the time window

def parse_window(text):
    """The windows of a transition from 'mon-thu 09:00-17:00 America/Los_Angeles' (';' between windows): a list of
    {days, start, end, zone}. Days: mon .. sun, a range (mon-fri), a list (mon,wed), weekdays, weekends, daily; none:
    every day. An end before the start runs over midnight (22:00-04:00). No zone: the setting timezone."""
    out = []
    for part in [p.strip() for p in str(text or "").split(";") if p.strip()]:
        words = part.replace(",", " ").split()
        zone = None
        if words and ("/" in words[-1] or words[-1].upper() in ("UTC", "GMT")):
            zone = words.pop()
            core.local_zone(zone)  # refuses a zone it does not know
        span = [w for w in words if _RANGE.match(w)]
        if len(span) != 1:
            raise core.RiverError(f"the window {part!r} needs one time span such as 09:00-17:00")
        h1, m1, h2, m2 = (int(x) for x in _RANGE.match(span[0]).groups())
        if h1 > 23 or h2 > 24 or m1 > 59 or m2 > 59 or (h2 == 24 and m2):
            raise core.RiverError(f"the time span {span[0]!r} is not a time of day")
        if (h1, m1) == (h2, m2):
            raise core.RiverError(f"the time span {span[0]!r} starts and ends at the same time")
        days = set()
        for w in (w.lower() for w in words if w != span[0]):
            if w in _DAY_SETS:
                days.update(_DAY_SETS[w])
            elif re.match(r"^[a-z]{3}-[a-z]{3}$", w) and w[:3] in core.WEEKDAYS and w[4:] in core.WEEKDAYS:
                a, b = core.WEEKDAYS.index(w[:3]), core.WEEKDAYS.index(w[4:])
                days.update((a + i) % 7 for i in range((b - a) % 7 + 1))
            elif w[:3] in core.WEEKDAYS and w in (w[:3], core._DAY_NAMES[core.WEEKDAYS.index(w[:3])]):
                days.add(core.WEEKDAYS.index(w[:3]))
            else:
                raise core.RiverError(f"the window {part!r}: {w!r} is not a day (mon .. sun, mon-fri, weekdays, "
                                      "weekends, daily)")
        out.append({"days": sorted(days) or list(range(7)), "start": (h1, m1), "end": (h2 % 24, m2), "zone": zone})
    if not out:
        raise core.RiverError("give a window such as 'mon-thu 09:00-17:00', or none to remove it")
    return out


def window_state(windows, at, zone=""):
    """(inside, change): whether the time at is inside one of the windows, and when that changes next (it closes, or
    the next one opens; None when no window opens in the next eight days)."""
    inside, ends, starts = False, [], []
    for w in windows:
        tz = core.local_zone(w["zone"] or zone)
        today = at.astimezone(tz).date()
        for d in range(-1, 9):
            day = today + timedelta(days=d)
            if day.weekday() not in w["days"]:
                continue
            a = datetime(day.year, day.month, day.day, *w["start"], tzinfo=tz)
            b = datetime(day.year, day.month, day.day, *w["end"], tzinfo=tz)
            if b <= a:
                b += timedelta(days=1)
            if a <= at < b:
                inside = True
                ends.append(b)
            elif a > at:
                starts.append(a)
    # In UTC: core.iso writes a time as UTC without converting it.
    if inside:
        return True, max(ends).astimezone(timezone.utc)
    return False, min(starts).astimezone(timezone.utc) if starts else None


# ---------------------------------------------------------------- reads

_EMPTY = {"mode": "auto", "mode_arg": None, "mode_by": None, "mode_at": None, "time_window": None, "window_by": None,
          "window_at": None, "freezes": None, "fault_rule": 0, "rule_by": None, "rule_at": None, "block_reason": None,
          "block_by": None, "block_at": None, "block_until": None, "block_item": None, "delay_until": None,
          "delay_by": None, "delay_at": None, "delay_reason": None, "disabled_reason": None, "disabled_by": None,
          "disabled_at": None}


def get(conn, stage_id):
    """The transition into a stage as a dict: a stage with no row has an automatic arrow with no controls."""
    r = conn.execute("SELECT * FROM pipeline_transitions WHERE stage_id=?", (stage_id,)).fetchone()
    return dict(r) if r else dict(_EMPTY, stage_id=stage_id)


def _ensure(conn, pid, sid):
    """The row of a transition, made on its first change (inside a tx)."""
    conn.execute("INSERT OR IGNORE INTO pipeline_transitions(pipeline_id, stage_id) VALUES (?,?)", (pid, sid))


def _freezes(t):
    try:
        return json.loads(t["freezes"]) if t["freezes"] else []
    except ValueError:
        return []


def _zone(conn, project):
    pid = conn.execute("SELECT id FROM projects WHERE name=?", (project,)).fetchone() if project else None
    return core.setting(conn, "timezone", project_id=pid["id"] if pid else None)


def _fault(conn, pipeline, cache=None):
    """The oldest open fault of a pipeline: its own fault items (a failed stage, #6118), and the open ops faults of its
    project and of the targets of its environments. None when none is open."""
    if cache is not None and "fault" in cache:
        return cache["fault"]
    heads = [f"pipeline:{pipeline['name']}:"] + [f"ops:{s}:" for s in [pipeline["project"]] + [r["target"] for r in conn.execute(
        "SELECT target FROM pipeline_envs WHERE pipeline_id=? AND target IS NOT NULL AND removed_at IS NULL",
        (pipeline["id"],))] if s]
    where = " OR ".join("substr(title, 1, ?)=?" for _ in heads)
    args = [x for h in heads for x in (len(h), h)]
    r = conn.execute("SELECT id, title FROM items WHERE kind='operational' AND status IN ('open','in_progress','held') "
                     f"AND ({where}) ORDER BY id LIMIT 1", args).fetchone()
    out = {"id": r["id"], "title": r["title"]} if r else None
    if cache is not None:
        cache["fault"] = out
    return out


def _item_open(conn, item_id):
    r = conn.execute("SELECT status FROM items WHERE id=?", (item_id,)).fetchone()
    return bool(r) and r["status"] not in core.CLOSED_STATES


def block_ended(conn, t, at):
    """True when the block of t has ended by itself: its time came, or its item closed."""
    if not t["block_reason"]:
        return False
    if t["block_until"] and core.parse_iso(t["block_until"]) <= at:
        return True
    return bool(t["block_item"]) and not _item_open(conn, t["block_item"])


def closed(conn, t, pipeline, at=None, cache=None):
    """The first control that keeps the transition closed now, or None: {kind, text, by, until}. kind: disabled,
    blocked, fault, frozen, window, delayed. A disabled, blocked or frozen transition asks nobody for a yes."""
    at = at or core.now()
    if t["disabled_at"]:
        return {"kind": "disabled", "by": t["disabled_by"], "until": None,
                "text": f"disabled by {t['disabled_by'] or 'a person'}: {t['disabled_reason']}; no version passes until "
                        f"someone enables it"}
    if t["block_reason"] and not block_ended(conn, t, at):
        until = (core.show_time(t["block_until"]) if t["block_until"] else
                 f"#{t['block_item']} closes" if t["block_item"] else "someone unblocks it")
        return {"kind": "blocked", "by": t["block_by"], "until": t["block_until"] or (
                f"#{t['block_item']}" if t["block_item"] else None),
                "text": f"blocked by {t['block_by'] or 'a person'}: {t['block_reason']} (until {until})"}
    if t["fault_rule"]:
        f = _fault(conn, pipeline, cache)
        if f:
            return {"kind": "fault", "by": f"the rule 'a fault of the pipeline is open' ({t['rule_by'] or 'a person'})",
                    "until": f"#{f['id']}", "text": f"blocked while fault #{f['id']} is open: {f['title'][:100]}"}
    for fz in _freezes(t):
        if core.parse_iso(fz["from"]) <= at < core.parse_iso(fz["to"]):
            return {"kind": "frozen", "by": fz.get("by"), "until": fz["to"],
                    "text": f"frozen until {core.show_time(fz['to'])} by {fz.get('by') or 'a person'}: {fz['reason']}"}
    if t["time_window"]:
        try:
            inside, change = window_state(parse_window(t["time_window"]), at, _zone(conn, pipeline["project"]))
        except core.RiverError as e:
            return {"kind": "window", "by": t["window_by"], "until": None, "text": f"the window cannot be read: {e}"}
        if not inside:
            return {"kind": "window", "by": t["window_by"], "until": core.iso(change) if change else None,
                    "text": f"outside its window ({t['time_window']})"
                            + (f": opens {core.show_time(core.iso(change))}" if change else "")}
    if t["delay_until"] and core.parse_iso(t["delay_until"]) > at:
        return {"kind": "delayed", "by": t["delay_by"], "until": t["delay_until"],
                "text": f"delayed until {core.show_time(t['delay_until'])} by {t['delay_by'] or 'a person'}: "
                        f"{t['delay_reason']}"}
    return None


def describe(conn, stage, pipeline, cache=None, at=None):
    """The arrow into a stage as the page and maxpm pipeline show draw it: the mode, each control with who set it and
    until when, the state now, and the versions that wait there."""
    at = at or core.now()
    t = get(conn, stage["id"])
    c = closed(conn, t, pipeline, at, cache)
    window = None
    if t["time_window"]:
        try:
            inside, change = window_state(parse_window(t["time_window"]), at, _zone(conn, pipeline["project"]))
            window = {"text": t["time_window"], "open": inside, "change": core.iso(change) if change else None,
                      "by": t["window_by"], "at": t["window_at"]}
        except core.RiverError:
            window = {"text": t["time_window"], "open": False, "change": None, "by": t["window_by"], "at": t["window_at"]}
    freezes = [dict(f, now=core.parse_iso(f["from"]) <= at < core.parse_iso(f["to"])) for f in _freezes(t)
               if core.parse_iso(f["to"]) > at]
    waiting = [dict(r) for r in conn.execute(
        "SELECT r.id run, v.rev, r.queued_at since, r.approved_by, r.passed_by, r.gate_note note, r.gate_item item, "
        "r.outside, r.outside_at "
        "FROM stage_runs r LEFT JOIN pipeline_versions v ON v.id=r.version_id WHERE r.stage_id=? AND r.state='queued' "
        "AND r.entered_at IS NULL "  # a version in a stage that starts manually waits in the stage, not here (#6138)
        "ORDER BY r.id DESC LIMIT ?", (stage["id"], WAITING_SHOWN))]
    block = None
    if t["block_reason"] and not block_ended(conn, t, at):
        block = {"reason": t["block_reason"], "by": t["block_by"], "at": t["block_at"], "until": t["block_until"],
                 "item": t["block_item"]}
    return {
        "mode": t["mode"], "arg": t["mode_arg"], "words": mode_words(t["mode"], t["mode_arg"]),
        "text": mode_text(t["mode"], t["mode_arg"]), "mode_by": t["mode_by"], "mode_at": t["mode_at"],
        "window": window, "freezes": freezes,
        "fault_rule": bool(t["fault_rule"]), "rule_by": t["rule_by"],
        "fault": _fault(conn, pipeline, cache) if t["fault_rule"] else None,
        "block": block,
        "delay": {"until": t["delay_until"], "by": t["delay_by"], "reason": t["delay_reason"]}
        if t["delay_until"] and core.parse_iso(t["delay_until"]) > at else None,
        "disabled": {"reason": t["disabled_reason"], "by": t["disabled_by"], "at": t["disabled_at"]}
        if t["disabled_at"] else None,
        "state": c["kind"] if c else "open", "state_text": c["text"] if c else mode_text(t["mode"], t["mode_arg"]),
        "set_by": (c or {}).get("by") or t["mode_by"], "until": (c or {}).get("until"),
        "waiting": waiting,
        **_join(conn, stage, pipeline, cache),
    }


def _join(conn, stage, pipeline, cache=None):
    """{"from": the stages before the arrow, "join": the newest version that passed some of them but not all, and
    the stages it still waits for} (#6142: a join lets a version in only when it passed every branch)."""
    key = ("graph", pipeline["id"])
    if cache is None or key not in cache:
        g = store.stage_graph(conn, pipeline["id"])
        if cache is not None:
            cache[key] = g
    else:
        g = cache[key]
    rows, prev, _ = g
    before = prev.get(stage["name"], [])
    out = {"from": before, "join": None}
    if len(before) < 2:
        return out
    ids = {r["name"]: r["id"] for r in rows}
    got = conn.execute("SELECT v.rev, s.name stage FROM stage_runs r JOIN pipeline_stages s ON s.id=r.stage_id JOIN "
                       "pipeline_versions v ON v.id=r.version_id WHERE r.state='passed' AND r.stage_id IN ("
                       + ",".join("?" * len(before)) + ") ORDER BY r.id DESC", [ids[b] for b in before]).fetchall()
    passed = {}
    for x in got:
        passed.setdefault(x["rev"], set()).add(x["stage"])
    there = {x["rev"] for x in conn.execute("SELECT v.rev FROM stage_runs r JOIN pipeline_versions v ON "
                                            "v.id=r.version_id WHERE r.stage_id=?", (stage["id"],))}
    for x in got:  # newest first
        missing = [b for b in before if b not in passed[x["rev"]]]
        if missing and x["rev"] not in there:
            out["join"] = {"rev": x["rev"], "missing": missing}
            break
    return out


def plain(mode, arg=None):
    """The arrow of a stage that has no controls of its own (a deploy target's default pipeline)."""
    return {"mode": mode, "arg": arg, "words": mode_words(mode, arg), "text": mode_text(mode, arg), "mode_by": None,
            "mode_at": None, "window": None, "freezes": [], "fault_rule": False, "rule_by": None, "fault": None,
            "block": None, "delay": None, "disabled": None, "state": "open", "state_text": mode_text(mode, arg),
            "set_by": None, "until": None, "waiting": [], "fixed": True, "from": None, "join": None}


# ---------------------------------------------------------------- changes

# The short word of each change for the audit log (#6144), from the first words of its text.
_ACTIONS = (("transition ", "transition"), ("blocked ", "block"), ("unblocked ", "unblock"), ("ended the delay", "delay"),
            ("delayed ", "delay"), ("disabled ", "disable"), ("enabled ", "enable"), ("removed the freezes", "freeze"),
            ("froze ", "freeze"), ("let version ", "pass"))


def _line(conn, pid, actor, text, reason=None, until=None, detail=None):
    """A line of the pipeline's audit log (inside a tx): river/pipeline/audit.py. It acts on the transition that the
    text names (build -> prod: the transition into prod), or on what a freeze names."""
    action = next((a for start, a in _ACTIONS if text.startswith(start)), "transition")
    m = re.search(r"transition \S+ -> (\S+?):?(?:\s|$)", text)
    place = f"the transition into {m.group(1)}" if m else (
        re.match(r"(?:froze|removed the freezes of) (.+?)(?: from |$)", text).group(1)
        if action == "freeze" and re.match(r"(?:froze|removed the freezes of) ", text) else None)
    audit.write(conn, pid, actor, action, text, place=place, reason=reason, until=until, detail=detail)


def _lead(conn, actor, what):
    from . import operators
    if operators.vouched(actor):
        return  # an operator's emergency action (#6145): operators.check let this actor act
    why = core._lead_refusal(conn, actor, what)
    if why:
        raise core.RiverError(why)


def _reason(reason, what):
    reason = (reason or "").strip()
    if not reason:
        raise core.RiverError(f"say why you {what}: --reason \"<why>\" (the people who look at the pipeline read it)")
    return reason


def _target(conn, pipeline, stage):
    """The pipeline row and the live stage row that a command names."""
    p = store._pipeline(conn, pipeline)
    s = conn.execute("SELECT * FROM pipeline_stages WHERE pipeline_id=? AND name=? AND removed_at IS NULL",
                     (p["id"], stage)).fetchone()
    if not s:
        raise core.RiverError(f"pipeline {p['name']} has no stage {stage}")
    return p, s


def _arrow(conn, p, s):
    _, prev, _ = store.stage_graph(conn, p["id"])  # a join has more than one stage before it (#6142)
    before = prev.get(s["name"], [])
    return f"{' + '.join(before)} -> {s['name']}" if before else f"start -> {s['name']}"


def set_mode(conn, pid, sid, mode, arg, actor):
    """Set the mode of the transition into a stage (inside a tx; define.stage_set calls it for --gate)."""
    _ensure(conn, pid, sid)
    conn.execute("UPDATE pipeline_transitions SET mode=?, mode_arg=?, mode_by=?, mode_at=? WHERE stage_id=?",
                 (mode, arg, actor, store._now(), sid))


def _when(text, zone):
    """A time a person types (2h, 'fri 17:00', 2026-11-27T08:00) as a stored UTC time."""
    return core.iso(core.parse_when(text, zone))


def change(conn, pipeline, stage, mode=None, window=None, fault_rule=None, reason=None, actor=None):
    """maxpm pipeline transition <name> <stage> --mode ... --window ... --fault-rule on|off: the standing rules of the
    arrow into a stage. window none removes the window. Returns describe() after the change."""
    _lead(conn, actor, "changes a transition, because it decides what reaches production")
    if mode is None and window is None and fault_rule is None:
        raise core.RiverError("give what to change: --mode, --window or --fault-rule")
    new_mode = parse_mode(mode) if mode is not None else None
    win = None
    if window is not None and window.strip().lower() not in ("", "none"):
        parse_window(window)
        win = " ".join(window.split())
    with core.tx(conn):
        p, s = _target(conn, pipeline, stage)
        old = get(conn, s["id"])
        _ensure(conn, p["id"], s["id"])
        parts = []
        if new_mode is not None and new_mode != (old["mode"], old["mode_arg"]):
            set_mode(conn, p["id"], s["id"], *new_mode, actor)
            parts.append(f"mode {mode_words(old['mode'], old['mode_arg'])} -> {mode_words(*new_mode)}")
        if window is not None and win != old["time_window"]:
            conn.execute("UPDATE pipeline_transitions SET time_window=?, window_by=?, window_at=? WHERE stage_id=?",
                         (win, actor, store._now(), s["id"]))
            parts.append(f"window {old['time_window'] or 'none'} -> {win or 'none'}")
        if fault_rule is not None and bool(fault_rule) != bool(old["fault_rule"]):
            conn.execute("UPDATE pipeline_transitions SET fault_rule=?, rule_by=?, rule_at=? WHERE stage_id=?",
                         (1 if fault_rule else 0, actor, store._now(), s["id"]))
            parts.append("blocked while a fault of the pipeline is open" if fault_rule else
                         "no longer blocked by an open fault")
        if not parts:
            raise core.RiverError(f"the transition {_arrow(conn, p, s)} has these rules already")
        _line(conn, p["id"], actor, f"transition {_arrow(conn, p, s)}: " + "; ".join(parts), reason)
    return dict(describe(conn, s, p), pipeline=p["name"], stage=s["name"], changed="; ".join(parts))


def block(conn, pipeline, stage, reason, until=None, until_done=None, actor=None):
    """maxpm pipeline block: no version passes until a time (--until), until an item closes (--until-done), or until
    someone unblocks it. A version that someone lets pass on purpose still does."""
    _lead(conn, actor, "blocks a transition")
    reason = _reason(reason, "block it")
    if until and until_done:
        raise core.RiverError("a block ends at a time (--until) or when an item closes (--until-done), not both")
    with core.tx(conn):
        p, s = _target(conn, pipeline, stage)
        end = _when(until, _zone(conn, p["project"])) if until else None
        if until_done is not None and not _item_open(conn, int(until_done)):
            raise core.RiverError(f"#{until_done} is not an open item")
        _ensure(conn, p["id"], s["id"])
        conn.execute("UPDATE pipeline_transitions SET block_reason=?, block_by=?, block_at=?, block_until=?, "
                     "block_item=? WHERE stage_id=?", (reason, actor, store._now(), end,
                                                       int(until_done) if until_done else None, s["id"]))
        what = (f"until {core.show_time(end)}" if end else f"until #{until_done} closes" if until_done
                else "until someone unblocks it")
        _line(conn, p["id"], actor, f"blocked the transition {_arrow(conn, p, s)} {what}", reason,
              end or (f"#{until_done}" if until_done else None))
    return dict(describe(conn, s, p), pipeline=p["name"], stage=s["name"], changed=f"blocked {what}")


def unblock(conn, pipeline, stage, actor=None):
    """maxpm pipeline unblock: end the block of a transition."""
    _lead(conn, actor, "unblocks a transition")
    with core.tx(conn):
        p, s = _target(conn, pipeline, stage)
        if not get(conn, s["id"])["block_reason"]:
            raise core.RiverError(f"the transition {_arrow(conn, p, s)} is not blocked")
        conn.execute("UPDATE pipeline_transitions SET block_reason=NULL, block_by=NULL, block_at=NULL, "
                     "block_until=NULL, block_item=NULL WHERE stage_id=?", (s["id"],))
        _line(conn, p["id"], actor, f"unblocked the transition {_arrow(conn, p, s)}")
    return dict(describe(conn, s, p), pipeline=p["name"], stage=s["name"], changed="unblocked")


def delay(conn, pipeline, stage, until, reason=None, actor=None):
    """maxpm pipeline delay: no version passes before a time (2h, 'fri 08:00'); none ends the delay."""
    _lead(conn, actor, "delays a transition")
    clear = str(until or "").strip().lower() in ("", "none")
    if not clear:
        reason = _reason(reason, "delay it")
    with core.tx(conn):
        p, s = _target(conn, pipeline, stage)
        _ensure(conn, p["id"], s["id"])
        if clear:
            if not get(conn, s["id"])["delay_until"]:
                raise core.RiverError(f"the transition {_arrow(conn, p, s)} has no delay")
            conn.execute("UPDATE pipeline_transitions SET delay_until=NULL, delay_by=NULL, delay_at=NULL, "
                         "delay_reason=NULL WHERE stage_id=?", (s["id"],))
            _line(conn, p["id"], actor, f"ended the delay of the transition {_arrow(conn, p, s)}")
            what = "the delay ended"
        else:
            end = _when(until, _zone(conn, p["project"]))
            conn.execute("UPDATE pipeline_transitions SET delay_until=?, delay_by=?, delay_at=?, delay_reason=? "
                         "WHERE stage_id=?", (end, actor, store._now(), reason, s["id"]))
            what = f"delayed until {core.show_time(end)}"
            _line(conn, p["id"], actor, f"{what}: the transition {_arrow(conn, p, s)}", reason, end)
    return dict(describe(conn, s, p), pipeline=p["name"], stage=s["name"], changed=what)


def disable(conn, pipeline, stage, reason, actor=None):
    """maxpm pipeline disable: no version passes until a person or the manager enables the transition again."""
    _lead(conn, actor, "disables a transition")
    reason = _reason(reason, "disable it")
    with core.tx(conn):
        p, s = _target(conn, pipeline, stage)
        _ensure(conn, p["id"], s["id"])
        conn.execute("UPDATE pipeline_transitions SET disabled_reason=?, disabled_by=?, disabled_at=? WHERE stage_id=?",
                     (reason, actor, store._now(), s["id"]))
        _line(conn, p["id"], actor, f"disabled the transition {_arrow(conn, p, s)} until someone enables it", reason)
    return dict(describe(conn, s, p), pipeline=p["name"], stage=s["name"], changed="disabled")


def enable(conn, pipeline, stage, actor=None):
    """maxpm pipeline enable: a disabled transition lets versions pass again."""
    _lead(conn, actor, "enables a transition")
    with core.tx(conn):
        p, s = _target(conn, pipeline, stage)
        if not get(conn, s["id"])["disabled_at"]:
            raise core.RiverError(f"the transition {_arrow(conn, p, s)} is not disabled")
        conn.execute("UPDATE pipeline_transitions SET disabled_reason=NULL, disabled_by=NULL, disabled_at=NULL "
                     "WHERE stage_id=?", (s["id"],))
        _line(conn, p["id"], actor, f"enabled the transition {_arrow(conn, p, s)}")
    return dict(describe(conn, s, p), pipeline=p["name"], stage=s["name"], changed="enabled")


def freeze(conn, pipeline, stage=None, start=None, end=None, reason=None, clear=False, actor=None):
    """maxpm pipeline freeze: no version passes from start to end (a time a person types; start none: now). With no
    stage: every transition of the pipeline. clear: remove the freezes that have not ended."""
    _lead(conn, actor, "freezes a transition")
    if not clear:
        reason = _reason(reason, "freeze it")
        if not end:
            raise core.RiverError("say until when: --to <time> ('fri 08:00', 2026-11-28T18:00, 3d)")
    with core.tx(conn):
        p = store._pipeline(conn, pipeline)
        stages = [_target(conn, pipeline, stage)[1]] if stage else conn.execute(
            "SELECT * FROM pipeline_stages WHERE pipeline_id=? AND removed_at IS NULL ORDER BY pos, id",
            (p["id"],)).fetchall()
        if not stages:
            raise core.RiverError(f"pipeline {p['name']} has no stages")
        zone, now = _zone(conn, p["project"]), core.now()
        where = f"the transition {_arrow(conn, p, stages[0])}" if stage else "every transition"
        if clear:
            n = 0
            for s in stages:
                left = [f for f in _freezes(get(conn, s["id"])) if core.parse_iso(f["to"]) <= now]
                n += len(_freezes(get(conn, s["id"]))) - len(left)
                conn.execute("UPDATE pipeline_transitions SET freezes=? WHERE stage_id=?",
                             (json.dumps(left) if left else None, s["id"]))
            if not n:
                raise core.RiverError(f"{where} of pipeline {p['name']} has no freeze to remove")
            _line(conn, p["id"], actor, f"removed the freezes of {where}")
            return {"pipeline": p["name"], "stage": stage, "changed": "freezes removed"}
        a = core.parse_when(start, zone) if start else now
        b = core.parse_when(end, zone, start=a)
        if b <= a:
            raise core.RiverError("the freeze ends before it starts")
        fz = {"from": core.iso(a), "to": core.iso(b), "reason": reason, "by": actor, "at": store._now()}
        for s in stages:
            _ensure(conn, p["id"], s["id"])
            keep = [f for f in _freezes(get(conn, s["id"])) if core.parse_iso(f["to"]) > now]
            conn.execute("UPDATE pipeline_transitions SET freezes=? WHERE stage_id=?", (json.dumps(keep + [fz]), s["id"]))
        what = f"froze {where} from {core.show_time(fz['from'])} to {core.show_time(fz['to'])}"
        _line(conn, p["id"], actor, what, reason, fz["to"])
    return {"pipeline": p["name"], "stage": stage, "changed": what, "freeze": fz}


def let_pass(conn, pipeline, stage, rev=None, reason=None, actor=None):
    """maxpm pipeline pass: let one waiting version past every control of the transition once (a block, the rule, a
    freeze, the window, a delay, disabled). A yes of the mode that the actor may give comes with it; a yes that only
    a named person gives still waits for that person."""
    _lead(conn, actor, "lets a version pass a transition")
    reason = _reason(reason, "let it pass")
    from . import runner
    with core.tx(conn):
        p, s = _target(conn, pipeline, stage)
        r = runner._waiting_run(conn, p, stage, rev)
        t = get(conn, s["id"])
        yes = r["approved_at"] is not None or t["mode"] in ("auto", "forge") or runner._may_say_yes(conn, t, actor)
        conn.execute("UPDATE stage_runs SET passed_by=?, passed_at=?, gate_note=NULL WHERE id=?",
                     (actor or "a person", store._now(), r["id"]))
        if yes and r["approved_at"] is None and t["mode"] not in ("auto", "forge"):
            conn.execute("UPDATE stage_runs SET approved_by=?, approved_at=? WHERE id=?",
                         (actor or "a person", store._now(), r["id"]))
        _line(conn, p["id"], actor, f"let version {r['rev']} pass the transition {_arrow(conn, p, s)} once (run "
                                    f"#{r['id']})" + ("" if yes else f"; it still waits for {mode_text(t['mode'], t['mode_arg'])}"),
              reason)
    if yes and r["gate_item"]:
        runner._drop_card(conn, r["gate_item"], f"let pass with maxpm pipeline pass (run #{r['id']})")
    return {"pipeline": p["name"], "stage": s["name"], "version": r["rev"], "run": r["id"], "passed_by": actor,
            "waits_for_yes": not yes}


def expire(conn):
    """Each pass of maxpm serve: a block whose time came or whose item closed, a delay that ended and a freeze that
    ended leave the transition, each with a line in the audit log (by maxpm)."""
    now = core.now()
    for t in conn.execute("SELECT t.*, s.name stage FROM pipeline_transitions t JOIN pipeline_stages s ON s.id=t.stage_id "
                          "WHERE t.block_reason IS NOT NULL OR t.delay_until IS NOT NULL OR t.freezes IS NOT NULL"
                          ).fetchall():
        sets, why = {}, []
        if t["block_reason"] and block_ended(conn, t, now):
            sets.update(block_reason=None, block_by=None, block_at=None, block_until=None, block_item=None)
            why.append(f"the block of {t['block_by'] or 'a person'} ended (" + (
                f"#{t['block_item']} closed" if t["block_item"] else "its time came") + ")")
        if t["delay_until"] and core.parse_iso(t["delay_until"]) <= now:
            sets.update(delay_until=None, delay_by=None, delay_at=None, delay_reason=None)
            why.append("the delay ended")
        fz = _freezes(t)
        left = [f for f in fz if core.parse_iso(f["to"]) > now]
        if len(left) != len(fz):
            sets["freezes"] = json.dumps(left) if left else None
            why.append("a freeze ended")
        if not sets:
            continue
        with core.tx(conn):
            conn.execute("UPDATE pipeline_transitions SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?",
                         list(sets.values()) + [t["id"]])
            p = conn.execute("SELECT * FROM pipelines WHERE id=?", (t["pipeline_id"],)).fetchone()
            s = conn.execute("SELECT * FROM pipeline_stages WHERE id=?", (t["stage_id"],)).fetchone()
            _line(conn, t["pipeline_id"], "maxpm", f"transition {_arrow(conn, p, s)}: " + "; ".join(why))


def export(conn, stage_id):
    """The standing rules of a transition for maxpm pipeline export: its mode, window, freezes that have not ended and
    the fault rule (a block, a delay or disabled is an action of a moment, not a definition)."""
    t = get(conn, stage_id)
    now = core.now()
    return {"mode": mode_words(t["mode"], t["mode_arg"]), "window": t["time_window"],
            "freezes": [{k: f[k] for k in ("from", "to", "reason")} for f in _freezes(t)
                        if core.parse_iso(f["to"]) > now],
            "fault_rule": bool(t["fault_rule"])}
