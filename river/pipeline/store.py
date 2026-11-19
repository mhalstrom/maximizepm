"""The records of the pipeline package and the one path that changes them (#6112).

Every change of a run, a step, a test or an environment comes from an event (ingest): an adapter's CDEvents, the
'::step' lines of a stage's output (steps.py), a JUnit report (junit.py), or a call of the runner (run_start,
run_finish), which makes its own event. ingest keeps each event in pipeline_events and applies it to the records.
The definitions of a pipeline (its stages and environments) are in define.py, the transitions into its stages in
transitions.py, each with its history.

The reads (pipelines, show, versions, run_show, tests, events) are what the view, the runner and the MaximizePM
connection read; each stays under 100 ms (policy p147) on indexed columns.
"""

import json
import re
import uuid
from datetime import timedelta

from .. import core
from . import audit, cdevents

STAGE_KINDS = ("build", "test", "review", "approve", "deploy", "check", "other")
RUN_STATES = ("queued", "running", "passed", "failed", "cancelled", "skipped")
FINAL = ("passed", "failed", "cancelled", "skipped")
OUTPUT_LINES = 60  # a run keeps its last lines
STEP_LINES = 20  # a step keeps its last lines
MESSAGE_MAX = 500
DETAIL_MAX = 4000

_NAME = re.compile(r"^[A-Za-z0-9][\w.-]{0,63}$")


def _now():
    return core.iso(core.now())


def _name(name, what):
    name = str(name or "").strip()
    if not _NAME.match(name):
        raise core.RiverError(f"the {what} name {name!r} must be 1 to 64 letters, digits, '.', '_' or '-', "
                              "starting with a letter or digit")
    return name


def _cut(text, n):
    if text is None:
        return None
    text = str(text)
    return text if len(text) <= n else text[:n - 1] + "…"


def _tail(old, lines, n):
    """The last n lines of old followed by lines."""
    keep = (old.split("\n") if old else []) + list(lines)
    return "\n".join(keep[-n:])


# ---------------------------------------------------------------- definitions

def _pipeline(conn, name):
    row = conn.execute("SELECT * FROM pipelines WHERE name=?", (name,)).fetchone()
    if not row:
        raise core.RiverError(f"no pipeline named {name!r}; a person or the manager adds it first")
    return row


# ---------------------------------------------------------------- branches and joins (#6142)

def stage_graph(conn, pid):
    """(the live stages in order, {stage: the stages it follows}, {stage: the stages that follow it}). A stage follows
    the stages its 'after' names, or else the stage before it in order; only stages earlier in the order count, so the
    flow never loops. Two stages after one are branches; a stage after two or more is a join (Decision 9)."""
    rows = conn.execute("SELECT * FROM pipeline_stages WHERE pipeline_id=? AND removed_at IS NULL ORDER BY pos, id",
                        (pid,)).fetchall()
    names = [r["name"] for r in rows]
    prev = {}
    for i, r in enumerate(rows):
        after = [a for a in (r["after"] or "").split(",") if a in names[:i]]
        prev[r["name"]] = after or ([names[i - 1]] if i and not r["after"] else [])
    nxt = {n: [m for m in names if n in prev[m]] for n in names}
    return rows, prev, nxt


def ancestors(prev, name):
    """Every stage that a version passes before it reaches stage name."""
    out, todo = set(), list(prev.get(name, []))
    while todo:
        n = todo.pop()
        if n not in out:
            out.add(n)
            todo.extend(prev.get(n, []))
    return out


def depth(prev, name, _memo=None):
    """How many stages lie before stage name on its longest path (the column of the page)."""
    memo = {} if _memo is None else _memo
    if name not in memo:
        memo[name] = 1 + max((depth(prev, p, memo) for p in prev.get(name, [])), default=-1)
    return memo[name]


def branch_siblings(conn, pid, stage):
    """The stages in the other branches at the place of stage (the same distance from the fork where its branch
    starts): a move back of every branch acts on them too. None when stage is on no branch."""
    rows, prev, nxt = stage_graph(conn, pid)
    cur, fork = stage, None
    while len(prev.get(cur, [])) == 1:
        p = prev[cur][0]
        if len(nxt[p]) > 1:
            fork = p
            break
        cur = p
    if fork is None:
        return []
    memo, d = {}, depth(prev, stage)
    return [r["name"] for r in rows if r["name"] != stage and fork in ancestors(prev, r["name"])
            and depth(prev, r["name"], memo) == d and len(prev[r["name"]]) == 1]


