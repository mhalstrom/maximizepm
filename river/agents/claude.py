"""The Claude Code adapter (docs/agent-adapters.md). Only its describe part so far: #5181 moves launch, resume and
identity here, #5182 the chat log, #5183 the state (its hooks), #5184 the setup, #5169 the settings and effort."""

DESCRIBE = {
    "id": "claude",
    "label": "Claude Code",
    "exe": "claude",
    "aliases": ["claude-code"],  # the platform name in default_agent, agent_rules, --agent and launch_agents
    "family": "claude",
    # claude --model takes these names as they are; claude_model_ids maps a name to a full id when a person wants one.
    "models": ["haiku", "sonnet", "opus", "fable"],
    "default_model": "",
    "model_ids": {},
    "prefix": "claude_",
}


# ---------------------------------------------------------------------------------------------------------------
# Effort, launch, identity and resume (#5181)
# ---------------------------------------------------------------------------------------------------------------

from pathlib import Path  # noqa: E402

from . import Raw  # noqa: E402

EFFORT = {
    "levels": ["low", "medium", "high", "xhigh", "max"],
    "map": {},
    "flag": ["--effort", "{value}"],
    "live": "/effort {value}",
    "read": r"--effort ([a-z][a-z0-9_-]{0,31})\b",
}

LAUNCH = {"session_name": True, "system_prompt_file": True, "hooks_file": True, "fork": True, "resume": True,
          "autocompact": True, "background": True}

IDENTITY = {"source": "env", "env": "CLAUDE_CODE_SESSION_ID", "log_glob": None, "pane": None, "url": None,
            "inside_env": ["CLAUDECODE"]}


def _q(s):
    from ..core import _shell_quote
    return _shell_quote(s)


def build_command(name, folder, model, effort, settings, prompt):
    """claude [--name N] [--model M] [--effort E] [--permission-mode P] <args> <prompt> [--remote-control N]. The args
    setting goes in as the person wrote it, after what MaximizePM puts before it, in this order: --autocompact,
    --settings (the hooks file), then --resume <id> (a resume) or --resume <base> --fork-session (a fork) after the
    skill's --append-system-prompt-file. The prompt goes before --remote-control: that flag takes an optional session
    name after it. A full model id can end in [1m], a glob in zsh (#4950): every word but the args is quoted."""
    from ..core import skill_prompt_args
    s = settings
    args = s.get("args", "") or ""
    if s.get("resume"):
        args = f"--resume {_q(s['resume'])} " + args
    else:
        if s.get("system_prompt_file"):
            args = skill_prompt_args(args)
        if s.get("fork"):
            args = f"--resume {_q(s['fork'])} --fork-session " + args
    if s.get("hooks_file") and "--settings" not in args:
        args = f"--settings {_q(s['hooks_file'])} " + args
    if s.get("autocompact") and "--autocompact" not in args:
        args = f"--autocompact {s['autocompact']} " + args
    words = ["claude"] + (["--name", name] if name else [])
    words += (["--model", model] if model else []) + ([w.format(value=effort) for w in EFFORT["flag"]] if effort else [])
    words += ["--permission-mode", s["permission_mode"]] if s.get("permission_mode") else []
    words += [Raw(args.strip())] if args.strip() else []
    words.append(prompt)
    words += ["--remote-control"] + ([name] if name else []) if s.get("remote_control") == "on" else []
    return words


def resume_command(session_ref, model, effort, settings, name=None):
    """claude --resume <session id>, with the profile's settings and settings['prompt'] as its first prompt."""
    return build_command(name, None, model, effort, {**settings, "resume": session_ref}, settings.get("prompt"))


def parse_command(words):
    """A hand-written claude command that a profile builds exactly: --model {model}, --effort {effort},
    --remote-control (with no session name after it), --permission-mode <a known mode>, and one prompt. Anything
    else (another flag, a fixed model) keeps it a custom command, so nothing is lost."""
    from ..core import LAUNCH_PLATFORMS
    if not words or Path(words[0]).name.lower() != "claude":
        return None
    modes = LAUNCH_PLATFORMS["claude-code"]["options"]["permission_mode"]["choices"]
    opts, prompt, i = {"remote_control": "off"}, None, 1
    while i < len(words):
        t, nxt = words[i], (words[i + 1] if i + 1 < len(words) else None)
        if t == "--model" and nxt == "{model}" or t == "--effort" and nxt == "{effort}":
            i += 2
        elif t == "--remote-control":
            if nxt is not None and not nxt.startswith("-"):
                return None  # a session name, or a prompt that the flag would take as one
            opts["remote_control"] = "on"
            i += 1
        elif t == "--permission-mode" and nxt in modes:
            opts["permission_mode"] = nxt
            i += 2
        elif not t.startswith("-") and prompt is None and t.strip() and ";" not in t:
            prompt = t
            i += 1
        else:
            return None
    return {"settings": opts, "prompt": prompt} if prompt is not None else None


# ---------------------------------------------------------------------------------------------------------------

