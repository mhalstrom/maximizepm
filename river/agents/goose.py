"""The Goose adapter (docs/agent-adapters.md, #5178): Block's goose CLI (command goose, Apache-2.0), read on goose 1.57.0
(Homebrew block-goose-cli) and in its source (crates/goose-cli, crates/goose).

- Launch: `goose session --name <name>` starts an interactive session; `goose run --interactive --name <name> --text
  <prompt>` sends a first prompt and stays interactive. The model is one "provider/model" text (the family is
  "multi"), which goes as `--provider <p> --model <m>`. The approval mode and the thinking effort have no flag: goose
  reads them from the environment (GOOSE_MODE, GOOSE_THINKING_EFFORT), so the command starts with `env NAME=value`.
  The folder is the pane's own folder: goose has no folder flag.
- Identity: the session id (20261111_1: the date, then a number) shows in the header of the pane at each start and
  resume ("\\____)    20261111_1 · <folder>"). The shell tool of goose gives each command AGENT_SESSION_ID.
- Chat log: goose keeps every session in one SQLite database (sessions.db: a sessions row, and a messages row for each
  message), not in a log file. MaximizePM's readers read a log file of JSON lines by byte (river/sessions.py), so
  chat_path() keeps a copy of the session as such a file beside the queue (<queue folder>/goose/<id>.jsonl): it
  appends the message rows that goose added since the last call, and a {"session": <row>} line when the row changed
  (its token counts, its mode). The copy only grows, so a cursor into it stays valid.
- State: goose has plugin hooks (<plugin>/hooks/hooks.json in ~/.agents/plugins, the folder that every Open Plugins
  agent reads), but no hook fires for its approval menu or its other menus, and /compact fires UserPromptSubmit with
  no Stop after it. GOOSE_STATUS_HOOK runs only for a typed prompt and never for a menu, and its program status is a
  terminal escape code that a pane capture does not keep. So the one source of the state is the screen (DETECT), and
  the session id comes from the pane (IDENTITY): the adapter installs no hook.
- Resume: `goose session --resume --session-id <id>` (or `--name <name>`); `goose run --resume ... --text <prompt>`
  when a first prompt follows the resume.
- Commands: the built-in slash commands of its help text and of goose's command list; custom ones are the recipes that
  config.yaml maps (slash_commands) and the skills (SKILL.md) of the folders that goose reads.

The fixtures in tests/fixtures/goose were recorded from goose 1.57.0 in tmux, with a local Ollama model (no account).
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

DESCRIBE = {
    "id": "goose",
    "label": "Goose",
    "exe": "goose",
    "aliases": ["goose-cli", "block-goose-cli"],
    "family": "multi",
    # A free "provider/model" text (ollama/qwen2.5:3b, anthropic/claude-sonnet-4-5); "" lets goose use the provider and
    # the model of its own config (goose configure).
    "models": [],
    "default_model": "",
    "model_ids": {},
    "prefix": "goose_",
}


def _home():
    return Path(os.environ.get("HOME") or Path.home())


def _root():
    """GOOSE_PATH_ROOT: when it is an absolute path, goose keeps its config, data and state below it."""
    r = os.environ.get("GOOSE_PATH_ROOT") or ""
    return Path(r) if r and os.path.isabs(r) else None


def config_dir():
    """goose's config folder: <GOOSE_PATH_ROOT>/config, or $XDG_CONFIG_HOME/goose (~/.config/goose)."""
    r = _root()
    if r:
        return r / "config"
    return Path(os.environ.get("XDG_CONFIG_HOME") or _home() / ".config") / "goose"


def sessions_db():
    """goose's session database: <GOOSE_PATH_ROOT>/data/sessions/sessions.db, or
    $XDG_DATA_HOME/goose/sessions/sessions.db (~/.local/share/goose/sessions/sessions.db)."""
    r = _root()
    base = r / "data" if r else Path(os.environ.get("XDG_DATA_HOME") or _home() / ".local" / "share") / "goose"
    return base / "sessions" / "sessions.db"


