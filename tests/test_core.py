import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from datetime import timedelta

from river import core
from river.core import RiverError


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        # The folder goes last, after the cleanups a test adds (a second connection): Windows cannot
        # delete a file that is open.
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "t.db")
        self.c = core.connect(self.path)

    def tearDown(self):
        self.c.close()

    def add(self, project, title, p=2, after=(), doer="any"):
        return core.item_add(self.c, project, title, p, "", doer, after, "t")["id"]


class Ordering(Base):
    def test_priority_inherits_from_dependents(self):
        core.project_add(self.c, "a")
        low = self.add("a", "low prereq", 4)
        mid = self.add("a", "mid", 2)
        top = self.add("a", "top", 0, after=[low])
        ann = core.annotate(self.c)
        self.assertEqual(ann[low]["effective_priority"], 0)
        self.assertEqual(ann[low]["priority_from"], top)
        ready = [a["id"] for a in core.ready_list(self.c)]
        self.assertEqual(ready, [low, mid])  # top waits on low

    def test_project_rank_breaks_ties(self):
        core.project_add(self.c, "a")
        core.project_add(self.c, "b")
        ia = self.add("a", "x")
        ib = self.add("b", "y")
        self.assertEqual([a["id"] for a in core.ready_list(self.c)], [ia, ib])
        core.project_rank(self.c, "b", 1)
        self.assertEqual([a["id"] for a in core.ready_list(self.c)], [ib, ia])

    def test_unblock_count_breaks_ties(self):
        core.project_add(self.c, "a")
        lone = self.add("a", "lone")
        hub = self.add("a", "hub")
        self.add("a", "d1", after=[hub])
        self.add("a", "d2", after=[hub])
        self.assertEqual(core.ready_list(self.c)[0]["id"], hub)
        self.assertNotEqual(lone, hub)

    def test_cycle_refused(self):
        core.project_add(self.c, "a")
        x = self.add("a", "x")
        y = self.add("a", "y", after=[x])
        z = self.add("a", "z", after=[y])
        with self.assertRaises(RiverError):
            core.dep_add(self.c, x, [z])

    def test_areas(self):
        core.project_add(self.c, "a")
        core.project_add(self.c, "b")
        a1 = self.add("a", "a1", 0)
        b1 = self.add("b", "b1", 3)
        b2 = self.add("b", "b2", 3, after=[b1])
        far = self.add("b", "far", 1)
        self.assertEqual(core.ready_list(self.c, project="b")[0]["id"], far)
        self.assertEqual([a["id"] for a in core.ready_list(self.c, project="a,b")][0], a1)
        self.assertEqual([a["id"] for a in core.ready_list(self.c, unblocks=b2)], [b1])
        near = [a["id"] for a in core.ready_list(self.c, near=str(b2))]
        self.assertEqual(near, [b1])  # far and a1 are not linked to b2

    def test_outside_blocker(self):
        core.project_add(self.c, "a")
        x = self.add("a", "x")
        core.block(self.c, x, "waiting on Stripe")
        self.assertEqual(core.ready_list(self.c), [])
        core.unblock(self.c, x)
        self.assertEqual(len(core.ready_list(self.c)), 1)

    def test_timed_blocker_ends_by_itself(self):
        core.project_add(self.c, "a")
        core.register(self.c, "sam", human=True)
        core.register(self.c, "ag")
        x = self.add("a", "file the EIN form", doer="human")
        y = self.add("a", "agent step")
        core.block(self.c, x, "IRS form closed", "ag", until="2m")
        core.block(self.c, y, None, "ag", until="3h")
        a = core.annotate(self.c)
        self.assertFalse(a[x]["ready"])
        self.assertTrue(a[x]["blocked_text"].startswith("blocked until "))
        self.assertEqual(a[y]["blocked_reason"], "waiting for a set time")
        self.assertEqual(core.needs_you(self.c), [])
        with self.assertRaises(RiverError):
            core.block(self.c, x, "x", "ag", until="2020-01-01T00:00Z")
        with self.assertRaises(RiverError):
            core.block(self.c, x, None, "ag")
        # Time passes: the sweep that every command runs clears both blockers.
        past = core.iso(core.now() - timedelta(seconds=1))
        self.c.execute("UPDATE items SET blocked_until=?", (past,))
        core.activity(self.c, "ag")
        a = core.annotate(self.c)
        self.assertTrue(a[x]["ready"] and a[y]["ready"])
        self.assertIsNone(a[x]["blocked_until"])
        self.assertEqual([n["item_id"] for n in core.needs_you(self.c)], [x])
        notes = self.c.execute("SELECT to_agent, body FROM messages WHERE kind='notice'").fetchall()
        self.assertEqual([(n["to_agent"], n["body"].split(" ended")[0]) for n in notes],
                         [("ag", f"the wait on #{y} agent step")])

    def test_zones_without_a_time_zone_database(self):
        # Windows has no zone database unless tzdata is installed: UTC still works, other names say how to fix it.
        from unittest import mock
        import zoneinfo
        def missing(name):
            raise zoneinfo.ZoneInfoNotFoundError(name)
        with mock.patch.object(zoneinfo, "ZoneInfo", missing), mock.patch.object(zoneinfo, "available_timezones", set):
            self.assertEqual(core.local_zone("UTC").utcoffset(None), timedelta(0))
            with self.assertRaisesRegex(RiverError, "pip install tzdata"):
                core.local_zone("America/New_York")
        with self.assertRaisesRegex(RiverError, "unknown time zone"):
            core.local_zone("Mars/Olympus")

    def test_parse_when(self):
        start = core.parse_iso("2026-09-27T15:00:00Z")  # a Sunday
        w = lambda s: core.iso(core.parse_when(s, start=start))
        self.assertEqual(w("2h"), "2026-09-27T17:00:00Z")
        self.assertEqual(w("mon 07:00 America/New_York"), "2026-09-28T11:00:00Z")
        self.assertEqual(w("Monday 7am America/New_York"), "2026-09-28T11:00:00Z")
        self.assertEqual(w("2026-09-28T07:00-04:00"), "2026-09-28T11:00:00Z")
        self.assertEqual(w("tomorrow 9 UTC"), "2026-09-28T09:00:00Z")
        self.assertEqual(w("sun 10:00 UTC"), "2026-10-04T10:00:00Z")  # today's has passed
        self.assertEqual(w("16:00 UTC"), "2026-09-27T16:00:00Z")
        for bad in ("soon", "25:00", "mon 07:00 Mars/Base"):
            with self.assertRaises(RiverError):
                w(bad)
        self.assertEqual(core.show_time("2026-09-28T11:00:00Z", "America/New_York")[-9:], " 7:00 EDT")


class Claims(Base):
    def setUp(self):
        super().setUp()
        core.project_add(self.c, "a")
        core.register(self.c, "ag1")
        core.register(self.c, "ag2")
        core.register(self.c, "sam", human=True)

    def test_claim_needs_ready(self):
        x = self.add("a", "x")
        y = self.add("a", "y", after=[x])
        with self.assertRaises(RiverError):
            core.claim(self.c, y, "ag1")

    def test_max_leases(self):
        self.add("a", "x")
        self.add("a", "y")
        core.next_item(self.c, claim=True, actor="ag1")
        with self.assertRaises(RiverError):
            core.next_item(self.c, claim=True, actor="ag1")
        core.config_set(self.c, "max_leases", "2", agent="ag1")
        self.assertEqual(len(core.next_item(self.c, claim=True, actor="ag1")), 1)

    def test_doer_filter(self):
        h = self.add("a", "bank", 0, doer="human")
        ai = self.add("a", "code", 3, doer="ai")
        self.assertEqual(core.next_item(self.c, actor="ag1")[0]["id"], ai)
        self.assertEqual(core.next_item(self.c, actor="sam")[0]["id"], h)

    def test_lease_expiry(self):
        x = self.add("a", "x")
        core.claim(self.c, x, "ag1")
        past = core.iso(core.now() - timedelta(minutes=1))
        self.c.execute("UPDATE items SET lease_expires_at=? WHERE id=?", (past, x))
        core.activity(self.c, None)
        self.assertEqual(core._item(self.c, x)["status"], "open")

    def test_done_reports_newly_ready(self):
        x = self.add("a", "x")
        y = self.add("a", "y", after=[x])
        core.claim(self.c, x, "ag1")
        self.assertEqual(core.done(self.c, x, "ok", "ag1")["now_ready"], [y])

    def test_parallel_claims_get_different_items(self):
        for i in range(6):
            self.add("a", f"i{i}")
        got, errs = [], []

        def worker(name):
            c = core.connect(self.path)
            core.register(c, name)
            try:
                got.extend(r["id"] for r in core.next_item(c, claim=True, actor=name))
            except RiverError as e:
                errs.append(e)
            finally:
                c.close()

        ts = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errs, [])
        self.assertEqual(len(got), 6)
        self.assertEqual(len(set(got)), 6)


class Areas(Base):
    def test_mine_uses_history(self):
        core.project_add(self.c, "a")
        core.project_add(self.c, "b")
        core.register(self.c, "ag")
        x = self.add("a", "x")
        linked = self.add("b", "linked", 3, after=[x])
        same = self.add("a", "same project", 3)
        other = self.add("b", "unrelated", 0)
        with self.assertRaises(RiverError):
            core.next_item(self.c, actor="ag", mine=True)
        core.claim(self.c, x, "ag")
        core.done(self.c, x, "ok", "ag")
        got = [a["id"] for a in core.next_item(self.c, actor="ag", mine=True, limit=5)]
        self.assertEqual(got, [linked, same])
        self.assertNotIn(other, got)

    def test_project_describe_and_show(self):
        core.project_add(self.c, "a", notes="old")
        core.register(self.c, "ag")
        x = self.add("a", "x")
        core.project_describe(self.c, "a", "Checkout pages in web/; know React")
        core.claim(self.c, x, "ag")
        p = core.project_show(self.c, "a")
        self.assertEqual(p["description"], "Checkout pages in web/; know React")
        self.assertEqual(p["working_now"], ["ag"])


class Go(Base):
    def setUp(self):
        super().setUp()
        self.web = os.path.join(self.dir.name, "web")
        self.api = os.path.join(self.dir.name, "api")
        os.makedirs(os.path.join(self.web, "src"))
        os.makedirs(self.api)
        core.project_add(self.c, "web", path=self.web)
        core.project_add(self.c, "api", path=self.api)

    def test_the_push_alert_of_the_item_that_go_gives_is_not_a_message_to_read(self):
        # #1605: the briefing said 'Messages for you: inbox: 1 unread, 1 alert', and the inbox was then empty.
        from river import cli
        core.register(self.c, "boss")
        core.register(self.c, "w1")
        x = self.add("web", "page")
        core.push(self.c, x, "w1", "start here", "boss")
        self.assertEqual(core.unread(self.c, "w1")["alerts"], 1)
        b = core.go(self.c, self.web, "w1")
        self.assertEqual((b["item"]["id"], b["messages"]["unread"], b["messages"]["alerts"]), (x, 0, 0))

        def text(brief):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.render_go(brief)
            return out.getvalue()
        self.assertNotIn("Messages for you", text(b))
        self.assertEqual(core.inbox(self.c, "w1"), [])
        # Another unread message still shows.
        core.release(self.c, x, actor="w1")
        core.send(self.c, "note", "read me", to="w1", actor="boss")
        b = core.go(self.c, self.web, "w1")
        self.assertEqual(b["messages"]["unread"], 1)
        self.assertIn("Messages for you: inbox: 1 unread", text(b))

    def test_the_briefing_has_the_rules_for_a_machine_limit_a_refused_command_and_a_look(self):
        # #1599 (research #1597): workers stood at their prompts for a full disk, a refused command, and the
        # word "push"; maxpm wait refuses while an item is held, so the wait for a limit is maxpm inbox --wait.
        import re
        from river import cli
        x, y = self.add("web", "page"), self.add("web", "form")

        def text(brief):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.render_go(brief)
            return re.sub(r"\s+", " ", out.getvalue())
        b = core.go(self.c, self.web)
        me, first = b["agent"], text(b)
        self.assertEqual(b["item"]["id"], x)
        self.assertIn(f"A limit of the machine (a full disk, a build stop, a push freeze, quiet time during a release "
                      f"gate) does not block the item: do the parts that do not need the limited thing, commit, keep "
                      f"#{x}, and wait for the manager's word that it ended: maxpm --as {me} inbox --wait", first)
        self.assertIn("A command is refused (the auto mode classifier, a permission prompt): do the parts you can. "
                      "Then it is a step for the user (below), with the exact command and the permission rule that "
                      "allows it. Stand at your prompt only for a release to production or a deletion of data", first)
        self.assertIn(f"The user should look at the finished result: push, maxpm --as {me} done {x}, then maxpm --as "
                      f"{me} add \"Look at <result>\" --doer human --found-during {x}. Ask before the push only for "
                      f"public text, a release to production, a step that deletes data or costs money, or when the "
                      f"item says so.", first)
        with self.assertRaisesRegex(RiverError, "finish or release it before you wait"):
            core.wait(self.c, self.web, me, step="0s", sleep=lambda s: None)
        # The short form of a later briefing keeps one line for each.
        core.done(self.c, x, "done", me)
        later = text(core.go(self.c, self.web, me))
        self.assertIn("Rules as before. Short form:", later)
        self.assertIn(f"a limit of the machine (disk, build stop, push freeze): keep the item, do the other parts, "
                      f"then maxpm --as {me} inbox --wait", later)
        self.assertIn("a refused command: a step for the user, with the command and the permission rule that allows it",
                      later)
        self.assertIn(f"the user should look at the result: push, done, then maxpm --as {me} add \"Look at ...\" "
                      f"--doer human --found-during {y}", later)

    def test_the_project_decides_the_look_at_a_finished_result(self):
        # #1668: one rule for all projects (push first, #1599) became a setting of each project.
        import re
        from river import cli
        x, y, z = self.add("web", "page"), self.add("web", "form"), self.add("api", "route")

        def text(brief):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.render_go(brief)
            return re.sub(r"\s+", " ", out.getvalue())

        def shown(name):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.render(cli.build_parser().parse_args(["project", "show", name]), core.project_show(self.c, name))
            return out.getvalue()
        with self.assertRaisesRegex(RiverError, "result_look is ask_first or push_first"):
            core.config_set(self.c, "result_look", "ask", project="web")
        core.config_set(self.c, "result_look", "ask_first", project="web")
        # ask_first: the worker commits, asks in the queue, and waits for the yes before the push.
        b = core.go(self.c, self.web)
        me, first = b["agent"], text(b)
        self.assertEqual((b["item"]["id"], b["result_look"]), (x, "ask_first"))
        self.assertIn(f"The user should look at the finished result: this project asks first (setting result_look). "
                      f"Commit, do not push, and maxpm --as {me} ask <person> \"Look at <result>: <where>. Push?\" "
                      f"--item {x} then wait at your prompt for the yes. MaximizePM keeps your lease while the question "
                      f"is open. After the yes: push, then maxpm --as {me} done {x}.", first)
        self.assertNotIn("--doer human --found-during", first)
        core.done(self.c, x, "done", me)
        later = text(core.go(self.c, self.web, me))
        self.assertIn(f"the user should look at the result: this project asks first: commit, no push, maxpm --as {me} "
                      f"ask <person> \"Look at ...\" --item {y}, wait for the yes, then push and done", later)
        self.assertEqual(core.project_show(self.c, "web")["result_look"], "ask_first")
        self.assertIn("  look at a finished result: ask first (the worker shows the result and waits for the yes "
                      "before the push); maxpm config set result_look push_first --project web\n", shown("web"))
        # push_first (the default): a project that sets nothing keeps the rule of #1599.
        b = core.go(self.c, self.api)
        other, first = b["agent"], text(b)
        self.assertEqual((b["item"]["id"], b["result_look"]), (z, "push_first"))
        self.assertIn(f"The user should look at the finished result: push, maxpm --as {other} done {z}, then maxpm "
                      f"--as {other} add \"Look at <result>\" --doer human --found-during {z}. Ask before the push only "
                      f"for public text", first)
        self.assertNotIn("asks first", first)
        self.assertEqual(core.project_show(self.c, "api")["result_look"], "push_first")
        self.assertIn("  look at a finished result: push, then ask (the worker pushes and adds an item for the look); "
                      "maxpm config set result_look ask_first --project api\n", shown("api"))

    def test_a_project_linked_to_another_folder_moves_only_with_move(self):
        other = os.path.join(self.dir.name, "other")
        os.makedirs(other)
        with self.assertRaises(RiverError) as e:
            core.project_path(self.c, "web", other)
        self.assertIn("--move", str(e.exception))
        self.assertEqual(core._project(self.c, "web")["path"], os.path.realpath(self.web))
        core.project_path(self.c, "web", other, move=True)
        self.assertEqual(core._project(self.c, "web")["path"], os.path.realpath(other))
        core.project_path(self.c, "api", self.api)  # the same folder again is fine

    def test_go_in_an_unlinked_folder_names_a_test_queue(self):
        lone = os.path.join(self.dir.name, "lone")
        os.makedirs(lone)
        old = os.environ.get("MAXPM_DB")
        os.environ["MAXPM_DB"] = self.path
        try:
            self.assertIn("QUEUE: ", core.queue_note())
            with self.assertRaises(RiverError) as e:
                core.go(self.c, lone)
            self.assertIn("MAXPM_DB", str(e.exception))
            self.assertIn("Ask the user", str(e.exception))
        finally:
            if old is None:
                del os.environ["MAXPM_DB"]
            else:
                os.environ["MAXPM_DB"] = old

    def test_worker_from_folder_and_resume(self):
        x = self.add("web", "x", doer="ai")
        b = core.go(self.c, os.path.join(self.web, "src"))
        self.assertEqual(b["role"], "worker")
        self.assertEqual(b["item"]["id"], x)
        self.assertTrue(b["agent"].startswith("web-"))
        again = core.go(self.c, self.web, actor=b["agent"])
        self.assertTrue(again.get("resumed"))

    def test_unblocker(self):
        a1 = self.add("api", "endpoint", doer="ai")
        self.add("web", "page", after=[a1], doer="ai")
        b = core.go(self.c, self.web)
        self.assertEqual(b["role"], "unblocker")
        self.assertEqual(b["item"]["id"], a1)

    def test_planner_when_empty_or_outside_blocked(self):
        self.assertEqual(core.go(self.c, self.web)["role"], "planner")
        x = self.add("web", "x")
        core.block(self.c, x, "waiting on design")
        self.assertEqual(core.go(self.c, self.web)["role"], "planner")

    def test_auto_continue_setting(self):
        self.add("web", "x")
        self.assertTrue(core.go(self.c, self.web)["auto_continue"])
        core.config_set(self.c, "auto_continue", "off")
        self.assertFalse(core.go(self.c, self.web)["auto_continue"])
        with self.assertRaises(RiverError):
            core.config_set(self.c, "auto_continue", "maybe")

    def test_idle_when_others_hold_everything(self):
        self.add("web", "x", doer="ai")
        first = core.go(self.c, self.web)
        self.assertEqual(first["role"], "worker")
        self.assertEqual(core.go(self.c, self.web)["role"], "idle")

    def test_unlinked_folder_refused(self):
        with self.assertRaises(RiverError):
            core.go(self.c, self.dir.name)


class Capacity(Base):
    def test_slots_and_excess(self):
        core.project_add(self.c, "a")
        self.add("a", "x", doer="ai")
        self.add("a", "y", doer="ai")
        self.add("a", "h", doer="human")
        cap = core.capacity(self.c)
        self.assertEqual(cap["spare_slots"], 2)
        for n in ("s1", "s2", "s3", "s4"):
            core.register(self.c, n)
        cap = core.capacity(self.c)
        self.assertEqual(cap["spare_slots"], 0)
        self.assertEqual(cap["excess_sessions"], 2)
        self.assertEqual(len(cap["ready_for_humans"]), 1)

    def test_settings_precedence(self):
        core.project_add(self.c, "a")
        x = self.add("a", "x")
        core.config_set(self.c, "lease_ttl", "1h")
        core.config_set(self.c, "lease_ttl", "2h", project="a")
        core.config_set(self.c, "lease_ttl", "3h", agent="sam")
        core.config_set(self.c, "lease_ttl", "4h", item=x)
        self.assertEqual(core.setting(self.c, "lease_ttl", item_id=x, agent="sam"), "4h")
        core.config_unset(self.c, "lease_ttl", item=x)
        self.assertEqual(core.setting(self.c, "lease_ttl", item_id=x, agent="sam"), "3h")
        self.assertEqual(core.setting(self.c, "lease_ttl", item_id=x), "2h")
        with self.assertRaises(RiverError):
            core.config_set(self.c, "lease_ttl", "soon")


class Messages(Base):
    def setUp(self):
        super().setUp()
        core.project_add(self.c, "a")
        core.register(self.c, "alice")
        core.register(self.c, "bob")
        self.x = self.add("a", "x")

    def test_alert_accept_claims_now_or_keeps_for_later(self):
        found = self.add("a", "found work")
        m = core.message(self.c, "alert", "you need this", to="alice", item=found, actor="bob")[0]
        core.inbox(self.c, "alice")  # reading an alert does not answer it
        core.accept_message(self.c, m["id"], "alice")  # alice holds nothing: she takes it now
        self.assertEqual(core._item(self.c, found)["assignee"], "alice")
        self.assertEqual(core.message_show(self.c, m["id"])["state"], "accepted")
        later = self.add("a", "later work")
        m2 = core.message(self.c, "alert", "and this", holder_of=found, item=later, actor="bob")[0]
        self.assertEqual(m2["to_agent"], "alice")
        core.accept_message(self.c, m2["id"], "alice")  # busy: kept for after the current item
        it = core._item(self.c, later)
        self.assertEqual((it["status"], it["reserved_for"], it["reserved_by"]), ("open", "alice", "bob"))
        self.assertEqual([r["body"][:10] for r in core.inbox(self.c, "bob")], ["yes to you", "yes to you"])
        q = core.message(self.c, "question", "why?", to="bob", actor="alice")[0]
        with self.assertRaises(RiverError):
            core.accept_message(self.c, q["id"], "bob")  # only alerts
        with self.assertRaises(RiverError):
            core.message(self.c, "alert", "x", holder_of=self.x, actor="bob")  # nobody holds x

    def test_ask_by_file_and_prerequisite_done_notice(self):
        core.project_add(self.c, "p", path=self.dir.name)
        pre = core.item_add(self.c, "p", "pre", actor="t")["id"]
        big = core.item_add(self.c, "p", "big", actor="t", touches="app.py")["id"]
        core.claim(self.c, big, "alice")
        core.dep_add(self.c, big, [pre], "t")
        qs = core.message(self.c, "question", "is app.py yours?", file="app.py", actor="bob")
        self.assertEqual([q["to_agent"] for q in qs], ["alice"])
        core.claim(self.c, pre, "bob")
        core.done(self.c, pre, "made the table", "bob")
        notes = [m["body"] for m in core.inbox(self.c, "alice") if m["kind"] == "notice"]
        self.assertEqual(len(notes), 1)
        self.assertIn(f"#{pre} pre is done (output: made the table); your #{big} waits on it", notes[0])

    def test_unanswered_question_nudges(self):
        q = core.send(self.c, "question", "why?", to="alice", actor="bob")
        self.assertEqual(core.unread(self.c, "alice")["questions_waiting"], 0)
        old = core.iso(core.now() - timedelta(hours=2))
        self.c.execute("UPDATE messages SET created_at=? WHERE id=?", (old, q["id"]))
        self.assertEqual(core.unread(self.c, "alice")["questions_waiting"], 1)
        core.activity(self.c, "bob")  # alice is active: no notice
        self.assertEqual([m for m in core.inbox(self.c, "bob") if m["kind"] == "notice"], [])
        self.c.execute("UPDATE agents SET last_seen=? WHERE name='alice'", (old,))
        core.activity(self.c, "bob")
        core.activity(self.c, "bob")
        notes = [m["body"] for m in core.inbox(self.c, "bob") if m["kind"] == "notice"]
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0].startswith("alice is away"))

    def test_unread_message_to_gone_agent_tells_sender_once(self):
        m = core.send(self.c, "alert", "look at this", to="alice", actor="bob")
        self.c.execute("UPDATE agents SET last_seen=? WHERE name='alice'", (core.iso(core.now() - timedelta(hours=2)),))
        core.activity(self.c, "bob")  # away is not gone
        self.assertEqual([x for x in core.inbox(self.c, "bob") if x["kind"] == "notice"], [])
        self.c.execute("UPDATE agents SET last_seen=? WHERE name='alice'", (core.iso(core.now() - timedelta(days=2)),))
        core.activity(self.c, "bob")
        core.activity(self.c, "bob")
        notes = [x["body"] for x in core.inbox(self.c, "bob") if x["kind"] == "notice"]
        self.assertEqual(len(notes), 1)
        self.assertIn(f"never read your alert #{m['id']}", notes[0])

    def test_question_to_item_reaches_next_holder_and_answer_closes_it(self):
        q = core.send(self.c, "question", "why?", item=self.x, actor="bob")
        self.assertIsNone(q["to_agent"])
        self.assertEqual(core.unread(self.c, "alice")["unread"], 0)
        core.claim(self.c, self.x, "alice")
        self.assertEqual({k: v for k, v in core.unread(self.c, "alice").items() if k in ("unread", "alerts", "questions")}, {"unread": 1, "alerts": 0, "questions": 1})
        self.assertEqual([m["id"] for m in core.inbox(self.c, "alice")], [q["id"]])
        # Read, but still waiting for an answer, so it stays in the inbox.
        self.assertEqual({k: v for k, v in core.unread(self.c, "alice").items() if k in ("unread", "alerts", "questions")}, {"unread": 0, "alerts": 0, "questions": 1})
        self.assertEqual(len(core.inbox(self.c, "alice")), 1)
        a = core.answer(self.c, q["id"], "because", actor="alice")
        self.assertEqual(a["to_agent"], "bob")
        self.assertEqual(core.message_show(self.c, q["id"])["state"], "answered")
        self.assertEqual(core.inbox(self.c, "alice"), [])
        self.assertEqual(core.unread(self.c, "bob")["unread"], 1)
        with self.assertRaises(RiverError):
            core.answer(self.c, q["id"], "again", actor="alice")

    def test_send_to_held_item_goes_to_holder(self):
        core.claim(self.c, self.x, "alice")
        m = core.send(self.c, "alert", "stop", item=self.x, actor="bob")
        self.assertEqual(m["to_agent"], "alice")
        self.assertEqual(core.unread(self.c, "alice")["alerts"], 1)
        inbox = core.inbox(self.c, "alice")
        self.assertEqual(core.message_show(self.c, inbox[0]["id"])["state"], "read")
        self.assertEqual(core.inbox(self.c, "alice"), [])
        self.assertEqual(len(core.inbox(self.c, "alice", include_read=True)), 1)

    def test_reply_goes_to_sender_and_joins_thread(self):
        n = core.send(self.c, "note", "fyi", to="alice", actor="bob")
        r = core.send(self.c, "note", "thanks", reply_to=n["id"], actor="alice")
        self.assertEqual(r["to_agent"], "bob")
        self.assertEqual(r["thread_id"], n["id"])
        self.assertEqual(core.unread(self.c, "alice")["unread"], 0)  # replying reads it
        t = core.thread(self.c, r["id"], "bob")
        self.assertEqual([m["id"] for m in t["messages"]], [n["id"], r["id"]])
        self.assertEqual(core.unread(self.c, "bob")["unread"], 0)

    def test_refusals(self):
        with self.assertRaises(RiverError):
            core.send(self.c, "note", "hi", actor="bob")  # no recipient
        with self.assertRaises(RiverError):
            core.send(self.c, "note", "hi", to="nobody", actor="bob")
        with self.assertRaises(RiverError):
            core.send(self.c, "answer", "hi", to="alice", actor="bob")
        n = core.send(self.c, "note", "hi", to="alice", actor="bob")
        with self.assertRaises(RiverError):
            core.answer(self.c, n["id"], "no", actor="alice")
        q = core.send(self.c, "question", "?", to="alice", actor="bob")
        with self.assertRaises(RiverError):
            core.answer(self.c, q["id"], "self", actor="bob")

    def test_lease_expiry_sends_notice(self):
        core.claim(self.c, self.x, "alice")
        past = core.iso(core.now() - timedelta(minutes=1))
        self.c.execute("UPDATE items SET lease_expires_at=? WHERE id=?", (past, self.x))
        core.activity(self.c, "bob")
        box = core.inbox(self.c, "alice")
        self.assertEqual([(m["kind"], m["from_agent"], m["item_id"]) for m in box], [("notice", "maxpm", self.x)])


class Targets(Base):
    def test_target_groups_projects(self):
        core.target_add(self.c, "web", "rsync, then restart")
        core.project_add(self.c, "site", target="web")
        core.project_add(self.c, "api")
        core.project_target(self.c, "api", "web")
        core.project_add(self.c, "tool")
        t = core.target_show(self.c, "web")
        self.assertEqual([p["name"] for p in t["projects"]], ["site", "api"])
        self.assertEqual(t["description"], "rsync, then restart")
        self.assertIsNone(t["owner"])
        self.assertEqual([(t["name"], t["projects"]) for t in core.target_list(self.c)], [("web", 2)])
        core.project_target(self.c, "api", None)
        self.assertEqual([p["name"] for p in core.target_show(self.c, "web")["projects"]], ["site"])

    def test_refusals(self):
        core.project_add(self.c, "a")
        with self.assertRaises(RiverError):
            core.project_target(self.c, "a", "nowhere")
        core.target_add(self.c, "web")
        with self.assertRaises(RiverError):
            core.target_add(self.c, "web")
        with self.assertRaises(RiverError):
            core.target_add(self.c, "Bad Name")

    def test_old_database_gains_target_column(self):
        self.c.close()
        import sqlite3
        raw = sqlite3.connect(self.path)
        raw.executescript("DROP TABLE projects; CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, "
                          "rank INTEGER NOT NULL, notes TEXT NOT NULL DEFAULT '', archived INTEGER NOT NULL DEFAULT 0, "
                          "created_at TEXT NOT NULL);")
        raw.close()
        self.c = core.connect(self.path)
        cols = {r["name"] for r in self.c.execute("PRAGMA table_info(projects)")}
        self.assertTrue({"path", "target"} <= cols)


class Log(Base):
    def test_completed_by_day_with_progress(self):
        core.project_add(self.c, "a")
        core.project_add(self.c, "b")
        core.register(self.c, "ag")
        x, y, z = self.add("a", "x"), self.add("a", "y"), self.add("b", "z")
        dropped = self.add("a", "d")
        core.claim(self.c, x, "ag")
        core.done(self.c, x, "commit abc", "ag")
        core.done(self.c, z, None, "t")
        core.drop(self.c, dropped, "t")
        old = core.iso(core.now() - timedelta(days=10))
        self.c.execute("UPDATE items SET closed_at=? WHERE id=?", (old, z))
        log = core.completed(self.c)
        self.assertEqual([i["id"] for i in log["items"]], [x])
        self.assertEqual(log["items"][0]["by_agent"], "ag")
        self.assertEqual(log["items"][0]["output"], "commit abc")
        self.assertEqual(log["by_day"][0]["day"], log["items"][0]["closed_at"][:10])
        prog = {p["project"]: p for p in log["progress"]}
        self.assertEqual((prog["a"]["done"], prog["a"]["total"], prog["a"]["done_in_window"]), (1, 2, 1))
        self.assertEqual((prog["b"]["done"], prog["b"]["done_in_window"]), (1, 0))
        every = core.completed(self.c, since=None)
        self.assertEqual({i["id"] for i in every["items"]}, {x, z})
        self.assertEqual([i["id"] for i in core.completed(self.c, "b", None)["items"]], [z])
        self.assertNotIn(y, {i["id"] for i in every["items"]})
        with self.assertRaises(RiverError):
            core.completed(self.c, since="soon")


class Context(Base):
    def test_context_touches_check(self):
        core.project_add(self.c, "a")
        i = core.item_add(self.c, "a", "x", context="why", touches=["b.py", "a.py", "b.py", " "], check="make test")
        self.assertEqual((i["context"], i["touches"], i["check"]), ("why", ["b.py", "a.py"], "make test"))
        core.item_edit(self.c, i["id"], touches="c.py, d.py")
        self.assertEqual(core.item_show(self.c, i["id"])["touches"], ["c.py", "d.py"])
        core.item_edit(self.c, i["id"], touches=[], check="")
        got = core.item_show(self.c, i["id"])
        self.assertEqual((got["touches"], got["check"], got["context"]), ([], "", "why"))
        self.assertEqual(core.next_item(self.c)[0]["context"], "why")

    def test_old_database_gains_context_columns(self):
        self.c.close()
        import sqlite3
        raw = sqlite3.connect(self.path)
        for col in ("context", "touches", "check"):
            raw.execute(f'ALTER TABLE items DROP COLUMN "{col}"')
        raw.commit()
        raw.close()
        self.c = core.connect(self.path)
        cols = {r["name"] for r in self.c.execute("PRAGMA table_info(items)")}
        self.assertTrue({"context", "touches", "check"} <= cols)


