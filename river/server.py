"""Local web page for MaximizePM. Binds to 127.0.0.1 only."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import core, relay
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
                ".png": "image/png", ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8",
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


def _watched():
    """The server's code and every page file, in subfolders too (components/); vendor/ never changes by hand."""
    return sorted(PKG.glob("*.py")) + sorted(
        f for f in STATIC.rglob("*") if f.is_file() and "vendor" not in f.relative_to(STATIC).parts[:1]
        and not any(part.startswith(".") for part in f.relative_to(STATIC).parts))


def _build_id():
    """Changes whenever a watched file changes; the page reloads itself in dev mode."""
    return str(max((f.stat().st_mtime_ns for f in _watched()), default=0))


def _code_id():
    """Changes when the server's Python code changes (git pull, a commit, an edit). The page files are
    read from disk on every request, but the code runs as it was when the server started."""
    return str(max((f.stat().st_mtime_ns for f in PKG.glob("*.py")), default=0))


BOOT_CODE = _code_id()


def code_stale():
    """True when the river code on disk is newer than the code this server runs: it needs a restart."""
    return _code_id() != BOOT_CODE

def _applescript_str(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# Tests (and maxpm launch tests) set this to a fake that receives what would open a terminal.
TERMINAL_RUNNER = None


def _can_open_terminal(runner, hint, launch_in=None):
    if launch_in == "tmux":  # no Terminal app needed: any system with tmux, over SSH too
        if TMUX_RUNNER is None and not _tmux_cmd():
            raise RiverError("launch_in is tmux, and tmux is not installed: brew install tmux (Linux: apt install tmux), "
                             "or use a Terminal tab: maxpm config set launch_in tab")
        return
    runner = runner or TERMINAL_RUNNER
    if core.PLATFORM not in ("darwin", "win32") and runner is None:
        raise RiverError(f"starting an agent from the page works on macOS and Windows only; start one yourself: {hint}")


def launch_agent(conn, project=None, runner=None, agent=None, actor=None, model=None, effort=None, launch_in=None,
                 options=None, prompt=None):
    """Open a Terminal window in the project folder of the most important ready agent item and run
    the command of the chosen launch_agents entry there (default: the Claude Code profile), so one click starts one
    agent session. macOS and Windows. prompt (maxpm launch --prompt): the new session's first instruction,
    after the profile's prompt."""
    # With no project named, Start spreads sessions: first a project with ready work and no agent yet.
    # A session that waits for work in that project (maxpm wait) gets the item: no new session needed.
    # Else the new session gets a name and the item is pushed to it, so the next Start sees the project covered.
    # A custom prompt always opens a new session: a waiting one would get it only second-hand, as a message.
    t = core.launch_target(conn, project, agent, None, model, effort, launch_in, spread=project is None, options=options,
                           prompt=prompt)
    waiting = None if t["custom_prompt"] else core.waiting_agent_for(conn, t["project"], t["item"]["id"])
    if waiting:
        core.push(conn, t["item"]["id"], waiting, "from the Start button: you were waiting for work", actor)
        return {**t, "pushed_to": waiting}
    _can_open_terminal(runner, "cd <project folder> && claude go", t["launch_in"])
    return _start_for_item(conn, t, runner, actor, "Start opened this session for it")


def dispatch_item(conn, item_id, runner=None, agent=None, actor=None, model=None, effort=None, launch_in=None,
                  options=None, prompt=None):
    """Start work on one ready item: a session that waits for work in its project gets it (push);
    else river names a new session, reserves the item for it (push), and opens the chosen agent in the
    project folder with MAXPM_AGENT set to that name and MAXPM_FOCUS=item:<id>, so its first maxpm go takes it.
    With a custom prompt (maxpm launch --prompt) it always opens a new session."""
    t = core.launch_target(conn, agent=agent, item=item_id, model=model, effort=effort, launch_in=launch_in,
                           options=options, actor=actor, prompt=prompt)
    waiting = None if t["custom_prompt"] else core.waiting_agent_for(conn, t["project"], t["item"]["id"])
    if waiting:
        core.push(conn, t["item"]["id"], waiting, "from the page: Dispatch; you were waiting for work", actor)
        return {**t, "pushed_to": waiting}
    _can_open_terminal(runner, f"cd <project folder> && MAXPM_FOCUS=item:{t['item']['id']} claude go", t["launch_in"])
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
                  launch_in=None, options=None):
    """Open an agent session for one item from its drawer. A ready item: Dispatch. A person's item: a
    session that does it together with the person. An item that waits: a session that first takes what
    blocks it. The session learns which from MAXPM_FOCUS, which its maxpm go reads."""
    it = core.item_show(conn, item_id)
    if it["status"] not in core.OPEN_STATES:
        raise RiverError(f"#{item_id} is {it['status']}; there is nothing to open an agent on")
    mine = person and it["assignee"] == person  # a person claimed it (Claim next): help them do it
    if it["status"] == "in_progress" and not mine:
        raise RiverError(f"#{item_id} is in progress by {it['assignee']}; message them instead")
    if it["doer"] == "human" or mine:
        focus = f"help:{it['id']}" + (f"@{person}" if person else "")
    elif it["ready"]:
        return dispatch_item(conn, it["id"], runner, agent, actor, model, effort, launch_in, options)
    else:
        focus = f"unblock:{it['id']}"
    p = core._project(conn, it["project"])
    if not p["path"]:
        raise RiverError(f"project {p['name']} has no folder, so MaximizePM cannot start a session there: "
                         f"maxpm project path {p['name']} <folder>")
    return {**_open_focused(conn, p, focus, runner, agent, model, effort, launch_in, options),
            "item": {"id": it["id"], "title": it["title"]}}


