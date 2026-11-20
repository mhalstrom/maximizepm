"""The Antigravity CLI adapter (#6248): Google's coding agent, the command `agy`, the successor of Gemini CLI (Google
retired Gemini CLI for free, AI Pro and AI Ultra accounts on 2026-06-18). mark 2026-11-19: one of the three main
agents, with Claude Code and Codex.

Read from Google's pages (antigravity.google/docs: getting-started, install, cli/reference, cli/headless,
cli/conversations, hooks, customization-discovery; 2026-11-19) and measured on agy 1.5.13 in a tmux pane on this Mac
(tests/fixtures/agy: the screens and a cleaned transcript of that run).

The sources this adapter declares, one for each signal:

- State: the screen (DETECT). agy has hooks (Stop with fullyIdle, PreToolUse), but only in hooks.json files in its own
  folder (~/.gemini/config/hooks.json) or in the project (.agents/hooks.json); no flag of one start adds one, and
  MaximizePM puts nothing into a CLI's home (p140). The screen shows each state: a spinner line while it works, a
  menu with its question while it waits for the person, '? for shortcuts' under the input line when it waits for a
  prompt. With the setting agy_activity_hooks on (#6351, section 7b), MaximizePM writes its hooks into the project's
  .agents/hooks.json, and they report each turn's start and end too.
- Session id: the environment (IDENTITY). agy gives each shell command it runs ANTIGRAVITY_AGENT=1 and
  ANTIGRAVITY_CONVERSATION_ID=<the conversation's id>, so each maxpm command records the id (core.session_from_env).
- Chat log: ~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript_full.jsonl, one JSON line for each
  step of the conversation. A resume (--conversation <id>) adds to the same file.

Guards before a command: Claude Code's PreToolUse hook refuses a push while an alert is unread (core.push_guard) and
a tmux kill-server of MaximizePM's server (core.tmux_guard). agy has a PreToolUse hook too, but it loads hooks only
from hooks.json files in its home or the project, where an adapter installs nothing (p140). An agy session gets the
tmux guard as deny rules of its --settings instead (SETTINGS_JSON, #6251). push_guard has no such form: it reads the
queue's state (an unread alert, a blocked item) when the command runs, and a fixed rule cannot. With the setting
agy_activity_hooks on (#6351), the project's hooks file has MaximizePM's PreToolUse hook, which runs both guards.
"""

import json
import os
import re
import shlex
from pathlib import Path

from . import CliList, Raw, model_tier, parse_reset_time

# ---------------------------------------------------------------------------------------------------------------
# 1. Describe
# ---------------------------------------------------------------------------------------------------------------

DESCRIBE = {
    "id": "antigravity",
    "label": "Antigravity CLI",
    "exe": "agy",
    "aliases": ["agy", "antigravity-cli"],
    # agy runs Google's Gemini models and others (Claude, GPT-OSS) by its own ids: no family of model_ladder, so an
    # item's --min-model and --max-model do not apply to them, as for a provider/model agent.
    "family": "multi",
    # No list of its own: read_models gives the models of the installed agy, one per model (agy lists each model once
    # for each effort level, "gemini-4-neon-high"; the launch gives the level with --effort, #6256), and the list of
    # 2026-11-19 (MODELS_SEEN) when agy does not answer. A model that agy no longer lists does not show.
    "models": [],
    # Neon, Carbon, Argon and Hydrogen: no Google page found on 2026-11-19 ranks the four, so they keep the order of
    # `agy models` (#6256).
    "tiers": ["neon", "carbon", "argon", "hydrogen"],
    "default_model": "",
    "model_ids": {},
    "prefix": "agy_",
    # agy's /model keeps the new model for later sessions too ("persists across sessions", cli/reference): typed
    # from the page it would change the person's default. A restart with --model changes this session only.
    "model_live": None,
    "model_note": "A restart applies it. /model in its terminal also changes the model of later sessions.",
    "model_read": r"(?:^|\s)--model(?:=|\s+)(\S+)",
}

# What `agy models` printed on 2026-11-19 (agy 1.5.13, a Google AI Ultra account): one line per model and effort
# level, its id and its name. The list when the installed agy does not answer (a sandbox: it cannot open its port).
MODELS_SEEN = """\
gemini-4-neon-high\tGemini 4 Neon (High)
gemini-4-neon-xhigh\tGemini 4 Neon (XHigh)
gemini-4-neon-medium\tGemini 4 Neon (Medium)
gemini-4-neon-low\tGemini 4 Neon (Low)
gemini-4-carbon-medium\tGemini 4 Carbon (Medium)
gemini-4-carbon-high\tGemini 4 Carbon (High)
gemini-4-carbon-low\tGemini 4 Carbon (Low)
gemini-4-argon-medium\tGemini 4 Argon (Medium)
gemini-4-argon-high\tGemini 4 Argon (High)
gemini-4-argon-low\tGemini 4 Argon (Low)
gemini-4-hydrogen-high\tGemini 4 Hydrogen (High)
gemini-4-hydrogen-xhigh\tGemini 4 Hydrogen (XHigh)
gemini-4-hydrogen-medium\tGemini 4 Hydrogen (Medium)
gemini-4-hydrogen-low\tGemini 4 Hydrogen (Low)
gemini-pro-orchestrator-neon\tOrchestrator (Neon)
gemini-pro-orchestrator\tOrchestrator (Carbon)
gemini-pro-orchestrator-argon\tOrchestrator (Argon)
gemini-3.8-flash-high\tGemini 3.8 Flash (High)
gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)
gemini-3.8-flash-low\tGemini 3.8 Flash (Low)
claude-opus-5-5-low\tClaude Opus 5.5 (Low)
claude-opus-5-5-medium\tClaude Opus 5.5 (Medium)
claude-opus-5-5-high\tClaude Opus 5.5 (High)
claude-sonnet-5-5-low\tClaude Sonnet 5.5 (Low)
claude-sonnet-5-5-medium\tClaude Sonnet 5.5 (Medium)
claude-sonnet-5-5-high\tClaude Sonnet 5.5 (High)
gpt-oss-120b-medium\tGPT-OSS 120B (Medium)
"""

# The level names of `agy models` (its ids end in -<level>; its names in '(<Level>)').
_LEVELS = ("low", "medium", "high", "xhigh", "max")
_ID_LEVEL = re.compile(r"^(.+)-(" + "|".join(_LEVELS) + r")$")


def parse_models(text):
    """The models of `agy models` output, one dict per model (#6256): {"name": the id with no level, "label": its
    name with no level, "efforts": its levels low to high ([] when agy lists it with no level: the Orchestrators take
    no --effort)}, in agy's order. An Orchestrator is of the generation of the models it runs (Gemini 4) and comes
    after them."""
    out = {}
    for line in (text or "").splitlines():
        parts = line.strip().split("\t", 1)
        if len(parts) != 2 or not re.fullmatch(r"[a-z0-9][\w.-]*", parts[0]):
            continue
        mid, label = parts[0], parts[1].strip()
        m = _ID_LEVEL.match(mid)
        lm = re.match(r"^(.*?)\s*\(([^)]*)\)$", label)
        if m and lm and lm.group(2).lower() == m.group(2):
            base, level, label = m.group(1), m.group(2), lm.group(1)
        else:
            base, level = mid, None
        row = out.setdefault(base, {"name": base, "label": label, "efforts": []})
        if level and level not in row["efforts"]:
            row["efforts"].append(level)
    for row in out.values():
        row["efforts"].sort(key=_LEVELS.index)
        if "orchestrator" in row["name"]:
            # Below the plain models, in the order of the model it names ("Orchestrator (Neon)").
            row.update(version="4", tier=model_tier(row["label"], DESCRIBE["tiers"]) - 10)
    return list(out.values())


