"""The browser of a project in the side panel (#4631): the WebSocket framing, the network rule, the keys, the
controller rule, the project of a command, maxpm browser, the page's routes, and one real Chrome when there is one."""

import base64
import contextlib
import http.server
import io
import json
import os
import re
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import chrome, timing
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


class Guide(unittest.TestCase):
    """#5683: every session reads the skill, so its browser section is short; maxpm browser -h has the rest."""

    def test_the_skill_names_the_browser_in_few_words_and_the_help_has_the_rest(self):
        text = (cli.GUIDES / "maxpm" / "SKILL.md").read_text(encoding="utf-8")
        section = text.split("## The project's browser\n", 1)[1].split("\n## ", 1)[0]
        self.assertLess(len(section.split()), 100)
        for words in ("maxpm browser open", "snapshot", "find", "text", 'click "Comments"', "--in",
                      "never type a password", "maxpm browser -h"):
            self.assertIn(words, section)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["browser", "-h"])
        h = out.getvalue()
        for op in ("outline", "snapshot", "find", "text", "html", "extract", "screenshot", "hover", "scroll", "drag",
                   "upload", "image", "dialog", "batch", "size", "errors", "record", "steps", "run", "control", "give",
                   "annotation"):
            self.assertRegex(h, rf"\n    {op} ")
        for rule in ("Act by name", "--in", "control --take", "browser_take_after", "Execution context was destroyed",
                     "browser_allow", "never type a password", "Never solve a CAPTCHA", "screenshot <target> --grid"):
            self.assertIn(rule, h)


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
        self.assertEqual(self.asked("click", "--selector", "a.more")[0]["selector"], "css:a.more")
        with mock.patch.object(cli, "ask_server", lambda *a, **k: {}), self.assertRaisesRegex(RiverError, "x and y"):
            self.run_cli("-q", "--as", "ag1", "browser", "click", "10")

    def test_a_point_inside_an_element_and_its_picture(self):
        """#5689: --at on click, hover, scroll and drag (--to-at: where a drag ends); screenshot <target> --grid."""
        self.assertEqual(self.asked("click", "Map", "--at", "30%,70%")[0]["at"], "30%,70%")
        self.assertEqual(self.asked("hover", "@e4", "--at", "12,40")[0]["at"], "12,40")
        self.assertEqual(self.asked("scroll", "Map", "--at", "50%,50%")[0]["at"], "50%,50%")
        a = self.asked("drag", "@e5", "@e5", "--at", "10%,10%", "--to-at", "90%,90%")[0]
        self.assertEqual((a["selector"], a["to"], a["at"], a["to_at"]), ("@e5", "@e5", "10%,10%", "90%,90%"))
        a = self.asked("screenshot", "Map", "--grid", res={"project": "web", "png": base64.b64encode(b"x").decode(),
                                                          "element": {"target": "@e7", "box": [300, 200], "scale": 2,
                                                                      "picture": [600, 400], "grid": True}})
        self.assertEqual((a[0]["selector"], a[0]["grid"]), ("Map", True))
        self.assertIn("(600x400 pixels of a box of 300x200, scale 2; the grid lines are each 10%; a point: maxpm "
                      "browser click @e7 --at <x%>,<y%>)", a[1])
        self.assertEqual(browser.parse_at("30%, 70%"), [30.0, True, 70.0, True])
        self.assertEqual(browser.parse_at("12,40.5"), [12.0, False, 40.5, False])
        with self.assertRaises(RiverError):
            browser.parse_at("left")
        self.assertEqual(browser.step_words({"action": "click", "selector": "Map", "at": "30%,70%"}),
                         ["click", "Map", "--at", "30%,70%"])

    def test_the_other_pointer_actions(self):
        """#5669: click --double --right --with; hover a name or a point; scroll [target] [amount]; drag from to."""
        a = self.asked("click", "Row", "--double", "--right", "--with", "shift,Ctrl")[0]
        self.assertEqual((a["count"], a["button"], a["mods"]), (2, "right", ["shift", "ctrl"]))
        self.assertEqual(self.asked("hover", "link", "Products")[0]["selector"], 'link "Products"')
        self.assertEqual((lambda a: (a["x"], a["y"]))(self.asked("hover", "10", "20")[0]), (10.0, 20.0))
        a = self.asked("scroll", "Results", "-300")[0]
        self.assertEqual((a["selector"], a["amount"]), ("Results", "-300"))
        self.assertEqual(self.asked("scroll")[0].get("amount"), None)
        a = self.asked("drag", "Card", "120,40")[0]
        self.assertEqual((a["selector"], a["to"]), ("Card", [120.0, 40.0]))
        _, out = self.asked("click", "Row", "--double", res={"title": "X", "url": "u", "count": 2})
        self.assertEqual(out.strip(), "X  u  (double left click)")

    def test_an_action_takes_a_name_a_role_and_name_and_its_row(self):
        """#5667: click "Comments", click link Comments (two words), --in "<row>"; fill "Month" 2026-10."""
        self.assertEqual(self.asked("click", "Comments")[0]["selector"], "Comments")
        a = self.asked("click", "link", "Comments", "--in", "Invoice 1042")[0]
        self.assertEqual((a["selector"], a["within"]), ('link "Comments"', "Invoice 1042"))
        self.assertEqual(self.asked("fill", "Month", "2026-10")[0]["selector"], "Month")
        self.assertEqual(self.asked("type", "--selector", "#q", "hi")[0]["selector"], "css:#q")
        with mock.patch.object(cli, "ask_server", lambda *a, **k: {}), \
                self.assertRaisesRegex(RiverError, 'name its role first \\(link "Two"\\)'):
            self.run_cli("-q", "--as", "ag1", "browser", "click", "One", "Two")

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

    def test_dialog_answers_the_box_and_an_action_prints_the_box(self):
        """#5674: maxpm browser dialog accept [text] | dismiss, and the line of a box that waits."""
        a, _ = self.asked("dialog", "accept", "Bob")
        self.assertEqual((a["action"], a["accept"], a["text"]), ("dialog", True, "Bob"))
        a, _ = self.asked("dialog", "dismiss")
        self.assertEqual((a["accept"], a["text"]), (False, None))
        with self.assertRaisesRegex(RiverError, "dismiss takes no text"):
            self.asked("dialog", "dismiss", "x")
        box = {"type": "prompt", "message": "Your name?", "default": "Ann", "url": "u"}
        _, out = self.asked("click", "@e3", res={"title": "X", "url": "u", "navigated": False, "dialog": box})
        self.assertEqual(out.splitlines(), ["X  u", 'dialog prompt "Your name?" (default "Ann"): maxpm browser '
                                                    'dialog accept "<text>", or dismiss'])
        _, out = self.asked("status", res={"project": "web", "running": True, "title": "X", "url": "u", "dialog": box})
        self.assertIn('  dialog prompt "Your name?"', out)
        _, out = self.asked("dialog", "accept", "Bob", res={"title": "X", "url": "u", "answered": {
            "type": "prompt", "message": "Your name?", "accept": True}})
        self.assertEqual(out.strip(), "X  u  (accepted the prompt box)")

    def test_a_box_ends_the_wait_of_a_call_and_refuses_the_next_one(self):
        """#5674, with no Chrome: a box that opens ends the call that waits for the page; a later call fails at once
        and names it; a box that asks to leave the page is accepted; the close lets calls through again."""
        b = browser.get("web")
        sent = []

        class WS:
            def send(self, text):
                sent.append(json.loads(text))
        b.ws, b.session, b.target = WS(), "S1", "T1"
        b.start = lambda window=None: None
        got = {}

        def click():
            try:
                b.page("Input.dispatchMouseEvent", {"type": "mouseReleased"})
            except RiverError as e:
                got["e"] = e
        t = threading.Thread(target=click)
        t.start()
        while not b.wait:
            time.sleep(0.01)
        b._event("Page.javascriptDialogOpening", {"type": "beforeunload", "message": ""}, "S1")
        self.assertEqual((sent[-1]["method"], sent[-1]["params"], b.dialog), ("Page.handleJavaScriptDialog",
                                                                               {"accept": True}, None))
        b._event("Page.javascriptDialogOpening", {"type": "confirm", "message": "Delete this?"}, "S1")
        t.join(2)
        self.assertIsInstance(got["e"], browser.DialogOpen)
        self.assertEqual(b.info()["dialog"], {"type": "confirm", "message": "Delete this?", "default": "", "url": ""})
        n = len(sent)
        with self.assertRaisesRegex(RiverError, 'confirm box "Delete this\\?": maxpm browser dialog accept, or'):
            b.evaluate("1")
        self.assertEqual(len(sent), n)  # nothing went to Chrome
        b._event("Page.javascriptDialogClosed", {"result": True}, "S1")
        self.assertIsNone(b.dialog)

    def test_snapshot_prints_refs_a_delta_and_what_it_left_out(self):
        snap = {"lines": ["# Sign up", '@e1 textbox "Name" value="Bob"'], "more": 3, "frames": 1,
                "url": "https://x.test/", "title": "Form"}
        a, out = self.asked("snapshot", "-i", "--scope", "form", res={"snapshot": snap})
        self.assertEqual((a["interactive"], a["scope"], a["delta"]), (True, "form", False))
        self.assertEqual(out.splitlines(), [
            "page: Form  https://x.test/", "# Sign up", '@e1 textbox "Name" value="Bob"',
            "... 3 more: --region <name> or --scope '<css>' for a part of the page, or --max <n>",
            "(1 frame not listed: open the frame's address)"])
        _, out = self.asked("snapshot", "--delta", res={"snapshot": {**snap, "delta": True, "lines": [], "more": 0,
                                                                     "frames": 0, "removed": ["@e2", "@e4"]}})
        self.assertEqual(out.splitlines()[1:], ["(no new or changed element)", "gone: @e2 @e4"])

    def test_the_window_a_region_find_and_html_scope(self):
        """#5671: the default lists the window and names the way to the rest; --all, --region and find; html --scope
        says the size apart from the HTML."""
        snap = {"lines": ['@e1 link "Home"'], "more": 0, "outside": 40, "regions": ["nav", "main"], "frames": 0,
                "url": "u", "title": "T"}
        a, out = self.asked("snapshot", "-i", res={"snapshot": snap})
        self.assertEqual((a["all"], a["region"]), (False, None))
        self.assertEqual(out.splitlines()[-1], '... 40 more out of the window: --all, --region <nav|main>, or maxpm '
                                               'browser find "<word>"')
        a, _ = self.asked("snapshot", "--all", "--region", "main", res={"snapshot": snap})
        self.assertEqual((a["all"], a["region"]), (True, "main"))
        a, out = self.asked("find", "invoice", "1042", res={"snapshot": {**snap, "outside": 0}})
        self.assertEqual((a["action"], a["find"], a["interactive"]), ("snapshot", "invoice 1042", True))
        self.assertEqual(out.splitlines(), ["page: T  u", '@e1 link "Home"'])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            a, out = self.asked("html", "--scope", "@e3", res={"html": "<b>x</b>", "bytes": 8})
        self.assertEqual((a["scope"], out.strip(), err.getvalue().strip()), ("@e3", "<b>x</b>", "maxpm: 8 bytes of HTML"))
        # #5670: open asks for the outline and prints it under the page line; --no-outline does not ask.
        a, out = self.asked("open", "x.test", res={"title": "X", "url": "u", "outline": ["[main] 3 links"]})
        self.assertEqual((a["outline"], out.splitlines()), (True, ["X  u", "[main] 3 links"]))
        self.assertFalse(self.asked("open", "x.test", "--no-outline")[0]["outline"])
        a, out = self.asked("outline", res={"title": "X", "url": "u", "outline": ["[page] 1 link"]})
        self.assertEqual((a["action"], out.splitlines()), ("outline", ["X  u", "[page] 1 link"]))

    def test_text_takes_a_section_grep_from_and_says_what_is_left(self):
        """#5672: maxpm browser text --section/--grep/--from/--max/--all, and open --text."""
        a, out = self.asked("text", "--section", "Production", "--from", "120", res={
            "title": "X", "url": "u", "text": "Production\nmore", "rest": 2500, "next": 3800, "section": "Production"})
        self.assertEqual((a["section"], a["from"]), ("Production", 120))
        self.assertEqual(out.splitlines(), ["Production", "more", '... 2.5 KB more: --section "Production" --from 3800'])
        a, out = self.asked("text", "--grep", "battery", res={"text": "a battery", "rest": 0, "matches": 1})
        self.assertEqual((a["grep"], out.splitlines()), ("battery", ["(1 line with the word)", "a battery"]))
        a, out = self.asked("text", res={"text": "Intro", "rest": 101200, "next": 3775})
        self.assertNotIn("section", a)
        self.assertEqual(out.splitlines()[-1], '... 101.2 KB more: --from 3775, --section "<heading>" (maxpm browser '
                                               'outline lists them), --grep <word>, or --all')
        a, out = self.asked("open", "x.test", "--text", "--no-outline", res={"title": "X", "url": "u", "text": "Hi",
                                                                             "rest": 0})
        self.assertEqual((a["read"], out.splitlines()), (True, ["X  u", "Hi"]))

    def test_extract_reads_the_fields_and_prints_one_row_a_line(self):
        """#5677: name=<css>, name=<css>@attr, name=@attr; the JSON array with one row on each line."""
        self.assertEqual(browser.parse_field("title=.titleline > a"), ["title", ".titleline > a", None])
        self.assertEqual(browser.parse_field("link=a[href^='item']@href"), ["link", "a[href^='item']", "href"])
        self.assertEqual(browser.parse_field("id=@data-id"), ["id", "", "data-id"])
        with self.assertRaisesRegex(RiverError, "a field is name=<css>"):
            browser.parse_field("title")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            a, out = self.asked("extract", "t=a", "--rows", "@e12", "--limit", "2", res={
                "data": [{"t": "One"}, {"t": None}], "count": 30})
        self.assertEqual((a["rows"], a["fields"], a["limit"]), ("@e12", [["t", "a", None]], 2))
        self.assertEqual(out.splitlines(), ['[{"t":"One"},', '{"t":null}]'])
        self.assertEqual(json.loads(out), [{"t": "One"}, {"t": None}])
        self.assertIn("2 of 30 rows: --limit <n> for more", err.getvalue())

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
        # #5705: on a slow runner the held batches passed INPUT_GAP before the check; the gap scales on CI.
        with patches[0], patches[1], mock.patch.object(browser, "INPUT_GAP", timing.limit(browser.INPUT_GAP)):
            inp(2, "b")
            inp(3, "c")
            inp(1, "a", "p2")  # another pane counts on its own
            timing.wait_for(lambda: self._keys(got), 5, "the other pane's key")
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
        # #5705: a GitHub macOS runner took longer than 0.15 s between two frames of the test, so a sharp frame came
        # while the page moved; the quiet time scales on CI.
        p = mock.patch.object(browser, "SHARP_QUIET", timing.limit(0.2))
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
        for i in range(10):  # a scroll: new frames one after the other, each one shown at once
            self.paint(b"frame %d" % i)
            self.assertEqual((b.frame["data"], b.frame.get("sharp")), (base64.b64encode(b"frame %d" % i).decode(), None))
            browser.frame(b, b.seq, 0.01, "pane")  # the panel polls while it watches
        self.assertEqual(self.shots, [])
        # The page holds still: one sharp frame.
        timing.wait_for(lambda: b.frame.get("sharp"), 10, "the sharp frame")
        self.assertEqual(self.shots, ["Page.captureScreenshot"])
        self.paint(b"frame 9")  # the screenshot painted the page again: the sharp frame stays
        self.assertTrue(b.frame.get("sharp"))
        self.paint(b"frame 10")  # the page changes: the new frame at once
        self.assertIsNone(b.frame.get("sharp"))
        timing.wait_for(lambda: b.frame.get("sharp"), 10, "the next sharp frame")
        self.assertEqual(len(self.shots), 2)

    def test_the_echo_of_the_screenshot_before_its_reply_keeps_the_sharp_frame(self):
        """#5706: the screenshot's repaint can send its echo frame before the screenshot's reply comes (a loaded
        computer). The echo is no change: the screenshot stays the sharp frame. Before, each echo threw the screenshot
        away and asked for another, whose echo did the same ('no sharp frame came', one run in three)."""
        b = self.b
        b.call = lambda method, params=None, session=None, timeout=None: (
            self.shots.append(method), self.paint(b"frame 1"), {"data": base64.b64encode(b"sharp").decode()})[-1]
        self.paint(b"frame 1")
        timing.wait_for(lambda: b.frame.get("sharp"), 3, "the sharp frame")
        self.assertEqual(self.shots, ["Page.captureScreenshot"])
        self.paint(b"frame 2")  # a real change during no screenshot: shown at once, and the next sharp frame follows
        self.assertIsNone(b.frame.get("sharp"))
        timing.wait_for(lambda: b.frame.get("sharp"), 3, "the next sharp frame")


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
        got, polls, seq = [], [], b.seq
        for _ in range(2):
            t = threading.Thread(target=lambda: got.append(browser.frame(b, seq, timing.limit(3), "p", many=True)))
            t.start()
            polls.append(t)
        timing.wait_for(lambda: b.watching == 2, 5, "two polls that wait")
        self.put(b, b"two")
        timing.wait_for(lambda: len(got) == 1 and b.watching == 1, 5, "one poll that took the change")
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
        if os.name != "nt":  # #5716: no component and model downloads into the profile; the page works as above
            import subprocess
            ran = subprocess.run(["ps", "-ww", "-o", "args=", "-p", str(b.state["pid"])], capture_output=True,
                                 text=True).stdout
            self.assertIn("--disable-component-update", ran)
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
        sharp = None
        for _ in range(int(timing.limit(50))):  # it failed once on a loaded Mac in the full run (#5705)
            sharp = b.frame if b.frame.get("sharp") else None  # read once: a new screencast frame can follow it
            if sharp:
                break
            f = browser.frame(b, f["seq"], 1)
        self.assertIsNotNone(sharp, "no sharp frame came")  # #5706: SharpWhenStill has the race that it lost
        self.assertEqual((sharp["w"], sharp["h"], jpeg_size(sharp["data"])), (500, 400, (1000, 800)))
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


