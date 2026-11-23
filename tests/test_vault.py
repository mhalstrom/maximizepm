"""maxpm secret (#5757): save a value by name, give it to a command or a .env file, never print it."""

import contextlib
import io
import json
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


class FakeSecurity:
    """/usr/bin/security as the Keychain backend calls it: -i reads one command per line and cuts each line at 4096
    bytes (as macOS 26 does, measured in #5840); find-generic-password -w prints the stored password."""
    LINE = 4096

    def __init__(self):
        self.items = {}

    def __call__(self, args, stdin=None):
        import re
        import subprocess
        if args == ["-i"]:
            for line in stdin.decode().splitlines():
                line = line[:self.LINE]
                m = re.match(r'add-generic-password -U -s "([^"]*)" -a "([^"]*)" .* -X ([0-9a-fA-F]*)$', line)
                if m:
                    hx = m.group(3)[:len(m.group(3)) // 2 * 2]
                    self.items[(m.group(1), m.group(2))] = bytes.fromhex(hx).decode()
                m = re.match(r'delete-generic-password -s "([^"]*)" -a "([^"]*)"$', line)
                if m:
                    self.items.pop((m.group(1), m.group(2)), None)
            return subprocess.CompletedProcess(args, 0, b"", b"")
        if args[0] == "find-generic-password":
            v = self.items.get((args[2], args[4]))
            return subprocess.CompletedProcess(args, 0 if v is not None else 44, (v + "\n").encode() if v else b"", b"")
        raise AssertionError(args)


class KeychainParts(Base):
    """#5840: mark's Allow failed with 'Invalid base64-encoded string: number of data characters (1997)': security -i
    cut the line of a 2 KB browser sign-in. A long value is stored in parts now."""

    def setUp(self):
        super().setUp()
        self.sec = FakeSecurity()
        self.k = vault.Keychain()
        p = mock.patch.object(vault.Keychain, "_run", lambda _self, args, stdin=None: self.sec(args, stdin))
        p.start()
        self.addCleanup(p.stop)

    def test_long_values_of_every_padding_come_back_whole(self):
        for n in (10, 899, 900, 901, 1499, 1500, 1501, 4000, 4001, 4002, 60000):  # base64 ends in ==, = and none
            data = os.urandom(n)
            self.k.put(self.c, "gmail", data)
            self.assertEqual(self.k.get(self.c, "gmail"), data, n)
        self.k.put(self.c, "gmail", b"short")  # a shorter value removes the old parts
        self.assertEqual(self.k.get(self.c, "gmail"), b"short")
        self.assertEqual([a for _s, a in self.sec.items], ["gmail"])
        self.k.put(self.c, "gmail", os.urandom(5000))
        self.k.delete(self.c, "gmail")
        self.assertEqual(self.sec.items, {})

    def test_a_value_cut_before_is_a_refusal_that_names_its_length_only(self):
        svc = self.k.service(self.c)
        self.sec.items[(svc, "old")] = "b64:" + "QUJD" * 499 + "Q"  # 1997 characters, as mark's
        with self.assertRaises(RiverError) as e:
            self.k.get(self.c, "old")
        self.assertIn("a cut value of the secret old (1997 base64 characters): save it again", str(e.exception))
        self.assertNotIn("QUJD", str(e.exception))


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
        vault.set_level(self.c, "google-client-secret", env=True)  # a person allows the file (#5825)
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

    def manager(self):
        core.register(self.c, "manager-1")
        self.c.execute("UPDATE agents SET role='manager' WHERE name='manager-1'")
        p = mock.patch.object(core, "active_manager", lambda conn, but=None: "manager-1")
        p.start()
        self.addCleanup(p.stop)

    def alerts(self, to):
        return [r["body"] for r in self.c.execute("SELECT body FROM messages WHERE kind='alert' AND to_agent=? "
                                                  "ORDER BY id", (to,))]

    def test_no_grant_is_refused_and_asks_the_person_for_an_unmarked_set(self):
        iid = self.hold("deploy")
        self.manager()
        msg = self.refused("dev")
        self.assertIn("the set stripe-live", msg)
        self.assertIn(f"MaximizePM asked mark for the grant (maxpm secret grant stripe-live --item {iid})", msg)
        self.assertEqual(vault.log(self.c, "stripe-key")["log"][0]["action"], "refused")
        self.assertEqual(len(self.alerts("mark")), 1)  # the set is not marked: the person, not the manager
        self.assertEqual(self.alerts("manager-1"), [])
        self.assertIn("asked mark for the grant already", self.refused("dev"))
        self.assertEqual(len(self.alerts("mark")), 1)  # once an hour
        self.assertEqual(self.use(None), b"sk_live_1")  # a person needs no grant
        self.assertEqual(self.use("mark"), b"sk_live_1")

    def test_the_manager_grants_only_the_sets_the_person_marked(self):
        self.manager()
        iid = self.hold("deploy")
        with self.assertRaises(RiverError) as e:
            vault.grant(self.c, "stripe-live", "item", str(iid), actor="manager-1")
        self.assertIn("answers each request for stripe-key (Ask me): only a person may grant the set stripe-live",
                      str(e.exception))
        vault.set_change(self.c, "stripe-live", manager_may_grant=True, actor="mark")
        self.assertEqual(vault.listing(self.c)["secrets"][0]["ask"], "allow")  # the mark is on each credential
        with self.assertRaises(RiverError):
            vault.set_change(self.c, "stripe-live", manager_may_grant=False, actor="manager-1")  # the mark is the person's
        with self.assertRaises(RiverError):
            vault.set_ask(self.c, "stripe-key", "ask", actor="manager-1")
        self.refused("dev")
        self.assertIn("the person allows it without asking", self.alerts("manager-1")[0])
        g = vault.grant(self.c, "stripe-live", "item", str(iid), actor="manager-1")
        self.assertEqual(g["granted_by"], "manager-1")
        self.assertEqual(self.use("dev"), b"sk_live_1")
        lines = vault.grant_summary(self.c)
        self.assertEqual(lines, [f"granted by manager-1: g{g['id']} set stripe-live to task #{iid}"])
        note = self.c.execute("SELECT body FROM messages WHERE kind='note' AND to_agent='mark'").fetchone()
        self.assertIn(f"g{g['id']} set stripe-live to task #{iid}", note["body"])
        self.assertIsNone(vault.grant_summary(self.c))  # once a day
        vault.revoke(self.c, g["grant"], actor="manager-1")
        with self.assertRaises(RiverError):
            vault.grant(self.c, "stripe-live", "item", str(iid), actor="dev")  # a worker never grants

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


class Identity(Base):
    """#5823: a credential check confirms the session from the processes above the command, never from --as."""

    def setUp(self):
        super().setUp()
        old = vault.IDENTITY
        vault.IDENTITY = None
        self.addCleanup(setattr, vault, "IDENTITY", old)
        core.register(self.c, "mark", human=True)
        core.register(self.c, "dev")
        core.register(self.c, "other")
        self.c.execute("UPDATE agents SET pid=500, pid_start=111, host=? WHERE name='dev'", (core.this_host(),))
        self.c.execute("UPDATE agents SET pid=600, pid_start=222, host=? WHERE name='other'", (core.this_host(),))
        self.chain = None
        # #6970: keep process-table probes off the host's real processes (outside a sandbox, report --hook runs
        # _record_hook_process -> pid_alive / find_agent_process / proc_tty, which overwrote dev's fake pid 500).
        for target, fn in (
            ("river.peer.chain", lambda pid: self.chain),
            ("river.peer.holder", lambda lp, rp, hints=(): None),
            ("river.peer.lookable", lambda lp, rp: False),
            ("river.core.pid_alive", lambda pid: False),
            ("river.core.find_agent_process", lambda conn, name, start=None, env=None: (None, None)),
            ("river.core.proc_info", lambda pid: None),
            ("river.core.proc_tty", lambda pid: None),
        ):
            p = mock.patch(target, fn)
            p.start()
            self.addCleanup(p.stop)

    def who(self, actor):
        return vault.session_of(self.c, actor)

    def refused(self, actor):
        with self.assertRaises(RiverError) as e:
            self.who(actor)
        return str(e.exception)

    def test_the_page_through_the_relay_is_the_person(self):
        """#5840: mark's Allow on his phone came through the relay, whose connection serve's own thread holds: no
        process tells, and the check refused it. The relay's header secret marks it as the person's page act."""
        from river import relay, server
        vault.PEER.value = vault.RELAY_PAGE
        self.addCleanup(setattr, vault.PEER, "value", None)
        self.assertEqual(self.who("mark"), {"name": "mark", "person": True, "how": "the page through the relay"})
        self.assertIn("acts as a person, not as the session dev", self.refused("dev"))
        vault.PEER.value = (None, None, None)  # any other request that no process holds is still refused
        self.assertIn("cannot read which session", self.refused("mark"))
        self.assertTrue(relay.page_request({"X-Maxpm-Relay": relay.PAGE_SECRET}))
        self.assertFalse(relay.page_request({"X-Maxpm-Relay": "guess"}))
        self.assertIn("relay.page_request(self.headers)", Path(server.__file__).read_text(encoding="utf-8"))

    def test_the_page_over_tailscale_is_the_person(self):
        """#5869: mark's Fill on his iPhone over Tailscale was refused ('cannot read which session'): Tailscale's
        network extension runs as root and holds the connection, so no process of this user does. A session that
        writes the header itself holds its own connection, and the check finds that session."""
        vault.PEER.value = (vault.TAILNET_PAGE, 61000, 8765, "mark@example.com")
        self.addCleanup(setattr, vault.PEER, "value", None)
        holder = {"pid": None}
        for name, fn in (("holder", lambda lp, rp, hints=(): holder["pid"]), ("lookable", lambda lp, rp: True)):
            p = mock.patch(f"river.peer.{name}", fn)
            p.start()
            self.addCleanup(p.stop)
        self.assertEqual(self.who("mark"), {"name": "mark", "person": True,
                                            "how": "the page over Tailscale (mark@example.com)"})
        self.assertIn("acts as a person, not as the session dev", self.refused("dev"))
        holder["pid"] = 900  # a session wrote Tailscale-User-Login itself: its own process holds the connection
        self.chain = [(900, 500, "curl", 1), (500, 1, "2.1.325", 111)]
        self.assertEqual(self.who(None)["name"], "dev")
        self.assertIn("runs in the session dev, not mark", self.refused("mark"))
        holder["pid"], self.chain = None, None
        with mock.patch("river.peer.lookable", lambda lp, rp: False):  # the kernel does not tell: refused
            self.assertIn("cannot read which session", self.refused("mark"))

    def test_serve_marks_a_request_that_tailscale_forwarded(self):
        from river import server
        from tests.test_tailnet import Fake, NAME, use
        seen = {}
        server.OPS["peer_probe"] = lambda c, a, who: seen.update(peer=vault.PEER.value) or {}
        self.addCleanup(server.OPS.pop, "peer_probe")
        use(self, Fake())
        raw = json.dumps({"op": "peer_probe", "actor": "mark", "args": {}}).encode()
        h = server.Handler.__new__(server.Handler)
        h.path, h.client_address, h.rfile, out = "/api/action", ("127.0.0.1", 61000), io.BytesIO(raw), {}
        h.server = mock.Mock(server_address=("127.0.0.1", 8765))
        h.headers = {"Content-Length": str(len(raw)), "Host": NAME, "Origin": f"https://{NAME}",
                     "Tailscale-User-Login": "mark@example.com", "X-Forwarded-For": "100.64.0.2"}
        h._send = lambda code, body, ctype=None: out.update(code=code, body=body)
        h.do_POST()
        self.assertEqual(out["code"], 200, out)
        self.assertEqual(seen["peer"], (vault.TAILNET_PAGE, 61000, 8765, "mark@example.com"))

    def test_the_session_above_the_command_is_who_asks(self):
        self.chain = [(900, 800, "Python", 1), (800, 500, "zsh", 1), (500, 400, "2.1.325", 111), (400, 1, "tmux", 1)]
        self.assertEqual(self.who("dev")["name"], "dev")
        self.assertEqual(self.who(None), {"name": "dev", "person": False, "how": "process 500 (2.1.325)"})
        self.assertIn("runs in the session dev, not other", self.refused("other"))
        self.assertIn("not mark", self.refused("mark"))  # a session cannot type a person's name

    def test_a_reused_pid_is_not_the_session(self):
        self.chain = [(900, 500, "Python", 1), (500, 1, "2.1.325", 999)]  # pid 500 again, another start
        self.assertIn("recorded no session for that process", self.refused("dev"))

    def test_a_person_has_no_agent_above(self):
        self.chain = [(900, 800, "Python", 1), (800, 700, "zsh", 1), (700, 1, "login", 1)]
        self.assertTrue(self.who(None)["person"])
        self.assertTrue(self.who("mark")["person"])
        self.assertIn("no process of the session dev", self.refused("dev"))

    def test_an_unrecorded_agent_and_an_unread_chain_are_refused(self):
        self.chain = [(900, 800, "Python", 1), (800, 1, "codex", 5)]
        self.assertIn("run maxpm go in the session first", self.refused("mark"))
        self.chain = None  # a sandbox hides the parents
        self.assertIn("cannot read which session", self.refused(None))

    def test_serve_reads_the_holder_of_the_connection(self):
        seen = {}

        def holder(lport, rport, hints):
            seen["args"] = (lport, rport, list(hints))
            return 500
        self.chain = [(500, 1, "2.1.325", 111)]  # the sandbox proxy inside the session's own process
        with mock.patch("river.peer.holder", holder):
            vault.PEER.value = (51000, 7766, "900")
            try:
                self.assertEqual(self.who("dev")["name"], "dev")
            finally:
                vault.PEER.value = None
        self.assertEqual(seen["args"][:2], (51000, 7766))
        self.assertEqual(seen["args"][2][0], "900")  # the CLI's pid first, then the recorded sessions'
        self.assertIn(500, seen["args"][2])

    def test_grants_follow_the_confirmed_session(self):
        vault.BACKEND.put(self.c, "k", b"v")
        self.c.execute("INSERT INTO secrets (name, created_at) VALUES ('k', 'x')")
        self.chain = [(700, 1, "login", 1)]
        vault.set_change(self.c, "s", add=["k"])  # a person
        vault.grant(self.c, "s", "agent", "other")
        self.chain = [(900, 500, "Python", 1), (500, 1, "2.1.325", 111)]
        with self.assertRaises(RiverError) as e:
            vault.get(self.c, "k", "other")  # dev types the name of the session that has the grant
        self.assertIn("runs in the session dev", str(e.exception))
        with self.assertRaises(RiverError):
            vault.grant(self.c, "s", "agent", "dev", actor="mark")  # dev types the person's name
        self.chain = [(900, 600, "Python", 1), (600, 1, "codex", 222)]
        self.assertEqual(vault.get(self.c, "k", "other"), b"v")

    def test_set_process_keeps_the_start_and_a_live_session_keeps_its_process(self):
        me = os.getpid()
        core.set_process(self.c, "dev", me, "python", env={})
        r = self.c.execute("SELECT pid, pid_start FROM agents WHERE name='dev'").fetchone()
        self.assertEqual(r["pid"], me)
        from river import peer
        self.assertEqual(r["pid_start"], (peer.info(me) or (0, 0, "", None))[3])
        if r["pid_start"] is not None:  # the kernel answers here: another process cannot take dev's place
            self.assertFalse(core.set_process(self.c, "dev", os.getppid(), "zsh", env={"MAXPM_AGENT": "dev"}))

    def test_bang_commands_parses_wrappers_and_chains(self):
        """#6912: extract exact maxpm argvs from a person's '!' chat input, stripping env/wrappers."""
        self.assertEqual(
            vault.bang_commands("! maxpm secret grant toolscaled-services-dev --item 6910"),
            [("secret", "grant", "toolscaled-services-dev", "--item", "6910")],
        )
        self.assertEqual(
            vault.bang_commands("! env -u MAXPM_TOKEN maxpm secret set s --add k"),
            [("secret", "set", "s", "--add", "k")],
        )
        self.assertEqual(
            vault.bang_commands("  ! MAXPM_AGENT= maxpm secret set s --add k && maxpm secret grant s --item 1"),
            [("secret", "set", "s", "--add", "k"), ("secret", "grant", "s", "--item", "1")],
        )
        self.assertEqual(vault.bang_commands("maxpm secret grant s --item 1"), [])
        self.assertEqual(vault.bang_commands("! git status"), [])
        self.assertEqual(vault.bang_commands("!"), [])

    def test_person_bang_allows_grant_once_and_refuses_agent_bash(self):
        """#6912: mark typing '! maxpm secret grant ...' in a session's chat counts as mark once; the agent's own
        Bash call with the exact same command is refused."""
        vault.clear_bangs()
        self.addCleanup(vault.clear_bangs)
        core.project_add(self.c, "site")
        it = core.item_add(self.c, "site", "Use secret", actor="mark")["id"]
        vault.BACKEND.put(self.c, "k", b"v")
        self.c.execute("INSERT INTO secrets (name, created_at) VALUES ('k', 'x')")
        self.chain = [(700, 1, "login", 1)]
        vault.set_change(self.c, "devset", add=["k"], actor="mark")

        # Agent dev tries to grant the set from its Bash tool: refused.
        argv = ("secret", "grant", "devset", "--item", str(it))
        self.chain = [(900, 500, "Python", 10), (500, 1, "2.1.325", 111)]
        vault.PEER.value = (51000, 7766, "900")
        vault.PEER.argv = argv
        vault.PEER.bang = None
        self.addCleanup(setattr, vault.PEER, "value", None)
        self.addCleanup(setattr, vault.PEER, "argv", None)
        self.addCleanup(setattr, vault.PEER, "bang", None)
        with mock.patch("river.peer.holder", lambda lp, rp, hints=(): 900):
            with self.assertRaisesRegex(RiverError, "only a person or the manager"):
                vault.grant(self.c, "devset", "item", str(it), actor="dev")

            # Mark types '! maxpm secret grant devset --item <it>' in dev's chat: succeeds as mark.
            self.assertEqual(vault.record_bang("dev", f"! maxpm secret grant devset --item {it}", person="mark"), 1)
            g = vault.grant(self.c, "devset", "item", str(it), actor="dev")
            self.assertEqual(g["granted_by"], "mark")

            # Now dev runs the exact same command from its Bash tool (new process pid 901): refused!
            vault.PEER.bang = None
            self.chain = [(901, 500, "Python", 20), (500, 1, "2.1.325", 111)]
            with mock.patch("river.peer.holder", lambda lp, rp, hints=(): 901):
                with self.assertRaisesRegex(RiverError, "only a person or the manager"):
                    vault.grant(self.c, "devset", "item", str(it), actor="dev")

            # Wrong session or wrong argv is also refused.
            vault.record_bang("other", f"! maxpm secret grant devset --item {it}", person="mark")
            vault.record_bang("dev", "! maxpm secret list", person="mark")
            with mock.patch("river.peer.holder", lambda lp, rp, hints=(): 901):
                with self.assertRaisesRegex(RiverError, "only a person or the manager"):
                    vault.grant(self.c, "devset", "item", str(it), actor="dev")

    def test_hook_route_removed_and_agent_cannot_forge_bang(self):
        """#6912: UserPromptSubmit never records a person bang allowance, and no person_bang op exists for an agent to
        POST directly."""
        from river import server
        vault.clear_bangs()
        self.addCleanup(vault.clear_bangs)
        self.assertNotIn("person_bang", server.OPS)
        # Running maxpm report --hook claude with a '!' prompt (e.g. from a script file) records no bang.
        payload = json.dumps({"hook_event_name": "UserPromptSubmit",
                              "prompt": "! env -u MAXPM_TOKEN maxpm secret set s2 --add k2"})
        with mock.patch.dict(os.environ, {"MAXPM_AGENT": "dev"}), \
                mock.patch("sys.stdin", io.StringIO(payload)):
            self.assertEqual(cli.run(["report", "--hook", "claude"]), 0)
        self.assertIsNone(vault.claim_bang("dev", ("secret", "set", "s2", "--add", "k2")))
        self.assertEqual(tuple(self.c.execute("SELECT pid, pid_start FROM agents WHERE name='dev'").fetchone()),
                         (500, 111))

        # An agent calling session_send directly (without PAGE_TOKEN / as non-person) is refused by server.
        raw = json.dumps({"op": "session_send", "actor": "dev",
                          "args": {"agent": "dev", "text": "! maxpm secret grant s2 --item 1"}}).encode()
        h = server.Handler.__new__(server.Handler)
        h.path, h.client_address, h.rfile, out = "/api/action", ("127.0.0.1", 61001), io.BytesIO(raw), {}
        h.server = mock.Mock(server_address=("127.0.0.1", 8765))
        h.headers = {"Content-Length": str(len(raw)), "Host": "127.0.0.1:8765"}
        h._send = lambda code, body, ctype=None: out.update(code=code, body=body)
        h.do_POST()
        self.assertEqual(out["code"], 403)
        self.assertIsNone(vault.claim_bang("dev", ("secret", "grant", "s2", "--item", "1")))

        # bang_claim_op works only for a bang recorded from the person's page (session_send), once per process.
        vault.record_bang("dev", "! env -u MAXPM_TOKEN maxpm secret set s2 --add k2", person="mark")
        argv = ["secret", "set", "s2", "--add", "k2"]
        self.chain = [(920, 500, "Python", 40), (500, 1, "2.1.325", 111)]
        vault.PEER.value = (51001, 7766, "920")
        self.addCleanup(setattr, vault.PEER, "value", None)
        with mock.patch("river.peer.holder", lambda lp, rp, hints=(): 920):
            r1 = server.OPS["bang_claim"](self.c, {"argv": argv}, "dev")
            self.assertTrue(r1["ok"])
            self.assertEqual(r1["person"], "mark")
            self.assertTrue(server.OPS["bang_claim"](self.c, {"argv": argv}, "dev")["ok"])
        self.chain = [(921, 500, "Python", 50), (500, 1, "2.1.325", 111)]
        with mock.patch("river.peer.holder", lambda lp, rp, hints=(): 921):
            self.assertFalse(server.OPS["bang_claim"](self.c, {"argv": argv}, "dev")["ok"])
        vault.record_bang("dev", "! maxpm secret set s2 --add k2", person="mark", ttl=-1)
        with mock.patch("river.peer.holder", lambda lp, rp, hints=(): 921):
            self.assertFalse(server.OPS["bang_claim"](self.c, {"argv": argv}, "dev")["ok"])

    def test_session_send_records_person_bang_but_not_manager(self):
        """#6912: typing '! maxpm ...' in the Sessions tab (server.session_send) records a one-shot person bang;
        manager sends never do."""
        from river import server
        vault.clear_bangs()
        self.addCleanup(vault.clear_bangs)
        with mock.patch.object(server, "_typing_pane", lambda *a, **k: "%1"), \
                mock.patch.object(server, "_no_question", lambda c, ag, p: p), \
                mock.patch.object(server, "send_keys", lambda *a, **k: None), \
                mock.patch.object(server, "_keep_prompt", lambda *a, **k: None), \
                mock.patch.object(server, "ENTER_PAUSE", 0):
            server.session_send(self.c, "dev", "! maxpm secret grant s --item 1", who="mark", manager=False)
            hit = vault.claim_bang("dev", ("secret", "grant", "s", "--item", "1"))
            self.assertEqual(hit, {"person": "mark"})
            self.assertIsNone(vault.claim_bang("dev", ("secret", "grant", "s", "--item", "1")))
            server.session_send(self.c, "dev", "! maxpm secret grant s --item 1", who="boss", manager=True)
            self.assertIsNone(vault.claim_bang("dev", ("secret", "grant", "s", "--item", "1")))

    def test_cli_bang_ask_and_allow_deny_dialog_and_direct_post_refused(self):
        """#6912: a CLI '!' command queues an Allow/Deny dialog (bang_ask); nothing runs until the person clicks Allow
        (bang_decide, ONLY_PERSON). An agent POSTing bang_decide directly or running the hook from a script is refused."""
        from river import rights, server
        vault.clear_bangs()
        self.addCleanup(vault.clear_bangs)
        self.assertIs(rights.OPS["bang_decide"], rights.ONLY_PERSON)

        # Person's '!' in dev's terminal pane queues a refused command via bang_ask_op (process tree confirms dev).
        argv = ["secret", "grant", "devset", "--item", "6910"]
        pane_bang = "! maxpm secret grant devset --item 6910\n"
        self.chain = [(930, 500, "Python", 60), (500, 1, "2.1.325", 111)]
        vault.PEER.value = (51002, 7766, "930")
        self.addCleanup(setattr, vault.PEER, "value", None)
        with mock.patch("river.peer.holder", lambda lp, rp, hints=(): 930), \
                mock.patch.object(server, "agent_terminals", lambda conn: {"dev": "%9"}), \
                mock.patch.object(server, "_tmux", lambda *a, **kw: pane_bang):
            q = server.OPS["bang_ask"](self.c, {"argv": argv, "cwd": "/tmp"}, "dev")
            self.assertTrue(q["queued"])
            ask_id = q["ask"]["id"]
            self.assertEqual(q["ask"]["command"], "maxpm secret grant devset --item 6910")
            # Queuing grants NOTHING: no bang allowance exists for dev.
            self.assertIsNone(vault.claim_bang("dev", tuple(argv)))
            self.assertEqual(vault.list_bang_asks(),
                             [{"id": ask_id, "agent": "dev", "command": "maxpm secret grant devset --item 6910"}])

            # Agent Bash call POSTing bang_decide directly to /api/action is refused (409, ONLY_PERSON).
            raw = json.dumps({"op": "bang_decide", "actor": "dev",
                              "args": {"id": ask_id, "allow": True}}).encode()
            h = server.Handler.__new__(server.Handler)
            h.path, h.client_address, h.rfile, out = "/api/action", ("127.0.0.1", 51002), io.BytesIO(raw), {}
            h.server = mock.Mock(server_address=("127.0.0.1", 7766))
            h.headers = {"Content-Length": str(len(raw)), "Host": "127.0.0.1:7766", "X-Maxpm-Pid": "930"}
            h._send = lambda code, body, ctype=None: out.update(code=code, body=body)
            h.do_POST()
            self.assertEqual(out["code"], 409)
            self.assertIn("refused", out["body"]["error"])
            # Request is still pending (agent could not consume it).
            self.assertEqual(len(vault.list_bang_asks()), 1)

            # Direct call to bang_decide_op from an agent process is also refused by vault.session_of.
            with self.assertRaisesRegex(RiverError, "only a person"):
                server.OPS["bang_decide"](self.c, {"id": ask_id, "allow": True}, "dev")

        # Person clicks Allow on the page: runs exact argv with agent env stripped, shell=False.
        self.chain = [(700, 1, "login", 1)]
        vault.PEER.value = vault.RELAY_PAGE
        ran = {}

        def fake_run(cmd, **kw):
            ran["cmd"], ran["env"], ran["cwd"] = cmd, kw.get("env"), kw.get("cwd")
            return mock.Mock(returncode=0, stdout="granted g1\n", stderr="")

        with mock.patch("subprocess.run", fake_run), \
                mock.patch.dict(os.environ, {"MAXPM_TOKEN": "tok", "MAXPM_AGENT": "dev", "CLAUDECODE": "1"}):
            res = server.OPS["bang_decide"](self.c, {"id": ask_id, "allow": True}, "mark")
        self.assertTrue(res["allowed"])
        self.assertEqual(res["output"], "granted g1")
        self.assertEqual(ran["cmd"][1:], ["-m", "river.cli", *argv])
        self.assertNotIn("MAXPM_TOKEN", ran["env"])
        self.assertNotIn("MAXPM_AGENT", ran["env"])
        self.assertNotIn("CLAUDECODE", ran["env"])
        self.assertEqual(vault.list_bang_asks(), [])

        # Person clicks Deny: runs nothing and removes the ask.
        self.chain = [(930, 500, "Python", 60), (500, 1, "2.1.325", 111)]
        vault.PEER.value = (51002, 7766, "930")
        with mock.patch("river.peer.holder", lambda lp, rp, hints=(): 930), \
                mock.patch.object(server, "agent_terminals", lambda conn: {"dev": "%9"}), \
                mock.patch.object(server, "_tmux", lambda *a, **kw: pane_bang):
            ask2 = server.OPS["bang_ask"](self.c, {"argv": argv}, "dev")["ask"]["id"]
        vault.PEER.value = vault.RELAY_PAGE
        with mock.patch("subprocess.run") as mrun:
            res2 = server.OPS["bang_decide"](self.c, {"id": ask2, "allow": False}, "mark")
            self.assertFalse(res2["allowed"])
            mrun.assert_not_called()
        self.assertEqual(vault.list_bang_asks(), [])

        # Running maxpm report --hook claude from a script file records no bang allowance.
        import subprocess
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tf:
            tf.write(
                "import io, json, os, sys\n"
                "from river import cli\n"
                "os.environ['MAXPM_AGENT'] = 'dev'\n"
                "sys.stdin = io.StringIO(json.dumps({'hook_event_name': 'UserPromptSubmit', "
                "'prompt': '! maxpm secret grant devset --item 6910'}))\n"
                "sys.exit(cli.run(['report', '--hook', 'claude']))\n"
            )
            script_path = tf.name
        self.addCleanup(lambda: os.path.exists(script_path) and os.unlink(script_path))
        repo = str(Path(core.__file__).resolve().parent.parent)
        r = subprocess.run([sys.executable, script_path], capture_output=True, text=True,
                           env={**os.environ, "MAXPM_DB": str(core.db_path()), "PYTHONPATH": repo}, timeout=15)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(vault.claim_bang("dev", tuple(argv)))

    def test_agent_refusal_queues_no_bang_ask_and_done_mistakes_never_ask(self):
        """#6934: an agent's own refused maxpm command (person-only refusal, done --changed 'for a person', or wrong
        --commit) must never queue a bang_ask dialog; only a command shown as '! ...' in the session's terminal pane
        queues one, and a queued ask drops when the same command later succeeds or expires."""
        from river import cli, server
        vault.clear_bangs()
        self.addCleanup(vault.clear_bangs)
        core.project_add(self.c, "site")
        it = core.item_add(self.c, "site", "ship something")["id"]
        core.claim(self.c, it, "dev")

        # 1) Agent's own done mistakes (--changed with path/too long -> CHANGED_RULE says 'for a person',
        #    and wrong --commit) must never call bang_ask.
        asked = []
        fake_ask = lambda conn, op, payload, **kw: asked.append((op, payload)) or {"queued": True}
        def fake_commits(folder, item_id, given=None):
            if given:
                raise RiverError(f"--commit: unknown revision {given[0]!r} (in {folder}); name commits of the project's git folder")
            return []
        with mock.patch.object(cli, "_inside_agent", lambda: True), \
                mock.patch.object(core, "running_serve", lambda conn: True), \
                mock.patch.object(core, "_git_folder", lambda conn, pid: "/tmp/repo"), \
                mock.patch.object(core, "proof_commits", fake_commits), \
                mock.patch.object(cli, "ask_server", fake_ask):
            for argv in (
                ["--as", "dev", "done", str(it), "--output", "done",
                 "--changed", "Added GET /v1/llm-catalog/revisions endpoint for updates"],
                ["--as", "dev", "done", str(it), "--output", "done",
                 "--changed", " ".join(["word"] * 35)],
                ["--as", "dev", "done", str(it), "--commit", "715dd34", "--output", "done"],
            ):
                with mock.patch.object(sys, "argv", ["maxpm", *argv]):
                    with self.assertRaises(RiverError):
                        cli.run(argv)
        self.assertEqual(asked, [])
        self.assertEqual(vault.list_bang_asks(), [])

        # 1b) An actual agent subprocess getting a person-only refusal prints no dialog notice and queues no bang_ask.
        import subprocess
        repo = str(Path(core.__file__).resolve().parent.parent)
        sub = subprocess.run(
            [sys.executable, "-m", "river.cli", "--as", "mark", "list"],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "MAXPM_DB": str(core.db_path()), "PYTHONPATH": repo,
                 "CLAUDECODE": "1", "MAXPM_AGENT": "dev"},
        )
        self.assertEqual(sub.returncode, 2, sub.stderr)
        self.assertIn("never runs as a person", sub.stderr)
        self.assertNotIn("Allow/Deny dialog", sub.stderr)
        self.assertEqual(vault.list_bang_asks(), [])

        # 2) Agent subprocess hitting a person-only refusal with no '!' in its terminal pane queues no bang_ask
        #    (both with no tmux pane and with a pane showing an agent Bash tool call).
        argv = ["secret", "grant", "devset", "--item", str(it)]
        self.chain = [(930, 500, "Python", 60), (500, 1, "2.1.325", 111)]
        vault.PEER.value = (51002, 7766, "930")
        self.addCleanup(setattr, vault.PEER, "value", None)
        with mock.patch("river.peer.holder", lambda lp, rp, hints=(): 930):
            with mock.patch.object(server, "agent_terminals", lambda conn: {}):
                q = server.OPS["bang_ask"](self.c, {"argv": argv, "cwd": "/tmp"}, "dev")
                self.assertFalse(q["queued"])
            with mock.patch.object(server, "agent_terminals", lambda conn: {"dev": "%9"}), \
                    mock.patch.object(server, "_tmux", lambda *a, **kw: "● Bash(maxpm secret grant devset --item "
                                                                        f"{it})\n  ⎿  Running...\n"):
                q = server.OPS["bang_ask"](self.c, {"argv": argv, "cwd": "/tmp"}, "dev")
                self.assertFalse(q["queued"])
        self.assertEqual(vault.list_bang_asks(), [])

        # 3) A person's '!' in the session's terminal pane proves it and opens the dialog.
        with mock.patch("river.peer.holder", lambda lp, rp, hints=(): 930), \
                mock.patch.object(server, "agent_terminals", lambda conn: {"dev": "%9"}), \
                mock.patch.object(server, "_tmux", lambda *a, **kw: f"! maxpm secret grant devset --item {it}\n"):
            q = server.OPS["bang_ask"](self.c, {"argv": argv, "cwd": "/tmp"}, "dev")
            self.assertTrue(q["queued"])
        self.assertEqual(len(vault.list_bang_asks(self.c)), 1)

        # 4) If the same command later succeeds for that agent, the queued ask is dropped.
        later = core.iso(core.now() + core.timedelta(seconds=2))
        self.c.execute("INSERT INTO runs(at, actor, cmd, words, exit) VALUES (?,?,?,?,0)",
                       (later, "dev", "secret", json.dumps(argv)))
        self.c.commit()
        self.assertEqual(vault.list_bang_asks(self.c), [])

        # 5) Expired asks are dropped.
        vault.ask_bang("dev", argv, cwd="/tmp", ttl=-1)
        self.assertEqual(vault.list_bang_asks(self.c), [])


