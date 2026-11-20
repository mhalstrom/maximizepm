"""github: the MaximizePM pipeline plugin for GitHub Actions, and for Gitea and Forgejo, which have GitHub's API.

It is also the worked example of the plugin contract (maxpm guide pipelines, 'Write a plugin'). Copy it for another
CI system: keep main(), describe() and the shape of poll() and act(); change the calls of the API.

MaximizePM runs this program once for each call, with one JSON request on stdin; it prints one JSON answer.
- poll: for each stage that names a workflow (maxpm pipeline stage ... --workflow tests.yml): the newest runs of that
  workflow become pipelinerun events, their jobs taskrun events, and a finished run's JUnit artifacts (named junit*,
  or the stage's --junit name) go back as reports, which MaximizePM reads. On GitHub, each environment's deployments
  with a success status become service.deployed events, and once an hour the names of the secrets of each deploy
  stage's environment go back as "secrets" (names only; the stage card shows them as keys that GitHub keeps).
- start, rerun, rollback: workflow_dispatch of the stage's workflow on the branch (setting ref), with the inputs
  maxpm_run (MaximizePM's run id) and maxpm_rev (the commit). The workflow names its run 'maxpm run <id>' (run-name)
  and checks out maxpm_rev; the next poll links the two runs by that name.
- stop: cancels the run.
- agents (#6152): when the request lists agent jobs that MaximizePM waits for, the runs of the job maxpm-agent
  (.github/workflows/agent.yml, written by maxpm runner github) go back as "agents": each one's state, and at its
  end the proof and the transcript of its artifact maxpm-agent.

Not possible here, and why: a run that a push or a tag starts carries no MaximizePM id; poll links it to the version
that waits at that stage (same commit). Gitea and Forgejo list no deployments, and some of their versions have no
jobs, artifacts or cancel calls: the read goes on without them, and stop says to stop the run on its page.

The token: a fine-grained token with Actions read and write, Deployments read and Contents read (and Environments
read for the secret names; without it the stage cards show none), saved as a maxpm
secret and mapped to this plugin's secret 'token'. It comes in the request; this program keeps no copy.

Only the standard library, so it runs with the Python that runs MaximizePM ({python} in plugin.json).
"""

import io
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone

RUNS = 10  # the newest runs of a workflow that a poll reads
DONE_KEEP = 300  # finished runs that the state remembers as read
NAMES = {"github": "GitHub", "gitea": "Gitea", "forgejo": "Forgejo"}
LINK = re.compile(r"\bmaxpm run (\d+)\b")  # the run-name of a run that MaximizePM started
OUTCOME = {"success": "success", "failure": "failure", "timed_out": "failure", "startup_failure": "failure",
           "cancelled": "cancel", "skipped": "skip", "neutral": "success", "stale": "cancel",
           "action_required": "skip"}


class Failed(Exception):
    """A call that did not work: main() answers {"error": <its words>} and exits 1. status: the HTTP status, if any."""

    def __init__(self, words, status=None):
        super().__init__(words)
        self.status = status


# ---------------------------------------------------------------- the API

