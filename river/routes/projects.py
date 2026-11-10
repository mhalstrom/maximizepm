"""The Projects tab: projects, the Add item form, and goals. (#4656: each view has its routes here; server.py keeps the
frame.)

GET: (test, handler) pairs that Handler.do_GET tries in order; a handler answers with h._send. OPS: page actions,
merged into server.OPS."""

from .. import core, server  # noqa: F401  (server: read at call time, so tests that replace its globals still work)
from ..core import RiverError  # noqa: F401
import hmac  # noqa: F401



GET = [
]

OPS = {
    "project_add": lambda c, a, who: core.project_add(c, a["name"], a.get("rank"), a.get("notes", ""), who),
    "project_rank": lambda c, a, who: core.project_rank(c, a["name"], a["rank"], who),
    "project_describe": lambda c, a, who: core.project_describe(c, a["name"], a["text"], who),
    "item_add": lambda c, a, who: core.item_add(c, a["project"], a["title"], int(a.get("priority", 2)),
                                                a.get("notes", ""), a.get("doer", "any"),
                                                [int(x) for x in a.get("after", [])], who,
                                                a.get("context", ""), a.get("touches"), a.get("check", ""),
                                                a.get("blocks"), a.get("mode"), a.get("found_during"),
                                                [int(x) for x in a.get("feeds", [])], a.get("due"),
                                                goals=a.get("goals"), models=a.get("models"), tier=a.get("tier")),
    "goal_add": lambda c, a, who: core.goal_add(c, a["project"], a["name"], a.get("outcome", ""), a.get("done_when", ""),
                                                who, shared=bool(a.get("shared"))),
    "goal_edit": lambda c, a, who: core.goal_edit(c, a["name"], a.get("outcome"), a.get("done_when"), a.get("new_name"), who,
                                                  a.get("shared")),
    "goal_rank": lambda c, a, who: core.goal_rank(c, a["name"], a["rank"], who),
    "goal_own": lambda c, a, who: core.goal_own(c, a["name"], who),
    "goal_release": lambda c, a, who: core.goal_release(c, a["name"], who, a.get("no_handoff")),
    "goal_done": lambda c, a, who: core.goal_done(c, a["name"], a.get("result", ""), who, bool(a.get("drop_open"))),
    "goal_reopen": lambda c, a, who: core.goal_reopen(c, a["name"], who),
}