def _open(path):
    """The database at path, read only (goose writes it in WAL mode while a session runs), or None."""
    if not path or not Path(path).is_file():
        return None
    try:
        conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def read_models():
    """The "provider/model" texts of goose's model cache (provider_inventory_models in sessions.db), or None when it
    has none: goose fills it when a person lists the models of a provider (goose configure)."""
    conn = _open(sessions_db())
    if conn is None:
        return None
    try:
        rows = conn.execute("SELECT e.provider_id, m.model_id FROM provider_inventory_models m "
                            "JOIN provider_inventory_entries e ON e.inventory_key=m.inventory_key "
                            "ORDER BY e.provider_id, m.ordinal").fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return [f"{r['provider_id']}/{r['model_id']}" for r in rows] or None


# ---------------------------------------------------------------------------------------------------------------
# 2. Settings
# ---------------------------------------------------------------------------------------------------------------

MODES = ["auto", "approve", "smart_approve", "chat"]

SETTINGS = [
    {"key": "mode", "label": "Tool approval", "kind": "choice", "choices": MODES, "default": "",
     "text": "auto runs each tool call; approve asks before each one; smart_approve asks only for risky ones; chat "
             "calls no tool. Empty: goose's own GOOSE_MODE.",
     "flag": [],  # build_command puts GOOSE_MODE=<value> before the command: goose has no flag for it
     "live": "/mode {value}", "read": r"(?:^|\s)GOOSE_MODE=(\w+)"},
    {"key": "max_turns", "label": "Max turns", "kind": "text", "choices": [], "default": "",
     "text": "how many turns goose takes without a person's input before it asks to continue; empty: goose's own",
     "flag": ["--max-turns", "{value}"], "live": None, "read": r"--max-turns[= ](\d+)"},
    {"key": "builtins", "label": "Built-in extensions", "kind": "text", "choices": [], "default": "",
     "text": "more built-in extensions for the session, comma-separated (for example todo,summarize)",
     "flag": ["--with-builtin", "{value}"], "live": "/builtin {value}", "read": r"--with-builtin[= ](\S+)"},
    {"key": "args", "label": "More arguments", "kind": "text", "choices": [], "default": "",
     "text": "more arguments for the goose command, put before the prompt", "flag": [], "live": None, "read": None},
]

# ---------------------------------------------------------------------------------------------------------------
# 3. Effort
# ---------------------------------------------------------------------------------------------------------------

# goose's thinking effort (GOOSE_THINKING_EFFORT; goose itself reads "xhigh" as max). The session row keeps it in its
# model_config_json (request_params.thinking_effort), which each {"session": ...} line of the chat copy holds.
EFFORT = {
    "levels": ["off", "low", "medium", "high", "max"],
    "map": {"xhigh": "max"},
    "flag": [],  # build_command puts GOOSE_THINKING_EFFORT=<value> before the command: goose has no flag for it
    "live": None,
    "read": r'thinking_effort\\?"\s*:\s*\\?"(\w+)',
}

# ---------------------------------------------------------------------------------------------------------------
# 4. Launch
# ---------------------------------------------------------------------------------------------------------------

# fork: goose forks only in `goose session --resume --fork`, which takes no first prompt; autocompact: goose takes a
# share of the context window (GOOSE_AUTO_COMPACT_THRESHOLD), not a token count; system_prompt_file: --system takes
# text, not a file.
LAUNCH = {"session_name": True, "resume": True}

_EFFORT_ENV = "GOOSE_THINKING_EFFORT"


def _env_words(effort, settings):
    out = [f"GOOSE_MODE={settings['mode']}"] if settings.get("mode") else []
    level = EFFORT["map"].get(effort, effort) if effort else None
    return out + ([f"{_EFFORT_ENV}={level}"] if level else [])


def _model_words(model):
    """--provider and --model of a "provider/model" text; a text with no "/" is a model of the configured provider."""
    model = (model or "").strip()
    if not model:
        return []
    provider, _, name = model.partition("/")
    return ["--provider", provider, "--model", name] if name else ["--model", provider]


