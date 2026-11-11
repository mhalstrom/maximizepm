"""The You view of Sessions (the Board's until #5102): Needs you, the inbox, threads, prompts to copy, and the
actions of the page's work controls (messages, pushes, takeovers, Claim next, Register, Close finished, a drop on a
session). (#4656: each view has its routes here; server.py keeps the frame.)

GET: (test, handler) pairs that Handler.do_GET tries in order; a handler answers with h._send. OPS: page actions,
merged into server.OPS."""

from .. import core, server  # noqa: F401  (server: read at call time, so tests that replace its globals still work)
from ..core import RiverError  # noqa: F401
import hmac  # noqa: F401


def needs_you(h, path):
    from urllib.parse import parse_qs, urlsplit
    q = {k: v[0] for k, v in parse_qs(urlsplit(h.path).query).items()}
    conn = core.connect()
    try:
        return h._send(200, {"events": server.needs_you_view(conn, q.get("human"))})
    finally:
        conn.close()

def prompt(h, path):
    from urllib.parse import parse_qs, urlsplit
    q = {k: v[0] for k, v in parse_qs(urlsplit(h.path).query).items()}
    conn = core.connect()
    try:
        if path == "/api/prompt-all":
            return h._send(200, {"prompt": core.prompt_for_all(conn, q.get("person") or None)})
        return h._send(200, {"prompt": core.prompt_for(conn, int(path.split("/")[3]), q.get("person") or None)})
    except (RiverError, ValueError) as e:
        return h._send(404, {"error": str(e)})
    finally:
        conn.close()

def inbox(h, path):
    from urllib.parse import parse_qs, urlsplit
    q = {k: v[0] for k, v in parse_qs(urlsplit(h.path).query).items()}
    conn = core.connect()
    try:
        # The page only looks: messages turn read when the person acts on them (Mark read, answer, reply).
        return h._send(200, {"messages": core.inbox(conn, q.get("agent"), bool(q.get("all")), mark_read=False)})
    except RiverError as e:
        return h._send(400, {"error": str(e)})
    finally:
        conn.close()

def thread(h, path):
    conn = core.connect()
    try:
        return h._send(200, core.thread(conn, int(path.rsplit("/", 1)[1])))
    except (RiverError, ValueError) as e:
        return h._send(404, {"error": str(e)})
    finally:
        conn.close()


GET = [
    (lambda path: path == "/api/needs-you", needs_you),
    (lambda path: path == "/api/prompt-all" or (path.startswith("/api/item/") and path.endswith("/prompt")), prompt),
    (lambda path: path == "/api/inbox", inbox),
    (lambda path: path.startswith("/api/thread/"), thread),
]

OPS = {
    "push": lambda c, a, who: core.push(c, a["id"], a["to"], a.get("note"), who),
    "push_cancel": lambda c, a, who: core.cancel_push(c, a["id"], who),
    "undo_takeover": lambda c, a, who: core.undo_takeover(c, a["id"], who),
    "takeover_seen": lambda c, a, who: core.takeover_seen(c, a["id"], who),
    "next_claim": lambda c, a, who: core.next_item(c, a.get("project"), a.get("unblocks"), True, who, 1, a.get("near"),
                                                   bool(a.get("mine"))),
    "register": lambda c, a, who: core.register(c, a["name"], bool(a.get("human")), a.get("note", "")),
    # An asker that waits at its prompt gets the answer typed there at once, not at the next pass (#5303).
    "answer": lambda c, a, who: (core.answer(c, int(a["msg"]), a["body"], who), server.paste_answers(c))[0],
    "message_read": lambda c, a, who: server._message_read(c, int(a["msg"])),
    "send": lambda c, a, who: core.send(c, a["kind"], a["body"], a.get("to"), a.get("item"), a.get("reply"), who,
                                        a.get("level"), bool(a.get("blocked"))),
    "give": lambda c, a, who: core.give(c, int(a["id"]), a["to"], who),
    "split": lambda c, a, who: core.split(c, int(a["id"]), [t for t in a["titles"] if t.strip()], who),
    "queue_add": lambda c, a, who: core.queue_add(c, a["agent"], a.get("id"), a.get("message"), bool(a.get("first")),
                                                  a.get("before"), who),
    "tmux_tidy": lambda c, a, who: server.tmux_tidy(c),
    "decline_message": lambda c, a, who: core.decline_message(c, int(a["msg"]), a.get("note"), who),
}
