"""Runners (#6151): servers where an agent works on a pipeline's task with no person, no terminal and no prompt.

mark 2026-11-18 (canvas release-tracking, goal part 2): "hooks for getting agents running on deployment tasks and
issues without required human environment". A trigger rule of a pipeline (#6150) names where its agent runs: this
computer (a session in a terminal, as before), or a runner from this list. The list is shared; each pipeline ticks
the runners it may use (pipelines.runners), and a rule may name only those.

    maxpm runner add ci-1 --ssh ci@ci-1     checks the server over SSH, installs this computer's maxpm there (as maxpm
                                            host setup does), and finds the agent CLIs on its PATH

What a run is, step by step (serve_pass, each pass of maxpm serve):

1. queue: a rule acts and its task (a fault or a task) names a runner: a session record <runner>-r<run> is made and
   the task is reserved for it, so maxpm serve starts no session here for it.
2. start, when the runner has a free slot: one `ssh -R` connection. The port it opens on the runner's 127.0.0.1 leads
   back to maxpm serve here. The shell script goes on stdin (the keys never stand in a command line): it clones the
   project there once (from the origin of the project's folder here), adds a worktree at the version, and runs the
   agent CLI with no terminal (CLIS: Claude Code print mode, Codex exec). What the agent prints comes back on the
   connection into the run's transcript, a file beside the queue.
3. While it runs, maxpm on the runner sends each command through that port (POST /api/runner-run) with the run's
   key (MAXPM_RUNNER_KEY) and the session's token (MAXPM_TOKEN). serve checks both (request_for) and runs the
   command here, on this computer's queue, in the project's folder here. So the agent claims, reports its steps
   (maxpm now), asks (a card, never a prompt) and closes its task as any session does.
4. end: the CLI exits, max_minutes pass (serve stops it), or a person stops it. The transcript's cost and last words
   are kept; a task that is still open goes back to the queue and the manager hears why.

How it reaches the queue: only over this SSH connection, which this computer opens (Tailscale SSH or a key, policy
p159). Nothing listens on the tailnet for it, and the runner needs no address of this computer. A CI runner of a forge
is outside the tailnet: that is #6152. The model key of the CLI is the runner's own (a key in its environment, or the
CLI's own sign-in there), or a set granted to the runner (#6153: maxpm secret grant <set> --runner <name>; its secret
carries the variable the CLI reads, maxpm secret level <name> --var ANTHROPIC_API_KEY). The run also gets the sets
that its rule granted to its task (pipeline/keys.run_env), in the script on stdin; the transcript masks them. The list
of what an agent may do with no prompt per pipeline comes with #6154 (until then: ALLOW, or the runner's allow).

Standard library only."""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import secrets
import shlex
import subprocess
from pathlib import Path

from . import __version__, core, host
from .core import RiverError

THIS = "this computer"
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
FOLDER = "~/maxpm-runner"  # where the projects are cloned on a runner
# What a Claude Code agent may use on a runner with no prompt, until each pipeline has its own list (#6154): maxpm
# and git, and reading and changing files. Any other tool is refused, never asked (permission mode dontAsk).
ALLOW = "Bash(maxpm:*) Bash(git:*) Read Edit Write Glob Grep"
# The agent CLIs that run with no terminal and no prompt (their own --help, read 2026-11-19: Claude Code 2.1.328,
# codex-cli 0.159.1). key: what the CLI needs on the runner to call its model.
CLIS = {
    "claude": {"label": "Claude Code", "type": "claude-code",
               "key": "a secret with --var ANTHROPIC_API_KEY in a set granted to the runner (#6153), else "
                      "ANTHROPIC_API_KEY in the runner's environment"},
    "codex": {"label": "Codex", "type": "codex", "key": "a secret with --var CODEX_API_KEY in a set granted to the "
                                                        "runner, else codex login or CODEX_API_KEY there"},
}
LOG_KEEP = 400      # transcript lines that maxpm runner log and the page show at most
SPAWN = None        # tests: fn(argv, script, transcript_path) -> an object with pid, poll() and terminate()
GIT = None          # tests: fn(folder) -> the origin address of a project's folder, or None
SERVE_PORT = None   # tests: the port of maxpm serve
PROCS = {}          # run id -> the ssh process of this serve


def _now():
    return core.iso(core.now())


def _hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


def _may(conn, actor, what):
    why = core._lead_refusal(conn, actor, what)
    if why:
        raise RiverError(why)


def _runner(conn, name):
    r = conn.execute("SELECT * FROM runners WHERE name=?", (name,)).fetchone()
    if r is None:
        names = [x["name"] for x in conn.execute("SELECT name FROM runners ORDER BY name")]
        raise RiverError(f"no runner {name!r}; the runners: {', '.join(names) or 'none yet'} "
                         f"(a person or the manager adds one: maxpm runner add <name> --ssh <target>)")
    return r


def _info(r):
    d = dict(r)
    d["clis"] = json.loads(r["clis"] or "{}")
    d["allow"] = r["allow"] or ALLOW
    d["folder"] = r["folder"] or FOLDER
    return d


# ---------------------------------------------------------------- the list

