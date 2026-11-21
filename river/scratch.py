"""Build folders of sessions (#6398).

On 2026-11-20 the disk went from 23 GB free to 117 MB in 30 minutes: sessions of done items had built clean copies
with Cargo in their Claude Code scratchpads (/tmp/claude-<uid>/<project slug>/<session id>/scratchpad/, 2-11 GB
each, 32 GB in all), and the folders stayed after maxpm stopped the sessions. mark 2026-11-20: 'builds are not
allowed to be in the scratch folder. it needs to be in a location that will be auto cleaned'.

1. Each session that maxpm starts gets a build folder of its own, <session_builds>/<session name>/, made at the start
   and named by the variable MAXPM_BUILD_DIR (server._open_terminal). Its briefing says: a build outside the
   checkout's own target (a clean copy, an export, a test copy) goes to CARGO_TARGET_DIR=$MAXPM_BUILD_DIR/<name>.
2. Each pass of the loop of maxpm serve (serve_pass) removes the build folder of each session of this queue (an
   agents row or a gone_agents copy) that ended (no agents row, or stopped or gone and holding nothing:
   space._session_ended) and was not seen for GRACE. A folder younger than GRACE stays (a session that is starting).
3. In the same pass, a Cargo target that such a session left in its Claude Code scratchpad goes too: a folder that
   holds a CACHEDIR.TAG file (Cargo writes one at the top of each target folder) or a Cargo target layout
   (debug/deps or release/deps). Only scratchpads of session ids that MaximizePM recorded for the session
   (agent_sessions) are looked at, so a person's own Claude Code session is left alone. The walk follows no link,
   so the project's own target folder stays even when a link in a scratchpad points to it.

Each session whose folders went gets one line in the events, '<session> ended: removed ...', with the bytes as du
counts them (clones of files share blocks, so the disk may gain less). A scratchpad that was walked is remembered
with the change time of its folder (meta key META), so a pass walks it again only when its top folder changed. The
scratchpads are listed at most once in SCAN_EVERY.
"""

import json
import os
import shutil
from datetime import timedelta
from pathlib import Path

from . import core

META = "scratch"
GRACE = timedelta(minutes=2)  # a session seen this recently, or a build folder this new, stays
# A list of all scratchpads took 194 ms for 403 of them (2026-11-20); serve passes every 30 s, so that walk runs at
# most once in SCAN_EVERY. The build folders under session_builds go at each pass.
SCAN_EVERY = timedelta(minutes=5)
SKIP = {".git", "node_modules"}  # folders the walk does not enter: no build folder of Cargo is in them
ENV = "MAXPM_BUILD_DIR"


def build_root(conn=None):
    """The folder that holds the build folder of each session (setting session_builds)."""
    v = core.setting(conn, "session_builds") if conn is not None else core.DEFAULT_SETTINGS["session_builds"]
    return Path(os.path.expanduser(v or core.DEFAULT_SETTINGS["session_builds"]))


def build_dir(conn, name, make=False):
    """The build folder of session `name`; make: create it. None for a name that is no folder name."""
    if not name or name in (".", "..") or os.sep in name or (os.altsep and os.altsep in name):
        return None
    d = build_root(conn) / name
    if make:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
    return d


def _is_build(d):
    """d is a build folder: a CACHEDIR.TAG file in it, or a Cargo target layout."""
    return (os.path.isfile(os.path.join(d, "CACHEDIR.TAG"))
            or any(os.path.isdir(os.path.join(d, kind, "deps")) for kind in ("debug", "release")))


def build_dirs(scratchpad):
    """The build folders under a scratchpad, top-most only, with no link on the way."""
    out = []
    for root, dirs, _ in os.walk(scratchpad):
        keep = []
        for name in dirs:
            p = os.path.join(root, name)
            if name in SKIP or os.path.islink(p):
                continue
            if _is_build(p):
                out.append(Path(p))
            else:
                keep.append(name)
        dirs[:] = keep
    return out


def scratchpads():
    """{session id: [scratchpad folders]} under each folder of Claude Code's scratchpads."""
    from . import server
    out = {}
    for base in server.claude_tmps():
        try:
            slugs = [s for s in base.iterdir() if s.is_dir() and not s.is_symlink()]
        except OSError:
            continue
        for slug in slugs:
            try:
                sessions = list(slug.iterdir())
            except OSError:
                continue
            for s in sessions:
                pad = s / "scratchpad"
                if not s.is_symlink() and pad.is_dir() and not pad.is_symlink():
                    out.setdefault(s.name, []).append(pad)
    return out


def _owners(conn, sids):
    """{session id: [agents, newest first]} from agent_sessions."""
    out = {}
    sids = list(sids)
    for i in range(0, len(sids), 400):
        part = sids[i:i + 400]
        for r in conn.execute(f"SELECT session_id, agent FROM agent_sessions WHERE session_id IN "
                              f"({','.join('?' * len(part))}) ORDER BY first_seen DESC, rowid DESC", part):
            out.setdefault(r[0], []).append(r[1])
    return out


def _ended(conn, name, t):
    """The session ended (space._session_ended) and was not seen in GRACE."""
    from .space import _session_ended
    r = conn.execute("SELECT last_seen FROM agents WHERE name=?", (name,)).fetchone()
    if r and r["last_seen"] and t - core.parse_iso(r["last_seen"]) < GRACE:
        return False
    return _session_ended(conn, name)


