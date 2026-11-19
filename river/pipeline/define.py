"""The definition of a pipeline: its stages and environments, changed by the maxpm pipeline commands (#6113).

mark 2026-11-18 (Decision 5 of canvas release-tracking): a pipeline is kept in MaximizePM and changed by commands, as
the hooks of a target are; maxpm pipeline export prints it as a JSON file to keep in git. Only a person or the
manager changes a pipeline, since its stages run commands and its transitions decide what reaches production. Each
change is a row of pipeline_changes with its actor (maxpm pipeline history).

The arrow into each stage is a transition (transitions.py, #6139). maxpm pipeline stage --gate (also --mode) and
maxpm pipeline gate set its mode: auto, yes [<name>|manager], delay <time>, release [every <time>], forge; the words of
the gates before it (auto after 30m, manager, person [<name>]) still work.
"""

import json
import re

from .. import core
from . import audit, store, transitions

_FIELDS = ("kind", "run", "workflow", "env", "mode", "junit", "waves", "check", "rollback", "wave_wait")


def parse_waves(text):
    """Servers in waves from 'web-1; web-2, web-3' (a ';' between waves, a ',' or a space between servers): the
    list of lists that pipeline_stages.waves keeps as JSON. '' clears it."""
    waves = [[h for h in re.split(r"[,\s]+", w.strip()) if h] for w in (text or "").split(";")]
    waves = [w for w in waves if w]
    for h in (h for w in waves for h in w):
        if not re.match(r"^[\w.@:-]{1,100}$", h):
            raise core.RiverError(f"the server name {h!r} has a character that is not a letter, digit, '.', '-', "
                                  "'_', '@' or ':'")
    return waves


def _may(conn, actor):
    why = core._lead_refusal(conn, actor, "changes a pipeline, because its stages run commands and its gates decide "
                                          "what reaches production")
    if why:
        raise core.RiverError(why)


def _change(conn, pid, actor, text, detail=None):
    """A change of the definition: a line of the audit log (#6144). It acts on the stage or the environment that
    the text names first, else on the pipeline."""
    m = re.match(r"(stage|environment) (\S+)", text)
    place = f"{m.group(1)} {m.group(2)}" if m else "the pipeline"
    audit.write(conn, pid, actor, "define", text, place=place, detail=detail)


def pipeline_add(conn, name, project=None, notes=None, actor=None):
    name = store._name(name, "pipeline")
    with core.tx(conn):
        _may(conn, actor)
        if conn.execute("SELECT 1 FROM pipelines WHERE name=?", (name,)).fetchone():
            raise core.RiverError(f"a pipeline named {name} exists already: maxpm pipeline show {name}")
        pid = conn.execute("INSERT INTO pipelines(name, project, notes, created_by, created_at) VALUES (?,?,?,?,?)",
                           (name, project, notes, actor, store._now())).lastrowid
        _change(conn, pid, actor, f"pipeline {name} added" + (f" for project {project}" if project else ""))
        from . import triggers
        triggers.add_default(conn, pid, actor)  # #6150: the rule 'any stage fails' (maxpm pipeline triggers)
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
    t = transitions.get(conn, s["id"])
    return {"name": s["name"], "pos": s["pos"], "kind": s["kind"], "run": s["run"], "workflow": s["workflow"],
            "env": env["name"] if env else None, "mode": transitions.mode_words(t["mode"], t["mode_arg"]),
            "junit": s["junit"],
            "waves": "; ".join(", ".join(w) for w in json.loads(s["waves"])) if s["waves"] else None,
            "check": s["check_cmd"], "rollback": s["rollback_cmd"], "wave_wait": s["wave_wait"]}


