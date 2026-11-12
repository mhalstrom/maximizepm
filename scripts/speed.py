#!/usr/bin/env python3
"""The speed test of MaximizePM (#5491): every maxpm command, end to end (process start to output), and the page's
state answer, on a copy of a large queue. It fails when the median time of a command, or of a page answer, is over
the limit (mark 2026-11-12: under 100 ms on the same computer).

    python3 scripts/speed.py                          # a queue of 3,000 items it makes (seed/large.py)
    python3 scripts/speed.py --from ~/.maximizepm/maxpm.db   # a copy of a real queue (the file is only read)
    python3 scripts/speed.py --limit 150 --runs 7 --only show,next

It never touches the queue of MAXPM_DB: every run uses a copy in a temporary folder, and a write command gets a
fresh copy for each run. The times depend on the load of the computer: the report starts with the load average."""
import argparse
import json
import os
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if "--repo" in sys.argv[:-1]:  # the code to measure: another checkout (to compare with the code before a change)
    ROOT = os.path.abspath(os.path.expanduser(sys.argv[sys.argv.index("--repo") + 1]))
sys.path.insert(0, ROOT)
MAXPM = [sys.executable, os.path.join(ROOT, "bin", "maxpm"), "-q"]

# (name, words, writes). {open} is an open agent item, {held} the item the test agent holds, {agent} its name.
COMMANDS = [
    ("version", "--version", False),
    ("show", "show {held}", False),
    ("show --brief", "show {held} --brief", False),
    ("next", "next -n 3", False),
    ("list", "list", False),
    ("status", "status", False),
    ("who", "who", False),
    ("inbox", "inbox --peek", False),
    ("needs-you", "needs-you", False),
    ("blockers", "blockers {open}", False),
    ("search", "search report", False),
    ("heartbeat", "heartbeat", True),
    ("now", "now testing", True),
    ("edit", "edit {open} --short speed", True),
    ("add", "add {project} Speed_probe_item --new probe", True),
    ("note", "send note --to {agent} hello", True),
    ("release", "release {held} --note probe", True),
    ("done", "done {held} --output probe", True),
    ("claim", "claim {open}", False),  # after a release: a fresh copy each run
    ("go", "go --project {project}", True),
]


def make_queue(path, items, open_items):
    from seed import large
    from river import core
    conn = core.connect(path)
    large.load(conn, items, open_items)
    conn.close()


def prepare(path):
    """Register the test agent with one held item; returns the words to fill in the commands."""
    from river import core
    conn = core.connect(path)
    try:
        agent = "speed-test"
        core.register(conn, agent)
        def ready():
            return [a["id"] for a in core.ready_list(conn) if a["doer"] in ("any", "ai") and a["kind"] == "work"
                    and not a["reserved_for"] and not a["tier"]]
        open_ids = ready()
        if len(open_ids) < 2:  # a queue with little ready work: two items of its own (in the copy)
            project = core.project_list(conn)[0]["name"]
            for n in (1, 2):
                core.item_add(conn, project, f"Speed test item number {n} zq{n}x", actor=agent, new="speed test")
            open_ids = ready()
        core.claim(conn, open_ids[0], agent)
        project = core.item_show(conn, open_ids[0])["project"]
        return {"agent": agent, "held": open_ids[0], "open": open_ids[1], "project": project}
    finally:
        conn.close()


