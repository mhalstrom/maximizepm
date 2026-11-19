"""The Pulumi Delivery adapter of pipelines (#6133): Pulumi Delivery runs the releases; MaximizePM shows them like a
script pipeline's and holds the yes at a gate.

A Delivery pipeline is a pipeline stack (org/project/stack: maxpm pipeline forge <name> pulumi --repo
org/project/stack --secret <name>), and a MaximizePM stage names a Delivery stage (--workflow). One Delivery run
takes one release through every stage, so one run of a MaximizePM stage is that stage in one Delivery run (key
pd-<run number>-<stage>).

What Pulumi documents (public preview, routes under /api/preview that can change; read 2026-11-18): start a run
(POST .../delivery/dispatch), read one run (GET .../delivery/runs/<n>), continue a gate (POST .../gates/<id>/continue),
approve a gate's approval condition (POST /api/change-requests/<org>/<id>/approve), retry and cancel a run. The fields
read here are the ones that Pulumi's own gate hook reads: summary.number, summary.status.status, and stages[] with
name, status.status and gate (id, status, conditions[] with name, kind, state, changeRequestId). Not documented, so
not read: a list of runs, a run's commit and times, its jobs, a stage's roll back, redeploy, pause or force-promote,
and webhooks. Therefore:

- runs are read by number: from the pipeline's ref (the first run to read; --ref) up to the first number that Pulumi
  has no run for, at most READ_MAX in one pass; ref then moves to the oldest run that has not ended;
- a run's version is the version that MaximizePM gave it (maxpm pipeline start), else 'release <n>' when the run
  names its release number, else 'run <n>';
- a stage's steps and times are not shown: the run shows when MaximizePM read each change;
- a roll back is refused here: Pulumi's page does it (the stage's Actions menu).

A gated stage: when the MaximizePM gate opens, dispatch continues the Delivery gate (each pending manual condition;
an approval condition only after a person's yes, through its change request). A version that maxpm pipeline start
sends starts a Delivery run at the head of its source: Delivery cannot pin a commit from outside. The token is the
value of a maxpm secret (a Pulumi access token), sent as 'Authorization: token <value>'. FETCH is the test hook.
"""

import json
import re
import urllib.parse

from .. import core
from . import cdevents, github, store

POLL_EVERY = 60
READ_MAX = 20
RETRY_GRACE = 600  # seconds a retried stage's old end is not taken for the end of the retry
API = "https://api.pulumi.com"
FETCH = None
FINAL = ("succeeded", "failed", "canceled", "cancelled", "superseded")
_STATE = {"waiting": "queued", "queued": "queued", "running": "running", "in_progress": "running",
          "succeeded": "passed", "failed": "failed", "canceled": "cancelled", "cancelled": "cancelled",
          "superseded": "cancelled", "skipped": "skipped"}
_OUTCOME = {"passed": "success", "failed": "failure", "cancelled": "cancel", "skipped": "skip"}


def _root(p):
    return (p["forge_url"] or API).rstrip("/")


def _base(p):
    org, project, stack = (urllib.parse.quote(x, safe="") for x in p["repo"].split("/"))
    return f"{_root(p)}/api/preview/stacks/{org}/{project}/{stack}/delivery"


