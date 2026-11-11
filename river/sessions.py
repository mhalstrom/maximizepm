"""The Sessions tab of maxpm serve: every running agent session, with a read-only view of its transcript (#4592).

The page cannot show claude.ai or chatgpt.com in a frame (both refuse it), so maxpm serve reads the transcript file
each agent CLI writes on this computer and sends its lines to the page: the person's and the agent's text, and
each tool call as one line. Each agent's adapter knows where its log is and how to read a line (its chat log part,
river/agents, #5182): Claude Code's is found by the session id (agent_sessions), Codex's by the thread id (the
agent's native_address). The readers here are the same for every agent. Nothing here writes to a session: a
person types through its Open link (session_url), its terminal, or the text bar of the large view
(server.session_send, for a session in a tmux pane).
"""

import json
import os
import re
import time
from pathlib import Path

from . import core  # first: core loads the agent adapters while it loads (#5181)
from . import agents as _agents

TAIL_BYTES = 2 * 1024 * 1024  # the first read of the large view: the last part of the transcript only
CARD_BYTES = 256 * 1024      # the read of a card: its last few lines
LIMIT = 300               # at most this many lines in one answer
TEXT_MAX = 4000           # a longer text is cut
TOOL_MAX = 160            # the one line of a tool call
DETAIL_MAX = 4000         # a tool call's input, and its result, when the page opens it
PREVIEW = 4               # the lines a card shows
ADOPT_QUIET = 120         # tmux_adopt: a session moves into tmux after its turn ended and it wrote nothing this long
# The chat provider of a session (#4627): its agent adapter gives the label, the voice text (SETUP["voice"], #5184) and
# the settings of its Settings dialog (Adapter.session_specs, #5169). There is no table of providers here (#5185).


def _place(conn, item_id):
    """{project, goals} of an item: where the session's links to Plan go."""
    r = conn.execute("SELECT p.name FROM items i JOIN projects p ON p.id = i.project_id WHERE i.id=?", (item_id,)).fetchone()
    goals = [g["name"] for g in conn.execute(
        "SELECT g.name FROM item_goals ig JOIN goals g ON g.id = ig.goal_id WHERE ig.item_id=? ORDER BY g.rank, g.name",
        (item_id,))]
    return {"project": r["name"] if r else None, "goals": goals}


def provider(agent_type, kind=None):
    """{id, label, agent, voice_cli} of a session's chat provider: its agent type, else the kind of its transcript.
    agent: the id of its adapter (GET /api/agents), or None; voice_cli: its adapter's SETUP["voice"]."""
    a = _agents.find(agent_type) if agent_type else _adapter(kind)
    pid = agent_type or (core._platform_of(a) if a else None)
    if not pid:
        return None
    return {"id": pid, "label": a.label if a else pid, "agent": a.id if a else None,
            "voice_cli": (a.setup or {}).get("voice") if a else None}
STATES = {"working": "working", "prompt": "waits at a prompt", "answer": "waits for an answer", "idle": "idle",
          "expert": "talks with you",
          "stopping": "ending: asked to stop", "starting": "not connected yet: it ran no maxpm command",
          "ended": "ended before its first maxpm command"}


def _zstd():
    try:
        from compression import zstd  # Python 3.14 and later
        return zstd
    except ImportError:
        return None


def _adapter(kind):
    """The adapter of a chat log kind (the adapter's id: claude, codex), or None."""
    return _agents.find(kind) if kind else None


def newest(paths):
    """The path of the newest file among paths (by its time of change), or None."""
    best = None
    for p in paths:
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if best is None or m > best[0]:
            best = (m, p)
    return best[1] if best else None


def transcript(conn, agent, root=None, croot=None):
    """(kind, path) of the agent's newest chat log on this computer: kind is the id of the agent adapter that reads it
    (claude, codex); or (None, None). Each adapter with a chat log part finds the log of its session by what its
    CHAT['ref'] names. root and croot: the log folders of Claude Code and Codex that a test gives."""
    a = conn.execute("SELECT agent_type, platform, native_address FROM agents WHERE name=?", (agent,)).fetchone()
    if a is None:
        return None, None
    r = conn.execute("SELECT session_id FROM agent_sessions WHERE agent=? ORDER BY first_seen DESC, rowid DESC LIMIT 1",
                     (agent,)).fetchone()
    runs = _agents.find(a["platform"]) if a["platform"] else None
    roots = {"claude": root, "codex": croot}
    best = None
    for ad in _agents.load().values():
        how = ad.chat_ref()
        ref = (r["session_id"] if how == "session" and r else
               a["native_address"] if how == "address" and runs is not None and runs.id == ad.id else None)
        path = ad.chat_path(ref, None, roots.get(ad.id)) if ref and ad.supports("chat") else None
        try:
            m = path.stat().st_mtime if path else None
        except OSError:
            continue
        if m is not None and (best is None or m > best[0]):
            best = (m, ad.id, path)
    return (best[1], best[2]) if best else (None, None)


def session_ref(conn, agent, ad, path):
    """The agent's session id whose chat log is path (#5266): the name of the log file before its first dot when it
    is one of the agent's session ids (Claude Code: <id>.jsonl), else the id whose chat_path is path (Copilot:
    <id>/events.jsonl), newest first; else that file name."""
    stem = Path(path).name.split(".")[0]
    ids = [r["session_id"] for r in conn.execute(
        "SELECT session_id FROM agent_sessions WHERE agent=? ORDER BY first_seen DESC, rowid DESC", (agent,))]
    if stem in ids or ad is None or ad.chat_ref() != "session":
        return stem
    for sid in ids:
        try:
            got = ad.chat_path(sid)
        except Exception:  # noqa: BLE001 - an adapter's reader (a user module too) never breaks a restart
            continue
        if got and Path(got) == Path(path):
            return sid
    return stem


def no_log_note(conn, agent, root=None, croot=None):
    """Why the Sessions tab shows no chat of `agent` (#5257): which agent's log MaximizePM looked for (the adapter of
    the agent's type) and why it found none: the agent's type is not known yet, the agent keeps no log that
    MaximizePM reads, no session id is recorded yet, or the log file is not on this computer."""
    a = conn.execute("SELECT agent_type, platform, native_address FROM agents WHERE name=?", (agent,)).fetchone()
    ad = _agents.find((a["agent_type"] or a["platform"]) if a else None)
    head = f"No chat of {agent} on this computer"
    if ad is None:
        readers = [x.label for x in _agents.load().values() if x.supports("chat")]
        return (f"{head}: MaximizePM does not know which agent it runs yet (it learns it from the session's first "
                f"maxpm command). It reads the chat logs of {', '.join(readers)}.")
    if not ad.supports("chat"):
        return f"{head}: {ad.label} keeps no chat log that MaximizePM reads."
    how = ad.chat_ref()
    if how is None:
        return (f"{head}: MaximizePM has no rule to find the log of a {ad.label} session (its adapter names no "
                f"CHAT['ref']).")
    ref = None
    if how == "session":
        r = conn.execute("SELECT session_id FROM agent_sessions WHERE agent=? ORDER BY first_seen DESC, rowid DESC "
                         "LIMIT 1", (agent,)).fetchone()
        ref = r["session_id"] if r else None
    elif how == "address":
        ref = a["native_address"]
    if not ref:
        return (f"{head}: no {ad.label} session id of {agent} is recorded yet, so MaximizePM cannot find its log; "
                f"its next maxpm command records it.")
    path = ad.chat_path(ref, None, {"claude": root, "codex": croot}.get(ad.id))
    zst = "" if _zstd() or ad.id != "codex" else " (a compressed Codex log needs Python 3.14)"
    if path is None:
        return f"{head}: {ad.label} has no log of session {ref} here{zst}."
    return f"{head}: the {ad.label} log of session {ref} is not here ({path}){zst}."


