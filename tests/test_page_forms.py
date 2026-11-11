"""#5289 (#5224 for Pi, OpenCode and Grok): each slash command says how it works from the page (run), which Settings row
does it (setting) or that the Usage panel gives its view (page); each adapter reads its model's context window and the
context of its last model call (chat_cache_line), for the context meter. The commands were tried in each CLI in tmux
on 2026-11-11; the context windows match what each CLI's own screen showed (Grok 256k, Pi 500k for
openai-codex/gpt-6.1-sol, OpenCode 200k for opencode/big-pickle)."""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from river import agents, core, sessions

FIX = Path(__file__).parent / "fixtures"


def fold(ad, path):
    st = {"last": None, "ttl": None, "model": None}
    with open(path, "rb") as f:
        for raw in f:
            ad.chat_cache_line(raw, st)
    return st


class Commands(unittest.TestCase):
    def marks(self, aid):
        a = agents.get(aid)
        self.assertEqual(agents.check(a.module, aid), [])
        self.assertTrue(all(c.get("run") in agents.COMMAND_RUNS for c in a.commands), aid)
        return ({c["name"]: c["setting"] for c in a.commands if c.get("setting")},
                sorted(c["name"] for c in a.commands if c.get("page") == "usage"),
                {c["name"]: c["run"] for c in a.commands})

    def test_grok(self):
        settings, usage, runs = self.marks("grok")
        self.assertEqual(settings, {"model": "model", "effort": "effort"})
        self.assertEqual(usage, ["context", "session-info", "usage"])
        self.assertEqual((runs["resume"], runs["rewind"], runs["compact"], runs["new"]), ("terminal", "terminal", "now", "now"))

    def test_pi(self):
        settings, usage, runs = self.marks("pi")
        self.assertEqual(settings, {"model": "model", "thinking": "effort"})
        self.assertEqual(usage, ["session"])
        self.assertEqual((runs["settings"], runs["resume"], runs["compact"]), ("terminal", "terminal", "now"))

    def test_opencode(self):
        settings, usage, runs = self.marks("opencode")
        self.assertEqual(settings, {"models": "model"})
        self.assertEqual(usage, ["stats", "usage"])
        self.assertEqual((runs["status"], runs["sessions"], runs["effort"], runs["compact"]),
                         ("terminal", "terminal", "terminal", "now"))


class Windows(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def test_grok_reads_its_models_cache(self):
        (self.home / "models_cache.json").write_text(json.dumps({"models": {
            "grok-4.7": {"info": {"id": "grok-4.7", "context_window": 256000}}}}))
        with mock.patch.dict(os.environ, {"GROK_HOME": str(self.home)}):
            self.assertEqual(agents.get("grok").context_window("grok-4.7"), 256000)
            self.assertIsNone(agents.get("grok").context_window("grok-9"))

    def test_pi_reads_its_model_store_by_provider(self):
        store = self.home / ".pi" / "agent"
        store.mkdir(parents=True)
        (store / "models-store.json").write_text(json.dumps({
            "openai-codex": {"models": [{"id": "gpt-6.1-sol", "contextWindow": 500000}]},
            "openai": {"models": [{"id": "gpt-6.1-sol", "contextWindow": 272000},
                                  {"id": "gpt-5.6-luna", "contextWindow": 272000}]}}))
        with mock.patch.object(Path, "home", classmethod(lambda cls: self.home)):
            pi = agents.get("pi")
            self.assertEqual(pi.context_window("openai-codex/gpt-6.1-sol"), 500000)
            self.assertEqual(pi.context_window("openai/gpt-6.1-sol"), 272000)
            self.assertIsNone(pi.context_window("gpt-6.1-sol"))  # two providers, two windows: none
            self.assertEqual(pi.context_window("gpt-5.6-luna"), 272000)

    def test_opencode_reads_its_models_dev_catalog(self):
        from river.agents import opencode
        db = self.home / "opencode"
        db.mkdir()
        conn = sqlite3.connect(db / "opencode.db")
        conn.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)")
        body = {"opencode": {"models": {"big-pickle": {"limit": {"context": 200000, "output": 32000}}}},
                "openrouter": {"models": {"meta/muse-spark": {"limit": {"context": 1048576}}}}}
        conn.execute("INSERT INTO kv VALUES (?, ?)", ("models-dev:v2:catalog", json.dumps({"body": json.dumps(body)})))
        conn.commit()
        conn.close()
        opencode._catalog.update(at=0.0, models=None)
        self.addCleanup(opencode._catalog.update, at=0.0, models=None)
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.home)}):
            oc = agents.get("opencode")
            self.assertEqual(oc.context_window("opencode/big-pickle"), 200000)
            self.assertEqual(oc.context_window("openrouter/meta/muse-spark"), 1048576)
            self.assertIsNone(oc.context_window("opencode/nope"))


