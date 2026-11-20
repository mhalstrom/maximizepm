"""An agent job inside a forge's own CI (#6152): when a stage that runs a GitHub workflow fails, GitHub starts the
agent in its own runner, and MaximizePM reads the job's state and proof back.

mark 2026-11-18 (canvas release-tracking, Plan 2 row G; goal part 2: "hooks for getting agents running on deployment
tasks and issues" from any pipeline system). A GitHub runner is outside the tailnet, and GitHub cannot call this
computer, so the job does not reach the queue while it runs. The way back is the pipeline's own read of GitHub (the
github plugin, once a minute), the same path that brings the stage runs:

    maxpm runner github <pipeline>     writes .github/workflows/agent.yml into a checkout of the repository, and sets
                                       the pipeline's stage-failed rules to run their agent in GitHub Actions

1. A stage's workflow fails on the branch the pipeline follows. GitHub starts the job maxpm-agent (on workflow_run),
   in the same kind of runner, at the failed commit. Its run-name, 'maxpm agent <failed run id>', links the two.
2. The plugin's poll reads the failed run; the stage-failed rule opens the fault task and, because the rule runs on
   'github', queue() below makes a runner run for it (runner_runs, runner 'github'): the task is reserved for the
   agent record github-r<run>, so maxpm serve starts no session here for it.
3. The job reads the failed log, runs the agent CLI with no terminal and no prompt (the model key is a repository
   secret that a person sets: policy g6, nobody searches for keys), and the agent commits its fix. The job then runs
   the proof command with no token, and keeps the fix as a git bundle. Its token only reads: a step can read a job's
   token from the runner's memory (#6177). The job maxpm-push, on a new runner with a clean checkout, pushes the
   bundle's commits to the branch maxpm/fix-<failed run id> with no hook, opens a pull request, and uploads the
   artifact maxpm-agent: proof.json and the transcript.
4. Each poll asks the plugin about the open runs (open_runs): the plugin answers "agents" with the job's state, and
   at its end the proof and the transcript; take() keeps them. The run ends as a runner run ends (runners._end): the
   task, still open, goes back to the queue with the proof in a note, and the manager hears of it. The session that
   takes the task checks the pull request and merges it.

A rule on 'github' whose event or stage did not run in GitHub (a command stage of this computer), or ran on another
branch than the one the pipeline follows, falls back to a session here. A run whose job GitHub did not show within
QUEUE_MAX ends as failed, and its task goes back too.

GitLab CI (#6172) takes the same path with its own file:

    maxpm runner gitlab <pipeline>     writes the job maxpm-agent into .gitlab-ci.yml of a checkout (between two marked
                                       lines, so a second run replaces it), and moves the rules as for GitHub

The job is in GitLab's own last stage .post with when: on_failure, so GitLab starts it in the pipeline of the failed
commit, after a job failed. Its key is the stage run's own: the failed stage run gl-<pipeline id>-<stage> and the job
are in one GitLab pipeline, and the gitlab plugin finds the job maxpm-agent among that pipeline's jobs. GitLab gives
every CI/CD variable to every job, so the agent and the proof command run as another user of the job (#6179, see
_isolate): only the model key goes in. The CI/CD variable MAXPM_GITLAB_TOKEN (a project access token that a person
sets, policy g6) reads the failed jobs' logs and pushes the fix; push options open the merge request. The artifact
holds the proof and the transcript, and the plugin reads the two files by path.

Pulumi Delivery (#6173). Delivery has no step that runs when a stage fails, no log of a job by its API, and no way to
call this computer (Pulumi's docs, read 2026-11-19: stages run only after their upstream stages succeeded; a job that
fails fails its stage). So MaximizePM starts the job itself, as a Pulumi Deployment, the hosted runner of Pulumi Cloud:

    maxpm runner pulumi <pipeline> --env <ESC environment>    keeps the job's settings (pipelines.agent_job) and sets
                                                              the stage-failed rules to run their agent in Pulumi

1. The pulumi plugin's poll reads a failed Delivery stage (key pd-<run>-<stage>); the rule opens the fault task, and
   queue() makes a runner run for it, as for GitHub.
2. The next poll asks the plugin about the open runs; a run with no job carries "start", the whole request of the
   deployment (pulumi_request): operation preview on the agent stack (--stack; the pipeline stack when none), whose
   deployment settings a person set in Pulumi Cloud (the source repository and its branch). Its pre-run commands
   write the job's script and run it inside `pulumi env run <the ESC environment>`: the model key is there, set by a
   person (policy g6), and also MAXPM_GIT_TOKEN when the fix should go back as a branch. The plugin answers the
   deployment's id as the run's job.
3. The script saves what Pulumi shows of the failed Delivery run, runs the agent with no terminal (as another user,
   which cannot read the git token or the Pulumi token, #6179), runs --test, and pushes the commit to maxpm/fix-pd-<run>-<stage>
   with a pull request on GitHub; with no git token the fix comes back as a patch. It prints its proof, the end of the
   transcript and the patch as '::maxpm-' lines of the deployment's log, and always ends with 0 (the preview after it
   changes nothing).
4. Each poll reads the deployment's status; at its end the plugin reads its log and answers the proof, the
   transcript and the patch; take() keeps them, as for GitHub.

Standard library only."""

from __future__ import annotations

import base64
import json
import re
import shlex
import subprocess
from pathlib import Path

from .. import __version__, core, runners
from ..core import RiverError
from . import store

FORGES = {"github": "GitHub Actions", "gitlab": "GitLab CI",  # a rule's runner that is a forge's own CI: its label
          "pulumi": "Pulumi Deployments"}
SOURCE = {"github": "GitHub", "gitlab": "GitLab", "pulumi": "Pulumi Delivery"}  # the pipeline's source each one needs
KEY = {"github": "gh-run-", "gitlab": "gl-", "pulumi": "pd-"}  # the start of the key of a stage run that ran there
WORKFLOW = "agent.yml"  # the workflow file of the agent job, and the setting 'agent' of the github plugin
FILE = {"github": f".github/workflows/{WORKFLOW}", "gitlab": ".gitlab-ci.yml"}  # where the job is, in the checkout
CHANGE = {"github": "pull request", "gitlab": "merge request", "pulumi": "pull request"}  # what the job opens
IMAGE = "node:20"  # GitLab: the job's image (it has npm, git and python3); maxpm runner gitlab --image changes it
TOKEN_VAR = "MAXPM_GITLAB_TOKEN"  # GitLab: the CI/CD variable with a project access token (the logs and the push)
JOB = "maxpm-agent"     # the job's name, and the name of its artifact
PUSH_JOB = "maxpm-push"  # GitHub: the job after the agent's, on a new runner, that pushes the fix (#6177)
WORK = "maxpm-agent-work"  # GitHub: the agent job's artifact for the push job (the fix as a git bundle, the logs)
OUT = "/tmp/maxpm-agent"  # the job's own files on the runner (the log, the transcript, the proof): the artifact
QUEUE_MAX = 30 * 60     # seconds: a run whose job the forge did not show by then fails
RUN_MAX = 6 * 3600      # seconds: GitHub ends a job after 6 hours
TRANSCRIPT_MAX = 1 << 20  # bytes of a transcript kept
# What the agent may use with no prompt in the job: read and change files, and the git commands that make a commit.
# It gets no push: the job pushes its commit after it, with a token that the agent's step does not hold.
ALLOW = ("Read Edit Write Glob Grep Bash(git status:*) Bash(git diff:*) Bash(git log:*) Bash(git show:*) "
         "Bash(git add:*) Bash(git commit:*)")
# The repository secret of the model key. Antigravity CLI (agy, #6249) takes a Gemini API key with its model provider
# gemini (antigravity.google/docs/install), which agy_line sets for the run alone.
SECRET = {"claude": "ANTHROPIC_API_KEY", "codex": "CODEX_API_KEY", "agy": "GEMINI_API_KEY"}
PACKAGE = {"claude": "@anthropic-ai/claude-code", "codex": "@openai/codex"}  # npm, inside the job only
# agy is no npm package: Google's script puts its one program into the folder that --dir names (its install.sh, read
# 2026-11-20). The jobs put it where the agent's own user reads it.
AGY_INSTALL = "curl -fsSL https://antigravity.google/cli/install.sh | bash -s -- --dir {dir}"


def install_line(cli, folder="/usr/local/bin"):
    """The shell command that installs the job's agent CLI: npm for Claude Code and Codex; agy into `folder`."""
    if cli == "agy":
        return AGY_INSTALL.format(dir=folder)
    return f"npm install -g {PACKAGE[cli]}"


def agy_rules(tools):
    """Claude Code's tool rules as agy's permission rules (#6249): Bash(<command>:*) and Bash(<command>) become
    command(<command>), the form agy took for maxpm (river/agents/antigravity.py ALLOW_MAXPM). agy's --mode
    accept-edits takes the file tools (Read, Edit, Write, Glob, Grep); another rule has no agy form and is left out.
    Not measured yet: a rule of several words (command(git commit)) matching a command that starts with them."""
    out = []
    for t in tools:
        m = re.match(r"^Bash\((.+?)(?::\*)?\)$", t.strip())
        if m and f"command({m.group(1).strip()})" not in out:
            out.append(f"command({m.group(1).strip()})")
    return out


def agy_line(tools, deny=(), provider=True):
    """agy in print mode with no terminal (#6249): one JSON object at its end (conversation_id, status, response,
    error), file changes with no question (--mode accept-edits), and the commands of `tools` allowed and of `deny`
    refused, for this run alone (--settings, never saved). provider: the model provider gemini, which takes
    GEMINI_API_KEY; False: agy's own sign-in."""
    settings = {"permissions": {"allow": agy_rules(tools), **({"deny": agy_rules(deny)} if deny else {})}}
    if provider:
        settings["modelProvider"] = "gemini"
    return ('agy -p "$MAXPM_PROMPT" --output-format json --mode accept-edits --settings '
            + shlex.quote(json.dumps(settings, separators=(",", ":"))))
