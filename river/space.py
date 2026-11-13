"""Size limits of the queue (#5711, mark 2026-11-13: 'a setting for max size, at which it starts to clean up oldest
first until its back under the limit').

Two limits, both settings (mark 2026-11-13: 'that limit should be configurable'): db_max_size for the database
(the data in it, not its free pages) and folder_max_size for the folder beside the queue (all of it, the database
too). 'off' turns a limit off. When a limit is crossed, maxpm serve (serve_pass, in the loop; and once a day) or
maxpm cleanup --space removes the oldest removable data first, in a fixed order, until the size is at 90% of the
limit (LOW_WATER: so the data of the next day does not start it again at once). Each kind of data has its own
minimum age (a space_*_after setting): nothing younger goes, even over the limit.

The database, in this order:
  1. ops records: the runs of the operations checks (not the newest run of a check, not a run that a detection
     names), their state history, and detections of closed or no fault items;
  2. notices that were read, and the send records of closed needs-you rows;
  3. events of items closed before space_events_after; kept: the first and the last event of each item, events of
     a person, and events that hold a decision, an answer, a check, or the close;
  4. canvas versions between the first and the newest (not a version that a comment was written on);
  5. ended sessions: agents not seen since space_sessions_after that hold nothing, their sent prompts, and old
     hook run records.
  Then VACUUM, so the file shrinks.
Never removed: items (open or closed: title, notes, output, changed line), open items' history, policies, messages
other than read notices, needs-you rows, goals and their handoffs, settings of live agents.

The folder, in this order: the caches of browsers that do not run (Cache, Code Cache, GPU caches: Chrome builds
them again; the sign-ins stay), uploads older than space_files_after, and backups older than it except the newest.
Browser profiles are sign-ins, not data to delete: the report counts them.

A real run writes one event (no item) and sends the active manager one note. --dry-run removes nothing: it runs the
database part inside a transaction that it rolls back, so its counts are exact."""

import json
import os
import re
import shutil
from datetime import timedelta
from pathlib import Path

from . import core
from .core import RiverError, iso, now, parse_duration, parse_iso, setting, tx

UNITS = {"b": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3, "tb": 1024 ** 4}
LOW_WATER = 0.9
BATCH = 500  # rows in one transaction: a command that waits for the lock waits for one batch, not the cleanup
ROW = 64  # bytes a row takes beside its text (the row header, its rowid and its index entries), for the estimate
EVERY = timedelta(days=1)  # serve checks the limits at least this often, and measures the folder
FOLDER_EVERY = timedelta(hours=1)  # a walk of the folder takes about half a second for 2 GB
TELL_OVER_EVERY = timedelta(days=1)  # over a limit with nothing left to remove: the manager hears once a day
META = "space"
# Chrome's caches: folders that it fills from the network and builds again when they are gone (no sign-in, no
# history, no page storage). The last four are components that Chrome downloads into each profile (its model
# files, a speech engine, the download cache of components): about 125 MB in each of 9 profiles on mark's Mac.
BROWSER_CACHES = ("Cache", "Code Cache", "GPUCache", "DawnWebGPUCache", "DawnGraphiteCache", "GrShaderCache",
                  "GraphiteDawnCache", "ShaderCache", "component_crx_cache", "optimization_guide_model_store",
                  "WasmTtsEngine", "OnDeviceHeadSuggestModel")


def parse_size(value):
    """A size setting: '500MB', '2GB', '64KB', '1048576' (bytes); None for off."""
    v = re.sub(r"\s+", "", str(value or "")).lower()
    if v in ("off", "0", ""):
        return None
    m = re.match(r"^(\d+(?:\.\d+)?)(b|kb|mb|gb|tb)?$", v)
    if not m or float(m.group(1)) <= 0:
        raise RiverError(f"a size is a number with KB, MB or GB, such as 500MB or 2GB, or off; not {value!r}")
    return int(float(m.group(1)) * UNITS[m.group(2) or "b"])


def show_size(n):
    if n is None:
        return "off"
    for unit, size in (("GB", UNITS["gb"]), ("MB", UNITS["mb"]), ("KB", UNITS["kb"])):
        if n >= size:
            x = n / size
            return f"{x:.1f} {unit}" if x < 10 else f"{x:.0f} {unit}"
    return f"{n} bytes"


# ---------------------------------------------------------------- measures

def db_file(conn):
    return Path(conn.execute("PRAGMA database_list").fetchone()[2])


