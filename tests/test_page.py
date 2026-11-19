"""The page as a whole in a browser (#4652, #4653, #4656): its addresses, every view, the side panel's tabs."""
import json
import os
import tempfile
import unittest
from pathlib import Path

import struct

from river import core, server
from tests import timing
from tests.test_view_sessions import TmuxBase, claude_line


def gray_jpeg(w):
    """A baseline gray JPEG of w x 8 px (w a multiple of 8): each block has DC 0 and an end of block, with one-code
    Huffman tables. The tests need a JPEG that Chrome decodes, and the standard library makes none."""
    seg = lambda m, body: b"\xff" + bytes([m]) + struct.pack(">H", len(body) + 2) + body  # noqa: E731
    one = bytes([1] + [0] * 15) + b"\x00"
    bits = "00" * (w // 8)
    bits += "1" * (-len(bits) % 8)
    return (b"\xff\xd8" + seg(0xDB, b"\x00" + b"\x01" * 64)
            + seg(0xC0, b"\x08" + struct.pack(">HH", 8, w) + b"\x01\x01\x11\x00")
            + seg(0xC4, b"\x00" + one) + seg(0xC4, b"\x10" + one) + seg(0xDA, b"\x01\x01\x00\x00\x3f\x00")
            + bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)) + b"\xff\xd9")


class PageBrowser(TmuxBase):
    """The real page (app.js) in Chrome, beside the running session ag."""

    HEAD = r'''
const q = s => document.querySelector(s);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const until = async (f, what) => { for (let i = 0; i < UNTIL_TRIES; i++) { if (f()) return; await sleep(100); } throw new Error(what); };
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
                    # until() waits 15 s on a fast machine; on CI it scales (#5705).
                    head = f"const UNTIL_TRIES = {int(timing.limit(150))};" + PageBrowser.HEAD
                    return self._send(200, (head + driver).encode(), "text/javascript; charset=utf-8")
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
            # The driver's clicks are no person's: no autoplay rule may hold an AudioContext (the microphone tests).
            "--autoplay-policy=no-user-gesture-required",
            *([f"--window-size={window}"] if window else []), *flags,
            f"http://127.0.0.1:{httpd.server_address[1]}{'/__frame' if frame else path}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            ran = got.wait(timing.limit(40))  # a slow GitHub macOS runner took longer (#5705)
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

    def test_the_item_drawer_fits_a_390_px_screen(self):
        """#5701: at 390 px (a real phone frame) no part of the item drawer, its buttons, its fields and a long
        message text, reaches past the right edge of the screen."""
        item = core.item_add(self.c, "site", "logo")["id"]
        core.send(self.c, "note", "x" * 300 + " " + "/very/long/path/" * 20, item=item, actor="ag")
        driver = r'''try {
  await until(() => window.__appReady, "app.js loaded");
  history.pushState({}, "", "/?item=''' + str(item) + r'''");
  window.dispatchEvent(new PopStateEvent("popstate"));
  await until(() => q("#drawer").classList.contains("open") && q("#drawer [data-do=save]") && q("#drawer .ev .body"), "the drawer with its message");
  await new Promise(r => setTimeout(r, 400));
  const d = q("#drawer");
  const over = [...d.querySelectorAll("*")].filter(e => e.getBoundingClientRect().width && e.getBoundingClientRect().right > innerWidth + 0.5)
    .map(e => e.tagName + "." + e.className + "#" + e.id + " " + Math.round(e.getBoundingClientRect().right));
  post({ w: innerWidth, scroll: d.scrollWidth, client: d.clientWidth, over: over.slice(0, 8) });
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        r = self.drive(driver, "/", size="390,844")
        self.assertEqual(r["w"], 390, r)
        self.assertEqual(r["over"], [], r)
        self.assertLessEqual(r["scroll"], r["client"], r)

    def test_deploy_runners_fits_a_390_px_screen(self):
        """#6171: Deploy > Runners at 390 px (a real phone frame): the bar with Add a runner, a runner with a long
        SSH target, This computer, the agents on runners with a long result, and the command block stay inside the
        screen; only the command block scrolls sideways, in its own box. (A screenshot of headless Chrome with
        --window-size=390 lays the page out at 500 px and cuts it: that is no phone, see #5415.)"""
        core.register(self.c, "mark", human=True)
        item = core.item_add(self.c, "site", "Fix the failing build step of the release pipeline")["id"]
        t = core.iso(core.now())
        with core.tx(self.c):
            self.c.execute("INSERT INTO runners(name, ssh, added_by, added_at, system, version, tailnet, clis, cli, "
                           "state, checked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           ("ci-build-server-1", "ci-runner@ci-build-server-1.tailnet-example.ts.net", "mark", t,
                            "Linux x86_64", "maxpm 0.9.0", "100.101.102.103",
                            json.dumps({"claude": "2.1.40 (Claude Code)", "codex": "codex-cli 0.70.0"}), "claude", "ok", t))
            for state, result in (("running", None), ("failed", "the tests failed twice: " + "/a-long-folder-name" * 8)):
                self.c.execute("INSERT INTO runner_runs(runner, item, agent, pipeline, rule, state, cli, queued_at, "
                               "started_at, result, cost) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                               ("ci-build-server-1", item, "runner-agent-1", "release-pipeline", "on-stage-failed",
                                state, "claude", t, t, result, 1.25))
        driver = r'''try {
  await until(() => window.__appReady, "app.js loaded");
  q('nav button[data-tab="deploy"]').click();
  await until(() => q('[data-dp-part="runners"]'), "Deploy and its parts");
  q('[data-dp-part="runners"]').click();
  await until(() => q("#dpPart .rn-cmds") && q("#dpPart [data-rn-stop]"), "Deploy > Runners with its runs");
  await new Promise(r => setTimeout(r, 300));
  const cmds = q("#dpPart .rn-cmds");
  const over = [...q("#dpPart").querySelectorAll("*")].filter(e => !cmds.contains(e) || e === cmds)
    .filter(e => e.getBoundingClientRect().width && e.getBoundingClientRect().right > innerWidth + 0.5)
    .map(e => e.tagName + "." + e.className + " " + Math.round(e.getBoundingClientRect().right));
  post({ w: innerWidth, over: over.slice(0, 8), page: document.documentElement.scrollWidth <= innerWidth,
    add: q('#dpPart [data-rn-add]').getBoundingClientRect().right <= innerWidth,
    cmdsScroll: cmds.scrollWidth > cmds.clientWidth });
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        r = self.drive(driver, "/", size="390,844")
        self.assertEqual(r["w"], 390, r)
        self.assertEqual(r["over"], [], r)
        self.assertTrue(r["page"] and r["add"], r)
        self.assertTrue(r["cmdsScroll"], r)  # the long command lines scroll in their box, not the page

    def test_the_arrows_of_a_pipeline_show_their_transitions_and_open_their_controls(self):
        """#6139: each arrow between two stages shows its mode or the control that holds it, who set it and the
        version that waits there; a click opens its controls with their buttons. At 1280 and at 390 px."""
        from river.pipeline import define, runner, transitions
        define.pipeline_add(self.c, "app", project="site")
        define.env_set(self.c, "app", "prod")
        define.stage_set(self.c, "app", "build", "build")
        define.stage_set(self.c, "app", "test", "test")
        define.stage_set(self.c, "app", "beta", "test")
        define.stage_set(self.c, "app", "prod", "deploy", env="prod", gate="yes mark", tier="prod")
        transitions.block(self.c, "app", "test", "the test database is down", until="2h", actor="mark")
        t = core.now()
        transitions.change(self.c, "app", "beta", window=f"{t + core.timedelta(hours=2):%H:%M}-"
                           f"{t + core.timedelta(hours=3):%H:%M} UTC", actor="mark")
        runner._queue(self.c, "app", "test", "abc1234")
        driver = r'''
