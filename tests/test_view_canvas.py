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