def _known(conn, name):
    """This queue knows the session (an agents row, or a gone_agents copy): a folder of a session of another queue
    (MAXPM_DB, the tests) stays."""
    return bool(conn.execute("SELECT 1 FROM agents WHERE name=? UNION ALL SELECT 1 FROM gone_agents WHERE name=? "
                             "LIMIT 1", (name, name)).fetchone())


def _mtime(p):
    try:
        return os.stat(p).st_mtime_ns
    except OSError:
        return None


def _load(conn):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (META,)).fetchone()
    try:
        return json.loads(r["value"]) if r else {}
    except ValueError:
        return {}


def _save(conn, m):
    with core.tx(conn):
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (META, json.dumps(m)))


def sweep_item_targets(conn, dry_run=False):
    """Remove the build folders of closed items (#6635): <project folder>/target/<item id> of an item that is done or
    dropped, in every project folder. On 2026-11-21 the disk reached 0.3 GB free with 14 builds of finished items.
    Only a folder named by an item id of this queue goes, and the walk follows no link. Returns [(path, bytes)]."""
    from .space import tree_bytes
    out = []
    for r in conn.execute("SELECT path FROM projects WHERE path IS NOT NULL AND path != ''"):
        base = Path(os.path.expanduser(r["path"])) / "target"
        try:
            kids = [d for d in base.iterdir() if d.name.isdigit() and d.is_dir() and not d.is_symlink()]
        except OSError:
            continue
        for d in kids:
            it = conn.execute("SELECT status FROM items WHERE id=?", (int(d.name),)).fetchone()
            if it is None or it["status"] not in core.CLOSED_STATES:
                continue
            out.append((str(d), tree_bytes(d)))
            if not dry_run:
                shutil.rmtree(d, ignore_errors=True)
    return out


def _inside(p, root):
    return str(p.resolve()).startswith(str(root.resolve()) + os.sep)


def sweep(conn, dry_run=False):
    """Remove the build folders of ended sessions and the Cargo targets in their scratchpads (see the module text).
    Returns {session: {"build": bytes or None, "folders": n, "bytes": b, "paths": [...]}} of what went (dry_run:
    what would go)."""
    from .space import tree_bytes
    t = core.now()
    done = {}

    def took(name):
        return done.setdefault(name, {"build": None, "folders": 0, "bytes": 0, "paths": []})

    root = build_root(conn)
    try:
        builds = [d for d in root.iterdir() if d.is_dir() and not d.is_symlink()] if root.is_dir() else []
    except OSError:
        builds = []
    for d in builds:
        m = _mtime(d)
        if m is None or t.timestamp() - m / 1e9 < GRACE.total_seconds() or not _known(conn, d.name) \
                or not _ended(conn, d.name, t):
            continue
        took(d.name)["build"] = tree_bytes(d)
        took(d.name)["paths"].append(str(d))
        if not dry_run:
            shutil.rmtree(d, ignore_errors=True)

    state = _load(conn)
    at = state.get("at")
    if not dry_run and at and t - core.parse_iso(at) < SCAN_EVERY:
        return _report(conn, done)
    pads = scratchpads()
    seen = state.get("pads") or {}
    live = {str(p) for ps in pads.values() for p in ps}
    memo = {k: v for k, v in seen.items() if k in live}  # a scratchpad that is gone leaves the memo
    todo = {sid: ps for sid, ps in pads.items() if any(memo.get(str(p)) != _mtime(p) for p in ps)}
    owners = _owners(conn, todo) if todo else {}
    for sid, ps in todo.items():
        names = owners.get(sid)
        if not names or not all(_ended(conn, n, t) for n in names):
            continue
        for pad in ps:
            if memo.get(str(pad)) == _mtime(pad):
                continue
            for d in build_dirs(pad):
                if not _inside(d, pad):  # never a folder outside this scratchpad
                    continue
                g = took(names[0])
                g["folders"] += 1
                g["bytes"] += tree_bytes(d)
                g["paths"].append(str(d))
                if not dry_run:
                    shutil.rmtree(d, ignore_errors=True)
            if not dry_run:
                memo[str(pad)] = _mtime(pad)
    if dry_run:
        return done
    _save(conn, {"at": core.iso(t), "pads": memo})
    return _report(conn, done)


def _report(conn, done):
    """One event line and one serve log line for each session whose folders went."""
    for name, g in done.items():
        line = stop_line(name, g)
        with core.tx(conn):
            core._event(conn, None, "maxpm", line)
        print(f"builds: {line}", flush=True)
    return done


def stop_line(name, g):
    """'<session> ended: removed its build folder (2.1 GB) and 3 build folders in its scratchpad (11 GB)'."""
    from .space import show_size
    parts = []
    if g["build"] is not None:
        parts.append(f"its build folder ({show_size(g['build'])})")
    if g["folders"]:
        n = g["folders"]
        parts.append(f"{n} build folder{'s' if n != 1 else ''} in its scratchpad ({show_size(g['bytes'])})")
    return f"{name} ended: removed " + " and ".join(parts)


def serve_pass(conn):
    """One pass of the loop of maxpm serve: sweep."""
    return sweep(conn)
