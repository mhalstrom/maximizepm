"""The Gemini CLI adapter (docs/agent-adapters.md, #5174): Google's coding agent, the command `gemini` (npm package
@google/gemini-cli, Apache-2.0).

Read from the CLI's own source, version 0.68.0 (packages/cli and packages/core of github.com/google-gemini/gemini-cli):
the yargs options of `gemini` (config.ts), the chat recording service (chatRecordingService.ts, the JSONL session
files), the hook system (hooks/*.ts), the custom command loader (FileCommandLoader.ts), the built-in commands
(BuiltinCommandLoader.ts) and the text of the interactive UI (InputPrompt, LoadingIndicator, ToolConfirmationMessage,
FolderTrustDialog, AuthDialog).

The sources this adapter declares, one for each signal:

- State: the screen (DETECT). Gemini CLI has hooks (BeforeAgent, AfterAgent, Notification, SessionStart, SessionEnd,
  in settings.json, with the payload session_id, transcript_path, cwd, hook_event_name, timestamp), but they are not
  a whole source of the state: AfterAgent does not fire when the person cancels a turn (Esc) or when the turn ends in
  an error (GeminiClient.sendMessageStream fires it only after processTurn returns), and no settings hook runs in a
  folder that the person has not trusted yet ("Project hooks disabled because the folder is not trusted"; folder
  trust is on by default). The screen shows each of these.
- Session id: IDENTITY, the new log file. Each session writes ~/.gemini/tmp/<project>/chats/session-<YYYY-MM-DDTHH-MM>-
  <first 8 characters of the id>.jsonl, and its first line holds the whole sessionId. --resume <id> appends to the
  same file, so a resumed session has no second file.
"""

import json
import os
import re
from pathlib import Path

from . import Raw

# ---------------------------------------------------------------------------------------------------------------
# 1. Describe
# ---------------------------------------------------------------------------------------------------------------

DESCRIBE = {
    "id": "gemini",
    "label": "Gemini CLI",
    "exe": "gemini",
    "aliases": ["gemini-cli"],
    # Gemini's models are no family of model_ladder: an item's --min-model and --max-model do not apply to them,
    # as for a provider/model agent (FAMILIES: "multi").
    "family": "multi",
    # The aliases that `gemini --model` takes as they are (models.ts: GEMINI_MODEL_ALIAS_*): auto lets Gemini route
    # each request between pro and flash. A full id ("gemini-3.1-pro-preview") goes as it is too.
    "models": ["auto", "pro", "flash", "flash-lite"],
    "default_model": "",
    "model_ids": {},
    "prefix": "gemini_",
    # /model set <name> changes the model of a running session for that session only (setModelCommand; --persist
    # would write it into the settings).
    "model_live": "/model set {value}",
    "model_read": r"(?:^|\s)(?:--model|-m)(?:=|\s+)(\S+)",
}

# ---------------------------------------------------------------------------------------------------------------
# 2. Settings
# ---------------------------------------------------------------------------------------------------------------

APPROVAL_MODES = ["default", "auto_edit", "yolo", "plan"]

SETTINGS = [
    {"key": "approval_mode", "label": "Approval mode", "kind": "choice", "choices": APPROVAL_MODES, "default": "",
     "text": "default asks before each tool; auto_edit allows file edits; yolo allows every tool; plan only reads. "
             "Empty: Gemini's own setting.",
     "flag": ["--approval-mode", "{value}"],
     # Shift+Tab cycles the mode in a running session; no command that MaximizePM types sets one value.
     "live": None, "read": r"--approval-mode(?:=|\s+)(default|auto_edit|yolo|plan)\b", "session": True,
     "note": "A restart applies it; Shift+Tab in the session cycles the mode for now."},
    {"key": "sandbox", "label": "Sandbox", "kind": "toggle", "choices": [], "default": "off",
     "text": "Run its tools in Gemini's sandbox (Seatbelt on macOS, or a container).",
     "flag": ["--sandbox"], "live": None, "read": None, "session": True},
    {"key": "skip_trust", "label": "Trust the folder", "kind": "toggle", "choices": [], "default": "off",
     "text": "Trust the project folder for this session, so Gemini does not ask 'Do you trust the files in this "
             "folder?' at the start.",
     "flag": ["--skip-trust"], "live": None, "read": None},
    {"key": "args", "label": "Other arguments", "kind": "text", "choices": [], "default": "",
     "text": "More words for the gemini command line, as you type them in a shell.",
     "flag": [], "live": None, "read": None},
]

