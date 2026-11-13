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
import sqlite3
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


def db_used(conn, s="main"):
    """The bytes the data takes: the pages in use (free pages are space that a VACUUM gives back)."""
    ps = conn.execute(f"PRAGMA {s}.page_size").fetchone()[0]
    pc = conn.execute(f"PRAGMA {s}.page_count").fetchone()[0]
    fl = conn.execute(f"PRAGMA {s}.freelist_count").fetchone()[0]
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


def _notices(conn, cutoff, n, s="main"):
    rows = conn.execute(
        f"SELECT m.id, length(m.body) FROM {s}.messages m WHERE m.kind='notice' AND m.read_at IS NOT NULL "
        f"AND m.state<>'open' AND m.created_at<? "
        f"AND m.id NOT IN (SELECT message_id FROM {s}.needs_you WHERE message_id IS NOT NULL) "
        f"AND m.id NOT IN (SELECT reply_to FROM {s}.messages WHERE reply_to IS NOT NULL) "
        f"AND m.id NOT IN (SELECT thread_id FROM {s}.messages WHERE thread_id<>id) ORDER BY m.id LIMIT ?",
        (cutoff, n)).fetchall()
    if rows or s != "main":
        return _gone(conn, f"{s}.messages", rows)
    # The send records of needs-you rows that are closed: the dispatcher sends nothing more for them.
    rows = conn.execute(
        "SELECT s.id, COALESCE(length(s.last_error),0) FROM notifications s JOIN needs_you y ON y.id=s.event_id "
        "WHERE s.state IN ('sent','failed') AND y.closed_at IS NOT NULL AND s.created_at<? ORDER BY s.id LIMIT ?",
        (cutoff, n)).fetchall()
    return _gone(conn, "notifications", rows)


# The events that stay with a closed item: they hold a decision, an answer, a check or the close.
_KEEP_EVENT = ("done%", "dropped%", "marked done%", "reopened%", "check %", "%answer%", "%decision%", "%decided%",
               "%takeover%", "%took over%")


def _events(conn, cutoff, n, s="main"):
    keep = " OR ".join("e.change LIKE ?" for _ in _KEEP_EVENT)
    rows = conn.execute(
        f"SELECT e.id, length(e.change)+COALESCE(length(e.actor),0) FROM {s}.events e JOIN {s}.items i "
        "ON i.id=e.item_id WHERE i.status IN ('done','dropped') AND i.closed_at<? "
        f"AND e.id<>(SELECT MIN(id) FROM {s}.events f WHERE f.item_id=e.item_id) "
        f"AND e.id<>(SELECT MAX(id) FROM {s}.events f WHERE f.item_id=e.item_id) "
        f"AND NOT ({keep}) "
        "AND NOT EXISTS (SELECT 1 FROM main.agents a WHERE a.name=e.actor AND a.kind='human') ORDER BY e.id LIMIT ?",
        (cutoff, *_KEEP_EVENT, n)).fetchall()
    return _gone(conn, f"{s}.events", rows)


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


# The archive file over archive_max_size: its oldest history goes; items, their titles, outputs and decisions stay.
ARCHIVE_STAGES = (("archived read notices", "space_notices_after", _notices),
                  ("archived events", "space_events_after", _events))


def _vacuum(conn, s="main"):
    """Give the free pages back to the disk. Other commands wait for it (about a second for 100 MB)."""
    try:
        conn.execute(f"VACUUM {s}")
        conn.execute(f"PRAGMA {s}.wal_checkpoint(TRUNCATE)")
        return None
    except Exception as e:  # a busy queue: the next run tries again
        return f"{type(e).__name__}: {e}"


def _db_part(conn, dry_run, t, s="main"):
    """The database (s main, with db_max_size and STAGES) or the archive file (s archive, archive_max_size and
    ARCHIVE_STAGES) over its limit: the stages remove the oldest rows first. The caller compacts the file."""
    limit = parse_size(setting(conn, "db_max_size" if s == "main" else "archive_max_size"))
    used = db_used(conn, s)
    out = {"limit": limit, "used": used, "removed": []}
    if s == "main":
        out["file"] = db_files_bytes(conn)
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
        for what, key, fn in (STAGES if s == "main" else ARCHIVE_STAGES):
            cutoff = iso(t - parse_duration(setting(conn, key)))
            rows = freed = 0
            while est > target:
                if dry_run:
                    k, b = fn(conn, cutoff, BATCH) if s == "main" else fn(conn, cutoff, BATCH, s)
                else:
                    with tx(conn):
                        k, b = fn(conn, cutoff, BATCH) if s == "main" else fn(conn, cutoff, BATCH, s)
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
    out.update(used_after=max(est, 0), over=est > limit)  # run() measures again after it compacts the file
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


