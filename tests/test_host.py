"""maxpm host setup <target> (#5876): install on a remote host over SSH, serve at login there, the page over
Tailscale there, and the move of the queue. A fake ssh plays the host."""

import contextlib
import io
import json
import os
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from river import __version__, cli, core, host

URL = "https://mini.tail1234.ts.net/"


class FakeHost:
    """The host at the other end of ssh: what it has installed, its queue's projects, and what it received."""

    def __init__(self, test):
        self.test = test
        self.version = None
        self.projects = []
        self.answers = True
        self.phone = {"installed": True, "running": True, "serving": True, "url": URL, "enable": None}
        self.phone_err = ""
        self.files = {}
        self.ran = []

    def __call__(self, argv, data, timeout):
        self.test.assertEqual(argv[:5], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"])
        cmd = argv[6]
        self.ran.append(cmd)
        if not self.answers:
            return 255, "", "ssh: connect to host mini port 22: Connection refused"
        if cmd == "uname -sm":
            return 0, "Linux x86_64", ""
        if cmd.endswith("maxpm --quiet --version"):
            return (0, self.version, "") if self.version else (127, "", "maxpm: command not found")
        if "install-standalone.sh" in cmd:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                self.archive = tar.getnames()
            self.version = f"maxpm {__version__}"
            return 0, "Installed", ""
        if cmd.endswith("--json project list"):
            return 0, json.dumps(self.projects), ""
        if "db path" in cmd:
            name = "maxpm-archive.db" if "-archive" in cmd else "maxpm.db"
            self.files[name] = data
            return 0, f"/home/me/.maximizepm/{name}", ""
        if cmd.endswith("serve --kick"):
            return 1, "", "maxpm: install one first"
        if cmd.endswith("serve --install-login"):
            return 0, "It also runs with nobody logged in, from boot on (systemd linger is now on).", ""
        if cmd.endswith("--json phone --on"):
            return (1, "", self.phone_err) if self.phone_err else (0, json.dumps(self.phone), "")
        raise AssertionError(f"unexpected command on the host: {cmd}")


class HostSetup(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        env = mock.patch.dict(os.environ, {"MAXPM_DB": str(Path(d.name) / "q.db"), "MAXPM_AGENT": "",
                                           "MAXPM_LOCAL": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.conn = core.connect()
        self.addCleanup(self.conn.close)
        core.project_add(self.conn, "web", path=str(Path(d.name) / "web"))
        self.host = FakeHost(self)
        host.SSH_RUNNER = self.host
        self.addCleanup(setattr, host, "SSH_RUNNER", None)

    def run_cli(self, *words):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.run(list(words))
        return out.getvalue()

    def test_a_new_host_gets_this_code_serve_at_login_and_the_page(self):
        text = self.run_cli("host", "setup", "mini")
        self.assertIn("install-standalone.sh", self.host.archive)
        self.assertIn("river/host.py", self.host.archive)
        self.assertFalse(any("__pycache__" in n for n in self.host.archive))
        self.assertIn(f"installed maxpm {__version__} from this computer's code", text)
        self.assertIn("systemd linger is now on", text)
        self.assertIn(f"The page: {URL}", text)
        self.assertIn("▀", text)  # the QR code for the phone
        self.assertEqual(self.host.files, {})  # no --move: the queue stays here
        self.assertIsNone(host.moved(self.conn))

    def test_a_host_with_this_version_is_not_installed_again(self):
        self.host.version = f"maxpm {__version__}"
        text = self.run_cli("host", "setup", "mini")
        self.assertFalse(any("install-standalone.sh" in c for c in self.host.ran))
        self.assertIn("is there already", text)

    def test_no_answer_without_a_prompt_names_tailscale_ssh(self):
        self.host.answers = False
        with self.assertRaisesRegex(core.RiverError, "tailscale set --ssh"):
            self.run_cli("host", "setup", "mini")

    def test_move_sends_the_queue_and_this_copy_then_refuses(self):
        text = self.run_cli("host", "setup", "mini", "--move")
        sent = Path(tempfile.mkdtemp()) / "sent.db"
        sent.write_bytes(self.host.files["maxpm.db"])
        names = [r[0] for r in sqlite3.connect(sent).execute("SELECT name FROM projects")]
        self.assertIn("web", names)
        self.assertNotIn(host.MOVED_KEY, [r[0] for r in sqlite3.connect(sent).execute("SELECT key FROM meta")])
        self.assertIn("maxpm project path web <folder>", text)
        self.assertEqual(host.moved(self.conn)["url"], URL)
        with self.assertRaisesRegex(core.RiverError, f"this queue moved to mini .*open {URL}.*maxpm host forget"):
            self.run_cli("list")
        self.assertIn("in use here again", self.run_cli("host", "forget"))
        self.run_cli("list")

    def test_move_refuses_a_host_queue_with_projects(self):
        self.host.projects = [{"name": "other"}]
        with self.assertRaisesRegex(core.RiverError, "does not merge two queues"):
            self.run_cli("host", "setup", "mini", "--move")
        self.assertIsNone(host.moved(self.conn))
        self.assertEqual(self.host.files, {})

    def test_tailscale_without_operator_rights_names_the_command(self):
        self.host.phone_err = "maxpm: tailscale serve failed: Access denied: serve config denied"
        text = self.run_cli("host", "setup", "mini")
        self.assertIn("sudo tailscale set --operator=$USER", text)

    def test_a_bad_target_is_refused_before_ssh(self):
        with self.assertRaisesRegex(core.RiverError, "not an SSH target"):
            host.setup(self.conn, "-oProxyCommand=x")
        self.assertEqual(self.host.ran, [])


if __name__ == "__main__":
    unittest.main()