# Gemini CLI has no effort flag and no effort command: the thinking level is part of its model configs
# (settings.json modelConfigs), per model. The page hides effort for it.
EFFORT = None

# ---------------------------------------------------------------------------------------------------------------
# 4. Launch
# ---------------------------------------------------------------------------------------------------------------

# No session name (Gemini titles a session by itself: the summary in its log), no system prompt file (its system
# prompt comes from GEMINI_SYSTEM_MD, a variable, not a word of the command), no hooks file (its hooks live in
# settings.json), no fork. --include-directories adds the queue folder to its workspace; --resume <id> resumes.
LAUNCH = {"session_name": False, "system_prompt_file": False, "hooks_file": False, "extra_dirs": True, "fork": False,
          "resume": True, "autocompact": False, "background": False}


def _on(value):
    return str(value or "").strip().lower() in ("on", "true", "1", "yes")


def build_command(name, folder, model, effort, settings, prompt):
    """gemini [--resume <id>] [--model M] [--approval-mode A] [--sandbox] [--skip-trust] --include-directories <queue
    folder> <args> [--prompt-interactive <prompt>]. Gemini has no flag for a session name, and no effort. The first
    prompt goes with --prompt-interactive: Gemini runs it and stays open. A bare positional prompt does the same in a
    terminal, but prints a notice that positional arguments now default to interactive mode."""
    from ..core import river_dir
    s = settings
    words = ["gemini"] + (["--resume", s["resume"]] if s.get("resume") else [])
    words += ["--model", model] if model else []
    mode = (s.get("approval_mode") or "").strip()
    if mode:
        words += [w.format(value=mode) for w in SETTINGS[0]["flag"]]
    words += ["--sandbox"] if _on(s.get("sandbox")) else []
    words += ["--skip-trust"] if _on(s.get("skip_trust")) else []
    words += ["--include-directories", river_dir()]
    words += [Raw(s["args"].strip())] if (s.get("args") or "").strip() else []
    words += ["--prompt-interactive", prompt] if prompt else []
    return words


def resume_command(session_ref, model, effort, settings, name=None):
    """gemini --resume <session id>, with the profile's settings and settings['prompt'] as its first prompt. Gemini
    finds the id among the sessions of the project folder it runs in (SessionSelector.resolveSession), so the resume
    runs in the session's own folder."""
    if not session_ref:
        return None
    return build_command(name, None, model, effort, {**settings, "resume": session_ref}, settings.get("prompt"))


def parse_command(words):
    """A hand-written gemini command that a profile builds exactly: --model {model}, --approval-mode with a known
    value, --sandbox, --skip-trust, --include-directories of the queue folder, and one prompt (with
    --prompt-interactive or -i, or as the one positional word). Anything else: None."""
    from ..core import river_dir
    if not words or Path(words[0]).name.lower() not in ("gemini", "gemini-cli"):
        return None
    opts, prompt, i = {}, None, 1
    while i < len(words):
        t, nxt = words[i], (words[i + 1] if i + 1 < len(words) else None)
        if t in ("-m", "--model") and nxt == "{model}":
            i += 2
        elif t == "--approval-mode" and nxt in APPROVAL_MODES:
            opts["approval_mode"] = nxt
            i += 2
        elif t.startswith("--approval-mode=") and t.split("=", 1)[1] in APPROVAL_MODES:
            opts["approval_mode"] = t.split("=", 1)[1]
            i += 1
        elif t in ("-y", "--yolo"):
            opts["approval_mode"] = "yolo"
            i += 1
        elif t in ("-s", "--sandbox"):
            opts["sandbox"] = "on"
            i += 1
        elif t == "--skip-trust":
            opts["skip_trust"] = "on"
            i += 1
        elif t == "--include-directories" and nxt is not None and (
                nxt == "{maxpm_dir}" or os.path.realpath(os.path.expanduser(nxt)) == os.path.realpath(river_dir())):
            i += 2
        elif t in ("-i", "--prompt-interactive") and nxt is not None and prompt is None:
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

# Gemini sets GEMINI_CLI=1 in the environment of each shell command it runs (GEMINI_CLI_IDENTIFICATION_ENV_VAR), so a
# maxpm command that Gemini runs knows it runs inside Gemini. Gemini has no web link for a session.
IDENTITY = {"source": "log", "env": None, "log_glob": "~/.gemini/tmp/*/chats/session-*.jsonl", "pane": None,
            "url": None, "inside_env": ["GEMINI_CLI"]}


