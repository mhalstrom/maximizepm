"""install.sh with no usable Python names the installer that brings its own Python (#5)."""
import os
import shlex
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

    def install(self, root=ROOT):
        env = {"PATH": str(self.bin), "HOME": str(self.home)}
        return subprocess.run(["sh", str(Path(root, "install.sh"))], env=env, capture_output=True, text=True,
                              timeout=30)

    def printed_command(self, res):
        """The command that install.sh printed, as a shell reads it."""
        line = next(x for x in res.stderr.splitlines() if "install-standalone.sh" in x)
        return shlex.split(line)

    def old_python(self, version):
        fake = self.bin / "python3"
        fake.write_text("#!/bin/sh\ncase \"$2\" in *'>= (3, 10)'*) exit 1 ;; *) echo %s ;; esac\n" % version,
                        encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)

    def assert_points_to_the_standalone_installer(self, res, root=ROOT):
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertEqual(self.printed_command(res),
                         ["sh", str(Path(root, "install-standalone.sh")), "--from", str(root)])
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

    def test_the_printed_command_is_safe_to_paste_from_a_checkout_with_spaces(self):
        # A checkout in "My Projects/it's here": the shell reads the printed line as the same two paths.
        root = Path(self.tmp.name, "My Projects", "it's here")
        root.mkdir(parents=True)
        shutil.copy(ROOT / "install.sh", root / "install.sh")
        self.old_python("3.9.6")
        res = self.install(root)
        self.assert_points_to_the_standalone_installer(res, root)
        self.assertIn(f"'{Path(self.tmp.name, 'My Projects')}", res.stderr)  # quoted, not left bare

    def test_the_standalone_installer_takes_a_checkout(self):
        # The command that install.sh prints stays a real one.
        text = (ROOT / "install-standalone.sh").read_text(encoding="utf-8")
        self.assertIn("--from) ", text)


if __name__ == "__main__":
    unittest.main()
