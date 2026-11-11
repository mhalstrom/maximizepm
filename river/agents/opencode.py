"""The OpenCode adapter (docs/agent-adapters.md, #5171): opencode, the V2 CLI of OpenCode (Homebrew opencode-v2).

What this module rests on, read from OpenCode 2.2.7 on macOS (its --help, its binary, its database, and runs in tmux):

- The full TUI (`opencode [dir]`) talks to a server. Without --standalone it is OpenCode's shared background service
  (`opencode serve --service`), which reads its config once, when the first TUI starts it; with --standalone each TUI
  starts a private server with its own environment. The TUI has no --model flag: the model goes in by the config
  (OPENCODE_CONFIG_CONTENT, {"model": "provider/model"}), so it applies only with --standalone (the standalone
  setting, on by default). The model is a free "provider/model" text (opencode models lists them), so the family is
  "multi". The default is a free model of OpenCode's own provider (mark 2026-11-11: use the free models, no key).
- Effort: OpenCode calls it the model's variant (low, medium, high, ...; each model has its own). No flag and no config
  key sets it at the start (the TUI keeps the last variant of each model in ~/.local/state/opencode/model.json), and
  /effort opens a picker with no argument. So the adapter has no effort part, and the page hides effort.
- --session <id> continues a session, or creates it with that id at the first prompt. --prompt sends the first prompt.
- State and the session id come from one TUI plugin (install_hooks): a folder with tui.js, loaded through
  OPENCODE_CLI_CONFIG_CONTENT ({"plugins": [<folder>]}), the environment form of ~/.config/opencode/cli.json. It is
  herdr's V2 opencode plugin (Apache-2.0, src/integration/assets/opencode/herdr-tui-session.js), changed to call
  maxpm report: session.execution.started is working; succeeded and interrupted are idle; failed is blocked with the
  error (the turn stopped and waits for a person, for example 'Free usage limit reached'); permission.asked and
  form.created are blocked until their reply. It reports the session that the pane shows (the TUI has tabs), with the
  resume command, and maxpm release when the TUI exits.
- Sessions live in OpenCode's SQLite database (~/.local/share/opencode/opencode.db, $XDG_DATA_HOME, or $OPENCODE_DB):
  table session_v2 (id, directory, title, fork_session_id, tokens), and session_message (one row per user message,
  assistant step, idle mark, compaction or synthetic note, ordered by seq). There is no log file, so chat_path copies a
  session's rows into a JSONL file beside the queue (opencode/chats/<id>.jsonl, again when the database changes), and
  the shared chat readers read that file as any other log.
- Its shell tool sets OPENCODE=1, AI_AGENT=opencode and OPENCODE_SESSION_ID for each command it runs.
- Custom slash commands: the .md files of command/ and commands/ (any depth: sub/x.md is /sub/x) in the global config
  folder (~/.config/opencode, $OPENCODE_CONFIG_DIR) and in .opencode of the folder. Skills are no slash commands
  (/skills lists them).
- A permission screen has Allow once, Always allow and Reject. Reject ends the turn, and the person types the answer
  at the prompt: that is the "tell it instead" option.
"""

import json
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path

from . import Raw

DESCRIBE = {
    "id": "opencode",
    "label": "OpenCode",
    "exe": "opencode",
    "aliases": ["open-code"],
    "family": "multi",
    # OpenCode's own free models (opencode models, 2026-11-11), no sign-in and no key. Big Pickle did the read, write
    # and edit tool calls of the recorded session well. read_models() reads the list of the installed CLI.
    "models": ["opencode/big-pickle", "opencode/deepseek-v4.1-flash-free", "opencode/glm-5.3-flash-free",
               "opencode/minimax-m3.1-flash-free"],
    "default_model": "opencode/big-pickle",
    "model_ids": {},
    "prefix": "opencode_",
}

SETTINGS = [
    {"key": "standalone", "label": "Own server", "kind": "toggle", "choices": [], "default": "on",
     "text": "a private OpenCode server for this session (--standalone); the model applies only with it, as the "
             "shared background service keeps the config it started with",
     "flag": ["--standalone"], "live": None, "read": r"(--standalone)\b", "session": True},
    {"key": "auto", "label": "Approve permissions", "kind": "toggle", "choices": [], "default": "off",
     "text": "approve each permission that the config does not deny (--auto)",
     "flag": ["--auto"], "live": None, "read": r"(--auto)\b", "session": True},
    {"key": "args", "label": "More arguments", "kind": "text", "choices": [], "default": "",
     "text": "more arguments, put before the folder", "flag": [], "live": None, "read": None},
    {"key": "prompt", "label": "First prompt", "kind": "text", "choices": [],
     "default": "run maxpm go in this folder and follow the briefing",
     "text": "the first prompt (--prompt; OpenCode reads AGENTS.md of the folder)", "flag": [], "live": None,
     "read": None},
]