def _probe(target):
    """What the server has, in one SSH call: its system, its maxpm, the agent CLIs on its PATH, its Tailscale address."""
    script = ('uname -sm; echo "--"; PATH="$HOME/.local/bin:$PATH"; maxpm --version 2>/dev/null | tail -1; echo "--"; '
              + "".join(f'v=$({c} --version 2>/dev/null | head -1) && [ -n "$v" ] && echo "{c} $v"; ' for c in CLIS)
              + 'echo "--"; tailscale ip -4 2>/dev/null | head -1; true')
    code, out, err = host._ssh3(target, script, timeout=60)
    if code:
        raise RiverError(f"ssh {target} does not answer without a prompt ({err or out or 'exit ' + str(code)}). Turn "
                         f"on Tailscale SSH on the server (tailscale set --ssh) or add your SSH key there, check that "
                         f"'ssh {target} true' works, then try again")
    parts = (out + "\n").split("--\n")
    parts += [""] * (4 - len(parts))
    system = parts[0].strip().splitlines()[-1] if parts[0].strip() else ""
    if not system.startswith(("Linux", "Darwin")):
        raise RiverError(f"{target} runs {system or 'an unknown system'}; a runner runs Linux or macOS")
    clis = {}
    for line in parts[2].splitlines():
        name, _, v = line.strip().partition(" ")
        if name in CLIS and v:
            clis[name] = v.strip()
    version = parts[1].strip()
    return {"system": system, "version": version if version.startswith("maxpm ") else None, "clis": clis,
            "tailnet": parts[3].strip() or None}


def add(conn, name, ssh, cli=None, slots=None, max_minutes=None, max_cost=None, actor=None):
    """maxpm runner add: check the server, install this computer's maxpm there when its version differs, and keep it
    in the list. Adding a name again checks it again and keeps its settings."""
    _may(conn, actor, "adds a runner, because a runner runs agents with the queue's rights")
    name = (name or "").strip().lower()
    if not NAME.match(name) or name == "this":
        raise RiverError("a runner's name is lower-case letters, digits and '-' (up to 32), such as ci-1")
    host._check_target(ssh)
    if cli and cli not in CLIS:
        raise RiverError(f"the agent CLI of a runner is one of: {', '.join(CLIS)}")
    steps = []
    found = _probe(ssh)
    steps.append(f"{ssh} answers over SSH: {found['system']}")
    want = f"maxpm {__version__}"
    if found["version"] != want:
        host.install(ssh)
        steps.append(f"installed {want} from this computer's code" + (f" (it had {found['version']})"
                                                                     if found["version"] else ""))
        found["version"] = want
    else:
        steps.append(f"{want} is there already")
    steps.append("agent CLIs there: " + (", ".join(f"{CLIS[c]['label']} {v}" for c, v in found["clis"].items())
                                         or "none on its PATH"))
    steps.append(f"Tailscale address: {found['tailnet']}" if found["tailnet"] else
                 "Tailscale does not run there: SSH reaches it, but Tailscale SSH is the way policy p159 asks for")
    old = conn.execute("SELECT * FROM runners WHERE name=?", (name,)).fetchone()
    cli = cli or (old["cli"] if old else None) or ("claude" if "claude" in found["clis"] or not found["clis"]
                                                   else next(iter(found["clis"])))
    with core.tx(conn):
        conn.execute("INSERT INTO runners(name, ssh, added_by, added_at) VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE "
                     "SET ssh=excluded.ssh", (name, ssh, actor, _now()))
        conn.execute("UPDATE runners SET system=?, version=?, tailnet=?, clis=?, cli=?, state='ok', error=NULL, "
                     "checked_at=? WHERE name=?", (found["system"], found["version"], found["tailnet"],
                                                   json.dumps(found["clis"]), cli, _now(), name))
        core._event(conn, None, actor or "maxpm", f"runner {name} ({ssh}) {'checked again' if old else 'added'}")
    if slots is not None or max_minutes is not None or max_cost is not None:
        change(conn, name, slots=slots, max_minutes=max_minutes, max_cost=max_cost, actor=actor)
    r = _info(_runner(conn, name))
    if cli not in r["clis"]:
        steps.append(f"{CLIS[cli]['label']} is not on its PATH: install it there, sign it in "
                     f"({CLIS[cli]['key']}), then run maxpm runner check {name}")
    return {"runner": r, "steps": steps}


def check(conn, name, actor=None):
    """Ask the server again what it has (no install). A server that does not answer is marked bad: no run starts."""
    _may(conn, actor, "checks a runner")
    r = _runner(conn, name)
    try:
        found = _probe(r["ssh"])
    except RiverError as e:
        with core.tx(conn):
            conn.execute("UPDATE runners SET state='bad', error=?, checked_at=? WHERE name=?", (str(e), _now(), name))
        return _info(_runner(conn, name))
    with core.tx(conn):
        conn.execute("UPDATE runners SET system=?, version=?, tailnet=?, clis=?, state='ok', error=NULL, checked_at=? "
                     "WHERE name=?", (found["system"], found["version"], found["tailnet"], json.dumps(found["clis"]),
                                      _now(), name))
    return _info(_runner(conn, name))


