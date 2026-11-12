"""The page walk check (#5516): source maps, their build from the released commit, the walk in the project's browser,
and one subject for each error group."""

import hashlib
import http.server
import json
import os
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from river import browser, core
from river.plugins import pages
from river.routes import browser as routes

B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def vlq(*nums):
    out = ""
    for n in nums:
        v = (-n << 1) | 1 if n < 0 else n << 1
        while True:
            d, v = v & 31, v >> 5
            out += B64[d | (32 if v else 0)]
            if not v:
                break
    return out


# The script and its map: line 0 is boom() of src/app.ts, line 1 its call.
APP_JS = 'function a(){throw new Error("boom 7")}\na();\n'
APP_TS = 'export function boom() {\n  // the error of the test\n  throw new Error("boom 7")\n}\n\nboom()\n'
APP_MAP = json.dumps({"version": 3, "file": "app.js", "sources": ["webpack://web/./src/app.ts"], "names": ["boom"],
                      "mappings": ",".join([vlq(0, 0, 0, 0, 0), vlq(13, 0, 2, 2), vlq(6, 0, 0, 6)]) + ";" + vlq(0, 0, 3, -8)})
BUILD = ("import pathlib\np = pathlib.Path('dist/assets'); p.mkdir(parents=True, exist_ok=True)\n"
         "(p / 'app.js').write_text(open('app.js.src').read()); (p / 'app.js.map').write_text(open('app.map.src').read())\n"
         "q = pathlib.Path('node_modules/x'); q.mkdir(parents=True, exist_ok=True); (q / 'x.js').write_text('x')\n"
         "(q / 'x.js.map').write_text('{}')\n")


def git(folder, *args):
    return subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, check=True).stdout.strip()


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

    def repo(self, js=APP_JS, command=None):
        """A git folder for the project, whose build writes dist/assets/app.js and its map; returns its commit."""
        f = Path(self.tmp.name, "repo")
        if not f.exists():
            f.mkdir()
            git(f, "init", "-q")
            git(f, "config", "user.email", "t@example.com")
            git(f, "config", "user.name", "t")
            (f / "src").mkdir()
            (f / "src" / "app.ts").write_text(APP_TS, encoding="utf-8")
            (f / "build.py").write_text(BUILD, encoding="utf-8")
            core.project_path(self.c, "web", str(f))
        (f / "app.js.src").write_text(js, encoding="utf-8")
        (f / "app.map.src").write_text(APP_MAP, encoding="utf-8")
        git(f, "add", "-A")
        git(f, "commit", "-q", "--allow-empty", "-m", "a build")
        count = Path(self.tmp.name, "builds")
        text = "The bundle is built by Vite.\ncommand: " + (command or f"python3 build.py && echo x >> {count}")
        if pages.map_command(self.c, "web") != text.split("command: ")[1]:
            core.ops_system_edit(self.c, "web", {"code.source_maps": text})
        self.folder = f
        return git(f, "rev-parse", "HEAD")

    def builds(self):
        p = Path(self.tmp.name, "builds")
        return len(p.read_text(encoding="utf-8").splitlines()) if p.exists() else 0


