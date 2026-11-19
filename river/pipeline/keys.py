"""Keys for agents with no person (#6153): which credential sets a stage, a tier or a runner may read.

mark 2026-11-18 (canvas release-tracking, goal part 2, task H; from the Pulumi comparison: "only the production stage
reads production tokens"). An agent that a trigger rule starts on a runner has no person's computer, browser sign-in or
Keychain prompt. Its keys are maxpm secrets in credential sets (river/vault.py) that a person grants to the place where
it works:

    maxpm secret grant <set> --stage <pipeline>/<stage>   the stage may read the set: its command gets the secrets
    maxpm secret grant <set> --tier <pipeline>/<tier>     the same for each stage of that tier (every prod stage)
    maxpm secret grant <set> --runner <runner>            each agent run on that runner gets them (the model key)

The rules:
- A set granted to stages or tiers of a pipeline is a stage key of that pipeline: only those stages read it. A trigger
  rule that names it gives it only to a task of one of those stages, and holds it back for a task of another stage
  (the rule's result says so). So only a prod stage, and an agent on a prod stage's task, reads a prod key.
- A stage's command on this computer gets the secrets of its stage's and its tier's sets as variables (each secret's
  variable, else its name in capitals, '-' and '.' as '_'), and MaximizePM masks them in its output. It gets no other
  secret.
- A run on a runner gets the runner's sets (the model key) and the sets that its rule granted to its task, as variables
  in the script on stdin (never on a command line); its transcript masks them.
- Never a person's sign-in: a set that holds a saved sign-in or browser cookies (secrets.kind signin, cookies) is
  refused for a stage, a tier or a runner, and such a secret added to the set later is left out.
- Keys that the deployment system keeps itself (GitHub environment secrets, a Pulumi ESC environment) are shown by
  name only: a plugin's answer names them ("secrets"), or a person names them (maxpm pipeline keys <p> <stage>
  --kept-in "..." --names a,b). MaximizePM never reads their values.

Who: a person grants and revokes; the manager only a set whose credentials the person allows without asking
(vault._may_grant, #5842). An operator's emergency Revoke ends a pipeline's stage keys too (operators.revoke).

Standard library only."""

from __future__ import annotations

import json
import re

from .. import core
from . import audit, store

SCOPES = ("stage", "tier", "runner")
PERSON_KINDS = ("signin", "cookies")  # secrets.kind of a person's sign-in: never a key of a stage or a runner
KEPT_MAX = 100  # names that one stage's outside store may list


def var_of(name, var=None):
    """The variable that carries a secret: its own (maxpm secret level <name> --var X), else the name in capitals."""
    if var:
        return var
    key = re.sub(r"[^A-Za-z0-9_]", "_", name).upper()
    return "_" + key if key[:1].isdigit() else key


def _set(conn, name):
    from .. import vault
    return vault._set(conn, name)


def _members(conn, set_name):
    return [r["name"] for r in conn.execute(
        "SELECT m.name FROM secret_set_members m JOIN secret_sets s ON s.id=m.set_id WHERE s.name=? ORDER BY m.name",
        (set_name,))]


def _person_signins(conn, set_name):
    """The secrets of the set that are a person's sign-in (a password with its site, or browser cookies)."""
    return [r["name"] for r in conn.execute(
        "SELECT x.name FROM secret_set_members m JOIN secret_sets s ON s.id=m.set_id JOIN secrets x ON x.name=m.name "
        "WHERE s.name=? AND x.kind IN (%s) ORDER BY x.name" % ",".join("?" * len(PERSON_KINDS)),
        (set_name, *PERSON_KINDS))]


def _live_stages(conn, pid):
    return conn.execute("SELECT * FROM pipeline_stages WHERE pipeline_id=? AND removed_at IS NULL ORDER BY pos, id",
                        (pid,)).fetchall()


