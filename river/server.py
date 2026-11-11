"""Local web page for MaximizePM. Binds to 127.0.0.1 only."""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import core, dictation, relay
from .core import RiverError

STATIC = Path(__file__).resolve().parent / "static"
PKG = Path(__file__).resolve().parent
DEV = {"on": False}
# The desktop app (desktop/) starts the server with MAXPM_DESKTOP=1: the app updates itself, so the
# page's git Update button does not apply there.
DESKTOP = os.environ.get("MAXPM_DESKTOP") == "1"
BOOT = str(time.time())  # changes when the server restarts; the page waits for a new one after an update


# Types the page's own files use; anything else under static is sent as bytes.
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".map": "application/json", ".json": "application/json", ".svg": "image/svg+xml",
                ".png": "image/png", ".ico": "image/x-icon", ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8",
                ".md": "text/plain; charset=utf-8"}


def static_file(path, root=None):
    """The (bytes, content type) of a file under river/static for a URL path, or None.

    Refuses anything that resolves outside the folder: '..', symlinks out, encoded slashes, hidden files."""
    from urllib.parse import unquote
    root = (root or STATIC).resolve()
    rel = unquote(path.lstrip("/")) if "%2f" not in path.lower() and "%5c" not in path.lower() else None
    if not rel or "\\" in rel or "\0" in rel or any(part.startswith(".") for part in rel.split("/")):
        return None
    f = (root / rel).resolve()
    if root not in f.parents or not f.is_file():
        return None
    name = f.name.lower()
    ctype = "text/plain; charset=utf-8" if name.startswith("license") else STATIC_TYPES.get(f.suffix.lower(), "application/octet-stream")
    return f.read_bytes(), ctype


# The page's addresses (#4653): a path for each view, for example /sessions/<session>/artifacts/<artifact id>,
# with ?as=<person> and ?item=<id>. Each one gets the page, with a <base> that points back to the page's root, so
# its relative addresses (app.js, api/state) work at any depth, here and on the relay's tunnel.
PAGE_VIEWS = ("sessions", "you", "plan", "artifacts", "settings", "terminal")
# The tabs before Sessions and Plan (#5102, design #4657): nothing forwards; each answers "not found" with a link to /.
OLD_VIEWS = ("board", "projects", "targets", "capacity", "graph", "done")
OLD_VIEW_PAGE = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width, initial-scale=1'>"
                 "<title>Not found: MaximizePM</title><body style='font:16px system-ui,sans-serif;margin:40px'>"
                 "<p>This page is not found. The page has two areas now: Sessions and Plan.</p>"
                 "<p><a href='/'>Open MaximizePM</a></p></body>")


def page_html(path):
    """The page (bytes) for a path of a view, with its <base>; None for any other path."""
    if path in ("/", "/index.html"):
        depth = 0
    else:
        parts = path.split("/")[1:]
        if parts[0] not in PAGE_VIEWS or "" in parts[:-1] or any(x in (".", "..") for x in parts):
            return None
        depth = len(parts) - 1
    base = "../" * depth or "./"
    page = (STATIC / "index.html").read_bytes().replace(b"<head>", f'<head>\n<base href="{base}">'.encode(), 1)
    # Each tab's HTML is a file of its own (#4656): static/views/<view>.html, at its <!-- view: <view> --> mark.
    # A view's HTML can hold the mark of a part of it (sessions.html holds you.html, #5102): a second pass fills those.
    for _ in range(2):
        page = re.sub(rb"<!-- view: (\w+) -->", lambda m: (STATIC / "views" / (m.group(1).decode() + ".html")).read_bytes(),
                      page)
    return page


def _code_files():
    """The server's Python code: river/*.py and the routes of each view (river/routes/*.py, #4656)."""
    return sorted(PKG.glob("*.py")) + sorted(PKG.glob("routes/*.py"))


def _watched():
    """The server's code and every page file, in subfolders too (components/, views/); vendor/ never changes by
    hand."""
    return _code_files() + sorted(
        f for f in STATIC.rglob("*") if f.is_file() and "vendor" not in f.relative_to(STATIC).parts[:1]
        and not any(part.startswith(".") for part in f.relative_to(STATIC).parts))


def _build_id():
    """Changes whenever a watched file changes; the page reloads itself in dev mode."""
    return str(max((f.stat().st_mtime_ns for f in _watched()), default=0))


_PAGE_BUILD = {"at": 0.0, "id": ""}

# The shape of /api/state that the page's code reads (#4850). Raise it, with PAGE_SHAPE in static/app.js, when a
# change of the state breaks a page that runs older code: such a page then loads itself again (at most once in 10
# minutes). Any other change of the code only shows the page a bar with Reload.
STATE_SHAPE = 1


def page_build():
    """The version of the code the page runs (#4690): _build_id, read again after 2 seconds at most. An open page
    that sees it change shows a bar with Reload (#4850); it loads itself again only when STATE_SHAPE changed too."""
    if time.time() - _PAGE_BUILD["at"] > 2:
        _PAGE_BUILD.update(at=time.time(), id=_build_id())
    return _PAGE_BUILD["id"]


def _code_id():
    """Changes when the server's Python code changes (git pull, a commit, an edit). The page files are
    read from disk on every request, but the code runs as it was when the server started."""
    return str(max((f.stat().st_mtime_ns for f in _code_files()), default=0))


BOOT_CODE = _code_id()


def code_stale():
    """True when the river code on disk is newer than the code this server runs: it needs a restart."""
    return _code_id() != BOOT_CODE

# Tests (and maxpm launch tests) set this to a fake that receives what would open a terminal.
TERMINAL_RUNNER = None


def _can_open_terminal(runner, hint):
    """Refuse a start that cannot open: every agent starts in a pane of the tmux session maxpm (#5105), on any
    system and over SSH, so one terminal (maxpm view) and the page's chats show them all. Windows, where tmux
    does not run, opens a console window. A test's runner opens nothing."""
    if (runner or TERMINAL_RUNNER) is not None or core.PLATFORM == "win32" or TMUX_RUNNER is not None:
        return
    if not _tmux_cmd():
        raise RiverError(f"every agent starts in tmux, and tmux is not installed: {core.tmux_install()}; "
                         f"or start one yourself: {hint}")


def launch_agent(conn, project=None, runner=None, agent=None, actor=None, model=None, effort=None, options=None,
                 prompt=None):
    """Open a tmux pane in the project folder of the most important ready agent item and run
    the command of the chosen launch_agents entry there (default: the Claude Code profile), so one click starts one
    agent session. prompt (maxpm launch --prompt): the new session's first instruction,
    after the profile's prompt."""
    # With no project named, Start spreads sessions: first a project with ready work and no agent yet.
    # A session that waits for work in that project (maxpm wait) gets the item: no new session needed.
    # Else the new session gets a name and the item is pushed to it, so the next Start sees the project covered.
    # A custom prompt always opens a new session: a waiting one would get it only second-hand, as a message.
    t = core.launch_target(conn, project, agent, None, model, effort, spread=project is None, options=options,
                           prompt=prompt)
    waiting = None if t["custom_prompt"] else core.waiting_agent_for(conn, t["project"], t["item"]["id"])
    if waiting:
        core.push(conn, t["item"]["id"], waiting, "from the Start button: you were waiting for work", actor)
        return {**t, "pushed_to": waiting}
    _can_open_terminal(runner, "cd <project folder> && claude go")
    return _start_for_item(conn, t, runner, actor, "Start opened this session for it")


def dispatch_item(conn, item_id, runner=None, agent=None, actor=None, model=None, effort=None, options=None,
                  prompt=None):
    """Start work on one ready item: a session that waits for work in its project gets it (push);
    else river names a new session, reserves the item for it (push), and opens the chosen agent in the
    project folder with MAXPM_AGENT set to that name and MAXPM_FOCUS=item:<id>, so its first maxpm go takes it.
    With a custom prompt (maxpm launch --prompt) it always opens a new session."""
    t = core.launch_target(conn, agent=agent, item=item_id, model=model, effort=effort, options=options, actor=actor,
                           prompt=prompt)
    waiting = None if t["custom_prompt"] else core.waiting_agent_for(conn, t["project"], t["item"]["id"])
    if waiting:
        core.push(conn, t["item"]["id"], waiting, "from the page: Dispatch; you were waiting for work", actor)
        return {**t, "pushed_to": waiting}
    _can_open_terminal(runner, f"cd <project folder> && MAXPM_FOCUS=item:{t['item']['id']} claude go")
    return _start_for_item(conn, t, runner, actor, "Dispatch started this session for it")


def _start_for_item(conn, t, runner, actor, why, by="the page"):
    """Name a new session, reserve the item for it (push), and open the agent with MAXPM_AGENT and
    MAXPM_FOCUS=item:<id>: its first maxpm go claims that item, or says why not. A session that never runs
    a river command is a manager finding (not connected) and does not count as the project's agent."""
    import secrets
    name = f"{t['project']}-{secrets.token_hex(2)}"
    core.register(conn, name, note=f"{core.STARTED_NOTE} #{t['item']['id']}")
    core.push(conn, t["item"]["id"], name, f"from {by}: {why}", actor)
    _open_terminal(t, {"MAXPM_AGENT": name, "MAXPM_FOCUS": f"item:{t['item']['id']}", **t["env"]}, runner)
    if t.get("fork_of"):  # it reads the goal's base from the cache, which keeps the base warm
        core.base_used(conn, t["fork_of"]["session_id"])
        with core.tx(conn):
            core._event(conn, t["item"]["id"], actor or "maxpm", f"{name} starts as a fork of the base of goal "
                        f"{t['fork_of']['goal']} (session {t['fork_of']['session_id'][:8]})")
    if t.get("custom_prompt"):  # what the session was told, for the people who read the item's history
        flat = " ".join(t["custom_prompt"].split())
        with core.tx(conn):
            core._event(conn, t["item"]["id"], actor or "maxpm", f"{name} launched with a custom prompt: "
                        + flat[:200] + ("..." if len(flat) > 200 else ""))
    return {**t, "session_name": name}


def open_agent_on(conn, item_id, runner=None, agent=None, person=None, actor=None, model=None, effort=None,
                  options=None):
    """Open an agent session for one item from its drawer. A ready item: Dispatch. A person's item: a
    session that does it together with the person. An item that waits: a session that first takes what
    blocks it. The session learns which from MAXPM_FOCUS, which its maxpm go reads."""
    it = core.item_show(conn, item_id)
    if it["status"] not in core.OPEN_STATES:
        raise RiverError(f"#{item_id} is {it['status']}; there is nothing to open an agent on")
    if it["backlog"]:
        raise RiverError(f"#{item_id} is in the backlog; make it open again first (Activate, or maxpm activate {item_id})")
    mine = person and it["assignee"] == person  # a person claimed it (Claim next): help them do it
    if it["status"] == "in_progress" and not mine:
        raise RiverError(f"#{item_id} is in progress by {it['assignee']}; message them instead")
    if it["doer"] == "human" or mine:
        focus = f"help:{it['id']}" + (f"@{person}" if person else "")
    elif it["ready"]:
        return dispatch_item(conn, it["id"], runner, agent, actor, model, effort, options)
    else:
        focus = f"unblock:{it['id']}"
    p = core._project(conn, it["project"])
    if not p["path"]:
        raise RiverError(f"project {p['name']} has no folder, so MaximizePM cannot start a session there: "
                         f"maxpm project path {p['name']} <folder>")
    return {**_open_focused(conn, p, focus, runner, agent, model, effort, options),
            "item": {"id": it["id"], "title": it["title"]}}


def open_needs_you(conn, runner=None, agent=None, person=None, model=None, effort=None, options=None):
    """You view, Go through with an agent (#261, #5103): a session that works through everything that waits on the
    person, with them, one item at a time (core.prompt_for_all), in the folder of the most important such item's
    project. Its name is set before it starts (MAXPM_AGENT), so the page opens its chat on the Sessions tab at once.
    It is the person's assistant (#5110): its own session type, named for its job (you-<id>), long-lived, with no
    queue work. One per person: while it runs, this opens it again ("existing") and starts nothing."""
    import secrets
    person = core._person(conn, person)
    have = core.assistant_of(conn, person)
    if have:
        return {"session_name": have, "agent": have, "existing": True}
    ann = core.annotate(conn)
    items = sorted((a for a in ann.values() if a["ready"] and a["doer"] == "human" and not a["project_archived"]),
                   key=lambda a: a["sort_key"])
    projects = [core._project(conn, a["project"]) for a in items] + [
        core._project(conn, p["name"]) for p in core.project_list(conn)]
    p = next((x for x in projects if x["path"]), None)
    if p is None:
        raise RiverError("no project has a folder, so MaximizePM cannot start a session: maxpm project path <name> <folder>")
    name = f"you-{secrets.token_hex(2)}"
    while conn.execute("SELECT 1 FROM agents WHERE name=?", (name,)).fetchone():
        name = f"you-{secrets.token_hex(2)}"
    core.assistant_make(conn, name, person)
    try:
        t = _open_focused(conn, p, f"needs:@{person}", runner, agent, model, effort, options, agent_name=name)
    except Exception:
        core.unregister(conn, name)  # no session started: no assistant stands in the way of the next click
        raise
    return {**t, "session_name": name}


def deploy_now(conn, target, review=False, runner=None, agent=None, actor=None, model=None, effort=None,
               options=None):
    """Targets tab, Deploy now (or Review and deploy): when the deploy item (or its review) is ready and
    the target has no owner to alert, open an agent in a folder of the target's projects that takes it."""
    r = core.deploy_now(conn, target, review, actor)
    if not r["ready"] or r.get("alerted"):
        return r
    p = _target_folder(conn, target)
    return {**r, **_open_focused(conn, p, f"{'review' if review else 'deploy'}:{target}", runner, agent,
                                 model, effort, options)}


def _target_folder(conn, target):
    """A folder to start a session for a target: the first of its projects that has one."""
    p = next((core._project(conn, x["name"]) for x in core.target_show(conn, target)["projects"]
              if core._project(conn, x["name"])["path"]), None)
    if p is None:
        raise RiverError(f"no project of target {target} has a folder, so MaximizePM cannot start a session there: "
                         f"maxpm project path <name> <folder>")
    return p


def _same_queue(db):
    """A river command that asks the server to act (start a session for a command in a
    sandbox) names its queue file. Refuse another one, so a test queue never opens sessions from the real one."""
    if db is not None and str(Path(db).expanduser().resolve()) != str(core.db_path().expanduser().resolve()):
        raise RiverError(f"this maxpm serve uses another queue ({core.db_path()})")


def _agent_for(conn, model):
    """The launch_agents entry that runs this model: the first of its family, else the first of no known family."""
    opts = core.launch_options(conn)
    fam = core._family(core.parse_ladder(core.setting(conn, "model_ladder")), model)[0] if model else None
    pick = (next((o for o in opts if fam and o["family"] == fam), None)
            or next((o for o in opts if o["family"] is None), None) or opts[0])
    return pick["label"], pick


# The role of a session that a focus starts (agent_for.<role>, #5168). A person's assistant (needs:) has no role yet.
FOCUS_ROLES = {"deploy": "deployer", "review": "reviewer", "help": "worker", "unblock": "worker", "context": "context"}


def _open_focused(conn, p, focus, runner, agent, model=None, effort=None, options=None, agent_name=None):
    """Open the chosen agent in a project folder with MAXPM_FOCUS set; its maxpm go reads it. agent_name: the agent name
    it registers as (MAXPM_AGENT), when the caller registered one. Else river names the session (<project>-<hex>, the
    name its maxpm go would choose) and registers it before it starts (#5201): the pane then carries @maxpm_agent at
    once, so the page never shows 'This session runs outside tmux' for a pane that maxpm opened itself."""
    import secrets
    _can_open_terminal(runner, f"cd {p['path']}, set MAXPM_FOCUS={focus}, then claude go")
    kind = focus.split(":")[0]
    if agent is None and kind in FOCUS_ROLES:  # agent_for.<role>, else default_agent (#5168)
        agent = core.role_agent(conn, FOCUS_ROLES[kind], p["id"])
    title = core.focus_title(conn, focus)
    t = {"project": p["name"], "path": p["path"], "focus": focus, "session_title": title,
         **core._launch_agent_cmd(conn, p["id"], agent, model, effort, options, title)}
    named = agent_name
    if named is None:
        named = f"{p['name']}-{secrets.token_hex(2)}"
        while conn.execute("SELECT 1 FROM agents WHERE name=?", (named,)).fetchone():
            named = f"{p['name']}-{secrets.token_hex(2)}"
        core.register(conn, named, note=f"{core.STARTED_NOTE} {focus}")
    try:
        _open_terminal(t, {"MAXPM_FOCUS": focus, "MAXPM_AGENT": named, **t["env"]}, runner)
    except Exception:
        if agent_name is None:  # no session started: no name of ours stays on the page
            core.unregister(conn, named)
        raise
    return {**t, "session_name": named}


def manage_command(cmd):
    """A launch_agents command that starts a manager instead of a worker: 'maxpm go' (a first prompt) or the
    word go (Claude Code's `claude go`) becomes manage."""
    import re
    if "maxpm go" in cmd:
        return cmd.replace("maxpm go", "maxpm manage")
    out, n = re.subn(r"(?<=\s)go(?=\s|$)", "manage", cmd, count=1)
    if not n:
        raise RiverError(f"cannot make a manager from the command {cmd!r}: it has no 'go' or 'maxpm go' to replace")
    return out


def start_manager(conn, runner=None, agent=None, actor=None, model=None, effort=None, options=None):
    """Start manager (the page's Manager section): the chosen agent with manage in place of go, in the folder
    of the first project that has one, compacted at manager_autocompact. Refuses while a manager is active."""
    import secrets
    other = core.active_manager(conn)
    if other:
        raise RiverError(f"{other} is the active manager; open its chat instead")
    p = next((core._project(conn, x["name"]) for x in core.project_list(conn) if x.get("path")), None)
    if p is None:
        raise RiverError("no project has a folder, so MaximizePM cannot start a session: maxpm project path <name> <folder>")
    _can_open_terminal(runner, f"cd {p['path']} && claude manage")
    if agent is None:  # agent_for.manager, else default_agent (#5168)
        agent = core.role_agent(conn, "manager", p["id"])
    t = {"project": p["name"], "path": p["path"], "session_title": "maxpm manager",
         **core._launch_agent_cmd(conn, p["id"], agent, model, effort, options, "maxpm manager",
                                  autocompact=core.autocompact_tokens(core.setting(conn, "manager_autocompact")))}
    t["command"] = manage_command(t["command"])
    name = f"manager-{secrets.token_hex(2)}"
    core.register(conn, name, note="started from the page as the manager")
    core._set_role_note(conn, name, "manager", None)  # the next Start manager sees it at once
    _open_terminal(t, {"MAXPM_AGENT": name, **t["env"]}, runner)
    return {**t, "session_name": name}


EXPERT_PROMPT = ("You are {name}, an expert session of MaximizePM on: {topic}. A person talks with you about it, for hours or "
                 "days. Run maxpm --as {name} go once: it shows your news, your task with the talk so far, and your notes. "
                 "Read what it names before your first answer. Then answer the person. You take "
                 "no queue work by yourself; keep your notes current: maxpm --as {name} expert notes {name} --put <file>.")


def start_expert(conn, name, topic=None, project=None, resume=False, runner=None, agent=None, actor=None, model=None,
                 effort=None, options=None, item=None, summary=None, sources=(), canvases=(),
                 sessions=()):
    """maxpm expert start (#4955, #4959): open an agent session for an expert-tier task, in the folder of its project.
    The task: item (made expert-tier), else on resume the open expert-tier task it had before, else a new
    expert-tier task with the topic as its title. The session claims it before it opens, so every expert has a task.
    resume: claude --resume of the last Claude Code session the expert ran in, so the talk goes on with its context;
    with none, a fresh session that reads its notes. The model is the person's choice: a limit of the task that
    excludes it moves to it.
    summary, sources, canvases, sessions: the talk so far (#4969), which goes into the task's context, so the expert
    reads it there. The manager hands a talk over with a summary: it may not start an expert on a new topic without
    one, so a long talk never stays in the manager's context or is lost."""
    core.expert_handover_check(conn, canvases, sessions)
    if (actor and item is None and not resume and not (summary or "").strip()
            and conn.execute("SELECT 1 FROM agents WHERE name=? AND role='manager' AND kind='ai'", (actor,)).fetchone()):
        raise RiverError(f"the manager hands a talk to the expert with the talk so far: add --summary \"<what was said, "
                         f"what is decided, what is open>\" (or --summary-file <path>), and --canvas <name>, --source "
                         f"<file or url>, --session {actor} for what it must read")
    old = conn.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone()
    if old is not None and old["role"] != "expert":
        raise RiverError(f"{name} is a session that runs now ({old['role'] or 'no role'}); pick another name, or make that "
                         f"session an expert in its own chat: maxpm --as {name} expert adopt")
    if old is not None and old["pid"] and old["host"] == core.this_host() and core.pid_alive(old["pid"]):
        raise RiverError(f"the expert {name} runs now (process {old['pid']}); talk with it in its session, or end it "
                         f"first: maxpm expert end {name}")
    was_topic, was_project = core.expert_topic_of(conn, name)
    task = core._item(conn, item) if item is not None else core.expert_task(conn, name) if resume or old else None
    if task is not None and task["assignee"] not in (None, name):
        raise RiverError(f"#{task['id']} is held by {task['assignee']}")
    if task is not None and task["status"] not in ("open", "in_progress", "held"):
        raise RiverError(f"#{task['id']} is {task['status']}; an expert takes an open task")
    if task is None:
        topic = topic or was_topic
        if not topic:
            raise RiverError(f"{name} needs a task: --topic \"...\" adds an expert-tier task with that title, or "
                             f"--item <id> takes one")
        # The topic names the work it takes up (#4953): the new task continues it, and gets its model (#4961).
        linked = next((int(m) for m in re.findall(r"#(\d+)", topic)
                       if conn.execute("SELECT 1 FROM items WHERE id=?", (int(m),)).fetchone()), None)
        if not model and linked is None:
            raise RiverError(f"say which model the expert {name} runs: --model <name> (a person talks with it for days, "
                             f"so it is your choice, not the default {core.setting(conn, 'default_model') or 'of the agent'}); "
                             f"or name the item whose work it takes up in the topic (#<id>) or with --item <id>, and it "
                             f"gets that work's model")
        project = project or (core._project_name(conn, core._item(conn, linked)["project_id"]) if linked else None) \
            or was_project or next((x["name"] for x in core.project_list(conn) if x.get("path")), None)
        if not project:
            raise RiverError("an expert starts in the folder of a project: maxpm project path <name> <folder>")
        task = core._item(conn, core.item_add(conn, project, topic, actor=actor, tier="expert", found_during=linked)["id"])
    elif task["tier"] != "expert":
        core.item_edit(conn, task["id"], actor=actor, tier="expert")
    p = core._project(conn, core._project_name(conn, task["project_id"]))
    if not p["path"]:
        raise RiverError(f"project {p['name']} has no folder, so MaximizePM cannot start a session: "
                         f"maxpm project path {p['name']} <folder>")
    _can_open_terminal(runner, f"cd {p['path']} && claude go")
    topic = task["title"]
    if model:
        model_why = "you chose it"
    elif old is not None and old["model"] and resume:
        model, model_why = old["model"], "the model it ran before"
    else:
        model, model_why = core.session_model(conn, core._item(conn, task["id"]))
        if not model:
            raise RiverError(f"#{task['id']} names no model, so say which model the expert {name} runs: --model <name>")
    first = EXPERT_PROMPT.format(name=name, topic=topic)
    handed = core.expert_handover(conn, task["id"], actor, summary, sources, canvases, sessions)
    # Resume: the chat of the agent it ran in (agents.agent_type, Claude Code when not recorded), through that
    # adapter's resume_command (#5168); an agent that cannot resume starts fresh and reads the notes.
    platform = core.agent_type_of(conn, name) or "claude-code"
    ad = core._adapters().find(platform)
    sid = core.last_claude_session(conn, name) if resume and ad is not None and ad.supports("resume") else None
    if agent is None and not sid:  # agent_for.expert, else default_agent, for the task (#5168)
        agent = core.role_agent(conn, "expert", p["id"], task["id"])
    core.expert_make(conn, name, topic, p["name"], actor)
    if model:
        core.set_agent_model(conn, name, model)
        _fit_task_model(conn, task, name, model, actor)
    if core._item(conn, task["id"])["assignee"] != name:
        core.claim(conn, task["id"], name)
    if sid:
        r = core.resume_command(conn, name, core._platform_of(ad), sid, p["id"], {"prompt": first, "model": model,
                                                                                "effort": effort})
        t = {"command": r["command"], "env": r["env"], "agent": r["agent"], "resumed": sid}
    else:
        t = core._launch_agent_cmd(conn, p["id"], agent, model, effort, options, name, prompt=first)
        t = {**t, "env": {"MAXPM_AGENT": name, **t["env"]}}
    t.update(project=p["name"], path=p["path"], session_title=name, task=task["id"])
    _open_terminal(t, t["env"], runner)
    with core.tx(conn):
        core._event(conn, task["id"], actor or "maxpm", f"expert {name} started" + (
            f" (resumes session {sid})" if sid else (" (fresh: no session to resume)" if resume else "")))
    return {**t, "session_name": name, "topic": topic, "model": model, "model_why": model_why, "handover": handed}


