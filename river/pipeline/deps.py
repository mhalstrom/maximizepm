"""Dependency releases (#6603, mark 2026-11-21: 'I would like to be able to independently update the dependencies of
libraries in our code base as well as the code in our code base ... trigger deployments for each of these cases
independently. For every single one of our pipelines. If desired.').

A pipeline has two kinds of release, started on their own:
- code: as before, a commit of the project's main (maxpm pipeline start, the release from holding);
- deps: the commit that the pipeline released last, plus only an update of its dependency files. maxpm serve runs the
  pipeline's update commands (pipelines.deps_cmds, one per ecosystem, in their order: put the audit fix first) in a
  detached worktree at that commit, and commits the dependency files they changed (pipelines.deps_files, else
  DEFAULT_FILES) on the branch maxpm/deps/<pipeline>-<run>. A command that changes another tracked file fails the
  update: no code of main goes out with it.

What the commit then does depends on how the pipeline releases:
- a deploy target in mode deploy (toolscaled-prod, maximizepm-github): a release item of the target pinned at the
  commit, made at the start (so no code release is cut while the update runs) and held by DEPS_BY until the update
  ends. It goes the way of every release: the release-cut hook, the review (setting review), the person's yes
  (release_approval person), the deploy hook with MAXPM_REV=<the commit> and MAXPM_RELEASE_KIND=deps, the watch.
- a deploy target in mode push (its sessions push main): one task for an agent that tests the commit and pushes it.
- a pipeline of its own: the commit goes into its first stage as a version.

After the update, a task for an agent brings it into main (a cherry-pick that the next code release's gate checks),
and every open code release of the target waits on that task, so the next code release keeps the update. When main
changed the same file, the task says to take main's file and run the update commands again on main, never to merge a
lock file's text by hand.
"""

import fnmatch
import json
import os
import signal
import subprocess
import tempfile
import threading

from .. import core
from . import audit, store

KINDS = ("code", "deps")
KIND_WORDS = {"code": "code release", "deps": "dependency release"}
DEPS_BY = "maxpm deps update"  # items.blocked_set_by of a release item while its update runs
# The files an update may change, by name in any folder: lock files and manifests of the usual ecosystems.
DEFAULT_FILES = ("Cargo.toml", "Cargo.lock", "package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock",
                 "pnpm-lock.yaml", "bun.lock", "bun.lockb", "pyproject.toml", "poetry.lock", "uv.lock",
                 "requirements*.txt", "Pipfile", "Pipfile.lock", "go.mod", "go.sum", "Gemfile", "Gemfile.lock",
                 "composer.json", "composer.lock", "mix.exs", "mix.lock", "pubspec.yaml", "pubspec.lock",
                 "Package.swift", "Package.resolved", "flake.lock")
OPEN_RUNS = ("queued", "running")
SYNC = {"on": False}  # tests: the serve pass runs an update itself, not in a thread
LOCK = threading.Lock()
RUNNING = {}  # run id -> True while a thread of this process runs it


# ---------------------------------------------------------------- the settings of a pipeline

def _list(text):
    try:
        v = json.loads(text) if text else []
    except ValueError:
        return []
    return [x for x in v if isinstance(x, str) and x.strip()] if isinstance(v, list) else []


def _mode(conn, p):
    """deploy, push (a deploy target's mode), or own (a pipeline of its own)."""
    if not p["target"]:
        return "own"
    t = conn.execute("SELECT mode FROM targets WHERE name=?", (p["target"],)).fetchone()
    return (t["mode"] if t else None) or "deploy"


def settings(conn, p):
    """The dependency release of the pipeline row p: {on, commands, files, own_files, mode, last}."""
    files = _list(p["deps_files"])
    last = conn.execute("SELECT * FROM deps_runs WHERE pipeline_id=? ORDER BY id DESC LIMIT 1", (p["id"],)).fetchone()
    return {"pipeline": p["name"], "on": bool(p["deps_on"]), "commands": _list(p["deps_cmds"]),
            "files": files or list(DEFAULT_FILES), "own_files": bool(files), "mode": _mode(conn, p),
            "last": _run_dict(last) if last else None}


def head(conn, name):
    """The dependency release of pipeline name for the head of its page and maxpm pipeline show: {on, mode, last:
    {state, at, rev, errors, ...}}, or None when it has no stored row, or the kind is off and never ran."""
    row = conn.execute("SELECT * FROM pipelines WHERE name=?", (name,)).fetchone()
    if row is None:
        return None
    s = settings(conn, row)
    if not s["on"] and not s["last"]:
        return None
    last = s["last"]
    return {"on": s["on"], "mode": s["mode"],
            "last": {"id": last["id"], "state": last["state"], "at": last["ended_at"] or last["created_at"],
                     "rev": last["rev"], "base": last["base"], "errors": last["errors"],
                     "deploy": last["deploy_id"], "item": last["item_id"], "fix": last["fix_id"]} if last else None}


