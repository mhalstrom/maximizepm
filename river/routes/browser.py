"""The browser of a project in the side panel (#4631; river/browser.py): its frames for the panel (a long poll, with
the page's token), and one page action, op "browser", for the panel and for maxpm browser alike. The person's mouse
and keys come through op "browser_input", which needs the page's token (server.TYPING_OPS)."""

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
    if action == "status":
        return b.info()
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
        return browser.click(b, a.get("selector"), None if x is None else float(x), None if y is None else float(y))
    if action == "type":
        return browser.type_text(b, a.get("text") or "", a.get("selector"))
    if action == "key":
        return browser.press(b, a.get("key") or "")
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
        return browser.screenshot(b, bool(a.get("full")))
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
        b.start(window=bool(a.get("window", True)))
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
    return browser.person_input(b, a.get("ev") or {})


GET = [
    (lambda path: path == "/api/browser/frame", frame),
]

OPS = {
    "browser": act,
    "browser_input": person_input,
}
