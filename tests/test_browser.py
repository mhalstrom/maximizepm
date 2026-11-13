"""The browser of a project in the side panel (#4631): the WebSocket framing, the network rule, the keys, the
controller rule, the project of a command, maxpm browser, the page's routes, and one real Chrome when there is one."""

import base64
import contextlib
import http.server
import io
import json
import os
import socket
import struct
import tempfile
import threading
import time
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

    def test_words_are_a_search(self):
        """#5635: words with a space, or one word with no dot that is no host and port, search Google, as in Chrome."""
        g = "https://www.google.com/search?q="
        self.assertEqual(browser.norm_url("two words"), g + "two+words")
        self.assertEqual(browser.norm_url("python"), g + "python")
        self.assertEqual(browser.norm_url("what is 3.5 & more"), g + "what+is+3.5+%26+more")
        self.assertEqual(browser.norm_url("news.ycombinator.com"), "https://news.ycombinator.com")
        self.assertEqual(browser.norm_url("localhost"), "http://localhost")
        self.assertEqual(browser.norm_url("my-box:8080/x"), "https://my-box:8080/x")
        self.assertEqual(browser.norm_url("[::1]:3000"), "http://[::1]:3000")
        self.assertEqual(browser.norm_url("https://example.com/a?q=1"), "https://example.com/a?q=1")


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

    def test_a_session_takes_it_when_the_person_asked_or_left_it(self):
        """#5629: --take (and a Playwright input) takes the browser from the person when the person's newest message
        in the session's chat is newer than their last input in it, or they have not used it for browser_take_after."""
        core.register(self.c, "ag1"); core.register(self.c, "mark", human=True)
        it = core.item_add(self.c, "web", "Click the story", actor="mark")["id"]
        core.claim(self.c, it, "ag1")
        b = browser.get("web")
        browser.take(self.c, b, "mark")  # mark solves a CAPTCHA in the panel
        with self.assertRaisesRegex(RiverError, "control --take"):
            browser.control(self.c, b, "ag1")
        with self.assertRaisesRegex(RiverError, "used it 0s ago, with no message in your chat since"):
            browser.control(self.c, b, "ag1", take=True)
        with self.assertRaisesRegex(RiverError, "no message in your chat since"):  # a Playwright click: refused too
            browser.may_change(self.c, b, "ag1", False, take=True)
        b.person_at -= 5  # his last click in the panel, five seconds before he writes
        b.state["controller"]["since"] = core.iso(core.now() - __import__("datetime").timedelta(seconds=5))
        # mark asks in ag1's chat: its prompt hook records the time, and the take succeeds.
        core.activity_event(self.c, {"hook_event_name": "UserPromptSubmit", "prompt": "click the 14th story"},
                            {"MAXPM_AGENT": "ag1"})
        info = browser.control(self.c, b, "ag1", take=True)
        self.assertEqual((info["controller"]["who"], info["controller"]["took"], info["controller"]["from"]),
                         ("ag1", "you asked in its chat", "mark"))
        self.assertTrue(any("took the browser of web from mark (you asked in its chat)" in r[0] for r in
                            self.c.execute("SELECT change FROM events WHERE item_id=? AND actor='ag1'", (it,))))
        # mark takes it back and types: his input wins again until he asks or leaves it for 2 minutes.
        browser.take(self.c, b, "mark")
        self.assertNotIn("took", b.info()["controller"])
        with self.assertRaisesRegex(RiverError, "no message in your chat since"):
            browser.may_change(self.c, b, "ag1", False, take=True)
        self.c.execute("UPDATE agents SET person_said_at=NULL WHERE name='ag1'")
        b.person_at -= 121
        b.state["controller"]["since"] = core.iso(core.now() - __import__("datetime").timedelta(seconds=121))
        browser.may_change(self.c, b, "ag1", False, take=True)  # the Playwright click takes it
        self.assertEqual(b.info()["controller"]["took"], "you had not used it for 2m")
        core.config_set(self.c, "browser_take_after", "10m")
        browser.take(self.c, b, "mark")
        with self.assertRaisesRegex(RiverError, "wait until they have not used it for 10m"):
            browser.control(self.c, b, "ag1", take=True)

    def test_a_gone_session_holds_it_no_more(self):
        core.register(self.c, "ag1"); core.register(self.c, "ag2")
        b = browser.get("web")
        browser.control(self.c, b, "ag1")
        core.unregister(self.c, "ag1")
        browser.may_change(self.c, b, "ag2", False)
        browser.control(self.c, b, "ag2")
        self.assertEqual(b.info()["controller"]["who"], "ag2")

    def test_headless_chrome_gets_the_user_agent_of_a_window(self):
        """#5619: HeadlessChrome in the user agent makes Google's sign-in refuse; the flag gives Chrome's own."""
        out = mock.Mock(stdout="Google Chrome 157.0.8103.52 \n")
        with mock.patch.object(browser.subprocess, "run", return_value=out):
            ua = browser.user_agent("chrome")
        self.assertIn("Chrome/157.0.0.0 Safari/537.36", ua)
        self.assertNotIn("Headless", ua)
        with mock.patch.object(browser.subprocess, "run", side_effect=OSError):
            self.assertIsNone(browser.user_agent("chrome"))

    def test_a_refused_page_and_a_popup_that_closes(self):
        """#5619: the panel says when a site refuses a browser with no window; a closed popup shows its opener."""
        b = browser.get("web")
        b.targets = {"a": {"url": "https://example.com/", "title": "", "opener": None},
                     "p": {"url": "https://accounts.google.com/v3/signin/rejected?x=1", "title": "", "opener": "a"}}
        b.target = "p"
        self.assertTrue(b.info()["refused"])
        b.target = "a"
        self.assertFalse(b.info()["refused"])
        b.target = "p"
        with mock.patch.object(browser.threading, "Thread") as th:
            b._event("Target.targetDestroyed", {"targetId": "p"}, None)
        self.assertEqual(th.call_args.kwargs["args"], ("a",))

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
        with self.assertRaisesRegex(RiverError, "names the mode"):
            core.config_set(self.c, "browser_mode", "bash scripts/test-browser.sh window", project="web")
        core.config_set(self.c, "browser_source", "cdp:9339", project="web")
        self._modes(lambda port: False)
        # A start with a mode refuses before it connects (no test reaches the person's browser on 9339).
        with mock.patch.object(browser.Browser, "_version", side_effect=AssertionError("connected")):
            with self.assertRaisesRegex(RiverError, "only through the setting browser_mode"):
                browser.get("web").start(window=True)

    def _modes(self, fn):
        old = browser.MODE_RUNNER
        browser.MODE_RUNNER = fn
        self.addCleanup(lambda: setattr(browser, "MODE_RUNNER", old))

    def _attached(self, window=False):
        """An attached browser (browser_source cdp:9339) with no Chrome: start and stop are fakes."""
        core.config_set(self.c, "browser_source", "cdp:9339", project="web")
        b = browser.get("web")
        b.attached, b.attached_window = 9339, window
        b.ws = object()
        starts = []

        def start(window=None):
            b.attached, b.ws = 9339, object()
            b.attached_window = "window" in self.calls[-1] if self.calls else window
            starts.append(window)

        def stop():
            b.ws, b.attached = None, None
        b.start, b._stop_chrome = start, stop
        return b, starts

    def test_open_as_a_window_runs_browser_mode_under_the_lock(self):
        """#5619: an attached Chrome opens as a window and goes back headless through browser_mode, under its lock."""
        b, starts = self._attached()
        with self.assertRaisesRegex(RiverError, "maxpm config set browser_mode"):
            browser.attached_mode(self.c, b, "mark", True)
        self.assertEqual(self.calls, ["lock mark", "unlock mark"])  # the lock goes back after the refusal too
        core.config_set(self.c, "browser_mode", "mode {mode}", project="web")
        self.calls.clear()
        info = browser.attached_mode(self.c, b, "mark", True)
        self.assertEqual(self.calls, ["lock mark", "mode window", "unlock mark"])
        self.assertTrue(info["window"])
        self.calls.clear()
        browser.attached_mode(self.c, b, "mark", True)  # the mode it has already: nothing runs
        self.assertEqual(self.calls, [])
        browser.attached_mode(self.c, b, "mark", False)
        self.assertEqual(self.calls, ["lock mark", "mode headless", "unlock mark"])
        self.assertFalse(b.info()["window"])

    def test_a_mode_change_waits_for_another_holder_of_the_lock(self):
        b, _ = self._attached()
        core.config_set(self.c, "browser_mode", "mode {mode}", project="web")
        browser.control(self.c, b, "ag1")
        self.calls.clear()
        with self.assertRaisesRegex(RiverError, "ag1 holds the lock .* press Take control first"):
            browser.attached_mode(self.c, b, "mark", True)
        self.assertEqual(self.calls, [])
        browser.take(self.c, b, "mark")  # the person holds it: the mode changes and the lock stays the person's
        self.calls.clear()
        browser.attached_mode(self.c, b, "mark", True)
        self.assertEqual(self.calls, ["mode window"])
        self.assertEqual(b.state["locked"]["who"], "mark")

    def test_the_panel_offers_a_window_only_with_browser_mode(self):
        b, _ = self._attached()
        self.assertFalse(b.info()["modes"])
        b.modes = True
        self.assertTrue(b.info()["modes"])
        self.assertTrue(browser.Browser("other").info()["modes"])  # serve's own Chrome always can

    def test_the_window_route_uses_browser_mode_for_an_attached_chrome(self):
        b, _ = self._attached()
        with mock.patch.object(browser, "attached_mode", return_value={}) as m:
            routes.act(self.c, {"action": "window", "project": "web", "window": True}, "mark")
        m.assert_called_once()
        self.assertEqual(m.call_args.args[2:], ("mark", True))

    def test_a_window_goes_back_into_the_panel_by_itself(self):
        """#5650: a browser shown as a window goes back headless when the person closes the window, and after
        browser_window_idle with no input in it and no change of its page; the panel says who opened it and why it
        came back."""
        import datetime
        # serve's own Chrome: the person closes the window; the next start is headless, not a new window.
        b = browser.get("web", "ag1")
        b.state["window"] = True
        browser.mark_window(b, "mark", True, True)

        class Closed:
            def recv(self):
                raise EOFError
        b.ws = ws = Closed()
        b._reader(ws)
        self.assertEqual((b.state["window"], b.state["window_end"]["why"]), (False, "the window was closed"))
        self.assertNotIn("window_by", b.state)
        # No input for browser_window_idle (10m): it starts again with no window.
        b2 = browser.get("web", "ag2")
        b2.ws, b2.state["window"], starts = object(), True, []
        b2.start = lambda window=None: starts.append(window)
        browser.mark_window(b2, "ag2", False, True)
        info = b2.info()
        self.assertEqual((info["window_by"]["who"], info["window_idle"]), ("ag2", "10m"))
        b2.state["window_by"]["at"] = core.iso(core.now() - datetime.timedelta(minutes=11))
        b2.active_at = b2.person_at = time.time() - 300
        self.assertEqual(browser.window_sweep(self.c), [])  # a change of its page 5 minutes ago
        b2.active_at = b2.person_at = time.time() - 700
        self.assertEqual(browser.window_sweep(self.c), [("web", "ag2", "no input in the window for 11m")])
        self.assertEqual((starts, b2.state["window_end"]["why"]), ([False], "no input in the window for 11m"))
        # An attached Chrome whose window was closed: browser_mode starts it again headless.
        a, _ = self._attached(window=True)
        core.config_set(self.c, "browser_mode", "mode {mode}", project="web")
        a.ws = None
        self.calls.clear()
        self.assertIn(("web", None, "the window was closed"), browser.window_sweep(self.c))
        self.assertIn("mode headless", self.calls)

    def test_a_session_opens_a_window_only_when_the_person_asked_in_its_chat(self):
        b = browser.get("web", "ag1")
        starts = []
        b.start = lambda window=None: starts.append(window)
        with self.assertRaisesRegex(RiverError, "only when the person asked for it in its chat"):
            routes.act(self.c, {"action": "window", "project": "web", "window": True}, "ag1")
        core.activity_event(self.c, {"hook_event_name": "UserPromptSubmit", "prompt": "open it as a window"},
                            {"MAXPM_AGENT": "ag1"})
        info = routes.act(self.c, {"action": "window", "project": "web", "window": True}, "ag1")
        self.assertEqual((starts, b.state["window_by"]["who"], b.state["window_by"]["person"]), ([True], "ag1", False))
        routes.act(self.c, {"action": "window", "project": "web", "window": False}, "ag1")  # back: always
        self.assertNotIn("window_by", b.state)
        self.assertIsNone(info["window_by"])  # the fake start opened no window


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
        self.assertFalse(asked[0][1]["cursor"])
        err = io.StringIO()
        with mock.patch.object(cli, "ask_server", fake), contextlib.redirect_stderr(err):
            self.run_cli("-q", "--as", "ag1", "browser", "screenshot", "--cursor", "--out", target)
        self.assertTrue(asked[1][1]["cursor"])
        self.assertIn("no session pointed in this tab", err.getvalue())  # the fake drew no pointer

    def test_click_needs_a_selector_or_a_point(self):
        core.register(self.c, "ag1")
        with mock.patch.object(cli, "ask_server", lambda *a, **k: {}), \
                self.assertRaisesRegex(RiverError, "name what to click"):
            self.run_cli("-q", "--as", "ag1", "browser", "click")

    def asked(self, *words, res=None):
        """The arguments that maxpm browser sends to serve, and what it prints for res."""
        core.register(self.c, "ag1")
        got = []

        def fake(conn, op, args, actor=None, timeout=5, retries=3):
            got.append(args)
            return res or {"project": "web", "url": "https://x.test/", "title": "X"}
        with mock.patch.object(cli, "ask_server", fake):
            out = self.run_cli("-q", "--as", "ag1", "browser", *words)
        return got[0], out

    def test_click_takes_a_ref_a_selector_or_a_point(self):
        self.assertEqual(self.asked("click", "@e3")[0]["selector"], "@e3")
        self.assertEqual(self.asked("click", "#go")[0]["selector"], "#go")
        a = self.asked("click", "10", "20.5")[0]
        self.assertEqual((a["x"], a["y"], a.get("selector")), (10.0, 20.5, None))
        self.assertEqual(self.asked("click", "--selector", "a.more")[0]["selector"], "a.more")
        with mock.patch.object(cli, "ask_server", lambda *a, **k: {}), self.assertRaisesRegex(RiverError, "x and y"):
            self.run_cli("-q", "--as", "ag1", "browser", "click", "10")

    def test_type_fill_select_check_and_the_wait(self):
        a, out = self.asked("type", "@e5", "two words", "--wait", "load", "--snapshot")
        self.assertEqual((a["action"], a["selector"], a["text"], a["wait"], a["snapshot"]),
                         ("type", "@e5", "two words", "load", True))
        self.assertEqual(self.asked("type", "hi")[0].get("selector"), None)
        self.assertEqual(self.asked("fill", "@e5", "x")[0], {"action": "fill", "selector": "@e5", "text": "x",
                                                            "project": None, "cwd": os.getcwd()})
        self.assertEqual(self.asked("select", "@e7", "Small", "Large")[0]["values"], ["Small", "Large"])
        self.assertEqual(self.asked("uncheck", "@e8")[0]["action"], "uncheck")
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.asked("click", "@e3", "--wait", "soon")

    def test_an_action_prints_one_line_and_its_snapshot(self):
        res = {"project": "web", "url": "https://x.test/two", "title": "Two", "navigated": True,
               "snapshot": {"lines": ['@e9 link "Back"'], "more": 0, "frames": 0, "url": "", "title": ""}}
        _, out = self.asked("click", "@e3", "--snapshot", res=res)
        self.assertEqual(out.splitlines(), ["new page: Two  https://x.test/two", '@e9 link "Back"'])
        _, out = self.asked("check", "@e3", res={"title": "X", "url": "u", "checked": True})
        self.assertEqual(out.strip(), "X  u  (checked)")

    def test_snapshot_prints_refs_a_delta_and_what_it_left_out(self):
        snap = {"lines": ["# Sign up", '@e1 textbox "Name" value="Bob"'], "more": 3, "frames": 1,
                "url": "https://x.test/", "title": "Form"}
        a, out = self.asked("snapshot", "-i", "--scope", "form", res={"snapshot": snap})
        self.assertEqual((a["interactive"], a["scope"], a["delta"]), (True, "form", False))
        self.assertEqual(out.splitlines(), [
            "page: Form  https://x.test/", "# Sign up", '@e1 textbox "Name" value="Bob"',
            "... 3 more: --scope '<css>' for a part of the page, or --max <n>",
            "(1 frame not listed: open the frame's address)"])
        _, out = self.asked("snapshot", "--delta", res={"snapshot": {**snap, "delta": True, "lines": [], "more": 0,
                                                                     "frames": 0, "removed": ["@e2", "@e4"]}})
        self.assertEqual(out.splitlines()[1:], ["(no new or changed element)", "gone: @e2 @e4"])

    def test_batch_sends_the_lines_as_one_request(self):
        f = os.path.join(self.tmp.name, "steps")
        Path(f).write_text("# a note\nclick @e3\n\nmaxpm browser fill @e5 'two words'\nkey Enter --wait load\n"
                           "snapshot -i\n", encoding="utf-8")
        steps = [{"step": {"action": "click", "selector": "@e3"}, "result": {"title": "A", "url": "a"}},
                 {"step": {"action": "fill", "selector": "@e5"}, "error": "@e5 is no longer on the page"}]
        a, out = self.asked("batch", "--file", f, res={"steps": steps, "ok": True})
        self.assertEqual([s["action"] for s in a["steps"]], ["click", "fill", "key", "snapshot"])
        self.assertEqual((a["steps"][1]["text"], a["steps"][2]["wait"]), ("two words", "load"))
        self.assertEqual(out.splitlines(), ["> click @e3", "A  a", "> fill @e5", "maxpm: @e5 is no longer on the page"])
        with self.assertRaisesRegex(RiverError, "the batch stopped at line 2; the 2 after it did not run"):
            self.asked("batch", "--file", f, res={"steps": steps, "ok": False, "left": 2})
        Path(f).write_text("screenshot\n", encoding="utf-8")
        with self.assertRaisesRegex(RiverError, "line 1 of the batch: screenshot is not for a batch"):
            self.asked("batch", "--file", f)
        Path(f).write_text("click @e1\nfly away\n", encoding="utf-8")
        with self.assertRaisesRegex(RiverError, r"line 2 of the batch \(fly away\)"):
            self.asked("batch", "--file", f)

    def test_status_says_who_controls_it(self):
        core.register(self.c, "ag1")
        res = {"project": "web", "running": True, "window": False, "url": "https://example.com/", "title": "Example",
               "tabs": [], "controller": {"who": "ag1", "kind": "session", "sub": "browser", "since": "now"}}
        with mock.patch.object(cli, "ask_server", lambda *a, **k: res):
            out = self.run_cli("-q", "--as", "ag1", "browser", "status")
        self.assertIn("controlled by ag1 (subagent browser)", out)
        self.assertIn("Example  https://example.com/", out)


