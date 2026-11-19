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
        p["account"] = _accounts().for_pipeline(conn, conn.execute("SELECT * FROM pipelines WHERE name=?",
                                                                   (name,)).fetchone())  # #6147
        work = _links().stage_work(conn, name).get(name) or {}
    else:
        p = targets.default_pipeline(conn, name)
        work = (_links().stage_work(conn, name, defaults={name: p}).get(name) or {})
        p["versions"] = [{"rev": r["version"], "deploy": r["deploy"], "stages": r["stages"]} for r in p.pop("releases")]
    _with_holding(conn, name, p["stages"])
    for s in p["stages"]:  # the faults and agents under each stage card (#6149)
        s["work"] = work.get(s["name"]) or {"faults": [], "agents": []}
    return p


def _with_holding(conn, name, stages):
    """Holding as the first stage (#6140): in front of a pipeline's stages, or in the place of the holding stage of a
    target's default pipeline (whose arrow after it is the release already). The list changes in place."""
    hs = targets.holding_stage(targets.holding_soon(conn, name))  # the last read at once, a new one in a thread
    if stages and stages[0]["name"] == "holding":
        stages[0] = dict(stages[0], holding=hs["holding"], last_run=hs["last_run"], kind="holding")
    else:
        stages.insert(0, hs)
    return stages


def holding(conn, path):
    """GET /api/pipeline-holding/<name>: the commits that wait in holding, with their tasks, sessions, changes and
    review, and the release after it (#6140)."""
    return targets.holding(conn, unquote(path.split("/api/pipeline-holding/", 1)[1]))


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
    (lambda path: path.startswith("/api/pipeline-holding/"), _with(holding)),
    (lambda path: path.startswith("/api/pipeline/"), _with(pipeline)),
]

def _runner():
    from ..pipeline import runner
    return runner


def _accounts():
    from ..pipeline import accounts
    return accounts


# The page's pipeline actions (#6117), as the person chosen in "You are": each one is the maxpm pipeline command of the
# same name, with its checks (only a person or the manager acts) and a line in the pipeline's history.
OPS = {
    "pipeline_hold": lambda c, a, who: _runner().hold(c, a["pipeline"], a.get("stage"), a.get("reason"), who),
    "pipeline_unhold": lambda c, a, who: _runner().release_hold(c, a["pipeline"], a.get("stage"), who),
    "pipeline_approve": lambda c, a, who: _runner().approve(c, a["pipeline"], a["stage"], a.get("rev"), who),
    "pipeline_rerun": lambda c, a, who: _runner().run_again(c, a["pipeline"], a["stage"], a["rev"], who,
                                                            reason=a.get("reason") or None),
    # #6141: Move back here, in Versions in <stage>
    # #6145: the operators of a pipeline (a person changes them) and its emergency actions (its operators)
    "pipeline_operators": lambda c, a, who: _operators().change(
        c, a["pipeline"], a.get("add") or [], a.get("remove") or [],
        None if a.get("agents") is None else bool(a["agents"]), who),
    "pipeline_emergency": lambda c, a, who: _emergency(c, a, who),
    # #6138: Run on a stage that starts manually (the function checks: an operator, a person or the manager)
    # #6143: the definition back to a version (a person or the manager; one more version)
    "pipeline_definition_back": lambda c, a, who: _versions().restore(c, a["pipeline"], int(a["n"]), a.get("reason"),
                                                                     who),
    "pipeline_run_stage": lambda c, a, who: _runner().run_stage(c, a["pipeline"], a["stage"], a.get("rev"), who),
    "pipeline_move_back": lambda c, a, who: (_runner().move_back_branches if a.get("every_branch")  # #6142
                                             else _runner().move_back)(c, a["pipeline"], a["stage"], a["rev"],
                                                                       a.get("reason"), who),
    "pipeline_rerun_failed": lambda c, a, who: _runner().rerun_failed(c, int(a["run"]), who),
    "pipeline_rollback": lambda c, a, who: _runner().rollback(c, a["pipeline"], a["env"], a["rev"], a.get("reason"), who),
    "pipeline_stop": lambda c, a, who: _runner().stop(c, int(a["run"]), who),
    # #6147: Check now on the Accounts view (a person or the manager)
    "pipeline_account_check": lambda c, a, who: _accounts().check_now(c, a.get("secret") or None, who),
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
    # #6144: a correction of a line of the audit log is a new line (the actor of that line, a person or the manager)
    "pipeline_correct": lambda c, a, who: _audit().correct(c, int(a["line"]), a.get("text"), who),
    # #6150: a trigger rule: {pipeline, action add|edit|off|on|remove, rule, event, stage, tier, does, runner, keys,
    # reason}, as maxpm pipeline trigger
    "pipeline_trigger": lambda c, a, who: trigger_op(c, a, who),
    # #6140: the release after holding: the reviewed commits, all of them, or up to one commit
    "pipeline_release": lambda c, a, who: targets.release(c, a["pipeline"], a.get("upto") or None, bool(a.get("all")),
                                                          who),
    # #6148: a person adds a forge token on the page (checked with the forge before it is saved), or has a session
    # make it in the project's browser
    "pipeline_token_add": lambda c, a, who: _accounts().add_token(c, a.get("forge"), a.get("token"), a.get("name"),
                                                                  a.get("url"), who),
    "pipeline_token_make": lambda c, a, who: _accounts().make_token(c, a.get("forge"), a.get("name"), a.get("url"),
                                                                    a.get("project"), who),
    # #6151: runners (Deploy > Runners, and the ticks on a pipeline's Triggers tab)
    "runner_add": lambda c, a, who: _runners().add(c, a["name"], a["ssh"], a.get("cli") or None, None, None, None, who),
    "runner_check": lambda c, a, who: _runners().check(c, a["name"], who),
    "runner_allow": lambda c, a, who: _runners().allow_pipeline(c, a["pipeline"], a["name"], bool(a.get("on", True)),
                                                                who),
    "runner_stop": lambda c, a, who: _runners().stop(c, int(a["run"]), who, a.get("reason")),
}