class Maps(Base):
    def test_the_map_gives_the_source_line_and_column_of_a_generated_position(self):
        m = pages.SourceMap(json.loads(APP_MAP))
        self.assertEqual(m.lookup(0, 2), {"source": "src/app.ts", "line": 1, "column": 1, "name": "boom"})
        self.assertEqual(m.lookup(0, 25), {"source": "src/app.ts", "line": 3, "column": 9, "name": None})
        self.assertEqual(m.lookup(1, 0)["line"], 6)
        self.assertIsNone(m.lookup(7, 0))
        index = pages.SourceMap({"version": 3, "sections": [{"offset": {"line": 3, "column": 0},
                                                             "map": json.loads(APP_MAP)}]})
        self.assertEqual(index.lookup(4, 0)["line"], 6)
        self.assertIsNone(index.lookup(1, 0))
        self.assertEqual([pages.clean_source(s) for s in ("webpack://app/./src/a.ts", "../../src/b.ts", "/c.js")],
                         ["src/a.ts", "src/b.ts", "c.js"])

    def test_the_maps_are_built_once_from_the_commit_in_production_without_changing_the_checkout(self):
        first = self.repo()
        self.assertEqual(pages.build_maps(self.c, "web")["note"],
                         "the project has no deploy target: the build of HEAD, and the hash check decides")
        second = self.repo(js=APP_JS.replace("boom 7", "boom 8"))
        core.target_add(self.c, "prod", "the servers")
        core.project_target(self.c, "web", "prod")
        it = core.item_add(self.c, "web", "release 1")["id"]
        self.c.execute("UPDATE items SET kind='deploy', target='prod', status='done', closed_at=?, cut_rev=? WHERE id=?",
                       (core.iso(core.now()), first[:10], it))
        self.c.commit()
        out = pages.build_maps(self.c, "web")
        self.assertEqual((out["rev"], out["note"]), (first, None))
        (entry,) = out["files"]["app.js"]
        self.assertEqual((entry["file"], entry["sha256"]), ("dist/assets/app.js", hashlib.sha256(APP_JS.encode()).hexdigest()))
        self.assertNotIn("x.js", out["files"])  # node_modules holds no map of the project
        self.assertEqual(json.loads((pages.map_dir("web", first) / entry["map"]).read_text(encoding="utf-8")), json.loads(APP_MAP))
        self.assertEqual((git(self.folder, "status", "--porcelain"), git(self.folder, "rev-parse", "HEAD")), ("", second))
        self.assertFalse((self.folder / "dist").exists())
        n = self.builds()
        pages.build_maps(self.c, "web")
        self.assertEqual(self.builds(), n)  # kept for the commit
        self.assertEqual(pages.build_maps(self.c, "web", rev="HEAD")["rev"], second)

    def test_a_failed_build_says_why_and_waits_before_it_runs_again(self):
        self.repo(command=f"echo x >> {Path(self.tmp.name, 'builds')}; echo 'vite: not found' >&2; exit 127")
        out = pages.build_maps(self.c, "web")
        self.assertEqual(out["error"], "the source map command ended with exit 127: vite: not found")
        self.assertEqual((pages.build_maps(self.c, "web")["error"], self.builds()), (out["error"], 1))
        self.repo(command="python3 -c 'print(1)'")
        self.assertEqual(pages.build_maps(self.c, "web")["error"], "the source map command wrote no .map file beside a script")

    def test_no_command_and_no_folder_say_what_to_set(self):
        self.assertIn("maxpm project path web <folder>", pages.build_maps(self.c, "web")["error"])
        self.repo()
        core.ops_system_edit(self.c, "web", {"code.source_maps": "Vite builds them"})
        self.assertIn('--set "code.source_maps=command: ', pages.build_maps(self.c, "web")["error"])


def walked(url="https://web.example/", sha=None, other=None):
    """A walk result as browser.walk gives it."""
    app = "https://web.example/assets/app.js"
    return {"project": "web", "pages": [
        {"url": url, "load": None, "scripts": {app: sha or hashlib.sha256(APP_JS.encode()).hexdigest(),
                                               "https://web.example/assets/vendor.js": "0" * 64},
         "errors": [
             {"kind": "exception", "message": "Error: boom 7", "frames": [
                 {"function": "a", "url": app, "line": 0, "column": 19},
                 {"function": "", "url": app, "line": 1, "column": 0}]},
             {"kind": "console", "message": "failed to load item 12", "frames": []},
             {"kind": "console", "message": "failed to load item 13", "frames": []},
             {"kind": "console", "message": "ResizeObserver loop completed", "frames": []},
             {"kind": "exception", "message": "TypeError: x is undefined", "frames": [
                 {"function": "t", "url": "https://web.example/assets/vendor.js", "line": 0, "column": 4}]}],
         "requests": [{"url": "https://web.example/api/items/123?x=1", "method": "GET", "type": "Fetch", "status": 500},
                      {"url": "https://web.example/api/items/456", "method": "GET", "type": "Fetch", "status": 500}]},
        {"url": "https://web.example/settings", "load": "net::ERR_NAME_NOT_RESOLVED", "scripts": {}, "errors": [],
         "requests": []},
        {"url": "https://web.example/late", "skipped": True}] + ([other] if other else [])}


