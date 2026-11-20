"""Free space on the disk of the queue (#6506).

On 2026-11-20 the disk of mark's Mac had 143 MB free, and writes of SQLite and git failed for every session: one
day of Cargo builds had filled the target folder of one project with 62 GB. maxpm serve now looks at the free space
of the volume that holds the queue at each pass of its loop (one statvfs call):

1. Below disk_warn_free (30 GB): one alert to the active manager (with no manager: to the person), once each time
   the free space falls below the level. The alert comes again only after the free space was above the level.
2. Below disk_sweep_free (20 GB): the sweep, at most once in SWEEP_EVERY. It removes the build folders of ended
   sessions at once (scratch.sweep, which also runs at each pass), then runs disk_sweep_cmd in a thread: a command of
   this computer (a shell line) that removes the build output and the caches that this computer can build again.
   MaximizePM knows no project's build folders, so what goes is the person's choice, in that command. After the
   command, the same recipient gets one alert with the free space before and after and the last line of the output.
   With no disk_sweep_cmd the alert says so and names the setting.
3. Both alerts give the size of the temp folder that the sandboxed sessions share, and its largest entries
   (tmp_line): on 2026-11-20 a test script also filled it.

Each level is a size (30GB) or off."""

import os
import shutil
import signal
import subprocess
import threading
from datetime import timedelta

from . import core

META = "disk"
SWEEP_EVERY = timedelta(hours=1)  # a sweep that freed too little does not run again at each pass
SWEEP_TIMEOUT = timedelta(minutes=30)  # a sweep command still running then is stopped with every process it started
RUNNING = threading.Lock()  # one sweep at a time in this process
# Tests set this to a function (cmd) -> (exit status, output) that replaces the shell.
RUNNER = None


def free_bytes(conn):
    """The bytes free on the volume that holds the queue, as df shows them (blocks free to a user)."""
    from .space import db_file
    return shutil.disk_usage(db_file(conn).parent).free


def tmp_line(top=3):
    """The size of the temp folder of the sandboxed sessions (TMPDIR /tmp/claude-<uid>, which they all share) and
    its largest entries: on 2026-11-20 a test script filled it with test queues until 1.1 GB of the disk was free.
    A walk of 10 GB took 1.7 s, so only an alert runs it."""
    from . import server
    from .space import _file_bytes, show_size, tree_bytes
    parts = []
    for base in server.claude_tmps():
        try:
            kids = list(base.iterdir())
        except OSError:
            continue
        sizes = sorted(((tree_bytes(p) if p.is_dir() and not p.is_symlink() else _file_bytes(p)), p.name)
                       for p in kids)
        if sizes:
            big = ", ".join(f"{n} {show_size(s)}" for s, n in reversed(sizes[-top:]))
            parts.append(f"{base} holds {show_size(sum(s for s, _ in sizes))} (largest: {big})")
    return "; ".join(parts) or "no session temp folder"


def _recipient(conn):
    mgr = core.active_manager(conn)
    if mgr:
        return mgr
    r = conn.execute("SELECT name FROM agents WHERE kind='human' ORDER BY last_seen DESC").fetchone()
    return r["name"] if r else None


def _tell(conn, body):
    """One alert to the manager (or the person) and one event."""
    to = _recipient(conn)
    with core.tx(conn):
        core._event(conn, None, "maxpm", body)
        if to:
            core._send(conn, "alert", "maxpm", body, to=to)
    print(f"disk: {body}", flush=True)
    return to


def _load(conn):
    import json
    r = conn.execute("SELECT value FROM meta WHERE key=?", (META,)).fetchone()
    try:
        return json.loads(r["value"]) if r else {}
    except ValueError:
        return {}


def _save(conn, m):
    import json
    with core.tx(conn):
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (META, json.dumps(m)))


def _shell(cmd):
    """Run the sweep command: (exit status, output). Past SWEEP_TIMEOUT its process group is stopped."""
    if RUNNER is not None:
        return RUNNER(cmd)
    p = subprocess.Popen(["/bin/sh", "-c", cmd], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, cwd=os.path.expanduser("~"), start_new_session=True)
    try:
        out, _ = p.communicate(timeout=SWEEP_TIMEOUT.total_seconds())
        return p.returncode, out or ""
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        out, _ = p.communicate()
        return None, (out or "") + f"\nstopped after {SWEEP_TIMEOUT}"


def _last_line(out):
    lines = [x.strip() for x in (out or "").splitlines() if x.strip()]
    return lines[-1][:300] if lines else "no output"


def sweep(conn, before, cmd):
    """The sweep (see the module text), in the caller's thread. Returns the text of the alert it sent."""
    from . import scratch
    from .space import show_size
    scratch.sweep(conn)
    if not cmd:
        after = free_bytes(conn)
        body = (f"Disk: {show_size(after)} free on the disk of the queue, below disk_sweep_free. maxpm serve removed "
                f"the build folders of ended sessions; nothing else is set to go. Free build output and caches, and "
                f"set the command that does it: maxpm config set disk_sweep_cmd '<command>'")
    else:
        rc, out = _shell(cmd)
        after = free_bytes(conn)
        how = "ran" if rc == 0 else ("was stopped" if rc is None else f"exited {rc}")
        body = (f"Disk: the sweep {how}: {show_size(before)} free before, {show_size(after)} after. "
                f"disk_sweep_cmd: {_last_line(out)}")
    _tell(conn, f"{body}. Temp: {tmp_line()}")
    return body


def serve_pass(conn, wait=False):
    """One pass of the loop of maxpm serve. wait: run the sweep in this thread (tests, maxpm cleanup)."""
    from .space import parse_size, show_size
    warn = parse_size(core.setting(conn, "disk_warn_free"))
    low = parse_size(core.setting(conn, "disk_sweep_free"))
    if warn is None and low is None:
        return None
    free = free_bytes(conn)
    t = core.now()
    m = _load(conn)
    changed = False
    if warn is not None:
        if free >= warn and m.get("warned"):
            m.pop("warned")
            changed = True
        elif free < warn and not m.get("warned"):
            m["warned"] = core.iso(t)
            changed = True
            sweep_at = f" At {show_size(low)} maxpm serve runs the sweep (disk_sweep_cmd)." if low is not None else ""
            _tell(conn, f"Disk: {show_size(free)} free on the disk of the queue, below disk_warn_free "
                        f"({show_size(warn)}). Builds and caches fill it: free what can be built again.{sweep_at} "
                        f"Temp: {tmp_line()}")
    due = low is not None and free < low and (
        not m.get("swept") or t - core.parse_iso(m["swept"]) >= SWEEP_EVERY)
    if due and RUNNING.acquire(blocking=False):
        m["swept"] = core.iso(t)
        _save(conn, m)
        cmd = (core.setting(conn, "disk_sweep_cmd") or "").strip()
        if wait:
            try:
                return sweep(conn, free, cmd)
            finally:
                RUNNING.release()

        def run():
            c = None
            try:
                c = core.connect(str(_db(conn)))
                sweep(c, free, cmd)
            except Exception as e:
                print(f"disk: the sweep failed: {type(e).__name__}: {e}", flush=True)
            finally:
                if c is not None:
                    c.close()
                RUNNING.release()

        threading.Thread(target=run, name="disk-sweep", daemon=True).start()
        return "started"
    if changed:
        _save(conn, m)
    return None


def _db(conn):
    from .space import db_file
    return db_file(conn)
