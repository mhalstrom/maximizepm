"""drive.py <T> <label> <item> [model] [effort]: one end-to-end run of an agent on the test queue <T> (#6399).

It starts the agent for <item> through the test serve (maxpm launch, as Dispatch does) and follows the session in the
queue: maxpm go ran, the item claimed, the item done, maxpm wait, a question answered, maxpm stop. It answers no card
of the agent (trust, approval): those are the person's, and a step that waits at one fails with the card's text.
Each step goes with its time to stdout and to <T>/<tag>.log. It only reads the queue file; it writes through maxpm.
With FOLLOW=<session> in the environment it follows that session instead of a launch. OPTIONS='key=value ...' adds
launch settings (maxpm launch --option), as a person sets them in the launch dialog.

<label> is a launch_agents label ('Claude Code', 'GitHub Copilot CLI'); '-' leaves model or effort out.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request

T, LABEL, ITEM = sys.argv[1], sys.argv[2], int(sys.argv[3])
MODEL = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] != "-" else None
EFFORT = sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] != "-" else None
HERE = os.path.dirname(os.path.abspath(__file__))
TAG = re.sub(r"\W+", "-", LABEL.lower()).strip("-")
LOG = open(os.path.join(T, f"{TAG}.log"), "a", buffering=1)
T0 = time.time()
CARDS = []


def log(*a):
    line = f"[{time.time() - T0:6.0f}s] " + " ".join(str(x) for x in a)
    print(line, flush=True)
    LOG.write(line + "\n")


def q(*args):
    r = subprocess.run(["bash", os.path.join(HERE, "q.sh"), T, *args], capture_output=True, text=True, timeout=120)
    return (r.stdout + r.stderr).strip()


def db(sql, *p):
    c = sqlite3.connect(f"file:{T}/q.db?mode=ro", uri=True, timeout=10)
    try:
        return c.execute(sql, p).fetchall()
    finally:
        c.close()


PORT = int(q("config", "get", "serve_port").split('"value": "')[-1].split('"')[0])


def session(name):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/sessions", timeout=30) as r:
            return next((s for s in json.loads(r.read())["sessions"] if s.get("name") == name), None)
    except Exception as e:  # noqa: BLE001 - a test driver: log and go on
        log("api error", e)
        return None


def cards(name):
    """Log each new card (trust, approval) that the session shows on the page. It never answers one."""
    s = session(name)
    for w in (s or {}).get("waiting") or []:
        if w.get("options") and w.get("sig") not in [c[1] for c in CARDS]:
            log("CARD", repr(w.get("question")), w.get("options"), "command:", w.get("command"))
            CARDS.append((time.time(), w.get("sig")))
    return s


def at_card(name):
    return CARDS and time.time() - CARDS[-1][0] > 90 and ((cards(name) or {}).get("waiting"))


def until(what, fn, name, limit):
    if at_card(name):
        log("SKIP", what, "(the session waits at a card)")
        return None
    end = time.time() + limit
    while time.time() < end:
        got = fn()
        if got:
            log("OK", what, got if got is not True else "")
            return got
        cards(name)
        if at_card(name):
            log("FAIL", what, "(the session waits at a card that a person answers)")
            return None
        time.sleep(5)
    s = session(name)
    log("FAIL", what, f"(nothing in {limit}s)", "state:", s and s.get("state"), "doing:", s and s.get("doing"))
    return None


# 1. maxpm launch (Dispatch)
if os.environ.get("FOLLOW"):
    NAME = os.environ["FOLLOW"]
    log("FOLLOW", NAME)
else:
    args = ["launch", "--item", str(ITEM), "--agent", LABEL] + (["--model", MODEL] if MODEL else []) + (
        ["--effort", EFFORT] if EFFORT else [])
    for opt in os.environ.get("OPTIONS", "").split():  # launch settings, as the dialog sets them: force=on
        args += ["--option", opt]
    out = q(*args)
    log("LAUNCH", " ".join(args), "->", out.splitlines()[0] if out else "")
    m = re.search(r" as (\S+) in ", out)
    if not m:
        log("FAIL launch", out)
        sys.exit(1)
    NAME = m.group(1)
log("session", NAME, "model, agent type:", db("SELECT model, agent_type FROM agents WHERE name=?", NAME))

# 2-4. maxpm go names it; it claims and closes its item (the history, so a claim between two looks counts too)
until("maxpm go ran", lambda: db("SELECT 1 FROM agents WHERE name=? AND go_seen IS NOT NULL", NAME), NAME, 300)
until("claimed", lambda: db("SELECT at FROM events WHERE item_id=? AND actor=? AND change LIKE 'claimed%'", ITEM, NAME),
      NAME, 300)
until("done", lambda: db("SELECT output FROM items WHERE id=? AND status='done'", ITEM), NAME, 600)
folder = (db("SELECT p.path FROM items i JOIN projects p ON p.id=i.project_id WHERE i.id=?", ITEM) or [[None]])[0][0]
if folder:
    log("git log:", subprocess.run(["git", "-C", folder, "log", "--all", "--oneline", "-3"], capture_output=True,
                                   text=True).stdout.replace("\n", " | "))
log("history:", db("SELECT substr(at, 12, 8), actor, substr(change, 1, 90) FROM events WHERE item_id=? ORDER BY at", ITEM))

# 5. maxpm wait
until("waits (maxpm wait)", lambda: db("SELECT waiting_since FROM agents WHERE name=? AND waiting_since IS NOT NULL",
                                       NAME), NAME, 300)

# 6. a message reaches it
q("register", "tester", "--human")
sent = q("--as", "tester", "send", "question", "--to", NAME,
         "Test message: answer it with maxpm answer and the text 'pong', then wait again.")
log("SENT", sent.splitlines()[0] if sent else "")
mid = re.search(r"#(\d+)", sent)
if mid:
    mid = int(mid.group(1))
    until("message answered", lambda: db("SELECT body FROM messages WHERE reply_to=? AND from_agent=?", mid, NAME),
          NAME, 360)
    log("message:", db("SELECT state, read_at, delivered_via, native_status FROM messages WHERE id=?", mid))

# 7. maxpm stop ends it: its row leaves the queue (serve finishes a stop of a session idle at its prompt)
st = q("--as", "tester", "stop", NAME, "--reason", "end of the end-to-end test run")
log("STOP", st.splitlines()[0] if st else "")
CARDS.clear()
until("session ended", lambda: not db("SELECT 1 FROM agents WHERE name=?", NAME), None, 420)
log("END")
