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
#      "platform":      the name that settings, launch profiles (@claude-code) and a session's agent type store, when
#                       it is not the id (Claude Code: "claude-code"); it must be the id or an alias (#5185),
#    }
# read_models() -> [model name or CLI id] | None: an optional reader of the models the CLI knows now (Codex:
# ~/.codex/models_cache.json); None when it cannot read them, and DESCRIBE["models"] stands.
DESCRIBE_KEYS = {"id": str, "label": str, "exe": str, "aliases": (list, tuple), "family": str,
                 "models": (list, tuple), "default_model": str, "model_ids": dict, "prefix": str,
                 "model_live": (str, type(None)), "model_read": (str, type(None)), "platform": str}
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
                       "autocompact", "background", "fixed_on_resume")
#    session_name: it takes a session name; system_prompt_file: the maxpm skill goes into its system prompt;
#    hooks_file: MaximizePM's hooks file goes on its command line; extra_dirs: the queue folder goes on its command
#    line as a folder it may write; fork: a session starts as a fork of another one; resume: build_command resumes a
#    session (resume_command uses it); autocompact: it compacts at a token count; background: it runs a command in
#    the background and wakes the session when it ends (maxpm wait runs there, not in the foreground);
#    fixed_on_resume: a resume keeps the settings the session started with and refuses a flag that differs (Hermes),
#    so resume_command passes none, and the Settings dialog changes them live or not at all (#5252).
# The MaximizePM keys in settings, each only when the caller wants it and LAUNCH has its capability:
LAUNCH_KEYS = {
    "system_prompt_file": "True: put the maxpm skill into the system prompt",
    "hooks_file": "the path of MaximizePM's hooks file",
    "fork": "the session id to start a fork of",
    "resume": "the session id to resume (resume_command)",
    "autocompact": "the token count to compact at",
    "noflags": "the words a person took out of the args setting (they stay out of what the adapter adds itself)",
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
#      "log_glob":   the glob of the new log file in a known folder ("log" source; ~ expands), or None,
#      "pane":       a regex whose group 1 is the id in the pane's text ("pane" source), or None,
#      "url":        its web link with {id} ("https://.../{id}"), or one link for every session (Codex's web app), or None,
#      "inside_env": the variables that tell that a command runs inside this agent (CLAUDECODE, CODEX_THREAD_ID);
#                    this replaces core.agent_type_from_env,
#      "id_pattern": a regex that the whole id must match, or None for SAFE_ID (#5212: an id from the environment
#                    names a log file, so it never holds a path or a glob),
#      "process":    which process is the session's own (agents.pid: maxpm who, the busy checks, maxpm stop --kill):
#                    "ancestor" (the default), the first process above a maxpm command's shells; "terminal", that
#                    process only when it runs on a terminal, else none (#5247: Hermes runs the tools of every
#                    session in one background gateway with no terminal, so the ancestor is that shared gateway),
#    }
# With source "env" and CHAT["ref"] "session", each maxpm command records the id it finds in "env" for the acting
# agent (core.session_from_env, agent_sessions): the Sessions tab then finds the agent's chat log by it (#5212).
IDENTITY_SOURCES = ("hook", "env", "log", "pane")
IDENTITY_KEYS = {"source": str, "env": (str, type(None)), "log_glob": (str, type(None)), "pane": (str, type(None)),
                 "url": (str, type(None)), "inside_env": (list, tuple), "id_pattern": (str, type(None)),
                 "process": (str, type(None))}
IDENTITY_PROCESS = ("ancestor", "terminal")
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
#     each model call once, and fork True when the session started as a fork of another (core.measure_usage, the
#     size of a goal base);
#   chat_settings(lines) -> {setting key: value} as the session runs now;
#   chat_cache_line(raw, st): fold one log line (bytes) into the prompt cache state st (a dict that starts as
#     {"last": None, "ttl": None, "model": None}; the reader may keep keys of its own there): st["last"] = {"start",
#     "at", "context"} of the last model call (when it started, its last line, the tokens of its context),
#     st["ttl"] the cache lifetime the log shows ("1h", "5m"; None: the cache_ttl setting), st["model"];
#   chat_texts(raw) -> [(text, time, how)]: the texts of one log line (bytes) where the Artifacts tab looks for files
#     and links; how is "wrote" for the path of a CHAT["file_tools"] call, else "named". Tool results are left out.
# An entry is a dict with "at" (ISO time or None), "role" (one of ENTRY_ROLES) and "text", and by role:
#   user: "images" (a list of paths or data URLs) when it has some; tool: "id" (the call id), "name", "detail";
#   result: "for" (the call id), "error" (bool); divider: "kind" ("compacted", "resumed").
ENTRY_ROLES = ("assistant", "user", "tool", "result", "divider", "summary", "event")
TURNS = ("working", "idle")
CHAT_REFS = ("session", "address")
CHAT_FORMATS = ("jsonl", "text")
CHAT_KEYS = {"compressed": bool, "file_tools": (list, tuple), "ref": str, "cache_write": bool, "format": str,
             "block": (str, type(None))}

# 7. State. STATE = {
#      "state":      the one source of the state: "hook" (install_hooks puts a hook or plugin in place that calls
#                    maxpm report) or "detect" (maxpm's pane watcher applies DETECT and calls maxpm report),
#      "session_id": the one source of the session id: "hook" (the report carries --session-id) or IDENTITY,
#    }
# No chain of fallbacks: each signal has one declared source.
# install_hooks(folder, report) -> [argv words to add to the start] | None: puts the hooks in place (core.with_hooks
# puts words that start with env before the command, other words right after the program); `report` is the
# maxpm report command (a list of argv words) that each hook runs with its --state.
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
#    }
# DETECT = {
#      "process":      the process name of the agent in the pane,
#      "process_args": optional: a regex that finds the agent in the command line (ps args) of a process on the
#                      pane's terminal, for a CLI whose launcher shows neither its name nor a script (Hermes runs
#                      'python3 -I -c <script>'); the pane watcher tries it after "process",
#      "idle":         a regex of the pane when the agent waits at its prompt,
#      "working":      a regex of the pane while it runs a turn ("esc to interrupt"),
#      "blocked":      [regexes], one for each screen that waits for a person (a permission menu, a question, trust),
#      "tell_instead": a regex of the menu option that lets the person type an answer instead, or None,
#      "image_paste":  how a pasted image shows in its prompt, with {n} ("[Image #{n}]"), or None,
#      and the layout of a blocked screen, each optional (DETECT_LAYOUT holds the values of a key left out):
#      "menu":         the characters that mark the selected option of a menu ("❯›"),
#      "other":        the characters that mark each other option, at the column of the selected one's mark (cliclack:
#                      "●" selected, "○" each other one); "": the other options have no mark, and their text starts
#                      at the column of the selected option's text (Claude Code, Codex),
#      "frame":        the characters of the frame some CLIs draw around a prompt,
#      "rule":         a regex of a dashed rule (the input box's edge),
#      "hint":         a regex of the line that names keys ("Enter to confirm · Esc to cancel"),
#    }
# The pane watcher of maxpm serve (server.watch_prompts) reads each agent's pane with its DETECT: a blocked screen
# alerts the person (with its question and options) for every agent; for an agent whose STATE["state"] is
# "detect" it also reports the state (blocked, working, idle) as maxpm report does.
STATE_SOURCES = ("hook", "detect")
STATE_KEYS = {"state": str, "session_id": str}
HOOK_REPORT_KEYS = {"state": (str, type(None)), "event": str, "session_id": (str, type(None)),
                    "message": (str, type(None)), "call": (dict, type(None)), "shows": bool,
                    "waits": (list, type(None)), "prompt": (str, type(None)), "again": bool}
DETECT_KEYS = {"process": str, "process_args": str, "idle": str, "working": str, "blocked": (list, tuple),
               "tell_instead": (str, type(None)), "image_paste": (str, type(None)),
               "menu": str, "other": str, "frame": str, "rule": str, "hint": str}
DETECT_LAYOUT = {
    "menu": "❯›",
    "other": "",
    "frame": "│┃╭╮╰╯┌┐└┘",
    "rule": r"^\s*[─━╌╍┄┅═-]{8,}\s*$",
    "hint": r"\b(enter|esc|return|tab)\b.*\bto (confirm|cancel|select|continue|go back)\b|\[[yY]/[nN]\]",
}

# 8. Input. tmux paste is the same for every agent; the image paste rule and the "tell it instead" text are in
# DETECT. INPUT = {"native": argv words with {address} and {text} for a native message command (Codex:
# codex queue --thread {address} {text}), or None}.
INPUT_KEYS = {"native": (list, tuple, type(None))}

# 9. Resume. resume_command(session_ref, model, effort, settings, name=None) -> argv, or None when it cannot resume that
# session. The arguments are those of build_command; settings["prompt"] is the first prompt after the resume.

# 10. Commands. COMMANDS = [{"name": "compact" (no "/"), "description": str, "hint": the argument hint or ""}, ...]:
# the built-in slash commands. read_commands(folder) -> the same shape: its custom commands for a session that runs in
# folder (Claude: ~/.claude/commands, .claude/commands, the skills, plugin commands; Codex: ~/.codex/prompts).
COMMAND_KEYS = {"name": str, "description": str, "hint": str}

# 11. Setup (#5184). SETUP = {
#      "instructions": the instruction file it reads in a project folder (CLAUDE.md, AGENTS.md, GEMINI.md),
#      "import":       the line with {file} by which its instruction file imports another one ("@{file}"), or None;
#                      an agent whose file is not SHARED_INSTRUCTIONS and that has it reads AGENTS.md through it,
#      "skills_dir":   the folder of its skills (~ expands), or None,
#      "voice":        what its own voice mode does, one sentence, or None,
#      "open_link":    the label of the page's button that opens a session's web link (IDENTITY["url"]), or None,
#      "open_title":   that button's tooltip, or None,
#      "no_link":      why the page cannot open a session of it that runs in no tmux pane here, or None,
#    }
# write_mcp_config(path=None, remove=False) -> one text that says what changed: adds MaximizePM's MCP server (maxpm
# mcp, setup_entry()) to the agent's config file (path: another file than its own), or takes it out again.
SETUP_KEYS = {"instructions": str, "import": (str, type(None)), "skills_dir": (str, type(None)),
              "voice": (str, type(None)), "open_link": (str, type(None)), "open_title": (str, type(None)),
              "no_link": (str, type(None))}

# Each part: the module names that make it up, and the dict whose keys check (name, shape).
PARTS = {
    "describe": {"data": ("DESCRIBE", DESCRIBE_KEYS), "functions": ("read_models",), "required": True},
    "settings": {"data": ("SETTINGS", SETTING_KEYS), "functions": ()},
    "effort": {"data": ("EFFORT", EFFORT_KEYS), "functions": ("read_effort_levels", "read_effort")},
    "launch": {"data": ("LAUNCH", None), "functions": ("build_command", "parse_command"), "required": True},
    "identity": {"data": ("IDENTITY", IDENTITY_KEYS), "functions": ()},
    "chat": {"data": ("CHAT", CHAT_KEYS), "functions": ("chat_path", "parse_chat", "parse_chat_line", "chat_chain",
                                                        "chat_title", "chat_cwd", "chat_usage", "chat_requests",
                                                        "chat_settings", "chat_cache_line", "chat_texts")},
    "state": {"data": ("STATE", STATE_KEYS), "functions": ("install_hooks", "read_hook")},
    "detect": {"data": ("DETECT", DETECT_KEYS), "functions": ()},
    "input": {"data": ("INPUT", INPUT_KEYS), "functions": ()},
    "resume": {"data": None, "functions": ("resume_command",)},
    "commands": {"data": ("COMMANDS", COMMAND_KEYS), "functions": ("read_commands",)},
    "setup": {"data": ("SETUP", SETUP_KEYS), "functions": ("write_mcp_config",)},
}

BUILTIN = ("claude", "codex", "pi", "copilot", "hermes", "gemini", "grok", "opencode", "goose", "aider", "amp", "cursor")
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
            for k in ("idle", "working", "rule", "hint", "tell_instead", "process_args"):
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
        self.first_prompt = getattr(m, "FIRST_PROMPT", "argv")
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
        return out

    def options(self):
        """{key: spec} of the settings that are config keys (<prefix><key>): the options of a launch profile
        (@<agent> key=value in launch_agents), the Setup section and the launch dialog."""
        return {s["key"]: s for s in self.setting_specs() if s["config"]}

    def effort_choices(self):
        """The effort levels the launch dialog and the Settings dialog offer, as MaximizePM names them: each level of
        MaximizePM whose name for the agent (EFFORT['map'], else the same name) is one of EFFORT['levels'], or every
        level when the levels come from the CLI per model; [] when the agent has no effort."""
        if not self.effort:
            return []
        levels = self.effort.get("levels")
        if not levels:
            return list(MAXPM_EFFORTS)
        return [lv for lv in MAXPM_EFFORTS if self.effort_name(lv) in levels]

    def session_specs(self):
        """The Settings dialog of a running session: the model, the effort, then each setting with session True.
        Each spec says how the dialog reads the value from the command line ("cmd": a regex; "flag": a toggle's
        word), whether it changes in place ("live"), and how a restart applies it ("restart": model, effort, opt (a
        profile option, with its config key in "default") or flag (a word added or taken out))."""
        d = self.describe
        out = [{"key": "model", "label": "Model", "kind": "model", "live": d.get("model_live"), "restart": "model",
                "cmd": d.get("model_read")}]
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
        return out

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

    def chat_settings(self, lines):
        f = self._fn("chat_settings")
        return dict(f(list(lines)) or {}) if f else {}

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

    def read_hook(self, payload):
        """What a hook's own input says (a hook report, HOOK_REPORT_KEYS, with a default for each key left out), or
        None when the agent has no read_hook or the input is no event of it."""
        f = self._fn("read_hook")
        got = f(payload if isinstance(payload, dict) else {}) if f else None
        if not got:
            return None
        return {"state": None, "event": "", "session_id": None, "message": None, "call": None, "shows": False,
                "waits": None, "prompt": None, "again": False, **got}

    def layout(self):
        """Its DETECT with the layout keys it leaves out (DETECT_LAYOUT): what the pane watcher reads a screen with."""
        return {**DETECT_LAYOUT, **(self.detect or {})}

    def write_mcp_config(self, path=None, remove=False):
        f = self._fn("write_mcp_config")
        if f is None:
            raise RiverError(f"agent {self.id} has no MCP config writer (write_mcp_config); add it to {self._file()}")
        return f(path, remove)

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

# The instruction file that most agents read in a project folder (Codex, OpenCode, Amp and others). An agent with its
# own file and an import line (SETUP["import"]) reads it through that line: Claude Code's CLAUDE.md is "@AGENTS.md".
SHARED_INSTRUCTIONS = "AGENTS.md"


def instruction_files():
    """(own, shared, line, label): the instruction file of the first agent that imports the shared file, the shared
    file, the import line that its own file holds, and that agent's label; ('CLAUDE.md', 'AGENTS.md', '@AGENTS.md',
    'Claude Code') with the built-in agents."""
    for m in ids():
        a = load()[m]
        st = a.setup or {}
        if st.get("import") and st.get("instructions") not in (None, SHARED_INSTRUCTIONS):
            return st["instructions"], SHARED_INSTRUCTIONS, st["import"].format(file=SHARED_INSTRUCTIONS), a.label
    raise RiverError("no registered agent imports " + SHARED_INSTRUCTIONS)  # a defect: claude.py has it


def import_files():
    """[(own, line, label)] for every agent whose own instruction file reads SHARED_INSTRUCTIONS through an import line
    (SETUP["import"]), one entry per file, in registry order: ('CLAUDE.md', '@AGENTS.md', 'Claude Code') first, then
    ('GEMINI.md', '@AGENTS.md', 'Gemini CLI') and any other (#5218). maxpm init writes each of them."""
    out, seen = [], set()
    for m in ids():
        a = load()[m]
        st = a.setup or {}
        own = st.get("instructions")
        if st.get("import") and own not in (None, SHARED_INSTRUCTIONS) and own not in seen:
            seen.add(own)
            out.append((own, st["import"].format(file=SHARED_INSTRUCTIONS), a.label))
    return out


def shared_readers():
    """The labels of the agents that read SHARED_INSTRUCTIONS itself ('Codex' with the built-in agents)."""
    got = load()
    return [got[m].label for m in ids() if (got[m].setup or {}).get("instructions") == SHARED_INSTRUCTIONS]


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
                    "setup": dict(a.setup or {}), "main": i == 0})
    return {"agents": out, "errors": errors()}


__all__ = ["Adapter", "Raw", "BUILTIN", "PARTS", "STATES", "DETECT_LAYOUT", "HOOK_REPORT_KEYS", "MAXPM_EFFORTS", "FAMILIES", "check", "load", "errors", "ids",
           "find", "get", "from_env", "override_dir", "SHARED_INSTRUCTIONS", "instruction_files", "import_files", "shared_readers",
           "setup_entry", "page_list", "ENV_LAST", "env_order", "by_exe"]


# Last, after every name above: core imports the built-in adapters while it loads (their setting defaults), and they import
# Raw from here. With this import at the top, a program that imported river.agents first got a half-loaded package
# when core asked for Raw, and failed (#5207). Here, either module may be imported first.
from ..core import RiverError, river_dir  # noqa: E402
