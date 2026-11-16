"""The Targets tab: Deploy now, release cadence and Release now, cut, and hooks. (#4656: each view has its routes here;
server.py keeps the frame.)

GET: (test, handler) pairs that Handler.do_GET tries in order; a handler answers with h._send. OPS: page actions,
merged into server.OPS."""

from .. import core, report, server  # noqa: F401  (server: read at call time, so tests that replace its globals still work)
from ..core import RiverError  # noqa: F401
import hmac  # noqa: F401



def release_report(h, path):
    """GET /api/report/<id>: the review report of the release of a deploy item or of its review (#6003)."""
    conn = core.connect()
    try:
        return h._send(200, report.report(conn, int(path.rsplit("/", 1)[1])))
    except (RiverError, ValueError) as e:
        return h._send(404, {"error": str(e)})
    finally:
        conn.close()


GET = [
    (lambda path: path.startswith("/api/report/"), release_report),
]

OPS = {
    "deploy_now": lambda c, a, who: server.deploy_now(c, a["target"], bool(a.get("review")), agent=a.get("agent"), actor=who,
                                               **server._launch_args(a)),
    "target_cadence": lambda c, a, who: core.target_cadence(c, a["target"], a.get("cadence") or "off", who),
    "release_now": lambda c, a, who: core.release_now(c, a["target"], a.get("reason"), who),
    "release_summary": lambda c, a, who: report.summary_set(c, int(a["id"]), a.get("text"), who),
    "target_cut": lambda c, a, who: core.target_cut(c, a["target"], a.get("rev"), who),
    "target_hook": lambda c, a, who: core.target_hook(c, a["target"], a.get("event"), a.get("command"),
                                                     bool(a.get("clear")), who),
}