# ---------------------------------------------------------------- records an event names (inside a tx)

def _stage_id(conn, pid, name, kind=None):
    """The stage of that name; an event that names a stage the pipeline lacks adds it at the end (created_by
    'event'), so a forge's run shows before a person describes the stage."""
    name = _name(name, "stage")
    row = conn.execute("SELECT id FROM pipeline_stages WHERE pipeline_id=? AND name=?", (pid, name)).fetchone()
    if row:
        return row["id"]
    last = conn.execute("SELECT COALESCE(MAX(pos), 0) FROM pipeline_stages WHERE pipeline_id=?", (pid,)).fetchone()[0]
    return conn.execute("INSERT INTO pipeline_stages(pipeline_id, name, pos, kind, created_by, created_at) "
                        "VALUES (?,?,?,?, 'event', ?)",
                        (pid, name, last + 1, kind if kind in STAGE_KINDS else "other", _now())).lastrowid


def _env_id(conn, pid, name, at):
    name = _name(name, "environment")
    row = conn.execute("SELECT id FROM pipeline_envs WHERE pipeline_id=? AND name=?", (pid, name)).fetchone()
    if row:
        return row["id"]
    return conn.execute("INSERT INTO pipeline_envs(pipeline_id, name, created_at) VALUES (?,?,?)",
                        (pid, name, at)).lastrowid


def _version_id(conn, pid, rev, ref=None, title=None):
    """The version of a commit. A short id and the full id of one commit are one version: the longer one stays."""
    rev = str(rev or "").strip()
    if not rev:
        return None
    rows = conn.execute("SELECT id, rev FROM pipeline_versions WHERE pipeline_id=? AND (rev=? OR "
                        "(length(?)>=7 AND length(rev)>=7 AND (substr(rev,1,length(?))=? OR substr(?,1,length(rev))=rev)))",
                        (pid, rev, rev, rev, rev, rev)).fetchall()
    if rows:
        vid, old = rows[0]["id"], rows[0]["rev"]
        if len(rev) > len(old):
            conn.execute("UPDATE pipeline_versions SET rev=? WHERE id=?", (rev, vid))
        if ref or title:
            conn.execute("UPDATE pipeline_versions SET ref=COALESCE(?, ref), title=COALESCE(?, title) WHERE id=?",
                         (ref, title, vid))
        return vid
    return conn.execute("INSERT INTO pipeline_versions(pipeline_id, rev, ref, title, created_at) VALUES (?,?,?,?,?)",
                        (pid, rev, ref, title, _now())).lastrowid


def _run_of(conn, pid, ev, need=True):
    """The stage run an event belongs to: customData.maxpm.run (our id), maxpm.run_key or the taskrun's
    pipelineRun.id (the pipelinerun subject id)."""
    m, c = ev["maxpm"], ev["content"]
    if m.get("run") is not None:
        row = conn.execute("SELECT * FROM stage_runs WHERE id=? AND pipeline_id=?", (m["run"], pid)).fetchone()
        if row:
            return row
    pr = c.get("pipelineRun")
    key = m.get("run_key") or (pr.get("id") if isinstance(pr, dict) else None)
    if key:
        row = conn.execute("SELECT * FROM stage_runs WHERE pipeline_id=? AND key=?", (pid, str(key))).fetchone()
        if row:
            return row
    if need:
        raise core.RiverError(f"the {ev['subject']} event {ev['id']} names no stage run of this pipeline; set "
                              "customData.maxpm.run (the run id) or run_key (its pipelinerun subject id)")
    return None


def _pipeline_of(conn, ev, pipeline):
    name = pipeline or ev["maxpm"].get("pipeline")
    if name:
        return _pipeline(conn, name)["id"]
    run = ev["maxpm"].get("run")
    if run is not None:
        row = conn.execute("SELECT pipeline_id FROM stage_runs WHERE id=?", (run,)).fetchone()
        if row:
            return row["pipeline_id"]
    raise core.RiverError(f"the event {ev['id']} names no pipeline; set customData.maxpm.pipeline, or name the "
                          "pipeline when you send it")


# ---------------------------------------------------------------- applying events

