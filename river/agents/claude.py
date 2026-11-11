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
            "inside_env": ["CLAUDECODE"],
            # its session ids are UUIDs (core._SESSION_ID_RE): the name of its transcript, <id>.jsonl
            "id_pattern": r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"}


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


# ---------------------------------------------------------------------------------------------------------------
# Chat log (#5182). Claude Code writes <config dir>/projects/<folder>/<session id>.jsonl, one JSON object a line:
# a user or assistant line holds a message; a model call writes one line per content block, each with the call's usage.
# ---------------------------------------------------------------------------------------------------------------

import json as _json  # noqa: E402
import re as _re  # noqa: E402
from datetime import timedelta as _timedelta  # noqa: E402

from .. import core as _core  # noqa: E402


def _s():
    """river.sessions, imported when first used: core imports the adapters while it loads, before sessions can."""
    from .. import sessions
    return sessions

CHAT = {"compressed": False, "file_tools": ["Write", "Edit", "MultiEdit", "NotebookEdit"], "ref": "session",
        "cache_write": True}

# A line this much older than the first line of a transcript was copied from the session a fork started from.
FORK_SLACK = 30


def chat_path(session_ref, folder=None, root=None):
    """The newest transcript of the session id in the transcripts folder (core.transcripts_root), or None."""
    return _s().newest(Path(root or _core.transcripts_root()).glob(f"*/{session_ref}.jsonl"))


def parse_chat_line(d):
    """The page lines of one Claude Code transcript line, and its turn state (working, idle, or None)."""
    # A compaction (#5120): Claude Code keeps the lines before it in the file and writes a compact_boundary line, then
    # the summary as a user line. The chat shows a divider, and the summary folded under it, never as the person's turn.
    if d.get("type") == "system" and d.get("subtype") == "compact_boundary" and not d.get("isSidechain"):
        return [{"at": d.get("timestamp"), "role": "divider", "kind": "compacted", "text": "Compacted"}], None
    if d.get("type") not in ("user", "assistant") or d.get("isSidechain") or d.get("isMeta"):
        return [], None
    m = d.get("message") if isinstance(d.get("message"), dict) else {}
    at, content, out = d.get("timestamp"), m.get("content"), []
    if d.get("isCompactSummary"):
        text = content if isinstance(content, str) else _s()._parts_text(content, ("text",))
        return [{"at": at, "role": "summary", "text": _s()._clip(text.strip(), _s().TEXT_MAX)}], None
    if d["type"] == "assistant":
        for x in content if isinstance(content, list) else []:
            if not isinstance(x, dict):
                continue
            if x.get("type") == "text" and x.get("text", "").strip():
                out.append({"at": at, "role": "assistant", "text": _s()._clip(x["text"], _s().TEXT_MAX)})
            elif x.get("type") == "tool_use":
                out.append({"at": at, "role": "tool", "text": _s()._tool_line(x.get("name"), x.get("input")),
                            "plain": _s().plain_tool(x.get("name"), x.get("input")),
                            "id": x.get("id"), "detail": _s()._detail(x.get("input"))})
        stop = m.get("stop_reason")
        return out, ("idle" if stop in ("end_turn", "stop_sequence") else "working" if stop else None)
    for x in content if isinstance(content, list) else []:
        if isinstance(x, dict) and x.get("type") == "tool_result" and x.get("tool_use_id"):
            out.append({"at": at, "role": "result", "for": x["tool_use_id"], "error": bool(x.get("is_error")),
                        "text": _s()._clip(_s()._result_text(x.get("content")), _s().DETAIL_MAX)})
    text = _s()._parts_text(content, ("text",)).strip()
    if text.startswith("<task-notification>"):
        out.append({"at": at, "role": "event", "text": "a background task ended"})
    elif (text and not text.startswith("<")) or any(isinstance(x, dict) and x.get("type") == "image"
                                                   for x in content if isinstance(content, list)):
        # tags the CLI adds (a command's name and output, reminders) are no turn of the person
        out.append(_s()._user_turn(at, text, sum(1 for x in content if isinstance(x, dict) and x.get("type") == "image")
                                 if isinstance(content, list) else 0))
    return out, "working"


