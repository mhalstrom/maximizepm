"""maxpm serve at login (macOS launchd): maxpm serve --install-login and --remove-login, and the watchdog:
maxpm serve --install-watchdog and --remove-watchdog.

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


def _macos(what: str) -> None:
    if core.PLATFORM != "darwin" and LAUNCHCTL is None:
        raise RiverError(f"maxpm serve --install-{what} is for macOS (launchd); on Linux use a systemd user unit")


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
    """Writes the launchd agent and loads it. When a maxpm serve already answers, launchd starts its own copy
    only at the next login (two would fight for the port)."""
    _macos("login")
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
    return {"plist": str(path), "port": port, "started_now": loaded, "already_running": running,
            "agent_loaded": agent_loaded, "log": log}


def remove() -> dict:
    path = plist_path()
    existed = path.exists()
    _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    if existed:
        path.unlink()
    return {"plist": str(path), "removed": existed}


def install_watchdog(conn) -> dict:
    """Writes the watchdog's launchd job and loads it: maxpm ops check --outside at load and every WATCHDOG_EVERY
    seconds. A job that is loaded already is loaded again, so it runs the new file: it holds no state between runs."""
    _macos("watchdog")
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
    n = conn.execute("SELECT COUNT(*) n FROM checks WHERE outside=1").fetchone()["n"]
    return {"plist": str(path), "every": WATCHDOG_EVERY, "log": log, "checks": n}


def remove_watchdog() -> dict:
    path = plist_path(WATCHDOG_LABEL)
    existed = path.exists()
    _launchctl("bootout", f"gui/{os.getuid()}/{WATCHDOG_LABEL}")
    if existed:
        path.unlink()
    return {"plist": str(path), "removed": existed}
