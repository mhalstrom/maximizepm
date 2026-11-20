"""Keep each agent CLI up to date (#6430, mark 2026-11-20: 'we need to have an update process for our cli agents').

Codex CLI 0.159.1 refused the model gpt-6.2-sol until the manager updated it by hand to 0.171.0. Now maxpm serve reads,
when it starts and then once a day, the installed version and the newest version of each agent CLI that is installed
here, and keeps them in agent-versions.json beside the queue (check). The page reads that file with no network call
(status): a newer version opens the dialog '<Agent> is out of date' (mark 2026-11-20: 'this should show a dialog that
says, X agent is out of date. update now button'), and Settings > Agents lists every agent with its versions.

Where the program is says how it was installed, and so how to read the newest version and how to update it
(install_of): an npm package (the npm registry; npm install -g <package>@<version>), a Homebrew formula or cask
(formulae.brew.sh; brew upgrade), a uv or pipx tool (PyPI; uv tool upgrade), else the CLI's own updater, from the
UPDATE part of its adapter (claude install <version>, grok update --version <version>).

Update now (request) queues the update; serve runs it in a pass of its loop (serve_pass), only while no process of
that CLI runs on this computer: the update of a running CLI can break it. Until the update ends, new starts of that
agent wait (hold, read by core.pick_provider). After the update a smoke test (UPDATE['smoke']: one short request that
is not interactive, on the agent's default model) must answer OK. When it does not, the old version goes back (where
the install method can install a given version). Every failed update adds a task that investigates it (_investigate),
and the dialog's row links it. An update that asks for a password,
sudo or a sign-in becomes a person's item with the command: MaximizePM types no password. The setting
agent_auto_update names the agents that serve updates with no click; none by default (mark 2026-11-20).
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import core
from .core import RiverError

FILE = "agent-versions.json"  # beside the queue
LOCK_FILE = "agent-versions.lock"  # the page's process and the loop's process both write the file
EVERY = 24 * 3600  # a check a day
AFTER_ERROR = 3600  # a check with an error tries again after an hour
LATER = 24 * 3600  # Later hides the dialog of one version for a day
VERSION_TIMEOUT = 20
UPDATE_TIMEOUT = 900
SMOKE_TIMEOUT = 180
ACTIVE = ("queued", "updating", "testing")  # an update in these states holds new starts of the agent
# How long a state may last before a task investigates it (#6461, mark 2026-11-20: 'this also should have a timer so
# if its waiting for the test we open a task to investigate'). An update command takes 10 s to 2 min (npm, brew) and
# stops at UPDATE_TIMEOUT; a smoke test takes 5 to 30 s and stops at SMOKE_TIMEOUT: past those, the thread hangs. A
# wait for sessions counts only while none of MaximizePM's sessions of the agent works (busy_now): a session's work
# can take hours, and maxpm serve ends an idle one after idle_end (15m), so 30 minutes idle means that something else
# holds the update (a person's own session, a server that is no session).
LIMIT = {"updating": UPDATE_TIMEOUT + 120, "testing": SMOKE_TIMEOUT + 120, "queued": 30 * 60}
OK_ANSWER = re.compile(r"\bOK\b", re.I)
# The output of an update that waits for a person: a password, sudo, a sign-in, a folder that only another user writes.
NEEDS_PERSON = re.compile(r"(?i)\bsudo\b|password|\bEACCES\b|permission denied|not writable|operation not permitted|"
                          r"\blog ?in\b|\bsign ?in\b|authenticat")

FETCH = None  # tests: fn(url, timeout) -> bytes, in place of the network
RUN = None  # tests: fn(argv, timeout, cwd) -> (exit code, output), in place of a process
PS = None  # tests: fn() -> [the args of each process], in place of ps
WHICH = None  # tests: fn(exe) -> its path or None
KILL = None  # tests: fn(pid, signal), in place of os.kill
SYNC = False  # tests: the check and the update run in the caller's thread
_lock = threading.RLock()
_threads = {}  # "check" or an adapter id -> the thread of this process that runs it
_started = {"checked": False}  # the first pass of this process checks at once (serve started)


# ---------------------------------------------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------------------------------------------

def read(conn):
    """What the last check and the updates wrote: {"checked_at", "error", "agents": {id: {...}}}; empty before the
    first check."""
    p = core._beside_queue(conn, FILE)
    try:
        d = json.loads(p.read_text(encoding="utf-8")) if p else {}
    except (OSError, ValueError):
        d = {}
    if not isinstance(d.get("agents"), dict):
        d["agents"] = {}
    return d


def change(conn, fn):
    """Read the file, let fn change the dict, and write it back, under a lock that holds across the processes of serve.
    Returns fn's result."""
    p = core._beside_queue(conn, FILE)
    with _lock:
        lockf = None
        try:
            if p is not None:
                import fcntl
                lockf = open(p.with_name(LOCK_FILE), "a")
                fcntl.flock(lockf, fcntl.LOCK_EX)
            d = read(conn)
            out = fn(d)
            if p is not None:
                tmp = p.with_name(p.name + ".tmp")
                tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
                os.replace(tmp, p)
            return out
        finally:
            if lockf is not None:
                lockf.close()


