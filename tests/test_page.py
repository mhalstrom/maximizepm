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

    def drive(self, driver, path="/", steps=None, size=None, flags=()):
        """Run driver (a module) on every page the browser loads, from path; it posts its result to /__result.
        steps: name -> function that the driver runs on the server side with fetch("/__step/<name>")."""
        import subprocess
        import threading
        from unittest.mock import patch
        from tests.test_server import _chrome, _chrome_window, _frame_page
        if not _chrome():
            self.skipTest("no Chrome on this computer")
        window, frame = _chrome_window(size)
        got, result = threading.Event(), {}
        tag = b'<script type="module" src="app.js" onerror="riverLoadFailed()"></script>'
        drive = (b'<script type="module">import "./app.js"; window.__appReady = true;</script>'
                 b'<script type="module" src="/__drive.js"></script>')

        class H(server.Handler):
            def do_GET(self):
                if self.path == "/__frame" and frame:
                    return self._send(200, _frame_page(path, frame), "text/html; charset=utf-8")
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
            *([f"--window-size={window}"] if window else []), *flags,
            f"http://127.0.0.1:{httpd.server_address[1]}{'/__frame' if frame else path}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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

    def test_a_phone_size_lays_the_page_out_at_that_width(self):
        """#5415: headless Chrome makes no window narrower than 500 px, so '390,844' gave a 500 px page and no phone
        test saw a real phone. The page now loads in a 390 px frame: its viewport and its media queries are 390 px."""
        driver = r'''
try {
  await until(() => window.__appReady, "the page");
  post({w: innerWidth, h: innerHeight, phone: matchMedia("(max-width: 759px)").matches});
} catch (e) { post({error: String(e.stack || e)}); }
'''
        self.assertEqual(self.drive(driver, "/", size="390,844"), {"w": 390, "h": 844, "phone": True})
        self.assertEqual(self.drive(driver, "/", size="1280,900")["w"], 1280)

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
  await until(() => window.__appReady && q("#sessYouRow .srow"), "Sessions, the first page, with its You row");
  q("#actor").value = "mark"; q("#actor").dispatchEvent(new Event("change"));
  await until(() => q("#inbox .msg") || q("#inboxTitle").textContent.includes("mark"), "the inbox of mark");
  q('nav button[data-tab="plan"]').click(); q('[data-plan-view="overview"]').click();
  await until(() => q("#work .wp"), "Plan > Overview: Work");
  q("#work [data-wp]").click();
  await until(() => q("#work .wp-items"), "a click opens a project of Work");
  check(q("#capacity .stats") && q("#next"), "Capacity and Next up");
  const seen = {};
  for (const name of ["plan", "settings", "sessions"]) {
    q(name === "settings" ? "#settingsBtn" : `nav button[data-tab="${name}"]`).click();  // Settings: from the ⋯ (#5116)
    await until(() => shown(name), "the tab " + name);
    await sleep(300);
    seen[name] = q("#tab-" + name).textContent.length > 20;
  }
  q('nav button[data-tab="plan"]').click(); q('[data-plan-view="items"]').click();
  await until(() => q("#projects [data-ptable]") || q("#projects .pcard, #projects [data-fold]"), "the project cards");
  const fold = q("#projects [data-fold]"); if (fold) { fold.click(); await sleep(100); }
  check(q("#blockedList").textContent.includes("waits on the logo"), "blocked items on Projects");
  q('nav button[data-tab="plan"]').click(); q('[data-plan-view="releases"]').click();
  await until(() => q("#targets").textContent.includes("prod") && location.pathname === "/plan/releases", "Plan > Releases: the target prod");
  q('[data-plan-view="goals"]').click();
  await until(() => q('#goalCards [data-goalcard="launch"]') && q("#goalCards").textContent.includes("Done when: it answers"), "Plan > Goals: launch with its done-when");
  q('nav button[data-tab="plan"]').click(); q('[data-plan-view="done"]').click();
  await until(() => q("#log").textContent.includes("logo"), "the done log");
  q('[data-plan-view="graph"]').click();
  await until(() => location.pathname === "/plan/graph" && !q("#plan-graph").classList.contains("hidden"), "Plan > Graph");
  q('#planSide [data-scope-project="site"]').click();
  await until(() => location.search.includes("project=site") && q("#graphTitle").textContent === "site", "the scope site");
  await until(() => q('#planSide [data-scope-goal="launch"]'), "the goal launch under its project");
  q('#planSide [data-scope-goal="launch"]').click();
  await until(() => location.search.includes("goal=launch") && q("#planScope").textContent.includes("launch"), "the scope launch");
  q('#planSide [data-scope-project=""]').click();
  await until(() => !location.search.includes("project="), "All projects");
  q("#hdrMore").click(); q("#settingsBtn").click();
  await until(() => q("#settingsTable").textContent.includes("lease_ttl") && location.pathname === "/settings"
    && !q("header").classList.contains("more"), "the settings from the ⋯, at /settings, the menu closed");
  check(q("#people").textContent.includes("mark"), "Settings > People");
  // The links both ways (#5102 step 7): the drawer's project opens Plan > Items with that scope.
  window.riverOpen(ITEM);
  await until(() => q('#drawer [data-plan-go="items"]'), "the drawer's link to its project in Plan");
  q('#drawer [data-plan-go="items"]').click();
  await until(() => location.pathname === "/plan/items" && location.search.includes("project=site") && !q("#drawer").classList.contains("open"), "Plan > Items of site");
  check([...document.querySelectorAll("#projects .tabulator-col-title")].some(c => c.textContent === "Session"), "the Session column");
  window.riverOpen(ITEM);
  await until(() => q("#drawer").classList.contains("open"), "the drawer");
  q("#dClose").click();
  // Close goes Back (history.back, which ends later): wait for it, else it lands after the next open (#4716).
  await until(() => !q("#drawer").classList.contains("open") && !location.search.includes("item="), "the drawer closes");
  // Escape closes it too, and takes ?item= out of the address (#4716); with a dialog open, Escape closes only the dialog.
  window.riverOpen(ITEM);
  await until(() => q("#drawer").classList.contains("open") && location.search.includes("item="), "the drawer again");
  const esc = () => document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  q("#searchDlg").classList.remove("hidden");
  esc();
  check(q("#drawer").classList.contains("open"), "Escape over a dialog leaves the drawer open");
  check(q("#searchDlg").classList.contains("hidden"), "Escape closes the dialog");
  esc();
  await until(() => !q("#drawer").classList.contains("open") && !location.search.includes("item="), "Escape closes the drawer");
  // The 3 s refresh fetches the open item again; Escape while that fetch runs closes the drawer for good (#4766).
  window.riverOpen(ITEM);
  await until(() => q("#drawer").classList.contains("open") && location.search.includes("item="), "the drawer a third time");
  window.riverOpen(ITEM); esc();
  await sleep(1000);
  check(!q("#drawer").classList.contains("open"), "a refresh that ends after Escape leaves the drawer closed");
  await sleep(500);
  post({ seen, errors });
} catch (e) { post({ error: String(e.stack || e) + " errors: " + errors.join(" | ") }); }
'''.replace("ITEM", str(self.it))
        got = self.drive(driver)
        self.assertEqual(got["errors"], [])
        self.assertTrue(all(got["seen"].values()), got["seen"])

    # The hover sweep (#5106): a script cannot hover, so the page's style sheets are copied with a class (.__hover) in
    # place of :hover, the originals are turned off, and the class goes on an element and its ancestors, as a hover
    # would. Each element's layout box (offset*, which a transform does not change) is compared before and after.
    HOVER_SWEEP = r'''
const done = new WeakSet();
const sel = (t) => t.replace(/:hover\b/g, ".__hover");
const ruleText = (r) => r instanceof CSSStyleRule ? `${sel(r.selectorText)} { ${r.style.cssText} }`
  : r instanceof CSSMediaRule ? `@media ${r.conditionText} { ${[...r.cssRules].map(ruleText).join("\n")} }`
  : r instanceof CSSSupportsRule ? `@supports ${r.conditionText} { ${[...r.cssRules].map(ruleText).join("\n")} }`
  : r.cssText;
