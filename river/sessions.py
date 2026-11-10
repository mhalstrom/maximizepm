"""The Sessions tab of maxpm serve: every running agent session, with a read-only view of its transcript (#4592).

The page cannot show claude.ai or chatgpt.com in a frame (both refuse it), so maxpm serve reads the transcript file
each agent CLI writes on this computer and sends its lines to the page: the person's and the agent's text, and
each tool call as one line. Claude Code writes <config dir>/projects/<folder>/<session id>.jsonl (the session id
comes from agent_sessions); Codex writes ~/.codex/sessions/<y>/<m>/<d>/rollout-<time>-<thread id>.jsonl, which it
compresses to .jsonl.zst (the thread id is the agent's native_address). Nothing here writes to a session: a
person types through its Open link (session_url), its terminal, or the text bar of the large view
(server.session_send, for a session in a tmux pane).
"""

import json
import os
import re
import time
from pathlib import Path

from . import core

TAIL_BYTES = 2 * 1024 * 1024  # the first read of the large view: the last part of the transcript only
CARD_BYTES = 256 * 1024      # the read of a card: its last few lines
LIMIT = 300               # at most this many lines in one answer
TEXT_MAX = 4000           # a longer text is cut
TOOL_MAX = 160            # the one line of a tool call
DETAIL_MAX = 4000         # a tool call's input, and its result, when the page opens it
PREVIEW = 4               # the lines a card shows
ADOPT_QUIET = 120         # tmux_adopt: a session moves into tmux after its turn ended and it wrote nothing this long
PLATFORMS = {"claude": "claude-code", "codex": "codex"}
# The chat providers the Sessions tab knows (#4627): the label each session shows. A new provider is one more row
# here (#4625 adds its settings); the page draws what it gets.
# Each provider's settings (#4625): how the Settings dialog reads each one (from the session's transcript, else
# its command line), whether it changes in place (a command typed into its pane while it is idle), and how a
# restart applies it (model, effort, a profile option, or a flag). A new provider is one more table here.
_CC = core.LAUNCH_PLATFORMS["claude-code"]["options"]
_CX = core.LAUNCH_PLATFORMS["codex"]["options"]
# Voice (#4626): what each CLI's own voice mode does, and the way the page takes speech for its sessions. Both
# CLIs record the microphone of the computer they run on and take no audio from outside (checked in Claude Code
# 2.1.319 and Codex 0.159.1), so the page dictates: the browser turns speech into text in the message box, and the
# person sends it as a typed message.
PROVIDERS = {
    "claude-code": {"label": "Claude Code", "voice": {"page": "dictate", "cli": (
        "/voice [hold|tap|off] in its terminal: hold-to-talk dictation into its prompt; it records the microphone of "
        "the computer it runs on (SoX) and needs a claude.ai login; it takes no audio from outside")}, "settings": [
        {"key": "model", "label": "Model", "kind": "model", "live": "/model {value}", "restart": "model",
         "cmd": r"--model (\S+)"},
        {"key": "effort", "label": "Effort", "kind": "choice", "choices": ["low", "medium", "high", "xhigh", "max"],
         "live": "/effort {value}", "restart": "effort", "cmd": r"--effort (\S+)"},
        {"key": "permission_mode", "label": "Permission mode", "kind": "choice", "choices": _CC["permission_mode"]["choices"],
         "restart": "opt", "default": "claude_permission_mode", "cmd": r"--permission-mode (\S+)",
         "note": "Shift+Tab in its terminal also cycles it"},
        {"key": "remote_control", "label": "Remote Control", "kind": "choice", "choices": ["on", "off"],
         "restart": "opt", "default": "claude_remote_control", "flag": "--remote-control"},
    ]},
    "codex": {"label": "Codex", "voice": {"page": "dictate", "cli": (
        "/voice in its terminal: a spoken conversation (OpenAI realtime, the feature realtime_conversation) with the "
        "microphone and speakers of the computer it runs on; it takes no audio from outside")}, "settings": [
        {"key": "model", "label": "Model", "kind": "model", "restart": "model", "cmd": r"(?:^|\s)-m (\S+)"},
        {"key": "effort", "label": "Reasoning effort", "kind": "choice", "choices": core.PLATFORM_EFFORTS["openai"],
         "restart": "effort", "cmd": r"model_reasoning_effort=(\S+)"},
        {"key": "approval", "label": "Approval", "kind": "choice", "choices": _CX["approval"]["choices"] + ["untrusted"],
         "restart": "opt", "default": "codex_approval", "cmd": r"--ask-for-approval (\S+)"},
        {"key": "sandbox", "label": "Sandbox", "kind": "choice", "choices": _CX["sandbox"]["choices"],
         "restart": "opt", "default": "codex_sandbox", "cmd": r"--sandbox (\S+)"},
        {"key": "approve_for_me", "label": "Approve for me (auto)", "kind": "choice", "choices": ["on", "off"],
         "restart": "flag", "flag": "--approve-for-me"},
        {"key": "no_daemon", "label": "No shared background server", "kind": "choice", "choices": ["on", "off"],
         "restart": "flag", "flag": "--no-daemon"},
    ]},
}


def provider(agent_type, kind=None):
    """{id, label} of a session's chat provider: its agent type, else the kind of its transcript."""
    pid = agent_type or PLATFORMS.get(kind or "")
    if not pid:
        return None
    voice = PROVIDERS.get(pid, {}).get("voice") or {}
    return {"id": pid, "label": PROVIDERS.get(pid, {}).get("label") or pid, "voice": voice.get("page"),
            "voice_cli": voice.get("cli")}
STATES = {"working": "working", "prompt": "waits at a prompt", "answer": "waits for an answer", "idle": "idle",
          "expert": "talks with you",
          "stopping": "asked to stop", "starting": "not connected yet: it ran no maxpm command",
          "ended": "ended before its first maxpm command"}


def codex_root():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"


def _zstd():
    try:
        from compression import zstd  # Python 3.14 and later
        return zstd
    except ImportError:
        return None


