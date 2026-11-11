"""The Aider adapter (docs/agent-adapters.md, #5177): aider, the AI pair programmer in the terminal (aider.chat).

Read from Aider 0.86.2 on macOS (uv tool install aider-chat), its --help, its source and a recorded session
(tests/fixtures/aider). What Aider gives, and what this module does with it:

- No hook, plugin or extension system, and no session id. The state comes from the screen (DETECT). Aider's
  --notifications-command runs only when a reply is ready, so it cannot give working or blocked, and one signal
  has one source.
- The chat log is a Markdown file (--chat-history-file; by default .aider.chat.history.md in the git root, which
  every session of the folder appends to). Each session that MaximizePM starts gets its own file,
  <maxpm folder>/aider/<session name>.chat.history.md, so the session name is the session's id: maxpm chose it
  before the start, and build_command puts it in MAXPM_AIDER_SESSION (IDENTITY source "env").
- Resume: the same file with --restore-chat-history (Aider reads the old turns back into the chat). The files
  added to the chat and the chat mode are not restored.
- The first prompt: Aider has no flag that sends a first prompt and keeps the chat open (--message and
  --message-file exit after one reply; a /code in a --load file runs, but its turn is not kept). build_command
  therefore leaves the prompt out; FIRST_PROMPT says that the start path pastes it into the pane when the idle
  prompt shows, as any later message.
- Aider runs no shell command by itself: it proposes one ("Run shell command? (Y)es/(N)o"), and a person says yes.
  --yes-always answers the other questions, but never that one. A session therefore needs a person to run maxpm
  commands; on the Sessions page the person answers in the message box.
- Commands that Aider runs (/run, a proposed shell command) inherit its environment. Aider sets no variable of its
  own, so build_command passes --set-env MAXPM_AIDER_SESSION=<name> (IDENTITY env and inside_env).
- The model is a litellm "provider/model" text (anthropic/claude-sonnet-4-5, openai/gpt-5, gemini/...), or one of
  Aider's aliases (sonnet, opus, haiku, gemini, deepseek, ...): family "multi". Aider signs in with an API key in
  the environment, a .env file or .aider.conf.yml (or OpenRouter's sign-in when it finds none); MaximizePM never
  passes a key.
"""

import re
import shlex
from pathlib import Path

from ..core import river_dir
from . import Raw

# ---------------------------------------------------------------------------------------------------------------
# 1. Describe
# ---------------------------------------------------------------------------------------------------------------

DESCRIBE = {
    "id": "aider",
    "label": "Aider",
    "exe": "aider",
    "aliases": ["aider-chat"],  # the name of its Python package (pip, uv tool, pipx)
    "family": "multi",
    # Aider's own aliases (aider/models.py MODEL_ALIASES) and litellm "provider/model" texts; any text goes.
    "models": ["sonnet", "opus", "haiku", "gemini", "deepseek", "openai/gpt-5", "anthropic/claude-sonnet-4-5"],
    "default_model": "",  # Aider picks a model from the API keys it finds
    "model_ids": {},
    "prefix": "aider_",
}

# ---------------------------------------------------------------------------------------------------------------
# 2. Settings
# ---------------------------------------------------------------------------------------------------------------

EDIT_FORMATS = ["diff", "whole", "udiff", "diff-fenced", "architect", "ask"]

