"""install.sh with no usable Python names the installer that brings its own Python (#5)."""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class NoPython(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bin = Path(self.tmp.name, "bin")
        self.bin.mkdir()
        for tool in ("sh", "dirname", "sed", "cat", "mkdir", "ln"):  # what install.sh runs, and no python3
            os.symlink(shutil.which(tool), self.bin / tool)
        self.home = Path(self.tmp.name, "home")
        self.home.mkdir()

    def install(self):
        env = {"PATH": str(self.bin), "HOME": str(self.home)}
        return subprocess.run(["sh", str(ROOT / "install.sh")], env=env, capture_output=True, text=True, timeout=30)

    def old_python(self, version):
        fake = self.bin / "python3"
        fake.write_text("#!/bin/sh\ncase \"$2\" in *'>= (3, 10)'*) exit 1 ;; *) echo %s ;; esac\n" % version,
                        encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)

    def assert_points_to_the_standalone_installer(self, res):
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn(f"sh {ROOT}/install-standalone.sh --from {ROOT}", res.stderr)
        self.assertFalse((self.home / ".local" / "bin" / "maxpm").exists())  # nothing was linked

    def test_an_old_python_names_its_version_and_the_standalone_installer(self):
        self.old_python("3.9.6")
        res = self.install()
        self.assertIn("python3 3.10 or later is required; this one is 3.9.6", res.stderr)
        self.assert_points_to_the_standalone_installer(res)

    def test_no_python_names_the_standalone_installer(self):
        res = self.install()
        self.assertIn("python3 is required (3.10 or later)", res.stderr)
        self.assert_points_to_the_standalone_installer(res)

    def test_the_standalone_installer_takes_a_checkout(self):
        # The command that install.sh prints stays a real one.
        text = (ROOT / "install-standalone.sh").read_text(encoding="utf-8")
        self.assertIn("--from) ", text)


if __name__ == "__main__":
    unittest.main()