def transcript(conn, agent, root=None, croot=None):
    """(kind, path) of the agent's newest transcript on this computer: kind claude or codex; or (None, None)."""
    a = conn.execute("SELECT agent_type, platform, native_address FROM agents WHERE name=?", (agent,)).fetchone()
    if a is None:
        return None, None
    found = []
    for r in conn.execute("SELECT session_id FROM agent_sessions WHERE agent=? ORDER BY first_seen DESC, rowid DESC "
                          "LIMIT 1", (agent,)):
        found += [("claude", p) for p in Path(root or core.transcripts_root()).glob(f"*/{r['session_id']}.jsonl")]
    tid = a["native_address"] if (a["platform"] or "").lower() == "codex" else None
    if tid and all(c.isalnum() or c == "-" for c in tid):
        base = Path(croot or codex_root())
        found += [("codex", p) for p in base.glob(f"*/*/*/rollout-*-{tid}.jsonl")]
        if _zstd():
            found += [("codex", p) for p in base.glob(f"*/*/*/rollout-*-{tid}.jsonl.zst")]
    best = None
    for kind, p in found:
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if best is None or m > best[0]:
            best = (m, kind, p)
    return (best[1], best[2]) if best else (None, None)


def _read(path, cursor, tail=TAIL_BYTES):
    """(text, start, end): the complete lines of the file from byte `cursor` on (None: its last `tail` bytes), the
    byte where that text starts, and the byte after its last complete line, the next cursor."""
    if str(path).endswith(".zst"):
        data = _zstd().ZstdDecompressor().decompress(Path(path).read_bytes())  # a session that writes: what is there
        size = len(data)
        start = max(0, size - tail) if cursor is None else min(max(0, int(cursor)), size)
        chunk = data[start:]
    else:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            start = max(0, size - tail) if cursor is None else min(max(0, int(cursor)), size)
            f.seek(start)
            chunk = f.read(size - start)
    if cursor is None and start > 0:  # the tail starts inside a line: from the next one
        cut = chunk.find(b"\n") + 1
        chunk, start = chunk[cut:], start + cut
    end = chunk.rfind(b"\n") + 1  # a line the CLI still writes waits for the next read
    return chunk[:end].decode("utf-8", "replace"), start, start + end


def _clip(s, n):
    s = str(s or "").strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def _tool_line(name, inp):
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except ValueError:
            return f"{name}: {_clip(' '.join(inp.split()), TOOL_MAX)}"
    inp = inp if isinstance(inp, dict) else {}
    what = next((inp[k] for k in ("description", "command", "cmd", "file_path", "path", "pattern", "query", "url",
                                  "prompt", "skill") if inp.get(k)), "")
    if isinstance(what, list):
        what = " ".join(map(str, what))
    return f"{name}: {_clip(' '.join(str(what).split()), TOOL_MAX)}" if what else str(name)


def _detail(inp):
    """A tool call's input as the page shows it opened: the command itself, else the input as JSON."""
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except ValueError:
            return _clip(inp, DETAIL_MAX)
    if isinstance(inp, dict) and isinstance(inp.get("command"), (str, list)):
        c = inp["command"]
        return _clip(" ".join(map(str, c)) if isinstance(c, list) else c, DETAIL_MAX)
    return _clip(json.dumps(inp, indent=2, ensure_ascii=False), DETAIL_MAX)


def _result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(x.get("text", "") for x in content if isinstance(x, dict) and x.get("type") in
                         ("text", "input_text", "output_text"))
    if isinstance(content, dict):
        return str(content.get("output") or content.get("content") or "")
    return ""


def _parts_text(content, kinds):
    if isinstance(content, str):
        return content
    return "\n".join(x.get("text", "") for x in content or [] if isinstance(x, dict) and x.get("type") in kinds)


def _claude(d):
    """The page lines of one Claude Code transcript line, and its turn state (working, idle, or None)."""
    if d.get("type") not in ("user", "assistant") or d.get("isSidechain") or d.get("isMeta"):
        return [], None
    m = d.get("message") if isinstance(d.get("message"), dict) else {}
    at, content, out = d.get("timestamp"), m.get("content"), []
    if d["type"] == "assistant":
        for x in content if isinstance(content, list) else []:
            if not isinstance(x, dict):
                continue
            if x.get("type") == "text" and x.get("text", "").strip():
                out.append({"at": at, "role": "assistant", "text": _clip(x["text"], TEXT_MAX)})
            elif x.get("type") == "tool_use":
                out.append({"at": at, "role": "tool", "text": _tool_line(x.get("name"), x.get("input")),
                            "id": x.get("id"), "detail": _detail(x.get("input"))})
        stop = m.get("stop_reason")
        return out, ("idle" if stop in ("end_turn", "stop_sequence") else "working" if stop else None)
    for x in content if isinstance(content, list) else []:
        if isinstance(x, dict) and x.get("type") == "tool_result" and x.get("tool_use_id"):
            out.append({"at": at, "role": "result", "for": x["tool_use_id"], "error": bool(x.get("is_error")),
                        "text": _clip(_result_text(x.get("content")), DETAIL_MAX)})
    text = _parts_text(content, ("text",)).strip()
    if text.startswith("<task-notification>"):
        out.append({"at": at, "role": "event", "text": "a background task ended"})
    elif (text and not text.startswith("<")) or any(isinstance(x, dict) and x.get("type") == "image"
                                                   for x in content if isinstance(content, list)):
        # tags the CLI adds (a command's name and output, reminders) are no turn of the person
        out.append(_user_turn(at, text, sum(1 for x in content if isinstance(x, dict) and x.get("type") == "image")
                              if isinstance(content, list) else 0))
    return out, "working"