def _close_steps(conn, run_id, at):
    """Steps still open when their run ends: 'stopped', since no end line came."""
    conn.execute("UPDATE stage_steps SET state='stopped', ended_at=?, seconds=CASE WHEN started_at IS NULL THEN NULL "
                 "ELSE ROUND((julianday(?) - julianday(started_at)) * 86400) END WHERE run_id=? AND state='running'",
                 (at, at, run_id))


def _apply_pipelinerun(conn, pid, ev):
    m, c, at, pred = ev["maxpm"], ev["content"], ev["at"], ev["predicate"]
    key = ev["subject_id"] or None
    run = conn.execute("SELECT * FROM stage_runs WHERE pipeline_id=? AND key=?", (pid, key)).fetchone() if key else None
    if run is None and m.get("run") is not None:
        run = conn.execute("SELECT * FROM stage_runs WHERE id=? AND pipeline_id=?", (m["run"], pid)).fetchone()
        if run is not None and key and run["key"] != key:  # a forge run that we started: it keeps the forge's id
            conn.execute("UPDATE stage_runs SET key=?, source=? WHERE id=?", (key, ev["source"], run["id"]))
            run = conn.execute("SELECT * FROM stage_runs WHERE id=?", (run["id"],)).fetchone()
    vid = _version_id(conn, pid, m.get("version"), m.get("ref"), m.get("title"))
    if run is None:
        stage = m.get("stage") or c.get("pipelineName")
        if not stage:
            raise core.RiverError(f"the pipelinerun event {ev['id']} names no stage; set customData.maxpm.stage "
                                  "or subject.content.pipelineName")
        sid = _stage_id(conn, pid, stage, m.get("kind"))
        rid = conn.execute("INSERT INTO stage_runs(pipeline_id, stage_id, version_id, key, state, url, host, source) "
                           "VALUES (?,?,?,?, 'queued', ?,?,?)",
                           (pid, sid, vid, key or str(uuid.uuid4()), c.get("url"), m.get("where"),
                            ev["source"])).lastrowid
        run = conn.execute("SELECT * FROM stage_runs WHERE id=?", (rid,)).fetchone()
    else:
        conn.execute("UPDATE stage_runs SET version_id=COALESCE(version_id, ?), url=COALESCE(?, url), "
                     "host=COALESCE(?, host) WHERE id=?", (vid, c.get("url"), m.get("where"), run["id"]))
    rid, final = run["id"], run["state"] in FINAL
    if pred == "queued":
        conn.execute("UPDATE stage_runs SET queued_at=COALESCE(queued_at, ?) WHERE id=?", (at, rid))
    elif pred == "started":
        conn.execute("UPDATE stage_runs SET started_at=COALESCE(started_at, ?), state=CASE WHEN ? THEN state "
                     "ELSE 'running' END WHERE id=?", (at, final, rid))
    elif pred == "finished":
        errors = c.get("errors")
        state = cdevents.outcome(c.get("outcome"))
        conn.execute("UPDATE stage_runs SET state=?, ended_at=?, errors=COALESCE(?, errors) WHERE id=?",
                     (state, at, _cut(errors, MESSAGE_MAX) if errors else None, rid))
        _close_steps(conn, rid, at)
        if not (final and run["state"] == state):  # the same end read again is no new line (#6144)
            audit.run_end(conn, rid, state, _cut(errors, MESSAGE_MAX) if errors else None)
    else:
        return rid, f"pipelinerun.{pred}: kept; no change of the run"
    return rid, None


