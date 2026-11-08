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
PROVIDERS = {"claude-code": {"label": "Claude Code"}, "codex": {"label": "Codex"}}


def provider(agent_type, kind=None):
    """{id, label} of a session's chat provider: its agent type, else the kind of its transcript."""
    pid = agent_type or PLATFORMS.get(kind or "")
    if not pid:
        return None
    return {"id": pid, "label": PROVIDERS.get(pid, {}).get("label") or pid}
STATES = {"working": "working", "prompt": "waits at a prompt", "answer": "waits for an answer", "idle": "idle",
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
    """A person's turn: its text, the images it names that river saved for a session (server.save_upload, #4610) as
    <agent>/<file> for GET /api/upload/, and how many image blocks the CLI put in it."""
    import re
    root = re.escape(str(Path(core.river_dir()) / "uploads"))
    found = re.findall(root + r"/([^/\s]+)/([^/\s]+\.(?:png|jpg|gif|webp))", text)
    e = {"at": at, "role": "user", "text": _clip(text, TEXT_MAX)}
    if found:
        e["images"] = [f"{a}/{f}" for a, f in found]
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
    if a.get("waiting_since"):
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


def waiting_for(conn, a, prompt=None, pane=False):
    """What waits for a person in one session (#4607): the prompt its tmux pane shows (server.pane_prompts); with no
    pane, the permission prompt its hooks reported, which a person answers in the session itself; and each open
    maxpm question from it to a person."""
    out = []
    if prompt:
        out.append({"kind": "prompt", **prompt})
    elif not pane and a.get("activity_prompt"):
        call = core._activity_call(a["activity_prompt"])
        out.append({"kind": "prompt", "elsewhere": True, "question": f"A permission prompt for {call}" if call
                    else "A permission prompt", "options": []})
    for r in conn.execute("SELECT m.id, m.body, m.item_id, m.to_agent FROM messages m JOIN agents p ON p.name=m.to_agent "
                          "AND p.kind='human' WHERE m.kind='question' AND m.state='open' AND m.from_agent=? ORDER BY m.id",
                          (a["name"],)):
        out.append({"kind": "question", "id": r["id"], "body": r["body"], "item_id": r["item_id"], "to": r["to_agent"]})
    return out


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
                tail = dict(tail, entries=[e for e in tail["entries"] if e["role"] != "result"][-PREVIEW:])
            except (OSError, ValueError, EOFError):
                path = None
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
                "waiting": waiting_for(conn, a, prompt, bool((terminals or {}).get(a["name"]))), "name": a["name"], "also": [], "agent_type": a.get("agent_type"), "model": a.get("model"),
                "role": a.get("role"), "note": a.get("note"),
                "holds": [{"id": h["id"], "title": h["title"], "status": h["status"]} for h in a["holds"]],
                "owns": a.get("owns") or [], "state": key, "state_label": label,
                "session_url": a.get("session_url"), "terminal": bool((terminals or {}).get(a["name"])),
                "transcript": kind if path else None, "last": tail["entries"],
                "at": tail["entries"][-1]["at"] if tail["entries"] else a.get("last_seen")}
        cards.append(card)
        seen.update((k, card) for k in keys)
    order = {"prompt": 0, "answer": 1, "working": 2, "starting": 3, "stopping": 4, "idle": 5, "ended": 6}
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


# The Artifacts tab (#4620): what the sessions made or named that the side panel can show. Files a session wrote
# (Write, Edit), full file paths, local pages (localhost, 127.0.0.1) and claude.ai artifact links in its own text,
# the person's turns, and its tool calls (a screenshot's file name). Results of tools are left out: a listing of
# files names many that nobody made.
ARTIFACT_BYTES = 4 * 1024 * 1024
ARTIFACT_DAYS = 1  # agents seen this recently, besides the running ones
_ART_CACHE = {}  # path -> (size, mtime, hits)
_FILE_RX = None
_URL_RX = None


def _hits_in(text, at, how):
    import re
    global _FILE_RX, _URL_RX
    _FILE_RX = _FILE_RX or re.compile(r"(?<![\w.:/-])((?:~|/)(?:[\w.@~+-]+/)*[\w@~+-][\w.@~+-]*\.(?:png|jpe?g|gif|webp|svg|pdf|html?|md))\b")
    _URL_RX = _URL_RX or re.compile(r"https?://(?:(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?(?:/[^\s)\]>'\"`\\]*)?"
                                    r"|claude\.ai/(?:code/)?artifact/[\w-]+)")
    out = [{"target": m, "kind": "file", "how": how, "at": at} for m in _FILE_RX.findall(text or "")]
    for u in _URL_RX.findall(text or ""):
        u = u.rstrip(".,;:")
        out.append({"target": u, "kind": "artifact" if "claude.ai/" in u else "page", "how": how, "at": at})
    return out


def _artifact_hits(path):
    """The hits of one transcript's last ARTIFACT_BYTES, read again only when the file changed."""
    st = Path(path).stat()
    got = _ART_CACHE.get(str(path))
    if got and got[:2] == (st.st_size, st.st_mtime_ns):
        return got[2]
    text, _s, _e = _read(path, None, ARTIFACT_BYTES)
    hits = []
    for line in text.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
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
    _ART_CACHE[str(path)] = (st.st_size, st.st_mtime_ns, hits)
    return hits


def artifacts(conn, terminals=None, allowed=None, root=None, croot=None, limit=300):
    """[{target, kind (file, page, artifact), how (wrote, named), session, item, at}], newest first, one per target:
    from the running sessions and the agents seen in the last ARTIFACT_DAYS. allowed(path): whether the side panel
    may show that file (server.local_file); a file it may not show is left out."""
    names = {a["name"] for a in running(conn, terminals)}
    since = core.iso(core.now() - core.timedelta(days=ARTIFACT_DAYS))
    names |= {r["name"] for r in conn.execute("SELECT name FROM agents WHERE kind='ai' AND last_seen>=?", (since,))}
    best = {}
    for name in sorted(names):
        kind, path = transcript(conn, name, root, croot)
        if not path:
            continue
        try:
            hits = _artifact_hits(path)
        except (OSError, ValueError, EOFError):
            continue
        for h in hits:
            old = best.get(h["target"])
            if old is None or (h["at"] or "") > (old["at"] or "") or (h["how"] == "wrote" and old["how"] != "wrote"):
                best[h["target"]] = dict(h, session=name)
    out = []
    for h in sorted(best.values(), key=lambda h: h["at"] or "", reverse=True):
        if h["kind"] == "file" and allowed is not None and not allowed(h["target"]):
            continue
        r = conn.execute("SELECT item_id FROM holds WHERE agent=? AND started_at<=? AND (ended_at IS NULL OR ended_at>=?) "
                         "ORDER BY started_at DESC LIMIT 1", (h["session"], (h["at"] or "")[:19] + "Z", (h["at"] or "")[:19] + "Z")
                         ).fetchone()
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