SESSION_ID = r"\d{8}_\d+"


def build_command(name, folder, model, effort, settings, prompt):
    """[env GOOSE_MODE=<mode> GOOSE_THINKING_EFFORT=<level>] goose session|run --interactive [--name <name> |
    --resume --session-id <id>] [--provider P --model M] [--max-turns N] [--with-builtin B] [args] [--text <prompt>].
    goose session when there is no first prompt, else goose run --interactive. folder: the pane's folder."""
    s = settings or {}
    argv = ["goose", "run", "--interactive"] if prompt else ["goose", "session"]
    ref = str(s.get("resume") or "").strip()
    if ref:
        argv += ["--resume"] + (["--session-id", ref] if re.fullmatch(SESSION_ID, ref) else ["--name", ref])
    elif name:
        argv += ["--name", name]
    argv += _model_words(model)
    if str(s.get("max_turns") or "").strip():
        argv += ["--max-turns", str(s["max_turns"]).strip()]
    if (s.get("builtins") or "").strip():
        argv += ["--with-builtin", s["builtins"].strip()]
    if (s.get("args") or "").strip():
        argv.append(Raw(s["args"].strip()))
    if prompt:
        argv += ["--text", prompt]
    env = _env_words(effort, s)
    return (["env"] + env + argv) if env else argv


def parse_command(words):
    """A hand-written goose command that a profile builds exactly: [env GOOSE_MODE=<mode>
    GOOSE_THINKING_EFFORT={effort}] goose run --interactive [--model {model}] [--max-turns N] [--with-builtin B]
    --text <prompt>. Anything else (a fixed model, a recipe, a session name, another flag): None."""
    w, opts = list(words), {}
    if w and Path(w[0]).name == "env":
        w = w[1:]
        while w and "=" in w[0] and not w[0].startswith("-"):
            k, _, v = w.pop(0).partition("=")
            if k == "GOOSE_MODE" and v in MODES:
                opts["mode"] = v
            elif not (k == _EFFORT_ENV and v == "{effort}"):
                return None
    if len(w) < 3 or Path(w[0]).name.lower() != "goose" or w[1] != "run":
        return None
    prompt, interactive, i = None, False, 2
    while i < len(w):
        t, nxt = w[i], (w[i + 1] if i + 1 < len(w) else None)
        if t in ("--interactive", "-s"):
            interactive, i = True, i + 1
        elif t == "--model" and nxt == "{model}":
            i += 2
        elif t == "--max-turns" and nxt and nxt.isdigit():
            opts["max_turns"], i = nxt, i + 2
        elif t == "--with-builtin" and nxt and not nxt.startswith("-"):
            opts["builtins"], i = nxt, i + 2
        elif t in ("--text", "-t") and nxt and prompt is None:
            prompt, i = nxt, i + 2
        else:
            return None
    return {"settings": opts, "prompt": prompt} if interactive and prompt else None


# ---------------------------------------------------------------------------------------------------------------
# 5. Session identity
# ---------------------------------------------------------------------------------------------------------------

IDENTITY = {
    "source": "pane",
    "env": None,
    "log_glob": None,
    # The header that goose prints at each start and resume: "   \____)    20261111_1 · /Users/me/work".
    "pane": r"\\____\)\s+(" + SESSION_ID + r")\s+·",
    "url": None,
    # goose's shell tool gives each command it runs the session id.
    "inside_env": ["AGENT_SESSION_ID"],
}

# ---------------------------------------------------------------------------------------------------------------
# 6. Chat log: a copy of the session from goose's database, as JSON lines
# ---------------------------------------------------------------------------------------------------------------

CHAT = {"compressed": False, "file_tools": ["write", "edit", "developer__text_editor"], "ref": "session",
        "cache_write": False}

