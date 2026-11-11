"""The Grok CLI adapter (docs/agent-adapters.md, #5172): xAI's Grok Build TUI, the command grok.

Read from Grok 1.0.77 on macOS (grok --help, ~/.grok/docs/user-guide, a recorded session: tests/fixtures/grok):

- Launch: grok [--agent <file>] [-m <model>] [--reasoning-effort <level>] [--permission-mode <mode>]
  [--sandbox <profile>] [PROMPT]. It has no session name flag (/rename names a session from inside).
- Resume: grok --resume <session id> keeps the id and adds to the same log; --fork-session with it starts a fork.
- Sessions: ~/.grok/sessions/<the URL-encoded real cwd>/<session id>/ (updates.jsonl, summary.json, ...).
  $GROK_HOME replaces ~/.grok.
- State: Grok runs hooks for the turn start (UserPromptSubmit), each turn end (Stop, StopFailure, StopCancelled), the
  idle backstop (Notification idle_prompt) and a waiting permission menu (Notification permission_prompt). Every hook
  gets the session id ($GROK_SESSION_ID, and sessionId in the JSON on stdin) and the session's environment
  (MAXPM_AGENT). So the hooks are the one source of the state and of the session id. They live in an agent
  definition that the session starts with (grok --agent <file>, install_hooks): no file in ~/.grok, and no hook in a
  person's own Grok sessions.
"""

import json
import os
import shlex
import sys
import time
from pathlib import Path

from . import Raw

DESCRIBE = {
    "id": "grok",
    "label": "Grok CLI",
    "exe": "grok",
    "aliases": ["grok-cli", "grok-build"],
    # Grok's models are xAI's (grok-4.7), a family whose ladder MaximizePM does not know: an item's
    # --min-model and --max-model do not apply to it, as for a provider/model agent.
    "family": "multi",
    "models": ["grok-4.7"],  # Grok 1.0.77; read_models reads the list Grok fetched last
    "default_model": "",
    "model_ids": {},
    "prefix": "grok_",
    # /model <model> switches the model of a running session; the command line names it as -m or --model (#5262).
    "model_live": "/model {value}",
    "model_read": r"(?:^|\s)(?:-m|--model)[= ](\S+)",
}


def grok_home(env=None):
    """Grok's own folder: $GROK_HOME, else ~/.grok."""
    env = os.environ if env is None else env
    return Path(env.get("GROK_HOME") or Path.home() / ".grok").expanduser()


def _models_cache(env=None):
    """Grok's model list as it fetched it last (models_cache.json: {"models": {id: {"info": {...}}}}), or {}."""
    try:
        got = json.loads((grok_home(env) / "models_cache.json").read_text())
    except (OSError, ValueError):
        return {}
    models = got.get("models") if isinstance(got, dict) else None
    return models if isinstance(models, dict) else {}


def read_models():
    """The ids of the models that Grok shows (its models cache, hidden ones left out), or None with no cache."""
    out = [mid for mid, m in _models_cache().items()
           if isinstance(m, dict) and not (m.get("info") or {}).get("hidden")]
    return sorted(out, reverse=True) or None


# ---------------------------------------------------------------------------------------------------------------
# 2. Settings
# ---------------------------------------------------------------------------------------------------------------

PERMISSION_MODES = ["default", "acceptEdits", "auto", "dontAsk", "bypassPermissions", "plan"]
SANDBOXES = ["off", "workspace", "devbox", "read-only", "strict"]

SETTINGS = [
    {"key": "permission_mode", "label": "Permission mode", "kind": "choice", "choices": PERMISSION_MODES,
     "default": "",
     "text": "when Grok asks before a tool call (--permission-mode; bypassPermissions is always-approve); "
             "empty: Grok's own setting ([ui] permission_mode in ~/.grok/config.toml)",
     "flag": ["--permission-mode", "{value}"], "live": None, "read": r"--permission-mode[= ]([A-Za-z]+)",
     "session": True},
    {"key": "sandbox", "label": "Sandbox", "kind": "choice", "choices": SANDBOXES, "default": "",
     "text": "the sandbox profile of its file and network access (--sandbox); empty: Grok's own setting",
     "flag": ["--sandbox", "{value}"], "live": None, "read": r"--sandbox[= ]([A-Za-z_-]+)", "session": True},
    {"key": "args", "label": "More arguments", "kind": "text", "default": "",
     "text": "more arguments, put before the prompt", "flag": [], "live": None, "read": None},
    {"key": "prompt", "label": "First prompt", "kind": "text",
     "default": "run maxpm go in this folder and follow the briefing",
     "text": "the first prompt", "flag": [], "live": None, "read": None},
]


# ---------------------------------------------------------------------------------------------------------------
# 3. Effort
# ---------------------------------------------------------------------------------------------------------------

EFFORT = {
    # The levels of /effort; grok-4.5 has no xhigh. read_effort_levels reads them per model from Grok's cache.
    "levels": ["low", "medium", "high", "xhigh"],
    "map": {"max": "xhigh"},
    "flag": ["--reasoning-effort", "{value}"],
    "live": "/effort {value}",
    # The session's command line names it (#5262); Grok's updates.jsonl does not.
    "read": r"--reasoning-effort[= ]([a-z]+)",
}


