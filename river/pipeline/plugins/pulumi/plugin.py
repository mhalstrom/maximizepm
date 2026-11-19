"""pulumi: the MaximizePM pipeline plugin for Pulumi Delivery (public preview).

A worked example of the plugin contract (maxpm guide pipelines, 'Write a plugin') for a system whose API has no list
of runs and no webhooks: the plugin reads runs by number and keeps its place in its state.

A Delivery pipeline is a pipeline stack (setting repo: org/project/stack), and each MaximizePM stage names a Delivery
stage (maxpm pipeline stage ... --workflow <Delivery stage>). One Delivery run takes one release through every stage,
so one MaximizePM stage run is one stage of one Delivery run: its key is pd-<run number>-<stage>.

What Pulumi documents (routes under /api/preview, read 2026-11-18, which can change): start a run (POST
.../delivery/dispatch), read one run (GET .../delivery/runs/<n>), continue a gate (POST .../gates/<id>/continue),
approve an approval condition (POST /api/change-requests/<org>/<id>/approve), retry and cancel a run. The fields read
here are the ones Pulumi's own gate hook reads: summary.number, summary.status.status, and stages[] with name,
status.status and gate (id, status, conditions[] with name, kind, state, changeRequestId).

Not documented, so not done, and what follows:
- no list of runs: poll reads runs by number from its state's 'next' (at first the setting ref) up to the first
  number that has no run, at most READ_MAX; 'next' then moves to the oldest run that has not ended;
- no commit and no times of a run: its version is the one MaximizePM sent (maxpm pipeline start), else
  'release <n>' when the run names its release, else 'run <n>'; a stage shows no steps;
- no roll back of a stage (it is in the stage's Actions menu on Pulumi's page): this plugin lists no rollback, so
  MaximizePM refuses it and says where to do it;
- Delivery cannot run a commit named from outside: start begins a run at the head of its source.

The yes at a MaximizePM transition continues the Delivery gate: each pending manual condition, and an approval
condition only after a person's yes (the run's approved_by_person). When the gate is not open yet, poll continues it
when the run comes to it. The token: a Pulumi access token, mapped to this plugin's secret 'token'.
"""

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