_SECRET_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,99}$")
SCRIPT = "/tmp/maxpm-agent.sh"  # the Pulumi job's script, which its first pre-run command writes
PATCH_MAX = 300_000  # bytes of a patch that a Pulumi job prints in its log
_ESC_ENV = re.compile(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+){1,2}$")
_STACK = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")


def label(name):
    return FORGES.get(name, name)


def choices(p):
    """The forge runners that pipeline p may name: its source's own CI."""
    return [p["forge"]] if p["forge"] in FORGES else []


def conf(p):
    """The settings of a pipeline's agent job in Pulumi (maxpm runner pulumi), or None."""
    try:
        c = json.loads(p["agent_job"] or "null")
    except (ValueError, TypeError, IndexError, KeyError):
        return None
    return c if isinstance(c, dict) and c.get("stack") else None


# ---------------------------------------------------------------- a run

def queue(conn, p, rule, item_id, ev):
    """A stage-failed rule on a forge acted: a runner run that waits for the forge's job of the failed run. Returns
    the words for the rule's result. A run that did not come from the forge falls back to a session here."""
    forge = rule["runner"]
    here = f"; a session here takes #{item_id}"
    if ev["event"] != "stage-failed":
        return f"; {label(forge)} starts its agent job only when a stage fails{here}"
    row = conn.execute("SELECT r.key, s.workflow, v.ref FROM stage_runs r JOIN pipeline_stages s ON s.id=r.stage_id "
                       "LEFT JOIN pipeline_versions v ON v.id=r.version_id WHERE r.id=?", (ev.get("run_id"),)).fetchone()
    if row is None or not row["workflow"] or not (row["key"] or "").startswith(KEY.get(forge, "-")):
        return f"; stage {ev['stage']} did not run in {label(forge)}{here}"
    ref = p["ref"] or "main"
    if forge != "pulumi" and row["ref"] and row["ref"] != ref:  # the job runs only on the branch the pipeline follows
        return f"; it failed on {row['ref']}, and the job {JOB} runs only on {ref}{here}"
    if forge == "pulumi" and not conf(p):  # Pulumi's ref is the first run number; the job needs its settings
        return f"; no agent job is set for {label(forge)} (maxpm runner pulumi {p['name']} --env <ESC environment>){here}"
    it = core._item(conn, item_id)
    if it["assignee"] or (it["reserved_for"] and (it["reserved_until"] or "9") > runners._now()):
        return f"; #{item_id} is with {it['assignee'] or it['reserved_for']} already: no job in {label(forge)}"
    if conn.execute("SELECT 1 FROM runner_runs WHERE item=? AND state IN ('queued', 'running')", (item_id,)).fetchone():
        return f"; #{item_id} waits for or runs on a runner already"
    with core.tx(conn):
        run_id = conn.execute("INSERT INTO runner_runs(runner, item, agent, pipeline, rule, rev, state, queued_at, "
                              "forge_key) VALUES (?,?,?,?,?,?,?,?,?)",
                              (forge, item_id, "", p["name"], f"t{rule['id']}", ev.get("rev"), "queued",
                               runners._now(), row["key"])).lastrowid
    runners._reserve(conn, run_id, forge, item_id, (QUEUE_MAX + RUN_MAX) // 60, f"t{rule['id']}",
                     f"the job {JOB} in {label(forge)}")
    return f"; the job {JOB} in {label(forge)} works on it (run #{run_id})"


def open_runs(conn, p):
    """What a poll asks the plugin about: the runs of pipeline p that wait for or run in its forge's job."""
    if p["forge"] not in FORGES:
        return []
    c = conf(p) if p["forge"] == "pulumi" else None
    out = []
    for r in conn.execute("SELECT * FROM runner_runs WHERE runner=? AND pipeline=? AND state IN ('queued', "
                          "'running') ORDER BY id", (p["forge"], p["name"])):
        d = {"run": r["id"], "key": r["forge_key"], "state": r["state"], "job": r["job"], "since": r["queued_at"]}
        if p["forge"] == "pulumi" and c:  # #6173: MaximizePM starts the Pulumi job; the plugin sends the request
            d["stack"] = c["stack"]
            if not r["job"]:
                d["start"] = pulumi_request(p, c, r["forge_key"], _does(conn, r["rule"]))
        out.append(d)
    return out


def _does(conn, rule):
    """What the rule that queued a run asks its agent to do."""
    m = re.match(r"t(\d+)$", rule or "")
    row = conn.execute("SELECT does FROM pipeline_triggers WHERE id=?", (int(m[1]),)).fetchone() if m else None
    return (row["does"] if row else None) or "Find the cause of the failure and fix it."


STATES = ("queued", "running", "ended", "failed", "stopped")


def take(conn, p, answers, plugin_name):
    """The "agents" of a plugin's answer: [{"run", "state", "job", "url", "started_at", "proof", "transcript",
    "note"}]. A run that started is marked running; a run that ended keeps its proof and transcript and ends
    (runners._end). Returns the runs that ended."""
    if not isinstance(answers, list) or not all(isinstance(a, dict) for a in answers):
        raise RiverError(f"plugin {plugin_name}: \"agents\" is a list of {{\"run\": <id>, \"state\": "
                         f"{'|'.join(STATES)}, ...}} objects")
    ended = []
    for a in answers:
        run = conn.execute("SELECT * FROM runner_runs WHERE id=? AND pipeline=? AND runner=? AND state IN ('queued', "
                           "'running')", (a.get("run"), p["name"], p["forge"])).fetchone()
        if run is None:
            continue  # ended here already (stopped, or too old), or not a run of this pipeline
        state = a.get("state")
        sets = {k: str(a[k])[:500] for k in ("job", "url") if isinstance(a.get(k), str) and a[k]}
        if isinstance(a.get("note"), str) and a["note"] and state not in STATES[2:]:
            sets["result"] = a["note"][:500]  # such as: the workflow is not in the repository yet
        started = state in ("running",) + STATES[2:] and run["state"] == "queued"
        if started:
            sets.update(state="running", started_at=_at(a.get("started_at")))
        if sets:
            with core.tx(conn):
                conn.execute("UPDATE runner_runs SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?",
                             [*sets.values(), run["id"]])
                if started:
                    core._event(conn, run["item"], "maxpm", f"run #{run['id']}: the job {JOB} started in "
                                                            f"{label(run['runner'])}" + (f" ({sets['url']})" if
                                                                                         sets.get("url") else ""))
        if state in STATES[2:]:
            ended.append(_finish(conn, run["id"], state, a))
    return ended


def _at(text):
    try:
        return core.iso(core.parse_iso(text)) if text else runners._now()
    except (TypeError, ValueError):
        return runners._now()


def _finish(conn, run_id, state, a):
    """The job ended: its transcript into the run's file, its proof on the run and on the task, then the end."""
    run = conn.execute("SELECT * FROM runner_runs WHERE id=?", (run_id,)).fetchone()
    proof = a.get("proof") if isinstance(a.get("proof"), dict) else {}
    path = run["transcript"]
    if isinstance(a.get("transcript"), str) and a["transcript"]:
        path = Path(core.river_dir()) / "runners" / f"run{run['id']}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(a["transcript"][-TRANSCRIPT_MAX:], encoding="utf-8")
        path = str(path)
    if isinstance(a.get("patch"), str) and a["patch"].strip():  # #6173: a Pulumi job with no git token
        patch = Path(core.river_dir()) / "runners" / f"run{run['id']}.patch"
        patch.parent.mkdir(parents=True, exist_ok=True)
        patch.write_text(a["patch"][:PATCH_MAX], encoding="utf-8")
        proof = {**proof, "patch": str(patch)}
    _cost, last = runners._transcript_end(path) if path else (None, None)
    change = CHANGE.get(run["runner"], "pull request")
    words = proof_text(proof, last, a.get("note"), change)
    if proof.get("pr"):
        check = f"read the {change}, check that its tests pass, merge it"
    elif proof.get("patch"):
        check = (f"apply the patch in a worktree of the repository (git am {proof['patch']}), run the tests, and "
                 f"commit it")
    else:
        check = f"read the branch {proof.get('branch') or 'of the fix'}, check that its tests pass, merge it"
    with core.tx(conn):
        conn.execute("UPDATE runner_runs SET transcript=?, proof=? WHERE id=?",
                     (path, json.dumps(proof)[:20000] if proof else None, run["id"]))
        if proof.get("commit") or proof.get("pr"):
            core._send(conn, "note", run["agent"],
                       f"The agent job in {label(run['runner'])} (run #{run['id']}"
                       + (f", {run['url']}" if run["url"] else "") + f") made a fix: {words}. Check it: {check}, "
                       f"then close this task. Its transcript: maxpm runner log {run['id']}.", item_id=run["item"])
    code = proof.get("agent_exit") if isinstance(proof.get("agent_exit"), int) else None
    run = conn.execute("SELECT * FROM runner_runs WHERE id=?", (run["id"],)).fetchone()  # with its transcript
    return runners._end(conn, run, state, code, words)


def proof_text(proof, last=None, note=None, change="pull request"):
    """The proof of a job in one line: what it committed, where, and the test's result; or that it fixed nothing."""
    if proof.get("commit"):
        text = f"proof ready: commit {str(proof['commit'])[:12]}"
        if proof.get("branch"):
            text += f" on branch {proof['branch']}"
        if proof.get("pr"):
            text += f", {change} {proof['pr']}"
        if proof.get("patch"):
            text += f", the patch {proof['patch']}"
        test = proof.get("test") if isinstance(proof.get("test"), dict) else {}
        if test.get("command"):
            text += f"; {test['command']}: " + ("passed" if test.get("exit") == 0 else f"exit {test.get('exit')}")
            if test.get("last"):
                text += f" ({str(test['last'])[:200]})"
    else:
        text = "no fix committed"
    if proof.get("error"):
        text += f"; {str(proof['error'])[:300]}"
    if note and not proof:
        text += f"; {note}"
    if last:
        text += f". The agent's last words: {str(last)[:600]}"
    return text