class CredentialsPanel(Base):
    """#5829: the Credentials panel of Settings reads names, levels, sets and grants in one GET, never a value; its
    changes are op secret, as the person on the page."""

    class Handler:
        def __init__(self, path):
            self.path, self.sent = path, None

        def _send(self, code, body, *a, **k):
            self.sent = (code, body)

    def test_the_read_shows_names_and_never_a_value_and_the_page_s_actions_work(self):
        import base64
        import json
        core.project_add(self.c, "site")
        core.register(self.c, "mark", human=True)
        act = routes_secrets.act
        act(self.c, {"action": "signin", "name": "google-mark", "site": "https://accounts.google.com/x",
                     "account": "me@gmail.com", "value_b64": base64.b64encode("pässwörd-123".encode()).decode()}, "mark")
        vault.put(self.c, "cf-token", TOKEN, None)
        act(self.c, {"action": "set", "name": "google"}, "mark")
        act(self.c, {"action": "set", "name": "google", "add": ["google-mark"]}, "mark")
        out = act(self.c, {"action": "asking", "name": "google-mark", "ask": "allow"}, "mark")  # #5842: When asked
        self.assertEqual((out["ask"], out["changed"]), ("allow", True))
        with self.assertRaises(RiverError):
            act(self.c, {"action": "asking", "name": "google-mark", "ask": "maybe"}, "mark")
        g = act(self.c, {"action": "grant", "name": "google", "scope": "project", "value": "site", "until": None}, "mark")
        act(self.c, {"action": "level", "name": "cf-token", "level": "locked"}, "mark")
        h = self.Handler("/api/credentials")
        self.assertTrue(any(r[0]("/api/credentials") and r[1] is routes_secrets.credentials for r in routes_secrets.GET))
        routes_secrets.credentials(h, h.path)
        code, body = h.sent
        self.assertEqual(code, 200)
        text = json.dumps(body, ensure_ascii=False)
        for secret in ("pässwörd-123", TOKEN.decode(), base64.b64encode(TOKEN).decode()):
            self.assertNotIn(secret, text)
        rows = {r["name"]: r for r in body["credentials"]}
        self.assertEqual((rows["google-mark"]["kind"], rows["google-mark"]["account"], rows["google-mark"]["sets"]),
                         ("signin", "me@gmail.com", ["google"]))
        self.assertEqual(rows["cf-token"]["level"], "locked")
        self.assertEqual((rows["google-mark"]["ask"], rows["cf-token"]["ask"]), ("allow", "ask"))
        self.assertEqual([(s["name"], s["manager_may_grant"]) for s in body["sets"]], [("google", True)])
        self.assertEqual([x["grant"] for x in body["grants"]], [g["grant"]])
        self.assertEqual(body["levels"], list(vault.LEVELS))
        act(self.c, {"action": "revoke", "grant": g["id"]}, "mark")
        routes_secrets.credentials(h, h.path)
        self.assertEqual(h.sent[1]["grants"], [])

    def test_the_read_lists_the_session_browsers_that_run(self):
        """#5840: the panel saves a session browser's sign-in; it offers only the browsers that serve runs now."""
        from river import browser

        class B:
            def __init__(self, on, proxy=None):
                self.on, self.proxy = on, proxy

            def running(self):
                return self.on

        self.addCleanup(lambda saved=dict(browser.BROWSERS): (browser.BROWSERS.clear(), browser.BROWSERS.update(saved)))
        browser.BROWSERS.clear()
        browser.BROWSERS.update({("site", "dev-2"): B(True), ("site", "dev-1"): B(False, proxy=object()),
                                 ("site", "gone"): B(False), ("site", None): B(True)})
        h = self.Handler("/api/credentials")
        routes_secrets.credentials(h, h.path)
        self.assertEqual(h.sent[1]["browsers"], [{"session": "dev-1", "project": "site"},
                                                 {"session": "dev-2", "project": "site"}])