def stage_set(conn, pipeline, name, kind=None, run=None, workflow=None, env=None, gate=None, pos=None, remove=False,
              actor=None, junit=None, waves=None, check=None, rollback=None, wave_wait=None):
    """Add a stage (kind needed), or change the fields given ('' clears run, workflow or env), or move it (pos), or
    remove it (its runs stay; adding the name again brings it back). A stage runs a command (run) or a forge
    workflow (workflow), not both; a deploy stage names its environment."""
    name = store._name(name, "stage")
    if kind is not None and kind not in store.STAGE_KINDS:
        raise core.RiverError(f"the stage kind {kind!r} is not one of {', '.join(store.STAGE_KINDS)}")
    if run and workflow:
        raise core.RiverError("a stage runs a command (--run) or a forge workflow (--workflow), not both")
    rule = transitions.parse_mode(gate) if gate is not None else None
    with core.tx(conn):
        _may(conn, actor)
        p = store._pipeline(conn, pipeline)
        pid, row = p["id"], _stage_row(conn, p["id"], name)
        old = _stage_detail(conn, row) if row is not None else None  # before the mode changes below
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
        if junit is not None:  # a test stage's JUnit XML file, in the project folder (#6114)
            sets["junit"] = junit.strip() or None
        if waves is not None:  # a stage over many servers (#6120)
            w = parse_waves(waves)
            sets["waves"] = json.dumps(w) if w else None
        if check is not None:
            sets["check_cmd"] = check.strip() or None
        if rollback is not None:
            sets["rollback_cmd"] = rollback.strip() or None
        if wave_wait is not None:
            if wave_wait.strip():
                core.parse_duration(wave_wait.strip())
            sets["wave_wait"] = wave_wait.strip() or None
        if row is None:
            if kind is None:
                raise core.RiverError(f"a new stage needs its kind: --kind {'|'.join(store.STAGE_KINDS)}")
            sid = conn.execute("INSERT INTO pipeline_stages(pipeline_id, name, pos, kind, created_by, created_at) "
                               "VALUES (?,?, 0, ?,?,?)", (pid, name, kind, actor, store._now())).lastrowid
            changed = "added"
        else:
            sid = row["id"]
            changed = "added again" if row["removed_at"] else "changed"
            if changed == "changed" and not sets and pos is None and rule is None:
                raise core.RiverError(f"stage {name} exists; give what to change: --kind, --run, --workflow, --env, "
                                      "--gate or --pos")
            if row["removed_at"]:
                sets["removed_at"] = None
        if sets:
            conn.execute("UPDATE pipeline_stages SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?",
                         list(sets.values()) + [sid])
        if rule is not None:  # the mode of the transition into the stage (#6139)
            t = transitions.get(conn, sid)
            if rule != (t["mode"], t["mode_arg"]):
                transitions.set_mode(conn, pid, sid, *rule, actor)
        new = _stage_row(conn, pid, name)
        if new["kind"] == "deploy" and not new["env_id"]:
            raise core.RiverError(f"a deploy stage names its environment: maxpm pipeline stage {p['name']} {name} "
                                  "--env <name>")
        if row is None or row["removed_at"] or pos is not None:
            _place(conn, pid, sid, pos)
        detail = _stage_detail(conn, _stage_row(conn, pid, name))
        if changed == "changed":
            parts = [f"{k} {old[k] or 'none'} -> {detail[k] or 'none'}" for k in ("pos",) + _FIELDS
                     if old[k] != detail[k]]
        else:
            parts = [f"{k} {detail[k]}" for k in _FIELDS + ("pos",) if detail[k] and (k, detail[k]) != ("mode", "auto")]
        _change(conn, pid, actor, f"stage {name} {changed}" + (": " + ", ".join(parts) if parts else ""), detail)
    return {"pipeline": p["name"], "stage": name, "changed": changed, "detail": detail}


FORGES = ("github", "gitlab", "gitea", "forgejo", "pulumi", "none")
_SHAPES = {"gitlab": (r"^[\w.-]+(/[\w.-]+)+$", "group/project"),
           "pulumi": (r"^[\w.-]+/[\w.-]+/[\w.-]+$", "org/project/stack (the pipeline stack)")}


