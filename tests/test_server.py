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


class PageGoals(unittest.TestCase):
    """Goals on the page: the state carries goals with progress; the goal ops and item tags work."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["MAXPM_DB"] = os.path.join(self.dir.name, "t.db")
        self.c = core.connect()
        core.project_add(self.c, "a")
        core.register(self.c, "ag")

    def tearDown(self):
        self.c.close()
        os.environ.pop("MAXPM_DB", None)
        self.dir.cleanup()

    def op(self, _op, who, **args):
        return server.OPS[_op](self.c, args, who)

    def get(self, path):
        h = server.Handler.__new__(server.Handler)
        h.path, h.client_address, h.headers, out = path, ("127.0.0.1", 5555), {"Host": "127.0.0.1:8765"}, {}
        h._send = lambda code, body, ctype=None: out.update(code=code, body=body)
        h.do_GET()
        return out["code"], out["body"]

    def test_item_models_through_the_page(self):
        i = self.op("item_add", None, project="a", title="x", models={"model": "opus", "effort": "high"})["id"]
        self.op("item_edit", None, id=i, models={"max_model": "fable", "effort": "none"})
        code, st = self.get("/api/state")
        it = next(x for x in st["items"] if x["id"] == i)
        self.assertEqual((it["model"], it["effort"], it["max_model"]), ("opus", None, "fable"))
        self.assertEqual(st["effort_levels"], ["low", "medium", "high", "xhigh", "max"])
        self.assertIn("claude", st["model_ladder"])

    def test_goal_ops_tags_and_state(self):
        self.op("goal_add", None, project="a", name="ship", outcome="it ships", done_when="users can install it")
        self.op("goal_add", None, project="a", name="docs")
        one = self.op("item_add", None, project="a", title="build", goals=["ship"])["id"]
        two = self.op("item_add", None, project="a", title="loose")["id"]
        self.op("item_edit", None, id=two, goals=["docs"])
        self.op("item_edit", None, id=two, untag=["docs"])
        self.op("goal_rank", None, name="docs", rank=1)
        self.op("goal_own", "ag", name="ship")
        code, st = self.get("/api/state")
        goals = {g["name"]: g for g in st["goals"]}
        self.assertEqual([g["name"] for g in st["goals"]], ["docs", "ship"])
        self.assertEqual((goals["ship"]["owner"], goals["ship"]["items_open"]), ("ag", [one]))
        self.assertEqual({i["id"]: i["goals"] for i in st["items"]}, {one: ["ship"], two: []})
        self.op("goal_done", "ag", name="ship", result="shipped", drop_open=True)
        self.op("goal_edit", None, name="docs", outcome="readers find answers", done_when="guide exists")
        code, st = self.get("/api/state")
        goals = {g["name"]: g for g in st["goals"]}
        self.assertEqual((goals["ship"]["status"], goals["ship"]["result"]), ("complete", "shipped"))
        self.assertEqual(goals["docs"]["outcome"], "readers find answers")
        self.op("goal_reopen", None, name="ship")
        self.assertEqual(self.op("goal_edit", None, name="ship", shared=True)["shared"], 1)  # the page: no owner
        with self.assertRaisesRegex(Exception, "shared"):
            self.op("goal_own", "ag", name="ship")
        self.assertEqual(core.goal_show(self.c, "ship")["status"], "open")



class LaunchAgent(unittest.TestCase):
    def setUp(self):
        self.platform, core.PLATFORM = core.PLATFORM, "darwin"  # the Terminal tests; Windows has its own
        self.dir = tempfile.TemporaryDirectory()
        os.environ["MAXPM_DB"] = os.path.join(self.dir.name, "t.db")
        self.c = core.connect()
        core.config_set(self.c, "claude_skill_prompt", "off")  # exact commands; LaunchProfiles tests the skill file

    def tearDown(self):
        core.PLATFORM = self.platform
        self.c.close()
        os.environ.pop("MAXPM_DB", None)
        self.dir.cleanup()

    def test_a_page_on_a_riverdb_queue_starts_agents_on_it(self):
        core.project_add(self.c, "shop", path=self.dir.name)
        core.item_add(self.c, "shop", "first")
        sent, old = [], os.environ.get("MAXPM_DB")
        os.environ["MAXPM_DB"] = os.path.join(self.dir.name, "q.db")
        try:
            server.launch_agent(self.c, runner=sent.append)
        finally:
            if old is None:
                del os.environ["MAXPM_DB"]
            else:
                os.environ["MAXPM_DB"] = old
        self.assertRegex(sent[0], r"MAXPM_DB=\S*q\.db'? MAXPM_AGENT=\S+ MAXPM_FOCUS=item:\d+ claude --name \S+ \S+ go")  # the path is quoted when it needs it

    def test_goal_context_session_builds_a_base_and_workers_start_as_forks(self):
        import json
        from river import cli
        sid = "44444444-4444-4444-8444-444444444444"
        root = os.path.join(self.dir.name, "claude")
        os.makedirs(os.path.join(root, "projects", "-shop"))
        with open(os.path.join(root, "projects", "-shop", sid + ".jsonl"), "w") as f:
            f.write(json.dumps({"type": "assistant", "timestamp": core.iso(core.now()), "message": {"id": "m", "usage": {
                "input_tokens": 3, "cache_read_input_tokens": 30000, "cache_creation_input_tokens": 9000}}}) + "\n")
        core.project_add(self.c, "shop", path=self.dir.name)
        core.goal_add(self.c, "shop", "checkout", "customers can pay", actor="t")
        a = core.item_add(self.c, "shop", "pay button", goals=["checkout"])["id"]
        b = core.item_add(self.c, "shop", "receipt mail", goals=["checkout"])["id"]
        c = core.item_add(self.c, "shop", "fix a typo in the footer", goals=["checkout"])["id"]
        core.item_goal_context(self.c, c, False, "t")
        sent = []
        self.assertEqual(server.auto_context(self.c, runner=sent.append), [])  # goal_context is off by default
        core.config_set(self.c, "goal_context", "on")
        self.assertEqual(server.auto_context(self.c, runner=sent.append), ["checkout"])
        self.assertIn("MAXPM_FOCUS=context:checkout", sent[0])
        self.assertEqual(server.auto_context(self.c, runner=sent.append), [])  # one start per CONTEXT_RETRY
        # The context session: its own briefing, no claims, then it records itself as the base.
        g = core.go(self.c, self.dir.name, focus="context:checkout")
        me = g["agent"]
        self.assertEqual((g["role"], [x["id"] for x in g["context"]["items"]]), ("context", [a, b]))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.render_go(g)
        self.assertIn(f"maxpm --as {me} goal base checkout --ready", out.getvalue())
        with self.assertRaisesRegex(RiverError, "context session"):
            core.claim(self.c, a, me)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": root}):
            r = core.goal_base(self.c, "checkout", me, ready=True, session=sid, cwd=self.dir.name)
        self.assertEqual((r["base"]["tokens"], r["base"]["warm"], r["warning"]), (39003, True, None))
        # A worker of the goal starts as a fork of the warm base; the fork keeps it warm.
        t = server.dispatch_item(self.c, a, runner=sent.append)
        self.assertEqual(t["fork_of"]["session_id"], sid)
        self.assertIn(f"MAXPM_FORK_OF=checkout", sent[-1])
        self.assertIn(f"claude --name checkout --resume {sid} --fork-session go --remote-control checkout", sent[-1])
        self.assertEqual(core.goal_base(self.c, "checkout")["base"]["forks"], 1)
        # Fresh instead: an item that needs no goal context, another model, a cold base.
        self.assertIsNone(server.dispatch_item(self.c, c, runner=sent.append)["fork_of"])
        self.assertNotIn("--fork-session", sent[-1])
        self.assertIsNone(core.fork_base(self.c, core.annotate(self.c)[b], self.dir.name, "sonnet", "claude-code"))
        self.assertIsNone(core.fork_base(self.c, core.annotate(self.c)[b], self.dir.name, None, "codex"))
        self.c.execute("UPDATE goal_bases SET used_at=?", (core.iso(core.now() - core.timedelta(hours=1)),))
        self.assertIsNone(server.dispatch_item(self.c, b, runner=sent.append)["fork_of"])

    def test_dispatch_starts_a_named_session_for_one_item(self):
        core.project_add(self.c, "shop", path=self.dir.name)
        top = core.item_add(self.c, "shop", "first", priority=0)["id"]
        x = core.item_add(self.c, "shop", "second", priority=3)["id"]
        sent = []
        t = server.dispatch_item(self.c, x, runner=sent.append)
        name = t["session_name"]
        self.assertEqual((t["item"]["id"], core._item(self.c, x)["reserved_for"]), (x, name))
        # The session and its tab get the item's name (#558): claude --name, the Remote Control name, the title.
        self.assertEqual(t["session_title"], f"#{x} second")
        self.assertIn(f"MAXPM_AGENT={name} MAXPM_FOCUS=item:{x} claude --name '#{x} second' go --remote-control "
                      f"'#{x} second'", sent[0])
        self.assertIn(f"printf '\\\\033]0;%s\\\\007' '#{x} second'; cd ", sent[0])
        b = core.go(self.c, self.dir.name, name, focus=f"item:{x}")  # its first go takes that item, not the top one
        self.assertEqual(b["item"]["id"], x)
        with self.assertRaises(RiverError):
            server.dispatch_item(self.c, x, runner=sent.append)  # held now
        core.register(self.c, "idle")
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET role='waiting', waiting_in='shop', waiting_since=? WHERE name='idle'",
                           (core.iso(core.now()),))
        r = server.dispatch_item(self.c, top, runner=sent.append)
        self.assertEqual((r["pushed_to"], len(sent)), ("idle", 1))  # a waiting session gets it; no new Terminal

    def test_launch_starts_a_reviewer_for_a_ready_release_review_and_skips_its_authors(self):
        # #902: launch --item <review> refused the review, and launch --project <deploy project> found nothing.
        core.target_add(self.c, "web", "push, then smoke test")
        core.project_add(self.c, "site", target="web", path=self.dir.name)
        core.config_set(self.c, "review", "on")
        for n in ("dev", "ops"):
            core.register(self.c, n)
        core.target_own(self.c, "web", "ops")
        a = core.item_add(self.c, "site", "page")["id"]
        core.claim(self.c, a, "dev")
        dep = core.done(self.c, a, "commit", "dev", ship_it=True)["shipped_in"]
        rv = next(i for i in core.item_show(self.c, a)["unblocks"] if core._item(self.c, i)["kind"] == "review")
        deploy_project = core.item_show(self.c, rv)["project"]
        self.assertIsNone(core._project(self.c, deploy_project)["path"])
        self.assertEqual(core.release_authors(self.c, rv), {"dev"})
        # Launch --project of the deploy project takes the review; the session opens in a folder of the release.
        t = core.launch_target(self.c, deploy_project)
        self.assertEqual((t["item"]["id"], t["project"], t["path"]), (rv, "site", core._project(self.c, "site")["path"]))
        with self.assertRaisesRegex(RiverError, "only the owner of its target"):
            core.launch_target(self.c, item=dep)  # the deploy item stays the owner's
        # The author waits for work in the folder: it does not get the review; a new session does.
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET role='waiting', waiting_in='site', waiting_since=? WHERE name='dev'",
                           (core.iso(core.now()),))
        sent = []
        t = server.dispatch_item(self.c, rv, runner=sent.append)
        self.assertNotIn("pushed_to", t)
        self.assertEqual(len(sent), 1)
        b = core.go(self.c, self.dir.name, t["session_name"], focus=f"item:{rv}")
        self.assertEqual((b["role"], b["item"]["id"]), ("reviewer", rv))
        # Another session that waits there and wrote nothing in the release gets it pushed, as a reviewer.
        core.release(self.c, rv, actor=t["session_name"])
        with core.tx(self.c):
            self.c.execute("UPDATE items SET reserved_for=NULL, reserved_until=NULL WHERE id=?", (rv,))
        core.register(self.c, "other")
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET role='waiting', waiting_in='site', waiting_since=? WHERE name='other'",
                           (core.iso(core.now()),))
        self.assertEqual(server.dispatch_item(self.c, rv, runner=sent.append)["pushed_to"], "other")
        b = core.go(self.c, self.dir.name, "other")
        self.assertEqual((b["role"], b["item"]["id"]), ("reviewer", rv))

    def _release(self, owner="ops", review="on"):
        """A target web with one project, a release of one item by dev, and its review; returns (review, deploy)."""
        core.target_add(self.c, "web", "push, then smoke test")
        core.project_add(self.c, "site", target="web", path=self.dir.name)
        core.config_set(self.c, "review", review)
        core.register(self.c, "dev")
        if owner:
            core.register(self.c, owner)
            core.target_own(self.c, "web", owner)
        a = core.item_add(self.c, "site", "page")["id"]
        core.claim(self.c, a, "dev")
        dep = core.done(self.c, a, "commit", "dev", ship_it=True)["shipped_in"]
        rv = next((i for i in core.item_show(self.c, a)["unblocks"] if core._item(self.c, i)["kind"] == "review"), None)
        return rv, dep

    def _wait(self, name, project="site"):
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET role='waiting', waiting_in=?, waiting_since=?, last_seen=? WHERE name=?",
                           (project, core.iso(core.now()), core.iso(core.now()), name))

    def _quiet(self, name, minutes):
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET last_seen=? WHERE name=?",
                           (core.iso(core.now() - core.timedelta(minutes=minutes)), name))

    def test_serve_gives_a_ready_review_to_a_waiting_agent_that_did_not_write_the_release_else_starts_one(self):
        # #966: every review needed the manager to queue or launch a reviewer.
        rv, dep = self._release()
        self._wait("dev")  # the author waits in the folder: it never gets its own release's review
        sent = []
        r = server.auto_release(self.c, runner=sent.append)
        name = r["started"][rv]
        self.assertEqual((len(sent), r["pushed"]), (1, {}))
        self.assertIn(f"MAXPM_FOCUS=item:{rv}", sent[0])
        self.assertEqual(core._item(self.c, rv)["reserved_for"], name)
        # The next passes start nothing more for it: it is reserved for the new session.
        self.assertEqual(server.auto_release(self.c, runner=sent.append)["started"], {})
        self.assertEqual(len(sent), 1)
        b = core.go(self.c, self.dir.name, name, focus=f"item:{rv}")
        self.assertEqual((b["role"], b["item"]["id"]), ("reviewer", rv))
        # Released again: another agent that waits there and wrote nothing in the release gets it pushed.
        core.release(self.c, rv, actor=name)
        with core.tx(self.c):
            self.c.execute("UPDATE items SET reserved_for=NULL, reserved_until=NULL WHERE id=?", (rv,))
            self.c.execute("DELETE FROM events WHERE item_id=? AND change LIKE 'release:%'", (rv,))
        core.register(self.c, "other")
        self._wait("other")
        r = server.auto_release(self.c, runner=sent.append)
        self.assertEqual((r["pushed"], len(sent)), ({rv: "other"}, 1))
        # auto_review off: nothing.
        core.release(self.c, core.go(self.c, self.dir.name, "other")["item"]["id"], actor="other")
        with core.tx(self.c):
            self.c.execute("UPDATE items SET reserved_for=NULL, reserved_until=NULL WHERE id=?", (rv,))
            self.c.execute("DELETE FROM events WHERE item_id=? AND change LIKE 'release:%'", (rv,))
        core.config_set(self.c, "auto_review", "off")
        self.assertEqual(server.auto_release(self.c, runner=sent.append), {"pushed": {}, "started": {}, "alerted": {}, "failed": {}})

    def test_serve_keeps_the_review_from_authors_of_items_that_the_release_commits_name(self):
        # #1236: the covered list held only shipped items; the commits up to the release pin named more, and serve
        # pushed the review to agents who wrote some of them.
        core._RELEASE_COMMITS.clear()
        self.addCleanup(core._RELEASE_COMMITS.clear)
        rv, dep = self._release()
        for n in ("writer", "busy", "fresh"):
            core.register(self.c, n)
        b = core.item_add(self.c, "site", "fix written but not shipped")["id"]
        core.claim(self.c, b, "writer")
        core.done(self.c, b, "commit 1c8bf", "writer")
        d = core.item_add(self.c, "site", "half done")["id"]
        core.claim(self.c, d, "busy")
        core.project_add(self.c, "elsewhere")
        other = core.item_add(self.c, "elsewhere", "issue with the same number")["id"]
        core.claim(self.c, other, "writer")
        core.done(self.c, other, "x", "writer")
        core.config_set(self.c, "release_commits",
                        f"printf 'Fix the page (#{b})\\n\\nPart of #{d}, issue #{other}; see #99999\\n'")
        self.assertEqual(core.release_commit_items(self.c, core._item(self.c, rv)), {b, d, other})
        self.assertEqual(core.release_authors(self.c, rv), {"dev", "writer", "busy"})
        for n in ("writer", "busy"):
            self._wait(n)
        sent = []
        r = server.auto_release(self.c, runner=sent.append)
        self.assertEqual(r["pushed"], {})  # both waiting agents wrote part of the release
        self.assertEqual(len(sent), 1)
        # The done item joins the release review and the deploy item; the open one does not hold the release back.
        waits = lambda i: {r["blocked_by"] for r in self.c.execute("SELECT blocked_by FROM deps WHERE item_id=?", (i,))}
        self.assertIn(b, waits(rv))
        self.assertIn(b, waits(dep))
        self.assertNotIn(d, waits(rv))
        self.assertNotIn(other, waits(rv), "an item of a project with another target joins nothing")
        self.assertTrue(self.c.execute("SELECT 1 FROM events WHERE item_id=? AND change LIKE '%they join the release%'",
                                       (rv,)).fetchone())
        with self.assertRaisesRegex(RiverError, "you worked on what release review"):
            core.claim(self.c, rv, "writer")
        # Another waiting agent that wrote nothing in the range gets the next one.
        with core.tx(self.c):
            self.c.execute("UPDATE items SET reserved_for=NULL, reserved_until=NULL WHERE id=?", (rv,))
            self.c.execute("DELETE FROM events WHERE item_id=? AND change LIKE 'release:%'", (rv,))
        self._wait("fresh")
        self.assertEqual(server.auto_release(self.c, runner=sent.append)["pushed"], {rv: "fresh"})

    def test_release_commits_reads_the_last_release_and_fails_quietly(self):
        core._RELEASE_COMMITS.clear()
        self.addCleanup(core._RELEASE_COMMITS.clear)
        rv, dep = self._release()
        old = core.item_add(self.c, "site", "old work")["id"]
        with core.tx(self.c):  # a done deploy of the same target: its output names the last release
            self.c.execute("INSERT INTO items(project_id,title,rank,kind,target,status,output,created_at,closed_at) "
                           "SELECT project_id,'Deploy web',rank+1,'deploy','web','done',?,created_at,? FROM items "
                           "WHERE id=?",
                           (f"Release 3 live: abc1234 (ships #{old})", core.iso(core.now()), dep))
        core.config_set(self.c, "release_commits", 'printf "%s %s" "$MAXPM_LAST_RELEASE" "$MAXPM_TARGET"')
        self.assertEqual(core.release_commit_items(self.c, core._item(self.c, rv)), {old})
        core._RELEASE_COMMITS.clear()
        core.config_set(self.c, "release_commits", f"echo '#{old}'; exit 3")
        self.assertEqual(core.release_commit_items(self.c, core._item(self.c, rv)), set(), "a failed command names nothing")
        core.config_set(self.c, "release_commits", "")
        self.assertEqual(core.release_authors(self.c, rv), {"dev"})

    def test_the_usual_release_pass_builds_no_graph(self):
        core.target_add(self.c, "web")
        core.project_add(self.c, "site", target="web", path=self.dir.name)
        core.config_set(self.c, "review", "on")
        core.ship(self.c, core.item_add(self.c, "site", "page")["id"])  # the release waits on open work
        with mock.patch.object(core, "annotate", side_effect=AssertionError("no graph")):
            self.assertEqual(server.auto_release(self.c, runner=[].append),
                             {"pushed": {}, "started": {}, "alerted": {}, "failed": {}})

    def test_serve_starts_no_release_session_past_max_sessions_and_does_not_repeat_a_failure(self):
        rv, dep = self._release()
        core.config_set(self.c, "max_sessions", "2")  # dev and ops are live
        sent = []
        r = server.auto_release(self.c, runner=sent.append)
        self.assertEqual((r["started"], r["failed"], sent), ({}, {rv: "max_sessions 2"}, []))
        hist = lambda: [e["change"] for e in core.item_show(self.c, rv)["events"] if e["change"].startswith("release:")]
        server.auto_release(self.c, runner=sent.append)
        self.assertEqual(len(hist()), 1)  # one line in the history, not one each pass
        self.assertIn("max_sessions", hist()[0])
        core.config_set(self.c, "max_sessions", "0")
        self.assertEqual(server.auto_release(self.c, runner=sent.append)["started"], {})  # within RELEASE_GAP
        with core.tx(self.c):
            self.c.execute("UPDATE events SET at=? WHERE item_id=? AND change LIKE 'release:%'",
                           (core.iso(core.now() - core.timedelta(seconds=server.RELEASE_GAP + 1)), rv))
        self.assertIn(rv, server.auto_release(self.c, runner=sent.append)["started"])

    def test_serve_alerts_the_owner_of_a_ready_deploy_once_and_starts_a_deployer_when_the_owner_cannot_take_it(self):
        rv, dep = self._release()
        core.register(self.c, "rev")
        core.claim(self.c, rv, "rev")
        core.review_pass(self.c, rv, "ok", "rev")
        self._wait("ops")  # the owner waits for work: the alert wakes it
        sent = []
        r = server.auto_release(self.c, runner=sent.append)
        self.assertEqual((r["alerted"], r["started"], sent), ({dep: "ops"}, {}, []))
        self.assertEqual(server.auto_release(self.c, runner=sent.append)["alerted"], {})  # one alert
        self.assertIn("you have messages", core.wait(self.c, self.dir.name, "ops", sleep=lambda s: None, step="1s")["why"])
        b = core.go(self.c, self.dir.name, "ops")
        self.assertEqual((b["role"], b["item"]["id"]), ("deployer", dep))
        # The next release: the owner holds nothing and ran no command for a while: idle at its prompt.
        core.done(self.c, dep, "v2", "ops")
        a = core.item_add(self.c, "site", "header")["id"]
        core.claim(self.c, a, "dev")
        core.done(self.c, a, "commit", "dev", ship_it=True)
        rv2 = next(i for i in core.item_show(self.c, a)["unblocks"] if core._item(self.c, i)["kind"] == "review")
        dep2 = next(i for i in core.item_show(self.c, a)["unblocks"] if core._item(self.c, i)["kind"] == "deploy")
        core.claim(self.c, rv2, "rev")
        core.review_pass(self.c, rv2, "ok", "rev")
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET role='idle' WHERE name='ops'")
        self._quiet("ops", 5)
        self.assertFalse(core.owner_can_deploy(self.c, "ops")[0])
        r = server.auto_release(self.c, runner=sent.append)
        name = r["started"][dep2]
        self.assertTrue(name.startswith("deploy-web-"))
        self.assertIn("MAXPM_FOCUS=deploy:web", sent[0])
        self.assertIn(f"MAXPM_AGENT={name}", sent[0])
        self.assertEqual(core.target_show(self.c, "web")["owner"], name)
        self.assertIn("gave target web", core.inbox(self.c, "ops", mark_read=False)[-1]["body"])
        # The new owner has connect_within to connect: no second session meanwhile.
        self.assertEqual(server.auto_release(self.c, runner=sent.append)["started"], {})
        b = core.go(self.c, self.dir.name, name, focus="deploy:web")
        self.assertEqual((b["role"], b["item"]["id"]), ("deployer", dep2))

    def test_a_deployer_session_waits_only_for_what_go_gives_a_deployer(self):
        # #1012: a deployer (MAXPM_FOCUS=deploy:<target>) waited while its deploy waited on the review; maxpm wait
        # said WORK for a console item, maxpm go said IDLE (the focus makes every go a deployer's), and so on.
        rv, dep = self._release()
        x = core.item_add(self.c, "site", "walk the new screen")["id"]
        nap = dict(sleep=lambda s: None, step="1s")
        core.inbox(self.c, "ops")  # the deployer read the notices about its release
        r = core.wait(self.c, self.dir.name, "ops", focus="deploy:web", **nap)
        self.assertEqual(r["result"], "again")
        self.assertEqual(core.go(self.c, self.dir.name, "ops", focus="deploy:web")["role"], "idle")
        # It waits in no project, so Dispatch and maxpm serve push no project work to it.
        core.wait(self.c, self.dir.name, "ops", focus="deploy:web", **nap)
        self.assertIsNone(core.waiting_agent_for(self.c, "site", x))
        self.assertNotIn("pushed_to", server.dispatch_item(self.c, x, runner=[].append))
        # The review passes: the deploy wakes it, and go gives it.
        core.register(self.c, "rev")
        core.claim(self.c, rv, "rev")
        core.review_pass(self.c, rv, "ok", "rev")
        self.assertEqual(core.wait(self.c, self.dir.name, "ops", focus="deploy:web", **nap)["why"],
                         f"#{dep} is ready: Deploy web")
        self.assertEqual(core.go(self.c, self.dir.name, "ops", focus="deploy:web")["item"]["id"], dep)
        # A reviewer session (review:<target>) wakes for a ready review only; with no focus, the item wakes an agent.
        core.done(self.c, dep, "v1", "ops")
        y = core.item_add(self.c, "site", "footer")["id"]
        core.claim(self.c, y, "dev")
        core.done(self.c, y, "commit", "dev", ship_it=True)
        rv2 = next(i for i in core.item_show(self.c, y)["unblocks"] if core._item(self.c, i)["kind"] == "review")
        core.register(self.c, "checker")
        self.assertEqual(core.wait(self.c, self.dir.name, "checker", focus="review:web", **nap)["why"],
                         f"#{rv2} is ready: {core._item(self.c, rv2)['title']}")
        core.claim(self.c, rv2, "rev")
        self.assertEqual(core.wait(self.c, self.dir.name, "checker", focus="review:web", **nap)["result"], "again")
        z = core.item_add(self.c, "site", "about page")["id"]
        core.register(self.c, "plain")
        self.assertEqual(core.wait(self.c, self.dir.name, "plain", **nap)["why"], f"#{z} is ready: about page")

    def test_a_target_with_no_owner_gets_a_deployer_standing_starts_it_during_the_review_and_off_starts_none(self):
        rv, dep = self._release(owner=None)
        sent = []
        r = server.auto_release(self.c, runner=sent.append)
        self.assertEqual(list(r["started"]), [rv])  # the review is not passed: launch waits for it
        self.assertEqual(core.target_show(self.c, "web")["deployer"], "launch")
        core.target_deployer(self.c, "web", "standing")
        r = server.auto_release(self.c, runner=sent.append)
        name = r["started"][dep]
        self.assertEqual(core.target_show(self.c, "web")["owner"], name)
        b = core.go(self.c, self.dir.name, name, focus="deploy:web")
        self.assertEqual(b["role"], "idle")  # it owns the target and waits for the review
        reviewer = core._item(self.c, rv)["reserved_for"]
        core.claim(self.c, rv, reviewer)
        core.review_pass(self.c, rv, "ok", reviewer)
        self._wait(name)
        self.assertEqual(server.auto_release(self.c, runner=sent.append)["alerted"], {dep: name})
        # off: nothing for the deploy item, also with no owner.
        with core.tx(self.c):
            self.c.execute("UPDATE targets SET owner=NULL, owner_expires_at=NULL")
        core.target_deployer(self.c, "web", "off")
        self.assertEqual(server.auto_release(self.c, runner=sent.append)["started"], {})
        with self.assertRaisesRegex(RiverError, "launch, standing, or off"):
            core.target_deployer(self.c, "web", "always")

    def test_a_release_that_waits_for_the_cadence_starts_no_reviewer_and_no_standing_deployer(self):
        # #1571: each ship request became a release of its own; the cadence makes them collect.
        rv, dep = self._release(owner=None)
        core.register(self.c, "dev2")
        core.claim(self.c, rv, "dev2")
        core.review_pass(self.c, rv, "ok", "dev2")
        core.target_own(self.c, "web", "dev2")
        core.claim(self.c, dep, "dev2")
        core.done(self.c, dep, "release v1", "dev2")
        core.target_release(self.c, "web", "dev2")
        core.target_cadence(self.c, "web", "2h", "dev")
        core.target_deployer(self.c, "web", "standing")
        a = core.item_add(self.c, "site", "form")["id"]
        core.claim(self.c, a, "dev")
        nxt = core.done(self.c, a, "commit", "dev", ship_it=True)
        rv2 = nxt["ship_release"]["start"]
        self.assertEqual((nxt["ship_release"]["waits"], core._item(self.c, rv2)["kind"]), (True, "review"))
        sent = []
        self.assertEqual(server.auto_release(self.c, runner=sent.append),
                         {"pushed": {}, "started": {}, "alerted": {}, "failed": {}})
        self.assertEqual(sent, [])
        # Release now from the page, with a reason: the reviewer and the standing deployer start.
        core.register(self.c, "mark", human=True)
        with self.assertRaisesRegex(RiverError, "say why"):
            server.OPS["release_now"](self.c, {"target": "web"}, "mark")
        r = server.OPS["release_now"](self.c, {"target": "web", "reason": "a fix of a production defect"}, "mark")
        self.assertEqual((r["waits"], r["early"]), (False, "a fix of a production defect"))
        started = server.auto_release(self.c, runner=sent.append)["started"]
        self.assertEqual(sorted(started), sorted([rv2, nxt["shipped_in"]]))
        self.assertEqual(server.OPS["target_cadence"](self.c, {"target": "web", "cadence": "off"}, "mark")["cadence"], "")

    def test_a_started_session_takes_its_item_or_says_why_and_one_that_never_connects_is_a_finding(self):
        core.project_add(self.c, "shop", path=self.dir.name)
        core.item_add(self.c, "shop", "first", priority=0)
        x = core.item_add(self.c, "shop", "second", priority=3)["id"]
        y = core.item_add(self.c, "shop", "third", priority=3)["id"]
        sent = []
        t = server.dispatch_item(self.c, x, runner=sent.append)
        # The push is gone (cancelled, or it ran out): the focus still takes the item.
        with core.tx(self.c):
            self.c.execute("UPDATE items SET reserved_for=NULL, reserved_until=NULL WHERE id=?", (x,))
        b = core.go(self.c, self.dir.name, t["session_name"], focus=f"item:{x}")
        self.assertEqual((b["role"], b["item"]["id"]), ("worker", x))
        t2 = server.dispatch_item(self.c, y, runner=sent.append)  # a second session, for the check below
        # Someone else holds it: go says so loudly and gives other work.
        core.register(self.c, "late")
        b = core.go(self.c, self.dir.name, "late", focus=f"item:{x}")
        self.assertIn(f"STARTED THIS SESSION FOR #{x}", b["focus_note"])
        self.assertNotEqual(b["item"]["id"], x)
        core.done(self.c, b["item"]["id"], output="ok", actor="late")
        # After the item is done, the focus that stays in the session's environment says nothing.
        core.done(self.c, x, output="ok", actor=t["session_name"])
        b = core.go(self.c, self.dir.name, t["session_name"], focus=f"item:{x}")
        self.assertNotIn("focus_note", b)
        # A session that runs no river command: a finding after connect_within, and not the project's agent.
        self.assertEqual(core.manager_findings(self.c)["not_connected"], [])
        self.assertIn("shop", core.covered_projects(self.c))
        old = core.iso(core.now() - core.parse_duration("6m"))
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET registered_at=?, last_seen=? WHERE name=?", (old, old, t2["session_name"]))
            self.c.execute("UPDATE agents SET last_seen=? WHERE name IN (?, 'late')", (old, t["session_name"]))
        (nc,) = core.manager_findings(self.c)["not_connected"]
        self.assertEqual((nc["agent"], nc["item"]["id"]), (t2["session_name"], y))
        self.assertNotIn("shop", core.covered_projects(self.c))

    def test_a_stopped_or_unconnected_session_gives_its_pushes_back(self):
        core.project_add(self.c, "shop", path=self.dir.name)
        x = core.item_add(self.c, "shop", "one")["id"]
        y = core.item_add(self.c, "shop", "two")["id"]
        sent = []
        core.register(self.c, "mark", human=True)
        a = server.dispatch_item(self.c, x, runner=sent.append)["session_name"]
        core.stop_agent(self.c, a, "wrong project", actor="mark")
        self.assertIsNone(core.item_show(self.c, x)["reserved_for"])
        self.assertNotIn("shop", core.covered_projects(self.c))
        # Never connected: the sweep takes the push back after connect_within.
        b = server.dispatch_item(self.c, y, runner=sent.append)["session_name"]
        old = core.iso(core.now() - core.parse_duration("6m"))
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET registered_at=?, last_seen=? WHERE name=?", (old, old, b))
        core.activity(self.c, "mark")
        self.assertIsNone(core.item_show(self.c, y)["reserved_for"])
        self.assertIn(f"push to {b} taken back", core.item_show(self.c, y)["events"][0]["change"])
        # By hand: maxpm push <id> --cancel; a re-pushed item leaves the LEASE RAN OUT finding.
        from river import cli
        import contextlib, io
        core.register(self.c, "w")
        core.push(self.c, y, "w", actor="mark")
        with core.tx(self.c):
            self.c.execute("UPDATE items SET needs_check=1 WHERE id=?", (y,))
        self.assertEqual(core.manager_findings(self.c)["lost_leases"], [])
        with contextlib.redirect_stdout(io.StringIO()):
            cli.run(["--as", "mark", "push", str(y), "--cancel"])
        self.assertIsNone(core.item_show(self.c, y)["reserved_for"])
        self.assertEqual([l["id"] for l in core.manager_findings(self.c)["lost_leases"]], [y])

    def test_launch_hands_an_item_reserved_for_the_manager_to_the_new_session(self):
        # #641: an item given to a manager (who takes no work) could not start, and nothing said how to free it.
        from river import cli
        import contextlib, io
        core.project_add(self.c, "shop", path=self.dir.name)
        x = core.item_add(self.c, "shop", "x")["id"]
        y = core.item_add(self.c, "shop", "y")["id"]
        for n in ("boss", "w"):
            core.register(self.c, n)
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET role='manager' WHERE name='boss'")
            self.c.execute("UPDATE items SET reserved_for='boss' WHERE id IN (?,?)", (x, y))
        sent = []
        with self.assertRaisesRegex(RiverError, f"reserved for boss\\); .*maxpm edit {x} --unreserve"):
            server.dispatch_item(self.c, x, runner=sent.append, actor="w")
        t = server.dispatch_item(self.c, x, runner=sent.append, actor="boss")
        it = core._item(self.c, x)
        self.assertEqual((it["reserved_for"], it["reserved_by"], len(sent)), (t["session_name"], "boss", 1))
        self.assertEqual(core.go(self.c, self.dir.name, t["session_name"], focus=f"item:{x}")["item"]["id"], x)
        with contextlib.redirect_stdout(io.StringIO()):
            cli.run(["--as", "boss", "edit", str(y), "--unreserve"])
        self.assertIsNone(core._item(self.c, y)["reserved_for"])
        self.assertIn("reservation for boss cancelled", core.item_show(self.c, y)["events"][0]["change"])
        core.claim(self.c, y, "w")

    def test_open_agent_on_a_person_item_or_a_waiting_item(self):
        from river import cli
        import contextlib, io
        core.project_add(self.c, "shop", path=self.dir.name)
        h = core.item_add(self.c, "shop", "sign the contract", doer="human")["id"]
        blocker = core.item_add(self.c, "shop", "draft the contract")["id"]
        waits = core.item_add(self.c, "shop", "ship it", after=[blocker])["id"]
        sent = []
        core.register(self.c, "mark", human=True)
        t = server.open_agent_on(self.c, h, runner=sent.append, person="mark")
        self.assertIn(f"MAXPM_FOCUS=help:{h}@mark claude --name 'help #{h} sign the contract' go", sent[-1])
        self.assertEqual(t["session_title"], f"help #{h} sign the contract")  # the tab title too (#562)
        b = core.go(self.c, self.dir.name, None, focus=t["focus"])
        self.assertEqual((b["role"], b["item"]), ("helper", None))
        self.assertEqual(b["help_prompt"], core.prompt_for(self.c, h, "mark"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.render_go(b)
        self.assertIn(f"Item #{h}: sign the contract", out.getvalue())
        t = server.open_needs_you(self.c, runner=sent.append, person="mark")
        self.assertIn("MAXPM_FOCUS=needs:@mark claude --name 'needs you' go", sent[-1])
        b = core.go(self.c, self.dir.name, None, focus=t["focus"])
        self.assertEqual(b["help_prompt"], core.prompt_for_all(self.c, "mark"))
        t = server.open_agent_on(self.c, waits, runner=sent.append)
        self.assertIn(f"MAXPM_FOCUS=unblock:{waits}", sent[-1])
        b = core.go(self.c, self.dir.name, None, focus=t["focus"])
        self.assertEqual((b["role"], b["item"]["id"]), ("unblocker", blocker))
        with self.assertRaises(RiverError):
            server.open_agent_on(self.c, blocker, runner=sent.append)  # held now: message the holder

    def test_deploy_now_without_an_owner_opens_a_deployer(self):
        core.target_add(self.c, "web")
        core.project_add(self.c, "site", target="web", path=self.dir.name)
        core.register(self.c, "dev")
        x = core.item_add(self.c, "site", "page")["id"]
        core.claim(self.c, x, "dev")
        core.done(self.c, x, "commit", "dev", ship_it=True)
        sent = []
        r = server.deploy_now(self.c, "web", runner=sent.append)
        self.assertIn("MAXPM_FOCUS=deploy:web claude --name 'deploy web' go --remote-control 'deploy web'", sent[-1])
        b = core.go(self.c, self.dir.name, None, focus=r["focus"])
        self.assertEqual((b["role"], b["item"]["id"]), ("deployer", r["deploy"]["id"]))

    def test_claim_next_by_a_person_opens_an_agent_that_helps(self):
        core.project_add(self.c, "shop", path=self.dir.name)
        core.register(self.c, "mark", human=True)
        x = core.item_add(self.c, "shop", "write the copy")["id"]  # anyone can do it
        core.claim(self.c, x, "mark")
        sent = []
        t = server.open_agent_on(self.c, x, runner=sent.append, person="mark")
        self.assertIn(f"MAXPM_FOCUS=help:{x}@mark claude --name 'help #{x} write the copy' go", sent[-1])
        b = core.go(self.c, self.dir.name, None, focus=t["focus"])
        self.assertEqual(b["role"], "helper")
        self.assertIn("mark took it to do themselves", b["help_prompt"])
        with self.assertRaisesRegex(RiverError, "in progress by mark"):
            server.open_agent_on(self.c, x, runner=sent.append, person="someone-else")

    def test_launch_dialog_choices_fill_the_command(self):
        core.project_add(self.c, "shop", path=self.dir.name)
        x = core.item_add(self.c, "shop", "work", models={"min_model": "opus"})["id"]
        sent = []
        t = server.launch_agent(self.c, runner=sent.append, model="opus", effort="high", launch_in="window")
        self.assertEqual(t["command"], f"claude --name '#{x} work' --model opus --effort high go --remote-control '#{x} work'")
        self.assertIn(f"MAXPM_MODEL=opus claude --name '#{x} work' --model opus --effort high go", sent[-1])
        self.assertNotIn("keystroke", sent[-1])  # launch_in window, although the setting says tab
        x = core.item_add(self.c, "shop", "more work", models={"min_model": "opus"})["id"]  # Start reserved the first
        t = server.launch_agent(self.c, runner=sent.append)
        self.assertEqual(t["command"], f"claude --name '#{x} more work' go --remote-control '#{x} more work'")  # no choice: the flags drop out
        self.assertNotIn("MAXPM_MODEL", sent[-1])
        x = core.item_add(self.c, "shop", "third", models={"min_model": "opus"})["id"]
        with self.assertRaisesRegex(RiverError, "needs at least opus"):
            server.dispatch_item(self.c, x, runner=sent.append, model="sonnet")
        # A session that waits for work gets the item only when its model is allowed.
        core.register(self.c, "w")
        core.set_agent_model(self.c, "w", "sonnet")
        self.c.execute("UPDATE agents SET role='waiting', waiting_in='shop', waiting_since='2026-01-01T00:00:00Z' WHERE name='w'")
        self.assertIsNone(core.waiting_agent_for(self.c, "shop", x))
        self.assertEqual(core.waiting_agent_for(self.c, "shop"), "w")
        core.set_agent_model(self.c, "w", "opus")
        self.assertEqual(server.dispatch_item(self.c, x, runner=sent.append)["pushed_to"], "w")
        core.cancel_push(self.c, x)
        core.unregister(self.c, "w")
        core.PLATFORM = "win32"  # tearDown restores it
        t = server.dispatch_item(self.c, x, runner=sent.append, model="fable")
        self.assertEqual(sent[-1]["env"]["MAXPM_MODEL"], "fable")
        b = core.go(self.c, self.dir.name, t["session_name"], model=sent[-1]["env"]["MAXPM_MODEL"])
        self.assertEqual((b["item"]["id"], core.agent_model(self.c, t["session_name"])), (x, "fable"))

    def test_a_deploy_opens_a_monitor_session(self):
        core.register(self.c, "ag")
        core.target_add(self.c, "web", "push")
        core.target_monitor(self.c, "web", "watch /health for 10 minutes")
        core.project_add(self.c, "site", target="web", path=self.dir.name)
        w = core.item_add(self.c, "site", "page")["id"]
        core.claim(self.c, w, "ag")
        core.done(self.c, w, "abc", "ag", ship_it=True)
        dep = core.item_show(self.c, w)["unblocks"][0]
        core.target_own(self.c, "web", "ag")
        core.claim(self.c, dep, "ag")
        (m,) = core.pending_monitors(self.c)
        with self.assertRaisesRegex(RiverError, "another queue"):
            server.open_monitors(self.c, runner=[].append, db=os.path.join(self.dir.name, "other.db"))
        sent = []
        (r,) = server.open_monitors(self.c, runner=sent.append, db=str(core.db_path()))
        self.assertEqual((r["id"], r["project"], r["model"]), (m["id"], "site", "sonnet"))
        self.assertRegex(sent[-1], rf"MAXPM_FOCUS=monitor:{m['id']} MAXPM_MODEL=sonnet claude --name 'monitor #{m['id']} [^']+' "
                                   rf"--model sonnet --effort low go")
        self.assertEqual(server.open_monitors(self.c, runner=sent.append), [])  # opened once
        self.assertEqual(len(sent), 1)
        # No server answers: the command says how to start the session by hand.
        from river import cli
        self.c.execute("DELETE FROM events WHERE change LIKE 'monitor session opened%'")
        import urllib.error
        import urllib.request
        from unittest import mock
        core.config_set(self.c, "serve_port", "1")
        with mock.patch.object(urllib.request.OpenerDirector, "open",
                               side_effect=urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))):
            got = cli.ask_server_for_monitors(self.c, timeout=1)
        self.assertIn("no maxpm serve answers on port 1", got["error"])
        self.assertIn(f"MAXPM_FOCUS=monitor:{m['id']} claude go", cli._monitor_lines(got)[0])

    def test_start_spreads_sessions_across_projects(self):
        os.makedirs(os.path.join(self.dir.name, "b"))
        core.project_add(self.c, "a", path=self.dir.name)
        core.project_add(self.c, "b", path=os.path.join(self.dir.name, "b"))
        a = [core.item_add(self.c, "a", f"a{n}", priority=0)["id"] for n in range(3)]
        b = [core.item_add(self.c, "b", f"b{n}", priority=3)["id"] for n in range(2)]
        sent = []
        self.assertEqual(core.state(self.c)["start_next"]["id"], a[0])
        t1 = server.launch_agent(self.c, runner=sent.append)
        self.assertEqual((t1["item"]["id"], t1["why"]), (a[0], "first agent for project a"))
        self.assertEqual(core.state(self.c)["start_next"]["project"], "b")
        t2 = server.launch_agent(self.c, runner=sent.append)
        self.assertEqual((t2["item"]["id"], t2["why"]), (b[0], "first agent for project b; a already has one"))
        t3 = server.launch_agent(self.c, runner=sent.append)
        self.assertEqual(t3["item"]["id"], a[1])
        self.assertIn("every project with ready work has an agent", t3["why"])
        # A project whose only sessions are gone counts as uncovered again.
        old = core.iso(core.now() - core.timedelta(days=3))
        self.c.execute("UPDATE agents SET last_seen=? WHERE name IN (?,?)", (old, t1["session_name"], t3["session_name"]))
        self.assertEqual(server.launch_agent(self.c, runner=sent.append)["item"]["id"], a[2])
        # A named project keeps its choice.
        t = server.launch_agent(self.c, "b", runner=sent.append)
        self.assertEqual((t["project"], t["why"]), ("b", None))

    def test_river_launch_from_the_command_line(self):
        import contextlib
        import io
        from unittest import mock
        from river import cli
        core.project_add(self.c, "shop", path=self.dir.name)
        x = core.item_add(self.c, "shop", "work", models={"min_model": "opus"})["id"]
        y = core.item_add(self.c, "shop", "more")["id"]
        core.register(self.c, "mark", human=True)
        sent = []
        server.TERMINAL_RUNNER = sent.append
        self.addCleanup(setattr, server, "TERMINAL_RUNNER", None)
        outside = mock.patch.object(cli, "in_sandbox", lambda: False)  # also when an agent runs the tests in one
        outside.start()
        self.addCleanup(outside.stop)

        def river(*args):
            with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.run(["-q", "--as", "mark", *args]), 0)
            return out.getvalue()
        out = river("launch", "--dry-run", "--model", "opus", "--effort", "high", "--window")
        self.assertRegex(out, rf"would start Claude Code in shop \(\S+\) for #{x} work")
        self.assertIn(f"MAXPM_MODEL=opus claude --name '#{x} work' --model opus --effort high go --remote-control "
                      f"'#{x} work'   (in a new window)", out)
        self.assertEqual(sent, [])
        with self.assertRaisesRegex(RiverError, "needs at least opus"):
            cli.dispatch(self.c, cli.build_parser().parse_args(["launch", "--model", "sonnet"]), "mark")
        out = river("launch", "--item", str(y), "--model", "sonnet")
        self.assertRegex(out, rf"started Claude Code as shop-\w+ in shop for #{y} more")
        self.assertIn(f"MAXPM_MODEL=sonnet claude --name '#{y} more' --model sonnet go", sent[-1])
        out = river("launch", "--project", "shop", "--tab")
        self.assertIn(f"for #{x} work", out)
        self.assertIn('keystroke "t"', sent[-1])

    def test_river_launch_in_a_sandbox_asks_river_serve(self):
        # A sandbox blocks Terminal and tmux for the command. maxpm serve runs outside it and opens the session.
        import base64
        import contextlib
        import io
        import urllib.error
        import urllib.request
        from unittest import mock
        from river import cli
        core.project_add(self.c, "shop", path=self.dir.name)
        x = core.item_add(self.c, "shop", "work", models={"min_model": "opus"})["id"]
        y = core.item_add(self.c, "shop", "more")["id"]
        core.register(self.c, "boss")
        sent, asked, allowed = [], [], [True]
        server.TERMINAL_RUNNER = sent.append  # what maxpm serve runs
        self.addCleanup(setattr, server, "TERMINAL_RUNNER", None)

        def serve(data):
            h = server.Handler.__new__(server.Handler)
            h.path, h.client_address, h.rfile, out = "/api/action", ("127.0.0.1", 5555), io.BytesIO(data), {}
            h.headers = {"Host": "127.0.0.1:8765", "Content-Length": str(len(data))}
            h._send = lambda code, body, ctype=None: out.update(code=code, body=json.dumps(body, default=str).encode())
            h.do_POST()
            return out["code"], out["body"]

        def fake_open(opener, req, timeout=None):
            asked.append((req.host, req.get_header("Proxy-authorization")))
            if req.host.startswith("127.0.0.1"):  # the sandbox refuses a connection to this computer
                raise urllib.error.URLError(PermissionError(1, "Operation not permitted"))
            if not allowed[0]:  # its proxy refuses a host the sandbox does not allow
                raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {"X-Proxy-Error": "blocked-by-allowlist"},
                                             io.BytesIO(b"Connection blocked by network allowlist\n"))
            code, body = serve(req.data)
            if code != 200:
                raise urllib.error.HTTPError(req.full_url, code, "Conflict", {}, io.BytesIO(body))
            return io.BytesIO(body)

        def river(*args):
            with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.run(["-q", "--as", "boss", *args]), 0)
            return out.getvalue()
        env = mock.patch.dict(os.environ, {"SANDBOX_RUNTIME": "1", "HTTP_PROXY": "http://us%40er:pw@localhost:50401"})
        env.start()
        self.addCleanup(env.stop)
        net = mock.patch.object(urllib.request.OpenerDirector, "open", fake_open)
        net.start()
        self.addCleanup(net.stop)
        out = river("launch", "--item", str(y), "--model", "sonnet", "--window")
        self.assertRegex(out, rf"started Claude Code as shop-\w+ in shop for #{y} more")
        self.assertIn("maxpm serve opened it: this session runs in a sandbox", out)
        self.assertIn(f"MAXPM_MODEL=sonnet claude --name '#{y} more' --model sonnet go", sent[-1])
        self.assertNotIn("keystroke", sent[-1])  # --window went to the server
        self.assertEqual(asked, [("127.0.0.1:8765", None),
                                 ("localhost:50401", "Basic " + base64.b64encode(b"us@er:pw").decode())])
        self.assertIn("boss: pushed to shop-", "\n".join(f"{e['actor']}: {e['change']}" for e in core.item_show(self.c, y)["events"]))
        # --prompt goes to maxpm serve with the rest; the command refuses a bad one before it asks.
        z = core.item_add(self.c, "shop", "third")["id"]
        river("launch", "--item", str(z), "--prompt", "Read 'notes.md';\nthen fix it")
        self.assertIn(f"""claude --name '#{z} third' 'go\n\nRead '"'"'notes.md'"'"';\nthen fix it' --remote-control""",
                      sent[-1].replace('\\"', '"'))  # the AppleScript string, unescaped
        del asked[:]
        with self.assertRaisesRegex(RiverError, "--prompt is empty"):
            cli.dispatch(self.c, cli.build_parser().parse_args(["launch", "--prompt", " "]), "boss")
        self.assertEqual(asked, [])
        # A dry run opens nothing, so it asks nobody.
        del asked[:]
        self.assertIn(f"would start Claude Code in shop", river("launch", "--dry-run", "--model", "opus"))
        self.assertEqual(asked, [])
        # maxpm serve refuses in its own words, and it serves one queue only.
        launch = cli.build_parser().parse_args(["launch", "--model", "sonnet"])
        with self.assertRaisesRegex(RiverError, "needs at least opus"):
            cli.dispatch(self.c, launch, "boss")
        code, body = serve(json.dumps({"op": "launch_agent", "args": {"db": os.path.join(self.dir.name, "o.db")}}).encode())
        self.assertEqual((code, "another queue" in json.loads(body)["error"]), (409, True))
        # The sandbox decides: with the host not allowed, or with no proxy, the command says what to allow.
        allowed[0] = False
        with self.assertRaisesRegex(RiverError, r"refused the connection to maxpm serve on 127.0.0.1:8765 "
                                                r"\(Connection blocked by network allowlist\)\. Allow the host"):
            cli.dispatch(self.c, launch, "boss")
        del os.environ["HTTP_PROXY"]
        os.environ.pop("http_proxy", None)
        with self.assertRaisesRegex(RiverError, "Allow the host 127.0.0.1:8765 for this command, or run it outside"):
            cli.dispatch(self.c, launch, "boss")
        self.assertEqual(len(sent), 2)
        self.assertEqual(core.item_show(self.c, x)["status"], "open")

    def test_manager_section_start_chat_queue_and_stop(self):
        core.project_add(self.c, "shop", path=self.dir.name)
        x = core.item_add(self.c, "shop", "work")["id"]
        core.register(self.c, "mark", human=True)
        core.register(self.c, "w1")
        self.assertIsNone(core.state(self.c)["manager"])
        sent = []
        t = server.start_manager(self.c, runner=sent.append, model="opus")
        # The manager compacts at manager_autocompact (200k), in the same session (#1312).
        self.assertEqual(t["command"], "claude --name 'maxpm manager' --model opus --autocompact 200000 manage "
                                       "--remote-control 'maxpm manager'")
        self.assertEqual(t["autocompact"], 200000)
        self.assertIn(f"MAXPM_AGENT={t['session_name']} MAXPM_MODEL=opus claude --name 'maxpm manager' --model opus "
                      f"--autocompact 200000 manage", sent[-1])
        with self.assertRaisesRegex(RiverError, "is the active manager"):
            server.start_manager(self.c, runner=sent.append)
        self.assertEqual(server.manage_command('codex "run maxpm go in this folder"'), 'codex "run maxpm manage in this folder"')
        # A launch command from before the name maxpm runs river, the same command.
        self.assertEqual(server.manage_command('codex "run river go in this folder"'), 'codex "run river manage in this folder"')
        m = t["session_name"]
        core.queue_add(self.c, "w1", x, actor=m)
        st = core.state(self.c)
        self.assertEqual((st["manager"]["name"], st["manager"]["actions"][0]["change"][:9]), (m, "queued fo"))
        self.assertEqual(st["queues"]["w1"][0]["item"], x)
        # The page's ops: queue, stop, open chat.
        OPS = server.OPS
        OPS["queue_remove"](self.c, {"agent": "w1", "ref": str(x)}, "mark")
        OPS["queue_add"](self.c, {"agent": "w1", "message": "commit first"}, "mark")
        r = OPS["stop_agent"](self.c, {"agent": "w1", "reason": ""}, "mark")  # the page needs no reason
        self.assertIn("ends", r)
        self.assertEqual(core.stop_request(self.c, "w1")["stop_reason"], "stopped from the page by mark")
        with self.assertRaisesRegex(RiverError, "say why"):
            core.stop_agent(self.c, "w1", " ", actor="m")  # maxpm stop still needs --reason
        self.assertEqual(OPS["open_chat"](self.c, {"agent": "w1"}, "mark")["hint"][:24], "No chat to open for w1: ")
        core.record_session_url(self.c, "w1", "https://claude.ai/code/session_x")
        self.assertEqual(OPS["open_chat"](self.c, {"agent": "w1"}, "mark")["url"], "https://claude.ai/code/session_x")

    def test_launch_options_follow_each_agents_platform(self):
        old = server._login_shell_which
        server._login_shell_which = lambda names: {n: "/bin/" + n for n in names}
        self.addCleanup(setattr, server, "_login_shell_which", old)
        server.setup_agent_add(self.c, "Codex")
        core.config_set(self.c, "launch_agents", core.setting(self.c, "launch_agents") + "; Mine=myagent go")
        opts = {o["label"]: o for o in core.state(self.c)["launch_options"]}
        self.assertEqual((opts["Claude Code"]["family"], [m["name"] for m in opts["Claude Code"]["models"]]),
                         ("claude", ["haiku", "sonnet", "opus", "fable"]))
        self.assertEqual(opts["Claude Code"]["efforts"], ["low", "medium", "high", "xhigh", "max"])
        self.assertEqual((opts["Codex"]["family"], [m["name"] for m in opts["Codex"]["models"]]),
                         ("openai", ["luna", "terra", "sol", "astra"]))
        self.assertTrue(opts["Codex"]["takes_model"] and opts["Codex"]["takes_effort"])
        self.assertTrue(all(m["note"] for o in ("Claude Code", "Codex") for m in opts[o]["models"]))
        self.assertEqual((opts["Mine"]["family"], len(opts["Mine"]["models"]), opts["Mine"]["takes_model"]), (None, 8, False))
        self.assertEqual([o["name"] for o in opts["Claude Code"]["options"]], ["remote_control", "permission_mode", "skill_prompt"])
        self.assertEqual([o["name"] for o in opts["Codex"]["options"]], ["sandbox", "approval"])
        self.assertEqual(opts["Mine"]["options"], [])
        c = core.fill_launch_command('codex -m {model} -c model_reasoning_effort={effort} "go"', "sol", "xhigh")
        self.assertEqual(c, 'codex -m sol -c model_reasoning_effort=xhigh "go"')
        self.assertEqual(core.fill_launch_command('codex -m {model} -c model_reasoning_effort={effort} "go"', None, None),
                         'codex "go"')
        self.assertEqual(core.fill_launch_command("x --v --effort={effort} go", None, None), "x --v go")
        # {name}: the session's name, quoted by river; with no name the flag before it drops out (#562).
        self.assertEqual(core.fill_launch_command("x --name {name} go", None, None, "#7 fix it"), "x --name '#7 fix it' go")
        self.assertEqual(core.fill_launch_command("x --name {name} go", None, None), "x go")
        self.assertEqual(core.focus_title(self.c, "needs:@mark"), "needs you")
        self.assertEqual(core.focus_title(self.c, "review:web"), "review web")

    def test_windows_opens_a_console_window_with_the_env_set(self):
        core.project_add(self.c, "shop", path=self.dir.name)
        x = core.item_add(self.c, "shop", "work")["id"]
        core.PLATFORM = "win32"  # tearDown restores it
        sent = []
        t = server.dispatch_item(self.c, x, runner=sent.append)
        # cmd reads double quotes, not the single quotes of a POSIX shell.
        self.assertEqual(t["command"], f'claude --name "#{x} work" go --remote-control "#{x} work"')
        self.assertEqual(sent[0], {"args": f"cmd /k title #{x} work & {t['command']}", "cwd": t["path"],
                                   "env": {"MAXPM_DB": str(core.db_path()), "MAXPM_AGENT": t["session_name"],
                                           "MAXPM_FOCUS": f"item:{x}"}})
        core.PLATFORM = "linux"
        core.item_add(self.c, "shop", "more work")
        with self.assertRaisesRegex(RiverError, "macOS and Windows"):
            server.launch_agent(self.c)

    def test_opens_terminal_in_the_folder_of_the_top_ready_item(self):
        folder = os.path.join(self.dir.name, 'my "shop" app')
        core.project_add(self.c, "shop", path=self.dir.name)
        core.project_path(self.c, "shop", folder, move=True)
        core.project_add(self.c, "nofolder")
        core.item_add(self.c, "shop", "person step", doer="human")
        x = core.item_add(self.c, "shop", "agent step", priority=1)["id"]
        for n in range(6):  # each Start reserves one item for the session it opens
            core.item_add(self.c, "shop", f"more work {n}")
        sent = []
        t = server.launch_agent(self.c, runner=sent.append)
        self.assertEqual((t["project"], t["item"]["id"], t["command"]), ("shop", x, f"claude --name '#{x} agent step' go --remote-control '#{x} agent step'"))
        self.assertIn('tell application "Terminal"', sent[0])
        self.assertIn('keystroke "t" using command down', sent[0])  # a new tab by default
        core.config_set(self.c, "launch_in", "window")
        server.launch_agent(self.c, runner=sent.append)
        self.assertNotIn("keystroke", sent[-1])
        core.config_set(self.c, "launch_in", "tab")
        self.assertIn(t["command"], sent[0])
        self.assertIn('my \\"shop\\" app', sent[0])  # quotes escaped inside the AppleScript string
        core.config_set(self.c, "launch_agents", 'Claude Code=claude go; Codex=codex "run maxpm go and follow it"')
        t = server.launch_agent(self.c, runner=sent.append, agent="Codex")
        self.assertEqual((t["agent"], t["command"]), ("Codex", 'codex "run maxpm go and follow it"'))
        self.assertIn('codex \\"run maxpm go and follow it\\"', sent[-1])
        self.assertEqual(server.launch_agent(self.c, runner=sent.append)["agent"], "Claude Code")  # first is default
        self.assertEqual(core.state(self.c)["launch_agents"], ["Claude Code", "Codex"])
        with self.assertRaises(RiverError):
            server.launch_agent(self.c, runner=sent.append, agent="Nope")
        with self.assertRaises(RiverError):
            core.config_set(self.c, "launch_agents", "just a command")
        core.item_add(self.c, "nofolder", "x")
        with self.assertRaises(RiverError):
            server.launch_agent(self.c, "nofolder", runner=sent.append)  # no folder to start in
        with self.assertRaises(RiverError):
            server.launch_agent(self.c, "nosuch", runner=sent.append)

    def test_new_tab_waits_for_terminal_in_front_and_for_the_new_tab(self):
        core.project_add(self.c, "shop", path=self.dir.name)
        core.item_add(self.c, "shop", "agent step")
        sent = []
        server.launch_agent(self.c, runner=sent.append)
        s = sent[0]
        # Command-T goes to the app in front: the script waits for Terminal first, not a fixed delay.
        self.assertLess(s.index('frontmost of process "Terminal"'), s.index('keystroke "t"'))
        self.assertNotIn("delay 0.5", s)
        # The command runs only after the tab count grew; otherwise it falls back to a new window.
        self.assertLess(s.index("set {windowsBefore, tabsBefore}"), s.index('keystroke "t"'))
        # Terminal lists each tab of a tabbed window as its own window, so a new window counts as the new tab.
        self.assertLess(s.index("(count of windows) > windowsBefore or"), s.index("in selected tab of front window"))
        self.assertIn('error "Terminal opened no new tab"', s)
        self.assertEqual(s.count("do script"), 3)  # the tab, the fallback window, and the no-window case


class FakeTmux:
    """Enough of tmux for river's commands: panes in windows of one session, with the options river sets."""

    def __init__(self):
        self.session, self.panes, self.windows, self.calls, self.n = False, [], {}, [], 0
        self.full = False  # True: no space for one more pane in a window

    def end(self, pane):
        """The agent CLI of this pane ended a long time after its start: a shell is all that runs there."""
        pane["running"], pane["started"] = "zsh", "1"

    def _pane(self, window=None, name=None):
        if window is None:
            window = f"@{self.n}"
            self.windows[window] = {"name": name, "tile": ""}
        pane = {"id": f"%{self.n}", "window": window, "name": "", "agent": "", "started": "", "running": "claude",
                "keys": []}
        self.n += 1
        self.panes.append(pane)
        return pane["id"]

    def __call__(self, a):
        self.calls.append(a)
        if ";" in a:  # several commands in one: each one's output, in order; nothing when one fails
            parts, part = [], []
            for x in [*a, ";"]:
                if x == ";":
                    parts.append(self._one(part))
                    part = []
                else:
                    part.append(x)
            return None if None in parts else "\n".join(parts)
        return self._one(a)

    def _one(self, a):
        cmd, opt = a[0], lambda flag: a[a.index(flag) + 1] if flag in a else None
        pane = next((p for p in self.panes if p["id"] in (opt("-t"), opt("-s"))), None)
        if cmd == "has-session":
            return "" if self.session and self.panes else None
        if cmd == "new-session":
            self.session = True
            return self._pane(name=opt("-n"))
        if cmd == "new-window":
            return self._pane(name=opt("-n"))
        if cmd == "split-window":
            return None if self.full else self._pane(window=opt("-t"))
        if cmd == "set-option" and "-p" in a:
            pane[{"@maxpm_name": "name", "@maxpm_agent": "agent", "@maxpm_started": "started"}[a[-2]]] = a[-1]
        elif cmd == "set-option" and "-w" in a and "@maxpm_tile" in a:
            self.windows[opt("-t")]["tile"] = "" if "-u" in a else "1"
        elif cmd == "send-keys":
            pane["keys"].append(a[-1])
        elif cmd == "rename-window":
            self.windows[opt("-t")]["name"] = a[-1]
        elif cmd == "join-pane":
            if self.full:
                return None
            pane["window"] = opt("-t")
        elif cmd == "break-pane":
            pane["window"] = f"@{self.n}"
            self.windows[pane["window"]] = {"name": opt("-n"), "tile": ""}
            self.n += 1
        elif cmd == "kill-pane":
            self.panes.remove(pane)
        elif cmd == "list-panes":
            return "\n".join("|".join([p["id"], p["window"], self.windows[p["window"]]["tile"],
                                       str(sum(q["window"] == p["window"] for q in self.panes)), p["running"],
                                       "/dev/ttys00" + p["id"][1:], p["agent"], p.get("started", ""), p["name"]])
                             for p in self.panes)
        elif cmd == "show-options":
            return "C-b"
        elif cmd == "display-message":  # terminal_screen: the pane's size and cursor; else the text as it is
            return f"120|40|3|7|{pane['running']}|{pane['name']}" if pane else a[-1]
        elif cmd == "capture-pane":  # what the pane shows: with -e the colours too
            if not pane:
                return None
            return pane.get("screen", "") if "-e" in a else re.sub(r"\x1b\[[0-9;]*m", "", pane.get("screen", ""))
        return ""