def accept_expert(conn, pid, who=None, runner=None):
    """Continue in an expert (#4990): the person took the manager's proposal. Start the expert as maxpm expert start
    does, with the talk so far the proposal holds (actor: the manager, which handed the talk over), then close the
    proposal; the manager hears it. A start that fails keeps the proposal open, so the person can try again."""
    p = core._proposal(conn, pid)
    if p["state"] != "open":
        raise RiverError(f"expert proposal p{p['id']} is {p['state']} already")
    res = start_expert(conn, p["name"], p["topic"], p["project"], actor=p["manager"], model=p["model"],
                       effort=p["effort"], item=p["item_id"], summary=p["summary"], sources=p["sources"],
                       canvases=p["canvases"], sessions=p["sessions"], runner=runner)
    core.expert_proposal_end(conn, p["id"], "started", who, expert=res["session_name"])
    return {**res, "proposal": p["id"]}


def fresh_expert(conn, name, actor=None, runner=None, new=None):
    """maxpm expert fresh (#5023): the expert's context left the prompt cache, so its next message reads all of it
    again at the write price. A fresh expert on the same model and task reads only the old one's handoff (its notes
    canvas) and the task's context: the old one ends (its task goes back to the queue), and the new one, <name>-2
    (or the next free number, or new), takes the task. Refused while the old one has no task."""
    old = core._expert(conn, name)
    task = core.expert_task(conn, name)
    if task is None:
        raise RiverError(f"the expert {name} has no task to hand on; start a new one: maxpm expert start <name> --topic \"...\"")
    notes = core.expert_notes_name(name)
    has_notes = conn.execute("SELECT 1 FROM canvases WHERE name=? AND deleted_at IS NULL", (notes,)).fetchone()
    if not new:
        m = re.match(r"^(.*?)-(\d+)$", name)
        base, n = (m.group(1), int(m.group(2))) if m else (name, 1)
        new = next(f"{base}-{i}" for i in range(n + 1, n + 100)
                   if not conn.execute("SELECT 1 FROM agents WHERE name=?", (f"{base}-{i}",)).fetchone())
    model = old["model"]
    if not model:
        raise RiverError(f"the model of {name} is not recorded; start the fresh expert yourself: maxpm expert start "
                         f"<name> --item {task['id']} --model <model>")
    ended = core.expert_end(conn, name, actor, f"its context left the prompt cache; the fresh expert {new} goes on "
                                               f"from its handoff")
    summary = (f"You go on with the talk of the expert {name}: its context left the prompt cache, so a fresh session "
               f"that reads only its handoff costs less than its next message. "
               + (f"Read its handoff first: maxpm canvas show {notes}. " if has_notes else
                  f"It wrote no handoff (maxpm canvas show {notes} has nothing): read this task's context, and ask the "
                  f"person what was decided. ")
               + f"Then tell the person in one line that you continue {name}'s talk, and keep the handoff current: "
               f"maxpm --as {new} expert notes {new} --put <file>.")
    started = start_expert(conn, new, item=task["id"], model=model, actor=actor, runner=runner, summary=summary,
                           canvases=[notes] if has_notes else ())
    return {"fresh_from": name, "ended": ended, **started}


def _fit_task_model(conn, task, name, model, actor):
    """The person chose the expert's model: a min or max limit of its task that excludes it moves to that model."""
    ladder = core.parse_ladder(core.setting(conn, "model_ladder"))
    fam, pos = core._family(ladder, model)
    why = core._model_refusal(conn, core._item(conn, task["id"]), name, any_type=True)
    if fam is None or not why:
        return
    lname = ladder[fam][pos]
    core.item_edit(conn, task["id"], actor=actor, models={"max_model" if "at most" in why else "min_model": lname})


def _open_terminal(t, env, runner=None):
    """Run the agent command (t["command"]) in the project folder (t["path"]) with env set, in a new pane of the
    maxpm tmux session (#5105); on Windows, where tmux does not run, in a new console window. runner (tests) gets
    the command line that would go into the pane, and {"args", "cwd", "env"} on Windows; TMUX_RUNNER gets the
    tmux commands."""
    import os
    import shlex
    import subprocess
    runner = runner or TERMINAL_RUNNER
    # The agent uses the same queue as this page: a page on a MAXPM_DB queue starts agents on it too.
    if os.environ.get("MAXPM_DB"):
        env = {"MAXPM_DB": str(core.db_path()), **env}
    if core.PLATFORM == "win32":
        # cmd /k keeps the window open when the agent ends; the command line goes to cmd as written.
        title = f"title {t['session_title']} & " if t.get("session_title") else ""
        spec = {"args": f"cmd /k {title}{t['command']}", "cwd": t["path"], "env": dict(env)}
        try:
            (runner or (lambda s: subprocess.Popen(s["args"], cwd=s["cwd"], env={**os.environ, **s["env"]},
                                                   creationflags=subprocess.CREATE_NEW_CONSOLE)))(spec)
        except OSError as e:
            raise RiverError(f"could not open a console window in {t['path']}: {e}")
        return
    line = (f"cd {shlex.quote(t['path'])} && " + "".join(f"{k}={shlex.quote(v)} " for k, v in env.items())
            + t["command"])
    if runner:
        runner(line)
        return
    t["tmux_pane"] = _tmux_open(t, env, line)
    if t.get("first_prompt") and t.get("platform") and t["tmux_pane"]:
        hold_first_prompt(t["tmux_pane"], t["platform"], t["first_prompt"])


def hold_first_prompt(pane, kind, prompt):
    """Keep the first prompt of an agent that takes none on its command line (FIRST_PROMPT 'paste'), until
    paste_first_prompts sees its idle prompt in this pane (#5221)."""
    conn = core.connect()
    try:
        with core.tx(conn):
            conn.execute("INSERT OR REPLACE INTO pane_prompts (pane, kind, prompt, created_at) VALUES (?,?,?,?)",
                         (pane, kind, prompt, core.iso(core.now())))
    finally:
        conn.close()


def paste_first_prompts(conn):
    """One look of maxpm serve (each pass): paste each held first prompt (hold_first_prompt) into its pane, then Enter,
    once the agent's idle prompt (DETECT['idle'], _screen_state) shows there, as the message box of the Sessions tab
    sends a text of several lines. A question that the agent asks first (a trust or setup question) shows no idle
    prompt, so the prompt waits until the person has answered it. A pane that ended, or that this queue does not
    know any more, drops its prompt. Returns the panes it pasted into."""
    rows = conn.execute("SELECT pane, kind, prompt FROM pane_prompts ORDER BY created_at").fetchall()
    if not rows or not core.tmux_path() and TMUX_RUNNER is None:
        return []
    live = {p["pane"]: p for p in _tmux_panes() if _own_pane(p)}
    gone = [r["pane"] for r in rows if r["pane"] not in live or live[r["pane"]]["ended"]]
    if gone:
        with core.tx(conn):
            conn.executemany("DELETE FROM pane_prompts WHERE pane=?", [(p,) for p in gone])
    rows = [r for r in rows if r["pane"] not in gone]
    texts, done = _pane_texts([r["pane"] for r in rows]), []
    for r in rows:
        ad = core._adapters().find(r["kind"])
        if not (ad and ad.detect) or r["pane"] not in texts:
            continue
        if _screen_state(_prompt_tail(texts[r["pane"]]), ad.detect) != "idle":
            continue
        with core.tx(conn):  # first, so a failed paste is never typed twice
            if not conn.execute("DELETE FROM pane_prompts WHERE pane=?", (r["pane"],)).rowcount:
                continue  # another serve pass took it
        _tmux("set-buffer", "-b", "maxpm-send", "--", r["prompt"])
        _tmux("paste-buffer", "-p", "-d", "-b", "maxpm-send", "-t", r["pane"])
        _tmux("send-keys", "-t", r["pane"], "Enter")
        done.append(r["pane"])
    return done


# Each agent river starts is a pane of one tmux session, so one terminal shows them all
# (maxpm view), over SSH too, and nothing needs AppleScript. TMUX_CMD (tests) is a tmux command line with a
# server of its own; TMUX_RUNNER (tests) gets each argument list in its place and returns the output, or
# None for a command that fails.
TMUX_CMD = None
TMUX_SESSION = "maxpm"
TMUX_RUNNER = None
TMUX_SHELLS = {"sh", "bash", "zsh", "fish", "dash", "ksh", "csh", "tcsh", "nu"}
# One line per pane: the free text (the session's name) comes last.
_TMUX_PANE = ("#{pane_id}|#{window_id}|#{@maxpm_tile}|#{window_panes}|#{pane_current_command}|#{pane_tty}|#{@maxpm_agent}|"
              "#{@maxpm_started}|#{@maxpm_db}|#{@maxpm_name}")
# A new pane shows a shell first: tmux starts the login shell, the shell reads its files, and only then it runs
# the command line river typed. For START_GRACE seconds after river opened a pane, a shell there is a session
# that starts, not one that ended. Without it, a tidy pass a few seconds after a start closed the pane before
# the agent CLI ran: auto_tidy comes right after auto_release and fresh_sessions in the same pass of the loop,
# so every tidy_every a session serve had just started never connected (#1011).
START_GRACE = 60


def _tmux_cmd():
    """The tmux command line, or None when tmux is not installed."""
    import shutil
    if TMUX_CMD:
        return TMUX_CMD if shutil.which(TMUX_CMD[0]) else None
    exe = core.tmux_path()
    return [exe] if exe else None


def _tmux(*args, check=True):
    """Run one tmux command and return its output. When it fails: RiverError with tmux's own words, or
    None with check off (has-session: no such session; split-window: no space)."""
    import subprocess
    if TMUX_RUNNER is not None:
        out = TMUX_RUNNER(list(args))
        if out is None and check:
            raise RiverError(f"tmux {args[0]} failed")
        return out
    cmd = _tmux_cmd()
    if not cmd:
        raise RiverError("tmux is not installed: brew install tmux (Linux: apt install tmux)")
    try:
        r = subprocess.run([*cmd, *args], capture_output=True, text=True, timeout=15, env=_tmux_env())
    except (OSError, subprocess.SubprocessError) as e:
        raise RiverError(f"tmux {args[0]}: {e}")
    if r.returncode:
        why = (r.stderr or r.stdout).strip()
        # A sandboxed session (Claude Code's sandbox, Codex's) cannot reach the tmux socket at all.
        if "Operation not permitted" in why or "Permission denied" in why:
            raise RiverError(f"tmux {args[0]}: {why}. A sandbox around this session blocks the tmux socket: run "
                             f"this command outside the sandbox, or start the session from the MaximizePM page")
        if check:
            raise RiverError(f"tmux {args[0]}: {why}")
        return None
    return r.stdout.rstrip("\n")


def _tmux_env():
    """The environment for a tmux command. The first command starts the tmux server, and every pane gets
    the server's environment: when an agent session runs maxpm launch, its own name, focus, and session
    ids (MAXPM_*, CLAUDE*, CODEX_*) must stay out, or each new agent would start as a copy of that session."""
    import os
    keep = ("CLAUDE_CONFIG_DIR", "CODEX_HOME")
    return {k: v for k, v in os.environ.items() if k in keep or not k.startswith(("MAXPM_", "CLAUDE", "CODEX_"))}


def _tmux_panes(everywhere=False):
    """The panes of the maxpm tmux session (everywhere: of every session); [] when there is none. "ended": only
    a shell runs there, and the pane is older than START_GRACE ("starting": it is not)."""
    out = _tmux("list-panes", *(["-a"] if everywhere else ["-s", "-t", "=" + TMUX_SESSION]), "-F", _TMUX_PANE,
                check=False)
    rows, t = [], core.now().timestamp()
    for line in (out or "").splitlines():
        f = line.split("|", 9)
        if len(f) == 10:
            shell = f[4].lstrip("-") in TMUX_SHELLS
            starting = shell and f[7].isdigit() and t - int(f[7]) < START_GRACE
            rows.append({"pane": f[0], "window": f[1], "tile": f[2] == "1", "window_panes": int(f[3] or 1),
                         "running": f[4].lstrip("-"), "ended": shell and not starting, "starting": starting,
                         "tty": f[5], "agent": f[6] or None, "db": f[8] or None, "name": f[9]})
    return rows


def _queue_db():
    """The database of this queue, as river tags the panes it opens with it (@maxpm_db)."""
    return str(core.db_path().expanduser().resolve())


def _own_pane(p):
    """Whether this queue opened the pane: its @maxpm_db is this queue's database. A pane with no tag (opened
    before river tagged panes) belongs to the main queue only. Every maxpm serve tidies the one tmux server of
    the user, also a serve of a test or demo queue (MAXPM_DB): there the real agents are not in the queue, so it
    closed their panes while they ran. On 2026-11-10 two demo serves closed the panes of six live sessions in 15
    minutes, the session that started one of them too (#5054). A queue tidies only its own panes."""
    return p.get("db") == _queue_db() if p.get("db") else _queue_db() == _main_db()


def _main_db():
    """The main queue's database in the user's real home folder: a demo may set HOME to a folder of its own."""
    try:
        import pwd
        home = pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError):  # Windows
        home = os.path.expanduser("~")
    return str(Path(home, ".maximizepm", "maxpm.db").resolve())


def _tmux_pane_of(tty):
    """The tmux pane on this terminal device, or None (also when tmux is not installed or not running)."""
    if TMUX_RUNNER is None and not _tmux_cmd():
        return None
    try:
        return next((p for p in _tmux_panes(everywhere=True) if p["tty"] == tty), None)
    except RiverError:
        return None


def _tmux_open(t, env, line):
    """Start an agent in a new pane of the maxpm tmux session and return the pane id: a window of its own,
    or one more pane of the side-by-side window when maxpm view made one. tmux starts the pane's own login
    shell and river types the command line into it, as Terminal's do script does: the shell's PATH finds
    the agent CLI, and the pane stays (with what the agent printed) after the agent ends. Nothing takes
    the keyboard: a person who answers a prompt in another pane keeps typing there."""
    name = t.get("session_title") or t["project"]
    new = ["-c", t["path"], "-P", "-F", "#{pane_id}"]
    pane = None
    if _tmux("has-session", "-t", "=" + TMUX_SESSION, check=False) is None:
        # With no terminal attached yet, a window gets this size; it follows the terminal that attaches.
        pane = _tmux("new-session", "-d", "-s", TMUX_SESSION, "-n", name, "-x", "200", "-y", "50", *new)
        _tmux("set-option", "-t", f"={TMUX_SESSION}:", "default-size", "200x50")
    else:
        tile = next((p["window"] for p in _tmux_panes() if p["tile"]), None)
        if tile:  # no space left there for one more pane: a window of its own
            pane = _tmux("split-window", "-d", "-t", tile, *new, check=False)
            if pane:
                _tmux("select-layout", "-t", tile, "tiled")
        pane = pane or _tmux("new-window", "-d", "-t", f"={TMUX_SESSION}:", "-n", name, *new)
    _tmux("set-option", "-p", "-t", pane, "@maxpm_name", name)
    _tmux("set-option", "-p", "-t", pane, "@maxpm_started", str(int(core.now().timestamp())))
    _tmux("set-option", "-p", "-t", pane, "@maxpm_db", _queue_db())  # only this queue tidies the pane (#5054)
    if env.get("MAXPM_AGENT"):
        _tmux("set-option", "-p", "-t", pane, "@maxpm_agent", env["MAXPM_AGENT"])
    _tmux("send-keys", "-t", pane, "-l", line)
    _tmux("send-keys", "-t", pane, "Enter")
    return pane


def _pane_texts(panes):
    """{pane: the text it shows now} for these pane ids, with one tmux command; {} when a pane closed meanwhile."""
    import secrets
    if not panes:
        return {}
    mark, args = f"maxpm-pane-{secrets.token_hex(8)}:", []
    for n, pane in enumerate(panes):  # a line of our own before each pane's text says where it starts
        args += [";", "display-message", "-p", f"{mark}{n}", ";", "capture-pane", "-p", "-t", pane]
    out, texts, at = _tmux(*args[1:], check=False), {}, None
    for line in (out or "").splitlines():
        if line.startswith(mark) and line[len(mark):].isdigit():
            at = panes[int(line[len(mark):])]
            texts[at] = []
        elif at is not None:
            texts[at].append(line)
    return {pane: "\n".join(lines) for pane, lines in texts.items()}


def _prompt_tail(text):
    """The last lines of a pane's text (no blank ones): where a prompt shows. Claude Code blinks the dot (⏺)
    of the tool call that waits on a permission prompt, so it is a space on every other look. A space in its
    place gives the same lines on each look, and a prompt that stays on the screen stays the same (#4280)."""
    import re
    lines = [x.rstrip() for x in text.splitlines() if x.strip()]
    return [re.sub(r"^(\s*)⏺", r"\1 ", x) for x in lines[-PROMPT_LINES:]]


def _done_panes(conn, panes, terminals=None):
    """{pane: why} for the panes whose session is done, which maxpm view --tidy closes. A pane is done when
    only a shell runs there: the agent CLI ended (a pane river opened less than START_GRACE ago still starts).
    With the queue (conn) a pane is also done while the agent CLI
    stays open and idle, when the agent river named for it takes no work any more: it is not registered (its
    maxpm wait ended, or it unregistered), it ended after a stop (or ran no river command for away_after
    after one), or it is gone, and it holds and owns nothing.
    A registered agent whose process runs in the pane decides in place of the named one (a session that began
    again under another name). A pane that shows a prompt (prompt_pattern) is never done by the queue's word:
    a person may want to answer it; but the pane of an agent with a stop request is done with its prompt too,
    since an answer would only let it read the stop and end. A pane with no agent name (a session for a person's
    item) is done only by its shell. terminals: agent_terminals(conn), when the caller has it."""
    import re
    panes = [p for p in panes if _own_pane(p)]  # another queue's pane is never done here (#5054)
    done = {p["pane"]: "its agent ended" for p in panes if p["ended"]}
    if conn is None:
        return done
    here = {}
    for agent, pane in (agent_terminals(conn) if terminals is None else terminals).items():
        here.setdefault(pane, []).append(agent)
    maybe, stopped = {}, set()
    registered = {r["name"] for r in conn.execute("SELECT name FROM agents")}
    for p in panes:
        # The named agent is registered and its process runs in another terminal: nothing here says it is done.
        if p["ended"] or not p["agent"] or (p["agent"] in registered and not here.get(p["pane"])):
            continue
        why = f"{p['agent']} is not in the queue any more"
        for name in here.get(p["pane"], ()):
            a = core.agent_status(conn, name)
            # A stop is a request: the agent may still commit. It ended when its last go ran (role stopped); one
            # that ran no river command for away_after never read the stop, and river refuses its claims.
            idle = core.now() - core.parse_iso(a["last_seen"]) > core.parse_duration(core.setting(conn, "away_after", agent=name))
            ended = a["state"] == "gone" or (a["state"] == "stopped" and (a["role"] == "stopped" or idle))
            if not ended or a["holds"] or a["owns"] or conn.execute(
                    "SELECT 1 FROM goals WHERE owner=? AND status='open'", (name,)).fetchone():
                break
            why = (f"{name} is gone" if a["state"] == "gone" else f"{name} stopped" if a["role"] == "stopped"
                   else f"{name} was asked to stop and is idle")
            if a.get("stop_at"):
                stopped.add(p["pane"])
        else:
            maybe[p["pane"]] = why
    pattern = core.setting(conn, "prompt_pattern").strip()
    texts = _pane_texts(sorted(set(maybe) - stopped)) if pattern else dict.fromkeys(maybe, "")
    agent_of = {p["pane"]: (here.get(p["pane"]) or [p["agent"]])[0] for p in panes}
    for pane, why in maybe.items():
        rx, layout, _ = _pane_rule(conn, agent_of.get(pane)) if pattern else (None, None, None)
        if pane in stopped or (pane in texts and not (rx and _prompt_found(_prompt_tail(texts[pane]), rx, layout))):
            done[pane] = why
    return done


def tmux_done(conn, terminals=None):
    """The panes maxpm view --tidy closes now, for the page: [{"pane", "name", "why"}]; [] with no tmux."""
    if TMUX_RUNNER is None and not _tmux_cmd():
        return []
    try:
        panes = [p for p in _tmux_panes() if p["name"]]
        done = _done_panes(conn, panes, terminals)
    except RiverError:
        return []
    return [{"pane": p["pane"], "name": p["name"], "why": done[p["pane"]]} for p in panes if p["pane"] in done]


def tmux_view(layout=None, tidy=False, conn=None):
    """The agents river started in tmux, for maxpm view. layout "tile" puts every agent
    pane side by side in one window, and new agents then join it; "windows" gives each agent a window of
    its own again; None changes nothing. tidy first closes the panes whose session is done (_done_panes:
    only a shell runs there, or, with conn, the agent takes no work any more). Panes that a person made are
    left alone. Returns the panes (each with "done": why --tidy closes it, or None), the panes it closed, and
    the tmux command that shows the session: attach, or switch-client inside tmux."""
    import os
    target = "=" + TMUX_SESSION
    if _tmux("has-session", "-t", target, check=False) is None:
        raise RiverError(f"no agent runs in tmux (there is no tmux session {TMUX_SESSION!r}): start one with "
                         f"maxpm launch, or from the page")
    mine = lambda: [p for p in _tmux_panes() if p["name"]]
    closed, left = [], []
    if tidy:
        panes = mine()
        done = _done_panes(conn, panes)
        for p in panes:
            if p["pane"] in done and _tmux("kill-pane", "-t", p["pane"], check=False) is not None:
                closed.append({"pane": p["pane"], "name": p["name"], "why": done[p["pane"]]})
        if closed and _tmux("has-session", "-t", target, check=False) is None:
            return {"session": TMUX_SESSION, "panes": [], "closed": closed, "left": [], "layout": layout, "show": None}
    panes = mine()
    tile = next((p["window"] for p in panes if p["tile"]), None)
    if layout == "tile" and panes:
        if tile is None:
            tile = panes[0]["window"]
            _tmux("set-option", "-w", "-t", tile, "@maxpm_tile", "1")
            _tmux("rename-window", "-t", tile, "agents")
            # Each pane shows its session's name on its top border.
            _tmux("set-option", "-w", "-t", tile, "pane-border-status", "top")
            _tmux("set-option", "-w", "-t", tile, "pane-border-format", " #{@maxpm_name} ")
        for p in panes:
            if p["window"] != tile:
                if _tmux("join-pane", "-d", "-s", p["pane"], "-t", tile, check=False) is None:
                    left.append(p["name"])  # no space for one more pane: it keeps its window
                _tmux("select-layout", "-t", tile, "tiled")
        _tmux("select-layout", "-t", tile, "tiled")
        _tmux("select-window", "-t", tile)
    elif layout == "windows" and tile:
        inside = [p for p in panes if p["window"] == tile]
        for p in inside[1:]:
            _tmux("break-pane", "-d", "-s", p["pane"], "-n", p["name"], "-t", f"{target}:")
        _tmux("set-option", "-w", "-u", "-t", tile, "@maxpm_tile")
        _tmux("set-option", "-w", "-u", "-t", tile, "pane-border-status")
        _tmux("set-option", "-w", "-u", "-t", tile, "pane-border-format")
        if inside:
            _tmux("rename-window", "-t", tile, inside[0]["name"])
    panes = mine()
    done = _done_panes(conn, panes)
    for p in panes:
        p["done"] = done.get(p["pane"])
    # The keys a person needs, in the status line for a few seconds after the session shows.
    prefix = (_tmux("show-options", "-gv", "prefix", check=False) or "C-b").replace("C-", "Ctrl-")
    hint = f"{prefix} then: an arrow = the next pane, z = one pane large (and back), n = the next window, d = leave"
    if done:  # first, so a narrow terminal still shows it
        hint = f"{len(done)} done (maxpm view --tidy closes {'it' if len(done) == 1 else 'them'}) | {hint}"
    show = ["switch-client", "-t", target] if os.environ.get("TMUX") else ["attach-session", "-t", target]
    return {"session": TMUX_SESSION, "panes": panes, "closed": closed, "left": left, "layout": layout,
            "show": [*(_tmux_cmd() or ["tmux"]), *show, ";", "display-message", "-d", "6000", hint]}


