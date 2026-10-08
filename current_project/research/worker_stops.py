"""What stops a worker (#1597): each time a worker session stopped or paused an item it held, by cause.

Four sources, all read-only:
  1. The queue: how each hold of an item by a worker ended (done, a release with new prerequisites, a hold
     for a prerequisite, maxpm blocked, a release with a note, a lease that ran out, a release by maxpm serve
     because the session stood at its prompt). The cause of a release or a block comes from its text.
  2. The transcripts: each time a worker ended its turn while it held an item, and how long it stood there.
     A turn that ends to wait for the session's own background command or subagent is counted apart.
  3. The questions of workers (to a person, a manager, another agent): the time to the answer, and whether
     the asker held the item and made tool calls while it waited.
  4. The refusals of the auto mode classifier in the transcripts, and what the worker did next.

Run from the repository: python3 current_project/research/worker_stops.py [since [until]] [--detail]
(ISO times, UTC). --detail prints each stop, for a check of the cause rules by hand."""
import collections
import json
import re
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from river import core  # noqa: E402

argv = [a for a in sys.argv[1:] if not a.startswith("--")]
DETAIL = "--detail" in sys.argv
SINCE = datetime.fromisoformat(argv[0] if argv else "2026-10-06T00:00:00+00:00")
UNTIL = datetime.fromisoformat(argv[1]) if len(argv) > 1 else datetime.now(timezone.utc)
MIN_WAIT = 5  # minutes: a shorter stand at the prompt is not counted
c = sqlite3.connect(f"file:{core.db_path()}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
root = core.transcripts_root()


def ts(s):
    return core._ts(s) if isinstance(s, str) else None


def z(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def mins(a, b):
    return (b - a).total_seconds() / 60


def spread(xs):
    """'n; median; longest; sum' of a list of minutes."""
    if not xs:
        return "0"
    med = statistics.median(xs)
    med = f"{med * 60:.0f}s" if med < 2 else f"{med:.0f}m"
    return f"{len(xs)}; median {med}; longest {max(xs):.0f}m; sum {sum(xs) / 60:.1f}h"


def is_worker(agent):
    return not agent.startswith("manager-") and agent not in people


people = {r["name"] for r in c.execute("SELECT name FROM agents WHERE kind='human'")}
items = {r["id"]: r for r in c.execute("SELECT * FROM items")}
holds = [dict(h, s=ts(h["started_at"]), e=ts(h["ended_at"])) for h in c.execute("SELECT * FROM holds ORDER BY id")]
by_agent, by_item = collections.defaultdict(list), collections.defaultdict(list)
for h in holds:
    by_agent[h["agent"]].append(h)
    by_item[h["item_id"]].append(h)
dependents = collections.defaultdict(list)
for r in c.execute("SELECT item_id, blocked_by FROM deps WHERE kind='blocks'"):
    dependents[r["blocked_by"]].append(r["item_id"])


def waited_on(item_id, at):
    """The items that waited on item_id at that time: its direct dependents that were open then."""
    out = []
    for d in dependents[item_id]:
        i = items.get(d)
        if i and ts(i["created_at"]) <= at and (not i["closed_at"] or ts(i["closed_at"]) > at):
            out.append(d)
    return out


def waiting_text(rows):
    """rows: (item ids, time) of each stop. Work items and releases (a review or a deploy item) that waited."""
    work, rel, any_work, any_rel = set(), set(), 0, 0
    for ids, at in rows:
        deps = {d for i in ids for d in waited_on(i, at)}
        w = {d for d in deps if items[d]["kind"] not in ("review", "deploy", "fixes")}
        work |= w
        rel |= deps - w
        any_work += bool(w)
        any_rel += bool(deps - w)
    return (f"a work item waited on it in {any_work} (work items: {len(work)}); "
            f"a release waited on it in {any_rel} (review or deploy items: {len(rel)})")


def stood_still(item_id, at):
    """Minutes from a stop until the next agent took the item (or it closed); until now when neither."""
    nxt = [h["s"] for h in by_item[item_id] if h["s"] > at]
    i = items[item_id]
    if i["closed_at"] and ts(i["closed_at"]) > at:
        nxt.append(ts(i["closed_at"]))
    return mins(at, min(nxt)) if nxt else mins(at, UNTIL)


# ---- the transcripts: each turn end, tool call, and classifier refusal of the worker sessions ----

owner = {}
for agent, sid in c.execute("SELECT agent, session_id FROM agent_sessions ORDER BY first_seen, rowid"):
    owner.setdefault(sid, agent)


def text_of(content):
    if isinstance(content, str):
        return content
    return "\n".join(x.get("text", "") for x in content or [] if isinstance(x, dict) and x.get("type") == "text")


def result_text(x):
    t = x.get("content")
    return t if isinstance(t, str) else " ".join(y.get("text", "") for y in t or [] if isinstance(y, dict))


def held_at(agent, t):
    return [h for h in by_agent[agent] if h["s"] <= t and (not h["e"] or h["e"] > t)]


# The cause of a stand at the prompt, from the last text of the turn; the first rule that matches.
OWN = "not a stop: it waited for its own background command or subagent"
PROMPT_CAUSES = [
    ("the disk was full: it asked the person to free space",
     r"[Ff]ree (some )?disk|disk (filled|has space)|space is free|cannot run commands|May I delete"),
    ("a refused command: it asked the person for a yes or a permission rule",
     r"permission rule|/permissions|permission prompt|approve the command|[Cc]lassifier|you must approve|a different route"),
    ("a yes of the person before a release to production", r"reply \"deploy\""),
    ("a yes of the person before a push (the person looks at the result first)", r"your \"push\"|say \"push\"|Reply 'push'"),
    ("the push freeze: the work is done, the push waits", r"push freeze is still on"),
    ("a decision or an answer of the person, asked in chat or with maxpm ask",
     r"Your answer|[Ww]aiting on you|wait for your|Reply \"|reply \"|until \w+ answers|Tell me if you want"),
]
OWN_WORK = (r"I (wait|am waiting|continue to wait) for|I continue when|continues?\.$|runs? (now|in the background)|"
            r"in progress|notifies me|I report the result|I push when|[Ww]hen the (gate|worker|reader)|still (runs|reads)|"
            r"renews my lease|keeps my lease|report can reach me|The worker builds")


def prompt_cause(text, woke):
    if woke.startswith("<task-notification>") or "teammate-message" in woke[:120]:
        return OWN
    for name, rx in PROMPT_CAUSES:
        if re.search(rx, text[-900:]):
            return name
    return OWN if re.search(OWN_WORK, text[-400:]) else "other: a report at the prompt, the item still held"


SEND = r"maxpm\b[^|;&\n]*\s(alert|ask|send (question|alert))\s"
WAITS = r"maxpm\b[^|;&\n]*\s(wait|inbox --wait|manage --watch)\b|\bsleep \d|\buntil\b.*\bdo\b"
LOOKS = r"maxpm\b[^|;&\n]*\s(inbox|heartbeat|thread|show|who|status|blockers)\b"


def command_of(x):
    """The shell command of a tool call; the tool's name for another tool."""
    inp = x.get("input") if isinstance(x.get("input"), dict) else {}
    return inp.get("command") or x.get("name") or ""


def after_send(recs, i):
    """What the session did after the tool call at recs[i] that sent a message: 'ended its turn' (it waits for
    the reply at its prompt), 'waited in ...' (maxpm wait, inbox --wait, a sleep or a loop), or 'continued'
    (another tool call). A look at the queue (inbox, show, heartbeat) decides nothing. A wait command that
    runs in the background decides nothing by itself: the session waited when its turn then ended."""
    background = None
    for y in recs[i + 1:]:
        if y[1] != "assistant":
            continue
        if y[2] == "end_turn" and "text" in y[3]:
            return background or "ended its turn"
        for x in y[6] if isinstance(y[6], list) else []:
            if isinstance(x, dict) and x.get("type") == "tool_use":
                cmd = command_of(x)
                w = re.search(WAITS, cmd)
                if w:
                    how = "waited in " + ("maxpm " + w.group(1) if w.group(1) else "a sleep or a loop of its own")
                    if not (isinstance(x.get("input"), dict) and x["input"].get("run_in_background")):
                        return how
                    background = how + ", in the background, and ended its turn"
                    continue
                if re.search(SEND, cmd) or re.search(LOOKS, cmd):
                    continue
                return "continued"
    return background or "ended its turn"


sends = collections.defaultdict(list)      # agent -> (time of the tool call, what the session did after it)
turn_ends = collections.defaultdict(list)  # agent -> (time, last text, time of the next line, the next prompt)
activity = collections.defaultdict(list)   # agent -> times of its tool calls
denied = []                                # (time, agent, reason, items held, tool calls after it, time of the turn's end)
sessions = 0
for sid, agent in owner.items():
    if not is_worker(agent) or agent not in by_agent:
        continue
    for path in sorted(Path(root).glob(f"*/{sid}.jsonl")):
        recs = []
        for line in open(path, errors="replace"):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict) or d.get("isSidechain") or d.get("type") not in ("user", "assistant"):
                continue
            t = ts(d.get("timestamp"))
            m = d.get("message") if isinstance(d.get("message"), dict) else {}
            cont = m.get("content")
            if not t:
                continue
            kinds = ["str"] if isinstance(cont, str) else [x.get("type") for x in cont or [] if isinstance(x, dict)]
            recs.append((t, d["type"], m.get("stop_reason"), kinds, text_of(cont), bool(d.get("isMeta")), cont))
        if not recs or recs[-1][0] < SINCE:
            continue
        sessions += 1
        for i, r in enumerate(recs):
            if r[0] < SINCE or r[0] > UNTIL:
                continue
            if r[1] == "assistant" and "tool_use" in r[3]:
                activity[agent].append(r[0])
                cmd = " ".join(command_of(x) for x in r[6] if isinstance(x, dict) and x.get("type") == "tool_use")
                if re.search(SEND, cmd):
                    sends[agent].append((r[0], after_send(recs, i)))
            if r[1] == "user" and "tool_result" in r[3]:
                for x in r[6]:
                    if not (isinstance(x, dict) and x.get("is_error")):
                        continue
                    reason = re.match(r"Permission for this action was denied by the Claude Code auto mode classifier\. "
                                      r"Reason: \[?([^\].]+)", result_text(x))
                    if not reason:
                        continue
                    calls, end = 0, None
                    for y in recs[i + 1:]:
                        if y[1] == "assistant" and "tool_use" in y[3]:
                            calls += 1
                        if y[1] == "assistant" and y[2] == "end_turn" and "text" in y[3]:
                            end = y[0]
                            break
                    why = reason.group(1)
                    denied.append((r[0], agent, "no reason given" if why.startswith("The server-side") else why,
                                   [h["item_id"] for h in held_at(agent, r[0])], calls, end))
            if r[1] == "assistant" and r[2] == "end_turn" and "text" in r[3]:
                # The stand ends at the session's next line: a prompt (a person, a background command that
                # ended, a message of a subagent), or its own next request.
                nxt = next((x for x in recs[i + 1:] if not x[5]), None)
                woke = nxt[4] if nxt and nxt[1] == "user" and "tool_result" not in nxt[3] else ""
                turn_ends[agent].append((r[0], r[4], nxt and nxt[0], woke))

# ---- 1. the queue: how each hold ended ----

# The cause of a release or a block, from its text; the first rule that matches.
MACHINE = r"[Dd]isk|[Bb]uild stop|Docker"
NOTE_CAUSES = [
    ("a refused command (auto mode classifier): released with a note", r"classifier"),
    ("review: the reviewer wrote commits of the release", r"self-approval|I authored|own release|my #?\d* ?(doc )?commit"),
    ("review: the pinned revision was old", r"pin is stale"),
    ("split into pieces (the pieces are new items)", r"\bSplit (into|by)\b"),
    ("claimed too early: it waits on another item", r"is not live|Not ready:"),
    ("push freeze for a release review: the work is done, the push waits", r"push freeze|after the freeze|\(freeze\)"),
    ("all sessions stop: a restart of tmux or of the computer", r"tmux restart|[Ww]ind-down|[Ss]topped by manager"),
    ("machine limit: disk space, a build stop, Docker down", MACHINE),
    ("a missing tool or sign-in", r"signed.in|sign-in|browser agent|MCP tools"),
    ("claimed too early: it waits on another item", r"[Ww]aits on #|[Ww]aits for #|not live|Not ready|as a prerequisite"),
    ("an alert or note of the manager read as a stop", r"manager's hold|Paused|manager alert"),
]


def note_cause(text):
    for name, rx in NOTE_CAUSES:
        if re.search(rx, text):
            return name
    return "other (a handoff note)"


def prereq_cause(kind, pre):
    """The cause of a release or a hold for a new prerequisite, from the prerequisite."""
    if pre["kind"] == "fixes":
        return "review failed: the fix list waits for a person (review fail --ask)"
    if pre["kind"] == "deploy":
        return "prerequisite: a deploy (the item is a check on production)"
    if pre["doer"] == "human":
        if re.search(MACHINE, pre["title"]):
            return "machine limit: disk space, a build stop, Docker down"
        if re.match(r"(Approve|Decide|Pick|Answer|Choose)\b", pre["title"]):
            return "prerequisite: a decision of a person"
        return "prerequisite: a step only a person can do (account, sign-in, a walk)"
    if kind == "review":
        return "review failed: fix items for agents"
    return "prerequisite: work for an agent"


stops = []  # (cause, at, item, agent, how, text)
ends = collections.Counter()
hold_how = {}
for h in holds:
    if h["s"] < SINCE or h["s"] > UNTIL or not is_worker(h["agent"]):
        continue
    kind = items[h["item_id"]]["kind"]
    if not h["e"] or h["e"] > UNTIL:
        ends[(kind, "still held")] += 1
        continue
    evs = [r["change"] for r in c.execute(
        "SELECT change FROM events WHERE item_id=? AND at>=? AND at<=? ORDER BY id",
        (h["item_id"], z(h["e"] - timedelta(seconds=2)), z(h["e"] + timedelta(seconds=2))))]
    how, text, pre = None, [], []
    for ch in evs:
        if ch.startswith("done"):
            how = "done"
            break
        if ch.startswith("released: new prerequisites"):
            how, pre = "released for a new prerequisite (--blocks --release)", re.findall(r"#(\d+)", ch)
        elif ch.startswith("held by"):
            how = "held for a prerequisite (--blocks --keep)"
            pre = re.findall(r"#(\d+)", ch) or [w.split()[2] for w in evs if w.startswith("waits on")]
        elif ch.startswith("blocked:"):
            how = "maxpm blocked, with a reason"
            text.append(ch)
        elif re.match(r"released: .* stopped at its prompt", ch) or "left the queue" in ch:
            how = how or "taken back by maxpm serve: the session stood at its prompt"
        elif ch.startswith("waited "):
            how = "released after human_wait_max"
        elif ch.startswith("lease expired"):
            how = "the lease ran out"
        elif ch.startswith("dropped"):
            how = "dropped"
        elif ch.startswith("released"):
            how = how or "released with a note"
            text.append(ch)
    how = how or "other"
    ends[(kind, how)] += 1
    hold_how[h["id"]] = how
    if how in ("done", "dropped", "other"):
        continue
    if pre:
        cause = prereq_cause(kind, items[int(pre[0])])
        text = ["#" + p + " " + items[int(p)]["title"] for p in pre]
    elif text:
        cause = note_cause(" ".join(text))
    elif how.startswith("taken back") or how == "the lease ran out":
        # What the session's last turn before it said: the cause of the stand.
        last = [e for e in turn_ends[h["agent"]] if h["s"] <= e[0] <= h["e"]]
        cause = "(the hold ended with no action of the worker) " + (
            prompt_cause(last[-1][1], last[-1][3]) if last else "no turn end in the transcript: the session ended or hung")
        text = [last[-1][1][-200:]] if last else []
    else:
        cause = how
    stops.append((cause, h["e"], h["item_id"], h["agent"], how, " | ".join(text)))

print(f"Window {z(SINCE)} to {z(UNTIL)}")
print("\n== 1. How the holds of workers ended (item kind, end, count) ==")
for (kind, how), n in sorted(ends.items(), key=lambda x: -x[1]):
    print(f"{n:5}  {kind:8} {how}")

print("\n== 1b. Stops the queue shows, by cause: count; minutes the item stood still (to the next claim or its "
      "close); what waited on the item at the stop ==")
groups = collections.defaultdict(list)
for s in stops:
    groups[s[0]].append(s)
for cause, rows in sorted(groups.items(), key=lambda x: -len(x[1])):
    still = [stood_still(r[2], r[1]) for r in rows]
    hows = collections.Counter(r[4].split(" (")[0].split(":")[0] for r in rows)
    print(f"\n{cause}\n    stops {len(rows)} on {len({r[2] for r in rows})} items by {len({r[3] for r in rows})} sessions; "
          f"stood still: {spread(still)}\n    {waiting_text([([r[2]], r[1]) for r in rows])}")
    print("    how: " + ", ".join(f"{k} {v}" for k, v in hows.most_common()))
    if DETAIL:
        for r in sorted(rows, key=lambda r: r[1]):
            print(f"      {r[1]:%m-%d %H:%M} #{r[2]} {r[3]} still {stood_still(r[2], r[1]):.0f}m "
                  f"waited {waited_on(r[2], r[1])} :: {r[5][:230]!r}")

# A person's item that a worker added as a prerequisite: the time to the person's answer.
answer = []
for cause, at, item_id, agent, how, text in stops:
    if cause.startswith("prerequisite: a decision") or cause.startswith("prerequisite: a step only"):
        pre = items[int(re.match(r"#(\d+)", text).group(1))]
        if pre["closed_at"]:
            answer.append(mins(ts(pre["created_at"]), ts(pre["closed_at"])))
print(f"\nA person's item added as a prerequisite: minutes from the add to its close: {spread(answer)}")

# ---- 2. the transcripts: a turn that ended while the session held an item ----

waits = []  # (cause, at, agent, items held, minutes, text)
for agent, rows in turn_ends.items():
    for at, text, nxt, woke in rows:
        mine = held_at(agent, at)
        if not mine:
            continue
        end = min([x for x in (nxt, UNTIL) if x] + [h["e"] or UNTIL for h in mine])
        if mins(at, end) >= MIN_WAIT:
            waits.append((prompt_cause(text, woke), at, agent, [h["item_id"] for h in mine], mins(at, end), text))

print(f"\n== 2. Turns that ended while the session held an item, {MIN_WAIT} minutes or more at the prompt "
      f"({sessions} worker sessions with a transcript) ==")
groups = collections.defaultdict(list)
for w in waits:
    groups[w[0]].append(w)
for cause, rows in sorted(groups.items(), key=lambda x: -len(x[1])):
    print(f"\n{cause}\n    waits {len(rows)} in {len({r[2] for r in rows})} sessions on "
          f"{len({i for r in rows for i in r[3]})} items; at the prompt: {spread([r[4] for r in rows])}\n"
          f"    {waiting_text([(r[3], r[1]) for r in rows])}")
    if DETAIL:
        for r in sorted(rows, key=lambda r: r[1]):
            print(f"      {r[1]:%m-%d %H:%M} {r[2]} holds {r[3]} {r[4]:.0f}m waited "
                  f"{[waited_on(i, r[1]) for i in r[3]]} :: {r[5][-260:]!r}")

# ---- 3. the questions of workers ----

print("\n== 3. Questions of workers: to whom; count; minutes to the answer; the asker held the item of the "
      "question at the ask; of those, made no tool call until the answer (5 minutes or more) ==")
qs = collections.defaultdict(list)
for m in c.execute("SELECT * FROM messages WHERE kind='question' AND created_at>=? AND created_at<=?",
                   (z(SINCE), z(UNTIL))):
    if not is_worker(m["from_agent"]):
        continue
    to = m["to_agent"] or ""
    whom = "a person" if to in people else "a manager" if to.startswith("manager-") else "another agent"
    at = ts(m["created_at"])
    ans = c.execute("SELECT min(created_at) FROM messages WHERE reply_to=? AND kind='answer'", (m["id"],)).fetchone()[0]
    end = ts(ans) or ts(m["closed_at"])
    held = [h for h in held_at(m["from_agent"], at) if m["item_id"] in (None, h["item_id"])]
    # The time the asker held the item after the ask: to the answer, or to the end of the hold.
    kept = mins(at, min([end or UNTIL] + [h["e"] or UNTIL for h in held])) if held else 0
    calls = [t for t in activity[m["from_agent"]] if at + timedelta(minutes=1) < t < (end or UNTIL)]
    qs[whom].append((m, at, end, bool(held), not calls and mins(at, end or UNTIL) >= MIN_WAIT, kept))
for whom, rows in qs.items():
    done = [mins(at, end) for m, at, end, held, idle, kept in rows if end]
    idle = [r for r in rows if r[3] and r[4]]
    print(f"{whom}: {len(rows)} questions, {len(rows) - len(done)} with no answer yet; to the answer: {spread(done)}\n"
          f"    held the item at the ask: {sum(1 for r in rows if r[3])}; the item stayed held to the answer or "
          f"the end of the hold: {spread([r[5] for r in rows if r[3]])}\n"
          f"    of those, no tool call until the answer: {len(idle)} "
          f"({spread([mins(r[1], r[2] or UNTIL) for r in idle])})")
    if DETAIL:
        for m, at, end, held, idle, kept in rows:
            print(f"      {at:%m-%d %H:%M} #{m['id']} {m['from_agent']} item {m['item_id']} "
                  f"{'held ' + format(kept, '.0f') + 'm' if held else 'not held'} {'IDLE' if held and idle else ''} "
                  f"answer {mins(at, end or UNTIL):.0f}m{'' if end else ' (open)'} :: {m['body'][:200]!r}")

# ---- 4. refusals of the auto mode classifier ----

print("\n== 4. Refusals of the auto mode classifier in worker sessions ==")
turns = {}  # one row per turn that had a refusal: (agent, time of the turn's end) -> the turn's last refusal
for d in denied:
    turns[(d[1], d[5])] = d
print(f"{len(denied)} refused calls in {len({d[1] for d in denied})} sessions, in {len(turns)} turns; "
      f"{sum(1 for d in denied if d[3])} while the session held an item")
print("reasons: " + ", ".join(f"{k} {v}" for k, v in collections.Counter(d[2] for d in denied).most_common()))
after = collections.Counter()
for (agent, end), d in sorted(turns.items(), key=lambda x: x[1][0]):
    stand = next((w for w in waits if w[2] == agent and w[1] == end and w[0] != OWN), None)
    gave = [h for h in by_agent[agent] if h["item_id"] in d[3] and h["e"] and d[0] <= h["e"] <= d[0] + timedelta(minutes=20)
            and hold_how.get(h["id"]) not in ("done", None)]
    if stand:
        out = "ended the turn and stood at the prompt with the item (5 minutes or more)"
    elif gave:
        out = "released or blocked the item in the next 20 minutes"
    elif d[4] > 2:
        out = "continued in the same turn (more than two tool calls after the refusal)"
    else:
        out = "ended the turn (a stand under 5 minutes, or no item held)"
    after[out] += 1
    if DETAIL:
        print(f"      {d[0]:%m-%d %H:%M} {agent} holds {d[3]} [{d[2]}] calls after {d[4]} :: {out}"
              f"{' ' + format(stand[4], '.0f') + 'm' if stand else ''}")
for k, v in after.most_common():
    print(f"{v:5}  {k}")

# ---- 5. alerts and questions of workers to the manager: did the sender stop until the reply? ----

print("\n== 5. Alerts and questions of workers to a manager: what the sender did after the send ==")
eq = turns_n = 0
for u in c.execute("SELECT * FROM item_usage WHERE measured_at>=? AND agent NOT LIKE 'manager-%'", (z(SINCE),)):
    eq += core.usage_eq(dict(u))
    turns_n += u["turns"]
rows = []
for m in c.execute("SELECT * FROM messages WHERE kind IN ('alert','question') AND to_agent LIKE 'manager-%' "
                   "AND created_at>=? AND created_at<=?", (z(SINCE), z(UNTIL))):
    who = m["from_agent"]
    if not is_worker(who) or who == "maxpm":
        continue
    at = ts(m["created_at"])
    call = [x for x in sends[who] if at - timedelta(seconds=120) <= x[0] <= at + timedelta(seconds=2)]
    did = call[-1][1] if call else "no transcript line for the send"
    if re.search(r"was added before your .*Stop and wait for it", m["body"]):
        did = "sent by MaximizePM itself: a worker's command added a prerequisite to an item the manager holds"
    reply = c.execute("SELECT min(created_at) FROM messages WHERE from_agent=? AND to_agent=? AND created_at>=? "
                      "AND (reply_to=? OR created_at<=?)",
                      (m["to_agent"], who, m["created_at"], m["id"], z(at + timedelta(minutes=30)))).fetchone()[0]
    mine = held_at(who, at)
    # What the queue knows without a question to the sender: its item left its hands or was blocked near
    # the send, or the sender then stood at its prompt for MIN_WAIT minutes or more.
    near = any(h["e"] and abs(mins(at, h["e"])) <= 3 and hold_how.get(h["id"]) not in ("done", None)
               for h in by_agent[who])
    stand = any(at <= e[0] <= at + timedelta(minutes=3) and mins(e[0], e[2] or UNTIL) >= MIN_WAIT
                for e in turn_ends[who])
    known = "its item was released or blocked at the send" if near else (
        "it stood at its prompt 5 minutes or more" if stand else "nothing")
    rows.append((m, did, mins(at, ts(reply)) if reply else None, bool(mine), known))
print(f"{len(rows)} messages ({sum(1 for r in rows if r[0]['kind'] == 'alert')} alerts, "
      f"{sum(1 for r in rows if r[0]['kind'] == 'question')} questions) from {len({r[0]['from_agent'] for r in rows})} "
      f"sessions; {sum(1 for r in rows if r[3])} from a session that held an item")
for kind in ("alert", "question"):
    print(f"\n{kind}:")
    for did, n in collections.Counter(r[1] for r in rows if r[0]["kind"] == kind).most_common():
        sub = [r for r in rows if r[0]["kind"] == kind and r[1] == did]
        print(f"  {n:4}  {did}; the manager's reply: {spread([r[2] for r in sub if r[2] is not None])}; "
              f"no reply in 30 minutes: {sum(1 for r in sub if r[2] is None)}")
        for known, k in collections.Counter(r[4] for r in sub).most_common():
            print(f"          what the queue saw: {known}: {k}")
if turns_n:
    print(f"\nOne more turn of a worker (a question back) costs about {eq / turns_n / 1000:.1f}k eq "
          f"(mean of {turns_n} requests of workers on items measured in the window)")
if DETAIL:
    for m, did, reply, held, known in rows:
        print(f"      {m['created_at'][5:16]} #{m['id']} {m['kind']} {m['from_agent']} {'held' if held else ''} "
              f"{did} | reply {reply if reply is None else round(reply, 1)}m | {known} :: {m['body'][:150]!r}")