class LaunchInTmux(unittest.TestCase):
    """launch_in tmux: each agent is a pane of the tmux session maxpm, and maxpm view shows them side by side."""

    def setUp(self):
        self.platform, core.PLATFORM = core.PLATFORM, "linux"  # no Terminal app: tmux needs none
        self.dir = tempfile.TemporaryDirectory()
        os.environ["MAXPM_DB"] = os.path.join(self.dir.name, "t.db")
        self.c = core.connect()
        core.project_add(self.c, "shop", path=self.dir.name)
        self.tmux = server.TMUX_RUNNER = FakeTmux()
        self.in_tmux = os.environ.pop("TMUX", None)  # the tests can run in a tmux pane (an agent river started there)

    def tearDown(self):
        if self.in_tmux is not None:
            os.environ["TMUX"] = self.in_tmux
        core.PLATFORM, server.TMUX_RUNNER = self.platform, None
        server.TIDY.update(at=None, screens={})
        server.BUSY.clear()
        self.c.close()
        os.environ.pop("MAXPM_DB", None)
        self.dir.cleanup()

    def test_each_agent_starts_in_a_pane_of_the_maxpm_session(self):
        x = core.item_add(self.c, "shop", "first: the work")["id"]
        y = core.item_add(self.c, "shop", "second")["id"]
        with self.assertRaisesRegex(RiverError, "works on macOS and Windows only"):
            server.launch_agent(self.c)  # the setting says tab, and Linux has no Terminal app
        with self.assertRaisesRegex(RiverError, "launch_in is auto, tab, window, or tmux"):
            core.config_set(self.c, "launch_in", "screen")
        core.config_set(self.c, "launch_in", "tmux")
        t = server.launch_agent(self.c)
        first = self.tmux.panes[0]
        self.assertEqual((t["launch_in"], t["tmux_pane"]), ("tmux", "%0"))
        # The first agent makes the session; its window and its pane carry the session's name and the agent's.
        self.assertEqual(self.tmux.calls[1][:6], ["new-session", "-d", "-s", "maxpm", "-n", f"#{x} first: the work"])
        self.assertEqual((first["name"], first["agent"]), (f"#{x} first: the work", t["session_name"]))
        # river types the command line into the pane's shell: the folder, the session's variables, the command.
        self.assertEqual(first["keys"][1], "Enter")
        self.assertIn(f"MAXPM_AGENT={t['session_name']} MAXPM_FOCUS=item:{x} {t['command']}", first["keys"][0])
        self.assertTrue(first["keys"][0].startswith("cd "))
        # The next agent is a window of the same session, opened in the background (-d): no window takes the keyboard.
        t = server.dispatch_item(self.c, y, launch_in="tmux")
        self.assertEqual([c for c in self.tmux.calls if c[0] == "new-window"][0][:6],
                         ["new-window", "-d", "-t", "=maxpm:", "-n", f"#{y} second"])
        self.assertEqual(self.tmux.panes[1]["window"], "@1")
        # A session for another purpose has no river name yet: its pane has the purpose only.
        core.register(self.c, "mark", human=True)
        z = core.item_add(self.c, "shop", "sign the form", doer="human")["id"]
        t = server.open_agent_on(self.c, z, person="mark")
        self.assertEqual((self.tmux.panes[2]["name"], self.tmux.panes[2]["agent"]), (f"help #{z} sign the form", ""))
        # One launch can still pick a Terminal tab, which this system does not have.
        core.item_add(self.c, "shop", "third")
        with self.assertRaisesRegex(RiverError, "works on macOS and Windows only"):
            server.launch_agent(self.c, launch_in="tab")

    def test_maxpm_view_puts_the_agents_side_by_side_and_back(self):
        core.config_set(self.c, "launch_in", "tmux")
        with self.assertRaisesRegex(RiverError, "no agent runs in tmux"):
            server.tmux_view("tile")
        for n in range(3):
            core.item_add(self.c, "shop", f"work {n}")
            server.launch_agent(self.c)
        self.tmux.panes.append({"id": "%9", "window": "@1", "name": "", "agent": "", "running": "vim", "keys": []})  # a person's own pane
        v = server.tmux_view(None)
        self.assertEqual([p["pane"] for p in v["panes"]], ["%0", "%1", "%2"])  # only the panes river made
        self.assertFalse(any(c[0] in ("join-pane", "break-pane") for c in self.tmux.calls))  # --list changes nothing
        v = server.tmux_view("tile")
        self.assertEqual({p["window"] for p in v["panes"]}, {"@0"})
        self.assertEqual((self.tmux.windows["@0"], v["left"]), ({"name": "agents", "tile": "1"}, []))
        self.assertIn(["set-option", "-w", "-t", "@0", "pane-border-format", " #{@maxpm_name} "], self.tmux.calls)
        self.assertIn(["select-window", "-t", "@0"], self.tmux.calls[-3:])
        self.assertEqual(v["show"][1:4], ["attach-session", "-t", "=maxpm"])
        self.assertIn("Ctrl-b then: an arrow", v["show"][-1])
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-501/default,1,0"}):  # run from a tmux pane: no attach inside tmux
            self.assertEqual(server.tmux_view(None)["show"][1:4], ["switch-client", "-t", "=maxpm"])
        # While the agents are side by side, a new agent joins them; with no space left it gets its own window.
        core.item_add(self.c, "shop", "work 3")
        server.launch_agent(self.c)
        self.assertEqual(self.tmux.panes[-1]["window"], "@0")
        self.tmux.full = True
        core.item_add(self.c, "shop", "work 4")
        server.launch_agent(self.c)
        self.assertNotEqual(self.tmux.panes[-1]["window"], "@0")
        self.assertEqual(server.tmux_view("tile")["left"], [self.tmux.panes[-1]["name"]])
        # --tidy closes a pane whose agent ended (a shell is all that runs there); --windows separates the others.
        ended = self.tmux.panes[1]["name"]
        self.tmux.end(self.tmux.panes[1])
        v = server.tmux_view("windows", tidy=True)
        self.assertEqual(([c["name"] for c in v["closed"]], ended in [p["name"] for p in v["panes"]]), ([ended], False))
        self.assertEqual(len({p["window"] for p in v["panes"]}), len(v["panes"]))
        self.assertEqual(self.tmux.windows["@0"], {"name": v["panes"][0]["name"], "tile": ""})
        self.assertEqual(next(p for p in self.tmux.panes if p["id"] == "%9")["window"], "@1")  # the person's pane stays where it was

    def test_the_command_line_and_the_page(self):
        import contextlib
        import io
        from unittest import mock
        from river import cli
        core.register(self.c, "mark", human=True)
        x = core.item_add(self.c, "shop", "work")["id"]

        def river(*args):
            with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.run(["-q", "--as", "mark", *args]), 0)
            return out.getvalue()
        outside = mock.patch.object(cli, "in_sandbox", lambda: False)  # also when an agent runs the tests in one
        outside.start()
        self.addCleanup(outside.stop)
        self.assertIn("(in a new tmux pane)", river("launch", "--dry-run", "--tmux"))
        out = river("launch", "--tmux")
        self.assertRegex(out, rf"started Claude Code as shop-\w+ in shop for #{x} work \(.*\); tmux pane %0: maxpm view shows it")
        # A command with no terminal (an agent, a test) prints the panes and attaches nothing.
        out = river("view")
        self.assertRegex(out, rf"%0  #{x} work  \[shop-\w+\]\n")
        self.assertIn("A person sees the agents with: maxpm view", out)
        self.assertNotIn("A person sees", river("view", "--list"))
        # A new pane shows a shell before the agent CLI runs: for START_GRACE that is a start, not an end.
        self.tmux.panes[0]["running"] = "zsh"
        self.assertNotIn("its agent ended", river("view", "--list"))
        self.assertNotIn("closed", river("view", "--tidy"))
        self.tmux.end(self.tmux.panes[0])
        self.assertIn("(its agent ended: maxpm view --tidy closes it)", river("view", "--list"))
        self.assertIn(f"closed (its agent ended): #{x} work", river("view", "--tidy"))
        # Open chat for an agent in a pane, with no web link, names maxpm view.
        self.assertIs(core.state(self.c)["tmux"], False)  # the tests see no tmux (tests/__init__.py)
        with mock.patch.object(core, "tmux_path", lambda: "/opt/homebrew/bin/tmux"):
            self.assertIs(core.state(self.c)["tmux"], True)
        self.tmux.panes.append({"id": "%5", "window": "@0", "name": "#9 other", "agent": "w1", "running": "claude", "keys": []})
        self.tmux.windows["@0"] = {"name": "w", "tile": ""}
        self.assertEqual(server._tmux_pane_of("/dev/ttys005")["name"], "#9 other")
        self.assertIsNone(server._tmux_pane_of("/dev/ttys001"))

    def test_tidy_closes_the_pane_of_a_session_that_is_done_while_its_cli_stays_open(self):
        import contextlib
        import io
        from river import cli
        core.config_set(self.c, "launch_in", "tmux")
        core.register(self.c, "mark", human=True)
        ids = [core.item_add(self.c, "shop", f"work {n}")["id"] for n in range(7)]
        works, left, asks, stopped, gone, again, other = [server.launch_agent(self.c, "shop")["session_name"] for _ in ids]
        panes = {name: p for name, p in zip((works, left, asks, stopped, gone, again, other), self.tmux.panes)}
        z = core.item_add(self.c, "shop", "sign the form", doer="human")["id"]
        server.open_agent_on(self.c, z, person="mark")  # a pane with a purpose and no agent name
        self.tmux.panes.append({"id": "%9", "window": "@1", "name": "", "agent": "", "running": "vim", "keys": []})  # a person's own
        done = lambda: {p["name"]: p["done"] for p in server.tmux_view(None, conn=self.c)["panes"] if p["done"]}
        # Every agent CLI runs (no shell shows), and every session is in the queue: nothing to close.
        self.assertEqual((done(), server.tmux_view(None, tidy=True, conn=self.c)["closed"]), ({}, []))
        # The river work of a session ended and its CLI stays open and idle: maxpm wait ended it (unregistered).
        core.unregister(self.c, left)
        self.assertEqual(done(), {panes[left]["name"]: f"{left} is not in the queue any more"})
        self.assertEqual(server.tmux_view(None)["panes"][1]["done"], None)  # without the queue only a shell counts
        # A pane that shows a prompt stays: a person may want to answer it.
        core.unregister(self.c, asks)
        panes[asks]["screen"] = "\x1b[1mBash command\x1b[0m\n  git push\n\nDo you want to proceed?\n\x1b[34m❯ 1. Yes\x1b[0m\n  2. No"
        self.assertNotIn(panes[asks]["name"], done())
        # A stop is a request: the pane is done after the agent's last go, not while it still commits. An agent
        # that never read the stop (an idle CLI: no river command for away_after) is done too.
        core.stop_agent(self.c, stopped, "the plan changed", "mark")
        self.assertNotIn(panes[stopped]["name"], done())
        seen = self.c.execute("SELECT last_seen FROM agents WHERE name=?", (stopped,)).fetchone()[0]
        late = core.iso(core.now() - 2 * core.parse_duration(core.setting(self.c, "away_after")))
        self.c.execute("UPDATE agents SET last_seen=? WHERE name=?", (late, stopped))
        self.assertEqual(done()[panes[stopped]["name"]], f"{stopped} was asked to stop and is idle")
        self.c.execute("UPDATE agents SET last_seen=? WHERE name=?", (seen, stopped))
        self.assertEqual(core.go(self.c, self.dir.name, stopped)["ended"], True)
        self.assertEqual(done()[panes[stopped]["name"]], f"{stopped} stopped")
        # A session that is gone and holds nothing is done; one that still holds an item is not.
        core.claim(self.c, ids[0], works)
        old = core.iso(core.now() - core.timedelta(days=2))
        self.c.execute("UPDATE agents SET last_seen=? WHERE name IN (?, ?)", (old, works, gone))
        self.assertEqual((core.agent_status(self.c, works)["state"], done().get(panes[gone]["name"])), ("gone", f"{gone} is gone"))
        self.assertNotIn(panes[works]["name"], done())
        # The session began again in its pane under another name: that agent's process runs there, so the pane stays.
        core.unregister(self.c, again)
        core.register(self.c, "shop-new")
        self.c.execute("UPDATE agents SET pid=?, host=? WHERE name='shop-new'", (os.getpid(), core.this_host()))  # a live process
        server.PS_RUNNER = lambda pids: f" {os.getpid()} ttys00{panes[again]['id'][1:]}\n"
        self.addCleanup(setattr, server, "PS_RUNNER", None)
        self.assertNotIn(panes[again]["name"], done())
        want = {panes[n]["name"] for n in (left, stopped, gone)}
        self.assertEqual(set(done()), want)

        def river(*args):
            with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.run(["-q", "--as", "mark", *args]), 0)
            return out.getvalue()
        out = river("view", "--list")
        self.assertIn(f"[{left}]  ({left} is not in the queue any more: maxpm view --tidy closes it)", out)
        self.assertEqual(out.count("maxpm view --tidy closes it"), 3)
        # The page lists the same panes, and its button closes them: one tmux command reads the panes that may be done.
        self.assertEqual({p["name"] for p in server.tmux_done(self.c)}, want)
        self.assertEqual(sum(c[0] == "display-message" for c in self.tmux.calls[-2:]), 1)
        self.assertIn("3 done (maxpm view --tidy closes them) | Ctrl-b", server.tmux_view("tile", conn=self.c)["show"][-1])
        closed = server.OPS["tmux_tidy"](self.c, {}, "mark")["closed"]
        self.assertEqual(({c["name"] for c in closed}, closed[0]["why"]), (want, f"{left} is not in the queue any more"))
        self.assertEqual({p["id"] for p in self.tmux.panes}, {panes[n]["id"] for n in (works, asks, again, other)} | {"%7", "%9"})
        self.assertEqual((server.tmux_done(self.c), server.tmux_view(None, tidy=True, conn=self.c)["closed"]), ([], []))
        # The person answered the prompt and the CLI ended: the shell that is left is done too, as before.
        self.tmux.end(panes[asks])
        self.assertIn(f"closed (its agent ended): {panes[asks]['name']}", river("view", "--list", "--tidy"))
        # With no pattern for a prompt, the screen is not read.
        core.config_set(self.c, "prompt_pattern", "")
        core.unregister(self.c, other)
        calls = len(self.tmux.calls)
        self.assertEqual([p["name"] for p in server.tmux_done(self.c)], [panes[other]["name"]])
        self.assertFalse(any(c[0] == "display-message" for c in self.tmux.calls[calls:]))

    def test_river_serve_tidies_every_tidy_every_and_never_a_busy_agent(self):
        import threading
        from unittest import mock
        from river import notify
        core.config_set(self.c, "launch_in", "tmux")
        clock = [core.now()]
        fake_now = mock.patch.object(core, "now", lambda: clock[0])
        fake_now.start()
        self.addCleanup(fake_now.stop)

        def tick(seconds):
            clock[0] += core.timedelta(seconds=seconds)
            return [c["name"] for c in server.auto_tidy(self.c)]
        # No agent runs in tmux yet: nothing to close, and the pass waits tidy_every for the next look.
        self.assertEqual(tick(0), [])
        calls = len(self.tmux.calls)
        self.assertEqual(tick(60), [])
        self.assertEqual(self.tmux.calls[calls:], [])  # not due: no tmux command at all
        for n in range(4):
            core.item_add(self.c, "shop", f"work {n}")
        ended, idle, busy, works = [server.launch_agent(self.c, "shop")["session_name"] for _ in range(4)]
        panes = {name: p for name, p in zip((ended, idle, busy, works), self.tmux.panes)}
        for name, p in panes.items():
            p["screen"] = f"{name} at its prompt"
        panes[ended]["running"] = "zsh"  # the agent CLI ended: a shell is all that is left
        core.unregister(self.c, idle)  # the CLI stays open, and the agent left the queue
        core.unregister(self.c, busy)
        # Due after tidy_every (20m): the pane with a shell closes at once; the open CLIs wait for idle_after (2m).
        self.assertEqual(tick(18 * 60), [])
        self.assertEqual(tick(60), [panes[ended]["name"]])
        for seconds in (40, 40):
            panes[busy]["screen"] += "\n... working"  # its screen changes: a busy agent stays
            self.assertEqual(tick(seconds), [])
        self.assertEqual(tick(40), [panes[idle]["name"]])  # the same screen for 2m
        panes[busy]["screen"] += "\n... done"
        self.assertEqual((tick(60), tick(60)), ([], []))
        self.assertEqual(tick(60), [panes[busy]["name"]])
        self.assertEqual([p["agent"] for p in self.tmux.panes], [works])  # a session in the queue stays
        # After a pass with nothing left to wait for, the next one comes after tidy_every.
        core.unregister(self.c, works)
        self.assertEqual(tick(60), [])
        calls = len(self.tmux.calls)
        self.assertEqual((tick(18 * 60), self.tmux.calls[calls:]), ([], []))
        self.assertEqual((tick(60), tick(120)), ([], [panes[works]["name"]]))
        # 0s: never; and the setting is a duration.
        core.item_add(self.c, "shop", "work 4")
        server.launch_agent(self.c, "shop")
        last = self.tmux.panes[-1]
        last["running"] = "zsh"
        core.config_set(self.c, "tidy_every", "0s")
        self.assertEqual(tick(3600), [])
        with self.assertRaisesRegex(RiverError, "bad duration"):
            core.config_set(self.c, "tidy_every", "often")
        core.config_set(self.c, "tidy_every", "5m")
        self.assertEqual(tick(0), [last["name"]])
        # The manager's briefing names it, and the loop of maxpm serve runs the pass every time.
        self.assertEqual(core.manage(self.c, self.dir.name)["tidy_every"], "5m")
        stop, looks = threading.Event(), []
        with mock.patch.object(server, "watch_prompts", lambda c: None), \
                mock.patch.object(server, "fresh_sessions", lambda c: None), \
                mock.patch.object(server, "auto_tidy", lambda c: (looks.append(1), stop.set())):
            notify.loop(stop, interval_s=1)
        self.assertEqual(looks, [1])

    def test_a_busy_agent_keeps_its_lease_and_its_item(self):
        from unittest import mock
        core.config_set(self.c, "launch_in", "tmux")
        core.register(self.c, "mark", human=True)
        core.register(self.c, "other")
        x = core.item_add(self.c, "shop", "run every check")["id"]
        name = server.launch_agent(self.c)["session_name"]
        pane, clock = self.tmux.panes[0], [core.now()]
        self.addCleanup(server.IDLE.clear)
        fake_now = mock.patch.object(core, "now", lambda: clock[0])
        fake_now.start()
        self.addCleanup(fake_now.stop)
        procs = {1000: (1, "9:00:00")}  # the agent CLI
        core.PROC_RUNNER = lambda: "".join(f"{pid} {ppid} {age}\n" for pid, (ppid, age) in procs.items())
        self.addCleanup(setattr, core, "PROC_RUNNER", None)
        self.c.execute("UPDATE agents SET pid=1000, host=? WHERE name=?", (core.this_host(), name))

        def tick(seconds=30):
            clock[0] += core.timedelta(seconds=seconds)
            return server.watch_busy(self.c)
        held = lambda: (core.activity(self.c, "other"), core.item_show(self.c, x))[1]["assignee"]
        # With no agent at work the pass runs no tmux command.
        calls = len(self.tmux.calls)
        self.assertEqual((tick(), self.tmux.calls[calls:]), ([], []))
        clock[0] += core.timedelta(seconds=1)
        core.activity(self.c, name)
        core.claim(self.c, x, name)
        # 35 minutes of work with no river command: the screen changes, so each pass renews the lease (30m).
        pane["screen"] = "✻ Working… (0s)"
        self.assertEqual(tick(), [])  # the first look: nothing to compare with
        for n in range(1, 70):
            pane["screen"] = f"✻ Working… ({n * 30}s)"
            self.assertEqual(tick(), [name])
        self.assertEqual(held(), name)
        # It waits on a prompt for a person: its work is in the folder, and it keeps the item.
        pane["screen"] = "Bash command\n  git push\nDo you want to proceed?\n❯ 1. Yes\n  2. No"
        self.assertEqual(([tick(600) for _ in range(4)], held()), ([[name]] * 4, name))
        # A long test run: the screen stays the same for 40 minutes, and a process runs below the agent CLI. The
        # lease holds; and a note for the agent starts no fresh session, although its screen looks idle.
        pane["screen"] = "⏺ Bash(make check-all)\n  ⎿  Running…"
        procs[3000] = (1000, "00:05")
        note = core.send(self.c, "note", "one more thing", to=name, item=x, actor="mark")["id"]
        nothing = {"started": {}, "ended": {}, "failed": {}}
        for _ in range(40):
            self.assertEqual((tick(60), server.fresh_sessions(self.c)), ([name], nothing))
        self.assertEqual(held(), name)
        # The test run ended and the agent stopped at its prompt: now the note goes to a fresh session, and the
        # idle agent ends before the new session starts.
        del procs[3000]
        order = []
        end_idle, start_fresh = core.end_idle, server.start_fresh
        with mock.patch.object(core, "end_idle", lambda *a, **k: (order.append("end"), end_idle(*a, **k))[1]), \
                mock.patch.object(server, "start_fresh", lambda *a, **k: (order.append("start"), start_fresh(*a, **k))[1]):
            runs = [(tick(), server.fresh_sessions(self.c))[1] for _ in range(6)]
        (r,) = [r for r in runs if r["started"]]
        self.assertEqual((list(r["started"]), list(r["ended"]), order), ([x], [name], ["end", "start"]))
        self.assertIn(f"[note #{note} from mark to {name}] one more thing", core.item_show(self.c, x)["notes"])
        # A quiet screen and no command: the lease runs out as before. busy_max 0s: river looks at nothing.
        fresh = r["started"][x]
        clock[0] += core.timedelta(seconds=1)
        core.activity(self.c, fresh)
        core.claim(self.c, x, fresh)
        self.tmux.panes[-1]["screen"] = "⏺ Done.\n\n│ >  │"
        self.assertEqual(([tick(600) for _ in range(4)], held()), ([[]] * 4, None))
        core.claim(self.c, x, fresh)
        core.config_set(self.c, "busy_max", "0s")
        calls = len(self.tmux.calls)
        self.assertEqual((tick(), self.tmux.calls[calls:]), ([], []))
        # The loop of maxpm serve runs the pass every time.
        import threading
        from river import notify
        stop, looks = threading.Event(), []
        with mock.patch.object(server, "watch_prompts", lambda c: None), \
                mock.patch.object(server, "fresh_sessions", lambda c: None), \
                mock.patch.object(server, "auto_tidy", lambda c: None), \
                mock.patch.object(server, "watch_busy", lambda c: (looks.append(1), stop.set())):
            notify.loop(stop, interval_s=1)
        self.assertEqual(looks, [1])

    def test_tmux_is_not_installed(self):
        server.TMUX_RUNNER = None
        cmd, server.TMUX_CMD = server.TMUX_CMD, ["tmux-that-is-not-installed"]
        self.addCleanup(setattr, server, "TMUX_CMD", cmd)
        core.item_add(self.c, "shop", "work")
        with self.assertRaisesRegex(RiverError, "tmux is not installed: brew install tmux .* maxpm config set launch_in tab"):
            server.launch_agent(self.c, launch_in="tmux")
        self.assertEqual(core.next_item(self.c, "shop")[0]["reserved_for"], None)  # nothing was reserved for a session that never opened
        self.assertIsNone(server._tmux_pane_of("/dev/ttys001"))

    def test_an_agent_session_keeps_its_own_variables_out_of_the_panes(self):
        old = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(old)))
        os.environ.update({"MAXPM_AGENT": "manager-1", "MAXPM_FOCUS": "item:1", "CLAUDECODE": "1",
                           "CLAUDE_CODE_SESSION_ID": "s", "CODEX_THREAD_ID": "t", "CLAUDE_CONFIG_DIR": "/c", "CODEX_HOME": "/h"})
        env = server._tmux_env()
        self.assertEqual({k for k in env if k.startswith(("MAXPM_", "CLAUDE", "CODEX_"))}, {"CLAUDE_CONFIG_DIR", "CODEX_HOME"})
        self.assertEqual(env["PATH"], os.environ["PATH"])

    def test_the_page_shows_an_agents_terminal_and_types_into_it(self):
        core.config_set(self.c, "launch_in", "tmux")
        core.register(self.c, "mark", human=True)
        x = core.item_add(self.c, "shop", "work")["id"]
        t = server.launch_agent(self.c)
        name, pane = t["session_name"], self.tmux.panes[0]
        pane["screen"] = "\x1b[1mDo you trust this folder?\x1b[0m\n> 1. Yes"
        # The session ran no river command yet (it waits on a prompt): the pane that got its name is its terminal.
        self.assertEqual(server.agent_terminals(self.c), {name: "%0"})
        got = server.terminal_screen(self.c, name)
        self.assertEqual((got["pane"], got["width"], got["height"], got["cursor"], got["ended"], got["name"]),
                         ("%0", 120, 40, [3, 7], False, f"#{x} work"))
        self.assertEqual(got["text"], pane["screen"])
        # A person answers the prompt from the page: text as it is, and keys by name.
        pane["keys"].clear()
        r = server.OPS["terminal_keys"](self.c, {"agent": name, "keys": [{"text": "1"}, {"key": "Enter"}, {"text": "-l; rm"}]}, "mark")
        self.assertEqual((r["sent"], pane["keys"]), (3, ["1", "Enter", "-l; rm"]))
        self.assertEqual(self.tmux.calls[-1], ["send-keys", "-t", "%0", "-l", "--", "-l; rm"])  # typed, never read as a key name
        # Nothing goes when one entry is not a key river sends; an agent may not type into another agent's terminal.
        for bad in ([{"key": "Enter"}, {"key": "kill-server"}], [{"text": ""}], [{"text": "x" * 4001}], "Enter", [{"key": "C-c"}] * 201):
            with self.assertRaisesRegex(RiverError, "not a key MaximizePM sends|a list of at most 200"):
                server.terminal_keys(self.c, name, bad, "mark")
        core.register(self.c, "other-agent")
        with self.assertRaisesRegex(RiverError, "only a person types into an agent's terminal"):
            server.terminal_keys(self.c, name, [{"key": "Enter"}], "other-agent")
        self.assertEqual(pane["keys"], ["1", "Enter", "-l; rm"])
        # An agent that runs in no pane, and a pane of no agent, have no terminal on the page.
        with self.assertRaisesRegex(RiverError, "other-agent does not run in a tmux pane"):
            server.terminal_screen(self.c, "other-agent")
        with self.assertRaisesRegex(RiverError, "does not run in a tmux pane"):
            server.terminal_keys(self.c, "other-agent", [{"key": "Enter"}], "mark")
        # A session the person started in tmux by hand: its process's terminal device names the pane.
        self.tmux.panes.append({"id": "%7", "window": "@0", "name": "", "agent": "", "running": "claude", "keys": []})
        self.c.execute("UPDATE agents SET pid=4242, host=? WHERE name='other-agent'", (core.this_host(),))
        server.PS_RUNNER = lambda pids: "".join(f" {p} ttys007\n" for p in pids)
        self.addCleanup(setattr, server, "PS_RUNNER", None)
        self.assertEqual(server.agent_terminals(self.c), {name: "%0", "other-agent": "%7"})
        self.assertEqual(server.open_chat(self.c, name).get("hint", "")[:16], "No chat to open ")  # no process recorded for it
        # The page's state lists them, and without tmux there are none.
        server.TMUX_RUNNER = lambda a: ""
        self.assertEqual(server.agent_terminals(self.c), {})

    def test_an_agents_terminal_answers_this_computer_only(self):
        core.config_set(self.c, "launch_in", "tmux")
        core.item_add(self.c, "shop", "work")
        name = server.launch_agent(self.c)["session_name"]

        def call(method, path, headers, body=None, ip="127.0.0.1"):
            import io
            from email.message import Message
            h = server.Handler.__new__(server.Handler)
            h.path, h.client_address, h.headers, out = path, (ip, 5555), Message(), {}
            raw = json.dumps(body).encode() if body is not None else b""
            for k, v in {"Content-Length": str(len(raw)), **headers}.items():
                h.headers[k] = v
            h.rfile = io.BytesIO(raw)
            h._send = lambda code, body, ctype=None: out.update(code=code, body=body)
            getattr(h, "do_" + method)()
            return out["code"], out["body"]
        local = {"Host": "127.0.0.1:8765"}
        keys = {"op": "terminal_keys", "args": {"agent": name, "keys": [{"key": "Enter"}]}}
        self.assertEqual(call("GET", f"/api/terminal?agent={name}", local)[0], 200)
        state = call("GET", "/api/state", local)[1]
        self.assertEqual((state["terminals"], state["tmux_done"]), ({name: "%0"}, []))  # the agent is in the queue: its pane stays
        self.assertEqual(call("POST", "/api/action", {**local, "Origin": "http://127.0.0.1:8765"}, keys)[1]["result"]["sent"], 1)
        self.assertEqual(call("GET", "/api/terminal?agent=nobody", local)[0], 409)
        sent = len(self.tmux.panes[0]["keys"])
        # A page of another site whose name points at this computer (DNS rebinding), a tunnel or proxy, another computer.
        for headers, ip in (({"Host": "evil.example:8765", "Origin": "http://evil.example:8765"}, "127.0.0.1"),
                            ({**local, "Origin": "http://evil.example"}, "127.0.0.1"),
                            ({**local, "X-Forwarded-For": "203.0.113.9"}, "127.0.0.1"),
                            (local, "192.168.1.20")):
            self.assertEqual(call("GET", f"/api/terminal?agent={name}", headers, ip=ip)[0], 403, headers)
            code, body = call("POST", "/api/action", headers, keys, ip=ip)
            self.assertEqual(code, 403, headers)
        self.assertIn("only this computer may call maxpm serve", body["error"])
        self.assertEqual(len(self.tmux.panes[0]["keys"]), sent)

    def test_a_prompt_in_an_agents_terminal_reaches_the_person(self):
        import threading
        from unittest import mock
        from river import notify
        core.config_set(self.c, "launch_in", "tmux")
        core.config_set(self.c, "notify_channels", "log")
        core.register(self.c, "mark", human=True)
        core.item_add(self.c, "shop", "work")
        name = server.launch_agent(self.c)["session_name"]
        pane, clock = self.tmux.panes[0], [core.now()]
        self.addCleanup(server.PROMPTS.clear)
        fake_now = mock.patch.object(core, "now", lambda: clock[0])
        fake_now.start()
        self.addCleanup(fake_now.stop)

        def watch(after=30):
            clock[0] += core.timedelta(seconds=after)
            return server.watch_prompts(self.c)
        events = lambda: [(e["summary"], e["from_agent"]) for e in core.needs_you(self.c, "mark")]
        ask = "\x1b[1mBash command\x1b[0m\n  rm -rf build\n\nDo you want to proceed?\n\x1b[34m❯ 1. Yes\x1b[0m\n  2. No"
        # An agent that works, then one that asks: the person hears of it when the prompt stays (prompt_wait).
        pane["screen"] = "⏺ Read 3 files\n✻ Pondering… (12s)"
        self.assertEqual(watch(), {"waiting": {}, "told": [], "closed": []})
        pane["screen"] = ask
        self.assertEqual(watch(), {"waiting": {name: "Do you want to proceed?"}, "told": [], "closed": []})
        self.assertEqual(watch(5)["told"], [])
        self.assertEqual(watch()["told"], [name])
        self.assertEqual(events(), [(f"alert from {name}: waits on a prompt in its terminal: Do you want to proceed?", name)])
        self.assertEqual(watch()["told"], [])  # once for a prompt
        # The notification opens the agent's Terminal on the page; its web session when it has one (a phone).
        (row,) = core.outbox(self.c)
        self.assertEqual(notify.compose(self.c, [row])[2], f"http://127.0.0.1:8765/#terminal-{name}")
        self.c.execute("UPDATE agents SET session_url='https://claude.ai/code/session_1' WHERE name=?", (name,))
        self.assertEqual(notify.compose(self.c, [row])[2], "https://claude.ai/code/session_1")
        # maxpm serve starts again while the prompt waits: no second alert.
        server.PROMPTS.clear()
        self.assertEqual((watch()["told"], watch()["told"], len(events())), ([], [], 1))
        # The person only marks it read: nothing more for the same prompt. A new prompt is a new alert.
        server.OPS["message_read"](self.c, {"msg": row["message_id"]}, "mark")
        self.assertEqual((watch()["told"], watch()["told"], events()), ([], [], []))
        pane["screen"] = ask.replace("Do you want to proceed?", "Do you want to make this edit to a.py?")
        self.assertEqual((watch()["told"], watch()["told"]), ([], [name]))
        self.assertIn("make this edit to a.py?", events()[0][0])
        # The person answers in the Terminal: the screen moves on, and river closes the alert.
        pane["screen"] = "⏺ Edited a.py"
        (mid,) = watch()["closed"]
        self.assertEqual((events(), core.message_show(self.c, mid)["state"], server.PROMPTS), ([], "read", {}))
        # Not a prompt: the words further up the screen, and the screen an ended agent left behind.
        pane["screen"] = "Do you want to proceed?\n" + "\n".join(f"line {n}" for n in range(server.PROMPT_LINES))
        self.assertEqual(watch()["waiting"], {})
        pane["screen"], pane["running"] = ask, "zsh"
        self.assertEqual((watch()["waiting"], watch()["told"]), ({}, []))
        # The words are a setting: another CLI's prompt needs no code change; empty turns the watch off.
        pane["screen"], pane["running"] = "Allow the command? [y/N]", "codex"
        self.assertEqual(watch()["waiting"], {})
        core.config_set(self.c, "prompt_pattern", core.DEFAULT_SETTINGS["prompt_pattern"] + r"|\[y/N\]")
        core.config_set(self.c, "prompt_wait", "1m")
        self.assertEqual((watch()["told"], watch()["told"], watch()["told"]), ([], [], [name]))
        core.config_set(self.c, "prompt_pattern", "")
        self.assertEqual((len(watch()["closed"]), events()), (1, []))
        with self.assertRaisesRegex(RiverError, "regular expression"):
            core.config_set(self.c, "prompt_pattern", "(")
        # The loop of maxpm serve looks at the panes on every pass, with or without a notification channel.
        stop, looks = threading.Event(), []
        with mock.patch.object(server, "watch_prompts", lambda c: (looks.append(1), stop.set())):
            notify.loop(stop, interval_s=1)
        self.assertEqual(looks, [1])

    def test_tmux_is_the_default_when_tmux_is_installed(self):
        from unittest import mock
        core.register(self.c, "mark", human=True)
        core.item_add(self.c, "shop", "first")
        core.item_add(self.c, "shop", "second")
        self.assertEqual(core.setting(self.c, "launch_in"), "auto")
        # No tmux on this computer: a Terminal tab, as before (and Linux has no Terminal app).
        self.assertEqual((core.state(self.c)["launch_in"], server.setup_status(self.c)["launch_in"]), ("tab", "tab"))
        with self.assertRaisesRegex(RiverError, "works on macOS and Windows only"):
            server.launch_agent(self.c)
        # tmux is installed: every start opens a tmux pane, also the fresh sessions of maxpm serve.
        with mock.patch.object(core, "tmux_path", lambda: "/opt/homebrew/bin/tmux"):
            self.assertEqual((core.state(self.c)["launch_in"], server.setup_status(self.c)["launch_in"]), ("tmux", "tmux"))
            t = server.launch_agent(self.c)
            self.assertEqual((t["launch_in"], t["tmux_pane"]), ("tmux", "%0"))
            core.unregister(self.c, t["session_name"])
            self.assertEqual(server.start_fresh(self.c, t["item"]["id"], "an answer came for it")["launch_in"], "tmux")
            self.assertEqual(len(self.tmux.panes), 2)
            # The setting of a person wins over auto, and auto can be set again.
            core.config_set(self.c, "launch_in", "tab")
            self.assertEqual(core.state(self.c)["launch_in"], "tab")
            core.config_set(self.c, "launch_in", "auto")
            self.assertEqual(core.state(self.c)["launch_in"], "tmux")
            # One start can still choose.
            self.assertEqual(core.launch_target(self.c, launch_in="window")["launch_in"], "window")

    def _deploy_ready(self):
        """Target web with project site; dev finished one item, so deploy item is ready. Returns its id."""
        core.config_set(self.c, "launch_in", "tmux")
        core.target_add(self.c, "web", "push, then smoke test")
        core.project_add(self.c, "site", target="web", path=self.dir.name)
        core.register(self.c, "mark", human=True)
        core.register(self.c, "dev")
        a = core.item_add(self.c, "site", "page")["id"]
        core.claim(self.c, a, "dev")
        return core.done(self.c, a, "commit", "dev", ship_it=True)["shipped_in"]

    def test_a_deployer_that_waits_on_a_person_keeps_its_item_and_target_and_serve_starts_no_other(self):
        # #1010: deployers asked mark for the production go in their pane (the auto mode classifier refused
        # make prod-release). They looked idle: serve took the deploy back on a note, gave the target to a new
        # deployer, and ended the one that waited, which closed its pane and the question in it.
        dep = self._deploy_ready()
        clock = [core.now()]
        fake_now = mock.patch.object(core, "now", lambda: clock[0])
        fake_now.start()
        self.addCleanup(fake_now.stop)
        self.addCleanup(server.IDLE.clear)
        name = server.auto_release(self.c)["started"][dep]
        pane = self.tmux.panes[-1]
        # Each deployer has a session name of its own: two starts beside a live one of the same name never connected.
        self.assertEqual(pane["name"], f"deploy web {name[-4:]}")
        self.assertIn(f"claude --name 'deploy web {name[-4:]}' ", pane["keys"][0])
        core.register(self.c, "boss")
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET role='manager' WHERE name='boss'")
        clock[0] += core.timedelta(seconds=1)
        b = core.go(self.c, self.dir.name, name, focus="deploy:web")
        self.assertEqual((b["role"], b["item"]["id"]), ("deployer", dep))
        from river import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.render_go(b)
        self.assertIn(f"ask <person> \"<what to approve>\" --item {dep}", out.getvalue())
        q = core.send(self.c, "question", "make prod-release is ready: reply 'deploy' in my terminal", to="mark",
                      item=dep, actor=name)["id"]
        self.assertEqual(core.waits_on_person(self.c), {name: q})
        clock[0] += core.timedelta(seconds=1)
        core.send(self.c, "note", "send me a note when the next pin is written", to=name, item=dep, actor="dev")
        pane["screen"] = "⏺ The release needs your approval: reply 'deploy'.\n\n╭────╮\n│ >  │\n╰────╯"
        nothing = {"started": {}, "ended": {}, "failed": {}}
        for _ in range(60):  # five hours at its prompt: past the lease, busy_max (4h), idle_end, and owner checks
            clock[0] += core.timedelta(seconds=300)
            core.activity(self.c, "boss")
            server.watch_busy(self.c)
            self.assertEqual(server.fresh_sessions(self.c), nothing)
            self.assertEqual(server.auto_release(self.c)["started"], {})
        it = core.item_show(self.c, dep)
        self.assertEqual((it["status"], it["assignee"], core.target_show(self.c, "web")["owner"]), ("in_progress", name, name))
        self.assertIn(pane, self.tmux.panes)
        told = [m for m in core.inbox(self.c, "boss", mark_read=False) if m["reply_to"] == q]
        self.assertEqual(len(told), 1)  # one alert for the question, not one each pass
        self.assertIn(f"{name} waits at its prompt for mark's answer to question #{q} (#{dep})", told[0]["body"])
        # mark answers in the terminal and the deploy is done: the question no longer holds anything.
        clock[0] += core.timedelta(seconds=1)
        core.done(self.c, dep, "release 7", name)
        self.assertEqual(core.waits_on_person(self.c), {})
        # A question about an item the agent does not hold, or to another agent, is no wait on a person.
        x = core.item_add(self.c, "site", "other")["id"]
        core.send(self.c, "question", "which header?", to="mark", item=x, actor=name)
        core.send(self.c, "question", "which header?", to="dev", item=x, actor=name)
        self.assertEqual(core.waits_on_person(self.c), {})

    def test_serve_keeps_the_last_lines_of_a_session_that_ended_before_it_connected(self):
        dep = self._deploy_ready()
        name = server.auto_release(self.c)["started"][dep]
        pane = self.tmux.panes[-1]
        pane["running"], pane["screen"] = "zsh", "$ claude --name 'deploy web'\nError: could not start\n$"
        # The tidy pass comes right after auto_release in the same pass of the loop, and a new pane shows a shell
        # until the agent CLI runs: the pass leaves the pane for START_GRACE (#1011: it closed bde9 and 98b5).
        self.assertEqual(server.auto_tidy(self.c), [])
        self.assertEqual(len(self.tmux.panes), 1)
        self.assertFalse(any("ended before it ran" in e["change"] for e in core.item_show(self.c, dep)["events"]))
        server.TIDY["at"] = None  # the next pass that is due
        late = core.now() + core.timedelta(seconds=server.START_GRACE)
        with mock.patch.object(core, "now", lambda: late):
            self.assertEqual([p["pane"] for p in server.auto_tidy(self.c)], [pane["id"]])
        last = core.item_show(self.c, dep)["events"]
        self.assertTrue(any(e["change"].startswith(f"{name} ended before it ran a maxpm command; the last lines of its "
                                                   f"pane {pane['id']}: ") and "Error: could not start" in e["change"]
                            for e in last))

    def test_a_session_that_waits_for_its_own_background_work_keeps_its_item_when_news_comes(self):
        # #1611: four sessions waited for a test run or a subagent; a message came, and serve gave their item to
        # a fresh session and closed their pane.
        from tests.test_core import Transcript
        sid = "55555555-5555-4555-8555-555555555555"
        core.config_set(self.c, "launch_in", "tmux")
        core.register(self.c, "mark", human=True)
        item = core.item_add(self.c, "shop", "rename the field")["id"]
        name = server.launch_agent(self.c)["session_name"]
        pane, clock = self.tmux.panes[0], [core.now()]
        root = tempfile.mkdtemp(dir=self.dir.name)
        for cleanup in (server.IDLE.clear, server.BUSY.clear, server.BACKGROUND.clear, core._BG_CACHE.clear):
            self.addCleanup(cleanup)
        for patch in (mock.patch.object(core, "now", lambda: clock[0]),
                      mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": root})):
            patch.start()
            self.addCleanup(patch.stop)

        def tick(after=30):
            clock[0] += core.timedelta(seconds=after)
            return server.fresh_sessions(self.c)
        nothing = {"started": {}, "ended": {}, "failed": {}}
        # The session takes the item, starts a long test run in the background, and ends its turn.
        clock[0] += core.timedelta(seconds=1)
        core.activity(self.c, name)
        core.claim(self.c, item, name)
        core.record_claude_session(self.c, name, sid)
        tr = Transcript(root, sid)
        use = tr.background(clock[0], "b1", "cargo test -p server", "Run the server tests")
        pane["screen"] = "⏺ I wait for the test run to end.\n\n╭────╮\n│ >  │\n╰────╯\n  ? for shortcuts"
        clock[0] += core.timedelta(seconds=1)
        core.send(self.c, "note", "main moved: rebase before you push", to=name, actor="mark")
        # News for it, a quiet screen, no river command: it keeps its item, and its pane stays.
        self.assertEqual([tick() for _ in range(12)], [nothing] * 12)
        it = core.item_show(self.c, item)
        self.assertEqual((it["assignee"], pane in self.tmux.panes), (name, True))
        told = [e["change"] for e in it["events"] if "waits for its own background work" in e["change"]]
        self.assertEqual(len(told), 1)  # once for the task, not in each pass
        self.assertIn("command Run the server tests", told[0])
        # Its lease does not run out while the test run goes on (lease_ttl 30m).
        for _ in range(8):
            clock[0] += core.timedelta(minutes=5)
            self.assertEqual(server.watch_busy(self.c), [name])
            core.activity(self.c, "mark")  # any river command runs the sweep
        self.assertEqual(core.item_show(self.c, item)["assignee"], name)
        # The run ends and the harness wakes the session. If it then stays at its prompt with the news, the item
        # goes to a fresh session, as before.
        tr.ended(clock[0], "b1", use)
        self.assertEqual(server.watch_busy(self.c), [])
        r = [tick() for _ in range(6)]
        started = [x["started"] for x in r if x["started"]]
        self.assertEqual(([list(x) for x in started], pane in self.tmux.panes), ([[item]], False))
        self.assertIn("main moved: rebase before you push", core.item_show(self.c, item)["notes"])

    def test_work_for_an_agent_idle_at_its_prompt_goes_to_a_fresh_session(self):
        import threading
        from unittest import mock
        from river import notify
        core.config_set(self.c, "launch_in", "tmux")
        core.register(self.c, "mark", human=True)
        first = core.item_add(self.c, "shop", "first")["id"]
        name = server.launch_agent(self.c)["session_name"]
        pane, clock = self.tmux.panes[0], [core.now()]
        self.addCleanup(server.IDLE.clear)
        fake_now = mock.patch.object(core, "now", lambda: clock[0])
        fake_now.start()
        self.addCleanup(fake_now.stop)
        idle = "⏺ Done: the item is finished and pushed.\n\n╭────╮\n│ >  │\n╰────╯\n  ? for shortcuts"
        pane["screen"] = idle
        sent = lambda p: [k for k in p["keys"][2:]]  # the first two are the command line river typed at the launch
        agents = lambda: {r["name"] for r in self.c.execute("SELECT name FROM agents WHERE kind='ai'")}

        def tick(after=30):
            clock[0] += core.timedelta(seconds=after)
            return server.fresh_sessions(self.c)

        def command(agent):  # the agent runs a river command
            clock[0] += core.timedelta(seconds=1)
            core.activity(self.c, agent)
        nothing = {"started": {}, "ended": {}, "failed": {}}
        # A session that never ran a river command is not idle at its prompt: the manager finds it (not connected).
        self.assertEqual([tick(300) for _ in range(4)], [nothing] * 4)
        # It connects, finishes its item, and stops at its prompt with nothing in hand. Within idle_end (15m) it
        # stays, and with no news river runs no tmux command at all.
        command(name)
        core.claim(self.c, first, name)
        core.done(self.c, first, "ok", actor=name)
        calls = len(self.tmux.calls)
        self.assertEqual([tick(60) for _ in range(5)], [nothing] * 5)
        self.assertEqual(len(self.tmux.calls), calls)
        # Work for it: river does not type into its pane. When the screen stays the same for idle_after (2m), river
        # takes the push back, starts a fresh session for the item, and ends the idle agent: its pane closes.
        second = core.item_add(self.c, "shop", "second", models={"model": "opus", "effort": "high"})["id"]
        clock[0] += core.timedelta(seconds=1)
        core.push(self.c, second, name, "take this one", "mark")
        self.assertEqual([tick()["started"] for _ in range(4)], [{}] * 4)
        r = tick()
        (fresh,) = r["started"].values()
        self.assertEqual((list(r["started"]), r["ended"]), ([second], {name: "idle at its prompt; fresh sessions took its work"}))
        self.assertEqual((sent(pane), pane in self.tmux.panes, name in agents()), ([], False, False))
        new = self.tmux.panes[-1]
        self.assertIn(f"MAXPM_AGENT={fresh} MAXPM_FOCUS=item:{second} ", new["keys"][0])
        self.assertIn("--model opus --effort high", new["keys"][0])  # the item's model and effort
        self.assertEqual(core.item_show(self.c, second)["reserved_for"], fresh)
        # A working screen, and a prompt that a person answers, are not idle; a shell is not the agent CLI.
        command(fresh)
        core.claim(self.c, second, fresh)
        q = core.send(self.c, "question", "export now or later?", to="mark", item=second, actor=fresh)["id"]
        clock[0] += core.timedelta(seconds=1)
        core.answer(self.c, q, "now", actor="mark")
        for screen in ("✻ Working… (3s)", "✻ Working… (33s)", "✻ Working… (63s)", "✻ Working… (93s)", "✻ Working… (123s)"):
            new["screen"] = screen
            self.assertEqual(tick(), nothing)
        new["screen"] = "Bash command\n  rm -rf build\nDo you want to proceed?\n❯ 1. Yes\n  2. No"
        self.assertEqual([tick() for _ in range(6)], [nothing] * 6)
        new["screen"], new["running"] = idle, "zsh"
        self.assertEqual([tick() for _ in range(6)], [nothing] * 6)
        # At its prompt, with the answer for its item: the item goes to a fresh session with the answer in its notes,
        # and the next session checks first what the idle one left in the folder.
        new["running"] = "claude"
        r = [tick() for _ in range(5)][-1]
        (third,) = r["started"].values()
        self.assertEqual((list(r["started"]), list(r["ended"]), sent(new)), ([second], [fresh], []))
        it = core.item_show(self.c, second)
        self.assertIn(f"[answer #{q + 1} from mark to {fresh}] now", it["notes"])
        self.assertIn("Check git status", it["notes"])
        self.assertEqual((it["needs_check"], it["reserved_for"]), (1, third))
        # Idle with nothing in hand for idle_end: the agent ends, and the sender of what it never read hears it.
        command(third)
        clock[0] += core.timedelta(seconds=1)
        note = core.send(self.c, "note", "fyi", to=third, actor="mark")["id"]
        command(third)
        self.tmux.panes[-1]["screen"] = idle
        r = [tick(60) for _ in range(18)]  # idle_end, then idle_after with the same screen
        self.assertEqual([x["ended"] for x in r if x["ended"]], [{third: "idle at its prompt with nothing in hand for 15m"}])
        self.assertNotIn(third, agents())
        told = self.c.execute("SELECT body FROM messages WHERE to_agent='mark' AND reply_to=?", (note,)).fetchone()[0]
        self.assertIn(f"{third} ended (idle at its prompt with nothing in hand for 15m) before it read your note #{note}", told)
        # A manager or a planner waits at its prompt for a person or a watch: never ended.
        core.register(self.c, "boss")
        self.c.execute("UPDATE agents SET role='manager', last_seen=? WHERE name='boss'", (core.iso(clock[0] + core.timedelta(seconds=1)),))
        self.assertEqual([tick(300) for _ in range(6)], [nothing] * 6)
        # The settings: durations, and on or off.
        for key, bad in (("idle_after", "soon"), ("idle_end", "soon"), ("fresh_sessions", "maybe")):
            with self.assertRaisesRegex(RiverError, "bad duration|on or off"):
                core.config_set(self.c, key, bad)
        # The loop of maxpm serve runs the pass every time.
        stop, looks = threading.Event(), []
        with mock.patch.object(server, "watch_prompts", lambda c: None), \
                mock.patch.object(server, "fresh_sessions", lambda c: (looks.append(1), stop.set())):
            notify.loop(stop, interval_s=1)
        self.assertEqual(looks, [1])

    def test_an_answer_for_an_agent_that_ended_starts_a_fresh_session(self):
        from unittest import mock
        core.config_set(self.c, "launch_in", "tmux")
        core.register(self.c, "mark", human=True)
        core.register(self.c, "ann")
        x = core.item_add(self.c, "shop", "export the report")["id"]
        y = core.item_add(self.c, "shop", "send the invoice")["id"]
        plain = core.item_add(self.c, "shop", "planned after the person's step")["id"]
        # ann needs the person: it files a person's item, releases its item, and ends.
        core.claim(self.c, x, "ann")
        h = core.item_add(self.c, "shop", "Pick the format", doer="human", blocks=x, mode="release", actor="ann")["id"]
        core.item_add(self.c, "shop", "a step first", doer="human", blocks=plain, actor="mark")
        core.claim(self.c, y, "ann")
        q = core.send(self.c, "question", "which customer?", to="mark", item=y, actor="ann")["id"]
        core.release(self.c, y, actor="ann")
        core.unregister(self.c, "ann")
        # The person answers both: each item starts in a fresh session, with the answer in its notes.
        core.done(self.c, h, "CSV", actor="mark")
        core.answer(self.c, q, "ACME", actor="mark")
        # A session that waits for work does not take them: maxpm serve starts them within a pass or two.
        core.register(self.c, "cy")
        ids = lambda: [a["id"] for a in core.next_item(self.c, "shop", actor="cy", limit=9)]
        self.assertFalse({x, y} & set(ids()))
        with mock.patch.object(core, "now", lambda: core.datetime.now(core.timezone.utc) + core.FRESH_GRACE * 2):
            self.assertTrue({x, y} <= set(ids()))  # with no maxpm serve, after FRESH_GRACE go takes them
        started = server.fresh_sessions(self.c)["started"]
        self.assertEqual(sorted(started), [x, y])
        self.assertIn(f"[#{h} Pick the format: done by mark] CSV", core.item_show(self.c, x)["notes"])
        self.assertIn("[question", core.item_show(self.c, y)["notes"])
        self.assertIn("[answer from mark] ACME", core.item_show(self.c, y)["notes"])
        self.assertEqual({p["agent"] for p in self.tmux.panes}, set(started.values()))
        # An item no agent worked on yet waits for go as before; each item starts once.
        self.assertEqual(server.fresh_sessions(self.c)["started"], {})
        # fresh_sessions off: the answer goes in the notes, and whoever runs go next takes the item.
        core.config_set(self.c, "fresh_sessions", "off")
        core.register(self.c, "bo")
        z = core.item_add(self.c, "shop", "third")["id"]
        core.claim(self.c, z, "bo")
        h2 = core.item_add(self.c, "shop", "Approve", doer="human", blocks=z, mode="release", actor="bo")["id"]
        core.done(self.c, h2, "yes", actor="mark")
        self.assertEqual((server.fresh_sessions(self.c)["started"], core.item_show(self.c, z)["ready"]), ({}, True))
        self.assertIn(z, ids())
        self.assertIn("done by mark] yes", core.item_show(self.c, z)["notes"])

    def test_with_a_real_tmux_server(self):
        """The same commands against tmux itself, on a server of its own (never the user's)."""
        import shutil
        import time
        if not shutil.which("tmux"):
            self.skipTest("tmux is not installed")
        server.TMUX_RUNNER = None
        # The server's socket gets a folder of its own: tearDown deletes self.dir before the cleanups run, and a
        # server whose socket is gone keeps running, out of reach of kill-server (#946). The cleanups run last
        # first: kill-server, then TMUX_CMD back, then the folder. They run also when the test fails.
        sock = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, sock, True)
        conf = Path(sock, "tmux.conf")
        conf.write_text("set -g default-shell /bin/sh\n")  # a shell that starts at once, and no profile of the user
        cmd, server.TMUX_CMD = server.TMUX_CMD, ["tmux", "-S", str(Path(sock, "tmux.sock")), "-f", str(conf)]
        self.addCleanup(setattr, server, "TMUX_CMD", cmd)

        def end_server():
            try:
                server._tmux("kill-server", check=False)
            except RiverError:
                pass  # a sandbox blocks the socket: then no server started either
        self.addCleanup(end_server)  # before the start: a start that fails halfway may have started the server
        try:
            server._open_terminal({"project": "shop", "path": self.dir.name, "session_title": "#1 first: a | b",
                                   "command": "sleep 60", "launch_in": "tmux"}, {"MAXPM_AGENT": "shop-aaaa"})
        except RiverError as e:
            self.skipTest(f"tmux cannot run here: {e}")  # a sandbox blocks its socket
        for name, command in (("#2 second", "sleep 60"), ("needs you", "true")):
            server._open_terminal({"project": "shop", "path": self.dir.name, "session_title": name, "command": command,
                                   "launch_in": "tmux"}, {"MAXPM_FOCUS": "needs:"})

        def panes(want):
            for _ in range(100):
                got = {p["name"]: p for p in server.tmux_view(None)["panes"]}
                if all(want(p) for p in got.values()):
                    return got
                time.sleep(0.1)
            self.fail(f"the panes did not get there: {got}")
        # A pane river just opened shows a shell: for START_GRACE it starts, and no tidy closes it (#1011).
        got = panes(lambda p: p["starting"] if p["name"] == "needs you" else p["running"] == "sleep")
        self.assertEqual(([p["ended"] for p in got.values()], server.tmux_view(None, tidy=True)["closed"]),
                         ([False, False, False], []))
        self.addCleanup(setattr, server, "START_GRACE", server.START_GRACE)
        server.START_GRACE = 0
        got = panes(lambda p: p["ended"] if p["name"] == "needs you" else p["running"] == "sleep")
        self.assertEqual([got[n]["agent"] for n in ("#1 first: a | b", "#2 second", "needs you")], ["shop-aaaa", None, None])
        self.assertEqual(len({p["window"] for p in got.values()}), 3)
        self.assertEqual([p["ended"] for p in got.values()], [False, False, True])
        self.assertEqual(server._tmux_pane_of(got["#2 second"]["tty"])["pane"], got["#2 second"]["pane"])
        v = server.tmux_view("tile")
        self.assertEqual(({p["window"] for p in v["panes"]}, {p["tile"] for p in v["panes"]}, v["left"]),
                         ({got["#1 first: a | b"]["window"]}, {True}, []))
        self.assertEqual(server._tmux("display-message", "-p", "-t", v["panes"][0]["window"], "#{window_name} #{pane-border-status}"),
                         "agents top")
        server._open_terminal({"project": "shop", "path": self.dir.name, "session_title": "#4 fourth", "command": "sleep 60",
                               "launch_in": "tmux"}, {})
        self.assertEqual(len({p["window"] for p in server.tmux_view(None)["panes"]}), 1)  # it joined the others
        v = server.tmux_view("windows", tidy=True)
        self.assertEqual(([c["name"] for c in v["closed"]], len(v["panes"]), len({p["window"] for p in v["panes"]})),
                         (["needs you"], 3, 3))
        self.assertEqual({p["tile"] for p in v["panes"]}, {False})
        names = server._tmux("list-windows", "-t", "=maxpm", "-F", "#{window_name}").splitlines()
        self.assertEqual(sorted(names), ["#1 first: a | b", "#2 second", "#4 fourth"])
        # The page's terminal: read a pane and type into it (a shell here, never an agent).
        core.register(self.c, "shop-aaaa")
        server._tmux("send-keys", "-t", got["#1 first: a | b"]["pane"], "C-c")
        self.assertEqual(server.agent_terminals(self.c), {"shop-aaaa": got["#1 first: a | b"]["pane"]})
        server.terminal_keys(self.c, "shop-aaaa", [{"text": "printf '\\033[31m%s\\033[0m\\n' river-$((40+2))"}, {"key": "Enter"}])
        for _ in range(100):
            scr = server.terminal_screen(self.c, "shop-aaaa")
            if "river-42" in scr["text"]:
                break
            time.sleep(0.1)
        self.assertIn("\x1b[31mriver-42", scr["text"])  # the colours come with the text
        self.assertEqual((scr["name"], scr["ended"], scr["width"] > 20), ("#1 first: a | b", True, True))
        # An agent idle at its prompt with work for it: river types nothing into its pane. It closes the pane and starts
        # a fresh session for the work (a script stands for the agent CLI).
        from unittest import mock
        fake = Path(self.dir.name, "fake-agent")
        fake.write_text('echo "fake agent: $MAXPM_AGENT $MAXPM_FOCUS"\nexec sleep 60\n')
        # /bin/sh runs it: macOS scans a new executable file before its first start (Gatekeeper; it took 13 s on a busy
        # Mac, past the 10 s the panes get), and never a script that a system shell reads.
        core.config_set(self.c, "launch_agents", f"Fake=/bin/sh {fake}")
        core.config_set(self.c, "launch_in", "tmux")
        server._open_terminal({"project": "shop", "path": self.dir.name, "session_title": "#6 sixth", "command": f"/bin/sh {fake}",
                               "launch_in": "tmux"}, {"MAXPM_AGENT": "shop-cccc"})
        sixth = panes(lambda p: p["name"] != "#6 sixth" or p["running"] == "sleep")["#6 sixth"]["pane"]
        core.register(self.c, "shop-cccc")
        core.register(self.c, "mark", human=True)
        clock = [core.now() + core.timedelta(seconds=1)]
        self.addCleanup(server.IDLE.clear)
        with mock.patch.object(core, "now", lambda: clock[0]):
            core.activity(self.c, "shop-cccc")
            seventh = core.item_add(self.c, "shop", "seventh")["id"]
            clock[0] += core.timedelta(seconds=1)
            core.push(self.c, seventh, "shop-cccc", "take it", "mark")
            runs = []
            for _ in range(6):
                clock[0] += core.timedelta(seconds=40)
                runs.append(server.fresh_sessions(self.c))
        (fresh,) = [r["started"][seventh] for r in runs if r["started"]]
        self.assertEqual([r["ended"] for r in runs if r["ended"]], [{"shop-cccc": "idle at its prompt; fresh sessions took its work"}])
        self.assertNotIn(sixth, [p["pane"] for p in server._tmux_panes()])
        (new,) = [p for p in panes(lambda p: p["agent"] != fresh or p["running"] == "sleep").values() if p["agent"] == fresh]
        self.assertEqual(new["name"], f"#{seventh} seventh")
        self.assertIn(f"fake agent: {fresh} item:{seventh}", server._pane_texts([new["pane"]])[new["pane"]])
        server._tmux("kill-pane", "-t", new["pane"])
        # A session that left the queue while its command still runs is done; not while its pane shows a prompt.
        # One tmux command reads the panes that may be done, and gives nothing when one of them closed meanwhile.
        server._open_terminal({"project": "shop", "path": self.dir.name, "session_title": "#5 fifth", "command": "sleep 60",
                               "launch_in": "tmux"}, {"MAXPM_AGENT": "shop-bbbb"})
        fifth = panes(lambda p: p["name"] != "#5 fifth" or p["running"] == "sleep")["#5 fifth"]["pane"]
        other = got["#2 second"]["pane"]
        texts = server._pane_texts([fifth, other])
        flat = {pane: text.replace("\n", "") for pane, text in texts.items()}  # a long command line wraps
        self.assertEqual((set(flat), "MAXPM_AGENT=shop-bbbb sleep 60" in flat[fifth], "MAXPM_FOCUS=needs: sleep 60" in flat[other]),
                         ({fifth, other}, True, True))
        self.assertEqual(server._pane_texts([fifth, "%999"]), {})
        self.assertEqual([(p["name"], p["why"]) for p in server.tmux_done(self.c)],
                         [("#1 first: a | b", "its agent ended"), ("#5 fifth", "shop-bbbb is not in the queue any more")])
        server._tmux("send-keys", "-t", fifth, "-l", "Do you want to proceed?")  # the terminal shows what is typed
        for _ in range(100):
            if "proceed?" in server._pane_texts([fifth])[fifth]:
                break
            time.sleep(0.1)
        self.assertEqual([p["name"] for p in server.tmux_done(self.c)], ["#1 first: a | b"])
        self.assertEqual([c["name"] for c in server.tmux_tidy(self.c)["closed"]], ["#1 first: a | b"])
        self.assertEqual(sorted(p["name"] for p in server.tmux_view(None, conn=self.c)["panes"]), ["#2 second", "#4 fourth", "#5 fifth"])
        # maxpm serve tidies by itself: a shell closes at once, an open CLI after the same screen for idle_after, a prompt never.
        server._open_terminal({"project": "shop", "path": self.dir.name, "session_title": "#8 eighth", "command": "sleep 60",
                               "launch_in": "tmux"}, {"MAXPM_AGENT": "shop-dddd"})
        panes(lambda p: p["name"] != "#8 eighth" or p["running"] == "sleep")
        server._tmux("send-keys", "-t", other, "C-c")
        panes(lambda p: p["name"] != "#2 second" or p["ended"])
        self.addCleanup(server.TIDY.update, at=None, screens={})
        clock = [core.now()]
        with mock.patch.object(core, "now", lambda: clock[0]):
            self.assertEqual([c["name"] for c in server.auto_tidy(self.c)], ["#2 second"])
            clock[0] += core.timedelta(minutes=1)
            self.assertEqual(server.auto_tidy(self.c), [])
            clock[0] += core.timedelta(minutes=1)
            self.assertEqual([c["name"] for c in server.auto_tidy(self.c)], ["#8 eighth"])
        self.assertEqual(sorted(p["name"] for p in server.tmux_view(None)["panes"]), ["#4 fourth", "#5 fifth"])


