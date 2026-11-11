"""The Cursor CLI adapter (docs/agent-adapters.md, #5175): cursor-agent, Cursor's coding agent in a terminal.

What this module rests on, read from cursor-agent 2026.11.11-043ef28 on macOS (its --help, its JavaScript bundle in
~/.local/share/cursor-agent/versions/<version>/, and sessions recorded in a tmux pane; tests/fixtures/cursor):

- Cursor routes many providers (Claude, GPT, Gemini, Grok, its own Composer), so its family is "multi": no
  --min-model or --max-model limit applies. "auto" lets Cursor pick. The effort is part of the model id
  (claude-opus-5-5-high, gpt-5.6-sol-low), not a flag: build_command puts it there. The bracket form that --help
  names ('claude-opus-4-8[effort=high]') is refused for these models ("Cannot use this model").
- State comes from the screen (DETECT): Cursor's hooks have no event for a permission menu (its table maps Claude
  Code's PermissionRequest and Notification to none), so only the pane shows blocked. Working: a braille spinner with
  a word (Working, Running, Editing) or "ctrl+c to stop"; idle: the input line "→ ..."; blocked: the questions of its
  approval menus and the workspace trust question.
- The session id comes from the environment: each shell command that Cursor runs has CURSOR_AGENT=1 and
  CURSOR_CONVERSATION_ID=<chat id> (the same id as the hooks' session_id), so each maxpm command of the session sees
  it, as Codex's CODEX_THREAD_ID. No hook goes into ~/.cursor: Cursor has no hooks file for one start (a plugin from
  --plugin-dir gets the tool events, but not sessionStart, beforeSubmitPrompt or stop).
- Chat log: ~/.cursor/projects/<workspace key>/agent-transcripts/<chat id>/<chat id>.jsonl, one JSON object per line
  ({"role": "user"|"assistant", "message": {"content": [...]}}, and {"type": "turn_ended"} at a turn's end). No
  times, no tool results, no token counts. A resume (--resume <chat id>) appends to the same file.
  ~/.cursor/chats/<md5 of the workspace path>/<chat id>/meta.json holds the chat's title and cwd.
- Custom slash commands: .md files in .cursor/commands and .claude/commands (the workspace and ~), and skills
  (SKILL.md) in ~/.cursor/skills, ~/.claude/skills, ~/.agents/skills, ~/.codex/skills and the workspace's
  .cursor/skills, .claude/skills, .agents/skills.
"""

import hashlib
import json
import os
import re
from pathlib import Path

from . import Raw

DESCRIBE = {
    "id": "cursor",
    "label": "Cursor CLI",
    "exe": "cursor-agent",
    # Not "agent": the installer links ~/.local/bin/agent to cursor-agent, but that name is the Grok CLI's too.
    "aliases": ["cursor-agent", "cursor-cli"],
    "family": "multi",
    "models": ["auto", "opus", "sonnet", "haiku", "fable", "sol", "terra", "luna", "composer-2.5", "grok-4.7"],
    "default_model": "",  # Cursor's own default (cursor-agent models marks it; Auto on a new account)
    # Cursor takes a base id and picks the effort it names as its default ("claude-opus-5-5" is Opus 5.5 at medium).
    "model_ids": {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5",
                  "fable": "claude-fable-5-2", "sol": "gpt-5.6-sol", "terra": "gpt-5.6-terra", "luna": "gpt-5.6-luna"},
    "prefix": "cursor_",
}