def parse_chat(lines):
    return _s().parse_lines(parse_chat_line, lines)


def chat_chain(path, refs=None):
    """The transcripts of one chat, oldest first, path last (#5120): a session that Claude Code resumed into a new file
    (a new session id of the agent) goes on in the same chat. Only files in the folder of the newest one count: a
    session id that another session recorded for this agent is not its chat."""
    out = []
    for sid in refs or ():
        p = Path(path).parent / f"{sid}.jsonl"
        if p != Path(path) and p not in out and p.is_file():
            out.append(p)
    return out + [Path(path)]


def chat_title(lines):
    """The session title: the last custom-title record, or None."""
    for line in reversed(lines):
        if "custom-title" not in line:
            continue
        try:
            d = _json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("type") == "custom-title" and isinstance(d.get("customTitle"), str):
            return d["customTitle"]
    return None


def chat_settings(lines):
    """The settings the transcript shows last: Claude Code puts the model and effort on each reply and a line for each
    permission mode."""
    got = {}
    for line in lines:
        if '"permission-mode"' in line:
            m = _re.search(r'"permissionMode":\s*"([^"]+)"', line)
            if m:
                got["permission_mode"] = m.group(1)
        elif line.startswith('{"') and '"assistant"' in line:
            try:
                d = _json.loads(line)
            except ValueError:
                continue
            if d.get("type") != "assistant":
                continue
            msg = d.get("message") if isinstance(d.get("message"), dict) else {}
            if msg.get("model"):
                got["model"] = msg["model"]
            if isinstance(d.get("effort"), str):
                got["effort"] = d["effort"]
    return got


def chat_cache_line(raw, st):
    """Fold one transcript line (bytes) into the prompt cache state st (#5023): the start of the last model call (the
    line before its answer: the person's prompt or a tool result), its context, and the cache lifetime in force (the
    writes of the last call that wrote: 1h or 5m; a read keeps the lifetime of the entry it read)."""
    if b'"user"' not in raw and b'"assistant"' not in raw:  # a quick test before the line is decoded
        return
    try:
        d = _json.loads(raw)
    except ValueError:
        return
    if not isinstance(d, dict) or not isinstance(d.get("timestamp"), str) or d.get("isSidechain"):
        return
    at = d["timestamp"]
    m = d.get("message") if isinstance(d.get("message"), dict) else {}
    if d.get("type") == "user":
        st["sent"] = at
        return
    u = m.get("usage")
    if d.get("type") != "assistant" or not isinstance(u, dict) or m.get("model") == "<synthetic>":
        return
    context = sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
                                           "output_tokens"))
    if m.get("id") != st.get("mid"):  # a new model call; its lines after the first repeat its usage
        st["mid"] = m.get("id")
        st["last"] = {"start": st.get("sent") or at, "at": at, "context": context}
    else:
        st["last"] = dict(st["last"], at=at, context=context)
    cc = u.get("cache_creation") or {}
    if (cc.get("ephemeral_1h_input_tokens") or 0) > 0:
        st["ttl"] = "1h"
    elif (cc.get("ephemeral_5m_input_tokens") or 0) > 0:
        st["ttl"] = "5m"
    st["model"] = m.get("model") or st["model"]


# Lines that never give an Artifacts hit, skipped before they are decoded: tool results and Claude Code's copies of
# the API requests. JSON escapes a quote in text, so these words in a message do not match.
_TEXTS_SKIP = (b'"type":"tool_result"', b'"type":"api-request')


def chat_texts(raw):
    """The texts of one transcript line (bytes) for the Artifacts tab: the text of the person's and the session's turns,
    the path of each file a CHAT['file_tools'] call wrote, and the input of each other tool call."""
    if any(k in raw for k in _TEXTS_SKIP):
        return []
    try:
        d = _json.loads(raw)
    except ValueError:
        return []
    if not isinstance(d, dict):
        return []
    m = d.get("message") if isinstance(d.get("message"), dict) else None
    if not m or d.get("type") not in ("user", "assistant") or d.get("isSidechain"):
        return []
    out, at, content = [], d.get("timestamp"), m.get("content")
    if isinstance(content, str):
        out.append((content, at, "named"))
    for x in content if isinstance(content, list) else []:
        if not isinstance(x, dict):
            continue
        if x.get("type") == "text":
            out.append((x.get("text", ""), at, "named"))
        elif x.get("type") == "tool_use":
            inp = x.get("input") if isinstance(x.get("input"), dict) else {}
            if x.get("name") in CHAT["file_tools"] and inp.get("file_path"):
                out.append((" " + str(inp["file_path"]), at, "wrote"))
            else:
                out.append((_json.dumps(inp, ensure_ascii=False), at, "named"))
    return out


