"""The Codex adapter (docs/agent-adapters.md). Only its describe part so far: #5181 moves launch, resume and
identity here, #5182 the chat log, #5183 the state, #5184 the setup, #5169 the settings and effort."""

DESCRIBE = {
    "id": "codex",
    "label": "Codex",
    "exe": "codex",
    "aliases": [],
    "family": "openai",
    "models": ["luna", "terra", "sol", "astra"],
    "default_model": "",
    # Codex takes full ids, and they change with each OpenAI release (the default of codex_model_ids, core.py).
    "model_ids": {"luna": "gpt-6-luna", "terra": "gpt-5.6-terra", "sol": "gpt-6.1-sol", "astra": "gpt-6-astra"},
    "prefix": "codex_",
}


def read_models():
    """The model ids in Codex's own model cache (~/.codex/models_cache.json, or $CODEX_HOME), or None without one."""
    got = codex_models()
    return sorted(got) if got else None


# ---------------------------------------------------------------------------------------------------------------
# Effort, launch, identity and resume (#5181)
# ---------------------------------------------------------------------------------------------------------------

import os  # noqa: E402
from pathlib import Path  # noqa: E402

from . import Raw  # noqa: E402

EFFORT = {
    "levels": ["low", "medium", "high", "xhigh", "max"],  # when Codex's model cache does not list the model
    "map": {},
    "flag": ["-c", "model_reasoning_effort={value}"],
    "live": None,
    "read": r"model_reasoning_effort=([a-z][a-z0-9_-]{0,31})\b",
}

LAUNCH = {"extra_dirs": True, "resume": True}

# A Codex session records no task link: its web link is Codex's web app (core.CODEX_WEB).
IDENTITY = {"source": "env", "env": "CODEX_THREAD_ID", "log_glob": None, "pane": None, "url": "https://chatgpt.com/codex",
            "inside_env": ["CODEX_THREAD_ID", "CODEX_SANDBOX"]}


def read_effort_levels(model):
    """The effort levels Codex takes for model id `model`, from its model cache; None when the cache does not list it.
    MaximizePM refuses another effort for that model before it starts Codex."""
    return (codex_models() or {}).get(model) if model else None


def build_command(name, folder, model, effort, settings, prompt):
    """codex [resume <id>] [-m M] [-c model_reasoning_effort=E] [--sandbox S] [--ask-for-approval A] --add-dir <queue
    folder> [--no-daemon] <args> <prompt>. Codex has no flag for a session name. Codex 0.159 ends at start, in a
    session with an API key, when it may use its shared background server ("requires api_key_model_discovery to be
    disabled"); --no-daemon runs it alone (#4621), when the installed Codex has the flag and the person did not take
    it out (settings['noflags'])."""
    from ..core import river_dir
    s = settings
    words = ["codex"] + (["resume", s["resume"]] if s.get("resume") else [])
    words += (["-m", model] if model else []) + ([w.format(value=effort) for w in EFFORT["flag"]] if effort else [])
    words += ["--sandbox", s["sandbox"]] if s.get("sandbox") else []
    words += ["--ask-for-approval", s["approval"]] if s.get("approval") else []
    words += ["--add-dir", river_dir()]
    if ("--no-daemon" in codex_flags() and "--no-daemon" not in s.get("args", "")
            and "--no-daemon" not in (s.get("noflags") or ())):
        words.append("--no-daemon")
    words += [Raw(s["args"].strip())] if s.get("args", "").strip() else []
    words.append(prompt)
    return words


def resume_command(session_ref, model, effort, settings, name=None):
    """codex resume <thread id>, with the profile's settings and settings['prompt'] as its first prompt."""
    return build_command(name, None, model, effort, {**settings, "resume": session_ref}, settings.get("prompt"))


