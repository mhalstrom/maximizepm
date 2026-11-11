"""Agent adapters (docs/agent-adapters.md): one module for each coding agent CLI that MaximizePM runs in a tmux pane.

An adapter is a plain Python module, river/agents/<id>.py, whose module-level names below make up its parts. Data is
plain dicts and lists (raw strings for regexes); a function only where data cannot say it. A module in
~/.maximizepm/agents/<id>.py (the folder of the queue, core.river_dir(), then agents/) replaces the built-in module
of the same id, or adds a new agent, with no MaximizePM release.

Each part is optional unless it is marked required; the page shows only what an adapter supports.

 1. Describe (required): DESCRIBE, and optionally read_models().
 2. Settings: SETTINGS, a list of setting specs.
 3. Effort: EFFORT, and optionally read_effort_levels(model) and read_effort(entries).
 4. Launch (required): build_command(), LAUNCH (capabilities), and optionally parse_command().
 5. Session identity: IDENTITY.
 6. Chat log: chat_path() and parse_chat() together, CHAT, and optionally chat_chain(), chat_title(), chat_cwd(),
    chat_usage(), chat_settings().
 7. State: STATE, and DETECT (screen rules) or install_hooks() (a hook or plugin that calls maxpm report).
 8. Input: INPUT.
 9. Resume: resume_command().
10. Commands: COMMANDS, and optionally read_commands(folder).
11. Setup: SETUP, and optionally write_mcp_config().

The loader refuses a module without a valid DESCRIBE. A module without build_command loads (the built-in claude and
codex modules have only their describe part until #5181), but no start path can start it: Adapter.build_command
raises RiverError, and Adapter.missing() names the required parts it lacks.

No call site uses the adapters yet (#5167 lays the foundation; #5181-#5185 move each call site here).
"""

import importlib.util
import os
import re
from pathlib import Path

from ..core import RiverError, river_dir

# ---------------------------------------------------------------------------------------------------------------
# The contract: names and shapes
# ---------------------------------------------------------------------------------------------------------------

# The three states an agent reports (maxpm report --state). Gone is no state: it comes from maxpm release, or from the
# end of the agent's process.
STATES = ("working", "idle", "blocked")

# The effort levels of MaximizePM (the effort_levels setting); EFFORT["map"] maps each one to the agent's own name.
MAXPM_EFFORTS = ("low", "medium", "high", "xhigh", "max")

# The model families whose ladders MaximizePM knows (model_ladder). An item's --min-model and --max-model apply only
# to them. "multi": the agent takes a free "provider/model" text (OpenCode, Pi, Goose), and no limit applies.
FAMILIES = ("claude", "openai", "multi")

# 1. Describe (required). DESCRIBE = {
#      "id":            the module's file name without .py ("claude"); the key of the registry,
#      "label":         the name people read ("Claude Code"),
#      "exe":           the program that starts it ("claude"),
#      "aliases":       other names that mean this agent, in settings and in --agent ("claude-code"),
#      "family":        one of FAMILIES,
#      "models":        the model names MaximizePM offers for it (names of model_ladder, or "provider/model" texts),
#      "default_model": the model when nothing names one; "" lets the agent pick its own,
#      "model_ids":     {model name: the id the CLI takes} for a name that the CLI does not take as it is,
#      "prefix":        the prefix of its config keys: a setting spec "args" is the config key "<prefix>args",
#    }
# read_models() -> [model name or CLI id] | None: an optional reader of the models the CLI knows now (Codex:
# ~/.codex/models_cache.json); None when it cannot read them, and DESCRIBE["models"] stands.
DESCRIBE_KEYS = {"id": str, "label": str, "exe": str, "aliases": (list, tuple), "family": str,
                 "models": (list, tuple), "default_model": str, "model_ids": dict, "prefix": str}
DESCRIBE_REQUIRED = ("id", "label", "exe", "family")

# 2. Settings. SETTINGS = [{
#      "key":     the option's name; its config key is DESCRIBE["prefix"] + key (claude_permission_mode),
#      "label":   the name in the Setup section, the launch dialog and the Settings dialog,
#      "kind":    one of SETTING_KINDS,
#      "choices": the values of a "choice" ([] for another kind),
#      "default": the built-in value, a string ("on"/"off" for a toggle; "" lets the agent use its own setting),
#      "text":    one line of help,
#      "flag":    how the value goes on the command line: argv words with {value} ("--permission-mode", "{value}"),
#                 for a toggle the words when it is on; [] when build_command uses it in its own way,
#      "live":    the text typed into a running session to change it ("/model {value}"); None: a restart applies it,
#      "read":    a regex whose group 1 reads the value back from the session's command line; None if it cannot,
#    }, ...]
SETTING_KINDS = ("toggle", "choice", "text")
SETTING_KEYS = {"key": str, "label": str, "kind": str, "choices": (list, tuple), "default": str, "text": str,
                "flag": (list, tuple), "live": (str, type(None)), "read": (str, type(None))}