_SESSION_COLS = ("id", "name", "working_dir", "created_at", "updated_at", "provider_name", "model_config_json",
                 "goose_mode", "accumulated_input_tokens", "accumulated_output_tokens", "accumulated_total_tokens",
                 "accumulated_cache_read_tokens", "accumulated_cache_write_tokens", "accumulated_cost",
                 "parent_session_id")
_MESSAGE_COLS = ("message_id", "role", "content_json", "created_timestamp", "metadata_json")

COMPACTED = "Your context was compacted"  # the start of the hidden assistant text that follows a compaction summary
COMPACTION_DONE = "Compaction complete"


def copy_dir():
    """Where the chat copies are: goose/ beside the queue."""
    from ..core import river_dir
    return Path(river_dir()) / "goose"


def _session_row(conn, session_ref):
    """The sessions row of an id (20261111_1) or a name; for a name used twice, the newest session."""
    cols = ", ".join(_SESSION_COLS)
    r = conn.execute(f"SELECT {cols} FROM sessions WHERE id=?", (session_ref,)).fetchone()
    return r or conn.execute(f"SELECT {cols} FROM sessions WHERE name=? ORDER BY created_at DESC, id DESC LIMIT 1",
                             (session_ref,)).fetchone()


def _loads(text, default):
    try:
        v = json.loads(text) if isinstance(text, str) else text
    except ValueError:
        return default
    return default if v is None else v


def _texts(content):
    return [c.get("text") or "" for c in content if isinstance(c, dict) and c.get("type") == "text"]


def _hidden_text(row):
    """The text of a message row that the person does not see (metadata userVisible false), else None."""
    if _loads(row["metadata_json"], {}).get("userVisible", True):
        return None
    content = _loads(row["content_json"], [])
    return "\n".join(_texts(content if isinstance(content, list) else [])).strip()


class _Lock:
    """An exclusive lock on a file while a copy is brought up to date (two page requests at once); none on Windows."""

    def __init__(self, path):
        self.path, self.f = path, None

    def __enter__(self):
        try:
            import fcntl
        except ImportError:
            return self
        self.f = open(self.path, "a")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self.f:
            self.f.close()


def copy_session(db, session_ref, out_dir):
    """Bring the copy of one session up to date from goose's database db, and return its path (out_dir/<id>.jsonl),
    or None when db has no such session. Appended: a {"session": <row>} line when the row changed since the last
    call, then each message row added since (its columns, and "row": goose's row id). A compaction's marker row (the
    hidden assistant text "Your context was compacted ...") gets "summary": the hidden user text before it, so each
    line reads alone. A copy that is shorter than its state says (removed by hand) starts again."""
    conn = _open(db)
    if conn is None or not session_ref:
        return None
    try:
        s = _session_row(conn, session_ref)
        if not s:
            return None
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path, state_file = out_dir / f"{s['id']}.jsonl", out_dir / f"{s['id']}.state.json"
        with _Lock(out_dir / f"{s['id']}.lock"):
            state = _loads(state_file.read_text() if state_file.exists() else "{}", {})
            size = path.stat().st_size if path.exists() else 0
            if size < state.get("size", 0) or not path.exists():
                state, size = {}, 0
            session = dict(s)
            lines = [] if state.get("session") == session else [{"session": session}]
            rows = conn.execute(f"SELECT id, {', '.join(_MESSAGE_COLS)} FROM messages WHERE session_id=? AND id>? "
                                "ORDER BY id", (s["id"], state.get("last", 0))).fetchall()
            for r in rows:
                d = {"row": r["id"], **{k: r[k] for k in _MESSAGE_COLS}}
                if r["role"] == "assistant" and (_hidden_text(r) or "").startswith(COMPACTED):
                    before = conn.execute("SELECT content_json, metadata_json FROM messages WHERE session_id=? AND "
                                          "id<? AND role='user' ORDER BY id DESC LIMIT 1", (s["id"], r["id"])).fetchone()
                    d["summary"] = (before and _hidden_text(before)) or ""
                lines.append(d)
            if lines:
                with open(path, "a", encoding="utf-8") as f:
                    f.write("".join(json.dumps(x) + "\n" for x in lines))
                state = {"session": session, "last": rows[-1]["id"] if rows else state.get("last", 0),
                         "size": path.stat().st_size}
                state_file.write_text(json.dumps(state))
        return path
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def chat_path(session_ref, folder=None, root=None):
    """The copy of a session as JSON lines (copy_session), brought up to date from goose's database; root, when given,
    is the folder of sessions.db (the tests give one)."""
    db = Path(root) / "sessions.db" if root else sessions_db()
    return copy_session(db, session_ref, copy_dir())