def tmux_tidy(conn):
    """The page's Close button for the sessions that are done: the same as maxpm view --list --tidy."""
    return {"closed": tmux_view(None, tidy=True, conn=conn)["closed"]}


# maxpm serve tidies by itself (tidy_every). TIDY: when its last pass ended, and for each pane it may close while
# the agent CLI is still open, the pane's screen and since when that screen has not changed.
TIDY = {"at": None, "screens": {}}


def auto_tidy(conn):
    """One pass of the loop of maxpm serve (every notify_interval): every tidy_every (0s: never) it closes the panes
    that maxpm view --tidy closes (_done_panes). A pane where the agent CLI still runs closes only when its screen
    stayed the same for idle_after: a screen that changes is a busy agent. Such a pane is read again on the next
    passes, until it is idle or no longer done. A pane whose agent CLI ended before its agent ran a river command
    (a start that failed) leaves its last lines in the history of the item it was started for, so a person sees
    why. Returns the panes it closed: [{"pane", "name", "why"}]."""
    import re
    every = core.parse_duration(core.setting(conn, "tidy_every"))
    t = core.now()
    if not every.total_seconds() or (TIDY["at"] and t - TIDY["at"] < every):
        return []
    tidy_uploads()  # files sent to sessions, after UPLOAD_DAYS
    if TMUX_RUNNER is None and not _tmux_cmd():
        return []
    try:
        if _tmux("has-session", "-t", "=" + TMUX_SESSION, check=False) is None:
            TIDY.update(at=t, screens={})  # no agent runs in tmux
            return []
        panes = [p for p in _tmux_panes() if p["name"]]
        done = _done_panes(conn, panes)
        open_cli = [p["pane"] for p in panes if p["pane"] in done and not p["ended"]]
        failed = {}
        for p in panes:
            a = p["pane"] in done and p["ended"] and p["agent"] and conn.execute(
                "SELECT note FROM agents WHERE name=? AND last_seen=registered_at", (p["agent"],)).fetchone()
            if a:
                m = re.search(r"#(\d+)", a["note"] or "")
                failed[p["pane"]] = (p["agent"], int(m.group(1)) if m else None)
        texts = _pane_texts(open_cli + sorted(failed)) if open_cli or failed else {}
    except RiverError:
        return []
    after = core.parse_duration(core.setting(conn, "idle_after"))
    screens, busy, closed = {}, False, []
    for p in panes:
        why = done.get(p["pane"])
        if not why:
            continue
        if not p["ended"]:
            if p["pane"] not in texts:
                continue  # the pane closed since the list
            st = TIDY["screens"].get(p["pane"])
            if not st or st["sig"] != texts[p["pane"]]:
                st = {"sig": texts[p["pane"]], "since": t}
            screens[p["pane"]] = st
            if t - st["since"] < after:
                busy = True
                continue
        if p["pane"] in failed and p["pane"] in texts:
            agent, item = failed[p["pane"]]
            tail = " | ".join(_prompt_tail(_plain(texts[p["pane"]]))[-8:])
            with core.tx(conn):
                known = item and conn.execute("SELECT 1 FROM items WHERE id=?", (item,)).fetchone()
                core._event(conn, item if known else None, "maxpm",
                            f"{agent} ended before it ran a maxpm command; the last lines of its pane {p['pane']}: "
                            + (tail[-800:] or "(empty)"))
        if _tmux("kill-pane", "-t", p["pane"], check=False) is not None:
            closed.append({"pane": p["pane"], "name": p["name"], "why": why})
            screens.pop(p["pane"], None)
            # The history says when a pane closed and why: a session that disappeared can be traced (#5054).
            with core.tx(conn):
                core._event(conn, None, "maxpm", f"tidy closed the tmux pane {p['pane']} ({p['name']}): {why}")
    TIDY["screens"] = screens
    if not busy:
        TIDY["at"] = t
    return closed


# The tests start tmux servers of their own (TMUX_CMD: tmux -S <tmp>/tmux.sock -f <tmp>/tmux.conf). A test that
# deleted its temporary folder before it ended its server left the server running with no socket, so no tmux
# command reaches it again. maxpm serve ends such servers every tidy_every (auto_end_orphans), and maxpm view
# --orphans lists them (--tidy ends them now). ORPHAN_PS (tests) returns the lines of ps: "pid ppid etime args".
ORPHAN_SESSIONS = ("maxpm", "river")  # river: the session's name in the first versions
ORPHAN_MIN_AGE = 60  # seconds: a server that just started may not have made its socket yet
ORPHANS = {"at": None}
ORPHAN_PS = None


def _temp_folders():
    """The folders where the tests' temporary folders are: the system's, also as their real paths."""
    import tempfile
    roots = {"/tmp", "/private/tmp", "/var/folders", "/private/var/folders", tempfile.gettempdir()}
    return tuple(roots | {os.path.realpath(r) for r in roots})


def tmux_orphans():
    """The tmux servers a MaximizePM test started and left behind: [{"pid", "socket", "session", "age"}]. Only an
    exact command line counts, tmux -S <D>/tmux.sock -f <D>/tmux.conf new-session -d -s maxpm|river, with D a folder
    tmp* in a temporary folder of the system. And only when <D>/tmux.sock is gone, the server ran ORPHAN_MIN_AGE, and
    each process below it is a shell. So never the default tmux server (where maxpm serve starts agents), a server
    a tmux command still reaches, or one with an agent CLI or another command in a pane. RiverError with no ps."""
    import subprocess
    if core.PLATFORM == "win32" and ORPHAN_PS is None:
        return []
    try:
        text = ORPHAN_PS() if ORPHAN_PS else subprocess.run(
            ["ps", "-A", "-ww", "-o", "pid=,ppid=,etime=,args="], capture_output=True, text=True, timeout=10,
            check=True).stdout
    except (OSError, subprocess.SubprocessError) as e:
        raise RiverError(f"ps: {e}. A sandbox around this session may block it: run this command outside the sandbox")
    procs, below = {}, {}
    for line in text.splitlines():
        f = line.split(None, 3)
        try:
            pid, ppid, age = int(f[0]), int(f[1]), core._elapsed(f[2])
        except (IndexError, ValueError):
            continue
        procs[pid] = {"age": age, "args": f[3] if len(f) > 3 else ""}
        below.setdefault(ppid, []).append(pid)
    # normpath: on Windows the command line has D/tmux.sock, and os.path joins with a backslash.
    inside, out = tuple(os.path.join(os.path.normpath(r), "") for r in _temp_folders()), []
    for pid, p in procs.items():
        a = p["args"].split()
        if (len(a) < 9 or os.path.basename(a[0]) != "tmux" or a[1] != "-S" or a[3] != "-f"
                or a[5:8] != ["new-session", "-d", "-s"]):
            continue
        folder, sock = os.path.split(os.path.normpath(a[2]))
        if (sock != "tmux.sock" or os.path.normpath(a[4]) != os.path.join(folder, "tmux.conf") or a[8] not in ORPHAN_SESSIONS
                or not os.path.basename(folder).startswith("tmp") or not folder.startswith(inside)
                or os.path.exists(a[2]) or p["age"] < ORPHAN_MIN_AGE):
            continue
        todo, shells = list(below.get(pid, ())), True
        while todo and shells:
            child = todo.pop()
            first = (procs[child]["args"].split() or [""])[0]
            shells = os.path.basename(first).lstrip("-") in TMUX_SHELLS
            todo += below.get(child, ())
        if shells:
            out.append({"pid": pid, "socket": a[2], "session": a[8], "age": p["age"]})
    return sorted(out, key=lambda o: -o["age"])


def end_tmux_orphans():
    """End the servers tmux_orphans finds (SIGTERM: tmux closes its panes and exits). Returns them, each with
    "ended": True, or False when the signal failed."""
    import signal
    found = tmux_orphans()
    for o in found:
        try:
            os.kill(o["pid"], signal.SIGTERM)
            o["ended"] = True
        except OSError:
            o["ended"] = False
    return found


HOOKS = {"running": 0}  # the hooks that a thread of maxpm serve runs now; the loop does not start again meanwhile


def hooks_soon(conn, token=None, cwd=None):
    """Run the hooks that wait (core.run_hooks) in a thread of their own, and return whether one started. With a
    token: those of the events that an action of the page caused, so the page gets its answer at once. With
    none (one pass of the loop of maxpm serve): those whose command ended before it ran them. A command in the
    sandbox of an agent session asks for its own token (the op run_hooks, #1826) and reads the results from the
    queue. Each result goes to the history of its deploy item, and a failure alerts the target owner."""
    q = "SELECT 1 FROM hook_runs WHERE started_at IS NULL AND " + ("queued_by=?" if token else "queued_at<?")
    old = core.iso(core.now() - core.timedelta(seconds=core.HOOK_ORPHAN_AFTER))
    if conn.execute(q, (token or old,)).fetchone() is None:
        return False

    def work():
        HOOKS["running"] += 1
        try:
            c = core.connect()
            try:
                for h in core.run_hooks(c, cwd, token=token, orphans=token is None):
                    print(f"hook {h['event']} of target {h['target']}: {h['result']}: {h['command']}", flush=True)
            finally:
                c.close()
        except Exception as e:  # a busy queue: the next pass of the loop takes the hook
            print(f"hook: {e}", flush=True)
        finally:
            HOOKS["running"] -= 1
    threading.Thread(target=work, daemon=True).start()
    return True


def auto_end_orphans(conn):
    """One pass of the loop of maxpm serve: every tidy_every (0s: never) it ends the tmux servers the tests left
    behind (tmux_orphans). Returns the servers it ended."""
    every = core.parse_duration(core.setting(conn, "tidy_every"))
    t = core.now()
    if not every.total_seconds() or (ORPHANS["at"] and t - ORPHANS["at"] < every):
        return []
    ORPHANS["at"] = t
    try:
        return [o for o in end_tmux_orphans() if o["ended"]]
    except RiverError:
        return []


ADOPT = {"failed": {}}  # agent -> when its last move into tmux failed: the next try waits ADOPT_RETRY
ADOPT_RETRY = 600


def adopt_sessions(conn, runner=None, end=None):
    """One pass of the loop of maxpm serve (tmux_adopt, #4598): each agent session that runs outside tmux on this
    computer and is quiet at its prompt (sessions.adopt_check) moves into a tmux pane. Returns {agent: pane}."""
    from . import sessions
    if TMUX_RUNNER is None and not _tmux_cmd():
        return {}
    terminals = agent_terminals(conn)
    agents = [a for a in sessions.running(conn, terminals) if not terminals.get(a["name"])]
    # Never end a process that runs in a tmux pane, whatever name the queue gives it: look at its terminal device
    # itself. When ps gives no answer, nothing moves (#5060).
    ttys = _pid_ttys(sorted({str(a["pid"]) for a in agents if a.get("pid")})) if agents and core.PLATFORM != "win32" else {}
    if ttys is None:
        return {}
    try:
        in_tmux = {p["tty"] for p in _tmux_panes(everywhere=True)} if ttys else set()
    except RiverError:
        return {}
    agents = [a for a in agents if ttys.get(str(a.get("pid"))) not in in_tmux]
    waiting = core.waits_on_person(conn, [a["name"] for a in agents]) if agents else {}
    moved, t = {}, time.time()
    for a in agents:
        if t - ADOPT["failed"].get(a["name"], 0) < ADOPT_RETRY:
            continue
        kind, path = sessions.transcript(conn, a["name"])
        if not path:
            continue
        try:
            tail = sessions.entries(kind, path, None, 1, sessions.CARD_BYTES)
        except (OSError, ValueError, EOFError):
            continue
        if sessions.adopt_check(conn, a, kind, path, tail["turn"], tail["cwd"], waiting)[0] != "now":
            continue
        try:
            moved[a["name"]] = move_into_tmux(conn, a, kind, path, tail["cwd"], runner, end)
        except RiverError as e:
            manager = core.active_manager(conn) if "model" in str(e) else None
            body = f"tmux_adopt did not move {a['name']} into tmux: {e}"
            if manager and manager != a["name"] and not conn.execute(  # once: the manager decides the model (#5064)
                    "SELECT 1 FROM messages WHERE to_agent=? AND body=?", (manager, body)).fetchone():
                with core.tx(conn):
                    core._send(conn, "notice", "maxpm", body, to=manager)
            ADOPT["failed"][a["name"]] = t
            print(f"tmux_adopt: {a['name']}: {e}", flush=True)
    return moved


REVIVE = {"at": {}}  # agent -> when maxpm serve last resumed it: the next resume waits REVIVE_RETRY
REVIVE_RETRY = 600


def revive_sessions(conn, runner=None):
    """One pass of the loop of maxpm serve (revive_sessions, #5061; mark 2026-11-10: 'the session is missing from
    tmux so we need to get it back there'): a session whose process ended with no stop, End or unregister (a
    crash, a closed window or pane, a pane that another serve closed as on 2026-11-10, #5054) resumes its chat in
    a tmux pane at once, with the same name, model, item and context, and the manager gets a notice. An end on
    purpose sets a stop (maxpm stop, End, kill) or removes the agent (unregister, wait END, idle_end, expert end):
    such a session stays ended. Only a session that holds an item, a goal or a target, or the manager when no other
    is active, and only one that ran a river command or a hook in the last revive_within. A session resumes at
    most once per REVIVE_RETRY. Returns {agent: pane}."""
    from . import sessions
    if core.setting(conn, "revive_sessions") != "on" or core.PLATFORM == "win32" or (
            TMUX_RUNNER is None and not _tmux_cmd()):
        return {}
    since = core.iso(core.now() - core.parse_duration(core.setting(conn, "revive_within")))
    rows = conn.execute(
        "SELECT * FROM agents WHERE kind='ai' AND stop_at IS NULL AND pid IS NOT NULL AND host=? AND pid_cmd IS NOT "
        "NULL AND coalesce(via, '')<>'http' AND max(last_seen, coalesce(activity_at, '')) >= ? ORDER BY name",
        (core.this_host(), since)).fetchall()
    out, t = {}, time.time()
    for r in rows:
        name, pid = r["name"], r["pid"]
        if t - REVIVE["at"].get(name, 0) < REVIVE_RETRY:
            continue
        if core.pid_alive(pid):
            info = core.proc_info(pid)
            if info is None or info[2] == r["pid_cmd"]:
                continue  # it runs (or ps cannot tell)
        a = core.agent_status(conn, name)
        goal = conn.execute("SELECT 1 FROM goals WHERE owner=? AND status='open'", (name,)).fetchone()
        manager = r["role"] == "manager" and core.active_manager(conn, but=name) is None
        if not (a["holds"] or a["owns"] or goal or manager):
            continue
        kind, path = sessions.transcript(conn, name)
        if not path:
            continue
        try:
            cwd = sessions.entries(kind, path, None, 1, sessions.CARD_BYTES)["cwd"]
        except (OSError, ValueError, EOFError):
            continue
        REVIVE["at"][name] = t
        held = ", ".join(f"#{h['id']}" for h in a["holds"]) or ("the manager" if manager else "its goal or target")
        try:
            pane = move_into_tmux(conn, a, kind, path, cwd, runner, ended=True,
                                  why="ended with no stop (revive_sessions) and resumes in a tmux pane")
            body = (f"{name} ended with no stop (its process {pid} is gone) while it held {held}: MaximizePM resumed "
                    f"its chat in tmux pane {pane or '?'}. If it ended on purpose: maxpm stop {name} --reason \"...\"")
            out[name] = pane
        except RiverError as e:
            body = f"{name} ended with no stop (its process {pid} is gone) while it held {held}, and did not resume: {e}"
            print(f"revive_sessions: {name}: {e}", flush=True)
        to = core.active_manager(conn, but=name)
        if to:
            with core.tx(conn):
                core._send(conn, "notice", "maxpm", body, to=to)
    return out


def _end_process(pid, grace=5.0):
    """End a process: taskkill on Windows; SIGTERM then SIGKILL on Unix. True when it has ended."""
    if os.name == "nt":
        import subprocess
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        return not core.pid_alive(pid)
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    for _ in range(int(grace / 0.2)):
        time.sleep(0.2)
        try:
            os.waitpid(pid, os.WNOHANG)  # a child of this process (tests): reap it
        except (ChildProcessError, OSError):
            pass
        if not core.pid_alive(pid):
            return True
    os.kill(pid, signal.SIGKILL)
    time.sleep(0.2)
    return not core.pid_alive(pid)


def move_into_tmux(conn, a, kind, path, cwd, runner=None, end=None, overrides=None,
                   why="moves into a tmux pane (tmux_adopt)", ended=False):
    """Move one agent session into a tmux pane (tmux_adopt): end its process (only one process may write a chat),
    then resume the same chat in a new pane of the maxpm tmux session, in its folder, with the same MAXPM_AGENT.
    The agent keeps its items, goals and targets: its leases run from now, and its old process id is cleared, so
    the queue does not count it as gone before the new process runs a river command. Returns the pane."""
    from . import sessions
    name, pid = a["name"], a["pid"]
    # The chat to resume: the address of an agent whose log is found by it (Codex's thread id), else the session id
    # that names the log file (Claude Code), #5182.
    ad = sessions._adapter(kind)
    sid = a.get("native_address") if ad and ad.chat_ref() == "address" else Path(path).name.split(".")[0]
    held = conn.execute("SELECT project_id FROM items WHERE assignee=? AND status IN ('in_progress','held') "
                        "ORDER BY id LIMIT 1", (name,)).fetchone()
    names = core.projects_for_dir(conn, cwd)
    project_id = held["project_id"] if held else core._project(conn, names[0])["id"] if names else None
    platform = core._platform_of(core._adapters().get(kind))  # the adapter of the chat's kind (#5168)
    t = core.resume_command(conn, name, platform, sid, project_id, overrides, transcript=path)
    if not ended:  # ended: its process ended already (revive_sessions); a pid that came again is another's
        info = core.proc_info(pid)
        if info is not None and info[2] != a["pid_cmd"]:
            raise RiverError(f"PID {pid} now runs another command ({info[2][:80]}); nothing moved")
        if not (end or _end_process)(pid):
            raise RiverError(f"its process {pid} did not end; nothing moved")
    with core.tx(conn):
        conn.execute("UPDATE agents SET pid=NULL, pid_cmd=NULL, busy_at=NULL WHERE name=?", (name,))
        core._touch_agent(conn, name)
        core._event(conn, None, "maxpm", f"{name} {why}: its process {pid} ended, and the same chat resumes "
                                         f"({t['agent']})")
    spec = {"command": t["command"], "path": cwd, "project": name, "session_title": a.get("session") or name,
            "platform": t.get("platform"), "first_prompt": t.get("first_prompt")}
    _open_terminal(spec, t["env"], runner)
    return spec.get("tmux_pane")


def _quiet_session(conn, agent, who):
    """(agent_status, kind, path, tail) of a session a person may change now: a person asks, its transcript is here,
    and its turn has ended with no prompt open. RiverError says what stands in the way."""
    from . import sessions
    row = conn.execute("SELECT kind FROM agents WHERE name=?", (who,)).fetchone() if who else None
    if row and row["kind"] != "human":
        raise RiverError("only a person changes a session's settings")
    a = core.agent_status(conn, agent)
    kind, path = sessions.transcript(conn, agent)
    if not path:
        raise RiverError(f"no transcript of {agent} on this computer, so MaximizePM cannot tell when it is quiet")
    tail = sessions.entries(kind, path, None, 1, sessions.CARD_BYTES)
    if tail["turn"] != "idle" or a.get("activity_prompt") or core.waits_on_person(conn, [agent]):
        raise RiverError(f"{agent} works now or waits on a prompt or an answer; change its settings when its turn has ended")
    return a, kind, path, tail


def session_setting_live(conn, agent, key, value, who=None):
    """The Settings dialog (#4625): change one setting of a running session in place, where its provider allows
    it (Claude Code /model, /effort): the command goes into its tmux pane while the session is quiet."""
    from . import sessions
    a, kind, path, _tail = _quiet_session(conn, agent, who)
    prov = sessions.provider(a.get("agent_type"), kind)["id"]
    spec = sessions.setting_spec(prov, key)
    if not spec.get("live"):
        raise RiverError(f"{spec['label']} changes only with a restart: use Restart with these settings")
    value = sessions.check_value(spec, value, conn, prov)
    if pane_prompts(conn, {agent: _typing_pane(conn, agent, who)}).get(agent):
        raise RiverError(f"a prompt waits in the terminal of {agent}: answer it first")
    terminal_keys(conn, agent, [{"text": spec["live"].format(value=value)}, {"key": "Enter"}], who)
    with core.tx(conn):
        core._event(conn, None, who or "maxpm", f"{agent}: {spec['label']} set to {value} in place")
    return {"agent": agent, "key": key, "value": value}


def session_restart(conn, agent, changes, make_default=False, who=None, runner=None, end=None):
    """The Settings dialog (#4625): end a quiet session and resume the same chat with new settings (the way
    tmux_adopt moves a session), so it keeps its name, items and context. make_default: also make each changed
    provider option the default (maxpm config claude_* or codex_*)."""
    from . import sessions
    if not isinstance(changes, dict) or not changes:
        raise RiverError("no setting changed")
    a, kind, path, tail = _quiet_session(conn, agent, who)
    if not (a.get("pid") and a.get("host") == core.this_host() and a.get("pid_cmd") and core.pid_alive(a["pid"])):
        raise RiverError(f"the process of {agent} on this computer is not known; restart it by hand")
    if not tail.get("cwd"):
        raise RiverError(f"the transcript of {agent} does not name its folder")
    prov = sessions.provider(a.get("agent_type"), kind)["id"]
    ov, done, defaults = {"opts": {}, "flags": [], "noflags": []}, [], []
    for key, value in changes.items():
        spec = sessions.setting_spec(prov, key)
        value = sessions.check_value(spec, value, conn, prov)
        if spec["restart"] in ("model", "effort"):
            ov[spec["restart"]] = value
        elif spec["restart"] == "opt":
            ov["opts"][key] = value
        else:
            (ov["flags"] if value == "on" else ov["noflags"]).append(spec["flag"])
        done.append(f"{spec['label']} {value}")
        if make_default and spec.get("default"):
            defaults.append((spec["default"], value))
    ov["prompt"] = (f"MaximizePM restarted this session with new settings ({'; '.join(done)}); your name, items and "
                    f"context are the same. Start again any background command you had running, then continue where "
                    f"you stopped: run maxpm --as {agent} go if you are not sure.")
    pane = move_into_tmux(conn, a, kind, path, tail["cwd"], runner, end, ov, f"restarted with new settings ({'; '.join(done)})")
    for k, v in defaults:
        core.config_set(conn, k, v)
    return {"agent": agent, "pane": pane, "changed": done, "defaults": [k for k, _ in defaults]}


def tmux_orphans_view(end=False):
    """maxpm view --orphans: the servers tmux_orphans finds; with end (--tidy), it ends them first."""
    return {"orphans": end_tmux_orphans() if end else tmux_orphans(), "ended": end}