# 3. Effort. EFFORT = None (the agent has no effort; the page hides it) or {
#      "levels": the agent's own levels, or None when read_effort_levels(model) reads them from the CLI per model,
#      "map":    {MaximizePM level: the agent's level}; a level with no entry goes as it is,
#      "flag":   argv words with {value} ("--effort", "{value}"),
#      "live":   the text typed into a running session ("/effort {value}"), or None,
#      "read":   a regex whose group 1 reads the effort back from a chat log line, or None (or read_effort(entries)),
#    }
EFFORT_KEYS = {"levels": (list, tuple, type(None)), "map": dict, "flag": (list, tuple),
               "live": (str, type(None)), "read": (str, type(None))}

# 4. Launch (required). build_command(name, folder, model, effort, settings, prompt) -> argv (a list of str):
#    name: the session's MaximizePM name; folder: where it runs; model and effort: MaximizePM names (effort may be
#    None); settings: {setting key: value} after the most specific scope wins; prompt: the first prompt.
# LAUNCH = {capability: bool}, the parts of a start that the agent takes. parse_command(argv) -> a profile
# {"model", "effort", "settings": {key: value}, "prompt"} read back from an old command, or None when argv does
# not start this agent.
LAUNCH_CAPABILITIES = ("session_name", "system_prompt_file", "hooks_file", "extra_dirs", "fork")

# 5. Session identity. IDENTITY = {
#      "source":     how MaximizePM learns the session's id after the start: one of IDENTITY_SOURCES,
#      "env":        the variable that holds it ("env" source), or None,
#      "log_glob":   the glob of the new log file in a known folder ("log" source; ~ expands), or None,
#      "pane":       a regex whose group 1 is the id in the pane's text ("pane" source), or None,
#      "url":        its web link with {id} ("https://.../{id}"), or None,
#      "inside_env": the variables that tell that a command runs inside this agent (CLAUDECODE, CODEX_THREAD_ID);
#                    this replaces core.agent_type_from_env,
#    }
IDENTITY_SOURCES = ("hook", "env", "log", "pane")
IDENTITY_KEYS = {"source": str, "env": (str, type(None)), "log_glob": (str, type(None)), "pane": (str, type(None)),
                 "url": (str, type(None)), "inside_env": (list, tuple)}

# 6. Chat log. chat_path(session_ref, folder) -> the log file of a session (a path), or None. parse_chat(lines) ->
# (entries, turn): the shared entries of a list of log lines (each a str), and the turn state at the end ("working",
# "idle", or None when the lines do not say). CHAT = {"compressed": bool (the file is gzip), "file_tools": [the tool
# names that write a file, for the Artifacts tab]}. Optional readers, each over the lines of one file:
# chat_chain(path) -> [paths] across resumes, oldest first; chat_title(lines) -> str or None; chat_cwd(lines) ->
# str or None; chat_usage(lines) -> {"input", "output", "cache_read", "cache_write"} token counts; chat_settings(lines)
# -> {setting key: value} as the session runs now.
# An entry is a dict with "at" (ISO time or None), "role" (one of ENTRY_ROLES) and "text", and by role:
#   user: "images" (a list of paths or data URLs) when it has some; tool: "id" (the call id), "name", "detail";
#   result: "for" (the call id), "error" (bool); divider: "kind" ("compacted", "resumed").
ENTRY_ROLES = ("assistant", "user", "tool", "result", "divider", "summary", "event")
TURNS = ("working", "idle")
CHAT_KEYS = {"compressed": bool, "file_tools": (list, tuple)}