def _read(path, cursor, tail=TAIL_BYTES):
    """(text, start, end): the complete lines of the file from byte `cursor` on (None: its last `tail` bytes), the
    byte where that text starts, and the byte after its last complete line, the next cursor."""
    raw, start, end = _read_raw(path, cursor, tail)
    return raw.decode("utf-8", "replace"), start, end


def _bytes(path, start, end):
    """The bytes of the file from start to end (a .zst log: of its decompressed text)."""
    if str(path).endswith(".zst"):
        return _zstd().ZstdDecompressor().decompress(Path(path).read_bytes())[start:end]
    with open(path, "rb") as f:
        f.seek(start)
        return f.read(max(0, end - start))


def _size(path):
    if str(path).endswith(".zst"):
        return len(_zstd().ZstdDecompressor().decompress(Path(path).read_bytes()))
    return Path(path).stat().st_size


def _read_raw(path, cursor, tail=TAIL_BYTES):
    """_read, as bytes."""
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
    return chunk[:end], start, start + end


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


# What a tool call does, in plain words for a person (#5086): the second line of a row on the Sessions tab when the
# session wrote no 'maxpm now' lately. Never a tool name, a link, a path, or an id.
_TOOL_WORDS = [
    (r"^(Read|NotebookRead|view_image)$", "Reads the code"),
    (r"^(Edit|MultiEdit|NotebookEdit|apply_patch)$", "Edits the code"),
    (r"^Write$", "Writes a file"),
    (r"^(Grep|Glob|LS|LSP)$", "Searches the code"),
    (r"^WebSearch$", "Searches the web"),
    (r"^WebFetch$", "Reads a web page"),
    (r"^(TodoWrite|TaskCreate|TaskUpdate|update_plan)$", "Plans the next steps"),
    (r"^(AskUserQuestion|request_user_input)$", "Asks you a question"),
    (r"^(ToolSearch|Skill)$", "Reads a guide"),
    (r"^(Monitor|ScheduleWakeup|TaskOutput|BashOutput|SendMessage)$", "Waits for a helper"),
    (r"^Artifact", "Publishes a page"),
    (r"(?i)__browser_(navigate|tabs|navigate_back)$|__navigate$|__tabs_", "Opens a page in a browser"),
    (r"(?i)__browser_take_screenshot$|__computer$|__gif_creator$|__upload_image$", "Looks at a page in a browser"),
    (r"(?i)__browser_|__read_page$|__find$|__form_input$|__javascript_tool$|__get_page_text$", "Works in a browser"),
]
_CMD_WORDS = [
    (r"\b(unittest|pytest|npm (run )?test|cargo test|go test|jest|vitest)\b", "Runs the tests"),
    (r"\bgit (commit)\b", "Commits the change"),
    (r"\bgit (push)\b", "Pushes the change"),
    (r"\bgit (log|show|diff|status|blame)\b", "Reads the history of the code"),
    (r"\bgit (pull|fetch|merge|rebase|worktree|checkout|switch)\b", "Updates the code"),
    (r"\bmaxpm\b", "Updates the queue"),
    (r"\b(npm|pip|brew|cargo|uv) (install|add|ci)\b", "Installs tools"),
    (r"\b(npm run build|make|cargo build|tsc)\b", "Builds the code"),
    (r"\b(grep|rg|find|ls)\b", "Searches the code"),
    (r"\b(cat|sed -n|head|tail|less|wc)\b", "Reads the code"),
    (r"\b(curl|wget)\b", "Reads a web page"),
    (r"\b(sleep|wait)\b", "Waits"),
]
_NOT_A_VERB = {"git", "npm", "python", "python3", "the", "a", "an", "all", "new", "maxpm", "this", "each", "no"}


def _third(desc):
    """'Run the tests' -> 'Runs the tests': a Claude Code command description, as the session does it now."""
    w = desc.split(" ", 1)
    v = w[0]
    if not re.fullmatch(r"[A-Z][a-z]+", v) or v.lower() in _NOT_A_VERB or v.endswith("ing") or (
            v.endswith("s") and not v.endswith("ss")):
        return desc
    v = v[:-1] + "ies" if re.search(r"[^aeiou]y$", v) else v + "es" if re.search(r"(s|sh|ch|x|z|o)$", v) else v + "s"
    return " ".join([v] + w[1:])


def plain_tool(name, inp):
    """What a tool call does, in at most about 10 plain words, or None."""
    name = str(name or "")
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except ValueError:
            inp = {"command": inp}
    inp = inp if isinstance(inp, dict) else {}
    desc = " ".join(str(inp.get("description") or "").split())
    # A short description with no path, link, id or code is the session's own plain words (Bash, Agent).
    if desc and len(desc) <= 70 and not re.search(r"(?<![\w&])#\d+|https?://|\S/|/\S|`|\$|__", desc):
        return _third(desc[0].upper() + desc[1:]).rstrip(".")
    if name in ("Agent", "Task"):
        return "Asks a helper agent"
    for pat, words in _TOOL_WORDS:
        if re.search(pat, name):
            return words
    cmd = inp.get("command") or inp.get("cmd") or ""
    cmd = " ".join(map(str, cmd)) if isinstance(cmd, list) else str(cmd)
    if cmd or name in ("Bash", "shell", "exec_command", "local_shell", "PowerShell"):
        return next((w for pat, w in _CMD_WORDS if re.search(pat, cmd)), "Runs a command")
    m = re.match(r"mcp__(?:claude_ai_|plugin_[a-z]+_)?([A-Za-z0-9-]+?)(?:_[A-Za-z0-9]+)?__", name)
    if m:
        return "Uses " + m.group(1).replace("-", " ").replace("_", " ")
    return "Works"


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


def parse_lines(parse_line, lines):
    """(entries, turn) of a list of log lines (each a str), with parse_line(d) -> (entries, turn) for one decoded
    line: the parse_chat of an adapter."""
    out, turn = [], None
    for line in lines:
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        got, st = parse_line(d)
        out += got
        turn = st or turn
    return out, turn


_TAIL_CACHE = {}  # (path, tail) -> (size, mtime, the answer of entries(kind, path)): the page asks every few seconds


def entries(kind, path, cursor=None, limit=LIMIT, tail=TAIL_BYTES):
    """{entries, cursor, turn, before}: the page lines of the transcript after cursor (None: its last part), at most
    limit of them (the newest), the cursor of the next read, the state of the last turn (working, idle, or None), and
    the byte where the line of the first entry starts: the earlier lines are before it (older_page)."""
    if cursor is None:
        st = Path(path).stat()
        got = _TAIL_CACHE.get((str(path), tail))
        if not got or got[:2] != (st.st_size, st.st_mtime_ns):
            got = _TAIL_CACHE[(str(path), tail)] = (st.st_size, st.st_mtime_ns, _entries(kind, path, None, tail=tail))
        r = got[2]
    else:
        r = _entries(kind, path, cursor)
    k = _first(r["offs"], limit)
    return {"entries": r["entries"][k:], "cursor": r["cursor"], "turn": r["turn"], "cwd": r["cwd"],
            "before": r["offs"][k] if k else r["start"]}


