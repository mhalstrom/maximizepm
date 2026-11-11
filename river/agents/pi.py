"""The Pi adapter (docs/agent-adapters.md, #5170): pi, the coding agent of the npm package @earendil-works/pi-coding-agent
(before: @mariozechner/pi-coding-agent).

What this module rests on, read from Pi 0.73.1 (#5170) and checked against Pi 1.5.2, as installed (#5204: pi --help,
dist/core/slash-commands.js, docs/):

- Pi takes a free "provider/model" text (--model, with an optional ":<thinking>" suffix), so its family is "multi":
  no --min-model or --max-model limit applies to it.
- Thinking levels: off, minimal, low, medium, high, xhigh, max (--thinking; max is new in 1.x). /thinking <level>
  changes it in a running session (1.x; 0.73.1 had no command for it).
- Pi has no permission popups, no plan mode and no background bash (its README, "Design Principles"). Its own screens
  never wait for a person, so the state is working or idle. Only another extension's dialog can wait, and no
  extension sees that of another one: Pi reports no blocked state. Pi 1.x asks one question at start, the project
  trust question, but only in a folder with Pi resources of its own (.pi/settings.json, extensions, skills: its
  docs/security.md), before an extension loads; --approve trusts the folder for one run, /trust saves the answer.
- State and the session id come from one TypeScript extension (install_hooks): it calls maxpm report on
  session_start (idle, with the session id and the resume command), agent_start (working) and agent_end (idle), and
  maxpm release on session_shutdown with reason "quit". It runs only in a session that MaximizePM started (it needs
  MAXPM_AGENT), and it names the session with MAXPM_AGENT (/resume shows that name).
- Sessions: ~/.pi/agent/sessions/--<cwd with / as ->--/<timestamp>_<uuid>.jsonl ($PI_CODING_AGENT_DIR moves
  ~/.pi/agent, $PI_CODING_AGENT_SESSION_DIR the sessions). One JSON object per line; the entries form a tree by id and
  parentId, and the chat on the page is the branch from the last entry back to the root. A resume (--session <id>)
  appends to the same file; /fork and /clone start a new file whose header names the old one (parentSession).
- Pi sets PI_CODING_AGENT=true in its own process, so each command that its bash tool runs inherits it.
- A pasted image goes into Pi's prompt as the path of a temporary file, so the image paste of MaximizePM is a path.
"""

import json
import os
import re
import shlex
from pathlib import Path

from . import Raw

DESCRIBE = {
    "id": "pi",
    "label": "Pi",
    "exe": "pi",
    "aliases": ["pi-coding-agent"],
    "family": "multi",
    # Pi resolves a pattern against the models of the providers a person signed in to (pi --list-models): an exact
    # "provider/id" first, else the newest model whose id holds the text. So the model_ladder names go as they are
    # (opus: the newest Opus of any signed-in provider), and a person may name any "provider/id" instead. "" lets Pi
    # use its own defaultProvider and defaultModel (settings.json).
    "models": ["opus", "sonnet", "haiku"],
    "default_model": "",
    "model_ids": {},
    "prefix": "pi_",
}

# The settings of a start. "args" and "prompt" go in by build_command; the others by their flag.
SETTINGS = [
    {"key": "tools", "label": "Tools", "kind": "text", "choices": [], "default": "",
     "text": "the tools Pi may use, comma-separated (--tools; read, bash, edit, write, grep, find, ls); empty: all",
     "flag": ["--tools", "{value}"], "live": None, "read": r"(?:--tools|-t)[ =](\S+)"},
    {"key": "offline", "label": "Offline start", "kind": "toggle", "choices": [], "default": "off",
     "text": "no network at start: no update check and no telemetry (--offline)",
     "flag": ["--offline"], "live": None, "read": r"(--offline)\b"},
    {"key": "args", "label": "More arguments", "kind": "text", "choices": [], "default": "",
     "text": "more arguments, put before the prompt", "flag": [], "live": None, "read": None},
    {"key": "prompt", "label": "First prompt", "kind": "text", "choices": [],
     "default": "run maxpm go in this folder and follow the briefing",
     "text": "the first prompt (Pi reads AGENTS.md or CLAUDE.md of the folder)", "flag": [], "live": None,
     "read": None},
]