def show(conn, name):
    """maxpm pipeline deps <name>: the settings and the last dependency releases."""
    p = store._pipeline(conn, name)
    out = settings(conn, p)
    out["runs"] = [_run_dict(r) for r in conn.execute("SELECT * FROM deps_runs WHERE pipeline_id=? ORDER BY id DESC "
                                                      "LIMIT 10", (p["id"],))]
    return out


def configure(conn, name, commands=None, on=None, files=None, actor=None):
    """maxpm pipeline deps <name> --cmd ... --on|--off --files ...: a person or the manager sets the commands (they
    replace the list; an empty list clears it), the switch, and the file patterns ('' : the default)."""
    if commands is None and on is None and files is None:
        return show(conn, name)
    why = core._lead_refusal(conn, actor, "sets the dependency release of a pipeline")
    if why:
        raise core.RiverError(why)
    p = store._pipeline(conn, name)
    cmds = [c.strip() for c in commands if c.strip()] if commands is not None else _list(p["deps_cmds"])
    if on and not cmds:
        raise core.RiverError(f"name the update commands first, one per ecosystem: maxpm pipeline deps {p['name']} "
                              f"--cmd \"cargo update\" --cmd \"npm update --prefix frontend\" --on")
    changes = []
    with core.tx(conn):
        if commands is not None:
            conn.execute("UPDATE pipelines SET deps_cmds=? WHERE id=?", (json.dumps(cmds) if cmds else None, p["id"]))
            changes.append("commands: " + ("; ".join(cmds) if cmds else "none"))
        if files is not None:
            pats = [f.strip() for f in files.split(",") if f.strip()]
            conn.execute("UPDATE pipelines SET deps_files=? WHERE id=?", (json.dumps(pats) if pats else None, p["id"]))
            changes.append("files: " + (", ".join(pats) if pats else "the default"))
        if on is not None:
            conn.execute("UPDATE pipelines SET deps_on=? WHERE id=?", (1 if on else 0, p["id"]))
            changes.append("on" if on else "off")
        audit.write(conn, p["id"], actor, "define", "dependency release: " + "; ".join(changes),
                    place="the dependency release")
    return show(conn, name)


# ---------------------------------------------------------------- the start

def _allowed(path, patterns):
    base = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(base, pat) or fnmatch.fnmatch(path, pat) for pat in patterns)


def _git(root, *args, timeout=60):
    try:
        r = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return None, str(e)
    return (r.stdout if r.returncode == 0 else None), (r.stderr or r.stdout).strip()


def _commit(root, rev):
    out, _ = _git(root, "rev-parse", "--verify", "-q", f"{rev}^{{commit}}")
    return out.strip() if out else None


def _root(conn, p):
    """The git folder of the pipeline's project (a deploy target: where its hooks run), or None."""
    from .. import review
    folder = core._hook_folder(conn, p["target"]) if p["target"] else None
    if not folder and p["project"]:
        r = conn.execute("SELECT path FROM projects WHERE name=?", (p["project"],)).fetchone()
        folder = r["path"] if r else None
    return review.repo_root(os.path.expanduser(folder)) if folder and os.path.isdir(os.path.expanduser(folder)) else None


def _base(conn, p, root):
    """(commit, label): what the pipeline released last. A deploy target in mode deploy: the pin of its last done
    release. In mode push: main as its sessions pushed it (main's upstream, else main). A pipeline of its own: the
    newest version that passed its last stage."""
    mode = _mode(conn, p)
    if mode == "deploy":
        for d in conn.execute("SELECT id, cut_rev FROM items WHERE kind='deploy' AND target=? AND status='done' AND "
                              "cut_rev IS NOT NULL ORDER BY closed_at DESC, id DESC LIMIT 10", (p["target"],)):
            c = _commit(root, d["cut_rev"])
            if c:
                return c, f"the pin of release #{d['id']}"
        raise core.RiverError(f"no release of {p['target']} went out at a commit of {root} yet, so there is no last "
                              f"release to update: release the code first (maxpm pipeline start {p['name']})")
    if mode == "push":
        for ref, label in (("main@{upstream}", "main as pushed"), ("main", "main")):
            c = _commit(root, ref)
            if c:
                return c, label
        raise core.RiverError(f"{root} has no branch main to update")
    last = conn.execute("SELECT id FROM pipeline_stages WHERE pipeline_id=? AND removed_at IS NULL ORDER BY pos DESC, "
                        "id DESC LIMIT 1", (p["id"],)).fetchone()
    r = conn.execute("SELECT v.rev FROM stage_runs r JOIN pipeline_versions v ON v.id=r.version_id WHERE r.stage_id=? "
                     "AND r.state='passed' ORDER BY r.id DESC LIMIT 1", (last["id"],)).fetchone() if last else None
    c = _commit(root, r["rev"]) if r else None
    if not c:
        raise core.RiverError(f"no version passed the last stage of {p['name']} yet, so there is no last release to "
                              f"update: release the code first (maxpm pipeline start {p['name']})")
    return c, "the last version that passed every stage"


