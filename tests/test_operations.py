"""Operations areas, playbooks, triggers and events (#6625, plan: canvas operations-tab)."""

import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from river import cli, core
from river import operations as o
from river.core import RiverError


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "t.db")
        env = mock.patch.dict(os.environ, {"MAXPM_DB": self.path})
        env.start()
        self.addCleanup(env.stop)
        self.c = core.connect(self.path)
        self.addCleanup(self.c.close)
        core.target_add(self.c, "prod", "the servers")
        core.project_add(self.c, "site", target="prod")
        core.register(self.c, "dev")
        core.register(self.c, "boss")
        self.c.execute("UPDATE agents SET role='manager' WHERE name='boss'")
        self.c.commit()
        core.register(self.c, "mark", human=True)

    def url(self, scope, name, area=None, playbook=None, **kw):
        return core.ops_add(self.c, scope, name, "url", dict({"url": "https://example.com/"}, **kw), "mark", area,
                            playbook)


class Migration(Base):
    def old_queue(self):
        """A queue from before #6625: a checks table with a runbook and a repair on each check."""
        self.c.close()
        path = os.path.join(self.dir.name, "old.db")
        core.connect(path).close()
        raw = sqlite3.connect(path)
        raw.executescript(
            "DROP VIEW checks; DELETE FROM triggers;"
            "CREATE TABLE checks (id INTEGER PRIMARY KEY, scope_kind TEXT NOT NULL, scope TEXT NOT NULL, name TEXT NOT "
            "NULL, form TEXT NOT NULL, config TEXT NOT NULL, every TEXT NOT NULL, fail_after INTEGER NOT NULL, "
            "recover_after INTEGER NOT NULL, timeout TEXT NOT NULL, severity TEXT NOT NULL, runbook TEXT, subject "
            "INTEGER NOT NULL DEFAULT 0, repair TEXT, paused_until TEXT, state TEXT NOT NULL DEFAULT 'new', created_by "
            "TEXT, created_at TEXT NOT NULL, changed_by TEXT, changed_at TEXT, UNIQUE (scope_kind, scope, name));"
            "INSERT INTO targets(name, created_at) VALUES ('prod', '2026-11-01T00:00:00Z');"
            "INSERT INTO projects(name, rank, target, created_at) VALUES ('site', 1, 'prod', '2026-11-01T00:00:00Z');"
            "INSERT INTO checks VALUES (7, 'project', 'site', 'home', 'url', '{\"url\": \"https://example.com/\", "
            "\"statuses\": \"200-399\"}', '5m', 3, 2, '30s', 'item', 'Look at the proxy first', 0, NULL, NULL, "
            "'fault', 'mark', '2026-11-01T00:00:00Z', NULL, NULL);"
            "INSERT INTO checks VALUES (9, 'target', 'prod', 'api', 'cmd', '{\"command\": \"true\"}', '1m', 1, 1, "
            "'10s', 'item', NULL, 0, 'systemctl restart api', NULL, 'healthy', 'mark', '2026-11-01T00:00:00Z', NULL, "
            "NULL);"
            "INSERT INTO checks VALUES (11, 'project', 'site', 'blog', 'url', '{\"url\": \"https://example.com/b\", "
            "\"statuses\": \"200\"}', '5m', 3, 2, '30s', 'item', NULL, 0, NULL, NULL, 'new', 'mark', "
            "'2026-11-01T00:00:00Z', NULL, NULL);"
            "DELETE FROM meta WHERE key LIKE 'schema:%';")
        raw.commit()
        raw.close()
        self.c = core.connect(path)
        self.addCleanup(self.c.close)

    def test_the_checks_of_an_older_queue_move_into_triggers_with_their_ids_areas_and_playbooks(self):
        self.old_queue()
        self.assertEqual(self.c.execute("SELECT type FROM sqlite_master WHERE name='checks'").fetchone()[0], "view")
        rows = {r["name"]: dict(r) for r in self.c.execute("SELECT * FROM checks")}
        self.assertEqual({k: (v["id"], v["state"]) for k, v in rows.items()},
                         {"home": (7, "fault"), "api": (9, "healthy"), "blog": (11, "new")})
        self.assertEqual(rows["home"]["runbook"], "Look at the proxy first")
        self.assertEqual((rows["api"]["runbook"], rows["api"]["repair"]), (None, "systemctl restart api"))
        self.assertEqual((rows["blog"]["runbook"], rows["blog"]["repair"]), (None, None))
        self.assertEqual(rows["blog"]["layer"], "app")  # a column the old table lacked takes the default
        areas = {(a["project"], a["name"], a["target"]) for a in o.area_list(self.c)["areas"]}
        self.assertEqual(areas, {("site", "general", None), ("site", "prod", "prod")})
        books = {b["name"]: b for b in o.playbook_list(self.c, "site")["playbooks"]}
        self.assertEqual(set(books), {"default", "home", "api"})
        self.assertEqual(books["home"]["knowledge"], "Look at the proxy first")
        self.assertEqual(books["default"]["triggers"], ["site/blog"])
        self.assertTrue(books["default"]["is_default"])
        self.assertEqual(sorted(c["name"] for c in core.ops_list(self.c)["checks"]), ["api", "blog", "home"])


