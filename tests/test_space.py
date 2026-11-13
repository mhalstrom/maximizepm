"""Size limits (#5711): over db_max_size or folder_max_size the oldest removable data goes first, to 90% of the limit;
a dry run removes nothing; items, decisions, answers, the newest backup and browser sign-ins stay."""

import contextlib
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from river import cli, core, space

OLD = "2026-01-01T00:00:00Z"


class SpaceBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        self.path = str(self.root / "t.db")
        env = mock.patch.dict(os.environ, {"MAXPM_DB": self.path})
        env.start()
        self.addCleanup(env.stop)
        self.c = core.connect(self.path)
        self.addCleanup(self.c.close)
        core.config_set(self.c, "folder_max_size", "off")
        core.config_set(self.c, "archive_after", "off")  # the Archive tests turn it on

    def closed_item(self, title, n_events=40):
        """A done item, closed long ago, with many mechanical events and one answer and one person's event."""
        core.project_add(self.c, "p") if not self.c.execute("SELECT 1 FROM projects WHERE name='p'").fetchone() else None
        iid = core.item_add(self.c, "p", title, 2, "", "any", (), "t")["id"]
        with core.tx(self.c):
            for i in range(n_events):
                core._event(self.c, iid, "bot", f"conflicts with {i} (touches overlap) " + "x" * 400)
            self.c.execute("INSERT INTO agents (name, kind, registered_at, last_seen) VALUES ('mark','human',?,?) "
                           "ON CONFLICT(name) DO NOTHING", (OLD, OLD))
            core._event(self.c, iid, "mark", "mark said: ship it")
            core._event(self.c, iid, "bot", "answered question #3")
            core._event(self.c, iid, "bot", "done: the output")
            self.c.execute("UPDATE items SET status='done', closed_at=?, output='the output' WHERE id=?", (OLD, iid))
        return iid


class Sizes(SpaceBase):
    def test_parse_and_show(self):
        self.assertEqual(space.parse_size("500MB"), 500 * 1024 ** 2)
        self.assertEqual(space.parse_size("2 GB"), 2 * 1024 ** 3)
        self.assertEqual(space.parse_size("64kb"), 64 * 1024)
        self.assertIsNone(space.parse_size("off"))
        with self.assertRaises(core.RiverError):
            space.parse_size("big")
        with self.assertRaises(core.RiverError):
            core.config_set(self.c, "db_max_size", "lots")
        core.config_set(self.c, "db_max_size", "500MB")
        core.config_set(self.c, "space_events_after", "60d")
        with self.assertRaises(core.RiverError):
            core.config_set(self.c, "space_events_after", "soon")
        self.assertEqual(space.show_size(81 * 1024 ** 2), "81 MB")
        self.assertEqual(space.show_size(int(2.2 * 1024 ** 3)), "2.2 GB")

    def test_settings_show_the_size_now(self):
        now = core.config_list(self.c)["now"]
        self.assertTrue(now["db_max_size"].startswith("now "))
        self.assertIn("not measured", now["folder_max_size"])