def _user_turn(at, text, images=0):
    """A person's turn, including saved uploads and how many image blocks the CLI put in it."""
    import re
    root = re.escape(str(Path(core.river_dir()) / "uploads").replace("\\", "/"))
    found = re.findall(root + r"/([^/\s]+)/([0-9]{8}-[0-9]{6}-[0-9a-f]{8}(?:-[a-zA-Z0-9_.-]+|\.(?:png|jpg|gif|webp)))", text.replace("\\", "/"))
    e = {"at": at, "role": "user", "text": _clip(text, TEXT_MAX)}
    for a, f in dict.fromkeys(found):
        path = f"{a}/{f}"
        if Path(f).suffix.lower() in {".png", ".jpg", ".gif", ".webp"}:
            e.setdefault("images", []).append(path)
        else:
            e.setdefault("files", []).append({"path": path, "name": re.sub(r"^\d{8}-\d{6}-[a-f0-9]{8}-", "", f)})
    if images:
        e["image_count"] = images
    return e


_CODEX_SKIP = ("# AGENTS.md", "<environment_context>", "<user_instructions>", "<")


def _codex(d):
    """The page lines of one Codex session log line, and its turn state."""
    p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
    at, t = d.get("timestamp"), p.get("type")
    if d.get("type") == "event_msg":
        return [], {"task_started": "working", "task_complete": "idle", "turn_aborted": "idle"}.get(t)
    if d.get("type") != "response_item":
        return [], None
    if t == "message" and p.get("role") in ("user", "assistant"):
        text = _parts_text(p.get("content"), ("input_text", "output_text", "text")).strip()
        if not text or (p["role"] == "user" and text.startswith(_CODEX_SKIP)):
            return [], None
        if p["role"] == "user":
            return [_user_turn(at, text, sum(1 for x in p.get("content") or [] if isinstance(x, dict)
                                             and x.get("type") in ("input_image", "image")))], None
        return [{"at": at, "role": p["role"], "text": _clip(text, TEXT_MAX)}], None
    if t in ("function_call", "custom_tool_call", "local_shell_call"):
        inp = p.get("arguments") if t == "function_call" else p.get("input") if t == "custom_tool_call" else p.get("action")
        return [{"at": at, "role": "tool", "text": _tool_line(p.get("name") or "shell", inp), "id": p.get("call_id"),
                 "detail": _detail(inp)}], None
    if t in ("function_call_output", "custom_tool_call_output", "local_shell_call_output") and p.get("call_id"):
        return [{"at": at, "role": "result", "for": p["call_id"],
                 "text": _clip(_result_text(p.get("output")), DETAIL_MAX)}], None
    return [], None


_TAIL_CACHE = {}  # (path, tail) -> (size, mtime, the answer of entries(kind, path)): the page asks every few seconds


def entries(kind, path, cursor=None, limit=LIMIT, tail=TAIL_BYTES):
    """{entries, cursor, turn}: the page lines of the transcript after cursor (None: its last part), at most limit
    of them (the newest), the cursor of the next read, and the state of the last turn (working, idle, or None)."""
    if cursor is None:
        st = Path(path).stat()
        got = _TAIL_CACHE.get((str(path), tail))
        if not got or got[:2] != (st.st_size, st.st_mtime_ns):
            got = _TAIL_CACHE[(str(path), tail)] = (st.st_size, st.st_mtime_ns, _entries(kind, path, None, tail=tail))
        return dict(got[2], entries=got[2]["entries"][-limit:])
    return _entries(kind, path, cursor, limit)


def _entries(kind, path, cursor, limit=LIMIT, tail=TAIL_BYTES):
    text, _start, end = _read(path, cursor, tail)
    parse = _claude if kind == "claude" else _codex
    out, turn, cwd = [], None, None
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        got, st = parse(d)
        out += got
        turn = st or turn
        p = d.get("payload") if isinstance(d.get("payload"), dict) else d  # the folder the session runs in
        cwd = p.get("cwd") if isinstance(p.get("cwd"), str) else cwd
    return {"entries": out[-limit:], "cursor": end, "turn": turn, "cwd": cwd}


def state(conn, a, turn, waiting=None):
    """(key, label) of what a session does now: stopping, prompt (a permission prompt), answer (an open question to
    a person), idle (in maxpm wait, or its turn ended), or working."""
    if a.get("stop_at"):
        return "stopping", STATES["stopping"]
    if a.get("activity_prompt"):
        return "prompt", STATES["prompt"]
    if a["name"] in (waiting if waiting is not None else core.waits_on_person(conn, [a["name"]])):
        return "answer", STATES["answer"]
    if a.get("role") == "expert" and turn != "working":
        # An expert waits at its prompt for the person by design (#4955): not idle, and nothing waits on the person.
        return "expert", STATES["expert"]
    if a.get("waiting_since") and not (turn == "working" and a.get("activity_at") and a.get("wait_at")
                                       and a["activity_at"] > a["wait_at"]):
        # A turn after the last maxpm wait started: a person talks with it while its wait runs in the background.
        return "idle", "idle: waits for work"
    if turn == "idle" or (turn is None and (a.get("activity_event") == "Stop" or a.get("role") == "idle")):
        bg = core.background_work(conn, a["name"])  # its harness wakes it when this ends
        return "idle", f"idle: waits on {bg[0]['what'] or 'its background work'}" if bg else "idle: at its prompt"
    return "working", STATES["working"]


def adopt_check(conn, a, kind, path, turn, cwd, waiting=None):
    """tmux_adopt (#4598) for a session outside tmux: ("now", why) when maxpm serve moves it into a tmux pane now,
    ("later", why) when it moves after its turn, or ("never", why)."""
    if core.setting(conn, "tmux_adopt", agent=a["name"]) != "on":
        return "never", "tmux_adopt is off"
    if os.name == "nt" or not core.tmux_path():
        return "never", "tmux is not installed"
    if not path or kind not in PLATFORMS:
        return "never", "MaximizePM has no transcript of it, so it does not know which chat to resume"
    if not (a.get("pid") and a.get("host") == core.this_host() and a.get("pid_cmd")):
        return "never", "its process on this computer is not known"
    if not cwd:
        return "never", "its transcript does not name its folder"
    if a.get("stop_at"):
        return "never", "it is asked to stop"
    try:
        quiet = time.time() - Path(path).stat().st_mtime
    except OSError:
        return "never", "its transcript is gone"
    if (a.get("activity_prompt") or turn != "idle" or quiet < ADOPT_QUIET
            or a["name"] in (waiting if waiting is not None else core.waits_on_person(conn, [a["name"]]))
            or core.background_work(conn, a["name"])):
        return "later", (f"MaximizePM moves it into tmux when its turn has ended and it has been quiet for "
                         f"{ADOPT_QUIET // 60} minutes, with no prompt, question or background work open")
    return "now", "it is quiet at its prompt"