def db_used(conn):
    """The bytes the data takes: the pages in use (free pages are space that a VACUUM gives back)."""
    ps = conn.execute("PRAGMA page_size").fetchone()[0]
    pc = conn.execute("PRAGMA page_count").fetchone()[0]
    fl = conn.execute("PRAGMA freelist_count").fetchone()[0]
    return (pc - fl) * ps


def db_files_bytes(conn):
    """The database file and its write-ahead log on disk."""
    f = db_file(conn)
    return sum(_file_bytes(Path(str(f) + s)) for s in ("", "-wal", "-shm"))


def _file_bytes(p):
    try:
        st = os.lstat(p)
    except OSError:
        return 0
    return getattr(st, "st_blocks", 0) * 512 or st.st_size


def tree_bytes(path):
    """The bytes on disk under a folder (as du counts them), without following links."""
    total = 0
    for root, dirs, files in os.walk(path):
        for name in files:
            total += _file_bytes(os.path.join(root, name))
    return total


def _profiles(root):
    """The browser profiles beside the queue (the project's and each session's)."""
    return sorted(p for p in list(root.glob("browsers/*/profile")) + list(root.glob("browsers/*/sessions/*/profile"))
                  if p.is_dir())


def _caches(profile):
    return [p for base in (profile, profile / "Default") for p in (base / n for n in BROWSER_CACHES)
            if p.is_dir() and not p.is_symlink()]


def _running(profile):
    """Chrome runs on this profile: it keeps the SingletonLock link (<host>-<pid>) there while it runs. A Chrome that
    was stopped hard leaves the link, so the pid must live, and not as a zombie (a Chrome that serve stopped and did
    not reap shows as one). When the process cannot be read, it counts as running: its cache stays."""
    import subprocess
    lock = profile / "SingletonLock"
    if not os.path.lexists(lock):
        return False
    try:
        pid = int(os.readlink(lock).rsplit("-", 1)[1])
    except (OSError, ValueError, IndexError):
        return True
    if not core.pid_alive(pid):
        return False
    if os.name == "nt":
        return True
    try:
        stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return True
    return not stat.strip().startswith("Z")  # no line (a sandbox hides the process): running


def _meta(conn):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (META,)).fetchone()
    try:
        return json.loads(r["value"]) if r else {}
    except ValueError:
        return {}


def _save_meta(conn, m):
    with tx(conn):
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (META, json.dumps(m)))


def current(conn):
    """The sizes for the page's settings: the database now, the folder as serve measured it last."""
    m = _meta(conn)
    folder = f"now {show_size(m['folder'])} (measured {core.show_time(m['folder_at'])})" if m.get("folder") else \
        "not measured yet (maxpm cleanup --dry-run)"
    return {"db_max_size": f"now {show_size(db_used(conn))}", "folder_max_size": folder}


# ---------------------------------------------------------------- the database, oldest first

def _gone(conn, table, rows):
    ids = [r[0] for r in rows]
    for i in range(0, len(ids), 400):
        part = ids[i:i + 400]
        conn.execute(f"DELETE FROM {table} WHERE id IN ({','.join('?' * len(part))})", part)
    return len(ids), sum((r[1] or 0) + ROW for r in rows)


def _ops(conn, cutoff, n):
    rows = conn.execute(
        "SELECT id, COALESCE(length(summary),0)+COALESCE(length(perfdata),0)+COALESCE(length(evidence),0)"
        "+COALESCE(length(output),0)+COALESCE(length(subjects),0)+COALESCE(length(missed),0) FROM ops_runs r "
        "WHERE started_at<? AND ended_at IS NOT NULL "
        "AND id<>(SELECT MAX(id) FROM ops_runs x WHERE x.check_id=r.check_id) "
        "AND id NOT IN (SELECT run_id FROM ops_detections WHERE run_id IS NOT NULL) ORDER BY id LIMIT ?",
        (cutoff, n)).fetchall()
    if rows:
        return _gone(conn, "ops_runs", rows)
    rows = conn.execute(
        "SELECT id, COALESCE(length(note),0)+length(key) FROM ops_events e WHERE at<? "
        "AND id<>(SELECT MAX(id) FROM ops_events x WHERE x.key=e.key) ORDER BY id LIMIT ?", (cutoff, n)).fetchall()
    if rows:
        return _gone(conn, "ops_events", rows)
    rows = conn.execute(
        "SELECT d.id, COALESCE(length(d.summary),0)+COALESCE(length(d.evidence),0)+COALESCE(length(d.why),0) "
        "FROM ops_detections d LEFT JOIN items i ON i.id=d.item_id WHERE d.last_seen<? "
        "AND (d.item_id IS NULL OR i.status IN ('done','dropped')) ORDER BY d.last_seen LIMIT ?", (cutoff, n)).fetchall()
    return _gone(conn, "ops_detections", rows)