def read_effort_levels(model):
    """The effort levels that Grok offers for model (models_cache.json, reasoning_efforts), lowest first; None when
    the cache does not know the model, so EFFORT['levels'] stands; [] for a model with no effort."""
    m = _models_cache().get(model or "")
    info = m.get("info") if isinstance(m, dict) else None
    if not isinstance(info, dict):
        return None
    if not info.get("supports_reasoning_effort"):
        return []
    got = [e.get("id") for e in info.get("reasoning_efforts") or () if isinstance(e, dict) and e.get("id")]
    order = {lv: i for i, lv in enumerate(EFFORT["levels"])}
    return sorted(got, key=lambda lv: order.get(lv, len(order)))


# ---------------------------------------------------------------------------------------------------------------
# 4. Launch and 9. Resume
# ---------------------------------------------------------------------------------------------------------------

# Grok takes no session name and no hooks file of MaximizePM's (its hooks come in with install_hooks' --agent file).
# It forks with --resume <id> --fork-session.
LAUNCH = {"session_name": False, "system_prompt_file": False, "hooks_file": False, "extra_dirs": False,
          "fork": True, "resume": True}


def build_command(name, folder, model, effort, settings, prompt):
    """grok [--resume <id> [--fork-session]] [-m M] [--reasoning-effort E] [--permission-mode P] [--sandbox S]
    <args> [prompt]. The args setting goes in as the person wrote it (a Raw word). settings['resume'] resumes a
    session, settings['fork'] starts a fork of one. Grok runs in the folder of its pane, so folder adds nothing; it
    takes no session name, so name adds nothing either."""
    s = settings
    words = ["grok"]
    if s.get("resume"):
        words += ["--resume", s["resume"]]
    elif s.get("fork"):
        words += ["--resume", s["fork"], "--fork-session"]
    words += ["-m", model] if model else []
    level = (EFFORT["map"].get(effort, effort)) if effort else None
    words += [w.format(value=level) for w in EFFORT["flag"]] if level else []
    for spec in SETTINGS:
        if spec["flag"] and s.get(spec["key"]):
            words += [w.format(value=s[spec["key"]]) for w in spec["flag"]]
    words += [Raw(s["args"].strip())] if (s.get("args") or "").strip() else []
    if prompt:
        words.append(prompt)
    return words


def resume_command(session_ref, model, effort, settings, name=None):
    """grok --resume <session id>, with the profile's settings and settings['prompt'] (if any) as the first prompt."""
    if not session_ref:
        return None
    return build_command(name, None, model, effort, {**settings, "resume": session_ref}, settings.get("prompt"))


def parse_command(words):
    """A hand-written grok command that a profile builds exactly: -m/--model {model}, --reasoning-effort/--effort
    {effort}, --permission-mode <a known mode> (--always-approve and --yolo are bypassPermissions), --sandbox <a
    known profile>, and one prompt. Anything else keeps it a custom command, so nothing is lost."""
    if not words or Path(words[0]).name.lower() != "grok":
        return None
    opts, prompt, i = {}, None, 1
    while i < len(words):
        t, nxt = words[i], (words[i + 1] if i + 1 < len(words) else None)
        if t in ("-m", "--model") and nxt == "{model}" or t in ("--reasoning-effort", "--effort") and nxt == "{effort}":
            i += 2
        elif t == "--permission-mode" and nxt in PERMISSION_MODES:
            opts["permission_mode"] = nxt
            i += 2
        elif t in ("--always-approve", "--yolo"):
            opts["permission_mode"] = "bypassPermissions"
            i += 1
        elif t == "--sandbox" and nxt in SANDBOXES:
            opts["sandbox"] = nxt
            i += 2
        elif not t.startswith("-") and prompt is None and t.strip() and ";" not in t:
            prompt = t
            i += 1
        else:
            return None
    return {"settings": opts, "prompt": prompt} if prompt is not None else None


# ---------------------------------------------------------------------------------------------------------------
# 5. Identity and 7. State: Grok's hooks call maxpm report
# ---------------------------------------------------------------------------------------------------------------

# The session id: the SessionStart hook reports it (maxpm report --session-id, with the resume command). Grok also sets
# GROK_SESSION_ID in the commands its tools run (Grok 1.0.77; it sets GROK_AGENT there too, but a person may set that
# one to pick an agent).
IDENTITY = {"source": "hook", "env": None, "log_glob": None, "pane": None, "url": None,
            "inside_env": ["GROK_SESSION_ID"]}

STATE = {"state": "hook", "session_id": "hook"}