def place(conn, scope, value):
    """(pipeline row or None, the stored value) of a key's place; refused when it names nothing."""
    if scope not in SCOPES:
        raise core.RiverError(f"a key goes to one of {', '.join(SCOPES)}, not {scope!r}")
    value = str(value or "").strip()
    if scope == "runner":
        if not conn.execute("SELECT 1 FROM runners WHERE name=?", (value,)).fetchone():
            known = [r["name"] for r in conn.execute("SELECT name FROM runners ORDER BY name")]
            raise core.RiverError(f"no runner {value!r}; " + (f"runners: {', '.join(known)}" if known else
                                                               "add one: maxpm runner add <name> --ssh <target>"))
        return None, value
    if "/" not in value:
        raise core.RiverError(f"name the {scope} with its pipeline: --{scope} <pipeline>/<{scope}>, for example "
                              f"--{scope} shop/{'prod' if scope == 'tier' else 'deploy'}")
    name, what = (x.strip() for x in value.split("/", 1))
    p = store._pipeline(conn, name)
    stages = _live_stages(conn, p["id"])
    if scope == "stage" and not any(s["name"] == what for s in stages):
        raise core.RiverError(f"pipeline {p['name']} has no stage {what!r}; its stages: "
                              f"{', '.join(s['name'] for s in stages) or 'none'}")
    if scope == "tier" and not any(s["tier"] == what for s in stages):
        tiers = sorted({s["tier"] for s in stages if s["tier"]})
        raise core.RiverError(f"no stage of pipeline {p['name']} has the tier {what!r}; its tiers: "
                              f"{', '.join(tiers) or 'none yet (maxpm pipeline stage <name> <stage> --tier <tier>)'}")
    return p, what


def places(conn):
    """The choices of a key's place for the Credentials page: {stage: ['<pipeline>/<stage>'], tier: [...], runner}."""
    stages, tiers = [], []
    for s in conn.execute("SELECT p.name pipeline, s.name, s.tier FROM pipeline_stages s JOIN pipelines p ON "
                          "p.id=s.pipeline_id WHERE s.removed_at IS NULL ORDER BY p.name, s.pos, s.id"):
        stages.append(f"{s['pipeline']}/{s['name']}")
        if s["tier"] and f"{s['pipeline']}/{s['tier']}" not in tiers:
            tiers.append(f"{s['pipeline']}/{s['tier']}")
    return {"stage": stages, "tier": tiers,
            "runner": [r["name"] for r in conn.execute("SELECT name FROM runners ORDER BY name")]}


def _text(conn, k):
    """Where a key grant goes, in words."""
    if k["scope"] == "runner":
        return f"runner {k['value']}"
    p = conn.execute("SELECT name FROM pipelines WHERE id=?", (k["pipeline_id"],)).fetchone()
    pname = p["name"] if p else f"pipeline {k['pipeline_id']}"
    return f"stage {pname}/{k['value']}" if k["scope"] == "stage" else f"every {k['value']} stage of {pname}"


def info(conn, k):
    """A key grant in the shape of vault._grant_info, so maxpm secret grants and the page list both kinds."""
    return {"id": k["id"], "grant": f"k{k['id']}", "set": k["set_name"], "scope": k["scope"], "value": k["value"],
            "pipeline": (conn.execute("SELECT name FROM pipelines WHERE id=?", (k["pipeline_id"],)).fetchone()
                         or {"name": None})["name"] if k["pipeline_id"] else None,
            "covers": _text(conn, k), "note": k["note"], "granted_by": k["granted_by"], "granted_at": k["granted_at"],
            "until": None, "active": not k["revoked_at"],
            "ended": (f"revoked {core.show_time(k['revoked_at'])} by {k['revoked_by'] or 'a person'}"
                      if k["revoked_at"] else None)}


def _line(conn, k, actor, action, text, reason=None):
    """A line of the pipeline's audit log for a stage or tier key (inside the caller's tx)."""
    if k["pipeline_id"]:
        audit.write(conn, k["pipeline_id"], actor, action, text, place=f"{k['scope']} {k['value']}", reason=reason,
                    detail={"key": f"k{k['id']}", "set": k["set_name"]})
        from . import versions  # a numbered version of the definition: its keys are part of it (#6143)
        versions.snapshot(conn, k["pipeline_id"], actor, text + (f": {reason}" if reason else ""))


