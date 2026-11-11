"""The Amp adapter (docs/agent-adapters.md, #5179): the program `amp` (ampcode.com, installed by its install.sh into
~/.amp/bin, with a link in ~/.local/bin).

Read from Amp CLI 0.0.1794369644 on macOS, 2026-11-11 (amp --help, amp plugins show-docs, amp threads export, and
sessions in a tmux pane; the fixtures in tests/fixtures/amp were recorded then):

- State comes from the screen (DETECT), as the design names it for Amp. Amp has a plugin API (TypeScript run by Bun,
  events session.start, agent.start, agent.end, tool.call, tool.result, and a thread state stream), but it loads
  plugins only from ~/.config/amp/plugins ($XDG_CONFIG_HOME/amp/plugins) and from the project's .amp/plugins: no flag
  or setting loads one for a single start. A plugin there runs in every later Amp session of the person, and a start
  with its own XDG_CONFIG_HOME would also move the config of every tool that the agent's shell commands run (gh,
  git). Also, the thread state stream gave no 'awaiting-approval' while an approval waited, and tool.call runs before
  the approval: no plugin event shows the blocked state. The screen shows all three states:
  - working: the bottom edge of the input box names the activity, "╰ ≋ Connecting ─", "╰ ≈ Waiting ─",
    "╰ ≋ Streaming ─", "╰ ≋ Running Tools ─" (the glyph and the letters animate);
  - idle: the bottom edge is a plain rule up to the folder, "╰────── /path (branch) ─╯";
  - blocked: a framed menu marked with ‣: "Approval Required" (Allow Once, Reject with feedback, Allow All for This
    Session, Allow All for Every Session) and "Out of Credits" (Add Paid Credits, Retry, Dismiss).
  The pane title follows the state too ("⠤ <title> - amp - <folder>" while it works, "◆ Input needed - amp -
  <folder>" while it asks), but the pane watcher reads the pane's text.
- The session is an Amp thread, T-<uuid>. Amp sets AMP_CURRENT_THREAD_ID (and AGENT_THREAD_ID, and AGENT=amp) in the
  environment of each shell command it runs: the maxpm commands of the session find their thread there (IDENTITY).
  The web link of a thread is https://ampcode.com/threads/<id>.
- The chat lives on Amp's server; the local logs (~/.cache/amp/logs) hold only debug lines. `amp threads export <id>`
  prints the whole thread as JSON in about half a second. chat_path runs it at most every REFRESH seconds and appends
  what is new to a JSONL file beside the queue (amp/threads/<id>.jsonl), one finished message per line, so the file
  only grows and the Sessions tab reads it line by line like any other log.
- Effort is Amp's agent mode (--mode low|medium|high|ultra), which also picks the model: medium ran claude-opus-5-5,
  low ran gpt-5.6-terra. No flag names a model, so DESCRIBE has no models and the family is multi. Ctrl+S cycles
  the mode in a running session; no typed text does.
- A first prompt goes on stdin (`echo "<prompt>" | amp`, Amp's documented form); the screen stays interactive.
  `amp threads continue <id>` resumes a thread. Amp keeps an unsent draft of each thread across restarts.
- Amp has no slash commands: "/" at the start of an empty prompt opens its command palette (as Ctrl+O does), whose
  list fills while the keys come. Typed at once, as the message box of the page sends text, "/amp show version" and
  Enter twice ended in a draft of a new thread in an orb (a paid remote machine). So COMMANDS is empty.
"""

import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

from . import Raw

DESCRIBE = {
    "id": "amp",
    "label": "Amp",
    "exe": "amp",
    "aliases": ["ampcode", "amp-cli"],
    # The mode picks the model (EFFORT); no flag names one, so MaximizePM offers none and no model limit applies.
    "family": "multi",
    "models": [],
    "default_model": "",
    "model_ids": {},
    "prefix": "amp_",
    "model_live": None,
    "model_read": None,
}

# ---------------------------------------------------------------------------------------------------------------
# Settings, effort, launch, identity and resume
# ---------------------------------------------------------------------------------------------------------------