def call(conn, p, method, path, body=None, missing=False, ok=()):
    """One call of the Pulumi API for pipeline p, as JSON. missing: None for a 404; ok: other statuses that are not
    a failure (a 409 'the gate is not open': someone continued it). Refuses on any other status of 300 or more."""
    headers = {"Authorization": f"token {github._token(conn, p)}", "Accept": "application/json",
               "User-Agent": "maxpm-pipeline"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    url = _root(p) + path if path.startswith("/api/") else _base(p) + path
    status, out = (FETCH or github._fetch)(method, url, headers, data)
    if (missing and status == 404) or status in ok:
        return None
    if status >= 300:
        try:
            msg = json.loads(out or b"{}").get("message") or ""
        except (ValueError, AttributeError):
            msg = (out or b"")[:200].decode("utf-8", "replace")
        raise core.RiverError(f"Pulumi answered {status} to {method} {path}: {msg}")
    return json.loads(out) if out else {}


def _status(x):
    """A status of Pulumi's form {"status": "running"} (or a plain word)."""
    return x.get("status") if isinstance(x, dict) else x


def _rev(conn, p, n, summary):
    """The version of Delivery run n (see the module text)."""
    row = conn.execute("SELECT v.rev FROM stage_runs r JOIN pipeline_versions v ON v.id=r.version_id WHERE "
                       "r.pipeline_id=? AND r.key LIKE ? ORDER BY r.id LIMIT 1", (p["id"], f"pd-{n}-%")).fetchone()
    if row:
        return row["rev"]
    rel = summary.get("releaseNumber") or summary.get("release")
    rel = rel.get("number") if isinstance(rel, dict) else rel
    return f"release {rel}" if isinstance(rel, int) else f"run {n}"


def _waiting(conn, p, s, rev, first):
    """The run of stage s that waits for this Delivery run: one queued for its version (hand_on), or, on the first
    stage, the oldest one that dispatch started before the run had a number."""
    r = conn.execute("SELECT r.id FROM stage_runs r JOIN pipeline_versions v ON v.id=r.version_id WHERE r.stage_id=? "
                     "AND r.state='queued' AND (r.key IS NULL OR r.key NOT LIKE 'pd-%') AND v.rev=? ORDER BY r.id "
                     "LIMIT 1", (s["id"], rev)).fetchone()
    if r is None and first:
        r = conn.execute("SELECT id FROM stage_runs WHERE stage_id=? AND state='queued' AND runner='forge:pulumi' AND "
                         "(key IS NULL OR key NOT LIKE 'pd-%') ORDER BY id LIMIT 1", (s["id"],)).fetchone()
    return r["id"] if r else None


def _events(conn, p, s, n, st, run_status, rev, row, first):
    """The pipelinerun events of MaximizePM stage s in Delivery run n (st: the run's stage)."""
    key = f"pd-{n}-{s['workflow']}"
    status, gate = _status(st.get("status")), st.get("gate") or {}
    if status in (None, "pending") and gate.get("status") != "open" and row is None:
        return []  # Delivery has not reached it: no card for it yet
    state = "queued" if gate.get("status") == "open" else _STATE.get(status, "queued")
    ended = None
    if state in ("queued", "running") and run_status in FINAL:
        state, ended = "cancelled", f"the Delivery run ended ({run_status}) before stage {st.get('name')} did"
    if row is not None and row["purpose"] and row["runner"] == "forge:pulumi" and not row["started_at"] and \
            state in _OUTCOME and row["approved_at"] and \
            (core.now() - core.parse_iso(row["approved_at"])).total_seconds() < RETRY_GRACE:
        state = "queued"  # the end that Pulumi still shows is the one before the retry
    again = conn.execute("SELECT COUNT(*) FROM stage_runs WHERE pipeline_id=? AND key LIKE ?",
                         (p["id"], key + "~%")).fetchone()[0]
    eid = f"{key}.{again}"
    extra = {"pipeline": p["name"], "stage": s["name"], "version": rev,
             "run": None if row is not None else _waiting(conn, p, s, rev, first)}
    base = {"pipelineName": s["name"]}
    out = [cdevents.make("pipelinerun", "queued", key, base, extra, "pulumi", None, eid + "-queued")]
    if state in ("running", "passed", "failed"):
        out.append(cdevents.make("pipelinerun", "started", key, base, extra, "pulumi", None, eid + "-started"))
    if state in _OUTCOME:
        errors = ended or (f"Pulumi Delivery: stage {st.get('name')} {status}" if state == "failed" else None)
        out.append(cdevents.make("pipelinerun", "finished", key, {**base, "outcome": _OUTCOME[state], "errors": errors},
                                 extra, "pulumi", None, f"{eid}-finished"))
    return out


def _continue(conn, p, n, gate, person):
    """Continue the open gate of Delivery run n: each pending manual condition, and, after a person's yes (person),
    each pending approval through its change request. Returns the conditions it did not continue."""
    conds = [c for c in gate.get("conditions") or [] if c.get("state") == "pending"]
    path = f"/runs/{n}/gates/{urllib.parse.quote(str(gate['id']), safe='')}/continue"
    if not conds:
        call(conn, p, "POST", path, {}, ok=(409,))
        return []
    left = []
    for c in conds:
        if c.get("kind") == "manual":
            call(conn, p, "POST", path, {"condition": c.get("name")}, ok=(409,))
        elif c.get("kind") == "approval" and c.get("changeRequestId") and person:
            org = urllib.parse.quote(p["repo"].split("/")[0], safe="")
            call(conn, p, "POST", f"/api/change-requests/{org}/{c['changeRequestId']}/approve", {})
        else:
            left.append(f"{c.get('kind')} {c.get('name')}")
    return left


def _by_person(conn, row):
    return bool(row["approved_by"]) and core._is_person(conn, row["approved_by"])


def poll(conn, p):
    """Read pipeline p's Delivery runs into events. Returns counts."""
    counts = {"runs": 0, "events": 0}
    stages = {s["workflow"]: s for s in conn.execute(
        "SELECT * FROM pipeline_stages WHERE pipeline_id=? AND removed_at IS NULL AND workflow IS NOT NULL ORDER BY "
        "pos", (p["id"],))}
    first = min(stages.values(), key=lambda s: s["pos"])["workflow"] if stages else None
    start = int(p["ref"] or 1)
    # First the runs below the ref that a stage run still waits on (a retry opens an ended run again), then the runs
    # from the ref on, up to the first number that has no run yet.
    for n in sorted({int(re.match(r"pd-(\d+)-", r["key"])[1]) for r in conn.execute(
            "SELECT key FROM stage_runs WHERE pipeline_id=? AND state IN ('queued','running') AND key LIKE 'pd-%'",
            (p["id"],))}):
        if n < start and _read_run(conn, p, n, stages, first, counts) is not None:
            counts["runs"] += 1
    n, oldest_open = start, None
    while counts["runs"] < READ_MAX:
        run_status = _read_run(conn, p, n, stages, first, counts)
        if run_status is None:
            break
        counts["runs"] += 1
        if oldest_open is None and run_status not in FINAL:
            oldest_open = n
        n += 1
    nxt = str(oldest_open or n)
    if nxt != (p["ref"] or "1"):
        with core.tx(conn):
            conn.execute("UPDATE pipelines SET ref=? WHERE id=?", (nxt, p["id"]))
    return counts


def _read_run(conn, p, n, stages, first, counts):
    """Read Delivery run n into events and continue a gate that a yes in MaximizePM opened. Returns the run's
    status ('' when it names none), or None when Pulumi has no run n."""
    from . import runner
    run = call(conn, p, "GET", f"/runs/{n}", missing=True)
    if run is None:
        return None
    summary = run.get("summary") or {}
    run_status = _status(summary.get("status")) or ""
    rev = _rev(conn, p, n, summary)
    for st in run.get("stages") or []:
        s = stages.get(st.get("name"))
        if s is None:
            continue
        key = f"pd-{n}-{s['workflow']}"
        row = conn.execute("SELECT * FROM stage_runs WHERE pipeline_id=? AND key=?", (p["id"], key)).fetchone()
        evs = [e for e in _events(conn, p, s, n, st, run_status, rev, row, s["workflow"] == first)
               if not github._seen_by(conn, "pulumi", e["context"]["id"])]
        if evs:
            store.ingest(conn, evs)
            counts["events"] += len(evs)
            rev = _rev(conn, p, n, summary)  # a run that dispatch started keeps the version it was sent with
        row = conn.execute("SELECT * FROM stage_runs WHERE pipeline_id=? AND key=?", (p["id"], key)).fetchone()
        if row is None:
            continue
        if row["state"] == "passed" and any(".finished." in e["context"]["type"] for e in evs):
            runner.hand_on(conn, row["id"])
        gate = st.get("gate") or {}
        if gate.get("status") == "open" and row["runner"] == "forge:pulumi" and row["state"] == "queued":
            left = _continue(conn, p, n, gate, _by_person(conn, row))
            if _gate_note(n, left) != row["gate_note"]:
                with core.tx(conn):
                    conn.execute("UPDATE stage_runs SET gate_note=? WHERE id=?", (_gate_note(n, left), row["id"]))
    return run_status


def _gate_note(n, left):
    if not left:
        return f"continued the gate of Delivery run {n}"
    return (f"continued what it may of the gate of Delivery run {n}; on Pulumi's page a person decides: "
            + ", ".join(left))


def poll_due(conn):
    """Each Pulumi Delivery pipeline whose last read is POLL_EVERY old; an error stays on it (poll_error)."""
    out = {}
    old = core.iso(core.now() - core.timedelta(seconds=POLL_EVERY))
    for p in conn.execute("SELECT * FROM pipelines WHERE forge='pulumi' AND repo IS NOT NULL AND "
                          "(polled_at IS NULL OR polled_at<=?)", (old,)).fetchall():
        try:
            out[p["name"]], err = poll(conn, p), None
        except (core.RiverError, OSError, ValueError, KeyError, TypeError) as e:
            out[p["name"]], err = {"error": str(e)}, str(e)[:500]
        with core.tx(conn):
            conn.execute("UPDATE pipelines SET polled_at=?, poll_error=? WHERE id=?", (core.iso(core.now()), err, p["id"]))
    return out


def _row(conn, run_id):
    r = conn.execute("SELECT r.*, s.workflow, s.pos, v.rev FROM stage_runs r JOIN pipeline_stages s ON s.id=r.stage_id "
                     "LEFT JOIN pipeline_versions v ON v.id=r.version_id WHERE r.id=?", (run_id,)).fetchone()
    p = conn.execute("SELECT * FROM pipelines WHERE id=?", (r["pipeline_id"],)).fetchone()
    return r, p


def _number(conn, r):
    """The Delivery run that carries a stage run: its own key, else a run of its version."""
    m = re.match(r"pd-(\d+)-", r["key"] or "")
    if m:
        return int(m[1])
    row = conn.execute("SELECT key FROM stage_runs WHERE pipeline_id=? AND version_id=? AND key LIKE 'pd-%' ORDER BY "
                       "id DESC LIMIT 1", (r["pipeline_id"], r["version_id"])).fetchone() if r["version_id"] else None
    return int(re.match(r"pd-(\d+)-", row["key"])[1]) if row else None


def dispatch(conn, run_id):
    """A gate opened: continue the gate of the stage in its Delivery run (now, or when the run comes to it), retry
    the Delivery run (a run again), or start a Delivery run (a version that no run carries yet)."""
    r, p = _row(conn, run_id)
    purpose = r["purpose"] or ""
    if purpose == "rollback":
        raise core.RiverError("Pulumi documents no API to roll a stage back: use Roll back in the stage's Actions "
                              "menu on the Pulumi Delivery page")
    n = _number(conn, r)
    key, note = None, None
    if n is None:
        if purpose:
            raise core.RiverError(f"no Pulumi Delivery run carries {r['rev']}")
        out = call(conn, p, "POST", "/dispatch", {}) or {}
        n = out.get("runNumber") if isinstance(out.get("runNumber"), int) else None
        note = (f"started Delivery run {n}" if n else "started a Delivery run") + \
            " at the head of its source (Delivery cannot run a commit named from outside); it shows within a minute"
    elif purpose:  # a run again, or a run of the failed tests: Delivery runs the failed members of the run again
        call(conn, p, "POST", f"/runs/{n}/retry", {})
        note = f"retried Delivery run {n}: it runs its failed members again with the same commit"
    else:
        st = next((x for x in (call(conn, p, "GET", f"/runs/{n}") or {}).get("stages") or []
                   if x.get("name") == r["workflow"]), None)
        gate = (st or {}).get("gate") or {}
        if gate.get("status") == "open":
            note = _gate_note(n, _continue(conn, p, n, gate, _by_person(conn, r)))
        else:
            note = f"the yes is given: MaximizePM continues the gate when Delivery run {n} comes to it"
    if n:
        key = f"pd-{n}-{r['workflow']}"
    with core.tx(conn):
        if n and not conn.execute("SELECT 1 FROM stage_runs WHERE pipeline_id=? AND key LIKE 'pd-%' LIMIT 1",
                                  (r["pipeline_id"],)).fetchone():
            conn.execute("UPDATE pipelines SET ref=? WHERE id=?", (str(n), r["pipeline_id"]))  # its first Delivery run
        if key and r["key"] != key:
            # The stage in that Delivery run belongs to this run from now on: an earlier run of it (the run that a
            # run again repeats) keeps its record under another key.
            conn.execute("UPDATE stage_runs SET key=key || '~' || id WHERE pipeline_id=? AND key=? AND id<>?",
                         (r["pipeline_id"], key, run_id))
            conn.execute("UPDATE stage_runs SET key=? WHERE id=?", (key, run_id))
        conn.execute("UPDATE stage_runs SET runner='forge:pulumi', gate_note=? WHERE id=?", (note, run_id))


def cancel(conn, run_id):
    """Cancel the Delivery run that carries a stage run: Pulumi cancels the whole run, each stage of it."""
    r, p = _row(conn, run_id)
    n = _number(conn, r)
    if n is None:
        raise core.RiverError(f"no Pulumi Delivery run carries run #{run_id} yet")
    call(conn, p, "POST", f"/runs/{n}/cancel", {})
    return {"run": run_id, "delivery_run": n, "cancelled": True}