def grant(conn, set_name, scope, value, note=None, actor=None):
    """Grant a set to a stage, a tier or a runner, until a revoke. A person, or the manager for a set whose
    credentials the person allows without asking."""
    from .. import vault
    s = _set(conn, set_name)
    actor = vault._may_grant(conn, actor, s, "grant")["name"] or actor
    p, v = place(conn, scope, value)
    signins = _person_signins(conn, set_name)
    if signins:
        raise core.RiverError(f"the set {set_name} holds a person's sign-in ({', '.join(signins)}): an agent with no "
                              f"person never gets one. Put the {scope}'s keys in a set of tokens and keys only")
    pid = p["id"] if p else None
    with core.tx(conn):
        old = conn.execute("SELECT * FROM pipeline_keys WHERE set_name=? AND scope=? AND value=? AND "
                           "pipeline_id IS ? AND revoked_at IS NULL", (set_name, scope, v, pid)).fetchone()
        if old:
            return {**info(conn, old), "already": True}
        kid = conn.execute("INSERT INTO pipeline_keys (set_name, scope, pipeline_id, value, note, granted_by, "
                           "granted_at) VALUES (?,?,?,?,?,?,?)",
                           (set_name, scope, pid, v, note, actor, core.iso(core.now()))).lastrowid
        k = conn.execute("SELECT * FROM pipeline_keys WHERE id=?", (kid,)).fetchone()
        _line(conn, k, actor, "key", f"key k{kid}: {_text(conn, k)} may read the set {set_name} "
              f"({', '.join(_members(conn, set_name)) or 'empty'})", reason=note)
    return {**info(conn, k), "already": False}


def _row(conn, ref):
    t = str(ref).strip().lstrip("kK#")
    k = conn.execute("SELECT * FROM pipeline_keys WHERE id=?", (int(t),)).fetchone() if t.isdigit() else None
    if k is None:
        raise core.RiverError(f"no key grant {ref}: maxpm secret grants lists them (k<n>)")
    return k


def revoke(conn, ref=None, set_name=None, scope=None, value=None, actor=None, reason=None, vouched=False,
           pipeline_id=None):
    """End key grants: one by its id (k3), or a set's grants to one place, or (scope None) all of a set's key
    grants (pipeline_id: only that pipeline's stages and tiers). vouched: an operator's emergency Revoke
    (operators.revoke checked who)."""
    from .. import vault
    if ref is not None:
        rows = [_row(conn, ref)]
    else:
        q, args = "SELECT * FROM pipeline_keys WHERE set_name=?", [set_name]
        if scope:
            p, v = place(conn, scope, value)
            q += " AND scope=? AND value=? AND pipeline_id IS ?"
            args += [scope, v, p["id"] if p else None]
        elif pipeline_id is not None:
            q += " AND pipeline_id=?"
            args.append(pipeline_id)
        rows = conn.execute(q, args).fetchall()
    if not vouched:
        for name in {k["set_name"] for k in rows} or {set_name}:
            s = conn.execute("SELECT * FROM secret_sets WHERE name=?", (name,)).fetchone()
            if s is not None:  # a deleted set: its key grants give nothing; anyone who may revoke ends them
                actor = vault._may_grant(conn, actor, s, "revoke")["name"] or actor
    live = [k for k in rows if not k["revoked_at"]]
    with core.tx(conn):
        at = core.iso(core.now())
        for k in live:
            conn.execute("UPDATE pipeline_keys SET revoked_at=?, revoked_by=?, revoke_reason=? WHERE id=?",
                         (at, actor or "a person", reason, k["id"]))
            _line(conn, k, actor, "revoke", f"key k{k['id']} revoked: {_text(conn, k)} no longer reads the set "
                  f"{k['set_name']}", reason=reason)
    return {"revoked": [info(conn, conn.execute("SELECT * FROM pipeline_keys WHERE id=?", (k["id"],)).fetchone())
                        for k in live]}


def grants(conn, set_name=None, ended=False, pipeline=None, runner=None):
    q, args, where = "SELECT * FROM pipeline_keys", [], []
    if set_name:
        where.append("set_name=?")
        args.append(set_name)
    if not ended:
        where.append("revoked_at IS NULL")
    if pipeline:
        where.append("pipeline_id=?")
        args.append(store._pipeline(conn, pipeline)["id"])
    if runner:
        where.append("scope='runner' AND value=?")
        args.append(runner)
    q += (" WHERE " + " AND ".join(where)) if where else ""
    return [info(conn, k) for k in conn.execute(q + " ORDER BY id", args)]