def parse_command(words):
    """A hand-written codex command that a profile builds exactly: -m {model}, -c model_reasoning_effort={effort},
    --sandbox and --ask-for-approval with a known value, --add-dir of the queue folder, and one prompt."""
    from ..core import river_dir
    if not words or Path(words[0]).name.lower() != "codex":
        return None
    choices = {sp["key"]: sp for sp in SETTINGS}
    opts, prompt, i = {}, None, 1
    while i < len(words):
        t, nxt = words[i], (words[i + 1] if i + 1 < len(words) else None)
        if t in ("-m", "--model") and nxt == "{model}" or t in ("-c", "--config") and nxt == "model_reasoning_effort={effort}":
            i += 2
        elif t in ("-s", "--sandbox") and nxt in choices["sandbox"]["choices"]:
            opts["sandbox"] = nxt
            i += 2
        elif t in ("-a", "--ask-for-approval") and nxt in choices["approval"]["choices"]:
            opts["approval"] = nxt
            i += 2
        elif t == "--add-dir" and nxt is not None and (
                nxt == "{maxpm_dir}" or os.path.realpath(os.path.expanduser(nxt)) == os.path.realpath(river_dir())):
            i += 2
        elif not t.startswith("-") and prompt is None and t.strip() and ";" not in t:
            prompt = t
            i += 1
        else:
            return None
    return {"settings": opts, "prompt": prompt} if prompt is not None else None


# ---------------------------------------------------------------------------------------------------------------

STATE = {"state": "detect", "session_id": "identity"}

# The words of its blocked screens: an approval ("Would you like to run the following command?"), the trust question,
# "Press enter to confirm or esc to cancel", a numbered menu with its marker (›). The same words as Claude Code's:
# the default of the prompt_pattern setting was one pattern for both.
BLOCKED = [r"Do you (want to|trust)", r"Would you like to", r"[Ee]nter to confirm", r"[❯›]\s*\d+\.\s"]

DETECT = {
    "process": "codex",
    "idle": r"^\s*›(\s|$)",  # the marker of its input box
    "working": r"(?i)\besc to interrupt\b",
    "blocked": BLOCKED,
    "tell_instead": r"(?i)\bno\b.*\btell\b",  # "No, and tell Codex what to do differently"
    "image_paste": None,  # not read yet: an image path is pasted alone, as for every agent
}

# Its native message command (the default of the native_message setting): codex queue adds a message to a thread.
INPUT = {"native": ["codex", "queue", "--thread", "{address}", "--message", "{text}"]}


# ---------------------------------------------------------------------------------------------------------------
# Chat log (#5182). Codex writes ~/.codex/sessions/<y>/<m>/<d>/rollout-<time>-<thread id>.jsonl (or $CODEX_HOME),
# which it compresses to .jsonl.zst; the thread id is the agent's native_address.
# ---------------------------------------------------------------------------------------------------------------

import json as _json  # noqa: E402



def _s():
    """river.sessions, imported when first used: core imports the adapters while it loads, before sessions can."""
    from .. import sessions
    return sessions

CHAT = {"compressed": True, "file_tools": [], "ref": "address", "cache_write": False}


def chat_root():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"


def chat_path(session_ref, folder=None, root=None):
    """The newest session log of the thread id, or None. A .zst log counts only when Python can read it (3.14)."""
    tid = session_ref
    if not tid or not all(c.isalnum() or c == "-" for c in tid):
        return None
    base = Path(root or chat_root())
    found = list(base.glob(f"*/*/*/rollout-*-{tid}.jsonl"))
    if _s()._zstd():
        found += list(base.glob(f"*/*/*/rollout-*-{tid}.jsonl.zst"))
    return _s().newest(found)


_SKIP = ("# AGENTS.md", "<environment_context>", "<user_instructions>", "<")