SETTINGS = [
    {"key": "no_ide", "label": "No IDE context", "kind": "toggle", "default": "on",
     "text": "leave out the file and selection open in the person's editor (--no-ide): a session that MaximizePM "
             "starts works apart from the editor",
     "flag": ["--no-ide"], "live": None, "read": None, "session": True},
    {"key": "quiet", "label": "No sound alerts", "kind": "toggle", "default": "off",
     "text": "no sound when a turn ends or Amp asks (--no-notifications)",
     "flag": ["--no-notifications"], "live": None, "read": None, "session": True},
    {"key": "fast", "label": "Fast mode", "kind": "toggle", "default": "off",
     "text": "faster serving for the models that have it, billed at a premium (--fast)",
     "flag": ["--fast"], "live": None, "read": None, "session": True},
    {"key": "visibility", "label": "Thread visibility", "kind": "choice",
     "choices": ["private", "unlisted", "workspace", "group"], "default": "",
     "text": "who can see the thread on ampcode.com; empty: Amp's own default",
     "flag": ["--visibility", "{value}"], "live": None, "read": r"--visibility[ =](private|unlisted|workspace|group)\b"},
    {"key": "settings_file", "label": "Settings file", "kind": "text", "choices": [], "default": "",
     "text": "another Amp settings file (--settings-file); empty: ~/.config/amp/settings.json",
     "flag": ["--settings-file", "{value}"], "live": None, "read": r"--settings-file[ =](\S+)"},
    {"key": "args", "label": "More arguments", "kind": "text", "choices": [], "default": "",
     "text": "more arguments, put after the others", "flag": [], "live": None, "read": None},
    {"key": "prompt", "label": "First prompt", "kind": "text", "choices": [],
     "default": "run maxpm go in this folder and follow the briefing",
     "text": "the first prompt, on stdin (Amp reads AGENTS.md of the folder)", "flag": [], "live": None,
     "read": None},
]

MODES = ["low", "medium", "high", "ultra"]

EFFORT = {
    "levels": MODES,
    "map": {"xhigh": "ultra", "max": "ultra"},  # Amp's top mode is ultra
    "flag": ["--mode", "{value}"],
    "live": None,  # Ctrl+S cycles the mode; no text changes it
    # The mode on the command line, or the agentMode of the thread (a meta line of the chat file).
    "read": r'(?:--mode[ =]|-m |"agentMode":\s*")(low|medium|high|ultra)\b',
    "label": "Mode",
}

LAUNCH = {"resume": True}  # no session name, no system prompt flag, no hooks file, no extra folders, no fork

IDENTITY = {"source": "env", "env": "AMP_CURRENT_THREAD_ID", "log_glob": None, "pane": None,
            "url": "https://ampcode.com/threads/{id}", "inside_env": ["AMP_CURRENT_THREAD_ID"]}

_THREAD = re.compile(r"T-[0-9A-Za-z][0-9A-Za-z-]{7,63}\Z")


def _on(v):
    return v is True or str(v).strip().lower() in ("on", "true", "1", "yes")


def build_command(name, folder, model, effort, settings, prompt):
    """[printf '%s\\n' <prompt> |] amp [--mode M] [--no-ide] [--no-notifications] [--fast] [--visibility V]
    [--settings-file F] [args] [threads continue <id>]. The prompt goes on stdin, as Amp documents it. model: Amp
    takes none (its mode picks one), so it is left out; name: Amp takes none."""
    s = settings or {}
    words = [Raw(f"printf '%s\\n' {shlex.quote(prompt)} |")] if prompt else []
    words.append("amp")
    level = EFFORT["map"].get(effort, effort) if effort else None
    if level:
        words += [w.format(value=level) for w in EFFORT["flag"]]
    for spec in SETTINGS:
        k, v = spec["key"], s.get(spec["key"], spec["default"])
        if not spec["flag"]:
            continue
        if spec["kind"] == "toggle":
            words += spec["flag"] if _on(v) else []
        elif str(v or "").strip():
            words += [w.format(value=str(v).strip()) for w in spec["flag"]]
    if (s.get("args") or "").strip():
        words.append(Raw(s["args"].strip()))
    if s.get("resume"):
        words += ["threads", "continue", str(s["resume"])]
    return words