def change(conn, name, cli=None, slots=None, max_minutes=None, max_cost=None, allow=None, folder=None, actor=None):
    """maxpm runner set: its agent CLI, slots, time and cost limits, the tools allowed with no prompt, its folder."""
    _may(conn, actor, "changes a runner")
    r = _runner(conn, name)
    new = {}
    if cli is not None:
        if cli not in CLIS:
            raise RiverError(f"the agent CLI of a runner is one of: {', '.join(CLIS)}")
        new["cli"] = cli
    for key, v, low in (("slots", slots, 1), ("max_minutes", max_minutes, 1)):
        if v is not None:
            if int(v) < low:
                raise RiverError(f"{key} is a whole number of at least {low}")
            new[key] = int(v)
    if max_cost is not None:
        if float(max_cost) <= 0:
            raise RiverError("max_cost is a number of dollars above 0")
        new["max_cost"] = float(max_cost)
    if allow is not None:
        new["allow"] = " ".join(allow.split()) or None
    if folder is not None:
        if folder and not (folder.startswith(("/", "~/"))):
            raise RiverError("the folder of a runner is a full path (/srv/maxpm) or starts at its home (~/maxpm)")
        new["folder"] = folder or None
    new = {k: v for k, v in new.items() if v != r[k]}
    if not new:
        raise RiverError(f"runner {name} has these values already; give what to change: --cli, --slots, "
                         f"--max-minutes, --max-cost, --allow or --folder")
    with core.tx(conn):
        conn.execute("UPDATE runners SET " + ", ".join(f"{k}=?" for k in new) + " WHERE name=?", [*new.values(), name])
        core._event(conn, None, actor or "maxpm", f"runner {name}: " + "; ".join(f"{k} {v}" for k, v in new.items()))
    return _info(_runner(conn, name))


def remove(conn, name, actor=None):
    """Take a runner off the list: refused while it runs a task or a rule names it."""
    _may(conn, actor, "removes a runner")
    _runner(conn, name)
    busy = conn.execute("SELECT id FROM runner_runs WHERE runner=? AND state IN ('queued', 'running')",
                        (name,)).fetchall()
    if busy:
        raise RiverError(f"runner {name} has runs that wait or run ({', '.join('#' + str(r['id']) for r in busy)}); "
                         f"stop them first: maxpm runner stop <run>")
    rules = [f"{r['name']} t{r['id']}" for r in conn.execute(
        "SELECT t.id, p.name FROM pipeline_triggers t JOIN pipelines p ON p.id=t.pipeline_id WHERE t.runner=?", (name,))]
    if rules:
        raise RiverError(f"the trigger rules {', '.join(rules)} run their agents on {name}; change them first "
                         f"(maxpm pipeline trigger <pipeline> edit t<n> --runs-on \"{THIS}\")")
    with core.tx(conn):
        for p in conn.execute("SELECT id, runners FROM pipelines WHERE runners IS NOT NULL").fetchall():
            names = [n for n in json.loads(p["runners"] or "[]") if n != name]
            conn.execute("UPDATE pipelines SET runners=? WHERE id=?", (json.dumps(names) if names else None, p["id"]))
        conn.execute("DELETE FROM runners WHERE name=?", (name,))
        core._event(conn, None, actor or "maxpm", f"runner {name} removed")
    return {"removed": name}


# ---------------------------------------------------------------- which pipelines use which runner

def ticked(p):
    """The runners a pipeline row ticks (may use)."""
    try:
        return [n for n in json.loads(p["runners"] or "[]") if isinstance(n, str)]
    except (ValueError, TypeError, IndexError, KeyError):
        return []


def choices(conn, p):
    """Where a rule of pipeline p may run its agent: this computer, and each runner it ticks that is on the list."""
    have = {r["name"] for r in conn.execute("SELECT name FROM runners")}
    return [THIS] + [n for n in ticked(p) if n in have]


def allow_pipeline(conn, pipeline, name, on=True, actor=None):
    """maxpm runner allow|deny <runner> <pipeline>: tick or untick a runner for a pipeline. Untick is refused while a
    rule of that pipeline names the runner."""
    from .pipeline import define, store
    _may(conn, actor, "changes which runners a pipeline may use, because they run its agents")
    p = store._pipeline(conn, pipeline)
    _runner(conn, name)
    names = ticked(p)
    if on == (name in names):
        raise RiverError(f"pipeline {p['name']} {'may use' if on else 'does not use'} runner {name} already")
    if not on:
        rules = [f"t{r['id']}" for r in conn.execute("SELECT id FROM pipeline_triggers WHERE pipeline_id=? AND runner=?",
                                                     (p["id"], name))]
        if rules:
            raise RiverError(f"the rules {', '.join(rules)} of {p['name']} run on {name}; change them first")
    names = names + [name] if on else [n for n in names if n != name]
    with core.tx(conn):
        conn.execute("UPDATE pipelines SET runners=? WHERE id=?", (json.dumps(names) if names else None, p["id"]))
        define._change(conn, p["id"], actor, f"runner {name} {'may run' if on else 'no longer runs'} the agents of its "
                       f"trigger rules", {"runner": name, "allowed": on})
    return {"pipeline": p["name"], "runners": names}