# A tool call or a prompt says the session works; PermissionRequest names the tool call that waits on a permission
# prompt, and the Notification permission_prompt says that the prompt shows; Stop says the turn ended (#4288).
HOOK_EVENTS = ("PostToolUse", "UserPromptSubmit", "PermissionRequest", "Notification", "Stop")
HOOK_FILE = "claude-hooks.json"  # beside the queue; claude --settings reads it

STATE = {"state": "hook", "session_id": "hook"}

# The words of its blocked screens: a permission menu ("Do you want to proceed?"), the trust question ("Do you trust
# the files in this folder?"), the end of plan mode ("Would you like to proceed?"), a numbered menu with its marker.
BLOCKED = [r"Do you (want to|trust)", r"Would you like to", r"[Ee]nter to confirm", r"[❯›]\s*\d+\.\s"]

DETECT = {
    "process": "claude",
    "idle": r"^\s*❯(\s|$)",  # the marker of its input box, under a rule
    "working": r"(?i)\besc to interrupt\b",
    "blocked": BLOCKED,
    "tell_instead": r"(?i)\bno\b.*\btell\b",  # "No, and tell Claude what to do differently"
    "image_paste": "[Image #{n}]",  # an image path pasted alone becomes this in its prompt (#4618)
}

INPUT = {"native": None}  # messages reach it by tmux paste (or its Unix socket inbox: core.native_message)


def hook_settings(report):
    """The Claude Code settings (claude --settings) with MaximizePM's hooks: each event runs `report --hook claude`,
    which reads the hook's JSON on stdin. report: the maxpm report command (argv words)."""
    import shlex
    run = [{"type": "command", "command": " ".join(shlex.quote(w) for w in [*report, "--hook", "claude"]),
            "timeout": 10}]
    return {"hooks": {ev: [{"matcher": "", "hooks": run}] if ev in ("PostToolUse", "PermissionRequest")
                      else [{"hooks": run}] for ev in HOOK_EVENTS}}


def install_hooks(folder, report):
    """Write the hook file beside the queue when its text changes; the start adds --settings <file>."""
    import json
    from pathlib import Path
    from ..core import RiverError, river_dir
    f = Path(river_dir()) / HOOK_FILE
    text = json.dumps(hook_settings(report), indent=2) + "\n"
    try:
        if not f.exists() or f.read_text() != text:
            f.write_text(text)
    except OSError as e:
        raise RiverError(f"cannot write the activity hook file {f}: {e}; turn the hooks off: "
                         f"maxpm config set claude_activity_hooks off")
    return ["--settings", str(f)]


_TASK_ENDED = {"completed", "failed", "killed", "stopped", "cancelled", "canceled", "done", "error"}


def _waits(payload):
    """The background work that will wake a session whose turn ended, from the Stop hook's input (Claude Code 2.1 and
    later): background_tasks (a Bash command with run_in_background, a background subagent, a monitor or workflow)
    that has not ended, and session_crons (ScheduleWakeup, CronCreate, /loop). A list of short texts
    ('local_bash: make check'), empty when nothing will wake it; None when the input has neither field (an older
    Claude Code), so MaximizePM looks at the processes instead (#4293)."""
    tasks, crons = payload.get("background_tasks"), payload.get("session_crons")
    if not isinstance(tasks, list) and not isinstance(crons, list):
        return None
    out = []
    for t in tasks if isinstance(tasks, list) else ():
        if not isinstance(t, dict) or str(t.get("status") or "").lower() in _TASK_ENDED:
            continue
        what = next((str(t[k]) for k in ("command", "agent_type", "name", "tool", "description") if t.get(k)), "")
        out.append(f"{t.get('type') or 'task'}: {' '.join(what.split())[:120]}" if what else str(t.get("type") or "task"))
    for c in crons if isinstance(crons, list) else ():
        if isinstance(c, dict):
            out.append(f"wakeup {c.get('schedule') or ''}".strip())
    return out


def read_hook(payload):
    """A Claude Code hook's JSON input as a hook report (river/agents HOOK_REPORT_KEYS), or None for another event."""
    ev = str(payload.get("hook_event_name") or "")
    if ev not in HOOK_EVENTS:
        return None
    ntype = str(payload.get("notification_type") or "")
    sid = payload.get("session_id")
    out = {"event": ev + (f":{ntype}" if ntype else ""), "session_id": sid if isinstance(sid, str) and sid else None}
    if ev == "PostToolUse":
        out["state"] = "working"
    elif ev == "UserPromptSubmit":
        prompt = payload.get("prompt")
        out.update(state="working", prompt=prompt if isinstance(prompt, str) else "")
    elif ev == "PermissionRequest":
        inp = payload.get("tool_input")
        inp = {k: v[:400] for k, v in inp.items() if isinstance(v, str)} if isinstance(inp, dict) else {}
        out.update(state="blocked", call={"tool": str(payload.get("tool_name") or ""), "input": inp})
    elif ev == "Notification":
        if ntype == "permission_prompt":
            out.update(state="blocked", shows=True, message=str(payload.get("message") or "")[:160])
        else:
            out["state"] = None  # idle_prompt, auth_success, ...: only the time
    else:  # Stop
        out.update(state="idle", waits=_waits(payload), again=bool(payload.get("stop_hook_active")))
    return out
