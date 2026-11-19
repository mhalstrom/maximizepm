"""The definition of a pipeline: its stages, environments and gates, changed by the maxpm pipeline commands (#6113).

mark 2026-11-18 (Decision 5 of canvas release-tracking): a pipeline is kept in MaximizePM and changed by commands, as
the hooks of a target are; maxpm pipeline export prints it as a JSON file to keep in git. Only a person or the
manager changes a pipeline, since its stages run commands and its gates decide what reaches production. Each change
is a row of pipeline_changes with its actor (maxpm pipeline history).

Gate rules, in the words of maxpm pipeline gate:
    auto                 the stage starts by itself when the one before it passed
    auto after <time>    ... and the checks of the stage before it passed for that long (30m)
    manager              the manager lets each version in
    person [<name>]      a person lets each version in (that person, when named)
"""

import json

from .. import core
from . import store

_FIELDS = ("kind", "run", "workflow", "env", "gate", "junit")


def _may(conn, actor):
    why = core._lead_refusal(conn, actor, "changes a pipeline, because its stages run commands and its gates decide "
                                          "what reaches production")
    if why:
        raise core.RiverError(why)


def _change(conn, pid, actor, text, detail=None):
    conn.execute("INSERT INTO pipeline_changes(pipeline_id, at, actor, change, detail) VALUES (?,?,?,?,?)",
                 (pid, store._now(), actor, text, json.dumps(detail, separators=(",", ":")) if detail else None))


def parse_gate(text):
    """(gate, rule) from the words of a gate rule; refuses other words."""
    words = str(text or "").split()
    if words[:1] == ["auto"] and len(words) == 1:
        return "auto", None
    if words[:2] == ["auto", "after"] and len(words) == 3:
        try:
            core.parse_duration(words[2])
        except (core.RiverError, ValueError):
            words = []
        else:
            return "auto", words[2]
    if words == ["manager"]:
        return "manager", None
    if words[:1] == ["person"] and len(words) <= 2:
        return "person", words[1] if len(words) == 2 else None
    raise core.RiverError(f"the gate rule {text!r} is not one of: auto; auto after <time> (30m); manager; "
                          "person; person <name>")


def pipeline_add(conn, name, project=None, notes=None, actor=None):
    name = store._name(name, "pipeline")
    with core.tx(conn):
        _may(conn, actor)
        if conn.execute("SELECT 1 FROM pipelines WHERE name=?", (name,)).fetchone():
            raise core.RiverError(f"a pipeline named {name} exists already: maxpm pipeline show {name}")
        pid = conn.execute("INSERT INTO pipelines(name, project, notes, created_by, created_at) VALUES (?,?,?,?,?)",
                           (name, project, notes, actor, store._now())).lastrowid
        _change(conn, pid, actor, f"pipeline {name} added" + (f" for project {project}" if project else ""))
    return dict(store._pipeline(conn, name))


def _env_row(conn, pid, name):
    return conn.execute("SELECT * FROM pipeline_envs WHERE pipeline_id=? AND name=?", (pid, name)).fetchone()