LEVELS = ["off", "minimal", "low", "medium", "high", "xhigh", "max"]

EFFORT = {
    "levels": LEVELS,
    "map": {},  # each MaximizePM level has the same name in Pi
    "flag": ["--thinking", "{value}"],
    "live": "/thinking {value}",
    # A thinking_level_change entry of the session file.
    "read": r'"type":"thinking_level_change".*"thinkingLevel":"([a-z]+)"',
}

LAUNCH = {
    "session_name": True,  # the state extension names the session with MAXPM_AGENT (pi.setSessionName)
    "system_prompt_file": True,  # --append-system-prompt takes a file's path and appends its text
    "fork": True,  # --fork <session id>
    "resume": True,  # --session <session id>: Pi appends to the same session file
    # No hooks_file (the state extension goes in by install_hooks: -e <file>), no extra_dirs (Pi has no sandbox), no
    # autocompact (Pi compacts by its own settings.json), no background (Pi has no background bash).
}

IDENTITY = {
    "source": "hook",  # the state extension reports ctx.sessionManager.getSessionId() on session_start
    "env": None,
    "log_glob": "~/.pi/agent/sessions/*/*.jsonl",
    "pane": None,
    "url": None,
    "inside_env": ["PI_CODING_AGENT"],
}

# The log is found by the newest session id that the extension reported (#5255: without "ref" the Sessions tab never
# asked for it). Pi's usage counts cache writes apart (cacheWrite), so a call after the cache ran out pays that price.
CHAT = {"compressed": False, "file_tools": ["write", "edit"], "ref": "session", "cache_write": True}

STATE = {"state": "hook", "session_id": "hook"}

INPUT = {"native": None}

# Pi's built-in slash commands (BUILTIN_SLASH_COMMANDS of dist/core/slash-commands.js, Pi 1.5.2), in its order, with
# the argument hints of docs/slash-commands.md; /mcp from the same page (built-in MCP support is on unless --no-mcp).
COMMANDS = [
    {"name": "settings", "description": "Open settings menu", "hint": ""},
    {"name": "model", "description": "Select model (opens selector UI)", "hint": "[provider/model]"},
    {"name": "tree", "description": "Navigate session tree (switch branches)", "hint": ""},
    {"name": "thinking", "description": "Set thinking level", "hint": "[level]"},
    {"name": "scoped-models", "description": "Enable/disable models for Ctrl+P cycling", "hint": ""},
    {"name": "export", "description": "Export session (HTML default, or specify path: .html/.jsonl)", "hint": "[path]"},
    {"name": "import", "description": "Import a JSONL session, preserving its ID and working directory", "hint": "<path>"},
    {"name": "share", "description": "Share session via a Radius artifact or a GitHub gist", "hint": ""},
    {"name": "bug", "description": "Report a bug to the Pi developers", "hint": "[description]"},
    {"name": "copy", "description": "Copy last agent message to clipboard", "hint": ""},
    {"name": "name", "description": "Set session display name", "hint": "[name]"},
    {"name": "session", "description": "Show session info and stats", "hint": ""},
    {"name": "changelog", "description": "Show changelog entries", "hint": ""},
    {"name": "hotkeys", "description": "Show all keyboard shortcuts", "hint": ""},
    {"name": "fork", "description": "Create a new fork from a previous user message", "hint": ""},
    {"name": "clone", "description": "Duplicate the current session at the current position", "hint": ""},
    {"name": "trust", "description": "Save project trust decision for future sessions", "hint": ""},
    {"name": "login", "description": "Configure provider authentication", "hint": "[provider]"},
    {"name": "logout", "description": "Remove provider authentication", "hint": ""},
    {"name": "new", "description": "Start a new session", "hint": ""},
    {"name": "compact", "description": "Manually compact the session context", "hint": "[instructions]"},
    {"name": "resume", "description": "Resume another session", "hint": ""},
    {"name": "reload", "description": "Reload keybindings, extensions, skills, prompts, themes, and context files",
     "hint": ""},
    {"name": "mcp", "description": "Manage MCP servers", "hint": ""},
    {"name": "quit", "description": "Quit pi", "hint": ""},
]

