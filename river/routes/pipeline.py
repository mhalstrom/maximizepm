"""The pipeline view of Plan (#6116): the reads that river/static/views/pipeline.js draws. The actions (hold, approve,
roll back, run again) come with #6117.

GET /api/pipelines            the pipelines, and each deploy target that no pipeline covers (its default pipeline)
GET /api/pipeline/<name>      one pipeline: its stages with their last run, its environments, and its versions
GET /api/pipeline-run/<id>    one stage run: its steps, its tests (failed first), its last lines, and the prompt that
                              analyzes its failed tests"""

import json
from urllib.parse import unquote

from .. import core
from ..core import RiverError
from ..pipeline import prompt, store, targets

TESTS_MAX = 2000  # the test results one read sends


def _with(fn, query=False):
    """A GET handler of fn(conn, path); query: fn gets the path with its query string (the frame cuts it off)."""
    def handler(h, path):
        conn = core.connect()
        try:
            return h._send(200, fn(conn, h.path if query else path))
        except (RiverError, ValueError) as e:
            return h._send(404, {"error": str(e)})
        finally:
            conn.close()
    return handler


def pipelines(conn, path):
    out = []
    for p in store.pipelines(conn):
        envs = [r["target"] for r in conn.execute("SELECT target FROM pipeline_envs WHERE pipeline_id=? AND target IS "
                                                  "NOT NULL AND removed_at IS NULL", (p["id"],))]
        out.append({"name": p["name"], "project": p["project"], "default": False, "targets": envs})
    for t in targets.default_list(conn):
        projects = [r["name"] for r in conn.execute("SELECT name FROM projects WHERE target=? AND archived=0 AND name<>? "
                                                    "ORDER BY rank, id", (t["name"], f"deploy-{t['name']}"))]
        out.append({"name": t["name"], "project": projects[0] if projects else None, "projects": projects,
                    "default": True, "targets": [t["name"]]})
    return {"pipelines": out}


def pipeline(conn, path):
    name = unquote(path.split("/api/pipeline/", 1)[1])
    if conn.execute("SELECT 1 FROM pipelines WHERE name=?", (name,)).fetchone():
        p = store.show(conn, name)
        p["versions"] = store.versions(conn, name)
        return p
    p = targets.default_pipeline(conn, name)
    p["versions"] = [{"rev": r["version"], "deploy": r["deploy"], "stages": r["stages"]} for r in p.pop("releases")]
    return p


def run(conn, path):
    run_id = int(path.rsplit("/", 1)[1])
    r = store.run_show(conn, run_id)
    r["test_list"] = store.tests(conn, run_id, limit=TESTS_MAX)
    if r["tests"].get("failed") or r["tests"].get("error"):
        r["prompt"] = prompt.failure_prompt(conn, run_id)["text"]
    return r


GET = [
    (lambda path: path == "/api/pipelines", _with(pipelines)),
    (lambda path: path.startswith("/api/pipeline-run/"), _with(run)),
    (lambda path: path.startswith("/api/pipeline/"), _with(pipeline)),
]

def _runner():
    from ..pipeline import runner
    return runner


# The page's pipeline actions (#6117), as the person chosen in "You are": each one is the maxpm pipeline command of the
# same name, with its checks (only a person or the manager acts) and a line in the pipeline's history.
OPS = {
    "pipeline_hold": lambda c, a, who: _runner().hold(c, a["pipeline"], a.get("stage"), a.get("reason"), who),
    "pipeline_unhold": lambda c, a, who: _runner().release_hold(c, a["pipeline"], a.get("stage"), who),
    "pipeline_approve": lambda c, a, who: _runner().approve(c, a["pipeline"], a["stage"], a.get("rev"), who),
    "pipeline_rerun": lambda c, a, who: _runner().run_again(c, a["pipeline"], a["stage"], a["rev"], who),
    "pipeline_rerun_failed": lambda c, a, who: _runner().rerun_failed(c, int(a["run"]), who),
    "pipeline_rollback": lambda c, a, who: _runner().rollback(c, a["pipeline"], a["env"], a["rev"], a.get("reason"), who),
    "pipeline_stop": lambda c, a, who: _runner().stop(c, int(a["run"]), who),
    # #6118: the prompt of a failed run as a fix task, and with start a session on it
    "pipeline_fix": lambda c, a, who: _links().fix_task(c, int(a["run"]), who, bool(a.get("start"))),
    # #6139: the controls of a transition (the arrow into a stage)
    "pipeline_transition": lambda c, a, who: _transitions().change(
        c, a["pipeline"], a["stage"], a.get("mode"), a.get("window"),
        None if a.get("fault_rule") is None else bool(a["fault_rule"]), a.get("reason"), who),
    "pipeline_block": lambda c, a, who: _transitions().block(c, a["pipeline"], a["stage"], a.get("reason"),
                                                             a.get("until") or None, a.get("until_done") or None, who),
    "pipeline_unblock": lambda c, a, who: _transitions().unblock(c, a["pipeline"], a["stage"], who),
    "pipeline_delay": lambda c, a, who: _transitions().delay(c, a["pipeline"], a["stage"], a.get("until"),
                                                             a.get("reason"), who),
    "pipeline_disable": lambda c, a, who: _transitions().disable(c, a["pipeline"], a["stage"], a.get("reason"), who),
    "pipeline_enable": lambda c, a, who: _transitions().enable(c, a["pipeline"], a["stage"], who),
    "pipeline_pass": lambda c, a, who: _transitions().let_pass(c, a["pipeline"], a["stage"], a.get("rev"),
                                                               a.get("reason"), who),
    "pipeline_freeze": lambda c, a, who: _transitions().freeze(c, a["pipeline"], a.get("stage") or None,
                                                               a.get("from") or None, a.get("to"), a.get("reason"),
                                                               bool(a.get("clear")), who),
}