# ---------------------------------------------------------------- what each stage and run may read

def _gone(conn, set_name):
    return conn.execute("SELECT 1 FROM secret_sets WHERE name=?", (set_name,)).fetchone() is None


def stage_sets(conn, pid, stage, tier=None):
    """The sets that a stage may read now: [{set, grant, by: 'stage' | 'tier'}], each set once."""
    out, seen = [], set()
    for k in conn.execute("SELECT * FROM pipeline_keys WHERE pipeline_id=? AND revoked_at IS NULL AND "
                          "((scope='stage' AND value=?) OR (scope='tier' AND value=?)) ORDER BY scope, id",
                          (pid, stage, tier or "\x00")):
        if k["set_name"] not in seen and not _gone(conn, k["set_name"]):
            seen.add(k["set_name"])
            out.append({"set": k["set_name"], "grant": f"k{k['id']}", "by": k["scope"]})
    return out


def bound(conn, pid):
    """The stage keys of a pipeline: {set: the names of the live stages that may read it}."""
    stages = _live_stages(conn, pid)
    out = {}
    for k in conn.execute("SELECT * FROM pipeline_keys WHERE pipeline_id=? AND revoked_at IS NULL ORDER BY id", (pid,)):
        names = [s["name"] for s in stages if (s["name"] if k["scope"] == "stage" else s["tier"]) == k["value"]]
        out.setdefault(k["set_name"], [])
        out[k["set_name"]] += [n for n in names if n not in out[k["set_name"]]]
    return out


def held_back(conn, pid, set_name, stage):
    """Why a task of this stage may not get the set (it is a key of other stages of the pipeline only), or None."""
    where = bound(conn, pid).get(set_name)
    if where is None or stage in where:
        return None
    return f"{set_name} is a key of {', '.join(where) or 'no live stage'} only"


def rule_check(conn, p, keys, stage=None, tier=None, runner=None):
    """A trigger rule may name a set only where its tasks may read it: refused for a rule of one stage (or tier)
    that none of whose stages may read a stage key, and for a rule on a runner, a set with a person's sign-in."""
    marks = bound(conn, p["id"])
    watched = [s["name"] for s in _live_stages(conn, p["id"])
               if (stage and s["name"] == stage) or (tier and s["tier"] == tier)] if (stage or tier) else None
    for k in keys:
        if watched is not None and k in marks and not set(watched) & set(marks[k]):
            raise core.RiverError(f"the set {k} is a key of stage{'s' if len(marks[k]) > 1 else ''} "
                                  f"{', '.join(marks[k]) or '(none live)'} of {p['name']} only, and this rule watches "
                                  f"{'stage ' + stage if stage else 'the ' + tier + ' stages'}: an agent on a stage "
                                  f"gets only that stage's keys")
        if runner and runner != "this computer":
            signins = _person_signins(conn, k)
            if signins:
                raise core.RiverError(f"the set {k} holds a person's sign-in ({', '.join(signins)}): an agent on "
                                      f"runner {runner} never gets one")


def rule_keys(conn, pid, keys, runner=None):
    """What a rule's keys are, for its card: [{set, stages (None: any stage)}], then the runner's sets."""
    marks = bound(conn, pid)
    out = [{"set": k, "stages": marks.get(k)} for k in keys]
    if runner and runner != "this computer":
        out += [{"set": k["set"], "stages": None, "runner": runner} for k in grants(conn, runner=runner)
                if k["set"] not in keys]
    return out


