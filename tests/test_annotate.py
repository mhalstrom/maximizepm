"""Annotate in the Browser pane (#5655): the stored sets, the text for the session, the states of the notes, the
commands, the page's op, the go briefing, and the selector code in one real Chrome when there is one."""

import base64
import contextlib
import http.server
import io
import os
import struct
import tempfile
import threading
import unittest
import zlib
from unittest import mock

from tests import chrome
from river import annotate, browser, cli, core, server
from river.core import RiverError
from river.routes import browser as routes


def png(w=4, h=3):
    """A small real PNG, as base64."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
    body = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    return base64.b64encode(body).decode()


NOTES = [
    {"kind": "element", "text": "The label is cut off", "at": {"x": 10, "y": 20},
     "box": {"x": 8, "y": 18, "w": 60, "h": 20}, "page": {"x": 8, "y": 118, "w": 60, "h": 20},
     "ref": {"selector": "#save", "tag": "button", "text": "Save"}},
    {"kind": "box", "text": "Too much space\nhere", "box": {"x": 40, "y": 200, "w": 300, "h": 120},
     "page": {"x": 40, "y": 300, "w": 300, "h": 120}, "ref": {"selector": "main > div", "tag": "div", "text": ""},
     "inside": [{"selector": "h2", "tag": "h2", "text": "Plans"}]},
    {"kind": "text", "text": "", "at": {"x": 500, "y": 300}, "page": {"x": 500, "y": 400}},
]


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
        core.register(self.c, "mark", human=True)
        core.register(self.c, "ag1")
        self.item = core.item_add(self.c, "web", "Fix the pricing page", actor="mark")["id"]
        core.claim(self.c, self.item, "ag1")
        browser.BROWSERS.clear()
        self.addCleanup(browser.BROWSERS.clear)

    def save(self, notes=NOTES, **kw):
        return annotate.save(self.c, "web", "ag1", "mark", "https://example.com/pricing", "Pricing", 1280, 860, png(),
                             notes, {"pen": 2, "arrow": 1}, **kw)

    def last_event(self):
        return self.c.execute("SELECT change FROM events WHERE item_id=? ORDER BY id DESC LIMIT 1",
                              (self.item,)).fetchone()["change"]

    def cli(self, *words):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            cli.run(list(words))
        return out.getvalue()


class Store(Base):
    def test_a_set_keeps_the_picture_the_notes_and_the_held_item(self):
        s = self.save()
        self.assertEqual((s["agent"], s["item_id"], s["author"], s["open"]), ("ag1", self.item, "mark", 3))
        with open(s["picture"], "rb") as f:
            self.assertTrue(f.read().startswith(b"\x89PNG"))
        self.assertEqual([x["n"] for x in s["notes"]], [1, 2, 3])
        self.assertEqual(s["notes"][1]["inside"], [{"selector": "h2", "tag": "h2", "text": "Plans"}])
        self.assertIn("annotations a", self.last_event())

    def test_the_text_names_each_element_as_annotatetofix_does(self):
        s = self.save()
        t = annotate.prompt(s, "mark", attached=True)
        self.assertIn(f"[maxpm annotations a{s['id']} from mark] Page: https://example.com/pricing (Pricing)", t)
        self.assertIn("Picture (attached): ", t)
        self.assertIn('1. Element `<button>` found by `#save`, text "Save"\n   Problem: The label is cut off', t)
        self.assertIn("2. Area 300 × 120 px at x 40, y 300 of the page; inside `<div>` found by `main > div`, no text; "
                      'in the area: `<h2>` found by `h2`, text "Plans"\n   Problem: Too much space\n   here', t)
        self.assertIn("3. At x 500, y 400 of the page\n   Problem: (no text: see the picture)", t)
        self.assertIn("Drawings in the picture: 2 pen lines, 1 arrow.", t)
        self.assertIn(f"maxpm browser annotation {s['id']} fix <n>", t)

    def test_a_long_set_is_cut_to_fit_the_chat(self):
        s = self.save([{"kind": "text", "text": "x" * 500, "at": {"x": 1, "y": 1}}] * 20)
        t = annotate.prompt(s, "mark", limit=annotate.SEND_MAX)
        self.assertLessEqual(len(t), annotate.SEND_MAX)
        self.assertIn(f"more: maxpm browser annotation {s['id']}", t)

    def test_fix_not_fixed_and_reopen(self):
        s = self.save()
        with self.assertRaisesRegex(RiverError, "what you changed"):
            annotate.mark(self.c, s["id"], 1, "fix", "", "ag1")
        with self.assertRaisesRegex(RiverError, "notes 1 to 3"):
            annotate.mark(self.c, s["id"], 9, "fix", "x", "ag1")
        s = annotate.mark(self.c, f"a{s['id']}", 1, "fix", "Wider button, commit abc123", "ag1")
        self.assertEqual((s["notes"][0]["state"], s["notes"][0]["fix"]["note"], s["open"]),
                         ("fixed", "Wider button, commit abc123", 2))
        s = annotate.mark(self.c, s["id"], 2, "not-fixed", "The space comes from the CMS", "ag1")
        self.assertEqual((s["notes"][1]["state"], s["open"]), ("not_fixed", 2))
        s = annotate.mark(self.c, s["id"], 1, "reopen", "Still cut on a phone", "mark")
        self.assertEqual((s["notes"][0]["state"], s["notes"][0]["again"], s["open"]), ("open", "Still cut on a phone", 3))
        self.assertIn("Still wrong: Still cut on a phone", annotate.prompt(s))
        # Fixed notes leave the text for the session.
        s = annotate.mark(self.c, s["id"], 3, "fix", "done", "ag1")
        self.assertNotIn("\n3. ", annotate.prompt(s))
        self.assertIn("note 3: fixed: done", self.last_event())

    def test_the_sets_of_a_session_or_its_item_and_the_go_briefing(self):
        s = self.save()
        self.assertEqual([x["id"] for x in annotate.sets(self.c, agent="ag1")], [s["id"]])
        self.assertEqual([x["id"] for x in annotate.sets(self.c, agent="ag9", item_id=self.item)], [s["id"]])
        self.assertEqual(annotate.brief(self.c, "ag9", self.item),
                         [{"id": s["id"], "url": "https://example.com/pricing", "open": 3, "notes": 3}])
        for n in (1, 2, 3):
            annotate.mark(self.c, s["id"], n, "fix", "ok", "ag1")
        self.assertEqual(annotate.brief(self.c, "ag1", self.item), [])

    def test_bad_input_is_refused(self):
        with self.assertRaisesRegex(RiverError, "PNG"):
            annotate.save(self.c, "web", "ag1", "mark", "u", "", 1, 1, base64.b64encode(b"GIF89a").decode(), [])
        with self.assertRaisesRegex(RiverError, "element, a box, or a text"):
            self.save([{"kind": "pen"}])
        with self.assertRaisesRegex(RiverError, "no annotation set"):
            annotate.show(self.c, 999)


class Commands(Base):
    def test_list_show_and_fix(self):
        s = self.save()
        out = self.cli("--as", "ag1", "browser", "annotations")
        self.assertIn(f"a{s['id']}  ", out)
        self.assertIn("3 note(s), 3 open", out)
        out = self.cli("--as", "ag1", "browser", "annotation", str(s["id"]))
        self.assertIn(f"picture: {s['picture']}", out)
        self.assertIn("1. [open] Element `<button>` found by `#save`", out)
        out = self.cli("--as", "ag1", "browser", "annotation", str(s["id"]), "fix", "1", "--note", "Wider button")
        self.assertIn("1. [fixed] Element", out)
        self.assertIn("   ag1: Wider button", out)
        self.assertIn("2 open", self.cli("--as", "ag1", "browser", "annotations", "--open"))
        with self.assertRaisesRegex(RiverError, "name the note"):
            cli.run(["--as", "ag1", "browser", "annotation", str(s["id"]), "fix"])

    def test_the_go_briefing_names_an_open_set(self):
        s = self.save()
        out = self.cli("--as", "ag1", "go", "--project", "web")
        self.assertIn(f"marked-up page a{s['id']} (https://example.com/pricing): 3 of 3 note(s) open", out)


class PageOp(Base):
    def test_send_stores_and_types_one_message_with_the_picture(self):
        self.assertIn("browser_annotate", server.TYPING_OPS)
        sent = []
        with mock.patch.object(server, "session_send", lambda conn, to, text, **kw: sent.append((to, text, kw)) or {}):
            r = server.OPS["browser_annotate"](self.c, {
                "project": "web", "session": "ag1", "url": "https://example.com/pricing", "title": "Pricing",
                "w": 1280, "h": 860, "png": png(), "notes": NOTES, "drawings": {"pen": 1}, "send": True}, "mark")
        self.assertEqual((r["sent"], r["told"]), (True, "ag1"))
        to, text, kw = sent[0]
        self.assertEqual((to, kw["hold"], kw["files"][0]["name"]), ("ag1", True, f"annotations-a{r['id']}.png"))
        self.assertTrue(text.startswith(f"[maxpm annotations a{r['id']} from mark]"))
        self.assertTrue(annotate.show(self.c, r["id"])["sent_at"])

    def test_copy_needs_no_session_and_send_again_keeps_the_set(self):
        r = server.OPS["browser_annotate"](self.c, {"project": "web", "url": "u", "w": 10, "h": 10, "png": png(),
                                                     "notes": NOTES[:1]}, "mark")
        self.assertFalse(r["sent"])
        self.assertIn("Element `<button>`", r["prompt"])
        with self.assertRaisesRegex(RiverError, "no session to send to"):
            server.OPS["browser_annotate"](self.c, {"project": "web", "url": "u", "png": png(), "send": True}, "mark")
        with mock.patch.object(server, "session_send", lambda *a, **kw: {}):
            again = server.OPS["browser_annotate"](self.c, {"project": "web", "session": "ag1", "url": "u", "png": png(),
                                                            "notes": NOTES, "send": True, "id": r["id"]}, "mark")
        self.assertEqual((again["id"], len(again["notes"])), (r["id"], 3))

    def test_a_session_cannot_send_and_a_failed_chat_says_where_it_is(self):
        with self.assertRaisesRegex(RiverError, "only a person"):
            server.OPS["browser_annotate"](self.c, {"project": "web", "png": png()}, "ag1")

        def fail(*a, **kw):
            raise RiverError("ag1 does not run in a tmux pane on this computer")
        with mock.patch.object(server, "session_send", fail):
            with self.assertRaisesRegex(RiverError, r"stored as a\d+, but the chat of ag1 did not get it"):
                server.OPS["browser_annotate"](self.c, {"project": "web", "session": "ag1", "url": "u", "png": png(),
                                                        "notes": [], "send": True}, "mark")

    def test_the_pane_lists_the_sets_and_reopens_a_note(self):
        s = self.save()
        annotate.mark(self.c, s["id"], 1, "fix", "ok", "ag1")
        r = server.OPS["browser"](self.c, {"action": "annotations", "project": "web", "session": "ag1"}, "mark")
        self.assertEqual([x["id"] for x in r["sets"]], [s["id"]])
        r = server.OPS["browser"](self.c, {"action": "annotation_mark", "project": "web", "session": "ag1",
                                           "id": s["id"], "n": 1}, "mark")
        self.assertEqual(r["sets"][0]["notes"][0]["state"], "open")
        with self.assertRaisesRegex(RiverError, "maxpm browser annotation"):
            server.OPS["browser"](self.c, {"action": "annotation_mark", "project": "web", "id": s["id"], "n": 1}, "ag1")

    def test_freeze_gives_the_picture_and_the_size_of_the_view_now(self):
        # #5660: the frame that the pane shows can be older than its last resize; the drawing takes this size.
        b = browser.get("web", "ag1")
        with mock.patch.object(b, "evaluate", lambda js, timeout=None: [454, 524]), \
                mock.patch.object(browser, "screenshot", lambda b: {"png": png(), "url": "u"}):
            r = server.OPS["browser"](self.c, {"action": "freeze", "project": "web", "session": "ag1"}, "mark")
        self.assertEqual((r["view"], r["png"]), ([454, 524], png()))

    def test_the_marks_of_this_page_follow_its_scroll(self):
        s = self.save()
        b = browser.get("web", "ag1")
        with mock.patch.object(b, "running", lambda: True), \
                mock.patch.object(b, "info", lambda: {"url": "https://example.com/pricing"}), \
                mock.patch.object(annotate, "page_call",
                                  lambda b, mode, arg: {"scroll": [0, 100], "at": [{"x": 9, "y": 19, "w": 61, "h": 21},
                                                                                   None, None]}):
            sets = routes.annotations_here(b, [annotate.show(self.c, s["id"])])
        here = [x["here"] for x in sets[0]["notes"]]
        self.assertEqual(here[0], {"x": 9, "y": 19, "w": 61, "h": 21})  # the element where the page has it now
        self.assertEqual(here[1], {"x": 40, "y": 200, "w": 300, "h": 120})  # its place on the page less the scroll
        self.assertEqual((here[2]["x"], here[2]["y"]), (500, 300))


class RealChrome(Base):
    def test_pick_area_and_locate_in_a_page(self):
        if not chrome() or not browser.chrome_path():
            self.skipTest("no Chrome on this computer")
        import socket
        try:
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
        except OSError:
            self.skipTest("this sandbox does not allow a local port")
        body = (b"<style>body{margin:0}</style><main id=m><div class=row style='height:80px'>"
                b"<button data-testid=save style='width:100px;height:30px'>Save</button></div>"
                b"<div style='height:200px'><h2 style='margin:0'>Plans</h2><p style='margin:0'>Two</p></div></main>")

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
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
        b = browser.get("web")
        self.addCleanup(b.close)
        b.resize(640, 480)
        b.open(f"http://127.0.0.1:{port}/")
        r = annotate.page_call(b, "pick", {"x": 20, "y": 10})
        self.assertEqual(r["ref"], {"selector": '[data-testid="save"]', "tag": "button", "text": "Save"})
        self.assertEqual((r["box"]["w"], r["box"]["h"]), (100, 30))
        r = annotate.page_call(b, "area", {"x": 0, "y": 82, "w": 300, "h": 60})
        self.assertEqual(r["ref"]["selector"], "div:nth-of-type(2)")  # the shortest path that is unique
        self.assertEqual([x["text"] for x in r["inside"]], ["Plans", "Two"])
        r = annotate.page_call(b, "locate", [{"selector": "#gone", "tag": "button", "text": "Save"}, None])
        self.assertEqual((r["at"][0]["w"], r["at"][1]), (100, None))  # found again by its text


if __name__ == "__main__":
    unittest.main()