def _runners():
    from .. import runners
    return runners


def _transitions():
    from ..pipeline import transitions
    return transitions


def _operators():
    from ..pipeline import operators
    return operators


def _emergency(conn, a, who):
    """op pipeline_emergency {pipeline, action stop|resume|agents|moveback|revoke, reason, stage, key} (#6145)."""
    o, p, act = _operators(), a["pipeline"], a.get("action")
    if act == "stop":
        return o.stop(conn, p, a.get("reason"), who)
    if act == "resume":
        return o.resume(conn, p, who)
    if act == "agents":
        return o.stop_agents(conn, p, a.get("reason"), who)
    if act == "moveback":
        return o.move_back(conn, p, a["stage"], a.get("reason"), who)
    if act == "revoke":
        return o.revoke(conn, p, a["key"], a.get("reason"), who)
    raise RiverError(f"the emergency action {act!r} is not one of: stop, resume, agents, moveback, revoke")


def _versions():
    from ..pipeline import versions
    return versions


def definition_versions(conn, path):
    """GET /api/pipeline-versions?pipeline=<name>[&compare=A,B]: the versions of a pipeline's definition, newest
    first, or what changed from A to B (#6143)."""
    from urllib.parse import parse_qs, urlsplit
    q = parse_qs(urlsplit(path).query)
    if not q.get("pipeline"):
        raise RiverError("name the pipeline: ?pipeline=<name>")
    if q.get("compare"):
        a, b = (int(x) for x in q["compare"][0].split(","))
        return _versions().compare(conn, q["pipeline"][0], a, b)
    return _versions().listing(conn, q["pipeline"][0])


def emergency_plan(conn, path):
    """GET /api/pipeline-emergency?pipeline=<name>: what each emergency action acts on, and the operators (#6145)."""
    from urllib.parse import parse_qs, urlsplit
    q = parse_qs(urlsplit(path).query)
    if not q.get("pipeline"):
        raise RiverError("name the pipeline: ?pipeline=<name>")
    return _operators().plan(conn, q["pipeline"][0])


def _audit():
    from ..pipeline import audit
    return audit


def trigger_op(conn, a, who):
    from ..pipeline import triggers
    act = a.get("action")
    if act == "add":
        return triggers.rule_add(conn, a["pipeline"], a.get("event"), a.get("does"), a.get("stage"), a.get("tier"),
                                 a.get("runner"), a.get("keys"), who)
    if act == "edit":
        return triggers.rule_edit(conn, a["pipeline"], a["rule"], a.get("event"), a.get("does"), a.get("stage"),
                                  a.get("tier"), a.get("runner"), a.get("keys"), who)
    if act == "remove":
        return triggers.rule_remove(conn, a["pipeline"], a["rule"], who)
    if act in ("on", "off"):
        return triggers.rule_switch(conn, a["pipeline"], a["rule"], act == "on", a.get("reason"), who)
    raise RiverError("the action of a trigger rule is add, edit, off, on or remove")


def trigger_rules(conn, path):
    """GET /api/pipeline-triggers/<name>: the trigger rules of a pipeline and what they did lately (#6150)."""
    from ..pipeline import triggers
    return triggers.rules(conn, unquote(path.split("/api/pipeline-triggers/", 1)[1]))


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


