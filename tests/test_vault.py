"""maxpm secret (#5757): save a value by name, give it to a command or a .env file, never print it."""

import contextlib
import io
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from river import cli, core, vault
from river.core import RiverError
from river.routes import secrets as routes_secrets

TOKEN = b"cf-TOKEN-7f3a9e1d0b2c"


class Stdin:
    def __init__(self, data):
        self.buffer = io.BytesIO(data)

    def isatty(self):
        return False


class Vault(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "t.db")
        env = mock.patch.dict(os.environ, {"MAXPM_DB": self.path})
        env.start()
        self.addCleanup(env.stop)
        self.c = core.connect(self.path)
        self.addCleanup(self.c.close)
        old = vault.BACKEND
        vault.BACKEND = vault.Files()
        self.addCleanup(setattr, vault, "BACKEND", old)

    def run_cli(self, words, stdin=b""):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", Stdin(stdin)), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            try:
                rc = cli.run(["-q", "--as", "dev"] + words)
            except RiverError as e:  # cli.main prints it and exits 2
                rc = 2
                err.write(str(e))
        return rc, out.getvalue() + err.getvalue()

    def test_put_list_pipe_never_print_the_value(self):
        rc, out = self.run_cli(["secret", "put", "cf-token", "--note", "Cloudflare, Workers edit"], TOKEN + b"\n")
        self.assertEqual(rc, 0, out)
        self.assertIn("saved secret cf-token", out)
        f = os.path.join(self.dir.name, "secrets", "cf-token")
        self.assertEqual(Path(f).read_bytes(), TOKEN)  # echo's line end is gone
        self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o600)
        rc, out = self.run_cli(["secret", "list"])
        self.assertIn("cf-token", out)
        self.assertIn("never used", out)
        got = os.path.join(self.dir.name, "got")
        rc, out = self.run_cli(["secret", "pipe", "cf-token", "--", sys.executable, "-c",
                                f"import sys; open({got!r}, 'wb').write(sys.stdin.buffer.read())"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(Path(got).read_bytes(), TOKEN)
        self.assertIn("gave secret cf-token", out)
        rc, out2 = self.run_cli(["--json", "secret", "list"])
        rc, out3 = self.run_cli(["secret", "log"])
        for text in (out, out2, out3):
            self.assertNotIn(TOKEN.decode(), text)
        self.c.close()
        self.assertNotIn(TOKEN, Path(self.path).read_bytes())  # the queue keeps names only
        self.c = core.connect(self.path)
        self.assertIn("use", out3)
        self.assertIn("put", out3)

    def test_failed_command_is_an_error(self):
        self.run_cli(["secret", "put", "k"], b"v")
        rc, out = self.run_cli(["secret", "pipe", "k", "--", sys.executable, "-c", "import sys; sys.exit(3)"])
        self.assertEqual(rc, 2)
        self.assertIn("exit 3", out)
        rc, out = self.run_cli(["secret", "pipe", "k"])
        self.assertEqual(rc, 2)
        self.assertIn("name the command", out)

    def test_env_adds_then_replaces_the_line(self):
        self.run_cli(["secret", "put", "google-client-secret"], b"GOCSPX-abc def")
        env = os.path.join(self.dir.name, ".env")
        with open(env, "w") as f:
            f.write("A=1\nGOOGLE_CLIENT_SECRET=old\nB=2\n")
        rc, out = self.run_cli(["secret", "env", "google-client-secret", env])
        self.assertEqual(rc, 0, out)
        self.assertIn("replaced GOOGLE_CLIENT_SECRET", out)
        self.assertEqual(Path(env).read_text(), "A=1\nGOOGLE_CLIENT_SECRET='GOCSPX-abc def'\nB=2\n")
        new = os.path.join(self.dir.name, "new.env")
        rc, out = self.run_cli(["secret", "env", "google-client-secret", new, "--key", "SECRET"])
        self.assertIn("added SECRET", out)
        self.assertEqual(stat.S_IMODE(os.stat(new).st_mode), 0o600)

    def test_value_rules(self):
        pem = b"-----BEGIN KEY-----\nabc\n-----END KEY-----\n"
        self.assertEqual(vault.clean(pem), pem)  # a key file keeps its lines
        self.assertEqual(vault.clean(b"tok\r\n"), b"tok")
        with self.assertRaises(RiverError):
            vault.clean(b"")
        with self.assertRaises(RiverError):
            vault.check_name("../etc")
        rc, out = self.run_cli(["secret", "put", "x"], b"")
        self.assertEqual(rc, 2)
        self.assertIn("no value came", out)

    def test_delete_and_unknown_name(self):
        self.run_cli(["secret", "put", "k"], b"v")
        rc, out = self.run_cli(["secret", "delete", "k"])
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(os.path.join(self.dir.name, "secrets", "k")))
        rc, out = self.run_cli(["secret", "pipe", "k", "--", "cat"])
        self.assertEqual(rc, 2)
        self.assertIn("no secret k", out)
        rows = [r["action"] for r in vault.log(self.c, "k")["log"]]
        self.assertEqual(rows, ["delete", "put"])

    def test_log_names_the_held_item(self):
        core.project_add(self.c, "q")
        iid = core.item_add(self.c, "q", "deploy the worker", actor="t")["id"]
        core.register(self.c, "dev")
        core.claim(self.c, iid, "dev")
        vault.put(self.c, "k", b"v", "dev")
        self.assertEqual(vault.log(self.c, "k")["log"][0]["item_id"], iid)

    def test_from_browser_reads_in_serve(self):
        """--from-browser: the route reads the element's text in the project browser and saves it; the reply has no
        value."""
        class B:
            def running(self):
                return True
        seen = {}

        def on_element(b, sel, js):
            seen["sel"] = sel
            return "Your token: cf-abc123 (copy it now)"
        with mock.patch("river.routes.browser._browser_of", lambda conn, a, who: B()), \
                mock.patch("river.routes.browser._resolve", lambda conn, b, who, act, t, within=None, verb=None: "@e4"), \
                mock.patch("river.browser.on_element", on_element):
            res = routes_secrets.act(self.c, {"action": "put", "name": "cf", "from_browser": "API token",
                                              "match": r"token: (\S+)"}, "dev")
        self.assertEqual(seen["sel"], "@e4")
        self.assertNotIn("cf-abc123", repr(res))
        self.assertEqual(vault.Files().get(self.c, "cf"), b"cf-abc123")
        self.assertEqual(vault.log(self.c, "cf")["log"][0]["detail"], "browser")

    def test_sandbox_goes_through_serve(self):
        vault.BACKEND = None
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.dict(os.environ, {"SANDBOX_RUNTIME": "1"}):
            self.assertTrue(vault.needs_server())
            asked = []
            with mock.patch.object(cli, "ask_server", lambda conn, op, args, actor=None, timeout=5: asked.append(
                    (op, args)) or {"secrets": [], "store": "keychain"}):
                rc, out = self.run_cli(["secret", "list"])
        self.assertEqual(asked[0][0], "secret")
        self.assertIn("no secrets", out)


if __name__ == "__main__":
    unittest.main()