def _at(ts):
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _detail(args):
    """A tool call's arguments in one line: the command, the path, or the JSON."""
    if not isinstance(args, dict):
        return ""
    for k in ("command", "path", "file_path", "query", "name"):
        if isinstance(args.get(k), str):
            return args[k]
    return json.dumps(args, sort_keys=True)[:300]


def _result_text(result):
    value = (result or {}).get("value") or {}
    if (result or {}).get("status") != "success":
        return str((result or {}).get("error") or value or "")
    parts = value.get("content") if isinstance(value, dict) else None
    return "\n".join(_texts(parts or []))


def parse_chat_line(d):
    """(entries, turn) of one line of the copy (a dict). Shown: what the person sees in the pane (metadata userVisible),
    and a compaction as a divider with its summary. turn: "idle" after an answer of the assistant, an error or a
    compaction; "working" after a prompt, a tool call or a tool result; None when the line says neither."""
    role, content = d.get("role"), _loads(d.get("content_json"), [])
    if not role or not isinstance(content, list):
        return [], None  # a {"session": ...} line, or no message
    meta, at = _loads(d.get("metadata_json"), {}), _at(d.get("created_timestamp"))
    if not meta.get("userVisible", True):
        if role == "assistant" and "\n".join(_texts(content)).strip().startswith(COMPACTED):
            out = [{"at": at, "role": "divider", "kind": "compacted", "text": "Compacted"}]
            if d.get("summary"):
                out.append({"at": at, "role": "summary", "text": d["summary"]})
            return out, "idle"
        return [], None  # the session start, the turn context, the summary itself, an approval's answer
    if role == "assistant" and meta.get("agentVisible") is False and _texts(content) == [COMPACTION_DONE]:
        return [], "idle"  # the divider says it already
    out, turn, images = [], None, []
    for c in content:
        if not isinstance(c, dict):
            continue
        kind = c.get("type")
        if kind == "text" and c.get("text"):
            out.append({"at": at, "role": "user" if role == "user" else "assistant", "text": c["text"]})
            turn = "working" if role == "user" else "idle"
        elif kind == "image" and c.get("data"):
            images.append(f"data:{c.get('mimeType') or 'image/png'};base64,{c['data']}")
        elif kind == "toolRequest":
            call = ((c.get("toolCall") or {}).get("value")) or {}
            out.append({"at": at, "role": "tool", "id": c.get("id"), "name": call.get("name") or "",
                        "detail": _detail(call.get("arguments")), "text": ""})
            turn = "working"
        elif kind == "toolResponse":
            res = c.get("toolResult") or {}
            err = res.get("status") != "success" or bool((res.get("value") or {}).get("isError"))
            out.append({"at": at, "role": "result", "for": c.get("id"), "error": err, "text": _result_text(res)})
            turn = "working"
        elif kind == "actionRequired" and (c.get("data") or {}).get("actionType") == "toolConfirmation":
            out.append({"at": at, "role": "event",
                        "text": f"Asks to approve {(c.get('data') or {}).get('toolName') or 'a tool call'}"})
        elif kind == "systemNotification" and c.get("notificationType") == "inlineMessage" and c.get("msg"):
            out.append({"at": at, "role": "event", "text": c["msg"]})
        elif kind == "error":
            out.append({"at": at, "role": "event", "text": str(c.get("error") or c.get("message") or "error")})
            turn = "idle"
    if images:
        if out and out[0]["role"] == "user":
            out[0]["images"] = images
        else:
            out.insert(0, {"at": at, "role": "user", "text": "", "images": images})
            turn = turn or "working"
    return out, turn