class Accounts(Base):
    """#5811: the person names once which account agents may use on a site; a session's reply names it there."""

    def run_cli(self, *words):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            cli.run(["-q", *words])
        return out.getvalue()

    def test_pairs_parse_and_a_bad_value_is_refused(self):
        self.assertEqual(browser.parse_accounts("https://www.Google.com=me@gmail.com, github.com=me x.com=me x.com=me"),
                         [("google.com", "me@gmail.com"), ("github.com", "me"), ("x.com", "me")])
        with self.assertRaisesRegex(RiverError, "site=account pairs, not 'google.com'"):
            core.config_set(self.c, "browser_accounts", "google.com")

    def test_add_list_and_remove_keep_the_other_pairs(self):
        browser.account_change(self.c, "google.com", "me@gmail.com")
        browser.account_change(self.c, "google.com", "two@gmail.com")
        browser.account_change(self.c, "github.com", "me", project="web")
        browser.account_change(self.c, "google.com", "me@gmail.com")  # twice: one pair
        self.assertEqual(core.setting(self.c, "browser_accounts"), "google.com=me@gmail.com google.com=two@gmail.com")
        self.assertEqual(browser.accounts(self.c, "web"),
                         [("google.com", "me@gmail.com"), ("google.com", "two@gmail.com"), ("github.com", "me")])
        self.assertEqual(browser.accounts(self.c, "other"), [("google.com", "me@gmail.com"), ("google.com", "two@gmail.com")])
        browser.account_change(self.c, "google.com", "two@gmail.com", remove=True)
        browser.account_change(self.c, "github.com", project="web", remove=True)  # no account: every pair of the site
        self.assertEqual(browser.accounts(self.c, "web"), [("google.com", "me@gmail.com")])
        self.assertIsNone(self.c.execute("SELECT 1 FROM settings WHERE scope='project:web'").fetchone())
        with self.assertRaisesRegex(RiverError, "no account is listed for x.com for every project"):
            browser.account_change(self.c, "x.com", remove=True)
        with self.assertRaisesRegex(RiverError, "name the account"):
            browser.account_change(self.c, "x.com", "")

    def test_the_command(self):
        self.assertIn("no accounts listed", self.run_cli("browser", "accounts"))
        self.run_cli("browser", "accounts", "add", "google.com", "me@gmail.com")
        self.run_cli("browser", "accounts", "add", "github.com", "me", "--project", "web")
        out = self.run_cli("browser", "accounts")
        self.assertIn("google.com  me@gmail.com  (every project)", out)
        self.assertIn("github.com  me  (project web)", out)
        self.run_cli("browser", "accounts", "remove", "github.com", "--project", "web")
        self.assertNotIn("github.com", self.run_cli("browser", "accounts"))

    def test_the_reply_names_the_account_on_a_sign_in_page_or_says_how_to_record_one(self):
        browser.account_change(self.c, "google.com", "me@gmail.com")
        note = browser.account_note
        self.assertIn("accounts agents may use on accounts.google.com: me@gmail.com",
                      note(self.c, "web", "https://accounts.google.com/v3/signin/identifier"))
        self.assertIn("on mail.google.com: me@gmail.com", note(self.c, "web", "https://mail.google.com/", opened=True))
        self.assertIsNone(note(self.c, "web", "https://mail.google.com/mail/u/0"))  # signed in: no line on each page
        self.assertIn("maxpm browser accounts add notion.so <account>", note(self.c, "web", "https://www.notion.so/login"))
        self.assertIn("maxpm browser accounts add acme.io <account>", note(self.c, "web", "https://sso.acme.io/"))
        for url in ("https://example.com/author/x", "about:blank", "https://example.com/"):
            self.assertIsNone(note(self.c, "web", url, opened=True), url)


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
        self.assertLess(time.time() - t0, timing.limit(2.0, below=browser.NAV_START))
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


# A page whose script follows its links as a client route (as Next.js does): the address changes 0.6 s after the click,
# the title a moment later.
ROUTED = (b"<title>Guide</title><a id=r href=/reviews/x>Review X</a> <a href=#part>Part</a> "
          b"<button onclick=\"history.pushState({}, '', '/later'); document.title = 'Later'\">Route</button>"
          b"<script>document.getElementById('r').addEventListener('click', e => { e.preventDefault();"
          b" setTimeout(() => { history.pushState({}, '', '/reviews/x');"
          b" setTimeout(() => { document.title = 'Review X' }, 100) }, 600) })</script>")


class ClientRoutes(Base):
    """#5717 in a real Chrome: after the page's script changes the address, the reply of the action, the snapshot's
    header and status name the new address and title, not the page before."""

    def test_a_route_of_the_pages_script_is_the_page_the_replies_name(self):
        if not chrome() or not browser.chrome_path():
            self.skipTest("no Chrome on this computer")
        if not _can_bind():
            self.skipTest("this sandbox does not allow a local port")

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(ROUTED)))
                self.end_headers()
                self.wfile.write(ROUTED)

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
        b.open(f"http://127.0.0.1:{port}/guide")
        lines = browser.snapshot(b, interactive=True)["snapshot"]["lines"]
        ref = {line.split('"')[1]: line.split()[0] for line in lines}
        # A link that the script follows later: the click waits for the new address and its title.
        t0 = time.time()
        out = browser.click(b, ref["Review X"])
        self.assertLess(time.time() - t0, timing.limit(1.5, below=browser.NAV_START + browser.ROUTE_TITLE))
        self.assertEqual((out["navigated"], out["url"], out["title"]),
                         (True, f"http://127.0.0.1:{port}/reviews/x", "Review X"))
        self.assertEqual(browser.snapshot(b, interactive=True)["url"], f"http://127.0.0.1:{port}/reviews/x")
        # A link to a part of the page is no new page, and it does not wait for a title.
        t0 = time.time()
        out = browser.click(b, ref["Part"])
        self.assertLess(time.time() - t0, timing.limit(0.5, below=browser.ROUTE_TITLE))
        self.assertEqual((out["navigated"], out["url"]), (False, f"http://127.0.0.1:{port}/reviews/x#part"))
        # A button whose script changes the address and the title at once.
        out = browser.click(b, ref["Route"])
        self.assertEqual((out["url"], out["title"]), (f"http://127.0.0.1:{port}/later", "Later"))
        # A change that no action made: status reads the page again.
        b.evaluate("history.pushState({}, '', '/elsewhere'); document.title = 'Elsewhere'; 1")
        timing.wait_for(lambda: b.stale, what="Chrome's event of the new address")
        res = routes.act(self.c, {"action": "status", "project": "web"}, None)
        self.assertEqual((res["url"], res["title"]), (f"http://127.0.0.1:{port}/elsewhere", "Elsewhere"))


LONG = (b"<title>Invoices</title><nav aria-label=Site><a href=/>Home</a> <a href=/help>Help</a></nav><main>"
        b"<table><thead><tr><th>Invoice</th><th>Status</th><th>Actions</th></tr></thead><tbody>"
        + b"".join(b"<tr><td><a href=/i/%d>Invoice %d</a></td><td>paid</td><td><button>Edit</button>"
                   b"<button class='btn icon-trash' title=''></button></td></tr>" % (n, n) for n in (1041, 1042, 1043))
        + b"</tbody></table><section><h2>Card A</h2><button>Remove</button></section>"
          b"<section><h2>Card B</h2><button>Remove</button></section><button data-testid=chat-open></button>"
          b"<div style='height:3000px'></div>"
        + b"".join(b"<a href=/far/%d>Far link %d</a> " % (n, n) for n in range(25)) + b"</main>")


