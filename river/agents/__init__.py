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
 6. Chat log: chat_path() and parse_chat() together, CHAT, and optionally parse_chat_line(), chat_chain(),
    chat_title(), chat_cwd(), chat_usage(), chat_requests(), chat_settings(), chat_cache_line(), chat_texts().
 7. State: STATE, and DETECT (screen rules) or install_hooks() (a hook or plugin that calls maxpm report).
 8. Input: INPUT.
 9. Resume: resume_command().
10. Commands: COMMANDS, and optionally read_commands(folder).
11. Setup: SETUP, and optionally write_mcp_config() and desktop_app().
12. Update: UPDATE (how to read its version, update it and test it; river/agentupdate.py).

The loader refuses a module without a valid DESCRIBE. A module without build_command loads (the built-in claude and
codex modules have only their describe part until #5181), but no start path can start it: Adapter.build_command
raises RiverError, and Adapter.missing() names the required parts it lacks.

No call site uses the adapters yet (#5167 lays the foundation; #5181-#5185 move each call site here).
"""

import importlib.util
import os
import re
from pathlib import Path

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
#      "model_live":    the text typed into a running session to change its model ("/model {value}"), or None: a
#                       restart applies it (the Settings dialog of a session, #5169),
#      "model_read":    a regex whose group 1 reads the model back from the session's command line, or None,
#      "model_restart": False when a restart (a resume of the same session) cannot change the model: the session keeps
#                       the model it started with (OpenCode, #5285); True by default,
#      "model_note":    a short line on how a person changes the model of a running session, when MaximizePM cannot
#                       ("change it in its terminal with /models"), or None,
#      "platform":      the name that settings, launch profiles (@claude-code) and a session's agent type store, when
#                       it is not the id (Claude Code: "claude-code"); it must be the id or an alias (#5185),
#      "tiers":         the tier words of its model names that TIER_STRENGTH does not know, strongest first
#                       (Antigravity: ["neon", "carbon", "argon", "hydrogen"]); order_models uses them (#6256),
#    }
# read_models() -> [model name or CLI id, or a dict of model facts] | None: an optional reader of the models the CLI
# knows now (Codex: ~/.codex/models_cache.json); None when it cannot read them, and DESCRIBE["models"] stands. A dict
# gives what a name cannot say (#6256): {"name": the id the CLI takes, "label": the name people read, "efforts": the
# agent's effort levels for this model ([] for none), "version": "4.1" (its generation), "tier": a number (higher is
# stronger)}; order_models reads the version and the tier from the name when the dict leaves them out.
# context_window(model) -> int | None: the tokens of that model's context window, for the context meter of a session
# (#5224); None when the adapter does not know it.
DESCRIBE_KEYS = {"id": str, "label": str, "exe": str, "aliases": (list, tuple), "family": str,
                 "models": (list, tuple), "default_model": str, "model_ids": dict, "prefix": str,
                 "model_live": (str, type(None)), "model_read": (str, type(None)), "platform": str,
                 "model_restart": bool, "model_note": (str, type(None)), "tiers": (list, tuple)}
DESCRIBE_REQUIRED = ("id", "label", "exe", "family")

# ---------------------------------------------------------------------------------------------------------------
# One order of models for every agent (#6256, mark 2026-11-19): newest generation first; within a generation, the
# strongest model first (Fable 5.2, Opus 5.5, Sonnet 5.5, Haiku 5.5, then the Claude 4 models). The models of one
# provider and one maker stay together, in the order the agent first names them. order_models is the one sort; each
# adapter feeds it its model facts (DESCRIBE['tiers'], the dicts of read_models).
# ---------------------------------------------------------------------------------------------------------------

# The strength of the tier words that MaximizePM knows, higher is stronger. An adapter adds its own (DESCRIBE['tiers']).
TIER_STRENGTH = {"fable": 4, "opus": 3, "sonnet": 2, "haiku": 1,
                 "astra": 4, "sol": 3, "terra": 2, "luna": 1,
                 "ultra": 4, "pro": 3, "flash": 2, "lite": 1, "mini": 1, "nano": 0}

# The maker of a model, from a word of its name: the words of one maker keep its models together.
MAKER_WORDS = {"anthropic": "anthropic", "claude": "anthropic", "fable": "anthropic", "opus": "anthropic",
               "sonnet": "anthropic", "haiku": "anthropic",
               "openai": "openai", "gpt": "openai", "astra": "openai", "sol": "openai", "terra": "openai",
               "luna": "openai", "codex": "openai",
               "google": "google", "gemini": "google", "gemma": "google"}

# Names that are no model but a choice of the agent itself: they come first.
CHOICE_MODELS = ("auto", "default")

# The effort level at the end of a model id (gemini-4-neon-high, claude-opus-5-5-xhigh, gpt-5.6-sol-max-fast).
LEVEL_SUFFIX = re.compile(r"-(none|minimal|low|medium|high|xhigh|extra-high|max)(-fast)?$")

_WORD = re.compile(r"[-_./: ()]+")
_VERSION = re.compile(r"[a-z]?(\d{1,3}(?:\.\d{1,3})*)")


def model_words(name):
    """The words of a model name, lower case: 'amazon-bedrock/anthropic.claude-opus-4-1' -> [..., 'claude', 'opus',
    '4', '1']."""
    return [w for w in _WORD.split(str(name or "").lower()) if w]


def model_version(name):
    """The version of a model name as numbers, or (): 'claude-opus-5-5' -> (5, 5), 'gpt-6.1-sol' -> (6, 1),
    'deepseek-v4.1-flash' -> (4, 1), 'gemini-4-neon' -> (4,). The first run of number words counts; a word such as
    '120b' or '900k' (a size) is no version, and neither is a date (20250805)."""
    model = str(name or "").lower().rsplit("/", 1)[-1]
    out = []
    for w in re.split(r"[-_ ]+", model):
        m = _VERSION.fullmatch(w)
        if m:
            out += [int(x) for x in m.group(1).split(".")]
        elif out:
            break
    return tuple(out)


def model_tier(name, tiers=()):
    """The strength of a model's tier word: the adapter's own words (tiers, strongest first) first, else
    TIER_STRENGTH; 0 when the name has no tier word."""
    words = model_words(name)
    own = [t.lower() for t in tiers or ()]
    for w in words:
        if w in own:
            return len(own) - own.index(w)
    return max((TIER_STRENGTH[w] for w in words if w in TIER_STRENGTH), default=0)


def model_maker(name):
    """The maker of a model ('anthropic', 'openai', 'google'), else the first word of its model part ('deepseek')."""
    words = model_words(str(name or "").rsplit("/", 1)[-1])
    return next((MAKER_WORDS[w] for w in words if w in MAKER_WORDS), words[0] if words else "")


def _version_of(m):
    v = m.get("version")
    if v is None:
        return model_version(m.get("name")) or model_version(m.get("id"))
    return tuple(int(x) for x in re.findall(r"\d+", str(v)))


class CliList:
    """A list that a CLI prints (`agy models`, `cursor-agent models`, `opencode models --standalone`, `pi --list-models`;
    #6257, #6512). Only maxpm serve
    starts the CLI: each pass of its loop calls refresh_lists(), which reads a list older than `ttl` seconds in a
    thread and keeps it in a file beside the queue (cli-lists/<exe>.json). get() starts nothing (#6510: a page build
    in a fresh process started `cursor-agent models`, which asks api2.cursor.sh over the network; p147: a page build
    answers in 100 ms): it gives the last read of this process or of serve's file, else the fallback. parse(text) ->
    a list of JSON values; runner: a test hook, a function () -> the text, used instead of the command."""

    LOOK = 30  # seconds between two looks at the file: serve's loop may run in another process (river/loop.py)
    ALL = []

    def __init__(self, argv, parse, fallback=(), ttl=600, timeout=60):
        import threading
        self.argv, self.parse, self.fallback, self.ttl, self.timeout = list(argv), parse, list(fallback), ttl, timeout
        self.runner = None
        self.state = {"at": 0.0, "list": None, "reader": None, "looked": 0.0, "mtime": None}
        self.lock = threading.Lock()
        CliList.ALL.append(self)

    def path(self):
        from .. import core
        return core.db_path().parent / "cli-lists" / f"{Path(self.argv[0]).name}.json"

    def get(self):
        import time
        with self.lock:
            now = time.time()
            if now - self.state["looked"] >= self.LOOK:
                self.state["looked"] = now
                self._load()
            got = self.state["list"]
        return list(got if got is not None else self.fallback)

    def refresh(self):
        """Start a read in a thread when the list is older than ttl and no read runs (maxpm serve's loop)."""
        import threading
        import time
        with self.lock:
            self._load()
            if self.state["reader"] is None and (self.state["list"] is None
                                                 or time.time() - self.state["at"] >= self.ttl):
                self.state["reader"] = threading.Thread(target=self._read, daemon=True, name=f"{self.argv[0]}-list")
                self.state["reader"].start()

    def wait(self):
        """Wait for a read that runs (tests)."""
        r = self.state["reader"]
        if r is not None:
            r.join()

    def reset(self):
        """Forget the list, and remove its file (tests)."""
        with self.lock:
            self.state.update(at=0.0, list=None, reader=None, looked=0.0, mtime=None)
            try:
                self.path().unlink()
            except OSError:
                pass

    def _load(self):
        """Take serve's file when it is newer than the list this process holds (inside the lock)."""
        import json
        try:
            p = self.path()
            mtime = p.stat().st_mtime
            if mtime == self.state["mtime"]:
                return
            d = json.loads(p.read_text(encoding="utf-8"))
            got, at = d["list"], float(d["at"])
        except (OSError, ValueError, KeyError, TypeError):
            return
        self.state["mtime"] = mtime
        if isinstance(got, list) and got and at > self.state["at"]:
            self.state.update(list=got, at=at)

    def _read(self):
        import json
        import subprocess
        import time
        got = []
        try:
            out = self.runner() if self.runner else subprocess.run(
                self.argv, capture_output=True, text=True, timeout=self.timeout, stdin=subprocess.DEVNULL).stdout
            got = list(self.parse(out or ""))
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        finally:
            at = time.time()
            if got:
                try:
                    p = self.path()
                    p.parent.mkdir(parents=True, exist_ok=True)
                    tmp = p.with_name(f".{p.name}.{os.getpid()}")
                    tmp.write_text(json.dumps({"at": at, "argv": self.argv, "list": got}), encoding="utf-8")
                    os.replace(tmp, p)
                except (OSError, TypeError, ValueError):
                    pass
            with self.lock:
                self.state.update(at=at, list=got or list(self.fallback), reader=None)


def refresh_lists(conn=None):
    """maxpm serve, each pass: read each CLI's list that is older than its ttl, in a thread (CliList.refresh)."""
    for cl in CliList.ALL:
        cl.refresh()


def split_levels(rows, levels=("none", "minimal", "low", "medium", "high", "xhigh", "max")):
    """Models whose ids carry the effort level (#6257: Cursor's gpt-5.6-sol-high, gpt-5.3-codex-low-fast): one row per
    model, {"name": the id without the level, "label", "efforts": its levels low to high}. rows: (id, label) in the
    CLI's order. A '-fast' end stays on the model (gpt-5.3-codex-fast), the level goes before it; a label loses its
    level words ('Codex 5.3 Low Fast' -> 'Codex 5.3 Fast'). A model that the CLI also lists with no level keeps that
    id's label."""
    lv = re.compile(r"^(.+?)-(" + "|".join(levels) + r")(-fast)?$")
    words = re.compile(r"\s+(?:None|Minimal|Low|Medium|High|Extra High|XHigh|Max)\b", re.I)
    out = {}
    for mid, label in rows:
        m = lv.match(mid)
        base, level = ((m.group(1) + (m.group(3) or "")), m.group(2)) if m else (mid, None)
        row = out.setdefault(base, {"name": base, "label": None, "efforts": []})
        if level is None:
            row["label"] = label  # the id with no level names the model best
            continue
        if level not in row["efforts"]:
            row["efforts"].append(level)
        if row["label"] is None:
            row["label"] = " ".join(words.sub("", label).split())
    for row in out.values():
        row["efforts"].sort(key=list(levels).index)
    return list(out.values())


_ORDER_MEMO = {}


def order_models(models, tiers=()):
    """The models in the one order of #6256, as a new list. models: model names or dicts with a "name" (and the facts
    "id", "version", "tier" of read_models). The agent's own choices (auto) first; then each provider (the part before
    a '/') and each maker in the order the list first names them; within one maker the newest generation (the first
    number of the version; a name with no version is the current model), then the strongest tier, then the newer
    version, then the shorter name. Each list is sorted once (OpenCode's list has hundreds)."""
    rows = [m if isinstance(m, dict) else {"name": m} for m in models or ()]
    memo_key = (tuple((r.get("name"), r.get("id"), r.get("version"), r.get("tier")) for r in rows), tuple(tiers or ()))
    order = _ORDER_MEMO.get(memo_key)
    if order is None:
        providers, makers, keys = {}, {}, []
        for i, r in enumerate(rows):
            name = str(r.get("name") or "")
            provider = name.split("/", 1)[0] if "/" in name else ""
            maker = model_maker(name) or model_maker(r.get("id"))
            p = providers.setdefault(provider, len(providers))
            g = makers.setdefault((provider, maker), len(makers))
            v = _version_of(r)
            tier = r["tier"] if r.get("tier") is not None else model_tier(name, tiers) or model_tier(r.get("id"), tiers)
            keys.append((name.lower() not in CHOICE_MODELS, p, g, -(v[0] if v else 10 ** 6), -tier,
                         tuple(-x for x in (v + (0,) * 4)[:4]), len(name), i))
        order = [k[-1] for k in sorted(keys)]
        if len(_ORDER_MEMO) > 64:
            _ORDER_MEMO.clear()
        _ORDER_MEMO[memo_key] = order
    return [models[i] for i in order]

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
#      "read":    a regex whose group 1 reads the value back from the session's command line; None if it cannot
#                 (a toggle with a "flag" reads as on when its first word is on the command line),
#      "config":  True (the default): a config key that the Setup section, the launch dialog (a toggle or a choice)
#                 and maxpm config set; False: no config key, only a flag that the Settings dialog of a running
#                 session adds or takes out at a restart (a toggle with a "flag"),
#      "session": True: the Settings dialog of a running session shows it (it needs "read", or a toggle's "flag", to
#                 show the value the session runs with); False (the default): only the Setup section and the launch
#                 dialog,
#      "note":    a short line the Settings dialog shows under it, or None,
#    }, ...]
SETTING_KINDS = ("toggle", "choice", "text")
SETTING_KEYS = {"key": str, "label": str, "kind": str, "choices": (list, tuple), "default": str, "text": str,
                "flag": (list, tuple), "live": (str, type(None)), "read": (str, type(None)), "config": bool,
                "session": bool, "note": (str, type(None))}

# config_settings() -> {setting key: value}: optional, what a session of the agent takes from the agent's own config
# when its command line names nothing (#6280: ~/.codex/config.toml model and model_reasoning_effort; agy's mode is
# auto), the agent's built-in default where its config names none. The Settings dialog of a session shows it last.
# SETTINGS_READ = {setting key: (source, ...)}: where the Settings dialog reads each setting of a running session
# (session_specs: model, effort and each setting with session True) when its command line names none: "chat"
# (chat_settings), "screen" (screen_settings), "config" (config_settings). A toggle with a flag needs none (its flag
# is on the command line or not). tests/test_model_order.py refuses a setting with no source (#6280).
# 3. Effort. EFFORT = None (the agent has no effort; the page hides it) or {
#      "levels": the agent's own levels, or None when read_effort_levels(model) reads them from the CLI per model,
#      "map":    {MaximizePM level: the agent's level}; a level with no entry goes as it is,
#      "flag":   argv words with {value} ("--effort", "{value}"),
#      "live":   the text typed into a running session ("/effort {value}"), or None,
#      "read":   a regex whose group 1 reads the effort back from a chat log line or the session's command line,
#                or None (or read_effort(entries)),
#      "label":  the name of the effort in the launch and Settings dialogs ("Reasoning effort"); "Effort" by default,
#    }
# The launch dialog and the Settings dialog offer MaximizePM's levels whose name for the agent is one of its "levels"
# (Adapter.effort_choices); the adapter turns a level into its own name (Adapter.effort_name).
EFFORT_KEYS = {"levels": (list, tuple, type(None)), "map": dict, "flag": (list, tuple),
               "live": (str, type(None)), "read": (str, type(None)), "label": str}

# 4. Launch (required). build_command(name, folder, model, effort, settings, prompt) -> argv (a list of str):
#    name: the session's MaximizePM name (None: no name); folder: where it runs (None when the caller does not say);
#    model: the id the CLI takes (MaximizePM maps a model_ladder name through model_ids first), or None; effort: a
#    MaximizePM level, or None; settings: {setting key: value} after the most specific scope wins, plus the
#    MaximizePM keys of LAUNCH_KEYS when the caller wants them; prompt: the first prompt. MaximizePM quotes each word
#    for the shell, but a Raw word, which goes in as it is (the args setting: a person's own shell text).
# LAUNCH = {capability: bool}, the parts of a start that the agent takes (LAUNCH_CAPABILITIES). parse_command(argv)
# -> a profile {"settings": {key: value}, "prompt": str} read back from an old hand-written command (a list of words;
# "{model}" and "{effort}" stand for the values MaximizePM fills in), or None when a profile cannot build it exactly.
LAUNCH_CAPABILITIES = ("session_name", "system_prompt_file", "hooks_file", "extra_dirs", "fork", "resume",
                       "autocompact", "background", "fixed_on_resume", "mcp_config", "read_dirs")
#    session_name: it takes a session name; system_prompt_file: the maxpm skill goes into its system prompt;
#    hooks_file: MaximizePM's hooks file goes on its command line; extra_dirs: the queue folder goes on its command
#    line as a folder it may write; fork: a session starts as a fork of another one; resume: build_command resumes a
#    session (resume_command uses it); autocompact: it compacts at a token count; background: it runs a command in
#    the background and wakes the session when it ends (maxpm wait runs there, not in the foreground);
#    fixed_on_resume: a resume keeps the settings the session started with and refuses a flag that differs (Hermes),
#    so resume_command passes none, and the Settings dialog changes them live or not at all (#5252); mcp_config: it
#    takes a file of MCP servers to add on its command line (the project browser's Playwright MCP, browser_mcp, #5405);
#    read_dirs: it reads the project's read_dirs with no question for each file, writes nothing in them and reads none
#    of their secret files (#6281); build_command turns the keys read_dirs and secret_files into its own form, or
#    needs none (Codex: its sandbox reads the whole disk).
# The MaximizePM keys in settings, each only when the caller wants it and LAUNCH has its capability:
LAUNCH_KEYS = {
    "system_prompt_file": "True: put the maxpm skill into the system prompt",
    "hooks_file": "the path of MaximizePM's hooks file, or of the queue guard alone (core.settings_file, #6412)",
    "fork": "the session id to start a fork of",
    "resume": "the session id to resume (resume_command)",
    "autocompact": "the token count to compact at",
    "mcp_config": "the path of a file of MCP servers to add (browser_mcp: Playwright MCP on the project's browser)",
    "noflags": "the words a person took out of the args setting (they stay out of what the adapter adds itself)",
    "read_dirs": "[absolute folders] the session reads with no question for each file and writes nothing in (#6281)",
    "secret_files": "[absolute paths] the secret files in read_dirs at the start (core.secret_files), never read",
}


class Raw(str):
    """A word of build_command that MaximizePM puts into the shell command as it is, with no quotes."""


# FIRST_PROMPT: how the first prompt reaches the agent. "argv" (the default): build_command puts it on the command
# line. "paste": the CLI has no flag that sends a prompt and keeps the chat open (Aider), so build_command and
# resume_command leave it out, and maxpm serve pastes it into the agent's tmux pane, then Enter, once DETECT['idle']
# shows there (#5221). An adapter with "paste" needs DETECT.
FIRST_PROMPTS = ("argv", "paste")

# 5. Session identity. IDENTITY = {
#      "source":     how MaximizePM learns the session's id after the start: one of IDENTITY_SOURCES,
#      "env":        the variable that holds it ("env" source), or None,
#      "pane":       a regex whose group 1 is the id in the pane's text ("pane" source), or None,
#      "url":        its web link with {id} ("https://.../{id}"), or one link for every session (Codex's web app), or None,
#      "inside_env": the variables that tell that a command runs inside this agent (CLAUDECODE, CODEX_THREAD_ID);
#                    this replaces core.agent_type_from_env,
#      "id_pattern": a regex that the whole id must match, or None for SAFE_ID (#5212: an id from the environment
#                    names a log file, so it never holds a path or a glob),
#      "process":    which process is the session's own (agents.pid: maxpm who, the busy checks, maxpm stop --kill):
#                    "ancestor" (the default), the first process above a maxpm command's shells; "terminal", that
#                    process only when it runs on a terminal, else none (#5247: Hermes runs the tools of every
#                    session in one background gateway with no terminal, so the ancestor is that shared gateway);
#                    "nearest_terminal", the first process upward from there that runs on a terminal (#5283:
#                    OpenCode runs its tools from a private server, a child of its TUI with no terminal),
#    }
# start_session(name, settings) -> the session id that a start chooses itself, or None: the arguments of build_command
# (Aider names its log by the start's name). The start path records it at once (agent_sessions), so the Sessions tab
# finds the chat before the session's first maxpm command (#5280).
# With source "env" and CHAT["ref"] "session", each maxpm command records the id it finds in "env" for the acting
# agent (core.session_from_env, agent_sessions): the Sessions tab then finds the agent's chat log by it (#5212).
IDENTITY_SOURCES = ("hook", "env", "pane")
IDENTITY_KEYS = {"source": str, "env": (str, type(None)), "pane": (str, type(None)),
                 "url": (str, type(None)), "inside_env": (list, tuple), "id_pattern": (str, type(None)),
                 "process": (str, type(None))}
IDENTITY_PROCESS = ("ancestor", "terminal", "nearest_terminal")
SAFE_ID = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"

# 6. Chat log (#5182: river/sessions.py reads every chat log through this part, and core.measure_usage the usage).
# chat_path(session_ref, folder, root=None) -> the log file of a session (a path), or None; session_ref is what
# CHAT["ref"] names, and root, when given, replaces the agent's own log folder (the tests give one). parse_chat(lines)
# -> (entries, turn): the shared entries of a list of log lines (each a str), and the turn state at the end
# ("working", "idle", or None when the lines do not say). CHAT = {
#      "compressed":  bool: its log can be zstd-compressed (a .zst file; the readers decompress by the file's suffix),
#      "file_tools":  [the tool names that write a file, for the Artifacts tab],
#      "ref":         what finds the log of an agent's session: one of CHAT_REFS: "session" (the newest session id of
#                     the agent, agent_sessions) or "address" (its native_address, when the agent runs this CLI),
#      "cache_write": bool: a call after the prompt cache ran out reads the context at the cache write price (Claude
#                     Code); False: at the input price (Codex),
#      "format":      one of CHAT_FORMATS: "jsonl" (the default: one JSON object per line, parse_chat_line) or "text"
#                     (#5222: entries of several lines, Aider's Markdown; the Sessions tab calls parse_chat over each
#                     block of lines, and each entry of a block scrolls with the byte where its block starts),
#      "commands":    bool (default True): the log records a slash command that the person types (/reasoning show);
#                     False (#5253: Hermes keeps no record of its local commands): the Sessions tab does not wait to
#                     see a sent slash command in the chat,
#      "block":       for "text": a regex of a line that starts a block (a run of such lines starts one block: a prompt
#                     of several lines); None: each read is one block. Cut where no entry spans the cut (Aider: at a
#                     prompt, "^#### "); an entry with no time gets the time of the entry before it,
#    }
# Optional readers:
#   parse_chat_line(d) -> (entries, turn) of one decoded log line (a dict): the Sessions tab parses line by line, to
#     know the byte where each entry's line starts (scrolling up); without it, it calls parse_chat([line]);
#   chat_chain(path, refs=None) -> [paths] of one chat across resumes, oldest first, path last; refs: the agent's
#     session refs, oldest first (Claude Code finds its resumed files by them; a log that names its parent needs none);
#   chat_title(lines) -> str or None; chat_cwd(lines) -> str or None;
#   chat_usage(lines) -> {"input", "output", "cache_read", "cache_write"} token counts of the lines;
#   chat_requests(session_ref, root=None) -> (requests, fork) or None when no log of it is on this computer: requests
#     {request id: (time, {"input", "output", "cache_read", "cache_write", "write_1h", "write_5m"} token counts)},
#     each model call once, and fork True when the session started as a fork of another (core.measure_usage);
#   chat_settings(lines) -> {setting key: value} as the session runs now;
#   chat_cache_line(raw, st): fold one log line (bytes) into the prompt cache state st (a dict that starts as
#     {"last": None, "ttl": None, "model": None}; the reader may keep keys of its own there): st["last"] = {"start",
#     "at", "context"} of the last model call (when it started, its last line, the tokens of its context),
#     st["ttl"] the cache lifetime the log shows ("1h", "5m"; None: the cache_ttl setting), st["model"];
#   chat_texts(raw) -> [(text, time, how)]: the texts of one log line (bytes) where the Artifacts tab looks for files
#     and links; how is "wrote" for the path of a CHAT["file_tools"] call, else "named". Tool results are left out.
#   background_scan(path, start, st) -> end: fold the complete lines of the log from byte start on (new_lines) into
#     st (a dict, {} at first; the reader keeps its own keys there) and return the byte after the last one. Its tasks
#     are st["tasks"]: {task id: {"id", "kind" ("command" or "subagent"), "at" (start, datetime), "what" (a short
#     description), "command", "end" (datetime, None while it runs), "status"}}: the background commands and
#     subagents that the agent CLI started (core.session_tasks, #6454). Only for an agent with LAUNCH["background"].
#   chat_limits(lines) -> [{"name", "used_percent", "window_minutes", "resets_at"}, ..., {"name": "credits",
#     "text"}] | None: the plan limits that the log says last (Codex: the rate_limits of its token_count events), for
#     the Usage panel (#5224); None when the agent writes none to its log (they show only on its screen).
# An entry is a dict with "at" (ISO time or None), "role" (one of ENTRY_ROLES) and "text", and by role:
#   user: "images" (a list of paths or data URLs) when it has some; tool: "id" (the call id), "name", "detail";
#   result: "for" (the call id), "error" (bool); divider: "kind" ("compacted", "resumed").
ENTRY_ROLES = ("assistant", "user", "tool", "result", "divider", "summary", "event")
TURNS = ("working", "idle")
CHAT_REFS = ("session", "address")
CHAT_FORMATS = ("jsonl", "text")
CHAT_KEYS = {"compressed": bool, "file_tools": (list, tuple), "ref": str, "cache_write": bool, "format": str,
             "commands": bool,
             "block": (str, type(None))}

# 7. State. STATE = {
#      "state":      the one source of the state: "hook" (install_hooks puts a hook or plugin in place that calls
#                    maxpm report) or "detect" (maxpm's pane watcher applies DETECT and calls maxpm report),
#      "session_id": the one source of the session id: "hook" (the report carries --session-id) or IDENTITY,
#    }
# No chain of fallbacks: each signal has one declared source.
# install_hooks(folder, report) -> [argv words to add to the start] | None: puts the hooks in place (core.with_hooks
# puts words that start with env before the command, other words right after the program); `report` is the
# maxpm report command (a list of argv words) that each hook runs with its --state. An adapter whose state comes from
# DETECT may have install_hooks too, for a hook before a shell command only (Codex, #6254): read_hook gives "run".
# project_hooks(folder, report, on) -> the hook file's path | None: for a CLI that loads hooks only from a file in the
# project (agy's .agents/hooks.json, #6351): puts MaximizePM's hooks into the start folder's file (on) or takes them
# out (off), keeping the person's own hooks; behind the agent's setting activity_hooks (core.project_hooks).
# hook_answer(res) -> text | None: what maxpm report --hook <id> prints for this CLI (its own JSON), from what
# core.hook_report returned (deny, remind) and the messages that wait at a turn end (deliver: core.turn_end_messages).
# Without it, the output is Claude Code's JSON (Codex reads the same).
# read_hook(payload) -> a hook report (HOOK_REPORT_KEYS) | None: for a hook that passes its own input on stdin
# (maxpm report --hook <id>; Claude Code's JSON), what that input says. None: the input is no event of this agent.
# A hook report = {
#      "state":   one of STATES, or None (the event says nothing of the state; only the time is kept),
#      "event":   the agent's own name of the event ("Stop", "Notification:permission_prompt"), for the history,
#      "session_id": the agent's id of the session, or None,
#      "message": blocked: what it waits for, or None,
#      "call":    blocked by a permission: {"tool": name, "input": {key: text}} of the call that waits, or None,
#      "shows":   blocked: True when the person sees the question now (the manager hears of it at once),
#      "waits":   idle: [short texts] of the agent's own background work that will wake it; [] for none; None
#                 when the input does not say (MaximizePM then looks at the processes below the session),
#      "prompt":  working: the text that the person (or a session) typed, or None,
#      "again":   idle: True when the agent ends its turn a second time after a hook kept it going (no reminder),
#      "run":     the shell command that the agent is about to run (a hook before the tool call), or None: MaximizePM
#                 may refuse it (core.push_guard, #5381); the hook records nothing else,
#    }
# screen_settings(lines) -> {setting key: value}: optional, the settings that the session's screen shows now (agy's
# footer: mode, model and effort, #6260), from the plain lines of its pane; {} when the screen shows none. The Settings
# dialog of a session takes them before the chat log's (chat_settings): the screen shows a change at once.
# model_group(model) -> str | None: optional, the quota group of a model when the agent counts its usage per group of
# models (agy: 'Gemini models', 'Claude and GPT models', #6382), named as screen_limits names its windows
# ('<group>: 5-hour'). A limit stop of a session then limits only its model's group; None: the whole agent.
# DETECT = {
#      "process":      the process name of the agent in the pane,
#      "process_args": optional: a regex that finds the agent in the command line (ps args) of a process on the
#                      pane's terminal, for a CLI whose launcher shows neither its name nor a script (Hermes runs
#                      'python3 -I -c <script>'); the pane watcher tries it after "process",
#      "idle":         a regex of the pane when the agent waits at its prompt,
#      "working":      a regex of the pane while it runs a turn ("esc to interrupt"),
#      "blocked":      [regexes], one for each screen that waits for a person (a permission menu, a question, trust),
#      "refused":      optional: [regexes] of an error that ends each turn because the provider refuses the model or
#                      the account (Codex's 400 'model is not supported', #6429); a group (?P<model>...) names the
#                      model. The pane watcher then ends the session and starts its item on the next provider,
#      "tell_instead": a regex of the menu option that lets the person type an answer instead, or None,
#      "image_paste":  how a pasted image shows in its prompt, with {n} ("[Image #{n}]"), or None,
#      and the layout of a blocked screen, each optional (DETECT_LAYOUT holds the values of a key left out):
#      "menu":         the characters that mark the selected option of a menu ("❯›"),
#      "other":        the characters that mark each other option, at the column of the selected one's mark (cliclack:
#                      "●" selected, "○" each other one); "": the other options have no mark, and their text starts
#                      at the column of the selected option's text (Claude Code, Codex),
#      "item":         where the mark stands in an option's line: a regex with {mark} for it and no group of its own
#                      ("{mark}": the mark comes first; Grok: r"\d+ \({mark}\)" for "3 (●) Yes, proceed", #5261),
#      "frame":        the characters of the frame some CLIs draw around a prompt,
#      "rule":         a regex of a dashed rule (the input box's edge),
#      "hint":         a regex of the line that names keys ("Enter to confirm · Esc to cancel"),
#      "row":          a regex of a menu on one line, whose group 1 holds the options, two or more spaces apart
#                      (OpenCode: "Allow once   Always allow   Reject      ⇆ select  enter confirm"); Left and Right
#                      move in it. Its selected option shows by colour only, so the page takes the first one as
#                      selected (OpenCode selects it at each new request). "": no such menu (#5281),
#      "fixed":        a regex of a question (its text or an option) that takes only one of its options, no typed text,
#                      when it has no "tell_instead" option: the question card then has no Other (#5904). "": none,
#      "ask":          a regex of the first line of the CLI's own question dialog (Claude Code's AskUserQuestion: its
#                      tab line "←  ☐ Color  ☒ Size  ✔ Submit  →", or " ☐ Pet" for one question); the page then reads
#                      the dialog and answers it on its own card (#6234). "": none,
#      "ask_form":     how that dialog is drawn and answered: "claude" (server._ask_dialog), or a form of
#                      river/ask_forms: "codex", "copilot", "opencode" (#6235),
#      "ask_open":     {"line": a regex of the line that shows a question closed, "keys": the tmux keys that open
#                      it}, or None. Codex's Default mode shows a question closed ('? 1 question', 'shift+← to
#                      answer'); MaximizePM opens each one once, so that the page reads it (#6258),
#    }
# The pane watcher of maxpm serve (server.watch_prompts) reads each agent's pane with its DETECT: a blocked screen
# alerts the person (with its question and options) for every agent; for an agent whose STATE["state"] is
# "detect" it also reports the state (blocked, working, idle) as maxpm report does.
STATE_SOURCES = ("hook", "detect")
STATE_KEYS = {"state": str, "session_id": str}
HOOK_REPORT_KEYS = {"state": (str, type(None)), "event": str, "session_id": (str, type(None)),
                    "message": (str, type(None)), "call": (dict, type(None)), "shows": bool,
                    "waits": (list, type(None)), "prompt": (str, type(None)), "again": bool, "run": (str, type(None))}
DETECT_KEYS = {"process": str, "process_args": str, "idle": str, "working": str, "blocked": (list, tuple),
               "limit": (list, tuple), "refused": (list, tuple),
               "tell_instead": (str, type(None)), "image_paste": (str, type(None)),
               "menu": str, "other": str, "item": str, "frame": str, "rule": str, "hint": str, "row": str,
               "fixed": str, "ask": str, "ask_form": str, "ask_open": (dict, type(None))}
ASK_FORMS = ("claude", "codex", "copilot", "opencode", "antigravity", "hermes")
DETECT_LAYOUT = {
    "menu": "❯›",
    "other": "",
    "item": "{mark}",
    "frame": "│┃╭╮╰╯┌┐└┘",
    # A rule can hold one short name between its dashes: Claude Code writes the session's name into the top edge of
    # its input box ("──────── maxpm manager ─"). The plain rule alone let the person's typed "2. comments ..." in
    # that box read as a numbered menu, and the person got an alert to answer their own text (#6155).
    "rule": r"^\s*(?=.*[─━╌╍┄┅═-]{8})[─━╌╍┄┅═-]+(?:\s+[^─━╌╍┄┅═\s][^─━╌╍┄┅═]{0,78}?\s+[─━╌╍┄┅═-]+)?\s*$",
    # Also Codex's key lines ("enter select · esc back", "enter default · s session · esc back") and the key line over
    # Claude Code's /model menu ("◐ Medium effort (default) ←/→ to adjust", #5220).
    "hint": r"\b(enter|esc|return|tab)\b.*\bto (confirm|cancel|select|continue|go back)\b|\[[yY]/[nN]\]"
            r"|^\s*enter \w+ ·.*\besc \w+|←/→ to adjust",
    "row": "",
    # The trust question of each CLI (Claude Code's 'Yes, I trust this folder', Codex, Antigravity CLI, Cursor,
    # Copilot, Grok, Pi): its Esc exits the CLI, so the card offers no Other there (#5904).
    "fixed": r"(?i)\btrust\b",
    "ask": "",
    "ask_form": "claude",
    "ask_open": None,
}

# 8. Input. tmux paste is the same for every agent; the image paste rule and the "tell it instead" text are in
# DETECT. INPUT = {"native": argv words with {address} and {text} for a native message command (Codex:
# codex queue --thread {address} {text}), or None,
#      "interrupt": the tmux keys (INTERRUPT_KEYS) that end the agent's running turn and keep its session, as a person
#                   presses them in its terminal (Claude Code: ["Escape"]; OpenCode asks for a second Esc:
#                   ["Escape", "Escape"]), or None: the agent has no such key, and the page's Stop says so (#5547),
#      "interrupt_again": {"text": a slash command, "does": what it does, in a few words}, or None: what a second Stop
#                   of the same turn types after the keys (Codex: {"text": "/stop", "does": "end its background
#                   commands"}: its Esc leaves the turn's shell commands running, #5563)}.
INPUT_KEYS = {"native": (list, tuple, type(None)), "interrupt": (list, tuple, type(None)),
              "interrupt_again": (dict, type(None))}
INTERRUPT_KEYS = ("Escape", "C-c")

# 9. Resume. resume_command(session_ref, model, effort, settings, name=None) -> argv, or None when it cannot resume that
# session. The arguments are those of build_command; settings["prompt"] is the first prompt after the resume.

# 10. Commands. COMMANDS = [{"name": "compact" (no "/"), "description": str, "hint": the argument hint or ""}, ...]:
# the built-in slash commands. read_commands(folder) -> the same shape: its custom commands for a session that runs in
# folder (Claude: ~/.claude/commands, .claude/commands, the skills, plugin commands; Codex: ~/.codex/prompts).
# "run" (#5220) says how the command works from the page's message box, typed with no arguments; the "/" list marks it:
#   "now":      it runs at once; what it does shows in the chat, or it changes the session (/compact, /plan),
#   "menu":     it opens a numbered menu, which the page's ! dialog shows and answers (/model, Codex's /review),
#   "terminal": its view or its result shows only in the terminal (a full-screen view, a slider, a card printed in
#               the pane: /config, Codex's /status); the page sends it and links to the terminal.
# No "run" (a custom command, or an adapter that does not say): the page sends it and does not mark it.
# "setting" (mark 2026-11-11, #5220): the key of the Settings dialog's row that does what the command does with no
# argument ("model", "effort"). Typed with no argument, the page opens that dialog at the row, and nothing goes to the
# terminal ("Change now" types the command with its argument); with an argument it goes as typed.
# "page" (#5224, mark 2026-11-11: 'about usage, context etc'): a page form that does what the command shows, typed
# with no argument; nothing goes to the terminal. "usage": the session's Usage panel (its context against the
# model's window, its tokens, its calls, and the plan limits where the adapter reads them: chat_limits).
COMMAND_KEYS = {"name": str, "description": str, "hint": str, "run": str, "setting": str, "page": str}
COMMAND_RUNS = ("now", "menu", "terminal")
COMMAND_PAGES = ("usage",)

# 11. Setup (#5184). SETUP = {
#      "instructions": the instruction file it reads in a project folder (CLAUDE.md, AGENTS.md),
#      "replaces":     the instruction file it read before, whose rules maxpm init moves into SHARED_INSTRUCTIONS and
#                      then deletes (Claude Code: CLAUDE.md, #6024), or None,
#      "skills_dir":   the folder of its skills (~ expands), or None,
#      "voice":        what its own voice mode does, one sentence, or None,
#      "open_link":    the label of the page's button that opens a session's web link (IDENTITY["url"]), or None,
#      "open_title":   that button's tooltip, or None,
#      "no_link":      why the page cannot open a session of it that runs in no tmux pane here, or None,
#    }
# write_mcp_config(path=None, remove=False) -> one text that says what changed: adds MaximizePM's MCP server (maxpm
# mcp, setup_entry()) to the agent's config file (path: another file than its own), or takes it out again.
# desktop_app() -> None when the agent's desktop chat app is not on this computer, else {"name": the app's name,
# "added": True when its config has MaximizePM's server, "path": that config file}: the setup guide offers
# write_mcp_config() for it (#6477).
# write_guard(path=None, remove=False) -> one text that says what changed: adds the queue guard (#6412: no write to
# the queue file and its -wal and -shm files) to the agent's own settings, for sessions that a person starts by hand
# (maxpm setup-agent --guard), or takes it out again.
SETUP_KEYS = {"instructions": str, "replaces": (str, type(None)),
              "skills_dir": (str, type(None)),
              "voice": (str, type(None)), "open_link": (str, type(None)), "open_title": (str, type(None)),
              "no_link": (str, type(None))}

# 12. Update (#6430). UPDATE = {
#      "version":    the arguments that print the installed version (default ["--version"]),
#      "smoke":      the arguments of one short request that is not interactive and must answer OK, the test after an
#                    update ({model}: the id of the agent's default model; with none, the word and the flag before it
#                    go), or None: the version command is the test,
#      "self":       the arguments of the CLI's own update command, for a program that no package manager installed
#                    ({version}: the version to install; with none, its newest), or None,
#      "self_check": the arguments of the CLI's own check for a newer version, and "self_read" a regex whose group 1
#                    reads the newest version from its output,
#      "npm", "pypi", "brew", "cask": the package where the vendor publishes the same versions, for the newest version
#                    of a program that updates itself (Claude Code: npm @anthropic-ai/claude-code),
#      "service":    a regex of the args of the CLI's background server, which is no session: an update does not wait
#                    for it, and stops it after the update, so the next start runs it on the new version (OpenCode:
#                    'opencode serve --service', #6461),
#    }
# A program that npm, Homebrew, uv or pipx installed updates with that package manager: river/agentupdate.py reads the
# method from where the program is (install_of), and these keys only add to it.
UPDATE_KEYS = {"version": (list, tuple), "smoke": (list, tuple, type(None)), "self": (list, tuple, type(None)),
               "self_check": (list, tuple), "self_read": str, "npm": str, "pypi": str, "brew": str, "cask": str,
               "service": str}

# Each part: the module names that make it up, and the dict whose keys check (name, shape).
PARTS = {
    "describe": {"data": ("DESCRIBE", DESCRIBE_KEYS), "functions": ("read_models", "context_window"), "required": True},
    "settings": {"data": ("SETTINGS", SETTING_KEYS), "functions": ("config_settings",)},
    "effort": {"data": ("EFFORT", EFFORT_KEYS), "functions": ("read_effort_levels", "read_effort")},
    "launch": {"data": ("LAUNCH", None), "functions": ("build_command", "parse_command", "trust_workspace",
                                                       "untrust_workspace", "install_guard"), "required": True},
    "identity": {"data": ("IDENTITY", IDENTITY_KEYS), "functions": ("start_session",)},
    "chat": {"data": ("CHAT", CHAT_KEYS), "functions": ("chat_path", "parse_chat", "parse_chat_line", "chat_chain",
                                                        "chat_title", "chat_cwd", "chat_usage", "chat_requests",
                                                        "chat_settings", "chat_cache_line", "chat_texts",
                                                        "chat_limits", "background_scan")},
    "state": {"data": ("STATE", STATE_KEYS), "functions": ("install_hooks", "read_hook", "project_hooks",
                                                           "hook_answer")},
    "detect": {"data": ("DETECT", DETECT_KEYS), "functions": ("screen_settings", "screen_limits", "limit_stop",
                                                             "latest_limits", "model_group")},
    "input": {"data": ("INPUT", INPUT_KEYS), "functions": ()},
    "resume": {"data": None, "functions": ("resume_command",)},
    "commands": {"data": ("COMMANDS", COMMAND_KEYS), "functions": ("read_commands",)},
    "setup": {"data": ("SETUP", SETUP_KEYS), "functions": ("write_mcp_config", "write_guard", "desktop_app")},
    "update": {"data": ("UPDATE", UPDATE_KEYS), "functions": ()},
}

def new_lines(path, start, chunk=1 << 22):
    """(offset, end, text) of each complete line of the file from byte start on: where it starts, the byte after its
    newline, and its text. A last line with no newline yet is left for the next read. It reads chunk bytes at a time:
    a first read of a log of 280 MB holds no copy of the whole file."""
    with open(path, "rb") as f:
        f.seek(start)
        rest, pos = b"", start
        while True:
            got = f.read(chunk)
            if not got:
                return
            got = rest + got
            cut = got.rfind(b"\n") + 1
            rest = got[cut:]
            for line in got[:cut].split(b"\n")[:-1]:
                yield pos, pos + len(line) + 1, line.decode("utf-8", "replace")
                pos += len(line) + 1


BUILTIN = ("claude", "codex", "antigravity", "pi", "copilot", "hermes", "grok", "opencode", "goose", "aider", "amp", "cursor")
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


def _setting_problems(i, s):
    """The problems of one setting spec beyond its shape (#5169): a key that is no config key name or that the
    Settings dialog uses itself, a default that its kind does not take, a flag-only setting that is no toggle."""
    out, where, kind = [], f"SETTINGS[{i}]", s.get("kind")
    if isinstance(s.get("key"), str) and not _ID.match(s["key"]):
        out.append(f"{where}['key'] is {s['key']!r}; a key is a-z, 0-9 and _")
    if s.get("key") in ("model", "effort"):
        out.append(f"{where}['key'] is {s['key']!r}: the model and the effort come from DESCRIBE and EFFORT")
    default = s.get("default", "off" if kind == "toggle" else "")
    if kind == "toggle" and default not in ("on", "off"):
        out.append(f"{where}['default'] is {default!r}; a toggle is on or off")
    if kind == "choice" and default and default not in (s.get("choices") or ()):
        out.append(f"{where}['default'] is {default!r}, not one of its choices (or '' for the agent's own)")
    if kind == "choice" and not s.get("choices"):
        out.append(f"{where} is a choice with no choices")
    if kind == "choice" and "" in (s.get("choices") or ()):
        # The page puts 'the agent's own' (value '') first in each choice itself; a '' here shows a second, blank
        # option (#5235). A default of '' means the agent's own.
        out.append(f"{where}['choices'] holds ''; leave it out: the default '' means the agent's own")
    if s.get("config") is False and not (kind == "toggle" and s.get("flag")):
        out.append(f"{where} has config False: such a setting is a toggle with a flag")
    if s.get("config") is False and not s.get("session"):
        out.append(f"{where} has config False and session False: nothing shows it")
    return out


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
        if isinstance(d.get("platform"), str) and d["platform"].lower() not in (
                [str(d.get("id", "")).lower()] + [str(x).lower() for x in d.get("aliases") or ()]):
            out.append(f"DESCRIBE['platform'] is {d['platform']!r}; it must be the id or one of the aliases")
        if d.get("family") is not None and d.get("family") not in FAMILIES:
            out.append(f"DESCRIBE['family'] is {d.get('family')!r}, not one of {', '.join(FAMILIES)}")
    for i, s in enumerate(getattr(module, "SETTINGS", None) or ()):
        out += _shape(f"SETTINGS[{i}]", s, SETTING_KEYS, ("key", "kind"))
        if isinstance(s, dict):
            if s.get("kind") not in SETTING_KINDS:
                out.append(f"SETTINGS[{i}]['kind'] is {s.get('kind')!r}, not one of {', '.join(SETTING_KINDS)}")
            if s.get("read"):
                out += _regex(f"SETTINGS[{i}]['read']", s["read"])
            out += _setting_problems(i, s)
    keys = [s.get("key") for s in getattr(module, "SETTINGS", None) or () if isinstance(s, dict)]
    out += [f"SETTINGS has the key {k!r} twice" for k in sorted({k for k in keys if keys.count(k) > 1})]
    if isinstance(d, dict) and d.get("model_read"):
        out += _regex("DESCRIBE['model_read']", d["model_read"])
    e = getattr(module, "EFFORT", None)
    if e is not None:
        out += _shape("EFFORT", e, EFFORT_KEYS)
        if isinstance(e, dict):
            out += [f"EFFORT['map'] maps {k!r}, which is no MaximizePM effort level"
                    for k in e.get("map") or {} if k not in MAXPM_EFFORTS]
            if e.get("read"):
                out += _regex("EFFORT['read']", e["read"])
    first = getattr(module, "FIRST_PROMPT", "argv")
    if first not in FIRST_PROMPTS:
        out.append(f"FIRST_PROMPT is {first!r}, not one of {', '.join(FIRST_PROMPTS)}")
    elif first == "paste" and not isinstance(getattr(module, "DETECT", None), dict):
        out.append("FIRST_PROMPT is 'paste', but the module has no DETECT: maxpm serve cannot see its idle prompt")
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
        if isinstance(ident, dict) and ident.get("process") not in (None,) + IDENTITY_PROCESS:
            out.append(f"IDENTITY['process'] is {ident.get('process')!r}, not one of {', '.join(IDENTITY_PROCESS)}")
        if isinstance(ident, dict) and isinstance(ident.get("id_pattern"), str):
            out += _regex("IDENTITY['id_pattern']", ident["id_pattern"])
    if (getattr(module, "chat_path", None) is None) != (getattr(module, "parse_chat", None) is None):
        out.append("the chat part needs chat_path and parse_chat together")
    chat = getattr(module, "CHAT", None)
    if chat is not None:
        out += _shape("CHAT", chat, CHAT_KEYS)
        if isinstance(chat, dict) and chat.get("format") is not None and chat.get("format") not in CHAT_FORMATS:
            out.append(f"CHAT['format'] is {chat.get('format')!r}, not one of {', '.join(CHAT_FORMATS)}")
        if isinstance(chat, dict) and isinstance(chat.get("block"), str):
            out += _regex("CHAT['block']", chat["block"])
        if isinstance(chat, dict) and chat.get("ref") is not None and chat.get("ref") not in CHAT_REFS:
            out.append(f"CHAT['ref'] is {chat.get('ref')!r}, not one of {', '.join(CHAT_REFS)}")
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
            for k in ("idle", "working", "rule", "hint", "tell_instead", "process_args", "ask"):
                if isinstance(det.get(k), str):
                    out += _regex(f"DETECT[{k!r}]", det[k])
            if isinstance(det.get("ask_open"), dict):
                op = det["ask_open"]
                if not isinstance(op.get("line"), str) or not op.get("keys") or \
                        not all(isinstance(k, str) for k in op.get("keys") or ()):
                    out.append("DETECT['ask_open'] needs a 'line' regex and a list of 'keys'")
                else:
                    out += _regex("DETECT['ask_open']['line']", op["line"])
            if isinstance(det.get("ask_form"), str) and det["ask_form"] not in ASK_FORMS:
                out.append(f"DETECT['ask_form'] is {det['ask_form']!r}, not one of {', '.join(ASK_FORMS)}")
            if isinstance(det.get("item"), str):
                if "{mark}" not in det["item"]:
                    out.append("DETECT['item'] has no {mark}")
                else:
                    rx = det["item"].replace("{mark}", "(x)")
                    bad = _regex("DETECT['item']", rx)
                    out += bad or ([] if re.compile(rx).groups == 1 else
                                   ["DETECT['item'] has a group of its own; use (?:...)"])
            for i, p in enumerate(det.get("blocked") or ()):
                out += _regex(f"DETECT['blocked'][{i}]", p) if isinstance(p, str) else [f"DETECT['blocked'][{i}] is no str"]
            for key in ("limit", "refused"):
                for i, p in enumerate(det.get(key) or ()):
                    out += _regex(f"DETECT[{key!r}][{i}]", p) if isinstance(p, str) else [f"DETECT[{key!r}][{i}] is no str"]
    if getattr(module, "INPUT", None) is not None:
        out += _shape("INPUT", module.INPUT, INPUT_KEYS)
        keys = module.INPUT.get("interrupt") if isinstance(module.INPUT, dict) else None
        if keys is not None and (not keys or any(k not in INTERRUPT_KEYS for k in keys)):
            out.append(f"INPUT['interrupt'] is {list(keys)!r}: one or more of {', '.join(INTERRUPT_KEYS)}, or None")
        again = module.INPUT.get("interrupt_again") if isinstance(module.INPUT, dict) else None
        if again is not None and not (isinstance(again.get("text"), str) and again["text"].startswith("/")
                                      and isinstance(again.get("does"), str) and again["does"].strip()
                                      and set(again) == {"text", "does"}):
            out.append(f"INPUT['interrupt_again'] is {again!r}: {{'text': '/<command>', 'does': '<a few words>'}}, "
                       f"or None")
    for i, c in enumerate(getattr(module, "COMMANDS", None) or ()):
        out += _shape(f"COMMANDS[{i}]", c, COMMAND_KEYS, ("name",))
        if isinstance(c, dict) and "run" in c and c["run"] not in COMMAND_RUNS:
            out.append(f"COMMANDS[{i}]['run'] is {c['run']!r}, not one of {', '.join(COMMAND_RUNS)}")
        if isinstance(c, dict) and "page" in c and c["page"] not in COMMAND_PAGES:
            out.append(f"COMMANDS[{i}]['page'] is {c['page']!r}, not one of {', '.join(COMMAND_PAGES)}")
        if isinstance(c, dict) and c.get("setting") not in (None, "model", "effort") and c["setting"] not in [
                x.get("key") for x in getattr(module, "SETTINGS", None) or () if isinstance(x, dict)]:
            out.append(f"COMMANDS[{i}]['setting'] is {c['setting']!r}, which is no setting of the Settings dialog")
    if getattr(module, "SETUP", None) is not None:
        out += _shape("SETUP", module.SETUP, SETUP_KEYS, ("instructions",))
    up = getattr(module, "UPDATE", None)
    if up is not None:
        out += _shape("UPDATE", up, UPDATE_KEYS)
        if isinstance(up, dict):
            for k in ("self_read", "service"):
                if isinstance(up.get(k), str):
                    out += _regex(f"UPDATE[{k!r}]", up[k])
            for k in ("version", "smoke", "self", "self_check"):
                if isinstance(up.get(k), (list, tuple)) and not all(isinstance(w, str) for w in up[k]):
                    out.append(f"UPDATE[{k!r}] holds a word that is no str")
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
        self.first_prompt = getattr(m, "FIRST_PROMPT", "argv")
        self.identity = getattr(m, "IDENTITY", None)
        self.chat = getattr(m, "CHAT", None)
        self.state = getattr(m, "STATE", None)
        self.detect = getattr(m, "DETECT", None)
        self.input = dict(getattr(m, "INPUT", None) or {})
        self.commands = list(getattr(m, "COMMANDS", None) or ())
        self.setup = getattr(m, "SETUP", None)
        self.update = getattr(m, "UPDATE", None)

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

    # -- Settings and effort (#5169): what the Setup section, the launch dialog and the Settings dialog of a running
    # session draw. Each spec with the contract's defaults filled in.

    def setting_specs(self):
        """Its SETTINGS, each with every key of SETTING_KEYS (the contract's default where the module leaves one
        out)."""
        out = []
        for s in self.settings:
            kind = s["kind"]
            out.append({"label": s["key"].replace("_", " ").capitalize(), "choices": [], "text": "", "flag": [],
                        "live": None, "read": None, "config": True, "session": False, "note": None, **s,
                        "default": s.get("default", "off" if kind == "toggle" else "")})
        # Every agent's default model and effort (#6264: agy_model gemini-4-neon, agy_effort medium): a start that
        # names none takes them (core._launch_agent_cmd). No flag: MaximizePM gives them to build_command as its
        # model and effort. Text, so the launch dialog does not list them under More (it has its own two fields).
        have = {s["key"] for s in out}
        if self.describe.get("prefix"):
            if "model" not in have:
                out.append(self._default_spec("model", "Default model", "the model a start of this agent takes when it "
                                              "names none; empty: the agent's own default"))
            if "effort" not in have and self.effort:
                out.append(self._default_spec("effort", "Default effort", "the effort a start of this agent takes when "
                                              "it names none; empty: the agent's own default"))
        return out

    @staticmethod
    def _default_spec(key, label, text):
        return {"key": key, "label": label, "kind": "text", "choices": [], "default": "", "text": text, "flag": [],
                "live": None, "read": None, "config": True, "session": False, "note": None}

    def options(self):
        """{key: spec} of the settings that are config keys (<prefix><key>): the options of a launch profile
        (@<agent> key=value in launch_agents), the Setup section and the launch dialog."""
        return {s["key"]: s for s in self.setting_specs() if s["config"]}

    def effort_choices(self, levels=None):
        """The effort levels the launch dialog and the Settings dialog offer, as MaximizePM names them, low to high:
        each level of the agent (levels, else EFFORT['levels']) once (#6256). A MaximizePM level that EFFORT['map']
        turns into another level's name (Aider: xhigh and max are high) is no second choice: the level of the same
        name stands for it, else the highest level that maps to it. Every MaximizePM level when the levels come from
        the CLI per model and none is given; [] when the agent has no effort."""
        if not self.effort:
            return []
        levels = self.effort.get("levels") if levels is None else levels
        if not levels:
            return list(MAXPM_EFFORTS)
        pick = {}
        for lv in MAXPM_EFFORTS:
            name = self.effort_name(lv)
            if name in levels and (lv == name or pick.get(name) != name):
                pick[name] = lv
        return [lv for lv in MAXPM_EFFORTS if lv in pick.values()]

    def model_efforts(self, model_id, levels=None):
        """The effort levels (MaximizePM names, low to high) that model model_id takes (#6256): from levels (the
        agent's names, a model dict of read_models), else read_effort_levels(model_id); None when neither knows the
        model (the agent's levels stand), [] when the model takes no effort."""
        if not self.effort:
            return []
        if levels is None:
            f = self._fn("read_effort_levels")
            try:
                levels = f(model_id) if f and model_id else None
            except Exception:  # noqa: BLE001 - a reader of a CLI's files: the agent's levels, not an error page
                levels = None
        if levels is None:
            return None
        return self.effort_choices(list(levels)) if levels else []

    def session_specs(self):
        """The Settings dialog of a running session: the model, the effort, then each setting with session True.
        Each spec says how the dialog reads the value from the command line ("cmd": a regex; "flag": a toggle's
        word), whether it changes in place ("live"), and how a restart applies it ("restart": model, effort, opt (a
        profile option, with its config key in "default") or flag (a word added or taken out)). "fixed": True for a
        setting that neither a live command nor a restart changes (#5285: OpenCode's model; every setting with no live
        command of an agent whose LAUNCH has fixed_on_resume, Hermes); the dialog shows its value only."""
        d = self.describe
        out = [{"key": "model", "label": "Model", "kind": "model", "live": d.get("model_live"), "restart": "model",
                "cmd": d.get("model_read"), "note": d.get("model_note"),
                "fixed": not d.get("model_live") and d.get("model_restart") is False}]
        if self.effort:
            out.append({"key": "effort", "label": self.effort.get("label") or "Effort", "kind": "choice",
                        "choices": self.effort_choices(), "live": self.effort.get("live"), "restart": "effort",
                        "cmd": self.effort.get("read")})
        for s in self.setting_specs():
            if not s["session"]:
                continue
            toggle = s["kind"] == "toggle"
            out.append({"key": s["key"], "label": s["label"], "kind": "choice" if toggle else s["kind"],
                        "choices": ["on", "off"] if toggle else list(s["choices"]), "live": s["live"],
                        "restart": "opt" if s["config"] else "flag",
                        "default": self.config_key(s["key"]) if s["config"] else None, "cmd": s["read"],
                        "flag": s["flag"][0] if toggle and s["flag"] else None, "note": s["note"]})
        for spec in out:  # a resume that keeps every setting (Hermes): only a live command changes one
            spec["fixed"] = bool(spec.get("fixed") or (self.launch.get("fixed_on_resume") and not spec.get("live")))
        return out

    def read_models(self):
        f = self._fn("read_models")
        if f is None:
            return list(self.describe["models"])
        # Once in a page state build (#6394): it reads the CLI's files (a JSON cache, a SQLite file), and the
        # launch options of each entry asked again.
        from .. import core
        got = core._memo(("read_models", self.id, self.source), f)
        return list(got) if got else list(self.describe["models"])

    def context_window(self, model):
        """The tokens of the model's context window (#5224: the context meter), or None when the adapter does not
        know it. context_window(model) -> int | None."""
        f = self._fn("context_window")
        try:
            got = f(model) if f and model else None
        except Exception:  # noqa: BLE001 - a reader of a CLI's files: none, not an error page
            return None
        return int(got) if isinstance(got, (int, float)) and got > 0 else None

    def build_command(self, name, folder, model, effort, settings, prompt):
        f = self._fn("build_command")
        if f is None:
            raise RiverError(f"agent {self.id} has no launch part (build_command), so MaximizePM cannot start it; "
                             f"add it to {self._file()}")
        return list(f(name, folder, model, effort, dict(settings or {}), prompt))

    def parse_command(self, argv):
        f = self._fn("parse_command")
        return f(list(argv)) if f else None

    def trust_workspace(self, folder, command, env=None):
        """Trust a worktree that MaximizePM made before the agent starts there (#6339), via a start flag on `command`
        or the CLI's own trusted-folders file. Returns the command (with any added flags)."""
        f = self._fn("trust_workspace")
        if f is None or not folder:
            return command
        got = f(folder, command, env)
        return command if got is None else got

    def untrust_workspace(self, folder, env=None):
        """Remove a deleted MaximizePM worktree from the CLI's trusted-folders file (#6339), if the adapter added it."""
        f = self._fn("untrust_workspace")
        if f is not None and folder:
            f(folder, env)

    def start_session(self, name, settings=None):
        """The session id that a start with this name and these settings chooses (start_session), or None."""
        f = self._fn("start_session")
        got = f(name, dict(settings or {})) if f else None
        return str(got) if got else None

    def resume_command(self, session_ref, model=None, effort=None, settings=None, name=None):
        f = self._fn("resume_command")
        got = f(session_ref, model, effort, dict(settings or {}), name=name) if f else None
        return list(got) if got else None

    def parse_chat(self, lines):
        f = self._fn("parse_chat")
        return f(list(lines)) if f else ([], None)

    # The chat log readers (#5182). Each one gives the empty answer when the module lacks it.

    def has(self, name):
        """Whether the module has the function `name` (a chat log reader, for example)."""
        return callable(self._fn(name))

    def chat_format(self):
        """How its log is written (CHAT['format']): 'jsonl' (the default) or 'text'."""
        return (self.chat or {}).get("format") or "jsonl"

    def chat_ref(self):
        """What finds the log of a session (CHAT['ref']): 'session', 'address', or None."""
        return (self.chat or {}).get("ref")

    def chat_path(self, session_ref, folder=None, root=None):
        """The log file of a session (a Path), or None; root (a folder that replaces the agent's own log folder) goes to
        the module only when it is given. A module that returns a str gets a Path back: the Sessions tab calls stat()
        on it, and one adapter's str hid every session of the tab (#5260)."""
        f = self._fn("chat_path")
        if f is None or not session_ref:
            return None
        got = f(session_ref, folder, root=root) if root else f(session_ref, folder)
        return Path(got) if got else None

    def parse_chat_line(self, d, line=None):
        """(entries, turn) of one decoded log line; line: its text, for a module with parse_chat only."""
        f = self._fn("parse_chat_line")
        if f is not None:
            return f(d)
        import json
        return self.parse_chat([line if line is not None else json.dumps(d)])

    def chat_chain(self, path, refs=None):
        f = self._fn("chat_chain")
        return list(f(path, list(refs or ()))) if f else [path]

    def chat_title(self, lines):
        f = self._fn("chat_title")
        return f(list(lines)) if f else None

    def chat_requests(self, session_ref, root=None):
        f = self._fn("chat_requests")
        if f is None or not session_ref:
            return None
        return f(session_ref, root=root) if root else f(session_ref)

    def background_scan(self, path, start, st):
        """The byte after the last line that the module's background_scan read into st, or None without the reader
        or for an agent with no background commands (LAUNCH['background'] false: Codex)."""
        f = self._fn("background_scan")
        if f is None or not (self.launch or {}).get("background"):
            return None
        return f(path, start, st)

    def chat_settings(self, lines):
        f = self._fn("chat_settings")
        return dict(f(list(lines)) or {}) if f else {}

    def config_settings(self):
        """What a session takes from the agent's own config (#6280), {} without the reader or when it fails."""
        f = self._fn("config_settings")
        try:
            return {k: v for k, v in dict(f() or {}).items() if v} if f else {}
        except Exception:  # noqa: BLE001 - a reader of a CLI's files: the other sources stand
            return {}

    def settings_read(self):
        """{setting key: (source, ...)}: the module's SETTINGS_READ (#6280)."""
        return {k: tuple(v) for k, v in dict(getattr(self.module, "SETTINGS_READ", None) or {}).items()}

    def screen_settings(self, lines):
        """The settings the session's screen shows now (#6260), {} without the reader or when it fails."""
        f = self._fn("screen_settings")
        try:
            return dict(f(list(lines)) or {}) if f else {}
        except Exception:  # noqa: BLE001 - a reader of a screen: the other sources stand
            return {}

    def chat_usage(self, lines):
        """{"input", "output", "cache_read", "cache_write"} token counts of the lines, or None without the reader."""
        f = self._fn("chat_usage")
        got = f(list(lines)) if f else None
        return dict(got) if got else None

    def chat_limits(self, lines):
        """The plan limits the chat log says last (#5224): [{"name", "used_percent", "remaining_percent",
        "window_minutes", "resets_at" (ISO or None)}], {"credits": text} entries too; None when the agent writes none
        to its log (they show only on its screen). chat_limits(lines) -> list | None."""
        f = self._fn("chat_limits")
        got = f(list(lines)) if f else None
        return _norm_limits(got) if got is not None else None

    def screen_limits(self, lines, now=None):
        """The plan or quota limits shown on the session's screen (/usage, /status, #6348), in the same shape as
        chat_limits; None when the screen shows none or the adapter has no reader."""
        f = self._fn("screen_limits")
        if f is None:
            return None
        try:
            got = f(list(lines), now=now) if "now" in f.__code__.co_varnames[:f.__code__.co_argcount] else f(list(lines))
        except Exception:  # noqa: BLE001 - a reader of a screen: none, not a loop error
            return None
        return _norm_limits(got) if got else None

    def model_group(self, model):
        """The quota group of `model` (#6382), or None without the reader or for a model of no group."""
        f = self._fn("model_group")
        try:
            return (f(model) or None) if f and model else None
        except Exception:  # noqa: BLE001 - a reader of a name: the whole agent counts
            return None

    def latest_limits(self, root=None):
        """The plan limits that the agent wrote last to any of its logs (#6348: Codex), in the shape of chat_limits,
        read also when no session of the agent runs; None when the adapter has no reader or no log has them."""
        f = self._fn("latest_limits")
        if f is None:
            return None
        try:
            got = f(root=root)
        except Exception:  # noqa: BLE001 - a reader of the agent's logs: none, not a loop error
            return None
        return _norm_limits(got) if got else None

    def limit_stop(self, lines, now=None):
        """Whether the session's screen shows that it stopped at its usage/quota limit (#6348):
        {"message": str, "resets_at": ISO | None, "resets_text": str | None}, or None."""
        f = self._fn("limit_stop")
        if f is not None:
            try:
                return f(list(lines), now=now) if "now" in f.__code__.co_varnames[:f.__code__.co_argcount] else f(list(lines))
            except Exception:  # noqa: BLE001 - a reader of a screen: none, not a loop error
                return None
        pats = (self.detect or {}).get("limit")
        return match_limit_stop(lines, pats, now=now) if pats else None

    def model_refused(self, lines):
        """Whether the session's screen shows that the provider refuses its model or account (DETECT['refused'],
        #6429): {"message": the error's words, "model": the model it names or None}, or None. The lines are read
        as one text, since a long error wraps over several lines of the pane."""
        pats = (self.detect or {}).get("refused")
        if not pats:
            return None
        text = re.sub(r"\s+", " ", " ".join(str(x).strip() for x in lines or ()))
        for p in pats:
            m = re.search(p, text)
            if m:
                return {"message": text[m.start():m.end() + 80].strip()[:240], "model": m.groupdict().get("model")}
        return None

    def chat_cache_line(self, raw, st):
        f = self._fn("chat_cache_line")
        if f is not None:
            f(raw, st)

    def chat_texts(self, raw):
        f = self._fn("chat_texts")
        return list(f(raw) or ()) if f else []

    def read_commands(self, folder):
        f = self._fn("read_commands")
        return list(self.commands) + (list(f(folder) or ()) if f else [])

    def install_hooks(self, folder, report):
        f = self._fn("install_hooks")
        return f(folder, list(report)) if f else None

    def project_hooks(self, folder, report, on):
        f = self._fn("project_hooks")
        return f(folder, list(report), bool(on)) if f and folder else None

    def hook_answer(self, res):
        f = self._fn("hook_answer")
        return f(dict(res)) if f else None

    def read_hook(self, payload):
        """What a hook's own input says (a hook report, HOOK_REPORT_KEYS, with a default for each key left out), or
        None when the agent has no read_hook or the input is no event of it."""
        f = self._fn("read_hook")
        got = f(payload if isinstance(payload, dict) else {}) if f else None
        if not got:
            return None
        return {"state": None, "event": "", "session_id": None, "message": None, "call": None, "shows": False,
                "waits": None, "prompt": None, "again": False, "run": None, **got}

    def layout(self):
        """Its DETECT with the layout keys it leaves out (DETECT_LAYOUT): what the pane watcher reads a screen with."""
        return {**DETECT_LAYOUT, **(self.detect or {})}

    def write_mcp_config(self, path=None, remove=False):
        f = self._fn("write_mcp_config")
        if f is None:
            raise RiverError(f"agent {self.id} has no MCP config writer (write_mcp_config); add it to {self._file()}")
        return f(path, remove)

    def desktop_app(self):
        """Its desktop chat app on this computer ({name, added, path}), or None."""
        f = self._fn("desktop_app")
        return f() if f else None

    def write_guard(self, path=None, remove=False):
        f = self._fn("write_guard")
        if f is None:
            raise RiverError(f"agent {self.id} has no queue guard for sessions started by hand: its sandbox takes "
                             f"the rule only at each start, which MaximizePM adds itself")
        return f(path, remove)

    def install_guard(self, folder=None):
        """The words that load the queue guard (#6412) for a start without MaximizePM's hooks file, or []."""
        f = self._fn("install_guard")
        return [str(w) for w in f(folder) or ()] if f else []

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
    """The override folder and its files with their times. Inside a page state build (core.BUILD_MEMO), read once:
    a build looked up adapters about 400 times, and each lookup made a stat of the folder (#6394)."""
    from .. import core
    if core.BUILD_MEMO.get() is not None:
        return core._memo("agents._key", _key_now)
    return _key_now()


def _key_now():
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
    it by hand (a name that no agent has counts as none). The order is env_order(): a Codex started from a Claude Code
    shell keeps CLAUDECODE."""
    env = os.environ if env is None else env
    named = find(env.get("MAXPM_AGENT_TYPE"))
    if named:
        return named
    got = load()
    return next((got[m] for m in env_order() if got[m].inside(env)), None)


# The agents whose environment another agent started from their shell keeps: they come last when MaximizePM tells
# which agent a command runs inside (from_env, core.session_id_from_env). A Codex started from a Claude Code shell
# keeps CLAUDECODE and CLAUDE_CODE_SESSION_ID, so claude is last of all.
ENV_LAST = ("codex", "claude")


def env_order():
    """The ids of the registered agents in the order from_env tests their environment: every other agent by id, then
    ENV_LAST."""
    got = load()
    return sorted(m for m in got if m not in ENV_LAST) + [m for m in ENV_LAST if m in got]


def by_exe(exe):
    """The agent whose program (DESCRIBE['exe']) is the last part of `exe` (any case), or None; built-in agents
    first."""
    n = Path(exe or "").name.lower()
    got = load()
    return next((got[m] for m in ids() if n and got[m].describe["exe"].lower() == n), None) if n else None


# ---------------------------------------------------------------------------------------------------------------
# Setup (#5184): the instruction files of a project folder, the MCP server entry, and what the page reads
# ---------------------------------------------------------------------------------------------------------------

# The instruction file that most agents read in a project folder (Claude Code, Codex, Antigravity CLI, OpenCode, Amp
# and others).
SHARED_INSTRUCTIONS = "AGENTS.md"


def replaced_files():
    """[(old, label)]: the instruction file that an agent read before it read SHARED_INSTRUCTIONS (SETUP["replaces"]),
    and that agent's label, one entry per file, in registry order. maxpm init moves the rules of each one into
    SHARED_INSTRUCTIONS and deletes it."""
    out, seen = [], set()
    for m in ids():
        a = load()[m]
        old = (a.setup or {}).get("replaces")
        if old and old != SHARED_INSTRUCTIONS and old not in seen:
            seen.add(old)
            out.append((old, a.label))
    return out


def setup_entry():
    """The MCP server entry {command, args, env} that a chat app or an agent CLI runs: this Python and this MaximizePM
    by full path (an app does not have the shell's PATH), the queue this command uses, and MAXPM_CHAT=1 (a chat has
    no folder)."""
    import shutil
    import sys
    from .. import core
    script = Path(__file__).resolve().parent.parent.parent / "bin" / "maxpm"
    if script.is_file():
        cmd, args = sys.executable, [str(script), "mcp"]
    elif shutil.which("maxpm"):
        cmd, args = str(Path(shutil.which("maxpm")).resolve()), ["mcp"]
    else:
        raise RiverError("cannot find the maxpm program to give the Claude desktop app; install MaximizePM first")
    return {"command": cmd, "args": args,
            "env": {"MAXPM_DB": str(core.db_path().expanduser().resolve()), "MAXPM_CHAT": "1"}}


def page_list():
    """What the page reads of each agent (GET /api/agents), so that it tests no agent name: its describe part, the
    parts it supports, its launch capabilities and its setup texts. "main": the first registered agent (Claude Code),
    whose sessions carry no agent tag on the Sessions tab."""
    got = load()
    out = []
    for i, m in enumerate(ids()):
        a = got[m]
        d = a.describe
        out.append({"id": a.id, "label": a.label, "exe": d["exe"], "names": a.names(), "family": d["family"],
                    "models": list(d["models"]), "default_model": d["default_model"], "prefix": d["prefix"],
                    "parts": {p: a.supports(p) for p in PARTS}, "launch": dict(a.launch),
                    "setup": dict(a.setup or {}), "main": i == 0,
                    "chat_commands": (a.chat or {}).get("commands", True),
                    "interrupt": list(a.input.get("interrupt") or []),
                    "interrupt_again": a.input.get("interrupt_again")})
    return {"agents": out, "errors": errors()}


# ---------------------------------------------------------------------------------------------------------------
# Tier 1 (#6252, mark 2026-11-19: 'we should add it and have a definitive feature set')
# ---------------------------------------------------------------------------------------------------------------

# The tier 1 agents, by adapter id. Each one has every feature of the Tier 1 section of docs/agent-adapters.md;
# tests/test_agent_tier1.py fails when one lacks a feature that the section does not name as an exception. The setup
# guide shows them first (Choose an agent).
TIER1 = ("claude", "codex", "antigravity", "hermes")


def is_tier1(name):
    """Whether `name` (an id, an alias or a platform name: claude-code) names a registered tier 1 agent."""
    a = find(name)
    return bool(a) and a.id in TIER1


# ---------------------------------------------------------------------------------------------------------------
# Usage limits & reset-time parsing (#6348)
# ---------------------------------------------------------------------------------------------------------------

_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_ISO_RX = re.compile(r"\b(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2}))\b")
_REL_RX = re.compile(
    r"(?:resets?\s+in|refreshes\s+in|try\s+again\s+in|available\s+in|wait)\s+~?\s*"
    r"((?:\d+\s*(?:d(?:ays?)?|h(?:ours?|rs?)?|m(?:in(?:ute)?s?)?|s(?:ec(?:ond)?s?)?)\s*)+)",
    re.I,
)
_REL_PART = re.compile(r"(\d+)\s*(d(?:ays?)?|h(?:ours?|rs?)?|m(?:in(?:ute)?s?)?|s(?:ec(?:ond)?s?)?)", re.I)
_TZ_RX = re.compile(r"\(([A-Za-z_]+(?:/[A-Za-z0-9_+-]+)*)\)")
_DATE_TIME_RX = re.compile(
    r"(?:resets?(?:\s+at)?|try\s+again\s+at)\s+"
    r"(?:(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?P<day>\d{1,2})(?:,?\s*(?:at\s+)?)?)?"
    r"(?:(?P<hour>\d{1,2})(?::(?P<min>\d{2}))?\s*(?P<ampm>am|pm)\b|(?P<h24>\d{1,2}):(?P<m24>\d{2})\b)?"
    # Codex /status: '(resets 2:46 PM on 26 Nov)'
    r"(?:\s+on\s+(?P<day2>\d{1,2})\s+(?P<mon2>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b)?",
    re.I,
)


def parse_reset_time(text, now=None):
    """Parse a reset time from a CLI usage or limit-stop message (#6348).
    Returns (resets_at_iso_utc_or_None, resets_text_or_None)."""
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    s = " ".join(str(text or "").split())
    if not s:
        return None, None
    if now is None:
        from .. import core
        now = core.now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    # 1. Exact ISO timestamp
    m = _ISO_RX.search(s)
    if m:
        raw = m.group(1)
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ"), raw
        except ValueError:
            pass
    # 2. Relative duration: 'Resets in ~30m', 'resets in 2h 15m', 'Try again in 45 minutes'
    m = _REL_RX.search(s)
    if m:
        span = m.group(1).strip()
        secs = 0
        for num, unit in _REL_PART.findall(span):
            u = unit[0].lower()
            secs += int(num) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[u]
        if secs > 0:
            dt = (now + timedelta(seconds=secs)).astimezone(timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ"), f"in {span}"
    # 3. Date / clock time: 'resets 3pm (America/Los_Angeles)', 'resets Nov 25, 3pm', 'resets 14:30'
    m = _DATE_TIME_RX.search(s)
    if m and (m.group("hour") or m.group("h24") or m.group("mon")):
        tz_m = _TZ_RX.search(s[m.start():])
        # A clock time with no zone is the computer's own time (Codex: 'resets 2:09 PM').
        from .. import core
        tz = core._machine_zone()
        if tz_m:
            try:
                tz = ZoneInfo(tz_m.group(1))
            except Exception:  # noqa: BLE001 - '(Sonnet only)' is no zone
                tz_m = None
        local_now = now.astimezone(tz)
        hour = minute = 0
        if m.group("hour"):
            hour = int(m.group("hour"))
            minute = int(m.group("min") or 0)
            ampm = m.group("ampm").lower()
            if ampm == "pm" and hour != 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0
        elif m.group("h24"):
            hour, minute = int(m.group("h24")), int(m.group("m24"))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            try:
                if m.group("mon") or m.group("mon2"):
                    mon = _MONTHS[(m.group("mon") or m.group("mon2")).lower()[:3]]
                    day = int(m.group("day") or m.group("day2"))
                    year = local_now.year
                    cand = datetime(year, mon, day, hour, minute, tzinfo=tz)
                    if cand < local_now - timedelta(hours=1):
                        cand = datetime(year + 1, mon, day, hour, minute, tzinfo=tz)
                else:
                    cand = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    if cand <= local_now:
                        cand += timedelta(days=1)
                raw = m.group(0).strip()
                if tz_m and tz_m.group(0) not in raw:
                    raw += f" {tz_m.group(0)}"
                return cand.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), raw
            except ValueError:
                pass
    return None, None


def match_limit_stop(lines, patterns, now=None):
    """Scan pane `lines` for any regex in `patterns`. Returns {"message", "resets_at", "resets_text"} or None."""
    if not patterns:
        return None
    seq = [str(ln) for ln in (lines or ())]
    compiled = [re.compile(p) for p in patterns]
    for i in range(len(seq) - 1, -1, -1):
        line = seq[i].strip()
        if not line:
            continue
        if any(rx.search(line) for rx in compiled):
            # Include up to 2 neighboring lines when reset time appears on the next line.
            chunk = " ".join(x.strip() for x in seq[i:min(len(seq), i + 3)] if x.strip())
            resets_at, resets_text = parse_reset_time(chunk, now=now)
            msg = line
            if resets_text and resets_text.lower() not in msg.lower() and i + 1 < len(seq):
                nxt = seq[i + 1].strip()
                if nxt and re.search(r"(?i)reset|try again|available", nxt):
                    msg = f"{msg} · {nxt}"
            return {"message": msg[:240], "resets_at": resets_at, "resets_text": resets_text}
    return None


def _norm_limits(rows):
    """Normalize plan/quota limit rows so every percentage row carries both used_percent and remaining_percent."""
    out = []
    for r in rows or ():
        if not isinstance(r, dict):
            continue
        d = dict(r)
        if d.get("used_percent") is not None and d.get("remaining_percent") is None:
            d["used_percent"] = round(max(0.0, min(100.0, float(d["used_percent"]))), 1)
            d["remaining_percent"] = round(max(0.0, 100.0 - d["used_percent"]), 1)
        elif d.get("remaining_percent") is not None and d.get("used_percent") is None:
            d["remaining_percent"] = round(max(0.0, min(100.0, float(d["remaining_percent"]))), 1)
            d["used_percent"] = round(max(0.0, 100.0 - d["remaining_percent"]), 1)
        out.append(d)
    return out


__all__ = ["Adapter", "Raw", "BUILTIN", "PARTS", "STATES", "DETECT_LAYOUT", "HOOK_REPORT_KEYS", "INTERRUPT_KEYS", "MAXPM_EFFORTS", "FAMILIES", "check", "load", "errors", "ids",
           "find", "get", "from_env", "override_dir", "SHARED_INSTRUCTIONS", "replaced_files",
           "setup_entry", "page_list", "ENV_LAST", "env_order", "by_exe", "TIER1", "is_tier1",
           "parse_reset_time", "match_limit_stop"]


# Last, after every name above: core imports the built-in adapters while it loads (their setting defaults), and they import
# Raw from here. With this import at the top, a program that imported river.agents first got a half-loaded package
# when core asked for Raw, and failed (#5207). Here, either module may be imported first.
from ..core import RiverError, river_dir  # noqa: E402