# 7. State. STATE = {
#      "state":      the one source of the state: "hook" (install_hooks puts a hook or plugin in place that calls
#                    maxpm report) or "detect" (maxpm's pane watcher applies DETECT and calls maxpm report),
#      "session_id": the one source of the session id: "hook" (the report carries --session-id) or IDENTITY,
#      "background": True when the agent waits in the foreground for its background commands,
#    }
# No chain of fallbacks: each signal has one declared source.
# install_hooks(folder, report) -> [argv words to add to the start] | None: puts the hooks in place; `report` is the
# maxpm report command (a list of argv words) that each hook runs with its --state.
# DETECT = {
#      "process":      the process name of the agent in the pane,
#      "idle":         a regex of the pane when the agent waits at its prompt,
#      "working":      a regex of the pane while it runs a turn ("esc to interrupt"),
#      "blocked":      [regexes], one for each screen that waits for a person (a permission menu, a question, trust),
#      "tell_instead": the text of the menu option that lets the person type an answer instead, or None,
#      "image_paste":  how a pasted image shows in its prompt, with {n} ("[Image #{n}]"), or None,
#    }
STATE_SOURCES = ("hook", "detect")
STATE_KEYS = {"state": str, "session_id": str, "background": bool}
DETECT_KEYS = {"process": str, "idle": str, "working": str, "blocked": (list, tuple),
               "tell_instead": (str, type(None)), "image_paste": (str, type(None))}

# 8. Input. tmux paste is the same for every agent; the image paste rule and the "tell it instead" text are in
# DETECT. INPUT = {"native": argv words with {address} and {text} for a native message command (Codex:
# codex queue --thread {address} {text}), or None}.
INPUT_KEYS = {"native": (list, tuple, type(None))}

# 9. Resume. resume_command(session_ref, model, effort, settings) -> argv, or None when it cannot resume that session.

# 10. Commands. COMMANDS = [{"name": "compact" (no "/"), "description": str, "hint": the argument hint or ""}, ...]:
# the built-in slash commands. read_commands(folder) -> the same shape: its custom commands for a session that runs in
# folder (Claude: ~/.claude/commands, .claude/commands, the skills, plugin commands; Codex: ~/.codex/prompts).
COMMAND_KEYS = {"name": str, "description": str, "hint": str}

# 11. Setup. SETUP = {
#      "instructions": the instruction file it reads in a project folder (CLAUDE.md, AGENTS.md, GEMINI.md),
#      "skills_dir":   the folder of its skills (~ expands), or None,
#      "voice":        what its own voice mode does, one sentence, or None,
#      "open_link":    how a person opens a link it prints, one sentence, or None,
#    }
# write_mcp_config(url) -> [the files it changed]: points the agent at MaximizePM's MCP server.
SETUP_KEYS = {"instructions": str, "skills_dir": (str, type(None)), "voice": (str, type(None)),
              "open_link": (str, type(None))}

# Each part: the module names that make it up, and the dict whose keys check (name, shape).
PARTS = {
    "describe": {"data": ("DESCRIBE", DESCRIBE_KEYS), "functions": ("read_models",), "required": True},
    "settings": {"data": ("SETTINGS", SETTING_KEYS), "functions": ()},
    "effort": {"data": ("EFFORT", EFFORT_KEYS), "functions": ("read_effort_levels", "read_effort")},
    "launch": {"data": ("LAUNCH", None), "functions": ("build_command", "parse_command"), "required": True},
    "identity": {"data": ("IDENTITY", IDENTITY_KEYS), "functions": ()},
    "chat": {"data": ("CHAT", CHAT_KEYS), "functions": ("chat_path", "parse_chat", "chat_chain", "chat_title",
                                                        "chat_cwd", "chat_usage", "chat_settings")},
    "state": {"data": ("STATE", STATE_KEYS), "functions": ("install_hooks",)},
    "detect": {"data": ("DETECT", DETECT_KEYS), "functions": ()},
    "input": {"data": ("INPUT", INPUT_KEYS), "functions": ()},
    "resume": {"data": None, "functions": ("resume_command",)},
    "commands": {"data": ("COMMANDS", COMMAND_KEYS), "functions": ("read_commands",)},
    "setup": {"data": ("SETUP", SETUP_KEYS), "functions": ("write_mcp_config",)},
}

BUILTIN = ("claude", "codex")
_ID = re.compile(r"[a-z][a-z0-9_]*\Z")


# ---------------------------------------------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------------------------------------------

def _shape(where, d, keys, required=()):
    """The problems of one dict against its keys: an unknown key, a missing required key, a value of a wrong type."""
    if not isinstance(d, dict):
        return [f"{where} is a {type(d).__name__}, not a dict"]
    out = [f"{where} has no {k!r}" for k in required if k not in d]
    for k, v in d.items():
        if k not in keys:
            out.append(f"{where} has an unknown key {k!r} (known: {', '.join(keys)})")
        elif not isinstance(v, keys[k]):
            out.append(f"{where}[{k!r}] is a {type(v).__name__}")
    return out


