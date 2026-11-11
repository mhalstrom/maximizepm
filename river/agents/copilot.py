"""The GitHub Copilot CLI adapter (docs/agent-adapters.md, #5176): the program `copilot` (npm @github/copilot).

Read from Copilot CLI 1.0.109 on macOS, 2026-11-11 (copilot --help, copilot help <topic>, and real sessions in tmux;
the fixtures in tests/fixtures/copilot were recorded then):

- State and session id come from hooks (STATE). install_hooks writes a small plugin beside the queue
  (<queue folder>/copilot-plugin: plugin.json, hooks.json, maxpm_hook.py), and the start loads it with
  --plugin-dir, so only the sessions that MaximizePM starts run it. Its hooks call maxpm report:
  userPromptSubmitted and postToolUse(Failure) working; agentStop idle; notification permission_prompt and
  elicitation_dialog blocked (the menu shows then); sessionEnd maxpm release. sessionStart reports the session id
  and the resume command. permissionRequest is no signal: it runs also for a call that Copilot allows by itself.
- Copilot runs sessionStart at the first prompt, not at the start. Before it, a new folder shows the folder trust
  question, which no hook reports: DETECT names that screen, for the pane watcher's prompt layout only.
- A session is the folder ~/.copilot/session-state/<id>/ ($COPILOT_HOME), its chat log events.jsonl. A resume
  (copilot --resume <id>) appends to the same file, after a session.resume line.
- Copilot also reads a project's Claude Code files: CLAUDE.md, .claude/commands, .claude/skills, and the hooks of
  .claude/settings.json under Claude Code's event names. A project whose .claude/settings.json runs maxpm hooks
  runs them in Copilot too.
"""

import json
import os
import re
from pathlib import Path

from . import Raw

DESCRIBE = {
    "id": "copilot",
    "label": "GitHub Copilot CLI",
    "exe": "copilot",
    "aliases": ["copilot-cli", "github-copilot"],
    # Copilot serves models of several makers under its own ids, as its /model picker listed them on 2026-11-11;
    # "auto" is Copilot Fusion, which picks one for each task (the only model of Copilot Free). Which ids a person
    # may use depends on the Copilot plan, so no model_ladder limit applies.
    "family": "multi",
    "models": ["auto", "claude-opus-5.5", "claude-sonnet-5.5", "claude-fable-5.2", "gpt-6.1-sol", "gpt-6.1-terra",
               "gpt-6-luna", "gpt-5.6-sol"],
    "default_model": "",
    "model_ids": {},
    "prefix": "copilot_",
    "model_live": "/model {value}",  # /model takes a model id in a running session
    "model_read": r"--model (\S+)",
}

# ---------------------------------------------------------------------------------------------------------------
# Settings, effort, launch, identity and resume
# ---------------------------------------------------------------------------------------------------------------

SETTINGS = [
    {"key": "permissions", "label": "Permissions", "kind": "choice",
     "choices": ["assisted-approval", "allow-all-tools", "allow-all"], "default": "",
     "text": "empty: Copilot asks before each tool call that changes something; assisted-approval: its safety judge "
             "decides; allow-all-tools: no question for tools; allow-all: also any path and URL",
     "flag": ["--{value}"], "live": None, "read": r"--(assisted-approval|allow-all-tools|allow-all)\b",
     "session": True},
    {"key": "mode", "label": "Mode", "kind": "choice", "choices": ["interactive", "plan", "autopilot"],
     "default": "", "text": "the agent mode at the start; plan writes a plan first, autopilot goes on by itself",
     "flag": ["--mode", "{value}"], "live": None, "read": r"--mode (interactive|plan|autopilot)\b",
     "session": True},
    {"key": "args", "label": "More arguments", "kind": "text", "choices": [], "default": "",
     "text": "more copilot arguments, as shell text", "flag": [], "live": None, "read": None},
    {"key": "prompt", "label": "Prompt", "kind": "text", "choices": [],
     "default": "run maxpm go in this folder and follow the briefing",
     "text": "the first prompt (copilot -i); Copilot also reads the project's AGENTS.md and CLAUDE.md",
     "flag": [], "live": None, "read": None},
]