def open_needs_you(conn, runner=None, agent=None, person=None, model=None, effort=None, launch_in=None, options=None):
    """Needs you, Open agent: a session that works through everything that waits on the person, with
    them (the Copy prompt text), in the folder of the most important such item's project."""
    ann = core.annotate(conn)
    items = sorted((a for a in ann.values() if a["ready"] and a["doer"] == "human" and not a["project_archived"]),
                   key=lambda a: a["sort_key"])
    projects = [core._project(conn, a["project"]) for a in items] + [
        core._project(conn, p["name"]) for p in core.project_list(conn)]
    p = next((x for x in projects if x["path"]), None)
    if p is None:
        raise RiverError("no project has a folder, so MaximizePM cannot start a session: maxpm project path <name> <folder>")
    return _open_focused(conn, p, "needs:" + (f"@{person}" if person else ""), runner, agent, model, effort, launch_in,
                         options)


def deploy_now(conn, target, review=False, runner=None, agent=None, actor=None, model=None, effort=None,
               launch_in=None, options=None):
    """Targets tab, Deploy now (or Review and deploy): when the deploy item (or its review) is ready and
    the target has no owner to alert, open an agent in a folder of the target's projects that takes it."""
    r = core.deploy_now(conn, target, review, actor)
    if not r["ready"] or r.get("alerted"):
        return r
    p = _target_folder(conn, target)
    return {**r, **_open_focused(conn, p, f"{'review' if review else 'deploy'}:{target}", runner, agent,
                                 model, effort, launch_in, options)}


def _target_folder(conn, target):
    """A folder to start a session for a target: the first of its projects that has one."""
    p = next((core._project(conn, x["name"]) for x in core.target_show(conn, target)["projects"]
              if core._project(conn, x["name"])["path"]), None)
    if p is None:
        raise RiverError(f"no project of target {target} has a folder, so MaximizePM cannot start a session there: "
                         f"maxpm project path <name> <folder>")
    return p


def _same_queue(db):
    """A river command that asks the server to act (open a monitor, or start a session for a command in a
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


def open_monitors(conn, runner=None, db=None):
    """Open a session for each monitor item nobody holds yet (a deploy just started): in a folder of the
    target's projects, with MAXPM_FOCUS=monitor:<id>, the item's model and effort (a monitor defaults to
    sonnet, low). The command asks the running server for this after it claims a deploy item; db must name
    this server's queue (_same_queue)."""
    _same_queue(db)
    out = []
    ann = None
    for m in core.pending_monitors(conn):
        ann = ann or core.annotate(conn)
        it = ann[m["id"]]
        try:
            p = _target_folder(conn, m["target"])
            agent, opt = _agent_for(conn, it["model"])
            model = it["model"] if any(x["name"] == it["model"] for x in opt["models"]) else None
            effort = it["effort"] if it["effort"] in opt["efforts"] else None
            t = _open_focused(conn, p, f"monitor:{m['id']}", runner, agent, model, effort)
        except RiverError as e:
            out.append({"id": m["id"], "error": str(e)})
            continue
        with core.tx(conn):
            core._event(conn, m["id"], "maxpm", f"monitor session opened ({t['agent']} in {p['name']})")
        out.append({"id": m["id"], "agent": t["agent"], "project": p["name"], "model": model})
    return out


def _open_focused(conn, p, focus, runner, agent, model=None, effort=None, launch_in=None, options=None):
    """Open the chosen agent in a project folder with MAXPM_FOCUS set; its maxpm go reads it."""
    launch_in = core._launch_in(conn, p["id"], launch_in)
    _can_open_terminal(runner, f"cd {p['path']}, set MAXPM_FOCUS={focus}, then claude go", launch_in)
    name = core.focus_title(conn, focus)
    t = {"project": p["name"], "path": p["path"], "focus": focus, "session_title": name,
         **core._launch_agent_cmd(conn, p["id"], agent, model, effort, options, name),
         "launch_in": launch_in}
    _open_terminal(t, {"MAXPM_FOCUS": focus, **t["env"]}, runner)
    return t


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


def start_manager(conn, runner=None, agent=None, actor=None, model=None, effort=None, launch_in=None, options=None):
    """Start manager (the page's Manager section): the chosen agent with manage in place of go, in the folder
    of the first project that has one, compacted at manager_autocompact. Refuses while a manager is active."""
    import secrets
    other = core.active_manager(conn)
    if other:
        raise RiverError(f"{other} is the active manager; open its chat instead")
    p = next((core._project(conn, x["name"]) for x in core.project_list(conn) if x.get("path")), None)
    if p is None:
        raise RiverError("no project has a folder, so MaximizePM cannot start a session: maxpm project path <name> <folder>")
    launch_in = core._launch_in(conn, p["id"], launch_in)
    _can_open_terminal(runner, f"cd {p['path']} && claude manage", launch_in)
    t = {"project": p["name"], "path": p["path"], "session_title": "maxpm manager",
         **core._launch_agent_cmd(conn, p["id"], agent, model, effort, options, "maxpm manager",
                                  autocompact=core.autocompact_tokens(core.setting(conn, "manager_autocompact"))),
         "launch_in": launch_in}
    t["command"] = manage_command(t["command"])
    name = f"manager-{secrets.token_hex(2)}"
    core.register(conn, name, note="started from the page as the manager")
    core._set_role_note(conn, name, "manager", None)  # the next Start manager sees it at once
    _open_terminal(t, {"MAXPM_AGENT": name, **t["env"]}, runner)
    return {**t, "session_name": name}