class TmuxOrphans(unittest.TestCase):
    """The tmux servers the tests left behind with their socket gone (#946): found by their exact command line, and
    ended by maxpm serve every tidy_every and by maxpm view --orphans --tidy."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["MAXPM_DB"] = os.path.join(self.dir.name, "t.db")
        self.c = core.connect()
        core.register(self.c, "mark", human=True)
        self.gone = os.path.join(tempfile.gettempdir(), "tmpgone946x")  # never made
        self.live = tempfile.mkdtemp()
        Path(self.live, "tmux.sock").touch()  # a server that a tmux command still reaches
        self.addCleanup(shutil.rmtree, self.live, True)
        self.addCleanup(setattr, server, "ORPHAN_PS", None)
        self.addCleanup(server.ORPHANS.update, at=None)

    def tearDown(self):
        self.c.close()
        os.environ.pop("MAXPM_DB", None)
        self.dir.cleanup()

    def ps(self, *rows):
        """rows: (pid, ppid, etime, args); the args of a test's server from (folder, session)."""
        lines = []
        for pid, ppid, age, args in rows:
            if isinstance(args, tuple):
                d, session = args
                args = f"tmux -S {d}/tmux.sock -f {d}/tmux.conf new-session -d -s {session} -n #1 first: a | b -x 200"
            lines.append(f"{pid:>6} {ppid:>6} {age:>12} {args}")
        server.ORPHAN_PS = lambda: "\n".join(lines) + "\n"

    def test_only_a_tests_server_with_its_socket_gone_and_only_shells(self):
        elsewhere = os.path.join(os.path.expanduser("~"), "tmpgone946x")
        self.ps((10, 1, "2-03:00:00", (self.gone, "maxpm")),
                (11, 10, "2-03:00:00", "-sh"),
                (12, 10, "2-03:00:00", "/bin/sh"),
                (20, 1, "05:00", (self.gone, "river")),  # the session's first name
                (30, 1, "05:00", (self.live, "maxpm")),  # its socket is there
                (40, 1, "00:30", (self.gone + "y", "maxpm")),  # it may not have made its socket yet
                (50, 1, "05:00", (self.gone + "z", "maxpm")),
                (51, 50, "05:00", "-zsh"),
                (52, 51, "04:00", "claude --name #9 work"),  # an agent CLI in a pane
                (60, 1, "05:00", (elsewhere, "maxpm")),  # not in a temporary folder
                (70, 1, "05:00", (self.gone + "w", "work")),  # another session
                (80, 1, "05:00", (os.path.join(tempfile.gettempdir(), "mine"), "maxpm")),  # not a tmp* folder
                (90, 1, "05:00", f"tmux -S {self.gone}v/tmux.sock -f /etc/tmux.conf new-session -d -s maxpm"),
                (100, 1, "05:00", "/opt/homebrew/bin/tmux new-session -d -s maxpm -n #479 Write -x 200 -y 50"),
                (110, 1, "05:00", f"tmux -S {self.gone}u/tmux.sock attach"))
        self.assertEqual([(o["pid"], o["session"], o["age"]) for o in server.tmux_orphans()],
                         [(10, "maxpm", 2 * 86400 + 3 * 3600), (20, "river", 300)])
        self.assertEqual(server.tmux_orphans()[0]["socket"], self.gone + "/tmux.sock")

    def test_serve_ends_them_every_tidy_every_and_the_command_lists_then_ends(self):
        from river import cli
        self.ps((10, 1, "1-00:00:00", (self.gone, "maxpm")), (30, 1, "05:00", (self.live, "maxpm")))
        killed = []
        with mock.patch.object(server.os, "kill", lambda pid, sig: killed.append(pid)):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.run(["--as", "mark", "view", "--orphans"])
            self.assertEqual(killed, [])
            self.assertIn("left behind: tmux server PID 10 (session maxpm, 24h00m old", out.getvalue())
            self.assertIn("maxpm view --orphans --tidy ends it", out.getvalue())
            with contextlib.redirect_stdout(out):
                cli.run(["--as", "mark", "view", "--orphans", "--tidy"])
            self.assertEqual(killed, [10])
            self.assertIn("ended: tmux server PID 10", out.getvalue())
            clock = [core.now()]
            with mock.patch.object(core, "now", lambda: clock[0]):
                self.assertEqual([o["pid"] for o in server.auto_end_orphans(self.c)], [10])
                clock[0] += core.timedelta(minutes=19)
                self.assertEqual(server.auto_end_orphans(self.c), [])  # tidy_every 20m
                clock[0] += core.timedelta(minutes=1)
                self.assertEqual([o["pid"] for o in server.auto_end_orphans(self.c)], [10])
                core.config_set(self.c, "tidy_every", "0s")
                clock[0] += core.timedelta(hours=1)
                self.assertEqual(server.auto_end_orphans(self.c), [])
            self.assertEqual(killed, [10, 10, 10])
        # No ps (a sandbox blocks it): the command says so; maxpm serve's pass finds nothing.
        server.ORPHAN_PS = mock.Mock(side_effect=OSError("Operation not permitted"))
        with self.assertRaisesRegex(RiverError, "sandbox"):
            server.tmux_orphans()
        core.config_set(self.c, "tidy_every", "20m")
        server.ORPHANS.update(at=None)
        self.assertEqual(server.auto_end_orphans(self.c), [])