class Kinds(Base):
    def setUp(self):
        super().setUp()
        core.project_add(self.c, "a")
        core.register(self.c, "ag")
        core.register(self.c, "bo")

    def test_overlapping_touches_conflict_automatically(self):
        api = core.item_add(self.c, "a", "api", touches=["src/api.py"])["id"]
        whole = core.item_add(self.c, "a", "src dir", touches=["src/"])["id"]
        docs = core.item_add(self.c, "a", "docs", touches=["README.md"])["id"]
        ann = core.annotate(self.c)
        self.assertEqual(ann[whole]["conflicts"], [api])
        self.assertEqual(ann[docs]["conflicts"], [])
        core.claim(self.c, api, "ag")
        ann = core.annotate(self.c)
        self.assertFalse(ann[whole]["ready"])
        self.assertEqual(ann[whole]["busy_conflicts"], [api])
        self.assertTrue(ann[docs]["ready"])
        with self.assertRaisesRegex(RiverError, f"conflicts with #{api}, which is in progress"):
            core.claim(self.c, whole, "bo")
        self.assertEqual([a["id"] for a in core.ready_list(self.c)], [docs])
        # The agent that holds the other item can take it: a conflict keeps two agents apart, not one (#600).
        self.assertEqual([a["id"] for a in core.ready_list(self.c, actor="ag")], [whole, docs])
        self.c.execute("INSERT OR REPLACE INTO settings(scope,key,value) VALUES ('global','max_leases','2')")
        core.push(self.c, whole, "ag", actor="bo")
        self.assertEqual(core.accept(self.c, whole, "ag")["assignee"], "ag")
        core.release(self.c, whole, actor="ag")
        third = core.item_add(self.c, "a", "api tests", touches=["src/api.py", "README.md"])["id"]
        core.claim(self.c, docs, "bo")
        with self.assertRaisesRegex(RiverError, "which is in progress"):
            core.claim(self.c, third, "ag")  # bo holds one of the items it conflicts with
        core.release(self.c, docs, actor="bo")
        core.done(self.c, api, "ok", "ag")
        self.assertTrue(core.annotate(self.c)[whole]["ready"])

    def test_add_with_feeds(self):
        first = core.item_add(self.c, "a", "make the table")["id"]
        other = core.item_add(self.c, "a", "other")["id"]
        n = core.item_add(self.c, "a", "use the table", after=[other], feeds=[first])["id"]
        show = core.item_show(self.c, n)
        self.assertEqual({d["id"]: d["kind"] for d in show["waits_on_detail"]}, {first: "feeds", other: "blocks"})

    def test_touches_compare_within_each_project_folder(self):
        d = self.dir.name
        core.project_add(self.c, "site1", path=os.path.join(d, "one"))
        core.project_add(self.c, "site2", path=os.path.join(d, "two"))
        core.project_add(self.c, "same", path=os.path.join(d, "one"))  # a second project in the same folder
        core.project_add(self.c, "nofolder")
        core.project_add(self.c, "nofolder2")
        a = core.item_add(self.c, "site1", "a", touches="public/")["id"]
        b = core.item_add(self.c, "site2", "b", touches="public/index.html")["id"]
        c = core.item_add(self.c, "same", "c", touches="public/index.html")["id"]
        e = core.item_add(self.c, "nofolder", "e", touches="public/")["id"]
        f = core.item_add(self.c, "nofolder2", "f", touches="public/x")["id"]
        g = core.item_add(self.c, "nofolder", "g", touches="public/x")["id"]
        h = core.item_add(self.c, "site2", "h", touches=os.path.join(d, "one", "public", "y"))["id"]
        ann = core.annotate(self.c)
        self.assertEqual(ann[a]["conflicts"], [c, h])  # same folder, and an absolute path into it
        self.assertEqual(ann[b]["conflicts"], [])  # another repository's public/
        self.assertEqual(ann[e]["conflicts"], [g])  # no folder: only its own project
        self.assertEqual(ann[f]["conflicts"], [])

    def test_touch_edit_adds_and_removes_auto_conflicts(self):
        x = core.item_add(self.c, "a", "x", touches=["a.py"])["id"]
        y = core.item_add(self.c, "a", "y", touches=["b.py"])["id"]
        self.assertEqual(core.annotate(self.c)[x]["conflicts"], [])
        core.item_edit(self.c, y, touches=["a.py"])
        self.assertEqual(core.annotate(self.c)[x]["conflicts"], [y])
        core.item_edit(self.c, y, touches=["c.py"])
        self.assertEqual(core.annotate(self.c)[x]["conflicts"], [])

    def test_manual_conflict_and_order_replaces_it(self):
        x, y = self.add("a", "x"), self.add("a", "y")
        core.dep_add(self.c, x, [y], kind="conflicts")
        self.assertEqual(core.annotate(self.c)[y]["conflicts"], [x])
        self.assertEqual(core.annotate(self.c)[y]["waits_on"], [])
        core.dep_add(self.c, y, [x])  # an order between them replaces the conflict link
        ann = core.annotate(self.c)
        self.assertEqual((ann[y]["conflicts"], ann[y]["waits_on"]), ([], [x]))
        with self.assertRaises(RiverError):
            core.dep_add(self.c, x, [y], kind="conflicts")
        core.dep_remove(self.c, y, [x])
        core.dep_add(self.c, y, [x], kind="conflicts")
        core.dep_remove(self.c, x, [y])  # either side removes a conflict
        self.assertEqual(core.annotate(self.c)[y]["conflicts"], [])

    def test_feeds_waits_then_shows_output(self):
        up = self.add("a", "up")
        down = self.add("a", "down")
        core.dep_add(self.c, down, [up], kind="feeds")
        self.assertFalse(core.annotate(self.c)[down]["ready"])
        core.done(self.c, up, "made get_user(id)", "t")
        got = core.item_show(self.c, down)
        self.assertTrue(got["ready"])
        self.assertEqual([(f["id"], f["output"]) for f in got["fed_by_detail"]], [(up, "made get_user(id)")])

    def test_conflicting_ready_items_are_one_slot(self):
        core.item_add(self.c, "a", "x", touches=["f.py"])
        core.item_add(self.c, "a", "y", touches=["f.py"])
        core.item_add(self.c, "a", "z", touches=["g.py"])
        cap = core.capacity(self.c)  # setUp registers two idle agents
        self.assertEqual(cap["spare_slots"] + len(cap["agents_idle"]) - cap["excess_sessions"], 2)
        self.assertEqual(len(cap["ready_for_agents"]), 3)

    def test_the_graph_is_one_view_while_another_session_adds_a_linked_item(self):
        # The items and their links are separate reads. Another session's new item, with a link, between them
        # gave a link to an item the first read did not have, and the command stopped (KeyError).
        x = core.item_add(self.c, "a", "one")["id"]
        other = core.connect(self.path)
        self.addCleanup(other.close)
        added = []

        class Between:
            """This session's connection; the other session writes right after the items are read."""
            def __init__(self, conn):
                self.conn = conn

            def __getattr__(self, name):
                return getattr(self.conn, name)

            def execute(self, sql, *args):
                rows = self.conn.execute(sql, *args)
                if sql == "SELECT * FROM items" and not added:
                    rows = rows.fetchall()
                    added.append(core.item_add(other, "a", "two", after=[x])["id"])
                return rows
        before = sorted(core.annotate(self.c))
        self.assertEqual((sorted(core.annotate(Between(self.c))), len(added)), (before, 1))  # the view from before that write
        self.assertEqual(sorted(core.annotate(self.c)), before + added)
        with core.tx(self.c):  # inside a transaction the view is one already
            self.assertEqual(len(core.annotate(self.c)), len(before) + 1)

    def test_conflicts_do_not_count_as_cycles(self):
        x, y = self.add("a", "x"), self.add("a", "y")
        z = self.add("a", "z", after=[x])
        core.dep_add(self.c, z, [y], kind="conflicts")
        core.dep_add(self.c, x, [y])  # no loop: conflicts have no direction
        self.assertEqual(core.annotate(self.c)[x]["waits_on"], [y])


class Ownership(Base):
    def setUp(self):
        super().setUp()
        core.target_add(self.c, "web")
        for n in ("ag", "bo"):
            core.register(self.c, n)

    def test_parallel_own_has_one_winner(self):
        names = [f"w{i}" for i in range(8)]
        for n in names:
            core.register(self.c, n)
        won, refused = [], []
        start = threading.Barrier(len(names))

        def worker(name):
            c = core.connect(self.path)
            try:
                start.wait()
                core.target_own(c, "web", name)
                won.append(name)
            except RiverError as e:
                refused.append(str(e))
            finally:
                c.close()

        ts = [threading.Thread(target=worker, args=(n,)) for n in names]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(won), 1)
        self.assertEqual(len(refused), len(names) - 1)
        self.assertTrue(all(f"owned by {won[0]}" in e for e in refused))
        self.assertEqual(core.target_show(self.c, "web")["owner"], won[0])

    def test_own_is_renewed_and_repeatable(self):
        core.target_own(self.c, "web", "ag")
        self.c.execute("UPDATE targets SET owner_expires_at=? WHERE name='web'",
                       (core.iso(core.now() + timedelta(minutes=5)),))
        core.activity(self.c, "ag")
        left = core.parse_iso(core.target_show(self.c, "web")["owner_expires_at"]) - core.now()
        self.assertGreater(left, timedelta(hours=7))
        core.target_own(self.c, "web", "ag")  # owning again is fine

    def test_expiry_frees_target_and_notifies(self):
        core.target_own(self.c, "web", "ag")
        self.c.execute("UPDATE targets SET owner_expires_at=? WHERE name='web'",
                       (core.iso(core.now() - timedelta(minutes=1)),))
        core.target_own(self.c, "web", "bo")
        self.assertEqual(core.target_show(self.c, "web")["owner"], "bo")
        box = core.inbox(self.c, "ag")
        self.assertEqual([(m["kind"], m["from_agent"]) for m in box], [("notice", "maxpm")])
        self.assertIn("web", box[0]["body"])

    def test_give_release_and_refusals(self):
        with self.assertRaises(RiverError):
            core.target_give(self.c, "web", "bo", "ag")  # nobody owns it
        core.target_own(self.c, "web", "ag")
        with self.assertRaises(RiverError):
            core.target_release(self.c, "web", "bo")
        with self.assertRaises(RiverError):
            core.unregister(self.c, "ag")
        core.target_give(self.c, "web", "bo", "ag")
        self.assertEqual(core.target_show(self.c, "web")["owner"], "bo")
        self.assertEqual(core.unread(self.c, "bo")["unread"], 1)
        self.assertEqual([o["name"] for o in core.agent_status(self.c, "bo")["owns"]], ["web"])
        core.target_release(self.c, "web", "bo")
        self.assertIsNone(core.target_show(self.c, "web")["owner"])

    def _last_seen(self, name, ago):
        self.c.execute("UPDATE agents SET last_seen=? WHERE name=?", (core.iso(core.now() - ago), name))

    def test_takeover_needs_an_owner_who_is_not_active(self):
        core.target_own(self.c, "web", "ag")
        with self.assertRaises(RiverError) as e:
            core.target_own(self.c, "web", "bo", takeover="deploy is waiting")
        self.assertIn("owned by ag", str(e.exception))
        self.assertEqual(core.target_show(self.c, "web")["owner"], "ag")

    def test_away_owner_refusal_names_the_takeover(self):
        core.target_own(self.c, "web", "ag")
        self._last_seen("ag", timedelta(hours=2))
        with self.assertRaises(RiverError) as e:
            core.target_own(self.c, "web", "bo")
        self.assertIn("ag is away", str(e.exception))
        self.assertIn("--takeover", str(e.exception))
        with self.assertRaises(RiverError):
            core.target_own(self.c, "web", "bo", takeover="  ")  # a reason is required

    def test_takeover_from_an_away_owner_moves_the_target_and_tells_them(self):
        core.target_own(self.c, "web", "ag")
        self._last_seen("ag", timedelta(hours=2))
        core.target_own(self.c, "web", "bo", takeover="deploy is waiting")
        t = core.target_show(self.c, "web")
        self.assertEqual(t["owner"], "bo")
        self.assertGreater(core.parse_iso(t["owner_expires_at"]) - core.now(), timedelta(hours=7))
        box = core.inbox(self.c, "ag")
        self.assertEqual([(m["kind"], m["from_agent"]) for m in box], [("notice", "bo")])
        self.assertIn("deploy is waiting", box[0]["body"])
        self.assertIn("maxpm target give web --to ag", box[0]["body"])

    def test_takeover_from_a_gone_owner(self):
        core.target_own(self.c, "web", "ag")
        self._last_seen("ag", timedelta(days=2))
        core.target_own(self.c, "web", "bo", takeover="owner session ended")
        self.assertEqual(core.target_show(self.c, "web")["owner"], "bo")

    def test_a_person_can_give_any_target(self):
        core.register(self.c, "pat", human=True)
        core.target_own(self.c, "web", "ag")
        core.target_give(self.c, "web", "bo", "pat")
        self.assertEqual(core.target_show(self.c, "web")["owner"], "bo")
        self.assertEqual(core.unread(self.c, "bo")["unread"], 1)
        self.assertIn("gave target web to bo", core.inbox(self.c, "ag")[0]["body"])
        with self.assertRaises(RiverError):
            core.target_give(self.c, "web", "ag", "ag")  # an agent that is not the owner still cannot


class Status(Base):
    def test_status_counts_and_recent(self):
        core.project_add(self.c, "a")
        core.register(self.c, "ag")
        x, y = self.add("a", "x"), self.add("a", "y")
        h = self.add("a", "sign", doer="human")
        w = self.add("a", "later", after=[y])
        core.claim(self.c, x, "ag")
        core.done(self.c, x, "ok", "ag")
        core.claim(self.c, y, "ag")
        st = core.status(self.c)
        row = st["projects"][0]
        self.assertEqual({k: row[k] for k in ("done", "open", "ready", "in_progress", "human_waiting", "blocked")},
                         {"done": 1, "open": 3, "ready": 1, "in_progress": 1, "human_waiting": 1, "blocked": 1})
        self.assertEqual([r["id"] for r in st["recent"]], [x])
        self.assertEqual([h_["id"] for h_ in st["human_waiting"]], [h])
        self.assertEqual([a["holds"][0]["id"] for a in st["agents"] if a["name"] == "ag"], [y])
        self.assertNotEqual(w, h)

    def test_who_and_status_hide_ended_agents_and_show_is_brief(self):
        from river import cli
        core.project_add(self.c, "a")
        for n in ("live", "old", "gone", "holder"):
            core.register(self.c, n)
        x = self.add("a", "held by a gone agent")
        y = self.add("a", "next step", after=[x])
        core.item_edit(self.c, y, None, None, None, None, "t", "why " * 200)
        core.claim(self.c, x, "holder")
        core.register(self.c, "mark", human=True)
        core.stop_agent(self.c, "old", "finished", actor="mark")
        t = core.iso(core.now() - core.timedelta(days=2))
        self.c.execute("UPDATE agents SET last_seen=? WHERE name IN ('gone','holder')", (t,))
        names = lambda rows: sorted(a["name"] for a in rows)
        # A gone agent that holds an item stays: someone must act on it.
        self.assertEqual(names(core.who(self.c, everyone=False)), ["holder", "live", "mark"])
        self.assertEqual(names(core.who(self.c)), ["gone", "holder", "live", "mark", "old"])
        st = core.status(self.c)
        self.assertEqual((names(st["agents"]), st["ended"]), (["holder", "live", "mark"], 2))
        old_env = os.environ.get("MAXPM_DB")
        os.environ["MAXPM_DB"] = self.path
        self.addCleanup(lambda: os.environ.pop("MAXPM_DB") if old_env is None else os.environ.update(MAXPM_DB=old_env))

        def run(*words):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                cli.run(list(words))
            return out.getvalue(), err.getvalue()
        out, err = run("who")
        self.assertNotIn("old (", out)
        self.assertIn("2 stopped or gone agent(s) that hold nothing are hidden: maxpm who --all", err)
        out, err = run("who", "--all")
        self.assertIn("old (ai, stopped)", out)
        self.assertNotIn("hidden", err)
        self.assertIn("(2 stopped or gone agent(s) that hold nothing: maxpm who --all)", run("status")[0])
        full, brief = run("show", str(y))[0], run("show", str(y), "--brief")[0]
        self.assertIn("added to a", full)  # the history
        self.assertNotIn("added to a", brief)
        self.assertLess(len(brief), len(full) // 2)
        self.assertIn(f"waits on: #{x} held by a gone agent (in_progress)", brief)
        self.assertIn(f"all of it: maxpm show {y}", brief)
        self.assertTrue(all(len(line) < 400 for line in brief.splitlines()))


class Transcript:
    """Writes a Claude Code transcript for a test: the lines that core.background_work reads."""

    def __init__(self, root, sid, folder="-work-a"):
        os.makedirs(os.path.join(root, "projects", folder), exist_ok=True)
        self.path, self.n = os.path.join(root, "projects", folder, f"{sid}.jsonl"), 0

    def add(self, when, kind, content=None, **more):
        row = {"type": kind, "timestamp": when.strftime("%Y-%m-%dT%H:%M:%S.000Z"), **more}
        if content is not None:
            row["message"] = {"role": kind, "content": content}
        with open(self.path, "a") as f:
            f.write(json.dumps(row) + "\n")

    def use(self, when, name, **inp):
        """A tool call; returns its id."""
        self.n += 1
        self.add(when, "assistant", [{"type": "tool_use", "id": f"toolu_{self.n:04d}", "name": name, "input": inp}])
        return f"toolu_{self.n:04d}"

    def result(self, when, use, text):
        self.add(when, "user", [{"type": "tool_result", "tool_use_id": use, "content": text}])

    def background(self, when, task, command, description):
        use = self.use(when, "Bash", command=command, description=description, run_in_background=True)
        self.result(when, use, f"Command running in background with ID: {task}. Output is being written to: /tmp/x")
        return use

    def ended(self, when, task, use, status="completed", how="user"):
        note = (f"<task-notification>\n<task-id>{task}</task-id>\n<tool-use-id>{use}</tool-use-id>\n"
                f"<status>{status}</status>\n<summary>ended</summary>\n</task-notification>")
        if how == "user":
            self.add(when, "user", note)
        elif how == "attachment":  # the form of a notification that comes in the middle of a turn
            self.add(when, "attachment", attachment={"type": "queued_command", "prompt": note})
        else:
            self.add(when, "queue-operation", operation="enqueue", content=note)


class BackgroundWork(Base):
    """A session that waits for a background command or a subagent of its own is not idle: the transcript says
    what the harness started and what ended (#1611)."""
    SID = "44444444-4444-4444-8444-444444444444"

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "a")
        core.register(self.c, "ag")
        self.root = os.path.join(self.dir.name, "claude")
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.root})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(core._BG_CACHE.clear)
        self.t0 = core.now() - timedelta(minutes=30)
        self.tr = Transcript(self.root, self.SID)
        self.what = lambda: [(w["kind"], w["what"]) for w in core.background_work(self.c, "ag")]

    def at(self, minutes):
        return self.t0 + timedelta(minutes=minutes)

    def test_a_command_or_a_subagent_that_has_not_ended_is_work(self):
        self.assertEqual(self.what(), [])  # no session known
        core.record_claude_session(self.c, "ag", self.SID)
        self.assertEqual(self.what(), [])  # no transcript on this computer
        # A command started in the background, and its end.
        use = self.tr.background(self.at(1), "b1", "cargo test -p server", "Run the server tests")
        self.assertEqual(self.what(), [("command", "Run the server tests")])
        self.tr.ended(self.at(5), "b1", use)
        self.assertEqual(self.what(), [])
        # A command that ran into its time limit: the harness moved it to the background.
        use = self.tr.use(self.at(6), "Bash", command="make check-all", description="Run the full gate")
        self.tr.result(self.at(16), use, "Command did not complete within its 600s timeout and was moved to the "
                                         "background (ID: b2). Output is being written to: /tmp/y")
        self.assertEqual(self.what(), [("command", "Run the full gate")])
        self.tr.ended(self.at(17), "b2", use, "failed", how="attachment")
        self.assertEqual(self.what(), [])
        # A background subagent; its notification can come as a queue line.
        use = self.tr.use(self.at(18), "Agent", description="Review the pricing commits", prompt="...")
        self.tr.result(self.at(18), use, [{"type": "text", "text": "Async agent launched successfully. (internal)\n"
                                                                   "agentId: a77 (internal ID)"}])
        self.assertEqual(self.what(), [("subagent", "Review the pricing commits")])
        self.tr.ended(self.at(20), "a77", use, how="queue")
        self.assertEqual(self.what(), [])
        # A task that the session stopped itself.
        self.tr.background(self.at(21), "b3", "npm run dev", "Run the dev server")
        self.assertEqual(self.what(), [("command", "Run the dev server")])
        self.tr.use(self.at(22), "TaskStop", task_id="b3")
        self.assertEqual(self.what(), [])

    def test_a_wait_a_lease_loop_an_old_task_and_quoted_text_are_not_work(self):
        core.record_claude_session(self.c, "ag", self.SID)
        self.tr.background(self.at(1), "b1", "maxpm --as ag inbox --wait", "Wait for messages")
        self.tr.background(self.at(1), "b2", "cd /work && maxpm --as ag wait", "Wait for work")
        self.tr.background(self.at(1), "b3", "for i in $(seq 1 18); do maxpm --as ag heartbeat; sleep 600; done",
                           "Renew the item lease every 10 minutes")
        self.assertEqual(self.what(), [])
        # A tool result that quotes the words (a grep of another transcript) starts nothing.
        use = self.tr.use(self.at(2), "Bash", command="grep background other.jsonl", description="Search")
        self.tr.result(self.at(2), use, "other.jsonl: Command running in background with ID: b9. Output is ...")
        self.assertEqual(self.what(), [])
        # A real task counts for busy_max (4h) after its start, not for ever: its end can be lost.
        self.tr.background(self.at(3), "b4", "pytest -q", "Run the tests")
        self.assertEqual(self.what(), [("command", "Run the tests")])
        late = core.now() + timedelta(hours=4)
        with mock.patch.object(core, "now", lambda: late):
            self.assertEqual(self.what(), [])
        core.config_set(self.c, "busy_max", "0s")
        self.assertEqual(self.what(), [])


class Usage(Base):
    """The token cost of each item, from the Claude Code transcripts of the sessions that held it (#1102)."""
    SID, SID2, BASE = ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222",
                       "33333333-3333-4333-8333-333333333333")

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "a")
        for n in ("ag", "ag2"):
            core.register(self.c, n)
        core.register(self.c, "mark", human=True)
        self.root = os.path.join(self.dir.name, "claude")
        os.makedirs(os.path.join(self.root, "projects", "-work-a"))
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.root})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.t0 = core.now() - timedelta(hours=2)  # the holds trigger stamps the real time

    def at(self, minutes):
        return core.iso(self.t0 + timedelta(minutes=minutes))

    def write(self, sid, lines, sub=None):
        import json
        folder = os.path.join(self.root, "projects", "-work-a")
        if sub:
            folder = os.path.join(folder, sid, "subagents")
            os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, f"{sub or sid}.jsonl"), "w") as f:
            for x in lines:
                f.write(json.dumps(x) + "\n")

    def req(self, minutes, mid, read=1000, w1=100, out=10, inp=5):
        u = {"input_tokens": inp, "cache_read_input_tokens": read, "cache_creation_input_tokens": w1,
             "cache_creation": {"ephemeral_1h_input_tokens": w1, "ephemeral_5m_input_tokens": 0}, "output_tokens": out}
        return {"type": "assistant", "timestamp": self.at(minutes).replace("Z", ".500Z"),
                "message": {"id": mid, "usage": u, "content": []}}

    def hold(self, item, agent, start, end):
        self.c.execute("UPDATE holds SET started_at=?, ended_at=? WHERE item_id=? AND agent=?",
                       (self.at(start), end if end is None else self.at(end), item, agent))

    def test_holds_follow_every_change_of_holder(self):
        x = self.add("a", "x")
        core.claim(self.c, x, "ag")
        core.give(self.c, x, "ag2", actor="ag")
        core.release(self.c, x, actor="ag2")
        core.claim(self.c, x, "ag")
        core.done(self.c, x, "ok", "ag")
        rows = [(r["agent"], r["ended_at"] is not None) for r in self.c.execute(
            "SELECT * FROM holds WHERE item_id=? ORDER BY id", (x,))]
        self.assertEqual(rows, [("ag", True), ("ag2", True), ("ag", True)])

    def test_done_measures_the_sessions_that_held_it(self):
        x, y = self.add("a", "x"), self.add("a", "y")
        core.claim(self.c, x, "ag")
        core.record_claude_session(self.c, "ag", self.SID)
        core.record_claude_session(self.c, "mark", self.SID2)  # a person: no transcript of theirs
        self.assertEqual([r[0] for r in self.c.execute("SELECT agent FROM agent_sessions")], ["ag"])
        self.write(self.SID, [
            {"type": "queue-operation", "operation": "dequeue", "timestamp": self.at(0)},
            {"type": "user", "timestamp": self.at(0), "message": {"content": "go"}},
            self.req(1, "before"),               # before the claim: overhead, not x
            self.req(5, "m1"), self.req(5, "m1"),  # one request, two content blocks
            self.req(20, "m2", read=50000, w1=0, out=300),
            self.req(40, "on-y"),                 # it took y later; y's
            self.req(130, "after-done"),          # after done (now is minute 120)
        ])
        self.write(self.SID, [self.req(7, "sub1", read=10, w1=0, out=1)], sub="agent-1")
        self.hold(x, "ag", 2, None)
        self.c.execute("INSERT INTO holds (item_id, agent, started_at, ended_at) VALUES (?,?,?,?)",
                       (y, "ag", self.at(30), self.at(45)))
        res = core.done(self.c, x, "ok", "ag")
        u = res["usage"]
        self.assertEqual((u["sessions"], u["started"], u["turns"]), (1, "fresh", 3))
        self.assertEqual((u["cache_read"], u["write_1h"], u["output"], u["input"]), (51010, 100, 311, 15))
        self.assertEqual(u["eq"], round(15 + 0.1 * 51010 + 2 * 100))
        self.assertEqual(u["peak_context"], 50005)
        self.assertEqual(core.item_show(self.c, x)["usage"]["eq"], u["eq"])
        # y: the request while it held both went to y, the one it took last.
        self.assertEqual([r["turns"] for r in core.measure_usage(self.c, y)], [1])

    def test_a_fork_leaves_out_what_it_copied_and_the_report_compares(self):
        from river import cli
        x, z = self.add("a", "x"), self.add("a", "z")
        for i, sid in ((x, self.SID), (z, self.SID2)):
            core.claim(self.c, i, "ag" if i == x else "ag2")
            core.record_claude_session(self.c, "ag" if i == x else "ag2", sid)
        self.hold(x, "ag", 0, 30)
        self.hold(z, "ag2", 0, 30)
        self.write(self.SID, [{"type": "user", "timestamp": self.at(0), "message": {"content": "go"}},
                              self.req(1, "a1"), self.req(2, "a2")])
        # The fork: claude -p writes its queue line, then the base's lines with their older times.
        copied = [{"type": "user", "timestamp": self.at(-5), "message": {"content": "base"}}, self.req(-4, "b1")]
        self.write(self.SID2, [{"type": "queue-operation", "operation": "dequeue", "timestamp": self.at(0)}] + copied
                   + [{"type": "user", "timestamp": self.at(1), "message": {"content": "work"}}, self.req(2, "f1")])
        self.assertEqual(core.read_transcript(os.path.join(self.root, "projects", "-work-a", self.SID2 + ".jsonl"))[1], True)
        for i in (x, z):
            core.measure_usage(self.c, i)
        self.c.execute("UPDATE items SET status='done', closed_at=? WHERE id IN (?,?)", (core.iso(core.now()), x, z))
        rep = core.usage_report(self.c)
        self.assertEqual({g["started"]: (g["items"], g["median_turns"]) for g in rep["groups"]},
                         {"fresh": (1, 2), "fork": (1, 1)})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.render_usage(rep)
            cli._print_show(core.item_show(self.c, z))
        self.assertIn(f"#{z}", out.getvalue())
        self.assertIn("usage:   1 session(s) (fork), 1 turns", out.getvalue())
        # A fresh session whose lines are a little out of order is no fork.
        self.write(self.SID, [{"type": "attachment", "timestamp": self.at(0)},
                              {"type": "user", "timestamp": self.at(-0.01), "message": {"content": "go"}},
                              self.req(1, "a1")])
        self.assertFalse(core.read_transcript(os.path.join(self.root, "projects", "-work-a", self.SID + ".jsonl"))[1])

    def test_an_interactive_fork_is_known_by_its_snapshot_time(self):
        path = os.path.join(self.root, "projects", "-work-a", self.SID + ".jsonl")
        snap = lambda m: {"type": "file-history-snapshot", "snapshot": {"timestamp": self.at(m)}}
        self.write(self.SID, [{"type": "custom-title", "customTitle": "w"}, snap(10),
                              {"type": "user", "timestamp": self.at(2), "message": {"content": "base"}},
                              self.req(3, "copied"),
                              {"type": "user", "timestamp": self.at(10), "message": {"content": "go"}},
                              self.req(11, "own")])
        reqs, fork = core.read_transcript(path)
        self.assertEqual((fork, sorted(reqs)), (True, ["own"]))
        # A fresh interactive session: its first message comes just after its snapshot.
        self.write(self.SID, [snap(0), {"type": "user", "timestamp": self.at(0.005), "message": {"content": "go"}},
                              self.req(1, "own")])
        self.assertFalse(core.read_transcript(path)[1])

    def test_no_transcript_no_usage_and_the_session_id_from_the_environment(self):
        x = self.add("a", "x")
        core.claim(self.c, x, "ag")
        core.record_claude_session(self.c, "ag", self.SID)
        self.assertIsNone(core.done(self.c, x, "ok", "ag").get("usage"))
        self.assertEqual(core.claude_session_from_env({"CLAUDE_CODE_SESSION_ID": self.SID}), self.SID)
        self.assertIsNone(core.claude_session_from_env({"CLAUDE_CODE_SESSION_ID": "../x"}))
        self.assertIsNone(core.claude_session_from_env({}))


class Ship(Base):
    def setUp(self):
        super().setUp()
        core.target_add(self.c, "web", "rsync, then smoke test")
        core.project_add(self.c, "site", target="web")
        core.project_add(self.c, "api", target="web")
        for n in ("dev", "ops"):
            core.register(self.c, n)

    def test_two_projects_share_one_deploy_item_only_owner_claims(self):
        a = self.add("site", "page", p=1)
        b = self.add("api", "endpoint")
        core.done(self.c, a, "abc", "t", ship_it=True)
        d1 = core.ship(self.c, b, "dev")
        self.assertEqual(d1["kind"], "deploy")
        self.assertEqual(d1["project"], "deploy-web")
        self.assertEqual(d1["waits_on"], sorted([a, b]))
        self.assertEqual(d1["priority"], 1)
        self.assertEqual(d1["context"], "rsync, then smoke test")
        core.done(self.c, b, "def", "t")
        self.assertEqual([x["id"] for x in core.next_item(self.c, actor="dev")], [])
        with self.assertRaises(RiverError):
            core.claim(self.c, d1["id"], "dev")
        core.target_own(self.c, "web", "ops")
        self.assertEqual([x["id"] for x in core.next_item(self.c, actor="ops")], [d1["id"]])
        core.claim(self.c, d1["id"], "ops")
        c = self.add("site", "later")
        d2 = core.ship(self.c, c, "dev")  # the first deploy item is taken: a new one starts
        self.assertNotEqual(d2["id"], d1["id"])
        done = core.done(self.c, d1["id"], "release r-42", "ops")
        self.assertEqual([x["id"] for x in done["ships"]], sorted([a, b]))
        self.assertEqual(core.unread(self.c, "ops")["unread"], 1)  # notice for the request made while ops owned it
        t = core.state(self.c)["targets"][0]
        self.assertEqual((t["name"], t["owner"], t["project_names"]), ("web", "ops", ["site", "api"]))
        self.assertEqual([(d["id"], [x["id"] for x in d["ships"]]) for d in t["pending"]], [(d2["id"], [c])])
        self.assertEqual((t["last_deploy"]["id"], t["last_deploy"]["output"]), (d1["id"], "release r-42"))
        self.assertEqual([x["id"] for x in t["last_deploy"]["ships"]], sorted([a, b]))

    def test_ship_refusals_and_repeat(self):
        core.project_add(self.c, "misc")
        with self.assertRaises(RiverError):
            core.ship(self.c, self.add("misc", "x"), "dev")
        a = self.add("site", "page")
        d = core.ship(self.c, a, "dev")
        self.assertEqual(core.ship(self.c, a, "dev")["id"], d["id"])
        with self.assertRaises(RiverError):
            core.ship(self.c, d["id"], "dev")


class Plan(Base):
    def test_plan_brief_refuses_claims_and_go_switches_back(self):
        core.project_add(self.c, "a", notes="thing")
        core.project_add(self.c, "b")
        x = self.add("a", "x")
        b = core.plan(self.c, self.dir.name)
        me = b["agent"]
        self.assertTrue(me.startswith("planner-"))
        q = b["questions"]
        self.assertEqual(q["projects_without_description"], ["b"])
        self.assertEqual([i["id"] for i in q["items_without_notes"]], [x])
        self.assertEqual(core.agent_status(self.c, me)["role"], "planner")
        self.assertEqual(core.capacity(self.c)["agents_idle"], [])
        with self.assertRaises(RiverError):
            core.claim(self.c, x, me)
        core.project_path(self.c, "a", self.dir.name)
        g = core.go(self.c, self.dir.name, me)
        self.assertEqual((g["role"], g["item"]["id"]), ("worker", x))

    def test_plan_refuses_a_session_that_holds_work(self):
        core.project_add(self.c, "a")
        core.register(self.c, "ag")
        core.claim(self.c, self.add("a", "x"), "ag")
        with self.assertRaises(RiverError):
            core.plan(self.c, self.dir.name, "ag")


class DueDates(Base):
    def test_due_inherits_warns_once_per_stage_and_never_reorders(self):
        core.project_add(self.c, "a")
        core.register(self.c, "mark", human=True)
        pre = self.add("a", "prerequisite", p=3)
        other = self.add("a", "other", p=2)
        goal = core.item_add(self.c, "a", "outcome", 3, after=[pre], due="2099-01-01 UTC")["id"]
        ann = core.annotate(self.c)
        self.assertEqual((ann[pre]["effective_due"], ann[pre]["due_from"]), ("2099-01-01T23:59:00Z", goal))
        self.assertIsNone(ann[pre]["due_state"])
        self.assertEqual([x["id"] for x in core.ready_list(self.c)], [other, pre])  # order unchanged
        core.item_edit(self.c, goal, due=core.iso(core.now() + timedelta(days=1)))  # inside due_warn_before
        core.activity(self.c, "mark")
        core.activity(self.c, "mark")
        alerts = [m["body"] for m in core.inbox(self.c, "mark") if m["kind"] == "alert"]
        self.assertEqual(len(alerts), 1)
        self.assertIn(f"#{goal} outcome is due soon", alerts[0])
        self.assertIn(f"Still open before it: #{pre}", alerts[0])
        self.assertEqual(core.annotate(self.c)[pre]["due_state"], "soon")
        self.c.execute("UPDATE items SET due=? WHERE id=?", (core.iso(core.now() - timedelta(minutes=1)), goal))
        core.activity(self.c, "mark")
        alerts = [m["body"] for m in core.inbox(self.c, "mark") if m["kind"] == "alert"]
        self.assertEqual(len(alerts), 1)
        self.assertIn("is past its due date", alerts[0])
        self.assertEqual(core.status(self.c)["due"][0]["due_state"], "overdue")
        core.item_edit(self.c, goal, due="none")
        self.assertIsNone(core._item(self.c, goal)["due"])

    def test_parse_due(self):
        self.assertEqual(core.parse_due("2026-10-15 UTC"), "2026-10-15T23:59:00Z")
        self.assertEqual(core.parse_due("2026-10-15 17:00 UTC"), "2026-10-15T17:00:00Z")
        self.assertIsNone(core.parse_due("none"))


