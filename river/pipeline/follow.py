"""Followed pipelines (#6167): MaximizePM follows an outside system's flow and controls none of it.

mark 2026-11-18: "also we need to be able to just follow along with outside systems", then "pipelines we are just
following along with we can still have hooks to fix issues etc".

- maxpm pipeline follow <name> [--source <plugin>] [--project P]: the pipeline (added when it is new) is followed. Its
  runs come from its source: a plugin (#6159) or events in over HTTP (#6163). An event that names a stage the pipeline
  lacks adds it (store._stage_id), so its stages need no setup.
- A followed pipeline has no transitions, start, run again, Run, move back, hold, stop or override of ours: each one
  refuses with check(), and the runner hands no version on (the outside system reports each stage itself).
- What still acts on its events: its trigger rules (#6150): an agent works on the fault where we can act (our code, a
  pull request, a fix task, a message to its owner), and a rule's hook command runs (triggers._hook). Stop its agents
  and Revoke its keys on the Emergency tab still apply: they act on our side.
- maxpm pipeline follow <name> --off: we control its flow again.
"""

from .. import core
from . import store


def is_followed(p):
    return bool(p and "followed" in p.keys() and p["followed"])


def check(conn, p, what):
    """Refuse an action of our flow on a followed pipeline: p is its row (or its name)."""
    if isinstance(p, str):
        p = store._pipeline(conn, p)
    if p["forge"] == "github":
        from . import targets
        names = targets.check_targets(conn, p["id"])
        if names:
            raise core.RiverError(f"pipeline {p['name']} supplies GitHub checks to {', '.join(names)}. "
                                  "Use the deploy target's release controls.")
    if is_followed(p):
        src = p["forge"] or ("events in over HTTP" if p["intake_secret"] else "its outside system")
        raise core.RiverError(f"pipeline {p['name']} is followed: {src} runs its flow, and MaximizePM {what} there "
                              f"never. Its trigger rules and hooks still act on its events (maxpm pipeline triggers "
                              f"{p['name']}); to control its flow here: maxpm pipeline follow {p['name']} --off")


def follow(conn, name, source=None, project=None, off=False, actor=None):
    """maxpm pipeline follow: make a pipeline followed (adding it when it is new), or --off: ours again. --source
    names the plugin that reads it (maxpm pipeline forge <name> <plugin> --set ... sets its settings)."""
    from . import define, plugin
    define._may(conn, actor)
    row = conn.execute("SELECT * FROM pipelines WHERE name=?", (name,)).fetchone()
    if row is None:
        if off:
            raise core.RiverError(f"no pipeline named {name!r}")
        define.pipeline_add(conn, name, project=project, actor=actor)
    p = store._pipeline(conn, name)
    from . import target_flow
    target_flow.refuse_define(p, "follows an outside system")  # #6176
    if off:
        if not is_followed(p):
            raise core.RiverError(f"pipeline {name} is not followed")
        with core.tx(conn):
            conn.execute("UPDATE pipelines SET followed=NULL, followed_by=NULL, followed_at=NULL WHERE id=?", (p["id"],))
            define._change(conn, p["id"], actor, f"pipeline {name} is no longer followed: MaximizePM runs its flow")
        return {"pipeline": name, "followed": False}
    if source:
        plugin.use(conn, name, source, actor=actor)
    p = store._pipeline(conn, name)
    if not is_followed(p):
        with core.tx(conn):
            conn.execute("UPDATE pipelines SET followed=1, followed_by=?, followed_at=? WHERE id=?",
                         (actor, store._now(), p["id"]))
            define._change(conn, p["id"], actor, f"pipeline {name} is followed: its outside system runs its flow"
                           + (f" ({p['forge']})" if p["forge"] else ""))
    p = store._pipeline(conn, name)
    return {"pipeline": name, "followed": True, "source": p["forge"] or ("events in" if p["intake_secret"] else None)}