class SnapshotLimits(Base):
    """#5671 in a real Chrome: the window by default with a count of the rest, repeated rows folded with the names
    that every row has once, a name that says too little with its card or a test id, a region, find, html --scope."""

    def test_the_window_rows_context_region_find_and_html_scope(self):
        if not chrome() or not browser.chrome_path():
            self.skipTest("no Chrome on this computer")
        if not _can_bind():
            self.skipTest("this sandbox does not allow a local port")

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(LONG)))
                self.end_headers()
                self.wfile.write(LONG)

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
        act = lambda **a: routes.act(self.c, {"project": "web", **a}, None)  # noqa: E731
        act(action="open", url=f"http://127.0.0.1:{port}/")
        s = act(action="snapshot", interactive=True)["snapshot"]
        text = "\n".join(s["lines"])
        self.assertEqual((s["outside"], s["regions"]), (25, ["nav", "main"]), text)
        self.assertNotIn("Far link", text)
        self.assertEqual(s["lines"][0], '[nav "Site"]')
        rows = [x for x in s["lines"] if x.startswith("  ")]
        self.assertIn('3 rows (each ends with "Edit", button class=icon-trash):', s["lines"])
        self.assertEqual(len(rows), 3, text)
        self.assertRegex(rows[1], r'^  2\. @e\d+ "Invoice 1042" · @e\d+ @e\d+$')
        self.assertRegex(text, r'@e\d+ button "Remove" \(in "Card A"\)\n@e\d+ button "Remove" \(in "Card B"\)')
        self.assertRegex(text, r'@e\d+ button "" testid=chat-open')
        # A region; find, also by the row's text; one element's HTML by its ref, with its size.
        nav = act(action="snapshot", interactive=True, region="site")["snapshot"]["lines"]
        self.assertEqual([x.split(" ", 1)[1] for x in nav], ['link "Home"', 'link "Help"'])
        with self.assertRaisesRegex(RiverError, 'no region "footer" on the page; it has nav "Site", main'):
            act(action="snapshot", region="footer")
        found = act(action="snapshot", interactive=True, find="card b")["snapshot"]["lines"]
        self.assertEqual(len(found), 2, found)  # the region line and the button
        self.assertRegex(found[1], r'button "Remove" \(in "Card B"\)')
        far = act(action="snapshot", interactive=True, find="far link 24")["snapshot"]["lines"]
        self.assertRegex(far[-1], r'^@e\d+ link "Far link 24"$')
        ref = rows[0].split()[1]
        h = act(action="html", scope=ref)
        self.assertEqual((h["html"], h["bytes"]), ('<a href="/i/1041">Invoice 1041</a>', 34))
        everything = act(action="snapshot", interactive=True, all=True)["snapshot"]
        self.assertEqual(everything["outside"], 0)
        self.assertIn("Far link 24", "\n".join(everything["lines"]))

        # #5667: an action by name, with no snapshot first. One match: its ref, the same as the snapshot's.
        lines = "\n".join(everything["lines"])
        ref_of = lambda pat: "@" + __import__("re").search(r"@(e\d+) " + pat, lines).group(1)  # noqa: E731
        t0 = time.time()
        self.assertEqual(browser.resolve_target(b, "Invoice 1042"), rows[1].split()[1])
        self.assertLess(time.time() - t0, timing.limit(0.1))  # p147: no page load, under 100 ms
        self.assertEqual(browser.resolve_target(b, 'link "Far link 24"'), ref_of('link "Far link 24"'))
        self.assertEqual(browser.resolve_target(b, "Remove", "card b"), ref_of(r'button "Remove" \(in "Card B"\)'))
        self.assertEqual(browser.resolve_target(b, "Edit", "Invoice 1043"), rows[2].split()[-2])
        with self.assertRaisesRegex(RiverError, '^11 elements are "Far link 1…"'):  # a cut name: the names so started
            browser.resolve_target(b, "Far link 1…")
        # Several: the candidates with their rows, and nothing happens.
        with self.assertRaises(RiverError) as e:
            act(action="click", selector="Edit")
        msg = str(e.exception)
        self.assertTrue(msg.startswith('3 elements are "Edit"; nothing was done. Name one: maxpm browser click @e<n>'), msg)
        self.assertIn('button "Edit" (in "Invoice 1042")', msg)
        # None: the nearest names and the way on; a CSS selector that matches still works.
        with self.assertRaisesRegex(RiverError, 'no element is button "Home"; the nearest: link "Home". Else: maxpm '
                                                'browser html --scope'):
            act(action="click", selector='button "Home"')
        self.assertEqual(browser.resolve_target(b, "nav a"), "css:nav a")
        h = act(action="html", scope="Invoice 1041")
        self.assertEqual(h["html"], '<a href="/i/1041">Invoice 1041</a>')
        out = act(action="click", selector="Help")
        self.assertEqual((out["navigated"], out["url"]), (True, f"http://127.0.0.1:{port}/help"))

        # #5670: open replies with the outline (for a session; the pane asks for none); maxpm browser outline.
        want = ['[nav "Site"] 2 links', "[main] 28 links, 9 buttons, 3 rows", "  ## Card A", "  ## Card B"]
        self.assertEqual(act(action="open", url=f"http://127.0.0.1:{port}/", outline=True)["outline"], want)
        self.assertNotIn("outline", act(action="open", url=f"http://127.0.0.1:{port}/"))
        self.assertEqual(act(action="outline", heads=1)["outline"], want[:3] + ["(1 more headings: maxpm browser "
                                                                                 "snapshot --all)"])

        # #5677: extract, rows by CSS and by the ref (or name) of an element in a repeated row; a missing field is null.
        f = [browser.parse_field(x) for x in ("inv=a", "link=a@href", "status=td:nth-child(2)", "note=.none")]
        res = act(action="extract", rows="tbody tr", fields=f)
        self.assertEqual((res["count"], res["data"][1]), (3, {"inv": "Invoice 1042", "status": "paid", "note": None,
                                                              "link": f"http://127.0.0.1:{port}/i/1042"}))
        by_name = act(action="extract", rows="Invoice 1043", fields=f[:1], limit=2)
        self.assertEqual((by_name["count"], by_name["data"]), (3, [{"inv": "Invoice 1041"}, {"inv": "Invoice 1042"}]))
        self.assertEqual(act(action="extract", fields=[browser.parse_field("card=h2")])["data"], [{"card": "Card A"}])
        with self.assertRaisesRegex(RiverError, "not a CSS selector: td:::x"):
            act(action="extract", rows="tbody tr", fields=[["x", "td:::x", None]])
        with self.assertRaisesRegex(RiverError, "is in no repeated row: name the rows with --rows"):
            act(action="extract", rows="Help")


COVER = (b"<title>Cover</title><style>.f{position:fixed;left:0}</style>"
         b"<button class=f style='bottom:10px;width:200px;height:40px' onclick=\"document.title='bottom'\">Bottom</button>"
         b"<div role=dialog aria-label=Cookies class=f style='bottom:0;right:0;height:120px;background:#eee'>"
         b"<p>We use cookies</p><button onclick='this.parentNode.remove()'>Accept</button></div>"
         b"<button class=f style='top:100px;width:300px;height:40px' onclick=\"document.title='half'\">Half</button>"
         b"<div class=f style='top:90px;left:100px;width:100px;height:60px;background:red'></div>"
         b"<div style='margin-top:200px'><button id=t onclick=\"document.title='target'\">Target</button>"
         b"<button onclick='t.replaceWith(t.cloneNode(true))'>Redraw</button>"
         b"<button onclick=\"const x = document.createElement('button'); x.textContent = 'Target'; "
         b"t.after(x)\">Twin</button></div>")


class Aim(Base):
    """#5668 in a real Chrome: a button under a cookie banner is not clicked and the reply names the banner and its
    buttons; a button half covered is clicked where it shows; a ref of a button that the page drew again is found
    again by its role and name, and two such buttons are named instead."""

    def test_a_covered_element_is_not_clicked_and_a_redrawn_one_is_found_again(self):
        if not chrome() or not browser.chrome_path():
            self.skipTest("no Chrome on this computer")
        if not _can_bind():
            self.skipTest("this sandbox does not allow a local port")

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(COVER)))
                self.end_headers()
                self.wfile.write(COVER)

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
        act = lambda **a: routes.act(self.c, {"project": "web", **a}, None)  # noqa: E731
        act(action="open", url=f"http://127.0.0.1:{port}/")
        with self.assertRaisesRegex(RiverError, r'^@e\d+ is covered by @e\d+ dialog "Cookies"; in it: @e\d+ button '
                                                r'"Accept"; nothing was clicked'):
            act(action="click", selector="Bottom")
        self.assertEqual(b.evaluate("document.title"), "Cover")
        act(action="click", selector="Half")  # its middle is under the red box; its left part shows
        self.assertEqual(b.evaluate("document.title"), "half")
        act(action="click", selector="Accept")
        act(action="click", selector="Bottom")
        self.assertEqual(b.evaluate("document.title"), "bottom")
        # The page draws Target again: its old ref finds the new one, and the reply says so.
        lines = act(action="snapshot", interactive=True)["snapshot"]["lines"]
        ref = next(x for x in lines if '"Target"' in x).split()[0]
        act(action="click", selector="Redraw")
        out = act(action="click", selector=ref)
        self.assertEqual(b.evaluate("document.title"), "target")
        self.assertRegex(out["notes"][0], rf'^{ref} was drawn again; it is @e\d+ button "Target" now$')
        self.assertIn(out["notes"][0], cli._browser_result("click", out))
        lines = act(action="snapshot", interactive=True)["snapshot"]["lines"]
        ref = next(x for x in lines if '"Target"' in x).split()[0]
        act(action="click", selector="Redraw")
        act(action="click", selector="Twin")
        with self.assertRaisesRegex(RiverError, f"{ref} is no longer on the page, and 2 elements have its role and name"):
            act(action="click", selector=ref)


FILES = (b"<title>Files</title><a href=/report.csv>Report</a> <label for=f>Choose a file</label>"
         b"<input id=f type=file style='display:none' onchange=\"document.title = 'got ' + this.files[0].name\">")


class PageSize(Base):
    """#5676 in a real Chrome: maxpm browser size phone gives the page 390 px with touch and a screenshot 390 px wide
    with no pane open; a pane that opens does not change it, the person's use of the pane does; size pane and
    <w>x<h>; status says who set it."""

    def test_size_by_command_and_the_pane(self):
        import struct as _struct
        if not chrome() or not browser.chrome_path():
            self.skipTest("no Chrome on this computer")
        if not _can_bind():
            self.skipTest("this sandbox does not allow a local port")

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"<title>Size</title><meta name=viewport content='width=device-width'><p>hello</p>"
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
        core.register(self.c, "ag1")
        b = browser.get("web", "ag1")
        self.addCleanup(b.close)
        act = lambda **a: routes.act(self.c, {"project": "web", **a}, "ag1")  # noqa: E731
        act(action="open", url=f"http://127.0.0.1:{port}/", show=False)
        out = act(action="size", size="phone")
        self.assertEqual((out["view"], out["forced"]["name"], out["forced"]["by"]), ([390, 844], "phone", "ag1"))
        self.assertTrue(b.evaluate("matchMedia('(pointer: coarse)').matches"))
        png = base64.b64decode(browser.screenshot(b)["png"])
        self.assertEqual(_struct.unpack(">II", png[16:24]), (390, 844))  # the PNG's width and height
        b.resize(1000, 700, 1, None, pane="p1")  # a pane opens: the size stays the session's
        self.assertEqual(b.evaluate("innerWidth"), 390)
        b.drive("p1")  # the person clicks in the pane: the pane's size again
        self.assertEqual((b.forced, b.evaluate("innerWidth"), b.evaluate("matchMedia('(pointer: coarse)').matches")),
                         (None, 1000, False))
        self.assertEqual(act(action="size", size="800x600")["view"], [800, 600])
        self.assertIn("size 800x600, set by ag1", self._status(b))
        act(action="size", size="pane")
        self.assertEqual(b.evaluate("innerWidth"), 1000)
        with self.assertRaisesRegex(RiverError, "a size is phone, desktop, pane, or <width>x<height>"):
            act(action="size", size="huge")

    def _status(self, b):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.render_browser(type("A", (), {"bcmd": "status"})(), b.info())
        return out.getvalue()


ERRS = (b"<title>Err</title><button onclick=\"fetch('/api/fail')\">Load</button>"
        b"<button onclick=\"console.error('bad thing'); throw new Error('boom')\">Break</button>"
        b"<button onclick=\"document.title = 'fine'\">Fine</button>")


class PageErrors(Base):
    """#5675 in a real Chrome: the reply of an action counts the page's errors and failed requests since it began
    (a request with status 500, console.error, an uncaught error), says nothing when there are none, and maxpm
    browser errors lists them."""

    def test_an_action_counts_its_errors_and_errors_lists_them(self):
        if not chrome() or not browser.chrome_path():
            self.skipTest("no Chrome on this computer")
        if not _can_bind():
            self.skipTest("this sandbox does not allow a local port")

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                fail = self.path == "/api/fail"
                body = b"no" if fail else ERRS
                self.send_response(500 if fail else 200)
                self.send_header("Content-Type", "text/plain" if fail else "text/html")
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
        core.register(self.c, "ag1")
        b = browser.get("web", "ag1")
        self.addCleanup(b.close)
        act = lambda **a: routes.act(self.c, {"project": "web", **a}, "ag1")  # noqa: E731
        act(action="open", url=f"http://127.0.0.1:{port}/", show=False)
        out = act(action="click", selector="Load")
        self.assertEqual(out["page_errors"], {"errors": 0, "requests": 1})
        self.assertIn("1 failed request (maxpm browser errors)", cli._browser_result("click", out))
        out = act(action="click", selector="Break")
        self.assertEqual(out["page_errors"], {"errors": 2, "requests": 0})
        self.assertIn("2 errors (maxpm browser errors)", cli._browser_result("click", out))
        self.assertNotIn("page_errors", act(action="click", selector="Fine"))
        errs = act(action="errors", clear=True)["errors"]
        self.assertEqual([(e["kind"], e.get("status") or e.get("source")) for e in errs],
                         [("request", 500), ("error", "console"), ("error", "exception")])
        self.assertEqual(errs[0]["url"], f"http://127.0.0.1:{port}/api/fail")
        self.assertIn("boom", errs[2]["message"])
        self.assertEqual(act(action="errors")["errors"], [])
        self.assertEqual(act(action="errors")["lines"], [])


def _render(fn, res):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        fn(res)
    return out.getvalue()


def _jpeg_size(data):
    """The width and height of a JPEG, from its start-of-frame block."""
    i = 2
    while i < len(data):
        kind, n = data[i + 1], int.from_bytes(data[i + 2:i + 4], "big")
        if kind in (0xC0, 0xC1, 0xC2):
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        i += 2 + n
    return None


