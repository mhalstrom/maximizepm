"""The page as a whole in a browser (#4652, #4653, #4656): its addresses, every view, the side panel's tabs."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from river import core, server
from tests.test_view_sessions import TmuxBase, claude_line


class PageBrowser(TmuxBase):
    """The real page (app.js) in Chrome, beside the running session ag."""

    HEAD = r'''
const q = s => document.querySelector(s);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const until = async (f, what) => { for (let i = 0; i < 150; i++) { if (f()) return; await sleep(100); } throw new Error(what); };
const check = (v, what) => { if (!v) throw new Error(what); };
const post = o => navigator.sendBeacon(new URL("/__result", location).href, JSON.stringify(o));
'''

    def drive(self, driver, path="/", steps=None):
        """Run driver (a module) on every page the browser loads, from path; it posts its result to /__result.
        steps: name -> function that the driver runs on the server side with fetch("/__step/<name>")."""
        import subprocess
        import threading
        from unittest.mock import patch
        from tests.test_server import _chrome
        if not _chrome():
            self.skipTest("no Chrome on this computer")
        got, result = threading.Event(), {}
        tag = b'<script type="module" src="app.js" onerror="riverLoadFailed()"></script>'
        drive = (b'<script type="module">import "./app.js"; window.__appReady = true;</script>'
                 b'<script type="module" src="/__drive.js"></script>')

        class H(server.Handler):
            def do_GET(self):
                if self.path.startswith("/__step/"):
                    (steps or {})[self.path[len("/__step/"):]]()
                    return self._send(204, b"")
                if self.path == "/__drive.js":
                    return self._send(200, (PageBrowser.HEAD + driver).encode(), "text/javascript; charset=utf-8")
                page = server.page_html(self.path.split("?")[0])
                if page is not None:
                    assert tag in page
                    return self._send(200, page.replace(tag, tag + drive), "text/html; charset=utf-8")
                return super().do_GET()

            def do_POST(self):
                if self.path == "/__result":
                    result.update(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                    got.set()
                    return self._send(204, b"")
                return super().do_POST()

        patched = patch.object(core, "transcripts_root", lambda: self.root)
        patched.start()
        self.addCleanup(patched.stop)
        try:
            httpd = server._Server(("127.0.0.1", 0), H)
        except PermissionError:
            self.skipTest("no local port here")
        httpd.daemon_threads = True
        httpd.handle_error = lambda request, address: None  # Chrome ends while it still polls: a dropped connection
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        chrome = subprocess.Popen([
            _chrome(), "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
            "--disable-extensions", f"--user-data-dir={self.dir.name}/browser",
            f"http://127.0.0.1:{httpd.server_address[1]}{path}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            ran = got.wait(40)
        finally:
            chrome.kill()
            chrome.wait()
            httpd.shutdown()
            httpd.server_close()
        self.assertTrue(ran, "browser test did not report")
        self.assertNotIn("error", result, result.get("error"))
        return result

    def test_every_view_draws_and_its_controls_answer(self):
        """#4656: each tab of the page is a module of its own (views/*.js); each one draws with the queue's data, and
        its controls (and the frame's) still answer a click, with no error on the page."""
        core.goal_add(self.c, "site", "launch", outcome="the site is live", done_when="it answers")
        core.target_add(self.c, "prod", description="push to main")
        core.project_target(self.c, "site", "prod")
        later = core.item_add(self.c, "site", "footer", goals=["launch"])["id"]
        core.block(self.c, later, reason="waits on the logo", actor="mark")
        first = core.item_add(self.c, "site", "logo", doer="human")["id"]
        core.claim(self.c, first, "mark")
        core.done(self.c, first, output="drawn", actor="mark")
        core.send(self.c, "question", "Which color?", to="mark", actor="ag")
        driver = r'''
const errors = [];
window.addEventListener("error", e => { if (!/ResizeObserver loop/.test(e.message)) errors.push(String(e.message)); });
window.addEventListener("unhandledrejection", e => errors.push(String(e.reason && (e.reason.stack || e.reason))));
const shown = id => !q("#tab-" + id).classList.contains("hidden");
try {
  await until(() => window.__appReady && q("#work .wp, #work .muted"), "the Board drew");
  q("#actor").value = "mark"; q("#actor").dispatchEvent(new Event("change"));
  await until(() => q("#inbox .msg") || q("#inboxTitle").textContent.includes("mark"), "the inbox of mark");
  q("#work [data-wp]").click();
  await until(() => q("#work .wp-items"), "a click opens a project of Work");
  check(q("#strip [data-strip]"), "the status strip");
  const seen = {};
  for (const name of ["projects", "targets", "capacity", "graph", "done", "settings", "artifacts", "sessions", "board"]) {
    q(`nav button[data-tab="${name}"]`).click();
    await until(() => shown(name), "the tab " + name);
    await sleep(300);
    seen[name] = q("#tab-" + name).textContent.length > 20;
  }
  q('nav button[data-tab="projects"]').click();
  await until(() => q("#projects [data-ptable]") || q("#projects .pcard, #projects [data-fold]"), "the project cards");
  const fold = q("#projects [data-fold]"); if (fold) { fold.click(); await sleep(100); }
  check(q("#blockedList").textContent.includes("waits on the logo"), "blocked items on Projects");
  q('nav button[data-tab="targets"]').click();
  await until(() => q("#targets").textContent.includes("prod"), "the target prod");
  q('nav button[data-tab="done"]').click();
  await until(() => q("#log").textContent.includes("logo"), "the done log");
  q('nav button[data-tab="settings"]').click();
  await until(() => q("#settingsTable").textContent.includes("lease_ttl"), "the settings");
  q('nav button[data-tab="board"]').click();
  q("#strip [data-strip=capacity]").click();
  await until(() => shown("capacity"), "the strip goes to Capacity");
  window.riverOpen(ITEM);
  await until(() => q("#drawer").classList.contains("open"), "the drawer");
  q("#dClose").click();
  await until(() => !q("#drawer").classList.contains("open"), "the drawer closes");
  await sleep(500);
  post({ seen, errors });
} catch (e) { post({ error: String(e.stack || e) + " errors: " + errors.join(" | ") }); }
'''.replace("ITEM", str(self.it))
        got = self.drive(driver)
        self.assertEqual(got["errors"], [])
        self.assertTrue(all(got["seen"].values()), got["seen"])

    def test_a_new_prompt_or_question_of_the_open_session_opens_the_dialog_once(self):
        """#4663: the ! dialog opens by itself when the open session comes to a prompt or asks a question; Dismiss
        keeps it closed until something new waits."""
        core.config_set(self.c, "setup_done", "on")

        def question():
            c = core.connect()
            try:
                core.send(c, "question", "Push the header?", to="mark", item=self.it, actor="ag")
            finally:
                c.close()
        driver = r'''
const open = () => !q("#waitDlg").classList.contains("hidden");
try {
  await until(() => window.__appReady && open(), "the dialog opens by itself for the prompt");
  check(q("#waitList").textContent.includes("Do you want to proceed?"), "it shows the prompt");
  q("#waitDismiss").click();
  await sleep(7000);  // two refreshes of the Sessions tab
  check(!open(), "Dismiss keeps the same prompt closed");
  check(q("#sessWait [data-wait-open]"), "the wait line links to the dialog");
  check(!q("#sessWait").textContent.includes("type the answer below"), "no text about typing the answer below");
  await fetch("/__step/question");
  await until(open, "a new question opens it again");
  check(q("#waitList").textContent.includes("Push the header?"), "it shows the question");
  q("#waitDismiss").click();
  await sleep(4000);
  check(!open(), "and Dismiss keeps it closed");
  q("#sessWait [data-wait-open]").click();
  check(open(), "Answer it opens it");
  post({ ok: true });
} catch (e) { post({ error: String(e.stack || e) }); }
'''
        self.assertEqual(self.drive(driver, "/sessions/ag", {"question": question}), {"ok": True})

    def test_a_local_page_seen_through_the_relay_says_where_it_runs(self):
        """#4711: on the relay website a local page (localhost, 127.0.0.1) opens the viewer's own computer, so the
        side panel shows its address to copy in place of a frame; on the computer of maxpm serve it is a frame."""
        driver = r'''try {
  await until(() => window.__appReady, "app.js loaded");
  const { awayFromHome } = await import("/components/sidePanel.js");
  check(!awayFromHome(), "the test page runs on this computer");
  for (const o of ["http://127.0.0.1:8765", "http://localhost:5173", "http://[::1]:8765"]) check(!awayFromHome(o), o + " is home");
  for (const o of ["https://relay.example.org", "http://192.168.1.20:8765", "https://localhost.example.com"]) check(awayFromHome(o), o + " is away");
  post({ ok: true });
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        self.assertEqual(self.drive(driver, "/sessions/ag"), {"ok": True})

    def test_a_click_on_each_tab_of_the_side_panel_shows_it(self):
        """#4652: the panel's tabs once had data-tab, which the page's own click handler reads as a tab of the
        page: a click hid the Sessions tab."""
        driver = r'''try {
  await until(() => window.__appReady, "app.js loaded");
  q('nav button[data-tab="sessions"]').click();
  await until(() => !q("#tab-sessions").classList.contains("hidden") && q("#sessTitle").textContent === "ag", "session ag shown");
  const { openTool } = await import("/components/sidePanel.js");
  for (const k of ["one", "two", "three"]) openTool("t-" + k, "Tool " + k, box => { box.textContent = "body of " + k; });
  const shown = [];
  for (const k of ["one", "two", "three", "one"]) {
    const find = () => [...document.querySelectorAll("#spTabs .sp-tab-l")].find(x => x.textContent.includes("Tool " + k));
    find().click();
    await sleep(50);
    const b = find();  // the tabs are drawn again after a click
    check(b, "the tab " + k + " is still there");
    check(!q("#tab-sessions").classList.contains("hidden"), "the Sessions tab stays after a click on " + k);
    check(q('nav button[data-tab="sessions"]').classList.contains("on"), "the page tab stays Sessions");
    check(b.getAttribute("aria-selected") === "true" && b.closest(".sp-tab").classList.contains("on"), k + " is the active tab");
    check(document.querySelectorAll("#spTabs .sp-tab.on").length === 1, "one active tab");
    shown.push(q("#spBody").textContent);
  }
  post({ shown });
} catch (e) { post({ error: String(e.stack || e) }); }
'''
        self.assertEqual(self.drive(driver)["shown"], ["body of one", "body of two", "body of three", "body of one"])

    def test_each_view_has_an_address_that_reloads_and_goes_back(self):
        """#4653: a path for each view, the session and the artifact of its side panel in the address, ?item= for
        the drawer; a reload shows the same view, and Back returns."""
        plan = server.uploads_dir() / "ag" / "plan.md"  # a file the side panel may show, that ag wrote
        plan.parent.mkdir(parents=True)
        plan.write_text("# Plan")
        target = str(plan.resolve())
        self.write(claude_line("assistant", [{"type": "tool_use", "name": "Write",
                                              "input": {"file_path": target, "content": "# Plan"}}], "tool_use"))
        driver = r'''
const step = sessionStorage.getItem("step") || "graph";
const where = () => location.pathname + location.search;
try {
  await until(() => window.__appReady, "app.js loaded");
  const { artId } = await import("./lib.js");
  const { openInPanel } = await import("./components/sidePanel.js");
  const target = TARGET, id = artId(target);
  if (step === "graph") {
    await until(() => !q("#tab-graph").classList.contains("hidden"), "the path /graph shows the Graph tab");
    q('nav button[data-tab="sessions"]').click();
    await until(() => where() === "/sessions/ag", "Sessions with its session ag: " + where());
    openInPanel(target);
    await until(() => where() === "/sessions/ag/artifacts/" + id, "the artifact in the address: " + where());
    openInPanel("http://127.0.0.1:9/other");
    await until(() => where() === "/sessions/ag/artifacts/" + artId("http://127.0.0.1:9/other"), "another tab: " + where());
    [...document.querySelectorAll("#spTabs .sp-tab-l")].find(b => b.textContent.includes("plan.md")).click();
    await until(() => where() === "/sessions/ag/artifacts/" + id, "a click on a tab: " + where());
    sessionStorage.setItem("step", "reload");
    location.reload();
  } else if (step === "reload") {
    await until(() => q("#spTabs .sp-tab.on") && q("#spTabs .sp-tab.on").textContent.includes("plan.md"), "the artifact after a reload");
    check(where() === "/sessions/ag/artifacts/" + id, "the same address after a reload: " + where());
    window.riverOpen(ITEM);
    await until(() => where() === "/sessions/ag/artifacts/" + id + "?item=" + ITEM && q("#drawer").classList.contains("open"), "the drawer: " + where());
    history.back();
    await until(() => !q("#drawer").classList.contains("open") && where() === "/sessions/ag/artifacts/" + id, "Back closes the drawer: " + where());
    history.back();  // to /sessions/ag (before the artifact? replaced) and then /graph
    await until(() => !q("#tab-graph").classList.contains("hidden") && where() === "/graph", "Back to the Graph: " + where());
    check(q('nav button[data-tab="graph"]').classList.contains("on"), "the Graph button is on");
    sessionStorage.setItem("step", "link");
    location.href = "/artifacts/" + id + "?as=mark";
  } else if (step === "link") {
    await until(() => where() === "/sessions/ag/artifacts/" + id + "?as=mark", "an artifact link opens it beside its session: " + where());
    check(q("#actor").value === "mark", "?as= picks the person");
    post({ ok: true });
  }
} catch (e) { post({ error: step + ": " + String(e.stack || e) }); }
'''.replace("TARGET", json.dumps(target)).replace("ITEM", str(self.it))
        self.assertEqual(self.drive(driver, "/graph"), {"ok": True})


class PageAddresses(unittest.TestCase):
    """#4653: maxpm serve answers the page for the path of each view, with a <base> back to the page's root."""

    def test_each_view_path_gets_the_page_with_its_base(self):
        import re
        from urllib.parse import urljoin
        for path, base in [("/", "./"), ("/sessions", "./"), ("/sessions/", "../"), ("/sessions/ag", "../"),
                           ("/sessions/ag/artifacts/k3x9", "../../../"), ("/graph", "./"), ("/artifacts/k3x9", "../")]:
            page = server.page_html(path)
            self.assertIsNotNone(page, path)
            got = re.search(rb'<base href="([^"]*)">', page).group(1).decode()
            self.assertEqual(got, base, path)
            # Through the relay the page lives under /app/: the base leads back there too.
            self.assertEqual(urljoin("https://relay.example/app" + path, got), "https://relay.example/app/", path)
            self.assertLess(page.index(b"<base"), page.index(b"app.css"), "the base comes before the first address")
        for path in ("/api/state", "/app.js", "/components/chat.js", "/items/3", "/sessions//x", "/sessions/../api"):
            self.assertIsNone(server.page_html(path), path)

    def test_links_of_notices_use_the_paths(self):
        from river import notify
        with tempfile.TemporaryDirectory() as d:
            os.environ["MAXPM_DB"] = os.path.join(d, "t.db")
            c = core.connect()
            try:
                self.assertTrue(notify.page_url(c, item_id=7).endswith("/?item=7"))
                self.assertTrue(notify.page_url(c, terminal="web agent").endswith("/sessions/web%20agent"))
            finally:
                c.close()
                os.environ.pop("MAXPM_DB", None)
