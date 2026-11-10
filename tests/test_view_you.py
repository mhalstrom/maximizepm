"""The You view of Sessions (the Board's until #5102; #4656: the tests of each view in a file of their own): its
message routes."""
import contextlib
import io
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from river import core, server
from river.core import RiverError


class PageMessages(unittest.TestCase):
    """The page's message routes: inbox, thread, item messages, and the send/offer/give/split/decline actions."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["MAXPM_DB"] = os.path.join(self.dir.name, "t.db")
        self.c = core.connect()
        core.project_add(self.c, "a")
        for n in ("ag", "bo"):
            core.register(self.c, n)
        core.register(self.c, "mark", human=True)
        self.big = core.item_add(self.c, "a", "big", actor="ag")["id"]
        core.claim(self.c, self.big, "ag")

    def tearDown(self):
        self.c.close()
        os.environ.pop("MAXPM_DB", None)
        self.dir.cleanup()

    def op(self, name, who, **args):
        return server.OPS[name](self.c, args, who)

    def get(self, path):
        h = server.Handler.__new__(server.Handler)
        h.path, h.client_address, h.headers, out = path, ("127.0.0.1", 5555), {"Host": "127.0.0.1:8765"}, {}
        h._send = lambda code, body, ctype=None: out.update(code=code, body=body)
        h.do_GET()
        return out["code"], out["body"]

    def test_offer_split_give_and_inbox(self):
        offer = self.op("offer", "bo", item=self.big, body="I can take the tests")
        code, body = self.get("/api/inbox?agent=ag")
        self.assertEqual((code, [m["id"] for m in body["messages"]]), (200, [offer["id"]]))
        self.assertTrue(body["messages"][0]["unread"])  # the page only looks
        res = self.op("split", "ag", id=self.big, titles=["tests", " ", "docs"])
        self.assertEqual(len(res["split_into"]), 2)
        self.assertEqual(core.message_show(self.c, offer["id"])["state"], "accepted")
        self.op("give", "ag", id=self.big, to="bo")
        self.assertEqual(core._item(self.c, self.big)["assignee"], "bo")

    def test_every_route_answers_this_computer_only(self):
        # maxpm serve has no sign-in. A page of another site whose name is made to point at 127.0.0.1 (DNS
        # rebinding) is same-origin for the browser, so the Host header decides, for reads and for actions.
        import io

        def call(method, path, headers, body=None, ip="127.0.0.1"):
            raw = json.dumps(body).encode() if body is not None else b""
            h = server.Handler.__new__(server.Handler)
            h.path, h.client_address, h.rfile, out = path, (ip, 5555), io.BytesIO(raw), {}
            h.headers = {"Content-Length": str(len(raw)), **headers}
            h._send = lambda code, body, ctype=None: out.update(code=code, body=body)
            getattr(h, "do_" + method)()
            return out["code"], out["body"]
        note = {"op": "send", "actor": "bo", "args": {"kind": "note", "body": "hello", "to": "ag"}}
        local, sent = {"Host": "127.0.0.1:8765"}, lambda: len(core.inbox(self.c, "ag", True, mark_read=False))
        for headers in (local, {**local, "Origin": "http://127.0.0.1:8765"},  # a river command; the page
                        {"Host": "localhost:8765", "Origin": "http://localhost:8765"}):
            self.assertEqual([call("GET", path, headers)[0] for path in ("/", "/app.js", "/api/state", f"/api/item/{self.big}")],
                             [200] * 4, headers)
            self.assertEqual(call("POST", "/api/action", headers, note)[0], 200, headers)
        self.assertEqual(sent(), 3)
        for headers, ip, why in (
                ({"Host": "evil.example:8765", "Origin": "http://evil.example:8765"}, "127.0.0.1", "only on 127.0.0.1"),
                ({"Host": "evil.example:8765"}, "127.0.0.1", "only on 127.0.0.1"),
                ({}, "127.0.0.1", "only on 127.0.0.1"),
                ({**local, "Origin": "https://evil.example"}, "127.0.0.1", "cross-origin"),
                ({**local, "X-Forwarded-For": "203.0.113.9"}, "127.0.0.1", "tunnel or proxy"),
                (local, "192.168.1.20", "only this computer")):
            for method, path in (("GET", "/"), ("GET", "/app.js"), ("GET", "/api/state"), ("GET", f"/api/item/{self.big}"),
                                 ("GET", "/api/inbox?agent=ag"), ("POST", "/api/action")):
                code, body = call(method, path, headers, note, ip)
                self.assertEqual(code, 403, (headers, path))
                self.assertIn(why, body["error"])
        # Another page on this computer is not river's page: it reads nothing it could not read before, and acts on nothing.
        code, body = call("POST", "/api/action", {**local, "Origin": "http://localhost:3000"}, note)
        self.assertEqual((code, body["error"]), (403, "cross-origin request refused"))
        self.assertEqual(sent(), 3)

    def test_question_reply_decline_thread_and_item_messages(self):
        q = self.op("send", "bo", kind="question", body="Which port?", to="mark", item=self.big)
        self.op("send", "mark", kind="note", body="8765", reply=q["id"])
        alert = self.op("send", "ag", kind="alert", body="stop", to="bo")
        self.op("decline_message", "bo", msg=alert["id"], note="busy")
        self.assertEqual(core.message_show(self.c, alert["id"])["state"], "declined")
        code, body = self.get(f"/api/thread/{q['id']}")
        self.assertEqual([m["body"] for m in body["messages"]], ["Which port?", "8765"])
        code, body = self.get(f"/api/item/{self.big}")
        self.assertEqual(len(body["messages"]), 2)
        self.assertEqual(self.get("/api/inbox")[0], 400)  # no agent named

    def test_the_page_moves_an_item_to_the_backlog_and_activates_it(self):
        x = core.item_add(self.c, "a", "someday", actor="mark")["id"]
        y = core.item_add(self.c, "a", "after it", after=[x], actor="mark")["id"]
        with self.assertRaisesRegex(core.RiverError, "--force"):
            self.op("backlog", "mark", id=x, note="not now")
        self.op("backlog", "mark", id=x, note="not now", force="moved from the page")
        code, body = self.get("/api/state")
        it = next(i for i in body["items"] if i["id"] == x)
        self.assertEqual((it["backlog"], it["backlog_note"], it["ready"]), (True, "not now", False))
        code, body = self.get(f"/api/item/{y}")
        self.assertEqual(body["waits_on_detail"][0]["status"], "backlog")
        with self.assertRaisesRegex(core.RiverError, "in the backlog"):
            server.open_agent_on(self.c, x)
        self.assertTrue(self.op("activate", "mark", id=x)["ready"])

    def test_search_route_finds_open_done_and_dropped_items(self):
        # The search field of the page header (#2968): core.search with the filters of maxpm search.
        core.project_add(self.c, "b")
        done = core.item_add(self.c, "a", "fix the login page", actor="ag")["id"]
        core.claim(self.c, done, "bo")
        core.done(self.c, done, output="abc123: login works", actor="bo")
        gone = core.item_add(self.c, "a", "login page redesign", actor="ag")["id"]
        core.drop(self.c, gone, actor="mark")
        other = core.item_add(self.c, "b", "login page tests", actor="ag")["id"]
        code, body = self.get("/api/search?q=login+page")
        self.assertEqual((code, [m["id"] for m in body["items"]]), (200, [other, gone, done]))  # newest first
        self.assertEqual({m["id"]: m["status"] for m in body["items"]}, {other: "open", gone: "dropped", done: "done"})
        self.assertEqual(next(m for m in body["items"] if m["id"] == done)["output"], "abc123: login works")
        self.assertEqual([m["id"] for m in self.get("/api/search?q=login&status=done")[1]["items"]], [done])
        self.assertEqual([m["id"] for m in self.get("/api/search?q=login&status=closed&project=a")[1]["items"]],
                         [gone, done])
        self.assertEqual([m["id"] for m in self.get("/api/search?q=login&project=b&since=7d")[1]["items"]], [other])
        code, body = self.get("/api/search?q=login&n=1")
        self.assertEqual((body["total"], len(body["items"])), (3, 1))
        code, body = self.get("/api/search?q=login+redesign+checkout")  # no item holds every word: the closest
        self.assertEqual((code, body["items"]), (200, []))
        self.assertIn(gone, [m["id"] for m in body["closest"]])
        self.assertEqual(self.get("/api/search?q=")[0], 400)
        self.assertEqual(self.get("/api/search?q=login&status=nope")[0], 400)
        self.assertEqual(self.get("/api/search?q=login&n=x")[0], 400)

    def test_page_has_the_search_field(self):
        html = (Path(server.__file__).parent / "static" / "index.html").read_text()
        for el in ('id="search"', 'id="searchDlg"', 'id="searchText"', 'id="searchStatus"', 'id="searchProject"'):
            self.assertIn(el, html)
        self.assertIn('fetch("api/search?"', (Path(server.__file__).parent / "static" / "app.js").read_text())