def running(conn, terminals=None):
    """The agent_status of every AI agent whose session runs: its process on this computer lives, or it has a tmux
    pane here, or it is active and recorded no process (a sandbox that cannot run ps)."""
    out, here = [], core.this_host()
    for r in conn.execute("SELECT name FROM agents WHERE kind='ai' ORDER BY name"):
        a = core.agent_status(conn, r["name"])
        if core.ended(a) or a["state"] in ("stopped", "gone") or a.get("via") == "http":
            continue
        if a.get("pid") and a.get("host") == here:
            if not core.pid_alive(a["pid"]):
                continue
        elif not ((terminals or {}).get(a["name"]) or (not a.get("pid") and a["state"] == "active")):
            continue
        out.append(a)
    return out


def _open_questions(conn, name):
    return conn.execute("SELECT m.id, m.body, m.item_id, m.to_agent, m.created_at FROM messages m JOIN agents p ON "
                        "p.name=m.to_agent AND p.kind='human' WHERE m.kind='question' AND m.state='open' AND "
                        "m.from_agent=? ORDER BY m.id", (name,)).fetchall()


def _last_talk(entries):
    """The time (datetime) of the last turn that a person typed in a session's chat, from its transcript lines: not a
    message that MaximizePM delivered ([maxpm ...]); tags and task notifications are no user turn already."""
    for e in reversed(entries or ()):
        if e.get("role") == "user" and e.get("at") and core._talk(e.get("text") or ""):
            return core._ts(e["at"])
    return None


def waiting_for(conn, a, prompt=None, pane=False, said=None):
    """What waits for a person in one session (#4607): the prompt its tmux pane shows (server.pane_prompts); with no
    pane, the permission prompt its hooks reported, which a person answers in the session itself; and each open
    maxpm question from it to a person, except one asked before the person's last prompt in its chat (said, #4995):
    the person answered that one there."""
    out = []
    if prompt:
        out.append({"kind": "prompt", **prompt})
    elif not pane and a.get("activity_prompt"):
        call = core._activity_call(a["activity_prompt"])
        out.append({"kind": "prompt", "elsewhere": True, "question": f"A permission prompt for {call}" if call
                    else "A permission prompt", "options": []})
    limit = core.parse_duration(core.setting(conn, "question_popup_max", agent=a["name"]))
    for r in _open_questions(conn, a["name"]):
        if said and (core._ts(r["created_at"]) or said) <= said:
            continue
        # An old question does not open the dialog by itself (#5001); the person still reaches it from the card.
        old = bool(limit.total_seconds()) and core.now() - core.parse_iso(r["created_at"]) > limit
        out.append({"kind": "question", "id": r["id"], "body": r["body"], "item_id": r["item_id"], "to": r["to_agent"],
                    "asked_at": r["created_at"], "old": old})
    return out


WORK_NAME_MAX = 60


def _short_title(t):
    t = " ".join(re.sub(r"^#\d+\s+", "", str(t or "")).split())
    return t if len(t) <= WORK_NAME_MAX else t[:WORK_NAME_MAX - 1].rstrip() + "…"


def custom_title(path, tail=256 * 1024):
    """The Claude Code session title (the last custom-title record of its transcript), or None."""
    if not path or str(path).endswith(".zst"):
        return None
    text, _s, _e = _read(path, None, tail)
    for line in reversed(text.splitlines()):
        if "custom-title" not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("type") == "custom-title" and isinstance(d.get("customTitle"), str):
            return d["customTitle"]
    return None


def work_name(conn, a, path=None, root=None, croot=None):
    """What a session works on, as the name the page shows for it (#4695); its agent id stays its address. In this
    order: the title of the item it holds; the goal it owns; Manager; the deploy targets it owns; the item it
    finished last ("last: ..."); its Claude Code session title; else its agent id without the four hex digits."""
    holds = a.get("holds") or []
    if holds:
        return _short_title(holds[0]["title"])
    g = conn.execute("SELECT name FROM goals WHERE owner=? AND status='open' ORDER BY rank LIMIT 1", (a["name"],)).fetchone()
    if g:
        return "Goal: " + g["name"]
    if a.get("role") == "manager":
        return "Manager"
    owns = [o.get("name") if isinstance(o, dict) else str(o) for o in a.get("owns") or []]
    if owns:
        return "Deploys " + ", ".join(owns)
    r = conn.execute("SELECT title FROM items WHERE assignee=? AND status='done' ORDER BY closed_at DESC LIMIT 1",
                     (a["name"],)).fetchone()
    if r:
        return "last: " + _short_title(r["title"])
    if path is None:
        _kind, path = transcript(conn, a["name"], root, croot)
    try:
        t = custom_title(path)
    except (OSError, ValueError):
        t = None
    if t:
        return _short_title(t)
    return re.sub(r"-[0-9a-f]{4}$", "", a["name"])


