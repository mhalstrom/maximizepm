"""The '::step' lines that a stage's command prints, so a long stage shows its parts and their times (#6112).

    ::step start <name> [kind=build|test|deploy|check|wait] [where=<host>] [parallel] [at=<RFC 3339 time>]
    ::step end <name> passed|failed|skipped [where=<host>] [<time taken: 9m02s, 45s, 1h2m>] [at=<time>] [<note>]

A step is its name and its host: 'deploy where=edge-1' and 'deploy where=cell-1' are two steps. An end line ends
the newest open step of that name (and host, when it names one); an end line with no start before it makes a step
that ended then, and started the time taken before. An end line with no state word means passed. With no at=, a
line's time is the time MaximizePM reads it: the runner reads a stage's output while it runs.

Each step line becomes a taskrun event (store.ingest), so a forge adapter's steps and these lines are one record.
Every other line goes to the last lines of the run, and of the newest open step.
"""

import re
import uuid
from datetime import timedelta

from .. import core
from . import cdevents, store

STEP_KINDS = ("build", "test", "deploy", "check", "wait")
STEP_STATES = ("passed", "failed", "skipped")

_LINE = re.compile(r"^\s*::step\s+(start|end)\s+(\S+)\s*(.*)$")
_TOOK = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?$")


def _took(word):
    """The seconds of a time taken (9m02s, 45s, 1h2m, 1.5s), or None for another word."""
    m = _TOOK.match(word)
    if not m or not any(m.groups()):
        return None
    h, mi, s = m.groups()
    return int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)


def parse(line):
    """The parts of one '::step' line, or None for any other line."""
    m = _LINE.match(line)
    if not m:
        return None
    step = {"op": m.group(1), "name": m.group(2), "kind": None, "where": None, "parallel": False, "state": None,
            "seconds": None, "at": None}
    note = []
    for t in m.group(3).split():
        k, eq, v = t.partition("=")
        if eq and k == "kind" and v in STEP_KINDS:
            step["kind"] = v
        elif eq and k == "where" and v:
            step["where"] = v
        elif eq and k == "at" and v:
            try:
                step["at"] = cdevents.timestamp(v)
            except core.RiverError:
                note.append(t)
        elif t == "parallel":
            step["parallel"] = True
        elif step["op"] == "end" and t in STEP_STATES and step["state"] is None:
            step["state"] = t
        elif step["seconds"] is None and _took(t) is not None:
            step["seconds"] = _took(t)
        else:
            note.append(t)
    if step["op"] == "end" and step["state"] is None:
        step["state"] = "passed"
    step["note"] = " ".join(note) or None
    return step


def _event(conn, run, step, at, source):
    """The taskrun event of one step line (inside the tx: an end line looks for its open step)."""
    when = step["at"] or at
    if step["op"] == "start":
        key = f"{step['name']}@{step['where'] or ''}/{uuid.uuid4().hex[:8]}"
        return cdevents.make("taskrun", "started", key,
                             {"taskName": step["name"], "pipelineRun": {"id": run["key"], "source": source}},
                             {"pipeline": run["pipeline"], "run": run["id"], "kind": step["kind"],
                              "where": step["where"], "parallel": step["parallel"] or None, "note": step["note"]},
                             source, when)
    q, args = "SELECT key, host FROM stage_steps WHERE run_id=? AND name=? AND state='running'", [run["id"], step["name"]]
    if step["where"]:
        q, args = q + " AND host=?", args + [step["where"]]
    open_ = conn.execute(q + " ORDER BY id DESC LIMIT 1", args).fetchone()
    key = open_["key"] if open_ else f"{step['name']}@{step['where'] or ''}/{uuid.uuid4().hex[:8]}"
    word = {"passed": "success", "failed": "failure", "skipped": "skip"}[step["state"]]
    return cdevents.make("taskrun", "finished", key,
                         {"taskName": step["name"], "pipelineRun": {"id": run["key"], "source": source},
                          "outcome": word},
                         {"pipeline": run["pipeline"], "run": run["id"], "kind": step["kind"],
                          "where": step["where"] or (open_["host"] if open_ else None), "seconds": step["seconds"],
                          "note": step["note"]},
                         source, when)