# The settings of a start. "args" and "prompt" go in by build_command; the others by their flag.
SETTINGS = [
    {"key": "force", "label": "Run Everything", "kind": "toggle", "choices": [], "default": "off",
     "text": "run every command without asking, unless the permissions deny it (--force)",
     "flag": ["--force"], "live": "/run-everything", "read": r"(--force|--yolo)\b"},
    {"key": "auto_review", "label": "Auto-review", "kind": "toggle", "choices": [], "default": "off",
     "text": "a classifier of Cursor runs safe tool calls and asks for the rest (--auto-review)",
     "flag": ["--auto-review"], "live": "/auto-review", "read": r"(--auto-review)\b"},
    {"key": "sandbox", "label": "Sandbox", "kind": "choice", "choices": ["enabled", "disabled"], "default": "",
     "text": "run its commands in Cursor's sandbox; empty: as cli-config.json says (--sandbox)",
     "flag": ["--sandbox", "{value}"], "live": None, "read": r"--sandbox[ =](enabled|disabled)\b"},
    {"key": "mode", "label": "Mode", "kind": "choice", "choices": ["plan", "ask"], "default": "",
     "text": "plan: read-only, it proposes a plan; ask: questions and answers, read-only (--mode)",
     "flag": ["--mode", "{value}"], "live": None, "read": r"--mode[ =](plan|ask)\b"},
    {"key": "trust", "label": "Trust the folder", "kind": "toggle", "choices": [], "default": "off",
     "text": "start in a folder that Cursor does not trust yet without its trust question (--trust)",
     "flag": ["--trust"], "live": None, "read": r"(--trust)\b"},
    {"key": "approve_mcps", "label": "Approve MCP servers", "kind": "toggle", "choices": [], "default": "off",
     "text": "use every MCP server of the folder without asking (--approve-mcps)",
     "flag": ["--approve-mcps"], "live": None, "read": r"(--approve-mcps)\b"},
    {"key": "args", "label": "More arguments", "kind": "text", "choices": [], "default": "",
     "text": "more arguments, put before the prompt", "flag": [], "live": None, "read": None},
    {"key": "prompt", "label": "First prompt", "kind": "text", "choices": [],
     "default": "run maxpm go in this folder and follow the briefing",
     "text": "the first prompt (Cursor reads AGENTS.md, CLAUDE.md and .cursor/rules of the folder)", "flag": [],
     "live": None, "read": None},
]

# The effort suffixes of Cursor's model ids. A GPT id also takes none; gpt-5.5 names xhigh extra-high.
LEVELS = ["low", "medium", "high", "xhigh", "max"]
_SUFFIX = re.compile(r"-(none|minimal|low|medium|high|xhigh|extra-high|max)(-fast)?$")

EFFORT = {
    "levels": LEVELS,
    "map": {},
    "flag": [],  # no flag: build_command puts the level into the model id (claude-opus-5-5 -> claude-opus-5-5-high)
    "live": None,  # /model and the model commands open a picker; no text sets the effort alone
    "read": r"--model[ =]\S*?-(low|medium|high|xhigh|max)(?:-fast)?(?=\s|$)",
}

LAUNCH = {
    "resume": True,  # --resume <chat id>: Cursor appends to the same transcript
    # No session_name (only /rename in a running chat), no system_prompt_file (Cursor has no flag for one; it reads
    # AGENTS.md and its rules), no hooks_file (no hooks file for one start), no fork (/fork only in a running chat),
    # no autocompact, no background. No extra_dirs: Cursor has --add-dir, but its sandbox is off unless the person
    # turns it on, and a folder there becomes a workspace root that Cursor indexes.
}

IDENTITY = {
    "source": "env",  # each shell command of the session has CURSOR_CONVERSATION_ID
    "env": "CURSOR_CONVERSATION_ID",
    "log_glob": "~/.cursor/projects/*/agent-transcripts/*/*.jsonl",
    "pane": None,
    "url": None,
    "inside_env": ["CURSOR_AGENT"],  # Cursor sets CURSOR_AGENT=1 for the commands it runs
}

CHAT = {"compressed": False, "file_tools": ["Write"], "ref": "session", "cache_write": False}

STATE = {"state": "detect", "session_id": "identity"}

# The questions of its approval menus (the strings of its bundle) and of the workspace trust screen.
BLOCKED = [
    r"Run this (command|command outside the sandbox|MCP tool)\?",
    r"(Write to|Read|Delete) this file\?",
    r"Proceed with this edit\?",
    r"Allow (this )?web (search|fetch)\?",
    r"Ready to (proceed|build)\?",
    r"Set Run Everything as your default\?",
    r"Do you trust the contents of this directory\?",
]