EFFORT = None  # see the module's text: OpenCode's variant cannot be set at the start, and /effort takes no argument

LAUNCH = {
    "resume": True,  # --session <id>
    # No session_name (the TUI names a session from its first prompt; /rename), no system_prompt_file, no fork (only
    # opencode run and mini take --fork), no hooks_file (the plugin goes in by install_hooks), no extra_dirs, no
    # autocompact, no background.
}

# A session id of OpenCode: ses_ and base62 (ses_e366d1e13ffe0SgSDgPP6MNKXx). --session <id> takes any such text.
SESSION_ID = r"ses_[A-Za-z0-9]{8,64}"

IDENTITY = {
    "source": "hook",  # the plugin reports the session that the pane shows, with --session-id
    "env": None,
    "log_glob": None,
    "pane": None,
    "url": None,
    "inside_env": ["OPENCODE_SESSION_ID"],  # its shell tool sets it for each command
    "id_pattern": SESSION_ID,
}

# ref: the Sessions tab finds the chat by the agent's newest session id (the plugin reports it); cache_write False:
# OpenCode's providers bill a call after the cache ran out at the input price (its rows count no cache writes).
CHAT = {"compressed": False, "file_tools": ["write", "edit"], "ref": "session", "cache_write": False}

STATE = {"state": "hook", "session_id": "hook"}

# The screens of the TUI, recorded in tmux (tests/fixtures/opencode). The plugin is the one source of the state: these
# rules are no second source, but the data of the pane (the process, the "tell it instead" option), and the tests
# check them against the recorded screens.
DETECT = {
    "process": "opencode",
    # The footer under the prompt box starts with the folder when no turn runs.
    "idle": r"(?m)^\s*[~/]\S*\s{2,}.*ctrl\+p commands",
    # While a turn runs, the footer starts with a moving bar and 'esc interrupt'.
    "working": r"(?m)^\s*[⬝■]{4,}\s+esc (?:again to )?interrupt",
    "blocked": [r"△ Permission required", r"⇆ select\s+enter confirm"],
    # Reject opens a text box ("Tell OpenCode what to do differently"): the person's answer goes there.
    "tell_instead": "Reject",
    "image_paste": None,
    # The permission menu is one line: '   Allow once   Always allow   Reject      ctrl+f fullscreen  ⇆ select  enter
    # confirm' (with mono output 'left/right select'). Left, Right and Tab move the selection, which wraps; each new
    # request starts at 'Allow once' (OpenCode 2.2.7, #5281). Always allow asks again: Confirm   Cancel.
    "row": r"^\s*(\S.*?)\s{3,}(?:ctrl\+f fullscreen\s+)?(?:⇆|left/right) select\s+enter confirm",
}

INPUT = {"native": None}

