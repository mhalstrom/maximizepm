"""Numbered versions of a pipeline's definition (#6143).

mark 2026-11-18 (canvas release-tracking, task P): "the specific implmentation details are created at setup time and
can be modified as needed with versioning". Each change of the definition (its environments, stages, commands and
workflows, branches, the standing rules of its transitions, its trigger rules with their runners and keys, its runners,
its source) makes version n+1: the whole definition as maxpm pipeline export prints it, who changed it, when, and
why (the change's own line). A change that leaves the definition as it was (a block, a delay, a freeze: actions of a
moment) makes none.

- snapshot: called by define._change and transitions._line inside their tx, after the change.
- listing, show, compare: maxpm pipeline versions <name> [--show N] [--compare A B]; the head of the pipeline's page.
- restore: maxpm pipeline versions <name> --back N --reason "...": the definition becomes what version N was, as one
  more version (N is never rewritten). The source (forge or plugin, its token) and the freezes stay as they are now.
"""

import json
import threading

from .. import core
from . import store


def _form(conn, p):
    """The definition as a version keeps it: the export, less the freezes (each ends by itself)."""
    from . import define
    d = define.export(conn, p["name"])
    for s in d["stages"]:
        s["transition"] = {k: v for k, v in s["transition"].items() if k != "freezes"}
    return d


def _text(d):
    return json.dumps(d, sort_keys=True, separators=(",", ":"))


_HELD = threading.local()  # a restore makes one version: the steps it takes make none


def snapshot(conn, pid, actor, why, force=False):
    """A new version when the definition changed since the last one (inside the caller's tx). Returns its number."""
    if getattr(_HELD, "pid", None) == pid and not force:
        return None
    p = conn.execute("SELECT * FROM pipelines WHERE id=?", (pid,)).fetchone()
    if p is None:
        return None
    text = _text(_form(conn, p))
    last = conn.execute("SELECT n, definition FROM pipeline_def_versions WHERE pipeline_id=? ORDER BY n DESC LIMIT 1",
                        (pid,)).fetchone()
    if last is not None and last["definition"] == text:
        return None
    n = (last["n"] if last else 0) + 1
    conn.execute("INSERT INTO pipeline_def_versions(pipeline_id, n, at, actor, why, definition) VALUES (?,?,?,?,?,?)",
                 (pid, n, store._now(), actor, why, text))
    return n


def _first(conn, pid):
    """A pipeline made before versions began has none: its definition now is version 1."""
    if not conn.execute("SELECT 1 FROM pipeline_def_versions WHERE pipeline_id=?", (pid,)).fetchone():
        with core.tx(conn):
            snapshot(conn, pid, None, "the definition when versions began")


def current(conn, pid):
    """The newest version of a pipeline: {n, at, actor, why}, or None."""
    _first(conn, pid)
    r = conn.execute("SELECT n, at, actor, why FROM pipeline_def_versions WHERE pipeline_id=? ORDER BY n DESC LIMIT 1",
                     (pid,)).fetchone()
    return dict(r) if r else None


def _version(conn, p, n):
    r = conn.execute("SELECT * FROM pipeline_def_versions WHERE pipeline_id=? AND n=?", (p["id"], int(n))).fetchone()
    if r is None:
        raise core.RiverError(f"pipeline {p['name']} has no definition version {n} (maxpm pipeline versions {p['name']})")
    return r


def listing(conn, pipeline, limit=50):
    """The versions of a pipeline's definition, newest first."""
    p = store._pipeline(conn, pipeline)
    _first(conn, p["id"])
    rows = conn.execute("SELECT n, at, actor, why FROM pipeline_def_versions WHERE pipeline_id=? ORDER BY n DESC LIMIT ?",
                        (p["id"], limit)).fetchall()
    return {"pipeline": p["name"], "versions": [dict(r) for r in rows]}


def show(conn, pipeline, n):
    """One version: its definition, who, when, why."""
    p = store._pipeline(conn, pipeline)
    r = _version(conn, p, n)
    return {"pipeline": p["name"], "n": r["n"], "at": r["at"], "actor": r["actor"], "why": r["why"],
            "definition": json.loads(r["definition"])}


def _flat(d):
    """The definition as {path: value}: environments and stages by name, trigger rules by their place."""
    out = {}
    for k in ("project", "notes", "forge", "runners"):
        out[k] = d.get(k)
    for e in d.get("environments", []):
        for k, v in e.items():
            if k != "name":
                out[f"environment {e['name']} {k}"] = v
        out[f"environment {e['name']}"] = "present"
    for i, s in enumerate(d.get("stages", []), 1):
        out[f"stage {s['name']}"] = f"place {i}"
        for k, v in s.items():
            if k == "transition":
                for tk, tv in v.items():
                    out[f"stage {s['name']} transition {tk}"] = tv
            elif k != "name":
                out[f"stage {s['name']} {k}"] = v
    for i, t in enumerate(d.get("triggers", []), 1):
        out[f"trigger rule {i}"] = t
    return out


def compare(conn, pipeline, a, b):
    """What changed from version a to version b: [{what, a, b}], each part that differs."""
    p = store._pipeline(conn, pipeline)
    fa, fb = (_flat(json.loads(_version(conn, p, x)["definition"])) for x in (a, b))
    diff = [{"what": k, "a": fa.get(k), "b": fb.get(k)} for k in sorted(set(fa) | set(fb), key=_order(fa, fb))
            if fa.get(k) != fb.get(k)]
    return {"pipeline": p["name"], "a": int(a), "b": int(b), "changes": diff}


def _order(fa, fb):
    keys = list(dict.fromkeys(list(fa) + list(fb)))
    return keys.index