def _apply_taskrun(conn, pid, ev):
    m, c, at, pred = ev["maxpm"], ev["content"], ev["at"], ev["predicate"]
    run = _run_of(conn, pid, ev)
    key = ev["subject_id"] or str(uuid.uuid4())
    name = c.get("taskName") or m.get("step") or key
    kind = m.get("kind")
    step = conn.execute("SELECT * FROM stage_steps WHERE run_id=? AND key=?", (run["id"], key)).fetchone()
    if step is None:
        conn.execute("INSERT INTO stage_steps(run_id, key, name, kind, host, parallel, state) "
                     "VALUES (?,?,?,?,?,?, 'running')",
                     (run["id"], key, _cut(name, 200), kind, m.get("where"), 1 if m.get("parallel") else 0))
        step = conn.execute("SELECT * FROM stage_steps WHERE run_id=? AND key=?", (run["id"], key)).fetchone()
    if run["state"] == "queued":  # a step started: the run runs
        conn.execute("UPDATE stage_runs SET state='running', started_at=COALESCE(started_at, ?) WHERE id=?",
                     (at, run["id"]))
    note = _cut(m.get("note") or c.get("errors"), MESSAGE_MAX)
    if pred == "started":
        conn.execute("UPDATE stage_steps SET started_at=COALESCE(started_at, ?), note=COALESCE(?, note) WHERE id=?",
                     (at, note, step["id"]))
    elif pred == "finished":
        secs = m.get("seconds")
        started = step["started_at"]
        if started is None and secs is not None:  # a lone end line with its time: it started that long before
            started = core.iso(core.parse_iso(at) - timedelta(seconds=float(secs)))
        if secs is None and started:
            secs = (core.parse_iso(at) - core.parse_iso(started)).total_seconds()
        conn.execute("UPDATE stage_steps SET state=?, started_at=?, ended_at=?, seconds=?, note=COALESCE(?, note) "
                     "WHERE id=?", (cdevents.outcome(c.get("outcome")), started, at, secs, note, step["id"]))
    else:
        return run["id"], f"taskrun.{pred}: kept; no change of the step"
    return run["id"], None


def _apply_testsuiterun(conn, pid, ev):
    m, c, at, pred = ev["maxpm"], ev["content"], ev["at"], ev["predicate"]
    run = _run_of(conn, pid, ev)
    key = ev["subject_id"] or str(uuid.uuid4())
    suite = c.get("testSuite") if isinstance(c.get("testSuite"), dict) else {}
    name = suite.get("name") or m.get("suite") or key
    row = conn.execute("SELECT * FROM test_suites WHERE run_id=? AND key=?", (run["id"], key)).fetchone()
    if row is None:
        conn.execute("INSERT INTO test_suites(run_id, key, name, state, started_at) VALUES (?,?,?, 'running', ?)",
                     (run["id"], key, _cut(name, 200), at))
        row = conn.execute("SELECT * FROM test_suites WHERE run_id=? AND key=?", (run["id"], key)).fetchone()
    if pred in ("queued", "started"):
        return run["id"], None
    if pred != "finished":
        return run["id"], f"testsuiterun.{pred}: kept; no change of the suite"
    n = conn.execute("SELECT COUNT(*), SUM(state IN ('failed','error')), SUM(state='skipped'), SUM(seconds) "
                     "FROM test_results WHERE suite_id=?", (row["id"],)).fetchone()
    state = cdevents.outcome(c.get("outcome"), "failed" if n[1] else "passed")
    conn.execute("UPDATE test_suites SET state=?, ended_at=?, tests=?, failed=?, skipped=?, seconds=? WHERE id=?",
                 (state, at, n[0], n[1] or 0, n[2] or 0, m.get("seconds", n[3]), row["id"]))
    return run["id"], None


def _apply_testcaserun(conn, pid, ev):
    m, c, at, pred = ev["maxpm"], ev["content"], ev["at"], ev["predicate"]
    tsr = c.get("testSuiteRun") if isinstance(c.get("testSuiteRun"), dict) else {}
    run = _run_of(conn, pid, ev, need=not tsr.get("id"))
    suite = None
    if tsr.get("id"):
        q = "SELECT s.* FROM test_suites s JOIN stage_runs r ON r.id=s.run_id WHERE s.key=? AND r.pipeline_id=?"
        args = [str(tsr["id"]), pid]
        if run is not None:
            q, args = q + " AND s.run_id=?", args + [run["id"]]
        suite = conn.execute(q + " ORDER BY s.id DESC LIMIT 1", args).fetchone()
        if suite is not None and run is None:
            run = conn.execute("SELECT * FROM stage_runs WHERE id=?", (suite["run_id"],)).fetchone()
    if run is None:
        raise core.RiverError(f"the testcaserun event {ev['id']} names no stage run or test suite run of this "
                              "pipeline; set customData.maxpm.run or send its testsuiterun first")
    case = c.get("testCase") if isinstance(c.get("testCase"), dict) else {}
    name = case.get("name") or case.get("id") or ev["subject_id"]
    suite_name = m.get("suite") or (suite["name"] if suite else None) or "tests"
    if pred == "finished":
        state = cdevents.test_outcome(c.get("outcome"))
    elif pred == "skipped":
        state = "skipped"
    else:
        return run["id"], None  # queued, started: the result comes with finished
    if state == "cancelled":
        state = "skipped"
    conn.execute(
        "INSERT INTO test_results(run_id, suite_id, suite, name, state, seconds, message, detail, file, at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id, suite, name) DO UPDATE SET suite_id=excluded.suite_id, "
        "state=excluded.state, seconds=excluded.seconds, message=excluded.message, detail=excluded.detail, "
        "file=excluded.file, at=excluded.at",
        (run["id"], suite["id"] if suite else None, _cut(suite_name, 300), _cut(name, 500), state, m.get("seconds"),
         _cut(c.get("reason") or m.get("message"), MESSAGE_MAX), _cut(m.get("detail"), DETAIL_MAX), m.get("file"), at))
    return run["id"], None


