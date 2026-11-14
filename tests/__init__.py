"""The tests never reach the tmux of the person who runs them. Every agent starts in a tmux pane: a test fakes
the pane (a runner, or server.TERMINAL_RUNNER, gets the line that would go into it), the whole of tmux (the fake
server.TMUX_RUNNER), or runs a tmux server of its own (server.TMUX_CMD). With no tmux found (core.tmux_path), a
start refuses; a test of an installed tmux sets core.tmux_path itself.

The tests also run as no session: a session that maxpm serve opened has its name, purpose, and model in
the environment (MAXPM_FOCUS=deploy:<target> makes go look only for a deploy), and a test that runs go or
the MCP server there would read them (#2261). A test that needs one sets it itself. MAXPM_DB stays: a test
names its own database, and the variable never points a test at the real queue by being removed."""
import os

from river import core, dictation, server

for _name in ("MAXPM_AGENT", "MAXPM_AGENT_TYPE", "MAXPM_CHAT", "MAXPM_DESKTOP", "MAXPM_FOCUS", "MAXPM_FORK_OF",
              "MAXPM_MODEL", "MAXPM_QUIET", "MAXPM_APP_BUNDLE", "MAXPM_APP_DIR", "MAXPM_APP_SHELL"):
    os.environ.pop(_name, None)
# Nor the agent session that runs the tests: a command records the session's id and web link from these, and that
# write made a read command (maxpm show) sync the needs-you rows in a Claude Code session with Remote Control only,
# so a test passed there and failed in every other shell (#5581).
from river import agents as _agents  # noqa: E402
_got = _agents.load()
for _name in ["CLAUDE_CODE_BRIDGE_SESSION_ID"] + [(_got[m].identity or {}).get("env") for m in _agents.env_order()]:
    if _name:
        os.environ.pop(_name, None)

core.tmux_path = lambda: None
from river import review as _review  # noqa: E402
_review.COUNT_AGAIN = float("inf")  # the page state starts no git count of the sessions' folders (#5855)
core.LOGIN_PATH_RUNNER = lambda: None  # a test never starts the person's login shell (#5611); a test sets its own
dictation.BUILD = False  # a test never builds the dictation helper into ~/.maximizepm/bin
from river.agents import codex as codex_adapter  # noqa: E402
codex_adapter.CODEX_FLAGS = set()  # not the installed Codex: a test of its flags sets them
server.ENTER_PAUSE = server.COMMAND_LOOK = server.INTERRUPT_PAUSE = 0  # no waits for a CLI's paste window or a command's screen (#5220)
server.DELIVER_RUNNER = lambda agent: None  # no thread types a held message (#5587); a test sets its own
server.STATE_TTL = 0  # each /api/state of a test builds anew; StateCache tests the shared build (#5428)
from river.agents import opencode as opencode_adapter  # noqa: E402
opencode_adapter.MODELS_RUNNER = lambda: ""  # never the installed OpenCode (opencode models): its described models stand
from river import release as _release  # noqa: E402
from river import vault as _vault  # noqa: E402

# The tests run inside an agent session (or a person's shell), so the process tree would name that session: a credential
# check trusts the --as name here (#5823). The tests of the process-tree check (tests.test_vault.Identity) set None.
_vault.IDENTITY = lambda conn, actor: {"name": actor, "person": not actor or core._is_person(conn, actor), "how": "test"}


def _no_network(url, timeout):
    raise OSError("the tests ask GitHub for no release (#5601); a test sets release.FETCH itself")


_release.FETCH = _no_network


def chrome():
    """The Chrome (or Chromium, or Edge) that a test starts, or None, so the test skips. Every test that starts a real
    browser asks here. MAXPM_CHROME names one; MAXPM_NO_BROWSER=1 gives None: the checks of a release on GitHub run
    without the browser tests (mark 2026-11-12), and so does its Intel job (#5583)."""
    import shutil
    if os.environ.get("MAXPM_NO_BROWSER"):
        return None
    for c in (os.environ.get("MAXPM_CHROME"), "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              "/Applications/Chromium.app/Contents/MacOS/Chromium",
              "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
              *(shutil.which(n) for n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"))):
        if c and os.path.exists(c):
            return c
    return None


def proc_after_exec(pid, want, wait=5.0):
    """core.proc_info(pid) once the new child's command line shows `want` (the program it starts) and stays the same
    for 0.2 s, at most wait seconds (#5295). ps right after Popen can find the child before its exec (the test
    runner's command line, or none), and Homebrew's framework Python execs a second time, from bin/python3.14 into
    Python.app/Contents/MacOS/Python, with the same arguments: a pid_cmd read before that made a later check ('PID ...
    now runs another command') fail under the load of the full suite. The last answer when it never settles."""
    import time
    end, last = time.time() + wait, None
    while True:
        info = core.proc_info(pid)
        if info is not None and want in info[2] and last is not None and info[2] == last[2]:
            return info
        if time.time() > end:
            return info
        last = info if info is not None and want in info[2] else None
        time.sleep(0.2)
