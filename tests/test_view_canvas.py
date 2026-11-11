"""Canvases in the page (#4768): /canvas/<id>/<version> in a sandbox of its own origin, network per canvas_network,
the canvas tab of the side panel, live reload, the Artifacts lists, and full screen."""
import json
import os
import tempfile
import time
import unittest
from urllib.parse import parse_qs, urlsplit

from river import core, server
from river.routes import canvas as canvas_route


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["MAXPM_DB"] = os.path.join(self.dir.name, "t.db")
        self.c = core.connect()
        core.project_add(self.c, "web")
        core.register(self.c, "ag")
        self.it = core.item_add(self.c, "web", "the plan")["id"]
        core.claim(self.c, self.it, "ag")
        self.cv = core.canvas_put(self.c, "plan", "<html><body><h1>Plan</h1></body></html>", "ag", kind="html",
                                  title="Release plan")["id"]

    def tearDown(self):
        self.c.close()
        os.environ.pop("MAXPM_DB", None)
        self.dir.cleanup()

    def get(self, path):
        h = server.Handler.__new__(server.Handler)
        h.path, h.client_address, h.headers, out = path, ("127.0.0.1", 5555), {"Host": "127.0.0.1:8765"}, {}

        def send(code, body, ctype="application/json", headers=None):
            out.update(code=code, body=body, ctype=ctype, headers=headers or {})
        h._send = send
        h.do_GET()
        return out

    def key(self, target=None):
        out = self.get(f"/api/view-key?target={target or f'canvas:{self.cv}'}&session=ag&item={self.it}"
                       f"&token={server.PAGE_TOKEN}")
        self.assertEqual(out["code"], 200, out)
        q = parse_qs(urlsplit(out["body"]["url"]).query)
        return out["body"]["url"], f"e={q['e'][0]}&k={q['k'][0]}"


class CanvasRoutes(Base):
    def test_a_version_needs_its_own_key_not_the_page_token(self):
        self.assertEqual(self.get(f"/canvas/{self.cv}/latest")["code"], 403)
        self.assertEqual(self.get(f"/canvas/{self.cv}/latest?token={server.PAGE_TOKEN}")["code"], 403,
                         "the page's token never goes into a canvas address: the canvas could read it")
        _, sig = self.key()
        out = self.get(f"/canvas/{self.cv}/latest?{sig}")
        self.assertEqual((out["code"], out["ctype"]), (200, "text/html; charset=utf-8"))
        self.assertIn(b"<h1>Plan</h1>", out["body"])
        self.assertIn(b"maxpm canvas helper", out["body"])
        self.assertLess(out["body"].index(b"maxpm canvas helper"), out["body"].index(b"</body>"))
        self.assertIn(b"function findMark(", out["body"])  # #4877: the find code of static/components/findText.js
        self.assertNotIn(b"export function", out["body"])
        self.assertEqual(out["headers"]["Referrer-Policy"], "no-referrer")
        self.assertEqual(out["headers"]["X-Content-Type-Options"], "nosniff")
        # Another canvas's key opens nothing here.
        other = core.canvas_put(self.c, "other", "<p>x</p>", "ag", kind="html")["id"]
        _, other_sig = self.key(f"canvas:{other}")
        self.assertEqual(self.get(f"/canvas/{self.cv}/latest?{other_sig}")["code"], 403)
        # An ended key opens nothing.
        end = int(time.time()) - 1
        self.assertEqual(self.get(f"/canvas/{self.cv}/latest?e={end}&k={server.view_key(self.c, f'canvas:{self.cv}', end)}")["code"], 403)

    def test_versions_meta_and_kinds(self):
        core.canvas_put(self.c, "plan", "<html><body><h1>Plan two</h1></body></html>", "ag", note="two")
        _, sig = self.key()
        self.assertIn(b"Plan two", self.get(f"/canvas/{self.cv}/latest?{sig}")["body"])
        self.assertIn(b"<h1>Plan</h1>", self.get(f"/canvas/{self.cv}/1?{sig}")["body"])
        self.assertEqual(self.get(f"/canvas/{self.cv}/9?{sig}")["code"], 404)
        meta = self.get(f"/canvas/{self.cv}/meta?{sig}")["body"]
        self.assertEqual((meta["version"], meta["title"], meta["kind"]), (2, "Release plan", "html"))
        self.assertEqual([(v["version"], v["note"]) for v in meta["versions"]], [(1, None), (2, "two")])
        self.assertNotIn("shown", meta, "meta carries no text")
        md = core.canvas_put(self.c, "notes", "# Notes\n<script>x</script>", "ag", kind="md")["id"]
        _, msig = self.key(f"canvas:{md}")
        out = self.get(f"/canvas/{md}/latest?{msig}")
        self.assertEqual((out["ctype"], out["body"]), ("text/plain; charset=utf-8", b"# Notes\n<script>x</script>"))
        svg = core.canvas_put(self.c, "chart", "<svg xmlns='http://www.w3.org/2000/svg'/>", "ag", kind="svg")["id"]
        _, ssig = self.key(f"canvas:{svg}")
        self.assertEqual(self.get(f"/canvas/{svg}/latest?{ssig}")["ctype"], "image/svg+xml")
        core.canvas_delete(self.c, "notes", "ag")
        self.assertEqual(self.get(f"/canvas/{md}/latest?{msig}")["code"], 404)

    def test_the_sandbox_and_the_setting_canvas_network(self):
        _, sig = self.key()
        csp = lambda: self.get(f"/canvas/{self.cv}/latest?{sig}")["headers"]["Content-Security-Policy"]
        self.assertEqual(csp(), "sandbox allow-scripts; frame-ancestors 'self'", "on: any host, still its own origin")
        self.assertNotIn("allow-same-origin", csp())
        core.config_set(self.c, "canvas_network", "off")
        off = csp()
        self.assertIn("default-src 'none'", off)
        self.assertIn("connect-src 'none'", off)
        self.assertIn("script-src 'unsafe-inline' 'unsafe-eval';", off)
        core.config_set(self.c, "canvas_network", "cdn.jsdelivr.net, cdnjs.cloudflare.com", project="web")
        hosts = csp()
        self.assertIn("script-src 'unsafe-inline' 'unsafe-eval' cdn.jsdelivr.net cdnjs.cloudflare.com;", hosts)
        self.assertIn("connect-src cdn.jsdelivr.net cdnjs.cloudflare.com", hosts)
        with self.assertRaisesRegex(core.RiverError, "canvas_network is on, off, or hosts"):
            core.config_set(self.c, "canvas_network", "a b/c")

    def test_full_screen(self):
        url, _ = self.key()
        self.assertTrue(url.startswith("/view?"))
        out = self.get(url)
        self.assertEqual(out["code"], 200)
        page = out["body"].decode()
        self.assertIn("<title>Release plan · MaximizePM</title>", page)
        self.assertIn(f"src='/canvas/{self.cv}/latest?e=", page)
        self.assertIn("sandbox='allow-scripts'", page)
        self.assertIn("data-v='1'", page)
        self.assertIn("from ag", page)
        with self.assertRaisesRegex(core.RiverError, "no canvas"):
            server.view_url(self.c, "canvas:999")
        bad = self.get(f"/api/view-key?target=canvas:999&token={server.PAGE_TOKEN}")
        self.assertEqual(bad["code"], 409)

    def test_the_state_and_the_artifacts_lists_have_the_canvases_first(self):
        st = core.state(self.c)
        self.assertEqual([(x["id"], x["version"], x["agent"], x["title"]) for x in st["canvases"]],
                         [(self.cv, 1, "ag", "Release plan")])
        self.assertNotIn("body", st["canvases"][0])
        for path in ("/api/artifacts", "/api/artifacts?agent=ag"):
            rows = self.get(path)["body"]["artifacts"]
            self.assertEqual(rows[0]["target"], f"canvas:{self.cv}", path)
            self.assertEqual((rows[0]["kind"], rows[0]["version"], rows[0]["title"], rows[0]["item"]),
                             ("canvas", 1, "Release plan", self.it))
        core.register(self.c, "other")
        self.assertEqual(self.get("/api/artifacts?agent=other")["body"]["artifacts"], [],
                         "a session's list holds the canvases it wrote")