# ---------------------------------------------------------------- the archive (#5712)
#
# mark 2026-11-13: 'we can also probably separate out old tasks from the rest of the items that need to be cleaned'.
# Items closed longer than archive_after move, with their history, into <queue>-archive.db beside the queue (its
# own SQLite file, ATTACHed as 'archive'), so the main database keeps open items, recent ones and what they link to,
# and each page build and command reads less. The archive holds copies: the move copies first and commits, then
# deletes from the main database, so a crash between leaves a copy in both, never in neither. The main database wins:
# an item in both is the main one. Reads: maxpm show and search read the archive too (marked archived); the Done tab
# ('with archived'), maxpm usage --archive and health --archive bring it in on demand (the manager decided
# 2026-11-13: the page builds read only the main database). Any command that names an archived item by id
# (reopen, dep, edit, add --found-during, ...) brings it back first (core._item).

ARCHIVE = "archive"
# The tables that move with an item: (table, the columns that name the item, the columns that name a row).
ARCHIVE_TABLES = (("items", ("id",), ("id",)), ("events", ("item_id",), ("id",)), ("messages", ("item_id",), ("id",)),
                  ("needs_you", ("item_id",), ("id",)), ("deps", ("item_id", "blocked_by"), ("item_id", "blocked_by")),
                  ("item_goals", ("item_id",), ("item_id", "goal_id")), ("item_refs", ("item_id",), ("item_id", "ref")),
                  ("holds", ("item_id",), ("id",)), ("item_usage", ("item_id",), ("item_id", "session_id")),
                  ("close_checks", ("closed_id", "item_id"), ("closed_id", "item_id")))
ARCHIVE_BATCH = 200  # items in one move


class _Undo(Exception):
    """Raised inside a tx to roll it back: a read of archived items as if they were in the main database."""


def archive_path(conn):
    f = db_file(conn)
    return f.with_name(f.stem + "-archive" + (f.suffix or ".db"))


def attached(conn):
    return any(r[1] == ARCHIVE for r in conn.execute("PRAGMA database_list"))


def attach(conn, create=False):
    """ATTACH the archive file when it exists (create: make it). SQLite attaches only outside a transaction: inside
    one this returns whether it is attached already. core.connect attaches it, so a command sees it at once."""
    if attached(conn):
        return True
    p = archive_path(conn)
    if conn.in_transaction or not (create or p.exists()):
        return False
    conn.execute("ATTACH DATABASE ? AS archive", (str(p),))
    if create:
        conn.execute("PRAGMA archive.journal_mode=WAL")
    return True


def _cols(conn, s, table):
    return [r[1] for r in conn.execute(f"PRAGMA {s}.table_info({table})")]


def _sync_tables(conn):
    """The archive's tables have the main tables' columns (a later release adds columns to the main ones), without
    their constraints: a foreign key cannot reach across two files."""
    for t, keys, _ in ARCHIVE_TABLES:
        have = _cols(conn, ARCHIVE, t)
        main = list(conn.execute(f"PRAGMA main.table_info({t})"))
        if not have:
            conn.execute(f"CREATE TABLE archive.{t} (" + ", ".join(f'"{r[1]}" {r[2] or ""}' for r in main) + ")")
            for k in keys:
                conn.execute(f"CREATE INDEX archive.{t}_{k} ON {t}({k})")
            continue
        for r in main:
            if r[1] not in have:
                conn.execute(f'ALTER TABLE archive.{t} ADD COLUMN "{r[1]}" {r[2] or ""}')


def _ids(conn, sql, params=()):
    try:
        return {r[0] for r in conn.execute(sql, params) if r[0] is not None}
    except sqlite3.OperationalError:  # a table of another schema version
        return set()