def parse_chat_line(d):
    """The page lines of one Codex session log line, and its turn state."""
    p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
    at, t = d.get("timestamp"), p.get("type")
    if d.get("type") == "compacted":  # Codex's compaction (#5120): its summary is the payload's message
        out = [{"at": at, "role": "divider", "kind": "compacted", "text": "Compacted"}]
        if isinstance(p.get("message"), str) and p["message"].strip():
            out.append({"at": at, "role": "summary", "text": _s()._clip(p["message"].strip(), _s().TEXT_MAX)})
        return out, None
    if d.get("type") == "event_msg":
        return [], {"task_started": "working", "task_complete": "idle", "turn_aborted": "idle"}.get(t)
    if d.get("type") != "response_item":
        return [], None
    if t == "message" and p.get("role") in ("user", "assistant"):
        text = _s()._parts_text(p.get("content"), ("input_text", "output_text", "text")).strip()
        if not text or (p["role"] == "user" and text.startswith(_SKIP)):
            return [], None
        if p["role"] == "user":
            return [_s()._user_turn(at, text, sum(1 for x in p.get("content") or [] if isinstance(x, dict)
                                                and x.get("type") in ("input_image", "image")))], None
        return [{"at": at, "role": p["role"], "text": _s()._clip(text, _s().TEXT_MAX)}], None
    if t in ("function_call", "custom_tool_call", "local_shell_call"):
        inp = p.get("arguments") if t == "function_call" else p.get("input") if t == "custom_tool_call" else p.get("action")
        return [{"at": at, "role": "tool", "text": _s()._tool_line(p.get("name") or "shell", inp), "id": p.get("call_id"),
                 "plain": _s().plain_tool(p.get("name") or "shell", inp), "detail": _s()._detail(inp)}], None
    if t in ("function_call_output", "custom_tool_call_output", "local_shell_call_output") and p.get("call_id"):
        return [{"at": at, "role": "result", "for": p["call_id"],
                 "text": _s()._clip(_s()._result_text(p.get("output")), _s().DETAIL_MAX)}], None
    return [], None


def parse_chat(lines):
    return _s().parse_lines(parse_chat_line, lines)


def chat_settings(lines):
    """The settings the log shows last: a turn_context at each turn."""
    got = {}
    for line in lines:
        if '"turn_context"' not in line:
            continue
        try:
            p = _json.loads(line).get("payload") or {}
        except ValueError:
            continue
        for k, v in (("model", p.get("model")), ("effort", p.get("effort")), ("approval", p.get("approval_policy")),
                     ("sandbox", (p.get("sandbox_policy") or {}).get("type") if isinstance(p.get("sandbox_policy"), dict) else None)):
            if v:
                got[k] = v
    return got


def chat_cache_line(raw, st):
    """Fold one log line (bytes) into the prompt cache state st (#5023). A Codex log says how much of the input was
    cached, not for how long: the cache_ttl setting then. input_tokens holds the cached part."""
    if b'"token_count"' not in raw and b'"turn_context"' not in raw:
        return
    try:
        d = _json.loads(raw)
    except ValueError:
        return
    if not isinstance(d, dict) or not isinstance(d.get("timestamp"), str) or d.get("isSidechain"):
        return
    at = d["timestamp"]
    p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
    if d.get("type") == "turn_context":
        st["model"] = p.get("model") or st["model"]
    elif p.get("type") == "token_count" and isinstance((p.get("info") or {}).get("last_token_usage"), dict):
        u = p["info"]["last_token_usage"]
        st["last"] = {"start": at, "at": at, "context": (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0)}


def chat_texts(raw):
    """The texts of one log line (bytes) for the Artifacts tab: the text of each message and the input of each tool
    call. Tool results are left out."""
    if b'"type":"function_call_output"' in raw:
        return []
    try:
        d = _json.loads(raw)
    except ValueError:
        return []
    if not isinstance(d, dict):
        return []
    p = d.get("payload") if isinstance(d.get("payload"), dict) else None
    if not p or d.get("type") != "response_item":
        return []
    at = d.get("timestamp")
    if p.get("type") == "message":
        return [(_s()._parts_text(p.get("content"), ("input_text", "output_text", "text")), at, "named")]
    if p.get("type") in ("function_call", "custom_tool_call"):
        return [(str(p.get("arguments") or p.get("input") or ""), at, "named")]
    return []


