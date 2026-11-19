"""The no-forge runner (#6114): maxpm serve runs the stages of a pipeline on this computer, in order.

maxpm pipeline start <name> [--rev <commit>] queues the version at the first stage. Each pass of the loop of maxpm
serve (serve_pass) then:

- ends as failed a run that this process no longer runs (serve stopped while it ran);
- for each queued run, looks at the gate of its stage (gate): auto opens at once; 'auto after 30m' opens 30 minutes
  after the stage before passed, while no check of that stage's environment (its target) or of the project failed
  since; manager waits for maxpm pipeline approve; person asks with a person's approval item (the card of
  release_approval person) and opens when a person marks it done, and cancels the run when it is dropped;
- starts each run whose gate is open, one at a time for each stage, in a thread of its own.

A stage run runs the stage's command in the project's folder, in its own process group, with MAXPM_PIPELINE,
MAXPM_STAGE, MAXPM_REV, MAXPM_RUN and MAXPM_ENV set, for at most stage_timeout. Its output goes to the run as it
comes, and its '::step' lines become steps (steps.ingest_output). A test stage that names a JUnit file
(maxpm pipeline stage ... --junit <path>) has the file read after its command. A stage that passes hands the version
to the next stage (and a deploy stage records it in its environment); a stage that fails stops the pipeline for
that version. A stage with no command (a review or an approval only) passes when its gate opens. A stage with a
forge workflow and no command is left to that forge's adapter.

No session types a stage's command: serve runs it, outside every sandbox, as it runs a target's hooks.
"""

import os
import queue
import subprocess
import threading
import time
import uuid

from .. import core
from . import cdevents, junit, steps, store

RUNNING = {}  # run id -> the thread of this process that runs it; server.restart_blocked waits for them
LOCK = threading.Lock()
SYNC = {"on": False}  # tests: the pass runs a stage itself, not in a thread
FLUSH_EVERY = 1.0  # seconds between two writes of a stage's new output lines
FLUSH_LINES = 200


def _row(conn, run_id):
    return conn.execute(
        "SELECT r.*, s.name stage, s.kind, s.run cmd, s.workflow, s.junit, s.gate, s.gate_rule, s.pos, s.env_id, "
        "p.name pipeline, p.project, v.rev FROM stage_runs r JOIN pipeline_stages s ON s.id=r.stage_id "
        "JOIN pipelines p ON p.id=r.pipeline_id LEFT JOIN pipeline_versions v ON v.id=r.version_id WHERE r.id=?",
        (run_id,)).fetchone()


def _folder(conn, project):
    """The folder of the pipeline's project (a read of the projects table: one of the few calls into MaximizePM)."""
    r = conn.execute("SELECT path FROM projects WHERE name=?", (project,)).fetchone() if project else None
    return os.path.expanduser(r["path"]) if r and r["path"] else None


def _project_id(conn, project):
    r = conn.execute("SELECT id FROM projects WHERE name=?", (project,)).fetchone() if project else None
    return r["id"] if r else None