def _notices(conn, cutoff, n):
    rows = conn.execute(
        "SELECT m.id, length(m.body) FROM messages m WHERE m.kind='notice' AND m.read_at IS NOT NULL AND m.state<>'open' "
        "AND m.created_at<? AND m.id NOT IN (SELECT message_id FROM needs_you WHERE message_id IS NOT NULL) "
        "AND m.id NOT IN (SELECT reply_to FROM messages WHERE reply_to IS NOT NULL) "
        "AND m.id NOT IN (SELECT thread_id FROM messages WHERE thread_id<>id) ORDER BY m.id LIMIT ?",
        (cutoff, n)).fetchall()
    if rows:
        return _gone(conn, "messages", rows)
    # The send records of needs-you rows that are closed: the dispatcher sends nothing more for them.
    rows = conn.execute(
        "SELECT s.id, COALESCE(length(s.last_error),0) FROM notifications s JOIN needs_you y ON y.id=s.event_id "
        "WHERE s.state IN ('sent','failed') AND y.closed_at IS NOT NULL AND s.created_at<? ORDER BY s.id LIMIT ?",
        (cutoff, n)).fetchall()
    return _gone(conn, "notifications", rows)


# The events that stay with a closed item: they hold a decision, an answer, a check or the close.
_KEEP_EVENT = ("done%", "dropped%", "marked done%", "reopened%", "check %", "%answer%", "%decision%", "%decided%",
               "%takeover%", "%took over%")


def _events(conn, cutoff, n):
    keep = " OR ".join("e.change LIKE ?" for _ in _KEEP_EVENT)
    rows = conn.execute(
        "SELECT e.id, length(e.change)+COALESCE(length(e.actor),0) FROM events e JOIN items i ON i.id=e.item_id "
        "WHERE i.status IN ('done','dropped') AND i.closed_at<? "
        "AND e.id<>(SELECT MIN(id) FROM events f WHERE f.item_id=e.item_id) "
        "AND e.id<>(SELECT MAX(id) FROM events f WHERE f.item_id=e.item_id) "
        f"AND NOT ({keep}) "
        "AND NOT EXISTS (SELECT 1 FROM agents a WHERE a.name=e.actor AND a.kind='human') ORDER BY e.id LIMIT ?",
        (cutoff, *_KEEP_EVENT, n)).fetchall()
    return _gone(conn, "events", rows)


def _canvases(conn, cutoff, n):
    rows = conn.execute(
        "SELECT v.id, length(v.body)+COALESCE(length(v.note),0) FROM canvas_versions v WHERE v.created_at<? "
        "AND v.version>(SELECT MIN(version) FROM canvas_versions x WHERE x.canvas_id=v.canvas_id) "
        "AND v.version<(SELECT MAX(version) FROM canvas_versions x WHERE x.canvas_id=v.canvas_id) "
        "AND NOT EXISTS (SELECT 1 FROM canvas_comments c WHERE c.canvas_id=v.canvas_id AND c.version=v.version) "
        "ORDER BY v.created_at, v.id LIMIT ?", (cutoff, n)).fetchall()
    return _gone(conn, "canvas_versions", rows)


def _sessions(conn, cutoff, n):
    rows = conn.execute(
        "SELECT id, length(body) FROM sent_prompts WHERE sent_at<? ORDER BY id LIMIT ?", (cutoff, n)).fetchall()
    if rows:
        return _gone(conn, "sent_prompts", rows)
    rows = conn.execute(
        "SELECT id, COALESCE(length(output),0)+length(command)+COALESCE(length(result),0) FROM hook_runs "
        "WHERE queued_at<? AND ended_at IS NOT NULL ORDER BY id LIMIT ?", (cutoff, n)).fetchall()
    if rows:
        return _gone(conn, "hook_runs", rows)
    # Agents not seen since the cutoff that hold nothing: the same rule as maxpm unregister, which deletes the row.
    names = [r[0] for r in conn.execute(
        "SELECT name FROM agents a WHERE kind='ai' AND last_seen<? "
        "AND NOT EXISTS (SELECT 1 FROM items i WHERE i.assignee=a.name AND i.status IN ('in_progress','held')) "
        "AND NOT EXISTS (SELECT 1 FROM items i WHERE i.reserved_for=a.name AND i.status IN ('open','in_progress','held')) "
        "AND NOT EXISTS (SELECT 1 FROM targets t WHERE t.owner=a.name) "
        "AND NOT EXISTS (SELECT 1 FROM goals g WHERE g.owner=a.name) "
        "AND NOT EXISTS (SELECT 1 FROM queue_entries q WHERE q.agent=a.name) "
        "ORDER BY last_seen LIMIT ?", (cutoff, n))]
    for name in names:
        conn.execute("DELETE FROM session_pins WHERE agent=?", (name,))  # its cascade, with the key checks off
        conn.execute("DELETE FROM agents WHERE name=?", (name,))
        conn.execute("DELETE FROM settings WHERE scope=?", (f"agent:{name}",))
    return len(names), len(names) * 600