class Levels(Base):
    """Protection levels (#5825): locked, hidden (the default), open."""

    def setUp(self):
        super().setUp()
        self.run_cli(["secret", "put", "gh-token", "--var", "GH_TOKEN"], TOKEN)

    def py(self, code):
        return ["--", sys.executable, "-c", code]

    def test_hidden_masks_the_value_and_its_forms_in_the_output(self):
        rc, out = self.run_cli(["secret", "pipe", "gh-token"] + self.py(
            "import sys, base64; v = sys.stdin.buffer.read(); print(v.decode()); print(base64.b64encode(v).decode());"
            " print(v.hex()); print('err', v.decode(), file=sys.stderr)"))
        self.assertEqual(rc, 0, out)
        self.assertNotIn(TOKEN.decode(), out)
        self.assertNotIn(TOKEN.hex(), out)
        self.assertNotIn(__import__("base64").b64encode(TOKEN).decode().rstrip("="), out)
        self.assertGreaterEqual(out.count("***"), 4)
        self.assertIn("masked", out)

    def test_run_puts_it_in_its_variable(self):
        rc, out = self.run_cli(["secret", "run", "gh-token"] + self.py(
            "import os; print('len', len(os.environ['GH_TOKEN'])); print(os.environ['GH_TOKEN'])"))
        self.assertEqual(rc, 0, out)
        self.assertIn(f"len {len(TOKEN)}", out)
        self.assertNotIn(TOKEN.decode(), out)
        self.assertIn("in GH_TOKEN", out)
        rc, out = self.run_cli(["secret", "run", "gh-token", "--var", "OTHER"] + self.py(
            "import os; print('OTHER' in os.environ)"))
        self.assertIn("True", out)

    def test_open_shows_it(self):
        vault.set_level(self.c, "gh-token", "open")
        got = os.path.join(self.dir.name, "got")
        rc, out = self.run_cli(["secret", "pipe", "gh-token"] + self.py(
            f"import sys; open({got!r}, 'wb').write(sys.stdin.buffer.read())"))
        self.assertEqual(rc, 0, out)
        self.assertEqual(Path(got).read_bytes(), TOKEN)
        self.assertNotIn("masked", out)

    def test_locked_allows_only_the_named_commands(self):
        vault.set_level(self.c, "gh-token", "locked", add_commands=["gh"])
        rc, out = self.run_cli(["secret", "pipe", "gh-token"] + self.py("print(1)"))
        self.assertEqual(rc, 2)
        self.assertIn("locked to the commands gh", out)
        self.assertIn("maxpm secret level gh-token --command", out)
        self.assertEqual(vault.log(self.c, "gh-token")["log"][0]["action"], "refused")
        exe = os.path.basename(sys.executable)
        vault.set_level(self.c, "gh-token", add_commands=[f"{exe} -c"])  # its first words; the path does not count
        rc, out = self.run_cli(["secret", "pipe", "gh-token"] + self.py("import sys; print(sys.stdin.read())"))
        self.assertEqual(rc, 0, out)
        self.assertIn("***", out)
        self.assertTrue(vault._command_allowed(["npx wrangler"], ["/usr/local/bin/npx", "wrangler", "deploy"]))
        self.assertFalse(vault._command_allowed(["npx wrangler"], ["npx", "cowsay"]))

    def test_env_is_a_permission(self):
        f = os.path.join(self.dir.name, ".env")
        rc, out = self.run_cli(["secret", "env", "gh-token", f])
        self.assertEqual(rc, 2)
        self.assertIn("writing it into a file is off", out)
        self.assertIn("maxpm secret level gh-token --env on", out)
        self.assertFalse(os.path.exists(f))
        core.register(self.c, "mark", human=True)
        rc, out = self.run_cli(["--as", "mark", "secret", "level", "gh-token", "--env", "on"])
        self.assertEqual(rc, 0, out)
        self.assertIn("env may write it", out)
        rc, out = self.run_cli(["secret", "env", "gh-token", f])
        self.assertEqual(rc, 0, out)

    def test_only_a_person_changes_the_level(self):
        rc, out = self.run_cli(["secret", "level", "gh-token", "open"])
        self.assertEqual(rc, 2)
        self.assertIn("only a person", out)
        rc, out = self.run_cli(["secret", "list"])
        self.assertIn("hidden", out)

    def test_masker_masks_a_value_that_two_chunks_split(self):
        m = vault.Masker(TOKEN)
        half = len(TOKEN) // 2
        out = m.feed(b"before " + TOKEN[:half]) + m.feed(TOKEN[half:] + b" after") + m.flush()
        self.assertEqual(out, b"before *** after")
        pem = b"-----BEGIN KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END KEY-----\n"
        self.assertEqual(vault.Masker(pem).feed(b"x MIIEvQIBADANBgkqhkiG9w0BAQEFAASC y") + b"", b"")
        m = vault.Masker(pem)
        self.assertEqual(m.feed(b"x MIIEvQIBADANBgkqhkiG9w0BAQEFAASC y") + m.flush(), b"x *** y")


