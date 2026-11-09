"""maxpm serve at login (macOS launchd): maxpm serve --install-login and --remove-login.

The person's computer stays at home while they reach it through the relay, so maxpm serve must run after a
restart without anyone at the keyboard: a launchd agent in ~/Library/LaunchAgents starts it at login and again
when it stops with an error. Standard library only.
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
LAUNCHCTL = None  # test hook: (argv) -> (returncode, output)


def plist_path() -> Path:
    return Path(os.path.expanduser("~/Library/LaunchAgents")) / f"{LABEL}.plist"


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
    if core.PLATFORM != "darwin" and LAUNCHCTL is None:
        raise RiverError("maxpm serve --install-login is for macOS (launchd); on Linux use a systemd user unit")
    port = int(core.setting(conn, "serve_port"))
    env = {"PATH": os.pathsep.join(dict.fromkeys(
        [os.path.dirname(sys.executable), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin",
         "/sbin"]))}
    if os.environ.get("MAXPM_DB"):
        env["MAXPM_DB"] = os.environ["MAXPM_DB"]
    log = str(Path(core.river_dir()) / "serve.log")
    spec = {"Label": LABEL, "ProgramArguments": [sys.executable, _maxpm(), "serve", "--port", str(port)],
            "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 30,
            "EnvironmentVariables": env, "StandardOutPath": log, "StandardErrorPath": log,
            "ProcessType": "Interactive"}
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        plistlib.dump(spec, f)
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
