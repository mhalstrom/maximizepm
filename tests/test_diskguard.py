"""Free space on the disk of the queue (#6506) and temp-folder growth guard (#6509)."""

import os
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from river import core, diskguard, server

GB = 1024 ** 3
KB = 1024
FREE_BYTES = diskguard.free_bytes  # the real one; setUp replaces it with a fake


def _ps_lines(rows):
    return "\n".join(f"{pid} {ppid} {etime} {cmd}" for pid, ppid, etime, cmd in rows)


class DiskGuard(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        env = mock.patch.dict(os.environ, {"MAXPM_DB": str(self.root / "t.db")})
        env.start()
        self.addCleanup(env.stop)
        self.c = core.connect(str(self.root / "t.db"))
        self.addCleanup(self.c.close)
        core.config_set(self.c, "session_builds", str(self.root / "builds"))
        p = mock.patch.object(server, "CLAUDE_TMP", str(self.root / "claude-501"))
        p.start()
        self.addCleanup(p.stop)
        core.register(self.c, "mark", human=True)
        self.free = [50 * GB]
        p = mock.patch.object(diskguard, "free_bytes", lambda conn: self.free[0])
        p.start()
        self.addCleanup(p.stop)
        self.ran = []

        def runner(cmd):
            self.ran.append(cmd)
            self.free[0] += 40 * GB
            return 0, "removed 40 GB\n"
        diskguard.RUNNER = runner
        self.addCleanup(setattr, diskguard, "RUNNER", None)

    def alerts(self):
        return [r["body"] for r in self.c.execute(
            "SELECT body FROM messages WHERE kind='alert' AND to_agent='mark' ORDER BY id")]

    def test_one_alert_each_time_the_free_space_falls_below_the_warn_level(self):
        diskguard.serve_pass(self.c, wait=True)
        self.assertEqual(self.alerts(), [])
        self.free[0] = 25 * GB
        diskguard.serve_pass(self.c, wait=True)
        diskguard.serve_pass(self.c, wait=True)
        self.assertEqual(len(self.alerts()), 1)
        self.assertIn("below disk_warn_free (30 GB)", self.alerts()[0])
        self.assertIn("Temp: no session temp folder", self.alerts()[0])
        self.free[0] = 35 * GB
        diskguard.serve_pass(self.c, wait=True)
        self.free[0] = 29 * GB
        diskguard.serve_pass(self.c, wait=True)
        self.assertEqual(len(self.alerts()), 2)
        self.assertEqual(self.ran, [])

    def test_below_the_sweep_level_the_command_runs_once_an_hour_and_the_alert_gives_before_and_after(self):
        core.config_set(self.c, "disk_sweep_cmd", "echo clean")
        self.free[0] = 10 * GB
        diskguard.serve_pass(self.c, wait=True)
        self.assertEqual(self.ran, ["echo clean"])
        self.assertIn("the sweep ran: 10 GB free before, 50 GB after. disk_sweep_cmd: removed 40 GB",
                      self.alerts()[-1])
        self.free[0] = 10 * GB
        diskguard.serve_pass(self.c, wait=True)
        self.assertEqual(len(self.ran), 1)  # not again within SWEEP_EVERY
        later = core.now() + diskguard.SWEEP_EVERY + timedelta(minutes=1)
        with mock.patch.object(core, "now", lambda: later):
            diskguard.serve_pass(self.c, wait=True)
        self.assertEqual(len(self.ran), 2)

    def test_the_alerts_name_the_largest_entries_of_the_session_temp_folder(self):
        tmp = self.root / "claude-501"
        (tmp / "race-test").mkdir(parents=True)
        (tmp / "race-test" / "q.db").write_bytes(b"x" * 300000)
        (tmp / "small.txt").write_bytes(b"x")
        self.free[0] = 10 * GB
        diskguard.serve_pass(self.c, wait=True)
        for body in self.alerts():
            self.assertRegex(body, r"Temp: \S+claude-501 holds \S+ KB \(largest: race-test \S+ KB, small.txt ")

    def test_with_no_command_the_alert_names_the_setting(self):
        self.free[0] = 10 * GB
        diskguard.serve_pass(self.c, wait=True)
        self.assertEqual(self.ran, [])
        self.assertIn("maxpm config set disk_sweep_cmd", self.alerts()[-1])

    def test_off_turns_the_guard_off_and_a_bad_size_is_refused(self):
        core.config_set(self.c, "disk_warn_free", "off")
        core.config_set(self.c, "disk_sweep_free", "off")
        self.free[0] = 1 * GB
        self.assertIsNone(diskguard.serve_pass(self.c, wait=True))
        self.assertEqual(self.alerts(), [])
        with self.assertRaises(core.RiverError):
            core.config_set(self.c, "disk_warn_free", "lots")

    def test_the_alert_goes_to_the_active_manager_when_there_is_one(self):
        with mock.patch.object(core, "active_manager", lambda conn, but=None: "manager-1"):
            self.free[0] = 25 * GB
            diskguard.serve_pass(self.c, wait=True)
        r = self.c.execute("SELECT to_agent FROM messages WHERE kind='alert'").fetchall()
        self.assertEqual([x["to_agent"] for x in r], ["manager-1"])

    def test_a_real_command_runs_in_a_shell_and_its_last_line_is_kept(self):
        diskguard.RUNNER = None
        rc, out = diskguard._shell("echo one; echo two")
        self.assertEqual((rc, diskguard._last_line(out)), (0, "two"))

    def test_free_bytes_reads_the_volume_of_the_queue(self):
        import shutil
        self.assertEqual(FREE_BYTES(self.c) // GB, shutil.disk_usage(self.root).free // GB)


class TmpGuard(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        env = mock.patch.dict(os.environ, {"MAXPM_DB": str(self.root / "t.db")})
        env.start()
        self.addCleanup(env.stop)
        self.c = core.connect(str(self.root / "t.db"))
        self.addCleanup(self.c.close)
        self.tmp_dir = self.root / "claude-501"
        self.tmp_dir.mkdir(parents=True)
        p = mock.patch.object(server, "CLAUDE_TMP", str(self.tmp_dir))
        p.start()
        self.addCleanup(p.stop)
        p_free = mock.patch.object(diskguard, "free_bytes", lambda conn: 50 * GB)
        p_free.start()
        self.addCleanup(p_free.stop)
        diskguard._TMP_LAST.clear()
        self.addCleanup(diskguard._TMP_LAST.clear)
        self.addCleanup(setattr, diskguard, "LSOF", None)
        core.register(self.c, "mark", human=True)
        core.project_add(self.c, "proj", actor="mark")

    def _add_session(self, name, pid):
        core.register(self.c, name)
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET pid=?, host=? WHERE name=?", (pid, core.this_host(), name))

    def _msgs(self, to_agent, kind=None):
        q = "SELECT body FROM messages WHERE to_agent=?"
        args = [to_agent]
        if kind:
            q += " AND kind=?"
            args.append(kind)
        return [r["body"] for r in self.c.execute(q + " ORDER BY id", args)]

    def test_tmp_max_size_default_off_and_validation(self):
        self.assertEqual(core.DEFAULT_SETTINGS["tmp_max_size"], "2GB")
        core.config_set(self.c, "tmp_max_size", "500MB", actor="mark")
        self.assertEqual(core.setting(self.c, "tmp_max_size"), "500MB")
        core.config_set(self.c, "tmp_max_size", "off", actor="mark")
        self.assertEqual(diskguard.check_tmp(self.c), [])
        with self.assertRaises(core.RiverError):
            core.config_set(self.c, "tmp_max_size", "huge", actor="mark")

    def test_growing_entry_maps_writer_to_command_root_ends_it_and_notifies(self):
        core.config_set(self.c, "tmp_max_size", "100KB", actor="mark")
        self._add_session("worker-1", 1000)
        item = core.item_add(self.c, "proj", "run race test", actor="mark")["id"]
        core.claim(self.c, item, "worker-1")

        # First pass records baseline (empty temp folder).
        self.assertEqual(diskguard.check_tmp(self.c), [])

        # Entry grows past 100KB.
        entry = self.tmp_dir / "c6358"
        entry.mkdir()
        (entry / "q.db").write_bytes(b"x" * (150 * KB))

        ps = _ps_lines([
            (1000, 1, "30:00", "claude --model opus"),
            (1001, 1000, "29:59", "node /opt/mcp/server.js"),
            (1050, 1000, "05:00", "/bin/zsh -c python3 race_test.py"),
            (1051, 1050, "05:00", "python3 race_test.py"),
            (1052, 1051, "04:59", "python3 worker.py"),
        ])
        diskguard.LSOF = lambda path: "p1052\nf3\naw\n"
        killed = []

        def fake_kill(conn, agent, pid=None, **kw):
            killed.append((agent, pid, kw.get("by_serve"), kw.get("reason")))
            return {"agent": agent, "root_pid": 1000, "pid": pid, "command": "python3 race_test.py",
                    "killed_pids": [1052, 1051, 1050], "force_killed": []}

        with mock.patch.object(core, "SHELL_RUNNER", lambda: ps), \
             mock.patch.object(core, "kill_session_command", fake_kill):
            stopped = diskguard.check_tmp(self.c)

        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]["agent"], "worker-1")
        self.assertEqual(stopped[0]["pid"], 1050)  # command root, not leaf 1052
        self.assertEqual(killed[0][0], "worker-1")
        self.assertEqual(killed[0][1], 1050)
        self.assertTrue(killed[0][2])
        self.assertIn("c6358", killed[0][3])

        notes = self._msgs("worker-1", kind="note")
        self.assertEqual(len(notes), 1)
        self.assertIn("ended your command (PID 1050: python3 race_test.py)", notes[0])
        self.assertIn("c6358", notes[0])
        self.assertIn("tmp_max_size (100 KB)", notes[0])

        alerts = self._msgs("mark", kind="alert")
        self.assertEqual(len(alerts), 1)
        self.assertIn("ended a command in worker-1", alerts[0])

        # Next pass does not re-trigger unless it grows by another 100KB.
        with mock.patch.object(core, "SHELL_RUNNER", lambda: ps), \
             mock.patch.object(core, "kill_session_command", fake_kill):
            self.assertEqual(diskguard.check_tmp(self.c), [])
        self.assertEqual(len(killed), 1)

    def test_limit_applies_to_one_entry_growth_not_folder_total_or_preexisting_static(self):
        core.config_set(self.c, "tmp_max_size", "100KB", actor="mark")
        # Pre-existing large entry (200KB) before baseline.
        old = self.tmp_dir / "old-large"
        old.mkdir()
        (old / "data.bin").write_bytes(b"x" * (200 * KB))
        # Establish baseline.
        self.assertEqual(diskguard.check_tmp(self.c), [])

        # Three new entries of 60KB each (folder grows by 180KB > 100KB, but each entry < 100KB).
        for i in range(3):
            d = self.tmp_dir / f"small-{i}"
            d.mkdir()
            (d / "f.bin").write_bytes(b"x" * (60 * KB))
        # Pre-existing entry grows by 20KB (< 100KB).
        (old / "extra.bin").write_bytes(b"x" * (20 * KB))

        # Project slug folder with two session scratchpads of 60KB each (slug total 120KB > 100KB, each session < 100KB).
        slug = self.tmp_dir / "-Users-mark-Projects-proj"
        for sid in ("sess-a", "sess-b"):
            sp = slug / sid / "scratchpad"
            sp.mkdir(parents=True)
            (sp / "out.bin").write_bytes(b"x" * (60 * KB))

        diskguard.LSOF = lambda path: self.fail("lsof should not run when no single entry grew by tmp_max_size")
        self.assertEqual(diskguard.check_tmp(self.c), [])
        self.assertEqual(self._msgs("mark", kind="alert"), [])

        # Now one session's scratchpad grows by another 60KB (total 120KB >= 100KB).
        (slug / "sess-a" / "scratchpad" / "more.bin").write_bytes(b"x" * (60 * KB))
        self._add_session("worker-a", 2000)
        ps = _ps_lines([
            (2000, 1, "20:00", "claude"),
            (2010, 2000, "02:00", "/bin/zsh -c python3 dump.py"),
        ])
        diskguard.LSOF = lambda path: "p2010\nf4\naw\n"
        with mock.patch.object(core, "SHELL_RUNNER", lambda: ps), \
             mock.patch.object(core, "kill_session_command",
                               lambda conn, agent, pid=None, **kw: {"agent": agent, "root_pid": 2000, "pid": pid,
                                                                    "command": "python3 dump.py", "killed_pids": [pid],
                                                                    "force_killed": []}):
            stopped = diskguard.check_tmp(self.c)
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]["agent"], "worker-a")
        self.assertIn("sess-a", stopped[0]["label"])

    def test_prefers_writer_over_reader_and_never_kills_agent_root(self):
        core.config_set(self.c, "tmp_max_size", "100KB", actor="mark")
        self._add_session("reader-1", 1000)
        self._add_session("writer-2", 2000)
        diskguard.check_tmp(self.c)

        entry = self.tmp_dir / "shared-out"
        entry.mkdir()
        (entry / "big.bin").write_bytes(b"x" * (120 * KB))

        ps = _ps_lines([
            (1000, 1, "30:00", "claude"),
            (1010, 1000, "01:00", "/bin/zsh -c cat shared-out/big.bin"),
            (2000, 1, "30:00", "claude"),
            (2010, 2000, "01:00", "/bin/zsh -c python3 fill.py"),
        ])
        # PID 1010 has read access, PID 2000 is the agent root itself, PID 2010 has write access.
        diskguard.LSOF = lambda path: "p1010\nf3\nar\np2000\nf5\naw\np2010\nf4\naw\n"
        killed = []

        def fake_kill(conn, agent, pid=None, **kw):
            killed.append((agent, pid))
            return {"agent": agent, "root_pid": 2000, "pid": pid, "command": "python3 fill.py",
                    "killed_pids": [pid], "force_killed": []}

        with mock.patch.object(core, "SHELL_RUNNER", lambda: ps), \
             mock.patch.object(core, "kill_session_command", fake_kill):
            stopped = diskguard.check_tmp(self.c)
        self.assertEqual(killed, [("writer-2", 2010)])
        self.assertEqual([s["agent"] for s in stopped], ["writer-2"])

    def test_non_session_writer_alerts_once_without_killing(self):
        core.config_set(self.c, "tmp_max_size", "100KB", actor="mark")
        diskguard.check_tmp(self.c)

        entry = self.tmp_dir / "external-dump"
        entry.mkdir()
        (entry / "big.bin").write_bytes(b"x" * (120 * KB))
        diskguard.LSOF = lambda path: "p99999\nf3\naw\n"
        self.assertEqual(diskguard.check_tmp(self.c), [])
        alerts = self._msgs("mark", kind="alert")
        self.assertEqual(len(alerts), 1)
        self.assertIn("external-dump", alerts[0])
        self.assertIn("99999", alerts[0])
        self.assertIn("no live session command was found", alerts[0])
        # Second pass does not spam.
        self.assertEqual(diskguard.check_tmp(self.c), [])
        self.assertEqual(len(self._msgs("mark", kind="alert")), 1)

    def test_real_lsof_finds_writing_process(self):
        f = self.tmp_dir / "real-open.bin"
        with open(f, "wb") as fh:
            fh.write(b"hello")
            fh.flush()
            w_file, _ = diskguard._lsof_pids(f)
            w_dir, _ = diskguard._lsof_pids(self.tmp_dir)
        self.assertIn(os.getpid(), w_file)
        self.assertIn(os.getpid(), w_dir)

    def test_real_subprocess_tree_is_ended_when_entry_grows_past_limit(self):
        core.config_set(self.c, "tmp_max_size", "100KB", actor="mark")
        # Register our own test process as the session root so the spawned child is a real descendant.
        self._add_session("real-worker", os.getpid())
        diskguard.check_tmp(self.c)

        entry = self.tmp_dir / "runaway"
        entry.mkdir()
        target = entry / "out.bin"
        proc = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, time; f=open(sys.argv[1], 'wb'); f.write(b'x'*(150*1024)); f.flush(); time.sleep(30)",
             str(target)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        def _cleanup_proc():
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        self.addCleanup(_cleanup_proc)
        # Wait briefly for the child to write the 150KB file.
        import time
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and (not target.exists() or target.stat().st_size < 150 * KB):
            time.sleep(0.05)
        self.assertGreaterEqual(target.stat().st_size, 150 * KB)

        # Provide the process tree (macOS sandbox blocks ps -A) while using real lsof and real kill_session_command.
        ps = _ps_lines([
            (os.getpid(), 1, "10:00", "python3 unittest"),
            (proc.pid, os.getpid(), "00:02", "python3 runaway"),
        ])
        with mock.patch.object(core, "SHELL_RUNNER", lambda: ps):
            stopped = diskguard.check_tmp(self.c, grace=2.0, server_grace=timedelta(0))
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]["agent"], "real-worker")
        self.assertEqual(stopped[0]["pid"], proc.pid)
        proc.wait(timeout=5)
        self.assertIsNotNone(proc.returncode)

    def test_serve_pass_throttles_tmp_checks_by_tmp_every(self):
        core.config_set(self.c, "tmp_max_size", "100KB", actor="mark")
        calls = []
        t0 = core.now()
        with mock.patch.object(diskguard, "check_tmp", lambda conn: calls.append(1) or []), \
             mock.patch.object(core, "now", lambda: t0):
            diskguard.serve_pass(self.c, wait=False)
            diskguard.serve_pass(self.c, wait=False)
        self.assertEqual(len(calls), 1)
        t1 = t0 + diskguard.TMP_EVERY + timedelta(seconds=1)
        with mock.patch.object(diskguard, "check_tmp", lambda conn: calls.append(1) or []), \
             mock.patch.object(core, "now", lambda: t1):
            diskguard.serve_pass(self.c, wait=False)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
