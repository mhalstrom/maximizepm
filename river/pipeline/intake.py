"""Events in from any deployment system (#6163): Jenkins, Argo CD, Octopus or a cron job reports to a pipeline
with no plugin. Three ways in, each into store.ingest, as a plugin's events go:

- maxpm pipeline event <pipeline> --file <path or -> reads CDEvents: one JSON object, a JSON list, or one object on
  each line (JSON lines);
- maxpm pipeline report <run> [--steps <path or ->] [--junit <path>] adds '::step' lines (steps.py) and a JUnit file
  (junit.py) to a stage run;
- POST /api/pipeline-events takes the same over HTTP: {"pipeline": <name>, "events": [...]} or {"run": <id>,
  "steps": <text>, "junit": <xml>}, with 'Authorization: Bearer <token>'. The token is the value of a maxpm secret
  that a person or the manager names for that pipeline (maxpm pipeline intake <pipeline> --secret <name>), so a token
  reaches one pipeline only. maxpm serve answers this address only on this computer and from the person's tailnet
  (server.Handler._refuse, policy p159); a request from a browser page (with an Origin) is refused.

An event's id is kept once for each pipeline (the id gets the pipeline's name before it), so the same change sent
twice changes nothing. A run that an event ends as passed hands its version to the next stage, as a forge's does.
"""

import hmac
import json

from .. import core
from . import junit, steps, store

MAX_BYTES = 8 << 20  # the largest request body or file
MAX_EVENTS = 2000  # events in one request


def parse(text):
    """The events of a text: one JSON object, a JSON list of them, or one object on each line. Refuses with what is
    wrong, before any write."""
    text = (text or "").strip()
    if not text:
        raise core.RiverError("no events: send CDEvents JSON (one object, a list, or one object on each line)")
    try:
        got = json.loads(text)
        events = got if isinstance(got, list) else [got]
    except ValueError:
        events = []
        for n, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except ValueError as e:
                raise core.RiverError(f"line {n} is not JSON ({e}); send one CDEvents object on each line") from None
    if len(events) > MAX_EVENTS:
        raise core.RiverError(f"more than {MAX_EVENTS} events in one request; send the rest in another")
    return events


def _own(ev, pipeline):
    """The event as it is kept: its id once for this pipeline."""
    if not isinstance(ev, dict) or not isinstance(ev.get("context"), dict):
        return ev  # cdevents.parse refuses it with the reason
    ev = dict(ev, context=dict(ev["context"]))
    if ev["context"].get("id"):
        ev["context"]["id"] = f"{pipeline}:{ev['context']['id']}"
    return ev


def _audit(conn, pid, actor, via, change, run_id=None):
    """One line of the pipeline's audit log for each delivery that changed something (#6144): who sent it (the
    person or the manager at the command, or the token over HTTP)."""
    from . import audit
    with core.tx(conn):
        audit.write(conn, pid, actor, "report", f"{change} ({via})", kind="forge" if via == "http" else None,
                    place="the pipeline", run_id=run_id)


def take(conn, pipeline, events, actor=None, via="maxpm pipeline event"):
    """Keep the events of one pipeline (all of them or none) and hand on the versions whose runs they end as
    passed. Returns {'pipeline', 'events': new, 'duplicates', 'runs': [the stage runs they touched]}."""
    from . import runner
    p = store._pipeline(conn, pipeline)
    res = store.ingest(conn, [_own(e, p["name"]) for e in events], pipeline=p["name"])
    runner.hand_on_passed(conn, res)
    new = [r for r in res if not r.get("duplicate")]
    if new:
        _audit(conn, p["id"], actor, via, f"{len(new)} event(s) in")
    return {"pipeline": p["name"], "events": len(new), "duplicates": len(res) - len(new),
            "runs": sorted({r["run"] for r in res if r.get("run")})}