def _regex(where, text):
    try:
        re.compile(text)
    except re.error as e:
        return [f"{where} is no valid regex: {e}"]
    return []


def check(module, mid=None):
    """The problems of an adapter module against the contract above, as plain sentences; [] when it is valid. A
    missing required part other than DESCRIBE is no problem here: Adapter.missing() names it."""
    out = []
    d = getattr(module, "DESCRIBE", None)
    if d is None:
        return ["it has no DESCRIBE (the describe part is required)"]
    out += _shape("DESCRIBE", d, DESCRIBE_KEYS, DESCRIBE_REQUIRED)
    if isinstance(d, dict):
        if mid and d.get("id") != mid:
            out.append(f"DESCRIBE['id'] is {d.get('id')!r}; it must be the file name, {mid!r}")
        if d.get("family") is not None and d.get("family") not in FAMILIES:
            out.append(f"DESCRIBE['family'] is {d.get('family')!r}, not one of {', '.join(FAMILIES)}")
    for i, s in enumerate(getattr(module, "SETTINGS", None) or ()):
        out += _shape(f"SETTINGS[{i}]", s, SETTING_KEYS, ("key", "kind"))
        if isinstance(s, dict):
            if s.get("kind") not in SETTING_KINDS:
                out.append(f"SETTINGS[{i}]['kind'] is {s.get('kind')!r}, not one of {', '.join(SETTING_KINDS)}")
            if s.get("read"):
                out += _regex(f"SETTINGS[{i}]['read']", s["read"])
    e = getattr(module, "EFFORT", None)
    if e is not None:
        out += _shape("EFFORT", e, EFFORT_KEYS)
        if isinstance(e, dict):
            out += [f"EFFORT['map'] maps {k!r}, which is no MaximizePM effort level"
                    for k in e.get("map") or {} if k not in MAXPM_EFFORTS]
            if e.get("read"):
                out += _regex("EFFORT['read']", e["read"])
    launch = getattr(module, "LAUNCH", None)
    if launch is not None:
        if not isinstance(launch, dict):
            out.append("LAUNCH is no dict")
        else:
            out += [f"LAUNCH has an unknown capability {k!r} (known: {', '.join(LAUNCH_CAPABILITIES)})"
                    for k in launch if k not in LAUNCH_CAPABILITIES]
    ident = getattr(module, "IDENTITY", None)
    if ident is not None:
        out += _shape("IDENTITY", ident, IDENTITY_KEYS, ("source",))
        if isinstance(ident, dict) and ident.get("source") not in IDENTITY_SOURCES:
            out.append(f"IDENTITY['source'] is {ident.get('source')!r}, not one of {', '.join(IDENTITY_SOURCES)}")
    if (getattr(module, "chat_path", None) is None) != (getattr(module, "parse_chat", None) is None):
        out.append("the chat part needs chat_path and parse_chat together")
    st = getattr(module, "STATE", None)
    if st is not None:
        out += _shape("STATE", st, STATE_KEYS, ("state",))
        if isinstance(st, dict):
            if st.get("state") not in STATE_SOURCES:
                out.append(f"STATE['state'] is {st.get('state')!r}, not one of {', '.join(STATE_SOURCES)}")
            elif st["state"] == "hook" and not callable(getattr(module, "install_hooks", None)):
                out.append("STATE['state'] is 'hook', but the module has no install_hooks")
            elif st["state"] == "detect" and getattr(module, "DETECT", None) is None:
                out.append("STATE['state'] is 'detect', but the module has no DETECT")
            if st.get("session_id") not in (None, "hook", "identity"):
                out.append(f"STATE['session_id'] is {st.get('session_id')!r}, not 'hook' or 'identity'")
    det = getattr(module, "DETECT", None)
    if det is not None:
        out += _shape("DETECT", det, DETECT_KEYS, ("idle", "working", "blocked"))
        if isinstance(det, dict):
            for k in ("idle", "working"):
                if isinstance(det.get(k), str):
                    out += _regex(f"DETECT[{k!r}]", det[k])
            for i, p in enumerate(det.get("blocked") or ()):
                out += _regex(f"DETECT['blocked'][{i}]", p) if isinstance(p, str) else [f"DETECT['blocked'][{i}] is no str"]
    if getattr(module, "INPUT", None) is not None:
        out += _shape("INPUT", module.INPUT, INPUT_KEYS)
    for i, c in enumerate(getattr(module, "COMMANDS", None) or ()):
        out += _shape(f"COMMANDS[{i}]", c, COMMAND_KEYS, ("name",))
    if getattr(module, "SETUP", None) is not None:
        out += _shape("SETUP", module.SETUP, SETUP_KEYS, ("instructions",))
    for part in PARTS.values():
        for f in part["functions"]:
            v = getattr(module, f, None)
            if v is not None and not callable(v):
                out.append(f"{f} is no function")
    return out


