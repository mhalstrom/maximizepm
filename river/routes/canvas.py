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
from pathlib import Path

from .. import core, server  # noqa: F401  (server: read at call time, so tests that replace its globals still work)
from ..core import RiverError

PATH = re.compile(r"^/canvas/c?(\d+)/(latest|meta|\d+)$")

# Added to an HTML canvas. It talks only to its parent (the side panel, or /view):
# - scroll: it says where the page is scrolled, and scrolls there again after a new version loads, so a live reload
#   keeps the place the person reads (#4768);
# - comment mode (#4769, as annotatetofix): it marks the part under the mouse, and a click sends that part (a short
#   CSS path, its text, and the selected text) to the panel, which opens a comment box for it; Escape ends the mode;
# - show: it scrolls to the part of a comment and marks it for a moment;
# - find (#4877): it marks each place of the words that the panel's find box asks for (the code of
#   static/components/findText.js, put inline here), goes to the next or previous one, and says "n of m"; after the
#   panel says it finds ("finder"), Cmd+F or Ctrl+F in the canvas opens the panel's find box (in /view the browser's
#   own find stays).
FIND_JS = re.sub(r"^export ", "", (Path(__file__).resolve().parent.parent / "static" / "components" / "findText.js")
                 .read_text(encoding="utf-8"), flags=re.M)
HELPER = """<script>/* maxpm canvas helper (#4768, #4769, #4877) */(function () {
  var t, P = window.parent, mode = false, hi = null, finder = false, fq = "", fm = [], fi = 0;
  if (!P || P === window) return;
""" + FIND_JS + """
  var css = document.createElement("style");
  css.textContent = ".maxpm-hi{outline:2px solid #1f6feb!important;outline-offset:2px!important;cursor:crosshair!important}"
    + ".maxpm-flash{outline:3px solid #d29922!important;outline-offset:3px!important}"
    + "mark.maxpm-find{background:#ffe066!important;color:#000!important;border-radius:2px}"
    + "mark.maxpm-find-on{background:#ff9632!important;outline:2px solid #ff9632!important}";
  (document.head || document.documentElement).appendChild(css);
  function path(el) {
    var parts = [];
    while (el && el.nodeType === 1 && el !== document.body && el !== document.documentElement && parts.length < 8) {
      if (el.id) { parts.unshift("#" + CSS.escape(el.id)); break; }
      var i = 1, s = el;
      while ((s = s.previousElementSibling)) if (s.tagName === el.tagName) i++;
      parts.unshift(el.tagName.toLowerCase() + ":nth-of-type(" + i + ")");
      el = el.parentElement;
    }
    return parts.join(" > ") || "body";
  }
  function mark(el) { if (hi) hi.classList.remove("maxpm-hi"); hi = el; if (el) el.classList.add("maxpm-hi"); }
  addEventListener("scroll", function () { clearTimeout(t); t = setTimeout(function () {
    P.postMessage({ maxpmCanvas: "scroll", x: scrollX, y: scrollY }, "*"); }, 120); }, { passive: true });
  addEventListener("mouseover", function (e) { if (mode) mark(e.target); }, true);
  addEventListener("click", function (e) {
    if (!mode) return;
    e.preventDefault(); e.stopPropagation();
    var el = e.target, quote = String(getSelection() || "").trim();
    mark(null);
    P.postMessage({ maxpmCanvas: "pick", path: path(el), text: (el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim().slice(0, 200),
                    quote: quote.slice(0, 500) }, "*");
  }, true);
  addEventListener("keydown", function (e) {
    if (mode && e.key === "Escape") P.postMessage({ maxpmCanvas: "escape" }, "*");
    if (finder && (e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && (e.key === "f" || e.key === "F")) {
      e.preventDefault();
      P.postMessage({ maxpmCanvas: "find-open", text: String(getSelection() || "").trim().slice(0, 200) }, "*");
    }
  });
  addEventListener("message", function (e) {
    var d = e.data || {};
    if (e.source !== P) return;
    if (d.maxpmCanvas === "scrollTo") scrollTo(d.x || 0, d.y || 0);
    if (d.maxpmCanvas === "mode") { mode = !!d.on; if (!mode) mark(null); }
    if (d.maxpmCanvas === "show") {
      var el = null;
      try { el = d.path ? document.querySelector(d.path) : null; } catch (x) { el = null; }
      if (el) { el.scrollIntoView({ block: "center", behavior: "smooth" }); el.classList.add("maxpm-flash");
        setTimeout(function () { el.classList.remove("maxpm-flash"); }, 1600); }
      P.postMessage({ maxpmCanvas: "shown", found: !!el }, "*");
    }
    if (d.maxpmCanvas === "finder") finder = true;
    if (d.maxpmCanvas === "find") {
      // step 0: mark again (new words go to the first place; again: the same words after a change, no scroll).
      var q = String(d.q || "").slice(0, 200), step = +d.step || 0, root = document.body || document.documentElement;
      finder = true;
      if (step === 0 || q !== fq || !fm.length || !fm[0].isConnected) {
        var keep = q === fq && d.again ? fi : 0;
        fm = findMark(root, q, null); fi = findGo(fm, keep, !d.again); fq = q;
      } else fi = findGo(fm, fi + step, true);
      P.postMessage({ maxpmCanvas: "found", q: fq, n: fm.length, i: fi }, "*");
    }
  });
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


def with_kit(conn, body):
    """An HTML canvas that asks for the kit (#4983, <meta name="maxpm-kit">): the kit's styles first in its head (the
    page's own styles come after, so they win), and before its last </body> the states of the queue items it names
    (read now, so a task graph shows them as they are) and the kit's script."""
    import json
    css = (core.CANVAS_KIT_DIR / "kit.css").read_text(encoding="utf-8")
    js = (core.CANVAS_KIT_DIR / "kit.js").read_text(encoding="utf-8")
    items = json.dumps(core.canvas_items(conn, body)).replace("</", "<\\/")
    style = f"<style>{css}</style>"
    m = re.search(r"<head\b[^>]*>", body, re.I)
    body = body[:m.end()] + style + body[m.end():] if m else style + body
    tail = f'<script type="application/json" id="maxpm-items">{items}</script><script>{js}</script>'
    i = body.lower().rfind("</body>")
    return body[:i] + tail + body[i:] if i >= 0 else body + tail


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
            body = with_kit(conn, v["body"]) if core.uses_kit(v["body"]) else v["body"]
            return h._send(200, with_helper(body).encode(), "text/html; charset=utf-8", headers)
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