class Database(SpaceBase):
    def fill(self):
        self.items = [self.closed_item(f"old work {i}") for i in range(6)]
        self.open_id = core.item_add(self.c, "p", "open work", 2, "", "any", (), "t")["id"]
        with core.tx(self.c):
            for i in range(30):
                core._event(self.c, self.open_id, "bot", f"claimed {i} " + "y" * 400)
            for i in range(300):
                self.c.execute("INSERT INTO ops_runs (check_id, key, started_at, ended_at, status, output) "
                               "VALUES (1, 'ops:p:c', ?, ?, 'ok', ?)", (OLD, OLD, "z" * 300))
            self.c.execute("INSERT INTO ops_runs (check_id, key, started_at, ended_at, status, output) "
                           "VALUES (1, 'ops:p:c', ?, ?, 'ok', 'newest')", (OLD, OLD))

    def test_under_the_limit_nothing_goes(self):
        self.fill()
        before = self.c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        res = space.run(self.c)
        self.assertEqual(res["db"]["removed"], [])
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM events").fetchone()[0], before)

    def test_over_the_limit_oldest_removable_first_and_protected_rows_stay(self):
        self.fill()
        used = space.db_used(self.c)
        # A limit that the ops runs alone cannot meet: the events of closed items go too.
        core.config_set(self.c, "db_max_size", str(used - 160 * 1024))
        events_before = self.c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        dry = space.run(self.c, dry_run=True)
        self.assertTrue(dry["dry_run"])
        self.assertEqual([r["what"] for r in dry["db"]["removed"]], ["ops records", "events of closed items"])
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM events").fetchone()[0], events_before)  # nothing went
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM ops_runs").fetchone()[0], 301)
        res = space.run(self.c, actor="t")
        self.assertEqual([r["what"] for r in res["db"]["removed"]], ["ops records", "events of closed items"])
        self.assertEqual(res["db"]["removed"][0]["rows"], 300)  # the newest run of the check stays
        self.assertEqual(self.c.execute("SELECT output FROM ops_runs").fetchone()[0], "newest")
        self.assertFalse(res["db"]["over"])
        self.assertLess(space.db_used(self.c), used)
        # Every item stays with its title and output; the open item keeps all its history.
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM items").fetchone()[0], 7)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM events WHERE item_id=? AND change LIKE 'claimed%'",
                                        (self.open_id,)).fetchone()[0], 30)
        for iid in self.items:
            kept = [r[0] for r in self.c.execute("SELECT change FROM events WHERE item_id=? ORDER BY id", (iid,))]
            self.assertIn("mark said: ship it", kept)  # a person's event
            self.assertIn("answered question #3", kept)
            self.assertIn("done: the output", kept)
            self.assertTrue(kept[0].startswith("added to p"))  # the first event
        # The oldest closed item lost its mechanical events first.
        first = self.c.execute("SELECT COUNT(*) FROM events WHERE item_id=? AND change LIKE 'conflicts%'",
                               (self.items[0],)).fetchone()[0]
        self.assertEqual(first, 0)
        # One event without an item records the run.
        ev = self.c.execute("SELECT change FROM events WHERE item_id IS NULL AND change LIKE 'space cleanup%'").fetchall()
        self.assertEqual(len(ev), 1)
        self.assertIn("events of closed items", ev[0][0])

    def test_minimum_age_holds_over_the_limit(self):
        self.fill()
        core.config_set(self.c, "db_max_size", "16KB")
        core.config_set(self.c, "space_ops_after", "36500d")
        core.config_set(self.c, "space_events_after", "36500d")
        res = space.run(self.c)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM ops_runs").fetchone()[0], 301)
        self.assertTrue(res["db"]["over"])
        self.assertIn("still over the limit: database", res["summary"])

    def test_canvas_versions_between_first_and_newest(self):
        core.project_add(self.c, "p")
        with core.tx(self.c):
            self.c.execute("INSERT INTO canvases (id, project_id, name, kind, created_at, updated_at) "
                           "VALUES (1,(SELECT id FROM projects WHERE name='p'),'c','md',?,?)", (OLD, OLD))
            for v in range(1, 11):
                self.c.execute("INSERT INTO canvas_versions (canvas_id, version, body, created_at) VALUES (1,?,?,?)",
                               (v, "b" * 20000, OLD))
            self.c.execute("INSERT INTO canvas_comments (canvas_id, version, author, body, state, created_at) "
                           "VALUES (1, 4, 'mark', 'fix this', 'open', ?)", (OLD,))
        core.config_set(self.c, "db_max_size", "16KB")
        space.run(self.c)
        left = [r[0] for r in self.c.execute("SELECT version FROM canvas_versions ORDER BY version")]
        self.assertEqual(left, [1, 4, 10])  # the first, the commented one, the newest

    def test_read_notices_go_unread_ones_stay(self):
        core.project_add(self.c, "p")
        with core.tx(self.c):
            for i in range(50):
                read = OLD if i % 2 else None
                self.c.execute("INSERT INTO messages (kind, from_agent, to_agent, thread_id, body, state, created_at, "
                               "read_at) VALUES ('notice','maxpm','a',0,?,?,?,?)",
                               ("n" * 2000, "read" if read else "open", OLD, read))
            self.c.execute("UPDATE messages SET thread_id=id")
        core.config_set(self.c, "db_max_size", "16KB")
        space.run(self.c)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM messages WHERE read_at IS NOT NULL").fetchone()[0], 0)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM messages WHERE read_at IS NULL").fetchone()[0], 25)

    def test_ended_sessions_go_busy_ones_stay(self):
        core.project_add(self.c, "p")
        iid = core.item_add(self.c, "p", "held", 2, "", "any", (), "t")["id"]
        with core.tx(self.c):
            for n in ("old1", "old2", "holder"):
                self.c.execute("INSERT INTO agents (name, kind, registered_at, last_seen, note) VALUES (?,'ai',?,?,?)",
                               (n, OLD, OLD, "w" * 3000))
            self.c.execute("UPDATE items SET status='in_progress', assignee='holder' WHERE id=?", (iid,))
        core.config_set(self.c, "db_max_size", "16KB")
        space.run(self.c)
        names = {r[0] for r in self.c.execute("SELECT name FROM agents")}
        self.assertIn("holder", names)
        self.assertNotIn("old1", names)

    def test_manager_gets_one_notice(self):
        self.fill()
        with core.tx(self.c):
            self.c.execute("INSERT INTO agents (name, kind, role, registered_at, last_seen) VALUES "
                           "('mgr','ai','manager',?,?)", (core.iso(core.now()), core.iso(core.now())))
        core.config_set(self.c, "db_max_size", str(space.db_used(self.c) - 40 * 1024))
        with mock.patch.object(core, "active_manager", return_value="mgr"):
            space.run(self.c)
        notes = self.c.execute("SELECT body FROM messages WHERE to_agent='mgr'").fetchall()
        self.assertEqual(len(notes), 1)
        self.assertIn("space cleanup: removed", notes[0][0])


