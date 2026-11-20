"""Install an agent CLI and sign in to it from the setup guide (#6239).

Each agent CLI that MaximizePM starts gets its vendor's official install method here, read on the vendor's own page
on 2026-11-19 (the method that needs no Node and no Homebrew first, where the vendor has one). A click runs the
install, or the CLI's own sign-in, in a window of the Terminal tab (routes/terminal.py, the tmux session
maxpm-shell): the setup guide shows the last lines, and the person opens the window to answer a question or type a
password. MaximizePM types nothing there and reads no token (p140: nothing goes into a CLI's home from here).

The installers put their programs in folders that the short PATH of an app started from Finder does not hold
(~/.local/bin, ~/.opencode/bin, Homebrew's bin): find() looks there too, and server._tmux_env gives those folders to
the panes, so the Start button finds what the guide found."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

from .core import RiverError

# platform -> how its vendor installs it. install: the shell command; cask: the Homebrew cask that install_command uses
# in place of install on a computer with Homebrew (optional);
# signin: the CLI's own sign-in command, or None (it asks at its first start, or uses a key in the environment);
# status: the command that exits 0 when signed in, or None (no vendor documents one); dirs: where it goes.
# Sources (read 2026-11-19): code.claude.com/docs/en/setup and cli-reference; github.com/openai/codex README and
# learn.chatgpt.com/docs/auth; github.com/github/copilot-cli README; antigravity.google/docs/getting-started and
# /docs/install;
# opencode.ai README (sst/opencode dev); github.com/block/goose README; aider.chat/docs/install.html;
# ampcode.com/docs/cli; cursor.com/docs/cli/installation; pi-mono coding-agent README; hermes-agent README.
METHODS = {
    "claude-code": {"install": "curl -fsSL https://claude.ai/install.sh | bash",
                    "signin": "claude auth login", "status": ["auth", "status"], "dirs": ["~/.local/bin"]},
    "codex": {"install": "curl -fsSL https://chatgpt.com/codex/install.sh | sh",
              # --device-auth: a link and a code that work in any browser, also on the phone (codex login alone waits
              # for a browser on this computer)
              "signin": "codex login --device-auth", "status": ["login", "status"], "dirs": ["~/.local/bin", "~/.codex/bin"]},
    "copilot": {"install": "curl -fsSL https://gh.io/copilot-install | bash",
                "signin": None, "status": None, "dirs": ["~/.local/bin"]},  # sign-in: /login inside copilot
    # Antigravity CLI (#6248), Google's coding agent: one Go program, agy. Google's script puts it in
    # ~/.local/bin (antigravity.google/docs/getting-started); on a Mac with Homebrew the cask antigravity-cli puts it
    # in Homebrew's bin, and brew upgrades it. It has no sign-in command: agy with no arguments asks for the sign-in
    # (Google OAuth, or a Google Cloud project) at its first start, and agy models exits 1 ("Please sign in to view
    # available models") until then, 0 after (measured with agy 1.5.13, about one second).
    "antigravity": {"install": "curl -fsSL https://antigravity.google/cli/install.sh | bash",
                    "cask": "antigravity-cli", "signin": "agy", "status": ["models"], "dirs": ["~/.local/bin"]},
    "opencode": {"install": "curl -fsSL https://opencode.ai/v2/install | bash",
                 "signin": None, "status": None, "dirs": ["~/.opencode/bin"]},
    "goose": {"install": "curl -fsSL https://github.com/aaif-goose/goose/releases/download/stable/download_cli.sh"
                         " | CONFIGURE=false bash", "signin": "goose configure", "status": None,
              "dirs": ["~/.local/bin"]},
    "aider": {"install": "curl -LsSf https://aider.chat/install.sh | sh",
              "signin": None, "status": None, "dirs": ["~/.local/bin"]},  # API keys in the environment
    "amp": {"install": "curl -fsSL https://ampcode.com/install.sh | bash",
            "signin": "amp login", "status": None, "dirs": ["~/.local/bin", "~/.amp/bin"]},
    "cursor": {"install": "curl https://cursor.com/install -fsS | bash",
               "signin": "cursor-agent login", "status": None, "dirs": ["~/.local/bin"]},
    "pi": {"install": "curl -fsSL https://pi.dev/install.sh | sh",
           "signin": None, "status": None, "dirs": ["~/.local/bin"]},  # sign-in: /login inside pi
    "hermes": {"install": "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash",
               "signin": "hermes setup --portal", "status": None, "dirs": ["~/.local/bin", "~/.hermes/bin"]},
    # Two programs are named grok (xAI's Grok Build, the community grok-cli); this is xAI's own.
    "grok": {"install": "curl -fsSL https://x.ai/cli/install.sh | bash",
             "signin": "grok login --device-auth", "status": None, "dirs": ["~/.local/bin", "~/.grok/bin"]},
}
# Where installers put programs, besides each method's dirs: Homebrew, the system, npm's and bun's global bins.
COMMON_DIRS = ["~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "~/.npm-global/bin", "~/.bun/bin",
               "~/.volta/bin"]
STATUS_TTL = 30.0  # seconds a sign-in status stays valid: the guide asks again after that
STATUS_RUNNER = None  # tests: fn(argv) -> exit code
_status = {}  # platform -> (when, True/False)
_lock = threading.Lock()


def method(platform):
    if platform not in METHODS:
        raise RiverError(f"no install method for {platform!r}; the agents: {', '.join(METHODS)}")
    return METHODS[platform]


BREW = None  # tests: the path of brew, or "" for none, in place of core.brew_path()


def install_command(platform):
    """The shell command that installs the agent here: brew install --cask <cask> when the method has a cask and this
    Mac has Homebrew (brew then upgrades it; casks are for macOS), else the vendor's install command."""
    from . import core
    m = method(platform)
    brew = BREW if BREW is not None else core.brew_path()
    if m.get("cask") and core.PLATFORM == "darwin" and brew:
        return f"{_q(brew)} install --cask {m['cask']}"
    return m["install"]


