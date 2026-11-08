"""The Sessions tab of maxpm serve: every running agent session, with a read-only view of its transcript (#4592).

The page cannot show claude.ai or chatgpt.com in a frame (both refuse it), so maxpm serve reads the transcript file
each agent CLI writes on this computer and sends its lines to the page: the person's and the agent's text, and
each tool call as one line. Claude Code writes <config dir>/projects/<folder>/<session id>.jsonl (the session id
comes from agent_sessions); Codex writes ~/.codex/sessions/<y>/<m>/<d>/rollout-<time>-<thread id>.jsonl, which it
compresses to .jsonl.zst (the thread id is the agent's native_address). Nothing here writes to a session: a
person types in the session itself, through its Open link (session_url) or its terminal.
"""

import json
import os
from pathlib import Path

from . import core

TAIL_BYTES = 2 * 1024 * 1024  # the first read of the large view: the last part of the transcript only
CARD_BYTES = 256 * 1024      # the read of a card: its last few lines
LIMIT = 300               # at most this many lines in one answer
TEXT_MAX = 4000           # a longer text is cut
TOOL_MAX = 160            # the one line of a tool call
PREVIEW = 4               # the lines a card shows
STATES = {"working": "working", "prompt": "waits at a prompt", "answer": "waits for an answer", "idle": "idle",
          "stopping": "asked to stop"}


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
                out.append({"at": at, "role": "tool", "text": _tool_line(x.get("name"), x.get("input"))})
        stop = m.get("stop_reason")
        return out, ("idle" if stop in ("end_turn", "stop_sequence") else "working" if stop else None)
    text = _parts_text(content, ("text",)).strip()
    if text.startswith("<task-notification>"):
        out.append({"at": at, "role": "event", "text": "a background task ended"})
    elif text and not text.startswith("<"):  # tags the CLI adds: a command's name and output, reminders
        out.append({"at": at, "role": "user", "text": _clip(text, TEXT_MAX)})
    return out, "working"


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
        return [{"at": at, "role": p["role"], "text": _clip(text, TEXT_MAX)}], None
    if t in ("function_call", "custom_tool_call", "local_shell_call"):
        inp = p.get("arguments") if t == "function_call" else p.get("input") if t == "custom_tool_call" else p.get("action")
        return [{"at": at, "role": "tool", "text": _tool_line(p.get("name") or "shell", inp)}], None
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
    out, turn = [], None
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
    return {"entries": out[-limit:], "cursor": end, "turn": turn}


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


def views(conn, terminals=None, root=None, croot=None):
    """The cards of the Sessions tab: one per running agent session, with its item, state, Open link, and its last
    lines."""
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
                tail = entries(kind, path, None, PREVIEW, CARD_BYTES)
            except (OSError, ValueError, EOFError):
                path = None
        key, label = state(conn, a, tail["turn"], waiting)
        card = {"name": a["name"], "also": [], "agent_type": a.get("agent_type"), "model": a.get("model"),
                "role": a.get("role"), "note": a.get("note"),
                "holds": [{"id": h["id"], "title": h["title"]} for h in a["holds"]],
                "owns": a.get("owns") or [], "state": key, "state_label": label,
                "session_url": a.get("session_url"), "terminal": bool((terminals or {}).get(a["name"])),
                "transcript": kind if path else None, "last": tail["entries"],
                "at": tail["entries"][-1]["at"] if tail["entries"] else a.get("last_seen")}
        cards.append(card)
        seen.update((k, card) for k in keys)
    order = {"prompt": 0, "answer": 1, "working": 2, "stopping": 3, "idle": 4}
    return sorted(cards, key=lambda c: (order.get(c["state"], 5), c["name"]))


def view(conn, agent, cursor=None, root=None, croot=None):
    """The large view of one session: its lines after cursor (None: the last LIMIT of them), the next cursor, and
    its state. Raises RiverError for an agent that has no transcript on this computer."""
    a = core.agent_status(conn, agent)
    kind, path = transcript(conn, agent, root, croot)
    if not path:
        raise core.RiverError(f"no transcript of {agent} on this computer: MaximizePM reads Claude Code transcripts "
                              f"and Codex session logs" + ("" if _zstd() else " (Codex logs need Python 3.14)"))
    got = entries(kind, path, cursor)
    turn = got["turn"] if got["turn"] or cursor is None else entries(kind, path)["turn"]
    key, label = state(conn, a, turn)
    return {"agent": agent, "transcript": kind, "entries": got["entries"], "cursor": got["cursor"],
            "turn": turn, "state": key, "state_label": label, "session_url": a.get("session_url")}