class Folder(SpaceBase):
    def file(self, rel, size, age_days=0):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
        t = time.time() - age_days * 86400
        os.utime(p, (t, t))
        return p

    def test_caches_of_stopped_browsers_then_old_uploads_then_old_backups(self):
        prof = self.root / "browsers" / "p" / "sessions" / "s1" / "profile"
        self.file("browsers/p/sessions/s1/profile/Default/Cache/data_1", 400_000)
        self.file("browsers/p/sessions/s1/profile/Default/Code Cache/js/a", 200_000)
        cookies = self.file("browsers/p/sessions/s1/profile/Default/Cookies", 50_000)
        # A running browser keeps its cache: its lock names this process.
        run_cache = self.file("browsers/p/profile/Default/Cache/data_1", 300_000)
        os.symlink(f"host-{os.getpid()}", self.root / "browsers" / "p" / "profile" / "SingletonLock")
        old_up = self.file("uploads/a/old.png", 100_000, age_days=30)
        new_up = self.file("uploads/a/new.png", 100_000)
        b1 = self.file("backups/one.tgz", 100_000, age_days=40)
        b2 = self.file("backups/two.tgz", 100_000, age_days=35)
        self.assertTrue(prof.is_dir())
        total = space.tree_bytes(self.root)
        core.config_set(self.c, "folder_max_size", str(total - 300_000))
        dry = space.run(self.c, dry_run=True)
        self.assertEqual([r["what"] for r in dry["folder"]["removed"]], ["browser caches"])
        self.assertTrue((prof / "Default" / "Cache").is_dir())  # a dry run removes nothing
        res = space.run(self.c)
        self.assertFalse((prof / "Default" / "Cache").exists())
        self.assertTrue(cookies.exists())  # sign-ins stay
        self.assertTrue(run_cache.exists())
        self.assertEqual(res["folder"]["kept"]["browser_profiles"], 2)
        self.assertTrue(old_up.exists())  # under the limit after the caches: the uploads stayed
        # A smaller limit: old uploads go, then old backups but the newest.
        core.config_set(self.c, "folder_max_size", "1KB")
        res = space.run(self.c)
        self.assertFalse(old_up.exists())
        self.assertTrue(new_up.exists())
        self.assertFalse(b1.exists())
        self.assertTrue(b2.exists())  # the newest backup always stays
        self.assertTrue(res["folder"]["over"])

    def test_stale_lock_is_a_stopped_browser(self):
        self.file("browsers/p/profile/Default/Cache/data_1", 300_000)
        os.symlink("host-999999999", self.root / "browsers" / "p" / "profile" / "SingletonLock")
        core.config_set(self.c, "folder_max_size", "1KB")
        space.run(self.c)
        self.assertFalse((self.root / "browsers" / "p" / "profile" / "Default" / "Cache").exists())


class Serve(SpaceBase):
    def test_serve_pass_runs_when_over_and_once_a_day(self):
        self.assertIsNotNone(space.serve_pass(self.c))  # never ran: it checks
        self.assertIsNone(space.serve_pass(self.c))  # ran just now with nothing removed
        Database.fill(self)
        core.config_set(self.c, "db_max_size", "16KB")
        m = space._meta(self.c)
        m["last_run"] = "2026-01-01T00:00:00Z"
        space._save_meta(self.c, m)
        res = space.serve_pass(self.c)
        self.assertTrue(res["db"]["removed"])