# The screens of Grok 1.0.77 in a pane (tests/fixtures/grok/pane_*.txt). The state comes from the hooks; maxpm serve's
# pane watcher reads a blocked screen with these rules (its question and options). Test blocked first: a permission
# menu shows under the running turn's "[stop]" line.
DETECT = {
    "process": "grok",
    # tmux names the process of a release build after its file (grok-1.0.77-mac): found in its command line instead.
    "process_args": r"(?:^|/)grok(?:-\d[\w.]*-[a-z]+)?(?:\s|$)",
    # The footer of the prompt with nothing running: "Shift+Tab:mode │ Ctrl+x:shortcuts" (while a turn runs,
    # Ctrl+c:cancel comes between them); or the welcome screen of a new session, which has no footer yet.
    "idle": r"(?m)Shift\+Tab:mode\s+│\s+Ctrl\+x:shortcuts\s*$|Resume session\s+ctrl\+r",
    # The status line of a running turn ends in "[stop]": "⠴ Thinking… 1.4s   3.0s ⇣1.33k [stop]".
    "working": r"(?m)\[stop\]\s*$",
    # The pane watcher reads these screens with the layout keys below (#5261): the trust question (its last option,
    # "No, quit  n", is the last line of the prompt), each numbered menu in a ┃ box (a permission, the free usage
    # limit), and the free usage limit's own words.
    "blocked": [r"Do you trust the contents of this directory\?", r"(?m)^\s*No, quit\s+n\s*$",
                r"(?m)^\s*┃\s+\d+ \([○●]\) \S", r"You hit your free usage limit"],
    "tell_instead": r"No, reject \(type to add feedback\)",
    "image_paste": "[Image #{n}]",
    "menu": "●",  # the selected option: "3 (●) Yes, proceed"; the others are "(○)"
    "other": "○",
    "item": r"\d+ \({mark}\)",  # the mark comes after the option's number
    "frame": "│┃╭╮╰╯",
    # Also under a prompt: the version line at the foot of the trust screen ("Grok Build  1.0.77 [stable]").
    "rule": r"^\s*[─━╌╍┄┅═-]{8,}\s*$|^\s*Grok Build\s+\d[\w.]*\s+\[\w+\]\s*$",
    # "1/5:select │ Tab:next option │ ←/→:scope │ ... │ Ctrl+c:cancel"; "Tab:next answer │ Esc:scrollback │ ..."
    "hint": r"\d+/\d+:select\s+│|Tab:next answer\s+│",
}

# The hooks go into a Grok agent definition (a Markdown file with a front matter, grok --agent <file>) beside the
# queue: its hooks run for that session only (Grok names them agent:maxpm:<event>), so nothing goes into ~/.grok and a
# person's own Grok sessions run no MaximizePM hook. prompt_mode extend keeps Grok's own system prompt and adds the
# body; agents_md keeps AGENTS.md and CLAUDE.md in the session's context.
#
# Each hook runs grok-hook.py with its event name. One event's input cannot say all that Grok's own rules for a busy
# and idle indicator need (docs/user-guide/10-hooks.md), so the script keeps a small record per session (the prompt
# ids that started and ended, the tool call before a permission menu, the last state it passed on). It drops what is
# not the session's state (a subagent's events, a late turn end of an older prompt, a tool event after its turn, the
# same state twice) and then becomes, by exec, one maxpm command, as a Claude Code hook runs it: maxpm report --hook
# grok with the event on stdin (read_hook below), maxpm report --state idle --session-id <id> -- grok --resume <id>
# at SessionStart, or maxpm release at SessionEnd. A Stop's reply (a reminder to close answered questions) goes back
# to Grok, which reads it as a Claude Code Stop hook's.
AGENT_FILE = "grok-agent.md"
HOOK_SCRIPT_FILE = "grok-hook.py"
STATE_DIR = "grok-hooks"          # one file per session
HOOK_TIMEOUT = 10                 # seconds, as Claude Code's hooks
WORKING_EVERY = 60                # seconds: a tool call reports working again at most this often
PROMPTS_KEPT = 50                 # prompt ids kept per session, to know which turn is the newest
STATE_KEEP = 7 * 24 * 3600        # seconds: a state file older than this is of a session that ended unseen
PROMPT_MAX = 16000                # characters of a typed prompt passed on (it closes the questions it answers)

# The events the hooks listen to. PreToolUse only remembers the tool call that a permission menu may then ask about
# (Grok's permission_prompt says only "Tool permission requested").
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop",
               "StopFailure", "StopCancelled", "Notification", "SessionEnd")

HOOK_SCRIPT = '''"""MaximizePM's Grok hook (written by river/agents/grok.py install_hooks; it is written again when it changes).
Each Grok hook event runs it with the event name; it passes the session's state on to maxpm report."""
import sys
sys.path.insert(0, {root!r})
from river.agents.grok import hook_main
sys.exit(hook_main(sys.argv[1:], {report!r}, state_dir={state!r}))
'''

AGENT_BODY = ("This is a MaximizePM session: its hooks report its state to MaximizePM. Follow the project's "
              "instructions (CLAUDE.md, AGENTS.md) and the maxpm briefing.")


