"""maxpm serve at login (macOS launchd, Linux systemd user units): maxpm serve --install-login and
--remove-login, the watchdog: maxpm serve --install-watchdog and --remove-watchdog, and maxpm serve --kick, which
starts serve again through its login agent (the repair of the serve check).

The person's computer stays at home while they reach it through the relay, so maxpm serve must run after a
restart without anyone at the keyboard: a launchd agent in ~/Library/LaunchAgents starts it at login and again
when it stops with an error. The watchdog (#5441) is a second launchd job that runs maxpm ops check --outside every
minute, with no maxpm serve: it runs the checks that serve cannot run about itself, starts their repairs, and sends
a notice when serve is down. Standard library only.
"""

from __future__ import annotations

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
    Python, and the queue of this command."""
    env = {"PATH": os.pathsep.join(dict.fromkeys(
        [os.path.dirname(_maxpm()), os.path.dirname(sys.executable), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin",
         "/sbin"]))}
    if os.environ.get("MAXPM_DB"):
        env["MAXPM_DB"] = os.environ["MAXPM_DB"]
    return env


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


def _install_launchd(conn) -> dict:
    """Writes the launchd agent and loads it. When a maxpm serve already answers, launchd starts its own copy
    only at the next login (two would fight for the port)."""
    port = int(core.setting(conn, "serve_port"))
    log = str(Path(core.river_dir()) / "serve.log")
    spec = {"Label": LABEL, "ProgramArguments": [sys.executable, _maxpm(), "serve", "--port", str(port)],
            "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 30,
            "EnvironmentVariables": _env(), "StandardOutPath": log, "StandardErrorPath": log,
            "ProcessType": "Interactive"}
    path = plist_path()
    _write(path, spec)
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
    spec = {"Label": WATCHDOG_LABEL, "ProgramArguments": [sys.executable, _maxpm(), "ops", "check", "--outside"],
            "RunAtLoad": True, "StartInterval": WATCHDOG_EVERY, "EnvironmentVariables": _env(),
            "StandardOutPath": log, "StandardErrorPath": log}
    path = plist_path(WATCHDOG_LABEL)
    _write(path, spec)
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