def archivable(conn, cutoff):
    """The items closed before cutoff that may move, oldest close first. One stays when an open item links to it
    (a dep either way), when a row that stays names it (a queue entry, a canvas, a marked-up page, an open needs-you
    row, an open goal, an operations record, a schedule, a policy, a message of another item that answers one of its
    messages), when it is the newest closed deploy item of its target (a release cadence counts from it), or when an
    item that stays was found during it (its found_during would name an item the main database lacks)."""
    rows = conn.execute("SELECT id, closed_at FROM items WHERE status IN ('done','dropped') AND closed_at<?",
                        (cutoff,)).fetchall()
    cand = {r[0]: r[1] for r in rows}
    if not cand:
        return []
    open_ids = _ids(conn, f"SELECT id FROM items WHERE status IN {core.OPEN_STATES}")
    keep = set()
    for a, b in conn.execute("SELECT item_id, blocked_by FROM deps"):
        if a in open_ids:
            keep.add(b)
        if b in open_ids:
            keep.add(a)
    for sql in ("SELECT item_id FROM queue_entries", "SELECT item_id FROM canvases", "SELECT item_id FROM annotations",
                "SELECT item_id FROM expert_proposals", "SELECT item_id FROM ops_causes",
                "SELECT item_id FROM ops_detections", "SELECT item_id FROM ops_proposals", "SELECT item_id FROM ops_systems",
                "SELECT last_item FROM schedules", "SELECT item_id FROM needs_you WHERE closed_at IS NULL",
                "SELECT ig.item_id FROM item_goals ig JOIN goals g ON g.id=ig.goal_id WHERE g.status='open'",
                "SELECT MAX(id) FROM items WHERE kind='deploy' AND status IN ('done','dropped') GROUP BY target",
                "SELECT m.item_id FROM messages m JOIN needs_you y ON y.message_id=m.id "
                "WHERE y.item_id IS NULL OR y.item_id<>m.item_id",
                "SELECT p.item_id FROM messages c JOIN messages p ON p.id=c.reply_to OR (p.id=c.thread_id AND c.id<>p.id) "
                "WHERE c.item_id IS NULL OR c.item_id<>p.item_id"):
        keep |= _ids(conn, sql)
    for r in conn.execute("SELECT items FROM policies WHERE items IS NOT NULL AND items<>''"):
        keep |= {int(x) for x in re.findall(r"\d+", str(r[0]))}
    S = set(cand) - keep
    children = {}
    for i, f in conn.execute("SELECT id, found_during FROM items WHERE found_during IS NOT NULL"):
        children.setdefault(f, []).append(i)
    while True:
        drop = {i for i in S if any(c not in S for c in children.get(i, ()))}
        if not drop:
            break
        S -= drop
    return sorted(S, key=lambda i: (cand[i], i))


def _cond(keys, ids, prefix=""):
    marks = ",".join("?" * len(ids))
    return " OR ".join(f"{prefix}{k} IN ({marks})" for k in keys), list(ids) * len(keys)


def _move(conn, ids):
    """Copy the items and their rows into the archive (one transaction), then delete them from the main database
    (another), with the foreign key checks off: archivable() keeps every item that a staying row names."""
    with tx(conn):
        for t, keys, row in ARCHIVE_TABLES:
            cond, params = _cond(keys, ids)
            cols = ",".join(f'"{c}"' for c in _cols(conn, "main", t))
            rk = ",".join(row)
            # Copies of the same rows from an earlier move (the item came back): the main rows replace them, and
            # rows that only the archive holds (a link to an item that stayed archived) stay.
            conn.execute(f"DELETE FROM archive.{t} WHERE ({rk}) IN (SELECT {rk} FROM main.{t} WHERE {cond})", params)
            conn.execute(f"INSERT INTO archive.{t} ({cols}) SELECT {cols} FROM main.{t} WHERE {cond}", params)
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        with tx(conn):
            ny, params = _cond(("item_id",), ids)
            conn.execute(f"DELETE FROM main.notifications WHERE event_id IN (SELECT id FROM main.needs_you WHERE {ny})",
                         params)
            for t, keys, _ in reversed(ARCHIVE_TABLES):  # the items last
                cond, params = _cond(keys, ids)
                conn.execute(f"DELETE FROM main.{t} WHERE {cond}", params)
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _archive_part(conn, dry_run, t):
    after = setting(conn, "archive_after")
    out = {"after": after, "items": 0, "rows": 0}
    if str(after).strip().lower() == "off":
        return out
    ids = archivable(conn, iso(t - parse_duration(after)))
    out["items"] = len(ids)
    if not ids:
        return out
    if not dry_run:
        attach(conn, create=True)
        with tx(conn):
            _sync_tables(conn)
    for i in range(0, len(ids), ARCHIVE_BATCH):
        part = ids[i:i + ARCHIVE_BATCH]
        for tname, keys, _ in ARCHIVE_TABLES[1:]:  # the rows that go with the items
            cond, params = _cond(keys, part)
            out["rows"] += conn.execute(f"SELECT COUNT(*) FROM main.{tname} WHERE {cond}", params).fetchone()[0]
        if not dry_run:
            _move(conn, part)
    return out