class Watch(Base):
    """#5616: the panel draws the driving session's pointer and keys; a click glides and a text goes key by key while
    someone watches; a secret field shows dots; the person hands the browser to a session."""

    def fake(self, watched=True, owner=None):
        b = browser.get("web", owner)
        b.target = "T1"
        sent = []
        b.page = lambda method, params=None, timeout=None: sent.append((method, params or {})) or {}
        b.evaluate = lambda expr, timeout=None: False  # no secret field, and nothing else to read
        b.settle = lambda timeout=None: None
        b.info = lambda: {"project": "web", "url": "https://example.com/", "title": "Example"}
        if watched:
            b.watched = __import__("time").time()
        self.sleeps = []
        p = mock.patch.object(browser.time, "sleep", lambda s: self.sleeps.append(s))
        p.start()
        self.addCleanup(p.stop)
        return b, sent

    def test_a_click_glides_on_a_curve_then_presses_while_someone_watches(self):
        core.register(self.c, "ag1")
        b, sent = self.fake()
        browser.click(b, x=400, y=300, who="ag1")
        moves = [p for m, p in sent if p.get("type") == "mouseMoved"]
        self.assertEqual(len(moves), browser.GLIDE_STEPS)
        self.assertEqual((moves[-1]["x"], moves[-1]["y"]), (400.0, 300.0))
        # Not on the straight line: the middle of the move stands beside it.
        mid = moves[len(moves) // 2]
        x0, y0 = 400 - 160, 300 - 90
        across = abs((mid["x"] - x0) * (300 - y0) - (mid["y"] - y0) * (400 - x0)) / ((160 ** 2 + 90 ** 2) ** 0.5)
        self.assertGreater(across, 5)
        self.assertEqual([p["type"] for m, p in sent if m == "Input.dispatchMouseEvent"][-2:],
                         ["mousePressed", "mouseReleased"])
        self.assertAlmostEqual(sum(self.sleeps[:browser.GLIDE_STEPS - 1]), browser.GLIDE_TIME * (13 / 14), 3)
        d = browser.drawn(b)["pointer"]
        self.assertEqual((d["x"], d["y"], d["press"], d["px"], d["py"], d["who"]), (400, 300, 1, 400, 300, "ag1"))
        # Another tab shows: nothing is drawn over it.
        b.target = "T2"
        self.assertNotIn("pointer", browser.drawn(b))

    def test_nobody_watches_so_it_clicks_and_types_at_once(self):
        b, sent = self.fake(watched=False)
        browser.click(b, x=10, y=20, who="ag1")
        self.assertEqual([p["type"] for m, p in sent], ["mouseMoved", "mousePressed", "mouseReleased"])
        sent.clear()
        browser.type_text(b, "hello", who="ag1")
        self.assertEqual(sent, [("Input.insertText", {"text": "hello"})])
        self.assertEqual(browser.drawn(b)["typed"]["text"], "hello")

    def test_browser_pace_person_and_fast(self):
        b, sent = self.fake(watched=False)
        core.config_set(self.c, "browser_pace", "person", project="web")
        self.assertTrue(browser.pace(b))
        core.config_set(self.c, "browser_pace", "fast", project="web")
        b.watched = __import__("time").time()
        self.assertFalse(browser.pace(b))
        with self.assertRaisesRegex(RiverError, "browser_pace is watch"):
            core.config_set(self.c, "browser_pace", "slow", project="web")
        with self.assertRaisesRegex(RiverError, "browser_shot_cursor is on or off"):
            core.config_set(self.c, "browser_shot_cursor", "yes", project="web")

    def test_typing_goes_key_by_key_and_the_panel_shows_the_keys(self):
        b, sent = self.fake()
        browser.type_text(b, "hi\n", who="ag1")
        keys = [(p["type"], p["key"]) for m, p in sent if m == "Input.dispatchKeyEvent"]
        self.assertEqual(keys, [("keyDown", "h"), ("keyUp", "h"), ("keyDown", "i"), ("keyUp", "i"),
                                ("keyDown", "Enter"), ("keyUp", "Enter")])
        self.assertEqual([p.get("text") for m, p in sent if p["type"] == "keyDown"], ["h", "i", "\r"])
        self.assertEqual(browser.drawn(b)["typed"]["text"], "hi⏎")
        self.assertTrue(all(s <= browser.KEY_TIME for s in self.sleeps))
        # A long text takes TYPE_MAX at most.
        self.sleeps.clear()
        browser.type_text(b, "x" * 400, who="ag1")
        self.assertLessEqual(sum(self.sleeps), browser.TYPE_MAX + 0.01)
        self.assertEqual(len(browser.drawn(b)["typed"]["text"]), browser.TYPED_KEEP)

    def test_a_secret_field_shows_dots_and_its_text_is_never_kept(self):
        b, sent = self.fake()
        b.evaluate = lambda expr, timeout=None: expr == browser.SECRET_JS or None
        browser.type_text(b, "hunter2", who="ag1")
        t = browser.drawn(b)["typed"]
        self.assertEqual((t["text"], t["secret"]), ("•" * 7, True))
        self.assertNotIn("hunter2", json.dumps(b.typed))
        # A page that does not answer counts as secret.

        def broken(expr, timeout=None):
            raise RiverError("no answer")
        b.evaluate = broken
        self.assertTrue(browser.secret(b))

    def test_a_password_after_an_email_shows_dots(self):
        # The recording of #5616 showed the password: its keys went on the run of the email, which was not secret.
        b, sent = self.fake()
        field = ["email"]
        b.evaluate = lambda expr, timeout=None: (field[0] == "pw") if expr == browser.SECRET_JS else [5, 5]
        browser.type_text(b, "me@x.test", who="ag1")
        field[0] = "pw"
        browser.type_text(b, "hunter2", who="ag1")
        self.assertEqual(browser.drawn(b)["typed"]["text"], "•" * 7)
        # Through the relay: a press ends the run, so the next key looks at its field again.
        field[0] = "email"
        tabs = {"S1": "T1"}
        browser.relay_input(b, "ag1", {"sessionId": "S1", "method": "Input.insertText", "params": {"text": "me"}}, tabs)
        field[0] = "pw"
        browser.relay_input(b, "ag1", {"sessionId": "S1", "method": "Input.dispatchMouseEvent",
                                       "params": {"type": "mousePressed", "x": 5, "y": 5}}, tabs)
        browser.relay_input(b, "ag1", {"sessionId": "S1", "method": "Input.insertText", "params": {"text": "pw"}}, tabs)
        self.assertEqual(browser.drawn(b)["typed"]["text"], "••")
        self.assertNotIn("hunter2", json.dumps(b.typed))
        # A fill with no press (a script focused the field) is looked at each time.
        field[0] = "email"
        browser.relay_input(b, "ag1", {"sessionId": "S1", "method": "Input.insertText", "params": {"text": "me"}}, tabs)
        field[0] = "pw"
        browser.relay_input(b, "ag1", {"sessionId": "S1", "method": "Input.insertText", "params": {"text": "pw"}}, tabs)
        self.assertEqual(browser.drawn(b)["typed"]["text"], "••")

    def test_enter_ends_a_run_of_keys_so_the_next_field_is_looked_at_again(self):
        b, _ = self.fake()
        looks = []
        browser.note_keys(b, "ag1", "me", hidden=lambda: looks.append(1) or False)
        browser.note_keys(b, "ag1", key="Tab", hidden=lambda: looks.append(1) or False)
        browser.note_keys(b, "ag1", "pw", hidden=lambda: looks.append(1) or True)
        self.assertEqual((len(looks), browser.drawn(b)["typed"]["text"]), (2, "••"))

    def test_the_relay_records_the_input_of_a_session_for_the_shown_tab(self):
        b, _ = self.fake()
        tabs = {}
        browser.track_targets(tabs, {"method": "Target.attachedToTarget",
                                     "params": {"sessionId": "S1", "targetInfo": {"targetId": "T1"}}})
        browser.track_targets(tabs, {"method": "Target.attachedToTarget",
                                     "params": {"sessionId": "S2", "targetInfo": {"targetId": "T2"}}})
        browser.relay_input(b, "ag1", {"sessionId": "S1", "method": "Input.dispatchMouseEvent",
                                       "params": {"type": "mousePressed", "x": 5, "y": 6}}, tabs)
        for k in ("a", "b"):
            browser.relay_input(b, "ag1", {"sessionId": "S1", "method": "Input.dispatchKeyEvent",
                                           "params": {"type": "keyDown", "key": k, "text": k}}, tabs)
        browser.relay_input(b, "ag1", {"sessionId": "S1", "method": "Input.dispatchKeyEvent",
                                       "params": {"type": "keyDown", "key": "c", "text": "c", "modifiers": 4}}, tabs)
        browser.relay_input(b, "ag1", {"sessionId": "S2", "method": "Input.insertText", "params": {"text": "other tab"}}, tabs)
        browser.relay_input(b, "ag1", {"sessionId": "S1", "method": "Input.insertText", "params": {"text": "!"}}, tabs)
        d = browser.drawn(b)
        self.assertEqual((d["pointer"]["x"], d["pointer"]["press"], d["typed"]["text"]), (5, 1, "ab!"))
        browser.track_targets(tabs, {"method": "Target.detachedFromTarget", "params": {"sessionId": "S1"}})
        self.assertEqual(tabs, {"S2": "T2"})

    def test_the_frame_poll_carries_the_pointer_and_counts_as_watching(self):
        b, _ = self.fake(watched=False)
        b.watched = 0
        browser.note_pointer(b, "ag1", 1, 2)
        f = browser.frame(b, 0, 0.01)
        self.assertEqual((f["pointer"]["x"], f["pointer"]["y"]), (1, 2))
        self.assertTrue(browser.pace(b))

    def test_a_session_s_first_input_opens_the_browser_in_its_panel_once(self):
        routes.SHOWN.clear()
        self.addCleanup(routes.SHOWN.clear)
        n = len(routes.browser_opens())
        routes.first_input("web", "ag1")
        routes.first_input("web", "ag1")
        opens = routes.browser_opens()
        self.assertEqual(len(opens), n + 1)
        self.assertEqual((opens[-1]["session"], opens[-1]["project"]), ("ag1", "web"))

    def test_hand_to_a_session_gives_it_control_and_tells_its_chat(self):
        core.register(self.c, "ag1"); core.register(self.c, "mark", human=True)
        self.assertIn("browser_hand", server.TYPING_OPS)
        b, _ = self.fake(owner="ag1")  # the browser of ag1's panel (#5636)
        b.running = lambda: True
        browser.control(self.c, b, "ag1")
        browser.take(self.c, b, "mark")
        told = []
        with mock.patch.object(server, "_typing_pane", side_effect=RiverError("ag1 has no pane")):
            with self.assertRaisesRegex(RiverError, "no pane"):
                server.OPS["browser_hand"](self.c, {"project": "web", "to": "ag1"}, "mark")
        self.assertEqual(b.state["controller"]["who"], "mark")  # it stays with the person
        with mock.patch.object(server, "_typing_pane", lambda *a, **k: "%1"), \
                mock.patch.object(server, "session_send", lambda conn, agent, text, who=None, hold=False:
                                  told.append((agent, text, who, hold))):
            with self.assertRaisesRegex(RiverError, "only a person"):
                server.OPS["browser_hand"](self.c, {"project": "web", "to": "ag1"}, "ag1")
            out = server.OPS["browser_hand"](self.c, {"project": "web", "to": "ag1"}, "mark")
        self.assertEqual(b.state["controller"]["who"], "ag1")
        self.assertEqual(told, [("ag1", "The browser is at Example (https://example.com/). Take it from here.",
                                 "mark", True)])
        self.assertEqual(out["told"], "ag1")
        with self.assertRaisesRegex(RiverError, "no session nobody"):
            server.OPS["browser_hand"](self.c, {"project": "web", "to": "nobody"}, "mark")

    def test_screenshot_cursor_draws_the_pointer_in_and_takes_it_out(self):
        b, sent = self.fake()
        js = []
        b.evaluate = lambda expr, timeout=None: js.append(expr)
        b.page = lambda method, params=None, timeout=None: {"data": "PNG"}
        self.assertEqual(browser.screenshot(b, cursor=True)["cursor"], False)  # nobody pointed yet
        self.assertEqual(js, [])
        browser.note_pointer(b, "ag1", 50, 60, press=True)
        out = browser.screenshot(b, cursor=True)
        self.assertEqual((out["png"], out["cursor"]), ("PNG", True))
        self.assertTrue(js[0].startswith(browser.CURSOR_JS) and js[0].endswith("(50.0,60.0,50.0,60.0)"))
        self.assertEqual(js[1], browser.CURSOR_OFF)
        js.clear()
        self.assertFalse(browser.screenshot(b)["cursor"])
        self.assertEqual(js, [])


class FakeHandler:
    def __init__(self, path):
        self.path = path
        self.sent = None

    def _send(self, code, body, *a, **k):
        self.sent = (code, body)


class SaveSignins(Base):
    """#5633: Save sign-ins for the project copies the cookies of a session's browser into the project's profile."""

    def test_only_a_person_saves_and_the_cookies_go_through_the_project_chrome(self):
        core.register(self.c, "ag1"); core.register(self.c, "mark", human=True)
        with self.assertRaisesRegex(RiverError, "only a person"):
            routes.act(self.c, {"action": "save_signins", "project": "web"}, "ag1")
        s = browser.get("web", "ag1")
        s.running = lambda: True
        s.call = lambda method, params=None, **k: {"cookies": [
            {"name": "sid", "value": "1", "domain": ".example.test", "path": "/", "expires": 2e9, "size": 4,
             "session": False, "httpOnly": True, "secure": True},
            {"name": "tmp", "value": "2", "domain": "example.test", "path": "/", "expires": -1, "size": 4, "session": True}]}
        p = browser.get("web")
        got, events = [], []
        p.running = lambda: False  # the project's Chrome does not run: it starts for this and closes again
        p.start = lambda window=None: events.append("start")
        p.close = lambda: events.append("close")
        p.call = lambda method, params=None, **k: got.append((method, params))
        out = routes.act(self.c, {"action": "save_signins", "project": "web", "session": "ag1"}, "mark")
        self.assertEqual(out["saved"], 2)
        self.assertEqual(events, ["start", "close"])
        sent = got[0][1]["cookies"]
        self.assertEqual(got[0][0], "Storage.setCookies")
        self.assertNotIn("size", sent[0])  # only what setCookies takes
        self.assertEqual((sent[0]["expires"], "expires" in sent[1]), (2e9, False))  # a session cookie stays one
        with self.assertRaisesRegex(RiverError, "only the browser of a session"):
            browser.save_signins(p)

    def test_a_real_cookie_reaches_the_project_profile(self):
        if not chrome() or not browser.chrome_path():
            self.skipTest("no Chrome on this computer")
        if not _can_bind():
            self.skipTest("this sandbox does not allow a local port")
        core.register(self.c, "ag1")
        s = browser.get("web", "ag1")
        self.addCleanup(s.close)
        s.start()
        s.call("Storage.setCookies", {"cookies": [{"name": "signed_in", "value": "yes", "domain": ".example.test",
                                                    "path": "/", "expires": time.time() + 86400}]})
        self.assertEqual(browser.save_signins(s)["saved"], 1)
        p = browser.get("web")
        self.addCleanup(p.close)
        self.assertFalse(p.running())  # it started for the copy and closed again: the cookie is on disk
        p.start()
        names = [c["name"] for c in p.call("Storage.getCookies")["cookies"]]
        self.assertIn("signed_in", names)


class TabStrip(Base):
    """#5632: the pane's tab strip shows each tab with its icon (Chrome's Favicons of the profile) and the session whose
    pointer is on it."""

    def test_each_tab_has_its_icon_and_the_driving_session(self):
        import sqlite3
        b = browser.get("web", "ag1")
        path = b.dir / "profile" / "Default" / "Favicons"
        path.parent.mkdir(parents=True)
        con = sqlite3.connect(path)
        con.executescript("CREATE TABLE icon_mapping (page_url TEXT, icon_id INTEGER);"
                          "CREATE TABLE favicon_bitmaps (icon_id INTEGER, width INTEGER, image_data BLOB);")
        con.executemany("INSERT INTO icon_mapping VALUES (?,?)", [("https://example.com/", 1), ("https://github.com/", 2)])
        con.executemany("INSERT INTO favicon_bitmaps VALUES (?,?,?)",
                        [(1, 16, b"sixteen"), (1, 32, b"thirty-two"), (2, 64, b"gh")])
        con.commit(); con.close()
        b.targets = {"T1": {"url": "https://example.com/", "title": "Example", "opener": None},
                     "T2": {"url": "https://github.com/settings", "title": "GitHub", "opener": None},  # its site's icon
                     "T3": {"url": "about:blank", "title": "", "opener": None}}
        b.target = "T1"
        b.pointer = {"x": 1, "y": 2, "who": "ag1", "target": "T2", "n": 1, "press": 0}
        tabs = {t["id"]: t for t in b.info()["tabs"]}
        icon = lambda raw: "data:image/png;base64," + base64.b64encode(raw).decode()
        self.assertEqual(tabs["T1"]["icon"], icon(b"thirty-two"))  # the bitmap nearest 32 px
        self.assertEqual(tabs["T2"]["icon"], icon(b"gh"))
        self.assertIsNone(tabs["T3"]["icon"])
        self.assertEqual([tabs[k]["agent"] for k in ("T1", "T2", "T3")], [None, "ag1", None])
        self.assertEqual([tabs[k]["on"] for k in ("T1", "T2", "T3")], [True, False, False])


class SessionBrowsers(Base):
    """#5636 (mark 2026-11-12: 'I think it should be session specific'): each session drives a Chrome of its own."""

    def test_each_session_has_its_own_browser_and_folder(self):
        core.register(self.c, "ag1"); core.register(self.c, "ag2")
        a = routes.act(self.c, {"action": "status", "project": "web"}, "ag1")
        b = routes.act(self.c, {"action": "status", "project": "web"}, "ag2")
        p = routes.act(self.c, {"action": "status", "project": "web", "session": "ag1"}, None)  # the person, ag1's panel
        self.assertEqual((a["owner"], b["owner"], p["owner"]), ("ag1", "ag2", "ag1"))
        self.assertIs(browser.BROWSERS[("web", "ag1")], browser.get("web", "ag1"))
        self.assertEqual(browser.get("web", "ag1").dir, browser.browser_dir("web") / "sessions" / "ag1")
        self.assertEqual(browser.get("web").dir, browser.browser_dir("web"))  # the project's own, for page walks
        with mock.patch.object(browser, "walk", lambda b, pages, settle, timeout: {"owner": b.owner}):
            self.assertEqual(routes.act(self.c, {"action": "walk", "project": "web", "pages": ["a.test"]}, "ag1"),
                             {"owner": None})
        core.config_set(self.c, "browser_source", "cdp:9339", project="web")  # an attached Chrome: a context each (#5634)
        self.assertEqual(browser.get("web", "ag2", self.c).owner, "ag2")

    def test_the_first_start_copies_the_sign_ins_and_no_caches(self):
        src = browser.browser_dir("web") / "profile"
        for rel in ("Local State", "Default/Cookies", "Default/Cookies-journal", "Default/Network/Cookies",
                    "Default/Login Data", "Default/Local Storage/leveldb/000003.log", "Default/History",
                    "Default/Cache/Cache_Data/data_0"):
            (src / rel).parent.mkdir(parents=True, exist_ok=True)
            (src / rel).write_text(rel)
        dst = browser.session_dir("web", "ag1") / "profile"
        browser.copy_profile(src, dst)
        got = sorted(str(p.relative_to(dst)) for p in dst.rglob("*") if p.is_file())
        self.assertEqual(got, ["Default/Cookies", "Default/Cookies-journal", "Default/Local Storage/leveldb/000003.log",
                               "Default/Login Data", "Default/Network/Cookies", "Local State"])
        browser.copy_profile(browser.browser_dir("none") / "profile", browser.session_dir("none", "ag1") / "profile")
        self.assertTrue((browser.session_dir("none", "ag1") / "profile").is_dir())  # no project profile yet: empty

    def test_the_sweep_stops_a_gone_or_idle_session_browser(self):
        core.register(self.c, "ag1"); core.register(self.c, "ag2"); core.register(self.c, "ag3")
        closed = []
        bs = {}
        for name in ("ag1", "ag2", "ag3"):
            b = bs[name] = browser.get("web", name)
            b.running = lambda: True
            b.close = lambda name=name: closed.append(name)
        project_b = browser.get("web")
        project_b.running = lambda: True
        project_b.close = lambda: closed.append("project")
        core.unregister(self.c, "ag1")  # its session ended
        bs["ag2"].used -= 31 * 60  # nobody used it for browser_idle_end (30m)
        bs["ag3"].used -= 31 * 60
        bs["ag3"].watching = 1  # a panel waits for its frames: in use
        self.assertEqual(sorted(browser.sweep(self.c)), [("web", "ag1"), ("web", "ag2")])
        self.assertEqual(sorted(closed), ["ag1", "ag2"])  # never the project's own browser
        self.assertNotIn(("web", "ag1"), browser.BROWSERS)  # a gone session's browser goes
        self.assertIn(("web", "ag2"), browser.BROWSERS)  # an idle one starts again at its next use

    def test_the_sweep_stops_a_chrome_an_earlier_serve_left_for_a_gone_session(self):
        d = browser.session_dir("web", "old")
        d.mkdir(parents=True)
        (d / "state.json").write_text(json.dumps({"project": "web", "owner": "old", "pid": 4242}))
        closed = []
        with mock.patch.object(core, "pid_alive", lambda pid: pid == 4242), \
                mock.patch.object(browser.Browser, "close", lambda b: closed.append((b.project, b.owner))):
            self.assertEqual(browser.sweep(self.c), [("web", "old")])
        self.assertEqual(closed, [("web", "old")])
        self.assertEqual(browser.sweep(self.c), [])  # its Chrome ended (pid 4242 is not alive here)

    def test_the_panel_s_frames_come_from_its_session_s_browser(self):
        browser.get("web", "ag1").seq = 7
        h = FakeHandler(f"/api/browser/frame?project=web&session=ag2&after=0&token={server.PAGE_TOKEN}")
        routes.frame(h, "/api/browser/frame")
        self.assertEqual(h.sent[1]["seq"], 0)  # ag2 has no browser yet: not ag1's
        self.assertIsNone(browser.find("web", "ag2"))
        self.assertIs(browser.find("web", "ag1"), browser.get("web", "ag1"))
        core.config_set(self.c, "browser_idle_end", "10m")
        with self.assertRaises(RiverError):
            core.config_set(self.c, "browser_idle_end", "soon")


class NoChrome(Base):
    """#5624: with no Google Chrome, the panel, the setup guide and maxpm browser ask the person to download it."""

    def setUp(self):
        super().setUp()
        missing = os.path.join(self.tmp.name, "no-chrome")
        patch =mock.patch.dict(os.environ, {"MAXPM_CHROME": missing})
        patch.start()
        self.addCleanup(patch.stop)
        self.missing = missing

    def test_a_command_says_where_to_download_it(self):
        why = browser.no_chrome()
        self.assertIn(browser.CHROME_DOWNLOAD, why)
        self.assertIn(f"MAXPM_CHROME names {self.missing}", why)
        self.assertIn("tells the person and does not try again", why)
        with self.assertRaisesRegex(RiverError, "https://www.google.com/chrome/"):
            browser.get("web").start()
        self.assertFalse(browser.get("web").running())

    def test_the_panel_offers_the_download_and_works_after_the_install(self):
        got = routes.act(self.c, {"action": "start", "project": "web"}, "mark")
        self.assertEqual((got["running"], got["download"]), (False, browser.CHROME_DOWNLOAD))
        self.assertIn("not on this computer", got["no_chrome"])
        Path(self.missing).write_text("")  # installed: the next start finds it, with no restart of serve
        self.assertIsNone(browser.no_chrome())
        with mock.patch.object(browser.Browser, "start") as start:
            got = routes.act(self.c, {"action": "start", "project": "web"}, "mark")
        start.assert_called_once()
        self.assertNotIn("no_chrome", got)

    def test_an_attached_browser_needs_no_chrome_here(self):
        core.config_set(self.c, "browser_source", "cdp:9339", project="web")
        with mock.patch.object(browser.Browser, "start"):
            got = routes.act(self.c, {"action": "start", "project": "web"}, "mark")
        self.assertNotIn("no_chrome", got)

    def test_the_setup_guide_shows_the_step(self):
        self.assertFalse(server.setup_status(self.c)["chrome"])


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
        # Each session has a browser of its own (#5636): ag1's control holds in ag1's browser, not in ag2's.
        st2 = server.OPS["browser"](self.c, {"action": "status", "project": "web"}, "ag2")
        self.assertEqual((st2["owner"], st2["controller"]), ("ag2", None))
        with self.assertRaisesRegex(RiverError, "ag1 controls the browser of web"):
            server.OPS["browser"](self.c, {"action": "open", "project": "web", "session": "ag1", "url": "example.com"},
                                  None)  # the person, in ag1's panel
        with self.assertRaisesRegex(RiverError, "not running"):
            server.OPS["browser_input"](self.c, {"project": "web", "ev": {"type": "text", "text": "x"}}, None)

    def _sent(self, b, slow=0.0):
        """b.page records each call to Chrome (a key: its keyDown and its char/keyUp; slow: seconds each call takes)."""
        got = []

        def page(method, params, timeout=None):
            time.sleep(slow)
            got.append((method, params))
        return got, [mock.patch.object(b, "running", lambda: True), mock.patch.object(b, "page", page)]

    @staticmethod
    def _keys(got):
        return [p.get("text") for m, p in got if m == "Input.insertText" or p.get("type") in ("keyDown", "char")
                if p.get("text")]

    def test_the_input_of_one_request_goes_in_order(self):
        # #5479: the keys typed while a request runs come in the next one, as a list. #5651: the request answers at
        # once, and serve sends the calls to Chrome after it, in order.
        b = browser.get("web")
        got, patches = self._sent(b)
        with patches[0], patches[1]:
            out = server.OPS["browser_input"](self.c, {"project": "web", "evs": [
                {"type": "key", "key": "h"}, {"type": "key", "key": "i"}, {"type": "key", "key": "Enter"}]}, None)
            server.OPS["browser_input"](self.c, {"project": "web", "ev": {"type": "text", "text": "x"}}, None)
            self.assertTrue(b.inputs.settle())
            with self.assertRaisesRegex(RiverError, "unknown input"):  # a bad input is refused in the request itself
                server.OPS["browser_input"](self.c, {"project": "web", "ev": {"type": "dance"}}, None)
        self.assertEqual(self._keys(got), ["h", "i", "\r", "x"])
        self.assertEqual(out, {"ok": True, "error": None})

    def test_numbered_batches_go_in_the_panes_order(self):
        # #5651: the pane sends each batch at once; a later one can come first (another thread of serve, the relay's
        # pool). It waits for the ones before it, and a batch that never comes is passed after INPUT_GAP.
        b = browser.get("web")
        got, patches = self._sent(b)
        inp = lambda n, k, pane="p1": server.OPS["browser_input"](
            self.c, {"project": "web", "pane": pane, "n": n, "ev": {"type": "text", "text": k}}, None)
        with patches[0], patches[1]:
            inp(2, "b")
            inp(3, "c")
            inp(1, "a", "p2")  # another pane counts on its own
            time.sleep(0.05)
            self.assertEqual(self._keys(got), ["a"])
            inp(1, "a")
            self.assertTrue(b.inputs.settle())
            self.assertEqual(self._keys(got), ["a", "a", "b", "c"])
            inp(3, "old")  # a number that went already: a request that came twice
            inp(5, "e")    # 4 is lost
            t = time.monotonic()
            self.assertTrue(b.inputs.settle())
            self.assertGreaterEqual(time.monotonic() - t, browser.INPUT_GAP * 0.8)
            inp(6, "f")
            self.assertTrue(b.inputs.settle())
        self.assertEqual(self._keys(got), ["a", "a", "b", "c", "e", "f"])

    def test_moves_and_wheel_steps_that_wait_join(self):
        # #5651: while Chrome is slow, of the moves that wait only the newest goes, and wheel steps add up; a press
        # never joins, so the order of a drag holds.
        b = browser.get("web")
        got, patches = self._sent(b, slow=0.1)
        mv = lambda x: {"type": "mouse", "action": "move", "x": x, "y": 5, "button": "left"}
        wh = lambda dy: {"type": "mouse", "action": "wheel", "x": 1, "y": 1, "dx": 0, "dy": dy}
        with patches[0], patches[1]:
            server.OPS["browser_input"](self.c, {"project": "web", "evs": [
                {"type": "mouse", "action": "down", "x": 1, "y": 5}, mv(2), mv(3), mv(4),
                {"type": "mouse", "action": "up", "x": 4, "y": 5}, wh(40), wh(40), wh(-10), wh(-20)]}, None)
            self.assertTrue(b.inputs.settle())
        # The steps down add up (80); the step up replaces them (mark: the page went on down before it turned); the
        # next step up adds to it.
        kinds = [(p["type"], p.get("x"), p.get("deltaY")) for m, p in got]
        self.assertEqual(kinds, [("mousePressed", 1, None), ("mouseMoved", 4, None), ("mouseReleased", 4, None),
                                 ("mouseWheel", 1, -30)])
        self.assertEqual((got[1][1]["button"], got[1][1]["buttons"]), ("left", 1))  # a drag: the button is held

    def test_the_first_click_takes_a_browser_that_nobody_controls(self):
        # #5651 (mark: the browser was 'controlled by nobody' while he used it): a hover takes nothing; the first
        # click, wheel step or key of the person takes control, with no Take control first.
        b = browser.get("web")
        got, patches = self._sent(b)
        with patches[0], patches[1]:
            server.OPS["browser_input"](self.c, {"project": "web", "ev": {"type": "mouse", "action": "move", "x": 1, "y": 1}}, None)
            self.assertIsNone(b.state.get("controller"))
            server.OPS["browser_input"](self.c, {"project": "web", "ev": {"type": "mouse", "action": "down", "x": 1, "y": 1}}, None)
            self.assertTrue(b.inputs.settle())
        self.assertEqual(b.state["controller"]["kind"], "person")

    def test_a_refusal_of_chrome_comes_in_the_next_answer(self):
        b = browser.get("web")

        def page(method, params, timeout=None):
            raise RiverError("the browser refused Input.insertText: no")
        with mock.patch.object(b, "running", lambda: True), mock.patch.object(b, "page", page):
            server.OPS["browser_input"](self.c, {"project": "web", "ev": {"type": "text", "text": "x"}}, None)
            self.assertTrue(b.inputs.settle())
            out = server.OPS["browser_input"](self.c, {"project": "web", "ev": {"type": "text", "text": "y"}}, None)
        self.assertIn("refused", out["error"])

    def test_a_session_open_asks_the_page_to_show_it(self):
        routes._ask_open("web", "ag1", "ag1")
        self.assertEqual(routes.browser_opens()[-1], {"seq": routes._open_seq[0], "project": "web", "session": "ag1",
                                                      "by": "ag1"})


class Suggest(Base):
    """#5635: the address bar suggests the addresses that the browser visited: Chrome's History of its profile (read
    from a copy, kept in memory) and the pages its tabs showed; an address that starts with the text first."""

    def history(self, b, rows):
        import sqlite3
        path = b.dir / "profile" / "Default" / "History"
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT, visit_count INTEGER, "
                    "typed_count INTEGER, last_visit_time INTEGER, hidden INTEGER DEFAULT 0)")
        con.executemany("INSERT INTO urls (url, title, visit_count, typed_count, last_visit_time) VALUES (?, ?, ?, ?, ?)",
                        rows)
        con.commit()
        con.close()
        return path

    def test_history_and_the_pages_of_the_tabs(self):
        b = browser.get("web")
        t0 = 13300000000000000  # Chrome's microseconds since 1601
        self.history(b, [("https://news.ycombinator.com/", "Hacker News", 30, 2, t0),
                         ("https://github.com/mhalstrom/maximizepm", "maximizepm on GitHub", 3, 0, t0 + 5),
                         ("https://www.google.com/search?q=hacker", "hacker - Google Search", 9, 0, t0 + 9),
                         ("https://example.com/news", "Example news", 1, 0, t0 + 7)])
        urls = lambda text: [x["url"] for x in browser.suggest(b, text)]
        # A start of the address comes first (www. and the scheme do not count), then the most visited.
        self.assertEqual(urls("news"), ["https://news.ycombinator.com/", "https://example.com/news"])
        self.assertEqual(urls("ex"), ["https://example.com/news"])
        self.assertEqual(urls("github maximizepm"), ["https://github.com/mhalstrom/maximizepm"])
        self.assertEqual(urls("hacker"), ["https://news.ycombinator.com/"])  # not Google's search page
        self.assertEqual(urls("   "), [])
        # A page that a tab showed counts at once, before Chrome writes its History.
        b._event("Target.targetInfoChanged", {"targetInfo": {"type": "page", "targetId": "T1",
                                                              "url": "https://docs.python.org/3/", "title": "Python docs"}}, None)
        self.assertEqual(browser.suggest(b, "python"), [{"url": "https://docs.python.org/3/", "title": "Python docs"}])

    def test_the_history_is_read_once_and_again_when_it_changed(self):
        b = browser.get("web")
        path = self.history(b, [("https://a.example/", "A", 1, 0, 13300000000000000)])
        reads = []
        real = browser.read_history
        with mock.patch.object(browser, "read_history", lambda p: reads.append(p) or real(p)):
            browser.suggest(b, "a")
            browser.suggest(b, "a")
            self.assertEqual(len(reads), 1)  # from memory
            os.utime(path, (1, 1))
            b.history["at"] = 0  # the wait between two reads is over
            browser.suggest(b, "a")
            for _ in range(200):  # a changed file is read in a thread: the answer came from memory
                if not b.history["reading"]:
                    break
                time.sleep(0.01)
            self.assertEqual(len(reads), 2)

    def test_a_browser_with_no_profile_suggests_what_its_tabs_showed(self):
        b = browser.get("web")
        b.attached = 9222  # a Chrome that serve did not start: its History is its own
        browser.note_visit(b, "https://example.org/", "Example")
        browser.note_visit(b, "chrome://settings", "Settings")
        self.assertEqual(browser.suggest(b, "exam"), [{"url": "https://example.org/", "title": "Example"}])
        self.assertEqual(browser.suggest(b, "settings"), [])

    def test_the_op_answers_without_chrome(self):
        browser.note_visit(browser.get("web"), "https://example.org/", "Example")
        got = server.OPS["browser"](self.c, {"action": "suggest", "project": "web", "text": "example"}, None)
        self.assertEqual(got, {"items": [{"url": "https://example.org/", "title": "Example"}]})
        self.assertFalse(browser.get("web").running())