def hook_lines(conn, hook_run, lines, at=None):
    """The '::step' lines of a deploy target's hook run (#6126), into hook_steps: core._run_hook gives them as they
    come. A line's at= time is taken as given; with none, the time of this call. Returns the number of step lines."""
    now = cdevents.timestamp(at) if at else core.iso(core.now())
    n = 0
    with core.tx(conn):
        for line in lines:
            step = parse(line)
            if step is None:
                continue
            n += 1
            when = step["at"] or now
            if step["op"] == "start":
                conn.execute("INSERT INTO hook_steps(hook_run, name, kind, host, parallel, state, started_at, note) "
                             "VALUES (?,?,?,?,?, 'running', ?,?)", (hook_run, step["name"][:200], step["kind"],
                                                                     step["where"], int(step["parallel"]), when,
                                                                     step["note"]))
                continue
            q, args = "SELECT * FROM hook_steps WHERE hook_run=? AND name=? AND state='running'", [hook_run, step["name"]]
            if step["where"]:
                q, args = q + " AND host=?", args + [step["where"]]
            row = conn.execute(q + " ORDER BY id DESC LIMIT 1", args).fetchone()
            secs = step["seconds"]
            if row is None:  # an end line with no start: it started the time taken before
                started = core.iso(core.parse_iso(when) - timedelta(seconds=secs)) if secs is not None else None
                conn.execute("INSERT INTO hook_steps(hook_run, name, kind, host, state, started_at, ended_at, seconds, "
                             "note) VALUES (?,?,?,?,?,?,?,?,?)", (hook_run, step["name"][:200], step["kind"],
                                                                   step["where"], step["state"], started, when, secs,
                                                                   step["note"]))
                continue
            if secs is None:
                secs = (core.parse_iso(when) - core.parse_iso(row["started_at"])).total_seconds()
            conn.execute("UPDATE hook_steps SET state=?, ended_at=?, seconds=?, note=COALESCE(?, note) WHERE id=?",
                         (step["state"], when, secs, step["note"], row["id"]))
    return n


def ingest_output(conn, run_id, text, at=None, source="maxpm"):
    """Read a piece of a stage's output: whole lines (the runner keeps a cut last line for its next call). Each
    '::step' line becomes a taskrun event; the other lines go to the last lines of the run and of the newest open
    step. One transaction. Returns the number of step lines and of other lines."""
    at = cdevents.timestamp(at) if at else core.iso(core.now())
    lines = text.splitlines() if isinstance(text, str) else list(text)
    n_steps, plain, per_step = 0, [], {}
    with core.tx(conn):
        run = conn.execute("SELECT r.id, r.key, p.name pipeline FROM stage_runs r JOIN pipelines p "
                           "ON p.id=r.pipeline_id WHERE r.id=?", (run_id,)).fetchone()
        if not run:
            raise core.RiverError(f"no stage run {run_id}")

        def newest():
            row = conn.execute("SELECT id FROM stage_steps WHERE run_id=? AND state='running' "
                               "ORDER BY started_at DESC, id DESC LIMIT 1", (run_id,)).fetchone()
            return row["id"] if row else None

        current = newest()
        for line in lines:
            step = parse(line)
            if step is None:
                plain.append(line)
                if current is not None:
                    per_step.setdefault(current, []).append(line)
                continue
            store._ingest_one(conn, cdevents.parse(_event(conn, run, step, at, source)))
            n_steps += 1
            current = newest()
        store.run_output(conn, run_id, plain)
        for sid, ls in per_step.items():
            store.step_output(conn, sid, ls)
    return {"steps": n_steps, "lines": len(plain)}
