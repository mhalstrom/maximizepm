"""The tests never reach the tmux of the person who runs them. Every agent starts in a tmux pane: a test fakes
the pane (a runner, or server.TERMINAL_RUNNER, gets the line that would go into it), the whole of tmux (the fake
server.TMUX_RUNNER), or runs a tmux server of its own (server.TMUX_CMD). With no tmux found (core.tmux_path), a
start refuses; a test of an installed tmux sets core.tmux_path itself.

The tests also run as no session: a session that maxpm serve opened has its name, purpose, and model in
the environment (MAXPM_FOCUS=deploy:<target> makes go look only for a deploy), and a test that runs go or
the MCP server there would read them (#2261). A test that needs one sets it itself.

MAXPM_DB names a queue in a temporary folder before any test runs: a test names its own database, and one that does
not (cli.run of a command that needs no queue, such as agent-login) writes its run log row there, never to the real
queue (#5969).

Many tests take MAXPM_DB out of the environment when they end, so a later core.connect() with no path fell back to
the person's real queue and ran this code's migrations on it: a test run in a worktree dropped a table that main
still read (#6000). So the default queue of a test run is a file in the temporary folder too (core.HOME_DB), and
core.connect refuses the real queue file outright."""
import atexit
import os
import shutil
import socketserver
import tempfile
from pathlib import Path

# #6341: socketserver.serve_forever defaults to poll_interval=0.5s, so every test's httpd.shutdown() waited up to
# 0.5s. Polling at 20ms keeps shutdown near-instant without busy-waiting.
_orig_serve_forever = socketserver.BaseServer.serve_forever


def _fast_serve_forever(self, poll_interval=0.02):
    return _orig_serve_forever(self, poll_interval=poll_interval)


socketserver.BaseServer.serve_forever = _fast_serve_forever

_queue_dir = tempfile.mkdtemp(prefix="maxpm-tests-")
atexit.register(shutil.rmtree, _queue_dir, True)
os.environ["MAXPM_DB"] = os.path.join(_queue_dir, "maxpm.db")
# Codex's folder: an empty one, so no test reads the person's Codex model cache (#6257: its models join the list).
os.environ["CODEX_HOME"] = os.path.join(_queue_dir, "codex")

from river import core, dictation, server  # noqa: E402


# #6430: a pass of maxpm serve reads the agent CLIs' versions (river/agentupdate.py). In a test it finds no agent
# program and asks no registry (a full run asked x.ai and formulae.brew.sh), unless the test sets the fakes itself.
# SYNC=True runs the check in the test's own thread so no background thread writes agent-versions.lock into a
# TemporaryDirectory while tearDown() removes it (#6517).
from river import agentupdate  # noqa: E402


def _no_network(url, timeout):
    raise OSError("the tests ask no registry")


agentupdate.WHICH = lambda exe: None
agentupdate.FETCH = _no_network
agentupdate.SYNC = True

REAL_HOME_DB = core.HOME_DB  # the default of a real run, for the test of the default (DbLocation)
REAL_QUEUE = Path(server._main_db())  # the person's queue, in the real home folder (a test may change HOME)
core.HOME_DB = Path(_queue_dir, "home", "maxpm.db")
_connect = core.connect
USE_TEMPLATE_DB = True
_TEMPLATE_LOCK = __import__("threading").Lock()
_TEMPLATE_DB = Path(_queue_dir, "_template.db")


def _ensure_template():
    if _TEMPLATE_DB.exists():
        return
    c = _connect(_TEMPLATE_DB)
    try:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        c.close()


def _test_connect(path=None):
    p = Path(path) if path else core.db_path()
    if str(path) != ":memory:" and p.expanduser().resolve() == REAL_QUEUE:
        raise AssertionError(f"a test opened the real queue {REAL_QUEUE}; give it a temporary database (#6000)")
    if USE_TEMPLATE_DB and str(path) != ":memory:":
        with _TEMPLATE_LOCK:
            if not p.exists():
                _ensure_template()
                p.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(_TEMPLATE_DB, p)
    conn = _connect(path)
    try:
        conn.execute("PRAGMA synchronous=OFF")
    except Exception:
        pass
    return conn


core.connect = _test_connect

# Nor a session's token (core.TOKEN_ENV): a session that maxpm serve started has one, and every command of a test then
# refused it as a token that the test's queue did not make (#6020). Nor MAXPM_LOCAL (commands skip serve) or
# MAXPM_RELAY_URL (another relay) of the shell that runs the tests.
for _name in ("MAXPM_AGENT", "MAXPM_AGENT_TYPE", "MAXPM_CHAT", "MAXPM_DESKTOP", "MAXPM_FOCUS",
              "MAXPM_MODEL", "MAXPM_QUIET", "MAXPM_APP_BUNDLE", "MAXPM_APP_DIR", "MAXPM_APP_SHELL",
              core.TOKEN_ENV, "MAXPM_LOCAL", "MAXPM_RELAY_URL"):
    os.environ.pop(_name, None)