const subjects = new Set(["button", ".chip", ".row", ".srow", "tr", "a", "summary"]);
function hoverable() {  // copy each sheet not yet copied (a view loads its sheet when its tab opens)
  for (const sh of [...document.styleSheets]) {
    if (done.has(sh) || (sh.ownerNode && sh.ownerNode.dataset && sh.ownerNode.dataset.hoverCopy)) continue;
    let rules; try { rules = [...sh.cssRules]; } catch (e) { done.add(sh); continue; }  // another site's sheet
    const st = document.createElement("style"); st.dataset.hoverCopy = "1";
    st.textContent = rules.map(ruleText).join("\n"); document.head.append(st);
    sh.disabled = true; done.add(sh);
    // What a hover rule hovers: the compound selector that holds :hover, without its pseudo-classes.
    for (const t of rules.flatMap(function all(r) { return r.cssRules ? [...r.cssRules].flatMap(all) : [r]; }))
      for (const part of (t.selectorText || "").split(","))
        for (const comp of part.split(/[\s>+~]+/)) if (comp.includes(":hover")) {
          const s = comp.replace(/:not\([^)]*\)/g, "").replace(/::?[\w-]+(\([^)]*\))?/g, "");
          if (s) subjects.add(s);
        }
  }
}
const visible = (e) => e.offsetParent !== null || getComputedStyle(e).position === "fixed";
const box = (e) => visible(e) ? [e.offsetLeft, e.offsetTop, e.offsetWidth, e.offsetHeight].join(",") : "hidden";
const name = (e) => e.tagName.toLowerCase() + (e.id ? "#" + e.id : "") + (typeof e.className === "string" && e.className.trim()
  ? "." + e.className.trim().split(/\s+/).filter(c => c !== "__hover").join(".") : "") + (e.textContent ? ` "${e.textContent.trim().slice(0, 30)}"` : "");
function sweep(where) {  // one task: the page cannot draw again between the looks
  hoverable();
  const all = [...document.body.querySelectorAll("*")].filter(e => e instanceof HTMLElement);
  const before = new Map(all.map(e => [e, box(e)]));
  const targets = [...new Set([...subjects].flatMap(s => { try { return [...document.querySelectorAll(s)]; } catch (e) { return []; } }))]
    .filter(e => e instanceof HTMLElement && before.get(e) !== "hidden").slice(0, 400);
  const moved = [];
  for (const t of targets) {
    const chain = []; for (let e = t; e && e !== document.documentElement; e = e.parentElement) chain.push(e);
    chain.forEach(e => e.classList.add("__hover"));
    for (const e of all) if (before.get(e) !== "hidden" && box(e) !== before.get(e)) {
      moved.push(`${where}: a hover on ${name(t)} moves ${name(e)} (${before.get(e)} to ${box(e)})`); break;
    }
    chain.forEach(e => e.classList.remove("__hover"));
  }
  return [targets.length, moved];
}
try {
  const quiet = document.createElement("style"); quiet.dataset.hoverCopy = "1";
  quiet.textContent = "*, *::before, *::after { transition: none !important; animation: none !important; }";
  document.head.append(quiet);
  await until(() => window.__appReady && q("#sessYouRow .srow"), "Sessions, the first page");
  q("#actor").value = "mark"; q("#actor").dispatchEvent(new Event("change"));
  const out = { moved: [], hovered: {} };
  const look = async (where) => { await sleep(700); const [n, m] = sweep(where); out.hovered[where] = n; out.moved.push(...m); };
  for (const tab of [...document.querySelectorAll("nav button[data-tab]")].map(b => b.dataset.tab)) {
    q(`nav button[data-tab="${tab}"]`).click();
    await until(() => !q("#tab-" + tab).classList.contains("hidden"), "the tab " + tab);
    if (tab !== "plan") { await look(tab); continue; }
    for (const v of ["overview", "items", "goals", "graph", "releases", "done"]) { q(`[data-plan-view="${v}"]`).click(); await look("plan/" + v); }
  }
  q('nav button[data-tab="sessions"]').click();
  q("#sessYouRow .srow-main").click(); await look("sessions/you");
  q("#sessArtsRow .srow-main").click(); await look("sessions/artifacts");
  post(out);
} catch (e) { post({ error: String(e && e.stack || e) }); }'''

    def sweep_hovers(self, size):
        core.config_set(self.c, "setup_done", "on")
        core.goal_add(self.c, "site", "launch", outcome="the site is live", done_when="it answers")
        core.target_add(self.c, "prod", description="push to main")
        core.project_target(self.c, "site", "prod")
        later = core.item_add(self.c, "site", "footer", goals=["launch"])["id"]
        core.block(self.c, later, reason="waits on the logo", actor="mark")
        core.item_add(self.c, "site", "Approve the logo", doer="human")
        first = core.item_add(self.c, "site", "logo", doer="human")["id"]
        core.claim(self.c, first, "mark")
        core.done(self.c, first, output="drawn", actor="mark")
        core.item_add(self.c, "site", "the pricing page", goals=["launch"])
        core.send(self.c, "question", "Which color?", to="mark", actor="ag")
        got = self.drive(self.HOVER_SWEEP, "/sessions/ag", size=size)
        self.assertGreater(got["hovered"]["sessions"], 5, got["hovered"])  # the sweep found the rows and the buttons
        self.assertEqual(got["moved"], [], "\n".join(got["moved"]))

    # Typing in the message box (#5111, mark: 'when typing into the input bar the chat above shifts a lot and flashes
    # and a scroll bar appear a lot and it cuts and shifts a lot'): 200 characters and 5 new lines, a frame after each
    # key. A chat element that moves on the screen, a chat node that is replaced, a scroll of the chat, or a scroll bar
    # of the box below its height limit fails.
    TYPING = r'''