def views(conn, terminals=None, root=None, croot=None, prompts=None, ended_panes=None):
    """The cards of the Sessions tab: one per running agent session, with its item, state, Open link, its last
    lines, and what waits for a person in it (prompts: server.pane_prompts)."""
    agents = running(conn, terminals)
    waiting = core.waits_on_person(conn, [a["name"] for a in agents])
    # One session can run commands under several names (a manager that acted --as a worker): one card for it, in
    # the name the queue saw last, unless another name holds the work.
    agents.sort(key=lambda a: (bool(a["holds"] or a.get("owns")), a.get("last_seen") or ""), reverse=True)
    cards, seen = [], {}
    for a in agents:
        kind, path = transcript(conn, a["name"], root, croot)
        keys = [k for k in (path and ("t", str(path)), a.get("pid") and ("p", a.get("host"), a["pid"])) if k]
        first = next((seen[k] for k in keys if k in seen), None)
        if first is not None:
            first["also"].append(a["name"])
            continue
        tail = {"entries": [], "turn": None}
        if path:
            try:
                tail = entries(kind, path, None, LIMIT, CARD_BYTES)
                tail = dict(tail, entries=[e for e in tail["entries"] if e["role"] != "result"][-PREVIEW:],
                            said=_last_talk(tail["entries"]))
            except (OSError, ValueError, EOFError):
                path = None
        # The person's last prompt in the chat (#4995): a question asked before it was answered there, also when no
        # hook closed it (a session with no MaximizePM hooks), so the card does not show it as waiting.
        said = tail.get("said")
        if said and a["name"] in waiting and not any(
                (core._ts(q["created_at"]) or said) > said for q in _open_questions(conn, a["name"])):
            waiting = {k: v for k, v in waiting.items() if k != a["name"]}
        key, label = state(conn, a, tail["turn"], waiting)
        prompt = (prompts or {}).get(a["name"])
        if prompt and key != "stopping":
            key, label = "prompt", STATES["prompt"]
        elif not path and not a.get("pid") and key not in ("stopping", "answer"):
            # It ran no maxpm command and wrote no transcript: not working (#4621). Its pane shows a shell: it ended.
            key = "ended" if (terminals or {}).get(a["name"]) in (ended_panes or ()) else "starting"
            label = STATES[key]
        adopt = None if (terminals or {}).get(a["name"]) else adopt_check(
            conn, a, kind, path, tail["turn"], tail.get("cwd"), waiting)
        card = {"adopt": adopt and {"when": adopt[0], "why": adopt[1]}, "provider": provider(a.get("agent_type"), kind),
                "waiting": waiting_for(conn, a, prompt, bool((terminals or {}).get(a["name"])), said), "name": a["name"],
                "title": work_name(conn, a, path, root, croot), "also": [], "agent_type": a.get("agent_type"), "model": a.get("model"),
                "role": a.get("role"), "note": a.get("note"), "topic": a.get("expert_topic"),
                "talked_at": a.get("talked_at"),
                "holds": [{"id": h["id"], "title": h["title"], "status": h["status"]} for h in a["holds"]],
                "owns": a.get("owns") or [], "state": key, "state_label": label,
                "session_url": a.get("session_url"), "terminal": bool((terminals or {}).get(a["name"])),
                "transcript": kind if path else None, "last": tail["entries"],
                "at": tail["entries"][-1]["at"] if tail["entries"] else a.get("last_seen")}
        cards.append(card)
        seen.update((k, card) for k in keys)
    order = {"prompt": 0, "answer": 1, "working": 2, "expert": 2, "starting": 3, "stopping": 4, "idle": 5, "ended": 6}
    return sorted(cards, key=lambda c: (order.get(c["state"], 5), c["name"]))


_REF = None


def item_refs(conn, entries):
    """{id: {title, status, assignee}} of the queue items that the lines name as #<number> (#4609): the page links
    only these, and its pop-up shows each one's state and the session that holds it (#4611)."""
    import re
    global _REF
    _REF = _REF or re.compile(r"(?<![\w&])#(\d{1,7})\b")
    ids = sorted({int(m) for e in entries for m in _REF.findall(e.get("text", "") + " " + e.get("detail", ""))})
    out = {}
    for n in range(0, len(ids), 500):
        part = ids[n:n + 500]
        out.update({str(r["id"]): {"title": r["title"], "status": r["status"], "assignee": r["assignee"]}
                    for r in conn.execute(f"SELECT id, title, status, assignee FROM items "
                                          f"WHERE id IN ({','.join('?' * len(part))})", part)})
    return out


def view(conn, agent, cursor=None, root=None, croot=None):
    """The large view of one session: its lines after cursor (None: the last LIMIT of them), the next cursor, and
    its state; with a note and no lines for an agent that has no transcript on this computer."""
    a = core.agent_status(conn, agent)
    kind, path = transcript(conn, agent, root, croot)
    if not path:  # a session in a pane still gets the text bar
        key, label = state(conn, a, None)
        return {"agent": agent, "transcript": None, "entries": [], "cursor": None, "turn": None, "state": key,
                "state_label": label, "session_url": a.get("session_url"),
                "note": f"No transcript of {agent} on this computer: MaximizePM reads Claude Code transcripts and "
                        f"Codex session logs" + ("." if _zstd() else " (Codex logs need Python 3.14).")}
    got = entries(kind, path, cursor)
    turn = got["turn"] if got["turn"] or cursor is None else entries(kind, path)["turn"]
    key, label = state(conn, a, turn)
    return {"agent": agent, "transcript": kind, "provider": provider(a.get("agent_type"), kind),
            "entries": got["entries"], "cursor": got["cursor"],
            "refs": item_refs(conn, got["entries"]),
            "turn": turn, "state": key, "state_label": label, "session_url": a.get("session_url")}


def last_cwd(path, tail=1024 * 1024):
    """The folder the session ran in at its last transcript line that names one (Claude Code: cwd; Codex: the
    cwd of its turn context), or None. For relative paths in its text (#4689)."""
    text, _s, _e = _read(path, None, tail)
    for line in reversed(text.splitlines()):
        if '"cwd"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        p = d.get("payload") if isinstance(d, dict) and isinstance(d.get("payload"), dict) else d
        if isinstance(p, dict) and isinstance(p.get("cwd"), str):
            return p["cwd"]
    return None