class EditHistory(Base):
    def test_save_without_changes_logs_nothing(self):
        core.project_add(self.c, "a")
        x = self.add("a", "x")
        before = self.c.execute("SELECT COUNT(*) FROM events WHERE item_id=?", (x,)).fetchone()[0]
        core.item_edit(self.c, x, title="x", notes="", doer="any", project="a", context="", touches=[], check="")
        after = self.c.execute("SELECT COUNT(*) FROM events WHERE item_id=?", (x,)).fetchone()[0]
        self.assertEqual(after, before)
        core.item_edit(self.c, x, title="y", doer="ai")
        changes = [r[0] for r in self.c.execute("SELECT change FROM events WHERE item_id=? ORDER BY id", (x,))][-2:]
        self.assertEqual(changes, ["title changed", "doer any -> ai"])


class PlanFile(Base):
    PLAN = """# Launch
- P0 Live test order (human)
  - Deploy the site (human)
    1. Menu page
    2. [ ] Preorder form (ai)
        - Friday list
    3. [x] Pick a host
        - Stale child

> a quote
"""

    def test_parse_plan(self):
        rows = core.parse_plan(self.PLAN)
        self.assertEqual([(r["title"], r["priority"], r["doer"], r["parent"]) for r in rows], [
            ("Live test order", 0, "human", None), ("Deploy the site", 2, "human", 0), ("Menu page", 2, "any", 1),
            ("Preorder form", 2, "ai", 1), ("Friday list", 2, "any", 3), ("Stale child", 2, "any", 1)])
        with self.assertRaises(RiverError):
            core.parse_plan("# only a heading\n")

    def test_add_plan_links_children_to_parents(self):
        core.project_add(self.c, "shop")
        res = core.add_plan(self.c, "shop", self.PLAN, "t")
        ids = {r["title"]: r["id"] for r in res["items"]}
        ann = core.annotate(self.c)
        self.assertEqual(ann[ids["Live test order"]]["waits_on"], [ids["Deploy the site"]])
        self.assertEqual(ann[ids["Deploy the site"]]["waits_on"],
                         sorted([ids["Menu page"], ids["Preorder form"], ids["Stale child"]]))
        self.assertEqual(ann[ids["Preorder form"]]["waits_on"], [ids["Friday list"]])
        self.assertEqual(ann[ids["Friday list"]]["effective_priority"], 0)  # the outcome's P0 flows down
        self.assertEqual(core.add_plan(self.c, "shop", "a\n  b\n", dry_run=True)["items"][1]["parent"], 0)
        self.assertEqual(len(core.item_list(self.c, "shop")), 6)

    def test_add_dry_run_without_a_plan_file_is_refused_and_adds_nothing(self):
        from river import cli
        core.project_add(self.c, "shop")
        with self.assertRaisesRegex(RiverError, "--dry-run goes with --from"):
            cli.dispatch(self.c, cli.build_parser().parse_args(["add", "shop", "Try it", "--dry-run"]), "t")
        self.assertEqual(core.item_list(self.c, "shop"), [])


class Goals(Base):
    def setUp(self):
        super().setUp()
        core.project_add(self.c, "shop")
        core.register(self.c, "ag")
        core.register(self.c, "bo")

    def test_goal_lifecycle(self):
        core.goal_add(self.c, "shop", "checkout", "customers can pay", "a test order succeeds", "t")
        core.goal_add(self.c, "shop", "speed", "pages load in 1s", actor="t")
        core.goal_rank(self.c, "speed", 1)
        self.assertEqual([g["name"] for g in core.goal_list(self.c)], ["speed", "checkout"])
        core.goal_own(self.c, "checkout", "ag")
        with self.assertRaises(RiverError):
            core.goal_own(self.c, "checkout", "bo")  # one owner at a time
        a = core.item_add(self.c, "shop", "cart", actor="ag")["id"]  # tagged with the goal ag owns
        b = core.item_add(self.c, "shop", "pay", actor="bo", goals=["checkout", "speed"])["id"]
        c = core.item_add(self.c, "shop", "untagged", actor="bo")["id"]
        self.assertEqual(core.annotate(self.c)[a]["goals"], ["checkout"])
        self.assertEqual([x["id"] for x in core.item_list(self.c, goal="checkout")], [a, b])
        core.item_edit(self.c, b, untag=["speed"])
        core.item_edit(self.c, c, goals=["speed"])
        g = core.goal_show(self.c, "checkout")
        self.assertEqual((g["owner"], g["items_open"]), ("ag", [a, b]))
        with self.assertRaises(RiverError):
            core.goal_done(self.c, "checkout", "shipped", "ag")  # items still open
        with self.assertRaises(RiverError):
            core.goal_done(self.c, "checkout", "shipped", "bo")  # not the owner
        core.claim(self.c, a, "ag")
        core.done(self.c, a, "ok", "ag")
        g = core.goal_done(self.c, "checkout", "cart works; pay dropped for now", "ag", drop_open=True)
        self.assertEqual((g["status"], g["owner"], g["items_dropped"]), ("complete", None, [b]))
        self.assertEqual([x["name"] for x in core.project_show(self.c, "shop")["goals"]], ["speed"])
        core.goal_reopen(self.c, "checkout")
        self.assertEqual(core.goal_show(self.c, "checkout")["status"], "open")

    def test_owner_adds_an_item_with_no_goal(self):
        core.goal_add(self.c, "shop", "g1", actor="t")
        core.goal_own(self.c, "g1", "ag")
        a = core.item_add(self.c, "shop", "typo fix", actor="ag", goals=[])["id"]  # maxpm add --no-goal
        b = core.item_add(self.c, "shop", "goal work", actor="ag")["id"]
        ann = core.annotate(self.c)
        self.assertEqual((ann[a]["goals"], ann[b]["goals"]), ([], ["g1"]))

    def test_owner_goal_tags_only_items_in_its_project(self):
        core.project_add(self.c, "blog")
        core.goal_add(self.c, "shop", "g1", actor="t")
        core.goal_own(self.c, "g1", "ag")
        a = core.item_add(self.c, "blog", "other project", actor="ag")["id"]
        b = core.item_add(self.c, "blog", "named goal", actor="ag", goals=["g1"])["id"]
        ann = core.annotate(self.c)
        self.assertEqual((ann[a]["goals"], ann[b]["goals"]), ([], ["g1"]))

    def test_owner_lease_expires_with_notice(self):
        core.goal_add(self.c, "shop", "g1", actor="t")
        core.goal_own(self.c, "g1", "ag")
        self.c.execute("UPDATE goals SET owner_expires_at=?", (core.iso(core.now() - timedelta(minutes=1)),))
        core.activity(self.c, "bo")
        self.assertIsNone(core.goal_show(self.c, "g1")["owner"])
        self.assertIn("goal g1 expired", core.inbox(self.c, "ag")[0]["body"].replace("ownership of ", ""))


class GoalOwners(Base):
    def setUp(self):
        super().setUp()
        core.project_add(self.c, "shop", path=self.dir.name)
        for n in ("ag", "bo", "cy"):
            core.register(self.c, n)

    def go(self, who):
        return core.go(self.c, self.dir.name, actor=who)

    def test_owner_takes_goal_items_then_unblockers_then_judges_then_next_goal(self):
        core.goal_add(self.c, "shop", "checkout", "customers can pay", "a test order succeeds", "t")
        core.goal_add(self.c, "shop", "speed", "fast pages", actor="t")
        pre = core.item_add(self.c, "shop", "db index")["id"]  # outside the goal
        pay = core.item_add(self.c, "shop", "pay button", after=[pre], goals=["checkout"])["id"]
        b = self.go("ag")  # takes the top free goal; its only item waits, so it takes the unblocker
        self.assertEqual((b["role"], b.get("took_goal"), b["item"]["id"]), ("owner", True, pre))
        self.assertEqual(core.goal_show(self.c, "checkout")["owner"], "ag")
        core.done(self.c, pre, "ok", "ag")
        b = self.go("ag")
        self.assertEqual((b["role"], b["item"]["id"]), ("owner", pay))
        core.done(self.c, pay, "ok", "ag")
        b = self.go("ag")
        self.assertEqual((b["role"], b["item"], b["goal_action"]), ("owner", None, "judge"))
        core.goal_done(self.c, "checkout", "orders work", "ag")
        b = core.go(self.c, self.dir.name, actor="ag", role="owner")  # the owner continues with the next free goal
        self.assertEqual((b["goal"]["name"], b.get("took_goal")), ("speed", True))
        self.assertEqual(b["goal_action"], "judge")  # a goal with no items is not complete until its owner says so
        b = self.go("bo")  # no free goal left: today's worker behavior
        self.assertNotEqual(b["role"], "owner")

    def test_handoff_versions_briefings_and_release_give_need_a_current_one(self):
        from river import cli
        core.register(self.c, "mark", human=True)
        core.goal_add(self.c, "shop", "checkout", "customers can pay", "a test order succeeds", "t")
        a = core.item_add(self.c, "shop", "pay button", goals=["checkout"])["id"]
        b = core.item_add(self.c, "shop", "receipt mail", goals=["checkout"])["id"]
        core.goal_own(self.c, "checkout", "ag")
        core.goal_release(self.c, "checkout", "ag")  # nothing finished yet: no handoff needed
        core.goal_own(self.c, "checkout", "ag")
        core.claim(self.c, a, "ag")
        core.done(self.c, a, "ok", "ag")
        with self.assertRaisesRegex(RiverError, r"handoff of goal checkout is missing; you finished #%d" % a):
            core.goal_release(self.c, "checkout", "ag")
        with self.assertRaisesRegex(RiverError, "--no-handoff"):
            core.goal_give(self.c, "checkout", "bo", "ag")
        with self.assertRaisesRegex(RiverError, "owned by ag, who keeps its handoff"):
            core.goal_handoff(self.c, "checkout", "my view", "bo")
        with self.assertRaisesRegex(RiverError, "empty"):
            core.goal_handoff(self.c, "checkout", "  ", "ag")
        core.goal_handoff(self.c, "checkout", "Stripe test keys in .env.\nLeft: receipts.", "ag")
        self.assertIsNone(core.goal_show(self.c, "checkout")["handoff_due"])
        # The next session on the goal's items reads it; the owner's briefing shows it too.
        core.goal_give(self.c, "checkout", "bo", "ag")
        g = self.go("bo")
        self.assertEqual((g["role"], g["item"]["id"]), ("owner", b))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.render_go(g)
        self.assertIn("HANDOFF v1 (ag, ", out.getvalue())
        self.assertIn("    Left: receipts.", out.getvalue())
        # An owner that finished nothing of the goal releases it with no new handoff.
        core.release(self.c, b, actor="bo")
        core.goal_release(self.c, "checkout", "bo")
        # A person or the manager may write it; every version stays.
        core.goal_handoff(self.c, "checkout", "v2 from mark", "mark")
        h = core.goal_handoff(self.c, "checkout", version=1)
        self.assertEqual((h["handoff"]["text"].splitlines()[0], [v["version"] for v in h["versions"]]),
                         ("Stripe test keys in .env.", [1, 2]))
        # A worker on an item of a goal it does not own reads the handoff in its item.
        core.goal_edit(self.c, "checkout", shared=True, actor="mark")
        g = self.go("cy")
        self.assertEqual((g["item"]["id"], [(h["goal"], h["version"]) for h in g["handoffs"]]), (b, [("checkout", 2)]))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.render_go(g)
        self.assertIn("goal checkout: HANDOFF v2 (mark, ", out.getvalue())
        # A reason lets an owner go without a current handoff, and it goes in the history.
        core.goal_edit(self.c, "checkout", shared=False, actor="mark")
        core.goal_own(self.c, "checkout", "cy")
        self.c.execute("UPDATE goal_handoffs SET created_at=? WHERE version=2",
                       (core.iso(core.now() - timedelta(minutes=1)),))
        core.done(self.c, b, "ok", "cy")
        with self.assertRaisesRegex(RiverError, r"\(v2, .*\) is older than #%d receipt mail" % b):
            core.goal_release(self.c, "checkout", "cy")
        core.goal_release(self.c, "checkout", "cy", no_handoff="the goal is finished; nothing to hand on")
        self.assertTrue(any("release goal checkout without a current handoff: the goal is finished" in e["change"]
                            for e in core.recent_events(self.c)))

    def test_subgoals_one_level_and_the_parent_handoff_comes_first(self):
        from river import cli
        core.project_add(self.c, "other")
        core.goal_add(self.c, "shop", "checkout", "customers can pay", actor="t")
        core.goal_add(self.c, "shop", "refunds", "customers get money back", actor="t", parent="checkout")
        with self.assertRaisesRegex(RiverError, "one level. Use its parent: --parent checkout"):
            core.goal_add(self.c, "shop", "partial", actor="t", parent="refunds")
        with self.assertRaisesRegex(RiverError, "in its parent's project"):
            core.goal_add(self.c, "other", "elsewhere", actor="t", parent="checkout")
        self.assertEqual((core.goal_show(self.c, "refunds")["parent"], core.goal_show(self.c, "checkout")["subgoals"]),
                         ("checkout", ["refunds"]))
        core.goal_handoff(self.c, "checkout", "Payments use Stripe.", "t")
        core.goal_handoff(self.c, "refunds", "Refunds go through the same client.", "t")
        x = core.item_add(self.c, "shop", "refund button", goals=["refunds"])["id"]
        # A worker on the sub-goal's item reads the parent's handoff, then the sub-goal's.
        core.goal_edit(self.c, "refunds", shared=True, actor="t")
        g = self.go("ag")
        self.assertEqual((g["item"]["id"], [h["goal"] for h in g["handoffs"]]), (x, ["checkout", "refunds"]))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.render_go(g)
            cli.render(cli.build_parser().parse_args(["goal", "show", "refunds"]), core.goal_show(self.c, "refunds"))
        text = out.getvalue()
        self.assertLess(text.index("Payments use Stripe."), text.index("Refunds go through the same client."))
        self.assertIn("sub-goal of checkout", text)
        self.assertIn("parent goal checkout: HANDOFF v1", text)
        self.assertEqual(core.context_brief(self.c, "refunds")["parent_handoff"]["text"], "Payments use Stripe.")
        with self.assertRaisesRegex(RiverError, "open sub-goals: refunds"):
            core.goal_done(self.c, "checkout", "done", "t")

    def test_waiting_goal_does_other_work_and_names_the_blocker_owner(self):
        core.goal_add(self.c, "shop", "g1", "one", actor="t")
        core.goal_add(self.c, "shop", "g2", "two", actor="t")
        core.goal_own(self.c, "g2", "bo")
        blocker = core.item_add(self.c, "shop", "shared lib", goals=["g2"])["id"]
        mine = core.item_add(self.c, "shop", "feature", after=[blocker], goals=["g1"])["id"]
        other = core.item_add(self.c, "shop", "unrelated")["id"]
        core.claim(self.c, blocker, "bo")
        core.goal_own(self.c, "g1", "ag")
        b = self.go("ag")  # owns g1; its item waits on bo's work, so it takes other ready work
        self.assertEqual((b["goal"]["name"], b["role"], b["item"]["id"]), ("g1", "worker", other))
        held = b["goal"]["blockers_held"]
        self.assertEqual([(x["id"], x["assignee"], x["goal"], x["goal_owner"]) for x in held], [(blocker, "bo", "g2", "bo")])
        m = core.message(self.c, "question", "when is the lib ready?", goal="g2", actor="ag")[0]
        self.assertEqual(m["to_agent"], "bo")
        self.assertEqual(core.goal_show(self.c, "g1")["items_open"], [mine])

    def test_plain_go_never_forces_a_goal(self):
        core.goal_add(self.c, "shop", "legal", "pages live", actor="t")
        core.item_add(self.c, "shop", "sign the contract", doer="human", goals=["legal"])
        loose = core.item_add(self.c, "shop", "fix typo", priority=1)["id"]
        b = self.go("ag")  # the goal has only a human item: ag works the untagged item, owns nothing
        self.assertEqual((b["role"], b["item"]["id"], b.get("took_goal")), ("worker", loose, None))
        self.assertIsNone(core.goal_show(self.c, "legal")["owner"])
        core.goal_add(self.c, "shop", "speed", "fast", actor="t")
        idx = core.item_add(self.c, "shop", "add index", priority=0, goals=["speed"])["id"]
        b = self.go("bo")  # the best ready work is a goal item: bo owns that goal
        self.assertEqual((b["role"], b["item"]["id"], b["goal"]["name"]), ("owner", idx, "speed"))

    def test_owner_hears_when_others_add_claim_or_finish_its_items(self):
        core.goal_add(self.c, "shop", "g1", actor="t")
        core.goal_own(self.c, "g1", "ag")
        x = core.item_add(self.c, "shop", "task", actor="bo", goals=["g1"])["id"]
        with self.assertRaisesRegex(RiverError, "reserved for ag, who owns goal g1"):
            core.claim(self.c, x, "bo")  # the owner's agent items are reserved for it
        core.register(self.c, "mk", human=True)  # a person is not bound by the reservation
        core.claim(self.c, x, "mk")
        core.done(self.c, x, "shipped", "mk")
        notes = [m["body"] for m in core.inbox(self.c, "ag") if m["kind"] == "notice"]
        self.assertEqual([n.split(" #")[0] for n in notes], ["bo added", "mk claimed", "mk finished"])
        core.goal_give(self.c, "g1", "cy", "ag")
        self.assertEqual(core.goal_show(self.c, "g1")["owner"], "cy")

    def test_goal_frees_when_owner_is_away(self):
        core.goal_add(self.c, "shop", "g1", actor="t")
        core.goal_own(self.c, "g1", "ag")
        self.c.execute("UPDATE goals SET owner_expires_at=?", (core.iso(core.now() - timedelta(seconds=1)),))
        core.activity(self.c, "bo")
        self.assertIsNone(core.goal_show(self.c, "g1")["owner"])


class GoalClaim(Base):
    """A goal owner's claim on the goal's items (#285)."""

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "shop", path=self.dir.name)
        for n in ("ag", "bo"):
            core.register(self.c, n)
        core.goal_add(self.c, "shop", "g1", actor="t")
        self.x = core.item_add(self.c, "shop", "agent task", goals=["g1"])["id"]
        self.h = core.item_add(self.c, "shop", "person step", doer="human", goals=["g1"])["id"]
        self.loose = core.item_add(self.c, "shop", "other work", priority=3)["id"]

    def left(self, when):
        return core.parse_iso(when) - core.now()

    def test_owner_can_give_a_goal_item_and_the_other_agent_claims_it(self):
        core.goal_own(self.c, "g1", "ag")
        core.register(self.c, "cy")
        core.give(self.c, self.x, "bo", "ag")
        it = core._item(self.c, self.x)
        self.assertEqual((it["reserved_for"], it["reserved_by"]), ("bo", "ag"))
        self.assertIsNotNone(it["reserved_until"])  # it comes back to the goal if bo does not take it
        with self.assertRaisesRegex(RiverError, "reserved for bo"):
            core.claim(self.c, self.x, "cy")
        self.assertEqual(core.claim(self.c, self.x, "bo")["assignee"], "bo")
        with self.assertRaisesRegex(RiverError, "not yours to give"):
            core.give(self.c, self.loose, "bo", "cy")  # nobody's goal item: nothing to give

    def test_a_goal_item_pushed_to_another_agent_can_be_accepted(self):
        core.goal_own(self.c, "g1", "ag")
        core.push(self.c, self.x, "bo", actor="ag")
        self.assertEqual(core.accept(self.c, self.x, "bo")["assignee"], "bo")

    def test_owner_gets_the_goal_items_others_do_not(self):
        core.goal_own(self.c, "g1", "ag")
        ann = core.annotate(self.c)
        self.assertEqual((ann[self.x]["reserved_for"], ann[self.x]["goal_reserved"]), ("ag", "g1"))
        self.assertIsNone(ann[self.h]["reserved_for"])  # a person's item stays on their list
        self.assertEqual([a["id"] for a in core.next_item(self.c, actor="bo", limit=5)], [self.loose])
        with self.assertRaisesRegex(RiverError, "reserved for ag, who owns goal g1"):
            core.claim(self.c, self.x, "bo")
        self.assertEqual(core.go(self.c, self.dir.name, "bo")["item"]["id"], self.loose)
        core.release(self.c, self.loose, actor="bo")
        b = core.go(self.c, self.dir.name, "ag")
        self.assertEqual(b["item"]["id"], self.x)
        lease = self.left(core._item(self.c, self.x)["lease_expires_at"])
        self.assertTrue(timedelta(hours=3, minutes=59) < lease <= timedelta(hours=4))  # goal_lease, not lease_ttl

    def test_a_shared_goal_has_no_owner_and_its_items_are_open_to_every_agent(self):
        core.goal_own(self.c, "g1", "ag")
        with self.assertRaisesRegex(RiverError, "a person or a manager decides"):
            core.goal_edit(self.c, "g1", shared=True, actor="bo")  # an agent that does not own it
        core.register(self.c, "boss")
        self.c.execute("UPDATE agents SET role='manager' WHERE name='boss'")
        g = core.goal_edit(self.c, "g1", shared=True, actor="boss")  # the owner goes, and hears why
        self.assertEqual((g["shared"], g["owner"]), (1, None))
        self.assertIn("made goal g1 a shared goal", core.inbox(self.c, "ag")[-1]["body"])
        self.assertIsNone(core.annotate(self.c)[self.x]["reserved_for"])
        with self.assertRaisesRegex(RiverError, "goal g1 is shared: nobody owns it"):
            core.goal_own(self.c, "g1", "ag")
        b = core.go(self.c, self.dir.name, "ag", role="owner")  # --role owner skips it
        self.assertEqual((b["role"], b["why"]), ("idle", "no goal here is free"))
        y = core.item_add(self.c, "shop", "second lane", goals=["g1"])["id"]
        b = core.go(self.c, self.dir.name, "ag")  # plain go: a worker on the goal's item, not its owner
        self.assertEqual((b["role"], b["item"]["id"], b.get("goal"), b["shared_goals"]), ("worker", self.x, None, ["g1"]))
        b = core.go(self.c, self.dir.name, "bo")  # another agent works on the goal at the same time
        self.assertEqual((b["role"], b["item"]["id"]), ("worker", y))
        self.assertIsNone(core.goal_show(self.c, "g1")["owner"])
        with self.assertRaisesRegex(RiverError, "a person or a manager decides"):
            core.goal_edit(self.c, "g1", shared=False, actor="ag")
        core.register(self.c, "mk", human=True)
        self.assertEqual(core.goal_edit(self.c, "g1", shared=False, actor="mk")["shared"], 0)
        self.assertEqual(core.goal_own(self.c, "g1", "bo")["owner"], "bo")
        self.assertEqual(core.goal_edit(self.c, "g1", shared=True, actor="bo")["owner"], None)  # the owner hands it to all
        self.assertEqual(core.goal_add(self.c, "shop", "g2", shared=True)["shared"], 1)

    def test_a_goal_is_free_when_its_owner_is_gone_or_ends(self):
        core.goal_own(self.c, "g1", "ag")
        self.c.execute("UPDATE agents SET last_seen=? WHERE name='ag'", (core.iso(core.now() - timedelta(hours=25)),))
        self.assertEqual(core.claim(self.c, self.x, "bo")["assignee"], "bo")  # gone_after passed: nothing is reserved
        self.assertIsNone(core.goal_show(self.c, "g1")["owner"])
        core.release(self.c, self.x, actor="bo")
        core.goal_own(self.c, "g1", "bo")
        core.unregister(self.c, "bo")
        self.assertIsNone(core.goal_show(self.c, "g1")["owner"])

    def test_lease_length_renewal_release_and_expiry(self):
        core.goal_own(self.c, "g1", "ag", lease="6h")
        self.assertTrue(self.left(core.goal_show(self.c, "g1")["owner_expires_at"]) > timedelta(hours=5, minutes=59))
        self.c.execute("UPDATE goals SET owner_expires_at=?", (core.iso(core.now() + timedelta(minutes=5)),))
        core.activity(self.c, "ag")  # any command of the owner renews the claim for its lease
        self.assertTrue(self.left(core.goal_show(self.c, "g1")["owner_expires_at"]) > timedelta(hours=5, minutes=59))
        core.goal_release(self.c, "g1", "ag")
        self.assertIsNone(core.annotate(self.c)[self.x]["reserved_for"])
        core.goal_own(self.c, "g1", "ag")  # a new ownership starts from goal_lease again
        self.assertTrue(self.left(core.goal_show(self.c, "g1")["owner_expires_at"]) <= timedelta(hours=4))
        self.c.execute("UPDATE goals SET owner_expires_at=?", (core.iso(core.now() - timedelta(seconds=1)),))
        self.assertIsNone(core.annotate(self.c)[self.x]["reserved_for"])  # expired: free before any sweep
        core.activity(self.c, "bo")
        self.assertIn("open to every agent", [m for m in core.inbox(self.c, "ag") if m["kind"] == "notice"][-1]["body"])
        core.claim(self.c, self.x, "bo")
        with self.assertRaises(RiverError):
            core.goal_own(self.c, "g1", "ag", lease="soon")


class DbLocation(unittest.TestCase):
    def test_the_queue_is_one_file_in_the_home_folder_or_where_the_variable_says(self):
        from pathlib import Path
        old = os.environ.pop("MAXPM_DB", None)
        try:
            self.assertEqual(core.HOME_DB, Path("~/.maximizepm/maxpm.db"))
            self.assertEqual(core.db_path(), core.HOME_DB.expanduser())
            os.environ["MAXPM_DB"] = "/tmp/other.db"
            self.assertEqual(core.db_path(), Path("/tmp/other.db"))
            self.assertIn("set by MAXPM_DB", core.queue_note())
        finally:
            os.environ.pop("MAXPM_DB", None)
            if old is not None:
                os.environ["MAXPM_DB"] = old


class SkillsInstall(unittest.TestCase):
    def test_link_copy_and_refuse_real_folder(self):
        from pathlib import Path
        from river import cli
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            lines = cli.install_skills(dest)
            self.assertTrue((dest / "maxpm").is_symlink())
            self.assertTrue((dest / "maxpm-planner" / "SKILL.md").is_file())
            cli.install_skills(dest)  # again: links are replaced
            (dest / "maxpm").unlink()
            (dest / "maxpm").mkdir()
            with self.assertRaises(RiverError):
                cli.install_skills(dest)  # a real folder may hold edits
            cli.install_skills(dest, copy=True, force=True)
            self.assertFalse((dest / "maxpm").is_symlink())
            self.assertEqual(len(lines), 3)  # maxpm, maxpm-planner, maxpm-manager
            self.assertTrue((dest / "maxpm-manager" / "SKILL.md").is_file())


class DecisionFormat(Base):
    def test_prompt_carries_the_decision_form(self):
        core.project_add(self.c, "a")
        core.register(self.c, "mark", human=True)
        x = core.item_add(self.c, "a", "Pick a plan", doer="human", context="Two plans in docs/plans.md")["id"]
        p = core.prompt_for(self.c, x, "mark")
        for part in ("Decision <n> of <total>", "Why it matters", "My recommendation", "Your answer",
                     "one decision at a time"):
            self.assertIn(part, p)


class Sessions(Base):
    def test_session_name_recorded_and_shown(self):
        core.project_add(self.c, "a", path=self.dir.name)
        core.register(self.c, "ag", session="toolscaledcore-60")
        self.assertEqual(core.who(self.c)[0]["session"], "toolscaledcore-60")
        b = core.go(self.c, self.dir.name)
        self.assertIsNone(b["session"])  # a new agent: the briefing asks for it
        b = core.go(self.c, self.dir.name, actor=b["agent"], session="web-7")
        self.assertEqual(b["session"], "web-7")
        for bad in ("", "  ", "x" * 129, "two\x07words", "name [not a ref!]"):
            with self.assertRaises(RiverError):
                core.set_session(self.c, "ag", bad)
        r = core.set_session(self.c, "ag", "#581 A goal can be marked  no owner [412676]")  # a name river gave
        self.assertEqual((r["session"], r["session_ref"]), ("#581 A goal can be marked no owner", "412676"))
        r = core.set_session(self.c, "ag", "toolscaledcore-90 [46d1d3]")
        self.assertEqual((r["session"], r["session_ref"]), ("toolscaledcore-90", "46d1d3"))
        r = core.set_session(self.c, "ag", "toolscaledcore-90", ref="922c83")
        self.assertEqual(r["session_ref"], "922c83")


class WhoFile(Base):
    def test_who_file_matches_held_touches(self):
        root = self.dir.name
        core.project_add(self.c, "a", path=root)
        core.register(self.c, "ag")
        core.register(self.c, "bo")
        x = core.item_add(self.c, "a", "core work", actor="ag", touches="river/core.py, tests/")["id"]
        y = core.item_add(self.c, "a", "page work", actor="bo", touches="river/static/index.html")["id"]
        core.item_add(self.c, "a", "open, not held", actor="bo", touches="river/core.py")
        core.claim(self.c, x, "ag")
        core.claim(self.c, y, "bo")
        names = lambda f, cwd=None: [(a["name"], [t["id"] for t in a["touching"]]) for a in core.who(self.c, file=f, cwd=cwd)]
        self.assertEqual(names("river/core.py"), [("ag", [x])])
        self.assertEqual(names("tests/test_core.py"), [("ag", [x])])  # inside a touched directory
        self.assertEqual(names("river"), [("ag", [x]), ("bo", [y])])  # a directory holds touched files
        self.assertEqual(names(os.path.join(root, "river", "core.py")), [("ag", [x])])
        self.assertEqual(names("core.py", cwd=os.path.join(root, "river")), [("ag", [x])])
        self.assertEqual(names("/elsewhere/river/core.py"), [])
        self.assertEqual(names("README.md"), [])


class PathsOverlap(unittest.TestCase):
    def test_windows_backslashes_match_forward_slashes(self):
        self.assertTrue(core._paths_overlap("C:\\repo\\river\\core.py", "C:/repo/river"))
        self.assertTrue(core._paths_overlap("river\\static\\app.js", "river/static/"))
        self.assertFalse(core._paths_overlap("river\\static2", "river/static"))


class KeepRelease(Base):
    def setUp(self):
        super().setUp()
        core.project_add(self.c, "a", path=self.dir.name)
        core.register(self.c, "ag")
        core.register(self.c, "bo")
        self.p = self.add("a", "parent")
        core.claim(self.c, self.p, "ag")

    def test_keep_reserves_and_resumes(self):
        n = core.item_add(self.c, "a", "fix", actor="ag", blocks=self.p, mode="keep")["id"]
        st = core._item(self.c, self.p)
        self.assertEqual((st["status"], st["assignee"]), ("held", "ag"))
        self.assertEqual(core._item(self.c, n)["reserved_for"], "ag")
        self.assertEqual(core.next_item(self.c, actor="bo"), [])
        with self.assertRaises(RiverError):
            core.claim(self.c, n, "bo")
        g = core.go(self.c, self.dir.name, "ag")  # max_leases 1: the hold does not use it up
        self.assertEqual(g["item"]["id"], n)
        res = core.done(self.c, n, "ok", "ag")
        self.assertEqual(res["resumed"], [self.p])
        st = core._item(self.c, self.p)
        self.assertEqual((st["status"], st["assignee"]), ("in_progress", "ag"))
        self.assertIsNotNone(st["lease_expires_at"])

    def test_release_with_a_note_that_names_an_item_it_does_not_wait_on_gives_a_hint(self):
        import contextlib
        import io
        from river import cli
        other, far = self.add("a", "the other work"), self.add("a", "work behind a link")
        gone = self.add("a", "finished work")
        core.claim(self.c, gone, "bo")
        core.done(self.c, gone, "ok", "bo")
        res = core.release(self.c, self.p, f"waits on #{other}; see also #{gone}, #{self.p} and #9999", "ag")
        self.assertEqual(res["hint"], f"the note names #{other}, but #{self.p} does not wait on it, so go gives "
                                      f"#{self.p} out again; add the link: maxpm dep {self.p} --on {other}")
        # ag holds the item again, also while it waits on another item.
        hold = lambda: self.c.execute("UPDATE items SET status='in_progress', assignee='ag' WHERE id=?", (self.p,))
        # Only a hint: the release went through, and no link was added.
        st = core._item(self.c, self.p)
        self.assertEqual((st["status"], st["assignee"], core._open_prereqs(self.c, self.p)), ("open", None, []))
        # With the link, and through another open item, the item waits: no hint. Two names: both commands.
        core.dep_add(self.c, self.p, [other])
        core.dep_add(self.c, other, [far])
        hold()
        self.assertNotIn("hint", core.release(self.c, self.p, f"waits on #{other} and on #{far}", "ag"))
        free, free2 = self.add("a", "free one"), self.add("a", "free two")
        for note in (None, "no item named", f"only #{self.p} itself"):
            hold()
            self.assertNotIn("hint", core.release(self.c, self.p, note, "ag"))
        hold()
        res = core.release(self.c, self.p, f"needs #{free2} and #{free}", "ag")
        self.assertIn(f"does not wait on them, so go gives #{self.p} out again; add the link: "
                      f"maxpm dep {self.p} --on {free} ; maxpm dep {self.p} --on {free2}", res["hint"])
        # The command prints it after the item.
        hold()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            a = cli.build_parser().parse_args(["--as", "ag", "release", str(self.p), "--note", f"waits on #{free}"])
            cli.render(a, cli.dispatch(self.c, a, "ag"))
        self.assertIn(f"HINT: the note names #{free}, but #{self.p} does not wait on it", out.getvalue())

    def test_default_mode_releases(self):
        n = core.item_add(self.c, "a", "big", actor="ag", blocks=self.p)["id"]
        st = core._item(self.c, self.p)
        self.assertEqual((st["status"], st["assignee"]), ("open", None))
        self.assertIsNone(core._item(self.c, n)["reserved_for"])
        self.assertEqual([x["id"] for x in core.next_item(self.c, actor="bo")], [n])

    def test_keep_over_limit_releases_and_marks_replan(self):
        core.config_set(self.c, "keep_prereq_limit", "1")
        core.item_add(self.c, "a", "one", actor="ag", blocks=self.p, mode="keep")
        core.item_add(self.c, "a", "two", actor="ag", blocks=self.p, mode="keep")
        st = core._item(self.c, self.p)
        self.assertEqual((st["status"], st["replan"]), ("open", 1))
        self.assertEqual([r for r in self.c.execute("SELECT id FROM items WHERE reserved_for IS NOT NULL")], [])

    def test_replan_threshold_marks_item_with_late_prerequisites(self):
        core.config_set(self.c, "replan_threshold", "2")
        core.item_add(self.c, "a", "one", actor="ag", blocks=self.p, mode="keep")
        self.assertEqual(core._item(self.c, self.p)["replan"], 0)
        core.item_add(self.c, "a", "two", actor="ag", blocks=self.p, mode="keep")
        st = core._item(self.c, self.p)
        self.assertEqual((st["status"], st["replan"], st["late_prereqs"]), ("held", 1, 2))  # still kept
        q = core.plan(self.c, self.dir.name, "bo")["questions"]
        self.assertEqual([(x["id"], x["late_prereqs"]) for x in q["replan"]], [(self.p, 2)])
        core.replanned(self.c, self.p, "split into two", "ag")
        st = core._item(self.c, self.p)
        self.assertEqual((st["replan"], st["late_prereqs"]), (0, 0))
        # Prerequisites added to an item nobody holds are planning, not late work.
        other = self.add("a", "unclaimed")
        for t in ("x", "y", "z"):
            core.item_add(self.c, "a", t, actor="bo", blocks=other)
        self.assertEqual(core._item(self.c, other)["late_prereqs"], 0)

    def test_hold_expiry_releases_with_notice_and_keep_restores(self):
        n = core.item_add(self.c, "a", "fix", actor="ag", blocks=self.p, mode="keep")["id"]
        self.c.execute("UPDATE items SET hold_expires_at=? WHERE id=?", (core.iso(core.now() - timedelta(minutes=1)), self.p))
        core.activity(self.c, "bo")
        self.assertEqual(core._item(self.c, self.p)["status"], "open")
        self.assertIsNone(core._item(self.c, n)["reserved_for"])
        self.assertEqual([m["kind"] for m in core.inbox(self.c, "ag")], ["notice"])
        core.keep(self.c, self.p, "ag")
        self.assertEqual(core._item(self.c, self.p)["status"], "held")
        self.assertEqual(core._item(self.c, n)["reserved_for"], "ag")
        core.release(self.c, self.p, actor="ag")
        self.assertIsNone(core._item(self.c, n)["reserved_for"])
        core.claim(self.c, n, "bo")
        with self.assertRaises(RiverError):
            core.keep(self.c, self.p, "ag")  # bo holds the prerequisite now

    def test_wait_on_a_person_ends_after_human_wait_max(self):
        core.register(self.c, "mark", human=True)
        other = self.add("a", "other work")
        h = core.item_add(self.c, "a", "decide", doer="human", actor="ag", blocks=self.p, mode="keep")
        left = core.parse_iso(h["holder_wait"]["until"]) - core.now()
        self.assertTrue(timedelta(minutes=29) < left <= timedelta(minutes=30))  # human_wait_max, not hold_ttl
        b = core.go(self.c, self.dir.name, "ag")
        self.assertEqual((b["item"]["id"], [x["id"] for x in b["human_wait"]["on"]]), (self.p, [h["id"]]))
        # Commands renew a hold, but never past human_wait_max after the wait began.
        self.c.execute("UPDATE items SET held_at=? WHERE id=?", (core.iso(core.now() - timedelta(minutes=29)), self.p))
        self.c.execute("UPDATE items SET created_at=? WHERE id=?", (core.iso(core.now() - timedelta(minutes=29)), h["id"]))
        core.activity(self.c, "ag")
        left = core.parse_iso(core._item(self.c, self.p)["hold_expires_at"]) - core.now()
        self.assertTrue(left <= timedelta(minutes=1))
        self.c.execute("UPDATE items SET hold_expires_at=? WHERE id=?", (core.iso(core.now() - timedelta(seconds=1)), self.p))
        core.activity(self.c, "bo")
        st = core._item(self.c, self.p)
        self.assertEqual((st["status"], st["assignee"]), ("open", None))
        self.assertEqual(core._open_prereqs(self.c, self.p), [h["id"]])  # still waits on the person
        self.assertIn("maxpm go", core.inbox(self.c, "ag")[0]["body"])
        self.assertTrue(any(m["kind"] == "alert" and f"#{self.p}" in m["body"] for m in core.inbox(self.c, "mark")))
        self.assertEqual(core.go(self.c, self.dir.name, "ag")["item"]["id"], other)

    def test_hold_without_a_person_keeps_hold_ttl(self):
        core.item_add(self.c, "a", "fix", actor="ag", blocks=self.p, mode="keep")
        left = core.parse_iso(core._item(self.c, self.p)["hold_expires_at"]) - core.now()
        self.assertTrue(left > timedelta(hours=1))

    def test_dep_with_mode_and_without(self):
        other = self.add("a", "existing")
        core.dep_add(self.c, self.p, [other], "ag")  # no mode: the parent stays as it was
        self.assertEqual(core._item(self.c, self.p)["status"], "in_progress")
        core.dep_add(self.c, self.p, [other], "ag", mode="release")
        self.assertEqual(core._item(self.c, self.p)["status"], "open")