def restore(conn, pipeline, n, reason, actor=None):
    """Make the definition what version n was, as one more version: environments, stages (their fields, order and
    branches), the standing rules of each transition, the trigger rules and the runners. The source, the freezes and
    the keys of its stages (#6153: a grant is a person's act, with its own check) stay as they are now. A person or
    the manager."""
    if not (reason or "").strip():
        raise core.RiverError("say why: --reason \"<why this version>\" (the audit log keeps it)")
    why = core._lead_refusal(conn, actor, "puts a pipeline's definition back")
    if why:
        raise core.RiverError(why)
    p = store._pipeline(conn, pipeline)
    old = json.loads(_version(conn, p, n)["definition"])
    _first(conn, p["id"])
    _HELD.pid = p["id"]
    try:
        _apply(conn, p, old, n, reason, actor)
    finally:
        _HELD.pid = None
    with core.tx(conn):
        from . import audit
        m = snapshot(conn, p["id"], actor, f"back to version {n}: {reason.strip()}")
        audit.write(conn, p["id"], actor, "define", f"put the definition back to version {n}"
                    + (f" (now version {m})" if m else " (it was the same)"), place="the pipeline", reason=reason.strip())
    return {"pipeline": p["name"], "back_to": int(n), "version": m or (current(conn, p["id"]) or {}).get("n")}


def _apply(conn, p, old, n, reason, actor):
    """The steps of a restore: each part of the definition that differs from version n (old) becomes as it was."""
    from . import define, transitions, triggers
    from .. import runners as runners_mod
    now = _form(conn, p)
    name = p["name"]
    # Environments: add or change the ones of version n, remove the others (their stages go first, below).
    want_envs = {e["name"]: e for e in old["environments"]}
    for e in old["environments"]:
        have = next((x for x in now["environments"] if x["name"] == e["name"]), None)
        if have != e:
            define.env_set(conn, name, e["name"], target=e.get("target") or "", url=e.get("url") or "",
                           notes=e.get("notes") or "", actor=actor)
    # Stages: remove the ones version n lacks, then set each one in its order, with its branch after the others exist.
    want = [s["name"] for s in old["stages"]]
    for s in now["stages"]:
        if s["name"] not in want:
            define.stage_set(conn, name, s["name"], remove=True, actor=actor)
    for pos, s in enumerate(old["stages"], 1):
        t = s.get("transition") or {}
        cur = next((x for x in _form(conn, p)["stages"] if x["name"] == s["name"]), None)
        fields = dict(kind=s["kind"], run=s.get("run") or "", workflow=s.get("workflow") or "", env=s.get("env") or "",
                      junit=s.get("junit") or "", waves=s.get("waves") or "", check=s.get("check") or "",
                      rollback=s.get("rollback") or "", wave_wait=s.get("wave_wait") or "", tier=s.get("tier") or "",
                      start=s.get("start") or "auto", gate=t.get("mode") or "auto")
        if cur is None or any((cur.get(k) or "") != (v or "") for k, v in fields.items() if k != "gate") \
                or (cur.get("transition") or {}).get("mode") != fields["gate"] or _form_pos(conn, p, s["name"]) != pos:
            define.stage_set(conn, name, s["name"], pos=pos, actor=actor, **fields)
    for s in old["stages"]:
        cur = next(x for x in _form(conn, p)["stages"] if x["name"] == s["name"])
        if (cur.get("after") or "") != (s.get("after") or ""):
            define.stage_set(conn, name, s["name"], after=s.get("after") or "", actor=actor)
        t, ct = s.get("transition") or {}, cur.get("transition") or {}
        if (t.get("window") or "") != (ct.get("window") or "") or bool(t.get("fault_rule")) != bool(ct.get("fault_rule")):
            transitions.change(conn, name, s["name"], window=t.get("window") or "none",
                               fault_rule=bool(t.get("fault_rule")), reason=reason, actor=actor)
    for e in now["environments"]:
        if e["name"] not in want_envs:
            define.env_set(conn, name, e["name"], remove=True, actor=actor)
    # Trigger rules: the ones of version n, in its order (a rule that is the same stays, with its history).
    keep = []
    for r in conn.execute("SELECT * FROM pipeline_triggers WHERE pipeline_id=? ORDER BY id", (p["id"],)).fetchall():
        same = triggers.export_one(r)
        if same in old["triggers"] and same not in keep:
            keep.append(same)
        else:
            triggers.rule_remove(conn, name, f"t{r['id']}", actor=actor)
    for t in old["triggers"]:
        if t in keep:
            continue
        r = triggers.rule_add(conn, name, t["event"], does=t["does"], stage=t.get("stage"), tier=t.get("tier"),
                              runner=t.get("runner"), keys=",".join(t.get("keys") or []) or None, actor=actor)
        if not t.get("on", True):
            triggers.rule_switch(conn, name, r["name"], False, reason=f"as in version {n}", actor=actor)
    # Runners its rules may use.
    have = set(json.loads(store._pipeline(conn, name)["runners"] or "[]"))
    for r in sorted(set(old.get("runners") or []) - have):
        runners_mod.allow_pipeline(conn, name, r, True, actor=actor)
    for r in sorted(have - set(old.get("runners") or [])):
        runners_mod.allow_pipeline(conn, name, r, False, actor=actor)


def _form_pos(conn, p, stage):
    r = conn.execute("SELECT name FROM pipeline_stages WHERE pipeline_id=? AND removed_at IS NULL ORDER BY pos, id",
                     (p["id"],)).fetchall()
    names = [x["name"] for x in r]
    return names.index(stage) + 1 if stage in names else None