def _may_start(conn, p, actor):
    """A person, the manager, maxpm itself (a schedule), or the owner of the deploy target start a dependency release,
    as they cut a code release (core.target_cut)."""
    if actor in (None, "maxpm") or core._lead_refusal(conn, actor, "") is None:
        return None
    if p["target"]:
        t = conn.execute("SELECT owner FROM targets WHERE name=?", (p["target"],)).fetchone()
        if t and t["owner"] == actor:
            return None
    return (f"refused: a person, the manager, or the target owner starts a dependency release of {p['name']}. Ask "
            f"the manager: maxpm ask <manager> \"start the dependency release of {p['name']}\"")


def start(conn, name, rev=None, actor=None):
    """maxpm pipeline start <name> --kind deps: a dependency release. rev: a commit made by hand on top of the last
    release that changes only dependency files (after a failed update); maxpm then runs no command. Returns
    {pipeline, kind, run, base, deploy?}."""
    p = store._pipeline(conn, name)
    why = _may_start(conn, p, actor)
    if why:
        raise core.RiverError(why)
    s = settings(conn, p)
    if not s["on"]:
        raise core.RiverError(f"the dependency release of {p['name']} is off; a person or the manager turns it on: "
                              f"maxpm pipeline deps {p['name']} --cmd \"<update command>\" --on")
    if not s["commands"] and not rev:
        raise core.RiverError(f"{p['name']} has no dependency update commands: maxpm pipeline deps {p['name']} "
                              f"--cmd \"<update command>\"")
    root = _root(conn, p)
    if not root:
        raise core.RiverError(f"the project of {p['name']} has no git folder: maxpm project path <project> <folder>")
    busy = conn.execute(f"SELECT id FROM deps_runs WHERE pipeline_id=? AND state IN {OPEN_RUNS}", (p["id"],)).fetchone()
    if busy:
        raise core.RiverError(f"dependency update u{busy['id']} of {p['name']} runs now: maxpm pipeline deps {p['name']}")
    mode = _mode(conn, p)
    if mode == "push":
        left = conn.execute("SELECT r.item_id FROM deps_runs r JOIN items i ON i.id=r.item_id WHERE r.pipeline_id=? "
                            f"AND i.status IN {core.OPEN_STATES}", (p["id"],)).fetchone()
        if left:
            raise core.RiverError(f"the last dependency update of {p['name']} is not pushed yet (#{left['item_id']}); "
                                  f"start the next one after it")
    if mode == "deploy":
        plan = core.release_plan(conn, p["target"])
        if plan["running"]:
            raise core.RiverError(f"release #{plan['running']} of {p['target']} runs now; start the dependency release "
                                  f"after it is out (maxpm show {plan['running']})")
    base, label = _base(conn, p, root)
    hand = None
    if rev:
        hand = _commit(root, rev)
        if not hand:
            raise core.RiverError(f"{rev} is no commit of {root}")
        files = _changed(root, base, hand)
        bad = [f for f in files if not _allowed(f, s["files"])]
        if not _is_ancestor(root, base, hand) or bad or not files:
            raise core.RiverError(
                f"{rev} is no dependency update of {label} ({base[:12]}): "
                + ("it is not on top of it" if not _is_ancestor(root, base, hand) else
                   f"it changes {', '.join(bad[:5])}, which are no dependency files" if bad else "it changes nothing")
                + f". Make it on top of {base[:12]} and change only {', '.join(s['files'][:6])}, ...")
    t = core.iso(core.now())
    dep_id = None
    with core.tx(conn):
        if mode == "deploy":
            dep_id = _release_item(conn, p, base, label, actor, t)
        run = conn.execute("INSERT INTO deps_runs(pipeline_id, base, base_label, state, deploy_id, started_by, "
                           "created_at, by_hand) VALUES (?,?,?,?,?,?,?,?)",
                           (p["id"], base, label, "queued", dep_id, actor or "a person", t, 1 if hand else None)).lastrowid
        audit.write(conn, p["id"], actor, "start", f"started dependency release u{run} on {label} ({base[:12]})"
                    + (f": release #{dep_id}" if dep_id else "") + (f", with the commit {hand[:12]} made by hand"
                                                                    if hand else ""),
                    place="the dependency release", rev=base)
        if dep_id:
            core._event(conn, dep_id, actor, f"dependency release u{run} of {p['name']}: maxpm updates the "
                                             f"dependencies of {label} ({base[:12]}); the release waits for it")
    if hand:
        _finish(conn, run, "passed", rev=hand, files=_changed(root, base, hand), output="the commit was made by hand")
    return {"pipeline": p["name"], "kind": "deps", "run": run, "base": base, "base_label": label, "deploy": dep_id,
            "mode": mode, "text": f"dependency release u{run} of {p['name']} started on {label} ({base[:12]})"
            + (f": release #{dep_id}" if dep_id else "")}


