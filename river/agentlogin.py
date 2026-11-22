"""maxpm agent-login claude|codex: the sign-in of an agent CLI on a computer with no browser (#5877), such as a host
that maxpm host setup made (#5876).

Each CLI has a sign-in for a machine without a browser: it prints a link, the person opens it on the laptop or the
phone and signs in there. Claude Code (claude auth login) then shows a code that goes back into the CLI; Codex
(codex login --device-auth) shows a code to type on the page of the link, and its CLI waits until that is done.
Both must keep running while the person signs in, so they run in a window of the maxpm tmux session (detached), and
this module reads the link and the code from its screen. policy p30: the person does only the browser step, once;
no script with interactive steps. Nothing here writes into a CLI's home (p140): each CLI keeps its own sign-in.

Not measured on a real host yet: the link and the code are read with patterns that fit both CLIs' texts (an https
link; a code such as ABCD-12345)."""

from __future__ import annotations

import re
import shutil
import subprocess
import time

from .core import RiverError

AGENTS = {
    "claude": {"name": "Claude Code", "status": ["claude", "auth", "status"], "login": "claude auth login",
               "paste": True},
    "codex": {"name": "Codex", "status": ["codex", "login", "status"], "login": "codex login --device-auth",
              "paste": False},
    # Antigravity CLI (#6248) has no sign-in command: agy itself asks at its start, first for the method ("Select
    # login method: 1. Google OAuth 2. Use a Google Cloud project"; choose: Enter takes the first), then, with no
    # browser on the computer, it prints the link and waits for the code that the page shows (antigravity.google
    # /docs/install, 'Over SSH'). agy models exits 0 only when it is signed in.
    "agy": {"name": "Antigravity CLI", "status": ["agy", "models"], "login": "agy", "paste": True,
            "choose": (r"Select login method", "Enter")},
    # Hermes Agent (#6681): `hermes setup --portal` runs Nous Portal's OAuth device-code flow (prints the link and
    # user code, polls until complete: paste False). `hermes status` exits 0 and prints 'Providers: none connected'
    # when no provider is authenticated.
    "hermes": {"name": "Hermes", "status": ["hermes", "status"], "status_not": r"Providers:\s*none connected",
               "login": "hermes setup --portal", "paste": False},
}
STATUS_RUNNER = None  # tests: fn(argv) -> exit code (0: signed in)
WAIT = 20.0  # seconds that start waits for the link on the window's screen
DONE_WAIT = 20.0  # seconds that a pasted code waits for the CLI to say it is signed in
POLL = 0.5
_LINK = re.compile(r"https://[^\s\"'<>]+")
_CODE = re.compile(r"\b[A-Z0-9]{4,}-[A-Z0-9]{4,}\b")


def _agent(agent):
    if agent not in AGENTS:
        raise RiverError(f"no sign-in for {agent!r}; the agents: {', '.join(AGENTS)}")
    return AGENTS[agent]


def _window(agent):
    from . import server
    return f"={server.TMUX_SESSION}:signin-{agent}"


def signed_in(agent):
    """True or False, or None when the CLI is not on this computer."""
    argv = _agent(agent)["status"]
    if STATUS_RUNNER is not None:
        return STATUS_RUNNER(argv) == 0
    if not shutil.which(argv[0]):
        return None
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
        bad = _agent(agent).get("status_not")
        return r.returncode == 0 and (not bad or not re.search(bad, (r.stdout or "") + (r.stderr or "")))
    except (OSError, subprocess.SubprocessError):
        return False


def _screen(agent):
    from . import server
    return server._tmux("capture-pane", "-p", "-J", "-t", _window(agent), check=False) or ""


def read_screen(text):
    """The link and the code that a sign-in shows, from its screen: ({url}, {code}) or None for each."""
    links = _LINK.findall(text or "")
    codes = _CODE.findall(text or "")
    return (links[-1].rstrip(".,)") if links else None), (codes[-1] if codes else None)