# ---------------------------------------------------------------------------------------------------------------
# Settings (#5169): the launch profile options (each also the config key codex_<key>), and the Settings dialog of a
# running session. build_command reads them by key.
# ---------------------------------------------------------------------------------------------------------------

DESCRIBE["model_live"] = None
DESCRIBE["model_read"] = r"(?:^|\s)-m (\S+)"
EFFORT["label"] = "Reasoning effort"

SETTINGS = [
    {"key": "sandbox", "label": "Sandbox", "kind": "choice", "default": "",
     "choices": ["read-only", "workspace-write", "danger-full-access"],
     "text": "sandbox (--sandbox); empty: Codex's own setting. MaximizePM adds --add-dir for the queue folder",
     "flag": ["--sandbox", "{value}"], "read": r"--sandbox (\S+)", "session": True},
    {"key": "approval", "label": "Approval", "kind": "choice", "default": "",
     "choices": ["on-request", "never", "untrusted"],
     "text": "when Codex asks before it runs a command (--ask-for-approval); empty: Codex's own setting",
     "flag": ["--ask-for-approval", "{value}"], "read": r"--ask-for-approval (\S+)", "session": True},
    # Codex takes full ids, and they change with each OpenAI release: update this list then.
    {"key": "model_ids", "label": "Model ids", "kind": "text",
     "default": "luna=gpt-6-luna, terra=gpt-5.6-terra, sol=gpt-6.1-sol, astra=gpt-6-astra",
     "text": "the id Codex gets (codex -m) for a model_ladder name (name=id, ...); a name with no entry goes as it is"},
    {"key": "args", "label": "Arguments", "kind": "text", "default": "", "text": "more arguments, put before the prompt"},
    {"key": "prompt", "label": "Prompt", "kind": "text", "default": "run maxpm go in this folder and follow the briefing",
     "text": "the first prompt (Codex does not read CLAUDE.md)"},
    # Flags of one session only: the Settings dialog adds or takes them out at a restart; no config key.
    {"key": "approve_for_me", "label": "Approve for me (auto)", "kind": "toggle", "default": "off",
     "text": "Codex approves its own requests (--approve-for-me)", "flag": ["--approve-for-me"], "config": False,
     "session": True},
    {"key": "no_daemon", "label": "No shared background server", "kind": "toggle", "default": "off",
     "text": "Codex runs without its shared background server (--no-daemon; MaximizePM adds it when the installed "
             "Codex has it)", "flag": ["--no-daemon"], "config": False, "session": True},
]


# ---------------------------------------------------------------------------------------------------------------
# 10. Commands (#5186): the "/" list of the Sessions message box
# ---------------------------------------------------------------------------------------------------------------