class NeedsYou(Base):
    def setUp(self):
        super().setUp()
        core.project_add(self.c, "a")
        core.register(self.c, "mark", human=True)
        core.register(self.c, "ag")

    def sync(self):
        with core.tx(self.c):
            core.sync_needs_you(self.c)

    def test_human_item_opens_when_ready_and_closes_when_claimed(self):
        core.config_set(self.c, "notify_channels", "mac,ntfy")
        prep = self.add("a", "prep")
        sign = self.add("a", "sign", doer="human", after=[prep])
        self.sync()
        self.assertEqual(core.needs_you(self.c), [])
        core.claim(self.c, prep, "ag")
        core.done(self.c, prep, None, "ag")
        self.sync()
        self.sync()  # idempotent: still one event
        ev = core.needs_you(self.c)
        self.assertEqual([(e["kind"], e["item_id"]) for e in ev], [("item", sign)])
        self.assertEqual(sorted(n["channel"] for n in ev[0]["notifications"]), ["mac", "ntfy"])
        self.assertEqual(len(core.outbox(self.c)), 2)
        core.claim(self.c, sign, "mark")
        self.sync()
        self.assertEqual(core.needs_you(self.c), [])
        closed = core.needs_you(self.c, include_closed=True)
        self.assertEqual(closed[0]["close_reason"], "claimed")
        self.assertEqual(core.outbox(self.c), [])  # closed before it was sent: nothing to send

    def test_most_important_first(self):
        low = self.add("a", "low", p=3, doer="human")
        mid = self.add("a", "mid", p=2, doer="human")
        feeds = self.add("a", "feeds a P0", p=3, doer="human")
        top = self.add("a", "top", p=0, after=[feeds])
        q = core.send(self.c, "question", "which plan?", to="mark", actor="ag")
        alert = core.send(self.c, "alert", "disk full", to="mark", actor="ag")
        self.sync()
        ev = core.needs_you(self.c, human="mark")
        self.assertEqual([e["message_id"] or e["item_id"] for e in ev], [q["id"], alert["id"], feeds, mid, low])
        first_item = ev[2]
        self.assertEqual((first_item["priority"], first_item["priority_from"], first_item["unblocks_count"]), (0, top, 1))
        self.assertEqual(ev[4]["priority"], 3)
        self.assertIsNone(ev[0]["priority"])

    def test_question_to_human_and_answer(self):
        q = core.send(self.c, "question", "which plan?", to="mark", actor="ag")
        core.send(self.c, "question", "agents only", to="ag", actor="mark")
        self.sync()
        ev = core.needs_you(self.c, human="mark")
        self.assertEqual([(e["kind"], e["message_id"], e["human"]) for e in ev], [("message", q["id"], "mark")])
        self.assertEqual(core.outbox(self.c), [])  # no channels configured: nothing to send
        core.answer(self.c, q["id"], "B", actor="mark")
        self.sync()
        self.assertEqual(core.needs_you(self.c, human="mark"), [])
        self.assertEqual(core.needs_you(self.c, include_closed=True)[0]["close_reason"], "answered")

    def test_outbox_retry_then_give_up(self):
        core.config_set(self.c, "notify_channels", "ntfy")
        self.add("a", "sign", doer="human")
        self.sync()
        (n,) = core.outbox(self.c)
        core.outbox_mark(self.c, n["id"], False, "timeout")
        self.assertEqual(core.outbox(self.c)[0]["attempts"], 1)
        for _ in range(core.NOTIFY_MAX_ATTEMPTS - 1):
            core.outbox_mark(self.c, n["id"], False, "timeout")
        self.assertEqual(core.outbox(self.c), [])
        with self.assertRaises(RiverError):
            core.config_set(self.c, "notify_channels", "Bad Name")

    def test_sent_once(self):
        core.config_set(self.c, "notify_channels", "mac")
        self.add("a", "sign", doer="human")
        self.sync()
        (n,) = core.outbox(self.c)
        core.outbox_mark(self.c, n["id"], True)
        self.sync()
        self.assertEqual(core.outbox(self.c), [])


class Dispatch(Base):
    def setUp(self):
        super().setUp()
        from river import notify
        self.notify = notify
        self.sent, self.fail = [], []
        notify.register_channel("fake", lambda conn: self._send)
        core.project_add(self.c, "a")
        core.config_set(self.c, "notify_channels", "fake")

    def tearDown(self):
        self.notify.ADAPTERS.pop("fake", None)
        super().tearDown()

    def _send(self, title, body, url):
        if self.fail:
            raise OSError(self.fail[0])
        self.sent.append((title, body, url))

    def age_outbox(self, seconds):
        self.c.execute("UPDATE notifications SET created_at=?", (core.iso(core.now() - timedelta(seconds=seconds)),))

    def test_batches_after_window_and_sends_once(self):
        i = self.add("a", "sign", doer="human")
        self.add("a", "pay", doer="human")
        res = self.notify.run(self.c)
        self.assertEqual((res[0]["sent"], res[0]["rows"]), (False, 2))  # inside the batch window
        self.assertEqual(self.sent, [])
        self.age_outbox(61)
        res = self.notify.run(self.c)
        self.assertTrue(res[0]["sent"])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("2 things need you", self.sent[0][0])
        self.assertEqual(self.notify.run(self.c, now_=True), [])  # nothing left
        self.assertEqual(len(self.sent), 1)
        self.assertNotEqual(i, None)

    def test_single_links_to_item_and_failures_retry(self):
        i = self.add("a", "sign", doer="human")
        self.fail.append("network down")
        res = self.notify.run(self.c, now_=True)
        self.assertIn("network down", res[0]["error"])
        st = self.notify.status(self.c)["channels"][0]
        self.assertEqual((st["channel"], st["pending"]), ("fake", 1))
        self.assertIn("network down", st["last_error"])
        self.fail.clear()
        self.notify.run(self.c, now_=True)
        self.assertEqual(self.sent[0][2], f"http://127.0.0.1:8765/#item-{i}")
        self.assertEqual(self.notify.status(self.c)["channels"][0]["pending"], 0)

    def test_test_and_unknown_channel(self):
        self.assertTrue(self.notify.test(self.c, "fake")["ok"])
        with self.assertRaises(RiverError):
            self.notify.test(self.c, "nope")
        core.config_set(self.c, "notify_batch_window", "0s")
        with self.assertRaises(RiverError):
            core.config_set(self.c, "notify_interval", "often")


class Ntfy(Base):
    def test_setup_masks_and_posts(self):
        from unittest import mock
        from river import notify
        out = notify.setup_ntfy(self.c)
        topic = core.setting(self.c, "ntfy_topic")
        self.assertTrue(topic.startswith("maxpm-") and len(topic) > 12)
        self.assertIn(topic, "\n".join(out["subscribe"]))
        self.assertIn("ntfy", core._channels(core.setting(self.c, "notify_channels")))
        listed = [o for o in core.config_list(self.c)["overrides"] if o["key"] == "ntfy_topic"]
        self.assertNotIn(topic, listed[0]["value"])
        self.assertNotIn(topic, " ".join(e["change"] for e in core.recent_events(self.c)))
        core.config_set(self.c, "ntfy_token", "tk_secret_123")
        seen = []

        class Resp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake(req, timeout):
            seen.append(req)
            return Resp()

        with mock.patch("urllib.request.urlopen", fake):
            notify.ADAPTERS["ntfy"](self.c)("Überweisung fällig", "body ✓", "http://127.0.0.1:8765/#item-3")
            notify.ADAPTERS["ntfy"](self.c)("t", "b", "https://claude.ai/code/session_01Abc")
            core.config_set(self.c, "ntfy_click", "https://example.org/river")
            notify.ADAPTERS["ntfy"](self.c)("t", "b", None)
        req, session, fallback = seen
        # A phone cannot open the local page: it gets ntfy_click. A session link goes through as it is.
        self.assertEqual(session.get_header("Click"), "https://claude.ai/code/session_01Abc")
        self.assertEqual(fallback.get_header("Click"), "https://example.org/river")
        with self.assertRaises(RiverError):
            core.config_set(self.c, "ntfy_click", "claude.ai")
        self.assertEqual(req.full_url, f"https://ntfy.sh/{topic}")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.data, "body ✓".encode())
        self.assertTrue(req.get_header("Title").startswith("=?UTF-8?B?"))
        self.assertEqual(req.get_header("Click"), "https://claude.ai/code")
        self.assertEqual(req.get_header("Authorization"), "Bearer tk_secret_123")
        # Priority high: below it the Android app shows no pop-over banner (#480).
        self.assertEqual(req.get_header("Priority"), "high")
        core.config_set(self.c, "ntfy_priority", "urgent")
        with mock.patch("urllib.request.urlopen", fake):
            notify.ADAPTERS["ntfy"](self.c)("t", "b", None)
        self.assertEqual(seen[-1].get_header("Priority"), "urgent")
        with self.assertRaisesRegex(RiverError, "min, low, default, high, urgent"):
            core.config_set(self.c, "ntfy_priority", "loud")

    def test_no_topic_and_bad_values(self):
        from river import notify
        with self.assertRaises(RiverError):
            notify.ADAPTERS["ntfy"](self.c)
        with self.assertRaises(RiverError):
            core.config_set(self.c, "ntfy_topic", "has space")
        with self.assertRaises(RiverError):
            core.config_set(self.c, "ntfy_url", "ntfy.sh")


class Email(Base):
    def setUp(self):
        super().setUp()
        from unittest import mock
        from pathlib import Path
        from river import notify
        self.notify, self.mock = notify, mock
        self.pw = Path(self.dir.name) / "smtp-password"
        self._p = mock.patch.object(notify, "PASSWORD_FILE", self.pw)
        self._p.start()
        self._e = mock.patch.dict(os.environ, {}, clear=False)
        self._e.start()
        os.environ.pop("MAXPM_SMTP_PASSWORD", None)
        for k, v in (("email_to", "me@example.com"), ("smtp_host", "smtp.example.com"),
                     ("smtp_user", "bot@example.com")):
            core.config_set(self.c, k, v)

    def tearDown(self):
        self._p.stop()
        self._e.stop()
        super().tearDown()

    def test_sends_with_starttls_and_login(self):
        self.pw.write_text("s3cret\n")
        self.pw.chmod(0o600)
        box = []

        class FakeSMTP:
            def __init__(self, host, port, timeout): box.append(("open", host, port))
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def starttls(self): box.append(("starttls",))
            def login(self, u, p): box.append(("login", u, p))
            def send_message(self, m): box.append(("send", m["To"], m["From"], m["Subject"], m.get_content()))

        with self.mock.patch("smtplib.SMTP", FakeSMTP):
            self.notify.ADAPTERS["email"](self.c)("River: needs you", "#3 sign", "http://127.0.0.1:8765/#item-3")
        self.assertEqual(box[0], ("open", "smtp.example.com", 587))
        self.assertEqual(box[1:3], [("starttls",), ("login", "bot@example.com", "s3cret")])
        self.assertEqual(box[3][1:4], ("me@example.com", "bot@example.com", "River: needs you"))
        self.assertIn("#item-3", box[3][4])

    def test_password_rules(self):
        with self.assertRaises(RiverError):
            self.notify.ADAPTERS["email"](self.c)  # user set, no password
        self.pw.write_text("x")
        self.pw.chmod(0o644)
        if os.name != "nt":  # Windows has no POSIX modes: river skips this check there
            with self.assertRaises(RiverError):
                self.notify.smtp_password()
        os.environ["MAXPM_SMTP_PASSWORD"] = "from-env"
        self.assertEqual(self.notify.smtp_password(), "from-env")
        self.assertNotIn("from-env", str(core.config_list(self.c)))
        with self.assertRaises(RiverError):
            core.config_set(self.c, "email_to", "not an address")

    def test_email_has_its_own_batch_window(self):
        from river import notify
        sent = []
        notify.register_channel("email", lambda conn: lambda t, b, u: sent.append(t))
        try:
            core.project_add(self.c, "a")
            core.config_set(self.c, "notify_channels", "email")
            self.add("a", "sign", doer="human")
            self.c.execute("UPDATE notifications SET created_at=?", (core.iso(core.now() - timedelta(minutes=2)),))
            self.assertFalse(notify.run(self.c)[0]["sent"])  # 2m old, email waits 10m
            self.c.execute("UPDATE notifications SET created_at=?", (core.iso(core.now() - timedelta(minutes=11)),))
            self.assertTrue(notify.run(self.c)[0]["sent"])
        finally:
            notify.register_channel("email", notify._email_channel)


class Mac(Base):
    def run_mac(self, notifier, returncode=0):
        from unittest import mock
        from river import notify
        seen = []

        class Done:
            def __init__(self): self.returncode, self.stderr = returncode, "boom"

        def fake_run(cmd, **kw):
            seen.append(cmd)
            return Done()

        with mock.patch("sys.platform", "darwin"), mock.patch("shutil.which", lambda name: notifier), \
                mock.patch("subprocess.run", fake_run):
            notify.ADAPTERS["mac"](self.c)('Say "yes"', "#3 sign", "http://127.0.0.1:8765/#item-3")
        return seen[0]

    def test_osascript_passes_texts_as_arguments(self):
        cmd = self.run_mac(None)
        self.assertEqual(cmd[0], "osascript")
        self.assertEqual(cmd[-3:], ['Say "yes"', "#3 sign", "http://127.0.0.1:8765/#item-3"])
        self.assertNotIn('Say "yes"', " ".join(cmd[:-3]))

    def test_terminal_notifier_opens_the_page(self):
        cmd = self.run_mac("/opt/homebrew/bin/terminal-notifier")
        self.assertEqual(cmd[0], "/opt/homebrew/bin/terminal-notifier")
        self.assertEqual(cmd[cmd.index("-open") + 1], "http://127.0.0.1:8765/#item-3")

    def test_failure_and_other_platforms(self):
        from unittest import mock
        from river import notify
        with self.assertRaises(OSError):
            self.run_mac(None, returncode=1)
        with mock.patch("sys.platform", "linux"), self.assertRaises(RiverError):
            notify.ADAPTERS["mac"](self.c)


class AddingWork(Base):
    def test_found_during_links_and_project_inference(self):
        core.project_add(self.c, "a", path=self.dir.name)
        core.project_add(self.c, "b")
        src = self.add("b", "work")
        self.assertEqual(core.project_for_add(self.c, "/", related=src), "b")
        self.assertEqual(core.project_for_add(self.c, self.dir.name), "a")
        with self.assertRaises(RiverError):
            core.project_for_add(self.c, "/")
        found = core.item_add(self.c, "b", "odd bug", found_during=src, actor="t")
        self.assertEqual(found["found_during"], src)
        self.assertEqual(found["waits_on"], [])
        self.assertEqual([x["id"] for x in core.item_show(self.c, src)["found_here"]], [found["id"]])
        with self.assertRaises(RiverError):
            core.item_add(self.c, "b", "x", found_during=999)


class Deployer(Base):
    def setUp(self):
        super().setUp()
        core.target_add(self.c, "web", "push, then smoke test")
        core.project_add(self.c, "site", target="web", path=self.dir.name)
        for n in ("dev", "ops"):
            core.register(self.c, n)
        w = self.add("site", "page")
        core.claim(self.c, w, "dev")
        core.done(self.c, w, "abc", "dev", ship_it=True)
        self.work = self.add("site", "other")
        self.deploy = core.item_show(self.c, w)["unblocks"][0]

    def test_owner_gets_deploy_item_first(self):
        core.target_own(self.c, "web", "ops")
        b = core.go(self.c, self.dir.name, "ops")
        self.assertEqual((b["role"], b["item"]["id"]), ("deployer", self.deploy))
        self.assertEqual(b["target"]["name"], "web")
        self.assertEqual([d["title"] for d in b["ships"]], ["page"])
        again = core.go(self.c, self.dir.name, "ops")
        self.assertEqual((again["role"], again.get("resumed")), ("deployer", True))

    def test_role_deployer_takes_a_free_target(self):
        b = core.go(self.c, self.dir.name, "ops", role="deployer")
        self.assertEqual((b["role"], b["item"]["id"]), ("deployer", self.deploy))
        self.assertEqual(core.target_show(self.c, "web")["owner"], "ops")
        # A non-owner gets normal work, never the deploy item.
        b2 = core.go(self.c, self.dir.name, "dev")
        self.assertEqual((b2["role"], b2["item"]["id"]), ("worker", self.work))

    def test_role_deployer_refused_when_owned(self):
        core.target_own(self.c, "web", "dev")
        b = core.go(self.c, self.dir.name, "ops", role="deployer")
        self.assertEqual(b["role"], "idle")
        self.assertIn("owned by dev", b["why"])


class Push(Base):
    def setUp(self):
        super().setUp()
        core.project_add(self.c, "a", path=self.dir.name)
        for n in ("boss", "aa", "bb"):
            core.register(self.c, n)
        self.first = self.add("a", "first", p=0)
        self.x = self.add("a", "pushed one", p=3)

    def test_push_then_go_claims_it_and_others_cannot(self):
        core.push(self.c, self.x, "aa", "you know this code", "boss")
        self.assertEqual(core.unread(self.c, "aa")["alerts"], 1)
        with self.assertRaises(RiverError):
            core.claim(self.c, self.x, "bb")
        self.assertEqual([i["id"] for i in core.next_item(self.c, actor="bb", limit=5)], [self.first])
        self.assertEqual(core.next_item(self.c, actor="aa")[0]["id"], self.x)  # pushed first, despite P3
        b = core.go(self.c, self.dir.name, "aa")
        self.assertEqual((b["item"]["id"], b["item"]["assignee"]), (self.x, "aa"))
        self.assertIn("pushed to you by boss", b["why"])
        alert = [m for m in core.inbox(self.c, "aa", include_read=True) if m["kind"] == "alert"][0]
        self.assertEqual(alert["state"], "accepted")

    def test_decline_reopens_and_tells_pusher(self):
        core.push(self.c, self.x, "aa", None, "boss")
        with self.assertRaises(RiverError):
            core.decline(self.c, self.x, "no", "bb")
        core.decline(self.c, self.x, "busy", "aa")
        it = core._item(self.c, self.x)
        self.assertEqual((it["reserved_for"], it["reserved_until"]), (None, None))
        self.assertIn("busy", core.inbox(self.c, "boss")[0]["body"])
        core.claim(self.c, self.x, "bb")

    def test_expiry_reopens_with_notice(self):
        core.push(self.c, self.x, "aa", None, "boss")
        self.c.execute("UPDATE items SET reserved_until=? WHERE id=?", (core.iso(core.now() - timedelta(minutes=1)), self.x))
        core.activity(self.c, "bb")
        self.assertIsNone(core._item(self.c, self.x)["reserved_for"])
        self.assertTrue(any(m["kind"] == "notice" for m in core.inbox(self.c, "boss")))
        with self.assertRaises(RiverError):
            core.accept(self.c, self.x, "aa")

    def test_cancel_push_tells_the_agent(self):
        core.push(self.c, self.x, "aa", None, "boss")
        core.cancel_push(self.c, self.x, "boss")
        self.assertIsNone(core._item(self.c, self.x)["reserved_for"])
        self.assertTrue(any("cancelled" in m["body"] for m in core.inbox(self.c, "aa")))
        with self.assertRaises(RiverError):
            core.cancel_push(self.c, self.x, "boss")

    def test_unregister_ends_every_reservation_of_the_agent(self):
        # #548: a reservation with no time limit (given, or a prerequisite of a held item) outlived its agent.
        core.push(self.c, self.x, "aa", None, "boss")
        self.c.execute("UPDATE items SET reserved_for='aa' WHERE id=?", (self.first,))
        core.unregister(self.c, "aa")
        for i in (self.x, self.first):
            it = core._item(self.c, i)
            self.assertEqual((it["reserved_for"], it["reserved_until"], it["reserved_by"]), (None, None, None))
        core.claim(self.c, self.first, "bb")

    def test_a_reservation_for_an_unregistered_name_ends_at_the_next_sweep(self):
        self.c.execute("UPDATE items SET reserved_for='ghost' WHERE id=?", (self.first,))
        core.activity(self.c, "bb")
        self.assertIsNone(core._item(self.c, self.first)["reserved_for"])

    def test_a_person_or_the_agent_ends_a_reservation_that_is_not_a_push(self):
        self.c.execute("UPDATE items SET reserved_for='aa' WHERE id=?", (self.first,))
        core.register(self.c, "mark", human=True)
        with self.assertRaisesRegex(RiverError, "reserved for aa"):
            core.cancel_push(self.c, self.first, "bb")
        core.cancel_push(self.c, self.first, "mark")
        self.assertIsNone(core._item(self.c, self.first)["reserved_for"])
        self.assertTrue(any("cancelled" in m["body"] for m in core.inbox(self.c, "aa")))
        self.c.execute("UPDATE items SET reserved_for='aa' WHERE id=?", (self.first,))
        core.cancel_push(self.c, self.first, "aa")
        self.assertIsNone(core._item(self.c, self.first)["reserved_for"])

    def test_a_released_or_expired_claim_of_a_pushed_item_keeps_no_reservation(self):
        # #641: a claim keeps reserved_for; given back, the item stayed reserved for the agent that had it.
        core.push(self.c, self.x, "aa", None, "boss")
        core.claim(self.c, self.x, "aa")
        core.release(self.c, self.x, "not mine", "aa")
        self.assertIsNone(core._item(self.c, self.x)["reserved_for"])
        core.push(self.c, self.x, "aa", None, "boss")
        core.claim(self.c, self.x, "aa")
        self.c.execute("UPDATE items SET lease_expires_at=? WHERE id=?", (core.iso(core.now() - timedelta(minutes=1)), self.x))
        core.activity(self.c, "bb")
        it = core._item(self.c, self.x)
        self.assertEqual((it["status"], it["reserved_for"], it["reserved_by"]), ("open", None, None))
        core.claim(self.c, self.x, "bb")

    def test_nothing_stays_reserved_for_an_agent_that_is_gone(self):
        self.c.execute("UPDATE items SET reserved_for='aa' WHERE id=?", (self.first,))
        core.activity(self.c, "bb")
        self.assertEqual(core._item(self.c, self.first)["reserved_for"], "aa")  # aa is active
        self.c.execute("UPDATE agents SET last_seen=? WHERE name='aa'", (core.iso(core.now() - timedelta(days=2)),))
        core.activity(self.c, "bb")
        self.assertIsNone(core._item(self.c, self.first)["reserved_for"])
        self.assertIn("reservation for aa ended: aa is gone", core.item_show(self.c, self.first)["events"][0]["change"])

    def test_the_one_an_item_is_reserved_for_pushes_it_on_and_refusals_name_the_command(self):
        self.c.execute("UPDATE items SET reserved_for='boss' WHERE id=?", (self.x,))
        with self.assertRaisesRegex(RiverError, f"reserved for boss; .*maxpm edit {self.x} --unreserve"):
            core.push(self.c, self.x, "bb", None, "aa")
        with self.assertRaisesRegex(RiverError, f"maxpm edit {self.x} --unreserve"):
            core.claim(self.c, self.x, "bb")
        with self.assertRaisesRegex(RiverError, f"reserved for boss; .*maxpm edit {self.x} --unreserve"):
            core.release(self.c, self.x, None, "boss")
        self.c.execute("UPDATE agents SET role='manager' WHERE name='boss'")
        with self.assertRaisesRegex(RiverError, f"reserved for you: .*maxpm launch --item {self.x}.*maxpm edit {self.x} --unreserve"):
            core.claim(self.c, self.x, "boss")
        core.push(self.c, self.x, "bb", None, "boss")
        it = core._item(self.c, self.x)
        self.assertEqual((it["reserved_for"], it["reserved_by"], bool(it["reserved_until"])), ("bb", "boss", True))
        core.claim(self.c, self.x, "bb")

    def test_push_refusals(self):
        core.claim(self.c, self.first, "bb")
        with self.assertRaises(RiverError):
            core.push(self.c, self.first, "aa", None, "boss")  # not open
        core.push(self.c, self.x, "aa", None, "boss")
        with self.assertRaises(RiverError):
            core.push(self.c, self.x, "bb", None, "boss")  # already pushed to someone else
        with self.assertRaises(RiverError):
            core.push(self.c, self.x, "nobody", None, "boss")


class HumanSteps(Base):
    def test_go_brief_names_the_people(self):
        core.project_add(self.c, "a", path=self.dir.name)
        core.register(self.c, "mark", human=True)
        self.add("a", "x")
        b = core.go(self.c, self.dir.name, "ag")
        self.assertEqual(b["humans"], ["mark"])


class NeedsYouPage(Base):
    """The page's needs-you list and its two message actions (river/server.py)."""

    def setUp(self):
        super().setUp()
        from river import server
        self.server = server
        core.register(self.c, "mark", human=True)
        core.register(self.c, "bot")
        core.project_add(self.c, "a")

    def test_item_events_carry_context_and_close_when_done(self):
        iid = core.item_add(self.c, "a", "approve policy", 2, "notes here", "human", (), "bot",
                            "decide the five points at the end of the draft")["id"]
        (ev,) = self.server.needs_you_view(self.c, "mark")
        self.assertEqual((ev["item_id"], ev["item_context"], ev["item_notes"]),
                         (iid, "decide the five points at the end of the draft", "notes here"))
        core.done(self.c, iid, "approved", "mark")
        self.assertEqual(self.server.needs_you_view(self.c, "mark"), [])

    def test_answer_and_read_close_message_events(self):
        q = core.send(self.c, "question", "which server?", to="mark", actor="bot")
        a = core.send(self.c, "alert", "look at #3", to="mark", actor="bot")
        kinds = sorted(e["message_kind"] for e in self.server.needs_you_view(self.c, "mark"))
        self.assertEqual(kinds, ["alert", "question"])
        self.server.OPS["answer"](self.c, {"msg": q["id"], "body": "OVH"}, "mark")
        self.server.OPS["message_read"](self.c, {"msg": a["id"]}, "mark")
        self.assertEqual(self.server.needs_you_view(self.c, "mark"), [])
        with self.assertRaises(RiverError):
            self.server.OPS["message_read"](self.c, {"msg": 9999}, "mark")


class Takeover(Base):
    def setUp(self):
        super().setUp()
        core.project_add(self.c, "a")
        core.register(self.c, "mark", human=True)
        core.register(self.c, "ag")
        self.h = self.add("a", "pick a color", doer="human")

    def test_takeover_clears_the_users_list_with_one_notice_and_undo(self):
        q = core.send(self.c, "question", "which color?", to="mark", item=self.h, actor="ag")
        with self.assertRaises(RiverError):
            core.claim(self.c, self.h, "ag")
        with self.assertRaises(RiverError):
            core.takeover(self.c, self.h, " ", "ag")
        it = core.takeover(self.c, self.h, "brand guide says blue", "ag")
        self.assertEqual((it["doer"], it["status"], it["assignee"]), ("ai", "in_progress", "ag"))
        self.assertEqual(core.needs_you(self.c), [])
        self.assertEqual(core.message_show(self.c, q["id"])["state"], "read")
        notes = [m for m in core.inbox(self.c, "mark") if m["kind"] == "notice"]
        self.assertEqual(len(notes), 1)
        self.assertIn("undo-takeover", notes[0]["body"])
        self.assertEqual([t["id"] for t in core.state(self.c)["takeovers"]], [self.h])
        core.undo_takeover(self.c, self.h, "mark")
        it = core._item(self.c, self.h)
        self.assertEqual((it["doer"], it["status"], it["assignee"], it["takeover_by"]), ("human", "open", None, None))
        self.assertEqual(len(core.needs_you(self.c)), 1)

    def test_agent_done_or_drop_needs_a_note(self):
        with self.assertRaises(RiverError):
            core.done(self.c, self.h, None, "ag")
        core.drop(self.c, self.h, "ag", note="not needed on the free tier")
        self.assertEqual(core.takeovers(self.c)[0]["takeover_kind"], "dropped")
        core.takeover_seen(self.c, self.h)
        self.assertEqual(core.takeovers(self.c), [])
        core.undo_takeover(self.c, self.h, "mark")
        self.assertEqual(core._item(self.c, self.h)["status"], "open")
        core.done(self.c, self.h, "did it", "mark")  # a person needs no note


class Prompts(Base):
    def test_prompt_for_item_and_all(self):
        core.project_add(self.c, "a", path=self.dir.name)
        core.register(self.c, "mark", human=True)
        h = core.item_add(self.c, "a", "Approve policy", doer="human", context="docs/policy.md, 5 decisions")["id"]
        self.add("a", "Launch", p=0, after=[h])
        q = core.send(self.c, "question", "Which plan?", to="mark", actor="ag")
        p = core.prompt_for(self.c, h)
        from pathlib import Path
        folder = Path(self.dir.name).resolve()
        for part in (f"cd {folder} && maxpm --as mark show {h}", "docs/policy.md", "It blocks: #",
                     f"maxpm --as mark done {h} --output", "takeover"):
            self.assertIn(part, p)
        allp = core.prompt_for_all(self.c, "mark")
        self.assertIn("1. Item #", allp)
        self.assertIn(f"maxpm --as mark answer {q['id']}", allp)
        core.done(self.c, h, "approved", "mark")
        self.assertIn("Nothing", core.prompt_for_all(self.c, "nobody"))


