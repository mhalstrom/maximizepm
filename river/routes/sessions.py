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

def session_prompts(h, path):
    """The prompts sent to a session from the message box, newest first, for its Up arrow (#4855)."""
    from urllib.parse import parse_qs, urlsplit
    from .. import sessions
    conn = core.connect()
    try:
        agent = parse_qs(urlsplit(h.path).query).get("agent", [""])[0]
        return h._send(200, {"agent": agent, "prompts": sessions.sent_prompts(conn, agent)})
    finally:
        conn.close()

def session_commands(h, path):
    """The slash commands of a session, for the "/" list of its message box (#5186)."""
    from urllib.parse import parse_qs, urlsplit
    from .. import sessions
    conn = core.connect()
    try:
        return h._send(200, sessions.session_commands(conn, parse_qs(urlsplit(h.path).query).get("agent", [""])[0]))
    finally:
        conn.close()

def session_usage(h, path):
    """The Usage panel of a session (#5224): its context, tokens, calls and plan limits."""
    from urllib.parse import parse_qs, urlsplit
    from .. import sessions
    conn = core.connect()
    try:
        return h._send(200, sessions.session_usage(conn, parse_qs(urlsplit(h.path).query).get("agent", [""])[0]))
    except RiverError as e:
        return h._send(409, {"error": str(e)})
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

def cards(conn):
    """The cards of every running session (sessions.views), as GET /api/sessions gives them: the agents' statuses,
    the settings and the tmux pane list read once, as in a page state build (#5597). It writes nothing. Without it,
    the 834 agents cost 3,600 queries and 50 ms of the interpreter every 3 s."""
    from .. import sessions
    with server.read_memo():
        terminals = server.agent_terminals(conn)
        try:
            ended = {p["pane"] for p in server._tmux_panes(everywhere=True) if p["ended"]} if terminals else set()
        except RiverError:
            ended = set()
        rows = sessions.views(conn, terminals, prompts=server.pane_prompts(conn, terminals), ended_panes=ended)
        for r in rows:  # its first Stop waits for a second one (#5563): the page keeps Stop while it is idle
            does = server.stop_waits(conn, r["name"]) if r["name"] in server.STOPPED else None
            if does:
                r["stop_again"] = does
    return rows


# The status of each session at the last pass of status_push: {agent: status}. None until the first pass, which only
# records: a restart of maxpm serve sends no notice for the states that were there before it.
STATUS_LAST = {"by": None}
STATUS_WORDS = {"needs_you": "needs you", "done": "is done", "working": "works", "idle": "is idle"}


def status_push(conn):
    """A push notice for each session that entered a state of notify_status since the last pass (#5852; the notify
    loop of maxpm serve, every notify_interval). The setting of the session's project wins over the global one. A
    session that is new since the last pass sends nothing. needs_you sends nothing while a manager is active (it
    sees prompts and questions first), nor for a session with an open needs-you notice of the queue (its question
    or prompt alert already went out). Several changes in one pass go out as one notice. Returns the changes sent."""
    if core.setting(conn, "notify_status") == "off" and not conn.execute(
            "SELECT 1 FROM settings WHERE key='notify_status' AND value!='off' LIMIT 1").fetchone():
        STATUS_LAST["by"] = None  # off everywhere: no pass, and the next pass after it is set only records
        return []
    if not conn.execute("SELECT 1 FROM settings WHERE key='notify_channels' AND value!='' LIMIT 1").fetchone():
        return []  # no channel for the queue or any person (notify.direct)
    rows = cards(conn)
    before, now_by = STATUS_LAST["by"], {r["name"]: r["status"] for r in rows}
    STATUS_LAST["by"] = now_by
    if before is None:
        return []
    pids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM projects")}
    manager = core.active_manager(conn)
    sent = []
    for r in rows:
        old, new = before.get(r["name"]), r["status"]
        if old is None or old == new:
            continue
        want = core._channels(core.setting(conn, "notify_status", project_id=pids.get(r.get("project"))))
        if new not in want:
            continue
        if new == "needs_you" and (manager or conn.execute(
                "SELECT 1 FROM needs_you n JOIN messages m ON m.id=n.message_id WHERE n.closed_at IS NULL AND "
                "m.from_agent=? LIMIT 1", (r["name"],)).fetchone()):
            continue
        sent.append(r)
    if not sent:
        return []
    from .. import notify
    lines = [f"{r['title']} {STATUS_WORDS[r['status']]}" + (f" ({r['project']})" if r.get("project") else "")
             + (f": {r['doing']}" if r.get("doing") else "") for r in sent]
    if len(sent) == 1:
        title, body = f"MaximizePM: {sent[0]['title']} {STATUS_WORDS[sent[0]['status']]}", lines[0]
        url = notify.page_url(conn, terminal=sent[0]["name"])
    else:
        title, body = f"MaximizePM: {len(sent)} sessions changed", "\n".join(lines[:10])
        url = notify.page_url(conn) + "status"  # the status inbox of the page
    notify.direct(conn, title, body, url)
    return [{"agent": r["name"], "status": r["status"]} for r in sent]