class Groups(Base):
    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, pages, "WALK", None)
        os.environ["MAXPM_NOISE"] = "ResizeObserver loop"
        self.addCleanup(os.environ.pop, "MAXPM_NOISE", None)

    def walk(self, result):
        seen = []
        pages.WALK = lambda conn, project, ps, settle, timeout: seen.append((project, ps, settle, timeout)) or result
        return seen

    def test_each_error_group_is_a_subject_with_its_frames_mapped_to_the_source(self):
        self.repo()
        seen = self.walk(walked())
        res = pages.run("walk", "web", "https://web.example/,https://web.example/settings https://web.example/late",
                        2, 40)
        self.assertEqual(seen, [("web", ["https://web.example/", "https://web.example/settings",
                                         "https://web.example/late"], 2.0, 40.0)])
        self.assertEqual((res["status"], res["pages"], res["skipped"], res["noise"]), (2, 2, 1, 1))
        by = {g["message"]: g for g in res["groups"]}
        self.assertEqual(sorted(by), ["Error: boom 7", "GET https://web.example/api/items/123?x=1: HTTP 500",
                                      "TypeError: x is undefined", "failed to load item 12",
                                      "the page did not load: net::ERR_NAME_NOT_RESOLVED"])
        self.assertEqual(by["failed to load item 12"]["count"], 2)  # the numbers are general in a group
        self.assertEqual(by["GET https://web.example/api/items/123?x=1: HTTP 500"]["count"], 2)
        out = pages.lines(res)
        self.assertEqual(out[0], "PAGES CRITICAL - 5 error groups on 2 of 2 page(s): failed to load item 12; GET "
                                 "https://web.example/api/items/123?x=1: HTTP 500; Error: boom 7; 1 page(s) skipped at "
                                 "the timeout | groups=5 pages=2")
        boom = out.index(next(x for x in out if "CRITICAL Error: boom 7" in x))
        self.assertEqual(out[boom + 1:boom + 3], ["  at a (src/app.ts:3:9)", "  at <anonymous> (src/app.ts:6:1)"])
        self.assertIn("  at t (https://web.example/assets/vendor.js:1:5)", out)  # no map of vendor.js: as it ran
        # MaximizePM reads it as any plugin's output: one critical subject for each group, and the evidence pack finds
        # the source file of the project in the frames.
        subjects = core._ops_subjects([x for x in out[1:] if not x.startswith("{")], "critical")
        self.assertEqual(sorted(subjects), sorted(g["subject"] for g in res["groups"]))
        self.assertEqual({s["status"] for s in subjects.values()}, {"critical"})
        self.assertEqual(core._ops_frames("\n".join(out), str(self.folder))[0][:2], ("src/app.ts", 3))
        self.assertEqual(json.loads(out[-1])["maps"]["scripts"], 1)
        # The same errors on the next walk are the same subjects.
        again = pages.run("walk", "web", "https://web.example/", 2, 40)
        self.assertEqual({g["subject"] for g in again["groups"]}, set(subjects))

    def test_a_script_that_is_not_the_build_stays_minified_and_says_why(self):
        rev = self.repo()
        self.walk(walked(sha="f" * 64))
        res = pages.run("walk", "web", "https://web.example/")
        (boom,) = [g for g in res["groups"] if g["message"] == "Error: boom 7"]
        self.assertEqual(pages.frame_text(boom["frames"][0]),
                         f"at a (https://web.example/assets/app.js:1:20)  [the script the browser ran is not the build of "
                         f"{rev[:12]} (its SHA-256 differs)]")

    def test_no_error_is_ok_and_a_walk_that_did_not_run_is_unknown(self):
        self.walk({"project": "web", "pages": [{"url": "https://web.example/", "load": None, "scripts": {}, "errors": [],
                                                "requests": []}]})
        res = pages.run("walk", "web", "https://web.example/")
        self.assertEqual((res["status"], pages.lines(res)[0]),
                         (0, "PAGES OK - no browser error on 1 page(s) | groups=0 pages=1"))
        self.assertEqual(res["maps"]["note"], "no stack frame to map")  # no build when nothing needs it

        def refuse(*a):
            raise core.RiverError("maxpm serve does not answer")
        pages.WALK = refuse
        res = pages.run("walk", "web", "https://web.example/")
        self.assertEqual((res["status"], res["summary"]), (3, "the walk did not run: maxpm serve does not answer"))
        self.walk({"project": "web", "pages": [{"url": "https://web.example/", "skipped": True}]})
        self.assertEqual(pages.run("walk", "web", "https://web.example/")["status"], 3)


