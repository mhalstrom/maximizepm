"""The Artifacts tab: what the sessions made or named. (#4656: each view has its routes here; server.py keeps the
frame.)

GET: (test, handler) pairs that Handler.do_GET tries in order; a handler answers with h._send. OPS: page actions,
merged into server.OPS."""

from .. import core, server  # noqa: F401  (server: read at call time, so tests that replace its globals still work)
from ..core import RiverError  # noqa: F401
import hmac  # noqa: F401


def canvas_rows(conn, agent=None):
    """The canvases as rows of the Artifacts lists, first (#4768): all of them, or the ones an agent wrote a version
    of."""
    wrote = None
    if agent:
        wrote = {r[0] for r in conn.execute("SELECT DISTINCT canvas_id FROM canvas_versions WHERE author=?", (agent,))}
    return [{"kind": "canvas", "target": f"canvas:{c['id']}", "session": c["agent"], "how": "wrote",
             "item": c["item_id"], "at": c["updated_at"], "title": c["title"] or c["name"], "name": c["name"],
             "version": c["version"], "open_comments": c["open_comments"], "project": c["project"]}
            for c in core.canvas_heads(conn) if wrote is None or c["id"] in wrote]


def artifacts(h, path):
    """The Artifacts tab (#4620)."""
    from .. import sessions
    conn = core.connect()
    try:
        def allowed(f):
            try:
                server.local_file(conn, f, read=False)
                return True
            except RiverError:
                return False
        from urllib.parse import parse_qs, urlsplit
        one = parse_qs(urlsplit(h.path).query).get("agent", [""])[0] or None
        return h._send(200, {"artifacts": canvas_rows(conn, one) + sessions.artifacts(
            conn, None if one else server.agent_terminals(conn), allowed, agent=one)})
    finally:
        conn.close()


GET = [
    (lambda path: path == "/api/artifacts", artifacts),
]

OPS = {}