def is_archived(conn, iid):
    """The item is in the archive and not in the main database."""
    if not attach(conn):
        return False
    return bool(conn.execute("SELECT 1 FROM archive.items WHERE id=?", (int(iid),)).fetchone()) and \
        not conn.execute("SELECT 1 FROM main.items WHERE id=?", (int(iid),)).fetchone()


def _restore_rows(conn, ids):
    """Copy archived items back into the main database, with their rows, inside the caller's transaction. A link to
    an item that stays archived does not come (a dep, a close check), and a message's answer to a message that is
    not in the main database loses that link. The archive keeps its copies; the main database wins from now on."""
    conn.execute("PRAGMA defer_foreign_keys=ON")  # the items come in id order; one may be found during a later one
    ids = set(ids)
    todo = list(ids)
    while todo:  # the items they were found during, when those are archived too
        cond, params = _cond(("id",), todo)
        todo = [r[0] for r in conn.execute(
            f"SELECT found_during FROM archive.items WHERE ({cond}) AND found_during IS NOT NULL "
            f"AND found_during NOT IN (SELECT id FROM main.items)", params) if r[0] not in ids
            and conn.execute("SELECT 1 FROM archive.items WHERE id=?", (r[0],)).fetchone()]
        ids |= set(todo)
    for i in range(0, len(ids), 400):
        part = sorted(ids)[i:i + 400]
        for t, keys, row in ARCHIVE_TABLES:
            cond, params = _cond(keys, part, "a.")
            cols = _cols(conn, "main", t)
            have = set(_cols(conn, ARCHIVE, t))
            sel = ",".join(f'a."{c}"' if c in have else "NULL" for c in cols)
            extra = ""
            if t == "items":
                extra = " AND a.id NOT IN (SELECT id FROM main.items)"
            elif t in ("deps", "close_checks"):  # both of its items must be in the main database
                extra = "".join(f" AND a.{k} IN (SELECT id FROM main.items)" for k in keys)
            elif t == "item_goals":
                extra = " AND a.goal_id IN (SELECT id FROM main.goals)"
            conn.execute(f"INSERT OR IGNORE INTO main.{t} ({','.join(chr(34) + c + chr(34) for c in cols)}) "
                         f"SELECT {sel} FROM archive.{t} a WHERE ({cond}){extra}", params)
        marks = ",".join("?" * len(part))
        conn.execute(f"UPDATE main.messages SET reply_to=NULL WHERE item_id IN ({marks}) AND reply_to IS NOT NULL "
                     f"AND reply_to NOT IN (SELECT id FROM main.messages)", part)
        conn.execute(f"DELETE FROM main.needs_you WHERE item_id IN ({marks}) AND message_id IS NOT NULL "
                     f"AND message_id NOT IN (SELECT id FROM main.messages)", part)
    return ids


def restore(conn, iid):
    """Bring an archived item back into the main database (core._item calls it for an id it does not find). Returns
    True when the item is in the main database now."""
    if not is_archived(conn, iid):
        return False
    conn.execute("SAVEPOINT maxpm_restore")
    try:
        _restore_rows(conn, [int(iid)])
        core._event(conn, int(iid), "maxpm", "back from the archive: a command named it")
        conn.execute("RELEASE maxpm_restore")
    except BaseException:
        conn.execute("ROLLBACK TO maxpm_restore")
        conn.execute("RELEASE maxpm_restore")
        raise
    return True