# The Artifacts tab (#4620): what the sessions made or named that the side panel can show. Files a session wrote
# (Write, Edit), full file paths, local pages (localhost, 127.0.0.1) and claude.ai artifact links in its own text,
# the person's turns, and its tool calls (a screenshot's file name). Results of tools are left out: a listing of
# files names many that nobody made.
# Each transcript is read whole the first time, line by line, and later only from the end of the last read: a
# session lists every file it named, in a transcript of any size. A shorter tail missed them: pictures that a
# session reads fill its transcript fast (about 1 MB each in a tool result, #4681). 18 MB takes about 30 ms.
ARTIFACT_DAYS = 1  # agents seen this recently, besides the running ones
_ART_CACHE = {}  # path -> (size, mtime, end: the byte after the last complete line read, hits)
# Lines that never give a hit, skipped before they are decoded: tool results (left out, see above) and Claude
# Code's copies of the API requests. JSON escapes a quote in text, so these words in a message do not match.
_FILE_RX = None
_URL_RX = None
_ART_SKIP = (b'"type":"tool_result"', b'"type":"function_call_output"', b'"type":"api-request')


def _hits_in(text, at, how):
    import re
    global _FILE_RX, _URL_RX
    _FILE_RX = _FILE_RX or re.compile(r"(?<![\w.:/\\-])((?:[a-zA-Z]:[/\\]|\\\\|~/|/)(?:[\w.@~+-]+[/\\])*[\w@~+-][\w.@~+-]*\.(?:png|jpe?g|gif|webp|svg|pdf|html?|md))\b")
    _URL_RX = _URL_RX or re.compile(r"https?://(?:(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?(?:/[^\s)\]>'\"`\\]*)?"
                                    r"|claude\.ai/(?:code/)?artifact/[\w-]+)")
    out = [{"target": m, "kind": "file", "how": how, "at": at} for m in _FILE_RX.findall(text or "")]
    for u in _URL_RX.findall(text or ""):
        u = u.rstrip(".,;:")
        out.append({"target": u, "kind": "artifact" if "claude.ai/" in u else "page", "how": how, "at": at})
    return out


def _artifact_hits(path):
    """The hits of one transcript: all of it at first, then the lines it added since the last read. A file that
    became shorter (written again) is read again from its start."""
    st = Path(path).stat()
    got = _ART_CACHE.get(str(path))
    if got and got[:2] == (st.st_size, st.st_mtime_ns):
        return got[3]
    start, hits = (got[2], got[3]) if got and st.st_size >= got[2] else (0, [])
    new, end = _scan_from(path, start)
    hits = hits + new
    _ART_CACHE[str(path)] = (st.st_size, st.st_mtime_ns, end, hits)
    return hits


def _scan_from(path, start):
    """(hits, end): the hits in the complete lines from byte start on, and the byte after the last of them. A line
    the CLI still writes waits for the next read."""
    if str(path).endswith(".zst"):  # compressed: read as a whole (Codex logs, small)
        text, _s, end = _read(path, start)
        return [h for line in text.splitlines() for h in _line_hits(line.encode())], end
    hits, end = [], start
    with open(path, "rb") as f:
        f.seek(start)
        for raw in f:
            if not raw.endswith(b"\n"):
                break
            end += len(raw)
            hits += _line_hits(raw)
    return hits, end


def _line_hits(raw):
    """The hits of one transcript line (bytes)."""
    if any(k in raw for k in _ART_SKIP):
        return []
    try:
        d = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(d, dict):
        return []
    hits = []
    at = d.get("timestamp")
    m = d.get("message") if isinstance(d.get("message"), dict) else None
    p = d.get("payload") if isinstance(d.get("payload"), dict) else None
    if m and d.get("type") in ("user", "assistant") and not d.get("isSidechain"):
        content = m.get("content")
        if isinstance(content, str):
            hits += _hits_in(content, at, "named")
        for x in content if isinstance(content, list) else []:
            if not isinstance(x, dict):
                continue
            if x.get("type") == "text":
                hits += _hits_in(x.get("text", ""), at, "named")
            elif x.get("type") == "tool_use":
                inp = x.get("input") if isinstance(x.get("input"), dict) else {}
                if x.get("name") in ("Write", "Edit", "MultiEdit", "NotebookEdit") and inp.get("file_path"):
                    hits += [dict(h, how="wrote") for h in _hits_in(" " + str(inp["file_path"]), at, "wrote")]
                else:
                    hits += _hits_in(json.dumps(inp, ensure_ascii=False), at, "named")
    elif p and d.get("type") == "response_item":
        if p.get("type") == "message":
            hits += _hits_in(_parts_text(p.get("content"), ("input_text", "output_text", "text")), at, "named")
        elif p.get("type") in ("function_call", "custom_tool_call"):
            hits += _hits_in(str(p.get("arguments") or p.get("input") or ""), at, "named")
    return hits


def _stamp(at):
    """A transcript time (2026-11-08T12:34:56.789Z) in the form of the holds table (2026-11-08T12:34:56Z)."""
    return (at or "")[:19] + "Z"


def artifacts(conn, terminals=None, allowed=None, root=None, croot=None, limit=300, agent=None, item=None, since=None):
    """[{target, kind (file, page, artifact), how (wrote, named), session, item, at}], newest first, one per target:
    from the running sessions and the agents seen in the last ARTIFACT_DAYS. allowed(path): whether the side panel
    may show that file (server.local_file); a file it may not show is left out. agent: that one session only, also
    when it ended long ago (its session view, #4630). item: what the sessions that held that item made or named
    while they held it, also long ago (maxpm artifacts --item, #4838). since: an ISO time; only hits from then on,
    and without agent or item, the agents seen since then in place of ARTIFACT_DAYS."""
    windows = {}  # agent -> [(start, end)] of its holds of item
    if item is not None:
        for r in conn.execute("SELECT agent, started_at, ended_at FROM holds WHERE item_id=? ORDER BY started_at", (item,)):
            if not agent or r["agent"] == agent:
                windows.setdefault(r["agent"], []).append((r["started_at"], r["ended_at"] or "9999"))
        names = set(windows)
    elif agent:
        names = {agent}
    else:
        names = {a["name"] for a in running(conn, terminals)}
        seen = since or core.iso(core.now() - core.timedelta(days=ARTIFACT_DAYS))
        names |= {r["name"] for r in conn.execute("SELECT name FROM agents WHERE kind='ai' AND last_seen>=?", (seen,))}
    best = {}
    for name in sorted(names):
        kind, path = transcript(conn, name, root, croot)
        if not path:
            continue
        try:
            hits = _artifact_hits(path)
        except (OSError, ValueError, EOFError):
            continue
        if since:
            hits = [h for h in hits if _stamp(h["at"]) >= since]
        if item is not None:
            hits = [h for h in hits if any(s <= _stamp(h["at"]) <= e for s, e in windows[name])]
        for h in hits:
            old = best.get(h["target"])
            if old is None or (h["at"] or "") > (old["at"] or "") or (h["how"] == "wrote" and old["how"] != "wrote"):
                best[h["target"]] = dict(h, session=name)
    out = []
    for h in sorted(best.values(), key=lambda h: h["at"] or "", reverse=True):
        if h["kind"] == "file" and allowed is not None and not allowed(h["target"]):
            continue
        if item is not None:
            out.append(dict(h, item=item))
        else:
            r = conn.execute("SELECT item_id FROM holds WHERE agent=? AND started_at<=? AND (ended_at IS NULL OR ended_at>=?) "
                             "ORDER BY started_at DESC LIMIT 1", (h["session"], _stamp(h["at"]), _stamp(h["at"]))).fetchone()
            out.append(dict(h, item=r["item_id"] if r else None))
        if len(out) >= limit:
            break
    return out