def agent_definition(python, script):
    """The text of the agent file: each event of HOOK_EVENTS runs `python script <event>`. The front matter is YAML;
    each command is a JSON string, which YAML reads as the same double-quoted string."""
    lines = ["---", "name: maxpm", "description: A MaximizePM session (its hooks report its state)",
             "prompt_mode: extend", "agents_md: true", "hooks:"]
    for ev in HOOK_EVENTS:
        cmd = f"{shlex.quote(python)} {shlex.quote(script)} {ev}"
        lines += [f"  {ev}:", "    - hooks:", "        - type: command", f"          command: {json.dumps(cmd)}",
                  f"          timeout: {HOOK_TIMEOUT}"]
    return "\n".join(lines + ["---", "", AGENT_BODY, ""])


def _write(path, text):
    """Write path when its text differs; True when it wrote."""
    try:
        if path.read_text() == text:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)
    return True


def install_hooks(folder, report, python=None, where=None):
    """Write the agent file and its hook script beside the queue (core.river_dir(), or where), each when its text
    changes, and return the words that start the session with them: --agent <file>. report: the maxpm report command
    (argv words, core.report_command())."""
    if where is None:
        from ..core import river_dir
        where = river_dir()
    where = Path(where)
    script = where / HOOK_SCRIPT_FILE
    root = str(Path(__file__).resolve().parents[2])
    _write(script, HOOK_SCRIPT.format(root=root, report=list(report), state=str(where / STATE_DIR)))
    agent = where / AGENT_FILE
    _write(agent, agent_definition(python or sys.executable, str(script)))
    return ["--agent", str(agent)]


def decide(event, payload, st, now):
    """What one hook event means for the session's state: (state or None, release). st is the session's record;
    decide changes it. Grok's rules (docs/user-guide/10-hooks.md, 'A complete busy and idle indicator'): a subagent's
    events are not the session's; a turn end of an older prompt changes nothing (a cancelled turn reports late); a
    turn end with no prompt id, or of a prompt that never started, settles the session as idle."""
    if payload.get("subagentType"):
        return None, False
    pid = payload.get("promptId")
    started, ended = st.setdefault("started", []), st.setdefault("ended", [])
    newest = started[-1] if started else None
    if event == "SessionEnd":
        return None, True
    if event == "SessionStart":
        return ("working" if newest and newest not in ended else "idle"), False  # a prompt may come first
    if event == "UserPromptSubmit":
        if pid and pid not in started:
            started.append(pid)
            del started[:-PROMPTS_KEPT]
        return "working", False
    if event in ("Stop", "StopFailure", "StopCancelled"):
        if event == "Stop" and payload.get("reason") not in (None, "end_turn"):
            return None, False  # the Stop of the session's end: SessionEnd follows
        if pid and pid in started and pid != newest:
            return None, False  # an older turn, ended after a newer one started
        if pid and pid not in ended:
            ended.append(pid)
            del ended[:-PROMPTS_KEPT]
        return "idle", False
    if event == "PreToolUse":
        st["pending"] = _call(payload)
        return None, False
    if event == "Notification":
        kind = payload.get("notificationType")
        return {"idle_prompt": "idle", "permission_prompt": "blocked"}.get(kind), False
    if event in ("PostToolUse", "PostToolUseFailure"):
        # A tool call while the newest turn is open: the turn goes on (after a permission menu, too). Grok 1.0.77
        # sends no promptId with a tool event, so the open turn decides: a background command that ends after its
        # turn ended says nothing. Passed on again only after WORKING_EVERY, so a turn of many tool calls does not
        # start maxpm for each one.
        if pid and pid != newest or not newest or newest in ended:
            return None, False
        if st.get("last") == "working" and now - st.get("last_at", 0) < WORKING_EVERY:
            return None, False
        return "working", False
    return None, False


def _call(payload):
    """The tool call of a PreToolUse: {"tool": name, "input": {key: text}} (the shape of a hook report's call)."""
    inp = payload.get("toolInput")
    inp = {k: v[:400] for k, v in inp.items() if isinstance(v, str)} if isinstance(inp, dict) else {}
    return {"tool": str(payload.get("toolName") or ""), "input": inp}


def _exec(argv, data, env):
    """Become argv (os.execvpe), with data (or nothing) on stdin: the maxpm command then runs in the hook's place,
    as a Claude Code hook's command does (its parent is the agent's, and its reply goes to Grok)."""
    import tempfile
    f = tempfile.TemporaryFile()
    f.write((data or "").encode())
    f.seek(0)
    os.dup2(f.fileno(), 0)
    sys.stdout.flush()
    if "--hook" not in argv:  # only maxpm report --hook has a reply for Grok (a Stop's reminder)
        null = os.open(os.devnull, os.O_WRONLY)
        os.dup2(null, 1)
    os.execvpe(argv[0], argv, env)


def _locked(path):
    """An open file of path, locked for this process (fcntl where it exists)."""
    f = open(path, "a+")
    try:
        import fcntl
        fcntl.flock(f, fcntl.LOCK_EX)
    except (ImportError, OSError):
        pass
    return f