def _decoded(lines):
    for line in lines:
        d = _loads(line, None)
        if isinstance(d, dict):
            yield d


def parse_chat(lines):
    """(entries, turn) of lines of the copy: parse_chat_line of each one."""
    out, turn = [], None
    for d in _decoded(lines):
        got, st = parse_chat_line(d)
        out += got
        turn = st or turn
    return out, turn


def _session(lines):
    """The last {"session": <row>} of the lines, or None."""
    rows = [d["session"] for d in _decoded(lines) if isinstance(d.get("session"), dict)]
    return rows[-1] if rows else None


def chat_title(lines):
    return (_session(lines) or {}).get("name") or None


def chat_cwd(lines):
    return (_session(lines) or {}).get("working_dir") or None


def chat_usage(lines):
    """The session's token counts: its accumulated_* columns (every model call, compactions too); without a session
    line, the sum of the usage of each message."""
    s = _session(lines)
    if s and s.get("accumulated_input_tokens") is not None:
        return {"input": s.get("accumulated_input_tokens") or 0, "output": s.get("accumulated_output_tokens") or 0,
                "cache_read": s.get("accumulated_cache_read_tokens") or 0,
                "cache_write": s.get("accumulated_cache_write_tokens") or 0}
    out = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    for d in _decoded(lines):
        u = _loads(d.get("metadata_json"), {}).get("usage") or {}
        out["input"] += u.get("inputTokens") or 0
        out["output"] += u.get("outputTokens") or 0
        out["cache_read"] += u.get("cacheReadTokens") or 0
        out["cache_write"] += u.get("cacheWriteTokens") or 0
    return out


def chat_settings(lines):
    """The settings as the session runs now: its approval mode (goose_mode, which /mode changes)."""
    s = _session(lines)
    return {"mode": s["goose_mode"]} if s and s.get("goose_mode") else {}


def chat_requests(session_ref, root=None):
    """(requests, fork) of a session from goose's usage ledger (one row for each model call, compactions too), or None
    when its database or the session is not on this computer. goose has no request id: the key is the ledger row."""
    conn = _open(Path(root) / "sessions.db" if root else sessions_db())
    if conn is None or not session_ref:
        return None
    try:
        s = _session_row(conn, session_ref)
        if not s:
            return None
        rows = conn.execute("SELECT id, created_timestamp, input_tokens, output_tokens, cache_read_tokens, "
                            "cache_write_tokens FROM usage_ledger WHERE session_id=? ORDER BY id", (s["id"],)).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return {f"goose-{r['id']}": (_at(r["created_timestamp"]), {
        "input": r["input_tokens"] or 0, "output": r["output_tokens"] or 0, "cache_read": r["cache_read_tokens"] or 0,
        "cache_write": r["cache_write_tokens"] or 0, "write_1h": 0, "write_5m": 0}) for r in rows}, False


def chat_texts(raw):
    """The texts of one line of the copy (bytes) for the Artifacts tab: the text of each turn the person sees, the path
    of each file a CHAT['file_tools'] call wrote, and the input of each other tool call. Tool results are left out."""
    d = _loads(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw, None)
    if not isinstance(d, dict) or not d.get("role") or not _loads(d.get("metadata_json"), {}).get("userVisible", True):
        return []
    at, out = _at(d.get("created_timestamp")), []
    for c in _loads(d.get("content_json"), []) or []:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "text" and c.get("text"):
            out.append((c["text"], at, "named"))
        elif c.get("type") == "toolRequest":
            call = ((c.get("toolCall") or {}).get("value")) or {}
            args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
            path = args.get("path") or args.get("file_path")
            if call.get("name") in CHAT["file_tools"] and isinstance(path, str):
                out.append((path, at, "wrote"))
            elif args:
                out.append((json.dumps(args), at, "named"))
    return out