def item_session(conn, item_id, root=None, croot=None):
    """The pop-up of an item anywhere on the page (#4622, #4628): its title and state, and the session to open, with
    why: (1) the one that holds it; (2) the owner of its sub-goal, then of the parent goal; (3) for a goal with no
    owner (shared), a running session that holds another item of that sub-goal or goal, newest first; (4) the last
    one that held it. With none, "goal" names its goal. Each session says whether it runs (active or away) and
    whether its transcript is on this computer (an ended session opens read only)."""
    it = conn.execute("SELECT id, title, status, assignee FROM items WHERE id=?", (item_id,)).fetchone()
    if it is None:
        raise core.RiverError(f"no item #{item_id}")
    holder = it["assignee"] if it["status"] in ("in_progress", "held") else None
    goals = [dict(r) for r in conn.execute(
        "SELECT g.id, g.name, g.owner, g.parent_id FROM goals g JOIN item_goals ig ON ig.goal_id=g.id "
        "WHERE ig.item_id=? ORDER BY g.parent_id IS NULL, g.id", (item_id,))]
    chain = []  # each tagged goal (sub-goals first), then its parent
    for g in goals:
        chain.append(g)
        if g["parent_id"] and not any(x["id"] == g["parent_id"] for x in chain + goals):
            p = conn.execute("SELECT id, name, owner, parent_id FROM goals WHERE id=?", (g["parent_id"],)).fetchone()
            if p:
                chain.append(dict(p))

    def state(name):
        a = conn.execute("SELECT * FROM agents WHERE name=? AND kind='ai'", (name,)).fetchone()
        return (core._agent_state(conn, a) if a else "gone"), a

    pick, why = None, None
    if holder:
        pick, why = holder, "holds it"
    if not pick:
        for g in chain:
            if g["owner"]:
                pick, why = g["owner"], f"owns goal {g['name']}"
                break
    if not pick:
        for g in chain:
            for r in conn.execute("SELECT i.assignee FROM items i JOIN item_goals ig ON ig.item_id=i.id WHERE ig.goal_id=? "
                                  "AND i.id<>? AND i.status IN ('in_progress','held') AND i.assignee IS NOT NULL "
                                  "ORDER BY i.claimed_at DESC", (g["id"], item_id)):
                if state(r["assignee"])[0] in ("active", "away"):
                    pick, why = r["assignee"], f"works on goal {g['name']}"
                    break
            if pick:
                break
    if not pick:
        r = conn.execute("SELECT agent FROM holds WHERE item_id=? ORDER BY started_at DESC, id DESC LIMIT 1", (item_id,)).fetchone()
        if r:
            pick, why = r["agent"], "finished it" if it["status"] == "done" else "held it last"
    out = {"id": it["id"], "title": it["title"], "status": it["status"], "holder": holder, "session": None,
           "goal": chain[0]["name"] if chain else None}
    if pick:
        st, a = state(pick)
        out["session"] = {"name": pick, "why": why, "running": st in ("active", "away"), "state": st,
                          "viewable": bool(a) and transcript(conn, pick, root, croot)[1] is not None}
    return out


# The Items view of a session (#4623): what it did with items, from the history by its name; also for an ended one.
ITEM_ACTIONS = (("claimed", "claimed"), ("done", "finished"), ("released", "released"), ("given to", "gave"),
                ("pushed to", "pushed"), ("dropped", "dropped"), ("taken over", "took over"), ("reopened", "reopened"))


def session_items(conn, agent, limit=200):
    """[{id, title, status, did (its actions, oldest first), at (the last one)}], the newest first: each item the
    agent claimed, finished, released, gave, pushed, dropped, took over or reopened."""
    rows = {}
    for r in conn.execute("SELECT e.id AS eid, e.item_id, e.at, e.change, i.title, i.status FROM events e JOIN items i ON i.id=e.item_id "
                          "WHERE e.actor=? ORDER BY e.id", (agent,)):
        did = next((label for word, label in ITEM_ACTIONS if r["change"].startswith(word)), None)
        if not did:
            continue
        x = rows.setdefault(r["item_id"], {"id": r["item_id"], "title": r["title"], "status": r["status"], "did": [], "at": r["at"]})
        if not x["did"] or x["did"][-1] != did:
            x["did"].append(did)
        x["at"], x["_n"] = r["at"], r["eid"]
    out = sorted(rows.values(), key=lambda x: x["_n"], reverse=True)[:limit]
    for x in out:
        del x["_n"]
    return out


PROMPTS_KEEP = 100  # the prompts kept for each session (#4855)


