"""Canvases in the page (#4768): a version of a canvas at /canvas/<id>/<version> (or latest), and its versions at
/canvas/<id>/meta, for the side panel's canvas tab and for full screen (/view). (#4656: each view has its routes
here; server.py keeps the frame.)

A canvas is a page that a session wrote, so maxpm treats it as a stranger:
- it comes with a full-screen key for "canvas:<id>" (GET /api/view-key with the page's token), never with the
  page's token: the page can read its own address, and the key opens only this canvas;
- Content-Security-Policy: sandbox allow-scripts, without allow-same-origin: it runs in an origin of its own, so it
  cannot read maxpm's page, its token, its storage or its API, and a POST from it has Origin: null, which serve
  refuses;
- the setting canvas_network: on (any host), off (nothing from the network), or a list of hosts;
- Referrer-Policy: no-referrer: a load from another host does not carry the key in the Referer.

GET: (test, handler) pairs that Handler.do_GET tries in order; a handler answers with h._send. OPS: page actions,
merged into server.OPS."""

import re

from .. import core, server  # noqa: F401  (server: read at call time, so tests that replace its globals still work)
from ..core import RiverError

PATH = re.compile(r"^/canvas/c?(\d+)/(latest|meta|\d+)$")

# Added to an HTML canvas: it tells the panel where the page is scrolled, and scrolls there again after the panel
# loads a new version, so a live reload keeps the place the person reads (#4768). It talks only to its parent.
HELPER = """<script>/* maxpm canvas helper (#4768) */(function () {
  var t, P = window.parent;
  if (!P || P === window) return;
  addEventListener("scroll", function () { clearTimeout(t); t = setTimeout(function () {
    P.postMessage({ maxpmCanvas: "scroll", x: scrollX, y: scrollY }, "*"); }, 120); }, { passive: true });
  addEventListener("message", function (e) { var d = e.data || {};
    if (e.source === P && d.maxpmCanvas === "scrollTo") scrollTo(d.x || 0, d.y || 0); });
  P.postMessage({ maxpmCanvas: "ready" }, "*");
})();</script>"""


def csp(conn, project_id):
    """The Content-Security-Policy of a canvas of this project, from the setting canvas_network."""
    hosts = core.canvas_hosts(core.setting(conn, "canvas_network", project_id=project_id))
    rules = ["sandbox allow-scripts", "frame-ancestors 'self'"]
    if hosts is not None:
        src = " ".join(hosts)
        rules += ["default-src 'none'", f"script-src 'unsafe-inline' 'unsafe-eval' {src}".strip(),
                  f"style-src 'unsafe-inline' {src}".strip(), f"img-src data: blob: {src}".strip(),
                  f"font-src data: {src}".strip(), f"media-src data: blob: {src}".strip(),
                  f"connect-src {src or chr(39) + 'none' + chr(39)}", "form-action 'none'"]
    return "; ".join(rules)


def with_helper(body):
    """The HTML of a canvas with the helper script before its last </body>, else at its end."""
    i = body.lower().rfind("</body>")
    return body[:i] + HELPER + body[i:] if i >= 0 else body + HELPER


def meta(conn, cid):
    """A canvas without its text: kind, title, item, the versions (newest last) and the comments."""
    d = core.canvas_show(conn, cid)
    d.pop("shown", None)
    return d


def canvas(h, path):
    from urllib.parse import parse_qs, urlsplit
    m = PATH.match(path)
    q = {k: v[0] for k, v in parse_qs(urlsplit(h.path).query).items()}
    conn = core.connect()
    try:
        cid, what = int(m.group(1)), m.group(2)
        if not server.view_ok(conn, f"canvas:{cid}", q.get("e"), q.get("k")):
            return h._send(403, {"error": "this canvas link has ended or is not valid: open the canvas again"})
        if what == "meta":
            return h._send(200, meta(conn, cid))
        c, v = core.canvas_version(conn, cid, what)
        headers = {"Content-Security-Policy": csp(conn, c["project_id"]), "X-Content-Type-Options": "nosniff",
                   "Referrer-Policy": "no-referrer"}
        if c["kind"] == "html":
            return h._send(200, with_helper(v["body"]).encode(), "text/html; charset=utf-8", headers)
        if c["kind"] == "svg":
            return h._send(200, v["body"].encode(), "image/svg+xml", headers)
        return h._send(200, v["body"].encode(), "text/plain; charset=utf-8", headers)  # md: the page draws it
    except (RiverError, ValueError) as e:
        return h._send(404, {"error": str(e)})
    finally:
        conn.close()


GET = [
    (lambda path: bool(PATH.match(path)), canvas),
]

OPS = {}