def _apply_service(conn, pid, ev):
    m, c, at, pred = ev["maxpm"], ev["content"], ev["at"], ev["predicate"]
    env = c.get("environment") if isinstance(c.get("environment"), dict) else {}
    name = m.get("env") or env.get("id")
    if not name:
        raise core.RiverError(f"the service event {ev['id']} names no environment; set "
                              "subject.content.environment.id")
    if pred not in ("deployed", "upgraded", "rolledback", "removed"):
        return None, f"service.{pred}: kept; no change of the environment"
    eid = _env_id(conn, pid, name, at)
    rev = m.get("version")
    art = str(c.get("artifactId") or "")
    if not rev and "@" in art:  # pkg:generic/toolscaled@8d8d2fd
        rev = art.rsplit("@", 1)[1].split("?")[0]
    vid = _version_id(conn, pid, rev, m.get("ref"), m.get("title")) if pred != "removed" else None
    run = _run_of(conn, pid, ev, need=False)
    conn.execute("INSERT INTO pipeline_deploys(env_id, version_id, run_id, kind, at) VALUES (?,?,?,?,?)",
                 (eid, vid, run["id"] if run else None, pred, at))
    if run is None:  # a deploy of a stage run is the line of that run's end (#6144)
        audit.deploy_event(conn, pid, ev["source"], name, rev, pred)
    # An event older than the environment's current version changes only the history.
    conn.execute("UPDATE pipeline_envs SET version_id=?, since=? WHERE id=? AND (since IS NULL OR since<=?)",
                 (vid, at, eid, at))
    return run["id"] if run else None, None


def _apply_environment(conn, pid, ev):
    c, at, pred = ev["content"], ev["at"], ev["predicate"]
    name = c.get("name") or ev["subject_id"]
    eid = _env_id(conn, pid, name, at)
    if pred in ("created", "modified"):
        conn.execute("UPDATE pipeline_envs SET url=COALESCE(?, url), removed_at=NULL WHERE id=?", (c.get("url"), eid))
    elif pred == "deleted":
        conn.execute("UPDATE pipeline_envs SET removed_at=? WHERE id=?", (at, eid))
    else:
        return None, f"environment.{pred}: kept; no change of the environment"
    return None, None


APPLY = {"pipelinerun": _apply_pipelinerun, "taskrun": _apply_taskrun, "testsuiterun": _apply_testsuiterun,
         "testcaserun": _apply_testcaserun, "service": _apply_service, "environment": _apply_environment}


def _ingest_one(conn, ev, pipeline=None):
    """Keep one parsed event and apply it, inside the caller's tx. The same event again (its source and id) does
    nothing: a forge adapter that asks every minute may send it twice."""
    old = conn.execute("SELECT id, run_id FROM pipeline_events WHERE source=? AND event_id=?",
                       (ev["source"], ev["id"])).fetchone()
    if old:
        return {"event": old["id"], "run": old["run_id"], "type": ev["type"], "duplicate": True}
    pid = _pipeline_of(conn, ev, pipeline)
    fn = APPLY.get(ev["subject"])
    run_id, note = fn(conn, pid, ev) if fn else (None, f"{ev['subject']}: kept; no record for this subject")
    eid = conn.execute("INSERT INTO pipeline_events(event_id, source, type, at, received_at, pipeline_id, run_id, body) "
                       "VALUES (?,?,?,?,?,?,?,?)",
                       (ev["id"], ev["source"], ev["type"], ev["at"], _now(), pid, run_id,
                        json.dumps(ev["event"], separators=(",", ":")))).lastrowid
    out = {"event": eid, "run": run_id, "type": ev["type"]}
    if note:
        out["note"] = note
    return out