def _tidy(folder, now):
    """Remove the state files of sessions that ended with no SessionEnd (a crash, a killed pane)."""
    for p in folder.glob("*.state"):
        try:
            if now - p.stat().st_mtime > STATE_KEEP:
                p.unlink()
        except OSError:
            pass


def _slim(event, payload, st):
    """The part of the event that read_hook reads: no tool output, no answer text."""
    keep = ("hookEventName", "sessionId", "promptId", "notificationType", "message", "reason", "stopHookActive",
            "backgroundTasks", "sessionCrons", "error")
    out = {k: payload[k] for k in keep if k in payload}
    if isinstance(payload.get("prompt"), str):
        out["prompt"] = payload["prompt"][:PROMPT_MAX]
    out["maxpmEvent"] = event
    if event == "Notification" and payload.get("notificationType") == "permission_prompt" and st.get("pending"):
        out["maxpmCall"] = st["pending"]
    return out


def hook_main(argv, report, stdin=None, env=None, now=None, run=None, state_dir=None):
    """One Grok hook: argv is [event]; the event's JSON comes on stdin. It ends as one maxpm command (run, by default
    _exec), or exits 0 with nothing to say. It never fails Grok's hook."""
    env = os.environ if env is None else env
    try:
        if not env.get("MAXPM_AGENT") or not argv:
            return 0  # a Grok session that MaximizePM did not start
        event = argv[0]
        try:
            payload = json.loads((stdin if stdin is not None else sys.stdin).read() or "{}")
        except ValueError:
            payload = {}
        payload = payload if isinstance(payload, dict) else {}
        sid = str(env.get("GROK_SESSION_ID") or payload.get("sessionId") or "")
        if not sid or "/" in sid or sid.startswith("."):
            return 0
        now = time.time() if now is None else now
        if not state_dir:
            import tempfile
            state_dir = Path(tempfile.gettempdir()) / f"maxpm-{STATE_DIR}"
        folder = Path(state_dir)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{sid}.state"
        existed = path.exists()
        if event == "SessionStart":
            _tidy(folder, now)
        cmd, data = None, None
        with _locked(path) as f:
            f.seek(0)
            try:
                st = json.loads(f.read() or "{}")
            except ValueError:
                st = {}
            state, release = decide(event, payload, st, now)
            if release:
                if report and report[-1] == "report":
                    cmd = list(report[:-1]) + ["release"]
            elif event == "SessionStart" and state:
                # Always: it records the session id and the resume command (hook_report stores neither).
                cmd = list(report) + ["--state", state, "--session-id", sid, "--seq", str(time.time_ns()),
                                      "--", "grok", "--resume", sid]
            elif state and (state != st.get("last") or state == "blocked"
                            or event in ("PostToolUse", "PostToolUseFailure")):
                cmd, data = list(report) + ["--hook", "grok"], json.dumps(_slim(event, payload, st))
            if cmd and state:
                st.update(last=state, last_at=now)
            f.seek(0)
            f.truncate()
            f.write(json.dumps(st))
            f.flush()
            if env.get("MAXPM_HOOK_LOG"):
                with open(env["MAXPM_HOOK_LOG"], "a") as log:
                    log.write(json.dumps({"at": now, "event": event, "state": state, "cmd": cmd}) + "\n")
            if release or (not existed and not cmd):
                try:  # the session ended, or a late hook of an ended session (the Stop of its shutdown) made the file
                    path.unlink()
                except OSError:
                    pass
            if cmd:
                # The lock goes with the command (the file stays open through exec), so the next hook of this
                # session waits until this report is stored: the reports of one session never overtake each other.
                os.set_inheritable(f.fileno(), True)
                (run or _exec)(cmd, data, dict(env))
    except Exception:  # noqa: BLE001 - a hook must never stop Grok; the state stays as it was
        return 0
    return 0


# Grok's own event names in its hook input (hookEventName), for an input with no maxpmEvent.
_EVENT_NAMES = {"session_start": "SessionStart", "user_prompt_submit": "UserPromptSubmit",
                "pre_tool_use": "PreToolUse", "post_tool_use": "PostToolUse",
                "post_tool_use_failure": "PostToolUseFailure", "stop": "Stop", "stop_failure": "StopFailure",
                "stop_cancelled": "StopCancelled", "notification": "Notification", "session_end": "SessionEnd"}
_TASK_ENDED = {"completed", "failed", "killed", "cancelled", "canceled", "stopped", "done", "error"}


def _waits(payload):
    """The background work that will wake a session whose turn ended, from the Stop hook's input: backgroundTasks
    (shell, monitor, subagent) that have not ended, and sessionCrons (scheduled wakeups, /loop). Short texts
    ('shell: make check'); [] when nothing will; None when the input has neither field."""
    tasks, crons = payload.get("backgroundTasks"), payload.get("sessionCrons")
    if not isinstance(tasks, list) and not isinstance(crons, list):
        return None
    out = []
    for t in tasks if isinstance(tasks, list) else ():
        if not isinstance(t, dict) or str(t.get("status") or "").lower() in _TASK_ENDED:
            continue
        what = next((str(t[k]) for k in ("command", "agentType", "description") if t.get(k)), "")
        out.append(f"{t.get('type') or 'task'}: {' '.join(what.split())[:120]}" if what else str(t.get("type") or "task"))
    for c in crons if isinstance(crons, list) else ():
        if isinstance(c, dict):
            out.append(f"wakeup {c.get('schedule') or ''}".strip())
    return out