# The models of the installed agy (`agy models`, about two seconds), read by a thread at most once in ten minutes
# (agents.CliList); MODELS_SEEN until the first read ends, and when agy does not answer. MODELS.runner: a test hook.
MODELS = CliList(["agy", "models"], parse_models, fallback=parse_models(MODELS_SEEN))


def read_models():
    """The models of the installed agy, one dict per model (parse_models)."""
    return [dict(m, efforts=list(m["efforts"])) for m in MODELS.get()]


def read_effort_levels(model):
    """The effort levels agy takes for a model (from read_models; an id with its level, as an old command names it,
    counts as its model); None for a model agy does not list, [] for one with no level (an Orchestrator)."""
    m = _ID_LEVEL.match(model or "")
    names = {model} | ({m.group(1)} if m else set())
    return next((list(r["efforts"]) for r in read_models() if r["name"] in names), None)

# ---------------------------------------------------------------------------------------------------------------
# 2. Settings
# ---------------------------------------------------------------------------------------------------------------

MODES = ["accept-edits", "auto", "plan"]

SETTINGS = [
    {"key": "mode", "label": "Agent mode", "kind": "choice", "choices": MODES, "default": "",
     "text": "accept-edits allows file edits; auto allows edits and safe commands; plan only reads. Empty: agy's own "
             "setting (auto since agy 1.5.7, where the account has it).",
     "flag": ["--mode", "{value}"], "live": None, "read": r"--mode(?:=|\s+)(accept-edits|auto|plan)\b",
     "session": True, "note": "A restart applies it; Shift+Tab in the session cycles the mode for now."},
    {"key": "skip_permissions", "label": "Skip every permission", "kind": "toggle", "choices": [], "default": "off",
     "text": "Allow every tool with no question (--dangerously-skip-permissions).",
     "flag": ["--dangerously-skip-permissions"], "live": None, "read": None, "session": True},
    {"key": "sandbox", "label": "Sandbox", "kind": "toggle", "choices": [], "default": "off",
     "text": "Run its terminal commands in agy's sandbox.",
     "flag": ["--sandbox"], "live": None, "read": None, "session": True},
    {"key": "args", "label": "Other arguments", "kind": "text", "choices": [], "default": "",
     "text": "More words for the agy command line, as you type them in a shell.",
     "flag": [], "live": None, "read": None},
    # #6351: agy reads hooks only from a file in the project, so the setting writes one there (project_hooks).
    {"key": "activity_hooks", "label": "Activity hooks", "kind": "toggle", "choices": [], "default": "off",
     "text": "Write MaximizePM's hooks into the project's .agents/hooks.json at each start: the push guard before a "
             "shell command, the turn's start and end, and waiting messages at a turn's end. Off takes them out.",
     "flag": [], "live": None, "read": None},
]

# 3. Effort: --effort low|medium|high|xhigh|max, the same names as MaximizePM's levels. /effort opens a menu in the
# session; the page changes it by a restart.
EFFORT = {"levels": ["low", "medium", "high", "xhigh", "max"], "map": {}, "flag": ["--effort", "{value}"],
          "live": None, "read": r"--effort(?:=|\s+)(low|medium|high|xhigh|max)\b", "label": "Reasoning effort"}

# ---------------------------------------------------------------------------------------------------------------
# 4. Launch
# ---------------------------------------------------------------------------------------------------------------

# No session name on the command line (/rename in the session), no system prompt file, no hooks file on the command
# line (the project's .agents/hooks.json instead, behind activity_hooks: project_hooks), no fork flag.
# No extra folder: a folder that --add-dir names becomes where its shell commands run (#6278, build_command);
# --conversation <id> resumes.
# Background: agy runs a command that lasts more than a few seconds as a background task (its footer: '1 task(s)')
# and wakes the session when it ends. In the end-to-end run of 2026-11-19 its maxpm wait ran so, returned STOP after a
# stop request from the page, and the session woke, ran maxpm go and ended.
LAUNCH = {"session_name": False, "system_prompt_file": False, "hooks_file": False, "extra_dirs": False, "fork": False,
          "resume": True, "autocompact": False, "background": True, "read_dirs": True}


def _on(value):
    return str(value or "").strip().lower() in ("on", "true", "1", "yes")


# The maxpm commands of a session (measured on agy 1.5.13, 2026-11-19): auto mode runs each command in agy's
# sandbox, which refuses to run a program outside the workspace ('zsh:1: operation not permitted: maxpm'); a
# permissions.allow rule alone does not change that. With the rule, a command that the model runs with
# BypassSandbox runs outside the sandbox with no question, and in the other modes a maxpm command runs with no
# question. --settings holds it for this session only; agy never saves it, so nothing goes into agy's own files (p140).
# The deny rules are the tmux guard (core.tmux_guard, #6251). Measured with agy 1.5.13 on 2026-11-20 (echo in place of
# tmux): a rule matches the command's first words, after agy takes off variables and env before it ('FOO=1 cmd',
# 'env FOO=1 cmd'), in each command of a chain ('cd . && cmd'), and a deny rule wins over an allow rule. So
# 'command(tmux kill-server)' refuses the bare kill-server and 'TMUX_TMPDIR=<folder> tmux kill-server' (#6246), and a
# test's own 'tmux -L <name> kill-server' runs. Not covered, which core.tmux_guard refuses: another option before
# kill-server with no server of its own ('tmux -f <file> kill-server').
DENY_TMUX = ["command(tmux kill-server)", "command(tmux -L maximizepm kill-server)",
             "command(tmux -Lmaximizepm kill-server)"]
# The read commands that a session runs with no question (#6400). In auto mode agy sends each command that its fixed
# rules do not pass (a command run with BypassSandbox, or in the sandbox a chain with 'echo ---' or 'git remote -v')
# to a safety check that calls a Gemini model. When the Gemini 5-hour quota is spent, that call fails ('Individual
# quota reached', in agy's log) and agy asks the person: 'Reason: the auto-run safety check did not complete', also in
# a Claude session. An allow rule passes the command before that check. Measured with agy 1.5.13 on 2026-11-20 while
# the Gemini quota was spent: chains of these commands, also with BypassSandbox and after a maxpm command, ran with no
# question; 'echo hi > <file outside the workspace>' with BypassSandbox still asked, so a redirect is not allowed.
# Not git diff, git log or git show (#6405): each takes --output=<file>, and with command(git diff) in allow agy 1.5.13
# let 'git diff --output=<file outside the workspace>' with BypassSandbox (and in accept-edits) write the file with no
# question; deny wildcards like command(*--output*) did not match.
READ_COMMANDS = ["git status", "git remote -v", "ls", "pwd", "cat", "head", "tail", "wc", "grep", "echo"]
ALLOW = ["command(maxpm)"] + [f"command({c})" for c in READ_COMMANDS]
SETTINGS_JSON = json.dumps({"permissions": {"allow": ALLOW, "deny": DENY_TMUX}}, separators=(",", ":"))
# The --settings of older starts, which parse_command reads back too.
_OLD_SETTINGS = (
    json.dumps({"permissions": {"allow": ["command(maxpm)"], "deny": DENY_TMUX}}, separators=(",", ":")),
    json.dumps({"permissions": {"allow": ["command(maxpm)"] + [f"command({c})" for c in (
        "git status", "git log", "git diff", "git show", "git remote -v", "ls", "pwd", "cat", "head", "tail",
        "wc", "grep", "echo")], "deny": DENY_TMUX}}, separators=(",", ":")),
)