if __name__ == "__main__":
    unittest.main()


class CanvasOpen(Base):
    """#4783: maxpm canvas open asks maxpm serve; the state carries the request for the page."""

    def setUp(self):
        super().setUp()
        server.CANVAS_OPENS.clear()
        self.addCleanup(server.CANVAS_OPENS.clear)

    def test_the_request_goes_into_the_state_for_a_while(self):
        core.register(self.c, "mark", human=True)
        got = server.OPS["canvas_open"](self.c, {"name": "plan"}, "ag")
        self.assertEqual((got["canvas"], got["session"], got["opened"]), ("plan", "ag", True))
        other = server.OPS["canvas_open"](self.c, {"name": "plan", "session": "ag"}, "mark")
        self.assertEqual(other["seq"], got["seq"] + 1)
        st = self.get("/api/state")["body"]
        self.assertEqual([(x["canvas"], x["session"], x["by"], x["title"]) for x in st["canvas_opens"]],
                         [(self.cv, "ag", "ag", "Release plan"), (self.cv, "ag", "mark", "Release plan")])
        with self.assertRaisesRegex(core.RiverError, "no agent nobody"):
            server.OPS["canvas_open"](self.c, {"name": "plan", "session": "nobody"}, "ag")
        with self.assertRaisesRegex(core.RiverError, "no canvas"):
            server.OPS["canvas_open"](self.c, {"name": "nope"}, "ag")
        for x in server.CANVAS_OPENS:
            x["at"] -= server.CANVAS_OPEN_KEEP + 1
        self.assertEqual(server.canvas_opens(), [])

    def test_the_command_and_the_mcp_tool_ask_maxpm_serve(self):
        from unittest import mock
        import contextlib
        import io
        from river import cli, mcp
        asked = []
        with mock.patch.object(cli, "ask_server", lambda conn, op, args, actor=None, **k: asked.append((op, args, actor))
                               or {"canvas": "plan", "id": self.cv, "session": "ag", "seq": 1, "opened": True}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.run(["-q", "--as", "ag", "canvas", "open", "plan"])
        self.assertEqual(asked, [("canvas_open", {"name": "plan", "project": None, "session": None}, "ag")])
        self.assertIn("canvas plan opens in the side panel of ag", out.getvalue())
        self.assertEqual(mcp.canvas_argv({"action": "open", "name": "plan", "session": "ag"}),
                         ["canvas", "open", "plan", "--session=ag"])


class ChromePanel(unittest.TestCase):
    """The Sessions tab on session ag, in Chrome (tests/test_view_sessions.drive_page)."""

    def setUp(self):
        from tests import test_view_sessions as tvs
        self.tvs = tvs
        self.base = tvs.Base("run")
        self.base.setUp()
        self.addCleanup(self.base.tearDown)

    def put(self, text):
        def run():
            c = core.connect()
            try:
                return core.canvas_put(c, "plan", text, "ag", kind="html", title="Release plan")["version"]
            finally:
                c.close()
        return run


class PanelCanvas(ChromePanel):
    """The canvas tab of the side panel: a canvas that the shown session makes opens by itself, a new version loads
    in place, and the version menu shows an older one."""

    def test_a_new_canvas_opens_and_follows_its_newest_version(self):
        driver = self.tvs.DRIVE_HEAD + r'''
try {
  await until(() => q("#sessTitle").textContent === "ag" && /updated/.test(q("#stamp").textContent), "the session ag");
  await sleep(400);
  await fetch("/__put1");
  await until(() => q(".sp-cver") && q("iframe.sp-canvas"), "the new canvas in a tab of the panel");
  const f = q("iframe.sp-canvas");
  const out = { tab: q(".sp-tab.on").textContent.trim(), v: q(".sp-cver").value, src: f.getAttribute("src"),
                sandbox: f.getAttribute("sandbox") };
  await fetch("/__put2");
  await until(() => q(".sp-cver") && q(".sp-cver").value === "2", "version 2 in place");
  out.options = [...q(".sp-cver").options].map(o => o.value);
  const sel = q(".sp-cver"); sel.value = "1"; sel.dispatchEvent(new Event("change"));
  await until(() => q("[data-cv-newest]") && /\/1\?/.test(q("iframe.sp-canvas").getAttribute("src")), "version 1");
  out.pinned = q("iframe.sp-canvas").getAttribute("src");
  post(out);
} catch (e) { post({ error: String(e.stack || e) }); }
'''
        got = self.tvs.drive_page(self.base, driver, routes={"/__put1": self.put("<body><h1>One</h1></body>"),
                                                             "/__put2": self.put("<body><h1>Two</h1></body>")})
        self.assertNotIn("error", got, got)
        self.assertIn("Release plan", got["tab"])
        self.assertEqual((got["v"], got["sandbox"]), ("1", "allow-scripts"))
        self.assertRegex(got["src"], r"^canvas/\d+/latest\?e=\d+&k=")
        self.assertNotIn("token", got["src"])
        self.assertEqual(got["options"], ["2", "1"])
        self.assertRegex(got["pinned"], r"^canvas/\d+/1\?")

    def test_canvas_open_shows_an_existing_canvas_and_offers_another_sessions(self):
        """#4783: a canvas that existed before the page loaded opens on request; one for another session waits in
        that session's panel, and a bar offers Show."""
        def ask(session):
            def run():
                c = core.connect()
                try:
                    core.register(c, "bo") if session == "bo" else None
                    return server.canvas_open(c, {"name": "plan", "session": session}, "ag")["seq"]
                finally:
                    c.close()
            return run
        server.CANVAS_OPENS.clear()
        self.addCleanup(server.CANVAS_OPENS.clear)
        self.put("<body><h1>Old</h1></body>")()  # before the page loads: it does not open by itself
        driver = self.tvs.DRIVE_HEAD + r"""
try {
  await until(() => q("#sessTitle").textContent === "ag" && /updated/.test(q("#stamp").textContent), "the session ag");
  await sleep(3500);
  const before = !!q("iframe.sp-canvas");
  await fetch("/__open_ag");
  await until(() => q("iframe.sp-canvas"), "the canvas opens on request");
  const out = { before, tab: q(".sp-tab.on").textContent.trim(), bar: !!(q("#cvOffer") && !q("#cvOffer").classList.contains("hidden")) };
  await fetch("/__open_bo");
  await until(() => q("#cvOffer") && !q("#cvOffer").classList.contains("hidden"), "the bar for another session");
  out.text = q("#cvOffer .cv-offer-t").textContent;
  out.stored = JSON.parse(localStorage.getItem("river.sidepanel.tabs") || "{}").bo;
  post(out);
} catch (e) { post({ error: String(e.stack || e) }); }
"""
        got = self.tvs.drive_page(self.base, driver, routes={"/__open_ag": ask("ag"), "/__open_bo": ask("bo")})
        self.assertNotIn("error", got, got)
        self.assertFalse(got["before"])
        self.assertIn("Release plan", got["tab"])
        self.assertFalse(got["bar"], "the Sessions tab shows ag: no bar")
        self.assertEqual(got["text"], 'ag opened the canvas "Release plan" in the side panel of bo.')
        self.assertEqual(got["stored"]["on"], got["stored"]["targets"][-1])
        self.assertRegex(got["stored"]["on"], r"^canvas:\d+$")

class PanelComments(ChromePanel):
    """#4769: comments in the canvas tab, in Chrome: pick a part of a Markdown canvas, a draft, Send all, the
    session's reply, the Markdown editor; the frame's pick of an HTML canvas."""

    def call(self, fn):
        def run():
            c = core.connect()
            try:
                return fn(c)
            finally:
                c.close()
        return run

    def test_comment_on_a_part_send_reply_and_edit(self):
        driver = self.tvs.DRIVE_HEAD + r'''
const chips = () => [...document.querySelectorAll(".cv-c .cv-st")].map(x => x.textContent);
try {
  await until(() => q("#sessTitle").textContent === "ag" && /updated/.test(q("#stamp").textContent), "the session ag");
  q("#actor").value = "mark"; q("#actor").dispatchEvent(new Event("change"));
  await sleep(400);
  await fetch("/__md");
  await until(() => q(".sp-canvas h1") && q(".sp-cside"), "the Markdown canvas and its comments");
  const out = { none: q(".sp-cside").textContent.includes("No comments yet") };
  q("[data-cv-mode]").click();
  out.pressed = q("[data-cv-mode]").getAttribute("aria-pressed");
  q(".sp-canvas h2").click();
  await until(() => q(".cv-compose"), "the comment box");
  out.on = q(".cv-compose .muted").textContent.replace(/\s+/g, " ").trim();
  const ta = q(".cv-compose textarea"); ta.value = "Add the dates"; ta.dispatchEvent(new Event("input", { bubbles: true }));
  out.kept = JSON.parse(localStorage.getItem([...Object.keys(localStorage)].find(k => k.startsWith("river.canvas.draft.")))).text;
  q("[data-cv-draft]").click();
  await until(() => chips().join() === "draft", "the draft");
  q("[data-cv-page]").click();
  await until(() => q(".cv-compose"), "the page comment box");
  q(".cv-compose textarea").value = "Shorter, please"; q("[data-cv-draft]").click();
  await until(() => chips().length === 2, "two drafts");
  out.sendall = q("[data-cv-sendall]").textContent.trim();
  q("[data-cv-sendall]").click();
  await until(() => chips().join() === "sent,sent", "both sent");
  await fetch("/__reply");
  await until(() => chips().includes("answered"), "the session's reply");
  out.reply = q(".cv-r").textContent.replace(/\s+/g, " ").trim();
  q("[data-cv-edit]").click();
  await until(() => q(".cv-edit textarea"), "the editor");
  q(".cv-edit textarea").value = "# Notes\n\n## Dates\n\nMay 1."; q("[data-cv-save]").click();
  await until(() => q(".sp-cver") && q(".sp-cver").value === "2" && q(".sp-canvas h2"), "version 2 by mark");
  out.v2 = q(".sp-canvas").textContent.replace(/\s+/g, " ").trim();
  post(out);
} catch (e) { post({ error: String(e.stack || e) }); }
'''
        def md(c):
            return core.canvas_put(c, "notes", "# Notes\n\n## Dates\n\nSoon.", "ag", kind="md", title="Notes")["id"]

        def reply(c):
            top = [x for x in core.canvas_comments(c, "notes", "ag")["comments"] if x["anchor"]][0]
            return core.canvas_reply(c, top["id"], "Dates are in v2", "ag")["id"]
        got = self.tvs.drive_page(self.base, driver, routes={"/__md": self.call(md), "/__reply": self.call(reply)})
        self.assertNotIn("error", got, got)
        self.assertTrue(got["none"])
        self.assertEqual(got["pressed"], "true")
        self.assertIn("On “Dates”", got["on"])
        self.assertEqual(got["kept"], "Add the dates", "the box's text stays in this browser while the person writes")
        self.assertEqual(got["sendall"], "Send all 2 drafts to the session")
        self.assertIn("Dates are in v2", got["reply"])
        self.assertIn("May 1.", got["v2"])
        c = self.base.c
        threads = core.canvas_show(c, "notes")["comments"]
        self.assertEqual(sorted((x["state"], (x["anchor"] or {}).get("text") or "the page") for x in threads),
                         [("read", "Dates"), ("read", "the page")])  # the session read them before its reply
        self.assertTrue(any("2 comment(s) on canvas notes" in m["body"] for m in core.inbox(c, "ag", mark_read=False)))
        show = core.canvas_show(c, "notes")
        self.assertEqual((show["version"], show["versions"][-1]["author"], show["agent"]), (2, "mark", "ag"))

    def test_the_frame_picks_a_part_of_an_html_canvas(self):
        driver = self.tvs.DRIVE_HEAD + r'''
try {
  await until(() => q("#sessTitle").textContent === "ag" && /updated/.test(q("#stamp").textContent), "the session ag");
  await sleep(400);
  await fetch("/__put1");
  await until(() => q("iframe.sp-canvas") && q("[data-cv-mode]"), "the canvas");
  q("[data-cv-mode]").click();
  const f = q("iframe.sp-canvas");
  // What the helper script in the frame sends after a click on its heading.
  window.dispatchEvent(new MessageEvent("message", { source: f.contentWindow,
    data: { maxpmCanvas: "pick", path: "h1:nth-of-type(1)", text: "One", quote: "On" } }));
  await until(() => q(".cv-compose"), "the comment box");
  // A message from another window is not the frame's.
  window.dispatchEvent(new MessageEvent("message", { source: window, data: { maxpmCanvas: "pick", path: "x", text: "Evil" } }));
  await sleep(200);
  post({ on: q(".cv-compose .muted").textContent.replace(/\s+/g, " ").trim(), pressed: q("[data-cv-mode]").getAttribute("aria-pressed") });
} catch (e) { post({ error: String(e.stack || e) }); }
'''
        got = self.tvs.drive_page(self.base, driver, routes={"/__put1": self.put("<body><h1>One</h1></body>")})
        self.assertNotIn("error", got, got)
        self.assertEqual(got["on"], "On “On” h1:nth-of-type(1)")
        self.assertEqual(got["pressed"], "false", "a pick ends the comment mode")


class PanelFind(ChromePanel):
    """#4877: Find in the side panel marks the words in a Markdown canvas (in this page) and in an HTML canvas (through
    its helper script, in a frame of its own origin), with n of m, Enter, Shift+Enter, Esc and Cmd+F."""

    def call(self, fn):
        def run():
            c = core.connect()
            try:
                return fn(c)
            finally:
                c.close()
        return run

    def test_find_in_a_markdown_and_an_html_canvas(self):
        driver = self.tvs.DRIVE_HEAD + r"""
const n = () => q("#spFindN").textContent;
const key = (el, k, o = {}) => el.dispatchEvent(new KeyboardEvent("keydown", Object.assign({ key: k, bubbles: true, cancelable: true }, o)));
const type = (v) => { q("#spFindQ").value = v; q("#spFindQ").dispatchEvent(new Event("input", { bubbles: true })); };
// A new canvas opens in a tab beside the one the person reads: click it.
const tab = async (name) => { await until(() => [...document.querySelectorAll(".sp-tab-l")].some(b => b.textContent.includes(name)), name);
  [...document.querySelectorAll(".sp-tab-l")].find(b => b.textContent.includes(name)).click(); };
try {
  await until(() => q("#sessTitle").textContent === "ag" && /updated/.test(q("#stamp").textContent), "the session ag");
  await sleep(400);
  await fetch("/__md");
  await until(() => q(".sp-canvas h1"), "the Markdown canvas");
  const out = { hidden: q("#spFind").classList.contains("hidden") };
  q("#spFindBtn").click();
  out.focus = document.activeElement === q("#spFindQ");
  type("relay");
  await until(() => n() === "1 of 3", "three matches");
  out.marks = [...document.querySelectorAll(".sp-canvas mark.maxpm-find")].map(m => m.textContent);
  out.on = q(".sp-canvas mark.maxpm-find-on").textContent;
  key(q("#spFindQ"), "Enter"); out.next = n();
  key(q("#spFindQ"), "Enter", { shiftKey: true }); key(q("#spFindQ"), "Enter", { shiftKey: true }); out.prev = n();
  type("nowhere"); await until(() => n() === "No match", "no match");
  key(q("#spFindQ"), "Escape");
  out.closed = q("#spFind").classList.contains("hidden") && !document.querySelector("mark.maxpm-find");
  out.text = q(".sp-canvas").textContent.replace(/\s+/g, " ").trim();
  // Cmd+F after a click in the panel opens the box again, with the words of before.
  q(".sp-canvas h1").dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
  key(document.body, "f", { metaKey: true });
  out.reopened = !q("#spFind").classList.contains("hidden") && q("#spFindQ").value === "nowhere";
  // An HTML canvas: the helper script in its frame marks and counts.
  type("step");
  await fetch("/__html");
  await tab("Steps");
  await until(() => q("iframe.sp-canvas"), "the HTML canvas");
  await until(() => n() === "1 of 3", "three matches in the frame");
  key(q("#spFindQ"), "Enter"); await until(() => n() === "2 of 3", "the next one in the frame");
  key(q("#spFindQ"), "Enter", { shiftKey: true }); key(q("#spFindQ"), "Enter", { shiftKey: true });
  await until(() => n() === "3 of 3", "round to the last one");
  // A picture (an SVG canvas) is out of reach, and the box says so.
  await fetch("/__svg");
  await tab("Pic");
  await until(() => /cannot read a picture/.test(n()), "the picture");
  post(out);
} catch (e) { post({ error: String(e.stack || e), n: n() }); }
"""
        def md(c):
            return core.canvas_put(c, "notes", "# Relay notes\n\n## The relay\n\nA relay, and **more**.\n", "ag",
                                   kind="md", title="Notes")["id"]

        def html(c):
            return core.canvas_put(c, "steps", "<body><h1>Steps</h1><p>step one, step two</p></body>", "ag",
                                   kind="html", title="Steps")["id"]

        def svg(c):
            return core.canvas_put(c, "pic", "<svg xmlns='http://www.w3.org/2000/svg'><text y='20'>step</text></svg>",
                                   "ag", kind="svg", title="Pic")["id"]
        got = self.tvs.drive_page(self.base, driver, routes={"/__md": self.call(md), "/__html": self.call(html),
                                                             "/__svg": self.call(svg)})
        self.assertNotIn("error", got, got)
        self.assertTrue(got["hidden"])
        self.assertTrue(got["focus"])
        self.assertEqual(got["marks"], ["Relay", "relay", "relay"])
        self.assertEqual(got["on"], "Relay")
        self.assertEqual((got["next"], got["prev"]), ("2 of 3", "3 of 3"))
        self.assertTrue(got["closed"], "Esc closes the box and takes the marks away")
        self.assertEqual(got["text"], "Relay notes The relay edit A relay, and more.", "the text is whole again")
        self.assertTrue(got["reopened"])


DOC = """# Relay

## What it is

writing: what the relay is for

## Decisions

Decision 1 of 2: Where it runs  (item #1)
What you decide: the host.
Your answer: "OVH" or "Cloudflare".

Decision 2 of 2: Price
Your answer: "$5 a month".

```
## inside code
```
"""


class Docs(Base):
    """#4708: a doc is a Markdown canvas written section by section, with decisions answered in place."""

    def setUp(self):
        super().setUp()
        core.register(self.c, "mark", human=True)
        self.doc = core.canvas_put(self.c, "relay", DOC, "ag", kind="md", title="Relay")["id"]

    def body(self):
        return core.canvas_show(self.c, "relay")["shown"]["body"]

    def test_sections_replace_add_and_pending(self):
        self.assertEqual([(s["title"], s["level"]) for s in core.md_sections(DOC)],
                         [("Relay", 1), ("What it is", 2), ("Decisions", 2)], "a heading in code is not a section")
        r = core.canvas_section(self.c, "relay", "what it is", "A relay that shows the page anywhere.", "ag")
        self.assertEqual((r["version"], r["added"], r["section"]), (2, False, "what it is"))
        self.assertIn("## What it is\n\nA relay that shows the page anywhere.\n\n## Decisions", self.body())
        r = core.canvas_section(self.c, "relay", "Costs", None, "ag", pending="the monthly cost")
        self.assertTrue(r["added"])
        self.assertTrue(self.body().endswith("## Costs\n\nwriting: the monthly cost\n"))
        # The text may come with its own heading: it is not doubled.
        core.canvas_section(self.c, "relay", "Costs", "## Costs\n\n$5 a month.", "ag")
        self.assertEqual(self.body().count("## Costs"), 1)
        with self.assertRaisesRegex(core.RiverError, "sections are for a Markdown canvas"):
            core.canvas_section(self.c, "plan", "x", "y", "ag")
        with self.assertRaisesRegex(core.RiverError, "give the section's text"):
            core.canvas_section(self.c, "relay", "Costs", "  ", "ag")
        # A person's change of a section: a new version by the person, and a note to the session.
        r = core.canvas_section(self.c, "relay", "Costs", "$4 a month.", "mark")
        self.assertEqual((r["agent"], core.canvas_show(self.c, "relay")["versions"][-1]["author"]), ("ag", "mark"))
        self.assertTrue(any("mark changed the section 'Costs' of doc relay" in m["body"]
                            for m in core.inbox(self.c, "ag", mark_read=False)))

    def test_decisions_are_answered_in_place(self):
        self.assertEqual(core.doc_decisions(DOC), [{"n": 1, "of": 2, "name": "Where it runs"},
                                                   {"n": 2, "of": 2, "name": "Price"}])
        a = core.canvas_answer(self.c, "relay", 1, "Cloudflare", "mark")
        self.assertEqual((a["state"], a["anchor"]["path"], a["decision"]), ("sent", "decision:1", 1))
        self.assertTrue(any("Answer to decision 1 of 2 (Where it runs): Cloudflare" in m["body"]
                            for m in core.inbox(self.c, "ag", mark_read=False)))
        with self.assertRaisesRegex(core.RiverError, "has no decision 3; it has 1, 2"):
            core.canvas_answer(self.c, "relay", 3, "x", "mark")
        with self.assertRaisesRegex(core.RiverError, "needs words"):
            core.canvas_answer(self.c, "relay", 2, " ", "mark")

    def test_the_same_answer_or_comment_again_is_one(self):
        # #4845: an answer and a comment came 2 to 6 times, 0 to 5 seconds apart.
        from unittest import mock
        msgs = lambda: [m for m in core.inbox(self.c, "ag", mark_read=False) if "Answer to decision 1" in m["body"]]
        a = core.canvas_answer(self.c, "relay", 1, "Cloudflare", "mark")
        b = core.canvas_answer(self.c, "relay", 1, "Cloudflare ", "mark")
        self.assertEqual((b["id"], b.get("again"), a.get("again")), (a["id"], True, None))
        self.assertEqual(len(msgs()), 1)
        self.assertNotEqual(core.canvas_answer(self.c, "relay", 1, "OVH", "mark")["id"], a["id"])  # other words
        d = core.canvas_comment(self.c, "relay", "Too long", "mark")  # a draft, then the same words sent: sent once
        e = core.canvas_comment(self.c, "relay", "Too long", "mark", send_now=True)
        self.assertEqual((e["id"], e["state"]), (d["id"], "sent"))
        self.assertNotEqual(core.canvas_comment(self.c, "relay", "Too long", "mark", {"path": "h2"})["id"], d["id"])  # another part
        later = core.now() + core.COMMENT_AGAIN + core.timedelta(seconds=1)
        with mock.patch.object(core, "now", lambda: later):
            self.assertNotEqual(core.canvas_answer(self.c, "relay", 1, "Cloudflare", "mark")["id"], a["id"])
        self.assertEqual(len(msgs()), 3)

    def test_the_command_and_the_mcp_tool(self):
        import contextlib
        import io
        from river import cli, mcp
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(["-q", "--as", "ag", "canvas", "section", "relay", "Costs", "--pending", "the cost"])
            cli.run(["-q", "--as", "mark", "canvas", "answer", "relay", "2", "$5 a month"])
        self.assertIn("section 'Costs' added: version 2", out.getvalue())
        self.assertIn("answered decision 2 (Price)", out.getvalue())
        self.assertEqual(mcp.canvas_argv({"action": "section", "name": "relay", "heading": "Costs", "text": "x"}),
                         ["canvas", "section", "relay", "Costs", "--text=x"])
        self.assertEqual(mcp.canvas_argv({"action": "section", "name": "relay", "heading": "Costs", "pending": "p"}),
                         ["canvas", "section", "relay", "Costs", "--pending=p"])
        self.assertEqual(mcp.canvas_argv({"action": "answer", "name": "relay", "decision": 1, "text": "OVH"}),
                         ["canvas", "answer", "relay", "1", "OVH"])
        self.assertEqual(server.OPS["canvas_answer"](self.c, {"name": "relay", "decision": 1, "text": "OVH"}, "mark")["decision"], 1)


class PanelDoc(ChromePanel):
    """#4708: a doc in the side panel: a pending section, decision cards answered in place, Edit of a section."""

    def test_a_doc_in_the_panel(self):
        def put(c):
            return core.canvas_put(c, "relay", DOC, "ag", kind="md", title="Relay")["id"]

        def call(fn):
            def run():
                c = core.connect()
                try:
                    return fn(c)
                finally:
                    c.close()
            return run
        driver = self.tvs.DRIVE_HEAD + r'''
try {
  await until(() => q("#sessTitle").textContent === "ag" && /updated/.test(q("#stamp").textContent), "the session ag");
  q("#actor").value = "mark"; q("#actor").dispatchEvent(new Event("change"));
  await sleep(400);
  await fetch("/__doc");
  await until(() => q(".doc-decision"), "the doc with its decision cards");
  const out = { pending: q(".doc-pending").textContent.trim(), cards: [...document.querySelectorAll(".doc-decision")].map(c => c.id),
                opts: [...document.querySelectorAll("#decision-1 [data-doc-opt]")].map(b => b.textContent),
                title: q("#decision-1 .doc-dtitle").textContent.trim(),
                labels: [...document.querySelectorAll("#decision-1 .doc-label")].map(b => b.textContent),
                text: q("#decision-1 > p").textContent,
                row: [...q("#decision-1 .doc-ans-row").children].map(x => x.tagName) };
  // #4845: a second click while the first answer goes does nothing; the card says that it is sending.
  q('#decision-1 [data-doc-opt="Cloudflare"]').click();
  out.sending = q("#decision-1 [data-doc-answer]").textContent; out.disabled = q('#decision-1 [data-doc-opt="OVH"]').disabled;
  q('#decision-1 [data-doc-opt="OVH"]').click(); q('#decision-1 [data-doc-opt="Cloudflare"]').click();
  await until(() => q("#decision-1 .doc-answered"), "the answer on its card");
  out.answered = q("#decision-1 .doc-answered").textContent.replace(/\s+/g, " ").trim();
  const ed = [...document.querySelectorAll(".sp-canvas h2 .doc-edit")].find(b => /What it is/.test(b.title));
  ed.click();
  await until(() => q(".doc-sec-edit textarea"), "the section's box");
  out.box = q(".doc-sec-edit textarea").value;
  q(".doc-sec-edit textarea").value = "It shows the page from anywhere."; q(".doc-sec-edit .btn.primary").click();
  await until(() => q(".sp-cver") && q(".sp-cver").value === "2" && !q(".doc-pending"), "version 2");
  out.v2 = q(".sp-canvas").textContent.includes("It shows the page from anywhere.");
  post(out);
} catch (e) { post({ error: String(e.stack || e) }); }
'''
        got = self.tvs.drive_page(self.base, driver, routes={"/__doc": call(put)})
        self.assertNotIn("error", got, got)
        self.assertEqual(got["pending"], "Being written: what the relay is for")
        self.assertEqual(got["cards"], ["decision-1", "decision-2"])
        self.assertEqual(got["opts"], ["OVH", "Cloudflare"])
        # #5360: the first line is the card's title, the labels of the form are marked, and no word is lost.
        self.assertEqual(got["title"], "Decision 1 of 2: Where it runs  (item #1)")
        self.assertEqual(got["labels"], ["What you decide:", "Your answer:"])
        self.assertEqual(got["text"], 'Decision 1 of 2: Where it runs  (item #1)What you decide: the host.\n'
                                      'Your answer: "OVH" or "Cloudflare".')
        self.assertEqual(got["row"], ["INPUT", "BUTTON"], "the box and Answer have a row of their own")
        self.assertIn("mark answered", got["answered"])
        self.assertIn("Cloudflare", got["answered"])
        self.assertEqual((got["sending"], got["disabled"]), ("Sending…", True))
        answers = [m for m in core.canvas_show(self.base.c, "relay")["comments"] if (m["anchor"] or {}).get("path") == "decision:1"]
        self.assertEqual(len(answers), 1, answers)
        self.assertEqual(got["box"], "writing: what the relay is for")
        self.assertTrue(got["v2"])
        show = core.canvas_show(self.base.c, "relay")
        self.assertEqual((show["version"], show["agent"]), (2, "ag"))

    def test_a_new_version_of_a_doc_changes_only_its_changed_sections(self):
        # #4844: no draw of the whole doc for a new version or an answer: the sections that stay keep their elements,
        # the words typed in a decision's box stay, and the place on screen stays.
        def call(fn):
            def run():
                c = core.connect()
                try:
                    return fn(c)
                finally:
                    c.close()
            return run
        put = call(lambda c: core.canvas_put(c, "relay", DOC + "\n## Notes\n\n" + "A line of notes.\n\n" * 60,
                                             "ag", kind="md", title="Relay")["id"])
        section = call(lambda c: core.canvas_section(c, "relay", "What it is", "It shows the page from anywhere.", "ag")["version"])
        answer = call(lambda c: core.canvas_answer(c, "relay", 1, "OVH", "mark")["id"])
        driver = self.tvs.DRIVE_HEAD + r'''
try {
  await until(() => q("#sessTitle").textContent === "ag" && /updated/.test(q("#stamp").textContent), "the session ag");
  q("#actor").value = "mark"; q("#actor").dispatchEvent(new Event("change"));
  await sleep(400);
  await fetch("/__doc");
  await until(() => q("#decision-2"), "the doc");
  const box = q("#spBody"), card = q("#decision-1"), notes = [...document.querySelectorAll(".sp-canvas h2")].find(h => /Notes/.test(h.textContent));
  const inp = q("#decision-2 [data-doc-other]");
  inp.focus(); inp.value = "$6 a month";
  box.scrollTop = 200;
  const at = card.getBoundingClientRect().top;
  let emptied = false;
  const watch = new MutationObserver(() => { if (!q(".sp-canvas") || !q(".sp-cbar")) emptied = true; });
  watch.observe(box, { childList: true, subtree: true });
  await fetch("/__section");
  await until(() => q(".sp-cver").value === "2" && q(".sp-canvas").textContent.includes("from anywhere"), "version 2 in place");
  const out = { emptied, same_card: q("#decision-1") === card, same_notes: notes.isConnected,
                typed: q("#decision-2 [data-doc-other]").value, focus: document.activeElement === q("#decision-2 [data-doc-other]"),
                moved: Math.abs(q("#decision-1").getBoundingClientRect().top - at), pending: !!q(".doc-pending") };
  await fetch("/__answer");
  await until(() => q("#decision-1 .doc-answered"), "the answer on its card");
  out.after_answer = { same_card: q("#decision-1") === card, typed: q("#decision-2 [data-doc-other]").value, emptied,
                       answered: q("#decision-1 .doc-answered").textContent.replace(/\s+/g, " ").trim(),
                       button: q("#decision-1 [data-doc-answer]").textContent };
  // The Edit of a section that stayed reads the newest text.
  const ed = [...document.querySelectorAll(".sp-canvas h2 .doc-edit")].find(b => /What it is/.test(b.title));
  ed.click();
  await until(() => q(".doc-sec-edit textarea"), "the section's box");
  out.edit_box = q(".doc-sec-edit textarea").value;
  watch.disconnect();
  // The lists read again every 10 seconds: the same rows are not drawn again.
  const { showRows } = await import("/views/artifacts.js");
  const el = document.createElement("div"), row = { kind: "file", target: "/p/a.png", session: "ag", how: "wrote", at: null };
  showRows(el, [row], "none"); const first = el.firstElementChild;
  showRows(el, [{ ...row }], "none"); out.rows_same = el.firstElementChild === first;
  showRows(el, [{ ...row, target: "/p/b.png" }], "none"); out.rows_new = el.firstElementChild !== first;
  post(out);
} catch (e) { post({ error: String(e.stack || e) }); }
'''
        got = self.tvs.drive_page(self.base, driver, routes={"/__doc": put, "/__section": section, "/__answer": answer})
        self.assertNotIn("error", got, got)
        self.assertFalse(got["emptied"], got)
        self.assertTrue(got["same_card"] and got["same_notes"], got)
        self.assertEqual((got["typed"], got["focus"], got["pending"]), ("$6 a month", True, False))
        self.assertLess(got["moved"], 2, got)
        a = got["after_answer"]
        self.assertTrue(a["same_card"], a)
        self.assertEqual((a["typed"], a["emptied"], a["button"]), ("$6 a month", False, "Answer again"))
        self.assertIn("OVH", a["answered"])
        self.assertEqual(got["edit_box"], "It shows the page from anywhere.")
        self.assertEqual((got["rows_same"], got["rows_new"]), (True, True))


KIT_PAGE = """<!doctype html><html><head><meta charset=utf-8><meta name="maxpm-kit"><style>h1{{color:red}}</style></head>
<body><main class="kit"><h1>Plan</h1><div class="kit-graph" data-items="{a} #{b}, 99999"></div>
<p>Waits on <span data-item="{b}"></span>.</p>
<section class="kit-decision" data-decision="1" data-options="A|B"><h3>Decision 1 of 2: Split the goal</h3>
<dl><dt>Your answer</dt><dd>"A" or "B"</dd></dl></section></main></body></html>"""


class Kit(Base):
    """#4983: the canvas kit: maxpm adds its styles, the states of the items it names, and its script to an HTML
    canvas with <meta name="maxpm-kit">; local pictures go inline at put; a kit decision card is answered in place."""

    def setUp(self):
        super().setUp()
        core.register(self.c, "mark", human=True)
        self.a = core.item_add(self.c, "web", "Read the tasks </script><b>")["id"]
        self.b = core.item_add(self.c, "web", "Propose the parts", after=[self.a])["id"]
        self.kit = core.canvas_put(self.c, "visual", KIT_PAGE.format(a=self.a, b=self.b), "ag", kind="html")["id"]

    def test_a_kit_page_gets_the_styles_the_item_states_and_the_script(self):
        _, sig = self.key(f"canvas:{self.kit}")
        body = self.get(f"/canvas/{self.kit}/latest?{sig}")["body"].decode()
        self.assertLess(body.index(".kit-graph"), body.index("h1{color:red}"), "the page's own styles come after the kit's")
        self.assertIn("maxpm canvas kit", body)
        self.assertLess(body.index('id="maxpm-items"'), body.index("maxpm canvas helper"))
        raw = body.split('id="maxpm-items">', 1)[1].split("</script>", 1)[0]
        items = json.loads(raw.replace("<\\/", "</"))
        self.assertEqual(set(items), {str(self.a), str(self.b)}, "an id that is not in the queue is left out")
        self.assertEqual(items[str(self.b)]["deps"], [self.a])
        self.assertTrue(items[str(self.b)]["waits"])
        self.assertEqual(items[str(self.a)]["title"], "Read the tasks </script><b>")
        self.assertNotIn("</script><b>", raw, "a title cannot end the script")
        core.release(self.c, self.it, "ag")
        core.claim(self.c, self.a, "ag")
        core.done(self.c, self.a, "ag")
        body = self.get(f"/canvas/{self.kit}/latest?{sig}")["body"].decode()
        items = json.loads(body.split('id="maxpm-items">', 1)[1].split("</script>", 1)[0].replace("<\\/", "</"))
        self.assertEqual((items[str(self.a)]["status"], items[str(self.b)]["waits"]), ("done", False),
                         "the states are read when the page loads, not when it was put")
        _, sig = self.key()
        plain = self.get(f"/canvas/{self.cv}/latest?{sig}")["body"].decode()
        self.assertNotIn("maxpm-items", plain)
        self.assertNotIn(".kit-graph", plain)

    def test_a_kit_decision_card_is_answered_in_place(self):
        a = core.canvas_answer(self.c, "visual", 1, "A", "mark")
        self.assertEqual((a["decision"], a["name"], a["anchor"]["path"]), (1, "Split the goal", "decision:1"))
        self.assertTrue(any("Answer to decision 1 of 2 (Split the goal): A" in m["body"]
                            for m in core.inbox(self.c, "ag", mark_read=False)))
        with self.assertRaisesRegex(core.RiverError, "has no decision 2; it has 1"):
            core.canvas_answer(self.c, "visual", 2, "B", "mark")

    def test_local_pictures_go_inline_at_put(self):
        import base64
        import contextlib
        import io
        from river import cli
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
        d = os.path.join(self.dir.name, "pages")
        os.makedirs(os.path.join(d, "img"))
        with open(os.path.join(d, "img", "shot.png"), "wb") as f:
            f.write(png)
        with open(os.path.join(d, "notes.md"), "w") as f:
            f.write("# Notes\n\n![The page](img/shot.png \"title\")\n\n![Gone](gone.png)\n\n![Web](https://x.test/a.png)\n")
        with open(os.path.join(d, "page.html"), "w") as f:
            f.write("<html><body><img alt=x src='img/shot.png'><img src=\"data:image/png;base64,AA\"></body></html>")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(["-q", "--as", "ag", "canvas", "put", os.path.join(d, "notes.md"), "--name", "notes"])
            cli.run(["-q", "--as", "ag", "canvas", "put", os.path.join(d, "page.html"), "--name", "page"])
        md = core.canvas_show(self.c, "notes")["shown"]["body"]
        self.assertIn('![The page](data:image/png;base64,iVBOR', md)
        self.assertIn(' "title")', md)
        self.assertIn("![Gone](gone.png)", md)
        self.assertIn("https://x.test/a.png", md)
        self.assertIn("<img alt=x src='data:image/png;base64,iVBOR", core.canvas_show(self.c, "page")["shown"]["body"])
        self.assertIn("1 local picture(s) put inline", out.getvalue())
        self.assertIn("picture not put inline: gone.png", out.getvalue())

    def test_the_kit_command_the_mcp_tool_and_http(self):
        import contextlib
        import io
        from river import cli, mcp
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(["-q", "canvas", "kit"])
        self.assertIn('<meta name="maxpm-kit">', out.getvalue())
        for part in ("kit-graph", "kit-cards", "kit-split", "kit-bars", "kit-timeline", "kit-frame", "kit-shot",
                     "kit-decision", "data-item="):
            self.assertIn(part, out.getvalue())
        self.assertEqual(mcp.canvas_argv({"action": "kit"}), ["canvas", "kit"])
        self.assertIn("maxpm-kit", server.OPS["canvas_kit"](self.c, {}, "ag")["starter"])
        self.assertTrue(core.uses_kit(core.canvas_kit()["starter"]))
        self.assertFalse(core.uses_kit("<p>maxpm-kit</p>"))


class PanelKit(ChromePanel):
    """#4983: in the side panel, a kit decision card's answer goes to the session only after the person's own click;
    a Markdown canvas shows pictures put inline (and no picture from the network) and draws ```mermaid diagrams."""

    def put_kit(self):
        def run():
            c = core.connect()
            try:
                body = ('<html><head><meta name="maxpm-kit"></head><body><section class="kit-decision" data-decision="1" '
                        'data-options="A|B"><h3>Decision 1 of 1: Split</h3></section></body></html>')
                return core.canvas_put(c, "plan", body, "ag", kind="html", title="Release plan")["version"]
            finally:
                c.close()
        return run

    def put_md(self):
        def run():
            c = core.connect()
            try:
                png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
                body = (f"# Notes\n\n![shot]({png})\n\n![web](https://example.com/x.png)\n\n"
                        "```mermaid\nflowchart TD\n  A[Read] --> B[Plan]\n```\n")
                return core.canvas_put(c, "notes", body, "ag", kind="md", title="Notes")["version"]
            finally:
                c.close()
        return run

    def test_a_kit_answer_and_a_markdown_canvas(self):
        c = core.connect()
        core.register(c, "mark", human=True)
        c.close()
        driver = self.tvs.DRIVE_HEAD + r'''
try {
  await until(() => q("#sessTitle").textContent === "ag" && /updated/.test(q("#stamp").textContent), "the session ag");
  const you = q("#actor"); if (![...you.options].some(o => o.value === "mark")) you.append(new Option("mark", "mark"));
  you.value = "mark";
  await sleep(400);
  await fetch("/__kit");
  await until(() => q("iframe.sp-canvas"), "the kit canvas");
  const f = q("iframe.sp-canvas"), out = {};
  const send = (data) => window.dispatchEvent(new MessageEvent("message", { source: f.contentWindow, data }));
  // No click of the person: the frame's answer is refused.
  Object.defineProperty(navigator, "userActivation", { configurable: true, value: { isActive: false } });
  send({ maxpmCanvas: "answer", decision: 1, text: "B" });
  await sleep(500);
  out.before = (await (await fetch("/__answers")).json()).n;
  Object.defineProperty(navigator, "userActivation", { configurable: true, value: { isActive: true } });
  send({ maxpmCanvas: "answer", decision: 1, text: "A" });
  for (let i = 0; i < 50 && (await (await fetch("/__answers")).json()).n !== 1; i++) await sleep(100);
  out.answer = (await (await fetch("/__answers")).json()).body;
  post(out);
} catch (e) { post({ error: String(e.stack || e) }); }
'''

        def answers():
            c = core.connect()
            try:
                rows = [x for x in core.canvas_comments(c, "plan", "mark")["comments"]
                        if (x.get("anchor") or {}).get("path") == "decision:1"]
                return {"n": len(rows), "body": rows[0]["body"] if rows else ""}
            finally:
                c.close()
        got = self.tvs.drive_page(self.base, driver, routes={"/__kit": self.put_kit(), "/__answers": answers})
        self.assertNotIn("error", got, got)
        self.assertEqual(got["before"], 0, "an answer with no click of the person does not count")
        self.assertEqual(got["answer"], "Answer to decision 1 of 1 (Split): A")

    def test_a_markdown_canvas_with_pictures_and_a_diagram(self):
        driver = self.tvs.DRIVE_HEAD + r'''
try {
  await until(() => q("#sessTitle").textContent === "ag" && /updated/.test(q("#stamp").textContent), "the session ag");
  await sleep(400);
  const out = {};
  await fetch("/__md");
  await until(() => q(".sp-md.sp-canvas"), "the markdown canvas");
  const md = q(".sp-md.sp-canvas");
  out.imgs = [...md.querySelectorAll("img:not(.cv-diagram img)")].map(i => (i.getAttribute("src") || "").slice(0, 22));
  if (window.mermaid) {
    await until(() => md.querySelector(".cv-diagram img"), "the diagram");
    out.diagram = md.querySelector(".cv-diagram img").getAttribute("src").slice(0, 26);
  }
  post(out);
} catch (e) { post({ error: String(e.stack || e) }); }
'''
        got = self.tvs.drive_page(self.base, driver, routes={"/__md": self.put_md()})
        self.assertNotIn("error", got, got)
        self.assertEqual(got["imgs"][0], "data:image/png;base64,")
        self.assertEqual(got["imgs"][1:], [""], "a picture from the network keeps no address: it loads nothing")
        if "diagram" in got:
            self.assertEqual(got["diagram"], "data:image/svg+xml;charset")
