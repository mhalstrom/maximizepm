"""The Sessions tab: the running sessions, a session's chat, settings and items, files sent to it, the side panel's
files and frame check, full screen, and the actions of the message box and its dialogs. (#4656: each view has its
routes here; server.py keeps the frame.)

GET: (test, handler) pairs that Handler.do_GET tries in order; a handler answers with h._send. OPS: page actions,
merged into server.OPS."""

from .. import core, server  # noqa: F401  (server: read at call time, so tests that replace its globals still work)
from ..core import RiverError  # noqa: F401
import hmac  # noqa: F401


def upload(h, path):
    """A file sent to a session."""
    got = server.upload_file(path[len("/api/upload/"):])
    if not got:
        return h._send(404, {"error": "not found"})
    headers = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": server.FILE_CSP}
    if got[1] == "application/octet-stream":
        headers["Content-Disposition"] = "attachment"
    return h._send(200, got[0], got[1], headers)

def file(h, path):
    """The side panel of the Sessions tab (#4619)."""
    from urllib.parse import parse_qs, urlsplit
    q = {k: v[0] for k, v in parse_qs(urlsplit(h.path).query).items()}
    conn = core.connect()
    try:
        # The page's token, or a full-screen key for this very file (#4620).
        if not (hmac.compare_digest(q.get("token", ""), server.PAGE_TOKEN)
                or (path == "/api/file" and server.view_ok(conn, q.get("path", ""), q.get("e"), q.get("k")))):
            return h._send(403, {"error": "this needs the page's token: reload the page"})
        if path == "/api/frame-check":
            return h._send(200, server.frame_check(q.get("url", "")))
        got = server.local_file(conn, q.get("path", ""))
    except RiverError as e:
        return h._send(404, {"error": str(e)})
    finally:
        conn.close()
    return h._send(200, got[0], got[1], {"Content-Security-Policy": server.FILE_CSP, "X-Content-Type-Options": "nosniff"})

def file_check(h, path):
    """#4689: the paths a session's text names, checked for the side panel (with the page's token)."""
    from urllib.parse import parse_qs, urlsplit
    q = parse_qs(urlsplit(h.path).query)
    if not hmac.compare_digest(q.get("token", [""])[0], server.PAGE_TOKEN):
        return h._send(403, {"error": "this needs the page's token: reload the page"})
    conn = core.connect()
    try:
        return h._send(200, {"paths": server.file_check(conn, q.get("path", []), q.get("agent", [""])[0] or None)})
    finally:
        conn.close()

def session_settings(h, path):
    """The Settings dialog of a session (#4625)."""
    from urllib.parse import parse_qs, urlsplit
    from .. import sessions
    conn = core.connect()
    try:
        return h._send(200, sessions.session_settings(conn, parse_qs(urlsplit(h.path).query).get("agent", [""])[0]))
    except RiverError as e:
        return h._send(409, {"error": str(e)})
    finally:
        conn.close()

def session_items(h, path):
    """The Items view of a session (#4623)."""
    from urllib.parse import parse_qs, urlsplit
    from .. import sessions
    conn = core.connect()
    try:
        agent = parse_qs(urlsplit(h.path).query).get("agent", [""])[0]
        return h._send(200, {"agent": agent, "items": sessions.session_items(conn, agent)})
    finally:
        conn.close()

def full_screen(h, path):
    """Full screen in a tab of its own (#4620)."""
    from urllib.parse import parse_qs, urlsplit
    q = {k: v[0] for k, v in parse_qs(urlsplit(h.path).query).items()}
    conn = core.connect()
    try:
        if path == "/view":
            code, page = server.view_page(conn, q)
            return h._send(code, page.encode(), "text/html; charset=utf-8")
        if not hmac.compare_digest(q.get("token", ""), server.PAGE_TOKEN):
            return h._send(403, {"error": "this needs the page's token: reload the page"})
        return h._send(200, {"url": server.view_url(conn, q.get("target", ""), q.get("session", ""), q.get("item", ""))})
    except RiverError as e:
        return h._send(409, {"error": str(e)})
    finally:
        conn.close()

def running(h, path):
    """The Sessions tab: running sessions, read only (#4592)."""
    from urllib.parse import parse_qs, urlsplit
    from .. import sessions
    q = parse_qs(urlsplit(h.path).query)
    conn = core.connect()
    try:
        terminals = server.agent_terminals(conn)
        if path == "/api/sessions":
            try:
                ended = {p["pane"] for p in server._tmux_panes(everywhere=True) if p["ended"]} if terminals else set()
            except RiverError:
                ended = set()
            return h._send(200, {"sessions": sessions.views(conn, terminals, prompts=server.pane_prompts(conn, terminals),
                                                               ended_panes=ended)})
        cursor, agent = q.get("cursor", [""])[0], q.get("agent", [""])[0]
        return h._send(200, dict(sessions.view(conn, agent, int(cursor) if cursor.isdigit() else None),
                                    terminal=bool(terminals.get(agent))))
    except RiverError as e:
        return h._send(409, {"error": str(e)})
    finally:
        conn.close()


GET = [
    (lambda path: path.startswith("/api/upload/"), upload),
    (lambda path: path in ("/api/file", "/api/frame-check"), file),
    (lambda path: path == "/api/file-check", file_check),
    (lambda path: path == "/api/session-settings", session_settings),
    (lambda path: path == "/api/session-items", session_items),
    (lambda path: path in ("/view", "/api/view-key"), full_screen),
    (lambda path: path in ("/api/sessions", "/api/session"), running),
]

OPS = {
    "session_send": lambda c, a, who: server.session_send(c, a["agent"], a.get("text", ""), who, a.get("images"),
                                                          a.get("files")),
    "session_setting": lambda c, a, who: server.session_setting_live(c, a["agent"], a["key"], a.get("value"), who),
    "session_restart": lambda c, a, who: server.session_restart(c, a["agent"], a.get("changes"),
                                                                bool(a.get("make_default")), who),
    "prompt_answer": lambda c, a, who: server.prompt_answer(c, a["agent"], a.get("sig", ""), a.get("pick"),
                                                            a.get("other"), who),
}