SETUP = {
    "instructions": "AGENTS.md",  # Pi reads AGENTS.md or CLAUDE.md, from ~/.pi/agent and each folder up to cwd
    "skills_dir": "~/.pi/agent/skills",
    "voice": None,
    "open_link": None,
}


# ---------------------------------------------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------------------------------------------

def agent_dir(env=None):
    """Pi's own folder: $PI_CODING_AGENT_DIR, or ~/.pi/agent."""
    env = os.environ if env is None else env
    return Path(env.get("PI_CODING_AGENT_DIR") or "~/.pi/agent").expanduser()


def sessions_dir(env=None):
    """The folder of the session folders: $PI_CODING_AGENT_SESSION_DIR, or sessions/ in agent_dir()."""
    env = os.environ if env is None else env
    got = env.get("PI_CODING_AGENT_SESSION_DIR")
    return Path(got).expanduser() if got else agent_dir(env) / "sessions"


def folder_key(folder):
    """The name of the session folder of a working folder, as Pi's getDefaultSessionDir makes it: the leading slash
    goes, each / \\ and : becomes -, with -- before and after (/Users/a/p -> --Users-a-p--)."""
    return "--" + re.sub(r"[/\\:]", "-", re.sub(r"^[/\\]", "", str(folder))) + "--"


# ---------------------------------------------------------------------------------------------------------------
# 4. Launch
# ---------------------------------------------------------------------------------------------------------------

def _on(v):
    return v is True or str(v).strip().lower() in ("on", "true", "1", "yes")


def _skill_file():
    """A file of the maxpm skill for --append-system-prompt (the same file as Claude Code's skill_prompt), or None."""
    from .. import core
    words = shlex.split(core.skill_prompt_args(""))
    return words[-1] if words[-2:-1] == ["--append-system-prompt-file"] else None


def build_command(name, folder, model, effort, settings, prompt):
    """pi [--model <provider/model>] [--thinking <level>] [--tools T] [--offline] [--append-system-prompt <skill>]
    [--fork <id> | --session <id>] [args] [prompt]. name: the state extension reads it from MAXPM_AGENT."""
    s = settings or {}
    argv = ["pi"]
    if model:
        argv += ["--model", model]
    level = EFFORT["map"].get(effort, effort) if effort else None
    if level:
        argv += [w.format(value=level) for w in EFFORT["flag"]]
    if (s.get("tools") or "").strip():
        argv += ["--tools", s["tools"].strip()]
    if _on(s.get("offline", "off")):
        argv.append("--offline")
    if s.get("system_prompt_file"):
        f = _skill_file()
        argv += ["--append-system-prompt", f] if f else []
    if s.get("fork"):
        argv += ["--fork", str(s["fork"])]
    elif s.get("resume"):
        argv += ["--session", str(s["resume"])]
    if (s.get("args") or "").strip():
        argv.append(Raw(s["args"].strip()))
    if prompt:
        argv.append(prompt)
    return argv