DETECT = {
    "process": "cursor-agent",
    "idle": r"^\s*→ ",  # its input line ("→ Add a follow-up", "→ Plan, search, build anything", or typed text)
    # A braille spinner with a word ("⠘⠣ Running  86 tokens"); Reconnecting shows after a turn too, so not that one.
    # "ctrl+c to stop" stands at the right of the input line while a turn runs.
    "working": r"^\s*[⠀-⣿]{1,2}\s+(?!Reconnecting)[A-Z][a-z]+\b|\bctrl\+c to stop\b",
    "blocked": BLOCKED,
    "tell_instead": r"Skip & tell the agent what to do instead",
    "image_paste": None,
    "menu": "→▶",  # the marker of the selected option: → in an approval menu, ▶ on the trust screen
}

INPUT = {"native": None}

# Its built-in slash commands (the "/" list of 2026.11.11-043ef28). The model commands (/claude-opus-5-5, ...) depend
# on the account and are left out.
COMMANDS = [
    {"name": "model", "description": "Select model", "hint": "[filter]"},
    {"name": "goal", "description": "Start a durable goal that continues while idle", "hint": "[objective]"},
    {"name": "add-dir", "description": "Add a directory to the current workspace", "hint": "[path]"},
    {"name": "save-workspace", "description": "Persist the current workspace directories", "hint": "<name>"},
    {"name": "load-workspace", "description": "Load a saved workspace by name", "hint": "<name>"},
    {"name": "run-everything", "description": "Toggle Run Everything", "hint": ""},
    {"name": "auto-review", "description": "Toggle Auto-review", "hint": ""},
    {"name": "plan", "description": "Create a plan or show existing plan with options", "hint": "[prompt]"},
    {"name": "ask", "description": "Toggle ask mode (Q&A, read-only; no edits or command execution)", "hint": ""},
    {"name": "debug", "description": "Toggle debug mode or submit a prompt in debug mode", "hint": "[prompt]"},
    {"name": "logs", "description": "Show the current debug log file path", "hint": ""},
    {"name": "update", "description": "Update Cursor Agent to the latest version", "hint": ""},
    {"name": "max-mode", "description": "Toggle max mode", "hint": ""},
    {"name": "fast", "description": "Toggle fast mode", "hint": ""},
    {"name": "rename", "description": "Rename the current chat session", "hint": "<name>"},
    {"name": "clear", "description": "Start a new chat session", "hint": ""},
    {"name": "resume", "description": "Open recent chats and resume one", "hint": ""},
    {"name": "fork", "description": "Fork the current chat into a new session", "hint": ""},
    {"name": "summarize", "description": "Summarize the conversation to reduce context", "hint": ""},
    {"name": "rewind", "description": "Jump back to a previous message", "hint": ""},
    {"name": "vim", "description": "Toggle Vim keys", "hint": ""},
    {"name": "line-numbers", "description": "Toggle line numbers in code blocks", "hint": ""},
    {"name": "show-thinking", "description": "Toggle thinking block display", "hint": ""},
    {"name": "status-indicators", "description": "Toggle terminal title status indicators", "hint": ""},
    {"name": "shell", "description": "Enter Shell Mode (hint: type ! on an empty line)", "hint": "[command]"},
    {"name": "about", "description": "Show CLI version, system, and account info", "hint": ""},
    {"name": "setup-terminal", "description": "Configure your terminal for newlines", "hint": ""},
    {"name": "help", "description": "Show help", "hint": "[command]"},
    {"name": "feedback", "description": "Share feedback with the team", "hint": "<message>"},
    {"name": "open", "description": "Open the repository's git root in Cursor", "hint": ""},
    {"name": "copy-request-id", "description": "Copy last request ID", "hint": ""},
    {"name": "copy-conversation-id", "description": "Copy current conversation ID", "hint": ""},
    {"name": "team", "description": "Switch the active team for this account", "hint": "[team]"},
    {"name": "mcp", "description": "Manage MCP servers (list, list-tools)", "hint": "[subcommand]"},
    {"name": "plugin", "description": "Manage plugins", "hint": "[subcommand]"},
    {"name": "config", "description": "Configure CLI settings interactively", "hint": ""},
    {"name": "copy", "description": "Copy a previous message to the clipboard", "hint": ""},
    {"name": "sandbox", "description": "Sandbox settings", "hint": ""},
    {"name": "logout", "description": "Sign out from Cursor", "hint": ""},
    {"name": "quit", "description": "Exit", "hint": ""},
]

