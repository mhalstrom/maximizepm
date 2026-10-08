"""What wakes the manager (#1582): each end of a background `maxpm manage --watch` in the manager sessions'
transcripts, by what the watch printed (new messages, new findings, the time limit), the kinds of messages and
findings, and two simulations of rules that let the less urgent ones wait: one wait for all of them, and a wait
for each of two levels (normal: a note or another notice; low: a ship request notice, a waiting or human
finding). Reads the queue read-only. Run from the
repository: python3 current_project/research/manager_wakes.py [since [until]] (ISO times, UTC)."""
import collections
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from river import core  # noqa: E402

since = datetime.fromisoformat(sys.argv[1] if len(sys.argv) > 1 else "2026-10-06T00:00:00+00:00")
until = datetime.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else datetime.now(timezone.utc)
c = sqlite3.connect(f"file:{core.db_path()}?mode=ro", uri=True)
root = core.transcripts_root()
# A session belongs to the agent that used it first (as in manager_cost.py).
owner = {}
for agent, sid in c.execute("SELECT agent, session_id FROM agent_sessions ORDER BY first_seen, rowid"):
    owner.setdefault(sid, agent)
URGENT_FINDINGS = {"question", "uncovered", "stuck", "lost", "target", "unconnected"}  # not: waiting, human
WATCH = re.compile(r"(?:^|\t|→)(CHANGED: [^\n]*|NEW MESSAGES \(\d+\)[^\n]*|NOTHING NEW[^\n]*|STOP REQUESTED[^\n]*)", re.M)
MESSAGE = re.compile(r"#\d+ (alert|question|note|notice|answer|offer) from \S+")


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


def is_watch(summary):
    return "atch" in summary and any(w in summary for w in ("queue", "manage", "finding"))


# In file order: ("end", time, summary) for the end of a background command, and ("out", time, first line, text)
# for a tool result that holds the output of a watch.
events = []
for sid, agent in owner.items():
    if not agent.startswith("manager-"):
        continue
    for path in sorted(Path(root).glob(f"*/{sid}.jsonl")):
        for line in path.read_text(errors="replace").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            t = core._ts(r.get("timestamp") or "")
            if r.get("type") != "user" or t is None or not since <= t < until:
                continue
            content = (r.get("message") or {}).get("content")
            text = text_of(content)
            result = isinstance(content, list) and any(isinstance(x, dict) and x.get("type") == "tool_result"
                                                       for x in content)
            if "<task-notification>" in text and not result:
                m = re.search(r"<summary>(.*?)</summary>", text, re.S)
                events.append(("end", t, m.group(1) if m else ""))
            elif result and WATCH.search(text):
                events.append(("out", t, WATCH.search(text).group(1), text))

ends = collections.Counter("watch" if is_watch(e[2]) else "other" for e in events if e[0] == "end")
results, finding_kinds, message_kinds, alone = (collections.Counter() for _ in range(4))
wakes = []  # (time, urgent, time limit, only ship request notices, only findings, has normal, has low)
for n, e in enumerate(events):
    if e[0] != "end" or not is_watch(e[2]):
        continue
    out = events[n + 1] if n + 1 < len(events) and events[n + 1][0] == "out" else None
    head = out[2].split("; resolved:")[0] if out else ""
    body = out[3] if out else ""
    kinds = set()
    if head.startswith("CHANGED"):
        kinds = {x.strip().split(":")[0] for x in head[len("CHANGED:"):].split(",") if x.strip()}
        for k in kinds:
            finding_kinds[k] += 1
    results["a finding" if kinds else "messages" if head.startswith("NEW MESSAGES") else "the time limit"
            if head.startswith("NOTHING") else "a stop request" if head.startswith("STOP") else "not read"] += 1
    lines = body.splitlines()
    got = []
    for j, text in enumerate(lines):
        m = MESSAGE.search(text)
        if m:
            nxt = lines[j + 1] if j + 1 < len(lines) else ""
            got.append("ship request notice" if m.group(1) == "notice" and "ship request" in nxt else m.group(1))
    for k in got:
        message_kinds[k] += 1
    if len(got) == 1 and not kinds:
        alone[got[0]] += 1
    wakes.append((e[1], bool(set(got) & {"alert", "question"}) or bool(kinds & URGENT_FINDINGS) or head.startswith("STOP"),
                  head.startswith("NOTHING"), bool(got) and set(got) == {"ship request notice"} and not kinds,
                  bool(kinds) and not got, bool(set(got) & {"note", "notice", "answer", "offer"}),
                  "ship request notice" in got or bool(kinds & {"waiting", "human"})))