# ---------------------------------------------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------------------------------------------

class Adapter:
    """One loaded adapter module, with a default for each optional part. The data parts are attributes (describe,
    settings, effort, launch, identity, chat, state, detect, input, commands, setup); the function parts are methods
    that raise RiverError (or return None, where the contract says so) when the module lacks them."""

    def __init__(self, module, source):
        self.module = module
        self.source = source  # "builtin", or the path of the user's module
        m = module
        self.describe = {"aliases": [], "models": [], "default_model": "", "model_ids": {}, "prefix": "",
                         **m.DESCRIBE}
        self.id = self.describe["id"]
        self.label = self.describe["label"]
        self.settings = list(getattr(m, "SETTINGS", None) or ())
        self.effort = getattr(m, "EFFORT", None)
        self.launch = dict(getattr(m, "LAUNCH", None) or {})
        self.identity = getattr(m, "IDENTITY", None)
        self.chat = getattr(m, "CHAT", None)
        self.state = getattr(m, "STATE", None)
        self.detect = getattr(m, "DETECT", None)
        self.input = dict(getattr(m, "INPUT", None) or {})
        self.commands = list(getattr(m, "COMMANDS", None) or ())
        self.setup = getattr(m, "SETUP", None)

    def __repr__(self):
        return f"<Adapter {self.id} ({self.source})>"

    def names(self):
        """Its id and aliases, lower case: each one names it in a setting or --agent."""
        return [self.id.lower()] + [a.lower() for a in self.describe["aliases"]]

    def _fn(self, name):
        return getattr(self.module, name, None)

    def supports(self, part):
        """Whether the module gives part (a key of PARTS)."""
        spec = PARTS[part]
        if spec["data"] and getattr(self.module, spec["data"][0], None) is not None:
            return True
        if part == "chat":
            return self._fn("parse_chat") is not None
        if part == "launch":
            return self._fn("build_command") is not None
        return any(self._fn(f) is not None for f in spec["functions"])

    def missing(self):
        """The required parts the module lacks: a start path refuses such an agent."""
        return [p for p, spec in PARTS.items() if spec.get("required") and not self.supports(p)]

    def config_key(self, key):
        """The config key of setting spec `key`: DESCRIBE['prefix'] + key."""
        return self.describe["prefix"] + key

    def effort_name(self, level):
        """The agent's name for MaximizePM effort `level`, or None when the agent has no effort."""
        if not self.effort or not level:
            return None
        return (self.effort.get("map") or {}).get(level, level)

    def read_models(self):
        f = self._fn("read_models")
        got = f() if f else None
        return list(got) if got else list(self.describe["models"])

    def build_command(self, name, folder, model, effort, settings, prompt):
        f = self._fn("build_command")
        if f is None:
            raise RiverError(f"agent {self.id} has no launch part (build_command), so MaximizePM cannot start it; "
                             f"add it to {self._file()}")
        return list(f(name, folder, model, effort, dict(settings or {}), prompt))

    def parse_command(self, argv):
        f = self._fn("parse_command")
        return f(list(argv)) if f else None

    def resume_command(self, session_ref, model=None, effort=None, settings=None):
        f = self._fn("resume_command")
        got = f(session_ref, model, effort, dict(settings or {})) if f else None
        return list(got) if got else None

    def chat_path(self, session_ref, folder=None):
        f = self._fn("chat_path")
        return f(session_ref, folder) if f else None

    def parse_chat(self, lines):
        f = self._fn("parse_chat")
        return f(list(lines)) if f else ([], None)

    def read_commands(self, folder):
        f = self._fn("read_commands")
        return list(self.commands) + (list(f(folder) or ()) if f else [])

    def install_hooks(self, folder, report):
        f = self._fn("install_hooks")
        return f(folder, list(report)) if f else None

    def inside(self, env):
        """Whether a command with environment `env` runs inside this agent (IDENTITY['inside_env'])."""
        return any(env.get(v) for v in (self.identity or {}).get("inside_env") or ())

    def _file(self):
        return self.source if self.source != "builtin" else f"river/agents/{self.id}.py"