class _Redirect(urllib.request.HTTPRedirectHandler):
    """urllib keeps every header on a redirect, the token too. An artifact download redirects to a storage host
    (GitHub: an Azure blob with its own signed address), which answers 401 to a token that is not its own (#6459):
    the token goes only to the host that it was sent to."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            new.remove_header("Authorization")
        return new


_OPENER = urllib.request.build_opener(_Redirect)


def http(method, url, headers, body):
    """One HTTP call: (status, bytes). Tests pass their own function with the same form (recorded answers)."""
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class Api:
    """The forge of one request: its address, its token and its name."""

    def __init__(self, req, fetch):
        s = req.get("settings") or {}
        self.forge = s.get("forge") or "github"
        self.name = NAMES.get(self.forge, self.forge)
        self.repo = s.get("repo")
        self.ref = s.get("ref") or "main"
        self.token = (req.get("secrets") or {}).get("token")
        self.fetch = fetch
        if not self.repo:
            raise Failed(f"the pipeline names no repository: maxpm pipeline forge <pipeline> {self.forge} --repo owner/name")
        if not self.token:
            raise Failed(f"the pipeline names no secret for its {self.name} token: maxpm pipeline forge <pipeline> "
                         f"{self.forge} --repo {self.repo} --secret <name>")
        if self.forge == "github":
            self.base = "https://api.github.com"
        elif s.get("url"):
            self.base = s["url"].rstrip("/") + "/api/v1"  # Gitea and Forgejo: GitHub's Actions API at /api/v1
        else:
            raise Failed(f"the pipeline names no address for its {self.name}: maxpm pipeline forge <pipeline> "
                         f"{self.forge} --url https://<host> ...")

    def call(self, method, path, body=None, raw=False):
        """The answer as JSON (raw: bytes). A status of 300 or more fails with the forge's own message."""
        if self.forge == "github":
            headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                       "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "maxpm-pipeline"}
        else:
            headers = {"Authorization": f"token {self.token}", "Accept": "application/json",
                       "User-Agent": "maxpm-pipeline"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        url = path if path.startswith("https://") else self.base + path
        status, out = self.fetch(method, url, headers, data)
        if status >= 300:
            try:
                msg = json.loads(out or b"{}").get("message") or ""
            except (ValueError, AttributeError):
                msg = (out or b"")[:200].decode("utf-8", "replace")
            raise Failed(f"{self.name} answered {status} to {method} {path}: {msg}", status)
        if raw:
            return out
        return json.loads(out) if out else {}

    def artifact(self, art):
        """The zip of one artifact, or None with the reason. The forge answers its download with a redirect to a
        storage host; a refusal there says nothing of the token (the artifact list just read with it worked), so it
        fails only this artifact, and its words never read as the forge's refusal of the token (#6459)."""
        try:
            return zipfile.ZipFile(io.BytesIO(self.call("GET", art["archive_download_url"], raw=True))), None
        except Failed as e:
            return None, f"the download of the artifact {art.get('name')} failed with HTTP status {e.status}"
        except zipfile.BadZipFile:
            return None, f"the artifact {art.get('name')} is not a zip file"

    def optional(self, path, default):
        """A read that some Gitea and Forgejo versions do not have (jobs, artifacts): default when they have none."""
        try:
            return self.call("GET", path)
        except Failed:
            if self.forge == "github":
                raise
            return default


# ---------------------------------------------------------------- events

def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def event(subject, predicate, subject_id, content, maxpm, source, at, event_id):
    """One CDEvents event. MaximizePM keeps each event id once, so the same change read again gives the same id."""
    types = {"pipelinerun": "pipelineRun", "taskrun": "taskRun", "service": "service"}
    return {"context": {"version": "0.4.1", "id": event_id, "source": source, "timestamp": at or now(),
                        "type": f"dev.cdevents.{subject}.{predicate}.0.2.0"},
            "subject": {"id": subject_id, "type": types[subject],
                        "content": {k: v for k, v in content.items() if v is not None}},
            "customData": {"maxpm": {k: v for k, v in maxpm.items() if v is not None}}}


FJ_DONE = {"success": "success", "failure": "failure", "cancelled": "cancelled", "skipped": "skipped"}


def norm(run):
    """A run or a job in GitHub's form. Gitea's has it (started_at, completed_at for the times); Forgejo's has title,
    commit_sha, prettyref, one status word (success, failure, running, waiting, ...) and started, stopped."""
    st = run.get("status")
    if st in ("queued", "in_progress", "completed", "waiting", "requested", "pending") and "conclusion" in run:
        status, conclusion = ("queued" if st in ("waiting", "requested", "pending") else st), run.get("conclusion")
    elif st in FJ_DONE:
        status, conclusion = "completed", FJ_DONE[st]
    elif st == "running":
        status, conclusion = "in_progress", None
    else:
        status, conclusion = st if st in ("queued", "in_progress", "completed") else "queued", run.get("conclusion")
    path = (run.get("path") or "").split("@")[0]
    return {**run, "status": status, "conclusion": conclusion,
            "head_sha": run.get("head_sha") or run.get("commit_sha"),
            "head_branch": run.get("head_branch") or run.get("prettyref"),
            "display_title": run.get("display_title") or run.get("title"),
            "run_started_at": run.get("run_started_at") or run.get("started_at") or run.get("started"),
            "updated_at": run.get("updated_at") or run.get("completed_at") or run.get("stopped") or run.get("updated"),
            "created_at": run.get("created_at") or run.get("created"),
            "completed_at": run.get("completed_at") or run.get("stopped"),
            "started_at": run.get("started_at") or run.get("started"),
            "workflow": run.get("workflow_id") if isinstance(run.get("workflow_id"), str) else path.rsplit("/", 1)[-1]}


def runs_of(api, workflow):
    """The newest runs of a workflow, oldest first (so a newer version is handed on last). Pull-request runs are
    skipped: a pipeline tracks the repository's own commits (push, tag, workflow_dispatch), not unmerged or fork PRs
    whose runs may also be held for approval (conclusion 'action_required', #6473). Gitea and Forgejo list the runs
    of the whole repository: keep those of this workflow."""
    if api.forge == "github":
        runs = api.call(
            "GET", f"/repos/{api.repo}/actions/workflows/{workflow}/runs?per_page={RUNS}&exclude_pull_requests=true"
        ).get("workflow_runs", [])
    else:
        got = api.call("GET", f"/repos/{api.repo}/actions/runs?limit=50")
        listed = got.get("workflow_runs") or got.get("runs") or [] if isinstance(got, dict) else got
        runs = [r for r in (norm(r) for r in listed) if r["workflow"] == workflow]
    runs = [r for r in runs if not str(r.get("event") or "").startswith("pull_request")][:RUNS]
    return list(reversed(runs))


def waiting_for(stage, run, open_runs):
    """MaximizePM's run that waits at this stage for the commit of a run that names none (a push or a tag started
    it): that run is the forge's run of it."""
    if LINK.search(run.get("display_title") or "") or not run.get("head_sha"):
        return None
    if any(r["key"] == f"gh-run-{run['id']}" for r in open_runs):
        return None  # linked already
    for r in sorted(open_runs, key=lambda r: r["id"]):
        if (r["stage"] == stage and r["state"] == "queued" and not (r["key"] or "").startswith("gh-run-")
                and r["version"] and run["head_sha"].startswith(r["version"])):
            return r["id"]
    return None


def run_events(api, stage, run, waiting):
    """The pipelinerun events of one workflow run: queued, started, finished."""
    key = f"gh-run-{run['id']}"
    m = LINK.search(run.get("display_title") or run.get("name") or "")
    maxpm = {"stage": stage, "version": run.get("head_sha"), "ref": run.get("head_branch"),
             "title": (run.get("display_title") or "")[:200] or None, "run": int(m.group(1)) if m else waiting}
    base = {"pipelineName": stage, "url": run.get("html_url")}
    ids = f"gh-run-{run['id']}-{run.get('run_attempt') or 1}"
    out = [event("pipelinerun", "queued", key, base, maxpm, api.forge, run.get("created_at"), ids + "-queued")]
    if run.get("status") in ("in_progress", "completed") and run.get("run_started_at"):
        out.append(event("pipelinerun", "started", key, base, maxpm, api.forge, run["run_started_at"], ids + "-started"))
    if run.get("status") == "completed":
        done = {**base, "outcome": OUTCOME.get(run.get("conclusion"), "failure"),
                "errors": None if run.get("conclusion") == "success" else run.get("conclusion")}
        out.append(event("pipelinerun", "finished", key, done, maxpm, api.forge, run.get("updated_at"), ids + "-finished"))
    return out


def job_events(api, run):
    """The jobs of a run as taskrun events (steps of MaximizePM's run, found by the run's key)."""
    jobs = api.optional(f"/repos/{api.repo}/actions/runs/{run['id']}/jobs?per_page=100", {}).get("jobs") or []
    out = []
    for j in (jobs if api.forge == "github" else [norm(j) for j in jobs]):
        key = f"gh-job-{j['id']}"
        base = {"taskName": j.get("name") or key, "pipelineRun": {"id": f"gh-run-{run['id']}", "source": api.forge},
                "url": j.get("html_url")}
        if j.get("started_at"):
            out.append(event("taskrun", "started", key, base, {}, api.forge, j["started_at"], key + "-started"))
        if j.get("status") == "completed":
            out.append(event("taskrun", "finished", key, {**base, "outcome": OUTCOME.get(j.get("conclusion"), "failure")},
                             {}, api.forge, j.get("completed_at"), key + "-finished"))
    return out


def junit_reports(api, stage, run):
    """The JUnit files of a finished run: the XML files in its artifacts named junit* (or the stage's --junit name).
    MaximizePM reads them (its parser refuses what is not JUnit)."""
    arts = api.optional(f"/repos/{api.repo}/actions/runs/{run['id']}/artifacts?per_page=100", {}).get("artifacts") or []
    want = stage.get("junit")
    out = []
    for a in arts:
        if a.get("expired") or not (a["name"] == want if want else a["name"].lower().startswith("junit")):
            continue
        z, _why = api.artifact(a)
        if z is None:
            continue  # this report is skipped; the read of the run goes on (#6459)
        with z:
            for name in z.namelist():
                if name.lower().endswith(".xml"):
                    out.append({"run_key": f"gh-run-{run['id']}", "xml": z.read(name).decode("utf-8", "replace")})
    return out


def deploy_events(api, envs, seen):
    """GitHub's deployments of each environment with a success status (Gitea and Forgejo have no deployments API:
    a deploy stage that passes records its version in MaximizePM)."""
    out = []
    if api.forge != "github":
        return out
    for env in envs:
        deps = api.call("GET", f"/repos/{api.repo}/deployments?environment={env['name']}&per_page=5")
        for d in deps if isinstance(deps, list) else []:
            if d["id"] in seen:
                continue
            sts = api.call("GET", f"/repos/{api.repo}/deployments/{d['id']}/statuses?per_page=1")
            if not sts or sts[0].get("state") != "success":
                continue
            seen.add(d["id"])
            out.append(event("service", "deployed", f"{env['name']}", {"environment": {"id": env["name"]}},
                             {"version": d.get("sha")}, api.forge, sts[0].get("created_at"),
                             f"gh-dep-{d['id']}-{sts[0]['id']}"))
    return out


SECRETS_EVERY = 3600  # seconds between two reads of the environments' secret names


def secret_names(api, stages, state):
    """The names of the secrets that each GitHub environment of a deploy stage keeps (never their values: the API
    gives none), once an hour (the state's 'secrets_at'). MaximizePM shows them on the stage card as keys that GitHub
    keeps. A token that may not list them (fine-grained: Environments read) gives nothing, and the read goes on."""
    if api.forge != "github":
        return None
    at = state.get("secrets_at") or 0
    if datetime.now(timezone.utc).timestamp() - at < SECRETS_EVERY:
        return None
    out = []
    for stage in stages:
        if not stage.get("env"):
            continue
        try:
            got = api.call("GET", f"/repos/{api.repo}/environments/{stage['env']}/secrets?per_page=100")
        except Failed:
            continue
        out.append({"stage": stage["name"], "kept_in": f"GitHub environment {stage['env']}",
                    "names": [s["name"] for s in got.get("secrets") or [] if s.get("name")]})
    state["secrets_at"] = int(datetime.now(timezone.utc).timestamp())
    return out


# ---------------------------------------------------------------- the calls

def poll(req, api):
    """Read what changed. The state keeps the finished runs read already (a run, an attempt) and the deployments
    reported, so a poll does not read them again."""
    state = req.get("state") or {}
    done, deps = list(state.get("done") or []), set(state.get("deployments") or [])
    events, reports, runs = [], [], 0
    open_runs = req.get("runs") or []
    for stage in (req.get("pipeline") or {}).get("stages") or []:
        if not stage.get("work"):
            continue  # this stage runs its own command on MaximizePM's computer
        for run in runs_of(api, stage["work"]):
            runs += 1
            mark = f"{run['id']}-{run.get('run_attempt') or 1}"
            if mark in done:
                continue  # read to its end before
            events += run_events(api, stage["name"], run, waiting_for(stage["name"], run, open_runs))
            events += job_events(api, run)
            if run.get("status") == "completed":
                reports += junit_reports(api, stage, run)
                done.append(mark)
    events += deploy_events(api, (req.get("pipeline") or {}).get("envs") or [], deps)
    out_state = {"done": done[-DONE_KEEP:], "deployments": sorted(deps)[-DONE_KEEP:],
                 "secrets_at": state.get("secrets_at") or 0}
    names = secret_names(api, (req.get("pipeline") or {}).get("stages") or [], out_state)
    answer = {"events": events, "reports": reports, "counts": {"runs": runs}, "state": out_state}
    if names is not None:
        answer["secrets"] = names
    if req.get("agents"):
        answer["agents"] = agent_runs(api, req)
    return answer


# ---------------------------------------------------------------- the agent job (#6152)

AGENT_LINK = re.compile(r"\bmaxpm agent (\d+)\b")  # the run-name of the job maxpm-agent: the failed run's id
AGENT_ART = "maxpm-agent"
TRANSCRIPT_KEEP = 400  # the last lines of the transcript that go back
AGENT_STATE = {"success": "ended", "cancelled": "stopped", "skipped": "stopped"}


def agent_runs(api, req):
    """The job maxpm-agent (.github/workflows/agent.yml, which maxpm runner github writes): GitHub starts it when a
    stage's workflow fails, and names its run 'maxpm agent <the failed run's id>'. For each run that MaximizePM waits
    for ({"run", "key": "gh-run-<failed id>"}): its state, and at its end the proof and the transcript of its
    artifact maxpm-agent."""
    wf = (req.get("settings") or {}).get("agent") or "agent.yml"
    try:
        runs = api.call("GET", f"/repos/{api.repo}/actions/workflows/{wf}/runs?per_page=30").get("workflow_runs", [])
    except Failed as e:
        if " 404 " not in str(e):
            raise
        return [{"run": a["run"], "note": f"{api.name} has no workflow {wf} in {api.repo} yet: commit the file that "
                                          f"maxpm runner github wrote to the default branch"} for a in req["agents"]]
    newest = {}
    for r in runs:  # newest first: the first run that names a failed run is its newest attempt
        m = AGENT_LINK.search(r.get("display_title") or r.get("name") or "")
        if m:
            newest.setdefault(m.group(1), r)
    out = []
    for a in req["agents"]:
        r = newest.get(str(a.get("key") or "").rsplit("-", 1)[-1])
        if r is None:
            continue
        d = {"run": a["run"], "job": f"gh-run-{r['id']}", "url": r.get("html_url"),
             "started_at": r.get("run_started_at") or r.get("created_at")}
        if r.get("status") != "completed":
            d["state"] = "running" if r.get("status") == "in_progress" else "queued"
        else:
            d["state"] = AGENT_STATE.get(r.get("conclusion"), "failed")
            d["ended_at"] = r.get("updated_at")
            d.update(agent_artifact(api, r))
        out.append(d)
    return out


def agent_artifact(api, run):
    """proof.json and the end of transcript.jsonl from the artifact maxpm-agent of a finished job."""
    arts = api.call("GET", f"/repos/{api.repo}/actions/runs/{run['id']}/artifacts?per_page=100").get("artifacts") or []
    art = next((a for a in arts if a.get("name") == AGENT_ART and not a.get("expired")), None)
    if art is None:
        return {"note": f"the job left no artifact {AGENT_ART}"}
    out = {}
    z, why = api.artifact(art)
    if z is None:
        return {"note": why}
    with z:
        names = {n.rsplit("/", 1)[-1]: n for n in z.namelist()}
        if "proof.json" in names:
            try:
                proof = json.loads(z.read(names["proof.json"]).decode("utf-8", "replace"))
                out["proof"] = proof if isinstance(proof, dict) else {}
            except ValueError:
                out["note"] = "its proof.json is not JSON"
        if "transcript.jsonl" in names:
            lines = z.read(names["transcript.jsonl"]).decode("utf-8", "replace").splitlines()
            out["transcript"] = "\n".join(lines[-TRANSCRIPT_KEEP:]) + "\n"
    return out


def act(req, api):
    """start, rerun, rollback: workflow_dispatch with the run's id and commit (the older commit of a rerun or a roll
    back). stop: cancel the forge's run."""
    run = req["run"]
    if req["call"] == "stop":
        m = re.match(r"gh-run-(\d+)$", run.get("key") or "")
        if not m:
            raise Failed(f"run #{run['id']} has no {api.name} run yet; it shows within a minute of its start")
        try:
            api.call("POST", f"/repos/{api.repo}/actions/runs/{m.group(1)}/cancel")
        except Failed as e:
            if api.forge == "github" or (" 404 " not in str(e) and " 405 " not in str(e)):
                raise
            raise Failed(f"this {api.name} cannot stop a run by its API; stop run {m.group(1)} on its Actions page")
        return {"note": f"asked {api.name} to stop run {m.group(1)}"}
    api.call("POST", f"/repos/{api.repo}/actions/workflows/{run['work']}/dispatches",
             {"ref": api.ref, "inputs": {"maxpm_run": str(run["id"]), "maxpm_rev": run.get("version") or ""}})
    return {"note": f"started on {api.name} ({run['work']}); its run shows within a minute"}


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
    raise Failed(f"github does not do {req['call']}")


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
