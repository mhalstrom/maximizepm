"""How worker agents use their inboxes (#1596): each message to a worker (an agent that is not a manager and not a
person), matched to the first place its text shows in the transcripts of that worker's Claude Code sessions. It
prints the counts by kind and sender, how each message was read and how long it waited, what a waiting worker did
after `maxpm wait` returned for messages, the cost of a read, and the broadcast notes that came too late (a push or
a build that started between the note and its read). Reads the queue read-only. Run from the repository:
python3 current_project/research/worker_inbox.py [since [until]] (ISO times, UTC)."""
import bisect
import collections
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from river import core  # noqa: E402

since = datetime.fromisoformat(sys.argv[1] if len(sys.argv) > 1 else "2026-10-06T21:34:32+00:00")
until = datetime.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else datetime.now(timezone.utc)
c = sqlite3.connect(f"file:{core.db_path()}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
root = core.transcripts_root()
humans = {r[0] for r in c.execute("SELECT name FROM agents WHERE kind='human'")}
# A session belongs to the agent that used it first (as in manager_cost.py).
owner = {}
for agent, sid in c.execute("SELECT agent, session_id FROM agent_sessions ORDER BY first_seen, rowid"):
    owner.setdefault(sid, agent)
sessions = collections.defaultdict(list)
for sid, agent in owner.items():
    sessions[agent].append(sid)

MAXPM = re.compile(r"\b(?:maxpm|river)\b(?:\s+(?:--as|--project)\s+\S+|\s+-q|\s+--json)*\s+([a-z-]+)((?:\s+--?[a-z-]+)*)")
PUSH = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?push\b(?!-)")
BUILD = re.compile(r"\b(cargo\s+(build|test|nextest|clippy|check|run)|make\s+\S*(check|test|build)|check-all|vitest|playwright test)")


def text_of(content):
    """The text of a transcript message, with the text of its tool results."""
    if isinstance(content, str):
        return content
    out = []
    for x in content or []:
        if isinstance(x, dict) and x.get("type") == "text":
            out.append(x.get("text", ""))
        elif isinstance(x, dict) and x.get("type") == "tool_result":
            out.append(text_of(x.get("content")))
    return "\n".join(out)


def commands(cmd):
    """The maxpm commands in one shell command, as words: 'inbox', 'inbox --wait', 'go', ..."""
    out = []
    cmd = cmd.split("<<")[0]  # not the text of a here-document
    for m in MAXPM.finditer(cmd):
        out.append("inbox --wait" if m.group(1) == "inbox" and "--wait" in cmd[m.start():m.start() + 200]
                   else "inbox --peek" if m.group(1) == "inbox" and "--peek" in cmd[m.start():m.start() + 200]
                   else m.group(1))
    return out


def read_session(sid):
    """The events of one session in file order: ("use", time, id, tool, command, background), ("res", time, tool
    use id, text), ("usr", time, text) for a prompt or a notification, ("txt", time, text) for text of the agent, and ("req", time, eq, context) once for
    each API request."""
    ev, seen = [], set()
    for path in sorted(Path(root).glob(f"*/{sid}.jsonl")):
        for line in path.read_text(errors="replace").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            t = core._ts(r.get("timestamp") or "")
            if t is None or r.get("isSidechain"):
                continue
            msg = r.get("message") or {}
            if r.get("type") == "assistant":
                u = msg.get("usage")
                if u and msg.get("id") not in seen:
                    seen.add(msg.get("id"))
                    ctx = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (
                        u.get("cache_creation_input_tokens") or 0)
                    ev.append(("req", t, core.usage_eq(u), ctx))
                for x in msg.get("content") or []:
                    if isinstance(x, dict) and x.get("type") == "text" and x.get("text", "").strip():
                        ev.append(("txt", t, x["text"]))
                    if isinstance(x, dict) and x.get("type") == "tool_use":
                        i = x.get("input") or {}
                        ev.append(("use", t, x.get("id"), x.get("name"),
                                   i.get("command") or i.get("file_path") or i.get("task_id") or "",
                                   bool(i.get("run_in_background"))))
            elif r.get("type") == "user":
                content = msg.get("content")
                if isinstance(content, list) and any(isinstance(x, dict) and x.get("type") == "tool_result"
                                                     for x in content):
                    for x in content:
                        if isinstance(x, dict) and x.get("type") == "tool_result":
                            ev.append(("res", t, x.get("tool_use_id"), text_of(x.get("content"))))
                else:
                    ev.append(("usr", t, text_of(content)))
            elif r.get("type") == "attachment" and (r.get("attachment") or {}).get("type") == "queued_command":
                p = r["attachment"].get("prompt")
                ev.append(("usr", t, p if isinstance(p, str) else text_of(p)))
    return ev


def source(a):
    return ("MaximizePM" if a == "maxpm" else "the manager" if a.startswith("manager-") else "a person"
            if a in humans else "another worker")


def shape(m):
    """The kind of a message, with the broadcasts of the manager apart (a note or an alert that it sent with the
    same first words to three or more workers within two minutes)."""
    if m["from_agent"] == "maxpm" and m["kind"] == "alert":
        return "push alert from MaximizePM"
    if m["kind"] == "alert" and " pushed #" in m["body"][:60]:
        return "push alert"
    if m["kind"] == "alert" and m["body"].startswith("STOP requested"):
        return "stop request alert"
    return m["kind"]


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else 0


def mins(x):
    return f"{x:.1f}m" if x < 90 else f"{x / 60:.1f}h"


msgs = [dict(r) for r in c.execute("SELECT * FROM messages WHERE created_at>=? AND created_at<? ORDER BY id",
                                   (core.iso(since), core.iso(until)))]
msgs = [m for m in msgs if m["to_agent"] and not m["to_agent"].startswith("manager-") and m["to_agent"] not in humans]
# A broadcast: the same sender and the same first 30 characters to three or more workers within two minutes.
groups = collections.defaultdict(list)
for m in msgs:
    groups[(m["from_agent"], m["kind"], m["body"][:30], m["created_at"][:15])].append(m)
for g in groups.values():
    for m in g:
        m["broadcast"] = len(g) >= 3
        m["group"] = len(g)

events, index = {}, {}
for agent in {m["to_agent"] for m in msgs}:
    ev = []
    for sid in sessions.get(agent, []):
        ev += read_session(sid)
    ev.sort(key=lambda e: e[1])
    events[agent] = ev
    index[agent] = {e[2]: n for n, e in enumerate(ev) if e[0] == "use"}

for m in msgs:
    ev = events[m["to_agent"]]
    m["created"] = core._ts(m["created_at"])
    m["read"] = core._ts(m["read_at"]) if m["read_at"] else None
    m["has_transcript"] = bool(ev)
    m["alive"] = any(e[0] == "req" and e[1] >= m["created"] for e in ev)  # a request after the message came
    pat = re.compile(rf"#{m['id']} {m['kind']} from ")
    m["seen"] = m["via"] = m["at"] = None
    for n, e in enumerate(ev):
        if e[0] in ("res", "usr") and e[1] >= m["created"] - timedelta(seconds=5) and pat.search(e[-1]):
            m["seen"], m["at"] = e[1], n
            use = ev[index[m["to_agent"]][e[2]]] if e[0] == "res" and e[2] in index[m["to_agent"]] else None
            if use is None:
                m["via"] = "a notification of a background command"
            elif use[3] != "Bash":
                m["via"] = "the output file of a background command"
            else:
                cs = commands(use[4])
                m["via"] = next((k for k in ("inbox --wait", "inbox", "inbox --peek", "thread", "wait", "go", "accept",
                                             "decline", "show") if k in cs), "another command")
                if m["via"] == "inbox --wait":
                    m["via"] = "inbox --wait in the foreground"
            break
    if m["seen"] is None:
        m["via"] = ("never read" if m["read"] is None else "closed by a claim, never shown"
                    if m["state"] in ("accepted", "declined") else "marked read, text not in the transcript")
    # The first command result after the message that shows the status line with the unread count.
    m["footer"] = next((e[1] for e in ev if e[0] == "res" and e[1] >= m["created"]
                        and re.search(r"inbox: \d+ unread", e[3])), None)

print(f"Messages to workers, {since:%Y-%m-%d %H:%M} to {until:%Y-%m-%d %H:%M} UTC: {len(msgs)} messages to "
      f"{len({m['to_agent'] for m in msgs})} workers; {sum(m['has_transcript'] for m in msgs)} to a worker with a "
      f"transcript")

print("\n| Kind | From | Messages | In a broadcast | Read | Never read | Median wait before the read | 90% |"
      "\n|---|---|---:|---:|---:|---:|---:|---:|")
rows = collections.defaultdict(list)
for m in msgs:
    rows[(shape(m), source(m["from_agent"]))].append(m)
for (k, s), g in sorted(rows.items(), key=lambda x: -len(x[1])):
    w = [(m["read"] - m["created"]).total_seconds() / 60 for m in g if m["read"]]
    print(f"| {k} | {s} | {len(g)} | {sum(m['broadcast'] for m in g)} | {len(w)} | {len(g) - len(w)} | "
          f"{mins(pct(w, .5))} | {mins(pct(w, .9))} |")

b = [g for g in groups.values() if len(g) >= 3]
print(f"\nBroadcasts: {len(b)} sends to {sum(len(g) for g in b)} inboxes (median {pct([len(g) for g in b], .5)} "
      f"workers for each); {sum(len(g) for g in groups.values() if len(g) < 3)} messages to one or two workers")

print("\n| How the worker read it | Messages | Median wait | 90% | Longest |\n|---|---:|---:|---:|---:|")
via = collections.defaultdict(list)
for m in msgs:
    if m["has_transcript"]:
        via[m["via"]].append(m)
for k, g in sorted(via.items(), key=lambda x: -len(x[1])):
    w = [((m["seen"] or m["read"]) - m["created"]).total_seconds() / 60 for m in g if m["seen"] or m["read"]]
    print(f"| {k} | {len(g)} | {mins(pct(w, .5)) if w else ''} | {mins(pct(w, .9)) if w else ''} | "
          f"{mins(max(w)) if w else ''} |")

never = [m for m in msgs if m["has_transcript"] and m["read"] is None]
print(f"\nNever read: {len(never)}; the worker made no request after the message came: "
      f"{sum(not m['alive'] for m in never)}; the worker worked on and did not read it: {sum(m['alive'] for m in never)}")

# The plain `maxpm inbox`: what told the worker to run it, and how long each step took.
plain = [m for m in msgs if m["via"] == "inbox" and m["footer"] and m["footer"] <= m["seen"]]
a = [(m["footer"] - m["created"]).total_seconds() / 60 for m in plain]
bb = [(m["seen"] - m["footer"]).total_seconds() / 60 for m in plain]
print(f"\nRead with a plain maxpm inbox after the status line showed the count: {len(plain)} messages. From the "
      f"message to the status line: median {mins(pct(a, .5))}, 90% {mins(pct(a, .9))}. From the status line to the "
      f"read: median {mins(pct(bb, .5))}, 90% {mins(pct(bb, .9))}")

# Each maxpm command of a worker, the gaps between them, and the inbox calls.
gaps, calls, empty, waits_fg, waits_bg, blocked, wait_mail, wait_all = [], 0, 0, 0, 0, 0.0, 0, 0
after_wake = collections.Counter()
read_cost, read_ctx = [], []
for agent, ev in events.items():
    last = None
    res_of = {e[2]: e for e in ev if e[0] == "res"}
    for n, e in enumerate(ev):
        if e[0] != "use" or e[3] != "Bash" or not since <= e[1] < until:
            continue
        cs = commands(e[4])
        if not cs:
            continue
        if last is not None:
            gaps.append((e[1] - last).total_seconds() / 60)
        r = res_of.get(e[2])
        last = r[1] if r else e[1]  # a gap runs from the end of one maxpm command to the start of the next
        if "inbox" in cs or "inbox --peek" in cs:
            calls += 1
            empty += bool(r and "(inbox empty)" in r[3])
            if r and "inbox" in cs:
                nxt = next((x for x in ev[n:] if x[0] == "req" and x[1] >= r[1]), None)
                if nxt:
                    read_cost.append(nxt[2])
                    read_ctx.append(nxt[3])
        if "inbox --wait" in cs:
            if e[5]:
                waits_bg += 1
            else:
                waits_fg += 1
                blocked += (r[1] - e[1]).total_seconds() / 60 if r else 0
        if "wait" in cs and r:
            wait_all += 1
            if "you have messages" in r[3]:
                wait_mail += 1
                # What the worker ran after a wait that returned for messages, to the next wait or 12 commands on.
                did = []
                for x in ev[n + 1:]:
                    if x[0] == "usr" or len(did) >= 12:
                        break
                    if x[0] == "use":
                        k = commands(x[4]) if x[3] == "Bash" else []
                        if "wait" in k and did:
                            break
                        did += k or ["(other tool)"]
                acts = set(did) - {"inbox", "inbox --peek", "go", "wait", "who", "status", "show", "heartbeat", "list"}
                after_wake["only read the inbox, ran go, and waited again" if not acts else
                           "released, finished, or ended" if acts & {"release", "done", "unregister"} else
                           "sent a message or an answer" if acts & {"note", "send", "answer", "alert", "ask", "accept",
                                                                    "decline"} else "ran other commands"] += 1

print(f"\nTime between two maxpm commands of a worker (the wait for 'the next command'): {len(gaps)} gaps, median "
      f"{mins(pct(gaps, .5))}, 75% {mins(pct(gaps, .75))}, 90% {mins(pct(gaps, .9))}; over 10m: "
      f"{sum(g > 10 for g in gaps)}; over 30m: {sum(g > 30 for g in gaps)}")
print(f"maxpm inbox calls: {calls}; with '(inbox empty)': {empty}")
print(f"The request after a maxpm inbox result: median {pct(read_cost, .5) / 1000:.0f}k eq, mean "
      f"{sum(read_cost) / max(1, len(read_cost)) / 1000:.0f}k eq, median context {pct(read_ctx, .5) / 1000:.0f}k")
print(f"maxpm inbox --wait: {waits_bg} in the background, {waits_fg} in the foreground "
      f"({blocked / 60:.1f} hours blocked in the foreground)")
print(f"maxpm wait results: {wait_all}; returned for messages: {wait_mail}")
print("\n| After a maxpm wait that returned for messages, the worker | Times |\n|---|---:|")
for k, v in after_wake.most_common():
    print(f"| {k} | {v} |")

# Broadcast notes that tell workers to stop pushes or builds: what the worker started before it read the note.
print("\n| Broadcast | Inboxes | Read | Median wait | Read after more than 10m | The worker started the named "
      "command between the note and its read |\n|---|---:|---:|---:|---:|---:|")
RULES = (("push freeze starts", re.compile(r"push freeze|PUSH FREEZE", re.I), re.compile(r"freeze ended|no push freeze"),
          PUSH),
         ("builds must stop (disk, load, gate)", re.compile(r"STOP ALL BUILDS|DISK STOP|start no (new )?cargo|"
                                                            r"gate starts|run no cargo", re.I), None, BUILD))
for name, want, skip, cmd in RULES:
    g = [m for m in msgs if m["broadcast"] and want.search(m["body"][:200])
         and not (skip and skip.search(m["body"][:60])) and m["has_transcript"]]
    w = [((m["seen"] or m["read"]) - m["created"]).total_seconds() / 60 for m in g if m["seen"] or m["read"]]
    late = 0
    for m in g:
        end = m["seen"] or m["read"] or until
        late += any(e[0] == "use" and e[3] == "Bash" and m["created"] < e[1] < end and cmd.search(e[4])
                    for e in events[m["to_agent"]])
    print(f"| {name} | {len(g)} | {len(w)} | {mins(pct(w, .5))} | {sum(x > 10 for x in w)} | {late} |")

# What the worker ran in its next six tool calls after it read a message.
QUEUE_ACTS = {"send", "note", "alert", "ask", "answer", "accept", "decline", "release", "give", "drop", "offer",
              "split", "reopen", "queue", "blocked", "claim"}
LOOK = {"wait", "inbox", "inbox --wait", "inbox --peek", "heartbeat", "go", "who", "show", "status", "thread", "list"}


def group_of(m):
    k = shape(m)
    if k in ("push alert", "push alert from MaximizePM", "stop request alert"):
        return k
    s = source(m["from_agent"])
    if k in ("note", "alert"):
        return f"{k} from {s}, {'broadcast' if m['broadcast'] else 'to one worker'}" if s == "the manager" else f"{k} from {s}"
    return k


after = collections.defaultdict(collections.Counter)
for m in msgs:
    if m["at"] is None:
        continue
    did, other = set(), 0
    n = 0
    for x in events[m["to_agent"]][m["at"] + 1:]:
        if x[0] == "usr" or n >= 6:
            break
        if x[0] == "use":
            n += 1
            k = commands(x[4]) if x[3] == "Bash" else []
            did |= set(k)
            other += not k or bool(set(k) - LOOK - QUEUE_ACTS - {"done", "add", "edit"}) or PUSH.search(x[4]) is not None
    after[group_of(m)]["sent a message or changed the queue" if did & QUEUE_ACTS else
                       "went on with other tools" if other else "only waited or looked for work"] += 1
print("\n| Message | Read in a transcript | Then sent a message or changed the queue | Then only waited or looked "
      "for work | Then went on with other tools |\n|---|---:|---:|---:|---:|")
for k, v in sorted(after.items(), key=lambda x: -sum(x[1].values())):
    print(f"| {k} | {sum(v.values())} | {v['sent a message or changed the queue']} | "
          f"{v['only waited or looked for work']} | {v['went on with other tools']} |")

# A maxpm wait that returned for messages and changed nothing: its cost, and the messages that caused it.
cost, woke, quiet = [], collections.Counter(), 0
for agent, ev in events.items():
    res_of = {e[2]: e for e in ev if e[0] == "res"}
    mine = [(m["seen"], m) for m in msgs if m["to_agent"] == agent and m["seen"]]
    for n, e in enumerate(ev):
        r = res_of.get(e[2]) if e[0] == "use" and e[3] == "Bash" and "wait" in commands(e[4]) else None
        if not r or "you have messages" not in r[3] or not since <= e[1] < until:
            continue
        did, eq, end = [], 0.0, r[1]
        for x in ev[n + 1:]:
            if x[0] == "usr" or len(did) >= 12:
                break
            if x[0] == "req" and x[1] >= r[1]:
                eq += x[2]
            if x[0] == "use":
                k = commands(x[4]) if x[3] == "Bash" else []
                if "wait" in k and did:
                    end = x[1]
                    break
                did += k or ["(other tool)"]
        if set(did) - {"inbox", "inbox --peek", "go", "wait", "who", "status", "show", "heartbeat", "list"}:
            continue
        quiet += 1
        cost.append(eq)
        for t, m in mine:
            if r[1] <= t <= end:
                woke[group_of(m)] += 1
print(f"\nmaxpm wait returned for messages and the worker only read them and waited again: {quiet} times, "
      f"{sum(cost) / 1e6:.1f}M eq, median {pct(cost, .5) / 1000:.0f}k eq for each. The messages read in them:")
for k, v in woke.most_common():
    print(f"  {k}: {v}")

# A foreground maxpm inbox --wait: how it ended.
ended = collections.Counter()
users = set()
for agent, ev in events.items():
    res_of = {e[2]: e for e in ev if e[0] == "res"}
    for e in ev:
        if e[0] == "use" and e[3] == "Bash" and not e[5] and "inbox --wait" in commands(e[4]) and since <= e[1] < until:
            r = res_of.get(e[2])
            users.add(agent)
            ended["new messages" if r and "NEW MESSAGES" in r[3] else "no message (the time limit)"] += 1
print(f"\nForeground maxpm inbox --wait, by {len(users)} workers: " + ", ".join(f"{k} {v}" for k, v in ended.items()))

# A start note read when its end note was there already (the worker read both in one read).
START = re.compile(r"push freeze|PUSH FREEZE|STOP ALL BUILDS|DISK STOP|start no (new )?cargo|gate starts", re.I)
END = re.compile(r"freeze ended|gate ended|GO \(manager\)|GO again|NORMAL OPERATIONS|no push freeze", re.I)
stale = total = 0
for m in msgs:
    if not (m["broadcast"] and START.search(m["body"][:200]) and not END.search(m["body"][:60])):
        continue
    t = m["seen"] or m["read"]
    if not t:
        continue
    total += 1
    stale += any(o["to_agent"] == m["to_agent"] and o["from_agent"] == m["from_agent"] and END.search(o["body"][:60])
                 and m["created"] < o["created"] <= t for o in msgs)
print(f"\nStart notes (a freeze, a build stop) read: {total}; read when the end note was there already: {stale}")

# A push between a freeze note and its read: did the hook refuse it?
late = refused = 0
for m in msgs:
    if not (m["broadcast"] and re.search(r"push freeze", m["body"][:200], re.I)
            and not re.search(r"freeze ended|no push freeze", m["body"][:60])):
        continue
    end = m["seen"] or m["read"] or until
    ev = events[m["to_agent"]]
    res_of = {e[2]: e for e in ev if e[0] == "res"}
    for e in ev:
        if e[0] == "use" and e[3] == "Bash" and m["created"] < e[1] < end and PUSH.search(e[4]):
            late += 1
            r = res_of.get(e[2])
            refused += bool(r and re.search(r"freeze|refus|rejected|hook", r[3], re.I))
            break
print(f"Push freeze notes with a push between the note and its read: {late}; the result of that push names a freeze, "
      f"a hook, or a refusal: {refused}")

# The go briefing says "Messages for you", and the inbox call that follows is empty.
said = said_empty = 0
for agent, ev in events.items():
    res_of = {e[2]: e for e in ev if e[0] == "res"}
    uses = [e for e in ev if e[0] == "use" and e[3] == "Bash" and commands(e[4])]
    for n, e in enumerate(uses):
        r = res_of.get(e[2])
        if "go" in commands(e[4]) and r and "Messages for you: inbox:" in r[3] and since <= e[1] < until:
            said += 1
            nxt = next((x for x in uses[n + 1:n + 4] if "inbox" in commands(x[4])), None)
            said_empty += bool(nxt and res_of.get(nxt[2]) and "(inbox empty)" in res_of[nxt[2]][3])
print(f"go briefings with 'Messages for you': {said}; the inbox call after it was empty: {said_empty}")

# Messages in one read, and messages for each worker.
reads = collections.Counter((m["to_agent"], m["at"]) for m in msgs if m["at"] is not None)
sizes = sorted(reads.values())
per = sorted(collections.Counter(m["to_agent"] for m in msgs).values())
print(f"Reads: {len(sizes)}; with one message: {sum(s == 1 for s in sizes)}; with three or more: "
      f"{sum(s >= 3 for s in sizes)}; largest: {sizes[-1] if sizes else 0}")
print(f"Messages for each worker: median {pct(per, .5)}, 90% {pct(per, .9)}, most {per[-1]}")
old = [m for m in msgs if (m["seen"] or m["read"]) and ((m["seen"] or m["read"]) - m["created"]).total_seconds() > 1800]
print(f"Read more than 30m after they came: {len(old)}: " + ", ".join(
    f"{k} {v}" for k, v in collections.Counter(group_of(m) for m in old).most_common()))

# The cost of the request that follows an empty inbox call and a foreground wait that ended at its time limit.
total = sum(e[2] for ev in events.values() for e in ev if e[0] == "req" and since <= e[1] < until)
empty_eq = limit_eq = all_eq = 0.0
for agent, ev in events.items():
    res_of = {e[2]: e for e in ev if e[0] == "res"}
    for n, e in enumerate(ev):
        if e[0] != "use" or e[3] != "Bash" or not since <= e[1] < until:
            continue
        cs, r = commands(e[4]), res_of.get(e[2])
        nxt = next((x for x in ev[n:] if x[0] == "req" and x[1] >= r[1]), None) if r else None
        if not nxt:
            continue
        if "inbox" in cs or "inbox --peek" in cs:
            all_eq += nxt[2]
            empty_eq += nxt[2] if "(inbox empty)" in r[3] else 0
        elif "inbox --wait" in cs and not e[5] and "NEW MESSAGES" not in r[3]:
            limit_eq += nxt[2]
print(f"\nAll requests of these workers: {total / 1e6:.0f}M eq. The request after each maxpm inbox call: "
      f"{all_eq / 1e6:.1f}M eq; after an empty one: {empty_eq / 1e6:.1f}M eq; after a foreground inbox --wait that "
      f"ended at its time limit: {limit_eq / 1e6:.1f}M eq")