def start(agent):
    """Run the CLI's sign-in in a detached tmux window and return its link (and code). A signed-in CLI: nothing to do."""
    from . import server
    a = _agent(agent)
    now = signed_in(agent)
    if now is None:
        raise RiverError(f"{a['status'][0]} is not on this computer: install {a['name']} first")
    if now:
        return {"agent": agent, "signed_in": True}
    server._tmux("kill-window", "-t", _window(agent), check=False)  # an older sign-in that waits: start again
    if server._tmux("has-session", "-t", "=" + server.TMUX_SESSION, check=False) is None:
        server._tmux("new-session", "-d", "-s", server.TMUX_SESSION, "-n", f"signin-{agent}", "-x", "200", "-y", "50",
                     a["login"])
    else:
        server._tmux("new-window", "-d", "-t", f"={server.TMUX_SESSION}:", "-n", f"signin-{agent}", a["login"])
    end = time.monotonic() + WAIT
    url = code = None
    text = ""
    chose = not a.get("choose")
    while time.monotonic() < end:
        text = _screen(agent)
        if not chose and re.search(a["choose"][0], text):  # a menu before the link: its first option, once
            server._tmux("send-keys", "-t", _window(agent), a["choose"][1])
            chose = True
            continue
        url, code = read_screen(text)
        if url and (a["paste"] or code):
            break
        time.sleep(POLL)
    if not url:
        last = " ".join(text.split())[-300:]
        raise RiverError(f"{a['name']} showed no sign-in link in {int(WAIT)} seconds"
                         + (f" (its screen: {last})" if last else "")
                         + f"; look at it: {server.tmux_line()} attach -t {server.TMUX_SESSION}, window signin-{agent}")
    res = {"agent": agent, "signed_in": False, "url": url, "paste": a["paste"]}
    if a["paste"]:
        res["next"] = f"open the link, sign in, and give the code it shows: maxpm agent-login {agent} --code <code>"
    else:
        res["code"] = code
        res["next"] = (f"open the link, sign in, and type the code {code}; {a['name']} finishes by itself "
                       f"(maxpm agent-login {agent} says when)")
    return res


def paste(agent, code):
    """The code from the browser into the waiting Claude Code sign-in, then wait until it says it is signed in."""
    from . import server
    a = _agent(agent)
    if not a["paste"]:
        raise RiverError(f"{a['name']} takes no code here: type the code on the page of its link")
    code = (code or "").strip()
    if not code or any(c.isspace() for c in code):
        raise RiverError("give the code that the sign-in page shows, as one word")
    if server._tmux("list-panes", "-t", _window(agent), check=False) is None:
        raise RiverError(f"no {a['name']} sign-in waits for a code: run maxpm agent-login {agent} first")
    server._tmux("send-keys", "-t", _window(agent), "-l", code)
    server._tmux("send-keys", "-t", _window(agent), "Enter")
    end = time.monotonic() + DONE_WAIT
    while time.monotonic() < end:
        if signed_in(agent):
            return {"agent": agent, "signed_in": True}
        time.sleep(POLL)
    last = " ".join(_screen(agent).split())[-300:]
    raise RiverError(f"{a['name']} is not signed in {int(DONE_WAIT)} seconds after the code"
                     + (f" (its screen: {last})" if last else "") + f"; start again: maxpm agent-login {agent}")


def render(res):
    from . import tailnet
    name = AGENTS[res["agent"]]["name"]
    if res.get("signed_in"):
        return f"{name} is signed in here."
    try:  # a sign-in link with its OAuth words can be longer than a QR code here takes (about 200 bytes)
        qr = tailnet.qr_text(res["url"]) + "\n"
    except ValueError:
        qr = ""
    return (f"{name} waits for its sign-in. Open this link on your laptop or phone:\n  {res['url']}\n"
            + qr + f"Then {res['next']}.")