def _values(conn, sets, who, use, argv):
    """The secrets of the sets as variables: (env, mask forms, given [{secret, var, set}], left [why])."""
    from .. import vault
    env, forms, given, left = {}, [], [], []
    for set_name in sets:
        for name in _members(conn, set_name):
            row = conn.execute("SELECT * FROM secrets WHERE name=?", (name,)).fetchone()
            if row is None:
                continue
            if row["kind"] in PERSON_KINDS:
                left.append(f"{name}: a person's sign-in is never given to {who}")
                continue
            why = vault.check_level(row, "run", argv)
            if why:
                left.append(why)
                continue
            var = var_of(name, row["var"])
            if any(g["var"] == var and g["secret"] != name for g in given):
                left.append(f"{name}: the variable {var} holds another secret already "
                            f"(maxpm secret level {name} --var <another>)")
                continue
            try:
                data = vault.backend().get(conn, name)
            except core.RiverError as e:
                left.append(f"{name}: {e}")
                continue
            if data is None:
                left.append(f"{name}: its value is gone from the {vault.backend().name} store (maxpm secret put {name})")
                continue
            env[var] = data.decode("utf-8", "replace")
            forms += vault.mask_forms(data)
            if not any(g["secret"] == name for g in given):
                given.append({"secret": name, "var": var, "set": set_name})
                with core.tx(conn):
                    conn.execute("UPDATE secrets SET used_at=?, used_by=?, uses=uses+1 WHERE name=?",
                                 (core.iso(core.now()), who, name))
                    vault._log(conn, name, who, "use", f"{use}; set {set_name}")
    return env, sorted(set(forms), key=len, reverse=True), given, left


def stage_env(conn, run):
    """The variables of a stage run's command on this computer: the secrets of its stage's and tier's sets.
    run: a row of runner._row. Returns (env, mask forms, given, left)."""
    s = conn.execute("SELECT name, tier FROM pipeline_stages WHERE id=?", (run["stage_id"],)).fetchone()
    sets = [x["set"] for x in stage_sets(conn, run["pipeline_id"], s["name"], s["tier"])] if s else []
    if not sets:
        return {}, [], [], []
    try:
        import shlex
        argv = shlex.split(run["cmd"] or "")
    except ValueError:
        argv = (run["cmd"] or "").split()
    return _values(conn, sets, f"stage {run['pipeline']}/{s['name']}", f"run #{run['id']} of stage {s['name']}", argv)


def run_env(conn, run, cli):
    """The variables of an agent run on a runner: the runner's sets and the sets granted to its task alone.
    Returns (env, mask forms, given, left)."""
    from .. import vault
    sets = [k["set"] for k in grants(conn, runner=run["runner"])]
    at = core.iso(core.now())
    for g in conn.execute("SELECT g.*, s.name set_name FROM secret_grants g JOIN secret_sets s ON s.id=g.set_id "
                          "WHERE g.scope='item' AND g.value=? ORDER BY g.id", (str(run["item"]),)):
        if not vault._grant_end(conn, g, at) and g["set_name"] not in sets:
            sets.append(g["set_name"])
    if not sets:  # _values leaves out a person's sign-in, whatever granted its set, and says so

        return {}, [], [], []
    return _values(conn, sets, f"runner run #{run['id']}", f"run #{run['id']} on runner {run['runner']} "
                   f"(task #{run['item']})", [cli])


def mask(forms, text):
    """The text with every form of the given values replaced by ***."""
    for f in forms:
        s = f.decode("utf-8", "replace") if isinstance(f, bytes) else f
        if s:
            text = text.replace(s, "***")
    return text


# ---------------------------------------------------------------- keys that the deployment system keeps

def _clean_names(names):
    if isinstance(names, str):
        names = [x for x in re.split(r"[,\s]+", names) if x]
    if not isinstance(names, list) or not all(isinstance(x, str) for x in names):
        raise core.RiverError("names is a list of the secrets' names")
    names = [x.strip()[:200] for x in names if x.strip()]
    if len(names) > KEPT_MAX:
        raise core.RiverError(f"at most {KEPT_MAX} names for one stage")
    return sorted(dict.fromkeys(names))


def kept(conn, pipeline, stage, kept_in, names, actor=None):
    """maxpm pipeline keys <p> <stage> --kept-in "<where>" --names a,b: the names of the keys that the deployment
    system keeps for a stage (no names: forget that store). A person or the manager."""
    from . import define
    define._may(conn, actor)
    p = store._pipeline(conn, pipeline)
    if not any(s["name"] == stage for s in _live_stages(conn, p["id"])):
        raise core.RiverError(f"pipeline {p['name']} has no stage {stage!r}")
    kept_in = (kept_in or "").strip()
    if not kept_in:
        raise core.RiverError("say where the system keeps them: --kept-in \"Pulumi ESC environment org/proj/prod\"")
    names = _clean_names(names or [])
    with core.tx(conn):
        _keep(conn, p["id"], stage, kept_in, names, "person", actor)
        audit.write(conn, p["id"], actor, "key", f"stage {stage}: {kept_in} keeps "
                    + (", ".join(names) if names else "nothing that MaximizePM shows"), place=f"stage {stage}")
    return {"pipeline": p["name"], "stage": stage, "kept_in": kept_in, "names": names}