def listing(conn, runs=8):
    """Deploy > Runners and maxpm runner list: each runner with what uses it (the pipelines that tick it, the rules
    that name it), its load, and its last runs; and this computer, which runs every rule that names no runner."""
    pipes = conn.execute("SELECT id, name, runners FROM pipelines ORDER BY name").fetchall()
    rules = conn.execute("SELECT t.id, t.runner, t.enabled, p.name FROM pipeline_triggers t JOIN pipelines p ON "
                         "p.id=t.pipeline_id ORDER BY p.name, t.id").fetchall()
    out = []
    for r in conn.execute("SELECT * FROM runners ORDER BY name"):
        d = _info(r)
        d["used_by"] = [p["name"] for p in pipes if r["name"] in ticked(p)]
        d["rules"] = [{"pipeline": x["name"], "rule": f"t{x['id']}", "on": bool(x["enabled"])} for x in rules
                      if x["runner"] == r["name"]]
        d["busy"] = conn.execute("SELECT COUNT(*) FROM runner_runs WHERE runner=? AND state='running'",
                                 (r["name"],)).fetchone()[0]
        d["waiting"] = conn.execute("SELECT COUNT(*) FROM runner_runs WHERE runner=? AND state='queued'",
                                    (r["name"],)).fetchone()[0]
        d["runs"] = [_run_info(x) for x in conn.execute("SELECT * FROM runner_runs WHERE runner=? ORDER BY id DESC "
                                                        "LIMIT ?", (r["name"], runs))]
        last = conn.execute("SELECT MAX(COALESCE(ended_at, started_at)) FROM runner_runs WHERE runner=?",
                            (r["name"],)).fetchone()[0]
        d["last_seen"] = max(x for x in (last, r["checked_at"]) if x) if (last or r["checked_at"]) else None
        d.pop("key_hash", None)
        from .pipeline import keys  # #6153: the sets every run here gets (the model key), by name
        d["keys"] = [{"set": k["set"], "grant": k["grant"], "secrets": keys._members(conn, k["set"])}
                     for k in keys.grants(conn, runner=r["name"])]
        out.append(d)
    this = {"name": THIS, "rules": [{"pipeline": x["name"], "rule": f"t{x['id']}", "on": bool(x["enabled"])}
                                    for x in rules if x["runner"] == THIS]}
    return {"runners": out, "this": this, "clis": {k: {"label": v["label"], "key": v["key"]} for k, v in CLIS.items()}}


def _run_info(r):
    d = {k: r[k] for k in r.keys() if k not in ("key_hash", "port")}
    if d.get("result") and len(d["result"]) > 300:
        d["result"] = d["result"][:300] + "…"
    return d


def runs(conn, name=None, limit=30):
    q = "SELECT * FROM runner_runs" + (" WHERE runner=?" if name else "") + " ORDER BY id DESC LIMIT ?"
    if name:
        _runner(conn, name)
    return {"runs": [_run_info(r) for r in conn.execute(q, (name, limit) if name else (limit,))]}


def run_for_item(conn, item_id):
    """The newest run of a task, or None: the Agents view says where its agent runs."""
    r = conn.execute("SELECT * FROM runner_runs WHERE item=? ORDER BY id DESC LIMIT 1", (item_id,)).fetchone()
    return _run_info(r) if r else None


# ---------------------------------------------------------------- a run

def queue(conn, name, item_id, pipeline=None, rule=None, rev=None):
    """A rule's task goes to a runner: a session record for it, and the task reserved for that session (so maxpm serve
    starts no session for it here). Returns the words for the rule's result. A task that a session holds or has
    reserved already (a fault that another rule opened) is left with it."""
    r = _runner(conn, name)
    it = core._item(conn, item_id)
    if it["assignee"] or (it["reserved_for"] and (it["reserved_until"] or "9") > _now()):
        return f"; #{item_id} is with {it['assignee'] or it['reserved_for']} already: no run on {name}"
    if conn.execute("SELECT 1 FROM runner_runs WHERE item=? AND state IN ('queued', 'running')", (item_id,)).fetchone():
        return f"; #{item_id} waits for or runs on a runner already"
    with core.tx(conn):
        run_id = conn.execute("INSERT INTO runner_runs(runner, item, agent, pipeline, rule, rev, state, cli, queued_at) "
                              "VALUES (?,?,?,?,?,?,?,?,?)", (name, item_id, "", pipeline, rule, rev, "queued", r["cli"],
                                                             _now())).lastrowid
    agent = f"{name}-r{run_id}"
    core.register(conn, agent, note=f"runner {name}: #{item_id}")
    until = core.iso(core.now() + core.timedelta(minutes=int(r["max_minutes"]) + 60))
    with core.tx(conn):
        conn.execute("UPDATE runner_runs SET agent=? WHERE id=?", (agent, run_id))
        conn.execute("UPDATE agents SET host=? WHERE name=?", (f"runner {name}", agent))
        conn.execute("UPDATE items SET reserved_for=?, reserved_until=?, reserved_by=? WHERE id=?",
                     (agent, until, rule and f"trigger {rule}" or "maxpm", item_id))
        core._event(conn, item_id, "maxpm", f"reserved for {agent}: an agent on runner {name} (run #{run_id})")
    return f"; runs on {name} (run #{run_id})"


