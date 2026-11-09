"""Demo transcripts for the pictures (#4666): what two stand-in agents said and did in Claude Code, so the Sessions tab
shows a chat and the Artifacts tab shows what they wrote. Writes the files they wrote into the demo project folder,
their transcripts under ~/.claude/projects/, and records each agent's session id in the queue. It also forgets the
process each demo agent recorded (the shell of seed/screenshots.sh, gone now), so the Sessions tab finds each agent
by its tmux pane, as for a session that maxpm launch started; the other agents, which hold nothing, end.
Run after seed/screenshots.sh, with the same HOME (and MAXPM_DB, if set):  python3 seed/transcripts.py <project folder>"""
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from river import core  # noqa: E402

shop = Path(sys.argv[1]).resolve()
now = datetime.now(timezone.utc)


def at(minutes_ago):
    return (now - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def line(kind, content, ago, stop=None):
    msg = {"role": kind, "content": content}
    if stop:
        msg["stop_reason"] = stop
    return json.dumps({"type": kind, "timestamp": at(ago), "message": msg, "cwd": str(shop)}) + "\n"


def say(text, ago):
    return line("assistant", [{"type": "text", "text": text}], ago, "end_turn")


def call(name, ago, result, **args):
    i = "toolu_" + uuid.uuid4().hex[:20]
    return (line("assistant", [{"type": "tool_use", "id": i, "name": name, "input": args}], ago, "tool_use")
            + line("user", [{"type": "tool_result", "tool_use_id": i, "content": result}], ago))


def write(rel, text):
    f = shop / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    return str(f)


photos = write("docs/product-photos.md", "# Product photos\n\nThe photos in `web/public/products/` are 1600 px wide now "
               "(they were 4000 px). `web/scripts/resize.js` makes new ones the same size.\n\n| | Before | After |\n"
               "|---|---|---|\n| Width | 4000 px | 1600 px |\n| Page weight | 38 MB | 4.1 MB |\n")
write("web/scripts/resize.js", "// Resize every photo in a folder to 1600 px wide.\n")
orders = write("api/orders.md", "# Orders API\n\n- `POST /orders`: a new order from a cart\n- `GET /orders/{id}`: one order "
               "and its status\n- `GET /orders?page=2`: the orders of a customer, 20 a page\n\nStatus: `new`, `paid`, "
               "`shipped`, `cancelled`.\n")
coverage = write("api/coverage.html", "<!doctype html><title>Coverage</title><h1>api/orders.py: 94%</h1>")

chats = {
    "web-agent": (
        line("user", "go", 14)
        + call("Bash", 14, "You are MaximizePM agent web-agent. Role: WORKER.\nYOUR ITEM #10: Resize the product photos",
               command="maxpm go", description="Take work from the queue")
        + say("I take #10, which web-agent took over from alex: the product photos are 4000 px wide. I resize them to "
              "1600 px with a small script, so the next photos come out the same.", 13)
        + call("Write", 12, "Wrote 31 lines", file_path=str(shop / "web/scripts/resize.js"), content="...")
        + call("Bash", 10, "24 photos: 4000 px to 1600 px", command="node web/scripts/resize.js web/public/products",
               description="Resize the photos")
        + line("user", "Keep the originals somewhere, in case we need the large ones.", 9)
        + call("Bash", 8, "moved 24 files", command="git mv web/public/products/originals web/originals",
               description="Keep the originals")
        + call("Write", 7, "Wrote 9 lines", file_path=photos, content="...")
        + say(f"Done: 24 photos at 1600 px, the originals in `web/originals/`, and a note in {photos}. The product "
              "list is at http://localhost:5173/products: the page is 38 MB lighter. Now I commit.", 6)
        + line("assistant", [{"type": "tool_use", "id": "toolu_commit", "name": "Bash",
                              "input": {"command": 'git commit -am "Resize the product photos"',
                                        "description": "Commit the resized photos"}}], 5, "tool_use")
    ),
    "api-agent": (
        line("user", "go", 40)
        + call("Bash", 40, "You are MaximizePM agent api-agent. Role: WORKER.\nYOUR ITEM #2: Build the orders API",
               command="maxpm go", description="Take work from the queue")
        + call("Read", 39, "Read 61 lines", file_path=orders)
        + say("I build the orders API from api/orders.md: three routes, and the status values it names.", 38)
        + call("Write", 30, "Wrote 142 lines", file_path=str(shop / "api/orders.py"), content="...")
        + call("Bash", 25, "12 passed in 0.84s", command="pytest tests/test_orders.py", description="Run the tests")
        + call("Write", 24, "Wrote 1 file", file_path=coverage, content="...")
        + say(f"The three routes work and the tests pass; coverage is in {coverage}. The order history page needs "
              "pages, so I add `?page=` to GET /orders next.", 22)
    ),
}
# The two agents that seed/shoot.js adds: a few lines each.
for agent, item, title, steps in [
        ("search-agent", 15, "Add search to the product list",
         [("Read", "Read 97 lines", {"file_path": str(shop / "web/products/List.tsx")}),
          ("Bash", "6 passed", {"command": "npm test -- SearchBox", "description": "Run the tests"})]),
        ("email-agent", 16, "Send an email when an order ships",
         [("Read", "Read 44 lines", {"file_path": str(shop / "api/email.py")}),
          ("Bash", "1 failed, 7 passed", {"command": "pytest tests/test_email.py", "description": "Run the tests"})])]:
    text = line("user", "go", 9) + call("Bash", 9, f"You are MaximizePM agent {agent}. Role: WORKER.\nYOUR ITEM #{item}: {title}",
                                         command="maxpm go", description="Take work from the queue")
    for n, (name, result, args) in enumerate(steps):
        text += call(name, 8 - 2 * n, result, **args)
    chats[agent] = text + say("One test fails on the subject line; I fix it now." if agent == "email-agent"
                              else "The search box filters the list as you type; next, the empty result.", 3)

conn = core.connect()
for agent, text in chats.items():
    sid = str(uuid.uuid4())
    folder = core.transcripts_root() / ("-" + str(shop).strip("/").replace("/", "-"))
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{sid}.jsonl").write_text(text)
    core.record_claude_session(conn, agent, sid)
with core.tx(conn):
    conn.execute(f"UPDATE agents SET pid=NULL, host=NULL, pid_cmd=NULL WHERE name IN ({','.join('?' * len(chats))})",
                 list(chats))
# The other agent sessions of the seed (build-agent, and the one the go briefing of seed/shoot.js named) hold nothing
# and have no pane: they ended.
for r in conn.execute("SELECT name FROM agents WHERE kind='ai'").fetchall():
    if r["name"] not in chats and not conn.execute("SELECT 1 FROM items WHERE assignee=? AND status IN ('in_progress','held')",
                                                    (r["name"],)).fetchone():
        core.unregister(conn, r["name"])
conn.close()
