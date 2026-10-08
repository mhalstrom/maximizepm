"""Manager cost for each day, from the Claude Code transcripts (#1312; the eq of #1096: input + 0.1 x cache
read + 2.0 x 1-hour cache write). Reads the queue read-only. A session is a manager's when its agent's name
starts with manager-. Run from the repository: python3 current_project/research/manager_cost.py [since [until]]
(ISO times, UTC). A second table (#1313) counts, for the manager sessions in that time, what started each turn,
the watch commands, and the compactions."""
import collections
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from river import core  # noqa: E402

since = datetime.fromisoformat(sys.argv[1] if len(sys.argv) > 1 else "2026-10-06T00:00:00+00:00")
until = datetime.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else datetime.now(timezone.utc)
c = sqlite3.connect(f"file:{core.db_path()}?mode=ro", uri=True)
root = core.transcripts_root()
mgr, allx, reqs, ctx = (collections.defaultdict(float) for _ in range(4))
hours, seen, managers = collections.defaultdict(set), set(), []
# A session belongs to the agent that used it first: a manager also runs commands with other agents' names (#1313).
for agent, sid in c.execute("SELECT agent, session_id FROM agent_sessions ORDER BY first_seen, rowid"):
    if sid in seen:
        continue
    seen.add(sid)
    if agent.startswith("manager-"):
        managers.append(sid)
    got = core._session_requests(sid, root)
    for t, u in (got[0].values() if got else ()):
        if not since <= t < until:
            continue
        d, e = t.strftime("%Y-%m-%d"), core.usage_eq(u)
        allx[d] += e
        if agent.startswith("manager-"):
            mgr[d] += e
            reqs[d] += 1
            ctx[d] += sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens",
                                                   "cache_creation_input_tokens"))
            hours[d].add(t.strftime("%d%H"))
print("| Day (UTC) | Manager eq | All agents eq | Share | Requests | eq per request | Mean context "
      "| Hours with requests | eq per hour |")
print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
for d in sorted(allx):
    n, h = reqs[d] or 1, len(hours[d]) or 1
    print(f"| {d} | {mgr[d] / 1e6:.1f}M | {allx[d] / 1e6:.1f}M | {100 * mgr[d] / allx[d]:.0f}% | {int(reqs[d]):,} "
          f"| {mgr[d] / n / 1e3:.1f}k | {ctx[d] / n / 1e3:.0f}k | {len(hours[d])} | {mgr[d] / h / 1e6:.2f}M |")
m, a = sum(mgr.values()), sum(allx.values())
print(f"\nAll days: manager {m / 1e6:.1f}M of {a / 1e6:.1f}M ({100 * m / max(a, 1):.0f}%)")

# What the manager sessions did in that time (their main transcripts): a turn starts with a message (a person, or
# a message that river delivers into the session) or with the end of a background command (a task notification).
n = collections.Counter()
pre = []
for sid in managers:
    for path in sorted(Path(root).glob(f"*/{sid}.jsonl")):
        for line in path.read_text(errors="replace").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            t = core._ts(r.get("timestamp") or "")
            if t is None or not since <= t < until:
                continue
            msg = r.get("message") or {}
            if r.get("subtype") == "compact_boundary":
                n["compactions"] += 1
                pre.append((r.get("compactMetadata") or {}).get("preTokens") or 0)
            elif r.get("type") == "assistant":
                for part in msg.get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "tool_use" and part.get("name") == "Bash":
                        cmd = part["input"].get("command", "")
                        n["manage --watch started"] += "manage --watch" in cmd
                        n["inbox --wait started"] += "inbox --wait" in cmd
            elif r.get("type") == "user" and not r.get("isMeta") and not r.get("isCompactSummary"):
                parts = msg.get("content")
                text = parts if isinstance(parts, str) else " ".join(
                    x.get("text", "") for x in parts if isinstance(x, dict) and x.get("type") == "text") if all(
                    isinstance(x, dict) and x.get("type") == "text" for x in parts or [None]) else None
                if text is not None and "<command-name>" not in text and "<local-command" not in text:
                    n["turns: the end of a background command" if "<task-notification>" in text
                      else "turns: a message"] += 1
print("\n| Manager sessions in that time | Count |\n|---|---:|")
for k in ("turns: a message", "turns: the end of a background command", "manage --watch started",
          "inbox --wait started", "compactions"):
    print(f"| {k} | {n[k]:,} |")
if pre:
    print(f"\nCompactions start at {min(pre) / 1e3:.0f}k to {max(pre) / 1e3:.0f}k tokens of context.")