# ---------------------------------------------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------------------------------------------

_cache = {"key": None, "adapters": {}, "errors": {}}


def override_dir():
    """The folder of the user's adapter modules: agents/ beside the queue (~/.maximizepm/agents by default)."""
    return Path(river_dir()) / "agents"


def _load_file(mid, path):
    """Import the user's module at path under its own name, so it never replaces the built-in module object."""
    spec = importlib.util.spec_from_file_location(f"maxpm_user_agent_{mid}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _builtin(mid):
    return importlib.import_module(f"{__name__}.{mid}")


def _key():
    d = override_dir()
    try:
        files = sorted((p.name, p.stat().st_mtime_ns) for p in d.glob("*.py")) if d.is_dir() else []
    except OSError:
        files = []
    return str(d), tuple(files)


def load(refresh=False):
    """{id: Adapter} of every agent: the built-in modules, then each user module in override_dir(), which replaces
    the built-in module of its id or adds an agent. A user module that does not import or does not pass check() is
    left out and named in errors(); the built-in module of that id, if any, stays. The result is cached until a
    file in the override folder changes."""
    key = _key()
    if not refresh and _cache["key"] == key:
        return dict(_cache["adapters"])
    adapters, errors = {}, {}
    for mid in BUILTIN:
        module = _builtin(mid)
        problems = check(module, mid)
        if problems:  # a defect of MaximizePM itself: the tests catch it
            raise RiverError(f"the built-in agent adapter {mid} is invalid: {'; '.join(problems)}")
        adapters[mid] = Adapter(module, "builtin")
    for name, _ in key[1]:
        mid, path = name[:-3], Path(key[0]) / name
        if not _ID.match(mid):
            errors[mid] = f"{path}: the file name must be an id (a-z, 0-9, _), as {mid!r} is not"
            continue
        try:
            module = _load_file(mid, path)
        except Exception as e:  # noqa: BLE001 - the user's code: any error leaves it out, and errors() names it
            errors[mid] = f"{path} does not load: {type(e).__name__}: {e}"
            continue
        problems = check(module, mid)
        if problems:
            errors[mid] = f"{path}: {'; '.join(problems)}"
            continue
        adapters[mid] = Adapter(module, str(path))
    seen = {}
    for a in adapters.values():
        for n in a.names():
            if n in seen and seen[n] != a.id:
                errors[a.id] = f"{a._file()}: the name {n!r} is also agent {seen[n]}'s; rename one of them"
            seen.setdefault(n, a.id)
    _cache.update(key=key, adapters=adapters, errors=errors)
    return dict(adapters)


def errors():
    """{id: why} of each user module that load() left out."""
    load()
    return dict(_cache["errors"])


def ids():
    """The ids of the registered agents, built-in ones first."""
    got = load()
    return [m for m in BUILTIN if m in got] + sorted(m for m in got if m not in BUILTIN)


def find(name):
    """The adapter that `name` names (its id or an alias, any case), or None."""
    n = (name or "").strip().lower()
    if not n:
        return None
    got = load()
    if n in got:
        return got[n]
    return next((a for a in got.values() if n in a.names()), None)


def get(name):
    """The adapter that `name` names; RiverError when there is none."""
    a = find(name)
    if a is None:
        bad = errors().get((name or "").strip().lower())
        raise RiverError(f"agent {name!r} is not registered" + (f" ({bad})" if bad else "") +
                         f"; the agents are {', '.join(ids())}, and a module in {override_dir()}/<id>.py adds one")
    return a


def from_env(env=None):
    """The adapter of the agent that a command runs inside (IDENTITY['inside_env']), or None. MAXPM_AGENT_TYPE names
    it by hand. Non-built-in adapters first, then codex before claude: a Codex started from a Claude Code shell keeps
    CLAUDECODE (core.agent_type_from_env reads it the same way until #5181)."""
    env = os.environ if env is None else env
    if env.get("MAXPM_AGENT_TYPE"):
        return find(env["MAXPM_AGENT_TYPE"])
    got = load()
    order = sorted(m for m in got if m not in BUILTIN) + [m for m in ("codex", "claude") if m in got]
    return next((got[m] for m in order if got[m].inside(env)), None)


__all__ = ["Adapter", "BUILTIN", "PARTS", "STATES", "MAXPM_EFFORTS", "FAMILIES", "check", "load", "errors", "ids",
           "find", "get", "from_env", "override_dir"]