def _release_item(conn, p, base, label, actor, t):
    """Inside a tx: the release item of a deploy target for a dependency release. It counts as cut from now on (no code
    release is cut while it runs, and a ship request joins the next release item), with no pin until the update ends,
    and DEPS_BY holds it."""
    target = p["target"]
    pname = f"deploy-{target}"
    prow = conn.execute("SELECT id FROM projects WHERE name=?", (pname,)).fetchone()
    if prow is None:
        top = conn.execute("SELECT COALESCE(MAX(rank),0) m FROM projects WHERE archived=0").fetchone()["m"]
        conn.execute("INSERT INTO projects(name,rank,notes,target,created_at) VALUES (?,?,?,?,?)",
                     (pname, top + 1, f"Deploys to target {target}, made by maxpm ship. Only the target owner takes "
                      f"these items.", target, t))
        prow = conn.execute("SELECT id FROM projects WHERE name=?", (pname,)).fetchone()
    top = conn.execute("SELECT COALESCE(MAX(rank),0) m FROM items WHERE project_id=?", (prow["id"],)).fetchone()["m"]
    return conn.execute(
        "INSERT INTO items(project_id,title,short,notes,priority,rank,doer,context,kind,target,created_at,cut_at,"
        "blocked_reason,blocked_at,blocked_set_by) VALUES (?,?,?,?,?,?,'any',?,'deploy',?,?,?,?,?,?)",
        (prow["id"], f"Deploy {target}: dependency update of {base[:12]}", "Dependency release",
         "Put the release id in the output.", 2, top + 1,
         f"A dependency release of {target} (maxpm pipeline start {p['name']} --kind deps, by {actor or 'a person'}): "
         f"{label} ({base}) plus only an update of its dependency files, made by the commands of maxpm pipeline deps "
         f"{p['name']}. It goes out the way every release of {target} does: its review, the person's yes when the "
         f"target asks for one, its deploy hook with MAXPM_REV=<the commit> and MAXPM_RELEASE_KIND=deps.",
         target, t, t, f"maxpm updates the dependencies of {label} ({base[:12]}) for this release", t, DEPS_BY)
    ).lastrowid


def _changed(root, a, b):
    out, _ = _git(root, "diff", "--name-only", a, b)
    return [x for x in (out or "").splitlines() if x.strip()]