const frame = () => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
try {
  await until(() => window.__appReady && q("#sessCol .msg, #sessCol .bubble, #sessCol > *"), "the chat of ag");
  await document.fonts.ready;
  // The page settles first (its fonts, the first answers): the frame of the chat stays the same for longer than a poll.
  for (let last = "", still = 0; still < 2500; ) {
    const r = q("#sessBody").getBoundingClientRect(), now = `${r.top},${r.height},${q("#sessBody").scrollTop}`;
    still = now === last ? still + 100 : 0; last = now; await sleep(100);
  }
  const body = q("#sessBody"), text = q("#sessText");
  check(body.scrollHeight > body.clientHeight + 50, "the chat is long enough to scroll");
  const out = { moved: [], replaced: 0, scrolled: 0, boxbar: 0, keys: 0 };
  const els = () => [...q("#sessCol").querySelectorAll("*")];
  const where = () => new Map(els().map(e => { const r = e.getBoundingClientRect(); return [e, `${r.left},${r.top},${r.width},${r.height}`]; }));
  const seen = new MutationObserver(ms => { for (const m of ms) out.replaced += m.removedNodes.length; });
  seen.observe(q("#sessCol"), { childList: true, subtree: true });
  body.addEventListener("scroll", () => out.scrolled++);
  const top = body.scrollTop, before = where();
  text.focus();
  const keys = [...("Please look at the header of the checkout page. ".repeat(5).slice(0, 200))];
  for (let i = 0; i < 5; i++) keys.splice(40 * (i + 1), 0, "\n");
  for (const k of keys) {
    text.setRangeText(k, text.selectionStart, text.selectionEnd, "end");
    text.dispatchEvent(new InputEvent("input", { bubbles: true, data: k, inputType: k === "\n" ? "insertLineBreak" : "insertText" }));
    await frame();
    out.keys++;
    if (text.scrollHeight > text.clientHeight + 1 && text.offsetHeight < innerHeight / 3 - 2) out.boxbar++;
    const now = where();
    for (const [e, b] of before) if (e.isConnected && now.get(e) !== b) { out.moved.push(`key ${out.keys}: ${e.tagName}.${e.className} ${b} to ${now.get(e)}`); break; }
    if (out.moved.length > 3) break;
  }
  out.top = body.scrollTop === top;
  out.win = [scrollY, document.documentElement.scrollHeight, innerHeight, q("#sessLayout").getBoundingClientRect().top, q("#sessLayout").style.height];
  out.grew = text.offsetHeight > 60;  // the box did grow with its lines
  seen.disconnect();
  post(out);
} catch (e) { post({ error: String(e && e.stack || e) }); }'''

    def type_in_the_box(self, size):
        from tests.test_view_sessions import claude_line
        self.pane["screen"] = "❯ \n"  # the session waits at its prompt: no permission prompt comes in while the test types
        self.write("".join(claude_line("assistant", [{"type": "text", "text": f"Step {n}: I read the file and wrote a line "
                                                       f"about it, so the chat is long."}], "end_turn")
                           + claude_line("user", f"Next one, please ({n}).") for n in range(40)))
        got = self.drive(self.TYPING, "/sessions/ag", size=size)
        self.assertEqual(got["keys"], 205, got)
        self.assertTrue(got["grew"], got)
        self.assertEqual((got["moved"], got["replaced"], got["scrolled"], got["boxbar"], got["top"]), ([], 0, 0, 0, True),
                         "\n".join(got["moved"]) or got)

    def test_archive_a_project_in_plan_and_restore_it(self):
        """#5197: Archive on a project of Plan > Items asks first and says that nothing is deleted; the project then
        leaves the left bar, Archived projects (closed at first) lists it, and Restore brings it back."""
        core.project_add(self.c, "old")
        core.item_add(self.c, "old", "a leftover")
        driver = r"""
try {
  await until(() => window.__appReady && q("#sessYouRow .srow"), "the page");
  q("#actor").value = "mark"; q("#actor").dispatchEvent(new Event("change"));
  q('nav button[data-tab="plan"]').click(); q('[data-plan-view="items"]').click();
  await until(() => q('#projects [data-archive-project="old"]'), "Archive on the project old");
  check(!q("[data-plan-archived]"), "no Archived projects while none is archived");
  q('#projects [data-archive-project="old"]').click();
  await until(() => q("#archDlg") && !q("#archDlg").classList.contains("hidden"), "the confirm dialog");
  const text = q("#archDlg").textContent;
  check(/1 open item leaves the queue/.test(text) && /Nothing is deleted/.test(text), "the dialog says what archiving does: " + text);
  q("[data-arch-cancel]").click();
  await sleep(300);
  check(q('#planSide [data-scope-project="old"]'), "Cancel archives nothing");
  q('#projects [data-archive-project="old"]').click();
  await until(() => !q("#archDlg").classList.contains("hidden"), "the dialog again");
  q("[data-arch-go]").click();
  await until(() => !q('#planSide [data-scope-project="old"]') && q("[data-plan-archived]"), "old leaves the left bar");
  check(/Archived projects \(1\)/.test(q("[data-plan-archived]").textContent) && !q("#planArchived"), "the list is closed at first");
  q("[data-plan-archived]").click();
  await until(() => q('#planArchived [data-restore-project="old"]'), "Restore in the Archived projects list");
  check(/1 open/.test(q("#planArchived").textContent), "its open items: " + q("#planArchived").textContent);
  q('#planArchived [data-restore-project="old"]').click();
  await until(() => q('#planSide [data-scope-project="old"]') && !q("[data-plan-archived]"), "old is back");
  post({ ok: true });
} catch (e) { post({ error: String(e.stack || e) }); }
"""
        self.drive(driver)
        self.assertEqual(core.project_archived(self.c), [])
        self.assertEqual([e["change"] for e in core.recent_events(self.c, 2)], ["project old restored", "project old archived"])

    def test_a_table_in_a_message_keeps_its_words_whole_in_a_wide_chat(self):
        """#5155 (mark: 'look at the format of that table. we need have the chat size be a bit wider'): at 1280 px the
        chat column takes most of the pane, and in a 5-column table no cell breaks inside a word ('Ti me r'): a short
        column takes the width of its longest word, long text wraps between words, and the page does not scroll
        sideways (a table wider than its bubble scrolls in its own box)."""
        from tests.test_view_sessions import claude_line
        long = ("the release went out at 07:00 after the review passed and the hook ran without errors, and the "
                "monitor saw no new errors in the log for an hour")
        table = ("Here is the state of the targets:\n\n| Target | Timer | Gate | Who pushes | Result |\n|---|---|---|---|---|\n"
                 + "".join(f"| prod{n} | none | review | the deployer | {long} |\n" for n in range(3))
                 + "| docs | 2h | none | manager | ok |\n")
        wide = "| " + " | ".join(f"Column{n}" for n in range(14)) + " |\n|" + "---|" * 14 + "\n| " + " | ".join(
            f"value{n}" for n in range(14)) + " |\n"
        self.write(claude_line("assistant", [{"type": "text", "text": table}], "end_turn")
                   + claude_line("assistant", [{"type": "text", "text": "And a wide one:\n\n" + wide}], "end_turn"))
        driver = r'''try {
  await until(() => window.__appReady && q("#sessCol .msg-agent table"), "the table in the chat");
  await sleep(300);
  const col = q("#sessCol"), body = q("#sessBody");
  const broken = [];
  for (const cell of col.querySelectorAll(".msg-agent th, .msg-agent td")) {
    const walk = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
    for (let t = walk.nextNode(); t; t = walk.nextNode()) {
      for (const m of t.data.matchAll(/\S+/g)) {
        const r = document.createRange(); r.setStart(t, m.index); r.setEnd(t, m.index + m[0].length);
        const tops = new Set([...r.getClientRects()].map(x => Math.round(x.top)));
        if (tops.size > 1) broken.push(m[0]);
      }
    }
  }
  const tables = [...col.querySelectorAll(".msg-agent table")];
  const last = tables[0].querySelector("tbody tr td:last-child");
  const one = tables[0].querySelector("tbody tr:last-child td:last-child");  // 'ok': one line
  post({ broken, colw: col.getBoundingClientRect().width, panew: body.clientWidth,
         wraps: last.getBoundingClientRect().height > 2 * one.getBoundingClientRect().height,
         pageSide: body.scrollWidth - body.clientWidth,
         ownScroll: tables[1].scrollWidth > tables[1].clientWidth,
         inBubble: tables.every(t => t.getBoundingClientRect().right <= t.closest(".msg-agent").getBoundingClientRect().right + 1) });
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        got = self.drive(driver, "/sessions/ag", size="1280,900")
        self.assertEqual(got["broken"], [], got)
        self.assertGreater(got["colw"], 0.85 * got["panew"], got)
        self.assertEqual((got["wraps"], got["pageSide"], got["ownScroll"], got["inBubble"]), (True, 0, True, True), got)

    def test_the_message_bar_and_the_bubbles_share_one_column(self):
        """#5555 (mark, a 2520 px window: the bar's right edge ran about 225 px past the bubbles'): a long agent turn,
        a person's turn and the message bar end at the same left and right edges, at every width, with the side
        panel closed and open."""
        long = " ".join(["the release went out after the review passed and the monitor saw no new errors"] * 8)
        self.write(claude_line("user", "please check the release " + long)
                   + claude_line("assistant", [{"type": "text", "text": long}], "end_turn"))
        driver = r'''try {
  await until(() => window.__appReady && q("#sessCol .msg-agent") && q("#sessCol .msg-user .bubble") && q("#sessBar"), "the chat and its bar");
  await sleep(300);
  const edges = () => {
    const r = s => [...document.querySelectorAll(s)].pop().getBoundingClientRect();  // the long turns come last
    const a = r("#sessCol .msg-agent"), u = r("#sessCol .msg-user .bubble"), b = r("#sessBar");
    return { agent: [Math.round(a.left), Math.round(a.right)], user: Math.round(u.right), bar: [Math.round(b.left), Math.round(b.right)] };
  };
  const out = { closed: edges() };
  const open = q('.sess-lists [data-sp-open="items"]');
  if (open && open.offsetParent) {
    open.click();
    await until(() => !q("#sessPanel").classList.contains("hidden"), "the side panel");
    await sleep(300);
    out.open = edges();
  }
  post(out);
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        for size in ("2520,1400", "1280,900", "390,844"):
            got = self.drive(driver, "/sessions/ag", size=size)
            if size != "390,844":
                self.assertIn("open", got, "the side panel did not open at " + size)
            for state in ("closed", "open"):
                e = got.get(state)
                if not e:
                    continue
                with self.subTest(size=size, panel=state):
                    self.assertLessEqual(abs(e["agent"][0] - e["bar"][0]), 1, got)
                    self.assertLessEqual(abs(e["agent"][1] - e["bar"][1]), 1, got)
                    self.assertLessEqual(abs(e["user"] - e["bar"][1]), 1, got)

    def test_a_task_link_opens_the_task_in_the_work_of_its_session(self):
        """#5572 (mark 2026-11-12: 'understand the task in the context of what the session has done and will do', 'there
        should be a button to Go to Session'): a #id in the chat and the head's task open a large view over the chat:
        the session's line (Done, Now, Next) with the task marked, the task beside it, Go to Session and Open the full
        task. On a phone the task is a sheet at the bottom with Go to Session, and nothing scrolls sideways."""
        first = core.item_add(self.c, "site", "the logo", short="The logo")["id"]
        core.release(self.c, self.it, "later", "ag")
        core.claim(self.c, first, "ag")
        found = core.item_add(self.c, "site", "a broken link in the footer", actor="ag", found_during=first)["id"]
        core.done(self.c, first, output="x", actor="ag", changed="The logo is new")
        core.claim(self.c, self.it, "ag")
        self.write(claude_line("assistant", [{"type": "text", "text": f"I finished #{first} and now work on #{self.it}."}],
                               "end_turn"))
        driver = r'''try {
  await until(() => window.__appReady && q(`#sessCol [data-open="FIRST"]`) && q(`#sessMenuItem [data-open="IT"]`), "the #id in the chat and the head's task");
  const shown = () => q("#taskDlg") && !q("#taskDlg").classList.contains("hidden") && q("#taskDlg .tc-ev.on");
  const vis = (s) => { const e = q(s); return !!(e && e.offsetParent); };
  const t0 = performance.now();
  q(`#sessCol [data-open="FIRST"]`).click();
  await until(shown, "the task view from the chat");
  const ms = Math.round(performance.now() - t0);
  const box = q("#taskDlg .tc-box");
  const out = { ms, head: q("#taskDlg .tc-head h2").textContent, on: q("#taskDlg .tc-ev.on").dataset.tcTask,
    groups: [...document.querySelectorAll("#taskDlg .tc-grp")].map(g => g.textContent),
    kid: (q("#taskDlg .tc-kid") || {}).textContent || "", changed: q("#taskDlg .tc-det").textContent.includes("The logo is new"),
    headGo: vis("#taskDlg .tc-head .tc-gobtn"), sheetGo: vis("#taskDlg .tc-det .tc-gobtn"),
    side: box.scrollWidth - box.clientWidth, wide: box.getBoundingClientRect().width };
  // A row of the line moves the view to that task; the head's task opens it too.
  q(`#taskDlg [data-tc-task="FOUND"]`).click();
  await until(() => shown() && q("#taskDlg .tc-ev.on, #taskDlg .tc-det") && q("#taskDlg .tc-det").textContent.includes("broken link"), "the found task");
  q("#taskDlg [data-tc-close]").click();
  await until(() => q("#taskDlg").classList.contains("hidden"), "closed");
  const head = q(`#sessItem [data-open="IT"]`) || q(`#sessMenuItem [data-open="IT"]`);
  head.click();
  await until(() => shown() && q("#taskDlg .tc-ev.on").dataset.tcTask === "IT", "the task view from the head");
  out.headOn = q("#taskDlg .tc-ev.on").dataset.tcTask;
  out.nowGroup = q("#taskDlg .tc-ev.on").closest(".tc-line") && [...q("#taskDlg .tc-line").children].indexOf(q("#taskDlg .tc-ev.on"))
    > [...q("#taskDlg .tc-line").children].findIndex(c => c.textContent === "Now");
  [...document.querySelectorAll("#taskDlg [data-tc-full]")].find(b => b.offsetParent).click();
  await until(() => q("#taskDlg").classList.contains("hidden") && !q("#sessPanel").classList.contains("hidden"), "the full task in the side panel");
  head.click();
  await until(shown, "the task view again");
  [...document.querySelectorAll("#taskDlg [data-tc-go]")].find(b => b.offsetParent).click();
  await until(() => q("#taskDlg").classList.contains("hidden"), "Go to Session closes the view");
  out.path = location.pathname;
  post(out);
} catch (e) { post({ error: String(e && e.stack || e) }); }'''.replace("FIRST", str(first)).replace("FOUND", str(found)).replace("IT", str(self.it))
        for size in ("1280,900", "390,844"):
            got = self.drive(driver, "/sessions/ag", size=size)
            with self.subTest(size=size):
                self.assertTrue(got["head"].startswith("Session:"), got)
                self.assertEqual((got["on"], got["headOn"]), (str(first), str(self.it)), got)
                self.assertEqual(got["groups"][1:], ["Now", "Next"], got)
                self.assertIn("It found one more task", got["kid"])
                self.assertTrue(got["changed"], got)
                self.assertTrue(got["nowGroup"], got)
                self.assertEqual(got["side"], 0, got)
                self.assertTrue(got["path"].startswith("/sessions/ag"), got)  # the side panel keeps the full task
                self.assertLess(got["ms"], 500, got)  # p147: the route answers in a few ms; the rest is the page
                phone = size.startswith("390")
                self.assertEqual((got["headGo"], got["sheetGo"]), (not phone, phone), got)
                if not phone:
                    self.assertGreater(got["wide"], 900, got)

    def test_typing_in_the_message_box_moves_nothing_at_desktop_width(self):
        """#5111: typing in the message box moves nothing in the chat above it, replaces nothing, scrolls nothing."""
        self.type_in_the_box("1240,900")

    def test_typing_in_the_message_box_moves_nothing_at_phone_width(self):
        """#5111: the same at 390 px."""
        self.type_in_the_box("390,844")

    def test_a_hover_moves_nothing_on_any_tab_at_desktop_width(self):
        """#5106 (mark: 'the pin button when you mouse over it changes to the turn off button. this layout shift is
        really annoying'): on every tab at 1240 px, a hover on any row, button, chip or element with a :hover rule
        moves no other element and hides none."""
        self.sweep_hovers("1240,900")

    def test_a_hover_moves_nothing_on_any_tab_at_phone_width(self):
        """#5106: the same sweep at 390 px."""
        self.sweep_hovers("390,844")

    def test_dictation_in_a_box_and_in_the_message_box(self):
        """#5107: a box that takes text gets a microphone while it has the focus; the page records (Chrome's fake
        microphone here), sends the audio to serve, and the helper's text goes in at the cursor, the words not final
        yet first, then the final text. The message box of a chat has its own microphone, for every provider."""
        from river import dictation
        from tests.test_dictation import FAKE
        fake = Path(self.dir.name, "fake.py")
        fake.write_text(FAKE)
        old, dictation.HELPER = dictation.HELPER, [__import__("sys").executable, str(fake)]
        self.addCleanup(setattr, dictation, "HELPER", old)
        driver = r'''try {
  await until(() => window.__appReady && q("#work .wp, #work .muted"), "the Board drew");
  q("#searchBtn").click();  // the search dialog: its field takes words
  const box = q("#searchText");
  await until(() => !q("#searchDlg").classList.contains("hidden"), "the search dialog");
  box.value = "find "; box.focus(); box.setSelectionRange(5, 5);
  const mic = q(".mic-float");
  await until(() => !mic.classList.contains("hidden"), "the microphone shows beside the focused box");
  const out = { left: mic.getBoundingClientRect().left > box.getBoundingClientRect().left };
  mic.click();
  await until(() => /^find heard \d+ bytes/.test(box.value), "words not final yet show in the box");
  out.focus = document.activeElement === box && mic.classList.contains("on");
  mic.click();
  await until(() => /^find \d+ bytes in [a-z]{2}(-[A-Z]{2})?\.$/.test(box.value) && !mic.classList.contains("on"), "the final text");
  out.final = true;
  q("#searchClose").click();
  await until(() => mic.classList.contains("hidden"), "the microphone goes with the focus");
  q('nav button[data-tab="sessions"]').click();
  await until(() => q("#sessMic") && !q("#sessMic").classList.contains("hidden"), "the message box has its microphone");
  q("#sessText").focus();
  await sleep(100);
  out.own = mic.classList.contains("hidden");  // no second microphone over the message box
  post(out);
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        got = self.drive(driver, "/", flags=("--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"))
        self.assertEqual(got, {"left": True, "focus": True, "final": True, "own": True})

    def test_enter_while_dictating_sends_once_and_the_box_stays_empty(self):
        """#5118 (mark: 'when dicating when we press enter it needs to send the text and stop listening'; one dictated
        message went out three times): Enter while the microphone listens stops it, takes the final text, sends it
        once and empties the box. Audio that comes after it puts nothing back, so the next Enter sends nothing."""
        from river import dictation
        from tests.test_dictation import FAKE
        fake = Path(self.dir.name, "fake.py")
        fake.write_text(FAKE)
        old, dictation.HELPER = dictation.HELPER, [__import__("sys").executable, str(fake)]
        self.addCleanup(setattr, dictation, "HELPER", old)
        self.pane["screen"] = "❯ \n"  # the session waits at its prompt: the box sends
        driver = r'''try {
  const box = () => q("#sessText"), mic = () => q("#sessMic");
  const enter = () => box().dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
  await until(() => window.__appReady && mic() && !mic().classList.contains("hidden") && !mic().disabled && !box().disabled,
              "the message box has its microphone");
  box().focus();
  mic().click();
  // A failure says what the box and the microphone showed (#5583: on the Intel runner of a release, no words came in 15 s).
  const seen = () => " (box " + JSON.stringify(box().value) + ", microphone " + (mic().classList.contains("on") ? "on" : "off")
    + ", line " + JSON.stringify(q("#sessDictate").textContent) + ", box " + (box().disabled ? "disabled" : "enabled") + ")";
  await until(() => /heard \d+ bytes/.test(box().value), "words not final yet show in the box").catch(e => { throw new Error(e.message + seen()); });
  enter();
  await until(() => box().value === "" && !mic().classList.contains("on"), "Enter sent the text and stopped the microphone")
    .catch(e => { throw new Error(e.message + seen()); });
  await sleep(2000);  // the fake microphone goes on; eight flushes of audio would have come
  const out = { empty: box().value === "", off: !mic().classList.contains("on") };
  enter();  // an empty box sends nothing
  await sleep(500);
  post(out);
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        got = self.drive(driver, "/", flags=("--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"))
        self.assertEqual(got, {"empty": True, "off": True})
        typed = [c for c in self.tmux.calls if any("bytes" in str(x) for x in c)]
        self.assertEqual(len(typed), 1, typed)
        self.assertRegex(next(str(x) for x in typed[0] if "bytes" in str(x)), r"^\d+ bytes in [a-z]{2}(-[A-Z]{2})?\.$")

    def test_the_chat_scrolls_up_through_two_compactions_to_the_start(self):
        """#5120: a compaction is a thin divider with its summary folded closed under it; scrolling up loads the earlier
        lines a page at a time, and the lines in sight stay where they are, up to 'Start of the session'."""
        from tests.test_view_sessions import History
        h = History.__new__(History)
        self.path.write_text("")
        self.write(h.lines(0, 200) + h.boundary(1) + h.lines(200, 200) + h.boundary(2) + h.lines(400, 200))
        driver = r'''try {
  const col = q("#sessCol"), body = q("#sessBody");
  const talk = () => col.querySelectorAll(".msg-user, .msg-agent").length;
  await until(() => window.__appReady && talk() > 100, "the chat of ag");
  const out = { first: talk(), jumps: [] };
  for (let i = 0; i < 20 && !col.querySelector('.chat-div[data-kind="start"]'); i++) {
    const anchor = col.firstElementChild;
    body.scrollTop = 0;
    const y = anchor.getBoundingClientRect().top;
    await until(() => col.firstElementChild !== anchor, "earlier lines came");
    out.jumps.push(Math.round(anchor.getBoundingClientRect().top - y));
  }
  const divs = [...col.querySelectorAll(".chat-div")];
  out.all = talk();
  out.divs = divs.map(d => d.dataset.kind);
  out.compacted = divs.filter(d => d.dataset.kind === "compacted").every(d => /^Compacted · /.test(d.textContent));
  out.summaries = [...col.querySelectorAll("details.chat-summary")].map(d => d.open);
  out.start = col.firstElementChild.textContent;
  out.person = [...col.querySelectorAll(".msg-user")].some(m => /summary/.test(m.textContent));
  post(out);
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        got = self.drive(driver, "/")
        self.assertLess(got.get("first", 0), 1200, got)
        self.assertEqual((got["all"], got["divs"], got["compacted"], got["summaries"], got["start"], got["person"]),
                         (1200, ["start", "compacted", "compacted"], True, [False, False], "Start of the session", False))
        self.assertTrue(got["jumps"] and all(abs(j) <= 1 for j in got["jumps"]), got["jumps"])

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

    def test_a_collapsed_row_on_a_phone_lists_only_the_columns_with_a_value(self):
        """#5085: at 390 px an item table puts the columns that do not fit under each row; a column with no value
        (no due date, nothing it waits on, no goal, anyone may do it) is left out there."""
        plain = core.item_add(self.c, "site", "plain")["id"]
        dated = core.item_add(self.c, "site", "dated", doer="human", due="2030-01-01")["id"]
        driver = r'''try {
  await until(() => window.__appReady, "app.js loaded");
  q('nav button[data-tab="plan"]').click(); q('[data-plan-view="items"]').click();
  await until(() => q("#projects [data-ptable], #projects [data-fold]"), "the project cards");
  if (!q("#projects .tabulator-row")) { const f = q("#projects [data-fold]"); if (f) f.click(); }
  await until(() => q("#projects .tabulator-responsive-collapse"), "a collapsed row");
  const text = (id) => { const r = [...document.querySelectorAll("#projects .tabulator-row")].find(x => x.querySelector(".id, [tabulator-field=id]") && x.textContent.includes(id));
    const c = r && r.querySelector(".tabulator-responsive-collapse"); return c ? c.textContent.replace(/\s+/g, " ") : null; };
  post({ plain: text("plain"), dated: text("dated") });
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        r = self.drive(driver, "/", size="390,844")
        self.assertIsNotNone(r["plain"], r)
        for label in ("Due", "Waits on", "Goals", "Who"):
            self.assertNotIn(label, r["plain"], r)
        self.assertIn("P2", r["plain"], r)
        self.assertIn("Due", r["dated"], r)
        self.assertIn("Who", r["dated"], r)

    def test_rows_of_a_long_session_list_keep_their_whole_height(self):
        """#4723: in the list (a flex column of fixed height) the rows shrank and cut off their last lines; now each
        row keeps its height and the list scrolls."""
        driver = r'''try {
  await until(() => window.__appReady, "app.js loaded");
  q('nav button[data-tab="sessions"]').click();
  await until(() => q("#tab-sessions .slist .srow"), "a session row");
  // A refresh of the list (every few seconds, and again when one comes during a read: #5087) draws the rows anew
  // and drops the clones (#5090): add them again until the list is long, and measure in the same turn.
  let list, cut;
  await until(() => {
    list = q("#tab-sessions .slist");
    const rows = list.querySelectorAll(".srow");
    if (rows.length < 16) for (let i = rows.length; i < 16; i++) list.appendChild(rows[0].cloneNode(true));
    cut = [...list.querySelectorAll(".srow")].filter(r => r.scrollHeight > r.clientHeight + 1);
    return list.scrollHeight > list.clientHeight;
  }, "the list is longer than its box");
  check(!cut.length, cut.length + " rows cut off their content: " + (cut[0] && cut[0].clientHeight + " < " + cut[0].scrollHeight));
  post({ ok: true });
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        # A desktop window: on a phone (under 760 px, the default window here) the tab shows the list or the chat,
        # and the list has no fixed height (#4885).
        self.assertEqual(self.drive(driver, "/sessions/ag", size="1280,900"), {"ok": True})

    def test_the_managers_proposal_of_an_expert_is_a_card_in_its_chat(self):
        """#4990: the manager proposes an expert; its chat shows a card with the topic and the model. Dismiss closes
        it (on a phone, 390 px); Continue in an expert starts the expert and opens its chat (1280 px)."""
        core.project_path(self.c, "site", self.dir.name)
        p = core.expert_propose(self.c, "rx", "the refund rules", "site", model="opus", summary="30 days", actor="ag")
        driver = r'''try {
  await until(() => window.__appReady, "app.js loaded");
  await until(() => q("#sessTitle").textContent === "ag" && !q("#sessPropose").classList.contains("hidden"), "the card");
  const card = q("#sessPropose");
  check(/^the (refund rules|pricing design)$/.test(card.querySelector(".sp-topic").textContent) && card.textContent.includes("opus"), "topic and model");
  check(q("[data-propose-go]").textContent === "Continue in an expert", "the start button");
  check(document.documentElement.scrollWidth <= innerWidth && card.scrollWidth <= card.clientWidth + 1, "no sideways scroll");
  const want = new URL(location).searchParams.get("act") || location.hash.slice(1);
  if (want === "go") {
    q("[data-propose-go]").click();
    try { await until(() => q("#sessTitle").textContent === "px", "the expert's chat opens"); }
    catch (e) { throw new Error(e.message + "; the page says: " + q("#toast").textContent + "; title " + q("#sessTitle").textContent); }
    check(q("#sessPropose").classList.contains("hidden"), "the expert's chat shows no card");
  } else {
    q("[data-propose-no]").click();
    await until(() => q("#sessPropose").classList.contains("hidden"), "Dismiss closes the card");
  }
  post({ ok: true });
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        self.assertEqual(self.drive(driver, "/sessions/ag#no", size="390,844"), {"ok": True})
        self.assertEqual(core._proposal(self.c, p["id"])["state"], "dismissed")
        p = core.expert_propose(self.c, "px", "the pricing design", "site", model="opus", summary="two plans", actor="ag")
        self.assertEqual(self.drive(driver, "/sessions/ag#go", size="1280,900"), {"ok": True})
        self.assertEqual(core._proposal(self.c, p["id"])["state"], "started")
        self.assertEqual(core.expert_task(self.c, "px") is not None, True)

    def test_each_session_keeps_its_draft_through_a_switch_and_a_reload(self):
        """#5112 (mark: 'we need to maintain what is in the input for each session when we switch session as well as
        when we reload'): text typed for session A, then B, both come back after a switch and after a reload of the
        page, with A's cursor and its attached file (saved at once; the draft holds its id). Send empties A's draft."""
        self.pane["screen"] = "❯ \n"
        core.register(self.c, "wt")
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET role='waiting', waiting_in='site' WHERE name='wt'")
        self.tmux._pane(name="wt")
        self.tmux.panes[-1].update(agent="wt", screen="❯ \n")
        driver = r'''try {
  const go = async (name) => {
    history.pushState(null, "", "/sessions/" + name); dispatchEvent(new PopStateEvent("popstate"));
    await until(() => q("#sessTitle").textContent === name && !q("#sessText").disabled, "the chat of " + name);
  };
  const type = (t) => { const x = q("#sessText"); x.focus(); x.setRangeText(t, x.selectionStart, x.selectionEnd, "end");
                        x.dispatchEvent(new InputEvent("input", { bubbles: true, data: t, inputType: "insertText" })); };
  await until(() => window.__appReady && q("#sessTitle").textContent === "ag" && !q("#sessText").disabled, "the chat of ag");
  if (!sessionStorage.getItem("reloaded")) {
    type("first line for ag");
    const x = q("#sessText"); x.setSelectionRange(5, 5);  // the cursor in the middle
    const dt = new DataTransfer(); dt.items.add(new File(["notes"], "notes.txt", { type: "text/plain" }));
    q("#sessPickFiles").files = dt.files; q("#sessPickFiles").dispatchEvent(new Event("change"));
    await until(() => q("#sessFiles .sess-file") && !q("#sessSend").disabled, "the file is attached and saved");
    await go("wt");
    check(q("#sessText").value === "" && !q("#sessFiles .sess-file"), "wt starts with an empty box");
    type("a note for wt");
    await go("ag");
    const back = { text: q("#sessText").value, at: q("#sessText").selectionStart, file: q("#sessFiles").textContent.includes("notes.txt") };
    sessionStorage.setItem("back", JSON.stringify(back));
    sessionStorage.setItem("reloaded", "1");
    location.reload();
  } else {
    const out = { back: JSON.parse(sessionStorage.getItem("back")) };
    await until(() => q("#sessText").value, "ag's draft after the reload");
    out.ag = { text: q("#sessText").value, at: q("#sessText").selectionStart, file: q("#sessFiles").textContent.includes("notes.txt") };
    await go("wt");
    out.wt = q("#sessText").value;
    await go("ag");
    q("#sessBar").requestSubmit();
    await until(() => q("#sessText").value === "", "Send empties the box");
    out.after = localStorage.getItem("river.draft.ag");
    post(out);
  }
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        got = self.drive(driver, "/sessions/ag")
        same = {"text": "first line for ag", "at": 5, "file": True}
        self.assertEqual(got, {"back": same, "ag": same, "wt": "a note for wt", "after": None})
        sent = [c[-1] for c in self.tmux.calls if c[0] == "set-buffer"]
        self.assertTrue(any(x.startswith(" Attached files:") and "notes.txt\n\nfirst line for ag" in x for x in sent), sent)

    def _stop_and_send_now(self, size):
        """#5547, #5587 (mark 2026-11-12: 'the stop button should not be up at the top'): ag waits at a permission
        prompt in its pane, so it can be stopped. With the box empty, a Stop square stands in the send arrow's place in
        the message bar, and the head has no Stop; with text, the arrow and the bolt (Send now) show instead. Both fit
        the box at this width, and the square reads in light and in dark."""
        driver = r'''try {
  await until(() => window.__appReady && q("#sessTitle").textContent === "ag" && !q("#sessText").disabled
                    && q("#sessBar").classList.contains("can-now"), "the chat of ag that can stop");
  const box = (s) => q(s).getBoundingClientRect(), head = q(".sess-head");
  const reads = () => { const cs = getComputedStyle(q("#sessStop")); return cs.color !== cs.backgroundColor; };
  const out = { headFits: head.scrollWidth <= head.clientWidth + 1, headStop: !!head.querySelector(":scope > [data-stop], :scope > * > [data-stop]:not(.sess-mi)"),
                stopIn: box("#sessStop").width > 0 && box("#sessStop").right <= q("#sessText").parentElement.getBoundingClientRect().right,
                arrowHidden: box("#sessSend").width === 0 && box("#sessNow").width === 0, light: reads() };
  document.documentElement.dataset.theme = "dark"; out.dark = reads(); delete document.documentElement.dataset.theme;
  const x = q("#sessText"); x.focus(); x.setRangeText("use the other file", 0, 0, "end");
  x.dispatchEvent(new Event("input", { bubbles: true }));
  await until(() => box("#sessNow").width > 0, "Send now shows with text");
  out.nowIn = box("#sessNow").right <= box("#sessSend").left + 1 && box("#sessSend").right <= innerWidth;
  out.stopHidden = box("#sessStop").width === 0;
  q("#sessNow").click();
  await until(() => x.value === "" && box("#sessStop").width > 0, "Send now empties the box; Stop is back");
  await fetch("/__step/after_now");
  await until(() => q("#sessOpen [data-stop]"), "Stop its turn in the menu");
  out.menu = [...q("#sessOpen").querySelectorAll("button, a")].map(b => b.childNodes[0].textContent.trim());
  q("#sessStop").click();
  await sleep(400);
  post(out);
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        keys = {}
        self.c.execute("UPDATE agents SET agent_type='claude-code' WHERE name='ag'")
        self.pane["keys"].clear()
        got = self.drive(driver, "/sessions/ag", size=size, steps={"after_now": lambda: keys.update(now=list(self.pane["keys"]))})
        self.assertEqual(got, {"headFits": True, "headStop": False, "stopIn": True, "arrowHidden": True, "light": True,
                               "dark": True, "nowIn": True, "stopHidden": True,
                               "menu": ["Open on claude.ai", "Stop its turn", "Pin to the top", "Settings", "End"]})
        self.assertEqual(keys["now"], ["Escape", "use the other file", "Enter"])
        self.assertEqual(self.pane["keys"][3:], ["Escape"])  # Stop

    def test_stop_and_send_now_at_desktop_width(self):
        self._stop_and_send_now("1280,900")

    def test_stop_and_send_now_at_phone_width(self):
        self._stop_and_send_now("390,844")

    def test_the_sessions_tab_on_a_phone(self):
        """#4885: at phone width the page is no wider than the screen, each field has 16 px text (Safari on iOS zooms
        into a smaller one), and the Sessions tab shows the chat of one session or the list: Sessions goes back to
        the list, a row shows its chat again."""
        driver = r'''try {
  await until(() => window.__appReady, "app.js loaded");
  q('nav button[data-tab="sessions"]').click();
  await until(() => q("#sessTitle").textContent === "ag" && q("#tab-sessions .slist .srow"), "session ag shown");
  const lay = q("#sessLayout"), shown = (el) => el.getBoundingClientRect().height > 0;
  const out = { width: innerWidth, wide: document.documentElement.scrollWidth > innerWidth,
    fonts: ["#search", "#sessText"].map(s => getComputedStyle(q(s)).fontSize),
    chat: lay.classList.contains("phone-chat") && shown(q("#sessMain")) && !shown(q(".sess-side")) && shown(q("#sessBack")) };
  q("#sessBack").click();
  out.list = !lay.classList.contains("phone-chat") && shown(q(".sess-side")) && !shown(q("#sessMain"));
  q('#sessions .srow[data-session] .srow-main').click();  // not the manager's empty row
  await until(() => lay.classList.contains("phone-chat"), "the chat again");
  out.again = shown(q("#sessMain")) && !shown(q(".sess-side"));
  post(out);
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        got = self.drive(driver, "/sessions/ag", size="390,844")
        self.assertEqual(got, {"width": got.get("width"), "wide": False, "fonts": ["16px", "16px"], "chat": True,
                               "list": True, "again": True})
        self.assertLess(got["width"], 760)

    def test_the_you_row_opens_what_waits_on_you(self):
        """#5102 steps 1 and 3 (design #4657): the You row at the top of the Sessions list has a ! badge with what waits on
        the person, and opens the You view (Needs you, the inbox) at /you in place of a session's chat; a session row
        shows its chat again, Back returns to You, and the Board's 'need you' count goes there too. The Artifacts row
        under it shows the artifacts of all sessions at /artifacts, in place of the Artifacts tab."""
        core.config_set(self.c, "setup_done", "on")
        core.item_add(self.c, "site", "Approve the logo", doer="human")
        core.send(self.c, "question", "Which color?", to="mark", actor="ag")
        driver = r'''try {
  await until(() => window.__appReady && q("#sessYouRow .wbadge"), "the You row with its badge");
  const shown = (el) => el.getBoundingClientRect().height > 0;
  const out = { badge: q("#sessYouRow .wbadge").textContent.trim() };
  q("#sessYouRow .srow-main").click();
  await until(() => location.pathname === "/you" && shown(q("#sessYou")), "the You view at /you");
  await until(() => q("#needsYou").textContent.includes("Approve the logo") && q("#inboxTitle").textContent.includes("mark"), "Needs you and the inbox");
  out.you = !shown(q("#sessMain")) && q("#sessYouRow .srow").classList.contains("on") && !q("#sessions .srow.on");
  // The session rows come from a poll of their own (api/sessions), which can end after the You view shows (#5272).
  await until(() => q('#sessions .srow[data-session="ag"] .srow-main'), "the session row of ag");
  q('#sessions .srow[data-session="ag"] .srow-main').click();
  await until(() => location.pathname === "/sessions/ag" && shown(q("#sessMain")), "the chat of ag");
  out.chat = !shown(q("#sessYou")) && !q("#sessYouRow .srow").classList.contains("on");
  history.back();
  await until(() => location.pathname === "/you" && shown(q("#sessYou")), "Back shows You again");
  await until(() => q("#sessArtsRow .srow-main"), "the Artifacts row");
  q("#sessArtsRow .srow-main").click();
  await until(() => location.pathname === "/artifacts" && shown(q("#sessArts")) && !shown(q("#sessYou")), "the Artifacts row: /artifacts");
  out.arts = q("#sessArtsRow .srow").classList.contains("on") && !q("#sessYouRow .srow").classList.contains("on")
    && !q('nav button[data-tab="artifacts"]');
  history.back();
  await until(() => location.pathname === "/you" && shown(q("#sessYou")) && !shown(q("#sessArts")), "Back from Artifacts to You");
  post(out);
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        got = self.drive(driver, "/sessions/ag")
        self.assertEqual(got, {"badge": "! 2", "you": True, "chat": True, "arts": True})

    def test_the_you_row_counts_all_that_needs_you_and_not_what_was_taken_off(self):
        """#5114, mark 2026-11-10: the You row said '! 900' and '100 need you · 800 taken off your list'. 100 was the
        cap of the Needs you list, not a count, and the badge added the items an agent took off the list, which need
        nothing. Now the line has the true counts, the badge counts only what needs the person now, and the list says
        that it shows the most important 100."""
        core.config_set(self.c, "setup_done", "on")
        ids = [core.item_add(self.c, "site", f"Sign form {i}", doer="human")["id"] for i in range(106)]  # 1 message: the takeover note
        core.register(self.c, "helper")
        core.takeover(self.c, ids[0], "I fill it in myself", actor="helper")
        # The inbox comes in a poll of its own, after the state: wait for its count in the line, not only for the
        # badge (#5153: on a slow CI machine the line was read before ' · 1 message' came).
        driver = r'''try {
  await until(() => window.__appReady && q("#sessYouRow .wbadge") && /message/.test(q("#sessYouRow .sub").textContent),
              "the You row with its badge and the count of the inbox");
  const out = { badge: q("#sessYouRow .wbadge").textContent.trim(), line: q("#sessYouRow .sub").textContent.trim(),
                // One line under the You row: its own, not also its group's (#5114: two lines with nothing between).
                lines: [q("#sessYouRow").closest(".slist-mgr"), q("#sessYouRow .srow")].map(e => getComputedStyle(e).borderBottomWidth).join(" ") };
  q("#sessYouRow .srow-main").click();
  await until(() => q("#needsYou .ny-more"), "the note under the list");
  out.list = document.querySelectorAll("#needsYou .nyl").length;
  out.more = q("#needsYou .ny-more").textContent.replace(/\s+/g, " ").trim();
  out.title = document.title;
  post(out);
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        got = self.drive(driver, "/sessions/ag")
        self.assertTrue(got.pop("more").startswith("The 100 most important of 105."))
        self.assertEqual(got, {"badge": "! 105", "line": "105 need you · 1 taken off your list · 1 message", "lines": "0px 1px",
                               "list": 100, "title": "(105) MaximizePM"})

    def test_the_first_page_is_sessions_with_you_when_something_waits(self):
        """#5102 step 5 (design #4657, mark's answer 2A): / is Sessions; it shows the You view when something waits on
        the person, else the session chosen last. The Board and Capacity tabs are gone."""
        core.config_set(self.c, "setup_done", "on")
        core.send(self.c, "question", "Which color?", to="mark", actor="ag")
        driver = r'''try {
  await until(() => window.__appReady && !q("#sessYou").classList.contains("hidden"), "the You view on the first page");
  post({ path: location.pathname, tab: q("#tabName").textContent, tabs: [...document.querySelectorAll("nav button")].map(b => b.dataset.tab) });
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        self.assertEqual(self.drive(driver, "/?as=mark"), {"path": "/you", "tab": "Sessions",
                                                           "tabs": ["sessions", "plan", "terminal"]})

    def test_the_work_controls_of_the_sessions_list(self):
        """#5102 step 2 (design #4657): Start an agent at the top of the list; Show ended at its foot, with the ended
        sessions; Give work in the head of a waiting session; the queue and + item in a session's Items list."""
        core.config_set(self.c, "setup_done", "on")
        footer = core.item_add(self.c, "site", "footer")["id"]
        core.register(self.c, "old", note="built the logo")
        core.register(self.c, "wt")
        core.queue_add(self.c, "ag", footer, actor="mark")
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET last_seen='2026-01-01T00:00:00Z' WHERE name='old'")
            self.c.execute("UPDATE agents SET role='waiting', waiting_in='site' WHERE name='wt'")
        driver = r'''try {
  await until(() => window.__appReady && q("#sessStart [data-launch]") && q("#sessFoot [data-sess-ended]"), "Start an agent and Show ended");
  q("#sessFoot [data-sess-ended]").click();
  await until(() => q('#sessEnded .srow[data-session="old"]'), "the ended session");
  check(q('#sessions .srow[data-session="ag"][data-agent="ag"]'), "a running row is a drop target");
  q('[data-sp-open="items"]').click();
  await until(() => q("#spBody .sp-queue") && q("#spBody .sp-queue").textContent.includes("#FOOTER"), "the queue in Items");
  q("#spBody [data-sp-add] input").value = "found in passing";
  q("#spBody [data-sp-add]").requestSubmit();
  await until(() => q("#toast").textContent.includes("found during #ITEM"), "+ item adds it");
  history.pushState(null, "", "/sessions/wt"); dispatchEvent(new PopStateEvent("popstate"));
  await until(() => q("#sessGive [data-give]"), "Give work for the waiting session");
  q("#sessGive [data-give]").click();
  await until(() => q("#toast").textContent.includes("Gave #"), "Give work pushes the item");
  post({ ok: true });
} catch (e) { post({ error: String(e && e.stack || e) }); }'''.replace("FOOTER", str(footer)).replace("ITEM", str(self.it))
        self.assertEqual(self.drive(driver, "/sessions/ag"), {"ok": True})
        found = [i for i in core.item_list(self.c, "site") if i["title"] == "found in passing"]
        self.assertEqual(len(found), 1)
        self.assertEqual(core._item(self.c, found[0]["id"])["reserved_for"], "wt")  # footer is in the queue of ag

    def test_the_message_box_stays_above_the_phone_keyboard(self):
        """#4902: on an iPhone the keyboard makes the visible part of the page (visualViewport) smaller and scrolls the
        page; the Sessions tab then covers that visible part, so the message box sits right above the keyboard. A
        browser here has no keyboard: a stand-in visualViewport says what an iPhone's says. #4910: iOS Safari makes
        window.innerHeight smaller with the keyboard too; the second run says so, and the tab still covers the visible
        part."""
        driver = r'''try {
  await until(() => window.__appReady, "app.js loaded");
  q('nav button[data-tab="sessions"]').click();
  await until(() => q("#sessTitle").textContent === "ag", "session ag shown");
  const lay = q("#sessLayout"), box = q("#sessText");
  // As for a session that the page can type into: the box shows and stays enabled (the poll hides and disables it
  // for this one, which takes the focus away).
  const sbar = q("#sessBar"); sbar.classList.remove("hidden"); sbar.classList.toggle = () => false; box.disabled = false;
  Object.defineProperty(box, "disabled", { get: () => false, set: () => {}, configurable: true });
  const FULL = innerHeight, vv = Object.assign(new EventTarget(), { height: Math.round(innerHeight * 0.55), offsetTop: 230, width: innerWidth });
  Object.defineProperty(window, "visualViewport", { value: vv, configurable: true });
  if (IOS) Object.defineProperty(window, "innerHeight", { get: () => vv.height, configurable: true });
  // The session waits on the person: the page opens its dialog once by itself, which takes the focus. Close it first.
  await until(() => q("#waitDlg") && !q("#waitDlg").classList.contains("hidden"), "the wait dialog").catch(() => {});
  if (q("#waitDlg")) q("#waitDlg").classList.add("hidden");
  box.focus();
  await sleep(450);
  const r = lay.getBoundingClientRect(), bar = q("#sessBar").getBoundingClientRect();
  const out = { kb: lay.classList.contains("phone-kb"), fixed: getComputedStyle(lay).position,
    top: Math.round(r.top), height: Math.round(r.height), want: [vv.offsetTop, vv.height],
    barEnd: Math.round(vv.offsetTop + vv.height - bar.bottom) };
  // iOS moves the page while the keyboard is open. The page listens to the real visualViewport (from its load), not
  // to this stand-in: a resize of the window runs the same fit.
  vv.offsetTop = 260; dispatchEvent(new Event("resize"));
  await sleep(100);
  out.moved = Math.round(lay.getBoundingClientRect().top);
  vv.height = FULL; vv.offsetTop = 0;  // the keyboard closes
  box.blur();
  await sleep(450);
  out.after = { kb: lay.classList.contains("phone-kb"), top: lay.style.top, fixed: getComputedStyle(lay).position };
  post(out);
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        for ios in (False, True):
            with self.subTest(ios=ios):
                got = self.drive(f"const IOS = {'true' if ios else 'false'};\n" + driver, "/sessions/ag", size="390,844")
                self.assertTrue(got["kb"], got)
                self.assertEqual((got["fixed"], got["top"], got["height"]), ("fixed", *got["want"]))
                self.assertGreaterEqual(got["barEnd"], 0, "the message box is inside the visible part")
                self.assertLess(got["barEnd"], 40, "and right above the keyboard: no empty band under it")
                self.assertEqual(got["moved"], 260)
                self.assertEqual(got["after"], {"kb": False, "top": "", "fixed": "static"})

    def test_the_header_of_a_phone_is_one_bar(self):
        """#4903: at phone width the header is one bar of 48 px: the mark, the tab's name, which opens the sheet of all
        tabs, the magnifier, which opens the search, and the ⋯ menu (Settings is there, #5116). The bar hides while the page scrolls down and
        comes back on a scroll up; the session head is one line (#5115) with the name, the state and the ⋯ (#5402:
        Items and Artifacts left the name about 12 characters, so they are rows of the ⋯ card), and its ⋯ shows a card
        with Items, Artifacts, Open session, Settings and End; a tap outside closes it."""
        for i in range(40):
            core.item_add(self.c, "site", f"filler {i}")
        driver = r'''try {
  await until(() => window.__appReady && q("#sessYouRow .srow"), "Sessions, the first page");
  const box = (s) => q(s).getBoundingClientRect(), shown = (s) => box(s).height > 0;
  const header = q("header"), out = {};
  out.bar = Math.round(box("header").height);
  out.parts = ["header h1.brand .mark", "#tabBtn", "#searchBtn", "#hdrMore"].every(shown) && !shown("#search") && !shown("nav")
    && !shown("header h1.brand .logo-light") && !shown("header h1.brand .logo-dark");
  out.name = q("#tabName").textContent;
  q("#tabBtn").click();
  out.sheet = [...document.querySelectorAll("nav button")].filter(b => b.getBoundingClientRect().height >= 48).length;
  out.scrim = shown("#hdrScrim");
  q('nav button[data-tab="plan"]').click();
  out.picked = q("#tabName").textContent + " " + shown("nav") + " " + shown("#hdrScrim") + " " + !q("#tab-plan").classList.contains("hidden");
  q("#hdrMore").click();
  out.menu = shown("#hdrMenu") && shown("#settingsBtn") && shown("#updateBtn") && shown("#theme");
  q("#hdrScrim").click();
  out.closed = !shown("#hdrMenu");
  q("#hdrMore").click(); q("#settingsBtn").click();  // Settings opens from the ⋯ (#5116)
  out.settings = q("#tabName").textContent + " " + !q("#tab-settings").classList.contains("hidden") + " " + shown("#hdrMenu");
  q("#searchBtn").click();
  out.search = !q("#searchDlg").classList.contains("hidden") && document.activeElement === q("#searchText");
  q("#searchClose").click();
  q('nav button[data-tab="plan"]').click();
  // Room to scroll whatever Plan shows.
  document.body.style.minHeight = "3000px";
  await sleep(100);
  for (const y of [100, 200, 300, 400]) { scrollTo(0, y); await sleep(60); }
  await sleep(100);
  out.down = header.classList.contains("hdr-hide") && box("header").bottom <= 0;
  for (const y of [380, 360, 340]) { scrollTo(0, y); await sleep(60); }
  await sleep(100);
  out.up = !header.classList.contains("hdr-hide");
  scrollTo(0, 400); await sleep(100); scrollTo(0, 500); await sleep(100);
  q("#hdrMore").click(); scrollTo(0, 700); await sleep(150);
  out.menuKeeps = !header.classList.contains("hdr-hide");
  q("#hdrMore").click(); scrollTo(0, 0);
  q('nav button[data-tab="sessions"]').click();
  if (!q("#sessYou").classList.contains("hidden")) q("#youBack").click();  // the first page showed You (#5102)
  await until(() => q('#sessions .srow[data-session="ag"] .srow-main'), "the row of ag");
  q('#sessions .srow[data-session="ag"] .srow-main').click();
  await until(() => q("#sessTitle").textContent === "ag" && q("#sessLayout").classList.contains("phone-chat"), "the chat of ag");
  out.head = Math.round(box(".sess-head").height) <= 48 && !shown("#sessMenu") && !shown("#sessItem") && shown("#sessMore")
    && !shown('.sess-lists [data-sp-open="items"]') && box(".sess-head h2").width > 200;
  q("#sessMore").click();
  out.more = shown("#sessMenu") && shown("#sessOpen") && q("#sessMore").getAttribute("aria-expanded") === "true"
    && shown('#sessMenu [data-sp-open="items"]') && shown('#sessMenu [data-sp-open="artifacts"]')
    && Math.round(box(".sess-head").height) <= 48;
  q("#sessBody").dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));  // a tap outside the menu
  await sleep(50);
  out.moreCloses = !q("#sessMain").classList.contains("head-more");
  post(out);
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        got = self.drive(driver, "/", size="390,844")
        self.assertEqual(got, {"bar": 48, "parts": True, "name": "Sessions", "sheet": 3, "scrim": True,
                               "picked": "Plan false false true", "menu": True, "closed": True,
                               "settings": "Settings true false", "search": True,
                               "down": True, "up": True, "menuKeeps": True, "head": True, "more": True,
                               "moreCloses": True})

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
    await until(() => !q("#tab-plan").classList.contains("hidden") && !q("#plan-graph").classList.contains("hidden"), "the path /plan/graph shows Plan > Graph");
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
    history.back();  // to /sessions/ag (before the artifact? replaced) and then /plan/graph
    await until(() => !q("#plan-graph").classList.contains("hidden") && where() === "/plan/graph", "Back to the Graph: " + where());
    check(q('nav button[data-tab="plan"]').classList.contains("on") && q('[data-plan-view="graph"]').classList.contains("on"), "Plan and Graph are on");
    sessionStorage.setItem("step", "link");
    location.href = "/artifacts/" + id + "?as=mark";
  } else if (step === "link") {
    await until(() => where() === "/sessions/ag/artifacts/" + id + "?as=mark", "an artifact link opens it beside its session: " + where());
    // The picker fills from the state, which can come after the address is shown (#5276).
    await until(() => q("#actor").value === "mark", "?as= picks the person: " + q("#actor").value);
    post({ ok: true });
  }
} catch (e) { post({ error: step + ": " + String(e.stack || e) }); }
'''.replace("TARGET", json.dumps(target)).replace("ITEM", str(self.it))
        self.assertEqual(self.drive(driver, "/plan/graph"), {"ok": True})


class PageAddresses(unittest.TestCase):
    """#4653: maxpm serve answers the page for the path of each view, with a <base> back to the page's root."""

    def test_each_view_path_gets_the_page_with_its_base(self):
        import re
        from urllib.parse import urljoin
        for path, base in [("/", "./"), ("/sessions", "./"), ("/sessions/", "../"), ("/sessions/ag", "../"),
                           ("/sessions/ag/artifacts/k3x9", "../../../"), ("/artifacts/k3x9", "../"), ("/plan/graph", "../"),
                           ("/you", "./")]:
            page = server.page_html(path)
            self.assertIsNotNone(page, path)
            got = re.search(rb'<base href="([^"]*)">', page).group(1).decode()
            self.assertEqual(got, base, path)
            # On the relay's tunnel the page lives at the root, as here.
            self.assertEqual(urljoin("https://mark-ab3cd9.relay.example" + path, got), "https://mark-ab3cd9.relay.example/", path)
            self.assertLess(page.index(b"<base"), page.index(b"app.css"), "the base comes before the first address")
        for path in ("/api/state", "/app.js", "/components/chat.js", "/items/3", "/sessions//x", "/sessions/../api"):
            self.assertIsNone(server.page_html(path), path)
        # The tabs before Sessions and Plan (#5102 step 7): nothing forwards; each answers "not found", with a link to /.
        for path in ("/board", "/projects", "/targets", "/capacity", "/graph", "/done", "/graph/x"):
            self.assertIsNone(server.page_html(path), path)
            h, out = server.Handler.__new__(server.Handler), {}
            h.path, h.client_address, h.headers = path, ("127.0.0.1", 5555), {"Host": "127.0.0.1:8765"}
            h._send = lambda code, body, ctype=None: out.update(code=code, body=body)
            h.do_GET()
            self.assertEqual(out["code"], 404, path)
            self.assertIn(b"<a href='/'>Open MaximizePM</a>", out["body"], path)

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
