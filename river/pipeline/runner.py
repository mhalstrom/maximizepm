"""The no-forge runner (#6114): maxpm serve runs the stages of a pipeline on this computer, in order.

maxpm pipeline start <name> [--rev <commit>] queues the version at the first stage. Each pass of the loop of maxpm
serve (serve_pass) then:

- ends as failed a run that this process no longer runs (serve stopped while it ran);
- for each queued run, asks the transition into its stage (gate, transitions.py #6139): its controls (disabled, a
  block, the fault rule, a freeze, the window, a delay) hold it; its mode decides the rest: auto opens at once; a
  delay of 30m opens 30 minutes after the stage before passed, while no check of that stage's environment (its
  target) or of the project failed since; the manager's yes and the release wait for maxpm pipeline approve (the
  release with a cadence also opens by itself); a person's yes asks with a person's approval item (the card of
  release_approval person) and opens when a person marks it done, and cancels the run when it is dropped;
- starts each run that may enter, one at a time for each stage, in a thread of its own.

A stage run runs the stage's command in the project's folder, in its own process group, with MAXPM_PIPELINE,
MAXPM_STAGE, MAXPM_REV, MAXPM_RUN and MAXPM_ENV set, for at most stage_timeout. Its output goes to the run as it
comes, and its '::step' lines become steps (steps.ingest_output). A test stage that names a JUnit file
(maxpm pipeline stage ... --junit <path>) has the file read after its command. A stage that passes hands the version
to the next stage (and a deploy stage records it in its environment); a stage that fails stops the pipeline for
that version. A stage with no command (a review or an approval only) passes when it may enter. A stage with a
forge workflow and no command is left to that forge's adapter.

No session types a stage's command: serve runs it, outside every sandbox, as it runs a target's hooks.
"""

import json
import os
import queue
import subprocess
import threading
import time
import uuid

from .. import core
from . import audit, cdevents, junit, steps, store, transitions

FORGES = {"github": "GitHub", "gitlab": "GitLab", "gitea": "Gitea", "forgejo": "Forgejo",
          "pulumi": "Pulumi Delivery"}  # the forges with an adapter (Pulumi Delivery: #6133)
_ADAPTERS = {"gitea": "github", "forgejo": "github"}  # Gitea and Forgejo have GitHub's Actions API (#6122)


def _adapter(forge):
    """The module of a forge's adapter: river/pipeline/<forge>.py."""
    import importlib
    return importlib.import_module(f"{__package__}.{_ADAPTERS.get(forge, forge)}")


RUNNING = {}  # run id -> the thread of this process that runs it; server.restart_blocked waits for them
LOCK = threading.Lock()
SYNC = {"on": False}  # tests: the pass runs a stage itself, not in a thread
FLUSH_EVERY = 1.0  # seconds between two writes of a stage's new output lines
FLUSH_LINES = 200


