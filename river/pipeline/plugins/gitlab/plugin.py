"""gitlab: the MaximizePM pipeline plugin for GitLab CI (gitlab.com or a GitLab server of your own).

A worked example of the plugin contract (maxpm guide pipelines, 'Write a plugin') for a system where one pipeline
holds every stage of a commit. MaximizePM keeps the order and the transitions (an approval gate on GitLab needs a
paid plan; here it needs none).

Each MaximizePM stage names a GitLab stage (maxpm pipeline stage <p> <s> --workflow <GitLab stage>). One MaximizePM
stage run is the jobs of that GitLab stage in the pipeline of one commit: its key is gl-<pipeline id>-<stage>.

- poll: the newest PIPELINES pipelines. For each MaximizePM stage, its jobs give one pipelinerun (its state from the
  jobs: a failed job that may not fail fails it; a manual job that waits keeps it queued) and a taskrun for each job.
  A finished pipeline's test report (GitLab makes it from the jobs' JUnit artifacts) gives test events, each suite to
  the stage of the job that made it. Each environment's last deployment with status success gives service.deployed.
- start: plays the manual jobs of the stage in the pipeline of the run's commit (when: manual in .gitlab-ci.yml).
- rerun, rollback: retries the stage's jobs in that commit's pipeline. That is GitLab's own roll back of an
  environment: the deploy job of an older pipeline runs again.
- stop: cancels the stage's running jobs.
- agents (#6172): when the request lists agent jobs that MaximizePM waits for, the job maxpm-agent (in .gitlab-ci.yml,
  written by maxpm runner gitlab) of each failed run's GitLab pipeline goes back as "agents": its state, and at its
  end the proof and the transcript of its artifact. stop of such a run cancels that job.

What it cannot do, and why: a stage whose jobs start by themselves (no when: manual) runs before its transition
opens; give its transition the mode forge, so MaximizePM only reads it. The token: a project access token with the
api scope, sent as PRIVATE-TOKEN.
"""

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

PIPELINES = 10  # the newest pipelines that a poll reads
TESTED_KEEP = 200  # pipelines whose test report the state remembers as read
STATE = {"success": "passed", "failed": "failed", "canceled": "cancelled", "skipped": "skipped", "manual": "queued",
         "created": "queued", "pending": "queued", "waiting_for_resource": "queued", "preparing": "queued",
         "scheduled": "queued", "running": "running"}
TEST = {"success": "pass", "failed": "fail", "error": "error", "skipped": "skip"}
OUTCOME = {"passed": "success", "failed": "failure", "cancelled": "cancel", "skipped": "skip"}


class Failed(Exception):
    """A call that did not work: main() answers {"error": <its words>} and exits 1."""