def open_chat(conn, agent, runner=None):
    """Bring the person into an agent's chat: its web link when the session has one (Claude Code with
    --remote-control); else, on macOS, the Terminal tab that runs its process (by the tty of the PID river
    recorded); else a hint that says why and what to do."""
    import subprocess
    a = core.agent_status(conn, agent)
    if a.get("session_url"):
        return {"agent": agent, "url": a["session_url"]}
    why = ("it has no web link: Remote Control is off for Claude Code (maxpm config set claude_remote_control on)"
           if core.setting(conn, "claude_remote_control") == "off"
           else "it has no web link (start Claude Code with --remote-control for one)")
    if a.get("pid") and a.get("host") == core.this_host() and core.pid_alive(a["pid"]):
        try:
            tty = subprocess.run(["ps", "-o", "tty=", "-p", str(a["pid"])], capture_output=True, text=True,
                                 timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            tty = ""
        if tty and tty not in ("??", "?") and (core.PLATFORM == "darwin" or runner or TERMINAL_RUNNER):
            dev = tty if tty.startswith("/dev/") else "/dev/" + tty
            pane = _tmux_pane_of(dev)
            if pane:
                return {"agent": agent, "tmux_pane": pane["pane"],  # the page opens its terminal view
                        "hint": f"{agent} runs in tmux ({pane['name'] or pane['pane']}): `maxpm view` in a terminal shows it."}
            script = "\n".join([
                'tell application "Terminal"',
                '  repeat with w in windows',
                '    repeat with t in tabs of w',
                f'      if tty of t is {_applescript_str(dev)} then',
                '        set selected of t to true', '        set index of w to 1', '        activate',
                '        return "ok"', '      end if', '    end repeat', '  end repeat', 'end tell', 'return "none"'])
            run = runner or TERMINAL_RUNNER or (lambda sc: subprocess.run(
                ["osascript", "-e", sc], capture_output=True, text=True, timeout=10).stdout)
            try:
                got = run(script)
            except (OSError, subprocess.SubprocessError) as e:
                got = str(e)
            if (got or "").strip() == "ok":
                return {"agent": agent, "focused": True, "tty": dev}
            why = f"no Terminal tab runs {dev} (it may run in another terminal app)"
        else:
            why = "its process has no terminal (a background session)"
    elif a.get("pid"):
        why = f"its process runs on {a.get('host')}, not here" if a.get("host") != core.this_host() else "its process has ended"
    return {"agent": agent, "hint": f"No chat to open for {agent}: {why}."
            + (f" Its Claude Code session: {a['session']}." if a.get("session") else "")}


def _open_terminal(t, env, runner=None):
    """Run the agent command (t["command"]) in the project folder (t["path"]) with env set: in a new
    Terminal tab or window on macOS (launch_in), in a new console window on Windows, or with launch_in
    tmux in a pane of the maxpm tmux session on any system. runner (tests) gets the AppleScript on macOS,
    and {"args", "cwd", "env"} on Windows; TMUX_RUNNER gets the tmux commands."""
    import os
    import shlex
    import subprocess
    runner = runner or TERMINAL_RUNNER
    # The agent uses the same queue as this page: a page on a MAXPM_DB queue starts agents on it too.
    if os.environ.get("MAXPM_DB"):
        env = {"MAXPM_DB": str(core.db_path()), **env}
    if t["launch_in"] == "tmux":
        t["tmux_pane"] = _tmux_open(t, env, f"cd {shlex.quote(t['path'])} && "
                                    + "".join(f"{k}={shlex.quote(v)} " for k, v in env.items()) + t["command"])
        return
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
    # The tab or window gets the session's name as its title until the agent CLI sets its own (Claude Code
    # shows the --name it got; other CLIs may keep this one).
    title = f"printf '\\033]0;%s\\007' {shlex.quote(t['session_title'])}; " if t.get("session_title") else ""
    shell = (title + f"cd {shlex.quote(t['path'])} && " + "".join(f"{k}={shlex.quote(v)} " for k, v in env.items())
             + t["command"])
    cmd = _applescript_str(shell)
    if t["launch_in"] == "tab":
        # Terminal has no "new tab" command: press Command-T in it, then run the command in that tab.
        # A key press goes to the app in front, so wait until Terminal is in front (else Command-T opens a
        # browser tab). Then wait until the new tab exists (else the command runs in the old tab, which
        # can hold a busy agent). Terminal's AppleScript lists each tab of a tabbed window as a window of
        # its own, so a new tab shows as one more window, or as one more tab of the front window. Pressing keys needs the Accessibility permission once. Without it, or
        # when no new tab appears, fall back to a new window.
        script = "\n".join([
            'tell application "Terminal"', '  activate', '  set hasWindow to (count of windows) > 0', 'end tell',
            'if hasWindow then', '  try',
            '    tell application "System Events"',
            '      repeat 40 times',
            '        if frontmost of process "Terminal" then exit repeat',
            '        delay 0.05',
            '      end repeat',
            '      if not (frontmost of process "Terminal") then error "Terminal is not in front"',
            '    end tell',
            '    tell application "Terminal" to set {windowsBefore, tabsBefore} to {count of windows, count of tabs of front window}',
            '    tell application "System Events" to tell process "Terminal" to keystroke "t" using command down',
            '    set gotTab to false',
            '    repeat 60 times',
            '      delay 0.05',
            '      tell application "Terminal" to set gotTab to (count of windows) > windowsBefore or (count of tabs of front window) > tabsBefore',
            '      if gotTab then exit repeat',
            '    end repeat',
            '    if not gotTab then error "Terminal opened no new tab"',
            f'    tell application "Terminal" to do script {cmd} in selected tab of front window',
            '  on error', f'    tell application "Terminal" to do script {cmd}', '  end try',
            'else', f'  tell application "Terminal" to do script {cmd}', 'end if'])
    else:
        script = f'tell application "Terminal"\n  activate\n  do script {cmd}\nend tell'
    try:
        (runner or (lambda s: subprocess.run(["osascript", "-e", s], check=True, capture_output=True,
                                             text=True, timeout=20)))(script)
    except (OSError, subprocess.SubprocessError) as e:
        why = (getattr(e, "stderr", "") or str(e)).strip()
        raise RiverError(f"could not open Terminal: {why}. macOS may ask once to let MaximizePM control Terminal "
                         f"(System Settings, Privacy & Security, Automation)")


# launch_in tmux: each agent river starts is a pane of one tmux session, so one terminal shows them all
# (maxpm view), over SSH too, and nothing needs AppleScript. TMUX_CMD (tests) is a tmux command line with a
# server of its own; TMUX_RUNNER (tests) gets each argument list in its place and returns the output, or
# None for a command that fails.
TMUX_CMD = None
TMUX_SESSION = "maxpm"
TMUX_RUNNER = None
TMUX_SHELLS = {"sh", "bash", "zsh", "fish", "dash", "ksh", "csh", "tcsh", "nu"}
# One line per pane: the free text (the session's name) comes last.
_TMUX_PANE = "#{pane_id}|#{window_id}|#{@maxpm_tile}|#{window_panes}|#{pane_current_command}|#{pane_tty}|#{@maxpm_agent}|#{@maxpm_started}|#{@maxpm_name}"
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
        f = line.split("|", 8)
        if len(f) == 9:
            shell = f[4].lstrip("-") in TMUX_SHELLS
            starting = shell and f[7].isdigit() and t - int(f[7]) < START_GRACE
            rows.append({"pane": f[0], "window": f[1], "tile": f[2] == "1", "window_panes": int(f[3] or 1),
                         "running": f[4].lstrip("-"), "ended": shell and not starting, "starting": starting,
                         "tty": f[5], "agent": f[6] or None, "name": f[8]})
    return rows


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
    """The last lines of a pane's text (no blank ones): where a prompt shows."""
    return [x.rstrip() for x in text.splitlines() if x.strip()][-PROMPT_LINES:]


def _done_panes(conn, panes, terminals=None):
    """{pane: why} for the panes whose session is done, which maxpm view --tidy closes. A pane is done when
    only a shell runs there: the agent CLI ended (a pane river opened less than START_GRACE ago still starts).
    With the queue (conn) a pane is also done while the agent CLI
    stays open and idle, when the agent river named for it takes no work any more: it is not registered (its
    maxpm wait ended, or it unregistered), it ended after a stop (or ran no river command for away_after
    after one), or it is gone, and it holds and owns nothing.
    A registered agent whose process runs in the pane decides in place of the named one (a session that began
    again under another name). A pane that shows a prompt (prompt_pattern) is never done by the queue's word:
    a person may want to answer it. A pane with no agent name (a session for a person's item) is done only
    by its shell. terminals: agent_terminals(conn), when the caller has it."""
    import re
    done = {p["pane"]: "its agent ended" for p in panes if p["ended"]}
    if conn is None:
        return done
    here = {}
    for agent, pane in (agent_terminals(conn) if terminals is None else terminals).items():
        here.setdefault(pane, []).append(agent)
    maybe = {}
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
        else:
            maybe[p["pane"]] = why
    pattern = core.setting(conn, "prompt_pattern").strip()
    texts = _pane_texts(sorted(maybe)) if pattern else dict.fromkeys(maybe, "")
    rx = re.compile(pattern) if pattern else None
    for pane, why in maybe.items():
        if pane in texts and not (rx and any(rx.search(x) for x in _prompt_tail(texts[pane]))):
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
    """The agents river started in tmux (launch_in tmux), for maxpm view. layout "tile" puts every agent
    pane side by side in one window, and new agents then join it; "windows" gives each agent a window of
    its own again; None changes nothing. tidy first closes the panes whose session is done (_done_panes:
    only a shell runs there, or, with conn, the agent takes no work any more). Panes that a person made are
    left alone. Returns the panes (each with "done": why --tidy closes it, or None), the panes it closed, and
    the tmux command that shows the session: attach, or switch-client inside tmux."""
    import os
    target = "=" + TMUX_SESSION
    if _tmux("has-session", "-t", target, check=False) is None:
        raise RiverError(f"no agent runs in tmux (there is no tmux session {TMUX_SESSION!r}). Start agents there: "
                         f"maxpm launch --tmux, or for every start: maxpm config set launch_in tmux")
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
    pids = {str(a["pid"]): a["name"] for a in agents if a["pid"] and a["host"] == core.this_host()}
    if pids and core.PLATFORM != "win32":
        try:
            ps = (PS_RUNNER or (lambda p: subprocess.run(["ps", "-o", "pid=,tty=", "-p", ",".join(p)], capture_output=True,
                                                         text=True, timeout=5).stdout))(sorted(pids))
        except (OSError, subprocess.SubprocessError):
            ps = ""
        by_tty = {p["tty"]: p["pane"] for p in panes}
        for line in ps.splitlines():
            pid, _, tty = line.strip().partition(" ")
            tty = tty.strip()
            pane = by_tty.get(tty if tty.startswith("/dev/") else "/dev/" + tty)
            if pane and pid in pids:
                out[pids[pid]] = pane
    for p in panes:
        if p["agent"] and p["pane"] not in out.values() and any(a["name"] == p["agent"] for a in agents):
            out.setdefault(p["agent"], p["pane"])
    return out


PS_RUNNER = None  # tests: gets the pids, returns the lines "pid tty"


def terminal_screen(conn, agent):
    """What an agent's tmux pane shows now, for the page: the lines with their colours (escape sequences),
    the size, and the cursor. One tmux command."""
    pane = agent_terminals(conn).get(agent)
    if not pane:
        raise RiverError(f"{agent} does not run in a tmux pane on this computer (start agents there: maxpm config set launch_in tmux)")
    return _pane_screen(agent, pane)


def _pane_screen(agent, pane):
    out = _tmux("display-message", "-p", "-t", pane,
                "#{pane_width}|#{pane_height}|#{cursor_x}|#{cursor_y}|#{pane_current_command}|#{@maxpm_name}",
                ";", "capture-pane", "-p", "-e", "-t", pane)
    head, _, text = out.partition("\n")
    f = head.split("|", 5)
    if len(f) != 6:
        raise RiverError(f"tmux gave no answer for pane {pane}")
    return {"agent": agent, "pane": pane, "width": int(f[0]), "height": int(f[1]), "cursor": [int(f[2]), int(f[3])],
            "ended": f[4].lstrip("-") in TMUX_SHELLS, "name": f[5], "text": text}


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


def watch_prompts(conn):
    """One look at every agent's tmux pane (the notify loop of maxpm serve, every notify_interval): when one of
    the last lines matches the setting prompt_pattern and those lines stay the same for prompt_wait, each
    person gets an alert from that agent (core.PROMPT_NOTE and the line), which opens a needs-you event; the
    page shows the agent's Terminal for it. When the prompt is gone (answered, or the agent ended), river marks
    the alert read, which closes the event. A prompt that stays on the screen alerts once. Returns
    {"waiting": {agent: line}, "told": [agents], "closed": [message ids]}."""
    import re
    pattern = core.setting(conn, "prompt_pattern").strip()
    waiting = {}
    if pattern:
        rx = re.compile(pattern)
        for agent, pane in sorted(agent_terminals(conn).items()):
            try:
                screen = _pane_screen(agent, pane)
            except RiverError:
                continue  # the pane closed since the list
            if screen["ended"]:
                continue  # a shell: what the agent printed stays, and nobody waits there
            tail = _prompt_tail(_plain(screen["text"]))
            line = next((x.strip() for x in tail if rx.search(x)), None)
            if line:
                waiting[agent] = (f"{core.PROMPT_NOTE}: {line[:200]}", "\n".join(tail))
    t, wait = core.now(), core.parse_duration(core.setting(conn, "prompt_wait"))
    for agent in [a for a in PROMPTS if a not in waiting]:
        del PROMPTS[agent]
    told = []
    alerts = [dict(r) for r in conn.execute(
        "SELECT id, from_agent, body FROM messages WHERE kind='alert' AND read_at IS NULL AND body LIKE ?",
        (core.PROMPT_NOTE + "%",))]
    closed = [m["id"] for m in alerts if waiting.get(m["from_agent"], ("",))[0] != m["body"]]
    if not waiting and not closed:
        return {"waiting": {}, "told": [], "closed": []}  # the usual pass: nothing to write
    with core.tx(conn):
        core._mark_read(conn, closed)
        people = [r["name"] for r in conn.execute("SELECT name FROM agents WHERE kind='human' ORDER BY name")]
        for agent, (body, sig) in waiting.items():
            st = PROMPTS.get(agent)
            if not st or st["sig"] != sig:
                st = PROMPTS[agent] = {"sig": sig, "since": t, "told": st["told"] if st else None}
            if any(m["from_agent"] == agent and m["id"] not in closed for m in alerts):
                st["told"] = body  # also after maxpm serve started again: the alert is there
            if st["told"] == body or t - st["since"] < wait:
                continue
            held = conn.execute("SELECT id FROM items WHERE assignee=? AND status IN ('in_progress','held') ORDER BY id",
                                (agent,)).fetchone()
            for person in people:
                core._send(conn, "alert", agent, body, to=person, item_id=held and held["id"])
            if people:  # with no person in the queue yet, the first one who registers gets it
                st["told"] = body
                told.append(agent)
        if told or closed:
            core.sync_needs_you(conn)
    return {"waiting": {a: b[len(core.PROMPT_NOTE) + 2:] for a, (b, _) in waiting.items()}, "told": told, "closed": closed}


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
    pattern = core.setting(conn, "prompt_pattern").strip()
    rx = re.compile(pattern) if pattern else None
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
        if screen["ended"] or (rx and any(rx.search(x) for x in _prompt_tail(text))):
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
    pattern = core.setting(conn, "prompt_pattern").strip()
    rx = re.compile(pattern) if pattern else None
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
        if (agent in BUSY and BUSY[agent] != text) or (rx and any(rx.search(x) for x in _prompt_tail(text))):
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
            label = _agent_for(conn, w["model"])[0] if w["model"] else None
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
    item for it, and opens the agent in the project folder (launch_in). The item's notes are its context."""
    it = core.annotate(conn)[item_id]
    p = core._project(conn, it["project"])
    label = core.agent_for_type(conn, it["agent"], p["id"]) if it["agent"] else _agent_for(conn, it["model"])[0]
    opt = next((o for o in core.launch_options(conn) if o["label"] == label), None)
    model = it["model"] if opt and any(x["name"] == it["model"] for x in opt["models"]) else None
    effort = it["effort"] if opt and it["effort"] in opt["efforts"] else None
    t = core.launch_target(conn, agent=label, item=item_id, model=model, effort=effort)
    _can_open_terminal(runner, f"cd {t['path']} && MAXPM_FOCUS=item:{item_id} claude go", t["launch_in"])
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
    Managers and planners are left alone: they wait at their prompt for a person or a watch command.
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
                          "AND COALESCE(role, '') NOT IN ('manager', 'planner')").fetchall():
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
    launch_in = core._launch_in(conn, p["id"])
    _can_open_terminal(runner, f"cd {p['path']}, set MAXPM_FOCUS={focus}, then claude go", launch_in)
    # Each deployer gets a session name of its own (claude --name, --remote-control): the one it replaces may still
    # run, and #1010 saw both starts beside a live session of the same name end before they connected.
    tag = secrets.token_hex(2)
    title = f"{core._short_title(core.focus_title(conn, focus), 43)} {tag}"
    t = {"project": p["name"], "path": p["path"], "focus": focus, "session_title": title,
         **core._launch_agent_cmd(conn, p["id"], label, model, effort, None, title), "launch_in": launch_in}
    name = f"deploy-{target}-{tag}"
    core.register(conn, name, note=f"{core.STARTED_NOTE} #{dep_id}")
    with core.tx(conn):
        core.target_handoff(conn, target, name, why)
    _open_terminal(t, {"MAXPM_AGENT": name, "MAXPM_FOCUS": focus, **t["env"]}, runner)
    return {**t, "session_name": name}