SETTINGS = [
    {"key": "edit_format", "label": "Edit format", "kind": "choice", "choices": EDIT_FORMATS, "default": "",
     "text": "How the model writes its edits; empty uses the model's own default (ask: answers only, no edits).",
     "flag": ["--edit-format", "{value}"], "live": "/chat-mode {value}", "read": r"--edit-format[= ](\S+)"},
    {"key": "auto_commits", "label": "Commit each edit", "kind": "toggle", "choices": [], "default": "on",
     "text": "Aider commits each edit it makes, with a message it writes (/undo takes the last one back).",
     "flag": [], "live": None, "read": None},  # on is Aider's default; off: --no-auto-commits (OFF_FLAGS)
    {"key": "yes_always", "label": "Yes to every question", "kind": "toggle", "choices": [], "default": "off",
     "text": "Answers yes to Aider's questions (add a file, create a file); a shell command still waits for a person.",
     "flag": ["--yes-always"], "live": None, "read": None},
    {"key": "cache_prompts", "label": "Prompt cache", "kind": "toggle", "choices": [], "default": "off",
     "text": "Asks the provider to cache the prompt (Anthropic, DeepSeek): later turns cost less.",
     "flag": ["--cache-prompts"], "live": None, "read": None},
    # A start asks no question (#5270): nobody watches a new pane, and the first prompt waits for the idle line.
    {"key": "gitignore", "label": "Add .aider* to .gitignore", "kind": "toggle", "choices": [], "default": "off",
     "text": "On: Aider asks at the start to add its files (.aider*) to the project's .gitignore. Off: it does not ask.",
     "flag": [], "live": None, "read": None},  # off: --no-gitignore (OFF_FLAGS)
    {"key": "model_warnings", "label": "Model warnings at the start", "kind": "toggle", "choices": [],
     "default": "off",
     "text": "On: Aider warns about a model it knows little of (or a missing variable such as OLLAMA_API_BASE) and "
             "asks to open its docs. Off: it starts at once; a missing key shows at the first prompt.",
     "flag": [], "live": None, "read": None},  # off: --no-show-model-warnings (OFF_FLAGS)
    {"key": "analytics", "label": "Aider's analytics", "kind": "toggle", "choices": [], "default": "off",
     "text": "On: Aider's own choice (it asks one new user in ten, once). Off: no analytics for the session, and no "
             "question; a choice stored before stays as it is.",
     "flag": [], "live": None, "read": None},  # off: --no-analytics (OFF_FLAGS)
    {"key": "weak_model", "label": "Weak model", "kind": "text", "choices": [], "default": "",
     "text": "The model for commit messages and chat summaries; empty: Aider picks one for the main model.",
     "flag": ["--weak-model", "{value}"], "live": "/weak-model {value}", "read": r"--weak-model[= ](\S+)"},
    {"key": "args", "label": "More arguments", "kind": "text", "choices": [], "default": "",
     "text": "Other aider flags, as you type them in a shell (aider --help lists them).",
     "flag": [], "live": None, "read": None},
]

# ---------------------------------------------------------------------------------------------------------------
# 3. Effort
# ---------------------------------------------------------------------------------------------------------------

# Aider passes --reasoning-effort to the model's API as it is (reasoning_effort; OpenRouter: reasoning.effort), and
# only to a model whose settings accept it ("does not support 'reasoning_effort', ignoring" otherwise). The common
# levels are low, medium and high. "read" finds it on a command line (--reasoning-effort high) and in the chat log's
# model announcement ("Model: <name> with <format> edit format, reasoning high").
EFFORT = {
    "levels": ["low", "medium", "high"],
    "map": {"xhigh": "high", "max": "high"},
    "flag": ["--reasoning-effort", "{value}"],
    "live": "/reasoning-effort {value}",
    "read": r"(?:--reasoning-effort[= ]|edit format\b.*?, reasoning )([a-z][a-z0-9_-]{0,31})\b",
}

# ---------------------------------------------------------------------------------------------------------------
# 4. Launch
# ---------------------------------------------------------------------------------------------------------------

# system_prompt_file: the maxpm skill goes in as a read-only file (--read), the one way Aider takes a text into each
# turn. resume: --restore-chat-history on the session's own chat log.
LAUNCH = {"session_name": False, "system_prompt_file": True, "hooks_file": False, "extra_dirs": False,
          "fork": False, "resume": True, "autocompact": False, "background": False}

# Where the first prompt goes: "paste" (build_command leaves it out; the start path types it into the pane when
# DETECT['idle'] shows, as a later message): agents.FIRST_PROMPTS, server.paste_first_prompts (#5221).
FIRST_PROMPT = "paste"

SESSION_ENV = "MAXPM_AIDER_SESSION"

# The toggles whose "off" is a flag of Aider's (on is Aider's own behaviour, with no flag).
OFF_FLAGS = {"auto_commits": "--no-auto-commits", "gitignore": "--no-gitignore",
             "model_warnings": "--no-show-model-warnings", "analytics": "--no-analytics"}


def chat_dir():
    """The folder of the chat logs of the sessions that MaximizePM starts: aider/ beside the queue."""
    return Path(river_dir()) / "aider"


def _safe(name):
    return re.sub(r"[^\w.-]", "_", str(name or "")) or "aider"


