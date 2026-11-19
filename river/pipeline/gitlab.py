"""The GitLab adapter of pipelines (#6121): GitLab CI runs the work; MaximizePM holds the order and the gates (an
approval gate on GitLab needs Premium; here it needs no paid plan).

In GitLab one pipeline holds every stage of a commit, so a MaximizePM stage names a GitLab stage
(maxpm pipeline stage <p> <s> --workflow <gitlab stage>), and one run of it is the jobs of that GitLab stage in the
pipeline of a commit (key gl-<pipeline id>-<stage>). maxpm serve reads the API once a minute (poll_due) for each
pipeline whose forge is gitlab (maxpm pipeline forge <name> gitlab --repo group/project --secret <name>
[--url https://gitlab.example.com]):

- the newest pipelines: for each MaximizePM stage, its GitLab jobs become a pipelinerun (its state from the jobs: a
  failed job that may not fail fails it; a manual job that waits keeps it queued) and taskrun events;
- the pipeline's test report (GET .../test_report, from the jobs' JUnit artifacts) becomes test events of the stage
  whose job made each suite;
- each environment's last deployment with the status success becomes a service.deployed event.

A gated stage of manual jobs (when: manual) starts when its gate opens: dispatch plays them. A run again or a roll
back (maxpm pipeline rerun, rollback) of a commit retries the stage's jobs in that commit's pipeline, which is
GitLab's own roll back of an environment. cancel cancels the stage's running jobs. The token is the value of a
maxpm secret (a project access token with the api scope), sent as PRIVATE-TOKEN. FETCH is the test hook, as in
github.py.
"""

import json
import urllib.parse

from .. import core
from . import cdevents, github, store

POLL_EVERY = 60
PIPELINES = 10
FETCH = None
_STATE = {"success": "passed", "failed": "failed", "canceled": "cancelled", "skipped": "skipped", "manual": "queued",
          "created": "queued", "pending": "queued", "waiting_for_resource": "queued", "preparing": "queued",
          "scheduled": "queued", "running": "running"}
_TEST = {"success": "pass", "failed": "fail", "error": "error", "skipped": "skip"}


def _base(p):
    return (p["forge_url"] or "https://gitlab.com").rstrip("/") + "/api/v4"


def _pid(p):
    return urllib.parse.quote(p["repo"], safe="")