# Nor the agent session that runs the tests: a command records the session's id and web link from these, and that
# write made a read command (maxpm show) sync the needs-you rows in a Claude Code session with Remote Control only,
# so a test passed there and failed in every other shell (#5581).
from river import agents as _agents  # noqa: E402
_got = _agents.load()
for _name in ["CLAUDE_CODE_BRIDGE_SESSION_ID"] + [(_got[m].identity or {}).get("env") for m in _agents.env_order()]:
    if _name:
        os.environ.pop(_name, None)

from river import agentinstall as _agentinstall  # noqa: E402
# Nor the person's agent programs (#6239): the setup guide also looks in ~/.local/bin and Homebrew's bin, and a test
# of the guide found the Claude Code of the person who ran it there. A test of the search sets its own folders.
_agentinstall.DIRS = []
_agentinstall.STATUS_RUNNER = lambda argv: 1
REAL_TMUX_PATH = core.tmux_path  # for the test of the lookup itself, with a fake app folder (#6237)
core.tmux_path = lambda: None
# Nor the tmux server of the person's sessions (#6246). Tests run in an agent session, in a tmux pane, and there TMUX
# names that server: a tmux command with no -L or -S of its own (in a test, or in a program that a test starts) went
# to it. With TMUX gone and TMUX_TMPDIR in the test folder, such a command starts a server of the test run instead.
os.environ.pop("TMUX", None)
os.environ.pop("TMUX_PANE", None)
os.environ["TMUX_TMPDIR"] = _queue_dir
# The tests never probe the network (#6099): this computer is online for every test, offline or in a sandbox. A test
# of an outage sets core.NET_PROBE itself.
core.NET_PROBE = lambda hosts, dns: None
# The start commands of the tests carry no queue guard (#6412: no session writes the queue file), so the tests of exact
# commands stay as they were; tests.test_agents.QueueGuard turns it on.
core.QUEUE_GUARD = False
from river import cli as _cli  # noqa: E402
# The tests run inside an agent CLI (its variables are in their environment), but a test's command is a person's
# terminal unless the test says otherwise: the permission table (#5974) reads this hook. Its tests set their own.
_cli.INSIDE_AGENT = lambda: False


def person_env(env=None):
    """An environment for a maxpm subprocess that runs as a person's terminal: no agent CLI's own variables, no
    session name or token (a subprocess does not see the hook above)."""
    env = dict(os.environ if env is None else env)
    got = _agents.load()
    inside = {k for m in got for k in ((got[m].identity or {}).get("inside_env") or ())}
    for k in list(env):
        if k in inside or k in ("MAXPM_AGENT", "MAXPM_AGENT_TYPE", "MAXPM_TOKEN", "MAXPM_CHAT"):
            del env[k]
    return env