# The built-in slash commands of the TUI (its "/" list, OpenCode 2.2.7).
COMMANDS = [
    {"name": "agents", "description": "Switch agent", "hint": ""},
    {"name": "btw", "description": "One-shot answer from the session's context without adding to the conversation",
     "hint": "<question>"},
    {"name": "cd", "description": "Change working directory", "hint": ""},
    {"name": "clear", "description": "Clear session", "hint": ""},
    {"name": "compact", "description": "Compact session", "hint": ""},
    {"name": "connect", "description": "Connect an integration", "hint": ""},
    {"name": "continue", "description": "Switch session", "hint": ""},
    {"name": "copy", "description": "Copy last assistant message", "hint": ""},
    {"name": "debug", "description": "View debug info", "hint": ""},
    {"name": "diff", "description": "Open diff viewer", "hint": ""},
    {"name": "editor", "description": "Open editor", "hint": ""},
    {"name": "effort", "description": "Switch model variant", "hint": ""},
    {"name": "exit", "description": "Exit the app", "hint": ""},
    {"name": "export", "description": "Export session transcript", "hint": ""},
    {"name": "fork", "description": "Fork session", "hint": ""},
    {"name": "goal", "description": "keep working until an objective is met", "hint": "[pause|resume|clear]"},
    {"name": "help", "description": "Help", "hint": ""},
    {"name": "highlights", "description": "Show release highlights", "hint": ""},
    {"name": "init", "description": "guided AGENTS.md setup", "hint": ""},
    {"name": "mcps", "description": "MCP servers", "hint": ""},
    {"name": "models", "description": "Switch model", "hint": ""},
    {"name": "new", "description": "New session", "hint": ""},
    {"name": "open", "description": "Open session or project", "hint": ""},
    {"name": "pair", "description": "Pair device", "hint": ""},
    {"name": "plugins", "description": "Plugins", "hint": ""},
    {"name": "project", "description": "Open session or project", "hint": ""},
    {"name": "projects", "description": "Open session or project", "hint": ""},
    {"name": "q", "description": "Exit the app", "hint": ""},
    {"name": "quit", "description": "Exit the app", "hint": ""},
    {"name": "reload", "description": "Reload configuration", "hint": ""},
    {"name": "rename", "description": "Rename session", "hint": ""},
    {"name": "restart", "description": "Restart service", "hint": ""},
    {"name": "resume", "description": "Switch session", "hint": ""},
    {"name": "review", "description": "review changes, defaults to uncommitted", "hint": "[commit|branch|pr]"},
    {"name": "sessions", "description": "Switch session", "hint": ""},
    {"name": "settings", "description": "Open settings", "hint": ""},
    {"name": "skills", "description": "Skills", "hint": ""},
    {"name": "stats", "description": "Usage statistics", "hint": ""},
    {"name": "status", "description": "View status", "hint": ""},
    {"name": "terminal", "description": "New terminal", "hint": ""},
    {"name": "themes", "description": "Switch theme", "hint": ""},
    {"name": "thinking", "description": "Switch model variant", "hint": ""},
    {"name": "timeline", "description": "Jump to message", "hint": ""},
    {"name": "undo", "description": "Undo previous message", "hint": ""},
    {"name": "update", "description": "Update OpenCode", "hint": ""},
    {"name": "usage", "description": "View usage", "hint": ""},
]

SETUP = {
    "instructions": "AGENTS.md",  # /init writes it; OpenCode reads AGENTS.md (and CLAUDE.md) of the folder
    "skills_dir": "~/.config/opencode/skills",
    "voice": None,
    "open_link": None,
}


# ---------------------------------------------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------------------------------------------