def _can_bind():
    try:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.close()
        return True
    except OSError:
        return False


def jpeg_size(data):
    """(width, height) of a base64 JPEG, from its frame header."""
    d, i = base64.b64decode(data), 2
    while i < len(d):
        m, n = d[i + 1], struct.unpack(">H", d[i + 2:i + 4])[0]
        if m in (0xC0, 0xC1, 0xC2):
            return struct.unpack(">HH", d[i + 5:i + 9])[::-1]
        i += 2 + n
    return None


class Fit(Base):
    """#5625: what the panel says of its size, its density and its screen, within limits."""

    def test_resize_limits(self):
        b = browser.Browser("web")
        b.resize(100, 5000, 3.5, [390, "844"])
        self.assertEqual((b.size, b.dsf, b.screen), ((200, 3000), browser.DSF_MAX, (390, 844)))
        b.resize(800, 600, "x", "bad")
        self.assertEqual((b.size, b.dsf, b.screen), ((800, 600), 1, None))
        self.assertEqual(b.info()["size"], [800, 600])

    def test_which_panel_sets_the_size(self):
        """mark 2026-11-12: the panel where the person last used the browser sets the size, also while a session
        drives; with none, the largest open panel; the others show the frame scaled and change nothing."""
        b = browser.Browser("web")
        b.resize(1000, 800, 2, [1512, 982], pane="mac")
        b.resize(370, 620, 3, [390, 844], pane="phone")
        self.assertEqual((b.size, b.dsf), ((1000, 800), 2))  # nobody used it yet: the largest
        b.drive("phone")  # the person taps in the phone's panel
        self.assertEqual((b.size, b.dsf), ((370, 620), browser.DSF_MAX))
        b.resize(1200, 900, 2, None, pane="mac")  # the Mac's panel grows: it changes nothing
        self.assertEqual(b.size, (370, 620))
        b.resize(360, 600, 3, None, pane="phone")  # the phone turns: its size holds
        self.assertEqual(b.size, (360, 600))
        b.panes["phone"]["at"] -= browser.PANE_GONE + 1  # the phone's panel closed
        self.assertTrue(b.pane_seen("mac"))
        self.assertFalse(b.pane_seen("phone"))
        self.assertEqual(b.size, (1200, 900))

    def test_the_moving_frames_go_smaller_when_a_sharp_frame_follows(self):
        """#5658: at a density above 1 the screencast frames show only while the page moves, so their JPEG quality
        is lower; the screencast starts again when the density crosses 1."""
        b = browser.Browser("web")
        calls = []
        b.call = lambda method, params=None, session=None, timeout=None: calls.append((method, params)) or {}
        b._want_sharp = lambda now=False: None
        b.session, b.cast_quality = "S", browser.STILL_QUALITY
        b.resize(800, 600, 1)
        b._fit()
        self.assertEqual([m for m, _ in calls], ["Emulation.setDeviceMetricsOverride"])  # the same quality: no restart
        b.resize(800, 600, 2)
        calls.clear()
        b._fit()
        self.assertEqual([(m, (p or {}).get("quality")) for m, p in calls],
                         [("Emulation.setDeviceMetricsOverride", None), ("Page.stopScreencast", None),
                          ("Page.startScreencast", browser.MOVING_QUALITY)])
        self.assertLess(browser.MOVING_QUALITY, browser.STILL_QUALITY)