class OfferGiveSplit(Base):
    def setUp(self):
        super().setUp()
        core.project_add(self.c, "a")
        for n in ("ag", "bo"):
            core.register(self.c, n)
        self.goal = self.add("a", "Stripe activation")
        self.page = core.item_add(self.c, "a", "Privacy page", blocks=self.goal, actor="t")["id"]
        core.claim(self.c, self.page, "bo")

    def test_offer_split_then_helper_takes_a_piece(self):
        o = core.offer(self.c, "I can take the draft", self.page, actor="ag")
        self.assertEqual((o["kind"], o["to_agent"]), ("offer", "bo"))
        res = core.split(self.c, self.page, ["draft text", "address"], "bo")
        self.assertEqual(core._item(self.c, self.page)["status"], "held")
        self.assertEqual(core.message_show(self.c, o["id"])["state"], "accepted")
        got = core.next_item(self.c, unblocks=self.goal, claim=True, actor="ag")
        self.assertIn(got[0]["id"], res["split_into"])
        core.done(self.c, got[0]["id"], "drafted", "ag")
        other = [n for n in res["split_into"] if n != got[0]["id"]][0]
        core.done(self.c, other, "found", "t")
        st = core._item(self.c, self.page)
        self.assertEqual((st["status"], st["assignee"]), ("in_progress", "bo"))

    def test_give_moves_the_lease_and_accepts_the_offer(self):
        o = core.offer(self.c, "let me", self.page, actor="ag")
        with self.assertRaises(RiverError):
            core.give(self.c, self.page, "ag", "ag")
        core.give(self.c, self.page, "ag", "bo")
        it = core._item(self.c, self.page)
        self.assertEqual((it["assignee"], it["status"]), ("ag", "in_progress"))
        self.assertEqual(core.message_show(self.c, o["id"])["state"], "accepted")
        with self.assertRaises(RiverError):
            core.give(self.c, self.page, "ag", "bo")  # not bo's any more

    def test_decline_offer_and_offer_refusals(self):
        o = core.offer(self.c, "let me", self.page, actor="ag")
        with self.assertRaises(RiverError):
            core.decline_message(self.c, o["id"], "no", "ag")  # not for ag
        core.decline_message(self.c, o["id"], "almost done", "bo")
        self.assertEqual(core.message_show(self.c, o["id"])["state"], "declined")
        self.assertTrue(any("almost done" in m["body"] for m in core.inbox(self.c, "ag")))
        with self.assertRaises(RiverError):
            core.offer(self.c, "x", self.goal, actor="ag")  # nobody holds it


class SessionLinks(Base):
    """A notification opens the session of the agent that asked or added the item, when it has a web link."""

    def test_env_record_and_origin(self):
        from river import notify
        self.assertEqual(core.session_url_from_env({"CLAUDE_CODE_BRIDGE_SESSION_ID": "session_01Abc"}),
                         "https://claude.ai/code/session_01Abc")
        self.assertIsNone(core.session_url_from_env({"CLAUDE_CODE_BRIDGE_SESSION_ID": "x/../y"}))
        self.assertIsNone(core.session_url_from_env({}))
        core.project_add(self.c, "a")
        core.register(self.c, "ag")
        core.register(self.c, "old")
        core.register(self.c, "mark", human=True)
        core.record_session_url(self.c, "ag", "https://claude.ai/code/session_01Abc")
        core.record_session_url(self.c, "mark", "https://claude.ai/code/session_x")  # people have no agent session
        step = core.item_add(self.c, "a", "approve it", doer="human", actor="ag")["id"]
        other = core.item_add(self.c, "a", "sign it", doer="human", actor="old")["id"]
        q = core.send(self.c, "question", "which port?", "mark", None, None, "ag")
        self.assertEqual(core.origin_session_url(self.c, item_id=step), "https://claude.ai/code/session_01Abc")
        self.assertIsNone(core.origin_session_url(self.c, item_id=other))
        self.assertEqual(core.origin_session_url(self.c, other, q["id"]), "https://claude.ai/code/session_01Abc")
        row = lambda i, m=None: {"summary": "s", "item_id": i, "message_id": m}
        self.assertEqual(notify.compose(self.c, [row(step)])[2], "https://claude.ai/code/session_01Abc")
        self.assertTrue(notify.compose(self.c, [row(other)])[2].endswith(f"#item-{other}"))  # falls back to the page
        self.assertTrue(notify.compose(self.c, [row(step), row(other)])[2].startswith("http://127.0.0.1:"))
        # A batch from one session opens that session.
        self.assertEqual(notify.compose(self.c, [row(step), row(other, q["id"])])[2], "https://claude.ai/code/session_01Abc")


if __name__ == "__main__":
    unittest.main()


class CommandNames(unittest.TestCase):
    """The command is maxpm: one entry point, and no text names another."""

    def test_the_product_names(self):
        self.assertTrue(core.names_product("uses MaximizePM"))
        self.assertFalse(core.names_product("uses another queue"))
        self.assertFalse(hasattr(core, "OLD_PRODUCTS"))

    def test_no_agent_takes_the_name_of_the_notices(self):
        with tempfile.TemporaryDirectory() as d:
            c = core.connect(os.path.join(d, "q.db"))
            with self.assertRaises(core.RiverError):
                core.register(c, "maxpm")
            self.assertEqual(core.register(c, "maxpm-1")["name"], "maxpm-1")
            c.close()

    def test_the_command_is_maxpm(self):
        import subprocess
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, "MAXPM_DB": str(Path(d, "q.db"))}
            h = subprocess.run([sys.executable, str(root / "bin" / "maxpm"), "--help"], capture_output=True, text=True, env=env)
            self.assertEqual(h.returncode, 0, h.stderr)
            self.assertTrue(h.stdout.startswith("usage: maxpm "), h.stdout[:40])
            self.assertNotRegex(h.stdout, r"\briver\b")
            v = subprocess.run([sys.executable, str(root / "bin" / "maxpm"), "--version"], capture_output=True, text=True, env=env)
            self.assertRegex(v.stdout.strip(), r"^maxpm \d+\.\d+")
            subprocess.run([sys.executable, str(root / "bin" / "maxpm"), "project", "add", "shop"], check=True,
                           capture_output=True, env=env)
            ls = subprocess.run([sys.executable, str(root / "bin" / "maxpm"), "project", "list"], capture_output=True,
                                text=True, env=env)
            self.assertIn("shop", ls.stdout)
        text = (root / "pyproject.toml").read_text()
        self.assertIn('maxpm = "river.cli:main"', text)
        self.assertNotIn('river = "river.cli:main"', text)
        self.assertIn('name = "maximizepm"', text)


class AgentBlock(unittest.TestCase):
    def test_block_is_added_and_not_repeated(self):
        from pathlib import Path
        from river import cli
        self.assertIn("uses MaximizePM (`maxpm`)", cli.AGENT_SNIPPET)
        self.assertIn("`maxpm go`", cli.AGENT_SNIPPET)
        self.assertNotIn("river", cli.AGENT_SNIPPET)
        with tempfile.TemporaryDirectory() as d:
            new, notes = Path(d, "AGENTS.md"), Path(d, "CLAUDE.md")
            self.assertIn("added", cli._append_block(new))  # created
            self.assertIn("already", cli._append_block(new))
            self.assertEqual(new.read_text(), cli.AGENT_SNIPPET)
            notes.write_text("# Notes\n")
            self.assertIn("added", cli._append_block(notes))
            self.assertEqual(notes.read_text(), "# Notes\n\n" + cli.AGENT_SNIPPET)

    def test_no_old_blocks_and_no_refresh(self):
        # No old installs (mark, 2026-10-03): the earlier blocks, init --refresh and the briefing's note are gone.
        from river import cli
        for name in ("OLD_SNIPPETS", "old_blocks", "refresh_blocks"):
            self.assertFalse(hasattr(cli, name), name)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.run(["init", "--refresh"])

    def test_the_session_step_is_only_for_claude_code(self):
        from pathlib import Path
        from river import cli
        with tempfile.TemporaryDirectory() as d:
            c = core.connect(Path(d, "r.db"))
            core.project_add(c, "p", path=d)
            old = os.environ.pop("CLAUDECODE", None)
            try:
                for env, shown in ((None, False), ("1", True)):
                    if env:
                        os.environ["CLAUDECODE"] = env
                    b = core.go(c, d)
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        cli.render_go(b)
                    self.assertEqual("Claude Code session name" in out.getvalue(), shown)
            finally:
                os.environ.pop("CLAUDECODE", None)
                if old is not None:
                    os.environ["CLAUDECODE"] = old
            c.close()

    def test_instructions_one_file_for_every_agent(self):
        from pathlib import Path
        from river import cli
        with tempfile.TemporaryDirectory() as d:
            c, a = Path(d, "CLAUDE.md"), Path(d, "AGENTS.md")
            cli.setup_instructions(d)  # nothing yet: AGENTS.md holds the block, CLAUDE.md imports it
            self.assertEqual((c.read_text(), a.read_text()), ("@AGENTS.md\n", cli.AGENT_SNIPPET))
            self.assertEqual(cli.instructions_layout(d), "shared")
            cli.setup_instructions(d)  # again: nothing changes
            self.assertEqual((c.read_text(), a.read_text()), ("@AGENTS.md\n", cli.AGENT_SNIPPET))
        with tempfile.TemporaryDirectory() as d:
            c, a = Path(d, "CLAUDE.md"), Path(d, "AGENTS.md")
            c.write_text("# Rules\n\nPush to main deploys.\n")
            a.write_text(cli.AGENT_SNIPPET)  # what an older maxpm init made
            self.assertEqual(cli.instructions_layout(d), "claude_only")
            lines = cli.setup_instructions(d)  # no choice given: both get the block, and the choice is named
            self.assertIn("maxpm init --move", lines[-1])
            self.assertIn("Push to main", c.read_text())
            cli.setup_instructions(d, move=True)
            self.assertEqual(c.read_text(), "@AGENTS.md\n")
            text = a.read_text()
            self.assertIn("Push to main deploys", text)
            self.assertEqual(text.count("## Work queue"), 1)
            self.assertEqual(cli.instructions_layout(d), "shared")
        with tempfile.TemporaryDirectory() as d:
            c, a = Path(d, "CLAUDE.md"), Path(d, "AGENTS.md")
            c.write_text("claude rules\n"); a.write_text("codex rules\n")
            self.assertEqual(cli.instructions_layout(d), "both")
            lines = cli.setup_instructions(d, move=True)  # two sets of rules: river never merges them
            self.assertIn("different rules", lines[-1])
            self.assertIn(cli.AGENT_SNIPPET, c.read_text()); self.assertIn(cli.AGENT_SNIPPET, a.read_text())
            self.assertIn("claude rules", c.read_text())
        with tempfile.TemporaryDirectory() as d:
            c, a = Path(d, "CLAUDE.md"), Path(d, "AGENTS.md")
            c.write_text("claude rules\n")
            cli.setup_instructions(d, move=False)  # keep apart
            self.assertEqual((c.read_text().count("## Work queue"), a.read_text()), (1, cli.AGENT_SNIPPET))


class DeployNow(Base):
    """The Targets tab's Deploy now and Review and deploy (review setting off)."""

    def setUp(self):
        super().setUp()
        core.target_add(self.c, "web", "push, then smoke test")
        core.project_add(self.c, "site", target="web", path=self.dir.name)
        core.register(self.c, "dev")

    def test_refuses_with_nothing_collected_then_waits_then_alerts_the_owner(self):
        with self.assertRaisesRegex(RiverError, "nothing is collected"):
            core.deploy_now(self.c, "web")
        a = self.add("site", "page")
        core.ship(self.c, a, "dev")
        r = core.deploy_now(self.c, "web")
        self.assertEqual((r["ready"], [w["id"] for w in r["waits_on"]], r["start"]["kind"]), (False, [a], "deploy"))
        core.claim(self.c, a, "dev")
        core.done(self.c, a, "commit", "dev")
        core.target_own(self.c, "web", "dev")
        r = core.deploy_now(self.c, "web")
        self.assertEqual((r["ready"], r["alerted"]), (True, "dev"))
        self.assertTrue(any(m["kind"] == "alert" and "deploy now" in m["body"] for m in core.inbox(self.c, "dev")))
        d = r["deploy"]["id"]
        core.claim(self.c, d, "dev")
        core.done(self.c, d, "release v1", "dev")
        h = core.targets_view(self.c)[0]["history"]
        self.assertEqual([(x["id"], x["output"], x["done_by"], [s["id"] for s in x["ships"]]) for x in h],
                         [(d, "release v1", "dev", [a])])

    def test_review_and_deploy_adds_a_review_even_with_review_off(self):
        a = self.add("site", "page")
        core.claim(self.c, a, "dev")
        core.done(self.c, a, "commit", "dev", ship_it=True)
        r = core.deploy_now(self.c, "web", review=True)
        self.assertEqual((r["start"]["kind"], r["ready"]), ("review", True))
        self.assertIn(a, core.item_show(self.c, r["review"]["id"])["waits_on"])  # it covers the finished work
        v = core.targets_view(self.c)[0]["pending"][0]
        self.assertEqual(([x["id"] for x in v["ships"]], v["review"]["id"]), ([a], r["review"]["id"]))
        self.assertIn(r["review"]["id"], core.item_show(self.c, r["deploy"]["id"])["waits_on"])
        b = core.go(self.c, self.dir.name, None, focus="review:web")  # the session the page opens
        self.assertEqual((b["role"], b["item"]["id"]), ("reviewer", r["review"]["id"]))


class ReleaseCadence(Base):
    """A target with a release cadence collects ship requests, and the first step of the release (its review, or
    the deploy item with no review) waits until the end of the last release plus the cadence (#1571)."""

    def setUp(self):
        super().setUp()
        core.target_add(self.c, "web", "push, then smoke test")
        core.project_add(self.c, "site", target="web", path=self.dir.name)
        for n in ("dev", "rev", "ops"):
            core.register(self.c, n)
        core.register(self.c, "mark", human=True)
        core.target_own(self.c, "web", "ops")
        self.clock = [core.now()]
        fake_now = mock.patch.object(core, "now", lambda: self.clock[0])
        fake_now.start()
        self.addCleanup(fake_now.stop)

    def later(self, **kw):
        self.clock[0] += timedelta(**kw)
        core.activity(self.c, "dev")  # every command runs the sweep

    def shipped(self, title="page"):
        """An item that dev finished and asked to ship; returns (item, the ship result)."""
        a = self.add("site", title)
        core.claim(self.c, a, "dev")
        core.done(self.c, a, "commit", "dev")
        return a, core.ship(self.c, a, "dev")

    def release(self, review=True):
        """One whole release of one item: returns its deploy item."""
        a, dep = self.shipped("first")
        if review:
            rv = dep["release"]["start"]
            core.claim(self.c, rv, "rev")
            core.review_pass(self.c, rv, "fine", "rev", self.dir.name)
        core.claim(self.c, dep["id"], "ops")
        core.done(self.c, dep["id"], "release v1", "ops")
        return dep["id"]

    def test_cadence_values(self):
        day = timedelta(days=1)
        self.assertEqual([core.parse_cadence(x) for x in ("2h", " 1D ", "1w", "1mo", "off", "", "0")],
                         [("2h", timedelta(hours=2)), ("1d", day), ("1w", 7 * day), ("1mo", 30 * day),
                          ("", None), ("", None), ("", None)])
        for bad in ("soon", "2", "0h", "1y"):
            with self.assertRaisesRegex(RiverError, "bad cadence"):
                core.target_cadence(self.c, "web", bad)
        self.assertEqual(core.target_cadence(self.c, "web", "1w", "mark")["cadence"], "1w")
        self.assertEqual(core.target_cadence(self.c, "web", "off", "mark")["cadence"], "")

    def test_the_review_of_the_next_release_waits_until_the_last_release_plus_the_cadence(self):
        core.config_set(self.c, "review", "on")
        core.target_cadence(self.c, "web", "2h", "mark")
        self.assertIn("can start now", core.target_show(self.c, "web")["release"]["text"])
        first = self.release()  # no release before it: it starts at once
        self.later(minutes=30)
        a, dep = self.shipped("form")
        p = dep["release"]
        rv = p["start"]
        self.assertEqual((p["waits"], p["deploy"], p["last"]["id"], core._item(self.c, rv)["kind"]),
                         (True, dep["id"], first, "review"))
        self.assertIn("(in 1h30m)", p["text"])
        it = core.annotate(self.c)[rv]
        self.assertEqual((it["ready"], it["blocked_until"]), (False, core.iso(self.clock[0] + timedelta(minutes=90))))
        self.assertIn("release cadence 2h of target web", it["blocked_text"])
        with self.assertRaisesRegex(RiverError, "release cadence 2h"):
            core.claim(self.c, rv, "rev")
        with self.assertRaisesRegex(RiverError, "release-now web"):
            core.unblock(self.c, rv, "rev")
        self.assertIsNone(core.go(self.c, self.dir.name, "rev", role="reviewer").get("item"))
        # More ship requests collect on the same release, and do not move its time.
        b, dep2 = self.shipped("footer")
        self.assertEqual((dep2["id"], dep2["release"]["start"], [x["id"] for x in dep2["release"]["ships"]]),
                         (dep["id"], rv, [a, b]))
        self.assertEqual(core.done(self.c, self.add_done(), "c", "dev", ship_it=True)["ship_release"]["waits"], True)
        t = core.target_show(self.c, "web")["release"]
        self.assertEqual((t["waits"], t["cadence"], len(t["ships"])), (True, "2h", 3))
        self.assertTrue(core.targets_view(self.c)[0]["release"]["waits"])
        self.later(minutes=89)
        self.assertFalse(core.annotate(self.c)[rv]["ready"])
        self.later(minutes=1)
        it = core.annotate(self.c)[rv]
        self.assertEqual((it["ready"], it["blocked_reason"]), (True, None))
        self.assertTrue(any(e["change"].startswith("release cadence: the wait ended")
                            for e in core.item_show(self.c, rv)["events"]))
        self.assertEqual(core.go(self.c, self.dir.name, "rev", role="reviewer")["item"]["id"], rv)
        now_runs = core.target_show(self.c, "web")["release"]  # the reviewer took the review: that is the cut
        self.assertEqual((now_runs["current"]["id"], now_runs["deploy"], now_runs["waits"]), (dep["id"], None, False))
        self.assertIn(f"release #{dep['id']} of web runs now", now_runs["text"])

    def add_done(self):
        a = self.add("site", "more")
        core.claim(self.c, a, "dev")
        return a

    def test_release_now_needs_a_reason_and_the_history_keeps_it(self):
        core.config_set(self.c, "review", "on")
        with self.assertRaisesRegex(RiverError, "no release cadence"):
            core.release_now(self.c, "web", "now", "mark")
        core.target_cadence(self.c, "web", "1d", "mark")
        with self.assertRaisesRegex(RiverError, "nothing is collected"):
            core.release_now(self.c, "web", "now", "mark")
        self.release()
        a, dep = self.shipped("fix")
        rv = dep["release"]["start"]
        with self.assertRaisesRegex(RiverError, "say why this release cannot wait"):
            core.release_now(self.c, "web", " ", "mark")
        # A worker cannot: the target owner, the manager, or a person decides it.
        with self.assertRaisesRegex(RiverError, rf"decision of the target owner \(ops\).*maxpm alert ops .* --item {dep['id']}"):
            core.release_now(self.c, "web", "my change is urgent", "dev")
        self.assertFalse(core.annotate(self.c)[rv]["ready"])
        r = core.release_now(self.c, "web", "login fails in production", "mark")
        self.assertEqual((r["waits"], r["early"], r["item"]["id"], r["item"]["ready"]),
                         (False, "login fails in production", rv, True))
        self.assertTrue(any(e["actor"] == "mark" and e["change"].startswith("release now (the cadence 1d permits")
                            and e["change"].endswith("): login fails in production")
                            for e in core.item_show(self.c, dep["id"])["events"]))
        self.assertTrue(any("release now" in m["body"] and "login fails" in m["body"] for m in core.inbox(self.c, "ops")))
        self.later(minutes=5)  # the sweep does not hold it again
        self.assertTrue(core.annotate(self.c)[rv]["ready"])
        with self.assertRaisesRegex(RiverError, "nothing waits for the cadence"):
            core.release_now(self.c, "web", "again", "mark")
        # The release after that one waits again, from the end of the early release.
        core.claim(self.c, rv, "rev")
        core.review_pass(self.c, rv, "fine", "rev", self.dir.name)
        core.claim(self.c, dep["id"], "ops")
        core.done(self.c, dep["id"], "release v2", "ops")
        nxt = self.shipped("later")[1]["release"]
        self.assertEqual((nxt["waits"], nxt["next_at"]), (True, core.iso(self.clock[0] + timedelta(days=1))))
        # The target owner decides by itself, and so does the manager; no person approves.
        self.assertEqual(core.release_now(self.c, "web", "a fix of a production defect", "ops")["waits"], False)
        self.assertFalse(any("release now" in m["body"] and "production defect" in m["body"]
                             for m in core.inbox(self.c, "ops")))  # no notice to itself

    def test_the_manager_releases_sooner_by_its_own_decision(self):
        core.target_cadence(self.c, "web", "6h", "mark")
        self.release(review=False)
        dep = self.shipped("fix")[1]["id"]
        core.register(self.c, "boss")
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET role='manager' WHERE name='boss'")
        with self.assertRaisesRegex(RiverError, r"the manager \(boss\), or a person"):
            core.release_now(self.c, "web", "urgent", "dev")
        r = core.release_now(self.c, "web", "checkout is down", "boss")
        self.assertEqual((r["item"]["id"], r["item"]["ready"]), (dep, True))
        self.assertTrue(any("checkout is down" in m["body"] for m in core.inbox(self.c, "ops")))  # the owner is told

    def test_with_no_review_the_deploy_item_waits_and_deploy_now_is_refused(self):
        core.target_cadence(self.c, "web", "1h", "mark")
        self.release(review=False)
        a, dep = self.shipped("form")
        self.assertEqual((dep["release"]["start"], dep["release"]["waits"]), (dep["id"], True))
        self.assertFalse(core.annotate(self.c)[dep["id"]]["ready"])
        self.assertIsNone(core.go(self.c, self.dir.name, "ops", role="deployer").get("item"))
        for review in (False, True):
            with self.assertRaisesRegex(RiverError, "release cadence 1h.*release-now web"):
                core.deploy_now(self.c, "web", review=review)
        self.assertEqual(core.cadence_holds(self.c, dep["id"]), dep["id"])
        self.later(hours=1)
        self.assertIsNone(core.cadence_holds(self.c, dep["id"]))
        self.assertEqual(core.go(self.c, self.dir.name, "ops", role="deployer")["item"]["id"], dep["id"])

    def test_a_release_that_started_is_not_held_and_a_change_of_the_cadence_moves_the_wait(self):
        core.config_set(self.c, "review", "on")
        self.release()
        a, dep = self.shipped("form")
        rv = dep["release"]["start"]
        self.assertTrue(core.annotate(self.c)[rv]["ready"])  # no cadence: at once
        core.target_cadence(self.c, "web", "2h", "mark")  # it applies to the release that collects now
        self.assertEqual(core._item(self.c, rv)["blocked_until"], core.iso(self.clock[0] + timedelta(hours=2)))
        core.target_cadence(self.c, "web", "4h", "mark")
        self.assertEqual(core._item(self.c, rv)["blocked_until"], core.iso(self.clock[0] + timedelta(hours=4)))
        core.target_cadence(self.c, "web", "off", "mark")
        self.assertTrue(core.annotate(self.c)[rv]["ready"])
        # A review that a reviewer took once: the release has started, and a cadence set later does not hold it,
        # also when the review comes back after its fixes.
        core.claim(self.c, rv, "rev")
        core.target_cadence(self.c, "web", "2h", "mark")
        fix = core.review_fail(self.c, rv, ["null check"], actor="rev")["fixes"][0]["id"]
        core.claim(self.c, fix, "dev")
        core.done(self.c, fix, "fixed", "dev")
        self.later(minutes=1)
        it = core.annotate(self.c)[rv]
        self.assertEqual((it["ready"], it["blocked_reason"]), (True, None))
        self.assertEqual(core.target_show(self.c, "web")["release"]["current"]["id"], dep["id"])

    def test_a_ship_request_while_a_deploy_runs_waits_for_it_and_then_for_the_cadence(self):
        core.target_cadence(self.c, "web", "2h", "mark")
        a, dep = self.shipped("first")
        core.claim(self.c, dep["id"], "ops")
        b, nxt = self.shipped("second")
        p = nxt["release"]
        cut = self.clock[0]  # the deployer took the deploy item: the cut, and the cadence counts from it
        self.assertEqual((p["waits"], p["running"], p["next_at"], nxt["id"] != dep["id"]),
                         (True, dep["id"], core.iso(cut + timedelta(hours=2)), True))
        self.assertIn(f"release #{dep['id']} of web runs now", p["text"])
        self.assertIn("release order of target web", core._item(self.c, nxt["id"])["blocked_reason"])
        self.later(minutes=20)
        self.assertFalse(core.annotate(self.c)[nxt["id"]]["ready"])
        core.done(self.c, dep["id"], "release v1", "ops")
        self.later(minutes=10)
        it = core._item(self.c, nxt["id"])
        self.assertEqual(it["blocked_until"], core.iso(cut + timedelta(hours=2)))
        self.assertIn("release cadence 2h of target web: the last release was cut", it["blocked_reason"])
        self.later(minutes=89)
        self.assertFalse(core.annotate(self.c)[nxt["id"]]["ready"])
        self.later(minutes=1)
        self.assertTrue(core.annotate(self.c)[nxt["id"]]["ready"])

    def test_a_blocker_that_a_person_set_on_the_release_stays(self):
        core.target_cadence(self.c, "web", "1h", "mark")
        self.release(review=False)
        dep = self.shipped("form")[1]["id"]
        core.block(self.c, dep, "the data centre moves this week", "mark")
        self.later(hours=2)
        it = core._item(self.c, dep)
        self.assertEqual((it["blocked_reason"], it["blocked_set_by"]), ("the data centre moves this week", "mark"))
        core.unblock(self.c, dep, "mark")
        self.assertTrue(core.annotate(self.c)[dep]["ready"])

    def test_the_commands_say_when_the_change_goes_out(self):
        from river import cli

        def run(*words):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                    mock.patch.dict(os.environ, {"MAXPM_DB": self.path, "MAXPM_QUIET": "1"}):
                cli.run(list(words))
            return out.getvalue()
        self.assertIn("release cadence: off", run("target", "show", "web"))
        self.assertIn("release cadence 2h: the next release of web can start now", run("target", "cadence", "web", "2h"))
        self.release(review=False)
        a = self.add_done()
        out = run("--as", "dev", "done", str(a), "--output", "c", "--ship")
        self.assertRegex(out, r"release cadence 2h: the next release of web can start .* \(in 2h00m\); your change goes out "
                              r"with the next release \(deploy #\d+\)\. The target owner or the manager decides a "
                              r"release sooner \(maxpm target release-now web --reason")
        self.assertIn("your change goes out with the next release", run("--as", "dev", "ship", str(a)))
        out = run("target", "show", "web")
        self.assertIn("waits for it: deploy #", out)
        self.assertIn(f"#{a} more (done)", out)
        out = run("--as", "mark", "target", "release-now", "web", "--reason", "mark asked for it")
        self.assertIn("goes out sooner (mark asked for it)", out)
        self.assertIn("which is ready", out)


class DoneWaitsOnPrerequisites(Base):
    """Done is refused while an item it waits on is open; a new prerequisite alerts whoever must stop."""

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "a")
        core.target_add(self.c, "web", "push")
        core.project_add(self.c, "site", target="web")
        for n in ("dev", "ops", "mark"):
            core.register(self.c, n)

    def alerts(self, who):
        return [m for m in core.inbox(self.c, who) if m["kind"] == "alert"]

    def test_done_is_refused_until_prerequisites_close_or_forced_with_a_reason(self):
        x, y = self.add("a", "x"), self.add("a", "y")
        core.dep_add(self.c, x, [y], "t")
        with self.assertRaises(RiverError) as e:
            core.done(self.c, x, "out", "t")
        self.assertIn(f"#{y}", str(e.exception))
        with self.assertRaises(RiverError):
            core.done(self.c, x, "out", "t", force="  ")
        core.done(self.c, x, "out", "t", force="the part y covers is not needed")
        it = core.item_show(self.c, x)
        self.assertEqual(it["status"], "done")
        self.assertTrue(any(f"#{y} still open: the part y covers" in e["change"] for e in it["events"]))
        z = self.add("a", "z", after=[y])
        core.done(self.c, y, "y done", "t")
        self.assertEqual(core.done(self.c, z, "fine", "t")["status"], "done")  # nothing open: no reason needed

    def test_a_deploy_is_refused_even_with_force(self):
        a = self.add("site", "page")
        d = core.ship(self.c, a, "dev")
        with self.assertRaises(RiverError) as e:
            core.done(self.c, d["id"], "release", "ops", force="ship it anyway")
        self.assertIn("deploy", str(e.exception))

    def test_a_prerequisite_added_to_a_held_item_alerts_the_holder(self):
        x, y, z = self.add("a", "x"), self.add("a", "y"), self.add("a", "z")
        core.claim(self.c, x, "dev")
        core.dep_add(self.c, x, [y], "ops")
        got = self.alerts("dev")
        self.assertEqual([(m["item_id"], m["from_agent"]) for m in got], [(x, "ops")])
        self.assertIn(f"#{y}", got[0]["body"])
        core.dep_add(self.c, x, [z], "dev")  # the holder adding its own prerequisite is not news
        self.assertEqual(self.alerts("dev"), [])

    def test_a_review_added_to_a_deploy_alerts_the_target_owner(self):
        a = self.add("site", "page")
        core.claim(self.c, a, "dev")
        core.target_own(self.c, "web", "ops")
        dep = core.done(self.c, a, "commit", "dev", ship_it=True)["shipped_in"]
        core.inbox(self.c, "ops")  # the ship request notice
        r = core.deploy_now(self.c, "web", review=True, actor="mark")
        got = self.alerts("ops")
        self.assertEqual([m["item_id"] for m in got], [dep])
        self.assertIn("review", got[0]["body"].lower())
        with self.assertRaises(RiverError):
            core.done(self.c, dep, "release", "ops")
        self.assertIsNotNone(r)