def session_settings(read_dirs=(), secret_files=()):
    """The --settings JSON of a start: SETTINGS_JSON, and for the project's read_dirs (#6281) read_file(<folder>) in
    allow (agy reads in it and below with no question), write_file(<folder>) in deny, and read_file(<file>) in deny for
    each secret file (agy's rules take a path, not a pattern; a deny wins over an allow; antigravity.google/docs/
    permissions, read 2026-11-20). Never --add-dir: it moves where agy runs its shell commands (#6278). Measured
    2026-11-20 with agy 1.5.13: a session read a read folder's Cargo.toml with no question and was refused its .env."""
    if not read_dirs:
        return SETTINGS_JSON
    allow = ALLOW + [f"read_file({d})" for d in read_dirs]
    deny = DENY_TMUX + [f"write_file({d})" for d in read_dirs] + [f"read_file({f})" for f in secret_files]
    return json.dumps({"permissions": {"allow": allow, "deny": deny}}, separators=(",", ":"))


# The note after the first prompt. run_command has a BypassSandbox parameter only in auto mode (agy's default, also with
# no --mode) without --dangerously-skip-permissions (#6368). Measured with agy 1.5.13 on 2026-11-20, from the tool
# schema that agy keeps in each conversation and from print-mode runs: auto mode had it with Claude, Gemini and GPT-OSS
# models; accept-edits, plan, and --dangerously-skip-permissions (also in auto mode) had it with none, so a Claude
# model there said 'run_command doesn't have a BypassSandbox parameter' and ran maxpm without it, which worked. The note
# goes only where the parameter can be, and says 'when' for a default mode that agy's own settings may change.
# It asks for each maxpm command in a call of its own (#6400): with the older note the model ran chains such as
# 'maxpm now ...; maxpm show N; git log | head' with BypassSandbox, and outside the sandbox each command that no allow
# rule names goes to agy's safety check (READ_COMMANDS). Measured with agy 1.5.13 on 2026-11-20: with this note the
# model ran maxpm alone and the other commands in the sandbox in 3 of 3 chats.
MAXPM_NOTE = ("Run each maxpm command in a run_command call of its own, with BypassSandbox set to true when run_command "
              "has that parameter: agy's sandbox cannot run maxpm, and this session allows maxpm with no question. Run "
              "other commands in the sandbox, in calls without maxpm.")
_OLD_NOTES = ("When your run_command tool has a BypassSandbox parameter, set it to true for each maxpm command: agy's "
              "sandbox cannot run maxpm, and this session allows maxpm with no question.",
              "Run each maxpm command with BypassSandbox set to true: agy's sandbox cannot run maxpm, and this session "
              "allows maxpm with no question.")


def note_applies(settings):
    """True when the session's run_command can have BypassSandbox: auto mode or no mode, without skip_permissions."""
    return (settings.get("mode") or "").strip() in ("", "auto") and not _on(settings.get("skip_permissions"))


def build_command(name, folder, model, effort, settings, prompt):
    """agy [--conversation <id>] [--model M] [--effort E] [--mode M] [--dangerously-skip-permissions] [--sandbox]
    --settings <SETTINGS_JSON> <args> [-i <prompt>, then MAXPM_NOTE when note_applies]. agy has no flag for a session
    name. -i runs the first prompt and keeps the session open."""
    s = settings
    words = ["agy"] + (["--conversation", s["resume"]] if s.get("resume") else [])
    # One way to set the level (#6256): --effort with the model's id. agy refuses an id with a level and an --effort
    # of another level ("--model gemini-4-neon-low conflicts with --effort=high", measured 2026-11-19), so an id with
    # its level (an old command, an item's model) loses the level when an effort is given; and an Orchestrator takes
    # no --effort ("--effort is not supported for model").
    m = _ID_LEVEL.match(model or "")
    if m and effort in EFFORT["levels"]:
        model = m.group(1)
    words += ["--model", model] if model else []
    words += ["--effort", effort] if effort in EFFORT["levels"] and read_effort_levels(model) != [] else []
    mode = (s.get("mode") or "").strip()
    if mode in MODES:
        words += ["--mode", mode]
    words += ["--dangerously-skip-permissions"] if _on(s.get("skip_permissions")) else []
    words += ["--sandbox"] if _on(s.get("sandbox")) else []
    # No --add-dir (#6278): in a chat (-i), agy 1.5.13 runs a shell command with no folder of its own in a folder that
    # --add-dir names, not in its start folder, so 'maxpm go' ran in ~/.maximizepm and found no project. Measured
    # 2026-11-20 in panes (pwd, and maxpm go): with --add-dir <queue folder>, 6 of 7 starts ran it in that folder, also
    # with '--add-dir .' after it; with no --add-dir, in the start folder each time (agy -p too). The maxpm commands
    # need no queue folder in the workspace: they run outside agy's sandbox (BypassSandbox and SETTINGS_JSON).
    words += ["--settings", session_settings(s.get("read_dirs") or (), s.get("secret_files") or ())]
    words += [Raw(s["args"].strip())] if (s.get("args") or "").strip() else []
    words += ["-i", f"{prompt}\n\n{MAXPM_NOTE}" if note_applies(s) else prompt] if prompt else []
    return words


def resume_command(session_ref, model, effort, settings, name=None):
    """agy --conversation <id>, with the profile's settings and settings['prompt'] as its first prompt (measured: the
    resumed session runs the prompt and keeps the whole chat). At its exit agy prints the same command:
    'agy --conversation=<id>'."""
    if not session_ref:
        return None
    return build_command(name, None, model, effort, {**settings, "resume": session_ref}, settings.get("prompt"))


def trust_workspace(folder, command, env=None):
    """Add `folder` to trustedWorkspaces in agy's session-only --settings JSON (#6339), so a worktree that MaximizePM
    made starts without agy's folder trust prompt and without writing to ~/.gemini/antigravity-cli/settings.json."""
    if not folder or not command:
        return command
    try:
        words = shlex.split(command)
    except ValueError:
        return command
    if not words:
        return command
    raw = str(folder)
    real = os.path.realpath(raw)
    paths = [real] if real == raw else [raw, real]

    def _add(text):
        try:
            data = json.loads(text)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        trusted = list(data.get("trustedWorkspaces") or ()) if isinstance(data.get("trustedWorkspaces"), list) else []
        for p in paths:
            if p not in trusted:
                trusted.append(p)
        data["trustedWorkspaces"] = trusted
        return json.dumps(data, separators=(",", ":"))

    for i, w in enumerate(words):
        if w == "--settings" and i + 1 < len(words):
            updated = _add(words[i + 1])
            if updated is not None:
                words[i + 1] = updated
            return shlex.join(words)
        if w.startswith("--settings="):
            updated = _add(w.split("=", 1)[1])
            if updated is not None:
                words[i] = "--settings=" + updated
            return shlex.join(words)
    payload = json.dumps({"trustedWorkspaces": paths}, separators=(",", ":"))
    return shlex.join(words[:1] + ["--settings", payload] + words[1:])