def chat_file(name):
    """The chat log of session `name`: <maxpm folder>/aider/<name>.chat.history.md."""
    return chat_dir() / f"{_safe(name)}.chat.history.md"


def input_file(name):
    """The prompt history of session `name` (Up in Aider's input line): beside its chat log, not in the project."""
    return chat_dir() / f"{_safe(name)}.input.history"


def _skill_file():
    """A file of the maxpm skill (the same file as Claude Code's skill_prompt), or None."""
    from .. import core
    words = shlex.split(core.skill_prompt_args(""))
    return words[-1] if words[-2:-1] == ["--append-system-prompt-file"] else None


def build_command(name, folder, model, effort, settings, prompt):
    """aider [--model M] [--reasoning-effort E] [settings] --chat-history-file <its log> --input-history-file <its
    prompts> [--restore-chat-history] --no-show-release-notes --set-env MAXPM_AIDER_SESSION=<name> [--read <skill>]
    [--read AGENTS.md] [args]. The session's log is chat_file(name); a resume (settings['resume']) appends to the log
    and the prompts of the session it resumes. With no name and no resume, Aider keeps both in the git root. The
    prompt is not in the argv (FIRST_PROMPT). AGENTS.md of the folder goes in with --read, as Aider reads no
    instruction file by itself. A start asks no question (#5270), as nobody watches a new pane yet: the release notes
    question is off, and so are, by their settings' defaults, the .gitignore question, the analytics question and the
    model warnings (with
    them off, ollama_chat/<model> reaches the local Ollama at its default address with no OLLAMA_API_BASE)."""
    s = settings or {}
    argv = ["aider"]
    if model:
        argv += ["--model", model]
    level = EFFORT["map"].get(effort, effort) if effort else None
    if level:
        argv += [w.format(value=level) for w in EFFORT["flag"]]
    for spec in SETTINGS:
        value = str(s.get(spec["key"]) or spec["default"] or "").strip()
        if spec["key"] in OFF_FLAGS:
            argv += [OFF_FLAGS[spec["key"]]] if value == "off" else []
        elif spec["kind"] == "toggle":
            argv += list(spec["flag"]) if value == "on" else []
        elif value and spec["flag"]:
            argv += [w.format(value=value) for w in spec["flag"]]
    log = s.get("resume") or name
    if log:
        argv += ["--chat-history-file", str(chat_file(log)), "--input-history-file", str(input_file(log))]
    if s.get("resume"):
        argv.append("--restore-chat-history")
    argv.append("--no-show-release-notes")
    if name:
        argv += ["--set-env", f"{SESSION_ENV}={name}"]
    skill = _skill_file() if s.get("system_prompt_file") else None
    if skill:
        argv += ["--read", skill]
    if folder and Path(folder, SETUP["instructions"]).is_file():
        argv += ["--read", SETUP["instructions"]]
    if (s.get("args") or "").strip():
        argv.append(Raw(s["args"].strip()))
    return argv


def start_session(name, settings):
    """The session id a start chooses (agents part 5, #5280): the name that build_command gives the chat log and
    MAXPM_AIDER_SESSION, or the session that a resume continues."""
    return (settings or {}).get("resume") or name or None


def parse_command(words):
    """A hand-written aider command that a profile builds exactly: --model {model}, --reasoning-effort {effort}, and
    the flags of SETTINGS. Aider takes no prompt on its command line, so the profile's prompt is None. Anything else
    (a fixed model, --message, an API key, another flag): None, and the command stays custom."""
    if not words or Path(words[0]).name.lower() != "aider":
        return None
    opts, i = {}, 1
    choices = next(sp["choices"] for sp in SETTINGS if sp["key"] == "edit_format")
    while i < len(words):
        t, nxt = words[i], (words[i + 1] if i + 1 < len(words) else None)
        if t == "--model" and nxt == "{model}" or t == "--reasoning-effort" and nxt == "{effort}":
            i += 2
        elif t in ("--edit-format", "--chat-mode") and nxt in choices:
            opts["edit_format"] = nxt
            i += 2
        elif t == "--weak-model" and nxt and not nxt.startswith("-"):
            opts["weak_model"] = nxt
            i += 2
        elif t in OFF_FLAGS.values():
            opts[next(k for k, f in OFF_FLAGS.items() if f == t)] = "off"
            i += 1
        elif t in ("--yes-always", "--cache-prompts"):
            opts[t[2:].replace("-", "_")] = "on"
            i += 1
        else:
            return None
    return {"settings": opts, "prompt": None}