def read_hook(payload):
    """A Grok hook's input as a hook report (river/agents HOOK_REPORT_KEYS), or None for an event of no state.
    grok-hook.py passes it on (maxpm report --hook grok) with maxpmEvent, and for a permission menu maxpmCall, the
    tool call that waits."""
    ev = str(payload.get("maxpmEvent") or _EVENT_NAMES.get(str(payload.get("hookEventName") or "")) or "")
    ntype = str(payload.get("notificationType") or "")
    sid = payload.get("sessionId")
    out = {"event": ev + (f":{ntype}" if ntype else ""), "session_id": sid if isinstance(sid, str) and sid else None}
    if ev == "UserPromptSubmit":
        prompt = payload.get("prompt")
        out.update(state="working", prompt=prompt if isinstance(prompt, str) else "")
    elif ev in ("PostToolUse", "PostToolUseFailure"):
        out["state"] = "working"
    elif ev == "Notification" and ntype == "permission_prompt":
        call = payload.get("maxpmCall")
        out.update(state="blocked", shows=True, message=str(payload.get("message") or "")[:160],
                   call=call if isinstance(call, dict) else None)
    elif ev == "Notification" and ntype == "idle_prompt":
        out["state"] = "idle"
    elif ev in ("Stop", "StopFailure", "StopCancelled"):
        out.update(state="idle", waits=_waits(payload), again=bool(payload.get("stopHookActive")))
    elif ev == "SessionStart":
        out["state"] = "idle"
    else:
        return None
    return out


# ---------------------------------------------------------------------------------------------------------------
# 6. Chat log: ~/.grok/sessions/<URL-encoded cwd>/<session id>/updates.jsonl
# ---------------------------------------------------------------------------------------------------------------

# updates.jsonl holds the session's ACP updates, one JSON line each: {"timestamp": <s>, "method": "session/update" (or
# "_x.ai/session/update"), "params": {"sessionId", "update": {"sessionUpdate": <kind>, ...}, "_meta"}}. A resume
# (grok --resume <id>) adds to the same file, so a session has one file and no chain.
# Grok bills a call after its prompt cache ran out at the input price (its usage has no cache write).
CHAT = {"compressed": False, "file_tools": ["write", "search_replace"], "ref": "session", "cache_write": False}

RESULT_MAX = 4000  # characters of a tool's output kept in a result entry


def chat_path(session_ref, folder=None, root=None):
    """The update log of session session_ref: under the folder's own name when folder is given (Grok names it by the
    real path, URL-encoded), else in any folder of ~/.grok/sessions (a session id is unique). None when none is.
    root replaces ~/.grok/sessions."""
    from urllib.parse import quote
    sid = str(session_ref or "")
    if not sid or "/" in sid or sid.startswith("."):
        return None
    base = Path(root) if root else grok_home() / "sessions"
    if folder:
        p = base / quote(os.path.realpath(os.path.expanduser(folder)), safe="") / sid / "updates.jsonl"
        if p.is_file():
            return p
    got = sorted(base.glob(f"*/{sid}/updates.jsonl"))
    return got[0] if got else None


def _at(x):
    t = x.get("timestamp") if isinstance(x, dict) else None
    if not isinstance(t, (int, float)):
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _content_text(items):
    """The text of an update's content: a block, or a list of tool content items (text, or a diff of a file)."""
    out = []
    for c in items if isinstance(items, list) else [items]:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "content":
            c = c.get("content") or {}
        if c.get("type") == "text" and c.get("text"):
            out.append(c["text"])
        elif c.get("type") == "diff" and c.get("path"):
            out.append(f"{c['path']}: {len((c.get('newText') or '').splitlines())} lines written")
    return "\n".join(out)


def _image(c):
    """A data URL (or the uri) of an image content block, or None."""
    if not isinstance(c, dict) or c.get("type") != "image":
        return None
    if c.get("data"):
        return f"data:{c.get('mimeType') or 'image/png'};base64,{c['data']}"
    return c.get("uri")


def _updates(lines):
    for line in lines:
        try:
            x = line if isinstance(line, dict) else json.loads(line)
        except (TypeError, ValueError):
            continue
        u = ((x.get("params") or {}).get("update") if isinstance(x, dict) else None)
        if isinstance(u, dict) and u.get("sessionUpdate"):
            yield x, u


