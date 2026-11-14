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
        self.assertIn("did not mark the set stripe-live", str(e.exception))
        vault.set_change(self.c, "stripe-live", manager_may_grant=True, actor="mark")
        with self.assertRaises(RiverError):
            vault.set_change(self.c, "stripe-live", manager_may_grant=False, actor="manager-1")  # the mark is the person's
        self.refused("dev")
        self.assertIn("the manager may grant", self.alerts("manager-1")[0])
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
        p = mock.patch("river.peer.chain", lambda pid: self.chain)
        p.start()
        self.addCleanup(p.stop)

    def who(self, actor):
        return vault.session_of(self.c, actor)

    def refused(self, actor):
        with self.assertRaises(RiverError) as e:
            self.who(actor)
        return str(e.exception)

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
        act(self.c, {"action": "set", "name": "google", "manager_may_grant": True}, "mark")
        act(self.c, {"action": "set", "name": "google", "add": ["google-mark"]}, "mark")
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
        self.assertEqual([(s["name"], s["manager_may_grant"]) for s in body["sets"]], [("google", True)])
        self.assertEqual([x["grant"] for x in body["grants"]], [g["grant"]])
        self.assertEqual(body["levels"], list(vault.LEVELS))
        act(self.c, {"action": "revoke", "grant": g["id"]}, "mark")
        routes_secrets.credentials(h, h.path)
        self.assertEqual(h.sent[1]["grants"], [])


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


if __name__ == "__main__":
    unittest.main()