EFFORT = {
    "label": "Reasoning effort",
    # copilot --reasoning-effort; which levels a model takes depends on the model (none and minimal too).
    "levels": ["none", "minimal", "low", "medium", "high", "xhigh", "max"],
    "map": {},
    "flag": ["--reasoning-effort", "{value}"],
    "live": None,  # the /model picker changes it with the arrow keys; no typed command
    # The effort on the session's command line (--reasoning-effort low; the Settings dialog reads it there, #5258),
    # or in a session.model_change line of events.jsonl.
    "read": r'(?:--reasoning-effort[= ]|"reasoningEffort":\s*")([a-z]+)',
}

# A session name only for a new session (--name); the queue folder as a folder it may use (--add-dir).
LAUNCH = {"session_name": True, "extra_dirs": True, "resume": True}

# The hook reports the id (sessionStart); COPILOT_CLI is set in the environment of its hooks and commands.
IDENTITY = {"source": "hook", "env": None, "log_glob": None, "pane": None,
            "url": None, "inside_env": ["COPILOT_CLI"]}


def build_command(name, folder, model, effort, settings, prompt):
    """copilot [--resume <id> | --name N] [--model M] [--reasoning-effort E] [--<permissions>] [--mode M]
    --add-dir <queue folder> <args> [-i <prompt>]. -i starts the interactive session and runs the prompt; with no
    prompt it waits at its own prompt. A resumed session keeps its name, so --name goes only on a new one."""
    from ..core import river_dir
    s = settings
    words = ["copilot"]
    words += ["--resume", s["resume"]] if s.get("resume") else (["--name", name] if name else [])
    words += (["--model", model] if model else [])
    words += ([w.format(value=effort) for w in EFFORT["flag"]] if effort else [])
    words += [f"--{s['permissions']}"] if s.get("permissions") else []
    words += ["--mode", s["mode"]] if s.get("mode") else []
    words += ["--add-dir", river_dir()]
    words += [Raw(s["args"].strip())] if (s.get("args") or "").strip() else []
    words += ["-i", prompt] if prompt else []
    return words


def resume_command(session_ref, model, effort, settings, name=None):
    """copilot --resume <session id>, with the profile's settings and settings['prompt'] as its first prompt."""
    if not session_ref:
        return None
    return build_command(name, None, model, effort, {**settings, "resume": session_ref}, settings.get("prompt"))


def parse_command(words):
    """A hand-written copilot command that a profile builds exactly: --model {model}, --reasoning-effort {effort},
    a known permission flag, --mode with a known mode, --add-dir of the queue folder, and -i <prompt>."""
    from ..core import river_dir
    if not words or Path(words[0]).name.lower() != "copilot":
        return None
    perms = SETTINGS[0]["choices"]
    modes = SETTINGS[1]["choices"]
    opts, prompt, i = {}, None, 1
    while i < len(words):
        t, nxt = words[i], (words[i + 1] if i + 1 < len(words) else None)
        if t == "--model" and nxt == "{model}" or t == "--reasoning-effort" and nxt == "{effort}":
            i += 2
        elif t.startswith("--") and t[2:] in perms and "permissions" not in opts:
            opts["permissions"] = t[2:]
            i += 1
        elif t == "--mode" and nxt in modes:
            opts["mode"] = nxt
            i += 2
        elif t == "--add-dir" and nxt is not None and (
                nxt == "{maxpm_dir}" or os.path.realpath(os.path.expanduser(nxt)) == os.path.realpath(river_dir())):
            i += 2
        elif t in ("-i", "--interactive") and nxt and prompt is None and ";" not in nxt:
            prompt = nxt
            i += 2
        else:
            return None
    return {"settings": opts, "prompt": prompt} if prompt is not None else None


# ---------------------------------------------------------------------------------------------------------------
# State: the hook plugin
# ---------------------------------------------------------------------------------------------------------------

STATE = {"state": "hook", "session_id": "hook"}

# The events the plugin hooks, each with the state it reports (None: maxpm_hook.py decides from the payload).
HOOK_EVENTS = {"sessionStart": "idle", "userPromptSubmitted": "working", "postToolUse": "working",
               "postToolUseFailure": "working", "notification": None, "agentStop": "idle", "sessionEnd": "gone"}

