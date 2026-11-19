"""The GitHub adapter of pipelines (#6119): GitHub Actions runs the work of a stage; MaximizePM holds the order and
the gates (mark 2026-11-18: the gates live in the layer, so a private repository needs no Enterprise plan).

GitHub cannot call MaximizePM (no public address), so maxpm serve reads the REST API once a minute (poll_due, from
runner.serve_pass) for each pipeline whose forge is github (maxpm pipeline forge <name> github --repo owner/name
--secret <name>):

- each stage with a workflow (maxpm pipeline stage ... --workflow tests.yml): its newest runs become pipelinerun
  events (queued, started, finished), their jobs taskrun events, and, after the run, its JUnit artifacts (artifacts
  named junit*, or the stage's --junit name) test events;
- each environment of the pipeline: its newest deployments with a success status become service.deployed events.

Every event has an id made from the GitHub ids, so a run read again changes nothing. A stage run that passed hands
the version on (runner.hand_on), as a run of the no-forge runner does.

A gated stage with a workflow and no command starts on GitHub only when its gate opens: runner.serve_pass calls
dispatch, which sends workflow_dispatch on the pipeline's branch with the inputs maxpm_run (our run's id) and maxpm_rev
(the commit). The workflow names its run 'maxpm run <id>' (run-name) and checks out maxpm_rev; poll links that run to
ours. cancel stops a GitHub run.

The token is the value of a maxpm secret (policy g6: never from the environment or a file of the home folder); a
fine-grained token with Actions read and write, Deployments read and Contents read on the repository. FETCH is a test
hook: fn(method, url, headers, body) -> (status, bytes).
"""

import io
import json
import re
import time
import urllib.error
import urllib.request
import zipfile

from .. import core
from . import cdevents, junit, store

API = "https://api.github.com"
POLL_EVERY = 60  # seconds between two reads of one pipeline
RUNS = 10  # the newest runs of a workflow that a read covers
TOKEN_KEEP = 600  # seconds a token read from the secret store stays in memory (each read is a line of its log)
FETCH = None
_TOKENS = {}
_LINK = re.compile(r"\bmaxpm run (\d+)\b")
_CONCLUSION = {"success": "success", "failure": "failure", "timed_out": "failure", "startup_failure": "failure",
               "cancelled": "cancel", "skipped": "skip", "neutral": "success", "stale": "cancel"}


def _token(conn, p):
    if not p["secret"]:
        raise core.RiverError(f"pipeline {p['name']} names no secret for its GitHub token: maxpm pipeline forge "
                              f"{p['name']} github --repo {p['repo'] or 'owner/name'} --secret <name>")
    got = _TOKENS.get(p["secret"])
    if got and time.monotonic() - got[1] < TOKEN_KEEP:
        return got[0]
    from .. import vault
    value = vault.get(conn, p["secret"], use=f"pipeline {p['name']}: the GitHub API").decode().strip()
    _TOKENS[p["secret"]] = (value, time.monotonic())
    return value