def auto_release(conn, runner=None):
    """One pass of the loop of maxpm serve (every notify_interval): releases move with no manager.
    - A ready release review that nobody holds or has reserved (auto_review on): a session that waits for work in
      a project of the release and worked on nothing in it (release_authors) gets it pushed; else a new session.
    - A ready deploy item nobody holds, by its target's deployer mode (core.DEPLOYER_MODES): the target owner gets
      one alert when it can take it (core.owner_can_deploy); with no owner, or one that cannot, a new deployer
      session gets the target. With standing, that session starts while the release still waits on its review.
    No session starts while max_sessions agent sessions are live. Returns {"pushed": {item: agent},
    "started": {item: session}, "alerted": {item: owner}, "failed": {item: why}}."""
    out = {"pushed": {}, "started": {}, "alerted": {}, "failed": {}}
    ann = None
    cap = int(core.setting(conn, "max_sessions") or 0)

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
                          f"and you worked on nothing in it", "maxpm")
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
                           f"WHERE i.kind='deploy' AND i.status IN {core.OPEN_STATES} AND t.deployer<>'off' "
                           "ORDER BY i.id").fetchall()
    seen = set()
    for d in deploys:
        if d["target"] in seen:
            continue  # one deploy item at a time for each target: the first open one
        seen.add(d["target"])
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
    row = conn.execute("SELECT kind FROM agents WHERE name=?", (who,)).fetchone() if who else None
    if row and row["kind"] != "human":
        raise RiverError("only a person types into an agent's terminal")
    pane = agent_terminals(conn).get(agent)
    if not pane:
        raise RiverError(f"{agent} does not run in a tmux pane on this computer")
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
    return {"agent": agent, "pane": pane, "sent": len(keys)}


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
        return bool(_git(REPO, "status", "--porcelain", "--", str(PKG), timeout=20))
    except RiverError:
        return False


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
    from .cli import AGENT_SNIPPET, _imports_agents
    if not path.exists():
        return "missing"
    text = path.read_text(errors="replace")
    if path.name == "CLAUDE.md" and _imports_agents(text):
        return _block_state(path.with_name("AGENTS.md"))  # Claude Code reads AGENTS.md through the import
    return "current" if AGENT_SNIPPET in text else "old" if core.names_product(text) else "missing"


