"""The Done tab: what was finished, by day. (#4656: each view has its routes here; server.py keeps the frame.)

GET: (test, handler) pairs that Handler.do_GET tries in order; a handler answers with h._send. OPS: page actions,
merged into server.OPS."""

from .. import core, server  # noqa: F401  (server: read at call time, so tests that replace its globals still work)
from ..core import RiverError  # noqa: F401
import hmac  # noqa: F401


def log(h, path):
    from urllib.parse import parse_qs, urlsplit
    q = {k: v[0] for k, v in parse_qs(urlsplit(h.path).query).items()}
    conn = core.connect()
    try:
        since = q.get("since", "7d")
        if since == "archived":  # all time, the archived items too, read on demand (#5712)
            from .. import space
            return h._send(200, space.with_archive(conn, lambda: core.completed(conn, q.get("project") or None, None)))
        return h._send(200, core.completed(conn, q.get("project") or None, None if since == "all" else since))
    except RiverError as e:
        return h._send(400, {"error": str(e)})
    finally:
        conn.close()


GET = [
    (lambda path: path == "/api/log", log),
]

OPS = {}