def parse_command(words):
    """A hand-written agy command that a profile builds exactly: --model {model}, --effort {effort}, --mode with a
    known value, --dangerously-skip-permissions, --sandbox, --add-dir of the queue folder, and one prompt (with -i or
    --prompt-interactive). Anything else: None."""
    from ..core import river_dir
    if not words or Path(words[0]).name.lower() not in ("agy", "antigravity"):
        return None
    opts, prompt, i = {}, None, 1
    while i < len(words):
        t, nxt = words[i], (words[i + 1] if i + 1 < len(words) else None)
        if t == "--model" and nxt == "{model}" or t == "--effort" and nxt == "{effort}":
            i += 2
        elif t == "--mode" and nxt in MODES:
            opts["mode"] = nxt
            i += 2
        elif t.startswith("--mode=") and t.split("=", 1)[1] in MODES:
            opts["mode"] = t.split("=", 1)[1]
            i += 1
        elif t == "--dangerously-skip-permissions":
            opts["skip_permissions"] = "on"
            i += 1
        elif t == "--sandbox":
            opts["sandbox"] = "on"
            i += 1
        elif t == "--add-dir" and nxt is not None and (  # an old start's queue folder (#6278: no longer added)
                nxt == "{maxpm_dir}" or os.path.realpath(os.path.expanduser(nxt)) == os.path.realpath(river_dir())):
            i += 2
        elif t == "--settings" and nxt in (SETTINGS_JSON,) + _OLD_SETTINGS:
            i += 2
        elif t in ("-i", "--prompt-interactive") and nxt is not None and prompt is None:
            prompt = nxt
            for note in (MAXPM_NOTE,) + _OLD_NOTES:
                if nxt.endswith("\n\n" + note):
                    prompt = nxt[:-len(note) - 2]
            i += 2
        else:
            return None
    return {"settings": opts, "prompt": prompt} if prompt is not None else None


# ---------------------------------------------------------------------------------------------------------------
# 5. Session identity
# ---------------------------------------------------------------------------------------------------------------