class Requests(Base):
    """#5834 (mark 2026-11-14): a session asks the manager; for a set the person did not mark, the manager proposes
    a checklist; the person's OK grants exactly the ticked credentials."""

    def setUp(self):
        super().setUp()
        core.project_add(self.c, "site")
        core.register(self.c, "mark", human=True)
        core.register(self.c, "dev")
        core.register(self.c, "manager-1")
        self.c.execute("UPDATE agents SET role='manager' WHERE name='manager-1'")
        for n in ("stripe-key", "stripe-webhook", "db-url"):
            vault.put(self.c, n, b"v-" + n.encode(), None)
        vault.set_change(self.c, "stripe-live", add=["stripe-key", "stripe-webhook"], actor="mark")
        self.iid = core.item_add(self.c, "site", "refund flow", actor="t")["id"]
        core.claim(self.c, self.iid, "dev")
        p = mock.patch.object(core, "active_manager", lambda conn, but=None: "manager-1")
        p.start()
        self.addCleanup(p.stop)

    def last(self, to, kind="alert"):
        r = self.c.execute("SELECT body FROM messages WHERE kind=? AND to_agent=? ORDER BY id DESC", (kind, to)).fetchone()
        return r["body"] if r else None

    def test_checklist_for_a_set_that_is_not_marked(self):
        r = vault.request(self.c, "stripe-live", "refunds need the live key", "dev")
        self.assertEqual((r["state"], r["to"], r["covers"]), ("open", "manager-1", f"task #{self.iid}"))
        self.assertIn("The person answers each request for stripe-key, stripe-webhook (Ask me): put a checklist",
                      self.last("manager-1"))
        p = vault.propose(self.c, r["request"], ["stripe-key", "stripe-webhook", "db-url"], ["stripe-key"],
                          actor="manager-1")
        self.assertEqual(p["state"], "asked")
        body = self.last("mark")
        self.assertIn("[x] stripe-key", body)
        self.assertIn("[ ] stripe-webhook", body)
        self.assertIn("/settings", body)
        with self.assertRaises(RiverError):
            vault.approve(self.c, r["request"], actor="manager-1")  # the OK is the person's
        a = vault.approve(self.c, r["request"], ["stripe-key", "db-url"], actor="mark")  # mark unticks and adds
        self.assertEqual((a["state"], a["chosen"]), ("granted", ["stripe-key", "db-url"]))
        self.assertEqual(vault.get(self.c, "stripe-key", "dev"), b"v-stripe-key")
        self.assertEqual(vault.get(self.c, "db-url", "dev"), b"v-db-url")
        with self.assertRaises(RiverError):
            vault.get(self.c, "stripe-webhook", "dev")  # exactly the selection
        self.assertIn("allowed stripe-key, db-url", self.last("dev", "notice"))
        core.done(self.c, self.iid, "ok", "dev")
        with self.assertRaises(RiverError):
            vault.get(self.c, "stripe-key", "dev")  # the grant ended with the task

    def test_a_marked_set_is_the_managers_grant(self):
        vault.set_change(self.c, "stripe-live", manager_may_grant=True, actor="mark")
        r = vault.request(self.c, "stripe-live", "refunds", "dev")
        self.assertIn(f"maxpm secret grant stripe-live --item {self.iid}", self.last("manager-1"))
        vault.grant(self.c, "stripe-live", "item", str(self.iid), actor="manager-1")
        self.assertEqual(vault.requests(self.c, every=True)["requests"][0]["state"], "granted")
        self.assertEqual(vault.requests(self.c)["requests"], [])
        self.assertIn(f"for your request {r['request']}", self.last("dev", "notice"))
        res = vault.check(self.c, "dev")
        self.assertEqual([g["set"] for g in res["grants"]], ["stripe-live"])
        self.assertEqual(res["grants"][0]["granted_by"], "manager-1")
        vault.get(self.c, "stripe-key", "dev")
        self.assertEqual(vault.check(self.c, "dev")["uses"][0]["action"], "use")

    def test_the_manager_approves_only_the_credentials_allowed_without_asking(self):
        # #5842: the setting is per credential, so a set can hold one of each
        vault.set_ask(self.c, "stripe-key", "allow", actor="mark")
        r = vault.request(self.c, "stripe-live", "refunds", "dev")
        body = self.last("manager-1")
        self.assertIn(f"allows stripe-key without asking: maxpm secret approve {r['request']} --only stripe-key", body)
        self.assertIn("each request for stripe-webhook (Ask me)", body)
        with self.assertRaises(RiverError) as e:
            vault.approve(self.c, r["request"], ["stripe-key", "stripe-webhook"], actor="manager-1")
        self.assertIn("stripe-webhook (Ask me)", str(e.exception))
        with self.assertRaises(RiverError):
            vault.approve(self.c, r["request"], ["stripe-key"], actor="dev")  # never the session itself
        a = vault.approve(self.c, r["request"], ["stripe-key"], actor="manager-1")
        self.assertEqual((a["state"], a["chosen"], a["decided_by"]), ("granted", ["stripe-key"], "manager-1"))
        self.assertEqual(vault.get(self.c, "stripe-key", "dev"), b"v-stripe-key")
        with self.assertRaises(RiverError):
            vault.get(self.c, "stripe-webhook", "dev")
        self.assertIn(f"granted by manager-1: {a['grant']}", vault.grant_summary(self.c)[0])  # the person's daily list
        rc, out = self.run_cli(["--as", "mark", "secret", "asking", "stripe-key", "ask"])
        self.assertEqual(rc, 0, out)
        self.assertIn("Ask me", out)

    def test_an_old_queue_keeps_the_managers_sets(self):
        # #5842: a set marked 'the manager may grant' before makes each of its credentials 'Allow without asking'
        self.c.execute("ALTER TABLE secrets DROP COLUMN ask")
        self.c.execute("UPDATE secret_sets SET manager_may_grant=1 WHERE name='stripe-live'")
        core._migrate(self.c)
        asks = {r["name"]: r["ask"] for r in vault.listing(self.c)["secrets"]}
        self.assertEqual(asks, {"stripe-key": "allow", "stripe-webhook": "allow", "db-url": "ask"})
        self.assertTrue(vault.sets(self.c)["sets"][0]["manager_may_grant"])

    def test_refuse_and_cli(self):
        r = vault.request(self.c, "stripe-live", "refunds", "dev")
        vault.refuse(self.c, r["request"], "use the test key", actor="manager-1")
        self.assertIn("use the test key", self.last("dev", "notice"))
        rc, out = self.run_cli(["secret", "request", "stripe-live", "--reason", "refunds again"])
        self.assertEqual(rc, 0, out)
        self.assertIn("r2 open: dev asks for stripe-live", out)
        rc, out = self.run_cli(["--as", "manager-1", "secret", "propose", "r2", "--offer", "stripe-key,db-url",
                                "--tick", "stripe-key"])
        self.assertIn("checklist [x] stripe-key, [ ] db-url", out)
        rc, out = self.run_cli(["--as", "mark", "secret", "approve", "r2"])
        self.assertEqual(rc, 0, out)
        self.assertIn("allowed stripe-key", out)
        rc, out = self.run_cli(["--as", "mark", "secret", "check", "dev"])
        self.assertIn(f"set task-{self.iid}-", out)
        self.assertNotIn("req2", out)
        rc, out = self.run_cli(["secret", "requests", "--all"])
        self.assertIn("r1 refused", out)

    def test_refused_loose_secrets_group_into_one_request_for_the_item(self):
        # #6905: refused loose secrets (in no set) create/group one request for the item, with no typed commands.
        vault.put(self.c, "loose-1", b"val-1", None)
        vault.put(self.c, "loose-2", b"val-2", None)
        for n in ("loose-1", "loose-2"):
            with self.assertRaises(RiverError) as e:
                vault.get(self.c, n, "dev", f"pipe: cmd {n}")
            self.assertNotIn("maxpm secret set", str(e.exception))
            self.assertIn("request r1", str(e.exception))
        reqs = vault.requests(self.c)["requests"]
        self.assertEqual(len(reqs), 1)
        self.assertEqual((reqs[0]["request"], reqs[0]["state"], reqs[0]["set_name"]), ("r1", "asked", None))
        self.assertEqual((reqs[0]["offered"], reqs[0]["ticked"]), (["loose-1", "loose-2"], ["loose-1", "loose-2"]))
        self.assertEqual(reqs[0]["covers"], f"task #{self.iid}")
        alerts = self.c.execute("SELECT body, read_at FROM messages WHERE kind='alert' AND to_agent='mark' ORDER BY id").fetchall()
        self.assertEqual(len(alerts), 1)  # grouped into one alert, not two
        self.assertIn("loose-1, loose-2", alerts[0]["body"])
        self.assertIn("(request r1)", alerts[0]["body"])
        self.assertNotIn("maxpm secret set", alerts[0]["body"])
        self.assertIsNone(alerts[0]["read_at"])
        # Allow makes the set and grants it to this item only, and marks the alert read.
        a = vault.approve(self.c, "r1", ["loose-1", "loose-2"], actor="mark")
        self.assertEqual((a["state"], a["chosen"]), ("granted", ["loose-1", "loose-2"]))
        self.assertIsNotNone(self.c.execute("SELECT read_at FROM messages WHERE kind='alert' AND to_agent='mark'").fetchone()["read_at"])
        self.assertEqual(vault.get(self.c, "loose-1", "dev"), b"val-1")
        self.assertEqual(vault.get(self.c, "loose-2", "dev"), b"val-2")
        # Another session on another task still has no access.
        core.register(self.c, "dev2")
        other = core.item_add(self.c, "site", "other task", actor="t")["id"]
        core.claim(self.c, other, "dev2")
        with self.assertRaises(RiverError):
            vault.get(self.c, "loose-1", "dev2")
        # Closing the item ends the grant.
        core.done(self.c, self.iid, "ok", "dev")
        with self.assertRaises(RiverError):
            vault.get(self.c, "loose-1", "dev")

    def test_request_loose_secrets_via_cli_and_propose(self):
        # #6905: maxpm secret request names loose secrets; manager proposes; Allow grants and pipe works.
        vault.put(self.c, "loose-a", b"alpha", None)
        vault.put(self.c, "loose-b", b"beta", None)
        rc, out = self.run_cli(["secret", "request", "loose-a", "loose-b", "--reason", "e2e test"])
        self.assertEqual(rc, 0, out)
        self.assertIn("r1 open: dev asks for loose-a, loose-b", out)
        self.assertIn("maxpm secret propose r1 --offer loose-a,loose-b --tick loose-a,loose-b", self.last("manager-1"))
        # Manager can propose without repeating --offer (uses the request's secrets).
        rc, out = self.run_cli(["--as", "manager-1", "secret", "propose", "r1"])
        self.assertEqual(rc, 0, out)
        self.assertIn("checklist [x] loose-a, [x] loose-b", out)
        # Allow via routes/secrets.py (same op as the page dialog), with 'Do not ask again' on loose-a.
        from river.routes import secrets as routes_secrets
        res = routes_secrets.act(self.c, {"action": "approve", "request": "r1", "only": ["loose-a", "loose-b"],
                                          "always": ["loose-a"]}, "mark")
        self.assertEqual((res["state"], res["chosen"]), ("granted", ["loose-a", "loose-b"]))
        asks = {r["name"]: r["ask"] for r in vault.listing(self.c)["secrets"]}
        self.assertEqual((asks["loose-a"], asks["loose-b"]), ("allow", "ask"))
        # Now the session's secret pipe works.
        rc, out = self.run_cli(["secret", "pipe", "loose-a", "--", sys.executable, "-c",
                                "import sys; assert sys.stdin.read() == 'alpha'"])
        self.assertEqual(rc, 0, out)
        rc, out = self.run_cli(["secret", "pipe", "loose-b", "--", sys.executable, "-c",
                                "import sys; assert sys.stdin.read() == 'beta'"])
        self.assertEqual(rc, 0, out)

    def test_refuse_loose_secret_request_names_secrets(self):
        vault.put(self.c, "loose-x", b"x", None)
        r = vault.request(self.c, "loose-x", "check", "dev")
        self.assertEqual((r["state"], r["set_name"], r["offered"]), ("open", None, ["loose-x"]))
        vault.refuse(self.c, r["request"], "not needed", actor="manager-1")
        notice = self.last("dev", "notice")
        self.assertIn("the secrets loose-x", notice)
        self.assertNotIn("the set None", notice)

    def test_approve_reuses_exact_existing_set_and_names_new_set_from_item(self):
        # #6916: approving the exact members of an existing set grants that set with no copy;
        # loose keys with no matching set get a name from the item, not reqN.
        before_sets = [s["name"] for s in vault.sets(self.c)["sets"]]
        self.assertEqual(before_sets, ["stripe-live"])
        r = vault.request(self.c, "stripe-live", "billing", "dev")
        vault.propose(self.c, r["request"], ["stripe-key", "stripe-webhook"], ["stripe-key", "stripe-webhook"],
                      actor="manager-1")
        notices_before = self.c.execute("SELECT COUNT(*) FROM messages WHERE kind='notice' AND to_agent='dev'").fetchone()[0]
        res = vault.approve(self.c, r["request"], ["stripe-key", "stripe-webhook"], actor="mark")
        self.assertEqual(res["set"], "stripe-live")
        self.assertEqual([s["name"] for s in vault.sets(self.c)["sets"]], ["stripe-live"])
        notices_after = self.c.execute("SELECT COUNT(*) FROM messages WHERE kind='notice' AND to_agent='dev'").fetchone()[0]
        self.assertEqual(notices_after - notices_before, 1)  # no duplicate notice from grant()

        # Loose keys that match an existing set also reuse that set with no copy.
        r_match = vault.request(self.c, ["stripe-key", "stripe-webhook"], "also billing", "dev")
        res_match = vault.approve(self.c, r_match["request"], ["stripe-key", "stripe-webhook"], actor="mark")
        self.assertEqual(res_match["set"], "stripe-live")
        self.assertEqual([s["name"] for s in vault.sets(self.c)["sets"]], ["stripe-live"])

        # Loose keys with no matching set create a set named from the item, not reqN.
        vault.put(self.c, "loose-1", b"v1", None)
        vault.put(self.c, "loose-2", b"v2", None)
        r_loose1 = vault.request(self.c, ["loose-1"], "one key", "dev")
        res_loose1 = vault.approve(self.c, r_loose1["request"], ["loose-1"], actor="mark")
        self.assertTrue(res_loose1["set"].startswith(f"task-{self.iid}-"), res_loose1["set"])
        self.assertFalse(res_loose1["set"].startswith("req"), res_loose1["set"])

        # A second distinct set for the same item gets a -2 suffix instead of colliding.
        r_loose2 = vault.request(self.c, ["loose-1", "loose-2"], "two keys", "dev")
        res_loose2 = vault.approve(self.c, r_loose2["request"], ["loose-1", "loose-2"], actor="mark")
        self.assertEqual(res_loose2["set"], f"{res_loose1['set']}-2")


if __name__ == "__main__":
    unittest.main()
