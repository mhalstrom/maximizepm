"""The Hermes adapter (docs/agent-adapters.md, #5173): hermes, the Hermes Agent CLI of Nous Research.

What this module rests on, read from Hermes Agent v0.22.7 (its source in ~/.hermes/hermes-agent, and a session that
it ran in tmux on 2026-11-10; tests/fixtures/hermes holds what that session printed and stored):

- Hermes takes a model of any provider: --provider names the provider (openai-codex, anthropic, nous, openrouter, ...)
  and -m the model of that provider, so its family is "multi" and no --min-model or --max-model limit applies.
  MaximizePM starts it on openai-codex with gpt-6.1-terra (mark, 2026-11-03).
- `hermes chat --cli` is the classic prompt_toolkit REPL; --tui is another screen. MaximizePM always starts the
  classic one, so the screen rules of DETECT fit. `-q <prompt>` on a real terminal seeds an interactive session.
- Effort: --reasoning none|minimal|low|medium|high|xhigh|max|ultra, and /reasoning <level> in a running session.
- The chat is no file: Hermes keeps every session in one SQLite database, $HERMES_HOME/state.db (~/.hermes). A
  session is a row of `sessions` (id "local-<64 hex>" for an interactive one; the banner shows its first 8 hex
  digits), its messages are rows of `messages`. chat_lines() reads them as JSON lines (the session row first), and
  parse_chat() and the other chat readers take those lines. chat_path() keeps a copy of them in a file beside the
  queue (<queue folder>/hermes/<session id>.jsonl), since the Sessions tab reads a chat log as a file.
- State comes from the screen (DETECT): a turn shows "type to redirect this turn" under the prompt; at the prompt the
  status bar shows the idle timer "⏲"; a question or a command approval shows a box and "↑/↓ to select".
- The session id comes from the environment: Hermes gives each shell command of its terminal tool HERMES_SESSION_ID
  (IDENTITY['env']), so a maxpm command that runs inside Hermes knows its session. The tools of every session run in
  Hermes's one background gateway, not in the CLI of the pane: the CLI sends the gateway only an allowlist of its own
  variables (PATH, a venv, nvm and the like), and MaximizePM's variables travel only when the config names them
  (terminal.client_env_passthrough, #5241); see CLIENT_ENV.
- Resume: hermes chat --resume <session id>. Hermes reads AGENTS.md, CLAUDE.md and .cursorrules of the folder.
"""

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import Raw

# ---------------------------------------------------------------------------------------------------------------
# 1. Describe
# ---------------------------------------------------------------------------------------------------------------

DEFAULT_PROVIDER = "openai-codex"

DESCRIBE = {
    "id": "hermes",
    "label": "Hermes",
    "exe": "hermes",
    "aliases": ["hermes-agent"],
    "family": "multi",
    # The models of the default provider (openai-codex) that a person picks most; read_models() reads the full list
    # that Hermes cached for that provider. -m takes the id as it is.
    "models": ["gpt-6.1-terra", "gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna"],
    "default_model": "gpt-6.1-terra",
    "model_ids": {},
    "prefix": "hermes_",
    # /model <id> switches the model of the running session (session-scoped); a resume cannot (#5252).
    "model_live": "/model {value}",
    # The Settings dialog of a running session reads the model back from its command line (#5253).
    "model_read": r"(?:^|\s)(?:-m|--model)[ =](\S+)",
}


def hermes_home(env=None):
    """Hermes's own folder: $HERMES_HOME, else ~/.hermes."""
    env = os.environ if env is None else env
    return Path(os.path.expanduser(env.get("HERMES_HOME") or "~/.hermes"))