def env_set(conn, pipeline, name, target=None, url=None, notes=None, remove=False, actor=None):
    """Add an environment, or change the fields given ('' clears one), or remove it (a stage must not name it)."""
    name = store._name(name, "environment")
    with core.tx(conn):
        _may(conn, actor)
        p = store._pipeline(conn, pipeline)
        row = _env_row(conn, p["id"], name)
        if remove:
            if row is None or row["removed_at"]:
                raise core.RiverError(f"pipeline {p['name']} has no environment {name}")
            used = conn.execute("SELECT name FROM pipeline_stages WHERE env_id=? AND removed_at IS NULL",
                                (row["id"],)).fetchone()
            if used:
                raise core.RiverError(f"stage {used['name']} deploys to {name}; remove or change that stage first")
            conn.execute("UPDATE pipeline_envs SET removed_at=? WHERE id=?", (store._now(), row["id"]))
            _change(conn, p["id"], actor, f"environment {name} removed")
            return {"pipeline": p["name"], "env": name, "changed": "removed"}
        given = {k: v for k, v in (("target", target), ("url", url), ("notes", notes)) if v is not None}
        if row is None:
            conn.execute("INSERT INTO pipeline_envs(pipeline_id, name, target, url, notes, created_at) "
                         "VALUES (?,?,?,?,?,?)",
                         (p["id"], name, target or None, url or None, notes or None, store._now()))
            changed = "added"
        elif row["removed_at"] or given:
            conn.execute("UPDATE pipeline_envs SET removed_at=NULL" + "".join(f", {k}=?" for k in given)
                         + " WHERE id=?", [v or None for v in given.values()] + [row["id"]])
            changed = "added again" if row["removed_at"] else "changed"
        else:
            raise core.RiverError(f"environment {name} exists; give what to change: --target, --url or --notes")
        new = dict(_env_row(conn, p["id"], name))
        detail = {k: new[k] for k in ("name", "target", "url", "notes")}
        _change(conn, p["id"], actor, f"environment {name} {changed}"
                + "".join(f", {k} {v or 'none'}" for k, v in given.items()), detail)
    return {"pipeline": p["name"], "env": name, "changed": changed, "detail": detail}


def _stage_row(conn, pid, name):
    return conn.execute("SELECT * FROM pipeline_stages WHERE pipeline_id=? AND name=?", (pid, name)).fetchone()


def _live(conn, pid):
    return [r["id"] for r in conn.execute("SELECT id FROM pipeline_stages WHERE pipeline_id=? AND removed_at IS NULL "
                                          "ORDER BY pos, id", (pid,))]


def _place(conn, pid, stage_id, pos):
    """Put the stage at pos (1 is the first) among the live stages, and number them 1, 2, 3 again."""
    order = [i for i in _live(conn, pid) if i != stage_id]
    if stage_id is not None:
        at = len(order) if pos is None else min(max(int(pos), 1) - 1, len(order))
        order.insert(at, stage_id)
    for n, i in enumerate(order, 1):
        conn.execute("UPDATE pipeline_stages SET pos=? WHERE id=?", (n, i))


def _stage_detail(conn, s):
    env = conn.execute("SELECT name FROM pipeline_envs WHERE id=?", (s["env_id"],)).fetchone() if s["env_id"] else None
    return {"name": s["name"], "pos": s["pos"], "kind": s["kind"], "run": s["run"], "workflow": s["workflow"],
            "env": env["name"] if env else None, "gate": store.gate_text(s["gate"], s["gate_rule"]),
            "junit": s["junit"]}


