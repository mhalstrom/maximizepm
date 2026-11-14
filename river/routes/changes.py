"""The Changes view (#5855): a session's diff, an item's or a release's commits, and the person's answer (comments to
the agent, Reviewed, Approve, Request changes). The work is in river/review.py.

GET /api/diff?session=<name>&base=origin|uncommitted|review, or ?item=<id>. OPS: review_send, review_mark."""

from .. import core, review
from ..core import RiverError


def diff(h, path):
    from urllib.parse import parse_qs, urlsplit
    q = {k: v[0] for k, v in parse_qs(urlsplit(h.path).query).items()}
    conn = core.connect()
    try:
        if q.get("item", "").isdigit():
            return h._send(200, review.item_diff(conn, int(q["item"])))
        if q.get("session"):
            return h._send(200, review.session_diff(conn, q["session"], q.get("base") or "origin"))
        return h._send(400, {"error": "name a session (?session=) or an item (?item=)"})
    except RiverError as e:
        return h._send(409, {"error": str(e)})
    finally:
        conn.close()


def _send(c, a, who):
    item = int(a["item"]) if str(a.get("item") or "").isdigit() else None
    out = review.send(c, a.get("comments") or [], agent=a.get("session") or None, item=item, actor=who,
                      request=bool(a.get("request")))
    if a.get("request"):
        review.mark(c, "changes", agent=a.get("session") or None, item=item, sha=a.get("sha") or None, actor=who)
    return out


def _mark(c, a, who):
    item = int(a["item"]) if str(a.get("item") or "").isdigit() else None
    return review.mark(c, a.get("kind") or "", agent=a.get("session") or None, item=item, sha=a.get("sha") or None,
                       actor=who)


GET = [
    (lambda path: path == "/api/diff", diff),
]

OPS = {
    "review_send": _send,
    "review_mark": _mark,
}