class ReleaseReview(Base):
    """With review on, a release waits on one review of everything it ships, not on a review per item."""

    def setUp(self):
        super().setUp()
        core.target_add(self.c, "web", "push, then smoke test")
        core.project_add(self.c, "site", target="web", path=self.dir.name)
        for n in ("dev", "rev", "ops"):
            core.register(self.c, n)
        core.config_set(self.c, "review", "on")
        core.config_set(self.c, "review_prompt", "run /code-review on the release diff")
        core.target_own(self.c, "web", "ops")
        self.a, self.b = self.add("site", "page"), self.add("site", "form")
        for i in (self.a, self.b):
            core.claim(self.c, i, "dev")
            core.done(self.c, i, "commit", "dev", ship_it=True)
        self.deploy = core.item_show(self.c, self.a)["unblocks"]
        self.review = [i for i in self.deploy if core.item_show(self.c, i)["kind"] == "review"][0]
        self.deploy = [i for i in self.deploy if i != self.review][0]

    def test_fail_with_ask_waits_on_the_user_and_adds_only_approved_fixes(self):
        core.register(self.c, "mark", human=True)
        core.claim(self.c, self.review, "rev")
        r = core.review_fail(self.c, self.review, ["null check in form", "rename a variable"],
                             note="crash on empty form", actor="rev", ask=True)
        h = r["asked"]["id"]
        it = core.item_show(self.c, h)
        self.assertEqual((it["doer"], it["kind"], it["notes"]), ("human", "fixes", "- null check in form\n- rename a variable"))
        self.assertEqual(core._item(self.c, self.review)["status"], "open")  # released; it waits on the person
        self.assertIn(h, core.item_show(self.c, self.review)["waits_on"])
        self.assertIn(h, [e["item_id"] for e in core.needs_you(self.c, "mark")])
        core.item_edit(self.c, h, notes="- null check in form", actor="mark")  # the person keeps one fix
        res = core.done(self.c, h, "approved one", "mark")
        self.assertEqual([f["title"] for f in res["fixes_added"]], ["null check in form"])
        fix = res["fixes_added"][0]["id"]
        self.assertIn(fix, core.item_show(self.c, self.review)["waits_on"])
        self.assertFalse(core.annotate(self.c)[self.review]["ready"])
        core.claim(self.c, fix, "dev")
        core.done(self.c, fix, "fixed", "dev")
        self.assertTrue(core.annotate(self.c)[self.review]["ready"])  # the review comes back after the fix

    def test_fail_with_ask_then_drop_adds_no_fixes(self):
        core.claim(self.c, self.review, "rev")
        h = core.review_fail(self.c, self.review, ["something"], actor="rev", ask=True)["asked"]["id"]
        core.drop(self.c, h, note="the user said no", actor="ops")
        self.assertTrue(core.annotate(self.c)[self.review]["ready"])
        self.assertEqual(len(core.item_show(self.c, self.review)["waits_on"]), 3)  # a, b, and the dropped ask

    def test_one_review_covers_the_release_and_gates_the_deploy(self):
        rv = core.item_show(self.c, self.review)
        self.assertEqual(sorted(rv["waits_on"]), sorted([self.a, self.b]))
        self.assertEqual(rv["context"], "run /code-review on the release diff")
        self.assertIn(self.review, core.item_show(self.c, self.deploy)["waits_on"])
        self.assertFalse(core.annotate(self.c)[self.deploy]["ready"])
        # The target owner gets nothing to deploy yet; a session in the folder gets the review first.
        b = core.go(self.c, self.dir.name, "rev")
        self.assertEqual((b["role"], b["item"]["id"]), ("reviewer", self.review))
        core.review_pass(self.c, self.review, None, "rev")
        g = core.go(self.c, self.dir.name, "ops")
        self.assertEqual((g["role"], g["item"]["id"]), ("deployer", self.deploy))

    def test_a_reviewer_that_wrote_a_commit_of_the_release_says_so(self):
        # #1616: the pin moved after the review went out and held commits of the reviewer. It released with a
        # note, and go gave it the same review again seconds later.
        from river import cli
        core.register(self.c, "rev2")
        b = core.go(self.c, self.dir.name, "rev")
        self.assertEqual((b["role"], b["item"]["id"]), ("reviewer", self.review))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.render_go(b)
        self.assertIn(f'release {self.review} --author "<your commits>"', out.getvalue())
        with self.assertRaisesRegex(RiverError, "say which commits"):
            core.release(self.c, self.review, actor="rev", author=" ")
        with self.assertRaisesRegex(RiverError, "held by rev, not rev2"):
            core.release(self.c, self.review, actor="rev2", author="abc123")
        it = core.release(self.c, self.review, "the pin moved", "rev", author="abc123 (#9)")
        self.assertEqual((it["status"], it["assignee"]), ("open", None))
        self.assertEqual(core.recent_events(self.c, 1)[0]["change"],
                         "released as an author of the release: abc123 (#9) (the pin moved)")
        self.assertIn("rev", core.release_authors(self.c, self.review))
        self.assertIn("worked on what release review", core.author_refusal(self.c, core._item(self.c, self.review), "rev"))
        # go, a claim, a push, and a session that waits: this review goes to rev no more.
        self.assertNotEqual(core.go(self.c, self.dir.name, "rev")["role"], "reviewer")
        self.assertEqual(core.go(self.c, self.dir.name, "rev", role="reviewer")["role"], "idle")
        for f in (lambda: core.claim(self.c, self.review, "rev"), lambda: core.push(self.c, self.review, "rev", actor="ops")):
            with self.assertRaisesRegex(RiverError, "worked on what release review"):
                f()
        core.wait(self.c, self.dir.name, "rev", step="0s", sleep=lambda s: None)
        core.wait(self.c, self.dir.name, "rev2", step="0s", sleep=lambda s: None)
        self.assertEqual(core.waiting_agent_for(self.c, "site", self.review), "rev2")
        # Another session takes it. A plain release keeps the reviewer (a review that failed comes back to it).
        b = core.go(self.c, self.dir.name, "rev2")
        self.assertEqual((b["role"], b["item"]["id"]), ("reviewer", self.review))
        core.release(self.c, self.review, "a break", "rev2")
        self.assertEqual(core.go(self.c, self.dir.name, "rev2")["item"]["id"], self.review)
        # The flag is for a review only.
        x = self.add("site", "other work")
        core.claim(self.c, x, "rev")
        with self.assertRaisesRegex(RiverError, "--author is for a release review"):
            core.release(self.c, x, actor="rev", author="abc123")

    def test_the_agent_that_wrote_the_release_never_gets_its_review(self):
        # #1027: the author's go after maxpm ship claimed the new review four times in one night.
        b = core.go(self.c, self.dir.name, "dev")
        self.assertNotEqual(b["role"], "reviewer")
        self.assertNotIn("claim_refused", b)
        self.assertEqual(core.go(self.c, self.dir.name, "dev", role="reviewer")["role"], "idle")
        skipped = []
        self.assertEqual(core.next_item(self.c, None, claim=True, actor="dev", limit=5, skipped=skipped), [])
        self.assertIn((self.review, "you worked on the release it reviews"), [(x["id"], x["why"]) for x in skipped])
        names = ["site"]
        self.assertIsNone(core._work_for(self.c, "dev", names, role="reviewer"))
        self.assertIsNotNone(core._work_for(self.c, "rev", names, role="reviewer"))
        core.register(self.c, "mark", human=True)
        for f in (lambda: core.claim(self.c, self.review, "dev"),
                  lambda: core.push(self.c, self.review, "dev", actor="ops"),
                  lambda: core.queue_add(self.c, "dev", self.review, actor="mark")):
            with self.assertRaises(RiverError) as e:
                f()
            self.assertIn("worked on what release review", str(e.exception))
        self.assertEqual(core._item(self.c, self.review)["status"], "open")
        # Another agent takes it; so may a person.
        b = core.go(self.c, self.dir.name, "rev")
        self.assertEqual((b["role"], b["item"]["id"]), ("reviewer", self.review))
        core.release(self.c, self.review, actor="rev")
        self.assertEqual(core.claim(self.c, self.review, "mark")["assignee"], "mark")

    def test_fail_adds_fixes_the_review_waits_on(self):
        core.claim(self.c, self.review, "rev")
        res = core.review_fail(self.c, self.review, ["escape the form input"], "XSS in form", actor="rev")
        fix = res["fixes"][0]
        self.assertEqual(fix["project"], "site")
        self.assertEqual(res["status"], "open")
        self.assertIn(fix["id"], res["waits_on"])
        self.assertIn("XSS in form", core.item_show(self.c, fix["id"])["context"])
        self.assertFalse(core.annotate(self.c)[self.review]["ready"])
        core.claim(self.c, fix["id"], "dev")
        core.done(self.c, fix["id"], "fixed", "dev")
        self.assertTrue(core.annotate(self.c)[self.review]["ready"])
        with self.assertRaises(RiverError):
            core.review_fail(self.c, self.review, [], actor="rev")

    def test_review_cmd_must_pass(self):
        self.c.execute("UPDATE items SET \"check\"='make test' WHERE id=?", (self.review,))
        core.claim(self.c, self.review, "rev")

        class R:
            def __init__(self, code):
                self.returncode, self.stdout, self.stderr = code, "1 failed", ""
        with self.assertRaises(RiverError) as e:
            core.review_pass(self.c, self.review, None, "rev", runner=lambda c, d: R(1))
        self.assertIn("1 failed", str(e.exception))
        res = core.review_pass(self.c, self.review, None, "rev", runner=lambda c, d: R(0))
        self.assertEqual((res["status"], res["review_cmd"]), ("done", "make test"))

    def test_review_cmd_past_review_timeout_is_stopped_with_its_children(self):
        # A gate that runs past review_timeout stops, with the processes it started, and the error names
        # the setting; the reviewer's lease stays renewed while it runs (#1142).
        # The gate is a Python script, so the same command runs in sh and in cmd.exe (Windows).
        pidfile = os.path.join(self.dir.name, "child.pid")
        gate = os.path.join(self.dir.name, "gate.py")
        with open(gate, "w") as f:
            f.write("import subprocess, sys\n"
                    "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                    f"open({pidfile!r}, 'w').write(str(p.pid))\n"
                    "print('started', flush=True)\n"
                    "p.wait()\n")
        self.c.execute("UPDATE items SET \"check\"=? WHERE id=?", (f'"{sys.executable}" "{gate}"', self.review))
        core.config_set(self.c, "review_timeout", "2s")
        core.claim(self.c, self.review, "rev")
        self.c.execute("UPDATE items SET lease_expires_at='2000-01-01T00:00:00Z' WHERE id=?", (self.review,))
        old = core.REVIEW_RENEW_EVERY
        core.REVIEW_RENEW_EVERY = 0.3
        self.addCleanup(setattr, core, "REVIEW_RENEW_EVERY", old)
        t = time.monotonic()
        with self.assertRaises(RiverError) as e:
            core.review_pass(self.c, self.review, None, "rev")
        self.assertLess(time.monotonic() - t, 20)
        msg = str(e.exception)
        self.assertIn("review_timeout (2s)", msg)
        self.assertIn("maxpm config set review_timeout", msg)
        self.assertIn("started", msg)
        it = core.item_show(self.c, self.review)
        self.assertEqual(it["status"], "in_progress")
        self.assertGreater(core.parse_iso(it["lease_expires_at"]), core.now())
        with open(pidfile) as f:
            pid = int(f.read())
        for _ in range(40):
            if not core.pid_alive(pid):
                break
            time.sleep(0.05)
        else:
            self.fail("the review command's child process still runs")
        core.config_set(self.c, "review_timeout", "0s")  # no limit
        self.c.execute("UPDATE items SET \"check\"=? WHERE id=?", (f'"{sys.executable}" -c "pass"', self.review))
        self.assertEqual(core.review_pass(self.c, self.review, None, "rev")["status"], "done")

    def test_review_steps_add_edit_move_remove(self):
        a = core.review_step_add(self.c, "site", "read every diff")
        b = core.review_step_add(self.c, "site", "make test", run=True)
        c = core.review_step_add(self.c, "site", "check the page on a phone", at=1)
        self.assertEqual([s["id"] for s in core.review_steps(self.c, "site")], [c["id"], a["id"], b["id"]])
        self.assertEqual([s["pos"] for s in core.review_steps(self.c, "site")], [1, 2, 3])
        core.review_step_move(self.c, c["id"], 3)
        self.assertEqual([s["id"] for s in core.review_steps(self.c, "site")], [a["id"], b["id"], c["id"]])
        e = core.review_step_edit(self.c, a["id"], text="read every diff twice", run=True)
        self.assertEqual((e["text"], e["kind"]), ("read every diff twice", "run"))
        core.review_step_remove(self.c, b["id"])
        self.assertEqual([(s["id"], s["pos"]) for s in core.review_steps(self.c, "site")], [(a["id"], 1), (c["id"], 2)])
        with self.assertRaises(RiverError):
            core.review_step_add(self.c, "site", "  ")
        with self.assertRaises(RiverError):
            core.review_step_add(self.c, "nope", "x")

    def test_pass_follows_the_steps_of_each_project(self):
        do = core.review_step_add(self.c, "site", "read every diff")
        cmd = core.review_step_add(self.c, "site", "make test", run=True)
        rv = core.item_show(self.c, self.review)
        self.assertEqual([s["id"] for s in rv["review_steps"][0]["steps"]], [do["id"], cmd["id"]])
        core.claim(self.c, self.review, "rev")
        ran = []

        class R:
            def __init__(self, code):
                self.returncode, self.stdout, self.stderr = code, "", "boom"
        with self.assertRaises(RiverError) as e:
            core.review_pass(self.c, self.review, None, "rev", runner=lambda c, d: ran.append(c) or R(0))
        self.assertIn("read every diff", str(e.exception))
        self.assertEqual(ran, [])  # nothing runs before the written steps are confirmed
        with self.assertRaises(RiverError) as e:
            core.review_pass(self.c, self.review, None, "rev", confirm="all", runner=lambda c, d: R(2))
        self.assertIn("make test", str(e.exception))
        res = core.review_pass(self.c, self.review, None, "rev", confirm=[str(do["id"])],
                               runner=lambda c, d: ran.append((c, d)) or R(0))
        self.assertEqual(res["status"], "done")
        self.assertEqual([(c, os.path.realpath(d)) for c, d in ran], [("make test", os.path.realpath(self.dir.name))])  # in the project folder
        self.assertEqual([r["result"] for r in res["review_steps"]], ["confirmed", "exit 0"])
        hist = [e["change"] for e in core.item_show(self.c, self.review)["events"]]
        self.assertTrue(any(f"review step {do['id']} (site) confirmed" in h for h in hist))

    def test_after_the_cut_new_work_joins_the_next_release_with_a_review_of_its_own(self):
        # #1619: nine late ship requests joined a release whose review had passed; it got a second review, and
        # its deploy item could not close for the release that was live.
        before = core.item_show(self.c, self.deploy)["waits_on"]
        core.claim(self.c, self.review, "rev")  # the reviewer takes the review: the cut
        cut = core._item(self.c, self.deploy)
        self.assertTrue(cut["cut_at"])
        self.assertTrue(any(e["change"].startswith("release cut with 2 items (rev took review")
                            for e in core.item_show(self.c, self.deploy)["events"]))
        c = self.add("site", "late")
        core.claim(self.c, c, "dev")
        core.done(self.c, c, "commit", "dev")
        nxt = core.ship(self.c, c, "dev")
        self.assertNotEqual(nxt["id"], self.deploy)
        self.assertEqual(core.item_show(self.c, self.deploy)["waits_on"], before)  # exactly the items of the cut
        (new,) = [i for i in nxt["waits_on"] if core.item_show(self.c, i)["kind"] == "review"]
        self.assertEqual(core.item_show(self.c, new)["waits_on"], [c])
        # The next release starts after this one is done: its review is not ready, and go gives it to nobody.
        core.register(self.c, "rev2")
        it = core.annotate(self.c)[new]
        self.assertEqual((it["ready"], it["blocked_until"]), (False, None))
        self.assertIn(f"release order of target web: release #{self.deploy}", it["blocked_reason"])
        self.assertIsNone(core.go(self.c, self.dir.name, "rev2", role="reviewer").get("item"))
        p = nxt["release"]
        self.assertEqual((p["waits"], p["current"]["id"], p["deploy"], p["in_cut"]), (True, self.deploy, nxt["id"], False))
        # The release that is cut closes with its own items.
        core.review_pass(self.c, self.review, "ok", "rev")
        core.claim(self.c, self.deploy, "ops")
        core.done(self.c, self.deploy, "release v1", "ops")
        core.activity(self.c, "dev")  # any command runs the sweep
        it = core.annotate(self.c)[new]
        self.assertEqual((it["ready"], it["blocked_reason"]), (True, None))
        self.assertEqual(core.go(self.c, self.dir.name, "rev2", role="reviewer")["item"]["id"], new)

    def test_a_fix_that_the_review_of_a_cut_release_waits_on_goes_out_with_that_release(self):
        core.claim(self.c, self.review, "rev")
        fix = core.review_fail(self.c, self.review, ["null check in form"], actor="rev")["fixes"][0]["id"]
        core.claim(self.c, fix, "dev")
        r = core.done(self.c, fix, "fixed", "dev", ship_it=True)
        self.assertEqual((r["shipped_in"], r["ship_release"]["in_cut"]), (self.deploy, True))
        self.assertIn(fix, core.item_show(self.c, self.deploy)["waits_on"])
        self.assertTrue(core.annotate(self.c)[self.review]["ready"])  # the review comes back, in the same release

    def test_the_owner_cuts_a_release_with_a_command_before_the_review_starts(self):
        from river import cli
        core.register(self.c, "mark", human=True)
        with self.assertRaisesRegex(RiverError, r"the target owner \(ops\), the manager, or a person cuts"):
            core.target_cut(self.c, "web", "abc1234", "dev")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.dict(os.environ, {"MAXPM_DB": self.path, "MAXPM_QUIET": "1"}):
            cli.run(["--as", "ops", "target", "cut", "web", "--rev", "abc1234"])
        self.assertIn(f"release #{self.deploy} of web is cut with 2 items", out.getvalue())
        dep = core.item_show(self.c, self.deploy)
        self.assertTrue(dep["cut_at"])
        self.assertIn("abc1234", dep["notes"])
        self.assertTrue(any("maxpm target cut by ops): abc1234" in e["change"] for e in dep["events"]))
        self.assertTrue(core.annotate(self.c)[self.review]["ready"])  # the cut release goes on: its review is ready
        with self.assertRaisesRegex(RiverError, f"nothing is collected for web.*Release #{self.deploy} is cut already"):
            core.target_cut(self.c, "web", None, "ops")
        c = self.add("site", "late")
        core.claim(self.c, c, "dev")
        core.done(self.c, c, "commit", "dev")
        nxt = core.ship(self.c, c, "dev")
        self.assertNotEqual(nxt["id"], self.deploy)
        # The next release cannot be cut while this one runs; with a reason it can start sooner.
        with self.assertRaisesRegex(RiverError, "runs now.*release-now web"):
            core.target_cut(self.c, "web", "def5678", "ops")
        r = core.release_now(self.c, "web", "a fix of a production defect", "mark")
        self.assertEqual((r["waits"], r["item"]["ready"]), (False, True))
        self.assertTrue(core.target_cut(self.c, "web", "def5678", "mark")["cut"]["cut_at"])

    def test_review_off_changes_nothing(self):
        core.config_set(self.c, "review", "off")
        c = self.add("site", "x")
        d = core.ship(self.c, c, "dev")
        self.assertEqual(d["id"], self.deploy)
        self.assertEqual([i for i in core.item_show(self.c, c)["unblocks"]], [self.deploy])


class TrackerRefs(Base):
    def setUp(self):
        super().setUp()
        core.project_add(self.c, "a")
        core.project_add(self.c, "b")

    def test_refs_link_show_and_refuse_duplicates_in_a_project(self):
        refs = core.parse_refs(["github:o/r#12", "jira:PROJ-1"], ["", "https://x.atlassian.net/browse/PROJ-1"])
        i = core.item_add(self.c, "a", "fix it", actor="t", refs=refs)
        self.assertEqual([(r["ref"], r["url"]) for r in i["refs"]],
                         [("github:o/r#12", "https://github.com/o/r/issues/12"),
                          ("jira:PROJ-1", "https://x.atlassian.net/browse/PROJ-1")])
        with self.assertRaises(RiverError) as e:
            core.item_add(self.c, "a", "again", actor="t", refs=core.parse_refs(["jira:PROJ-1"]))
        self.assertIn(f"#{i['id']}", str(e.exception))
        self.assertEqual(len(core.item_list(self.c, "a")), 1)  # the refused add left nothing behind
        core.item_add(self.c, "b", "other project", actor="t", refs=core.parse_refs(["jira:PROJ-1"]))
        core.done(self.c, i["id"], "ok", "t")
        core.item_add(self.c, "a", "reopened upstream", actor="t", refs=core.parse_refs(["jira:PROJ-1"]))
        self.assertEqual(len(core.item_list(self.c, ref="jira:PROJ-1")), 3)  # closed ones too

    def test_edit_adds_and_removes_refs_and_bad_forms_are_refused(self):
        i = self.add("a", "x")
        core.item_edit(self.c, i, refs=core.parse_refs(["linear:ENG-42"]), actor="t")
        self.assertEqual([r["ref"] for r in core.item_show(self.c, i)["refs"]], ["linear:ENG-42"])
        core.item_edit(self.c, i, unref=["linear:ENG-42"], actor="t")
        self.assertEqual(core.item_show(self.c, i)["refs"], [])
        with self.assertRaises(RiverError):
            core.item_edit(self.c, i, unref=["linear:ENG-42"], actor="t")
        for bad in (["PROJ-1"], ["jira: x"], ["Jira:X"]):
            with self.assertRaises(RiverError):
                core.parse_refs(bad)
        with self.assertRaises(RiverError):
            core.parse_refs(["jira:X"], ["https://a", "https://b"])
        with self.assertRaises(RiverError):
            core.parse_refs(["jira:X"], ["javascript:alert(1)"])


class ProjectTracker(Base):
    def test_set_show_clear_and_briefs(self):
        core.project_add(self.c, "a", path=self.dir.name)
        self.assertEqual(core.project_tracker(self.c, "a")["tracker"], "")
        core.project_tracker(self.c, "a", "github o/r via gh", "t")
        self.assertEqual(core.project_show(self.c, "a")["tracker"], "github o/r via gh")
        self.assertEqual(core.project_list(self.c)[0]["tracker"], "github o/r via gh")
        self.assertEqual(core.plan(self.c, self.dir.name, "p")["trackers"], {"a": "github o/r via gh"})
        core.item_add(self.c, "a", "x", actor="t")
        self.assertEqual(core.go(self.c, self.dir.name, "w")["trackers"], {"a": "github o/r via gh"})
        core.project_tracker(self.c, "a", "none", "t")
        self.assertEqual(core.project_tracker(self.c, "a", "none", "t")["tracker"], "")  # clearing twice is fine


class TrackerWriteBack(Base):
    def setUp(self):
        super().setUp()
        core.project_add(self.c, "a")
        core.register(self.c, "ag")
        core.project_tracker(self.c, "a", "github o/r via gh")
        self.i = core.item_add(self.c, "a", "fix", actor="t", refs=core.parse_refs(["github:o/r#3", "jira:P-1"]))["id"]
        core.claim(self.c, self.i, "ag")

    def test_done_leaves_a_reminder_until_synced(self):
        with self.assertRaises(RiverError):
            core.synced(self.c, self.i, actor="ag")  # not done yet
        res = core.done(self.c, self.i, "abc", "ag")
        self.assertEqual(res["tracker"], "github o/r via gh")
        self.assertEqual([u["id"] for u in core.unsynced(self.c, "ag")], [self.i])
        self.assertEqual(core.unsynced(self.c, "other"), [])
        self.assertEqual(core.go(self.c, self.dir.name, "ag", project="a")["unsynced"][0]["id"], self.i)
        core.synced(self.c, self.i, "jira:P-1", "ag")
        self.assertEqual([x["ref"] for x in core.unsynced(self.c)[0]["refs"]], ["github:o/r#3"])
        with self.assertRaises(RiverError):
            core.synced(self.c, self.i, "jira:NOPE", "ag")
        core.synced(self.c, self.i, actor="ag")
        self.assertEqual(core.status(self.c)["unsynced"], [])

    def test_done_synced_records_it_at_once(self):
        res = core.done(self.c, self.i, "abc", "ag", synced_=True)
        self.assertTrue(all(x["synced_at"] for x in res["refs"]))
        self.assertEqual(core.unsynced(self.c), [])

    def test_old_item_refs_table_gets_the_column(self):
        self.c.execute("CREATE TABLE r2 AS SELECT item_id, ref, url, created_at FROM item_refs")
        self.c.execute("DROP TABLE item_refs")
        self.c.execute("ALTER TABLE r2 RENAME TO item_refs")
        self.c.close()
        self.c = core.connect(self.path)
        self.assertIn("synced_at", {r["name"] for r in self.c.execute("PRAGMA table_info(item_refs)")})


class Version(unittest.TestCase):
    def test_version_flag(self):
        import contextlib
        import io
        from river import __version__, cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as e:
            cli.build_parser().parse_args(["--version"])
        self.assertEqual((e.exception.code, out.getvalue().strip()), (0, f"maxpm {__version__}"))


class Cleanup(Base):
    def setUp(self):
        super().setUp()
        core.project_add(self.c, "a", path=self.dir.name)
        core.register(self.c, "ag")

    def _expire(self, i):
        core.claim(self.c, i, "ag")
        self.c.execute("UPDATE items SET lease_expires_at='2000-01-01T00:00:00Z' WHERE id=?", (i,))
        with core.tx(self.c):
            core._sweep(self.c)

    def test_expired_lease_is_suspect_until_checked(self):
        i = self.add("a", "half done")
        self._expire(i)
        self.assertTrue(core.item_show(self.c, i)["needs_check"])
        self.assertEqual(core.cleanup(self.c, git=False)[0]["reasons"], ["lease expired"])
        b = core.go(self.c, self.dir.name, "other")
        self.assertTrue(b["item"]["needs_check"])
        core.release(self.c, i, None, "other")
        with self.assertRaises(RiverError):
            core.check(self.c, i, "partial", actor="t")  # partial needs a note
        core.check(self.c, i, "partial", "tests missing", "t")
        self.assertIn("[checked partial] tests missing", core.item_show(self.c, i)["notes"])
        self.assertEqual(core.cleanup(self.c, git=False), [])

    def test_commit_naming_the_item_and_check_done(self):
        import subprocess
        i = self.add("a", "write the page")
        git = lambda *a: subprocess.run(["git", "-C", self.dir.name, *a], capture_output=True, check=True)
        git("init", "-q"); git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
                               "-m", f"Write the page (#{i})")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", f"unrelated #{i}0")
        rows = core.cleanup(self.c)
        self.assertEqual((rows[0]["id"], rows[0]["reasons"]), (i, ["commit names it"]))
        self.assertEqual(len(rows[0]["evidence"]), 1)  # #{i}0 is another item
        self.assertEqual(core.cleanup(self.c, git=False), [])
        with self.assertRaises(RiverError):
            core.check(self.c, i, "done", actor="t")  # done needs where the work is
        res = core.check(self.c, i, "done", "commit abc", "t")
        self.assertEqual((res["status"], res["output"]), ("done", "found done in a check: commit abc"))

    def test_stale_ready_item(self):
        i = self.add("a", "old")
        self.c.execute("UPDATE items SET created_at='2020-01-01T00:00:00Z' WHERE id=?", (i,))
        self.assertEqual(core.cleanup(self.c, git=False)[0]["reasons"], ["stale"])
        core.config_set(self.c, "stale_after", "100000d")
        self.assertEqual(core.cleanup(self.c, git=False), [])
        core.config_set(self.c, "stale_after", "14d")
        core.check(self.c, i, "open", None, "t")  # still wanted: the check restarts the clock
        self.assertEqual(core.cleanup(self.c, git=False), [])


class Wait(Base):
    def setUp(self):
        super().setUp()
        core.project_add(self.c, "a", path=self.dir.name)
        core.project_add(self.c, "b")
        core.register(self.c, "w")

    def wait(self, **kw):
        return core.wait(self.c, self.dir.name, "w", step=kw.pop("step", "0s"), sleep=lambda s: None, **kw)

    def test_again_then_work_from_a_push_or_ready_item(self):
        self.assertEqual(self.wait()["result"], "again")
        self.assertEqual(core.agent_status(self.c, "w")["role"], "waiting")
        cap = core.capacity(self.c)
        self.assertEqual((cap["agents_waiting"], [x["kind"] for x in cap["advice"]]), (["w"], ["waiting"]))
        other = self.add("b", "elsewhere")  # not in the folder's project: no wake
        self.assertEqual(self.wait()["result"], "again")
        core.push(self.c, other, "w", None, "t")
        res = self.wait()
        self.assertEqual((res["result"], res["why"]), ("work", f"#{other} was pushed to you"))
        core.decline(self.c, other, None, "w")
        i = self.add("a", "here")
        self.assertIn(f"#{i} is ready", self.wait()["why"])
        core.go(self.c, self.dir.name, "w")
        self.assertIsNone(core.agent_status(self.c, "w")["waiting_since"])  # work ends the wait
        with self.assertRaises(RiverError):
            self.wait()  # it holds an item now

    def test_end_after_wait_max_releases_goals_and_unregisters(self):
        core.goal_add(self.c, "a", "g")
        core.goal_own(self.c, "g", "w")
        core.config_set(self.c, "wait_max", "0s")
        self.assertEqual(self.wait()["result"], "end")
        self.assertIsNone(self.c.execute("SELECT 1 FROM agents WHERE name='w'").fetchone())
        self.assertIsNone(core.goal_show(self.c, "g")["owner"])

    def test_capacity_still_warns_about_idle_sessions_that_do_not_wait(self):
        core.register(self.c, "idle1")
        self.wait()
        kinds = {x["kind"]: x["text"] for x in core.capacity(self.c)["advice"]}
        self.assertIn("waiting", kinds)
        self.assertIn("1 session(s) have no work", kinds["too_many"])


class Models(Base):
    """Recommended model and effort per item, min/max limits, and sessions that declare their model (#319)."""

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "a")
        core.project_path(self.c, "a", self.dir.name)
        for n in ("s1", "s2"):
            core.register(self.c, n)

    def test_add_and_edit_store_the_fields_and_validate_them(self):
        i = core.item_add(self.c, "a", "x", models={"model": "Opus", "effort": "high", "min_model": "opus, sol"})
        self.assertEqual((i["model"], i["effort"], i["min_model"], i["model_from"]), ("opus", "high", "opus, sol", "item"))
        with self.assertRaisesRegex(RiverError, "effort is one of"):
            core.item_edit(self.c, i["id"], models={"effort": "huge"})
        with self.assertRaisesRegex(RiverError, "not in the model_ladder"):
            core.item_edit(self.c, i["id"], models={"max_model": "gpt5"})
        with self.assertRaisesRegex(RiverError, "one model per family"):
            core.item_edit(self.c, i["id"], models={"max_model": "opus, fable"})
        with self.assertRaisesRegex(RiverError, "stronger than max"):
            core.item_edit(self.c, i["id"], models={"max_model": "sonnet"})
        e = core.item_edit(self.c, i["id"], models={"min_model": "none", "model": ""})
        self.assertIsNone(e["min_model"])
        self.assertIsNone(e["model"])
        self.assertEqual(e["effort"], "high")

    def test_ladder_has_no_order_across_families(self):
        ladder = core.parse_ladder(core.setting(self.c, "model_ladder"))
        self.assertEqual(ladder["claude"], ["haiku", "sonnet", "opus", "fable"])
        self.assertEqual(ladder["openai"], ["luna", "terra", "sol", "astra"])
        self.assertEqual(core.model_check(ladder, "sonnet", "opus", None)[0], False)
        self.assertEqual(core.model_check(ladder, "fable", None, "opus")[0], False)
        self.assertEqual(core.model_check(ladder, "opus", "opus", "opus"), (True, ""))
        ok, why = core.model_check(ladder, "astra", "opus", None)
        self.assertTrue(ok)
        self.assertIn("another family", why)
        self.assertFalse(core.model_check(ladder, "terra", "opus, sol", None)[0])
        ok, why = core.model_check(ladder, "mystery", "opus", None)
        self.assertTrue(ok)
        self.assertIn("not in model_ladder", why)
        with self.assertRaisesRegex(RiverError, "more than one place"):
            core.config_set(self.c, "model_ladder", "a: x, y; b: y")

    def test_defaults_come_from_project_and_kind_settings(self):
        core.config_set(self.c, "default_model", "sonnet", project="a")
        core.config_set(self.c, "default_effort", "low", kind="work")
        core.config_set(self.c, "default_max_model", "sonnet", project="a")
        i = self.add("a", "monitor")
        own = core.item_add(self.c, "a", "big", models={"model": "fable", "min_model": "opus"})["id"]
        ann = core.annotate(self.c)
        self.assertEqual((ann[i]["model"], ann[i]["model_from"]), ("sonnet", "project:a"))
        self.assertEqual((ann[i]["effort"], ann[i]["effort_from"]), ("low", "kind:work"))
        self.assertEqual(ann[own]["model"], "fable")
        # The project's max sonnet contradicts the item's own min opus: the item's own limit wins.
        self.assertEqual((ann[own]["min_model"], ann[own]["max_model"]), ("opus", None))
        self.assertEqual(core.setting(self.c, "default_effort", item_id=i), "low")
        with self.assertRaisesRegex(RiverError, "effort is one of"):
            core.config_set(self.c, "default_effort", "extreme")

    def test_a_session_gets_only_items_its_model_is_allowed_for(self):
        big = core.item_add(self.c, "a", "big", 1, models={"min_model": "opus"})["id"]
        small = core.item_add(self.c, "a", "small", 2, models={"max_model": "sonnet"})["id"]
        rec = core.item_add(self.c, "a", "rec", 3, models={"model": "fable"})["id"]
        core.set_agent_model(self.c, "s1", "sonnet")
        skipped = []
        got = core.next_item(self.c, "a", actor="s1", limit=5, skipped=skipped)
        self.assertEqual([x["id"] for x in got], [small, rec])  # the recommendation alone never blocks
        self.assertEqual([x["id"] for x in skipped], [big])
        self.assertIn("needs at least opus", skipped[0]["why"])
        with self.assertRaisesRegex(RiverError, "needs at least opus"):
            core.claim(self.c, big, "s1")
        # Without a declared model nothing is filtered; with --model the filter applies without an agent.
        self.assertEqual(len(core.next_item(self.c, "a", limit=5)), 3)
        self.assertEqual([x["id"] for x in core.next_item(self.c, "a", limit=5, model="fable")], [big, rec])

    def test_go_records_the_model_and_says_what_it_skipped(self):
        big = core.item_add(self.c, "a", "big", 1, models={"min_model": "opus"})["id"]
        small = self.add("a", "small", 2)
        b = core.go(self.c, self.dir.name, "s1", model="sonnet")
        self.assertEqual(b["item"]["id"], small)
        self.assertEqual(b["model"], "sonnet")
        self.assertEqual([x["id"] for x in b["model_skipped"]], [big])
        self.assertEqual(core.agent_model(self.c, "s1"), "sonnet")
        b2 = core.go(self.c, self.dir.name, "s2", model="opus")
        self.assertEqual(b2["item"]["id"], big)


class Monitor(Base):
    """A target's monitor text: claiming a deploy adds a monitor item; its session follows the deploy (#321)."""

    def setUp(self):
        super().setUp()
        core.target_add(self.c, "web", "push, then smoke test")
        core.project_add(self.c, "site", target="web", path=self.dir.name)
        for n in ("dev", "ops", "mon", "big"):
            core.register(self.c, n)
        core.register(self.c, "mark", human=True)
        w = self.add("site", "page")
        core.claim(self.c, w, "dev")
        core.done(self.c, w, "abc", "dev", ship_it=True)
        self.deploy = core.item_show(self.c, w)["unblocks"][0]

    def monitors(self):
        return [r["id"] for r in self.c.execute("SELECT id FROM items WHERE kind='monitor'")]

    def test_no_monitor_text_no_monitor_item(self):
        core.target_own(self.c, "web", "ops")
        b = core.go(self.c, self.dir.name, "ops")
        self.assertEqual((b["role"], b["monitor_item"]), ("deployer", None))
        self.assertEqual(self.monitors(), [])

    def test_claiming_a_deploy_adds_one_monitor_with_a_weak_model(self):
        core.target_monitor(self.c, "web", "check https://example.test/health for 15 minutes")
        self.assertEqual(core.target_show(self.c, "web")["monitor"], "check https://example.test/health for 15 minutes")
        core.target_own(self.c, "web", "ops")
        b = core.go(self.c, self.dir.name, "ops")
        (m,) = self.monitors()
        self.assertEqual(b["monitor_item"]["id"], m)
        a = core.annotate(self.c)[m]
        self.assertEqual((a["found_during"], a["target"], a["doer"], a["ready"]), (self.deploy, "web", "ai", True))
        self.assertIn("example.test/health", a["context"])
        self.assertIn("ships #", a["context"])
        self.assertEqual((a["model"], a["effort"], a["max_model"], a["max_model_from"]), ("sonnet", "low", "sonnet", "kind:monitor"))
        self.assertEqual([p["id"] for p in core.pending_monitors(self.c)], [m])
        view = core.targets_view(self.c)[0]
        self.assertEqual(view["pending"][0]["monitors"][0]["id"], m)
        # A person's own setting for monitors wins over the built-in default.
        core.config_set(self.c, "default_max_model", "opus", kind="monitor")
        self.assertEqual(core.annotate(self.c)[m]["max_model"], "opus")

    def test_the_monitor_session_gets_its_briefing(self):
        core.target_monitor(self.c, "web", "watch the error rate for 10 minutes")
        core.target_own(self.c, "web", "ops")
        core.go(self.c, self.dir.name, "ops")
        (m,) = self.monitors()
        refused = core.go(self.c, self.dir.name, "big", focus=f"monitor:{m}", model="fable")
        self.assertIn("allows at most sonnet", refused["focus_note"])
        core.set_agent_model(self.c, "big", None)
        b = core.go(self.c, self.dir.name, "mon", focus=f"monitor:{m}", model="sonnet")
        self.assertEqual((b["role"], b["item"]["id"]), ("monitor", m))
        self.assertEqual(b["monitor"]["deploy"]["id"], self.deploy)
        self.assertEqual((b["monitor"]["deployer"], b["monitor"]["person"]), ("ops", "mark"))
        self.assertEqual(core.pending_monitors(self.c), [])
        again = core.go(self.c, self.dir.name, "mon")
        self.assertEqual((again["role"], again.get("resumed")), ("monitor", True))


class GoalLeases(Base):
    """Items of the agent's own goals and targets count against goal_max_leases, apart from max_leases (#414)."""

    def test_goal_work_counts_apart(self):
        core.project_add(self.c, "a")
        core.register(self.c, "o")
        core.goal_add(self.c, "a", "ship")
        core.goal_own(self.c, "ship", "o")
        plain, other = self.add("a", "plain"), self.add("a", "other")
        g = [core.item_add(self.c, "a", f"g{n}", goals=["ship"])["id"] for n in range(4)]
        prereq = self.add("a", "prereq")
        core.dep_add(self.c, g[3], [prereq])
        core.claim(self.c, plain, "o")
        with self.assertRaisesRegex(RiverError, r"max_leases 1"):
            core.claim(self.c, other, "o")
        core.claim(self.c, g[0], "o")  # a goal item: the owner takes it although it holds plain
        core.claim(self.c, prereq, "o")  # it unblocks a goal item: goal work too
        core.claim(self.c, g[1], "o")
        with self.assertRaisesRegex(RiverError, r"goal_max_leases 3"):
            core.claim(self.c, g[2], "o")
        core.config_set(self.c, "goal_max_leases", "4", agent="o")
        core.claim(self.c, g[2], "o")

    def test_a_target_owner_takes_its_deploy_while_it_holds_work(self):
        core.target_add(self.c, "web")
        core.project_add(self.c, "site", target="web")
        for n in ("dev", "ops", "x"):
            core.register(self.c, n)
        w = self.add("site", "page")
        core.claim(self.c, w, "dev")
        core.done(self.c, w, "abc", "dev", ship_it=True)
        dep = core.item_show(self.c, w)["unblocks"][0]
        core.target_own(self.c, "web", "ops")
        busy = self.add("site", "busy")
        core.claim(self.c, busy, "ops")
        core.claim(self.c, dep, "ops")
        self.assertEqual(core.item_show(self.c, dep)["assignee"], "ops")
        # give: the same rule for the one who receives
        core.claim(self.c, self.add("site", "x's work"), "x")
        with self.assertRaisesRegex(RiverError, r"max_leases 1"):
            core.give(self.c, busy, "x", "ops")