def _serve_port():
    if SERVE_PORT is not None:
        return SERVE_PORT
    from . import client
    try:
        with open(client.serve_file(), encoding="utf-8") as f:
            return int(json.load(f)["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _origin(folder):
    if GIT is not None:
        return GIT(folder)
    try:
        r = subprocess.run(["git", "-C", str(folder), "remote", "get-url", "origin"], capture_output=True, text=True,
                           timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def prompt_for(run, item_id, runner_name):
    return (f"You are a MaximizePM agent on the runner {runner_name}, with no person, no terminal and no permission "
            f"prompt. Run `maxpm go`: it gives you task #{item_id}. Do it as its briefing says, in this folder (a "
            f"worktree of the project on the runner). Nobody answers at a prompt: a question for a person goes to the "
            f"queue (maxpm ask, or maxpm add ... --doer human --blocks {item_id} --release), and then you stop. "
            f"Commit on a branch named maxpm/run-{run}, push it to origin (the queue's computer does not see this "
            f"folder), and name the commit in `maxpm done {item_id} --commit <sha> --test \"<command>\" --result "
            f"\"<its last line>\"`. When you cannot finish, `maxpm release {item_id} --note \"<what is done, what is "
            f"left>\"`. Report each step with `maxpm now \"<about 10 words>\"`. Then stop: do not run maxpm wait.")


def _cli_line(r, run, model):
    """The agent CLI command with no terminal: its prompt is in $MAXPM_PROMPT."""
    if (run["cli"] or r["cli"]) == "codex":
        return ('codex exec --json --sandbox workspace-write -c sandbox_workspace_write.network_access=true '
                '--skip-git-repo-check "$MAXPM_PROMPT"')
    allow = " ".join(shlex.quote(t) for t in (r["allow"] or ALLOW).split())
    return ('claude -p "$MAXPM_PROMPT" --output-format stream-json --verbose --permission-mode dontAsk '
            f'--max-budget-usd {float(r["max_cost"]):g} ' + (f"--model {shlex.quote(model)} " if model else "")
            + f"--allowedTools {allow}")


def script_for(r, run, origin, project, port, key, token, model=None, keys_env=None):
    """The shell script that a run sends on stdin (keys stay out of every command line). keys_env: the variables of
    the runner's and the task's credential sets (#6153, pipeline/keys.run_env)."""
    folder = r["folder"] or FOLDER
    base = ('"$HOME"/' + shlex.quote(folder[2:]) if folder.startswith("~/") else shlex.quote(folder)) \
        + "/" + shlex.quote(project)
    start = (f'git -C "$base" fetch -q origin && git -C "$base" cat-file -e {shlex.quote(run["rev"])}^{{commit}} 2>/dev/null '
             f'|| {{ echo "maxpm: the version {run["rev"]} is not on origin: push it, then run the task again"; exit 3; }}; '
             f'start={shlex.quote(run["rev"])}' if run["rev"] else
             'git -C "$base" fetch -q origin HEAD && start=$(git -C "$base" rev-parse FETCH_HEAD)')
    env = {**(keys_env or {}),
           "MAXPM_RUNNER_PORT": str(port), "MAXPM_RUNNER_KEY": key, core.TOKEN_ENV: token, "MAXPM_AGENT": run["agent"],
           "MAXPM_FOCUS": f"item:{run['item']}", "MAXPM_RUNNER": r["name"],
           "MAXPM_AGENT_TYPE": CLIS[run["cli"] or r["cli"]]["type"]}
    prompt = prompt_for(run["id"], run["item"], r["name"])
    return "\n".join([
        "set -e",
        'export PATH="$HOME/.local/bin:$PATH"',
        f"base={base}",
        f'if [ ! -d "$base/.git" ]; then mkdir -p "$(dirname "$base")"; git clone -q {shlex.quote(origin)} "$base"; fi',
        start,
        f'work="$base-runs/run{run["id"]}"',
        'git -C "$base" worktree add -q --detach "$work" "$start"',
        'cd "$work"',
        'mkdir -p "$HOME/.maximizepm/runner"',
        f'echo $$ > "$HOME/.maximizepm/runner/run{run["id"]}.pid"',
        "export " + " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items()),
        "MAXPM_PROMPT=$(cat <<'MAXPM_PROMPT_END'", prompt, "MAXPM_PROMPT_END", ")",
        "export MAXPM_PROMPT",
        f'echo "maxpm: run {run["id"]} of task #{run["item"]} starts in $work at $start"',
        "exec " + _cli_line(r, run, model) + " </dev/null",
        ""])


def _start(conn, run):
    """Start a queued run: its token, its key, and the ssh connection with the script on stdin."""
    r = _runner(conn, run["runner"])
    row = conn.execute("SELECT i.model, p.name AS project, p.path FROM items i JOIN projects p ON p.id=i.project_id "
                       "WHERE i.id=?", (run["item"],)).fetchone()
    port = _serve_port()
    origin = _origin(row["path"]) if row and row["path"] else None
    why = ("its task is gone" if row is None else
           "runner {0} is marked bad: {1}".format(r["name"], r["error"]) if r["state"] == "bad" else
           f"project {row['project']} has no folder here (maxpm project path)" if not row["path"] else
           f"the folder of project {row['project']} has no git remote origin: the runner clones it from there"
           if not origin else "maxpm serve names no port (no serve file)" if not port else None)
    if why:
        return _end(conn, run, "failed", None, why)
    key = secrets.token_urlsafe(24)
    token = core.token_new(conn, run["agent"], by="maxpm serve")
    core.scope_start(conn, run["agent"], [f"tasks:{run['item']}"], f"the task of run #{run['id']} on {r['name']}")
    rport = random.randint(30000, 59999)
    with core.tx(conn):
        # The token's commands come from another computer: its process chain is not this computer's (core.token_session
        # checks the chain only on its own host), and request_for checks the run's key instead.
        conn.execute("UPDATE session_tokens SET host=? WHERE hash=?", (f"runner:{r['name']}", core._token_hash(token)))
    model = row["model"] if (run["cli"] or r["cli"]) == "claude" else None
    # #6153: the runner's keys (the model key) and the keys its rule granted to the task, never a person's sign-in
    from .pipeline import keys
    keys_env, forms, given, left = keys.run_env(conn, run, run["cli"] or r["cli"])
    script = script_for(r, dict(run), origin, row["project"], rport, key, token, model, keys_env)
    path = Path(core.river_dir()) / "runners" / f"run{run['id']}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30", "-R", f"127.0.0.1:{rport}:127.0.0.1:{port}", r["ssh"], "sh -s"]
    try:
        proc = SPAWN(argv, script, str(path)) if SPAWN else _spawn(argv, script, path, forms)
    except OSError as e:
        return _end(conn, run, "failed", None, f"ssh did not start: {e}")
    PROCS[run["id"]] = proc
    said = ("; keys " + ", ".join(f"{g['secret']} as ${g['var']}" for g in given) if given else "; no keys") + (
        "; left out: " + "; ".join(left) if left else "")
    with core.tx(conn):
        conn.execute("UPDATE runner_runs SET state='running', started_at=?, pid=?, port=?, key_hash=?, transcript=? "
                     "WHERE id=?", (_now(), proc.pid, rport, _hash(key), str(path), run["id"]))
        core._event(conn, run["item"], "maxpm", f"run #{run['id']} started on runner {r['name']} ({run['agent']})"
                    + said)
    return {"started": run["id"]}


# #6153: the process between ssh and the transcript when a run has keys. It reads the forms of the key values from a
# file of mode 0600 and deletes it at once, starts ssh (its stdin is this process's: the script), and writes each line
# of ssh's output with the values masked. It is a process of its own, not a thread of maxpm serve, so the run lives
# through a restart of serve as before (serve finds it again by its pid). It exits with ssh's exit code; a SIGTERM
# (stop) ends ssh.
MASK_CODE = r"""
import base64, json, os, signal, subprocess, sys
forms_file, log, argv = sys.argv[1], sys.argv[2], sys.argv[4:]
with open(forms_file) as f:
    forms = [base64.b64decode(x) for x in json.load(f)]
os.unlink(forms_file)
p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
signal.signal(signal.SIGTERM, lambda *a: p.terminate())
with open(log, "ab") as out:
    for line in p.stdout:
        for x in forms:
            line = line.replace(x, b"***")
        out.write(line)
        out.flush()
sys.exit(p.wait())
"""


def _spawn(argv, script, path, forms=None):
    """Start ssh with the script on stdin; its output goes to the transcript file. With forms (#6153: the forms of
    the run's key values), MASK_CODE stands between them, so no key value is kept in the file."""
    if forms:
        import base64
        import sys
        ff = Path(str(path) + ".forms")
        fd = os.open(ff, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump([base64.b64encode(x).decode() for x in forms], f)
        argv = [sys.executable, "-c", MASK_CODE, str(ff), str(path), "--", *argv]
        try:
            with open(path, "ab") as out:  # what the masking process itself says, if it fails
                proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=out,
                                        start_new_session=True)
        except OSError:
            ff.unlink(missing_ok=True)
            raise
    else:
        with open(path, "ab") as out:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=out, stderr=subprocess.STDOUT,
                                    start_new_session=True)
    try:
        proc.stdin.write(script.encode())
        proc.stdin.close()
    except OSError:
        pass
    return proc


def _exit_code(run):
    """The exit code of a running run's ssh process, None while it runs, or 'lost' when this serve lost it (a restart)."""
    proc = PROCS.get(run["id"])
    if proc is not None:
        return proc.poll()
    pid = run["pid"]
    if not pid:
        return "lost"
    try:  # a child of the serve before a restart (an exec of the same process): reap it here
        got, status = os.waitpid(int(pid), os.WNOHANG)
        if got == 0:
            return None
        return os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else status >> 8
    except ChildProcessError:
        return None if core.pid_alive(pid) else "lost"
    except OSError:
        return "lost"


def stop(conn, run_id, actor=None, reason=None, by_serve=False):
    """Stop a run: the agent CLI on the runner (its pid file), then the ssh connection here. A queued run only ends."""
    if not by_serve:
        _may(conn, actor, "stops an agent on a runner")
    run = conn.execute("SELECT * FROM runner_runs WHERE id=?", (int(run_id),)).fetchone()
    if run is None:
        raise RiverError(f"no runner run #{run_id}: maxpm runner runs lists them")
    if run["state"] not in ("queued", "running"):
        raise RiverError(f"run #{run['id']} has ended already ({run['state']})")
    why = (reason or "").strip() or "stopped by a person"
    with core.tx(conn):
        conn.execute("UPDATE runner_runs SET stopped_by=?, stop_reason=? WHERE id=?", (actor or "maxpm", why, run["id"]))
    if run["state"] == "queued":
        return _end(conn, run, "stopped", None, why)
    r = conn.execute("SELECT ssh FROM runners WHERE name=?", (run["runner"],)).fetchone()
    if r:
        host._ssh3(r["ssh"], f'f="$HOME/.maximizepm/runner/run{run["id"]}.pid"; [ -f "$f" ] && kill -TERM "$(cat "$f")"; '
                   f'true', timeout=30)
    proc = PROCS.get(run["id"])
    if proc is not None:
        try:
            proc.terminate()
        except OSError:
            pass
    return {"run": run["id"], "stopping": True, "reason": why}


def _transcript_end(path):
    """(cost, the agent's last words) from a transcript: Claude Code's result line, or Codex's last agent message."""
    cost, last = None, None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-LOG_KEEP:]
    except OSError:
        return None, None
    for line in lines:
        d = _json_line(line)
        if d is None:
            if line.strip():
                last = line.strip()
            continue
        if d.get("type") == "result":
            cost = d.get("total_cost_usd", cost)
            last = d.get("result") or last
        elif d.get("type") == "item.completed" and (d.get("item") or {}).get("type") == "agent_message":
            last = d["item"].get("text") or last
    return cost, last