def _transitions():
    from ..pipeline import transitions
    return transitions


def _links():
    from ..pipeline import links
    return links


def version(conn, path):
    """GET /api/pipeline-version?pipeline=<name>&rev=<commit>: the tasks of a version (#6118)."""
    from urllib.parse import parse_qs, urlsplit
    q = parse_qs(urlsplit(path).query)
    if not q.get("pipeline") or not q.get("rev"):
        raise RiverError("name the pipeline and the version: ?pipeline=<name>&rev=<commit>")
    return _links().version_tasks(conn, q["pipeline"][0], q["rev"][0])


def item(conn, path):
    """GET /api/pipeline-item/<id>: where a task runs (#6118)."""
    return _links().item_versions(conn, int(path.rsplit("/", 1)[1]))


GET += [
    (lambda path: path == "/api/pipeline-version", _with(version, query=True)),
    (lambda path: path.startswith("/api/pipeline-item/"), _with(item)),
]


# ---------------------------------------------------------------- the Deploy tab (#6136)

SOURCES = {"github": "GitHub", "gitlab": "GitLab", "gitea": "Gitea", "forgejo": "Forgejo", "pulumi": "Pulumi Delivery"}
AUDIT_MAX = 500  # the lines of the audit log one read sends


def _source(p, saved):
    """Where a pipeline's runs come from, and the state of the token it reads them with: local (this computer),
    missing (no secret named, or the named one is not saved), bad (the forge refused the last read), new (not read
    yet), ok."""
    if not p.get("forge"):
        return {"forge": None, "label": "this computer", "state": "local"}
    secret = p.get("secret")
    if p.get("plugin_settings"):  # a plugin (#6159): the secrets its manifest needs, mapped to maxpm secrets; none is ok
        names = list((json.loads(p["plugin_settings"]).get("secrets") or {}).values())
        lacking = [n for n in names if n not in saved]
        state = ("missing" if lacking else "bad" if p.get("poll_error") else "ok" if p.get("polled_at") else "new")
        return {"forge": p["forge"], "plugin": True, "label": f"plugin {p['forge']}", "repo": p.get("repo"),
                "url": p.get("forge_url"),
                "secret": lacking[0] if lacking else secret, "state": state, "error": p.get("poll_error"),
                "read_at": p.get("polled_at")}
    state = ("missing" if not secret or secret not in saved else "bad" if p.get("poll_error")
             else "ok" if p.get("polled_at") else "new")
    return {"forge": p["forge"], "label": SOURCES.get(p["forge"], p["forge"]), "repo": p.get("repo"),
            "url": p.get("forge_url"), "secret": secret, "state": state, "error": p.get("poll_error"),
            "read_at": p.get("polled_at")}


def _strip(stages):
    """The stages of a pipeline as the Deploy list draws them: a bar for each, the state of its last run."""
    out = []
    for s in stages:
        r = s.get("last_run") or {}
        t = s.get("transition") or {}
        out.append({"name": s["name"], "kind": s["kind"], "env": s.get("env"),
                    "transition": {k: t.get(k) for k in ("mode", "arg", "text", "state", "state_text", "set_by",
                                                         "until")},
                    "state": r.get("state") or "none", "version": r.get("version"), "fault": r.get("fault_item"),
                    "run": r.get("id"), "by_source": bool(s.get("workflow") and not s.get("run"))})
    return out