PLUGIN_DIR = "copilot-plugin"

# The hook program. It runs alone (sys.executable of maxpm, no import of river): it reads the payload on stdin,
# finds what to report, and runs the maxpm report command that follows its two first arguments. It prints nothing
# (Copilot reads a hook's output as a decision) and never fails the hook.
HOOK_SCRIPT = r'''"""MaximizePM's hook for GitHub Copilot CLI (written by river/agents/copilot.py; changes are lost)."""
import json
import os
import subprocess
import sys

STATES = %(states)s
BLOCKED = {"permission_prompt", "elicitation_dialog"}


def report_args(event, payload):
    """The words after `maxpm` for one hook call, or None when it reports nothing."""
    sid = payload.get("sessionId") or payload.get("session_id")
    seq = payload.get("timestamp")
    state = STATES.get(event)
    if event == "notification":
        kind = payload.get("notification_type") or payload.get("notificationType")
        if kind in BLOCKED:
            state = "blocked"
        elif kind == "agent_idle":
            state = "idle"
    if state is None:
        return None
    if state == "gone":
        return ["release"]
    out = ["report", "--state", state]
    if state == "blocked":
        out += ["--message", str(payload.get("message") or payload.get("title") or "Copilot asks")[:400]]
    if sid:
        out += ["--session-id", str(sid)]
    if isinstance(seq, int) and not isinstance(seq, bool):
        out += ["--seq", str(seq)]
    if event == "sessionStart" and sid:
        out += ["--", "copilot", "--resume", str(sid)]
    return out


def main(argv):
    event, maxpm = argv[1], argv[2:]
    if not os.environ.get("MAXPM_AGENT"):
        return
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    args = report_args(event, payload if isinstance(payload, dict) else {})
    if args:
        subprocess.run(maxpm + args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=8)


if __name__ == "__main__":
    try:
        main(sys.argv)
    except Exception:  # noqa: BLE001 - a hook never stops Copilot
        pass
''' % {"states": repr({k: v for k, v in HOOK_EVENTS.items() if v})}


def plugin_files(report, folder, python=None):
    """{file name: text} of the hook plugin in `folder`. report: the argv words of `maxpm` (the program and any flags
    before the command), which the hook program runs with report or release after them. Each hook runs the program
    with no shell (exec), so no quoting differs between macOS, Linux and Windows."""
    import sys
    exe = python or sys.executable
    hooks = {e: [{"type": "command", "exec": exe, "args": [str(Path(folder) / "maxpm_hook.py"), e, *report], "timeoutSec": 10}]
             for e in HOOK_EVENTS}
    return {
        "plugin.json": json.dumps({"name": "maxpm", "version": "1.0.0",
                                   "description": "MaximizePM: reports the session's state with maxpm report"},
                                  indent=2) + "\n",
        "hooks.json": json.dumps({"version": 1, "hooks": hooks}, indent=2) + "\n",
        "maxpm_hook.py": HOOK_SCRIPT,
    }


def install_hooks(folder, report):
    """Write the hook plugin beside the queue (each file only when its text changes) and return the words that load
    it: --plugin-dir <its folder>. report is the maxpm command (argv words) that each hook runs; a trailing
    'report' word is dropped, since the hook adds report or release itself."""
    from ..core import RiverError, river_dir
    words = list(report)
    if words and words[-1] == "report":
        words = words[:-1]
    d = Path(river_dir()) / PLUGIN_DIR
    try:
        d.mkdir(parents=True, exist_ok=True)
        for fname, text in plugin_files(words, d).items():
            f = d / fname
            if not f.exists() or f.read_text() != text:
                f.write_text(text)
    except OSError as e:
        raise RiverError(f"cannot write the Copilot hook plugin in {d}: {e}")
    return ["--plugin-dir", str(d)]