def report(conn, run_id, steps_text=None, junit_xml=None, actor=None, via="maxpm pipeline report"):
    """Add '::step' lines and a JUnit report to a stage run. Returns {'run', 'steps', 'lines', 'tests', 'failed'}."""
    row = conn.execute("SELECT pipeline_id FROM stage_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise core.RiverError(f"no stage run {run_id}: maxpm pipeline show <pipeline> names the runs")
    if not steps_text and not junit_xml:
        raise core.RiverError("nothing to report: give --steps (the '::step' lines) or --junit (a JUnit file)")
    out = {"run": run_id, "steps": 0, "lines": 0, "tests": 0}
    if steps_text:
        out.update(steps.ingest_output(conn, run_id, steps_text, source="report"))
    if junit_xml:
        data = junit_xml.encode() if isinstance(junit_xml, str) else junit_xml
        got = junit.ingest_junit(conn, run_id, xml=data, source="report")
        out.update(tests=got["tests"], failed=got["failed"] + got["error"])
    _audit(conn, row["pipeline_id"], actor, via, f"run #{run_id}: {out['steps']} step line(s), {out['tests']} test(s) "
           "reported", run_id)
    return out


def _lead(conn, actor, what):
    why = core._lead_refusal(conn, actor, what)
    if why:
        raise core.RiverError(why)


def event_cmd(conn, pipeline, text, actor=None):
    """maxpm pipeline event: a person or the manager (a cron job of the person runs as the person)."""
    _lead(conn, actor, "sends events to a pipeline")
    return take(conn, pipeline, parse(text), actor)


def report_cmd(conn, run_id, steps_text=None, junit_xml=None, actor=None):
    """maxpm pipeline report: a person or the manager."""
    _lead(conn, actor, "reports steps and tests of a stage run")
    return report(conn, run_id, steps_text, junit_xml, actor)


def set_secret(conn, pipeline, secret, actor=None):
    """maxpm pipeline intake <pipeline> --secret <name> (or --off): the maxpm secret whose value a sender shows to
    POST /api/pipeline-events for this pipeline."""
    from . import define
    from .. import vault
    if secret:
        vault.check_name(secret)
    with core.tx(conn):
        define._may(conn, actor)
        p = store._pipeline(conn, pipeline)
        conn.execute("UPDATE pipelines SET intake_secret=? WHERE id=?", (secret, p["id"]))
        define._change(conn, p["id"], actor, f"events in over HTTP: token from secret {secret}" if secret else
                       "events in over HTTP: off")
    return {"pipeline": p["name"], "secret": secret}


def _pipeline_of(conn, body):
    if body.get("pipeline"):
        return store._pipeline(conn, str(body["pipeline"]))
    if body.get("run") is not None:
        r = conn.execute("SELECT p.* FROM stage_runs r JOIN pipelines p ON p.id=r.pipeline_id WHERE r.id=?",
                         (body["run"],)).fetchone()
        if r:
            return r
    raise core.RiverError("name the pipeline ({\"pipeline\": <name>, \"events\": [...]}) or the run ({\"run\": <id>})")


def http(conn, token, body):
    """POST /api/pipeline-events: (status, answer). body: the request's JSON object; token: the Bearer token."""
    if not isinstance(body, dict):
        return 400, {"error": "send one JSON object: {\"pipeline\", \"events\"} or {\"run\", \"steps\", \"junit\"}"}
    try:
        p = _pipeline_of(conn, body)
    except core.RiverError as e:
        return 404 if "no pipeline" in str(e) or "no stage run" in str(e) else 400, {"error": str(e)}
    if not p["intake_secret"]:
        return 403, {"error": f"pipeline {p['name']} takes no events over HTTP; a person or the manager turns it on: "
                              f"maxpm pipeline intake {p['name']} --secret <name>"}
    from . import forge
    try:
        want = forge.secret(conn, p["intake_secret"], f"pipeline {p['name']}: events in over HTTP")
    except core.RiverError as e:
        return 503, {"error": f"the token of pipeline {p['name']} cannot be read: {e}"}
    if not token or not hmac.compare_digest(token.encode(), want.encode()):
        return 401, {"error": f"the token is not the one of pipeline {p['name']}"}
    who = f"token {p['intake_secret']}"
    try:
        if "events" in body:
            events = body["events"] if isinstance(body["events"], list) else [body["events"]]
            if len(events) > MAX_EVENTS:
                return 413, {"error": f"more than {MAX_EVENTS} events in one request"}
            return 200, {"ok": True, "result": take(conn, p["name"], events, who, "http")}
        run = body.get("run")
        if not isinstance(run, int) or not conn.execute("SELECT 1 FROM stage_runs WHERE id=? AND pipeline_id=?",
                                                        (run, p["id"])).fetchone():
            return 404, {"error": f"pipeline {p['name']} has no stage run {run}"}
        return 200, {"ok": True, "result": report(conn, run, body.get("steps"), body.get("junit"), who, "http")}
    except core.RiverError as e:
        return 400, {"error": str(e)}