def serve_pass(conn):
    """One pass of maxpm serve (from runners.serve_pass): a run whose job did not show within QUEUE_MAX, or did not
    end within RUN_MAX, fails and gives its task back; the others keep their record alive."""
    out = []
    t = core.now()
    q = ",".join("?" * len(FORGES))
    for run in conn.execute(f"SELECT * FROM runner_runs WHERE state IN ('queued', 'running') AND runner IN ({q}) "
                            f"ORDER BY id", tuple(FORGES)).fetchall():
        age = (t - core.parse_iso(run["started_at"] or run["queued_at"])).total_seconds()
        if run["state"] == "queued" and age > QUEUE_MAX and run["runner"] == "pulumi":
            out.append(runners._end(conn, run, "failed", None, (run["result"] + "; " if run["result"] else "") +
                                    f"no deployment of the agent job started in Pulumi within {QUEUE_MAX // 60}m: "
                                    f"read the pipeline's last poll (maxpm pipeline show {run['pipeline']}) and the "
                                    f"job's settings (maxpm runner pulumi {run['pipeline']} --print)"))
        elif run["state"] == "queued" and age > QUEUE_MAX:
            where = (f"is .github/workflows/{WORKFLOW} on the default branch (maxpm runner github {run['pipeline']}), "
                     f"and its model key set as a repository secret?" if run["runner"] == "github" else
                     f"is the job {JOB} in .gitlab-ci.yml of the failed commit (maxpm runner {run['runner']} "
                     f"{run['pipeline']}), and its model key set as a CI/CD variable?")
            out.append(runners._end(conn, run, "failed", None, (run["result"] + "; " if run["result"] else "") +
                                    f"{label(run['runner'])} showed no job {JOB} for the failed run within "
                                    f"{QUEUE_MAX // 60}m: {where}"))
        elif run["state"] == "running" and age > RUN_MAX + 600:
            out.append(runners._end(conn, run, "failed", None, f"the job {JOB} reported no end within "
                                                                f"{RUN_MAX // 3600}h"))
        else:
            with core.tx(conn):
                core._renew(conn, run["agent"], t)
    return out


def stop(conn, run, actor, why):
    """Stop a forge run: ask the forge to cancel its job (through the pipeline's plugin), then end it here."""
    with core.tx(conn):
        conn.execute("UPDATE runner_runs SET stopped_by=?, stop_reason=? WHERE id=?", (actor or "maxpm", why, run["id"]))
    if run["job"]:
        from . import plugin
        try:
            p = store._pipeline(conn, run["pipeline"])
            m = plugin.of(p)
            plugin._trusted(conn, m, p)
            req = plugin._request(conn, p, m, "stop")
            req["run"] = {"id": run["id"], "key": run["job"], "stage": None, "version": run["rev"],
                          "work": WORKFLOW if run["runner"] == "github" else JOB}
            if run["runner"] == "pulumi":  # #6173: the deployment runs on the agent stack, not on the pipeline stack
                req["agent_stack"] = (conf(p) or {}).get("stack")
            plugin.call(m, req)
        except RiverError as e:
            why += f"; cancel the job on its page{': ' + run['url'] if run['url'] else ''} ({e})"
    res = runners._end(conn, run, "stopped", None, why)
    return {"run": run["id"], "state": res["state"], "reason": why}


def listing(conn, runs=8):
    """Deploy > Runners: each forge's CI that a pipeline may use, with the rules that name it and its last runs."""
    out = []
    for name, text in FORGES.items():
        pipes = [r["name"] for r in conn.execute("SELECT name FROM pipelines WHERE forge=? ORDER BY name", (name,))]
        if not pipes:
            continue
        rules = [{"pipeline": x["name"], "rule": f"t{x['id']}", "on": bool(x["enabled"])} for x in conn.execute(
            "SELECT t.id, t.enabled, p.name FROM pipeline_triggers t JOIN pipelines p ON p.id=t.pipeline_id WHERE "
            "t.runner=? ORDER BY p.name, t.id", (name,))]
        count = lambda s: conn.execute("SELECT COUNT(*) FROM runner_runs WHERE runner=? AND state=?",  # noqa: E731
                                       (name, s)).fetchone()[0]
        last = conn.execute("SELECT MAX(COALESCE(ended_at, started_at)) FROM runner_runs WHERE runner=?",
                            (name,)).fetchone()[0]
        out.append({"name": name, "label": text, "pipelines": pipes, "used_by": sorted({x["pipeline"] for x in rules}),
                    "rules": rules, "busy": count("running"), "waiting": count("queued"), "last_seen": last,
                    "what": ("a deployment of the agent stack that MaximizePM starts when a Delivery stage fails"
                             if name == "pulumi" else f"the job {JOB} in {FILE[name]} of each "
                             + ("repository" if name == "github" else "project")),
                    "runs": [runners._run_info(x) for x in conn.execute(
                        "SELECT * FROM runner_runs WHERE runner=? ORDER BY id DESC LIMIT ?", (name, runs))]})
    return out


# ---------------------------------------------------------------- maxpm runner github <pipeline>

def _git(folder, *args):
    try:
        r = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def same_repo(url, repo, host="github.com"):
    """Whether a git address is the repository owner/name (GitLab: group/subgroup/project) on host: an https
    address, ssh://git@host:2222/..., or git@host:...."""
    m = re.search(rf"{re.escape(host)}(?::\d+)?[:/]+(.+?)(?:\.git)?/?$", url or "", re.I)
    return bool(m) and m.group(1).lower() == (repo or "").lower()


def workflow_name(folder, file):
    """The name GitHub gives a workflow (workflow_run names workflows by it): its top-level name, else its path."""
    path = f".github/workflows/{file}"
    try:
        text = (Path(folder) / path).read_text(encoding="utf-8") if folder else ""
    except OSError:
        text = ""
    for line in text.splitlines():
        m = re.match(r"name:\s*(.+?)\s*$", line)
        if m:
            return m.group(1).strip("'\"")
    return path


def tool_list(text):
    """The tools of a list such as 'Read Edit Bash(git commit:*)' or 'Read, Bash(git log:*)': a rule in parentheses
    keeps its spaces."""
    return re.findall(r"[A-Za-z_][A-Za-z0-9_.-]*(?:\([^)]*\))?", text or "")


def _yaml_str(s):
    return json.dumps(s)  # a JSON string is a YAML string


def prompt(does, test):
    """The agent's instructions in the job ('${{' is taken out: GitHub would read it as an expression)."""
    text = ("You are a MaximizePM agent in a GitHub Actions job, with no person, no terminal and no permission "
            "prompt. The workflow ${{ github.event.workflow_run.name }} failed for commit "
            "${{ github.event.workflow_run.head_sha }} on ${{ github.event.workflow_run.head_branch }}: "
            f"${{{{ github.event.workflow_run.html_url }}}}. Its failed log is in the file {OUT}/failed.log. This "
            "folder is a checkout of that commit.\n\n"
            + _task(does.replace('${{', '$ {{'), test, "the log and the code"))
    return text


def _task(does, test, where):
    """The part of the agent's instructions that the GitHub and Pulumi jobs share: what to do, the proof, the commit."""
    return (f"What to do: {does}\n\n"
            f"Find the cause in {where}, and fix it in this folder. "
            + (f"Prove the fix: run `{test}`. " if test else "")
            + "Commit the fix with a message that says what you changed and why (git add, git commit). Do not push: "
            "the job pushes your commit to a branch and opens a pull request after you. When you cannot fix it, "
            "commit nothing. End with a short report: the cause, what you changed, and the proof.")


def _tools(allow, test):
    tools = tool_list(allow or ALLOW)
    if test and not allow:
        tools.append(f"Bash({shlex.split(test)[0]}:*)")
    return tools


def _agent_cmd(cli, tools, max_cost):
    """The shell line that runs the agent CLI with no terminal (GitHub and Pulumi); its transcript goes to
    $MAXPM_OUT."""
    if cli == "codex":
        return ('codex exec --json --sandbox workspace-write --skip-git-repo-check "$MAXPM_PROMPT" '
                '> "$MAXPM_OUT/transcript.jsonl" 2> "$MAXPM_OUT/agent.err" || echo $? > "$MAXPM_OUT/agent-exit"')
    if cli == "agy":
        return (agy_line(tools) + ' > "$MAXPM_OUT/transcript.jsonl" 2> "$MAXPM_OUT/agent.err" '
                '|| echo $? > "$MAXPM_OUT/agent-exit"')
    return ('claude -p "$MAXPM_PROMPT" --output-format stream-json --verbose --permission-mode dontAsk '
            f'--max-budget-usd {float(max_cost):g} --add-dir {OUT} --allowedTools '
            + " ".join(shlex.quote(t) for t in tools)
            + ' > "$MAXPM_OUT/transcript.jsonl" 2> "$MAXPM_OUT/agent.err" || echo $? > "$MAXPM_OUT/agent-exit"')