def _keep(conn, pid, stage, kept_in, names, source, actor):
    if names:
        conn.execute("INSERT OR REPLACE INTO pipeline_kept_keys (pipeline_id, stage, kept_in, names, source, at, by) "
                     "VALUES (?,?,?,?,?,?,?)", (pid, stage, kept_in, json.dumps(names), source, core.iso(core.now()),
                                                actor))
    else:
        conn.execute("DELETE FROM pipeline_kept_keys WHERE pipeline_id=? AND stage=? AND kept_in=?",
                     (pid, stage, kept_in))


def take_plugin(conn, p, plugin_name, items):
    """A plugin answer's "secrets": [{"stage", "kept_in", "names"}], names only. It replaces what this plugin said
    before for that stage and store."""
    if not isinstance(items, list) or not all(isinstance(x, dict) and isinstance(x.get("stage"), str)
                                              and isinstance(x.get("kept_in"), str) for x in items):
        raise core.RiverError(f"plugin {plugin_name}: \"secrets\" is a list of {{\"stage\": <name>, \"kept_in\": "
                              "<where>, \"names\": [<name>, ...]}}")
    with core.tx(conn):
        for x in items[:200]:
            names = _clean_names(x.get("names") or [])
            row = conn.execute("SELECT source FROM pipeline_kept_keys WHERE pipeline_id=? AND stage=? AND kept_in=?",
                               (p["id"], x["stage"], x["kept_in"][:200])).fetchone()
            if row and row["source"] == "person":
                continue  # a person's list stays
            _keep(conn, p["id"], x["stage"], x["kept_in"][:200], names, plugin_name, f"plugin {plugin_name}")
    return len(items)


# ---------------------------------------------------------------- the view

def view(conn, pipeline):
    """Each stage's keys, for the stage cards and maxpm pipeline keys: {stage: {sets: [{set, grant, by, secrets}],
    secrets: the names, kept: [{kept_in, names, source}]}}, the runners' keys and the trigger rules' keys."""
    p = store._pipeline(conn, pipeline)
    out = {}
    for s in _live_stages(conn, p["id"]):
        sets = [dict(x, secrets=_members(conn, x["set"])) for x in stage_sets(conn, p["id"], s["name"], s["tier"])]
        out[s["name"]] = {"tier": s["tier"], "sets": sets,
                          "secrets": sorted({n for x in sets for n in x["secrets"]}), "kept": []}
    for r in conn.execute("SELECT * FROM pipeline_kept_keys WHERE pipeline_id=? ORDER BY stage, kept_in", (p["id"],)):
        if r["stage"] in out:
            out[r["stage"]]["kept"].append({"kept_in": r["kept_in"], "names": json.loads(r["names"]),
                                            "source": r["source"], "at": r["at"]})
    from .. import runners
    run_keys = {name: [k["set"] for k in grants(conn, runner=name)] for name in runners.ticked(p)}
    rules = [{"rule": f"t{r['id']}", "runner": r["runner"],
              "keys": rule_keys(conn, p["id"], [k for k in (r["keys"] or "").split(",") if k], r["runner"])}
             for r in conn.execute("SELECT * FROM pipeline_triggers WHERE pipeline_id=? ORDER BY id", (p["id"],))]
    return {"pipeline": p["name"], "stages": out, "runners": run_keys, "rules": rules}


def for_cards(conn, pipeline):
    """{stage: what its card shows}: the set names and secret names MaximizePM gives it, and the outside names."""
    try:
        v = view(conn, pipeline)["stages"]
    except core.RiverError:
        return {}
    return {name: {"sets": [x["set"] for x in s["sets"]], "secrets": s["secrets"], "detail": s["sets"],
                   "kept": s["kept"]} for name, s in v.items()}
