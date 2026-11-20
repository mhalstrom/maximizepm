"""Free space on the disk of the queue (#6506): one alert below disk_warn_free, the sweep below disk_sweep_free."""

import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from river import core, diskguard, server

GB = 1024 ** 3
FREE_BYTES = diskguard.free_bytes  # the real one; setUp replaces it with a fake


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


if __name__ == "__main__":
    unittest.main()
