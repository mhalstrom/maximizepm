"""jsonl: the example plugin of MaximizePM's pipeline plugin contract (maxpm guide pipelines, 'Write a plugin').

The release system it connects to is a file. Another program (a cron job, a deploy script) appends one JSON line
each time a run changes:

    {"run": "nightly-17", "stage": "build", "version": "3f2a9c1", "state": "running", "at": "2026-11-18T10:00:00Z"}
    {"run": "nightly-17", "stage": "build", "version": "3f2a9c1", "state": "passed", "at": "2026-11-18T10:04:00Z"}

state is queued, running, passed, failed or cancelled; "url" and "maxpm_run" (the id of the MaximizePM stage run
that a start request asked for) are optional.

MaximizePM runs this program once for each call. It writes one JSON request on stdin and reads one JSON answer on
stdout. The program imports nothing of MaximizePM's: copy it, change the two places marked 'your system', and you
have a plugin for another system.
"""

import json
import sys


def describe(req):
    # The same name, version, level and actions as plugin.json: MaximizePM compares them.
    with open("plugin.json", encoding="utf-8") as f:
        m = json.load(f)
    return {k: m[k] for k in ("name", "version", "level", "actions")}


def event(predicate, line, n):
    """One CDEvents pipelinerun event for a line. The ids must be the same each time the same line is read:
    MaximizePM keeps an event once, so a line read twice changes nothing."""
    content = {"pipelineName": line["stage"]}
    if line.get("url"):
        content["url"] = line["url"]
    if predicate == "finished":
        content["outcome"] = {"passed": "success", "failed": "failure", "cancelled": "cancel"}[line["state"]]
        if line.get("errors"):
            content["errors"] = line["errors"]
    maxpm = {"stage": line["stage"], "version": line.get("version")}
    if line.get("maxpm_run") is not None:
        maxpm["run"] = int(line["maxpm_run"])  # links the run to the stage run that MaximizePM asked for
    return {"context": {"version": "0.4.1", "id": f"line-{n}", "source": "jsonl",
                        "type": f"dev.cdevents.pipelinerun.{predicate}.0.2.0", "timestamp": line.get("at")},
            "subject": {"id": str(line["run"]), "type": "pipelineRun", "content": content},
            "customData": {"maxpm": maxpm}}


PREDICATE = {"queued": "queued", "running": "started", "passed": "finished", "failed": "finished",
             "cancelled": "finished"}


def poll(req):
    # Your system: read what changed since the last poll. Here: the lines after the last one read. The state that
    # the answer returns comes back in the next request, so the plugin keeps no file of its own.
    done = (req.get("state") or {}).get("lines", 0)
    events, n = [], 0
    try:
        with open(req["settings"]["file"], encoding="utf-8") as f:
            for n, text in enumerate(f, 1):
                if n <= done or not text.strip():
                    continue
                line = json.loads(text)
                if line.get("state") not in PREDICATE:
                    raise ValueError(f"line {n}: state is one of {', '.join(PREDICATE)}")
                events.append(event(PREDICATE[line["state"]], line, n))
    except FileNotFoundError:
        n = done  # nothing written yet
    return {"events": events, "state": {"lines": max(n, done)}}


def request(req, what):
    # Your system: ask it to start or stop a run. Here: append a request line that the other program reads.
    path = req["settings"].get("requests")
    if not path:
        return {"error": f"set where {what} requests go: maxpm pipeline forge <pipeline> jsonl --set requests=<path>"}
    run = req["run"]
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"request": what, "maxpm_run": run["id"], "stage": run["stage"], "work": run["work"],
                            "version": run["version"], "run": run["key"]}) + "\n")
    return {"note": f"asked for {what} in {path}"}


def main():
    req = json.load(sys.stdin)
    call = req["call"]
    if call == "describe":
        out = describe(req)
    elif call == "poll":
        out = poll(req)
    elif call in ("start", "stop"):
        out = request(req, call)
    else:
        out = {"error": f"jsonl does not do {call}"}
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