DIRS = None  # tests: the folders to search instead of the real ones


def dirs():
    """Every folder where an installer may put an agent program, expanded, in order, without repeats."""
    if DIRS is not None:
        return list(DIRS)
    out = []
    for d in COMMON_DIRS + [d for m in METHODS.values() for d in m["dirs"]]:
        p = os.path.expanduser(d)
        if p not in out:
            out.append(p)
    return out


def extra_path(path=None):
    """PATH with the installers' folders that exist added after its own: the panes of agents and sign-ins find the
    programs that the guide found, also when the app started from Finder with /usr/bin:/bin:/usr/sbin:/sbin."""
    have = [p for p in (path if path is not None else os.environ.get("PATH", "")).split(os.pathsep) if p]
    return os.pathsep.join(have + [d for d in dirs() if d not in have and os.path.isdir(d)])


def find(names, found=None):
    """{name: full path or None}: what a login shell found (found, from server._login_shell_which), else the
    installers' folders."""
    found = dict(found or {})
    for n in names:
        if not found.get(n):
            found[n] = next((os.path.join(d, n) for d in dirs() if os.access(os.path.join(d, n), os.X_OK)), None)
    return found


def signed_in(platform, exe):
    """True or False from the CLI's own status command, or None when it has none or is not installed. Kept for
    STATUS_TTL seconds: the guide asks on each draw, and a status command can take a second."""
    st = METHODS.get(platform, {}).get("status")
    if not st or not exe:
        return None
    with _lock:
        hit = _status.get(platform)
        if hit and time.monotonic() - hit[0] < STATUS_TTL:
            return hit[1]
    argv = [exe, *st]
    if STATUS_RUNNER is not None:
        ok = STATUS_RUNNER(argv) == 0
    else:
        try:
            ok = subprocess.run(argv, capture_output=True, timeout=10, stdin=subprocess.DEVNULL,
                                env={**os.environ, "PATH": extra_path()}).returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
    with _lock:
        _status[platform] = (time.monotonic(), ok)
    return ok


def forget_status(platform=None):
    with _lock:
        if platform:
            _status.pop(platform, None)
        else:
            _status.clear()