def config_dir(env=None):
    """OpenCode's global config folder: $OPENCODE_CONFIG_DIR, else $XDG_CONFIG_HOME/opencode, else ~/.config/opencode."""
    env = os.environ if env is None else env
    if env.get("OPENCODE_CONFIG_DIR"):
        return Path(env["OPENCODE_CONFIG_DIR"]).expanduser()
    return Path(env.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "opencode"


def db_path(env=None):
    """OpenCode's database: $OPENCODE_DB, else $XDG_DATA_HOME/opencode/opencode.db, else ~/.local/share/opencode."""
    env = os.environ if env is None else env
    if env.get("OPENCODE_DB"):
        return Path(env["OPENCODE_DB"]).expanduser()
    return Path(env.get("XDG_DATA_HOME") or "~/.local/share").expanduser() / "opencode" / "opencode.db"


def _own_dir():
    """The folder of this adapter's files beside the queue (opencode/ in core.river_dir())."""
    from ..core import river_dir
    return Path(river_dir()) / "opencode"


# ---------------------------------------------------------------------------------------------------------------
# 1. Describe: the models of the installed CLI
# ---------------------------------------------------------------------------------------------------------------

MODELS_RUNNER = None  # a test hook: a function () -> the text of `opencode models`
_models = {"at": 0.0, "list": None}


def read_models():
    """The "provider/model" ids that `opencode models --standalone` lists (the providers a person connected, and
    OpenCode's free ones), read at most once in ten minutes; None when the CLI does not answer."""
    if _models["list"] is not None and time.time() - _models["at"] < 600:
        return list(_models["list"])
    try:
        if MODELS_RUNNER:
            out = MODELS_RUNNER()
        else:
            out = subprocess.run(["opencode", "models", "--standalone"], capture_output=True, text=True,
                                 timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    got = [ln.strip() for ln in (out or "").splitlines() if re.fullmatch(r"[\w.-]+/\S+", ln.strip())]
    if not got:
        return None
    _models.update(at=time.time(), list=got)
    return list(got)


# ---------------------------------------------------------------------------------------------------------------
# 4. Launch
# ---------------------------------------------------------------------------------------------------------------

def _on(v):
    return v is True or str(v).strip().lower() in ("on", "true", "1", "yes")


CONFIG_VAR = "OPENCODE_CONFIG_CONTENT"
CLI_CONFIG_VAR = "OPENCODE_CLI_CONFIG_CONTENT"


def build_command(name, folder, model, effort, settings, prompt):
    """[env OPENCODE_CONFIG_CONTENT={"model": <model>}] opencode [--standalone] [--auto] [--session <id>] [args]
    [--prompt <prompt>] [folder]. name: the plugin reads it from MAXPM_AGENT; effort: OpenCode has none at the start.
    The model goes in by the config only with --standalone (the shared service keeps its own config)."""
    s = settings or {}
    standalone = _on(s.get("standalone", "on"))
    argv = []
    if model and standalone:
        argv += ["env", f"{CONFIG_VAR}={json.dumps({'model': model}, separators=(',', ':'))}"]
    argv.append("opencode")
    if standalone:
        argv.append("--standalone")
    if _on(s.get("auto", "off")):
        argv.append("--auto")
    if s.get("resume"):
        argv += ["--session", str(s["resume"])]
    if (s.get("args") or "").strip():
        argv.append(Raw(s["args"].strip()))
    if prompt:
        argv += ["--prompt", prompt]
    if folder:
        argv.append(str(folder))
    return argv


def parse_command(words):
    """A hand-written opencode command that a profile builds exactly: --standalone, --auto, and --prompt <text>, with
    an optional env OPENCODE_CONFIG_CONTENT='{"model":"{model}"}' first. Anything else (a session, a server, another
    flag or subcommand, a folder): None."""
    w = list(words or ())
    if w[:1] == ["env"] and len(w) > 1 and w[1].startswith(CONFIG_VAR + "="):
        try:
            cfg = json.loads(w[1].split("=", 1)[1])
        except ValueError:
            return None
        if cfg != {"model": "{model}"}:
            return None
        w = w[2:]
    if not w or Path(w[0]).name.lower() != "opencode":
        return None
    opts, prompt, i = {"standalone": "off"}, None, 1
    while i < len(w):
        t, nxt = w[i], (w[i + 1] if i + 1 < len(w) else None)
        if t == "--standalone":
            opts["standalone"] = "on"
            i += 1
        elif t == "--auto":
            opts["auto"] = "on"
            i += 1
        elif t == "--prompt" and nxt is not None and prompt is None and nxt.strip():
            prompt = nxt
            i += 2
        else:
            return None
    return {"settings": opts, "prompt": prompt} if prompt is not None else None


# ---------------------------------------------------------------------------------------------------------------
# 7. State: the TUI plugin
# ---------------------------------------------------------------------------------------------------------------

PLUGIN_DIR = "maxpm-state"

# The plugin that reports OpenCode's state to MaximizePM (docs/agent-adapters.md, step 7). __REPORT__ is the maxpm report
# command as a JSON list, __RESUME__ the words of the resume command before the session id. It is herdr's V2 plugin
# (Apache-2.0, herdrdev/herdr src/integration/assets/opencode/herdr-tui-session.js, setup()), changed to call maxpm
# report and to report a failed turn's error as the message.
PLUGIN = """\
// MaximizePM: reports this OpenCode session's state with maxpm report (written by river/agents/opencode.py; do not
// edit). From herdr's V2 opencode plugin (Apache-2.0, github.com/herdrdev/herdr), changed to call maxpm report.
import { spawn } from "node:child_process";

const REPORT = __REPORT__;
const RESUME = __RESUME__;
const RELEASE = REPORT.map((w) => (w === "report" ? "release" : w));
const POLL_MS = 250;

function run(argv) {
  // A report never waits for maxpm, and its failure never stops OpenCode.
  try {
    spawn(argv[0], argv.slice(1), { stdio: "ignore", detached: true }).unref();
  } catch {}
}

function text(value) {
  return typeof value === "string" ? value : "";
}

function setup(api) {
  if (!process.env.MAXPM_AGENT) return; // a session that MaximizePM did not start: no report
  let disposed = false;
  let selected;
  let state = "idle";
  let why = "";
  let last;
  let seq = 0;
  const sessions = new Map();
  const blockers = new Map(); // "permission:<id>" or "form:<id>" -> [session id, message]

  function root(id) {
    const seen = new Set();
    while (typeof id === "string" && !seen.has(id)) {
      seen.add(id);
      const session = api.data.session.get(id) ?? sessions.get(id);
      if (!session) return id;
      if (!session.parentID) return id;
      id = session.parentID;
    }
  }

  function current() {
    const route = api.ui.router.current();
    return route && route.type === "session" ? root(route.sessionID) : undefined;
  }

  function publish() {
    if (disposed || !selected) return;
    const mine = [...blockers.values()].filter(([id]) => root(id) === selected);
    const value = mine.length ? "blocked" : state;
    const message = mine.length ? mine[0][1] : value === "blocked" ? why : "";
    const key = `${selected}\\0${value}\\0${message}`;
    if (key === last) return;
    last = key;
    // The number grows across restarts of the same session too: a late report never replaces a newer one.
    seq = Math.max(seq + 1, Date.now());
    const args = ["--state", value, "--seq", String(seq)];
    if (message) args.push("--message", message.slice(0, 300));
    args.push("--session-id", selected, "--", ...RESUME, selected);
    run([...REPORT, ...args]);
  }

  function sync() {
    if (disposed) return;
    const id = current();
    if (id !== selected) {
      selected = id;
      last = undefined;
      why = "";
      if (id) state = api.data.session.status(id) === "running" ? "working" : "idle";
    }
    publish();
  }

  function receive(payload) {
    const event = payload && payload.details;
    const data = event && event.data;
    if (disposed || !event || data == null) return;
    if (event.type === "session.created") sessions.set(data.sessionID, { id: data.sessionID, parentID: data.parentID });
    sync();
    const id = event.type === "form.created" ? data.form && data.form.sessionID : data.sessionID;
    switch (event.type) {
      case "permission.asked": {
        // 2.2.7: {id, sessionID, action: "read", resources: [".env"], ...}
        const where = Array.isArray(data.resources) ? data.resources.filter((r) => typeof r === "string").join(", ") : "";
        const what = [text(data.action), where].filter(Boolean).join(" ");
        blockers.set(`permission:${data.id}`, [id, what ? `permission: ${what}` : "permission required"]);
        break;
      }
      case "permission.replied":
        blockers.delete(`permission:${data.requestID}`);
        break;
      case "form.created":
        blockers.set(`form:${data.form && data.form.id}`, [id, text(data.form && data.form.title) || "a question"]);
        break;
      case "form.replied":
      case "form.cancelled":
        blockers.delete(`form:${data.id}`);
        break;
      case "session.execution.started":
        if (id !== selected) return;
        state = "working";
        why = "";
        break;
      case "session.execution.succeeded":
      case "session.execution.interrupted":
        if (id !== selected) return;
        state = "idle";
        why = "";
        break;
      case "session.execution.failed":
        if (id !== selected) return;
        state = "blocked";
        why = text(data.error && data.error.message) || "the turn failed";
        break;
      default:
        return;
    }
    publish();
  }

  const unsubscribe = api.data.listen(receive);
  const poll = setInterval(sync, POLL_MS);
  poll.unref?.();
  sync();
  // The TUI disposes its plugins when it quits (and on /reload, after which setup reports the state again).
  return () => {
    disposed = true;
    clearInterval(poll);
    unsubscribe();
    run(RELEASE);
  };
}

export default { id: "maxpm.state", setup };
"""


def plugin_text(report, resume):
    """The plugin's text for the maxpm report command `report` (argv words) and the resume words `resume`."""
    return PLUGIN.replace("__REPORT__", json.dumps(list(report))).replace("__RESUME__", json.dumps(list(resume)))


def _user_cli_plugins(env=None):
    """The plugins of the person's own cli.json (or of their OPENCODE_CLI_CONFIG_CONTENT): the plugin list of the
    environment replaces that of the file, so the start names them again. [] when there are none."""
    env = os.environ if env is None else env
    texts = [env.get(CLI_CONFIG_VAR) or ""]
    try:
        texts.append((config_dir(env) / "cli.json").read_text())
    except OSError:
        pass
    for t in texts:
        if not t.strip():
            continue
        # cli.json is JSONC: drop // and /* */ comments and trailing commas, outside of strings.
        t = re.sub(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/', lambda m: m.group(1) or "", t, flags=re.S)
        t = re.sub(r",(\s*[}\]])", r"\1", t)
        try:
            got = json.loads(t).get("plugins")
        except (ValueError, AttributeError):
            continue
        if isinstance(got, list):
            return [p for p in got if isinstance(p, (str, list))]
    return []


def install_hooks(folder, report):
    """Write the state plugin beside the queue (opencode/maxpm-state/tui.js), and return the words that load it:
    env OPENCODE_CLI_CONFIG_CONTENT={"plugins": [<the person's own>, <the plugin's folder>]}. The words go BEFORE
    the command (an env prefix; OpenCode has no flag for a plugin). It changes nothing in ~/.config/opencode: an
    opencode that a person starts by hand reports nothing. folder: the session's folder (the plugin is the same for
    each folder)."""
    d = _own_dir() / PLUGIN_DIR
    plugins = [p for p in _user_cli_plugins() if p != str(d)] + [str(d)]
    cli = f"{CLI_CONFIG_VAR}={json.dumps({'plugins': plugins}, separators=(',', ':'))}"
    text = plugin_text(report, ["env", cli, "opencode", "--standalone", "--session"])
    path = d / "tui.js"
    try:
        same = path.read_text() == text
    except OSError:
        same = False
    if not same:
        d.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(text)
        tmp.replace(path)
    return ["env", cli]


# ---------------------------------------------------------------------------------------------------------------
# 9. Resume
# ---------------------------------------------------------------------------------------------------------------

def resume_command(session_ref, model, effort, settings, name=None):
    """opencode --standalone --session <id>: the session goes on with its own model, unless a model is given."""
    if not session_ref or not re.fullmatch(r"[\w-]+", str(session_ref)):
        return None
    s = {k: v for k, v in (settings or {}).items() if k != "prompt"}
    return build_command(name, None, model, effort, {**s, "resume": session_ref}, (settings or {}).get("prompt"))


# ---------------------------------------------------------------------------------------------------------------
# 6. Chat log: the session's rows of the database, copied into a JSONL file
# ---------------------------------------------------------------------------------------------------------------

SESSION_COLUMNS = ("id", "directory", "title", "parent_id", "fork_session_id", "agent", "model", "time_created",
                   "time_updated", "tokens_input", "tokens_output", "tokens_reasoning", "tokens_cache_read",
                   "tokens_cache_write", "cost")


def _connect(db):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def export_lines(session_id, db=None):
    """The lines of a session's chat file: a header {"type": "session", <session_v2 columns>}, then one line per
    session_message row {"id", "type", "seq", "time", "data"} in seq order. None when the database or the session is
    not there."""
    db = Path(db) if db else db_path()
    if not db.is_file():
        return None
    try:
        conn = _connect(db)
        try:
            cols = ", ".join(SESSION_COLUMNS)
            head = conn.execute(f"SELECT {cols} FROM session_v2 WHERE id=?", (session_id,)).fetchone()
            if head is None:
                return None
            rows = conn.execute("SELECT id, type, seq, time_created, data FROM session_message WHERE session_id=? "
                                "ORDER BY seq", (session_id,)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    h = {"type": "session", **{k: head[k] for k in SESSION_COLUMNS}}
    # cwd: the key where the Sessions tab looks for a session's folder (sessions.last_cwd: the / list's commands).
    h["cwd"] = h["directory"]
    try:
        h["model"] = json.loads(h["model"]) if h["model"] else None
    except ValueError:
        pass
    out = [json.dumps(h, ensure_ascii=False)]
    for r in rows:
        try:
            data = json.loads(r["data"])
        except ValueError:
            data = {}
        out.append(json.dumps({"id": r["id"], "type": r["type"], "seq": r["seq"], "time": r["time_created"],
                               "data": data}, ensure_ascii=False))
    return out


def _db_mtime(db):
    got = 0
    for p in (db, Path(str(db) + "-wal")):
        try:
            got = max(got, p.stat().st_mtime_ns)
        except OSError:
            pass
    return got


def chat_path(session_ref, folder=None, root=None):
    """The chat file of a session id (opencode/chats/<id>.jsonl beside the queue), written again from the database
    when the database changed after it; a path of such a file as it is. None when the session is not in the
    database, and at once for an id that is no OpenCode id (the Sessions tab asks each adapter with the newest id of
    an agent of any kind). folder: unused (a session id is unique in the database). root: the folder of
    opencode.db in place of OpenCode's own (a test)."""
    ref = str(session_ref or "").strip()
    if not ref:
        return None
    if ref.endswith(".jsonl") or os.sep in ref:
        p = Path(ref).expanduser()
        return p if p.is_file() else None
    if not re.fullmatch(SESSION_ID, ref):
        return None
    db = Path(root) / "opencode.db" if root else db_path()
    out = _own_dir() / "chats" / f"{ref}.jsonl"
    stamp = _db_mtime(db)
    try:
        if stamp and out.stat().st_mtime_ns >= stamp:
            return out
    except OSError:
        pass
    lines = export_lines(ref, db)
    if lines is None:
        return out if out.is_file() else None
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + f".{os.getpid()}.tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(out)
    return out


def _rows(lines):
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


def _iso(ms):
    if not isinstance(ms, (int, float)):
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms / 1000)) + f".{int(ms) % 1000:03d}Z"


# The argument that says best what a call of each built-in tool does.
TOOL_DETAIL = {"bash": "command", "shell": "command", "read": "path", "edit": "path", "write": "path",
               "grep": "pattern", "glob": "pattern", "list": "path", "webfetch": "url", "websearch": "query"}


def _detail(name, args):
    args = args if isinstance(args, dict) else {}
    key = TOOL_DETAIL.get(name)
    for k in ((key,) if key else ()) + ("path", "filePath", "command", "pattern"):
        if isinstance(args.get(k), str):
            return args[k]
    return json.dumps(args, ensure_ascii=False)[:300] if args else ""


def _content_text(content):
    if isinstance(content, str):
        return content
    return "\n".join(c.get("text", "") for c in content or () if isinstance(c, dict) and c.get("type") == "text")


def _images(files):
    out = []
    for f in files or ():
        if not isinstance(f, dict):
            continue
        mime = f.get("mime") or f.get("mimeType") or ""
        url = f.get("url") or f.get("path") or ""
        if mime.startswith("image/") and url:
            out.append(url)
    return out


def _entries(row):
    """The shared entries of one chat row."""
    kind, d = row.get("type"), row.get("data") or {}
    at = _iso((d.get("time") or {}).get("created")) or _iso(row.get("time"))
    if kind == "user":
        e = {"at": at, "role": "user", "text": d.get("text", "")}
        imgs = _images(d.get("files"))
        return [{**e, "images": imgs} if imgs else e]
    if kind == "assistant":
        out = []
        for c in d.get("content") or ():
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text" and c.get("text", "").strip():
                out.append({"at": at, "role": "assistant", "text": c["text"]})
            elif c.get("type") == "tool":
                st = c.get("state") or {}
                out.append({"at": at, "role": "tool", "text": c.get("name", ""), "id": c.get("id", ""),
                            "name": c.get("name", ""), "detail": _detail(c.get("name"), st.get("input"))})
                if st.get("status") == "completed":
                    out.append({"at": at, "role": "result", "text": _content_text(st.get("content")),
                                "for": c.get("id", ""), "error": False})
                elif st.get("status") == "error":
                    out.append({"at": at, "role": "result", "text": (st.get("error") or {}).get("message", ""),
                                "for": c.get("id", ""), "error": True})
        # An interrupt ends the turn here. Any other error of a step shows on the idle row that closes the turn
        # (outcome failed), once: the Sessions tab reads line by line, so it cannot drop a second copy.
        if (d.get("error") or {}).get("type") == "aborted":
            out.append({"at": at, "role": "event", "text": "interrupted"})
        return out
    if kind == "idle":
        if d.get("outcome") == "failed":
            msg = (d.get("error") or {}).get("message") or "the turn failed"
            return [{"at": at, "role": "event", "text": f"error: {msg}"}]
        return []
    if kind == "compaction":
        return [{"at": at, "role": "divider", "text": "compacted", "kind": "compacted"},
                {"at": at, "role": "summary", "text": d.get("summary", "")}]
    return []  # synthetic: a note OpenCode adds for the model (a file the person opened), not a chat line


def _turn(rows):
    """working or idle at the end of the rows, or None when they hold no message."""
    last = next((r for r in reversed(rows) if r.get("type") in ("user", "assistant", "idle", "compaction")), None)
    if last is None:
        return None
    d = last.get("data") or {}
    if last["type"] == "idle":
        return "idle"
    if last["type"] == "user":
        return "working"
    if last["type"] == "compaction":
        return "idle" if d.get("status") == "completed" else "working"
    # assistant: a step that ended with an error (an interrupt) ends the turn; any other step is followed by an idle
    # row when the turn ends.
    return "idle" if d.get("error") else "working"


def parse_chat_line(d):
    """(entries, turn) of one decoded line of a chat file; the header line gives none."""
    if not isinstance(d, dict) or d.get("type") == "session":
        return [], None
    return _entries(d), _turn([d])


def parse_chat(lines):
    """(entries, turn) of the lines of one chat file."""
    rows = [r for r in _rows(lines) if r.get("type") != "session"]
    return [e for r in rows for e in _entries(r)], _turn(rows)


def _header(lines):
    return next((r for r in _rows(list(lines)[:3]) if r.get("type") == "session"), None)


def chat_chain(path, refs=None):
    """The chat files across forks, oldest first: each header's fork_session_id, back to the first. refs: unused (a
    resume goes on in the same session, and a fork names its parent)."""
    out, p = [], Path(path)
    while p is not None and p not in out:
        out.append(p)
        try:
            with open(p) as f:
                head = _header([f.readline()])
        except OSError:
            break
        parent = (head or {}).get("fork_session_id")
        p = chat_path(parent) if parent else None
    return out[::-1]


def chat_title(lines):
    return (_header(lines) or {}).get("title") or None


def chat_cwd(lines):
    head = _header(lines) or {}
    return head.get("cwd") or head.get("directory")


def chat_usage(lines):
    """The tokens of the session: OpenCode's own totals in the header (session_v2, with the compaction steps), else
    the sum of each assistant row's tokens. Output counts the reasoning tokens too."""
    tot = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    rows = _rows(lines)
    head = next((r for r in rows[:3] if r.get("type") == "session"), None)
    if head is not None:
        return {"input": int(head.get("tokens_input") or 0),
                "output": int(head.get("tokens_output") or 0) + int(head.get("tokens_reasoning") or 0),
                "cache_read": int(head.get("tokens_cache_read") or 0),
                "cache_write": int(head.get("tokens_cache_write") or 0)}
    for r in rows:
        t = (r.get("data") or {}).get("tokens") if r.get("type") == "assistant" else None
        if isinstance(t, dict):
            cache = t.get("cache") or {}
            tot["input"] += int(t.get("input") or 0)
            tot["output"] += int(t.get("output") or 0) + int(t.get("reasoning") or 0)
            tot["cache_read"] += int(cache.get("read") or 0)
            tot["cache_write"] += int(cache.get("write") or 0)
    return tot


def chat_settings(lines):
    """{"model": "provider/model"} of the last model step, or {} when there is none."""
    for r in reversed(_rows(lines)):
        m = (r.get("data") or {}).get("model") if r.get("type") == "assistant" else None
        if isinstance(m, dict) and m.get("id"):
            return {"model": f"{m.get('providerID')}/{m['id']}" if m.get("providerID") else m["id"]}
    return {}


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


def _commands_in(base):
    out = []
    for sub in ("command", "commands"):
        d = Path(base) / sub
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*.md")):
            try:
                meta, body = _front(f.read_text(errors="replace"))
            except OSError:
                continue
            first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
            name = f.relative_to(d).with_suffix("").as_posix()
            out.append({"name": name, "description": meta.get("description") or first[:120],
                        "hint": meta.get("argument-hint", "")})
    return out


def read_commands(folder):
    """The custom commands of a session in folder: command/ and commands/ of .opencode in the folder and in each
    folder up to the git root, then of the global config folder. A name that comes twice keeps the first one."""
    out = []
    if folder:
        d = Path(folder).expanduser()
        while True:
            out += _commands_in(d / ".opencode")
            if (d / ".git").exists() or d.parent == d:
                break
            d = d.parent
    out += _commands_in(config_dir())
    builtin = {c["name"] for c in COMMANDS}
    seen, uniq = set(builtin), []
    for c in out:
        if c["name"] not in seen:
            seen.add(c["name"])
            uniq.append(c)
    return uniq