def resume_command(session_ref, model, effort, settings, name=None):
    """amp threads continue <thread id>, with the profile's settings and settings['prompt'] on stdin. The mode goes
    on only when it is given: else the thread keeps its own."""
    if not session_ref or not _THREAD.match(str(session_ref)):
        return None
    s = {k: v for k, v in (settings or {}).items() if k != "prompt"}
    return build_command(name, None, None, effort, {**s, "resume": str(session_ref)}, (settings or {}).get("prompt"))


def parse_command(words):
    """A hand-written amp command that a profile builds exactly: `echo <prompt> |` or `printf '%s\\n' <prompt> |`,
    then amp with --mode {effort} and the flags of the toggles and visibility. Anything else (execute mode, a thread
    to continue, another flag): None."""
    words = list(words or ())
    prompt = None
    if len(words) >= 4 and words[0] in ("echo", "printf") and "|" in words:
        bar = words.index("|")
        head = words[1:bar]
        if words[0] == "printf" and head[:1] == ["%s\\n"]:
            head = head[1:]
        if len(head) != 1:
            return None
        prompt, words = head[0], words[bar + 1:]
    if not words or Path(words[0]).name.lower() != "amp" or prompt is None:
        return None
    flags = {tuple(s["flag"]): s["key"] for s in SETTINGS if s["kind"] == "toggle"}
    opts, i = {}, 1
    while i < len(words):
        t, nxt = words[i], (words[i + 1] if i + 1 < len(words) else None)
        if t in ("--mode", "-m") and nxt == "{effort}":
            i += 2
        elif (t,) in flags:
            opts[flags[(t,)]] = "on"
            i += 1
        elif t == "--visibility" and nxt in SETTINGS[3]["choices"]:
            opts["visibility"] = nxt
            i += 2
        else:
            return None
    opts.setdefault("no_ide", "off")  # a hand-written command without --no-ide keeps the editor's context
    return {"settings": opts, "prompt": prompt}


# ---------------------------------------------------------------------------------------------------------------
# State: screen rules
# ---------------------------------------------------------------------------------------------------------------

STATE = {"state": "detect", "session_id": "identity"}

DETECT = {
    "process": "amp",
    # The bottom edge of the input box, a plain rule up to the folder: Amp waits at its prompt.
    "idle": r"^\s*╰─{8,}.*─╯\s*$",
    # The bottom edge with an activity on its left: "╰ ≋ Connecting ─", "╰ ≈ Waiting ─", "╰ ≋ Running Tools ─".
    "working": r"^\s*╰ \S+ [A-Za-z][A-Za-z ]*? ─",
    # The words of each menu that waits for a person, on the menu's own lines (a prompt's words must stand at or
    # under its menu): the approval of a tool call, and the end of the credits.
    "blocked": [r"\bReject with feedback\b", r"\bAllow All for (This|Every) Session\b",
                r"\bAdd Paid Credits \(ampcode\.com/pay\)"],
    "tell_instead": r"(?i)\breject with feedback\b",
    "image_paste": None,  # Ctrl+V pastes an image from the clipboard; a pasted path stays text
    "menu": "‣",
    # The lines that may stand under an approval menu: the edges of the input box (a rule with the mode or the
    # folder in it, and while a turn runs the activity before it: "≋ Running Tools ────"), and "Up next (1)" with
    # the list of the approvals that wait after this one.
    "rule": r"^\s*(?:(?:\S+ [A-Za-z][A-Za-z ]*? )?[─━]{8,}|Up next \(\d+\)\s*$|\d+\. Approval Required\b)",
    "hint": r"\bEnter select\b",
}

INPUT = {"native": None}

# ---------------------------------------------------------------------------------------------------------------
# Chat log: the thread from `amp threads export`, kept as JSONL beside the queue
# ---------------------------------------------------------------------------------------------------------------

CHAT = {"compressed": False, "file_tools": ["create_file", "edit_file"], "ref": "session", "cache_write": True}