SETUP = {
    "instructions": "AGENTS.md",  # Cursor reads AGENTS.md and CLAUDE.md of the folder, and .cursor/rules
    "skills_dir": "~/.cursor/skills",
    "voice": None,
    "open_link": None,
}


# ---------------------------------------------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------------------------------------------

def cursor_dir(env=None):
    """Cursor's own folder: ~/.cursor (HOME of env)."""
    env = os.environ if env is None else env
    return Path(env.get("HOME") or "~").expanduser() / ".cursor"


def workspace_key(folder):
    """The name of a workspace's folder in ~/.cursor/projects, as Cursor makes it: each run of characters other than
    a-z, A-Z and 0-9 becomes one -, with none at the ends (/Users/a/my.app -> Users-a-my-app)."""
    return re.sub(r"-+", "-", re.sub(r"[^a-zA-Z0-9]", "-", str(folder))).strip("-")


def chats_key(folder):
    """The name of a workspace's folder in ~/.cursor/chats: the MD5 of its real path."""
    return hashlib.md5(os.path.realpath(str(folder)).encode()).hexdigest()


# ---------------------------------------------------------------------------------------------------------------
# 4. Launch
# ---------------------------------------------------------------------------------------------------------------

def _on(v):
    return v is True or str(v).strip().lower() in ("on", "true", "1", "yes")


def model_word(model, effort):
    """The --model word: the model id, with the effort as its suffix when both are given, the model is no "auto",
    and its id names no effort yet (claude-opus-5-5 + high -> claude-opus-5-5-high). None for no model."""
    if not model:
        return None
    level = EFFORT["map"].get(effort, effort) if effort else None
    if not level or model == "auto" or _SUFFIX.search(model):
        return model
    return f"{model}-{level}"


def build_command(name, folder, model, effort, settings, prompt):
    """cursor-agent [--resume <id>] [--model <id>[-<effort>]] [--force] [--auto-review] [--sandbox S] [--mode M]
    [--trust] [--approve-mcps] [args] [prompt]. Cursor has no flag for a session name: name is not used. An effort
    with no model goes nowhere: Cursor has no effort of its own default to set."""
    s = settings or {}
    argv = ["cursor-agent"]
    if s.get("resume"):
        argv += ["--resume", str(s["resume"])]
    word = model_word(model, effort)
    if word:
        argv += ["--model", word]
    for spec in SETTINGS:
        key, value = spec["key"], s.get(spec["key"], spec["default"])
        if not spec["flag"]:
            continue
        if spec["kind"] == "toggle":
            argv += spec["flag"] if _on(value) else []
        elif (value or "").strip():
            argv += [w.format(value=value.strip()) for w in spec["flag"]]
    if (s.get("args") or "").strip():
        argv.append(Raw(s["args"].strip()))
    if prompt:
        argv.append(prompt)
    return argv


