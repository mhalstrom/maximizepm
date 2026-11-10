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

    def drive(self, driver, path="/", steps=None, size=None):
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
            *([f"--window-size={size}"] if size else []),
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
    q(`nav button[data-tab="${name}"]`).click();
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
  q('nav button[data-tab="settings"]').click();
  await until(() => q("#settingsTable").textContent.includes("lease_ttl"), "the settings");
  check(q("#people").textContent.includes("mark"), "Settings > People");
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

    def test_a_hover_moves_nothing_on_any_tab_at_desktop_width(self):
        """#5106 (mark: 'the pin button when you mouse over it changes to the turn off button. this layout shift is
        really annoying'): on every tab at 1240 px, a hover on any row, button, chip or element with a :hover rule
        moves no other element and hides none."""
        self.sweep_hovers("1240,900")

    def test_a_hover_moves_nothing_on_any_tab_at_phone_width(self):
        """#5106: the same sweep at 390 px."""
        self.sweep_hovers("390,844")

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
  q('#sessions .srow[data-session="ag"] .srow-main').click();
  await until(() => location.pathname === "/sessions/ag" && shown(q("#sessMain")), "the chat of ag");
  out.chat = !shown(q("#sessYou")) && !q("#sessYouRow .srow").classList.contains("on");
  history.back();
  await until(() => location.pathname === "/you" && shown(q("#sessYou")), "Back shows You again");
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
                                                           "tabs": ["sessions", "plan", "terminal", "settings"]})

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
        tabs, the magnifier, which opens the search, and the ⋯ menu. The bar hides while the page scrolls down and
        comes back on a scroll up; the session head is one line, and its ⋯ shows Settings, Open session, Items and
        Artifacts."""
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
  out.menu = shown("#hdrMenu") && shown("#updateBtn") && shown("#theme");
  q("#hdrScrim").click();
  out.closed = !shown("#hdrMenu");
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
  out.head = Math.round(box(".sess-head").height) <= 48 && !shown("#sessOpen") && !shown(".sess-item") && shown("#sessMore");
  q("#sessMore").click();
  out.more = shown("#sessOpen") && shown(".sess-item") && q("#sessMore").getAttribute("aria-expanded") === "true";
  q('.sess-item [data-sp-open="items"]').click();
  await sleep(50);
  out.moreCloses = !q("#sessMain").classList.contains("head-more");
  post(out);
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        got = self.drive(driver, "/", size="390,844")
        self.assertEqual(got, {"bar": 48, "parts": True, "name": "Sessions", "sheet": 4, "scrim": True,
                               "picked": "Plan false false true", "menu": True, "closed": True, "search": True,
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
    check(q("#actor").value === "mark", "?as= picks the person");
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
                           ("/sessions/ag/artifacts/k3x9", "../../../"), ("/graph", "./"), ("/artifacts/k3x9", "../"), ("/plan/graph", "../"),
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