def _row(conn, run_id):
    return conn.execute(
        "SELECT r.*, s.name stage, s.kind, s.run cmd, s.workflow, s.junit, s.pos, s.env_id, "
        "p.held_reason pipe_hold, p.held_by pipe_hold_by, s.waves, s.check_cmd, s.rollback_cmd, s.wave_wait, "
        "p.name pipeline, p.project, p.forge, v.rev FROM stage_runs r JOIN pipeline_stages s ON s.id=r.stage_id "
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
        _act(conn, p["id"], actor, f"started version {rev} at stage {first['name']} (run #{run})", "start",
             f"stage {first['name']}", rev, run)
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


def _mode(conn, r, t):
    """(open, note) of the transition's mode alone (transitions.py): auto opens; forge never (the forge starts it);
    a yes, the release or a delay opens with an approval of the run; a delay also opens by itself after its time while
    the checks pass; the release with a cadence after that time since the last version entered; a person's yes
    opens when that person marks the approval item done. note 'declined': the person dropped the item."""
    mode, arg = t["mode"], t["mode_arg"]
    if mode == "auto":
        return True, None
    if mode == "forge":  # #6119: a push or a tag starts it on the forge; the adapter links its run to this one
        return False, "the forge starts it by itself (a push or a tag); MaximizePM reads its run"
    if r["approved_at"]:
        return True, None
    if mode == "delay":
        prev = _previous(conn, r)
        since = (prev["ended_at"] if prev else None) or r["queued_at"] or store._now()
        bad = _failed_check(conn, r, prev, since)
        if bad:
            return False, (f"check {bad} failed since {since}: the transition (a delay of {arg}) stays closed; a "
                           f"person or the manager lets the version in: {_approve_cmd(r)}")
        opens = core.parse_iso(since) + core.parse_duration(arg)
        if core.now() < opens:
            return False, f"opens at {core.iso(opens)} (a delay of {arg}) while the checks pass"
        return True, None
    if mode == "release":
        if arg:
            last = conn.execute("SELECT MAX(started_at) FROM stage_runs WHERE stage_id=? AND purpose IS NULL AND "
                                "started_at IS NOT NULL", (r["stage_id"],)).fetchone()[0]
            due = core.parse_iso(last) + core.parse_duration(arg) if last else None
            if due is None or core.now() >= due:
                return True, None
            return False, (f"waits for the release: the cadence lets the next version in at {core.iso(due)}, or the "
                           f"manager or a person releases it now: {_approve_cmd(r)}")
        return False, f"waits for the release: the manager or a person releases it: {_approve_cmd(r)}"
    if arg == "manager":
        return False, f"waits for the manager: {_approve_cmd(r)}"
    who = arg or "a person"  # a person's yes
    if r["gate_item"]:
        it = conn.execute("SELECT status FROM items WHERE id=?", (r["gate_item"],)).fetchone()
        if it and it["status"] == "done":
            by = core._done_by(conn, r["gate_item"])
            if core._is_person(conn, by) and (not arg or by == arg):
                return True, f"approved by {by} (#{r['gate_item']})"
        if it is None or it["status"] == "dropped":
            return False, "declined"
    return False, f"waits for {who}'s yes" + (f" on #{r['gate_item']}" if r["gate_item"] else "")


def gate(conn, r, ask=True):
    """(open, note): whether the queued run r may enter its stage now, and why not. A hold of the whole pipeline
    stops every start. Then the transition into the stage (#6139): its controls (disabled, a block, the fault rule,
    a freeze, the window, a delay) hold every version but one let past them (maxpm pipeline pass, a run again, a roll
    back); its mode decides the rest. ask: ask the manager or a person for the yes the mode needs, once, unless the
    transition is disabled, blocked or frozen (then nobody is asked)."""
    if r["pipe_hold"]:
        return False, f"every stage is held by {r['pipe_hold_by'] or 'a person'}: {r['pipe_hold']}"
    t = transitions.get(conn, r["stage_id"])
    c = None if r["passed_at"] else transitions.closed(
        conn, t, {"id": r["pipeline_id"], "name": r["pipeline"], "project": r["project"]})
    ok, note = _mode(conn, r, t)
    if note == "declined":
        return False, note
    if c is None:
        return (True, note) if ok else (False, _ask(conn, r, t, note) if ask else note)
    if ok:
        said = f"; {r['approved_by']} said yes" if r["approved_at"] and t["mode"] not in ("auto", "forge") else ""
        return False, c["text"] + said
    if ask and c["kind"] in ("window", "delayed"):
        note = _ask(conn, r, t, note)
    return False, f"{c['text']}; it {note}" if note.startswith("waits") else f"{c['text']}; {note}"


def _ask(conn, r, t, note):
    """Ask once for the yes of the mode: an alert to the active manager (the manager's yes, the release), or a
    person's approval item in the pipeline's project (a person's yes). Returns the note to keep on the run."""
    if t["mode"] == "release" or (t["mode"] == "yes" and t["mode_arg"] == "manager"):
        if r["gate_note"] and "(asked " in r["gate_note"]:
            return note + r["gate_note"][r["gate_note"].index(" (asked "):]
        mgr = core.active_manager(conn)
        if not mgr:
            return note + " (no manager runs now: asked when one starts)"
        what = "the release" if t["mode"] == "release" else "the manager's yes"
        with core.tx(conn):
            core._send(conn, "alert", "maxpm", f"pipeline {r['pipeline']}: version {(r['rev'] or '')[:12]} waits at "
                       f"the transition into stage {r['stage']} for {what}. Let it in: {_approve_cmd(r)}", to=mgr)
        return note + f" (asked {mgr})"
    if t["mode"] == "yes" and not r["gate_item"]:
        if not r["project"] or not _project_id(conn, r["project"]):
            return note + f": pipeline {r['pipeline']} names no project for the card; a person runs {_approve_cmd(r)}"
        who = t["mode_arg"] or "a person"
        item = core.item_add(
            conn, r["project"], f"Approve pipeline {r['pipeline']} stage {r['stage']}: version {(r['rev'] or '')[:12]}",
            doer="human", actor="maxpm", approve_once=True,
            context=(f"Pipeline {r['pipeline']}: version {r['rev']} passed the stages before {r['stage']} and waits "
                     f"at the transition into it for {who}'s yes (run #{r['id']}).\nApprove: mark this item done (only "
                     f"a person); maxpm serve then starts stage {r['stage']} when the transition's controls let it "
                     f"(a block, a window). Decline: drop it; the version stops here."))
        with core.tx(conn):
            conn.execute("UPDATE stage_runs SET gate_item=? WHERE id=?", (item["id"], r["id"]))
        return f"waits for {who}'s yes on #{item['id']}"
    return note


def _waiting_run(conn, p, stage, rev=None):
    """The version that waits at the transition into a stage (the newest one; rev names one when several wait)."""
    q = ("SELECT r.id FROM stage_runs r JOIN pipeline_stages s ON s.id=r.stage_id LEFT JOIN pipeline_versions v "
         "ON v.id=r.version_id WHERE r.pipeline_id=? AND s.name=? AND r.state='queued' AND s.removed_at IS NULL")
    args = [p["id"], stage]
    if rev:
        q, args = q + " AND (v.rev=? OR substr(v.rev,1,length(?))=?)", args + [rev, rev, rev]
    rows = conn.execute(q + " ORDER BY r.id DESC", args).fetchall()
    if not rows:
        raise core.RiverError(f"no version waits at stage {stage} of pipeline {p['name']}"
                              + (f" for {rev}" if rev else ""))
    if len(rows) > 1 and not rev:
        raise core.RiverError(f"{len(rows)} versions wait at stage {stage}: name one with --rev <commit>")
    return _row(conn, rows[0]["id"])


def _yes_refusal(conn, t, stage, actor):
    """Why actor may not give the yes of the transition into stage, or None. A person's yes takes a person (the one
    it names); the manager's yes, the release and a delay take a person or the manager."""
    if t["mode"] == "yes" and t["mode_arg"] != "manager":
        if actor is not None and not core._is_person(conn, actor):
            return f"the transition into stage {stage} takes a person's yes; {actor} is not a person"
        if t["mode_arg"] and actor is not None and actor != t["mode_arg"]:
            return f"the transition into stage {stage} takes {t['mode_arg']}'s yes"
        return None
    return core._lead_refusal(conn, actor, "lets a version through a transition")


def _may_say_yes(conn, t, actor):
    return _yes_refusal(conn, t, "", actor) is None


def approve(conn, pipeline, stage, rev=None, actor=None):
    """maxpm pipeline approve: give the yes of the transition into a stage for the version that waits there (a
    person's yes, the manager's yes, the release, or a delay cut short). Its controls (a block, a window) still hold
    the version; maxpm pipeline pass lets it past them."""
    p = store._pipeline(conn, pipeline)
    r = _waiting_run(conn, p, stage, rev)
    t = transitions.get(conn, r["stage_id"])
    if t["mode"] in ("auto", "forge"):
        raise core.RiverError(f"the transition into stage {stage} is {transitions.mode_text(t['mode'], t['mode_arg'])}"
                              f": it takes no yes. To let {(r['rev'] or '')[:12]} past its controls: maxpm pipeline "
                              f"pass {p['name']} {stage} --rev {(r['rev'] or '')[:12]} --reason \"<why>\"")
    why = _yes_refusal(conn, t, stage, actor)
    if why:
        raise core.RiverError(why)
    with core.tx(conn):
        conn.execute("UPDATE stage_runs SET approved_by=?, approved_at=?, gate_note=NULL WHERE id=?",
                     (actor or "a person", store._now(), r["id"]))
        _act(conn, p["id"], actor, f"approved version {r['rev']} into stage {stage} (run #{r['id']})", "approve",
             f"the transition into {stage}", r["rev"], r["id"])
    if r["gate_item"]:
        it = conn.execute("SELECT status FROM items WHERE id=?", (r["gate_item"],)).fetchone()
        if it and it["status"] not in core.CLOSED_STATES:
            core.drop(conn, r["gate_item"], actor, note=f"approved with maxpm pipeline approve (run #{r['id']})")
    return {"pipeline": p["name"], "stage": stage, "version": r["rev"], "run": r["id"], "approved_by": actor}


# ---------------------------------------------------------------- running a stage

def _command(cmd, folder, limit, env, on_lines, run_id=None):
    """Run cmd in its own process group; give its output lines to on_lines as they come (every FLUSH_EVERY seconds).
    Returns (exit code, timed out)."""
    p = subprocess.Popen(cmd, shell=True, cwd=folder, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, errors="replace", start_new_session=True)
    if run_id is not None:
        PROCS[run_id] = p  # maxpm pipeline stop finds it
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
        if (r["purpose"] or "").startswith("failed:"):  # only the failed tests of that run: one per line
            env["MAXPM_TESTS"] = "\n".join(f"{t['suite']}.{t['name']}" for t in store.tests(
                conn, int(r["purpose"].split(":", 1)[1])) if t["state"] in ("failed", "error"))
        began = time.time()
        if r["waves"]:
            state, errors = _waves(conn, r, folder, limit, limit_text, env)
        else:
            state, errors = _single(conn, r, r["cmd"], folder, limit, limit_text, env)
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


def _single(conn, r, cmd, folder, limit, limit_text, env, on_lines=None):
    """Run one command of a stage run to its end: ("passed", None) or ("failed", why)."""
    run_id = r["id"]
    try:
        if folder and not os.path.isdir(folder):
            raise OSError(f"the folder {folder} does not exist")
        try:
            code, timed_out = _command(cmd, folder, limit, env, on_lines or (lambda lines: steps.ingest_output(
                conn, run_id, lines)), run_id)
        finally:
            PROCS.pop(run_id, None)
    except OSError as e:
        return "failed", f"the command did not start: {e}"
    if timed_out:
        return "failed", f"stopped at stage_timeout ({limit_text})"
    if code != 0:
        return "failed", f"exit {code}"
    return "passed", None


def _waves(conn, r, folder, limit, limit_text, env):
    """A stage over many servers (#6120): for each wave, each server's deploy (the stage's command with MAXPM_HOST)
    and its check, as steps with where=<host>; after a wave, wave_wait, then its checks again. A failed deploy or
    check stops the rollout: the servers of that wave go back to the version before (the roll back command), and
    the next waves do not start. Returns (state, errors)."""
    waves = json.loads(r["waves"])
    prev = None
    if r["env_id"]:
        v = conn.execute("SELECT v.rev FROM pipeline_envs e JOIN pipeline_versions v ON v.id=e.version_id WHERE e.id=?",
                         (r["env_id"],)).fetchone()
        prev = v["rev"] if v else None
    run_id = r["id"]

    def step(name, host, kind, cmd):
        steps.ingest_output(conn, run_id, [f"::step start {name} kind={kind} where={host}"])
        st, why = _single(conn, r, cmd, folder, limit, limit_text,
                          {**env, "MAXPM_HOST": host, "MAXPM_PREV_REV": prev or "", "MAXPM_WAVE": str(n)})
        steps.ingest_output(conn, run_id, [f"::step end {name} where={host} {st}" + (f" {why}" if why else "")])
        return st == "passed", why

    for n, wave in enumerate(waves, 1):
        done, bad = [], None
        for host in wave:
            ok, why = step("deploy", host, "deploy", r["cmd"])
            done.append(host)
            if not ok:
                bad = f"wave {n}: {host} did not deploy ({why})"
                break
            if r["check_cmd"]:
                ok, why = step("check", host, "check", r["check_cmd"])
                if not ok:
                    bad = f"wave {n}: {host} failed its check ({why})"
                    break
        if bad is None and r["wave_wait"] and n < len(waves):
            steps.ingest_output(conn, run_id, [f"::step start wait kind=wait where=wave-{n}"])
            time.sleep(core.parse_duration(r["wave_wait"]).total_seconds())  # timing: the stage's own wait
            steps.ingest_output(conn, run_id, [f"::step end wait where=wave-{n} passed"])
            for host in wave if r["check_cmd"] else []:
                ok, why = step("check", host, "check", r["check_cmd"])
                if not ok:
                    bad = f"wave {n}: {host} failed its check after {r['wave_wait']} ({why})"
                    break
        if bad:
            back = []
            if r["rollback_cmd"]:
                for host in done:
                    ok, _why = step("rollback", host, "deploy", r["rollback_cmd"])
                    back.append(host + ("" if ok else " (failed)"))
            left = sum(len(w) for w in waves[n:])
            return "failed", bad + (f"; rolled back {', '.join(back)}" if back else "") + (
                f"; {left} server(s) of later waves not started" if left else "")
    return "passed", None


def _hand_on(conn, r):
    """A stage passed: its environment runs the version now (a deploy stage), and the next stage gets it."""
    if r["kind"] == "deploy" and r["env_id"]:
        e = conn.execute("SELECT name FROM pipeline_envs WHERE id=?", (r["env_id"],)).fetchone()
        if e:
            store.deployed(conn, r["pipeline"], e["name"], r["rev"],
                           run_id=r["id"], kind="rolledback" if r["purpose"] == "rollback" else "deployed")
    if r["purpose"] == "rollback" or (r["purpose"] or "").startswith("failed:"):
        return  # a roll back or a run of some tests hands nothing on: a person decides what comes next
    nxt = conn.execute("SELECT id, name FROM pipeline_stages WHERE pipeline_id=? AND removed_at IS NULL AND pos>? "
                       "ORDER BY pos, id LIMIT 1", (r["pipeline_id"], r["pos"])).fetchone()
    if nxt:
        # A newer version replaces an older one that still waits at that gate (#6119): the person approves the
        # newest, and a stage never runs a version that a newer one passed by.
        for old in conn.execute("SELECT id, gate_item FROM stage_runs WHERE stage_id=? AND state='queued' AND "
                                "approved_at IS NULL AND passed_at IS NULL AND runner IS NULL AND version_id IS NOT ?",
                                (nxt["id"], r["version_id"])).fetchall():
            store.run_finish(conn, old["id"], "cancelled", errors=f"a newer version came: {r['rev']}")
            _drop_card(conn, old["gate_item"], f"a newer version came to stage {nxt['name']}: {r['rev']}")
        if conn.execute("SELECT 1 FROM stage_runs WHERE stage_id=? AND version_id IS ? AND (state IN ('queued','running') "
                        "OR key LIKE 'gl-%' OR key LIKE 'gh-run-%' OR key LIKE 'pd-%' OR source=?)",
                        (nxt["id"], r["version_id"], r["forge"] if r["forge"] and r["forge"] not in FORGES
                         else "\0")).fetchone():
            return  # it waits or ran there already (a forge made the run: GitLab's jobs of that stage, #6121; the
            # stage in the same Pulumi Delivery run, #6133; a plugin's system ran it, #6159)
        _queue(conn, r["pipeline"], nxt["name"], r["rev"])


def hand_on(conn, run_id):
    """A stage run passed (here, or on a forge): its version goes on (_hand_on)."""
    _hand_on(conn, _row(conn, run_id))


def _drop_card(conn, item_id, note):
    if item_id:
        it = conn.execute("SELECT status FROM items WHERE id=?", (item_id,)).fetchone()
        if it and it["status"] not in core.CLOSED_STATES:
            core.drop(conn, item_id, "maxpm", note=note)


def _lead(conn, actor, what):
    why = core._lead_refusal(conn, actor, what)
    if why:
        raise core.RiverError(why)


def _act(conn, pid, actor, text, action, place=None, rev=None, run=None, reason=None, kind=None):
    """Each action is a line of the pipeline's audit log with its actor (inside a tx; maxpm pipeline audit, #6144)."""
    audit.write(conn, pid, actor, action, text, kind=kind, place=place, rev=rev, run_id=run, reason=reason)


def _stage_row(conn, p, stage):
    s = conn.execute("SELECT * FROM pipeline_stages WHERE pipeline_id=? AND name=? AND removed_at IS NULL",
                     (p["id"], stage)).fetchone()
    if not s:
        raise core.RiverError(f"pipeline {p['name']} has no stage {stage}")
    return s


def run_again(conn, pipeline, stage, rev, actor=None, purpose="rerun", note=None, reason=None):
    """maxpm pipeline rerun: run one stage again for a version, an older one too. The person or the manager sends
    it on purpose, so it passes the transition before the stage (its yes and its controls); a hold of the whole
    pipeline still stops it."""
    _lead(conn, actor, "runs a stage again")
    p = store._pipeline(conn, pipeline)
    from . import plugin
    plugin.need(conn, p, _stage_row(conn, p, stage), plugin.action_for(purpose))  # a plugin may not list it (#6159)
    run = _queue(conn, p["name"], stage, rev)
    with core.tx(conn):
        conn.execute("UPDATE stage_runs SET approved_by=?, approved_at=?, passed_by=?, passed_at=?, purpose=? "
                     "WHERE id=?", (actor or "a person", store._now(), actor or "a person", store._now(), purpose, run))
        _act(conn, p["id"], actor, note or f"run stage {stage} again for {rev} (run #{run})",
             "rerun-failed" if purpose.startswith("failed:") else purpose, f"stage {stage}", rev, run, reason)
    return {"pipeline": p["name"], "stage": stage, "version": rev, "run": run}


def hold(conn, pipeline, stage=None, reason=None, actor=None):
    """maxpm pipeline hold: with no stage, hold every stage (no stage run starts; work that runs goes on to its end).
    With a stage, block the transition into it (transitions.block, until someone unblocks it). Needs the reason."""
    if stage:
        transitions.block(conn, pipeline, stage, reason, actor=actor)
        return {"pipeline": store._pipeline(conn, pipeline)["name"], "stage": stage, "held": reason.strip()}
    _lead(conn, actor, "holds a pipeline")
    reason = (reason or "").strip()
    if not reason:
        raise core.RiverError("say why: --reason \"<why>\" (the people who look at the pipeline read it)")
    p = store._pipeline(conn, pipeline)
    with core.tx(conn):
        conn.execute("UPDATE pipelines SET held_reason=?, held_by=?, held_at=? WHERE id=?",
                     (reason, actor, store._now(), p["id"]))
        _act(conn, p["id"], actor, f"held every stage: {reason}", "hold", "every stage", reason=reason)
    return {"pipeline": p["name"], "stage": None, "held": reason}


def release_hold(conn, pipeline, stage=None, actor=None):
    """maxpm pipeline unhold: end the hold of every stage, or with a stage the block of the transition into it."""
    if stage:
        transitions.unblock(conn, pipeline, stage, actor=actor)
        return {"pipeline": store._pipeline(conn, pipeline)["name"], "stage": stage, "held": None}
    _lead(conn, actor, "ends a hold of a pipeline")
    p = store._pipeline(conn, pipeline)
    with core.tx(conn):
        if not p["held_reason"]:
            raise core.RiverError(f"pipeline {p['name']} is not held")
        conn.execute("UPDATE pipelines SET held_reason=NULL, held_by=NULL, held_at=NULL WHERE id=?", (p["id"],))
        _act(conn, p["id"], actor, "ended the hold of every stage", "unhold", "every stage")
    return {"pipeline": p["name"], "stage": None, "held": None}


def rollback(conn, pipeline, env, rev, reason=None, actor=None):
    """maxpm pipeline rollback: put an older version on an environment again: the deploy stage of that environment
    runs with it, and the transition into it is blocked, so the flow brings no newer version there until a person
    unblocks it."""
    _lead(conn, actor, "rolls back an environment")
    p = store._pipeline(conn, pipeline)
    s = conn.execute("SELECT s.name FROM pipeline_stages s JOIN pipeline_envs e ON e.id=s.env_id WHERE "
                     "s.pipeline_id=? AND e.name=? AND s.removed_at IS NULL ORDER BY s.pos LIMIT 1",
                     (p["id"], env)).fetchone()
    if not s:
        raise core.RiverError(f"no stage of pipeline {p['name']} deploys to {env}")
    from . import plugin
    plugin.need(conn, p, _stage_row(conn, p, s["name"]), "rollback")  # before the block (#6159)
    why = f"rolled back {env} to {rev}" + (f": {reason}" if reason else "")
    transitions.block(conn, p["name"], s["name"], why, actor=actor)
    out = run_again(conn, p["name"], s["name"], rev, actor, purpose="rollback", note=why, reason=reason)
    return {**out, "env": env, "held": why}


def rerun_failed(conn, run_id, actor=None):
    """maxpm pipeline rerun-failed: run a test stage again for the same version, for its failed tests only: the
    command gets them in MAXPM_TESTS, one suite.name per line (a forge workflow runs whole)."""
    _lead(conn, actor, "runs failed tests again")
    r = _row(conn, run_id)
    if r is None:
        raise core.RiverError(f"no stage run {run_id}")
    bad = [t for t in store.tests(conn, run_id) if t["state"] in ("failed", "error")]
    if not bad:
        raise core.RiverError(f"run #{run_id} has no failed test")
    return run_again(conn, r["pipeline"], r["stage"], r["rev"], actor, purpose=f"failed:{run_id}",
                     note=f"run the {len(bad)} failed test(s) of run #{run_id} again for {r['rev']}")


PROCS = {}  # run id -> the Popen of its command, in this process (stop)


def stop(conn, run_id, actor=None):
    """maxpm pipeline stop: stop a stage run: its command here, its run on the forge, or the wait at its gate."""
    why = core._lead_refusal(conn, actor, "stops a stage run")
    if why:
        raise core.RiverError(why)
    r = _row(conn, run_id)
    if r is None:
        raise core.RiverError(f"no stage run {run_id}")
    if r["state"] in store.FINAL:
        raise core.RiverError(f"run #{run_id} is {r['state']} already")
    from . import plugin
    if plugin.plugin_run(conn, r):  # a plugin's run: the plugin stops it, when it lists stop (#6159)
        p = conn.execute("SELECT * FROM pipelines WHERE id=?", (r["pipeline_id"],)).fetchone()
        plugin.need(conn, p, {"workflow": r["workflow"], "run": r["cmd"]}, "stop")
        with core.tx(conn):
            _act(conn, r["pipeline_id"], actor, f"stopped run #{run_id} of stage {r['stage']} ({r['state']})", "stop",
                 f"stage {r['stage']}", r["rev"], run_id)
        plugin.act(conn, run_id, "stop")  # the next poll records its end
        return {"run": run_id, "stopped": f"by plugin {r['forge']}"}
    with core.tx(conn):
        _act(conn, r["pipeline_id"], actor, f"stopped run #{run_id} of stage {r['stage']} ({r['state']})", "stop",
             f"stage {r['stage']}", r["rev"], run_id)
    forge = (r["runner"] or "")[6:] if (r["runner"] or "").startswith("forge:") else \
        r["forge"] if (r["key"] or "").startswith(("gh-run-", "gl-", "pd-")) else None
    if forge in FORGES:
        _adapter(forge).cancel(conn, run_id)  # the next read of the forge records its end
        return {"run": run_id, "stopped": f"on {FORGES[forge]}"}
    p = PROCS.get(run_id)
    if p is not None:
        core._stop_group(p)  # the run's thread records the end
        return {"run": run_id, "stopped": "its command"}
    store.run_finish(conn, run_id, "cancelled", errors=f"stopped by {actor or 'a person'}")
    _drop_card(conn, r["gate_item"], f"run #{run_id} was stopped")
    return {"run": run_id, "stopped": "the wait at its gate" if r["state"] == "queued" else "its record"}


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
    for r in conn.execute("SELECT id FROM stage_runs WHERE state='running' AND runner IS NOT NULL AND runner NOT LIKE "
                          "'forge:%'").fetchall():
        if r["id"] not in mine:
            store.run_finish(conn, r["id"], "failed", errors="maxpm serve stopped while it ran")


def serve_pass(conn):
    """One pass of the loop of maxpm serve: end lost runs, open the gates that may open, start what may start.
    Returns {"started": [run ids], "waiting": {run id: note}}."""
    _lost(conn)
    transitions.expire(conn)  # blocks, delays and freezes whose end came (#6139)
    from . import github
    github.poll_due(conn)  # the GitHub pipelines, once a minute each (#6119)
    _adapter("gitlab").poll_due(conn)  # and the GitLab ones (#6121); Gitea and Forgejo: github.poll_due (#6122)
    _adapter("pulumi").poll_due(conn)  # and the Pulumi Delivery ones (#6133)
    from . import plugin
    plugin.poll_due(conn)  # and the pipelines whose source is a plugin (#6159)
    started, waiting = [], {}
    rows = conn.execute("SELECT r.id FROM stage_runs r JOIN pipeline_stages s ON s.id=r.stage_id WHERE "
                        "r.state='queued' AND s.removed_at IS NULL ORDER BY r.id").fetchall()
    for row in rows:
        r = _row(conn, row["id"])
        forge = r["workflow"] and not r["cmd"]
        if forge and (r["runner"] or not (r["forge"] in FORGES or plugin.is_plugin(r["forge"]))):
            continue  # started on the forge already (its adapter reads the run), or a forge with no adapter here
        busy = conn.execute("SELECT id FROM stage_runs WHERE stage_id=? AND state='running'", (r["stage_id"],)).fetchone()
        if busy or any(_row(conn, s)["stage_id"] == r["stage_id"] for s in started):
            open_, note = False, f"waits for run #{busy['id'] if busy else started[-1]} of stage {r['stage']}"
        else:
            open_, note = gate(conn, r)
        if note == "declined":
            by = conn.execute("SELECT actor FROM events WHERE item_id=? AND change LIKE 'drop%' ORDER BY id DESC "
                              "LIMIT 1", (r["gate_item"],)).fetchone() if r["gate_item"] else None
            with core.tx(conn):  # #6144: the person's no on the card is a line of the audit log, with that person
                _act(conn, r["pipeline_id"], by["actor"] if by else None,
                     f"declined version {r['rev']} at the transition into stage {r['stage']} (#{r['gate_item']})",
                     "decline", f"the transition into {r['stage']}", r["rev"], r["id"])
            store.run_finish(conn, r["id"], "cancelled", errors=f"the yes at the gate was declined (#{r['gate_item']})")
            continue
        if not open_:
            if note != r["gate_note"]:
                with core.tx(conn):
                    conn.execute("UPDATE stage_runs SET gate_note=? WHERE id=?", (note, r["id"]))
            waiting[r["id"]] = note
            continue
        if note and r["gate_item"] and not r["approved_at"]:  # approved through the card
            with core.tx(conn):
                by = core._done_by(conn, r["gate_item"])
                conn.execute("UPDATE stage_runs SET approved_by=?, approved_at=? WHERE id=?",
                             (by, store._now(), r["id"]))
                _act(conn, r["pipeline_id"], by, f"approved version {r['rev']} into stage {r['stage']} on "
                     f"#{r['gate_item']} (run #{r['id']})", "approve", f"the transition into {r['stage']}", r["rev"],
                     r["id"])
        started.append(r["id"])
        if forge and r["forge"] not in FORGES:  # the gate opened: the plugin starts it in its system (#6159)
            try:
                plugin.act(conn, r["id"], plugin.action_for(r["purpose"]))
            except core.RiverError as e:
                store.run_finish(conn, r["id"], "failed", errors=f"plugin {r['forge']} did not start it: {e}")
        elif forge:  # the gate opened: the forge runs it (#6119, #6121)
            try:
                _adapter(r["forge"]).dispatch(conn, r["id"])
            except core.RiverError as e:
                store.run_finish(conn, r["id"], "failed", errors=f"{FORGES[r['forge']]} did not start it: {e}")
        elif SYNC["on"]:
            _execute(conn, r["id"])
        else:
            _launch(r["id"])
    from . import links
    links.serve_pass(conn)  # a failed stage opens a fault item (#6118)
    return {"started": started, "waiting": waiting}