def _usage(u):
    """A transcript usage dict in the shared form of chat_usage."""
    cc = u.get("cache_creation") or {}
    w1, w5 = cc.get("ephemeral_1h_input_tokens"), cc.get("ephemeral_5m_input_tokens")
    if w1 is None and w5 is None:
        w1, w5 = u.get("cache_creation_input_tokens") or 0, 0
    return {"input": u.get("input_tokens") or 0, "output": u.get("output_tokens") or 0,
            "cache_read": u.get("cache_read_input_tokens") or 0,
            "cache_write": u.get("cache_creation_input_tokens") or 0, "write_1h": w1 or 0, "write_5m": w5 or 0}


def read_transcript(path):
    """(requests, fork) of one transcript file. requests: {message id: (time, usage)}, each API request once
    (Claude Code writes one line per content block, with the same usage). A fork (claude --resume <id>
    --fork-session) starts with lines copied from its base, with the base's older times: they are left out,
    and fork is True. Copied lines come before the session's own first request: a line there is copied when
    it is FORK_SLACK older than the first line, or when the first user message is older than the start line
    before it: claude -p writes a queue line when it starts, an interactive session a file-history snapshot
    (its time inside the snapshot). Not the file's birth time: files are written again."""
    reqs, first, fork, dequeued, leading, users = {}, None, False, None, True, 0
    slack = _timedelta(seconds=FORK_SLACK)
    with open(path, errors="replace") as f:
        for line in f:
            if not leading and ('"usage"' not in line or '"assistant"' not in line):
                continue
            try:
                d = _json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            snap = d.get("snapshot") if d.get("type") == "file-history-snapshot" else None
            if leading and isinstance(snap, dict) and isinstance(snap.get("timestamp"), str) and users == 0:
                dequeued = _core._ts(snap["timestamp"]) or dequeued
                continue
            t = _core._ts(d["timestamp"]) if isinstance(d.get("timestamp"), str) else None
            if t is None:
                continue
            if leading:
                first = first or t
                if d.get("type") == "queue-operation":
                    dequeued = t
                    continue
                users += d.get("type") == "user"
                before_queue = dequeued and t < dequeued - _timedelta(seconds=1)
                if t < first - slack or (before_queue and (fork or (users == 1 and d.get("type") == "user"))):
                    fork = True
                    continue
            msg = d.get("message") if isinstance(d.get("message"), dict) else {}
            if d.get("type") == "assistant" and isinstance(msg.get("usage"), dict):
                reqs.setdefault(msg.get("id") or d["timestamp"], (t, _usage(msg["usage"])))
                leading = False
    return reqs, fork


def chat_requests(session_ref, root=None):
    """All requests of a Claude Code session, its subagents' too, and whether it started as a fork; None when no
    transcript of it is on this computer."""
    mains = sorted(Path(root or _core.transcripts_root()).glob(f"*/{session_ref}.jsonl"))
    if not mains:
        return None
    reqs, fork = {}, False
    for p in mains:
        r, f = read_transcript(p)
        reqs.update(r)
        fork = fork or f
        for sub in sorted(p.parent.glob(f"{session_ref}/subagents/*.jsonl")):
            reqs.update(read_transcript(sub)[0])
    return reqs, fork


# ---------------------------------------------------------------------------------------------------------------
# Settings (#5169): the launch profile options (each also the config key claude_<key>), and the Settings dialog of a
# running session. build_command reads them by key.
# ---------------------------------------------------------------------------------------------------------------

DESCRIBE["model_live"] = "/model {value}"
DESCRIBE["model_read"] = r"--model (\S+)"

