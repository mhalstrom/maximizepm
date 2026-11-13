"""maxpm serve at login (macOS launchd, Linux systemd user units): maxpm serve --install-login and
--remove-login, the watchdog: maxpm serve --install-watchdog and --remove-watchdog, and maxpm serve --kick, which
starts serve again through its login agent (the repair of the serve check).

The person's computer stays at home while they reach it through the relay, so maxpm serve must run after a
restart without anyone at the keyboard: a launchd agent in ~/Library/LaunchAgents starts it at login and again
when it stops with an error. The watchdog (#5441) is a second launchd job that runs maxpm ops check --outside every
minute, with no maxpm serve: it runs the checks that serve cannot run about itself, starts their repairs, and sends
a notice when serve is down. Standard library only.

The desktop app (#5649) installs the watchdog itself at each start (app_start), with no terminal command. Its jobs run
the app's own launcher (app_launcher), which runs the code version that the app runs now, so they follow each
one-click update. The app's "Keep MaximizePM running in the background" is the login agent above, installed by the app.
serve_wanted says whether maxpm serve should run at all: a login agent starts it, or the app is open. When neither
holds, the person stopped serve on purpose, and the watchdog runs none of the checks that need it.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from . import core
from .core import RiverError

LABEL = "com.maximizepm.serve"
WATCHDOG_LABEL = "com.maximizepm.watchdog"
WATCHDOG_EVERY = 60  # seconds between two runs of maxpm ops check --outside
LAUNCHCTL = None  # test hook: (argv) -> (returncode, output)
SYSTEMCTL = None  # test hook: (argv) -> (returncode, output)
SERVE_UNIT = "maxpm-serve.service"  # the Linux names (#5520)
WATCHDOG_UNIT = "maxpm-watchdog"  # .service and .timer


def plist_path(label: str = LABEL) -> Path:
    return Path(os.path.expanduser("~/Library/LaunchAgents")) / f"{label}.plist"


def _env() -> dict:
    """The environment of a launchd job: a PATH with this maxpm (the checks run maxpm ops probe), Homebrew and this
    Python, and the queue of this command. In the app: the app's launcher first, and the app's variables, so the
    job's maxpm is the app's (its updates, no git Update) and its Python writes no .pyc into the app bundle."""
    first = str(app_launcher().parent) if app_mode() else os.path.dirname(_maxpm())
    env = {"PATH": os.pathsep.join(dict.fromkeys(
        [first, os.path.dirname(sys.executable), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin",
         "/sbin"]))}
    if os.environ.get("MAXPM_DB"):
        env["MAXPM_DB"] = os.environ["MAXPM_DB"]
    if app_mode():
        env.update({k: os.environ[k] for k in APP_ENV if os.environ.get(k)})
        env.update(MAXPM_APP_DIR=str(_app_dir()), PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    return env


# The variables of the desktop app that its jobs keep (desktop/main.js sets them for the serve it starts).
APP_ENV = ("MAXPM_DESKTOP", "MAXPM_APP_BUNDLE", "MAXPM_APP_DIR", "MAXPM_APP_SHELL")
APP_LAUNCHER_MARK = "# MaximizePM app: the launcher of its launchd jobs"


def app_mode() -> bool:
    """The desktop app runs this code (#5649): it sets MAXPM_DESKTOP and names its bundle."""
    return os.environ.get("MAXPM_DESKTOP") == "1" and bool(os.environ.get("MAXPM_APP_BUNDLE"))


def _app_dir() -> Path:
    from . import release
    return release.app_dir()


def app_launcher() -> Path:
    """The program of the app's launchd jobs: a launcher in the app's folder."""
    return _app_dir() / "bin" / "maxpm"


def _app_launcher_text() -> str:
    """The launcher runs the code version named in the app's file current (each one-click update moves it, #5601),
    else the copy inside the app, with the app's Python. So the jobs need no change at an update."""
    import shlex
    bundle = Path(os.environ["MAXPM_APP_BUNDLE"])
    py, cur = shlex.quote(sys.executable), shlex.quote(str(_app_dir() / "current"))
    return "\n".join([
        "#!/bin/sh",
        f"{APP_LAUNCHER_MARK} (#5649). The app writes it at each start: it runs the code version",
        "# that the app runs now, else the app's own copy.",
        f"c=$(head -n 1 {cur} 2>/dev/null)",
        f'if [ -n "$c" ] && [ -f "$c/bin/maxpm" ]; then exec {py} "$c/bin/maxpm" "$@"; fi',
        f'exec {py} {shlex.quote(str(bundle / "bin" / "maxpm"))} "$@"', ""])


def _write_app_launcher() -> Path:
    path, text = app_launcher(), _app_launcher_text()
    if not path.is_file() or path.read_text(encoding="utf-8", errors="replace") != text:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(".maxpm.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.chmod(0o755)
        os.replace(tmp, path)
    return path


def _program(*args) -> list:
    """The command of a job: the app's launcher in the app (written now), else this Python and this maxpm."""
    if app_mode():
        return [str(_write_app_launcher()), *args]
    return [sys.executable, _maxpm(), *args]


def _write(path: Path, spec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        plistlib.dump(spec, f)


def _system(what: str) -> str:
    """launchd (macOS) or systemd (Linux): the one that starts jobs here. A test picks one with its hook."""
    if LAUNCHCTL is not None and SYSTEMCTL is None:
        return "launchd"
    if SYSTEMCTL is not None:
        return "systemd"
    if core.PLATFORM == "darwin":
        return "launchd"
    if core.PLATFORM.startswith("linux") and shutil.which("systemctl"):
        return "systemd"
    raise RiverError(f"maxpm serve --{what} needs launchd (macOS) or systemd (Linux); start maxpm serve with this "
                     f"computer's own way to run a program at login")


def unit_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")) / "systemd" / "user"


def _systemctl(*args) -> tuple[int, str]:
    argv = ["systemctl", "--user", *args]
    if SYSTEMCTL is not None:
        return SYSTEMCTL(argv)
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return r.returncode, (r.stdout + r.stderr).strip()


def _quote(arg: str) -> str:
    """One word of a systemd ExecStart line: quoted when it holds a space or a quote."""
    if arg and not any(c in arg for c in ' "\'\\$%;'):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$").replace("%", "%%") + '"'


def _unit_env() -> str:
    return "".join(f"Environment={_quote(f'{k}={v}')}\n" for k, v in _env().items())


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _maxpm() -> str:
    """The maxpm command that runs now: bin/maxpm of this folder, else the one on PATH."""
    here = Path(__file__).resolve().parent.parent / "bin" / "maxpm"
    return str(here) if here.exists() else (shutil.which("maxpm") or "maxpm")


def _launchctl(*args) -> tuple[int, str]:
    argv = ["launchctl", *args]
    if LAUNCHCTL is not None:
        return LAUNCHCTL(argv)
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return r.returncode, (r.stdout + r.stderr).strip()


def _serving(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def install(conn) -> dict:
    """maxpm serve at login and again after a crash: launchd or systemd (#5520)."""
    return _install_launchd(conn) if _system("install-login") == "launchd" else _install_systemd(conn)


def _install_systemd(conn) -> dict:
    """Writes the user unit maxpm-serve.service and enables it. When a maxpm serve already answers, systemd
    starts its own copy only at the next login (two would fight for the port)."""
    port = int(core.setting(conn, "serve_port"))
    log = str(Path(core.river_dir()) / "serve.log")
    path = unit_dir() / SERVE_UNIT
    _write_text(path, "[Unit]\nDescription=MaximizePM: maxpm serve (the page, the relay, the loop)\n\n[Service]\n"
                f"ExecStart={' '.join(_quote(a) for a in [sys.executable, _maxpm(), 'serve', '--port', str(port)])}\n"
                f"{_unit_env()}Restart=on-failure\nRestartSec=30\n"
                f"StandardOutput=append:{log}\nStandardError=append:{log}\n\n[Install]\nWantedBy=default.target\n")
    code, out = _systemctl("daemon-reload")
    if code:
        raise RiverError(f"systemctl --user daemon-reload failed: {out}; the file is {path}")
    running = _serving(port)
    code, out = _systemctl("enable", SERVE_UNIT) if running else _systemctl("enable", "--now", SERVE_UNIT)
    if code:
        raise RiverError(f"systemctl --user enable failed: {out}; the file is {path}")
    return {"file": str(path), "port": port, "started_now": not running, "already_running": running,
            "agent_loaded": False, "log": log, "system": "systemd"}


def _serve_spec(conn) -> dict:
    port = int(core.setting(conn, "serve_port"))
    log = str(Path(core.river_dir()) / "serve.log")
    return {"Label": LABEL, "ProgramArguments": _program("serve", "--port", str(port)),
            "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 30,
            "EnvironmentVariables": _env(), "StandardOutPath": log, "StandardErrorPath": log,
            "ProcessType": "Interactive"}


def _watchdog_spec() -> dict:
    log = str(Path(core.river_dir()) / "watchdog.log")
    return {"Label": WATCHDOG_LABEL, "ProgramArguments": _program("ops", "check", "--outside"),
            "RunAtLoad": True, "StartInterval": WATCHDOG_EVERY, "EnvironmentVariables": _env(),
            "StandardOutPath": log, "StandardErrorPath": log}


def _install_launchd(conn) -> dict:
    """Writes the launchd agent and loads it. When a maxpm serve already answers, launchd starts its own copy
    only at the next login (two would fight for the port)."""
    port = int(core.setting(conn, "serve_port"))
    log = str(Path(core.river_dir()) / "serve.log")
    path = plist_path()
    _write(path, _serve_spec(conn))
    uid = os.getuid()
    # Loaded already (it may run this very maxpm serve): leave it; launchd reads the new file at the next login.
    agent_loaded = _launchctl("print", f"gui/{uid}/{LABEL}")[0] == 0
    running = _serving(port)
    loaded = False
    if not running and not agent_loaded:
        code, out = _launchctl("bootstrap", f"gui/{uid}", str(path))
        if code:
            raise RiverError(f"launchctl bootstrap failed: {out}; the file is {path}")
        loaded = True
    return {"plist": str(path), "file": str(path), "port": port, "started_now": loaded, "already_running": running,
            "agent_loaded": agent_loaded, "log": log, "system": "launchd"}


def remove() -> dict:
    if _system("remove-login") == "systemd":
        return _remove_units(SERVE_UNIT)
    path = plist_path()
    existed = path.exists()
    _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    if existed:
        path.unlink()
    return {"plist": str(path), "file": str(path), "removed": existed}


def _remove_units(*names) -> dict:
    """Disable and stop the user units, then remove their files (systemd)."""
    for n in names:
        _systemctl("disable", "--now", n)
    paths = [unit_dir() / n for n in names]
    paths += [p.with_suffix(".service") for p in paths if p.suffix == ".timer"]
    existed = False
    for p in paths:
        if p.exists():
            p.unlink()
            existed = True
    _systemctl("daemon-reload")
    return {"file": str(paths[0]), "removed": existed}


def _outside_checks(conn) -> int:
    return conn.execute("SELECT COUNT(*) n FROM checks WHERE outside=1").fetchone()["n"]


def install_watchdog(conn) -> dict:
    """maxpm ops check --outside every WATCHDOG_EVERY seconds: a launchd job or a systemd timer (#5520)."""
    if _system("install-watchdog") == "launchd":
        return _install_watchdog_launchd(conn)
    log = str(Path(core.river_dir()) / "watchdog.log")
    service, timer = unit_dir() / f"{WATCHDOG_UNIT}.service", unit_dir() / f"{WATCHDOG_UNIT}.timer"
    _write_text(service, "[Unit]\nDescription=MaximizePM: the watchdog (maxpm ops check --outside)\n\n[Service]\n"
                "Type=oneshot\n"
                f"ExecStart={' '.join(_quote(a) for a in [sys.executable, _maxpm(), 'ops', 'check', '--outside'])}\n"
                f"{_unit_env()}StandardOutput=append:{log}\nStandardError=append:{log}\n")
    _write_text(timer, f"[Unit]\nDescription=MaximizePM: the watchdog every {WATCHDOG_EVERY} s\n\n[Timer]\n"
                f"OnActiveSec=0\nOnUnitActiveSec={WATCHDOG_EVERY}s\nAccuracySec=5s\n\n[Install]\nWantedBy=timers.target\n")
    for args in (("daemon-reload",), ("enable", "--now", timer.name), ("restart", timer.name)):
        code, out = _systemctl(*args)
        if code:
            raise RiverError(f"systemctl --user {' '.join(args)} failed: {out}; the files are in {unit_dir()}")
    return {"file": str(timer), "every": WATCHDOG_EVERY, "log": log, "checks": _outside_checks(conn),
            "system": "systemd"}


def _install_watchdog_launchd(conn) -> dict:
    """Writes the watchdog's launchd job and loads it: maxpm ops check --outside at load and every WATCHDOG_EVERY
    seconds. A job that is loaded already is loaded again, so it runs the new file: it holds no state between runs."""
    log = str(Path(core.river_dir()) / "watchdog.log")
    path = plist_path(WATCHDOG_LABEL)
    _write(path, _watchdog_spec())
    uid = os.getuid()
    if _launchctl("print", f"gui/{uid}/{WATCHDOG_LABEL}")[0] == 0:
        _launchctl("bootout", f"gui/{uid}/{WATCHDOG_LABEL}")
    code, out = _launchctl("bootstrap", f"gui/{uid}", str(path))
    if code:
        raise RiverError(f"launchctl bootstrap failed: {out}; the file is {path}")
    return {"plist": str(path), "file": str(path), "every": WATCHDOG_EVERY, "log": log, "checks": _outside_checks(conn),
            "system": "launchd"}


def remove_watchdog() -> dict:
    if _system("remove-watchdog") == "systemd":
        return _remove_units(f"{WATCHDOG_UNIT}.timer")
    path = plist_path(WATCHDOG_LABEL)
    existed = path.exists()
    _launchctl("bootout", f"gui/{os.getuid()}/{WATCHDOG_LABEL}")
    if existed:
        path.unlink()
    return {"plist": str(path), "file": str(path), "removed": existed}


def kick() -> dict:
    """maxpm serve --kick: stop maxpm serve and start it again through its login agent, also when it hangs (the
    repair of the serve check). Refused when no login agent is installed: then nothing would start it again."""
    if _system("kick") == "systemd":
        if not (unit_dir() / SERVE_UNIT).exists():
            raise RiverError("no login agent starts maxpm serve here: install one first, maxpm serve --install-login")
        code, out = _systemctl("restart", SERVE_UNIT)
    else:
        if not plist_path().exists():
            if _stop_app_serve():
                return {"kicked": True, "how": "app"}
            raise RiverError("no login agent starts maxpm serve here: install one first, maxpm serve --install-login")
        uid = os.getuid()
        if _launchctl("print", f"gui/{uid}/{LABEL}")[0] != 0:  # the file is there, launchd has not loaded it
            code, out = _launchctl("bootstrap", f"gui/{uid}", str(plist_path()))
            if code == 0:
                return {"kicked": True, "how": "loaded"}
        code, out = _launchctl("kickstart", "-k", f"gui/{uid}/{LABEL}")
    if code:
        raise RiverError(f"the login agent did not start maxpm serve again: {out}")
    return {"kicked": True, "how": "restarted"}


def _app_marker() -> dict | None:
    """The desktop app while it is open (#5649): desktop/main.js writes <app folder>/open, {app, serve, db} (its own
    pid, the pid of the serve it started or null, the queue), and removes it when it quits. None when the app is not
    open for this queue (no file, its process is gone, or another queue)."""
    try:
        m = json.loads((_app_dir() / "open").read_text(encoding="utf-8"))
        pid = int(m["app"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not core.pid_alive(pid):
        return None
    if m.get("db") and Path(str(m["db"])).expanduser().resolve() != Path(str(core.db_path())).expanduser().resolve():
        return None
    return m


def serve_wanted() -> bool:
    """Whether maxpm serve should run now (#5649): a login agent starts it (maxpm serve --install-login, or the app's
    Keep MaximizePM running in the background), or the desktop app is open. Otherwise the person stopped serve on
    purpose (quit the app, or stopped a serve started in a terminal), and the watchdog runs none of the checks that
    need serve: no repair, no notice."""
    return plist_path().exists() or (unit_dir() / SERVE_UNIT).exists() or _app_marker() is not None


KILL_WAIT = 5.0  # seconds a serve gets to stop after SIGTERM, before SIGKILL


def _stop_app_serve() -> bool:
    """The repair while the app is open and no login agent runs serve: stop the serve that the app started; the app
    starts it again (desktop/main.js watch). False when there is no such serve: a serve that the app did not start
    (a terminal's) is never stopped."""
    import signal
    import time
    m = _app_marker()
    try:
        pid = int((m or {}).get("serve") or 0)
    except (TypeError, ValueError):
        pid = 0
    if not pid or not core.pid_alive(pid):
        return False
    os.kill(pid, signal.SIGTERM)
    until = time.monotonic() + KILL_WAIT
    while time.monotonic() < until and core.pid_alive(pid):
        time.sleep(0.1)
    if core.pid_alive(pid):
        os.kill(pid, signal.SIGKILL)
    return True


def _job_program(label: str) -> list | None:
    """The program of an installed launchd job, [] for a file that does not read, or None when there is none."""
    path = plist_path(label)
    if not path.exists():
        return None
    try:
        return list(plistlib.loads(path.read_bytes()).get("ProgramArguments") or [])
    except Exception:  # noqa: BLE001 - a broken file is another install's: leave it alone
        return []


def _apps(program: list | None) -> bool:
    return bool(program) and program[0] == str(app_launcher())


def _same_file(path: Path, spec: dict) -> bool:
    try:
        return path.read_bytes() == plistlib.dumps(spec)
    except OSError:
        return False


def app_start(conn) -> dict:
    """The desktop app at each start (#5649): install or update the watchdog, so a person with only the app gets it
    with no terminal command. Its jobs run the app's launcher, which follows each update. A login serve or a watchdog
    that another install made (a git clone: maxpm serve --install-login in a terminal) is left alone, with the other
    job: that install keeps both. Returns {watchdog: installed|current|left|none, background: on|off|other, by}."""
    if not app_mode() or (LAUNCHCTL is None and core.PLATFORM != "darwin"):
        return {"watchdog": "none", "background": "off", "by": None}
    serve, dog = _job_program(LABEL), _job_program(WATCHDOG_LABEL)
    other = next((p for p in (serve, dog) if p is not None and not _apps(p)), None)
    out = {"background": "off" if serve is None else "on" if _apps(serve) else "other",
           "by": " ".join(other) if other is not None else None}
    if other is not None:
        return {**out, "watchdog": "left"}
    if serve is not None:
        spec = _serve_spec(conn)  # the app moved, or its shell changed: launchd reads the file at the next load
        if not _same_file(plist_path(), spec):
            _write(plist_path(), spec)
    spec = _watchdog_spec()
    if _same_file(plist_path(WATCHDOG_LABEL), spec) and _launchctl("print", f"gui/{os.getuid()}/{WATCHDOG_LABEL}")[0] == 0:
        return {**out, "watchdog": "current"}
    _install_watchdog_launchd(conn)
    return {**out, "watchdog": "installed"}
