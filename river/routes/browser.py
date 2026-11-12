"""The browser of a project in the side panel (#4631; river/browser.py): its frames for the panel (a long poll, with
the page's token), and one page action, op "browser", for the panel and for maxpm browser alike. The person's mouse
and keys come through op "browser_input", which needs the page's token (server.TYPING_OPS). A session's Playwright MCP
reaches the browser through its own CDP address, GET /cdp/<agent> (#5405)."""

import hmac
import threading
import time

from .. import browser, core, server  # noqa: F401  (server: read at call time, so tests that replace its globals work)
from ..core import RiverError

# Requests to show a browser in a session's side panel (maxpm browser open): the state carries them, as with
# canvases (server.CANVAS_OPENS). In memory only, for BROWSER_OPEN_KEEP seconds.
BROWSER_OPENS = []
BROWSER_OPEN_KEEP = 120
_open_lock = threading.Lock()
_open_seq = [0]


def browser_opens():
    with _open_lock:
        cut = time.time() - BROWSER_OPEN_KEEP
        BROWSER_OPENS[:] = [x for x in BROWSER_OPENS if x["at"] >= cut][-50:]
        return [{k: v for k, v in x.items() if k != "at"} for x in BROWSER_OPENS]


def _ask_open(project, session, by):
    with _open_lock:
        _open_seq[0] += 1
        BROWSER_OPENS.append({"seq": _open_seq[0], "project": project, "session": session, "by": by, "at": time.time()})


# The sessions whose side panel was asked to show the browser at their first input (#5616): (project, session).
SHOWN = set()


def first_input(project, session):
    """A session's first click or key in a project's browser opens the browser in its side panel, once; the person
    closes it and it stays closed."""
    if session and (project, session) not in SHOWN:
        SHOWN.add((project, session))
        _ask_open(project, session, session)


def frame(h, path):
    """GET /api/browser/frame?project=<name>&after=<seq>&token=<page token>: the newest frame after that seq, or the
    status alone after a few seconds with nothing new."""
    from urllib.parse import parse_qs, urlsplit
    q = {k: v[0] for k, v in parse_qs(urlsplit(h.path).query).items()}
    if not hmac.compare_digest(q.get("token", ""), server.PAGE_TOKEN):
        return h._send(403, {"error": "this needs the page's token: reload the page"})
    project = q.get("project", "")
    b = browser.BROWSERS.get(project)
    if b is None:
        return h._send(200, {"project": project, "running": False, "seq": 0, "tabs": [], "controller": None})
    try:
        return h._send(200, browser.frame(b, int(q.get("after") or 0), min(float(q.get("wait") or 8), 20)))
    except (RiverError, ValueError) as e:
        return h._send(409, {"error": str(e)})


def _person(conn, who):
    if not who:
        return True  # the page with no "You are" chosen: a person at this computer
    r = conn.execute("SELECT kind FROM agents WHERE name=?", (who,)).fetchone()
    return bool(r and r["kind"] == "human")


# action -> whether it changes the page (the controller rule holds for it)
CHANGES = {"open", "click", "type", "key", "back", "forward", "reload", "eval", "tab", "new_tab", "close_tab"}


def act(conn, a, who):
    """One browser action. a: action, and project, or agent (its item's project), or cwd (the project of the folder);
    then url, selector, x, y, text, key, js, full, sub, target, w, h, window as the action needs."""
    action = a.get("action") or "status"
    project = browser.project_of(conn, a.get("project"), a.get("agent") or (None if _person(conn, who) else who), a.get("cwd"))
    b = browser.get(project)
    person = _person(conn, who)
    if action in CHANGES:
        browser.may_change(conn, b, who, person)
        if not person and action in ("click", "type", "key"):
            first_input(project, who)
    if action == "status":
        return b.info()
    if action == "walk":  # a page walk check (#5516): its own background tab, so it changes nothing the person sees
        pages = a.get("pages") or []
        if not isinstance(pages, list) or not pages:
            raise RiverError("a page walk needs pages: a list of addresses")
        return browser.walk(b, [browser.norm_url(u) for u in pages], float(a.get("settle") or 3),
                            float(a.get("timeout") or 60))
    if action == "start":  # the panel shows the browser: start it, and say so
        b.start()
        return b.info()
    if action == "open":
        out = b.open(a.get("url") or "")
        if not person and a.get("show", True):
            _ask_open(project, who, who)
        return out
    if action == "click":
        x, y = a.get("x"), a.get("y")
        return browser.click(b, a.get("selector"), None if x is None else float(x), None if y is None else float(y),
                             who=None if person else who)
    if action == "type":
        return browser.type_text(b, a.get("text") or "", a.get("selector"), who=None if person else who)
    if action == "key":
        return browser.press(b, a.get("key") or "", who=None if person else who)
    if action in ("back", "forward"):
        return browser.history(b, -1 if action == "back" else 1)
    if action == "reload":
        return browser.reload(b)
    if action == "eval":
        return {**b.info(), "value": b.evaluate(a.get("js") or "")}
    if action == "html":
        return {**b.info(), "html": b.evaluate("document.documentElement.outerHTML")}
    if action == "text":
        return {**b.info(), "text": b.evaluate("document.body ? document.body.innerText : ''")}
    if action == "screenshot":
        return browser.screenshot(b, bool(a.get("full")), bool(a.get("cursor")))
    if action == "tab":
        return browser.switch_tab(b, a.get("target") or "")
    if action == "new_tab":
        return browser.new_tab(b, a.get("url") or "about:blank")
    if action == "close_tab":
        return browser.close_tab(b, a.get("target"))
    if action == "control":
        if person:
            return browser.take(conn, b, who)
        return browser.control(conn, b, who, a.get("sub"))
    if action == "give":
        if person:
            raise RiverError("a person gives the browser back with Give back (action give_back)")
        if not (a.get("sub") or "").strip():
            raise RiverError("name the subagent: maxpm browser give <label>")
        return browser.control(conn, b, who, a.get("sub"))
    if action == "release":
        return browser.release(conn, b, who)
    if action == "give_back":
        return browser.give_back(conn, b, who)
    if action == "resize":
        b.resize(a.get("w") or 0, a.get("h") or 0)
        return b.info()
    if action == "window":  # decision 3: a normal window for a site that refuses a headless browser, and back
        url = b.info()["url"] if b.running() else ""
        window = bool(a.get("window", True))
        if b._attached_now():  # a Chrome that serve did not start: its browser_mode, under the lock (#5619)
            browser.attached_mode(conn, b, who, window)
        else:
            b.start(window=window)
        if url and url != "about:blank":
            b.open(url)
        return b.info()
    if action == "close":
        if not person:
            c = b.state.get("controller")
            if c and c["who"] != who and not browser.controller_gone(conn, c):
                raise RiverError(f"the browser of {project} is controlled by {c['who']}; maxpm browser status")
        b.close()
        return b.info()
    raise RiverError(f"unknown browser action {action!r}")