def gemini_dir(env=None):
    """Gemini's own folder: ~/.gemini, or $GEMINI_CLI_HOME/.gemini (Storage.getGlobalGeminiDir)."""
    return Path((os.environ if env is None else env).get("GEMINI_CLI_HOME") or Path.home()) / ".gemini"


LOG_RECENT = 600  # seconds: a command runs in a turn, and the turn wrote its session's log a short time before


def session_from_log(folder, env=None, now=None):
    """The id of the Gemini session that a command in `folder` runs in (#5819), for core.session_from_env: Gemini 0.68
    gives its shell no session id variable (only GEMINI_CLI=1). Gemini records a tool call in its log only after the
    call ends, so the log cannot name the command that runs now; the turn that runs it wrote the log a short time
    before. So: the main log (its first line holds a sessionId; a stray session-*.jsonl with none, seen after a
    resume, does not count) of the project of `folder` or of a folder above it (projects.json) that was written in
    the last LOG_RECENT seconds. None when there is none, or when two main logs of the project were written in that
    time: two Gemini sessions work in one folder, and a wrong id is worse than none."""
    import time
    gdir = gemini_dir(env)
    try:
        projects = json.loads((gdir / "projects.json").read_text(encoding="utf-8")).get("projects") or {}
    except (OSError, ValueError, AttributeError):
        return None
    real = {os.path.realpath(p): slug for p, slug in projects.items()}
    here, slug = Path(os.path.realpath(folder)), None
    for d in [here, *here.parents]:
        slug = real.get(str(d))
        if slug:
            break
    if not slug:
        return None
    since = (now if now is not None else time.time()) - LOG_RECENT
    recent = []
    for p in (gdir / "tmp" / slug / "chats").glob("session-*.jsonl"):
        try:
            t = p.stat().st_mtime
        except OSError:
            continue
        if t >= since:
            recent.append((t, p))
    ids = {r.get("sessionId") for _, p in sorted(recent, reverse=True)
           for r in [_first_record(p)] if isinstance(r, dict) and r.get("sessionId")}
    return next(iter(ids)) if len(ids) == 1 else None