def run_once(db, words, env):
    """(wall seconds, CPU seconds, exit status) of one command in a new process."""
    before = os.times()
    t = time.perf_counter()
    p = subprocess.run(MAXPM + words, env={**env, "MAXPM_DB": db}, cwd=ROOT,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    wall = time.perf_counter() - t
    after = os.times()
    cpu = (after.children_user - before.children_user) + (after.children_system - before.children_system)
    return wall, cpu, p.returncode, p.stderr.strip().splitlines()[-1:] if p.returncode else []


def page_times(db, runs):
    """The page's state: one build (first load), the first answer's bytes, and a poll with no change."""
    out = {}
    code = f"""
import json, sys, time
sys.path.insert(0, {ROOT!r})
from river import server, statedelta
walls = []
for k in range({runs}):
    server._state_cache.clear()
    t = time.perf_counter(); st = server.page_state(False); first = statedelta.answer(statedelta.slim(st), None)
    body = json.dumps(first, default=str); walls.append(time.perf_counter() - t)
t = time.perf_counter(); poll = json.dumps(statedelta.answer(statedelta.slim(server.page_state(False)), first["v"]), default=str)
print(json.dumps({{"build": sorted(walls)[len(walls) // 2], "first_bytes": len(body), "poll_bytes": len(poll),
                  "poll": time.perf_counter() - t}}))
"""
    p = subprocess.run([sys.executable, "-c", code], env={**os.environ, "MAXPM_DB": db}, cwd=ROOT,
                       capture_output=True, text=True)
    if p.returncode:
        return {"error": (p.stderr.strip().splitlines() or ["?"])[-1]}
    out.update(json.loads(p.stdout))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--from", dest="src", help="a queue database to copy (read only)")
    ap.add_argument("--items", type=int, default=3000, help="without --from: the size of the queue it makes")
    ap.add_argument("--open", type=int, default=300, help="without --from: how many of its items stay open")
    ap.add_argument("--limit", type=float, default=100, help="the limit in ms for the median of each command")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--only", help="a comma-separated list of command names")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--repo", help="the checkout whose code it measures (default: this one)")
    a = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix="maxpm-speed-")
    try:
        base = os.path.join(tmp, "base.db")
        if a.src:
            src = sqlite3.connect(f"file:{os.path.expanduser(a.src)}?mode=ro", uri=True)
            dst = sqlite3.connect(base)
            src.backup(dst)
            src.close()
            dst.close()
        else:
            make_queue(base, a.items, a.open)
        fill = prepare(base)
        env = {k: v for k, v in os.environ.items() if not k.startswith("MAXPM_")}
        env.update(MAXPM_AGENT=fill["agent"], MAXPM_QUIET="1")
        only = set(a.only.split(",")) if a.only else None
        rows, failed = [], []
        load = os.getloadavg()[0] if hasattr(os, "getloadavg") else None
        for name, words, writes in COMMANDS:
            if only and name not in only:
                continue
            argv = words.format(**fill).split()
            walls, cpus, err = [], [], []
            for k in range(a.runs):
                db = base
                if writes or name == "claim":
                    db = os.path.join(tmp, "run.db")
                    for ext in ("", "-wal", "-shm"):
                        if os.path.exists(db + ext):
                            os.remove(db + ext)
                    shutil.copy(base, db)
                    if name == "claim":
                        run_once(db, ["release", str(fill["held"]), "--note", "probe"], env)
                wall, cpu, rc, e = run_once(db, argv, env)
                walls.append(wall)
                cpus.append(cpu)
                err = err or e
            med = statistics.median(walls) * 1000
            row = {"command": name, "median_ms": round(med, 1), "cpu_ms": round(statistics.median(cpus) * 1000, 1),
                   "max_ms": round(max(walls) * 1000, 1), "error": err[0] if err else None}
            rows.append(row)
            if med > a.limit or err:
                failed.append(name)
        page = page_times(base, a.runs) if not only or "page" in only else {}
        if page.get("error") or page.get("build", 0) * 1000 > a.limit or page.get("poll", 0) * 1000 > a.limit:
            failed.append("page")
        conn = sqlite3.connect(base)
        size = conn.execute("SELECT count(*), sum(status IN ('open','in_progress','held')) FROM items").fetchone()
        conn.close()
        report = {"load": load, "items": size[0], "open": size[1], "limit_ms": a.limit, "commands": rows,
                  "page": page, "failed": failed}
        if a.json:
            print(json.dumps(report, indent=1))
        else:
            print(f"queue: {size[0]} items, {size[1]} open; load average {load:.1f}; limit {a.limit:.0f} ms (median "
                  f"of {a.runs} runs, process start to exit)")
            for r in rows:
                mark = "FAIL" if r["command"] in failed else "ok  "
                print(f"  {mark} {r['command']:<14} {r['median_ms']:7.1f} ms  (cpu {r['cpu_ms']:.1f}, max "
                      f"{r['max_ms']:.1f})" + (f"  error: {r['error']}" if r["error"] else ""))
            if page:
                if page.get("error"):
                    print(f"  FAIL page: {page['error']}")
                else:
                    print(f"  {'FAIL' if 'page' in failed else 'ok  '} page state  build {page['build'] * 1000:.1f} ms, "
                          f"first answer {page['first_bytes'] / 1e6:.2f} MB, poll {page['poll'] * 1000:.1f} ms "
                          f"({page['poll_bytes']} bytes)")
            print("FAILED: " + ", ".join(failed) if failed else "all under the limit")
        return 1 if failed else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