# Screen rules: the state comes from the hooks, but the pane watcher reads the prompt layout from these (the trust
# question comes before any hook), and the input part uses tell_instead.
DETECT = {
    "process": "copilot",
    "idle": r"/ commands · \? help",
    "working": r"ctrl\+c interrupt",
    "blocked": [r"Do you want to run this command\?", r"Do you trust the files in this folder\?",
                r"Copilot needs information\.", r"↑/↓ to navigate · enter to select · esc to cancel",
                r"enter accept · ctrl\+d decline"],
    "tell_instead": r"No, and tell Copilot what to do differently",
    "image_paste": None,
    # The key lines of its menus: "↑/↓ to navigate · enter to select · esc to cancel" and, in the ask_user form,
    # "↑/↓ select · enter accept · ctrl+d decline · esc cancel".
    "hint": r"\benter to (select|confirm)\b|\benter accept\b",
}

INPUT = {"native": None}

# ---------------------------------------------------------------------------------------------------------------
# Chat log: ~/.copilot/session-state/<id>/events.jsonl
# ---------------------------------------------------------------------------------------------------------------

# create and edit write files; bash runs commands (its writes are not known). A session's log is found by its id
# (the hooks report it). Copilot's usage lines count cache writes apart (cacheWriteTokens; its prompt cache lives 30
# minutes, session.usage_checkpoint), so a call after the cache ran out reads the context at the cache write price.
CHAT = {"compressed": False, "file_tools": ["create", "edit"], "ref": "session", "cache_write": True}

_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]{0,127}\Z")

# Copilot's tool names, as the names of the same tools that plain_tool knows: what a call does in plain words.
_PLAIN_NAMES = {"view": "Read", "create": "Write", "edit": "Edit", "bash": "Bash", "grep": "Grep", "glob": "Glob",
                "ask_user": "AskUserQuestion", "web_fetch": "WebFetch", "web_search": "WebSearch"}


def _s():
    """river.sessions, imported when first used: core imports the adapters while it loads, before sessions can."""
    from .. import sessions
    return sessions


def home():
    """Copilot's own folder: $COPILOT_HOME, else ~/.copilot."""
    return Path(os.environ.get("COPILOT_HOME") or Path.home() / ".copilot")


def chat_path(session_ref, folder=None, root=None):
    """The events.jsonl of session `session_ref` (under root in place of Copilot's session-state folder, when given),
    or None when there is none."""
    if not session_ref or not _ID.match(str(session_ref)):
        return None
    f = Path(root or home() / "session-state") / str(session_ref) / "events.jsonl"
    return f if f.is_file() else None


def _rows(lines):
    for line in lines:
        try:
            d = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(d, dict) and isinstance(d.get("data"), dict):
            yield d


def parse_chat_line(d):
    """The page lines of one events.jsonl line, and the turn state it shows. user.message: the person's prompt;
    assistant.message: the reply text, and a tool line for each call it asks for; tool.execution_complete: the
    result; permission.requested and human_response.recorded: events; session.compaction_complete: a 'compacted'
    divider and the summary; session.resume: a 'resumed' divider. The turn: working from a prompt, a turn start or a
    tool start; idle at a reply that asks for no tool (Copilot's loop ends there; agentStop follows), and at the
    end or the resume of the session."""
    s = _s()
    data = d.get("data") if isinstance(d.get("data"), dict) else {}
    t, at = d.get("type"), d.get("timestamp")
    if t == "user.message":
        text = str(data.get("content") or "").strip()
        images = sum(1 for a in data.get("attachments") or () if isinstance(a, dict) and a.get("type") == "image")
        return ([s._user_turn(at, text, images)] if text or images else []), "working"
    if t in ("assistant.turn_start", "tool.execution_start"):
        return [], "working"
    if t == "assistant.message":
        reqs = [r for r in data.get("toolRequests") or () if isinstance(r, dict)]
        out = [{"at": at, "role": "assistant", "text": s._clip(data["content"], s.TEXT_MAX)}] \
            if str(data.get("content") or "").strip() else []
        for r in reqs:
            name, args = str(r.get("name") or "tool"), r.get("arguments")
            out.append({"at": at, "role": "tool", "text": s._tool_line(name, args), "id": r.get("toolCallId"),
                        "name": name, "plain": s.plain_tool(_PLAIN_NAMES.get(name, name), args),
                        "detail": s._detail(args if args is not None else {})})
        return out, ("working" if reqs else "idle")
    if t == "tool.execution_complete":
        res = data.get("result")
        text = s._result_text(res) if res is not None else ""
        if not text and isinstance(data.get("error"), dict):
            text = data["error"].get("message") or ""
        return [{"at": at, "role": "result", "for": data.get("toolCallId"), "text": s._clip(text, s.DETAIL_MAX),
                 "error": not data.get("success", True)}], None
    if t == "permission.requested":
        p = data.get("permissionRequest") or {}
        what = p.get("fullCommandText") or p.get("path") or p.get("url") or p.get("kind") or ""
        return [{"at": at, "role": "event", "text": s._clip(f"Asks permission: {what}", s.TEXT_MAX)}], None
    if t == "human_response.recorded":
        r = data.get("response") or {}
        return [{"at": at, "role": "event",
                 "text": s._clip(f"Answered: {json.dumps(r.get('content'), ensure_ascii=False)}", s.TEXT_MAX)}], None
    if t == "session.compaction_complete" and data.get("success"):
        out = [{"at": at, "role": "divider", "kind": "compacted", "text": "Compacted"}]
        if str(data.get("summaryContent") or "").strip():
            out.append({"at": at, "role": "summary", "text": s._clip(data["summaryContent"], s.TEXT_MAX)})
        return out, None
    if t == "session.resume":
        return [{"at": at, "role": "divider", "kind": "resumed", "text": "Resumed"}], "idle"
    if t == "session.shutdown":
        return [], "idle"
    return [], None


