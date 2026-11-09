"""The Terminal tab (#4803): plain shells on this computer in the page, so a person can run a command (for example
npx wrangler login) from another computer or a phone through the relay. (#4656: each view has its routes here;
server.py keeps the frame.)

Each shell is a pane of its own tmux session, SHELL_SESSION (apart from the agents' panes): the person's login
shell in the home folder. The page shows it and types into it as it does for an agent's terminal (_pane_screen,
send_keys; the keys need the page's token). Rules:
- only a person opens, types into or closes a shell, never an agent;
- the setting web_terminal (on) allows it on the page of this computer; web_terminal_relay (off) is a second
  switch for the page shown through the relay, because whoever holds the relay sign-in could then run any
  command here;
- each shell that opens or closes is a line in the history (who, and @relay when it came through the relay).

GET: (test, handler) pairs that Handler.do_GET tries in order; a handler answers with h._send. OPS: page actions,
merged into server.OPS."""

import os
import re
import time

from .. import core, relay, server  # noqa: F401  (server: read at call time, so tests can replace its globals)
from ..core import RiverError

SHELL_SESSION = "maxpm-shell"
PANE = re.compile(r"^%\d+$")
# What the page says about a sign-in that calls back to this computer (wrangler login waits on localhost:8976).
CALLBACK_NOTE = ("A sign-in that opens a browser and waits for it on localhost (npx wrangler login, gcloud auth "
                 "login, gh auth login --web) finishes only in a browser on this computer. From afar, use the tool's "
                 "token sign-in: for wrangler, make an API token on dash.cloudflare.com and run "
                 "export CLOUDFLARE_API_TOKEN=<token>; for gh, gh auth login --with-token.")


def allowed(conn, who, via_relay):
    """Refuses unless a person asks and the settings allow shells where the page is shown."""
    row = conn.execute("SELECT kind FROM agents WHERE name=?", (who,)).fetchone() if who else None
    if not row or row["kind"] != "human":
        raise RiverError("only a person opens and uses a terminal: choose your name in 'You are' (a person)")
    if core.setting(conn, "web_terminal") != "on":
        raise RiverError("the Terminal tab is off on this computer: maxpm config set web_terminal on")
    if via_relay and core.setting(conn, "web_terminal_relay") != "on":
        raise RiverError("the Terminal tab is off through the relay: whoever holds your relay sign-in could run any "
                         "command on your Mac. To allow it, on the Mac: maxpm config set web_terminal_relay on")


def shells():
    """The open shells, oldest first: pane, name, who opened it, when, the command that runs, and the folder."""
    if server._tmux("has-session", "-t", "=" + SHELL_SESSION, check=False) is None:
        return []
    out = server._tmux("list-panes", "-s", "-t", "=" + SHELL_SESSION, "-F",
                       "#{pane_id}|#{@maxpm_name}|#{@maxpm_by}|#{@maxpm_started}|#{pane_current_command}|"
                       "#{pane_current_path}", check=False)
    rows = []
    for line in (out or "").splitlines():
        f = line.split("|", 5)
        if len(f) == 6 and PANE.match(f[0]):
            rows.append({"pane": f[0], "name": f[1] or f[0], "by": f[2] or None,
                         "started": int(f[3]) if f[3].isdigit() else None, "running": f[4].lstrip("-"),
                         "path": f[5].replace(os.path.expanduser("~"), "~", 1)})
    return sorted(rows, key=lambda r: (r["started"] or 0, r["pane"]))


def _pane(pane):
    """A pane of a shell, or RiverError: the page never reaches an agent's pane through these routes."""
    if not isinstance(pane, str) or not PANE.match(pane) or pane not in {s["pane"] for s in shells()}:
        raise RiverError(f"no open terminal {pane}: open one in the Terminal tab")
    return pane


def shell_open(conn, args, who):
    allowed(conn, who, relay_now())
    n = len(shells()) + 1
    name = f"shell {n}"
    new = ["-c", os.path.expanduser("~"), "-P", "-F", "#{pane_id}"]
    if server._tmux("has-session", "-t", "=" + SHELL_SESSION, check=False) is None:
        pane = server._tmux("new-session", "-d", "-s", SHELL_SESSION, "-n", name, "-x", "120", "-y", "36", *new)
    else:
        pane = server._tmux("new-window", "-d", "-t", f"={SHELL_SESSION}:", "-n", name, *new)
    for k, v in (("@maxpm_name", name), ("@maxpm_by", who), ("@maxpm_started", str(int(time.time())))):
        server._tmux("set-option", "-p", "-t", pane, k, v)
    with core.tx(conn):
        core._event(conn, None, who, f"opened a terminal on this computer ({name}, pane {pane})")
    return {"pane": pane, "name": name, "note": CALLBACK_NOTE}


def shell_keys(conn, args, who):
    allowed(conn, who, relay_now())
    pane = _pane(args.get("pane"))
    return {"pane": pane, "sent": server.send_keys(pane, args.get("keys"))}


def shell_close(conn, args, who):
    allowed(conn, who, relay_now())
    pane = _pane(args.get("pane"))
    server._tmux("kill-pane", "-t", pane)
    with core.tx(conn):
        core._event(conn, None, who, f"closed a terminal on this computer (pane {pane})")
    return {"closed": pane}


def relay_now():
    """Whether the action came through the relay (do_POST sets EVENT_VIA for it)."""
    return core.EVENT_VIA.get() == "relay"


def screen(h, path):
    """GET /api/shell: the open shells and the settings; with ?pane=, what that shell shows now."""
    from urllib.parse import parse_qs, urlsplit
    q = {k: v[0] for k, v in parse_qs(urlsplit(h.path).query).items()}
    via = relay.page_request(h.headers)
    conn = core.connect()
    try:
        on, through = core.setting(conn, "web_terminal") == "on", core.setting(conn, "web_terminal_relay") == "on"
        if not on or (via and not through):
            return h._send(403, {"error": "the Terminal tab is off " + ("through the relay" if on else "on this computer"),
                                 "web_terminal": on, "web_terminal_relay": through, "via_relay": via})
        if q.get("pane"):
            return h._send(200, server._pane_screen(None, _pane(q["pane"])))
        try:
            rows = shells()
            tmux = True
        except RiverError:
            rows, tmux = [], False
        return h._send(200, {"shells": rows, "tmux": tmux, "via_relay": via, "web_terminal_relay": through,
                             "note": CALLBACK_NOTE})
    except RiverError as e:
        return h._send(404, {"error": str(e)})
    finally:
        conn.close()


GET = [
    (lambda path: path == "/api/shell", screen),
]

OPS = {
    "shell_open": shell_open,
    "shell_keys": shell_keys,
    "shell_close": shell_close,
}
