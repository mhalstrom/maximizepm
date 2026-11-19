"""shipyard: a MaximizePM pipeline plugin for Shipyard CI (maxpm guide pipelines, 'Write a plugin').

Shipyard runs jobs (build, deploy, ...); one run of a job is a deploy. Each MaximizePM stage names its Shipyard job
with --workflow. A deploy is one stage run; its key is the deploy's id. Its tasks are the run's steps, and the JUnit
results of a finished deploy are its tests.

A start sends the tag 'maxpm-run-<id>' with the new deploy, so the next poll links the deploy to the MaximizePM stage
run that asked for it. Shipyard has no roll back: a person starts a deploy of an older commit in Shipyard.

handle(request, fetch) does the work; fetch(method, url, headers, body) -> (status, bytes) is the HTTP call, so a
test passes recorded answers instead of the network.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TAG = "maxpm-run-{}"
TAG_RE = re.compile(r"^maxpm-run-(\d+)$")
PREDICATE = {"waiting": "queued", "running": "started", "ok": "finished", "failed": "finished",
             "stopped": "finished"}
OUTCOME = {"ok": "success", "failed": "failure", "stopped": "cancel"}


class ShipyardError(Exception):
    pass


class Shipyard:
    """The Shipyard API of one project, over the fetch function."""

    def __init__(self, req, fetch):
        settings, secrets = req.get("settings") or {}, req.get("secrets") or {}
        for name in ("url", "project"):
            if not settings.get(name):
                raise ShipyardError(f"set the Shipyard {name}: maxpm pipeline forge <pipeline> shipyard --set {name}=...")
        if not secrets.get("key"):
            raise ShipyardError("no Shipyard key: map it with --secret key=<maxpm secret>")
        self.base = f"{settings['url'].rstrip('/')}/api/projects/{settings['project']}"
        self.key, self.fetch = secrets["key"], fetch

    def call(self, method, path, body=None, missing_ok=False):
        headers = {"X-Shipyard-Key": self.key, "Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        status, raw = self.fetch(method, self.base + path, headers, data)
        if status == 404 and missing_ok:
            return None
        if status >= 400:
            try:
                problem = json.loads(raw).get("problem") or raw.decode(errors="replace")
            except ValueError:
                problem = raw.decode(errors="replace")[:200]
            if status == 401:
                raise ShipyardError(f"Shipyard refused the key (401): {problem}. Save a new key with maxpm secret put.")
            raise ShipyardError(f"Shipyard answered {status} to {method} {path}: {problem}")
        return raw


def describe(req):
    # The same name, version, level and actions as plugin.json: MaximizePM compares them.
    with open(os.path.join(HERE, "plugin.json"), encoding="utf-8") as f:
        m = json.load(f)
    return {k: m[k] for k in ("name", "version", "level", "actions")}


def cd(kind, predicate, event_id, subject_id, timestamp, content, maxpm):
    """One CDEvents event. The id is the same each time the same change is read: MaximizePM keeps it once."""
    return {"context": {"version": "0.4.1", "id": event_id, "source": "shipyard",
                        "type": f"dev.cdevents.{kind}.{predicate}.0.2.0", "timestamp": timestamp},
            "subject": {"id": subject_id, "type": "pipelineRun" if kind == "pipelinerun" else "taskRun",
                        "content": content},
            "customData": {"maxpm": maxpm}}


def link(deploy, stage, runs):
    """The id of the MaximizePM stage run this deploy belongs to, or None."""
    key = str(deploy["id"])
    for r in runs:  # a run whose start or rerun answered this key
        if str(r.get("key") or "").split("~")[0] == key:
            return r["id"]
    m = TAG_RE.match(deploy.get("tag") or "")
    if m:  # a deploy that a start asked for
        return int(m.group(1))
    # A deploy that a push started: the run that waits at that stage for this commit (state "queued"; the agent that
    # wrote this plugin guessed "waiting", because the guide did not list the states, #6165).
    for r in runs:
        version = str(r.get("version") or "")
        if (r.get("stage") == stage and not r.get("key") and r.get("state") == "queued" and version
                and (deploy.get("commit") or "").startswith(version)):
            return r["id"]
    return None


def deploy_events(deploy, stage, run_id):
    key = str(deploy["id"])
    maxpm = {"stage": stage, "version": deploy.get("commit")}
    if run_id is not None:
        maxpm["run"] = run_id
    content = {"pipelineName": stage}
    if deploy.get("link"):
        content["url"] = deploy["link"]
    state = deploy.get("state")
    if state not in PREDICATE:
        raise ShipyardError(f"deploy {key}: unknown state {state!r}")
    events = []
    if state == "waiting":
        events.append(cd("pipelinerun", "queued", f"deploy-{key}-queued", key, deploy.get("started"), content, maxpm))
    if deploy.get("started") or state != "waiting":
        events.append(cd("pipelinerun", "started", f"deploy-{key}-started", key, deploy.get("started"),
                         content, maxpm))
    if PREDICATE[state] == "finished":
        events.append(cd("pipelinerun", "finished", f"deploy-{key}-finished", key, deploy.get("ended"),
                         dict(content, outcome=OUTCOME[state]), maxpm))
    # Its tasks are the run's steps.
    step = {"stage": stage, "version": deploy.get("commit"), "run_key": key}
    for task in deploy.get("tasks") or []:
        name, tid = task["name"], f"{key}/{task['name']}"
        if task.get("started"):
            events.append(cd("taskrun", "started", f"deploy-{key}-task-{name}-started", tid, task["started"],
                             {"taskName": name, "pipelineRun": {"id": key}}, step))
        if task.get("state") in OUTCOME:
            events.append(cd("taskrun", "finished", f"deploy-{key}-task-{name}-finished", tid, task.get("ended"),
                             {"taskName": name, "pipelineRun": {"id": key}, "outcome": OUTCOME[task["state"]]},
                             step))
    return events


def poll(req, api):
    # The deploys that changed after the cursor of the last poll. The cursor goes back in 'state'.
    cursor = (req.get("state") or {}).get("cursor", 0)
    page = json.loads(api.call("GET", f"/deploys?after={cursor}"))
    stages = {s.get("work"): s["name"] for s in (req.get("pipeline") or {}).get("stages", []) if s.get("work")}
    runs = req.get("runs") or []
    events, reports = [], []
    for deploy in page.get("deploys", []):
        stage = stages.get(deploy.get("job"))
        if stage is None:
            continue  # a job that no stage names
        events += deploy_events(deploy, stage, link(deploy, stage, runs))
        if deploy.get("state") in ("ok", "failed"):
            # A finished deploy's tests. The cursor lists it once after it ends; MaximizePM reads a report once.
            xml = api.call("GET", f"/deploys/{deploy['id']}/results", missing_ok=True)
            if xml:
                reports.append({"run_key": str(deploy["id"]), "xml": xml.decode("utf-8", errors="replace")})
    return {"events": events, "reports": reports, "state": {"cursor": max(page.get("cursor", cursor), cursor)}}


def start(req, api):
    run = req["run"]
    if not run.get("work"):
        raise ShipyardError(f"stage {run.get('stage')} names no Shipyard job: give it --workflow <job>")
    new = json.loads(api.call("POST", "/deploys", {"job": run["work"], "commit": run["version"],
                                                   "tag": TAG.format(run["id"])}))
    return {"key": str(new["id"]), "note": f"Shipyard deploy {new['id']} of {run['work']}"}


def deploy_key(req):
    key = str(req["run"].get("key") or "").split("~")[0]  # '<key>~<id>': an older record of the same deploy
    if not key:
        raise ShipyardError("this run has no Shipyard deploy yet")
    return key


def stop(req, api):
    key = deploy_key(req)
    api.call("POST", f"/deploys/{key}/stop", {})
    return {"note": f"asked Shipyard to stop deploy {key}"}


def rerun(req, api):
    # 'again' runs the same job and commit as the deploy the run had. A run with no deploy yet starts a new one.
    if not req["run"].get("key"):
        return start(req, api)
    key = deploy_key(req)
    new = json.loads(api.call("POST", f"/deploys/{key}/again", {}))
    out = {"key": str(new["id"]), "note": f"Shipyard deploy {new['id']}, again of {key}"}
    if req.get("tests"):
        out["note"] += "; Shipyard runs every test again, not only the failed ones"
    return out


def handle(req, fetch):
    call = req.get("call")
    if call == "describe":
        return describe(req)
    actions = {"poll": poll, "start": start, "stop": stop, "rerun": rerun}
    if call not in actions:
        return {"error": f"shipyard does not do {call}" + (": Shipyard has no roll back; start a deploy of the older"
                                                           " commit in Shipyard" if call == "rollback" else "")}
    try:
        return actions[call](req, Shipyard(req, fetch))
    except ShipyardError as e:
        return {"error": str(e)}


def urllib_fetch(method, url, headers, body):
    """The real HTTP call: (status, bytes). Its timeout stays under the manifest's 30 seconds."""
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main():
    try:
        out = handle(json.load(sys.stdin), urllib_fetch)
    except Exception as e:  # the network, a bad answer: MaximizePM shows the message as the pipeline's error
        out = {"error": f"{type(e).__name__}: {e}"}
    json.dump(out, sys.stdout)
    sys.exit(1 if "error" in out else 0)


if __name__ == "__main__":
    main()
