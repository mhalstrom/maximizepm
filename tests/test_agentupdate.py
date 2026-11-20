"""The agent CLIs' versions and their updates (#6430): the check, Update now, the wait for running sessions, the smoke
test and the rollback, the person's item for a password, automatic updates, and what the page reads."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from river import agentupdate as au
from river import core, server
from river.core import RiverError


class Base(unittest.TestCase):
    """A queue in a temporary folder and a Codex 0.159.1 installed there by npm (a fake prefix). Every command goes to
    run(): npm install changes the installed version, codex exec answers self.answer."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        db = str(self.root / "t.db")
        env = mock.patch.dict(os.environ, {"MAXPM_DB": db})
        env.start()
        self.addCleanup(env.stop)
        self.c = core.connect(db)
        self.addCleanup(self.c.close)
        self.pkg = self.root / "npm/lib/node_modules/@openai/codex"
        (self.pkg / "bin").mkdir(parents=True)
        (self.pkg / "bin/codex.js").write_text("")
        (self.root / "npm/bin").mkdir()
        os.symlink("../lib/node_modules/@openai/codex/bin/codex.js", self.root / "npm/bin/codex")
        self.set_version("0.159.1")
        self.bins = {"codex": str(self.root / "npm/bin/codex")}
        self.latest = {"@openai/codex": "0.171.0"}
        self.ran, self.ps, self.answer, self.install = [], [], (0, "OK"), None
        for name, value in (("RUN", self.command),("FETCH", self.fetch), ("PS", lambda: list(self.ps)),
                            ("WHICH", lambda exe: self.bins.get(exe)), ("SYNC", True)):
            old = getattr(au, name)
            setattr(au, name, value)
            self.addCleanup(setattr, au, name, old)
        au._threads.clear()
        au._started["checked"] = False
        self.addCleanup(au._started.update, checked=False)

    def set_version(self, v):
        self.version = v
        (self.pkg / "package.json").write_text(json.dumps({"name": "@openai/codex", "version": v}))

    def command(self, argv, timeout, cwd):
        self.ran.append(argv)
        if argv[-1] == "--version":
            return 0, f"codex-cli {self.version}"
        if "install" in argv and argv[-1].startswith("@openai/codex@"):
            if self.install:
                return self.install
            v = argv[-1].rsplit("@", 1)[1]
            self.set_version(self.latest["@openai/codex"] if v == "latest" else v)
            return 0, "added 1 package"
        if "exec" in argv:
            return self.answer
        return 1, "unknown command"

    def fetch(self, url, timeout):
        for pkg, v in self.latest.items():
            if url.endswith(pkg.replace("/", "%2F") + "/latest"):
                return json.dumps({"version": v}).encode()
        raise OSError("no such package " + url)

    def row(self, aid="codex"):
        return next(r for r in au.status(self.c)["agents"] if r["id"] == aid)


class Versions(Base):
    def test_versions_compare_by_their_numbers(self):
        self.assertTrue(au.newer("0.171.0", "0.159.1"))
        self.assertTrue(au.newer("1.0.10", "1.0.9"))
        self.assertFalse(au.newer("2.1", "2.1.0"))
        self.assertFalse(au.newer(None, "1.0.0"))
        self.assertEqual(au.parse_version("GitHub Copilot CLI 1.0.114. Run 'copilot update'"), "1.0.114")
        self.assertEqual(au.parse_version("2026.11.19-f005e9d"), "2026.11.19-f005e9d")

    def test_the_install_method_comes_from_where_the_program_is(self):
        reg = core._adapters()
        inst = au.install_of(reg.find("codex"))
        self.assertEqual((inst["how"], inst["package"], inst["prefix"], inst["version"]),
                         ("npm", "@openai/codex", str(self.root / "npm"), "0.159.1"))
        self.assertEqual(au.update_argv(reg.find("codex"), inst, "0.171.0")[-4:],
                         ["-g", "--prefix", str(self.root / "npm"), "@openai/codex@0.171.0"])
        for rel, exe, aid in (("brew/Cellar/opencode-v2/2.2.7_1/bin/opencode", "opencode", "opencode"),
                              ("brew/Caskroom/antigravity-cli/1.5.13,5999/wrap/agy", "agy", "antigravity"),
                              ("uv/tools/aider-chat/bin/aider", "aider", "aider"),
                              ("claude/versions/2.1.329", "claude", "claude")):
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("")
            self.bins[exe] = str(p)
        got = {aid: au.install_of(reg.find(aid)) for aid in ("opencode", "antigravity", "aider", "claude")}
        self.assertEqual({k: (v["how"], v["package"], v["version"]) for k, v in got.items()},
                         {"opencode": ("brew", "opencode-v2", "2.2.7"), "antigravity": ("cask", "antigravity-cli", "1.5.13"),
                          "aider": ("uv", "aider-chat", None), "claude": ("self", None, None)})
        self.assertEqual(au.update_argv(reg.find("antigravity"), got["antigravity"])[1:],
                         ["upgrade", "--cask", "antigravity-cli"])
        self.assertIsNone(au.update_argv(reg.find("opencode"), got["opencode"], "2.2.7"))  # Homebrew: no rollback
        self.assertEqual(au.update_argv(reg.find("aider"), got["aider"], "0.86.2")[1:],
                         ["tool", "install", "--force", "aider-chat==0.86.2"])
        self.assertEqual(au.update_argv(reg.find("claude"), got["claude"], "2.1.330")[1:], ["install", "2.1.330"])
        self.assertEqual(au.update_argv(reg.find("claude"), got["claude"])[1:], ["install"])  # its newest
        self.assertIsNone(au.install_of(reg.find("goose")))  # not installed

    def test_every_built_in_adapter_says_how_it_updates(self):
        from river import agents
        for aid in agents.BUILTIN:
            a = agents.find(aid)
            self.assertIsNotNone(a.update, aid)
            self.assertEqual(agents.check(a.module, aid), [], aid)