def _db(conn):
    return conn.execute("PRAGMA database_list").fetchone()[2]


# ---------------------------------------------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------------------------------------------

_VER = re.compile(r"\d+(?:\.\d+)+(?:-[0-9A-Za-z][0-9A-Za-z.]*)?")


def parse_version(text):
    """The first version in a text: 'codex-cli 0.171.0' -> '0.171.0'; None when it has none."""
    m = _VER.search(text or "")
    return m.group(0) if m else None


def _nums(v):
    m = re.match(r"\d+(?:\.\d+)*", str(v or "").lstrip("v"))
    return [int(x) for x in m.group(0).split(".")] if m else []


def newer(a, b):
    """True when version a is newer than version b, by the numbers before any -suffix (2.1 is 2.1.0). A missing
    version is never newer."""
    na, nb = _nums(a), _nums(b)
    if not na or not nb:
        return False
    n = max(len(na), len(nb))
    return na + [0] * (n - len(na)) > nb + [0] * (n - len(nb))


def _which(exe):
    if WHICH is not None:
        return WHICH(exe)
    from . import agentinstall
    return shutil.which(exe, path=agentinstall.extra_path()) if exe else None


def _env():
    """The environment of an update and a smoke test: the panes' (no MAXPM_*, CLAUDE*, CODEX_* of this process, the
    installers' folders on PATH), and NONINTERACTIVE for Homebrew."""
    from . import server
    return {**server._tmux_env(), "NONINTERACTIVE": "1", "HOMEBREW_NO_ANALYTICS": "1", "MAXPM_QUIET": "1"}


def _run(argv, timeout, cwd=None):
    """(exit code, output with stderr) of argv, with no input: a command that asks for a password fails instead of
    waiting. After timeout seconds its whole process group ends; the code is then 124."""
    if RUN is not None:
        return RUN(list(argv), timeout, cwd)
    try:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, errors="replace", cwd=cwd, env=_env(), start_new_session=True)
    except OSError as e:
        return 127, f"{type(e).__name__}: {e}"
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out or ""
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, 9)
        except OSError:
            pass
        out, _ = p.communicate()
        return 124, (out or "") + f"\n(stopped after {timeout} s)"