# The built-in slash commands that a person types most, with the descriptions of Codex 0.159.1 (read from its
# program).
COMMANDS = [
    {"name": "model", "description": "choose what model and reasoning effort to use", "hint": ""},
    {"name": "approvals", "description": "choose what Codex is allowed to do", "hint": ""},
    {"name": "review", "description": "review my current changes and find issues", "hint": ""},
    {"name": "new", "description": "start a new chat during a conversation", "hint": ""},
    {"name": "resume", "description": "resume a saved chat", "hint": ""},
    {"name": "fork", "description": "fork the current chat", "hint": ""},
    {"name": "rename", "description": "rename the current thread", "hint": "<name>"},
    {"name": "init", "description": "create an AGENTS.md file with instructions for Codex", "hint": ""},
    {"name": "compact", "description": "summarize conversation to prevent hitting the context limit", "hint": ""},
    {"name": "recap", "description": "summarize the current conversation now", "hint": ""},
    {"name": "plan", "description": "switch to Plan mode", "hint": ""},
    {"name": "goal", "description": "set or view the goal for a long-running task", "hint": "[goal]"},
    {"name": "side", "description": "start a side conversation in an ephemeral fork", "hint": "[question]"},
    {"name": "copy", "description": "copy the last response or part of it", "hint": ""},
    {"name": "export", "description": "export the conversation as markdown", "hint": ""},
    {"name": "diff", "description": "show git diff (including untracked files)", "hint": ""},
    {"name": "mention", "description": "mention a file", "hint": "<file>"},
    {"name": "status", "description": "show current session configuration and token usage", "hint": ""},
    {"name": "usage", "description": "view account usage or use a usage limit reset", "hint": ""},
    {"name": "skills", "description": "use skills to improve how Codex performs specific tasks", "hint": ""},
    {"name": "hooks", "description": "view and manage lifecycle hooks", "hint": ""},
    {"name": "mcp", "description": "list configured MCP tools; use /mcp verbose for details", "hint": "[verbose]"},
    {"name": "apps", "description": "manage apps", "hint": ""},
    {"name": "plugins", "description": "browse plugins", "hint": ""},
    {"name": "experimental", "description": "toggle experimental features", "hint": ""},
    {"name": "statusline", "description": "configure which items appear in the status line", "hint": ""},
    {"name": "theme", "description": "choose a syntax highlighting theme", "hint": ""},
    {"name": "vim", "description": "toggle Vim mode for the composer", "hint": ""},
    {"name": "voice", "description": "start or stop voice; use /voice settings to choose a voice", "hint": ""},
    {"name": "cd", "description": "change the current working directory", "hint": "<path>"},
    {"name": "pwd", "description": "show the current working directory", "hint": ""},
    {"name": "ps", "description": "list background terminals", "hint": ""},
    {"name": "clear", "description": "clear the terminal and start a new chat", "hint": ""},
    {"name": "feedback", "description": "send logs to maintainers", "hint": ""},
    {"name": "logout", "description": "log out of Codex", "hint": ""},
    {"name": "quit", "description": "exit Codex", "hint": ""},
]


def read_commands(folder):
    """The user's custom prompts (~/.codex/prompts/*.md, or $CODEX_HOME/prompts), which Codex runs as
    /prompts:<name>. Codex reads no project folder for them, so folder does not change the list."""
    import os
    from pathlib import Path
    from . import slash
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    return slash.md_commands(home / "prompts", "prompts:", recursive=False)


# ---------------------------------------------------------------------------------------------------------------
# 11. Setup (#5184)
# ---------------------------------------------------------------------------------------------------------------

SETUP = {
    "instructions": "AGENTS.md",
    "import": None,
    "skills_dir": None,
    # Voice (#4626): checked in Codex 0.159.1; it takes no audio from outside, so the page dictates (#5107).
    "voice": ("/voice in its terminal: a spoken conversation (OpenAI realtime, the feature realtime_conversation) with "
              "the microphone and speakers of the computer it runs on; it takes no audio from outside"),
    "open_link": None,
    "open_title": None,
    "no_link": "A Codex session has no web link, and this one runs in no tmux pane here",
}


def _toml_block(entry):
    """The [mcp_servers.maxpm] tables for an entry. A JSON string is also a valid TOML basic string."""
    import json
    lines = ["[mcp_servers.maxpm]", f"command = {json.dumps(entry['command'])}",
             "args = [" + ", ".join(json.dumps(a) for a in entry["args"]) + "]", "", "[mcp_servers.maxpm.env]"]
    lines += [f"{k} = {json.dumps(v)}" for k, v in entry["env"].items()]
    return "\n".join(lines) + "\n"


