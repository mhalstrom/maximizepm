import argparse
import contextlib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone

from river import core
from river.core import RiverError
from river.agents import claude as claude_agent


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        # The folder goes last, after the cleanups a test adds (a second connection): Windows cannot
        # delete a file that is open.
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "t.db")
        # Everything that lives beside the queue (the hook file claude-hooks.json, serve-loop.json) goes to the
        # temporary folder, in every class: two Models tests wrote the person's real ~/.maximizepm/claude-hooks.json
        # (#5381 fixed Models alone; #5398).
        env = mock.patch.dict(os.environ, {"MAXPM_DB": self.path})
        env.start()
        self.addCleanup(env.stop)
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

    def test_the_machine_zone_keeps_its_summer_time_rules(self):
        # #5549: with no timezone setting, a date in summer showed the winter offset of today (22:59 PST for a
        # time due 23:59 PDT), and a time typed for that date was an hour off.
        core._MACHINE_ZONE.clear()
        self.addCleanup(core._MACHINE_ZONE.clear)
        with mock.patch.dict(os.environ, {"TZ": "America/Los_Angeles"}):
            self.assertEqual(str(core.local_zone()), "America/Los_Angeles")
            self.assertTrue(core.show_time("2027-03-16T06:59:00Z").endswith("2027-03-15 23:59 PDT"))
            self.assertTrue(core.show_time("2026-12-16T07:59:00Z").endswith("2026-12-15 23:59 PST"))
            start = core.parse_iso("2026-11-12T05:00:00Z")
            self.assertEqual(core.iso(core.parse_when("2027-07-01 09:00", start=start)), "2027-07-01T16:00:00Z")
        with mock.patch.dict(os.environ, {"TZ": ""}), \
                mock.patch.object(core.os, "readlink", lambda p: "/var/db/timezone/zoneinfo/America/New_York"):
            self.assertEqual(str(core.local_zone()), "America/New_York")
        with mock.patch.dict(os.environ, {"TZ": ""}), mock.patch.object(core.os, "readlink", side_effect=OSError), \
                mock.patch("builtins.open", side_effect=OSError):
            self.assertIsNotNone(core.local_zone().utcoffset(core.now()))  # Windows: the offset of now, as before


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


class ContinueTree(Base):
    """auto_continue tree (#4994, mark: 'auto_continue should be on until its tree is done'): after done, go gives a
    session only the tasks of its own tree; unrelated work goes to a new session."""

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "p", path=self.dir.name)
        for n in ("w1", "w2", "w3"):
            core.register(self.c, n)
        core.register(self.c, "mark", human=True)
        core.goal_add(self.c, "p", "g", outcome="o", done_when="d", shared=True)
        self.a = core.item_add(self.c, "p", "A", priority=1, goals=["g"])["id"]
        self.b = core.item_add(self.c, "p", "B waits on A", priority=2, after=[self.a])["id"]
        self.e = core.item_add(self.c, "p", "E unrelated and urgent", priority=0)["id"]
        self.d = core.item_add(self.c, "p", "D of the same goal", priority=3, goals=["g"])["id"]

    def test_the_default_is_tree_and_go_keeps_to_the_tree(self):
        self.assertEqual(core.setting(self.c, "auto_continue"), "tree")
        with self.assertRaisesRegex(RiverError, "auto_continue is tree, on, or off"):
            core.config_set(self.c, "auto_continue", "yes")
        core.claim(self.c, self.a, "w1")
        c = core.item_add(self.c, "p", "C found during A", priority=2, found_during=self.a, goals=[])["id"]
        core.done(self.c, self.a, "did A", "w1")
        self.assertEqual(core.session_tree(self.c, "w1"), {self.b, c, self.d})
        got = []
        for _ in range(3):
            b = core.go(self.c, self.dir.name, "w1")
            got.append(b["item"]["id"])
            self.assertTrue(b["auto_continue"])
            core.done(self.c, b["item"]["id"], "done", "w1")
        self.assertEqual(sorted(got), sorted([self.b, c, self.d]))  # never the urgent unrelated E
        b = core.go(self.c, self.dir.name, "w1")
        self.assertEqual((b["role"], b.get("tree_done")), ("idle", True))
        self.assertIn("no task of your tree", b["why"])
        # wait does not wake it for E, and Dispatch gives E to a fresh waiting session, not to w1.
        self.assertIsNone(core._work_for(self.c, "w1", ["p"]))
        core.wait(self.c, self.dir.name, "w1", step="0s", sleep=lambda s: None)
        self.assertIsNone(core.waiting_agent_for(self.c, "p", self.e))
        core.wait(self.c, self.dir.name, "w2", step="0s", sleep=lambda s: None)
        self.assertEqual(core.waiting_agent_for(self.c, "p", self.e), "w2")
        # A fresh session takes any work: E first.
        self.assertEqual(core.go(self.c, self.dir.name, "w3")["item"]["id"], self.e)

    def test_its_queue_comes_first_and_on_takes_any_work(self):
        core.claim(self.c, self.a, "w1")
        core.done(self.c, self.a, "did A", "w1")
        x = core.item_add(self.c, "p", "X for w1", priority=4)["id"]
        core.queue_add(self.c, "w1", x, actor="mark")
        self.assertEqual(core.go(self.c, self.dir.name, "w1")["item"]["id"], x)  # its own queue, outside the tree
        core.done(self.c, x, "done", "w1")
        core.config_set(self.c, "auto_continue", "on", agent="w1")
        b = core.go(self.c, self.dir.name, "w1")
        self.assertEqual(b["item"]["id"], self.e)  # on: the most important ready work
        self.assertEqual(b["continue_mode"], "on")

    def test_the_tree_reaches_two_links_from_a_task_the_session_held(self):
        """#5573: with no bound, tasks with many links joined the parts of the graph, and most sessions' trees held
        over 100 open tasks of many projects. The tree now reaches TREE_STEPS (2) links from each task it held."""
        hub = core.item_add(self.c, "p", "Hub that many tasks wait on", priority=2)["id"]
        near = core.item_add(self.c, "p", "Waits on the hub", priority=2, after=[hub])["id"]
        far = core.item_add(self.c, "p", "Found during the one that waits on the hub", priority=2,
                            found_during=near)["id"]
        core.dep_add(self.c, hub, [self.a])
        core.claim(self.c, self.a, "w1")
        core.done(self.c, self.a, "did A", "w1")
        tree = core.session_tree(self.c, "w1")
        self.assertTrue({self.b, hub, near, self.d} <= tree)  # one link, two links, and the goal's task
        self.assertNotIn(far, tree)  # three links away
        self.assertNotIn(self.e, tree)
        core.claim(self.c, hub, "w1")  # a task it takes is a new start: the chain goes on
        self.assertIn(far, core.session_tree(self.c, "w1"))

    def test_a_goal_owners_tree_is_its_goal(self):
        core.goal_add(self.c, "p", "own", outcome="o", done_when="d")
        f = core.item_add(self.c, "p", "F of the owned goal", priority=3, goals=["own"])["id"]
        core.goal_own(self.c, "own", "w1")
        core.claim(self.c, self.a, "w1")
        core.done(self.c, self.a, "did A", "w1")
        self.assertIn(f, core.session_tree(self.c, "w1"))
        self.assertNotIn(self.e, core.session_tree(self.c, "w1"))


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
        core.config_set(self.c, "auto_continue", "on")  # it takes unrelated work after done (not tree, #4994)
        # #1599 (research #1597): workers stood at their prompts for a full disk, a refused command, and the
        # word "push"; maxpm wait refuses while an item is held, so the wait for a limit is maxpm inbox --wait.
        import re
        from river import cli
        x, y = self.add("web", "page"), self.add("web", "form")
        core.config_set(self.c, "result_look", "push_first", project="web")  # the rule of #1599 is this value (#1668)

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
                      "Then it is a step for the user (below), with the exact command and the reason of the refusal; "
                      "the user runs it or allows it. Stand at your prompt only for a release to production or a "
                      "deletion of data", first)
        self.assertIn(f"The finished result needs a look: push, maxpm --as {me} done {x}, then check it yourself "
                      f"(open it, take screenshots, compare with the item), or maxpm --as {me} add \"Check <result>: "
                      f"<where>, with screenshots\" --found-during {x} (an agent does it). Add a person item (--doer "
                      f"human) only for a taste or product decision that no rule and no earlier word of the person "
                      f"settles; the manager sees it first. Ask before the push only for public text, a release to "
                      f"production, a step that deletes data or costs money, or when the item says so.", first)
        with self.assertRaisesRegex(RiverError, "finish or release it before you wait"):
            core.wait(self.c, self.web, me, step="0s", sleep=lambda s: None)
        # The short form of a later briefing keeps one line for each.
        core.done(self.c, x, "done", me)
        later = text(core.go(self.c, self.web, me))
        self.assertIn("Rules as before. Short form:", later)
        self.assertIn(f"a limit of the machine (disk, build stop, push freeze): keep the item, do the other parts, "
                      f"then maxpm --as {me} inbox --wait", later)
        self.assertIn("a refused command: a step for the user, with the exact command and the reason of the refusal",
                      later)
        self.assertIn(f"the result needs a look: push, done, then check it yourself or maxpm --as {me} add \"Check "
                      f"<result> ...\" --found-during {y}; a person item only for a taste or product decision", later)
        self.assertNotIn("Look at <result>\" --doer human", first + later)

    def test_the_project_decides_the_look_at_a_finished_result(self):
        core.config_set(self.c, "auto_continue", "on")  # it takes unrelated work after done (not tree, #4994)
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
        # ask_first: the worker commits, asks in the queue, and waits for the yes before the push. A project where
        # nobody chose has this value (#1682).
        self.assertEqual(core.look_setup(self.c, "web"), {
            "value": "ask_first", "chosen": False, "recommended": "ask_first",
            "why": "it has no deploy target, so a push can be live at once"})
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
        self.assertIn("  look at a finished result: project web has result_look ask_first (nobody chose for this "
                      "project yet).\n", shown("web"))
        self.assertIn("    recommended here: ask_first, because it has no deploy target, so a push can be live at once. "
                      "Also ask_first when a push to main is live at once (a site, a public repository).\n"
                      "    tell the person the recommendation and its reason; store their answer: "
                      "maxpm config set result_look ask_first|push_first --project web\n", shown("web"))
        # push_first: the rule of #1599, for a project where the person chose it.
        core.config_set(self.c, "result_look", "push_first", project="api")
        b = core.go(self.c, self.api)
        other, first = b["agent"], text(b)
        self.assertEqual((b["item"]["id"], b["result_look"]), (z, "push_first"))
        self.assertIn(f"The finished result needs a look: push, maxpm --as {other} done {z}, then check it yourself",
                      first)
        self.assertIn(f"maxpm --as {other} add \"Check <result>: <where>, with screenshots\" --found-during {z} (an "
                      f"agent does it). Add a person item (--doer human) only for a taste or product decision", first)
        self.assertNotIn("asks first", first)
        self.assertEqual(core.project_show(self.c, "api")["result_look"], "push_first")
        self.assertIn("  look at a finished result: project api has result_look push_first (chosen for this project).\n",
                      shown("api"))
        self.assertIn("    change it: maxpm config set result_look ask_first --project api\n", shown("api"))

    def test_the_setup_of_a_project_shows_the_look_setting(self):
        # #1677, mark: 'It should just be very clear to the agent when it's being set up.'
        from river import cli

        def added(*words):
            a = cli.build_parser().parse_args(["project", "add", *words])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.render(a, cli.dispatch(self.c, a, "ag"))
            return out.getvalue()
        # #1682, mark's decision C: no silent default. Nobody chose: ask_first holds, and the reply says what to
        # recommend and why. No deploy target: a push can be live at once.
        text = added("shop", "--description", "the shop")
        self.assertIn("look at a finished result: project shop has result_look ask_first (nobody chose for this "
                      "project yet).\n"
                      "  ask_first:  the worker shows the result, asks in the queue, and waits for the person's yes "
                      "before the push.\n"
                      "  push_first: the worker pushes, closes the item, and checks the result (or adds an agent "
                      "check); the person gets only a taste or product decision.\n"
                      "  recommended here: ask_first, because it has no deploy target, so a push can be live at once. "
                      "Also ask_first when a push to main is live at once (a site, a public repository).\n"
                      "  tell the person the recommendation and its reason; store their answer: "
                      "maxpm config set result_look ask_first|push_first --project shop\n", text)
        # A deploy target puts a release step between a push and the users: recommend push_first. The value in
        # force stays ask_first until the person confirms.
        core.target_add(self.c, "prod", "rsync, then restart")
        text = added("blog", "--target", "prod")
        self.assertIn("project blog has result_look ask_first (nobody chose for this project yet).", text)
        self.assertIn("  recommended here: push_first, because it ships to the deploy target prod, so a release step "
                      "stands between a push and the users.", text)
        # maxpm project add takes the value that the person confirmed.
        text = added("docs", "--target", "prod", "--result-look", "push_first")
        self.assertIn("project docs has result_look push_first (chosen for this project).", text)
        self.assertIn("  change it: maxpm config set result_look ask_first --project docs\n", text)
        self.assertEqual(core.setting(self.c, "result_look", project_id=core._project(self.c, "docs")["id"]), "push_first")
        with self.assertRaisesRegex(RiverError, "result_look is ask_first or push_first"):
            core.project_add(self.c, "wiki", result_look="push")
        self.assertNotIn("wiki", [p["name"] for p in core.project_list(self.c)])
        # A list of projects stays short.
        a = cli.build_parser().parse_args(["project", "list"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.render(a, cli.dispatch(self.c, a, "ag"))
        self.assertNotIn("result_look", out.getvalue())
        # The guides say it too.
        self.assertIn("maxpm project add <name> --result-look ask_first|push_first", cli.SETUP)
        self.assertIn("A project\n       where nobody chose has this value.", cli.SETUP)
        self.assertIn("--result-look ask_first|push_first", (cli.GUIDES / "maxpm-manager" / "SKILL.md").read_text(encoding="utf-8"))

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

    def test_nothing_planned_when_empty_or_outside_blocked(self):
        # There is no planner role (#4978): the session is idle and says why.
        b = core.go(self.c, self.web)
        self.assertEqual((b["role"], b.get("nothing_planned")), ("idle", True))
        self.assertIn("the project has no open items", b["why"])
        x = self.add("web", "x")
        core.block(self.c, x, "waiting on design")
        b = core.go(self.c, self.web)
        self.assertEqual((b["role"], b["why"]), ("idle", "every open item waits on something outside the queue"))
        self.assertNotIn("planner", core.ROLES)

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
                          "created_at TEXT NOT NULL); DELETE FROM meta WHERE key LIKE 'schema:%';")  # an old queue has no schema id of this code (#5396)
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
        raw.execute("DELETE FROM meta WHERE key LIKE 'schema:%'")  # an old queue has no schema id of this code (#5396)
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

    def test_a_shared_file_of_more_than_conflict_max_lines_lines_makes_no_conflict(self):
        """#5579: every item that touched river/core.py conflicted with every other one, so most work on the project
        ran one item at a time; git merges changes in different parts of a large file."""
        root = os.path.join(self.dir.name, "big")
        os.makedirs(os.path.join(root, "src"))
        for name, n in (("big.py", 2001), ("small.py", 2000)):
            with open(os.path.join(root, "src", name), "w", encoding="utf-8") as f:
                f.write("x\n" * n)
        core.project_add(self.c, "b", path=root)
        one = core.item_add(self.c, "b", "one", touches=["src/big.py"])["id"]
        two = core.item_add(self.c, "b", "two", touches=["src/big.py", "src/small.py"])["id"]
        three = core.item_add(self.c, "b", "three", touches=["src/small.py"])["id"]
        whole = core.item_add(self.c, "b", "the folder", touches=["src/"])["id"]
        new = core.item_add(self.c, "b", "a new file", touches=["src/new.py"])["id"]
        ann = core.annotate(self.c)
        self.assertEqual(ann[one]["conflicts"], [])  # only the large file: no conflict
        self.assertEqual(ann[two]["conflicts"], [three, whole])  # the small file still conflicts
        self.assertEqual(ann[whole]["conflicts"], [two, three, new])  # the folder holds small and unwritten files
        core.config_set(self.c, "conflict_max_lines", "0", project="b")  # 0: every shared file conflicts again
        core.item_edit(self.c, one, touches=["src/big.py", "README.md"])
        self.assertEqual(core.annotate(self.c)[one]["conflicts"], [two, whole])

    def test_the_one_time_pass_removes_the_conflicts_of_a_large_file(self):
        root = os.path.join(self.dir.name, "big")
        os.makedirs(root)
        with open(os.path.join(root, "big.py"), "w", encoding="utf-8") as f:
            f.write("x\n" * 3000)
        core.project_add(self.c, "b", path=root)
        core.config_set(self.c, "conflict_max_lines", "0", project="b")
        one = core.item_add(self.c, "b", "one", touches=["big.py"])["id"]
        two = core.item_add(self.c, "b", "two", touches=["big.py"])["id"]
        self.assertEqual(core.annotate(self.c)[one]["conflicts"], [two])  # as before #5579
        core.config_unset(self.c, "conflict_max_lines", project="b")
        self.c.execute("DELETE FROM meta WHERE key='big_file_conflicts'")
        self.c.commit()
        core._migrate_big_file_conflicts(self.c)
        self.assertEqual(core.annotate(self.c)[one]["conflicts"], [])
        last = self.c.execute("SELECT change FROM events WHERE item_id IN (?, ?) ORDER BY id DESC", (one, two)).fetchone()
        self.assertIn("no longer conflicts with", last["change"])

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
        with open(self.path, "a", encoding="utf-8") as f:
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
        with open(os.path.join(folder, f"{sub or sid}.jsonl"), "w", encoding="utf-8") as f:
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
        self.assertEqual(claude_agent.read_transcript(os.path.join(self.root, "projects", "-work-a", self.SID2 + ".jsonl"))[1], True)
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
        self.assertFalse(claude_agent.read_transcript(os.path.join(self.root, "projects", "-work-a", self.SID + ".jsonl"))[1])

    def test_an_interactive_fork_is_known_by_its_snapshot_time(self):
        path = os.path.join(self.root, "projects", "-work-a", self.SID + ".jsonl")
        snap = lambda m: {"type": "file-history-snapshot", "snapshot": {"timestamp": self.at(m)}}
        self.write(self.SID, [{"type": "custom-title", "customTitle": "w"}, snap(10),
                              {"type": "user", "timestamp": self.at(2), "message": {"content": "base"}},
                              self.req(3, "copied"),
                              {"type": "user", "timestamp": self.at(10), "message": {"content": "go"}},
                              self.req(11, "own")])
        reqs, fork = claude_agent.read_transcript(path)
        self.assertEqual((fork, sorted(reqs)), (True, ["own"]))
        # A fresh interactive session: its first message comes just after its snapshot.
        self.write(self.SID, [snap(0), {"type": "user", "timestamp": self.at(0.005), "message": {"content": "go"}},
                              self.req(1, "own")])
        self.assertFalse(claude_agent.read_transcript(path)[1])

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

    def test_done_with_ship_refused_leaves_the_item_open(self):
        """#5578: done --ship in a project with no target printed the error, and the item was done all the same."""
        core.project_add(self.c, "misc")
        x = self.add("misc", "x")
        core.claim(self.c, x, "dev")
        with self.assertRaisesRegex(RiverError, rf"misc has no deploy target.*#{x} stays open \(close it without --ship"):
            core.done(self.c, x, "out", "dev", ship_it=True)
        it = core._item(self.c, x)
        self.assertEqual((it["status"], it["output"], it["assignee"]), ("in_progress", "", "dev"))
        self.assertEqual(core.done(self.c, x, "out", "dev")["status"], "done")

    def test_a_ship_of_an_open_item_warns_that_the_release_waits_on_it(self):
        # #5386: a walk item, or part 1 of a split build, stays open after its release; ship held the release.
        core.config_set(self.c, "review", "on")
        walk = self.add("site", "walk the onboarding")
        core.claim(self.c, walk, "dev")
        d = core.ship(self.c, walk, "dev")
        rv = [i for i in d["waits_on"] if i != walk][0]
        w = d["ship_warning"]
        self.assertIn(f"#{walk} is in progress: deploy #{d['id']} and review #{rv} now wait on it", w)
        self.assertIn(f"maxpm done {walk} --ship", w)
        self.assertIn(f"maxpm undep {d['id']} --on {walk}; maxpm undep {rv} --on {walk}", w)
        from river import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, {"MAXPM_DB": self.path, "MAXPM_QUIET": "1"}):
            cli.run(["--as", "dev", "ship", str(walk)])
        self.assertIn(f"  WARNING: #{walk} is in progress:", out.getvalue())
        # Done, then ship: no warning.
        x = self.add("site", "a page")
        core.claim(self.c, x, "ops")
        res = core.done(self.c, x, "abc", "ops", ship_it=True)
        self.assertNotIn("ship_warning", core.ship(self.c, x, "ops"))
        self.assertEqual(res["shipped_in"], d["id"])


class Plan(Base):
    def test_plan_says_the_planner_role_is_gone(self):
        # #4978, mark: 'get rid of planner'. maxpm plan stays only as a notice (CLAUDE.md files still name it).
        from river import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, {"MAXPM_DB": self.path, "MAXPM_QUIET": "1"}):
            self.assertEqual(cli.run(["plan"]), 0)
        self.assertIn("There is no planner role", out.getvalue())
        self.assertIn("maxpm guide manager, section Writing tasks", out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.run(["guide", "planner"])
        self.assertIn("## Writing tasks", (cli.GUIDES / "maxpm-manager" / "SKILL.md").read_text(encoding="utf-8"))



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


class GoalDoneWhen(Base):
    """The close of a goal's last open item asks whether its done-when holds: the owner, else the manager (#4970)."""
    def setUp(self):
        super().setUp()
        core.project_add(self.c, "shop")
        for n in ("ag", "bo"):
            core.register(self.c, n)

    def finish(self, item_id, who):
        core.claim(self.c, item_id, who)
        return core.done(self.c, item_id, "ok", who)

    def test_owner_is_asked_when_another_agent_closes_the_last_item(self):
        core.goal_add(self.c, "shop", "checkout", "customers can pay", "a test order succeeds", "t")
        a = core.item_add(self.c, "shop", "pay button", goals=["checkout"])["id"]
        b = core.item_add(self.c, "shop", "receipt", goals=["checkout"])["id"]
        core.goal_own(self.c, "checkout", "ag")
        r = core.done(self.c, a, "ok", "bo")  # one item still open: nobody is asked
        self.assertEqual(r["goal_checks"], [])
        r = core.drop(self.c, b, "bo", note="not needed")  # a drop of the last one asks too
        self.assertEqual([(x["goal"], x["asked"]) for x in r["goal_checks"]], [("checkout", "ag")])
        body = [m["body"] for m in core.inbox(self.c, "ag") if "last open item" in m["body"]]
        self.assertEqual(len(body), 1)  # in the notice that bo dropped it, not a second one
        self.assertIn(f"bo dropped #{b} receipt (your goal checkout): not needed. It was the last open item", body[0])
        self.assertIn("Does its done-when hold (a test order succeeds)?", body[0])
        self.assertIn("maxpm goal done checkout", body[0])
        self.assertEqual(core.manager_findings(self.c)["goal_checks"], [])  # the owner decides, not the manager

    def test_owner_that_closes_the_last_item_reads_the_question_in_the_reply(self):
        from river import cli
        core.goal_add(self.c, "shop", "checkout", done_when="a test order succeeds", actor="t")
        core.goal_own(self.c, "checkout", "ag")
        a = core.item_add(self.c, "shop", "pay button", actor="ag")["id"]
        core.item_add(self.c, "shop", "someday", actor="ag", backlog="later")
        r = self.finish(a, "ag")
        self.assertEqual([(x["asked"], len(x["backlog"])) for x in r["goal_checks"]], [("you", 1)])
        self.assertFalse([m for m in core.inbox(self.c, "ag") if "last open item" in m["body"]])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli._print_show(r)  # the reply of done
        self.assertIn("GOAL checkout has no open item now: you own it", out.getvalue())
        self.assertIn("--drop-open", out.getvalue())

    def test_goal_with_no_owner_is_a_manager_finding_until_completed_or_given_work(self):
        from river import cli
        core.goal_add(self.c, "shop", "speed", "fast pages", "p95 under 1s", "t", shared=True)
        core.goal_add(self.c, "shop", "empty", "nothing planned yet", actor="t")  # no items: no question
        a = core.item_add(self.c, "shop", "cache", goals=["speed"])["id"]
        self.assertEqual(core.manager_findings(self.c)["goal_checks"], [])
        r = self.finish(a, "ag")
        self.assertEqual([(x["goal"], x["asked"]) for x in r["goal_checks"]], [("speed", "manager")])
        f = core.manager_findings(self.c)
        self.assertEqual([(x["goal"], x["shared"], x["last"]["id"]) for x in f["goal_checks"]], [("speed", True, a)])
        self.assertIn("goal:speed", core._finding_keys(f))
        text = "\n".join(cli._findings_lines(f, "maxpm --as m"))
        self.assertIn("GOAL DONE? speed [shop] has no open item and is shared (no owner)", text)
        self.assertIn("Does its done-when hold (p95 under 1s)? Yes: maxpm --as m goal done speed", text)
        b = core.item_add(self.c, "shop", "cdn", goals=["speed"])["id"]  # the manager says no: more work
        self.assertEqual(core.manager_findings(self.c)["goal_checks"], [])
        self.finish(b, "ag")
        core.goal_done(self.c, "speed", "p95 is 0.8s", "m")
        self.assertEqual(core.manager_findings(self.c)["goal_checks"], [])

    def test_expired_owner_counts_as_no_owner(self):
        core.goal_add(self.c, "shop", "g1", actor="t")
        core.goal_own(self.c, "g1", "ag")
        a = core.item_add(self.c, "shop", "x", actor="ag")["id"]
        core.claim(self.c, a, "ag")
        self.c.execute("UPDATE goals SET owner_expires_at=?", (core.iso(core.now() - timedelta(minutes=1)),))
        r = core.done(self.c, a, "ok", "ag")
        self.assertEqual(r["goal_checks"][0]["asked"], "manager")
        self.assertEqual([x["goal"] for x in core.manager_findings(self.c)["goal_checks"]], ["g1"])

    def test_completed_sub_goal_asks_about_its_parent(self):
        core.goal_add(self.c, "shop", "launch", done_when="the shop is live", actor="t", shared=True)
        core.goal_add(self.c, "shop", "pay", actor="t", parent="launch")
        a = core.item_add(self.c, "shop", "stripe", goals=["pay"])["id"]
        r = self.finish(a, "ag")
        self.assertEqual([x["goal"] for x in r["goal_checks"]], ["pay"])  # the parent has an open sub-goal still
        g = core.goal_done(self.c, "pay", "stripe works", "ag")
        self.assertEqual([(x["goal"], x["asked"]) for x in g["goal_checks"]], [("launch", "manager")])
        self.assertEqual([x["goal"] for x in core.manager_findings(self.c)["goal_checks"]], ["launch"])


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
            self.assertFalse((dest / "maxpm-planner").exists())
            (dest / "maxpm-planner").symlink_to(dest / "gone", target_is_directory=True)  # a link from before #4978
            self.assertIn(f"removed {dest / 'maxpm-planner'} (the planner role is gone)", cli.install_skills(dest))
            self.assertFalse((dest / "maxpm-planner").is_symlink())
            (dest / "maxpm").unlink()
            (dest / "maxpm").mkdir()
            with self.assertRaises(RiverError):
                cli.install_skills(dest)  # a real folder may hold edits
            cli.install_skills(dest, copy=True, force=True)
            self.assertFalse((dest / "maxpm").is_symlink())
            self.assertEqual(len(lines), 2)  # maxpm, maxpm-manager
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
    def test_backslashes_match_forward_slashes(self):
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
        core.config_set(self.c, "auto_continue", "on")  # it takes unrelated work after done (not tree, #4994)
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
        self.assertEqual(self.sent[0][2], f"http://127.0.0.1:8765/?item={i}")
        self.assertEqual(self.notify.status(self.c)["channels"][0]["pending"], 0)

    def test_test_and_unknown_channel(self):
        self.assertTrue(self.notify.test(self.c, "fake")["ok"])
        with self.assertRaises(RiverError):
            self.notify.test(self.c, "nope")
        core.config_set(self.c, "notify_batch_window", "0s")
        with self.assertRaises(RiverError):
            core.config_set(self.c, "notify_interval", "often")


class Ntfy(Base):
    def setUp(self):
        super().setUp()
        # relay.json sits beside the queue database: keep this test away from the real one.
        p = mock.patch.dict(os.environ, {"MAXPM_DB": self.path})
        p.start()
        self.addCleanup(p.stop)

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


class EmailRemoved(Base):
    def test_old_email_settings_do_nothing(self):
        # The email channel is gone (#4986, mark: not completed, can be removed). A row of its settings that a
        # person set earlier stays in the settings table and does nothing; config list and unset still work.
        from river import notify
        self.assertNotIn("email", notify.ADAPTERS)
        for k in ("email_to", "email_from", "smtp_host", "smtp_port", "smtp_user", "email_batch_window"):
            self.assertNotIn(k, core.DEFAULT_SETTINGS)
            with self.assertRaisesRegex(RiverError, "unknown setting"):
                core.config_set(self.c, k, "x")
        with core.tx(self.c):
            self.c.execute("INSERT INTO settings(scope, key, value) VALUES ('global', 'email_to', 'me@example.com')")
        self.assertIn("email_to", str(core.config_list(self.c)["overrides"]))
        from river import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.dict(os.environ, {"MAXPM_DB": self.path, "MAXPM_QUIET": "1"}):
            cli.run(["config", "get"])
        self.assertIn("notify_channels", out.getvalue())
        core.config_unset(self.c, "email_to")
        self.assertNotIn("email_to", str(core.config_list(self.c)["overrides"]))


class Mac(Base):
    def setUp(self):
        super().setUp()
        # The hint marker sits beside the queue database: keep it in this test's folder.
        p = mock.patch.dict(os.environ, {"MAXPM_DB": self.path})
        p.start()
        self.addCleanup(p.stop)

    def run_mac(self, notifier, returncode=0, err=None):
        from river import notify
        seen = []

        class Done:
            def __init__(self): self.returncode, self.stderr = returncode, "boom"

        def fake_run(cmd, **kw):
            seen.append(cmd)
            return Done()

        with mock.patch("sys.platform", "darwin"), mock.patch("shutil.which", lambda name: notifier), \
                mock.patch("subprocess.run", fake_run), contextlib.redirect_stderr(err or io.StringIO()):
            notify.ADAPTERS["mac"](self.c)('Say "yes"', "#3 sign", "http://127.0.0.1:8765/#item-3")
        return seen[0]

    def test_osascript_hint_shows_once(self):
        from river import notify
        first, second = io.StringIO(), io.StringIO()
        self.run_mac(None, err=first)
        self.run_mac(None, err=second)
        self.assertIn(notify.MAC_HINT, first.getvalue())
        self.assertEqual(second.getvalue(), "")
        self.assertTrue(os.path.exists(os.path.join(self.dir.name, "mac-hint-shown")))
        core.config_set(self.c, "notify_channels", "mac")
        with mock.patch("sys.platform", "darwin"), mock.patch("shutil.which", lambda name: None):
            self.assertIsNone(notify.status(self.c)["hint"])

    def test_status_shows_the_hint_first(self):
        from river import notify
        core.config_set(self.c, "notify_channels", "mac")
        with mock.patch("sys.platform", "darwin"), mock.patch("shutil.which", lambda name: None):
            self.assertEqual(notify.status(self.c)["hint"], notify.MAC_HINT)
            self.assertIsNone(notify.status(self.c)["hint"])
        err = io.StringIO()
        self.run_mac(None, err=err)
        self.assertEqual(err.getvalue(), "")

    def test_terminal_notifier_never_hints(self):
        from river import notify
        err = io.StringIO()
        self.run_mac("/opt/homebrew/bin/terminal-notifier", err=err)
        self.assertEqual(err.getvalue(), "")
        core.config_set(self.c, "notify_channels", "mac")
        with mock.patch("sys.platform", "darwin"), \
                mock.patch("shutil.which", lambda name: "/opt/homebrew/bin/terminal-notifier"):
            self.assertIsNone(notify.status(self.c)["hint"])
        self.assertFalse(os.path.exists(os.path.join(self.dir.name, "mac-hint-shown")))

    def test_osascript_passes_texts_as_arguments(self):
        cmd = self.run_mac(None)
        self.assertEqual(cmd[0], "osascript")
        self.assertEqual(cmd[-3:], ['Say "yes"', "#3 sign", "http://127.0.0.1:8765/#item-3"])
        self.assertNotIn('Say "yes"', " ".join(cmd[:-3]))

    def test_terminal_notifier_opens_the_page(self):
        cmd = self.run_mac("/opt/homebrew/bin/terminal-notifier")
        self.assertEqual(cmd[0], "/opt/homebrew/bin/terminal-notifier")
        self.assertEqual(cmd[cmd.index("-open") + 1], "http://127.0.0.1:8765/#item-3")
        self.assertTrue(cmd[cmd.index("-contentImage") + 1].replace(os.sep, "/").endswith("brand/apple-touch-icon.png"))  # #4705

    def test_terminal_notifier_escapes_a_leading_bracket(self):
        from river import notify
        seen = []

        class Done:
            returncode, stderr = 0, ""

        with mock.patch("sys.platform", "darwin"), \
                mock.patch("shutil.which", lambda name: "/opt/homebrew/bin/terminal-notifier"), \
                mock.patch("subprocess.run", lambda cmd, **kw: seen.append(cmd) or Done()):
            notify.ADAPTERS["mac"](self.c)("(maxpm) alert", "[maxpm alert from manager] read it", None)
        cmd = seen[0]
        self.assertEqual(cmd[cmd.index("-title") + 1], "\\(maxpm) alert")
        self.assertEqual(cmd[cmd.index("-message") + 1], "\\[maxpm alert from manager] read it")
        for text in ("[x", "(x", "{x", "<x", '"x', "'x", "-x", "\\x"):
            with self.subTest(text=text):
                self.assertEqual(notify._tn_text(text), "\\" + text)

    def test_terminal_notifier_escapes_a_batch(self):
        """A batch body starts with '- ', which NSUserDefaults read as the next option: every batch failed (#5042)."""
        from river import notify
        seen = []

        class Done:
            returncode, stderr = 0, ""

        for i in (1, 2):
            self.c.execute("INSERT INTO needs_you(kind, summary, opened_at) VALUES ('item', ?, '2026-11-09T00:00:00Z')",
                           (f"#{i} a step is ready for a person",))
        rows = [dict(r, item_id=None, message_id=None, human=None)
                for r in self.c.execute("SELECT id, summary FROM needs_you ORDER BY id")]
        title, body, url = notify.compose(self.c, rows)
        self.assertTrue(body.startswith("- "))
        with mock.patch("sys.platform", "darwin"), \
                mock.patch("shutil.which", lambda name: "/opt/homebrew/bin/terminal-notifier"), \
                mock.patch("subprocess.run", lambda cmd, **kw: seen.append(cmd) or Done()):
            notify.ADAPTERS["mac"](self.c)(title, body, url)
        self.assertEqual(seen[0][seen[0].index("-message") + 1], "\\" + body)
        self.assertEqual(notify._tn_text("#3 sign"), "#3 sign")
        self.assertEqual(notify._tn_text(""), "")

    def test_failure_and_other_platforms(self):
        from unittest import mock
        from river import notify
        with self.assertRaises(OSError):
            self.run_mac(None, returncode=1)
        with mock.patch("sys.platform", "linux"), self.assertRaises(RiverError):
            notify.ADAPTERS["mac"](self.c)


class Wsl(Base):
    """MaximizePM in WSL 2, the supported way on Windows (#5180): the page and notices open on the Windows side."""

    class Done:
        def __init__(self, code=0):
            self.returncode, self.stderr = code, "boom"

    def wsl(self):
        return mock.patch.multiple(core, PLATFORM="linux"), mock.patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu"})

    def test_wsl_is_linux_with_the_wsl_variables_or_a_microsoft_kernel(self):
        with mock.patch.object(core, "PLATFORM", "darwin"), mock.patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu"}):
            self.assertFalse(core.is_wsl())
        with mock.patch.object(core, "PLATFORM", "linux"), mock.patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu"}):
            self.assertTrue(core.is_wsl())
        env = {k: v for k, v in os.environ.items() if k not in ("WSL_DISTRO_NAME", "WSL_INTEROP")}
        for release, want in (("5.15.153.1-microsoft-standard-WSL2", True), ("6.8.0-45-generic", False)):
            with self.subTest(release=release), mock.patch.object(core, "PLATFORM", "linux"), \
                    mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(core.Path, "read_text", lambda self, *a, **k: release):
                self.assertEqual(core.is_wsl(), want)

    def test_the_page_opens_in_the_windows_browser(self):
        seen = []
        run = lambda cmd, **kw: seen.append((cmd, kw)) or self.Done()
        a, b = self.wsl()
        with a, b, mock.patch("shutil.which", lambda name: "/mnt/c/WINDOWS/System32/WindowsPowerShell/v1.0/powershell.exe"
                                if name == "powershell.exe" else None):
            self.assertTrue(core.open_in_browser("http://127.0.0.1:8765/#item-3'x", run=run))
        cmd, kw = seen[0]
        self.assertTrue(cmd[0].endswith("powershell.exe"))
        self.assertEqual(cmd[-1], "Start-Process 'http://127.0.0.1:8765/#item-3''x'")
        with a, b, mock.patch("shutil.which", lambda name: "/usr/bin/wslview" if name == "wslview" else None):
            core.open_in_browser("http://127.0.0.1:8765/", run=run)
        self.assertEqual(seen[1][0], ["/usr/bin/wslview", "http://127.0.0.1:8765/"])
        with a, b, mock.patch("shutil.which", lambda name: None), mock.patch("os.access", lambda p, m: False):
            self.assertFalse(core.open_in_browser("http://127.0.0.1:8765/", run=run))
        with mock.patch.object(core, "PLATFORM", "darwin"), mock.patch("webbrowser.open", lambda url: seen.append(url) or True):
            self.assertTrue(core.open_in_browser("http://127.0.0.1:8765/"))
        self.assertEqual(seen[-1], "http://127.0.0.1:8765/")

    def test_a_windows_notice_carries_the_texts_as_data(self):
        import base64
        import re
        from river import notify
        seen = []
        a, b = self.wsl()
        with a, b, mock.patch("shutil.which", lambda name: "/mnt/c/ps/powershell.exe" if name == "powershell.exe" else None), \
                mock.patch("subprocess.run", lambda cmd, **kw: seen.append(cmd) or self.Done()):
            notify.ADAPTERS["windows"](self.c)('Say "yes" & <go>', "#3 sign; $(rm -rf ~)", "http://127.0.0.1:8765/#item-3")
        cmd = seen[0]
        self.assertEqual(cmd[:4], ["/mnt/c/ps/powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand"])
        script = base64.b64decode(cmd[4]).decode("utf-16-le")
        self.assertNotIn("Say", script)
        self.assertIn(notify.WINDOWS_APP_ID, script)
        xml = base64.b64decode(re.search(r"FromBase64String\('([^']+)'\)", script).group(1)).decode()
        self.assertIn("<text>Say \"yes\" &amp; &lt;go&gt;</text>", xml)
        self.assertIn("<text>#3 sign; $(rm -rf ~)</text>", xml)
        self.assertIn('activationType="protocol" launch="http://127.0.0.1:8765/#item-3"', xml)
        self.assertNotIn("launch=", notify.toast_command("ps", "t", "b", None)[4])

    def test_a_windows_notice_fails_aloud(self):
        from river import notify
        a, b = self.wsl()
        with a, b, mock.patch("shutil.which", lambda name: "/mnt/c/ps/powershell.exe"), \
                mock.patch("subprocess.run", lambda cmd, **kw: self.Done(1)), self.assertRaises(OSError):
            notify.ADAPTERS["windows"](self.c)("t", "b", None)
        with mock.patch.object(core, "PLATFORM", "darwin"), self.assertRaises(RiverError):
            notify.ADAPTERS["windows"](self.c)
        with a, b, mock.patch("shutil.which", lambda name: None), mock.patch("os.access", lambda p, m: False), \
                self.assertRaisesRegex(RiverError, "appendWindowsPath"):
            notify.ADAPTERS["windows"](self.c)


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
        core.config_set(self.c, "auto_continue", "on")  # it takes unrelated work after done (not tree, #4994)
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


class AddMine(Base):
    """maxpm add --mine: an item that a session adds for the work it does now is reserved for it at once,
    so no other session starts the same work (#4942)."""

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "a", path=self.dir.name)
        for n in ("boss", "aa", "bb"):
            core.register(self.c, n)
        self.held = self.add("a", "what aa holds", p=0)
        core.claim(self.c, self.held, "aa")

    def test_mine_reserves_it_for_the_adder_and_nobody_else_gets_it(self):
        a = core.item_add(self.c, "a", "research mark asked for in my chat", actor="aa", mine=True)
        x = a["id"]
        self.assertTrue(a["mine"])
        it = core._item(self.c, x)
        self.assertEqual((it["reserved_for"], it["reserved_until"], it["reserved_by"]), ("aa", None, "aa"))
        self.assertIn("reserved for aa: added for its own work (maxpm add --mine)",
                      [r["change"] for r in self.c.execute("SELECT change FROM events WHERE item_id=?", (x,))])
        self.assertEqual(core.next_item(self.c, actor="bb", limit=5), [])
        self.assertIsNone(core.go(self.c, self.dir.name, "bb").get("item"))
        with self.assertRaisesRegex(RiverError, "reserved for aa"):
            core.launch_target(self.c, item=x, actor="boss")  # Dispatch and launch --item start with it
        with self.assertRaisesRegex(RiverError, "already reserved for aa"):
            core.push(self.c, x, "bb", None, "boss")
        with self.assertRaises(RiverError):
            core.claim(self.c, x, "bb")
        core.done(self.c, x, "written, abc123", "aa")  # the adder closes it without a claim
        self.assertEqual(core._item(self.c, x)["status"], "done")

    def test_mine_ends_when_the_session_stops(self):
        x = core.item_add(self.c, "a", "my own work", actor="aa", mine=True)["id"]
        core.register(self.c, "mark", human=True)
        core.stop_agent(self.c, "aa", "end of the day", "mark")
        core.activity(self.c, "bb")
        self.assertIsNone(core._item(self.c, x)["reserved_for"])
        self.assertEqual([i["id"] for i in core.next_item(self.c, actor="bb", limit=5)], [x])

    def test_mine_refusals(self):
        before = self.c.execute("SELECT COUNT(*) n FROM items").fetchone()["n"]
        with self.assertRaisesRegex(RiverError, "needs an agent name"):
            core.item_add(self.c, "a", "t1", mine=True)
        with self.assertRaisesRegex(RiverError, "a person's item"):
            core.item_add(self.c, "a", "t2", doer="human", actor="aa", mine=True)
        with self.assertRaisesRegex(RiverError, "backlog"):
            core.item_add(self.c, "a", "t3", actor="aa", backlog="later", mine=True)
        with self.assertRaisesRegex(RiverError, "not registered"):
            core.item_add(self.c, "a", "t4", actor="nobody", mine=True)
        self.assertEqual(self.c.execute("SELECT COUNT(*) n FROM items").fetchone()["n"], before)

    def test_cli_mine_says_what_to_do(self):
        from river import cli
        old_env = os.environ.get("MAXPM_DB")
        os.environ["MAXPM_DB"] = self.path
        self.addCleanup(lambda: os.environ.pop("MAXPM_DB") if old_env is None else os.environ.update(MAXPM_DB=old_env))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            cli.run(["--as", "aa", "-q", "add", "a", "a note I write now", "--mine"])
        self.assertIn("reserved for you: no other session takes it", out.getvalue())
        self.assertIn("reserved for aa", out.getvalue())


class Experts(Base):
    """An expert session (#4955): the person talks with it for hours or days; it takes no queue work, and MaximizePM
    never ends it as it ends a worker."""

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "a", path=self.dir.name)
        core.register(self.c, "mark", human=True)
        core.register(self.c, "fab")
        self.ready = self.add("a", "ready work")

    def test_adopt_keeps_the_name_and_go_gives_no_item(self):
        x = core.expert_make(self.c, "fab", "the maxpm options paper", "a", "fab")
        self.assertEqual((x["name"], x["topic"], x["project"], x["state"]), ("fab", "the maxpm options paper", "a", "active"))
        b = core.go(self.c, self.dir.name, "fab")
        self.assertEqual((b["role"], b["item"]), ("expert", None))
        self.assertEqual(core._agent(self.c, "fab")["role"], "expert")
        with self.assertRaisesRegex(RiverError, "expert session: it waits at its prompt"):
            core.wait(self.c, self.dir.name, "fab", sleep=lambda s: None)
        self.assertIsNone(core.waiting_agent_for(self.c, "a", self.ready))
        self.assertEqual(core.capacity(self.c)["agents_active"], 0)
        # The person tells it to take an item: it claims, and stays an expert with its topic.
        core.claim(self.c, self.ready, "fab")
        a = core._agent(self.c, "fab")
        self.assertEqual((a["role"], a["expert_topic"]), ("expert", "the maxpm options paper"))
        b = core.go(self.c, self.dir.name, "fab")
        self.assertEqual((b["role"], [h["id"] for h in b["holds"]]), ("expert", [self.ready]))

    def test_an_expert_is_not_away_or_gone_by_time_and_its_talk_counts(self):
        core.expert_make(self.c, "fab", "topic", "a")
        old = core.iso(core.now() - core.timedelta(days=3))
        self.c.execute("UPDATE agents SET last_seen=?, talked_at=? WHERE name='fab'", (old, old))
        self.assertEqual(core.agent_status(self.c, "fab")["state"], "active")
        core.activity_event(self.c, {"hook_event_name": "UserPromptSubmit", "prompt": "and what about Paperclip?"},
                            {"MAXPM_AGENT": "fab"})
        self.assertNotEqual(core._agent(self.c, "fab")["talked_at"], old)
        core.activity_event(self.c, {"hook_event_name": "Stop"}, {"MAXPM_AGENT": "fab"})
        self.assertEqual(core.activity_watch(self.c), [])

    def test_the_sessions_tab_shows_an_expert_as_talking_not_idle(self):
        from river import sessions
        core.expert_make(self.c, "fab", "the competitor research", "a")
        a = core.agent_status(self.c, "fab")
        self.assertEqual(sessions.state(self.c, a, "idle"), ("expert", "talks with you"))
        self.assertEqual(sessions.state(self.c, a, None), ("expert", "talks with you"))
        self.assertEqual(sessions.state(self.c, a, "working")[0], "working")

    def test_expert_idle_end_ends_it_and_tells_the_senders(self):
        core.expert_make(self.c, "fab", "topic", "a")
        core.send(self.c, "question", "what did Beads do?", to="fab", actor="mark")
        self.assertEqual(core.end_quiet_experts(self.c), {})
        later = core.now() + core.timedelta(days=7, minutes=1)
        with mock.patch.object(core, "now", lambda: later):
            self.assertEqual(list(core.end_quiet_experts(self.c)), ["fab"])
        self.assertIsNone(self.c.execute("SELECT 1 FROM agents WHERE name='fab'").fetchone())
        self.assertTrue(any("fab ended" in m["body"] for m in core.inbox(self.c, "mark")))
        # Its topic and project stay in the history, for maxpm expert start fab --resume.
        self.assertEqual(core.expert_topic_of(self.c, "fab"), ("topic", "a"))
        core.config_set(self.c, "expert_idle_end", "0s")
        core.expert_make(self.c, "fab", "topic", "a")
        with mock.patch.object(core, "now", lambda: later + core.timedelta(days=90)):
            self.assertEqual(core.end_quiet_experts(self.c), {})

    def test_who_may_end_an_expert(self):
        core.expert_make(self.c, "fab", "topic", "a")
        core.register(self.c, "worker1")
        with self.assertRaisesRegex(RiverError, "an agent may not end the expert fab"):
            core.expert_end(self.c, "fab", "worker1")
        core.claim(self.c, self.ready, "fab")
        # An end gives back what it holds (#4959): the item is open again.
        out = core.expert_end(self.c, "fab", "mark")
        self.assertEqual((out["ended"], out["released"], out["done"]), ("fab", [self.ready], []))
        self.assertEqual(core._item(self.c, self.ready)["status"], "open")
        with self.assertRaisesRegex(RiverError, "not registered"):
            core.expert_end(self.c, "fab", "mark")
        with self.assertRaisesRegex(RiverError, "needs a topic"):
            core.expert_make(self.c, "fab", "  ", "a")

    def test_a_test_tier_task_goes_only_to_a_session_started_for_it(self):
        """#5565: the target session of a live test waits at its prompt by design; no worker takes its task."""
        core.register(self.c, "w1")
        t = core.item_add(self.c, "a", "test target: stay at your prompt", 0, tier="test")["id"]
        self.assertEqual(core._item(self.c, t)["tier"], "test")
        with self.assertRaisesRegex(RiverError, "a test-tier task is for an agent session"):
            core.item_add(self.c, "a", "x", doer="human", tier="test")
        self.assertEqual([i["id"] for i in core.next_item(self.c, "a", actor="w1", limit=5)], [self.ready])
        with self.assertRaisesRegex(RiverError, "test-tier task, the target of a live test"):
            core.claim(self.c, t, "w1")
        self.assertIsNone(core.waiting_agent_for(self.c, "a", t))
        core.register(self.c, "new1")
        core.push(self.c, t, "new1", "launched for it", "mark")
        core.claim(self.c, t, "new1")
        self.assertEqual(core._item(self.c, t)["assignee"], "new1")
        self.assertNotEqual(core._agent(self.c, "new1")["role"], "expert")  # a test session is no expert

    def test_an_expert_tier_task_goes_only_to_a_session_started_for_it(self):
        """#4959: expert is a tier of a task. A worker never gets it from go, next, wait, or Dispatch; the session
        started for it (pushed: launch --item) takes it and is an expert from its start; it keeps the task while the
        person talks with it; the end gives it back, or closes it with done."""
        core.register(self.c, "w1")
        t = core.item_add(self.c, "a", "talk through the Teams design", 0, tier="expert")["id"]
        self.assertEqual(core._item(self.c, t)["tier"], "expert")
        with self.assertRaisesRegex(RiverError, "tier is normal or expert or test"):
            core.item_add(self.c, "a", "x", tier="guru")
        with self.assertRaisesRegex(RiverError, "a person's item has no tier"):
            core.item_add(self.c, "a", "x", doer="human", tier="expert")
        self.assertEqual([i["id"] for i in core.next_item(self.c, "a", actor="w1", limit=5)], [self.ready])
        self.assertEqual(core.go(self.c, self.dir.name, "w1")["item"]["id"], self.ready)
        core.release(self.c, self.ready, "w1")
        with self.assertRaisesRegex(RiverError, "expert-tier task: a person talks it through"):
            core.claim(self.c, t, "w1")
        self.assertIsNone(core.waiting_agent_for(self.c, "a", t))
        # Capacity still counts it as ready work, so Start and the manager start a session for it.
        self.assertIn(t, core.capacity(self.c)["ready_for_agents"])
        # launch --item: a new session, the task pushed to it; its first go takes it, and it is an expert.
        core.register(self.c, "new1")
        core.push(self.c, t, "new1", "launched for it", "mark")
        b = core.go(self.c, self.dir.name, "new1", focus=f"item:{t}")
        self.assertEqual((b["role"], b["item"], [h["id"] for h in b["holds"]]), ("expert", None, [t]))
        a = core._agent(self.c, "new1")
        self.assertEqual((a["role"], a["expert_topic"]), ("expert", "talk through the Teams design"))
        self.assertEqual(core.expert_view(self.c, a)["task"], t)
        # Days of talk: the lease does not run out.
        later = core.now() + core.timedelta(days=2)
        with mock.patch.object(core, "now", lambda: later):
            core.activity(self.c, "mark")
        self.assertEqual(core._item(self.c, t)["assignee"], "new1")
        # The end gives the task back; --resume (expert_task) finds it; --done closes it.
        core.expert_end(self.c, "new1", "mark")
        self.assertEqual(core._item(self.c, t)["status"], "open")
        self.assertEqual(core.expert_task(self.c, "new1")["id"], t)
        core.expert_make(self.c, "new1", "talk through the Teams design", "a")
        core.claim(self.c, t, "new1")
        self.assertEqual(core.expert_end(self.c, "new1", "mark", "decided", done_task=True)["done"], [t])
        self.assertEqual(core._item(self.c, t)["status"], "done")
        # A task can change tier.
        core.item_edit(self.c, self.ready, actor="mark", tier="expert")
        self.assertEqual(core._item(self.c, self.ready)["tier"], "expert")
        core.item_edit(self.c, self.ready, actor="mark", tier="normal")
        self.assertIsNone(core._item(self.c, self.ready)["tier"])

    def test_a_request_for_an_expert_adds_an_expert_tier_task_and_tells_the_manager_how_to_start_it(self):
        """#5036: a session asked the manager for 'a fable expert' in free text; the manager added a plain task and
        launched a worker. maxpm expert request adds the expert-tier task itself (found during the requester's item,
        with the model asked for) and alerts the manager with the exact expert start line; a launch of that task
        (push and go) gives an expert."""
        from river import cli
        core.register(self.c, "w1")
        core.register(self.c, "mgr")
        self.c.execute("UPDATE agents SET role='manager' WHERE name='mgr'")
        self.c.commit()
        core.claim(self.c, self.ready, "w1")
        with self.assertRaisesRegex(RiverError, "--for names the person"):
            core.expert_request(self.c, "review the copy", "w1", "fable", person="mgr")
        r = core.expert_request(self.c, "review the copy of the landing page", "w1", "fable", person="mark",
                                context="Rules: keep the six prompts word for word.")["expert_request"]
        it = core._item(self.c, r["item"])
        self.assertEqual((it["tier"], it["model"], it["max_model"], it["found_during"], r["name"], r["to"]),
                         ("expert", "fable", "fable", self.ready, "a-expert", "mgr"))
        self.assertIn("Expert request of w1 for mark, found during", it["context"])
        self.assertIn("keep the six prompts", it["context"])
        self.assertEqual(r["start"], f"maxpm expert start a-expert --item {r['item']}")
        alert = core.message_show(self.c, r["message"])
        self.assertEqual((alert["kind"], alert["to_agent"]), ("alert", "mgr"))
        self.assertIn(f"EXPERT REQUEST: review the copy of the landing page (fable), for mark", alert["body"])
        self.assertIn(r["start"], alert["body"])
        # A worker never takes it; the session launched for it is an expert.
        with self.assertRaisesRegex(RiverError, "expert-tier task"):
            core.claim(self.c, r["item"], "w1")
        core.register(self.c, "a-1f2e")
        core.push(self.c, r["item"], "a-1f2e", "launched for it", "mgr")
        self.assertEqual(core.go(self.c, self.dir.name, "a-1f2e", focus=f"item:{r['item']}")["role"], "expert")
        # The next request takes the next free name; with no manager, the person hears it.
        self.c.execute("UPDATE agents SET role=NULL WHERE name='mgr'")
        self.c.commit()
        core.register(self.c, "a-expert")
        r2 = core.expert_request(self.c, "a second look", "w1", "opus")["expert_request"]
        self.assertEqual((r2["name"], r2["to"]), ("a-expert-2", "mark"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.render_expert({"expert_request": r2})
        self.assertIn(f"it starts as an expert: maxpm expert start a-expert-2 --item {r2['item']}", out.getvalue())
        with self.assertRaisesRegex(RiverError, "say which model"):
            core.expert_request(self.c, "no model", "mark", project="a")

    def test_adopt_makes_the_held_task_expert_tier(self):
        from river import cli
        old_env = os.environ.get("MAXPM_DB")
        os.environ["MAXPM_DB"] = self.path
        self.addCleanup(lambda: os.environ.pop("MAXPM_DB") if old_env is None else os.environ.update(MAXPM_DB=old_env))

        def run(*words):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                cli.run(list(words))
            return out.getvalue()
        with self.assertRaisesRegex(RiverError, "an expert has a task"):
            run("--as", "fab", "-q", "expert", "adopt")
        core.claim(self.c, self.ready, "fab")
        self.assertIn("fab is now an expert on: ready work", run("--as", "fab", "-q", "expert", "adopt"))
        self.assertEqual(core._item(self.c, self.ready)["tier"], "expert")
        x = run("-q", "add", "a", "a design talk", "--tier", "expert")
        self.assertIn("design talk", x)

    def test_cli_adopt_notes_list_and_end(self):
        from river import cli
        old_env = os.environ.get("MAXPM_DB")
        os.environ["MAXPM_DB"] = self.path
        self.addCleanup(lambda: os.environ.pop("MAXPM_DB") if old_env is None else os.environ.update(MAXPM_DB=old_env))

        def run(*words):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                cli.run(list(words))
            return out.getvalue()
        self.assertIn("fab is now an expert on: research", run("--as", "fab", "-q", "expert", "adopt", "--topic",
                                                               "research", "--project", "a"))
        self.assertEqual(core.expert_view(self.c, core._agent(self.c, "fab"))["topic"], "research")
        self.assertIn("EXPERT on: research", run("--as", "fab", "-q", "go", "--project", "a"))
        self.assertIn("no notes yet", run("--as", "fab", "-q", "expert", "notes", "fab"))
        f = os.path.join(self.dir.name, "notes.md")
        with open(f, "w", encoding="utf-8") as fh:
            fh.write("# Notes\n\n- Beads sorts by its own priority\n")
        self.assertIn("Beads sorts by its own priority", run("--as", "fab", "-q", "expert", "notes", "fab", "--put", f))
        self.assertIn("research", run("-q", "expert", "list"))
        self.assertIn("fab (expert, takes no queue work, active)", run("-q", "who"))
        self.assertIn("ended the expert fab", run("--as", "mark", "-q", "expert", "end", "fab"))
        self.assertIn("No experts", run("-q", "expert", "list"))


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


    def test_ok_all_marks_many_takeovers_seen_in_one_call(self):
        # 811 takeovers on the You view (#5365): OK all is one call, and it leaves a takeover it was not given.
        with core.tx(self.c):
            pid = self.c.execute("SELECT id FROM projects WHERE name='a'").fetchone()[0]
            for i in range(811):
                self.c.execute("INSERT INTO items (project_id, title, rank, doer, status, takeover_by, takeover_kind, takeover_at, "
                               "created_at) VALUES (?, ?, 0, 'ai', 'done', 'ag', 'done', ?, ?)",
                               (pid, f"t{i}", core.iso(core.now()), core.iso(core.now())))
        ids = [t["id"] for t in core.takeovers(self.c)]
        self.assertEqual(len(ids), 811)
        core.takeover(self.c, self.h, "came after the page drew the list", "ag")
        self.assertEqual(core.takeovers_seen(self.c, ids + [self.h * 1000, ids[0]], "mark"), {"seen": 811})
        self.assertEqual([t["id"] for t in core.takeovers(self.c)], [self.h])
        self.assertEqual(core.takeovers_seen(self.c, [], "mark"), {"seen": 0})


class Upgrade(Base):
    """connect() runs SCHEMA and the migrations once for each schema id, not on every command (#5396)."""

    def test_a_connect_writes_nothing_while_another_command_holds_the_write_lock(self):
        path = self.c.execute("PRAGMA database_list").fetchone()[2]
        holder = sqlite3.connect(path, isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")
        t = time.time()
        c = core.connect(path)
        self.addCleanup(c.close)
        self.assertLess(time.time() - t, 1)  # before #5396 it waited busy_timeout (10s), then died
        self.assertTrue(c.execute("SELECT 1 FROM meta WHERE key=?", ("schema:" + core.schema_print(),)).fetchone())
        holder.execute("ROLLBACK")

    def test_a_new_schema_id_runs_the_migrations_again(self):
        path = self.c.execute("PRAGMA database_list").fetchone()[2]
        self.c.execute("INSERT INTO settings(scope, key, value) VALUES ('global', 'launch_in', 'tab')")
        core.connect(path).close()  # the same schema id: nothing runs
        self.assertTrue(self.c.execute("SELECT 1 FROM settings WHERE key='launch_in'").fetchone())
        self.c.execute("DELETE FROM meta WHERE key LIKE 'schema:%'")
        core.connect(path).close()
        self.assertIsNone(self.c.execute("SELECT 1 FROM settings WHERE key='launch_in'").fetchone())
        self.assertEqual(self.c.execute("SELECT count(*) FROM meta WHERE key LIKE 'schema:%'").fetchone()[0], 1)

    def test_a_long_transaction_names_itself_in_locked_log(self):
        self.addCleanup(setattr, core, "LOCK_SLOW", core.LOCK_SLOW)
        core.LOCK_SLOW = 0
        core.project_add(self.c, "a")
        core.item_add(self.c, "a", "anything")
        with open(os.path.join(self.dir.name, "locked.log"), encoding="utf-8") as f:
            line = f.read().splitlines()[-1]
        self.assertRegex(line, r"held=\d+\.\ds pid=\d+ .* thread=MainThread in item_add$")

    def test_needs_you_and_a_command_take_no_write_lock_when_nothing_changes(self):
        core.project_add(self.c, "a")
        core.register(self.c, "mark", human=True)
        core.item_add(self.c, "a", "pick a color", doer="human")
        self.assertEqual(len(core.needs_you(self.c)), 1)
        holder = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")
        self.c.execute("PRAGMA busy_timeout=200")
        t = time.time()
        self.assertEqual(len(core.needs_you(self.c)), 1)  # before #5396 it waited for the lock, 2-4 s each time
        core.sync_needs_you_now(self.c)
        self.assertLess(time.time() - t, 0.5)
        holder.execute("ROLLBACK")

    def test_a_sync_that_meets_a_newer_write_runs_again_in_a_tx(self):
        calls = []
        real = core.sync_needs_you

        def once_locked(conn):
            calls.append(conn.in_transaction)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
            return real(conn)
        with mock.patch.object(core, "sync_needs_you", once_locked):
            core.sync_needs_you_now(self.c)
        self.assertEqual(calls, [True, True])
        self.assertFalse(self.c.in_transaction)

    def test_the_schema_id_is_the_same_in_every_process(self):
        out = {subprocess.run([sys.executable, "-c", "from river import core; print(core.schema_print())"],
                              capture_output=True, text=True, check=True).stdout.strip() for _ in range(3)}
        self.assertEqual(out, {core.schema_print()})

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
        # The agent that goes through the list (#5103): one at a time, read first, record each answer.
        for part in ("Never two in one message", "read it first", "SendUserFile", "My recommendation", "--found-during"):
            self.assertIn(part, allp)
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
        core.config_set(self.c, "notify_open", "agent")  # #4673: the page is the default
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
        self.assertTrue(notify.compose(self.c, [row(other)])[2].endswith(f"/?item={other}"))  # falls back to the page
        self.assertTrue(notify.compose(self.c, [row(step), row(other)])[2].startswith("http://127.0.0.1:"))
        # A batch from one session opens that session.
        self.assertEqual(notify.compose(self.c, [row(step), row(other, q["id"])])[2], "https://claude.ai/code/session_01Abc")


class NotifyOpen(Base):
    """notify_open (#4673): a tap opens the MaximizePM page at the thing, or the agent's own app."""

    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, {"MAXPM_DB": self.path})  # relay.json beside the test queue
        p.start()
        self.addCleanup(p.stop)
        core.project_add(self.c, "a")
        core.register(self.c, "cc")  # Claude Code with Remote Control
        core.register(self.c, "cx")  # Codex: no task link
        core.register(self.c, "plain")  # Claude Code without Remote Control
        core.register(self.c, "mark", human=True)
        core.record_session_url(self.c, "cc", "https://claude.ai/code/session_01Abc")
        self.c.execute("UPDATE agents SET agent_type='codex' WHERE name='cx'")
        self.c.execute("UPDATE agents SET agent_type='claude-code' WHERE name IN ('cc','plain')")
        self.c.commit()
        self.items = {a: core.item_add(self.c, "a", f"step of {a}", doer="human", actor=a)["id"] for a in ("cc", "cx", "plain")}
        self.q = core.send(self.c, "question", "which port?", "mark", None, None, "cx")["id"]

    def url(self, *rows):
        from river import notify
        return notify.compose(self.c, [{"summary": "s", "item_id": i, "message_id": m, "human": h} for i, m, h in rows])[2]

    def test_agent_opens_each_agent_type_else_the_page(self):
        self.assertEqual(core.setting(self.c, "notify_open"), "maxpm")
        core.config_set(self.c, "notify_open", "agent")
        self.assertEqual(self.url((self.items["cc"], None, None)), "https://claude.ai/code/session_01Abc")
        self.assertEqual(self.url((self.items["cx"], None, None)), "https://chatgpt.com/codex")
        self.assertEqual(self.url((None, self.q, "mark")), "https://chatgpt.com/codex")
        plain = self.items["plain"]
        self.assertRegex(self.url((plain, None, None)), rf"^http://127\.0\.0\.1:\d+/\?item={plain}$")
        # A batch: one app behind every row opens it; else the Board, where Needs you is.
        self.assertEqual(self.url((self.items["cx"], None, None), (None, self.q, None)), "https://chatgpt.com/codex")
        self.assertRegex(self.url((self.items["cc"], None, None), (self.items["cx"], None, None)), r"^http://127\.0\.0\.1:\d+/$")

    def test_maxpm_opens_the_page_at_the_thing_per_person(self):
        core.config_set(self.c, "notify_open", "agent")
        core.config_set(self.c, "notify_open", "maxpm", agent="mark")
        cc = self.items["cc"]
        self.assertTrue(self.url((cc, None, "mark")).endswith(f"/?item={cc}&as=mark"))
        self.assertTrue(self.url((None, self.q, "mark")).endswith("/sessions/cx?as=mark"))  # the ! dialog there
        self.assertRegex(self.url((cc, None, "mark"), (None, self.q, "mark")), r"^http://127\.0\.0\.1:\d+/\?as=mark$")
        # Somebody else (or anyone) keeps the global value.
        self.assertEqual(self.url((cc, None, None)), "https://claude.ai/code/session_01Abc")
        core.config_set(self.c, "notify_open", "maxpm")
        self.assertTrue(self.url((cc, None, None)).endswith(f"/?item={cc}"))
        with self.assertRaisesRegex(RiverError, "maxpm .* or agent"):
            core.config_set(self.c, "notify_open", "claude")

    def test_the_phone_opens_the_page_through_the_relay_else_ntfy_click(self):
        from river import notify, relay
        core.config_set(self.c, "ntfy_topic", "maxpm-test")
        core.config_set(self.c, "notify_channels", "ntfy")
        seen = []

        class Resp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def send(url):
            with mock.patch("urllib.request.urlopen", lambda req, timeout: seen.append(req) or Resp()):
                notify.ADAPTERS["ntfy"](self.c)("t", "b", url)
            return seen[-1].get_header("Click")

        local = "http://127.0.0.1:8765/sessions/cx?as=mark"
        self.assertEqual(send(local), "https://claude.ai/code")
        self.assertEqual(seen[-1].get_header("Icon"), "https://maximizepm.com/apple-touch-icon.png")  # #4705
        self.assertIn("ntfy_click (https://claude.ai/code)", notify.status(self.c)["phone"])
        self.assertIn("maxpm connect", notify.phone_note(self.c))
        with open(relay.config_path(), "w", encoding="utf-8") as f:
            json.dump({"url": "https://relay.example.org", "token": "tok",
                       "tunnel": "https://mark-ab3cd9.relay.example.org"}, f)
        self.assertEqual(send(local), "https://mark-ab3cd9.relay.example.org/sessions/cx?as=mark")
        self.assertEqual(send("http://127.0.0.1:8765/"), "https://mark-ab3cd9.relay.example.org/")
        self.assertEqual(send("https://chatgpt.com/codex"), "https://chatgpt.com/codex")
        self.assertIn("https://mark-ab3cd9.relay.example.org/", notify.status(self.c)["phone"])

    def test_setup_asks_and_status_shows_it(self):
        from river import cli, notify
        out = notify.setup_ntfy(self.c, actor="mark", open_="agent")
        self.assertEqual(core.setting(self.c, "notify_open", agent="mark"), "agent")
        self.assertEqual(core.setting(self.c, "notify_open"), "maxpm")
        text = "\n".join(out["subscribe"])
        self.assertIn("opens the agent's own app (claude.ai/code, chatgpt.com/codex) (notify_open agent for mark)", text)
        self.assertIn("maxpm config set notify_open maxpm --agent mark", text)
        st = notify.status(self.c)
        self.assertEqual((st["open"], st["open_people"]), ("maxpm", {"mark": "agent"}))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.render(cli.build_parser().parse_args(["notify", "status"]), st)
        self.assertIn("a tap opens the MaximizePM page", buf.getvalue())
        self.assertIn("mark: agent", buf.getvalue())


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
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('maxpm = "river.client:main"', text)  # serve runs it when it can, else river.cli (#5525)
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
            self.assertEqual(new.read_text(encoding="utf-8"), cli.AGENT_SNIPPET)
            notes.write_text("# Notes\n", encoding="utf-8")
            self.assertIn("added", cli._append_block(notes))
            self.assertEqual(notes.read_text(encoding="utf-8"), "# Notes\n\n" + cli.AGENT_SNIPPET)

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
            self.assertEqual((c.read_text(encoding="utf-8"), a.read_text(encoding="utf-8")), ("@AGENTS.md\n", cli.AGENT_SNIPPET))
            self.assertEqual(cli.instructions_layout(d), "shared")
            cli.setup_instructions(d)  # again: nothing changes
            self.assertEqual((c.read_text(encoding="utf-8"), a.read_text(encoding="utf-8")), ("@AGENTS.md\n", cli.AGENT_SNIPPET))
        with tempfile.TemporaryDirectory() as d:
            c, a = Path(d, "CLAUDE.md"), Path(d, "AGENTS.md")
            c.write_text("# Rules\n\nPush to main deploys.\n", encoding="utf-8")
            a.write_text(cli.AGENT_SNIPPET, encoding="utf-8")  # what an older maxpm init made
            self.assertEqual(cli.instructions_layout(d), "claude_only")
            lines = cli.setup_instructions(d)  # no choice given: both get the block, and the choice is named
            self.assertIn("maxpm init --move", lines[-1])
            self.assertIn("Push to main", c.read_text(encoding="utf-8"))
            cli.setup_instructions(d, move=True)
            self.assertEqual(c.read_text(encoding="utf-8"), "@AGENTS.md\n")
            text = a.read_text(encoding="utf-8")
            self.assertIn("Push to main deploys", text)
            self.assertEqual(text.count("## Work queue"), 1)
            self.assertEqual(cli.instructions_layout(d), "shared")
        with tempfile.TemporaryDirectory() as d:
            c, a = Path(d, "CLAUDE.md"), Path(d, "AGENTS.md")
            c.write_text("claude rules\n", encoding="utf-8"); a.write_text("codex rules\n", encoding="utf-8")
            self.assertEqual(cli.instructions_layout(d), "both")
            lines = cli.setup_instructions(d, move=True)  # two sets of rules: river never merges them
            self.assertIn("different rules", lines[-1])
            self.assertIn(cli.AGENT_SNIPPET, c.read_text(encoding="utf-8")); self.assertIn(cli.AGENT_SNIPPET, a.read_text(encoding="utf-8"))
            self.assertIn("claude rules", c.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as d:
            c, a = Path(d, "CLAUDE.md"), Path(d, "AGENTS.md")
            c.write_text("claude rules\n", encoding="utf-8")
            cli.setup_instructions(d, move=False)  # keep apart
            self.assertEqual((c.read_text(encoding="utf-8").count("## Work queue"), a.read_text(encoding="utf-8")), (1, cli.AGENT_SNIPPET))
        with tempfile.TemporaryDirectory() as d:
            c, a = Path(d, "CLAUDE.md"), Path(d, "AGENTS.md")
            c.write_text("@AGENTS.md\n", encoding="utf-8")
            a.write_text(cli.OLD_SNIPPET + "\n## Rules\n\nTest first.\n", encoding="utf-8")  # an init before #4978: the 'maxpm plan' line
            lines = cli.setup_instructions(d)  # init again: the old block becomes the current one, the rules stay
            self.assertIn("AGENTS.md: the work queue block is current again", lines[0])
            self.assertEqual(a.read_text(encoding="utf-8"), cli.AGENT_SNIPPET + "\n## Rules\n\nTest first.\n")
            self.assertEqual(cli.setup_instructions(d)[0], "AGENTS.md: already has the work queue block")


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
        for n in ("dev", "rev", "rev0", "ops"):  # rev0 reviews the first release; a review starts in a new session
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
            core.claim(self.c, rv, "rev0")
            core.review_pass(self.c, rv, "fine", "rev0", self.dir.name)
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

    def test_the_session_that_prepared_the_pin_or_wrote_a_commit_does_not_review(self):
        # #5385: a toolscaledcore session prepared the pin and then got the review of that release.
        core.register(self.c, "w9")
        core.register(self.c, "w8")
        core.register(self.c, "boss")
        core._set_role_note(self.c, "boss", "manager", None)
        core.target_cut(self.c, "web", rev="abc123", actor="ops")  # the owner cuts the release and pins abc123
        self.assertIn("ops", core.release_authors(self.c, self.review))
        core.target_cut(self.c, "web", rev="def456", actor="boss")  # the manager moves the pin
        self.assertIn("boss", core.release_authors(self.c, self.review))
        # A commit of the release carries the link of w8's session (its Claude-Session trailer).
        link = "https://claude.ai/code/session_01AbCdEf"
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET session_url=? WHERE name='w8'", (link,))
        core.config_set(self.c, "release_commits", f"printf 'Fix the form\\n\\nClaude-Session: {link}\\n'")
        core._RELEASE_COMMITS.clear()
        self.addCleanup(core._RELEASE_COMMITS.clear)
        self.assertEqual(core.release_commit_sessions(self.c, core._item(self.c, self.review)), {link})
        authors = core.release_authors(self.c, self.review)
        self.assertTrue({"dev", "ops", "boss", "w8"} <= authors, authors)
        self.assertNotIn("w9", authors)
        self.assertIn("worked on what release review", core.author_refusal(self.c, core._item(self.c, self.review), "w8"))
        # A reviewer whose claim cut the release is no author of it.
        core.claim(self.c, self.review, "w9")
        self.assertNotIn("w9", core.release_authors(self.c, self.review))

    def test_a_reviewer_that_takes_the_review_cuts_the_release_but_is_no_author(self):
        core.register(self.c, "w9")
        core.claim(self.c, self.review, "w9")
        self.assertTrue(core._item(self.c, self.deploy)["cut_at"])
        self.assertNotIn("w9", core.release_authors(self.c, self.review))

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

    def test_a_review_starts_in_a_new_session(self):
        # #4968, mark: 'all reviews should start with a clean context reviewer'. A session that held a task gets no
        # review from go, next, wait, claim, a push, a queue, or Dispatch's waiting session; a fresh one does.
        x = self.add("site", "other work")
        core.claim(self.c, x, "rev")
        core.done(self.c, x, "did it", "rev")
        why = core.reviewer_refusal(self.c, core._item(self.c, self.review), "rev")
        self.assertIn(f"this session held #{x} before", why)
        self.assertNotEqual(core.go(self.c, self.dir.name, "rev")["role"], "reviewer")
        b = core.go(self.c, self.dir.name, "rev", role="reviewer")
        self.assertEqual(b["role"], "idle")
        self.assertIn("starts in a new session", b["why"])
        skipped = []
        self.assertEqual(core.next_item(self.c, None, claim=True, actor="rev", limit=5, skipped=skipped), [])
        self.assertIn((self.review, "a review starts in a new session; this one held a task"),
                      [(i["id"], i["why"]) for i in skipped])
        self.assertIsNone(core._work_for(self.c, "rev", ["site"], role="reviewer"))
        core.register(self.c, "mark", human=True)
        for f in (lambda: core.claim(self.c, self.review, "rev"),
                  lambda: core.push(self.c, self.review, "rev", actor="ops"),
                  lambda: core.queue_add(self.c, "rev", self.review, actor="mark")):
            with self.assertRaisesRegex(RiverError, "starts in a new session"):
                f()
        core.wait(self.c, self.dir.name, "rev", step="0s", sleep=lambda s: None)
        self.assertIsNone(core.waiting_agent_for(self.c, "site", self.review))
        # A session that holds a task now (a give moved it there) is used too.
        y = self.add("site", "given work")
        core.register(self.c, "fresh")
        core.claim(self.c, y, "ops")
        core.give(self.c, y, "fresh", actor="ops")
        self.assertIn(f"held #{y}", core.reviewer_refusal(self.c, core._item(self.c, self.review), "fresh"))
        # A new session takes the review; after it, the session takes any work, and fixes.
        core.register(self.c, "rev2")
        core.wait(self.c, self.dir.name, "rev2", step="0s", sleep=lambda s: None)
        self.assertEqual(core.waiting_agent_for(self.c, "site", self.review), "rev2")
        b = core.go(self.c, self.dir.name, "rev2")
        self.assertEqual((b["role"], b["item"]["id"]), ("reviewer", self.review))
        fix = core.review_fail(self.c, self.review, ["escape the form input"], "XSS", actor="rev2")["fixes"][0]["id"]
        b = core.go(self.c, self.dir.name, "rev2")
        self.assertEqual((b["role"], b["item"]["id"]), ("worker", fix))
        core.done(self.c, fix, "fixed", "rev2")
        # The review comes back: the used session does not get it; the same name in a new session does.
        self.assertTrue(core.annotate(self.c)[self.review]["ready"])
        self.assertNotEqual((core.go(self.c, self.dir.name, "rev2").get("item") or {}).get("id"), self.review)
        self.assertNotIn("rev2", [core._item(self.c, self.review)["assignee"]])
        core.unregister(self.c, "rev")
        core.register(self.c, "rev")
        b = core.go(self.c, self.dir.name, "rev")
        self.assertEqual((b["role"], b["item"]["id"]), ("reviewer", self.review))
        # A person may take a review at any time.
        core.release(self.c, self.review, actor="rev")
        core.claim(self.c, self.add("site", "mark's task", doer="human"), "mark")
        self.assertIsNone(core.reviewer_refusal(self.c, core._item(self.c, self.review), "mark"))

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
        with open(gate, "w", encoding="utf-8") as f:
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
        with open(pidfile, encoding="utf-8") as f:
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
        # The next release cannot be cut while this one runs; with a reason it can start sooner. A --rev meanwhile
        # moves the commit of the running release (#5404).
        with self.assertRaisesRegex(RiverError, "runs now.*release-now web"):
            core.target_cut(self.c, "web", None, "ops")
        self.assertEqual(core.target_cut(self.c, "web", "abc9999", "ops")["moved"], "abc9999")
        self.assertIsNone(core._item(self.c, nxt["id"])["cut_at"])
        r = core.release_now(self.c, "web", "a fix of a production defect", "mark")
        self.assertEqual((r["waits"], r["item"]["ready"]), (False, True))
        res = core.target_cut(self.c, "web", "def5678", "mark")
        self.assertEqual((res["cut"]["id"], res.get("moved"), core._item(self.c, self.deploy)["cut_rev"]),
                         (nxt["id"], None, "abc9999"))

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
        core.item_add(self.c, "a", "x", actor="t")
        self.assertEqual(core.go(self.c, self.dir.name, "w")["trackers"], {"a": "github o/r via gh"})
        core.project_tracker(self.c, "a", "none", "t")
        self.assertEqual(core.project_tracker(self.c, "a", "none", "t")["tracker"], "")  # clearing twice is fine


class ProjectArchive(Base):
    """#5197: archive a project, list the archived ones, and restore one."""

    def test_archive_takes_its_items_out_and_restore_brings_them_back(self):
        for n in ("a", "b", "c"):
            core.project_add(self.c, n)
        x = self.add("b", "x")
        self.add("b", "y")
        core.register(self.c, "ag")
        core.claim(self.c, x, "ag")
        r = core.project_archive(self.c, "b", "mark")
        self.assertEqual((r["name"], r["archived"], r["open_items"], r["in_progress"], r["holders"]),
                         ("b", True, 2, 1, ["ag"]))
        self.assertEqual([(p["name"], p["rank"]) for p in core.project_list(self.c)], [("a", 1), ("c", 2)])
        self.assertEqual(core.ready_list(self.c), [])
        st = core.state(self.c)
        self.assertNotIn("b", [i["project"] for i in st["items"]])
        (arch,) = st["archived_projects"]
        self.assertEqual((arch["name"], arch["open_items"], arch["done_items"]), ("b", 2, 0))
        self.assertTrue(arch["archived_at"])
        with self.assertRaisesRegex(RiverError, "archived already; .*maxpm project restore b"):
            core.project_archive(self.c, "b")
        r = core.project_restore(self.c, "b", "mark")
        self.assertEqual((r["archived"], r["rank"]), (False, 3))
        self.assertEqual([p["name"] for p in core.project_list(self.c)], ["a", "c", "b"])
        self.assertEqual(core.project_archived(self.c), [])
        self.assertIn("y", [a["title"] for a in core.ready_list(self.c)])
        with self.assertRaisesRegex(RiverError, "not archived"):
            core.project_restore(self.c, "b")
        self.assertEqual([e["change"] for e in core.recent_events(self.c, 2)], ["project b restored", "project b archived"])

    def test_the_cli_archives_lists_and_restores(self):
        from river import cli
        core.project_add(self.c, "a")
        self.add("a", "x")
        self.c.close()
        os.environ["MAXPM_DB"], old = self.path, os.environ.get("MAXPM_DB")
        self.addCleanup(lambda: os.environ.__setitem__("MAXPM_DB", old) if old else os.environ.pop("MAXPM_DB", None))

        def run(*words):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                cli.run(["-q", *words])
            return out.getvalue()
        self.assertIn("project a is archived: its 1 open item left the queue", run("project", "archive", "a"))
        self.assertIn("a  (1 open, 0 done; archived", run("project", "list", "--archived"))
        self.assertIn("project a is back, at rank 1", run("project", "unarchive", "a"))
        self.assertIn("(no archived projects)", run("project", "list", "--archived"))
        self.c = core.connect(self.path)


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
        self.c.execute("DELETE FROM meta WHERE key LIKE 'schema:%'")  # an old queue has no schema id of this code (#5396)
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

    def test_a_person_typing_ends_a_foreground_wait(self):
        """#5256: Pi (and Codex) has no background commands, so it runs maxpm wait in the foreground, and its CLI
        held a message from the Sessions message box until the wait returned after wait_step (9m). A person's text
        typed into its pane (typed_to) ends such a wait at its next poll; a Claude Code wait runs in the
        background, gets the text at once, and goes on."""
        calls = []

        def typing(_s):  # the person types while the wait sleeps between two polls
            calls.append(1)
            if len(calls) == 1:
                core.typed_to(self.c, "w")
            else:  # no end by the typing: a push ends this wait, and the test sees which came
                core.push(self.c, self.add("a", "pushed"), "w", None, "t")
        for agent_type, result in (("pi", "message"), ("codex", "message"), ("claude-code", "work")):
            with core.tx(self.c):
                self.c.execute("UPDATE agents SET agent_type=?, typed_at=NULL WHERE name='w'", (agent_type,))
            calls.clear()
            res = core.wait(self.c, self.dir.name, "w", step="1h", sleep=typing)
            self.assertEqual(res["result"], result, agent_type)
            if result == "message":
                self.assertEqual((res["why"], len(calls)), ("a person typed to you in your terminal", 1))
                self.assertEqual(core.agent_status(self.c, "w")["role"], "waiting")  # it waits again after its answer
            else:
                pushed = int(res["why"].split()[0][1:])
                core.decline(self.c, pushed, None, "w")
                core.drop(self.c, pushed, "t", "test")
        # Text typed before this wait started was read at the prompt: it ends no later wait.
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET agent_type='pi', typed_at='2026-01-01T00:00:00Z' WHERE name='w'")
        self.assertEqual(self.wait()["result"], "again")

    def test_a_push_that_go_cannot_take_yet_is_no_work(self):
        # #4318: a pushed item that conflicts with another agent's item, or waits on an open prerequisite, made
        # wait return WORK at once while go gave IDLE. It waits now, and wakes when the item gets ready.
        core.register(self.c, "x")
        held, pushed, pre = self.add("b", "held"), self.add("b", "pushed"), self.add("b", "prereq")
        core.dep_add(self.c, pushed, [held], kind="conflicts")
        core.claim(self.c, held, "x")
        core.push(self.c, pushed, "w", None, "t")
        core.inbox(self.c, "w")  # the push alert is read
        self.assertEqual(self.wait()["result"], "again")
        self.assertIsNone(core.go(self.c, self.dir.name, "w").get("item"))  # go gives no item either
        core.done(self.c, held, "o", "x")
        self.assertEqual(self.wait()["why"], f"#{pushed} was pushed to you")
        core.dep_add(self.c, pushed, [pre])
        self.assertEqual(self.wait()["result"], "again")

    def test_end_after_wait_max_releases_goals_and_unregisters(self):
        core.goal_add(self.c, "a", "g")
        core.goal_own(self.c, "g", "w")
        core.config_set(self.c, "wait_max", "0s")
        self.assertEqual(self.wait()["result"], "end")
        self.assertIsNone(self.c.execute("SELECT 1 FROM agents WHERE name='w'").fetchone())
        self.assertIsNone(core.goal_show(self.c, "g")["owner"])

    def test_work_comes_from_a_queue_entry_a_message_and_a_stop(self):
        # #4948: the background wait wakes the session on each of these, as the foreground wait did.
        core.register(self.c, "mark", human=True)
        core.queue_add(self.c, "w", message="look at the logs", actor="mark")
        self.assertIn("your queue has an instruction", self.wait()["why"])
        for e in core.queue_list(self.c, "w")["entries"]:
            core.queue_remove(self.c, "w", f"e{e['entry']}", "w")
        core.send(self.c, "note", "main moved", to="w", actor="mark")
        self.assertEqual(self.wait()["why"], "you have messages (maxpm inbox)")
        core.inbox(self.c, "w")
        core.stop_agent(self.c, "w", "the day ends", "mark")
        self.assertEqual(self.wait()["result"], "stop")

    def test_an_older_background_wait_leaves_quietly(self):
        # #4948: a Claude Code session waits in a background command. A person talks with it, and the session
        # starts a new wait (or takes work) while the first one still runs: the first one ends with "left".
        later = []

        def newer(_s):
            if not later:
                later.append(core.wait(self.c, self.dir.name, "w", step="0s", sleep=lambda s: None))
        res = core.wait(self.c, self.dir.name, "w", step="1h", sleep=newer)
        self.assertEqual((later[0]["result"], res["result"], res["why"]),
                         ("again", "left", "a newer maxpm wait of this session runs"))
        i = self.add("b", "elsewhere")  # not in the folder's project: it wakes no wait
        res = core.wait(self.c, self.dir.name, "w", step="1h", sleep=lambda s: core.claim(self.c, i, "w"))
        self.assertEqual((res["result"], res["why"]), ("left", "this session took work since this wait started"))
        self.assertIsNotNone(self.c.execute("SELECT 1 FROM agents WHERE name='w'").fetchone())

    def test_a_persons_prompt_starts_the_wait_max_clock_again(self):
        # #4948: wait ended (and unregistered) a session while a person talked with it.
        from unittest import mock
        sid = "0b5a3c1e-1111-4222-8333-944455550000"
        core.record_claude_session(self.c, "w", sid)
        clock = [core.now()]
        p = mock.patch.object(core, "now", lambda: clock[0])
        p.start()
        self.addCleanup(p.stop)
        prompt = lambda text: core.activity_event(self.c, {"hook_event_name": "UserPromptSubmit", "session_id": sid,
                                                           "prompt": text}, env={})
        self.assertEqual(self.wait()["result"], "again")
        clock[0] += core.timedelta(minutes=30)
        prompt("why did the build fail?")
        clock[0] += core.timedelta(minutes=16)  # 46m after the first wait, 16m after the talk
        self.assertEqual(self.wait()["result"], "again")
        # The harness's wake for a background task, and a message MaximizePM delivered, are no talk.
        clock[0] += core.timedelta(minutes=20)
        prompt("<task-notification>\n<task-id>b1</task-id>\n</task-notification>")
        prompt("[maxpm note from mark] main moved")
        clock[0] += core.timedelta(minutes=10)
        self.assertEqual(self.wait()["result"], "end")
        self.assertIsNone(self.c.execute("SELECT 1 FROM agents WHERE name='w'").fetchone())

    def test_go_tells_a_claude_code_session_to_wait_in_the_background(self):
        core.register(self.c, "x")
        core.claim(self.c, self.add("a", "held by another"), "x")
        b = core.go(self.c, self.dir.name, "w", agent_type="claude-code")
        self.assertEqual((b["role"], b["wait_background"]), ("idle", True))
        self.assertFalse(core.go(self.c, self.dir.name, "w", agent_type="codex")["wait_background"])

    def test_a_ready_item_wakes_the_session_that_waits_longest_first(self):
        # #5205: one item got ready in a plan that several waiting sessions share (their trees, auto_continue tree);
        # each wait printed WORK, the first go took the item, and every other session got IDLE from go.
        from unittest import mock
        clock = [core.now()]
        p = mock.patch.object(core, "now", lambda: clock[0])
        p.start()
        self.addCleanup(p.stop)
        core.register(self.c, "v")
        wait = lambda who, step="0s": core.wait(self.c, self.dir.name, who, step=step, sleep=tick)

        def tick(_s):
            clock[0] += core.timedelta(seconds=3)
        self.assertEqual(wait("w")["result"], "again")  # w waits first
        clock[0] += core.timedelta(seconds=5)
        self.assertEqual(wait("v")["result"], "again")
        i = self.add("a", "one item")
        self.assertEqual(wait("v")["result"], "again")  # it wakes w, not v
        self.assertEqual(wait("w")["why"], f"#{i} is ready: one item")
        # w does not take it (its session is gone): v's wait takes it once it was ready for WAIT_SHARE.
        start = clock[0]
        res = wait("v", "1m")
        self.assertEqual(res["why"], f"#{i} is ready: one item")
        self.assertGreaterEqual(clock[0] - start, core.WAIT_SHARE)
        self.assertLess(clock[0] - start, core.WAIT_SHARE + core.timedelta(seconds=4))
        # w takes it: v's wait never wakes for it.
        core.claim(self.c, i, "w")
        self.assertEqual(wait("v", "1m")["result"], "again")

    def test_a_waiting_session_that_cannot_take_the_item_does_not_hold_it_back(self):
        core.register(self.c, "v")
        core.config_set(self.c, "auto_continue", "tree")
        own = self.add("a", "w's task")
        core.claim(self.c, own, "w")
        core.done(self.c, own, "o", "w")  # w's tree: what is linked to its task
        self.assertEqual(self.wait()["result"], "again")  # w waits first
        i = self.add("a", "not in w's tree")
        res = core.wait(self.c, self.dir.name, "v", step="0s", sleep=lambda s: None)
        self.assertEqual(res["why"], f"#{i} is ready: not in w's tree")  # w's go would not give it to w
        self.assertIsNone(core._first_waiter(self.c, own))

    def test_capacity_still_warns_about_idle_sessions_that_do_not_wait(self):
        core.register(self.c, "idle1")
        self.wait()
        kinds = {x["kind"]: x["text"] for x in core.capacity(self.c)["advice"]}
        self.assertIn("waiting", kinds)
        self.assertIn("1 session(s) have no work", kinds["too_many"])


class Models(Base):
    """Recommended model and effort per item, min/max limits, and sessions that declare their model (#319)."""

    def setUp(self):
        super().setUp()  # Base points MAXPM_DB at the test's queue: a launch writes its hook file there (#5381)
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
        self.assertFalse(ok)  # an unknown model takes no item with a limit (#4950)
        self.assertIn("not in model_ladder", why)
        self.assertIn("maxpm go --model <name>", why)
        self.assertEqual(core.model_check(ladder, "mystery", None, None), (True, ""))
        # A full CLI id counts as the ladder name in it (#4950).
        for full, name in (("claude-opus-5-5", "opus"), ("claude-opus-5-5[1m]", "opus"),
                           ("us.anthropic.claude-fable-5-2-v1:0", "fable"), ("claude-sonnet-5-5", "sonnet"),
                           ("gpt-6-astra", "astra"), ("gpt-5.4-luna", "luna"), ("Claude-Haiku-5-5", "haiku")):
            self.assertEqual(core._family(ladder, full), core._family(ladder, name), full)
        self.assertEqual(core._family(ladder, "opus-or-fable"), (None, None))  # two names: no guess
        self.assertEqual(core._family(ladder, "gpt5"), (None, None))
        self.assertEqual(core.model_check(ladder, "claude-opus-5-5", "fable", "fable")[0], False)
        self.assertEqual(core.model_check(ladder, "claude-opus-5-5", None, "opus"), (True, ""))
        self.assertEqual(core.model_check(ladder, "claude-fable-5-2", None, "opus")[0], False)
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

    def test_limits_do_not_apply_to_an_agent_of_the_multi_family(self):
        """#5236: OpenCode (Pi, Goose: DESCRIBE['family'] multi) runs a provider/model text, and every item had the
        global default max model, so such an agent could neither start on nor claim any item. Option A of
        docs/agent-adapters.md: the limits apply only to the families of model_ladder. The family decides, not the
        text: a multi model text that holds a ladder name (claude-fable-5) is still not ordered."""
        core.config_set(self.c, "default_max_model", "opus")
        ladder = core.parse_ladder(core.setting(self.c, "model_ladder"))
        ok, why = core.model_check(ladder, "opencode/big-pickle", None, "opus", "multi")
        self.assertTrue(ok)
        self.assertIn("multi family, which model_ladder does not order", why)
        self.assertTrue(core.model_check(ladder, "openrouter/anthropic/claude-fable-5", "fable", "opus", "multi")[0])
        # A known family keeps #4950 and its order.
        self.assertFalse(core.model_check(ladder, "mystery", None, "opus", "claude")[0])
        self.assertFalse(core.model_check(ladder, "fable", None, "opus", "claude")[0])
        x = core.item_add(self.c, "a", "any task")["id"]
        y = core.item_add(self.c, "a", "another task")["id"]
        core.register(self.c, "oc")
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET agent_type='opencode' WHERE name='oc'")
        # A provider/model text is a model name: '/' passes, and its case stays.
        self.assertEqual(core.set_agent_model(self.c, "oc", "openrouter/Qwen/Qwen3-Coder"), "openrouter/Qwen/Qwen3-Coder")
        self.assertEqual(core.set_agent_model(self.c, "oc", "opencode/big-pickle"), "opencode/big-pickle")
        self.assertEqual(core.session_family(self.c, "oc"), "multi")
        self.assertIsNone(core._model_refusal(self.c, core._item(self.c, x), "oc"))
        got = core.next_item(self.c, "a", claim=True, actor="oc")
        self.assertEqual([g["id"] for g in got], [x])
        core.release(self.c, x, "oc")
        core.claim(self.c, y, "oc")
        core.register(self.c, "mark", human=True)
        # The launch: the agent of the launch decides.
        r = core.launch_target(self.c, item=x, agent="OpenCode", model="opencode/big-pickle", actor="mark")
        self.assertEqual(r["model_why"], "chosen for this launch")
        with self.assertRaisesRegex(RiverError, "not in model_ladder"):
            core.launch_target(self.c, item=x, agent="Claude Code", model="my-claude-x", actor="mark")
        core.push(self.c, x, "oc", None, "mark")  # a hand-off applies the same rule
        # A Claude Code session with an unknown model still takes no item with a limit.
        core.set_agent_model(self.c, "s1", "my-claude-x")
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET agent_type='claude-code' WHERE name='s1'")
        core.release(self.c, y, "oc")
        with self.assertRaisesRegex(RiverError, "not in model_ladder"):
            core.claim(self.c, y, "s1")

    def test_push_give_and_queue_refuse_a_session_whose_model_the_limits_exclude(self):
        """#4950: each way to hand an item to a session applies the same limit as claim; before, a push or a
        queue entry held the item for a session that could never claim it, and give moved the lease to it."""
        for n, m in (("weak", "claude-opus-5-5"), ("strong", "fable")):
            core.register(self.c, n)
            core.set_agent_model(self.c, n, m)
        core.register(self.c, "mark", human=True)
        x = core.item_add(self.c, "a", "for fable", models={"min_model": "fable"})["id"]
        with self.assertRaisesRegex(RiverError, "push #%d to weak: needs at least fable" % x):
            core.push(self.c, x, "weak", None, "mark")
        with self.assertRaisesRegex(RiverError, "queue #%d to weak: needs at least fable" % x):
            core.queue_add(self.c, "weak", x, actor="mark")
        self.assertIsNone(core._item(self.c, x)["reserved_for"])
        with self.assertRaisesRegex(RiverError, "needs at least fable"):
            core.next_item(self.c, "a", claim=True, actor="weak") or core.claim(self.c, x, "weak")
        with self.assertRaisesRegex(RiverError, "pick another model"):
            core.launch_target(self.c, item=x, model="claude-opus-5-5", actor="mark")
        core.claim(self.c, x, "strong")
        with self.assertRaisesRegex(RiverError, "give #%d to weak" % x):
            core.give(self.c, x, "weak", "strong")
        self.assertEqual(core._item(self.c, x)["assignee"], "strong")
        core.release(self.c, x, "strong")
        core.push(self.c, x, "strong", None, "mark")  # a session whose model fits
        core.accept(self.c, x, "strong")

    def test_only_an_expert_takes_the_model_of_the_work_it_takes_up(self):
        """#4961: an expert for a design that a Fable session wrote started on the default Opus, so an expert task
        found during, or added before, an item whose work ran on a stronger model gets that model. #4979 (mark: 'we do
        not use fable except when specifically asked and for expert discussions'): a plain item found during or
        added before that work takes the default model and limits."""
        core.config_set(self.c, "default_model", "opus")
        core.config_set(self.c, "default_max_model", "opus")
        design = core.item_add(self.c, "a", "design", models={"max_model": "fable"})["id"]
        core.register(self.c, "fab")
        core.set_agent_model(self.c, "fab", "claude-fable-5-2")
        core.claim(self.c, design, "fab")
        self.assertEqual(core._item(self.c, design)["ran_model"], "fable")
        talk = core.item_add(self.c, "a", "talk it through", found_during=design, tier="expert")
        self.assertEqual((talk["model"], talk["max_model"]), ("fable", "fable"))
        self.assertTrue(any("it continues the work of #%d" % design in e["change"] for e in talk["events"]))
        # Plain items found during or added before the Fable work: the default, and no history line about it.
        for kw in ({"found_during": design}, {"blocks": design, "mode": "keep", "actor": "fab"}):
            it = core.item_add(self.c, "a", "a plain task", **kw)
            raw = core._item(self.c, it["id"])
            self.assertEqual((raw["model"], raw["min_model"], raw["max_model"]), (None, None, None))
            self.assertEqual((it["model"], it["max_model"]), ("opus", "opus"))
            self.assertFalse(any("continues the work" in e["change"] for e in it["events"]))
            self.assertEqual(core.session_model(self.c, raw), ("opus", "the default model (global)"))
        # A plain item that a Fable session claimed once: its next session takes the default, not Fable.
        self.assertEqual(core.session_model(self.c, core._item(self.c, design)), ("opus", "the default model (global)"))
        # The adder's own choice wins, for an expert task too.
        own = core.item_add(self.c, "a", "small talk", found_during=design, tier="expert", models={"model": "sonnet"})
        raw = core._item(self.c, own["id"])  # item_show gives the effective limits, with the defaults
        self.assertEqual((raw["model"], raw["max_model"]), ("sonnet", None))
        plain = core.item_add(self.c, "a", "plain")["id"]
        self.assertEqual(core.work_model(self.c, core._item(self.c, plain)), (None, None))
        follow = core.item_add(self.c, "a", "follow-up", found_during=plain, tier="expert")
        raw = core._item(self.c, follow["id"])
        self.assertEqual((raw["model"], raw["max_model"]), (None, None))
        # An old item with no session model: a max raised above the default max says the work was meant for it.
        old = core.item_add(self.c, "a", "old fable work", models={"max_model": "fable"})["id"]
        self.assertEqual(core.work_model(self.c, core._item(self.c, old))[0], "fable")
        # The model of a new session for an item, and why.
        self.assertEqual(core.session_model(self.c, core._item(self.c, talk["id"]))[0], "fable")
        self.assertEqual(core.session_model(self.c, core._item(self.c, plain)),
                         ("opus", "the default model (global)"))
        self.assertEqual(core.session_model(self.c, core._item(self.c, plain), defaults=False)[0], None)
        self.assertEqual(core.session_model(self.c, core._item(self.c, talk["id"]), family="openai")[0], None)

    def test_a_full_model_id_has_the_limits_of_its_ladder_name(self):
        """#4950: go --model claude-opus-5-5 took an item with --min-model fable, because the full id was not
        in the ladder and an unknown model passed every limit."""
        from river import cli
        fable = core.item_add(self.c, "a", "for fable", 0, models={"min_model": "fable", "max_model": "fable"})["id"]
        opus = core.item_add(self.c, "a", "for opus", 1, models={"max_model": "opus"})["id"]
        b = core.go(self.c, self.dir.name, "s1", model="claude-opus-5-5")
        self.assertEqual(b["item"]["id"], opus)
        self.assertEqual([x["id"] for x in b["model_skipped"]], [fable])
        self.assertIn("needs at least fable", b["model_skipped"][0]["why"])
        core.register(self.c, "s0")
        core.set_agent_model(self.c, "s0", "claude-opus-5-5")
        with self.assertRaisesRegex(RiverError, "needs at least fable"):
            core.claim(self.c, fable, "s0")
        # A model that the ladder does not know takes no item with a limit, and go says why.
        b = core.go(self.c, self.dir.name, "s2", model="mystery-7")
        self.assertIsNone(b.get("item"))
        self.assertIn("not in model_ladder", b["model_skipped"][0]["why"])
        # MAXPM_MODEL with a full id does the same as --model.
        core.release(self.c, opus, "s1")
        old = {k: os.environ.get(k) for k in ("MAXPM_DB", "MAXPM_MODEL")}
        os.environ.update(MAXPM_DB=self.path, MAXPM_MODEL="claude-fable-5-2[1m]")
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.update({k: v})
                                 for k, v in old.items()])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            cli.run(["--as", "s3", "go", "--project", "a"])
        self.assertEqual(core._item(self.c, fable)["assignee"], "s3")
        # The launch command quotes a full id: [1m] is a glob in zsh.
        from unittest import mock
        with mock.patch.object(core, "PLATFORM", "darwin"):
            self.assertIn("--model 'claude-opus-5-5[1m]' ", core.build_command("claude-code", {"prompt": "go"},
                                                                              model="claude-opus-5-5[1m]"))
        self.assertIn("--model opus ", core.build_command("claude-code", {"prompt": "go"}, model="opus"))


class ReleaseBase(Base):
    """A target web with the project site, one shipped item in its deploy item, and the owner ops."""

    def setUp(self):
        super().setUp()
        core.target_add(self.c, "web", "push, then smoke test")
        core.project_add(self.c, "site", target="web", path=self.dir.name)
        for n in ("dev", "ops"):
            core.register(self.c, n)
        core.register(self.c, "mark", human=True)
        w = self.add("site", "page")
        core.claim(self.c, w, "dev")
        core.done(self.c, w, "abc", "dev", ship_it=True)
        self.deploy = core.item_show(self.c, w)["unblocks"][0]
        core.target_own(self.c, "web", "ops")

    def release(self):
        core.claim(self.c, self.deploy, "ops")
        core.done(self.c, self.deploy, "v1 out", "ops")
        return core.parse_iso(core._item(self.c, self.deploy)["closed_at"])

    def run_at(self, name, at, status=200):
        c = core.ops_find(self.c, "site", name)
        with mock.patch.object(core, "OPS_HTTP", lambda url, limit: (status, "")):
            return core.ops_finish(self.c, core.ops_start(self.c, c, at=at), at=at)

    def due(self, at):
        return [c["name"] for c in core.ops_due(self.c, at)]

    def deploy_lines(self):
        return [r[0] for r in self.c.execute("SELECT change FROM events WHERE item_id=? AND change LIKE 'release watch%' "
                                             "ORDER BY id", (self.deploy,))]


class ReleaseWatch(ReleaseBase):
    """After each release of a target, its checks run at once as the watch of the release (#5459): then every
    ops_watch_every until fail_after (or recover_after) runs ended since the release, then on their own schedule. A fault in the watch
    opens its item found during the deploy item. The target's free monitor text is gone."""

    def test_the_checks_run_at_once_after_the_release_then_every_minute_then_on_their_schedule(self):
        core.ops_add(self.c, "site", "home", "url", {"url": "https://example.test/"}, actor="dev")
        self.run_at("home", core.now() - timedelta(minutes=1))
        t = self.release() + timedelta(seconds=1)
        self.assertEqual(self.due(t), ["home"])  # its own every (5m) is not over: the release makes it due
        self.assertIsNone(self.run_at("home", t)["watch"])  # run 1 of 3: no line yet
        self.assertEqual(self.due(t + timedelta(seconds=30)), [])
        self.assertEqual(self.due(t + timedelta(seconds=61)), ["home"])  # ops_watch_every
        self.run_at("home", t + timedelta(seconds=61))
        self.assertEqual(self.deploy_lines(), [])
        r = self.run_at("home", t + timedelta(seconds=122))  # run 3 of fail_after 3: the watch ends
        self.assertEqual(r["watch"], "release watch: site/home healthy after 3 run(s) since the release (last run ok)")
        self.assertEqual(self.deploy_lines(), [r["watch"]])
        self.assertEqual(self.due(t + timedelta(seconds=200)), [])  # its own schedule again
        self.assertEqual(self.due(t + timedelta(seconds=122 + 300)), ["home"])
        core.config_set(self.c, "ops_watch_every", "2m")
        self.assertEqual(core.setting(self.c, "ops_watch_every"), "2m")

    def test_a_cron_check_takes_no_part_in_the_watch_unless_ops_watch_cron_is_on(self):
        """A weekly job (#5538) runs at its cron times only; the setting ops_watch_cron (project) puts it in the watch."""
        core.config_set(self.c, "timezone", "UTC", project="site")
        core.ops_add(self.c, "site", "weekly", "url", {"url": "https://example.test/", "cron": "0 9 * * mon"},
                     actor="dev")
        released = self.release()
        with core.tx(self.c):  # added at the release: no cron time of its own passed before t
            self.c.execute("UPDATE checks SET created_at=?", (core.iso(released),))
        t = released + timedelta(seconds=1)
        self.assertEqual(self.due(t), [])
        self.assertIsNone(core._ops_watch(self.c, core.ops_find(self.c, "site", "weekly"), t))
        with self.assertRaisesRegex(RiverError, "ops_watch_cron is on or off"):
            core.config_set(self.c, "ops_watch_cron", "yes", project="site")
        core.config_set(self.c, "ops_watch_cron", "on", project="site")
        self.assertEqual(self.due(t), ["weekly"])

    def test_a_fault_in_the_watch_opens_its_item_found_during_the_deploy(self):
        core.ops_add(self.c, "site", "home", "url", {"url": "https://example.test/"}, actor="dev")
        t = self.release() + timedelta(seconds=1)
        r = [self.run_at("home", t + timedelta(seconds=61 * k), 503) for k in range(3)]
        self.assertEqual(core.ops_show(self.c, "site", "home")["state"], "fault")  # in 2 minutes, not 10
        self.assertEqual(r[2]["items"][0]["action"], "opened")
        it = core.item_show(self.c, r[2]["items"][0]["item"])
        self.assertEqual((it["kind"], it["found_during"]), ("operational", self.deploy))
        self.assertIn(f"The fault came in the watch of release #{self.deploy} to web", it["context"])
        self.assertEqual(self.deploy_lines(), [f"release watch: site/home in fault after 3 run(s) since the release: "
                                               f"#{it['id']}"])
        self.assertEqual(self.due(t + timedelta(seconds=122 + 61)), [])  # the fault ends the watch
        # A later run of the same fault is outside the watch: its note does not name the release.
        x = self.run_at("home", t + timedelta(minutes=40), 503)
        self.assertNotIn("watch", x)
        self.assertEqual(x["items"][0]["item"], it["id"])

    def test_a_check_of_kind_release_is_a_fault_at_one_failed_run_in_the_watch_and_after_fail_after_outside_it(self):
        core.ops_add(self.c, "site", "home", "url", {"url": "https://example.test/", "errors": "release"}, actor="dev")
        t = self.release() + timedelta(seconds=1)
        r = self.run_at("home", t, 503)  # the first run after the release, and the run that confirms it
        self.assertEqual((core.ops_show(self.c, "site", "home")["state"], r["items"][0]["action"]), ("fault", "opened"))
        self.assertEqual(core.item_show(self.c, r["items"][0]["item"])["found_during"], self.deploy)
        core.ops_add(self.c, "site", "api", "url", {"url": "https://api.example.test/", "errors": "release"},
                     actor="dev")
        for k in range(3):  # the three runs of its watch pass
            self.run_at("api", t + timedelta(seconds=61 * k))
        self.run_at("api", t + timedelta(hours=3), 503)  # after the watch, fail_after (3) counts
        self.assertEqual(core.ops_show(self.c, "site", "api")["state"], "failing")

    def test_with_watch_times_the_checks_run_at_each_time_after_the_release_and_a_missed_time_once_late(self):
        core.ops_add(self.c, "site", "home", "url", {"url": "https://example.test/", "every": "6h"}, actor="dev")
        self.run_at("home", core.now() - timedelta(minutes=1))
        self.assertEqual(core.target_watch_times(self.c, "web", "10m 1m, 1h")["watch_times"], "1m 10m 1h")
        with self.assertRaisesRegex(RiverError, "after the release"):
            core.target_watch_times(self.c, "web", "0m")
        t = self.release()
        self.assertEqual(self.due(t + timedelta(seconds=30)), [])  # the first time, 1m, has not come
        self.assertEqual(self.due(t + timedelta(seconds=61)), ["home"])
        self.assertIsNone(self.run_at("home", t + timedelta(seconds=61))["watch"])
        self.assertEqual(self.due(t + timedelta(minutes=5)), [])
        self.run_at("home", t + timedelta(minutes=10, seconds=5))
        # serve did not run from 30m to 3h: the time 1h passed with no run; it runs once, marked late.
        c = core.ops_due(self.c, t + timedelta(hours=3))[0]
        self.assertEqual((c["missed"]["late"], c["missed"]["time"], c["missed"]["release"]), (True, "1h", self.deploy))
        with mock.patch.object(core, "OPS_HTTP", lambda url, limit: (200, "")):  # as serve does: the due check
            r = core.ops_finish(self.c, core.ops_start(self.c, c, at=t + timedelta(hours=3)), at=t + timedelta(hours=3))
        self.assertEqual(json.loads(core.ops_history(self.c, "site", "home", 1)["runs"][0]["missed"])["time"], "1h")
        self.assertEqual(r["watch"], "release watch: site/home healthy after 3 of the watch times (1m 10m 1h) since "
                                     "the release (last run ok)")
        self.assertIn("release watch: site/home runs late for the watch time 1h", " ".join(self.deploy_lines()))
        self.assertEqual(self.due(t + timedelta(hours=4)), [])  # the series ended: its own schedule again

    def test_a_newer_release_ends_the_older_series_and_a_fault_at_a_watch_time_names_its_release(self):
        core.ops_add(self.c, "site", "home", "url", {"url": "https://example.test/", "every": "6h",
                                                     "fail_after": 1}, actor="dev")
        self.run_at("home", core.now() - timedelta(minutes=1))
        core.target_watch_times(self.c, "web", "1m 1h")
        t = self.release()
        self.run_at("home", t + timedelta(seconds=61))
        w = self.add("site", "fix")
        core.claim(self.c, w, "dev")
        core.done(self.c, w, "def", "dev", ship_it=True)
        second = core.item_show(self.c, w)["unblocks"][0]
        core.claim(self.c, second, "ops")
        with mock.patch.object(core, "now", lambda: t + timedelta(minutes=5)):
            core.done(self.c, second, "v2 out", "ops")
        t2 = core.parse_iso(core._item(self.c, second)["closed_at"])
        self.assertEqual(self.due(t + timedelta(hours=1, seconds=1)), ["home"])  # the 1m of release 2, never the 1h of 1
        r = self.run_at("home", t2 + timedelta(minutes=2), 503)
        it = core.item_show(self.c, r["items"][0]["item"])
        self.assertEqual(it["found_during"], second)

    def test_a_check_added_after_the_release_runs_on_its_own_schedule(self):
        t = self.release() + timedelta(seconds=5)
        core.ops_add(self.c, "site", "home", "url", {"url": "https://example.test/"}, actor="dev")
        self.c.execute("UPDATE checks SET created_at=?", (core.iso(t),))  # a second may hold both
        self.c.commit()
        self.assertEqual(self.due(t), ["home"])  # its first run
        self.run_at("home", t)
        self.assertEqual(self.due(t + timedelta(seconds=61)), [])

    def test_the_deployer_brief_and_target_show_name_the_checks_and_the_monitor_text_is_gone(self):
        from river import cli
        core.ops_add(self.c, "site", "home", "url", {"url": "https://example.test/"}, actor="dev")
        core.ops_add(self.c, "web", "health", "url", {"url": "https://example.test/health"}, actor="dev")
        self.assertEqual(core.target_show(self.c, "web")["checks"], ["web/health", "site/home"])
        self.assertNotIn("monitor", core.target_show(self.c, "web"))
        self.assertFalse(hasattr(core, "target_monitor"))
        b = core.go(self.c, self.dir.name, "ops")
        self.assertEqual((b["role"], b["item"]["id"]), ("deployer", self.deploy))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.render_go(b)
        text = out.getvalue()
        self.assertIn("after the deploy, maxpm serve runs its checks at once as the watch of the release: "
                      "web/health, site/home", text)
        self.assertNotIn("Do not roll back yourself.", text)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["target", "monitor", "web", "watch it"])


class FixVerify(ReleaseBase):
    """A fix is done when its check passes where it runs (#5461): the check that its --check names, or the check
    of the fault it fixes, gives the verdict at the end of the watch of the release that ships it."""

    def ship_fix(self, title, check="", found_during=None):
        if found_during:  # a fix of a fault waits on a proven cause (#5513)
            core.ops_cause(self.c, found_during, "the page broke", "true", "fix the page", actor="dev")
        x = core.item_add(self.c, "site", title, check=check, found_during=found_during, actor="dev")["id"]
        core.claim(self.c, x, "dev")
        core.done(self.c, x, "fixed", "dev", ship_it=True)
        self.assertIn(x, core.item_show(self.c, self.deploy)["waits_on"])
        return x

    def events(self, x):
        return [r[0] for r in self.c.execute("SELECT change FROM events WHERE item_id=? ORDER BY id", (x,))]

    def test_a_fix_of_a_fault_is_verified_when_the_check_recovers_after_the_release(self):
        core.ops_add(self.c, "site", "home", "url", {"url": "https://example.test/"}, actor="dev")
        t0 = core.now() - timedelta(minutes=30)
        for k in range(3):
            r = self.run_at("home", t0 + timedelta(minutes=5 * k), 503)
        fault = r["items"][0]["item"]
        x = self.ship_fix("Fix the home page", found_during=fault)
        self.assertEqual(core.fix_check(self.c, x)[1], f"the check of fault #{fault}")
        t = self.release() + timedelta(seconds=1)
        for k in range(3):
            r = self.run_at("home", t + timedelta(seconds=61 * k))
        self.assertIn("watch", r)
        self.assertEqual(core._item(self.c, x)["status"], "done")
        self.assertEqual([e for e in self.events(x) if e.startswith("verified")],
                         [f"verified: site/home (the check of fault #{fault}) is healthy after 3 run(s) since release "
                          f"#{self.deploy}"])

    def test_a_fix_whose_check_fails_after_the_release_is_open_again_with_the_evidence(self):
        core.ops_add(self.c, "site", "home", "url", {"url": "https://example.test/"}, actor="dev")
        x = self.ship_fix("Fix the home page", check="ops:site:home")
        other = self.ship_fix("Fix the footer", check="pytest tests/test_footer.py")
        self.assertEqual(core.fix_check(self.c, other), (None, None))  # a command is never run by serve
        t = self.release() + timedelta(seconds=1)
        for k in range(3):
            self.run_at("home", t + timedelta(seconds=61 * k), 503)
        it = core._item(self.c, x)
        self.assertEqual((it["status"], it["assignee"]), ("open", None))
        ev = self.events(x)
        self.assertEqual(ev[-2], "reopened")
        self.assertTrue(ev[-1].startswith(f"verification failed, open again: site/home (its --check) is fault after 3 "
                                          f"run(s) since release #{self.deploy}; last run critical: HTTP 503"), ev[-1])
        self.assertIn("maxpm ops history site home", ev[-1])
        self.assertEqual(core._item(self.c, other)["status"], "done")
        self.assertFalse([e for e in self.events(other) if "verif" in e])


    def test_after_two_released_fixes_did_not_clear_a_fault_no_more_agents_start_and_the_manager_decides(self):
        core.ops_add(self.c, "site", "home", "url", {"url": "https://example.test/"}, actor="dev")
        t0 = core.now() - timedelta(minutes=30)
        for k in range(3):
            r = self.run_at("home", t0 + timedelta(minutes=5 * k), 503)
        fault = r["items"][0]["item"]
        x = self.ship_fix("Fix the home page", found_during=fault)
        t = self.release() + timedelta(seconds=1)
        for k in range(3):
            r = self.run_at("home", t + timedelta(seconds=61 * k), 503)
        self.assertEqual(core._item(self.c, x)["status"], "open")  # one failed fix: it opens again, agents go on
        self.assertIsNone(core._item(self.c, fault)["blocked_reason"])
        core.claim(self.c, x, "dev")
        core.done(self.c, x, "fixed again", "dev", ship_it=True)
        second = self.c.execute("SELECT id FROM items WHERE kind='deploy' AND status='open'").fetchone()["id"]
        core.claim(self.c, second, "ops")
        with mock.patch.object(core, "now", lambda: t + timedelta(minutes=10)):
            core.done(self.c, second, "v2 out", "ops")
        t2 = core.parse_iso(core._item(self.c, second)["closed_at"]) + timedelta(seconds=1)
        for k in range(3):
            r = self.run_at("home", t2 + timedelta(seconds=61 * k), 503)
        for i in (fault, x):
            self.assertTrue(core._item(self.c, i)["blocked_reason"].startswith(
                "the manager decides: 2 released fixes did not clear ops:site:home"), i)
        self.assertTrue(self.c.execute("SELECT 1 FROM events WHERE item_id=? AND change LIKE 'ops fix limit: the manager "
                                       "decides: 2 released fixes%'", (fault,)).fetchone())


class SharedGoalHandoff(Base):
    """A shared goal keeps a handoff (#4974): the worker that finishes a task of it brings the handoff up to date,
    and each session that starts on the goal reads it, with a word when it is older than the last finished task."""

    def test_the_finishing_worker_keeps_it_and_the_next_session_reads_it(self):
        from river import cli
        core.project_add(self.c, "p", path=self.dir.name)
        for n in ("w1", "w2"):
            core.register(self.c, n)
        core.goal_add(self.c, "p", "lib", outcome="o", done_when="d", shared=True)
        a = core.item_add(self.c, "p", "first walk", goals=["lib"])["id"]
        b = core.item_add(self.c, "p", "second walk", goals=["lib"])["id"]
        core.claim(self.c, a, "w1")
        res = core.done(self.c, a, "walked", "w1")
        self.assertEqual(res["shared_handoffs"], ["lib"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli._print_show(res)
        self.assertIn("goal lib is shared, so you keep its handoff now: maxpm goal handoff lib --file <path>", out.getvalue())
        core.goal_handoff(self.c, "lib", "the walk procedure is in plan section 2", "w1")
        self.assertIsNone(core._handoff_due(self.c, core._goal(self.c, "lib")))
        # The next session on the goal reads it; the briefing says to keep it when it finishes.
        g = core.go(self.c, self.dir.name, "w2")
        self.assertEqual(g["item"]["id"], b)
        self.assertEqual([h["goal"] for h in g["handoffs"]], ["lib"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            print(cli.render_go(g) or "")
        self.assertIn("the walk procedure is in plan section 2", out.getvalue())
        self.assertIn("When you finish, bring its handoff up to date: maxpm --as w2 goal handoff lib", out.getvalue())
        # A task finished after the handoff: the next reader is told it is older.
        c = core.item_add(self.c, "p", "third walk", goals=["lib"])["id"]
        with core.tx(self.c):  # the handoff was written a minute before the next close
            self.c.execute("UPDATE goal_handoffs SET created_at=?", (core.iso(core.now() - timedelta(minutes=1)),))
        core.done(self.c, b, "walked too", "w2")
        due = core._handoff_due(self.c, core._goal(self.c, "lib"))
        self.assertEqual((due["id"], due["shared"]), (b, True))
        core.register(self.c, "w3")
        g = core.go(self.c, self.dir.name, "w3")
        self.assertEqual(g["item"]["id"], c)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            print(cli.render_go(g) or "")
        self.assertIn(f"The handoff is older than #{b} (second walk)", out.getvalue())
        # A person's close asks nothing of them.
        core.register(self.c, "mark", human=True)
        core.release(self.c, c, actor="w3")
        core.claim(self.c, c, "mark")
        self.assertNotIn("shared_handoffs", core.done(self.c, c, "x", "mark"))


class BrokenRules(Base):
    """The manager's briefing shows each role rule that the queue shows broken (#4976)."""

    def keys(self):
        return sorted(x["key"] for x in core.manager_findings(self.c)["broken_rules"])

    def test_each_broken_rule_shows(self):
        from river import cli
        core.target_add(self.c, "web", "push")
        core.project_add(self.c, "site", target="web", path=self.dir.name)
        for n in ("dev", "rev", "ops", "mgr"):
            core.register(self.c, n)
        core.register(self.c, "mark", human=True)
        core.config_set(self.c, "review", "on")
        self.assertEqual(self.keys(), [])
        # A review taken by a used session (a person's push before the rule, for example).
        x = self.add("site", "other")
        core.claim(self.c, x, "rev")
        core.done(self.c, x, "done", "rev")
        a = self.add("site", "page")
        core.claim(self.c, a, "dev")
        core.done(self.c, a, "commit", "dev", ship_it=True)
        rv = next(i for i in core.item_show(self.c, a)["unblocks"] if core._item(self.c, i)["kind"] == "review")
        dep = next(i for i in core.item_show(self.c, a)["unblocks"] if core._item(self.c, i)["kind"] == "deploy")
        with core.tx(self.c):
            self.c.execute("UPDATE items SET status='in_progress', assignee='rev' WHERE id=?", (rv,))
        self.assertEqual(self.keys(), [f"review:{rv}"])
        core.review_pass(self.c, rv, "ok", "rev")
        # A deploy that the manager closed.
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET role='manager' WHERE name='mgr'")
        with core.tx(self.c):
            self.c.execute("UPDATE items SET status='in_progress', assignee='mgr' WHERE id=?", (dep,))
        core.done(self.c, dep, "released", "mgr")
        self.assertEqual(self.keys(), [f"manager-deploy:{dep}"])
        # A session that holds the deploy of a target with a deploy hook types the production command.
        core.target_hook(self.c, "web", "deploy", "make release", actor="mark")
        b = self.add("site", "form")
        core.claim(self.c, b, "dev")
        dep2 = core.done(self.c, b, "commit", "dev", ship_it=True)["shipped_in"]
        with core.tx(self.c):  # taken before the hook was set: a release by hand, as it had to be
            self.c.execute("UPDATE items SET status='in_progress', assignee='ops', claimed_at=? WHERE id=?",
                           (core.iso(core.now() - timedelta(hours=1)), dep2))
            self.c.execute("UPDATE hooks SET set_at=?", (core.iso(core.now() - timedelta(minutes=5)),))
        self.assertNotIn(f"typed:{dep2}", self.keys())
        self.assertNotIn(f"manager-deploy:{dep}", [k for k in self.keys() if k.startswith("typed:")])
        with core.tx(self.c):  # taken after it
            self.c.execute("UPDATE items SET claimed_at=? WHERE id=?", (core.iso(core.now()), dep2))
        self.assertIn(f"typed:{dep2}", self.keys())
        # A switch left on: an automation setting off its default.
        core.config_set(self.c, "auto_continue", "off", actor="mgr")
        f = core.manager_findings(self.c)
        sw = next(x for x in f["broken_rules"] if x["key"] == "setting:auto_continue")
        self.assertIn("auto_continue is off for the whole queue (default tree), set by mgr at", sw["text"])
        self.assertIn("rule:setting:auto_continue", core._finding_keys(f))
        lines = "\n".join(cli._findings_lines(f, "maxpm --as mgr"))
        self.assertIn("BROKEN RULE (an automation runs as its default says): auto_continue is off", lines)
        self.assertIn("maxpm --as mgr config unset auto_continue", lines)
        # An expert on a model other than that of its work.
        t = core.item_add(self.c, "site", "the design talk", tier="expert", models={"model": "fable", "max_model": "fable"})["id"]
        core.expert_make(self.c, "ex", "the design talk", "site", "mark")
        core.set_agent_model(self.c, "ex", "opus")
        with core.tx(self.c):
            self.c.execute("UPDATE items SET status='in_progress', assignee='ex' WHERE id=?", (t,))
        ex = next(x for x in core.manager_findings(self.c)["broken_rules"] if x["key"] == "expert:ex")
        self.assertIn("the expert ex runs opus; its task", ex["text"])


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

    def test_a_queued_item_of_a_goal_another_agent_owns_is_the_queues(self):
        """#5089: the manager queued an item of a goal that another agent owns. wait said WORK, go said IDLE, and claim
        refused ('reserved for <owner>, who owns goal ...'). The queue wins, as annotate says: wait, go and claim agree."""
        core.goal_add(self.c, "a", "look", "o", "d")
        core.goal_own(self.c, "look", "s2")
        it = core.item_add(self.c, "a", "the shorter board", actor="s2")["id"]  # tagged with the goal s2 owns
        self.assertEqual(core.annotate(self.c)[it]["reserved_for"], "s2")
        core.queue_add(self.c, "s1", it, actor="mark")
        self.assertEqual(core.annotate(self.c)[it]["reserved_for"], "s1")
        self.assertIn(f"#{it} in your queue is ready", core._work_for(self.c, "s1", ["a"]))  # what wait says
        b = core.go(self.c, self.dir.name, "s1")
        self.assertEqual((b["item"]["id"], b["why"]), (it, f"#{it} is the first ready item in your queue"))
        # Another agent still may not take an item of the goal that is in nobody's queue.
        other = core.item_add(self.c, "a", "the header", actor="s2")["id"]
        core.register(self.c, "s3")
        with self.assertRaisesRegex(RiverError, "reserved for s2, who owns goal look"):
            core.claim(self.c, other, "s3")

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

    def test_the_stop_entry_goes_when_the_stopped_session_is_gone(self):
        # #5557: _agent_state says stopped before gone, so the sweep never dropped the queue of a stopped agent: 695 of
        # 706 queue entries on the live queue were stop entries of ended agents. The entry stays while the session may
        # run (a person can withdraw the stop), and goes with the queue once the session is gone.
        x = self.add("a", "x")
        core.queue_add(self.c, "s1", x, actor="mark")
        core.stop_agent(self.c, "s1", "project paused", actor="mark")
        core.stop_agent(self.c, "s2", "wrong model", actor="mark")
        core.go(self.c, self.dir.name, "s1")  # its stop finishes: the item goes back, the stop entry stays
        core.activity(self.c, "mark")
        self.assertEqual([e["kind"] for e in core.queue_list(self.c, "s1")["entries"]], ["stop"])
        old = core.iso(core.now() - timedelta(hours=25))
        self.c.execute("UPDATE agents SET last_seen=? WHERE name='s1'", (old,))
        core.activity(self.c, "mark")
        self.assertEqual(core.queue_list(self.c, "s1")["entries"], [])
        self.assertEqual(core.agent_status(self.c, "s1")["state"], "stopped")  # agents.stop_at keeps the stop
        self.assertEqual([e["kind"] for e in core.queue_list(self.c, "s2")["entries"]], ["stop"])


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
        if info is None:
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
        with mock.patch.object(core, "_cli_process_name", return_value=None):  # no agent CLI in the environment
            self.assertEqual(core.agent_process(self.p.pid)[0], self.p.pid)  # not a shell: it is the agent
        self.assertTrue(core._between("-zsh", "-zsh"))
        self.assertFalse(core._between("claude", "claude go"))
        self.p.kill()
        self.p.wait(10)
        self.assertFalse(core.pid_alive(self.p.pid))
        self.assertIsNone(core.proc_info(self.p.pid))

    def test_a_process_is_one_agents_only(self):
        """#5060: the manager ran maxpm --as <agent> ... in its session, and the manager's pid went into that agent's
        row: who showed a dead expert as active, its resume refused, and tmux_adopt ended the manager."""
        from unittest import mock
        from river import cli
        for n in ("mgr", "ex", "old"):
            core.register(self.c, n)
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET role='manager' WHERE name='mgr'")
        pid = lambda n: self.c.execute("SELECT pid FROM agents WHERE name=?", (n,)).fetchone()[0]
        self.assertTrue(core.set_process(self.c, "mgr", 500, "claude manage", env={"MAXPM_AGENT": "mgr"}))
        self.assertTrue(core.set_process(self.c, "ex", 600, "claude ex", env={}))
        # In the manager's session (MAXPM_AGENT=mgr), a command for ex records nothing for ex.
        self.assertFalse(core.set_process(self.c, "ex", 500, "claude manage", env={"MAXPM_AGENT": "mgr"}))
        with mock.patch.dict(os.environ, {"MAXPM_AGENT": "mgr", "MAXPM_DB": self.path}), mock.patch.object(core, "agent_process",
                                                                                  lambda: (500, "claude manage")):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.run(["--as", "ex", "heartbeat"]), 0)
        self.assertEqual((pid("mgr"), pid("ex")), (500, 600))
        # With no MAXPM_AGENT (a person's own terminal), the same process stays with the agent that has it.
        self.assertFalse(core.set_process(self.c, "ex", 500, "claude manage", env={}))
        self.assertEqual(pid("ex"), 600)
        # An old row whose pid came again for another command loses it.
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET pid=700, pid_cmd='zsh', host=? WHERE name='old'", (core.this_host(),))
        self.assertTrue(core.set_process(self.c, "ex", 700, "claude ex", env={}))
        self.assertEqual((pid("old"), pid("ex")), (None, 700))
        # The session that MAXPM_AGENT names takes its pid back from a row that took it before.
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET pid=500, pid_cmd='claude manage' WHERE name='old'")
        self.assertTrue(core.set_process(self.c, "mgr", 500, "claude manage", env={"MAXPM_AGENT": "mgr"}))
        self.assertEqual((pid("mgr"), pid("old")), (500, None))

    def test_only_the_manager_runs_a_command_as_another_agent(self):
        """#5060, mark 2026-11-10: 'the manager should be allowed to run for other agents'. A worker session is
        refused; the manager's command records nothing of its session for the other agent, so the hooks of the
        manager's session stay the manager's. A person's terminal (no MAXPM_AGENT) may act as anyone."""
        from unittest import mock
        from river import cli
        for n in ("mgr", "w1", "w2"):
            core.register(self.c, n)
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET role='manager' WHERE name='mgr'")
        sid = "11111111-2222-3333-4444-555555555555"

        def run(me, *args):
            env = {"MAXPM_DB": self.path, "CLAUDE_CODE_SESSION_ID": sid, "MAXPM_AGENT": me}
            # A live pid (this test's own): with a dead one, mgr counts as gone and is no active manager (CI).
            with mock.patch.dict(os.environ, env), mock.patch.object(core, "agent_process", lambda: (os.getpid(), "claude")), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                try:
                    return cli.run(["--as", args[0], *args[1:]])
                except RiverError as e:
                    return (2, str(e))
        sessions = lambda: [r[0] for r in self.c.execute("SELECT agent FROM agent_sessions WHERE session_id=? ORDER BY agent",
                                                         (sid,))]
        self.assertEqual(run("mgr", "mgr", "heartbeat"), 0)
        self.assertEqual(run("mgr", "w1", "note", "the manager acts for w1"), 0)
        self.assertEqual(sessions(), ["mgr"])  # the manager's session is not w1's
        self.assertEqual(core.agent_status(self.c, "w1")["note"], "the manager acts for w1")
        self.assertEqual(core.activity_agent(self.c, sid, {"MAXPM_AGENT": "mgr"}), "mgr")
        # An old row of another agent in the map of the session does not take the hooks of the session.
        core.record_claude_session(self.c, "w1", sid)
        self.assertEqual(core.activity_agent(self.c, sid, {"MAXPM_AGENT": "mgr"}), "mgr")
        # A worker is refused, and the refusal says what to do.
        with self.assertRaisesRegex(RiverError, "only the manager runs a command as another agent .--as w2.*alert mgr"):
            with mock.patch.dict(os.environ, {"MAXPM_AGENT": "w1"}):
                cli._for_other(self.c, "w2")
        self.assertEqual(run("w1", "w2", "note", "w1 acts for w2")[0], 2)
        self.assertNotEqual(core.agent_status(self.c, "w2")["note"], "w1 acts for w2")
        # A person's terminal, and a session that acts as itself or as a person, are no command for another agent.
        core.register(self.c, "mark", human=True)
        with mock.patch.dict(os.environ, {"MAXPM_AGENT": ""}):
            self.assertFalse(cli._for_other(self.c, "w2"))
        with mock.patch.dict(os.environ, {"MAXPM_AGENT": "w1"}):
            self.assertFalse(cli._for_other(self.c, "w1"))
            self.assertFalse(cli._for_other(self.c, "mark"))

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


class PersonHold(Base):
    """#5311: the rest of #5307. An alert to a person and a person item (doer human) go to the active manager first,
    as a question does; the manager does it, closes it, or forwards it; question_hold ends the wait."""

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "a", path=self.dir.name)
        core.register(self.c, "w1")
        core.register(self.c, "mark", human=True)
        self.x = self.add("a", "x")
        core.claim(self.c, self.x, "w1")
        core.manage(self.c, self.dir.name, "boss")
        core.manage_watch(self.c, "boss", step="0s", sleep=lambda s: None)

    def sees(self, **kw):
        with core.tx(self.c):
            core.sync_needs_you(self.c)
        key, val = next(iter(kw.items()))
        return any(n.get(key) == val for n in core.needs_you(self.c, "mark"))

    def worker_item(self, title="Look at the page"):
        return core.item_add(self.c, "a", title, doer="human", found_during=self.x, actor="w1")["id"]

    def test_an_alert_to_a_person_is_held_and_forwarded(self):
        m = core.message(self.c, "alert", "Deploy waits for you", to="mark", item=self.x, actor="w1")[0]
        self.assertEqual(m["held_for"], "boss")
        self.assertFalse(self.sees(message_id=m["id"]))
        self.assertEqual(core.inbox(self.c, "mark"), [])
        w = core.manage_watch(self.c, "boss", step="1h", sleep=lambda s: self.fail("no wait"))
        self.assertIn(f"maxpm forward {m['id']} --drop", w["messages"][0]["body"])
        core.forward(self.c, m["id"], "check it", actor="boss")
        self.assertTrue(self.sees(message_id=m["id"]))
        self.assertEqual(len(core.inbox(self.c, "mark")), 2)  # the alert and the manager's note

    def test_the_manager_closes_an_alert_with_drop(self):
        m = core.message(self.c, "alert", "Need a push?", to="mark", item=self.x, actor="w1")[0]
        core.forward(self.c, m["id"], "pushed it", actor="boss", drop=True)
        self.assertFalse(self.sees(message_id=m["id"]))
        self.assertEqual(core.unread(self.c, "boss")["unread"], 0)
        self.assertEqual(core.inbox(self.c, "mark"), [])

    def test_a_person_item_is_held_for_the_manager(self):
        i = self.worker_item()
        self.assertFalse(self.sees(item_id=i))
        self.assertEqual(core.item_show(self.c, i)["held_for"], "boss")
        self.assertNotIn("Look at the page", core.prompt_for_all(self.c, "mark"))
        self.assertEqual([x for x in core.status(self.c)["human_waiting"] if x["id"] == i], [])
        w = core.manage_watch(self.c, "boss", step="1h", sleep=lambda s: self.fail("no wait"))
        self.assertIn(f"maxpm forward --item {i}", w["messages"][0]["body"])
        self.assertEqual(w["findings"]["human_ready"][0]["held_for"], "boss")
        with self.assertRaisesRegex(RiverError, "only the manager"):
            core.forward(self.c, item=i, actor="w1")
        core.forward(self.c, item=i, note="I checked", actor="boss")
        self.assertTrue(self.sees(item_id=i))
        with self.assertRaisesRegex(RiverError, "with the person already"):
            core.forward(self.c, item=i, actor="boss")
        # Not held again.
        self.assertTrue(self.sees(item_id=i))

    def test_the_manager_does_a_person_item(self):
        i = self.worker_item()
        self.assertFalse(self.sees(item_id=i))
        core.drop(self.c, i, "boss", "not needed")
        self.assertFalse(self.sees(item_id=i))
        self.assertEqual(core.item_show(self.c, i)["held_for"], "")

    def test_a_person_item_goes_to_the_person_after_question_hold(self):
        clock = [core.now()]
        with mock.patch.object(core, "now", lambda: clock[0]):
            i = self.worker_item()
            self.assertFalse(self.sees(item_id=i))
            clock[0] += timedelta(minutes=9)
            self.assertFalse(self.sees(item_id=i))
            clock[0] += timedelta(minutes=2)
            self.assertTrue(self.sees(item_id=i))

    def test_the_exceptions(self):
        # A person's own item, an item the manager added, and every item while no manager is active.
        mine = core.item_add(self.c, "a", "Mine", doer="human", actor="mark")["id"]
        bosss = core.item_add(self.c, "a", "Boss", doer="human", actor="boss")["id"]
        self.assertTrue(self.sees(item_id=mine))
        self.assertTrue(self.sees(item_id=bosss))
        core.unregister(self.c, "boss")
        j = self.worker_item("Another")
        self.assertTrue(self.sees(item_id=j))
        core.config_set(self.c, "question_hold", "0s")
        core.manage(self.c, self.dir.name, "boss2")
        k = self.worker_item("Third")
        self.assertTrue(self.sees(item_id=k))


class QuestionHold(Base):
    """#5307: mark, 2026-11-11: worker questions that he had settled opened 'Waits for you' on his page; 'these sorts
    of questions should not come to me or block waiting for me'. A question from a session to a person goes to the
    active manager first; the person sees only what the manager forwards, or what it leaves for question_hold."""

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "a", path=self.dir.name)
        core.register(self.c, "w1")
        core.register(self.c, "mark", human=True)
        self.x = self.add("a", "x")
        core.claim(self.c, self.x, "w1")
        core.manage(self.c, self.dir.name, "boss")
        core.manage_watch(self.c, "boss", step="0s", sleep=lambda s: None)

    def events(self):
        return [dict(r) for r in self.c.execute("SELECT change FROM events WHERE item_id=?", (self.x,))]

    def person_sees(self, qid):
        with core.tx(self.c):
            core.sync_needs_you(self.c)
        return any(n.get("message_id") == qid for n in core.needs_you(self.c, "mark"))

    def test_the_manager_gets_it_first_and_forwards_it(self):
        q = core.message(self.c, "question", "Put it on main for the push?", to="mark", item=self.x, actor="w1")[0]
        self.assertEqual(q["held_for"], "boss")
        self.assertIn("held for the manager boss", q["held_note"])
        self.assertFalse(self.person_sees(q["id"]))
        self.assertNotIn("Put it on main", core.prompt_for_all(self.c, "mark"))
        from river import sessions
        self.assertEqual(sessions._open_questions(self.c, "w1"), [])
        # The asker's wait does not change: it still waits on the person's answer.
        self.assertEqual(core.waits_on_person(self.c, ["w1"]), {"w1": q["id"]})
        # The manager's watch wakes at once, with the question and the two commands.
        w = core.manage_watch(self.c, "boss", step="1h", sleep=lambda s: self.fail("no wait for a held question"))
        self.assertEqual(len(w["messages"]), 1)
        self.assertIn(f"maxpm forward {q['id']}", w["messages"][0]["body"])
        self.assertIn("Put it on main", w["messages"][0]["body"])
        self.assertTrue([x for x in w["findings"]["questions"] if x["id"] == q["id"] and x["held_for"] == "boss"])
        # Only a manager (or a person) forwards it.
        with self.assertRaisesRegex(RiverError, "only the manager"):
            core.forward(self.c, q["id"], actor="w1")
        core.forward(self.c, q["id"], "I checked the diff; it is safe", actor="boss")
        self.assertIsNone(core.message_show(self.c, q["id"])["held_for"])
        self.assertTrue(self.person_sees(q["id"]))
        ev = [n for n in core.needs_you(self.c, "mark") if n.get("message_id") == q["id"]][0]
        self.assertIn("the manager: I checked the diff", ev["summary"])
        self.assertEqual(sessions._open_questions(self.c, "w1")[0]["id"], q["id"])
        with self.assertRaisesRegex(RiverError, "with mark already"):
            core.forward(self.c, q["id"], actor="boss")

    def test_the_manager_answers_for_the_person(self):
        q = core.message(self.c, "question", "Which branch?", to="mark", item=self.x, actor="w1")[0]
        a = core.answer(self.c, q["id"], "main", actor="boss")
        self.assertEqual(core.message_show(self.c, q["id"])["state"], "answered")
        self.assertFalse(self.person_sees(q["id"]))
        t = core.thread(self.c, q["id"])["messages"]
        self.assertEqual([m.get("for_person") for m in t if m["id"] == a["id"]], ["mark"])
        self.assertTrue(any("for mark (the manager)" in h["change"] for h in self.events()))
        # The manager's alert about it is read: no watch wakes for it.
        self.assertEqual(core.unread(self.c, "boss")["unread"], 0)
        from river import cli
        out = io.StringIO()
        db = self.c.execute("PRAGMA database_list").fetchone()[2]
        with mock.patch.dict(os.environ, {"MAXPM_DB": db}), contextlib.redirect_stdout(out):
            cli.run(["--as", "w1", "thread", str(q["id"])])
        self.assertIn("answer from boss for mark to w1", out.getvalue())

    def test_with_no_manager_action_the_person_gets_it_after_question_hold(self):
        clock = [core.now()]
        with mock.patch.object(core, "now", lambda: clock[0]):
            q = core.message(self.c, "question", "Approve?", to="mark", item=self.x, actor="w1")[0]
            clock[0] += timedelta(minutes=9)
            self.assertFalse(self.person_sees(q["id"]))
            clock[0] += timedelta(minutes=2)
            self.assertTrue(self.person_sees(q["id"]))
        self.assertTrue(any("did not answer or forward it within question_hold" in h["change"]
                            for h in self.events()))

    def test_not_held_without_an_active_manager_or_for_the_manager_or_with_hold_0s(self):
        core.config_set(self.c, "question_hold", "0s")
        q = core.message(self.c, "question", "A?", to="mark", item=self.x, actor="w1")[0]
        self.assertIsNone(q["held_for"])
        self.assertTrue(self.person_sees(q["id"]))
        core.config_unset(self.c, "question_hold")
        q = core.message(self.c, "question", "B?", to="mark", actor="boss")[0]
        self.assertIsNone(q["held_for"])  # the manager's own question
        q = core.message(self.c, "question", "C?", to="boss", actor="w1")[0]
        self.assertIsNone(q["held_for"])  # not to a person
        core.unregister(self.c, "boss", "mark")
        q = core.message(self.c, "question", "D?", to="mark", item=self.x, actor="w1")[0]
        self.assertIsNone(q["held_for"])
        self.assertTrue(self.person_sees(q["id"]))

    def test_a_held_question_goes_to_the_person_when_the_manager_ends(self):
        q = core.message(self.c, "question", "E?", to="mark", item=self.x, actor="w1")[0]
        core.unregister(self.c, "boss", "mark")
        self.assertTrue(self.person_sees(q["id"]))
        self.assertTrue(any("is no longer active" in h["change"] for h in self.events()))


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

    def test_a_managers_unread_mail_goes_to_the_next_manager(self):
        """#5600: a release waited on an alert to a manager that a person had stopped; the next manager never saw
        it, and its sender got 'send it to another agent'."""
        x = self.add("a", "release v0.3.0")
        core.manage(self.c, self.dir.name, "boss")
        alert = core.send(self.c, "alert", "push main and the tag", to="boss", item=x, actor="w1")["id"]
        ask = core.send(self.c, "question", "which tag?", to="boss", actor="w2")["id"]
        core.stop_agent(self.c, "boss", "from the page", actor="mark")
        self.assertTrue(core.end_stopped(self.c, "boss")["ended"])
        # No manager runs: the mail waits for the next one, and each sender hears that.
        self.assertEqual({r["to_agent"] for r in self.c.execute("SELECT to_agent FROM messages WHERE id IN (?,?)",
                                                                 (alert, ask))}, {core.NEXT_MANAGER})
        told = [m["body"] for m in core.inbox(self.c, "w1") if m["kind"] == "notice"]
        self.assertTrue(any("waits for the next manager" in b for b in told), told)
        m = core.manage(self.c, self.dir.name, "boss2")
        self.assertEqual([h["id"] for h in m["handed"]], [alert, ask])
        self.assertEqual({r["id"] for r in core.inbox(self.c, "boss2") if r["unread"]} >= {alert, ask}, True)
        # A takeover hands on what the old manager did not read; a manager that is stopped while another runs too.
        late = core.send(self.c, "alert", "the gate is red", to="boss2", actor="w1")["id"]
        m3 = core.manage(self.c, self.dir.name, "boss3", takeover="boss2 went quiet")
        self.assertIn(late, [h["id"] for h in m3["handed"]])
        self.assertIn("It went to boss3", [m for m in core.inbox(self.c, "w1") if m["kind"] == "notice"][-1]["body"])
        core.register(self.c, "boss4")
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET role='manager' WHERE name='boss4'")
        q = core.send(self.c, "question", "deploy now?", to="boss4", actor="w2")["id"]
        core.stop_agent(self.c, "boss4", "two managers", actor="mark")
        core.end_stopped(self.c, "boss4")
        self.assertEqual(self.c.execute("SELECT to_agent FROM messages WHERE id=?", (q,)).fetchone()[0], "boss3")
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM messages WHERE to_agent='boss3' AND body LIKE 'STOP%'"
                                        ).fetchone()[0], 0)  # a stop request stays with the stopped manager
        from river import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.render(argparse.Namespace(cmd="manage", watch=False, json=False, actor="boss2"), m)
        self.assertIn("FROM THE LAST MANAGER", out.getvalue())

    def test_unpushed_and_untagged_finished_work_is_a_finding(self):
        """#5600: a target with no deploy hook releases by a push to main; its folder's main ahead of origin for
        over unpushed_after, and a done release item whose tag origin lacks, are findings of the manager."""
        os.makedirs(os.path.join(self.dir.name, ".git"))
        core.target_add(self.c, "site")
        core.project_target(self.c, "a", "site")
        old, new = int((core.now() - core.timedelta(hours=3)).timestamp()), int(core.now().timestamp())
        remote = ["abc\trefs/tags/v0.2.0", "def\trefs/tags/v0.2.0^{}"]
        calls = []

        def git(folder, args):
            calls.append(args[0])
            return f"{new}\n{old}\n" if args[0] == "log" else "\n".join(remote) + "\n"
        core.GIT_RUNNER = git
        self.addCleanup(setattr, core, "GIT_RUNNER", None)
        core._GIT_SEEN.clear()
        self.addCleanup(core._GIT_SEEN.clear)
        x = self.add("a", "Release v0.3.0: the notes, then the manager pushes main and the tag v0.3.0")
        core.done(self.c, x, output="on local main, not pushed", actor="w1")
        f = core.manager_findings(self.c)
        self.assertEqual([(u["target"], u["commits"]) for u in f["unpushed"]], [("site", 2)])
        self.assertEqual([(u["id"], u["tag"]) for u in f["untagged"]], [(x, "v0.3.0")])
        self.assertTrue({"unpushed:site", f"untagged:{x}"} <= set(core._finding_keys(f)))
        from river import cli
        lines = "\n".join(cli._findings_lines(f, "maxpm --as boss"))
        self.assertIn("UNPUSHED site", lines)
        self.assertIn(f"NOT TAGGED #{x}", lines)
        n = len(calls)
        core.manager_findings(self.c)
        self.assertEqual(len(calls), n)  # git once a minute (the watch looks every 3 s)
        core._GIT_SEEN.clear()
        remote.append("ghi\trefs/tags/v1.0.0")  # a later version went out: the old release is done with
        old = new - 600  # and the oldest unpushed commit is younger than unpushed_after
        f = core.manager_findings(self.c)
        self.assertEqual((f["unpushed"], f["untagged"]), ([], []))
        core._GIT_SEEN.clear()
        old = new - 4 * 3600
        core.target_hook(self.c, "site", "deploy", "make release", actor="mark")  # a deploy hook releases it instead
        self.assertEqual(core.manager_findings(self.c)["unpushed"], [])

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

    def test_a_manager_with_no_watch_hears_it_and_the_person_too(self):
        # #1898: on 2026-10-09 a manager ended a turn and started no watch: for 16 minutes nothing could wake
        # it, and a blocked question waited 11.9 minutes.
        from river import cli
        core.manage(self.c, self.dir.name, "boss")
        alerts = lambda: [dict(r) for r in self.c.execute(
            "SELECT id, body, read_at FROM messages WHERE to_agent='mark' AND kind='alert' ORDER BY id")]
        t0 = core.now()
        at = lambda minutes: mock.patch.object(core, "now", side_effect=lambda: t0 + timedelta(minutes=minutes))
        # A manager that never ran a watch (a manager in a chat) is not late.
        with at(30):
            core.send(self.c, "question", "which one?", to="boss", actor="w1", blocked=True)
            self.assertIsNone(core.watch_gap(self.c, "boss"))
            self.assertEqual(core.tell_watch_late(self.c), {"late": {}, "told": [], "closed": []})
            w = core.manage_watch(self.c, "boss", step="1h", sleep=lambda s: self.fail("a blocked message does not wait"))
            self.assertEqual(len(w["messages"]), 1)
            self.assertIsNone(core.watch_gap(self.c, "boss"))  # the watch ran until now
        # Two minutes after its watch ended, each command of the manager says so. No other agent hears it.
        with at(32):
            self.assertEqual(core._short(core.watch_gap(self.c, "boss")), "2m")
            self.assertIsNone(core.watch_gap(self.c, "w1"))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                cli._footer(self.c, "boss")
                cli._footer(self.c, "w1")
            self.assertEqual(err.getvalue().count("no watch runs"), 1)
            self.assertIn("no watch runs for 2m: start maxpm --as boss manage --watch in the background", err.getvalue())
            self.assertIsNone(core.manager_view(self.c)["no_watch"])
        # After WATCH_LATE with nothing due, the page says it and the person gets no alert. A note waits its level first.
        self.assertEqual(core.WATCH_LATE, timedelta(minutes=5))
        with at(36):
            core.send(self.c, "note", "#12 is on main", to="boss", actor="w1")
            self.assertEqual(core.manager_view(self.c)["no_watch"], "6m")
            self.assertEqual(core.tell_watch_late(self.c), {"late": {}, "told": [], "closed": []})
        with at(47):  # the note is due (manage_wait_high, 10m)
            r = core.tell_watch_late(self.c)
            self.assertEqual((r["late"], r["told"], r["closed"]), ({"boss": 1}, ["boss"], []))
            (a,) = alerts()
            self.assertTrue(a["body"].startswith("runs no watch (maxpm manage --watch) for 17m: 1 message waits for it. "), a["body"])
            self.assertIn("maxpm --as boss manage --watch", a["body"])
            self.assertEqual(core.prompt_alert_agent(self.c, a["id"]), "boss")  # the link opens its Terminal
            self.assertTrue(any(e.get("message_id") == a["id"] for e in core.needs_you(self.c)))
            # One alert for each time with no watch, also when more comes.
            core.send(self.c, "question", "may I run the probe?", to="boss", actor="w2", blocked=True)
            self.assertEqual(core.tell_watch_late(self.c), {"late": {"boss": 2}, "told": [], "closed": []})
        # The watch runs again: the alert closes.
        with at(48):
            self.assertEqual(len(core.manage_watch(self.c, "boss", step="0s", sleep=lambda s: None)["messages"]), 2)
            self.assertEqual(core.tell_watch_late(self.c), {"late": {}, "told": [], "closed": [a["id"]]})
            self.assertIsNotNone(alerts()[0]["read_at"])
            self.assertFalse(any(e.get("message_id") == a["id"] for e in core.needs_you(self.c)))
        # The next time, a blocked message is in the alert; a manager that is stopped gets none.
        with at(60):
            core.send(self.c, "alert", "the build is red", to="boss", actor="w1", blocked=True)
            self.assertEqual(core.tell_watch_late(self.c)["told"], ["boss"])
            self.assertIn("for 12m: 1 message waits for it, 1 blocked (the sender stands still). ", alerts()[1]["body"])
            core.stop_agent(self.c, "boss", "done for the day", actor="mark")
            self.assertEqual(core.tell_watch_late(self.c), {"late": {}, "told": [], "closed": [alerts()[1]["id"]]})

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
        self.c.execute("DELETE FROM meta WHERE key LIKE 'schema:%'")  # an old queue has no schema id of this code (#5396)
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
        # The owner is the manager: a ship request wakes it at once (#4289, mark 2026-11-04), one notice.
        self.assertEqual([(m["kind"], m["level"]) for m in got], [("notice", "now")])
        core.manage(self.c, self.dir.name, "boss")  # the briefing shows the findings of now; the watch starts after it
        w, minutes = self.watch_minutes(step="2h")
        self.assertEqual((w["result"], minutes), ("messages", 0))
        # Another owner: it gets the low notice, and the manager gets one that wakes it at once.
        core.target_give(self.c, "web", "w2", "mark")
        core.ship(self.c, self.add("a", "footer"), "w1")
        self.assertEqual(sorted((m["to_agent"], m["level"]) for m in self.c.execute(
            "SELECT to_agent, level FROM messages WHERE body LIKE 'ship request%footer%'")), [("boss", "now"), ("w2", "low")])
        core.inbox(self.c, "boss")

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
        core.config_set(self.c, "question_hold", "0s")  # the person has the item at once, not the manager (#5311)
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
        self.c.execute("DELETE FROM meta WHERE key LIKE 'schema:%'")  # an old queue has no schema id of this code (#5396)
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
            self.assertIn("target site is now web: 1 projects and 0 deploy and review items follow", out)
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
        with open(os.path.join(self.dir.name, "locked.log"), encoding="utf-8") as f:
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


class Operational(Base):
    """An operational task (#4973): work on the operation of the system, with its own default model, that starts
    in a new session; a trigger adds a cleanup of a person's old list."""

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "a", path=self.dir.name)
        for n in ("w1", "w2"):
            core.register(self.c, n)
        core.register(self.c, "mark", human=True)

    def test_its_default_model_comes_before_the_global_default(self):
        core.config_set(self.c, "default_model", "opus")
        core.config_set(self.c, "default_model", "fable", project="a")
        op = core.item_add(self.c, "a", "Reorder the graph of the a project", kind="operational", actor="mark")["id"]
        w = self.add("a", "build the page")
        ann = core.annotate(self.c)
        self.assertEqual((ann[op]["kind"], ann[op]["model"], ann[op]["effort"]), ("operational", "sonnet", "medium"))
        self.assertEqual(ann[w]["model"], "fable")
        core.config_set(self.c, "default_model", "opus", kind="operational")  # a setting of the kind still wins
        self.assertEqual(core.annotate(self.c)[op]["model"], "opus")
        with self.assertRaisesRegex(RiverError, "an operational task is for an agent session"):
            core.item_add(self.c, "a", "x", doer="human", kind="operational")
        with self.assertRaisesRegex(RiverError, "the kind of a task is work or operational"):
            core.item_add(self.c, "a", "x", kind="deploy")
        core.item_edit(self.c, w, kind="operational", actor="mark")
        self.assertEqual(core._item(self.c, w)["kind"], "operational")
        core.item_edit(self.c, w, kind="work", actor="mark")
        self.assertEqual(core._item(self.c, w)["kind"], "work")

    def test_after_its_operational_task_a_session_takes_no_work_of_another_kind(self):
        op = core.item_add(self.c, "a", "Find the cause of the failing checks", kind="operational")["id"]
        w = self.add("a", "Build the error counts")
        core.claim(self.c, op, "w1")
        core.done(self.c, op, "the cause", "w1")
        skipped = []
        self.assertEqual(core.next_item(self.c, None, claim=True, actor="w1", limit=5, skipped=skipped), [])
        self.assertIn((w, f"this session started for operational task #{op} on its own model; other work starts in "
                          f"a new session"), [(i["id"], i["why"]) for i in skipped])
        self.assertEqual(core.next_item(self.c, None, claim=True, actor="w2")[0]["id"], w)  # another session takes it

    def test_it_starts_in_a_new_session(self):
        op = core.item_add(self.c, "a", "Read the production logs for the failed login", kind="operational")["id"]
        x = self.add("a", "other work")
        core.claim(self.c, x, "w1")
        core.done(self.c, x, "done", "w1")
        skipped = []
        self.assertEqual(core.next_item(self.c, None, claim=True, actor="w1", limit=5, skipped=skipped), [])
        self.assertIn((op, "an operational task starts in a new session; this one held a task"),
                      [(i["id"], i["why"]) for i in skipped])
        with self.assertRaisesRegex(RiverError, f"operational task #{op} starts in a new session"):
            core.claim(self.c, op, "w1")
        core.wait(self.c, self.dir.name, "w1", step="0s", sleep=lambda s: None)
        self.assertIsNone(core.waiting_agent_for(self.c, "a", op))
        b = core.go(self.c, self.dir.name, "w2")
        self.assertEqual(b["item"]["id"], op)

    def old_items(self, n, days=10):
        ids = [self.add("a", f"decide thing {i}", doer="human") for i in range(n)]
        with core.tx(self.c):
            self.c.execute("UPDATE items SET created_at=? WHERE doer='human'", (core.iso(core.now() - timedelta(days=days)),))
        return ids

    def test_a_long_old_list_gets_one_cleanup_task(self):
        ids = self.old_items(9)
        core.needs_you(self.c, "mark")
        self.assertEqual(core.cleanup_trigger(self.c), [])  # 9 < list_cleanup_min (10)
        ids += self.old_items(1)
        core.needs_you(self.c, "mark")
        added = core.cleanup_trigger(self.c)
        self.assertEqual(len(added), 1)
        it = core.item_show(self.c, added[0])
        self.assertEqual((it["kind"], it["project"], it["doer"]), ("operational", "a", "ai"))
        self.assertTrue(it["title"].startswith("Clean up the list of mark: 10 items older than 7d"))
        self.assertIn(f"#{ids[0]}", it["context"])
        self.assertIn("maxpm done|drop <id> --note", it["context"])
        self.assertEqual(core.cleanup_trigger(self.c), [])  # one at a time
        core.claim(self.c, added[0], "w2")
        core.done(self.c, added[0], "closed 4, kept 6", "w2")
        self.assertEqual(core.cleanup_trigger(self.c), [])  # and at most one in each list_cleanup_after
        core.config_set(self.c, "list_cleanup_after", "0s")
        self.assertEqual(core.cleanup_trigger(self.c), [])


class ReviewPin(Base):
    """review pass pins the commit it checked (#5498): release #5486 of toolscaled-prod was cut by a reviewer that
    took its review, with no revision; the review passed at one commit, the main tree moved one commit on, and the
    deploy hook, with no MAXPM_REV, released the newer commit."""

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
                              capture_output=True, text=True, check=True).stdout.strip()

    def commit(self, text):
        with open(os.path.join(self.repo, "f.txt"), "w", encoding="utf-8") as f:
            f.write(text)
        self.git("add", "f.txt")
        self.git("commit", "-q", "-m", text)
        return self.git("rev-parse", "HEAD")

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.dir.name, "repo")
        os.mkdir(self.repo)
        self.git("init", "-q")
        self.a = self.commit("a")
        core.target_add(self.c, "web", "push")
        core.project_add(self.c, "site", target="web", path=self.repo)
        for n in ("dev", "rev", "ops"):
            core.register(self.c, n)
        core.register(self.c, "mark", human=True)
        core.config_set(self.c, "review", "on")
        core.target_own(self.c, "web", "ops")
        core.target_hook(self.c, "web", "deploy", f'"{sys.executable}" -c "print(1)"', actor="mark")
        x = self.add("site", "page")
        core.claim(self.c, x, "dev")
        core.done(self.c, x, "commit", "dev", ship_it=True)
        ids = core.item_show(self.c, x)["unblocks"]
        self.review = [i for i in ids if core.item_show(self.c, i)["kind"] == "review"][0]
        self.deploy = [i for i in ids if i != self.review][0]

    def test_the_review_commands_get_the_pin_as_maxpm_rev_and_the_brief_names_it(self):
        from river import cli
        core.target_cut(self.c, "web", rev=self.a, actor="ops")
        core.register(self.c, "rev2")
        b = core.go(self.c, self.repo, "rev2", role="reviewer")
        self.assertEqual((b["role"], b["item"]["id"]), ("reviewer", self.review))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.render_go(b)
        self.assertIn(f"pinned at {self.a}: review that commit; your review commands get it as MAXPM_REV", out.getvalue())
        seen = os.path.join(self.dir.name, "env.txt")
        core.item_edit(self.c, self.review, actor="rev2", check=f'"{sys.executable}" -c "import os; open({seen!r}, \'w\')'
                       f'.write(os.environ[\'MAXPM_REV\'] + \' \' + os.environ[\'MAXPM_REVIEW\'])"')
        core.review_pass(self.c, self.review, None, "rev2", cwd=self.repo)
        with open(seen, encoding="utf-8") as f:
            self.assertEqual(f.read(), f"{self.a} {self.review}")

    def deploy_rev(self):
        self.assertIsNotNone(core.deploy_hook_start(self.c, self.deploy, "tok"))
        return self.c.execute("SELECT rev FROM hook_runs WHERE event='deploy' ORDER BY id DESC LIMIT 1").fetchone()[0]

    def test_a_review_with_no_pin_pins_the_commit_it_checked_and_the_deploy_gets_it(self):
        core.claim(self.c, self.review, "rev")  # the cut: no revision
        self.assertIsNone(core._item(self.c, self.deploy)["cut_rev"])
        res = core.review_pass(self.c, self.review, None, "rev")
        self.assertEqual(res["reviewed"], self.a)
        self.assertIn(f"reviewed {self.a[:12]}", res["output"])
        self.assertEqual(core._item(self.c, self.deploy)["cut_rev"], self.a)
        self.commit("b")  # the main tree moves on after the review
        self.assertEqual(self.deploy_rev(), self.a)  # MAXPM_REV: the reviewed commit, not HEAD

    def test_a_folder_that_left_the_pin_is_refused_until_a_step_brings_it_back(self):
        core.claim(self.c, self.review, "rev")
        core.target_cut(self.c, "web", rev=self.a, actor="ops")  # the owner pins a
        b = self.commit("b")
        with self.assertRaisesRegex(RiverError, f"is at {b[:12]}, but the release is pinned at {self.a[:12]}"):
            core.review_pass(self.c, self.review, None, "rev")
        self.assertEqual(core._item(self.c, self.review)["status"], "in_progress")
        with self.assertRaisesRegex(RiverError, f"pinned at {self.a[:12]}, and you name {b[:12]}"):
            core.review_pass(self.c, self.review, None, "rev", rev=b)
        with self.assertRaisesRegex(RiverError, "--rev nope is not a commit"):
            core.review_pass(self.c, self.review, None, "rev", rev="nope")
        # A gate step that checks out the pin itself (as the toolscaled review gate does) passes.
        core.item_edit(self.c, self.review, check=f"git checkout -q {self.a}", actor="rev")
        res = core.review_pass(self.c, self.review, None, "rev", cwd=self.repo)
        self.assertEqual(res["reviewed"], self.a)
        self.assertEqual(core.item_show(self.c, self.review)["release_rev"], self.a)
        self.assertEqual(core._item(self.c, self.deploy)["cut_rev"], self.a)
        self.assertEqual(self.deploy_rev(), self.a)


class DeployHook(Base):
    """A target with a deploy hook releases with no session that types its command: maxpm serve runs the hook when
    the deploy item is ready; a failure keeps the deploy item open and starts the release again (#4971)."""

    def setUp(self):
        super().setUp()
        core.target_add(self.c, "web", "push, then smoke test")
        core.project_add(self.c, "site", target="web", path=self.dir.name)
        for n in ("dev", "rev", "ops"):
            core.register(self.c, n)
        core.register(self.c, "mark", human=True)
        core.config_set(self.c, "review", "on")
        core.target_own(self.c, "web", "ops")
        a = self.add("site", "page")
        core.claim(self.c, a, "dev")
        core.done(self.c, a, "commit", "dev", ship_it=True)
        ids = core.item_show(self.c, a)["unblocks"]
        self.review = [i for i in ids if core.item_show(self.c, i)["kind"] == "review"][0]
        self.deploy = [i for i in ids if i != self.review][0]
        self.flag = os.path.join(self.dir.name, "fail")
        path = os.path.join(self.dir.name, "release.py")
        with open(path, "w", encoding="utf-8") as f:  # fails while the file 'fail' exists; prints one line
            f.write("import os, sys\n"
                    f"if os.path.exists({self.flag!r}):\n    print('migration 7 failed'); sys.exit(4)\n"
                    "print('released', os.environ['MAXPM_DEPLOY'])\n")
        self.cmd = f'"{sys.executable}" "{path}"'
        with self.assertRaisesRegex(RiverError, "only a person or the manager"):
            core.target_hook(self.c, "web", "deploy", self.cmd, actor="ops")
        core.target_hook(self.c, "web", "deploy", self.cmd, actor="mark")

    def run_deploy(self):
        """What maxpm serve does in one pass for a ready deploy item: start the hook, then run it (here, not in a thread)."""
        run = core.deploy_hook_start(self.c, self.deploy, "test-token")
        self.assertIsNotNone(run)
        a = core.annotate(self.c)[self.deploy]
        self.assertFalse(a["ready"])  # it waits while the hook runs: nobody else takes it
        self.assertIn("the deploy hook of target web runs it", a["blocked_text"])
        with self.assertRaisesRegex(RiverError, "the hook ends the wait"):
            core.unblock(self.c, self.deploy, "ops")
        return core.run_hooks(self.c, token="test-token")

    def pass_review(self, who):
        core.register(self.c, who)
        core.claim(self.c, self.review, who)
        core.review_pass(self.c, self.review, None, who)

    def test_no_session_takes_the_deploy_item_and_the_hook_closes_it(self):
        self.pass_review("rev")
        self.assertTrue(core.annotate(self.c)[self.deploy]["ready"])
        # The owner's go, next, wait and claim give it to no session; a person may still take it.
        b = core.go(self.c, self.dir.name, "ops", role="deployer")
        self.assertEqual(b["role"], "idle")
        self.assertNotIn(self.deploy, [x["id"] for x in core.next_item(self.c, None, actor="ops", limit=9)])
        with self.assertRaisesRegex(RiverError, "target web has a deploy hook: maxpm serve runs the release"):
            core.claim(self.c, self.deploy, "ops")
        self.assertNotIn(f"#{self.deploy}", core._work_for(self.c, "ops", ["site"], role="deployer") or "")
        self.assertIsNone(core.deploy_hook_refusal(self.c, core._item(self.c, self.deploy), "mark"))
        r = self.run_deploy()
        self.assertEqual([(h["event"], h["ok"], h["deploy_end"]) for h in r], [("deploy", True, "done")])
        it = core.item_show(self.c, self.deploy)
        self.assertEqual((it["status"], it["assignee"], it["blocked_reason"]), ("done", None, None))
        self.assertIn(f"deploy hook: exit 0", it["output"])
        self.assertIn(f"(released {self.deploy})", it["output"])
        self.assertTrue(core._item(self.c, self.deploy)["cut_at"])  # the review cut it
        self.assertIsNone(core.deploy_hook_start(self.c, self.deploy, "x"))  # once

    def test_a_failure_keeps_the_deploy_open_and_starts_the_release_again(self):
        open(self.flag, "w", encoding="utf-8").close()
        self.pass_review("rev")
        r = self.run_deploy()
        self.assertEqual([(h["ok"], h["deploy_end"], h["alerted"]) for h in r], [(False, "failed", "ops")])
        fix, rv2 = r[0]["fix"], r[0]["review"]
        dep = core.item_show(self.c, self.deploy)
        self.assertEqual((dep["status"], dep["blocked_reason"]), ("open", None))
        self.assertIsNone(core._item(self.c, self.deploy)["cut_at"])  # the release starts again from its beginning
        self.assertIn(fix, dep["waits_on"])
        self.assertIn(rv2, dep["waits_on"])
        self.assertNotEqual(rv2, self.review)
        f = core.item_show(self.c, fix)
        self.assertEqual((f["project"], f["doer"], f["found_during"]), ("site", "ai", self.deploy))
        self.assertIn("migration 7 failed", f["context"])
        self.assertIn("Do not run the release command again", f["context"])
        self.assertEqual(core.item_show(self.c, rv2)["waits_on"], [fix])  # the new review covers the fix
        alert = [m for m in core.inbox(self.c, "ops") if "deploy hook of target web failed" in m["body"]]
        self.assertTrue(alert and "Do not run the command by hand" in alert[0]["body"])
        # The fix, a new review by a new session (it cuts the release again), then the hook runs again.
        os.remove(self.flag)
        core.claim(self.c, fix, "dev")
        core.done(self.c, fix, "fixed the migration", "dev")
        self.assertFalse(core.annotate(self.c)[self.deploy]["ready"])
        self.review = rv2
        self.pass_review("rev2")
        self.assertTrue(core._item(self.c, self.deploy)["cut_at"])
        r = self.run_deploy()
        self.assertEqual([(h["ok"], h["deploy_end"]) for h in r], [(True, "done")])
        self.assertEqual(core._item(self.c, self.deploy)["status"], "done")

    def test_with_no_review_the_hook_cuts_the_release_and_its_cut_hook_runs_first(self):
        core.drop(self.c, self.review, note="no review for this one", actor="mark")
        core.target_hook(self.c, "web", "release-cut", f'"{sys.executable}" -c "print(1)"', actor="mark")
        self.assertTrue(core.annotate(self.c)[self.deploy]["ready"])
        r = self.run_deploy()
        self.assertEqual([h["event"] for h in r], ["release-cut", "deploy"])
        self.assertEqual(core._item(self.c, self.deploy)["status"], "done")

    def test_a_run_that_never_ended_counts_as_failed(self):
        self.pass_review("rev")
        core.deploy_hook_start(self.c, self.deploy, "gone")
        with core.tx(self.c):  # maxpm serve took it and stopped while it ran
            self.c.execute("UPDATE hook_runs SET started_at=? WHERE event='deploy'",
                           (core.iso(core.now() - timedelta(hours=3)),))
        self.assertEqual(core.deploy_hook_lost(self.c), [self.deploy])
        dep = core.item_show(self.c, self.deploy)
        self.assertEqual((dep["status"], dep["blocked_reason"]), ("open", None))
        self.assertTrue(any("did not end" in e["change"] for e in dep["events"]))
        self.assertEqual(core.deploy_hook_lost(self.c), [])

    def _dead_runner(self):
        import subprocess
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        return f"{p.pid}:gone"

    def _took(self, run, runner, pid=None, ago=0):
        """The run as a process of maxpm serve took it: started ago seconds back, by runner."""
        with core.tx(self.c):
            self.c.execute("UPDATE hook_runs SET started_at=?, runner=?, pid=? WHERE id=?",
                           (core.iso(core.now() - timedelta(seconds=ago)), runner, pid, run))

    def test_a_deploy_hook_whose_process_ended_is_cut_at_once_and_runs_again(self):
        """#5654: a restart of maxpm serve ended the process that ran the deploy hook. The next pass sees it at once
        (not after deploy_timeout), and the release runs its deploy again by the normal path: no fix item."""
        self.pass_review("rev")
        run = core.deploy_hook_start(self.c, self.deploy, "gone")
        self._took(run, self._dead_runner())
        self.assertEqual(core.hooks_running(self.c), [])
        self.assertEqual([(c["run"], c["event"], c["again"]) for c in core.hook_runs_cut(self.c)],
                         [(run, "deploy", "deploy")])
        row = self.c.execute("SELECT * FROM hook_runs WHERE id=?", (run,)).fetchone()
        self.assertEqual((bool(row["ended_at"]), row["output"]), (True, core.HOOK_CUT))
        dep = core.item_show(self.c, self.deploy)
        self.assertEqual((dep["status"], dep["blocked_reason"]), ("open", None))
        self.assertTrue(any("it runs again from its beginning" in e["change"] for e in dep["events"]))
        self.assertFalse([i for i in dep["waits_on"] if core._item(self.c, i)["title"].startswith("Fix the failed")])
        self.assertTrue(core.annotate(self.c)[self.deploy]["ready"])
        self.assertEqual(core.hook_runs_cut(self.c), [])  # once
        run2 = core.deploy_hook_start(self.c, self.deploy, "again")
        self.assertNotEqual(run2, run)
        r = core.run_hooks(self.c, token="again")
        self.assertEqual([(h["ok"], h["deploy_end"]) for h in r], [(True, "done")])
        self.assertTrue(self.c.execute("SELECT runner FROM hook_runs WHERE id=?", (run2,)).fetchone()[0]
                        .startswith(f"{os.getpid()}:"))  # each run records the process that runs it
        self.assertTrue(self.c.execute("SELECT pid FROM hook_runs WHERE id=?", (run2,)).fetchone()[0])

    def test_a_run_counts_as_running_while_its_process_lives(self):
        self.pass_review("rev")
        run = core.deploy_hook_start(self.c, self.deploy, "x")
        self._took(run, core.OPS_PROCESS)
        self.assertEqual([r["id"] for r in core.hooks_running(self.c)], [run])
        self.assertEqual(core.hook_runs_cut(self.c), [])
        # serve started again with exec: the same pid, a new token. The loop's process knows serve's new one.
        other = os.getppid()
        self._took(run, f"{other}:old")
        self.assertEqual(len(core.hooks_running(self.c)), 1)  # its pid lives
        self.assertEqual(core.hooks_running(self.c, serve=f"{other}:new"), [])
        self.assertEqual(len(core.hooks_running(self.c, serve=f"{other}:old")), 1)
        self._took(run, f"{os.getpid()}:old")  # this process's own pid with another token: cut
        self.assertEqual(core.hooks_running(self.c), [])
        # A run of before #5654 has no runner: cut when it started before this serve did.
        self._took(run, None, ago=60)
        self.assertEqual(len(core.hooks_running(self.c)), 1)
        self.assertEqual(len(core.hooks_running(self.c, boot=core.iso(core.now() - timedelta(minutes=2)))), 1)
        self.assertEqual(core.hooks_running(self.c, boot=core.iso(core.now())), [])
        # Past its timeout and DEPLOY_HOOK_LOST it holds no restart: deploy_hook_lost ends it.
        self._took(run, core.OPS_PROCESS, ago=3 * 3600)
        self.assertEqual(core.hooks_running(self.c), [])

    def test_three_cuts_in_a_row_count_as_a_failed_deploy(self):
        self.pass_review("rev")
        for n in range(3):
            run = core.deploy_hook_start(self.c, self.deploy, f"t{n}")
            self.assertIsNotNone(run)
            self._took(run, self._dead_runner())
            cut = core.hook_runs_cut(self.c)
            self.assertEqual(cut[0]["again"], "deploy" if n < core.HOOK_CUTS_MAX else None)
        dep = core.item_show(self.c, self.deploy)
        fix = [i for i in dep["waits_on"] if core._item(self.c, i)["title"].startswith("Fix the failed deploy")]
        self.assertEqual(len(fix), 1)
        self.assertIn("3 times in a row", core._item(self.c, fix[0])["title"])

    def test_the_process_group_of_a_cut_hook_stops(self):
        """The hook's shell outlived the process that ran it (its parent ended, as at a restart of serve): the cut
        ends its process group before the deploy runs again, so two deploys never run at the same time."""
        import subprocess
        import time as _time

        def orphan():  # a process group whose parent ended: nothing of this test keeps it as a zombie
            out = subprocess.run([sys.executable, "-c", "import subprocess as s; p = s.Popen(['sleep', '60'], "
                                  "start_new_session=True, stdout=s.DEVNULL, stderr=s.DEVNULL); print(p.pid)"],
                                 capture_output=True, text=True, timeout=30)
            pid = int(out.stdout.strip())
            self.addCleanup(lambda: core.pid_alive(pid) and os.kill(pid, 9))
            return pid
        self.pass_review("rev")
        run = core.deploy_hook_start(self.c, self.deploy, "x")
        pid = orphan()
        self._took(run, self._dead_runner(), pid=pid)
        self.assertEqual(len(core.hook_runs_cut(self.c)), 1)
        for _ in range(100):
            if not core.pid_alive(pid):
                break
            _time.sleep(0.05)
        self.assertFalse(core.pid_alive(pid))
        # A pid that leads another process now (it started after the run) is not the hook's: it is left alone.
        pid = orphan()
        run = core.deploy_hook_start(self.c, self.deploy, "y")
        self._took(run, self._dead_runner(), pid=pid, ago=600)
        with mock.patch.object(core, "PS_LEADER_RUNNER", lambda p: _time.strftime("%a %b %d %H:%M:%S %Y")):
            self.assertEqual(len(core.hook_runs_cut(self.c)), 1)
        self.assertTrue(core.pid_alive(pid))

    def test_another_hook_that_was_cut_runs_again_once(self):
        self.pass_review("rev")
        cmd = f'"{sys.executable}" -c "print(1)"'
        with core.tx(self.c):
            run = self.c.execute("INSERT INTO hook_runs(target,event,command,item_id,deploy_id,actor,queued_at,"
                                 "queued_by) VALUES ('web','release-cut',?,?,?,'rev',?,'x')",
                                 (cmd, self.deploy, self.deploy, core.iso(core.now()))).lastrowid
        self._took(run, self._dead_runner())
        again = core.hook_runs_cut(self.c)[0]["again"]
        row = self.c.execute("SELECT * FROM hook_runs WHERE id=?", (again,)).fetchone()
        self.assertEqual((row["queued_by"], row["started_at"]), (f"cut:{run}", None))
        self.assertLess(row["queued_at"], core.iso(core.now() - timedelta(seconds=core.HOOK_ORPHAN_AFTER)))
        self._took(again, self._dead_runner())  # cut again: it stays ended, and the owner hears of it
        self.assertIsNone(core.hook_runs_cut(self.c)[0]["again"])
        self.assertTrue([m for m in core.inbox(self.c, "ops") if "was cut twice" in m["body"]])

    def test_a_deploy_hook_is_stopped_at_deploy_timeout(self):
        core.config_set(self.c, "deploy_timeout", "90m")
        seen = []

        def runner(cmd, folder, limit, renew, env):
            seen.append(limit)
            return mock.Mock(returncode=None, timed_out=True, stdout="", stderr="")
        with mock.patch.object(core, "HOOK_RUNNER", runner):
            self.pass_review("rev")
            r = self.run_deploy()
        self.assertEqual(seen, [5400])
        self.assertIn("stopped at deploy_timeout (90m)", r[0]["result"])
        self.assertEqual(r[0]["deploy_end"], "failed")

    def test_the_managers_watch_hears_of_the_start_and_of_a_failure_the_owner_has(self):
        """#5382: the deploy hook's start comes with the watch's next wake (low); its failure wakes it at once, also
        when the alert went to the target's owner."""
        core.register(self.c, "boss")
        core.manage(self.c, self.dir.name, "boss")
        open(self.flag, "w", encoding="utf-8").close()
        self.pass_review("rev")
        w = core.manage_watch(self.c, "boss", step="0s", sleep=lambda s: None)  # the pass wakes it (#4289)
        self.assertTrue(any("passed" in m["body"] for m in w["messages"]))
        core.deploy_hook_start(self.c, self.deploy, "test-token")
        t0, naps = core.now(), []
        with mock.patch.object(core, "now", side_effect=lambda: t0 + timedelta(minutes=len(naps))):
            w = core.manage_watch(self.c, "boss", step="2h", sleep=naps.append)
        self.assertEqual((w["result"], len(naps)), ("messages", 30))  # manage_wait_low: the start alone does not wake it
        self.assertIn(f"the deploy hook of target web started for deploy #{self.deploy}", w["messages"][0]["body"])
        r = core.run_hooks(self.c, token="test-token")
        self.assertEqual((r[0]["deploy_end"], r[0]["alerted"]), ("failed", "ops"))
        w = core.manage_watch(self.c, "boss", step="2h", sleep=lambda s: self.fail("the failure wakes it at once"))
        body = w["messages"][0]["body"]
        self.assertIn(f"the deploy hook of target web failed (exit 4", body)
        self.assertIn(f"ops has the alert. The release starts again: fix #{r[0]['fix']}, then a new review #{r[0]['review']}", body)



class ReleaseGate(DeployHook):
    """#5003: the release gate (release_approval review|person) and the cadence of a target are set for each project
    from its needs; with person, maxpm serve asks one yes for each ready release, and the deploy waits on it."""

    def approvals(self):
        return [r["id"] for r in core._approvals(self.c, self.deploy)]

    def test_the_setting_is_per_project_only(self):
        for bad in ({}, {"item": self.deploy}, {"agent": "ops"}):
            with self.assertRaisesRegex(RiverError, "set for each project from its needs"):
                core.config_set(self.c, "release_approval", "person", **bad)
        with self.assertRaisesRegex(RiverError, "review .* or person"):
            core.config_set(self.c, "release_approval", "always", project="site")
        self.assertEqual(core.release_approval(self.c, "web"), {"approval": "", "project": None})
        core.config_set(self.c, "release_approval", "person", project="site")
        self.assertEqual(core.release_approval(self.c, "web"), {"approval": "person", "project": "site"})
        r = core.config_set(self.c, "release_approval", "review", project="deploy-web")
        self.assertEqual(r["also"], "review=on")
        self.assertEqual(core.setting(self.c, "review", project_id=core._deploy_project_id(self.c, "web")), "on")
        self.assertEqual(core.release_approval(self.c, "web"), {"approval": "review", "project": "deploy-web"},
                         "the deploy project's value comes first")

    def test_person_a_yes_for_each_release_then_the_hook_runs(self):
        core.config_set(self.c, "release_approval", "person", project="site")
        self.pass_review("rev")
        self.assertIsNone(core.deploy_hook_start(self.c, self.deploy, "x"), "no release without the yes")
        asked = core.release_yes_pass(self.c)
        self.assertEqual(list(asked), [self.deploy])
        yes = asked[self.deploy]
        it = core.item_show(self.c, yes)
        self.assertEqual((it["doer"], it["project"]), ("human", "deploy-web"))
        self.assertTrue(it["title"].startswith("Approve release web: 1 item(s)"))
        self.assertIn("It ships:", it["context"])
        self.assertIn(f"Its review passed: #{self.review}", it["context"])
        self.assertFalse(core.annotate(self.c)[self.deploy]["ready"])
        self.assertEqual(core.release_yes_pass(self.c), {}, "it asks once")
        self.assertIn(yes, [n["item_id"] for n in core.needs_you(self.c) if n.get("item_id")])
        core.done(self.c, yes, "yes", "mark")
        self.assertTrue(core.annotate(self.c)[self.deploy]["ready"])
        self.assertIsNone(core.approval_refusal(self.c, core._item(self.c, self.deploy)))
        r = self.run_deploy()
        self.assertEqual([(h["ok"], h["deploy_end"]) for h in r], [(True, "done")])

    def test_a_failed_deploy_asks_again(self):
        core.config_set(self.c, "release_approval", "person", project="site")
        open(self.flag, "w", encoding="utf-8").close()
        self.pass_review("rev")
        yes = core.release_yes_pass(self.c)[self.deploy]
        core.done(self.c, yes, "yes", "mark")
        r = self.run_deploy()
        self.assertEqual(r[0]["deploy_end"], "failed")
        self.assertEqual(self.approvals(), [], "the changed release takes a new yes")
        os.remove(self.flag)
        core.claim(self.c, r[0]["fix"], "dev")
        core.done(self.c, r[0]["fix"], "fixed", "dev")
        core.register(self.c, "rev2")
        core.claim(self.c, r[0]["review"], "rev2")
        core.review_pass(self.c, r[0]["review"], None, "rev2")
        again = core.release_yes_pass(self.c)
        self.assertEqual(list(again), [self.deploy])
        self.assertNotEqual(again[self.deploy], yes)

    def test_person_without_a_hook_refuses_the_deployer_until_the_yes(self):
        core.target_hook(self.c, "web", "deploy", clear=True, actor="mark")
        core.config_set(self.c, "release_approval", "person", project="site")
        self.pass_review("rev")
        with self.assertRaisesRegex(RiverError, "takes a person's yes for each release"):
            core.claim(self.c, self.deploy, "ops")
        yes = core.release_yes_pass(self.c)[self.deploy]
        core.done(self.c, yes, "yes", "mark")
        self.assertEqual(core.claim(self.c, self.deploy, "ops")["assignee"], "ops")

    def test_the_cadence_has_a_reason_and_show_prints_both(self):
        import contextlib
        import io
        from river import cli
        core.target_cadence(self.c, "web", "2h", "mark", reason="a service with users: a fix is live soon")
        p = core.release_plan(self.c, "web")
        self.assertEqual((p["cadence"], p["cadence_reason"]), ("2h", "a service with users: a fix is live soon"))
        self.assertEqual(p["approval"]["approval"], "")
        core.config_set(self.c, "release_approval", "review", project="site")
        g = core.project_show(self.c, "site")["release_gate"]
        self.assertEqual((g["approval"], g["project"], g["cadence"]), ("review", "site", "2h"))
        from unittest import mock
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, {"MAXPM_DB": self.path}):
            cli.run(["-q", "--as", "mark", "target", "show", "web"])
            cli.run(["-q", "--as", "mark", "project", "show", "site"])
            cli.run(["-q", "--as", "mark", "target", "cadence", "web", "off", "--reason", "each push is a release"])
        text = out.getvalue()
        self.assertIn("release gate: a release goes out when its review passes (release_approval review, project site)", text)
        self.assertIn("(why: a service with users: a fix is live soon)", text)
        self.assertIn("release gate: ships when its review passes (set on site); cadence: 2h (a service with users", text)
        self.assertIn("release cadence: off (why: each push is a release)", text)
        self.assertEqual(core.release_plan(self.c, "web")["cadence_reason"], "each push is a release")

class Hooks(Base):
    """A hook is a command that MaximizePM runs when a release event of a target occurs (maxpm target hook)."""

    def setUp(self):
        super().setUp()
        core.target_add(self.c, "web", "push, then smoke test")
        core.project_add(self.c, "site", target="web", path=self.dir.name)
        for n in ("dev", "rev", "ops"):
            core.register(self.c, n)
        core.register(self.c, "mark", human=True)
        core.config_set(self.c, "review", "on")
        core.target_own(self.c, "web", "ops")
        a = self.add("site", "page")
        core.claim(self.c, a, "dev")
        core.done(self.c, a, "commit", "dev", ship_it=True)
        ids = core.item_show(self.c, a)["unblocks"]
        self.review = [i for i in ids if core.item_show(self.c, i)["kind"] == "review"][0]
        self.deploy = [i for i in ids if i != self.review][0]
        self.log = os.path.join(self.dir.name, "hook.log")

    def script(self, name, body):
        """A hook as a Python script, so the same command runs in sh and in cmd.exe (Windows)."""
        path = os.path.join(self.dir.name, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return f'"{sys.executable}" "{path}"'

    def recorder(self):
        """A hook that appends its environment and its folder to the log, and prints one line."""
        return self.script("record.py", "import json, os\n"
                           f"with open({self.log!r}, 'a') as f:\n"
                           "    f.write(json.dumps({k[6:].lower(): v for k, v in os.environ.items() if k in ('MAXPM_EVENT', "
                           "'MAXPM_TARGET', 'MAXPM_REV', 'MAXPM_ITEM', 'MAXPM_DEPLOY', 'MAXPM_AGENT')} | "
                           "{'cwd': os.path.realpath(os.getcwd())}) + '\\n')\n"
                           "print('recorded', os.environ['MAXPM_EVENT'])\n")

    def runs(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def test_each_release_event_runs_its_hook_once_with_the_environment(self):
        cmd = self.recorder()
        events = [e for e in core.HOOK_EVENTS if e != "deploy"]  # a deploy hook releases with no session (DeployHook)
        for event in events:
            core.target_hook(self.c, "web", event, cmd, actor="mark")
        here = os.path.realpath(self.dir.name)
        core.target_cut(self.c, "web", "abc1234", "ops")
        self.assertEqual(self.runs(), [])  # the hook runs after the transaction of the event, not inside it
        r = core.run_hooks(self.c)
        self.assertEqual([(h["event"], h["exit_code"], h["ok"], h["output"]) for h in r],
                         [("release-cut", 0, True, "recorded release-cut")])
        self.assertEqual(core.run_hooks(self.c), [])  # once
        core.claim(self.c, self.review, "rev")
        self.assertEqual(core.run_hooks(self.c), [])  # the release is cut already: the claim is no second cut
        fix = core.review_fail(self.c, self.review, ["null check"], actor="rev")["fixes"][0]["id"]
        self.assertEqual([h["event"] for h in core.run_hooks(self.c)], ["review-failed"])
        core.claim(self.c, fix, "dev")
        core.done(self.c, fix, "fixed", "dev")
        self.assertEqual(core.run_hooks(self.c), [])  # an item that is no review and no deploy is no release event
        core.claim(self.c, self.review, "rev")
        core.review_pass(self.c, self.review, None, "rev")
        self.assertEqual([h["event"] for h in core.run_hooks(self.c)], ["review-passed"])
        core.claim(self.c, self.deploy, "ops")
        core.done(self.c, self.deploy, "release 7", "ops")
        self.assertEqual([h["event"] for h in core.run_hooks(self.c)], ["deployed"])
        self.assertEqual(core.run_hooks(self.c), [])
        d, v = str(self.deploy), str(self.review)
        want = [("release-cut", d, "ops"), ("review-failed", v, "rev"), ("review-passed", v, "rev"), ("deployed", d, "ops")]
        self.assertEqual([(x["event"], x["item"], x["agent"]) for x in self.runs()], want)
        for x in self.runs():  # every event of the release: its target, its deploy item, the pinned revision, the folder
            self.assertEqual((x["target"], x["deploy"], x["rev"], x["cwd"]), ("web", d, "abc1234", here))
        history = [e["change"] for e in core.item_show(self.c, self.deploy)["events"]]
        for event in events:  # one line for each run, on the deploy item
            self.assertEqual(len([c for c in history if c.startswith(f"hook {event}: exit 0, ")]), 1, history)
        self.assertEqual([m for m in core.inbox(self.c, "ops") if "hook" in m["body"]], [])  # no alert: none failed

    def test_a_cut_that_comes_with_a_claim_runs_the_hook_with_no_revision(self):
        core.target_hook(self.c, "web", "release-cut", self.recorder(), actor="mark")
        core.claim(self.c, self.review, "rev")  # a reviewer took the review: that is the cut
        self.assertEqual([h["event"] for h in core.run_hooks(self.c)], ["release-cut"])
        x = self.runs()[0]
        self.assertEqual((x["event"], x["item"], x["rev"], x["agent"]), ("release-cut", str(self.deploy), "", "rev"))

    def test_an_event_of_a_target_with_no_hook_queues_nothing(self):
        core.target_hook(self.c, "web", "deployed", self.recorder(), actor="mark")
        core.target_cut(self.c, "web", "abc1234", "ops")
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM hook_runs").fetchone()[0], 0)
        self.assertEqual(core.run_hooks(self.c), [])

    def test_a_busy_hook_runs_again_until_hook_timeout(self):
        """#5605: build-sweep.sh mark refused at 5 of 37 cuts because a check ran in the tree, and nothing ran it
        again. A hook that exits 75 (busy) is queued again; maxpm serve runs it after HOOK_ORPHAN_AFTER."""
        count = os.path.join(self.dir.name, "n")
        busy = self.script("busy.py", "import os, sys\n"
                           f"p = {count!r}\nn = int(open(p).read()) + 1 if os.path.exists(p) else 1\n"
                           "open(p, 'w').write(str(n))\nprint('check-all runs' if n < 3 else 'marked')\n"
                           "sys.exit(75 if n < 3 else 0)\n")
        core.target_hook(self.c, "web", "release-cut", busy, actor="mark")
        core.target_cut(self.c, "web", "abc1234", "ops")
        first = core.run_hooks(self.c)[0]
        self.assertEqual((first["exit_code"], first["ok"]), (75, None))
        self.assertIn("runs it again", first["result"])
        self.assertEqual([m for m in core.inbox(self.c, "ops") if "hook" in m["body"]], [])  # no failure yet
        self.assertEqual(core.run_hooks(self.c, orphans=True), [])  # it waits HOOK_ORPHAN_AFTER for serve

        def later():  # the queued run is old enough for the orphan pass of maxpm serve
            with core.tx(self.c):
                self.c.execute("UPDATE hook_runs SET queued_at=? WHERE started_at IS NULL",
                               (core.iso(core.now() - core.timedelta(seconds=core.HOOK_ORPHAN_AFTER + 1)),))
            return core.run_hooks(self.c, orphans=True)
        (second,) = later()
        self.assertEqual((second["id"], second["ok"]), (first["again"], None))
        (third,) = later()
        self.assertEqual((third["exit_code"], third["ok"], third["output"]), (0, True, "marked"))
        self.assertEqual(later(), [])
        history = [e["change"] for e in core.item_show(self.c, self.deploy)["events"] if e["change"].startswith("hook")]
        self.assertEqual(sum("busy (exit 75)" in h for h in history), 2, history)
        # Busy until hook_timeout from its first run: a failure, with the alert.
        os.remove(count)
        core.config_set(self.c, "hook_timeout", "1m")  # shorter than HOOK_ORPHAN_AFTER: no time for another run
        core.target_cut(self.c, "web", "abc1235", "ops")
        (r,) = core.run_hooks(self.c)
        self.assertEqual((r["ok"], r.get("again")), (False, None))
        self.assertIn("still busy", r["result"])
        self.assertTrue(any("still busy" in e["change"] for e in core.item_show(self.c, r["deploy"])["events"]))

    def test_a_failing_hook_does_not_stop_the_event_and_alerts_the_owner(self):
        bad = self.script("bad.py", "import sys\nprint('no build folder')\nsys.exit(3)\n")
        core.target_hook(self.c, "web", "release-cut", bad, actor="mark")
        core.target_hook(self.c, "web", "deployed", bad, actor="mark")
        res = core.target_cut(self.c, "web", "abc1234", "mark")  # a person cuts; ops owns the target
        self.assertTrue(res["cut"]["cut_at"])
        h = core.run_hooks(self.c)[0]
        self.assertEqual((h["ok"], h["exit_code"], h["timed_out"], h["alerted"]), (False, 3, False, "ops"))
        self.assertIn("no build folder", h["output"])
        self.assertTrue(core.item_show(self.c, self.deploy)["cut_at"])  # the event stands
        alerts = [m for m in core.inbox(self.c, "ops") if m["kind"] == "alert" and "hook release-cut" in m["body"]]
        self.assertEqual(len(alerts), 1)
        self.assertIn("exit 3", alerts[0]["body"])
        self.assertIn("no build folder", alerts[0]["body"])
        self.assertEqual(alerts[0]["item_id"], self.deploy)
        self.assertTrue(any(e["change"].startswith("hook release-cut: exit 3, ")
                            for e in core.item_show(self.c, self.deploy)["events"]))
        # The owner's own command: it reads the result in the reply, and gets no alert from itself.
        core.claim(self.c, self.review, "rev")
        core.review_pass(self.c, self.review, None, "rev")
        core.claim(self.c, self.deploy, "ops")
        self.assertEqual(core.done(self.c, self.deploy, "release 7", "ops")["status"], "done")
        h = core.run_hooks(self.c)[0]
        self.assertEqual((h["event"], h["ok"], h["alerted"]), ("deployed", False, None))
        self.assertEqual(core.item_show(self.c, self.deploy)["status"], "done")
        # With no owner, the manager gets the alert.
        core.target_release(self.c, "web", "ops")
        core.manage(self.c, self.dir.name, "boss")
        b = self.add("site", "form")
        core.claim(self.c, b, "dev")
        core.done(self.c, b, "commit", "dev", ship_it=True)
        core.target_cut(self.c, "web", None, "mark")
        self.assertEqual(core.run_hooks(self.c)[0]["alerted"], "boss")

    def sandboxed(self, argv, serve=None):
        """cli.run as an agent's command in its sandbox (SANDBOX_RUNTIME). serve: what cli.ask_server does
        (None: no maxpm serve answers). Returns the output."""
        from river import cli

        def ask(conn, op, args, actor=None, timeout=5, retries=3):
            if serve is None:
                raise RiverError("no maxpm serve answers on port 8765 (URLError): start it with `maxpm serve`")
            self.assertEqual(op, "run_hooks")
            return serve(conn, args, actor)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(cli, "ask_server", ask), \
                mock.patch.dict(os.environ, {"MAXPM_DB": self.path, "MAXPM_QUIET": "1", "SANDBOX_RUNTIME": "1"}):
            cli.run(argv)
        return out.getvalue()

    def test_a_command_in_a_sandbox_has_maxpm_serve_run_its_hook(self):
        """#1826: maxpm serve runs outside every sandbox; the command waits for the result and prints it."""
        from river import server
        bad = self.script("bad.py", "import sys\nprint('no build folder')\nsys.exit(3)\n")
        core.target_hook(self.c, "web", "release-cut", self.recorder(), actor="mark")
        core.target_hook(self.c, "web", "review-passed", bad, actor="mark")
        asked = []

        def serve(conn, args, actor):  # the op of the page, as maxpm serve runs it: a thread of its own
            asked.append((args["token"] == core.hook_token(), actor))
            with contextlib.redirect_stdout(io.StringIO()):
                return server.OPS["run_hooks"](conn, args, actor)
        out = self.sandboxed(["--as", "rev", "claim", str(self.review)], serve)
        self.assertEqual(asked, [(True, "rev")])
        self.assertIn("hook release-cut of target web: exit 0, ", out)
        self.assertIn("recorded release-cut", out)
        self.assertIn("maxpm serve ran it, outside the sandbox of this session", out)
        self.assertEqual([(x["event"], x["agent"]) for x in self.runs()], [("release-cut", "rev")])
        out = self.sandboxed(["--as", "rev", "review", "pass", str(self.review), "--output", "ok"], serve)
        self.assertIn("hook review-passed of target web: exit 3, ", out)
        self.assertIn("no build folder", out)
        self.assertIn("the hook failed; the event stands", out)
        self.assertNotIn("ran in the sandbox", out)  # it did not: serve ran it
        alert = [m for m in core.inbox(self.c, "ops") if "hook review-passed" in m["body"]][0]
        self.assertNotIn("sandbox", alert["body"])
        self.assertEqual(self.sandboxed(["--as", "rev", "who"], serve).count("hook "), 0)  # no event: no question
        self.assertEqual(len(asked), 2)

    def test_a_run_that_serve_does_not_start_runs_in_the_command(self):
        old = core.HOOK_TAKE_AFTER
        core.HOOK_TAKE_AFTER = 0.2
        self.addCleanup(setattr, core, "HOOK_TAKE_AFTER", old)
        core.target_hook(self.c, "web", "release-cut", self.recorder(), actor="mark")
        out = self.sandboxed(["--as", "rev", "claim", str(self.review)], lambda conn, args, actor: {"started": False})
        self.assertIn("hook release-cut of target web: exit 0, ", out)
        self.assertNotIn("maxpm serve ran it", out)
        self.assertEqual(len(self.runs()), 1)

    def test_only_a_person_or_the_manager_changes_a_hook(self):
        """#1829 C: maxpm serve runs a hook outside the sandbox, so a worker does not set one."""
        with self.assertRaisesRegex(RiverError, "dev is an agent; only a person or the manager sets"):
            core.target_hook(self.c, "web", "deployed", "scripts/x.sh", actor="dev")
        core.target_hook(self.c, "web", "deployed", "scripts/x.sh", actor="mark")
        for kw in ({"command": "scripts/y.sh"}, {"clear": True}):
            with self.assertRaisesRegex(RiverError, "only a person or the manager"):
                core.target_hook(self.c, "web", "deployed", actor="ops", **kw)  # the target owner too
        self.assertEqual([h["command"] for h in core.target_hook(self.c, "web", actor="dev")["hooks"]],
                         ["scripts/x.sh"])  # a look is for everyone
        self.assertEqual(core.target_hook(self.c, "web", "deployed", actor="dev")["changed"], None)
        core.manage(self.c, self.dir.name, "boss")
        self.assertEqual(core.target_hook(self.c, "web", "deployed", "scripts/y.sh", actor="boss")["changed"], "changed")
        self.assertEqual(core.target_hook(self.c, "web", "deployed", clear=True)["changed"], "removed")  # no name: a person
        from river import cli
        with mock.patch.dict(os.environ, {"MAXPM_DB": self.path, "CLAUDECODE": "1"}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            os.environ.pop("MAXPM_AGENT", None)
            with self.assertRaisesRegex(RiverError, "name yourself"):  # with no name in an agent session
                cli.dispatch(self.c, cli.build_parser().parse_args(["target", "hook", "web", "deployed", "x"]), None)

    def test_a_failure_in_the_sandbox_of_an_agent_session_says_so(self):
        bad = self.script("bad.py", "import sys\nprint('Operation not permitted')\nsys.exit(1)\n")
        core.target_hook(self.c, "web", "release-cut", bad, actor="mark")
        core.target_hook(self.c, "web", "deployed", self.recorder(), actor="mark")
        out = io.StringIO(self.sandboxed(["--as", "rev", "claim", str(self.review)]))  # no maxpm serve answers
        self.assertIn("it ran in the sandbox of this session: maxpm serve did not run it (no maxpm serve answers",
                      out.getvalue())
        self.assertIn("hook release-cut of target web: exit 1, ", out.getvalue())
        self.assertIn("ran in the sandbox of an agent session", out.getvalue())
        alert = [m for m in core.inbox(self.c, "ops") if "hook release-cut" in m["body"]][0]
        self.assertIn("the hook had the limits of that sandbox", alert["body"])
        self.assertTrue(any(e["change"].startswith("hook release-cut: exit 1, ") and "(in the sandbox of an agent session)"
                            in e["change"] for e in core.item_show(self.c, self.deploy)["events"]))
        core.review_pass(self.c, self.review, None, "rev")
        core.claim(self.c, self.deploy, "ops")
        core.done(self.c, self.deploy, "release 7", "ops")
        h = core.run_hooks(self.c, sandbox=True)[0]  # a hook that passes in the sandbox says nothing of it
        self.assertEqual((h["ok"], h["sandbox"]), (True, True))
        self.assertFalse(any("sandbox" in e["change"] and e["change"].startswith("hook deployed")
                             for e in core.item_show(self.c, self.deploy)["events"]))

    def test_a_hook_whose_folder_is_gone_fails_and_the_event_stands(self):
        core.target_hook(self.c, "web", "release-cut", self.recorder(), actor="mark")
        self.c.execute("UPDATE projects SET path=? WHERE name='site'", (os.path.join(self.dir.name, "gone"),))
        core.target_cut(self.c, "web", None, "mark")
        h = core.run_hooks(self.c)[0]
        self.assertEqual((h["ok"], h["exit_code"], h["result"], h["alerted"]), (False, None, "not started", "ops"))
        self.assertIn("does not exist", h["output"])
        self.assertEqual(self.runs(), [])

    def test_a_hook_past_hook_timeout_is_stopped_with_its_children(self):
        pidfile = os.path.join(self.dir.name, "child.pid")
        slow = self.script("slow.py", "import subprocess, sys\n"
                           "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                           f"open({pidfile!r}, 'w').write(str(p.pid))\n"
                           "print('started', flush=True)\n"
                           "p.wait()\n")
        core.target_hook(self.c, "web", "release-cut", slow, actor="mark")
        with self.assertRaisesRegex(RiverError, "30m"):
            core.config_set(self.c, "hook_timeout", "soon")
        core.config_set(self.c, "hook_timeout", "2s", project="deploy-web")  # the hooks of this target only
        self.assertEqual(core.target_hook(self.c, "web")["timeout"], "2s")
        old = core.REVIEW_RENEW_EVERY
        core.REVIEW_RENEW_EVERY = 0.3
        self.addCleanup(setattr, core, "REVIEW_RENEW_EVERY", old)
        core.target_cut(self.c, "web", None, "mark")
        t = time.monotonic()
        h = core.run_hooks(self.c)[0]
        self.assertLess(time.monotonic() - t, 20)
        self.assertEqual((h["ok"], h["timed_out"], h["alerted"]), (False, True, "ops"))
        self.assertIn("stopped at hook_timeout (2s)", h["result"])
        self.assertIn("started", h["output"])
        with open(pidfile, encoding="utf-8") as f:
            pid = int(f.read())
        for _ in range(40):
            if not core.pid_alive(pid):
                break
            time.sleep(0.05)
        else:
            self.fail("the hook's child process still runs")
        self.assertTrue(any(e["change"].startswith("hook release-cut: stopped at hook_timeout (2s)")
                            for e in core.item_show(self.c, self.deploy)["events"]))
        self.assertTrue(core.item_show(self.c, self.deploy)["cut_at"])

    def test_set_change_clear_and_list(self):
        self.assertEqual(core.target_hook(self.c, "web")["hooks"], [])
        r = core.target_hook(self.c, "web", "deployed", " scripts/after-release.sh ", actor="mark")
        self.assertEqual((r["changed"], [(h["event"], h["command"], h["last"]) for h in r["hooks"]]),
                         ("set", [("deployed", "scripts/after-release.sh", None)]))
        core.target_hook(self.c, "web", "release-cut", "scripts/prepare.sh", actor="mark")
        r = core.target_hook(self.c, "web", "deployed", "scripts/clean.sh", actor="mark")
        self.assertEqual(r["changed"], "changed")
        want = [("release-cut", "scripts/prepare.sh"), ("deployed", "scripts/clean.sh")]  # one for each event
        self.assertEqual([(h["event"], h["command"]) for h in core.target_hook(self.c, "web")["hooks"]], want)
        self.assertEqual([(h["event"], h["command"]) for h in core.target_show(self.c, "web")["hooks"]], want)
        self.assertEqual(core.target_hook(self.c, "web", "deployed")["changed"], None)  # an event alone: a look
        core.target_rename(self.c, "web", "www", "mark")  # the hooks follow a new name of the target
        self.assertEqual([(h["event"], h["command"]) for h in core.target_hooks(self.c, "www")], want)
        r = core.target_hook(self.c, "www", "release-cut", clear=True, actor="mark")
        self.assertEqual((r["changed"], [h["event"] for h in r["hooks"]]), ("removed", ["deployed"]))
        with self.assertRaisesRegex(RiverError, "has no release-cut hook"):
            core.target_hook(self.c, "www", "release-cut", clear=True, actor="mark")
        with self.assertRaisesRegex(RiverError, "a command or --clear, not both"):
            core.target_hook(self.c, "www", "deployed", "x", clear=True, actor="mark")
        with self.assertRaisesRegex(RiverError, "the command is empty"):
            core.target_hook(self.c, "www", "deployed", "  ", actor="mark")
        with self.assertRaisesRegex(RiverError, "no target 'nowhere'"):
            core.target_hook(self.c, "nowhere", "deployed", "x")

    def test_an_unknown_event_is_refused_with_the_names(self):
        with self.assertRaises(RiverError) as e:
            core.target_hook(self.c, "web", "item-done", "scripts/x.sh", actor="mark")
        self.assertEqual(str(e.exception), "no hook event 'item-done'; the events: release-cut, review-passed, "
                                           "review-failed, deployed, deploy")
        with self.assertRaisesRegex(RiverError, "no hook event 'cut'"):
            core.target_hook(self.c, "web", "cut", clear=True)
        self.assertEqual(core.target_hook(self.c, "web")["hooks"], [])

    def test_a_command_runs_the_hooks_of_its_own_events_and_serve_those_left_behind(self):
        core.target_hook(self.c, "web", "release-cut", self.recorder(), actor="mark")
        core.target_cut(self.c, "web", None, "ops")
        self.c.execute("UPDATE hook_runs SET queued_by='another-command'")
        self.assertEqual(core.run_hooks(self.c), [])  # the command that caused the event runs it
        self.assertEqual(core.run_hooks(self.c, orphans=True), [])  # and that command may still run
        self.assertEqual(len(core.run_hooks(self.c, token="another-command")), 1)
        self.c.execute("UPDATE hook_runs SET started_at=NULL, ended_at=NULL, queued_at='2000-01-01T00:00:00Z'")
        self.assertEqual(len(core.run_hooks(self.c, orphans=True)), 1)  # it ended before it ran the hook
        self.assertEqual(len(self.runs()), 2)

    def test_the_commands_print_the_result_and_the_briefings_name_the_hooks(self):
        from river import cli
        env = {"MAXPM_DB": self.path, "MAXPM_QUIET": "1"}

        def run(*words):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), mock.patch.dict(os.environ, env), \
                    mock.patch.object(cli, "in_sandbox", lambda: False):  # also when an agent runs the tests in one
                cli.run(list(words))
            return out.getvalue()
        bad = self.script("bad.py", "import sys\nprint('no build folder')\nsys.exit(3)\n")
        self.assertIn("hooks: none", run("target", "hook", "web"))
        text = run("--as", "mark", "target", "hook", "web", "release-cut", self.recorder())
        self.assertIn("hook release-cut of target web set", text)
        self.assertIn("review-passed: the review of the release is done", text)  # the events that have no hook
        run("--as", "mark", "target", "hook", "web", "deployed", bad)
        with self.assertRaisesRegex(RiverError, "no hook event 'cut'; the events: release-cut"):
            run("--as", "mark", "target", "hook", "web", "cut", "x")
        text = run("--as", "ops", "target", "cut", "web", "--rev", "abc1234")
        self.assertIn(f"release #{self.deploy} of web is cut", text)
        self.assertIn("hook release-cut of target web: exit 0, ", text)
        self.assertIn("    recorded release-cut", text)
        self.assertEqual([x["rev"] for x in self.runs()], ["abc1234"])
        text = run("target", "show", "web")
        self.assertIn(f"(last run for #{self.deploy}: exit 0, ", text)
        self.assertIn("deployed: ", text)
        m = core.manage(self.c, self.dir.name, "boss")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.render_manage(m)
        self.assertIn("hooks of target web: MaximizePM runs release-cut: ", out.getvalue())
        self.assertIn("Do not run these commands by hand", out.getvalue())
        core.claim(self.c, self.review, "rev")
        core.review_pass(self.c, self.review, None, "rev")
        b = core.go(self.c, self.dir.name, "ops")
        self.assertEqual((b["role"], b["item"]["id"]), ("deployer", self.deploy))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.render_go(b)
        self.assertIn("hooks of target web: MaximizePM runs release-cut: ", out.getvalue())
        text = run("--as", "ops", "--json", "done", str(self.deploy), "--output", "release 7")
        res = json.loads(text)
        self.assertEqual(res["status"], "done")  # a hook that fails does not refuse the done
        self.assertEqual([(h["event"], h["exit_code"], h["output"]) for h in res["hooks_run"]],
                         [("deployed", 3, "no build folder")])


class Backlog(Base):
    """An item that nobody plans to take now (#2072): kept with its links, never offered."""

    def setUp(self):
        super().setUp()
        self.web = os.path.join(self.dir.name, "web")
        os.makedirs(self.web)
        core.project_add(self.c, "web", path=self.web)
        core.register(self.c, "mark", human=True)
        core.register(self.c, "w1")

    def cli(self, *words):
        from river import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.dict(os.environ, {"MAXPM_DB": self.path, "MAXPM_QUIET": "1"}):
            cli.run(list(words))
        return out.getvalue()

    def test_a_backlog_item_is_kept_but_never_offered(self):
        x = self.add("web", "someday", 0)
        y = self.add("web", "now", 3)
        core.item_edit(self.c, x, notes="the notes stay", actor="t")
        r = core.backlog(self.c, x, "no plans this quarter", "mark")
        self.assertEqual(r["waiting"], [])
        a = core.annotate(self.c)[x]
        self.assertTrue(a["backlog"])
        self.assertFalse(a["ready"])
        self.assertEqual((a["status"], a["priority"], a["notes"]), ("open", 0, "the notes stay"))
        self.assertEqual([i["id"] for i in core.ready_list(self.c)], [y])
        self.assertEqual(core.capacity(self.c)["ready_for_agents"], [y])
        b = core.go(self.c, self.web, "w1")
        self.assertEqual(b["item"]["id"], y)
        with self.assertRaisesRegex(RiverError, rf"#{x} is in the backlog .*maxpm activate {x}"):
            core.claim(self.c, x, "w1")
        with self.assertRaisesRegex(RiverError, "maxpm activate"):
            core.launch_target(self.c, item=x)
        with self.assertRaisesRegex(RiverError, "in the backlog"):
            core.push(self.c, x, "w1", None, "mark")
        with self.assertRaisesRegex(RiverError, "in the backlog"):
            core.queue_add(self.c, "w1", x, actor="mark")
        self.assertIn("moved to the backlog: no plans this quarter",
                      [e["change"] for e in core.item_show(self.c, x)["events"]])

    def test_the_counts_show_the_backlog_apart(self):
        x = self.add("web", "someday")
        self.add("web", "now")
        core.backlog(self.c, x, "later", "mark")
        p = core.status(self.c)["projects"][0]
        self.assertEqual((p["open"], p["ready"], p["blocked"], p["backlog"]), (1, 1, 0, 1))
        self.assertEqual(core.project_show(self.c, "web")["counts"]["backlog"], 1)
        self.assertEqual(core.project_show(self.c, "web")["counts"]["open"], 1)
        lst = core.project_list(self.c)[0]
        self.assertEqual((lst["open_items"], lst["backlog_items"]), (1, 1))
        self.assertNotIn(x, [i["id"] for i in core.item_list(self.c)])
        self.assertIn(x, [i["id"] for i in core.item_list(self.c, include_closed=True)])
        self.assertIn("(1 open, 1 in backlog)", self.cli("project", "list"))
        self.assertRegex(self.cli("--as", "mark", "status"), r"backlog\nweb .* 1\n")

    def test_a_person_backlog_item_sends_no_notification_and_leaves_needs_you(self):
        x = self.add("web", "sign the contract", doer="human")
        core.sync_needs_you(self.c)
        self.assertEqual([n["item_id"] for n in core.needs_you(self.c)], [x])
        core.backlog(self.c, x, "not this year", "mark")
        with core.tx(self.c):
            core.sync_needs_you(self.c)
        self.assertEqual(core.needs_you(self.c), [])
        self.assertEqual(self.c.execute("SELECT close_reason FROM needs_you WHERE item_id=?", (x,)).fetchone()[0],
                         "moved to the backlog")
        # A due date of a backlog item warns nobody.
        self.c.execute("UPDATE items SET due=? WHERE id=?", (core.iso(core.now() - timedelta(hours=1)), x))
        core.activity(self.c, "mark")
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM messages WHERE kind='alert'").fetchone()[0], 0)
        self.assertEqual(core.status(self.c)["human_waiting"], [])

    def test_backlog_refuses_held_closed_and_release_items(self):
        x = self.add("web", "taken")
        core.claim(self.c, x, "w1")
        with self.assertRaisesRegex(RiverError, "Release it first"):
            core.backlog(self.c, x, "later", "mark")
        core.done(self.c, x, "did it", "w1")
        with self.assertRaisesRegex(RiverError, "is done"):
            core.backlog(self.c, x, "later", "mark")
        y = self.add("web", "open")
        with self.assertRaisesRegex(RiverError, "say why"):
            core.backlog(self.c, y, "", "mark")
        core.backlog(self.c, y, "later", "mark")
        with self.assertRaisesRegex(RiverError, "in the backlog already"):
            core.backlog(self.c, y, "later", "mark")
        with self.assertRaisesRegex(RiverError, "not in the backlog"):
            core.activate(self.c, x, "mark")

    def test_an_item_that_others_wait_on_needs_force_and_they_say_so(self):
        pre = self.add("web", "prerequisite")
        dep = self.add("web", "needs it", after=[pre])
        with self.assertRaisesRegex(RiverError, rf"prerequisite of open items: #{dep} needs it.*--force"):
            core.backlog(self.c, pre, "later", "mark")
        r = core.backlog(self.c, pre, "later", "mark", force="the dependent waits too")
        self.assertEqual(r["waiting"], [dep])
        a = core.annotate(self.c)
        self.assertFalse(a[dep]["ready"])  # still waits: the backlog item is not done
        self.assertEqual(a[dep]["backlog_blockers"], [pre])
        self.assertTrue(core.blockers(self.c, dep)["children"][0]["backlog"])
        self.assertIn(f"a backlog item: later; maxpm activate {pre}", self.cli("blockers", str(dep)))
        self.assertIn(f"waits on #{pre} (a backlog item)", self.cli("list"))
        with self.assertRaisesRegex(RiverError, "would wait on a backlog item"):
            core.item_add(self.c, "web", "x", blocks=dep, backlog="later")
        self.assertEqual(core.backlog_list(self.c)[0]["waited_by"], [dep])
        # Nothing in the project can move: go names the backlog item.
        b = core.go(self.c, self.web, "w1")
        self.assertEqual(b["waits_on_backlog"], [{"id": dep, "title": "needs it", "backlog": [pre]}])
        r = core.activate(self.c, pre, "mark", "now we need it")
        self.assertTrue(r["ready"])
        self.assertFalse(core.annotate(self.c)[pre]["backlog"])

    def test_add_backlog_list_and_activate_from_the_cli(self):
        out = self.cli("--as", "mark", "add", "web", "dark mode", "--backlog", "after the launch", "-p", "1")
        self.assertIn("backlog: after the launch", out)
        self.cli("--as", "mark", "add", "web", "plain idea", "--backlog")
        self.add("web", "now")
        out = self.cli("backlog")
        self.assertIn("web:\n  #1    P1 dark mode", out)
        self.assertIn("why not now: after the launch", out)
        self.assertIn("why not now: added to the backlog", out)
        rows = json.loads(self.cli("--json", "backlog", "--project", "web"))["backlog"]
        self.assertEqual([r["id"] for r in rows], [1, 2])
        self.assertIn("#1 dark mode: open again, ready.", self.cli("--as", "mark", "activate", "1"))
        self.assertIn("moved to the backlog (later)", self.cli("--as", "mark", "backlog", "3", "--note", "later"))
        self.assertEqual([r["id"] for r in core.backlog_list(self.c)], [2, 3])

    def test_a_goal_names_its_backlog_items_and_done_refuses_while_they_are_tagged(self):
        core.goal_add(self.c, "web", "launch", "the site is live", actor="t")
        x = core.item_add(self.c, "web", "polish", goals=["launch"], actor="t")["id"]
        core.backlog(self.c, x, "after launch", "mark")
        g = core.goal_show(self.c, "launch")
        self.assertEqual((g["items_open"], g["items_backlog"]), ([], [x]))
        self.assertEqual([i["id"] for i in g["items"]], [x])
        with self.assertRaisesRegex(RiverError, rf"backlog items: #{x}"):
            core.goal_done(self.c, "launch", "live", "mark")
        core.goal_own(self.c, "launch", "w1")
        gb = core._goal_brief(self.c, "launch", "w1")
        self.assertEqual(gb["items_backlog"], [{"id": x, "title": "polish"}])
        core.goal_done(self.c, "launch", "live", "w1", drop_open=True)
        self.assertEqual(core.item_show(self.c, x)["status"], "dropped")
        core.reopen(self.c, x, "mark")  # a closed item leaves the backlog: reopened, it is ready
        self.assertTrue(core.annotate(self.c)[x]["ready"])

    def test_the_manager_and_go_skip_the_backlog(self):
        x = self.add("web", "someday")
        core.backlog(self.c, x, "later", "mark")
        self.assertEqual(core.manager_findings(self.c)["uncovered"], [])
        b = core.go(self.c, self.web, "w1")
        self.assertEqual((b["role"], b["item"], b["backlog"]), ("idle", None, 1))  # nothing open
        self.assertEqual([s["id"] for s in core.cleanup(self.c)], [])

    def test_the_columns_come_by_migration(self):
        self.c.execute("DROP INDEX items_light")  # an old queue has no index on backlog_at either (#5524)
        self.c.execute("DROP INDEX items_project_status")  # nor this one (#5580)
        self.c.execute("ALTER TABLE items DROP COLUMN backlog_at")
        self.c.execute("ALTER TABLE items DROP COLUMN backlog_note")
        self.c.execute("DELETE FROM meta WHERE key LIKE 'schema:%'")  # an old queue has no schema id of this code (#5396)
        self.c.close()
        self.c = core.connect(self.path)
        cols = {r["name"] for r in self.c.execute("PRAGMA table_info(items)")}
        self.assertTrue({"backlog_at", "backlog_note"} <= cols)


class TextQueue(Base):
    """A queue with filler work and one person, for the tests of the score (#2257, #3286)."""

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "web")
        core.project_add(self.c, "api")
        core.register(self.c, "mark", human=True)
        for a in ("w1", "w2", "w3"):
            core.register(self.c, a)
        # Filler work, so the common words weigh less than the rare ones.
        for t in ("Page header shows the logo", "Footer links to the terms", "Dark mode colors for the table",
                  "Login form remembers the email", "Export the report as a spreadsheet", "Search box on the list page"):
            core.item_add(self.c, "web", t, actor="w3")
        self.at(-3 * 3600)  # the filler is no series of the actors below

    def at(self, seconds):
        """Move every add event back in time, so the batch rule (one author, two hours) passes over it."""
        self.c.execute("UPDATE events SET at=? WHERE change LIKE 'added to %'",
                       (core.iso(core.now() + timedelta(seconds=seconds)),))
        self.c.commit()

    def cli(self, *words):
        from river import cli
        out, err = io.StringIO(), io.StringIO()
        # Not the session that runs the tests: its MAXPM_AGENT is no agent of this queue (#5576).
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.dict(os.environ, {"MAXPM_DB": self.path, "MAXPM_QUIET": "1", "MAXPM_AGENT": "",
                                             "CLAUDE_CODE_SESSION_ID": "", "CLAUDE_CODE_BRIDGE_SESSION_ID": ""}):
            try:
                cli.run(list(words))
            except RiverError as e:
                return out.getvalue() + str(e)
        return out.getvalue() + err.getvalue()

    def add(self, title, actor="w1", **kw):
        return core.item_add(self.c, kw.pop("project", "web"), title, actor=actor, **kw)["id"]


class SimilarWork(TextQueue):
    """Find related items before work repeats (#2257): search, the check of add, related, duplicates, merge."""

    def test_the_word_table_adds_new_items_and_counts_all_again_after_an_edit(self):
        """#5522: a runner keeps the word table; new work items add their words to it, and any other change (an edit
        of a title) counts every item again. Each way gives the table that a count of every item gives."""
        def fresh():
            core._DF.clear()
            for f in __import__("pathlib").Path(self.dir.name).glob("*-words.json"):
                f.unlink()
            return core._df(self.c)[:2]
        grew = []
        real = core._df_grow
        core._df_grow = lambda *a: grew.append(real(*a)) or grew[-1]
        self.addCleanup(setattr, core, "_df_grow", real)
        core._df(self.c)
        a = self.add("Worktree hooks refuse the release", context="the gate script")
        grew.clear()
        df, n, _ = core._df(self.c)  # grown from the table in memory
        self.assertTrue(grew and grew[0])
        self.assertEqual((dict(df), n), tuple((dict(x) if i == 0 else x) for i, x in enumerate(fresh())))
        self.assertEqual(df[core._word("worktree")], 1)
        core._DF.clear()  # another process: the file beside the queue, then the new item
        self.add("Worktree page")
        grew.clear()
        df, n, _ = core._df(self.c)
        self.assertTrue(grew and grew[0])
        self.assertEqual((dict(df), n), tuple((dict(x) if i == 0 else x) for i, x in enumerate(fresh())))
        self.assertEqual(df[core._word("worktree")], 2)
        core.item_edit(self.c, a, title="Gate hooks refuse the release", actor="t")
        grew.clear()
        df, n, _ = core._df(self.c)
        self.assertEqual(grew, [None])  # an edit: every item is counted again
        self.assertEqual(df[core._word("worktree")], 1)
        self.assertEqual((dict(df), n), tuple((dict(x) if i == 0 else x) for i, x in enumerate(fresh())))

    def test_identifiers_of_an_item_are_kept_while_its_text_is_the_same(self):
        """#5522: a close reads the identifiers of every open item; they are read again only when its text changed."""
        d = {"id": 9001, "title": "Fix river/core.py", "context": "see 'the gate hook script'", "notes": None}
        first = core._item_idents(d)
        self.assertIs(core._item_idents(dict(d)), first)
        self.assertEqual(first, frozenset({"river/core.py", "'the gate hook script'"}))
        self.assertEqual(core._item_idents({**d, "notes": "and docs/guide.md"}), first | {"docs/guide.md"})

    def test_search_finds_every_word_in_any_state_newest_first(self):
        a = self.add("Worktree script fails during a release", context="the hook refuses HEAD")
        b = self.add("Release notes page")
        c = self.add("Gate hook refuses the worktree", project="api")
        core.claim(self.c, a, "w1")
        core.done(self.c, a, "fixed in abc123", "w1")
        core.drop(self.c, c, "w2")
        r = core.search(self.c, "worktree hooks")
        self.assertEqual([m["id"] for m in r["items"]], [c, a])  # newest first; the stem finds hooks
        self.assertEqual(r["items"][1]["status"], "done")
        self.assertEqual(r["items"][1]["output"], "fixed in abc123")
        self.assertEqual([m["id"] for m in core.search(self.c, "worktree", project="web")["items"]], [a])
        self.assertEqual([m["id"] for m in core.search(self.c, "worktree", status="dropped")["items"]], [c])
        self.assertEqual(core.search(self.c, "release", status="open")["items"][0]["id"], b)
        self.assertEqual(core.search(self.c, "worktree", since="1h")["total"], 2)
        with self.assertRaises(RiverError):
            core.search(self.c, "the a of")
        none = core.search(self.c, "worktree spaceship")
        self.assertEqual(none["items"], [])
        self.assertIn(a, [m["id"] for m in none["closest"]])
        out = self.cli("search", "worktree", "hook")
        self.assertIn("2 item(s) with every word", out)
        self.assertIn(f"#{a}", out)
        self.assertIn("output: fixed in abc123", out)

    def test_add_refuses_the_same_work_and_shows_it(self):
        x = self.add("Worktree script fails with ref updates aborted by hook", context="gate lock")
        core.claim(self.c, x, "w1")
        out = self.cli("--as", "w2", "add", "web", "The worktree script fails: ref updates aborted by the hook")
        self.assertIn("nothing was added", out)
        self.assertIn(f"#{x} [in progress by w1]", out)
        self.assertIn(f"--item {x}", out)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM items").fetchone()[0], 7)
        with self.assertRaises(core.SimilarWork) as e:
            core.item_add(self.c, "web", "Worktree script fails, ref updates aborted by hook", actor="w2",
                          check_similar=True)
        self.assertEqual(e.exception.matches[0]["id"], x)
        # Different work: --new adds it, and the history says why.
        out = self.cli("--as", "w2", "add", "web", "Worktree script fails: ref updates aborted by hook on Windows",
                       "--new", "the Windows path, not the gate lock")
        y = max(r[0] for r in self.c.execute("SELECT id FROM items"))
        self.assertNotIn("nothing was added", out)
        changes = [e["change"] for e in core.item_show(self.c, y)["events"]]
        self.assertTrue(any(f"although like #{x}" in ch and "Windows path" in ch for ch in changes), changes)
        # Unlike work is added at once.
        self.assertIn("Billing", self.cli("--as", "w2", "add", "web", "Billing email for a failed card"))

    def test_the_check_passes_over_links_series_old_work_and_other_projects(self):
        x = self.add("Onboarding test: Notion connector walk")
        # The same author's series within two hours.
        series = self.add("Onboarding test: Notion connector walk again", check_similar=True)
        core.drop(self.c, series, "w1")
        # Another author names it: --found-during and --blocks know it.
        self.add("Onboarding test of the Notion connector walk", actor="w2", found_during=x, check_similar=True)
        # Another project.
        self.add("Onboarding test: Notion connector walk", actor="w2", project="api", check_similar=True)
        # Done longer ago than similar_days.
        old = self.add("Rotate the signing key of the mail sender")
        core.claim(self.c, old, "w1")
        core.done(self.c, old, None, "w1")
        self.c.execute("UPDATE items SET closed_at=? WHERE id=?", (core.iso(core.now() - timedelta(days=20)), old))
        self.at(-3 * 3600)
        self.add("Rotate the signing key of the mail sender", actor="w2", check_similar=True)
        # Done lately: refused.
        new = self.add("Purge the cache of the image proxy")
        core.claim(self.c, new, "w1")
        core.done(self.c, new, "purged", "w1")
        self.at(-3 * 3600)
        with self.assertRaises(core.SimilarWork) as e:
            self.add("Purge the cache of the image proxy", actor="w2", check_similar=True)
        self.assertEqual(e.exception.matches[0]["status"], "done")
        # The setting turns it off.
        core.config_set(self.c, "similar_min", "off", project="web")
        self.add("Purge the cache of the image proxy", actor="w2", check_similar=True)
        with self.assertRaises(RiverError):
            core.config_set(self.c, "similar_min", "2")
        with self.assertRaises(RiverError):
            core.config_set(self.c, "similar_days", "soon")

    def test_a_persons_item_is_checked_too(self):
        h = self.add("Approve the refund policy draft", doer="human", actor="w1")
        self.at(-3 * 3600)
        out = self.cli("--as", "w2", "add", "web", "Approve the refund policy draft (five decisions)", "--doer", "human")
        self.assertIn(f"#{h} [open]", out)
        self.assertIn("nothing was added", out)

    def test_overlapping_touches_count(self):
        x = self.add("Retry the upload of a large file", touches=["src/upload.py"])
        self.at(-3 * 3600)
        like = core.similar_items(self.c, "web", "Upload retry counts", touches=["src/upload.py"], actor="w2")
        plain = core.similar_items(self.c, "web", "Upload retry counts", actor="w2")
        self.assertEqual([m["id"] for m in like], [x])
        self.assertEqual(plain, [])

    def test_show_and_go_list_related_work(self):
        x = self.add("Export the invoice list as CSV")
        core.claim(self.c, x, "w1")
        core.done(self.c, x, "csv export in a1b2", "w1")
        self.at(-3 * 3600)
        y = self.add("Export the invoice list as CSV with the tax column", actor="w2", new="the tax column", priority=0)
        rel = core.related(self.c, y)
        self.assertEqual(rel[0]["id"], x)
        self.assertIn("related", self.cli("show", str(y)))
        self.assertIn("csv export in a1b2", self.cli("show", str(y)))
        # A linked item is shown as a link, not as related.
        z = self.add("Invoice list CSV export follow-up", actor="w3", found_during=y)
        self.assertNotIn(z, [m["id"] for m in core.related(self.c, y)])
        b = core.go(self.c, os.getcwd(), "w2", project="web")
        self.assertEqual(b["item"]["id"], y)
        self.assertEqual(b["related"][0]["id"], x)

    def test_duplicates_groups_open_items_and_merge_joins_them(self):
        a = self.add("Dark mode for the settings page", context="colors from the theme", touches=["web/settings.css"])
        self.at(-3 * 3600)
        b = self.add("Settings page in dark mode", actor="w2", context="use the theme colors", notes="see the mock",
                     new="test", touches=["web/theme.css"], refs=[("github:o/r#7", None)], priority=1)
        c = self.add("Dark mode for the settings page", actor="w3", new="test", after=[a, b])  # linked: not a duplicate
        g = core.duplicates(self.c)
        self.assertEqual(len(g), 1)
        self.assertEqual([m["id"] for m in g[0]["items"]], [a, b])
        self.assertIn("1 group(s)", self.cli("duplicates"))
        self.assertIn(f"#{a}", self.cli("duplicates", "--project", "web"))
        core.send(self.c, "note", "about b", to="w2", item=b, actor="w1")
        core.queue_add(self.c, "w2", item=b, actor="mark")
        with self.assertRaises(RiverError):
            core.merge(self.c, a, b, "", "mark")
        r = core.merge(self.c, a, b, "the same dark mode work", "mark")
        self.assertEqual(r["merged"]["id"], b)
        o = core.item_show(self.c, b)
        self.assertEqual(o["status"], "dropped")
        self.assertIn(f"duplicate of #{a}", o["output"])
        k = core.item_show(self.c, a)
        self.assertEqual(k["priority"], 1)
        self.assertIn(f"[from #{b}] see the mock", k["notes"])
        self.assertIn("use the theme colors", k["context"])
        self.assertEqual(sorted(k["touches"]), ["web/settings.css", "web/theme.css"])
        self.assertEqual([x["ref"] for x in k["refs"]], ["github:o/r#7"])
        self.assertIn(c, k["unblocks"])
        self.assertEqual(self.c.execute("SELECT item_id FROM messages WHERE body='about b'").fetchone()[0], a)
        self.assertEqual(self.c.execute("SELECT item_id FROM queue_entries WHERE agent='w2'").fetchone()[0], a)
        self.assertEqual(core.duplicates(self.c), [])
        with self.assertRaises(RiverError):  # closed
            core.merge(self.c, a, b, "again", "mark")

    def test_merge_refusals(self):
        a = self.add("Approve the logo", doer="human")
        b = self.add("Draw the logo", actor="w2")
        c = self.add("Draw the logo mark", actor="w3", new="x")
        with self.assertRaises(RiverError) as e:
            core.merge(self.c, a, b, "same", "mark")
        self.assertIn("a person's item merges only with a person's item", str(e.exception))
        core.claim(self.c, c, "w3")
        with self.assertRaises(RiverError) as e:
            core.merge(self.c, b, c, "same", "mark")
        self.assertIn("held by w3", str(e.exception))
        with self.assertRaises(RiverError):
            core.merge(self.c, b, b, "same", "mark")
        # The holder may merge its own item into another one.
        core.merge(self.c, b, c, "same", "w3")
        self.assertEqual(core.item_show(self.c, c)["status"], "dropped")


class CloseCheck(TextQueue):
    """A close lists the open items it may have made unnecessary (#3286): linked, near, or naming the same thing."""

    def finish(self, item_id, output=None, actor="w1", **kw):
        core.claim(self.c, item_id, actor)
        return core.done(self.c, item_id, output, actor, **kw)

    def listed(self, res):
        return {x["id"]: x["why"] for x in res["close_check"]}

    def test_identifiers(self):
        ids = core._idents("Open https://app.example.com/flows/4f2a91c3-1d2e-4b5a-9c8d-0e1f2a3b4c5d, then "
                           "automation 4f2a91c3 and commit 9f304ff; see docs/guide/setup.md and the step "
                           "'Before you connect Mail' for the person's 'look' (#41), and/or ask ops@example.com.")
        self.assertIn("4f2a91c", ids)  # the uuid and its prefix are one id
        self.assertIn("9f304ff", ids)
        self.assertIn("docs/guide/setup.md", ids)
        self.assertIn("'before you connect mail'", ids)
        self.assertIn("#41", ids)
        self.assertIn("ops@example.com", ids)
        self.assertIn("https://app.example.com/flows/4f2a91c3-1d2e-4b5a-9c8d-0e1f2a3b4c5d", ids)
        self.assertNotIn("and/or", ids)
        self.assertNotIn("'look'", ids)  # one word: too common to name one thing
        self.assertNotIn("1234567", core._idents("order 1234567"))  # digits only: a number, not an id

    def test_a_reset_lists_the_person_items_it_made_unnecessary_and_the_closer_decides(self):
        resume = self.add("Resume the sample automation in the demo workspace", doer="human", actor="w2",
                          context="Open https://app.example.com/automations/4f2a91c3-1d2e-4b5a-9c8d-0e1f2a3b4c5d "
                                  "and press Resume in the header.")
        again = self.add("Reconnect the test mail account in the demo workspace", doer="human", actor="w2",
                         context="Connections > Add connection, account 7be0d4e1, share it with everyone.")
        other = self.add("Look at the new pricing table", doer="human", actor="w2")
        reset = self.add("Reset the demo workspace for a new recording")
        core.sync_needs_you_now(self.c)  # as maxpm add does: the person items are on the person's list from the add
        self.at(-3 * 3600)
        res = self.finish(reset, "Reset done: automation 4f2a91c3 archived, mail account 7be0d4e1 disconnected")
        got = self.listed(res)
        self.assertEqual(set(got), {resume, again})
        self.assertIn("names 4f2a91c", got[resume])
        self.assertNotIn(other, got)
        out = self.cli("--as", "w1", "show", str(reset))
        self.assertNotIn("MAY BE UNNECESSARY", out)  # show prints only the item; the reply of done lists them
        # The closer keeps one with the reason; the check does not list it again for this close.
        out = self.cli("--as", "w1", "keep", str(again), "--close", str(reset), "--note", "the video needs it")
        self.assertIn(f"kept after the close of #{reset}: #{again}", out)
        self.assertTrue(any("kept after the close" in e["change"] for e in core.item_show(self.c, again)["events"]))
        self.assertIn("lists", self.cli("--as", "w1", "keep", str(again), "--close", str(reset)))
        # The other one waits in cleanup and in the manager's findings until someone decides.
        rows = core.cleanup(self.c, git=False)
        self.assertEqual(rows[0]["id"], resume)
        self.assertIn(core.UNNEEDED, rows[0]["reasons"])
        self.assertEqual([x["id"] for x in core.manager_findings(self.c)["unneeded"]], [resume])
        self.assertIn(f"unneeded:{resume}", core._finding_keys(core.manager_findings(self.c)))
        self.assertIn("still needed", self.cli("cleanup", "--no-git"))
        # An agent closes the person's item with a reason: the person gets one notice and can undo it.
        core.done(self.c, resume, None, "w1", note=f"#{reset} archived the automation")
        notice = self.c.execute("SELECT body FROM messages WHERE to_agent='mark' AND item_id=?", (resume,)).fetchone()
        self.assertIn("undo-takeover", notice["body"])
        self.assertEqual(core.close_open(self.c), [])
        self.assertEqual(core.manager_findings(self.c)["unneeded"], [])

    def test_the_reply_of_done_names_the_commands(self):
        look = self.add("Look at the export dialog 'Save as spreadsheet' after the next release", doer="human",
                        actor="w2")
        x = self.add("Remove the export dialog 'Save as spreadsheet'")
        self.at(-3 * 3600)
        core.claim(self.c, x, "w1")
        out = self.cli("--as", "w1", "done", str(x), "--output", "removed the dialog 'Save as spreadsheet'")
        self.assertIn(f"MAY BE UNNECESSARY NOW (the close of #{x}", out)
        self.assertIn(f"#{look} Look at the export dialog", out)
        self.assertIn("[a person's item]", out)
        self.assertIn(f"keep <id> --close {x}", out)
        self.assertIn("merge <keep> <id>", out)
        out = self.cli("--as", "w1", "keep", "--close", str(x))  # all of them at once
        self.assertIn(f"#{look}", out)
        self.assertEqual(core.close_open(self.c), [])

    def test_a_drop_lists_what_waits_on_it_and_what_was_found_during_it_first(self):
        x = self.add("Build the weekly digest mail")
        after = self.add("Translate the weekly digest", after=[x])
        found = self.add("Look at the digest mail in a phone mail app", doer="human", found_during=x)
        res = core.drop(self.c, x, "w1", note="the digest is no longer wanted")
        got = res["close_check"]
        self.assertEqual({g["id"] for g in got}, {after, found})
        self.assertEqual(got[0]["id"], found)  # person items first
        self.assertIn(f"waits on #{x}, which is dropped", self.listed(res)[after])
        self.assertIn("found during", self.listed(res)[found])
        # A done item's next steps are not listed: they are why it was done.
        y = self.add("Build the monthly digest mail")
        nxt = self.add("Translate the monthly digest", after=[y])
        look = self.add("Look at the monthly digest mail", doer="human", found_during=y)
        self.assertEqual(self.listed(self.finish(y, "built")), {})
        # Done with a note that says it is no longer needed: like a drop.
        z = self.add("Build the yearly digest mail")
        nz = self.add("Translate the yearly digest", after=[z])
        res = self.finish(z, "not needed: the monthly digest covers it")
        self.assertIn("no longer needed", self.listed(res)[nz])
        self.assertNotIn(nxt, self.listed(res))

    def test_a_forced_done_lists_what_it_still_waited_on(self):
        pre = self.add("Write the migration for the archive table")
        x = self.add("Archive old reports", after=[pre])
        res = core.done(self.c, x, "archived by hand", "w1", force="done by hand")
        self.assertIn("although it waited on this", self.listed(res)[pre])

    def test_a_conflicts_link_is_no_wait(self):
        """#4849: parts of one chain touch the same files, so they get conflicts links both ways; the done of an early
        part listed every later one as 'done although it waited on this'."""
        one = self.add("Relay part one: the scaffold", touches=["relay"])
        two = self.add("Relay part two: the account site", touches=["relay"], after=[one])
        other = self.add("Relay pages: the buy page", touches=["relay"])
        kinds = {(r["item_id"], r["blocked_by"]): r["kind"] for r in self.c.execute("SELECT * FROM deps")}
        self.assertIn("conflicts", kinds.values())  # the links that the old check read as waits
        res = self.finish(one, "built")
        for later in (two, other):
            self.assertNotIn("although it waited on this", self.listed(res).get(later, ""))
        # A real wait is still listed when the done is forced.
        pre = self.add("Relay part zero: the design", touches=["docs"])
        x = self.add("Relay part three: the socket", touches=["relay"], after=[pre])
        res = core.done(self.c, x, "built by hand", "w1", force="done by hand")
        self.assertIn("although it waited on this", self.listed(res)[pre])
        self.assertNotIn("although it waited on this", self.listed(res).get(two, ""))

    def test_a_revert_lists_the_persons_look_at_the_reverted_work(self):
        x = self.add("Show the trust prompt in a new folder")
        look = self.add("Look at the trust prompt in a new folder", doer="human", found_during=x)
        self.finish(x, "shown")
        self.c.execute("DELETE FROM close_checks")  # what the close of x listed does not count here
        self.c.commit()
        r = self.add("Undo the trust prompt change")
        res = self.finish(r, f"Revert 1a2b3c4 (#{x}): the prompt is gone again")
        self.assertIn(f"which #{r} reverts", self.listed(res)[look])

    def test_a_merge_lists_a_third_copy(self):
        a = self.add("Send the three test mails of the video to the test inbox", doer="human")
        self.at(-3 * 3600)
        b = self.add("Send the three test mails to the test inbox again", doer="human", actor="w2")
        self.at(-3 * 3600)
        c = self.add("Send three test mails to the test inbox before the recording", doer="human", actor="w3")
        res = core.merge(self.c, a, b, "the same mails", "w1")
        self.assertIn(c, self.listed(res))
        self.assertNotIn(a, self.listed(res))

    def test_a_reopen_checks_again_at_the_next_close(self):
        look = self.add("Look at the 'Weekly totals' card after the next release", doer="human", actor="w2")
        x = self.add("Drop the 'Weekly totals' card")
        self.at(-3 * 3600)
        self.finish(x, "dropped the 'Weekly totals' card")
        core.close_keep(self.c, x, look, "the card stays on the old page", "w1")
        core.reopen(self.c, x, "w1")
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM close_checks WHERE closed_id=?", (x,)).fetchone()[0], 0)
        self.assertIn(look, self.listed(self.finish(x, "dropped the 'Weekly totals' card again")))

    def test_cleanup_checks_past_closes_without_items_made_later(self):
        look = self.add("Look at the 'Team costs' sheet after the next release", doer="human", actor="w2")
        x = self.add("Fill the 'Team costs' sheet")
        self.at(-3 * 3600)
        self.finish(x, "filled the 'Team costs' sheet")
        later = self.add("Check the 'Team costs' sheet totals", doer="human", actor="w2")
        self.c.execute("UPDATE items SET created_at=? WHERE id=?", (core.iso(core.now() + timedelta(minutes=5)), later))
        self.c.execute("DELETE FROM close_checks")  # a close before the check existed
        self.c.commit()
        self.assertEqual(core.close_open(self.c), [])
        out = self.cli("cleanup", "--no-git", "--closes", "7d")
        self.assertIn(f"#{look}", out)
        self.assertNotIn(f"#{later} ", out)

    def test_reworded_person_asks_for_the_same_step_are_duplicates(self):
        a = self.add("Look at the step 'Before you connect Mail' after the next release; reply ok", doer="human",
                     context="Connections > Add connection > Mail. Its new last section 'The rules we follow.'")
        self.at(-3 * 3600)
        b = self.add("Read the 'Before you connect Mail' step and section 4 of the policy after the release",
                     doer="human", actor="w2", context="The policy page section 4 is new.")
        pair = [g["items"] for g in core.duplicates(self.c)]
        self.assertEqual([[m["id"] for m in g] for g in pair], [[a, b]])
        # The add check sees it too, for a person's item.
        self.at(-3 * 3600)
        with self.assertRaises(core.SimilarWork):
            self.add("Check the wording of 'Before you connect Mail' after the release", doer="human", actor="w3",
                     check_similar=True)
        # Agent items get no bonus: the words alone decide.
        self.assertEqual(core.similar_items(self.c, "web", "Check the wording of 'Before you connect Mail' after "
                                            "the release", actor="w3"), [])

    def test_a_keep_covers_every_close_and_later_word_matches(self):
        """#3291: a keep holds for every close that lists the item now, and for later closes that match by
        words only; an identifier brings it back."""
        look = self.add("Look at the report export page", doer="human", actor="w2")
        a = self.add("Report export page changes")
        b = self.add("Report export page fixes", actor="w3")
        self.at(-3 * 3600)
        self.assertIn(look, self.listed(self.finish(a, "report export page")))
        self.assertIn(look, self.listed(self.finish(b, "report export page", actor="w3")))
        core.close_keep(self.c, a, look, "the look is still to do", "w1")
        self.assertEqual(core.close_open(self.c), [])  # the keep covers the close of b too
        c = self.add("Report export page texts", actor="w3")
        self.at(-3 * 3600)
        res = self.finish(c, "report export page", actor="w3")
        self.assertNotIn(look, self.listed(res))
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM close_checks WHERE closed_id=?", (c,)).fetchone()[0], 0)
        self.c.execute("UPDATE items SET context=? WHERE id=?", ("The button 'Export report now' at the top.", look))
        e = self.add("Move the 'Export report now' button of the report export page", actor="w3")
        self.at(-3 * 3600)
        self.assertIn(look, self.listed(self.finish(e, "moved 'Export report now' to the right", actor="w3")))

    def test_words_alone_must_reach_similar_min(self):
        near = self.add("Look at the invoice list filters", doer="human", actor="w2")
        far = self.add("Look at the invoice totals line after the release", doer="human", actor="w2")
        x = self.add("Invoice list filters")
        self.at(-3 * 3600)
        got = self.listed(self.finish(x, "invoice list filters added"))
        self.assertIn(near, got)
        self.assertNotIn(far, got)
        self.assertGreaterEqual(float(got[near].split("score ")[1].rstrip(")")), 0.55)

    def test_a_title_series_is_no_match(self):
        self.assertTrue(core._title_series("Onboarding test: Notion (A1)", "Onboarding test: Box (A7)"))
        self.assertFalse(core._title_series("Onboarding test: Notion", "Onboarding test: Notion"))
        self.assertFalse(core._title_series("Onboarding test: Notion", "Connector walk: Notion"))
        self.assertFalse(core._title_series("Onboarding test of Notion", "Onboarding test of Box"))
        other = self.add("Onboarding test: Box connector walk on production", actor="w2")
        x = self.add("Onboarding test: Notion connector walk on production")
        self.at(-3 * 3600)
        self.assertNotIn(other, self.listed(self.finish(x, "Notion connector walk passed on production")))

    def test_a_drop_says_which_items_it_makes_ready(self):
        """#3292: an item that waited on a dropped item is ready at once, although its real precondition may
        still be false: the reply says so and gives the commands."""
        step = self.add("Connect the test accounts", doer="human", actor="w2")
        walk = self.add("Walk the shared accounts in the browser", actor="w2", after=[step])
        other = self.add("Write the walk notes", actor="w2", after=[step, walk])
        core.register(self.c, "w9")
        out = self.cli("--as", "w9", "drop", str(step), "--note", "the accounts are connected a new way")
        self.assertIn(f"#{walk} Walk the shared accounts", out)
        self.assertIn("[NOW READY]", out)
        self.assertIn(f"dep <id> --on <what it waits on>", out)
        rows = {r["id"]: r for r in core.close_open(self.c)}
        self.assertIn("the close makes it ready", rows[walk]["closes"][0]["why"])
        self.assertNotIn("makes it ready", rows[other]["closes"][0]["why"])  # it still waits on the walk


class Activity(Base):
    """The Claude Code hooks of a session that river starts report its activity (maxpm activity, #4288)."""
    SID = "0b5a3c1e-1111-4222-8333-944455556666"

    def setUp(self):
        super().setUp()
        from unittest import mock
        core.project_add(self.c, "shop")
        core.register(self.c, "mark", human=True)
        core.register(self.c, "boss")
        core._set_role_note(self.c, "boss", "manager", None)
        core.register(self.c, "w")
        self.item = self.add("shop", "Fix the build")
        core.claim(self.c, self.item, "w")
        core.record_claude_session(self.c, "w", self.SID)
        self.clock = [core.now()]
        fake_now = mock.patch.object(core, "now", lambda: self.clock[0])
        fake_now.start()
        self.addCleanup(fake_now.stop)

    def event(self, name, **more):
        return core.activity_event(self.c, {"hook_event_name": name, "session_id": self.SID, **more}, env={})

    def alerts(self):
        return [r["body"] for r in self.c.execute(
            "SELECT body FROM messages WHERE to_agent='boss' AND kind='alert' AND read_at IS NULL ORDER BY id")]

    def test_a_permission_prompt_or_a_turn_end_with_an_item_alerts_the_manager_at_once(self):
        self.assertEqual(self.event("PostToolUse", tool_name="Read")["alert"], None)
        a = core.agent_status(self.c, "w")
        row = self.c.execute("SELECT activity_at, activity_event FROM agents WHERE name='w'").fetchone()
        self.assertEqual((row["activity_event"], a["name"]), ("report:working", "w"))
        # The permission request names the call; the notice that the prompt shows alerts the manager, blocked.
        self.event("PermissionRequest", tool_name="Bash", tool_input={"command": "git push  origin main", "timeout": 5})
        mid = self.event("Notification", notification_type="permission_prompt",
                         message="Claude needs your permission to use Bash")["alert"]
        self.assertEqual(self.alerts(), [
            f"w stands still: it waits on a permission prompt in its terminal on #{self.item} Fix the build; it asks "
            f"to run Bash: git push origin main (Claude needs your permission to use Bash). Answer it in its terminal "
            f"or its session, or tell the person."])
        m = core.message_show(self.c, mid)
        self.assertEqual((m["from_agent"], m["item_id"], bool(m["blocked"])), ("w", self.item, True))
        self.assertIsNone(self.event("Notification", notification_type="permission_prompt",
                                     message="Claude needs your permission to use Bash")["alert"])  # once while unread
        # Another notice (idle) alerts nothing; a tool call clears the prompt.
        self.assertIsNone(self.event("Notification", notification_type="idle_prompt", message="waiting")["alert"])
        self.event("PostToolUse", tool_name="Bash")
        self.assertIsNone(self.c.execute("SELECT activity_prompt FROM agents WHERE name='w'").fetchone()[0])
        # The turn ends while it holds the item: it sits at its prompt.
        self.assertIsNotNone(self.event("Stop")["alert"])
        self.assertIn("it ended its turn while it holds", self.alerts()[-1])
        # Not while it waits on a person's answer (maxpm ask), and not with nothing in hand.
        core.message(self.c, "question", "Push to production?", to="mark", item=self.item, actor="w")
        self.c.execute("UPDATE messages SET read_at=? WHERE to_agent='boss'", (core.iso(core.now()),))
        self.assertIsNone(self.event("Stop")["alert"])
        core.release(self.c, self.item, "w")
        self.assertIsNone(self.event("Notification", notification_type="permission_prompt", message="x")["alert"])
        # A session river does not know: nothing recorded.
        self.assertIsNone(core.activity_event(self.c, {"hook_event_name": "Stop", "session_id": "other"}, env={})["agent"])
        self.assertEqual(core.activity_event(self.c, {"hook_event_name": "Stop"}, env={"MAXPM_AGENT": "w"})["agent"], "w")

    def test_a_persons_prompt_in_the_askers_chat_answers_its_open_questions(self):
        """#4995: mark answered two maxpm questions by typing in the asking session's chat; they stayed open, and the
        page kept 'Waits for you' and 'waits for an answer'. Now the prompt closes them, with its text as the answer."""
        core.config_set(self.c, "question_hold", "0s")  # the person has the questions at once, not the manager (#5307)
        q1 = core.send(self.c, "question", "Do you agree?", to="mark", item=self.item, actor="w")["id"]
        q2 = core.send(self.c, "question", "Which of the changes 1-3?", to="mark", item=self.item, actor="w")["id"]
        self.assertEqual(core.waits_on_person(self.c, ["w"]), {"w": q1})
        self.assertTrue(any(n.get("message_id") in (q1, q2) for n in core.needs_you(self.c)))
        # A message of MaximizePM or of another session (a tag) is no answer of the person.
        self.event("UserPromptSubmit", prompt="[maxpm alert from boss] look at #12")
        self.event("UserPromptSubmit", prompt="<cross-session-message from='x'>hi</cross-session-message>")
        self.assertEqual(core.message_show(self.c, q1)["state"], "open")
        self.clock[0] += core.timedelta(seconds=30)
        got = self.event("UserPromptSubmit", prompt="ok I agree, and do 1-3")
        self.assertEqual(sorted(got["answered"]), [q1, q2])
        for q in (q1, q2):
            self.assertEqual(core.message_show(self.c, q)["state"], "answered")
        ans = self.c.execute("SELECT * FROM messages WHERE kind='answer' AND reply_to=?", (q1,)).fetchone()
        self.assertEqual((ans["from_agent"], ans["to_agent"], ans["body"], ans["state"], ans["native_status"]),
                         ("mark", "w", "ok I agree, and do 1-3", "read", "in the chat"))
        self.assertEqual(core.inbox(self.c, "w"), [])  # the asker has the answer in its chat already
        self.assertEqual(core.waits_on_person(self.c, ["w"]), {})
        self.assertFalse(any(n.get("message_id") in (q1, q2) for n in core.needs_you(self.c)))
        self.assertTrue(any(f"answered question #{q1} in the chat of w" in e["change"]
                            for e in core.item_show(self.c, self.item)["events"]))
        # A question asked after the prompt stays open until the next one.
        q3 = core.send(self.c, "question", "Push?", to="mark", item=self.item, actor="w")["id"]
        self.assertEqual(core.message_show(self.c, q3)["state"], "open")
        # The asker records an answer that came in its chat itself: answer --in-chat (only the asker).
        with self.assertRaisesRegex(core.RiverError, "--self and --in-chat are for the asker"):
            core.answer(self.c, q3, "yes", "boss", in_chat=True)
        m = core.answer(self.c, q3, "yes, push", "w", in_chat=True)
        self.assertEqual((m["from_agent"], m["native_status"], core.message_show(self.c, q3)["state"]),
                         ("mark", "in the chat", "answered"))

    def test_the_stop_hook_asks_serve_to_type_a_held_message(self):
        """#5587: a message that the person sent during the turn waits (pending_sends); the turn's Stop hook asks
        maxpm serve to type it at the idle prompt (op pending_deliver), with a short limit, and a serve that does not
        answer fails nothing. A turn that goes on, or no held message, asks nothing."""
        from river import cli
        asked = []
        fake = lambda conn, op, args, **kw: asked.append((op, args, kw))
        run = lambda event: cli.activity_command(io.StringIO(json.dumps({"hook_event_name": event, "session_id": self.SID})))
        with mock.patch.dict(os.environ, {"MAXPM_DB": self.path}), mock.patch.object(cli, "ask_server", fake), \
                contextlib.redirect_stdout(io.StringIO()):
            run("Stop")
            core.pending_add(self.c, "w", "after this turn", [], "mark")
            run("PreToolUse")
            self.assertEqual(asked, [])
            run("Stop")
            self.assertEqual(asked, [("pending_deliver", {"agent": "w"}, {"timeout": 1, "retries": 0})])

        def down(*a, **k):
            raise RiverError("maxpm serve does not answer")
        with mock.patch.dict(os.environ, {"MAXPM_DB": self.path}), mock.patch.object(cli, "ask_server", down), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run("Stop"), 0)

    def test_the_asker_is_reminded_to_close_its_open_questions_until_it_does(self):
        """#5001 (mark: 'the session should try to close them'): go, the line after each command, and the Stop hook
        (once for each question, not at the turn end right after the question) remind the asker; answer --self and
        withdraw close a question; the reminders stop."""
        from river import cli
        q1 = core.send(self.c, "question", "Which plan?", to="mark", item=self.item, actor="w")["id"]
        self.assertIsNone(self.event("Stop")["remind"])  # it waits for the answer now
        self.clock[0] += core.timedelta(minutes=3)
        self.assertIsNone(self.event("Stop", stop_hook_active=True)["remind"])
        got = self.event("Stop")["remind"]
        self.assertIn(f"you have 1 open question(s) to mark (#{q1}): close each that is answered", got)
        self.assertIn("maxpm --as w answer <id> --self", got)
        self.assertIsNone(self.event("Stop")["remind"])  # once for each question
        # The hook command prints the block decision for Claude Code.
        q2 = core.send(self.c, "question", "Push?", to="mark", item=self.item, actor="w")["id"]
        self.clock[0] += core.timedelta(minutes=3)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"MAXPM_DB": self.path}), mock.patch.object(core, "now", lambda: self.clock[0]), \
                contextlib.redirect_stdout(out):
            cli.activity_command(io.StringIO(json.dumps({"hook_event_name": "Stop", "session_id": self.SID})))
        hook = json.loads(out.getvalue())
        self.assertEqual(hook["decision"], "block")
        self.assertIn(f"(#{q2})", hook["reason"])
        # go and the footer name both.
        brief = core.go(self.c, self.dir.name, "w", project="shop")
        self.assertEqual([q["id"] for q in brief["asked_open"]], [q1, q2])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.render_go(brief)
        self.assertIn(f"OPEN QUESTIONS YOU ASKED: you have 2 open question(s) to mark (#{q1}, #{q2})", out.getvalue())
        with contextlib.redirect_stderr(err):
            cli._footer(self.c, "w")
        self.assertIn(f"2 open question(s) you asked (#{q1}, #{q2})", err.getvalue())
        # The asker closes them: one answered elsewhere, one moot. Only the asker may.
        with self.assertRaisesRegex(core.RiverError, "only the asker"):
            core.withdraw(self.c, q2, "moot", "boss")
        m = core.answer(self.c, q1, "the small plan (canvas card)", "w", self_answer=True)
        self.assertEqual((m["from_agent"], m["native_status"], core.message_show(self.c, q1)["state"]),
                         ("mark", "by the asker", "answered"))
        self.assertEqual(core.withdraw(self.c, q2, "the release went out without it", "w")["withdrawn"], q2)
        self.assertEqual(core.message_show(self.c, q2)["state"], "read")
        self.assertEqual(core.waits_on_person(self.c, ["w"]), {})
        self.assertFalse(any(n.get("message_id") in (q1, q2) for n in core.needs_you(self.c)))
        self.assertEqual(core.go(self.c, self.dir.name, "w", project="shop")["asked_open"], [])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cli._footer(self.c, "w")
        self.assertNotIn("open question", err.getvalue())

    def test_the_probe_stuck_session_reports_a_silence_while_it_lasts(self):
        # #5440: the probe reads the same rule as activity_watch, writes nothing, and reports after the alert too.
        self.event("PostToolUse", tool_name="Bash")
        self.assertEqual(core.ops_probe(self.c, "stuck-session")["status"], "ok")
        self.clock[0] += core.timedelta(minutes=11)
        for _ in range(2):
            res = core.ops_probe(self.c, "stuck-session")
            self.assertEqual((res["status"], list(res["subjects"])), ("critical", ["w"]))
            self.assertIn("nothing for 11m while it holds #", res["subjects"]["w"])
        self.assertEqual(core.activity_watch(self.c), ["w"])  # the probe did not mark it told
        self.assertEqual(core.ops_probe(self.c, "stuck-session")["status"], "critical")

    def test_silence_with_an_item_in_hand_alerts_the_manager_once(self):
        self.event("PermissionRequest", tool_name="Bash", tool_input={"command": "make deploy"})
        self.assertEqual(core.activity_watch(self.c), [])
        self.clock[0] += core.timedelta(minutes=11)
        self.assertEqual(core.activity_watch(self.c), ["w"])
        self.assertIn("nothing for 11m while it holds", self.alerts()[0])
        self.assertIn("it waits on a permission prompt for Bash: make deploy", self.alerts()[0])
        self.clock[0] += core.timedelta(minutes=11)
        self.assertEqual(core.activity_watch(self.c), [])  # once for one silence
        # A new event, then silence again: a new alert. Not while it waits in maxpm wait.
        self.event("PostToolUse", tool_name="Bash")
        self.clock[0] += core.timedelta(minutes=11)
        self.c.execute("UPDATE agents SET waiting_since=? WHERE name='w'", (core.iso(core.now()),))
        self.assertEqual(core.activity_watch(self.c), [])
        self.c.execute("UPDATE agents SET waiting_since=NULL WHERE name='w'")
        self.assertEqual(core.activity_watch(self.c), ["w"])
        core.config_set(self.c, "activity_silence", "0s")
        self.assertEqual(core.activity_watch(self.c), [])

    def test_the_word_manager_reaches_the_active_manager(self):
        (m,) = core.message(self.c, "alert", "the gate is red", to="manager", actor="w")
        self.assertEqual(core.message_show(self.c, m["id"])["to_agent"], "boss")
        core.unregister(self.c, "boss")
        with self.assertRaisesRegex(RiverError, "no manager session is active now"):
            core.message(self.c, "note", "hello", to="manager", actor="w")

    def test_the_hook_closes_its_connection_even_when_recording_fails(self):
        from river import cli
        connect = core.connect
        opened = []

        def capture():
            conn = connect(self.path)
            opened.append(conn)
            return conn
        for fails in (False, True):
            with mock.patch.object(core, "connect", capture), mock.patch.object(core, "activity_event") as event:
                event.return_value = {"event": "PostToolUse", "agent": "w"}
                if fails:
                    event.side_effect = RuntimeError("failed hook")
                self.assertEqual(cli.activity_command(io.StringIO("{}")), 0)
            with self.assertRaises(sqlite3.ProgrammingError):
                opened[-1].execute("SELECT 1")

    def test_the_hook_file_and_the_command(self):
        import contextlib
        import io
        import json as _json
        from pathlib import Path
        from unittest import mock
        from river import agents, cli
        settings = core.activity_hook_settings()
        self.assertEqual(sorted(settings["hooks"]), sorted(agents.get("claude").module.HOOK_EVENTS))
        self.assertTrue(all(h["hooks"][0]["command"].endswith(" report --hook claude")
                            for hs in settings["hooks"].values() for h in hs))
        with mock.patch.dict(os.environ, {"MAXPM_DB": self.path}):
            f = core.activity_hook_file()
            self.assertEqual(_json.loads(Path(f).read_text(encoding="utf-8")), settings)
            out = io.StringIO()
            for text in ('{"hook_event_name": "Stop", "session_id": "%s"}' % self.SID, "not json", ""):
                with mock.patch.object(sys, "stdin", io.StringIO(text)), contextlib.redirect_stdout(out), \
                        mock.patch.object(core, "agent_process", lambda: (None, None)):
                    self.assertEqual(cli.run(["activity"]), 0)  # never fails the session, prints nothing
            self.assertEqual(out.getvalue(), "")
        self.assertEqual(self.c.execute("SELECT activity_event FROM agents WHERE name='w'").fetchone()[0], "report:idle")

    def test_a_test_tier_task_waits_at_its_prompt_by_design_and_alerts_nobody(self):
        # #5565: test sessions of #5552 sent the manager a blocked 'stands still' alert every few minutes.
        self.c.execute("UPDATE items SET tier='test' WHERE id=?", (self.item,))
        self.c.commit()
        self.event("PostToolUse", tool_name="Bash")
        self.assertIsNone(self.event("Stop")["alert"])
        self.clock[0] += timedelta(hours=1)
        self.assertEqual(core.silent_holders(self.c, told_too=True), [])
        self.c.execute("UPDATE items SET tier=NULL WHERE id=?", (self.item,))
        self.c.commit()
        self.assertEqual([s["row"]["name"] for s in core.silent_holders(self.c, told_too=True)], ["w"])

    def test_a_turn_end_that_waits_on_its_own_background_work_alerts_nobody(self):
        """#4293: build-loop-513a ended its turn to wait on its background gate run, and the manager heard that it
        stands still. The Stop hook's input lists the background work that will wake the session."""
        gate = [{"id": "b1", "type": "local_bash", "status": "running", "description": "gate",
                 "command": "make  check-all-once"}]
        self.assertIsNone(self.event("Stop", background_tasks=gate, session_crons=[])["alert"])
        waits = lambda: self.c.execute("SELECT activity_waits FROM agents WHERE name='w'").fetchone()[0]  # noqa: E731
        self.assertEqual(json.loads(waits()), ["local_bash: make check-all-once"])
        self.event("Notification", notification_type="idle_prompt", message="waiting")  # keeps what it waits on
        self.assertIsNotNone(waits())
        # The silence watch waits too, for at most busy_max.
        self.clock[0] += core.timedelta(minutes=30)
        self.assertEqual(core.activity_watch(self.c), [])
        self.clock[0] += core.timedelta(hours=4)
        self.c.execute("UPDATE agents SET last_seen=? WHERE name='boss'", (core.iso(core.now()),))  # still active
        self.assertEqual(core.activity_watch(self.c), ["w"])
        self.c.execute("UPDATE messages SET read_at=? WHERE to_agent='boss'", (core.iso(core.now()),))
        # The command ends and wakes the session: it works, then ends its turn with nothing in flight.
        self.event("UserPromptSubmit")
        self.assertIsNone(waits())
        done = [{**gate[0], "status": "completed"}]
        self.assertIsNotNone(self.event("Stop", background_tasks=done, session_crons=[])["alert"])
        self.assertIn("it ended its turn while it holds", self.alerts()[-1])
        self.c.execute("UPDATE messages SET read_at=? WHERE to_agent='boss'", (core.iso(core.now()),))
        # A background subagent and a wakeup count too.
        sub = [{"id": "a1", "type": "local_agent", "status": "running", "agent_type": "Explore", "description": "x"}]
        self.assertIsNone(self.event("Stop", background_tasks=sub)["alert"])
        self.assertIsNone(self.event("Stop", background_tasks=[], session_crons=[
            {"id": "c1", "schedule": "*/5 * * * *", "recurring": True, "prompt": "go"}])["alert"])
        self.assertEqual(json.loads(waits()), ["wakeup */5 * * * *"])

    def test_an_older_claude_code_looks_at_the_processes_without_the_hook_itself(self):
        """Without background_tasks in the Stop input: a process below the session that started after its last
        maxpm command, but not the hook that asks (busy_now skip)."""
        self.c.execute("UPDATE agents SET pid=100, host=? WHERE name='w'", (core.this_host(),))
        procs = ["100 1 10:00", f"{os.getppid()} 100 0:00", f"{os.getpid()} {os.getppid()} 0:00"]
        old = core.PROC_RUNNER
        self.addCleanup(setattr, core, "PROC_RUNNER", old)
        core.PROC_RUNNER = lambda: "\n".join(procs) + "\n"
        self.clock[0] += core.timedelta(seconds=30)
        self.assertIsNotNone(self.event("Stop")["alert"])  # only the hook runs below it
        self.c.execute("UPDATE messages SET read_at=? WHERE to_agent='boss'", (core.iso(core.now()),))
        procs.append("555 100 0:05")  # a command it started in the background
        self.assertIsNone(self.event("Stop")["alert"])
        self.assertEqual(json.loads(self.c.execute("SELECT activity_waits FROM agents WHERE name='w'").fetchone()[0]),
                         ["a process"])

    def test_the_hook_records_the_session_process_that_a_sandbox_hides(self):
        """A sandbox blocks ps for the agent's own commands, so agents.pid stays empty; the hook runs outside it."""
        import contextlib
        import io
        from unittest import mock
        from river import cli
        calls = []

        def found():
            calls.append(1)
            return 4242, "claude --name w"
        stop = '{"hook_event_name": "Stop", "session_id": "%s"}' % self.SID
        with mock.patch.dict(os.environ, {"MAXPM_DB": self.path}), mock.patch.object(core, "agent_process", found), \
                mock.patch.object(core, "pid_alive", lambda pid: pid == 4242), contextlib.redirect_stdout(io.StringIO()):
            for text in ('{"hook_event_name": "PostToolUse", "session_id": "%s"}' % self.SID, stop, stop):
                with mock.patch.object(sys, "stdin", io.StringIO(text)):
                    cli.run(["activity"])
        row = self.c.execute("SELECT pid, pid_cmd, host FROM agents WHERE name='w'").fetchone()
        self.assertEqual((row["pid"], row["pid_cmd"], row["host"]), (4242, "claude --name w", core.this_host()))
        self.assertEqual(len(calls), 1)  # not on a tool call, and not again while that process runs


class PushGuard(Base):
    """#5381: the text rule 'read the inbox before a push' failed ten times. A push or a launch of a session waits
    while it has an unread alert or a blocked item: the Claude Code hook before each Bash command refuses it."""
    SID = Activity.SID

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "shop")
        core.register(self.c, "boss")
        core.register(self.c, "w")
        self.item = self.add("shop", "Fix the build")
        core.claim(self.c, self.item, "w")
        core.record_claude_session(self.c, "w", self.SID)

    def event(self, name, **more):
        return core.activity_event(self.c, {"hook_event_name": name, "session_id": self.SID, **more}, env={})

    def bash(self, command):
        return self.event("PreToolUse", tool_name="Bash", tool_input={"command": command})

    def test_a_push_waits_for_an_unread_alert(self):
        before = self.c.execute("SELECT activity_at, activity_event FROM agents WHERE name='w'").fetchone()
        self.assertIsNone(self.bash("git push origin main")["deny"])  # nothing unread
        a = core.message(self.c, "alert", "Do not push: #12 must go first", to="w", actor="boss")[0]
        r = self.bash("git -C ~/shop push origin main")
        self.assertIn(f"alert #{a['id']} from boss: Do not push", r["deny"])
        self.assertIn("maxpm --as w inbox", r["deny"])
        self.assertIsNotNone(self.bash("maxpm launch --item 3")["deny"])
        self.assertIsNotNone(self.bash("gh pr merge 7")["deny"])
        # Other commands, a dry run, and the words in a message run.
        for cmd in ("git status", "maxpm launch --item 3 --dry-run", 'git commit -m "fix the push"',
                    'maxpm add "launch the site"', "maxpm --as w inbox"):
            self.assertIsNone(self.bash(cmd)["deny"], cmd)
        # The check records nothing: it runs before every Bash call.
        self.assertEqual(tuple(self.c.execute("SELECT activity_at, activity_event FROM agents WHERE name='w'").fetchone()),
                         tuple(before))
        core.inbox(self.c, "w")
        self.assertIsNone(self.bash("git push")["deny"])

    def test_a_blocked_item_holds_a_push_and_the_setting_turns_it_off(self):
        core.block(self.c, self.item, "waiting on the vendor", actor="w")
        self.assertIsNone(self.bash("git push")["deny"])  # its own block: it pushes the parts that are done
        core.block(self.c, self.item, "waiting on the vendor", actor="boss")
        self.assertIn(f"#{self.item} is blocked: waiting on the vendor", self.bash("git push")["deny"])
        core.message(self.c, "alert", "stop", to="w", actor="boss")
        core.config_set(self.c, "push_guard", "")
        self.assertIsNone(self.bash("git push")["deny"])
        with self.assertRaisesRegex(RiverError, "push_guard is a regular expression"):
            core.config_set(self.c, "push_guard", "(")

    def test_the_hook_prints_claude_codes_deny(self):
        import contextlib
        import io
        import json as _json
        from unittest import mock
        from river import agents, cli
        hooks = core.activity_hook_settings()["hooks"]
        self.assertEqual(hooks["PreToolUse"][0]["matcher"], "Bash")
        core.message(self.c, "alert", "Do not push yet", to="w", actor="boss")
        out = io.StringIO()
        text = _json.dumps({"hook_event_name": "PreToolUse", "session_id": self.SID, "tool_name": "Bash",
                            "tool_input": {"command": "git push"}})
        with mock.patch.dict(os.environ, {"MAXPM_DB": self.path}), mock.patch.object(sys, "stdin", io.StringIO(text)), \
                contextlib.redirect_stdout(out):
            self.assertEqual(cli.run(["report", "--hook", "claude"]), 0)
        got = _json.loads(out.getvalue())["hookSpecificOutput"]
        self.assertEqual((got["hookEventName"], got["permissionDecision"]), ("PreToolUse", "deny"))
        self.assertIn("Do not push yet", got["permissionDecisionReason"])
        self.assertEqual(set(agents.get("claude").read_hook({"hook_event_name": "PreToolUse"})), set(agents.HOOK_REPORT_KEYS))


class ManagerWake(Base):
    """A ship request, a review that passed and a deploy that is done wake the manager's watch at once (#4289)."""

    def test_the_release_steps_wake_the_manager(self):
        core.project_add(self.c, "a")
        core.target_add(self.c, "web", "push")
        core.project_target(self.c, "a", "web")
        core.config_set(self.c, "review", "on")
        core.register(self.c, "mark", human=True)
        for name in ("boss", "w1", "w2", "w3"):
            core.register(self.c, name)
        core._set_role_note(self.c, "boss", "manager", None)
        core.target_give(self.c, "web", "w3", "mark")
        x = self.add("a", "page")
        core.claim(self.c, x, "w1")
        dep = core.done(self.c, x, "abc123", "w1", ship_it=True)["shipped_in"]
        rv = self.c.execute("SELECT blocked_by FROM deps d JOIN items i ON i.id=d.blocked_by "
                            "WHERE d.item_id=? AND i.kind='review'", (dep,)).fetchone()[0]
        core.claim(self.c, rv, "w2")
        core.review_pass(self.c, rv, "checked the page", "w2", confirm="all")
        core.claim(self.c, dep, "w3")
        core.done(self.c, dep, "release-1", "w3")
        got = [(m["level"], m["body"]) for m in self.c.execute(
            "SELECT level, body FROM messages WHERE to_agent='boss' ORDER BY id")]
        self.assertEqual(got, [
            ("now", f"ship request: #{x} page joins deploy #{dep} for web"),
            ("now", f"review #{rv} of web passed: its deploy can start (checked the page)"),
            ("now", f"deploy #{dep} to web is done (release-1)")])
        # The watch returns for them at once.
        w = core.manage_watch(self.c, "boss", step="1h", sleep=lambda s: None)
        self.assertEqual(w["result"], "messages")
        # The manager's own steps wake nothing.
        y = self.add("a", "footer")
        core.claim(self.c, y, "w1")
        core.done(self.c, y, "def456", "w1")
        core.ship(self.c, y, "boss")
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM messages WHERE to_agent='boss' AND read_at IS NULL").fetchone()[0], 0)


class Canvas(Base):
    """#4648: canvases: pages that a session makes and maxpm keeps, with versions and comments."""

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "web")
        core.project_add(self.c, "api")
        core.register(self.c, "ag")
        core.register(self.c, "mark", human=True)
        self.it = self.add("web", "the plan")
        core.claim(self.c, self.it, "ag")

    def test_put_makes_versions_of_one_canvas_in_the_held_items_project(self):
        a = core.canvas_put(self.c, "Release-Plan", "<h1>Plan</h1>", "ag", filename="plan.html", title="Release plan")
        self.assertEqual((a["name"], a["kind"], a["project"], a["item_id"], a["version"]), ("release-plan", "html", "web", self.it, 1))
        b = core.canvas_put(self.c, "release-plan", "<h1>Plan</h1><p>two</p>", "ag", note="two")
        self.assertEqual((b["id"], b["version"], b["title"]), (a["id"], 2, "Release plan"))
        same = core.canvas_put(self.c, "release-plan", "<h1>Plan</h1><p>two</p>", "ag")
        self.assertTrue(same["unchanged"])
        self.assertEqual(same["version"], 2)
        self.assertEqual(core.canvas_show(self.c, "release-plan", version=1)["shown"]["body"], "<h1>Plan</h1>")
        self.assertIn("canvas release-plan v2", " ".join(e["change"] for e in core.item_show(self.c, self.it)["events"]))

    def test_put_keeps_the_canvas_on_its_own_item_while_the_writer_holds_another_one(self):
        """#4841: a put with no --item keeps the canvas's own item; the held item only names a new canvas's item."""
        a = core.canvas_put(self.c, "pages", "# v1", "ag", kind="md")
        self.assertEqual(a["item_id"], self.it)
        core.done(self.c, self.it, "ag")
        other = self.add("web", "the next one")
        core.claim(self.c, other, "ag")
        b = core.canvas_put(self.c, "pages", "# v2", "ag")
        self.assertEqual((b["version"], b["item_id"]), (2, self.it))
        self.assertEqual(core.canvas_put(self.c, "fresh", "# a", "ag", kind="md")["item_id"], other)

    def test_put_with_an_item_and_the_same_text_moves_the_canvas(self):
        a = core.canvas_put(self.c, "pages", "# v1", "ag", kind="md")
        other = self.add("web", "another")
        b = core.canvas_put(self.c, "pages", "# v1", "ag", item=other)
        self.assertEqual((b["unchanged"], b["version"], b["item_id"]), (True, 1, other))
        self.assertIn("canvas pages moved here from #%d" % self.it,
                      " ".join(e["change"] for e in core.item_show(self.c, other)["events"]))
        # The same text and the same item: nothing changes.
        self.assertEqual(core.canvas_put(self.c, "pages", "# v1", "ag", item=other)["item_id"], other)
        self.assertEqual(a["id"], b["id"])

    def test_kind_name_size_and_project_rules(self):
        with self.assertRaisesRegex(core.RiverError, "say the kind"):
            core.canvas_put(self.c, "x", "text", "ag")
        with self.assertRaisesRegex(core.RiverError, "canvas name"):
            core.canvas_put(self.c, "-bad", "text", "ag", kind="md")
        core.canvas_put(self.c, "notes", "# a", "ag", kind="markdown")
        with self.assertRaisesRegex(core.RiverError, "is md, not html"):
            core.canvas_put(self.c, "notes", "<p>a</p>", "ag", kind="html")
        core.config_set(self.c, "canvas_max", "10")
        with self.assertRaisesRegex(core.RiverError, "canvas_max"):
            core.canvas_put(self.c, "notes", "# a much longer page", "ag")
        with self.assertRaisesRegex(core.RiverError, "say the project"):
            core.canvas_put(self.c, "x", "# a", "mark", kind="md")
        # The same name in two projects: a name alone is not enough to read it.
        core.canvas_put(self.c, "notes", "# b", "mark", kind="md", project="api")
        with self.assertRaisesRegex(core.RiverError, "in the projects"):
            core.canvas_show(self.c, "notes")
        self.assertEqual(core.canvas_show(self.c, "notes", project="api")["shown"]["body"], "# b")

    def test_old_versions_go_but_version_one_stays(self):
        core.config_set(self.c, "canvas_versions", "3")
        for n in range(1, 7):
            d = core.canvas_put(self.c, "p", f"# v{n}", "ag", kind="md")
        self.assertEqual([v["version"] for v in d["versions"]], [1, 5, 6])  # three in all, version 1 among them

    def test_comments_are_drafts_until_sent_then_one_message_reaches_the_writer(self):
        core.canvas_put(self.c, "plan", "<h1>Plan</h1><p>Step one</p>", "ag", kind="html")
        a = core.canvas_comment(self.c, "plan", "Say when", "mark", {"quote": "Step one", "path": "p:nth-of-type(1)"})
        b = core.canvas_comment(self.c, "plan", "Good overall", "mark")
        self.assertEqual((a["state"], a["anchor"]["quote"], b["anchor"]), ("draft", "Step one", None))
        self.assertEqual(core.inbox(self.c, "ag", mark_read=False), [])
        sent = core.canvas_send(self.c, "plan", "mark")
        self.assertEqual((sent["sent"], sent["to"]), ([a["id"], b["id"]], "ag"))
        msg = core.message_show(self.c, sent["message"])
        self.assertEqual((msg["to_agent"], msg["item_id"]), ("ag", self.it))
        self.assertIn(f'c{a["id"]} on "Step one" (p:nth-of-type(1)): Say when', msg["body"])
        self.assertIn(f"c{b['id']} on the whole page: Good overall", msg["body"])
        with self.assertRaisesRegex(core.RiverError, "no draft comments"):
            core.canvas_send(self.c, "plan", "mark")
        # The writer reads them (sent becomes read), answers one and resolves it.
        got = core.canvas_comments(self.c, "plan", "ag", open_only=True)
        self.assertEqual([r["state"] for r in got["comments"]], ["read", "read"])
        core.canvas_reply(self.c, f"c{a['id']}", "Dates added in v2", "ag")
        core.canvas_resolve(self.c, a["id"], "ag", "v2")
        left = core.canvas_comments(self.c, "plan", "ag", open_only=True)["comments"]
        self.assertEqual([r["id"] for r in left], [b["id"]])
        full = core.canvas_show(self.c, "plan")["comments"]
        done = [r for r in full if r["id"] == a["id"]][0]
        self.assertEqual((done["state"], done["resolve_note"], done["replies"][0]["body"]), ("resolved", "v2", "Dates added in v2"))
        # The reply reaches the person who wrote the comment.
        notes = core.inbox(self.c, "mark", mark_read=False)
        self.assertTrue(any("Dates added in v2" in m["body"] for m in notes))

    def test_when_the_writer_is_gone_the_comments_go_to_the_items_next_holder(self):
        core.canvas_put(self.c, "plan", "# Plan", "ag", kind="md")
        core.release(self.c, self.it, actor="ag")
        core.unregister(self.c, "ag")
        core.canvas_comment(self.c, "plan", "More detail", "mark")
        sent = core.canvas_send(self.c, "plan", "mark")
        # #4769: nobody holds the open item, so a fresh session takes it, with the comments (fresh_sessions).
        self.assertEqual(sent["to"], f"a fresh session for #{self.it}")
        self.assertEqual(core.message_show(self.c, sent["message"])["item_id"], self.it)
        self.assertEqual(core.fresh_items(self.c), [self.it])
        # A closed item: a new item for the comments, found during it, which a fresh session takes.
        core.config_set(self.c, "fresh_sessions", "on")
        with core.tx(self.c):
            self.c.execute("UPDATE items SET status='done', closed_at='2026-11-09T00:00:00Z' WHERE id=?", (self.it,))
        core.canvas_comment(self.c, "plan", "And a summary", "mark")
        later = core.canvas_send(self.c, "plan", "mark")
        new = core.item_show(self.c, later["item"])
        self.assertEqual((new["title"], new["found_during"]), ("Answer the comments on canvas plan", self.it))
        self.assertIn("And a summary", new["context"])
        self.assertEqual(later["to"], f"a fresh session for #{later['item']}")
        self.assertEqual(core.fresh_items(self.c), [later["item"]])
        # A canvas with no item and no writer: nobody would get them.
        core.canvas_put(self.c, "loose", "# x", "mark", kind="md", project="api")
        core.canvas_comment(self.c, "loose", "hm", "mark")
        with self.assertRaisesRegex(core.RiverError, "no item"):
            core.canvas_send(self.c, "loose", "mark")

    def test_a_persons_edit_discard_and_the_go_briefing(self):
        core.canvas_put(self.c, "notes", "# Notes", "ag", kind="md")
        res = core.canvas_edit(self.c, "notes", "# Notes\n\nMore.", "mark")
        self.assertEqual((res["version"], res["agent"]), (2, "ag"), "a person's edit keeps the session as the writer")
        self.assertTrue(any("mark edited canvas notes" in m["body"] for m in core.inbox(self.c, "ag", mark_read=False)))
        core.canvas_comment(self.c, "notes", "Shorter", "mark")
        sent = core.canvas_send(self.c, "notes", "mark")
        self.assertEqual(sent["to"], "ag", "the comments still go to the session")
        with self.assertRaisesRegex(core.RiverError, "only a Markdown canvas"):
            core.canvas_put(self.c, "page", "<p>x</p>", "ag", kind="html")
            core.canvas_edit(self.c, "page", "<p>y</p>", "mark")
        d = core.canvas_comment(self.c, "notes", "draft", "mark")
        with self.assertRaisesRegex(core.RiverError, "not a draft of yours"):
            core.canvas_discard(self.c, d["id"], "ag")
        self.assertEqual(core.canvas_discard(self.c, d["id"], "mark"), {"discarded": d["id"]})
        with self.assertRaisesRegex(core.RiverError, "not a draft of yours"):
            core.canvas_discard(self.c, sent["sent"][0], "mark")
        core.release(self.c, self.it, actor="ag")
        brief = core.go(self.c, self.dir.name, actor="ag", project="web")
        self.assertEqual(brief["item"]["id"], self.it)
        self.assertEqual({x["name"]: x["open_comments"] for x in brief["canvases"]}, {"notes": 1, "page": 0})
        import contextlib
        import io
        from river import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.render_go(brief)  # it prints the briefing
        self.assertIn("canvas notes v2: 1 open comment(s) wait for a change", buf.getvalue())

    def test_list_delete_bring_back_and_save(self):
        core.canvas_put(self.c, "plan", "# Plan", "ag", kind="md")
        core.canvas_comment(self.c, "plan", "x", "mark")
        row = core.canvas_list(self.c, project="web")[0]
        self.assertEqual((row["name"], row["drafts"], row["open_comments"]), ("plan", 1, 0))
        self.assertEqual([r["name"] for r in core.canvas_list(self.c, mine="ag")], ["plan"])
        core.canvas_delete(self.c, "plan", "ag")
        self.assertEqual(core.canvas_list(self.c), [])
        with self.assertRaisesRegex(core.RiverError, "no canvas plan"):
            core.canvas_show(self.c, "plan")
        back = core.canvas_put(self.c, "plan", "# Plan 2", "ag")
        self.assertEqual(back["version"], 2)
        path = os.path.join(self.dir.name, "out")
        os.mkdir(path)
        saved = core.canvas_save(self.c, "plan", path, version=1)
        self.assertEqual(open(saved["path"], encoding="utf-8").read(), "# Plan")
        with self.assertRaisesRegex(core.RiverError, "exists"):
            core.canvas_save(self.c, "plan", path)

    def test_the_command_reads_a_file_or_stdin_and_the_kind_from_the_file_name(self):
        from river import cli
        f = os.path.join(self.dir.name, "chart.svg")
        with open(f, "w", encoding="utf-8") as fh:
            fh.write("<svg></svg>")
        old = os.environ.get("MAXPM_DB")
        os.environ["MAXPM_DB"] = self.path
        self.addCleanup(lambda: os.environ.__setitem__("MAXPM_DB", old) if old else os.environ.pop("MAXPM_DB", None))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("sys.stdin", io.StringIO("# from stdin")):
            cli.run(["-q", "--as", "ag", "canvas", "put", f, "--name", "chart"])
            cli.run(["-q", "--as", "ag", "canvas", "put", "-", "--name", "doc", "--kind", "md"])
            cli.run(["-q", "--as", "ag", "canvas", "show", "doc", "--text"])
        self.assertIn("chart  v1 svg  web", out.getvalue())
        self.assertTrue(out.getvalue().endswith("# from stdin"))


class ShortTitles(Base):
    """#5086: each item has a short title that the pages show (about 6 words); the title stays the full text. A session
    says what it does now with maxpm now."""

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "web")
        core.register(self.c, "ag")

    def test_add_edit_and_the_rules(self):
        iid = core.item_add(self.c, "web", "Make the GitHub tests workflow green: fix the 7 Windows failures",
                            actor="ag", short="  Fix the  Windows test runs ")["id"]
        self.assertEqual(core.item_show(self.c, iid)["short"], "Fix the Windows test runs")
        core.item_edit(self.c, iid, short="Green tests on Windows", actor="ag")
        self.assertEqual(core.item_show(self.c, iid)["short"], "Green tests on Windows")
        self.assertIn("short title: Green tests on Windows", [e["change"] for e in core.item_show(self.c, iid)["events"]])
        for bad in ("Fix #12 now", "Edit river/core.py", "see https://x.io", "one two three four five six seven eight nine"):
            with self.assertRaises(core.RiverError, msg=bad):
                core.item_edit(self.c, iid, short=bad, actor="ag")
        core.item_edit(self.c, iid, short="", actor="ag")
        self.assertIsNone(core.item_show(self.c, iid)["short"])

    def test_the_fallback_is_the_first_words_of_the_title(self):
        st = core.short_title
        self.assertEqual(st("#5073 Make the GitHub tests workflow of maximizepm green: fix the 7 Windows failures"),
                         "Make the GitHub tests workflow of maximizepm…")
        self.assertEqual(st("When the Quiet look (#5043) is done: new screenshots"), "When the Quiet look is done")
        self.assertEqual(st("Fix it: now"), "Fix it: now")  # fewer than three words before the colon: no cut there
        self.assertEqual(st("long title", "Short one"), "Short one")
        self.assertEqual(st("x" * 80), "x" * 59 + "…")

    def test_now_and_the_briefing(self):
        from river import cli
        iid = core.item_add(self.c, "web", "A long title of the work: with more words", actor="t")["id"]
        os.environ["MAXPM_DB"] = self.path
        self.addCleanup(os.environ.pop, "MAXPM_DB", None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(["-q", "--as", "ag", "now", "Runs the tests after the fix", "--work", "Windows tests"])
            cli.render_go(core.go(self.c, None, "ag", project="web"))
        a = core._agent(self.c, "ag")
        self.assertEqual((a["doing"], a["headline"]), ("Runs the tests after the fix", "Windows tests"))
        self.assertIn(f'short:   none yet. Write it now: maxpm --as ag edit {iid} --short', out.getvalue())
        self.assertIn('maxpm --as ag now "<about 10 plain words>"', out.getvalue())
        with self.assertRaises(core.RiverError):
            core.agent_now(self.c, "ag", "Edits river/cli.py")
        with self.assertRaises(core.RiverError):
            core.agent_now(self.c, "ag", "")



class Policies(Base):
    """Policies (#5308): global ones ship with MaximizePM (river/policies.json); user ones live in the queue, with
    scope, source and reason. The briefings print the ones that apply. The texts here are placeholders: a person's
    own rules never go into the repo."""

    def setUp(self):
        super().setUp()
        g = os.path.join(self.dir.name, "policies.json")
        with open(g, "w", encoding="utf-8") as f:
            json.dump({"policies": [{"id": "g1", "text": "Global rule A", "reason": "reason A", "source": "tester 2026-01-01"},
                                    {"id": "g2", "text": "Global rule B", "reason": "reason B", "source": "tester 2026-01-01",
                                     "role": "manager"}]}, f)
        patch = mock.patch.object(core, "GLOBAL_POLICIES_FILE", g)
        patch.start()
        self.addCleanup(patch.stop)
        core.project_add(self.c, "web", path=self.dir.name)
        core.project_add(self.c, "api")
        self.it = self.add("web", "Build the header")
        self.q = core.policy_add(self.c, "Rule Q", "reason Q", "tester 2026-01-01", actor="tester")
        self.p = core.policy_add(self.c, "Rule P", "reason P", "tester 2026-01-01", project="web", items=[str(self.it)],
                                 adds_to="g1")
        self.r = core.policy_add(self.c, "Rule R", "reason R", role="reviewer", actor="tester")
        self.m = core.policy_add(self.c, "Rule M", "reason M", role="manager")
        self.o = core.policy_add(self.c, "Rule O", "reason O", project="api")
        self.w = core.policy_add(self.c, "Rule W", "reason W", project="web", role="worker")

    def ids(self, rows):
        return [p["pid"] for p in rows]

    def test_add_checks_and_defaults(self):
        self.assertEqual((self.p["kind"], self.p["scope"], self.p["adds_to"]), ("user", "user: web", "g1"))
        self.assertEqual(self.p["items"][0]["id"], self.it)
        self.assertEqual(self.w["scope"], "user: web, worker")
        self.assertTrue(self.r["source"].startswith("tester 20"))  # no source: the actor, today
        with self.assertRaisesRegex(RiverError, "say why"):
            core.policy_add(self.c, "x", "")
        with self.assertRaisesRegex(RiverError, "no policy role"):
            core.policy_add(self.c, "x", "y", role="boss")
        with self.assertRaisesRegex(RiverError, "no item"):
            core.policy_add(self.c, "x", "y", items=["9999"])
        with self.assertRaisesRegex(RiverError, "no project"):
            core.policy_add(self.c, "x", "y", project="nope")
        with self.assertRaisesRegex(RiverError, "no global policy"):
            core.policy_add(self.c, "x", "y", adds_to="g9")

    def test_global_policies_change_only_in_the_repo(self):
        for f in (lambda: core.policy_edit(self.c, "g1", text="x"), lambda: core.policy_retire(self.c, "G1")):
            with self.assertRaisesRegex(RiverError, "g1 is a global policy: it ships with MaximizePM"):
                f()
        g = core.policy_show(self.c, "g1")
        self.assertEqual((g["kind"], g["scope"]), ("global", "global: queue"))
        self.assertEqual(self.ids(g["added_by_user"]), [self.p["pid"]])
        self.assertEqual(self.ids(core.policy_list(self.c, kind="global")), ["g1", "g2"])
        self.assertEqual(self.ids(core.policy_list(self.c, project="web")), [self.p["pid"], self.w["pid"]])

    def test_what_applies_to_each_role(self):
        q, p, r, m, o, w = (x["pid"] for x in (self.q, self.p, self.r, self.m, self.o, self.w))
        self.assertEqual(self.ids(core.policies_for(self.c, ["web"], "worker")), ["g1", q, p, w])
        self.assertEqual(self.ids(core.policies_for(self.c, ["web"], "reviewer")), ["g1", q, r, p, w])  # a worker too
        self.assertEqual(self.ids(core.policies_for(self.c, ["api"], "expert")), ["g1", q, o])
        self.assertEqual(self.ids(core.policies_for(self.c, role="manager")), ["g1", "g2", q, m])

    def test_a_global_policy_of_several_roles(self):
        g = os.path.join(self.dir.name, "policies2.json")
        with open(g, "w", encoding="utf-8") as f:
            json.dump({"policies": [{"id": "g1", "text": "Rule MW", "reason": "r", "source": "tester 2026-01-01",
                                     "role": ["manager", "worker"]}]}, f)
        with mock.patch.object(core, "GLOBAL_POLICIES_FILE", g):
            (g1,) = core.policy_list(self.c, kind="global")
            self.assertEqual((g1["scope"], g1["roles"]), ("global: manager, worker", ["manager", "worker"]))
            for role, has in (("manager", True), ("worker", True), ("reviewer", True), ("expert", False)):
                self.assertEqual("g1" in self.ids(core.policies_for(self.c, ["web"], role)), has, role)
            self.assertEqual(self.ids(core.policy_list(self.c, role="manager", kind="global")), ["g1"])
            self.assertEqual(self.ids(core.policy_list(self.c, role="expert", kind="global")), [])

    def test_go_and_manage_briefings_print_them(self):
        b = core.go(self.c, self.dir.name, "w1")
        self.assertEqual(b["item"]["id"], self.it)
        self.assertEqual(self.ids(b["policies"]), ["g1", self.q["pid"], self.p["pid"], self.w["pid"]])
        from river import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.render_go(b)
        self.assertIn("POLICIES", out.getvalue())
        self.assertIn("g1 [global: queue] Global rule A  (tester 2026-01-01)", out.getvalue())
        self.assertIn(f"{self.p['pid']} [user: web] Rule P  (tester 2026-01-01; adds to g1)", out.getvalue())
        m = core.manage(self.c, self.dir.name, "boss")
        self.assertEqual(self.ids(m["policies"]), ["g1", "g2", self.q["pid"], self.m["pid"]])
        self.assertEqual(m["other_policies"], 4)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.render_manage(m)
        self.assertIn("and 4 policy(ies) of projects or other roles", out.getvalue())

    def test_a_folder_policy_reaches_every_project_of_the_folder(self):
        # Several projects share one folder (#5376): a folder policy applies to each of their sessions, by the
        # session's folder or by the folder of its item's project, and to no session elsewhere.
        shared = os.path.join(self.dir.name, "repo")
        os.makedirs(os.path.join(shared, "sub"))
        core.project_add(self.c, "front", path=shared)
        core.project_add(self.c, "back", path=shared)
        f = core.policy_add(self.c, "Rule F", "reason F", folder=shared, role="worker")
        self.assertEqual(f["folder"], os.path.realpath(shared))
        self.assertTrue(f["scope"].startswith("user: folder ") and f["scope"].endswith("repo, worker"), f["scope"])
        self.assertIn(f["pid"], self.ids(core.policies_for(self.c, ["back"], "worker")))
        self.assertIn(f["pid"], self.ids(core.policies_for(self.c, [], "worker", cwd=os.path.join(shared, "sub"))))
        self.assertNotIn(f["pid"], self.ids(core.policies_for(self.c, ["api", "web"], "worker")))
        self.assertNotIn(f["pid"], self.ids(core.policies_for(self.c, ["front"], "expert")))
        self.assertNotIn(f["pid"], self.ids(core.policies_for(self.c, role="manager")))  # no manager role
        g = core.policy_add(self.c, "Rule FM", "reason", folder=shared, role="manager")
        self.assertIn(g["pid"], self.ids(core.policies_for(self.c, role="manager")))
        with self.assertRaisesRegex(RiverError, "not both"):
            core.policy_add(self.c, "x", "y", project="web", folder=shared)
        with self.assertRaisesRegex(RiverError, "no folder"):
            core.policy_add(self.c, "x", "y", folder=os.path.join(self.dir.name, "nope"))
        e = core.policy_edit(self.c, f["pid"], project="web")
        self.assertEqual((e["folder"], e["scope"]), (None, "user: web, worker"))
        e = core.policy_edit(self.c, f["pid"], folder=shared)
        self.assertEqual((e["project"], e["folder"]), (None, os.path.realpath(shared)))
        self.assertEqual(core.policy_edit(self.c, f["pid"], no_folder=True)["scope"], "user: worker")
        # go passes its folder: a session in a subfolder of the shared repo gets the folder policy.
        h = core.policy_add(self.c, "Rule FG", "reason", folder=shared)
        b = core.go(self.c, os.path.join(shared, "sub"), "w2")
        self.assertIn(h["pid"], self.ids(b["policies"]))

    def test_edit_and_retire(self):
        p = core.policy_edit(self.c, self.p["pid"], text="Rule P2", no_project=True, role="expert", items=[""],
                             adds_to="none")
        self.assertEqual((p["text"], p["scope"], p["items"], p["adds_to"]), ("Rule P2", "user: expert", [], None))
        self.assertTrue(p["changed_at"])
        p = core.policy_edit(self.c, p["id"], project="api", role="any", adds_to="g2")
        self.assertEqual((p["scope"], p["adds_to"]), ("user: api", "g2"))
        with self.assertRaisesRegex(RiverError, "say what changes"):
            core.policy_edit(self.c, p["id"])
        core.policy_retire(self.c, f"p{p['id']}", actor="tester")
        with self.assertRaisesRegex(RiverError, "no policy"):
            core.policy_show(self.c, p["id"])
        self.assertNotIn(p["pid"], self.ids(core.policy_list(self.c)))
        self.assertIn("retired", core.recent_events(self.c)[0]["change"])  # the history keeps one line

    def test_cli_and_state(self):
        from river import cli
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"MAXPM_DB": self.path}), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            cli.run(["--as", "tester", "policy", "add", "Rule N", "--reason", "reason N", "--role", "owner",
                     "--adds-to", "g1"])
            cli.run(["policy", "list", "--for", "owner", "--project", "web"])
            cli.run(["policy", "show", "g1"])
        self.assertIn("added p7 [user: owner] Rule N", out.getvalue())
        self.assertIn("Name it in your item's output", out.getvalue())
        self.assertIn("p7 [user: owner] Rule N", out.getvalue().split("Name it")[1])
        self.assertIn("your addition: p7 [user: owner] Rule N", out.getvalue())
        st = core.state(self.c)
        self.assertEqual([p["kind"] for p in st["policies"]], ["global"] * 2 + ["user"] * 7)
        self.assertIn("reviewer", st["policy_roles"])


class AnnotateOnce(Base):
    """Inside one build of the page's state and one command, annotate reads the item graph once while nothing
    writes (#5465, #5491)."""

    def test_one_read_until_a_write(self):
        core.project_add(self.c, "web")
        self.add("web", "First")
        runs = []
        real = core._annotate
        with mock.patch.object(core, "_annotate", side_effect=lambda c, closed=True: runs.append(1) or real(c, closed)):
            tok = core.ANN_SHARE.set({})
            try:
                a = core.annotate(self.c)
                b = core.annotate(self.c)
                self.assertEqual(b, a)
                self.assertEqual(len(runs), 1)
                b[1]["title"] = "changed"  # each call has its own copy of an item (#5491)
                self.assertEqual(core.annotate(self.c)[1]["title"], "First")
                self.add("web", "Second")  # a write: the next read sees it
                n = len(runs)
                self.assertEqual(len(core.annotate(self.c)), 2)
                self.assertEqual(len(runs), n + 1)
                # A whole result serves a caller of the open items too; it computes them anew only after a write.
                self.assertEqual(list(core.annotate(self.c, closed=False)), [1, 2])
                self.assertEqual(len(runs), n + 1)
            finally:
                core.ANN_SHARE.reset(tok)
            n = len(runs)
            core.annotate(self.c)
            core.annotate(self.c)  # outside a build: each call reads anew
            self.assertEqual(len(runs), n + 2)

    def test_the_open_items_and_a_closed_one_on_request(self):
        core.project_add(self.c, "web")
        a, b = self.add("web", "First"), self.add("web", "Second")
        self.c.execute("UPDATE items SET status='done', closed_at='2026-01-01T00:00:00Z' WHERE id=?", (a,))
        full = core.annotate(self.c)
        part = core.annotate(self.c, closed=False)
        self.assertEqual(list(part), [b])  # the iteration: open items only
        self.assertIn(a, part)
        self.assertEqual(part[a], full[a])  # a closed item, computed when asked for, is the same
        self.assertEqual(part.get(a), full[a])
        self.assertIsNone(part.get(999))
        self.assertEqual(part.light(a)["status"], "done")


class OpsProbes(Base):
    """maxpm ops probe (#5440): MaximizePM's own checks as Monitoring Plugins, read only."""

    def test_idle_work_needs_ready_work_and_no_session_for_the_window(self):
        core.project_add(self.c, "web")
        self.assertEqual(core.ops_probe(self.c, "idle-work")["status"], "ok")  # no work
        self.add("web", "Build it", doer="ai")
        self.assertEqual(core.ops_probe(self.c, "idle-work")["status"], "critical")  # work and never a session
        core.register(self.c, "w1")
        res = core.ops_probe(self.c, "idle-work")
        self.assertEqual((res["status"], res["perf"]["live"]), ("ok", 1))
        later = core.now() + core.timedelta(minutes=11)
        with mock.patch.object(core, "now", lambda: later), mock.patch.object(core, "live_sessions", lambda c: 0):
            res = core.ops_probe(self.c, "idle-work")
        self.assertEqual(res["status"], "critical")
        self.assertIn("no live session for 11m", res["summary"])

    def test_lease_and_release_loops_name_the_item(self):
        core.project_add(self.c, "web")
        a, b = self.add("web", "Loops"), self.add("web", "Calm")
        for _ in range(3):
            core._event(self.c, a, "maxpm", "lease expired (was w1); back to open")
        core._event(self.c, b, "maxpm", "lease expired (was w1); back to open")
        res = core.ops_probe(self.c, "lease-loop")
        self.assertEqual((res["status"], list(res["subjects"])), ("critical", [f"#{a}"]))
        self.assertIn("3 lease expiries in 6h", res["subjects"][f"#{a}"])
        core.config_set(self.c, "ops_lease_loop", "4/6h")
        self.assertEqual(core.ops_probe(self.c, "lease-loop")["status"], "ok")
        core.register(self.c, "w1")
        for _ in range(3):
            core.claim(self.c, b, "w1"); core.release(self.c, b, actor="w1", note="later")
        self.assertEqual(core.ops_probe(self.c, "release-loop")["status"], "ok")  # 3 of 4
        core.claim(self.c, b, "w1"); core.done(self.c, b, "w1"); core.reopen(self.c, b, actor="w1")
        res = core.ops_probe(self.c, "release-loop")
        self.assertEqual(list(res["subjects"]), [f"#{b}"])
        core._event(self.c, None, "w1", "released goal g; its items are open to every agent")  # no item: not counted
        self.assertEqual(len(core.ops_probe(self.c, "release-loop")["subjects"]), 1)
        for bad in ("3", "0/6h", "3/soon"):
            with self.assertRaises(RiverError):
                core.config_set(self.c, "ops_release_loop", bad)

    def test_the_command_prints_a_plugin_answer_and_its_exit_code(self):
        core.project_add(self.c, "web")
        a = self.add("web", "Loops")
        for _ in range(3):
            core._event(self.c, a, "maxpm", "lease expired (was w1); back to open")
        from river import cli
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"MAXPM_DB": self.path}), contextlib.redirect_stdout(out):
            code = cli.run(["ops", "probe", "lease-loop"])
            ok = cli.run(["ops", "probe", "release-loop"])
        lines = out.getvalue().splitlines()
        self.assertEqual((code, ok), (2, 0))
        self.assertTrue(lines[0].startswith("LEASE-LOOP CRITICAL - 1 item(s) with 3 or more lease expiries"), lines[0])
        self.assertIn("| items=1", lines[0])
        self.assertTrue(lines[1].startswith(f"#{a}: 3 lease expiries"))
        self.assertEqual(list(json.loads(lines[2])["subjects"]), [f"#{a}"])
        self.assertTrue(lines[3].startswith("RELEASE-LOOP OK - "))
        self.assertEqual(json.loads(lines[4]), {"subjects": {}})
        self.assertEqual(len(lines), 5)  # no footer, no hint


class OpsSelf(Base):
    """MaximizePM watches itself (#5442): the probes serve, relay and notices, and the profile maximizepm that ships
    in river/ops and that maxpm ops setup offers."""

    class Answer:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def serve(self, answers=True, loop=None):
        import urllib.request

        def urlopen(req, timeout=None):
            if not answers:
                raise OSError("Connection refused")
            return self.Answer()
        return (mock.patch.object(urllib.request, "urlopen", urlopen),
                mock.patch.object(core, "serve_loop", lambda conn: loop))

    def probe(self, name):
        return core.ops_probe(self.c, name)

    def test_serve_needs_an_answer_and_a_loop_that_is_not_late(self):
        a, b = self.serve(answers=False)
        with a, b:
            r = self.probe("serve")
        self.assertEqual(r["status"], "critical")
        self.assertIn("gives no answer on port", r["summary"])
        a, b = self.serve(loop=None)
        with a, b:
            self.assertIn("no maxpm serve loop of this queue runs", self.probe("serve")["summary"])
        late = {"started": core.iso(core.now() - timedelta(minutes=9)), "last_pass": core.iso(core.now() - timedelta(minutes=4)),
                "error": "boom"}
        a, b = self.serve(loop=late)
        with a, b:
            r = self.probe("serve")
        self.assertEqual(r["status"], "critical")  # notify_interval 30s x ops_serve_late 3, at least 1m
        self.assertIn("its loop passed no pass for 4m", r["summary"])
        self.assertIn("last error: boom", r["summary"])
        fresh = dict(late, last_pass=core.iso(core.now() - timedelta(seconds=20)))
        a, b = self.serve(loop=fresh)
        with a, b:
            r = self.probe("serve")
        self.assertEqual(r["status"], "ok")
        self.assertIn("loop_age_s", r["perf"])

    def test_relay_is_a_warning_while_serve_does_not_run(self):
        from river import relay

        def st(**kw):
            return mock.patch.object(relay, "saved_status", lambda: {"configured": True, "url": "https://r", **kw})
        with mock.patch.object(relay, "saved_status", lambda: {"configured": False}):
            self.assertEqual(self.probe("relay")["status"], "ok")
        with st(state="connected", since=core.iso(core.now())):
            self.assertEqual(self.probe("relay")["status"], "ok")
        with st(state="serve not running"):
            self.assertEqual(self.probe("relay")["status"], "warning")
        with st(state="waiting", error="timed out"):
            r = self.probe("relay")
        self.assertEqual((r["status"], r["summary"]), ("critical", "the relay https://r is waiting: timed out"))
        self.assertEqual(core.OPS_PROBE_EXIT["warning"], 1)

    def test_page_speed_is_the_p90_of_the_last_50_builds(self):
        # #5638: a warning over 100 ms, critical over 200 ms; a kept state (no build_ms) is no build.
        import urllib.request
        at = core.iso(core.now())

        def run(builds, served=10):
            rows = [{"at": at, "build_ms": b, "total_ms": b + 5} for b in builds] + [{"at": at, "total_ms": 0.1}] * served
            return core._probe_page_speed(self.c, rows)
        status, summary, perf = run([])
        self.assertEqual((status, perf["builds"]), ("ok", 0))
        status, summary, perf = run([500] * 10 + [60] * 45 + [130] * 5)  # the 10 slow ones are older than the last 50
        self.assertEqual((status, perf["builds"], perf["p90_ms"], perf["answer_p90_ms"]), ("ok", 50, 60, 65))
        self.assertIn("5 over 100 ms", summary)
        self.assertEqual(run([60] * 40 + [130] * 10)[0], "warning")
        status, summary, perf = run([60] * 40 + [250] * 10)
        self.assertEqual((status, perf["max_ms"]), ("critical", 250))
        self.assertIn("state-timing?profile=1", summary)
        with mock.patch.object(urllib.request, "urlopen", side_effect=OSError("Connection refused")):
            r = self.probe("page-speed")
        self.assertEqual(r["status"], "warning")  # serve down: the serve probe's fault
        self.assertIn("no build record on port", r["summary"])

    def test_notices_names_each_channel_whose_newest_attempt_failed(self):
        t = core.iso(core.now())
        eid = self.c.execute("INSERT INTO needs_you(kind,summary,opened_at) VALUES ('item','x',?)", (t,)).lastrowid
        eid2 = self.c.execute("INSERT INTO needs_you(kind,summary,opened_at) VALUES ('item','y',?)", (t,)).lastrowid
        rows = [(eid, "mac", "failed", 5, "OSError: off.\nEnable them"), (eid, "ntfy", "failed", 1, "down"),
                (eid2, "ntfy", "sent", 1, None), (eid2, "log", "pending", 0, None)]
        for r in rows:
            self.c.execute("INSERT INTO notifications(event_id,channel,state,attempts,last_error,created_at) "
                           "VALUES (?,?,?,?,?,?)", (*r, t))
        self.c.commit()
        r = self.probe("notices")
        self.assertEqual(r["status"], "critical")
        self.assertEqual(list(r["subjects"]), ["mac"])  # ntfy's newest went out
        self.assertIn("failed: OSError: off. Enable them", r["subjects"]["mac"])

    def test_the_shipped_profile_applies_to_its_own_project_and_watches_serve_from_outside(self):
        self.assertIn("maximizepm", core.ops_builtin_profiles())
        self.assertIsNone(core.ops_builtin_profile("../maximizepm"))
        prof = core.ops_builtin_profile("maximizepm")
        from river import cli
        out = io.StringIO()
        core.register(self.c, "mark", human=True)
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.run(["--as", "mark", "ops", "setup", "--profile", "maximizepm", "--yes"]), 0)
        self.assertIn(f"profile applied to project maximizepm: {len(prof['checks'])} added", out.getvalue())
        checks = {c["name"]: c for c in core.ops_list(self.c, "maximizepm")["checks"]}
        self.assertEqual(set(checks), {c["name"] for c in prof["checks"]})
        self.assertEqual(sorted(n for n, c in checks.items() if c["outside"]), ["relay", "serve"])
        self.assertEqual({checks[n]["severity"] for n in ("serve", "relay")}, {"page"})
        for c in checks.values():
            self.assertTrue(c["runbook"], c["name"])
            if c["outside"]:
                self.assertTrue(c["repair"], c["name"])
        # Each probe it runs exists, and the same apply again changes nothing.
        for c in checks.values():
            words = c["config"]["command"].split()
            if words[:3] == ["maxpm", "ops", "probe"]:
                self.assertIn(words[3], core.OPS_PROBES)
        self.assertEqual(sorted(core.ops_profile_apply(self.c, prof, yes=True)["same"]), sorted(checks))

    def test_setup_offers_maximizepm_itself_first_while_its_project_has_no_check(self):
        from river import cli
        core.register(self.c, "mark", human=True)
        core.project_add(self.c, "web")
        asked, replies = [], iter(["0", "", "n", ""])  # the project, no more checks, no phone, no outside switch

        def ask(q):
            asked.append(q)
            return next(replies)
        out = io.StringIO()
        with mock.patch.object(cli, "SETUP_ASK", ask), contextlib.redirect_stdout(out):
            cli.run(["--as", "mark", "ops", "setup"])
        text = out.getvalue()
        self.assertIn("  0. MaximizePM itself: 10 checks", text)
        self.assertIn("added 10 checks to the project maximizepm", text)
        self.assertIn("What should MaximizePM watch in maximizepm?", text)  # then more checks, as for any project
        self.assertEqual(len(core.ops_list(self.c, "maximizepm")["checks"]), 10)


class EditKeepsOldText(Base):
    """maxpm edit --notes or --context replaces the text; the history keeps the old one (#5384)."""

    def test_the_history_keeps_the_old_notes_and_context(self):
        core.project_add(self.c, "web")
        i = self.add("web", "Build the header")
        core.item_edit(self.c, i, notes="first notes", context="first context", actor="a")
        core.item_edit(self.c, i, notes="second", context="second context", actor="a")
        ev = [e["change"] for e in core.item_show(self.c, i)["events"]]
        self.assertIn("notes changed; the old notes: first notes", ev)
        self.assertIn("context changed; the old context: first context", ev)
        self.assertIn("notes changed", ev)  # the first text replaced nothing: no old text to keep


class ShippedPolicies(unittest.TestCase):
    """The global policies file that ships with MaximizePM (#5308) loads, and each entry is whole."""

    def test_the_shipped_file_is_valid(self):
        with open(core.GLOBAL_POLICIES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        ids = [r["id"] for r in data["policies"]]
        self.assertEqual(len(ids), len(set(ids)), "a global policy id is used twice")
        for r in data["policies"]:
            self.assertRegex(r["id"], r"^g[1-9]\d*$")
            self.assertLessEqual(set(r), {"id", "text", "reason", "source", "role"}, "a global policy names no project")
            for k in ("text", "reason", "source"):
                self.assertTrue(r.get(k, "").strip(), f"{r['id']} has no {k}")
            roles = r.get("role")
            for x in ([roles] if isinstance(roles, str) or roles is None else roles):
                self.assertIn(x, (None,) + core.POLICY_ROLES)
        self.assertEqual(len(core.global_policies()), len(ids))


class SessionSamples(Base):
    """maxpm serve records each live session's state once a minute, so maxpm health can show idle time (#5352)."""

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "a")
        core.project_add(self.c, "b")
        self.x = self.add("a", "x")
        self.y = self.add("a", "y")
        self.z = self.add("b", "z")
        core.register(self.c, "mark", human=True)
        for n in ("w1", "w2", "w3", "boss", "ex", "old"):
            core.register(self.c, n)
        core.claim(self.c, self.x, "w1")
        core.claim(self.c, self.y, "w3")
        core.claim(self.c, self.z, "w2")
        core.done(self.c, self.z, actor="w2")  # w2 has nothing in hand now; it worked in project b
        self.c.execute("UPDATE agents SET role='manager' WHERE name='boss'")
        self.c.execute("UPDATE agents SET role='expert' WHERE name='ex'")
        self.c.execute("UPDATE agents SET stop_at=? WHERE name='old'", (core.iso(core.now()),))
        self.c.commit()
        core.message(self.c, "question", "Push it?", to="mark", item=self.y, actor="w3")
        self.t0 = core.now()

    def rows(self):
        return {r["agent"]: dict(r) for r in self.c.execute("SELECT * FROM session_samples WHERE state<>'none'")}

    def test_each_live_session_gets_its_state(self):
        self.assertEqual(core.sample_sessions(self.c, at=self.t0), 5)
        r = self.rows()
        self.assertEqual({a: v["state"] for a, v in r.items()},
                         {"w1": "working", "w2": "waiting_work", "w3": "waiting_person", "boss": "manager",
                          "ex": "expert"})
        self.assertEqual(r["w1"]["item_id"], self.x)
        pa = self.c.execute("SELECT id FROM projects WHERE name='a'").fetchone()["id"]
        pb = self.c.execute("SELECT id FROM projects WHERE name='b'").fetchone()["id"]
        self.assertEqual(r["w1"]["project_id"], pa)
        self.assertEqual(r["w2"]["project_id"], pb)  # the project of the item it held last
        self.assertEqual(r["w1"]["secs"], 60)

    def test_at_most_once_per_sample_every_and_a_late_pass_counts_little(self):
        core.sample_sessions(self.c, at=self.t0)
        self.assertEqual(core.sample_sessions(self.c, at=self.t0 + core.timedelta(seconds=30)), 0)
        self.assertEqual(core.sample_sessions(self.c, at=self.t0 + core.timedelta(seconds=58)), 5)
        core.sample_sessions(self.c, at=self.t0 + core.timedelta(hours=3))  # serve was down: 5 minutes, not 3 hours
        secs = [r["secs"] for r in self.c.execute("SELECT secs FROM session_samples WHERE agent='w1' ORDER BY at")]
        self.assertEqual(secs, [60, 58, 300])

    def test_no_session_records_only_the_time(self):
        self.c.execute("UPDATE agents SET stop_at=? WHERE kind='ai'", (core.iso(core.now()),))
        self.c.commit()
        self.assertEqual(core.sample_sessions(self.c, at=self.t0), 0)
        self.assertEqual(self.rows(), {})
        self.assertEqual(core.idle_share(self.c, since=self.t0 - core.timedelta(minutes=1),
                                         until=self.t0 + core.timedelta(minutes=1))["total_secs"], 0)

    def test_old_rows_go_and_0s_turns_it_off(self):
        core.sample_sessions(self.c, at=self.t0 - core.timedelta(days=91))
        core.sample_sessions(self.c, at=self.t0)
        ats = {r["at"] for r in self.c.execute("SELECT at FROM session_samples")}
        self.assertEqual(ats, {core.iso(self.t0)})
        core.config_set(self.c, "sample_keep", "200d")
        core.config_set(self.c, "sample_every", "0s")
        self.assertEqual(core.sample_sessions(self.c, at=self.t0 + core.timedelta(hours=1)), 0)
        with self.assertRaises(core.RiverError):
            core.config_set(self.c, "sample_every", "often")

    def test_idle_share(self):
        for m in range(3):
            core.sample_sessions(self.c, at=self.t0 + core.timedelta(minutes=m))
        s = core.idle_share(self.c, since=self.t0, until=self.t0 + core.timedelta(minutes=5))
        self.assertEqual(s["total_secs"], 5 * 180)
        self.assertEqual(s["sessions"], 5)
        self.assertEqual(s["samples"], 15)
        self.assertAlmostEqual(s["idle"], 0.2)
        self.assertAlmostEqual(s["share"]["working"], 0.2)
        self.assertEqual(s["first"], core.iso(self.t0))
        a = core.idle_share(self.c, since=self.t0, until=self.t0 + core.timedelta(minutes=5), project="a")
        self.assertEqual(a["sessions"], 2)  # w1 working and w3 waiting on mark; w2's last item is in b
        self.assertEqual(a["idle"], 0)
        b = core.idle_share(self.c, since=self.t0, until=self.t0 + core.timedelta(minutes=5), project="b")
        self.assertEqual(b["idle"], 1.0)
        # since as a duration back from until: 1m before t0+2m holds only the last pass (t0+1m is at the edge)
        last = core.idle_share(self.c, "1m", until=self.t0 + core.timedelta(minutes=2, seconds=1))
        self.assertEqual(last["samples"], 5)


class Health(Base):
    """maxpm health: the measures of the canvas does-maxpm-work over a time and the time before (#5353)."""

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "a")
        core.project_add(self.c, "b")
        self.end = core.parse_iso("2026-11-11T00:00:00Z")
        self.ids = []

    def at(self, days_ago):
        return core.iso(self.end - core.timedelta(days=days_ago))

    def done_item(self, project, days_ago, kind="work", eq=None, context=""):
        i = self.add(project, f"item {len(self.ids)}")
        self.c.execute("UPDATE items SET status='done', kind=?, created_at=?, closed_at=?, context=? WHERE id=?",
                       (kind, self.at(days_ago + 0.5), self.at(days_ago), context, i))
        if eq is not None:
            self.c.execute("INSERT INTO item_usage VALUES (?, ?, 'w', 'fresh', 10, ?, 0, 0, 0, 100, 1000, ?)",
                           (i, f"s{i}", eq, self.at(days_ago)))
        self.ids.append(i)
        return i

    def needs_you(self, item, opened_days_ago, closed_days_ago=None):
        self.c.execute("INSERT INTO needs_you (kind, item_id, human, summary, opened_at, closed_at) "
                       "VALUES ('item', ?, 'mark', 's', ?, ?)",
                       (item, self.at(opened_days_ago),
                        None if closed_days_ago is None else self.at(closed_days_ago)))

    def event(self, item, days_ago, change):
        self.c.execute("INSERT INTO events (item_id, at, actor, change) VALUES (?, ?, 'w', ?)",
                       (item, self.at(days_ago), change))

    def seed(self):
        # The last 7 days: four done items in a, two in b; the 7 days before: two in a.
        x = [self.done_item("a", d, eq=e) for d, e in ((1, 100), (2, 300), (3, 200), (4, None))]
        x += [self.done_item("b", 1, kind="review", eq=50), self.done_item("b", 2)]
        self.done_item("a", 8, eq=1000)
        self.done_item("a", 9, eq=3000)
        self.event(x[0], 1, "reopened (was done)")
        self.event(x[1], 2, "lease expired (was w); back to open")
        self.event(x[1], 9, "lease expired (was w); back to open")
        self.event(x[2], 3, "left the queue (claimed)")  # not rework: it left an agent's own queue
        fix = self.add("a", "fix it")
        self.c.execute("UPDATE items SET created_at=?, context=? WHERE id=?",
                       (self.at(2), core.REVIEW_FIX_CONTEXT + " t (#1)", fix))
        self.needs_you(x[0], 3, 1)        # waited 48h
        self.needs_you(x[1], 2.5, 2)      # waited 12h
        self.needs_you(x[4], 1)           # still open: 24h old at the end
        self.needs_you(x[2], 9, 8.5)      # before: waited 12h
        self.c.commit()

    def test_the_measures_and_the_time_before(self):
        self.seed()
        h = core.health(self.c, "7d", until=self.end)
        n, b = h["now"], h["before"]
        self.assertEqual((n["done"], b["done"]), (6, 2))
        self.assertAlmostEqual(n["per_day"], 6 / 7)
        self.assertEqual(n["by_kind"], {"work": 5, "review": 1})
        self.assertEqual(n["by_project"], {"a": 4, "b": 2})
        self.assertEqual((n["reopens"], n["lost_claims"], n["review_fixes"]), (1, 1, 1))
        self.assertAlmostEqual(n["rework_rate"], 3 / 6)
        self.assertAlmostEqual(b["rework_rate"], 1 / 2)
        self.assertEqual(n["needs_you_opened"], 3)
        self.assertAlmostEqual(n["person_load"], 50)
        self.assertAlmostEqual(n["person_minutes_per_item"], 3 * 3 / 6)
        self.assertAlmostEqual(n["wait_mean_h"], 30)
        self.assertEqual(n["wait_p90_h"], 48)
        self.assertEqual(n["open_now"], 1)
        self.assertAlmostEqual(n["open_mean_age_h"], 24)
        self.assertEqual(b["wait_mean_h"], 12)
        self.assertEqual(n["cost_measured"], 4)
        self.assertEqual(n["cost_median_eq"], 150)
        self.assertEqual(n["cost_by_kind"], {"review": {"items": 1, "median_eq": 50},
                                             "work": {"items": 3, "median_eq": 200}})
        self.assertEqual(b["cost_median_eq"], 2000)

    def test_one_project_and_the_minutes_setting(self):
        self.seed()
        core.config_set(self.c, "health_person_minutes", "10")
        h = core.health(self.c, "7d", until=self.end, project="b")
        self.assertEqual(h["now"]["done"], 2)
        self.assertEqual(h["now"]["needs_you_opened"], 1)
        self.assertAlmostEqual(h["now"]["person_minutes_per_item"], 10 / 2)
        self.assertEqual(h["now"]["rework_rate"], 0)
        with self.assertRaises(core.RiverError):
            core.config_set(self.c, "health_person_minutes", "a few")
        with self.assertRaises(core.RiverError):
            core.health(self.c, "7d", until=self.end, project="nope")

    def test_the_change_log_shows_before_and_after(self):
        self.seed()
        core.register(self.c, "mark", human=True)
        core.register(self.c, "w")
        with self.assertRaises(core.RiverError) as e:
            core.change_add(self.c, "manager first", at=self.at(4), actor="w")
        self.assertIn("a person or the manager keeps the change log", str(e.exception))
        with self.assertRaises(core.RiverError):
            core.change_add(self.c, "later", at=core.now() + core.timedelta(days=1), actor="mark")
        c1 = core.change_add(self.c, "manager first", at=self.end - core.timedelta(days=4), actor="mark")
        core.change_add(self.c, "goals", at=self.end - core.timedelta(days=2), actor="mark")
        old = core.change_add(self.c, "long ago", at=self.end - core.timedelta(days=20), actor="mark")
        self.assertEqual([c["name"] for c in core.change_list(self.c)], ["long ago", "manager first", "goals"])
        h = core.health(self.c, "7d", until=self.end)
        self.assertEqual([c["name"] for c in h["changes"]], ["manager first", "goals"])
        first, second = h["changes"]
        # Before the first: 7 days, back to no change (items closed 8 and 9 days ago); after: cut at the second.
        self.assertEqual((first["before"]["done"], first["before"]["days"]), (2, 7))
        self.assertEqual((first["after"]["done"], first["after"]["days"]), (2, 2))  # closed 4 and 3 days ago
        self.assertEqual((second["before"]["done"], second["before"]["days"]), (2, 2))
        self.assertEqual((second["after"]["done"], second["after"]["days"]), (4, 2))  # cut at until
        self.assertAlmostEqual(second["after"]["per_day"], 2)
        self.assertEqual(first["id"], c1["id"])
        core.change_remove(self.c, f"c{old['id']}", actor="mark")
        self.assertEqual(len(core.change_list(self.c)), 2)
        with self.assertRaises(core.RiverError):
            core.change_remove(self.c, old["id"], actor="mark")

    def test_by_goal_compares_inside_each_project(self):
        g = core.goal_add(self.c, "a", "g1", "an outcome", "done when")
        gb = core.goal_add(self.c, "b", "g2", "an outcome", "done when")
        mk = lambda p, days, goal, mins, eq, rework=0, ny=0: self._goal_item(p, days, goal, mins, eq, rework, ny)
        # Project a: 2 goal items (slow, costly) and 3 without; project b: 1 with and 1 without; c: no goal items.
        core.project_add(self.c, "c")
        mk("a", 1, g, 60, 900, rework=1, ny=1)
        mk("a", 2, g, 40, 700)
        for d in (1, 2, 3):
            mk("a", d, None, 10, 100)
        mk("b", 1, gb, 30, 500)
        mk("b", 2, None, 30, 300, ny=2)
        mk("c", 1, None, 5, 50)
        review = self.done_item("a", 1, kind="review", eq=10)  # not task work: left out
        self.c.commit()
        h = core.health(self.c, "7d", until=self.end, by="goal")
        bg = h["by_goal"]
        self.assertEqual([p["project"] for p in bg["projects"]], ["a", "b"])
        a = bg["projects"][0]
        self.assertEqual((a["goal"]["done"], a["none"]["done"]), (2, 3))
        self.assertEqual(a["goal"]["median_minutes"], 50)
        self.assertEqual(a["goal"]["rework_rate"], 0.5)
        self.assertEqual(a["goal"]["person_load"], 50)
        self.assertEqual(a["goal"]["median_eq"], 800)
        self.assertTrue(a["goal"]["small"])
        t = bg["total"]
        self.assertEqual((t["goal"]["done"], t["none"]["done"]), (3, 4))
        # Weights: a weighs 2 (its smaller group), b weighs 1.
        self.assertAlmostEqual(t["goal"]["median_minutes"], (2 * 50 + 1 * 30) / 3)
        self.assertAlmostEqual(t["none"]["median_eq"], (2 * 100 + 1 * 300) / 3)
        self.assertAlmostEqual(t["none"]["person_load"], (2 * 0 + 1 * 200) / 3)
        self.assertEqual(bg["left_out"], {"goal": 0, "none": 1})
        self.assertIsNotNone(review)  # the review item is in neither group: 3 + 4 + 1 left out are the task items
        with self.assertRaises(core.RiverError):
            core.health(self.c, "7d", until=self.end, by="kind")

    def _goal_item(self, project, days_ago, goal, minutes, eq, rework, ny):
        i = self.done_item(project, days_ago, eq=eq)
        closed = self.end - core.timedelta(days=days_ago)
        self.c.execute("UPDATE items SET claimed_at=? WHERE id=?",
                       (core.iso(closed - core.timedelta(minutes=minutes)), i))
        if goal:
            self.c.execute("INSERT INTO item_goals (item_id, goal_id) VALUES (?, ?)", (i, goal["id"]))
        for _ in range(rework):
            self.event(i, days_ago + 0.1, "reopened")
        for _ in range(ny):
            self.needs_you(i, days_ago + 0.1, days_ago)
        return i

    def test_an_empty_queue_and_the_cli(self):
        h = core.health(self.c, "30d")
        self.assertIsNone(h["now"]["rework_rate"])
        self.assertIsNone(h["now"]["person_minutes_per_item"])
        self.seed()
        from river import cli
        out = io.StringIO()
        old = os.environ.get("MAXPM_DB")
        os.environ["MAXPM_DB"] = self.path
        self.addCleanup(lambda: os.environ.__setitem__("MAXPM_DB", old) if old else os.environ.pop("MAXPM_DB", None))
        with contextlib.redirect_stdout(out):
            cli.run(["--as", "", "health", "--until", "2026-11-11T00:00", "--since", "7d"])
        text = out.getvalue()
        self.assertIn("HEADLINE  person minutes per done item: 1.50  (before 1.50)", text)
        self.assertIn("a proxy: 3 min per needs-you row", text)
        self.assertIn("rework rate          50.0%", text)
        self.assertIn("session idle         no samples", text)
        self.assertNotIn("CHANGES", text)
        core.register(self.c, "mark", human=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(["--as", "mark", "change", "add", "manager first", "--at", "2026-11-08T00:00Z"])
            cli.run(["--as", "mark", "change", "list"])
            cli.run(["--as", "", "health", "--until", "2026-11-11T00:00", "--since", "7d"])
        text = out.getvalue()
        self.assertIn("added c1: manager first at 2026-11-08T00:00", text)
        self.assertIn("c1    2026-11-08T00:00  manager first  [mark]", text)
        self.assertIn("CHANGES in this time, before -> after (weak proof", text)
        self.assertIn("2026-11-08 manager first: person min/item ", text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(["--as", "", "health", "--until", "2026-11-11T00:00", "--by", "goal"])
        self.assertIn("no project has done items of both kinds in this time", out.getvalue())


class LaunchWaitingModel(Base):
    """maxpm launch --item gives the item to a waiting session only when its model fits: the item's limits, and the
    model the person names with --model (#5383)."""

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "p", path=self.dir.name)
        self.fab = core.item_add(self.c, "p", "review the design", models={"model": "fable", "min_model": "fable"},
                                 actor="t")["id"]
        self.plain = core.item_add(self.c, "p", "plain work", actor="t")["id"]

    def waits(self, name, model=None):
        core.register(self.c, name)
        if model:
            core.set_agent_model(self.c, name, model)
        core.wait(self.c, self.dir.name, name, step="0s", sleep=lambda s: None)

    def test_an_opus_session_does_not_get_a_fable_item(self):
        self.waits("op", "claude-opus-5-5")  # the case of 2026-11-10: a full model id
        self.assertIsNone(core.waiting_agent_for(self.c, "p", self.fab))
        self.assertEqual(core.waiting_agent_for(self.c, "p", self.plain), "op")

    def test_a_session_with_no_model_gets_no_item_with_a_min_model(self):
        self.waits("anon")
        self.assertIsNone(core.waiting_agent_for(self.c, "p", self.fab))
        self.assertEqual(core.waiting_agent_for(self.c, "p", self.plain), "anon")
        self.waits("fb", "claude-fable-5-2")
        self.assertEqual(core.waiting_agent_for(self.c, "p", self.fab), "fb")

    def test_the_model_the_person_names_starts_a_new_session(self):
        self.waits("op", "claude-opus-5-5")
        self.waits("anon")
        self.assertIsNone(core.waiting_agent_for(self.c, "p", self.plain, model="fable"))
        self.assertEqual(core.waiting_agent_for(self.c, "p", self.plain, model="opus"), "op")
        self.assertEqual(core.waiting_agent_for(self.c, "p", self.plain, model="claude-opus-5-5"), "op")
        from river import cli
        out = io.StringIO()
        old = os.environ.get("MAXPM_DB")
        os.environ["MAXPM_DB"] = self.path
        self.addCleanup(lambda: os.environ.__setitem__("MAXPM_DB", old) if old else os.environ.pop("MAXPM_DB", None))
        with contextlib.redirect_stdout(out):
            cli.run(["--json", "--as", "", "launch", "--item", str(self.plain), "--model", "fable", "--dry-run"])
        self.assertIsNone(json.loads(out.getvalue())["would_push_to"])


class OpsSystem(Base):
    """The system description of a project (#5503): one per project, in the queue, never in a repository; a person or
    the manager writes it in force, an agent proposes and a person or the manager accepts."""

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "shop")
        core.register(self.c, "dev")
        core.register(self.c, "boss")
        self.c.execute("UPDATE agents SET role='manager' WHERE name='boss'")
        self.c.commit()
        core.register(self.c, "mark", human=True)

    def body(self):
        return core.ops_system_show(self.c, "shop")["shown"]["body"]

    def test_a_person_writes_it_in_force_and_an_agent_proposes(self):
        self.assertIsNone(core.ops_system_show(self.c, "shop")["shown"])
        r = core.ops_system_edit(self.c, "shop", {"code.test": "make test", "rules.release_window": "2h"}, actor="mark")
        self.assertEqual((r["state"], r["changed"]), ("accepted", ["code.test", "rules.release_window"]))
        self.assertEqual(self.body(), {"code": {"test": "make test"}, "rules": {"release_window": "2h"}})
        with self.assertRaisesRegex(RiverError, "no field 'code.nope'.*code.repo"):
            core.ops_system_edit(self.c, "shop", {"code.nope": "x"}, actor="mark")
        with self.assertRaisesRegex(RiverError, "like this already"):
            core.ops_system_edit(self.c, "shop", {"code.test": "make test"}, actor="mark")
        with self.assertRaisesRegex(RiverError, "say why"):
            core.ops_system_edit(self.c, "shop", {"infra.hosts": "web1"}, actor="dev")
        r = core.ops_system_edit(self.c, "shop", {"infra.hosts": "web1: nginx, app"}, why="fault #9 had no hosts",
                                 actor="dev")
        self.assertEqual(r["state"], "proposed")
        self.assertNotIn("infra", self.body())  # not in force yet
        self.assertEqual(r["proposed"]["body"]["infra"], {"hosts": "web1: nginx, app"})
        self.assertTrue([m for m in core.inbox(self.c, "boss") if "proposes a change of the system description of "
                         "shop (infra.hosts)" in m["body"]])
        with self.assertRaisesRegex(RiverError, "only a person or the manager accepts a proposed system description"):
            core.ops_system_accept(self.c, "shop", actor="dev")
        # A person changes another field after the proposal: accepting it keeps that change.
        core.ops_system_edit(self.c, "shop", {"code.test": "make check"}, actor="mark")
        r = core.ops_system_accept(self.c, "shop", actor="boss")
        self.assertEqual(self.body(), {"code": {"test": "make check"}, "infra": {"hosts": "web1: nginx, app"},
                                       "rules": {"release_window": "2h"}})
        self.assertEqual([v["state"] for v in r["versions"]], ["accepted", "accepted", "accepted"])
        self.assertTrue([m for m in core.inbox(self.c, "dev") if "boss accepted your change" in m["body"]])
        hist = [x[0] for x in self.c.execute("SELECT change FROM events WHERE change LIKE 'ops system shop%' ORDER BY id")]
        self.assertEqual(hist[0], "ops system shop changed: code.test, rules.release_window")
        self.assertIn("ops system shop proposed: infra.hosts; why: fault #9 had no hosts", hist)

    def test_a_rejected_proposal_changes_nothing_and_an_empty_text_removes_a_field(self):
        core.ops_system_edit(self.c, "shop", body={"code": {"test": "make test", "build": "make"}}, actor="mark")
        core.ops_system_edit(self.c, "shop", {"code.build": "make all"}, why="the build changed", actor="dev")
        with self.assertRaisesRegex(RiverError, "say why"):
            core.ops_system_reject(self.c, "shop", actor="mark")
        core.ops_system_reject(self.c, "shop", actor="mark", note="make is right")
        self.assertEqual(self.body(), {"code": {"build": "make", "test": "make test"}})
        self.assertIsNone(core.ops_system_show(self.c, "shop")["proposed"])
        core.ops_system_edit(self.c, "shop", {"code.build": ""}, actor="mark")
        self.assertEqual(self.body(), {"code": {"test": "make test"}})
        self.assertEqual(core.ops_system_show(self.c, "shop", version=1)["shown"]["body"]["code"]["build"], "make")

    def test_it_follows_a_rename_and_stays_out_of_the_ops_profile(self):
        core.ops_system_edit(self.c, "shop", {"infra.rollback": "deploy.sh --rollback"}, actor="mark")
        core.project_rename(self.c, "shop", "store", actor="mark")
        self.assertEqual(core.ops_system_show(self.c, "store")["shown"]["body"],
                         {"infra": {"rollback": "deploy.sh --rollback"}})
        self.assertNotIn("rollback", json.dumps(core.ops_profile_export(self.c, "store")))

    def test_the_cli_shows_and_edits_it(self):
        from river import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(["--as", "mark", "-q", "ops", "system", "edit", "shop", "--set", "code.test=make test"])
            cli.run(["--as", "mark", "-q", "ops", "system", "show", "shop"])
        text = out.getvalue()
        self.assertIn("system description of shop: changed code.test; the new version is in force", text)
        self.assertIn("  code.test: make test", text)
        self.assertIn("  infra.hosts: (none: the hosts and the services on each", text)


class Ops(Base):
    """Operations checks of a project or a deploy target (#5429): the record, the rights, and the commands."""

    def setUp(self):
        super().setUp()
        core.target_add(self.c, "prod", "the servers")
        core.project_add(self.c, "site", target="prod")
        core.register(self.c, "dev")
        core.register(self.c, "boss")
        self.c.execute("UPDATE agents SET role='manager' WHERE name='boss'")
        self.c.commit()
        core.register(self.c, "mark", human=True)

    def history(self):
        return [r[0] for r in self.c.execute("SELECT change FROM events WHERE item_id IS NULL AND change LIKE 'ops %' "
                                             "ORDER BY id")]

    def test_project_show_has_a_monitoring_line_with_the_checks_of_the_project_and_its_target(self):
        from river import cli

        def show():
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.run(["-q", "project", "show", "site"])
            return [x for x in out.getvalue().splitlines() if "monitoring:" in x]
        self.assertEqual(show(), ["  monitoring: no checks (maxpm ops setup site)"])
        core.ops_add(self.c, "site", "home", "url", {"url": "https://example.com/"}, actor="dev")
        core.ops_add(self.c, "prod", "nightly", "beat", {"period": "1d"}, actor="dev")
        self.c.execute("UPDATE checks SET state='fault' WHERE name='nightly'")
        self.c.commit()
        it = core.item_add(self.c, "site", "ops:prod:nightly no beat in 1d", kind="operational", doer="ai",
                           actor="maxpm")["id"]
        core.ops_propose(self.c, "site", "disk", "cmd", {"command": "df -h"}, why="the disk fills", actor="dev")
        self.assertEqual(show(), [f"  monitoring: 2 checks (1 fault, 1 new); open faults: #{it}; 1 proposed "
                                  f"(maxpm ops list site)"])

    def test_any_session_adds_a_url_check_with_the_defaults_and_the_history_records_it(self):
        r = core.ops_add(self.c, "site", "home", "url", {"url": "https://example.com/"}, actor="dev")
        c = r["check"]
        self.assertEqual((c["scope_kind"], c["scope"], c["key"], c["state"]), ("project", "site", "ops:site:home", "new"))
        self.assertEqual(c["config"], {"url": "https://example.com/", "statuses": "200-399"})
        self.assertEqual((c["every"], c["fail_after"], c["recover_after"], c["timeout"], c["severity"]),
                         ("5m", 3, 2, "30s", "item"))
        self.assertEqual(self.history(), ["ops check site/home added: url GET https://example.com/ (200-399); every "
                                          "5m, fault after 3, item"])
        core.ops_add(self.c, "prod", "beat", "beat", {"period": "1h", "severity": "page"}, actor="dev")
        self.assertEqual([(x["scope"], x["name"]) for x in core.ops_list(self.c)["checks"]],
                         [("site", "home"), ("prod", "beat")])
        self.assertEqual([x["name"] for x in core.ops_list(self.c, "prod")["checks"]], ["beat"])
        self.assertEqual(core.ops_show(self.c, "prod", "beat")["key"], "ops:prod:beat")

    def test_a_command_or_a_repair_needs_a_person_or_the_manager_and_a_repair_on_a_target_a_person(self):
        for form, f in (("cmd", {"command": "true"}), ("plugin", {"command": "/usr/lib/check_disk -w 20%"}),
                        ("log", {"command": "tail -n 100 app.log", "pattern": "ERROR"})):
            with self.assertRaisesRegex(RiverError, f"only a person or the manager .* a {form} check"):
                core.ops_add(self.c, "site", form, form, f, actor="dev")
            core.ops_add(self.c, "site", form, form, f, actor="boss")
        with self.assertRaisesRegex(RiverError, "a repair command"):
            core.ops_add(self.c, "site", "home", "url", {"url": "https://example.com/", "repair": "restart"}, actor="dev")
        core.ops_add(self.c, "site", "home", "url", {"url": "https://example.com/", "repair": "restart"}, actor="boss")
        with self.assertRaisesRegex(RiverError, "only a person .* repair on the deploy target prod"):
            core.ops_add(self.c, "prod", "api", "url", {"url": "https://example.com/", "repair": "restart"}, actor="boss")
        core.ops_add(self.c, "prod", "api", "url", {"url": "https://example.com/", "repair": "restart"}, actor="mark")
        core.ops_add(self.c, "prod", "disk", "cmd", {"command": "df -h"})  # no name: a person at the terminal

    def test_an_agent_cannot_turn_a_url_check_into_a_command_nor_change_pause_or_remove_a_command_check(self):
        core.ops_add(self.c, "site", "home", "url", {"url": "https://example.com/"}, actor="dev")
        with self.assertRaisesRegex(RiverError, "a cmd check"):
            core.ops_edit(self.c, "site", "home", {"command": "true"}, form="cmd", actor="dev")
        r = core.ops_edit(self.c, "site", "home", {"every": "1m", "body": "Welcome"}, actor="dev")
        self.assertEqual((r["fields"], r["check"]["config"]["body"]), (["config", "every"], "Welcome"))
        self.assertIsNone(core.ops_edit(self.c, "site", "home", {"every": "1m"}, actor="dev")["changed"])
        core.ops_add(self.c, "site", "jobs", "cmd", {"command": "true"}, actor="boss")
        for act in (lambda: core.ops_edit(self.c, "site", "jobs", {"every": "1h"}, actor="dev"),
                    lambda: core.ops_pause(self.c, "site", "jobs", "2h", actor="dev"),
                    lambda: core.ops_rm(self.c, "site", "jobs", actor="dev")):
            with self.assertRaisesRegex(RiverError, "only a person or the manager"):
                act()
        r = core.ops_edit(self.c, "site", "jobs", {"command": "check_jobs --all"}, form="plugin", actor="boss")
        self.assertEqual((r["check"]["form"], r["check"]["config"]), ("plugin", {"command": "check_jobs --all"}))

    def test_each_setting_is_checked_before_the_check_is_kept(self):
        bad = [("url", {"url": "example.com"}, "http:// or https://"),
               ("url", {"url": "https://x.io", "statuses": "2xx"}, "bad statuses"),
               ("url", {"url": "https://x.io", "command": "true"}, "a url check has no --command"),
               ("url", {"url": "https://x.io", "subject": True}, "has no subjects"),
               ("url", {"url": "https://x.io", "every": "1m", "timeout": "2m"}, "longer than every"),
               ("url", {"url": "https://x.io", "every": "soon"}, "bad duration"),
               ("url", {"url": "https://x.io", "fail_after": 0}, "at least 1"),
               ("url", {"url": "https://x.io", "severity": "loud"}, "no severity"),
               ("log", {"command": "cat log"}, "needs --pattern"),
               ("log", {"command": "cat log", "pattern": "("}, "bad --pattern"),
               ("log", {"command": "cat log", "pattern": "E", "limit": 0}, "at least 1"),
               ("beat", {}, "needs --period"),
               ("cmd", {"command": "  "}, "needs --command"),
               ("ping", {}, "no form")]
        for form, f, why in bad:
            with self.assertRaisesRegex(RiverError, why):
                core.ops_add(self.c, "site", "x", form, f, actor="mark")
        self.assertEqual(core.ops_list(self.c)["checks"], [])
        with self.assertRaisesRegex(RiverError, "lower-case"):
            core.ops_add(self.c, "site", "Home", "beat", {"period": "1h"})
        with self.assertRaisesRegex(RiverError, "no project or deploy target 'nope'"):
            core.ops_add(self.c, "nope", "home", "beat", {"period": "1h"})
        core.ops_add(self.c, "site", "home", "beat", {"period": "1h"})
        with self.assertRaisesRegex(RiverError, "has a check 'home'"):
            core.ops_add(self.c, "site", "home", "beat", {"period": "1h"})
        c = core.ops_add(self.c, "site", "errors", "log", {"command": "cat new.log", "pattern": "ERROR", "subject": True})
        self.assertEqual((c["check"]["config"]["limit"], c["check"]["config"]["window"], c["check"]["subject"]),
                         (1, "15m", True))

    def test_a_name_that_is_a_project_and_a_target_needs_its_kind(self):
        core.project_add(self.c, "prod")
        with self.assertRaisesRegex(RiverError, "project:prod or target:prod"):
            core.ops_add(self.c, "prod", "beat", "beat", {"period": "1h"})
        core.ops_add(self.c, "target:prod", "beat", "beat", {"period": "1h"})
        core.ops_add(self.c, "project:prod", "beat", "beat", {"period": "2h"})
        self.assertEqual(sorted((x["scope_kind"], x["config"]["period"]) for x in core.ops_list(self.c)["checks"]),
                         [("project", "2h"), ("target", "1h")])

    def test_a_pause_and_a_resume_are_changes_of_state_and_a_removed_check_keeps_its_history(self):
        core.ops_add(self.c, "site", "home", "url", {"url": "https://example.com/"}, actor="dev")
        with self.assertRaisesRegex(RiverError, "--until <time>"):
            core.ops_pause(self.c, "site", "home", actor="dev")
        c = core.ops_pause(self.c, "site", "home", "2h", actor="dev")["check"]
        self.assertEqual(c["state"], "paused")
        self.assertTrue(c["paused_until"] > core.iso(core.now() + timedelta(hours=1)))
        self.assertEqual(core.ops_pause(self.c, "site", "home", resume=True, actor="dev")["check"]["state"], "new")
        with self.assertRaisesRegex(RiverError, "is not paused"):
            core.ops_pause(self.c, "site", "home", resume=True, actor="dev")
        core.ops_rm(self.c, "site", "home", actor="dev")
        self.assertEqual([(r["key"], r["state"]) for r in self.c.execute("SELECT key, state FROM ops_events ORDER BY id")],
                         [("ops:site:home", "paused"), ("ops:site:home", "resumed")])
        self.assertTrue(self.history()[-1].startswith("ops check site/home removed (was: url GET"))
        with self.assertRaisesRegex(RiverError, "site has no check 'home'"):
            core.ops_show(self.c, "site", "home")

    def test_the_command_line_adds_shows_and_refuses_an_unnamed_agent(self):
        from river import cli

        def run(*words, env=None):
            out = io.StringIO()
            with mock.patch.dict(os.environ, env or {}), contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(io.StringIO()):
                try:
                    code = cli.run(list(words))
                except RiverError as e:
                    return 2, str(e)
            return code, out.getvalue()
        code, out = run("--as", "mark", "ops", "add", "site", "errors", "log", "--command", "tail -n 50 app.log",
                        "--pattern", "ERROR", "--limit", "5", "--window", "10m", "--subject", "on",
                        "--runbook", "read app.log")
        self.assertEqual(code, 0)
        self.assertIn("check site/errors added", out)
        self.assertIn("runs a command outside the sandbox", out)
        code, out = run("--as", "mark", "ops", "list")
        self.assertIn("site/errors  [new; never ran]", out)
        code, out = run("--as", "mark", "ops", "show", "site", "errors")
        self.assertIn("pattern: ERROR", out)
        self.assertIn("a fault for each subject", out)
        code, out = run("ops", "rm", "site", "errors", env={"CLAUDECODE": "1"})
        self.assertEqual(code, 2)
        self.assertIn("name yourself", out)

    # ---- fault items (#5431)

    def ops_items(self):
        return self.c.execute("SELECT id, title, status, priority, doer, kind, context FROM items WHERE kind='operational' "
                              "ORDER BY id").fetchall()

    def notes(self, item_id):
        return [r[0] for r in self.c.execute("SELECT change FROM events WHERE item_id=? AND change LIKE 'ops %' "
                                             "ORDER BY id", (item_id,))]

    def test_a_fault_opens_one_item_notes_it_recovers_and_comes_back_within_a_day(self):
        core.ops_add(self.c, "site", "job", "plugin", {"command": "check_job", "fail_after": 2, "recover_after": 1,
                                                       "runbook": "read the job log"}, actor="boss")
        t = core.now()
        at = lambda m: t + timedelta(minutes=m)
        bad, good = self.proc(2, "JOB CRITICAL - no run for 2h|age=7200s\nlast run failed\n"), self.proc(0, "JOB OK")
        self.run_at("site", "job", at(0), bad)
        self.assertEqual(self.ops_items(), [])  # failing, not a fault yet
        r = self.run_at("site", "job", at(5), bad)
        (it,) = self.ops_items()
        self.assertEqual(r["items"], [{"key": "ops:site:job", "item": it["id"], "action": "opened", "kind": "unknown"}])
        self.assertTrue(it["title"].startswith("ops:site:job JOB CRITICAL - no run for 2h"))
        self.assertEqual((it["priority"], it["doer"], it["status"]), (1, "ai", "open"))
        for want in ("Key: ops:site:job", "last run failed", "Runbook: read the job log", "maxpm ops history site job"):
            self.assertIn(want, it["context"])
        alerts = [m["body"] for m in core.inbox(self.c, "boss")]
        self.assertTrue(any(f"new fault: ops:site:job" in b and f"#{it['id']}" in b for b in alerts), alerts)
        # Later runs: one note at most every ops_note_every (30m), no second item.
        for m in (10, 15, 40):
            self.run_at("site", "job", at(m), bad)
        self.assertEqual(len(self.ops_items()), 1)
        self.assertEqual(len([n for n in self.notes(it["id"]) if "still failing" in n]), 1)  # at 40m, not at 10 or 15
        # Recovery: a note, and the item stays open for the investigator.
        r = self.run_at("site", "job", at(45), good)
        self.assertEqual(r["items"][0]["action"], "recovered")
        self.assertIn("ops recovered at", self.notes(it["id"])[-1])
        self.assertEqual(self.ops_items()[0]["status"], "open")
        # Closed with the cause, then a fault within 24h: the same item again.
        core.done(self.c, it["id"], actor="boss", output="the job's disk was full")
        self.run_at("site", "job", at(60), bad); r = self.run_at("site", "job", at(65), bad)
        self.assertEqual(r["items"][0], {"key": "ops:site:job", "item": it["id"], "action": "reopened", "kind": "unknown"})
        self.assertEqual([x["status"] for x in self.ops_items()], ["open"])
        # After ops_reopen_within, a new item.
        core.done(self.c, it["id"], actor="boss", output="again the disk")
        self.run_at("site", "job", at(70), good)
        later = core.now() + timedelta(hours=25)
        with mock.patch.object(core, "now", lambda: later):
            self.run_at("site", "job", later, bad); r = self.run_at("site", "job", later + timedelta(minutes=5), bad)
        self.assertEqual(r["items"][0]["action"], "opened")
        self.assertEqual(len(self.ops_items()), 2)

    def test_the_briefing_of_a_fault_item_shows_the_check_and_its_commands(self):
        core.ops_add(self.c, "site", "job", "plugin", {"command": "check_job", "fail_after": 1,
                                                       "runbook": "read the job log"}, actor="boss")
        r = self.run_at("site", "job", core.now(), self.proc(2, "JOB CRITICAL - no run for 2h"))
        it = r["items"][0]["item"]
        core.register(self.c, "inv")
        b = core.go(self.c, self.dir.name, "inv", project="site", focus=f"item:{it}")
        self.assertEqual(b["item"]["id"], it)
        f = b["fault"]
        self.assertEqual((f["key"], f["storm"], [c["state"] for c in f["checks"]]), ("ops:site:job", False, ["fault"]))
        self.assertIn("g7", [p["pid"] for p in b["policies"]])
        from river import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.render_go(b)
        text = out.getvalue()
        for want in ("FAULT ops:site:job", "check site/job (plugin), now fault", "critical: JOB CRITICAL - no run for 2h",
                     "runbook: read the job log", "ops run site job", "ops history site job"):
            self.assertIn(want, text)
        self.assertIsNone(core.fault_key({"kind": "work", "title": "ops:site:job looks like a key"}))
        self.assertEqual(core.fault_key({"kind": "operational", "title": "ops:site: 4 checks failing"}), "ops:site:")

    def test_a_repair_needs_the_fault_item_and_twice_in_six_hours_then_a_person(self):
        # #5433, decision 4 B of #5380.
        core.ops_add(self.c, "site", "job", "plugin", {"command": "check_job", "fail_after": 1,
                                                       "repair": "systemctl restart job"}, actor="mark")
        c = core.ops_find(self.c, "site", "job")
        with self.assertRaisesRegex(RiverError, "no open fault item"):
            core.ops_repair_start(self.c, c, "it hangs", "dev")
        it = self.run_at("site", "job", core.now(), self.proc(2, "JOB CRITICAL - hangs"))["items"][0]["item"]
        core.register(self.c, "inv")
        core.claim(self.c, it, "inv")
        with self.assertRaisesRegex(RiverError, f"only the session that holds a fault item.*#{it} \\(held by inv\\)"):
            core.ops_repair_start(self.c, c, "it hangs", "dev")
        with self.assertRaisesRegex(RiverError, "say why"):
            core.ops_repair_start(self.c, c, " ", "inv")
        for _ in range(2):
            self.assertEqual(core.ops_repair_start(self.c, c, "the job hangs on its lock", "inv")["id"], it)
        with self.assertRaisesRegex(RiverError, "ran 2 times in 6h .ops_repair_max 2.: the next one needs a person"):
            core.ops_repair_start(self.c, c, "again", "inv")
        self.assertEqual(core.ops_repair_start(self.c, c, "I looked: restart it", "mark")["id"], it)  # a person may
        self.assertEqual(len(self.notes(it)), 3 + len([n for n in self.notes(it) if not n.startswith("ops repair")]))
        # The run: its result on the item; a timeout says so.
        with mock.patch.object(core, "OPS_RUNNER", self.proc(0, "restarted job")):
            r = core.ops_repair_run(self.c, c, core._item(self.c, it), "inv")
        self.assertEqual((r["ok"], r["exit_code"]), (True, 0))
        self.assertIn("ops repair ended: exit 0; the check runs once now\nrestarted job", self.notes(it)[-1])
        with mock.patch.object(core, "OPS_RUNNER", self.proc(None, "", timed_out=True)):
            r = core.ops_repair_run(self.c, c, core._item(self.c, it), "inv")
        self.assertFalse(r["ok"])
        self.assertIn("stopped at the check's timeout", self.notes(it)[-1])
        no = core.ops_add(self.c, "site", "home", "url", {"url": "https://example.com/"}, actor="dev")["check"]
        with self.assertRaisesRegex(RiverError, "has no repair command"):
            core.ops_repair_start(self.c, no, "x", "mark")

    def test_serve_runs_the_repair_then_the_check_once(self):
        from river import server
        core.ops_add(self.c, "site", "job", "plugin", {"command": "check_job", "fail_after": 1,
                                                       "repair": "systemctl restart job"}, actor="mark")
        it = self.run_at("site", "job", core.now(), self.proc(2, "JOB CRITICAL - hangs"))["items"][0]["item"]
        ran = []

        class Now:  # the thread runs at once
            def __init__(self, target, daemon=None):
                self.target = target

            def start(self):
                self.target()
        with mock.patch.dict(os.environ, {"MAXPM_DB": self.path}), mock.patch.object(server.threading, "Thread", Now), \
                mock.patch.object(server, "ops_start_now", lambda conn, c, who=None: ran.append(c["name"])), \
                mock.patch.object(core, "OPS_RUNNER", self.proc(0, "restarted")):
            r = server.OPS["ops_repair"](self.c, {"scope": "site", "name": "job", "reason": "hangs"}, "mark")
        self.assertEqual((r["item"], r["command"], ran), (it, "systemctl restart job", ["job"]))
        self.assertIn("ops repair ended: exit 0", self.notes(it)[-1])

    def test_subjects_have_items_of_their_own_and_a_note_check_none(self):
        core.ops_add(self.c, "site", "jobs", "plugin", {"command": "check_jobs", "fail_after": 1, "subject": True},
                     actor="boss")
        core.ops_add(self.c, "site", "soft", "plugin", {"command": "check_soft", "fail_after": 1, "severity": "note"},
                     actor="boss")
        t = core.now()
        r = self.run_at("site", "jobs", t, self.proc(2, "JOBS CRITICAL - 2 failed\nbackup: failed at 03:00\n"
                                                         "mail_sync: failed twice\n"))
        self.assertEqual(sorted(x["key"] for x in r["items"]), ["ops:site:jobs:backup", "ops:site:jobs:mail_sync"])
        self.assertEqual(len(self.ops_items()), 2)  # none for ops:site:jobs itself
        r = self.run_at("site", "soft", t, self.proc(2, "SOFT CRITICAL - slow"))
        self.assertEqual(r["items"], [{"key": "ops:site:soft", "item": None, "action": "manager-note", "kind": "unknown"}])
        self.assertEqual(len(self.ops_items()), 2)

    def test_a_storm_of_one_scope_shares_one_item(self):
        t = core.now()
        for i in range(6):
            core.ops_add(self.c, "site", f"c{i}", "plugin", {"command": "x", "fail_after": 1}, actor="boss")
        acts = [self.run_at("site", f"c{i}", t + timedelta(seconds=10 * i), self.proc(2, f"C{i} CRITICAL - down"))
                ["items"][0]["action"] for i in range(6)]
        self.assertEqual(acts, ["opened", "opened", "opened", "storm", "storm-note", "storm-note"])
        titles = [x["title"] for x in self.ops_items()]
        self.assertEqual(len(titles), 4)
        self.assertEqual(titles[3], "ops:site: 4 checks failing")
        # Another scope is not part of the storm.
        core.project_add(self.c, "api")
        core.ops_add(self.c, "api", "home", "plugin", {"command": "x", "fail_after": 1}, actor="boss")
        r = self.run_at("api", "home", t + timedelta(seconds=70), self.proc(2, "HOME CRITICAL - down"))
        self.assertEqual(r["items"][0]["action"], "opened")

    def test_the_record_keeps_every_detection_also_warnings_noise_and_ones_with_no_item(self):
        """#5510: each check or subject that a run found not ok has one row in the record, with its kind, first and last
        seen, the count, the last evidence and the item it opened; a warning and noise open no item but are there."""
        from river import cli
        t = core.now() - timedelta(hours=1)
        core.config_set(self.c, "ops_confirm", "off")
        core.ops_add(self.c, "site", "jobs", "plugin", {"command": "x", "subject": True, "fail_after": 1}, actor="boss")
        core.ops_add(self.c, "site", "disk", "plugin", {"command": "x"}, actor="boss")
        self.run_at("site", "disk", t, self.proc(1, "DISK WARNING - 85% used"))
        core.ops_noise_add(self.c, "site", "disk", "the disk fills each night and the job empties it",
                           pattern="DISK WARNING", actor="boss")
        self.run_at("site", "disk", t + timedelta(minutes=5), self.proc(1, "DISK WARNING - 86% used\n{\"used\": 86}"))
        self.run_at("site", "jobs", t, self.proc(2, "JOBS CRITICAL - one failed\nbackup: CRITICAL exit 1\nmail: OK done"))
        self.run_at("site", "disk", t + timedelta(minutes=10), self.proc(0, "DISK OK - 40% used"))
        rec = core.ops_detections(self.c, "site")
        by = {r["key"]: r for r in rec["rows"]}
        self.assertEqual(sorted(by), ["ops:site:disk", "ops:site:jobs:backup"])  # the check itself: a subject is not ok
        disk, backup = by["ops:site:disk"], by["ops:site:jobs:backup"]
        self.assertEqual((disk["status"], disk["count"], disk["kind"], disk["item_id"], disk["summary"], disk["evidence"]),
                         ("warning", 2, "noise", None, "DISK WARNING - 86% used", '{"used": 86}'))
        self.assertEqual((disk["first_seen"], disk["last_seen"]), (core.iso(t), core.iso(t + timedelta(minutes=5))))
        item = core._ops_item(self.c, "ops:site:jobs:backup")
        self.assertEqual((backup["status"], backup["count"], backup["subject"], backup["item_id"], backup["summary"]),
                         ("critical", 1, "backup", item["id"], "exit 1"))
        self.assertEqual(backup["kind"], "unknown")
        self.assertEqual(rec["kinds"], {"noise": 1, "unknown": 1})
        self.assertEqual([r["key"] for r in core.ops_detections(self.c, "site", kind="noise")["rows"]], ["ops:site:disk"])
        self.assertEqual([r["key"] for r in core.ops_detections(self.c, check="jobs")["rows"]], ["ops:site:jobs:backup"])
        self.assertEqual(core.ops_detections(self.c, since="30m")["rows"], [])
        with self.assertRaisesRegex(RiverError, "no kind 'loud'"):
            core.ops_detections(self.c, kind="loud")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(["-q", "ops", "record", "--project", "site"])
        lines = out.getvalue().splitlines()
        self.assertIn("noise  site/disk  warning, 2x since", lines[0])
        self.assertTrue(lines[0].endswith("  no item"))
        self.assertEqual(lines[-1], "kinds: noise 1, unknown 1")

    # ---- running the checks (#5430)

    def run_at(self, scope, name, at, runner=None, http=None):
        """Start and finish one run of a check at a time, with a fake command runner or HTTP answer."""
        c = core.ops_find(self.c, scope, name)
        with mock.patch.object(core, "OPS_RUNNER", runner), mock.patch.object(core, "OPS_HTTP", http):
            run = core.ops_start(self.c, c, at=at)
            return core.ops_finish(self.c, run, at=at)

    @staticmethod
    def proc(code, out="", timed_out=False):
        def runner(cmd, folder, limit, env):
            r = subprocess.CompletedProcess(cmd, code, out, "")
            r.timed_out = timed_out
            return r
        return runner

    def state(self, scope, name):
        return core.ops_show(self.c, scope, name)["state"]

    def events(self, scope, name):
        return [(e["key"], e["state"]) for e in reversed(core.ops_history(self.c, scope, name)["events"])]

    def test_the_state_follows_fail_after_and_recover_after_and_a_warning_counts_toward_neither(self):
        core.ops_add(self.c, "site", "job", "plugin", {"command": "check_job", "fail_after": 2, "recover_after": 2},
                     actor="boss")
        t = core.now()
        seq = [(0, "healthy"), (1, "healthy"), (2, "failing"), (1, "failing"), (3, "fault"), (0, "fault"),
               (1, "fault"), (0, "recovered"), (0, "healthy"), (2, "failing"), (0, "healthy")]
        for i, (code, want) in enumerate(seq):
            r = self.run_at("site", "job", t + timedelta(minutes=5 * i), self.proc(code, f"JOB {code}|age=3s\n"))
            self.assertEqual(self.state("site", "job"), want, (i, code))
        self.assertEqual([s for _, s in self.events("site", "job")],
                         ["healthy", "failing", "fault", "recovered", "healthy", "failing", "healthy"])
        last = core.ops_history(self.c, "site", "job", 1)["runs"][0]
        self.assertEqual((last["status"], last["summary"], last["perfdata"], last["exit_code"]),
                         ("ok", "JOB 0", "age=3s", 0))
        self.assertEqual(r["status"], "ok")

    def test_a_plugin_keeps_its_evidence_and_an_exit_outside_the_api_or_a_timeout_is_unknown(self):
        core.ops_add(self.c, "site", "disk", "plugin", {"command": "check_disk", "fail_after": 1}, actor="boss")
        t = core.now()
        self.run_at("site", "disk", t, self.proc(2, 'DISK CRITICAL - 3% free|free=3%\n{"mount": "/", "free": 3}\n'))
        run = core.ops_history(self.c, "site", "disk", 1)["runs"][0]
        self.assertEqual((run["status"], json.loads(run["evidence"])), ("critical", {"mount": "/", "free": 3}))
        self.assertEqual(self.state("site", "disk"), "fault")
        self.run_at("site", "disk", t + timedelta(minutes=5), self.proc(7, "odd\n"))
        self.assertEqual(core.ops_history(self.c, "site", "disk", 1)["runs"][0]["status"], "unknown")
        self.run_at("site", "disk", t + timedelta(minutes=10), self.proc(None, "", timed_out=True))
        run = core.ops_history(self.c, "site", "disk", 1)["runs"][0]
        self.assertEqual((run["status"], run["timed_out"]), ("unknown", 1))
        self.assertIn("timeout 30s", run["summary"])

    def test_a_real_command_past_its_timeout_stops_and_counts_as_unknown(self):
        core.ops_add(self.c, "site", "slow", "cmd", {"command": f"{sys.executable} -c 'import time; time.sleep(30)'",
                                                    "timeout": "1s", "every": "1m", "fail_after": 1}, actor="boss")
        t0 = time.monotonic()
        self.run_at("site", "slow", core.now())
        self.assertLess(time.monotonic() - t0, 20)
        run = core.ops_history(self.c, "site", "slow", 1)["runs"][0]
        self.assertEqual((run["status"], run["timed_out"]), ("unknown", 1))
        self.assertEqual(self.state("site", "slow"), "fault")

    def test_a_url_check_reads_the_status_and_the_body(self):
        core.ops_add(self.c, "site", "home", "url", {"url": "https://example.com/", "body": "Welcome", "fail_after": 1},
                     actor="dev")
        t = core.now()
        self.run_at("site", "home", t, http=lambda url, limit: (200, "<h1>Welcome</h1>"))
        self.assertEqual(self.state("site", "home"), "healthy")
        self.run_at("site", "home", t + timedelta(minutes=5), http=lambda url, limit: (200, "maintenance"))
        run = core.ops_history(self.c, "site", "home", 1)["runs"][0]
        self.assertEqual((run["status"], run["summary"]), ("critical", "HTTP 200, but the answer lacks 'Welcome'"))

        def down(url, limit):
            raise OSError("connection refused")
        self.run_at("site", "home", t + timedelta(minutes=10), http=down)
        self.assertEqual(core.ops_history(self.c, "site", "home", 1)["runs"][0]["summary"], "no answer: connection refused")
        self.run_at("site", "home", t + timedelta(minutes=15), http=lambda url, limit: (503, ""))
        self.assertEqual(core.ops_history(self.c, "site", "home", 1)["runs"][0]["summary"], "HTTP 503, not in 200-399")
        self.assertEqual(self.state("site", "home"), "fault")

    def test_a_beat_check_fails_when_no_beat_came_within_its_period(self):
        core.ops_add(self.c, "prod", "nightly", "beat", {"period": "1h", "every": "5m", "fail_after": 1}, actor="dev")
        t = core.now()
        self.run_at("prod", "nightly", t)
        self.assertEqual(self.state("prod", "nightly"), "healthy")
        self.run_at("prod", "nightly", t + timedelta(hours=2))
        self.assertEqual(self.state("prod", "nightly"), "fault")
        self.assertIn("no beat since the check was added", core.ops_history(self.c, "prod", "nightly", 1)["runs"][0]["summary"])
        with mock.patch.object(core, "now", lambda: t + timedelta(hours=2, minutes=1)):
            self.assertEqual(core.ops_beat(self.c, "nightly")["check"], "prod/nightly")
        self.run_at("prod", "nightly", t + timedelta(hours=2, minutes=5))
        self.run_at("prod", "nightly", t + timedelta(hours=2, minutes=10))
        self.assertEqual(self.state("prod", "nightly"), "recovered")
        core.ops_add(self.c, "site", "home", "url", {"url": "https://example.com/"}, actor="dev")
        with self.assertRaisesRegex(RiverError, "only a beat check takes a beat"):
            core.ops_beat(self.c, "site", "home")

    def test_a_log_check_counts_the_matching_lines_of_its_window(self):
        core.ops_add(self.c, "site", "errors", "log", {"command": "tail_new", "pattern": "ERROR", "limit": 3,
                                                      "window": "15m", "every": "5m", "fail_after": 1}, actor="boss")
        t = core.now()
        self.run_at("site", "errors", t, self.proc(0, "INFO a\nERROR b\nERROR c\n"))
        self.assertEqual(self.state("site", "errors"), "healthy")
        self.run_at("site", "errors", t + timedelta(minutes=5), self.proc(0, "ERROR d\n"))
        run = core.ops_history(self.c, "site", "errors", 1)["runs"][0]
        self.assertEqual((run["status"], run["summary"]), ("critical", "3 lines match in 15m (limit 3)"))
        self.assertEqual(json.loads(run["evidence"])["lines"], ["ERROR d"])
        # 20 minutes later the first two lines left the window.
        self.run_at("site", "errors", t + timedelta(minutes=20), self.proc(0, "ERROR e\n"))
        self.assertEqual(core.ops_history(self.c, "site", "errors", 1)["runs"][0]["summary"],
                         "2 lines match in 15m (limit 3)")
        self.run_at("site", "errors", t + timedelta(minutes=25), self.proc(1, ""))
        self.assertIn("could not run", core.ops_history(self.c, "site", "errors", 1)["runs"][0]["summary"])

    def seq(self, *results):
        """A fake runner that answers each run with the next (code, output)."""
        left = list(results)

        def runner(cmd, folder, limit, env):
            code, out = left.pop(0)
            r = subprocess.CompletedProcess(cmd, code, out, "")
            r.timed_out = False
            return r
        return runner

    def test_before_a_new_fault_opens_an_item_the_check_runs_once_again_and_a_pass_opens_nothing(self):
        core.ops_add(self.c, "site", "api", "cmd", {"command": "check_api", "fail_after": 1}, actor="boss")
        t = core.now()
        r = self.run_at("site", "api", t, self.seq((2, "down"), (0, "up")))  # a blip: the run again passes
        self.assertEqual((self.state("site", "api"), r["items"], r["confirms"]), ("healthy", [], r["run"] - 1))
        runs = core.ops_history(self.c, "site", "api")["runs"]
        self.assertEqual([(x["status"], x["confirms"]) for x in runs], [("ok", runs[1]["id"]), ("critical", None)])
        self.assertFalse([e for e in self.events("site", "api") if e[1] == "fault"])
        r = self.run_at("site", "api", t + timedelta(minutes=5), self.seq((2, "down"), (2, "still down")))
        self.assertEqual((self.state("site", "api"), r["items"][0]["action"]), ("fault", "opened"))
        self.assertEqual(r["changes"], [{"key": "ops:site:api", "from": "healthy", "to": "fault"}])
        core.config_set(self.c, "ops_confirm", "off")  # off: the run that makes the fault opens the item
        core.ops_add(self.c, "site", "db", "cmd", {"command": "check_db", "fail_after": 1}, actor="boss")
        r = self.run_at("site", "db", t, self.seq((2, "down")))
        self.assertEqual((self.state("site", "db"), "confirms" in r), ("fault", False))

    def test_a_check_of_kind_never_is_a_fault_at_its_first_failed_run(self):
        core.ops_add(self.c, "site", "leak", "cmd", {"command": "find_secrets", "errors": "never"}, actor="boss")
        c = core.ops_show(self.c, "site", "leak")
        self.assertEqual((c["fail_after"], c["errors"]), (3, "never"))
        self.assertIn("fault at the first failed run", self.history()[-1])
        self.run_at("site", "leak", core.now(), self.proc(2, "a key in the log"))
        self.assertEqual(self.state("site", "leak"), "fault")
        self.assertEqual(core.ops_profile_export(self.c, "site")["checks"][0]["errors"], "never")

    def test_a_log_check_of_kind_outside_counts_the_different_sources_not_the_lines(self):
        with self.assertRaisesRegex(RiverError, "needs a group that names the source"):
            core.ops_add(self.c, "site", "bad", "log", {"command": "tail_new", "pattern": "ERROR", "errors": "outside"},
                         actor="boss")
        with self.assertRaisesRegex(RiverError, "it needs a log check"):
            core.ops_add(self.c, "site", "bad", "cmd", {"command": "x", "errors": "outside"}, actor="boss")
        core.ops_add(self.c, "site", "4xx", "log", {"command": "tail_new", "pattern": r"400 from (\S+)", "limit": 3,
                                                   "fail_after": 1, "errors": "outside"}, actor="boss")
        t = core.now()
        self.run_at("site", "4xx", t, self.proc(0, "400 from bot\n" * 50 + "400 from a\n"))
        run = core.ops_history(self.c, "site", "4xx", 1)["runs"][0]
        self.assertEqual((run["status"], run["summary"]), ("ok", "2 different sources in 15m (limit 3; 51 lines)"))
        self.run_at("site", "4xx", t + timedelta(minutes=5), self.proc(0, "400 from b\n"))
        run = core.ops_history(self.c, "site", "4xx", 1)["runs"][0]
        self.assertEqual((run["status"], run["summary"]), ("critical", "3 different sources in 15m (limit 3; 52 lines)"))
        self.assertEqual(self.state("site", "4xx"), "fault")

    def test_a_log_check_of_kind_ours_fails_only_over_its_normal_rate(self):
        with self.assertRaisesRegex(RiverError, "needs --normal"):
            core.ops_add(self.c, "site", "bad", "log", {"command": "tail_new", "pattern": "ERROR", "errors": "ours"},
                         actor="boss")
        core.ops_add(self.c, "site", "500", "log", {"command": "tail_new", "pattern": "ERROR", "normal": 8,
                                                   "fail_after": 1, "errors": "ours"}, actor="boss")
        t = core.now()
        self.run_at("site", "500", t, self.proc(0, "ERROR a\nERROR b\n"))  # 8 an hour is 2 in 15m: normal
        run = core.ops_history(self.c, "site", "500", 1)["runs"][0]
        self.assertEqual((run["status"], run["summary"]), ("ok", "2 lines match in 15m; normal 8 an hour, 2.0 in 15m "
                                                                 "(limit 1)"))
        self.run_at("site", "500", t + timedelta(minutes=5), self.proc(0, "ERROR c\n"))
        self.assertEqual(core.ops_history(self.c, "site", "500", 1)["runs"][0]["status"], "critical")
        self.assertEqual(self.state("site", "500"), "fault")

    def test_with_subject_on_each_line_of_the_output_has_its_own_state(self):
        core.ops_add(self.c, "site", "queues", "cmd", {"command": "check_queues", "subject": True, "fail_after": 2},
                     actor="boss")
        t = core.now()
        # The form of maxpm ops probe: the status line, then '<subject>: <text>' (a status word is optional; with
        # none the subject has the run's status), then one JSON object of evidence.
        self.run_at("site", "queues", t, self.proc(1, "QUEUES CRITICAL - 1 late|late=1\nmail: CRITICAL 900 waiting\n"
                                                      "sms: OK 0 waiting\n"))
        self.run_at("site", "queues", t + timedelta(minutes=5),
                    self.proc(1, 'QUEUES CRITICAL - 1 late|late=1\nmail: 950 waiting\n{"subjects": {"mail": "950"}}\n'))
        run = core.ops_history(self.c, "site", "queues", 1)["runs"][0]
        self.assertEqual((run["summary"], run["perfdata"], json.loads(run["evidence"])),
                         ("QUEUES CRITICAL - 1 late", "late=1", {"subjects": {"mail": "950"}}))
        self.assertEqual(json.loads(run["subjects"]), {"mail": {"status": "critical", "text": "950 waiting"}})
        self.run_at("site", "queues", t + timedelta(minutes=10), self.proc(0, "QUEUES OK\n"))
        self.run_at("site", "queues", t + timedelta(minutes=15), self.proc(0, "QUEUES OK\nsms: OK\n"))
        self.assertEqual(self.events("site", "queues"),
                         [("ops:site:queues", "failing"), ("ops:site:queues:mail", "failing"),
                          ("ops:site:queues", "fault"), ("ops:site:queues:mail", "fault"),
                          ("ops:site:queues", "recovered"), ("ops:site:queues:mail", "recovered")])
        last = json.loads(core.ops_history(self.c, "site", "queues", 1)["runs"][0]["subjects"])
        self.assertEqual(last, {"sms": {"status": "ok", "text": ""}})

    def test_a_run_that_a_restart_of_serve_cut_runs_again_and_opens_no_fault(self):
        """#5627: serve --restart (execv: the same pid, a new start) cut running log checks, and each became a
        'the run never ended' fault. A cut run goes, runs again at once, and only a second cut in a row is a fault."""
        core.ops_add(self.c, "site", "logs", "cmd", {"command": "check_logs", "every": "5m", "fail_after": 1},
                     actor="boss")
        t = core.now() - timedelta(minutes=20)
        self.run_at("site", "logs", t, self.proc(0, "LOGS OK\n"))
        c = core.ops_find(self.c, "site", "logs")
        run = core.ops_start(self.c, c, at=t + timedelta(minutes=10))
        self.assertFalse(core._ops_cut(core.OPS_PROCESS))  # this process runs it
        with mock.patch.object(core, "OPS_PROCESS", f"{os.getpid()}:restarted"):  # serve started again
            cut = core.ops_close_lost(self.c, at=core.now())
            self.assertEqual([(x["status"], x["run"]) for x in cut], [("cut", run["id"])])
            self.assertIsNone(self.c.execute("SELECT 1 FROM ops_runs WHERE id=?", (run["id"],)).fetchone())
            self.assertEqual(self.state("site", "logs"), "healthy")
            self.assertEqual([x["name"] for x in core.ops_due(self.c, core.now())], ["logs"])  # at once
            self.assertIn("a stop of the process that ran it cut its run",
                          self.c.execute("SELECT change FROM events ORDER BY id DESC LIMIT 1").fetchone()[0])
            again = core.ops_start(self.c, c, at=core.now())
        # Its run is cut again before any run of it finished: lost at once, a fault.
        with mock.patch.object(core, "OPS_PROCESS", f"{os.getpid()}:restarted-twice"):
            lost = core.ops_close_lost(self.c, at=core.now())
        self.assertEqual([(x["status"], x["run"]) for x in lost], [("unknown", again["id"])])
        self.assertEqual(self.state("site", "logs"), "fault")
        self.assertTrue(core._ops_cut("999999999:gone"))  # another process that ended
        self.assertFalse(core._ops_cut(None))  # a run of before #5627: the timeout rule holds

    def test_a_due_check_waits_for_its_every_a_pause_ends_by_itself_and_a_lost_run_counts_as_unknown(self):
        core.ops_add(self.c, "site", "home", "url", {"url": "https://example.com/", "every": "5m", "fail_after": 1},
                     actor="dev")
        t = core.now()
        self.assertEqual([c["name"] for c in core.ops_due(self.c, t)], ["home"])
        c = core.ops_find(self.c, "site", "home")
        run = core.ops_start(self.c, c, at=t)
        self.assertIsNone(core.ops_start(self.c, c, at=t))  # one run at a time
        self.assertEqual(core.ops_due(self.c, t + timedelta(minutes=10)), [])  # a run is open
        self.assertEqual(core.ops_close_lost(self.c, running={run["id"]}, at=t + timedelta(minutes=10)), [])
        lost = core.ops_close_lost(self.c, at=t + timedelta(minutes=10))
        self.assertEqual([x["status"] for x in lost], ["unknown"])
        self.assertEqual(self.state("site", "home"), "fault")
        self.assertEqual(core.ops_due(self.c, t + timedelta(minutes=4)), [])
        self.assertEqual([c["name"] for c in core.ops_due(self.c, t + timedelta(minutes=5))], ["home"])
        with mock.patch.object(core, "now", lambda: t):
            core.ops_pause(self.c, "site", "home", until="1h", actor="dev")
        self.assertEqual(core.ops_due(self.c, t + timedelta(minutes=30)), [])
        self.assertEqual([c["name"] for c in core.ops_due(self.c, t + timedelta(hours=1))], ["home"])
        self.assertEqual(self.state("site", "home"), "new")
        self.assertEqual(self.events("site", "home")[-2:], [("ops:site:home", "paused"), ("ops:site:home", "resumed")])

    # ---- the outside switch (#5435)

    def test_serve_beats_to_the_outside_address_once_each_interval_and_a_failure_is_logged_not_retried(self):
        sent = []
        state = dict(core.OPS_DEADMAN)
        self.addCleanup(lambda: core.OPS_DEADMAN.update(state))
        core.OPS_DEADMAN.update(last=None, sending=False, sent=None, error=None)
        self.assertIsNone(core.ops_deadman(self.c))  # no address: no beat
        with self.assertRaisesRegex(RiverError, "http:// or https://"):
            core.config_set(self.c, "ops_deadman_url", "hc-ping.example/abc")
        core.config_set(self.c, "ops_deadman_url", "https://hc-ping.example/abc")
        core.config_set(self.c, "ops_deadman_every", "1m")
        gate = threading.Event()

        def get(url, limit):
            sent.append((url, limit))
            gate.wait(5)
            return 200
        clock = [1000.0]
        with mock.patch.object(core, "OPS_DEADMAN_GET", get), mock.patch.object(core, "_monotonic", lambda: clock[0]):
            t0 = time.monotonic()
            self.assertEqual(core.ops_deadman(self.c), "https://hc-ping.example/abc")
            self.assertLess(time.monotonic() - t0, 1)  # the GET waits in its thread, not in the loop
            clock[0] += 70
            self.assertIsNone(core.ops_deadman(self.c))  # the first one still runs: no second at the same time
            gate.set()
            for _ in range(100):
                if not core.OPS_DEADMAN["sending"]:
                    break
                time.sleep(0.02)
            self.assertIsNotNone(core.OPS_DEADMAN["sent"])
            self.assertEqual(core.ops_deadman(self.c), "https://hc-ping.example/abc")  # 70 s after the first
            for _ in range(100):
                if not core.OPS_DEADMAN["sending"]:
                    break
                time.sleep(0.02)
            clock[0] += 30
            self.assertIsNone(core.ops_deadman(self.c))  # 30 s after the last start: not yet
        self.assertEqual(sent, [("https://hc-ping.example/abc", 10.0)] * 2)

        def down(url, limit):
            raise OSError("connection refused")
        with mock.patch.object(core, "OPS_DEADMAN_GET", down), mock.patch.object(core, "_monotonic", lambda: clock[0]):
            clock[0] += 60
            with contextlib.redirect_stdout(io.StringIO()) as out:
                core.ops_deadman(self.c)
                for _ in range(100):
                    if not core.OPS_DEADMAN["sending"]:
                        break
                    time.sleep(0.02)
            self.assertIn("connection refused", core.OPS_DEADMAN["error"])
            self.assertIn("the beat to https://hc-ping.example/abc failed", out.getvalue())
            clock[0] += 5
            self.assertIsNone(core.ops_deadman(self.c))  # no retry in a loop: the next beat waits for the interval

    # A profile: every check of a project or a deploy target, and its ops_* settings, as one JSON file (#5436).
    PROFILE = {"maxpm_ops_profile": 1, "scope": "project:site", "checks": [
        {"name": "home", "form": "url", "url": "https://example.com/", "body": "Welcome", "every": "1m",
         "timeout": "10s", "severity": "page", "runbook": "the site is down: read the server log"},
        {"name": "nightly", "form": "beat", "period": "26h"},
        {"name": "errors", "form": "log", "command": "tail -n 500 app.log", "pattern": "ERROR", "limit": 5,
         "subject": True, "repair": "restart-app"}]}

    def checks(self, scope="site"):
        return {c["name"]: c for c in core.ops_list(self.c, scope)["checks"]}

    def test_the_file_format_export_then_apply_elsewhere_gives_the_same_checks(self):
        core.ops_profile_apply(self.c, self.PROFILE, yes=True, actor="mark")
        out = core.ops_profile_export(self.c, "site")
        self.assertEqual(out, {"maxpm_ops_profile": 1, "scope": "project:site", "checks": [
            {"name": "errors", "form": "log", "command": "tail -n 500 app.log", "pattern": "ERROR", "limit": 5,
             "window": "15m", "every": "5m", "fail_after": 3, "recover_after": 2, "timeout": "30s",
             "severity": "item", "repair": "restart-app", "subject": True},
            {"name": "home", "form": "url", "url": "https://example.com/", "statuses": "200-399", "body": "Welcome",
             "every": "1m", "fail_after": 3, "recover_after": 2, "timeout": "10s", "severity": "page",
             "runbook": "the site is down: read the server log"},
            {"name": "nightly", "form": "beat", "period": "26h", "every": "5m", "fail_after": 3, "recover_after": 2,
             "timeout": "30s", "severity": "item"}]})
        core.project_add(self.c, "shop")
        r = core.ops_profile_apply(self.c, json.loads(json.dumps(out)), scope="shop", yes=True, actor="mark")
        self.assertEqual(([x["name"] for x in r["add"]], r["written"]), (["errors", "home", "nightly"], True))
        strip = lambda cs: {n: {k: c[k] for k in core._OPS_COLS} for n, c in cs.items()}  # noqa: E731
        self.assertEqual(strip(self.checks("shop")), strip(self.checks("site")))

    def test_apply_shows_the_plan_first_writes_with_yes_and_a_second_apply_changes_nothing(self):
        core.ops_add(self.c, "site", "old", "url", {"url": "https://old.example.com/"}, actor="mark")
        core.ops_add(self.c, "site", "nightly", "beat", {"period": "26h"}, actor="mark")
        core.ops_pause(self.c, "site", "nightly", "2h", actor="mark")
        plan = core.ops_profile_apply(self.c, self.PROFILE, actor="mark")
        self.assertEqual(([x["name"] for x in plan["add"]], plan["change"], plan["remove"], plan["same"],
                          plan["written"]), (["home", "errors"], [], ["old"], ["nightly"], False))
        self.assertEqual(sorted(self.checks()), ["nightly", "old"])  # nothing written
        r = core.ops_profile_apply(self.c, self.PROFILE, yes=True, actor="mark")
        self.assertTrue(r["written"])
        self.assertEqual(sorted(self.checks()), ["errors", "home", "nightly"])
        self.assertEqual(self.checks()["nightly"]["state"], "paused")  # a check that stays keeps its pause
        again = core.ops_profile_apply(self.c, self.PROFILE, yes=True, actor="mark")
        self.assertEqual((again["add"], again["change"], again["remove"], again["written"]), ([], [], [], False))
        changed = json.loads(json.dumps(self.PROFILE))
        changed["checks"][0]["every"] = "2m"
        del changed["checks"][0]["runbook"]  # left out: no runbook
        r = core.ops_profile_apply(self.c, changed, yes=True, actor="mark")
        self.assertEqual(r["change"][0]["name"], "home")
        self.assertEqual(r["change"][0]["fields"], ["every", "runbook"])
        self.assertIsNone(self.checks()["home"]["runbook"])

    def test_one_check_the_actor_may_not_set_refuses_the_whole_profile_before_any_write(self):
        with self.assertRaisesRegex(RiverError, r"check errors \(to add\): dev is an agent; only a person or the "
                                                r"manager .* nothing was written"):
            core.ops_profile_apply(self.c, self.PROFILE, yes=True, actor="dev")
        self.assertEqual(self.checks(), {})  # not even the url and beat checks
        safe = {"maxpm_ops_profile": 1, "scope": "site", "checks": self.PROFILE["checks"][:2]}
        core.ops_profile_apply(self.c, safe, yes=True, actor="dev")  # an agent may set url and beat checks
        core.ops_add(self.c, "site", "jobs", "cmd", {"command": "true"}, actor="boss")
        with self.assertRaisesRegex(RiverError, "a cmd check"):
            core.ops_profile_apply(self.c, safe, yes=True, actor="dev")  # it would remove the cmd check
        self.assertIn("jobs", self.checks())
        target = {"maxpm_ops_profile": 1, "checks": [{"name": "api", "form": "url", "url": "https://x.io/",
                                                      "repair": "restart"}]}
        with self.assertRaisesRegex(RiverError, "only a person .* deploy target prod"):
            core.ops_profile_apply(self.c, target, scope="prod", yes=True, actor="boss")
        core.ops_profile_apply(self.c, target, scope="prod", yes=True, actor="mark")

    def test_a_bad_file_or_a_bad_setting_is_refused_before_any_write(self):
        bad = [({"checks": []}, "not an ops profile"),
               ({"maxpm_ops_profile": 1, "checks": [{"name": "a", "form": "beat", "period": "1h"},
                                                    {"name": "a", "form": "beat", "period": "2h"}]}, "two times: a"),
               ({"maxpm_ops_profile": 1, "checks": [{"name": "a", "form": "beat", "period": "1h", "colour": "red"}]},
                "no field colour"),
               ({"maxpm_ops_profile": 1, "checks": [{"name": "a"}]}, "has no form"),
               ({"maxpm_ops_profile": 1, "checks": [], "settings": {"review": "on"}}, "start with ops_"),
               ({"maxpm_ops_profile": 1, "checks": [{"name": "a", "form": "beat", "period": "1h"}],
                 "settings": {"ops_nothing": "x"}}, "ops_nothing")]
        for profile, why in bad:
            with self.assertRaisesRegex(RiverError, why):
                core.ops_profile_apply(self.c, profile, scope="site", yes=True, actor="mark")
        self.assertEqual(self.checks(), {})
        with self.assertRaisesRegex(RiverError, "names no scope"):
            core.ops_profile_apply(self.c, {"maxpm_ops_profile": 1, "checks": []}, actor="mark")

    def test_the_settings_of_the_scope_go_with_the_profile(self):
        with mock.patch.dict(core.DEFAULT_SETTINGS, {"ops_page": "unhealed 15m"}):
            p = {"maxpm_ops_profile": 1, "scope": "site", "checks": [], "settings": {"ops_page": "always"}}
            self.assertEqual(core.ops_profile_apply(self.c, p, actor="mark")["settings"], {"ops_page": "always"})
            core.ops_profile_apply(self.c, p, yes=True, actor="mark")
            self.assertEqual(core.setting(self.c, "ops_page", project_id=core._project(self.c, "site")["id"]), "always")
            self.assertEqual(core.ops_profile_export(self.c, "site")["settings"], {"ops_page": "always"})
            self.assertEqual(core.ops_profile_apply(self.c, p, actor="mark")["settings"], {})

    def test_the_command_line_exports_to_a_file_and_applies_it_with_yes(self):
        from river import cli
        core.ops_profile_apply(self.c, self.PROFILE, yes=True, actor="mark")
        path = os.path.join(self.dir.name, "site.json")
        core.project_add(self.c, "shop")

        def run(*words):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = cli.run(list(words))
            return code, out.getvalue()
        self.assertIn("(3 checks) is in", run("--as", "mark", "ops", "profile", "export", "site", "--out", path)[1])
        code, out = run("--as", "mark", "ops", "profile", "apply", path, "--scope", "shop")
        self.assertIn("nothing written yet", out)
        self.assertIn(f"write it: maxpm ops profile apply {path} --scope shop --yes", out)
        self.assertEqual(self.checks("shop"), {})
        code, out = run("--as", "mark", "ops", "profile", "apply", path, "--scope", "shop", "--yes")
        self.assertIn("profile for project:shop: written", out)
        self.assertEqual(sorted(self.checks("shop")), ["errors", "home", "nightly"])
        code, out = run("--as", "mark", "ops", "profile", "apply", path, "--scope", "shop", "--yes")
        self.assertIn("nothing to change (3 the same)", out)

    # ---- proposals and the guided setup (#5437)

    def notes_to(self, who):
        return [r[0] for r in self.c.execute("SELECT body FROM messages WHERE to_agent=? AND kind='note' ORDER BY id",
                                             (who,))]

    def test_an_agent_proposes_a_command_check_that_never_runs_until_the_manager_accepts_it(self):
        with self.assertRaisesRegex(RiverError, "say why"):
            core.ops_propose(self.c, "site", "jobs", "cmd", {"command": "check_jobs"}, actor="dev")
        r = core.ops_propose(self.c, "site", "jobs", "cmd", {"command": "check_jobs"},
                             why="the nightly jobs fail with no word", actor="dev")
        self.assertEqual((r["changed"], r["replaced"], r["proposal"]["changes"]), ("proposed", False, None))
        listed = core.ops_list(self.c, "site")
        self.assertEqual(listed["checks"], [])
        self.assertEqual([(p["name"], p["why"], p["proposed_by"]) for p in listed["proposals"]],
                         [("jobs", "the nightly jobs fail with no word", "dev")])
        self.assertEqual(core.ops_due(self.c), [])  # a proposal never runs
        self.assertIn("Accept: maxpm ops accept site jobs", self.notes_to("boss")[-1])
        with self.assertRaisesRegex(RiverError, "only a person or the manager accepts"):
            core.ops_accept(self.c, "site", "jobs", actor="dev")
        r = core.ops_accept(self.c, "site", "jobs", actor="boss")
        self.assertEqual((r["changed"], r["fields"], r["check"]["form"], r["check"]["created_by"]),
                         ("accepted", None, "cmd", "boss"))
        self.assertEqual([x["name"] for x in core.ops_due(self.c)], ["jobs"])
        self.assertIn("accepted your proposed check site/jobs", self.notes_to("dev")[-1])
        self.assertEqual(core.ops_proposals(self.c, "site"), [])

    def test_show_and_accept_name_the_cron_of_a_cron_check_not_its_every(self):
        """A weekly check showed 'every 5m' (#5538): its every is unused while it has a cron."""
        from river import cli
        core.ops_propose(self.c, "site", "weekly", "cmd", {"command": "check_catalog", "cron": "0 9 * * mon"},
                         why="the catalog changes each week", actor="dev")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.render_ops(argparse.Namespace(cmd="ops", ocmd="accept"),
                           core.ops_accept(self.c, "site", "weekly", actor="boss"))
            cli.render_ops(argparse.Namespace(cmd="ops", ocmd="show"), core.ops_show(self.c, "site", "weekly"))
        accepted, shown = out.getvalue().split("\n", 1)
        self.assertIn("at cron '0 9 * * mon', fault after 3", accepted)
        self.assertIn("    at cron '0 9 * * mon', timeout 30s, fault after 3", shown)
        self.assertNotIn("every 5m", out.getvalue())
        with self.assertRaisesRegex(RiverError, "no proposed check 'jobs'"):
            core.ops_accept(self.c, "site", "jobs", actor="boss")

    def test_a_proposed_change_replaces_the_open_one_and_a_reject_needs_a_reason(self):
        core.ops_add(self.c, "site", "home", "url", {"url": "https://example.com/"}, actor="dev")
        with self.assertRaisesRegex(RiverError, "like this already"):
            core.ops_propose(self.c, "site", "home", fields={"every": "5m"}, why="x", actor="dev")
        with self.assertRaisesRegex(RiverError, "has no check 'api': name the form"):
            core.ops_propose(self.c, "site", "api", fields={"url": "https://x.io/"}, why="x", actor="dev")
        core.ops_propose(self.c, "site", "home", fields={"every": "1m"}, why="a minute matters", actor="dev")
        r = core.ops_propose(self.c, "site", "home", fields={"every": "1m", "repair": "restart web"},
                             why="it can heal itself", actor="dev")
        self.assertEqual((r["replaced"], r["proposal"]["changes"]), (True, ["every", "repair"]))
        self.assertEqual(len(core.ops_proposals(self.c)), 1)
        with self.assertRaisesRegex(RiverError, "say why"):
            core.ops_reject(self.c, "site", "home", actor="boss")
        r = core.ops_reject(self.c, "site", "home", actor="boss", note="restart hides the cause")
        self.assertEqual((r["proposal"]["state"], r["proposal"]["note"]), ("rejected", "restart hides the cause"))
        self.assertEqual(core.ops_show(self.c, "site", "home")["every"], "5m")
        self.assertIn("rejected your proposed check site/home: restart hides the cause", self.notes_to("dev")[-1])

    def test_an_accept_keeps_the_rights_of_ops_add(self):
        core.ops_propose(self.c, "prod", "api", "url", {"url": "https://example.com/", "repair": "restart"},
                         why="the api hangs", actor="dev")
        with self.assertRaisesRegex(RiverError, "only a person .* repair on the deploy target prod"):
            core.ops_accept(self.c, "prod", "api", actor="boss")
        self.assertEqual(len(core.ops_proposals(self.c, "prod")), 1)
        r = core.ops_accept(self.c, "prod", "api", actor="mark", note="good catch")
        self.assertEqual((r["check"]["repair"], r["proposal"]["note"]), ("restart", "good catch"))

    def trial(self, args, actor, runner=None, http=None):
        from river import server
        with mock.patch.object(core, "OPS_RUNNER", runner), mock.patch.object(core, "OPS_HTTP", http):
            return server.OPS["ops_trial"](self.c, args, actor)

    def test_a_trial_runs_a_check_once_in_serve_and_writes_nothing(self):
        r = self.trial({"scope": "site", "form": "url", "fields": {"url": "https://example.com/", "body": "Welcome"}},
                       "dev", http=lambda url, t: (200, "<h1>Welcome</h1>"))
        self.assertEqual((r["check"], r["result"]["status"], r["result"]["summary"]),
                         ("site/example-com", "ok", "HTTP 200"))
        r = self.trial({"scope": "site", "form": "url", "fields": {"url": "https://example.com/", "body": "Welcome"}},
                       "dev", http=lambda url, t: (200, "<h1>Sorry</h1>"))
        self.assertEqual(r["result"]["status"], "critical")
        with self.assertRaisesRegex(RiverError, "only a person or the manager"):
            self.trial({"scope": "site", "form": "cmd", "fields": {"command": "check_jobs"}}, "dev")
        r = self.trial({"scope": "site", "form": "cmd", "fields": {"command": "check_jobs --all"}}, "boss",
                       self.proc(1, "2 jobs failed"))
        self.assertEqual((r["check"], r["result"]["status"], r["result"]["summary"]),
                         ("site/check_jobs", "critical", "2 jobs failed"))
        r = self.trial({"scope": "site", "form": "log", "fields": {"command": "tail app.log", "pattern": "ERROR",
                                                                  "limit": "2"}}, "boss",
                       self.proc(0, "ERROR a\nok\nERROR b\nERROR c"))
        self.assertEqual((r["result"]["status"], r["result"]["summary"]),
                         ("critical", "3 lines match now; a fault from 2 in 15m"))
        r = self.trial({"scope": "site", "name": "nightly", "form": "beat", "fields": {"period": "1d"}}, "dev")
        self.assertIsNone(r["result"])
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM checks").fetchone()[0], 0)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM ops_runs").fetchone()[0], 0)

    def setup_run(self, *words, answers=None, http=None, runner=None, beat=None):
        """maxpm ops setup through cli.run; maxpm serve's op ops_trial runs here. answers: the terminal's."""
        from river import cli, server
        asked, out = [], io.StringIO()
        replies = iter(answers or [])

        def ask(q):
            asked.append(q)
            return next(replies)

        def serve(conn, op, args, actor=None, timeout=5, retries=3):
            return server.OPS[op](self.c, args, actor)
        with mock.patch.object(cli, "ask_server", serve), mock.patch.object(core, "OPS_HTTP", http), \
                mock.patch.object(core, "OPS_RUNNER", runner), mock.patch.object(core, "OPS_DEADMAN_GET", beat), \
                mock.patch.object(cli, "SETUP_ASK", ask if answers is not None else None), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.run(list(words))
        return code, out.getvalue(), asked

    def test_setup_with_options_keeps_a_check_whose_first_run_passed(self):
        up = lambda url, t: (200, "ok")  # noqa: E731
        code, out, _ = self.setup_run("--as", "dev", "ops", "setup", "site", "--url", "https://example.com/", "--yes",
                                      http=up)
        self.assertIn("passed: HTTP 200", out)
        self.assertIn("kept example-com: url GET https://example.com/ (200-399); every 5m", out)
        down = lambda url, t: (503, "busy")  # noqa: E731
        _, out, _ = self.setup_run("--as", "dev", "ops", "setup", "site", "--url", "https://api.example.com/",
                                   "--yes", http=down)
        self.assertIn("not kept api-example-com: its first run failed (keep it anyway: --anyway)", out)
        self.setup_run("--as", "dev", "ops", "setup", "site", "--url", "https://api.example.com/", "--anyway", "--yes",
                       http=down)
        self.assertEqual(sorted(x["name"] for x in core.ops_list(self.c, "site")["checks"]),
                         ["api-example-com", "example-com"])
        _, out, _ = self.setup_run("--as", "dev", "ops", "setup", "site", "--command", "check_jobs", "--yes")
        self.assertIn("not kept check_jobs: dev is an agent", out)
        self.assertIn("propose it: add --why-propose", out)
        _, out, _ = self.setup_run("--as", "dev", "ops", "setup", "site", "--command", "check_jobs", "--why-propose",
                                   "jobs fail with no word", "--yes")
        self.assertIn("proposed check_jobs to the manager", out)
        _, out, _ = self.setup_run("--as", "dev", "ops", "setup", "site", "--period", "1d", "--name", "nightly",
                                   "--yes")
        self.assertIn("the job reports each time it runs: maxpm ops beat site nightly", out)

    def test_setup_with_options_sends_the_test_notice_and_sets_the_outside_switch(self):
        _, out, _ = self.setup_run("--as", "mark", "ops", "setup", "site", "--test-notice", "--deadman-url",
                                   "https://hc-ping.com/abc", "--yes", beat=lambda url, t: 200)
        self.assertIn("test notice: not sent (your phone gets no notices yet: maxpm notify setup ntfy)", out)
        self.assertIn("outside switch: https://hc-ping.com/abc; the first beat arrived", out)
        self.assertEqual(core.setting(self.c, "ops_deadman_url"), "https://hc-ping.com/abc")
        with self.assertRaisesRegex(RiverError, "nothing to set up"):
            self.setup_run("--as", "mark", "ops", "setup", "site", "--yes")

    def test_setup_asks_one_question_at_a_time_in_a_terminal(self):
        answers = ["1",                                    # site
                   "1", "https://example.com/", "", "", "", "y", "",     # url: address, body, name, every, phone, keep
                   "2", "", "nightly-backup", "", "",       # beat: period, name, phone, keep
                   "",                                      # no more checks
                   "n",                                     # no ntfy now
                   ""]                                      # no outside switch
        code, out, asked = self.setup_run("--as", "mark", "ops", "setup", answers=answers,
                                          http=lambda url, t: (200, "ok"))
        self.assertEqual(asked, [
            "Which project or deploy target should MaximizePM watch? (a number or a name) ",
            "Which one? (1-4, Enter: no more checks) ",
            "What address shows that your site is up? ",
            "Which text must the page hold? (Enter: none, a good HTTP status is enough) ",
            "A short name for this check [example-com] ",
            "How often should it run? [5m] ",
            "Should a fault of this check be able to reach your phone? (y/N) ",
            "Keep this check? (Y/n) ",
            "Which one? (1-4, Enter: no more checks) ",
            "How long may the job go without a report? (for example 1h or 1d) [1d] ",
            "Which job reports? Its short name, for example nightly-backup ",
            "Should a fault of this check be able to reach your phone? (y/N) ",
            "Keep this check? (Y/n) ",
            "Which one? (1-4, Enter: no more checks) ",
            "Set up notices to your phone now (the free ntfy app)? (Y/n) ",
            "The ping address (Enter: skip for now) "])
        self.assertIn("  1. project site\n  2. target prod\n", out)
        self.assertIn("passed: HTTP 200", out)
        checks = {x["name"]: x for x in core.ops_list(self.c, "site")["checks"]}
        self.assertEqual((checks["example-com"]["severity"], checks["nightly-backup"]["config"]),
                         ("page", {"period": "1d"}))
        self.assertIn("kept nightly-backup: beat a beat at least every 1d", out)
        self.assertIn("maxpm ops beat site nightly-backup", out)

    def test_setup_offers_a_proposal_when_the_person_at_the_terminal_is_an_agent(self):
        answers = ["3", "check_jobs", "", "", "", "y", "the nightly jobs fail with no word", "", "n", ""]
        _, out, asked = self.setup_run("--as", "dev", "ops", "setup", "site", answers=answers)
        self.assertIn("dev is an agent; only a person or the manager sets", out)
        self.assertEqual(asked[5:7], ["Propose it to the manager instead? (Y/n) ",
                                      "Why is it needed? (one line for the manager) "])
        self.assertIn("proposed check_jobs to the manager", out)
        self.assertEqual([p["name"] for p in core.ops_proposals(self.c, "site")], ["check_jobs"])


class OpsNoise(Base):
    """Noise rules (#5504): a check, a subject of it, or what matches a pattern is not a problem until the rule ends:
    a matching fault opens no item and starts no agent, and a log check does not count the matching lines."""

    run_at, proc = Ops.run_at, staticmethod(Ops.proc)

    def setUp(self):
        super().setUp()
        core.target_add(self.c, "prod", "the servers")
        core.project_add(self.c, "site", target="prod")
        for n in ("dev", "boss", "other"):
            core.register(self.c, n)
        self.c.execute("UPDATE agents SET role='manager' WHERE name='boss'")
        self.c.commit()
        core.register(self.c, "mark", human=True)

    def items(self):
        return [r[0] for r in self.c.execute("SELECT title FROM items WHERE kind='operational' ORDER BY id")]

    def test_a_rule_keeps_a_fault_out_of_the_queue_and_an_agents_rule_ends_within_30_days(self):
        core.ops_add(self.c, "site", "job", "plugin", {"command": "x", "fail_after": 1}, actor="boss")
        with self.assertRaisesRegex(RiverError, "ends within ops_noise_max"):
            core.ops_noise_add(self.c, "site", "job", "closed pages", until="60d", actor="dev")
        with self.assertRaisesRegex(RiverError, "say why"):
            core.ops_noise_add(self.c, "site", "job", " ", actor="dev")
        r = core.ops_noise_add(self.c, "site", "job", "the job's host is being retired", actor="dev")["rule"]
        self.assertEqual((r["state"], r["created_by"]), ("in force", "dev"))
        self.assertLessEqual(core.parse_iso(r["until"]), core.now() + timedelta(days=30))
        self.assertTrue([m for m in core.inbox(self.c, "boss") if f"dev added noise rule n{r['id']}" in m["body"]])
        res = self.run_at("site", "job", core.now(), self.proc(2, "JOB CRITICAL - down"))
        self.assertEqual(res["items"], [{"key": "ops:site:job", "item": None, "action": "noise", "rule": r["id"],
                                         "kind": "noise"}])
        self.assertEqual(self.items(), [])
        self.assertEqual(core.ops_noise_list(self.c, "site")[0]["hits"], 1)
        self.assertTrue(self.c.execute("SELECT 1 FROM events WHERE change LIKE 'ops noise: ops:site:job in fault, kept "
                                       "out of the queue by rule n%'").fetchone())
        # The rule ends: the fault that still lasts gets its item at the next run.
        with self.assertRaisesRegex(RiverError, "it, a person or the manager removes it"):
            core.ops_noise_rm(self.c, f"n{r['id']}", actor="other", note="x")
        core.ops_noise_rm(self.c, f"n{r['id']}", actor="boss", note="the host stays")
        self.assertEqual(core.ops_noise_list(self.c, "site"), [])
        self.run_at("site", "job", core.now() + timedelta(minutes=5), self.proc(2, "JOB CRITICAL - down"))
        self.assertEqual(self.items(), ["ops:site:job JOB CRITICAL - down"])

    def test_a_pattern_matches_the_summary_or_a_subject_and_a_log_check_does_not_count_its_lines(self):
        core.ops_add(self.c, "site", "certs", "plugin", {"command": "x", "fail_after": 1, "subject": True}, actor="boss")
        core.ops_noise_add(self.c, "site", "certs", "a test host", pattern="^staging", actor="dev")
        seen = {}

        def runner(cmd, folder, limit, env):
            seen["noise"] = env["MAXPM_NOISE"]
            return subprocess.CompletedProcess(cmd, 2, "CERTS CRITICAL\nstaging.example: expired\nshop.example: "
                                                       "expired\n", "")
        res = self.run_at("site", "certs", core.now(), runner)
        self.assertEqual(seen["noise"], "^staging")
        self.assertEqual(sorted((x["key"], x["action"]) for x in res["items"]),
                         [("ops:site:certs:shop.example", "opened"), ("ops:site:certs:staging.example", "noise")])
        core.ops_add(self.c, "site", "errs", "log", {"command": "cat log", "pattern": "Traceback|Error", "limit": 2,
                                                    "fail_after": 1}, actor="boss")
        core.ops_noise_add(self.c, "site", "errs", "a page closed its connection",
                           pattern="BrokenPipeError|ConnectionResetError", actor="dev")
        out = "Traceback\nBrokenPipeError: [Errno 32]\nConnectionResetError: reset\nok\n"
        res = self.run_at("site", "errs", core.now(), self.proc(0, out))
        self.assertEqual(res["status"], "ok")  # one line counts (Traceback), the limit is 2
        run = core.ops_history(self.c, "site", "errs", 1)["runs"][0]
        self.assertEqual(json.loads(run["evidence"])["noise"], 2)


class OpsKinds(Base):
    """The kinds of problems (#5509): each fault gets one kind before an agent starts, by eight rules in order with
    no model (river/kinds.json), and the kind decides what happens."""

    run_at, proc = Ops.run_at, staticmethod(Ops.proc)

    def setUp(self):
        super().setUp()
        core.target_add(self.c, "prod", "the servers")
        core.project_add(self.c, "site", target="prod")
        for n in ("dev", "boss"):
            core.register(self.c, n)
        self.c.execute("UPDATE agents SET role='manager' WHERE name='boss'")
        self.c.commit()
        core.register(self.c, "mark", human=True)

    def check(self, name, **f):
        core.ops_add(self.c, f.pop("scope", "site"), name, "plugin", {"command": "x", "fail_after": 1, **f}, actor="boss")

    def fault(self, name, code=2, out="CRITICAL - down", at=None, scope="site", timed_out=False):
        return self.run_at(scope, name, at or core.now(), self.proc(code, out, timed_out))["items"]

    def item(self, i):
        return core._item(self.c, i)

    def person(self, i):
        r = self.c.execute("SELECT i.* FROM deps d JOIN items i ON i.id=d.blocked_by WHERE d.item_id=? AND "
                           "i.doer='human'", (i,)).fetchone()
        return dict(r) if r else None

    def test_the_data_file_names_every_kind_of_the_design_and_each_known_message_has_a_kind(self):
        k = core.ops_kinds()
        self.assertEqual(list(k["kinds"]), ["broken-check", "noise", "planned", "dependency", "capacity", "service-down",
                                            "expired", "configuration", "data", "release-regression", "code-defect",
                                            "unknown"])
        self.assertEqual(len(k["rules"]), 8)
        for m in k["known_messages"]:
            self.assertIn(m["kind"], k["kinds"])
            re.compile(m["pattern"])
        self.assertTrue(all(d["doer"] in ("ai", "human", "repair", "none") for d in k["kinds"].values()))
        self.assertEqual([x for x, d in k["kinds"].items() if d.get("code")], ["release-regression", "code-defect"])
        self.assertEqual((core.ops_kind_id("Service down"), core.ops_kind_id("outside dependency"),
                          core.ops_kind_id("expired item"), core.ops_kind_id("bogus")),
                         ("service-down", "dependency", "expired", None))

    def test_rule_1_a_check_that_failed_itself_is_a_broken_check_also_when_a_noise_rule_matches(self):
        self.check("job")
        core.ops_noise_add(self.c, "site", "job", "a test host", actor="boss")
        [x] = self.fault("job", 3, "JOB UNKNOWN - no access")
        self.assertEqual((x["action"], x["kind"]), ("opened", "broken-check"))
        it = self.item(x["item"])
        self.assertEqual(it["doer"], "ai")
        self.assertTrue(it["context"].startswith("Kind: broken check (rule 1: the check ended UNKNOWN: JOB UNKNOWN - no "
                                                 "access).\nWhat happens: fix the check"), it["context"])
        self.check("logs")
        [x] = self.fault("logs", 2, "ssh: connect to host edge-1 port 22: Connection refused")
        self.assertEqual(x["kind"], "broken-check")
        self.check("slow")
        [x] = self.fault("slow", 2, "", timed_out=True)
        self.assertEqual(x["kind"], "broken-check")

    def test_rule_2_noise_opens_no_item(self):
        self.check("job")
        core.ops_noise_add(self.c, "site", "job", "bots", actor="boss")
        self.assertEqual(self.fault("job")[0]["kind"], "noise")
        self.assertIsNone(self.fault("job", at=core.now() + timedelta(minutes=5))[0]["item"])

    def test_rule_3_a_pause_of_a_check_of_the_same_system_makes_a_fault_planned(self):
        self.check("db", scope="prod")
        self.check("job")
        core.ops_pause(self.c, "prod", "db", until="2h", actor="boss")
        [x] = self.fault("job")
        self.assertEqual((x["action"], x["item"], x["kind"]), ("planned", None, "planned"))
        self.assertTrue(self.c.execute("SELECT 1 FROM events WHERE change LIKE 'ops planned: ops:site:job in fault while "
                                       "prod/db is paused until %'").fetchone())

    def test_rule_4_an_outside_dependency_waits_on_the_vendor_and_takes_the_faults_that_name_it_as_notes(self):
        core.ops_system_edit(self.c, "site", sets={"infra.dependencies": "Stripe: https://status.stripe.com "
                                                                         "54.187.174.169"}, why="the vendor")
        for n in ("stripe", "pay", "search"):
            self.check(n, command=n, **({"layer": "dependency"} if n == "stripe" else {}))
        [d] = self.fault("stripe", out="STRIPE CRITICAL - 503")
        it = self.item(d["item"])
        self.assertEqual(d["kind"], "dependency")
        self.assertTrue(it["blocked_reason"].startswith("outside dependency: waits on the vendor until site/stripe"))
        [p] = self.fault("pay", out="PAY CRITICAL - connect to 54.187.174.169 timed out")  # a word of its line
        self.assertEqual((p["action"], p["item"], p["kind"]), ("cause-note", d["item"], "dependency"))
        self.assertTrue(self.c.execute("SELECT 1 FROM events WHERE item_id=? AND change LIKE 'ops ops:site:pay in fault "
                                       "too at %'", (d["item"],)).fetchone())
        [s] = self.fault("search", out="SEARCH CRITICAL - the index is missing")  # it does not name the vendor
        self.assertEqual(s["action"], "opened")
        self.assertNotEqual(s["kind"], "dependency")
        # The vendor passes again: pay runs once at once; still in fault, it opens its own task.
        def runner(cmd, folder, limit, env):
            r = subprocess.CompletedProcess(cmd, 0 if cmd == "stripe" else 2,
                                            "STRIPE OK" if cmd == "stripe" else "PAY CRITICAL - declined", "")
            r.timed_out = False
            return r
        later = core.now() + timedelta(minutes=10)
        self.run_at("site", "stripe", later, runner)
        r = self.run_at("site", "stripe", later + timedelta(minutes=1), runner)
        self.assertIsNone(self.item(d["item"])["blocked_reason"])  # the vendor is back
        [after] = [x for x in r["items"] if x.get("action") == "run-after-dependency"]
        self.assertEqual((after["key"], after["status"], after["items"][0]["action"]), ("ops:site:pay", "critical",
                                                                                         "opened"))
        self.assertIn("PAY CRITICAL - declined", self.item(after["items"][0]["item"])["title"])

    def test_after_the_vendor_is_back_a_waiting_check_that_passes_opens_nothing(self):
        for n in ("stripe", "pay"):
            self.check(n, command=n, **({"layer": "dependency"} if n == "stripe" else {}))
        self.fault("stripe", out="STRIPE CRITICAL - 503")
        [p] = self.fault("pay", out="PAY CRITICAL - stripe answers 503")  # the check's name names it
        self.assertEqual(p["action"], "cause-note")
        later = core.now() + timedelta(minutes=10)
        self.run_at("site", "stripe", later, self.proc(0, "OK"))
        r = self.run_at("site", "stripe", later + timedelta(minutes=1), self.proc(0, "OK"))
        [after] = [x for x in r["items"] if x.get("action") == "run-after-dependency"]
        self.assertEqual((after["status"], after["items"]), ("ok", [{"key": "ops:site:pay", "item": None,
                                                                      "action": "healing"}]))
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM items WHERE kind='operational'").fetchone()[0], 1)

    def test_ops_deps_lists_the_dependencies_of_the_system_description_and_adds_a_check_for_each(self):
        core.ops_system_edit(self.c, "site", sets={"infra.dependencies": "Stripe: https://status.stripe.com "
                                                                         "api.stripe.com\nPostmark: "
                                                                         "https://status.postmarkapp.com\nan internal "
                                                                         "service with no page"}, why="vendors")
        r = core.ops_deps(self.c, "site")
        self.assertEqual([(d["name"], d["status"], d["check"]) for d in r["dependencies"]],
                         [("stripe", "https://status.stripe.com", None),
                          ("postmark", "https://status.postmarkapp.com", None),
                          ("an-internal-service-with-no-page", None, None)])
        self.assertEqual(r["dependencies"][0]["names"], ["api.stripe.com", "status.stripe.com", "stripe"])
        r = core.ops_deps(self.c, "site", add=True, actor="dev")
        self.assertEqual(r["added"], ["stripe", "postmark"])
        c = core.ops_find(self.c, "site", "stripe")
        self.assertEqual((c["form"], c["layer"], c["config"]["url"]), ("url", "dependency", "https://status.stripe.com"))
        self.assertEqual(core.ops_deps(self.c, "site", add=True, actor="dev")["added"], [])

    def test_rule_5_a_lower_layer_that_failed_first_gives_its_kind_and_its_item_the_notes(self):
        self.check("disk", layer="capacity")
        self.check("restart", layer="service", repair="systemctl restart app")
        self.check("app")
        [d] = self.fault("disk", out="DISK CRITICAL - 99% used")
        self.assertEqual(d["kind"], "capacity")
        p = self.person(d["item"])  # no repair: the person's step first, and the fault's task waits on it
        self.assertTrue(p["title"].startswith("Capacity at site/disk: DISK CRITICAL"), p["title"])
        self.assertEqual(core.item_show(self.c, d["item"])["ready"], False)
        [s] = self.fault("restart", out="APP CRITICAL - stopped", at=core.now() + timedelta(minutes=1))
        self.assertEqual((s["kind"], self.person(s["item"])), ("service-down", None))  # its repair is set
        [a] = self.fault("app", out="APP CRITICAL - 500s", at=core.now() + timedelta(minutes=2))
        self.assertEqual((a["action"], a["item"], a["kind"]), ("cause-note", d["item"], "capacity"))

    def test_rule_6_a_known_message_shipped_or_the_projects_own_gives_the_kind(self):
        self.check("job")
        [x] = self.fault("job", out="JOB CRITICAL\nOSError: [Errno 28] No space left on device")
        self.assertEqual(x["kind"], "capacity")
        self.assertIn("rule 6: the output names a known message: 'No space left on device'", self.item(x["item"])["context"])
        core.ops_system_edit(self.c, "site", {"rules.known_messages": "expired item: token revoked\nnot a kind: x\n"
                                              "data: /workspace \\d+ is broken/"}, why="seen", actor="mark")
        self.check("api")
        [x] = self.fault("api", out="API CRITICAL - token revoked by vendor")
        self.assertEqual(x["kind"], "expired")
        self.assertEqual(self.person(x["item"])["doer"], "human")
        self.check("ws")
        [x] = self.fault("ws", out="WS CRITICAL - workspace 42 is broken")
        self.assertEqual(x["kind"], "data")

    def test_rule_8_unknown_starts_a_short_sorting_session_that_gives_the_kind_and_adds_the_rule(self):
        self.check("job")
        [x] = self.fault("job", out="JOB CRITICAL - KeyError: 'plan'")
        it = self.item(x["item"])
        self.assertEqual((x["kind"], it["model"], it["effort"]), ("unknown", "sonnet", "low"))
        self.assertIn(f"maxpm ops kind {x['item']} <kind> --why", it["context"])
        with self.assertRaisesRegex(RiverError, "only the session that holds"):
            core.ops_kind_set(self.c, x["item"], "code defect", "a stack trace", actor="dev")
        core.claim(self.c, x["item"], "dev")
        with self.assertRaisesRegex(RiverError, "no kind 'bug'"):
            core.ops_kind_set(self.c, x["item"], "bug", "x", actor="dev")
        with self.assertRaisesRegex(RiverError, "maxpm ops noise add"):
            core.ops_kind_set(self.c, x["item"], "noise", "x", actor="dev")
        r = core.ops_kind_set(self.c, x["item"], "code defect", "a KeyError with a stack trace",
                              message="KeyError: 'plan'", actor="dev")
        self.assertEqual((r["kind"], r["doer"], r["person"], r["released"], r["known"]),
                         ("code-defect", "ai", None, True, {"line": "code-defect: KeyError: 'plan'", "state": "proposed"}))
        it = self.item(x["item"])
        self.assertEqual((it["status"], it["model"], it["effort"]), ("open", None, None))
        core.ops_system_accept(self.c, "site", actor="boss")
        self.check("job2")
        self.assertEqual(self.fault("job2", out="JOB CRITICAL - KeyError: 'plan'")[0]["kind"], "code-defect")

    def test_the_cli_lists_the_kinds_and_a_check_takes_a_layer(self):
        from river import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(["-q", "ops", "kinds"])
        self.assertIn("  service-down: a process stopped", out.getvalue())
        with self.assertRaisesRegex(RiverError, "no layer 'disk'"):
            core.ops_add(self.c, "site", "d", "url", {"url": "https://x.test/", "layer": "disk"}, actor="dev")
        core.ops_add(self.c, "site", "d", "url", {"url": "https://x.test/", "layer": "dependency"}, actor="dev")
        self.assertEqual(core.ops_profile_export(self.c, "site")["checks"][0]["layer"], "dependency")
        core.ops_edit(self.c, "site", "d", {"severity": "note"}, actor="dev")
        self.assertEqual(core.ops_find(self.c, "site", "d")["layer"], "dependency")  # an edit keeps it


class OpsKindRelease(ReleaseBase):
    """Rule 7 of the kinds (#5509): a fault that starts within the release window after a release, and did not occur
    in the 30 days before it, is a release regression."""

    def test_a_new_fault_soon_after_a_release_is_a_release_regression_and_an_old_one_is_not(self):
        core.ops_add(self.c, "site", "home", "url", {"url": "https://example.test/", "fail_after": 1}, actor="dev")
        core.ops_add(self.c, "site", "api", "url", {"url": "https://example.test/api", "fail_after": 1}, actor="dev")
        t0 = core.now() - timedelta(hours=1)
        self.run_at("api", t0, 500)  # api failed before the release
        self.run_at("api", t0 + timedelta(minutes=1), 200)
        self.run_at("home", t0, 200)
        released = self.release()
        r = self.run_at("home", released + timedelta(minutes=5), 500)
        [x] = [i for i in r["items"] if i["key"] == "ops:site:home"]
        self.assertEqual(x["kind"], "release-regression")
        self.assertIn(f"after release #{self.deploy} to web", core._item(self.c, x["item"])["context"])
        r = self.run_at("api", released + timedelta(minutes=5), 500)
        self.assertEqual(r["items"][0]["kind"], "unknown")
        core.config_set(self.c, "ops_release_window", "1m")
        core.ops_add(self.c, "site", "late", "url", {"url": "https://example.test/late", "fail_after": 1}, actor="dev")
        r = self.run_at("late", released + timedelta(minutes=5), 500)
        self.assertEqual(r["items"][0]["kind"], "unknown")


    def test_a_release_regression_names_the_last_good_release_and_the_rollback_and_waits_on_a_person(self):
        core.register(self.c, "boss")
        self.c.execute("UPDATE agents SET role='manager' WHERE name='boss'")
        self.c.commit()
        with self.assertRaisesRegex(RiverError, "only a person sets the rollback command"):
            core.ops_system_edit(self.c, "site", sets={"infra.rollback": "deploy.sh --to <rev>"}, actor="boss")
        core.ops_system_edit(self.c, "site", sets={"infra.rollback": "deploy.sh --to <rev>"}, actor="dev",
                             why="the rollback")  # an agent proposes it
        with self.assertRaisesRegex(RiverError, "only a person sets the rollback command"):
            core.ops_system_accept(self.c, "site", actor="boss")
        core.ops_system_accept(self.c, "site")  # a person
        first = self.release()
        core.ops_add(self.c, "site", "home", "url", {"url": "https://example.test/", "fail_after": 1}, actor="dev")
        w = self.add("site", "new page")
        core.claim(self.c, w, "dev")
        core.done(self.c, w, "def", "dev", ship_it=True)
        second = self.c.execute("SELECT id FROM items WHERE kind='deploy' AND status='open'").fetchone()["id"]
        self.run_at("home", first + timedelta(minutes=1), 200)
        core.claim(self.c, second, "ops")
        with mock.patch.object(core, "now", lambda: first + timedelta(minutes=30)):
            core.done(self.c, second, "v2 out", "ops")
        r = self.run_at("home", first + timedelta(minutes=35), 500)
        [x] = [i for i in r["items"] if i["key"] == "ops:site:home"]
        it = core.item_show(self.c, x["item"])
        self.assertEqual(x["kind"], "release-regression")
        self.assertTrue(it["context"].startswith(f"ROLLBACK FIRST: the fault came right after release #{second} to web. "
                                                 f"The last good release: release #{self.deploy}"), it["context"])
        self.assertIn("deploy.sh --to <rev>", it["context"])
        [p] = [core._item(self.c, i) for i in it["waits_on"]]
        self.assertEqual((p["doer"], p["priority"]), ("human", 0))
        self.assertTrue(p["title"].startswith(f"Roll back web to release #{self.deploy}: HTTP 500"), p["title"])
        self.assertIn("deploy.sh --to <rev>", p["context"])


class OpsOutside(Base):
    """The watchdog (#5441): checks marked outside run in maxpm ops check --outside, with no maxpm serve; it repairs
    a fault at most ops_outside_repair_max times in ops_outside_repair_window, then sends a notice itself."""

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "maxpm")
        core.register(self.c, "mark", human=True)
        self.calls = []
        self.check_code = 2

        def runner(cmd, folder, limit, env):
            self.calls.append(cmd)
            code = 0 if cmd.startswith("restart") else self.check_code
            r = subprocess.CompletedProcess(cmd, code, "SERVE CRITICAL - no answer" if code else "SERVE OK", "")
            r.timed_out = False
            return r
        p = mock.patch.object(core, "OPS_RUNNER", runner)
        p.start()
        self.addCleanup(p.stop)
        self.sent = []
        self.channel_ok = True

    def notice(self, title, body):
        self.sent.append((title, body))
        return [{"channel": "ntfy", "ok": self.channel_ok, "error": None if self.channel_ok else "down"}]

    def add(self, **kw):
        fields = {"command": "check_serve", "every": "1m", "timeout": "10s", "fail_after": 2, "outside": True,
                  "repair": "restart serve"}
        fields.update(kw)
        return core.ops_add(self.c, "maxpm", "serve", "plugin", fields, actor="mark")["check"]

    def test_an_outside_check_runs_only_in_the_watchdog_and_keeps_its_mark_in_the_profile(self):
        c = self.add()
        self.assertTrue(c["outside"])
        self.assertIn("outside serve (the watchdog runs it)", core.ops_list(self.c, "maxpm")["checks"][0]["summary"])
        self.assertEqual(core.ops_due(self.c), [])  # maxpm serve skips it
        self.assertEqual([x["name"] for x in core.ops_due(self.c, outside=True)], ["serve"])
        prof = core.ops_profile_export(self.c, "maxpm")
        self.assertTrue(prof["checks"][0]["outside"])
        self.assertEqual(core.ops_profile_apply(self.c, prof, yes=True, actor="mark")["same"], ["serve"])
        prof["checks"][0].pop("outside")
        self.assertEqual(core.ops_profile_apply(self.c, prof, actor="mark")["change"][0]["fields"], ["outside"])
        core.ops_edit(self.c, "maxpm", "serve", {"outside": False}, actor="mark")
        self.assertEqual([x["name"] for x in core.ops_due(self.c)], ["serve"])
        self.assertEqual(core.ops_due(self.c, outside=True), [])

    def test_a_serve_stopped_on_purpose_runs_none_of_the_checks_that_need_serve(self):
        # #5649: the person quit the desktop app and no login agent runs serve: maxpm's own probes do not run, so
        # no repair and no notice; a check of the person's own still runs.
        self.check_code = 0
        self.add(command="maxpm ops probe serve")
        core.ops_add(self.c, "maxpm", "site", "plugin", {"command": "check_site", "every": "1m", "timeout": "10s",
                                                         "outside": True}, actor="mark")
        t = core.now()
        for i in range(4):
            res = core.ops_outside(self.c, self.notice, t + timedelta(minutes=i), serve_wanted=False)
            self.assertEqual([r["check"] for r in res], ["maxpm/site"])
        self.assertEqual(self.calls, ["check_site"] * 4)
        # The app opens again: the serve check is due at once.
        res = core.ops_outside(self.c, self.notice, t + timedelta(minutes=4))
        self.assertIn("maxpm/serve", [r["check"] for r in res])

    def test_a_run_one_second_early_is_due_in_the_watchdog(self):
        self.add()
        t = core.now()
        core.ops_outside(self.c, self.notice, t)
        self.assertEqual(len(core.ops_outside(self.c, self.notice, t + timedelta(seconds=59))), 1)
        self.assertEqual(core.ops_outside(self.c, self.notice, t + timedelta(seconds=70)), [])  # the last ran at 59 s

    def test_three_repairs_in_the_window_then_one_notice_then_one_when_it_works_again(self):
        self.add()
        t = core.now()

        def at(i):
            return core.ops_outside(self.c, self.notice, t + timedelta(minutes=i))[0]
        r = at(0)
        self.assertEqual((r["changes"][0]["to"], "repair" in r), ("failing", False))
        reps = [at(i).get("repair", {}).get("n") for i in (1, 2, 3)]
        self.assertEqual(reps, [1, 2, 3])
        self.assertEqual(self.calls.count("restart serve"), 3)
        self.assertEqual(self.sent, [])  # the watchdog repairs before it asks a person
        r = at(4)
        self.assertEqual(r["repair_limit"], 3)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], "MaximizePM: maxpm/serve is down")
        self.assertIn("ran its repair 3 times in 30m (ops_outside_repair_max 3) and stopped", self.sent[0][1])
        item = [x for x in core.item_list(self.c, project="maxpm") if x["title"].startswith("ops:maxpm:serve ")][0]
        self.assertIn(f"Item #{item['id']}", self.sent[0][1])
        at(5)
        self.assertEqual(len(self.sent), 1)  # one notice for one fault
        self.assertEqual(self.calls.count("restart serve"), 3)
        notes = [r[0] for r in self.c.execute("SELECT change FROM events WHERE item_id=? ORDER BY id", (item["id"],))]
        self.assertEqual(len([n for n in notes if n.startswith("ops repair started: restart serve (the watchdog")]), 3)
        self.check_code = 0
        at(6)
        self.assertEqual(len(self.sent), 1)  # one pass is not a recovery (recover_after 2)
        at(7)
        self.assertEqual(self.sent[1][0], "MaximizePM: maxpm/serve works again")
        at(8)
        self.assertEqual(len(self.sent), 2)
        states = [r[0] for r in self.c.execute("SELECT state FROM ops_events ORDER BY id")]
        self.assertEqual([s for s in states if s in ("fault", "repair", "paged", "page-ended", "recovered")],
                         ["fault", "repair", "repair", "repair", "paged", "recovered", "page-ended"])
        # After the window, the watchdog repairs again.
        self.check_code = 2
        at(40)
        r = at(41)
        self.assertEqual(r["repair"]["n"], 1)

    def test_a_fault_with_no_repair_sends_its_notice_at_once_and_again_when_no_channel_took_it(self):
        self.add(repair="")
        t = core.now()
        self.channel_ok = False
        core.ops_outside(self.c, self.notice, t)
        r = core.ops_outside(self.c, self.notice, t + timedelta(minutes=1))[0]
        self.assertIn("it has no repair command", r["notice"]["body"])
        core.ops_outside(self.c, self.notice, t + timedelta(minutes=2))
        self.assertEqual(len(self.sent), 2)  # not recorded: tried again
        self.channel_ok = True
        core.ops_outside(self.c, self.notice, t + timedelta(minutes=3))
        core.ops_outside(self.c, self.notice, t + timedelta(minutes=4))
        self.assertEqual(len(self.sent), 3)

    def test_a_check_of_severity_note_sends_no_notice(self):
        self.add(repair="", severity="note")
        t = core.now()
        for i in range(4):
            core.ops_outside(self.c, self.notice, t + timedelta(minutes=i))
        self.assertEqual(self.sent, [])

    def test_maxpm_ops_check_outside_sends_on_the_channels_of_the_queue(self):
        from river import cli
        self.add(repair="", fail_after=1)
        core.config_set(self.c, "notify_channels", "log")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.run(["ops", "check", "--outside"]), 0)
        self.assertIn("maxpm/serve: critical; fault; notice log sent", out.getvalue())
        log = os.path.join(self.dir.name, "notifications.log")
        with open(log, encoding="utf-8") as f:
            self.assertEqual(json.loads(f.readline())["title"], "MaximizePM: maxpm/serve is down")


class OpsCron(Base):
    """Cron schedules (#5506): a check may run on a cron expression in its project's time zone, and after a stop each
    check runs once, never once for each time that passed; the run records how many times were due."""

    run_at = Ops.run_at

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "site")
        core.register(self.c, "dev")
        self.ok = lambda url, limit: (200, "")

    def t(self, hhmm, day=12):
        return datetime(2026, 11, day, int(hhmm[:2]), int(hhmm[3:]), tzinfo=timezone.utc)

    def test_the_parser_reads_the_five_fields_and_refuses_a_bad_one(self):
        (mins, hours, dom, mon, dow), dom_star, dow_star = core.cron_parse("*/15 9-17 * jan,dec mon-fri")
        self.assertEqual((sorted(mins), sorted(hours), len(dom), sorted(mon), sorted(dow), dom_star, dow_star),
                         ([0, 15, 30, 45], list(range(9, 18)), 31, [1, 12], [1, 2, 3, 4, 5], True, False))
        self.assertEqual(core.cron_parse("0 0 * * 7")[0][4], {0})  # 7 is Sunday too
        self.assertEqual(sorted(core.cron_parse("5/20 * * * *")[0][0]), [5, 25, 45])
        for bad, why in (("0 3 * *", "five fields"), ("60 * * * *", "bad minute '60'"), ("* * * * fri-mon", "larger"),
                         ("*/0 * * * *", "bad step"), ("0 3 * foo *", "bad month 'foo'")):
            with self.assertRaisesRegex(RiverError, why):
                core.cron_parse(bad)

    def test_the_last_time_and_the_count_in_utc_and_in_a_zone(self):
        utc = timezone.utc
        p = core.cron_parse
        self.assertEqual(core.cron_prev(p("0 3 * * *"), self.t("10:07"), utc), self.t("03:00"))
        self.assertEqual(core.cron_prev(p("*/15 * * * *"), self.t("10:07"), utc), self.t("10:00"))
        self.assertEqual(core.cron_prev(p("0 3 * * *"), self.t("02:59"), utc), self.t("03:00", 11))
        # Both day fields restricted: either one is enough (Thursday 2026-11-12 is not the 1st).
        self.assertEqual(core.cron_prev(p("0 0 1 * thu"), self.t("10:00"), utc), self.t("00:00"))
        la = core.local_zone("America/Los_Angeles")
        self.assertEqual(core.cron_prev(p("0 3 * * *"), self.t("12:00"), la), self.t("11:00"))  # 03:00 PST
        self.assertEqual(core.cron_count(p("0 */4 * * *"), self.t("00:30"), self.t("12:30"), utc), 3)
        self.assertEqual(core.cron_count(p("0 */4 * * *"), self.t("04:00"), self.t("04:00"), utc), 0)

    def test_a_cron_check_is_due_at_its_times_and_after_a_stop_runs_once_with_the_gap(self):
        core.config_set(self.c, "timezone", "UTC", project="site")
        with self.assertRaisesRegex(RiverError, "five fields"):
            core.ops_add(self.c, "site", "bad", "url", {"url": "https://x.test/", "cron": "daily"}, actor="dev")
        core.ops_add(self.c, "site", "home", "url", {"url": "https://x.test/", "cron": "0 */4 * * *",
                                                    "timeout": "10m", "every": "1m"}, actor="dev")
        with core.tx(self.c):
            self.c.execute("UPDATE checks SET created_at=? WHERE name='home'", (core.iso(self.t("00:30")),))
        due = lambda at: [c["name"] for c in core.ops_due(self.c, at)]
        self.assertEqual(due(self.t("03:59")), [])  # its first time is 04:00, not when it was added
        self.assertEqual(due(self.t("04:01")), ["home"])
        self.run_at("site", "home", self.t("04:01"), http=self.ok)
        self.assertEqual(due(self.t("07:59")), [])
        # MaximizePM was off from 04:01 to 16:30: 08:00, 12:00 and 16:00 passed; it runs once, and the run says so.
        [c] = core.ops_due(self.c, self.t("16:30"))
        self.assertEqual(c["missed"], {"count": 3, "since": "2026-11-12T04:01:00Z", "until": "2026-11-12T16:30:00Z"})
        with mock.patch.object(core, "OPS_HTTP", self.ok):  # as maxpm serve does: the due check starts its run
            core.ops_finish(self.c, core.ops_start(self.c, c, at=self.t("16:30")), at=self.t("16:30"))
        self.assertEqual(due(self.t("16:31")), [])
        run = core.ops_history(self.c, "site", "home", 1)["runs"][0]
        self.assertEqual(json.loads(run["missed"])["count"], 3)
        self.assertTrue(self.c.execute("SELECT 1 FROM events WHERE change LIKE 'ops check site/home: 3 times were due "
                                       "between % and %, when nothing ran it; it runs once now'").fetchone())
        self.assertEqual(core.ops_profile_export(self.c, "site")["checks"][0]["cron"], "0 */4 * * *")
        from river import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(["-q", "ops", "history", "site", "home"])
        self.assertIn("after a stop: 3 times were due between", out.getvalue())
        core.ops_edit(self.c, "site", "home", {"cron": "", "every": "15m"}, actor="dev")  # empty: every again
        self.assertIsNone(core.ops_find(self.c, "site", "home")["cron"])

    def test_an_every_check_after_a_stop_runs_once_and_records_the_times_that_passed(self):
        core.ops_add(self.c, "site", "home", "url", {"url": "https://x.test/", "every": "1h"}, actor="dev")
        self.run_at("site", "home", self.t("00:00"), http=self.ok)
        [c] = core.ops_due(self.c, self.t("01:00"))
        self.assertNotIn("missed", c)  # on time: no gap
        [c] = core.ops_due(self.c, self.t("12:10"))
        self.assertEqual(c["missed"]["count"], 12)


class Speed(Base):
    """The shortcuts of #5491 give the same answers as the long way."""

    def test_the_parser_of_one_command_reads_as_the_whole_one(self):
        from river import cli
        for words in (["show", "3", "--brief"], ["--as", "ag", "release", "4", "--note", "x"], ["--json", "next", "-n", "2"],
                      ["project", "add", "web", "--rank", "2"], ["list", "--project", "web"]):
            self.assertEqual(vars(cli.parse_args(words)), vars(cli.build_parser().parse_args(words)), words)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            cli.parse_args(["show", "--bogus", "3"])  # a mistake: the whole parser says what is wrong
        self.assertIn("unrecognized arguments: --bogus", err.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit):
            cli.parse_args(["show", "-h"])
        self.assertIn("usage: maxpm show", out.getvalue())

    def test_a_search_finds_a_word_with_an_underscore_and_a_stem(self):
        core.project_add(self.c, "web")
        a = self.add("web", "Fix page_state answers")
        b = self.add("web", "Tests for the pagexstate answer")  # an underscore is no wildcard
        core.item_edit(self.c, b, notes="it answered", actor="t")
        self.assertEqual([m["id"] for m in core.search(self.c, "page_state")["items"]], [a])
        self.assertEqual(sorted(m["id"] for m in core.search(self.c, "answer")["items"]), [a, b])
        self.assertEqual(core.search(self.c, "zzz nothing here")["items"], [])


class GraphVersion(Base):
    """graph_version grows with each change of the item graph, not with a renewal (#5521)."""

    def test_a_renewal_leaves_it_and_a_link_raises_it(self):
        core.project_add(self.c, "web")
        a, b = self.add("web", "First"), self.add("web", "Second")
        core.register(self.c, "ag")
        core.claim(self.c, a, "ag")
        n = core.graph_version(self.c)
        core.activity(self.c, "ag")  # renews the lease of #a and records that ag was seen
        self.assertEqual(core.graph_version(self.c), n)
        core.dep_add(self.c, b, [a], actor="ag")
        self.assertGreater(core.graph_version(self.c), n)
        n = core.graph_version(self.c)
        core.config_set(self.c, "timezone", "Europe/Paris")
        self.assertGreater(core.graph_version(self.c), n)

    def test_a_cached_result_shows_the_lease_as_it_is_now(self):
        core.project_add(self.c, "web")
        a = self.add("web", "First")
        core.register(self.c, "ag")
        core.claim(self.c, a, "ag")
        tok = core.ANN_SHARE.set({})
        try:
            before = core.annotate(self.c, closed=False)[a]["lease_expires_at"]
            self.c.execute("UPDATE items SET lease_expires_at='2099-01-01T00:00:00Z' WHERE id=?", (a,))
            self.assertNotEqual(before, "2099-01-01T00:00:00Z")
            self.assertEqual(core.annotate(self.c, closed=False)[a]["lease_expires_at"], "2099-01-01T00:00:00Z")
        finally:
            core.ANN_SHARE.reset(tok)

    def test_a_rollback_forgets_the_cached_graph(self):
        core.project_add(self.c, "web")
        a = self.add("web", "First")
        tok = core.ANN_SHARE.set({})
        try:
            with self.assertRaises(ZeroDivisionError):
                with core.tx(self.c):
                    self.c.execute("UPDATE items SET title='Changed' WHERE id=?", (a,))
                    self.assertEqual(core.annotate(self.c)[a]["title"], "Changed")
                    1 / 0
            self.assertEqual(core.annotate(self.c)[a]["title"], "First")
        finally:
            core.ANN_SHARE.reset(tok)

    def test_a_kept_graph_reads_again_only_the_changed_items(self):
        """#5577: inside a command or a runner, the next _load_graph reads only the items of graph_changes since the
        last one, also after another session's writes, and gives the graph that a whole read gives."""
        core.project_add(self.c, "web")
        a, b = self.add("web", "First"), self.add("web", "Second")
        c = self.add("web", "Third", after=[a])
        done = self.add("web", "Old")
        core.done(self.c, done, actor="t", output="x")
        other = core.connect(self.path)
        self.addCleanup(other.close)
        tok = core.ANN_SHARE.set({})
        self.addCleanup(core.ANN_SHARE.reset, tok)

        def same(closed=False):
            got = core._load_graph(self.c, closed)
            whole = core._graph_read(self.c, closed)
            self.assertEqual(got[1], whole["items"])
            self.assertEqual([{i: sorted(v) for i, v in d.items()} for d in got[2:]],
                             [{i: sorted(v) for i, v in d.items()} for d in whole["lists"]])
            return got
        first = same()
        self.assertEqual(first[1][done], {"id": done, "project_id": first[1][a]["project_id"], "status": "done",
                                          "priority": 2, "due": None, "backlog_at": None, "assignee": None})
        self.assertIs(core._load_graph(self.c, False)[1], first[1])  # nothing changed: the same graph
        core.dep_add(other, b, [a], kind="feeds")
        core.dep_add(other, c, [b], kind="conflicts")
        core.item_prio(other, c, 0)
        d = core.item_add(other, "web", "Fourth", after=[b])["id"]
        got = same()
        self.assertIs(got[1][done], first[1][done])  # an item that did not change: its row was not read again
        self.assertIsNot(got[1][c], first[1][c])
        self.assertEqual(got[1][c]["priority"], 0)
        core.dep_remove(other, c, [a])
        core.done(other, b, actor="t", output="x", force="test")
        self.assertEqual(same()[1][b]["status"], "done")
        self.assertNotIn("title", same()[1][b])  # closed: LIGHT_COLUMNS only
        self.assertEqual(same(closed=True)[1][b]["title"], "Second")
        with core.tx(other):  # a removed item and its links
            e = other.execute("INSERT INTO items (project_id, title, rank, created_at) SELECT project_id, 'Gone', 0, "
                              "created_at FROM items WHERE id=?", (a,)).lastrowid
            other.execute("INSERT INTO deps (item_id, blocked_by) VALUES (?, ?)", (d, e))
        self.assertEqual(same()[2][d], [b, e])
        with core.tx(other):
            other.execute("DELETE FROM deps WHERE blocked_by=?", (e,))
            other.execute("DELETE FROM items WHERE id=?", (e,))
        self.assertNotIn(e, same()[1])
        # activity keeps the last GRAPH_LOG_KEEP rows; a kept graph that is older reads the whole graph again.
        with mock.patch.object(core, "GRAPH_LOG_KEEP", 1):
            core.item_prio(other, a, 1)
            core.item_prio(other, a, 3)
            core.activity(other, "t")
            self.assertEqual(other.execute("SELECT count(*) FROM graph_changes").fetchone()[0], 1)
            self.assertEqual(same()[1][a]["priority"], 3)


class Schedules(Base):
    """Scheduled tasks (#5507): a repeating obligation adds its item at each due time, less its lead time; times that
    passed while serve did not run add one item, due at the newest of them."""

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "office")
        core.config_set(self.c, "timezone", "UTC")

    def items(self):
        return self.c.execute("SELECT id, title, doer, due, context FROM items ORDER BY id").fetchall()

    def test_a_yearly_obligation_adds_its_item_lead_before_its_due_date_once(self):
        s = core.schedule_add(self.c, "office", "File the annual report", yearly="03-15", lead="30d", doer="human",
                              context="The state's form; the numbers are in the books")["schedule"]
        self.assertEqual(s["cron"], "59 23 15 3 *")
        due = core.parse_iso(s["next_due"])
        self.assertEqual((due.month, due.day, due.hour, due.minute), (3, 15, 23, 59))
        self.assertEqual(core.parse_iso(s["next_add"]), due - timedelta(days=30))
        self.assertEqual(core.schedule_due(self.c, due - timedelta(days=30, minutes=1)), [])
        added = core.schedule_due(self.c, due - timedelta(days=29))
        self.assertEqual([(x["schedule"], x["due"], x["missed"]) for x in added], [(s["id"], s["next_due"], 0)])
        it = self.items()[-1]
        self.assertEqual((it["title"], it["doer"], it["due"]), ("File the annual report", "human", s["next_due"]))
        self.assertIn("The state's form", it["context"])
        self.assertIn(f"From schedule s{s['id']}", it["context"])
        self.assertEqual(core.schedule_due(self.c, due - timedelta(days=28)), [])  # once for each due time
        nxt = core.schedule_list(self.c)["schedules"][0]
        self.assertEqual((core.parse_iso(nxt["next_due"]).year, nxt["last_item"]), (due.year + 1, it["id"]))

    def test_due_times_that_passed_while_serve_did_not_run_add_one_item_due_at_the_newest(self):
        s = core.schedule_add(self.c, "office", "Check the backups", cron="0 9 * * *")["schedule"]
        first = core.parse_iso(s["next_due"])
        added = core.schedule_due(self.c, first + timedelta(days=3, hours=1))
        self.assertEqual([(x["due"], x["missed"]) for x in added], [(core.iso(first + timedelta(days=3)), 4)])
        self.assertEqual(len(self.items()), 1)
        self.assertIn("4 due times passed while maxpm serve did not run", self.items()[0]["context"])
        self.assertEqual(core.schedule_due(self.c, first + timedelta(days=3, hours=2)), [])
        self.assertEqual(len(core.schedule_due(self.c, first + timedelta(days=4))), 1)  # the next day, on time

    def test_a_schedule_needs_one_time_and_rm_keeps_its_items(self):
        for kw in ({}, {"cron": "0 9 * * *", "yearly": "03-15"}):
            with self.assertRaisesRegex(RiverError, "--cron .* or --yearly"):
                core.schedule_add(self.c, "office", "x", **kw)
        with self.assertRaisesRegex(RiverError, "month 2 has no day 30"):
            core.schedule_add(self.c, "office", "x", yearly="02-30")
        s = core.schedule_add(self.c, "office", "Renew the domain", cron="0 9 * * *")["schedule"]
        core.schedule_due(self.c, core.parse_iso(s["next_due"]))
        self.assertEqual(core.schedule_rm(self.c, f"s{s['id']}")["changed"], "removed")
        self.assertEqual((core.schedule_list(self.c)["schedules"], len(self.items())), ([], 1))
        with self.assertRaisesRegex(RiverError, "no schedule"):
            core.schedule_rm(self.c, s["id"])


class BuildMemo(Base):
    """#5524: inside a page state build (BUILD_MEMO) each agent's status is read once for all agents; the answer is
    the one agent_status gives outside a build, and a caller's change to it stays its own."""

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "a")

    def test_an_agent_status_in_a_build_is_the_same_as_outside(self):
        x = self.add("a", "x")
        core.register(self.c, "ag")
        core.register(self.c, "mark", human=True)
        core.claim(self.c, x, "ag")
        core.session_pin(self.c, "ag", actor="mark")
        outside = {n: core.agent_status(self.c, n) for n in ("ag", "mark")}
        tok = core.BUILD_MEMO.set({})
        try:
            inside = {n: core.agent_status(self.c, n) for n in ("ag", "mark")}
            self.assertEqual(inside, outside)
            self.assertEqual([h["id"] for h in inside["ag"]["holds"]], [x])
            self.assertEqual(inside["ag"]["pinned_by"], ["mark"])
            inside["ag"]["state"] = "changed"
            self.assertNotEqual(core.agent_status(self.c, "ag")["state"], "changed")
            with self.assertRaisesRegex(RiverError, "is not registered"):
                core.agent_status(self.c, "nobody")
        finally:
            core.BUILD_MEMO.reset(tok)

    def test_the_state_queues_are_the_queue_lists(self):
        x = self.add("a", "x")
        core.register(self.c, "ag")
        core.register(self.c, "mark", human=True)
        core.queue_add(self.c, "ag", x, actor="mark")
        core.queue_add(self.c, "ag", message="do this", actor="mark")
        self.assertEqual(core.state(self.c)["queues"], {"ag": core.queue_list(self.c, "ag")["entries"]})


class EvidencePack(Base):
    """The evidence pack of a new fault item (#5512): lower layers, error groups, the code location (a stack frame of
    the project, else the message found in the repository), the release before the first error with its commits that
    changed those files, and the items that touch them. No model."""

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
                              capture_output=True, text=True, check=True).stdout.strip()

    def commit(self, body, msg):
        with open(os.path.join(self.repo, "app", "pay.py"), "w", encoding="utf-8") as f:
            f.write(body)
        self.git("add", "app/pay.py")
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def release(self, rev, at):
        d = self.add("site", f"deploy {rev[:7]}")
        with core.tx(self.c):
            self.c.execute("UPDATE items SET kind='deploy', target='prod', status='done', closed_at=?, cut_rev=? "
                           "WHERE id=?", (core.iso(at), rev, d))
        return d

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.dir.name, "repo")
        os.makedirs(os.path.join(self.repo, "app"))
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        core.target_add(self.c, "prod", "the servers")
        core.project_add(self.c, "site", target="prod", path=self.repo)
        core.register(self.c, "boss")
        self.c.execute("UPDATE agents SET role='manager' WHERE name='boss'")
        self.c.commit()
        self.t = core.now()
        r1 = self.commit("def charge():\n    return 1\n", "first")
        self.release(r1, self.t - timedelta(hours=3))
        r2 = self.commit("def charge():\n    raise ValueError('could not reach the ledger service')\n", "call the ledger")
        self.r2 = self.release(r2, self.t - timedelta(hours=1))
        self.other = self.add("site", "Retry the ledger call")
        with core.tx(self.c):
            self.c.execute("UPDATE items SET touches='app/pay.py' WHERE id=?", (self.other,))
        core.ops_add(self.c, "site", "pay", "cmd", {"command": "check_pay", "fail_after": 1}, actor="boss")

    def fault(self, out):
        r = Ops.run_at(self, "site", "pay", self.t, Ops.proc(1, out))
        return core._item(self.c, r["items"][0]["item"])["context"]

    def test_a_python_traceback_gives_the_frame_the_release_its_commit_and_the_items_that_touch_the_file(self):
        ctx = self.fault('Traceback (most recent call last):\n  File "/srv/site/app/pay.py", line 2, in charge\n'
                         'ValueError: could not reach the ledger service 42')
        pack = ctx[ctx.index("EVIDENCE PACK"):]
        self.assertIn("1. Lower layers:\n  none in fault", pack)
        self.assertIn("2x  ValueError: could not reach the ledger service N", pack)  # the run and the one that confirms it
        self.assertIn("  app/pay.py:2 (python)", pack)
        self.assertIn(f"4. Release before the first error: #{self.r2} to prod", pack)
        self.assertIn("call the ledger", pack)
        self.assertNotIn("first", pack.split("its commits")[1].split("5.")[0])
        self.assertIn(f"#{self.other} [open] Retry the ledger call (touches app/pay.py)", pack)

    def test_with_no_frame_the_message_is_found_in_the_repository_at_the_commit_in_production(self):
        pack = self.fault("ERROR could not reach the ledger service (attempt 3)")
        self.assertIn("message found in the repository at", pack)
        self.assertIn("app/pay.py:2 holds 'could not reach the ledger service'", pack)
        self.assertIn(f"#{self.other} [open]", pack)

    def test_a_lower_layer_in_fault_comes_first(self):
        core.ops_add(self.c, "site", "db", "cmd", {"command": "check_db", "fail_after": 1, "layer": "service"},
                     actor="boss")
        Ops.run_at(self, "site", "db", self.t - timedelta(minutes=5), Ops.proc(2, "DB CRITICAL - down"))
        Ops.run_at(self, "site", "pay", self.t, Ops.proc(1, "ERROR something else"))  # its fault goes to db's item
        pack = core.ops_evidence_pack(self.c, core.ops_find(self.c, "site", "pay"), "ops:site:pay", self.t)
        self.assertIn("1. Lower layers:\n  service: site/db fault since", pack)

    def test_rust_and_javascript_frames_of_the_project_are_read(self):
        for f in ("src/api.rs", "web/app.js"):
            os.makedirs(os.path.dirname(os.path.join(self.repo, f)), exist_ok=True)
            open(os.path.join(self.repo, f), "w", encoding="utf-8").close()
        text = ("thread 'main' panicked at src/api.rs:41:9:\nindex out of bounds\n"
                "TypeError: x is undefined\n    at load (https://example.test/web/app.js:12:5)\n"
                "    at run (node:internal/main:3:1)")
        self.assertEqual(core._ops_frames(text, self.repo), [("src/api.rs", 41, "rust"), ("web/app.js", 12, "javascript")])


class Causes(Base):
    """The cause of a fault in a fixed form (#5513): a proof that MaximizePM runs before any fix, and a verdict from
    the fault's check after the action."""

    run_at, proc = Ops.run_at, staticmethod(Ops.proc)

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "site")
        for n in ("dev", "boss"):
            core.register(self.c, n)
        self.c.execute("UPDATE agents SET role='manager' WHERE name='boss'")
        self.c.commit()
        core.ops_add(self.c, "site", "disk", "cmd", {"command": "check_disk", "fail_after": 1}, actor="boss")
        core.ops_add(self.c, "site", "app", "cmd", {"command": "check_app", "fail_after": 1}, actor="boss")
        self.t = core.now()
        r = self.run_at("site", "app", self.t, self.proc(2, "ERROR: could not write the session: No space left"))
        self.fault = r["items"][0]["item"]

    def test_a_fix_waits_on_a_proof_that_passes_and_a_failed_proof_says_so(self):
        with self.assertRaisesRegex(RiverError, "its cause has no proof yet"):
            core.item_add(self.c, "site", "Clean the logs", found_during=self.fault, actor="dev")
        core.item_add(self.c, "site", "Grow the disk", doer="human", blocks=self.fault, actor="dev")  # a person's step
        core.item_add(self.c, "site", "Clean the logs", found_during=self.fault, actor="boss")  # the manager may
        with mock.patch.object(core, "OPS_RUNNER", self.proc(0, "/dev/disk1 41%")):
            r = core.ops_cause(self.c, self.fault, "the disk is full", "df -h /", "clean the logs",
                               expect="100%", actor="dev")
        self.assertEqual((r["passed"], r["expect"], r["check"]), (False, "text: 100%", "ops:site:app"))
        with self.assertRaisesRegex(RiverError, "no proof yet"):
            core.item_add(self.c, "site", "Rotate the logs", found_during=self.fault, actor="dev")
        with mock.patch.object(core, "OPS_RUNNER", self.proc(0, "/dev/disk1 100%")):
            r = core.ops_cause(self.c, self.fault, "the disk is full", "df -h /", "clean the logs", expect="100%",
                               actor="dev")
        self.assertTrue(r["passed"])
        core.item_add(self.c, "site", "Rotate the logs", found_during=self.fault, actor="dev")
        self.assertTrue(self.c.execute("SELECT 1 FROM events WHERE item_id=? AND change LIKE 'ops cause c% (proof "
                                       "passed): the disk is full%'", (self.fault,)).fetchone())

    def test_a_check_as_the_proof_runs_through_serve_and_expects_it_to_fail(self):
        def run_check(c):
            self.assertEqual(c["name"], "disk")
            return {"status": "critical", "summary": "DISK CRITICAL - 100% used"}
        r = core.ops_cause(self.c, self.fault, "the disk is full", "site/disk", "clean the logs", actor="dev",
                           run_check=run_check)
        self.assertEqual((r["passed"], r["expect"]), (True, "critical"))
        with self.assertRaisesRegex(RiverError, "runs in maxpm serve"):
            core.ops_cause(self.c, self.fault, "x", "ops:site:disk", "y", actor="dev")
        with self.assertRaisesRegex(RiverError, "--action is part of the fixed form"):
            core.ops_cause(self.c, self.fault, "x", "true", "", actor="dev")
        with self.assertRaisesRegex(RiverError, "no fault item"):
            core.ops_cause(self.c, core.item_add(self.c, "site", "plain", actor="dev")["id"], "x", "true", "y")

    def test_the_check_after_the_action_gives_the_verdict_and_a_failure_opens_the_item_again(self):
        core.ops_cause(self.c, self.fault, "the disk is full", "true", "clean the logs", actor="dev")
        core.claim(self.c, self.fault, "dev")
        core.done(self.c, self.fault, "cleaned the logs", "dev")
        r = self.run_at("site", "app", self.t + timedelta(minutes=10), self.proc(2, "ERROR: No space left"))
        self.assertEqual(r["verdicts"][0]["verdict"], "fail")
        it = core._item(self.c, self.fault)
        self.assertEqual(it["status"], "open")
        self.assertTrue(self.c.execute("SELECT 1 FROM events WHERE item_id=? AND change LIKE 'ops cause c%: the check "
                                       "ops:site:app still fails after the action%'", (self.fault,)).fetchone())
        core.ops_cause(self.c, self.fault, "the logs grow again", "true", "rotate the logs", actor="dev")
        core.claim(self.c, self.fault, "dev")
        core.done(self.c, self.fault, "rotated", "dev")
        r = self.run_at("site", "app", self.t + timedelta(minutes=20), self.proc(0, "OK"))
        self.assertEqual([v["verdict"] for v in r["verdicts"]], ["pass"])
        self.assertEqual(core._item(self.c, self.fault)["status"], "done")


class Changed(Base):
    """What changed, one plain line for a person (#5570): maxpm done --changed and maxpm edit --changed store it on the
    item; maxpm show and the item's API return it; --output stays the note for agents."""

    def test_done_and_edit_store_one_plain_line_and_refuse_ids_files_and_flags(self):
        from river import cli
        core.project_add(self.c, "p")
        core.register(self.c, "w")
        i = self.add("p", "Add a field outside to the checks")
        core.claim(self.c, i, "w")
        for bad, why in (("closes #12", "an id"), ("edits river/core.py", "a path"), ("adds --outside on", "a flag")):
            with self.assertRaisesRegex(RiverError, why):
                core.done(self.c, i, "8610692", "w", changed=bad)
        self.assertEqual(core._item(self.c, i)["status"], "in_progress")  # a refused line closes nothing
        r = core.done(self.c, i, "8610692 on local main", "w", changed="A check can now run outside the page server")
        self.assertEqual(r["changed"], "A check can now run outside the page server")
        self.assertEqual(core.item_show(self.c, i)["changed"], "A check can now run outside the page server")
        core.item_edit(self.c, i, changed="Checks can run while the page server is down", actor="w")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(["-q", "show", str(i)])
        self.assertIn("  changed: Checks can run while the page server is down", out.getvalue())
        j = self.add("p", "Another")
        core.claim(self.c, j, "w")
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            cli.run(["--as", "w", "done", str(j), "--output", "x"])
        self.assertIn(f"what changed, for a person: maxpm --as w edit {j} --changed", err.getvalue())


class WorkLine(Base):
    """GET /api/item/<id>/context (#5571, design #5560): the clicked task in the work of the session that owns it."""

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "a")
        for n in ("s1", "s2"):
            core.register(self.c, n)
        core.register(self.c, "mark", human=True)

    def test_the_line_of_a_session_done_now_and_next(self):
        x = core.item_add(self.c, "a", "build the cart for the shop", short="Build the cart")["id"]
        core.claim(self.c, x, "s1")
        y = core.item_add(self.c, "a", "fix the price rounding", actor="s1", found_during=x)["id"]
        core.done(self.c, x, output="abc123", actor="s1", changed="The cart works")
        z = core.item_add(self.c, "a", "ship the cart page", actor="s1")["id"]  # added while it held nothing
        w = self.add("a", "write the tests")
        core.claim(self.c, w, "s1")
        v = self.add("a", "a prerequisite")
        core.claim(self.c, v, "s2")
        core.dep_add(self.c, w, [v], actor="s1", mode="keep")
        u = self.add("a", "after the tests", after=[w])
        q = self.add("a", "queued work")
        core.queue_add(self.c, "s1", q, actor="mark")
        line = core.work_line(self.c, x)
        self.assertEqual((line["owner"], line["owner_is"], line["item"]["nobody_holds"]), ("s1", "did", False))
        self.assertEqual([(d["id"], d["outcome"], d["changed"]) for d in line["done"]], [(x, "done", "The cart works")])
        self.assertTrue(line["done"][0]["started"] <= line["done"][0]["ended"])
        self.assertEqual([(f["id"], f["status"]) for f in line["done"][0]["found"]], [(y, "open")])
        self.assertEqual([a["id"] for a in line["added"]], [z])
        self.assertEqual([(n["id"], [(o["id"], o["holder"]) for o in n["waits_on"]]) for n in line["now"]],
                         [(w, [(v, "s2")])])
        self.assertEqual([(n["id"], n["reason"]) for n in line["next"]],
                         [(q, "in its queue"), (y, "it added it; nobody holds it"),
                          (z, "it added it; nobody holds it"), (u, f"waits on its task #{w}")])
        self.assertEqual(line["people"]["s2"]["doing"], "a prerequisite")  # by what it does, not its name alone
        self.assertIsNone(line["goal"])
        self.assertEqual(line["item"]["ships"], [])
        # The task it holds gives the same line; a task it added that nobody holds says so.
        self.assertEqual(core.work_line(self.c, w)["owner_is"], "holds")
        self.assertEqual([d["id"] for d in core.work_line(self.c, w)["done"]], [x])
        added = core.work_line(self.c, y)
        self.assertEqual((added["owner"], added["owner_is"], added["item"]["nobody_holds"]), ("s1", "added", True))
        # A later registration of the same name starts a new line; the done task keeps the line it was done in.
        core.register(self.c, "s1")
        self.assertEqual([d["id"] for d in core.work_line(self.c, x)["done"]], [x])

    def test_an_ended_session_a_person_and_a_goal(self):
        core.goal_add(self.c, "a", "cart", outcome="people buy things", done_when="an order goes through")
        k = core.item_add(self.c, "a", "the order form", goals=["cart"])["id"]
        rest = core.item_add(self.c, "a", "the receipt", goals=["cart"])["id"]
        core.register(self.c, "s3")
        core.claim(self.c, k, "s3")
        core.done(self.c, k, output="x", actor="s3")
        core.unregister(self.c, "s3")  # an ended session has no agents row; its events stay
        line = core.work_line(self.c, k)
        self.assertEqual((line["owner"], [d["id"] for d in line["done"]]), ("s3", [k]))
        self.assertEqual(line["people"]["s3"]["state"], "ended")
        g = line["goal"]
        self.assertEqual((g["name"], g["outcome"], g["done_when"], g["done"], g["open"]),
                         ("cart", "people buy things", "an order goes through", 1, 1))
        self.assertEqual([t["id"] for t in g["open_tasks"]], [rest])
        self.assertEqual(g["sessions"], 1)  # s3 took a task of it
        m = core.item_add(self.c, "a", "sign the contract", doer="human", actor="mark")["id"]
        core.claim(self.c, m, "mark")
        person = core.work_line(self.c, m)
        self.assertEqual((person["owner"], person["done"], person["now"], person["next"]), ("mark", None, None, None))

    def test_the_line_reads_only_the_owners_events(self):
        # p147: the line reads the owner's events by the events_actor index, not the whole history.
        x = self.add("a", "x")
        core.claim(self.c, x, "s1")
        plan = " ".join(r[3] for r in self.c.execute(
            "EXPLAIN QUERY PLAN SELECT id, at, item_id, change FROM events WHERE actor=? AND id>=? AND id<? "
            "AND item_id IS NOT NULL ORDER BY id", ("s1", 0, 1 << 62)))
        self.assertIn("events_actor", plan)


class SessionSelf(Base):
    """#5576, mark 2026-11-12: 'this should not be allowed or possible'. A command records a session id, a session
    link or a process only for the agent whose session it runs in. The past cases: #5060 (the manager's session
    recorded for another agent), #5088 and #5576 (a worker whose agent had unregistered ran 'maxpm --as <manager>
    who', and the manager's chat on the Sessions tab showed the worker's ended chat)."""

    W1, MGR, GONE, NEW = ("11111111-2222-3333-4444-5555555555%02d" % i for i in range(1, 5))

    def setUp(self):
        super().setUp()
        for n in ("mgr", "w1", "w2"):
            core.register(self.c, n)
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET role='manager' WHERE name='mgr'")
        core.record_session(self.c, "mgr", self.MGR)
        core.record_session(self.c, "w1", self.W1)

    def run_cli(self, me, sid, *argv):
        from unittest import mock
        from river import cli
        env = {"MAXPM_DB": self.path, "MAXPM_AGENT": me, "CLAUDE_CODE_SESSION_ID": sid,
               "CLAUDE_CODE_BRIDGE_SESSION_ID": "session_01Abc"}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            try:
                return cli.run(list(argv))
            except RiverError as e:
                return str(e)

    def sessions(self, agent):
        return [r[0] for r in self.c.execute("SELECT session_id FROM agent_sessions WHERE agent=? ORDER BY rowid",
                                             (agent,))]

    def gone_worker(self, name="w9"):
        core.register(self.c, name)
        core.record_session(self.c, name, self.GONE)
        core.unregister(self.c, name)

    def test_5060_the_managers_session_stays_the_managers(self):
        self.assertEqual(self.run_cli("mgr", self.MGR, "--as", "w2", "note", "the manager acts for w2"), 0)
        self.assertEqual(core.agent_status(self.c, "w2")["note"], "the manager acts for w2")
        self.assertEqual(self.sessions("w2"), [])
        self.assertIsNone(self.c.execute("SELECT session_url FROM agents WHERE name='w2'").fetchone()[0])
        # With no MAXPM_AGENT the session id names the manager: the same.
        self.assertEqual(self.run_cli("", self.MGR, "--as", "w2", "note", "again"), 0)
        self.assertEqual(self.sessions("w2"), [])

    def test_5088_a_gone_workers_session_is_refused_as_another_agent(self):
        self.gone_worker()
        for actor in ("mgr", "w2"):  # w2 has no session yet: before, it took the worker's
            got = self.run_cli("", self.GONE, "--as", actor, "who")
            self.assertIn(f"this session is w9, and only the manager runs a command as another agent (--as {actor})",
                          got)
        self.assertEqual(self.sessions("mgr"), [self.MGR])
        self.assertEqual(self.sessions("w2"), [])

    def test_5576_a_gone_maxpm_agent_is_refused_as_another_agent(self):
        self.gone_worker("w4")
        got = self.run_cli("w4", self.GONE, "--as", "mgr", "who")
        self.assertIn("this session is w4", got)
        self.assertEqual(self.sessions("mgr"), [self.MGR])
        # The session of a gone agent may take a new name with a naming command (register, go).
        self.assertEqual(self.run_cli("w4", self.GONE, "register", "w5"), 0)
        self.assertEqual(self.sessions("w5"), [self.GONE])

    def test_a_live_workers_session_is_refused_and_records_nothing(self):
        got = self.run_cli("", self.W1, "--as", "w2", "note", "w1 acts for w2")
        self.assertIn("this session is w1", got)
        self.assertNotEqual(core.agent_status(self.c, "w2")["note"], "w1 acts for w2")
        self.assertEqual(self.sessions("w2"), [])

    def test_a_persons_terminal_acts_as_anyone_and_records_nothing(self):
        # A Claude Code chat of the person that no agent ran in, and a terminal with no session id.
        for sid in (self.NEW, ""):
            self.assertEqual(self.run_cli("", sid, "--as", "w2", "note", "the person acts for w2"), 0)
        self.assertEqual(self.sessions("w2"), [])

    def test_an_agents_own_new_session_id_is_recorded(self):
        # /clear gives the session a new id: the agent's own command records it, so its chat stays current.
        self.assertEqual(self.run_cli("w1", self.NEW, "--as", "w1", "note", "after /clear"), 0)
        self.assertEqual(self.sessions("w1"), [self.W1, self.NEW])
        self.assertEqual(self.c.execute("SELECT session_url FROM agents WHERE name='w1'").fetchone()[0],
                         "https://claude.ai/code/session_01Abc")

    def test_a_session_id_of_another_live_agent_is_not_taken(self):
        self.assertIs(core.record_session(self.c, "w2", self.W1), False)
        self.assertEqual(self.sessions("w2"), [])
        # MAXPM_AGENT names w2, and the session id is w1's: the command runs, and w1 keeps its chat.
        self.assertEqual(self.run_cli("w2", self.W1, "note", "mine"), 0)
        self.assertEqual(self.sessions("w2"), [])
        self.assertIs(core.record_session(self.c, "w2", self.W1, move=True), True)

    def test_session_move_by_a_person_or_the_manager(self):
        with core.tx(self.c):  # the wrong row that #5576 made
            self.c.execute("INSERT INTO agent_sessions (agent, session_id, first_seen) VALUES ('mgr', ?, '2099-01-01')",
                           (self.W1,))
        self.assertIn("only a person or the manager moves a session",
                      self.run_cli("w2", "", "--as", "w2", "session", "--move", self.W1, "--to", "w1"))
        self.assertEqual(self.run_cli("mgr", self.MGR, "--as", "mgr", "session", "--move", self.W1, "--to", "w1"), 0)
        self.assertEqual(self.sessions("mgr"), [self.MGR])
        self.assertEqual(self.sessions("w1"), [self.W1])
        self.assertIn("is no agent", self.run_cli("", "", "session", "--move", self.W1, "--to", "nobody"))
        self.assertIn("--move <session id> --to <agent>", self.run_cli("", "", "session", "--move", self.W1))



class AnnotateKept(Base):
    """#5585: annotate keeps its open items between page builds and commands (ann_kept) and computes again only what a
    change reaches. After every kind of change the kept result equals a full computation."""

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "a")
        core.project_add(self.c, "b")
        for n in ("s1", "s2"):
            core.register(self.c, n)
        core.register(self.c, "mark", human=True)
        self.share = {}
        tok = core.ANN_SHARE.set(self.share)
        self.addCleanup(core.ANN_SHARE.reset, tok)

    def same(self, what):
        kept = core.annotate(self.c, closed=False)
        tok = core.ANN_SHARE.set(None)
        try:
            full = core._annotate(self.c, closed=False)
        finally:
            core.ANN_SHARE.reset(tok)
        self.assertEqual(sorted(kept), sorted(full), what)
        for i in full:
            self.assertEqual(kept[i], full[i], f"{what}: #{i}")
        self.assertEqual(kept._links, full._links, f"{what}: the links")
        closed = [r[0] for r in self.c.execute("SELECT id FROM items WHERE status IN ('done','dropped') ORDER BY id")]
        for i in closed[-3:]:
            self.assertEqual(kept[i], full[i], f"{what}: closed #{i}")
        self.c.execute("UPDATE graph_version SET n = n")  # nothing; the next call reads the log
        return kept

    def test_the_kept_result_equals_a_full_one_after_each_change(self):
        a, b, c, d = (self.add("a", t) for t in ("a", "b", "c", "d"))
        e = core.item_add(self.c, "b", "e", after=[c])["id"]
        core.dep_add(self.c, b, [a])
        core.dep_add(self.c, c, [b])
        core.goal_add(self.c, "a", "g")
        self.same("first")
        seen = []
        real = core._ann_redo
        core._ann_redo = lambda *x: (lambda r: (seen.append(r), r)[1])(real(*x))
        self.addCleanup(setattr, core, "_ann_redo", real)
        steps = [
            ("a title", lambda: core.item_edit(self.c, d, title="d two", actor="s1")),
            ("a priority up the chain", lambda: core.item_prio(self.c, e, 0)),
            ("a link", lambda: core.dep_add(self.c, d, [c])),
            ("a link removed", lambda: core.dep_remove(self.c, d, [c])),
            ("a conflict", lambda: core.dep_add(self.c, d, [a], kind="conflicts")),
            ("a claim beside the conflict", lambda: core.claim(self.c, a, "s1")),
            ("a close", lambda: core.done(self.c, a, output="x", actor="s1")),
            ("a reopen", lambda: core.reopen(self.c, a, actor="s1")),
            ("a goal tag", lambda: core.item_edit(self.c, b, goals=["g"], actor="s1")),
            ("a goal owner", lambda: core.goal_own(self.c, "g", "s2")),
            ("a queue entry", lambda: core.queue_add(self.c, "s1", d, actor="mark")),
            ("an outside blocker", lambda: core.block(self.c, c, "waits on a vendor", "s1", until="2h")),
            ("a due date down the chain", lambda: core.item_edit(self.c, e, due="2030-01-01T00:00:00Z", actor="s1")),
            ("a project rank", lambda: core.project_rank(self.c, "b", 0)),
            ("a new item", lambda: core.item_add(self.c, "a", "f", after=[d])),
            ("a drop", lambda: core.drop(self.c, d, actor="s1", note="no")),
        ]
        for what, step in steps:
            step()
            self.same(what)
        self.assertTrue(any(r is not None and len(r) <= 2 for r in seen), seen)  # a title: its own item only

    def test_random_changes(self):
        import random
        rnd = random.Random(5585)
        ids = [self.add(rnd.choice("ab"), f"t{n}") for n in range(25)]
        self.same("first")
        for step in range(80):
            i, j = rnd.sample(ids, 2)
            op = rnd.choice(["prio", "dep", "undep", "conflict", "done", "reopen", "title", "block", "unblock", "add"])
            try:
                if op == "prio":
                    core.item_prio(self.c, i, rnd.randint(0, 4))
                elif op == "dep":
                    core.dep_add(self.c, i, [j])
                elif op == "undep":
                    core.dep_remove(self.c, i, [j])
                elif op == "conflict":
                    core.dep_add(self.c, i, [j], kind="conflicts")
                elif op == "done":
                    core.done(self.c, i, output="x", actor="s1", force="test")
                elif op == "reopen":
                    core.reopen(self.c, i, actor="s1")
                elif op == "title":
                    core.item_edit(self.c, i, title=f"t{i} {step}", actor="s1")
                elif op == "block":
                    core.block(self.c, i, "outside", "s1")
                elif op == "unblock":
                    core.unblock(self.c, i, "s1")
                else:
                    ids.append(core.item_add(self.c, rnd.choice("ab"), f"n{step}", after=[j])["id"])
            except RiverError:
                continue  # a refused change changes nothing
            self.same(f"step {step}: {op} #{i} #{j}")


class NativeWindows(unittest.TestCase):
    """mark 2026-11-12: MaximizePM runs on Windows only inside WSL 2. On native Windows each entry point of the maxpm
    command (bin/maxpm and the pyproject entry point, river.client.main; river.cli.main) stops at once with one line
    that says to install WSL 2, and runs nothing."""

    def test_each_entry_point_refuses_with_the_wsl_line(self):
        from river import cli, client
        for main in (client.main, cli.main):
            err = io.StringIO()
            with mock.patch.object(os, "name", "nt"), mock.patch.object(client, "run_remote") as remote, \
                    mock.patch.object(cli, "run") as run, contextlib.redirect_stderr(err), \
                    self.assertRaises(SystemExit) as end:
                main()
            self.assertEqual(end.exception.code, 2)
            self.assertEqual(err.getvalue(), f"maxpm: {client.WSL_ONLY}\n")
            self.assertIn("WSL 2", err.getvalue())
            remote.assert_not_called()
            run.assert_not_called()