class AgentQueue(Base):
    """maxpm queue: one agent's ordered queue of items and instructions, read before the project queue (#425)."""

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "a", path=self.dir.name)
        core.project_add(self.c, "b")
        for n in ("s1", "s2", "boss"):
            core.register(self.c, n)
        core.register(self.c, "mark", human=True)

    def test_queue_comes_first_and_is_kept_from_others(self):
        top = self.add("a", "top", 0)
        other = self.add("b", "in another project", 4)
        later = self.add("a", "later", 3)
        blocker = self.add("a", "blocker", 3)
        waits = self.add("a", "waits", 3, after=[blocker])
        for i in (waits, other, later):
            core.queue_add(self.c, "s1", i, actor="mark")
        core.queue_move(self.c, "s1", later, before=other, actor="mark")
        self.assertEqual([e["item"] for e in core.queue_list(self.c, "s1")["entries"]], [waits, later, other])
        with self.assertRaisesRegex(RiverError, "already in the queue of s1"):
            core.queue_add(self.c, "s2", later, actor="mark")
        with self.assertRaisesRegex(RiverError, "in the queue of s1"):
            core.claim(self.c, later, "s2")
        self.assertNotIn(later, [x["id"] for x in core.next_item(self.c, actor="s2", limit=9)])
        self.assertEqual([x["id"] for x in core.next_item(self.c, "a", actor="s1", limit=2)], [later, other])
        b = core.go(self.c, self.dir.name, "s1")  # waits is not ready: later, although top is more important
        self.assertEqual((b["item"]["id"], b["why"]), (later, f"#{later} is the first ready item in your queue"))
        self.assertEqual([e["item"] for e in core.queue_list(self.c, "s1")["entries"]], [waits, other])
        self.assertNotEqual(top, later)

    def test_a_queue_and_a_push_never_hold_one_item_for_two_agents(self):
        # #989: serve pushed an item to a waiting session, a manager then queued it for another; the push stayed,
        # so go in the queued session said idle (the claim was refused) until the pushed session declined.
        x = self.add("a", "pushed, then queued")
        core.push(self.c, x, "s2", actor="mark")
        core.queue_add(self.c, "s1", x, actor="mark")
        it = core.item_show(self.c, x)
        self.assertEqual((it["reserved_for"], it["reserved_until"]), ("s1", None))  # the queue's, not the push's
        self.assertIn("push to s2 ended: queued for s1", [e["change"] for e in it["events"]])
        self.assertTrue(any("in the queue of s1 now" in m["body"] for m in core.inbox(self.c, "s2")))
        with self.assertRaisesRegex(RiverError, "not pushed to s2"):
            core.accept(self.c, x, "s2")
        b = core.go(self.c, self.dir.name, "s1", focus="item:999")
        self.assertEqual((b["role"], b["item"]["id"]), ("worker", x))
        # The other way round: a push of a queued item to another agent is refused.
        y = self.add("a", "queued, then pushed")
        core.queue_add(self.c, "s1", y, actor="mark")
        with self.assertRaisesRegex(RiverError, "in the queue of s1; remove it there first: maxpm queue remove s1"):
            core.push(self.c, y, "s2", actor="mark")
        core.push(self.c, y, "s1", actor="mark")  # to the agent whose queue holds it: fine
        # A reservation with no end (a kept prerequisite) is not ended by a queue.
        z = self.add("a", "kept for s2")
        self.c.execute("UPDATE items SET reserved_for='s2' WHERE id=?", (z,))
        with self.assertRaisesRegex(RiverError, "reserved for s2; that agent, a person, or a manager ends"):
            core.queue_add(self.c, "s1", z, actor="mark")

    def test_instructions_are_read_first_and_only_people_or_managers_change_queues(self):
        x = self.add("a", "x")
        with self.assertRaisesRegex(RiverError, "a person or a manager changes queues"):
            core.queue_add(self.c, "s1", x, actor="s2")
        core.queue_add(self.c, "s1", message="commit what you have, then take #%d" % x, actor="mark")
        self.assertIn("instruction", core._work_for(self.c, "s1", ["a"]))  # maxpm wait wakes at once
        b = core.go(self.c, self.dir.name, "s1")
        (n,) = b["queue_instructions"]
        self.assertEqual((n["body"][:6], n["added_by"]), ("commit", "mark"))
        self.assertIsNotNone(core.queue_list(self.c, "s1")["entries"][0]["delivered_at"])
        with self.assertRaisesRegex(RiverError, "refused"):
            core.queue_remove(self.c, "s1", "e%d" % n["id"], actor="s2")
        core.queue_remove(self.c, "s1", "e%d" % n["id"], actor="s1")  # its own instruction, after it acts
        self.assertEqual(core.queue_list(self.c, "s1")["entries"], [])
        # A manager may change queues too.
        self.c.execute("UPDATE agents SET role='manager' WHERE name='boss'")
        z = self.add("a", "z")
        core.queue_add(self.c, "s2", z, actor="boss")
        with self.assertRaisesRegex(RiverError, "refused"):
            core.queue_remove(self.c, "s2", str(z), actor="s2")  # an agent does not drop its queued items

    def test_a_gone_or_stopped_session_gives_its_queue_back(self):
        x, y = self.add("a", "x"), self.add("a", "y")
        core.queue_add(self.c, "s1", x, actor="mark")
        core.queue_add(self.c, "s2", y, actor="mark")
        self.c.execute("UPDATE agents SET role='manager' WHERE name='boss'")
        core.agent_note(self.c, "boss", "managing")
        self.c.execute("UPDATE agents SET last_seen=? WHERE name='s1'", (core.iso(core.now() - core.timedelta(days=3)),))
        with core.tx(self.c):
            core._sweep(self.c)
        self.assertIsNone(core.annotate(self.c)[x]["queued_for"])
        self.assertIn("s1 is gone", core.inbox(self.c, "boss")[0]["body"])
        core.unregister(self.c, "s2")
        self.assertIsNone(core.annotate(self.c)[y]["reserved_for"])


class Stop(Base):
    """maxpm stop: a request at the front of the agent's queue; the agent commits, releases, and ends (#427)."""

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "a", path=self.dir.name)
        for n in ("s1", "s2"):
            core.register(self.c, n)
        core.register(self.c, "mark", human=True)

    def test_a_stopped_agent_keeps_no_reservation(self):
        # #641: only its pushes were taken back; an item it released after the stop stayed reserved for it.
        x, pre, y = self.add("a", "x"), self.add("a", "a prerequisite"), self.add("a", "y")
        core.push(self.c, x, "s1", None, "mark")
        core.claim(self.c, x, "s1")
        self.c.execute("UPDATE items SET reserved_for='s1' WHERE id=?", (pre,))  # no time limit, as for a kept prerequisite
        core.stop_agent(self.c, "s1", "wrong model", "mark")
        self.assertIsNone(core._item(self.c, pre)["reserved_for"])
        self.assertIn("reservation for s1 ended: s1 was stopped", core.item_show(self.c, pre)["events"][0]["change"])
        core.release(self.c, x, "stopped", "s1")
        self.assertIsNone(core._item(self.c, x)["reserved_for"])
        # Reserved for it after the stop (given to it): the next command of anyone, and its last go, end that.
        self.c.execute("UPDATE items SET reserved_for='s1' WHERE id=?", (y,))
        core.activity(self.c, "s2")
        self.assertIsNone(core._item(self.c, y)["reserved_for"])
        self.c.execute("UPDATE items SET reserved_for='s1' WHERE id=?", (y,))
        self.assertTrue(core._finish_stop(self.c, "s1"))
        self.assertIsNone(core._item(self.c, y)["reserved_for"])
        core.claim(self.c, y, "s2")

    def test_a_waiting_agent_ends_at_once(self):
        core.goal_add(self.c, "a", "g")
        core.goal_own(self.c, "g", "s1")
        x = self.add("a", "x")
        core.queue_add(self.c, "s1", x, actor="mark")
        self.c.execute("UPDATE agents SET role='waiting' WHERE name='s1'")
        with self.assertRaisesRegex(RiverError, "may not stop another"):
            core.stop_agent(self.c, "s1", "no", actor="s2")
        r = core.stop_agent(self.c, "s1", "project paused", actor="mark")
        self.assertTrue(r["waiting"])
        self.assertEqual(core.queue_list(self.c, "s1")["entries"][0]["kind"], "stop")
        w = core.wait(self.c, self.dir.name, "s1", step="1m", sleep=lambda s: None)
        self.assertEqual((w["result"], w["ended"], w["stop"]["stop_reason"]), ("stop", True, "project paused"))
        self.assertIsNone(core.goal_show(self.c, "g")["owner"])
        self.assertIsNone(core.annotate(self.c)[x]["queued_for"])
        self.assertEqual(core.agent_status(self.c, "s1")["state"], "stopped")
        with self.assertRaisesRegex(RiverError, "asked to stop"):
            core.claim(self.c, x, "s1")

    def test_a_working_agent_releases_then_ends(self):
        x, y = self.add("a", "x"), self.add("a", "y")
        core.claim(self.c, x, "s1")
        r = core.stop_agent(self.c, "s1", "wrong approach", actor="mark")
        self.assertFalse(r["waiting"])
        self.assertIn("after its next maxpm command", r["ends"])
        b = core.go(self.c, self.dir.name, "s1")
        self.assertEqual((b["role"], b["ended"], [h["id"] for h in b["stop_holds"]]), ("stopped", False, [x]))
        self.assertEqual(core.item_show(self.c, x)["status"], "in_progress")  # no new claim, the item stays
        from river import cli
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.render_go(b)
        self.assertIn("STOP REQUESTED by mark: wrong approach", out.getvalue())
        self.assertIn('release <id> --note "<what is done', out.getvalue())
        core.release(self.c, x, "half done", "s1")
        b = core.go(self.c, self.dir.name, "s1")
        self.assertTrue(b["ended"])
        self.assertEqual(core.item_show(self.c, y)["status"], "open")
        # A person withdraws the stop: remove the stop entry.
        with self.assertRaisesRegex(RiverError, "refused"):
            core.queue_remove(self.c, "s1", "e%d" % core.queue_list(self.c, "s1")["entries"][0]["entry"], actor="s1")
        core.queue_remove(self.c, "s1", "e%d" % core.queue_list(self.c, "s1")["entries"][0]["entry"], actor="mark")
        self.assertIsNone(core.stop_request(self.c, "s1"))


class NativeDelivery(Base):
    """Queue instructions, stops, and messages also go through the agent platform's own messaging (#426)."""

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "a", path=self.dir.name)
        for n in ("cx", "cc", "plain"):
            core.register(self.c, n)
        core.register(self.c, "mark", human=True)
        self.sent = []
        core.NATIVE_RUNNER = lambda args: (self.sent.append(args), (0, ""))[1]
        self.addCleanup(setattr, core, "NATIVE_RUNNER", None)

    def test_the_platform_comes_from_the_environment(self):
        self.assertEqual(core.native_from_env(self.c, {"CODEX_THREAD_ID": "t-1"}), ("Codex", "t-1"))
        self.assertEqual(core.native_from_env(self.c, {"CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/x.sock"}), (None, None))
        core.config_set(self.c, "native_message", core.setting(self.c, "native_message")
                        + "; Claude Code=CLAUDE_CODE_MESSAGING_SOCKET: uds {address} {message}")
        self.assertEqual(core.native_from_env(self.c, {"CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/x.sock", "CLAUDECODE": "1"}),
                         ("Claude Code", "/tmp/x.sock"))
        self.assertEqual(core.native_from_env(self.c, {"PATH": "/bin"}), (None, None))
        with self.assertRaisesRegex(RiverError, "Label=ENV_VAR: command"):
            core.config_set(self.c, "native_message", "Mine=myagent send {message}")
        core.config_set(self.c, "native_message", "Mine=MINE_ID: mine send {address} {message}")
        self.assertEqual(core.native_from_env(self.c, {"MINE_ID": "7"}), ("Mine", "7"))

    def test_only_a_blocked_message_or_a_persons_goes_into_the_managers_session(self):
        # #1583, #1608: a message into the manager's session wakes it; its watch brings the others later.
        core.manage(self.c, self.dir.name, "cx")
        core.set_native(self.c, "cx", "Codex", "thread-9")
        core.manage_watch(self.c, "cx", step="0s", sleep=lambda s: None)
        m = core.send(self.c, "note", "#12 is on main", to="cx", actor="cc")
        self.assertEqual((self.sent, m["native_status"]),
                         ([], "waits: the manager's watch brings a high message after its wait"))
        # An alert without the flag waits too: its sender goes on with its item.
        self.assertEqual(core.send(self.c, "alert", "the build is red", to="cx", actor="cc")["native_status"],
                         "waits: the manager's watch brings a high message after its wait")
        self.assertEqual(self.sent, [])
        self.assertEqual(core.send(self.c, "question", "which db? I cannot go on", to="cx", actor="cc",
                                   blocked=True)["native_status"], "sent")
        self.assertEqual(len(self.sent), 1)
        core.register(self.c, "mark", human=True)
        self.assertEqual(core.send(self.c, "note", "stop the release", to="cx", actor="mark")["native_status"], "sent")
        # The watch does not return for what the platform brought; it returns for the others when their wait ends.
        t0, naps = core.now(), []
        with mock.patch.object(core, "now", side_effect=lambda: t0 + timedelta(minutes=len(naps))):
            w = core.manage_watch(self.c, "cx", step="2h", sleep=naps.append)
        self.assertEqual((w["result"], len(naps)), ("messages", 10))
        self.assertLessEqual({"#12 is on main", "the build is red"}, {x["body"] for x in w["messages"]})
        # A worker gets every message at once, as before.
        core.set_native(self.c, "cc", "Codex", "thread-3")
        self.assertEqual(core.send(self.c, "note", "fyi", to="cc", actor="cx", level="low")["native_status"], "sent")

    def test_instructions_stops_and_messages_go_out_natively(self):
        core.set_native(self.c, "cx", "Codex", "thread-9")
        core.queue_add(self.c, "cx", message="commit and take #3 next", actor="mark")
        self.assertEqual(self.sent[-1][:4], ["codex", "queue", "--thread", "thread-9"])
        self.assertIn("commit and take #3 next", self.sent[-1][-1])
        self.assertEqual(core.queue_list(self.c, "cx")["entries"][0]["native_status"], "sent")
        m = core.send(self.c, "alert", "the build is red", to="cx", actor="mark")
        self.assertEqual(m["native_status"], "sent")
        core.queue_add(self.c, "plain", message="hello", actor="mark")  # no platform: the queue path only
        self.assertEqual(core.queue_list(self.c, "plain")["entries"][0]["native_status"], "no native channel")
        core.NATIVE_RUNNER = lambda args: (1, "no such thread")
        x = self.add("a", "x")
        core.claim(self.c, x, "cx")
        r = core.stop_agent(self.c, "cx", "plan changed", actor="mark")
        self.assertEqual(r["native"], "failed: exit 1: no such thread")

    def test_a_claude_code_session_inbox_gets_one_line(self):
        import socket
        import threading
        if not hasattr(socket, "AF_UNIX"):
            self.skipTest("no Unix sockets on this system (Windows): river says so in place of a delivery")
        d = tempfile.mkdtemp(dir="/tmp")  # a short path: a socket path has a low length limit
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        path = os.path.join(d, "s.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)
        got = []

        def serve():
            conn, _ = srv.accept()
            got.append(conn.recv(4096).decode())
            conn.close()
        t = threading.Thread(target=serve)
        t.start()
        core.NATIVE_RUNNER = None
        core.config_set(self.c, "native_message", "Claude Code=CLAUDE_CODE_MESSAGING_SOCKET: uds {address} {message}")
        core.set_native(self.c, "cc", "Claude Code", path)
        core.queue_add(self.c, "cc", message="two\nlines", actor="mark")
        t.join(5)
        srv.close()
        self.assertEqual(got[0], "[maxpm instruction from mark] two lines (maxpm --as <you> inbox; maxpm go shows your queue)\n")
        self.assertEqual(core.queue_list(self.c, "cc")["entries"][0]["native_status"], "sent")
        core.set_native(self.c, "cc", "Claude Code", os.path.join(d, "gone.sock"))
        self.assertTrue(core.deliver_native(self.c, "cc", "x").startswith("failed: exit 1:"))


class Kill(Base):
    """maxpm stop --kill: emergency only; ends the agent's process on this host and releases what it held (#428)."""

    def setUp(self):
        super().setUp()
        import subprocess
        import sys
        core.project_add(self.c, "a", path=self.dir.name)
        for n in ("ag", "other"):
            core.register(self.c, n)
        core.register(self.c, "mark", human=True)
        self.p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(lambda: (self.p.poll() is None and self.p.kill(), self.p.wait()))
        info = core.proc_info(self.p.pid)
        if info is None and os.name != "nt":  # Windows reads its process list itself
            self.skipTest("no ps on this system: river records no process there, so --kill has none to end")
        cmd = info[2]
        self.assertIn("time.sleep(60)", cmd)
        core.set_process(self.c, "ag", self.p.pid, cmd)

    def test_kill_ends_the_process_and_releases_everything(self):
        x = self.add("a", "x")
        after = self.add("a", "after", after=[x])
        core.claim(self.c, x, "ag")
        core.goal_add(self.c, "a", "g")
        core.goal_own(self.c, "g", "ag")
        with self.assertRaisesRegex(RiverError, "a person or a manager kills"):
            core.kill_agent(self.c, "ag", "stuck", actor="other")
        r = core.kill_agent(self.c, "ag", "stuck in a loop", actor="mark", sleep=lambda s: None)
        self.p.wait(10)
        self.assertTrue(r["killed"])
        self.assertIsNotNone(self.p.returncode)
        self.assertEqual([h["id"] for h in r["released"]], [x])
        self.assertIn("Uncommitted work", r["warning"])
        it = core.item_show(self.c, x)
        self.assertEqual((it["status"], it["assignee"]), ("open", None))
        self.assertIsNone(core.goal_show(self.c, "g")["owner"])
        self.assertEqual(core.agent_status(self.c, "ag")["state"], "stopped")
        self.assertTrue(any("killed ag (by mark: stuck in a loop)" in e["change"] for e in core.recent_events(self.c)))
        self.assertNotEqual(after, x)

    def test_the_process_lookup_knows_the_parent_and_what_stands_between(self):
        self.assertEqual(core.proc_info(self.p.pid)[0], os.getpid())
        self.assertEqual(core.proc_info(os.getpid())[0], os.getppid())
        self.assertTrue(core.pid_alive(self.p.pid))
        self.assertEqual(core.agent_process(self.p.pid)[0], self.p.pid)  # not a shell: it is the agent
        self.assertTrue(core._between("-zsh", "-zsh"))
        self.assertFalse(core._between("claude", "claude go"))
        with mock.patch("os.name", "nt"):
            self.assertTrue(core._between("cmd.exe", "cmd /k claude go"))
            self.assertTrue(core._between("maxpm.exe", "maxpm go"))
            self.assertTrue(core._between("python.exe", r"C:\py\python.exe C:\tools\maximizepm\bin\maxpm go"))
            self.assertTrue(core._between("python.exe", "python.exe -m river go"))  # the package is river
            self.assertFalse(core._between("python.exe", r"D:\maximizepm\venv\python.exe agent.py"))
            self.assertFalse(core._between("node.exe", "node claude.js go"))
        self.p.kill()
        self.p.wait(10)
        self.assertFalse(core.pid_alive(self.p.pid))
        self.assertIsNone(core.proc_info(self.p.pid))

    def test_refusals_and_a_dead_process_counts_as_gone(self):
        core.set_process(self.c, "ag", self.p.pid, "something else")
        with self.assertRaisesRegex(RiverError, "now runs another command"):
            core.kill_agent(self.c, "ag", "stuck", actor="mark")
        self.assertIsNone(self.p.poll())  # not killed
        core.set_process(self.c, "ag", self.p.pid, "x", host="elsewhere")
        with self.assertRaisesRegex(RiverError, "runs on elsewhere"):
            core.kill_agent(self.c, "ag", "stuck", actor="mark")
        with self.assertRaisesRegex(RiverError, "no recorded process"):
            core.kill_agent(self.c, "other", "stuck", actor="mark")
        core.set_process(self.c, "ag", self.p.pid, "x")
        self.assertEqual(core.agent_status(self.c, "ag")["state"], "active")
        self.p.kill()
        self.p.wait(10)
        self.assertEqual(core.agent_status(self.c, "ag")["state"], "gone")  # at once, not after gone_after


class Manager(Base):
    """maxpm manage: one manager session at a time that runs the other agents (#430)."""

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "a", path=self.dir.name)
        core.project_add(self.c, "b", path=self.dir.name)
        for n in ("w1", "w2"):
            core.register(self.c, n)
        core.register(self.c, "mark", human=True)

    def test_manager_autocompact_takes_what_claude_takes(self):
        # claude --autocompact takes auto or 100k to 1M; 200 is shorthand for 200k (#1312).
        for v, n in (("200k", 200000), ("200000", 200000), ("200", 200000), ("1M", 1000000), ("auto", None)):
            self.assertEqual(core.autocompact_tokens(v), n)
            core.config_set(self.c, "manager_autocompact", v)
        for v in ("50k", "2m", "lots", ""):
            with self.assertRaisesRegex(RiverError, "100k to 1M"):
                core.config_set(self.c, "manager_autocompact", v)

    def test_one_manager_at_a_time_and_it_takes_no_work(self):
        m = core.manage(self.c, self.dir.name, "boss")
        self.assertEqual((m["role"], core._agent(self.c, "boss")["role"]), ("manager", "manager"))
        x = self.add("a", "x")
        with self.assertRaisesRegex(RiverError, "manager session"):
            core.claim(self.c, x, "boss")
        with self.assertRaisesRegex(RiverError, "boss is the active manager"):
            core.manage(self.c, self.dir.name, "boss2")
        m2 = core.manage(self.c, self.dir.name, "boss2", takeover="boss went quiet")
        self.assertEqual(m2["took_over"], "boss")
        self.assertIsNone(core._agent(self.c, "boss")["role"])
        self.assertIn("took over as manager", core.inbox(self.c, "boss")[0]["body"])

    def test_findings_and_actions(self):
        x, y = self.add("a", "x"), self.add("b", "y")
        z = self.add("a", "z")
        core.claim(self.c, x, "w1")
        old = core.iso(core.now() - core.timedelta(hours=2))
        self.c.execute("UPDATE agents SET last_seen=? WHERE name='w1'", (old,))
        self.c.execute("UPDATE agents SET role='waiting', waiting_in='a', waiting_since=? WHERE name='w2'",
                       (core.iso(core.now() - core.timedelta(minutes=40)),))
        core.target_add(self.c, "web")
        core.target_own(self.c, "web", "w1")
        self.c.execute("UPDATE agents SET last_seen=? WHERE name='w1'", (old,))
        core.send(self.c, "question", "which database?", to="mark", actor="w2")
        m = core.manage(self.c, self.dir.name, "boss")
        f = m["findings"]
        self.assertEqual([(s["agent"], s["holds"]) for s in f["stuck"]], [("w1", [x])])
        self.assertEqual([w["agent"] for w in f["waiting_too_long"]], ["w2"])
        self.assertEqual([u["project"] for u in f["uncovered"]], ["b"])  # a has w2 waiting
        self.assertEqual([t["target"] for t in f["targets"]], ["web"])
        self.assertEqual(f["questions"][0]["body"], "which database?")
        # The manager acts: queue, give a target, stop; the history says so.
        core.queue_add(self.c, "w2", z, actor="boss")
        core.target_give(self.c, "web", "w2", actor="boss")
        core.stop_agent(self.c, "w1", "stuck", actor="boss")
        changes = [e["change"] for e in core.recent_events(self.c)]
        self.assertTrue(any(c.startswith("stopped w1") and c.endswith("(by manager boss)") for c in changes))
        self.assertTrue(any("target web given to w2" in c and "(by manager boss)" in c for c in changes))
        self.assertNotEqual(y, z)

    def test_watch_reports_what_is_new(self):
        core.manage(self.c, self.dir.name, "boss")
        w = core.manage_watch(self.c, "boss", step="0s", sleep=lambda s: None)
        self.assertEqual((w["result"], w["new"]), ("tick", []))
        self.add("b", "new work")
        core.config_set(self.c, "manage_settle", "0s")
        w = core.manage_watch(self.c, "boss", step="1m", sleep=lambda s: None)
        self.assertEqual((w["result"], w["new"]), ("change", ["uncovered:b"]))
        core.config_unset(self.c, "manage_settle")
        # A finding it reported before does not wake it again: it waits the whole manage_every.
        self.assertEqual(core.DEFAULT_SETTINGS["manage_every"], "30m")
        t0, naps = core.now(), []
        with mock.patch.object(core, "now", side_effect=lambda: t0 + timedelta(minutes=len(naps) * 10)):
            w = core.manage_watch(self.c, "boss", sleep=naps.append)
        self.assertEqual((w["result"], w["new"], len(naps)), ("tick", [], 3))  # 30m, not the 9m wait_step
        with self.assertRaisesRegex(RiverError, "not the manager"):
            core.manage_watch(self.c, "w1", step="0s")

    def test_a_group_of_findings_wakes_the_watch_once(self):
        # After the first new finding the watch waits manage_settle (2m), then returns the whole group (#1312).
        core.manage(self.c, self.dir.name, "boss")
        self.assertEqual(core.DEFAULT_SETTINGS["manage_settle"], "2m")
        t0, naps = core.now(), []

        def nap(s):
            naps.append(s)
            if len(naps) == 1:
                self.add("a", "first")
            elif len(naps) == 2:
                self.add("b", "second")
        with mock.patch.object(core, "now", side_effect=lambda: t0 + timedelta(minutes=len(naps))):
            w = core.manage_watch(self.c, "boss", sleep=nap)
        self.assertEqual((w["result"], w["new"], len(naps)), ("change", ["uncovered:a", "uncovered:b"], 3))
        # A finding that goes away before manage_settle wakes nothing.
        ids = []

        def come_and_go(s):
            naps.append(s)
            if not ids:
                ids.append(self.add("a", "brief"))
            elif len(ids) == 1:
                core.drop(self.c, ids[0], note="not needed", actor="mark")
                ids.append(None)
        t0, naps = core.now(), []
        with mock.patch.object(core, "now", side_effect=lambda: t0 + timedelta(minutes=len(naps))):
            w = core.manage_watch(self.c, "boss", step="5m", sleep=come_and_go)
        self.assertEqual((w["result"], w["new"]), ("tick", []))
        # A blocked message does not wait for the settle time.
        self.add("b", "third")
        core.send(self.c, "alert", "now", to="boss", actor="w1", blocked=True)
        w = core.manage_watch(self.c, "boss", step="1h", sleep=lambda s: self.fail("no settle wait for a message"))
        self.assertEqual(w["result"], "messages")
        with self.assertRaisesRegex(RiverError, "bad duration"):
            core.config_set(self.c, "manage_settle", "soon")

    def test_messages_wake_the_watch_so_one_watcher_is_enough(self):
        core.manage(self.c, self.dir.name, "boss")
        core.manage_watch(self.c, "boss", step="0s", sleep=lambda s: None)
        core.send(self.c, "alert", "hello boss", to="boss", actor="w1", blocked=True)
        w = core.manage_watch(self.c, "boss", step="1h", sleep=lambda s: self.fail("no wait with a message unread"))
        self.assertEqual((w["result"], [m["body"] for m in w["messages"]]), ("messages", ["hello boss"]))
        self.assertEqual(core.unread(self.c, "boss")["unread"], 0)  # marked read: the next watch blocks
        # A message that comes while it watches ends the watch; an open question already read does not wake it.
        w = core.manage_watch(self.c, "boss", step="1h", sleep=lambda s: core.send(
            self.c, "question", "which db?", to="boss", actor="w1", blocked=True))
        self.assertEqual(([m["body"] for m in w["messages"]], w["still_open"]), (["which db?"], 0))
        w = core.manage_watch(self.c, "boss", step="0s", sleep=lambda s: None)
        self.assertEqual((w["result"], w["messages"]), ("tick", []))
        # The text says it is the only watcher, and shows the message itself.
        from river import cli
        core.send(self.c, "alert", "w2 is stuck", to="boss", actor="w1", blocked=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.render_manage(core.manage_watch(self.c, "boss", step="1h", sleep=lambda s: None))
            cli.render_manage(core.manage(self.c, self.dir.name, "boss"))
        self.assertIn("NEW MESSAGES (1)", out.getvalue())
        self.assertIn("w2 is stuck", out.getvalue())
        self.assertIn("a message to you (a blocked one or a person's at once, another after manage_wait_high 10m, a low "
                      "one after manage_wait_low 30m; it prints every message that waits)", out.getvalue())
        self.assertNotIn("inbox --wait", out.getvalue())
        # With native delivery the platform brings messages: they do not wake the watch.
        with mock.patch.object(core, "has_native", return_value=True):
            core.send(self.c, "note", "native", to="boss", actor="w1")
            w = core.manage_watch(self.c, "boss", step="0s", sleep=lambda s: None)
        self.assertEqual((w["result"], w["messages"], w["unread"]), ("tick", [], 1))
        core.inbox(self.c, "boss")
        # A stop request ends the watch too.
        core.stop_agent(self.c, "boss", "done for the day", actor="mark")
        w = core.manage_watch(self.c, "boss", step="1h", sleep=lambda s: self.fail("no wait after a stop"))
        self.assertEqual(w["result"], "stop")

    def watch_minutes(self, **kw):
        """Run the watch with a clock that moves one minute for each poll; returns (result, minutes waited)."""
        t0, naps = core.now(), []
        with mock.patch.object(core, "now", side_effect=lambda: t0 + timedelta(minutes=len(naps))):
            w = core.manage_watch(self.c, "boss", sleep=naps.append, **kw)
        return w, len(naps)

    def test_each_level_of_message_has_its_wait_and_a_wake_brings_all_that_waits(self):
        # #1583: on 2026-10-08, 124 of 181 wakes of the manager were messages, most of them one note.
        core.manage(self.c, self.dir.name, "boss")
        core.manage_watch(self.c, "boss", step="0s", sleep=lambda s: None)
        self.assertEqual((core.MESSAGE_LEVELS, core.DEFAULT_SETTINGS["manage_wait_high"],
                          core.DEFAULT_SETTINGS["manage_wait_low"]), (("high", "low"), "10m", "30m"))
        # A note is high: it wakes the watch ten minutes after it was sent.
        m = core.send(self.c, "note", "#12 is on main", to="boss", actor="w1")
        self.assertEqual((m["level"], m["level_set"], m["blocked"], m["manager_wait"]), ("high", False, None, "10m"))
        w, minutes = self.watch_minutes(step="2h")
        self.assertEqual((w["result"], [x["body"] for x in w["messages"]], minutes), ("messages", ["#12 is on main"], 10))
        # A low message waits thirty minutes; the time limit of the watch (manage_every) brings it sooner.
        low = core.send(self.c, "note", "for your records", to="boss", actor="w1", level="low")
        self.assertEqual((low["level"], low["level_set"], low["manager_wait"]), ("low", True, "30m"))
        w, minutes = self.watch_minutes(step="2h")
        self.assertEqual((w["result"], minutes), ("messages", 30))
        core.send(self.c, "note", "again for your records", to="boss", actor="w1", level="low")
        w, minutes = self.watch_minutes(step="20m")
        self.assertEqual((w["result"], [x["body"] for x in w["messages"]], minutes),
                         ("messages", ["again for your records"], 20))
        # The level high is the default and is not stored; there is no third level.
        self.assertEqual(core.send(self.c, "alert", "x", to="w2", actor="w1", level="high")["level_set"], False)
        for old in ("urgent", "normal", "info"):
            with self.assertRaisesRegex(RiverError, "the level is one of high, low"):
                core.send(self.c, "note", "x", to="boss", actor="w1", level=old)
        # The waits are settings; 0s is at once.
        core.inbox(self.c, "boss")
        core.config_set(self.c, "manage_wait_high", "0s")
        core.send(self.c, "note", "three", to="boss", actor="w1")
        self.assertEqual(self.watch_minutes(step="2h")[1], 0)
        with self.assertRaisesRegex(RiverError, "bad duration"):
            core.config_set(self.c, "manage_wait_low", "later")
        with self.assertRaisesRegex(RiverError, "unknown setting 'manage_wait_normal'"):
            core.config_set(self.c, "manage_wait_normal", "5m")

    def test_an_alert_or_a_question_waits_unless_it_is_blocked(self):
        # #1608: after 78 of 128 alerts and questions to the manager, the sender went on with its item (#1597).
        core.manage(self.c, self.dir.name, "boss")
        core.manage_watch(self.c, "boss", step="0s", sleep=lambda s: None)
        a = core.send(self.c, "alert", "main is red since #12", to="boss", actor="w1")
        q = core.send(self.c, "question", "may I also rename the table?", to="boss", actor="w1")
        self.assertEqual([(m["level"], m["blocked"], m["manager_wait"]) for m in (a, q)], [("high", None, "10m")] * 2)
        w, minutes = self.watch_minutes(step="2h")
        self.assertEqual(([x["body"] for x in w["messages"]], minutes),
                         (["main is red since #12", "may I also rename the table?"], 10))
        # With the flag it wakes the watch at once, and brings the ones that wait, itself first: no second wake.
        core.send(self.c, "note", "one", to="boss", actor="w1")
        core.send(self.c, "note", "two", to="boss", actor="w2", level="low")
        b = core.send(self.c, "question", "which db? I cannot go on", to="boss", actor="w1", blocked=True)
        self.assertEqual((b["blocked"], "manager_wait" in b), ("sender", False))
        w, minutes = self.watch_minutes(step="2h")
        self.assertEqual(([x["body"] for x in w["messages"]], minutes), (["which db? I cannot go on", "one", "two"], 0))
        self.assertEqual(self.watch_minutes(step="5m")[0]["result"], "tick")
        # A low message with the flag does not wait either; the shortcut commands pass the flag.
        up = core.message(self.c, "note", "the disk is full", to="boss", actor="w1", level="low", blocked=True)[0]
        self.assertEqual((up["level"], up["blocked"]), ("low", "sender"))
        self.assertEqual(self.watch_minutes(step="2h")[1], 0)
        # A person's message always wakes the manager at once.
        core.register(self.c, "mark", human=True)
        p = core.send(self.c, "note", "stop the release", to="boss", actor="mark")
        self.assertEqual((p["blocked"], "manager_wait" in p), (None, False))
        self.assertEqual(self.watch_minutes(step="2h")[1], 0)
        # To a worker the flag is stored, and nothing waits.
        self.assertEqual("manager_wait" in core.send(self.c, "alert", "x", to="w2", actor="w1"), False)

    def test_maxpm_marks_a_message_blocked_when_it_sees_the_sender_stand_still(self):
        # #1597: one of the two signs showed 39 of the 50 past messages whose item stood still.
        core.manage(self.c, self.dir.name, "boss")
        core.manage_watch(self.c, "boss", step="0s", sleep=lambda s: None)
        blocked = lambda m: core.message_show(self.c, m["id"])["blocked"]
        # Sign 1: the sender asks, then waits for messages. The watch returns at once.
        q = core.send(self.c, "question", "which db?", to="boss", actor="w1")
        n = core.send(self.c, "note", "#12 is on main", to="boss", actor="w1")
        other = core.send(self.c, "alert", "main is red", to="boss", actor="w2")
        core.inbox_wait(self.c, "w1", timeout="0s", sleep=lambda s: None)
        self.assertEqual([blocked(m) for m in (q, n, other)], ["waits", None, None])  # not a note, not another sender
        w, minutes = self.watch_minutes(step="2h")
        self.assertEqual((w["messages"][0]["body"], minutes), ("which db?", 0))
        # Not a message that is older than three minutes, and not one that the manager read.
        old = core.send(self.c, "alert", "old news", to="boss", actor="w1")
        read = core.send(self.c, "alert", "read", to="boss", actor="w1")
        self.c.execute("UPDATE messages SET created_at=? WHERE id=?", (core.iso(core.now() - timedelta(minutes=4)), old["id"]))
        self.c.execute("UPDATE messages SET read_at=? WHERE id=?", (core.iso(core.now()), read["id"]))
        core.inbox_wait(self.c, "w1", timeout="0s", sleep=lambda s: None)
        self.assertEqual([blocked(m) for m in (old, read)], [None, None])
        core.inbox(self.c, "boss")
        # Sign 2: the sender releases or blocks the item of the message, after the send or just before it.
        x, y, z = self.add("a", "page"), self.add("a", "form"), self.add("a", "mail")
        core.config_set(self.c, "max_leases", "3", agent="w1")
        for i in (x, y, z):
            core.claim(self.c, i, "w1")
        about_x = core.send(self.c, "alert", "the review needs another agent", to="boss", item=x, actor="w1")
        about_y = core.send(self.c, "alert", "y goes on", to="boss", item=y, actor="w1")
        core.release(self.c, x, "I wrote a commit of this release", "w1")
        self.assertEqual([blocked(about_x), blocked(about_y)], ["item", None])
        self.assertEqual(self.watch_minutes(step="2h")[1], 0)  # it brought the alert about y too: read, so not raised
        about_y = core.send(self.c, "alert", "y needs the vendor's key", to="boss", item=y, actor="w1")
        core.block(self.c, y, "waits for the vendor's key", "w1")
        self.assertEqual(blocked(about_y), "item")
        after = core.send(self.c, "question", "who has the vendor's key?", to="boss", item=y, actor="w1")
        self.assertEqual((after["blocked"], "manager_wait" in after), ("item", False))
        # With no item on the message: an item that the sender released in the last three minutes.
        self.assertEqual(core.send(self.c, "alert", "disk is full", to="boss", actor="w1")["blocked"], "item")
        self.assertIsNone(core.send(self.c, "alert", "disk is full", to="boss", actor="w2")["blocked"])
        self.assertIsNone(core.send(self.c, "alert", "z goes on", to="boss", item=z, actor="w1")["blocked"])
        # A release by someone else (the manager takes the item back) is no sign of the holder.
        core.release(self.c, z, "to another agent", None)
        self.assertIsNone(blocked(core.send(self.c, "note", "fyi", to="boss", actor="w2")))
        # The manager's own messages are never raised.
        to_worker = core.send(self.c, "question", "is #9 done?", to="w1", actor="boss")
        self.assertIsNone(to_worker["blocked"])

    def test_the_three_levels_of_1583_become_two_levels_and_the_flag(self):
        core.manage(self.c, self.dir.name, "boss")
        ids = [core.send(self.c, "note", t, to="boss", actor="w1")["id"] for t in ("now", "later", "records")]
        for i, lvl in zip(ids, ("urgent", "normal", "low")):
            self.c.execute("UPDATE messages SET level=? WHERE id=?", (lvl, i))
        self.c.execute("INSERT INTO settings(scope, key, value) VALUES ('global', 'manage_wait_normal', '7m')")
        self.c.execute("DELETE FROM meta WHERE key='message_levels_1608'")
        self.c.commit()
        c2 = core.connect(self.path)
        self.addCleanup(c2.close)
        self.assertEqual([(m["level"], m["level_set"], m["blocked"]) for m in map(lambda i: core.message_show(c2, i), ids)],
                         [("high", False, "sender"), ("high", False, None), ("low", True, None)])
        self.assertEqual(core.setting(c2, "manage_wait_high"), "7m")
        self.assertEqual(c2.execute("SELECT count(*) FROM settings WHERE key='manage_wait_normal'").fetchone()[0], 0)
        # One time only: a later level is not touched.
        self.c.execute("UPDATE messages SET level='low' WHERE id=?", (ids[0],))
        self.c.commit()
        c3 = core.connect(self.path)
        self.addCleanup(c3.close)
        self.assertEqual(core.message_show(c3, ids[0])["level"], "low")

    def test_a_ship_request_notice_is_low_and_the_commands_take_a_level_and_the_flag(self):
        from river import cli
        core.manage(self.c, self.dir.name, "boss")
        core.target_add(self.c, "web", "push")
        core.project_target(self.c, "a", "web")
        core.target_give(self.c, "web", "boss", "mark")
        core.inbox(self.c, "boss")
        x = self.add("a", "page")
        core.ship(self.c, x, "w1")
        got = [m for m in core.inbox(self.c, "boss", mark_read=False) if "ship request" in m["body"]]
        self.assertEqual([(m["kind"], m["level"]) for m in got], [("notice", "low")])
        core.manage(self.c, self.dir.name, "boss")  # the briefing shows the findings of now; the watch starts after it
        w, minutes = self.watch_minutes(step="2h")
        self.assertEqual((w["result"], minutes), ("messages", 30))

        def run(*words):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                    mock.patch.dict(os.environ, {"MAXPM_DB": self.path, "MAXPM_QUIET": "1"}):
                cli.run(list(words))
            return out.getvalue()
        self.assertRegex(run("--as", "w1", "note", "boss", "no action needed", "--level", "low"), r"^sent #\d+ note to boss\n")
        # An alert or a question to the manager says how long it waits, and how to make it not wait.
        self.assertRegex(run("--as", "w1", "alert", "boss", "main is red"),
                         r"^sent #\d+ alert to boss \(the manager's watch brings it within 10m; with --blocked at once: "
                         r"use it when your item cannot move until the answer\)\n")
        self.assertRegex(run("--as", "w1", "ask", "boss", "which db? I cannot go on", "--blocked"),
                         r"^sent #\d+ question to boss, blocked\n")
        self.assertRegex(run("--as", "w1", "send", "note", "read this now", "--to", "boss", "--blocked"), r"to boss, blocked\n")
        self.assertRegex(run("--as", "w1", "alert", "w2", "to a worker"), r"^sent #\d+ alert to w2\n")
        out = run("--as", "boss", "inbox", "--peek")
        self.assertRegex(out, r"note from w1 to boss  \(.*, level low, new\)\n    no action needed")
        self.assertRegex(out, r"alert from w1 to boss  \([^,]*, new\)\n    main is red")
        self.assertRegex(out, r"question from w1 to boss  \(.*, open, blocked, new\)\n    which db\?")
        self.assertRegex(out, r"note from w1 to boss  \([^,]*, blocked, new\)\n    read this now")
        # A level of #1583 is refused with the way to say it now; MaximizePM shows what it saw.
        with self.assertRaisesRegex(RiverError, "the level is one of high, low; for a message that cannot wait, "
                                                "leave it out and add --blocked"):
            run("--as", "w1", "note", "boss", "x", "--level", "urgent")
        run("--as", "w1", "ask", "boss", "may I rename it?")
        core.inbox_wait(self.c, "w1", timeout="0s", sleep=lambda s: None)
        self.assertRegex(run("--as", "boss", "inbox", "--peek"),
                         r"open, blocked \(the sender waits for messages\), new\)\n    may I rename it\?")

    def test_a_low_finding_waits_and_another_finding_brings_it(self):
        core.manage(self.c, self.dir.name, "boss")
        core.manage_watch(self.c, "boss", step="0s", sleep=lambda s: None)
        h = self.add("a", "sign the contract", doer="human")  # an item that is ready for a person: low
        self.assertEqual(core._finding_keys(core.manager_findings(self.c)), [f"human:{h}"])
        w, minutes = self.watch_minutes(step="2h")
        self.assertEqual((w["result"], w["new"], minutes), ("change", [f"human:{h}"], 30))
        # With another finding (ready agent work and no agent) it comes after manage_settle, with the low one.
        h2 = self.add("a", "pay the invoice", doer="human")
        self.add("b", "new work")
        w, minutes = self.watch_minutes(step="2h")
        self.assertEqual((w["result"], w["new"], minutes), ("change", [f"human:{h2}", "uncovered:b"], 2))
        # A low finding that goes away wakes nothing.
        h3 = self.add("a", "call the bank", doer="human")
        t0, naps = core.now(), []

        def nap(s):
            naps.append(s)
            if len(naps) == 5:
                core.drop(self.c, h3, note="not needed", actor="mark")
        with mock.patch.object(core, "now", side_effect=lambda: t0 + timedelta(minutes=len(naps))):
            w = core.manage_watch(self.c, "boss", step="45m", sleep=nap)
        self.assertEqual((w["result"], w["new"], len(naps)), ("tick", [], 45))

    def test_inbox_wait_works_for_any_agent_but_the_active_manager(self):
        core.manage(self.c, self.dir.name, "boss")
        # The active manager has one watcher, manage --watch: a second one doubled its wakes (#1312).
        with self.assertRaisesRegex(RiverError, "only watcher.*boss manage --watch"):
            core.inbox_wait(self.c, "boss", "1m")
        # An open question already read does not wake it again; a timeout returns nothing.
        core.send(self.c, "question", "which db?", to="w1", actor="w2")
        core.inbox_wait(self.c, "w1", "1m")
        naps = []
        r = core.inbox_wait(self.c, "w1", "0s", sleep=naps.append)
        self.assertEqual((r["result"], r["messages"], naps), ("timeout", [], []))
        # A message that comes while it waits ends the wait.
        r = core.inbox_wait(self.c, "w1", "1h", sleep=lambda s: core.send(self.c, "alert", "now", to="w1", actor="w2"))
        self.assertEqual([m["body"] for m in r["messages"]], ["now"])
        core.stop_agent(self.c, "w1", "done for today", actor="mark")
        self.assertEqual(core.inbox_wait(self.c, "w1", "1h")["result"], "stop")
        # A manager that was taken over is no longer the active one: it may wait, and reads the alert.
        self.assertFalse(core.manage(self.c, self.dir.name, "boss2", takeover="x")["native"])
        self.assertIn("took over as manager", core.inbox_wait(self.c, "boss", "0s")["messages"][0]["body"])


class BusyLease(Base):
    """A lease does not run out while a command runs in the agent's session, and no second agent takes an item
    whose first agent still works on it."""

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "shop")
        for n, name in enumerate(("w1", "w2")):
            core.register(self.c, name)
            self.c.execute("UPDATE agents SET pid=?, host=? WHERE name=?", (1000 + n, core.this_host(), name))
        core.register(self.c, "mark", human=True)
        self.clock = [core.now()]
        fake_now = mock.patch.object(core, "now", lambda: self.clock[0])
        fake_now.start()
        self.addCleanup(fake_now.stop)
        # ps: each agent CLI (two hours old) with a server it started at its start; then what a test adds.
        self.procs = {1000: (1, "2:00:00"), 1001: (1, "2:00:00"), 2000: (1000, "1:59:58")}
        core.PROC_RUNNER = lambda: "".join(f"{pid:>6} {ppid:>6} {age:>12}\n" for pid, (ppid, age) in self.procs.items())
        self.addCleanup(setattr, core, "PROC_RUNNER", None)

    def later(self, **kw):
        self.clock[0] += timedelta(**kw)

    def status(self, x):
        it = core.item_show(self.c, x)
        return it["status"], it["assignee"]

    def test_a_long_command_keeps_the_lease(self):
        self.assertEqual([core._elapsed(x) for x in ("05", "01:05", "1:01:05", "2-01:01:05")], [5, 65, 3665, 176465])
        x = self.add("shop", "run every check")
        core.activity(self.c, "w1")
        core.claim(self.c, x, "w1")
        self.assertEqual(core.busy_now(self.c), set())  # only the server it started with: older than its last command
        # The agent starts a test run that takes 40 minutes, and runs no river command. Another agent's command
        # finds the lease past its time (30m): the test run is a process below the agent CLI, so river renews it.
        self.later(minutes=31)
        self.procs.update({3000: (1000, "30:50"), 3001: (3000, "30:49")})  # a shell and the test run below it
        self.assertEqual(core.busy_now(self.c), {"w1"})
        core.activity(self.c, "w2")
        self.assertEqual(self.status(x), ("in_progress", "w1"))
        self.assertIn("lease renewed: a command runs in the session of w1", [e["change"] for e in core.item_show(self.c, x)["events"]])
        self.assertEqual(core.next_item(self.c, "shop", claim=True, actor="w2"), [])
        # The test run ends and the agent stays silent: the lease runs out as before.
        del self.procs[3000], self.procs[3001]
        self.later(minutes=31)
        core.activity(self.c, "w2")
        self.assertEqual((self.status(x), core.item_show(self.c, x)["needs_check"]), (("open", None), 1))
        # Its session works again (it never ran a river command, so it does not know): no second agent takes the
        # item, by go or by claim; the first agent claims it again.
        self.procs[3002] = (1000, "00:20")
        skipped = []
        self.assertEqual(core.next_item(self.c, "shop", claim=True, actor="w2", skipped=skipped), [])
        self.assertEqual(skipped, [{"id": x, "title": "run every check", "why": "w1 still works on it in its session"}])
        with self.assertRaisesRegex(RiverError, f"the lease on #{x} ran out, but w1 still works on it .* maxpm stop w1"):
            core.claim(self.c, x, "w2")
        # A person or a manager stops the first session: then the item is free.
        core.stop_agent(self.c, "w1", "w2 takes it", actor="mark")
        self.assertEqual(core.claim(self.c, x, "w2")["assignee"], "w2")

    def test_the_first_agent_claims_again_and_the_signs_have_a_limit(self):
        x = self.add("shop", "merge the branch")
        core.activity(self.c, "w1")
        core.claim(self.c, x, "w1")
        self.later(minutes=31)
        core.activity(self.c, "w2")
        self.assertEqual(self.status(x), ("open", None))
        # maxpm serve saw the first session busy after the lease ran out (its tmux pane changed): the item waits
        # for it, for lease_ttl after the last such sign.
        self.later(minutes=1)
        self.assertEqual(core.keep_busy(self.c, {"w1", "nobody"}), ["w1"])
        self.assertEqual(core.still_worked(self.c), {x: "w1"})
        self.assertEqual(core.next_item(self.c, "shop", claim=True, actor="w2"), [])
        self.assertEqual(core.next_item(self.c, "shop", claim=True, actor="w1")[0]["assignee"], "w1")
        self.assertEqual(core.still_worked(self.c), {})
        # keep_busy renews what the agent holds, and leaves the time of its last river command.
        seen = self.c.execute("SELECT last_seen FROM agents WHERE name='w1'").fetchone()[0]
        self.later(minutes=20)
        core.keep_busy(self.c, {"w1"})
        self.later(minutes=20)
        core.activity(self.c, "w2")
        self.assertEqual((self.status(x), self.c.execute("SELECT last_seen FROM agents WHERE name='w1'").fetchone()[0]),
                         (("in_progress", "w1"), seen))
        # busy_max (4h) after its last river command, a command that still runs (a server it left) counts no more.
        self.procs[3000] = (1000, "10:00")
        self.assertEqual(core.busy_now(self.c), {"w1"})
        self.later(hours=4)
        self.assertEqual((core.busy_now(self.c), core.keep_busy(self.c, {"w1"})), (set(), []))
        core.activity(self.c, "w2")
        self.assertEqual(self.status(x), ("open", None))
        self.assertEqual(core.next_item(self.c, "shop", claim=True, actor="w2")[0]["assignee"], "w2")
        with self.assertRaisesRegex(RiverError, "bad duration"):
            core.config_set(self.c, "busy_max", "long")
        # An agent on another computer, or with no known process, or whose CLI ended: no sign.
        core.activity(self.c, "w1")
        self.procs[3001] = (1000, "00:00")
        self.later(seconds=30)
        self.assertEqual(core.busy_now(self.c), {"w1"})
        self.c.execute("UPDATE agents SET host='elsewhere' WHERE name='w1'")
        self.assertEqual(core.busy_now(self.c), set())
        self.c.execute("UPDATE agents SET host=? WHERE name='w1'", (core.this_host(),))
        del self.procs[1000]
        self.assertEqual(core.busy_now(self.c), set())
        core.PROC_RUNNER = lambda: (_ for _ in ()).throw(OSError("no ps"))
        self.assertEqual(core.busy_now(self.c), set())


