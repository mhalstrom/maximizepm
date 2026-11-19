"""Transitions for outside flows (#6164): an outside system asks MaximizePM whether a version may enter a stage.

mark 2026-11-18 (canvas release-tracking, 'Any deployment process, and plugins', gap (c), the level 'gate' of the
contract): a deploy that Jenkins, Argo CD, Octopus, a GitHub workflow on a runner of this computer, or a cron job runs
asks before each stage "may version 42 enter prod?", and the transition into that stage answers, with the same controls
as for a stage that MaximizePM runs (#6139): a block, the rule while a fault is open, a freeze, the window, a delay,
disabled, a hold of the whole pipeline, and its mode (a person's yes, the manager's yes, the release, a delay). The
outside system runs the stage itself; MaximizePM never runs its command.

    maxpm pipeline may <pipeline> <stage> --rev <version> [--wait <time>] [--from <system>]
    GET /api/pipeline-gate?pipeline=&stage=&rev=[&wait=<time>][&from=<system>]

The first question about a version at a stage queues a stage run of it there, marked outside (its asker): the arrow
lists it as a waiting version, a person's yes makes the approval card once, the manager gets the alert of the release
once (the same as for our own runs), and maxpm serve keeps its note but never starts it. The answer is yes when the
transition lets it in: the run is then running (the version entered the stage), and the outside system reports its end
with an event of that run (maxpm pipeline event, #6163), or the version's question at a later stage ends it as passed.
A no says what holds it and until when; --wait asks again every POLL seconds until a yes or the end of the wait.

Each change of the answer is one line of the audit log (#6144), by the asker, with the version, the stage and the
control: a poll that gets the same no again writes nothing.
"""

import time
import uuid

from .. import core
from . import audit, runner, store, transitions

POLL = 5.0  # seconds between two questions of a wait
WAIT_MAX = 3600  # the longest wait of one command (seconds)
HTTP_WAIT_MAX = 600  # the longest wait of one HTTP request: a request that waits holds one thread of maxpm serve
FINAL = ("passed", "failed", "cancelled", "skipped")


def _stage(conn, p, stage):
    s = conn.execute("SELECT * FROM pipeline_stages WHERE pipeline_id=? AND name=? AND removed_at IS NULL",
                     (p["id"], stage)).fetchone()
    if not s:
        names = [r["name"] for r in conn.execute("SELECT name FROM pipeline_stages WHERE pipeline_id=? AND removed_at "
                                                 "IS NULL ORDER BY pos, id", (p["id"],))]
        raise core.RiverError(f"pipeline {p['name']} has no stage {stage}; its stages: {', '.join(names) or 'none'}")
    if s["run"]:
        raise core.RiverError(f"stage {stage} of pipeline {p['name']} runs here: maxpm serve runs its command when its "
                              "transition opens. An outside system asks only for a stage that it runs itself (a stage "
                              "with no --run)")
    return s


def _run_for(conn, p, s, rev, asker):
    """The run of the version at the stage that the question is about (inside a tx): the newest one of the flow; a new
    queued run, marked outside, when there is none or the last one ended (a failed run asked again: a new try). A run
    that a person declined stays: the answer stays no."""
    vid = store._version_id(conn, p["id"], rev)
    r = conn.execute("SELECT * FROM stage_runs WHERE stage_id=? AND version_id=? AND purpose IS NULL ORDER BY id DESC "
                     "LIMIT 1", (s["id"], vid)).fetchone()
    now = store._now()
    if r is not None and (r["state"] not in FINAL or r["state"] == "passed" or _declined(r)):
        conn.execute("UPDATE stage_runs SET outside=COALESCE(outside, ?), outside_at=? WHERE id=?", (asker, now, r["id"]))
        return r["id"]
    return conn.execute("INSERT INTO stage_runs(pipeline_id, stage_id, version_id, key, state, queued_at, source, outside,"
                        " outside_at) VALUES (?,?,?,?, 'queued', ?,?,?,?)",
                        (p["id"], s["id"], vid, f"gate:{uuid.uuid4()}", now, asker, asker, now)).lastrowid


def _declined(r):
    return r["state"] == "cancelled" and "declined" in (r["errors"] or "")


def _end_earlier(conn, p, s, vid, asker):
    """The version asks at this stage, so it passed the stages before it: their outside runs that still run end as
    passed, and hand the version on as an event of their end does (a deploy stage: its environment runs it now)."""
    for x in conn.execute("SELECT r.id FROM stage_runs r JOIN pipeline_stages t ON t.id=r.stage_id WHERE r.pipeline_id=? "
                          "AND r.version_id=? AND r.outside IS NOT NULL AND r.state='running' AND t.pos<?",
                          (p["id"], vid, s["pos"])).fetchall():
        store.run_finish(conn, x["id"], "passed", source=asker)
        runner.hand_on(conn, x["id"])