def workflow_text(pipeline, repo, ref, names, cli="claude", secret=None, test=None, max_minutes=30, max_cost=5.0,
                  allow=None, does=None):
    """The file .github/workflows/agent.yml of the job."""
    secret = secret or SECRET[cli]
    agent = _agent_cmd(cli, _tools(allow, test), max_cost)
    run = "${{ github.event.workflow_run.id }}"
    text = prompt(does or "Find the cause of the failure and fix it.", test)
    lines = [
        f"# The MaximizePM agent job of pipeline {pipeline} (#6152), written by maxpm {__version__}:",
        f"#   maxpm runner github {pipeline}",
        "# When a workflow below fails on the branch the pipeline follows, an agent looks for the cause and fixes it",
        "# on a branch, with a pull request. MaximizePM reads this job's state and its artifact maxpm-agent (the proof",
        "# and the transcript). Write it again with the command; a change by hand is lost then.",
        f"# The model key: the repository secret {secret} (Settings > Secrets and variables > Actions).",
        f"name: {JOB}",
        f"run-name: maxpm agent {run}",
        "on:",
        "  workflow_run:",
        f"    workflows: [{', '.join(_yaml_str(n) for n in names)}]",
        "    types: [completed]",
        "permissions: {}",
        "env:",
        f"  MAXPM_OUT: {OUT}",
        f"  MAXPM_TEST: {_yaml_str(test or '')}",
        "  FAILED: ${{ github.event.workflow_run.head_sha }}",
        "concurrency:",
        f"  group: {JOB}-{run}",
        "jobs:",
        f"  {JOB}:",
        "    # Only a failure on the branch the pipeline follows, of a commit of this repository (never a fork's code",
        "    # with this job's rights), and never a fix branch of this job.",
        "    if: >-",
        "      github.event.workflow_run.conclusion == 'failure' &&",
        "      github.event.workflow_run.head_branch == '" + ref.replace("'", "''") + "' &&",
        "      github.event.workflow_run.head_repository.full_name == github.repository",
        "    runs-on: ubuntu-latest",
        f"    timeout-minutes: {int(max_minutes)}",
        "    # The agent's code runs in this job, and any step of a job can read the job's token from the runner's",
        "    # memory (sudo): this token only reads. The job after it pushes, on a new runner (#6177).",
        "    permissions:",
        "      contents: read",
        "      actions: read",
        "    steps:",
        "      - uses: actions/checkout@v4",
        "        with:",
        "          ref: ${{ github.event.workflow_run.head_sha }}",
        "          persist-credentials: false",
        "      - name: Read the failure",
        "        env:",
        "          GH_TOKEN: ${{ github.token }}",
        "        run: |",
        '          mkdir -p "$MAXPM_OUT"',
        f'          gh run view {run} --repo "$GITHUB_REPOSITORY" --log-failed 2>&1 | tail -c 200000 > "$MAXPM_OUT/failed.log" || true',
        "      - name: Install the agent CLI",
        *([f"        run: {install_line(cli)}"] if cli != "agy" else [
            "        run: |",
            f'          {install_line(cli, "$HOME/.local/bin")}',
            '          echo "$HOME/.local/bin" >> "$GITHUB_PATH"']),
        "      - name: The agent",
        "        env:",
        f"          {SECRET[cli]}: ${{{{ secrets.{secret} }}}}",
        "          MAXPM_PROMPT: |",
        *[("            " + x).rstrip() for x in text.splitlines()],
        "        run: |",
        '          git config user.name "maxpm-agent"',
        '          git config user.email "maxpm-agent@users.noreply.github.com"',
        f"          {agent}",
        "      - name: Test the fix",
        "        # The test runs the agent's code: no token in its environment.",
        "        if: always()",
        "        run: |",
        '          mkdir -p "$MAXPM_OUT"',
        '          if [ "$(git rev-parse HEAD)" != "$FAILED" ] && [ -n "$MAXPM_TEST" ]; then',
        '            bash -c "$MAXPM_TEST" > "$MAXPM_OUT/test.log" 2>&1; echo $? > "$MAXPM_OUT/test-exit"',
        "          fi",
        "      - name: Keep the fix for the push",
        "        # Only the commits go to the push job, as a git bundle: no hook and no setting of this checkout.",
        "        if: always()",
        "        run: |",
        '          mkdir -p "$MAXPM_OUT"; rm -f "$MAXPM_OUT/fix.bundle"',
        '          if [ "$(git rev-parse HEAD)" != "$FAILED" ]; then',
        '            git bundle create "$MAXPM_OUT/fix.bundle" HEAD "^$FAILED" 2> "$MAXPM_OUT/bundle.err" || true',
        "          fi",
        "      - uses: actions/upload-artifact@v4",
        "        if: always()",
        "        with:",
        f"          name: {WORK}",
        f"          path: {OUT}",
        "          retention-days: 1",
        f"  {PUSH_JOB}:",
        "    # A new runner: nothing of the agent's job runs here. Only this job's token can write.",
        f"    needs: {JOB}",
        f"    if: ${{{{ !cancelled() && needs.{JOB}.result != 'skipped' }}}}",
        "    runs-on: ubuntu-latest",
        "    timeout-minutes: 10",
        "    permissions:",
        "      contents: write",
        "      pull-requests: write",
        "    steps:",
        "      - uses: actions/checkout@v4",
        "        with:",
        "          ref: ${{ github.event.workflow_run.head_sha }}",
        "          persist-credentials: false",
        "      - uses: actions/download-artifact@v4",
        "        continue-on-error: true",
        "        with:",
        f"          name: {WORK}",
        f"          path: {OUT}",
        "      - name: Push the fix and open a pull request",
        "        if: always()",
        "        env:",
        "          GH_TOKEN: ${{ github.token }}",
        "          BASE: ${{ github.event.workflow_run.head_branch }}",
        "          WORKFLOW: ${{ github.event.workflow_run.name }}",
        "        run: |",
        "          set +e",
        '          mkdir -p "$MAXPM_OUT"',
        '          head="$FAILED"; branch=""; pr=""; error=""',
        '          test_exit=$(cat "$MAXPM_OUT/test-exit" 2>/dev/null)',
        '          case "$test_exit" in ""|*[!0-9]*) test_exit="";; esac',
        '          if [ -f "$MAXPM_OUT/fix.bundle" ]; then',
        '            if git fetch -q "$MAXPM_OUT/fix.bundle" HEAD 2> "$MAXPM_OUT/fetch.err"; then',
        "              head=$(git rev-parse FETCH_HEAD)",
        f'              branch="maxpm/fix-{run}"',
        '              if git -c core.hooksPath=/dev/null push -q "https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git" "$head:refs/heads/$branch"; then',
        '                python3 -I -c \'import json,sys; t=[json.loads(l) for l in open(sys.argv[1]) if l.startswith("{")]; '
        'r=[x.get("result") if x.get("type")=="result" else x.get("response") for x in t if x.get("type")=="result" or "conversation_id" in x]; '
        'print((r[-1] if r else "") or "The agent of MaximizePM made this fix.")\' '
        '"$MAXPM_OUT/transcript.jsonl" > "$MAXPM_OUT/summary.md" 2>/dev/null || echo "The agent of MaximizePM made this fix." > "$MAXPM_OUT/summary.md"',
        '                pr=$(gh pr create --repo "$GITHUB_REPOSITORY" --base "$BASE" --head "$branch" '
        '--title "maxpm agent: fix $WORKFLOW for ${FAILED:0:7}" --body-file "$MAXPM_OUT/summary.md" 2>"$MAXPM_OUT/pr.err") '
        '|| error="no pull request: $(tail -1 "$MAXPM_OUT/pr.err")"',
        "              else",
        '                error="the push of $branch failed"',
        "              fi",
        "            else",
        '              error="the fix did not come from the agent job: $(tail -1 "$MAXPM_OUT/fetch.err")"',
        "            fi",
        '          elif [ -s "$MAXPM_OUT/bundle.err" ]; then',
        '            error="no bundle of the fix: $(tail -1 "$MAXPM_OUT/bundle.err")"',
        "          fi",
        '          rm -f "$MAXPM_OUT/fix.bundle"',
        '          HEAD_SHA="$head" BRANCH="$branch" PR="$pr" ERROR="$error" TEST_EXIT="$test_exit" python3 -I - <<\'EOF\'',
        "          import json, os",
        '          out = os.environ["MAXPM_OUT"]',
        "          def tail(name):",
        "              try:",
        '                  lines = [x.strip() for x in open(os.path.join(out, name), errors="replace") if x.strip()]',
        "              except OSError:",
        "                  return None",
        "              return lines[-1][:300] if lines else None",
        '          changed = os.environ["HEAD_SHA"] != os.environ["FAILED"]',
        '          test = {"command": os.environ["MAXPM_TEST"], "exit": int(os.environ["TEST_EXIT"]), "last": tail("test.log")} '
        'if os.environ.get("TEST_EXIT") else None',
        '          proof = {"rev": os.environ["FAILED"],',
        '                   "commit": os.environ["HEAD_SHA"] if changed else None, "branch": os.environ["BRANCH"] or None,',
        '                   "pr": os.environ["PR"] or None, "test": test, "error": os.environ["ERROR"] or None,',
        '                   "agent_exit": int(tail("agent-exit") or 0)}',
        '          json.dump({k: v for k, v in proof.items() if v is not None}, open(os.path.join(out, "proof.json"), "w"))',
        "          EOF",
        "      - uses: actions/upload-artifact@v4",
        "        if: always()",
        "        with:",
        f"          name: {JOB}",
        f"          path: {OUT}",
        "          retention-days: 14",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- the job in GitLab CI (#6172)

BEGIN = "# >>> maxpm-agent"  # the marked lines around the job in .gitlab-ci.yml: a second write replaces what is between
END = "# <<< maxpm-agent"
_BLOCK = re.compile(r"^# >>> maxpm-agent\b.*?^# <<< maxpm-agent[^\n]*(?:\n|\Z)", re.M | re.S)


# ---------------------------------------------------------------- the agent's own user (GitLab and Pulumi, #6179)

# In GitLab and Pulumi the agent runs in the job that holds the tokens. A process of the job's own user reads them
# from /proc/<pid>/environ of the job's shell, whatever env -i or env -u took out of its own environment. So the
# agent and --test run as another user (the job runs as root), in a clone of their own, with a clean environment
# (only the model key, HOME, PATH, LANG and CI). The fix comes back as a git bundle after every process of that
# user has stopped, and the job's own checkout, which the agent cannot read or change, pushes it.
AGENT_UID = 47100
_DROP = "os.setgroups([]); os.setgid(uid); os.setuid(uid)"  # the change of user (a test that runs as one user skips it)
# gate.py <uid> <home> [NAME=value ...] <command ...>: the command as that user, with a clean environment;
# gate.py <uid> <home> --end: stop every process of that user (on Linux kill(-1) does not reach the caller).
_GATE_PY = r'''import os, re, sys
uid, home, args = int(sys.argv[1]), sys.argv[2], sys.argv[3:]
env = {"HOME": home, "USER": "maxpm-agent", "LOGNAME": "maxpm-agent", "LANG": "C.UTF-8", "CI": "true",
       "PATH": os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin"}
while args and re.match(r"[A-Za-z_][A-Za-z0-9_]*=", args[0]):
    k, v = args.pop(0).split("=", 1)
    env[k] = v
__DROP__
if args == ["--end"]:
    if os.getuid() == uid != 0:
        os.kill(-1, 9)
    sys.exit(0)
os.execvpe(args[0], args, env)
'''


def _isolate(email):
    """Bash lines, run in the job's checkout at $FAILED: the agent's user, its clone in $WORK/repo, and the function
    'agent' that runs a command as that user. When the agent cannot run apart from the tokens, they write why in
    $MAXPM_OUT/error, and the job runs no agent."""
    return [
        f'MAXPM_UID={AGENT_UID}; WORK=""',
        'if [ -s "$MAXPM_OUT/error" ]; then :',
        'elif ! PRIV=$(mktemp -d "${TMPDIR:-/tmp}/maxpm.XXXXXX") || ! WORK=$(mktemp -d "${TMPDIR:-/tmp}/maxpm.XXXXXX"); then',
        '  WORK=""; echo "the agent did not run: no temporary folder (mktemp -d)" > "$MAXPM_OUT/error"',
        "else",
        "cat > \"$PRIV/gate.py\" <<'MAXPM_PY'",
        _GATE_PY.replace("__DROP__", _DROP).rstrip("\n"),
        "MAXPM_PY",
        'agent() { python3 -I "$PRIV/gate.py" "$MAXPM_UID" "$WORK/home" "$@" < /dev/null; }',
        "# The agent's user changes nothing of this checkout and reads nothing of its .git (a remote may hold a token).",
        "chmod -R go-w . 2>/dev/null; chmod -R go-rwx .git 2>/dev/null",
        '{ grep -q "^maxpm-agent:" /etc/passwd || echo "maxpm-agent:x:$MAXPM_UID:$MAXPM_UID::$WORK/home:/bin/sh" '
        ">> /etc/passwd; } 2>/dev/null",
        '{ grep -q "^maxpm-agent:" /etc/group || echo "maxpm-agent:x:$MAXPM_UID:" >> /etc/group; } 2>/dev/null',
        'mkdir -p "$WORK/home"',
        'git clone -q --no-hardlinks --no-checkout . "$WORK/repo" 2> "$PRIV/clone.err" && '
        'git -C "$WORK/repo" checkout -q --detach "$FAILED" 2>> "$PRIV/clone.err"',
        'git -C "$WORK/repo" config user.name "maxpm-agent"',
        f'git -C "$WORK/repo" config user.email "{email}"',
        'chown -R "$MAXPM_UID:$MAXPM_UID" "$WORK" 2>/dev/null',
        'if ! agent true 2> "$PRIV/gate.err"; then',
        '  echo "the agent did not run: the job cannot start it as another user, apart from the tokens (the job '
        'must run as root): $(tail -1 "$PRIV/gate.err")" > "$MAXPM_OUT/error"',
        'elif agent cat "/proc/$$/environ" > /dev/null 2>&1; then',
        '  echo "the agent did not run: its user can read the environment of the job, with the tokens" '
        '> "$MAXPM_OUT/error"',
        'elif [ ! -d "$WORK/repo/.git" ]; then',
        '  echo "the agent did not run: no clone of the checkout: $(tail -1 "$PRIV/clone.err")" > "$MAXPM_OUT/error"',
        "fi",
        "fi",
    ]


def _after_agent():
    """Bash lines after the agent: --test as its user, its commits as a git bundle, then every process of its user
    stopped."""
    return [
        'agent git -C "$WORK/repo" rev-parse HEAD > "$PRIV/agent-head" 2> /dev/null',
        'if [ "$(cat "$PRIV/agent-head")" != "$FAILED" ]; then',
        "  # The test runs the agent's code: as the agent's user too.",
        '  if [ -n "$MAXPM_TEST" ]; then',
        '    (cd "$WORK/repo" && agent bash -c "$MAXPM_TEST") > "$MAXPM_OUT/test.log" 2>&1; TEST_EXIT=$?',
        "  fi",
        '  agent git -C "$WORK/repo" bundle create "$WORK/fix.bundle" HEAD "^$FAILED" > /dev/null '
        '2> "$MAXPM_OUT/bundle.err"',
        "fi",
        "for i in 1 2 3; do agent --end; done",
    ]


def _take_fix():
    """Bash lines: the bundle's commits into the job's own checkout, as data; $HEAD_SHA is the fix, else $FAILED.
    Nothing of the agent's clone runs as the job's user."""
    return [
        'HEAD_SHA="$FAILED"',
        'if [ -n "$WORK" ] && [ -f "$WORK/fix.bundle" ] && [ ! -L "$WORK/fix.bundle" ] && '
        'cp "$WORK/fix.bundle" "$PRIV/fix.bundle"; then',
        '  if git -c transfer.fsckObjects=true fetch -q "$PRIV/fix.bundle" HEAD 2> "$MAXPM_OUT/fetch.err"; then',
        "    HEAD_SHA=$(git rev-parse FETCH_HEAD)",
        "  else",
        '    echo "the fix did not come from the agent\'s clone: $(tail -1 "$MAXPM_OUT/fetch.err")" > "$MAXPM_OUT/error"',
        "  fi",
        'elif [ -s "$MAXPM_OUT/bundle.err" ]; then',
        '  echo "no bundle of the fix: $(tail -1 "$MAXPM_OUT/bundle.err")" > "$MAXPM_OUT/error"',
        "fi",
    ]


# ---------------------------------------------------------------- the job in GitLab CI, continued


def _gl(text):
    """A text that GitLab expands in a variable's value: '$' of the text stays a '$'."""
    return (text or "").replace("$", "$$")


def gitlab_prompt(does, test):
    """The agent's instructions in the GitLab job: the predefined variables in it are GitLab's to expand."""
    return ("You are a MaximizePM agent in a GitLab CI job, with no person, no terminal and no permission prompt. "
            "A job of the pipeline $CI_PIPELINE_URL failed for commit $CI_COMMIT_SHA on $CI_COMMIT_BRANCH. The failed "
            "jobs' logs are in the file $MAXPM_OUT/failed.log. This folder is a checkout of that commit.\n\n"
            f"What to do: {_gl(does)}\n\n"
            "Find the cause in the log and the code, and fix it in this folder. "
            + (f"Prove the fix: run `{_gl(test)}`. " if test else "")
            + "Commit the fix with a message that says what you changed and why (git add, git commit). Do not push: "
            "the job pushes your commit to a branch and opens a merge request after you. When you cannot fix it, "
            "commit nothing. End with a short report: the cause, what you changed, and the proof.")


def gitlab_text(pipeline, repo, ref, stages, cli="claude", secret=None, test=None, max_minutes=30, max_cost=5.0,
                allow=None, does=None, image=None):
    """The job maxpm-agent between the marked lines, for the end of .gitlab-ci.yml. stages: the GitLab stages of the
    pipeline's MaximizePM stages; a failure in another stage starts no agent (when the token reads the jobs)."""
    secret = secret or SECRET[cli]
    tools = tool_list(allow or ALLOW)
    if test and not allow:
        tools.append(f"Bash({shlex.split(test)[0]}:*)")
    when = (f'$CI_COMMIT_BRANCH == "{ref}" && $CI_PROJECT_PATH == "{repo}" && '
            '$CI_PIPELINE_SOURCE != "merge_request_event"')
    text = gitlab_prompt(does or "Find the cause of the failure and fix it.", test)
    script = gitlab_script(cli, secret, max_cost, tools)
    lines = [
        f"{BEGIN}: the MaximizePM agent job of pipeline {pipeline} (#6172), written by maxpm {__version__}:",
        f"#   maxpm runner gitlab {pipeline}",
        "# When a job of the pipeline fails on the branch the pipeline follows, an agent looks for the cause and fixes",
        "# it on a branch, with a merge request. MaximizePM reads this job's state and its artifact (the proof and the",
        "# transcript). Write it again with the command; a change by hand between the two marked lines is lost then.",
        f"# The model key: the CI/CD variable {secret} (Settings > CI/CD > Variables, masked).",
        f"# The logs, the push and the merge request: the CI/CD variable {TOKEN_VAR}, a project access token (role",
        "# Developer, scopes api and write_repository), masked.",
        "# The job runs as root: the agent and the test run as another user, which cannot read the tokens (#6179).",
        f"{JOB}:",
        "  stage: .post",
        f"  image: {image or IMAGE}",
        "  rules:",
        "    # Only a failure on the branch the pipeline follows, in this project (never a fork's copy of this file).",
        f"    - if: {_yaml_str(when)}",
        "      when: on_failure",
        "  allow_failure: true",
        "  interruptible: false",
        f"  timeout: {int(max_minutes)} minutes",
        "  variables:",
        "    MAXPM_OUT: $CI_PROJECT_DIR/maxpm-agent",
        f"    MAXPM_STAGES: {_yaml_str(' '.join(stages))}",
        f"    MAXPM_TEST: {_yaml_str(_gl(test))}",
        "    MAXPM_PROMPT: |",
        *[("      " + x).rstrip() for x in text.splitlines()],
        "  script:",
        "    - |",
        *[("      " + x).rstrip() for x in script],
        "  artifacts:",
        "    when: always",
        f"    name: {JOB}",
        "    paths: [maxpm-agent/]",
        "    expire_in: 14 days",
        f"{END}",
        "",
    ]
    return "\n".join(lines)


def gitlab_script(cli, secret, max_cost, tools):
    """The bash lines of the job maxpm-agent (its script), in the checkout of the failed commit."""
    key = f'{SECRET[cli]}="${secret}"'
    if cli == "codex":
        agent = (f'(cd "$WORK/repo" && agent {key} codex exec --json --sandbox workspace-write --skip-git-repo-check '
                 '"$MAXPM_PROMPT" > "$MAXPM_OUT/transcript.jsonl" 2> "$MAXPM_OUT/agent.err" || echo $? > '
                 '"$MAXPM_OUT/agent-exit")')
    elif cli == "agy":
        agent = (f'(cd "$WORK/repo" && agent {key} {agy_line(tools)} > "$MAXPM_OUT/transcript.jsonl" '
                 '2> "$MAXPM_OUT/agent.err" || echo $? > "$MAXPM_OUT/agent-exit")')
    else:
        agent = (f'(cd "$WORK/repo" && agent {key} claude -p "$MAXPM_PROMPT" --output-format stream-json --verbose '
                 f'--permission-mode dontAsk --max-budget-usd {float(max_cost):g} --add-dir "$MAXPM_OUT" '
                 '--allowedTools ' + " ".join(shlex.quote(t) for t in tools)
                 + ' > "$MAXPM_OUT/transcript.jsonl" 2> "$MAXPM_OUT/agent.err" || echo $? > "$MAXPM_OUT/agent-exit")')
    return [
        "set +e",
        "umask 022",
        'mkdir -p "$MAXPM_OUT"',
        'git remote set-url origin "$CI_PROJECT_URL.git"  # the clone\'s address holds the job token',
        'FAILED="$CI_COMMIT_SHA"; TEST_EXIT=""; WORK=""',
        "python3 -I - <<'EOF'",
        "import json, os, urllib.request",
        f'out, tok = os.environ["MAXPM_OUT"], os.environ.get("{TOKEN_VAR}")',
        'api = os.environ["CI_API_V4_URL"] + "/projects/" + os.environ["CI_PROJECT_ID"]',
        'stages = os.environ.get("MAXPM_STAGES", "").split()',
        "def get(path):",
        '    req = urllib.request.Request(api + path, headers={"PRIVATE-TOKEN": tok})',
        "    with urllib.request.urlopen(req, timeout=30) as r:",
        "        return r.read()",
        'log = ["The pipeline: " + os.environ["CI_PIPELINE_URL"]]',
        "try:",
        "    if not tok:",
        f'        raise RuntimeError("the CI/CD variable {TOKEN_VAR} is not set: read the failed jobs of the '
        f'pipeline above")',
        '    jobs = json.loads(get("/pipelines/" + os.environ["CI_PIPELINE_ID"] + "/jobs?scope%5B%5D=failed&per_page=100"))',
        f'    failed = [j for j in jobs if not j.get("allow_failure") and j.get("name") != "{JOB}"]',
        '    mine = [j for j in failed if not stages or j.get("stage") in stages]',
        "    if failed and not mine:",
        '        open(os.path.join(out, "skip"), "w").write("no failed job in the stages " + " ".join(stages))',
        "    for j in mine:",
        '        trace = get("/jobs/%d/trace" % j["id"]).decode("utf-8", "replace")',
        '        log.append("===== job %s (stage %s): %s\\n%s" % (j["name"], j["stage"], j.get("web_url"), trace[-100000:]))',
        "except Exception as e:",
        '    log.append("No log: %s" % e)',
        'open(os.path.join(out, "failed.log"), "w").write("\\n".join(log)[-200000:])',
        "EOF",
        'if [ -f "$MAXPM_OUT/skip" ]; then',
        f'  echo "{JOB}: $(cat "$MAXPM_OUT/skip"): no agent"',
        "else",
        f'{install_line(cli)} > "$MAXPM_OUT/npm.log" 2>&1',
        'chmod -R go-rwx "$CI_PROJECT_DIR.tmp" 2>/dev/null  # the CI/CD variables of type File',
        *_isolate("maxpm-agent@users.noreply.gitlab.com"),
        'if [ ! -s "$MAXPM_OUT/error" ]; then',
        agent,
        *_after_agent(),
        "fi",
        "fi",
        *_take_fix(),
        'branch=""; mr=""; error=""',
        'if [ "$HEAD_SHA" != "$FAILED" ]; then',
        '  branch="maxpm/fix-$CI_PIPELINE_ID"',
        '  auth="gitlab-ci-token:$CI_JOB_TOKEN"',
        f'  if [ -n "${TOKEN_VAR}" ]; then auth="oauth2:${TOKEN_VAR}"; fi',
        "  # No hook of the checkout runs: the agent could have written one, and the push holds the token.",
        '  git -c core.hooksPath=/dev/null push -o merge_request.create -o "merge_request.target=$CI_COMMIT_BRANCH" '
        '-o "merge_request.title=maxpm agent: fix pipeline $CI_PIPELINE_ID for $CI_COMMIT_SHORT_SHA" '
        '-o merge_request.remove_source_branch '
        '"$CI_SERVER_PROTOCOL://$auth@$CI_SERVER_HOST:$CI_SERVER_PORT/$CI_PROJECT_PATH.git" "$HEAD_SHA:refs/heads/$branch" '
        '> "$MAXPM_OUT/push.log" 2>&1',
        "  pushed=$?",
        '  sed -i "s|$auth|[masked]|g" "$MAXPM_OUT/push.log"',
        '  if [ "$pushed" -eq 0 ]; then',
        "    mr=$(grep -o 'https\\?://[^ ]*/-/merge_requests/[0-9]*' \"$MAXPM_OUT/push.log\" | tail -1)",
        '    if [ -z "$mr" ]; then error="pushed $branch, but GitLab opened no merge request"; fi',
        "  else",
        '    error="the push of $branch failed: $(tail -1 "$MAXPM_OUT/push.log")"',
        "  fi",
        "fi",
        'HEAD_SHA="$HEAD_SHA" BRANCH="$branch" PR="$mr" ERROR="$error" TEST_EXIT="$TEST_EXIT" python3 -I - <<\'EOF\'',
        "import json, os",
        'out = os.environ["MAXPM_OUT"]',
        "def tail(name):",
        "    try:",
        '        lines = [x.strip() for x in open(os.path.join(out, name), errors="replace") if x.strip()]',
        "    except OSError:",
        "        return None",
        "    return lines[-1][:300] if lines else None",
        'changed = os.environ["HEAD_SHA"] != os.environ["CI_COMMIT_SHA"]',
        'test = {"command": os.environ["MAXPM_TEST"], "exit": int(os.environ["TEST_EXIT"]), "last": tail("test.log")} '
        'if os.environ.get("TEST_EXIT") else None',
        'proof = {"rev": os.environ["CI_COMMIT_SHA"], "commit": os.environ["HEAD_SHA"] if changed else None,',
        '         "branch": os.environ["BRANCH"] or None, "pr": os.environ["PR"] or None, "test": test,',
        '         "error": os.environ["ERROR"] or tail("error")',
        '                  or (tail("skip") if os.path.exists(os.path.join(out, "skip")) else None),',
        '         "agent_exit": int(tail("agent-exit") or 0)}',
        'json.dump({k: v for k, v in proof.items() if v is not None}, open(os.path.join(out, "proof.json"), "w"))',
        "EOF",
    ]


def put_block(old, block):
    """The text of .gitlab-ci.yml with the job's marked block in it: in place of the block before, or at the end."""
    rest = _BLOCK.sub("", old or "", count=1)
    if re.search(rf"^{JOB}:", rest, re.M):
        raise RiverError(f"the file has a job {JOB} of its own, outside the lines that maxpm runner gitlab marks: "
                         f"rename it, then run this again")
    if _BLOCK.search(old or ""):
        return _BLOCK.sub(lambda m: block, old, count=1)
    return (old.rstrip("\n") + "\n\n" if (old or "").strip() else "") + block


def _target(p, forge, file, show, cwd):
    """(the file to write or None, the checkout's top folder or None): --file, else the place in this checkout."""
    top = _git(cwd or ".", "rev-parse", "--show-toplevel")
    if show:
        return None, top
    if file:
        path = Path(file).expanduser()
        return (path if path.is_absolute() else Path(cwd or ".").resolve() / path), top
    if not top:
        raise RiverError(f"this folder is not a git checkout: run it in a checkout of {p['repo']}, or name the "
                         f"file: --file <path>, or see it: --print")
    origin = _git(top, "remote", "get-url", "origin")
    host = "github.com"
    if forge == "gitlab":
        from urllib.parse import urlparse
        host = urlparse(p["forge_url"] or "https://gitlab.com").hostname or "gitlab.com"
    if not same_repo(origin, p["repo"], host):
        raise RiverError(f"this folder is a checkout of {origin or 'no remote origin'}, not of {p['repo']}: run "
                         f"it in a checkout of {p['repo']}, or name the file: --file <path>")
    path = Path(top) / FILE[forge]
    if forge == "gitlab" and not path.exists():
        raise RiverError(f"{top} has no .gitlab-ci.yml: the job joins the pipeline that GitLab runs from it; name "
                         f"another file with --file <path>")
    return path, top


def _moves(conn, p, stages):
    """The pipeline's stage-failed rules, and those of this computer that the forge's job takes over: enabled, for
    a stage (or a tier) that runs in the forge."""
    from . import triggers
    rules = conn.execute("SELECT * FROM pipeline_triggers WHERE pipeline_id=? AND event='stage-failed' ORDER BY id",
                         (p["id"],)).fetchall()
    stage_names = {s["name"] for s in stages}
    tiers = {s["tier"] for s in stages if s["tier"]}
    return rules, [r for r in rules if r["enabled"] and r["runner"] == triggers.THIS
                   and (not r["stage"] or r["stage"] in stage_names) and (not r["tier"] or r["tier"] in tiers)]


def _move(conn, p, rules, moves, actor):
    """Move the rules to the forge, or add one when the pipeline has none there. Returns a line for each."""
    from . import triggers
    out = []
    for r in moves:
        triggers.rule_edit(conn, p["name"], f"t{r['id']}", runner=p["forge"], actor=actor)
        out.append(f"t{r['id']} ({triggers.when_text(r).lower()}) now runs its agent in {label(p['forge'])}")
    if not any(r["runner"] == p["forge"] for r in rules) and not moves:
        r = triggers.rule_add(conn, p["name"], "stage-failed", runner=p["forge"], actor=actor)
        out.append(f"{r['name']} added: {r['when'].lower()}, its agent runs in {label(p['forge'])}")
    return out


def setup(conn, pipeline, cli="claude", secret=None, test=None, max_minutes=30, max_cost=5.0, allow=None, file=None,
          show=False, actor=None, cwd=None, forge="github", image=None):
    """maxpm runner github|gitlab <pipeline>: write the agent job into a checkout of the pipeline's repository, and
    set the pipeline's stage-failed rules on this computer to run their agent there. --print only shows the file."""
    runners._may(conn, actor, "writes the agent job of a pipeline and moves its trigger rules there, because the "
                              "job runs an agent with the repository's rights")
    if forge not in FILE:  # Pulumi has no file of the job: maxpm runner pulumi (setup_pulumi)
        raise RiverError(f"the agent job's file is written for one of: {', '.join(FILE)}")
    p = store._pipeline(conn, pipeline)
    if p["forge"] != forge:
        hint = {"github": "owner/name", "gitlab": "group/project"}[forge]
        raise RiverError(f"pipeline {p['name']} does not read {SOURCE[forge]} (its source: {p['forge'] or 'none'})"
                         + (f": its job is maxpm runner {p['forge']} {p['name']}" if p["forge"] in FORGES else
                            f"; the job runs in {', '.join(FORGES.values())} only, for now"
                            if p["forge"] in ("gitea", "forgejo") else
                            f": first maxpm pipeline forge {p['name']} {forge} --repo {hint} --secret <name>"))
    if image is not None and (forge != "gitlab" or not re.match(r"^[\w.:/@-]{1,200}$", image)):
        raise RiverError("--image is a GitLab job's image, such as node:20 or registry.example.com/ci/agent:1"
                         if forge == "gitlab" else "--image is for maxpm runner gitlab: GitHub runs ubuntu-latest")
    if cli not in SECRET:
        raise RiverError(f"the agent CLI of the job is one of: {', '.join(SECRET)}")
    secret = secret or SECRET[cli]
    kind = "repository secret" if forge == "github" else "CI/CD variable"
    if not _SECRET_NAME.match(secret) or secret == TOKEN_VAR:
        raise RiverError(f"a {kind}'s name is upper-case letters, digits and '_', such as ANTHROPIC_API_KEY"
                         + (f" (not {TOKEN_VAR}: that one holds the project access token)" if forge == "gitlab" else ""))
    if not 1 <= int(max_minutes) <= RUN_MAX // 60:
        raise RiverError(f"--max-minutes is a whole number from 1 to {RUN_MAX // 60} (the limit of the job)")
    if float(max_cost) <= 0:
        raise RiverError("--max-cost is a number of dollars above 0")
    if test is not None and not test.strip():
        test = None
    if test:
        try:
            shlex.split(test)
        except ValueError as e:
            raise RiverError(f"--test is a shell command: {e}") from None
    stages = conn.execute("SELECT name, workflow, tier FROM pipeline_stages WHERE pipeline_id=? AND removed_at IS "
                          "NULL AND workflow IS NOT NULL ORDER BY pos, id", (p["id"],)).fetchall()
    if not stages:
        raise RiverError(f"no stage of pipeline {p['name']} runs a " + (
            f"GitHub workflow: maxpm pipeline stage {p['name']} <stage> --workflow tests.yml" if forge == "github" else
            f"GitLab stage: maxpm pipeline stage {p['name']} <stage> --workflow <GitLab stage>"))
    ref = p["ref"] or "main"
    if forge == "gitlab" and ('"' in ref or '"' in (p["repo"] or "")):
        raise RiverError("the branch or the project of the pipeline has a '\"': GitLab's rule of the job cannot name it")
    path, top = _target(p, forge, file, show, cwd)
    if forge == "github":
        folder = (path.parent.parent.parent if path and path.parent.name == "workflows" else None) or top
        names = list(dict.fromkeys(workflow_name(folder, s["workflow"]) for s in stages))
    else:
        names = list(dict.fromkeys(s["workflow"] for s in stages))  # the GitLab stages
    rules, moves = _moves(conn, p, stages)
    does = next((r["does"] for r in moves + [x for x in rules if x["runner"] == p["forge"]]), None)
    if forge == "github":
        text = workflow_text(p["name"], p["repo"], ref, names, cli, secret, test, max_minutes, max_cost, allow, does)
    else:
        text = gitlab_text(p["name"], p["repo"], ref, names, cli, secret, test, max_minutes, max_cost, allow, does,
                           image)
    out = {"pipeline": p["name"], "repo": p["repo"], "forge": forge, "workflows": names,
           "file": str(path) if path else None, "secret": secret, "cli": cli, "rules": [],
           "text": text if show else None}
    if show:
        return out
    if forge == "gitlab":
        try:
            old = path.read_text(encoding="utf-8") if path.exists() else ""
        except (OSError, UnicodeDecodeError) as e:
            raise RiverError(f"cannot read {path}: {e}") from None
        text = put_block(old, text)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    out["rules"] = _move(conn, p, rules, moves, actor)
    from . import define
    with core.tx(conn):
        define._change(conn, p["id"], actor, f"agent job written ({path.name}, {cli}, secret {secret}"
                       + (f", test {test}" if test else "") + f") for the "
                       + ("workflows" if forge == "github" else "GitLab stages") + f" {', '.join(names)}",
                       {"file": str(path), "cli": cli, "secret": secret, "test": test, "workflows": names})
    return out


# ---------------------------------------------------------------- maxpm runner pulumi <pipeline> (#6173)

# What the Pulumi job does after the agent, in Python (the job's image has python3): its proof, the end of the
# transcript and the patch as '::maxpm-' lines of the deployment's log, which the pulumi plugin reads at the end.
_PROOF_PY = r'''import base64, gzip, json, os
out = os.environ["MAXPM_OUT"]
def tail(name):
    try:
        lines = [x.strip() for x in open(os.path.join(out, name), errors="replace") if x.strip()]
    except OSError:
        return None
    return lines[-1][:300] if lines else None
def read(name):
    try:
        with open(os.path.join(out, name), "rb") as f:
            return f.read()
    except OSError:
        return b""
def emit(tag, data):
    text = base64.b64encode(gzip.compress(data)).decode()
    for i in range(0, len(text), 1000):
        print("::maxpm-%s %s" % (tag, text[i:i + 1000]))
changed = os.environ["HEAD_SHA"] != os.environ["FAILED"]
test = ({"command": os.environ["MAXPM_TEST"], "exit": int(os.environ["TEST_EXIT"]), "last": tail("test.log")}
        if os.environ.get("TEST_EXIT") else None)
error, patch = tail("error"), read("fix.patch")
if len(patch) > __PATCH_MAX__:
    error, patch = "the patch has %d bytes, more than __PATCH_MAX__: read the transcript" % len(patch), b""
data = b"\n".join(read("transcript.jsonl").splitlines()[-400:])[-(1 << 20):]
while len(gzip.compress(data)) > 300000:
    data = data[len(data) // 2:]
if data:
    emit("transcript", data + b"\n")
if patch:
    emit("patch", patch)
proof = {"rev": os.environ["FAILED"], "commit": os.environ["HEAD_SHA"] if changed else None,
         "branch": os.environ.get("BRANCH") or None, "pr": os.environ.get("PR") or None, "test": test, "error": error,
         "agent_exit": int(tail("agent-exit") or 0)}
print("::maxpm-proof " + json.dumps({k: v for k, v in proof.items() if v is not None}))
'''

# The pull request on GitHub, with the token that only this step holds.
_PR_PY = r'''import json, os, sys, urllib.request
repo = sys.argv[1].split("/", 1)[1]
try:
    t = [json.loads(x) for x in open(os.environ["MAXPM_OUT"] + "/transcript.jsonl") if x.startswith("{")]
    # Claude Code's result line, or agy's one JSON object (#6249)
    body = ([x.get("result") if x.get("type") == "result" else x.get("response")
             for x in t if x.get("type") == "result" or "conversation_id" in x] or [""])[-1]
except (OSError, ValueError):
    body = ""
req = urllib.request.Request("https://api.github.com/repos/%s/pulls" % repo, method="POST", data=json.dumps({
    "title": "maxpm agent: fix " + os.environ["MAXPM_FAILED"], "head": os.environ["MAXPM_BRANCH"],
    "base": os.environ["MAXPM_BASE"], "body": body or "The agent of MaximizePM made this fix."}).encode(),
    headers={"Authorization": "Bearer " + os.environ["MAXPM_GIT_TOKEN"], "Accept": "application/vnd.github+json"})
print(json.load(urllib.request.urlopen(req, timeout=30))["html_url"])
'''


def pulumi_script(c):
    """The bash script of the Pulumi job (its first pre-run command writes it to SCRIPT). It always ends with 0: the
    proof in the log, not the deployment's status, says what the agent did."""
    agent = _agent_cmd(c["cli"], _tools(c.get("allow"), c.get("test")), c["max_cost"])
    key = SECRET[c["cli"]]
    return "\n".join([
        f"# The MaximizePM agent job in Pulumi Deployments (#6173), written by maxpm {__version__}:",
        "#   maxpm runner pulumi <pipeline>",
        "set +e",
        "umask 022",
        f"export MAXPM_OUT={OUT}",
        'mkdir -p "$MAXPM_OUT"',
        'cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"',
        'FAILED=$(git rev-parse HEAD); BRANCH=""; PR=""; TEST_EXIT=""',
        'curl -sS -H "Authorization: token $PULUMI_ACCESS_TOKEN" "$MAXPM_RUN_URL" > "$MAXPM_OUT/failed-run.json" '
        '2> "$MAXPM_OUT/curl.err"',
        f'{install_line(c["cli"])} > "$MAXPM_OUT/install.log" 2>&1 || echo "the install of {c["cli"]} '
        f'failed: $(tail -1 "$MAXPM_OUT/install.log")" > "$MAXPM_OUT/error"',
        "# The agent and the test run as another user, with the model key and neither the git token nor the Pulumi",
        "# token: they cannot push, change nothing in Pulumi, and cannot read the tokens of this shell (#6179).",
        *_isolate("maxpm-agent@users.noreply.github.com"),
        'if [ ! -s "$MAXPM_OUT/error" ]; then',
        f'(cd "$WORK/repo" && agent {key}="${key}" timeout {int(c["max_minutes"])}m {agent})',
        *_after_agent(),
        "fi",
        *_take_fix(),
        "cat > \"$MAXPM_OUT/pr.py\" <<'MAXPM_PY'",
        _PR_PY.rstrip("\n"),
        "MAXPM_PY",
        'if [ "$HEAD_SHA" != "$FAILED" ]; then',
        "  # Python runs isolated (-I) where a token is: no module of a checkout loads.",
        '  if [ -n "$MAXPM_GIT_TOKEN" ]; then',
        "    REPO=$(git remote get-url origin | sed -E 's#^[a-z+]+://##; s#^[^@/]*@##; s#^([^/:]+):#\\1/#; s#\\.git$##')",
        "    # No hook of the checkout runs: the agent could have written one, and the push holds the token.",
        '    if git -c core.hooksPath=/dev/null push -q "https://x-access-token:${MAXPM_GIT_TOKEN}@${REPO}.git" '
        '"$HEAD_SHA:refs/heads/$MAXPM_BRANCH" 2> "$MAXPM_OUT/push.err"; then',
        '      BRANCH="$MAXPM_BRANCH"',
        '      if [ "${REPO#github.com/}" != "$REPO" ]; then',
        '        PR=$(python3 -I "$MAXPM_OUT/pr.py" "$REPO" 2> "$MAXPM_OUT/pr.err") || echo "no pull request: '
        '$(tail -1 "$MAXPM_OUT/pr.err")" > "$MAXPM_OUT/error"',
        "      fi",
        "    else",
        '      echo "the push of $MAXPM_BRANCH failed: $(tail -1 "$MAXPM_OUT/push.err")" > "$MAXPM_OUT/error"',
        "    fi",
        "  fi",
        '  [ -n "$BRANCH" ] || git format-patch --stdout "$FAILED..$HEAD_SHA" > "$MAXPM_OUT/fix.patch"',
        "fi",
        "export FAILED HEAD_SHA BRANCH PR TEST_EXIT",
        "python3 -I - <<'MAXPM_PY'",
        _PROOF_PY.replace("__PATCH_MAX__", str(PATCH_MAX)).rstrip("\n"),
        "MAXPM_PY",
        "exit 0",
        ""])


def pulumi_prompt(does, test, n, stage, stack, branch):
    return (f"You are a MaximizePM agent in a Pulumi Deployments job, with no person, no terminal and no permission "
            f"prompt. Stage {stage} of Pulumi Delivery run {n} of the pipeline stack {stack} failed. What Pulumi shows "
            f"of that run (its stages, their status and gates) is in the file {OUT}/failed-run.json; Pulumi gives no "
            f"log of a Delivery job by its API, so read the stage's jobs and stacks in the code. This folder is a "
            f"checkout of the branch {branch}.\n\n" + _task(does, test, "that file and the code"))


def pulumi_request(p, c, key, does):
    """The body of POST /api/stacks/<agent stack>/deployments for the run whose failed stage has key
    pd-<run>-<stage>: a preview with the stack's own deployment settings (its source), whose pre-run commands write
    the script and run it inside the ESC environment. Plain strings as environment variables: Pulumi reads a string
    as a value that is not secret."""
    m = re.match(r"pd-(\d+)-(.+?)(?:~\d+)?$", key or "")
    n, stage = (m[1], m[2]) if m else ("?", key or "?")
    org, project, stack = ((p["repo"] or "").split("/") + ["", "", ""])[:3]
    q = lambda s: re.sub(r"[^A-Za-z0-9_.~-]", lambda x: "%%%02X" % ord(x[0]), s)  # noqa: E731
    url = (f"{(p['forge_url'] or 'https://api.pulumi.com').rstrip('/')}/api/preview/stacks/{q(org)}/{q(project)}/"
           f"{q(stack)}/delivery/runs/{n}")
    branch = "maxpm/fix-pd-" + n + "-" + (re.sub(r"[^A-Za-z0-9._-]+", "-", stage).strip("-.") or "stage")
    env = {"MAXPM_PROMPT": pulumi_prompt(does, c.get("test"), n, stage, p["repo"], c["branch"]),
           "MAXPM_TEST": c.get("test") or "", "MAXPM_BASE": c["branch"], "MAXPM_BRANCH": branch,
           "MAXPM_RUN_URL": url, "MAXPM_FAILED": f"stage {stage} of Delivery run {n}"}
    script = base64.b64encode(pulumi_script(c).encode()).decode()
    return {"operation": "preview", "inheritSettings": True,
            "operationContext": {"preRunCommands": [f"echo {script} | base64 -d > {SCRIPT}",
                                                    f"pulumi env run {shlex.quote(c['env'])} -- bash {SCRIPT}"],
                                 "environmentVariables": env}}


def setup_pulumi(conn, pipeline, env=None, stack=None, branch=None, cli="claude", test=None, max_minutes=30,
                 max_cost=5.0, allow=None, show=False, actor=None):
    """maxpm runner pulumi <pipeline> --env <ESC environment>: keep the agent job's settings on the pipeline and move
    its stage-failed rules to 'pulumi'. --print shows the settings and the script, and changes nothing."""
    runners._may(conn, actor, "sets the agent job of a pipeline and moves its trigger rules there, because the job "
                              "runs an agent with the repository's rights")
    p = store._pipeline(conn, pipeline)
    if p["forge"] != "pulumi":
        raise RiverError(f"pipeline {p['name']} does not read Pulumi Delivery (its source: {p['forge'] or 'none'}): "
                         + (f"its job is maxpm runner {p['forge']} {p['name']}" if p["forge"] in FILE else
                            f"first maxpm pipeline forge {p['name']} pulumi --repo org/project/stack --secret <name>"))
    old = conf(p) or {}
    env = env or old.get("env")
    if not show and not (env and _ESC_ENV.match(env)):
        raise RiverError("--env names the Pulumi ESC environment that holds the model key, project/env or "
                         "org/project/env; a person sets the key there (ANTHROPIC_API_KEY, or CODEX_API_KEY with "
                         "--cli codex, GEMINI_API_KEY with --cli agy), and MAXPM_GIT_TOKEN when the fix should come "
                         "back as a branch")
    stack = stack or old.get("stack") or p["repo"]
    if not _STACK.match(stack or ""):
        raise RiverError("--stack is the stack whose deployments run the job, org/project/stack (default: the "
                         "pipeline stack)")
    branch = branch or old.get("branch") or "main"
    if not _BRANCH.match(branch) or ".." in branch:
        raise RiverError("--branch is the branch that the job checks out and a fix goes into, such as main")
    if cli not in SECRET:
        raise RiverError(f"the agent CLI of the job is one of: {', '.join(SECRET)}")
    if not 1 <= int(max_minutes) <= RUN_MAX // 60:
        raise RiverError(f"--max-minutes is a whole number from 1 to {RUN_MAX // 60}")
    if float(max_cost) <= 0:
        raise RiverError("--max-cost is a number of dollars above 0")
    test = (test or "").strip() or None
    if test:
        try:
            shlex.split(test)
        except ValueError as e:
            raise RiverError(f"--test is a shell command: {e}") from None
    stages = conn.execute("SELECT name, workflow, tier FROM pipeline_stages WHERE pipeline_id=? AND removed_at IS "
                          "NULL AND workflow IS NOT NULL ORDER BY pos, id", (p["id"],)).fetchall()
    if not stages:
        raise RiverError(f"no stage of pipeline {p['name']} names a Delivery stage: maxpm pipeline stage {p['name']} "
                         f"<stage> --workflow <Delivery stage>")
    rules, moves = _moves(conn, p, stages)
    c = {"stack": stack, "env": env, "branch": branch, "cli": cli, "test": test, "max_minutes": int(max_minutes),
         "max_cost": float(max_cost), "allow": allow}
    out = {"pipeline": p["name"], "repo": p["repo"], "stages": [s["workflow"] for s in stages], "rules": [],
           **c, "script": pulumi_script(c) if show else None}
    if show:
        return out
    from . import define
    with core.tx(conn):
        conn.execute("UPDATE pipelines SET agent_job=? WHERE id=?", (json.dumps(c, sort_keys=True), p["id"]))
    out["rules"] = _move(conn, p, rules, moves, actor)
    with core.tx(conn):
        define._change(conn, p["id"], actor, f"agent job in Pulumi Deployments set (stack {stack}, ESC environment "
                       f"{env}, branch {branch}, {cli}" + (f", test {test}" if test else "") + ")", c)
    return out