def record_prompt(conn, agent, text, who=None):
    """Keep a prompt that a person sent to agent from the message box (#4855), for the box's Up arrow. The same
    text as the last one is kept once; only the newest PROMPTS_KEEP of a session stay."""
    text = (text or "").strip()
    if not agent or not text:
        return
    with core.tx(conn):
        last = conn.execute("SELECT body FROM sent_prompts WHERE agent=? ORDER BY id DESC LIMIT 1", (agent,)).fetchone()
        if last and last["body"] == text:
            conn.execute("UPDATE sent_prompts SET sent_at=?, author=? WHERE id=(SELECT MAX(id) FROM sent_prompts WHERE agent=?)",
                         (core.iso(core.now()), who, agent))
            return
        conn.execute("INSERT INTO sent_prompts (agent, author, body, sent_at) VALUES (?,?,?,?)",
                     (agent, who, text, core.iso(core.now())))
        conn.execute("DELETE FROM sent_prompts WHERE agent=? AND id NOT IN "
                     "(SELECT id FROM sent_prompts WHERE agent=? ORDER BY id DESC LIMIT ?)", (agent, agent, PROMPTS_KEEP))


def sent_prompts(conn, agent):
    """The prompts sent to agent from the message box, newest first (#4855): the texts only."""
    return [r["body"] for r in conn.execute("SELECT body FROM sent_prompts WHERE agent=? ORDER BY id DESC LIMIT ?",
                                            (agent, PROMPTS_KEEP))]


def _transcript_settings(kind, path):
    """The settings a session's transcript shows last: Claude Code puts the model and effort on each reply and a
    line for each permission mode; Codex a turn_context at each turn."""
    import re as _re
    text, _s, _e = _read(path, None, CARD_BYTES * 4)
    got = {}
    for line in text.splitlines():
        if kind == "claude":
            if '"permission-mode"' in line:
                m = _re.search(r'"permissionMode":\s*"([^"]+)"', line)
                if m:
                    got["permission_mode"] = m.group(1)
            elif line.startswith('{"') and '"assistant"' in line:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") != "assistant":
                    continue
                msg = d.get("message") if isinstance(d.get("message"), dict) else {}
                if msg.get("model"):
                    got["model"] = msg["model"]
                if isinstance(d.get("effort"), str):
                    got["effort"] = d["effort"]
        elif '"turn_context"' in line:
            try:
                p = json.loads(line).get("payload") or {}
            except ValueError:
                continue
            for k, v in (("model", p.get("model")), ("effort", p.get("effort")), ("approval", p.get("approval_policy")),
                         ("sandbox", (p.get("sandbox_policy") or {}).get("type") if isinstance(p.get("sandbox_policy"), dict) else None)):
                if v:
                    got[k] = v
    return got


def session_settings(conn, agent, root=None, croot=None):
    """The Settings dialog of a session (#4625): its provider's settings with the value it runs with now (from its
    transcript, else its command line), where each comes from, whether it changes in place or needs a restart,
    and the default setting (maxpm config) a restart can also change."""
    import re as _re
    a = core.agent_status(conn, agent)
    kind, path = transcript(conn, agent, root, croot)
    prov = provider(a.get("agent_type"), kind)
    if not prov or prov["id"] not in PROVIDERS or not PROVIDERS[prov["id"]].get("settings"):
        raise core.RiverError(f"MaximizePM knows no settings of {agent}'s chat provider")
    seen = {}
    if path:
        try:
            seen = _transcript_settings(kind, path)
        except (OSError, ValueError, EOFError):
            seen = {}
    cmd = a.get("pid_cmd") or ""
    models = core.platform_models(conn, prov["id"]) if prov["id"] in core.LAUNCH_PLATFORMS else []
    out = []
    for spec in PROVIDERS[prov["id"]]["settings"]:
        value, source = seen.get(spec["key"]), "session"
        if value is None and spec.get("cmd"):
            m = _re.search(spec["cmd"], cmd)
            value, source = (m.group(1), "command line") if m else (None, None)
        if value is None and spec.get("flag") and cmd:
            value, source = ("on" if spec["flag"] in cmd.split() else "off"), "command line"
        if value is None and spec.get("default"):
            value, source = core.setting(conn, spec["default"], agent=agent) or None, "default"
        row = {}
        if spec["kind"] == "model":
            # The models of the session's own provider (#4633), as the launch dialog lists them; the one it runs is
            # chosen, by its ladder name, else as it reports it.
            row = {"kind": "choice", "choices": [m["name"] for m in models], "reported": value,
                   "notes": {m["name"]: f"{m['id']}{' · ' + m['note'] if m['note'] else ''}" for m in models}}
            value = core.model_name_of(models, value) or value
        out.append({"key": spec["key"], "label": spec["label"], "kind": spec["kind"], "choices": spec.get("choices"),
                    **row,
                    "value": value, "source": source, "live": bool(spec.get("live")), "note": spec.get("note"),
                    "default_key": spec.get("default"),
                    "default_value": core.setting(conn, spec["default"]) if spec.get("default") else None})
    return {"agent": agent, "provider": prov, "settings": out, "transcript": kind if path else None}


def setting_spec(provider_id, key):
    spec = next((x for x in PROVIDERS.get(provider_id, {}).get("settings", []) if x["key"] == key), None)
    if spec is None:
        raise core.RiverError(f"{PROVIDERS.get(provider_id, {}).get('label', provider_id)} has no setting {key}")
    return spec


def check_value(spec, value, conn=None, provider_id=None):
    import re as _re
    value = str(value or "").strip()
    if spec["kind"] == "model":
        names = [m["name"] for m in core.platform_models(conn, provider_id)] if conn is not None and provider_id else []
        if names and value not in names:
            raise core.RiverError(f"{spec['label']} is one of {', '.join(names)}, not {value or 'empty'}")
        spec = dict(spec, kind="text")
    if spec["kind"] == "choice" and value not in spec["choices"]:
        raise core.RiverError(f"{spec['label']} is one of {', '.join(spec['choices'])}, not {value or 'empty'}")
    if spec["kind"] == "text" and not _re.match(r"^[\w.:@/-]{1,80}$", value):
        raise core.RiverError(f"{spec['label']}: a name such as opus or gpt-6.1-sol")
    return value