def _end(conn, run, state, code, why=None):
    """A run ends: its record, its token, its task (back to the queue when it is still open), the manager told."""
    cost, last = _transcript_end(run["transcript"]) if run["transcript"] else (None, None)
    run = conn.execute("SELECT * FROM runner_runs WHERE id=?", (run["id"],)).fetchone()
    if run["stopped_by"]:
        state, why = "stopped", why or run["stop_reason"]
    result = why or last or ("the agent CLI ended" if code == 0 else f"exit {code}")
    PROCS.pop(run["id"], None)
    it = conn.execute("SELECT * FROM items WHERE id=?", (run["item"],)).fetchone()
    held = it is not None and it["assignee"] == run["agent"] and it["status"] in ("in_progress", "held")
    if held:
        try:
            core.release(conn, run["item"], note=f"run #{run['id']} on runner {run['runner']} ended ({state}) before "
                                                 f"the task closed: {result[:300]}", actor=run["agent"])
        except RiverError:
            pass
    with core.tx(conn):
        conn.execute("UPDATE runner_runs SET state=?, ended_at=?, exit_code=?, cost=?, result=? WHERE id=?",
                     (state, _now(), code if isinstance(code, int) else None, cost, result[:4000], run["id"]))
        conn.execute("UPDATE items SET reserved_for=NULL, reserved_until=NULL, reserved_by=NULL WHERE id=? AND "
                     "reserved_for=?", (run["item"], run["agent"]))
        core.token_cancel(conn, run["agent"], f"run #{run['id']} on runner {run['runner']} ended")
        open_ = it is not None and it["status"] not in ("done", "dropped")
        core._event(conn, run["item"], "maxpm", f"run #{run['id']} on runner {run['runner']} {state}: {result[:200]}")
        manager = core.active_manager(conn)
        if open_ and manager and state != "stopped":
            core._send(conn, "alert", "maxpm", f"The agent on runner {run['runner']} (run #{run['id']}) {state} and "
                       f"#{run['item']} is still open: {result[:300]}. Its transcript: maxpm runner log {run['id']}.",
                       to=manager, item_id=run["item"])
    try:
        core.unregister(conn, run["agent"], "maxpm")
    except RiverError:
        pass
    return {"run": run["id"], "state": state, "result": result}