class StepRecord(Base):
    """#5682 in a real Chrome: while a record is on, each action adds a step with its line (names, not refs), its page
    errors and a JPEG; a failed action is a step too, with its code and the picture where it broke; stop puts the
    steps as one HTML canvas of the project, and record step <n> gives one step and the path of its picture."""

    def test_a_record_of_a_walk_that_fails_becomes_a_canvas(self):
        if not chrome() or not browser.chrome_path():
            self.skipTest("no Chrome on this computer")
        if not _can_bind():
            self.skipTest("this sandbox does not allow a local port")

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                fail = self.path == "/api/fail"
                body = b"no" if fail else ERRS
                self.send_response(500 if fail else 200)
                self.send_header("Content-Type", "text/plain" if fail else "text/html")
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
        core.register(self.c, "ag1")
        b = browser.get("web", "ag1")
        self.addCleanup(b.close)
        self.addCleanup(browser.RECORDING.discard, b)
        act = lambda **a: routes.act(self.c, {"project": "web", **a}, "ag1")  # noqa: E731
        act(action="open", url=f"http://127.0.0.1:{port}/", show=False)  # before the record: no step
        out = act(action="record", record="start", name="Check-Walk")
        self.assertTrue(out["on"])
        self.assertIn("record check-walk is on", _render(cli._record_lines, out))
        with self.assertRaises(RiverError):
            act(action="record", record="start")
        act(action="reload")
        snap = act(action="snapshot", interactive=True)["snapshot"]["lines"]  # a read: no step
        ref = re.search(r'(@e\d+) button "Load"', "\n".join(snap)).group(1)
        act(action="click", selector=ref)
        act(action="batch", steps=[{"action": "click", "selector": "Fine"}])  # each step of a batch is a step
        with self.assertRaises(RiverError):
            act(action="click", selector="Nothing like it")
        st = act(action="record")
        self.assertEqual(st["record"]["steps"], 4)
        self.assertEqual(st["record"]["failed"], [4])
        self.assertEqual([s["line"] for s in st["steps"]],
                         ["reload", "click 'button \"Load\"'", "click Fine", "click 'Nothing like it'"])
        self.assertEqual(st["steps"][1]["errors"], [f"request GET http://127.0.0.1:{port}/api/fail 500"])
        self.assertEqual(st["steps"][3]["code"], "not_found")
        text = _render(cli._record_lines, st)
        self.assertIn("record check-walk: 4 steps, failed at step 4", text)
        self.assertIn("4 FAILED not_found  click 'Nothing like it'", text)
        step = act(action="record", record="step", n=4)["step"]
        self.assertFalse(step["ok"])
        jpg = Path(step["picture"]).read_bytes()
        self.assertEqual(jpg[:2], b"\xff\xd8")
        w, h = _jpeg_size(jpg)
        self.assertLessEqual(w, browser.SHOT_WIDTH)
        self.assertIn("picture: " + step["picture"], _render(cli._record_lines, {"step": step}))
        with self.assertRaises(RiverError):
            act(action="record", record="step", n=9)
        out = act(action="record", record="stop")
        self.assertFalse(b.rec)
        self.assertEqual(out["canvas"]["name"], "check-walk")
        page = core.canvas_show(self.c, "check-walk", project="web")["shown"]["body"]
        self.assertIn('<meta name="maxpm-kit">', page)
        self.assertIn('<li id="s4" data-tone="bad">', page)
        self.assertEqual(page.count("data:image/jpeg;base64,"), 4)
        self.assertIn("request GET", page)
        self.assertIn("canvas check-walk v1", _render(cli._record_lines, out))
        act(action="reload")  # the record is off: no step
        self.assertEqual(act(action="record")["record"]["steps"], 4)
        # A page larger than the canvas limit keeps the failed step's picture inline and names the other files
        small = browser.record_page(b.last_rec, 50_000 + len(jpg) * 2 + sum(len(json.dumps(s)) * 2 for s in
                                                                               b.last_rec["steps"]))
        self.assertIn(f'alt="The page after step 4" src="data:image/jpeg', small)
        self.assertIn("Picture: ", small)
        self.assertEqual(browser.step_words({"action": "drag", "selector": 'button "Card"', "to": [120, 40],
                                             "mods": ["alt"]}), ["drag", 'button "Card"', "120,40", "--with", "alt"])


def _png(w, h):
    """A grey PNG of w x h pixels."""
    import zlib

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    rows = b"".join(b"\x00" + b"\x80" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


IMAGES = (b"<title>Shop</title><h1>Chairs</h1>"
          b"<img id=logo src='/img/small.png' srcset='/img/small.png 100w, /img/big.png 400w' sizes='100px' alt='Logo' "
          b"width=100 height=50 onclick=\"document.title = 'logo'\">"
          b"<img src='/img/dot.png' alt='' width=16 height=16>"
          b"<img src='/img/banner.png' alt='' width=200 height=100>"
          b"<svg role=img width=60 height=40><title>Sales chart</title><rect width=60 height=40 fill=teal></rect></svg>"
          b"<ul>" + b"".join(b"<li><a href='/p/%d'><img src='/img/chair%d.png' alt='%s chair' width=120 height=90></a> "
                              b"<span>%s Chair</span> <img src='/img/star.png' alt='rating: 4.%d' width=60 height=12> "
                              b"<a href='/b/%d'>Buy</a></li>" % (i, i, c, c, i, i)
                              for i, c in enumerate((b"Blue", b"Red", b"Green"), 1)) + b"</ul>")
CAPTCHA = (b"<title>Check</title><p>Prove you are a person</p><div class=g-captcha>"
           b"<img src='/img/code.png' alt='letters' width=120 height=40></div>")


class Images(Base):
    """#5688 in a real Chrome: an image is an element (alt, file, size, its row), an image in a link is one line, a
    small image with no alt is not listed; images act by name; image saves the file at its largest size; a CAPTCHA
    test is named and never saved."""

    def test_images_are_elements_and_image_saves_the_file(self):
        if not chrome() or not browser.chrome_path():
            self.skipTest("no Chrome on this computer")
        if not _can_bind():
            self.skipTest("this sandbox does not allow a local port")
        sizes = {"small": (100, 50), "big": (400, 200), "dot": (16, 16), "banner": (200, 100), "star": (60, 12),
                 "code": (120, 40)}

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/img/"):
                    stem = self.path[5:].split(".")[0]
                    body, kind = _png(*sizes.get(stem, (120, 90))), "image/png"
                else:
                    body, kind = CAPTCHA if self.path == "/cap" else IMAGES, "text/html"
                self.send_response(200)
                self.send_header("Content-Type", kind)
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
        core.register(self.c, "ag1")
        b = browser.get("web", "ag1")
        self.addCleanup(b.close)
        act = lambda **a: routes.act(self.c, {"project": "web", **a}, "ag1")  # noqa: E731
        act(action="open", url=f"http://127.0.0.1:{port}/", show=False)
        lines = act(action="snapshot", interactive=True)["snapshot"]["lines"]
        text = "\n".join(lines)
        self.assertRegex(text, r'@e\d+ image "Logo" small\.png 100x50')
        self.assertRegex(text, r'@e\d+ image "Sales chart" svg 60x40')
        self.assertRegex(text, r'@e\d+ image "" banner\.png 200x100')  # large: listed, with no name
        self.assertNotIn("dot.png", text)  # small and no alt: a decoration
        self.assertRegex(text, r'"Blue chair" image=chair1\.png 120x90 @e\d+ image "rating: 4\.1" star\.png 60x12')
        self.assertEqual(text.count("chair1.png"), 1)  # the image in the link is the link's line
        self.assertIn("[page] 6 links, 6 images, 3 rows", act(action="outline")["outline"])
        found = act(action="snapshot", interactive=True, find="rating: 4.2")["snapshot"]["lines"]
        self.assertEqual(len(found), 1, found)
        self.assertEqual(act(action="click", selector='image "Logo"')["title"], "logo")
        out = act(action="image", selector="Logo")["image"]
        self.assertEqual((Path(out["path"]).name, out["type"], out["size"]), ("big.png", "image/png", [400, 200]))
        self.assertEqual(Path(out["path"]).parent, b.dir / "downloads")
        self.assertEqual(_render(lambda r: cli.render_browser(type("A", (), {"bcmd": "image"})(), r), {"image": out}),
                         f"saved {out['path']} (image/png, 400x200, {out['bytes'] / 1000:.1f} KB)\n")
        a1 = act(action="image", selector="Blue chair")["image"]
        a2 = act(action="image", selector="Blue chair")["image"]
        self.assertEqual((Path(a1["path"]).name, Path(a2["path"]).name), ("chair1.png", "chair1 (2).png"))
        self.assertEqual(a1["size"], [120, 90])
        svg = act(action="image", selector="Sales chart")["image"]
        self.assertEqual(svg["type"], "image/svg+xml")
        self.assertIn("<title>Sales chart</title>", Path(svg["path"]).read_text())
        with self.assertRaises(browser.BrowserError) as e:
            act(action="image", selector="Logo", out=str(Path(tempfile.gettempdir()) / "x.png"))
        self.assertEqual(e.exception.code, "refused_file")
        mine = b.dir / "downloads" / "logo-copy.png"
        self.assertEqual(act(action="image", selector="Logo", out=str(mine))["image"]["path"], str(mine.resolve()))
        act(action="open", url=f"http://127.0.0.1:{port}/cap", show=False)
        snap = act(action="snapshot", interactive=True)["snapshot"]
        self.assertTrue(snap.get("captcha"))
        self.assertIn("CAPTCHA test. Do not solve it", "\n".join(cli._snapshot_lines(snap)))
        with self.assertRaises(browser.BrowserError) as e:
            act(action="image", selector="letters")
        self.assertEqual(e.exception.code, "captcha")
        self.assertEqual(browser.image_size(_png(7, 5)), [7, 5])
        self.assertEqual(browser.image_type(b"<svg xmlns='x'/>"), ("image/svg+xml", ".svg"))


CANVAS = (b"<title>Canvas</title><style>body{margin:0}</style>"
          b"<div style='position:relative;width:400px;height:200px;margin:20px'>"
          b"<canvas id=c width=400 height=200 style='background:#eee'></canvas>"
          b"<div id=cover style='position:absolute;right:0;top:0;width:100px;height:200px;background:#c00'></div></div>"
          b"<button id=icon style='width:20px;height:20px;padding:0;border:0;background:#08f'></button>"
          b"<div id=tall style='width:600px;height:3000px;background:linear-gradient(#fff,#000)'></div>"
          b"<div class=h-captcha style='width:300px;height:80px'>check</div>"
          b"<script>const c = document.getElementById('c'); let down = null;"
          b"let dragged = false;"
          b"c.onclick = e => { if (dragged) { dragged = false; return; } document.title = e.offsetX + ',' + e.offsetY; };"
          b"c.onmousemove = e => { if (!down) document.title = 'move ' + e.offsetX + ',' + e.offsetY; };"
          b"c.onmousedown = e => { down = [e.offsetX, e.offsetY]; };"
          b"c.onmouseup = e => { if (down[0] !== e.offsetX || down[1] !== e.offsetY) { dragged = true;"
          b"  document.title = 'drag ' + down + '-' + e.offsetX + ',' + e.offsetY; } down = null; };"
          b"</script>")


class PointInside(Base):
    """#5689 in a real Chrome: --at presses a point of an element's box (a canvas has no elements to name), with the
    aim check for that point; screenshot <target> is a picture of the element, scaled to about 1000 pixels at most
    (a small one larger), with a grid on request; a CAPTCHA element is refused."""

    def test_a_point_inside_a_canvas_and_the_picture_of_one_element(self):
        if not chrome() or not browser.chrome_path():
            self.skipTest("no Chrome on this computer")
        if not _can_bind():
            self.skipTest("this sandbox does not allow a local port")

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(CANVAS)))
                self.end_headers()
                self.wfile.write(CANVAS)

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        core.config_set(self.c, "browser_allow", f"127.0.0.1:{port}", project="web")
        core.register(self.c, "ag1")
        b = browser.get("web", "ag1")
        self.addCleanup(b.close)
        act = lambda **a: routes.act(self.c, {"project": "web", **a}, "ag1")  # noqa: E731
        act(action="open", url=f"http://127.0.0.1:{port}/", show=False)
        self.assertEqual(act(action="click", selector="css:#c", at="25%,50%")["title"], "100,100")
        self.assertEqual(act(action="click", selector="css:#c", at="10,20")["title"], "10,20")
        with self.assertRaises(browser.BrowserError) as e:  # the red box covers the right quarter: nothing is pressed
            act(action="click", selector="css:#c", at="90%,50%")
        self.assertEqual(e.exception.code, "covered")
        with self.assertRaises(browser.BrowserError) as e:
            act(action="click", selector="css:#c", at="120%,50%")
        self.assertEqual(e.exception.code, "not_found")
        self.assertIn("a box of 400x200 pixels", str(e.exception))
        with self.assertRaises(RiverError):
            act(action="click", at="50%,50%")
        self.assertEqual(act(action="hover", selector="css:#c", at="50%,50%")["title"], "move 200,100")
        self.assertEqual(act(action="drag", selector="css:#c", to="css:#c", at="10%,10%", to_at="60%,80%")["title"],
                         "drag 40,20-240,160")

        def picture(**a):
            out = act(action="screenshot", **a)
            return out["element"], browser.image_size(base64.b64decode(out["png"]))
        e, size = picture(selector="css:#c")
        self.assertEqual((e["box"], e["scale"], size), ([400, 200], 1, [400, 200]))
        e, size = picture(selector="css:#icon")  # small: larger, up to 4 times
        self.assertEqual((e["scale"], size), (4, [80, 80]))
        e, size = picture(selector="css:#tall")  # taller than the window: the whole box, 1000 pixels long
        self.assertEqual((e["box"], size), ([600, 3000], [200, 1000]))
        plain = act(action="screenshot", selector="css:#c")["png"]
        out = act(action="screenshot", selector="css:#c", grid=True)
        self.assertTrue(out["element"]["grid"])
        self.assertNotEqual(out["png"], plain)  # the lines are in the picture
        self.assertIsNone(b.evaluate("document.getElementById('__maxpm_grid')"))  # and gone from the page
        with self.assertRaises(browser.BrowserError) as e:
            act(action="screenshot", selector="css:.h-captcha")
        self.assertEqual(e.exception.code, "captcha")


POINTER = (b"<title>Pointer</title><style>#mm{display:none;position:absolute;background:#fff} #m:hover #mm{display:block}"
           b"</style><nav aria-label=Main>"
           b"<div id=m style='display:inline-block;padding:8px'>Products<div role=menu aria-label=Products id=mm>"
           b"<a role=menuitem href=/a>Alpha</a> <a role=menuitem href=/b>Beta</a></div></div></nav>"
           b"<button ondblclick=\"document.title = 'double'\">Twice</button>"
           b"<button oncontextmenu=\"document.title = 'right'; return false\">Menu</button>"
           b"<button onclick=\"document.title = event.shiftKey ? 'shift' : 'plain'\">Mod</button>"
           b"<input type=range id=r min=0 max=100 value=0 style='width:200px' "
           b"oninput=\"document.title = 'range ' + this.value\"><div style='height:3000px'></div>")