def _first(offs, limit):
    """The index of the first of the newest `limit` entries, moved back so that one line's entries stay together."""
    k = max(0, len(offs) - limit)
    while 0 < k < len(offs) and offs[k - 1] == offs[k]:
        k -= 1
    return k


def _parse_text(ad, raw, start, cut):
    """_parse of a log whose entries span several lines (CHAT format "text", #5222): parse_chat over each block of
    lines (CHAT['block']: a run of lines that match it starts a block), each entry with the byte where its block
    starts, an entry with no time with the time of the entry before it, and each call id with the block's byte. cut: the bytes start inside the log (the
    tail, a page back), so the lines before the first block start belong to a block that began earlier: left out,
    and "start" is where the first block starts."""
    rx = re.compile(ad.chat["block"]) if (ad.chat or {}).get("block") else None
    blocks, at, prev = [], start, False
    for line in raw.splitlines(keepends=True):
        text = line.decode("utf-8", "replace")
        hit = bool(rx and rx.search(text.rstrip("\r\n")))
        if hit and not prev or not blocks:
            blocks.append([at, []])
        prev = hit
        blocks[-1][1].append(text)
        at += len(line)
    if cut and rx and blocks and not rx.search(blocks[0][1][0].rstrip("\r\n")):
        blocks = blocks[1:]
    out, offs, turn, last = [], [], None, None
    for here, lines in blocks:
        got, st = ad.parse_chat(lines)
        for e in got:
            if e.get("at"):
                last = e["at"]
            elif last:
                e = {**e, "at": last}
            # a call id is unique in its block only: the byte of the block makes it unique in the log, the same in
            # every read
            e = {**e, **{k: f"{here}:{e[k]}" for k in ("id", "for") if e.get(k) is not None}}
            out.append(e)
            offs.append(here)
        turn = st or (None if got else turn)
    return {"entries": out, "offs": offs, "turn": turn, "cwd": None,
            "start": blocks[0][0] if blocks else start + len(raw)}


def _parse(kind, raw, start, cut=False):
    """The page lines of the transcript bytes raw (complete lines; the first starts at byte start), with the byte
    where the line of each one starts, the state of the last turn, and the folder the session runs in. A text log
    (CHAT format "text"): _parse_text, with cut."""
    ad = _adapter(kind)
    if ad is not None and ad.chat_format() == "text":
        return _parse_text(ad, raw, start, cut)
    out, offs, turn, cwd, at = [], [], None, None, start
    for line in raw.splitlines(keepends=True):
        here, at = at, at + len(line)
        if not line.strip():
            continue
        text = line.decode("utf-8", "replace")
        try:
            d = json.loads(text)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        got, st = ad.parse_chat_line(d, text) if ad else ([], None)
        out += got
        offs += [here] * len(got)
        turn = st or turn
        p = d.get("payload") if isinstance(d.get("payload"), dict) else d  # the folder the session runs in
        cwd = p.get("cwd") if isinstance(p.get("cwd"), str) else cwd
    if cwd is None and ad is not None and ad.has("chat_cwd"):
        # A log that keeps the folder under a key of its own (Copilot: session.start's data.context.cwd; Goose:
        # working_dir): the adapter reads it from these lines (#5266).
        cwd = _adapter_cwd(ad, raw.decode("utf-8", "replace").splitlines())
    return {"entries": out, "offs": offs, "turn": turn, "cwd": cwd, "start": start}


def _adapter_cwd(ad, lines):
    """The folder that the adapter's chat_cwd reads in lines, or None (a reader that fails reads nothing)."""
    try:
        got = ad.module.chat_cwd(list(lines))
    except Exception:  # noqa: BLE001 - an adapter's reader (a user module too) never breaks the chat
        return None
    return got if isinstance(got, str) and got else None


_HEAD_CWD = {}  # str(path) -> the folder chat_cwd reads at the start of the log: it does not change
HEAD_BYTES = 256 * 1024


def head_cwd(kind, path):
    """The folder the session started in, as the adapter's chat_cwd reads it in the first HEAD_BYTES of its log, or
    None. For a log that names its folder only at its start (Copilot's session.start, #5266), when the tail that the
    page and the Settings dialog read does not reach it. Kept for each file."""
    ad = _adapter(kind)
    if ad is None or not ad.has("chat_cwd") or not path:
        return None
    key = str(path)
    if key not in _HEAD_CWD:
        try:
            raw = _bytes(path, 0, HEAD_BYTES)
        except OSError:
            return None
        if len(raw) == HEAD_BYTES:
            raw = raw[:raw.rfind(b"\n") + 1]  # complete lines only
        _HEAD_CWD[key] = _adapter_cwd(ad, raw.decode("utf-8", "replace").splitlines())
    return _HEAD_CWD[key]


def _entries(kind, path, cursor, limit=LIMIT, tail=TAIL_BYTES):
    raw, start, end = _read_raw(path, cursor, tail)
    r = _parse(kind, raw, start, cut=cursor is None and start > 0)
    k = max(0, len(r["entries"]) - limit) if cursor is not None else 0
    return {"entries": r["entries"][k:], "offs": r["offs"][k:], "cursor": end, "turn": r["turn"],
            "cwd": r["cwd"] or head_cwd(kind, path), "start": r["start"]}


def _page_back(kind, path, before, limit=LIMIT):
    """(entries, before): the newest `limit` page lines of the transcript that end before byte `before`, and the byte
    where the first of their lines starts (0: the start of the file). It reads backward in growing windows."""
    window = 256 * 1024
    while True:
        start = max(0, before - window)
        raw = _bytes(path, start, before)
        if start > 0:
            cut = raw.find(b"\n") + 1
            if not cut:  # one line longer than the window
                window *= 4
                continue
            raw, start = raw[cut:], start + cut
        r = _parse(kind, raw, start, cut=start > 0)
        if len(r["entries"]) >= limit or start == 0:
            break
        window *= 4
    k = _first(r["offs"], limit)
    return r["entries"][k:], (r["offs"][k] if k else r["start"])


def transcript_chain(conn, agent, kind, path):
    """The chat logs of one agent's chat, oldest first, the newest (path) last (#5120): the adapter's chat_chain over
    the agent's session ids (agent_sessions, oldest first); only path for an adapter without one."""
    ad = _adapter(kind)
    if ad is None or not ad.has("chat_chain"):
        return [path]
    refs = [r["session_id"] for r in conn.execute(
        "SELECT session_id FROM agent_sessions WHERE agent=? ORDER BY first_seen, rowid", (agent,))]
    return ad.chat_chain(path, refs)


def _older(chain, i, before, entries):
    """(entries, older): the page lines with a 'Resumed' divider when they reach the start of a file after the first,
    and where the next earlier page starts ({file, before}; None at the start of the session)."""
    if before > 0:
        return entries, {"file": Path(chain[i]).name, "before": before}
    if i > 0:
        return ([{"at": entries[0]["at"] if entries else None, "role": "divider", "kind": "resumed", "text": "Resumed"}]
                + entries), {"file": Path(chain[i - 1]).name, "before": None}
    return entries, None