# The keys the page may send to an agent's terminal by name (tmux's names); any other input is plain text.
TERMINAL_KEYS = ("Enter", "Escape", "Tab", "BTab", "BSpace", "DC", "Space", "Up", "Down", "Left", "Right", "Home", "End",
                 "PPage", "NPage", *(f"C-{c}" for c in "abcdefghijklmnopqrstuvwxyz"))


def agent_terminals(conn):
    """{agent: pane} for the agents that run in a tmux pane on this host, in any tmux session: the page
    shows such an agent's terminal. An agent has the pane on the terminal device of its process; a session
    river started that ran no river command yet (it may wait on a prompt, which is when a person most
    needs its terminal) has the pane that got its name at launch."""
    import subprocess
    if TMUX_RUNNER is None and not _tmux_cmd():
        return {}
    try:
        panes = _tmux_panes(everywhere=True)
    except RiverError:
        return {}
    if not panes:
        return {}
    agents = [dict(r) for r in conn.execute("SELECT name, pid, host FROM agents WHERE kind='ai'")]
    out = {}
    # Several agents can have one pid (a command with --as for another agent wrote it there before #5060): each of
    # them maps to the pane. With one name per pid, the other agent counted as outside tmux, and tmux_adopt ended
    # the manager's process while it ran in its pane (2026-11-10).
    pids = {}
    for a in agents:
        if a["pid"] and a["host"] == core.this_host():
            pids.setdefault(str(a["pid"]), []).append(a["name"])
    if pids and core.PLATFORM != "win32":
        by_tty = {p["tty"]: p["pane"] for p in panes}
        for pid, tty in (_pid_ttys(sorted(pids)) or {}).items():
            pane = by_tty.get(tty)
            for name in (pids.get(pid, ()) if pane else ()):
                out[name] = pane
    for p in panes:
        if p["agent"] and p["pane"] not in out.values() and any(a["name"] == p["agent"] for a in agents):
            out.setdefault(p["agent"], p["pane"])
    return out


PS_RUNNER = None  # tests: gets the pids, returns the lines "pid tty"


def _pid_ttys(pids):
    """{pid: its terminal device, /dev/ttysNNN} for these pids (strings) that run on a terminal; None when ps fails.
    No pids: {} with no ps (#5075: 'ps -p ""' on macOS writes bytes that are not UTF-8 to stderr, and the decode
    error stopped the loop of maxpm serve at adopt_sessions)."""
    import subprocess
    pids = list(pids)
    if not pids:
        return {}
    try:
        ps = (PS_RUNNER or (lambda p: subprocess.run(["ps", "-o", "pid=,tty=", "-p", ",".join(p)], capture_output=True,
                                                     text=True, errors="replace", timeout=5).stdout))(pids)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    out = {}
    for line in ps.splitlines():
        pid, _, tty = line.strip().partition(" ")
        tty = tty.strip()
        if tty and tty not in ("?", "??"):
            out[pid] = tty if tty.startswith("/dev/") else "/dev/" + tty
    return out


def terminal_screen(conn, agent):
    """What an agent's tmux pane shows now, for the page: the lines with their colours (escape sequences),
    the size, and the cursor. One tmux command."""
    pane = agent_terminals(conn).get(agent)
    if not pane:
        raise RiverError(f"{agent} does not run in a tmux pane on this computer (a session that maxpm launch or the page "
                         f"started runs in one)")
    return _pane_screen(agent, pane)


def _pane_screen(agent, pane):
    out = _tmux("display-message", "-p", "-t", pane,
                "#{pane_width}|#{pane_height}|#{cursor_x}|#{cursor_y}|#{pane_current_command}|"
                "#{session_name}:#{window_index}.#{pane_index}|#{pane_tty}|#{@maxpm_name}",
                ";", "capture-pane", "-p", "-e", "-t", pane)
    head, _, text = out.partition("\n")
    f = head.split("|", 7)
    if len(f) != 8:
        raise RiverError(f"tmux gave no answer for pane {pane}")
    return {"agent": agent, "pane": pane, "width": int(f[0]), "height": int(f[1]), "cursor": [int(f[2]), int(f[3])],
            "ended": f[4].lstrip("-") in TMUX_SHELLS, "running": f[4].lstrip("-"), "target": f[5], "tty": f[6],
            "name": f[7], "text": text}


# An agent that waits on a prompt in its terminal (a permission prompt, a trust question) stops, and nobody
# sees it until a person looks. PROMPTS remembers, for each agent whose screen shows a prompt now, the lines
# river saw ("sig"), since when, and the alert it sent for them ("told").
PROMPTS = {}
PROMPT_LINES = 15  # a prompt is at the end of the screen: text further up is what the agent wrote before
_ESCAPES = None


def _plain(text):
    """A pane's text without its colours and other escape sequences."""
    import re
    global _ESCAPES
    _ESCAPES = _ESCAPES or re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]")
    return _ESCAPES.sub("", text)


# The layout of a blocked screen (the menu marker, the frame, the rule, the key hint) and its words are an agent's
# adapter's DETECT (river/agents, #5183); river/agents DETECT_LAYOUT holds the layout of an agent whose adapter
# leaves it out, and of a pane whose agent has no adapter.
_LAYOUTS = {}


def _layout(detect=None):
    """The compiled layout of a blocked screen: {"box", "mark", "rule", "hint", "tell"}, from an adapter's DETECT
    (or None: river/agents DETECT_LAYOUT)."""
    from . import agents
    d = {**agents.DETECT_LAYOUT, **{k: v for k, v in (detect or {}).items() if k in agents.DETECT_LAYOUT}}
    tell = (detect or {}).get("tell_instead") or r"(?i)\bno\b.*\btell\b"
    key = (*sorted(d.items()), tell)
    got = _LAYOUTS.get(key)
    if got is None:
        got = _LAYOUTS[key] = {"box": re.escape(d["frame"]), "frame": d["frame"],
                               "mark": re.compile(f"^(\\s*)([{re.escape(d['menu'])}])\\s*(\\S.*)$"),
                               "other": d["other"] and re.compile(f"^(\\s*)([{re.escape(d['other'])}])\\s*(\\S.*)$"),
                               "rule": re.compile(d["rule"]), "hint": re.compile(d["hint"], re.I),
                               "tell": re.compile(tell)}
    return got


class _AnyOf:
    """The words of an adapter's blocked screens (DETECT['blocked']): a match of any one of its patterns."""

    def __init__(self, patterns):
        self.rxs = [re.compile(x) for x in patterns]

    def search(self, text):
        return next((m for m in (r.search(text) for r in self.rxs) if m), None)


_WORDS = {}

TTY_PS_RUNNER = None  # tests: gets the tty (ttys003, pts/3), returns the command lines of its processes
_TTY_PROGRAMS = {}    # (tty, the pane's process name) -> (when, adapter id or None): ps once a minute, not each pass
TTY_PROGRAMS_KEEP = 60


def _tty_programs(tty):
    """The program names that run on a terminal: for each process, the base name of its program and, for an
    interpreter, of the script it runs ('Python /Users/x/.local/bin/aider' gives Python and aider; 'node
    /opt/homebrew/bin/gemini' gives node and gemini; 'python3 -m aider' gives python3 and aider). [] when ps fails."""
    import os
    import subprocess
    dev = tty[5:] if tty.startswith("/dev/") else tty
    try:
        out = (TTY_PS_RUNNER or (lambda t: subprocess.run(["ps", "-t", t, "-o", "args="], capture_output=True,
                                                          text=True, errors="replace", timeout=5).stdout))(dev)
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    names = []
    for line in (out or "").splitlines():
        w = line.split()
        if not w:
            continue
        names.append(os.path.basename(w[0]).lstrip("-"))
        rest = w[2:] if len(w) > 2 and w[1] == "-m" else w[1:]
        if rest and not rest[0].startswith("-"):
            names.append(os.path.basename(rest[0]))
    return names


def _process_adapter(process, tty=None):
    """The adapter whose DETECT['process'] runs in a pane: tmux's name of the pane's process, else (a script CLI,
    where tmux names the interpreter: Python, node) a program or script on the pane's terminal (#5223)."""
    from . import agents
    ads = [a for a in agents.load().values() if (a.detect or {}).get("process")]
    ad = next((a for a in ads if a.detect["process"] == process), None)
    if ad is not None or not tty:
        return ad
    key, t = (tty, process), core.now().timestamp()
    seen = _TTY_PROGRAMS.get(key)
    if seen is None or t - seen[0] > TTY_PROGRAMS_KEEP:
        names = set(_tty_programs(tty))
        seen = _TTY_PROGRAMS[key] = (t, next((a.id for a in ads if a.detect["process"] in names), None))
    return agents.find(seen[1]) if seen[1] else None


def _pane_rule(conn, agent, process=None, tty=None):
    """How maxpm serve reads the pane of `agent` (#5183): (rx, layout, adapter). rx: the words of a blocked screen,
    or None (no watch: prompt_pattern is empty). With the default prompt_pattern, each pane is read with the words
    of its agent's adapter (DETECT['blocked']); another value of the setting is read for every pane. The adapter is
    the one of the agent's CLI (core.state_adapter), else the one whose DETECT['process'] runs in the pane
    (process, tty: _process_adapter), else None: the setting's words and DETECT_LAYOUT."""
    ad = core.state_adapter(conn, agent) if agent else None
    if ad is None and process:
        ad = _process_adapter(process, tty)
    pattern = core.setting(conn, "prompt_pattern").strip()
    detect = ad.detect if ad is not None else None
    if not pattern:
        return None, _layout(detect), ad
    if detect and pattern == core.DEFAULT_SETTINGS["prompt_pattern"]:
        words = tuple(detect["blocked"])
        rx = _WORDS.get(words) or _WORDS.setdefault(words, _AnyOf(words))
    else:
        rx = re.compile(pattern)
    return rx, _layout(detect), ad


def _screen_state(tail, detect):
    """working or idle, from a pane's last lines and an adapter's DETECT, or None when the screen says neither (a
    blocked screen is _prompt_found). Working first: the input box shows while the agent runs a turn."""
    if any(re.search(detect["working"], x) for x in tail):
        return "working"
    if any(re.search(detect["idle"], x) for x in tail):
        return "idle"
    return None


def _prompt_parts(tail, layout=None):
    """What a person needs to answer a prompt, read from a pane's last lines (_prompt_tail): {"question": the
    sentence that asks (up to its question mark), or None; "options": [(text, selected)]: a menu, with the line
    the marker (❯ or ›) is on selected; "hint": the line that names keys (Enter to confirm · Esc to cancel)}.
    A menu is the marked line and the lines next to it whose text starts in the same column (numbered or not:
    Claude Code's trust question lists "No, exit" and "Yes, I trust this folder" with no numbers). "frame_end": the
    line after the box the menu is in (the menu's last line + 1 when no frame character starts its line): the lines
    under the menu inside its box belong to the prompt (Hermes puts the reason of a command approval there, #5213)."""
    lay = layout or _layout()
    lines = [re.sub(f"^\\s*[{lay['box']}]", "", x).rstrip(f" {lay['frame']}") for x in tail]
    hint = next((m.group(0) for m in map(lay["hint"].search, lines) if m), None)
    mark = lay["mark"]
    # The last marked line: the lines above a prompt can show what the person typed before ("❯ run the tests"),
    # and with the first one river found no question in a real Claude Code prompt (#4280).
    at = next((n for n in range(len(lines) - 1, -1, -1) if mark.match(lines[n])), None)
    options, top = [], len(lines)
    if at is not None:
        m = mark.match(lines[at])
        col, mcol, other = len(lines[at]) - len(m.group(3)), len(m.group(1)), lay["other"]

        def marked(x):
            """The text of an option that the layout's other mark marks at the selected mark's column, or None."""
            o = other and other.match(x)
            return o.group(3).strip() if o and len(o.group(1)) == mcol else None

        def option(x):
            return marked(x) is not None or (len(x) - len(x.lstrip()) == col and not lay["hint"].search(x))
        top = at
        while top > 0 and option(lines[top - 1]):
            top -= 1
        end = at + 1
        while end < len(lines) and option(lines[end]):
            end += 1
        options = [(m.group(3).strip() if n == at else marked(lines[n]) or lines[n].strip(), n == at)
                   for n in range(top, end)]
        framed = re.compile(f"^\\s*[{lay['box']}]")
        frame_end = end
        if framed.match(tail[at]):
            while frame_end < len(tail) and framed.match(tail[frame_end]):
                frame_end += 1
    question, qat = None, None
    for n in range(top - 1, -1, -1):
        if "?" in lines[n]:
            question, qat = lines[n][:lines[n].rindex("?") + 1].strip(), n
            break
    return {"question": question, "options": options, "hint": hint, "start": top if qat is None else qat,
            "top": top, "end": end if options else None, "frame_end": frame_end if options else None, "lines": lines,
            **_prompt_command(lines[:qat] if qat is not None else [], lay)}


def _prompt_found(tail, rx, layout=None):
    """The parts of the prompt (_prompt_parts) that a pane's last lines show, or None (#4943). A prompt is the last
    thing on the screen: below its menu and the rest of the menu's box (or, with no menu, below the last line that
    matches prompt_pattern) come only a key hint, a frame, or a rule. Claude Code and Codex hide the input box and the spinner while they ask.
    Before, a match anywhere in the last lines was a prompt: a working session whose reply ended in "Do you want
    to continue with ...?" waited at a prompt, and the person's queued message (marked ❯) and the tool call lines
    under it were its options; an answer would have typed a choice into the working session. Also no prompt: a
    marked line directly under a rule (the input box, with the person's text in it), and pattern words only above
    the prompt's question or menu (the reply text)."""
    if not any(rx.search(x) for x in tail):
        return None
    lay = layout or _layout()
    parts = _prompt_parts(tail, lay)
    lines, top, end = parts["lines"], parts["top"], parts["end"]
    if end is None:
        end = under = max(n for n, x in enumerate(tail) if rx.search(x)) + 1
    elif top > 0 and lay["rule"].match(lines[top - 1]):
        return None  # the input box: a rule, then the marked line with what the person typed
    else:
        under = parts["frame_end"]  # the lines in the menu's box under it are the prompt's (#5213)
    if not all(_footer(x, lay) for x in lines[under:]):
        return None  # more under it: the spinner, the input box, the status line, or the next tool call
    if not any(rx.search(x) for x in tail[min(parts["start"], end - 1):]):
        return None
    return parts


def _footer(line, layout=None):
    """A line that may come under a prompt's menu: empty after its frame, a rule, or a key hint."""
    lay = layout or _layout()
    return not line.strip() or bool(lay["rule"].match(line) or lay["hint"].search(line))


def _prompt_command(lines, layout=None):
    """What a permission prompt asks to run, from the lines above its question: {"tool": the header ("Bash
    command (unsandboxed)"), "command": the command's first line, "why": a line that says why it asks (auto
    mode's "held this command for the server-side classifier to review")}, each None when the lines lack it.
    Claude Code 2.1 puts the command between two dashed rules under the header; before, on the line under it."""
    rule = (layout or _layout())["rule"]
    why = next((x.strip() for x in reversed(lines) if re.search(r"classifier|held this command", x, re.I)), None)
    head = next((n for n in range(len(lines) - 1, -1, -1)
                 if re.match(r"^\s*(\w+ )?(command|file|edit|fetch|tool)\b", lines[n].strip(), re.I)
                 and len(lines[n].strip()) < 60), None)
    if head is None:
        return {"tool": None, "command": None, "why": why}
    below = lines[head + 1:]
    rules = [n for n, x in enumerate(below) if rule.match(x)]
    body = below[rules[0] + 1:rules[1]] if len(rules) >= 2 else [x for x in below if not rule.match(x)]
    body = [x.strip() for x in body if x.strip() and x.strip() != why and not x.strip().startswith("Tip:")]
    cmd = body[0] if body else None
    if cmd and len(body) > 1 and len(rules) >= 2:
        cmd += " …"  # a command of more lines: the first one names it
    return {"tool": lines[head].strip(), "command": cmd and cmd[:200], "why": why}


def _keys_for(n, at):
    """The keys that choose option n of a menu when option at is selected."""
    d = n - at
    if not d:
        return "Enter"
    key = "Down" if d > 0 else "Up"
    return f"{key}, then Enter" if abs(d) == 1 else f"{key} {abs(d)} times, then Enter"


PROMPT_ANSWER = "Answer it in its session on the Sessions tab of MaximizePM"


def prompt_alert(agent, target, tail, pane=None, item=None, layout=None):
    """The alert text for an agent that waits on a prompt: core.PROMPT_NOTE, the agent, its tmux window and pane,
    the item it holds, the command the prompt asks to run and why auto mode asks, the question, the options with
    the selected one, the keys that choose each, and where to answer (#2713, #4280). Before, the alert gave only
    the first line that matched prompt_pattern ("Enter to confirm · Esc to cancel"), and a person could do
    nothing with it. item: {"id", "title"} or None. Returns (text, question, sig): question is None when the
    lines hold no question; sig is the prompt's own lines, which stay the same while it waits (the lines above
    it can change: a spinner, a timer, the dot that blinks). layout: the agent's (_pane_rule)."""
    parts = _prompt_parts(tail, layout)
    q = parts["question"]
    where = f"{core.PROMPT_NOTE}: {agent} in tmux {target}" + (f" (pane {pane})" if pane else "")
    if item:
        where += f", on #{item['id']} {item['title'][:80]}"
    if q is None:
        last = " | ".join(x.strip() for x in tail[-3:])
        return f"{where}, with no question river can read. Its last lines: {last[:300]}", None, "\n".join(tail[-3:])
    text = where
    if parts["command"]:
        text += f". It asks to run ({parts['tool']}): {parts['command']}"
    if parts["why"]:
        text += f". {parts['why'][:160].rstrip('.')}"
    text += f". Question: {q[:240]}"
    opts = parts["options"]
    if opts:  # each option with the keys that choose it, from the selected one
        at = next(n for n, (_, sel) in enumerate(opts) if sel)
        text += " Keys: " + "; ".join(f"{_keys_for(n, at)} = {o}{' (selected now)' if sel else ''}"
                                      for n, (o, sel) in enumerate(opts))
        if parts["hint"] and "esc" in parts["hint"].lower():
            text += "; Esc = cancel"
    elif parts["hint"]:
        text += f" Keys: {parts['hint']}"
    return f"{text}. {PROMPT_ANSWER}.", q, "\n".join(tail[max(0, parts["start"] - 8):])


def _pane_session_url(pane):
    """The Remote Control link that Claude Code printed in a pane ("Continue here, on your phone, or at
    https://claude.ai/code/session_..."), or None: the link of a session that ran no river command yet, or
    one whose commands do not see CLAUDE_CODE_BRIDGE_SESSION_ID. The last one on the screen and its history."""
    import re
    try:
        text = _tmux("capture-pane", "-p", "-J", "-S", "-5000", "-t", pane, check=False) or ""
    except RiverError:
        return None
    found = re.findall(r"https://claude\.ai/code/session_[A-Za-z0-9]+", _plain(text))
    return found[-1] if found else None


def watch_prompts(conn):
    """One look at every agent's tmux pane (the notify loop of maxpm serve, every notify_interval): when one of
    the last lines matches the setting prompt_pattern and those lines stay the same for prompt_wait, each
    person gets an alert from that agent (prompt_alert: its window, the question, the options and their keys),
    which opens a needs-you event; the page shows the agent's Terminal for it. Lines with no question that river
    can read go to the active manager instead, which looks at the screen (the person, when no manager is active).
    An agent with a stop request gets no alert: nobody needs its answer. When the prompt is gone (answered, or
    the agent ended), river marks the alert read, which closes the event. A prompt that stays on the screen
    alerts once. Each pane is read with its agent's adapter (_pane_rule). The same look is the state source of an
    agent whose adapter's STATE['state'] is 'detect' (Codex): blocked, working or idle (_screen_state) goes to
    core.report as maxpm report does, when it changed (#5183). Returns {"waiting": {agent: question}, "told":
    [agents], "closed": [message ids]}, and "reported": {agent: state} when it recorded a state."""
    pattern = core.setting(conn, "prompt_pattern").strip()
    waiting, states = {}, {}
    if pattern:
        stopped = {r["name"] for r in conn.execute("SELECT name FROM agents WHERE stop_at IS NOT NULL")}
        for agent, pane in sorted(agent_terminals(conn).items()):
            if agent in stopped:
                continue  # a stop request: an answer would only let it read the stop and end (#2713, alert #7392)
            try:
                screen = _pane_screen(agent, pane)
            except RiverError:
                continue  # the pane closed since the list
            if screen["ended"]:
                continue  # a shell: what the agent printed stays, and nobody waits there
            rx, layout, ad = _pane_rule(conn, agent, screen.get("running"), screen.get("tty"))
            tail = _prompt_tail(_plain(screen["text"]))
            parts = _prompt_found(tail, rx, layout)
            if parts:
                held = conn.execute("SELECT id, title FROM items WHERE assignee=? AND status IN ('in_progress','held') "
                                    "ORDER BY id", (agent,)).fetchone()
                body, q, sig = prompt_alert(agent, screen["target"], tail, pane, held and dict(held), layout)
                waiting[agent] = (body, sig, q, held and held["id"], pane)
            if ad is not None and (ad.state or {}).get("state") == "detect" and ad.detect:
                state = "blocked" if parts else _screen_state(tail, ad.detect)
                if state:
                    states[agent] = (state, parts and parts["question"])
    reported = {"reported": got} if (got := _pane_reports(conn, states)) else {}
    t, wait = core.now(), core.parse_duration(core.setting(conn, "prompt_wait"))
    for agent in [a for a in PROMPTS if a not in waiting]:
        del PROMPTS[agent]
    told = []
    alerts = [dict(r) for r in conn.execute(
        "SELECT id, from_agent, body FROM messages WHERE kind='alert' AND read_at IS NULL AND body LIKE ?",
        (core.PROMPT_NOTE + "%",))]
    closed = [m["id"] for m in alerts if waiting.get(m["from_agent"], ("",))[0] != m["body"]]
    if not waiting and not closed:
        return {"waiting": {}, "told": [], "closed": [], **reported}  # the usual pass: nothing to write
    with core.tx(conn):
        core._mark_read(conn, closed)
        people = [r["name"] for r in conn.execute("SELECT name FROM agents WHERE kind='human' ORDER BY name")]
        manager = core.active_manager(conn)
        for agent, (body, sig, q, held, pane) in waiting.items():
            st = PROMPTS.get(agent)
            if not st or st["sig"] != sig:
                st = PROMPTS[agent] = {"sig": sig, "since": t, "told": st["told"] if st else None}
            if any(m["from_agent"] == agent and m["id"] not in closed for m in alerts):
                st["told"] = body  # also after maxpm serve started again: the alert is there
            if st["told"] == body or t - st["since"] < wait:
                continue
            # The notice opens the agent's web session (notify.compose: its session_url). A session that ran no
            # river command with Remote Control on has none yet: the link it printed in its pane (#4280).
            if not conn.execute("SELECT session_url FROM agents WHERE name=?", (agent,)).fetchone()["session_url"]:
                link = _pane_session_url(pane)
                if link:
                    conn.execute("UPDATE agents SET session_url=? WHERE name=?", (link, agent))
            to = [manager] if q is None and manager and manager != agent else people
            for person in to:
                core._send(conn, "alert", agent, body, to=person, item_id=held)
            if to:  # with no person in the queue yet, the first one who registers gets it
                st["told"] = body
                told.append(agent)
        if told or closed:
            core.sync_needs_you(conn)
    return {"waiting": {a: w[2] or w[0] for a, w in waiting.items()}, "told": told, "closed": closed, **reported}


def _pane_reports(conn, states):
    """The generic pane watcher's last step (#5183): for each agent whose pane gave a state ({agent: (state,
    message)}), the same record as maxpm report --state, when the state changed since the last report (a blocked
    screen also when its message changed). Returns {agent: state} of what it recorded."""
    out = {}
    for agent, (state, message) in sorted(states.items()):
        r = conn.execute("SELECT activity_event, activity_prompt FROM agents WHERE name=? AND kind='ai'",
                         (agent,)).fetchone()
        if not r:
            continue
        said = json.dumps({"message": (message or "")[:400]}) if state == "blocked" else None
        if r["activity_event"] == f"report:{state}" and (state != "blocked" or r["activity_prompt"] == said):
            continue
        try:
            core.report(conn, agent, state, message)
        except RiverError:
            continue
        out[agent] = state
    return out