def parse_command(words):
    """A hand-written cursor-agent command that a profile builds exactly: --model {model}, the flags of SETTINGS with
    a known value, and one prompt. Anything else (print mode, an API key, a fixed model, a worktree): None."""
    if not words or Path(words[0]).name.lower() != "cursor-agent":
        return None
    toggles = {w: s["key"] for s in SETTINGS if s["kind"] == "toggle" for w in s["flag"]}
    toggles["--yolo"] = "force"
    choices = {s["flag"][0]: s for s in SETTINGS if s["kind"] == "choice"}
    opts, prompt, i = {}, None, 1
    while i < len(words):
        t, nxt = words[i], (words[i + 1] if i + 1 < len(words) else None)
        if t == "--model" and nxt == "{model}":
            i += 2
        elif t in toggles:
            opts[toggles[t]] = "on"
            i += 1
        elif t in choices and nxt in choices[t]["choices"] and nxt:
            opts[choices[t]["key"]] = nxt
            i += 2
        elif t == "--plan":
            opts["mode"] = "plan"
            i += 1
        elif not t.startswith("-") and prompt is None and t.strip() and ";" not in t:
            prompt = t
            i += 1
        else:
            return None
    return {"settings": opts, "prompt": prompt} if prompt is not None else None


# ---------------------------------------------------------------------------------------------------------------
# 9. Resume
# ---------------------------------------------------------------------------------------------------------------

def resume_command(session_ref, model, effort, settings, name=None):
    """cursor-agent --resume <chat id>, with the profile's settings and settings['prompt'] as its first prompt. With
    no model the chat keeps the model it had."""
    if not session_ref:
        return None
    s = {k: v for k, v in (settings or {}).items() if k != "prompt"}
    return build_command(name, None, model, effort, {**s, "resume": session_ref}, (settings or {}).get("prompt"))


# ---------------------------------------------------------------------------------------------------------------
# 6. Chat log
# ---------------------------------------------------------------------------------------------------------------

_ID = re.compile(r"[0-9A-Za-z-]{8,64}\Z")


def chat_path(session_ref, folder=None, root=None):
    """The transcript of a chat id: first in the folder's project folder, then in every project folder of
    ~/.cursor/projects (root replaces that folder); None when there is none."""
    ref = str(session_ref or "").strip()
    if not _ID.match(ref):
        return None
    base = Path(root) if root else cursor_dir() / "projects"
    keys = [workspace_key(os.path.realpath(str(folder))), workspace_key(folder)] if folder else []
    for k in dict.fromkeys(keys):
        p = base / k / "agent-transcripts" / ref / f"{ref}.jsonl"
        if p.is_file():
            return p
    hits = sorted(base.glob(f"*/agent-transcripts/{ref}/{ref}.jsonl"))
    return hits[-1] if hits else None


def _rows(lines):
    """The JSON objects of the lines; a line that is no JSON (one Cursor still writes) is left out."""
    out = []
    for ln in lines:
        if isinstance(ln, bytes):
            ln = ln.decode("utf-8", "replace")
        ln = ln.strip() if isinstance(ln, str) else ""
        if not ln:
            continue
        try:
            row = json.loads(ln)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


_QUERY = re.compile(r"<user_query>\n?(.*?)\n?</user_query>", re.S)
_STAMP = re.compile(r"<timestamp>(.*?)</timestamp>")
# Cursor adds this user line itself after a dropped connection, and the model answers again.
CONTINUE = "Your previous response was interrupted. Continue from where you left off."

# The argument that says best what a call of each tool does.
TOOL_DETAIL = {"Shell": "command", "Read": "path", "Write": "path", "Grep": "pattern", "Glob": "glob_pattern",
               "WebFetch": "url", "WebSearch": "search_term", "Task": "description"}


def _detail(name, args):
    args = args if isinstance(args, dict) else {}
    key = TOOL_DETAIL.get(name)
    if key and isinstance(args.get(key), str):
        return args[key]
    for k in ("command", "path", "file_path", "pattern", "url"):
        if isinstance(args.get(k), str):
            return args[k]
    return json.dumps(args, ensure_ascii=False)[:300] if args else ""


def _user_text(text):
    """The person's words of a user line: the text in <user_query>, without Cursor's <timestamp> and context."""
    m = _QUERY.search(text)
    return m.group(1).strip() if m else text.strip()