def read_models(provider=DEFAULT_PROVIDER):
    """The model ids that Hermes cached for `provider` ($HERMES_HOME/provider_models_cache.json), or None."""
    try:
        got = json.loads((hermes_home() / "provider_models_cache.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    models = (got.get(provider) or {}).get("models") if isinstance(got, dict) else None
    return [m for m in models if isinstance(m, str)] if isinstance(models, list) and models else None


# ---------------------------------------------------------------------------------------------------------------
# 2. Settings
# ---------------------------------------------------------------------------------------------------------------

SETTINGS = [
    {"key": "provider", "label": "Provider", "kind": "text", "choices": [], "default": DEFAULT_PROVIDER,
     "text": "the inference provider (--provider: openai-codex, anthropic, nous, openrouter, ...); empty: the "
             "provider of Hermes's config.yaml", "flag": ["--provider", "{value}"], "live": None,
     "read": r"--provider[ =](\S+)"},
    {"key": "yolo", "label": "Run every command", "kind": "toggle", "choices": [], "default": "off",
     "text": "no approval question before a dangerous command (--yolo)", "flag": ["--yolo"], "live": None,
     "read": r"(--yolo)\b"},
    {"key": "checkpoints", "label": "Checkpoints", "kind": "toggle", "choices": [], "default": "off",
     "text": "a file system checkpoint before each destructive file change; /rollback restores it (--checkpoints)",
     "flag": ["--checkpoints"], "live": None, "read": r"(--checkpoints)\b"},
    {"key": "toolsets", "label": "Toolsets", "kind": "text", "choices": [], "default": "",
     "text": "the toolsets it may use, comma-separated (--toolsets); empty: those of Hermes's config",
     "flag": ["--toolsets", "{value}"], "live": None, "read": r"(?:--toolsets|-t)[ =](\S+)"},
    {"key": "skills", "label": "Skills", "kind": "text", "choices": [], "default": "",
     "text": "skills to load at the start, comma-separated (--skills)", "flag": ["--skills", "{value}"],
     "live": None, "read": r"(?:--skills|-s)[ =](\S+)"},
    {"key": "args", "label": "More arguments", "kind": "text", "choices": [], "default": "",
     "text": "more arguments, put before the prompt", "flag": [], "live": None, "read": None},
    {"key": "prompt", "label": "First prompt", "kind": "text", "choices": [],
     "default": "run maxpm go in this folder and follow the briefing",
     "text": "the first prompt (Hermes reads AGENTS.md and CLAUDE.md of the folder)", "flag": [], "live": None,
     "read": None},
]

# ---------------------------------------------------------------------------------------------------------------
# 3. Effort
# ---------------------------------------------------------------------------------------------------------------

EFFORT = {
    "levels": ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
    "map": {},  # MaximizePM's five levels are Hermes levels of the same name
    "flag": ["--reasoning", "{value}"],
    "live": "/reasoning {value}",
    "read": r"--reasoning[ =]([a-z]+)\b",
}

# ---------------------------------------------------------------------------------------------------------------
# 4. Launch
# ---------------------------------------------------------------------------------------------------------------

# Hermes has no flag for a session name (-c <name> --create-if-missing resumes a session of that title when one
# exists), and no flag for more folders or a system prompt file.
# fixed_on_resume (#5252): Hermes freezes a session's creation flags at its start (hermes_cli/gateway_chat.py _POLICY:
# model, provider, reasoning, toolsets, yolo, skills, checkpoints, ...). A resume may repeat one with the same value and
# refuses a different one ("Resume retains gateway session policy; creation overrides are unsupported on resume"), so
# resume_command passes none of them: the session keeps them all (also --checkpoints), and /model and /reasoning
# change the model and effort of the running session.
LAUNCH = {"resume": True, "fixed_on_resume": True}

_TOGGLES = [s for s in SETTINGS if s["kind"] == "toggle"]
_VALUED = [s for s in SETTINGS if s["kind"] != "toggle" and s["flag"]]


def _on(v):
    return str(v or "").strip().lower() in ("on", "true", "1", "yes")


def build_command(name, folder, model, effort, settings, prompt):
    """hermes chat --cli [--resume <id>] [--provider P] [-m M] [--reasoning E] [--yolo] [--checkpoints]
    [--toolsets T] [--skills S] <args> [-q <prompt>]. name and folder go nowhere: Hermes takes no session name, and
    MaximizePM opens the pane in the folder. The provider setting goes on when it is set; its default is
    openai-codex, so an empty value is the one way to use the provider of Hermes's own config."""
    s = settings
    words = ["hermes", "chat", "--cli"] + (["--resume", s["resume"]] if s.get("resume") else [])
    provider = s.get("provider", DEFAULT_PROVIDER)
    words += ["--provider", provider] if provider else []
    words += (["-m", model] if model else []) + ([w.format(value=effort) for w in EFFORT["flag"]] if effort else [])
    for spec in _TOGGLES:
        words += list(spec["flag"]) if _on(s.get(spec["key"], spec["default"])) else []
    for spec in _VALUED:
        if spec["key"] != "provider" and str(s.get(spec["key"]) or "").strip():
            words += [w.format(value=str(s[spec["key"]]).strip()) for w in spec["flag"]]
    words += [Raw(s["args"].strip())] if str(s.get("args") or "").strip() else []
    words += ["-q", prompt] if prompt else []
    return words


def resume_command(session_ref, model, effort, settings, name=None):
    """hermes chat --cli --resume <session id> [<args>] [-q <prompt>]: settings['prompt'] is the first prompt after
    the resume. No creation flag (no --provider, -m, --reasoning, toggle or toolset): Hermes keeps those the session
    started with and refuses one that differs (LAUNCH fixed_on_resume, #5252), so model, effort and settings go
    nowhere here. Hermes goes back into the session's recorded folder."""
    if not session_ref:
        return None
    s = settings or {}
    words = ["hermes", "chat", "--cli", "--resume", session_ref]
    words += [Raw(s["args"].strip())] if str(s.get("args") or "").strip() else []
    return words + (["-q", s["prompt"]] if s.get("prompt") else [])


_PARSE_TOGGLES = {w: spec["key"] for spec in _TOGGLES for w in spec["flag"]}
_PARSE_VALUED = {"--provider": "provider", "--toolsets": "toolsets", "-t": "toolsets", "--skills": "skills",
                 "-s": "skills"}


def parse_command(words):
    """A hand-written hermes command that a profile builds exactly: hermes [chat] [--cli], -m {model}, --reasoning
    {effort}, the setting flags, and one prompt (-q <prompt>, or the last plain word); None for anything else."""
    if not words or Path(words[0]).name.lower() != "hermes":
        return None
    opts, prompt, i = {"provider": ""}, None, 1
    if i < len(words) and words[i] == "chat":
        i += 1
    while i < len(words):
        t, nxt = words[i], (words[i + 1] if i + 1 < len(words) else None)
        if t == "--cli":
            i += 1
        elif t in ("-m", "--model") and nxt == "{model}" or t == "--reasoning" and nxt == "{effort}":
            i += 2
        elif t in _PARSE_TOGGLES:
            opts[_PARSE_TOGGLES[t]] = "on"
            i += 1
        elif t in _PARSE_VALUED and nxt is not None and not nxt.startswith("-"):
            opts[_PARSE_VALUED[t]] = nxt
            i += 2
        elif t in ("-q", "--query") and nxt is not None and prompt is None:
            prompt = nxt
            i += 2
        elif not t.startswith("-") and prompt is None and t.strip() and ";" not in t:
            prompt = t
            i += 1
        else:
            return None
    return {"settings": opts, "prompt": prompt} if prompt is not None else None


# ---------------------------------------------------------------------------------------------------------------
# 5. Session identity
# ---------------------------------------------------------------------------------------------------------------

# Hermes gives each shell command of its terminal tool the session's id in HERMES_SESSION_ID, so a maxpm command that
# runs inside Hermes knows its session, as CODEX_THREAD_ID tells it for Codex. Those commands run in Hermes's gateway
# (agent/session_client_env.py of Hermes 0.22.7): the CLI in the pane sends it, with session.create, only the
# variables of its allowlist, and terminal.client_env_passthrough adds names to that list. terminal.env_passthrough
# (#5211) is no help: it passes names from the gateway's own environment, which never had MaximizePM's variables. So
# MAXPM_AGENT, MAXPM_DB and the others reach maxpm only after `hermes config set terminal.client_env_passthrough ...`
# (CLIENT_ENV); before it, every maxpm command of a Hermes session reads the default queue as an unnamed agent.
# The first process above a maxpm command of a Hermes session is that shared gateway (a Python with no terminal,
# started once for every session), not the CLI in the pane: MaximizePM records it only when it runs on a terminal, so
# the busy checks and maxpm stop --kill never act on the gateway (#5247).
IDENTITY = {"source": "env", "env": "HERMES_SESSION_ID", "log_glob": None, "pane": None, "url": None,
            "inside_env": ["HERMES_SESSION_ID"], "process": "terminal"}

# The variables of MaximizePM that a maxpm command inside Hermes needs, for terminal.client_env_passthrough. Hermes
# lets a name travel unless it holds a credential word (KEY, TOKEN, SECRET, AUTH, ...) or starts with HERMES_.
CLIENT_ENV = ["MAXPM_AGENT", "MAXPM_FOCUS", "MAXPM_DB", "MAXPM_MODEL", "MAXPM_AGENT_TYPE", "MAXPM_QUIET"]


def client_env_command():
    """The command that lets MaximizePM's variables reach the shell commands of Hermes (a person runs it once; it
    applies to the sessions that start after it)."""
    return ["hermes", "config", "set", "terminal.client_env_passthrough", "[" + ", ".join(CLIENT_ENV) + "]"]


# ---------------------------------------------------------------------------------------------------------------
# 7. State: the screen rules
# ---------------------------------------------------------------------------------------------------------------

STATE = {"state": "detect", "session_id": "identity"}

DETECT = {
    # The CLI's own name (#5227): "python3.14", the process name tmux shows, would match every Python 3.14 program
    # in a pane. The hermes launcher is a shell script that execs the Python that Hermes installed for itself (3.14.9
    # in v0.22.7) as '<HERMES_HOME>/tools/python-.../bin/python3 -I -c <script>', so ps shows no script name either.
    "process": "hermes",
    # The launcher's script puts Hermes's source folder on sys.path first; ps shows that line in its command line
    # (#5229), for any HERMES_HOME.
    "process_args": r"sys\.path\.insert\(0, '[^']*/hermes-agent'\)",
    # At the prompt the status bar shows the idle timer (⏲ 59s); during a turn it shows ⏱.
    "idle": r"⏲ \d+(?:\.\d+)?[smh]\b",
    # During a turn the prompt line says that typed text redirects the turn.
    "working": r"type to redirect this turn",
    "blocked": [
        r"Hermes Agent needs your input",  # the clarify tool's question box
        r"⚠️? *Dangerous Command",  # a command approval
        r"↑/↓ to select, Enter to (?:lock|confirm)",  # every menu that waits for a person
    ],
    # The question box ends with an option that lets the person type the answer (a regex).
    "tell_instead": r"Other \(type your answer\)",
    # The layout of a blocked screen (river/agents DETECT_LAYOUT; server._prompt_found reads it). The menu marks its
    # selected option with ❯ inside a frame of │ ╭ ╰, as the defaults say. The lines inside the box under the menu
    # (an approval's reason, " delete in root path") are the prompt's (#5213). Under the box the watcher accepts only
    # a rule or a key hint, and Hermes draws more there, so "rule" also takes those lines: the spinner line
    # ("── ⠧ ❓ <question> · 25s ─── <title> ─"), the input line of a blocked turn ("? ❯", "⚠ ❯"), and the status bar
    # (" ☤ <model> · ...").
    "rule": r"^\s*[─━]{8,}\s*$|^\s*── .*─\s*$|^\s*[?⚠] ❯\s*$|^\s*☤ ",
    "hint": r"↑/↓ to select, Enter to (?:lock|confirm)",
    # A path of an image file as the first word of a prompt attaches the image; the prompt then shows a badge
    # "[📎 <file name>]" and no number, so MaximizePM pastes the path.
    "image_paste": None,
}

# Stop on the Sessions tab (#5547): Ctrl+C ends the turn; a second one at the prompt quits.
INPUT = {"native": None, "interrupt": ["C-c"]}

# ---------------------------------------------------------------------------------------------------------------
# 6. Chat log
# ---------------------------------------------------------------------------------------------------------------

# The chat of a session is rows of Hermes's database, not a file. The Sessions tab reads a chat log as a file, line by
# line (sessions.py); so chat_path keeps a copy of the session's rows as JSON lines in <queue folder>/hermes/<session
# id>.jsonl, and brings it up to date at each call. CHAT["ref"]: the session id that the agent's maxpm commands record.
# "commands": Hermes keeps no record of a local command (/reasoning show) in state.db (#5253).
CHAT = {"compressed": False, "file_tools": ["write_file", "patch"], "ref": "session", "cache_write": False,
        "commands": False}

_SESSION_COLS = ("id", "source", "model", "model_config", "billing_provider", "cwd", "title", "parent_session_id",
                 "started_at", "ended_at", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                 "reasoning_tokens")
_MESSAGE_COLS = ("id", "role", "content", "tool_call_id", "tool_calls", "tool_name", "timestamp", "finish_reason",
                 "display_kind", "_compressed_summary", "compacted", "active")
_SAFE_ID = re.compile(r"[A-Za-z0-9_.-]{1,200}\Z")


def db_path(root=None):
    """Hermes's database: state.db in root (a folder that a test gives), else in $HERMES_HOME (~/.hermes)."""
    return (Path(root) if root else hermes_home()) / "state.db"


def copy_dir():
    """Where chat_path keeps the copies of the chats: hermes/ beside the queue."""
    from ..core import river_dir
    return Path(river_dir()) / "hermes"


def _connect(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)


def _session_id(conn, ref):
    """The full id of session ref: the id itself, or the first characters of one (the banner's 8 hex digits)."""
    row = conn.execute("SELECT id FROM sessions WHERE id = ?", (ref,)).fetchone()
    if row:
        return row[0]
    rows = conn.execute("SELECT id FROM sessions WHERE id LIKE ? OR id LIKE ? ORDER BY started_at DESC LIMIT 2",
                        (ref.replace("%", "") + "%", "local-" + ref.replace("%", "") + "%")).fetchall()
    return rows[0][0] if len(rows) == 1 else None


def _read_session(session_ref, after=0, root=None):
    """(session id, the session line, [message lines]) of a session, or None when the database has no such session."""
    ref, path = str(session_ref or "").strip(), db_path(root)
    if not ref or not path.is_file():
        return None
    try:
        conn = _connect(path)
        try:
            sid = _session_id(conn, ref)
            if not sid:
                return None
            s = conn.execute(f"SELECT {', '.join(_SESSION_COLS)} FROM sessions WHERE id = ?", (sid,)).fetchone()
            rows = conn.execute(f"SELECT {', '.join(_MESSAGE_COLS)} FROM messages WHERE session_id = ? AND id > ? "
                                "ORDER BY id", (sid, int(after or 0))).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return (sid, json.dumps({"type": "session", **dict(zip(_SESSION_COLS, s))}),
            [json.dumps({"type": "message", **dict(zip(_MESSAGE_COLS, r))}) for r in rows])


def chat_lines(session_ref, after=0, root=None):
    """The lines of a session for parse_chat: one JSON object for the session row ({"type": "session", ...}), then
    one for each message row after message id `after` ({"type": "message", ...}), in order. [] when there is none."""
    got = _read_session(session_ref, after, root)
    return [got[1]] + got[2] if got else []


def _sync(path, session, messages):
    """Bring the copy at path up to date: append the new message rows (and the session row, when it changed since its
    last line); write the whole file again when an old row changed (/undo marks rows inactive, a compaction marks them
    compacted). Returns path, or None when it cannot write there."""
    try:
        old = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        old = []
    have = [x for x in old if x.startswith('{"type": "message"')]
    last = next((x for x in reversed(old) if x.startswith('{"type": "session"')), None)
    try:
        if old and messages[:len(have)] == have:
            add = ([session] if session != last else []) + messages[len(have):]
            if add:
                with open(path, "a", encoding="utf-8") as f:
                    f.write("".join(x + "\n" for x in add))
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text("".join(x + "\n" for x in [session] + messages), encoding="utf-8")
        tmp.replace(path)
        return path
    except OSError:
        return None


def chat_path(session_ref, folder=None, root=None):
    """The chat file of a session: the copy of its rows in copy_dir()/<session id>.jsonl, brought up to date now (the
    first call writes it). None when Hermes's database has no such session. root: the folder of state.db (a test)."""
    got = _read_session(session_ref, 0, root)
    if not got or not _SAFE_ID.match(got[0]):
        return None
    return _sync(copy_dir() / f"{got[0]}.jsonl", got[1], got[2])


def _rows(lines):
    for line in lines:
        try:
            got = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(got, dict):
            yield got


def _session(lines):
    """The newest session row among the lines (the copy appends one each time the row changes)."""
    got = None
    for r in _rows(lines):
        if r.get("type") == "session":
            got = r
    return got


def _at(ts):
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _parts(content):
    """(text, images) of a message's content: plain text, or a JSON list of parts (text, image_url)."""
    if not isinstance(content, str):
        return "", []
    s = content.strip()
    if s.startswith("[") and s.endswith("]"):
        try:
            parts = json.loads(s)
        except ValueError:
            parts = None
        if isinstance(parts, list) and all(isinstance(p, dict) and "type" in p for p in parts):
            text, images = [], []
            for p in parts:
                if p.get("type") in ("text", "input_text"):
                    text.append(str(p.get("text") or ""))
                elif p.get("type") in ("image_url", "input_image"):
                    u = p.get("image_url")
                    images.append(u.get("url") if isinstance(u, dict) else u)
            return "\n".join(t for t in text if t), [i for i in images if i]
    return content, []


TOOL_DETAIL = {"terminal": "command", "read_file": "path", "write_file": "path", "patch": "path",
               "search_files": "pattern", "web_search": "query", "browser_exec": "url", "delegate_task": "goal",
               "execute_code": "code", "image_generate": "prompt", "skill_view": "name"}


def _detail(name, args):
    """One line about a tool call: its main argument (the command, the path), else its arguments as JSON."""
    if not isinstance(args, dict):
        return str(args or "")[:300]
    key = TOOL_DETAIL.get(name)
    if key and args.get(key) is not None:
        return str(args[key]).splitlines()[0][:300] if str(args[key]).strip() else ""
    if name == "clarify" and isinstance(args.get("questions"), list):
        return "; ".join(str(q.get("question", "")) for q in args["questions"] if isinstance(q, dict))[:300]
    return json.dumps(args, ensure_ascii=False)[:300]


def _result(content):
    """(text, error) of a tool row: Hermes stores a JSON object (output, exit_code, error) for most tools."""
    try:
        got = json.loads(content) if isinstance(content, str) else None
    except ValueError:
        got = None
    if not isinstance(got, dict):
        return str(content or ""), False
    err = got.get("error")
    code = got.get("exit_code")
    error = bool(err) or (isinstance(code, int) and code != 0) or got.get("status") in ("blocked", "error")
    if "output" in got:
        text = str(got.get("output") or "")
        return (text + ("\n" if text and err else "") + (str(err) if err else "")), error
    return str(content), error


def parse_chat(lines):
    """(entries, turn) of a session's lines (chat_lines). A row with display_kind "hidden" or active 0 (rewound by
    /undo) is left out. A summary row of a compaction (_compressed_summary) comes after a divider "compacted"; the rows
    it replaced (compacted 1) stay, so the chat keeps its history. The turn is "working" when the last row is a
    person's prompt, a tool call or a tool result, and "idle" when it is an answer that ended (finish_reason stop)."""
    entries, turn = [], None
    for r in _rows(lines):
        if r.get("type") != "message" or not r.get("active", 1) or r.get("display_kind") == "hidden":
            continue
        at, role, kind = _at(r.get("timestamp")), r.get("role"), r.get("display_kind")
        if r.get("_compressed_summary"):
            entries.append({"at": at, "role": "divider", "kind": "compacted", "text": "Compacted"})
            entries.append({"at": at, "role": "summary", "text": _parts(r.get("content"))[0]})
            continue
        if role == "user":
            text, images = _parts(r.get("content"))
            if kind == "process_complete":
                entries.append({"at": at, "role": "event", "text": text})
                continue
            e = {"at": at, "role": "user", "text": text}
            if images:
                e["images"] = images
            entries.append(e)
            turn = "working"
        elif role == "assistant":
            text = _parts(r.get("content"))[0]
            if kind == "failed_turn":
                entries.append({"at": at, "role": "event", "text": text or "The turn failed"})
                turn = "idle"
                continue
            if text.strip():
                entries.append({"at": at, "role": "assistant", "text": text})
            try:
                calls = json.loads(r.get("tool_calls") or "[]")
            except ValueError:
                calls = []
            for c in calls if isinstance(calls, list) else []:
                fn = c.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (TypeError, ValueError):
                    args = fn.get("arguments")
                entries.append({"at": at, "role": "tool", "id": c.get("id") or c.get("call_id"),
                                "name": fn.get("name"), "detail": _detail(fn.get("name"), args), "text": ""})
            turn = "working" if calls or r.get("finish_reason") in ("tool_calls", "length") else "idle"
        elif role == "tool":
            text, error = _result(r.get("content"))
            entries.append({"at": at, "role": "result", "for": r.get("tool_call_id"), "error": error, "text": text})
            turn = "working"
    return entries, turn


def chat_title(lines):
    s = _session(lines)
    return (s or {}).get("title") or None


def chat_cwd(lines):
    s = _session(lines)
    return (s or {}).get("cwd") or None


def chat_usage(lines):
    """The token counts that Hermes summed for the session (its sessions row)."""
    s = _session(lines) or {}
    return {"input": int(s.get("input_tokens") or 0), "output": int(s.get("output_tokens") or 0),
            "cache_read": int(s.get("cache_read_tokens") or 0), "cache_write": int(s.get("cache_write_tokens") or 0)}


def chat_settings(lines):
    """The provider that the session runs on (its billing provider)."""
    s = _session(lines) or {}
    return {"provider": s["billing_provider"]} if s.get("billing_provider") else {}


def chat_texts(raw):
    """The texts of one line of the chat file where the Artifacts tab looks for files and links: the path of a
    write_file or patch call ("wrote"), and a person's prompt, an answer and the other tool calls' arguments
    ("named"). Tool results are left out."""
    try:
        r = json.loads(raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(r, dict) or r.get("type") != "message" or r.get("role") not in ("user", "assistant"):
        return []
    at, out = _at(r.get("timestamp")), []
    text = _parts(r.get("content"))[0]
    if text.strip():
        out.append((text, at, "named"))
    try:
        calls = json.loads(r.get("tool_calls") or "[]")
    except (TypeError, ValueError):
        calls = []
    for c in calls if isinstance(calls, list) else []:
        fn = (c.get("function") or {}) if isinstance(c, dict) else {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(args, dict):
            continue
        if fn.get("name") in CHAT["file_tools"] and isinstance(args.get("path"), str):
            out.append((args["path"], at, "wrote"))
        else:
            out += [(v, at, "named") for v in args.values() if isinstance(v, str) and v.strip()]
    return out


def read_effort(entries_or_lines):
    """The reasoning effort that the session runs at: model_config.reasoning_config.effort of its sessions row (the
    lines of chat_lines). None when the row does not say."""
    s = _session(entries_or_lines) or {}
    try:
        cfg = json.loads(s.get("model_config") or "{}")
    except (TypeError, ValueError):
        return None
    rc = cfg.get("reasoning_config") if isinstance(cfg, dict) else None
    return rc.get("effort") if isinstance(rc, dict) and rc.get("enabled", True) else None


# ---------------------------------------------------------------------------------------------------------------
# 10. Commands
# ---------------------------------------------------------------------------------------------------------------

# The built-in slash commands of the classic CLI that matter in a work session (hermes_cli/commands.py,
# COMMAND_REGISTRY, v0.22.7; /help lists all of them).
COMMANDS = [
    {"name": "new", "description": "Start a new session (fresh session ID + history)", "hint": "[name]"},
    {"name": "clear", "description": "Clear screen and start a new session", "hint": ""},
    {"name": "history", "description": "Show conversation history", "hint": ""},
    {"name": "retry", "description": "Retry the last message (resend to agent)", "hint": ""},
    {"name": "undo", "description": "Back up N user turns and re-prompt (default 1)", "hint": "[N]"},
    {"name": "title", "description": "Set a title for the current session", "hint": "[name]"},
    {"name": "branch", "description": "Branch the current session", "hint": "[--here] [name]"},
    {"name": "compress", "description": "Compress conversation context", "hint": "[here [N] | focus topic | --preview]"},
    {"name": "rollback", "description": "List or restore filesystem checkpoints", "hint": "[number] [--all]"},
    {"name": "stop", "description": "Kill all running background processes", "hint": ""},
    {"name": "bg", "description": "Run a prompt in a separate background session", "hint": "<prompt>"},
    {"name": "btw", "description": "Ask a side question about the current conversation without interrupting it",
     "hint": "<question>"},
    {"name": "agents", "description": "Show active agents and running tasks", "hint": ""},
    {"name": "queue", "description": "Queue a prompt for the next turn, or list/edit/rm/move/clear queued prompts",
     "hint": "[<prompt>|list|edit N <prompt>|rm N|move A B|clear]"},
    {"name": "steer", "description": "Inject a message after the next tool call without interrupting",
     "hint": "<prompt>"},
    {"name": "goal", "description": "Set a standing goal Hermes works on across turns until achieved",
     "hint": "[text | show | pause | resume | clear | status]"},
    {"name": "plan", "description": "Write a markdown implementation plan to .hermes/plans/ without executing anything",
     "hint": "[task]"},
    {"name": "review", "description": "Spawn an independent subagent to review the work just discussed",
     "hint": "[review instructions]"},
    {"name": "status", "description": "Show session, model, token, and context info", "hint": ""},
    {"name": "context", "description": "Show detailed context window view", "hint": "[all]"},
    {"name": "resume", "description": "Resume a previously-named session", "hint": "[name]"},
    {"name": "sessions", "description": "Browse and resume previous sessions", "hint": ""},
    {"name": "config", "description": "Show current configuration", "hint": ""},
    {"name": "model", "description": "Switch model (session-scoped; --global to persist)",
     "hint": "[model|reset] [--provider name] [--reasoning level]"},
    {"name": "reasoning", "description": "Manage reasoning effort and display", "hint": "[level|show|hide|full]"},
    {"name": "fast", "description": "Fast mode (normal/fast/auto)", "hint": "[normal|fast|auto|status]"},
    {"name": "yolo", "description": "Toggle YOLO mode (skip all dangerous command approvals)", "hint": ""},
    {"name": "approvals", "description": "Show or set the persistent dangerous-command approval mode",
     "hint": "[manual|smart|off]"},
    {"name": "verbose", "description": "Cycle tool progress display: off -> new -> all -> verbose", "hint": ""},
    {"name": "diff", "description": "Show git changes in the working directory", "hint": "[staged|all|session]"},
    {"name": "tools", "description": "Manage tools", "hint": "[list|disable|enable] [name...]"},
    {"name": "skills", "description": "Search, install, inspect, or manage skills", "hint": ""},
    {"name": "init", "description": "Generate or update AGENTS.md project instructions from a repo scan",
     "hint": "[notes]"},
    {"name": "image", "description": "Attach a local image file for your next prompt", "hint": "<path>"},
    {"name": "copy", "description": "Copy the last assistant response to clipboard", "hint": "[number]"},
    {"name": "usage", "description": "Show token usage and rate limits", "hint": ""},
    {"name": "help", "description": "Show available commands", "hint": "[skills|<filter>]"},
    {"name": "version", "description": "Show Hermes Agent version", "hint": ""},
    {"name": "quit", "description": "Exit the CLI", "hint": "[--delete]"},
]

_FRONT = re.compile(r"\A---\s*\n(.*?)\n---", re.S)


def _front(text):
    """name and description of a SKILL.md front matter (simple "key: value" lines)."""
    m = _FRONT.match(text)
    out = {}
    for line in (m.group(1).splitlines() if m else []):
        k, _, v = line.partition(":")
        if k.strip() in ("name", "description") and v.strip():
            out[k.strip()] = v.strip().strip("'\"")
    return out


# How each command works from the page (#5220, #5290): Hermes 0.22.7 in tmux on 2026-11-11 (with no provider signed
# in): /context and /usage print into its scrollback ('No active agent' before a first message), /sessions prints a
# list there; Hermes's chat log keeps none of it. /model and /reasoning set what the Settings rows Model and Effort
# set; /status, /context and /usage show what the Usage panel shows. The views and lists it prints need the terminal;
# the others run at once.
_RUNS = {
    "terminal": {"history", "rollback", "agents", "queue", "sessions", "resume", "config", "approvals", "diff",
                 "tools", "skills", "help", "version"},
}
for _c in COMMANDS:
    _c["run"] = next((k for k, v in _RUNS.items() if _c["name"] in v), "now")
    if _c["name"] in ("model", "reasoning"):
        _c["setting"], _c["run"] = ("model" if _c["name"] == "model" else "effort"), "terminal"
    if _c["name"] in ("status", "context", "usage"):
        _c["page"], _c["run"] = "usage", "terminal"


def context_window(model):
    """The tokens of a model's context window from Hermes's own cache (~/.hermes/context_length_cache.yaml, lines
    '<model>@<base url>: <tokens>', a value also on the next line); #5290. None when it has no entry for the model, or
    two entries that differ (two providers)."""
    from pathlib import Path
    try:
        lines = (Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes") / "context_length_cache.yaml"
                 ).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    import re
    found, pending = set(), False
    for ln in lines:
        m = re.match(r"^\s*(.+?)@(\S+):\s*(\d*)\s*$", ln)
        if m:
            pending = m.group(1) == model and not m.group(3)
            if m.group(1) == model and m.group(3):
                found.add(int(m.group(3)))
        elif pending and ln.strip().isdigit():
            found.add(int(ln.strip()))
            pending = False
    return found.pop() if len(found) == 1 else None


def read_commands(folder):
    """Each skill of $HERMES_HOME/skills is a slash command of the same name (/<skill name>): Hermes loads the skill
    into the turn. The skills sit at skills/<category>/<name>/SKILL.md or skills/<name>/SKILL.md."""
    root, out, seen = hermes_home() / "skills", [], set()
    try:
        files = sorted(list(root.glob("*/SKILL.md")) + list(root.glob("*/*/SKILL.md")))
    except OSError:
        return []
    for f in files:
        try:
            meta = _front(f.read_text(errors="replace", encoding="utf-8")[:4000])
        except OSError:
            continue
        name = meta.get("name") or f.parent.name
        if name in seen or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", name):
            continue
        seen.add(name)
        out.append({"name": name, "description": meta.get("description", "")[:200], "hint": ""})
    return out


# ---------------------------------------------------------------------------------------------------------------
# 11. Setup
# ---------------------------------------------------------------------------------------------------------------

SETUP = {"instructions": "AGENTS.md", "skills_dir": "~/.hermes/skills",
         "voice": "/voice on: Hermes listens and speaks (its own voice mode)",
         "open_link": None}