# An agent idle at its prompt runs no river command, and river never types into its pane: a stale session would
# continue a large context. River starts fresh sessions for its work, and ends it (fresh_sessions, idle_end).
# IDLE remembers, for each agent river looks at, the screen of its pane and since when that screen has not changed.
IDLE = {}


def _idle_panes(conn, names, after):
    """{agent: pane} for the agents among names whose tmux pane shows the agent CLI (not a shell) with no prompt
    (prompt_pattern: a person answers that) and the same screen for `after`. One look per pass of the loop."""
    import re
    for agent in [a for a in IDLE if a not in names]:
        del IDLE[agent]
    if not names:
        return {}  # the usual pass: no tmux command at all
    terminals = agent_terminals(conn)
    t, out = core.now(), {}
    for agent in sorted(names):
        pane = terminals.get(agent)
        if not pane:
            IDLE.pop(agent, None)
            continue
        try:
            screen = _pane_screen(agent, pane)
        except RiverError:
            continue  # the pane closed since the list
        text = _plain(screen["text"])
        rx, layout, _ = _pane_rule(conn, agent, screen.get("running"), screen.get("tty"))
        if screen["ended"] or (rx and _prompt_found(_prompt_tail(text), rx, layout)):
            IDLE.pop(agent, None)  # a shell, or a prompt that a person answers
            continue
        st = IDLE.get(agent)
        if not st or st["sig"] != text:
            IDLE[agent] = {"sig": text, "since": t}
        elif t - st["since"] >= after:
            out[agent] = pane
    return out


# A lease must not run out while its agent is busy (a long test run prints nothing and runs no river command).
# BUSY remembers, for each agent that holds work, the screen of its tmux pane at the last pass.
BUSY = {}


def watch_busy(conn):
    """One pass of the loop of maxpm serve (every notify_interval): the agents that hold work and are busy keep
    their leases (core.keep_busy). Busy: a command runs in the agent's session (core.busy_now), its tmux pane
    changed since the last pass, its pane shows a prompt (prompt_pattern: the agent waits on a person, and
    its work is in the folder), or a background command or subagent of its session runs (core.background_work). An agent that waits on a person's answer (core.waits_on_person) keeps its leases
    and its target with no time limit while its agent CLI runs, and the manager gets an alert for it. Returns the
    agents it kept."""
    import re
    names = {r["assignee"] for r in conn.execute(
        "SELECT DISTINCT assignee FROM items WHERE status IN ('in_progress','held') AND assignee IN "
        "(SELECT name FROM agents WHERE kind='ai')")}
    for agent in [a for a in BUSY if a not in names]:
        del BUSY[agent]
    waiting = core.waits_on_person(conn)
    if not core.parse_duration(core.setting(conn, "busy_max")).total_seconds():
        names = set()
    if not names and not waiting:
        return []  # the usual pass with no agent at work: no ps and no tmux command
    busy = core.busy_now(conn, names) if names else set()
    terminals = agent_terminals(conn) if TMUX_RUNNER is not None or _tmux_cmd() else {}
    for agent in sorted(names | set(waiting)):
        pane = terminals.get(agent)
        if not pane:
            BUSY.pop(agent, None)
            continue
        try:
            screen = _pane_screen(agent, pane)
        except RiverError:
            continue  # the pane closed since the list
        if screen["ended"]:
            BUSY.pop(agent, None)  # a shell: the agent CLI ended
            waiting.pop(agent, None)
            continue
        if agent not in names:
            continue
        text = _plain(screen["text"])
        rx, layout, _ = _pane_rule(conn, agent, screen.get("running"), screen.get("tty"))
        if (agent in BUSY and BUSY[agent] != text) or (rx and _prompt_found(_prompt_tail(text), rx, layout)):
            busy.add(agent)
        BUSY[agent] = text
    for agent in sorted(names - busy):
        if core.background_work(conn, agent):
            busy.add(agent)  # its own background command or subagent runs; the screen is quiet (#1611)
    if waiting:
        core.tell_manager_waits(conn, waiting)
    return core.keep_busy(conn, busy, waiting) if busy or waiting else []


BACKGROUND = set()  # (agent, task id) that fresh_sessions wrote into the history: once for each task


def _has_background(conn, agent, news):
    """True when a background command or subagent runs in the agent's session (core.background_work): it is
    not idle at its prompt. With news for it, the history of each item it holds says so, once for each task."""
    work = core.background_work(conn, agent)
    new = [w for w in work if (agent, w["id"]) not in BACKGROUND]
    if work and news and new:
        BACKGROUND.update((agent, w["id"]) for w in new)
        what = "; ".join(f"{w['kind']} {w['what'] or w['id']}" for w in work[:3]) + (" ..." if len(work) > 3 else "")
        with core.tx(conn):
            for r in conn.execute("SELECT id FROM items WHERE assignee=? AND status IN ('in_progress','held') ORDER BY id",
                                  (agent,)).fetchall():
                core._event(conn, r["id"], "maxpm", f"news came for {agent}, which waits for its own background work "
                            f"({what}): maxpm serve leaves the item with it; it reads the news when that work ends")
    return bool(work)


def auto_context(conn, runner=None):
    """One pass of the loop of maxpm serve (goal_context on): a goal with two or more ready agent items and no
    warm base gets a context session (MAXPM_FOCUS=context:<goal>). It reads the goal's handoff, items and files,
    records itself as the goal's base (maxpm goal base --ready), and ends; the goal's next workers start as
    forks of it. Returns the goals it started one for."""
    started = []
    for w in core.context_wanted(conn):
        p = core._project(conn, w["project"])
        try:
            label = core.role_agent(conn, "context", p["id"]) or (_agent_for(conn, w["model"])[0] if w["model"] else None)
            t = _open_focused(conn, p, f"context:{w['goal']}", runner, label, w["model"])
        except RiverError as e:
            print(f"maxpm serve: no context session for goal {w['goal']}: {e}", flush=True)
            continue
        with core.tx(conn):
            core._event(conn, None, "maxpm", f"context session started for goal {w['goal']} ({t['session_title']})")
        started.append(w["goal"])
    return started


def start_fresh(conn, item_id, why, runner=None):
    """Start a new session for one ready item, with the item's model and effort: river names it, reserves the
    item for it, and opens the agent in the project folder. The item's notes are its context."""
    it = core.annotate(conn)[item_id]
    p = core._project(conn, it["project"])
    label = core.agent_for_type(conn, it["agent"], p["id"]) if it["agent"] else _agent_for(conn, it["model"])[0]
    opt = next((o for o in core.launch_options(conn) if o["label"] == label), None)
    model = it["model"] if opt and any(x["name"] == it["model"] for x in opt["models"]) else None
    effort = it["effort"] if opt and it["effort"] in opt["efforts"] else None
    t = core.launch_target(conn, agent=label, item=item_id, model=model, effort=effort)
    _can_open_terminal(runner, f"cd {t['path']} && MAXPM_FOCUS=item:{item_id} claude go")
    return _start_for_item(conn, t, runner, "maxpm", why, by="maxpm serve")


def fresh_sessions(conn, runner=None):
    """One pass of the loop of maxpm serve (every notify_interval):
    - fresh_sessions on: start a fresh session for each item an answer came for (core.fresh_items).
    - An agent idle at its prompt (_idle_panes, idle_after) with news (core.idle_news): its work goes to fresh
      sessions (core.hand_over), and it ends.
    - An agent idle at its prompt with nothing in hand and no river command for idle_end: it ends.
    An agent in whose session a command runs (core.busy_now) is not idle, whatever its screen shows; nor is one
    that waits on a person's answer (core.waits_on_person): the person may answer in its terminal; nor one that
    waits for a background command or a subagent of its own (core.background_work): it reads its news when the
    harness wakes it.
    An agent that ends is unregistered, and the tmux pane river started for it closes; that comes before the
    fresh sessions start, so two sessions never work on one item.
    Managers, planners, experts and assistants are left alone: they wait at their prompt for a person or a watch
    command (an expert ends after expert_idle_end with no talk: core.end_quiet_experts; an assistant when its
    person ends it, #5110).
    Returns {"started": {item: session}, "ended": {agent: why}, "failed": {item: why}}."""
    out = {"started": {}, "ended": {}, "failed": {}}
    fresh = core.setting(conn, "fresh_sessions") == "on"
    after = core.parse_duration(core.setting(conn, "idle_after"))
    end = core.parse_duration(core.setting(conn, "idle_end"))

    def start(ids, why):
        for i in ids:
            try:
                out["started"][i] = start_fresh(conn, i, why, runner)["session_name"]
            except (RiverError, StopIteration) as e:
                out["failed"][i] = str(e)
                with core.tx(conn):
                    core._event(conn, i, "maxpm", f"no fresh session: {e}; the item waits in the queue")
    if fresh:
        start(core.fresh_items(conn), "an answer came for it")
    if not after.total_seconds():
        return out
    t, cands = core.now(), {}
    for a in conn.execute("SELECT name, last_seen, registered_at, role FROM agents WHERE kind='ai' AND stop_at IS NULL "
                          "AND COALESCE(role, '') NOT IN ('manager', 'planner', 'expert', 'assistant')").fetchall():
        idle = t - core.parse_iso(a["last_seen"])
        if a["last_seen"] == a["registered_at"] or idle < after:
            continue  # it never connected (the manager finds that), or it ran a river command just now
        news = core.idle_news(conn, a["name"], a["last_seen"]) if fresh else None
        holds = conn.execute("SELECT 1 FROM items WHERE assignee=? AND status IN ('in_progress','held')",
                             (a["name"],)).fetchone()
        if news or (end.total_seconds() and idle >= end and not holds):
            cands[a["name"]] = news
    for agent in core.busy_now(conn, set(cands)) if cands else ():
        del cands[agent]  # a long command with a quiet screen: the agent works
    for agent in core.waits_on_person(conn, set(cands)) if cands else ():
        del cands[agent]  # it asked a person and waits for the answer, maybe in its terminal: not idle
    for agent in [a for a in cands if _has_background(conn, a, cands[a])]:
        del cands[agent]  # it waits for its own background command or subagent: the harness wakes it (#1611)
    for agent, pane in _idle_panes(conn, cands, after).items():
        news, why = cands[agent], f"idle at its prompt with nothing in hand for {core.setting(conn, 'idle_end')}"
        ids = []
        if news:
            ids = core.hand_over(conn, agent, news)
            if ids:
                why = "idle at its prompt; fresh sessions took its work"
            elif not end.total_seconds() or t - core.parse_iso(conn.execute(
                    "SELECT last_seen FROM agents WHERE name=?", (agent,)).fetchone()["last_seen"]) < end:
                continue  # only messages and no work: it ends after idle_end, and their senders hear it
        try:
            ended = core.end_idle(conn, agent, why)
        except RiverError:
            ended = None
        if ended:
            IDLE.pop(agent, None)
            out["ended"][agent] = why
            if any(p["pane"] == pane and p["agent"] == agent for p in _tmux_panes(everywhere=True)):
                _tmux("kill-pane", "-t", pane, check=False)  # a pane river started for it; a person's own pane stays
        ann = core.annotate(conn)
        start([i for i in ids if ann[i]["ready"]], f"{agent} was idle at its prompt")
    return out


# A release needed the manager twice: to start a reviewer, and to wake the target owner after the review passed.
# auto_release does both in each pass of maxpm serve. It writes each start (and each failure) into the item's
# history with RELEASE_NOTE first, and starts nothing for the same item again within RELEASE_GAP of the last one:
# a session needs a minute or two to connect, and a failure must not repeat every pass.
RELEASE_NOTE = "release:"
RELEASE_GAP = 600


def _release_recent(conn, item_id):
    r = conn.execute("SELECT at FROM events WHERE item_id=? AND change LIKE ? AND change NOT LIKE ? ORDER BY id DESC "
                     "LIMIT 1", (item_id, RELEASE_NOTE + "%", RELEASE_NOTE + " alerted%")).fetchone()
    return r is not None and (core.now() - core.parse_iso(r["at"])).total_seconds() < RELEASE_GAP


def _release_event(conn, item_id, text):
    with core.tx(conn):
        core._event(conn, item_id, "maxpm", f"{RELEASE_NOTE} {text}")


def auto_operational(conn, runner=None):
    """One pass of the loop of maxpm serve (#4973): first the cleanup trigger (core.cleanup_trigger) adds an
    operational task for a person whose list holds old items. Then each ready operational task that nobody holds
    or has reserved goes to a session that waits for work in its project and held no task before, else to a new
    session (start_fresh): an operational task starts in a new session. No session starts while max_sessions agent
    sessions are live. Returns {"added": [ids], "pushed": {item: agent}, "started": {item: session}, "failed": {}}."""
    out = {"added": core.cleanup_trigger(conn), "pushed": {}, "started": {}, "failed": {}}
    rows = conn.execute("SELECT id FROM items WHERE kind='operational' AND status='open' AND assignee IS NULL "
                        "AND (reserved_for IS NULL OR reserved_until < ?) ORDER BY id", (core.iso(core.now()),)).fetchall()
    if not rows:
        return out
    cap = int(core.setting(conn, "max_sessions") or 0)
    ann = core.annotate(conn)
    for r in rows:
        a = ann.get(r["id"])
        if (a is None or not a["ready"] or a["reserved_for"] or a["project_archived"] or a["backlog"]
                or a["doer"] == "human" or _release_recent(conn, a["id"])):
            continue
        waiting = core.waiting_agent_for(conn, a["project"], a["id"])
        if waiting:
            try:
                core.push(conn, a["id"], waiting, "from maxpm serve: an operational task, and this session held no "
                          "task before", "maxpm")
            except RiverError as e:
                out["failed"][a["id"]] = str(e)
                continue
            _release_event(conn, a["id"], f"pushed to {waiting}, who waits for work")
            out["pushed"][a["id"]] = waiting
            continue
        if cap and core.live_sessions(conn) >= cap:
            out["failed"][a["id"]] = f"max_sessions {cap}"
            continue
        try:
            name = start_fresh(conn, a["id"], "operational task ready", runner)["session_name"]
        except (RiverError, StopIteration) as e:
            _release_event(conn, a["id"], f"no session for the operational task: {e}")
            out["failed"][a["id"]] = str(e)
            continue
        _release_event(conn, a["id"], f"started session {name} for the operational task")
        out["started"][a["id"]] = name
        ann = core.annotate(conn)
    return out


def start_deployer(conn, target, dep_id, why, runner=None):
    """Start a deployer session for a target: river names it, gives it the target (the old owner is told), and
    opens the agent in a folder of the target's projects with MAXPM_FOCUS=deploy:<target>, with the deploy
    item's model and effort. Its maxpm go claims the deploy item when it is ready, else it waits for it."""
    import secrets
    it = core.annotate(conn)[dep_id]
    p = _target_folder(conn, target)
    label = core.agent_for_type(conn, it["agent"], p["id"]) if it["agent"] else _agent_for(conn, it["model"])[0]
    opt = next((o for o in core.launch_options(conn) if o["label"] == label), None)
    model = it["model"] if opt and any(x["name"] == it["model"] for x in opt["models"]) else None
    effort = it["effort"] if opt and it["effort"] in opt["efforts"] else None
    focus = f"deploy:{target}"
    _can_open_terminal(runner, f"cd {p['path']}, set MAXPM_FOCUS={focus}, then claude go")
    # Each deployer gets a session name of its own (claude --name, --remote-control): the one it replaces may still
    # run, and #1010 saw both starts beside a live session of the same name end before they connected.
    tag = secrets.token_hex(2)
    title = f"{core._short_title(core.focus_title(conn, focus), 43)} {tag}"
    t = {"project": p["name"], "path": p["path"], "focus": focus, "session_title": title,
         **core._launch_agent_cmd(conn, p["id"], label, model, effort, None, title)}
    name = f"deploy-{target}-{tag}"
    core.register(conn, name, note=f"{core.STARTED_NOTE} #{dep_id}")
    with core.tx(conn):
        core.target_handoff(conn, target, name, why)
    _open_terminal(t, {"MAXPM_AGENT": name, "MAXPM_FOCUS": focus, **t["env"]}, runner)
    return {**t, "session_name": name}


def auto_release(conn, runner=None):
    """One pass of the loop of maxpm serve (every notify_interval): releases move with no manager.
    - A ready release review that nobody holds or has reserved (auto_review on): a session that waits for work in
      a project of the release and held no task before (used_session, release_authors) gets it pushed; else a new
      session. A review always starts in a session with a clean context (#4968).
    - A ready deploy item nobody holds, by its target's deployer mode (core.DEPLOYER_MODES): the target owner gets
      one alert when it can take it (core.owner_can_deploy); with no owner, or one that cannot, a new deployer
      session gets the target. With standing, that session starts while the release still waits on its review.
    - A ready deploy item of a target with a deploy hook (maxpm target hook <t> deploy): serve runs the hook, its
      release command, in a thread (core.deploy_hook_start); no session takes the item (#4971).
    No session starts while max_sessions agent sessions are live. Returns {"pushed": {item: agent},
    "started": {item: session}, "alerted": {item: owner}, "failed": {item: why}, "deploying": {item: hook run},
    "lost": [deploy items whose hook run never ended]}."""
    out = {"pushed": {}, "started": {}, "alerted": {}, "failed": {}, "deploying": {}, "lost": []}
    ann = None
    cap = int(core.setting(conn, "max_sessions") or 0)
    out["lost"] = core.deploy_hook_lost(conn)  # a deploy hook that never ended: its release starts again
    out["asked"] = core.release_yes_pass(conn)  # release_approval person: one yes for each ready release (#5003)

    def full(item_id):
        if cap and core.live_sessions(conn) >= cap:
            if not _release_recent(conn, item_id):
                _release_event(conn, item_id, f"no session started: {cap} agent sessions are live (max_sessions)")
            out["failed"][item_id] = f"max_sessions {cap}"
            return True
        return False
    # Candidates by SQL first (nothing open before them), so the usual pass builds no graph (core.annotate).
    clear = (f"NOT EXISTS (SELECT 1 FROM deps d JOIN items b ON b.id=d.blocked_by WHERE d.item_id=i.id "
             f"AND d.kind<>'conflicts' AND b.status IN {core.OPEN_STATES})")
    reviews = conn.execute(f"SELECT id FROM items i WHERE kind='review' AND status='open' AND assignee IS NULL "
                           f"AND (reserved_for IS NULL OR reserved_until < ?) AND {clear} ORDER BY id",
                           (core.iso(core.now()),)).fetchall()
    if reviews:
        with core.tx(conn):
            core._sweep(conn)  # a push that ran out ends here, as in every river command
    for r in reviews:
        ann = ann or core.annotate(conn)
        a = ann.get(r["id"])
        if (a is None or not a["ready"] or a["reserved_for"] or a["project_archived"]
                or core.setting(conn, "auto_review", item_id=a["id"]) != "on" or _release_recent(conn, a["id"])):
            continue
        joined = core.release_join_commits(conn, a["id"])  # what the release's commits name (release_commits)
        if joined:  # not a "release:" event: that would hold the next pass back (_release_recent)
            with core.tx(conn):
                core._event(conn, a["id"], "maxpm", "commits of the release name "
                            + ", ".join(f"#{i}" for i in joined) + "; they join the release (release_commits)")
        projects = [a["project"]] + [ann[b]["project"] for b in a["waits_on"] if b in ann]
        waiting = next((w for w in (core.waiting_agent_for(conn, p, a["id"]) for p in dict.fromkeys(projects)) if w),
                       None)
        if waiting:
            try:
                core.push(conn, a["id"], waiting, f"from maxpm serve: release {a['target']} waits on this review, "
                          f"and this session held no task before", "maxpm")
            except RiverError as e:  # taken or reserved since the look above
                out["failed"][a["id"]] = str(e)
                continue
            _release_event(conn, a["id"], f"pushed to {waiting}, who waits for work")
            out["pushed"][a["id"]] = waiting
            continue
        if full(a["id"]):
            continue
        try:
            name = start_fresh(conn, a["id"], "release review ready", runner)["session_name"]
        except (RiverError, StopIteration) as e:
            _release_event(conn, a["id"], f"no reviewer session: {e}")
            out["failed"][a["id"]] = str(e)
            continue
        _release_event(conn, a["id"], f"started reviewer session {name}")
        out["started"][a["id"]] = name
        ann = None
    deploys = conn.execute("SELECT i.id, i.status, i.target, t.owner, t.deployer FROM items i JOIN targets t ON t.name=i.target "
                           f"WHERE i.kind='deploy' AND i.status IN {core.OPEN_STATES} ORDER BY i.id").fetchall()
    seen = set()
    hooked = core.hooked_targets(conn)
    for d in deploys:
        # A deploy hook replaces the deployer: it runs whatever the deployer mode is (#5066: release #4912 waited
        # 7 minutes after its review because toolscaled-prod had a hook and deployer off). Without a hook, off
        # means maxpm serve starts nothing.
        if d["deployer"] == "off" and d["target"] not in hooked:
            continue
        if d["target"] in seen:
            continue  # one deploy item at a time for each target: the first open one
        seen.add(d["target"])
        if d["target"] in hooked:
            # The target's deploy hook is its release command: serve runs it when the deploy item is ready, and
            # starts no deployer session and alerts no owner (#4971). A failure starts the release again.
            if d["status"] != "open" or not conn.execute(f"SELECT 1 FROM items i WHERE id=? AND {clear}",
                                                         (d["id"],)).fetchone():
                continue
            ann = ann or core.annotate(conn)
            a = ann.get(d["id"])
            if a is None or a["project_archived"] or not a["ready"]:
                continue
            token = f"deploy:{d['id']}:{core.hook_token()}"
            run = core.deploy_hook_start(conn, d["id"], token)
            if run is None:
                continue
            hooks_soon(conn, token)
            out["deploying"][d["id"]] = run
            ann = None
            continue
        if d["status"] != "open" or (d["deployer"] != "standing" and not conn.execute(
                f"SELECT 1 FROM items i WHERE id=? AND {clear}", (d["id"],)).fetchone()):
            continue  # it runs already, or it waits on open work
        ann = ann or core.annotate(conn)
        a = ann.get(d["id"])
        if a is None or a["project_archived"] or not (a["ready"] or d["deployer"] == "standing"):
            continue
        if not a["ready"] and core.cadence_holds(conn, d["id"]):
            continue  # the release waits for the target's cadence: a standing deployer starts when it can start
        ok, why = core.owner_can_deploy(conn, d["owner"]) if d["owner"] else (False, "the target has no owner")
        if ok:
            note = f"{RELEASE_NOTE} alerted {d['owner']}"
            if a["ready"] and not conn.execute("SELECT 1 FROM events WHERE item_id=? AND change LIKE ?",
                                               (d["id"], note + ":%")).fetchone():
                with core.tx(conn):
                    core._send(conn, "alert", "maxpm", f"deploy #{d['id']} for {d['target']} is ready: take it now: "
                               f"maxpm --as {d['owner']} go --role deployer", to=d["owner"], item_id=d["id"])
                    core._event(conn, d["id"], "maxpm", f"{note}: the deploy is ready")
                out["alerted"][d["id"]] = d["owner"]
            continue
        if _release_recent(conn, d["id"]) or full(d["id"]):
            continue
        reason = (f"deploy #{d['id']} is ready" if a["ready"] else "a standing deployer waits for the review") + f"; {why}"
        try:
            name = start_deployer(conn, d["target"], d["id"], reason, runner)["session_name"]
        except RiverError as e:
            _release_event(conn, d["id"], f"no deployer session: {e}")
            out["failed"][d["id"]] = str(e)
            continue
        _release_event(conn, d["id"], f"started deployer session {name} ({reason})")
        out["started"][d["id"]] = name
    return out


def terminal_keys(conn, agent, keys, who=None):
    """Type into an agent's tmux pane from the page: keys is a list of {"text": "..."} (typed as it is) and
    {"key": "Enter"} (a key in TERMINAL_KEYS). Only a person does this: an agent does not answer another
    agent's prompts through river."""
    pane = _typing_pane(conn, agent, who)
    return {"agent": agent, "pane": pane, "sent": send_keys(pane, keys)}