def _first_record(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.loads(f.readline() or "{}")
    except (OSError, ValueError):
        return {}


def _project_slug(folder):
    """The short name of folder's project in ~/.gemini/projects.json (its chats are in tmp/<slug>/chats), or None."""
    try:
        projects = json.loads((gemini_dir() / "projects.json").read_text(encoding="utf-8")).get("projects") or {}
    except (OSError, ValueError, AttributeError):
        return None
    want = os.path.realpath(os.path.expanduser(str(folder)))
    return next((slug for path, slug in projects.items() if os.path.realpath(path) == want), None)


def chat_path(session_ref, folder=None, root=None):
    """The log of Gemini session `session_ref` (its whole id): the session-*-<id[:8]>.jsonl file whose first line
    holds that sessionId, in the chats folder of `folder`'s project first, then in any project. The newest such file
    when there are two. None when no file has it. root: the folder of the projects' chat folders, in place of
    ~/.gemini/tmp (the tests give one)."""
    ref = str(session_ref or "").strip()
    if not ref:
        return None
    if os.path.isfile(ref):
        return Path(ref)
    short = re.sub(r"[^a-zA-Z0-9_-]", "_", ref)[:8]
    tmp = Path(root) if root else gemini_dir() / "tmp"
    slug = _project_slug(folder) if folder else None
    for where in ([tmp / slug / "chats"] if slug else []) + [tmp]:
        pattern = f"session-*-{short}.jsonl" if where != tmp else f"*/chats/session-*-{short}.jsonl"
        found = [p for p in where.glob(pattern) if _first_record(p).get("sessionId") == ref]
        if found:
            return max(found, key=lambda p: p.stat().st_mtime)
    return None


# ---------------------------------------------------------------------------------------------------------------
# 6. Chat log
# ---------------------------------------------------------------------------------------------------------------

# write_file writes a whole file; replace is Gemini's edit tool (EDIT_TOOL_NAME). The log of an agent's session is
# found by its session id (agent_sessions). Gemini's context cache costs no more to write than plain input.
CHAT = {"compressed": False, "file_tools": ["write_file", "replace"], "ref": "session", "cache_write": False}

_IGNORED_USER = ("<session_context>", "<hook_context>")
_DONE = ("success", "error", "cancelled")


def _records(lines):
    """The session's messages as Gemini itself reads its JSONL file (createJsonlRecordAccumulator): a record with an
    id adds a message, or replaces the message with that id in place; {"$rewindTo": id} removes that message and all
    after it (all, when the id is unknown); {"$patch": ...} changes the content or tool results of a message, removes
    messages or orders them; {"$set": ...} changes the session's metadata. The first line is the metadata."""
    meta, msgs = {}, {}
    for line in lines:
        line = line.strip() if isinstance(line, str) else line
        if not line:
            continue
        try:
            r = json.loads(line) if isinstance(line, str) else line
        except ValueError:
            continue
        if not isinstance(r, dict):
            continue
        if isinstance(r.get("$rewindTo"), str):
            ids = list(msgs)
            cut = ids.index(r["$rewindTo"]) if r["$rewindTo"] in msgs else 0
            for k in ids[cut:]:
                del msgs[k]
        elif isinstance(r.get("$patch"), dict):
            p = r["$patch"]
            for u in ([p] if isinstance(p.get("id"), str) else []) + [u for u in p.get("updates") or () if isinstance(u, dict)]:
                m = msgs.get(u.get("id"))
                if not m:
                    continue
                if u.get("content") is not None:
                    m["content"] = u["content"]
                for tp in u.get("toolCalls") or ():
                    tc = next((t for t in m.get("toolCalls") or () if t.get("id") == tp.get("id")), None)
                    if tc is not None and "result" in tp:
                        tc["result"] = tp["result"]
            for k in p.get("removeIds") or ():
                msgs.pop(k, None)
            if isinstance(p.get("orderIds"), list):
                ordered = [k for k in p["orderIds"] if k in msgs]
                rest = [k for k in msgs if k not in set(ordered)]
                msgs = {k: msgs[k] for k in rest + ordered}
        elif isinstance(r.get("$set"), dict):
            meta.update(r["$set"])
        elif isinstance(r.get("id"), str):
            msgs[r["id"]] = dict(r)
        elif isinstance(r.get("sessionId"), str):
            meta.update(r)
    return meta, list(msgs.values())


def _text(content):
    """The text of a message's content: a string, or a list of parts ({"text"}, ...)."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        content = [content]
    return "".join(p.get("text", "") for p in content or () if isinstance(p, dict) and isinstance(p.get("text"), str))


def _images(content):
    out = []
    for p in content if isinstance(content, list) else ():
        if not isinstance(p, dict):
            continue
        d = p.get("inlineData")
        if isinstance(d, dict) and d.get("data") and str(d.get("mimeType", "")).startswith("image/"):
            out.append(f"data:{d['mimeType']};base64,{d['data']}")
        f = p.get("fileData")
        if isinstance(f, dict) and f.get("fileUri") and str(f.get("mimeType", "")).startswith("image/"):
            out.append(f["fileUri"])
    return out


def _only_tool_results(content):
    return isinstance(content, list) and content and all(
        isinstance(p, dict) and "functionResponse" in p for p in content)


def _detail(tc):
    args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
    for k in ("command", "file_path", "absolute_path", "dir_path", "path", "pattern", "query", "url", "prompt"):
        if isinstance(args.get(k), str) and args[k].strip():
            return args[k].strip()
    return (tc.get("description") or "").strip() or (json.dumps(args, ensure_ascii=False) if args else "")


def _shown_text(shown):
    """The text of a tool call's resultDisplay as Gemini 0.68 writes it: a string; for a shell command the terminal's
    lines, each a list of styled spans ({"text", "bold", "inverse", ...}); for a file edit an object with the diff
    (fileDiff). None for another form."""
    if isinstance(shown, str):
        return shown
    if isinstance(shown, dict):
        return shown["fileDiff"] if isinstance(shown.get("fileDiff"), str) else None
    if isinstance(shown, list) and all(isinstance(line, list) for line in shown):
        lines = ["".join(s.get("text", "") for s in line if isinstance(s, dict)).rstrip() for line in shown]
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)
    return None


def _result_text(tc):
    """What a tool call gave back: what Gemini showed the person (resultDisplay), else the response to the model.
    A refused or failed edit keeps the diff it proposed in resultDisplay: its error is the response's
    ("[Operation Cancelled] Reason: User denied execution.")."""
    display = tc.get("resultDisplay")
    shown = _shown_text(display)
    if shown is not None and (isinstance(display, str) or tc.get("status") in (None, "success")):
        return shown
    out = []
    for p in tc.get("result") or ():
        resp = (p.get("functionResponse") or {}).get("response") if isinstance(p, dict) else None
        if isinstance(resp, dict):
            v = resp.get("output", resp.get("error", ""))
            out.append(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
    return "\n".join(out)


def _message(m, text=True, tools=True):
    """(entries, turn) of one message. text: the model's text of a gemini message; tools: its tool calls."""
    at, kind, out, turn = m.get("timestamp"), m.get("type"), [], None
    if kind == "user":
        content = m.get("content")
        if _only_tool_results(content):
            return [], "working"
        shown = m.get("displayContent")
        said = _text(shown if shown is not None else content).strip()
        if said.startswith(_IGNORED_USER):
            return [], None
        e = {"at": at, "role": "user", "text": said}
        if _images(content):
            e["images"] = _images(content)
        return [e], "working"
    if kind == "gemini":
        said = _text(m.get("content")).strip()
        calls = [tc for tc in m.get("toolCalls") or () if isinstance(tc, dict)]
        if text and said:
            out.append({"at": at, "role": "assistant", "text": said})
        for tc in calls if tools else ():
            when = tc.get("timestamp") or at
            out.append({"at": when, "role": "tool", "text": tc.get("displayName") or tc.get("name", ""),
                        "id": tc.get("id", ""), "name": tc.get("name", ""), "detail": _detail(tc)})
            if tc.get("status") in _DONE:
                out.append({"at": when, "role": "result", "text": _result_text(tc), "for": tc.get("id", ""),
                            "error": tc.get("status") != "success"})
        if any(tc.get("status") not in _DONE for tc in calls):
            turn = "working"
        elif any(tc.get("status") == "cancelled" for tc in calls):
            turn = "idle"
        elif calls or not said:
            turn = "working"  # the model answers the tool results next; an empty line asked for tools
        else:
            turn = "idle"
        return out, turn
    if kind in ("info", "warning", "error"):
        said = _text(m.get("content")).strip()
        return ([{"at": at, "role": "event", "text": said, "level": kind}] if said else []), None
    return [], None


def parse_chat(lines):
    """(entries, turn) of the lines of one Gemini session file, read as Gemini reads it (_records: the last version
    of each message, with rewinds and patches). A user line is the person's prompt (displayContent, what the person
    typed, when Gemini expanded @file references in content); a gemini line is the model's text and its tool calls,
    each with its result (status success, error or cancelled); info, warning and error lines are events. The
    <session_context> setup message, the tool results that go back to the model as a user line (functionResponse
    parts) and the model's thoughts are no entries.
    The turn: working after a prompt, after a model line with no text (it asked for tools) and after tool results;
    idle after the model's text, or after a cancelled tool call; None when the file has no message yet. Gemini 0.68
    records the tool calls of a reply only when they are done, so while they run the reply's line has no calls yet:
    a reply with text then reads as idle (the screen, DETECT, is the state source). A turn that the person cancels
    (Esc) ends in an info line "Request cancelled.", then a $patch that removes it from the history: the cancelled
    prompt and the info line, or the reply with the refused tool call and the info line (seen in a real 0.68 log,
    session-2026-11-14T00-15-006c1604.jsonl). The text that streamed before the cancel is not in the log."""
    entries, turn = [], None
    for m in _records(lines)[1]:
        got, st = _message(m)
        entries += got
        turn = st or turn
    return entries, turn


def parse_chat_line(d):
    """(entries, turn) of one line of the log, for the Sessions tab, which reads the log line by line. Gemini 0.68
    writes each model reply as a line with its text, then the same message once more with its tool calls when the
    whole batch is done (ChatRecordingService.recordToolCalls appends them to the last message). So the text comes
    from a gemini line with no toolCalls, and the calls and results from a gemini line with them: each shows once. A
    rewind or a patch changes nothing that is already shown (parse_chat applies them)."""
    if not isinstance(d, dict) or not isinstance(d.get("id"), str) or "$patch" in d:
        return [], None
    if d.get("type") == "gemini":
        has_calls = bool(d.get("toolCalls"))
        return _message(d, text=not has_calls, tools=has_calls)
    return _message(d)


def chat_title(lines):
    """The summary Gemini wrote for the session ($set summary), else the person's first prompt, on one line."""
    meta, msgs = _records(lines)
    if isinstance(meta.get("summary"), str) and meta["summary"].strip():
        return " ".join(meta["summary"].split())
    for e in parse_chat(lines)[0]:
        if e["role"] == "user" and e["text"]:
            return " ".join(e["text"].split())[:120]
    return None


def chat_cwd(lines):
    """The folder of the session: the first of the workspace directories in its <session_context> message."""
    _, msgs = _records(lines)
    for m in msgs:
        if m.get("type") == "user":
            got = re.search(r"Workspace Directories:\*{0,2}\s*\n\s*-\s*(\S[^\n]*)", _text(m.get("content")))
            if got:
                return got.group(1).strip()
    return None


def chat_usage(lines):
    """The token counts of the session, from the tokens of each model line (its last version): input without the
    cached part, output with the thinking tokens, the cached part as cache_read. Gemini writes no cache."""
    _, msgs = _records(lines)
    out = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    for m in msgs:
        t = m.get("tokens") if m.get("type") == "gemini" else None
        if not isinstance(t, dict):
            continue
        cached = int(t.get("cached") or 0)
        out["input"] += max(int(t.get("input") or 0) - cached, 0)
        out["cache_read"] += cached
        out["output"] += int(t.get("output") or 0) + int(t.get("thoughts") or 0)
    return out


# ---------------------------------------------------------------------------------------------------------------
# 7. State: the screen
# ---------------------------------------------------------------------------------------------------------------

STATE = {"state": "detect", "session_id": "identity"}

# The pane watcher of maxpm serve (server.watch_prompts) reads a blocked screen first: a question and a menu that are
# the last thing on the screen. Gemini shows nothing under them: while a tool waits for approval it hides its whole
# input area, the loading line and the footer included (ui.collapseDrawerDuringApproval, on by default), and the
# trust and sign-in dialogs take the place of the input area. Else the watcher reads working, then idle: the input
# line stays on the screen while Gemini works (a person can type the next prompt).
# The trust question for a folder that --include-directories adds (#5818): Gemini asks it at each start for the queue
# folder, also with --skip-trust (that trusts only the main folder), until the folder has a rule in Gemini's own
# trustedFolders.json: its option "2. Yes, and remember the directories as trusted" writes one. No flag or setting of
# one launch avoids it without replacing Gemini's own trust file or turning folder trust off (p140).
TRUST_ADDED = r"Do you trust the following folders being added to this workspace\?"
DETECT = {
    # The gemini command is a node script (#!/usr/bin/env node): tmux names the pane's process node, and ps shows
    # 'node /opt/homebrew/bin/gemini' (and a child 'node --max-old-space-size=... /opt/homebrew/bin/gemini'). The
    # pane watcher finds the script's name on the pane's terminal (#5223); "node" would match every node program
    # (#5227).
    "process": "gemini",
    # The prompt sign of the input line: > (or * in yolo mode, ! in shell mode), then the empty-box text or the
    # person's draft. Gemini shows each earlier prompt of the chat with "> " too, so this holds only after the
    # blocked and working rules found nothing: the input line is there whenever no dialog covers it.
    "idle": r"^[\s│]*[>*!] +(?:Type your message or @path/to/file|\S)",
    # LoadingIndicator: "<phrase> (esc to cancel, 12s)" while a turn streams.
    "working": r"\(esc to cancel, \d",
    "blocked": [
        r"Allow execution of",                       # a shell command ("Allow execution of [Shell]?"), an MCP tool
        r"Apply this change\?",                      # a file edit
        r"Do you trust the files in this folder\?",  # folder trust, at the start
        TRUST_ADDED,                                 # the queue folder (--include-directories), at each start
        r"How would you like to authenticate for this project\?",  # sign-in, at the start
        r"Answer Questions",                          # ask_user: Gemini asks the person questions
        r"Ready to start implementation\?",          # exit_plan_mode
    ],
    # The option of each tool confirmation that refuses and lets the person tell Gemini what to do instead.
    "tell_instead": r"No, suggest changes \(esc\)",
    # A pasted image becomes @<path of the saved clipboard file>, with no number.
    "image_paste": None,
    # The menu row of the selected option: "● 1. Allow once" (BaseSelectionList's selectedIndicator); the others
    # are "  2. Allow for this session". The frame and the rule are those of DETECT_LAYOUT.
    "menu": "●",
    # #5818: a first prompt (--prompt-interactive) runs while the added folder's trust question waits. Gemini then
    # draws that question under the tool question, but the keys answer the tool question first
    # (pane_blocked_trust_dirs_tool.txt): the page must show the tool question.
    "behind": TRUST_ADDED,
    # #5817: the header of a tool question: "?", the tool's display name, two spaces, then what it runs or changes
    # ("? Shell  touch notes.txt", "? Edit  calc.py: def add(a, b): => def plus(a, b):").
    "header": r"^\s*\? (\S+(?: \S+)*?)(?:\s{2,}(\S.*))?$",
}

# ---------------------------------------------------------------------------------------------------------------
# 8. Input
# ---------------------------------------------------------------------------------------------------------------

# Stop on the Sessions tab (#5547): Esc cancels the turn ('esc to cancel, 12s').
INPUT = {"native": None, "interrupt": ["Escape"]}

# ---------------------------------------------------------------------------------------------------------------
# 10. Commands
# ---------------------------------------------------------------------------------------------------------------

# The built-in slash commands of 0.68.0 that every session has (BuiltinCommandLoader; /agents, /hooks, /oncall and
# the IDE ones show only with their setting).
COMMANDS = [{"name": n, "description": d, "hint": h} for n, d, h in (
    ("about", "Show version info", ""),
    ("auth", "Manage authentication", ""),
    ("bug", "Submit a bug report", "<description>"),
    ("chat", "Browse auto-saved conversations and manage chat checkpoints", "list|save|resume|delete|share <tag>"),
    ("clear", "Clear the screen and start a new session", ""),
    ("commands", "Manage custom slash commands", "list|reload"),
    ("compress", "Compresses the context by replacing it with a summary", ""),
    ("copy", "Copy the last result or code snippet to clipboard", ""),
    ("directory", "Manage workspace directories", "add|show <path>"),
    ("docs", "Open full Gemini CLI documentation in your browser", ""),
    ("editor", "Set external editor preference", ""),
    ("export-session", "Export the current session to a JSON file", ""),
    ("extensions", "Manage extensions", "list|update|explore"),
    ("help", "For help on gemini-cli", ""),
    ("init", "Analyzes the project and creates a tailored GEMINI.md file", ""),
    ("mcp", "Manage configured Model Context Protocol (MCP) servers", "list|desc|auth|refresh"),
    ("memory", "Commands for interacting with memory", "show|add|refresh|list"),
    ("model", "Manage model configuration", "set <model> [--persist]|manage"),
    ("permissions", "Manage folder trust settings and other permissions", ""),
    ("plan", "Switch to Plan Mode and view current plan", ""),
    ("policies", "Manage policies", ""),
    ("privacy", "Display the privacy notice", ""),
    ("quit", "Exit the cli", ""),
    ("restore", "Restore a tool call: reset the conversation and file history to before it", "<tool call id>"),
    ("resume", "Browse auto-saved conversations and manage chat checkpoints", ""),
    ("rewind", "Jump back to a specific message and restart the conversation", ""),
    ("settings", "View and edit Gemini CLI settings", ""),
    ("shortcuts", "Toggle the shortcuts panel above the input", ""),
    ("skills", "List, enable, disable, or reload Gemini CLI agent skills", "list|disable|enable|reload"),
    ("stats", "Check session stats", "session|model|tools"),
    ("tasks", "Toggle background tasks view", ""),
    ("theme", "Change the theme", ""),
    ("tools", "List available Gemini CLI tools", "desc"),
    ("vim", "Toggle vim mode on/off", ""),
    ("voice", "Toggle voice dictation mode", ""),
)]


def _toml_description(text):
    """The description of a custom command's TOML text; Python 3.10 has no tomllib, so a plain read of the one key."""
    try:
        import tomllib
        got = tomllib.loads(text).get("description")
        return got if isinstance(got, str) else ""
    except ImportError:
        pass
    except ValueError:
        return ""
    m = re.search(r'(?m)^\s*description\s*=\s*(?:"((?:[^"\\]|\\.)*)"|\'([^\']*)\')', text)
    if not m:
        return ""
    if m.group(1) is None:
        return m.group(2)  # a literal string: no escapes
    try:  # a basic string: its escapes (\" \\ \n \t \uXXXX) are JSON's
        return json.loads(f'"{m.group(1)}"')
    except ValueError:
        return m.group(1)


# How each command works from the page (#5220, #5290). Gemini CLI was not signed in on this Mac (its commands ran into
# its sign-in), so these come from its own descriptions: /model opens its model dialog (the Settings row Model);
# /stats shows the session's tokens (the Usage panel); the dialogs and lists need the terminal; the others run at once.
_RUNS = {
    "terminal": {"about", "auth", "chat", "commands", "directory", "docs", "editor", "extensions", "help", "mcp",
                 "memory", "permissions", "policies", "privacy", "restore", "resume", "rewind", "settings", "skills",
                 "theme", "tools"},
}
for _c in COMMANDS:
    _c["run"] = next((k for k, v in _RUNS.items() if _c["name"] in v), "now")
    if _c["name"] == "model":
        _c["setting"], _c["run"] = "model", "terminal"
    if _c["name"] == "stats":
        _c["page"], _c["run"] = "usage", "terminal"


def context_window(model):
    """The tokens of a Gemini model's window (#5290): the Gemini 2.5 and 3 models take 1,048,576 tokens of input; the
    aliases (auto, pro, flash, flash-lite) are those models. None for another model."""
    m = (model or "").lower()
    return 1_048_576 if m in ("auto", "pro", "flash", "flash-lite") or m.startswith("gemini-") else None


def chat_cache_line(raw, st):
    """Fold one line of a session file into the cache state (#5290, the context meter): a gemini message's tokens are
    one model call (its context: input with the cached part, output and thoughts), its model the message's model."""
    try:
        m = json.loads(raw)
    except (ValueError, TypeError):
        return
    t = m.get("tokens") if isinstance(m, dict) and m.get("type") == "gemini" else None
    if not isinstance(t, dict):
        return
    ctx = sum(int(t.get(k) or 0) for k in ("input", "output", "thoughts"))
    if ctx and m.get("timestamp"):
        st["last"] = {"start": m["timestamp"], "at": m["timestamp"], "context": ctx}
        if m.get("model"):
            st["model"] = m["model"]


def read_commands(folder):
    """The custom commands for a session in `folder`: each .toml file under ~/.gemini/commands and under
    <folder>/.gemini/commands; a file in a subfolder is <sub>:<name> (git/commit.toml is /git:commit). A project
    command replaces a user command of the same name, as in Gemini."""
    found = {}
    roots = [gemini_dir() / "commands"] + ([Path(folder) / ".gemini" / "commands"] if folder else [])
    for root in roots:
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*.toml")):
            rel = p.relative_to(root).with_suffix("")
            name = ":".join(re.sub(r"[^a-zA-Z0-9_\-.]", "_", part)[:50] for part in rel.parts)
            try:
                desc = _toml_description(p.read_text(encoding="utf-8"))
            except OSError:
                continue
            found[name] = {"name": name, "description": " ".join(desc.split())[:100] or f"Custom command from {p.name}",
                           "hint": ""}
    return list(found.values())


# ---------------------------------------------------------------------------------------------------------------
# 11. Setup
# ---------------------------------------------------------------------------------------------------------------

SETUP = {
    "instructions": "GEMINI.md",
    # GEMINI.md takes Gemini's own import: a token @<path> after whitespace (memoryImportProcessor, relative to the
    # file's folder), so maxpm init makes it the one line @AGENTS.md, as Claude Code's CLAUDE.md (#5218).
    "import": "@{file}",
    "skills_dir": "~/.gemini/skills",
    "voice": "/voice turns Gemini's voice dictation on or off.",
    "open_link": None,
}


def write_mcp_config(path=None, remove=False):
    """Add (or, with remove, take out) MaximizePM's MCP server in Gemini's user settings, ~/.gemini/settings.json
    (path: another file), as mcpServers.maxpm = {command, args, env}: setup_entry(), the stdio server `maxpm mcp`,
    with no MAXPM_CHAT (a Gemini session runs in a project folder). Other keys stay; the old file is kept as
    settings.json.bak. A file that is not plain JSON (Gemini allows comments) is not rewritten: RiverError names the
    gemini command that adds the server instead. Returns one text that says what changed."""
    import shlex
    from . import setup_entry
    from ..core import RiverError
    f = Path(path).expanduser() if path else gemini_dir() / "settings.json"
    entry = setup_entry()
    entry["env"].pop("MAXPM_CHAT", None)
    old = f.read_text(encoding="utf-8") if f.exists() else ""
    try:
        data = json.loads(old) if old.strip() else {}
    except ValueError:
        env = " ".join(f"-e {k}={shlex.quote(v)}" for k, v in entry["env"].items())
        raise RiverError(f"{f} is not plain JSON (comments?), so MaximizePM does not rewrite it; run "
                         f"`gemini mcp add --scope user {env} maxpm {shlex.join([entry['command'], *entry['args']])}` "
                         f"instead") from None
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
        return f"took MaximizePM out of {f} (the old file: {f.name}.bak). A new Gemini session has no maxpm tools."
    return (f"added MaximizePM to {f}" + (f" (the old file: {f.name}.bak)" if old else "") +
            ". A new Gemini session has the maxpm tools (/mcp lists them).")
