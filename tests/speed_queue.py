"""A queue of the size of a real one, written in a few seconds, for the page-speed test (#6505).

seed/large.py adds each item through core.item_add and core.done: 4,000 items took 8 minutes, and its queue has no
agents, messages, events or goals, so a page build on it took 8 ms where the same build on a copy of a real queue
took 28 ms (median). This one writes the rows in bulk with the sizes of a real queue of one person (2026-11-20:
about 4,000 items of which 170 open, 35 projects, 5,000 links, 60,000 events, 450 agents of which 30 live, 14,000
messages, 50 goals, 6 deploy targets with 140 releases and 27 checks, 150 policies). Its sizes are a little
larger, so the budget holds for a queue that grows.
"""
import random
from datetime import timedelta

from river import core

SIZES = {"projects": 35, "items": 6000, "open": 200, "working": 25, "deps": 7000, "events": 80000, "agents": 450,
         "live": 30, "messages": 16000, "goals": 60, "targets": 6, "releases": 25, "checks": 5, "policies": 120}

WORDS = ("orders", "checkout", "pricing page", "search", "invoices", "the importer", "email", "the footer",
         "accounts", "reports", "the API", "webhooks", "the cache", "sign-in", "exports", "the dashboard")
VERBS = ("Build", "Fix", "Review", "Test", "Document", "Speed up", "Clean up", "Add a check for")


def build(conn, sizes=None, seed=6505):
    """Write the queue into conn (a new, empty database). Returns the sizes it used."""
    s = dict(SIZES, **(sizes or {}))
    rnd = random.Random(seed)
    t0 = core.now() - timedelta(days=60)
    at = lambda minutes: core.iso(t0 + timedelta(minutes=minutes))  # noqa: E731
    span = 60 * 24 * 60
    core.register(conn, "alex", human=True)
    core.config_set(conn, "setup_done", "on")  # the setup guide would cover the page
    names = [f"project-{n:02d}" for n in range(1, s["projects"] + 1)]
    for n in names:
        core.project_add(conn, n)
    pids = [r["id"] for r in conn.execute("SELECT id FROM projects ORDER BY id")]
    live = [f"agent-{n:03d}" for n in range(s["live"])]
    gone = [f"agent-{n:03d}" for n in range(s["live"], s["agents"])]
    with core.tx(conn):
        stamp = core.iso(core.now())
        conn.executemany("INSERT INTO agents(name, kind, note, registered_at, last_seen, model, agent_type) VALUES "
                         "(?, 'ai', ?, ?, ?, 'opus', 'claude-code')",
                         [(a, f"works on {rnd.choice(WORDS)}", at(rnd.randrange(span)),
                           stamp if a in live else at(rnd.randrange(span))) for a in live + gone])
        conn.executemany("INSERT INTO goals(project_id, name, outcome, done_when, rank, status, created_at) VALUES "
                         "(?, ?, ?, ?, ?, ?, ?)",
                         [(rnd.choice(pids), f"goal-{g:02d}", f"{rnd.choice(WORDS)} works", "a test passes", g,
                           "open" if g % 3 else "complete", at(rnd.randrange(span))) for g in range(s["goals"])])
        n, first_open = s["items"], s["items"] - s["open"]
        rows = []
        for k in range(n):
            closed = k < first_open
            status = "done" if closed else "in_progress" if k >= n - s["working"] else "open"
            made = k * span // n
            rows.append((rnd.choice(pids), f"{rnd.choice(VERBS)} {rnd.choice(WORDS)} for {rnd.choice(WORDS)}, part {k}",
                         rnd.choice((1, 2, 2, 2, 3)), k, "human" if k % 23 == 0 else "any", status,
                         live[k % len(live)] if status == "in_progress" else None,
                         core.iso(core.now() + timedelta(minutes=30)) if status == "in_progress" else None,
                         "done" if closed else "", at(made), at(made + rnd.randrange(1, 600)) if closed else None))
        conn.executemany("INSERT INTO items(project_id, title, priority, rank, doer, status, assignee, "
                         "lease_expires_at, output, created_at, closed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        ids = [r["id"] for r in conn.execute("SELECT id FROM items ORDER BY id")]
        links = set()
        while len(links) < s["deps"]:  # most items wait on recent work, as in a real queue
            k = rnd.randrange(1, n)
            links.add((ids[k], ids[max(0, k - rnd.randrange(1, 40))]))
        conn.executemany("INSERT OR IGNORE INTO deps(item_id, blocked_by) VALUES (?, ?)", sorted(links))
        goals = [r["id"] for r in conn.execute("SELECT id FROM goals")]
        conn.executemany("INSERT OR IGNORE INTO item_goals(item_id, goal_id) VALUES (?, ?)",
                         [(i, rnd.choice(goals)) for i in ids if rnd.random() < 0.3])
        conn.executemany("INSERT INTO events(item_id, at, actor, change) VALUES (?, ?, ?, ?)",
                         [(rnd.choice(ids), at(rnd.randrange(span)), rnd.choice(live + gone), "a change")
                          for _ in range(s["events"])])
        everyone = live + gone + ["alex"]
        conn.executemany("INSERT INTO messages(kind, from_agent, to_agent, item_id, thread_id, body, state, "
                         "created_at, read_at) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)",
                         [(rnd.choice(("note", "alert", "question")), rnd.choice(everyone), rnd.choice(everyone),
                           rnd.choice(ids), "a message about the work", "read" if m % 20 else "open",
                           at(m * span // s["messages"]), at(m * span // s["messages"] + 5) if m % 20 else None)
                          for m in range(s["messages"])])
        conn.execute("UPDATE messages SET thread_id=id")
    # Deploy targets with their releases (one deploy item each) and the checks of their projects: the page's
    # Deploy rows and release plans are a large part of a live build.
    for t in range(s["targets"]):
        name = f"target-{t}"
        core.target_add(conn, name, "where the projects ship to")
        with core.tx(conn):
            conn.execute("UPDATE projects SET target=? WHERE id IN (?, ?)", (name, pids[2 * t], pids[2 * t + 1]))
            dpid = conn.execute("SELECT id FROM projects WHERE name=?", (names[2 * t],)).fetchone()["id"]
            conn.executemany("INSERT INTO items(project_id, title, priority, rank, doer, status, output, kind, target, "
                             "created_at, closed_at) VALUES (?, ?, 0, 0, 'ai', ?, ?, 'deploy', ?, ?, ?)",
                             [(dpid, f"Deploy {name}", "done" if r else "open", "released" if r else "", name,
                               at(span - r * 600 - 60), at(span - r * 600) if r else None)
                              for r in range(s["releases"])])
            conn.executemany("INSERT INTO triggers(scope_kind, scope, name, form, config, every, fail_after, "
                             "recover_after, timeout, severity, created_at) VALUES ('project', ?, ?, 'url', '{}', "
                             "'5m', 2, 1, '30s', 'item', ?)",
                             [(names[2 * t], f"check-{k}", at(0)) for k in range(s["checks"])])
    for p in range(s["policies"]):
        core.policy_add(conn, f"A rule for the sessions, number {p}: do the work this way.", "a reason",
                        source="alex 2026-11-20", actor="alex")
    core.sync_needs_you(conn)
    return s