class Pointer(Base):
    """#5669 in a real Chrome: a hover opens a menu and its items are in the reply; a double click, a right click, a
    click with shift held; a drag moves a slider; a scroll says where the page stands."""

    def test_hover_double_right_shift_drag_and_scroll(self):
        if not chrome() or not browser.chrome_path():
            self.skipTest("no Chrome on this computer")
        if not _can_bind():
            self.skipTest("this sandbox does not allow a local port")

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(POINTER)))
                self.end_headers()
                self.wfile.write(POINTER)

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        core.config_set(self.c, "browser_allow", f"127.0.0.1:{port}", project="web")
        core.register(self.c, "ag1")
        b = browser.get("web", "ag1")
        self.addCleanup(b.close)
        act = lambda **a: routes.act(self.c, {"project": "web", **a}, "ag1")  # noqa: E731
        act(action="open", url=f"http://127.0.0.1:{port}/", show=False)
        out = act(action="hover", selector="#m")
        self.assertEqual([x.split(" ", 2)[-1] if x.startswith("+ @") else x for x in out["changes"]],
                         ['+ [menu "Products"] 2 elements:', 'menuitem "Alpha"', 'menuitem "Beta"'])
        act(action="click", selector="Twice", count=2)
        self.assertEqual(b.evaluate("document.title"), "double")
        out = act(action="click", selector="Menu", button="right")
        self.assertEqual(b.evaluate("document.title"), "right")
        self.assertIn("(right click)", cli._browser_result("click", out)[0])
        act(action="click", selector="Mod", mods=["shift"])
        self.assertEqual(b.evaluate("document.title"), "shift")
        right, mid = b.evaluate("(() => { const r = document.getElementById('r').getBoundingClientRect(); "
                                "return [r.right - 1, r.top + r.height / 2]; })()")
        act(action="drag", selector="css:#r", to=[right, mid])
        self.assertGreater(int(b.evaluate("document.getElementById('r').value")), 90)
        out = act(action="scroll", amount="down")
        self.assertGreater(out["scrolled"]["y"], 0)
        self.assertIn("scrolled to", cli._browser_result("scroll", out)[0])
        with self.assertRaisesRegex(RiverError, "--with takes shift, ctrl, alt, meta, not hyper"):
            act(action="click", selector="Mod", mods=["hyper"])


SEARCH = (b"<title>Search</title><form action=/results><label>Search <input name=q></label><button>Go</button></form>"
          b"<button onclick=\"document.title = 'four'\">Four</button><button onclick=\"document.title = 'five'\">Five</button>")


class RunWalk(Base):
    """#5680 in a real Chrome: a session's walk is kept as steps with names (not refs); the steps with {q} in place
    of the word run the next day as one call with another input and give the last extract's rows; a failed step
    stops the run with its code and the page's outline; the person's Take control stops a run before its next step."""

    def test_record_steps_run_with_another_input_a_failed_step_and_the_person_s_control(self):
        if not chrome() or not browser.chrome_path():
            self.skipTest("no Chrome on this computer")
        if not _can_bind():
            self.skipTest("this sandbox does not allow a local port")
        import sys
        from urllib.parse import parse_qs, urlsplit

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                q = parse_qs(urlsplit(self.path).query).get("q", [""])[0]
                body = SEARCH if not self.path.startswith("/results") else (
                    b"<title>Results</title><ul>" + b"".join(
                        b"<li><a href=/item/%d>%s result %d</a> <span>info</span></li>" % (n, q.encode(), n)
                        for n in (1, 2, 3)) + b"</ul>")
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
        core.register(self.c, "ag1")
        core.register(self.c, "mark", human=True)
        b = browser.get("web", "ag1")
        self.addCleanup(b.close)
        patch = mock.patch.object(cli, "ask_server", lambda conn, op, args, actor=None, timeout=5, retries=3:
                                  server.OPS[op](conn, args, actor))
        patch.start()
        self.addCleanup(patch.stop)

        def maxpm(*words, stdin=""):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                    mock.patch.object(sys, "stdin", io.StringIO(stdin)):
                try:
                    code = cli.run(["-q", "--as", "ag1", "browser", *words, "--project", "web"])
                except SystemExit as e:
                    code = e.code
            return code, out.getvalue()
        url = f"http://127.0.0.1:{port}/"
        act = lambda **a: routes.act(self.c, {"project": "web", **a}, "ag1")  # noqa: E731
        act(action="open", url=url, show=False)
        act(action="fill", selector="Search", text="rust")
        lines = act(action="snapshot", interactive=True)["snapshot"]["lines"]
        go = next(x for x in lines if '"Go"' in x).split()[0]
        act(action="click", selector=go)  # by its ref: the step keeps its role and name
        act(action="extract", rows="li", fields=[["title", "a", None]])
        code, steps = maxpm("steps")
        self.assertEqual(steps.splitlines(), [f"open {url}", "fill Search rust", "click 'button \"Go\"'",
                                              "extract --rows li title=a"])
        # The next day: the same lines with {q}, another input, one call.
        walk = steps.replace("rust", "{q}")
        code, out = maxpm("run", "q=python", stdin=walk)
        self.assertEqual((code, json.loads(out)), (0, [{"title": f"python result {n}"} for n in (1, 2, 3)]))
        self.assertEqual(maxpm("steps")[1], steps)  # a run's steps do not join the walk
        with self.assertRaisesRegex(RiverError, "line 2 of the run needs the input q"):
            cli.run(["-q", "--as", "ag1", "browser", "run", "--lines", walk, "--project", "web"])
        # A failed step: its number, its code, and the outline of the page it stopped on.
        code, out = maxpm("run", "--lines", f"open {url}\nclick Nope\nclick Go")
        self.assertEqual(code, 2)
        self.assertTrue(out.startswith("step 2 (click Nope) failed after 1 steps: not_found: no element is \"Nope\""), out)
        self.assertIn("[page] 3 buttons, 1 field", out)
        # The person takes control after step 2: steps 3 to 5 do not happen.
        real, n = routes.act, [0]

        def counted(conn, a, who):
            out = real(conn, a, who)
            n[0] += 1
            if n[0] == 2:
                browser.take(conn, b, "mark")
            return out
        with mock.patch.object(routes, "act", counted):
            res = routes.run(self.c, {"project": "web", "steps": [
                {"action": "open", "url": url}, {"action": "fill", "selector": "Search", "text": "x"},
                {"action": "click", "selector": "Four"}, {"action": "click", "selector": "Five"},
                {"action": "snapshot"}]}, "ag1")
        self.assertEqual((res["ok"], res["done"], res["failed"]["step"], res["failed"]["code"]),
                         (False, 2, 3, "not_controller"))
        self.assertEqual(b.evaluate("document.title"), "Search")


class Files(Base):
    """#5679 in a real Chrome: a click that downloads replies with the file's path and size (a second one with the
    same name gets its own); an upload gives a file of the downloads to a hidden file field, and refuses a file from
    another folder."""

    def test_a_download_names_its_file_and_an_upload_fills_a_file_field(self):
        if not chrome() or not browser.chrome_path():
            self.skipTest("no Chrome on this computer")
        if not _can_bind():
            self.skipTest("this sandbox does not allow a local port")

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                csv = self.path == "/report.csv"
                body = b"a,b\n1,2\n" if csv else FILES
                self.send_response(200)
                self.send_header("Content-Type", "text/csv" if csv else "text/html")
                if csv:
                    self.send_header("Content-Disposition", 'attachment; filename="report.csv"')
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
        act = lambda **a: routes.act(self.c, {"project": "web", **a}, None)  # noqa: E731
        act(action="open", url=f"http://127.0.0.1:{port}/")
        out = act(action="click", selector="Report")
        d = out["downloads"][0]
        self.assertEqual((d["state"], d["bytes"], os.path.basename(d["path"]), out["navigated"]),
                         ("completed", 8, "report.csv", False))
        self.assertEqual(Path(d["path"]).read_bytes(), b"a,b\n1,2\n")
        self.assertIn(f"downloaded {d['path']} (8 bytes)", cli._browser_result("click", out))
        again = act(action="click", selector="Report")["downloads"][0]
        self.assertEqual(os.path.basename(again["path"]), "report (2).csv")
        out = act(action="upload", selector="Choose a file", files=[d["path"]])
        self.assertEqual((out["uploaded"], b.evaluate("document.title")), (["report.csv"], "got report.csv"))
        self.assertIn("(uploaded report.csv)", cli._browser_result("upload", out)[0])
        other = os.path.join(self.tmp.name, "secret.txt")
        Path(other).write_text("x")
        with self.assertRaises(browser.BrowserError) as e:
            act(action="upload", selector="Choose a file", files=[other])
        self.assertEqual(e.exception.code, "refused_file")


class ThreeDoors(Base):
    """#5678 in a real Chrome: the same walk gives the same replies through HTTP (op browser_call), the command with
    --json and the MCP tool browser: one shape, a read or a change, and the error codes with their candidates."""

    def test_one_shape_through_http_the_command_and_mcp(self):
        import re
        from river import mcp
        if not chrome() or not browser.chrome_path():
            self.skipTest("no Chrome on this computer")
        if not _can_bind():
            self.skipTest("this sandbox does not allow a local port")

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(LONG)))
                self.end_headers()
                self.wfile.write(LONG)

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        core.config_set(self.c, "browser_allow", f"127.0.0.1:{port}", project="web")
        core.register(self.c, "ag1")
        b = browser.get("web", "ag1")
        self.addCleanup(b.close)
        # serve's answer to the command and the MCP tool: the same ops, in this process (ask_server asks serve)
        patch = mock.patch.object(cli, "ask_server", lambda conn, op, args, actor=None, timeout=5, retries=3:
                                  server.OPS[op](conn, args, actor))
        patch.start()
        self.addCleanup(patch.stop)
        url = f"http://127.0.0.1:{port}/"
        walk = [  # (HTTP args, the command's words, the MCP arguments)
            ({"action": "open", "url": url, "show": False}, ["open", url, "--no-show"], {"action": "open", "url": url}),
            ({"action": "find", "find": "card b", "interactive": True} | {"action": "snapshot"}, ["find", "card b"],
             {"action": "find", "text": "card b"}),
            ({"action": "click", "selector": "Edit"}, ["click", "Edit"], {"action": "click", "target": "Edit"}),
            ({"action": "click", "selector": "Edit", "within": "Invoice 1042"},
             ["click", "Edit", "--in", "Invoice 1042"], {"action": "click", "target": "Edit", "in": "Invoice 1042"}),
            ({"action": "extract", "rows": "tbody tr", "fields": [["inv", "a", None]], "limit": 2},
             ["extract", "--rows", "tbody tr", "--limit", "2", "inv=a"],
             {"action": "extract", "rows": "tbody tr", "fields": ["inv=a"], "limit": 2}),
            ({"action": "click", "selector": "Help"}, ["click", "Help"], {"action": "click", "target": "Help"}),
        ]
        refs = lambda o: json.loads(re.sub(r"@e\d+", "@e", json.dumps(o)))  # noqa: E731  (each walk gives new refs)
        doors = {"http": [], "command": [], "mcp": []}
        m = mcp.Server()
        for http_args, words, mcp_args in walk:
            doors["http"].append(refs(routes.call(self.c, {"project": "web", **http_args}, "ag1")))
        for http_args, words, mcp_args in walk:
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                try:
                    cli.run(["-q", "--as", "ag1", "--json", "browser"] + words + ["--project", "web"])
                except SystemExit:
                    pass
            doors["command"].append(refs(json.loads(out.getvalue())))
        for http_args, words, mcp_args in walk:
            text, bad = m.call("browser", {**mcp_args, "project": "web", "json": True, "as": "ag1"})
            reply = json.JSONDecoder().raw_decode(text)[0]  # the JSON, then maxpm's footer line
            self.assertEqual(bad, not reply["ok"])
            doors["mcp"].append(refs(reply))
        self.assertEqual(doors["http"], doors["command"])
        self.assertEqual(doors["http"], doors["mcp"])
        r = doors["http"]
        self.assertEqual([(x["action"], x["kind"], x["ok"]) for x in r],
                         [("open", "change", True), ("snapshot", "read", True), ("click", "change", False),
                          ("click", "change", True), ("extract", "read", True), ("click", "change", True)])
        self.assertEqual((r[2]["error"]["code"], len(r[2]["error"]["candidates"])), ("ambiguous", 3))
        self.assertEqual(r[0]["changed"][:1], ['[nav "Site"] 2 links'])
        self.assertEqual(r[3]["changed"], ["no change"])
        self.assertEqual(r[4]["data"]["data"], [{"inv": "Invoice 1041"}, {"inv": "Invoice 1042"}])
        self.assertTrue(r[5]["navigated"])
        # The other codes: no element, a cover, a refused address.
        self.assertEqual(routes.call(self.c, {"project": "web", "action": "click", "selector": "Nope"}, "ag1")
                         ["error"]["code"], "not_found")
        self.assertEqual(routes.call(self.c, {"project": "web", "action": "open", "url": "http://10.1.2.3/"}, "ag1")
                         ["error"]["code"], "refused_address")


MENU = (b"<title>Menu</title><nav aria-label=Top><button onclick=\"menu.hidden = !menu.hidden\">Account</button>"
        b"<div id=menu role=menu aria-label=Account hidden><a role=menuitem href=/p>Profile</a>"
        b"<a role=menuitem href=/s>Settings</a><button role=menuitem>Sign out</button></div></nav>"
        b"<main><h1>Orders</h1><p id=note>No note</p><label><input type=checkbox> Agree</label><button>Nothing</button>"
        b"<button onclick=\"this.remove()\">Remove me</button><a href=/two>Two</a>"
        b"<button onclick=\"document.querySelector('h1').textContent = 'Order 42 saved'\">Save</button>"
        b"<button onclick=\"note.textContent = 'A longer note now'\">Note</button></main>")