# Measured in a shell command of agy 1.5.13: ANTIGRAVITY_AGENT=1, ANTIGRAVITY_CONVERSATION_ID=<uuid> (also
# ANTIGRAVITY_TRAJECTORY_ID, ANTIGRAVITY_APP_DATA_DIR, ANTIGRAVITY_PROJECT_ID). No web link for a local conversation.
IDENTITY = {"source": "env", "env": "ANTIGRAVITY_CONVERSATION_ID", "pane": None, "url": None,
            "inside_env": ["ANTIGRAVITY_AGENT"],
            "id_pattern": r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"}


def data_dir(env=None):
    """agy's own folder: ~/.gemini/antigravity-cli (the docs' <app_data_dir>; agy gives its commands it as
    ANTIGRAVITY_APP_DATA_DIR)."""
    return Path.home() / ".gemini" / "antigravity-cli"


def chat_path(session_ref, folder=None, root=None):
    """The transcript of conversation `session_ref` (its id): <root>/<id>/.system_generated/logs/transcript_full.jsonl,
    else transcript.jsonl in the same folder (the same steps, with each tool argument as a JSON text). root: the
    folder of the conversations, in place of ~/.gemini/antigravity-cli/brain (the tests give one). None when there is
    none."""
    ref = str(session_ref or "").strip()
    if not ref:
        return None
    if os.path.isfile(ref):
        return Path(ref)
    if not re.fullmatch(IDENTITY["id_pattern"], ref):
        return None
    logs = (Path(root) if root else data_dir() / "brain") / ref / ".system_generated" / "logs"
    for name in ("transcript_full.jsonl", "transcript.jsonl"):
        if (logs / name).is_file():
            return logs / name
    return None


# ---------------------------------------------------------------------------------------------------------------
# 6. Chat log
# ---------------------------------------------------------------------------------------------------------------

# edit_file changes a file (measured); write_to_file writes a whole one. agy's prompt cache costs no more to write
# than plain input.
CHAT = {"compressed": False, "file_tools": ["edit_file", "write_to_file"], "ref": "session", "cache_write": False}

_REQUEST = re.compile(r"<USER_REQUEST>\n?(.*?)\n?</USER_REQUEST>", re.S)
# The last <SYSTEM_MESSAGE> block of a SYSTEM_MESSAGE step: agy's preamble names the tag once before it.
_SYSTEM = re.compile(r"<SYSTEM_MESSAGE>\n?((?:(?!<SYSTEM_MESSAGE>).)*?)\n?</SYSTEM_MESSAGE>", re.S)
_STOP_HOOK = "Stop hook blocked termination:"
# The first lines of a tool's result: 'Created At: <time>' and 'Completed At: <time>', twice.
_STAMPS = re.compile(r"\A(?:(?:Created|Completed) At: [^\n]*\n)+")


def _step(d):
    return d.get("step_index") if isinstance(d.get("step_index"), int) else None


def _call_id(step, n):
    """The id of the n-th tool call of the model's step `step`: the step that holds its result. agy writes the result
    of each call as the next steps, in the order of the calls (measured: two calls at step 10, results at 11 and 12),
    so one line tells its result's call with no state."""
    return f"s{step + 1 + n}"


def _detail(args):
    for k in ("CommandLine", "Path", "AbsolutePath", "TargetFile", "Query", "Url", "SearchPath", "Pattern"):
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    qs = args.get("questions")
    if isinstance(qs, list):
        return " · ".join(q.get("question", "") for q in qs if isinstance(q, dict))
    return json.dumps(args, ensure_ascii=False) if args else ""


def _args(tc):
    """A tool call's arguments as a dict: transcript_full.jsonl keeps them as values, transcript.jsonl as JSON texts."""
    args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
    out = {}
    for k, v in args.items():
        if isinstance(v, str) and v[:1] in '"[{':
            try:
                v = json.loads(v)
            except ValueError:
                pass
        out[k] = v
    return out


def parse_chat_line(d):
    """(entries, turn) of one step of the transcript. USER_INPUT: the person's prompt (the text in <USER_REQUEST>);
    PLANNER_RESPONSE: the model's text and its tool calls; GENERIC from the model: the result of a tool call (status
    ERROR: a failed or refused call, its error); SYSTEM_MESSAGE with the Stop hook's reason: an event with the text
    that MaximizePM brought in at the turn's end (agy's own reminders to the model are no entries).
    The turn: working after a prompt, a tool call and a result; idle after a reply with text and no tool call, or an
    ERROR_MESSAGE (an interrupted or failed turn, #6261)."""
    if not isinstance(d, dict):
        return [], None
    at, kind, step = d.get("created_at"), d.get("type"), _step(d)
    if kind == "USER_INPUT":
        content = d.get("content") if isinstance(d.get("content"), str) else ""
        m = _REQUEST.search(content)
        said = (m.group(1) if m else content).strip()
        return ([{"at": at, "role": "user", "text": said}] if said else []), "working"
    if kind == "PLANNER_RESPONSE":
        out = []
        said = d.get("content").strip() if isinstance(d.get("content"), str) else ""
        if said:
            out.append({"at": at, "role": "assistant", "text": said})
        calls = [tc for tc in d.get("tool_calls") or () if isinstance(tc, dict)]
        for n, tc in enumerate(calls):
            args = _args(tc)
            out.append({"at": at, "role": "tool", "text": tc.get("name", ""),
                        "id": _call_id(step, n) if step is not None else "", "name": tc.get("name", ""),
                        "detail": _detail(args)})
        return out, ("working" if calls or not said else "idle")
    if kind == "GENERIC" and d.get("source") == "MODEL":
        error = d.get("status") == "ERROR"
        text = d.get("error") if error and isinstance(d.get("error"), str) else d.get("content")
        text = _STAMPS.sub("", text if isinstance(text, str) else "").strip()
        return [{"at": at, "role": "result", "text": text, "for": f"s{step}" if step is not None else "",
                 "error": error}], "working"
    if kind == "ERROR_MESSAGE":
        return [], "idle"
    if kind == "SYSTEM_MESSAGE":
        # The reason of MaximizePM's Stop hook (the messages that waited, #6351): the turn goes on with it, and the
        # chat shows what came in (#6367). agy's own reminders stay out.
        content = d.get("content") if isinstance(d.get("content"), str) else ""
        m = _SYSTEM.search(content)
        text = (m.group(1) if m else content).strip()
        if text.startswith(_STOP_HOOK):
            text = text[len(_STOP_HOOK):].strip()
            return ([{"at": at, "role": "event", "text": text}] if text else []), "working"
    return [], None


def _lines(lines):
    for line in lines:
        line = line.strip() if isinstance(line, str) else line
        if not line:
            continue
        try:
            d = json.loads(line) if isinstance(line, str) else line
        except ValueError:
            continue
        if isinstance(d, dict):
            yield d


def parse_chat(lines):
    """(entries, turn) of the lines of one transcript (parse_chat_line on each). A turn that the person cancels (Esc)
    leaves its prompt as the last step: it reads as working, and the screen (DETECT) says idle."""
    entries, turn = [], None
    for d in _lines(lines):
        got, st = parse_chat_line(d)
        entries += got
        turn = st or turn
    return entries, turn


def chat_title(lines):
    """The person's first prompt, on one line."""
    for e in parse_chat(lines)[0]:
        if e["role"] == "user" and e["text"]:
            return " ".join(e["text"].split())[:120]
    return None


def chat_usage(lines):
    """The token counts of the model's steps: input_tokens is the input without the cached part (cache_read_tokens),
    as measured (9879 + 0 at the first call, 3109 + 7842 at the next)."""
    out = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    for d in _lines(lines):
        if d.get("type") != "PLANNER_RESPONSE":
            continue
        out["input"] += int(d.get("input_tokens") or 0)
        out["cache_read"] += int(d.get("cache_read_tokens") or 0)
        out["output"] += int(d.get("output_tokens") or 0)
    return out


def _model_of(label):
    """(model id, level) for a name that agy shows ('Gemini 4 Neon (High)', 'Orchestrator (Carbon)', 'Gemini 4
    Neon' with the level apart), by the models of read_models; (None, None) for a name it does not list."""
    label = (label or "").strip()
    m = re.match(r"^(.*?)\s*\(([A-Za-z]+)\)$", label)
    for base, level in ((m.group(1), m.group(2).lower()) if m else (None, None), (label, None)):
        if base is None:
            continue
        row = next((r for r in read_models() if r["label"].lower() == base.lower()), None)
        if row and (level is None or level in row["efforts"]):
            return row["name"], level
    return None, None


def chat_settings(lines):
    """The model and effort of the session's last turn (#6260): each model step names its model id with the level
    ('gemini-4-neon-medium'), and a change of the model in the session adds a note to the next prompt ('The user
    changed setting `Model Selection` from None to Gemini 4 Neon (High)'). agy records no mode in the log
    (screen_settings reads it)."""
    got = {}
    for d in _lines(lines):
        mid, level = None, None
        if d.get("type") == "PLANNER_RESPONSE" and d.get("model"):
            m = _ID_LEVEL.match(str(d["model"]))
            mid, level = (m.group(1), m.group(2)) if m else (str(d["model"]), None)
        elif d.get("type") == "USER_INPUT":
            found = re.findall(r"`Model Selection` from .*? to (.+?)\. ", str(d.get("content") or ""))
            if found:
                mid, level = _model_of(found[-1])
        if mid:
            got["model"] = mid
            if level:
                got["effort"] = level
            else:
                got.pop("effort", None)
    return got


def config_settings():
    """What an agy session takes when its command line names nothing (#6280): the mode auto. agy's settings.json held
    no mode on 2026-11-20 (agy 1.5.13), and a start with no --mode showed 'auto' in its footer."""
    return {"mode": "auto"}


# Where the Settings dialog reads each setting of a running session (#6280).
SETTINGS_READ = {"model": ("chat", "screen"), "effort": ("chat", "screen"), "mode": ("screen", "config")}


# The footer at the right of the screen: '<mode> · <model> · <level>', the level left out for a model with none
# ('auto · Orchestrator (Neon)'). Measured on agy 1.5.13 for every mode and form of start (#6260).
FOOTER = re.compile(r"(?:^|\s)(" + "|".join(MODES) + r") · ([^·]+?)(?: · (" + "|".join(_LEVELS) + r"))?\s*$")


def screen_settings(lines):
    """What the session runs now, from its footer (#6260): {"mode", "model", "effort"}; {} when the screen shows no
    footer. The screen follows a /model or Shift+Tab at once, before the next turn writes the log."""
    for line in reversed(list(lines)):
        m = FOOTER.search(line)
        if not m:
            continue
        got = {"mode": m.group(1)}
        mid, level = _model_of(m.group(2))
        if mid:
            got["model"] = mid
        level = m.group(3) or level
        if level:
            got["effort"] = level
        elif mid and read_effort_levels(mid) == []:
            got["effort"] = "none"  # an Orchestrator takes no effort
        return got
    return {}


_GROUP = re.compile(r"^\s*([A-Z][A-Z0-9 ]*MODELS)\s*$")
_WINDOW = re.compile(r"^\s*(Weekly|Five Hour)\s+Limit\s+Remaining\s*$", re.I)
_PERCENT = re.compile(r"^\s*\[[^\]]*\]\s*(\d{1,3}(?:\.\d+)?)\s*%\s*$")


def screen_limits(lines, now=None):
    """The quota of agy's /usage screen, 'Models & Quota' (#6348; read from agy on 2026-11-20): for each group of
    models ('GEMINI MODELS', 'CLAUDE AND GPT MODELS') a 'Weekly Limit Remaining' and a 'Five Hour Limit Remaining',
    each with a bar and a percent left on the next line and 'Refreshes in 2h 31m' after it. One window per group and
    limit: 'Gemini models: 5-hour'."""
    seq = [str(ln) for ln in (lines or ())]
    if not any("Models & Quota" in ln for ln in seq):
        return None
    out, group, window = [], None, None
    for i, raw in enumerate(seq):
        g = _GROUP.match(raw)
        if g:
            # 'CLAUDE AND GPT MODELS' -> 'Claude and GPT models'
            words = [w.lower() if w in ("AND", "MODELS") else w if len(w) <= 3 else w.capitalize()
                     for w in g.group(1).split()]
            group, window = " ".join(words), None
            continue
        w = _WINDOW.match(raw)
        if w:
            window = "5-hour" if w.group(1).lower().startswith("five") else "weekly"
            continue
        p = _PERCENT.match(raw)
        if p and window:
            rem = round(max(0.0, min(100.0, float(p.group(1)))), 1)
            resets_at, resets_text = parse_reset_time(seq[i + 1] if i + 1 < len(seq) else "", now=now)
            out.append({"name": f"{group}: {window}" if group else window, "used_percent": round(100.0 - rem, 1),
                        "remaining_percent": rem, "resets_at": resets_at, "resets_text": resets_text})
            window = None
    return out or None


def model_group(model):
    """The quota group of a model, named as screen_limits names it (#6382): agy counts the Gemini models in one group
    and the Claude and GPT models in another, each with its own 5-hour and weekly limit (the Orchestrator's id starts
    with gemini-). None for another model: its limit stop then limits all of agy."""
    m = (model or "").strip().lower()
    if m.startswith("gemini"):
        return "Gemini models"
    if m.startswith(("claude", "gpt")):
        return "Claude and GPT models"
    return None


def chat_requests(session_ref, root=None):
    """(requests, fork) of a conversation for maxpm usage: each model step of its transcript is one model call, keyed
    by its step ('s<step>'), with its time and its token counts. agy writes no prompt cache, and no conversation starts
    as a fork of another here (/fork makes a new id), so fork is False. None when no transcript of it is here."""
    path = chat_path(session_ref, root=root)
    if path is None:
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    out = {}
    for d in _lines(lines):
        if d.get("type") != "PLANNER_RESPONSE" or _step(d) is None:
            continue
        out[f"s{_step(d)}"] = (d.get("created_at"), {
            "input": int(d.get("input_tokens") or 0), "output": int(d.get("output_tokens") or 0),
            "cache_read": int(d.get("cache_read_tokens") or 0), "cache_write": 0, "write_1h": 0, "write_5m": 0})
    return out, False


def context_window(model):
    """The tokens of a model's window, as agy's /context showed them on 2026-11-19 (agy 1.5.13): 1.0M for the Gemini
    models (gemini-3.8-flash-low, gemini-4-neon-high), 1M for the Claude models (claude-opus-5-5-high), 131.1k for
    gpt-oss-120b-medium. None for another model."""
    m = (model or "").lower()
    if m.startswith("gemini-"):
        return 1_048_576
    if m.startswith("claude-"):
        return 1_000_000
    if m.startswith("gpt-oss-"):
        return 131_072
    return None


def chat_cache_line(raw, st):
    """Fold one line of a transcript into the cache state (the context meter): each model step is one model call,
    its context the input, the cached part and the output."""
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return
    if not isinstance(d, dict) or d.get("type") != "PLANNER_RESPONSE":
        return
    ctx = sum(int(d.get(k) or 0) for k in ("input_tokens", "cache_read_tokens", "output_tokens"))
    if ctx and d.get("created_at"):
        st["last"] = {"start": d["created_at"], "at": d["created_at"], "context": ctx}
        if d.get("model"):
            st["model"] = d["model"]


# Background tasks (#6454), as agy 1.5.13 wrote them on 2026-11-20 (the footer's 'N task(s)', /tasks):
# - start: the result of a run_command that ran past WaitMsBeforeAsync, 'Tool is running as a background task with
#   task id: <conversation>/task-<step>' and 'Task Description: <the command>';
# - end: a SYSTEM_MESSAGE '[Message] timestamp=<t> sender=<task id> ... content=Task id "<task id>" finished with
#   result:' and 'Command exited with code <n>.'; the result of manage_task kill, 'Task "<task id>" cancelled.'; or
#   the result of manage_task status with a Status other than RUNNING (DONE).
_TASK_START = re.compile(r"\ATool is running as a background task with task id: (\S+)\s*\nTask Description: "
                         r"(.*?)(?:\nTask logs are available at:|\Z)", re.S)
_TASK_DONE = re.compile(r'timestamp=(\S+) sender=\S+ [^\n]*?content=Task id "([^"]+)" finished with result:'
                        r'(?:\s*Command exited with code (\d+))?')
_TASK_CANCEL = re.compile(r'\ATask "([^"]+)" cancelled\.')
_TASK_STATUS = re.compile(r"\ATask: (\S+)\nStatus: (\w+)")
_TASKS_MAX = 200


def _time(text):
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def background_scan(path, start, st):
    """Fold the new steps of a transcript into st (agents.background_scan): each background command, its start, the
    command, and its end with its status (completed, failed: exit code not 0, cancelled, or the Status of a
    manage_task status)."""
    from . import new_lines
    tasks = st.setdefault("tasks", {})

    def finish(tid, at, status):
        task = tasks.get(tid)
        if task and task["end"] is None:
            from datetime import datetime, timezone
            task["end"], task["status"] = at or datetime.now(timezone.utc), status

    end = start
    for _pos, end, line in new_lines(path, start):
        if "/task-" not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict) or not isinstance(d.get("content"), str):
            continue
        at = _time(d.get("created_at"))
        if d.get("type") == "GENERIC" and d.get("source") == "MODEL" and at:
            text = _STAMPS.sub("", d["content"])
            m = _TASK_START.match(text)
            if m:
                command = m.group(2).strip()
                tasks[m.group(1)] = {"id": m.group(1), "kind": "command", "at": at, "command": command,
                                     "what": " ".join(command.split())[:80], "end": None, "status": None}
            elif _TASK_CANCEL.match(text):
                finish(_TASK_CANCEL.match(text).group(1), at, "cancelled")
            elif _TASK_STATUS.match(text) and _TASK_STATUS.match(text).group(2) != "RUNNING":
                m = _TASK_STATUS.match(text)
                finish(m.group(1), at, "completed" if m.group(2) == "DONE" else m.group(2).lower())
        elif d.get("type") == "SYSTEM_MESSAGE":
            for t, tid, code in _TASK_DONE.findall(d["content"]):
                finish(tid, _time(t) or at, "failed" if code and code != "0" else "completed")
    old = sorted((v for v in tasks.values() if v["end"]), key=lambda v: v["end"])
    for v in old[:max(0, len(tasks) - _TASKS_MAX)]:
        del tasks[v["id"]]
    return end