def running(h, path):
    """The Sessions tab: running sessions, read only (#4592)."""
    from urllib.parse import parse_qs, urlsplit
    from .. import sessions
    q = parse_qs(urlsplit(h.path).query)
    conn = core.connect()
    try:
        if path == "/api/sessions":
            rows = cards(conn)
            with server.read_memo():
                body = {"sessions": rows, "proposals": core.expert_proposals(conn),
                        # The status inbox (#5852): which states send a push notice (the global value; Push on the page).
                        "notify_status": core.setting(conn, "notify_status")}
            return h._send(200, body)
        terminals = server.agent_terminals(conn)
        cursor, agent = q.get("cursor", [""])[0], q.get("agent", [""])[0]
        if "file" in q:  # scrolling up: the page of lines before a byte of one of its transcripts (#5120)
            before = q.get("before", [""])[0]
            return h._send(200, sessions.older_page(conn, agent, q["file"][0], int(before) if before.isdigit() else None))
        pane = terminals.get(agent)
        prompt = server.pane_prompts(conn, {agent: pane}).get(agent) if pane else None
        return h._send(200, dict(sessions.view(conn, agent, int(cursor) if cursor.isdigit() else None, prompt=prompt,
                                               pane=bool(pane)),
                                    terminal=bool(pane), pending=core.pending_list(conn, agent)))
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
    (lambda path: path == "/api/session-prompts", session_prompts),
    (lambda path: path == "/api/session-commands", session_commands),
    (lambda path: path == "/api/session-usage", session_usage),
    (lambda path: path in ("/view", "/api/view-key"), full_screen),
    (lambda path: path in ("/api/sessions", "/api/session"), running),
]

OPS = {
    "session_send": lambda c, a, who: server.session_send(c, a["agent"], a.get("text", ""), who, a.get("images"),
                                                          a.get("files"), hold=bool(a.get("hold"))),
    # Send now under a held message in the chat (#5587).
    "pending_send_now": lambda c, a, who: server.pending_send_now(c, a["agent"], a["id"], who),
    "session_upload": lambda c, a, who: server.session_upload(c, a["agent"], a.get("data"), a.get("name"), who),
    "session_upload_drop": lambda c, a, who: server.session_upload_drop(c, a["agent"], a.get("id"), who),
    "session_setting": lambda c, a, who: server.session_setting_live(c, a["agent"], a["key"], a.get("value"), who),
    "session_restart": lambda c, a, who: server.session_restart(c, a["agent"], a.get("changes"),
                                                                bool(a.get("make_default")), who),
    "prompt_answer": lambda c, a, who: server.prompt_answer(c, a["agent"], a.get("sig", ""), a.get("pick"),
                                                            a.get("other"), who, a.get("key")),
    # The card of an expert proposal in the manager's chat (#4990): Continue in an expert, or Dismiss.
    "expert_accept": lambda c, a, who: server.accept_expert(c, a["id"], who),
    "expert_dismiss": lambda c, a, who: core.expert_proposal_end(c, a["id"], "dismissed", who, a.get("why")),
    # The person has the session's chat open (#5852): done becomes idle in the status inbox.
    "session_seen": lambda c, a, who: core.session_seen(c, a["agent"], who),
}