class FakeChrome:
    """The CDP calls of browser.walk: a navigation sends the events of a page to the walk's listener."""

    def __init__(self, events):
        self.project, self.attached, self.mine, self.listeners, self.calls = "web", None, set(), {}, []
        self.events = events

    def start(self):
        pass

    def call(self, method, params=None, session=None, timeout=None):
        self.calls.append(method)
        if method == "Target.createTarget":
            return {"targetId": "T1"}
        if method == "Target.attachToTarget":
            return {"sessionId": "S1"}
        if method == "Page.navigate":
            on = self.listeners["S1"]
            for m, p in self.events(params["url"]):
                on(m, p)
            return {"errorText": "net::ERR_CONNECTION_REFUSED"} if "down" in params["url"] else {}
        if method == "Debugger.getScriptSource":
            return {"scriptSource": APP_JS}
        return {}


class Walk(Base):
    def test_the_walk_records_errors_failed_requests_and_the_hash_of_each_script_a_frame_names(self):
        app = "https://web.example/assets/app.js"

        def events(url):
            if "down" in url:
                return []
            return [
                ("Debugger.scriptParsed", {"url": app, "scriptId": "7"}),
                ("Network.requestWillBeSent", {"requestId": "1", "request": {"url": url + "api/x", "method": "POST"},
                                               "type": "Fetch"}),
                ("Network.responseReceived", {"requestId": "1", "type": "Fetch", "response": {"status": 502}}),
                ("Network.requestWillBeSent", {"requestId": "2", "request": {"url": url + "ok.css"}}),
                ("Network.responseReceived", {"requestId": "2", "response": {"status": 200}}),
                ("Network.requestWillBeSent", {"requestId": "3", "request": {"url": url + "gone.png"}, "type": "Image"}),
                ("Network.loadingFailed", {"requestId": "3", "errorText": "net::ERR_FAILED"}),
                ("Network.loadingFailed", {"requestId": "4", "canceled": True}),
                ("Runtime.exceptionThrown", {"exceptionDetails": {
                    "text": "Uncaught", "exception": {"description": "Error: boom 7\n    at a (app.js:1:20)"},
                    "stackTrace": {"callFrames": [{"functionName": "a", "url": app, "lineNumber": 0, "columnNumber": 19}]}}}),
                ("Runtime.consoleAPICalled", {"type": "error", "args": [{"type": "string", "value": "bad"},
                                                                         {"type": "object", "description": "Object"}]}),
                ("Runtime.consoleAPICalled", {"type": "log", "args": [{"type": "string", "value": "fine"}]}),
                ("Log.entryAdded", {"entry": {"level": "error", "source": "network", "text": "Failed to load resource"}}),
                ("Log.entryAdded", {"entry": {"level": "error", "source": "security", "text": "Refused to frame",
                                              "url": url}}),
                ("Page.loadEventFired", {}),
            ]
        b = FakeChrome(events)
        out = browser.walk(b, ["https://web.example/", "https://down.example/"], settle=0, timeout=30)
        first, down = out["pages"]
        self.assertEqual(first["requests"], [
            {"url": "https://web.example/api/x", "method": "POST", "type": "Fetch", "status": 502},
            {"url": "https://web.example/gone.png", "method": "GET", "type": "Image", "error": "net::ERR_FAILED"}])
        self.assertEqual([(e["kind"], e["message"]) for e in first["errors"]],
                         [("exception", "Error: boom 7"), ("console", "bad Object"), ("log", "Refused to frame")])
        self.assertEqual(first["errors"][0]["frames"], [{"function": "a", "url": app, "line": 0, "column": 19}])
        self.assertEqual(first["scripts"], {app: hashlib.sha256(APP_JS.encode()).hexdigest()})
        self.assertEqual((down["load"], down["errors"]), ("net::ERR_CONNECTION_REFUSED", []))
        self.assertEqual((b.listeners, b.calls[-1]), ({}, "Target.closeTarget"))  # the tab goes; nothing listens
        self.assertIn("Debugger.setSkipAllPauses", b.calls)
        late = browser.walk(FakeChrome(events), ["https://web.example/"], settle=0, timeout=0.5)
        self.assertEqual(late["pages"], [{"url": "https://web.example/", "skipped": True}])

    def test_the_walk_action_needs_a_list_of_pages(self):
        with self.assertRaisesRegex(core.RiverError, "a list of addresses"):
            routes.act(self.c, {"action": "walk", "project": "web", "pages": "https://x/"}, None)