class Checks(Base):
    def test_a_check_is_a_state_trigger_in_its_area_with_its_playbook(self):
        self.url("site", "home", runbook="Look at the proxy")
        self.url("prod", "api")
        t = o.trigger_show(self.c, "site", "home")
        self.assertEqual((t["condition"], t["arrival"], t["area"], t["playbook"], t["priority_used"]),
                         ("state", "poll", "general", "home", 1))
        t = o.trigger_show(self.c, "prod", "api")
        self.assertEqual((t["area"], t["playbook"], t["project"]), ("prod", "default", "site"))
        self.assertEqual(t["playbook_used"]["limits"], core.OPS_NO_LIMITS)

    def test_edit_moves_a_check_to_a_playbook_and_its_runbook_comes_from_there(self):
        self.url("site", "home")
        o.playbook_add(self.c, "site", "web", {"knowledge": "The site runs on one host", "done_means": "It answers"},
                       "mark")
        res = core.ops_edit(self.c, "site", "home", {}, actor="mark", playbook="web")
        self.assertEqual(res["fields"], ["playbook"])
        self.assertEqual(res["check"]["runbook"], "The site runs on one host")
        with self.assertRaisesRegex(RiverError, "own playbook"):
            core.ops_edit(self.c, "site", "home", {"runbook": "x"}, actor="mark", playbook="web")

    def test_a_runbook_edit_changes_the_checks_own_playbook_in_place(self):
        self.url("site", "home", runbook="one")
        core.ops_edit(self.c, "site", "home", {"runbook": "two"}, actor="mark")
        books = {b["name"]: b["knowledge"] for b in o.playbook_list(self.c, "site")["playbooks"]}
        self.assertEqual(books, {"home": "two"})
        core.ops_edit(self.c, "site", "home", {"runbook": ""}, actor="mark")
        self.assertIsNone(core.ops_show(self.c, "site", "home")["runbook"])
        self.assertEqual(o.trigger_show(self.c, "site", "home")["playbook"], "default")

    def test_a_check_in_another_area_of_its_project(self):
        o.area_add(self.c, "site", "db-eu", "the database in eu-west-1", "db-eu.example", actor="mark")
        self.url("site", "db", area="db-eu")
        self.assertEqual(o.area_show(self.c, "site", "db-eu")["trigger_list"][0]["name"], "db")
        o.area_add(self.c, "site", "prod-env", target="prod", actor="mark")
        with self.assertRaisesRegex(RiverError, "the deploy target prod"):
            self.url("site", "x", area="prod-env")  # a project check in the target's area: the scope differs


class Playbooks(Base):
    def test_a_playbook_needs_knowledge_and_done_means(self):
        with self.assertRaisesRegex(RiverError, "--done-means"):
            o.playbook_add(self.c, "site", "web", {"knowledge": "k"}, "mark")

    def test_only_a_person_sets_tools_and_limits_the_manager_the_rest(self):
        """mark 2026-11-21 (#6641)."""
        with self.assertRaisesRegex(RiverError, "only a person sets the limits"):
            o.playbook_add(self.c, "site", "web", {"knowledge": "k", "done_means": "d", "limits": "restart"}, "boss")
        o.playbook_add(self.c, "site", "web", {"knowledge": "k", "done_means": "d"}, "boss")
        o.playbook_edit(self.c, "site", "web", {"limits": "restart the app alone"}, actor="mark")
        self.assertEqual(o.playbook_show(self.c, "site", "web")["limits"], "restart the app alone")
        with self.assertRaisesRegex(RiverError, "an agent"):
            o.playbook_add(self.c, "site", "w2", {"knowledge": "k", "done_means": "d"}, "dev")

    def test_an_agent_proposes_knowledge_and_the_manager_accepts(self):
        o.playbook_add(self.c, "site", "web", {"knowledge": "old", "done_means": "d"}, "mark")
        with self.assertRaisesRegex(RiverError, "--why"):
            o.playbook_edit(self.c, "site", "web", {"knowledge": "new"}, actor="dev")
        with self.assertRaisesRegex(RiverError, "proposes the knowledge"):
            o.playbook_edit(self.c, "site", "web", {"model": "opus"}, why="w", actor="dev")
        res = o.playbook_edit(self.c, "site", "web", {"knowledge": "new"}, why="the logs moved", actor="dev")
        self.assertEqual(res["changed"], "proposed")
        self.assertEqual(o.playbook_show(self.c, "site", "web")["knowledge"], "old")
        with self.assertRaisesRegex(RiverError, "an agent"):
            o.playbook_decide(self.c, f"p{res['proposal']['id']}", True, "dev")
        o.playbook_decide(self.c, f"p{res['proposal']['id']}", True, "boss")
        self.assertEqual(o.playbook_show(self.c, "site", "web")["knowledge"], "new")
        note = self.c.execute("SELECT body FROM messages WHERE to_agent='dev' ORDER BY id DESC").fetchone()[0]
        self.assertIn("accepted", note)