def with_archive(conn, fn, ids=None):
    """fn() as if the archived items (ids, or all) were in the main database: they come back inside a transaction
    that is then rolled back, so the code that reads the main database reads them too, and nothing changes."""
    if not attach(conn):
        return fn()
    out = {}
    try:
        with tx(conn):
            if ids is None:
                ids = [r[0] for r in conn.execute("SELECT id FROM archive.items WHERE id NOT IN (SELECT id FROM main.items)")]
            if ids:
                _restore_rows(conn, ids)
            out["res"] = fn()
            raise _Undo
    except _Undo:
        pass
    return out["res"]


def archived_rows(conn, where="1", params=()):
    """Rows of archived items (not in the main database) with the columns of a search: for core.search."""
    if not attach(conn):
        return []
    return [dict(r, archived=True) for r in conn.execute(
        "SELECT i.id, i.project_id, p.name project, p.path, i.title, i.context, i.notes, i.output, i.status, i.kind, "
        "i.doer, i.assignee, i.touches, i.created_at, i.closed_at, i.backlog_at, i.found_during "
        "FROM archive.items i JOIN main.projects p ON p.id=i.project_id "
        f"WHERE i.id NOT IN (SELECT id FROM main.items) AND {where}", params)]


# ---------------------------------------------------------------- the run

def summary(res):
    """One line for the event and the note."""
    parts = []
    a = res.get("archive") or {}
    moved = (f"{'would archive' if res.get('dry_run') else 'archived'} {a['items']} items closed more than "
             f"{a['after']} ago, with {a['rows']} rows of their history; ") if a.get("items") else ""
    for side, key in (("db", "rows"), ("archive_db", "rows"), ("folder", "files")):
        for r in (res.get(side) or {}).get("removed", []):
            parts.append(f"{r[key]} {key} of {r['what']} ({show_size(r['bytes'])})")
    d, f = res["db"], res["folder"]
    verb = "would remove " if res.get("dry_run") else "removed "
    line = ("space cleanup: " + moved + (verb + ", ".join(parts) if parts else "nothing to remove")
            + f"; database {show_size(d['used'])} -> {show_size(d['used_after'])} (limit {show_size(d['limit'])})"
            + f", folder {show_size(f['size'])} -> {show_size(f['size_after'])} (limit {show_size(f['limit'])})")
    over = [n for n, x in (("database", d), ("archive", res.get("archive_db") or {}), ("folder", f)) if x.get("over")]
    if over:
        line += f"; still over the limit: {' and '.join(over)} (nothing older is removable: maxpm cleanup --dry-run)"
    return line


def run(conn, dry_run=False, actor=None):
    """Remove the oldest removable data until each size is under its limit (see the module text). dry_run: remove
    nothing, say what would go."""
    t = now()
    res = {"dry_run": dry_run, "archive": _archive_part(conn, dry_run, t), "db": _db_part(conn, dry_run, t)}
    if attach(conn):
        res["archive_db"] = _db_part(conn, dry_run, t, ARCHIVE)
    if not dry_run:  # compact what changed, then measure again
        if res["archive"]["items"] or res["db"]["removed"]:
            res["db"]["vacuum_error"] = _vacuum(conn)
            res["db"].update(used_after=db_used(conn), file_after=db_files_bytes(conn))
            res["db"]["over"] = bool(res["db"]["limit"] and res["db"]["used_after"] > res["db"]["limit"])
        ad = res.get("archive_db")
        if ad and ad["removed"]:
            ad["vacuum_error"] = _vacuum(conn, ARCHIVE)
            ad["used_after"] = db_used(conn, ARCHIVE)
            ad["over"] = bool(ad["limit"] and ad["used_after"] > ad["limit"])
    res["folder"] = _folder_part(conn, dry_run, t)
    res["summary"] = summary(res)
    if dry_run:
        return res
    m = _meta(conn)
    m.update(last_run=iso(t), folder=res["folder"]["size_after"], folder_at=iso(t),
             removed=bool(res["archive"]["items"] or res["db"]["removed"] or res["folder"]["removed"]
                          or (res.get("archive_db") or {}).get("removed")))
    over = res["db"].get("over") or res["folder"].get("over") or (res.get("archive_db") or {}).get("over")
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