def _toml_without(text):
    """The config text without its [mcp_servers.maxpm] table and subtables; every other line stays."""
    import re
    out, skip = [], False
    for line in text.splitlines(keepends=True):
        m = re.match(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$", line)
        if m:
            name = m.group(1).replace('"', "").replace("'", "").replace(" ", "")
            skip = name == "mcp_servers.maxpm" or name.startswith("mcp_servers.maxpm.")
        if not skip:
            out.append(line)
    return "".join(out)


def _toml_load(text, f):
    from ..core import RiverError
    try:
        import tomllib
    except ImportError:  # Python before 3.11: MaximizePM cannot check the file, so it changes it only by lines
        return None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise RiverError(f"{f} is not valid TOML ({e}); fix it first, MaximizePM does not overwrite it")


def write_mcp_config(path=None, remove=False):
    """Add (or remove) MaximizePM in ~/.codex/config.toml (or $CODEX_HOME), which the ChatGPT desktop app shares with
    the Codex CLI (maxpm setup-agent --chatgpt-desktop). Only the [mcp_servers.maxpm] tables change; the old file is
    kept as config.toml.bak."""
    import os
    from pathlib import Path
    from . import setup_entry
    from ..core import RiverError
    f = Path(path).expanduser() if path else Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser() / "config.toml"
    old = f.read_text() if f.exists() else ""
    before = _toml_load(old, f)
    had = before is not None and "maxpm" in (before.get("mcp_servers") or {}) or "[mcp_servers.maxpm]" in old
    base = _toml_without(old)
    if remove:
        if not had:
            return f"{f} has no MaximizePM entry; nothing changed"
        new = base
    else:
        entry = setup_entry()
        if before is not None and (before.get("mcp_servers") or {}).get("maxpm") == entry:
            return f"MaximizePM is already in {f}; nothing changed"
        new = base.rstrip("\n") + ("\n\n" if base.strip() else "") + _toml_block(entry)
    after = _toml_load(new, f)
    if after is not None:
        others = lambda d: {k: v for k, v in (d or {}).items() if k != "mcp_servers"}
        servers = lambda d: {k: v for k, v in ((d or {}).get("mcp_servers") or {}).items() if k != "maxpm"}
        if others(after) != others(before) or servers(after) != servers(before) or (
                not remove and after["mcp_servers"]["maxpm"] != entry):
            raise RiverError(f"MaximizePM could not add its entry to {f} without changing other settings; add it by hand: "
                             f"codex mcp add maxpm --env MAXPM_CHAT=1 -- maxpm mcp")
    if old:
        f.with_name(f.name + ".bak").write_text(old)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(new)
    if remove:
        return f"took MaximizePM out of {f} (the old file: {f.name}.bak). Restart the ChatGPT desktop app."
    return (f"added MaximizePM to {f}" + (f" (the old file: {f.name}.bak)" if old else "") + ".\n"
            "Restart the ChatGPT desktop app, then use Work or Codex mode (plain Chat mode does not reach servers "
            "on this computer). Ask it, for example: \"plan my next work with MaximizePM\" or \"what waits on me in MaximizePM?\". "
            "Codex CLI sessions see MaximizePM too; in a project folder they work as usual.")


# ---------------------------------------------------------------------------------------------------------------
# The installed Codex: its model cache and its flags (moved from core.py, #5185)
# ---------------------------------------------------------------------------------------------------------------

def codex_models():
    """{id: [effort levels]} from the Codex model cache (~/.codex/models_cache.json), or None without one."""
    import json
    from pathlib import Path
    path = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "models_cache.json"
    try:
        data = json.loads(path.read_text())
        return {m["slug"]: [x["effort"] for x in m.get("supported_reasoning_levels") or [] if x.get("effort")]
                for m in data.get("models") or [] if m.get("slug")}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


CODEX_FLAGS = None  # the flags `codex --help` lists, read once (tests set it); moved from core (#5185)


def codex_flags():
    """The long flags of the installed Codex CLI (an empty set when it does not run)."""
    global CODEX_FLAGS
    if CODEX_FLAGS is None:
        import re
        import subprocess
        try:
            out = subprocess.run(["codex", "--help"], capture_output=True, text=True, timeout=15).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        CODEX_FLAGS = set(re.findall(r"(?<![\w-])(--[a-z][\w-]*)", out))
    return CODEX_FLAGS