def _fetch(method, url, headers, body):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def call(conn, p, method, path, body=None, raw=False):
    """One call of the GitHub API for pipeline p: the answer as JSON (raw: bytes). Refuses on a status of 300 or
    more, with GitHub's message."""
    headers = {"Authorization": f"Bearer {_token(conn, p)}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "maxpm-pipeline"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    url = path if path.startswith("https://") else API + path
    status, out = (FETCH or _fetch)(method, url, headers, data)
    if status >= 300:
        try:
            msg = json.loads(out or b"{}").get("message") or ""
        except ValueError:
            msg = (out or b"")[:200].decode("utf-8", "replace")
        raise core.RiverError(f"GitHub answered {status} to {method} {path}: {msg}")
    if raw:
        return out
    return json.loads(out) if out else {}


def _stages(conn, p):
    return conn.execute("SELECT * FROM pipeline_stages WHERE pipeline_id=? AND removed_at IS NULL AND workflow IS NOT "
                        "NULL ORDER BY pos", (p["id"],)).fetchall()


def _seen(conn, event_id):
    return conn.execute("SELECT 1 FROM pipeline_events WHERE source='github' AND event_id=?", (event_id,)).fetchone()


def _waiting(conn, p, s, run):
    """A run of ours that waits at this stage for the version of a GitHub run that names none (a push or a tag
    started it: the gate forge): the GitHub run is that one."""
    if _LINK.search(run.get("display_title") or "") or not run.get("head_sha"):
        return None
    if conn.execute("SELECT 1 FROM stage_runs WHERE pipeline_id=? AND key=?", (p["id"], f"gh-run-{run['id']}")).fetchone():
        return None
    r = conn.execute("SELECT r.id FROM stage_runs r JOIN pipeline_versions v ON v.id=r.version_id WHERE r.stage_id=? "
                     "AND r.state='queued' AND (r.key IS NULL OR r.key NOT LIKE 'gh-run-%') AND (v.rev=? OR "
                     "substr(?,1,length(v.rev))=v.rev) ORDER BY r.id LIMIT 1", (s["id"], run["head_sha"],
                                                                                run["head_sha"])).fetchone()
    return r["id"] if r else None


def _run_events(p, s, run, waiting=None):
    """The pipelinerun events of one workflow run (waiting: our run that it is, when its title names none)."""
    key = f"gh-run-{run['id']}"
    m = _LINK.search(run.get("display_title") or run.get("name") or "")
    extra = {"pipeline": p["name"], "stage": s["name"], "version": run.get("head_sha"), "ref": run.get("head_branch"),
             "title": (run.get("display_title") or "")[:200] or None, "run": int(m.group(1)) if m else waiting}
    base = {"pipelineName": s["name"], "url": run.get("html_url")}
    attempt = run.get("run_attempt") or 1
    out = [cdevents.make("pipelinerun", "queued", key, base, extra, "github", _at(run.get("created_at")),
                         f"gh-run-{run['id']}-{attempt}-queued")]
    if run.get("status") in ("in_progress", "completed") and run.get("run_started_at"):
        out.append(cdevents.make("pipelinerun", "started", key, base, extra, "github", _at(run["run_started_at"]),
                                 f"gh-run-{run['id']}-{attempt}-started"))
    if run.get("status") == "completed":
        out.append(cdevents.make("pipelinerun", "finished", key,
                                 {**base, "outcome": _CONCLUSION.get(run.get("conclusion"), "failure"),
                                  "errors": None if run.get("conclusion") == "success" else run.get("conclusion")},
                                 extra, "github", _at(run.get("updated_at")), f"gh-run-{run['id']}-{attempt}-finished"))
    return out


def _at(s):
    return s or core.iso(core.now())


def _jobs(conn, p, run_id, gh_id):
    jobs = call(conn, p, "GET", f"/repos/{p['repo']}/actions/runs/{gh_id}/jobs?per_page=100").get("jobs", [])
    out = []
    for j in jobs:
        key = f"gh-job-{j['id']}"
        extra = {"pipeline": p["name"], "run": run_id, "kind": None}
        base = {"taskName": j.get("name") or key, "pipelineRun": {"id": f"gh-run-{gh_id}", "source": "github"},
                "url": j.get("html_url")}
        if j.get("started_at"):
            out.append(cdevents.make("taskrun", "started", key, base, extra, "github", j["started_at"], key + "-started"))
        if j.get("status") == "completed":
            out.append(cdevents.make("taskrun", "finished", key,
                                     {**base, "outcome": _CONCLUSION.get(j.get("conclusion"), "failure")}, extra,
                                     "github", _at(j.get("completed_at")), key + "-finished"))
    return out


def _artifacts(conn, p, s, run_id, gh_id):
    """The JUnit reports of a finished run, once: its artifacts named junit* (or the stage's --junit name)."""
    if conn.execute("SELECT 1 FROM test_suites WHERE run_id=? LIMIT 1", (run_id,)).fetchone():
        return 0
    arts = call(conn, p, "GET", f"/repos/{p['repo']}/actions/runs/{gh_id}/artifacts?per_page=100").get("artifacts", [])
    want = s["junit"]
    n = 0
    for a in arts:
        if a.get("expired") or not (a["name"] == want if want else a["name"].lower().startswith("junit")):
            continue
        data = call(conn, p, "GET", a["archive_download_url"], raw=True)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for name in z.namelist():
                if name.lower().endswith(".xml"):
                    junit.ingest_junit(conn, run_id, xml=z.read(name), source="github")
                    n += 1
    return n


def _deployments(conn, p):
    events = []
    for e in conn.execute("SELECT name FROM pipeline_envs WHERE pipeline_id=? AND removed_at IS NULL", (p["id"],)):
        deps = call(conn, p, "GET", f"/repos/{p['repo']}/deployments?environment={e['name']}&per_page=5")
        for d in deps if isinstance(deps, list) else []:
            sts = call(conn, p, "GET", f"/repos/{p['repo']}/deployments/{d['id']}/statuses?per_page=1")
            if not sts or sts[0].get("state") != "success":
                continue
            eid = f"gh-dep-{d['id']}-{sts[0]['id']}"
            if _seen(conn, eid):
                continue
            events.append(cdevents.make("service", "deployed", f"{p['name']}/{e['name']}",
                                        {"environment": {"id": e["name"], "source": "github"}},
                                        {"pipeline": p["name"], "version": d.get("sha")}, "github",
                                        _at(sts[0].get("created_at")), eid))
    return events


def poll(conn, p):
    """Read pipeline p's runs, jobs, test reports and deployments from GitHub into events. Returns counts."""
    from . import runner
    counts = {"runs": 0, "events": 0, "tests": 0, "deploys": 0}
    for s in _stages(conn, p):
        wf = s["workflow"]
        runs = call(conn, p, "GET", f"/repos/{p['repo']}/actions/workflows/{wf}/runs?per_page={RUNS}")
        for run in reversed(runs.get("workflow_runs", [])):  # oldest first, so a newer version is handed on last
            counts["runs"] += 1
            evs = [e for e in _run_events(p, s, run, _waiting(conn, p, s, run)) if not _seen(conn, e["context"]["id"])]
            if not evs and run.get("status") == "completed":
                continue  # read before, and over: nothing more to read
            res = store.ingest(conn, evs) if evs else []
            counts["events"] += len(res)
            row = conn.execute("SELECT id, state FROM stage_runs WHERE pipeline_id=? AND key=?",
                               (p["id"], f"gh-run-{run['id']}")).fetchone()
            if row is None:
                continue
            jobs = [e for e in _jobs(conn, p, row["id"], run["id"]) if not _seen(conn, e["context"]["id"])]
            if jobs:
                store.ingest(conn, jobs)
                counts["events"] += len(jobs)
            if run.get("status") == "completed":
                counts["tests"] += _artifacts(conn, p, s, row["id"], run["id"])
                if any(e["context"]["type"].startswith("dev.cdevents.pipelinerun.finished") for e in evs) \
                        and row["state"] == "passed":
                    runner.hand_on(conn, row["id"])
    deps = _deployments(conn, p)
    if deps:
        store.ingest(conn, deps)
        counts["deploys"] = len(deps)
    return counts


def poll_due(conn):
    """One pass of maxpm serve: read each GitHub pipeline whose last read is POLL_EVERY old. An error of one
    pipeline is kept on it (poll_error) and does not stop the others."""
    out = {}
    old = core.iso(core.now() - core.timedelta(seconds=POLL_EVERY))
    for p in conn.execute("SELECT * FROM pipelines WHERE forge='github' AND repo IS NOT NULL AND "
                          "(polled_at IS NULL OR polled_at<=?)", (old,)).fetchall():
        try:
            out[p["name"]] = poll(conn, p)
            err = None
        except (core.RiverError, OSError, ValueError, zipfile.BadZipFile) as e:
            out[p["name"]], err = {"error": str(e)}, str(e)[:500]
        with core.tx(conn):
            conn.execute("UPDATE pipelines SET polled_at=?, poll_error=? WHERE id=?", (core.iso(core.now()), err, p["id"]))
    return out


def dispatch(conn, run_id):
    """Start the workflow of a stage run whose gate opened: workflow_dispatch with maxpm_run and maxpm_rev."""
    r = conn.execute("SELECT r.*, s.workflow, s.name stage, v.rev FROM stage_runs r JOIN pipeline_stages s ON "
                     "s.id=r.stage_id LEFT JOIN pipeline_versions v ON v.id=r.version_id WHERE r.id=?", (run_id,)).fetchone()
    p = conn.execute("SELECT * FROM pipelines WHERE id=?", (r["pipeline_id"],)).fetchone()
    call(conn, p, "POST", f"/repos/{p['repo']}/actions/workflows/{r['workflow']}/dispatches",
         {"ref": p["ref"] or "main", "inputs": {"maxpm_run": str(run_id), "maxpm_rev": r["rev"] or ""}})
    with core.tx(conn):
        conn.execute("UPDATE stage_runs SET runner='forge:github', gate_note=? WHERE id=?",
                     (f"started on GitHub ({r['workflow']}); its run shows within a minute", run_id))


def cancel(conn, run_id):
    """Stop the GitHub run of a stage run."""
    r = conn.execute("SELECT * FROM stage_runs WHERE id=?", (run_id,)).fetchone()
    m = re.match(r"gh-run-(\d+)$", r["key"] or "")
    if not m:
        raise core.RiverError(f"run #{run_id} has no GitHub run yet; it shows within a minute of its start")
    p = conn.execute("SELECT * FROM pipelines WHERE id=?", (r["pipeline_id"],)).fetchone()
    call(conn, p, "POST", f"/repos/{p['repo']}/actions/runs/{m.group(1)}/cancel")
    return {"run": run_id, "github": int(m.group(1)), "cancelled": True}