REFRESH = 5  # seconds between two exports of the same thread
EXPORT_TIMEOUT = 20

# A tool's input key that says best what a call does, and Amp's tool names as the names plain_tool knows.
_PLAIN_NAMES = {"shell_command": "Bash", "create_file": "Write", "edit_file": "Edit", "read_web_page": "WebFetch",
                "web_search": "WebSearch", "Read": "Read", "Grep": "Grep", "glob": "Glob"}
# A message is finished when its turn has ended or a later message exists; else these say so.
_DONE = {"complete", "cancelled", "error", "aborted"}
_RESULT_DONE = {"done", "error", "cancelled", "rejected-by-user", "rejected"}


def _s():
    """river.sessions, imported when first used: core imports the adapters while it loads, before sessions can."""
    from .. import sessions
    return sessions


def threads_dir():
    """The folder of the thread files: amp/threads beside the queue."""
    from ..core import river_dir
    return Path(river_dir()) / "amp" / "threads"


def _path_of(uri):
    """A folder from a file:// URI, as Amp records the thread's working folder."""
    from urllib.parse import unquote, urlparse
    u = urlparse(str(uri or ""))
    return unquote(u.path) if u.scheme == "file" else (str(uri) if uri else None)


def _finished(m, later, ended):
    """Whether a message of an export will not change any more: a later message exists, the turn ended, or the
    message says it is done."""
    if later or ended:
        return True
    if m.get("role") == "assistant":
        return (m.get("state") or {}).get("type") in _DONE
    return all(((b.get("run") or {}).get("status") in _RESULT_DONE)
               for b in m.get("content") or () if isinstance(b, dict) and b.get("type") == "tool_result")


def _slim(m):
    """A message as the file keeps it: without the signatures of its thinking blocks (large, of no use here)."""
    m = dict(m)
    m["content"] = [{k: v for k, v in b.items() if k != "signature"} if isinstance(b, dict) else b
                    for b in m.get("content") or ()]
    return m


def export_lines(export, seen=None):
    """(lines, seen): the new lines of the thread file for an export (the JSON of `amp threads export`), and the
    record of what the file holds now. seen: the record of the last call (None: a new file). The file gets a thread
    line first; then each finished message once, in order (an unfinished one waits, and the ones after it with it);
    and a meta line (title, mode, state) whenever one of them changed."""
    seen = dict(seen or {})
    out = []
    if not seen.get("thread"):
        env = (export.get("env") or {}).get("initial") or {}
        out.append({"type": "thread", "id": export.get("id"), "cwd": _path_of(env.get("workingDirectory")),
                    "created": export.get("created")})
        seen["thread"] = export.get("id")
    meta = export.get("meta") or {}
    agent_state = (meta.get("lastKnownAgentState") or {}).get("state")
    ended = agent_state in ("idle", "error")
    msgs = sorted((m for m in export.get("messages") or () if isinstance(m, dict) and isinstance(m.get("messageId"), int)),
                  key=lambda m: m["messageId"])
    last = int(seen.get("last") or 0)
    for n, m in enumerate(msgs):
        if m["messageId"] <= last:
            continue
        if not _finished(m, n + 1 < len(msgs), ended):
            break
        out.append({"type": "message", **_slim(m)})
        last = m["messageId"]
    seen["last"] = last
    now = {"title": export.get("title"), "agentMode": export.get("agentMode") or meta.get("agentMode"),
           "state": agent_state}
    if now != seen.get("meta"):
        out.append({"type": "meta", "at": export.get("updatedAt") or (meta.get("lastKnownAgentState") or {}).get("updatedAt"),
                    **now})
        seen["meta"] = now
    return out, seen


