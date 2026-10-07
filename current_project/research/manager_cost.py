"""Manager cost for each day, from the Claude Code transcripts (#1312; the eq of #1096: input + 0.1 x cache
read + 2.0 x 1-hour cache write). Reads the queue read-only. A session is a manager's when its agent's name
starts with manager-. Run from the repository: python3 current_project/research/manager_cost.py [since [until]]
(ISO times, UTC)."""
import collections
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
hours, seen = collections.defaultdict(set), set()
for agent, sid in c.execute("SELECT agent, session_id FROM agent_sessions"):
    if sid in seen:
        continue
    seen.add(sid)
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