# ---------------------------------------------------------------------------------------------------------------
# 7. State: the screen
# ---------------------------------------------------------------------------------------------------------------

STATE = {"state": "detect", "session_id": "identity"}

# The pane watcher of maxpm serve (server.watch_prompts) reads a blocked screen first: a question and its menu are
# the last thing on the screen, above the key line ('↑/↓ Navigate · enter Confirm') and the footer. Else working (a
# spinner line: a braille character, then 'Generating...'), else idle: the footer under the input line, '? for
# shortcuts' at its left (or nothing there while the person's draft is in the input line), or the key line of the
# person's '/' list. The footer reads 'esc to cancel' while it works, while it asks, and also while that list is open,
# so it is no sign of work. At its right the footer shows the mode and the model ('auto · Gemini 3.8 Flash · low').
QUESTION = r"^\s*Question \d+/\d+: "
STATUS = r"^(?:esc to cancel)?\s{10,}\S[^·]*(?: · \S[^·]*)+$"
DETECT = {
    # Homebrew's agy is a wrapper that runs the program 'antigravity' (its cask); Google's script installs agy itself.
    "process": "antigravity",
    "process_args": r"(?:^|/)agy(?:\s|$)",
    "idle": r"^\? for shortcuts\b|^\s{10,}\S[^·]*(?: · \S[^·]*)+$|· tab Complete\s*$",
    "working": r"^[⠀-⣿]\s+\S",
    "blocked": [
        r"^\s*Run this command\?",                          # a terminal command
        r"^\s*Accept this file edit\?",                     # a file edit
        r"^\s*Allow creation of this file\?",               # a new file
        r"^\s*Allow access to this file\?",                 # a read outside the workspace
        r"^\s*Do you trust the contents of this project\?",  # a new project folder, at the start
        QUESTION,                                            # ask_question: agy asks the person
        r"^\s*Select login method",                         # sign-in, at the first start
    ],
    # Limit-stop text (#6348): 'Individual quota reached ... Resets in ~30m', 'Model quota reached', etc.
    "limit": [
        r"(?i)\b(?:individual|model|daily|weekly)?\s*quota\s+(?:reached|exceeded|exhausted)\b",
        r"(?i)\busage\s+limit\s+reached\b",
        r"(?i)\brate\s+limit\s+(?:reached|exceeded)\b",
    ],
    # 'No, cancel' (a command), 'No, reject this change' (an edit), 'No, deny creation' (a new file), 'No, deny
    # access' (a read outside the workspace): agy stops the turn and asks 'What should Antigravity CLI do instead?',
    # and the text goes in as the next prompt.
    "tell_instead": r"^No, (?:cancel|reject this change|deny creation|deny access)\b",
    "image_paste": None,
    # The selected option: '> 1. Yes, run command'; the others have two spaces.
    "menu": ">",
    # The key line under a menu, and agy's footer under it ('esc to cancel', then the mode and the model at the right).
    "hint": r"↑/↓ Navigate\b|\benter Submit\b|" + STATUS,
    # A question dialog (ask_question): 'Question 1/2: Which size?', its options ('[ ]' and '[x]' boxes when it takes
    # more answers), 'Write-in...' last, then the key line. river/ask_forms.Antigravity reads and answers it.
    "ask": QUESTION,
    "ask_form": "antigravity",
}