# The order of the database part, each with the setting of its minimum age.
STAGES = (("ops records", "space_ops_after", _ops),
          ("read notices", "space_notices_after", _notices),
          ("events of closed items", "space_events_after", _events),
          ("canvas versions", "space_canvas_after", _canvases),
          ("ended sessions", "space_sessions_after", _sessions))


def _vacuum(conn):
    """Give the free pages back to the disk. Other commands wait for it (about a second for 100 MB)."""
    try:
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return None
    except Exception as e:  # a busy queue: the next run tries again
        return f"{type(e).__name__}: {e}"


def _db_part(conn, dry_run, t):
    limit = parse_size(setting(conn, "db_max_size"))
    used = db_used(conn)
    out = {"limit": limit, "used": used, "file": db_files_bytes(conn), "removed": []}
    if not limit or used <= limit:
        out.update(used_after=used, over=False)
        return out
    target, est = int(limit * LOW_WATER), used
    # The foreign key checks are off while it deletes: each select above takes only rows that nothing refers to,
    # and with the checks on, each deleted message scanned the messages and needs-you tables (no index on
    # reply_to or message_id): 4,365 notices took 7 s. The setting is of this connection only.
    conn.execute("PRAGMA foreign_keys=OFF")
    if dry_run:
        conn.execute("BEGIN IMMEDIATE")
    try:
        for what, key, fn in STAGES:
            cutoff = iso(t - parse_duration(setting(conn, key)))
            rows = freed = 0
            while est > target:
                if dry_run:
                    k, b = fn(conn, cutoff, BATCH)
                else:
                    with tx(conn):
                        k, b = fn(conn, cutoff, BATCH)
                if not k:
                    break
                rows, freed, est = rows + k, freed + b, est - b
            if rows:
                out["removed"].append({"what": what, "rows": rows, "bytes": freed,
                                       "older_than": setting(conn, key)})
            if est <= target:
                break
    finally:
        if dry_run:
            conn.execute("ROLLBACK")
        conn.execute("PRAGMA foreign_keys=ON")
    if dry_run or not out["removed"]:
        out.update(used_after=max(est, 0), over=est > limit)
        return out
    out["vacuum_error"] = _vacuum(conn)
    out.update(used_after=db_used(conn), file_after=db_files_bytes(conn))
    out["over"] = out["used_after"] > limit
    return out


# ---------------------------------------------------------------- the folder, oldest first

def _candidates(root, cutoff):
    """What the folder part may remove, in its order: (what, path, bytes)."""
    caches = []
    for prof in _profiles(root):
        if not _running(prof):
            caches += [(p.stat().st_mtime, p) for p in _caches(prof)]
    for _, p in sorted(caches, key=lambda x: x[0]):
        yield "browser caches", p, tree_bytes(p)
    ups = []
    for r, _, files in os.walk(root / "uploads"):
        for name in files:
            p = Path(r) / name
            try:
                m = p.lstat().st_mtime
            except OSError:
                continue
            if m < cutoff:
                ups.append((m, p))
    for _, p in sorted(ups):
        yield "uploads", p, _file_bytes(p)
    backups = sorted((p.lstat().st_mtime, p) for p in (root / "backups").glob("*")) if (root / "backups").is_dir() else []
    for m, p in backups[:-1]:  # the newest backup always stays
        if m < cutoff:
            yield "backups", p, tree_bytes(p) if p.is_dir() else _file_bytes(p)


def _remove(p):
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p, ignore_errors=True)
    else:
        try:
            p.unlink()
        except OSError:
            pass


def _kept_profiles(root):
    profs = _profiles(root)
    size = sum(tree_bytes(p) - sum(tree_bytes(c) for c in _caches(p)) for p in profs)
    return {"browser_profiles": len(profs), "bytes": size, "running": sum(1 for p in profs if _running(p))}