def _entries(row, n):
    """The shared entries of one transcript line; n: its number, for the call ids (Cursor's lines have none)."""
    if row.get("type") == "turn_ended":
        st = row.get("status")
        return [{"at": None, "role": "event", "text": f"error: {row.get('error') or st}"}] if st == "error" else []
    role = row.get("role")
    content = (row.get("message") or {}).get("content")
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else [
        b for b in content or () if isinstance(b, dict)]
    out = []
    for i, b in enumerate(blocks):
        kind = b.get("type")
        if role == "user" and kind == "text":
            text = b.get("text", "")
            if text.strip() == CONTINUE:
                out.append({"at": None, "role": "event", "text": "interrupted; Cursor continued the turn"})
            elif text.strip():
                out.append({"at": None, "role": "user", "text": _user_text(text)})
        elif role == "user" and kind == "image":
            src = b.get("source") or {}
            url = f"data:{src.get('media_type', 'image/png')};base64,{src['data']}" if src.get("data") else b.get("path")
            if url:
                if out and out[-1]["role"] == "user":
                    out[-1].setdefault("images", []).append(url)
                else:
                    out.append({"at": None, "role": "user", "text": "", "images": [url]})
        elif role == "assistant" and kind == "text" and b.get("text", "").strip():
            out.append({"at": None, "role": "assistant", "text": b["text"]})
        elif role == "assistant" and kind == "tool_use":
            name = b.get("name", "")
            out.append({"at": None, "role": "tool", "text": name, "id": b.get("id") or f"l{n}.{i}", "name": name,
                        "detail": _detail(name, b.get("input"))})
    return out


def _turn(rows):
    """idle after a turn_ended line, working after anything else, None for no line."""
    if not rows:
        return None
    return "idle" if rows[-1].get("type") == "turn_ended" else "working"


def parse_chat(lines):
    """(entries, turn) of the lines of one transcript."""
    rows = _rows(lines)
    return [e for n, r in enumerate(rows) for e in _entries(r, n)], _turn(rows)


def meta(session_ref, folder=None, env=None):
    """The chat's meta.json in ~/.cursor/chats ({"title", "cwd", "createdAtMs", ...}), or None. The folder of the
    workspace narrows the search; without it, every workspace's chats are searched."""
    ref = str(session_ref or "").strip()
    if not _ID.match(ref):
        return None
    base = cursor_dir(env) / "chats"
    paths = [base / chats_key(folder) / ref / "meta.json"] if folder else []
    paths += sorted(base.glob(f"*/{ref}/meta.json"))
    for p in paths:
        try:
            got = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(got, dict):
            return got
    return None


# ---------------------------------------------------------------------------------------------------------------
# 10. Commands
# ---------------------------------------------------------------------------------------------------------------