class LastCall(unittest.TestCase):
    """The context of the last model call, from each agent's recorded log, as the context meter reads it."""

    def test_each_log_gives_its_last_call(self):
        st = fold(agents.get("grok"), FIX / "grok" / "updates.jsonl")
        self.assertEqual((st["model"], st["last"]["context"], st["last"]["start"]), ("grok-4.7", 19099, "2026-11-11T07:58:08Z"))
        st = fold(agents.get("pi"), FIX / "pi" / "session.jsonl")
        self.assertEqual((st["model"], st["last"]["context"]), ("mistral/mistral-small-latest", 1821))
        st = fold(agents.get("opencode"), FIX / "opencode" / "session.jsonl")
        self.assertEqual((st["model"], st["last"]["context"]), ("opencode/big-pickle", 8484))

    def test_the_meter_of_a_grok_session(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        (home / "models_cache.json").write_text(json.dumps({"models": {"grok-4.7": {"info": {"context_window": 256000}}}}))
        with mock.patch.dict(os.environ, {"GROK_HOME": str(home), "MAXPM_DB": str(home / "t.db")}):
            conn = core.connect()
            self.addCleanup(conn.close)
            core.register(conn, "gk")
            self.assertEqual(sessions.context_meter(conn, "gk", "grok", FIX / "grok" / "updates.jsonl"),
                             {"used": 19099, "window": 256000, "model": "grok-4.7"})



class Group2(unittest.TestCase):
    """#5290: Hermes, Gemini CLI and Cursor CLI."""

    def marks(self, aid):
        a = agents.get(aid)
        self.assertEqual(agents.check(a.module, aid), [])
        self.assertTrue(all(c.get("run") in agents.COMMAND_RUNS for c in a.commands), aid)
        return ({c["name"]: c["setting"] for c in a.commands if c.get("setting")},
                sorted(c["name"] for c in a.commands if c.get("page") == "usage"),
                {c["name"]: c["run"] for c in a.commands})

    def test_the_commands(self):
        settings, usage, runs = self.marks("hermes")
        self.assertEqual((settings, usage), ({"model": "model", "reasoning": "effort"}, ["context", "status", "usage"]))
        self.assertEqual((runs["sessions"], runs["compress"]), ("terminal", "now"))
        settings, usage, runs = self.marks("gemini")
        self.assertEqual((settings, usage), ({"model": "model"}, ["stats"]))
        self.assertEqual((runs["settings"], runs["compress"]), ("terminal", "now"))
        settings, usage, runs = self.marks("cursor")
        self.assertEqual((settings, usage), ({"model": "model", "sandbox": "sandbox"}, []))
        self.assertEqual((runs["config"], runs["resume"], runs["summarize"]), ("terminal", "terminal", "now"))

    def test_hermes_reads_its_context_length_cache(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / "context_length_cache.yaml").write_text(
            "context_lengths:\n  stealth/missingno@https://inference-api.nousresearch.com/v1: 262144\n"
            "  reflection/beam-1.0:free@https://inference-api.nousresearch.com/v1: \n    262144\n"
            "  gpt-6.1-terra@https://chatgpt.com/backend-api/codex: 500000\n"
            "  gpt-6.1-sol@https://chatgpt.com/backend-api/codex: 500000\n  gpt-6.1-sol@https://api.openai.com/v1: 272000\n")
        with mock.patch.dict(os.environ, {"HERMES_HOME": tmp.name}):
            h = agents.get("hermes")
            self.assertEqual((h.context_window("gpt-6.1-terra"), h.context_window("reflection/beam-1.0:free"),
                              h.context_window("gpt-6.1-sol"), h.context_window("nope")), (500000, 262144, None, None))

    def test_gemini_window_and_last_call(self):
        g = agents.get("gemini")
        self.assertEqual((g.context_window("gemini-3.8-flash"), g.context_window("pro"), g.context_window("gpt-6")),
                         (1_048_576, 1_048_576, None))
        st = fold(g, FIX / "gemini" / "session-2026-11-11T07-55-5d0c1b2a.jsonl")
        self.assertEqual(st["model"], "gemini-3.8-flash")
        self.assertGreater(st["last"]["context"], 0)


class Group3(unittest.TestCase):
    """#5291: GitHub Copilot CLI, Aider, Goose and Amp."""

    def marks(self, aid):
        a = agents.get(aid)
        self.assertEqual(agents.check(a.module, aid), [])
        self.assertTrue(all(c.get("run") in agents.COMMAND_RUNS for c in a.commands), aid)
        return ({c["name"]: c["setting"] for c in a.commands if c.get("setting")},
                sorted(c["name"] for c in a.commands if c.get("page") == "usage"),
                {c["name"]: c["run"] for c in a.commands})

    def test_the_commands(self):
        settings, usage, runs = self.marks("copilot")
        self.assertEqual((settings, usage), ({"model": "model", "permissions": "permissions"}, ["context", "usage"]))
        self.assertEqual((runs["permissions"], runs["session"], runs["compact"]), ("menu", "terminal", "now"))
        settings, usage, runs = self.marks("aider")
        self.assertEqual((settings, usage), ({"model": "model", "reasoning-effort": "effort"}, ["tokens"]))
        self.assertEqual((runs["settings"], runs["undo"]), ("terminal", "now"))
        settings, usage, runs = self.marks("goose")
        self.assertEqual((settings, usage), ({"model": "model", "mode": "mode"}, ["status"]))
        self.assertEqual((runs["mode"], runs["compact"]), ("menu", "now"))
        self.assertEqual(agents.get("amp").commands, [])  # Amp has no slash commands: "/" opens its palette

if __name__ == "__main__":
    unittest.main()