class Archive(SpaceBase):
    def setUp(self):
        super().setUp()
        core.config_set(self.c, "db_max_size", "off")
        core.config_unset(self.c, "archive_after")
        core.project_add(self.c, "p")
        self.old = [self.closed_item(f"old work {i}", n_events=3) for i in range(4)]
        self.recent = core.item_add(self.c, "p", "recent work", 2, "", "any", (), "t")["id"]
        core.done(self.c, self.recent, "finished", "t")
        self.open = core.item_add(self.c, "p", "open work", 2, "", "any", (), "t")["id"]
        # The open item waits on old[1], and old[2] was found during old[3] (which therefore stays only if old[2]
        # stays): old[1] stays; the rest move.
        core.dep_add(self.c, self.open, [self.old[1]], "t")
        with core.tx(self.c):
            self.c.execute("UPDATE items SET found_during=? WHERE id=?", (self.old[3], self.old[2]))
            self.c.execute("INSERT INTO messages (kind, from_agent, to_agent, item_id, thread_id, body, state, created_at) "
                           "VALUES ('note','a','b',?,0,'about the old work','read',?)", (self.old[0], OLD))
            self.c.execute("UPDATE messages SET thread_id=id")

    def ids(self, s="main"):
        return {r[0] for r in self.c.execute(f"SELECT id FROM {s}.items")}

    def test_old_closed_items_move_with_their_history(self):
        dry = space.run(self.c, dry_run=True)
        self.assertEqual(dry["archive"]["items"], 3)
        self.assertFalse(space.archive_path(self.c).exists())  # a dry run makes no archive
        res = space.run(self.c)
        self.assertEqual(res["archive"]["items"], 3)
        self.assertTrue(space.archive_path(self.c).exists())
        moved = {self.old[0], self.old[2], self.old[3]}
        self.assertEqual(self.ids() & moved, set())
        self.assertIn(self.old[1], self.ids())  # an open item waits on it
        self.assertIn(self.recent, self.ids())
        self.assertEqual(self.ids("archive"), moved)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM main.events WHERE item_id=?", (self.old[0],)).fetchone()[0], 0)
        self.assertGreater(self.c.execute("SELECT COUNT(*) FROM archive.events WHERE item_id=?",
                                          (self.old[0],)).fetchone()[0], 3)
        self.assertEqual(self.c.execute("SELECT body FROM archive.messages WHERE item_id=?", (self.old[0],)).fetchone()[0],
                         "about the old work")
        self.assertEqual(self.c.execute("PRAGMA main.foreign_key_check").fetchall(), [])
        # A new connection attaches the archive by itself.
        c2 = core.connect(self.path)
        self.addCleanup(c2.close)
        self.assertTrue(space.attached(c2))

    def test_show_and_search_read_the_archive_and_change_nothing(self):
        space.run(self.c)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(["show", str(self.old[0])])
        self.assertIn("ARCHIVED", out.getvalue())
        self.assertIn("old work 0", out.getvalue())
        self.assertNotIn(self.old[0], self.ids())  # still archived
        found = core.search(self.c, "old work")
        archived = {m["id"] for m in found["items"] if m.get("archived")}
        self.assertEqual(archived, {self.old[0], self.old[2], self.old[3]})
        self.assertEqual(core.search(self.c, "old work", status="open")["items"], [])

    def test_a_command_that_names_it_brings_it_back(self):
        space.run(self.c)
        core.reopen(self.c, self.old[2], "t")
        self.assertIn(self.old[2], self.ids())
        self.assertIn(self.old[3], self.ids())  # the item it was found during came too
        self.assertGreater(self.c.execute("SELECT COUNT(*) FROM main.events WHERE item_id=?", (self.old[2],)).fetchone()[0], 3)
        self.assertEqual(self.c.execute("PRAGMA main.foreign_key_check").fetchall(), [])
        # It may move again later: the archive's copies are replaced, not doubled.
        with core.tx(self.c):
            self.c.execute("UPDATE items SET status='done', closed_at=? WHERE id=?", (OLD, self.old[2]))
        space.run(self.c)
        self.assertNotIn(self.old[2], self.ids())
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM archive.items WHERE id=?", (self.old[2],)).fetchone()[0], 1)

    def test_with_archive_reads_them_on_demand(self):
        space.run(self.c)
        n_main = len(core.completed(self.c, None, None)["by_day"] and
                     [i for d in core.completed(self.c, None, None)["by_day"] for i in d["items"]])
        both = space.with_archive(self.c, lambda: core.completed(self.c, None, None))
        n_both = len([i for d in both["by_day"] for i in d["items"]])
        self.assertEqual(n_both - n_main, 3)
        self.assertNotIn(self.old[0], self.ids())  # rolled back

    def test_archive_after_off(self):
        core.config_set(self.c, "archive_after", "off")
        self.assertEqual(space.run(self.c)["archive"]["items"], 0)
        with self.assertRaises(core.RiverError):
            core.config_set(self.c, "archive_after", "never")


class Cli(SpaceBase):
    def test_cleanup_dry_run_prints_sizes_and_what_would_go(self):
        Database.fill(self)
        core.config_set(self.c, "db_max_size", "16KB")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(["cleanup", "--dry-run"])
        text = out.getvalue()
        self.assertIn("DRY RUN", text)
        self.assertIn("would remove 300 rows of ops records", text)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM ops_runs").fetchone()[0], 301)


if __name__ == "__main__":
    unittest.main()