# ---------------------------------------------------------------------------------------------------------------
# 7b. The project's hooks (#6351): the setting agy_activity_hooks (off by default; mark 2026-11-20 'a setting to
# enable aggy hooks'). agy loads hooks only from ~/.gemini/config/hooks.json (its home: p140) or from the project's
# .agents/hooks.json; no flag adds one for one start. With the setting on, each start of an agy session (and maxpm
# init) writes MaximizePM's hooks into the start folder's .agents/hooks.json, under one name of its own (HOOK_NAME), and
# keeps the person's other hooks in the file; with it off, the start takes out only that name.
#
# Measured with agy 1.5.13 on 2026-11-20 (a probe hook that logged its input, in a tmux pane):
# - The top level maps a hook name to its events. PreToolUse and PostToolUse take [{"matcher", "hooks": [handler]}];
#   the other events take [handler] with no "hooks" around it: agy refuses the whole file otherwise ('invalid hook:
#   command hook must specify command', in its log only), and /hooks still lists the hooks.
# - The hook runs in .agents with agy's environment (MAXPM_AGENT, MAXPM_TOKEN, ANTIGRAVITY_CONVERSATION_ID) and gets
#   camelCase JSON on stdin: conversationId, cwd, hookEventName, lastUserInput; PreToolUse: toolCall {name, args}
#   (run_command: args.CommandLine); PreInvocation: invocationNum (0 at the start of a turn); Stop: fullyIdle (no
#   background command runs), continuationActive, terminationReason.
# - PreToolUse answers {"decision": "deny", "reason": ...}: agy refuses the call, and the model reads 'tool call denied
#   by pre-tool hook: <reason>'. No output lets the call run.
# - Stop answers {"decision": "continue", "reason": ...}: the turn goes on, and the model reads the reason as a system
#   message ('Stop hook blocked termination: <reason>').
# - agy -p (print mode) runs no hook.
# No event says that agy waits for a person (a permission menu, a question): the blocked state stays the screen's
# (DETECT), and the pane watcher keeps its reports. The hooks add the exact start and end of each turn.
# ---------------------------------------------------------------------------------------------------------------

HOOK_NAME = "maximizepm"
HOOK_FILE = Path(".agents") / "hooks.json"
HOOK_TIMEOUT = 10
# A hook that a session MaximizePM did not start runs: it has neither variable, and the hook ends at once (#6351).
HOOK_ENV = ("MAXPM_AGENT", "MAXPM_TOKEN")


def hook_entry(report):
    """MaximizePM's events in agy's hooks.json: the shell command guard (PreToolUse of run_command), each model call
    (PreInvocation: working) and the turn end (Stop: idle, and the messages that wait). report: the maxpm report
    command (argv words)."""
    handler = {"type": "command", "command": " ".join(shlex.quote(w) for w in [*report, "--hook", "antigravity"]),
               "timeout": HOOK_TIMEOUT}
    return {"PreToolUse": [{"matcher": "run_command", "hooks": [handler]}],
            "PreInvocation": [handler], "Stop": [handler]}


def project_hooks(folder, report, on):
    """Put MaximizePM's hooks into <folder>/.agents/hooks.json (on) or take them out (off), and keep every other hook
    in the file. The file is written only when its text changes; a file that holds only MaximizePM's hooks goes when
    they go. Returns the file's path when it holds MaximizePM's hooks now, else None. A file that is not a JSON object
    stays as it is: on raises OSError (the start then names the setting), off leaves it."""
    f = Path(folder) / HOOK_FILE
    try:
        text = f.read_text(encoding="utf-8")
    except FileNotFoundError:
        if not on:
            return None
        text = None
    try:
        data = json.loads(text) if text and text.strip() else {}
    except ValueError:
        data = None
    if not isinstance(data, dict):
        if on:
            raise OSError(f"{f} is not a JSON object; MaximizePM adds its hooks only to one")
        return None
    want = dict(data)
    if on:
        want[HOOK_NAME] = hook_entry(report)
    else:
        want.pop(HOOK_NAME, None)
    if want == data and text is not None:
        return str(f) if on else None
    if not want:
        f.unlink(missing_ok=True)
        try:
            f.parent.rmdir()  # .agents, when MaximizePM's file was all it held
        except OSError:
            pass
        return None
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f".hooks.json.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(want, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, f)
    return str(f) if on else None


def read_hook(payload):
    """An agy hook's JSON input as a hook report (river/agents HOOK_REPORT_KEYS), or None for another event or tool."""
    ev = str(payload.get("hookEventName") or "")
    sid = payload.get("conversationId")
    out = {"event": ev, "session_id": sid if isinstance(sid, str) and sid else None}
    if ev == "PreToolUse":
        call = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
        if call.get("name") != "run_command":
            return None
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        cmd = args.get("CommandLine")
        out.update(state=None, run=cmd if isinstance(cmd, str) else "")
    elif ev == "PreInvocation":
        # The first model call of a turn carries the prompt that started it (a person's, or a message typed in).
        first = payload.get("invocationNum") == 0
        prompt = payload.get("lastUserInput")
        out.update(state="working", prompt=prompt if first and isinstance(prompt, str) else None)
    elif ev == "Stop":
        out.update(state="idle", waits=[] if payload.get("fullyIdle") is not False else ["a background command"],
                   again=bool(payload.get("continuationActive")))
    else:
        return None
    return out


def hook_answer(res):
    """What `maxpm report --hook antigravity` prints for agy (JSON), or None: a refusal of the shell command
    (res['deny']); at a turn end, the messages that wait for the session (res['deliver']) and the reminder to close
    answered questions (res['remind']), as one system message that makes the turn go on."""
    if res.get("deny"):
        return json.dumps({"decision": "deny", "reason": res["deny"]})
    said = [t for t in (res.get("deliver") or ()) if t] + ([res["remind"]] if res.get("remind") else [])
    if said:
        return json.dumps({"decision": "continue", "reason": "\n\n".join(said)})
    return None

# ---------------------------------------------------------------------------------------------------------------
# 8. Input
# ---------------------------------------------------------------------------------------------------------------

# Stop on the Sessions tab: Esc stops the turn ('Interrupted · What should Antigravity CLI do instead?').
INPUT = {"native": None, "interrupt": ["Escape"]}

# ---------------------------------------------------------------------------------------------------------------
# 10. Commands
# ---------------------------------------------------------------------------------------------------------------