def _get(url, timeout=20):
    if FETCH is not None:
        return FETCH(url, timeout)
    import urllib.request
    from . import __version__
    req = urllib.request.Request(url, headers={"User-Agent": f"MaximizePM/{__version__}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _registry(kind, name):
    """The newest version of a package in its registry: npm, pypi, brew (a formula) or cask."""
    from urllib.parse import quote
    if kind == "npm":
        return json.loads(_get(f"https://registry.npmjs.org/{quote(name, safe='@')}/latest"))["version"]
    if kind == "pypi":
        return json.loads(_get(f"https://pypi.org/pypi/{quote(name)}/json"))["info"]["version"]
    if kind == "brew":
        return json.loads(_get(f"https://formulae.brew.sh/api/formula/{quote(name)}.json"))["versions"]["stable"]
    if kind == "cask":
        return str(json.loads(_get(f"https://formulae.brew.sh/api/cask/{quote(name)}.json"))["version"]).split(",")[0]
    raise ValueError(kind)


def _brew_info(how, inst):
    """The newest version that this Homebrew knows of a formula or a cask (its taps as of its last brew update)."""
    brew = os.path.join(inst["prefix"], "bin", "brew")
    code, out = _run([brew, "info", "--json=v2", "--cask" if how == "cask" else "--formula", inst["package"]], 60)
    if code != 0:
        raise RuntimeError(f"brew info {inst['package']}: exit {code}: {_tail(out, 1)}")
    d = json.loads(out)
    if how == "cask":
        return str(d["casks"][0]["version"]).split(",")[0]
    return d["formulae"][0]["versions"]["stable"]


def install_of(ad):
    """How the program of adapter ad is installed here, from where its file is: {"path", "how" (npm, brew, cask, uv,
    pipx, self, or None: MaximizePM cannot update it), "package", "prefix", "version" (read from its folder, or
    None)}. None when the program is not installed."""
    path = _which(ad.describe["exe"])
    if not path:
        return None
    real = os.path.realpath(path)
    out = {"path": path, "how": None, "package": None, "prefix": None, "version": None}
    m = re.search(r"^(.*)/lib/node_modules/((?:@[^/]+/)?[^/]+)/", real)
    if m:
        out.update(how="npm", prefix=m.group(1), package=m.group(2))
        try:
            out["version"] = json.loads(Path(m.group(0), "package.json").read_text(encoding="utf-8"))["version"]
        except (OSError, ValueError, KeyError):
            pass
        return out
    m = re.search(r"^(.*)/(Cellar|Caskroom)/([^/]+)/([^/]+)/", real)
    if m:
        cask = m.group(2) == "Caskroom"
        out.update(how="cask" if cask else "brew", prefix=m.group(1), package=m.group(3),
                   version=m.group(4).split(",")[0] if cask else re.sub(r"_\d+$", "", m.group(4)))
        return out
    m = re.search(r"/(uv/tools|pipx/venvs)/([^/]+)/", real)
    if m:
        out.update(how="uv" if m.group(1).startswith("uv") else "pipx", package=m.group(2))
        return out
    if (ad.update or {}).get("self"):
        out["how"] = "self"
    return out


def installed_version(ad, inst):
    """The version of the installed program: from its version command, else from its folder. The command comes first:
    Copilot's npm package downloads its newer versions itself, so its package.json said 1.0.109 while 1.0.114 ran."""
    if not inst:
        return None
    code, out = _run([inst["path"], *((ad.update or {}).get("version") or ["--version"])], VERSION_TIMEOUT)
    return (parse_version(out) if code == 0 else None) or inst.get("version")


def newest_version(ad, inst):
    """The newest version of the agent where its install method gets it, else where its adapter says its vendor
    publishes it (UPDATE: self_check, npm, pypi, brew, cask). None when nothing says."""
    spec = ad.update or {}
    how = inst.get("how")
    if how == "npm":
        return _registry(how, inst["package"])
    if how in ("brew", "cask"):
        try:
            return _registry(how, inst["package"])
        except Exception:  # noqa: BLE001 - a formula of a tap is not on formulae.brew.sh (opencode-v2): brew knows it
            return _brew_info(how, inst)
    if how in ("uv", "pipx"):
        return _registry("pypi", inst["package"])
    if spec.get("self_check"):
        code, out = _run([inst["path"], *spec["self_check"]], VERSION_TIMEOUT)
        m = re.search(spec.get("self_read") or _VER.pattern, out or "")
        if code == 0 and m:
            return m.group(1) if m.groups() else m.group(0)
    for kind in ("npm", "pypi", "brew", "cask"):
        if spec.get(kind):
            return _registry(kind, spec[kind])
    return None


def _fill(args, key, value):
    """args with key replaced by value; with no value, the word that holds key goes, and the flag before it."""
    out = []
    for a in args:
        if key in a:
            if value:
                out.append(a.replace(key, value))
            elif out and out[-1].startswith("-"):
                out.pop()
        else:
            out.append(a)
    return out


def update_argv(ad, inst, version=None):
    """The command that installs `version` of the agent (None: its newest), by its install method; None when the
    method cannot install a given version and one is asked for (a rollback with Homebrew), or the agent has no
    method."""
    how, pkg = inst.get("how"), inst.get("package")
    if how == "npm":
        npm = os.path.join(inst["prefix"], "bin", "npm")
        npm = npm if os.access(npm, os.X_OK) else (_which("npm") or "npm")
        return [npm, "install", "-g", "--prefix", inst["prefix"], f"{pkg}@{version or 'latest'}"]
    if how in ("brew", "cask"):
        if version:
            return None
        brew = os.path.join(inst["prefix"], "bin", "brew")
        return [brew if os.access(brew, os.X_OK) else (_which("brew") or "brew"), "upgrade",
                "--formula" if how == "brew" else "--cask", pkg]
    if how == "uv":
        uv = _which("uv") or "uv"
        return [uv, "tool", "install", "--force", f"{pkg}=={version}"] if version else [uv, "tool", "upgrade", pkg]
    if how == "pipx":
        px = _which("pipx") or "pipx"
        return [px, "install", "--force", f"{pkg}=={version}"] if version else [px, "upgrade", pkg]
    if how == "self":
        args = (ad.update or {})["self"]
        if version and not any("{version}" in a for a in args):
            return None
        return [inst["path"], *_fill(args, "{version}", version)]
    return None


# ---------------------------------------------------------------------------------------------------------------
# Who runs the agent now
# ---------------------------------------------------------------------------------------------------------------

def _procs():
    """[(pid, args)] of every process here; the pid is None for a line of the PS hook with no pid."""
    if PS is not None:
        return [p if isinstance(p, tuple) else (None, p) for p in PS()]
    try:
        lines = subprocess.run(["ps", "-axo", "pid=,args="], capture_output=True, text=True,
                               timeout=10).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in lines:
        pid, _, args = line.strip().partition(" ")
        if pid.isdigit():
            out.append((int(pid), args.strip()))
    return out


def _service(ad, args):
    """True when the process is a background server of the CLI, no session (UPDATE['service'], a regex of its args):
    OpenCode's shared service 'opencode serve --service' ran for 9 days, and its update waited for '1 session' with no
    session of OpenCode (#6461)."""
    rx = (ad.update or {}).get("service")
    return bool(rx and re.search(rx, args))


def _root(inst):
    """The folder that holds the whole install: an npm package, a Homebrew formula or cask, a uv or pipx tool, else
    the folder of the program. A process whose program is in it runs this install (Codex's own binary is deep in
    its npm package)."""
    real = os.path.realpath(inst["path"])
    for rx in (r"^.*/lib/node_modules/(?:@[^/]+/)?[^/]+", r"^.*/(?:Cellar|Caskroom)/[^/]+",
               r"^.*/(?:uv/tools|pipx/venvs)/[^/]+"):
        m = re.match(rx, real)
        if m:
            return m.group(0)
    return os.path.dirname(real)


def running(ad, inst, procs=None):
    """How many sessions of this install of the agent's program run on this computer now (sessions_of)."""
    return len(sessions_of(ad, inst, procs))


def sessions_of(ad, inst, procs=None, services=False):
    """The processes [(pid, args)] of this install of the agent's program that run on this computer now: its
    sessions, MaximizePM's or a person's own; with services, only its background servers (_service), which no session
    count holds. A program that runs on node, bun or Python counts by its script. A program named
    with no folder counts when its name is the agent's (PATH finds this install); one with a folder counts when it is
    inside this install: a second install of the same CLI elsewhere (9 Codex sessions of /opt/homebrew while a test
    install in a temporary folder updated, 2026-11-20) does not hold this one's update."""
    exe, root = ad.describe["exe"], _root(inst)
    out = []
    for pid, line in _procs() if procs is None else procs:
        if _service(ad, line) != services:
            continue
        words = line.split()[:2]
        if not words:
            continue
        names = words[:1] + (words[1:2] if os.path.basename(words[0]).startswith(("node", "bun", "python", "deno"))
                             else [])
        for w in names:
            if "/" not in w:
                hit = w == exe
            else:
                rp = os.path.realpath(w)
                hit = rp == root or rp.startswith(root + os.sep)
            if hit:
                out.append((pid, line))
                break
    return out


def stop_services(ad, inst):
    """After an update, stop the background servers of the old version (sessions_of with services; none runs a
    session now, since the update waited for every session to end): the next start of the agent starts its server
    again on the new version, and the smoke test does not talk to the old one. Returns the args of each one stopped."""
    stopped = []
    for pid, args in sessions_of(ad, inst, services=True):
        if pid is None:
            continue
        try:
            (KILL or os.kill)(pid, 15)
            stopped.append(args)
        except OSError:
            pass
    return stopped


# ---------------------------------------------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------------------------------------------

def _adapters():
    reg = core._adapters()
    return [reg.load()[i] for i in reg.ids()]


def check_now(conn):
    """Read the installed and the newest version of each agent CLI that is installed here, and write them. Returns
    the file's dict."""
    t, rows, errors = time.time(), {}, []
    for ad in _adapters():
        inst = install_of(ad)
        if inst is None:
            rows[ad.id] = None
            continue
        row = {"installed": installed_version(ad, inst), "how": inst["how"], "package": inst["package"],
               "path": inst["path"], "latest": None, "error": None}
        try:
            row["latest"] = newest_version(ad, inst)
        except Exception as e:  # noqa: BLE001 - no network, a renamed package: this agent's row says so
            row["error"] = f"{type(e).__name__}: {e}"[:300]
            errors.append(ad.id)
        rows[ad.id] = row

    def write(d):
        d.update(checked_at=t, error=f"no newest version of {', '.join(errors)}" if errors else None)
        d.pop("check_asked", None)
        for aid, row in rows.items():
            a = d["agents"].setdefault(aid, {})
            if row is None:  # not installed here
                a.update(installed=None, how=None, path=None, latest=None, checked_at=t)
                continue
            if row["latest"] is None and row["error"]:
                row["latest"] = a.get("latest")  # keep the last newest version that was read
            a.update(row, checked_at=t)
        for aid in [k for k in d["agents"] if k not in rows]:
            d["agents"].pop(aid)
        return d
    return change(conn, write)


def _due(d):
    if not _started["checked"] or d.get("check_asked"):
        return True
    wait = AFTER_ERROR if d.get("error") else EVERY
    return time.time() - float(d.get("checked_at") or 0) >= wait


def _in_thread(key, fn, db):
    """Run fn(conn) in a thread of its own with a connection of its own (SYNC: now)."""
    def go():
        c = core.connect(db)
        try:
            fn(c)
        except Exception as e:  # noqa: BLE001 - one check or update, not the loop
            print(f"maxpm serve: agent versions ({key}): {type(e).__name__}: {e}", flush=True)
        finally:
            c.close()
            with _lock:
                _threads.pop(key, None)
    with _lock:
        if key in _threads:
            return False
        _threads[key] = th = threading.Thread(target=go, name=f"maxpm-agent-update-{key}", daemon=True)
    if SYNC:
        go()
    else:
        th.start()
    return True


# ---------------------------------------------------------------------------------------------------------------
# The update
# ---------------------------------------------------------------------------------------------------------------

def _set(conn, aid, **fields):
    def f(d):
        a = d["agents"].setdefault(aid, {})
        u = dict(a.get("update") or {})
        if fields.get("state") and fields["state"] != u.get("state"):
            u["state_at"] = time.time()  # the clock of LIMIT
        u.update(fields, at=time.time())
        a["update"] = u
        return u
    return change(conn, f)


def _tail(text, n=6):
    lines = [x.rstrip() for x in (text or "").splitlines() if x.strip()]
    return "\n".join(lines[-n:])[-1500:]


def _people(conn):
    m = core.active_manager(conn)
    return [m] if m else [r["name"] for r in conn.execute("SELECT name FROM agents WHERE kind='human' ORDER BY name")]


def _alert(conn, body):
    with core.tx(conn):
        for who in _people(conn):
            core._send(conn, "alert", "maxpm", body, to=who)


def _person_item(conn, ad, argv, out):
    """A person's item to run the update in a terminal (it asks for a password, sudo or a sign-in). Returns its id, or
    None when no project takes it (then the manager or the person gets an alert)."""
    cmd = shlex.join(argv)
    title = f"Update {ad.label} in a terminal: {cmd}"
    old = conn.execute("SELECT id FROM items WHERE title=? AND status IN ('open', 'in_progress', 'held')",
                       (title,)).fetchone()
    if old:
        return old["id"]
    p = conn.execute("SELECT name FROM projects WHERE archived=0 ORDER BY rank, id LIMIT 1").fetchone()
    ctx = (f"MaximizePM tried to update {ad.label}, and the update asks for something only a person gives (a "
           f"password, sudo or a sign-in). Run in a terminal: {cmd}. Then mark this item done; MaximizePM reads the "
           f"new version at its next check. Its last lines: {_tail(out, 4)}")
    if p is None:
        _alert(conn, ctx)
        return None
    return core.item_add(conn, p["name"], title[:300], priority=2, doer="human", actor="maxpm", goals=[],
                         short=f"Update {ad.label}"[:60], context=ctx)["id"]


def _task_project(conn):
    """The project of an investigate task: the one whose folder holds MaximizePM's own code, else the first project."""
    try:
        names = core.projects_for_dir(conn, Path(__file__).resolve().parent)
    except (OSError, ValueError):
        names = []
    if names:
        return names[0]
    p = conn.execute("SELECT name FROM projects WHERE archived=0 ORDER BY rank, id LIMIT 1").fetchone()
    return p["name"] if p else None


def _investigate(conn, ad, inst, old, target, failure, output=None, step=None, priority=2, shown="Last output"):
    """A failed update adds a task to investigate it (#6461, mark 2026-11-20: 'when we have a failure we should hand
    that off as a task to investigate'): the agent, its versions and the failure in the title; the message, the
    command, the output and what the install probe found in its context. One task while it is open for the same
    agent and the same versions. Returns its id, or None when no project takes it: then the manager or the person
    gets an alert."""
    head = f"Investigate the failed update of {ad.label} {old or '?'} to {target or 'its newest'}:"
    old_task = conn.execute("SELECT id FROM items WHERE status IN ('open', 'in_progress', 'held') AND "
                            "substr(title, 1, ?)=? ORDER BY id LIMIT 1", (len(head), head)).fetchone()
    if old_task:
        return old_task["id"]
    inst = inst or {}
    real = os.path.realpath(inst["path"]) if inst.get("path") else None
    probe = (f"how {inst.get('how') or 'unknown'}, package {inst.get('package') or 'none'}, prefix "
             f"{inst.get('prefix') or 'none'}, program {inst.get('path') or 'not found'}"
             + (f" (the file {real})" if real and real != inst.get("path") else ""))
    ctx = (f"MaximizePM tried to update {ad.label} from {old or 'an unknown version'} to {target or 'its newest'}, "
           f"and the update failed: {failure}.\n"
           + (f"Command: {step}\n" if step else "")
           + f"Install probe (river/agentupdate.py install_of): {probe}.\n"
           + (f"{shown}:\n{_tail(output, 15)}\n" if output else "")
           + f"Find the cause. When it is MaximizePM's (the install probe, the update command, the smoke test), fix "
             f"it in river/agentupdate.py with a test. Then update {ad.label} with Update now in Settings > Agents and "
             f"close this task with the cause. A step that needs a password, sudo or a sign-in goes to the person as "
             f"a person's item.")
    project = _task_project(conn)
    if project is None:
        _alert(conn, ctx)
        return None
    return core.item_add(conn, project, f"{head} {failure}"[:300], priority=priority, doer="ai", actor="maxpm",
                         goals=[], kind="operational", short=f"Failed update of {ad.label}"[:60],
                         context=ctx)["id"]


def _failed(conn, ad, inst, old, target, message, output=None, step=None, priority=2):
    """Mark the update failed, with the task that investigates it (_investigate)."""
    task = _investigate(conn, ad, inst, old, target, message, output, step, priority)
    return _set(conn, ad.id, state="failed", message=message, output=_tail(output) if output else None, task=task)


def _default_model(conn, ad):
    """The id of the model a start of the agent takes when it names none, or None (the agent picks)."""
    try:
        model = core.agent_default(conn, ad, "model") or ad.describe.get("default_model") or None
        return core.model_id(conn, core._platform_of(ad), model) if model else None
    except Exception:  # noqa: BLE001 - a bad setting: the agent picks
        return None


def smoke(conn, ad, inst):
    """(passed, what it shows): one short request that is not interactive (UPDATE['smoke']) in an empty folder must
    answer OK. An agent with no smoke request passes when its version command answers."""
    args = (ad.update or {}).get("smoke")
    if not args:
        code, out = _run([inst["path"], *((ad.update or {}).get("version") or ["--version"])], VERSION_TIMEOUT)
        return code == 0, f"{ad.describe['exe']} --version: {_tail(out, 1)}"
    argv = [inst["path"], *_fill(args, "{model}", _default_model(conn, ad))]
    with tempfile.TemporaryDirectory(prefix="maxpm-smoke-") as d:
        code, out = _run(argv, SMOKE_TIMEOUT, cwd=d)
    shown = " ".join(shlex.quote(w) for w in [ad.describe["exe"], *argv[1:]])
    return code == 0 and bool(OK_ANSWER.search(out or "")), f"{shown}: exit {code}; {_tail(out, 3)}"


def run_update(conn, aid):
    """Update one agent now (the state is 'updating'): the update command, its new version, the smoke test, and on a
    failed test the old version back. Writes each step to the file; the page shows it."""
    ad = core._adapters().find(aid)
    inst = install_of(ad) if ad else None
    if inst is None:
        return _set(conn, aid, state="failed", message="the program is not installed here")
    old = installed_version(ad, inst)
    target = read(conn)["agents"].get(aid, {}).get("latest")
    want = target if newer(target, old) else None
    # The newest version is what an update without a version installs too: Homebrew and a self updater with no
    # {version} cannot install a given one, and Goose 1.57.0 of Homebrew failed with 'does not know how Goose was
    # installed' for that (#6461).
    argv = update_argv(ad, inst, want) or (update_argv(ad, inst) if want else None)
    if argv is None:
        return _failed(conn, ad, inst, old, target, f"MaximizePM has no update command for {ad.label} as installed "
                                                    f"at {inst['path']}")
    step = shlex.join(argv)
    _set(conn, aid, state="updating", step=step, started=time.time(), pid=os.getpid(), old=old, task=None)
    code, out = _run(argv, UPDATE_TIMEOUT)
    if code != 0:
        if NEEDS_PERSON.search(out or ""):
            item = _person_item(conn, ad, argv, out)
            return _set(conn, aid, state="failed", person_item=item, output=_tail(out),
                        message="the update asks for a password, sudo or a sign-in"
                                + (f": your item #{item} has the command" if item else ""))
        return _failed(conn, ad, inst, old, target, f"the update failed (exit {code}); {old} still runs", out, step)
    inst = install_of(ad) or inst
    new = installed_version(ad, inst)
    change(conn, lambda d: d["agents"].setdefault(aid, {}).update(installed=new))
    if not newer(new, old):
        if want:  # the command passed, but the version did not change: a stale tap, another copy first on PATH
            return _failed(conn, ad, inst, old, target, f"the update ran, but {ad.label} is still {new or old}", out,
                           step)
        return _set(conn, aid, state="done", new=new, output=_tail(out),
                    message=f"{ad.label} {new or old} is the newest version; nothing changed")
    stopped = stop_services(ad, inst)
    if stopped:
        with core.tx(conn):
            core._event(conn, None, "maxpm", f"stopped the old {ad.label} server after its update: {'; '.join(stopped)}"
                                             f"; the next start of {ad.label} starts it on {new}")
    _set(conn, aid, state="testing", new=new, step="smoke test")
    ok, shown = smoke(conn, ad, inst)
    if ok:
        change(conn, lambda d: d["agents"][aid].update(updated_at=time.time()))
        with core.tx(conn):
            core._event(conn, None, "maxpm", f"updated {ad.label} {old} -> {new}; the smoke test answered OK")
        return _set(conn, aid, state="done", output=shown, message=f"updated {old} → {new}; the test answered OK")
    back = update_argv(ad, inst, old) if old else None
    restored = None
    if back:
        bcode, bout = _run(back, UPDATE_TIMEOUT)
        restored = installed_version(ad, install_of(ad) or inst) if bcode == 0 else None
        change(conn, lambda d: d["agents"][aid].update(installed=restored or new))
    if restored and not newer(restored, old) and not newer(old, restored):
        how = f"the old version {old} is back"
    else:
        how = (f"MaximizePM cannot put {old} back ({inst['how']} installs only the newest)" if not back
               else f"putting {old} back failed")
    # A new version that fails and stays is a broken agent: its task comes first.
    return _failed(conn, ad, inst, old, target or new, f"the test after the update failed; {how}", shown, step,
                   priority=2 if "is back" in how else 1)


# ---------------------------------------------------------------------------------------------------------------
# Actions and the pass of serve
# ---------------------------------------------------------------------------------------------------------------

def _find(aid):
    ad = core._adapters().find(aid or "")
    if ad is None:
        raise RiverError(f"no agent {aid!r}; the agents: {', '.join(core._adapters().ids())}")
    return ad


def request(conn, aid, who=None):
    """Update now: queue the update of agent aid. maxpm serve starts it at its next pass when no process of the agent
    runs, else when the last one ends; new starts of the agent wait for it. Returns the agent's status row."""
    ad = _find(aid)
    row = read(conn)["agents"].get(ad.id) or {}
    if not row.get("installed") and not install_of(ad):
        raise RiverError(f"{ad.label} is not installed here; Settings > Agents installs it")
    if (row.get("update") or {}).get("state") in ACTIVE:
        return agent_status(conn, ad.id)
    _set(conn, ad.id, state="queued", by=who or "maxpm", message=None, output=None, person_item=None, new=None,
         step=None, task=None, late=None, idle_from=None, waits=0, target=row.get("latest"))
    with core.tx(conn):
        core._event(conn, None, who or "maxpm", f"asked for the update of {ad.label}"
                                                + (f" to {row['latest']}" if row.get("latest") else ""))
    return agent_status(conn, ad.id)


def cancel(conn, aid, who=None):
    """Take back a queued update that has not started."""
    ad = _find(aid)

    def f(d):
        u = (d["agents"].get(ad.id) or {}).get("update") or {}
        if u.get("state") != "queued":
            raise RiverError(f"the update of {ad.label} is {u.get('state') or 'not asked'}: only a queued update stops")
        d["agents"][ad.id]["update"] = {**u, "state": "cancelled", "at": time.time(), "message": f"cancelled by {who}"}
    change(conn, f)
    return agent_status(conn, ad.id)


def later(conn, aid, who=None):
    """Later in the dialog: no dialog for this newest version of the agent for a day."""
    ad = _find(aid)
    change(conn, lambda d: d["agents"].setdefault(ad.id, {}).update(
        later={"version": (d["agents"].get(ad.id) or {}).get("latest"), "at": time.time(), "by": who}))
    return agent_status(conn, ad.id)


def check_soon(conn):
    """Check now: the next pass of serve reads every version again."""
    change(conn, lambda d: d.update(check_asked=time.time()))
    return status(conn)


def auto_ids(conn):
    """The adapter ids of agent_auto_update: 'all', or a list of agent names; none by default."""
    v = core.setting(conn, "agent_auto_update").strip()
    reg = core._adapters()
    if v == "all":
        return set(reg.ids())
    return {reg.find(n).id for n in re.split(r"[,\s]+", v) if n and reg.find(n)}


def set_auto(conn, aid, on, who=None):
    """Update agent aid by itself (on) or only with Update now (off): the setting agent_auto_update."""
    ad = _find(aid)
    reg = core._adapters()
    have = auto_ids(conn)
    have = (have | {ad.id}) if on else (have - {ad.id})
    value = "all" if have == set(reg.ids()) else ",".join(i for i in reg.ids() if i in have)
    if value:
        core.config_set(conn, "agent_auto_update", value, actor=who)
    else:
        core.config_unset(conn, "agent_auto_update", actor=who)
    return agent_status(conn, ad.id)


def _busy(conn, ad):
    """True when a session of MaximizePM on this agent works now (core.busy_now)."""
    names = [r["name"] for r in conn.execute("SELECT name FROM agents WHERE kind='ai' AND agent_type=? AND pid IS NOT "
                                             "NULL", (core._platform_of(ad),))]
    return bool(names) and bool(core.busy_now(conn, names))


def _late(conn, aid, a, u, since, what, procs=()):
    """A state past its LIMIT adds the task that investigates it (_investigate) and links it on the row; the update
    keeps its state (a wait that ends still updates). Returns the task's id, or None."""
    limit = LIMIT.get(u.get("state"))
    if not limit or u.get("task") or not since or time.time() - float(since) < limit:
        return None
    ad = core._adapters().find(aid)
    if ad is None:
        return None
    inst = install_of(ad) or {"path": a.get("path"), "how": a.get("how"), "package": a.get("package")}
    failure = f"for {round((time.time() - float(since)) / 60)} min it {what}"
    task = _investigate(conn, ad, inst, u.get("old") or a.get("installed"), u.get("target") or a.get("latest"),
                        failure, "\n".join(procs) or None, u.get("step"), shown="The processes it waits for")
    _set(conn, aid, task=task, late=failure)
    return task


def serve_pass(conn):
    """A pass of the loop of maxpm serve: the check when it is due (when serve starts, then once a day), an
    automatic update of an agent in agent_auto_update with a newer version, and each queued update whose agent no
    process runs, each in a thread of its own. An update that stopped with its process is marked failed. Returns the
    ids of the updates it started."""
    d = read(conn)
    db = _db(conn)
    if (core.setting(conn, "agent_update_check") == "on" or d.get("check_asked")) and _due(d) \
            and not core.net_down(conn):
        _started["checked"] = True
        _in_thread("check", check_now, db)
    auto = auto_ids(conn)
    started = []
    for aid, a in list(d["agents"].items()):
        u = a.get("update") or {}
        if u.get("state") in ("updating", "testing"):
            if aid in _threads or (u.get("pid") and u.get("pid") != os.getpid() and core.pid_alive(u["pid"])):
                # It runs here, or in another process of serve: past its limit, a task investigates the hang.
                _late(conn, aid, a, u, u.get("state_at") or u.get("started") or u.get("at"),
                      "is still updating" if u["state"] == "updating" else "is still in its smoke test")
                continue
            ad = core._adapters().find(aid)
            msg = "the update stopped with its process; check the version and update again"
            if ad is None:
                _set(conn, aid, state="failed", message=msg)
            else:
                _failed(conn, ad, install_of(ad) or {"path": a.get("path"), "how": a.get("how"),
                                                     "package": a.get("package")},
                        u.get("old") or a.get("installed"), u.get("target") or a.get("latest"), msg, step=u.get("step"))
            continue
        if aid in auto and u.get("state") not in ACTIVE and newer(a.get("latest"), a.get("installed")) \
                and not (u.get("state") == "failed" and u.get("target") == a.get("latest")):
            request(conn, aid, "maxpm")
            u = {"state": "queued"}
        if u.get("state") != "queued" or aid in _threads:
            continue
        ad = core._adapters().find(aid)
        inst = install_of(ad) if ad else None
        if ad is None or inst is None:
            _set(conn, aid, state="failed", message="the program is not installed here")
            continue
        procs = sessions_of(ad, inst)
        n = len(procs)
        if n:
            t, fields = time.time(), {}
            if (u.get("waits") or 0) != n:
                fields["waits"] = n
            # The clock of the wait runs while no MaximizePM session of the agent works; one more write a minute.
            if not u.get("idle_from") or (t - float(u["idle_from"]) >= 60 and _busy(conn, ad)):
                fields["idle_from"] = t
            if fields:
                u = _set(conn, aid, state="queued", **fields)
            _late(conn, aid, a, u, u.get("idle_from"),
                  f"waits for {n} process{'es' if n > 1 else ''} of {ad.label} while no MaximizePM session of "
                  f"{ad.label} works", [args for _, args in procs])
            continue

        def claim(d, aid=aid):
            uu = (d["agents"].get(aid) or {}).get("update") or {}
            if uu.get("state") != "queued":
                return False
            d["agents"][aid]["update"] = {**uu, "state": "updating", "waits": 0, "pid": os.getpid(),
                                          "started": time.time(), "at": time.time()}
            return True
        if change(conn, claim) and _in_thread(aid, lambda c, aid=aid: run_update(c, aid), db):
            started.append(aid)
    return started


def running_text():
    """Why serve may not start again now: an agent update that a thread of this process runs, or None."""
    with _lock:
        ups = [k for k in _threads if k != "check"]
    return f"the update of {', '.join(ups)} runs" if ups else None


def hold(conn, platform):
    """Why a new start of the agent waits now (core.pick_provider), or None: an update of it is queued or runs."""
    ad = core._adapters().find(platform or "")
    if ad is None:
        return None
    p = core._beside_queue(conn, FILE)
    if p is None or not p.exists():
        return None
    u = (read(conn)["agents"].get(ad.id) or {}).get("update") or {}
    if u.get("state") not in ACTIVE:
        return None
    what = {"queued": "waits to update", "updating": "updates now", "testing": "is tested after its update"}
    return f"{ad.label} {what[u['state']]}; its new sessions start after the update"


# ---------------------------------------------------------------------------------------------------------------
# What the page shows
# ---------------------------------------------------------------------------------------------------------------

def agent_status(conn, aid, d=None):
    d = d if d is not None else read(conn)
    ad = core._adapters().find(aid)
    a = d["agents"].get(aid) or {}
    u = dict(a.get("update") or {})
    lat = a.get("later") or {}
    out = {"id": aid, "label": ad.label if ad else aid, "installed": a.get("installed"), "latest": a.get("latest"),
           "how": a.get("how"), "path": a.get("path"), "checked_at": a.get("checked_at"),
           "updated_at": a.get("updated_at"), "error": a.get("error"),
           "newer": newer(a.get("latest"), a.get("installed")),
           "can_update": bool(a.get("path")) and a.get("how") in ("npm", "brew", "cask", "uv", "pipx", "self"),
           "auto": aid in auto_ids(conn), "update": u or None,
           "later": bool(lat and lat.get("version") == a.get("latest") and time.time() - float(lat.get("at") or 0)
                         < LATER)}
    out["dialog"] = out["newer"] and out["can_update"] and not out["later"] and u.get("state") not in ACTIVE
    return out


def status(conn):
    """GET /api/agent-versions: the last check and each registered agent's row, in registry order, with no network
    call and no process started."""
    d = read(conn)
    rows = [agent_status(conn, aid, d) for aid in core._adapters().ids()]
    return {"checked_at": d.get("checked_at"), "error": d.get("error"), "check": core.setting(conn, "agent_update_check"),
            "checking": "check" in _threads or bool(d.get("check_asked")), "agents": rows}