def serve_pass(conn):
    """One pass of maxpm serve: end the runs whose connection ended, stop the ones past max_minutes, keep the leases
    of the others (an agent that works for long without a maxpm command keeps its task), then start queued runs on
    runners with a free slot. Returns {"started", "ended", "stopped"}."""
    out = {"started": [], "ended": [], "stopped": []}
    t = core.now()
    for run in conn.execute("SELECT * FROM runner_runs WHERE state='running' ORDER BY id").fetchall():
        code = _exit_code(run)
        if code is None:
            r = conn.execute("SELECT max_minutes FROM runners WHERE name=?", (run["runner"],)).fetchone()
            most = int(r["max_minutes"]) if r else 60
            if run["stopped_by"]:  # asked to stop, and the connection still lives: end it from this side
                proc = PROCS.get(run["id"])  # only a process of this serve: a pid alone may belong to another now
                if proc is not None:
                    try:
                        proc.terminate()
                    except OSError:
                        pass
                continue
            if (t - core.parse_iso(run["started_at"])).total_seconds() > most * 60:
                stop(conn, run["id"], "maxpm", f"it ran longer than max_minutes ({most})", by_serve=True)
                out["stopped"].append(run["id"])
                continue
            with core.tx(conn):
                core._renew(conn, run["agent"], t)
            continue
        state = "ended" if code == 0 else "failed"
        out["ended"].append(_end(conn, run, state, code, "maxpm serve lost the connection (a restart)" if code == "lost"
                                 else None))
    for run in conn.execute("SELECT * FROM runner_runs WHERE state='queued' ORDER BY id").fetchall():
        r = conn.execute("SELECT * FROM runners WHERE name=?", (run["runner"],)).fetchone()
        if r is None:
            out["ended"].append(_end(conn, run, "failed", None, f"runner {run['runner']} is no longer on the list"))
            continue
        busy = conn.execute("SELECT COUNT(*) FROM runner_runs WHERE runner=? AND state='running'",
                            (r["name"],)).fetchone()[0]
        if busy >= int(r["slots"]):
            continue
        res = _start(conn, run)
        if res.get("started"):
            out["started"].append(run["id"])
        else:
            out["ended"].append(res)
    return out