# ---------------------------------------------------------------------------------------------------------------
# 5. Session identity
# ---------------------------------------------------------------------------------------------------------------

# build_command sets MAXPM_AIDER_SESSION to the session name with --set-env, and Aider passes its environment to each
# command it runs: a maxpm command there records the name as the session id (#5212), and the Sessions tab finds the
# log by it (CHAT ref "session", chat_file). The id pattern is that of chat_file's names.
IDENTITY = {
    "source": "env",
    "env": SESSION_ENV,
    "id_pattern": r"[A-Za-z0-9][\w.-]{0,127}",
    "log_glob": None,
    "pane": None,
    "url": None,
    "inside_env": [SESSION_ENV],
}

# ---------------------------------------------------------------------------------------------------------------
# 6. Chat log
# ---------------------------------------------------------------------------------------------------------------

# ref: the session id is the session name (IDENTITY), the stem of its log. cache_write: Aider caches a prompt only with
# --cache-prompts and a provider that caches (Anthropic): a call after the cache ran out costs the input price.
# A Markdown log (#5222): the Sessions tab parses it a block at a time, a block from each prompt ('#### ' lines) to
# the next; a session's start line ends the block before it, where parse_chat makes it a 'Resumed' divider.
CHAT = {"compressed": False, "file_tools": ["edit"], "ref": "session", "cache_write": False, "format": "text",
        "block": r"^#### "}

_START = re.compile(r"^# aider chat started at (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s*$")
_USER = re.compile(r"^#### ?(.*?)\s*$")
_QUOTE = re.compile(r"^>(?: (.*?))?\s*$")
_APPLIED = re.compile(r"^Applied edit to (.+)$")
_RUNNING = re.compile(r"^Running (.+)$")
_TOKENS = re.compile(r"Tokens: ([\d.]+k?) sent(?:, ([\d.]+k?) cache write)?(?:, ([\d.]+k?) cache hit)?, "
                     r"([\d.]+k?) received\.")
# A prompt of the person that starts a model turn: plain text, or one of these commands with text after it.
_TURN_COMMANDS = ("/ask ", "/code ", "/architect ", "/context ", "/help ")


def chat_path(session_ref, folder=None, root=None):
    """The chat log of a session: its own file in chat_dir() (or in root, a folder a test gives), by the session name;
    or session_ref itself when it names a .md file. None when there is no such file yet."""
    ref = str(session_ref or "").strip()
    if not ref:
        return None
    p = Path(ref).expanduser()
    if p.suffix == ".md":
        return p if p.is_file() else None
    p = (Path(root) / chat_file(ref).name) if root else chat_file(ref)
    return p if p.is_file() else None


def _iso(stamp):
    """Aider's local start time as ISO with this computer's offset."""
    from datetime import datetime
    return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").astimezone().isoformat(timespec="seconds")


