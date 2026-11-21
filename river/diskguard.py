"""Free space on the disk of the queue (#6506) and temp-folder growth guard (#6509).

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
4. Temp-folder growth limit (#6509): once per TMP_EVERY, check_tmp measures each entry in the shared session temp
   folder (/tmp/claude-<uid>; inside a Claude Code project slug directory, each session folder is measured on its own
   so unrelated sessions do not add together). When one entry grows by tmp_max_size (2 GB) above its baseline,
   maxpm serve finds the writing process with lsof, maps it to a live session on this host, ends that command's
   process tree via core.kill_session_command (the same way #6497 ends a hanging command), and tells the session and
   the manager why.

Each level is a size (30GB, 2GB) or off."""

import os
import shutil
import signal
import subprocess
import sys
import threading
from datetime import timedelta

from . import core

META = "disk"
SWEEP_EVERY = timedelta(hours=1)  # a sweep that freed too little does not run again at each pass
SWEEP_TIMEOUT = timedelta(minutes=30)  # a sweep command still running then is stopped with every process it started
TMP_EVERY = timedelta(minutes=1)  # walking 10 GB took 1.7 s; run at most once a minute in serve_pass
LSOF_TIMEOUT = 15  # seconds before giving up on lsof on a large temp directory
RUNNING = threading.Lock()  # one sweep at a time in this process
_TMP_LAST = {}  # db path -> last check_tmp timestamp (in-process throttle)
# Tests set RUNNER to (cmd) -> (exit status, output) and LSOF to (path) -> lsof -F pfa stdout.
RUNNER = None
LSOF = None


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
    gone = scratch.sweep_item_targets(conn)
    if gone:
        print(f"disk: removed {len(gone)} build folders of closed items ({show_size(sum(b for _, b in gone))})",
              flush=True)
    before = free_bytes(conn) if gone else before
    if not cmd:
        after = free_bytes(conn)
        body = (f"Disk: {show_size(after)} free on the disk of the queue, below disk_sweep_free. maxpm serve removed "
                f"the build folders of ended sessions and of closed items. To free more (caches, other build output), "
                f"set a command: maxpm config set disk_sweep_cmd '<command>'")
    else:
        rc, out = _shell(cmd)
        after = free_bytes(conn)
        how = "ran" if rc == 0 else ("was stopped" if rc is None else f"exited {rc}")
        body = (f"Disk: the sweep {how}: {show_size(before)} free before, {show_size(after)} after. "
                f"disk_sweep_cmd: {_last_line(out)}")
    _tell(conn, f"{body}. Temp: {tmp_line()}")
    return body


def _is_project_slug(p):
    """True for a Claude Code project-slug directory (/tmp/claude-<uid>/-Users-...) holding session folders."""
    if not (p.name.startswith("-") and p.is_dir() and not p.is_symlink()):
        return False
    try:
        for child in p.iterdir():
            if child.is_dir() and not child.is_symlink():
                if (child / "scratchpad").is_dir() or (child / "tasks").is_dir():
                    return True
    except OSError:
        return False
    return False


def _tmp_entries():
    """List (base, entry_path, label, size_bytes) for entries in the shared session temp folder(s).
    Inside a Claude Code project-slug folder, each session folder is one entry so normal sessions do not sum."""
    from . import server
    from .space import _file_bytes, tree_bytes
    out = []
    for base in server.claude_tmps():
        try:
            kids = list(base.iterdir())
        except OSError:
            continue
        for p in kids:
            if _is_project_slug(p):
                try:
                    sub = list(p.iterdir())
                except OSError:
                    continue
                for c in sub:
                    sz = tree_bytes(c) if c.is_dir() and not c.is_symlink() else _file_bytes(c)
                    out.append((base, c, f"{p.name}/{c.name}", sz))
            else:
                sz = tree_bytes(p) if p.is_dir() and not p.is_symlink() else _file_bytes(p)
                out.append((base, p, p.name, sz))
    return out