def call(conn, p, method, path, body=None):
    """One call of the GitLab API for pipeline p, as JSON. Refuses on a status of 300 or more."""
    headers = {"PRIVATE-TOKEN": github._token(conn, p), "Accept": "application/json", "User-Agent": "maxpm-pipeline"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    status, out = (FETCH or github._fetch)(method, _base(p) + path, headers, data)
    if status >= 300:
        try:
            msg = json.loads(out or b"{}").get("message") or json.loads(out or b"{}").get("error") or ""
        except (ValueError, AttributeError):
            msg = (out or b"")[:200].decode("utf-8", "replace")
        raise core.RiverError(f"GitLab answered {status} to {method} {path}: {msg}")
    return json.loads(out) if out else {}


def _stage_state(jobs):
    """One state for the jobs of a GitLab stage."""
    states = [("passed" if j.get("allow_failure") and j["status"] == "failed" else _STATE.get(j["status"], "queued"))
              for j in jobs]
    for s in ("running", "failed", "queued", "cancelled"):
        if s in states:
            return s
    return "passed" if "passed" in states else "skipped"


def _waiting(conn, p, s, sha):
    r = conn.execute("SELECT r.id FROM stage_runs r JOIN pipeline_versions v ON v.id=r.version_id WHERE r.stage_id=? "
                     "AND r.state='queued' AND (r.key IS NULL OR r.key NOT LIKE 'gl-%') AND v.rev=? ORDER BY r.id "
                     "LIMIT 1", (s["id"], sha)).fetchone()
    return r["id"] if r else None


def _events(conn, p, s, pl, jobs):
    """The pipelinerun and taskrun events of one MaximizePM stage in one GitLab pipeline."""
    key = f"gl-{pl['id']}-{s['name']}"
    state = _stage_state(jobs)
    stamp = max((j.get("finished_at") or j.get("started_at") or j.get("created_at") or "") for j in jobs) or None
    extra = {"pipeline": p["name"], "stage": s["name"], "version": pl.get("sha"), "ref": pl.get("ref"),
             "run": _waiting(conn, p, s, pl.get("sha")) if not conn.execute(
                 "SELECT 1 FROM stage_runs WHERE pipeline_id=? AND key=?", (p["id"], key)).fetchone() else None}
    base = {"pipelineName": s["name"], "url": pl.get("web_url")}
    sig = "-".join(f"{j['id']}:{j['status']}" for j in sorted(jobs, key=lambda j: j["id"]))
    out = [cdevents.make("pipelinerun", "queued", key, base, extra, "gitlab", jobs[0].get("created_at"), f"{key}-queued")]
    started = [j["started_at"] for j in jobs if j.get("started_at")]
    if started:
        out.append(cdevents.make("pipelinerun", "started", key, base, extra, "gitlab", min(started),
                                 f"{key}-started-{min(started)}"))
    if state in ("passed", "failed", "cancelled", "skipped"):
        word = {"passed": "success", "failed": "failure", "cancelled": "cancel", "skipped": "skip"}[state]
        bad = [j["name"] for j in jobs if j["status"] == "failed" and not j.get("allow_failure")]
        out.append(cdevents.make("pipelinerun", "finished", key,
                                 {**base, "outcome": word, "errors": ("failed: " + ", ".join(bad)) if bad else None},
                                 extra, "gitlab", stamp, f"{key}-finished-{sig}"))
    for j in jobs:
        jk = f"gl-job-{j['id']}"
        jb = {"taskName": j.get("name") or jk, "pipelineRun": {"id": key, "source": "gitlab"}, "url": j.get("web_url")}
        je = {"pipeline": p["name"]}
        if j.get("started_at"):
            out.append(cdevents.make("taskrun", "started", jk, jb, je, "gitlab", j["started_at"], jk + "-started"))
        if j.get("finished_at"):
            out.append(cdevents.make("taskrun", "finished", jk,
                                     {**jb, "outcome": "success" if j["status"] == "success" or j.get("allow_failure")
                                      else "failure" if j["status"] == "failed" else "cancel"}, je, "gitlab",
                                     j["finished_at"], jk + "-finished"))
    return out, state


def _tests(conn, p, pl, by_job_stage, stage_runs):
    """The pipeline's test report as test events, each suite to the stage run of the job that made it, once."""
    report = call(conn, p, "GET", f"/projects/{_pid(p)}/pipelines/{pl['id']}/test_report")
    events = []
    for suite in report.get("test_suites", []):
        run_id = stage_runs.get(by_job_stage.get(suite.get("name")))
        if not run_id or conn.execute("SELECT 1 FROM test_suites WHERE run_id=? AND name=? LIMIT 1",
                                      (run_id, suite["name"])).fetchone():
            continue
        skey = f"gl-suite-{pl['id']}-{suite['name']}"
        base = {"pipeline": p["name"], "run": run_id}
        events.append(cdevents.make("testsuiterun", "started", skey, {"testSuite": {"id": suite["name"],
                                                                                    "name": suite["name"]}}, base,
                                    "gitlab", None, skey + "-started"))
        for n, case in enumerate(suite.get("test_cases", [])):
            pred = "skipped" if case.get("status") == "skipped" else "finished"
            events.append(cdevents.make(
                "testcaserun", pred, f"{skey}/{n}",
                {"testCase": {"id": f"{case.get('classname')}.{case.get('name')}", "name": case.get("name")},
                 "testSuiteRun": {"id": skey, "source": "gitlab"}, "outcome": _TEST.get(case.get("status"), "fail"),
                 "reason": (case.get("system_output") or "").strip().splitlines()[0][:300]
                 if case.get("status") in ("failed", "error") and case.get("system_output") else None},
                {**base, "suite": case.get("classname") or suite["name"], "seconds": case.get("execution_time"),
                 "detail": (case.get("system_output") or "")[:4000] or None, "file": case.get("file")},
                "gitlab", None, f"{skey}/{n}"))
        events.append(cdevents.make("testsuiterun", "finished", skey, {"outcome": "fail" if suite.get("failed_count")
                                                                       or suite.get("error_count") else "pass"},
                                    {**base, "seconds": suite.get("total_time")}, "gitlab", None, skey + "-finished"))
    if events:
        store.ingest(conn, events)
    return len(events)


def poll(conn, p):
    """Read pipeline p's GitLab pipelines, jobs, test reports and deployments into events. Returns counts."""
    from . import runner
    counts = {"pipelines": 0, "events": 0, "tests": 0, "deploys": 0}
    stages = {s["workflow"]: s for s in conn.execute(
        "SELECT * FROM pipeline_stages WHERE pipeline_id=? AND removed_at IS NULL AND workflow IS NOT NULL", (p["id"],))}
    pls = call(conn, p, "GET", f"/projects/{_pid(p)}/pipelines?per_page={PIPELINES}")
    for pl in reversed(pls if isinstance(pls, list) else []):  # oldest first
        counts["pipelines"] += 1
        jobs = call(conn, p, "GET", f"/projects/{_pid(p)}/pipelines/{pl['id']}/jobs?per_page=100")
        by_stage, by_job_stage, runs, done = {}, {}, {}, []
        for j in jobs if isinstance(jobs, list) else []:
            by_stage.setdefault(j.get("stage"), []).append(j)
            by_job_stage[j.get("name")] = j.get("stage")
        for gl_stage, js in by_stage.items():
            s = stages.get(gl_stage)
            if s is None:
                continue
            evs, state = _events(conn, p, s, pl, js)
            evs = [e for e in evs if not github._seen_by(conn, "gitlab", e["context"]["id"])]
            finished = any(e["context"]["type"].startswith("dev.cdevents.pipelinerun.finished") for e in evs)
            if evs:
                store.ingest(conn, evs)
                counts["events"] += len(evs)
            row = conn.execute("SELECT id, state FROM stage_runs WHERE pipeline_id=? AND key=?",
                               (p["id"], f"gl-{pl['id']}-{gl_stage}")).fetchone()
            if row:
                runs[gl_stage] = row["id"]
                if finished and row["state"] == "passed":
                    done.append(row["id"])
        if runs and pl.get("status") in ("success", "failed", "canceled"):
            counts["tests"] += _tests(conn, p, pl, by_job_stage, runs)
        for run_id in done:
            runner.hand_on(conn, run_id)
    for e in call(conn, p, "GET", f"/projects/{_pid(p)}/environments?per_page=50") or []:
        d = e.get("last_deployment") or {}
        if d.get("status") != "success" or not conn.execute(
                "SELECT 1 FROM pipeline_envs WHERE pipeline_id=? AND name=? AND removed_at IS NULL",
                (p["id"], e["name"])).fetchone():
            continue
        eid = f"gl-dep-{d['id']}"
        if github._seen_by(conn, "gitlab", eid):
            continue
        store.ingest(conn, cdevents.make("service", "deployed", f"{p['name']}/{e['name']}",
                                         {"environment": {"id": e["name"], "source": "gitlab"}},
                                         {"pipeline": p["name"], "version": d.get("sha") or (d.get("deployable") or {})
                                          .get("commit", {}).get("id")}, "gitlab", d.get("created_at"), eid))
        counts["deploys"] += 1
    return counts


def poll_due(conn):
    """Each GitLab pipeline whose last read is POLL_EVERY old; an error stays on it (poll_error)."""
    out = {}
    old = core.iso(core.now() - core.timedelta(seconds=POLL_EVERY))
    for p in conn.execute("SELECT * FROM pipelines WHERE forge='gitlab' AND repo IS NOT NULL AND "
                          "(polled_at IS NULL OR polled_at<=?)", (old,)).fetchall():
        try:
            out[p["name"]], err = poll(conn, p), None
        except (core.RiverError, OSError, ValueError, KeyError) as e:
            out[p["name"]], err = {"error": str(e)}, str(e)[:500]
        with core.tx(conn):
            conn.execute("UPDATE pipelines SET polled_at=?, poll_error=? WHERE id=?", (core.iso(core.now()), err, p["id"]))
    return out


def _stage_jobs(conn, p, gl_stage, sha):
    pls = call(conn, p, "GET", f"/projects/{_pid(p)}/pipelines?sha={sha}&per_page=1")
    if not pls:
        raise core.RiverError(f"GitLab has no pipeline for {sha} yet")
    jobs = call(conn, p, "GET", f"/projects/{_pid(p)}/pipelines/{pls[0]['id']}/jobs?per_page=100")
    return pls[0], [j for j in jobs if j.get("stage") == gl_stage]


def dispatch(conn, run_id):
    """A gate opened: play the manual jobs of the stage in the pipeline of the run's commit, or retry its jobs (a run
    again, a roll back)."""
    r = conn.execute("SELECT r.*, s.workflow, v.rev FROM stage_runs r JOIN pipeline_stages s ON s.id=r.stage_id LEFT "
                     "JOIN pipeline_versions v ON v.id=r.version_id WHERE r.id=?", (run_id,)).fetchone()
    p = conn.execute("SELECT * FROM pipelines WHERE id=?", (r["pipeline_id"],)).fetchone()
    pl, jobs = _stage_jobs(conn, p, r["workflow"], r["rev"])
    if not jobs:
        raise core.RiverError(f"the GitLab pipeline of {r['rev']} has no job in stage {r['workflow']}")
    for j in jobs:
        what = "play" if j["status"] == "manual" else "retry" if j["status"] in ("success", "failed", "canceled") \
            else None
        if what:
            call(conn, p, "POST", f"/projects/{_pid(p)}/jobs/{j['id']}/{what}")
    key = f"gl-{pl['id']}-{r['workflow']}"
    with core.tx(conn):
        # The jobs of this stage in that GitLab pipeline belong to this run from now on: an earlier run of them (the
        # run that a run again or a roll back repeats) keeps its record under another key.
        conn.execute("UPDATE stage_runs SET key=key || '~' || id WHERE pipeline_id=? AND key=? AND id<>?",
                     (r["pipeline_id"], key, run_id))
        conn.execute("UPDATE stage_runs SET runner='forge:gitlab', key=?, gate_note=? WHERE id=?",
                     (key, f"started on GitLab (stage {r['workflow']} of pipeline {pl['id']}); its jobs show within a "
                      "minute", run_id))


def cancel(conn, run_id):
    """Cancel the running jobs of a stage run on GitLab."""
    r = conn.execute("SELECT r.*, s.workflow, v.rev FROM stage_runs r JOIN pipeline_stages s ON s.id=r.stage_id LEFT "
                     "JOIN pipeline_versions v ON v.id=r.version_id WHERE r.id=?", (run_id,)).fetchone()
    p = conn.execute("SELECT * FROM pipelines WHERE id=?", (r["pipeline_id"],)).fetchone()
    _pl, jobs = _stage_jobs(conn, p, r["workflow"], r["rev"])
    n = 0
    for j in jobs:
        if j["status"] in ("running", "pending", "created"):
            call(conn, p, "POST", f"/projects/{_pid(p)}/jobs/{j['id']}/cancel")
            n += 1
    return {"run": run_id, "gitlab_jobs": n, "cancelled": True}