def setup_status(conn):
    """What the setup guide shows: each check, and whether it is done."""
    import shutil
    from .cli import instructions_layout
    folders = {}
    for p in core.project_list(conn):
        if p["path"]:
            folders.setdefault(p["path"], []).append(p["name"])
    skills = Path("~/.claude/skills").expanduser()
    agents = core.parse_launch_agents(core.setting(conn, "launch_agents"))
    have = {label for label, _ in agents}
    # Agent programs as a new terminal finds them (the app itself has only Finder's short PATH).
    # Agents start in a new Terminal window, so that is where their programs must be found (on Windows: PATH).
    exes = sorted({exe for _, exe, _ in KNOWN_AGENTS} | {core.entry_exe(c) for _, c in agents})
    term = _login_shell_which(exes) if core.PLATFORM != "win32" else {e: shutil.which(e) for e in exes}
    return {
        "done": core.setting(conn, "setup_done") == "on",
        "folders": [{"path": d, "projects": names, "exists": Path(d).is_dir(),
                     "claude_md": _block_state(Path(d, "CLAUDE.md")), "agents_md": _block_state(Path(d, "AGENTS.md")),
                     "layout": instructions_layout(d) if Path(d).is_dir() else None}
                    for d, names in folders.items()],
        "projects_without_folder": [p["name"] for p in core.project_list(conn) if not p["path"]],
        "people": [r["name"] for r in conn.execute("SELECT name FROM agents WHERE kind='human' ORDER BY name")],
        "claude_home": Path("~/.claude").expanduser().is_dir(),
        "skills": {n: ("installed" if (skills / n / "SKILL.md").is_file() else "missing") for n in ("maxpm", "maxpm-planner")},
        "launch_agents": [label for label, _ in agents],
        "launch_in": core._launch_in(conn, None),
        # Each Start button agent, and whether its program is on this computer (the first word of its command).
        "start_agents": [{"label": label, "found": bool(term.get(core.entry_exe(cmd)))} for label, cmd in agents],
        "agent_clis": [{"label": label, "found": bool(term.get(exe)), "added": label in have,
                        "command": _shown_cmd(conn, cmd)} for label, exe, cmd in KNOWN_AGENTS],
        # The options of each launch profile the Start button uses, for toggles (Settings > Setup).
        "launch_profiles": core.launch_profiles(conn),
        "notify_channels": core._channels(core.setting(conn, "notify_channels")),
        "ntfy_ready": bool(core.setting(conn, "ntfy_topic")),
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


def setup_ntfy(conn, actor=None):
    from . import notify
    return notify.setup_ntfy(conn, actor=actor)


# Operations the page may call. Each maps JSON args to one core function.
def _launch_args(a):
    """The launch dialog's choices: model, effort, tab, window, or tmux, and profile options (each optional)."""
    return {**{k: a.get(k) or None for k in ("model", "effort", "launch_in")},
            "options": {k: str(v) for k, v in (a.get("options") or {}).items()} or None}


def _page_reason(a, who):
    """The reason of a stop from the page: the person's words, else who stopped it."""
    return (a.get("reason") or "").strip() or f"stopped from the page by {who or 'a person'}"


OPS = {
    # The page's Connect button for the relay: the device flow, finished in the background by maxpm serve.
    "relay_connect": lambda conn, a, who: relay.connect_from_page(bool(a.get("replace"))),
    "relay_disconnect": lambda conn, a, who: relay.disconnect(),
    "project_add": lambda c, a, who: core.project_add(c, a["name"], a.get("rank"), a.get("notes", ""), who),
    "project_rank": lambda c, a, who: core.project_rank(c, a["name"], a["rank"], who),
    "project_describe": lambda c, a, who: core.project_describe(c, a["name"], a["text"], who),
    "item_add": lambda c, a, who: core.item_add(c, a["project"], a["title"], int(a.get("priority", 2)),
                                                a.get("notes", ""), a.get("doer", "any"),
                                                [int(x) for x in a.get("after", [])], who,
                                                a.get("context", ""), a.get("touches"), a.get("check", ""),
                                                a.get("blocks"), a.get("mode"), a.get("found_during"),
                                                [int(x) for x in a.get("feeds", [])], a.get("due"),
                                                goals=a.get("goals"), models=a.get("models")),
    "item_edit": lambda c, a, who: core.item_edit(c, a["id"], a.get("title"), a.get("notes"), a.get("doer"),
                                                  a.get("project"), who, a.get("context"), a.get("touches"),
                                                  a.get("check"), a.get("due"), goals=a.get("goals"),
                                                  untag=a.get("untag"), models=a.get("models")),
    "goal_add": lambda c, a, who: core.goal_add(c, a["project"], a["name"], a.get("outcome", ""), a.get("done_when", ""),
                                                who, shared=bool(a.get("shared"))),
    "goal_edit": lambda c, a, who: core.goal_edit(c, a["name"], a.get("outcome"), a.get("done_when"), a.get("new_name"), who,
                                                  a.get("shared")),
    "goal_rank": lambda c, a, who: core.goal_rank(c, a["name"], a["rank"], who),
    "goal_own": lambda c, a, who: core.goal_own(c, a["name"], who),
    "goal_release": lambda c, a, who: core.goal_release(c, a["name"], who, a.get("no_handoff")),
    "goal_done": lambda c, a, who: core.goal_done(c, a["name"], a.get("result", ""), who, bool(a.get("drop_open"))),
    "goal_reopen": lambda c, a, who: core.goal_reopen(c, a["name"], who),
    "prio": lambda c, a, who: core.item_prio(c, a["id"], a["priority"], who),
    "move": lambda c, a, who: core.item_move(c, a["id"], a.get("before"), a.get("after"), who),
    "dep_add": lambda c, a, who: core.dep_add(c, a["id"], [int(x) for x in a["on"]], who, a.get("kind", "blocks")),
    "dep_remove": lambda c, a, who: core.dep_remove(c, a["id"], [int(x) for x in a["on"]], who),
    "claim": lambda c, a, who: core.claim(c, a["id"], who),
    "push": lambda c, a, who: core.push(c, a["id"], a["to"], a.get("note"), who),
    "push_cancel": lambda c, a, who: core.cancel_push(c, a["id"], who),
    "undo_takeover": lambda c, a, who: core.undo_takeover(c, a["id"], who),
    "takeover_seen": lambda c, a, who: core.takeover_seen(c, a["id"], who),
    "accept": lambda c, a, who: core.accept(c, a["id"], who),
    "decline": lambda c, a, who: core.decline(c, a["id"], a.get("note"), who),
    "next_claim": lambda c, a, who: core.next_item(c, a.get("project"), a.get("unblocks"), True, who, 1, a.get("near"),
                                                   bool(a.get("mine"))),
    "done": lambda c, a, who: core.done(c, a["id"], a.get("output"), who),
    "release": lambda c, a, who: core.release(c, a["id"], a.get("note"), who),
    "drop": lambda c, a, who: core.drop(c, a["id"], who),
    "reopen": lambda c, a, who: core.reopen(c, a["id"], who),
    "block": lambda c, a, who: core.block(c, a["id"], a.get("reason"), who, a.get("until")),
    "unblock": lambda c, a, who: core.unblock(c, a["id"], who),
    "replanned": lambda c, a, who: core.replanned(c, a["id"], a.get("note"), who),
    "register": lambda c, a, who: core.register(c, a["name"], bool(a.get("human")), a.get("note", "")),
    "config_set": lambda c, a, who: core.config_set(c, a["key"], str(a["value"]), a.get("project"), a.get("item"),
                                                    a.get("agent"), who),
    "config_unset": lambda c, a, who: core.config_unset(c, a["key"], a.get("project"), a.get("item"), a.get("agent"), who),
    "answer": lambda c, a, who: core.answer(c, int(a["msg"]), a["body"], who),
    "message_read": lambda c, a, who: _message_read(c, int(a["msg"])),
    "send": lambda c, a, who: core.send(c, a["kind"], a["body"], a.get("to"), a.get("item"), a.get("reply"), who,
                                        a.get("level")),
    "offer": lambda c, a, who: core.offer(c, a["body"], int(a["item"]), a.get("to"), who),
    "give": lambda c, a, who: core.give(c, int(a["id"]), a["to"], who),
    "split": lambda c, a, who: core.split(c, int(a["id"]), [t for t in a["titles"] if t.strip()], who),
    "setup_block": lambda c, a, who: setup_block(c, a["path"], a.get("move")),
    "setup_maxpm_cmd": lambda c, a, who: install_command(),
    "folder_add": lambda c, a, who: folder_add(c, a.get("path"), a.get("name"), a.get("description") or "", a.get("move"), who),
    "setup_skills": lambda c, a, who: setup_skills(),
    "setup_agent_add": lambda c, a, who: setup_agent_add(c, a["label"], who),
    "setup_ntfy": lambda c, a, who: setup_ntfy(c, who),
    # prompt: maxpm launch --prompt, which a command in a sandbox hands to maxpm serve.
    "launch_agent": lambda c, a, who: launch_agent(c, a.get("project"), agent=a.get("agent"), actor=who,
                                                   prompt=a.get("prompt"), **_launch_args(a)),
    "dispatch_item": lambda c, a, who: dispatch_item(c, int(a["id"]), agent=a.get("agent"), actor=who,
                                                     prompt=a.get("prompt"), **_launch_args(a)),
    "open_agent_on": lambda c, a, who: open_agent_on(c, int(a["id"]), agent=a.get("agent"), person=a.get("person"), actor=who,
                                                     **_launch_args(a)),
    "open_needs_you": lambda c, a, who: open_needs_you(c, agent=a.get("agent"), person=a.get("person"), **_launch_args(a)),
    "deploy_now": lambda c, a, who: deploy_now(c, a["target"], bool(a.get("review")), agent=a.get("agent"), actor=who,
                                               **_launch_args(a)),
    "target_cadence": lambda c, a, who: core.target_cadence(c, a["target"], a.get("cadence") or "off", who),
    "release_now": lambda c, a, who: core.release_now(c, a["target"], a.get("reason"), who),
    "target_cut": lambda c, a, who: core.target_cut(c, a["target"], a.get("rev"), who),
    "open_monitors": lambda c, a, who: open_monitors(c, db=a.get("db")),
    "queue_add": lambda c, a, who: core.queue_add(c, a["agent"], a.get("id"), a.get("message"), bool(a.get("first")),
                                                  a.get("before"), who),
    "queue_remove": lambda c, a, who: core.queue_remove(c, a["agent"], str(a["ref"]), who),
    # A person stops from the page without a reason; the agent then reads who stopped it.
    "stop_agent": lambda c, a, who: core.stop_agent(c, a["agent"], _page_reason(a, who), who),
    "kill_agent": lambda c, a, who: core.kill_agent(c, a["agent"], _page_reason(a, who), who),
    "start_manager": lambda c, a, who: start_manager(c, agent=a.get("agent"), actor=who, **_launch_args(a)),
    "open_chat": lambda c, a, who: open_chat(c, a["agent"]),
    "terminal_keys": lambda c, a, who: terminal_keys(c, a["agent"], a.get("keys"), who),
    "tmux_tidy": lambda c, a, who: tmux_tidy(c),
    "serve_restart": lambda c, a, who: (_same_queue(a.get("db")), serve_restart(c))[1],
    "serve_status": lambda c, a, who: {"boot": BOOT, "stale": code_stale()},
    "decline_message": lambda c, a, who: core.decline_message(c, int(a["msg"]), a.get("note"), who),
    "update": lambda c, a, who: _no_update_in_app() or update_apply(),
    "restart": lambda c, a, who: _no_update_in_app() or (_restart_soon(), {"restarting": True})[1],
}


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

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/mcp":
            return self._mcp("GET")
        if self._refuse():
            return
        if path in ("/", "/index.html"):
            return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/state":
            conn = core.connect()
            try:
                core.poll_activity(conn, None)  # a busy queue skips the sweep; the page still gets its state
                st = core.state(conn)
                st["goals"] = core.goal_list(conn, include_complete=True)
                if DEV["on"]:
                    st["dev_build"] = _build_id()
                st["desktop"] = DESKTOP
                st["terminals"] = agent_terminals(conn)  # the agents whose tmux pane the page can show
                st["tmux_done"] = tmux_done(conn, st["terminals"])  # the panes its Close button closes
                st["relay"] = relay.status()
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
        if path == "/api/log":
            from urllib.parse import parse_qs, urlsplit
            q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            conn = core.connect()
            try:
                since = q.get("since", "7d")
                return self._send(200, core.completed(conn, q.get("project") or None, None if since == "all" else since))
            except RiverError as e:
                return self._send(400, {"error": str(e)})
            finally:
                conn.close()
        if path == "/api/needs-you":
            from urllib.parse import parse_qs, urlsplit
            q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            conn = core.connect()
            try:
                return self._send(200, {"events": needs_you_view(conn, q.get("human"))})
            finally:
                conn.close()
        if path == "/api/prompt-all" or (path.startswith("/api/item/") and path.endswith("/prompt")):
            from urllib.parse import parse_qs, urlsplit
            q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            conn = core.connect()
            try:
                if path == "/api/prompt-all":
                    return self._send(200, {"prompt": core.prompt_for_all(conn, q.get("person") or None)})
                return self._send(200, {"prompt": core.prompt_for(conn, int(path.split("/")[3]), q.get("person") or None)})
            except (RiverError, ValueError) as e:
                return self._send(404, {"error": str(e)})
            finally:
                conn.close()
        if path == "/api/inbox":
            from urllib.parse import parse_qs, urlsplit
            q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            conn = core.connect()
            try:
                # The page only looks: messages turn read when the person acts on them (Mark read, answer, reply).
                return self._send(200, {"messages": core.inbox(conn, q.get("agent"), bool(q.get("all")), mark_read=False)})
            except RiverError as e:
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
        if path == "/api/setup":
            conn = core.connect()
            try:
                return self._send(200, setup_status(conn))
            finally:
                conn.close()
        if path.startswith("/api/thread/"):
            conn = core.connect()
            try:
                return self._send(200, core.thread(conn, int(path.rsplit("/", 1)[1])))
            except (RiverError, ValueError) as e:
                return self._send(404, {"error": str(e)})
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
            actor = body.get("actor") or None
            _same_queue(body.get("args", {}).get("db"))  # only a river command sends it (cli.ask_server)
            conn = core.connect()
            # The page shown through the relay (relay.handle_page): its actions are recorded as <name>@relay.
            via = core.EVENT_VIA.set("relay" if relay.page_request(self.headers) else None)
            try:
                core.activity(conn, actor)
                result = op(conn, body.get("args", {}), actor)
                with core.tx(conn):
                    core.sync_needs_you(conn)
                if body.get("op") in ("claim", "next_claim") and core.pending_monitors(conn):
                    try:  # a person claimed a deploy item on the page: its monitor starts too
                        open_monitors(conn)
                    except RiverError as e:
                        print(f"monitor: {e}", flush=True)
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
    code = {f: f.stat().st_mtime_ns for f in PKG.glob("*.py")}
    while True:
        time.sleep(1)
        now = {f: f.stat().st_mtime_ns for f in PKG.glob("*.py")}
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
    threading.Thread(target=notify.loop, args=(stop,), daemon=True, name="maxpm-notify").start()
    relay.start(stop, port)  # keeps the relay socket open while relay.json exists (maxpm connect)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