server.CLAUDE_JSON = os.devnull  # Claude Code's settings of the person who runs the tests say nothing here (#5892)
from river import review as _review  # noqa: E402
_review.COUNT_AGAIN = float("inf")  # the page state starts no git count of the sessions' folders (#5855)
from river.pipeline import targets as _targets  # noqa: E402
_targets.SYNC_HOLD["on"] = True  # holding is read in the test's own thread, never after its database is gone (#6140)
core.LOGIN_PATH_RUNNER = lambda: None  # a test never starts the person's login shell (#5611); a test sets its own
dictation.BUILD = False  # a test never builds the dictation helper into ~/.maximizepm/bin
# A test that starts a pane of a real tmux server makes the session's build folder (#6398): never in ~/Projects.
core.DEFAULT_SETTINGS["session_builds"] = os.path.join(_queue_dir, "session-builds")
from river import tailnet as _tailnet  # noqa: E402
# A test never asks the person's Tailscale (#5860): a phone notice's link went to this Mac's tailnet name when
# tailscale serve forwarded the page here. Tests of Tailscale set their own fake (tests/test_tailnet.py).
_tailnet.RUNNER = lambda args: (127, "")
from river.agents import codex as codex_adapter  # noqa: E402
codex_adapter.CODEX_FLAGS = set()  # not the installed Codex: a test of its flags sets them
server.ENTER_PAUSE = server.COMMAND_LOOK = server.INTERRUPT_PAUSE = 0  # no waits for a CLI's paste window or a command's screen (#5220)
server.DELIVER_RUNNER = lambda agent: None  # no thread types a held message (#5587); a test sets its own
server.PANE_PROCESS_FIND = lambda agent, process: (None, None)  # the pane watcher reads no real ps (#5907)
server.STATE_TTL = 0  # each /api/state of a test builds anew; StateCache tests the shared build (#5428)
from river.agents import opencode as opencode_adapter  # noqa: E402
opencode_adapter.MODELS_RUNNER = lambda: ""  # never the installed OpenCode (opencode models): its described models stand
from river.agents import antigravity as agy_adapter  # noqa: E402
agy_adapter.MODELS.runner = lambda: ""  # never the installed agy (agy models): MODELS_SEEN stands (#6256)
from river.agents import cursor as cursor_adapter  # noqa: E402
cursor_adapter.MODELS.runner = lambda: ""  # never the installed Cursor CLI (cursor-agent models): MODELS_SEEN (#6257)
from river.agents import pi as pi_adapter  # noqa: E402
pi_adapter.MODELS.runner = lambda: ""  # never the installed Pi (pi --list-models): its described models (#6257)
from river import browser as _browser  # noqa: E402
# The tests of a browser window and a passkey window run as on a computer with a screen, also on a Linux runner with no
# DISPLAY (#5878); tests.test_browser.NoScreen sets the other kinds.
_browser.SCREEN_KIND = "screen"
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
              "/Applications/Chromium.app/Contents/Chromium",
              "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
              *(shutil.which(n) for n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"))):
        if c and os.path.exists(c):
            return c
    return None


class _SharedChrome:
    """One headless Chrome kept across page tests (#6341): each test opens an isolated BrowserContext + tab over CDP
    instead of cold-booting a new Chrome process."""

    def __init__(self):
        self.proc = None
        self.ws = None
        self.seq = 0
        self.profile = None

    def _start(self):
        import json
        import subprocess
        import time
        import urllib.request
        exe = chrome()
        if not exe:
            return False
        if self.proc is not None and self.proc.poll() is None and self.ws is not None:
            return True
        self.close()
        self.profile = tempfile.mkdtemp(prefix="maxpm-chrome-", dir=_queue_dir)
        self.proc = subprocess.Popen([
            exe, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
            "--disable-extensions", "--autoplay-policy=no-user-gesture-required",
            "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={self.profile}", "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        port_file = Path(self.profile) / "DevToolsActivePort"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        end = time.monotonic() + 15
        ws_url = None
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                break
            try:
                port = int(port_file.read_text(encoding="utf-8").split()[0])
                with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
                    ws_url = json.loads(r.read())["webSocketDebuggerUrl"]
                    break
            except (OSError, ValueError, KeyError, IndexError):
                time.sleep(0.02)
        if not ws_url:
            self.close()
            raise RuntimeError("shared headless Chrome did not open its debugging port")
        self.ws = _browser.WS(ws_url)
        return True

    def _call(self, method, params=None, session=None):
        import json
        self.seq += 1
        cid = self.seq
        msg = {"id": cid, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self.ws.send(json.dumps(msg))
        while True:
            got = json.loads(self.ws.recv())
            if got.get("id") == cid:
                if "error" in got:
                    raise RuntimeError(f"CDP {method}: {got['error']}")
                return got.get("result", {})

    def close(self):
        ws, self.ws = self.ws, None
        if ws is not None:
            try:
                ws.close()
            except OSError:
                pass
        proc, self.proc = self.proc, None
        if proc is not None:
            try:
                proc.kill()
                proc.wait(5)
            except OSError:
                pass


_SHARED_CHROME = _SharedChrome()
atexit.register(_SHARED_CHROME.close)


def open_tab(url, window=None):
    """Context manager: open url in a fresh isolated BrowserContext + tab of the shared headless Chrome (#6341)."""
    import contextlib

    @contextlib.contextmanager
    def _cm():
        _SHARED_CHROME._start()
        ctx = tid = None
        try:
            ctx = _SHARED_CHROME._call("Target.createBrowserContext")["browserContextId"]
            tid = _SHARED_CHROME._call("Target.createTarget", {"url": "about:blank", "browserContextId": ctx})["targetId"]
            sid = _SHARED_CHROME._call("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
            w, h = (int(x) for x in window.split(",")) if window else (800, 600)
            _SHARED_CHROME._call("Emulation.setDeviceMetricsOverride", {
                "width": w, "height": h, "deviceScaleFactor": 1, "mobile": False,
            }, session=sid)
            _SHARED_CHROME._call("Page.navigate", {"url": url}, session=sid)
            yield
        except Exception:
            if ctx is None:
                _SHARED_CHROME.close()
            raise
        finally:
            try:
                if tid:
                    _SHARED_CHROME._call("Target.closeTarget", {"targetId": tid})
                if ctx:
                    _SHARED_CHROME._call("Target.disposeBrowserContext", {"browserContextId": ctx})
            except Exception:
                _SHARED_CHROME.close()

    return _cm()


_REAL_SLEEP = __import__("time").sleep


def own_sleep(record):
    """A fake time.sleep for mock.patch.object(server.time, "sleep", ...): record(seconds) for the sleeps of the
    test's own thread, a real sleep for every other thread. The patch replaces time.sleep for the whole process: a
    fake that only recorded put the sleeps of a background thread of an earlier test in the test's list, and that
    thread no longer slept, so it filled the list (#6495: ['sleep', 'sleep', 'sleep'] in the full run of release
    v1.5.0)."""
    import threading
    me = threading.get_ident()

    def sleep(seconds):
        if threading.get_ident() == me:
            record(seconds)
        else:
            _REAL_SLEEP(seconds)
    return sleep


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