def parse_command(words):
    """A hand-written pi command that a profile builds exactly: --model {model}, --thinking {effort}, --tools T,
    --offline, and one prompt. Anything else (print mode, an API key, a session picker, another flag): None."""
    if not words or Path(words[0]).name.lower() != "pi":
        return None
    opts, prompt, i = {}, None, 1
    while i < len(words):
        t, nxt = words[i], (words[i + 1] if i + 1 < len(words) else None)
        if t == "--model" and nxt == "{model}" or t == "--thinking" and nxt == "{effort}":
            i += 2
        elif t in ("--tools", "-t") and nxt and not nxt.startswith("-"):
            opts["tools"] = nxt
            i += 2
        elif t == "--offline":
            opts["offline"] = "on"
            i += 1
        elif not t.startswith("-") and prompt is None and t.strip() and ";" not in t:
            prompt = t
            i += 1
        else:
            return None
    return {"settings": opts, "prompt": prompt} if prompt is not None else None


# ---------------------------------------------------------------------------------------------------------------
# 7. State: the extension
# ---------------------------------------------------------------------------------------------------------------

EXTENSION_NAME = "maxpm-state.ts"

# The extension that reports Pi's state to MaximizePM (docs/agent-adapters.md, step 7). __REPORT__ is the maxpm report
# command as a JSON list, __SELF__ the path of this file (the resume command loads it again). No import: Pi loads a
# .ts file as it is, and the types are only a help for the reader.
EXTENSION = """\
// MaximizePM: reports this Pi session's state with maxpm report (written by river/agents/pi.py; do not edit).
const REPORT: string[] = __REPORT__;
const SELF: string = __SELF__;
const RELEASE: string[] = REPORT.map((w) => (w === "report" ? "release" : w));

export default function (pi: any) {
  const agent = process.env.MAXPM_AGENT;
  if (!agent) return; // a session that MaximizePM did not start: no report
  let seq = 0;
  let sessionId: string | undefined;
  const run = (argv: string[]) => {
    // A report never waits for maxpm, and its failure never stops Pi.
    pi.exec(argv[0], argv.slice(1), { timeout: 15000 }).catch(() => {});
  };
  const report = (state: string) => {
    // The number grows across restarts of the same session too: a late report never replaces a newer one.
    seq = Math.max(seq + 1, Date.now());
    const args = ["--state", state, "--seq", String(seq)];
    if (sessionId) args.push("--session-id", sessionId, "--", "pi", "-e", SELF, "--session", sessionId);
    run([...REPORT, ...args]);
  };
  pi.on("session_start", async (_event: any, ctx: any) => {
    sessionId = ctx.sessionManager.getSessionId();
    if (!pi.getSessionName()) pi.setSessionName(agent);
    report("idle");
  });
  pi.on("agent_start", async () => report("working"));
  pi.on("agent_end", async () => report("idle"));
  pi.on("session_shutdown", async (event: any) => {
    if (event.reason === "quit") run(RELEASE);
  });
}
"""


def extension_text(report, path):
    """The extension's text for the maxpm report command `report` (argv words), written at `path`."""
    return EXTENSION.replace("__REPORT__", json.dumps(list(report))).replace("__SELF__", json.dumps(str(path)))


def install_hooks(folder, report):
    """Write the state extension into the queue folder (pi/maxpm-state.ts beside the queue), and return the words
    that load it: -e <file>. It changes nothing in ~/.pi: a pi that a person starts by hand reports nothing.
    folder: the session's folder (the extension is the same for each folder)."""
    from ..core import river_dir
    path = Path(river_dir()) / "pi" / EXTENSION_NAME
    text = extension_text(report, path)
    try:
        same = path.read_text() == text
    except OSError:
        same = False
    if not same:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(text)
        tmp.replace(path)
    return ["-e", str(path)]


# ---------------------------------------------------------------------------------------------------------------
# 9. Resume
# ---------------------------------------------------------------------------------------------------------------

def resume_command(session_ref, model, effort, settings, name=None):
    """pi --session <id>: Pi appends to the same session file. Model and effort go on only when they are given: else
    the session keeps the model and thinking level it had (its last model_change and thinking_level_change)."""
    if not session_ref:
        return None
    s = {k: v for k, v in (settings or {}).items() if k not in ("fork", "prompt")}
    return build_command(name, None, model, effort, {**s, "resume": session_ref}, None)