def http(method, url, headers, body):
    """One HTTP call: (status, bytes). Tests pass their own function with the same form (recorded answers)."""
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class Api:
    def __init__(self, req, fetch):
        s = req.get("settings") or {}
        self.token = (req.get("secrets") or {}).get("token")
        if not s.get("repo"):
            raise Failed("the pipeline names no project: maxpm pipeline forge <pipeline> gitlab --repo group/project")
        if not self.token:
            raise Failed(f"the pipeline names no secret for its GitLab token: maxpm pipeline forge <pipeline> gitlab "
                         f"--repo {s['repo']} --secret <name>")
        self.base = (s.get("url") or "https://gitlab.com").rstrip("/") + "/api/v4"
        self.project = "/projects/" + urllib.parse.quote(s["repo"], safe="")  # group%2Fproject
        self.fetch = fetch

    def call(self, method, path, body=None, raw=False):
        """The answer as JSON (raw: bytes). A status of 300 or more fails with GitLab's own message."""
        headers = {"PRIVATE-TOKEN": self.token, "Accept": "application/json", "User-Agent": "maxpm-pipeline"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        status, out = self.fetch(method, self.base + self.project + path, headers, data)
        if status >= 300:
            try:
                got = json.loads(out or b"{}")
                msg = got.get("message") or got.get("error") or ""
            except (ValueError, AttributeError):
                msg = (out or b"")[:200].decode("utf-8", "replace")
            raise Failed(f"GitLab answered {status} to {method} {self.project}{path}: {msg}")
        if raw:
            return out or b""
        return json.loads(out) if out else {}


def event(subject, predicate, subject_id, content, maxpm, at, event_id):
    """One CDEvents event. MaximizePM keeps each event id once, so the same change read again gives the same id."""
    types = {"pipelinerun": "pipelineRun", "taskrun": "taskRun", "testsuiterun": "testSuiteRun",
             "testcaserun": "testCaseRun", "service": "service"}
    ctx = {"version": "0.4.1", "id": event_id, "source": "gitlab", "type": f"dev.cdevents.{subject}.{predicate}.0.2.0"}
    if at:
        ctx["timestamp"] = at
    return {"context": ctx, "subject": {"id": subject_id, "type": types[subject],
                                        "content": {k: v for k, v in content.items() if v is not None}},
            "customData": {"maxpm": {k: v for k, v in maxpm.items() if v is not None}}}


def stage_state(jobs):
    """One state for the jobs of a GitLab stage."""
    states = [("passed" if j.get("allow_failure") and j["status"] == "failed" else STATE.get(j["status"], "queued"))
              for j in jobs]
    for s in ("running", "failed", "queued", "cancelled"):
        if s in states:
            return s
    return "passed" if "passed" in states else "skipped"


def waiting_for(stage, sha, runs):
    """MaximizePM's run that waits at this stage for this commit, when no run has this pipeline's key yet."""
    for r in sorted(runs, key=lambda r: r["id"]):
        if r["stage"] == stage and r["state"] == "queued" and not (r.get("key") or "").startswith("gl-") \
                and r.get("version") == sha:
            return r["id"]
    return None


def stage_events(stage, gl_stage, pl, jobs, runs, known):
    """The pipelinerun and taskrun events of one MaximizePM stage in one GitLab pipeline. The key names the GitLab
    stage, as the key of start does."""
    key = f"gl-{pl['id']}-{gl_stage}"
    state = stage_state(jobs)
    stamp = max((j.get("finished_at") or j.get("started_at") or j.get("created_at") or "") for j in jobs) or None
    maxpm = {"stage": stage, "version": pl.get("sha"), "ref": pl.get("ref"),
             "run": None if key in known else waiting_for(stage, pl.get("sha"), runs)}
    base = {"pipelineName": stage, "url": pl.get("web_url")}
    sig = "-".join(f"{j['id']}:{j['status']}" for j in sorted(jobs, key=lambda j: j["id"]))
    out = [event("pipelinerun", "queued", key, base, maxpm, jobs[0].get("created_at"), f"{key}-queued")]
    started = [j["started_at"] for j in jobs if j.get("started_at")]
    if started:
        out.append(event("pipelinerun", "started", key, base, maxpm, min(started), f"{key}-started-{min(started)}"))
    if state in OUTCOME:
        bad = [j["name"] for j in jobs if j["status"] == "failed" and not j.get("allow_failure")]
        out.append(event("pipelinerun", "finished", key, {**base, "outcome": OUTCOME[state],
                                                          "errors": ("failed: " + ", ".join(bad)) if bad else None},
                         maxpm, stamp, f"{key}-finished-{sig}"))
    for j in jobs:
        jk = f"gl-job-{j['id']}"
        jb = {"taskName": j.get("name") or jk, "pipelineRun": {"id": key, "source": "gitlab"}, "url": j.get("web_url")}
        if j.get("started_at"):
            out.append(event("taskrun", "started", jk, jb, {}, j["started_at"], jk + "-started"))
        if j.get("finished_at"):
            ok = j["status"] == "success" or j.get("allow_failure")
            out.append(event("taskrun", "finished", jk, {**jb, "outcome": "success" if ok else "failure"
                                                         if j["status"] == "failed" else "cancel"}, {},
                             j["finished_at"], jk + "-finished"))
    return out


def test_events(pl, report, job_stage, stages):
    """The pipeline's test report as test events: each suite to the stage run of the job that made it."""
    out = []
    for suite in report.get("test_suites", []):
        stage = job_stage.get(suite.get("name"))
        if stage not in stages:
            continue
        run_key, skey = f"gl-{pl['id']}-{stage}", f"gl-suite-{pl['id']}-{suite['name']}"
        out.append(event("testsuiterun", "started", skey, {"testSuite": {"id": suite["name"], "name": suite["name"]}},
                         {"run_key": run_key}, None, skey + "-started"))
        for n, case in enumerate(suite.get("test_cases", [])):
            failed = case.get("status") in ("failed", "error") and case.get("system_output")
            out.append(event("testcaserun", "skipped" if case.get("status") == "skipped" else "finished", f"{skey}/{n}",
                             {"testCase": {"id": f"{case.get('classname')}.{case.get('name')}", "name": case.get("name")},
                              "testSuiteRun": {"id": skey, "source": "gitlab"},
                              "outcome": TEST.get(case.get("status"), "fail"),
                              "reason": case["system_output"].strip().splitlines()[0][:300] if failed else None},
                             {"run_key": run_key, "suite": case.get("classname") or suite["name"],
                              "seconds": case.get("execution_time"), "file": case.get("file"),
                              "detail": (case.get("system_output") or "")[:4000] or None}, None, f"{skey}/{n}"))
        out.append(event("testsuiterun", "finished", skey,
                         {"outcome": "fail" if suite.get("failed_count") or suite.get("error_count") else "pass"},
                         {"run_key": run_key, "seconds": suite.get("total_time")}, None, skey + "-finished"))
    return out


def poll(req, api):
    """Read the newest pipelines (oldest first, so a newer version is handed on last), their test reports, and the
    environments' deployments. The state keeps the pipelines whose test report was read."""
    state = req.get("state") or {}
    tested = list(state.get("tested") or [])
    runs = req.get("runs") or []
    known = {r.get("key") for r in runs}
    stages = {s["work"]: s for s in (req.get("pipeline") or {}).get("stages") or [] if s.get("work")}
    events, count = [], 0
    pls = api.call("GET", f"/pipelines?per_page={PIPELINES}")
    for pl in reversed(pls if isinstance(pls, list) else []):
        count += 1
        jobs = api.call("GET", f"/pipelines/{pl['id']}/jobs?per_page=100")
        by_stage, job_stage = {}, {}
        for j in jobs if isinstance(jobs, list) else []:
            by_stage.setdefault(j.get("stage"), []).append(j)
            job_stage[j.get("name")] = j.get("stage")
        mine = {gl_stage: js for gl_stage, js in by_stage.items() if gl_stage in stages}
        for gl_stage, js in mine.items():
            events += stage_events(stages[gl_stage]["name"], gl_stage, pl, js, runs, known)
        if mine and pl.get("status") in ("success", "failed", "canceled") and pl["id"] not in tested:
            got = test_events(pl, api.call("GET", f"/pipelines/{pl['id']}/test_report"), job_stage, stages)
            events += got
            if got:
                tested.append(pl["id"])
    envs = {e["name"] for e in (req.get("pipeline") or {}).get("envs") or []}
    for e in api.call("GET", "/environments?per_page=50") or []:
        d = e.get("last_deployment") or {}
        if d.get("status") != "success" or e["name"] not in envs:
            continue
        sha = d.get("sha") or ((d.get("deployable") or {}).get("commit") or {}).get("id")
        events.append(event("service", "deployed", e["name"], {"environment": {"id": e["name"], "source": "gitlab"}},
                            {"version": sha}, d.get("created_at"), f"gl-dep-{d['id']}"))
    answer = {"events": events, "counts": {"pipelines": count}, "state": {**state, "tested": tested[-TESTED_KEEP:]}}
    if req.get("agents"):
        answer["agents"] = agent_runs(api, req)
    return answer


# ---------------------------------------------------------------- the agent job (#6172)

AGENT_JOB = "maxpm-agent"  # the job that maxpm runner gitlab writes into .gitlab-ci.yml (stage .post, on_failure)
AGENT_DIR = "maxpm-agent"  # its artifact's folder, in the project folder
TRANSCRIPT_KEEP = 400  # the last lines of the transcript that go back
AGENT_STATE = {"running": "running", "success": "ended", "failed": "failed", "canceled": "stopped",
               "skipped": "stopped"}  # any other state of a job waits: queued
LIVE = ("created", "pending", "running", "waiting_for_resource", "preparing", "scheduled", "manual")


def agent_runs(api, req):
    """For each run that MaximizePM waits for ({"run", "key": "gl-<pipeline id>-<stage>"}): the job maxpm-agent of
    that GitLab pipeline, its state, and at its end the proof and the transcript of its artifact. A pipeline whose
    jobs all ended with no such job will not get it (the commit's .gitlab-ci.yml has none, or its rule did not
    match): the run ends at once, as failed, with the reason."""
    asks = {}
    for a in req["agents"]:
        m = re.match(r"gl-(\d+)-", str(a.get("key") or ""))
        if m:
            asks.setdefault(m.group(1), []).append(a)
    out = []
    for pl, runs in asks.items():
        jobs = api.call("GET", f"/pipelines/{pl}/jobs?per_page=100")
        jobs = jobs if isinstance(jobs, list) else []
        job = next((j for j in jobs if j.get("name") == AGENT_JOB), None)
        if job is None:
            if any(j.get("status") in LIVE for j in jobs):
                continue  # the pipeline still runs; GitLab makes the job at the start, so it may come with a retry
            d = {"state": "failed", "note": f"GitLab pipeline {pl} has no job {AGENT_JOB}: the .gitlab-ci.yml of "
                                            f"that commit does not have it (maxpm runner gitlab), or its rule did not "
                                            f"match (another branch)"}
        else:
            d = {"job": f"gl-job-{job['id']}", "url": job.get("web_url"),
                 "started_at": job.get("started_at") or job.get("created_at"),
                 "state": AGENT_STATE.get(job.get("status"), "queued")}
            if d["state"] not in ("queued", "running"):
                d["ended_at"] = job.get("finished_at")
                d.update(agent_artifact(api, job))
        out += [{"run": a["run"], **d} for a in runs]
    return out


def agent_artifact(api, job):
    """proof.json and the end of transcript.jsonl, read by path from the artifact of a finished job."""
    out = {}
    for name in ("proof.json", "transcript.jsonl"):
        try:
            data = api.call("GET", f"/jobs/{job['id']}/artifacts/{AGENT_DIR}/{name}", raw=True)
        except Failed as e:
            if " 404 " not in str(e):
                raise
            if name == "proof.json":
                out["note"] = f"the job left no {AGENT_DIR}/proof.json in its artifact"
            continue
        text = data.decode("utf-8", "replace")
        if name == "proof.json":
            try:
                proof = json.loads(text)
                out["proof"] = proof if isinstance(proof, dict) else {}
            except ValueError:
                out["note"] = "its proof.json is not JSON"
        else:
            out["transcript"] = "\n".join(text.splitlines()[-TRANSCRIPT_KEEP:]) + "\n"
    return out


def stage_jobs(api, gl_stage, sha):
    """The pipeline of a commit and the jobs of one of its stages."""
    pls = api.call("GET", f"/pipelines?sha={sha}&per_page=1")
    if not pls:
        raise Failed(f"GitLab has no pipeline for {sha} yet")
    jobs = api.call("GET", f"/pipelines/{pls[0]['id']}/jobs?per_page=100")
    return pls[0], [j for j in jobs if j.get("stage") == gl_stage]


def act(req, api):
    """start: play the stage's manual jobs; rerun, rollback: retry its jobs (an older commit's for a roll back); stop:
    cancel its running jobs. The answer's key gives the jobs of that pipeline's stage to this run."""
    run = req["run"]
    m = re.match(r"gl-job-(\d+)$", str(run.get("key") or ""))
    if req["call"] == "stop" and m:  # the agent job maxpm-agent (#6172)
        api.call("POST", f"/jobs/{m.group(1)}/cancel")
        return {"note": f"asked GitLab to cancel job {m.group(1)}"}
    pl, jobs = stage_jobs(api, run["work"], run.get("version"))
    if req["call"] == "stop":
        n = 0
        for j in jobs:
            if j["status"] in ("running", "pending", "created"):
                api.call("POST", f"/jobs/{j['id']}/cancel")
                n += 1
        return {"note": f"cancelled {n} GitLab job(s)"}
    if not jobs:
        raise Failed(f"the GitLab pipeline of {run.get('version')} has no job in stage {run['work']}")
    for j in jobs:
        what = "play" if j["status"] == "manual" else "retry" if j["status"] in ("success", "failed", "canceled") \
            else None
        if what:
            api.call("POST", f"/jobs/{j['id']}/{what}")
    return {"key": f"gl-{pl['id']}-{run['work']}",
            "note": f"started on GitLab (stage {run['work']} of pipeline {pl['id']}); its jobs show within a minute"}


def describe(req):
    with open("plugin.json", encoding="utf-8") as f:
        m = json.load(f)
    return {k: m[k] for k in ("name", "version", "level", "actions")}


def handle(req, fetch=http):
    """The answer to one request. fetch: the HTTP function (tests pass recorded answers)."""
    if req["call"] == "describe":
        return describe(req)
    api = Api(req, fetch)
    if req["call"] == "poll":
        return poll(req, api)
    if req["call"] in ("start", "rerun", "rollback", "stop"):
        return act(req, api)
    raise Failed(f"gitlab does not do {req['call']}")


def main():
    req = json.load(sys.stdin)
    try:
        out, code = handle(req), 0
    except Failed as e:
        out, code = {"error": str(e)}, 1
    json.dump(out, sys.stdout)
    sys.exit(code)


if __name__ == "__main__":
    main()