class Triggers(Base):
    def test_time_and_event_triggers_have_their_priority_and_stay_out_of_the_checks(self):
        o.trigger_add(self.c, "site", "weekly", "time", "poll", command="echo hi", cron="0 9 * * mon", actor="mark")
        o.trigger_add(self.c, "site", "tickets", "event", "push", priority=0, actor="boss")
        got = {t["name"]: (t["condition"], t["arrival"], t["priority_used"]) for t in o.trigger_list(self.c)["triggers"]}
        self.assertEqual(got, {"weekly": ("time", "poll", 2), "tickets": ("event", "push", 0)})
        self.assertEqual(core.ops_list(self.c)["checks"], [])  # no state trigger: nothing for the fault path to run
        o.trigger_edit(self.c, "site", "tickets", priority=-1, actor="mark")
        self.assertEqual(o.trigger_show(self.c, "site", "tickets")["priority_used"], 1)

    def test_the_rules_of_a_trigger(self):
        for kw, msg in ((dict(condition="state", arrival="poll"), "maxpm ops add"),
                        (dict(condition="time", arrival="push"), "polls"),
                        (dict(condition="event", arrival="poll"), "--command"),
                        (dict(condition="event", arrival="push", command="x"), "no routine")):
            with self.assertRaisesRegex(RiverError, msg):
                o.trigger_add(self.c, "site", "t", actor="mark", **kw)
        with self.assertRaisesRegex(RiverError, "an agent"):
            o.trigger_add(self.c, "site", "t", "event", "push", actor="dev")

    def test_an_event_id_is_recorded_once_and_repeats_are_counted(self):
        o.trigger_add(self.c, "site", "tickets", "event", "push", actor="mark")
        first = o.events_add(self.c, "site", "tickets", {"id": "T-1", "title": "Login fails", "link": "https://x/1"},
                             "mark")
        again = o.events_add(self.c, "site", "tickets", {"id": "T-1", "title": "Login fails"}, "mark")
        self.assertEqual((first["new"], again["new"], again["event"]["repeats"]), (True, False, 1))
        self.assertEqual(first["event"]["data"], {"link": "https://x/1"})
        with self.assertRaisesRegex(RiverError, "needs an id"):
            o.events_add(self.c, "site", "tickets", {"title": "no id"}, "mark")
        self.assertEqual(len(o.events_list(self.c)["events"]), 1)


class Commands(Base):
    def run_cli(self, *words):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, {"MAXPM_QUIET": "1"}):
            cli.run(list(words))
        return out.getvalue()

    def test_the_ops_commands_print_areas_playbooks_triggers_and_events(self):
        with mock.patch.object(core, "agent_type_from_env", lambda env: None):
            self.run_cli("ops", "area", "add", "site", "db-eu", "--place", "db-eu.example")
            self.run_cli("ops", "playbook", "add", "site", "tickets", "--knowledge", "Read it", "--done-means", "Replied")
            self.run_cli("ops", "trigger", "add", "site", "support", "--condition", "event", "--arrival", "push",
                         "--area", "db-eu", "--playbook", "tickets")
            out = self.run_cli("ops", "events", "add", "site", "support", "--id", "T-1", "--title", "Login fails",
                               "--data", '{"link": "https://x/1"}')
            self.assertIn("received: Login fails", out)
            out = self.run_cli("ops", "trigger", "list", "site")
            self.assertIn("site / db-eu", out)
            self.assertIn("site/support  [event, push; P1; playbook tickets]", out)
            out = self.run_cli("ops", "playbook", "show", "site", "tickets")
            self.assertIn("limits: investigate and propose only", out)
            out = json.loads(self.run_cli("--json", "ops", "events", "list"))
            self.assertEqual(out["events"][0]["data"], {"link": "https://x/1"})


if __name__ == "__main__":
    unittest.main()
