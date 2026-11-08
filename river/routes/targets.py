"""The Targets tab: Deploy now, release cadence and Release now, cut, and hooks. (#4656: each view has its routes here;
server.py keeps the frame.)

GET: (test, handler) pairs that Handler.do_GET tries in order; a handler answers with h._send. OPS: page actions,
merged into server.OPS."""

from .. import core, server  # noqa: F401  (server: read at call time, so tests that replace its globals still work)
from ..core import RiverError  # noqa: F401
import hmac  # noqa: F401



GET = [
]

OPS = {
    "deploy_now": lambda c, a, who: server.deploy_now(c, a["target"], bool(a.get("review")), agent=a.get("agent"), actor=who,
                                               **server._launch_args(a)),
    "target_cadence": lambda c, a, who: core.target_cadence(c, a["target"], a.get("cadence") or "off", who),
    "release_now": lambda c, a, who: core.release_now(c, a["target"], a.get("reason"), who),
    "target_cut": lambda c, a, who: core.target_cut(c, a["target"], a.get("rev"), who),
    "target_hook": lambda c, a, who: core.target_hook(c, a["target"], a.get("event"), a.get("command"),
                                                     bool(a.get("clear")), who),
}