try {
  await until(() => window.__appReady, "the page");
  q('nav button[data-tab="deploy"]').click();
  await until(() => q('#dpList [data-dp-open="app"]'), "Deploy: the pipeline app");
  check(q('#dpList').textContent.includes("⛔"), "the list marks the blocked transition");
  q('#dpList [data-dp-open="app"]').click();
  await until(() => q('#plRow [data-pl-arrow="prod"]'), "the arrows of app");
  const arrow = n => q(`#plRow [data-pl-arrow="${n}"]`).textContent.replace(/\s+/g, " ").trim();
  const out = { test: arrow("test"), beta: arrow("beta"), prod: arrow("prod"),
    // The first stage (the Holding stage since #6140) has no arrow: one arrow fewer than the stages.
    first: document.querySelectorAll('#plRow [data-pl-arrow]').length !== document.querySelectorAll('#plRow [data-pl-stage]').length - 1,
    tier: q('#plRow [data-pl-stage="prod"] .pl-tier') && q('#plRow [data-pl-stage="prod"] .pl-tier').textContent };  // #6137
  q('#plRow [data-pl-arrow="test"]').click();  // #6189: the transition opens in a dialog
  await until(() => q("#plArrowDlg:not(.hidden)") && q("#plArrowDlg").textContent.includes("build → test"), "the controls of build → test");
  out.detail = q("#plArrowDlg").textContent.replace(/\s+/g, " ");
  out.buttons = [...q("#plArrowDlg").querySelectorAll("button")].map(b => b.textContent.trim());
  out.over = [...q("#plArrowDlg").querySelectorAll("*")].filter(e => e.getBoundingClientRect().right > innerWidth + 0.5
    && !e.closest(".pl-table")).map(e => e.tagName + "." + e.className).slice(0, 5);
  q('#plArrowDlg [data-pl-arrowclose]').click();
  await until(() => q("#plArrowDlg.hidden"), "the dialog closes");
  // #6141: a stage shows Versions in <stage>, each run with its buttons.
  q('#plRow [data-pl-stage="test"]').click();
  await until(() => q("#plStageRuns").textContent.includes("Versions in test"), "Versions in test");
  out.runs = q("#plStageRuns").textContent.replace(/\s+/g, " ");
  out.stop = !!q('#plStageRuns [data-pl-stop]');
  post(out);
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        for size in ("1280,900", "390,844"):
            r = self.drive(driver, "/", size=size)
            self.assertIn("⛔ blocked", r["test"], r)
            self.assertIn("abc1234 waits", r["test"], r)
            self.assertIn("🕒 closed", r["beta"], r)
            self.assertIn("✋ mark's yes", r["prod"], r)
            self.assertFalse(r["first"], r)  # the first stage's arrow is plain: not drawn
            self.assertEqual(r["tier"], "PROD", r)  # the tier badge on the stage card (#6137)
            self.assertIn("the test database is down", r["detail"], r)
            self.assertIn("build → test", r["detail"], r)  # #6198: the title names the transition
            self.assertNotIn("Set by, until", r["detail"], r)  # no control table in the dialog
            for b in ("Unblock", "Delay", "Disable", "Let abc1234 pass", "Freeze", "Window"):
                self.assertIn(b, r["buttons"], r)
            self.assertEqual(r["over"], [], r)
            self.assertIn("abc1234", r["runs"], r)
            self.assertIn("waiting", r["runs"], r)  # its state, not 'ran' (#6182)
            self.assertTrue(r["stop"], r)  # the waiting run can be stopped

    def test_the_watch_of_a_release_has_one_row_for_each_check(self):
        """#6181: the watch of toolscaled-prod's release listed every check run in a narrow column, a page over
        30000 px tall. Now one row for each check, its name on one line; a click opens its runs. At 1500 and 390 px."""
        core.target_add(self.c, "web-prod", "deploys web", None)
        core.project_add(self.c, "web", target="web-prod")
        i = core.item_add(self.c, "web", "A change", actor="mark")["id"]
        core.done(self.c, i, output="done", actor="mark", ship_it=True)
        dep = self.c.execute("SELECT MAX(id) FROM items WHERE kind='deploy'").fetchone()[0]
        core.target_cut(self.c, "web-prod", rev="abc1234", actor=None)
        start = core.now() - core.timedelta(hours=5)
        self.c.execute("UPDATE items SET status='done', closed_at=? WHERE id=?", (core.iso(start), dep))
        for name in ("host-check-edge-cell-long-name", "backend-errors", "mcp-endpoint"):
            cid = self.c.execute(
                "INSERT INTO checks(scope_kind, scope, name, form, config, every, fail_after, recover_after, timeout, "
                "severity, created_at) VALUES ('target','web-prod',?,'http','{}','1m',1,1,'10s','item',?)",
                (name, core.iso(start))).lastrowid
            for n in range(240):
                at = core.iso(start + core.timedelta(minutes=n + 1))
                bad = name == "backend-errors" and n in (100, 101)
                self.c.execute("INSERT INTO ops_runs(check_id, key, started_at, ended_at, status, summary) VALUES "
                               "(?, 'k', ?, ?, ?, ?)", (cid, at, at, "critical" if bad else "ok",
                                                         "3 errors in the backend log" if bad else "fine"))
        self.c.commit()
        driver = r'''
try {
  await until(() => window.__appReady, "the page");
  q('nav button[data-tab="deploy"]').click();
  await until(() => q('#dpList [data-dp-open="web-prod"]'), "Deploy: the target web-prod");
  q('#dpList [data-dp-open="web-prod"]').click();
  await until(() => q('#plRow [data-pl-stage="watch"]'), "the stages of web-prod");
  q('#plRow [data-pl-stage="watch"]').click();
  await until(() => q("#plDetail .pl-checks"), "the checks of the watch");
  const rows = [...q("#plDetail").querySelectorAll("tr[data-pl-check]")];
  const name = r => r.querySelector(".pl-cname .title");
  const out = { rows: rows.length, names: rows.map(r => name(r).textContent.trim()),
    oneLine: rows.every(r => name(r).getClientRects().length === 1),
    card: q('#plRow [data-pl-stage="watch"]').textContent.replace(/\s+/g, " "),
    height: q("#plDetail").getBoundingClientRect().height, text: q("#plDetail").textContent.replace(/\s+/g, " "),
    dots: q('#plRow [data-pl-stage="watch"]').querySelectorAll(".pl-dot").length,
    over: [...q("#plDetail").querySelectorAll("*")].filter(e => e.getBoundingClientRect().right > innerWidth + 0.5)
      .map(e => e.tagName + "." + e.className).slice(0, 5) };
  rows[0].click();
  await until(() => q("#plDetail .pl-runlist"), "the runs of the first check");
  out.runs = q("#plDetail .pl-runlist").children.length;
  out.overOpen = [...q("#plDetail").querySelectorAll("*")].filter(e => e.getBoundingClientRect().right > innerWidth + 0.5)
    .map(e => e.tagName + "." + e.className).slice(0, 5);
  out.openHeight = q("#plDetail").getBoundingClientRect().height;
  q("#plDetail tr[data-pl-check]").click();
  await until(() => !q("#plDetail .pl-runlist"), "the runs close");
  post(out);
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        for size in ("1500,900", "390,844"):
            r = self.drive(driver, "/", size=size)
            self.assertEqual(r["rows"], 3, r)
            self.assertEqual(r["names"][0], "web-prod/backend-errors", r)  # the check that failed first
            self.assertIn("2 failed check run(s): web-prod/backend-errors (passes now)", r["text"], r)
            self.assertIn("3 errors in the backend log", r["text"], r)
            self.assertIn("240 runs", r["text"], r)
            self.assertIn("ends in 19h", r["text"], r)  # the watch failed before its 24 hours ended
            self.assertIn("failed, ends in 19h", r["card"], r)
            self.assertNotIn("s ago", r["text"].split("2 failed")[0], r)
            self.assertEqual(r["dots"], 3, r)  # one dot for each check on the stage card
            self.assertLess(r["height"], 1200, r)
            self.assertEqual(r["over"], [], r)
            self.assertEqual(r["overOpen"], [], r)
            self.assertEqual(r["runs"], 240, r)
            self.assertLess(r["openHeight"], 1600, r)  # the runs scroll in their own box
            if size.startswith("1500"):
                self.assertTrue(r["oneLine"], r)

    def test_the_stages_of_a_target_line_up_and_its_tables_are_short(self):
        """#6182 (mark 2026-11-19, the review and approval stages of toolscaled-prod): the stage cards line up (a long
        head is one line), the hold button is not inside the sentence, Versions in <stage> shows the newest 5 rows with
        Show all, each row's state (no 'ran'), live on the release in production, and the target card under the tables
        does not repeat the report's tasks. At 1500 and 390 px."""
        core.target_add(self.c, "toolscaled-prod", "deploys the app", None)
        core.project_add(self.c, "web", target="toolscaled-prod")
        core.config_set(self.c, "release_approval", "person", project="web")
        for n in range(8):
            i = core.item_add(self.c, "web", f"Change {n}", actor="mark")["id"]
            core.done(self.c, i, output="done", actor="mark", ship_it=True)
            dep = self.c.execute("SELECT MAX(id) FROM items WHERE kind='deploy'").fetchone()[0]
            core.target_cut(self.c, "toolscaled-prod", rev=f"{n:07x}abcdef", actor=None)
            if n < 7:
                self.c.execute("UPDATE items SET status='done', closed_at=? WHERE id=?",
                               (core.iso(core.now() - core.timedelta(hours=8 - n)), dep))
        self.c.commit()
        driver = r'''
try {
  await until(() => window.__appReady, "the page");
  q('nav button[data-tab="deploy"]').click();
  await until(() => q('#dpList [data-dp-open="toolscaled-prod"]'), "Deploy: the target");
  q('#dpList [data-dp-open="toolscaled-prod"]').click();
  await until(() => q('#plRow [data-pl-stage="deploy"]'), "the stages");
  const tops = [...document.querySelectorAll('#plRow .pl-stage .pl-name')].map(e => Math.round(e.getBoundingClientRect().top));
  const kind = q('#plRow [data-pl-stage="deploy"] .pl-kind');
  const out = { tops, kindLines: kind.getClientRects().length, kindH: kind.getBoundingClientRect().height,
    kindTitle: kind.title, holdInText: !!q('#plNote .pl-bar > span:first-child button'), hold: !!q('#plNote .pl-bar > button'),
    holdFont: getComputedStyle(q('#plRow [data-pl-stage="holding"] .pl-ver')).fontFamily };
  q('#plRow [data-pl-stage="deploy"]').click();
  await until(() => q("#plStageRuns .pl-runs"), "Versions in deploy");
  const rows = () => [...q("#plStageRuns").querySelectorAll(".pl-runs tr:not(.pl-th)")];
  out.rows = rows().length;
  out.head = [...q("#plStageRuns").querySelectorAll(".pl-runs th")].map(e => e.textContent.trim());
  out.text = q("#plStageRuns").textContent.replace(/\s+/g, " ");
  out.live = rows().filter(r => r.classList.contains("live")).map(r => r.querySelector(".mono").textContent.trim());
  // A box that scrolls sideways on purpose (the tables, the stage row, the tabs since #6188) may hold wider parts.
  const scrolls = e => { for (let p = e.parentElement; p; p = p.parentElement)
    if (["auto", "scroll"].includes(getComputedStyle(p).overflowX)) return true; return false; };
  out.over = [...document.querySelectorAll('#deploy-pipeline *')].filter(e => e.getBoundingClientRect().right > innerWidth + 0.5
    && !scrolls(e)).map(e => e.tagName + "." + e.className).slice(0, 5);
  q('#plStageRuns [data-pl-allruns]').click();
  await until(() => rows().length > 5, "Show all");
  out.all = rows().length;
  out.tasks = !!document.querySelector(".rel-tasks");
  // #6189: the chip after holding opens the release's dialog: what it does now, then what it can do.
  q('#plRow [data-pl-arrow]').click();
  await until(() => q("#plArrowDlg:not(.hidden)"), "the release dialog");
  out.dlg = q("#plArrowDlg").textContent.replace(/\s+/g, " ");
  out.dlgOver = [...q("#plArrowDlg").querySelectorAll("*")].filter(e => e.getBoundingClientRect().right > innerWidth + 0.5
    && !scrolls(e)).map(e => e.tagName + "." + e.className).slice(0, 5);
  post(out);
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        for size in ("1500,900", "390,844"):
            r = self.drive(driver, "/", size=size)
            # Every card's name on one line across the row (the deploy card's border is 2 px: pl-env).
            self.assertLessEqual(max(r["tops"]) - min(r["tops"]), 2, r)
            self.assertLess(r["kindH"], 20, r)  # the deploy card's head is one line
            self.assertEqual(r["kindTitle"], "deploy · toolscaled-prod", r)
            self.assertFalse(r["holdInText"], r)
            self.assertTrue(r["hold"], r)
            self.assertNotIn("monospace", r["holdFont"].lower(), r)  # 'nothing waits' is words
            self.assertEqual(r["rows"], 5, r)
            self.assertEqual(r["head"], ["When", "Version", "State", "Took", "Task", "Actions"], r)
            self.assertNotIn(" ran ", r["text"], r)
            self.assertNotIn("the release of deploy", r["text"], r)
            self.assertIn("Show all 8", r["text"], r)
            self.assertEqual(r["live"], ["0000006 live"], r)  # the newest done deploy, not the newest row
            self.assertEqual(r["all"], 8, r)
            self.assertFalse(r["tasks"], r)
            self.assertEqual(r["over"], [], r)
            self.assertIn("Release toolscaled-prod", r["dlg"], r)  # #6198: the title names the action
            self.assertIn("Block", r["dlg"], r)
            self.assertNotIn("Set by, until", r["dlg"], r)  # no control table, no paragraphs
            self.assertNotIn("The mode comes from", r["dlg"], r)
            self.assertEqual(r["dlgOver"], [], r)

    def test_the_next_step_waits_on_the_graph_and_approves_in_place(self):
        """#6193 (mark 2026-11-19: 'it has to be obvious'): a release that waits for the person's yes shows under the
        graph as the next step, its approval card highlighted, with 'Approve and release #N'; one confirm approves the
        person's approval item as them. Deploy now does not show while the yes waits. At 1920 and 390 px."""
        core.target_add(self.c, "toolscaled-prod", "deploys the app", None)
        core.project_add(self.c, "web", target="toolscaled-prod")
        core.config_set(self.c, "release_approval", "person", project="web")
        i = core.item_add(self.c, "web", "A change", actor="mark")["id"]
        core.done(self.c, i, output="done", actor="mark", ship_it=True)
        dep = self.c.execute("SELECT MAX(id) FROM items WHERE kind='deploy'").fetchone()[0]
        core.target_cut(self.c, "toolscaled-prod", rev="9783eb1abcdef", actor=None)
        core.release_yes_pass(self.c)
        appr = core._approvals(self.c, dep)[0]["id"]
        self.c.commit()
        driver = r'''
try {
  await until(() => window.__appReady, "the page");
  q("#actor").value = "mark";
  q('nav button[data-tab="deploy"]').click();
  await until(() => q('#dpList [data-dp-open="toolscaled-prod"]'), "Deploy: the target");
  q('#dpList [data-dp-open="toolscaled-prod"]').click();
  await until(() => q('.pl-next [data-pl-yes]'), "the next step");
  const b = q('.pl-next [data-pl-yes]').getBoundingClientRect();
  const out = { next: q(".pl-next").textContent.replace(/\s+/g, " "), waits: [...document.querySelectorAll(".pl-waits")].map(e => e.dataset.plStage || e.dataset.plArrow),
    inView: b.bottom <= innerHeight && b.right <= innerWidth, deploy: !!q('.pl-next [data-deploy]') };
  q('.pl-next [data-pl-yes]').click();
  await until(() => q('#plAskDlg:not(.hidden) [data-pl-askgo]'), "the confirm");
  out.confirm = q("#plAskDlg").textContent.replace(/\s+/g, " ");
  q('#plAskDlg [data-pl-askgo]').click();
  await until(() => !q('.pl-next [data-pl-yes]'), "the yes is given");
  post(out);
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        for size in ("1920,1080", "390,844"):
            r = self.drive(driver, "/", size=size)
            self.assertIn(f"Next: your yes for release #{dep}", r["next"], r)
            self.assertIn(f"Approve and release #{dep}", r["next"], r)
            self.assertEqual(r["waits"], ["approval"], r)
            self.assertTrue(r["inView"], r)  # no scroll
            self.assertFalse(r["deploy"], r)  # never around the yes
            self.assertIn(f"Approve release #{dep}?", r["confirm"], r)
            st = self.c.execute("SELECT status, assignee FROM items WHERE id=?", (appr,)).fetchone()
            self.assertEqual(st["status"], "done", r)
            with core.tx(self.c):  # open it again for the next size
                self.c.execute("UPDATE items SET status='open', closed_at=NULL WHERE id=?", (appr,))

    def test_a_running_release_is_the_next_bar_and_the_page_says_little(self):
        """#6209 (mark 2026-11-19 on maximizepm-github while its release ran: 'fix the wordy text'): the Next bar names
        the running release, its step and its time with a log link, and offers nothing that starts another release;
        no count of stages, no paragraph under Versions in deploy, the stage's links are plain, and the holding card's
        line is short. At 1500 and 390 px."""
        core.target_add(self.c, "toolscaled-prod", "deploys the app", None)
        core.project_add(self.c, "web", target="toolscaled-prod")
        for n in range(2):
            i = core.item_add(self.c, "web", f"Change {n}", actor="mark")["id"]
            core.done(self.c, i, output="done", actor="mark", ship_it=True)
        dep = self.c.execute("SELECT MIN(id) FROM items WHERE kind='deploy'").fetchone()[0]
        core.target_cut(self.c, "toolscaled-prod", rev="9783eb1abcdef", actor=None)
        t0 = core.iso(core.now() - core.timedelta(minutes=2))
        h = self.c.execute("INSERT INTO hook_runs(target, event, command, deploy_id, item_id, rev, queued_at, started_at) "
                           "VALUES ('toolscaled-prod','deploy','make it',?,?,'9783eb1abcdef',?,?)", (dep, dep, t0, t0)).lastrowid
        self.c.execute("INSERT INTO hook_steps(hook_run, name, state, started_at, ended_at, seconds) "
                       "VALUES (?,'check','passed',?,?,1)", (h, t0, t0))
        self.c.execute("INSERT INTO hook_steps(hook_run, name, state, started_at) VALUES (?,'test','running',?)", (h, t0))
        self.c.commit()
        driver = r'''
try {
  await until(() => window.__appReady, "the page");
  q("#actor").value = "mark";
  q('nav button[data-tab="deploy"]').click();
  await until(() => q('#dpList [data-dp-open="toolscaled-prod"]'), "Deploy: the target");
  q('#dpList [data-dp-open="toolscaled-prod"]').click();
  await until(() => q(".pl-next"), "the Next bar");
  const out = { next: q(".pl-next").textContent.replace(/\s+/g, " ").trim(), buttons: q(".pl-next").querySelectorAll("button").length,
    note: q("#plNote").textContent.replace(/\s+/g, " ").trim() };
  q('.pl-next [data-pl-stage="deploy"]').click();
  await until(() => q("#plStageRuns .pl-runs"), "Versions in deploy");
  out.runs = q("#plStageRuns").textContent.replace(/\s+/g, " ");
  out.links = [...q("#plDetail").querySelectorAll(".pl-opts button")].map(b => [b.textContent.trim(), b.className]);
  out.page = q("#deploy-pipeline").textContent.replace(/\s+/g, " ");
  post(out);
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        for size in ("1500,900", "390,844"):
            r = self.drive(driver, "/", size=size)
            self.assertIn("Releasing 9783eb1 · step test · 2m · log · report", r["next"], r)
            self.assertEqual(r["buttons"], 0, r)  # nothing that starts another release
            self.assertNotIn("stage", r["note"].replace("Hold all stages", ""), r)
            self.assertNotIn("Each row is a release", r["runs"], r)
            self.assertEqual([x[0] for x in r["links"]], ["The transition before it", "Send a version here…"], r)
            self.assertTrue(all("plain" in x[1] and "warn" not in x[1] for x in r["links"]), r)
            for words in ("starts an agent session", "as a pipeline", "Deploy now"):
                self.assertNotIn(words, r["page"], r)

    def test_the_window_of_a_transition_is_marked_on_a_week_calendar(self):
        """#6205 (mark 2026-11-19: 'probably a better way to do this is with a calander'): the Window of a transition
        is a week of hour cells; a preset or a drag marks hours and writes the window text, a typed text marks the
        hours, and Save stores a window that the server reads. At 1280 and 390 px."""
        from river.pipeline import define, transitions
        define.pipeline_add(self.c, "app", project="site")
        define.env_set(self.c, "app", "prod")
        define.stage_set(self.c, "app", "build", "build")
        define.stage_set(self.c, "app", "prod", "deploy", env="prod")
        self.c.commit()
        driver = r'''
try {
  await until(() => window.__appReady, "the page");
  q("#actor").value = "mark";
  q('nav button[data-tab="deploy"]').click();
  await until(() => q('#dpList [data-dp-open="app"]'), "Deploy: app");
  q('#dpList [data-dp-open="app"]').click();
  await until(() => q('#plRow [data-pl-arrow="prod"]'), "the arrows");
  q('#plRow [data-pl-arrow="prod"]').click();
  await until(() => q('#plArrowDlg [data-pl-change]'), "the Window option");
  q('#plArrowDlg [data-pl-change]').click();
  await until(() => q('#plAskDlg:not(.hidden) .wk-grid'), "the calendar");
  const f = q("#plF-window"), on = () => document.querySelectorAll("#plAskDlg .wk-cell.on").length;
  const out = { cells: document.querySelectorAll("#plAskDlg .wk-cell").length };
  q('#plAskDlg [data-wk-preset="work"]').click();
  out.preset = f.value; out.presetOn = on();
  f.value = "mon-thu 09:00-17:00 UTC; sat 22:00-02:00 UTC"; f.dispatchEvent(new Event("input"));
  out.typedOn = on();
  const cell = (k) => q(`#plAskDlg [data-wk="${k}"]`).getBoundingClientRect();
  const a = cell("4:9"), b = cell("4:11");  // a drag down friday 09:00-12:00
  const ev = (type, r) => new PointerEvent(type, { bubbles: true, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2, pointerId: 1 });
  q('#plAskDlg [data-wk="4:9"]').dispatchEvent(ev("pointerdown", a));
  q('#plAskDlg .wk').dispatchEvent(ev("pointermove", cell("4:10")));
  q('#plAskDlg .wk').dispatchEvent(ev("pointermove", b));
  window.dispatchEvent(new PointerEvent("pointerup", { bubbles: true }));
  out.dragged = f.value;
  // #6206: the zone field starts on the browser's zone; another zone from the list rewrites the window; 24-hour labels.
  const z = q("#plAskDlg .wk-zone");
  out.zoneStart = z.value; out.browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  out.hours = [...document.querySelectorAll("#plAskDlg .wk-hour")].map(e => e.textContent).filter(Boolean);
  z.value = "Europe/Berlin"; z.dispatchEvent(new Event("change"));
  out.berlin = f.value;
  out.over = [...q("#plAskDlg").querySelectorAll("*")].filter(e => e.getBoundingClientRect().right > innerWidth + 0.5).map(e => e.className).slice(0, 5);
  q('#plAskDlg [data-pl-askgo]').click();
  await until(() => q("#plAskDlg.hidden"), "saved");
  await sleep(500);
  post(out);
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        for size in ("1280,900", "390,844"):
            r = self.drive(driver, "/", size=size)
            self.assertEqual(r["cells"], 7 * 24, r)
            self.assertRegex(r["preset"], r"^mon-fri 09:00-17:00 \S+$", r)
            self.assertEqual(r["presetOn"], 5 * 8, r)
            self.assertEqual(r["typedOn"], 4 * 8 + 2 + 2, r)  # sat 22-24 and sun 00-02
            self.assertIn("fri 09:00-12:00 UTC", r["dragged"], r)
            self.assertEqual(r["zoneStart"], "UTC", r)  # the typed window named UTC
            self.assertIn("21:00", r["hours"], r)  # a 24-hour clock
            self.assertTrue(all("M" not in h for h in r["hours"]), r)
            self.assertIn("fri 09:00-12:00 Europe/Berlin", r["berlin"], r)
            self.assertEqual(r["over"], [], r)
            s = self.c.execute("SELECT id FROM pipeline_stages WHERE name='prod'").fetchone()["id"]
            w = transitions.get(self.c, s)["time_window"]
            self.assertTrue(w and transitions.parse_window(w), w)
            self.assertIn("fri 09:00-12:00 Europe/Berlin", w)  # the picked zone is kept

    def test_the_head_names_the_definition_version_and_versions_go_back(self):
        """#6143: the head of a pipeline's page names its definition version; Versions lists them, shows what one
        changed, and Go back makes one more version. At 1280 and at 390 px, nothing wider than the page."""
        from river.pipeline import define, versions
        core.register(self.c, "mark", human=True)
        define.pipeline_add(self.c, "api", project="site", actor="mark")
        define.stage_set(self.c, "api", "build", "build", run="make", actor="mark")
        define.stage_set(self.c, "api", "build", run="make all", actor="mark")
        driver = r'''
try {
  await until(() => window.__appReady, "the page");
  q('nav button[data-tab="deploy"]').click();
  await until(() => q('#dpList [data-dp-open="api"]'), "Deploy: the pipeline api");
  q('#dpList [data-dp-open="api"]').click();
  await until(() => q('[data-dv-open="api"]'), "the head's Versions");
  const out = { head: q("#dpHead").textContent.replace(/\s+/g, " ") };
  q('[data-dv-open="api"]').click();
  await until(() => q('#dvDlg:not(.hidden) [data-dv-diff="3"]'), "the versions");
  q('#dvDlg [data-dv-diff="3"]').click();
  await until(() => q("#dvDlg .dv-diff"), "what v3 changed");
  out.diff = q("#dvDlg .dv-diff").textContent;
  out.over = [...q("#dvDlg .box").querySelectorAll("*")].filter(e => e.getBoundingClientRect().right > innerWidth + 0.5).length;
  const back = q('#dvDlg [data-dv-back="2"]');
  if (back) {
    back.click();
    await until(() => [...document.querySelectorAll("textarea")].find(t => t.offsetParent), "the reason");
    const t = [...document.querySelectorAll("textarea")].find(t => t.offsetParent);
    t.value = "the old command was right";
    [...document.querySelectorAll("button")].find(b => b.offsetParent && b.textContent.trim() === "Go back").click();
    await until(() => q("#dvDlg").textContent.includes("back to version 2"), "the new version");
  }
  post(out);
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        r = self.drive(driver, "/", size="1280,900")
        self.assertIn("definition v3 · mark", r["head"], r)  # (the override's page test is below, #6146)
        self.assertIn("stage build run: make → make all", r["diff"], r)
        self.assertEqual(r["over"], 0, r)
        self.assertEqual(versions.listing(self.c, "api")["versions"][0]["why"], "back to version 2: the old command was right")
        self.assertEqual(self.c.execute("SELECT run FROM pipeline_stages WHERE name='build' AND pipeline_id=(SELECT id "
                                        "FROM pipelines WHERE name='api')").fetchone()[0], "make")
        r = self.drive(driver, "/", size="390,844")
        self.assertIn("definition v4 · mark", r["head"], r)
        self.assertEqual(r["over"], 0, r)

    def test_a_stage_sends_its_version_here_directly_and_runs_the_whole_pipeline(self):
        """#6146: a stage's 'Send a version here directly…' opens the Emergency tab's override with that stage; it
        shows what it skips; after the override the card says 'not through the pipeline' and the stage offers
        'Run the whole pipeline for <version>'."""
        from river.pipeline import define, runner, operators
        core.register(self.c, "mark", human=True)
        define.pipeline_add(self.c, "app", project="site")
        for s in ("build", "test", "prod"):
            define.stage_set(self.c, "app", s, "test")
        runner._queue(self.c, "app", "build", "abc1234")
        rid = runner.run_again(self.c, "app", "prod", "fix9999", "mark", purpose="override")["run"]
        from river.pipeline import store
        store.run_finish(self.c, rid, "passed")
        driver = r'''
try {
  await until(() => window.__appReady, "the page");
  q('nav button[data-tab="deploy"]').click();
  await until(() => q('#dpList [data-dp-open="app"]'), "Deploy: the pipeline app");
  q('#dpList [data-dp-open="app"]').click();
  await until(() => q('#plRow [data-pl-stage="prod"]'), "the stages of app");
  const out = { card: q('#plRow [data-pl-stage="prod"]').textContent.replace(/\s+/g, " ") };
  q('#plRow [data-pl-stage="prod"]').click();
  await until(() => q('[data-pl-whole]'), "Run the whole pipeline");
  out.whole = q('[data-pl-whole]').textContent.trim();
  q('[data-em-preset="prod"]').click();
  await until(() => q("#emOvStage"), "the override on the Emergency tab");
  out.stage = q("#emOvStage").value;
  out.acts = q(".dp-em-card ul").textContent.replace(/\s+/g, " ");
  out.go = q('[data-em-go="override"]').textContent.trim();
  post(out);
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        r = self.drive(driver, "/", size="1280,900")
        self.assertIn("⚠ not through the pipeline", r["card"], r)
        self.assertEqual(r["whole"], "Run the whole pipeline for fix9999", r)
        self.assertEqual(r["stage"], "prod", r)
        self.assertIn("It skips: build, test", r["acts"], r)
        self.assertTrue(r["go"].startswith("Send ") and r["go"].endswith(" to prod now"), r)

    def test_a_followed_pipeline_shows_read_only_and_offers_no_flow_action(self):
        """#6167: a followed pipeline: the head says 'followed · read only', the arrows are plain lines, a stage has no
        action of our flow, and the Emergency tab offers only Stop its agents and Revoke its keys."""
        from river.pipeline import cdevents, follow, store
        core.register(self.c, "mark", human=True)
        follow.follow(self.c, "partner", actor="mark")
        for i, stage in enumerate(("build", "deploy"), 1):
            store.ingest(self.c, cdevents.make("pipelinerun", "queued", f"j-{i}", {"pipelineName": stage},
                                               {"pipeline": "partner", "version": "ptn0001"}, "jenkins", None))
        driver = r'''
try {
  await until(() => window.__appReady, "the page");
  q('nav button[data-tab="deploy"]').click();
  await until(() => q('#dpList [data-dp-open="partner"]'), "Deploy: the pipeline partner");
  q('#dpList [data-dp-open="partner"]').click();
  await until(() => q('#plRow [data-pl-stage="deploy"]'), "the stages of partner");
  const out = { head: q("#dpHead").textContent, arrows: document.querySelectorAll("#plRow [data-pl-arrow]").length,
    hold: !!q("[data-pl-hold]") };
  q('#plRow [data-pl-stage="deploy"]').click();
  await until(() => q("#plDetail").textContent.includes("Followed"), "the stage detail");
  out.buttons = [...q("#plDetail").querySelectorAll("button")].length;
  q('[data-dp-tab="emergency"]').click();
  await until(() => q(".em-picks"), "the Emergency tab");
  out.picks = [...document.querySelectorAll("[data-em-pick]")].map(b => b.dataset.emPick);
  post(out);
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        r = self.drive(driver, "/", size="1280,900")
        self.assertIn("followed · read only", r["head"], r)
        self.assertEqual((r["arrows"], r["hold"], r["buttons"]), (0, False, 0), r)
        self.assertEqual(r["picks"], ["agents", "revoke"], r)

    def test_branches_draw_as_rows_that_fan_out_and_join(self):
        """#6142: a pipeline that splits draws each branch as a row of its own, with the lines that fan out and in;
        the join's arrow says what it waits for; no two stages overlap. At 1280 and at 390 px."""
        from river.pipeline import define
        define.pipeline_add(self.c, "api", project="site")
        for s, kind, after in (("build", "build", None), ("prod-a", "test", None), ("prod-b", "test", "build"),
                               ("smoke", "test", "prod-a,prod-b")):
            define.stage_set(self.c, "api", s, kind, after=after)
        driver = r'''
try {
  await until(() => window.__appReady, "the page");
  q('nav button[data-tab="deploy"]').click();
  await until(() => q('#dpList [data-dp-open="api"]'), "Deploy: the pipeline api");
  q('#dpList [data-dp-open="api"]').click();
  await until(() => q('#plRow [data-pl-stage="smoke"]'), "the stages of api");
  const box = n => q(`#plRow [data-pl-stage="${n}"]`).getBoundingClientRect();
  const cells = [...document.querySelectorAll("#plRow .pl-cell")].map(c => c.getBoundingClientRect());
  const over = cells.some((a, i) => cells.some((b, j) => i < j && a.left < b.right - 1 && b.left < a.right - 1
    && a.top < b.bottom - 1 && b.top < a.bottom - 1));
  post({ grid: q("#plRow").classList.contains("pl-grid"), fans: document.querySelectorAll("#plRow .pl-fan path").length,
    below: box("prod-b").top >= box("prod-a").bottom - 1, sameCol: Math.abs(box("prod-b").left - box("prod-a").left) < 2,
    joinRow: Math.abs(box("smoke").top - box("prod-a").top) < 2, join: q('#plRow [data-pl-arrow="smoke"]').textContent,
    over, page: document.documentElement.scrollWidth <= innerWidth });
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        for size in ("1280,900", "390,844"):
            r = self.drive(driver, "/", size=size)
            self.assertTrue(r["grid"] and r["below"] and r["sameCol"] and r["joinRow"], r)
            self.assertEqual(r["fans"], 4, r)  # two lines fan out to the branches, two fan in to the join
            self.assertIn("⋈ every branch", r["join"], r)
            self.assertFalse(r["over"], r)
            self.assertTrue(r["page"], r)

    def test_a_stage_that_starts_manually_shows_run_under_its_card(self):
        """#6138: the card of a stage that starts manually says so; the version that waits in it has Run under the
        card, and Run starts it. At 1280 and at 390 px, nothing wider than the page."""
        from river.pipeline import define, runner
        core.register(self.c, "mark", human=True)
        define.pipeline_add(self.c, "app", project="site")
        define.stage_set(self.c, "app", "build", "build")
        define.stage_set(self.c, "app", "prod", "build", start="manual")
        rid = runner._queue(self.c, "app", "prod", "run1234")
        self.c.execute("UPDATE stage_runs SET entered_at=? WHERE id=?", (core.iso(core.now()), rid))
        driver = r'''
try {
  await until(() => window.__appReady, "the page");
  q('nav button[data-tab="deploy"]').click();
  await until(() => q('#dpList [data-dp-open="app"]'), "Deploy: the pipeline app");
  q('#dpList [data-dp-open="app"]').click();
  await until(() => q('#plRow [data-pl-stage="prod"]'), "the stages of app");
  const out = { card: q('#plRow [data-pl-stage="prod"]').textContent.replace(/\s+/g, " "),
    build: q('#plRow [data-pl-stage="build"]').textContent.includes("✋"),
    run: q('#plRow [data-pl-run-stage="prod"]') && q('#plRow [data-pl-run-stage="prod"]').textContent.trim() };
  out.over = [...q("#plRow").querySelectorAll(".pl-stack, .pl-run")].filter(e => e.getBoundingClientRect().width > innerWidth)
    .map(e => e.className);
  if (q('#plRow [data-pl-run-stage="prod"]')) {
    q('#plRow [data-pl-run-stage="prod"]').click();
    await until(() => !q('#plRow [data-pl-run-stage="prod"]'), "Run is pressed");
  }
  post(out);
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        r = self.drive(driver, "/", size="1280,900")
        self.assertIn("✋ waits for Run", r["card"], r)
        self.assertFalse(r["build"], r)
        self.assertEqual(r["run"], "▶ Run run1234", r)
        self.assertEqual(r["over"], [], r)
        self.assertEqual(self.c.execute("SELECT run_by FROM stage_runs WHERE id=?", (rid,)).fetchone()[0], "mark")
        rid = runner._queue(self.c, "app", "prod", "run5678")
        self.c.execute("UPDATE stage_runs SET entered_at=? WHERE id=?", (core.iso(core.now()), rid))
        r = self.drive(driver, "/", size="390,844")
        self.assertEqual(r["run"], "▶ Run run5678", r)
        self.assertEqual(r["over"], [], r)

    def test_the_emergency_tab_shows_what_each_action_acts_on_and_stops_the_pipeline(self):
        """#6145: a pipeline's Emergency tab: its operators, four actions to pick, what the picked one acts on (a
        shared token warns), a reason, and Stop: a banner until Resume. At 1280 and at 390 px, nothing wider."""
        from river.pipeline import define, operators
        core.register(self.c, "mark", human=True)
        define.pipeline_add(self.c, "app", project="site")
        define.pipeline_add(self.c, "web")
        define.stage_set(self.c, "app", "build", "build")
        for p in ("app", "web"):
            define.forge_set(self.c, p, "github", repo=f"o/{p}", secret="gh-shared", actor="mark")
        operators.change(self.c, "app", add=["mark"], actor="mark")
        driver = r'''
try {
  await until(() => window.__appReady, "the page");
  q('nav button[data-tab="deploy"]').click();
  await until(() => q('#dpList [data-dp-open="app"]'), "Deploy: the pipeline app");
  q('#dpList [data-dp-open="app"]').click();
  await until(() => q('[data-dp-tab="emergency"]'), "the tabs of app");
  q('[data-dp-tab="emergency"]').click();
  await until(() => q(".em-picks"), "the Emergency tab");
  const out = { ops: q(".em-view").textContent.includes("mark"), picks: [...document.querySelectorAll("[data-em-pick]")].map(b => b.dataset.emPick) };
  q('[data-em-pick="revoke"]').click();
  await until(() => q(".dp-em-card").textContent.includes("Revoke its keys"), "the revoke card");
  out.revoke = q(".dp-em-card").textContent.replace(/\s+/g, " ");
  q('[data-em-pick="stop"]').click();
  await until(() => q(".dp-em-card").textContent.includes("Stop this pipeline"), "the stop card");
  if (q("#emWhy")) {
    q("#emWhy").value = "the prod database is slow";
    q('[data-em-go="stop"]').click();
  }
  await until(() => q(".em-view .pl-banner"), "the stop banner");
  out.banner = q(".em-view .pl-banner").textContent.replace(/\s+/g, " ");
  out.over = [...q(".em-view").querySelectorAll("*")].filter(e => e.getBoundingClientRect().right > innerWidth + 0.5)
    .map(e => e.tagName + "." + e.className).slice(0, 5);
  post(out);
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        for size in ("1280,900", "390,844"):
            r = self.drive(driver, "/", size=size)
            self.assertTrue(r["ops"], r)
            self.assertEqual(r["picks"], ["stop", "agents", "moveback", "override", "revoke"], r)  # override: #6146
            self.assertIn("gh-shared: the token that pipeline app reads its source with ⚠ web uses it too", r["revoke"], r)
            self.assertIn("app is stopped by mark", r["banner"], r)
            self.assertIn("the prod database is slow", r["banner"], r)
            self.assertEqual(r["over"], [], r)
        self.assertEqual(self.c.execute("SELECT actor, reason FROM pipeline_changes WHERE action='emergency-stop'")
                         .fetchall()[0][:], ("mark", "the prod database is slow"))

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
  check(!q("#capacity") && q("#next"), "Next up, and no Capacity: it is in Sessions only (#6005)");
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
  // Releases moved from Plan to the Deploy tab (#6136): the target prod is a pipeline row, its page has its card.
  q('nav button[data-tab="deploy"]').click();
  await until(() => q('#dpList [data-dp-open="prod"]') && location.pathname === "/deploy", "Deploy: the target prod as a pipeline");
  q('#dpList [data-dp-open="prod"]').click();
  await until(() => q("#targets").textContent.includes("prod") && location.pathname === "/deploy/pipeline/prod", "Deploy > prod: its release card");
  q('nav button[data-tab="plan"]').click(); q('[data-plan-view="goals"]').click();
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
  // #5848: Settings has tabs; Credentials is the first, and each tab shows only its own group.
  await until(() => location.pathname === "/settings" && !q("header").classList.contains("more")
    && q("#setTabs [data-set-tab]"), "the settings from the ⋯, at /settings, the menu closed");
  q('#setTabs [data-set-tab="sessions"]').click();
  await until(() => q("#setRows").textContent.includes("lease_ttl")
    && !q("#setRows").textContent.includes("ntfy_topic") && !q("#setPanel").classList.contains("hidden")
    && q("#credPanel").classList.contains("hidden"), "Settings > Sessions shows only its keys");
  check([...q("#setKey").options].some(o => o.value === "lease_ttl")
    && ![...q("#setKey").options].some(o => o.value === "ntfy_topic"), "the form lists the keys of the tab");
  // #5850: each key a row with its plain label, its field and the key small under it
  check(q("#setRows").textContent.includes("How long a claim lasts") && q('#setRows [data-set-input="lease_ttl"]'),
    "Settings > Sessions: lease_ttl is a row with its label and field");
  q('#setTabs [data-set-tab="people"]').click();
  await until(() => !q("#peoplePanel").classList.contains("hidden") && q("#setPanel").classList.contains("hidden"),
    "Settings > People");
  check(q("#people").textContent.includes("mark"), "Settings > People");
  q('#setTabs [data-set-tab="credentials"]').click();  // as a person leaves it: the first tab
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
    if (tab === "deploy") {  // its pipelines, then a pipeline's own page (#6136)
      await look(tab); const row = q("#dpList [data-dp-open]");
      if (row) { row.click(); await look("deploy/pipeline"); }
      continue;
    }
    if (tab !== "plan") { await look(tab); continue; }
    for (const v of ["overview", "items", "goals", "graph", "done"]) { q(`[data-plan-view="${v}"]`).click(); await look("plan/" + v); }
  }
  q('nav button[data-tab="sessions"]').click();
  q("#sessYouRow .srow-main").click(); await look("sessions/you");
  q('#sessTabs [data-sess-tab="arts"]').click(); await look("sessions/artifacts");
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
         wraps: last.getBoundingClientRect().height > 1.5 * one.getBoundingClientRect().height,  // two lines or more: Linux fonts are narrower
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
        fake.write_text(FAKE, encoding="utf-8")
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
        fake.write_text(FAKE, encoding="utf-8")
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
        # ag runs a turn (a permission prompt in its pane): the message waits for its end, once (#5587).
        held = [p["text"] for p in core.pending_list(self.c, "ag")]
        self.assertEqual(len(held), 1, held)
        self.assertRegex(held[0], r"^\d+ bytes in [a-z]{2}(-[A-Z]{2})?\.$")

    def test_the_chat_scrolls_up_through_two_compactions_to_the_start(self):
        """#5120: a compaction is a thin divider with its summary folded closed under it; scrolling up loads the earlier
        lines a page at a time, and the lines in sight stay where they are, up to 'Start of the session'."""
        from tests.test_view_sessions import History
        h = History.__new__(History)
        self.path.write_text("", encoding="utf-8")
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
  // A closed project opens with its fold. An open one builds its table in a later frame: a click on its fold
  // before the rows came closed it, and no row ever showed (#5713).
  const f = q('#projects [data-fold][aria-expanded="false"]'); if (f) f.click();
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
        # ag runs a turn (a permission prompt in its pane): the message and its file wait for the turn's end (#5587).
        (held,) = core.pending_list(self.c, "ag")
        self.assertEqual((held["text"], [n.endswith("notes.txt") for n in held["names"]]), ("first line for ag", [True]))

    def _stop_and_send_now(self, size):
        """#5547, #5587 (mark 2026-11-12: 'the stop button should not be up at the top'; 'the send now should not be
        a lightning bolt, instead it should be a little small button under the messages that are waiting'). ag waits
        at a permission prompt in its pane, so its turn runs. With the box empty, a Stop square stands in the send
        arrow's place, and the head has no Stop; with text, only the arrow shows. A message sent now waits under the
        chat, muted, with 'Waits for the turn · Send now'; nothing goes into the pane. It is there again after a
        reload; its Send now stops the turn and sends it. The square reads in light and dark, and fits."""
        driver = r'''try {
  const box = (s) => q(s).getBoundingClientRect();
  if (!sessionStorage.getItem("held")) {
    await until(() => window.__appReady && q("#sessTitle").textContent === "ag" && !q("#sessText").disabled
                      && q("#sessBar").classList.contains("can-now"), "the chat of ag that can stop");
    const head = q(".sess-head"), pill = q("#sessText").parentElement.getBoundingClientRect();
    const reads = () => { const cs = getComputedStyle(q("#sessStop")); return cs.color !== cs.backgroundColor; };
    const out = { headFits: head.scrollWidth <= head.clientWidth + 1,
                  headStop: !!head.querySelector(":scope > [data-stop], :scope > * > [data-stop]:not(.sess-mi)"),
                  stopIn: box("#sessStop").width > 0 && box("#sessStop").right <= pill.right,
                  arrowHidden: box("#sessSend").width === 0, bolt: !!q("#sessNow"), light: reads() };
    document.documentElement.dataset.theme = "dark"; out.dark = reads(); delete document.documentElement.dataset.theme;
    const x = q("#sessText"); x.focus(); x.setRangeText("use the other file", 0, 0, "end");
    x.dispatchEvent(new Event("input", { bubbles: true }));
    await until(() => box("#sessSend").width > 0, "the arrow shows with text");
    out.stopHidden = box("#sessStop").width === 0;
    out.arrowIn = box("#sessSend").right <= pill.right;
    q("#sessBar").requestSubmit();
    await until(() => x.value === "" && q("#sessPend [data-now]"), "the message waits under the chat");
    out.note = q("#sessPend .msg-note").textContent.trim();
    out.muted = getComputedStyle(q("#sessPend .bubble")).opacity < 1;
    sessionStorage.setItem("held", JSON.stringify(out));
    location.reload();
  } else {
    const out = JSON.parse(sessionStorage.getItem("held"));
    await until(() => window.__appReady && q("#sessPend [data-now]"), "the waiting message after a reload");
    out.afterReload = q("#sessPend .bubble").textContent.trim();
    await fetch("/__step/before_now");
    q("#sessPend [data-now]").click();
    await until(() => !q("#sessPend [data-now]"), "Send now sends it");
    await fetch("/__step/after_now");
    await until(() => q("#sessOpen [data-stop]") && box("#sessStop").width > 0, "Stop in the bar and in the menu");
    out.menu = [...q("#sessOpen").querySelectorAll("button, a")].map(b => b.childNodes[0].textContent.trim());
    q("#sessStop").click();
    await sleep(400);
    post(out);
  }
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        keys = {}
        self.c.execute("UPDATE agents SET agent_type='claude-code' WHERE name='ag'")
        self.pane["keys"].clear()
        got = self.drive(driver, "/sessions/ag", size=size,
                         steps={"before_now": lambda: keys.update(held=list(self.pane["keys"])),
                                "after_now": lambda: keys.update(now=list(self.pane["keys"]))})
        self.assertEqual(got, {"headFits": True, "headStop": False, "stopIn": True, "arrowHidden": True, "bolt": False,
                               "light": True, "dark": True, "stopHidden": True, "arrowIn": True,
                               "note": "Waits for the turn · Send now", "muted": True,
                               "afterReload": "use the other file",
                               "menu": ["Open on claude.ai", "Stop its turn", "Pin to the top", "Settings", "End"]})
        self.assertEqual(keys["held"], [])  # nothing went into the pane while it waited
        self.assertEqual(keys["now"], ["Escape", "use the other file", "Enter"])
        self.assertEqual(self.pane["keys"][3:], ["Escape"])  # Stop
        self.assertEqual(core.pending_list(self.c, "ag"), [])

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

    def test_the_session_list_folds_and_the_page_keeps_it(self):
        """#5626 (mark: 'we need to be able to collape the running session left panel'): the button at the top of the
        list folds it to a strip, the chat and the open side panel take its width, a reload keeps it folded, and
        Cmd+\\ opens it again. On a phone the list is a view of its own and has no such button."""
        driver = r'''try {
  await until(() => window.__appReady, "app.js loaded");
  if (innerWidth < 760) {
    q('nav button[data-tab="sessions"]').click();
    await until(() => q("#sessTitle").textContent === "ag", "session ag shown");
    q("#sessBack").click();
    post({ phone: q("#sessFold").getBoundingClientRect().width === 0 });
  } else {
    const step = sessionStorage.getItem("step") || "fold";
    const w = (s) => Math.round(q(s).getBoundingClientRect().width);
    await until(() => q("#sessTitle").textContent === "ag", "session ag shown");
    const { openInPanel } = await import("./components/sidePanel.js");
    if (step === "fold") {
      openInPanel("http://127.0.0.1:9/other");
      await until(() => w("#sessPanel") > 0, "the side panel");
      const side = w(".sess-side"), chat = w("#sessMain"), panel = w("#sessPanel");
      q("#sessFold").click();
      const out = { open: side >= 280, strip: w(".sess-side") < 60, wider: w("#sessMain") - chat >= side - 60,
                    panel: w("#sessPanel") === panel, rows: q("#sessions").getBoundingClientRect().width === 0,
                    label: q("#sessFold").getAttribute("aria-label") };
      sessionStorage.setItem("out", JSON.stringify(out)); sessionStorage.setItem("step", "reload");
      location.reload();
    } else {
      const out = JSON.parse(sessionStorage.getItem("out"));
      out.kept = w(".sess-side") < 60;
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "\\", metaKey: true, bubbles: true }));
      out.key = w(".sess-side") >= 280 && q("#sessFold").getAttribute("aria-expanded") === "true";
      post(out);
    }
  }
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        self.assertEqual(self.drive(driver, "/sessions/ag", size="1280,900"),
                         {"open": True, "strip": True, "wider": True, "panel": True, "rows": True,
                          "label": "Show the session list", "kept": True, "key": True})
        self.assertEqual(self.drive(driver, "/sessions/ag", size="390,844"), {"phone": True})

    def _address_suggestions(self, size):
        """#5635: the Browser pane's address field lists the addresses the browser visited that match the typed words,
        and a last row that searches Google; Down chooses a row, Esc closes the list. It fits a phone (p57)."""
        from unittest import mock
        from river import browser
        project = browser.project_of(self.c, agent="ag")
        b = browser.get(project, "ag")  # the browser of the panel's session (#5636)
        self.addCleanup(browser.BROWSERS.clear)
        browser.note_visit(b, "https://news.ycombinator.com/", "Hacker News")
        browser.note_visit(b, "https://example.com/news", "Example news")
        no = mock.patch.object(browser, "no_chrome", lambda: "no Chrome in this test")  # the pane starts no Chrome
        no.start()
        self.addCleanup(no.stop)
        driver = r'''try {
  await until(() => window.__appReady && q("#sessTitle").textContent === "ag", "session ag shown");
  const { openBrowser } = await import("./components/browserTool.js");
  await openBrowser("ag");
  await until(() => q(".br-addr"), "the address field");
  const addr = q(".br-addr"), list = () => q(".sugg-list");
  await until(() => !q(".br-msg").classList.contains("hidden") && q(".br-msg").textContent.includes("Chrome"), "the pane started");
  await sleep(300);
  addr.focus(); addr.value = "news"; addr.dispatchEvent(new Event("input"));
  await until(() => list() && !list().classList.contains("hidden"), "the list: " + document.activeElement.className);
  const rows = () => [...document.querySelectorAll(".sugg-row")];
  const out = { rows: rows().map(r => r.textContent), combo: addr.getAttribute("aria-expanded") };
  const key = (k) => addr.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true }));
  key("ArrowDown"); key("ArrowDown");
  out.chosen = q(".sugg-row.on").textContent;
  out.active = addr.getAttribute("aria-activedescendant") === q(".sugg-row.on").id;
  const r = list().getBoundingClientRect();
  out.fits = r.left >= 0 && r.right <= innerWidth && r.height > 0;
  out.wide = document.documentElement.scrollWidth > innerWidth;
  key("Escape");
  out.closed = list().classList.contains("hidden") && addr.getAttribute("aria-expanded") === "false";
  post(out);
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        got = self.drive(driver, "/sessions/ag", size=size)
        self.assertEqual(got, {"rows": ["Hacker Newshttps://news.ycombinator.com/", "Example newshttps://example.com/news",
                                        "Search Google for \u201cnews\u201d"],
                               "combo": "true", "chosen": "Example newshttps://example.com/news", "active": True,
                               "fits": True, "wide": False, "closed": True})

    def test_the_address_bar_suggests_at_desktop_width(self):
        self._address_suggestions("1280,900")

    def test_the_address_bar_suggests_at_phone_width(self):
        self._address_suggestions("390,844")

    def _tab_strip(self, size):
        """#5632: the tab strip shows each tab of the session's browser and a + button; the shown tab is marked; on a
        phone the strip scrolls sideways and the page does not get wider (p57)."""
        from unittest import mock
        from river import browser
        project = browser.project_of(self.c, agent="ag")
        b = browser.get(project, "ag")
        self.addCleanup(browser.BROWSERS.clear)
        b.targets = {f"T{i}": {"url": f"https://example{i}.com/", "title": f"A long page title number {i} of the test",
                               "opener": None} for i in range(1, 6)}
        b.target = "T2"
        no = mock.patch.object(browser, "no_chrome", lambda: "no Chrome in this test")  # the pane starts no Chrome
        no.start()
        self.addCleanup(no.stop)
        driver = r'''try {
  await until(() => window.__appReady && q("#sessTitle").textContent === "ag", "session ag shown");
  const { openBrowser } = await import("./components/browserTool.js");
  await openBrowser("ag");
  await until(() => document.querySelectorAll(".br-tab").length === 5, "five tabs");
  const s = q(".br-strip"), on = q(".br-tab.on");
  post({ tabs: document.querySelectorAll(".br-tab").length, on: on.textContent.trim(), plus: !!s.querySelector('[data-br="new_tab"]'),
         closes: s.querySelectorAll('[data-br="close_tab"]').length, scrolls: s.scrollWidth > s.clientWidth,
         wide: document.documentElement.scrollWidth > innerWidth, visible: on.getBoundingClientRect().right <= innerWidth });
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        return self.drive(driver, "/sessions/ag", size=size)

    def _frames(self, size):
        """#5637: the pane reads binary frames (bin=1) and shows each JPEG from a blob address; the next frame takes its
        place, the text over the stage goes, and the page does not get wider (p57)."""
        import base64
        from unittest import mock
        from river import browser
        project = browser.project_of(self.c, agent="ag")
        b = browser.get(project, "ag")
        self.addCleanup(browser.BROWSERS.clear)
        b.ws = object()  # running, with no Chrome: the test gives the frames

        def put(w):
            with b.cond:
                b.seq += 1
                b.frame = {"seq": b.seq, "data": base64.b64encode(gray_jpeg(w)).decode(), "w": w * 50, "h": 400}
                b.cond.notify_all()
        put(8)
        no = mock.patch.object(browser, "no_chrome", lambda: "no Chrome in this test")
        no.start()
        self.addCleanup(no.stop)
        driver = r'''try {
  await until(() => window.__appReady && q("#sessTitle").textContent === "ag", "session ag shown");
  const { openBrowser } = await import("./components/browserTool.js");
  await openBrowser("ag");
  const img = () => q(".br-img");
  await until(() => img() && img().naturalWidth === 8, "the first frame");
  const out = { blob: img().src.startsWith("blob:"), msg: q(".br-msg").classList.contains("hidden") };
  await fetch("/__step/second");
  await until(() => img().naturalWidth === 16, "the second frame");
  const r = img().getBoundingClientRect();
  out.drawn = r.width > 0 && r.height > 0 && r.right <= innerWidth;
  out.wide = document.documentElement.scrollWidth > innerWidth;
  post(out);
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        return self.drive(driver, "/sessions/ag", steps={"second": lambda: put(16)}, size=size)

    def _pointer(self, size):
        """#5893: the session's pointer glides only for a new move of the session; when the stage changes size (an edge
        dragged) it goes to its point's new place at once; a pointer with no move for PTR_IDLE fades."""
        import base64
        import time as _time
        from unittest import mock
        from river import browser
        project = browser.project_of(self.c, agent="ag")
        b = browser.get(project, "ag")
        self.addCleanup(browser.BROWSERS.clear)
        b.ws = object()  # running, with no Chrome: the test gives the frame and the pointer
        b.targets = {"T1": {"url": "https://example.com/", "title": "Example", "opener": None}}
        b.target = "T1"
        with b.cond:
            b.seq += 1
            b.frame = {"seq": b.seq, "data": base64.b64encode(gray_jpeg(8)).decode(), "w": 800, "h": 600}
        browser.note_pointer(b, "ag", 400, 300)

        def old():  # a move that came 20 s ago (a pane that opens later sees it so)
            b.pointer = {**b.pointer, "x": 50, "y": 50, "n": b.pointer["n"] + 1, "at": _time.time() - 20}
            browser._notify(b)
        no = mock.patch.object(browser, "no_chrome", lambda: "no Chrome in this test")
        no.start()
        self.addCleanup(no.stop)
        driver = r'''try {
  await until(() => window.__appReady && q("#sessTitle").textContent === "ag", "session ag shown");
  const { openBrowser } = await import("./components/browserTool.js");
  await openBrowser("ag");
  const ptr = () => q(".br-ptr"), tf = () => ptr().style.transform, dur = () => ptr().style.transitionDuration;
  await until(() => ptr() && !ptr().classList.contains("hidden") && tf(), "the pointer");
  await sleep(300);
  const t1 = tf(), out = { first: dur() };
  const st = q(".br-stage"); st.style.marginRight = Math.round(st.getBoundingClientRect().width / 2) + "px";
  await until(() => tf() !== t1, "the pointer follows the layout");
  out.layout = dur();
  const t2 = tf();
  await fetch("/__step/move");
  await until(() => tf() !== t2, "the new move");
  out.move = dur() !== "0ms";
  out.idle = ptr().classList.contains("br-ptr-idle");
  await fetch("/__step/old");
  await until(() => ptr().classList.contains("br-ptr-idle"), "an old move fades");
  out.faded = true;
  out.wide = document.documentElement.scrollWidth > innerWidth;
  post(out);
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        return self.drive(driver, "/sessions/ag", size=size,
                          steps={"move": lambda: browser.note_pointer(b, "ag", 100, 100), "old": old})

    def test_the_pointer_glides_only_for_a_move_at_desktop_width(self):
        self.assertEqual(self._pointer("1280,900"),
                         {"first": "0ms", "layout": "0ms", "move": True, "idle": False, "faded": True, "wide": False})

    def test_the_pointer_glides_only_for_a_move_at_phone_width(self):
        self.assertEqual(self._pointer("390,844"),
                         {"first": "0ms", "layout": "0ms", "move": True, "idle": False, "faded": True, "wide": False})

    def test_the_pane_shows_binary_frames_at_desktop_width(self):
        self.assertEqual(self._frames("1280,900"), {"blob": True, "msg": True, "drawn": True, "wide": False})

    def test_the_pane_shows_binary_frames_at_phone_width(self):
        self.assertEqual(self._frames("390,844"), {"blob": True, "msg": True, "drawn": True, "wide": False})

    def _box(self, size):
        """#5674: a prompt box of the page shows over the frame with its text, its default answer, OK and Cancel, inside
        the pane (p57: also at 390 px); OK sends the typed answer (op browser, action dialog) and the box goes."""
        import base64
        import time as _time
        from unittest import mock
        from river import browser
        project = browser.project_of(self.c, agent="ag")
        b = browser.get(project, "ag")
        self.addCleanup(browser.BROWSERS.clear)
        b.ws = object()  # running, with no Chrome: the test gives the frame and the box
        with b.cond:
            b.seq += 1
            b.frame = {"seq": b.seq, "data": base64.b64encode(gray_jpeg(8)).decode(), "w": 400, "h": 400}
            b.dialog = {"type": "prompt", "message": "Your name for the order? " * 4, "default": "Ann", "url": "u",
                        "session": "S", "at": _time.time()}
        answers = []

        def answer(b, accept=True, text=None, who=None, wait=None):
            answers.append((accept, text))
            with b.cond:
                b.dialog = None
                b.seq += 1
                b.cond.notify_all()
            return {**b.info(), "navigated": False}
        for p in (mock.patch.object(browser, "no_chrome", lambda: "no Chrome in this test"),
                  mock.patch.object(browser, "answer_dialog", answer)):
            p.start()
            self.addCleanup(p.stop)
        driver = r'''try {
  await until(() => window.__appReady && q("#sessTitle").textContent === "ag", "session ag shown");
  const { openBrowser } = await import("./components/browserTool.js");
  await openBrowser("ag");
  const box = () => q(".br-box");
  await until(() => box() && !box().classList.contains("hidden"), "the box");
  const r = box().getBoundingClientRect(), st = q(".br-stage").getBoundingClientRect(), ok = q('[data-br="dialog_yes"]');
  const out = { text: q(".br-boxt").textContent.startsWith("Your name for the order?"), value: q(".br-boxin").value,
    cancel: !q(".br-boxno").classList.contains("hidden"),
    inside: r.left >= st.left && r.right <= st.right && r.top >= st.top && ok.getBoundingClientRect().bottom <= st.bottom,
    wide: document.documentElement.scrollWidth > innerWidth };
  q(".br-boxin").value = "Bob";
  ok.click();
  await until(() => box().classList.contains("hidden"), "the box goes");
  post(out);
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        out = self.drive(driver, "/sessions/ag", size=size)
        self.assertEqual(answers, [(True, "Bob")])
        return out

    def test_the_pane_shows_a_box_of_the_page_at_desktop_width(self):
        self.assertEqual(self._box("1280,900"), {"text": True, "value": "Ann", "cancel": True,
                                                 "inside": True, "wide": False})

    def _rec(self, size):
        """#5707: Record steps in the pane starts a recording of the person's steps (op browser_steps); the button
        then says Stop recording with the count, inside the pane (p57: also at 390 px); Stop ends it."""
        import base64
        from river import browser
        project = browser.project_of(self.c, agent="ag")
        b = browser.get(project, "ag")
        self.addCleanup(browser.BROWSERS.clear)
        self.addCleanup(browser.STEPS_ON.clear)
        b.ws = object()  # running, with no Chrome: the test gives the frame
        with b.cond:
            b.seq += 1
            b.frame = {"seq": b.seq, "data": base64.b64encode(gray_jpeg(8)).decode(), "w": 400, "h": 400}
        driver = r'''try {
  await until(() => window.__appReady && q("#sessTitle").textContent === "ag", "session ag shown");
  const { openBrowser } = await import("./components/browserTool.js");
  await openBrowser("ag");
  const rec = () => q(".br-rec");
  await until(() => rec() && !rec().classList.contains("hidden"), "the Record button");
  const before = rec().textContent;
  rec().click();
  await until(() => rec().getAttribute("aria-pressed") === "true", "recording");
  const r = rec().getBoundingClientRect(), pane = q(".br").getBoundingClientRect();
  const out = { before, during: rec().textContent, inside: r.left >= pane.left && r.right <= pane.right + 0.5,
    wide: document.documentElement.scrollWidth > innerWidth };
  rec().click();
  await until(() => rec().getAttribute("aria-pressed") === "false", "stopped");
  out.after = rec().textContent;
  post(out);
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        out = self.drive(driver, "/sessions/ag", size=size)
        self.assertIsNone(b.psteps)
        return out

    def test_the_pane_records_steps_at_desktop_width(self):
        self.assertEqual(self._rec("1280,900"), {"before": "Record steps", "during": "Stop recording (0)",
                                                 "inside": True, "wide": False, "after": "Record steps"})

    def test_the_pane_records_steps_at_phone_width(self):
        self.assertEqual(self._rec("390,844"), {"before": "Record steps", "during": "Stop recording (0)",
                                                "inside": True, "wide": False, "after": "Record steps"})

    def test_the_pane_shows_a_box_of_the_page_at_phone_width(self):
        self.assertEqual(self._box("390,844"), {"text": True, "value": "Ann", "cancel": True,
                                                "inside": True, "wide": False})

    def _annotate(self, size):
        """#5655: Annotate in the Browser pane, with a real Chrome as the session's browser: the frame stops, a box, a
        pen line, an arrow, a text note and a selected element go on the layer, and Send stores the set and puts one
        message with the picture into the session's chat; the list then shows the set, and a fixed note's state and
        line. With MAXPM_ANNOTATE_SHOTS=<folder>, the pane and the picture are saved there."""
        import http.server
        import shutil
        import threading
        from unittest import mock
        from river import annotate, browser
        if not browser.chrome_path():
            self.skipTest("no Chrome on this computer")
        body = (b"<title>Pricing</title><style>body{font:16px system-ui;margin:24px} .plan{border:1px solid #ccc;"
                b"padding:16px;margin:12px 0;width:260px} button{font-size:16px;width:70px;white-space:nowrap;overflow:"
                b"hidden}</style><h1>Pricing</h1><div class=plan id=pro><h2>Pro</h2><p>$12 a month</p>"
                b"<button id=buy>Buy the Pro plan</button></div><div class=plan><h2>Team</h2><p>$40 a month</p></div>")

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        try:
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        except PermissionError:
            self.skipTest("no local port here")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        project = browser.project_of(self.c, agent="ag")
        core.config_set(self.c, "browser_allow", f"127.0.0.1:{srv.server_address[1]}", project=project)
        b = browser.get(project, "ag")
        self.addCleanup(browser.BROWSERS.clear)
        self.addCleanup(b.close)
        b.open(f"http://127.0.0.1:{srv.server_address[1]}/pricing")
        sent = []
        patched = mock.patch.object(server, "session_send", lambda conn, to, text, **kw: sent.append((to, text, kw)) or {})
        patched.start()
        self.addCleanup(patched.stop)
        shots = os.environ.get("MAXPM_ANNOTATE_SHOTS")
        tag = size.replace(",", "x")
        import socket
        with socket.socket() as s:  # a free port for the test browser's own CDP: never a Chrome of the person
            s.bind(("127.0.0.1", 0))
            cdp_port = s.getsockname()[1]

        def shot(name):
            def go():
                if not shots:
                    return
                import base64
                import urllib.request
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # no proxy for this computer
                tabs = json.loads(opener.open(f"http://127.0.0.1:{cdp_port}/json").read())
                ws = browser.WS(next(t for t in tabs if t["type"] == "page" and t["url"].startswith("http://127.0.0.1:")
                                     and ("/sessions/ag" in t["url"] or t["url"].endswith("/__frame")))[
                    "webSocketDebuggerUrl"])
                ws.send(json.dumps({"id": 1, "method": "Page.captureScreenshot", "params": {"format": "png"}}))
                while True:
                    m = json.loads(ws.recv())
                    if m.get("id") == 1:
                        break
                ws.close()
                Path(shots, f"{tag}-{name}.png").write_bytes(base64.b64decode(m["result"]["data"]))
            return go

        def fix():
            conn = core.connect()  # the step runs in a thread of the test server
            try:
                s = annotate.sets(conn, agent="ag")[0]
                annotate.mark(conn, s["id"], 3, "fix", "The button is wider now: it fits its label", "ag")
            finally:
                conn.close()
        driver = r'''try {
  await until(() => window.__appReady && q("#sessTitle").textContent === "ag", "session ag shown");
  // The dialogs of the fixture's session (its prompt, the setup) would cover the pane in the screenshots.
  document.head.insertAdjacentHTML("beforeend", "<style>.dlg, dialog { display: none !important; }</style>");
  const { openBrowser } = await import("./components/browserTool.js");
  await openBrowser("ag");
  const img = () => q(".br-img");
  await until(() => img() && img().naturalWidth > 100 && !q(".br-annbtn").classList.contains("hidden"), "the page and Annotate");
  q(".br-annbtn").click();
  await until(() => !q(".br-draw").classList.contains("hidden") && img().src.startsWith("data:image/png"), "the mode and its PNG");
  const layer = q(".br-draw"), out = { frozen: true };
  await sleep(200);
  const R = () => layer.getBoundingClientRect(), vb = layer.viewBox.baseVal;
  const at = (x, y) => ({ clientX: R().left + x * R().width / vb.width, clientY: R().top + y * R().height / vb.height });
  const ev = (type, x, y) => layer.dispatchEvent(new PointerEvent(type, { bubbles: true, cancelable: true, pointerId: 1, pointerType: "touch", ...at(x, y) }));
  const tool = (t) => q(`[data-ann="tool"][data-tool="${t}"]`).click();
  const note = async (text) => { await until(() => !q(".br-annnote").classList.contains("hidden"), "the note field");
    q(".br-nt").value = text; q(".br-annnote").requestSubmit(); };
  const drag = (pts) => { ev("pointerdown", ...pts[0]); for (const p of pts.slice(1)) ev("pointermove", ...p); ev("pointerup", ...pts[pts.length - 1]); };
  // A box over the Team plan, a pen line under the price, an arrow, a text note, the Buy button selected.
  tool("box"); drag([[16, 248], [330, 248], [330, 398]]); await note("Too much space in this plan");
  tool("pen"); drag([[42, 192], [70, 196], [100, 191], [130, 197], [150, 192]]);
  tool("arrow"); drag([[220, 60], [135, 45]]);
  tool("text"); ev("pointerdown", 230, 40); await note("Make the title bigger");
  tool("element"); ev("pointerdown", 60, 214);
  await until(() => [...layer.querySelectorAll("rect")].length >= 2, "the selected element's box");
  await note("The label is cut off");
  out.shapes = layer.querySelectorAll("[data-i]").length;
  out.numbers = [...layer.querySelectorAll("circle + text")].map(t => t.textContent);
  out.wide = document.documentElement.scrollWidth > innerWidth;
  await fetch("/__step/drawn");
  q('[data-ann="send"]').click();
  await until(() => q(".br-draw").classList.contains("hidden"), "Send ends the mode");
  out.live = !img().src.startsWith("data:image/png");
  await fetch("/__step/fix");
  q(".br-annbtn").click();
  await until(() => q(".br-set"), "the list of sets");
  await until(() => q(".br-st-fixed"), "a fixed note");
  out.fixed = q(".br-st-fixed").textContent.includes("The button is wider now");
  out.reopen = !!q('.br-st-fixed [data-ann="reopen"]');
  out.marks = layer.querySelectorAll(".br-old").length;
  await fetch("/__step/list");
  q('.br-st-fixed [data-ann="reopen"]').click();
  await until(() => !q(".br-st-fixed"), "reopened");
  document.activeElement.blur();
  q(".br").dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  out.esc = q(".br-draw").classList.contains("hidden");
  post(out);
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        got = self.drive(driver, "/sessions/ag", size=size, steps={"drawn": shot("1-drawn"), "fix": fix,
                                                                   "list": shot("3-list")},
                         flags=(f"--remote-debugging-port={cdp_port}",) if shots else ())
        s = annotate.sets(self.c, agent="ag")[0]
        if shots:
            shutil.copy(s["picture"], Path(shots, f"{tag}-2-picture.png"))
            Path(shots, f"{tag}-2-message.txt").write_text(sent[0][1], encoding="utf-8")
        return got, s, sent

    def test_annotate_at_desktop_width(self):
        got, s, sent = self._annotate("1280,900")
        self.assertEqual(got, {"frozen": True, "shapes": 7, "numbers": ["1", "2", "3"], "wide": False, "live": True,
                               "fixed": True, "reopen": True, "marks": 3, "esc": True})
        self.assertEqual([(x["kind"], x["text"]) for x in s["notes"]],
                         [("box", "Too much space in this plan"), ("text", "Make the title bigger"),
                          ("element", "The label is cut off")])
        self.assertEqual((s["notes"][2]["ref"]["selector"], s["drawings"]), ("#buy", {"pen": 1, "arrow": 1}))
        self.assertEqual(s["notes"][2]["state"], "open")  # the person reopened it
        to, text, kw = sent[0]
        self.assertEqual((to, kw["files"][0]["name"]), ("ag", f"annotations-a{s['id']}.png"))
        self.assertIn('3. Element `<button>` found by `#buy`, text "Buy the Pro plan"', text)
        with open(s["picture"], "rb") as f:
            head = f.read(24)
        self.assertEqual(struct.unpack(">II", head[16:24]), (s["w"], s["h"]))  # the page's size

    def test_annotate_at_phone_width(self):
        got, s, sent = self._annotate("390,844")
        self.assertEqual((got["shapes"], got["wide"], got["fixed"], got["esc"]), (7, False, True, True), got)

    def test_the_tab_strip_at_desktop_width(self):
        got = self._tab_strip("1280,900")
        self.assertEqual({k: got.get(k) for k in ("tabs", "on", "plus", "closes", "wide", "visible")},
                         {"tabs": 5, "on": "A long page title number 2 of the test×", "plus": True, "closes": 5,
                          "wide": False, "visible": True}, got)

    def test_the_tab_strip_at_phone_width(self):
        got = self._tab_strip("390,844")
        self.assertEqual({k: got.get(k) for k in ("tabs", "scrolls", "wide", "visible")},
                         {"tabs": 5, "scrolls": True, "wide": False, "visible": True}, got)

    def test_the_you_row_opens_what_waits_on_you(self):
        """#5102 steps 1 and 3 (design #4657): the You row at the top of the Sessions list has a ! badge with what waits on
        the person, and opens the You view (Needs you, the inbox) at /you in place of a session's chat; a session row
        shows its chat again, Back returns to You, and the Board's 'need you' count goes there too. The Artifacts row
        under it shows the artifacts of all sessions at /artifacts, in place of the Artifacts tab."""
        core.config_set(self.c, "setup_done", "on")
        core.item_add(self.c, "site", "Approve the logo", doer="human")
        core.send(self.c, "question", "Which color?", to="mark", actor="ag")
        core.sync_needs_you(self.c)  # as maxpm add does; the page build syncs nothing since #5597
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
  await until(() => q('#sessTabs [data-sess-tab="arts"]'), "the Artifacts tab");
  q('#sessTabs [data-sess-tab="arts"]').click();
  await until(() => location.pathname === "/artifacts" && shown(q("#sessArts")) && !shown(q("#sessYou")), "the Artifacts tab: /artifacts");
  out.arts = q('#sessTabs [data-sess-tab="arts"]').classList.contains("on") && !q("#sessYouRow .srow").classList.contains("on")
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
        core.sync_needs_you(self.c)  # as maxpm add does; the page build syncs nothing since #5597
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
  await until(() => location.pathname === "/you", "the address /you");  // it follows the view (a slow CI runner saw /)
  post({ path: location.pathname, tab: q("#tabName").textContent, tabs: [...document.querySelectorAll("nav button")].map(b => b.dataset.tab) });
} catch (e) { post({ error: String(e && e.stack || e) }); }'''
        self.assertEqual(self.drive(driver, "/?as=mark"), {"path": "/you", "tab": "Sessions",
                                                           "tabs": ["sessions", "plan", "deploy", "terminal"]})

    def test_the_work_controls_of_the_sessions_list(self):
        """#5102 step 2 (design #4657): Start an agent at the top of the list; All (#5886: Active | All at the right of
        the Sessions line) shows the ended sessions; Give work in the head of a waiting session; the queue and + item in a session's Items list."""
        core.config_set(self.c, "setup_done", "on")
        footer = core.item_add(self.c, "site", "footer")["id"]
        core.register(self.c, "old", note="built the logo")
        core.register(self.c, "wt")
        core.queue_add(self.c, "ag", footer, actor="mark")
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET last_seen='2026-01-01T00:00:00Z' WHERE name='old'")
            self.c.execute("UPDATE agents SET role='waiting', waiting_in='site' WHERE name='wt'")
        driver = r'''try {
  await until(() => window.__appReady && q("#sessStart [data-launch]") && q('#sessStatusRow [data-sess-show="all"]'), "Start an agent and All");
  q('#sessStatusRow [data-sess-show="all"]').click();
  await until(() => q('#sessEnded .srow[data-session="old"]'), "the ended session");
  await until(() => q('#sessions .srow[data-session="ag"][data-agent="ag"]'), "a running row is a drop target");
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
  document.body.style.minHeight = "";  // the room to scroll was for Plan: its page scrollbar (CI's Chrome) narrowed the chat
  q('nav button[data-tab="sessions"]').click();
  if (!q("#sessYou").classList.contains("hidden")) q("#youBack").click();  // the first page showed You (#5102)
  await until(() => q('#sessions .srow[data-session="ag"] .srow-main'), "the row of ag");
  q('#sessions .srow[data-session="ag"] .srow-main').click();
  await until(() => q("#sessTitle").textContent === "ag" && q("#sessLayout").classList.contains("phone-chat"), "the chat of ag");
  // The ⋯ card's Open session row comes with the first read of the session: on the macOS runner of GitHub it
  // answered after the title showed, and the row was empty (#5602).
  await until(() => q("#sessOpen").childElementCount, "the first read of ag");
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
        self.assertEqual(got, {"bar": 48, "parts": True, "name": "Sessions", "sheet": 4, "scrim": True,  # Deploy: #6136
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
        plan.write_text("# Plan", encoding="utf-8")
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
    // #5804: a realquicksite page needs the person's own sign-in: a new tab of the browser, not the panel.
    const opened = [], open = window.open;
    window.open = (u) => { opened.push(u); return null; };
    openInPanel("https://p1.realquicksite.com/a");
    window.open = open;
    check(opened[0] === "https://p1.realquicksite.com/a" && where() === "/sessions/ag/artifacts/" + artId("http://127.0.0.1:9/other"),
          "a realquicksite page opens in a new tab: " + JSON.stringify(opened) + " " + where());
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