class Check(Base):
    def test_the_check_reads_both_versions_and_the_page_opens_the_dialog_until_later(self):
        au.serve_pass(self.c)  # serve starts: the check runs at once
        r = self.row()
        self.assertEqual((r["installed"], r["latest"], r["how"], r["newer"], r["dialog"]),
                         ("0.159.1", "0.171.0", "npm", True, True))
        self.assertIsNone(self.row("goose")["installed"])
        self.ran.clear()
        au.serve_pass(self.c)  # within a day: no check again
        self.assertEqual(self.ran, [])
        au.later(self.c, "codex", "mark")
        self.assertFalse(self.row()["dialog"])
        self.latest["@openai/codex"] = "0.172.0"  # a newer version again: the dialog comes back
        au.check_now(self.c)
        self.assertTrue(self.row()["dialog"])

    def test_a_failed_lookup_keeps_the_last_newest_version(self):
        au.check_now(self.c)
        self.latest.clear()
        d = au.check_now(self.c)
        self.assertEqual(d["agents"]["codex"]["latest"], "0.171.0")
        self.assertIn("no such package", d["agents"]["codex"]["error"])
        self.assertIn("codex", d["error"])


class Update(Base):
    def setUp(self):
        super().setUp()
        au.check_now(self.c)
        au._started["checked"] = True
        core.register(self.c, "mark", human=True)

    def test_update_waits_for_running_sessions_holds_new_starts_then_updates_and_tests(self):
        au.request(self.c, "codex", "mark")
        native = self.pkg / "node_modules/@openai/codex-darwin-arm64/vendor/codex"
        native.parent.mkdir(parents=True)
        native.write_text("")
        self.ps = [f"node {self.bins['codex']} exec hello", f"{native} --sandbox x", "codex resume 1", "/bin/zsh -l",
                   "node /elsewhere/bin/codex exec", "python3 /x/bin/maxpm go codex"]  # another install, no codex
        self.assertEqual(au.serve_pass(self.c), [])
        r = self.row()
        self.assertEqual((r["update"]["state"], r["update"]["waits"], r["dialog"]), ("queued", 3, False))
        self.assertIn("waits to update", au.hold(self.c, "codex"))
        with self.assertRaisesRegex(RiverError, "Codex waits to update"):
            core.pick_provider(self.c, "codex", explicit=True)
        self.ps = []  # the session ended
        self.assertEqual(au.serve_pass(self.c), ["codex"])
        r = self.row()
        self.assertEqual((r["installed"], r["update"]["state"], r["newer"]), ("0.171.0", "done", False))
        self.assertIn("0.159.1 → 0.171.0", r["update"]["message"])
        self.assertTrue(r["updated_at"])
        self.assertIn(["install", "-g", "--prefix", str(self.root / "npm"), "@openai/codex@0.171.0"],
                      [a[1:] for a in self.ran])
        self.assertTrue(any("exec" in a and a[-1] == "Reply OK" for a in self.ran))
        self.assertIsNone(au.hold(self.c, "codex"))
        self.assertIn("updated Codex 0.159.1 -> 0.171.0",
                      self.c.execute("SELECT change FROM events ORDER BY rowid DESC LIMIT 1").fetchone()[0])

    def test_a_failed_smoke_test_puts_the_old_version_back_and_alerts(self):
        self.answer = (1, '{"type":"error","status":400,"message":"model not supported"}')
        au.request(self.c, "codex", "mark")
        au.serve_pass(self.c)
        r = self.row()
        self.assertEqual((r["update"]["state"], r["installed"]), ("failed", "0.159.1"))
        self.assertIn("the old version 0.159.1 is back", r["update"]["message"])
        self.assertEqual(self.ran[-2][-1], "@openai/codex@0.159.1")
        body = self.c.execute("SELECT body FROM messages WHERE to_agent='mark' AND kind='alert'").fetchone()[0]
        self.assertIn("failed its smoke test", body)
        self.assertIn("model not supported", body)
        self.assertIsNone(au.hold(self.c, "codex"))

    def test_an_update_that_asks_for_a_password_becomes_a_persons_item(self):
        core.project_add(self.c, "shop")
        self.install = (243, "npm ERR! code EACCES\nnpm ERR! Error: EACCES: permission denied, mkdir '/usr/lib/x'")
        au.request(self.c, "codex", "mark")
        au.serve_pass(self.c)
        r = self.row()
        self.assertEqual(r["update"]["state"], "failed")
        it = core.item_show(self.c, r["update"]["person_item"])
        self.assertEqual(it["doer"], "human")
        self.assertIn("Update Codex in a terminal:", it["title"])
        self.assertIn("@openai/codex@0.171.0", it["title"])
        au.request(self.c, "codex", "mark")
        au.serve_pass(self.c)
        self.assertEqual(self.row()["update"]["person_item"], r["update"]["person_item"])  # no second item

    def test_cancel_takes_back_a_queued_update(self):
        self.ps = [f"{self.bins['codex']} resume x"]
        au.request(self.c, "codex", "mark")
        au.serve_pass(self.c)
        au.cancel(self.c, "codex", "mark")
        self.assertEqual(self.row()["update"]["state"], "cancelled")
        self.assertIsNone(au.hold(self.c, "codex"))
        with self.assertRaisesRegex(RiverError, "only a queued update stops"):
            au.cancel(self.c, "codex", "mark")

    def test_auto_update_is_a_setting_per_agent_off_by_default(self):
        self.assertEqual(au.serve_pass(self.c), [])
        self.assertIsNone(self.row()["update"])
        au.set_auto(self.c, "codex", True, "mark")
        self.assertEqual(core.setting(self.c, "agent_auto_update"), "codex")
        self.assertTrue(self.row()["auto"])
        self.assertEqual(au.serve_pass(self.c), ["codex"])
        self.assertEqual(self.row()["installed"], "0.171.0")
        au.set_auto(self.c, "codex", False, "mark")
        self.assertEqual(core.setting(self.c, "agent_auto_update"), "")
        with self.assertRaisesRegex(RiverError, "no agent nosuch"):
            core.config_set(self.c, "agent_auto_update", "codex,nosuch")
        core.config_set(self.c, "agent_auto_update", "all")
        self.assertIn("codex", au.auto_ids(self.c))

    def test_an_update_that_stopped_with_its_process_is_marked_failed(self):
        au._set(self.c, "codex", state="updating", pid=os.getpid())
        self.assertIn("updates now", au.hold(self.c, "codex"))
        au.serve_pass(self.c)
        self.assertEqual(self.row()["update"]["state"], "failed")
        self.assertIn("stopped with its process", self.row()["update"]["message"])

    def test_the_page_ops_and_route(self):
        res = server.OPS["agent_update"](self.c, {"agents": ["codex"]}, "mark")
        self.assertEqual(res[0]["update"]["state"], "queued")
        server.OPS["agent_update_cancel"](self.c, {"agent": "codex"}, "mark")
        server.OPS["agent_update_later"](self.c, {"agents": ["codex"]}, "mark")
        self.assertTrue(self.row()["later"])
        server.OPS["agent_update_auto"](self.c, {"agent": "codex", "on": True}, "mark")
        server.OPS["agent_update_check"](self.c, {}, "mark")
        self.assertTrue(au.read(self.c).get("check_asked"))
        out = {}
        h = server.Handler.__new__(server.Handler)
        h.path, h.client_address = "/api/agent-versions", ("127.0.0.1", 5555)
        h.headers = {"Host": "127.0.0.1:8765"}
        h._send = lambda code, body, *a, **k: out.update(code=code, body=json.loads(json.dumps(body, default=str)))
        h.do_GET()
        self.assertEqual(out["code"], 200)
        row = next(r for r in out["body"]["agents"] if r["id"] == "codex")
        self.assertEqual((row["installed"], row["latest"], row["auto"]), ("0.159.1", "0.171.0", True))
        self.assertTrue(out["body"]["checking"])
        self.assertEqual(server.restart_blocked(self.c), None)


if __name__ == "__main__":
    unittest.main()