def send_keys(pane, keys):
    """Type keys into a tmux pane: a list of {"text": "..."} (typed as it is) and {"key": "Enter"} (a key in
    TERMINAL_KEYS). All are checked before the first one goes. Returns how many went."""
    if not isinstance(keys, list) or len(keys) > 200:
        raise RiverError("keys is a list of at most 200 entries")
    for k in keys:
        if isinstance(k, dict) and isinstance(k.get("text"), str) and 0 < len(k["text"]) <= 4000:
            continue
        if isinstance(k, dict) and k.get("key") in TERMINAL_KEYS:
            continue
        raise RiverError(f"not a key MaximizePM sends: {k!r}")
    for k in keys:
        if "text" in k:
            _tmux("send-keys", "-t", pane, "-l", "--", k["text"])
        else:
            _tmux("send-keys", "-t", pane, k["key"])
    return len(keys)


def _typing_pane(conn, agent, who):
    """The tmux pane a person may type into for this agent; RiverError for an agent, or with no pane."""
    row = conn.execute("SELECT kind FROM agents WHERE name=?", (who,)).fetchone() if who else None
    if row and row["kind"] != "human":
        raise RiverError("only a person types into an agent's terminal")
    pane = agent_terminals(conn).get(agent)
    if not pane:
        raise RiverError(f"{agent} does not run in a tmux pane on this computer")
    return pane