# ---------------------------------------------------------------------------------------------------------------
# 6. Chat log
# ---------------------------------------------------------------------------------------------------------------

def chat_path(session_ref, folder=None, root=None):
    """The session file of a session id (or its first characters) or of a path: first in the folder's session
    folder, then in every session folder (under root in place of Pi's sessions folder, when given); None when there
    is none."""
    ref = str(session_ref or "").strip()
    if not ref:
        return None
    p = Path(ref).expanduser()
    if ref.endswith(".jsonl") or os.sep in ref:
        return p if p.is_file() else None
    if not re.fullmatch(r"[0-9A-Za-z-]+", ref):
        return None
    root = Path(root) if root else sessions_dir()
    for pattern in ([f"{folder_key(folder)}/*_{ref}*.jsonl"] if folder else []) + [f"*/*_{ref}*.jsonl"]:
        hits = sorted(root.glob(pattern))
        if hits:
            return hits[-1]
    return None


def _rows(lines):
    """The JSON objects of the lines; a line that is no JSON (one Pi still writes) is left out."""
    out = []
    for ln in lines:
        ln = ln.strip() if isinstance(ln, str) else ln
        if not ln:
            continue
        try:
            row = json.loads(ln)
        except (ValueError, TypeError):
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _branch(rows):
    """The entries on the active branch, root first: from the last entry with an id, back by parentId. Pi appends each
    new entry under the leaf, so the last entry is the leaf."""
    by_id = {r["id"]: r for r in rows if r.get("id") and r.get("type") != "session"}
    leaf = next((r for r in reversed(rows) if r.get("id") and r.get("type") != "session"), None)
    path, seen = [], set()
    while leaf is not None and leaf["id"] not in seen:
        seen.add(leaf["id"])
        path.append(leaf)
        leaf = by_id.get(leaf.get("parentId"))
    return path[::-1]


def _text(content):
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or () if isinstance(b, dict) and b.get("type") == "text")


def _images(content):
    if isinstance(content, str):
        return []
    return [f"data:{b.get('mimeType', 'image/png')};base64,{b.get('data', '')}"
            for b in content or () if isinstance(b, dict) and b.get("type") == "image"]


# The argument that says best what a call of each built-in tool does.
TOOL_DETAIL = {"bash": "command", "read": "path", "edit": "path", "write": "path", "grep": "pattern", "find": "pattern",
               "ls": "path"}


# Pi's tool names, as the names of the same tools that plain_tool knows: what a call does in plain words.
_PLAIN_NAMES = {"bash": "Bash", "read": "Read", "edit": "Edit", "write": "Write", "grep": "Grep", "find": "Glob",
                "ls": "LS"}


def _tool(at, name, cid, args):
    """The page line of one tool call: "<tool>: <what>" (the command, the path, the pattern), as for Claude Code."""
    from .. import sessions
    args = args if isinstance(args, dict) else {}
    return {"at": at, "role": "tool", "text": sessions._tool_line(name, args), "id": cid, "name": name,
            "plain": sessions.plain_tool(_PLAIN_NAMES.get(name, name), args), "detail": _detail(name, args)}


def _detail(name, args):
    args = args if isinstance(args, dict) else {}
    key = TOOL_DETAIL.get(name)
    if key and isinstance(args.get(key), str):
        return args[key]
    return json.dumps(args, ensure_ascii=False)[:300] if args else ""