def parse_chat(lines):
    """(entries, turn) of the lines of an Aider chat log. The log has these blocks:
    '# aider chat started at <time>' starts a session (a later one in the same file: a resume, a divider);
    '#### <text>' lines are the person's prompt (one line each); '> <text>' lines are Aider's own output (warnings,
    questions with the answer, 'Applied edit to <file>', 'Running <command>', the token report) and become one event
    for each run of them, with a tool entry for each edit and each shell command; any other text, after a blank
    line, is the model's reply. A line with no '> ' directly under a '> ' line continues it (a message of several
    lines gets the '> ' only on its first line). turn: 'working' after a prompt with no reply yet, else None (the
    log does not say whether Aider still applies edits or waits at a question: the screen does). The log has no
    time but the start of each session: each entry gets the start of its session."""
    entries, at = [], None
    kind, buf, prev = None, [], "blank"
    tools = 0

    def flush():
        nonlocal kind, buf, tools
        text = "\n".join(buf).strip("\n")
        if kind == "user":
            entries.append({"at": at, "role": "user", "text": text})
        elif kind == "assistant" and text.strip():
            entries.append({"at": at, "role": "assistant", "text": text.strip()})
        elif kind == "event" and text.strip():
            entries.append({"at": at, "role": "event", "text": text})
            for line in buf:
                m = _APPLIED.match(line)
                r = _RUNNING.match(line)
                if m or r:
                    tools += 1
                    entries.append({"at": at, "role": "tool", "id": f"t{tools}",
                                    "name": "edit" if m else "shell", "text": (m or r).group(1),
                                    "detail": (m or r).group(1)})
        kind, buf = None, []

    for raw in lines:
        line = raw.rstrip("\n").rstrip("\r")
        m = _START.match(line)
        if m:
            flush()
            at = _iso(m.group(1))
            if entries:
                entries.append({"at": at, "role": "divider", "kind": "resumed", "text": "Resumed"})
            prev = "blank"
            continue
        u = _USER.match(line)
        q = _QUOTE.match(line)
        if u:
            if kind != "user":
                flush()
                kind = "user"
            buf.append(u.group(1))
            prev = "user"
        elif q:
            if kind != "event":
                flush()
                kind = "event"
            buf.append(q.group(1) or "")
            prev = "quote"
        elif not line.strip():
            if kind == "assistant":
                buf.append("")
            elif kind in ("user", "event"):
                flush()
            prev = "blank"
        elif prev == "quote" and kind == "event":
            buf.append(line.rstrip())  # the next line of a message of several lines
        else:
            if kind != "assistant":
                flush()
                kind = "assistant"
            buf.append(line.rstrip())
            prev = "text"
    flush()
    turn = None
    last = next((e for e in reversed(entries) if e["role"] != "divider"), None)
    if last and last["role"] == "user":
        text = last["text"]
        if not text.startswith("/") or text.startswith(_TURN_COMMANDS):
            turn = "working"
    return entries, turn


def _count(text):
    if not text:
        return 0
    return round(float(text[:-1]) * 1000) if text.endswith("k") else int(float(text))


def chat_usage(lines):
    """The token counts of the token reports ('Tokens: 2.5k sent, 1.2k cache write, 3.4k cache hit, 11 received.').
    Aider rounds counts of 1000 and more to 0.1k (1k from 10k), so the sums are close, not exact."""
    out = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    for line in lines:
        m = _TOKENS.search(line)
        if m:
            out["input"] += _count(m.group(1))
            out["cache_write"] += _count(m.group(2))
            out["cache_read"] += _count(m.group(3))
            out["output"] += _count(m.group(4))
    return out


_MODEL_LINE = re.compile(r"^(?:> )?(?:Main model|Model): (\S+) with (\S+) edit format")


def chat_settings(lines):
    """{setting key: value} as the session runs now, from the last model announcement ('Model: <name> with <format>
    edit format, reasoning high'): the edit format. Aider writes one at each start, /model, /chat-mode and
    /reasoning-effort."""
    got = {}
    for line in lines:
        m = _MODEL_LINE.match(line.rstrip())
        if m:
            got = {"edit_format": m.group(2)}
    return got


def read_effort(entries):
    """The reasoning effort of the last model announcement in the entries, or None (Aider then sends none)."""
    rx = re.compile(EFFORT["read"])
    for e in reversed(entries):
        if e["role"] != "event":
            continue
        for line in reversed(e["text"].split("\n")):
            if _MODEL_LINE.match(line):
                m = rx.search(line)
                return m.group(1) if m else None
    return None


# ---------------------------------------------------------------------------------------------------------------
# 7. State
# ---------------------------------------------------------------------------------------------------------------

STATE = {"state": "detect", "session_id": "identity"}

# The pane watcher (server.watch_prompts, #5183) applies each pattern to each of the pane's last lines (blank lines
# left out, spaces at the end cut): working first, then idle; blocked when a question is the last thing on the
# screen. A screen that matches neither working nor idle keeps the state the watcher reported last. Recorded
# screens: tests/fixtures/aider/screen-*.txt.
#
# The input line: Aider's prompt prefix is the edit format when it is not the model's default, then " multi" in
# multiline mode, then "> " (io.get_input); tmux drops the space at the end of a line.
_PROMPT = r"(?:[\w-]+(?: multi)?)?>"