def _export(ref):
    """The export of thread ref (a dict), or None when amp cannot give it now."""
    try:
        done = subprocess.run(["amp", "threads", "export", ref], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=EXPORT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    try:
        d = json.loads(done.stdout)
    except ValueError:
        return None
    return d if isinstance(d, dict) and d.get("id") == ref else None


def refresh(ref, path, export=None):
    """Bring the thread file at path up to date, at most every REFRESH seconds: export the thread (or take export, a
    dict the tests give) and append its new lines. The record of what the file holds is <path>.seen; a lock on it
    keeps two readers from appending the same lines. Returns the number of lines it appended."""
    path = Path(path)
    mark = path.with_name(path.name + ".seen")
    try:
        if export is None and mark.exists() and time.time() - mark.stat().st_mtime < REFRESH:
            return 0
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(mark, "a+") as lock:
            try:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass
            lock.seek(0)
            try:
                seen = json.loads(lock.read() or "{}")
            except ValueError:
                seen = {}
            if not path.exists():
                seen = {}  # the file went: write it again from the start
            got = export if export is not None else _export(ref)
            lines = []
            if got is not None:
                lines, seen = export_lines(got, seen)
                if lines:
                    with open(path, "a", encoding="utf-8") as f:
                        f.write("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines))
            lock.seek(0)
            lock.truncate()
            lock.write(json.dumps(seen))  # also after a failed export: the next try waits REFRESH seconds
        return len(lines)
    except OSError:
        return 0


def chat_path(session_ref, folder=None, root=None):
    """The thread file of a thread id, brought up to date from Amp (under root in place of the folder beside the
    queue, when given: then nothing is exported), or None when there is none."""
    ref = str(session_ref or "").strip()
    if not _THREAD.match(ref):
        return None
    f = Path(root or threads_dir()) / f"{ref}.jsonl"
    if root is None:
        refresh(ref, f)
    return f if f.is_file() else None


def _rows(lines):
    for line in lines:
        try:
            d = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(d, dict):
            yield d


_SUGGESTED = re.compile(r"\s*<suggested-replies>.*?</suggested-replies>\s*", re.S)


def _result(run):
    """The text of a tool result's run, and whether it failed."""
    run = run if isinstance(run, dict) else {}
    res = run.get("result")
    if isinstance(res, dict) and ("output" in res or "exitCode" in res):
        text = str(res.get("output") or "")
        failed = bool(res.get("exitCode"))
    else:
        text, failed = _s()._result_text(res), False
    if not text and run.get("error"):
        text = str(run["error"].get("message") if isinstance(run["error"], dict) else run["error"])
    return text, failed or run.get("status") not in ("done", None)


def parse_chat_line(d):
    """The page lines of one line of the thread file, and the turn state it shows. A user message: the person's
    prompt (working), or the results of tool calls; an assistant message: its text (without Amp's suggested replies)
    and a tool line for each call, working when it asks for a tool, else idle; a meta line: the thread's state
    (running: working; idle or error: idle), and an event when the last turn stopped with an error (Out of Credits)."""
    s = _s()
    t = d.get("type")
    if t == "meta":
        st = d.get("state")
        ev = [{"at": d.get("at"), "role": "event", "text": "The last turn stopped with an error"}] if st == "error" else []
        return ev, ("working" if st in ("running", "awaiting-approval") else "idle" if st in ("idle", "error") else None)
    if t != "message":
        return [], None
    at, role, out = d.get("createdAt"), d.get("role"), []
    blocks = [b for b in d.get("content") or () if isinstance(b, dict)]
    if role == "user":
        text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        if text:
            out.append(s._user_turn(at, text))
        for b in blocks:
            if b.get("type") == "tool_result":
                txt, failed = _result(b.get("run"))
                out.append({"at": at, "role": "result", "for": b.get("toolUseID"), "text": s._clip(txt, s.DETAIL_MAX),
                            "error": failed})
        return out, ("working" if text else None)
    if role == "assistant":
        calls = 0
        for b in blocks:
            if b.get("type") == "text" and _SUGGESTED.sub("", b.get("text") or "").strip():
                out.append({"at": at, "role": "assistant", "text": s._clip(_SUGGESTED.sub("", b["text"]), s.TEXT_MAX)})
            elif b.get("type") == "tool_use":
                calls += 1
                name, args = str(b.get("name") or "tool"), b.get("input") if isinstance(b.get("input"), dict) else {}
                out.append({"at": at, "role": "tool", "text": s._tool_line(name, args), "id": b.get("id"),
                            "name": name, "plain": s.plain_tool(_PLAIN_NAMES.get(name, name), args),
                            "detail": s._detail(args)})
        stop = (d.get("state") or {}).get("stopReason")
        if (d.get("state") or {}).get("type") == "cancelled" or stop == "cancelled":
            out.append({"at": at, "role": "event", "text": "interrupted"})
        return out, ("working" if calls or stop == "tool_use" else "idle")
    if role == "info":
        text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        return ([{"at": at, "role": "event", "text": s._clip(text, s.TEXT_MAX)}] if text else []), None
    return [], None


def parse_chat(lines):
    return _s().parse_lines(parse_chat_line, lines)


def chat_cwd(lines):
    return next((d.get("cwd") for d in _rows(lines) if d.get("type") == "thread"), None)


def chat_title(lines):
    """The thread's title: Amp names a thread after its first turn."""
    title = None
    for d in _rows(lines):
        if d.get("type") == "meta" and d.get("title"):
            title = d["title"]
    return title


def chat_usage(lines):
    """Token counts of the assistant messages. Amp's inputTokens are the part not read from or written to the cache,
    as for Claude Code."""
    out = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    for d in _rows(lines):
        u = d.get("usage") if d.get("type") == "message" and d.get("role") == "assistant" else None
        if isinstance(u, dict):
            out["input"] += int(u.get("inputTokens") or 0)
            out["output"] += int(u.get("outputTokens") or 0)
            out["cache_read"] += int(u.get("cacheReadInputTokens") or 0)
            out["cache_write"] += int(u.get("cacheCreationInputTokens") or 0)
    return out


def chat_settings(lines):
    """The mode the thread runs in now ({"effort": mode}) and the model of its last reply ({"model": id})."""
    got = {}
    for d in _rows(lines):
        if d.get("type") == "meta" and d.get("agentMode"):
            got["effort"] = d["agentMode"]
        elif d.get("type") == "message" and isinstance(d.get("usage"), dict) and d["usage"].get("model"):
            got["model"] = d["usage"]["model"]
    return got


# ---------------------------------------------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------------------------------------------

# No COMMANDS: Amp has no slash commands (see the top of this module), so the message box offers none.

SETUP = {
    # Amp reads AGENTS.md (and AGENT.md, CLAUDE.md when there is none) of the folder and the folders above it.
    "instructions": "AGENTS.md",
    "skills_dir": "~/.config/agents/skills",  # amp skill add --global; it also reads .agents/skills and .claude/skills
    "voice": None,
    "open_link": None,
}


def settings_file():
    """Amp's settings file: $AMP_SETTINGS_FILE, else amp/settings.json in $XDG_CONFIG_HOME or ~/.config."""
    got = os.environ.get("AMP_SETTINGS_FILE")
    if got:
        return Path(got).expanduser()
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "amp" / "settings.json"


def write_mcp_config(url):
    """Add MaximizePM's MCP server (an http server at url) to amp.mcpServers of Amp's settings file as 'maxpm', as
    `amp mcp add maxpm <url>` writes it; other keys stay, and the old file is kept as settings.json.bak. Returns the
    files it changed. A settings file with comments (JSONC) is not rewritten: the error names the amp command."""
    from ..core import RiverError
    f = settings_file()
    old = f.read_text() if f.exists() else ""
    try:
        data = json.loads(old) if old.strip() else {}
    except ValueError:
        raise RiverError(f"{f} is not plain JSON (Amp allows comments), so MaximizePM does not rewrite it; "
                         f"run: amp mcp add maxpm {url}")
    servers = data.get("amp.mcpServers", {}) if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        raise RiverError(f"{f}: expected an object with an amp.mcpServers object; fix it first")
    entry = {"url": url}
    if servers.get("maxpm") == entry:
        return []
    data.setdefault("amp.mcpServers", {})["maxpm"] = entry
    f.parent.mkdir(parents=True, exist_ok=True)
    if old:
        f.with_name(f.name + ".bak").write_text(old)
    f.write_text(json.dumps(data, indent=2) + "\n")
    return [str(f)]