def _front(text):
    """The front matter of a Markdown file as {key: value}, and the body."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta_ = {}
    for ln in parts[1].splitlines():
        k, sep, v = ln.partition(":")
        if sep:
            meta_[k.strip()] = v.strip().strip("\"'")
    return meta_, parts[2]


def _commands(d):
    """The commands of a folder of .md files: each file's name, its front matter's description (else its first
    line), and its argument-hint."""
    out = []
    for f in sorted(Path(d).glob("*.md")) if Path(d).is_dir() else ():
        try:
            m, body = _front(f.read_text(errors="replace"))
        except OSError:
            continue
        first = next((ln.strip().lstrip("#").strip() for ln in body.splitlines() if ln.strip()), "")
        out.append({"name": f.stem, "description": m.get("description") or first[:120],
                    "hint": m.get("argument-hint", "")})
    return out


def _skills(d):
    """The skills of a folder: each SKILL.md one level down, by the name of its front matter (else its folder)."""
    out = []
    for f in sorted(Path(d).glob("*/SKILL.md")) if Path(d).is_dir() else ():
        try:
            m, _ = _front(f.read_text(errors="replace"))
        except OSError:
            continue
        out.append({"name": m.get("name") or f.parent.name, "description": m.get("description", ""), "hint": ""})
    return out


def read_commands(folder, home=None):
    """The custom commands of a session in folder, in Cursor's order: the commands of .claude/commands and
    .cursor/commands of the folder, then of ~/.claude/commands and ~/.cursor/commands; then the skills of
    ~/.cursor/skills, the folder's .cursor/skills, .agents/skills and .claude/skills, and ~/.claude/skills,
    ~/.agents/skills and ~/.codex/skills. The first of a name wins. home: the folder for ~ (the tests give one)."""
    h = Path(home).expanduser() if home else Path("~").expanduser()
    f = Path(folder).expanduser() if folder else None
    dirs = ([f / ".claude" / "commands", f / ".cursor" / "commands"] if f else []) + [
        h / ".claude" / "commands", h / ".cursor" / "commands"]
    out = [c for d in dirs for c in _commands(d)]
    sk = [h / ".cursor" / "skills"] + ([f / ".cursor" / "skills", f / ".agents" / "skills", f / ".claude" / "skills"]
                                       if f else []) + [h / ".claude" / "skills", h / ".agents" / "skills",
                                                        h / ".codex" / "skills"]
    out += [c for d in sk for c in _skills(d)]
    builtin = {c["name"] for c in COMMANDS}
    seen, uniq = set(builtin), []
    for c in out:
        if c["name"] not in seen:
            seen.add(c["name"])
            uniq.append(c)
    return uniq


# ---------------------------------------------------------------------------------------------------------------
# Setup (#5250)
# ---------------------------------------------------------------------------------------------------------------

def write_mcp_config(path=None, remove=False):
    """Add (or remove) MaximizePM's MCP server in Cursor's user MCP file, ~/.cursor/mcp.json ({"mcpServers": {"maxpm":
    {command, args, env}}}; path: another file, such as a project's .cursor/mcp.json). The other servers and keys
    stay; the old file is kept as mcp.json.bak. The entry is setup_entry() without MAXPM_CHAT: a Cursor CLI session
    runs in a project folder, so go briefs it for that folder, not as a chat. Cursor asks once in each folder before
    it uses a new server, unless the session starts with --approve-mcps (the approve_mcps setting) or a person runs
    cursor-agent mcp enable maxpm there."""
    from . import setup_entry
    from ..core import RiverError
    f = Path(path).expanduser() if path else cursor_dir() / "mcp.json"
    old = f.read_text(encoding="utf-8") if f.exists() else ""
    try:
        data = json.loads(old) if old.strip() else {}
    except ValueError as e:
        raise RiverError(f"{f} is not valid JSON ({e}); fix it first, MaximizePM does not overwrite it") from None
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        raise RiverError(f"{f}: expected an object with an mcpServers object; fix it first")
    servers = data.setdefault("mcpServers", {})
    if remove:
        if "maxpm" not in servers:
            return f"{f} has no MaximizePM entry; nothing changed"
        del servers["maxpm"]
    else:
        entry = setup_entry()
        entry = {**entry, "env": {k: v for k, v in entry["env"].items() if k != "MAXPM_CHAT"}}
        if servers.get("maxpm") == entry:
            return f"MaximizePM is already in {f}; nothing changed"
        servers["maxpm"] = entry
    if old:
        f.with_name(f.name + ".bak").write_text(old, encoding="utf-8")
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".maxpm-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, f)
    if remove:
        return f"took MaximizePM out of {f} (the old file: {f.name}.bak). New cursor-agent sessions no longer see it."
    return (f"added MaximizePM to {f}" + (f" (the old file: {f.name}.bak)" if old else "") + ".\n"
            "New cursor-agent sessions see its tools (cursor-agent mcp list shows it). Cursor asks once in each folder "
            "before it uses the server: approve it there, run cursor-agent mcp enable maxpm in the folder, or turn on "
            "the Cursor CLI setting 'Approve MCP servers' (--approve-mcps).")