def _parse_lsof(text):
    """Parse 'lsof -w -n -P -F pfa' output into (writer_pids, other_pids), each unique and in order.
    writer_pids have write ('w') or read-write ('u') access; other_pids have other open handles (e.g. read, cwd)."""
    writers, others = [], []
    seen_w, seen_o = set(), set()
    cur_pid = None
    cur_has_mode = False

    def flush():
        if cur_pid is not None and not cur_has_mode and cur_pid not in seen_o:
            seen_o.add(cur_pid)
            others.append(cur_pid)

    for line in (text or "").splitlines():
        if not line:
            continue
        tag, val = line[0], line[1:]
        if tag == "p":
            flush()
            try:
                cur_pid = int(val)
            except ValueError:
                cur_pid = None
            cur_has_mode = False
        elif tag == "a" and cur_pid is not None:
            cur_has_mode = True
            mode = val.strip().lower()
            if "w" in mode or "u" in mode:
                if cur_pid not in seen_w:
                    seen_w.add(cur_pid)
                    writers.append(cur_pid)
            elif cur_pid not in seen_o:
                seen_o.add(cur_pid)
                others.append(cur_pid)
    flush()
    return writers, [p for p in others if p not in seen_w]


def _lsof_pids(path):
    """Find PIDs with open handles on path (file or directory). Returns (writer_pids, other_pids).
    Ignores lsof's non-zero exit code when stdout has data (macOS lsof exits 1 on unrelated unstatable mounts)."""
    if LSOF is not None:
        return _parse_lsof(LSOF(path))
    args = (["lsof", "-w", "-n", "-P", "-F", "pfa", "+D", str(path)] if path.is_dir()
            else ["lsof", "-w", "-n", "-P", "-F", "pfa", "--", str(path)])
    try:
        r = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=LSOF_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return [], []
    return _parse_lsof(r.stdout or "")


def _session_pids(conn):
    """{agent: root_pid} for live AI sessions on this host (same discovery as server.watch_commands)."""
    from . import server
    host = core.this_host()
    rows = conn.execute("SELECT name, pid, host FROM agents WHERE kind='ai' AND stop_at IS NULL "
                        "AND (pid IS NULL OR host=?) ORDER BY name", (host,)).fetchall()
    terminals = server.agent_terminals(conn) if server.TMUX_RUNNER is not None or server._tmux_cmd() else {}
    pids = {}
    for r in rows:
        if r["pid"] and r["host"] == host:
            pids[r["name"]] = r["pid"]
        elif r["name"] in terminals:
            p = server.pane_process(conn, r["name"])
            if p and p.get("pid"):
                pids[r["name"]] = p["pid"]
    return pids


def _map_to_session(candidate_pids, session_pids, ps_text, server_grace=None):
    """Map candidate PIDs to [(agent, command_root_pid), ...] under live sessions on this host.
    Walks up ppid to find the owning session root; prefers the child command root from core.session_child_commands
    so the whole command tree (e.g. a shell loop and its workers) is ended. Never targets a session's root PID."""
    if not candidate_pids or not session_pids or not ps_text:
        return []
    children, info = core._parse_ps_tree(ps_text)
    by_root = {root_pid: agent for agent, root_pid in session_pids.items()}
    cmds_by_agent = core.session_child_commands(ps_text, session_pids, server_grace=server_grace)
    cmd_desc = {}
    for agent, cmds in cmds_by_agent.items():
        cmd_desc[agent] = [(c["pid"], {c["pid"]} | core.descendant_pids(children, c["pid"])) for c in cmds]
    targets, seen = [], set()
    for wpid in candidate_pids:
        if wpid not in info:
            continue
        cur, visited = wpid, set()
        owner, root_pid = None, None
        while cur in info and cur not in visited:
            visited.add(cur)
            if cur in by_root:
                owner, root_pid = by_root[cur], cur
                break
            cur = info[cur]["ppid"]
        if owner is None or wpid == root_pid:
            continue
        target_pid = wpid
        for cpid, members in cmd_desc.get(owner, ()):
            if wpid in members:
                target_pid = cpid
                break
        key = (owner, target_pid)
        if key not in seen:
            seen.add(key)
            targets.append(key)
    return targets