SETTINGS = [
    {"key": "remote_control", "label": "Remote Control", "kind": "toggle", "default": "on",
     "text": "Remote Control: the session gets a web link that notifications and Open chat use",
     "flag": ["--remote-control"], "session": True},
    {"key": "permission_mode", "label": "Permission mode", "kind": "choice", "default": "",
     "choices": ["acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"],
     "text": "permission mode (--permission-mode); empty: Claude Code's own setting",
     "flag": ["--permission-mode", "{value}"], "read": r"--permission-mode (\S+)", "session": True,
     "note": "Shift+Tab in its terminal also cycles it"},
    {"key": "model_ids", "label": "Model ids", "kind": "text", "default": "",
     "text": "the id Claude Code gets for a model_ladder name (name=id, ...); a name with no entry goes as it is: "
             "claude --model takes haiku, sonnet, opus, and fable"},
    {"key": "args", "label": "Arguments", "kind": "text", "default": "", "text": "more arguments, put before the prompt"},
    {"key": "skill_prompt", "label": "Skill prompt", "kind": "toggle", "default": "on",
     "text": "the maxpm skill in the system prompt (--append-system-prompt-file, with the file args names, if any): "
             "each session reads it from the prompt cache, and does not load it again"},
    {"key": "prompt", "label": "Prompt", "kind": "text", "default": "go", "text": "the first prompt"},
    {"key": "activity_hooks", "label": "Activity hooks", "kind": "toggle", "default": "on",
     "text": "hooks that report the session's tool calls, permission prompts and turn ends to MaximizePM (--settings "
             "with the hook file; maxpm report --hook claude), so the manager hears of a session that stands still"},
]


# ---------------------------------------------------------------------------------------------------------------
# 10. Commands (#5186): the "/" list of the Sessions message box
# ---------------------------------------------------------------------------------------------------------------