def parse_chat(lines):
    """(entries, turn) of the lines of updates.jsonl. A message comes in chunks: the chunks in a row of one kind
    make one entry. A tool call is a tool entry (its raw name and the command or path); its completed or failed
    update is a result entry. Thoughts and hook runs are left out. The turn works from a user message to the
    turn_completed of its prompt."""
    entries, turn, last, last_prompt = [], None, None, None
    for x, u in _updates(lines):
        kind, at = u["sessionUpdate"], _at(x)
        if kind in ("user_message_chunk", "agent_message_chunk"):
            role = "user" if kind == "user_message_chunk" else "assistant"
            c = u.get("content") or {}
            text, img = _content_text(c), _image(c)
            prompt = (u.get("_meta") or {}).get("promptIndex") if role == "user" else None
            if last is not None and last["role"] == role and (role == "assistant" or prompt == last_prompt):
                last["text"] = last["text"] + text if role == "assistant" else "\n".join(t for t in (last["text"], text) if t)
            else:
                last = {"at": at, "role": role, "text": text}
                entries.append(last)
            last_prompt = prompt
            if img:
                last.setdefault("images", []).append(img)
            if role == "user":
                turn = "working"
            continue
        last = None
        if kind == "tool_call":
            raw = u.get("rawInput") or {}
            detail = next((raw[k] for k in ("command", "file_path", "path", "url", "pattern", "query")
                           if isinstance(raw, dict) and isinstance(raw.get(k), str)), "")
            entries.append({"at": at, "role": "tool", "id": u.get("toolCallId"), "name": u.get("title") or "tool",
                            "detail": detail, "text": (raw.get("description") or "") if isinstance(raw, dict) else ""})
        elif kind == "tool_call_update" and u.get("status") in ("completed", "failed"):
            entries.append({"at": at, "role": "result", "for": u.get("toolCallId"), "error": u["status"] == "failed",
                            "text": _content_text(u.get("content"))[:RESULT_MAX]})
        elif kind == "turn_completed":
            turn = "idle"
            why = u.get("stop_reason")
            if why and why != "end_turn":
                entries.append({"at": at, "role": "event", "text": f"turn ended: {why}"})
        elif kind == "current_mode_update" and u.get("currentModeId"):
            entries.append({"at": at, "role": "event", "text": f"mode: {u['currentModeId']}"})
    return entries, turn


def parse_chat_line(d):
    """(entries, turn) of one decoded line of updates.jsonl. Grok writes each message block whole (one chunk a
    line), so a line alone gives the same entries as the file."""
    return parse_chat([d])


def chat_usage(lines):
    """The tokens of the session's turns (turn_completed usage). Grok counts the cached part inside inputTokens;
    input here is the part that was not read from the cache, as for the other agents."""
    tot = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    for _, u in _updates(lines):
        us = u.get("usage") if u["sessionUpdate"] == "turn_completed" else None
        if not isinstance(us, dict):
            continue
        cached = int(us.get("cachedReadTokens") or 0)
        tot["input"] += max(0, int(us.get("inputTokens") or 0) - cached)
        tot["output"] += int(us.get("outputTokens") or 0)
        tot["cache_read"] += cached
        tot["cache_write"] += int(us.get("cacheCreationTokens") or 0)
    return tot


# ---------------------------------------------------------------------------------------------------------------
# 8. Input, 10. Commands, 11. Setup
# ---------------------------------------------------------------------------------------------------------------

INPUT = {"native": None}

# The built-in slash commands of Grok 1.0.77 (docs/user-guide/04-slash-commands.md), the ones a person types most.
COMMANDS = [
    {"name": "new", "description": "start a fresh session and clear the conversation", "hint": ""},
    {"name": "resume", "description": "open the session picker to reload an earlier session", "hint": ""},
    {"name": "compact", "description": "compress the conversation history to free context", "hint": "[what to keep]"},
    {"name": "context", "description": "show how the context window is used", "hint": ""},
    {"name": "session-info", "description": "show the session: auth, model, turns, context use", "hint": ""},
    {"name": "fork", "description": "branch the session into a new agent with its history", "hint": "[directive]"},
    {"name": "rewind", "description": "roll the conversation back to an earlier turn", "hint": ""},
    {"name": "rename", "description": "rename the session", "hint": "<title>"},
    {"name": "model", "description": "switch the model (and its effort)", "hint": "<model> [effort]"},
    {"name": "effort", "description": "set the reasoning effort of the current model", "hint": "low|medium|high|xhigh"},
    {"name": "always-approve", "description": "turn always-approve on or off (no permission prompts)", "hint": ""},
    {"name": "auto", "description": "turn auto-review on or off (a classifier approves safe tools)", "hint": ""},
    {"name": "plan", "description": "enter plan mode", "hint": ""},
    {"name": "view-plan", "description": "show the current saved plan", "hint": ""},
    {"name": "goal", "description": "set, manage or check an autonomous goal", "hint": "[goal]"},
    {"name": "loop", "description": "run a prompt on a recurring interval", "hint": "[interval] <prompt>"},
    {"name": "btw", "description": "send an aside without interrupting the current task", "hint": "<message>"},
    {"name": "copy", "description": "copy the last response", "hint": "[n] [file]"},
    {"name": "export", "description": "export the conversation to a file or the clipboard", "hint": ""},
    {"name": "memory", "description": "browse and manage saved memories", "hint": "[on|off]"},
    {"name": "remember", "description": "save a note to memory now", "hint": "<note>"},
    {"name": "hooks", "description": "view, add, remove and toggle hooks", "hint": ""},
    {"name": "plugins", "description": "view and install plugins", "hint": ""},
    {"name": "skills", "description": "view the installed skills", "hint": ""},
    {"name": "mcps", "description": "manage MCP servers", "hint": ""},
    {"name": "dashboard", "description": "open the agent dashboard of live sessions", "hint": ""},
    {"name": "usage", "description": "view credit usage or manage billing", "hint": ""},
    {"name": "settings", "description": "view and change the configuration", "hint": ""},
    {"name": "doctor", "description": "check terminal, clipboard, input and sandbox support", "hint": ""},
    {"name": "login", "description": "log in or sign in again", "hint": ""},
    {"name": "quit", "description": "quit Grok", "hint": ""},
]