def forge_set(conn, pipeline, forge, repo=None, secret=None, ref=None, actor=None, url=None, settings=None,
              secrets=None):
    """maxpm pipeline forge: where the pipeline's workflows run (#6119, #6121, #6133). A forge needs the repository
    (owner/name; on GitLab group/subgroup/project; on Pulumi Delivery the pipeline stack org/project/stack) and the
    name of the maxpm secret that holds the API token; url: a self-hosted forge's address; ref: the branch (Pulumi
    Delivery: the number of the first run to read); none: the stages run here (their --run commands). Any other name
    is an installed plugin (#6159): plugin.use takes its settings and secrets."""
    if forge not in FORGES:
        from . import plugin
        return plugin.use(conn, pipeline, forge, settings, secrets if secrets is not None else
                          ([secret] if secret else None), repo, url, ref, actor)
    if secrets and len(secrets) > 1:
        raise core.RiverError(f"the {forge} adapter takes one secret: --secret <name>")
    secret = secret or (secrets[0] if secrets else None)
    if settings:
        raise core.RiverError(f"the {forge} adapter has no --set settings; a plugin has them")
    if forge != "none":
        shape, words = _SHAPES.get(forge, (r"^[\w.-]+/[\w.-]+$", "owner/name"))
        if not repo or not re.match(shape, repo):
            raise core.RiverError("name the repository: --repo " + words)
        if forge == "pulumi" and ref is not None and not re.match(r"^[1-9]\d*$", str(ref)):
            raise core.RiverError("--ref names the first Delivery run to read: a whole number, such as 120")
        if forge in ("gitea", "forgejo") and not url:
            raise core.RiverError(f"name the address of the {forge}: --url https://<host> (Codeberg: "
                                  "https://codeberg.org)")
        if not secret:
            raise core.RiverError(f"name the maxpm secret that holds the {forge} token: --secret <name> (a person "
                                  "saves it: maxpm secret put <name>)")
        if url and not re.match(r"^https://[\w.-]+(:\d+)?(/[\w.-]+)*/?$", url):
            raise core.RiverError("the forge's address is https://<host>[/<path>]")
    with core.tx(conn):
        _may(conn, actor)
        p = store._pipeline(conn, pipeline)
        vals = (None,) * 5 if forge == "none" else (forge, repo, secret, ref, url)
        conn.execute("UPDATE pipelines SET forge=?, repo=?, secret=?, ref=?, forge_url=?, polled_at=NULL, "
                     "poll_error=NULL, plugin_settings=NULL, plugin_state=NULL WHERE id=?", (*vals, p["id"]))
        text = "forge none: the stages run here" if forge == "none" else (
            f"forge {forge}: repository {repo}" + (f" at {url}" if url else "") + f", token from secret {secret}"
            + (f", branch {ref or 'main'}" if forge not in ("gitlab", "pulumi") else "")
            + (f", runs read from run {ref or 1} on" if forge == "pulumi" else ""))
        _change(conn, p["id"], actor, text)
    return {"pipeline": p["name"], "forge": forge, "repo": repo, "secret": secret, "ref": ref, "url": url}


def gate_set(conn, pipeline, stage, rule, actor=None):
    """maxpm pipeline gate: the mode of the transition into a stage (the words of transitions.parse_mode)."""
    return stage_set(conn, pipeline, stage, gate=rule, actor=actor)


def export(conn, name):
    """The definition of a pipeline as one JSON object, to keep in git: its environments, its stages in order, and
    its trigger rules (#6150)."""
    from . import triggers
    p = store._pipeline(conn, name)
    envs = [{k: e[k] for k in ("name", "target", "url", "notes")} for e in conn.execute(
        "SELECT * FROM pipeline_envs WHERE pipeline_id=? AND removed_at IS NULL ORDER BY id", (p["id"],))]
    stages = []
    for s in conn.execute("SELECT * FROM pipeline_stages WHERE pipeline_id=? AND removed_at IS NULL ORDER BY pos, id",
                          (p["id"],)):
        d = _stage_detail(conn, s)
        d.pop("pos")
        d.pop("mode")
        d["transition"] = transitions.export(conn, s["id"])  # the arrow into the stage (#6139)
        stages.append(d)
    return {"pipeline": p["name"], "project": p["project"], "notes": p["notes"],
            "forge": {"name": p["forge"], "repo": p["repo"], "secret": p["secret"], "ref": p["ref"],
                      "url": p["forge_url"], **({"plugin": json.loads(p["plugin_settings"])}
                                                if p["plugin_settings"] else {})} if p["forge"] else None,
            "environments": envs, "stages": stages, "triggers": triggers.export(conn, p["id"])}


def history(conn, name, limit=50):
    """The changes of a pipeline's definition, newest first."""
    p = store._pipeline(conn, name)
    return [dict(r) for r in conn.execute("SELECT at, actor, change FROM pipeline_changes WHERE pipeline_id=? "
                                          "ORDER BY id DESC LIMIT ?", (p["id"], limit))]