class ActionChanges(Base):
    """#5673 in a real Chrome: the reply of a session's action says what changed: a menu that opened with its items,
    'no change', a changed checkbox, an element that went, and a new page's outline."""

    def test_the_reply_lists_what_came_what_changed_and_what_went(self):
        if not chrome() or not browser.chrome_path():
            self.skipTest("no Chrome on this computer")
        if not _can_bind():
            self.skipTest("this sandbox does not allow a local port")

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"<title>Two</title><main><a href=/>Back</a></main>" if self.path == "/two" else MENU
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
        core.register(self.c, "ag1")
        b = browser.get("web", "ag1")
        self.addCleanup(b.close)
        act = lambda **a: routes.act(self.c, {"project": "web", **a}, "ag1")  # noqa: E731
        act(action="open", url=f"http://127.0.0.1:{port}/", show=False)
        out = act(action="click", selector="Account")
        self.assertEqual([x.split(" ", 2)[-1] if x.startswith("+ @") else x for x in out["changes"]],
                         ['+ [menu "Account"] 3 elements:', 'menuitem "Profile"', 'menuitem "Settings"',
                          'menuitem "Sign out"'])
        self.assertIn('+ [menu "Account"] 3 elements:', cli._browser_result("click", out))
        t0 = time.time()
        self.assertEqual(act(action="click", selector="Nothing")["changes"], ["no change"])
        self.assertLess(time.time() - t0, timing.limit(0.1))  # p147
        self.assertRegex(act(action="check", selector="Agree")["changes"][0], r'^~ @e\d+ checkbox "Agree" checked$')
        self.assertRegex(act(action="click", selector="Remove me")["changes"][0], r"^- 1 gone: @e\d+$")
        # #5759: a heading or a paragraph that changed is no 'no change'
        self.assertEqual(act(action="click", selector="Save")["changes"], ['~ heading h1 "Order 42 saved"'])
        t0 = time.time()
        self.assertEqual(act(action="click", selector="Note")["changes"],
                         ["~ text changed (+10 characters): maxpm browser text"])
        self.assertLess(time.time() - t0, timing.limit(0.1))  # p147
        out = act(action="click", selector="Two")
        self.assertEqual((out["navigated"], out.get("changes"), out["outline"]), (True, None, ["[main] 1 link"]))


DOC = (b"<title>Doc</title><nav>Menu one</nav><main><h1>Doc</h1><p>Intro line</p><nav>Inner menu</nav>"
       b"<h2>Alpha</h2><p>alpha text</p><h3>Alpha sub</h3><p>sub text</p><h2>Beta</h2>"
       + b"".join(b"<p>beta line %d</p>" % n for n in range(40)) + b"<aside>Side box</aside>"
       b"<form>Search: <input name=q></form><p>3044 comments</p><a href=#c>3044 comments</a></main><footer>Foot</footer>")


class PageText(Base):
    """#5672 in a real Chrome: the main text without its menus, side boxes and footer; a section to the next heading of
    its level; the lines with a word; the limit, the rest and --from; --all; open --text."""

    def test_main_text_a_section_grep_the_limit_and_open_text(self):
        if not chrome() or not browser.chrome_path():
            self.skipTest("no Chrome on this computer")
        if not _can_bind():
            self.skipTest("this sandbox does not allow a local port")

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(DOC)))
                self.end_headers()
                self.wfile.write(DOC)

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
        act = lambda **a: routes.act(self.c, {"project": "web", **a}, None)  # noqa: E731
        out = act(action="open", url=f"http://127.0.0.1:{port}/", read=True, outline=False)
        t = out["text"]
        self.assertTrue(t.startswith("Doc\n\nIntro line"), t)
        # #5719: back from the tab's first page stays; a field with no label is named by the text before it, and its
        # name attribute finds it too; a name matches as a word inside a longer one.
        with self.assertRaisesRegex(RiverError, "there is no page before this one in this tab; it stays on"):
            act(action="back")
        self.assertEqual(b.info()["url"], f"http://127.0.0.1:{port}/")
        field = browser.resolve_target(b, "Search")
        self.assertEqual((browser.resolve_target(b, "q"), browser.resolve_target(b, 'textbox "search"')), (field, field))
        self.assertIn(f'{field} textbox "Search"', act(action="snapshot", interactive=True, find="search")["snapshot"]
                      ["lines"])
        self.assertEqual(act(action="html", scope="comments")["html"], '<a href="#c">3044 comments</a>')
        self.assertIn("beta line 39", t)
        for gone in ("Menu one", "Inner menu", "Side box", "Foot"):
            self.assertNotIn(gone, t)
        self.assertEqual((out["rest"], out["next"]), (0, None))
        self.assertEqual(act(action="text", section="alpha")["text"].split("\n"),
                         ["Alpha", "", "alpha text", "", "Alpha sub", "", "sub text"])
        with self.assertRaisesRegex(RiverError, 'no heading "Gamma"; the page has "Doc", "Alpha", "Alpha sub", "Beta"'):
            act(action="text", section="Gamma")
        g = act(action="text", grep="sub")
        self.assertEqual((g["matches"], g["text"].split("\n")), (2, ["alpha text", "Alpha sub", "sub text", "Beta"]))
        part = act(action="text", max=40)
        self.assertLessEqual(len(part["text"].encode()), 40)
        self.assertGreater(part["rest"], 0)
        self.assertIn(f"--from {part['next']}", cli._text_lines(part)[-1])
        nxt = act(action="text", **{"from": part["next"], "max": 40})
        self.assertFalse(t.startswith(nxt["text"]))
        self.assertIn(nxt["text"].strip()[:10], t)
        self.assertIn("Foot", act(action="text", all=True)["text"])
        sec = act(action="text", section="Beta", max=30)
        self.assertTrue(cli._text_lines(sec)[-1].startswith(f'... ') and f'--section "Beta" --from {sec["next"]}'
                        in cli._text_lines(sec)[-1])

    def test_star_images_and_grey_signs_read_as_a_person_sees_them(self):
        """#5718: DuckDuckGo shows a Yelp score as an image with alt 'rating: 4.5'; The Infatuation shows a price as
        four $ with the unused ones grey. The text has the score and $$$; a large photo's alt stays out; the page
        is as before."""
        if not chrome() or not browser.chrome_path():
            self.skipTest("no Chrome on this computer")
        if not _can_bind():
            self.skipTest("this sandbox does not allow a local port")
        svg = b"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='1' height='1'/%3E"
        page = (b"<title>Places</title><main><h1>Places</h1><p>La Stanza <img alt='rating: 4.5' width=60 height=12 src=\""
                + svg + b"\"> Yelp (475)</p><div style='display:flex'><span>$</span><span>$</span><span>$</span>"
                b"<span style='color:#bbb'>$</span></div><p>Cheap: <span><b>$</b><b style='opacity:.3'>$</b></span> "
                b"and all on: <span><b>$</b><b>$</b></span></p>"
                b"<img alt='A big plate of pasta' width=400 height=300 src=\"" + svg + b"\">"
                b"<a href=/x><img alt='Next page' width=200 height=80 src=\"" + svg + b"\"></a></main>")

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)

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
        b.open(f"http://127.0.0.1:{port}/")
        before = b.evaluate("document.body.outerHTML")
        t = browser.page_text(b)["text"]
        self.assertIn("La Stanza [rating: 4.5] Yelp (475)", t)
        self.assertIn("$$$", t)
        self.assertNotIn("$$$$", t)
        self.assertIn("Cheap: $ and all on: $$", t)
        self.assertIn("[Next page]", t)  # the whole label of its link
        self.assertNotIn("pasta", t)
        self.assertEqual(b.evaluate("document.body.outerHTML"), before)


FALLBACK = (b"<title>Invoices</title><main><button>Delete</button><button>Delete</button>"
            b"<button id=go class='btn primary' data-testid=go-btn style='width:80px;height:30px'"
            b" onclick=\"document.title = 'went'\"></button></main>")


class Fallbacks(Base):
    """#5692: the log of the cases where a session's names did not work and it fell back, and its reader."""

    def run_cli(self, *words):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(list(words))
        return out.getvalue()

    def test_the_log_keeps_host_and_path_and_the_reader_counts_by_site_and_kind(self):
        miss = core.fallback_log(self.c, "web", "ag1", "https://en.wikipedia.org/wiki/Lithium?x=secret#top",
                                 "not_found", action="click", tried="Edit section")
        core.fallback_log(self.c, "web", "ag1", "https://en.wikipedia.org/wiki/Lithium", "html_part", action="html",
                          tried="Edit section", target="css:#mw-content", miss=miss)
        core.fallback_log(self.c, "web", "ag2", "https://news.ycombinator.com/", "selector", action="click",
                          target=".morelink", used="a class=morelink")
        core.fallback_used(self.c, miss, 'a "edit" class=mw-editsection')
        rows = [dict(r) for r in self.c.execute("SELECT * FROM browser_fallbacks ORDER BY id")]
        self.assertEqual([(r["host"], r["path"]) for r in rows][0], ("en.wikipedia.org", "/wiki/Lithium"))
        self.assertNotIn("secret", json.dumps(rows))  # no query string
        self.assertEqual([r["used"] for r in rows[:2]], ['a "edit" class=mw-editsection'] * 2)
        res = core.browser_fallbacks(self.c)
        self.assertEqual(res["total"], 3)
        self.assertEqual(res["sites"][0], {"host": "en.wikipedia.org", "kinds": {"not_found": 1, "html_part": 1}, "n": 2})
        self.assertEqual([c["kind"] for c in res["cases"]], ["selector", "html_part", "not_found"])
        self.assertEqual(core.browser_fallbacks(self.c, site="wikipedia.org")["total"], 2)  # the end of a host
        self.assertEqual(core.browser_fallbacks(self.c, site="pedia.org")["total"], 0)
        out = self.run_cli("browser", "fallbacks", "--site", "wikipedia.org")
        self.assertIn("2 cases since", out)
        self.assertRegex(out, r"en\.wikipedia\.org  2: (html of a part 1, name not found 1|name not found 1, html of a "
                              r"part 1)")
        self.assertIn('/wiki/Lithium  name not found (click)  tried Edit section  used: a "edit" class=mw-editsection', out)
        self.assertIn("no fallbacks since", self.run_cli("browser", "fallbacks", "--site", "example.com"))
        with self.assertRaises(RiverError):
            core.fallback_log(self.c, "web", "ag1", "https://a.b/", "guess")

    def test_the_persons_dev_page_is_no_site_to_fix(self):
        dev = lambda u: browser.dev_page(self.c, "web", u)  # noqa: E731
        for u in ("http://localhost:5173/", "http://127.0.0.1:8000/x", "http://10.0.0.4/", "http://app.localhost/",
                  "about:blank"):
            self.assertTrue(dev(u), u)
        self.assertFalse(dev("https://example.com/"))
        core.config_set(self.c, "browser_allow", "myapp.test:8080", project="web")
        self.assertTrue(dev("http://myapp.test:8080/"))
        self.assertFalse(dev("http://myapp.test/"))

    def test_a_failed_name_and_the_fallbacks_after_it_in_a_real_chrome(self):
        if not chrome() or not browser.chrome_path():
            self.skipTest("no Chrome on this computer")
        if not _can_bind():
            self.skipTest("this sandbox does not allow a local port")

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(FALLBACK)))
                self.end_headers()
                self.wfile.write(FALLBACK)

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        core.config_set(self.c, "browser_allow", f"127.0.0.1:{port}", project="web")
        core.register(self.c, "ag1")
        b = browser.get("web", "ag1")
        self.addCleanup(b.close)
        act = lambda who="ag1", **a: routes.act(self.c, {"project": "web", **a}, who)  # noqa: E731
        rows = lambda: [dict(r) for r in self.c.execute("SELECT * FROM browser_fallbacks ORDER BY id")]  # noqa: E731
        act(action="open", url=f"http://127.0.0.1:{port}/inv?id=7", show=False)
        act(action="html")  # the person's dev page, no failed name: dev work, no case
        act(action="click", selector="css:#go")
        self.assertEqual(rows(), [])
        with self.assertRaises(browser.BrowserError):
            act(action="click", selector="Go on")
        with self.assertRaises(browser.BrowserError):  # the person's pane: no case
            act(None, action="click", selector="Go on", agent="ag1")
        act(action="html", scope="css:main")
        act(action="html")
        act(action="screenshot", selector="css:#go")
        t0 = time.time()
        act(action="click", selector="css:#go", changes=False)
        self.assertLess(time.time() - t0, timing.limit(0.1))  # p147, with the case logged
        act(action="html")  # the failed name's case ended with the click
        r = rows()
        self.assertEqual([(x["kind"], x["action"], x["tried"], x["target"]) for x in r],
                         [("not_found", "click", "Go on", None), ("html_part", "html", "Go on", "css:main"),
                          ("html_whole", "html", "Go on", None), ("picture", "screenshot", "Go on", "css:#go"),
                          ("selector", "click", "Go on", "#go")])
        self.assertEqual({x["used"] for x in r}, {"button class=btn primary testid=go-btn"})
        self.assertEqual({(x["host"], x["path"], x["agent"], x["project"]) for x in r},
                         {(f"127.0.0.1:{port}", "/inv", "ag1", "web")})
        self.assertEqual({x["miss"] for x in r[1:]}, {r[0]["id"]})
        with self.assertRaises(browser.BrowserError) as e:
            act(action="click", selector="Delete")
        ref = e.exception.candidates[0].split()[0]
        act(action="click", selector=ref)  # the names worked in the end: the element on the case, no new case
        r = rows()
        self.assertEqual((r[-1]["kind"], r[-1]["tried"], r[-1]["used"]), ("ambiguous", "Delete", 'button "Delete"'))
        with self.assertRaises(browser.BrowserError):
            act(action="click", selector="Nope")
        box = b.evaluate("(() => { const r = go.getBoundingClientRect(); return [r.x + 10, r.y + 10]; })()")
        act(action="click", x=box[0], y=box[1], changes=False)
        self.assertEqual((rows()[-1]["kind"], rows()[-1]["target"], rows()[-1]["used"]),
                         ("point", f"{box[0]:g},{box[1]:g}", "button class=btn primary testid=go-btn"))
        n = len(rows())
        with mock.patch.object(browser, "dev_page", return_value=False):  # a public site: a selector skips the names
            act(action="click", selector="css:#go", changes=False)
            act(action="html")
        self.assertEqual([(x["kind"], x["tried"]) for x in rows()[n:]], [("selector", None), ("html_whole", None)])