def _job_name(kind, label):
    return f"{'Install' if kind == 'install' else 'Sign in to'} {label}"


def jobs():
    """{platform: {kind: {pane, name, running, tail}}} for the install and sign-in windows in the Terminal tab:
    running while the command runs (the window keeps a shell after it), tail: its last lines."""
    from . import server
    from .routes import terminal
    if server._tmux("has-session", "-t", "=" + terminal.SHELL_SESSION, check=False) is None:
        return {}
    out = server._tmux("list-panes", "-s", "-t", "=" + terminal.SHELL_SESSION, "-F",
                       "#{pane_id}|#{@maxpm_job}|#{@maxpm_name}|#{@maxpm_exit}", check=False) or ""
    res = {}
    for line in out.splitlines():
        f = line.split("|", 3)
        if len(f) != 4 or ":" not in f[1]:
            continue
        kind, platform = f[1].split(":", 1)
        screen = server._tmux("capture-pane", "-p", "-J", "-t", f[0], check=False) or ""
        tail = [x.rstrip() for x in screen.splitlines() if x.strip()][-3:]
        res.setdefault(platform, {})[kind] = {"pane": f[0], "name": f[2], "running": not f[3],
                                              "exit": int(f[3]) if f[3].isdigit() else None, "tail": tail}
    return res


def run(conn, platform, kind, who):
    """Open a window of the Terminal tab that runs the install or the sign-in of an agent CLI, for a person. The
    window keeps a login shell after the command, so the person reads how it ended. Returns {pane, name}."""
    from . import core, server
    from .routes import terminal
    terminal.allowed(conn, who, terminal.relay_now())
    m = method(platform)
    label = core.platform_spec(platform)["label"]
    if kind == "install":
        cmd = install_command(platform)
    elif kind == "signin":
        cmd = m["signin"]
        if not cmd:
            raise RiverError(f"{label} has no sign-in command: it asks at its first start")
    else:
        raise RiverError("kind is install or signin")
    name = _job_name(kind, label)
    old = jobs().get(platform, {}).get(kind)
    if old:  # one window for each: an older one that still runs is ended, so its output does not mix with the new
        server._tmux("kill-pane", "-t", old["pane"], check=False)
    done = f"{label} is installed. Go back to MaximizePM's setup." if kind == "install" else \
        f"Back in MaximizePM's setup, {label} shows as signed in when it is."
    # The window's last step records the exit status on its pane (@maxpm_exit), which jobs() reads: the program that
    # runs in the pane does not say whether the command ended (it is bash, and then the person's shell).
    tmux_bin = (server._tmux_cmd() or ["tmux"])[0]
    script = (f"{cmd}\ns=$?\n{_q(tmux_bin)} set-option -p -t \"$TMUX_PANE\" @maxpm_exit \"$s\"\n"
              f"echo\nif [ $s -eq 0 ]; then echo {_q(done)}; else echo \"It stopped (exit $s).\"; fi\n"
              'exec "${SHELL:-/bin/zsh}" -l\n')
    new = ["-c", os.path.expanduser("~"), "-e", f"PATH={extra_path()}", "-P", "-F", "#{pane_id}", "/bin/bash", "-c", script]
    if server._tmux("has-session", "-t", "=" + terminal.SHELL_SESSION, check=False) is None:
        pane = server._tmux("new-session", "-d", "-s", terminal.SHELL_SESSION, "-n", name, "-x", "120", "-y", "36", *new)
    else:
        pane = server._tmux("new-window", "-d", "-t", f"={terminal.SHELL_SESSION}:", "-n", name, *new)
    for k, v in (("@maxpm_name", name), ("@maxpm_by", who), ("@maxpm_started", str(int(time.time()))),
                 ("@maxpm_job", f"{kind}:{platform}")):
        server._tmux("set-option", "-p", "-t", pane, k, v)
    forget_status(platform)
    with core.tx(conn):
        core._event(conn, None, who, f"opened a terminal that runs: {cmd} ({name}, pane {pane})")
    return {"pane": pane, "name": name, "command": cmd}


def _q(text):
    import shlex
    return shlex.quote(text)