# The built-in commands of agy 1.5.13, as its '/' list shows them (an alias in brackets there: /clear (new)).
COMMANDS = [{"name": n, "description": d, "hint": h} for n, d, h in (
    ("add-dir", "Add a directory to the workspace", "<path>"),
    ("agents", "List available custom agents", ""),
    ("artifact", "View and review artifacts", ""),
    ("boost", "Invoke the Boost multi-agent orchestrator for complex tasks", ""),
    ("browser", "Invoke a browser agent for web tasks", ""),
    ("btw", "Ask a side question without interrupting the current task", "<question>"),
    ("changelog", "Show release notes and changes", ""),
    ("clear", "Clear conversation and start a new one", ""),
    ("codesearch", "Search code in the workspace", "<query>"),
    ("compact", "Compact the conversation now (summarize history to free up the context window)", ""),
    ("config", "Open settings panel", ""),
    ("context", "Visualize current context usage", ""),
    ("copy", "Copy the last planner response to the clipboard", "[n|btw]"),
    ("credits", "Show remaining credits and purchase link", ""),
    ("diff", "View uncommitted changes and per-turn diffs", ""),
    ("effort", "Set the reasoning effort", ""),
    ("exit", "Exit the CLI", ""),
    ("feedback", "Submit qualitative feedback to improve the agent", ""),
    ("fork", "Create a branch of the current conversation at this point", "[project id]"),
    ("goal", "Run until the specified goal is completely finished", "<goal>"),
    ("grill-me", "Interview me to align on a plan", ""),
    ("help", "Show available commands and keybindings", ""),
    ("hooks", "Manage hook configurations for tool events", ""),
    ("keybindings", "Set custom keybindings", ""),
    ("learn", "Reflect on recent successes or corrections to capture reusable skills or rules", ""),
    ("logout", "Log out", ""),
    ("mcp", "Manage MCP servers", ""),
    ("model", "Set a model, or run a single prompt on another model", "[model] [prompt]"),
    ("open", "Open a file or view opened/edited files", ""),
    ("permissions", "Manage tool permissions", ""),
    ("plan", "Plan carefully before executing a task", ""),
    ("plugin", "Manage, discover, install, uninstall, enable, and disable plugins", ""),
    ("projects", "Create and edit projects", ""),
    ("record", "Dictate a prompt using your microphone", ""),
    ("remote-control", "Turn on remote control for this session (off to disable)", "[off]"),
    ("rename", "Rename the current conversation", "<name>"),
    ("resume", "Browse and resume past conversations", ""),
    ("rewind", "Rewind conversation to a previous message", ""),
    ("schedule", "Run an instruction on a recurring schedule or as a one-time timer", ""),
    ("skills", "List available skills", ""),
    ("statusline", "Toggle the statusline", ""),
    ("swarm", "Run a swarm: several agents working in parallel on one task", ""),
    ("tasks", "View background tasks", ""),
    ("teamwork", "Invoke a team of agents to autonomously tackle large projects", ""),
    ("title", "Toggle custom terminal window title", ""),
    ("usage", "View model quota usage", ""),
    ("voice", "Dictate a prompt or talk with the agent by voice", ""),
)]

# How each command works from the page: the panels, pickers and menus need the terminal; /model and /effort open the
# Settings dialog at their row (a restart applies them); /context and /usage open the Usage panel.
_TERMINAL = {"agents", "artifact", "changelog", "config", "copy", "credits", "diff", "help", "hooks", "keybindings",
             "logout", "mcp", "open", "permissions", "plugin", "projects", "record", "resume", "rewind", "skills",
             "tasks", "voice"}
for _c in COMMANDS:
    _c["run"] = "terminal" if _c["name"] in _TERMINAL else "now"
    if _c["name"] in ("model", "effort"):
        _c["setting"], _c["run"] = _c["name"], "terminal"
    if _c["name"] in ("context", "usage"):
        _c["page"], _c["run"] = "usage", "terminal"


def _skill_description(text):
    """The description in a SKILL.md's front matter, or ''."""
    m = re.match(r"\A---\s*\n(.*?)\n---", text, re.S)
    got = re.search(r"(?m)^description:\s*(.+)$", m.group(1)) if m else None
    return got.group(1).strip().strip("'\"") if got else ""


def read_commands(folder):
    """agy's skills run as slash commands: each <skills folder>/<name>/SKILL.md of the project (.agents/skills) and of
    the person (~/.gemini/antigravity-cli/skills, ~/.gemini/skills, ~/.gemini/config/skills; customization-discovery).
    A project skill replaces a person's skill of the same name."""
    home = Path.home() / ".gemini"
    roots = [home / "config" / "skills", home / "skills", data_dir() / "skills"]
    roots += [Path(folder) / ".agents" / "skills"] if folder else []
    found = {}
    for root in roots:
        if not root.is_dir():
            continue
        for p in sorted(root.glob("*/SKILL.md")):
            name = re.sub(r"[^a-zA-Z0-9_\-.]", "_", p.parent.name)[:50]
            try:
                desc = _skill_description(p.read_text(encoding="utf-8"))
            except OSError:
                continue
            found[name] = {"name": name, "description": " ".join(desc.split())[:100] or f"Skill {name}", "hint": ""}
    return list(found.values())


# ---------------------------------------------------------------------------------------------------------------
# 11. Setup
# ---------------------------------------------------------------------------------------------------------------

SETUP = {
    # agy reads AGENTS.md (and GEMINI.md) in the project folder and each folder above it.
    "instructions": "AGENTS.md",
    # Skills for agy alone; ~/.gemini/config/skills holds skills for the Antigravity app too.
    "skills_dir": "~/.gemini/antigravity-cli/skills",
    "voice": "/voice dictates a prompt or talks with agy by voice.",
    "open_link": None,
}

# 12. Update (#6430): agy --print answers one request and exits. Homebrew's cask antigravity-cli has the newest version
# also for an agy from Google's script.
UPDATE = {"smoke": ["--print", "Reply OK", "--model", "{model}"], "cask": "antigravity-cli"}


def write_mcp_config(path=None, remove=False):
    """Add (or, with remove, take out) MaximizePM's MCP server in agy's MCP file, ~/.gemini/config/mcp_config.json
    (path: another file), as mcpServers.maxpm = {command, args, env}: setup_entry(), the stdio server `maxpm mcp`,
    with no MAXPM_CHAT (an agy session runs in a project folder). The form is the one `agy mcp add` writes. Other
    servers stay; the old file is kept as mcp_config.json.bak. Returns one text that says what changed."""
    from . import setup_entry
    from ..core import RiverError
    f = Path(path).expanduser() if path else Path.home() / ".gemini" / "config" / "mcp_config.json"
    entry = setup_entry()
    entry["env"].pop("MAXPM_CHAT", None)
    old = f.read_text(encoding="utf-8") if f.exists() else ""
    try:
        data = json.loads(old) if old.strip() else {}
    except ValueError:
        raise RiverError(f"{f} is not plain JSON, so MaximizePM does not rewrite it; fix it first") from None
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        raise RiverError(f"{f}: expected an object with an mcpServers object; fix it first")
    servers = data.setdefault("mcpServers", {})
    if remove:
        if "maxpm" not in servers:
            return f"{f} has no MaximizePM entry; nothing changed"
        del servers["maxpm"]
    else:
        if servers.get("maxpm") == entry:
            return f"MaximizePM is already in {f}; nothing changed"
        servers["maxpm"] = entry
    if old:
        f.with_name(f.name + ".bak").write_text(old, encoding="utf-8")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    if remove:
        return f"took MaximizePM out of {f} (the old file: {f.name}.bak). A new agy session has no maxpm tools."
    return (f"added MaximizePM to {f}" + (f" (the old file: {f.name}.bak)" if old else "") +
            ". A new agy session has the maxpm tools (/mcp lists them).")