def parse_chat(lines):
    return _s().parse_lines(parse_chat_line, lines)


def chat_cwd(lines):
    """The folder of the session: session.start's context, or a later session.resume's."""
    cwd = None
    for d in _rows(lines):
        if d.get("type") in ("session.start", "session.resume"):
            cwd = (d["data"].get("context") or {}).get("cwd") or cwd
    return cwd


def chat_usage(lines):
    """Token counts of the session.usage_record lines. Copilot's inputTokens include the cached ones (an
    OpenAI-style count); here input is the part not read from or written to the cache, as for Claude Code."""
    out = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    for d in _rows(lines):
        if d.get("type") != "session.usage_record":
            continue
        u = d["data"].get("usage") or {}
        cr, cw = int(u.get("cacheReadTokens") or 0), int(u.get("cacheWriteTokens") or 0)
        out["input"] += max(0, int(u.get("inputTokens") or 0) - cr - cw)
        out["output"] += int(u.get("outputTokens") or 0)
        out["cache_read"] += cr
        out["cache_write"] += cw
    return out


def chat_settings(lines):
    """The model and effort the session runs now, from its last session.model_change: {"model", "effort"}."""
    got = {}
    for d in _rows(lines):
        if d.get("type") == "session.model_change":
            got = {"model": d["data"].get("newModel"), "effort": d["data"].get("reasoningEffort")}
    return {k: v for k, v in got.items() if v}


# ---------------------------------------------------------------------------------------------------------------
# Commands: the built-in slash commands (copilot help commands), and the custom ones of a folder
# ---------------------------------------------------------------------------------------------------------------