def _person_waits(conn, pipeline_id):
    """The versions that wait at a transition for a person's yes: not approved, and the transition not disabled or
    blocked (#6139)."""
    return [{"stage": r["stage"], "rev": r["rev"], "run": r["id"], "item": r["gate_item"], "since": r["queued_at"],
             "note": r["gate_note"]} for r in conn.execute(
        "SELECT r.id, r.gate_note, r.gate_item, r.queued_at, s.name stage, v.rev FROM stage_runs r "
        "JOIN pipeline_stages s ON s.id=r.stage_id JOIN pipeline_transitions t ON t.stage_id=s.id "
        "LEFT JOIN pipeline_versions v ON v.id=r.version_id WHERE r.pipeline_id=? AND r.state='queued' AND "
        "t.mode='yes' AND COALESCE(t.mode_arg, '')<>'manager' AND r.approved_at IS NULL AND s.removed_at IS NULL "
        "AND t.block_reason IS NULL AND t.disabled_at IS NULL ORDER BY r.id", (pipeline_id,))]


def deploy(conn, path):
    """GET /api/deploy: every pipeline and every target's default pipeline in one read, for the Deploy tab: its
    stages as a row, its environments, where its runs come from (and that token's state), and the versions that wait
    on a person's yes. `count` is the red number of the tab: those waits plus the tokens that fail."""
    saved = {r["name"] for r in conn.execute("SELECT name FROM secrets")}
    out = []
    for p in store.pipelines(conn):
        d = store.show(conn, p["name"])
        waits = [] if p.get("held_reason") else _person_waits(conn, p["id"])
        out.append({"name": p["name"], "project": p["project"], "default": False, "source": {**_source(p, saved), "actions": d["source"]["actions"]},
                    "held": p.get("held_reason"), "held_by": p.get("held_by"), "stages": _strip(d["stages"]),
                    "envs": [{"name": e["name"], "target": e.get("target"), "rev": e.get("rev"), "since": e.get("since"),
                              "history": e.get("history") or []} for e in d["envs"]],
                    "targets": [e["target"] for e in d["envs"] if e.get("target")], "waits": waits})
    for t in targets.default_list(conn):
        d = targets.default_pipeline(conn, t["name"])
        approval = next((s for s in d["stages"] if s["name"] == "approval"), None)
        r = (approval or {}).get("last_run") or {}
        waits = ([{"stage": "approval", "rev": r.get("version"), "run": None, "item": r.get("item"),
                   "since": r.get("started_at"), "note": "the release waits for a person's yes"}]
                 if r.get("state") == "queued" and r.get("item") else [])
        out.append({"name": t["name"], "project": d["project"], "default": True, "source": {"forge": None,
                    "label": "deploy target", "state": "local"}, "held": None, "held_by": None,
                    "stages": _strip(d["stages"]), "envs": [{"name": e["name"], "target": e.get("target"),
                    "rev": e.get("rev"), "since": e.get("since"), "history": e.get("history") or []} for e in d["envs"]],
                    "targets": [t["name"]], "waits": waits})
    bad = {(x["source"]["forge"], x["source"].get("secret")) for x in out if x["source"]["state"] in ("bad", "missing")}
    return {"pipelines": out, "count": sum(len(x["waits"]) for x in out) + len(bad)}


def audit(conn, path):
    """GET /api/pipeline-audit[?pipeline=<name>]: the changes of every pipeline (or of one), newest first: the
    definition and each action (hold, approve, roll back, run again, stop), with who did it (pipeline_changes)."""
    from urllib.parse import parse_qs, urlsplit
    q = parse_qs(urlsplit(path).query)
    name = (q.get("pipeline") or [None])[0]
    sql = ("SELECT c.id, c.at, c.actor, c.change, c.reason, c.until_at, p.name pipeline FROM pipeline_changes c "
           "JOIN pipelines p "
           "ON p.id=c.pipeline_id")
    args = []
    if name:
        sql, args = sql + " WHERE p.name=?", [name]
    return {"lines": [dict(r) for r in conn.execute(sql + " ORDER BY c.id DESC LIMIT ?", args + [AUDIT_MAX])],
            "max": AUDIT_MAX}


GET += [
    (lambda path: path == "/api/deploy", _with(deploy)),
    (lambda path: path == "/api/pipeline-audit", _with(audit, query=True)),
]