def ingest(conn, events, pipeline=None):
    """Keep and apply one event or a list of them, in one transaction: all of them or none. Each is a CDEvents
    object (a dict, or its JSON text). pipeline names the pipeline when the events do not
    (customData.maxpm.pipeline). Returns one result for each event: its row id, its stage run, and a note when it
    changed no record."""
    many = isinstance(events, (list, tuple))
    parsed = [cdevents.parse(e) for e in (events if many else [events])]  # every refusal before any write
    with core.tx(conn):
        out = [_ingest_one(conn, ev, pipeline) for ev in parsed]
    return out if many else out[0]


# ---------------------------------------------------------------- calls of the runner

_OUTCOME_WORD = {"passed": "success", "failed": "failure", "cancelled": "cancel", "skipped": "skip"}


def run_start(conn, pipeline, stage, version=None, where=None, key=None, url=None, source="maxpm", at=None):
    """A stage run that starts now, as a pipelinerun.started event. Returns the run's id."""
    ev = cdevents.make("pipelinerun", "started", key or str(uuid.uuid4()), {"pipelineName": stage, "url": url},
                       {"pipeline": pipeline, "stage": stage, "version": version, "where": where}, source, at)
    return ingest(conn, ev)["run"]


def run_finish(conn, run_id, state, errors=None, source="maxpm", at=None):
    """The end of a stage run (passed, failed, cancelled, skipped), as a pipelinerun.finished event."""
    if state not in _OUTCOME_WORD:
        raise core.RiverError(f"a run ends {', '.join(_OUTCOME_WORD)}, not {state!r}")
    row = conn.execute("SELECT r.key, p.name FROM stage_runs r JOIN pipelines p ON p.id=r.pipeline_id WHERE r.id=?",
                       (run_id,)).fetchone()
    if not row:
        raise core.RiverError(f"no stage run {run_id}")
    ev = cdevents.make("pipelinerun", "finished", row["key"], {"outcome": _OUTCOME_WORD[state], "errors": errors},
                       {"pipeline": row["name"], "run": run_id}, source, at)
    return ingest(conn, ev)


def deployed(conn, pipeline, env, version, run_id=None, kind="deployed", source="maxpm", at=None):
    """A version that now runs in an environment (deployed, upgraded, rolledback), or none (removed)."""
    ev = cdevents.make("service", kind, f"{pipeline}/{env}", {"environment": {"id": env, "source": source}},
                       {"pipeline": pipeline, "version": version, "run": run_id}, source, at)
    return ingest(conn, ev)


def run_output(conn, run_id, lines):
    """Add lines to the last lines of a run (inside a tx)."""
    if not lines:
        return
    old = conn.execute("SELECT output FROM stage_runs WHERE id=?", (run_id,)).fetchone()["output"]
    conn.execute("UPDATE stage_runs SET output=? WHERE id=?", (_tail(old, lines, OUTPUT_LINES), run_id))


def step_output(conn, step_id, lines):
    if not lines:
        return
    old = conn.execute("SELECT output FROM stage_steps WHERE id=?", (step_id,)).fetchone()["output"]
    conn.execute("UPDATE stage_steps SET output=? WHERE id=?", (_tail(old, lines, STEP_LINES), step_id))


# ---------------------------------------------------------------- reads

