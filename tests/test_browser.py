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
        self.assertEqual(json.loads((b.dir / "state.json").read_text())["controller"]["who"], "ag1")
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
        if not browser.chrome_path():
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

    def test_attach_uses_its_own_tab_and_leaves_the_browser_open(self):
        chrome = browser.chrome_path()
        if not chrome:
            self.skipTest("no Chrome on this computer")
        if not _can_bind():
            self.skipTest("this sandbox does not allow a local port")
        import subprocess, time
        prof = os.path.join(self.tmp.name, "shared")
        p = subprocess.Popen([chrome, "--headless=new", f"--user-data-dir={prof}", "--remote-debugging-port=0",
                              "--no-first-run", "data:text/html,<title>Theirs</title>"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(lambda: (p.kill(), p.wait()))
        port = None
        for _ in range(100):
            try:
                port = int(Path(prof, "DevToolsActivePort").read_text().split()[0])
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