class SharpWhenStill(Base):
    """#5637 (mark 2026-11-12: 'the layout in the browser keeps flashing'): while the page moves, the panel gets the
    screencast frames only; one sharp frame follows when the page holds still, and the repaint of its screenshot
    does not take its place. A poll of a panel that watches asks for no screenshot."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(browser, "SHARP_QUIET", 0.15)
        p.start()
        self.addCleanup(p.stop)
        self.b = b = browser.Browser("web")
        b.session, b.size, b.dsf, b.watched = "s1", (500, 400), 2, time.time()
        self.shots = []

        def call(method, params=None, session=None, timeout=None):
            self.shots.append(method)
            return {"data": base64.b64encode(b"sharp").decode()}
        b.call = call
        b.send = lambda *a, **k: None

    def paint(self, data):
        self.b.watched = time.time()
        self.b._event("Page.screencastFrame", {"sessionId": 1, "data": base64.b64encode(data).decode(),
                                               "metadata": {"deviceWidth": 500, "deviceHeight": 400}}, "s1")

    def test_no_sharp_frame_while_the_page_moves(self):
        b = self.b
        for i in range(10):  # a scroll: a new frame each 50 ms, each one shown at once
            self.paint(b"frame %d" % i)
            self.assertEqual((b.frame["data"], b.frame.get("sharp")), (base64.b64encode(b"frame %d" % i).decode(), None))
            browser.frame(b, b.seq, 0.01, "pane")  # the panel polls while it watches
            time.sleep(0.05)
        self.assertEqual(self.shots, [])
        time.sleep(0.4)  # the page holds still: one sharp frame
        self.assertEqual((self.shots, b.frame.get("sharp")), (["Page.captureScreenshot"], True))
        self.paint(b"frame 9")  # the screenshot painted the page again: the sharp frame stays
        self.assertTrue(b.frame.get("sharp"))
        self.paint(b"frame 10")  # the page changes: the new frame at once
        self.assertIsNone(b.frame.get("sharp"))
        time.sleep(0.4)
        self.assertEqual(len(self.shots), 2)


class ManyPolls(Base):
    """#5637: a pane over a long link keeps several polls open; each gets the next change, never one that another poll
    of the pane took; a poll that waits its whole time gets the frame after its own seq; the answer is binary."""

    def put(self, b, data):
        with b.cond:
            b.seq += 1
            b.frame = {"seq": b.seq, "data": base64.b64encode(data).decode(), "w": 500, "h": 400}
            b.cond.notify_all()

    def test_each_poll_gets_the_next_change(self):
        b = browser.Browser("web")
        self.put(b, b"one")
        got, polls = [], []
        for _ in range(2):
            t = threading.Thread(target=lambda: got.append(browser.frame(b, b.seq, 3, "p", many=True)))
            t.start()
            polls.append(t)
        time.sleep(0.2)
        self.put(b, b"two")
        time.sleep(0.1)
        self.put(b, b"three")
        for t in polls:
            t.join(5)
        self.assertEqual(sorted(base64.b64decode(g["frame"]["data"]) for g in got), [b"three", b"two"])
        # Nothing new for the whole poll: the frame after the panel's own seq, in case an answer was lost.
        again = browser.frame(b, 1, 0.1, "p", many=True)
        self.assertEqual((again["frame"]["seq"], again["waited"] >= 100), (3, True))
        self.assertNotIn("frame", browser.frame(b, 3, 0.1, "p", many=True))

    def test_the_binary_answer(self):
        b = browser.Browser("web")
        self.put(b, b"\xff\xd8jpeg")
        raw = browser.frame_bytes(browser.frame(b, 0, 0.1, "p", many=True))
        n = struct.unpack(">I", raw[:4])[0]
        head = json.loads(raw[4:4 + n])
        self.assertEqual((head["frame"], raw[4 + n:]), ({"w": 500, "h": 400, "sharp": False, "seq": 1}, b"\xff\xd8jpeg"))
        self.assertEqual(browser.frame_bytes({"seq": 0})[4:], b'{"seq": 0}')


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
        # The traits of a Chrome with a window (#5619): Google's search sent plain headless Chrome a CAPTCHA.
        self.assertEqual(b.evaluate("[navigator.webdriver, /Headless/.test(navigator.userAgent), outerWidth]"),
                         [False, False, browser.VIEW[0]])
        self.assertTrue(base64.b64decode(browser.screenshot(b)["png"]).startswith(b"\x89PNG"))
        f = browser.frame(b, 0, 5)
        self.assertEqual((f["frame"]["w"], f["frame"]["h"]), (640, 480))
        # #5616: while someone watches, a click glides and a text goes in key by key; the page gets both the same way.
        b.evaluate("document.getElementById('q').value = ''")
        b.watched = __import__("time").time()
        browser.type_text(b, "slow", "#q", who="ag1")
        browser.click(b, "#b", who="ag1")
        self.assertEqual(b.evaluate("document.title"), "clicked slow")
        self.assertEqual(browser.frame(b, 0, 1)["typed"]["text"], "slow")
        plain = browser.screenshot(b)
        drawn = browser.screenshot(b, cursor=True)
        self.assertEqual((plain["cursor"], drawn["cursor"]), (False, True))
        self.assertNotEqual(plain["png"], drawn["png"])
        self.assertIsNone(b.evaluate("document.getElementById('__maxpm_cursor')"))  # it went right after
        self.assertEqual(browser.screenshot(b)["png"], plain["png"])
        # #5625: the page takes the panel's size at its screen's density; the frames are sharp (two pixels for each CSS
        # pixel), the screen is a normal one when the panel's is smaller than the window, and a session's screenshot
        # keeps one pixel for each CSS pixel.
        b.resize(500, 400, 2, [390, 844])
        b.watched = __import__("time").time()
        f = browser.frame(b, 0, 5)
        for _ in range(50):
            if b.frame.get("sharp"):
                break
            f = browser.frame(b, f["seq"], 1)
        self.assertEqual((b.frame["w"], b.frame["h"], jpeg_size(b.frame["data"])), (500, 400, (1000, 800)))
        self.assertEqual(b.evaluate("[innerWidth, innerHeight, devicePixelRatio, screen.width >= outerWidth]"),
                         [500, 400, 2, True])
        shot = base64.b64decode(browser.screenshot(b)["png"])
        self.assertEqual(struct.unpack(">II", shot[16:24]), (500, 400))
        # A page of an allowed address still cannot reach another loopback port through the proxy.
        core.config_unset(self.c, "browser_allow", project="web")
        got = b.evaluate(f"fetch('http://127.0.0.1:{port}/sneak').then(r => 'got ' + r.status).catch(e => 'ERR')")
        self.assertIn(got, ("ERR", "got 403"))  # the proxy's refusal, or Chrome's own
        self.assertNotIn("/sneak", hits)
        browser.person_input(b, {"type": "key", "key": "Tab"})
        # #5651: a Chrome that starts at density 2 put a hover (a move with no button held) at half its point, until
        # the density was set again after a page loaded; serve sets it again after the first load.
        core.config_set(self.c, "browser_allow", f"127.0.0.1:{port},localhost:{port}", project="web")
        b.close()
        b.resize(500, 400, 2, [390, 844])
        for url in (f"http://127.0.0.1:{port}/page", f"http://127.0.0.1:{port}/again",
                    f"http://localhost:{port}/other-site"):
            b.open(url)
            b.evaluate("addEventListener('mousemove', (e) => { window.__at = [e.clientX, e.clientY]; }); 1")
            for i in range(50):
                browser.person_input(b, {"type": "mouse", "action": "move", "x": 120 + i % 2, "y": 90})
                if b.evaluate("window.__at") == [120 + i % 2, 90]:
                    break
                time.sleep(0.1)  # the density is set again just after the first load
            self.assertEqual(b.evaluate("window.__at"), [120 + i % 2, 90], url)


FORM = (b"<title>Form</title><h1>Sign up</h1><label>Name <input id=name></label>"
        b"<input id=pw type=password placeholder=Password><select id=size><option value=s>Small</option>"
        b"<option value=m>Medium</option></select><label><input type=checkbox id=ok> Agree</label>"
        b"<button id=go onclick=\"const v = i => document.getElementById(i); "
        b"document.title = 'sent ' + v('name').value + ' ' + v('size').value + ' ' + v('ok').checked\">Send</button>"
        b"<a href=/two>Next page</a><div hidden><button>Hidden</button></div><div id=host></div>"
        b"<script>host.attachShadow({mode: 'open'}).innerHTML = '<button>In shadow</button>'</script>")


class AgentActions(Base):
    """#5663: maxpm browser for an agent in a real Chrome: a snapshot with refs, click, fill, select and check by ref,
    the wait for the next page that a click led to, a ref of an earlier page, and a batch."""

    def test_snapshot_refs_fill_select_check_wait_and_batch(self):
        if not chrome() or not browser.chrome_path():
            self.skipTest("no Chrome on this computer")
        if not _can_bind():
            self.skipTest("this sandbox does not allow a local port")

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/two":
                    time.sleep(0.4)  # a slow next page: the click waits for it
                body = b"<title>Two</title><a href=/one>Back to one</a>" if self.path == "/two" else FORM
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
        core.config_set(self.c, "browser_allow", f"127.0.0.1:{port}", project="web")
        b = browser.get("web")
        self.addCleanup(b.close)
        b.open(f"http://127.0.0.1:{port}/one")
        s = browser.snapshot(b, interactive=True)["snapshot"]
        ref = {line.split('"')[1]: line.split()[0] for line in s["lines"]}
        self.assertEqual(sorted(ref), ["", "Agree", "In shadow", "Name", "Next page", "Password", "Send"], s["lines"])
        self.assertIn(f'{ref[""]} combobox "" value="Small"', s["lines"])
        self.assertIn(f'{ref["Agree"]} checkbox "Agree"', s["lines"])
        self.assertNotIn("Hidden", json.dumps(s))
        self.assertIn("# Sign up", browser.snapshot(b)["snapshot"]["lines"])  # without -i: the headings too
        again = browser.snapshot(b, interactive=True)["snapshot"]
        self.assertEqual(again["lines"], s["lines"])  # the same elements keep their refs
        self.assertFalse(b.evaluate("'__maxpm' in window"))  # the page's own scripts do not see the refs
        t0 = time.time()
        browser.fill(b, ref["Name"], "Ann")
        browser.fill(b, ref["Name"], "Bob")  # the old text goes
        browser.fill(b, ref["Password"], "hunter2")
        browser.select_options(b, ref[""], ["Medium"])
        browser.check(b, ref["Agree"])
        browser.check(b, ref["Agree"])  # checked already: no click
        out = browser.click(b, ref["Send"])
        self.assertLess(time.time() - t0, 2.0)
        self.assertEqual((b.evaluate("document.title"), out["navigated"]), ("sent Bob m true", False))
        d = browser.snapshot(b, interactive=True, delta=True)["snapshot"]
        self.assertTrue(d["delta"])
        self.assertEqual(sorted(d["lines"]), sorted([f'{ref["Name"]} textbox "Name" value="Bob"',
                                                     f'{ref["Password"]} textbox "Password" value=•••',
                                                     f'{ref[""]} combobox "" value="Medium"',
                                                     f'{ref["Agree"]} checkbox "Agree" checked']))
        with self.assertRaisesRegex(RiverError, "not a text field: maxpm browser check"):
            browser.fill(b, ref["Agree"], "x")
        with self.assertRaisesRegex(RiverError, 'no option "Large"; it has "Small", "Medium"'):
            browser.select_options(b, ref[""], ["Large"])
        # A click on a link waits for the next page, slow as it is; a ref of the page before fails.
        out = browser.click(b, ref["Next page"])
        self.assertEqual((out["navigated"], out["title"], b.evaluate("document.readyState")), (True, "Two", "complete"))
        with self.assertRaisesRegex(RiverError, "from an earlier page: run maxpm browser snapshot"):
            browser.click(b, ref["Send"])
        two = browser.snapshot(b, interactive=True)["snapshot"]
        self.assertEqual(len(two["lines"]), 1)
        self.assertGreater(int(two["lines"][0].split()[0][2:]), max(int(r[2:]) for r in ref.values()))
        # A batch: the steps in order, one answer; the first that fails ends it.
        res = routes.act(self.c, {"action": "batch", "project": "web", "steps": [
            {"action": "back"}, {"action": "snapshot", "interactive": True}, {"action": "click", "selector": "@e999"},
            {"action": "reload"}]}, None)
        self.assertEqual([("error" in s, s["step"]["action"]) for s in res["steps"]],
                         [(False, "back"), (False, "snapshot"), (True, "click")])
        self.assertEqual((res["ok"], res["left"], res["steps"][0]["result"]["url"]), (False, 1, f"http://127.0.0.1:{port}/one"))
        self.assertIn("is no longer on the page", res["steps"][2]["error"])


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

    def _shared(self):
        """A shared headless Chrome on a free port, as browser_source cdp:<port> of web; skips with no Chrome."""
        chrome_ = chrome() and browser.chrome_path()
        if not chrome_:
            self.skipTest("no Chrome on this computer")
        if not _can_bind():
            self.skipTest("this sandbox does not allow a local port")
        import subprocess
        prof = os.path.join(self.tmp.name, "shared")
        p = subprocess.Popen([chrome_, "--headless=new", f"--user-data-dir={prof}", "--remote-debugging-port=0",
                              "--no-first-run", "about:blank"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(self._stop_chrome, p, prof)
        port, end = None, time.monotonic() + browser.START_WAIT
        while port is None and time.monotonic() < end and p.poll() is None:
            try:
                port = int(Path(prof, "DevToolsActivePort").read_text(encoding="utf-8").split()[0])
            except (OSError, ValueError, IndexError):
                time.sleep(0.1)
        self.assertTrue(port, f"the shared Chrome did not start within {browser.START_WAIT}s (exit {p.poll()})")
        core.config_set(self.c, "browser_source", f"cdp:{port}", project="web")
        return port

    def test_each_session_has_a_context_of_its_own_with_the_sign_ins(self):
        """#5634: in the shared test browser each session's tabs are its own (a browser context), with the cookies of
        the default context; Save sign-ins puts a session's cookies back there; a closed session's context goes."""
        self._shared()
        core.register(self.c, "ag1"); core.register(self.c, "ag2")
        base = browser.get("web")  # the default context, as the shared tools see it
        self.addCleanup(base.close)
        base.start()
        base.call("Storage.setCookies", {"cookies": [{"name": "signed_in", "value": "yes", "domain": ".example.test",
                                                       "path": "/", "expires": time.time() + 86400}]})
        a, b = browser.get("web", "ag1"), browser.get("web", "ag2")
        self.addCleanup(a.close); self.addCleanup(b.close)
        a.start(); b.start()
        self.assertTrue(a.context and b.context and a.context != b.context)
        ids = lambda x: {t["id"] for t in x.info()["tabs"]}
        self.assertFalse(ids(a) & ids(b))  # each sees only its own tab
        names = lambda x, ctx: [c["name"] for c in x.call("Storage.getCookies", {"browserContextId": ctx})["cookies"]]
        self.assertIn("signed_in", names(a, a.context))  # the sign-ins came along
        a.call("Storage.setCookies", {"cookies": [{"name": "new_sign_in", "value": "1", "domain": ".other.test",
                                                    "path": "/", "expires": time.time() + 86400}],
                                      "browserContextId": a.context})
        self.assertNotIn("new_sign_in", [c["name"] for c in base.call("Storage.getCookies")["cookies"]])
        self.assertGreaterEqual(browser.save_signins(a)["saved"], 2)
        self.assertIn("new_sign_in", [c["name"] for c in base.call("Storage.getCookies")["cookies"]])
        gone = a.context
        a.close()  # the session ends: its context and tabs go with serve's connection
        time.sleep(0.5)
        self.assertNotIn(gone, base.call("Target.getBrowserContexts")["browserContextIds"])

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
        # The first Chrome of a fresh GitHub runner took more than 10 s to write its port (#5602): wait as long as
        # serve waits for its own Chrome (browser.START_WAIT), and stop at once when Chrome ended.
        port, end = None, time.monotonic() + browser.START_WAIT
        while port is None and time.monotonic() < end and p.poll() is None:
            try:
                port = int(Path(prof, "DevToolsActivePort").read_text(encoding="utf-8").split()[0])
            except (OSError, ValueError, IndexError):
                time.sleep(0.1)
        self.assertTrue(port, f"the shared Chrome did not start within {browser.START_WAIT}s (exit {p.poll()})")
        core.config_set(self.c, "browser_source", f"cdp:{port}", project="web")
        b = browser.get("web")
        b.start()
        info = b.info()
        self.assertEqual(info["source"], f"cdp:{port}")
        self.assertFalse(info["window"])  # headless, from the arguments of the Chrome on that port (#5619)
        self.assertFalse(info["modes"])  # no browser_mode: the panel offers no window
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


# The CDP route starts the project's Chrome (up to browser.START_WAIT) before it answers the WebSocket upgrade; the
# client's 10 s ran out on a slow GitHub runner (#5602).
WS_WAIT = browser.START_WAIT + 10

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

    def test_the_upgrade_answers_with_an_http_1_1_status_line(self):
        # #5657: agent-browser (a Rust WebSocket client) refuses 'HTTP/1.0 101'; RFC 6455 wants HTTP/1.1.
        if not _can_bind():
            self.skipTest("this sandbox does not allow a local port")
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)

        class FakeBrowser:
            port = 1
            def _attached_now(self): return False
            def start(self): pass
            def _version(self, port): return {"webSocketDebuggerUrl": "ws://127.0.0.1:1/x"}
        with mock.patch.object(browser, "get", lambda *a, **k: FakeBrowser()), \
                mock.patch.object(browser, "WS", lambda url: mock.Mock()), \
                mock.patch.object(browser, "cdp_session", lambda *a, **k: None):
            s = socket.create_connection(srv.server_address, timeout=5)
            self.addCleanup(s.close)
            s.sendall((f"GET /cdp/ag1?project=web&token={browser.cdp_token('web', 'ag1')} HTTP/1.1\r\n"
                       f"Host: 127.0.0.1:{srv.server_address[1]}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                       "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
            first = s.makefile("rb").readline().decode()
        self.assertTrue(first.startswith("HTTP/1.1 101 "), first)

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
        b = browser.get("web", "ag1")  # the CDP address of ag1 reaches ag1's own browser (#5636)
        self.addCleanup(b.close)
        with self.assertRaisesRegex(OSError, "403"):
            browser.WS(browser.cdp_url(self.c, "web", "ag1").replace("token=", "token=x"))
        ws = browser.WS(browser.cdp_url(self.c, "web", "ag1"), timeout=WS_WAIT)
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
        b = browser.get("web", "ag1")
        self.addCleanup(b.close)
        ws = browser.WS(browser.cdp_url(self.c, "web", "ag1"), timeout=WS_WAIT)
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
