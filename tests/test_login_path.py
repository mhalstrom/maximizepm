"""maxpm serve and the watchdog give their commands the PATH of a new terminal (#5611): the desktop app and launchd
start them with the short PATH of macOS, and checks that called cargo or pulumi ended with exit 127."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from river import core

SHORT = "/usr/bin:/bin:/usr/sbin:/sbin"
LOGIN = "/opt/homebrew/bin:/Users/x/.cargo/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


class LoginPath(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.c = core.connect(str(Path(d.name) / "t.db"))
        self.addCleanup(self.c.close)
        self.file = Path(d.name) / core.LOGIN_PATH_FILE
        self.asked = []
        old = core.LOGIN_PATH_RUNNER
        core.LOGIN_PATH_RUNNER = lambda: self.asked.append(1) or LOGIN
        self.addCleanup(setattr, core, "LOGIN_PATH_RUNNER", old)
        env = mock.patch.dict(os.environ, {"PATH": SHORT})
        env.start()
        self.addCleanup(env.stop)

    def test_the_short_path_of_the_app_gets_the_terminals_folders_first(self):
        added = core.adopt_login_path(self.c)
        self.assertEqual(os.environ["PATH"], LOGIN)
        self.assertEqual(added, ["/opt/homebrew/bin", "/Users/x/.cargo/bin", "/usr/local/bin"])
        self.assertEqual(self.file.read_text().strip(), LOGIN)  # kept for the watchdog

    def test_the_starters_own_folders_stay_first(self):
        os.environ["PATH"] = "/app/python/bin:/opt/homebrew/bin:" + SHORT  # launchd's job: maxpm's folder, Homebrew
        core.adopt_login_path(self.c)
        self.assertEqual(os.environ["PATH"].split(":")[:3], ["/app/python/bin", "/opt/homebrew/bin", "/Users/x/.cargo/bin"])
        self.assertEqual(os.environ["PATH"].split(":")[-4:], SHORT.split(":"))

    def test_a_terminals_path_with_every_folder_stays_as_it_is(self):
        mine = "/venv/bin:/usr/bin:/opt/homebrew/bin:/Users/x/.cargo/bin:/usr/local/bin:/bin:/usr/sbin:/sbin"
        os.environ["PATH"] = mine
        self.assertEqual(core.adopt_login_path(self.c), [])
        self.assertEqual(os.environ["PATH"], mine)

    def test_the_watchdog_reads_what_serve_kept_while_it_is_fresh(self):
        self.file.write_text("/kept/bin:/usr/bin\n")
        core.adopt_login_path(self.c, max_age=3600)
        self.assertEqual(self.asked, [])
        self.assertTrue(os.environ["PATH"].startswith("/kept/bin:"))
        old = time.time() - 7200
        os.utime(self.file, (old, old))
        os.environ["PATH"] = SHORT
        core.adopt_login_path(self.c, max_age=3600)  # too old: the login shell again
        self.assertEqual(self.asked, [1])
        self.assertEqual(os.environ["PATH"], LOGIN)

    def test_no_answer_of_the_login_shell_changes_nothing(self):
        core.LOGIN_PATH_RUNNER = lambda: None
        self.assertEqual(core.adopt_login_path(self.c), [])
        self.assertEqual(os.environ["PATH"], SHORT)

    @unittest.skipIf(os.name == "nt" or not os.path.exists("/bin/sh"), "needs a POSIX shell")
    def test_the_login_shell_is_read_from_its_marker_line(self):
        core.LOGIN_PATH_RUNNER = None
        with tempfile.TemporaryDirectory() as d:
            sh = Path(d) / "fake-shell"
            sh.write_text('#!/bin/sh\necho "a banner of the profile"\nPATH=/from/profile:$PATH\n'
                          'shift\neval "$1"\n')  # -ilc <script>: drop -ilc, run the script
            sh.chmod(0o755)
            with mock.patch.dict(os.environ, {"SHELL": str(sh)}):
                self.assertEqual(core.login_path(), "/from/profile:" + SHORT)


if __name__ == "__main__":
    unittest.main()