def _line(conn, r, asker, kind, said, text, until=None):
    """One audit line when the answer changed (inside a tx)."""
    if r["outside_said"] == said:
        return
    conn.execute("UPDATE stage_runs SET outside_said=? WHERE id=?", (said, r["id"]))
    audit.write(conn, r["pipeline_id"], asker, "ask", text, kind=kind, place=f"the transition into {r['stage']}",
                rev=r["rev"], run_id=r["id"], result=said.split(":")[0], until=until)


def ask(conn, pipeline, stage, rev, asker=None, actor=None):
    """Once: may version rev enter stage now? {may, why, until, pipeline, stage, version, run, asked_by}. Anyone may ask
    (a script, a person, an agent); the answer acts only on the outside run of that version at that stage."""
    rev = str(rev or "").strip()
    if not rev:
        raise core.RiverError("name the version: --rev <commit, tag or build number>")
    who = (asker or "").strip() or actor or "an outside system"
    kind = "forge" if (asker or "").strip() or not actor else None  # a system, in the audit log's filter of systems
    p = store._pipeline(conn, pipeline)
    s = _stage(conn, p, stage)
    with core.tx(conn):
        run_id = _run_for(conn, p, s, rev, who)
    r = runner._row(conn, run_id)
    _end_earlier(conn, p, s, r["version_id"], who)
    out = {"pipeline": p["name"], "stage": s["name"], "version": r["rev"], "run": r["id"], "asked_by": who,
           "until": None}
    if r["state"] in ("running", "passed"):
        return dict(out, may=True, why=f"version {r['rev']} entered stage {s['name']} at {r['started_at'] or r['queued_at']}")
    if _declined(r):
        return dict(out, may=False, why=f"declined: {r['errors']}")
    open_, note = runner.gate(conn, r)
    if note == "declined":
        store.run_finish(conn, r["id"], "cancelled", errors=f"the yes at the gate was declined (#{r['gate_item']})",
                         source=who)
        r = runner._row(conn, run_id)
        with core.tx(conn):
            _line(conn, r, who, kind, "no:declined", f"asked whether version {r['rev']} may enter stage {s['name']}: no, "
                                                    f"the yes was declined (#{r['gate_item']})")
        return dict(out, may=False, why=f"declined (#{r['gate_item']})")
    if not open_:
        c = transitions.closed(conn, transitions.get(conn, r["stage_id"]),
                               {"id": p["id"], "name": p["name"], "project": p["project"]})
        until = c["until"] if c and c.get("until") and str(c["until"])[:1].isdigit() else None
        said = f"no:{c['kind'] if c else 'hold' if r['pipe_hold'] else 'mode'}"
        with core.tx(conn):
            conn.execute("UPDATE stage_runs SET gate_note=? WHERE id=?", (note, r["id"]))
            _line(conn, r, who, kind, said, f"asked whether version {r['rev']} may enter stage {s['name']}: no, {note}",
                  until)
        return dict(out, may=False, why=note, until=until)
    with core.tx(conn):
        now = store._now()
        if r["gate_item"] and not r["approved_at"]:  # the person's yes on the card
            by = core._done_by(conn, r["gate_item"])
            conn.execute("UPDATE stage_runs SET approved_by=?, approved_at=? WHERE id=?", (by, now, r["id"]))
        conn.execute("UPDATE stage_runs SET state='running', started_at=?, gate_note=NULL WHERE id=?", (now, r["id"]))
        _line(conn, r, who, kind, "yes", f"asked whether version {r['rev']} may enter stage {s['name']}: yes; it entered "
                                         f"(run #{r['id']})" + (f", {note}" if note else ""))
    return dict(out, may=True, why=note or f"the transition into {s['name']} lets it in")


def may(conn, pipeline, stage, rev, wait=None, asker=None, actor=None, limit=WAIT_MAX, sleep=time.sleep):
    """ask(), again every POLL seconds until a yes or the end of wait (a duration: 30m; at most limit seconds)."""
    secs = min(core.parse_duration(wait).total_seconds(), limit) if wait else 0
    end = time.monotonic() + secs
    while True:
        res = ask(conn, pipeline, stage, rev, asker, actor)
        if res["may"] or time.monotonic() + POLL > end:
            return dict(res, waited=round(secs - max(0.0, end - time.monotonic())) if secs else 0)
        sleep(POLL)