def older_page(conn, agent, file, before=None, root=None, croot=None):
    """GET /api/session?agent=&file=&before= (#5120): the page lines of the agent's chat before byte `before` of its
    transcript `file` (None: its end), at most LIMIT, and where the next earlier page starts (older; None: the start of
    the session). Scrolling up in the chat reads the whole session this way, across every compaction."""
    kind, path = transcript(conn, agent, root, croot)
    if not path:
        raise core.RiverError(f"no transcript of {agent} on this computer")
    chain = transcript_chain(conn, agent, kind, path)
    i = next((n for n, p in enumerate(chain) if Path(p).name == file), None)
    if i is None:
        raise core.RiverError(f"{file} is not a transcript of {agent}")
    got, b = _page_back(kind, chain[i], _size(chain[i]) if before is None else max(0, int(before)))
    got, older = _older(chain, i, b, got)
    return {"agent": agent, "entries": got, "older": older, "refs": item_refs(conn, got)}


def state(conn, a, turn, waiting=None):
    """(key, label) of what a session does now: stopping, prompt (a permission prompt), answer (an open question to
    a person), idle (in maxpm wait, or its turn ended), or working."""
    if a.get("stop_at"):
        return "stopping", STATES["stopping"]
    if a.get("activity_prompt"):
        return "prompt", STATES["prompt"]
    if a["name"] in (waiting if waiting is not None else core.waits_on_person(conn, [a["name"]])):
        return "answer", STATES["answer"]
    if a.get("role") in ("expert", "assistant") and turn != "working":
        # An expert waits at its prompt for the person by design (#4955), and so does an assistant (#5110): not idle,
        # and nothing waits on the person.
        return "expert", STATES["expert"]
    if a.get("waiting_since") and not (turn == "working" and a.get("activity_at") and a.get("wait_at")
                                       and a["activity_at"] > a["wait_at"]):
        # A turn after the last maxpm wait started: a person talks with it while its wait runs in the background.
        return "idle", "idle: waits for work"
    if turn == "idle" or (turn is None and (a.get("activity_event") in ("Stop", "report:idle") or a.get("role") == "idle")):
        bg = core.background_work(conn, a["name"])  # its harness wakes it when this ends
        return "idle", f"idle: waits on {bg[0]['what'] or 'its background work'}" if bg else "idle: at its prompt"
    return "working", STATES["working"]


def pane_state(conn, a, turn, prompt=None, pane=False, waiting=None):
    """state() with what the session's tmux pane shows now (server.pane_prompts): a prompt there is a prompt. For an
    agent whose state comes from its pane (its adapter's STATE['state'] is 'detect': Codex), a pane read now that
    shows no prompt wins over the blocked state that the watch of maxpm serve recorded up to notify_interval before
    (#5220: after mark chose in Codex's /model menu, the page said for half a minute that it waited at a permission
    prompt)."""
    key, label = state(conn, a, turn, waiting)
    if prompt and key != "stopping":
        return "prompt", STATES["prompt"]
    if key == "prompt" and pane and not prompt and core._state_source(conn, a["name"]) == "detect":
        return state(conn, dict(a, activity_prompt=None), turn, waiting)
    return key, label