READ_MAX = 20  # Delivery runs that one poll reads
RETRY_GRACE = 600  # seconds after a retry when the old end that Pulumi still shows is not the retry's end
FINAL = ("succeeded", "failed", "canceled", "cancelled", "superseded")
STATE = {"waiting": "queued", "queued": "queued", "running": "running", "in_progress": "running",
         "succeeded": "passed", "failed": "failed", "canceled": "cancelled", "cancelled": "cancelled",
         "superseded": "cancelled", "skipped": "skipped"}
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
        self.repo = s.get("repo") or ""
        self.fetch = fetch
        if not self.token:
            raise Failed("the pipeline names no secret for its Pulumi token: maxpm pipeline forge <pipeline> pulumi "
                         "--repo org/project/stack --secret <name>")
        parts = self.repo.split("/")
        if len(parts) != 3:
            raise Failed("the pipeline stack is org/project/stack: maxpm pipeline forge <pipeline> pulumi --repo ...")
        self.org = parts[0]
        self.root = (s.get("url") or "https://api.pulumi.com").rstrip("/")
        org, project, stack = (urllib.parse.quote(x, safe="") for x in parts)
        self.base = f"{self.root}/api/preview/stacks/{org}/{project}/{stack}/delivery"

    def call(self, method, path, body=None, missing=False, ok=()):
        """The answer as JSON. missing: None for a 404; ok: statuses that are no failure (409: the gate is not open,
        someone continued it). Any other status of 300 or more fails with Pulumi's own message."""
        headers = {"Authorization": f"token {self.token}", "Accept": "application/json", "User-Agent": "maxpm-pipeline"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        url = self.root + path if path.startswith("/api/") else self.base + path
        status, out = self.fetch(method, url, headers, data)
        if (missing and status == 404) or status in ok:
            return None
        if status >= 300:
            try:
                msg = json.loads(out or b"{}").get("message") or ""
            except (ValueError, AttributeError):
                msg = (out or b"")[:200].decode("utf-8", "replace")
            raise Failed(f"Pulumi answered {status} to {method} {path}: {msg}")
        return json.loads(out) if out else {}


def word(x):
    """A status of Pulumi's form {"status": "running"} (or a plain word)."""
    return x.get("status") if isinstance(x, dict) else x


def event(predicate, key, content, maxpm, event_id):
    return {"context": {"version": "0.4.1", "id": event_id, "source": "pulumi",
                        "type": f"dev.cdevents.pipelinerun.{predicate}.0.2.0",
                        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
            "subject": {"id": key, "type": "pipelineRun", "content": {k: v for k, v in content.items() if v is not None}},
            "customData": {"maxpm": {k: v for k, v in maxpm.items() if v is not None}}}


def number_of(key):
    m = re.match(r"pd-(\d+)-", key or "")
    return int(m[1]) if m else None


class Poll:
    """One poll: the runs MaximizePM knows (the request's runs), and what the poll gives back."""

    def __init__(self, req, api):
        self.api = api
        self.runs = req.get("runs") or []
        self.by_key = {r["key"]: r for r in self.runs if r.get("key")}
        stages = [s for s in (req.get("pipeline") or {}).get("stages") or [] if s.get("work")]
        self.stages = {s["work"]: s for s in stages}
        self.first = min(stages, key=lambda s: s["pos"])["work"] if stages else None
        self.events, self.notes = [], []

    def version(self, n, summary):
        """The version of Delivery run n: the one MaximizePM sent, else its release, else 'run <n>'."""
        mine = sorted((r for r in self.runs if number_of(r.get("key")) == n and r.get("version")), key=lambda r: r["id"])
        if mine:
            return mine[0]["version"]
        rel = summary.get("releaseNumber") or summary.get("release")
        rel = rel.get("number") if isinstance(rel, dict) else rel
        return f"release {rel}" if isinstance(rel, int) else f"run {n}"

    def waiting(self, stage, version, first):
        """MaximizePM's run that waits for this Delivery run at a stage: one queued for its version, or, on the first
        stage, the oldest one that start began before the Delivery run had a number."""
        free = sorted((r for r in self.runs if r["stage"] == stage["name"] and r["state"] == "queued"
                       and not (r.get("key") or "").startswith("pd-")), key=lambda r: r["id"])
        for r in free:
            if r.get("version") == version:
                return r
        if first:
            return next((r for r in free if r.get("runner") == "forge:pulumi"), None)
        return None

    def stage_events(self, stage, n, st, run_status, version, row, waiting):
        key = f"pd-{n}-{stage['work']}"
        status, gate = word(st.get("status")), st.get("gate") or {}
        if status in (None, "pending") and gate.get("status") != "open" and row is None:
            return  # Delivery has not reached this stage: no card for it yet
        state = "queued" if gate.get("status") == "open" else STATE.get(status, "queued")
        ended = None
        if state in ("queued", "running") and run_status in FINAL:
            state, ended = "cancelled", f"the Delivery run ended ({run_status}) before stage {st.get('name')} did"
        if row and row.get("purpose") and row.get("runner") == "forge:pulumi" and not row.get("started_at") \
                and state in OUTCOME and row.get("approved_at") and seconds_since(row["approved_at"]) < RETRY_GRACE:
            state = "queued"  # the end that Pulumi still shows is the one before the retry
        again = sum(1 for r in self.runs if (r.get("key") or "").startswith(key + "~"))
        ids = f"{key}.{again}"
        maxpm = {"stage": stage["name"], "version": version, "run": None if row else (waiting or {}).get("id")}
        base = {"pipelineName": stage["name"]}
        self.events.append(event("queued", key, base, maxpm, ids + "-queued"))
        if state in ("running", "passed", "failed"):
            self.events.append(event("started", key, base, maxpm, ids + "-started"))
        if state in OUTCOME:
            errors = ended or (f"Pulumi Delivery: stage {st.get('name')} {status}" if state == "failed" else None)
            self.events.append(event("finished", key, {**base, "outcome": OUTCOME[state], "errors": errors}, maxpm,
                                     ids + "-finished"))

    def read(self, n):
        """Read Delivery run n, and continue a gate that a yes in MaximizePM opened before Delivery came to it.
        Returns the run's status ('' when it names none), or None when Pulumi has no run n."""
        run = self.api.call("GET", f"/runs/{n}", missing=True)
        if run is None:
            return None
        summary = run.get("summary") or {}
        run_status = word(summary.get("status")) or ""
        version = self.version(n, summary)
        for st in run.get("stages") or []:
            stage = self.stages.get(st.get("name"))
            if stage is None:
                continue
            row = self.by_key.get(f"pd-{n}-{stage['work']}")
            waiting = None if row else self.waiting(stage, version, stage["work"] == self.first)
            self.stage_events(stage, n, st, run_status, version, row, waiting)
            mine = row or waiting
            gate = st.get("gate") or {}
            if mine and gate.get("status") == "open" and mine.get("runner") == "forge:pulumi" and mine["state"] == "queued":
                left = continue_gate(self.api, n, gate, mine.get("approved_by_person"))
                self.notes.append({"run": mine["id"], "note": gate_note(n, left)})
        return run_status


def seconds_since(at):
    t = datetime.strptime(at[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds()


def continue_gate(api, n, gate, person):
    """Continue the open gate of Delivery run n: each pending manual condition, and, after a person's yes, each
    pending approval through its change request. Returns the conditions it left to a person on Pulumi's page."""
    conds = [c for c in gate.get("conditions") or [] if c.get("state") == "pending"]
    path = f"/runs/{n}/gates/{urllib.parse.quote(str(gate['id']), safe='')}/continue"
    if not conds:
        api.call("POST", path, {}, ok=(409,))
        return []
    left = []
    for c in conds:
        if c.get("kind") == "manual":
            api.call("POST", path, {"condition": c.get("name")}, ok=(409,))
        elif c.get("kind") == "approval" and c.get("changeRequestId") and person:
            api.call("POST", f"/api/change-requests/{urllib.parse.quote(api.org, safe='')}/{c['changeRequestId']}/approve", {})
        else:
            left.append(f"{c.get('kind')} {c.get('name')}")
    return left


def gate_note(n, left):
    if not left:
        return f"continued the gate of Delivery run {n}"
    return f"continued what it may of the gate of Delivery run {n}; on Pulumi's page a person decides: " + ", ".join(left)


def poll(req, api):
    """First the runs below 'next' that a MaximizePM run still waits on (a retry opens an ended run again), then the
    runs from 'next' on, up to the first number that has no run yet."""
    p = Poll(req, api)
    state = req.get("state") or {}
    start = int(state.get("next") or (req.get("settings") or {}).get("ref") or 1)
    count = 0
    for n in sorted({number_of(r.get("key")) for r in p.runs if r["state"] in ("queued", "running")} - {None}):
        if n < start and p.read(n) is not None:
            count += 1
    n, oldest_open = start, None
    while count < READ_MAX:
        status = p.read(n)
        if status is None:
            break
        count += 1
        if oldest_open is None and status not in FINAL:
            oldest_open = n
        n += 1
    return {"events": p.events, "notes": p.notes, "counts": {"runs": count}, "state": {**state, "next": oldest_open or n}}


def number_for(run, runs):
    """The Delivery run that carries a MaximizePM run: its own key, else a run of the same version."""
    n = number_of(run.get("key"))
    if n is not None:
        return n
    same = sorted((r for r in runs if r.get("version") == run.get("version") and number_of(r.get("key"))),
                  key=lambda r: -r["id"])
    return number_of(same[0]["key"]) if same and run.get("version") else None


def act(req, api):
    """start: continue the stage's gate in its Delivery run (now, or when the run comes to it), or start a Delivery
    run for a version no run carries yet. rerun: retry the Delivery run. stop: cancel the whole Delivery run."""
    run, runs, state = req["run"], req.get("runs") or [], req.get("state") or {}
    n = number_for(run, runs)
    if req["call"] == "stop":
        if n is None:
            raise Failed(f"no Pulumi Delivery run carries run #{run['id']} yet")
        api.call("POST", f"/runs/{n}/cancel", {})
        return {"note": f"cancelled Delivery run {n}"}
    if n is None:
        if req["call"] == "rerun":
            raise Failed(f"no Pulumi Delivery run carries {run.get('version')}")
        out = api.call("POST", "/dispatch", {}) or {}
        n = out.get("runNumber") if isinstance(out.get("runNumber"), int) else None
        note = (f"started Delivery run {n}" if n else "started a Delivery run") + \
            " at the head of its source (Delivery cannot run a commit named from outside); it shows within a minute"
    elif req["call"] == "rerun":
        api.call("POST", f"/runs/{n}/retry", {})
        note = f"retried Delivery run {n}: it runs its failed members again with the same commit"
    else:
        st = next((x for x in (api.call("GET", f"/runs/{n}") or {}).get("stages") or [] if x.get("name") == run["work"]),
                  None)
        gate = (st or {}).get("gate") or {}
        if gate.get("status") == "open":
            note = gate_note(n, continue_gate(api, n, gate, run.get("approved_by_person")))
        else:
            note = f"the yes is given: MaximizePM continues the gate when Delivery run {n} comes to it"
    out = {"note": note}
    if n:
        out["key"] = f"pd-{n}-{run['work']}"
        if not any(number_of(r.get("key")) for r in runs):
            out["state"] = {**state, "next": n}  # the first Delivery run of this pipeline: polls start there
    return out


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
    if req["call"] in ("start", "rerun", "stop"):
        return act(req, api)
    raise Failed(f"pulumi does not do {req['call']}: a roll back is in the stage's Actions menu on Pulumi's page")


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