DETECT = {
    # The pane runs Python with the aider script: tmux's pane_current_command is "Python" (macOS) or "python3", not
    # this name. The watcher finds Aider's adapter by the agent's type (MAXPM_AIDER_SESSION, IDENTITY).
    "process": "aider",
    # The empty input line: '>', 'diff>', 'ask multi>'. Once the person sends a prompt, the line keeps its text
    # ('> Add a script'), so a sent prompt in the scrollback is no idle line. While the person types, the line has
    # text too, and the screen says neither: the state stays idle.
    "idle": r"^" + _PROMPT + r"$",
    # Aider's spinner before the first token of a reply ('░█  Waiting for openai/gpt-5'); it clears it when the
    # reply starts. While the reply streams, the screen shows only its text (neither), so the state stays working
    # until the empty input line comes back. A shell command that runs after its question shows no spinner either.
    "working": r"\bWaiting for \S+$",
    # Every question of Aider (io.confirm_ask; Aider asks no other kind): '<question> (Y)es/(N)o[/(A)ll]
    # [/(S)kip all][/(D)on't ask again] [Yes]: ', with nothing typed after it. Examples: create a file, add a file,
    # run a shell command, add its output, fix lint errors, add .aider* to .gitignore, open a URL. In a narrow pane
    # (57 columns) prompt_toolkit puts the ':' on a line of its own, so the question line may end at ']' (#5265); the
    # watcher takes that ':' line as the rest of the question.
    "blocked": [r"\(Y\)es/\(N\)o(?:/\([A-Z]\)[^/\[]*)*\s\[[^\]]*\]:?$"],
    # Aider's questions take y, n, a, s or d; there is no option to type an answer instead.
    "tell_instead": None,
    # No pasted images in the prompt: /paste reads the clipboard, and /add <file.png> adds an image file.
    "image_paste": None,
}

# ---------------------------------------------------------------------------------------------------------------
# 8. Input
# ---------------------------------------------------------------------------------------------------------------

INPUT = {"native": None}

# ---------------------------------------------------------------------------------------------------------------
# 9. Resume
# ---------------------------------------------------------------------------------------------------------------


def resume_command(session_ref, model, effort, settings, name=None):
    """aider on the chat log of session_ref (a session name) with --restore-chat-history, which reads the old turns
    back into the chat; None when that session has no log. The files of the chat and its chat mode do not come back.
    A first prompt (settings['prompt']) goes in as at a start (FIRST_PROMPT)."""
    if not session_ref or chat_path(session_ref) is None:
        return None
    ref = Path(str(session_ref)).name.removesuffix(".chat.history.md")
    s = {k: v for k, v in (settings or {}).items() if k not in ("fork", "prompt")}
    return build_command(name or ref, None, model, effort, {**s, "resume": ref}, None)


# ---------------------------------------------------------------------------------------------------------------
# 10. Commands
# ---------------------------------------------------------------------------------------------------------------