_BUILTIN = [
    ("init", "Initialize instructions; add guidance after /init", ""),
    ("agent", "Browse and select agents", "[name]"),
    ("skills", "Manage skills", ""),
    ("mcp", "Manage MCP server configuration", ""),
    ("plugin", "Manage plugins and plugin marketplaces", ""),
    ("model", "Select the AI model for this session ('auto' lets Copilot pick)", "[model]"),
    ("delegate", "Send this session to GitHub and Copilot will create a PR", ""),
    ("fleet", "Enable fleet mode for parallel subagent execution", ""),
    ("autopilot", "Toggle autopilot mode, or set an autopilot objective", "[objective]"),
    ("tasks", "View running subagents and background shell commands", ""),
    ("ide", "Connect to an IDE workspace", ""),
    ("diff", "Review the changes made in the current directory", ""),
    ("pr", "Operate on pull requests for the current branch", ""),
    ("review", "Run code review agent to analyze changes", ""),
    ("security-review", "Analyze changes for security vulnerabilities", ""),
    ("rubber-duck", "Get an independent critique of your current work", ""),
    ("lsp", "Manage language server configuration", ""),
    ("terminal-setup", "Configure terminal for multiline input support (shift+enter)", ""),
    ("permissions", "Switch between permission modes", ""),
    ("allow-all", "Enable all permissions (tools, paths, and URLs)", ""),
    ("sandbox", "Manage sandbox settings", ""),
    ("add-dir", "Allow file access to a directory", "<directory>"),
    ("list-dirs", "Display allowed directories", ""),
    ("cwd", "Change working directory or show current directory", "[directory]"),
    ("reset-allowed-tools", "Reset session tool and path approvals", ""),
    ("resume", "Switch sessions by ID, task ID, or name", "[session]"),
    ("rename", "Rename the current session", "[name]"),
    ("fork", "Fork the current session into a new session", "[name]"),
    ("worktree", "Create a new git worktree and switch into it", ""),
    ("move", "Move your uncommitted changes into a new git worktree", "[branch]"),
    ("context", "Show context window token usage", ""),
    ("usage", "Display session usage metrics and statistics", ""),
    ("session", "View and manage sessions", "[subcommand]"),
    ("compact", "Summarize conversation history to reduce context window usage", "[focus]"),
    ("share", "Open sharing actions for this session", "[file|html|gist|link]"),
    ("remote", "Show remote status or toggle remote control", "[on|off|show]"),
    ("copy", "Copy the last response to the clipboard", ""),
    ("rewind", "Pick an earlier turn to rewind to", ""),
    ("help", "Show help for interactive commands", ""),
    ("changelog", "Display changelog for CLI versions", ""),
    ("feedback", "Report a bug or feature request", ""),
    ("diagnose", "Analyze session logs", ""),
    ("statusline", "Configure status line items", ""),
    ("update", "Update the CLI to the latest version", ""),
    ("version", "Display version information", ""),
    ("experimental", "Show available experimental features", ""),
    ("memory", "Show memory status, or enable/disable memory", ""),
    ("clear", "Abandon this session and start fresh", ""),
    ("instructions", "View and toggle custom instruction files", ""),
    ("ask", "Ask a side question without adding to history", "<question>"),
    ("config", "Open the configuration screen", "[area]"),
    ("env", "Show loaded environment details", ""),
    ("exit", "Exit the CLI", ""),
    ("limits", "View or edit session limits", ""),
    ("login", "Log in to Copilot", ""),
    ("logout", "Sign out of an account", ""),
    ("new", "Start a new conversation", ""),
    ("plan", "Create an implementation plan before coding", ""),
    ("refine", "Rewrite a rough prompt into a clear one", ""),
    ("research", "Run deep research investigation", "<topic>"),
    ("restart", "Restart the CLI", ""),
    ("settings", "Open the settings UI, or show, set, or unset a value", "[key] [value]"),
    ("subagents", "Configure each subagent's model and settings", ""),
    ("user", "Manage signed-in accounts", ""),
    ("vim", "Toggle Vim mode for the input", ""),
    ("voice", "Manage voice mode", ""),
]
COMMANDS = [{"name": n, "description": d, "hint": h} for n, d, h in _BUILTIN]


def _front(text):
    """The simple key: value lines of a Markdown file's front matter."""
    m = re.match(r"---\s*\n(.*?)\n---\s*(\n|\Z)", text, re.S)
    out = {}
    for line in (m.group(1).splitlines() if m else ()):
        k, sep, v = line.partition(":")
        if sep and k.strip() and not k.startswith((" ", "\t")):
            out[k.strip()] = v.strip().strip("'\"")
    return out


def _read(f):
    try:
        return f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# How each command works from the page (#5220, #5291): GitHub Copilot CLI in tmux on 2026-11-11. /permissions opens a