def simulate(wait, never=lambda w: False):
    """Wakes when the less urgent events wait `wait` minutes from the first one; `never`: events that wake nothing."""
    count, first = 0, None
    for w in wakes:
        if first is not None and (w[0] - first).total_seconds() / 60 >= wait:
            count, first = count + 1, None
        if never(w) and not w[1]:
            continue
        if w[1] or w[2]:
            count, first = count + 1, None
        elif first is None:
            first = w[0]
    return count + (first is not None)


def levels(normal, low):
    """Wakes when a normal event waits `normal` minutes and a low one `low` minutes; a wake brings all that wait."""
    count, due = 0, None
    for w in wakes:
        if due is not None and w[0] >= due:
            count, due = count + 1, None
        if w[1] or w[2]:
            count, due = count + 1, None
            continue
        ends = [w[0] + timedelta(minutes=m) for m, has in ((normal, w[5]), (low, w[6])) if has]
        if ends:
            due = min(ends + ([due] if due is not None else []))
    return count + (due is not None)


hours = (min(until, datetime.now(timezone.utc)) - since).total_seconds() / 3600
print(f"Background commands that ended: {ends['watch']} watches, {ends['other']} other commands, in {hours:.1f} hours")
print("\n| The watch returned for | Wakes |\n|---|---:|")
for k, v in results.most_common():
    print(f"| {k} | {v} |")
print("\n| Findings in the wakes | Wakes |\n|---|---:|")
for k, v in finding_kinds.most_common():
    print(f"| {k} | {v} |")
print("\n| Messages in the wakes | Messages | Wakes with only this one message |\n|---|---:|---:|")
for k, v in message_kinds.most_common():
    print(f"| {k} | {v} | {alone[k]} |")
gaps = sorted((b[0] - a[0]).total_seconds() / 60 for a, b in zip(wakes, wakes[1:]))
if gaps:
    print(f"\nTime between two wakes: median {gaps[len(gaps) // 2]:.1f}m; under 2m: {sum(g < 2 for g in gaps)}; "
          f"under 5m: {sum(g < 5 for g in gaps)}; under 10m: {sum(g < 10 for g in gaps)}")
print(f"\nUrgent wakes (an alert, a question, a stop, or a finding other than waiting and human): "
      f"{sum(w[1] for w in wakes)}")
print("\n| The less urgent wait | Wakes | Ship request notices also never wake alone | Waiting and human findings "
      "also never wake alone |\n|---|---:|---:|---:|")
for wait in (0, 2, 5, 10, 15, 30):
    print(f"| {wait}m | {simulate(wait)} | {simulate(wait, lambda w: w[3])} "
          f"| {simulate(wait, lambda w: w[3] or (w[4] and not w[1]))} |")
print("\n| Wait of the normal level | Wait of the low level | Wakes |\n|---|---|---:|")
for normal, low in ((2, 30), (5, 30), (10, 30), (15, 30), (30, 30), (5, 60), (10, 60), (15, 60), (10, 120)):
    print(f"| {normal}m | {low}m | {levels(normal, low)} |")
n = collections.Counter("urgent" if w[1] else "the time limit" if w[2] else "normal" if w[5] else "low" for w in wakes)
print("\nHighest level in each wake: " + ", ".join(f"{k} {v}" for k, v in n.most_common()))