def pipelines(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM pipelines ORDER BY name")]


def _run_dict(conn, r):
    if r is None:
        return None
    d = dict(r)
    d["version"] = (conn.execute("SELECT rev FROM pipeline_versions WHERE id=?", (r["version_id"],)).fetchone()
                    or [None])[0]
    d["steps"] = {s["state"]: s["n"] for s in conn.execute(
        "SELECT state, COUNT(*) n FROM stage_steps WHERE run_id=? GROUP BY state", (r["id"],))}
    d["tests"] = {s["state"]: s["n"] for s in conn.execute(
        "SELECT state, COUNT(*) n FROM test_results WHERE run_id=? GROUP BY state", (r["id"],))}
    return d


def show(conn, name):
    """A pipeline for the view: its stages left to right, each with its last run and the transition into it (the
    arrow before it, #6139), and its environments with the version that runs there now and their last deploys."""
    from . import transitions
    p = dict(_pipeline(conn, name))
    cache = {}  # one read of the pipeline's open faults for all its arrows
    envs = {}
    for e in conn.execute("SELECT e.*, v.rev FROM pipeline_envs e LEFT JOIN pipeline_versions v ON v.id=e.version_id "
                          "WHERE e.pipeline_id=? AND e.removed_at IS NULL ORDER BY e.id", (p["id"],)):
        d = dict(e)
        d["history"] = [dict(h) for h in conn.execute(
            "SELECT d.kind, d.at, d.run_id, v.rev FROM pipeline_deploys d LEFT JOIN pipeline_versions v "
            "ON v.id=d.version_id WHERE d.env_id=? ORDER BY d.id DESC LIMIT 5", (e["id"],))]
        envs[e["id"]] = d
    stages = []
    for s in conn.execute("SELECT * FROM pipeline_stages WHERE pipeline_id=? AND removed_at IS NULL ORDER BY pos, id",
                          (p["id"],)):
        d = dict(s)
        d["transition"] = transitions.describe(conn, s, p, cache)
        d["env"] = envs[s["env_id"]]["name"] if s["env_id"] in envs else None
        d["last_run"] = _run_dict(conn, conn.execute(
            "SELECT * FROM stage_runs WHERE stage_id=? ORDER BY id DESC LIMIT 1", (s["id"],)).fetchone())
        d["start"] = s["start"] or "auto"  # #6138: manual: a version that enters waits for Run
        d["prev"] = d["transition"].get("from") or []  # #6142: the stages it follows (branches, joins)
        w = conn.execute("SELECT r.id, v.rev, r.entered_at FROM stage_runs r LEFT JOIN pipeline_versions v ON "
                         "v.id=r.version_id WHERE r.stage_id=? AND r.state='queued' AND r.entered_at IS NOT NULL AND "
                         "r.run_at IS NULL ORDER BY r.id LIMIT 1", (s["id"],)).fetchone() if s["start"] else None
        d["waits_for_run"] = {"run": w["id"], "rev": w["rev"], "since": w["entered_at"]} if w else None
        stages.append(d)
    p["stages"] = stages
    p["envs"] = list(envs.values())
    from . import plugin
    p["source"] = plugin.actions(conn, p)  # what its source can do: the page greys out the rest (#6159)
    p.pop("plugin_state", None)
    return p


def versions(conn, name, limit=20):
    """The newest versions and, for each, the state of its last run in each stage (the view's second tab)."""
    pid = _pipeline(conn, name)["id"]
    vs = [dict(v) for v in conn.execute(
        "SELECT v.id, v.rev, v.ref, v.title, MAX(r.id) last FROM pipeline_versions v JOIN stage_runs r "
        "ON r.version_id=v.id WHERE v.pipeline_id=? GROUP BY v.id ORDER BY last DESC LIMIT ?", (pid, limit))]
    for v in vs:
        v["stages"] = {r["stage"]: {"run": r["id"], "state": r["state"]} for r in conn.execute(
            "SELECT s.name stage, r.id, r.state FROM stage_runs r JOIN pipeline_stages s ON s.id=r.stage_id "
            "WHERE r.id IN (SELECT MAX(id) FROM stage_runs WHERE version_id=? GROUP BY stage_id)", (v["id"],))}
        v.pop("last")
    return vs


def _what(purpose, kind):
    """What a run of a stage was, in words: the flow (deployed, ran), a move back, a run again, ..."""
    p = purpose or ""
    if p == "rollback":
        return "moved back"
    if p == "rerun":
        return "run again"
    if p.startswith("failed:"):
        return f"failed tests of run #{p.split(':', 1)[1]} again"
    if p in ("override", "whole"):  # #6146
        return {"override": "override", "whole": "whole pipeline run"}[p]
    return "deployed" if kind == "deploy" else "ran"


STAGE_VERSIONS = 30  # the runs that 'Versions in <stage>' lists


def stage_versions(conn, pipeline, stage, limit=STAGE_VERSIONS):
    """'Versions in <stage>' (#6141): each run of the stage, newest first, with its version, what it was (deployed,
    moved back, run again, ...), who sent it or let it in and why, its state and times, and whether that version is
    the one the stage's environment runs now. A move back is one more run, so the list is the stage's own record."""
    p = _pipeline(conn, pipeline)
    s = conn.execute("SELECT * FROM pipeline_stages WHERE pipeline_id=? AND name=?", (p["id"], stage)).fetchone()
    if not s:
        raise core.RiverError(f"pipeline {p['name']} has no stage {stage}")
    live = conn.execute("SELECT version_id FROM pipeline_envs WHERE id=?", (s["env_id"],)).fetchone() \
        if s["env_id"] else None
    out, live_seen = [], False
    for r in conn.execute("SELECT r.*, v.rev FROM stage_runs r LEFT JOIN pipeline_versions v ON v.id=r.version_id "
                          "WHERE r.stage_id=? ORDER BY r.id DESC LIMIT ?", (s["id"], limit)):
        # A stage with no environment: its newest passed run is the version it has now.
        is_live = bool(not live_seen and r["state"] == "passed" and (
            not s["env_id"] or (live and live["version_id"] and r["version_id"] == live["version_id"])))
        live_seen = live_seen or is_live
        who = r["sent_by"] or r["passed_by"] or r["approved_by"]
        why = r["reason"] or (f"{r['approved_by']} said yes" if r["approved_by"] and not r["sent_by"] else
                              "the flow: the stage before passed" if not r["purpose"] else None)
        took = None
        if r["started_at"] and r["ended_at"]:
            took = (core.parse_iso(r["ended_at"]) - core.parse_iso(r["started_at"])).total_seconds()
        out.append({"run": r["id"], "rev": r["rev"], "what": _what(r["purpose"], s["kind"]), "purpose": r["purpose"],
                    "by": who, "why": why, "state": r["state"], "queued_at": r["queued_at"],
                    "started_at": r["started_at"], "ended_at": r["ended_at"], "seconds": took, "errors": r["errors"],
                    "live": is_live})
    return {"pipeline": p["name"], "stage": s["name"], "kind": s["kind"], "env_id": s["env_id"], "runs": out,
            "branches": branch_siblings(conn, p["id"], s["name"])}  # #6142: a move back of every branch acts on them


def run_show(conn, run_id):
    """One stage run for its detail: the steps on a time line, the test suites, the last lines."""
    r = conn.execute("SELECT r.*, s.name stage, s.kind, p.name pipeline FROM stage_runs r JOIN pipeline_stages s "
                     "ON s.id=r.stage_id JOIN pipelines p ON p.id=r.pipeline_id WHERE r.id=?", (run_id,)).fetchone()
    if not r:
        raise core.RiverError(f"no stage run {run_id}")
    d = _run_dict(conn, r)
    d["step_list"] = [dict(s) for s in conn.execute(
        "SELECT * FROM stage_steps WHERE run_id=? ORDER BY COALESCE(started_at, ended_at), id", (run_id,))]
    d["suites"] = [dict(s) for s in conn.execute("SELECT * FROM test_suites WHERE run_id=? ORDER BY id", (run_id,))]
    return d


def tests(conn, run_id, state=None, limit=None):
    """Each test's result in a run, failed and error first, then passed, then skipped."""
    q = "SELECT * FROM test_results WHERE run_id=?"
    args = [run_id]
    if state:
        q, args = q + " AND state=?", args + [state]
    q += (" ORDER BY CASE state WHEN 'failed' THEN 0 WHEN 'error' THEN 0 WHEN 'passed' THEN 1 ELSE 2 END, "
          "suite, name")
    if limit:
        q, args = q + " LIMIT ?", args + [int(limit)]
    return [dict(t) for t in conn.execute(q, args)]


def events(conn, after=0, pipeline=None, limit=200):
    """The events kept after the row id `after`, oldest first: what the MaximizePM connection reads next."""
    q, args = "SELECT * FROM pipeline_events WHERE id>?", [after]
    if pipeline:
        q, args = q + " AND pipeline_id=?", args + [_pipeline(conn, pipeline)["id"]]
    out = []
    for e in conn.execute(q + " ORDER BY id LIMIT ?", args + [limit]):
        d = dict(e)
        d["body"] = json.loads(d["body"])
        out.append(d)
    return out