def check_tmp(conn, grace=5.0, sleep=None, server_grace=None):
    """Check each entry in the shared session temp folder against tmp_max_size (#6509).
    The limit applies to the growth of one entry above its baseline, not to the total folder size.
    When an entry grows past tmp_max_size, find the writing process (lsof), map it to a live session on this host,
    end that command's process tree, and tell the session and the manager why. Returns a list of stopped commands."""
    from .space import parse_size, show_size
    limit = parse_size(core.setting(conn, "tmp_max_size"))
    if limit is None:
        return []
    entries = _tmp_entries()
    m = _load(conn)
    if "tmp_base" not in m:
        m["tmp_base"] = {str(p): sz for _, p, _, sz in entries}
        _save(conn, m)
        return []
    base_map = dict(m["tmp_base"])
    changed = False
    cur_keys = {str(p) for _, p, _, _ in entries}
    for k in [k for k in base_map if k not in cur_keys]:
        del base_map[k]
        changed = True
    over = []
    for base, path, label, sz in entries:
        key = str(path)
        prev = base_map.get(key, 0)
        if sz < prev:
            base_map[key] = sz
            prev = sz
            changed = True
        growth = sz - prev
        if growth >= limit:
            over.append((base, path, label, sz, growth))
    if not over:
        if changed:
            m["tmp_base"] = base_map
            _save(conn, m)
        return []
    pids = _session_pids(conn)
    ps_text = core._shell_list() if pids else ""
    stopped = []
    for base, path, label, sz, growth in over:
        writers, others = _lsof_pids(path)
        targets = _map_to_session(writers, pids, ps_text, server_grace=server_grace)
        if not targets and others:
            targets = _map_to_session(others, pids, ps_text, server_grace=server_grace)
        killed_any = False
        for agent, target_pid in targets:
            why = (f"temp entry {label} grew by {show_size(growth)} (to {show_size(sz)}), "
                   f"past tmp_max_size ({show_size(limit)})")
            try:
                res = core.kill_session_command(conn, agent, pid=target_pid, reason=why, grace=grace, sleep=sleep,
                                                by_serve=True, pane_pid=pids.get(agent), server_grace=server_grace)
            except core.RiverError as e:
                print(f"disk: could not end command in {agent} for {label}: {e}", file=sys.stderr, flush=True)
                continue
            killed_any = True
            held = conn.execute("SELECT id FROM items WHERE assignee=? AND status IN ('in_progress','held') "
                                "ORDER BY claimed_at DESC LIMIT 1", (agent,)).fetchone()
            held_id = held["id"] if held else None
            manager = core.active_manager(conn)
            to_mgr = [manager] if manager and manager != agent else [
                r["name"] for r in conn.execute("SELECT name FROM agents WHERE kind='human' ORDER BY name")]
            with core.tx(conn):
                core._send(conn, "note", "maxpm",
                           f"maxpm serve ended your command (PID {res['pid']}: {res['command']}) because temp entry "
                           f"{label} in {base} grew by {show_size(growth)} (to {show_size(sz)}), past tmp_max_size "
                           f"({show_size(limit)}). Clean up temp files as you go or write less to TMPDIR.",
                           to=agent, item_id=held_id)
                for p in to_mgr:
                    core._send(conn, "alert", "maxpm",
                               f"maxpm serve ended a command in {agent} (PID {res['pid']}: {res['command']}): "
                               f"temp entry {label} in {base} grew by {show_size(growth)} (to {show_size(sz)}), "
                               f"past tmp_max_size ({show_size(limit)}).",
                               to=p, item_id=held_id)
                core.sync_needs_you(conn)
            stopped.append({"agent": agent, "pid": res["pid"], "command": res["command"], "entry": str(path),
                            "label": label, "growth": growth, "size": sz})
        if not killed_any:
            found = writers or others
            extra = (f" (open by PID {', '.join(str(p) for p in found[:5])}, not under a live session)"
                     if found else "")
            _tell(conn, f"Disk: temp entry {label} in {base} grew by {show_size(growth)} (to {show_size(sz)}), "
                        f"past tmp_max_size ({show_size(limit)}), but no live session command was found writing "
                        f"to it{extra}.")
        base_map[str(path)] = sz
        changed = True
    if changed:
        m["tmp_base"] = base_map
        _save(conn, m)
    return stopped


def serve_pass(conn, wait=False):
    """One pass of the loop of maxpm serve. wait: run the sweep in this thread (tests, maxpm cleanup)."""
    from .space import parse_size, show_size
    t = core.now()
    db_key = str(_db(conn))
    last_tmp = _TMP_LAST.get(db_key)
    if wait or last_tmp is None or t - last_tmp >= TMP_EVERY:
        _TMP_LAST[db_key] = t
        check_tmp(conn)
    warn = parse_size(core.setting(conn, "disk_warn_free"))
    low = parse_size(core.setting(conn, "disk_sweep_free"))
    if warn is None and low is None:
        return None
    free = free_bytes(conn)
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