def _head(folder):
    try:
        out = subprocess.run(["git", "-C", folder, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _queue(conn, pipeline, stage, rev, source="maxpm"):
    ev = cdevents.make("pipelinerun", "queued", str(uuid.uuid4()), {"pipelineName": stage},
                       {"pipeline": pipeline, "stage": stage, "version": rev}, source)
    return store.ingest(conn, ev)["run"]


def start(conn, pipeline, rev=None, actor=None):
    """maxpm pipeline start: queue a version (a commit; the project folder's HEAD when none) at the first stage."""
    p = store._pipeline(conn, pipeline)
    first = conn.execute("SELECT * FROM pipeline_stages WHERE pipeline_id=? AND removed_at IS NULL ORDER BY pos, id "
                         "LIMIT 1", (p["id"],)).fetchone()
    if not first:
        raise core.RiverError(f"pipeline {p['name']} has no stages; add one: maxpm pipeline stage {p['name']} "
                              "<stage> --kind <kind> --run \"<command>\"")
    if not rev:
        folder = _folder(conn, p["project"])
        rev = _head(folder) if folder else None
        if not rev:
            raise core.RiverError(f"name the version: maxpm pipeline start {p['name']} --rev <commit> (the project "
                                  "folder has no git HEAD to take)")
    vid = conn.execute("SELECT id FROM pipeline_versions WHERE pipeline_id=? AND (rev=? OR substr(rev,1,length(?))=? "
                       "OR substr(?,1,length(rev))=rev)", (p["id"], rev, rev, rev, rev)).fetchone()
    if vid:
        busy = conn.execute("SELECT r.id, s.name FROM stage_runs r JOIN pipeline_stages s ON s.id=r.stage_id WHERE "
                            "r.version_id=? AND r.state IN ('queued','running')", (vid["id"],)).fetchone()
        if busy:
            raise core.RiverError(f"version {rev} is in pipeline {p['name']} already: run #{busy['id']} at stage "
                                  f"{busy['name']}")
    run = _queue(conn, p["name"], first["name"], rev)
    with core.tx(conn):
        core._event(conn, None, actor or "maxpm", f"pipeline {p['name']}: version {rev} started at stage "
                                                  f"{first['name']} (run #{run})")
    return {"pipeline": p["name"], "stage": first["name"], "version": rev, "run": run}


# ---------------------------------------------------------------- gates

def _previous(conn, r):
    """The run of the stage before, for the same version, that passed (None for the first stage)."""
    return conn.execute("SELECT x.*, s.env_id FROM stage_runs x JOIN pipeline_stages s ON s.id=x.stage_id WHERE "
                        "x.pipeline_id=? AND x.version_id IS ? AND s.pos<? AND s.removed_at IS NULL AND "
                        "x.state='passed' ORDER BY s.pos DESC, x.id DESC LIMIT 1",
                        (r["pipeline_id"], r["version_id"], r["pos"])).fetchone()


def _failed_check(conn, r, prev, since):
    """The name of a check that failed since then: a check of the target of the environment that the stage before
    deployed to, or of the pipeline's project. None when every run since passed."""
    target = None
    if prev is not None and prev["env_id"]:
        e = conn.execute("SELECT target FROM pipeline_envs WHERE id=?", (prev["env_id"],)).fetchone()
        target = e["target"] if e else None
    row = conn.execute("SELECT c.scope, c.name FROM ops_runs o JOIN checks c ON c.id=o.check_id WHERE "
                       "((c.scope_kind='target' AND c.scope=?) OR (c.scope_kind='project' AND c.scope=?)) AND "
                       "o.started_at>=? AND o.status IN ('warning','critical') ORDER BY o.id LIMIT 1",
                       (target, r["project"], since)).fetchone()
    return f"{row['scope']}/{row['name']}" if row else None


def _approve_cmd(r):
    return f"maxpm pipeline approve {r['pipeline']} {r['stage']} --rev {(r['rev'] or '')[:12]}"


def gate(conn, r):
    """(open, note): whether the queued run r may start now, and why not."""
    if r["approved_at"]:
        return True, None
    g, rule = r["gate"], r["gate_rule"]
    if g == "auto":
        if not rule:
            return True, None
        prev = _previous(conn, r)
        since = (prev["ended_at"] if prev else None) or r["queued_at"] or store._now()
        bad = _failed_check(conn, r, prev, since)
        if bad:
            return False, (f"check {bad} failed since {since}: the gate (auto after {rule}) stays closed; a person or "
                           f"the manager lets the version in: {_approve_cmd(r)}")
        opens = core.parse_iso(since) + core.parse_duration(rule)
        if core.now() < opens:
            return False, f"opens at {core.iso(opens)} (auto after {rule}) while the checks pass"
        return True, None
    if g == "manager":
        return False, f"waits for the manager: {_approve_cmd(r)}"
    # person
    who = rule or "a person"
    if r["gate_item"]:
        it = conn.execute("SELECT status FROM items WHERE id=?", (r["gate_item"],)).fetchone()
        if it and it["status"] == "done":
            by = core._done_by(conn, r["gate_item"])
            if core._is_person(conn, by) and (not rule or by == rule):
                return True, f"approved by {by} (#{r['gate_item']})"
        if it is None or it["status"] == "dropped":
            return False, "declined"
    return False, f"waits for {who}'s yes" + (f" on #{r['gate_item']}" if r["gate_item"] else "")


def _ask(conn, r, note):
    """Ask once for the yes of a manager or person gate: an alert to the active manager, or a person's approval
    item in the pipeline's project. Returns the note to keep on the run."""
    if r["gate"] == "manager":
        if r["gate_note"] and "(asked " in r["gate_note"]:
            return r["gate_note"]
        mgr = core.active_manager(conn)
        if not mgr:
            return note + " (no manager runs now: asked when one starts)"
        with core.tx(conn):
            core._send(conn, "alert", "maxpm", f"pipeline {r['pipeline']}: version {(r['rev'] or '')[:12]} waits at "
                       f"the gate of stage {r['stage']} (manager). Let it in: {_approve_cmd(r)}", to=mgr)
        return note + f" (asked {mgr})"
    if r["gate"] == "person" and not r["gate_item"]:
        if not r["project"] or not _project_id(conn, r["project"]):
            return note + f": pipeline {r['pipeline']} names no project for the card; a person runs {_approve_cmd(r)}"
        who = r["gate_rule"] or "a person"
        item = core.item_add(
            conn, r["project"], f"Approve pipeline {r['pipeline']} stage {r['stage']}: version {(r['rev'] or '')[:12]}",
            doer="human", actor="maxpm", approve_once=True,
            context=(f"Pipeline {r['pipeline']}: version {r['rev']} passed the stages before {r['stage']} and waits "
                     f"at its gate for {who}'s yes (run #{r['id']}).\nApprove: mark this item done (only a person); "
                     f"maxpm serve then starts stage {r['stage']}. Decline: drop it; the version stops here."))
        with core.tx(conn):
            conn.execute("UPDATE stage_runs SET gate_item=? WHERE id=?", (item["id"], r["id"]))
        return f"waits for {who}'s yes on #{item['id']}"
    return note


def approve(conn, pipeline, stage, rev=None, actor=None):
    """maxpm pipeline approve: let a queued version into a stage now. A person gate takes a person (the one it
    names); any other gate takes a person or the manager."""
    p = store._pipeline(conn, pipeline)
    q = ("SELECT r.id FROM stage_runs r JOIN pipeline_stages s ON s.id=r.stage_id LEFT JOIN pipeline_versions v "
         "ON v.id=r.version_id WHERE r.pipeline_id=? AND s.name=? AND r.state='queued'")
    args = [p["id"], stage]
    if rev:
        q, args = q + " AND (v.rev=? OR substr(v.rev,1,length(?))=?)", args + [rev, rev, rev]
    rows = conn.execute(q + " ORDER BY r.id DESC", args).fetchall()
    if not rows:
        raise core.RiverError(f"no version waits at stage {stage} of pipeline {p['name']}"
                              + (f" for {rev}" if rev else ""))
    if len(rows) > 1 and not rev:
        raise core.RiverError(f"{len(rows)} versions wait at stage {stage}: name one with --rev <commit>")
    r = _row(conn, rows[0]["id"])
    if r["gate"] == "person":
        if actor is not None and not core._is_person(conn, actor):
            raise core.RiverError(f"the gate of stage {stage} takes a person's yes; {actor} is not a person")
        if r["gate_rule"] and actor is not None and actor != r["gate_rule"]:
            raise core.RiverError(f"the gate of stage {stage} takes {r['gate_rule']}'s yes")
    else:
        why = core._lead_refusal(conn, actor, "lets a version through a gate")
        if why:
            raise core.RiverError(why)
    with core.tx(conn):
        conn.execute("UPDATE stage_runs SET approved_by=?, approved_at=?, gate_note=NULL WHERE id=?",
                     (actor or "a person", store._now(), r["id"]))
        core._event(conn, None, actor or "maxpm", f"pipeline {p['name']}: approved version {r['rev']} into stage "
                                                  f"{stage} (run #{r['id']})")
    if r["gate_item"]:
        it = conn.execute("SELECT status FROM items WHERE id=?", (r["gate_item"],)).fetchone()
        if it and it["status"] not in core.CLOSED_STATES:
            core.drop(conn, r["gate_item"], actor, note=f"approved with maxpm pipeline approve (run #{r['id']})")
    return {"pipeline": p["name"], "stage": stage, "version": r["rev"], "run": r["id"], "approved_by": actor}


# ---------------------------------------------------------------- running a stage

def _command(cmd, folder, limit, env, on_lines):
    """Run cmd in its own process group; give its output lines to on_lines as they come (every FLUSH_EVERY seconds).
    Returns (exit code, timed out)."""
    p = subprocess.Popen(cmd, shell=True, cwd=folder, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, errors="replace", start_new_session=True)
    lines = queue.Queue()

    def read():
        try:
            for line in p.stdout:
                lines.put(line.rstrip("\n"))
        except (OSError, ValueError):
            pass  # the pipe closed after a stop at stage_timeout
        finally:
            lines.put(None)
    threading.Thread(target=read, daemon=True, name="maxpm-stage-output").start()
    end = time.monotonic() + limit if limit > 0 else None
    buf, last, timed_out, done, stopped_at = [], time.monotonic(), False, False, None
    while not done:
        try:
            line = lines.get(timeout=0.2)
        except queue.Empty:
            line = ""
            got = False
        else:
            got = True
        if got and line is None:
            done = True
        elif got:
            buf.append(line)
        if buf and (done or len(buf) >= FLUSH_LINES or time.monotonic() - last >= FLUSH_EVERY):
            on_lines(buf)
            buf, last = [], time.monotonic()
        if end is not None and not timed_out and time.monotonic() > end:
            timed_out, stopped_at = True, time.monotonic()
            core._stop_group(p)
        if stopped_at is not None and time.monotonic() - stopped_at > 15:
            break  # a process outside the group holds the pipe open
    try:
        code = p.wait(timeout=15)
    except subprocess.TimeoutExpired:
        code = None
    finally:
        try:
            p.stdout.close()
        except OSError:
            pass
    return code, timed_out


def _execute(conn, run_id):
    """Run one stage run to its end and hand the version on. Never an error: the run records what happened."""
    r = _row(conn, run_id)
    with core.tx(conn):
        conn.execute("UPDATE stage_runs SET runner=?, gate_note=NULL WHERE id=?", (core.OPS_PROCESS, run_id))
    store.ingest(conn, cdevents.make("pipelinerun", "started", r["key"], {"pipelineName": r["stage"]},
                                     {"pipeline": r["pipeline"], "run": run_id, "where": "local"}))
    state, errors = "passed", None
    if r["cmd"]:
        folder = _folder(conn, r["project"])
        env_name = None
        if r["env_id"]:
            e = conn.execute("SELECT name FROM pipeline_envs WHERE id=?", (r["env_id"],)).fetchone()
            env_name = e["name"] if e else None
        limit_text = core.setting(conn, "stage_timeout", project_id=_project_id(conn, r["project"]))
        limit = core.parse_duration(limit_text).total_seconds()
        env = {**os.environ, "MAXPM_PIPELINE": r["pipeline"], "MAXPM_STAGE": r["stage"], "MAXPM_REV": r["rev"] or "",
               "MAXPM_RUN": str(run_id), "MAXPM_ENV": env_name or ""}
        began = time.time()
        try:
            if folder and not os.path.isdir(folder):
                raise OSError(f"the folder {folder} does not exist")
            code, timed_out = _command(r["cmd"], folder, limit, env,
                                       lambda lines: steps.ingest_output(conn, run_id, lines))
        except OSError as e:
            code, timed_out = None, False
            errors = f"the command did not start: {e}"
        if timed_out:
            state, errors = "failed", f"stopped at stage_timeout ({limit_text})"
        elif code != 0:
            state, errors = "failed", errors or f"exit {code}"
        if r["junit"]:
            path = os.path.join(folder or "", os.path.expanduser(r["junit"]))
            try:
                fresh = os.path.getmtime(path) >= began - 1
            except OSError:
                fresh = False
            if not fresh:
                state, errors = "failed", (errors + "; " if errors else "") + f"no new JUnit report at {path}"
            else:
                try:
                    n = junit.ingest_junit(conn, run_id, path=path)
                    if (n["failed"] or n["error"]) and state == "passed":
                        state, errors = "failed", f"{n['failed'] + n['error']} of {n['tests']} tests failed"
                except core.RiverError as e:
                    state, errors = "failed", (errors + "; " if errors else "") + str(e)
    store.run_finish(conn, run_id, state, errors=errors)
    if state == "passed":
        _hand_on(conn, _row(conn, run_id))
    return state


def _hand_on(conn, r):
    """A stage passed: its environment runs the version now (a deploy stage), and the next stage gets it."""
    if r["kind"] == "deploy" and r["env_id"]:
        e = conn.execute("SELECT name FROM pipeline_envs WHERE id=?", (r["env_id"],)).fetchone()
        if e:
            store.deployed(conn, r["pipeline"], e["name"], r["rev"], run_id=r["id"])
    nxt = conn.execute("SELECT name FROM pipeline_stages WHERE pipeline_id=? AND removed_at IS NULL AND pos>? "
                       "ORDER BY pos, id LIMIT 1", (r["pipeline_id"], r["pos"])).fetchone()
    if nxt:
        _queue(conn, r["pipeline"], nxt["name"], r["rev"])


def _launch(run_id):
    """Run a stage run in a thread of its own with its own connection (SYNC: the caller runs it)."""
    def work():
        try:
            c = core.connect()
            try:
                _execute(c, run_id)
            finally:
                c.close()
        except Exception as e:  # noqa: BLE001 - the next pass ends the run as lost
            print(f"pipeline run #{run_id}: {type(e).__name__}: {e}", flush=True)
        finally:
            with LOCK:
                RUNNING.pop(run_id, None)
    with LOCK:
        RUNNING[run_id] = True
    threading.Thread(target=work, daemon=True, name=f"maxpm-stage-{run_id}").start()


def running_text():
    """Why maxpm serve may not start again now (server.restart_blocked), or None."""
    with LOCK:
        n = len(RUNNING)
    return f"{n} pipeline stage(s) run" if n else None


def _lost(conn):
    """A run that maxpm serve started and no thread of this process runs any more ended with serve: it failed."""
    with LOCK:
        mine = set(RUNNING)
    for r in conn.execute("SELECT id FROM stage_runs WHERE state='running' AND runner IS NOT NULL").fetchall():
        if r["id"] not in mine:
            store.run_finish(conn, r["id"], "failed", errors="maxpm serve stopped while it ran")


def serve_pass(conn):
    """One pass of the loop of maxpm serve: end lost runs, open the gates that may open, start what may start.
    Returns {"started": [run ids], "waiting": {run id: note}}."""
    _lost(conn)
    started, waiting = [], {}
    rows = conn.execute("SELECT r.id FROM stage_runs r JOIN pipeline_stages s ON s.id=r.stage_id WHERE "
                        "r.state='queued' AND s.removed_at IS NULL ORDER BY r.id").fetchall()
    for row in rows:
        r = _row(conn, row["id"])
        if r["workflow"] and not r["cmd"]:
            continue  # a forge runs it: its adapter reports the run
        busy = conn.execute("SELECT id FROM stage_runs WHERE stage_id=? AND state='running'", (r["stage_id"],)).fetchone()
        if busy or any(_row(conn, s)["stage_id"] == r["stage_id"] for s in started):
            open_, note = False, f"waits for run #{busy['id'] if busy else started[-1]} of stage {r['stage']}"
        else:
            open_, note = gate(conn, r)
        if note == "declined":
            store.run_finish(conn, r["id"], "cancelled", errors=f"the yes at the gate was declined (#{r['gate_item']})")
            continue
        if not open_:
            note = _ask(conn, r, note)
            if note != r["gate_note"]:
                with core.tx(conn):
                    conn.execute("UPDATE stage_runs SET gate_note=? WHERE id=?", (note, r["id"]))
            waiting[r["id"]] = note
            continue
        if note:  # approved through the card
            with core.tx(conn):
                conn.execute("UPDATE stage_runs SET approved_by=?, approved_at=? WHERE id=?",
                             (core._done_by(conn, r["gate_item"]), store._now(), r["id"]))
        started.append(r["id"])
        if SYNC["on"]:
            _execute(conn, r["id"])
        else:
            _launch(r["id"])
    return {"started": started, "waiting": waiting}