# numbered menu (the Settings row Permissions) and /model a picker (Model); /context shows its context view and /usage
# its plan's premium requests ('1 / 200 AIC'): the Usage panel gives the session's side of them (its plan limits show
# only on its screen). /session, /resume, /config, /settings and the other views need the terminal; the others run.
_RUNS = {
    "terminal": {"agent", "skills", "mcp", "plugin", "tasks", "ide", "diff", "pr", "lsp", "terminal-setup", "sandbox",
                 "list-dirs", "resume", "session", "share", "remote", "rewind", "help", "changelog", "diagnose",
                 "statusline", "version", "experimental", "memory", "instructions", "config", "env", "limits",
                 "login", "settings", "subagents", "user", "voice"},
}
for _c in COMMANDS:
    _c["run"] = next((k for k, v in _RUNS.items() if _c["name"] in v), "now")
    if _c["name"] in ("model", "permissions"):
        _c["setting"], _c["run"] = _c["name"], ("menu" if _c["name"] == "permissions" else "terminal")
    if _c["name"] in ("context", "usage"):
        _c["page"], _c["run"] = "usage", "terminal"


def read_commands(folder):
    """The custom slash commands of a session in `folder`, as the CLI lists them: the project's Claude Code
    commands (.claude/commands/*.md), and the skills of the project (.github/skills, .agents/skills,
    .claude/skills) and of the person (~/.copilot/skills, ~/.agents/skills). Copilot does not read ~/.claude."""
    out, seen = [], {c["name"] for c in COMMANDS}
    base = Path(folder) if folder else None

    def add(name, desc, hint=""):
        if name and name not in seen:
            seen.add(name)
            out.append({"name": name, "description": desc or "", "hint": hint or ""})

    if base:
        cdir = base / ".claude" / "commands"
        for f in sorted(cdir.rglob("*.md")) if cdir.is_dir() else ():
            fm = _front(_read(f))
            add(":".join(f.relative_to(cdir).with_suffix("").parts), fm.get("description"), fm.get("argument-hint"))
    dirs = [base / p for p in (".github/skills", ".agents/skills", ".claude/skills")] if base else []
    dirs += [home() / "skills", Path.home() / ".agents" / "skills"]
    for d in dirs:
        for f in sorted(d.glob("*/SKILL.md")) if d.is_dir() else ():
            fm = _front(_read(f))
            add(fm.get("name") or f.parent.name, fm.get("description"))
    return out


# ---------------------------------------------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------------------------------------------

SETUP = {
    # Copilot reads AGENTS.md, CLAUDE.md, GEMINI.md and .github/copilot-instructions.md in a project.
    "instructions": "AGENTS.md",
    "skills_dir": "~/.copilot/skills",
    "voice": "/voice on turns on dictation, transcribed on this computer (macOS on Apple silicon, Windows, Linux x64).",
    "open_link": None,
}


def write_mcp_config(path=None, remove=False):
    """Add (or, with remove, take out) MaximizePM's MCP server in Copilot's own list, $COPILOT_HOME/mcp-config.json
    (path: another file), as mcpServers.maxpm = {"type": "local", command, args, env, "tools": ["*"]}: setup_entry(),
    the stdio server `maxpm mcp`, with no MAXPM_CHAT (a Copilot session runs in a project folder). Other servers
    stay; the old file is kept as mcp-config.json.bak. Copilot Free's policy turns third-party MCP servers off
    ("Third-party MCP servers are disabled by your organization's Copilot policy"): on that plan the entry is kept
    but not used. Returns one text that says what changed."""
    from . import setup_entry
    from ..core import RiverError
    f = Path(path).expanduser() if path else home() / "mcp-config.json"
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
        server = setup_entry()
        server["env"].pop("MAXPM_CHAT", None)
        entry = {"type": "local", **server, "tools": ["*"]}
        if servers.get("maxpm") == entry:
            return f"MaximizePM is already in {f}; nothing changed"
        servers["maxpm"] = entry
    if old:
        f.with_name(f.name + ".bak").write_text(old)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, indent=2) + "\n")
    if remove:
        return f"took MaximizePM out of {f} (the old file: {f.name}.bak). A new Copilot session has no maxpm tools."
    return (f"added MaximizePM to {f}" + (f" (the old file: {f.name}.bak)" if old else "") +
            ". A new Copilot session has the maxpm tools (/mcp lists them).")
