"""The Artifacts tab: what the sessions made or named. (#4656: each view has its routes here; server.py keeps the
frame.)

GET: (test, handler) pairs that Handler.do_GET tries in order; a handler answers with h._send. OPS: page actions,
merged into server.OPS."""

from .. import core, server  # noqa: F401  (server: read at call time, so tests that replace its globals still work)
from ..core import RiverError  # noqa: F401
import hmac  # noqa: F401


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
        return h._send(200, {"artifacts": sessions.artifacts(conn, None if one else server.agent_terminals(conn),
                                                                allowed, agent=one)})
    finally:
        conn.close()


GET = [
    (lambda path: path == "/api/artifacts", artifacts),
]

OPS = {}