# ---------------------------------------------------------------------------------------------------------------
# 7. State
# ---------------------------------------------------------------------------------------------------------------

STATE = {"state": "detect", "session_id": "identity"}

# The pane watcher (server.watch_prompts) reads the last lines of the pane and tries each pattern on each line.
DETECT = {
    "process": "goose",
    # The input line while it waits: "> " with a hint, which shows only while the line is empty. The lines of earlier
    # prompts ("> Use the shell tool ...") keep what was typed, so they do not match. While the person types, no line
    # matches, and the state stays idle.
    "idle": r"^> (?:Enter to send · Ctrl\+\w newline|Interrupted, what should goose work on instead\?"
            r"|Press Ctrl\+C again to exit)",
    # The spinner line of a running turn: "◐  Accelerating abstract algebras...  (Ctrl+C to interrupt)". goose hides
    # the spinner while a tool runs and for the first prompt of goose run --text; those screens match no pattern, and
    # the state stays as it was.
    "working": r"\(Ctrl\+C to interrupt\)",
    # An open cliclack menu has "◆"; an answered one shows "◇". The tool approval (approve and smart_approve modes),
    # its form with a prompt of the tool, an MCP server's question, /compact, a session resumed in another folder,
    # and any other open menu.
    "blocked": [
        r"^◆\s+Goose would like to call the above tool, do you allow\?",
        r"^◆\s+Do you allow this tool call\?",
        r"^◆\s+Approve(?: this action)?\?",
        r"^◆\s+Are you sure you want to compact this conversation\?",
        r"^◆\s+.*Do you want to switch back to the original working directory\?",
        r"^◆\s+\S",
    ],
    "tell_instead": None,  # the approval menu has Allow, Always Allow, Deny and Cancel: no option to type an answer
    "image_paste": None,  # the goose CLI takes no pasted image; a path in the prompt goes to its read_image tool
    # cliclack marks the selected option with ● and each other one with ○, and draws │ before each line of a menu and
    # └ under it.
    "menu": "●",
    "other": "○",
}

# ---------------------------------------------------------------------------------------------------------------
# 8. Input
# ---------------------------------------------------------------------------------------------------------------

INPUT = {"native": None}

# ---------------------------------------------------------------------------------------------------------------
# 9. Resume
# ---------------------------------------------------------------------------------------------------------------


def resume_command(session_ref, model, effort, settings, name=None):
    """goose session --resume --session-id <id> (or --name <name>), with the profile's settings, and goose run
    --resume ... --text <prompt> when settings['prompt'] is a first prompt after the resume."""
    if not (session_ref or "").strip():
        return None
    s = dict(settings or {})
    return build_command(name, None, model, effort, {**s, "resume": session_ref.strip()}, s.get("prompt"))


# ---------------------------------------------------------------------------------------------------------------
# 10. Commands
# ---------------------------------------------------------------------------------------------------------------

COMMANDS = [
    {"name": "help", "description": "Show the commands", "hint": ""},
    {"name": "exit", "description": "End the session", "hint": ""},
    {"name": "mode", "description": "Set the tool approval mode", "hint": "auto|approve|smart_approve|chat"},
    {"name": "model", "description": "Show the model, or switch models (the same provider)", "hint": "[name]"},
    {"name": "compact", "description": "Compact the conversation history", "hint": ""},
    {"name": "clear", "description": "Clear the conversation history", "hint": ""},
    {"name": "new", "description": "Start a fresh session in this process", "hint": ""},
    {"name": "status", "description": "Show the model, provider, mode and token usage", "hint": ""},
    {"name": "skills", "description": "List skills, or enable skills by name", "hint": "[<name>...]"},
    {"name": "prompts", "description": "List the prompts of the extensions", "hint": "[--extension <name>]"},
    {"name": "prompt", "description": "Run a prompt, or show its info", "hint": "<name> [--info] [key=value...]"},
    {"name": "extension", "description": "Add a stdio extension", "hint": "<command>"},
    {"name": "builtin", "description": "Add built-in extensions", "hint": "<names>"},
    {"name": "goal", "description": "Set a goal the agent must meet before it finishes", "hint": "<goal>|off"},
    {"name": "grind", "description": "Set a goal the agent pursues until max turns", "hint": "<goal>|off"},
    {"name": "doctor", "description": "Check that the goose setup works", "hint": ""},
    {"name": "edit", "description": "Write the message in your editor", "hint": "[text]"},
    {"name": "r", "description": "Show or hide the full tool output", "hint": ""},
    {"name": "t", "description": "Change the theme", "hint": "[light|dark|ansi]"},
]