def _entries(row):
    """The shared entries of one session entry."""
    at = row.get("timestamp")
    kind = row.get("type")
    if kind == "message":
        m = row.get("message") or {}
        role = m.get("role")
        if role == "user":
            e = {"at": at, "role": "user", "text": _text(m.get("content"))}
            imgs = _images(m.get("content"))
            return [{**e, "images": imgs} if imgs else e]
        if role == "assistant":
            out = []
            for b in m.get("content") or ():
                if b.get("type") == "text" and b.get("text", "").strip():
                    out.append({"at": at, "role": "assistant", "text": b["text"]})
                elif b.get("type") == "toolCall":
                    out.append(_tool(at, b.get("name", ""), b.get("id", ""), b.get("arguments")))
            if m.get("stopReason") == "error" and m.get("errorMessage"):
                out.append({"at": at, "role": "event", "text": f"error: {m['errorMessage']}"})
            if m.get("stopReason") == "aborted":
                out.append({"at": at, "role": "event", "text": "interrupted"})
            return out
        if role == "toolResult":
            return [{"at": at, "role": "result", "text": _text(m.get("content")), "for": m.get("toolCallId", ""),
                     "error": bool(m.get("isError"))}]
        if role == "bashExecution":  # a !command the person typed
            rid = row.get("id", "")
            return [_tool(at, "bash", rid, {"command": m.get("command", "")}),
                    {"at": at, "role": "result", "text": m.get("output", ""), "for": rid,
                     "error": bool(m.get("exitCode")) or bool(m.get("cancelled"))}]
        if role == "custom" and m.get("display"):
            return [{"at": at, "role": "event", "text": _text(m.get("content"))}]
        if role in ("branchSummary", "compactionSummary"):
            return [{"at": at, "role": "summary", "text": m.get("summary", "")}]
        return []
    if kind == "compaction":
        return [{"at": at, "role": "divider", "text": "compacted", "kind": "compacted"},
                {"at": at, "role": "summary", "text": row.get("summary", "")}]
    if kind == "branch_summary":
        return [{"at": at, "role": "summary", "text": row.get("summary", "")}]
    if kind == "custom_message" and row.get("display"):
        return [{"at": at, "role": "event", "text": _text(row.get("content"))}]
    if kind == "model_change":
        return [{"at": at, "role": "event", "text": f"model: {row.get('provider')}/{row.get('modelId')}"}]
    if kind == "thinking_level_change":
        return [{"at": at, "role": "event", "text": f"thinking: {row.get('thinkingLevel')}"}]
    return []


def _turn(path):
    """working or idle from the last message on the branch, or None when it has none. A system message (Pi 1.x writes
    one when the tools or skills change) says nothing of the turn."""
    last = next((r["message"] for r in reversed(path) if r.get("type") == "message" and r.get("message")
                 and r["message"].get("role") != "system"), None)
    if last is None:
        return None
    role = last.get("role")
    if role == "assistant":
        return "working" if last.get("stopReason") == "toolUse" else "idle"
    if role in ("user", "toolResult"):
        return "working"
    return "idle"


def parse_chat(lines):
    """(entries, turn) of the lines of one session file: the active branch, root first."""
    path = _branch(_rows(lines))
    entries = [e for row in path for e in _entries(row)]
    return entries, _turn(path)


def _header(lines):
    return next((r for r in _rows(lines[:5]) if r.get("type") == "session"), None)


def _head(p):
    """(header, id of the first entry) of a session file; (None, None) when it cannot be read."""
    try:
        with open(p) as f:
            rows = _rows([f.readline(), f.readline()])
    except OSError:
        return None, None
    return (next((r for r in rows if r.get("type") == "session"), None),
            next((r.get("id") for r in rows if r.get("type") != "session"), None))


def chat_chain(path, refs=None):
    """The session files across forks and clones, oldest first: each header's parentSession, back to the first (it
    needs no refs, #5182). A /fork or /clone file holds a copy of its branch from the root, with the same entry ids
    (Pi 1.5.2, #5204): its parent adds nothing, and the chain stops there, so the chat shows no message twice. A
    child that does not start with its parent's first entry (newSession with a parentSession) continues it."""
    p = Path(path)
    out, (head, first) = [p], _head(p)
    while (head or {}).get("parentSession"):
        parent = Path(head["parentSession"])
        head, parent_first = _head(parent)
        if parent in out or head is None or (first and first == parent_first):
            break
        out.append(parent)
        first = parent_first
    return out[::-1]