BOXES = (b"<title>Boxes</title>"
         b"<button id=del onclick=\"document.title = confirm('Delete this?') ? 'deleted' : 'kept'\">Delete</button>"
         b"<button id=ask onclick=\"document.title = 'hi ' + prompt('Your name?', 'Ann')\">Ask</button>"
         b"<button id=al onclick=\"setTimeout(() => { alert('Saved'); document.title = 'after alert' }, 300)\">Later</button>"
         b"<a id=go href=/two onclick=\"return confirm('Leave?')\">Two</a><a id=part href=#end>Part</a>"
         b"<script>onbeforeunload = (e) => { e.preventDefault(); e.returnValue = '' }</script>")


class PageBoxes(Base):
    """#5674: a page's alert, confirm and prompt boxes in a real Chrome: the action that opens one replies with it,
    every later command is refused at once and names it, maxpm browser dialog accept|dismiss answers it, the pane's
    status carries it, and a box that asks whether to leave the page is accepted by itself."""

    def test_a_confirm_box_replies_with_its_text_and_dialog_accept_goes_on(self):
        if not chrome() or not browser.chrome_path():
            self.skipTest("no Chrome on this computer")
        if not _can_bind():
            self.skipTest("this sandbox does not allow a local port")

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"<title>Two</title>" if self.path == "/two" else BOXES
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
        act = lambda **a: routes.act(self.c, {"project": "web", **a}, None)  # noqa: E731
        act(action="open", url=f"http://127.0.0.1:{port}/")

        # The click that opens the box replies at once, with the box; the reply prints the way to answer it.
        t0 = time.time()
        out = act(action="click", selector="#del")
        self.assertLess(time.time() - t0, timing.limit(2.0, below=browser.NAV_START))
        self.assertEqual(out["dialog"], {"type": "confirm", "message": "Delete this?", "default": "",
                                         "url": f"http://127.0.0.1:{port}/"})
        self.assertIn('dialog confirm "Delete this?": maxpm browser dialog accept, or dismiss',
                      cli._browser_result("click", out))
        # Every other command fails at once and names the box; the status shows it.
        t0 = time.time()
        for a in ({"action": "snapshot"}, {"action": "click", "selector": "#ask"}, {"action": "text"}):
            with self.assertRaisesRegex(RiverError, 'the page shows a confirm box "Delete this\\?": maxpm browser '
                                                    'dialog accept, or maxpm browser dialog dismiss'):
                act(**a)
        self.assertLess(time.time() - t0, timing.limit(1.0, below=browser.LOADING_WAIT))
        self.assertEqual(act(action="status")["dialog"]["message"], "Delete this?")
        with self.assertRaisesRegex(RiverError, "a confirm box takes no text"):
            act(action="dialog", text="x")
        t0 = time.time()
        out = act(action="dialog", accept=True)
        self.assertLess(time.time() - t0, timing.limit(0.3, below=browser.NAV_START))  # no next page: no wait for one
        self.assertEqual((out["dialog"], out["answered"]["accept"], b.evaluate("document.title")),
                         (None, True, "deleted"))
        t0 = time.time()
        act(action="click", selector="#part")  # a link in the same page: no wait for a next page
        self.assertLess(time.time() - t0, timing.limit(0.3, below=browser.NAV_START))
        self.assertIn("(accepted the confirm box)", cli._browser_result("dialog", out)[0])
        act(action="click", selector="#del")
        act(action="dialog", accept=False)
        self.assertEqual(b.evaluate("document.title"), "kept")
        with self.assertRaisesRegex(RiverError, "the page shows no alert, confirm or prompt box"):
            act(action="dialog")

        # A prompt box with its default text; the answer is the text.
        out = act(action="click", selector="#ask")
        self.assertEqual((out["dialog"]["type"], out["dialog"]["default"]), ("prompt", "Ann"))
        act(action="dialog", accept=True, text="Bob")
        self.assertEqual(b.evaluate("document.title"), "hi Bob")

        # A box that opens later than the action: the next command names it.
        act(action="click", selector="#al")
        timing.wait_for(lambda: act(action="status").get("dialog"), 10, "the alert box")
        with self.assertRaisesRegex(RiverError, 'alert box "Saved"'):
            act(action="text")
        act(action="dialog")
        self.assertEqual(b.evaluate("document.title"), "after alert")

        # A link that asks first: after the yes, the reply is the next page. The page before asks whether to leave
        # it (beforeunload, after the clicks): that box is accepted by itself.
        self.assertEqual(act(action="click", selector="#go")["dialog"]["message"], "Leave?")
        t0 = time.time()
        out = act(action="dialog")
        self.assertEqual((out["navigated"], out["title"], out["dialog"]), (True, "Two", None))
        self.assertLess(time.time() - t0, timing.limit(3.0, below=browser.IDLE_WAIT))
        # A batch: the step that opens the box replies with it; the next step is refused, which ends the batch.
        act(action="back")
        res = act(action="batch", steps=[{"action": "click", "selector": "#del"}, {"action": "text"},
                                         {"action": "dialog"}])
        self.assertEqual(res["steps"][0]["result"]["dialog"]["message"], "Delete this?")
        self.assertIn("confirm box", res["steps"][1]["error"])
        res = act(action="batch", steps=[{"action": "dialog", "accept": False}, {"action": "text"}])
        self.assertTrue(res["ok"], res)
        self.assertEqual(b.evaluate("document.title"), "kept")


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
        wait = timing.limit(browser.START_WAIT)  # on CI the wait scales (#5705)
        port, end = None, time.monotonic() + wait
        while port is None and time.monotonic() < end and p.poll() is None:
            try:
                port = int(Path(prof, "DevToolsActivePort").read_text(encoding="utf-8").split()[0])
            except (OSError, ValueError, IndexError):
                time.sleep(0.1)
        self.assertTrue(port, f"the shared Chrome did not start within {wait:g}s (exit {p.poll()})")
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
        timing.wait_for(lambda: gone not in base.call("Target.getBrowserContexts")["browserContextIds"], 10,
                        "the end of the session's context")

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
        # serve waits for its own Chrome (browser.START_WAIT), and stop at once when Chrome ended. On CI the wait
        # scales: an ubuntu runner took more than 20 s once (#5705).
        wait = timing.limit(browser.START_WAIT)
        port, end = None, time.monotonic() + wait
        while port is None and time.monotonic() < end and p.poll() is None:
            try:
                port = int(Path(prof, "DevToolsActivePort").read_text(encoding="utf-8").split()[0])
            except (OSError, ValueError, IndexError):
                time.sleep(0.1)
        self.assertTrue(port, f"the shared Chrome did not start within {wait:g}s (exit {p.poll()})")
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


class Reaper(unittest.TestCase):
    """A Chrome that another process stopped by its pid is reaped by the process that started it (#5715)."""

    @unittest.skipIf(os.name == "nt", "POSIX zombies")
    def test_an_ended_child_is_reaped_and_dropped(self):
        import subprocess
        p = subprocess.Popen(["sh", "-c", "exit 3"])
        ref = p  # the Browser object keeps its Popen: Python's own cleanup of dropped ones does not reach it
        browser.track_child(p)
        self.addCleanup(lambda: browser.CHILDREN.remove(p) if p in browser.CHILDREN else None)
        end = time.time() + 5
        while time.time() < end and p in browser.CHILDREN:
            browser.reap()
            time.sleep(0.05)
        self.assertNotIn(p, browser.CHILDREN)
        self.assertEqual(ref.returncode, 3)
        with self.assertRaises(ChildProcessError):  # nothing is left to wait for: no zombie
            os.waitpid(p.pid, os.WNOHANG)
        self.assertTrue(browser._REAPER["thread"] is None or browser._REAPER["thread"].daemon)

    @unittest.skipIf(not hasattr(os, "posix_spawn"), "POSIX zombies")
    def test_serve_reaps_the_children_that_ended_before_its_start(self):
        # In a Python of its own: reap_orphans waits for any ended child, and this process has other tests' children.
        import subprocess
        import sys
        code = ("import os, time; from river import server\n"
                "pid = os.posix_spawn('/bin/sh', ['sh', '-c', 'exit 0'], os.environ)\n"  # no Popen knows it
                "end = time.time() + 5\n"
                "n = 0\n"
                "while time.time() < end and not n:\n"
                "    n = server.reap_orphans(); time.sleep(0.05)\n"
                "try:\n"
                "    os.waitpid(pid, os.WNOHANG); print('left', n)\n"
                "except ChildProcessError:\n"
                "    print('reaped', n)\n")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                             cwd=str(Path(__file__).resolve().parent.parent))
        self.assertEqual(out.stdout.strip(), "reaped 1", out.stderr)


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


class FakeSteps(browser.PersonSteps):
    """PersonSteps with the page's answers given by the test: AT maps a point (or 'focus') to what the page says."""

    def __init__(self, at, shown=10):
        super().__init__(mock.Mock(), "mark", "ag1")
        self.AT, self.n_shown = at, shown

    def _snap(self, o):
        if o.get("shown"):
            return {"shown": self.n_shown}
        return self.AT.get(tuple(o["at"]) if isinstance(o["at"], list) else o["at"])


def _el(role, name, **kw):
    return {"role": role, "name": name, "same": 1, "css": kw.pop("css", "div > " + role), "within": "", "secret": False,
            "field": False, "choice": None, "ref": kw.pop("ref", "e" + name), **kw}


class PersonStepRules(unittest.TestCase):
    """#5707: the rules that turn the person's input into steps, with the page's answers given."""

    def lines(self, rec):
        return [" ".join(browser.step_words(s)) for s in rec.stop()["steps"]]

    def test_clicks_keys_fill_drag_scroll_hover_select_and_a_secret(self):
        email = _el("textbox", "Email", field=True)
        pw = _el("textbox", "Password", field=True, secret=True)
        at = {(10, 10): _el("button", "Save"), (20, 20): email, "focus": email, (30, 30): _el("link", "Edit",
              same=3, within="Invoice 1042 …"), (40, 40): _el("clickable", "", css="#app > div:nth-of-type(2)"),
              (50, 50): _el("listitem", "Card A"), (90, 90): _el("region", "Done"), (60, 60): _el("button", "Menu"),
              (70, 70): _el("combobox", "Month", choice=["January"], ref="e9")}
        rec = FakeSteps(at)
        ev = lambda **k: rec.event(k)  # noqa: E731
        m = {"alt": False, "ctrl": False, "meta": False, "shift": False}
        ev(type="mouse", action="down", x=10, y=10, button="left", clicks=1)
        ev(type="mouse", action="up", x=10, y=10, button="left", clicks=1)
        ev(type="mouse", action="down", x=10, y=10, button="left", clicks=2)  # a double click
        ev(type="mouse", action="up", x=10, y=10, button="left", clicks=2)
        ev(type="mouse", action="down", x=20, y=20, button="left", clicks=1)  # the click into the field goes
        ev(type="mouse", action="up", x=20, y=20, button="left", clicks=1)
        for k in "ann@x.coo":
            ev(type="key", key=k, mods=m)
        ev(type="key", key="Backspace", mods=m)
        ev(type="text", text="m")
        ev(type="key", key="Enter", mods=m)
        ev(type="key", key="a", mods={**m, "meta": True})
        rec.AT["focus"] = pw
        for k in "s3cret":
            ev(type="key", key=k, mods=m)
        ev(type="mouse", action="down", x=30, y=30, button="right", clicks=1)
        ev(type="mouse", action="up", x=30, y=30, button="right", clicks=1)
        ev(type="mouse", action="down", x=40, y=40, button="left", clicks=1)
        ev(type="mouse", action="up", x=40, y=40, button="left", clicks=1)
        ev(type="mouse", action="down", x=50, y=50, button="left", clicks=1)  # a drag onto another element
        ev(type="mouse", action="move", x=70, y=70, button="left")
        ev(type="mouse", action="up", x=90, y=90, button="left", clicks=1)
        rec.event({"type": "mouse", "action": "wheel", "x": 5, "y": 5, "dx": 0, "dy": 100}, {"deltaX": 0, "deltaY": 100})
        rec.event({"type": "mouse", "action": "wheel", "x": 5, "y": 5, "dx": 0, "dy": 150}, {"deltaX": 0, "deltaY": 150})
        ev(type="mouse", action="move", x=60, y=60)  # rests on Menu, and a menu shows
        rec.hover["t"] -= browser.HOVER_REST + 0.1
        rec.n_shown = 14
        ev(type="mouse", action="move", x=61, y=61)
        ev(type="mouse", action="move", x=200, y=200)  # passes with nothing new: no step
        rec.AT[(200, 200)] = _el("text", "x")
        ev(type="mouse", action="down", x=70, y=70, button="left", clicks=1)
        ev(type="mouse", action="up", x=70, y=70, button="left", clicks=1)
        with mock.patch.object(browser, "on_element", lambda b, t, js: ["March"]):
            rec.bar({"action": "open", "url": "https://example.com/"})
        rec.bar({"action": "back"})
        got = rec.stop()
        lines = [" ".join(browser.step_words(s)) for s in got["steps"]]
        self.assertEqual(lines, [
            'click button "Save" --double',
            'fill textbox "Email" ann@x.com',
            "key Enter",
            "key a --with meta",
            'fill textbox "Password" {password}',
            'click link "Edit" --right --in Invoice 1042',
            "click css:#app > div:nth-of-type(2)",
            'drag Card A Done',
            "scroll 250",
            'hover button "Menu"',
            'select combobox "Month" March',
            "open https://example.com/",
            "back"])
        self.assertNotIn("s3cret", json.dumps(got))
        self.assertEqual(got["fallbacks"], [("css:#app > div:nth-of-type(2)", "clickable #app > div:nth-of-type(2)")])

    def test_a_point_the_page_cannot_name_keeps_the_point_and_an_error_never_stops_input(self):
        rec = FakeSteps({})
        rec.event({"type": "mouse", "action": "down", "x": 3, "y": 4, "button": "left", "clicks": 1})
        rec.event({"type": "mouse", "action": "up", "x": 3, "y": 4, "button": "left", "clicks": 1})
        rec.event({"type": "key"})  # no key: nothing, and no exception
        rec._snap = mock.Mock(side_effect=ValueError("page gone"))
        rec.event({"type": "mouse", "action": "down", "x": 1, "y": 1, "button": "left", "clicks": 1})
        self.assertGreaterEqual(rec.misses, 1)
        self.assertEqual(self.lines(rec)[0], "click 3 4")