class Watched(unittest.TestCase):
    def test_dev_reload_sees_subfolders_but_not_vendor(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            static = Path(d)
            for f in ("app.js", "components/chip.js", "vendor/lib/big.js", ".hidden/x.js"):
                (static / f).parent.mkdir(parents=True, exist_ok=True)
                (static / f).write_text("x")
            old, server.STATIC = server.STATIC, static
            try:
                names = [f.relative_to(static).as_posix() for f in server._watched() if f.suffix == ".js"]
                before = server._build_id()
                os.utime(static / "components/chip.js", ns=(1, 2 ** 62))
                self.assertNotEqual(server._build_id(), before)
            finally:
                server.STATIC = old
        self.assertEqual(names, ["app.js", "components/chip.js"])


if __name__ == "__main__":
    unittest.main()


class SetupGuide(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["MAXPM_DB"] = os.path.join(self.dir.name, "t.db")
        self.c = core.connect()
        self.folder = os.path.join(self.dir.name, "shop")
        os.mkdir(self.folder)
        core.project_add(self.c, "shop", path=self.folder)

    def tearDown(self):
        self.c.close()
        os.environ.pop("MAXPM_DB", None)
        self.dir.cleanup()

    def test_block_fix_writes_both_files_and_status_follows(self):
        before = server.setup_status(self.c)
        self.assertFalse(before["done"])
        self.assertEqual(before["folders"][0]["claude_md"], "missing")
        server.setup_block(self.c, self.folder)
        f = server.setup_status(self.c)["folders"][0]
        self.assertEqual((f["claude_md"], f["agents_md"]), ("current", "current"))

    def test_block_fix_can_move_claude_rules_to_agents_md(self):
        with open(os.path.join(self.folder, "CLAUDE.md"), "w") as f:
            f.write("# Rules\n")
        self.assertEqual(server.setup_status(self.c)["folders"][0]["layout"], "claude_only")
        server.setup_block(self.c, self.folder, move=True)
        f = server.setup_status(self.c)["folders"][0]
        self.assertEqual((f["claude_md"], f["agents_md"], f["layout"]), ("current", "current", "shared"))

    def test_add_a_project_folder_from_the_page(self):
        blog = os.path.join(self.dir.name, "My Blog")
        os.mkdir(blog)
        with open(os.path.join(blog, "CLAUDE.md"), "w") as f:
            f.write("# Rules\nNo tabs.\n")
        r = server.folder_add(self.c, blog, description="my writing")
        self.assertEqual((r["project"], r["layout"]), ("my-blog", "claude_only"))
        self.assertEqual(core._project(self.c, "my-blog")["path"], os.path.realpath(blog))
        self.assertEqual(core._project(self.c, "my-blog")["notes"], "my writing")
        self.assertIn("MaximizePM", Path(os.path.join(blog, "AGENTS.md")).read_text())
        # the rules choice, then the same folder again: nothing new, still one project
        r = server.folder_add(self.c, blog, move=True)
        self.assertEqual((r["project"], r["layout"]), ("my-blog", "shared"))
        self.assertEqual(Path(os.path.join(blog, "CLAUDE.md")).read_text().strip(), "@AGENTS.md")
        self.assertEqual(sum(p["name"] == "my-blog" for p in core.project_list(self.c)), 1)

    def test_add_a_project_folder_refuses_bad_paths_and_a_name_in_use_elsewhere(self):
        for bad in ("", "relative/path", os.path.join(self.dir.name, "missing")):
            with self.assertRaises(RiverError):
                server.folder_add(self.c, bad)
        other = os.path.join(self.dir.name, "other")
        os.mkdir(other)
        with self.assertRaisesRegex(RiverError, "--move"):
            server.folder_add(self.c, other, name="shop")
        self.assertEqual(core._project(self.c, "shop")["path"], os.path.realpath(self.folder))

    @unittest.skipIf(os.name == "nt", "the maxpm command installer is for macOS and Linux shells")
    def test_install_the_maxpm_command_writes_the_launcher_and_the_path_once(self):
        home = os.path.join(self.dir.name, "home")
        os.mkdir(home)
        found = {"path": None}
        old = (os.environ.get("HOME"), os.environ.get("SHELL"), server._login_shell_maxpm, core.PLATFORM)
        os.environ["HOME"], os.environ["SHELL"], core.PLATFORM = home, "/bin/zsh", "darwin"
        server._login_shell_maxpm = lambda: found["path"]
        try:
            self.assertFalse(server.command_status()["ok"])
            r = server.install_command()
            launcher = os.path.join(home, ".local", "bin", "maxpm")
            self.assertTrue(os.access(launcher, os.X_OK))
            self.assertIn(server.LAUNCHER_MARK, Path(launcher).read_text())
            prof = Path(os.path.join(home, ".zprofile")).read_text()
            self.assertIn('$HOME/.local/bin', prof)
            self.assertEqual(len(r["changed"]), 2)
            self.assertEqual(os.listdir(os.path.join(home, ".local", "bin")), ["maxpm"])  # one command, no other name
            self.assertNotRegex(Path(launcher).read_text().replace("river-app", ""), r"(?<![/\w])river(?![/\w])")
            found["path"] = launcher  # a new terminal now finds it
            self.assertTrue(server.command_status()["ok"])
            server.install_command()  # again: the profile line is not added twice
            self.assertEqual(Path(os.path.join(home, ".zprofile")).read_text(), prof)
            # a maxpm command the person installed (a clone, pip) is left alone
            found["path"] = "/opt/elsewhere/maxpm"
            self.assertTrue(server.command_status()["ok"])
            self.assertIn("already installed", server.install_command()["note"])
            # a launcher of an older copy is the guide's own, and the guide rewrites it
            with open(launcher, "w") as f:
                f.write("#!/bin/sh\n# MaximizePM launcher: runs an older copy ...\n")
            self.assertEqual(server.command_status()["launcher"], "old")
        finally:
            for k, v in (("HOME", old[0]), ("SHELL", old[1])):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            server._login_shell_maxpm, core.PLATFORM = old[2], old[3]

    def test_block_fix_refuses_a_folder_that_is_not_a_project(self):
        with self.assertRaises(RiverError):
            server.setup_block(self.c, self.dir.name)
        self.assertFalse(os.path.exists(os.path.join(self.dir.name, "CLAUDE.md")))

    def test_an_added_agent_becomes_the_default_when_the_first_is_not_installed(self):
        old = server._login_shell_which, core.PLATFORM
        server._login_shell_which, core.PLATFORM = (lambda names: {n: None for n in names}), "darwin"
        try:
            self.assertEqual(server.setup_agent_add(self.c, "Codex"), ["Codex", "Claude Code"])
        finally:
            server._login_shell_which, core.PLATFORM = old

    def test_add_agent_appends_to_launch_agents_once(self):
        old = server._login_shell_which
        server._login_shell_which = lambda names: {n: "/bin/" + n for n in names}
        self.addCleanup(setattr, server, "_login_shell_which", old)
        server.setup_agent_add(self.c, "Codex")
        labels = server.setup_agent_add(self.c, "Codex")
        self.assertEqual(labels, ["Claude Code", "Codex"])
        # Codex's sandbox writes only in the project folder: the command lets it write the queue too.
        self.assertEqual(dict(core.parse_launch_agents(core.setting(self.c, "launch_agents")))["Codex"], "@codex")
        codex = core._launch_agent_cmd(self.c, None, "Codex")["command"]
        self.assertRegex(codex, r"--add-dir [\"']?" + re.escape(str(core.db_path().resolve().parent)))
        shown = {a["label"]: a["command"] for a in server.setup_status(self.c)["agent_clis"]}
        self.assertEqual(shown["Codex"], codex)
        self.assertEqual(shown["Claude Code"], "claude go --remote-control")
        with self.assertRaises(RiverError):
            server.setup_agent_add(self.c, "nope")

    def test_dismiss_is_a_setting(self):
        core.config_set(self.c, "setup_done", "on")
        self.assertTrue(server.setup_status(self.c)["done"])
        with self.assertRaises(RiverError):
            core.config_set(self.c, "setup_done", "maybe")


class LaunchProfiles(unittest.TestCase):
    """Claude Code and Codex commands built from options (launch profiles), and the move of old command strings."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["MAXPM_DB"] = os.path.join(self.dir.name, "t.db")
        self.c = core.connect()
        self.platform = core.PLATFORM
        core.PLATFORM = "darwin"
        core.project_add(self.c, "shop", path=self.dir.name)
        core.config_set(self.c, "claude_skill_prompt", "off")  # exact commands; test_the_skill_goes_in_the_system_prompt

    def tearDown(self):
        core.PLATFORM = self.platform
        self.c.close()
        os.environ.pop("MAXPM_DB", None)
        self.dir.cleanup()

    def test_each_option_set_builds_the_real_flags(self):
        claude = core.profile_options(self.c, "claude-code")
        self.assertEqual(core.build_command("claude-code", claude), "claude go --remote-control")
        # A session name goes to --name and to Remote Control; Codex has no flag for one.
        self.assertEqual(core.build_command("claude-code", claude, name="ship v2"), "claude --name 'ship v2' go --remote-control 'ship v2'")
        codex_opts = core.profile_options(self.c, "codex")
        self.assertEqual(core.build_command("codex", codex_opts, name="ship v2"), core.build_command("codex", codex_opts))
        # The name: the goal the item serves, else the item; short, and without shell or quote characters.
        self.assertEqual(core.session_title(["ship v2"], 7, "work"), "ship v2")
        self.assertEqual(core.session_title([], 490, "Cells infra: Pulumi state to B2"), "#490 Cells infra: Pulumi state to B2")
        self.assertEqual(core.session_title([], 7, "it's \"x\" & `y`; $(z) " + "long " * 20)[:22], "#7 it s x y z long lon")
        self.assertLessEqual(len(core.session_title([], 7, "long " * 20)), 48)
        self.assertEqual(core.session_title([], 395, "Write the script for the complex workflow walkthrough video"),
                         "#395 Write the script for the complex workflow")
        self.assertEqual(core.build_command("claude-code", claude, "opus", "high"),
                         "claude --model opus --effort high go --remote-control")
        self.assertEqual(core.build_command("claude-code", {**claude, "remote_control": "off", "permission_mode": "plan",
                                                            "args": "--verbose", "prompt": "run maxpm go now"}, "opus"),
                         "claude --model opus --permission-mode plan --verbose 'run maxpm go now'")
        rd = core._shell_quote(core.river_dir())
        codex = core.profile_options(self.c, "codex")
        self.assertEqual(core.build_command("codex", codex, "sol", "xhigh"),
                         f"codex -m sol -c model_reasoning_effort=xhigh --add-dir {rd} "
                         "'run maxpm go in this folder and follow the briefing'")
        self.assertEqual(core.build_command("codex", {**codex, "sandbox": "workspace-write", "approval": "never",
                                                      "prompt": "go"}),
                         f"codex --sandbox workspace-write --ask-for-approval never --add-dir {rd} go")

    def test_a_session_for_an_item_with_a_goal_gets_the_goal_name(self):
        core.goal_add(self.c, "shop", "ship-v2")
        x = core.item_add(self.c, "shop", "work", goals=["ship-v2"])["id"]
        t = core.launch_target(self.c, item=x)
        self.assertEqual(t["session_title"], "ship-v2")
        self.assertEqual(t["command"], "claude --name ship-v2 go --remote-control ship-v2")

    def test_options_come_from_settings_the_entry_and_the_dialog(self):
        core.config_set(self.c, "launch_agents", "Claude Code=@claude-code; Plan=@claude-code permission_mode=plan; Codex=@codex")
        core.config_set(self.c, "claude_remote_control", "off")
        core.config_set(self.c, "claude_remote_control", "on", project="shop")  # most specific wins
        pid = core._project(self.c, "shop")["id"]
        self.assertEqual(core._launch_agent_cmd(self.c, None, None)["command"], "claude go")
        self.assertEqual(core._launch_agent_cmd(self.c, pid, None)["command"], "claude go --remote-control")
        self.assertEqual(core._launch_agent_cmd(self.c, pid, "Plan")["command"],
                         "claude --permission-mode plan go --remote-control")
        t = core._launch_agent_cmd(self.c, pid, "Plan", "opus", options={"remote_control": "off", "permission_mode": "auto"})
        self.assertEqual((t["command"], t["platform"]), ("claude --model opus --permission-mode auto go", "claude-code"))
        with self.assertRaisesRegex(RiverError, "no option 'args' to choose at launch"):
            core._launch_agent_cmd(self.c, pid, None, options={"args": "--x"})
        with self.assertRaisesRegex(RiverError, "sandbox is one of"):
            core._launch_agent_cmd(self.c, pid, "Codex", options={"sandbox": "wide-open"})
        core.config_set(self.c, "launch_agents", "Claude Code=@claude-code; Grok=grok go")
        with self.assertRaisesRegex(RiverError, "custom command"):
            core._launch_agent_cmd(self.c, pid, "Grok", options={"remote_control": "off"})
        # Settings and entries are checked when set.
        for key, value, msg in (("claude_remote_control", "yes", "on or off"), ("codex_approval", "always", "one of"),
                                ("claude_prompt", " ", "first prompt"), ("launch_agents", "X=@gemini", "platform is one of"),
                                ("launch_agents", "X=@codex colour=red", "no option 'colour'"),
                                ("launch_agents", "X=@codex sandbox", "option=value")):
            with self.assertRaisesRegex(RiverError, msg):
                core.config_set(self.c, key, value)
        # The page: the dialog's options per agent, and Setup's options per profile.
        opts = {o["label"]: o for o in core.state(self.c)["launch_options"]}
        self.assertEqual(opts["Claude Code"]["options"][0]["value"], "off")  # the global value
        self.assertEqual((opts["Grok"]["options"], opts["Grok"]["takes_model"]), ([], False))
        (prof,) = server.setup_status(self.c)["launch_profiles"]
        self.assertEqual([o["setting"] for o in prof["options"]],
                         ["claude_remote_control", "claude_permission_mode", "claude_model_ids", "claude_args",
                          "claude_skill_prompt", "claude_prompt"])

    def test_the_skill_goes_in_the_system_prompt(self):
        pid = core._project(self.c, "shop")["id"]
        core.config_unset(self.c, "claude_skill_prompt")  # the default: on
        cmd = core._launch_agent_cmd(self.c, pid, None)["command"]
        m = re.search(r"--append-system-prompt-file (\S+) go --remote-control$", cmd)
        self.assertIsNotNone(m, cmd)
        f = Path(m.group(1).strip("'"))  # quoted on Windows: the test's platform is darwin, the path has backslashes
        self.assertEqual(f.parent, Path(core.river_dir()) / "prompts")
        text = f.read_text()
        self.assertTrue(text.startswith(core.SKILL_PROMPT_HEAD))
        self.assertIn("maxpm go", text)
        self.assertNotIn("\nname: maxpm", text)  # the skill's front matter stays out
        # The same skill gives the same file, so every launch sends the same bytes (a prompt cache read).
        self.assertEqual(core._launch_agent_cmd(self.c, pid, None)["command"], cmd)
        # A primer that claude_args names goes in the same file, first; the other arguments stay.
        primer = Path(self.dir.name) / "primer.md"
        primer.write_text("# Primer\nThe map of the shop.\n")
        core.config_set(self.c, "claude_args", f"--verbose --append-system-prompt-file {shlex.quote(str(primer))}",
                        project="shop")
        cmd2 = core._launch_agent_cmd(self.c, pid, None)["command"]
        self.assertEqual(cmd2.count("--append-system-prompt-file"), 1)
        self.assertIn("claude --verbose --append-system-prompt-file ", cmd2)
        text2 = Path(re.search(r"--append-system-prompt-file (\S+)", cmd2).group(1).strip("'")).read_text()
        self.assertTrue(text2.startswith("# Primer\nThe map of the shop.\n\n" + core.SKILL_PROMPT_HEAD))
        # On Windows the arguments split the cmd.exe way: double quotes, and a path keeps its backslashes.
        core.PLATFORM = "win32"
        spaced = Path(self.dir.name) / "my primer.md"
        spaced.write_text("# Spaced\n")
        words = core.skill_prompt_args(f'--verbose --append-system-prompt-file "{spaced}"').split(
            " --append-system-prompt-file ")
        self.assertEqual(words[0], "--verbose")
        self.assertTrue(Path(words[1].strip('"')).read_text().startswith("# Spaced\n\n" + core.SKILL_PROMPT_HEAD))
        core.PLATFORM = "darwin"
        # A primer that cannot be read: the arguments stay as they are, and the skill stays out.
        core.config_set(self.c, "claude_args", "--append-system-prompt-file /no/such/primer.md", project="shop")
        self.assertIn("--append-system-prompt-file /no/such/primer.md go", core._launch_agent_cmd(self.c, pid, None)["command"])
        # Off: no file.
        core.config_set(self.c, "claude_skill_prompt", "off", project="shop")
        self.assertNotIn("--append-system-prompt-file /", core._launch_agent_cmd(self.c, pid, None)["command"].replace(
            "/no/such/primer.md", ""))

    def test_items_choose_an_agent_type(self):
        c = self.c
        core.config_set(c, "launch_agents", "Claude Code=@claude-code; Codex=@codex")
        css = core.item_add(c, "shop", "restyle the header", touches=["web/site.css"])["id"]
        logo = core.item_add(c, "shop", "draw a new logo")["id"]
        own = core.item_add(c, "shop", "write the docs", models={"agent": "Codex"})["id"]  # a launch_agents label
        plain = core.item_add(c, "shop", "fix the parser", priority=3)["id"]
        self.assertEqual(core.item_show(c, own)["agent"], "codex")
        with self.assertRaisesRegex(RiverError, "agent type is one of"):
            core.item_edit(c, plain, models={"agent": "gemini"})
        with self.assertRaisesRegex(RiverError, "runs openai models.*model opus is claude"):
            core.item_edit(c, own, models={"model": "opus"})
        # Rules pick a type from the files an item touches or a word in its title; default_agent is the fallback.
        core.config_set(c, "agent_rules", "codex: *.css, logo")
        with self.assertRaisesRegex(RiverError, "needs the form"):
            core.config_set(c, "agent_rules", "gemini: *.css")
        ann = core.annotate(c)
        self.assertEqual([(ann[i]["agent"], ann[i]["agent_from"]) for i in (css, logo, own, plain)],
                         [("codex", "agent_rules"), ("codex", "agent_rules"), ("codex", "item"), (None, None)])
        core.config_set(c, "default_agent", "claude-code", project="shop")
        self.assertEqual(core.annotate(c)[plain]["agent"], "claude-code")
        # A session's type comes from its environment, else its model's family.
        self.assertEqual([core.agent_type_from_env(e) for e in (
            {"CODEX_THREAD_ID": "x", "CLAUDECODE": "1"}, {"CLAUDECODE": "1"}, {"MAXPM_AGENT_TYPE": "codex", "CLAUDECODE": "1"}, {})],
            ["codex", "claude-code", "codex", None])
        core.register(c, "cl")
        core.set_agent_type(c, "cl", "claude-code")
        skipped = []
        got = core.next_item(c, "shop", actor="cl", limit=9, skipped=skipped)
        self.assertEqual([a["id"] for a in got], [plain])
        self.assertEqual({x["id"] for x in skipped}, {css, logo, own})
        self.assertIn("is for codex; this session runs claude-code", skipped[0]["why"])
        with self.assertRaisesRegex(RiverError, "is for codex"):
            core.claim(c, css, "cl")
        core.register(c, "cx")
        core.set_agent_model(c, "cx", "sol")  # no recorded type: sol is an OpenAI model, so Codex
        self.assertEqual(core.session_type(c, "cx"), "codex")
        self.assertEqual([a["id"] for a in core.next_item(c, "shop", actor="cx", limit=9)], [css, logo, own])
        # A person pushes an item to a session of another type: the session takes it.
        core.push(c, logo, "cl", "you do it", "mark")
        self.assertEqual(core.claim(c, logo, "cl")["status"], "in_progress")
        # Launch starts the item's type unless an agent is named; the manager counts ready work per type.
        self.assertEqual(core.launch_target(c, item=css)["agent"], "Codex")
        self.assertEqual(core.launch_target(c, item=css, agent="Claude Code")["agent"], "Claude Code")
        self.assertEqual(core.launch_target(c, item=plain)["agent"], "Claude Code")
        f = core.manager_findings(c)["uncovered"]
        self.assertEqual([(x["agent_type"], x["launch"], x["ready"]) for x in f], [("codex", "Codex", 2)])
        opts = {o["label"]: o["agent_type"] for o in core.state(c)["launch_options"]}
        self.assertEqual(opts, {"Claude Code": "claude-code", "Codex": "codex"})
        core.config_set(c, "launch_agents", "Claude Code=@claude-code")
        with self.assertRaisesRegex(RiverError, "no launch_agents entry runs it"):
            core.launch_target(c, item=css)

    def test_the_cli_gets_its_own_model_id_and_river_keeps_the_name(self):
        pid = core._project(self.c, "shop")["id"]
        rd = core._shell_quote(core.river_dir())
        codex_home = tempfile.TemporaryDirectory()
        self.addCleanup(codex_home.cleanup)
        os.environ["CODEX_HOME"] = codex_home.name
        self.addCleanup(os.environ.pop, "CODEX_HOME", None)
        core.config_set(self.c, "launch_agents", "Claude Code=@claude-code; Codex=@codex; Mine=codex -m {model} go")
        t = core._launch_agent_cmd(self.c, pid, "Codex", "astra", "high")
        self.assertEqual(t["command"], f"codex -m gpt-6-astra -c model_reasoning_effort=high --add-dir {rd} "
                                       "'run maxpm go in this folder and follow the briefing'")
        self.assertEqual((t["model"], t["model_id"], t["env"]), ("astra", "gpt-6-astra", {"MAXPM_MODEL": "astra"}))
        self.assertEqual(core._launch_agent_cmd(self.c, pid, "Mine", "terra")["command"], "codex -m gpt-5.6-terra go")
        # Claude Code takes the ladder names as they are.
        self.assertEqual(core._launch_agent_cmd(self.c, pid, "Claude Code", "fable", "max")["command"],
                         "claude --model fable --effort max go --remote-control")
        # A new release: change the list, per project if needed; a name with no entry goes as it is.
        core.config_set(self.c, "codex_model_ids", "astra=gpt-7-astra", project="shop")
        self.assertIn("-m gpt-7-astra ", core._launch_agent_cmd(self.c, pid, "Codex", "astra")["command"])
        self.assertIn("-m sol ", core._launch_agent_cmd(self.c, pid, "Codex", "sol")["command"])
        self.assertIn("-m gpt-6.1-sol ", core._launch_agent_cmd(self.c, None, "Codex", "sol")["command"])
        with self.assertRaisesRegex(RiverError, "name=id"):
            core.config_set(self.c, "codex_model_ids", "astra gpt-6-astra")
        # An item that names a CLI id keeps the ladder name, so limits, the page, and the dialog know it.
        x = core.item_add(self.c, "shop", "logo", models={"model": "gpt-6.1-sol"})["id"]
        self.assertEqual(core.item_show(self.c, x)["model"], "sol")
        self.assertEqual(core.state(self.c)["model_ids"]["codex"]["sol"], "gpt-6.1-sol")
        # Effort: Codex levels, or the model's own when the Codex model cache lists it.
        with self.assertRaisesRegex(RiverError, "not minimal"):
            core._launch_agent_cmd(self.c, None, "Codex", "sol", "minimal")
        Path(codex_home.name, "models_cache.json").write_text(json.dumps({"models": [
            {"slug": "gpt-6-luna", "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high")]}]}))
        self.assertIn("model_reasoning_effort=high", core._launch_agent_cmd(self.c, None, "Codex", "luna", "high")["command"])
        with self.assertRaisesRegex(RiverError, "gpt-6-luna takes effort low, medium, high, not max"):
            core._launch_agent_cmd(self.c, None, "Codex", "luna", "max")
        # The dialog offers each agent the models of its family only, and the CLI's effort levels.
        opts = {o["label"]: o for o in core.state(self.c)["launch_options"]}
        self.assertEqual([m["name"] for m in opts["Codex"]["models"]], ["luna", "terra", "sol", "astra"])
        self.assertEqual([m["name"] for m in opts["Claude Code"]["models"]], ["haiku", "sonnet", "opus", "fable"])
        self.assertEqual(opts["Codex"]["efforts"], ["low", "medium", "high", "xhigh", "max"])

    def test_a_launch_with_options_through_a_fake_runner(self):
        x = core.item_add(self.c, "shop", "work")["id"]
        sent = []
        server.TERMINAL_RUNNER = sent.append
        self.addCleanup(setattr, server, "TERMINAL_RUNNER", None)
        t = server.OPS["dispatch_item"](self.c, {"id": x, "model": "opus", "options": {"remote_control": "off"}}, "mark")
        self.assertEqual(t["command"], f"claude --name '#{x} work' --model opus go")
        self.assertIn(f"MAXPM_AGENT={t['session_name']} MAXPM_FOCUS=item:{x} MAXPM_MODEL=opus {t['command']}\"", sent[-1])
        y = core.item_add(self.c, "shop", "more")["id"]
        t = server.dispatch_item(self.c, y, runner=sent.append, options={"permission_mode": "acceptEdits"})
        self.assertIn(f"claude --name '#{y} more' --permission-mode acceptEdits go --remote-control '#{y} more'", sent[-1])
        # A manager: manage in place of go, the options still apply.
        # manager_autocompact auto: Claude Code's own point; an --autocompact in claude_args wins.
        core.config_set(self.c, "manager_autocompact", "auto")
        t = server.start_manager(self.c, runner=sent.append, options={"remote_control": "off"})
        self.assertEqual(t["command"], "claude --name 'maxpm manager' manage")
        core.config_set(self.c, "claude_args", "--autocompact 300k")
        t = core._launch_agent_cmd(self.c, None, None, options={"remote_control": "off"}, autocompact=200000)
        self.assertEqual((t["command"], t["autocompact"]), ("claude --autocompact 300k go", None))
        core.config_unset(self.c, "claude_args")
        # Open chat says why a session has no web link.
        core.register(self.c, "w1")
        core.config_set(self.c, "claude_remote_control", "off")
        self.assertIn("Remote Control is off", server.open_chat(self.c, "w1")["hint"])

    def test_a_custom_prompt_follows_the_profile_prompt(self):
        import shlex
        core.config_set(self.c, "launch_agents", "Claude Code=@claude-code; Codex=@codex; Grok=grok go")
        hard = 'Fix the "header", $HOME and `date`; it\'s 100% \\n done!\r\n\tthen push'
        want = 'Fix the "header", $HOME and `date`; it\'s 100% \\n done!\n then push'  # a tab becomes a space
        # Claude Code and Codex: the profile's prompt, a blank line, then the text, as one shell word.
        for agent, first in (("Claude Code", "go"), ("Codex", "run maxpm go in this folder and follow the briefing")):
            t = core._launch_agent_cmd(self.c, None, agent, name="#1 work", prompt=hard)
            self.assertEqual(t["custom_prompt"], want)
            self.assertIn(f"{first}\n\n{want}", shlex.split(t["command"]))
        self.assertEqual(shlex.split(core._launch_agent_cmd(self.c, None, "Claude Code", name="w", prompt=hard)["command"])[-3:],
                         [f"go\n\n{want}", "--remote-control", "w"])
        # The cap, an empty text, control characters, and a custom command are refused.
        self.assertEqual(core.custom_prompt("x" * core.CUSTOM_PROMPT_MAX), "x" * 4000)
        for bad, why in (("x" * 4001, "4001 characters; at most 4000"), (" \n ", "--prompt is empty"),
                         ("stop\x03", "control characters"), ("a\x1b[2J", "control characters")):
            with self.assertRaisesRegex(RiverError, re.escape(why)):
                core._launch_agent_cmd(self.c, None, "Claude Code", prompt=bad)
        with self.assertRaisesRegex(RiverError, "Grok is a custom command .* takes no --prompt"):
            core._launch_agent_cmd(self.c, None, "Grok", prompt="hi")
        # Windows: cmd cannot quote a line break, '"' or '%'; one line goes after the prompt with a space.
        core.PLATFORM = "win32"
        self.assertTrue(core._launch_agent_cmd(self.c, None, "Claude Code", prompt="fix it & push")["command"]
                        .endswith('"go fix it & push" --remote-control'))
        for bad in ("two\nlines", 'say "hi"', "100%"):
            with self.assertRaisesRegex(RiverError, "one line with no"):
                core._launch_agent_cmd(self.c, None, "Claude Code", prompt=bad)
        core.PLATFORM = "darwin"
        # A launch opens a new session even when one waits for work, and the item's history says what it was told.
        x = core.item_add(self.c, "shop", "work")["id"]
        core.register(self.c, "w1")
        self.c.execute("UPDATE agents SET role='waiting', waiting_in='shop', waiting_since=? WHERE name='w1'",
                       (core.iso(core.now()),))
        self.assertEqual(core.waiting_agent_for(self.c, "shop", x), "w1")
        sent, long = [], "Take over from w9: " + "step " * 60
        t = server.dispatch_item(self.c, x, runner=sent.append, actor="boss", prompt=long)
        self.assertNotIn("pushed_to", t)
        self.assertIn(shlex.quote("go\n\n" + long.strip()).replace('"', '\\"'), sent[-1])
        ev = [e["change"] for e in core.item_show(self.c, x)["events"] if "custom prompt" in e["change"]]
        self.assertEqual(ev, [f"{t['session_name']} launched with a custom prompt: " + long.strip()[:200] + "..."])
        y = core.item_add(self.c, "shop", "more")["id"]
        self.assertEqual(server.launch_agent(self.c, "shop", runner=sent.append)["pushed_to"], "w1")  # no prompt: as before
        z = core.item_add(self.c, "shop", "third")["id"]
        server.TERMINAL_RUNNER = sent.append
        self.addCleanup(setattr, server, "TERMINAL_RUNNER", None)
        t = server.OPS["launch_agent"](self.c, {"project": "shop", "prompt": "short one", "launch_in": "window"}, "boss")
        self.assertEqual(t["item"]["id"], z)
        self.assertIn(f"{t['session_name']} launched with a custom prompt: short one",
                      [e["change"] for e in core.item_show(self.c, z)["events"]])
        self.assertNotIn("custom prompt", str(core.item_show(self.c, y)["events"]))

    def test_the_command_line_takes_options(self):
        import contextlib
        import io
        from river import cli
        core.item_add(self.c, "shop", "work")
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.run(["-q", "launch", "--dry-run", "--option", "remote_control=off",
                                      "--option", "permission_mode=plan"]), 0)
        self.assertRegex(out.getvalue(), r"command: claude --name '#\d+ work' --permission-mode plan go   \(in a new tab\)")
        # --dry-run prints the whole command with the custom prompt; ';' is allowed in it.
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.run(["-q", "launch", "--dry-run", "--prompt", "fix a; then b\nand c"]), 0)
        self.assertRegex(out.getvalue(), r"command: claude --name '#\d+ work' 'go\n\nfix a; then b\nand c' --remote-control")

    def migrate(self, value, scope="global"):
        self.c.execute("INSERT INTO settings(scope,key,value) VALUES (?,?,?) "
                       "ON CONFLICT(scope,key) DO UPDATE SET value=excluded.value", (scope, "launch_agents", value))
        self.c.execute("DELETE FROM meta WHERE key='launch_profiles'")
        self.c.close()
        self.c = core.connect()
        return self.c.execute("SELECT value FROM settings WHERE scope=? AND key='launch_agents'", (scope,)).fetchone()["value"]

    def test_old_command_strings_become_profiles(self):
        # The report: a value saved before the launch dialog never got {model} and {effort}.
        self.assertEqual(self.migrate("Claude Code=claude go --remote-control"), "Claude Code=@claude-code")
        self.assertEqual(core._launch_agent_cmd(self.c, None, None, "opus", "high")["command"],
                         "claude --model opus --effort high go --remote-control")
        self.assertIn("Claude Code: claude go --remote-control -> @claude-code", core.launch_migration_note(self.c))
        # It runs once: a custom command set later stays as it is.
        core.config_set(self.c, "launch_agents", "Claude Code=claude go")
        self.c.close()
        self.c = core.connect()
        self.assertEqual(core.setting(self.c, "launch_agents"), "Claude Code=claude go")
        # Options: the first entry of a platform sets the scope's settings, a second one keeps its own.
        core.config_unset(self.c, "launch_agents")
        rd = core._shell_quote(core.river_dir())
        got = self.migrate(f"Claude Code=claude --model {{model}} --effort {{effort}} go; "
                           f"Planner=claude --permission-mode plan go --remote-control; "
                           f"Codex=codex -m {{model}} -c model_reasoning_effort={{effort}} --add-dir {rd} -s read-only "
                           f"\"run maxpm go and follow it\"; "
                           f"Fixed=claude --model sonnet go; Odd=claude --verbose go; Grok=grok \"run maxpm go\"", "project:shop")
        self.assertEqual(got, "Claude Code=@claude-code; Planner=@claude-code remote_control=on permission_mode=plan; "
                              "Codex=@codex; Fixed=claude --model sonnet go; Odd=claude --verbose go; Grok=grok \"run maxpm go\"")
        pid = core._project(self.c, "shop")["id"]
        self.assertEqual(core.setting(self.c, "claude_remote_control", project_id=pid), "off")
        self.assertEqual(core.setting(self.c, "claude_remote_control"), "on")  # other projects keep the default
        self.assertEqual((core.setting(self.c, "codex_sandbox", project_id=pid),
                          core.setting(self.c, "codex_prompt", project_id=pid)), ("read-only", "run maxpm go and follow it"))
        self.assertEqual(core._launch_agent_cmd(self.c, pid, "Codex", "sol")["command"],
                         f"codex -m gpt-6.1-sol --sandbox read-only --add-dir {rd} 'run maxpm go and follow it'")
        self.assertEqual(core._launch_agent_cmd(self.c, pid, "Planner")["command"],
                         "claude --permission-mode plan go --remote-control")
        # A remote-control name, or no prompt: custom, since a profile would change them.
        self.assertIsNone(core.profile_from_command("claude --remote-control mine go"))
        self.assertIsNone(core.profile_from_command("claude --model {model}"))
        self.assertIsNone(core.profile_from_command("codex --add-dir /elsewhere go"))
        from river import cli
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.run(["config", "get", "launch_agents", "--project", "shop"]), 0)
        self.assertIn("launch_agents migrated to launch profiles (project:shop)", out.getvalue())
        self.assertIn("claude_remote_control=off", out.getvalue())


class StartPushesToWaiting(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["MAXPM_DB"] = os.path.join(self.dir.name, "t.db")
        self.c = core.connect()
        core.project_add(self.c, "shop", path=self.dir.name)
        core.project_add(self.c, "other", path=self.dir.name + "/x")
        core.register(self.c, "w")

    def tearDown(self):
        self.c.close()
        os.environ.pop("MAXPM_DB", None)
        self.dir.cleanup()

    def test_waiting_session_gets_the_item_and_no_terminal_opens(self):
        core.wait(self.c, self.dir.name, "w", project="other", step="0s", sleep=lambda s: None)
        core.item_add(self.c, "shop", "agent step")
        x = core.item_add(self.c, "shop", "next step")["id"]
        sent = []
        t = server.launch_agent(self.c, runner=sent.append)  # w waits in another project: a new session
        self.assertNotIn("pushed_to", t)
        self.assertEqual(len(sent), 1)
        core.wait(self.c, self.dir.name, "w", project="shop", step="0s", sleep=lambda s: None)
        t = server.launch_agent(self.c, runner=sent.append)
        self.assertEqual((t["pushed_to"], len(sent)), ("w", 1))
        self.assertEqual(core.item_show(self.c, x)["reserved_for"], "w")


class ServeReload(unittest.TestCase):
    """maxpm serve runs new code without a person: it starts again by itself, or when maxpm serve --restart asks."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["MAXPM_DB"] = os.path.join(self.dir.name, "t.db")
        self.c = core.connect()
        self.addCleanup(server.RELOAD.update, {"seen": None, "bad": None})

    def tearDown(self):
        self.c.close()
        os.environ.pop("MAXPM_DB", None)
        self.dir.cleanup()

    def test_serve_starts_again_for_code_that_stayed_is_committed_and_loads(self):
        code, dirty, loads, asked = [server.BOOT_CODE], [False], [None], []
        with mock.patch.object(server, "_code_id", lambda: code[0]), \
                mock.patch.object(server, "_code_dirty", lambda: dirty[0]), \
                mock.patch.object(server, "_code_loads", lambda: (asked.append(code[0]), loads[0])[1]):
            ready = lambda: server.reload_ready(self.c)
            self.assertEqual((ready(), ready()), (None, None))  # the code it runs
            # A commit: the files must stay the same for one pass (nobody writes them now), then it starts again.
            code[0] = "2"
            self.assertEqual((ready(), ready()), (None, "2"))
            code[0] = "3"  # a second change in between: wait again
            self.assertEqual((ready(), ready()), (None, "3"))
            # An agent is in the middle of an edit (git shows a change that is not committed): the old code stays.
            code[0], dirty[0] = "4", True
            self.assertEqual((ready(), ready(), ready()), (None, None, None))
            dirty[0] = False
            self.assertEqual(ready(), "4")
            # Code that does not load never replaces the server; river asks once for each change.
            code[0], loads[0] = "5", "SyntaxError: invalid syntax"
            asked.clear()
            said = io.StringIO()
            with contextlib.redirect_stdout(said):
                self.assertEqual((ready(), ready(), ready(), asked), (None, None, None, ["5"]))
            self.assertEqual(said.getvalue().count("the new code does not load, so the old code keeps running"), 1)
            code[0], loads[0] = "6", None
            self.assertEqual((ready(), ready()), (None, "6"))
            # Dev mode restarts by itself, the app's code changes with the app, and the setting turns it off.
            with mock.patch.dict(server.DEV, {"on": True}):
                self.assertIsNone(ready())
            with mock.patch.object(server, "DESKTOP", True):
                self.assertIsNone(ready())
            core.config_set(self.c, "serve_reload", "off")
            self.assertIsNone(ready())
            with self.assertRaisesRegex(RiverError, "serve_reload is on or off"):
                core.config_set(self.c, "serve_reload", "maybe")

    def test_the_new_server_keeps_the_arguments_but_does_not_open_the_page_again(self):
        with mock.patch.object(sys, "argv", ["/x/bin/maxpm", "serve", "--port", "8765", "--open"]):
            self.assertEqual(server._restart_argv(), [sys.executable, "/x/bin/maxpm", "serve", "--port", "8765"])
            ran = []
            with mock.patch.object(os, "execv", lambda exe, argv: ran.append((exe, argv))), \
                    contextlib.redirect_stdout(io.StringIO()):
                server.restart_now()
            self.assertEqual(ran, [(sys.executable, [sys.executable, "/x/bin/maxpm", "serve", "--port", "8765"])])

    def test_only_the_loop_of_river_serve_starts_again(self):
        import threading
        from river import notify
        for port, want in ((8765, ["restart"]), (None, [])):  # maxpm notify run has the same loop, and no page
            stop, did = threading.Event(), []
            with mock.patch.dict(notify.SERVE_PORT, {"port": port}), \
                    mock.patch.object(server, "watch_prompts", lambda c: stop.set()), \
                    mock.patch.object(server, "reload_ready", lambda c: "2"), \
                    mock.patch.object(server, "restart_now", lambda why: did.append("restart")):
                notify.loop(stop, interval_s=1)
            self.assertEqual(did, want)

    def test_the_code_on_disk_loads_or_says_why_not(self):
        self.assertIsNone(server._code_loads())  # this clone
        bad = os.path.join(self.dir.name, "clone")
        os.makedirs(os.path.join(bad, "river"))
        open(os.path.join(bad, "river", "__init__.py"), "w").close()
        with open(os.path.join(bad, "river", "server.py"), "w") as f:
            f.write("def half(:\n")
        with mock.patch.object(server, "REPO", Path(bad)), mock.patch.object(server, "PKG", Path(bad, "river")):
            self.assertIn("SyntaxError", server._code_loads())
            self.assertFalse(server._code_dirty())  # not a git clone: nothing says that someone edits
        with mock.patch.object(server, "_code_loads", lambda: "SyntaxError: invalid syntax"):
            with self.assertRaisesRegex(RiverError, "does not load, so maxpm serve keeps the code it runs"):
                server.OPS["serve_restart"](self.c, {}, None)
        with self.assertRaisesRegex(RiverError, "another queue"):
            server.OPS["serve_restart"](self.c, {"db": "/somewhere/else.db"}, None)
        started = []
        with mock.patch.object(server, "_code_loads", lambda: None), \
                mock.patch.object(server, "_restart_soon", lambda why: started.append(why)):
            r = server.OPS["serve_restart"](self.c, {"db": str(core.db_path())}, None)
        self.assertEqual((r["restarting"], r["boot"], started), (True, server.BOOT, ["restart asked"]))
        self.assertEqual(server.OPS["serve_status"](self.c, {}, None), {"boot": server.BOOT, "stale": server.code_stale()})

    def test_river_serve_restart_waits_until_the_new_server_answers(self):
        from river import cli
        answers = [{"boot": "1", "stale": True}, RiverError("no maxpm serve answers"), RiverError("no maxpm serve answers"),
                   {"boot": "1", "stale": True}, {"boot": "2", "stale": False}]
        ops = []

        def ask(conn, op, args, **kw):
            ops.append(op)
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a
        with mock.patch.object(cli, "ask_server", ask):
            self.assertEqual(cli.serve_restart(self.c, sleep=lambda s: None), {"restarted": True, "stale": False})
        self.assertEqual(ops, ["serve_restart", "serve_status", "serve_status", "serve_status", "serve_status"])
        # No answer in time: the command says so and fails.
        with mock.patch.object(cli, "ask_server", lambda conn, op, args, **kw: {"boot": "1", "stale": True}):
            self.assertEqual(cli.serve_restart(self.c, wait=3, sleep=lambda s: None), {"restarted": False, "stale": True})
            out = io.StringIO()
            with contextlib.redirect_stdout(out), mock.patch.object(cli, "serve_restart", lambda conn: {"restarted": False, "stale": True}):
                self.assertEqual(cli.run(["serve", "--restart"]), 1)
            self.assertIn("did not answer again", out.getvalue())
            with contextlib.redirect_stdout(out), mock.patch.object(cli, "serve_restart", lambda conn: {"restarted": True, "stale": False}):
                self.assertEqual(cli.run(["serve", "--restart"]), 0)
            self.assertIn("maxpm serve started again and answers on port", out.getvalue())

    def test_a_command_reaches_a_server_that_starts_again_just_now(self):
        import urllib.error
        import urllib.request
        from river import cli
        tries = []

        class Answer(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class Opener:
            def open(self, req, timeout=None):
                tries.append(req.full_url)
                if len(tries) < 3:
                    raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
                return Answer(b'{"result": {"ok": true}}')
        with mock.patch.object(urllib.request, "build_opener", lambda *a: Opener()), mock.patch.object(cli, "RETRY_WAIT", 0):
            self.assertEqual(cli.ask_server(self.c, "serve_status", {}), {"ok": True})
            self.assertEqual(len(tries), 3)
            tries.clear()
            with self.assertRaisesRegex(RiverError, "no maxpm serve answers"):
                cli.ask_server(self.c, "serve_status", {}, retries=1)
            self.assertEqual(len(tries), 2)


class ServeLoop(unittest.TestCase):
    """The loop of maxpm serve keeps running, says when it is late, and reloads beside files that are not code
    (#1663: it ended on a queue that did not open, and nothing said so for 3 hours)."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["MAXPM_DB"] = os.path.join(self.dir.name, "t.db")
        self.c = core.connect()

    def tearDown(self):
        self.c.close()
        os.environ.pop("MAXPM_DB", None)
        self.dir.cleanup()

    def test_a_queue_that_does_not_open_fails_one_pass_and_not_the_loop(self):
        import sqlite3
        import threading
        from river import notify

        class Stop(threading.Event):
            def wait(self, timeout=None):  # no sleep between the passes
                return self.is_set()
        stop, real, tries, between = Stop(), core.connect, [], []

        def connect(*a, **k):
            tries.append(1)
            if len(tries) == 1:
                raise sqlite3.OperationalError("database is locked")
            return real(*a, **k)

        def look(c):
            between.append(core.serve_loop(self.c))
            stop.set()
        said = io.StringIO()
        with mock.patch.object(core, "connect", connect), mock.patch.object(server, "watch_prompts", look), \
                contextlib.redirect_stdout(said):
            notify.loop(stop, interval_s=1)
        self.assertEqual(len(tries), 2)  # the second pass came
        self.assertIn("maxpm notify: database is locked", said.getvalue())
        # The pass that failed is on record, and the next complete pass clears it.
        self.assertEqual((between[0]["error"], between[0]["last_pass"], between[0]["late"]),
                         ("OperationalError: database is locked", None, False))
        now = core.serve_loop(self.c)
        self.assertEqual((now["error"], now["late"], now["pid"]), (None, False, os.getpid()))
        self.assertIsNotNone(now["last_pass"])
        # A terminal that is gone does not end the loop either.
        stop.clear()
        with mock.patch.object(core, "connect", mock.Mock(side_effect=[sqlite3.OperationalError("locked"), real()])), \
                mock.patch.object(notify, "print", mock.Mock(side_effect=BrokenPipeError()), create=True), \
                mock.patch.object(server, "watch_prompts", lambda c: stop.set()):
            notify.loop(stop, interval_s=1)
        self.assertIsNone(core.serve_loop(self.c)["error"])

    def test_the_page_and_the_manager_see_a_loop_that_is_late(self):
        from river import cli

        def state():
            h = server.Handler.__new__(server.Handler)
            h.path, h.client_address, h.headers, out = "/api/state", ("127.0.0.1", 5555), {"Host": "127.0.0.1:8765"}, {}
            h._send = lambda code, body, ctype=None: out.update(body=body)
            h.do_GET()
            return out["body"]["loop"]
        ago = lambda **k: core.iso(core.now() - core.timedelta(**k))
        self.assertIsNone(state())  # no maxpm serve ran on this queue
        self.assertEqual(core.manager_findings(self.c)["serve_loop"], [])
        beat = lambda t, error=None: core.serve_loop_beat(
            self.c, {"started": ago(hours=5), "last_pass": t, "last_try": ago(minutes=1), "error": error})
        beat(ago(minutes=2))
        self.assertEqual((state()["late"], core.manager_findings(self.c)["serve_loop"]), (False, []))
        # No complete pass for 3 hours: the thread ended, a pass hangs, or every pass fails.
        beat(ago(hours=3), "OperationalError: database is locked")
        with mock.patch.object(core, "proc_info", lambda pid: (1, "python3", "python3 /x/bin/maxpm serve")):
            self.assertEqual((state()["late"], state()["age"]), (True, "3h00m"))
            f = core.manager_findings(self.c)
            self.assertEqual(core._finding_keys(f), ["serve_loop"])  # a new finding: it wakes the manager
            line = "\n".join(cli._findings_lines(f, "maxpm --as boss"))
            self.assertRegex(line, r"SERVE LOOP LATE: maxpm serve made no complete pass for 3h00m \(last error: "
                                   r"OperationalError: database is locked\).*maxpm serve --restart")
            # A slow loop is not late: notify_interval 10m gives it 10 passes.
            beat(ago(minutes=30))
            self.assertTrue(state()["late"])
            core.config_set(self.c, "notify_interval", "10m")
            self.assertFalse(state()["late"])
            core.config_unset(self.c, "notify_interval")
            beat(ago(minutes=2))  # the next complete pass ends the finding
            self.assertEqual(core.manager_findings(self.c)["serve_loop"], [])
        # The file of a serve that is gone says nothing: its process ended, or another program has its id now.
        beat(ago(hours=3))
        with mock.patch.object(core, "proc_info", lambda pid: (1, "vim", "vim notes.txt")):
            self.assertIsNone(state())
        with mock.patch.object(core, "pid_alive", lambda pid: False):
            self.assertIsNone(state())
        # The page has a button for it.
        js = (Path(server.STATIC) / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="loopLate"', (Path(server.STATIC) / "index.html").read_text(encoding="utf-8"))
        self.assertRegex(js, r"S\.loop")

    def test_serve_starts_the_loop_thread_again_when_it_ended(self):
        import threading
        from river import notify
        ran, stop = [], threading.Event()
        self.addCleanup(server.LOOP_THREAD.update, {"thread": None, "stop": None, "at": 0.0})
        self.assertFalse(server.keep_loop())  # no maxpm serve here (a test server, maxpm notify run)
        said = io.StringIO()
        with mock.patch.object(notify, "loop", lambda s: ran.append(1)), contextlib.redirect_stdout(said):
            server.LOOP_THREAD.update(thread=None, stop=stop)
            self.assertTrue(server.keep_loop())
            server.LOOP_THREAD["thread"].join(5)
            self.assertEqual((server.LOOP_THREAD["thread"].name, ran), ("maxpm-notify", [1]))
            self.assertFalse(server.keep_loop())  # it ended a moment ago: not twice a second
            server.LOOP_THREAD["at"] -= server.LOOP_RESTART_S
            self.assertTrue(server.keep_loop())
            server.LOOP_THREAD["thread"].join(5)
            self.assertEqual(ran, [1, 1])
            self.assertEqual(said.getvalue().count("the loop thread ended; it starts again"), 1)
            # A thread that runs stays; and after serve stops nothing starts.
            hold = threading.Event()
            with mock.patch.object(notify, "loop", lambda s: hold.wait(5)):
                server.LOOP_THREAD["at"] -= server.LOOP_RESTART_S
                self.assertTrue(server.keep_loop())
                server.LOOP_THREAD["at"] -= server.LOOP_RESTART_S
                self.assertFalse(server.keep_loop())
                hold.set()
                server.LOOP_THREAD["thread"].join(5)
            stop.set()
            server.LOOP_THREAD["at"] -= server.LOOP_RESTART_S
            self.assertFalse(server.keep_loop())
        # The server asks on every turn of serve_forever, and an error there does not stop the page.
        with mock.patch.object(server, "keep_loop", mock.Mock(side_effect=RuntimeError("can't start new thread"))), \
                contextlib.redirect_stdout(said):
            server._Server.service_actions(None)
        self.assertIn("the loop thread did not start: can't start new thread", said.getvalue())

    def test_a_file_that_is_not_code_does_not_hold_the_reload(self):
        repo = Path(self.dir.name, "clone")
        (repo / "river").mkdir(parents=True)
        git = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                        check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        (repo / "river" / "core.py").write_text("x = 1\n")
        git("add", "."), git("commit", "-q", "-m", "one")
        with mock.patch.object(server, "REPO", repo), mock.patch.object(server, "PKG", repo / "river"):
            self.assertFalse(server._code_dirty())
            # The Finder leaves a .DS_Store in the folder, also in a folder below; git does not track it.
            (repo / "river" / ".DS_Store").write_bytes(b"\0")
            (repo / "river" / "static").mkdir()
            (repo / "river" / "static" / ".DS_Store").write_bytes(b"\0")
            (repo / "river" / "notes.txt").write_text("a note\n")
            self.assertFalse(server._code_dirty())
            # New code that is not committed, and a change of a file git tracks, still hold it.
            (repo / "river" / "static" / "new.py").write_text("y = 2\n")
            self.assertTrue(server._code_dirty())
            (repo / "river" / "static" / "new.py").unlink()
            (repo / "river" / "core.py").write_text("x = 2\n")
            self.assertTrue(server._code_dirty())
            git("commit", "-q", "-am", "two")
            self.assertFalse(server._code_dirty())
        # And this clone ignores the file.
        self.assertIn(".DS_Store", (Path(server.REPO) / ".gitignore").read_text().split())


class PageUpdate(unittest.TestCase):
    """The Update button: status of the river clone against its upstream, and a fast-forward that restarts."""

    def setUp(self):
        import subprocess
        self.dir = tempfile.TemporaryDirectory()
        d = self.dir.name
        self.git = lambda repo, *a: subprocess.run(["git", "-C", repo, *a], check=True, capture_output=True, text=True).stdout.strip()
        self.up, self.clone = os.path.join(d, "up"), os.path.join(d, "clone")
        subprocess.run(["git", "init", "-q", "-b", "main", self.up], check=True)
        self.commit(self.up, "one")
        subprocess.run(["git", "clone", "-q", self.up, self.clone], check=True)

    def tearDown(self):
        self.dir.cleanup()

    def commit(self, repo, msg):
        with open(os.path.join(repo, msg), "w") as f:
            f.write(msg)
        self.git(repo, "add", msg)
        self.git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg)

    def test_behind_then_update_fast_forwards_and_restarts(self):
        self.assertEqual(server.update_status(self.clone)["behind"], 0)
        restarts = []
        self.assertFalse(server.update_apply(self.clone, lambda: restarts.append(1))["updated"])
        self.commit(self.up, "two")
        st = server.update_status(self.clone)
        self.assertEqual((st["behind"], len(st["commits"])), (1, 1))
        res = server.update_apply(self.clone, lambda: restarts.append(1))
        self.assertTrue(res["updated"])
        self.assertEqual(res["head"], self.git(self.up, "rev-parse", "--short", "HEAD"))
        self.assertEqual((res["behind"], restarts), (0, [1]))

    def test_refuses_a_clone_with_its_own_commits(self):
        self.commit(self.up, "two")
        self.commit(self.clone, "mine")
        with self.assertRaises(RiverError):
            server.update_apply(self.clone, lambda: self.fail("restarted"))

    def test_local_changes_in_the_way_say_so(self):
        self.commit(self.up, "two")
        with open(os.path.join(self.clone, "two"), "w") as f:
            f.write("mine, not committed")
        with self.assertRaises(RiverError) as e:
            server.update_apply(self.clone, lambda: self.fail("restarted"))
        self.assertIn("commit or stash", str(e.exception))

    def test_stale_when_the_code_changes_after_start(self):
        self.assertFalse(server.code_stale())
        old = server.BOOT_CODE
        try:
            server.BOOT_CODE = "0"
            self.assertTrue(server.code_stale())
        finally:
            server.BOOT_CODE = old

    def test_not_a_git_clone(self):
        self.assertEqual(server.update_status(self.dir.name), {"git": False})
        with self.assertRaises(RiverError):
            server.update_apply(self.dir.name, lambda: None)


class StaticFiles(unittest.TestCase):
    """The page's .js/.css/vendor files come from river/static; nothing outside it is served."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        base = os.path.realpath(self.dir.name)
        self.root = os.path.join(base, "static")
        os.makedirs(os.path.join(self.root, "vendor", "tab"))
        for rel, body in (("app.js", "export const a = 1;"), ("app.css", "b{}"), ("vendor/tab/LICENSE", "MIT"),
                          ("vendor/tab/t.min.js.map", "{}"), (".hidden", "x")):
            with open(os.path.join(self.root, rel), "w") as f:
                f.write(body)
        with open(os.path.join(base, "secret.txt"), "w") as f:
            f.write("no")
        os.symlink(os.path.join(base, "secret.txt"), os.path.join(self.root, "link.txt"))

    def tearDown(self):
        self.dir.cleanup()

    def test_types_and_refusals(self):
        from pathlib import Path
        get = lambda p: server.static_file(p, Path(self.root))
        self.assertEqual(get("/app.js"), (b"export const a = 1;", "text/javascript; charset=utf-8"))
        self.assertEqual(get("/app.css")[1], "text/css; charset=utf-8")
        self.assertEqual(get("/vendor/tab/LICENSE")[1], "text/plain; charset=utf-8")
        self.assertEqual(get("/vendor/tab/t.min.js.map")[1], "application/json")
        for bad in ("/../secret.txt", "/vendor/../../secret.txt", "/%2e%2e/secret.txt", "/vendor%2ftab%2fLICENSE",
                    "/link.txt", "/.hidden", "/vendor", "/", "/nope.js"):
            self.assertIsNone(get(bad), bad)

    def test_handler_serves_the_real_static_folder(self):
        h = server.Handler.__new__(server.Handler)
        h.client_address, h.headers, out = ("127.0.0.1", 5555), {"Host": "localhost:8765"}, {}
        h._send = lambda code, body, ctype=None: out.update(code=code, ctype=ctype)
        h.path = "/index.html"
        h.do_GET()
        self.assertEqual((out["code"], out["ctype"]), (200, "text/html; charset=utf-8"))
        h.path = "/../server.py"
        h.do_GET()
        self.assertEqual(out["code"], 404)


def _chrome():
    """A Chrome (or Chromium, or Edge) on this computer, or None. MAXPM_CHROME names one."""
    import shutil
    for c in (os.environ.get("MAXPM_CHROME"), "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              "/Applications/Chromium.app/Contents/MacOS/Chromium",
              "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
              *(shutil.which(n) for n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"))):
        if c and os.path.exists(c):
            return c
    return None


class OnboardingForm(unittest.TestCase):
    """The setup guide of the page (the first run): each field works with the Enter key, as its button does."""

    DRIVER = """
const post = (o) => navigator.sendBeacon("/__result", JSON.stringify(o));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const until = async (f, what) => { for (let i = 0; i < 150; i++) { const v = f(); if (v) return v; await sleep(100); } throw new Error("never: " + what); };
const q = (s) => document.querySelector(s);
// What a person does: type in the field, then press Enter.
const enter = async (sel, text) => { const f = await until(() => q(sel), sel); f.focus(); f.value = text;
  f.dispatchEvent(new Event("input", { bubbles: true }));
  f.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true })); };
try {
  await until(() => !q("#setup").classList.contains("hidden") && q("#suName"), "the guide opens with the name step");
  await enter("#suName", "mark");
  await until(() => !q("#suName"), "the name step is done");
  await enter('#setupSteps [data-ff="path"]', FOLDER);
  await until(() => q("#suTaskTitle"), "the folder step is done, and the task step has its field");
  await enter("#suTaskTitle", "add a README");
  await until(() => !q("#suTaskTitle"), "the task step is done");
  q("#setupSteps details.more").open = true;
  await enter("[data-tracker-for]", "github owner/repo via gh");
  await until(() => !q("[data-tracker-for]"), "the tracker is saved");
  // Start an agent: Cancel in its dialog, and the next click opens the dialog again at once.
  const open = () => !q("#launchDlg").classList.contains("hidden");
  const cancel = () => [...document.querySelectorAll("#launchDlg button")].find((b) => b.textContent.trim() === "Cancel").click();
  q("#setupSteps [data-launch]").click(); await until(open, "the launch dialog opens");
  cancel(); await until(() => !open(), "the launch dialog closes");
  q("#setupSteps [data-launch]").click(); await until(open, "the launch dialog opens again at once");
  cancel();
  const st = await (await fetch("/api/setup")).json(), state = await (await fetch("/api/state")).json();
  post({ people: st.people, projects: st.folders.map((f) => f.projects), items: state.items.map((i) => [i.title, i.doer]), actor: q("#actor").value });
} catch (e) { post({ error: String(e && e.stack || e) }); }
"""

    def test_the_fields_have_an_enter_key(self):
        app = (server.STATIC / "app.js").read_text()
        form = (server.STATIC / "components" / "folderForm.js").read_text()
        self.assertRegex(app, r'\$\("#setupSteps"\)\.addEventListener\("keydown"')
        for field in ('f.id === "suName"', 'f.id === "suTaskTitle"', "f.dataset.trackerFor"):
            self.assertIn(field, app)
        self.assertRegex(form, r'document\.addEventListener\("keydown"')

    @unittest.skipUnless(_chrome(), "no Chrome on this computer (MAXPM_CHROME names one)")
    def test_each_step_works_with_the_enter_key_in_a_browser(self):
        import threading
        got, result = threading.Event(), {}
        with tempfile.TemporaryDirectory() as d:
            folder = os.path.join(os.path.realpath(d), "proj")
            os.mkdir(folder)
            driver = self.DRIVER.replace("FOLDER", json.dumps(folder)).encode()
            page = (server.STATIC / "index.html").read_bytes().replace(
                b"</body>", b'<script type="module" src="/__drive.js"></script></body>')

            class H(server.Handler):
                def do_GET(self):
                    if self.path == "/":
                        return self._send(200, page, "text/html; charset=utf-8")
                    if self.path == "/__drive.js":
                        return self._send(200, driver, "text/javascript; charset=utf-8")
                    return super().do_GET()

                def do_POST(self):
                    if self.path == "/__result":
                        result.update(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                        got.set()
                        return self._send(204, b"")
                    return super().do_POST()

            os.environ["MAXPM_DB"] = os.path.join(d, "q.db")
            self.addCleanup(os.environ.pop, "MAXPM_DB", None)
            # The Start button shows only when an agent program is installed, and a CI runner has none.
            found = mock.patch.object(server, "_login_shell_which", lambda names: {n: "/bin/" + n for n in names})
            found.start()
            self.addCleanup(found.stop)
            try:
                httpd = server._Server(("127.0.0.1", 0), H)
            except PermissionError:
                self.skipTest("no local port here (a sandbox)")
            httpd.daemon_threads = True
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            chrome = subprocess.Popen(
                [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                 "--disable-extensions", "--window-size=1280,900", f"--user-data-dir={os.path.join(d, 'profile')}",
                 f"http://127.0.0.1:{httpd.server_address[1]}/"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                ran = got.wait(90)
            finally:
                chrome.kill()
                chrome.wait()
                httpd.shutdown()
                httpd.server_close()
            if not ran:
                self.skipTest("Chrome did not run the page here")
            self.assertNotIn("error", result, result.get("error"))
            self.assertEqual((result["people"], result["projects"], result["items"], result["actor"]),
                             (["mark"], [["proj"]], [["add a README", "ai"]], "mark"))
            c = core.connect()
            try:
                self.assertEqual([r["value"] for r in c.execute("SELECT value FROM settings WHERE key='tracker'")],
                                 ["github owner/repo via gh"])
            finally:
                c.close()
            self.assertIn("MaximizePM", Path(folder, "AGENTS.md").read_text())  # the folder got its block


class TerminalDialogLayout(unittest.TestCase):
    """The Terminal dialog has its size from the browser window. The columns and rows of the tmux pane do not
    change it: the pane's text scrolls inside the dialog, and the view ends at the prompt."""

    # Each screen is one answer of /api/terminal: [name, columns, rows, rows with text, the cursor's row].
    SCREENS = [["narrow", 40, 10, 10, 9], ["wide", 400, 10, 10, 9], ["tall", 80, 300, 300, 299],
               ["small", 20, 3, 3, 2], ["tall, text at the top", 80, 300, 5, 4]]
    PAGE = """<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/app.css">
<input id="actor" value="mark"><div id="toast"></div>
<script type="module">
const post = (o) => navigator.sendBeacon("/result", JSON.stringify(o));
try {
  const real = window.fetch;
  window.fetch = (u, o) => (String(u).replace(/^[/]/, "").startsWith("api/") ? new Promise(() => {}) : real(u, o));
  const t = await import("/components/terminalDialog.js");
  t.openTerminal("worker-1");
  const box = document.querySelector("#termDlg .box"), scr = document.querySelector("#termScreen");
  const row = (i, cols) => (String(i) + " " + "x".repeat(cols)).slice(0, cols);
  const screen = ([name, cols, rows, used, cursor], mark = "") => ({ name, pane: "%1", width: cols, height: rows, cursor: [0, cursor],
    text: Array.from({ length: rows }, (_, i) => (i < used - 1 ? mark + row(i, cols) : i === used - 1 ? "PROMPT>" : "")).join("\\n") + "\\n" });
  const measure = (name) => { const b = box.getBoundingClientRect(), s = scr.getBoundingClientRect(); return { name,
    box: [b.width, b.height], screen: [s.width, s.height], window: [innerWidth, innerHeight], inWindow: b.left >= 0 && b.top >= 0 && b.right <= innerWidth && b.bottom <= innerHeight,
    wider: scr.scrollWidth - scr.clientWidth, taller: scr.scrollHeight - scr.clientHeight, toEnd: scr.scrollHeight - scr.clientHeight - scr.scrollTop,
    rows: scr.textContent.split("\\n").length, last: scr.textContent.split("\\n").pop() }; };
  const out = [measure("empty")];
  for (const s of SCREENS) { t.showScreen(screen(s)); out.push(measure(s[0])); }
  // The person scrolls up in a tall pane: the next screen stays there. A key shows the end again.
  t.showScreen(screen(SCREENS[2])); scr.scrollTop = 0; t.showScreen(screen(SCREENS[2], "new ")); out.push(measure("scrolled up"));
  scr.dispatchEvent(new KeyboardEvent("keydown", { key: "a" })); out.push(measure("after a key"));
  post({ out, used: [t.usedRows("a\\nb\\n\\u001b[0m  \\n\\n"), t.usedRows("a\\n\\n\\n\\n", 2)] });
} catch (e) { post({ error: String(e && e.stack || e) }); }
</script>"""

    def test_the_styles_take_no_size_from_the_text(self):
        css = (server.STATIC / "app.css").read_text()
        rule = lambda sel: re.search(re.escape(sel) + r" \{([^}]*)\}", css).group(1)
        box, term = rule("#termDlg .box"), rule("#termDlg .term")
        self.assertRegex(box, r"(?<![-\w])width: min\(\d+px, 100%\)")
        self.assertIn("height: 100%", box)
        for want in ("flex: 1 1 0", "min-width: 0", "min-height: 0", "overflow: auto", "white-space: pre;", "font: 12px/"):
            self.assertIn(want, term)
        for sized_by_text in ("max-height", "ch,", "em;"):
            self.assertNotIn(sized_by_text, term)
        js = (server.STATIC / "components" / "terminalDialog.js").read_text()
        self.assertNotRegex(js, r"style\.(width|height|fontSize)|resize-pane|resize-window")

    @unittest.skipUnless(_chrome(), "no Chrome on this computer (MAXPM_CHROME names one)")
    def test_the_dialog_keeps_its_size_for_each_pane_size(self):
        import subprocess
        import threading
        from http.server import BaseHTTPRequestHandler
        got, result = threading.Event(), {}
        page = self.PAGE.replace("SCREENS", json.dumps(self.SCREENS)).encode()

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self, code, body=b"", ctype="text/plain"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                found = (page, "text/html; charset=utf-8") if self.path == "/" else server.static_file(self.path)
                self._answer(200, *found) if found else self._answer(404)

            def do_POST(self):
                result.update(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self._answer(204)
                got.set()

        try:
            httpd = server._Server(("127.0.0.1", 0), H)
        except PermissionError:
            self.skipTest("no local port here (a sandbox)")
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        with tempfile.TemporaryDirectory() as profile:
            chrome = subprocess.Popen(
                [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                 "--disable-extensions", "--window-size=1000,700", f"--user-data-dir={profile}",
                 f"http://127.0.0.1:{httpd.server_address[1]}/"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                ran = got.wait(60)
            finally:
                chrome.kill()
                chrome.wait()
                httpd.shutdown()
                httpd.server_close()
        if not ran:
            self.skipTest("Chrome did not run the page here")
        self.assertNotIn("error", result, result.get("error"))
        self.assertEqual(result["used"], ["a\nb", "a\n\n"])
        by = {m["name"]: m for m in result["out"]}
        first = by["empty"]
        self.assertTrue(first["inWindow"], first)
        self.assertGreater(first["screen"][1], 300, first)  # the screen fills the box
        for name, m in by.items():
            self.assertEqual((m["box"], m["screen"]), (first["box"], first["screen"]), name)
        # Text wider or taller than the screen scrolls inside it; lines do not wrap.
        self.assertEqual((by["narrow"]["wider"], by["narrow"]["taller"], by["narrow"]["rows"]), (0, 0, 10))
        self.assertGreater(by["wide"]["wider"], 1000)
        self.assertEqual((by["wide"]["taller"], by["wide"]["rows"]), (0, 10))
        # The end of a tall pane (the prompt) is in view; empty rows under the prompt do not push it out.
        self.assertGreater(by["tall"]["taller"], 1000)
        for name in ("tall", "after a key"):
            self.assertLess(by[name]["toEnd"], 1, name)
            self.assertEqual(by[name]["last"], "PROMPT>")
        self.assertEqual((by["tall, text at the top"]["taller"], by["tall, text at the top"]["rows"]), (0, 5))
        self.assertGreater(by["scrolled up"]["toEnd"], 1000)


class ServerBind(unittest.TestCase):
    def test_the_server_starts_without_a_dns_lookup_of_its_name(self):
        # HTTPServer asks DNS for the full host name; on a Mac with a slow network that hung for minutes.
        import socket
        from unittest import mock
        with mock.patch.object(socket, "getfqdn", side_effect=AssertionError("getfqdn called")):
            httpd = server._Server(("127.0.0.1", 0), server.Handler)
        try:
            self.assertEqual(httpd.server_name, "127.0.0.1")
            self.assertGreater(httpd.server_port, 0)
        finally:
            httpd.server_close()


def big_queue(n, open_items, projects=17, seed=696):
    """The items of a queue as the page gets them (the fields the graph reads): the first ones finished,
    the last open_items open, each waiting on up to two earlier items."""
    rnd = random.Random(seed)
    items = []
    for k in range(1, n + 1):
        waits = sorted(rnd.sample(range(max(1, k - 60), k), min(k - 1, rnd.choice((0, 0, 1, 1, 1, 2, 2)))))
        items.append({"id": k, "title": f"Check the import of orders for the reports page, part {k}",
                      "project": f"project-{rnd.randrange(projects):02d}", "doer": "human" if k % 23 == 0 else "any",
                      "status": "done" if k <= n - open_items else "open", "waits_on": waits, "unblocks": []})
    for i in items:
        for w in i["waits_on"]:
            items[w - 1]["unblocks"].append(i["id"])
        i["ready"] = i["status"] == "open" and all(items[w - 1]["status"] == "done" for w in i["waits_on"])
    return {"projects": [{"id": n, "name": f"project-{n:02d}"} for n in range(projects)], "items": items}


@unittest.skipUnless(shutil.which("node"), "node is not on this machine")
class GraphOfALargeQueue(unittest.TestCase):
    """The Graph tab draws a queue of 700 items, most of them finished (#696). Mermaid's own limits are 50,000
    characters and 500 edges; above them it draws an error picture. Node runs the page's own component
    (components/graphText.js) here; the drawing itself needs a browser (seed/large.py loads a queue for that)."""

    STATIC = Path(server.__file__).parent / "static"

    def graph(self, queue, project=None, done=True):
        code = (self.STATIC / "components" / "graphText.js").read_text(encoding="utf-8") + f"""
const S = {json.dumps(queue)}, proj = {json.dumps(project)}, fin = (i) => i.status === "done";
const items = S.items.filter(i => {json.dumps(done)} || !fin(i));
const {{ show, total, hidden }} = graphItems(items, proj ? items.filter(i => i.project === proj) : null);
const g = graphText(S.projects, items, show);
console.log(JSON.stringify({{ shown: [...show], total, hidden, chars: g.text.length, nodes: g.nodes, edges: g.edges,
  lines: g.text.split("\\n").length, limits: GRAPH_LIMITS, max: GRAPH_MAX }}));
"""
        r = subprocess.run(["node", "--input-type=module"], input=code, capture_output=True, encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_the_page_reads_fields_that_the_state_has(self):
        with tempfile.TemporaryDirectory() as d:
            c = core.connect(os.path.join(d, "t.db"))
            core.project_add(c, "a")
            core.item_add(c, "a", "one")
            st = core.state(c)
            c.close()
        self.assertLessEqual(set(big_queue(1, 1)["items"][0]), set(st["items"][0]))
        self.assertLessEqual({"id", "name"}, set(st["projects"][0]))

    def test_700_items_with_the_finished_ones_are_all_drawn_inside_the_limits(self):
        q = big_queue(700, 70)
        links = sum(len(i["waits_on"]) for i in q["items"])
        g = self.graph(q)
        self.assertEqual((g["nodes"], g["edges"], g["hidden"], g["total"]), (700, links, 0, 700))
        # More than Mermaid allows by default, so the page must raise both limits...
        self.assertGreater(g["chars"], 50000)
        self.assertGreater(g["edges"], 500)
        # ...and it does, with room: mermaid.initialize gets the limits of the component.
        self.assertLess(g["chars"] * 5, g["limits"]["maxTextSize"])
        self.assertLess(g["edges"] * 5, g["limits"]["maxEdges"])
        self.assertRegex((self.STATIC / "app.js").read_text(encoding="utf-8"), r"mermaid\.initialize\(\{[^;]*\.\.\.GRAPH_LIMITS \}\)")
        # Without the finished items: the open ones only.
        g = self.graph(q, done=False)
        self.assertEqual((g["nodes"], g["hidden"]), (70, 0))

    def test_above_the_item_limit_finished_items_away_from_open_work_are_left_out(self):
        q = big_queue(2000, 120)
        g = self.graph(q)
        by = {i["id"]: i for i in q["items"]}
        is_open = lambda x: by[x]["status"] == "open"
        self.assertGreater(2000, g["max"])
        self.assertEqual(g["total"], 2000)
        self.assertEqual(g["hidden"], 2000 - len(g["shown"]))
        self.assertGreater(g["hidden"], 1000)
        self.assertLessEqual({i for i in by if is_open(i)}, set(g["shown"]))  # every open item
        for x in g["shown"]:  # and of the finished ones, those next to an open item
            self.assertTrue(is_open(x) or any(is_open(y) for y in by[x]["waits_on"] + by[x]["unblocks"]), x)
        self.assertEqual(g["nodes"], len(g["shown"]))
        # At the limit itself nothing is left out.
        g = self.graph(big_queue(g["max"], 100))
        self.assertEqual((g["hidden"], g["nodes"]), (0, g["max"]))

    def test_a_project_shows_its_items_and_their_neighbors(self):
        q = big_queue(700, 70)
        mine = [i for i in q["items"] if i["project"] == "project-03"]
        want = {x for i in mine for x in [i["id"], *i["waits_on"], *i["unblocks"]]}
        g = self.graph(q, project="project-03")
        self.assertEqual((set(g["shown"]), g["hidden"]), (want, 0))
