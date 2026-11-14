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


class Vault(Base):
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

    def test_put_takes_the_project_of_the_folder(self):
        core.project_add(self.c, "site", path=self.dir.name)
        here = os.getcwd()
        os.chdir(self.dir.name)
        self.addCleanup(os.chdir, here)
        rc, out = self.run_cli(["secret", "put", "k"], b"v")
        self.assertEqual(rc, 0, out)
        self.assertEqual(vault.listing(self.c)["secrets"][0]["project"], "site")

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


class Grants(Base):
    """Sets and grants (#5822): a session uses a secret only when a grant of one of its sets covers it."""

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "site")
        core.project_add(self.c, "other")
        core.register(self.c, "mark", human=True)
        core.register(self.c, "dev")
        vault.put(self.c, "stripe-key", b"sk_live_1", None)  # a person saved it
        vault.set_change(self.c, "stripe-live", add=["stripe-key"], actor="mark")

    def use(self, actor):
        return vault.get(self.c, "stripe-key", actor, "pipe: test")

    def refused(self, actor):
        with self.assertRaises(RiverError) as e:
            self.use(actor)
        return str(e.exception)

    def hold(self, title, project="site", who="dev", **kw):
        iid = core.item_add(self.c, project, title, actor="t", **kw)["id"]
        core.claim(self.c, iid, who)
        return iid

    def test_no_grant_is_refused_and_names_the_set_and_the_manager(self):
        iid = self.hold("deploy")
        core.register(self.c, "manager-1")
        self.c.execute("UPDATE agents SET role='manager' WHERE name='manager-1'")
        with mock.patch.object(core, "active_manager", lambda conn, but=None: "manager-1"):
            msg = self.refused("dev")
        self.assertIn("the set stripe-live", msg)
        self.assertIn("maxpm ask manager-1", msg)
        self.assertIn(f"maxpm secret grant stripe-live --item {iid}", msg)
        self.assertEqual(vault.log(self.c, "stripe-key")["log"][0]["action"], "refused")
        self.assertEqual(self.use(None), b"sk_live_1")  # a person needs no grant
        self.assertEqual(self.use("mark"), b"sk_live_1")

    def test_project_grant_covers_a_session_on_its_task(self):
        g = vault.grant(self.c, "stripe-live", "project", "site", actor="mark")
        self.assertFalse(g["already"])
        self.refused("dev")  # holds nothing
        self.hold("charge", project="other")
        self.refused("dev")  # a task of another project
        self.c.execute("UPDATE items SET status='open', assignee=NULL WHERE assignee='dev'")
        self.hold("charge")
        self.assertEqual(self.use("dev"), b"sk_live_1")
        self.assertIn(f"grant {g['grant']}", vault.log(self.c, "stripe-key")["log"][0]["detail"])
        self.assertEqual(vault.grant(self.c, "stripe-live", "project", "site", actor="mark")["already"], True)
        vault.revoke(self.c, g["grant"], actor="mark")
        self.refused("dev")  # the next use after a revoke
        self.assertEqual(vault.grants(self.c)["grants"], [])
        self.assertIn("revoked", vault.grants(self.c, ended=True)["grants"][0]["ended"])

    def test_goal_grant_covers_owner_and_tagged_tasks_and_ends_with_the_goal(self):
        core.goal_add(self.c, "site", "launch", outcome="live")
        core.goal_add(self.c, "site", "billing", outcome="pay", parent="launch")
        vault.grant(self.c, "stripe-live", "goal", "launch", actor="mark")
        iid = self.hold("checkout page", goals=["billing"])  # a task of the sub-goal
        self.assertEqual(self.use("dev"), b"sk_live_1")
        core.register(self.c, "owner")
        core.goal_own(self.c, "launch", "owner")
        self.assertEqual(self.use("owner"), b"sk_live_1")
        self.c.execute("UPDATE goals SET status='complete' WHERE name='launch'")
        self.refused("dev")
        self.assertEqual(vault.grants(self.c, ended=True)["grants"][0]["ended"], "its goal is complete")
        del iid

    def test_task_kind_and_agent_grants_and_until(self):
        iid = self.hold("release", kind="operational")
        g = vault.grant(self.c, "stripe-live", "item", str(iid), actor="mark")
        self.assertEqual(self.use("dev"), b"sk_live_1")
        core.done(self.c, iid, "ok", "dev")
        self.refused("dev")  # a task grant ends with its task
        self.assertEqual(vault.grants(self.c, ended=True)["grants"][0]["ended"], "its task is closed")
        del g
        core.register(self.c, "ops")
        self.hold("cleanup", who="ops", kind="operational")
        vault.grant(self.c, "stripe-live", "kind", "operational", actor="mark")
        self.assertEqual(self.use("ops"), b"sk_live_1")
        self.refused("dev")  # holds no operational task now
        vault.revoke(self.c, set_name="stripe-live", scope="kind", value="operational", actor="mark")
        self.refused("ops")
        vault.grant(self.c, "stripe-live", "agent", "dev", until="1h", actor="mark")
        self.assertEqual(self.use("dev"), b"sk_live_1")
        self.c.execute("UPDATE secret_grants SET until='2000-01-01T00:00:00Z' WHERE scope='agent'")
        self.refused("dev")

    def test_only_a_person_changes_sets_and_grants(self):
        for f in (lambda: vault.set_change(self.c, "x", actor="dev"),
                  lambda: vault.grant(self.c, "stripe-live", "agent", "dev", actor="dev"),
                  lambda: vault.revoke(self.c, set_name="stripe-live", actor="dev")):
            with self.assertRaises(RiverError) as e:
                f()
            self.assertIn("only a person", str(e.exception))

    def test_replace_and_delete_need_a_grant_too(self):
        with self.assertRaises(RiverError):
            vault.put(self.c, "stripe-key", b"sk_other", "dev")
        with self.assertRaises(RiverError):
            vault.delete(self.c, "stripe-key", "dev")
        self.assertEqual(self.use(None), b"sk_live_1")
        vault.put(self.c, "own", b"v", "dev")  # a new name; the saver uses it while it is in no set
        self.assertEqual(vault.get(self.c, "own", "dev"), b"v")
        core.register(self.c, "dev2")
        with self.assertRaises(RiverError) as e:
            vault.get(self.c, "own", "dev2")
        self.assertIn("in no set", str(e.exception))

    def test_cli_set_grant_grants_revoke(self):
        vault.put(self.c, "stripe-pub", b"pk", None)
        rc, out = self.run_cli(["--as", "mark", "secret", "set", "stripe-live", "--add", "stripe-pub",
                                "--manager-may-grant", "on"])
        self.assertEqual(rc, 0, out)
        self.assertIn("stripe-key, stripe-pub", out)
        self.assertIn("the manager may grant it", out)
        rc, out = self.run_cli(["--as", "mark", "secret", "grant", "stripe-live", "--project", "site",
                                "--until", "7d", "--note", "billing work"])
        self.assertEqual(rc, 0, out)
        self.assertIn("set stripe-live to project site until", out)
        rc, out = self.run_cli(["secret", "grants"])
        self.assertIn("g1  set stripe-live to project site", out)
        rc, out = self.run_cli(["secret", "sets"])
        self.assertIn("1 grant(s)", out)
        rc, out = self.run_cli(["secret", "list"])
        self.assertIn("sets stripe-live", out)
        rc, out = self.run_cli(["--as", "mark", "secret", "revoke", "g1"])
        self.assertIn("revoked: g1", out)
        rc, out = self.run_cli(["secret", "grant", "stripe-live", "--agent", "dev"])
        self.assertEqual(rc, 2)
        self.assertIn("only a person", out)
        rc, out = self.run_cli(["--as", "mark", "secret", "set", "stripe-live", "--delete"])
        self.assertIn("deleted set stripe-live", out)
        self.assertEqual(vault.grants(self.c, ended=True)["grants"], [])


if __name__ == "__main__":
    unittest.main()