def _skill_dirs(folder):
    """The folders where goose finds skills (crates/goose/src/skills): the session folder's, then the global ones."""
    home, out = _home(), []
    if folder:
        f = Path(folder)
        out += [f / ".agents" / "skills", f / ".goose" / "skills", f / ".claude" / "skills"]
    out += [home / ".agents" / "skills", config_dir() / "skills", home / ".claude" / "skills",
            home / ".config" / "agents" / "skills"]
    plugins = (_root() / ".agents" / "plugins") if _root() else home / ".agents" / "plugins"
    try:
        out += sorted(p / "skills" for p in plugins.iterdir() if p.is_dir())
    except OSError:
        pass
    return out


def _front(text):
    """name and description of a SKILL.md front matter (--- ... ---), with no YAML parser."""
    m = re.match(r"---\s*\n(.*?)\n---", text, re.S)
    got = {}
    for line in (m.group(1).splitlines() if m else ()):
        k, sep, v = line.partition(":")
        if sep and k.strip() in ("name", "description"):
            got[k.strip()] = v.strip().strip("'\"")
    return got


def _recipes():
    """The recipe slash commands of config.yaml: slash_commands is a list of {command, recipe_path}. A small reader of
    that one key, as goose writes it, with no YAML parser."""
    try:
        text = (config_dir() / "config.yaml").read_text()
    except OSError:
        return []
    out, inside, cur = [], False, None
    for line in text.splitlines():
        if re.match(r"slash_commands:\s*$", line):
            inside = True
            continue
        if inside and line and not line[0].isspace() and not line.startswith("-"):
            break
        m = inside and re.match(r"\s*(-?)\s*(command|recipe_path):\s*(.+?)\s*$", line)
        if m:
            if m.group(1) or cur is None:
                cur = {}
                out.append(cur)
            cur[m.group(2)] = m.group(3).strip("'\"")
    return [c for c in out if c.get("command")]


def read_commands(folder):
    """The custom slash commands of a session in folder: the recipes of config.yaml, then the skills (each skill is a
    slash command by its name). A name of a built-in command is left out, as goose does."""
    taken = {c["name"] for c in COMMANDS}
    out = []
    for r in _recipes():
        name = r["command"].lstrip("/").lower()
        if name not in taken:
            taken.add(name)
            out.append({"name": name, "description": f"Run the recipe {r.get('recipe_path', '')}".strip(),
                        "hint": ""})
    for d in _skill_dirs(folder):
        try:
            files = sorted(d.glob("*/SKILL.md"))
        except OSError:
            continue
        for f in files:
            try:
                meta = _front(f.read_text(errors="replace"))
            except OSError:
                continue
            name = (meta.get("name") or f.parent.name).strip().lower().replace(" ", "-")
            if name and name not in taken:
                taken.add(name)
                out.append({"name": name, "description": meta.get("description", ""), "hint": ""})
    return out


# ---------------------------------------------------------------------------------------------------------------
# 11. Setup
# ---------------------------------------------------------------------------------------------------------------

SETUP = {
    "instructions": "AGENTS.md",  # goose also reads .goosehints
    "skills_dir": "~/.agents/skills",
    "voice": None,
    "open_link": "Cmd-click the link in the terminal.",
}