# The built-in slash commands that a person types most, with the descriptions of Claude Code 2.1.322 (read from its
# program). Not every one: commands for one platform, for a paid plan's setup, or for its own tests stay out.
COMMANDS = [
    {"name": "add-dir", "description": "Add a new working directory", "hint": "<path>"},
    {"name": "agents", "description": "Manage agent configurations", "hint": ""},
    {"name": "branch", "description": "Create a branch of the current conversation at this point", "hint": ""},
    {"name": "btw", "description": "Ask a quick side question without interrupting the main conversation",
     "hint": "<question>"},
    {"name": "clear", "description": "Start a new session with empty context; previous session stays on disk "
                                     "(resumable with /resume)", "hint": ""},
    {"name": "compact", "description": "Free up context by summarizing the conversation so far",
     "hint": "[instructions]"},
    {"name": "config", "description": "Open settings", "hint": ""},
    {"name": "context", "description": "Visualize current context usage as a colored grid", "hint": ""},
    {"name": "copy", "description": "Copy Claude's last response to clipboard (or /copy N for the Nth-latest)",
     "hint": "[N]"},
    {"name": "cost", "description": "Show the total cost and duration of the current session", "hint": ""},
    {"name": "doctor", "description": "Diagnose and verify your Claude Code installation and settings", "hint": ""},
    {"name": "effort", "description": "Set effort level for model usage", "hint": "[low|medium|high|xhigh|max]"},
    {"name": "exit", "description": "Exit the REPL", "hint": ""},
    {"name": "export", "description": "Export the current conversation to a file or clipboard", "hint": "[filename]"},
    {"name": "feedback", "description": "Send feedback to Anthropic or report a bug", "hint": ""},
    {"name": "fork", "description": "Copy this conversation into a new background session and keep working here",
     "hint": ""},
    {"name": "goal", "description": "Set a goal Claude checks before stopping", "hint": "<goal>"},
    {"name": "help", "description": "Show help and available commands", "hint": ""},
    {"name": "hooks", "description": "View hook configurations for tool events", "hint": ""},
    {"name": "ide", "description": "Manage IDE integrations and show status", "hint": ""},
    {"name": "init", "description": "Initialize a new CLAUDE.md file with codebase documentation", "hint": ""},
    {"name": "insights", "description": "Generate a report analyzing your Claude Code sessions", "hint": ""},
    {"name": "keybindings", "description": "Open your keyboard shortcuts file", "hint": ""},
    {"name": "login", "description": "Sign in with your Anthropic account", "hint": ""},
    {"name": "logout", "description": "Sign out from your Anthropic account", "hint": ""},
    {"name": "mcp", "description": "Manage MCP servers", "hint": ""},
    {"name": "memory", "description": "Edit CLAUDE.md files and memory settings", "hint": ""},
    {"name": "model", "description": "Set the AI model for Claude Code", "hint": "[model]"},
    {"name": "output-style", "description": "Set the output style", "hint": "[style]"},
    {"name": "permissions", "description": "Manage allow and deny tool permission rules", "hint": ""},
    {"name": "plan", "description": "Enable plan mode or view the current session plan", "hint": ""},
    {"name": "plugin", "description": "Manage Claude Code plugins", "hint": ""},
    {"name": "privacy-settings", "description": "View and update your privacy settings", "hint": ""},
    {"name": "recap", "description": "Generate a one-line session recap now", "hint": ""},
    {"name": "release-notes", "description": "View release notes", "hint": ""},
    {"name": "reload-plugins", "description": "Activate pending plugin changes in the current session", "hint": ""},
    {"name": "reload-skills", "description": "Pick up skills added or changed on disk during this session",
     "hint": ""},
    {"name": "resume", "description": "Resume a conversation", "hint": "[session]"},
    {"name": "review", "description": "Review a pull request", "hint": "[pr]"},
    {"name": "rewind", "description": "Restore the code and/or conversation to a previous point", "hint": ""},
    {"name": "sandbox", "description": "Configure the sandbox of Bash commands", "hint": ""},
    {"name": "security-review", "description": "Complete a security review of the pending changes on the current "
                                               "branch", "hint": ""},
    {"name": "skills", "description": "List available skills", "hint": ""},
    {"name": "status", "description": "Show Claude Code status including version, model, account, API "
                                      "connectivity, and tool statuses", "hint": ""},
    {"name": "statusline", "description": "Set up Claude Code's status line UI", "hint": ""},
    {"name": "tasks", "description": "List and manage background tasks", "hint": ""},
    {"name": "terminal-setup", "description": "Install the Shift+Enter key binding for newlines", "hint": ""},
    {"name": "theme", "description": "Change the theme", "hint": ""},
    {"name": "todos", "description": "List current todo items", "hint": ""},
    {"name": "upgrade", "description": "Upgrade your plan for higher rate limits and more Opus", "hint": ""},
    {"name": "usage", "description": "Show plan usage limits", "hint": ""},
    {"name": "vim", "description": "Toggle between Vim and Normal editing modes", "hint": ""},
    {"name": "voice", "description": "Toggle voice mode", "hint": "[hold|tap|off]"},
]


def _config_dir():
    import os
    from pathlib import Path
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _enabled_plugins(dirs):
    """{plugin key 'name@marketplace': on} from enabledPlugins of the user's settings, then each project's
    .claude/settings.json and settings.local.json, the folder nearest the session last (it wins)."""
    import json
    files = [_config_dir() / "settings.json"]
    for d in reversed(dirs):
        files += [d / ".claude" / "settings.json", d / ".claude" / "settings.local.json"]
    on = {}
    for f in files:
        try:
            got = json.loads(f.read_text(encoding="utf-8")).get("enabledPlugins")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(got, dict):
            on.update({k: bool(v) for k, v in got.items()})
    return on