# Files for a session (#4629): images keep their own paste (the CLIs attach them); other files go as readable
# paths in the prompt. Keep a safe version of the filename so the agent can identify the file and its format.
UPLOAD_MAX = 10 * 1024 * 1024
UPLOADS_PER_SEND = 5
UPLOAD_DAYS = 7  # auto_tidy removes older uploads
UPLOAD_KINDS = ((b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"), (b"GIF87a", ".gif"), (b"GIF89a", ".gif"))
UPLOAD_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}


def uploads_dir():
    return Path(core.river_dir()) / "uploads"


def upload_image_ext(raw):
    ext = next((e for magic, e in UPLOAD_KINDS if raw.startswith(magic)), None)
    return ext or (".webp" if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP" else None)


def save_upload(agent, data, name=None):
    """Save any file (base64), at most UPLOAD_MAX. Old image clients may omit the name."""
    import base64
    import binascii
    import secrets
    if not isinstance(data, str):
        raise RiverError("a file comes as base64")
    if len(data) > ((UPLOAD_MAX + 2) // 3) * 4:
        raise RiverError(f"a file is at most {UPLOAD_MAX // (1024 * 1024)} MB")
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError, TypeError):
        raise RiverError("a file comes as base64")
    if len(raw) > UPLOAD_MAX:
        raise RiverError(f"a file is at most {UPLOAD_MAX // (1024 * 1024)} MB")
    ext = upload_image_ext(raw)
    if name is not None and not isinstance(name, str):
        raise RiverError("a file name must be text")
    # No directory components, whitespace, terminal controls or shell syntax in a pasted path.
    name = re.sub(r"[^a-zA-Z0-9_.-]", "_", (name or "").replace("\\", "/").rsplit("/", 1)[-1])[:120].strip(".-")
    if not name:
        name = "image" + ext if ext else "file"
    if ext and Path(name).suffix.lower() not in {*UPLOAD_TYPES, ".jpeg"}:
        name += ext
    elif ext:
        name = str(Path(name).with_suffix(ext))
    elif Path(name).suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        name += ".bin"  # a file merely named .png must not trigger a CLI's image attachment handling
    folder = uploads_dir() / agent
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / f"{core.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}-{name}"
    f.write_bytes(raw)
    return str(f)


def upload_file(path):
    """GET /api/upload/<agent>/<file>: saved bytes, safe type. Nothing outside that agent's upload folder."""
    from urllib.parse import unquote
    parts = unquote(path).split("/")
    if len(parts) != 2 or any(not p or p.startswith(".") or "\\" in p for p in parts):
        return None
    root = uploads_dir().resolve()
    folder = root / parts[0]
    f = folder / parts[1]
    if folder.is_symlink() or f.is_symlink() or f.resolve().parent != folder or not f.is_file():
        return None
    raw = f.read_bytes()
    return raw, UPLOAD_TYPES.get(upload_image_ext(raw), "application/octet-stream")


def tidy_uploads(days=UPLOAD_DAYS):
    """Remove uploads older than days, and empty agent folders. Returns how many files went."""
    root, gone = uploads_dir(), 0
    if not root.is_dir():
        return 0
    cut = time.time() - days * 86400
    for d in root.iterdir():
        if not d.is_dir():
            continue
        for f in d.iterdir():
            try:
                if f.is_file() and f.stat().st_mtime < cut:
                    f.unlink()
                    gone += 1
            except OSError:
                pass
        try:
            d.rmdir()  # only when empty
        except OSError:
            pass
    return gone


def session_upload(conn, agent, data, name=None, who=None):
    """A file attached in the message box (#5112): saved at once, so the session's draft keeps its id, not its bytes,
    through a switch of sessions and a reload of the page. Send names it by the id."""
    _typing_pane(conn, agent, who)
    path = Path(save_upload(agent, data, name))
    return {"id": path.name, "url": f"api/upload/{agent}/{path.name}"}


def session_upload_drop(conn, agent, fid, who=None):
    """The person took a saved file out of the message box before Send (#5112): it goes."""
    path = Path(_uploaded(agent, fid))
    path.unlink(missing_ok=True)
    return {"dropped": path.name}


def _uploaded(agent, fid):
    """The path of a file session_upload saved for this agent, from its id; a refusal when it is not there."""
    if not isinstance(fid, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", fid) or fid.startswith("."):
        raise RiverError("a file id names a saved file of this session")
    path = uploads_dir() / agent / fid
    if not path.is_file():
        raise RiverError(f"the attached file {fid.split('-', 3)[-1]} is gone (uploads go after {UPLOAD_DAYS} days); "
                         f"attach it again")
    return str(path)


def session_send(conn, agent, text, who=None, images=None, files=None):
    """The composer of the Sessions tab (#4596, #4604): a person's text, typed into the agent's tmux pane, then
    Enter; text of several lines goes as one bracketed paste. An empty message sends nothing (#4617): the ! dialog
    answers a prompt. The text comes from the person: river types nothing by itself."""
    if not isinstance(text, str) or len(text) > 4000:
        raise RiverError("text is at most 4000 characters")
    text = text.replace("\r\n", "\n").strip()
    attachments = files if files is not None else images  # accept the previous image-only page too
    if attachments is not None and (not isinstance(attachments, list) or len(attachments) > UPLOADS_PER_SEND):
        raise RiverError(f"at most {UPLOADS_PER_SEND} files go with one message")
    if attachments:
        pane = _typing_pane(conn, agent, who)
        paths, saved = [], []
        try:
            for f in attachments:
                if not isinstance(f, dict):
                    raise RiverError("a file needs its name and base64 data")
                if f.get("id") is not None:  # saved before, with the draft (#5112)
                    paths.append(_uploaded(agent, f["id"]))
                else:
                    saved.append(save_upload(agent, f.get("data"), f.get("name")))
                    paths.append(saved[-1])
        except (RiverError, OSError):
            for path in saved:
                Path(path).unlink(missing_ok=True)
            raise
        image_paths = [p for p in paths if Path(p).suffix.lower() in UPLOAD_TYPES]
        other_paths = [p for p in paths if p not in image_paths]
        # Images must be pasted alone to become [Image #1] (#4618). Non-images are paths the agent reads with
        # its file tools (both Claude Code and Codex); explicit newlines keep several paths from joining.
        prompt = ("Attached files:\n" + "\n".join(other_paths)) if other_paths else ""
        if text:
            prompt += ("\n\n" if prompt else "") + text
        for part in image_paths + ([" " + prompt] if prompt else []):
            _tmux("set-buffer", "-b", "maxpm-send", "--", part)
            _tmux("paste-buffer", "-p", "-d", "-b", "maxpm-send", "-t", pane)
            time.sleep(PROMPT_PAUSE)
        _tmux("send-keys", "-t", pane, "Enter")
        _keep_prompt(conn, agent, text, who)
        return {"agent": agent, "pane": pane, "sent": 2, "text": prompt, "images": image_paths, "files": paths}
    if not text:
        raise RiverError("the message is empty: type a message first (the ! dialog answers a prompt)")
    if "\n" not in text:
        keys = [{"text": text}, {"key": "Enter"}]
        out = dict(terminal_keys(conn, agent, keys, who), text=text)
        _keep_prompt(conn, agent, text, who)
        return out
    # Several lines: a bracketed paste, which the agent CLI takes as one text (a typed new line would end the
    # prompt early), then Enter.
    pane = _typing_pane(conn, agent, who)
    _tmux("set-buffer", "-b", "maxpm-send", "--", text)
    _tmux("paste-buffer", "-p", "-d", "-b", "maxpm-send", "-t", pane)
    _tmux("send-keys", "-t", pane, "Enter")
    _keep_prompt(conn, agent, text, who)
    return {"agent": agent, "pane": pane, "sent": 2, "text": text}


def _keep_prompt(conn, agent, text, who):
    """The box's Up arrow brings the prompt back (#4855): the person's text, without the attached files."""
    from . import sessions
    sessions.record_prompt(conn, agent, text, who)


PROMPT_PAUSE = 0.4  # after a pick that leaves a prompt, before the text: the CLI draws its input box again (tests: 0)


def pane_prompts(conn, terminals):
    """{agent: prompt} for the agents whose tmux pane shows a prompt now (prompt_pattern), with one tmux command
    for every pane (#4607). A prompt: {question, options (their text, as the CLI lists them), selected (the option
    the marker is on), tool, command, why, lines (the prompt's lines as the pane shows them), sig (what the page
    sends back with an answer, so a prompt that changed gets no keys)}."""
    pattern = core.setting(conn, "prompt_pattern").strip()
    if not pattern or not terminals:
        return {}
    out = {}
    texts = _pane_texts(sorted(set(terminals.values())))
    for agent, pane in terminals.items():
        tail = _prompt_tail(_plain(texts.get(pane) or ""))
        rx, layout, _ = _pane_rule(conn, agent)
        parts = _prompt_found(tail, rx, layout)
        if not parts:
            continue
        options = [o[0] for o in parts["options"]]
        sig = "\x1f".join([parts["question"] or "", parts["command"] or "", *options])
        out[agent] = {"question": parts["question"], "options": options,
                      "selected": next((n for n, o in enumerate(parts["options"]) if o[1]), 0),
                      "tool": parts["tool"], "command": parts["command"], "why": parts["why"],
                      "lines": tail[parts["start"]:], "sig": sig}
    return out


def _menu_keys(at, n):
    """The keys that choose option n of a menu when option at is selected."""
    return [{"key": "Down" if n > at else "Up"}] * abs(n - at) + [{"key": "Enter"}]


def prompt_answer(conn, agent, sig, pick=None, other=None, who=None):
    """The prompt dialog of the Sessions tab (#4607): a person's answer to the prompt that the agent's tmux pane
    shows. pick: the number of an option (0 is the first); other: the person's text, sent by the CLI's own way to
    tell the agent what to do instead (the option of its adapter's DETECT['tell_instead'], "No, and tell ... what to
    do", else Escape), then Enter. Refused when the pane shows another prompt than the one the person read (sig)."""
    pane = _typing_pane(conn, agent, who)
    p = pane_prompts(conn, {agent: pane}).get(agent)
    if not p or p["sig"] != sig:
        raise RiverError(f"the prompt in the terminal of {agent} changed or is gone; look at it again")
    opts, at = p["options"], p["selected"]
    text = " ".join(str(other or "").split())
    if text:
        if len(text) > 4000:
            raise RiverError("text is at most 4000 characters")
        tell = _pane_rule(conn, agent)[1]["tell"]
        n = next((i for i, o in enumerate(opts) if tell.search(o)), None)
        terminal_keys(conn, agent, _menu_keys(at, n) if n is not None else [{"key": "Escape"}], who)
        time.sleep(PROMPT_PAUSE)
        terminal_keys(conn, agent, [{"text": text}, {"key": "Enter"}], who)
        return {"agent": agent, "pane": pane, "picked": opts[n] if n is not None else "Escape", "text": text}
    if not isinstance(pick, int) or not 0 <= pick < len(opts):
        raise RiverError("pick one of the prompt's options, or write what to do instead")
    terminal_keys(conn, agent, _menu_keys(at, pick), who)
    return {"agent": agent, "pane": pane, "picked": opts[pick], "text": ""}


# The side panel of the Sessions tab (#4619) shows local files: read only, with the page's token, and only under
# the project folders, their git worktrees, the Claude Code scratchpads of their sessions, and the uploads of
# sessions (#4683). Each file comes with a sandbox policy, so an
# HTML file runs in an origin of its own and can never act as the page (its POST has Origin null: refused).
FILE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
              ".webp": "image/webp", ".svg": "image/svg+xml", ".pdf": "application/pdf",
              ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
              ".md": "text/plain; charset=utf-8", ".txt": "text/plain; charset=utf-8"}
FILE_MAX = 50 * 1024 * 1024
FILE_CSP = "sandbox allow-scripts allow-popups allow-forms"
_ROOTS = {"at": 0.0, "roots": []}


def file_roots(conn):
    """The folders the side panel may show files from: every project folder, each git worktree of it, and the
    uploads folder. Read again after a minute (worktrees come and go)."""
    import subprocess
    if time.time() - _ROOTS["at"] < 60:
        return _ROOTS["roots"]
    roots = {uploads_dir().resolve()}
    for r in conn.execute("SELECT path FROM projects WHERE path IS NOT NULL AND path<>''"):
        p = Path(r["path"]).expanduser()
        if not p.is_dir():
            continue
        roots.add(p.resolve())
        try:
            out = subprocess.run(["git", "-C", str(p), "worktree", "list", "--porcelain"], capture_output=True,
                                 text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        roots.update(Path(x[9:]).resolve() for x in out.splitlines() if x.startswith("worktree "))
    _ROOTS.update(at=time.time(), roots=sorted(roots))
    return _ROOTS["roots"]


CLAUDE_TMP = None  # tests: the folder of Claude Code's session scratchpads (claude_tmps)


def claude_tmps():
    """The folders where Claude Code keeps each session's scratchpad: /tmp/claude-<uid>/<project slug>/<session id>/
    scratchpad (on macOS /private/tmp), and CLAUDE_CODE_TMPDIR: a session sees the claude-<uid> folder itself there,
    a person may set its parent. None on Windows."""
    if CLAUDE_TMP:
        return [Path(CLAUDE_TMP)]
    if not hasattr(os, "getuid"):
        return []
    own = f"claude-{os.getuid()}"
    env = os.environ.get("CLAUDE_CODE_TMPDIR")
    bases = [Path("/tmp") / own] + ([Path(env) if Path(env).name == own else Path(env) / own] if env else [])
    return list(dict.fromkeys(b.resolve() for b in bases))


def _scratchpad_of(conn, f):
    """The scratchpad folder that holds f, when it is the scratchpad of a Claude Code session in a project folder
    or one of its worktrees (#4683): the screenshots and pages a session makes for the person go there. Else None."""
    base = next((b for b in claude_tmps() if b in f.parents), None)
    if base is None:
        return None
    rel = f.relative_to(base).parts
    if len(rel) < 4 or rel[2] != "scratchpad":
        return None
    slugs = {re.sub(r"[^A-Za-z0-9]", "-", str(r)) for r in file_roots(conn)}
    return base.joinpath(*rel[:3]) if rel[0] in slugs else None


def local_file(conn, path, read=True):
    """(bytes, type) of a local file for the side panel; RiverError when the path is outside file_roots, hidden,
    not a file, of a type it does not show, or too large."""
    if not path or "\0" in path:
        raise RiverError("no file named")
    f = Path(path).expanduser()
    if not f.is_absolute():
        raise RiverError("the side panel shows a file by its full path")
    f = f.resolve()
    root = next((r for r in file_roots(conn) if r == f or r in f.parents), None) or _scratchpad_of(conn, f)
    hidden = [i for i, part in enumerate(f.parts) if i and part.startswith(".")]
    # One hidden folder is allowed: .playwright-mcp right under a project folder, where the Playwright MCP saves
    # the screenshots a session takes (#4683).
    if root and hidden == [len(root.parts)] and f.parts[hidden[0]] == ".playwright-mcp":
        hidden = []
    if hidden and uploads_dir().resolve() not in f.parents:
        raise RiverError("the side panel shows no hidden files")
    if root is None:
        raise RiverError("the side panel shows files only from the project folders, their worktrees, their sessions' "
                         "scratchpads, and uploads")
    kind = FILE_TYPES.get(f.suffix.lower())
    if kind is None:
        raise RiverError(f"the side panel does not show {f.suffix or 'this kind of'} files")
    if not f.is_file():
        raise RiverError("no such file")
    if f.stat().st_size > FILE_MAX:
        raise RiverError("the file is too large for the side panel")
    return (f.read_bytes() if read else b""), kind


def file_check(conn, paths, agent=None):
    """#4689: for each path that a session's text names, the full path it means and whether the side panel shows
    it, else why: {path: {target, ok, why}}. A file:// link and a ~ path become full paths; a relative path or a
    bare name is looked for in the session's folder (its transcript's cwd), then among the files the session
    wrote or named (the newest one whose path ends with it)."""
    from . import sessions
    cwd, named = None, []
    if agent:
        kind, tpath = sessions.transcript(conn, agent)
        if tpath:
            try:
                cwd = sessions.last_cwd(tpath)
                named = [h["target"] for h in sessions._artifact_hits(tpath, kind) if h["kind"] == "file"]
            except (OSError, ValueError, EOFError):
                pass
    return {p: _check_path(conn, p, cwd, named) for p in list(dict.fromkeys(paths))[:200]}


def _check_path(conn, p, cwd, named):
    import urllib.parse
    t = str(p or "").strip()
    if t.lower().startswith("file://"):
        u = urllib.parse.urlsplit(t)
        if u.netloc not in ("", "localhost"):
            return {"target": None, "ok": False, "why": "the link names a file on another computer"}
        t = urllib.parse.unquote(u.path)
        # A Windows file URL (file:///C:/Users/...) has its drive after the first "/": the path is C:\Users\...
        if core.PLATFORM == "win32" and re.match(r"^/[a-zA-Z]:[/\\]", t):
            t = str(Path(t[1:]))
    if not (t.startswith(("/", "~", "\\\\")) or re.match(r"^[a-zA-Z]:[/\\]", t)):
        found = None
        if cwd and Path(cwd, t).is_file():
            found = str(Path(cwd, t))
        if not found:
            end = "/" + re.sub(r"^(\./)+", "", t.replace("\\", "/"))
            found = next((n for n in reversed(named) if n.replace("\\", "/").endswith(end)), None)
        if not found:
            return {"target": None, "ok": False, "why": "no file of this session has this name"}
        t = found
    try:
        local_file(conn, t, read=False)
        return {"target": t, "ok": True, "why": None}
    except RiverError as e:
        return {"target": t, "ok": False, "why": str(e)}


# Full screen (#4620): each thing the side panel shows also has an address of its own, /view, for a browser tab of
# its own. A new tab cannot send the page's token, so the address carries a key: an HMAC of the target and an end
# time, made with a secret kept in the queue (it outlives a restart of maxpm serve). The key opens that one target
# until it ends, nothing else.
VIEW_HOURS = 8


def _view_secret(conn):
    import secrets
    r = conn.execute("SELECT value FROM meta WHERE key='view_secret'").fetchone()
    if r:
        return r["value"]
    with core.tx(conn):
        conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('view_secret', ?)", (secrets.token_hex(32),))
    return conn.execute("SELECT value FROM meta WHERE key='view_secret'").fetchone()["value"]


def view_key(conn, target, end):
    import hashlib
    return hmac.new(_view_secret(conn).encode(), f"{target}\x1f{int(end)}".encode(), hashlib.sha256).hexdigest()[:40]


def view_ok(conn, target, end, key):
    try:
        end = int(end)
    except (TypeError, ValueError):
        return False
    return end > time.time() and hmac.compare_digest(str(key or ""), view_key(conn, target, end))


def _file_target(target):
    """A full Unix, Windows drive or UNC path, including a home-relative path."""
    from pathlib import PureWindowsPath
    return str(target).startswith(("/", "~/", "~\\")) or PureWindowsPath(str(target)).is_absolute()


def _canvas_target(target):
    """The canvas id of a target canvas:<id> (#4768), else None."""
    m = re.fullmatch(r"canvas:c?(\d+)", str(target or ""))
    return int(m.group(1)) if m else None


def view_params(conn, target):
    """(end, key): a full-screen key for a target (a full file path, a web address, or canvas:<id>), valid for
    VIEW_HOURS. A canvas tab of the side panel loads its frame with it too, never with the page's token (#4768)."""
    cid = _canvas_target(target)
    if cid is not None:
        core.canvas_version(conn, cid)  # refuses a canvas that is gone
    elif not (_file_target(target) or re.match(r"^https?://", str(target))):
        raise RiverError("full screen shows a web address, a full file path, or a canvas")
    end = int(time.time() + VIEW_HOURS * 3600)
    return end, view_key(conn, target, end)


def view_url(conn, target, session="", item=""):
    """The full-screen address of a target (a full file path, a web address, or canvas:<id>), valid for VIEW_HOURS."""
    from urllib.parse import urlencode
    end, key = view_params(conn, target)
    q = {"t": target, "e": end, "k": key}
    if session:
        q["s"] = session
    if item:
        q["i"] = str(item)
    return "/view?" + urlencode(q)


def view_page(conn, q):
    """The HTML of /view: the target alone in the tab, under a thin bar with its name, the session and item it came
    from, Reload and Copy link."""
    import html
    from urllib.parse import quote
    t, end, key = q.get("t", ""), q.get("e", ""), q.get("k", "")
    e = html.escape
    if not view_ok(conn, t, end, key):
        return 403, ("<!doctype html><meta charset=utf-8><title>MaximizePM</title><body style='font:15px system-ui;padding:24px'>"
                     "This full-screen link has ended or is not valid. Open it again from the MaximizePM page.</body>")
    cid = _canvas_target(t)
    if cid is not None:
        return _canvas_view(conn, cid, q, int(end), key)
    is_file = _file_target(t)
    name = re.split(r"[/\\]", t.rstrip("/\\"))[-1] if is_file else re.sub(r"^https?://", "", t)[:80]
    src = f"/api/file?path={quote(t)}&e={int(end)}&k={quote(key)}" if is_file else t
    if is_file and re.search(r"\.(png|jpe?g|gif|webp|svg)$", t, re.I):
        body = f"<div class=fit><img src='{e(src)}' alt='{e(name)}'></div>"
    elif is_file and re.search(r"\.(md|txt)$", t, re.I):
        body = f"<div id=md class=md data-src='{e(src)}' data-md='{1 if t.lower().endswith('.md') else 0}'></div>"
    elif is_file or re.match(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?(/|$)", t, re.I) or frame_check(t)["frame"]:
        body = f"<iframe src='{e(src)}' sandbox='allow-scripts allow-forms allow-popups allow-same-origin'></iframe>"
    else:
        body = (f"<div class=card><b>{e(name)}</b><p>This site does not let another page show it in a frame.</p>"
                f"<a class=btn href='{e(t)}' rel='noopener noreferrer'>Open it here</a></div>")
    came = " · ".join(x for x in (q.get("s") and "from " + e(q["s"]), q.get("i", "").isdigit() and f"#{q['i']}") if x)
    return 200, _view_shell(t, name, came, body, _VIEW_MD_JS)


# Draws a Markdown target of /view (an .md file, a Markdown canvas): its text from data-src, cleaned by DOMPurify.
_VIEW_MD_JS = """const md = document.getElementById("md");
async function drawMd() {
  if (!md) return;
  const r = await fetch(md.dataset.src); const text = r.ok ? await r.text() : "MaximizePM cannot show this file.";
  if (md.dataset.md === "1" && r.ok) {
    const { marked } = await import("/vendor/marked/marked.esm.js");
    const DOMPurify = (await import("/vendor/dompurify/purify.es.mjs")).default;
    md.innerHTML = DOMPurify.sanitize(marked.parse(text), { FORBID_TAGS: ["style", "form", "input", "iframe", "script"] });
    const { drawDiagrams } = await import("/components/canvasMd.js");  // ```mermaid blocks as pictures (#4983)
    drawDiagrams(md);
  } else { const p = document.createElement("pre"); p.textContent = text; md.replaceChildren(p); }
}
await drawMd();
"""

# A canvas in /view follows its newest version (#4768): it asks for the version every three seconds and loads the
# new one in place; the helper script in an HTML canvas keeps the scroll position.
_VIEW_CANVAS_JS = """const cv = document.getElementById("cv"), ver = document.getElementById("ver");
let shown = +ver.dataset.v, pos = { x: 0, y: 0 };
addEventListener("message", (ev) => {
  const d = ev.data || {};
  if (!cv || ev.source !== cv.contentWindow) return;
  if (d.maxpmCanvas === "scroll") pos = { x: d.x, y: d.y };
  if (d.maxpmCanvas === "ready") cv.contentWindow.postMessage({ maxpmCanvas: "scrollTo", x: pos.x, y: pos.y }, "*");
});
setInterval(async () => {
  if (document.hidden) return;
  try {
    const r = await fetch(ver.dataset.meta); if (!r.ok) return;
    const j = await r.json();
    if (j.version === shown) return;
    shown = j.version; ver.textContent = "v" + shown;
    if (cv) cv.src = cv.src; else { const y = md.scrollTop; await drawMd(); md.scrollTop = y; }
  } catch (e) { /* the next poll asks again */ }
}, 3000);
"""


def _canvas_view(conn, cid, q, end, key):
    """/view of a canvas: its newest version in a sandboxed frame (HTML, SVG) or drawn (Markdown), live (#4768)."""
    import html
    from urllib.parse import quote
    e = html.escape
    try:
        c, v = core.canvas_version(conn, cid)
    except RiverError as err:
        return 404, _view_shell("", "MaximizePM", "", f"<div class=card>{e(str(err))}</div>")
    sig = f"e={int(end)}&k={quote(key)}"
    src, meta = f"/canvas/{cid}/latest?{sig}", f"/canvas/{cid}/meta?{sig}"
    if c["kind"] == "md":
        body = f"<div id=md class=md data-src='{e(src)}' data-md=1></div>"
    else:
        body = f"<iframe id=cv src='{e(src)}' sandbox='allow-scripts' title='{e(c['title'] or c['name'])}'></iframe>"
    came = " · ".join(x for x in (
        f"<span id=ver data-v='{v['version']}' data-meta='{e(meta)}'>v{v['version']}</span>", "canvas",
        q.get("s") and "from " + e(q["s"]), q.get("i", "").isdigit() and f"#{q['i']}") if x)
    return 200, _view_shell(f"canvas:{cid}", c["title"] or c["name"], came, body, _VIEW_MD_JS + _VIEW_CANVAS_JS)


def _view_shell(title, name, came, body, script=""):
    """The HTML of a /view page: a thin bar (name, where it came from, Reload, Copy link) over the body."""
    import html
    e = html.escape
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width, initial-scale=1">
<title>{e(name)} · MaximizePM</title><style>
:root{{--bg:#f6f7f9;--panel:#fff;--ink:#1b1f24;--muted:#5f6b7a;--line:#dde2e8;--accent:#1f6feb}}
@media (prefers-color-scheme: dark){{:root{{--bg:#0f1216;--panel:#171b21;--ink:#e6e9ee;--muted:#9aa5b1;--line:#2a3139;--accent:#4c8dff}}}}
html,body{{margin:0;height:100%;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
body{{display:flex;flex-direction:column}}
.bar{{display:flex;gap:10px;align-items:center;padding:6px 12px;background:var(--panel);border-bottom:1px solid var(--line);flex:none}}
.bar b{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}} .bar .m{{color:var(--muted);font-size:12px}} .bar .s{{flex:1}}
button,.btn{{font:inherit;font-size:13px;background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:3px 10px;cursor:pointer;text-decoration:none}}
iframe{{flex:1;border:0;width:100%;background:#fff}} .fit{{flex:1;overflow:auto;display:flex;align-items:flex-start;justify-content:center}} .fit img{{max-width:100%}}
.md{{flex:1;overflow:auto;padding:24px;max-width:56rem;margin:0 auto;width:100%;box-sizing:border-box}} .md pre{{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px;overflow:auto}}
.card{{margin:40px auto;max-width:32rem;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px}}
</style></head><body>
<div class=bar><b title="{e(title)}">{e(name)}</b><span class=m>{came}</span><span class=s></span>
<button onclick="location.reload()">Reload</button><button id=copy>Copy link</button></div>
{body}
<script type=module>
document.getElementById("copy").onclick = async (ev) => {{ try {{ await navigator.clipboard.writeText(location.href); ev.target.textContent = "Copied"; }} catch (e) {{ prompt("Copy this link", location.href); }} }};
{script}
</script></body></html>"""


def frame_check(url):
    """{"frame": whether the site lets another page show it in a frame, "title"} for an outside http(s) page, read
    from its headers (X-Frame-Options, CSP frame-ancestors). A page that cannot be read: frame False."""
    import re
    import urllib.request
    if not re.match(r"^https?://", url or ""):
        return {"frame": False, "title": "", "error": "not a web address"}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MaximizePM side panel"})
        with urllib.request.urlopen(req, timeout=5) as r:
            xfo = (r.headers.get("X-Frame-Options") or "").lower()
            csp = (r.headers.get("Content-Security-Policy") or "").lower()
            head = r.read(65536).decode("utf-8", "replace")
    except Exception as e:  # any failure: the panel offers a new tab
        return {"frame": False, "title": "", "error": str(e)[:200]}
    fa = re.search(r"frame-ancestors([^;]*)", csp)
    frame = not xfo and not (fa and "*" not in fa.group(1).split())
    m = re.search(r"<title[^>]*>(.*?)</title>", head, re.I | re.S)
    return {"frame": frame, "title": " ".join(m.group(1).split())[:200] if m else ""}


# A POST that types into a terminal needs this token, which only the page of this server reads (GET /api/token,
# for this computer only, and no other site may read the answer). A new one each start of maxpm serve.
PAGE_TOKEN = secrets.token_urlsafe(24)
TYPING_OPS = ("terminal_keys", "session_send", "session_upload", "session_upload_drop", "prompt_answer", "session_setting", "session_restart",
              "shell_open", "shell_keys", "shell_close")  # the Terminal tab's shells (#4803)


REPO = PKG.parent


def _git(repo, *args, timeout=60):
    import subprocess
    try:
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        raise RiverError(f"git {args[0]}: {e}")
    if r.returncode:
        raise RiverError(f"git {args[0]}: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def _no_update_in_app():
    if DESKTOP:
        raise RiverError("the desktop app updates itself; the git Update does not apply here")


def update_status(repo=REPO, fetch=True):
    """Whether this river install (a git clone) is behind its upstream branch: the page's Update button."""
    if not (Path(repo) / ".git").exists():
        return {"git": False}
    out = {"git": True, "fetch_error": None}
    if fetch:
        try:
            _git(repo, "fetch", "--quiet")
        except RiverError as e:
            out["fetch_error"] = str(e)
    out["head"] = _git(repo, "rev-parse", "--short", "HEAD")
    out["branch"] = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    try:
        out["upstream"] = _git(repo, "rev-parse", "--abbrev-ref", "@{u}")
    except RiverError:
        return {**out, "upstream": None, "behind": 0, "ahead": 0, "commits": []}
    out["behind"] = int(_git(repo, "rev-list", "--count", "HEAD..@{u}"))
    out["ahead"] = int(_git(repo, "rev-list", "--count", "@{u}..HEAD"))
    out["commits"] = _git(repo, "log", "--format=%h %s", "-20", "HEAD..@{u}").splitlines() if out["behind"] else []
    return out


def _restart_argv():
    """The command line that starts this server again: the same, without --open (the page is open already)."""
    return [sys.executable] + [a for a in sys.argv if a != "--open"]


def restart_now(why="code changed"):
    """Replace this process with a new maxpm serve on the same port, which runs the code on disk. Python
    sockets are not inherited across exec, so the port is free for the new process."""
    print(f"{why}; restarting", flush=True)
    os.execv(sys.executable, _restart_argv())


def _restart_soon(why="updated"):
    """Start the server process again after the reply goes out, so it runs the new code."""
    def go():
        time.sleep(0.8)
        restart_now(why)
    threading.Thread(target=go, daemon=True).start()


def _code_loads():
    """None when the river code on disk loads in a new Python, else the last line of its error: a restart
    onto code that does not start would leave no page at all."""
    import subprocess
    try:
        r = subprocess.run([sys.executable, "-c", "import river.server, river.cli, river.notify, river.mcp"],
                           cwd=str(REPO), capture_output=True, text=True, timeout=60,
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)
    return None if r.returncode == 0 else ((r.stderr or r.stdout).strip().splitlines() or ["it does not load"])[-1]


def _code_dirty():
    """True when git shows a change in the code folder that is not committed: someone is in the middle of an
    edit. False in a folder that is not a git clone (pip, the app): the files there change all at once."""
    try:
        lines = _git(REPO, "status", "--porcelain", "--untracked-files=all", "--", str(PKG), timeout=20).splitlines()
    except RiverError:
        return False
    # A file that git does not track counts only when it is code: a .DS_Store of the Finder in the folder held
    # every reload on a Mac (#1663).
    return any(not ln.startswith("??") or ln.rstrip('"').endswith(".py") for ln in lines)


# The code id maxpm serve saw on its last pass, and the one that did not load (it waits for the next change).
RELOAD = {"seen": None, "bad": None}


def reload_ready(conn):
    """One look of the loop of maxpm serve (every notify_interval): the code id to start again for, or None.
    The code on disk must be newer than the code this server runs (code_stale), the same as on the pass
    before (nobody writes it now), committed (in a git clone), and it must load. Setting serve_reload."""
    if DESKTOP or DEV["on"] or core.setting(conn, "serve_reload") != "on":
        return None  # the app's code changes with the app; dev mode restarts on every change by itself
    cur = _code_id()
    if cur == BOOT_CODE:
        RELOAD["seen"] = None
        return None
    if RELOAD["seen"] != cur:
        RELOAD["seen"] = cur
        return None
    if cur == RELOAD["bad"] or _code_dirty():
        return None
    why = _code_loads()
    if why:
        RELOAD["bad"] = cur
        print(f"maxpm serve: the new code does not load, so the old code keeps running: {why}", flush=True)
        return None
    return cur


def serve_restart(conn):
    """maxpm serve --restart: start again now, when the code on disk loads. The reply goes out first."""
    if DESKTOP:
        raise RiverError("the desktop app runs its own copy of the code; start the app again to restart it")
    why = _code_loads()
    if why:
        raise RiverError(f"the code on disk does not load, so maxpm serve keeps the code it runs: {why}")
    _restart_soon("restart asked")
    return {"restarting": True, "boot": BOOT, "stale": code_stale()}


def update_apply(repo=REPO, restart=_restart_soon):
    """Fast-forward this river install to its upstream branch, then restart the server. Agents and the
    command pick up the new code on their next run, because `river` and the skills link into this folder."""
    st = update_status(repo)
    if not st["git"]:
        raise RiverError("this copy of MaximizePM is not a git clone; update it the way you installed it")
    if st["fetch_error"]:
        raise RiverError(f"could not check for updates: {st['fetch_error']}")
    if not st["upstream"]:
        raise RiverError(f"branch {st['branch']} has no upstream to update from")
    if not st["behind"]:
        return {**st, "updated": False}
    if st["ahead"]:
        raise RiverError(f"this clone has {st['ahead']} commit(s) that {st['upstream']} does not have, so it "
                         f"cannot fast-forward; push or rebase them in {repo} yourself, then update")
    try:
        _git(repo, "merge", "--ff-only", "@{u}")
    except RiverError as e:
        raise RiverError(f"could not fast-forward {repo}: local changes are in the way; commit or stash them "
                         f"(git status), then update.\n{e}")
    restart()
    return {**update_status(repo, fetch=False), "updated": True, "from": st["head"], "commits": st["commits"]}


# Agent CLIs the setup guide offers for the Start button. Claude Code and Codex are launch profiles: river
# builds their commands from their options (core.LAUNCH_PLATFORMS). The others get an explicit first prompt,
# so they work even where the agent does not read the project's instruction file.
KNOWN_AGENTS = [
    ("Claude Code", "claude", "@claude-code"),
    ("Codex", "codex", "@codex"),
    ("Grok", "grok", 'grok "run maxpm go in this folder and follow the briefing"'),
    ("OpenCode", "opencode", 'opencode --prompt "run maxpm go in this folder and follow the briefing"'),
    ("Gemini", "gemini", 'gemini -i "run maxpm go in this folder and follow the briefing"'),
    # pi opens its terminal UI with a message argument as the first prompt; it reads AGENTS.md (#4924).
    ("pi", "pi", 'pi "run maxpm go in this folder and follow the briefing"'),
]


def _agent_cmd(cmd):
    """A KNOWN_AGENTS command with {maxpm_dir} filled in for this computer."""
    import shlex
    d = core.river_dir()
    return cmd.replace("{maxpm_dir}", f'"{d}"' if core.PLATFORM == "win32" else shlex.quote(d))


def _shown_cmd(conn, cmd):
    """What an entry runs, as the setup guide shows it: a profile's command with its current options."""
    prof = core.parse_profile(cmd)
    return core.build_command(prof[0], core.profile_options(conn, *prof)) if prof else _agent_cmd(cmd)


def _block_state(path):
    from . import agents
    from .cli import AGENT_SNIPPET, _imports_agents
    if not path.exists():
        return "missing"
    text = path.read_text(errors="replace")
    own, shared, _, _ = agents.instruction_files()
    if path.name == own and _imports_agents(text):
        return _block_state(path.with_name(shared))  # Claude Code reads AGENTS.md through the import
    return "current" if AGENT_SNIPPET in text else "old" if core.names_product(text) else "missing"


def setup_status(conn):
    """What the setup guide shows: each check, and whether it is done."""
    import shutil
    from . import notify
    from . import agents as adapters
    from .cli import instructions_layout
    own, shared, _, _ = adapters.instruction_files()
    folders = {}
    for p in core.project_list(conn):
        if p["path"]:
            folders.setdefault(p["path"], []).append(p["name"])
    skills = Path(adapters.get("claude").setup["skills_dir"]).expanduser()
    agents = core.parse_launch_agents(core.setting(conn, "launch_agents"))
    have = {label for label, _ in agents}
    # Agent programs as a new terminal finds them (the app itself has only Finder's short PATH).
    # Agents start in a new tmux pane, whose login shell must find their programs (on Windows: PATH).
    exes = sorted({exe for _, exe, _ in KNOWN_AGENTS} | {core.entry_exe(c) for _, c in agents})
    term = _login_shell_which(exes) if core.PLATFORM != "win32" else {e: shutil.which(e) for e in exes}
    return {
        "done": core.setting(conn, "setup_done") == "on",
        "folders": [{"path": d, "projects": names, "exists": Path(d).is_dir(),
                     "claude_md": _block_state(Path(d, own)), "agents_md": _block_state(Path(d, shared)),
                     "layout": instructions_layout(d) if Path(d).is_dir() else None}
                    for d, names in folders.items()],
        "projects_without_folder": [p["name"] for p in core.project_list(conn) if not p["path"]],
        "people": [r["name"] for r in conn.execute("SELECT name FROM agents WHERE kind='human' ORDER BY name")],
        "claude_home": Path("~/.claude").expanduser().is_dir(),
        "skills": {n: ("installed" if (skills / n / "SKILL.md").is_file() else "missing") for n in ("maxpm", "maxpm-manager")},
        "launch_agents": [label for label, _ in agents],
        "tmux": core.can_start_agents(),  # every agent starts in tmux (Windows: a console window)
        "tmux_install": core.tmux_install(),
        # Each Start button agent, and whether its program is on this computer (the first word of its command).
        "start_agents": [{"label": label, "found": bool(term.get(core.entry_exe(cmd)))} for label, cmd in agents],
        "agent_clis": [{"label": label, "found": bool(term.get(exe)), "added": label in have,
                        "command": _shown_cmd(conn, cmd)} for label, exe, cmd in KNOWN_AGENTS],
        # The options of each launch profile the Start button uses, for toggles (Settings > Setup).
        "launch_profiles": core.launch_profiles(conn),
        "notify_channels": core._channels(core.setting(conn, "notify_channels")),
        "ntfy_ready": bool(core.setting(conn, "ntfy_topic")),
        # Where a tap on a notification opens (#4673): the global value, and each person's own.
        "notify_open": core.setting(conn, "notify_open"),
        "notify_open_people": {r["scope"][6:]: r["value"] for r in conn.execute(
            "SELECT scope, value FROM settings WHERE key='notify_open' AND scope LIKE 'agent:%'")},
        "notify_phone": notify.phone_note(conn),
        "maxpm_cmd": command_status(),
    }


def setup_block(conn, path, move=None):
    """Add or update the work queue block for every agent in a registered project folder (maxpm init):
    AGENTS.md holds it and CLAUDE.md imports it; move=True first moves the rules in CLAUDE.md to AGENTS.md."""
    from .cli import setup_instructions
    folder = Path(path).expanduser().resolve()
    if folder not in {Path(p["path"]).resolve() for p in core.project_list(conn) if p["path"]}:
        raise RiverError(f"{path} is not the folder of a maxpm project")
    if not folder.is_dir():
        raise RiverError(f"{path} does not exist")
    return setup_instructions(folder, move)


def folder_add(conn, path, name=None, description="", move=None, actor=None):
    """Add a project folder from the page: what maxpm init does in that folder, without a terminal. The
    project is created (default name: the folder's) or linked; a project linked to another folder that
    still exists is refused (only the user moves it: maxpm project path --move). Then the agent block
    goes in AGENTS.md, and CLAUDE.md imports it; move is the CLAUDE.md rules choice (None: ask)."""
    from .cli import link_folder, setup_instructions, instructions_layout, folder_project_name
    if not (path or "").strip():
        raise RiverError("choose a folder")
    folder = Path(path.strip()).expanduser()
    if not folder.is_absolute():
        raise RiverError(f"{path} is not a full path; start it with / or ~")
    if not folder.is_dir():
        raise RiverError(f"{folder} is not a folder on this computer")
    folder = folder.resolve()
    name = (name or "").strip() or None
    if name:
        row = conn.execute("SELECT path FROM projects WHERE name=?", (name,)).fetchone()
        if row and row["path"] and Path(row["path"]) != folder and Path(row["path"]).is_dir():
            raise RiverError(f"project {name} is linked to {row['path']}. Pick another name for this folder, "
                             f"or move the project in a terminal: maxpm project path {name} {folder} --move")
    name, linked_here, lines = link_folder(conn, folder, name, description.strip(), actor)
    lines += setup_instructions(folder, move)
    return {"project": name or linked_here[0], "path": str(folder), "lines": lines,
            "layout": instructions_layout(folder), "default_name": folder_project_name(folder)}


# The maxpm command for agents: a small launcher in ~/.local/bin that runs this MaximizePM with this Python, so an
# agent started from the app (or any terminal) can run `maxpm go` on a Mac that has only the app.
LAUNCHER_MARK = "# MaximizePM launcher"


def _launcher_path():
    return Path("~/.local/bin/maxpm").expanduser()


def _login_shell_which(names):
    """Where a new terminal finds each command, {name: path or None}. The app runs with the short PATH
    that Finder gives, so ask a login shell started as Terminal starts one: the system PATH, then the
    profile files (not this process's PATH)."""
    import re
    import subprocess
    names = [n for n in names if re.fullmatch(r"[A-Za-z0-9._-]+", n)]
    shell = os.environ.get("SHELL") or "/bin/zsh"
    env = {k: os.environ[k] for k in ("HOME", "USER", "LOGNAME", "SHELL", "LANG", "TMPDIR") if k in os.environ}
    env.update(PATH="/usr/bin:/bin:/usr/sbin:/sbin", TERM="dumb")
    script = "; ".join(f'printf "@@{n}=%s\\n" "$(command -v {n})"' for n in names)
    try:
        r = subprocess.run([shell, "-ilc", script], capture_output=True, text=True, timeout=8,
                           stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.SubprocessError):
        return {n: None for n in names}
    found = dict(x[2:].split("=", 1) for x in r.stdout.splitlines() if x.startswith("@@") and "=" in x)
    return {n: (found.get(n) or "").strip() if (found.get(n) or "").strip().startswith("/") else None for n in names}


def _login_shell_maxpm():
    """What `maxpm` is in a new terminal, or None."""
    return _login_shell_which(["maxpm"])["maxpm"]


def command_status():
    """ok: `maxpm` works in a new terminal. ours: it is this launcher. launcher: the launcher's state
    (current / old / missing). shell_path: what a new terminal finds. where: where the launcher goes."""
    if core.PLATFORM == "win32":
        return {"ok": True, "unsupported": True}
    lp = _launcher_path()
    text = lp.read_text(errors="replace") if lp.is_file() else ""
    launcher = "missing" if not text else "current" if text == _launcher_text() else "old" if LAUNCHER_MARK in text else "other"
    found = _login_shell_maxpm()
    # A maxpm command that the setup guide did not write (a link to a clone, pip) is the person's own: fine.
    ours = bool(found) and Path(found).expanduser() == lp and launcher in ("current", "old")
    return {"ok": bool(found) and (not ours or launcher == "current"), "shell_path": found, "ours": ours,
            "launcher": launcher, "where": str(lp), "in_app_image": "/Volumes/" in str(Path(__file__).resolve())}


def _launcher_text():
    import shlex
    root = Path(__file__).resolve().parent.parent
    py, script = shlex.quote(sys.executable), shlex.quote(str(root / "bin" / "maxpm"))
    lines = [f"#!/bin/sh", f"{LAUNCHER_MARK}: runs the MaximizePM that the setup guide found ({root}).",
             "# The setup guide rewrites it (Add the maxpm command); delete it to remove it."]
    if DESKTOP:
        # An app opened from Downloads and then moved to Applications: use the copy in Applications.
        app = "/Applications/MaximizePM.app/Contents/Resources/river-app"
        lines += [f"if [ ! -f {script} ] && [ -x '{app}/python/bin/python3' ]; then",
                  f"  exec '{app}/python/bin/python3' '{app}/bin/maxpm' \"$@\"", "fi"]
    lines += [f"if [ ! -x {py} ] || [ ! -f {script} ]; then",
              f"  echo \"maxpm: {root} is gone (the app moved or was removed). Open MaximizePM, then Settings,\" >&2",
              "  echo \"the setup guide, and Add the maxpm command again.\" >&2", "  exit 1", "fi",
              f"exec {py} {script} \"$@\""]
    return "\n".join(lines) + "\n"


def install_command():
    """Write the launcher, and put ~/.local/bin on PATH in the login profile when a new terminal would not
    find it. A maxpm command that is not ours (a clone or pip) is left alone. Returns what changed."""
    if core.PLATFORM == "win32":
        raise RiverError("the maxpm command installer works on macOS for now; on Windows, add the bin folder of "
                         "MaximizePM to PATH")
    st = command_status()
    if st["in_app_image"]:
        raise RiverError("the app runs from its download image: drag MaximizePM to Applications, open it "
                         "from there, then add the maxpm command")
    if st["shell_path"] and not st["ours"]:
        return {"changed": [], "note": f"maxpm is already installed at {st['shell_path']}; left as it is"}
    lp, changed = _launcher_path(), []
    if lp.exists() and st["launcher"] == "other":
        raise RiverError(f"{lp} exists and is not the launcher of MaximizePM; move it away first")
    lp.parent.mkdir(parents=True, exist_ok=True)
    lp.write_text(_launcher_text())
    lp.chmod(0o755)
    changed.append(f"{lp}: runs MaximizePM from {Path(__file__).resolve().parent.parent}")
    if not _login_shell_maxpm():
        shell = Path(os.environ.get("SHELL") or "/bin/zsh").name
        prof = Path("~/.zprofile" if shell == "zsh" else "~/.bash_profile" if shell == "bash" else "~/.profile").expanduser()
        old = prof.read_text() if prof.exists() else ""
        line = 'export PATH="$HOME/.local/bin:$PATH"  # MaximizePM: the maxpm command'
        if line not in old:
            prof.write_text(old + ("\n" if old and not old.endswith("\n") else "") + line + "\n")
            changed.append(f"{prof}: adds ~/.local/bin to PATH for new terminals")
    return {"changed": changed, "status": command_status()}


def setup_skills():
    from .cli import install_skills
    return install_skills(Path("~/.claude/skills").expanduser())


def setup_agent_add(conn, label, actor=None):
    """Add one of KNOWN_AGENTS to launch_agents (the Start button's list)."""
    cmd = next((_agent_cmd(c) for lab, _, c in KNOWN_AGENTS if lab == label), None)
    if cmd is None:
        raise RiverError(f"unknown agent {label!r}")
    agents = core.parse_launch_agents(core.setting(conn, "launch_agents"))
    if label not in {a for a, _ in agents}:
        # The first agent is the Start button's default: when its program is not on this computer (the
        # default Claude Code on a Mac that has only Codex), the agent added now becomes the default.
        first = core.entry_exe(agents[0][1]) if agents else None
        missing = first and core.PLATFORM != "win32" and not _login_shell_which([first])[first]
        agents = [(label, cmd)] + agents if missing else agents + [(label, cmd)]
        core.config_set(conn, "launch_agents", "; ".join(f"{a}={c}" for a, c in agents), actor=actor)
    return [a for a, _ in agents]


def setup_ntfy(conn, actor=None, open_=None):
    from . import notify
    return notify.setup_ntfy(conn, actor=actor, open_=open_)


# Operations the page may call. Each maps JSON args to one core function.
def _launch_args(a):
    """The launch dialog's choices: model, effort, and profile options (each optional)."""
    return {**{k: a.get(k) or None for k in ("model", "effort")},
            "options": {k: str(v) for k, v in (a.get("options") or {}).items()} or None}


def _page_reason(a, who):
    """The reason of a stop from the page: the person's words, else who stopped it."""
    return (a.get("reason") or "").strip() or f"stopped from the page by {who or 'a person'}"


# Requests to open a canvas in the page (#4783): maxpm canvas open asks this server, the state carries the request,
# and the page opens the canvas in the side panel of that session. In memory only; a request lives
# CANVAS_OPEN_KEEP seconds, and a page counts only the ones that came after it loaded.
CANVAS_OPENS = []
CANVAS_OPEN_KEEP = 120
_canvas_open_lock = threading.Lock()
_canvas_open_seq = [0]


def canvas_open(conn, args, who):
    """Put a canvas in front of the person: the page opens it in the side panel of the session named (default:
    the asker's own session), and shows it when the Sessions tab shows that session; else it offers a button."""
    c = core.canvas_show(conn, args["name"], args.get("project"))
    session = (args.get("session") or who or "").strip()
    if session and not conn.execute("SELECT 1 FROM agents WHERE name=?", (session,)).fetchone():
        raise RiverError(f"no agent {session}: name the session whose side panel shows the canvas (maxpm who)")
    with _canvas_open_lock:
        _canvas_open_seq[0] += 1
        entry = {"seq": _canvas_open_seq[0], "canvas": c["id"], "name": c["name"], "title": c["title"] or c["name"],
                 "version": c["version"], "session": session, "by": who, "at": time.time()}
        CANVAS_OPENS.append(entry)
        _prune_canvas_opens()
    return {"canvas": c["name"], "id": c["id"], "session": session, "seq": entry["seq"], "opened": True}


def _prune_canvas_opens():
    cut = time.time() - CANVAS_OPEN_KEEP
    CANVAS_OPENS[:] = [x for x in CANVAS_OPENS if x["at"] >= cut][-50:]


def canvas_opens():
    """The open requests of the last CANVAS_OPEN_KEEP seconds, for the page's state."""
    with _canvas_open_lock:
        _prune_canvas_opens()
        return [{k: v for k, v in x.items() if k != "at"} for x in CANVAS_OPENS]


DICTATE_OPS = ("dictate_start", "dictate_audio", "dictate_stop")

OPS = {
    "canvas_open": canvas_open,
    # Canvases (#4648): the same operations as maxpm canvas and the MCP tool canvas, for the page and for any
    # agent that speaks HTTP (POST /api/action {"op": "canvas_put", "args": {...}, "actor": "<agent>"}).
    "canvas_put": lambda c, a, who: core.canvas_put(c, a["name"], a.get("text"), who, a.get("kind"), a.get("title"),
                                                    a.get("item"), a.get("project"), a.get("note")),
    "canvas_kit": lambda c, a, who: core.canvas_kit(),
    "canvas_list": lambda c, a, who: core.canvas_list(c, a.get("project"), a.get("item"),
                                                      who if a.get("mine") else None, bool(a.get("deleted"))),
    "canvas_show": lambda c, a, who: core.canvas_show(c, a["name"], a.get("project"), a.get("version")),
    "canvas_comments": lambda c, a, who: core.canvas_comments(c, a["name"], who, bool(a.get("open")), a.get("project")),
    "canvas_comment": lambda c, a, who: core.canvas_comment(c, a["name"], a.get("text"), who, a.get("anchor"),
                                                            a.get("version"), a.get("project"), bool(a.get("send"))),
    "canvas_send": lambda c, a, who: core.canvas_send(c, a["name"], who, a.get("comments"), a.get("project")),
    "canvas_reply": lambda c, a, who: core.canvas_reply(c, a["comment"], a.get("text"), who),
    "canvas_resolve": lambda c, a, who: core.canvas_resolve(c, a["comment"], who, a.get("note")),
    "canvas_delete": lambda c, a, who: core.canvas_delete(c, a["name"], who, a.get("project")),
    "canvas_discard": lambda c, a, who: core.canvas_discard(c, a["comment"], who),
    "canvas_edit": lambda c, a, who: core.canvas_edit(c, a["name"], a.get("text"), who, a.get("project")),
    "canvas_section": lambda c, a, who: core.canvas_section(c, a["name"], a.get("heading"), a.get("text"), who,
                                                            a.get("project"), a.get("note"), a.get("pending")),
    "canvas_answer": lambda c, a, who: core.canvas_answer(c, a["name"], a.get("decision"), a.get("text"), who,
                                                          a.get("project")),
    # The page's Connect button for the relay: the device flow, finished in the background by maxpm serve.
    "relay_connect": lambda conn, a, who: relay.connect_from_page(bool(a.get("replace"))),
    "relay_disconnect": lambda conn, a, who: relay.disconnect(),
    "item_edit": lambda c, a, who: core.item_edit(c, a["id"], a.get("title"), a.get("notes"), a.get("doer"),
                                                  a.get("project"), who, a.get("context"), a.get("touches"),
                                                  a.get("check"), a.get("due"), goals=a.get("goals"),
                                                  untag=a.get("untag"), models=a.get("models"), tier=a.get("tier"),
                                                  short=a.get("short")),
    "prio": lambda c, a, who: core.item_prio(c, a["id"], a["priority"], who),
    "move": lambda c, a, who: core.item_move(c, a["id"], a.get("before"), a.get("after"), who),
    "dep_add": lambda c, a, who: core.dep_add(c, a["id"], [int(x) for x in a["on"]], who, a.get("kind", "blocks")),
    "dep_remove": lambda c, a, who: core.dep_remove(c, a["id"], [int(x) for x in a["on"]], who),
    "claim": lambda c, a, who: core.claim(c, a["id"], who),
    "accept": lambda c, a, who: core.accept(c, a["id"], who),
    "decline": lambda c, a, who: core.decline(c, a["id"], a.get("note"), who),
    "done": lambda c, a, who: core.done(c, a["id"], a.get("output"), who),
    "release": lambda c, a, who: core.release(c, a["id"], a.get("note"), who),
    "drop": lambda c, a, who: core.drop(c, a["id"], who),
    "reopen": lambda c, a, who: core.reopen(c, a["id"], who),
    "backlog": lambda c, a, who: core.backlog(c, a["id"], a.get("note"), who, a.get("force")),
    "activate": lambda c, a, who: core.activate(c, a["id"], who, a.get("note")),
    "block": lambda c, a, who: core.block(c, a["id"], a.get("reason"), who, a.get("until")),
    "unblock": lambda c, a, who: core.unblock(c, a["id"], who),
    "replanned": lambda c, a, who: core.replanned(c, a["id"], a.get("note"), who),
    "offer": lambda c, a, who: core.offer(c, a["body"], int(a["item"]), a.get("to"), who),
    "setup_block": lambda c, a, who: setup_block(c, a["path"], a.get("move")),
    "setup_maxpm_cmd": lambda c, a, who: install_command(),
    "folder_add": lambda c, a, who: folder_add(c, a.get("path"), a.get("name"), a.get("description") or "", a.get("move"), who),
    "setup_skills": lambda c, a, who: setup_skills(),
    "setup_agent_add": lambda c, a, who: setup_agent_add(c, a["label"], who),
    "setup_ntfy": lambda c, a, who: setup_ntfy(c, who, a.get("open")),
    # prompt: maxpm launch --prompt, which a command in a sandbox hands to maxpm serve.
    "launch_agent": lambda c, a, who: launch_agent(c, a.get("project"), agent=a.get("agent"), actor=who,
                                                   prompt=a.get("prompt"), **_launch_args(a)),
    "dispatch_item": lambda c, a, who: dispatch_item(c, int(a["id"]), agent=a.get("agent"), actor=who,
                                                     prompt=a.get("prompt"), **_launch_args(a)),
    "open_agent_on": lambda c, a, who: open_agent_on(c, int(a["id"]), agent=a.get("agent"), person=a.get("person"), actor=who,
                                                     **_launch_args(a)),
    "open_needs_you": lambda c, a, who: open_needs_you(c, agent=a.get("agent"), person=a.get("person"), **_launch_args(a)),
    "run_hooks": lambda c, a, who: {"started": hooks_soon(c, str(a["token"]), a.get("cwd"))},
    "queue_remove": lambda c, a, who: core.queue_remove(c, a["agent"], str(a["ref"]), who),
    # A person stops from the page without a reason; the agent then reads who stopped it.
    "stop_agent": lambda c, a, who: core.stop_agent(c, a["agent"], _page_reason(a, who), who),
    "kill_agent": lambda c, a, who: core.kill_agent(c, a["agent"], _page_reason(a, who), who),
    "start_manager": lambda c, a, who: start_manager(c, agent=a.get("agent"), actor=who, **_launch_args(a)),
    "start_expert": lambda c, a, who: start_expert(c, a["name"], a.get("topic"), a.get("project"), bool(a.get("resume")),
                                                   agent=a.get("agent"), actor=who, item=a.get("item"),
                                                   summary=a.get("summary"), sources=a.get("sources") or (),
                                                   canvases=a.get("canvases") or (), sessions=a.get("sessions") or (),
                                                   **_launch_args(a)),
    # Dictation (#5107): the page's audio goes to the helper on this Mac; each reply has the text so far. They touch
    # no queue (DICTATE_OPS).
    "dictate_start": lambda c, a, who: dictation.start(a.get("lang")),
    "dictate_audio": lambda c, a, who: dictation.audio(a["id"], a.get("pcm")),
    "dictate_stop": lambda c, a, who: dictation.stop(a["id"]),
    "session_pin": lambda c, a, who: core.session_pin(c, a["agent"], who, a.get("on", True) is not False),
    "expert_end": lambda c, a, who: core.expert_end(c, a["name"], who, a.get("reason"), bool(a.get("done"))),
    "expert_fresh": lambda c, a, who: fresh_expert(c, a["name"], who, new=a.get("new")),
    "terminal_keys": lambda c, a, who: terminal_keys(c, a["agent"], a.get("keys"), who),
    "serve_restart": lambda c, a, who: (_same_queue(a.get("db")), serve_restart(c))[1],
    "serve_status": lambda c, a, who: {"boot": BOOT, "stale": code_stale()},
    "update": lambda c, a, who: _no_update_in_app() or update_apply(),
    "restart": lambda c, a, who: _no_update_in_app() or (_restart_soon(), {"restarting": True})[1],
}


def _view_routes():
    """The GET routes and page actions of each view (river/routes/<view>.py, #4656)."""
    from .routes import artifacts, canvas, done, projects, sessions, settings, targets, terminal, you
    views = (you, projects, sessions, artifacts, targets, done, settings, canvas, terminal)
    for v in views:
        for key in v.OPS:
            assert key not in OPS, key
        OPS.update(v.OPS)
    return [r for v in views for r in v.GET]


def _message_read(conn, msg_id):
    """Marks one alert or note read, which closes its needs-you event. Questions stay open until answered."""
    core.message_show(conn, msg_id)  # refuses an unknown id
    with core.tx(conn):
        core._mark_read(conn, [msg_id])
    return core.message_show(conn, msg_id)


def needs_you_view(conn, human=None):
    """Open needs-you events with what a person needs to act: the item's context and notes, the message's id."""
    rows = core.needs_you(conn, human or None)
    for r in rows:
        if r["item_id"] is not None:
            it = conn.execute("SELECT context, notes, status FROM items WHERE id=?", (r["item_id"],)).fetchone()
            if it:
                r.update(item_context=it["context"], item_notes=it["notes"], item_status=it["status"])
    return rows


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet
        pass

    def _send(self, code, body, ctype="application/json", headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/mcp":
            return self._mcp("GET")
        if self._refuse():
            return
        page = page_html(path)
        if page is not None:
            return self._send(200, page, "text/html; charset=utf-8")
        for route in VIEW_GET:  # each view's own routes (river/routes/<view>.py, #4656)
            if route[0](path):
                return route[1](self, path)
        if path == "/api/state":
            # The page asks with ?since=<version>[&closed=1]: then the answer holds only what changed since that
            # version, without done items unless closed=1 (statedelta, #4709). Without since: the whole state.
            from urllib.parse import parse_qs, urlsplit
            q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query, keep_blank_values=True).items()}
            delta = "since" in q
            conn = core.connect()
            try:
                core.poll_activity(conn, None)  # a busy queue skips the sweep; the page still gets its state
                st = core.state(conn, closed=not delta or q.get("closed") == "1")
                st["goals"] = core.goal_list(conn, include_complete=True)
                if DEV["on"]:
                    st["dev_build"] = _build_id()
                st["page_build"] = page_build()
                st["state_shape"] = STATE_SHAPE
                st["desktop"] = DESKTOP
                st["dictation"] = dictation.engine()  # "mac": speech to text on this Mac (#5107); else the browser's
                st["terminals"] = agent_terminals(conn)  # the agents whose tmux pane the page can show
                st["tmux_done"] = tmux_done(conn, st["terminals"])  # the panes its Close button closes
                from . import sessions
                # The agents whose session the Sessions tab shows: the Board's Open session goes there (#4687).
                run = sessions.running(conn, st["terminals"])
                st["sessions"] = [a["name"] for a in run]
                # What each running session works on, the name the page shows for it (#4695).
                st["session_names"] = {a["name"]: sessions.work_name(conn, a) for a in run}
                st["relay"] = relay.status()
                st["canvas_opens"] = canvas_opens()  # maxpm canvas open: the page opens the canvas (#4783)
                st["loop"] = core.serve_loop(conn)  # the page says when the loop of this server is late
                if delta:
                    from . import statedelta
                    return self._send(200, statedelta.answer(statedelta.slim(st), q["since"] or None))
                return self._send(200, st)
            finally:
                conn.close()
        if path == "/api/terminal":
            from urllib.parse import parse_qs, urlsplit
            conn = core.connect()
            try:
                return self._send(200, terminal_screen(conn, parse_qs(urlsplit(self.path).query).get("agent", [""])[0]))
            except RiverError as e:
                return self._send(409, {"error": str(e)})
            finally:
                conn.close()
        if path.startswith("/api/item-pop/") and path.rsplit("/", 1)[-1].isdigit():  # the item pop-up (#4622)
            from . import sessions
            conn = core.connect()
            try:
                return self._send(200, sessions.item_session(conn, int(path.rsplit("/", 1)[-1])))
            except RiverError as e:
                return self._send(404, {"error": str(e)})
            finally:
                conn.close()
        if path == "/api/token":
            return self._send(200, {"token": PAGE_TOKEN})
        if path == "/api/search":
            # The search field of the page header: core.search, the one behind maxpm search (#2968).
            from urllib.parse import parse_qs, urlsplit
            q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            conn = core.connect()
            try:
                return self._send(200, core.search(conn, q.get("q", ""), project=q.get("project") or None,
                                                   status=q.get("status") or None, since=q.get("since") or None,
                                                   limit=max(1, min(int(q.get("n") or 50), 500)),
                                                   kinds=bool(q.get("kinds"))))
            except (RiverError, ValueError) as e:
                return self._send(400, {"error": str(e)})
            finally:
                conn.close()
        if path == "/api/update":
            if DESKTOP:  # the page hides the button when git is false
                return self._send(200, {"git": False, "desktop": True, "boot": BOOT})
            try:
                return self._send(200, {**update_status(fetch="fetch=0" not in self.path), "boot": BOOT,
                                        "stale": code_stale()})
            except RiverError as e:
                return self._send(409, {"error": str(e)})
        if path == "/api/agents":  # each agent's describe part, the parts it supports, and its setup texts (#5184)
            from . import agents
            return self._send(200, agents.page_list())
        if path == "/api/setup":
            conn = core.connect()
            try:
                return self._send(200, setup_status(conn))
            finally:
                conn.close()
        if path.startswith("/api/item/"):
            conn = core.connect()
            try:
                iid = int(path.rsplit("/", 1)[1])
                res = core.item_show(conn, iid)
                res["tree"] = core.blockers(conn, iid)
                res["messages"] = core.item_messages(conn, iid)
                return self._send(200, res)
            except (RiverError, ValueError) as e:
                return self._send(404, {"error": str(e)})
            finally:
                conn.close()
        if not path.startswith("/api/"):
            got = static_file(path)
            if got:
                return self._send(200, *got)
            if path.split("/")[1] in OLD_VIEWS:
                return self._send(404, OLD_VIEW_PAGE.encode(), "text/html; charset=utf-8")
        return self._send(404, {"error": "not found"})

    def _refuse(self):
        """Answer 403 and return True when the request was not made on this computer, straight to maxpm serve.
        maxpm serve has no sign-in, and the page starts agents, types into their terminals, and changes the
        queue. A site whose name is made to point at 127.0.0.1 (DNS rebinding) passes the browser's same-origin
        rule, so the Host header decides, on every route; a tunnel or a proxy that says it forwards gets
        nothing either (mcp.local_refusal). A river command in a sandbox (cli.ask_server) passes: the sandbox's
        proxy runs on this computer and adds no such header."""
        from . import mcp
        why = mcp.local_refusal(self.client_address[0], dict(self.headers.items()), "maxpm serve")
        if why:
            self._send(403, {"error": why})
        return bool(why)

    def _mcp(self, method):
        """/mcp: river's MCP tools over Streamable HTTP, for this computer only (mcp.local_refusal)."""
        from . import mcp
        why = mcp.local_refusal(self.client_address[0], dict(self.headers.items()))
        if why:
            return self._send(403, {"error": why})
        sid = self.headers.get("Mcp-Session-Id")
        if method == "GET":  # no stream of server messages: river sends none
            self.send_response(405)
            self.send_header("Allow", "POST, DELETE")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if method == "DELETE":
            return self._send(200 if mcp.http_delete(sid) else 404, {})
        code, reply, headers = mcp.http_post(self.rfile.read(int(self.headers.get("Content-Length", 0))), sid)
        data = b"" if reply is None else json.dumps(reply).encode()
        self.send_response(code)
        for k, v in headers.items():
            self.send_header(k, v)
        if data:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_DELETE(self):
        if self.path.split("?")[0] == "/mcp":
            return self._mcp("DELETE")
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.split("?")[0] == "/mcp":
            return self._mcp("POST")
        if self._refuse():
            return
        if self.path != "/api/action":
            return self._send(404, {"error": "not found"})
        # Same-origin check: of the pages on this computer, only river's own acts (a river command sends no Origin).
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin and origin not in (f"http://{host}",):
            return self._send(403, {"error": "cross-origin request refused"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            op = OPS.get(body.get("op"))
            if not op:
                return self._send(400, {"error": f"unknown op {body.get('op')!r}"})
            if body.get("op") in TYPING_OPS and not hmac.compare_digest(
                    self.headers.get("X-Maxpm-Token") or "", PAGE_TOKEN):
                return self._send(403, {"error": "typing into a terminal needs the page's token, and it is missing or "
                                                 "old: reload the page"})
            actor = body.get("actor") or None
            _same_queue(body.get("args", {}).get("db"))  # only a river command sends it (cli.ask_server)
            if body.get("op") in DICTATE_OPS:  # audio, a few times a second: no queue, no history (#5107)
                return self._send(200, {"ok": True, "result": op(None, body.get("args", {}), actor)})
            conn = core.connect()
            # The page shown through the relay (relay.handle_page): its actions are recorded as <name>@relay.
            via = core.EVENT_VIA.set("relay" if relay.page_request(self.headers) else None)
            try:
                core.activity(conn, actor)
                try:
                    result = op(conn, body.get("args", {}), actor)
                finally:
                    hooks_soon(conn, core.hook_token())  # a release event of this action: its hook runs after the answer
                with core.tx(conn):
                    core.sync_needs_you(conn)
                return self._send(200, {"ok": True, "result": result})
            finally:
                core.EVENT_VIA.reset(via)
                conn.close()
        except RiverError as e:
            return self._send(409, {"error": str(e)})
        except (KeyError, ValueError, TypeError) as e:
            return self._send(400, {"error": f"bad request: {e}"})


def _restart_on_change(httpd):
    """Dev mode: when a Python file changes, stop serving and start the process again."""
    code = {f: f.stat().st_mtime_ns for f in _code_files()}
    while True:
        time.sleep(1)
        now = {f: f.stat().st_mtime_ns for f in _code_files()}
        if now != code:
            print("code changed; restarting", flush=True)
            # Replace the process in place. Shutting down first would let the main thread exit before this
            # line runs.
            time.sleep(0.3)  # let an editor finish writing
            os.execv(sys.executable, _restart_argv())


class _Server(ThreadingHTTPServer):
    """HTTPServer.server_bind asks DNS for the host's full name (socket.getfqdn), which can hang for
    a long time on a Mac with a slow or missing network. The page needs no name: skip the lookup."""
    def server_bind(self):
        import socketserver
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def service_actions(self):
        try:
            keep_loop()  # serve_forever calls this twice a second
        except Exception as e:  # the page must answer also when no thread can start
            print(f"maxpm serve: the loop thread did not start: {e}", flush=True)


# The loop thread of maxpm serve (notify.loop), its stop event, and when it started (time.monotonic).
LOOP_THREAD = {"thread": None, "stop": None, "at": 0.0}
LOOP_RESTART_S = 30  # a loop thread that ended starts again, but not more often than this


def keep_loop():
    """Start the loop thread of maxpm serve when it does not run: at the start of serve, and again when the
    thread ended. No error of a pass ends it (notify.loop), but when it ended on 2026-10-08 the page still
    answered and serve did none of its automatic work for 3 hours (#1663). True when it started one."""
    t, stop = LOOP_THREAD["thread"], LOOP_THREAD["stop"]
    if stop is None or stop.is_set() or (t is not None and t.is_alive()):
        return False
    if t is not None:
        if time.monotonic() - LOOP_THREAD["at"] < LOOP_RESTART_S:
            return False
        print("maxpm serve: the loop thread ended; it starts again", flush=True)
    from . import notify
    LOOP_THREAD.update(at=time.monotonic(), thread=threading.Thread(
        target=notify.loop, args=(stop,), daemon=True, name="maxpm-notify"))
    LOOP_THREAD["thread"].start()
    return True


def serve(port: int, open_browser=False, dev=False):
    httpd = _Server(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    DEV["on"] = dev
    print(f"MaximizePM on {url} (database {core.db_path()}){' [dev: restarts on code change]' if dev else ''}",
          flush=True)
    if dev:
        threading.Thread(target=_restart_on_change, args=(httpd,), daemon=True).start()
    from . import notify
    notify.SERVE_PORT["port"] = port
    stop = threading.Event()
    LOOP_THREAD.update(thread=None, stop=stop)
    keep_loop()
    relay.start(stop, port)  # keeps the relay socket open while relay.json exists (maxpm connect)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()


VIEW_GET = _view_routes()