def _can_bind():
    try:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
        return True
    except OSError:
        return False


class RealWalk(Base):
    def test_a_real_page_walk_maps_the_exception_to_the_source_and_reports_the_failed_request(self):
        if os.environ.get("MAXPM_NO_BROWSER") or not browser.chrome_path():
            self.skipTest("no Chrome on this computer")
        if not _can_bind():
            self.skipTest("this sandbox does not allow a local port")
        self.repo()
        files = {"/": (b'<title>Walk</title><script>fetch("/api/items/42")</script>'
                       b'<script src="/assets/app.js"></script>', "text/html"),
                 "/assets/app.js": (APP_JS.encode(), "text/javascript"), "/favicon.ico": (b"", "image/x-icon")}

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body, kind = files.get(self.path, (b"no", "text/plain"))
                self.send_response(200 if self.path in files else 500)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        port = srv.server_address[1]
        core.config_set(self.c, "browser_allow", f"127.0.0.1:{port}", project="web")
        browser.BROWSERS.clear()
        b = browser.get("web")
        self.addCleanup(browser.BROWSERS.clear)
        self.addCleanup(b.close)
        pages.WALK = lambda conn, project, ps, settle, timeout: browser.walk(b, ps, settle, timeout)
        self.addCleanup(setattr, pages, "WALK", None)
        res = pages.run("walk", "web", f"http://127.0.0.1:{port}/", 1, 30)
        by = {g["kind"]: g for g in res["groups"]}
        self.assertEqual(sorted(by), ["exception", "request"], res)
        self.assertEqual(by["request"]["message"], f"GET http://127.0.0.1:{port}/api/items/42: HTTP 500")
        top = by["exception"]["frames"][0]
        self.assertEqual((by["exception"]["message"], top["source"], top["source_line"]),
                         ("Error: boom 7", "src/app.ts", 3), top)
        self.assertEqual(b.info()["tabs"][0]["url"], "about:blank")  # the person's tab did not move