class PersonRecord(Base):
    """#5707 in a real Chrome: Record on, the person's clicks and keys through the pane's input op become steps by
    name; stop gives the lines, and maxpm browser run plays them again with another input."""

    def test_the_person_s_steps_run_again_with_another_value(self):
        if not chrome() or not browser.chrome_path():
            self.skipTest("no Chrome on this computer")
        if not _can_bind():
            self.skipTest("this sandbox does not allow a local port")

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = ROWS_FORM
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
        core.register(self.c, "ag1")
        core.register(self.c, "mark", human=True)
        b = browser.get("web", "ag1")
        self.addCleanup(b.close)
        act = lambda who, **a: routes.act(self.c, {"project": "web", "session": "ag1", **a}, who)  # noqa: E731
        act("ag1", action="open", url=f"http://127.0.0.1:{port}/")
        act("ag1", action="release")
        out = act("ag1", action="record_steps", do="start")
        self.assertEqual(out["steps_rec"], {"by": "the person", "steps": 0})

        def box(css):
            r = b.evaluate("(() => { const r = document.querySelector(%s).getBoundingClientRect(); "
                           "return [r.x + r.width / 2, r.y + r.height / 2]; })()" % json.dumps(css))
            return {"x": r[0], "y": r[1]}
        n = [0]

        def put(*evs):
            n[0] += 1
            routes.person_input(self.c, {"project": "web", "session": "ag1", "pane": "p", "n": n[0], "evs": list(evs)},
                                "mark")
            self.assertTrue(b.inputs.settle())
        m = {"alt": False, "ctrl": False, "meta": False, "shift": False}
        name = box("#name")
        put({"type": "mouse", "action": "down", **name, "button": "left", "clicks": 1},
            {"type": "mouse", "action": "up", **name, "button": "left", "clicks": 1})
        put(*[{"type": "key", "key": k, "mods": m} for k in "Ann"])
        save = box("tr:nth-of-type(2) button")
        put({"type": "mouse", "action": "down", **save, "button": "left", "clicks": 1},
            {"type": "mouse", "action": "up", **save, "button": "left", "clicks": 1})
        self.assertEqual(b.evaluate("document.querySelector('#out').textContent"), "saved Ann in row 2")
        sent = []
        with mock.patch.object(server, "session_send", lambda conn, to, text, **k: sent.append((to, text))):
            out = routes.steps_op(self.c, {"project": "web", "session": "ag1", "do": "stop"}, "mark")
        self.assertEqual(out["lines"], ["fill 'textbox \"Name\"' Ann", "click 'button \"Save\"' --in 'Row 2'"])
        self.assertEqual(sent[0][0], "ag1")
        self.assertIn(out["lines"][1], sent[0][1])
        # The lines, with {who} in place of the name, run again as one call with another value.
        lines = [ln.replace("Ann", "{who}") for ln in out["lines"]]
        act("mark", action="release")  # the person's clicks took control; they give it back
        act("ag1", action="open", url=f"http://127.0.0.1:{port}/")
        steps = [cli._browser_args(cli.build_parser(only="browser").parse_args(
            ["browser"] + __import__("shlex").split(ln.replace("{who}", "Bea")))) for ln in lines]
        res = routes.run(self.c, {"project": "web", "session": "ag1", "steps": steps}, "ag1")
        self.assertTrue(res["ok"], res)
        self.assertEqual(b.evaluate("document.querySelector('#out').textContent"), "saved Bea in row 2")


ROWS_FORM = (b"<title>Form</title><label>Name <input id=name></label><table>"
        b"<tr><td>Row 1</td><td><button onclick=\"save(1)\">Save</button></td></tr>"
        b"<tr><td>Row 2</td><td><button onclick=\"save(2)\">Save</button></td></tr></table><p id=out></p><script>"
        b"function save(n) { document.getElementById('out').textContent = 'saved ' + "
        b"document.getElementById('name').value + ' in row ' + n; }</script>")


STARS = "data:image/svg+xml;base64," + "PHN2Zz48L3N2Zz4" * 220  # 3.3 KB of an inline picture
PLACES = ("<title>Places</title><main><p>Results</p></main><aside><h1>Places</h1><div class=list>" + "".join(
    f"<article tabindex=0><h2>Place {n}</h2><img src='{STARS}' alt='rating: 4.{n % 10}'><p>{n} Main St</p>"
    f"<p>Open until 9</p></article>" for n in range(1, 21)) + "</div></aside>").encode()
CARD = ("<div class=card><h2><a href=/r/{k}>{name}</a></h2><a href=/map/{k}>{n} El Camino Real, Menlo Park, CA 94025</a>"
        "<span class=price>$$</span><ul class=tags><li><a href=/t/a>Date night</a></li><li><a href=/t/b>Patio</a></li>"
        "</ul><a href=/review/{k}>READ THE REVIEW</a></div>")
GUIDE = ("<title>Guide</title><main><h1>The best restaurants</h1><section><h2>The spots</h2>"
         + "".join((CARD.format(k=n, n=100 + n, name="Eylan" if n == 1 else f"Spot {n}")
                    + ("<div class=ad><a href=/near>Find the best spots nearby</a></div>" if n == 1 else "")
                    + ("<div class=slot><iframe title=ad></iframe></div>" if n == 3 else ""))
                   for n in range(1, 17)) + "</section></main>").encode()


class ExtractShapes(Base):
    """#5721 in a real Chrome: extract by a name finds the rows of a list whose rows are each one clickable element
    (a places list of <article tabindex=0>) and of a guide whose cards an ad and a link box cut into runs; html --scope
    shows an inline data: address as its type and size."""

    def test_places_list_and_guide_cards_by_name_and_html_without_data(self):
        if not chrome() or not browser.chrome_path():
            self.skipTest("no Chrome on this computer")
        if not _can_bind():
            self.skipTest("this sandbox does not allow a local port")

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = PLACES if self.path == "/places" else GUIDE
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
        act = lambda **a: routes.act(self.c, {"project": "web", **a}, None)  # noqa: E731
        act(action="open", url=f"http://127.0.0.1:{port}/places")
        f = [browser.parse_field(x) for x in ("name=h2", "rating=img@alt", "address=p")]
        res = act(action="extract", rows="Place 3", fields=f, limit=50)
        self.assertEqual(res["count"], 20)
        self.assertEqual(res["data"][2], {"name": "Place 3", "rating": "rating: 4.3", "address": "3 Main St"})
        html = act(action="html", scope="css:article")
        self.assertLess(html["bytes"], 600, html["html"])
        self.assertIn("data:image/svg+xml;base64,…(3300 bytes)", html["html"])

        act(action="open", url=f"http://127.0.0.1:{port}/guide")
        f = [browser.parse_field(x) for x in ("name=h2", "address=a[href^='/map']", "review=a[href^='/review']@href")]
        res = act(action="extract", rows='link "Eylan"', fields=f, limit=50)
        self.assertEqual(res["count"], 16)
        self.assertEqual(res["data"][0], {"name": "Eylan", "address": "101 El Camino Real, Menlo Park, CA 94025",
                                          "review": f"http://127.0.0.1:{port}/review/1"})
        self.assertEqual(res["data"][15]["name"], "Spot 16")
        by_review = act(action="extract", rows="READ THE REVIEW", within="Spot 5", fields=f[:1], limit=50)
        self.assertEqual(by_review["count"], 16)


class CutData(unittest.TestCase):
    def test_a_long_data_address_shows_its_type_and_size_and_a_short_one_stays(self):
        short = "data:image/gif;base64,R0lGODlhAQABAAAAACw="
        html = f'<img src="{STARS}"><i style="background:url(data:image/png;base64,{"A" * 500})"></i><img src="{short}">'
        self.assertEqual(browser.cut_data(html), '<img src="data:image/svg+xml;base64,…(3300 bytes)"><i style="background:'
                                                 'url(data:image/png;base64,…(500 bytes))"></i>' f'<img src="{short}">')


PLACE_ROWS = "".join(
    f"<article><h2>{n}. Place {n} Cucina</h2><img width=60 height=12 alt='rating: 4.{n % 10}' "
    f"src='data:image/gif;base64,R0lGODlhAQABAAAAACw='><span>({100 + n})</span><p>Italian · $$</p>"
    f"<p>{n} Santa Cruz Ave, Menlo Park</p><p>{'Great pasta and a kind staff. ' * 8}</p></article>"
    for n in range(1, 21))
MAP = "<div class=map><canvas width=300 height=200></canvas>" + "".join(f"<b>{n}</b>" for n in range(20, 0, -1)) + \
    "<button>Satellite</button></div>"


class TextBesideMap(Base):
    """#5722 in a real Chrome: when main holds only a map and a side bar (in main or beside it) holds the list, text
    reads the list (names, ratings, counts, cuisine, addresses) under 4 KB, --section finds a place's heading, and
    --grep finds the ratings."""

    def test_the_list_of_a_map_page_is_the_text(self):
        if not chrome() or not browser.chrome_path():
            self.skipTest("no Chrome on this computer")
        if not _can_bind():
            self.skipTest("this sandbox does not allow a local port")
        pages = {"/in": f"<title>Map</title><main>{MAP}<aside><h1>Places</h1>{PLACE_ROWS}</aside></main>",
                 "/beside": f"<title>Map</title><main>{MAP}</main><aside><h1>Places</h1>{PLACE_ROWS}</aside>",
                 "/article": "<title>Story</title><main><p>" + "A long story. " * 60 + "</p><aside><h2>Related</h2>"
                             "<p>Other stories that are not the story itself, and some more words here.</p>" * 4
                             + "</aside></main>"}

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = pages[self.path].encode()
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
        act = lambda **a: routes.act(self.c, {"project": "web", **a}, None)  # noqa: E731
        for path in ("/in", "/beside"):
            act(action="open", url=f"http://127.0.0.1:{port}{path}")
            t = act(action="text")
            self.assertLess(len(t["text"].encode()), 4000, path)
            self.assertEqual(t["rest"], 0, path)
            for n in (1, 13, 20):
                self.assertIn(f"{n}. Place {n} Cucina", t["text"], path)
                self.assertIn(f"{n} Santa Cruz Ave, Menlo Park", t["text"], path)
            self.assertIn("[rating: 4.3]", t["text"], path)
            self.assertIn("(113)", t["text"], path)
            sec = act(action="text", section="1. Place 1 Cucina")
            self.assertIn("1 Santa Cruz Ave", sec["text"], path)
            self.assertIn(("Great pasta and a kind staff. " * 8).strip(), sec["text"], path)  # a section keeps it whole
            self.assertIn("\nGreat pasta and a kind staff. Great pasta and a kind staff.…\n", t["text"], path)  # cut
            self.assertNotIn("2. Place 2", sec["text"], path)
            self.assertEqual(act(action="text", grep="rating:")["matches"], 20, path)
        # A story with a side bar of related links: the side bar stays out, and so does its heading.
        act(action="open", url=f"http://127.0.0.1:{port}/article")
        t = act(action="text")
        self.assertNotIn("Other stories", t["text"])
        with self.assertRaisesRegex(RiverError, 'no heading "Related"'):
            act(action="text", section="Related")


HIDDEN_MATCH = (b"<title>Lithium</title><main><details><summary>Minerals</summary><a href=/m>Spodumene</a></details>"
                b"<div style='width:0;height:0;overflow:hidden'><a href=/z>Spodumene</a></div>"
                b"<p>Ores such as <a href=/s>Spodumene</a> have a relatively high lithium content.</p></main>")


class HiddenMatches(Base):
    """#5779 in a real Chrome: of several elements with one name, the ones with no place to click (in a closed
    details, of zero size) say 'hidden' and come last; several matches still do nothing."""

    def test_hidden_matches_are_marked_and_last(self):
        if not chrome() or not browser.chrome_path():
            self.skipTest("no Chrome on this computer")
        if not _can_bind():
            self.skipTest("this sandbox does not allow a local port")

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(HIDDEN_MATCH)))
                self.end_headers()
                self.wfile.write(HIDDEN_MATCH)

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
        routes.act(self.c, {"project": "web", "action": "open", "url": f"http://127.0.0.1:{port}/"}, None)
        with self.assertRaises(browser.BrowserError) as e:
            browser.resolve_target(b, 'link "Spodumene"')
        # The link of the closed details has no box, so it is no match; the clipped one is, and says hidden.
        lines = e.exception.candidates
        self.assertEqual(e.exception.code, "ambiguous")
        self.assertEqual(len(lines), 2, lines)
        self.assertFalse(lines[0].endswith("(hidden)"), lines)
        self.assertTrue(lines[1].endswith("(hidden)"), lines)
        ref = lines[0].split()[0]
        out = routes.act(self.c, {"project": "web", "action": "click", "selector": ref}, None)
        self.assertTrue(out["url"].endswith("/s"), out["url"])  # the visible one, the first time
