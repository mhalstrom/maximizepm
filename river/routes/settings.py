"""The Settings tab: set and unset a setting. (#4656: each view has its routes here; server.py keeps the frame.)

GET: (test, handler) pairs that Handler.do_GET tries in order; a handler answers with h._send. OPS: page actions,
merged into server.OPS."""

from .. import core, server  # noqa: F401  (server: read at call time, so tests that replace its globals still work)
from ..core import RiverError  # noqa: F401
import hmac  # noqa: F401



GET = [
]

OPS = {
    "config_set": lambda c, a, who: core.config_set(c, a["key"], str(a["value"]), a.get("project"), a.get("item"),
                                                    a.get("agent"), who),
    "config_unset": lambda c, a, who: core.config_unset(c, a["key"], a.get("project"), a.get("item"), a.get("agent"), who),
}