def adopt_check(conn, a, kind, path, turn, cwd, waiting=None):
    """tmux_adopt (#4598) for a session outside tmux: ("now", why) when maxpm serve moves it into a tmux pane now,
    ("later", why) when it moves after its turn, or ("never", why)."""
    if core.setting(conn, "tmux_adopt", agent=a["name"]) != "on":
        return "never", "tmux_adopt is off"
    if os.name == "nt" or not core.tmux_path():
        return "never", "tmux is not installed"
    ad = _adapter(kind)
    if not path or ad is None or not ad.supports("resume"):
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
    pane here, or it is active (or asked to stop and still holds work) and recorded no process (a sandbox that
    cannot run ps)."""
    out, here = [], core.this_host()
    for r in conn.execute("SELECT name FROM agents WHERE kind='ai' ORDER BY name"):
        a = core.agent_status(conn, r["name"])
        # A session asked to stop stays while it still holds its task or a target: its row shows 'ending' (#5041).
        # When it has released them (core.ended), it leaves the list.
        if core.ended(a) or a["state"] == "gone" or a.get("via") == "http":
            continue
        if a.get("pid") and a.get("host") == here:
            if not core.pid_alive(a["pid"]):
                continue
        elif not ((terminals or {}).get(a["name"]) or (not a.get("pid") and a["state"] in ("active", "stopped"))):
            continue
        out.append(a)
    return out


def _open_questions(conn, name):
    # Not a question held for the manager (#5307): the person sees only what the manager forwards.
    return conn.execute("SELECT m.id, m.body, m.item_id, m.to_agent, m.created_at FROM messages m JOIN agents p ON "
                        "p.name=m.to_agent AND p.kind='human' WHERE m.kind='question' AND m.state='open' AND "
                        "m.held_for IS NULL AND m.from_agent=? ORDER BY m.id", (name,)).fetchall()


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
                    "asked_at": r["created_at"], "old": old, "note": core.forward_note(conn, r)})
    return out


WORK_NAME_MAX = 60
DOING_FRESH = 15 * 60  # seconds: a 'maxpm now' text older than this gives way to the plain words of the last tool call


def _short_title(t, short=None):
    return core.short_title(t, short)


def custom_title(path, kind, tail=256 * 1024):
    """The session title in the last `tail` bytes of its chat log (the adapter's chat_title), or None."""
    ad = _adapter(kind)
    if not path or ad is None or not ad.has("chat_title"):
        return None
    text, _s, _e = _read(path, None, tail)
    return ad.chat_title(text.splitlines())


def work_name(conn, a, path=None, root=None, croot=None, kind=None):
    """What a session works on, as the name the page shows for it (#4695); its agent id stays its address. In this
    order: its own headline (maxpm now --work) for its item; the short title of the item it holds; the goal it owns; Manager; the deploy targets it owns; the item it
    finished last; its session title (the chat log's, kind: its adapter); else its agent id without the four hex
    digits."""
    holds = a.get("holds") or []
    head, head_at = a.get("headline"), a.get("headline_at") or ""
    if holds:
        # The session's own headline (maxpm now --work) when it wrote it for this item; else the item's short title.
        if head and head_at >= (holds[0].get("claimed_at") or ""):
            return head
        return _short_title(holds[0]["title"], holds[0].get("short"))
    if head:
        last = conn.execute("SELECT MAX(closed_at) t FROM items WHERE assignee=? AND status='done'",
                            (a["name"],)).fetchone()["t"]
        if head_at >= (last or ""):
            return head
    g = conn.execute("SELECT name FROM goals WHERE owner=? AND status='open' ORDER BY rank LIMIT 1", (a["name"],)).fetchone()
    if g:
        return "Goal: " + g["name"]
    if a.get("role") == "manager":
        return "Manager"
    owns = [o.get("name") if isinstance(o, dict) else str(o) for o in a.get("owns") or []]
    if owns:
        return "Deploys " + ", ".join(owns)
    r = conn.execute("SELECT title, short FROM items WHERE assignee=? AND status='done' ORDER BY closed_at DESC LIMIT 1",
                     (a["name"],)).fetchone()
    if r:  # the item it finished last; its line 2 says that it waits (#5086: no 'last:')
        return _short_title(r["title"], r["short"])
    if path is None:
        kind, path = transcript(conn, a["name"], root, croot)
    try:
        t = custom_title(path, kind)
    except (OSError, ValueError):
        t = None
    if t:
        return _short_title(t)
    return re.sub(r"-[0-9a-f]{4}$", "", a["name"])


def doing_now(a, key, label, entries):
    """The second line of a session's row (#5086): what it does now, in plain words. While it works: its own words
    (maxpm now) from the last DOING_FRESH and from its present item, else the plain words of its last tool call;
    else what it waits for."""
    if key not in ("working", "expert"):
        t = re.sub(r"^idle: ", "", label or key)
        return t[:1].upper() + t[1:]
    at = a.get("doing_at")
    held = (a.get("holds") or [{}])[0].get("claimed_at") or ""
    if a.get("doing") and at and at >= held and (core.now() - core.parse_iso(at)).total_seconds() < DOING_FRESH:
        return a["doing"]
    for e in reversed(entries or ()):
        if e.get("role") == "tool":
            return e.get("plain") or "Works"
        if e.get("role") == "assistant":
            return "Writes to you" if key == "expert" else "Thinks about the next step"
        if e.get("role") == "user":
            return "Reads your message"
    return "Works"


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
        prompt = (prompts or {}).get(a["name"])
        # The override of pane_state only after a read of the panes (prompts given), not with no read at all.
        key, label = pane_state(conn, a, tail["turn"], prompt,
                                prompts is not None and bool((terminals or {}).get(a["name"])), waiting)
        if not path and not a.get("pid") and key not in ("stopping", "answer", "prompt"):
            # It ran no maxpm command and wrote no transcript: not working (#4621). Its pane shows a shell: it ended.
            # A prompt that the pane watcher reported (maxpm report --state blocked) waits on the person, also before
            # its first maxpm command: it stays a prompt (#5269).
            key = "ended" if (terminals or {}).get(a["name"]) in (ended_panes or ()) else "starting"
            label = STATES[key]
        adopt = None if (terminals or {}).get(a["name"]) else adopt_check(
            conn, a, kind, path, tail["turn"], tail.get("cwd"), waiting)
        card = {"doing": doing_now(a, key, label, tail["entries"]),
                "adopt": adopt and {"when": adopt[0], "why": adopt[1]}, "provider": provider(a.get("agent_type"), kind),
                "waiting": waiting_for(conn, a, prompt, bool((terminals or {}).get(a["name"])), said), "name": a["name"],
                "title": work_name(conn, a, path, root, croot, kind), "also": [], "agent_type": a.get("agent_type"), "model": a.get("model"),
                "role": a.get("role"), "note": a.get("note"), "topic": a.get("expert_topic"),
                "assistant_for": a.get("assistant_for"),
                "talked_at": a.get("talked_at"), "pinned_by": a.get("pinned_by") or [],
                # Its project and goals too, so the head's links to Plan need no other answer (#5124).
                "holds": [{"id": h["id"], "title": h["title"], "status": h["status"], **_place(conn, h["id"])}
                          for h in a["holds"]],
                "owns": a.get("owns") or [], "state": key, "state_label": label,
                # End (#5041): who asked it to stop and why, and End now for a process on this computer.
                "stop_by": a.get("stop_by"), "stop_reason": a.get("stop_reason"), "pid": a.get("pid"),
                # A session that ran no maxpm command records no process: End now ends the program in its own tmux
                # pane while one runs there (#5278: Aider).
                "can_kill": bool(a.get("can_kill")) or (
                    not a.get("pid") and a.get("state") != "gone" and bool((terminals or {}).get(a["name"]))
                    and (terminals or {}).get(a["name"]) not in (ended_panes or ())),
                "session_url": a.get("session_url"), "terminal": bool((terminals or {}).get(a["name"])),
                "transcript": kind if path else None, "last": tail["entries"],
                "at": tail["entries"][-1]["at"] if tail["entries"] else a.get("last_seen")}
        if a.get("role") == "expert":  # its prompt cache: what the next message costs (#5023)
            card["cache"] = cache_state(conn, a["name"], found=(kind, path)) if path else None
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


def view(conn, agent, cursor=None, root=None, croot=None, prompt=None, pane=False):
    """The large view of one session: its lines after cursor (None: the last LIMIT of them), the next cursor, and
    its state; with a note and no lines for an agent that has no transcript on this computer. prompt: the prompt its
    tmux pane shows now (server.pane_prompts), and pane: whether that pane was read for it (pane_state)."""
    a = core.agent_status(conn, agent)
    kind, path = transcript(conn, agent, root, croot)
    if not path:  # a session in a pane still gets the text bar
        key, label = pane_state(conn, a, None, prompt, pane)
        return {"agent": agent, "transcript": None, "entries": [], "cursor": None, "turn": None, "state": key,
                "state_label": label, "session_url": a.get("session_url"),
                "note": no_log_note(conn, agent, root, croot)}
    got = entries(kind, path, cursor)
    turn = got["turn"] if got["turn"] or cursor is None else entries(kind, path)["turn"]
    key, label = pane_state(conn, a, turn, prompt, pane)
    older = None
    if cursor is None:  # the first read: where the earlier lines start, for scrolling up (#5120)
        chain = transcript_chain(conn, agent, kind, path)
        got["entries"], older = _older(chain, len(chain) - 1, got["before"], got["entries"])
    return {"agent": agent, "transcript": kind, "provider": provider(a.get("agent_type"), kind),
            "entries": got["entries"], "cursor": got["cursor"], "older": older,
            "refs": item_refs(conn, got["entries"]),
            "turn": turn, "state": key, "state_label": label, "session_url": a.get("session_url"),
            "cache": cache_state(conn, agent, found=(kind, path)) if a.get("role") == "expert" else None,
            "context": context_meter(conn, agent, kind, path)}


def context_meter(conn, agent, kind, path):
    """{"used", "window", "model"} of the session's last model call, for the context meter of its header (#5224);
    None before its first call. window is None when the adapter does not know the model's."""
    c = cache_state(conn, agent, found=(kind, path)) if path else None
    if not c:
        return None
    ad = _adapter(kind)
    return {"used": c["context"], "window": ad.context_window(c["model"]) if ad and c["model"] else None,
            "model": c["model"]}


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


# The prompt cache of a session (#5023). A person talks with an expert for hours; after a pause the model reads the
# whole context again at the cache write price (Claude Code: 2x the input price for its 1-hour cache, 1.25x for the
# 5-minute one) instead of the read price (0.025x to 0.1x). From the chat log (the adapter's chat_cache_line): the
# start of the last model call, the context it read and wrote, and the cache lifetime in force. A cache entry lives
# that long from the start of the call that wrote or read it. A log that does not say how long: cache_ttl then.
_CACHE_SCAN = {}  # path -> (size, mtime, end: the byte after the last complete line read, state)


def _cache_new():
    return {"last": None, "ttl": None, "model": None}


def _cache_scan(kind, path):
    """The cache state of a transcript: read whole the first time, then only what it added since."""
    s = Path(path).stat()
    got = _CACHE_SCAN.get(str(path))
    if got and got[:2] == (s.st_size, s.st_mtime_ns):
        return got[3]
    ad = _adapter(kind)
    fold = ad.chat_cache_line if ad else (lambda raw, st: None)
    if str(path).endswith(".zst"):  # compressed: read as a whole (Codex logs, small)
        st = _cache_new()
        text, _s, end = _read(path, 0)
        for line in text.splitlines():
            fold(line.encode(), st)
    else:
        start, st = (got[2], dict(got[3])) if got and s.st_size >= got[2] else (0, _cache_new())
        end = start
        with open(path, "rb") as f:
            f.seek(start)
            for raw in f:
                if not raw.endswith(b"\n"):
                    break
                end += len(raw)
                fold(raw, st)
    _CACHE_SCAN[str(path)] = (s.st_size, s.st_mtime_ns, end, st)
    return st


def cache_state(conn, name, root=None, croot=None, at=None, found=None):
    """The prompt cache of a session, from its transcript (found: (kind, path) when the caller has it), or None with
    no transcript or no model call yet: state (warm or cold), until and left (seconds) while warm, ttl and where it
    comes from (transcript or setting), context (tokens the next call reads), last_call, model, next_usd and
    next_eq (the cost of the next message's context: at the read price while warm, else at the write price of the
    lifetime in force, in dollars and in input-token equivalents; no dollars for a model of no model_prices family),
    and fresh: cold with a context of expert_fresh_min or more, so a fresh expert from the handoff costs less."""
    kind, path = found or transcript(conn, name, root, croot)
    if not path:
        return None
    try:
        st = _cache_scan(kind, path)
    except (OSError, ValueError, EOFError):
        return None
    last = st["last"]
    start = core._ts(last["start"]) if last else None
    if start is None:
        return None
    context = last["context"]
    ad = _adapter(kind)
    write = bool(((ad.chat if ad else None) or {}).get("cache_write"))  # a cold call pays the write price
    ttl = st["ttl"]
    ttl_from = "transcript" if ttl else "setting"
    ttl = ttl or core.setting(conn, "cache_ttl", agent=name)
    life = core.parse_duration(ttl)
    until = start + life
    t = at or core.now()
    warm = t < until
    model = st["model"] or (conn.execute("SELECT model FROM agents WHERE name=?", (name,)).fetchone() or {"model": None})["model"]
    price = core.model_price(conn, model)
    if price:
        inp, read, w5, w1 = price
        per_m = read if warm else (w1 if life >= core.timedelta(hours=1) else w5) if write else inp
        usd, eq = context * per_m / 1e6, context * per_m / inp
    else:
        usd, eq = None, context * (core.EQ_READ if warm else core.EQ_WRITE_1H if life >= core.timedelta(hours=1)
                                    else core.EQ_WRITE_5M if write else 1)
    fresh_min = core._k_tokens(core.setting(conn, "expert_fresh_min", agent=name))
    return {"state": "warm" if warm else "cold", "until": core.iso(until),
            "left": max(0, int((until - t).total_seconds())), "ttl": ttl, "ttl_from": ttl_from, "context": context,
            "last_call": last["at"], "model": model, "next_usd": round(usd, 4) if usd is not None else None,
            "next_eq": round(eq), "fresh": not warm and context >= fresh_min, "fresh_min": fresh_min}


# The Usage panel of a session (#5224, mark 2026-11-11: 'about usage, context etc'): what the agent's /usage,
# /context, /cost, /status and /stats show, from data the adapter already reads, for every agent. The context of the
# last model call against the model's window (cache_state, the adapter's context_window), the tokens and model calls
# of the session (the adapter's chat_requests, as maxpm usage counts them), and the plan limits where the agent
# writes them to its log (the adapter's chat_limits); where it shows them only on its screen, the panel says so.
USAGE_TAIL = 2 * 1024 * 1024  # bytes of the log's end where the last plan limits are


def session_usage(conn, name, root=None, croot=None):
    """{agent, label, model, context, window, percent, calls, input, output, cache_read, cache_write, eq, limits,
    limits_note, measured}: each number None when the logs do not give it. calls and the tokens sum every session id
    of the agent (a resume or a restart starts a new one); eq is input-token equivalents (core.usage_eq)."""
    a = conn.execute("SELECT name, model, agent_type, native_address FROM agents WHERE name=?", (name,)).fetchone()
    if a is None:
        raise core.RiverError(f"{name} is not a registered agent")
    kind, path = transcript(conn, name, root, croot)
    ad = _adapter(kind) or (_agents.find(a["agent_type"]) if a["agent_type"] else None)
    cache = cache_state(conn, name, root, croot, found=(kind, path)) if path else None
    model = (cache or {}).get("model") or a["model"]
    context = (cache or {}).get("context")
    window = ad.context_window(model) if ad and model else None
    tot = {"calls": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "eq": 0.0}
    measured = False
    sids = [r[0] for r in conn.execute("SELECT session_id FROM agent_sessions WHERE agent=? ORDER BY first_seen",
                                       (name,))]
    if a["native_address"] and a["native_address"] not in sids:
        sids.append(a["native_address"])  # Codex: its thread id is the session's address (CHAT['ref'] address)
    for sid in sids:
        got = core._session_requests(sid, root)
        if not got:
            continue
        measured = True
        for _at, u in got[0].values():
            tot["calls"] += 1
            for k in ("input", "output", "cache_read"):
                tot[k] += u.get(k) or 0
            tot["cache_write"] += (u.get("write_1h") or 0) + (u.get("write_5m") or 0)
            tot["eq"] += core.usage_eq(u)
    if not measured and ad is not None and path and ad.has("chat_usage"):
        # No model calls to count (an adapter with no chat_requests): the tokens that the log itself sums up.
        try:
            text, _start, _end = _read(path, 0)
            u = ad.chat_usage(text.splitlines())
        except (OSError, ValueError, EOFError):
            u = None
        if u and any(u.get(k) for k in ("input", "output", "cache_read", "cache_write")):
            measured = True
            tot.update({k: u.get(k) or 0 for k in ("input", "output", "cache_read", "cache_write")}, calls=None)
            tot["eq"] = core.usage_eq({"input": tot["input"], "cache_read": tot["cache_read"], "write_1h": 0,
                                       "write_5m": tot["cache_write"]})
    limits = None
    if ad is not None and path and ad.has("chat_limits"):
        try:
            text, _start, _end = _read(path, None, USAGE_TAIL)
            limits = ad.chat_limits(text.splitlines())
        except (OSError, ValueError, EOFError):
            limits = None
    label = ad.label if ad else "This agent"
    note = None
    if limits is None:
        note = (f"{label} writes no plan limit to its log: its own screen shows them. Open the Terminal tab and type "
                f"its command there." if ad is None or not ad.has("chat_limits") else
                f"{label} has written no plan limit to this session's log yet.")
    pct = round(100 * context / window, 1) if context is not None and window else None
    return {"agent": name, "label": ad.label if ad else None, "model": model, "context": context, "window": window,
            "percent": pct, "measured": measured,
            **({k: (round(v) if k == "eq" and v is not None else v) for k, v in tot.items()} if measured else
               dict.fromkeys(tot)),
            "limits": limits, "limits_note": note}


# The Artifacts tab (#4620): what the sessions made or named that the side panel can show. Files a session wrote
# (Write, Edit), full file paths, local pages (localhost, 127.0.0.1) and claude.ai artifact links in its own text,
# the person's turns, and its tool calls (a screenshot's file name). Results of tools are left out: a listing of
# files names many that nobody made.
# Each transcript is read whole the first time, line by line, and later only from the end of the last read: a
# session lists every file it named, in a transcript of any size. A shorter tail missed them: pictures that a
# session reads fill its transcript fast (about 1 MB each in a tool result, #4681). 18 MB takes about 30 ms.
ARTIFACT_DAYS = 1  # agents seen this recently, besides the running ones
_ART_CACHE = {}  # path -> (size, mtime, end: the byte after the last complete line read, hits)
# Each adapter's chat_texts gives the texts of a line to look in, and skips tool results and lines with none.
_FILE_RX = None
_URL_RX = None


_ART_KEY_RX = None


def artifact_key(target):
    """One key for each thing (#5000), as the page's targetKey (static/lib.js) for a claude.ai artifact:
    https://claude.ai/artifact/<id> or code/artifact/<uuid>, whatever the scheme, the case of the host, and what
    follows the id. Other targets are their own key."""
    import re
    global _ART_KEY_RX
    _ART_KEY_RX = _ART_KEY_RX or re.compile(r"^https?://(?:www\.)?claude\.ai/((?:code/)?artifact/)([\w-]+)", re.I)
    m = _ART_KEY_RX.match(target or "")
    return f"https://claude.ai/{m.group(1).lower()}{m.group(2)}" if m else target


def _hits_in(text, at, how):
    import re
    global _FILE_RX, _URL_RX
    _FILE_RX = _FILE_RX or re.compile(r"(?<![\w.:/\\-])((?:[a-zA-Z]:[/\\]|\\\\|~/|/)(?:[\w.@~+-]+[/\\])*[\w@~+-][\w.@~+-]*\.(?:png|jpe?g|gif|webp|svg|pdf|html?|md))\b")
    _URL_RX = _URL_RX or re.compile(r"https?://(?:(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?(?:/[^\s)\]>'\"`\\]*)?"
                                    r"|(?i:claude\.ai/(?:code/)?artifact/)[\w-]+)")
    out = [{"target": m, "kind": "file", "how": how, "at": at} for m in _FILE_RX.findall(text or "")]
    for u in _URL_RX.findall(text or ""):
        u = u.rstrip(".,;:")
        out.append({"target": artifact_key(u), "kind": "artifact" if "claude.ai/" in u.lower() else "page", "how": how, "at": at})
    return out


def _artifact_hits(path, kind):
    """The hits of one chat log (kind: its adapter): all of it at first, then the lines it added since the last read.
    A file that became shorter (written again) is read again from its start."""
    st = Path(path).stat()
    got = _ART_CACHE.get(str(path))
    if got and got[:2] == (st.st_size, st.st_mtime_ns):
        return got[3]
    start, hits = (got[2], got[3]) if got and st.st_size >= got[2] else (0, [])
    new, end = _scan_from(path, start, _adapter(kind))
    hits = hits + new
    _ART_CACHE[str(path)] = (st.st_size, st.st_mtime_ns, end, hits)
    return hits


def _scan_from(path, start, ad):
    """(hits, end): the hits in the complete lines from byte start on, and the byte after the last of them. A line
    the CLI still writes waits for the next read."""
    if str(path).endswith(".zst"):  # compressed: read as a whole (Codex logs, small)
        text, _s, end = _read(path, start)
        return [h for line in text.splitlines() for h in _line_hits(line.encode(), ad)], end
    hits, end = [], start
    with open(path, "rb") as f:
        f.seek(start)
        for raw in f:
            if not raw.endswith(b"\n"):
                break
            end += len(raw)
            hits += _line_hits(raw, ad)
    return hits, end


def _line_hits(raw, ad):
    """The hits of one chat log line (bytes): in the texts that its adapter ad gives (chat_texts)."""
    return [h for text, at, how in (ad.chat_texts(raw) if ad else ()) for h in _hits_in(text, at, how)]


def _stamp(at):
    """A transcript time (2026-11-08T12:34:56.789Z) in the form of the holds table (2026-11-08T12:34:56Z)."""
    return (at or "")[:19] + "Z"


def artifacts(conn, terminals=None, allowed=None, root=None, croot=None, limit=300, agent=None, item=None, since=None):
    """[{target, kind (file, page, artifact), how (wrote, named), session, sessions, item, at}], newest first, one per
    target (artifact_key: the same claude.ai artifact under two addresses is one row, #5000); session: the one that
    named it last (or wrote it), sessions: each session that named it, that one first. Rows come
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
    best, by = {}, {}  # by: key -> {session: when it named it last}
    for name in sorted(names):
        kind, path = transcript(conn, name, root, croot)
        if not path:
            continue
        try:
            hits = _artifact_hits(path, kind)
        except (OSError, ValueError, EOFError):
            continue
        if since:
            hits = [h for h in hits if _stamp(h["at"]) >= since]
        if item is not None:
            hits = [h for h in hits if any(s <= _stamp(h["at"]) <= e for s, e in windows[name])]
        for h in hits:
            key = artifact_key(h["target"])
            old = best.get(key)
            last = by.setdefault(key, {})
            last[name] = max(last.get(name) or "", h["at"] or "")
            if old is None or (h["at"] or "") > (old["at"] or "") or (h["how"] == "wrote" and old["how"] != "wrote"):
                best[key] = dict(h, target=key, session=name)
    out = []
    for h in sorted(best.values(), key=lambda h: h["at"] or "", reverse=True):
        if h["kind"] == "file" and allowed is not None and not allowed(h["target"]):
            continue
        others = sorted((n for n in by[h["target"]] if n != h["session"]), key=lambda n: by[h["target"]][n], reverse=True)
        h = dict(h, sessions=[h["session"], *others])
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


def session_commands(conn, agent, root=None, croot=None):
    """The slash commands of a session for the "/" list of its message box (#5186): its agent's built-in commands,
    then the custom ones for the folder it runs in (its transcript's last cwd). An agent with no commands part, or
    a session whose agent MaximizePM does not know, gives an empty list."""
    from . import agents
    a = conn.execute("SELECT agent_type FROM agents WHERE name=?", (agent,)).fetchone()
    if a is None:
        return {"agent": agent, "provider": None, "folder": None, "commands": []}
    kind, path = transcript(conn, agent, root, croot)
    adapter = agents.find(a["agent_type"] or "") or agents.find(kind or "")
    folder = None
    if path:
        try:
            folder = last_cwd(path)
        except (OSError, ValueError, EOFError):
            folder = None
    if not folder and adapter is not None:
        folder = _folder_without_log(conn, agent, adapter, path)
    if adapter is None:
        return {"agent": agent, "provider": None, "folder": folder, "commands": []}
    return {"agent": agent, "provider": adapter.id, "folder": folder, "commands": adapter.read_commands(folder)}


def _folder_without_log(conn, agent, adapter, path):
    """The folder of a session whose chat log names none (#5301: Cursor, Grok, Copilot): the adapter's meta for the
    session's ref (Cursor's meta.json has a cwd), else the working folder of the session's process (as #5262 does)."""
    meta = getattr(adapter.module, "meta", None)
    if meta is not None:
        try:
            refs = [session_ref(conn, agent, adapter, path)] if path else []
            refs += [r["session_id"] for r in conn.execute(
                "SELECT session_id FROM agent_sessions WHERE agent=? ORDER BY first_seen DESC, rowid DESC", (agent,))]
            for ref in refs:
                got = meta(ref)
                if isinstance(got, dict) and got.get("cwd"):
                    return got["cwd"]
        except Exception:  # noqa: BLE001 - an adapter's reader never breaks the list
            pass
    a = conn.execute("SELECT pid, host FROM agents WHERE name=?", (agent,)).fetchone()
    if a and a["pid"] and a["host"] == core.this_host() and core.pid_alive(a["pid"]):
        return core.proc_cwd(a["pid"])
    return None


def _transcript_settings(kind, path):
    """The settings a session's chat log shows last (the adapter's chat_settings): Claude Code puts the model and
    effort on each reply and a line for each permission mode; Codex a turn_context at each turn."""
    ad = _adapter(kind)
    if ad is None or not ad.has("chat_settings"):
        return {}
    text, _s, _e = _read(path, None, CARD_BYTES * 4)
    return ad.chat_settings(text.splitlines())


def record_live(conn, agent, key, value, root=None, croot=None):
    """A setting that the page changed in place (#5288: the Settings dialog's Change now, or /model <value> and
    /effort <value> from the message box). Codex writes a turn_context only at its next turn, and Claude Code names
    the model only on its next reply, so until then the transcript shows the old value. Kept with the transcript's
    file and size now: session_settings shows the change until a line written after that names the setting (the next
    turn says what the session runs, also when the CLI did not take the change)."""
    import json as _json
    _kind, path = transcript(conn, agent, root, croot)
    try:
        size = Path(path).stat().st_size if path else None
    except OSError:
        size = None
    with core.tx(conn):
        r = conn.execute("SELECT live_settings FROM agents WHERE name=?", (agent,)).fetchone()
        if not r:
            return None
        try:
            live = _json.loads(r["live_settings"] or "{}")
        except ValueError:
            live = {}
        live[key] = {"value": value, "path": str(path) if path else None, "size": size}
        conn.execute("UPDATE agents SET live_settings=? WHERE name=?", (_json.dumps(live), agent))
    return live[key]


def _live_settings(conn, agent):
    import json as _json
    r = conn.execute("SELECT live_settings FROM agents WHERE name=?", (agent,)).fetchone()
    try:
        return _json.loads(r["live_settings"] or "{}") if r else {}
    except ValueError:
        return {}


LIVE_READ = 4 * 1024 * 1024  # at most this much of a transcript after a change in place


def _live_holds(kind, path, key, got):
    """Whether a change in place (record_live) still shows: the session's transcript is the same file, and nothing
    written after the change names the setting. Another file (a new session, a compaction into a new file) or
    a line that names it: the transcript says what the session runs."""
    if not got or not path or got.get("path") != str(path) or got.get("size") is None:
        return False
    ad = _adapter(kind)
    if ad is None or not ad.has("chat_settings"):
        return True
    try:
        with open(path, "rb") as f:
            f.seek(got["size"])
            text = f.read(LIVE_READ).decode("utf-8", "replace")
    except OSError:
        return False
    return ad.chat_settings(text.splitlines()).get(key) is None


def session_settings(conn, agent, root=None, croot=None):
    """The Settings dialog of a session (#4625): its provider's settings with the value it runs with now (from its
    transcript, else its command line), where each comes from, whether it changes in place or needs a restart,
    and the default setting (maxpm config) a restart can also change."""
    import re as _re
    a = core.agent_status(conn, agent)
    kind, path = transcript(conn, agent, root, croot)
    prov = provider(a.get("agent_type"), kind)
    ad = core._adapters().find(prov["id"]) if prov else None
    if ad is None:
        raise core.RiverError(f"MaximizePM knows no settings of {agent}'s chat provider")
    seen = {}
    if path:
        try:
            seen = _transcript_settings(kind, path)
        except (OSError, ValueError, EOFError):
            seen = {}
    cmd = a.get("pid_cmd") or pane_command(conn, agent, ad)
    models = core.platform_models(conn, prov["id"]) if prov["id"] in core.launch_platforms() else []
    live = _live_settings(conn, agent)
    out = []
    for spec in ad.session_specs():
        value, source = seen.get(spec["key"]), "session"
        got = live.get(spec["key"])
        if got and _live_holds(kind, path, spec["key"], got):
            # A change the page made in place, with no turn after it yet (#5288): newer than the transcript.
            value, source = got["value"], "changed now"
        if value is None and spec.get("cmd"):
            m = _re.search(spec["cmd"], cmd)
            # A toggle's regex matches its flag (or an alias such as --yolo): that is "on" (#5299).
            value, source = (("on" if spec.get("flag") else m.group(1)), "command line") if m else (None, None)
        if value is None and spec.get("flag") and cmd:
            value, source = ("on" if spec["flag"] in cmd.split() else "off"), "command line"
        if value is None and spec.get("default"):
            value, source = core.setting(conn, spec["default"], agent=agent) or None, "default"
        row = {}
        if spec["kind"] == "model":
            # The models of the session's own provider (#4633), as the launch dialog lists them; the one it runs is
            # chosen, by its ladder name, else as it reports it.
            # picks: the adapter's own models; the dialog lists them first when the list is long (OpenCode, #5242).
            row = {"kind": "choice", "choices": [m["name"] for m in models], "reported": value,
                   "notes": {m["name"]: f"{m['id']}{' · ' + m['note'] if m['note'] else ''}" for m in models},
                   "picks": [m["name"] for m in models if m.get("pick")]}
            value = core.model_name_of(models, value) or value
        out.append({"key": spec["key"], "label": spec["label"], "kind": spec["kind"], "choices": spec.get("choices"),
                    **row,
                    "value": value, "source": source, "live": bool(spec.get("live")), "note": spec.get("note"),
                    "fixed": bool(spec.get("fixed")),
                    "default_key": spec.get("default"),
                    "default_value": core.setting(conn, spec["default"]) if spec.get("default") else None})
    return {"agent": agent, "provider": prov, "settings": out, "transcript": kind if path else None}


def pane_command(conn, agent, ad):
    """The command line of the agent's CLI in the agent's tmux pane, or "" (#5253). For a session whose process
    maxpm does not record: Hermes runs the tools of every session in one gateway with no terminal, so maxpm keeps no
    process of a Hermes session (#5247), and the Settings dialog read no model or effort. The line on the pane's
    terminal that the adapter's DETECT finds (process_args, else process as the program or script name)."""
    from . import server
    try:
        pane = server.agent_terminals(conn).get(agent)
        tty = pane and next((p["tty"] for p in server._tmux_panes(everywhere=True) if p["pane"] == pane), None)
    except core.RiverError:
        return ""
    if not tty:
        return ""
    lines, d = server._tty_lines(tty), ad.detect or {}
    if d.get("process_args"):
        hit = next((x for x in lines if re.search(d["process_args"], x)), None)
        if hit:
            return hit
    exe = d.get("process") or ad.describe.get("exe")
    return next((x for x in lines if exe and exe in server._programs([x])), "")


def setting_spec(provider_id, key):
    """One spec of the Settings dialog of a session whose provider is provider_id (an agent's id or alias), from its
    adapter (#5169)."""
    ad = core._adapters().find(provider_id)
    spec = next((x for x in ad.session_specs() if x["key"] == key), None) if ad else None
    if spec is None:
        raise core.RiverError(f"{ad.label if ad else provider_id} has no setting {key}")
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