def _folder_part(conn, dry_run, t):
    limit = parse_size(setting(conn, "folder_max_size"))
    root = db_file(conn).parent
    size = tree_bytes(root)
    out = {"path": str(root), "limit": limit, "size": size, "removed": [], "kept": _kept_profiles(root)}
    if not limit or size <= limit:
        out.update(size_after=size, over=False)
        return out
    target, left = int(limit * LOW_WATER), size
    cutoff = (t - parse_duration(setting(conn, "space_files_after"))).timestamp()
    groups = {}
    for what, p, b in _candidates(root, cutoff):
        if left <= target:
            break
        if not dry_run:
            _remove(p)
        left -= b
        g = groups.setdefault(what, {"what": what, "files": 0, "bytes": 0})
        g["files"] += 1
        g["bytes"] += b
    if not dry_run and "uploads" in groups:
        for d in (root / "uploads").glob("*"):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
    out["removed"] = list(groups.values())
    out["size_after"] = tree_bytes(root) if groups and not dry_run else left
    out["over"] = out["size_after"] > limit
    if groups and not dry_run:
        out["kept"] = _kept_profiles(root)
    return out


# ---------------------------------------------------------------- the run

def summary(res):
    """One line for the event and the note."""
    parts = []
    for side, key in (("db", "rows"), ("folder", "files")):
        for r in res[side]["removed"]:
            parts.append(f"{r[key]} {key} of {r['what']} ({show_size(r['bytes'])})")
    d, f = res["db"], res["folder"]
    verb = "would remove " if res.get("dry_run") else "removed "
    line = ("space cleanup: " + (verb + ", ".join(parts) if parts else "nothing to remove")
            + f"; database {show_size(d['used'])} -> {show_size(d['used_after'])} (limit {show_size(d['limit'])})"
            + f", folder {show_size(f['size'])} -> {show_size(f['size_after'])} (limit {show_size(f['limit'])})")
    over = [n for n, x in (("database", d), ("folder", f)) if x.get("over")]
    if over:
        line += f"; still over the limit: {' and '.join(over)} (nothing older is removable: maxpm cleanup --dry-run)"
    return line


def run(conn, dry_run=False, actor=None):
    """Remove the oldest removable data until each size is under its limit (see the module text). dry_run: remove
    nothing, say what would go."""
    t = now()
    res = {"dry_run": dry_run, "db": _db_part(conn, dry_run, t), "folder": _folder_part(conn, dry_run, t)}
    res["summary"] = summary(res)
    if dry_run:
        return res
    m = _meta(conn)
    m.update(last_run=iso(t), folder=res["folder"]["size_after"], folder_at=iso(t),
             removed=bool(res["db"]["removed"] or res["folder"]["removed"]))
    over = res["db"].get("over") or res["folder"].get("over")
    tell = m["removed"] or (over and (not m.get("told_over") or t - parse_iso(m["told_over"]) >= TELL_OVER_EVERY))
    if over and tell:
        m["told_over"] = iso(t)
    _save_meta(conn, m)
    if tell:
        with tx(conn):
            core._event(conn, None, actor or "maxpm", res["summary"])
        mgr = core.active_manager(conn)
        if mgr:
            with tx(conn):
                core._send(conn, "notice", "maxpm", res["summary"], to=mgr, level="low")
        res["told"] = mgr
    return res


def serve_pass(conn):
    """One pass of the loop of maxpm serve: run the cleanup when a limit is crossed (the database is measured at each
    pass, the folder once in FOLDER_EVERY), and check once a day. Over a limit with nothing removed in the last
    FOLDER_EVERY: wait, so a queue that only holds protected data is not walked at every pass."""
    t = now()
    m = _meta(conn)
    last = parse_iso(m["last_run"]) if m.get("last_run") else None
    if last and not m.get("removed") and t - last < FOLDER_EVERY:
        return None
    dbl, fl = parse_size(setting(conn, "db_max_size")), parse_size(setting(conn, "folder_max_size"))
    if not dbl and not fl:
        return None
    over = bool(dbl and db_used(conn) > dbl)
    if not over and fl:
        at = parse_iso(m["folder_at"]) if m.get("folder_at") else None
        if not at or t - at >= FOLDER_EVERY:
            m.update(folder=tree_bytes(db_file(conn).parent), folder_at=iso(t))
            _save_meta(conn, m)
        over = m["folder"] > fl
    if not over and last and t - last < EVERY:
        return None
    return run(conn)