COMMAND_DESCRIPTION_MAX = 160


def _front(path):
    """{key: value} of the front matter of a Markdown file (simple 'key: value' lines between two '---' lines)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(64 * 1024)
    except OSError:
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        k, sep, v = line.partition(":")
        if sep and k.strip() and not line[:1].isspace():
            v = v.strip()
            out[k.strip().lower()] = v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'" else v
    return out


# How each command works from the page (#5220, #5289): tried in Grok 1.0.77 in tmux on 2026-11-11. /model and /effort
# with no argument list their choices (the Settings rows Model and Effort); /session-info and /context show full-screen
# views of the context against the model's window, and /usage its credit usage: the page's Usage panel gives them.
# /resume, /rewind, /dashboard and the extension views are full-screen pickers. The others run at once.
_RUNS = {
    "terminal": {"resume", "rewind", "fork", "dashboard", "hooks", "plugins", "skills", "mcps", "settings", "memory",
                 "doctor", "view-plan", "login"},
}
for _c in COMMANDS:
    _c["run"] = next((k for k, v in _RUNS.items() if _c["name"] in v), "now")
    if _c["name"] in ("model", "effort"):
        _c["setting"], _c["run"] = _c["name"], "terminal"
    if _c["name"] in ("context", "session-info", "usage"):
        _c["page"], _c["run"] = "usage", "terminal"


def context_window(model):
    """The tokens of a Grok model's context window (#5289): its models cache's context_window (grok-4.7: 256000, as
    /session-info shows), or None."""
    m = _models_cache().get(model or "")
    info = m.get("info") if isinstance(m, dict) else None
    got = info.get("context_window") if isinstance(info, dict) else None
    return int(got) if isinstance(got, (int, float)) and got > 0 else None


def chat_cache_line(raw, st):
    """Fold one line of updates.jsonl into the cache state (#5289, the context meter): Grok puts the tokens of the
    context so far in each update's _meta.totalTokens, and the model in a user chunk's _meta.modelId."""
    try:
        x = json.loads(raw)
    except (ValueError, TypeError):
        return
    p = x.get("params") if isinstance(x, dict) else None
    u = (p or {}).get("update") if isinstance(p, dict) else None
    if not isinstance(u, dict):
        return
    if u.get("sessionUpdate") == "user_message_chunk" and (u.get("_meta") or {}).get("modelId"):
        st["model"] = u["_meta"]["modelId"]
    total = (p.get("_meta") or {}).get("totalTokens")
    at = _at(x)
    if isinstance(total, int) and total > 0 and at:
        st["last"] = {"start": at, "at": at, "context": total}


def read_commands(folder):
    """Grok's custom slash commands for a session in folder: its skills (a folder with SKILL.md; user-invocable
    unless its front matter says false) and its legacy command files (commands/*.md), from the folder and its
    parents up to the home folder (.grok, .claude, .agents), then ~/.grok and ~/.claude. A nearer one of the same
    name wins, as in Grok."""
    home = Path.home()
    roots = []
    if folder:
        p = Path(folder).expanduser().resolve()
        while True:
            roots += [p / ".grok", p / ".claude", p / ".agents"]
            if p == home or p.parent == p:
                break
            p = p.parent
    roots += [grok_home(), home / ".claude"]
    out, seen = [], set()
    for root in roots:
        for f in sorted(root.glob("skills/*/SKILL.md")) + sorted(root.glob("commands/*.md")):
            fm = _front(f)
            name = fm.get("name") or (f.parent.name if f.name == "SKILL.md" else f.stem)
            if not name or name in seen or str(fm.get("user-invocable", "true")).lower() == "false":
                continue
            seen.add(name)
            out.append({"name": name, "description": (fm.get("description") or "")[:COMMAND_DESCRIPTION_MAX],
                        "hint": fm.get("argument-hint") or ""})
    return out


# Grok reads AGENTS.md and CLAUDE.md (and Agents.md, AGENT.md, CLAUDE.local.md) in each folder: the CLAUDE.md that
# MaximizePM's setup writes for Claude Code reaches it too, so a second file would say the same twice.
SETUP = {"instructions": "CLAUDE.md", "skills_dir": "~/.grok/skills", "voice": None, "open_link": None}