def stage_runs(conn, path):
    """GET /api/pipeline-stage?pipeline=<name>&stage=<stage>: Versions in <stage> (#6141)."""
    from urllib.parse import parse_qs, urlsplit
    q = parse_qs(urlsplit(path).query)
    if not q.get("pipeline") or not q.get("stage"):
        raise RiverError("name the pipeline and the stage: ?pipeline=<name>&stage=<stage>")
    return store.stage_versions(conn, q["pipeline"][0], q["stage"][0])


GET += [
    (lambda path: path == "/api/pipeline-stage", _with(stage_runs, query=True)),
    (lambda path: path == "/api/pipeline-emergency", _with(emergency_plan, query=True)),
    (lambda path: path == "/api/pipeline-versions", _with(definition_versions, query=True)),  # #6143
    (lambda path: path.startswith("/api/pipeline-triggers/"), _with(trigger_rules)),
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


def _with_account(src, a):
    """The source of a pipeline with its account (#6147): who the token is, until when, and its refusal. A refusal
    that the account's check saw counts as bad, also before the next read of the pipeline."""
    if not a:
        return src
    src = dict(src, owner=a["owner"], kind=a["kind"], expires_at=a["expires_at"], days_left=a["days_left"],
               checked_at=a["checked_at"], host=a["host"])
    if a["state"] == "refused":
        src.update(state="bad", error=a["error"], refused_since=a["refused_since"])
    return src


def _strip(stages, no_news=False, work=None):
    """The stages of a pipeline as the Deploy list draws them: a bar for each, the state of its last run, and the
    faults and agents under it (work: links.stage_work of this pipeline, #6149)."""
    out = []
    for s in stages:
        r = s.get("last_run") or {}
        t = s.get("transition") or {}
        out.append({"name": s["name"], "kind": s["kind"], "env": s.get("env"), "tier": s.get("tier"),
                    "start": s.get("start"), "waits_for_run": s.get("waits_for_run"),  # #6138
                    "prev": s.get("prev"),  # #6142: the stages it follows
                    "transition": {k: t.get(k) for k in ("mode", "arg", "text", "state", "state_text", "set_by",
                                                         "until")},
                    "state": r.get("state") or "none", "version": r.get("version"), "fault": r.get("fault_item"),
                    "run": r.get("id"), "by_source": bool(s.get("workflow") and not s.get("run")),
                    # its data comes from a forge whose token fails: what it shows is old (#6147)
                    "no_news": bool(no_news and s.get("workflow") and not s.get("run")),
                    "holding": s.get("holding"),  # the first stage: what waits for the release (#6140)
                    "work": (work or {}).get(s["name"]) or {"faults": [], "agents": []}})
    return out


def _person_waits(conn, pipeline_id):
    """The versions that wait at a transition for a person's yes: not approved, and the transition not disabled or
    blocked (#6139)."""
    return [{"stage": r["stage"], "rev": r["rev"], "run": r["id"], "item": r["gate_item"], "since": r["queued_at"],
             "note": r["gate_note"]} for r in conn.execute(
        "SELECT r.id, r.gate_note, r.gate_item, r.queued_at, s.name stage, v.rev FROM stage_runs r "
        "JOIN pipeline_stages s ON s.id=r.stage_id JOIN pipeline_transitions t ON t.stage_id=s.id "
        "LEFT JOIN pipeline_versions v ON v.id=r.version_id WHERE r.pipeline_id=? AND r.state='queued' AND "
        "t.mode='yes' AND COALESCE(t.mode_arg, '')<>'manager' AND r.approved_at IS NULL AND r.entered_at IS NULL "
        "AND s.removed_at IS NULL "
        "AND t.block_reason IS NULL AND t.disabled_at IS NULL ORDER BY r.id", (pipeline_id,))]


def deploy(conn, path):
    """GET /api/deploy: every pipeline and every target's default pipeline in one read, for the Deploy tab: its
    stages as a row, its environments, where its runs come from (and that token's state), and the versions that wait
    on a person's yes. `count` is the red number of the tab: those waits plus the tokens that fail. Each stage has its
    work: the faults and the agents under it (#6149)."""
    saved = {r["name"] for r in conn.execute("SELECT name FROM secrets")}
    account_list = _accounts().listing(conn)
    accts = {(a["forge"], a["host"], a["secret"] or ""): a for a in account_list}
    defaults = {t["name"]: targets.default_pipeline(conn, t["name"]) for t in targets.default_list(conn)}
    work = _links().stage_work(conn, defaults=defaults)
    out = []
    for p in store.pipelines(conn):
        d = store.show(conn, p["name"])
        waits = [] if p.get("held_reason") else _person_waits(conn, p["id"])
        src = _with_account(_source(p, saved), accts.get(_accounts().key_of(p)))
        src["actions"] = d["source"]["actions"]
        out.append({"name": p["name"], "project": p["project"], "default": False, "source": src,
                    "held": p.get("held_reason"), "held_by": p.get("held_by"),
                    "stages": _strip(_with_holding(conn, p["name"], d["stages"]), src["state"] in ("bad", "missing"),
                                     work.get(p["name"])),
                    "envs": [{"name": e["name"], "target": e.get("target"), "rev": e.get("rev"), "since": e.get("since"),
                              "history": e.get("history") or []} for e in d["envs"]],
                    "targets": [e["target"] for e in d["envs"] if e.get("target")], "waits": waits,
                    "definition": _versions().current(conn, p["id"])})  # #6143: the head's 'definition vN'
    for t in targets.default_list(conn):
        d = defaults[t["name"]]
        approval = next((s for s in d["stages"] if s["name"] == "approval"), None)
        r = (approval or {}).get("last_run") or {}
        waits = ([{"stage": "approval", "rev": r.get("version"), "run": None, "item": r.get("item"),
                   "since": r.get("started_at"), "note": "the release waits for a person's yes"}]
                 if r.get("state") == "queued" and r.get("item") else [])
        out.append({"name": t["name"], "project": d["project"], "default": True, "source": {"forge": None,
                    "label": "deploy target", "state": "local"}, "held": None, "held_by": None,
                    "stages": _strip(_with_holding(conn, t["name"], d["stages"]), work=work.get(t["name"])), "envs": [{"name": e["name"], "target": e.get("target"),
                    "rev": e.get("rev"), "since": e.get("since"), "history": e.get("history") or []} for e in d["envs"]],
                    "targets": [t["name"]], "waits": waits})
    bad = {(x["source"]["forge"], x["source"].get("secret")) for x in out if x["source"]["state"] in ("bad", "missing")}
    return {"pipelines": out, "accounts": account_list, "runners": _runners().listing(conn),  # #6151
            "count": sum(len(x["waits"]) for x in out) + len(bad)}


def runner_log(conn, path):
    """GET /api/runner-log/<run>: what the agent of a runner run printed, in words (#6151)."""
    return _runners().log(conn, int(path.rsplit("/", 1)[1]))


def audit(conn, path):
    """GET /api/pipeline-audit[?pipeline=&who=&since=&q=&limit=]: the audit log of every pipeline (or of one), newest
    first (river/pipeline/audit.py, #6144): who (a kind: person, agent, trigger, hook, forge, a comma list; or a name),
    since (today, 7d, a date), q (words that each line holds). limit: at most audit.EXPORT_MAX (the export)."""
    from urllib.parse import parse_qs, urlsplit
    from ..pipeline import audit as log
    q = {k: v[0] for k, v in parse_qs(urlsplit(path).query).items()}
    out = log.lines(conn, q.get("pipeline") or None, q.get("who") or None, q.get("since") or None, q.get("q") or None,
                    int(q["limit"]) if (q.get("limit") or "").isdigit() else AUDIT_MAX)
    names = [r["name"] for r in conn.execute("SELECT name FROM pipelines ORDER BY name")]
    return {**out, "max": AUDIT_MAX, "pipelines": names}


def _gate(h, path):
    """GET /api/pipeline-gate?pipeline=&stage=&rev=[&wait=30m][&from=<system>] (#6164): may this version enter the
    stage now? 200 and {"may": true, ...} for yes; 409 and {"may": false, "why", "until"} for no, so curl --fail exits
    with the answer; 404 and {"error"} for a question that names no stage. wait: at most gates.HTTP_WAIT_MAX."""
    from urllib.parse import parse_qs, urlsplit
    from ..pipeline import gates
    q = {k: v[0] for k, v in parse_qs(urlsplit(h.path).query).items()}
    conn = core.connect()
    try:
        res = gates.may(conn, q.get("pipeline"), q.get("stage"), q.get("rev"), q.get("wait") or None,
                        q.get("from") or None, None, limit=gates.HTTP_WAIT_MAX)
        return h._send(200 if res["may"] else 409, res)
    except (RiverError, ValueError) as e:
        return h._send(404, {"error": str(e)})
    finally:
        conn.close()


GET += [
    (lambda path: path == "/api/pipeline-gate", _gate),
    (lambda path: path == "/api/deploy", _with(deploy)),
    (lambda path: path.startswith("/api/runner-log/"), _with(runner_log)),
    (lambda path: path == "/api/pipeline-audit", _with(audit, query=True)),
]