# ---------------------------------------------------------------- commands from a runner

def request_for(conn, key, req):
    """POST /api/runner-run: a command of an agent on a runner, made into a command of this computer. The key must be
    the key of a run that runs now, and the session token (MAXPM_TOKEN) that run's session. The command then runs on
    this computer's queue, in the project's folder here, with only the session's own variables: nothing else of the
    runner's environment reaches serve."""
    if not key:
        raise RiverError("a command from a runner carries its run's key")
    run = conn.execute("SELECT * FROM runner_runs WHERE key_hash=? AND state='running'", (_hash(key),)).fetchone()
    if run is None:
        raise RiverError("this key is not of a run that runs now: the run ended or was stopped")
    env = req.get("env") if isinstance(req.get("env"), dict) else {}
    tok = env.get(core.TOKEN_ENV) or ""
    who = core.token_session(conn, tok, None) if tok else None
    if not who or who["agent"] != run["agent"]:
        raise RiverError(f"a command of run #{run['id']} carries the token of its session {run['agent']}")
    row = conn.execute("SELECT p.path FROM items i JOIN projects p ON p.id=i.project_id WHERE i.id=?",
                       (run["item"],)).fetchone()
    cwd = row["path"] if row and row["path"] and os.path.isdir(row["path"]) else str(Path.home())
    from . import client
    keep = {k: env[k] for k in ("MAXPM_QUIET", "MAXPM_MODEL") if env.get(k)}
    out = {k: v for k, v in req.items() if k not in ("env", "cwd", "stamp", "ppid", "pid", "chain", "caller")}
    out["env"] = {**keep, core.TOKEN_ENV: tok, "MAXPM_AGENT": run["agent"], "MAXPM_FOCUS": f"item:{run['item']}",
                  "MAXPM_RUNNER": run["runner"], "MAXPM_AGENT_TYPE": CLIS[run["cli"] or "claude"]["type"],
                  **({"MAXPM_DB": os.environ["MAXPM_DB"]} if os.environ.get("MAXPM_DB") else {}),
                  "HOME": str(Path.home()), "PATH": os.environ.get("PATH", "")}
    out.update(cwd=cwd, stamp=client.code_stamp(), ppid=None, pid=None)
    return out


# ---------------------------------------------------------------- the transcript

def _json_line(line):
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        d = json.loads(line)
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def _short(v, n=200):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n] + "…"


def readable(line):
    """One transcript line in words: (kind, text), or None for a line that says nothing to a person. Claude Code's
    stream-json and Codex's --json events (their shapes as their CLIs print them); any other line as it is."""
    d = _json_line(line)
    if d is None:
        return ("out", line.rstrip()) if line.strip() else None
    t = d.get("type")
    if t == "assistant":
        parts = []
        for c in (d.get("message") or {}).get("content") or []:
            if c.get("type") == "text" and c.get("text", "").strip():
                parts.append(("say", c["text"].strip()))
            elif c.get("type") == "tool_use":
                inp = c.get("input") or {}
                parts.append(("tool", f"{c.get('name')}: {_short(inp.get('command') or inp.get('file_path') or inp)}"))
        return parts or None
    if t == "result":
        cost = d.get("total_cost_usd")
        return ("end", f"{'failed' if d.get('is_error') else 'finished'}" + (f" · ${cost:.2f}" if cost else "")
                + (f": {_short(d.get('result') or '', 400)}" if d.get("result") else ""))
    if t == "system" and d.get("subtype") == "init":
        return ("start", f"{d.get('model') or 'the model'} in {d.get('cwd') or 'its folder'}")
    if t == "item.completed":
        it = d.get("item") or {}
        if it.get("type") == "agent_message":
            return ("say", it.get("text") or "")
        if it.get("type") == "command_execution":
            return ("tool", f"{_short(it.get('command') or '')} (exit {it.get('exit_code')})")
        return None
    if t == "turn.completed":
        u = d.get("usage") or {}
        return ("end", f"turn done · {u.get('input_tokens', 0)} tokens in, {u.get('output_tokens', 0)} out")
    if t == "error":
        return ("out", _short(d.get("message") or d))
    return None


def log(conn, run_id, n=LOG_KEEP):
    """maxpm runner log <run> and the page's Watch: the run, and its transcript in words (the last n lines)."""
    run = conn.execute("SELECT * FROM runner_runs WHERE id=?", (int(run_id),)).fetchone()
    if run is None:
        raise RiverError(f"no runner run #{run_id}: maxpm runner runs lists them")
    lines = []
    if run["transcript"]:
        try:
            with open(run["transcript"], encoding="utf-8", errors="replace") as f:
                raw = f.readlines()[-max(1, int(n)):]
        except OSError:
            raw = []
        for line in raw:
            got = readable(line)
            for kind, text in (got if isinstance(got, list) else [got] if got else []):
                lines.append({"kind": kind, "text": text})
    return {"run": _run_info(run), "lines": lines}