def stage_set(conn, pipeline, name, kind=None, run=None, workflow=None, env=None, gate=None, pos=None, remove=False,
              actor=None, junit=None):
    """Add a stage (kind needed), or change the fields given ('' clears run, workflow or env), or move it (pos), or
    remove it (its runs stay; adding the name again brings it back). A stage runs a command (run) or a forge
    workflow (workflow), not both; a deploy stage names its environment."""
    name = store._name(name, "stage")
    if kind is not None and kind not in store.STAGE_KINDS:
        raise core.RiverError(f"the stage kind {kind!r} is not one of {', '.join(store.STAGE_KINDS)}")
    if run and workflow:
        raise core.RiverError("a stage runs a command (--run) or a forge workflow (--workflow), not both")
    rule = parse_gate(gate) if gate is not None else None
    with core.tx(conn):
        _may(conn, actor)
        p = store._pipeline(conn, pipeline)
        pid, row = p["id"], _stage_row(conn, p["id"], name)
        if remove:
            if row is None or row["removed_at"]:
                raise core.RiverError(f"pipeline {p['name']} has no stage {name}")
            conn.execute("UPDATE pipeline_stages SET removed_at=? WHERE id=?", (store._now(), row["id"]))
            _place(conn, pid, None, None)
            _change(conn, pid, actor, f"stage {name} removed (its runs stay)")
            return {"pipeline": p["name"], "stage": name, "changed": "removed"}
        sets = {}
        if kind is not None:
            sets["kind"] = kind
        if run is not None:
            sets["run"] = run.strip() or None
            if run.strip():
                sets["workflow"] = None
        if workflow is not None:
            sets["workflow"] = workflow.strip() or None
            if workflow.strip():
                sets["run"] = None
        if env is not None:
            if env == "":
                sets["env_id"] = None
            else:
                e = _env_row(conn, pid, env)
                if e is None or e["removed_at"]:
                    raise core.RiverError(f"pipeline {p['name']} has no environment {env}; add it first: "
                                          f"maxpm pipeline env {p['name']} {env}")
                sets["env_id"] = e["id"]
        if rule is not None:
            sets["gate"], sets["gate_rule"] = rule
        if junit is not None:  # a test stage's JUnit XML file, in the project folder (#6114)
            sets["junit"] = junit.strip() or None
        if row is None:
            if kind is None:
                raise core.RiverError(f"a new stage needs its kind: --kind {'|'.join(store.STAGE_KINDS)}")
            sid = conn.execute("INSERT INTO pipeline_stages(pipeline_id, name, pos, kind, created_by, created_at) "
                               "VALUES (?,?, 0, ?,?,?)", (pid, name, kind, actor, store._now())).lastrowid
            changed = "added"
        else:
            sid = row["id"]
            changed = "added again" if row["removed_at"] else "changed"
            if changed == "changed" and not sets and pos is None:
                raise core.RiverError(f"stage {name} exists; give what to change: --kind, --run, --workflow, --env, "
                                      "--gate or --pos")
            if row["removed_at"]:
                sets["removed_at"] = None
        if sets:
            conn.execute("UPDATE pipeline_stages SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?",
                         list(sets.values()) + [sid])
        new = _stage_row(conn, pid, name)
        if new["kind"] == "deploy" and not new["env_id"]:
            raise core.RiverError(f"a deploy stage names its environment: maxpm pipeline stage {p['name']} {name} "
                                  "--env <name>")
        if row is None or row["removed_at"] or pos is not None:
            _place(conn, pid, sid, pos)
        detail = _stage_detail(conn, _stage_row(conn, pid, name))
        if changed == "changed":
            old = _stage_detail(conn, row)
            parts = [f"{k} {old[k] or 'none'} -> {detail[k] or 'none'}" for k in ("pos",) + _FIELDS
                     if old[k] != detail[k]]
        else:
            parts = [f"{k} {detail[k]}" for k in _FIELDS + ("pos",) if detail[k]]
        _change(conn, pid, actor, f"stage {name} {changed}" + (": " + ", ".join(parts) if parts else ""), detail)
    return {"pipeline": p["name"], "stage": name, "changed": changed, "detail": detail}


def gate_set(conn, pipeline, stage, rule, actor=None):
    """maxpm pipeline gate: the rule that lets a version into a stage."""
    return stage_set(conn, pipeline, stage, gate=rule, actor=actor)


def export(conn, name):
    """The definition of a pipeline as one JSON object, to keep in git: its environments and its stages in order."""
    p = store._pipeline(conn, name)
    envs = [{k: e[k] for k in ("name", "target", "url", "notes")} for e in conn.execute(
        "SELECT * FROM pipeline_envs WHERE pipeline_id=? AND removed_at IS NULL ORDER BY id", (p["id"],))]
    stages = []
    for s in conn.execute("SELECT * FROM pipeline_stages WHERE pipeline_id=? AND removed_at IS NULL ORDER BY pos, id",
                          (p["id"],)):
        d = _stage_detail(conn, s)
        d.pop("pos")
        stages.append(d)
    return {"pipeline": p["name"], "project": p["project"], "notes": p["notes"], "environments": envs,
            "stages": stages}


def history(conn, name, limit=50):
    """The changes of a pipeline's definition, newest first."""
    p = store._pipeline(conn, name)
    return [dict(r) for r in conn.execute("SELECT at, actor, change FROM pipeline_changes WHERE pipeline_id=? "
                                          "ORDER BY id DESC LIMIT ?", (p["id"], limit))]