def chat_title(lines):
    """The session's name: the last session_info entry (/name, or the name the extension set)."""
    return next((r.get("name") for r in reversed(_rows(lines)) if r.get("type") == "session_info" and r.get("name")),
                None)


def chat_cwd(lines):
    return (_header(list(lines)) or {}).get("cwd")


def chat_usage(lines):
    """The tokens of every model call in the file, on every branch: Pi bills each call once. Also the usage entries of
    Pi 1.x (a call with no message, such as a cache warm)."""
    tot = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    for r in _rows(lines):
        m = r.get("message") if r.get("type") == "message" else None
        u = (m or {}).get("usage") if (m or {}).get("role") == "assistant" else None
        if r.get("type") == "usage":
            u = r.get("usage")
        if isinstance(u, dict):
            for k, src in (("input", "input"), ("output", "output"), ("cache_read", "cacheRead"),
                           ("cache_write", "cacheWrite")):
                tot[k] += int(u.get(src) or 0)
    return tot


def read_effort(entries):
    """The thinking level from the shared entries: the last 'thinking: <level>' event, or None."""
    return next((e["text"].split(": ", 1)[1] for e in reversed(entries or ())
                 if e.get("role") == "event" and e.get("text", "").startswith("thinking: ")), None)


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
    meta = {}
    for ln in parts[1].splitlines():
        k, sep, v = ln.partition(":")
        if sep:
            meta[k.strip()] = v.strip().strip("\"'")
    return meta, parts[2]


def _templates(d):
    out = []
    for f in sorted(Path(d).glob("*.md")) if Path(d).is_dir() else ():
        try:
            meta, body = _front(f.read_text(errors="replace"))
        except OSError:
            continue
        first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
        out.append({"name": f.stem, "description": meta.get("description") or first[:120],
                    "hint": meta.get("argument-hint", "")})
    return out


def _skills(d, top_md=False):
    """Skills as /skill:<name>: each folder with SKILL.md (any depth), and with top_md (Pi's own skills folders,
    ~/.pi/agent/skills and .pi/skills) each .md file at the top too."""
    d = Path(d)
    if not d.is_dir():
        return []
    files = sorted(d.rglob("SKILL.md"))
    if top_md:
        files += sorted(f for f in d.glob("*.md") if f.name != "SKILL.md")
    out = []
    for f in files:
        try:
            meta, _ = _front(f.read_text(errors="replace"))
        except OSError:
            continue
        name = meta.get("name") or (f.parent.name if f.name == "SKILL.md" else f.stem)
        out.append({"name": f"skill:{name}", "description": meta.get("description", ""), "hint": ""})
    return out


def read_commands(folder):
    """The prompt templates (/name) and skills (/skill:name) of a session in folder: ~/.pi/agent/prompts and
    .pi/prompts; ~/.pi/agent/skills, ~/.agents/skills, .pi/skills, and .agents/skills of the folder and each folder
    up to the git root. Commands of extensions and packages are not read: only a running Pi knows them."""
    home = agent_dir()
    f = Path(folder).expanduser() if folder else None
    out = _templates(home / "prompts") + (_templates(f / ".pi" / "prompts") if f else [])
    out += _skills(home / "skills", True) + _skills(Path("~/.agents/skills").expanduser())
    if f:
        out += _skills(f / ".pi" / "skills", True)
        d = f
        while True:
            out += _skills(d / ".agents" / "skills")
            if (d / ".git").exists() or d.parent == d:
                break
            d = d.parent
    seen, uniq = set(), []
    for c in out:
        if c["name"] not in seen:
            seen.add(c["name"])
            uniq.append(c)
    return uniq