def _plugin_commands(dirs):
    """The commands and skills of each installed and enabled plugin, as '<plugin>:<name>' (installed_plugins.json
    names each plugin's folder; a project's install counts only for a session in that project)."""
    import json
    from pathlib import Path
    from . import slash
    try:
        installed = json.loads((_config_dir() / "plugins" / "installed_plugins.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    on, mine = _enabled_plugins(dirs), {str(d) for d in dirs}
    out = []
    for key, installs in sorted((installed.get("plugins") or {}).items()):
        if not on.get(key) or not isinstance(installs, list):
            continue
        for inst in installs:
            if not isinstance(inst, dict) or not inst.get("installPath"):
                continue
            if inst.get("scope") not in (None, "user") and str(inst.get("projectPath") or "") not in mine:
                continue
            base, prefix = Path(inst["installPath"]), key.split("@")[0] + ":"
            out += slash.md_commands(base / "commands", prefix) + slash.skill_commands(base / "skills", prefix)
            break
    return out


def read_commands(folder):
    """The custom slash commands of a session that runs in folder: the commands of .claude/commands and the skills
    of .claude/skills in folder and its parents up to the repository root, then the user's (~/.claude/commands,
    ~/.claude/skills), then the enabled plugins' ('<plugin>:<name>'). A name that comes twice: the nearer one."""
    from . import slash
    dirs = slash.project_dirs(folder)
    got = []
    for d in dirs:
        got += slash.md_commands(d / ".claude" / "commands")
    got += slash.md_commands(_config_dir() / "commands")
    for d in dirs:
        got += slash.skill_commands(d / ".claude" / "skills")
    got += slash.skill_commands(_config_dir() / "skills")
    got += _plugin_commands(dirs)
    built_in = {c["name"] for c in COMMANDS}
    return slash.unique(e for e in got if e["name"] not in built_in)


# ---------------------------------------------------------------------------------------------------------------
# 11. Setup (#5184)
# ---------------------------------------------------------------------------------------------------------------

SETUP = {
    "instructions": "CLAUDE.md",
    # CLAUDE.md that holds the one line @AGENTS.md imports it: Claude Code reads the same rules as other agents.
    "import": "@{file}",
    "skills_dir": "~/.claude/skills",
    # Voice (#4626): Claude Code records the microphone of the computer it runs on and takes no audio from outside
    # (checked in Claude Code 2.1.319), so the page dictates into its message box as for every agent (#5107).
    "voice": ("/voice [hold|tap|off] in its terminal: hold-to-talk dictation into its prompt; it records the microphone "
              "of the computer it runs on (SoX) and needs a claude.ai login; it takes no audio from outside"),
    "open_link": "Open on claude.ai",
    "open_title": "Open this session on claude.ai/code",
    "no_link": "This session has no Remote Control link and runs in no tmux pane here",
}


def desktop_config_path():
    """Where the Claude desktop app keeps its MCP servers."""
    import os
    import sys
    from pathlib import Path
    if sys.platform == "darwin":
        return Path("~/Library/Application Support/Claude/claude_desktop_config.json").expanduser()
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path("~/AppData/Roaming").expanduser(), "Claude",
                    "claude_desktop_config.json")
    return Path("~/.config/Claude/claude_desktop_config.json").expanduser()


def write_mcp_config(path=None, remove=False):
    """Add (or remove) the MaximizePM entry in the Claude desktop app's config (maxpm setup-agent --claude-desktop);
    other servers and settings stay. The old file is kept as <name>.bak when it changes."""
    import json
    from pathlib import Path
    from . import setup_entry
    from ..core import RiverError
    f = Path(path).expanduser() if path else desktop_config_path()
    old = f.read_text() if f.exists() else ""
    try:
        data = json.loads(old) if old.strip() else {}
    except ValueError as e:
        raise RiverError(f"{f} is not valid JSON ({e}); fix it first, MaximizePM does not overwrite it")
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        raise RiverError(f"{f}: expected an object with an mcpServers object; fix it first")
    servers = data.setdefault("mcpServers", {})
    if remove:
        if "maxpm" not in servers:
            return f"{f} has no MaximizePM entry; nothing changed"
        del servers["maxpm"]
    else:
        entry = setup_entry()
        if servers.get("maxpm") == entry:
            return f"MaximizePM is already in {f}; nothing changed"
        servers["maxpm"] = entry
    new = json.dumps(data, indent=2) + "\n"
    if old:
        f.with_name(f.name + ".bak").write_text(old)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(new)
    if remove:
        return f"took MaximizePM out of {f} (the old file: {f.name}.bak). Quit and reopen the Claude desktop app."
    return (f"added MaximizePM to {f}" + (f" (the old file: {f.name}.bak)" if old else "") + ".\n"
            "Quit and reopen the Claude desktop app. Then ask it, for example: \"plan my next work with MaximizePM\", "
            "\"what waits on me in MaximizePM?\", or \"take a writing item from MaximizePM\".")
