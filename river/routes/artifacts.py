"""The Artifacts tab: what the sessions made or named. (#4656: each view has its routes here; server.py keeps the
frame.)

GET: (test, handler) pairs that Handler.do_GET tries in order; a handler answers with h._send. OPS: page actions,
merged into server.OPS."""

from .. import core, server  # noqa: F401  (server: read at call time, so tests that replace its globals still work)
from ..core import RiverError  # noqa: F401
import hmac  # noqa: F401


def canvas_rows(conn, agent=None, item=None, since=None):
    """The canvases as rows of the Artifacts lists, first (#4768): all of them, or the ones an agent wrote a version
    of; item: the canvases of that item; since: changed from then on (an ISO time)."""
    wrote = None
    if agent:
        wrote = {r[0] for r in conn.execute("SELECT DISTINCT canvas_id FROM canvas_versions WHERE author=?", (agent,))}
    return [{"kind": "canvas", "target": f"canvas:{c['id']}", "session": c["agent"], "how": "wrote",
             "item": c["item_id"], "at": c["updated_at"], "title": c["title"] or c["name"], "name": c["name"],
             "version": c["version"], "open_comments": c["open_comments"], "project": c["project"]}
            for c in core.canvas_heads(conn) if (wrote is None or c["id"] in wrote)
            and (item is None or c["item_id"] == item) and (not since or (c["updated_at"] or "") >= since)]


def shows_file(conn):
    """allowed(path) for sessions.artifacts: whether the side panel shows that file (server.local_file)."""
    def allowed(f):
        try:
            server.local_file(conn, f, read=False)
            return True
        except RiverError:
            return False
    return allowed


def listing(conn, agent=None, item=None, since=None, terminals=None):
    """The rows of the Artifacts tab and of maxpm artifacts (#4838), one source: the canvases first, then what the
    sessions wrote or named, newest first. agent, item: of that session or item only, also long ago; since: an ISO
    time. With neither agent nor item, terminals (server.agent_terminals) names the running sessions."""
    from .. import sessions
    return canvas_rows(conn, agent, item, since) + sessions.artifacts(
        conn, None if agent or item is not None else terminals, shows_file(conn), agent=agent, item=item, since=since)


def artifacts(h, path):
    """The Artifacts tab (#4620)."""
    conn = core.connect()
    try:
        from urllib.parse import parse_qs, urlsplit
        one = parse_qs(urlsplit(h.path).query).get("agent", [""])[0] or None
        with server.read_memo():  # it writes nothing: the agents' statuses and the settings once (#5597)
            rows = listing(conn, one, terminals=None if one else server.agent_terminals(conn))
        return h._send(200, {"artifacts": rows})
    finally:
        conn.close()


GET = [
    (lambda path: path == "/api/artifacts", artifacts),
]

OPS = {}