# Aider 0.86.2's built-in commands (aider/commands.py, the first line of each cmd_ docstring). Aider has no custom
# commands; /load runs a file of commands.
COMMANDS = [
    {"name": "add", "description": "Add files to the chat so aider can edit them or review them in detail",
     "hint": "<file> ..."},
    {"name": "architect", "description": "Enter architect/editor mode using 2 different models. If no prompt "
     "provided, switches to architect/editor mode.", "hint": "[prompt]"},
    {"name": "ask", "description": "Ask questions about the code base without editing any files. If no prompt "
     "provided, switches to ask mode.", "hint": "[question]"},
    {"name": "chat-mode", "description": "Switch to a new chat mode", "hint": "<mode>"},
    {"name": "clear", "description": "Clear the chat history", "hint": ""},
    {"name": "code", "description": "Ask for changes to your code. If no prompt provided, switches to code mode.",
     "hint": "[prompt]"},
    {"name": "commit", "description": "Commit edits to the repo made outside the chat (commit message optional)",
     "hint": "[message]"},
    {"name": "context", "description": "Enter context mode to see surrounding code context. If no prompt "
     "provided, switches to context mode.", "hint": "[prompt]"},
    {"name": "copy", "description": "Copy the last assistant message to the clipboard", "hint": ""},
    {"name": "copy-context", "description": "Copy the current chat context as markdown, suitable to paste into a "
     "web UI", "hint": ""},
    {"name": "diff", "description": "Display the diff of changes since the last message", "hint": ""},
    {"name": "drop", "description": "Remove files from the chat session to free up context space",
     "hint": "[file] ..."},
    {"name": "edit", "description": "Alias for /editor: Open an editor to write a prompt", "hint": ""},
    {"name": "editor", "description": "Open an editor to write a prompt", "hint": ""},
    {"name": "editor-model", "description": "Switch the Editor Model to a new LLM", "hint": "<model>"},
    {"name": "exit", "description": "Exit the application", "hint": ""},
    {"name": "git", "description": "Run a git command (output excluded from chat)", "hint": "<args>"},
    {"name": "help", "description": "Ask questions about aider", "hint": "[question]"},
    {"name": "lint", "description": "Lint and fix in-chat files or all dirty files if none in chat",
     "hint": "[file] ..."},
    {"name": "load", "description": "Load and execute commands from a file", "hint": "<file>"},
    {"name": "ls", "description": "List all known files and indicate which are included in the chat session",
     "hint": ""},
    {"name": "map", "description": "Print out the current repository map", "hint": ""},
    {"name": "map-refresh", "description": "Force a refresh of the repository map", "hint": ""},
    {"name": "model", "description": "Switch the Main Model to a new LLM", "hint": "<model>"},
    {"name": "models", "description": "Search the list of available models", "hint": "<search>"},
    {"name": "multiline-mode", "description": "Toggle multiline mode (swaps behavior of Enter and Meta+Enter)",
     "hint": ""},
    {"name": "paste", "description": "Paste image/text from the clipboard into the chat. Optionally provide a "
     "name for the image.", "hint": "[name]"},
    {"name": "quit", "description": "Exit the application", "hint": ""},
    {"name": "read-only", "description": "Add files to the chat that are for reference only, or turn added files "
     "to read-only", "hint": "[file] ..."},
    {"name": "reasoning-effort", "description": "Set the reasoning effort level (values: number or low/medium/high "
     "depending on model)", "hint": "<level>"},
    {"name": "report", "description": "Report a problem by opening a GitHub Issue", "hint": ""},
    {"name": "reset", "description": "Drop all files and clear the chat history", "hint": ""},
    {"name": "run", "description": "Run a shell command and optionally add the output to the chat (alias: !)",
     "hint": "<command>"},
    {"name": "save", "description": "Save commands to a file that can reconstruct the current chat session's "
     "files", "hint": "<file>"},
    {"name": "settings", "description": "Print out the current settings", "hint": ""},
    {"name": "test", "description": "Run a shell command and add the output to the chat on non-zero exit code",
     "hint": "<command>"},
    {"name": "think-tokens", "description": "Set the thinking token budget, eg: 8096, 8k, 10.5k, 0.5M, or 0 to "
     "disable.", "hint": "<tokens>"},
    {"name": "tokens", "description": "Report on the number of tokens used by the current chat context", "hint": ""},
    {"name": "undo", "description": "Undo the last git commit if it was done by aider", "hint": ""},
    {"name": "voice", "description": "Record and transcribe voice input", "hint": ""},
    {"name": "weak-model", "description": "Switch the Weak Model to a new LLM", "hint": "<model>"},
    {"name": "web", "description": "Scrape a webpage, convert to markdown and send in a message", "hint": "<url>"},
]

# ---------------------------------------------------------------------------------------------------------------
# 11. Setup
# ---------------------------------------------------------------------------------------------------------------

SETUP = {
    # Aider reads no instruction file by itself; build_command passes this one with --read when the folder has it.
    "instructions": "AGENTS.md",
    "skills_dir": None,
    "voice": "/voice records from the microphone and types the words as the prompt (it needs an OpenAI key).",
    "open_link": None,
}


# How each command works from the page (#5220, #5291). Aider is a line-based program: a command prints its answer into
# the terminal. From its own descriptions (on this Mac it waited at its .gitignore question first): /model and
# /reasoning-effort set what the Settings rows Model and Effort set; /tokens reports the tokens of the chat (the Usage
# panel); the lists it prints (/settings, /models, /ls, /map, /diff, /help) need the terminal; the others run.
_RUNS = {
    "terminal": {"settings", "models", "ls", "map", "diff", "help", "editor", "edit", "editor-model", "weak-model"},
}
for _c in COMMANDS:
    _c["run"] = next((k for k, v in _RUNS.items() if _c["name"] in v), "now")
    if _c["name"] in ("model", "reasoning-effort"):
        _c["setting"], _c["run"] = ("model" if _c["name"] == "model" else "effort"), "terminal"
    if _c["name"] == "tokens":
        _c["page"], _c["run"] = "usage", "terminal"
