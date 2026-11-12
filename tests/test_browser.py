"""The browser of a project in the side panel (#4631): the WebSocket framing, the network rule, the keys, the
controller rule, the project of a command, maxpm browser, the page's routes, and one real Chrome when there is one."""

import base64
import contextlib
import http.server
import io
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests import chrome
from river import browser, cli, core, server
from river.core import RiverError
from river.routes import browser as routes


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        old = os.environ.get("MAXPM_DB")
        os.environ["MAXPM_DB"] = os.path.join(self.tmp.name, "t.db")
        self.addCleanup(lambda: os.environ.__setitem__("MAXPM_DB", old) if old else os.environ.pop("MAXPM_DB", None))
        self.c = core.connect()
        self.addCleanup(self.c.close)
        core.project_add(self.c, "web")
        browser.BROWSERS.clear()
        self.addCleanup(browser.BROWSERS.clear)


class Framing(unittest.TestCase):
    def test_a_frame_reads_back_at_each_length_form(self):
        for n in (0, 5, 125, 126, 300, 70000):
            data = os.urandom(n)
            for mask in (True, False):
                op, fin, got = browser.ws_read(io.BufferedReader(io.BytesIO(browser.ws_frame(0x2, data, mask))))
                self.assertEqual((op, fin, got), (0x2, True, data))

    def test_the_accept_key_of_the_rfc_example(self):
        self.assertEqual(browser.ws_accept("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")


class NetworkRule(unittest.TestCase):
    def test_loopback_and_private_addresses_are_refused(self):
        for host in ("127.0.0.1", "10.1.2.3", "192.168.0.9", "172.20.0.1", "169.254.169.254", "::1", "0.0.0.0"):
            with self.assertRaises(RiverError, msg=host) as e:
                browser.resolve(host, 80, [])
            self.assertIn("browser_allow", str(e.exception))

    def test_a_public_address_passes(self):
        self.assertEqual(browser.resolve("93.184.216.34", 443, []), ["93.184.216.34"])

    def test_browser_allow_names_a_host_or_a_host_and_port(self):
        allow = browser.parse_allow("localhost:5173, 127.0.0.1  10.0.0.5:8080")
        self.assertEqual(allow, ["localhost:5173", "127.0.0.1", "10.0.0.5:8080"])
        self.assertTrue(browser.resolve("127.0.0.1", 9999, allow))
        self.assertTrue(browser.resolve("10.0.0.5", 8080, allow))
        with self.assertRaises(RiverError):
            browser.resolve("10.0.0.5", 8081, allow)

    def test_addresses(self):
        self.assertEqual(browser.norm_url("example.com"), "https://example.com")
        self.assertEqual(browser.norm_url("localhost:3000/x"), "http://localhost:3000/x")
        self.assertEqual(browser.norm_url("about:blank"), "about:blank")
        for bad in ("file:///etc/passwd", "chrome://settings", "javascript:alert(1)"):
            with self.assertRaises(RiverError, msg=bad):
                browser.norm_url(bad)


class Keys(unittest.TestCase):
    def test_enter_sends_a_return(self):
        down, up = browser.key_events("Enter")
        self.assertEqual((down["type"], down["text"], down["windowsVirtualKeyCode"]), ("keyDown", "\r", 13))
        self.assertEqual(up["type"], "keyUp")

    def test_a_shortcut_types_no_text(self):
        self.assertEqual(browser.key_events("a")[0]["text"], "a")
        down = browser.key_events("a", {"meta": True})[0]
        self.assertNotIn("text", down)
        self.assertEqual(down["modifiers"], 4)

    def test_an_unknown_key_is_refused(self):
        with self.assertRaises(RiverError):
            browser.key_events("Hyper")


class Controller(Base):
    def test_one_controller_at_a_time(self):
        core.register(self.c, "ag1"); core.register(self.c, "ag2"); core.register(self.c, "mark", human=True)
        b = browser.get("web")
        browser.may_change(self.c, b, "ag2", False)  # nobody controls it: anyone may
        browser.control(self.c, b, "ag1")
        browser.may_change(self.c, b, "ag1", False)
        with self.assertRaisesRegex(RiverError, "controlled by ag1"):
            browser.may_change(self.c, b, "ag2", False)
        with self.assertRaisesRegex(RiverError, "Take control"):
            browser.may_change(self.c, b, "mark", True)
        with self.assertRaisesRegex(RiverError, "controlled by ag1"):
            browser.control(self.c, b, "ag2")
        browser.control(self.c, b, "ag1", "browser")  # give: the subagent's label, the session keeps it
        self.assertEqual(b.info()["controller"]["sub"], "browser")
        browser.take(self.c, b, "mark")
        browser.may_change(self.c, b, "mark", True)
        with self.assertRaisesRegex(RiverError, "the person \\(mark\\) controls"):
            browser.may_change(self.c, b, "ag1", False)
        browser.give_back(self.c, b, "mark")
        self.assertEqual(b.info()["controller"]["who"], "ag1")
        # The state stays for a restarted serve.
        self.assertEqual(json.loads((b.dir / "state.json").read_text(encoding="utf-8"))["controller"]["who"], "ag1")
        with self.assertRaisesRegex(RiverError, "not by ag2"):
            browser.release(self.c, b, "ag2")
        browser.release(self.c, b, "ag1")
        self.assertIsNone(b.info()["controller"])

    def test_a_gone_session_holds_it_no_more(self):
        core.register(self.c, "ag1"); core.register(self.c, "ag2")
        b = browser.get("web")
        browser.control(self.c, b, "ag1")
        core.unregister(self.c, "ag1")
        browser.may_change(self.c, b, "ag2", False)
        browser.control(self.c, b, "ag2")
        self.assertEqual(b.info()["controller"]["who"], "ag2")

    def test_the_profile_lives_beside_the_queue(self):
        self.assertEqual(browser.browser_dir("my web/app").parent, core.db_path().parent / "browsers")
        self.assertEqual(browser.browser_dir("my web/app").name, "my_web_app")


class SharedBrowserLock(Base):
    """browser_lock and browser_unlock (#5406): serve takes and gives back a shared browser's lock with the control."""

    def setUp(self):
        super().setUp()
        self.calls, self.refuse = [], set()

        def run(cmd, cwd):
            self.calls.append(cmd)
            return (3, f"held by someone else") if cmd in self.refuse else (0, "ok")
        old = browser.LOCK_RUNNER
        browser.LOCK_RUNNER = run
        self.addCleanup(lambda: setattr(browser, "LOCK_RUNNER", old))
        core.config_set(self.c, "browser_lock", "lock {who}", project="web")
        core.config_set(self.c, "browser_unlock", "unlock {who}", project="web")
        core.register(self.c, "ag1"); core.register(self.c, "ag2"); core.register(self.c, "mark", human=True)

    def test_the_lock_follows_the_controller(self):
        b = browser.get("web")
        browser.control(self.c, b, "ag1")
        browser.take(self.c, b, "mark")
        browser.give_back(self.c, b, "mark")
        browser.release(self.c, b, "ag1")
        self.assertEqual(self.calls, ["lock ag1", "unlock ag1", "lock mark", "unlock mark", "lock ag1", "unlock ag1"])

    def test_a_refused_lock_refuses_the_control(self):
        b = browser.get("web")
        self.refuse.add("lock ag2")
        with self.assertRaisesRegex(RiverError, "lock of web refused ag2: held by someone else"):
            browser.control(self.c, b, "ag2")
        self.assertIsNone(b.info()["controller"])
        browser.control(self.c, b, "ag1")
        self.refuse.add("lock mark")
        with self.assertRaisesRegex(RiverError, "refused mark"):
            browser.take(self.c, b, "mark")
        self.assertEqual(b.info()["controller"]["who"], "ag1")  # the session keeps it, and its lock again
        self.assertEqual(self.calls[-1], "lock ag1")

    def test_the_controller_renews_its_lock_while_it_acts(self):
        b = browser.get("web")
        browser.control(self.c, b, "ag1")
        browser.may_change(self.c, b, "ag1", False)
        self.assertEqual(self.calls, ["lock ag1"])  # renewed only after LOCK_RENEW
        b.state["locked"]["at"] -= browser.LOCK_RENEW + 1
        browser.may_change(self.c, b, "ag1", False)
        self.assertEqual(self.calls, ["lock ag1", "lock ag1"])

    def test_a_gone_session_gives_its_lock_back(self):
        b = browser.get("web")
        browser.control(self.c, b, "ag1")
        core.unregister(self.c, "ag1")
        browser.control(self.c, b, "ag2")
        self.assertEqual(self.calls, ["lock ag1", "unlock ag1", "lock ag2"])

    def test_the_settings_are_checked(self):
        self.assertEqual(core.browser_cdp_port("cdp:9339"), 9339)
        self.assertIsNone(core.browser_cdp_port(""))
        for bad in ("9339", "cdp:", "cdp:70000", "http://x"):
            with self.assertRaisesRegex(RiverError, "browser_source is empty"):
                core.config_set(self.c, "browser_source", bad, project="web")
        with self.assertRaisesRegex(RiverError, "names the holder"):
            core.config_set(self.c, "browser_lock", "bash lock.sh", project="web")
        core.config_set(self.c, "browser_source", "cdp:9339", project="web")
        with self.assertRaisesRegex(RiverError, "has its own window"):
            browser.get("web").start(window=True)


class ProjectOf(Base):
    def test_the_project_of_the_held_item_then_the_only_project(self):
        core.register(self.c, "ag1")
        self.assertEqual(browser.project_of(self.c, agent="ag1"), "web")  # one project: that one
        core.project_add(self.c, "api")
        with self.assertRaisesRegex(RiverError, "--project"):
            browser.project_of(self.c, agent="ag1")
        i = core.item_add(self.c, "api", "Build it")["id"]
        core.claim(self.c, i, "ag1")
        self.assertEqual(browser.project_of(self.c, agent="ag1"), "api")
        self.assertEqual(browser.project_of(self.c, project="web", agent="ag1"), "web")
        with self.assertRaises(RiverError):
            browser.project_of(self.c, project="nope")


    def test_a_project_with_a_folder_before_a_deploy_project(self):
        # #5478: a session whose last item was a deploy check got the browser of the deploy project (no folder).
        core.register(self.c, "ag1")
        core.project_add(self.c, "build", path=self.tmp.name)
        core.project_add(self.c, "deploy-prod")
        a = core.item_add(self.c, "build", "Build it")["id"]
        core.claim(self.c, a, "ag1"); core.done(self.c, a, "ag1")
        d = core.item_add(self.c, "deploy-prod", "Check the release")["id"]
        core.claim(self.c, d, "ag1"); core.done(self.c, d, "ag1")
        self.assertEqual(browser.project_of(self.c, agent="ag1"), "build")  # its last item with a folder
        core.claim(self.c, core.item_add(self.c, "deploy-prod", "Check again")["id"], "ag1")
        self.assertEqual(browser.project_of(self.c, agent="ag1"), "build")  # also while it holds a deploy item
        core.register(self.c, "ag2")
        core.claim(self.c, core.item_add(self.c, "deploy-prod", "Only this")["id"], "ag2")
        self.assertEqual(browser.project_of(self.c, agent="ag2"), "deploy-prod")  # no project with a folder: as before

class Command(Base):
    def run_cli(self, *words):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(list(words))
        return out.getvalue()

    def test_screenshot_writes_the_png_and_prints_its_path(self):
        core.register(self.c, "ag1")
        png = b"\x89PNG fake"
        asked = []

        def fake(conn, op, args, actor=None, timeout=5, retries=3):
            asked.append((op, args, actor))
            return {"project": "web", "running": True, "png": base64.b64encode(png).decode()}
        target = os.path.join(self.tmp.name, "s.png")
        with mock.patch.object(cli, "ask_server", fake):
            out = self.run_cli("-q", "--as", "ag1", "browser", "screenshot", "--out", target)
        self.assertEqual(out.strip(), os.path.realpath(target))
        with open(target, "rb") as f:
            self.assertEqual(f.read(), png)
        self.assertEqual((asked[0][0], asked[0][1]["action"], asked[0][2]), ("browser", "screenshot", "ag1"))

    def test_click_needs_a_selector_or_a_point(self):
        core.register(self.c, "ag1")
        with mock.patch.object(cli, "ask_server", lambda *a, **k: {}), \
                self.assertRaisesRegex(RiverError, "name what to click"):
            self.run_cli("-q", "--as", "ag1", "browser", "click")

    def test_status_says_who_controls_it(self):
        core.register(self.c, "ag1")
        res = {"project": "web", "running": True, "window": False, "url": "https://example.com/", "title": "Example",
               "tabs": [], "controller": {"who": "ag1", "kind": "session", "sub": "browser", "since": "now"}}
        with mock.patch.object(cli, "ask_server", lambda *a, **k: res):
            out = self.run_cli("-q", "--as", "ag1", "browser", "status")
        self.assertIn("controlled by ag1 (subagent browser)", out)
        self.assertIn("Example  https://example.com/", out)


class FakeHandler:
    def __init__(self, path):
        self.path = path
        self.sent = None

    def _send(self, code, body, *a, **k):
        self.sent = (code, body)


class Routes(Base):
    def test_the_frames_need_the_page_token(self):
        h = FakeHandler("/api/browser/frame?project=web&after=0")
        routes.frame(h, "/api/browser/frame")
        self.assertEqual(h.sent[0], 403)
        h = FakeHandler(f"/api/browser/frame?project=web&after=0&token={server.PAGE_TOKEN}")
        routes.frame(h, "/api/browser/frame")
        self.assertEqual(h.sent, (200, {"project": "web", "running": False, "seq": 0, "tabs": [], "controller": None}))

    def test_the_ops_and_the_input_token(self):
        self.assertIn("browser", server.OPS)
        self.assertIn("browser_input", server.TYPING_OPS)
        core.register(self.c, "ag1")
        st = server.OPS["browser"](self.c, {"action": "status", "project": "web"}, "ag1")
        self.assertEqual((st["project"], st["running"]), ("web", False))
        server.OPS["browser"](self.c, {"action": "control", "project": "web"}, "ag1")
        core.register(self.c, "ag2")
        with self.assertRaisesRegex(RiverError, "controlled by ag1"):
            server.OPS["browser"](self.c, {"action": "open", "project": "web", "url": "example.com"}, "ag2")
        with self.assertRaisesRegex(RiverError, "not running"):
            server.OPS["browser_input"](self.c, {"project": "web", "ev": {"type": "text", "text": "x"}}, None)

    def test_the_input_of_one_request_goes_in_order(self):
        # #5479: the keys typed while a request runs come in the next one, as a list.
        b = browser.get("web")
        got = []
        with mock.patch.object(b, "running", lambda: True), \
                mock.patch.object(browser, "person_input", lambda b, ev: got.append(ev) or {"ok": len(got)}):
            out = server.OPS["browser_input"](self.c, {"project": "web", "evs": [
                {"type": "key", "key": "h"}, {"type": "key", "key": "i"}, {"type": "key", "key": "Enter"}]}, None)
            server.OPS["browser_input"](self.c, {"project": "web", "ev": {"type": "text", "text": "x"}}, None)
        self.assertEqual([e.get("key") or e.get("text") for e in got], ["h", "i", "Enter", "x"])
        self.assertEqual(out, {"ok": 3})

    def test_a_session_open_asks_the_page_to_show_it(self):
        routes._ask_open("web", "ag1", "ag1")
        self.assertEqual(routes.browser_opens()[-1], {"seq": routes._open_seq[0], "project": "web", "session": "ag1",
                                                      "by": "ag1"})


def _can_bind():
    try:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.close()
        return True
    except OSError:
        return False


class RealChrome(Base):
    """One real headless Chrome: a page through the proxy, the text, a click, typing, and a blocked address."""

    def test_open_read_click_type_and_the_network_rule(self):
        if not chrome() or not browser.chrome_path():  # the browser of serve starts browser.chrome_path()
            self.skipTest("no Chrome on this computer")
        if not _can_bind():
            self.skipTest("this sandbox does not allow a local port")
        hits = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                body = (b"<title>Local</title><input id=q><button id=b onclick=\"document.title='clicked '+"
                        b"document.getElementById('q').value\">Go</button>")
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        b = browser.get("web")
        self.addCleanup(b.close)
        with self.assertRaisesRegex(RiverError, "loopback or private"):
            b.open(f"http://127.0.0.1:{port}/")
        self.assertEqual(hits, [])
        core.config_set(self.c, "browser_allow", f"127.0.0.1:{port}", project="web")
        b.resize(640, 480)
        info = b.open(f"http://127.0.0.1:{port}/page")
        self.assertEqual((info["title"], [h for h in hits if h != "/favicon.ico"]), ("Local", ["/page"]))
        browser.type_text(b, "hello", "#q")
        browser.click(b, "#b")
        self.assertEqual(b.evaluate("document.title"), "clicked hello")
        self.assertTrue(base64.b64decode(browser.screenshot(b)["png"]).startswith(b"\x89PNG"))
        f = browser.frame(b, 0, 5)
        self.assertEqual((f["frame"]["w"], f["frame"]["h"]), (640, 480))
        # A page of an allowed address still cannot reach another loopback port through the proxy.
        core.config_unset(self.c, "browser_allow", project="web")
        got = b.evaluate(f"fetch('http://127.0.0.1:{port}/sneak').then(r => 'got ' + r.status).catch(e => 'ERR')")
        self.assertIn(got, ("ERR", "got 403"))  # the proxy's refusal, or Chrome's own
        self.assertNotIn("/sneak", hits)
        browser.person_input(b, {"type": "key", "key": "Tab"})


class AttachedChrome(Base):
    """browser_source cdp:<port> (#5406): attach to a Chrome that runs already, use only serve's own tab, leave it open."""

    @staticmethod
    def _stop_chrome(p, prof):
        """Stop the shared Chrome with its helper processes (its own process group), and remove its profile: a
        helper still writes there for a moment, and the temporary folder's removal failed on it ('Directory not
        empty: Default', on Linux)."""
        import shutil
        import signal
        import time
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.wait()
        for _ in range(50):
            shutil.rmtree(prof, ignore_errors=True)
            if not os.path.exists(prof):
                break
            time.sleep(0.1)

    def test_attach_uses_its_own_tab_and_leaves_the_browser_open(self):
        chrome_ = chrome() and browser.chrome_path()
        if not chrome_:
            self.skipTest("no Chrome on this computer")
        if not _can_bind():
            self.skipTest("this sandbox does not allow a local port")
        import subprocess, time
        prof = os.path.join(self.tmp.name, "shared")
        p = subprocess.Popen([chrome_, "--headless=new", f"--user-data-dir={prof}", "--remote-debugging-port=0",
                              "--no-first-run", "data:text/html,<title>Theirs</title>"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(self._stop_chrome, p, prof)
        port = None
        for _ in range(100):
            try:
                port = int(Path(prof, "DevToolsActivePort").read_text(encoding="utf-8").split()[0])
                break
            except (OSError, ValueError, IndexError):
                time.sleep(0.1)
        self.assertTrue(port, "the shared Chrome did not start")
        core.config_set(self.c, "browser_source", f"cdp:{port}", project="web")
        b = browser.get("web")
        b.start()
        info = b.info()
        self.assertEqual(info["source"], f"cdp:{port}")
        self.assertEqual(len(info["tabs"]), 1)  # its own tab; the other tab stays out of it
        self.assertNotIn("Theirs", [t["title"] for t in info["tabs"]])
        own = info["tabs"][0]["id"]
        b.evaluate("document.title = 'Mine'")
        self.assertEqual(b.evaluate("document.title"), "Mine")
        browser.new_tab(b)
        self.assertEqual(len(b.info()["tabs"]), 2)
        b.close()
        self.assertIsNone(p.poll())  # serve leaves the shared Chrome running
        self.assertTrue(b._version(port)["Browser"])
        b.start()  # serve restarted: the same own tab again
        self.assertIn(b.info()["tabs"][0]["id"], (own, b.state["tab"]))
        b.close()


if __name__ == "__main__":
    unittest.main()


class CdpAddress(Base):
    """A session's own CDP address for the Playwright MCP tools (#5405): the token, the checks of each message, the
    relay, the route, and the MCP config in the launch command."""

    def test_the_token_is_one_for_each_session_and_project_and_stays(self):
        t = browser.cdp_token("web", "ag1")
        self.assertEqual(t, browser.cdp_token("web", "ag1"))
        self.assertNotEqual(t, browser.cdp_token("web", "ag2"))
        self.assertNotEqual(t, browser.cdp_token("shop", "ag1"))
        key = core.db_path().parent / "browsers" / "cdp.key"
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertIn(f"/cdp/ag1?project=web&token={t}", browser.cdp_url(self.c, "web", "ag1"))

    def test_each_message_is_checked_before_chrome_gets_it(self):
        core.register(self.c, "ag1"); core.register(self.c, "ag2"); core.register(self.c, "mark", human=True)
        b = browser.get("web")
        no = lambda m, p=None, who="ag1": browser.cdp_refusal(self.c, b, who, {"id": 1, "method": m, "params": p or {}})
        for m in ("Browser.close", "Tethering.bind", "Network.setBlockedURLs", "Target.exposeDevToolsProtocol"):
            self.assertIn("not allowed", no(m))
        self.assertIn("not file:", no("Page.navigate", {"url": "file:///etc/passwd"}))
        self.assertIn("not chrome:", no("Target.createTarget", {"url": "chrome://settings"}))
        self.assertIsNone(no("Page.navigate", {"url": "https://example.com"}))
        self.assertIsNone(no("Target.createTarget", {"url": "about:blank"}))
        # Downloads stay in the project's browser folder; an upload takes no file from elsewhere.
        msg = {"id": 2, "method": "Browser.setDownloadBehavior", "params": {"behavior": "allow", "downloadPath": "/tmp/x"}}
        self.assertIsNone(browser.cdp_refusal(self.c, b, "ag1", msg))
        self.assertEqual(msg["params"]["downloadPath"], str(b.dir / "downloads"))
        (b.dir / "downloads").mkdir(parents=True)
        (b.dir / "downloads" / "a.pdf").write_text("x", encoding="utf-8")
        self.assertIsNone(no("DOM.setFileInputFiles", {"files": [str(b.dir / "downloads" / "a.pdf")]}))
        self.assertIn("project folder", no("DOM.setFileInputFiles", {"files": ["/etc/hosts"]}))
        # The controller rule: changes wait for control; reads pass.
        browser.take(self.c, b, "mark")
        self.assertIn("the person (mark) controls", no("Input.dispatchMouseEvent", {"type": "mousePressed"}))
        self.assertIn("the person (mark) controls", no("Runtime.callFunctionOn"))
        self.assertIsNone(no("Page.captureScreenshot"))
        self.assertIsNone(no("Accessibility.getFullAXTree"))
        browser.give_back(self.c, b, "mark")
        browser.control(self.c, b, "ag1")
        self.assertIsNone(no("Input.dispatchKeyEvent"))
        self.assertIn("controlled by ag1", no("Page.navigate", {"url": "https://example.com"}, who="ag2"))

    def test_scripts_in_an_isolated_world_are_reads(self):
        # #5460: Playwright runs its reads (browser_snapshot) as scripts in its utility world; while the person
        # controls they pass, and a script in the page's own world (page.evaluate) still waits for control.
        core.register(self.c, "ag1"); core.register(self.c, "mark", human=True)
        b = browser.get("web")
        worlds = set()
        for m in ({"method": "Runtime.executionContextCreated", "sessionId": "S", "params": {"context": {
                      "id": 7, "uniqueId": "u7", "name": "__playwright_utility_world__", "auxData": {"type": "isolated"}}}},
                  {"method": "Runtime.executionContextCreated", "sessionId": "S", "params": {"context": {
                      "id": 1, "name": "", "auxData": {"type": "default", "isDefault": True}}}}):
            browser.track_worlds(worlds, m)
        self.assertEqual(worlds, {("S", 7), ("S", "u:u7")})
        no = lambda m, p, sid="S": browser.cdp_refusal(self.c, b, "ag1", {"id": 1, "method": m, "params": p, "sessionId": sid}, worlds)
        browser.take(self.c, b, "mark")
        self.assertIsNone(no("Runtime.callFunctionOn", {"executionContextId": 7, "functionDeclaration": "() => 1"}))
        self.assertIsNone(no("Runtime.evaluate", {"contextId": 7, "expression": "1"}))
        self.assertIsNone(no("Runtime.evaluate", {"uniqueContextId": "u7", "expression": "1"}))
        self.assertIsNone(no("Runtime.callFunctionOn", {"objectId": "-4211.7.12", "functionDeclaration": "() => 1"}))
        self.assertIsNone(no("Runtime.callFunctionOn", {"objectId": '{"injectedScriptId":7,"id":3}'}))
        for p in ({"expression": "1"}, {"contextId": 1, "expression": "1"}, {"objectId": "-4211.1.12"}, {"objectId": "x"}):
            self.assertIn("the person (mark) controls", no("Runtime.evaluate", p), p)
        self.assertIn("the person (mark) controls", no("Runtime.evaluate", {"contextId": 7}, sid="T"), "another target")
        self.assertIn("the person (mark) controls", no("Input.dispatchMouseEvent", {"type": "mousePressed"}))
        browser.track_worlds(worlds, {"method": "Runtime.executionContextDestroyed", "sessionId": "S",
                                      "params": {"executionContextId": 7, "executionContextUniqueId": "u7"}})
        self.assertIn("the person (mark) controls", no("Runtime.evaluate", {"contextId": 7}))
        browser.track_worlds(worlds, {"method": "Runtime.executionContextCreated", "sessionId": "S", "params": {
            "context": {"id": 9, "auxData": {"type": "isolated"}}}})
        browser.track_worlds(worlds, {"method": "Runtime.executionContextsCleared", "sessionId": "S", "params": {}})
        self.assertEqual(worlds, set())

    def test_the_relay_passes_what_is_allowed_and_answers_a_refusal_itself(self):
        import queue
        b = browser.get("web")

        class Upstream:
            def __init__(self):
                self.sent, self.events, self.closed = [], queue.Queue(), False

            def send(self, text):
                self.sent.append(json.loads(text))
                self.events.put(json.dumps({"id": json.loads(text)["id"], "result": {"ok": True}}))

            def recv(self):
                got = self.events.get()
                if got is None:
                    raise EOFError
                return got

            def close(self):
                self.closed = True
                self.events.put(None)

        up = Upstream()
        mine, theirs = socket.socketpair()
        self.addCleanup(mine.close)
        self.addCleanup(theirs.close)
        t = threading.Thread(target=browser.cdp_session, args=(self.c, b, "ag1", theirs.makefile("rb"), theirs, up))
        t.start()
        rf = mine.makefile("rb")
        mine.sendall(browser.ws_frame(0x1, json.dumps({"id": 1, "method": "Page.captureScreenshot"}).encode()))
        self.assertEqual(json.loads(browser.ws_read(rf)[2]), {"id": 1, "result": {"ok": True}})
        mine.sendall(browser.ws_frame(0x1, json.dumps({"id": 2, "method": "Browser.close", "sessionId": "S"}).encode()))
        reply = json.loads(browser.ws_read(rf)[2])
        self.assertEqual((reply["id"], reply["sessionId"]), (2, "S"))
        self.assertIn("maxpm: Browser.close is not allowed", reply["error"]["message"])
        up.events.put(json.dumps({"method": "Page.loadEventFired", "params": {}}))
        self.assertEqual(json.loads(browser.ws_read(rf)[2])["method"], "Page.loadEventFired")
        mine.sendall(browser.ws_frame(0x8, b""))
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual([m["method"] for m in up.sent], ["Page.captureScreenshot"])
        self.assertTrue(up.closed)

    def test_the_route_needs_the_token_and_a_websocket(self):
        h = FakeHandler("/cdp/ag1?project=web&token=nope")
        h.headers = {"Upgrade": "websocket", "Sec-WebSocket-Key": "x"}
        routes.cdp(h, "/cdp/ag1")
        self.assertEqual(h.sent[0], 403)
        h = FakeHandler(f"/cdp/ag1?project=web&token={browser.cdp_token('web', 'ag1')}")
        h.headers = {}
        routes.cdp(h, "/cdp/ag1")
        self.assertEqual(h.sent[0], 400)
        self.assertTrue(any(r[0]("/cdp/ag1") and r[1] is routes.cdp for r in routes.GET))

    def test_browser_mcp_puts_playwright_mcp_into_the_launch_command(self):
        pid = core._project(self.c, "web")["id"]
        self.assertNotIn("--mcp-config", core._launch_agent_cmd(self.c, pid, None, name="web-1a2b")["command"])
        with self.assertRaisesRegex(RiverError, "browser_mcp"):
            core.config_set(self.c, "browser_mcp", "npx 'open", project="web")
        core.config_set(self.c, "browser_mcp", "on", project="web")
        cmd = core._launch_agent_cmd(self.c, pid, None, name="web-1a2b")["command"]
        path = browser.browser_dir("web") / "mcp" / "web-1a2b.json"
        self.assertIn(f"--mcp-config {path}", cmd)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        server_cfg = json.loads(path.read_text(encoding="utf-8"))["mcpServers"][browser.MCP_NAME]
        self.assertEqual(server_cfg["command"], "npx")
        self.assertEqual(server_cfg["args"][:3], ["-y", "@playwright/mcp@latest", "--cdp-endpoint"])
        self.assertEqual(server_cfg["args"][3], browser.cdp_url(self.c, "web", "web-1a2b"))
        core.config_set(self.c, "browser_mcp", "npx -y @playwright/mcp@0.0.40", project="web")
        core._launch_agent_cmd(self.c, pid, None, name="web-1a2b")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["mcpServers"][browser.MCP_NAME]["args"][1], "@playwright/mcp@0.0.40")

    def test_a_real_chrome_through_the_cdp_address_of_serve(self):
        if not chrome() or not browser.chrome_path():  # the browser of serve starts browser.chrome_path()
            self.skipTest("no Chrome on this computer")
        if not _can_bind():
            self.skipTest("this sandbox does not allow a local port")
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        core.config_set(self.c, "serve_port", str(srv.server_address[1]))
        b = browser.get("web")
        self.addCleanup(b.close)
        with self.assertRaisesRegex(OSError, "403"):
            browser.WS(browser.cdp_url(self.c, "web", "ag1").replace("token=", "token=x"))
        ws = browser.WS(browser.cdp_url(self.c, "web", "ag1"))
        self.addCleanup(ws.close)
        ws.send(json.dumps({"id": 1, "method": "Browser.getVersion"}))
        self.assertIn("Chrome", json.loads(ws.recv())["result"]["product"])
        ws.send(json.dumps({"id": 2, "method": "Browser.close"}))
        self.assertIn("not allowed", json.loads(ws.recv())["error"]["message"])
        ws.send(json.dumps({"id": 3, "method": "Target.getTargets"}))
        got = json.loads(ws.recv())
        while "id" not in got:  # an event before the answer
            got = json.loads(ws.recv())
        self.assertTrue(any(t["type"] == "page" for t in got["result"]["targetInfos"]))
        self.assertTrue(b.running())  # Browser.close never reached Chrome

    def test_a_real_chrome_runs_reads_of_an_isolated_world_while_the_person_controls(self):
        # #5460: what Playwright does for browser_snapshot, by hand: an isolated world of the page, then scripts.
        if not chrome() or not browser.chrome_path():  # the browser of serve starts browser.chrome_path()
            self.skipTest("no Chrome on this computer")
        if not _can_bind():
            self.skipTest("this sandbox does not allow a local port")
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        core.config_set(self.c, "serve_port", str(srv.server_address[1]))
        core.register(self.c, "ag1"); core.register(self.c, "mark", human=True)
        b = browser.get("web")
        self.addCleanup(b.close)
        ws = browser.WS(browser.cdp_url(self.c, "web", "ag1"))
        self.addCleanup(ws.close)
        n = iter(range(1, 100))

        def call(method, params=None, sid=None):
            i = next(n)
            ws.send(json.dumps({"id": i, "method": method, "params": params or {}, **({"sessionId": sid} if sid else {})}))
            while True:
                got = json.loads(ws.recv())
                if got.get("id") == i:
                    return got
        page = next(t for t in call("Target.getTargets")["result"]["targetInfos"] if t["type"] == "page")
        sid = call("Target.attachToTarget", {"targetId": page["targetId"], "flatten": True})["result"]["sessionId"]
        call("Runtime.enable", sid=sid)
        frame = call("Page.getFrameTree", sid=sid)["result"]["frameTree"]["frame"]["id"]
        iso = call("Page.createIsolatedWorld", {"frameId": frame, "worldName": "__playwright_utility_world__"},
                   sid=sid)["result"]["executionContextId"]
        browser.take(self.c, b, "mark")
        got = call("Runtime.evaluate", {"contextId": iso, "expression": "document.title.length + 2", "returnByValue": True}, sid=sid)
        self.assertEqual(got.get("result", {}).get("result", {}).get("value"), 2, got)
        handle = call("Runtime.evaluate", {"contextId": iso, "expression": "({a: 1})"}, sid=sid)["result"]["result"]["objectId"]
        got = call("Runtime.callFunctionOn", {"objectId": handle, "functionDeclaration": "function () { return this.a; }",
                                              "returnByValue": True}, sid=sid)
        self.assertEqual(got.get("result", {}).get("result", {}).get("value"), 1, f"an objectId of the world: {handle} {got}")
        got = call("Runtime.evaluate", {"expression": "1"}, sid=sid)
        self.assertIn("the person (mark) controls", got["error"]["message"])