class Rename(Base):
    """maxpm project rename and maxpm target rename: everything follows, and the old name stops at once."""

    def setUp(self):
        super().setUp()
        self.folder = os.path.realpath(self.dir.name)
        core.target_add(self.c, "site", "push to main")
        core.project_add(self.c, "old", notes="the tool", path=self.folder, target="site")
        core.project_add(self.c, "other")
        core.project_tracker(self.c, "old", "github o/r via gh")
        core.goal_add(self.c, "old", "launch", "it is live", "the page answers")
        core.review_step_add(self.c, "old", "read the diff")
        self.item = self.add("old", "write it")
        core.register(self.c, "w1")
        self.c.execute("UPDATE agents SET role='waiting', waiting_in='other,old' WHERE name='w1'")

    def test_everything_of_the_project_follows_the_new_name(self):
        rank = core._project(self.c, "old")["rank"]
        res = core.project_rename(self.c, "old", "new", "t")
        self.assertEqual((res["name"], res["was"], res["items"], res["goals"], res["settings"], res["waiting_agents"]),
                         ("new", "old", 1, 1, 1, 1))
        p = core.project_show(self.c, "new")
        self.assertEqual((p["rank"], p["path"], p["target"], p["tracker"], p["description"]),
                         (rank, self.folder, "site", "github o/r via gh", "the tool"))
        self.assertEqual([i["id"] for i in core.item_list(self.c, "new")], [self.item])
        self.assertEqual([g["name"] for g in core.goal_list(self.c, "new")], ["launch"])
        self.assertEqual([s["project"] for s in core.review_steps(self.c, "new")], ["new"])
        self.assertEqual(core.projects_for_dir(self.c, self.folder), ["new"])
        self.assertEqual(core.item_show(self.c, self.item)["project"], "new")
        self.assertEqual(self.c.execute("SELECT scope FROM settings WHERE key='tracker'").fetchone()[0], "project:new")
        self.assertEqual(self.c.execute("SELECT waiting_in FROM agents WHERE name='w1'").fetchone()[0], "other,new")
        self.assertEqual(core.waiting_agent_for(self.c, "new"), "w1")
        self.assertIn("project old renamed to new", [e["change"].split(";")[0] for e in core.recent_events(self.c)])
        # A new agent in the folder gets the new name as its prefix; an agent with the old prefix keeps its name.
        core.register(self.c, "old-1a2b")
        self.assertTrue(core.go(self.c, self.folder)["agent"].startswith("new-"))
        self.assertEqual(core.go(self.c, self.folder, "old-1a2b")["agent"], "old-1a2b")

    def test_the_old_name_is_unknown_at_once(self):
        res = core.project_rename(self.c, "old", "new", "t")
        self.assertNotIn("alias_until", res)
        self.assertNotIn("aliases", core.project_show(self.c, "new"))
        for call in (lambda: core.project_show(self.c, "old"), lambda: core.item_list(self.c, "old"),
                     lambda: core.item_add(self.c, "old", "more", actor="t")):
            with self.assertRaisesRegex(RiverError, "no project 'old'"):
                call()
        # The old name is free at once, for a new project or for a rename.
        self.assertEqual(core.project_add(self.c, "old")["name"], "old")
        self.assertEqual(core.item_list(self.c, "old"), [])

    def test_a_rename_needs_a_free_name_and_can_go_back(self):
        for new, why in (("other", "exists"), ("New Name", "lower-case"), ("old", "has that name already")):
            with self.assertRaisesRegex(RiverError, why):
                core.project_rename(self.c, "old", new)
        with self.assertRaisesRegex(RiverError, "no project 'gone'"):
            core.project_rename(self.c, "gone", "new")
        core.project_rename(self.c, "old", "new")
        core.project_rename(self.c, "new", "old")
        self.assertEqual([i["id"] for i in core.item_list(self.c, "old")], [self.item])
        with self.assertRaisesRegex(RiverError, "no project 'new'"):
            core.item_list(self.c, "new")

    def test_an_old_queue_loses_its_aliases_table(self):
        self.c.execute("CREATE TABLE aliases (kind TEXT NOT NULL, alias TEXT NOT NULL, ref_id INTEGER NOT NULL, "
                       "created_at TEXT NOT NULL, until TEXT, PRIMARY KEY (kind, alias))")
        self.c.execute("INSERT INTO aliases VALUES ('project', 'older', 1, '2026-10-01T00:00:00Z', '2027-01-01T00:00:00Z')")
        self.c.commit()
        self.c.close()
        self.c = core.connect(self.path)
        self.assertIsNone(self.c.execute("SELECT 1 FROM sqlite_master WHERE name='aliases'").fetchone())
        self.assertEqual(core.project_show(self.c, "old")["name"], "old")

    def test_a_target_takes_its_projects_its_items_and_its_deploy_project(self):
        dep = core.ship(self.c, self.item, "t")
        core.config_set(self.c, "review", "on", project="deploy-site")
        self.assertEqual((dep["project"], dep["title"], dep["target"]), ("deploy-site", "Deploy site", "site"))
        with self.assertRaisesRegex(RiverError, "maxpm target rename site"):
            core.project_rename(self.c, "deploy-site", "releases")
        core.target_add(self.c, "taken")
        with self.assertRaisesRegex(RiverError, "exists"):
            core.target_rename(self.c, "site", "taken")
        with self.assertRaisesRegex(RiverError, "deploy items of target taken"):
            core.project_rename(self.c, "other", "deploy-taken")
        res = core.target_rename(self.c, "site", "web", "t")
        self.assertEqual((res["name"], res["was"], res["projects"], res["items"], res["deploy_project"]),
                         ("web", "site", 2, 1, "deploy-web"))
        dep = core.item_show(self.c, dep["id"])
        self.assertEqual((dep["project"], dep["title"], dep["target"]), ("deploy-web", "Deploy web", "web"))
        self.assertEqual(core.project_show(self.c, "old")["target"], "web")
        self.assertTrue(core._project(self.c, "deploy-web")["notes"].startswith("Deploys to target web,"))
        self.assertEqual(core.setting(self.c, "review", item_id=dep["id"]), "on")
        shown = core.target_show(self.c, "web")
        self.assertEqual((shown["name"], sorted(p["name"] for p in shown["projects"])), ("web", ["deploy-web", "old"]))
        self.assertNotIn("aliases", shown)
        # The old names of the target and of its deploy project are unknown at once.
        with self.assertRaisesRegex(RiverError, "no target 'site'"):
            core.target_show(self.c, "site")
        with self.assertRaisesRegex(RiverError, "no project 'deploy-site'"):
            core.project_show(self.c, "deploy-site")
        # The next ship request joins the same deploy item.
        second = self.add("old", "more")
        self.assertEqual(core.ship(self.c, second, "t")["id"], dep["id"])
        self.assertEqual(core.target_own(self.c, "web", "w1")["owner"], "w1")
        self.assertEqual(core.project_target(self.c, "other", "web")["target"], "web")
        self.assertEqual([t["name"] for t in core.targets_view(self.c) if t["pending"]], ["web"])

    def test_the_command_renames_and_says_that_the_old_name_stops(self):
        from river import cli
        old_env = os.environ.get("MAXPM_DB")
        os.environ["MAXPM_DB"] = self.path
        try:
            def run(*words):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    cli.run(list(words))
                return out.getvalue(), err.getvalue()
            out, err = run("project", "rename", "old", "new")
            self.assertIn("project old is now new: 1 items, 1 goals", out)
            self.assertIn("the old name old does not work from now on", out)
            self.assertNotIn("old name", err)
            self.assertIn("new  (1 open)  [target site]\n", run("project", "list")[0])
            out, err = run("target", "rename", "site", "web")
            self.assertIn("target site is now web: 1 projects and 0 deploy, review and monitor items follow", out)
            self.assertIn("the old name site does not work from now on", out)
            self.assertTrue(run("target", "show", "web")[0].startswith("web\n"))
            with self.assertRaises(SystemExit):
                run("project", "rename", "new", "newer", "--alias-for", "30d")
        finally:
            if old_env is None:
                del os.environ["MAXPM_DB"]
            else:
                os.environ["MAXPM_DB"] = old_env


class Locked(Base):
    """Other commands hold the write lock of the queue longer than busy_timeout (#1615)."""

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "a", path=self.dir.name)
        for n in ("w1", "w2"):
            core.register(self.c, n)
        self.c.execute("PRAGMA busy_timeout=20")  # the tests wait 20 ms for the lock, not 10 s
        self.other = core.connect(self.path)
        self.addCleanup(self.other.close)

    def lock(self):
        self.other.execute("BEGIN IMMEDIATE")

    def unlock(self):
        if self.other.in_transaction:
            self.other.execute("ROLLBACK")

    def test_a_write_waits_again_and_then_refuses_with_the_next_step(self):
        class Busy:
            """A connection whose first `fails` BEGIN IMMEDIATE find the queue locked."""
            def __init__(self, conn, fails, error="database is locked"):
                self.conn, self.fails, self.error, self.begins = conn, fails, error, 0

            def execute(self, sql, *args):
                if sql == "BEGIN IMMEDIATE":
                    self.begins += 1
                    if self.begins <= self.fails:
                        raise sqlite3.OperationalError(self.error)
                return self.conn.execute(sql, *args)
        busy = Busy(self.c, core.LOCK_TRIES - 1)
        with core.tx(busy):
            busy.execute("UPDATE agents SET note='x' WHERE name='w1'")
        self.assertEqual((busy.begins, core.agent_status(self.c, "w1")["note"]), (core.LOCK_TRIES, "x"))
        # Locked for every try: a refusal that says what to do, and nothing of the command ran.
        self.lock()
        with self.assertRaisesRegex(core.RiverLocked, "queue is busy.*Run the command again"):
            core.agent_note(self.c, "w1", "y")
        self.assertIsInstance(core.RiverLocked("x"), RiverError)  # the command prints 'maxpm: ...', no traceback
        self.unlock()
        self.assertEqual(core.agent_status(self.c, "w1")["note"], "x")
        # Another error of the database is not a lock: no second try.
        broken = Busy(self.c, 1, "disk I/O error")
        with self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O error"):
            with core.tx(broken):
                pass
        self.assertEqual(broken.begins, 1)

    def test_a_locked_queue_never_ends_the_watch(self):
        core.register(self.c, "mark", human=True)
        core.manage(self.c, self.dir.name, "boss")
        t0, naps = core.now(), []

        def nap(s):
            naps.append(s)
            self.unlock()
        # The renewal of a poll finds the queue locked: the watch skips it and goes on to its time limit.
        self.lock()
        with mock.patch.object(core, "now", side_effect=lambda: t0 + timedelta(minutes=len(naps) * 20)):
            w = core.manage_watch(self.c, "boss", sleep=nap)
        self.assertEqual((w["result"], len(naps)), ("tick", 2))
        # Locked when a message is due: nothing is marked read, and the next poll returns with it.
        core.send(self.c, "alert", "w1 is stuck", to="boss", actor="w2", blocked=True)
        naps.clear()
        self.lock()
        w = core.manage_watch(self.c, "boss", sleep=nap)
        self.assertEqual((w["result"], [m["body"] for m in w["messages"]], len(naps)), ("messages", ["w1 is stuck"], 1))
        # Locked at the last write (the findings it saw): the watch still returns.
        self.lock()
        self.assertEqual(core.manage_watch(self.c, "boss", step="0s", sleep=nap)["result"], "tick")

    def test_a_locked_queue_never_ends_a_wait(self):
        core.wait(self.c, self.dir.name, "w1", step="0s", sleep=lambda s: None)
        t0, naps = core.now(), []

        def nap(s):  # the queue is locked from the first poll to the second
            naps.append(s)
            self.lock() if len(naps) == 1 else self.unlock()
        with mock.patch.object(core, "now", side_effect=lambda: t0 + timedelta(minutes=len(naps) * 6)):
            w = core.wait(self.c, self.dir.name, "w1", step="9m", sleep=nap)
        self.assertEqual((w["result"], len(naps)), ("again", 2))
        # maxpm inbox --wait: the messages come at the next poll.
        core.send(self.c, "note", "freeze ended", to="w1", actor="w2")
        naps.clear()
        self.lock()
        w = core.inbox_wait(self.c, "w1", sleep=lambda s: (naps.append(s), self.unlock()))
        self.assertEqual((w["result"], [m["body"] for m in w["messages"]], len(naps)), ("messages", ["freeze ended"], 1))
        # The page: the state read skips the sweep of a busy queue.
        self.lock()
        self.assertEqual(core.poll_activity(self.c, None), [])
        self.unlock()
        # Each write that gave up left a line beside the queue (#1617).
        with open(os.path.join(self.dir.name, "locked.log")) as f:
            self.assertRegex(f.read(), r"(?m)^\d{4}-\d\d-\d\dT\S+Z tries=1 pid=\d+")

    def test_a_poll_renews_the_agent_and_sweeps_each_half_minute(self):
        # #1617: each poll of maxpm wait, manage --watch and the page ran the whole activity, every 3 s.
        t0, clock = core.now(), [0]
        with mock.patch.object(core, "now", side_effect=lambda: t0 + timedelta(seconds=clock[0])), \
                mock.patch.object(core, "_sweep", wraps=core._sweep) as sweep, \
                mock.patch.object(core, "sync_needs_you", wraps=core.sync_needs_you) as sync:
            seen = []
            for clock[0] in (0, 3, 27, 30, 33, 60):
                core.poll_activity(self.c, "w1")
                seen.append((sweep.call_count, sync.call_count, core._agent(self.c, "w1")["last_seen"]))
            self.assertEqual([x[:2] for x in seen], [(1, 1), (1, 1), (1, 1), (2, 2), (2, 2), (3, 3)])
            self.assertEqual([x[2] for x in seen], [core.iso(t0 + timedelta(seconds=n)) for n in (0, 3, 27, 30, 33, 60)])
            # The page has no actor: between two sweeps it writes nothing.
            clock[0] = 63
            before = self.c.total_changes
            core.poll_activity(self.c, None)
            self.assertEqual((self.c.total_changes, sweep.call_count), (before, 3))

    def test_the_process_list_and_the_release_commits_come_before_the_lock(self):
        # ps for the sweep: a lease ran out.
        self.c.execute("UPDATE agents SET pid=1000, host=? WHERE name='w1'", (core.this_host(),))
        x = self.add("a", "a long test run")
        core.claim(self.c, x, "w1")
        asked = []

        def ps():
            asked.append(self.c.in_transaction)
            return "  1000      1     2:00:00\n"
        core.PROC_RUNNER = ps
        self.addCleanup(setattr, core, "PROC_RUNNER", None)
        core.activity(self.c, "w2")
        self.assertEqual(asked, [])  # no lease ran out: nobody asks
        self.c.execute("UPDATE items SET lease_expires_at=? WHERE id=?", (core.iso(core.now() - timedelta(minutes=1)), x))
        core.activity(self.c, "w2")
        self.assertEqual((asked, core.item_show(self.c, x)["status"]), ([False], "open"))
        # ps for a claim: the item is open, and its first agent may still work on it.
        del asked[:]
        core.agent_note(self.c, "w2", "an ordinary write asks for no process list")
        self.assertEqual(asked, [])
        self.assertEqual(core.next_item(self.c, "a", claim=True, actor="w2")[0]["id"], x)
        self.assertEqual(asked, [False])
        # release_commits for a claim that can meet a release review.
        core.target_add(self.c, "web", "push")
        core.project_add(self.c, "site", target="web", path=self.dir.name)
        core.config_set(self.c, "review", "on")
        core.config_set(self.c, "release_commits", "echo nothing")
        core.target_own(self.c, "web", "w1")
        y = core.item_add(self.c, "site", "page", 2, "", "any", (), "t")["id"]
        core.claim(self.c, y, "w1")
        core.done(self.c, y, "commit", "w1", ship_it=True)
        review = [i for i in core.item_show(self.c, y)["unblocks"] if core.item_show(self.c, i)["kind"] == "review"][0]
        core._RELEASE_COMMITS.clear()
        ran = []
        import subprocess
        real = subprocess.run

        def run(cmd, *a, **kw):
            if cmd == "echo nothing":
                ran.append(self.c.in_transaction)
            return real(cmd, *a, **kw)
        core.register(self.c, "w3")
        with mock.patch.object(subprocess, "run", run):
            got = core.next_item(self.c, None, claim=True, actor="w3")
            self.assertEqual(([i["id"] for i in got], ran), ([review], [False]))
            core.release(self.c, review, actor="w3")
            core._RELEASE_COMMITS.clear()
            del ran[:]
            self.assertEqual(core.claim(self.c, review, "w3")["assignee"], "w3")
            self.assertEqual(ran, [False])