def _is_ancestor(root, a, b):
    try:
        return subprocess.run(["git", "-C", root, "merge-base", "--is-ancestor", a, b], capture_output=True,
                              timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# ---------------------------------------------------------------- maxpm serve runs the update

def serve_pass(conn):
    """One pass of the loop of maxpm serve: end the updates that no thread runs any more, start the queued ones (each
    in a thread of its own), and keep every open code release of a target waiting on the task that brings a
    dependency update into main. Returns {"started": [run ids]}."""
    with LOCK:
        mine = set(RUNNING)
    for r in conn.execute("SELECT id FROM deps_runs WHERE state='running'").fetchall():
        if r["id"] not in mine:
            _finish(conn, r["id"], "failed", errors="maxpm serve stopped while the update ran")
    started = []
    for r in conn.execute("SELECT id FROM deps_runs WHERE state='queued' ORDER BY id").fetchall():
        with core.tx(conn):
            if conn.execute("UPDATE deps_runs SET state='running', started_at=? WHERE id=? AND state='queued'",
                            (core.iso(core.now()), r["id"])).rowcount == 0:
                continue
        started.append(r["id"])
        if SYNC["on"]:
            _execute(conn, r["id"])
        else:
            _launch(r["id"])
    _keep_waits(conn)
    return {"started": started}


def _launch(run_id):
    def work():
        try:
            c = core.connect()
            try:
                _execute(c, run_id)
            finally:
                c.close()
        except Exception as e:  # noqa: BLE001 - the next pass ends the run as lost
            print(f"dependency update u{run_id}: {type(e).__name__}: {e}", flush=True)
        finally:
            with LOCK:
                RUNNING.pop(run_id, None)
    with LOCK:
        RUNNING[run_id] = True
    threading.Thread(target=work, daemon=True, name=f"maxpm-deps-{run_id}").start()


def running_text():
    """Why maxpm serve may not start again now, or None."""
    with LOCK:
        n = len(RUNNING)
    return f"{n} dependency update(s) run" if n else None


def _shell(cmd, cwd, env, limit):
    """Run one command in its own process group; (exit code or None on the time limit, output)."""
    try:
        proc = subprocess.Popen(cmd, shell=True, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, start_new_session=True)
    except OSError as e:
        return 127, str(e)
    try:
        out, _ = proc.communicate(timeout=limit or None)
        return proc.returncode, out or ""
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        out, _ = proc.communicate()
        return None, out or ""


def _tail(text, n=40):
    lines = [x for x in (text or "").splitlines() if x.strip()]
    return "\n".join(lines[-n:])


def _execute(conn, run_id):
    """Run the update of run_id: a detached worktree at its base, each command in order, then a commit of the
    dependency files they changed on maxpm/deps/<pipeline>-<run>. Ends the run (_finish)."""
    r = conn.execute("SELECT * FROM deps_runs WHERE id=?", (run_id,)).fetchone()
    p = conn.execute("SELECT * FROM pipelines WHERE id=?", (r["pipeline_id"],)).fetchone()
    s = settings(conn, p)
    root = _root(conn, p)
    if not root:
        return _finish(conn, run_id, "failed", errors="the project has no git folder")
    project_id = conn.execute("SELECT id FROM projects WHERE name=?", (p["project"],)).fetchone() if p["project"] else None
    limit = core.parse_duration(core.setting(conn, "deps_timeout", project_id=project_id["id"] if project_id else None))
    limit = limit.total_seconds()
    tmp = tempfile.mkdtemp(prefix=f"maxpm-deps-{run_id}-")
    log = []
    try:
        made, err = _git(root, "worktree", "add", "--detach", tmp, r["base"], timeout=300)
        if made is None:
            return _finish(conn, run_id, "failed", errors=f"git worktree add failed: {err}")
        env = {**os.environ, "MAXPM_PIPELINE": p["name"], "MAXPM_BASE": r["base"], "MAXPM_DEPS_RUN": str(run_id)}
        for cmd in s["commands"]:
            log.append(f"$ {cmd}")
            code, out = _shell(cmd, tmp, env, limit)
            log.append(_tail(out, 30))
            if code != 0:
                return _finish(conn, run_id, "failed", output=_tail("\n".join(log)),
                               errors=(f"{cmd} ran longer than deps_timeout and was stopped" if code is None else
                                       f"{cmd} exited {code}"))
        status, _ = _git(tmp, "status", "--porcelain", "-z", "--untracked-files=all")
        tracked, new = [], []
        for e in (status or "").split("\0"):
            if len(e) > 3:
                (new if e[:2] == "??" else tracked).append(e[3:])
        bad = [f for f in tracked if not _allowed(f, s["files"])]
        if bad:
            return _finish(conn, run_id, "failed", output=_tail("\n".join(log)),
                           errors=f"the commands changed {', '.join(bad[:5])}, which are no dependency files "
                                  f"(maxpm pipeline deps {p['name']} --files)")
        files = sorted(set(tracked) | {f for f in new if _allowed(f, s["files"])})
        if not files:
            return _finish(conn, run_id, "unchanged", output=_tail("\n".join(log)))
        _git(tmp, "add", "--", *files)
        who = _git(root, "config", "user.email")[0]
        ident = [] if who and who.strip() else ["-c", "user.name=MaximizePM", "-c", "user.email=maxpm@localhost"]
        msg = (f"Update the dependencies of {p['name']} on {r['base'][:12]} (dependency release u{run_id})\n\n"
               f"Files: {', '.join(files)}\nCommands: {'; '.join(s['commands'])}")
        _, err = _git(tmp, *ident, "commit", "-q", "-m", msg, timeout=120)
        sha = _commit(tmp, "HEAD")
        if not sha or sha == r["base"]:
            return _finish(conn, run_id, "failed", output=_tail("\n".join(log)), errors=f"git commit failed: {err}")
        branch = f"maxpm/deps/{p['name']}-{run_id}"
        _git(root, "branch", "-f", branch, sha)
        return _finish(conn, run_id, "passed", rev=sha, files=files, output=_tail("\n".join(log)), branch=branch)
    finally:
        _git(root, "worktree", "remove", "--force", tmp, timeout=120)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- the end of an update

def _finish(conn, run_id, state, rev=None, files=None, output=None, errors=None, branch=None):
    """End the update run_id: passed (rev, files), unchanged, or failed (errors). Makes the items that follow."""
    r = conn.execute("SELECT * FROM deps_runs WHERE id=?", (run_id,)).fetchone()
    if r is None or r["state"] not in OPEN_RUNS:
        return None
    p = conn.execute("SELECT * FROM pipelines WHERE id=?", (r["pipeline_id"],)).fetchone()
    mode = _mode(conn, p)
    t = core.iso(core.now())
    with core.tx(conn):
        if conn.execute(f"UPDATE deps_runs SET state=?, rev=?, files=?, output=?, errors=?, branch=?, ended_at=? "
                        f"WHERE id=? AND state IN {OPEN_RUNS}",
                        (state, rev, json.dumps(files) if files else None, output, errors, branch, t, run_id)
                        ).rowcount == 0:
            return None
        said = {"passed": f"updated {len(files or [])} dependency file(s): {rev[:12] if rev else ''}",
                "unchanged": "the dependencies are up to date: no file changed",
                "failed": f"the update failed: {errors}"}[state]
        audit.write(conn, p["id"], "maxpm", state, f"dependency release u{run_id}: {said}",
                    place="the dependency release", rev=rev or r["base"], reason=errors)
        dep = core._item(conn, r["deploy_id"]) if r["deploy_id"] else None
        if dep is not None and state == "passed":
            _ship_release(conn, p, r, dep, rev, files)
        elif dep is not None:
            conn.execute("UPDATE items SET status='dropped', closed_at=?, cut_at=NULL, blocked_reason=NULL, "
                         "blocked_at=NULL, blocked_set_by=NULL WHERE id=?", (t, dep["id"]))
            core._event(conn, dep["id"], "maxpm", f"dropped: dependency release u{run_id}: {said}")
    if state == "failed":
        _fix_task(conn, p, r, errors, output)
    elif state == "passed":
        if mode == "own":
            from . import runner
            try:
                runner.start(conn, p["name"], rev, "maxpm")
            except core.RiverError as e:
                with core.tx(conn):
                    audit.write(conn, p["id"], "maxpm", "start", f"dependency release u{run_id}: the version "
                                f"{rev[:12]} did not start: {e}", place="the dependency release", rev=rev)
        _main_task(conn, p, dict(r, rev=rev, branch=branch, files=json.dumps(files or [])), mode)
    core.sync_needs_you(conn)
    return state


def _ship_release(conn, p, r, dep, rev, files):
    """Inside a tx: the update of a deploy target passed. One done task records it (the release ships it, so its
    review, report and yes list it), and the release is cut at the commit the usual way (its release-cut hook, its
    review, its report)."""
    target = p["target"]
    proj = conn.execute("SELECT id FROM projects WHERE target=? AND archived=0 AND name<>? ORDER BY rank, id LIMIT 1",
                        (target, f"deploy-{target}")).fetchone()
    t = core.iso(core.now())
    change = conn.execute(
        "INSERT INTO items(project_id,title,short,priority,rank,doer,context,kind,status,output,touches,created_at,"
        "closed_at) VALUES (?,?,?,?,0,'ai',?,'work','done',?,?,?,?)",
        (proj["id"] if proj else dep["project_id"],
         f"Dependency update of {target} on {r['base'][:12]}: {', '.join(files[:4])}"
         + (f" and {len(files) - 4} more" if len(files) > 4 else ""), "Dependency update", dep["priority"],
         f"Made by maxpm (dependency release u{r['id']}): {r['base_label']} ({r['base']}) plus the files the update "
         f"commands changed. Commit {rev}" + (f" on branch {r['branch']}" if r["branch"] else "") + ".",
         f"{rev[:12]}: {', '.join(files)}", ",".join(files), t, t)).lastrowid
    core._event(conn, change, "maxpm", f"done: dependency release u{r['id']} updated {', '.join(files)} ({rev[:12]})")
    core._dep_add(conn, dep["id"], change, "maxpm", alert=False)
    conn.execute("UPDATE items SET cut_at=NULL, blocked_reason=NULL, blocked_at=NULL, blocked_set_by=NULL, title=? "
                 "WHERE id=?", (f"Deploy {target}: dependency update {rev[:12]} on {r['base'][:12]}", dep["id"]))
    core._release_cut(conn, dep["id"], "maxpm", f"dependency release u{r['id']}", rev)
    tg = core._target(conn, target)
    core._release_review(conn, core._item(conn, dep["id"]), tg, "maxpm")


def _cmds_text(p):
    return "; ".join(_list(p["deps_cmds"])) or "(none)"


def _main_task(conn, p, r, mode):
    """The task that brings the update into main; for a push target it also pushes it (its release). Its id goes on
    the run."""
    files = _list(r["files"])
    project = p["project"] or (conn.execute("SELECT name FROM projects WHERE target=? AND archived=0 AND name<>? "
                                            "ORDER BY rank, id LIMIT 1", (p["target"], f"deploy-{p['target']}")
                                            ).fetchone() or {"name": None})["name"]
    if not project:
        return None
    where = f"release #{r['deploy_id']}" if r["deploy_id"] else f"dependency release u{r['id']}"
    conflict = (f"When main changed the same file since {r['base'][:12]}, the cherry-pick stops there: do not merge "
                f"a lock file's text by hand. Take main's version of each such file (git checkout HEAD -- <file>), "
                f"run the update commands again on main ({_cmds_text(p)}), and check with git diff {r['rev'][:12]} -- "
                f"<file> that no dependency is older than in {r['rev'][:12]}.")
    if mode == "push":
        title = f"Push the dependency update {r['rev'][:12]} of {p['name']}"
        context = (f"maxpm updated the dependencies of {p['name']} on {r['base_label']} ({r['base'][:12]}): commit "
                   f"{r['rev']} on branch {r['branch']} changes only {', '.join(files)}. {p['name']} releases by a "
                   f"push, so this task is its dependency release. In your worktree on main: git cherry-pick "
                   f"{r['rev'][:12]}. {conflict} Run the project's tests, fast-forward main and push it the way "
                   f"the project says (no --ship). A person's yes first only when the target asks for one "
                   f"(release_approval person).")
    else:
        title = f"Bring the dependency update {r['rev'][:12]} of {p['name']} into main"
        context = (f"{where} of {p['name']} ships commit {r['rev']} (branch {r['branch']}): {r['base_label']} "
                   f"({r['base'][:12]}) plus only {', '.join(files)}. Bring the same change into main, so the next "
                   f"code release keeps it: in your worktree on main, git cherry-pick {r['rev'][:12]}. {conflict} "
                   f"Run the tests, commit with (#<this id>), fast-forward main the way the project says"
                   + (f", then maxpm done <this id> --ship: the next code release of {p['target']} waits on this "
                      f"task" if p["target"] else "") + ".")
    it = core.item_add(conn, project, title, priority=2, doer="ai", actor="maxpm", context=context,
                       touches=",".join(files), short="Dependency update into main" if mode != "push"
                       else "Push the dependency update",
                       found_during=r["deploy_id"] or None)
    with core.tx(conn):
        conn.execute("UPDATE deps_runs SET item_id=? WHERE id=?", (it["id"], r["id"]))
        _keep_waits(conn, in_tx=True)
    return it["id"]


def _fix_task(conn, p, r, errors, output):
    project = p["project"] or (conn.execute("SELECT name FROM projects WHERE target=? AND archived=0 AND name<>? "
                                            "ORDER BY rank, id LIMIT 1", (p["target"], f"deploy-{p['target']}")
                                            ).fetchone() or {"name": None})["name"]
    if not project:
        return None
    first = (errors or "failed").splitlines()[0][:100]
    context = (f"maxpm ran the dependency update of {p['name']} (u{r['id']}) on {r['base_label']} ({r['base']}) in a "
               f"worktree of its own, and it failed: {errors}.\nCommands: {_cmds_text(p)}\nTheir last lines:\n"
               f"{output or '(none)'}\n\nFind the cause. When the commands are wrong, ask the manager to change them "
               f"(maxpm pipeline deps {p['name']} --cmd ...). When a dependency needs a change of the code, that is a "
               f"code change: do it on main as a normal task. When the update works by hand: in a worktree at "
               f"{r['base'][:12]} (git worktree add --detach <folder> {r['base'][:12]}), run the commands, commit only "
               f"the dependency files, and ask the manager to start the release with your commit: maxpm pipeline "
               f"start {p['name']} --kind deps --rev <commit>.")
    it = core.item_add(conn, project, f"Fix the dependency update of {p['name']}: {first}", priority=2, doer="ai",
                       actor="maxpm", context=context, short="Fix the dependency update")
    with core.tx(conn):
        conn.execute("UPDATE deps_runs SET fix_id=? WHERE id=?", (it["id"], r["id"]))
    return it["id"]


def _keep_waits(conn, in_tx=False):
    """Every open code release of a deploy target waits on each open task that brings a dependency update of the
    target into main, so the next code release keeps the update. A task whose dependency release was dropped before
    anyone took it is dropped too."""
    def work():
        rows = conn.execute(
            "SELECT r.id, r.deploy_id, r.item_id, p.target FROM deps_runs r JOIN pipelines p ON p.id=r.pipeline_id "
            f"JOIN items i ON i.id=r.item_id WHERE r.item_id IS NOT NULL AND i.status IN {core.OPEN_STATES} AND "
            "p.target IS NOT NULL").fetchall()
        for r in rows:
            dep = core._item(conn, r["deploy_id"]) if r["deploy_id"] else None
            it = core._item(conn, r["item_id"])
            if dep is not None and dep["status"] == "dropped" and it["status"] == "open" and not it["assignee"]:
                conn.execute("UPDATE items SET status='dropped', closed_at=? WHERE id=?", (core.iso(core.now()), it["id"]))
                core._event(conn, it["id"], "maxpm", f"dropped: its dependency release #{dep['id']} was dropped")
                continue
            own = {x["deploy_id"] for x in conn.execute("SELECT deploy_id FROM deps_runs WHERE deploy_id IS NOT NULL")}
            for d in conn.execute(f"SELECT id FROM items WHERE kind='deploy' AND target=? AND status IN "
                                  f"{core.OPEN_STATES}", (r["target"],)).fetchall():
                if d["id"] in own or conn.execute("SELECT 1 FROM deps WHERE item_id=? AND blocked_by=?",
                                                  (d["id"], it["id"])).fetchone():
                    continue
                core._dep_add(conn, d["id"], it["id"], "maxpm", alert=False)
                core._event(conn, d["id"], "maxpm", f"waits on #{it['id']}: the dependency update of release "
                                                    f"#{r['deploy_id']} goes into main first, so this release keeps it")
    if in_tx:
        return work()
    with core.tx(conn):
        return work()


# ---------------------------------------------------------------- the kind of each version

def _run_dict(r):
    d = dict(r)
    d["files"] = _list(d.get("files"))
    return d


def deploy_kinds(conn, target):
    """{deploy item id: 'deps'} for the dependency releases of a target (the others are code releases)."""
    return {r["deploy_id"]: "deps" for r in conn.execute(
        "SELECT r.deploy_id FROM deps_runs r JOIN pipelines p ON p.id=r.pipeline_id WHERE p.target=? AND "
        "r.deploy_id IS NOT NULL", (target,))}


def rev_kinds(conn, pipeline_id):
    """{commit: 'deps'} for the versions of a pipeline that a dependency release made."""
    return {r["rev"]: "deps" for r in conn.execute("SELECT rev FROM deps_runs WHERE pipeline_id=? AND rev IS NOT NULL",
                                                   (pipeline_id,))}


def kind_of_deploy(conn, deploy_id):
    """code or deps: the kind of a release item (MAXPM_RELEASE_KIND of its hooks)."""
    if not deploy_id:
        return ""
    return "deps" if conn.execute("SELECT 1 FROM deps_runs WHERE deploy_id=?", (deploy_id,)).fetchone() else "code"


def base_of_deploy(conn, deploy_id):
    """The last release that a dependency release updates (MAXPM_DEPS_BASE of its hooks), or ''."""
    r = conn.execute("SELECT base FROM deps_runs WHERE deploy_id=? ORDER BY id DESC LIMIT 1",
                     (deploy_id,)).fetchone() if deploy_id else None
    return r["base"] if r else ""


def start_kind(conn, name, kind, rev=None, actor=None):
    """maxpm pipeline start <name> --kind code|deps (and a schedule): one start for both kinds."""
    kind = kind or "code"
    if kind not in KINDS:
        raise core.RiverError(f"the kind of a release is {' or '.join(KINDS)}, not {kind!r}")
    if kind == "deps":
        return start(conn, name, rev, actor)
    from . import runner
    out = runner.start(conn, name, rev, actor)
    return dict(out, kind="code")