def person_input(conn, a, who):
    """The person's mouse and keys in the panel (needs the page's token)."""
    b = browser.BROWSERS.get(a.get("project") or "")
    if b is None or not b.running():
        raise RiverError("the browser is not running: open a page first")
    browser.may_change(conn, b, who, True)
    out = None
    for ev in a.get("evs") or [a.get("ev") or {}]:  # the page sends what came during its last request at once (#5479)
        out = browser.person_input(b, ev or {})
    return out


HAND_TEXT = "The browser is at {title} ({url}). Take it from here."


def hand(conn, a, who):
    """Hand to <session> in the panel (#5616): the person gives the browser to the session whose side panel shows it,
    and its chat gets one message with the page, so it goes on from there (the same tab, the person's sign-in). A
    typing op: it types into the session's chat, so it needs the page's token."""
    project, to = a.get("project") or "", (a.get("to") or "").strip()
    if not to:
        raise RiverError("name the session to hand the browser to")
    r = conn.execute("SELECT kind FROM agents WHERE name=?", (to,)).fetchone()
    if not r or r["kind"] != "ai":
        raise RiverError(f"no session {to}: pick a session that runs now")
    if not _person(conn, who):
        raise RiverError("only a person hands the browser on; a session takes it with maxpm browser control")
    b = browser.BROWSERS.get(project)
    if b is None or not b.running():
        raise RiverError("the browser is not running: open a page first")
    server._typing_pane(conn, to, who)  # the chat can take the message: else the control stays with the person
    info = browser.hand(conn, b, who, to)
    text = HAND_TEXT.format(title=info.get("title") or "a page with no title", url=info.get("url") or "about:blank")
    try:
        server.session_send(conn, to, text, who=who, hold=True)
    except RiverError as e:
        raise RiverError(f"{to} controls the browser now, but its chat did not get the message: {e}")
    SHOWN.add((project, to))
    return {**info, "told": to, "text": text}


def cdp(h, path):
    """GET /cdp/<agent>?project=<name>&token=<t>, a WebSocket: one session's CDP connection to its project's browser
    (#5405). serve opens a CDP connection to Chrome for it and relays each message after browser.cdp_refusal. The
    token comes from the session's MCP config (browser.mcp_config_file); the real debugging port never leaves serve."""
    from urllib.parse import parse_qs, unquote, urlsplit
    u = urlsplit(h.path)
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    agent, project = unquote(u.path[len("/cdp/"):]), q.get("project", "")
    if not agent or not project or not hmac.compare_digest(q.get("token", ""), browser.cdp_token(project, agent)):
        return h._send(403, {"error": "this CDP address needs the token from the session's MCP config "
                                      "(setting browser_mcp; maxpm starts the session)"})
    key = h.headers.get("Sec-WebSocket-Key")
    if (h.headers.get("Upgrade") or "").lower() != "websocket" or not key:
        return h._send(400, {"error": "a CDP address takes a WebSocket (Playwright: --cdp-endpoint)"})
    conn = core.connect()
    try:
        if not conn.execute("SELECT 1 FROM projects WHERE name=?", (project,)).fetchone():
            return h._send(404, {"error": f"no project {project}"})
        b = browser.get(project)
        if b._attached_now():
            return h._send(409, {"error": f"the browser of {project} is a Chrome that serve did not start "
                                          f"(browser_source): sessions use maxpm browser for it, not a CDP address"})
        try:
            b.start()
            upstream = browser.WS(b._version(b.port)["webSocketDebuggerUrl"])
        except (RiverError, OSError, ValueError, KeyError) as e:
            return h._send(409, {"error": f"the browser of {project} did not start: {e}"})
        h.send_response_only(101, "Switching Protocols")
        h.send_header("Upgrade", "websocket")
        h.send_header("Connection", "Upgrade")
        h.send_header("Sec-WebSocket-Accept", browser.ws_accept(key))
        h.end_headers()
        h.wfile.flush()
        h.close_connection = True
        browser.cdp_session(conn, b, agent, h.rfile, h.connection, upstream, on_input=lambda: first_input(project, agent))
    finally:
        conn.close()


GET = [
    (lambda path: path == "/api/browser/frame", frame),
    (lambda path: path.startswith("/cdp/"), cdp),
]

OPS = {
    "browser": act,
    "browser_input": person_input,
    "browser_hand": hand,
}
