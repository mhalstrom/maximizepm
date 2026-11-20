"""One order of models and one effort list per model for every agent (#6256, mark 2026-11-19): the model list of the
launch dialog and of a session's Settings dialog is newest generation first, and within a generation strongest first;
the effort list shows only the levels that the agent and the chosen model take, low to high. agents.order_models is
the one sort; each adapter feeds it its facts (DESCRIBE['tiers'], the dicts of read_models)."""

import os
import tempfile
import unittest
from unittest import mock

from river import agents, core
from river.agents import antigravity


def order_problem(models, tiers=()):
    """The first place where models (platform_models rows) break the order of #6256, as a sentence; None when none
    does: within one provider and maker, the generation never rises, and within one generation the tier never rises.
    Each model's effort levels are MaximizePM levels, low to high."""
    prev = {}
    for m in models:
        name = m["name"]
        efforts = m.get("efforts")
        if efforts is not None and efforts != [lv for lv in agents.MAXPM_EFFORTS if lv in efforts]:
            return f"{name}: its efforts {efforts} are not MaximizePM levels from low to high"
        if name.lower() in agents.CHOICE_MODELS:
            continue
        provider = name.split("/", 1)[0] if "/" in name else ""
        group = (provider, agents.model_maker(name) or agents.model_maker(m.get("id")))
        v = agents._version_of(m)
        gen = v[0] if v else 10 ** 6
        tier = m["tier"] if m.get("tier") is not None else (
            agents.model_tier(name, tiers) or agents.model_tier(m.get("id"), tiers))
        last = prev.get(group)
        if last and (gen > last[1] or gen == last[1] and tier > last[2]):
            return f"{name} comes after {last[0]}, an older or weaker model of the same maker"
        prev[group] = (name, gen, tier)
    return None


def platforms():
    """(platform, adapter) of each registered agent that MaximizePM can start."""
    return [(core._platform_of(a), a) for a in agents.load().values() if not a.missing()]


class Db(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.c = core.connect(os.path.join(d.name, "t.db"))
        self.addCleanup(self.c.close)


class Order(unittest.TestCase):
    def test_versions_tiers_and_makers_read_from_the_names(self):
        self.assertEqual(agents.model_version("claude-opus-5-5"), (5, 5))
        self.assertEqual(agents.model_version("gpt-6.1-sol"), (6, 1))
        self.assertEqual(agents.model_version("opencode/deepseek-v4.1-flash-free"), (4, 1))
        self.assertEqual(agents.model_version("gpt-oss-120b"), ())  # a size, no version
        self.assertEqual(agents.model_version("gpt-6-astra-900k"), (6,))
        self.assertEqual(agents.model_version("opus"), ())
        self.assertEqual(agents.model_tier("claude-fable-5.2"), 4)
        self.assertEqual(agents.model_tier("gemini-4-argon", ["neon", "carbon", "argon"]), 1)
        self.assertEqual(agents.model_maker("amazon-bedrock/anthropic.claude-opus-4-1"), "anthropic")
        self.assertEqual(agents.model_maker("sol"), "openai")

    def test_newest_generation_first_then_the_strongest(self):
        # mark's example: Fable 5.2, Opus 5.5, Sonnet 5.5, Haiku 5.5, then older.
        self.assertEqual(agents.order_models(["claude-haiku-4-5", "claude-haiku-5-5", "claude-sonnet-5-5",
                                              "claude-opus-5-5", "claude-fable-5-2", "claude-opus-4-1"]),
                         ["claude-fable-5-2", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5",
                          "claude-opus-4-1", "claude-haiku-4-5"])
        self.assertEqual(agents.order_models(["haiku", "sonnet", "opus", "fable"]), ["fable", "opus", "sonnet", "haiku"])
        self.assertEqual(agents.order_models(["luna", "terra", "sol", "astra"]), ["astra", "sol", "terra", "luna"])
        self.assertEqual(agents.order_models(["gpt-6.1-terra", "gpt-6-luna", "gpt-6.2-sol", "gpt-6-astra",
                                              "gpt-5.6-terra", "gpt-6-astra-900k"]),
                         ["gpt-6-astra", "gpt-6-astra-900k", "gpt-6.2-sol", "gpt-6.1-terra", "gpt-6-luna",
                          "gpt-5.6-terra"])

    def test_the_agent_s_own_choice_first_and_each_maker_together(self):
        self.assertEqual(agents.order_models(["auto", "gpt-6.1-sol", "claude-sonnet-5.5", "claude-fable-5.2",
                                              "gpt-6-astra", "composer-2.5"]),
                         ["auto", "gpt-6-astra", "gpt-6.1-sol", "claude-fable-5.2", "claude-sonnet-5.5",
                          "composer-2.5"])

    def test_facts_of_a_dict_win_over_the_name(self):
        rows = [{"name": "a-orchestrator", "version": "4", "tier": -1}, {"name": "a-4-big"}, {"name": "a-3-big"}]
        self.assertEqual([r["name"] for r in agents.order_models(rows)], ["a-4-big", "a-orchestrator", "a-3-big"])


class Efforts(unittest.TestCase):
    def test_each_agent_level_is_offered_once_low_to_high(self):
        self.assertEqual(agents.get("aider").effort_choices(), ["low", "medium", "high"])
        self.assertEqual(agents.get("goose").effort_choices(), ["low", "medium", "high", "max"])
        self.assertEqual(agents.get("grok").effort_choices(), ["low", "medium", "high", "xhigh"])
        self.assertEqual(agents.get("amp").effort_choices(), ["low", "medium", "high", "max"])
        self.assertEqual(agents.get("opencode").effort_choices(), [])

    def test_a_model_s_levels_come_from_the_adapter(self):
        a = agents.get("antigravity")
        self.assertEqual(a.model_efforts("gemini-4-carbon"), ["low", "medium", "high"])
        self.assertEqual(a.model_efforts("gemini-pro-orchestrator"), [])
        self.assertIsNone(a.model_efforts("gemini-9-unknown"))
        self.assertEqual(agents.get("grok").model_efforts("x", ["low", "high", "xhigh"]), ["low", "high", "xhigh"])


class EveryAdapter(Db):
    def test_each_registered_agent_s_models_and_efforts_are_in_the_one_order(self):
        for platform, a in platforms():
            with self.subTest(agent=a.id):
                ms = core.platform_models(self.c, platform)
                tiers = a.describe.get("tiers") or ()
                self.assertIsNone(order_problem(ms, tiers))
                self.assertEqual(ms, agents.order_models(ms, tiers))
                self.assertEqual(len({m["name"] for m in ms}), len(ms), "a model shows twice")
                ch = a.effort_choices()
                self.assertEqual(ch, [lv for lv in agents.MAXPM_EFFORTS if lv in ch])
                self.assertEqual(len({a.effort_name(lv) for lv in ch}), len(ch), "an agent level shows twice")

    def test_the_launch_dialog_gets_the_same_order(self):
        opts = {o["platform"]: o for o in core.launch_options(self.c) if o["platform"]}
        for platform, a in platforms():
            if platform in opts:
                with self.subTest(agent=a.id):
                    self.assertEqual(opts[platform]["models"], core.platform_models(self.c, platform))


class Antigravity(Db):
    def setUp(self):
        super().setUp()
        self.addCleanup(mock.patch.stopall)
        antigravity.MODELS.reset()
        self.addCleanup(antigravity.MODELS.reset)

    def test_one_row_per_model_with_agy_s_name_and_its_levels(self):
        ms = core.platform_models(self.c, "antigravity")
        self.assertEqual([(m["label"], m["efforts"]) for m in ms], [
            ("Gemini 4 Neon", ["low", "medium", "high", "xhigh"]),
            ("Gemini 4 Carbon", ["low", "medium", "high"]),
            ("Gemini 4 Argon", ["low", "medium", "high"]),
            ("Gemini 4 Hydrogen", ["low", "medium", "high", "xhigh"]),
            ("Orchestrator (Neon)", []), ("Orchestrator (Carbon)", []), ("Orchestrator (Argon)", []),
            ("Gemini 3.8 Flash", ["low", "medium", "high"]),
            ("Claude Opus 5.5", ["low", "medium", "high"]),
            ("Claude Sonnet 5.5", ["low", "medium", "high"]),
            ("GPT-OSS 120B", ["medium"])])
        self.assertEqual(ms[0]["name"], "gemini-4-neon")

    def test_the_list_of_the_installed_agy_shows_a_new_model(self):
        mock.patch.object(antigravity.MODELS, "runner", lambda: "Fetching available models...\n"
                          "gemini-5-neon-low\tGemini 5 Neon (Low)\ngemini-5-neon-max\tGemini 5 Neon (Max)\n"
                          "gemini-4-neon-high\tGemini 4 Neon (High)\n").start()
        antigravity.read_models()  # starts the read; until it ends the list is MODELS_SEEN
        antigravity.MODELS.wait()
        ms = core.platform_models(self.c, "antigravity")
        self.assertEqual([(m["name"], m["efforts"]) for m in ms],
                         [("gemini-5-neon", ["low", "max"]), ("gemini-4-neon", ["high"])])

    def test_the_launch_gives_the_level_one_way(self):
        argv = antigravity.build_command(None, None, "gemini-4-carbon", "medium", {}, None)
        self.assertEqual(argv[1:5], ["--model", "gemini-4-carbon", "--effort", "medium"])
        # An id with its level (an old command) and an effort: agy refuses both, so the level of the id goes.
        argv = antigravity.build_command(None, None, "gemini-4-neon-low", "high", {}, None)
        self.assertEqual(argv[1:5], ["--model", "gemini-4-neon", "--effort", "high"])
        self.assertEqual(antigravity.build_command(None, None, "gemini-4-neon-low", None, {}, None)[1:3],
                         ["--model", "gemini-4-neon-low"])
        # An Orchestrator takes no --effort.
        argv = antigravity.build_command(None, None, "gemini-pro-orchestrator", "high", {}, None)
        self.assertNotIn("--effort", argv)

    def test_a_level_the_model_lacks_is_refused_before_the_start(self):
        a = agents.get("antigravity")
        with self.assertRaisesRegex(core.RiverError, "gemini-4-carbon takes effort low, medium, high, not max"):
            core._check_agent_effort(a, "gemini-4-carbon", "max")
        core._check_agent_effort(a, "gemini-pro-orchestrator", "high")  # no effort: it starts with none
        core._check_agent_effort(a, "gemini-4-neon", "xhigh")


class EachAgentsOwnList(Db):
    """#6257: each agent's list comes from its own CLI or cache where it has one, with its own names and levels."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(mock.patch.stopall)

    def test_ids_with_levels_become_one_model_each(self):
        rows = agents.split_levels([("gpt-5.3-codex-low", "Codex 5.3 Low"), ("gpt-5.3-codex", "Codex 5.3"),
                                    ("gpt-5.3-codex-xhigh", "Codex 5.3 Extra High"),
                                    ("gpt-5.3-codex-low-fast", "Codex 5.3 Low Fast"), ("composer-2.5", "Composer 2.5")])
        self.assertEqual(rows, [{"name": "gpt-5.3-codex", "label": "Codex 5.3", "efforts": ["low", "xhigh"]},
                                {"name": "gpt-5.3-codex-fast", "label": "Codex 5.3 Fast", "efforts": ["low"]},
                                {"name": "composer-2.5", "label": "Composer 2.5", "efforts": []}])

    def test_cursor_reads_its_list_and_puts_the_level_back_at_launch(self):
        from river.agents import cursor
        cursor.MODELS.reset()
        self.addCleanup(cursor.MODELS.reset)
        mock.patch.object(cursor.MODELS, "runner", lambda: "Available models\n\nauto - Auto (default)\n"
                          "gpt-5.6-sol-high - GPT-5.6 Sol 1M High\ngpt-5.6-sol-xhigh-fast - GPT-5.6 Sol 1M Extra High Fast\n"
                          "claude-opus-5-5-low - Claude Opus 5.5 1M Low\ncomposer-2.5 - Composer 2.5\n").start()
        cursor.read_models()
        cursor.MODELS.wait()
        ms = {m["name"]: m for m in core.platform_models(self.c, "cursor")}
        self.assertEqual((ms["gpt-5.6-sol"]["label"], ms["gpt-5.6-sol"]["efforts"]), ("GPT-5.6 Sol 1M", ["high"]))
        self.assertEqual(ms["gpt-5.6-sol-fast"]["efforts"], ["xhigh"])
        self.assertEqual(ms["composer-2.5"]["efforts"], [])
        self.assertEqual(cursor.model_word("gpt-5.6-sol-fast", "xhigh"), "gpt-5.6-sol-xhigh-fast")
        self.assertEqual(cursor.model_word("composer-2.5", "high"), "composer-2.5")  # a model with no level
        self.assertEqual(cursor.model_word("claude-opus-5-5", "low"), "claude-opus-5-5-low")

    def test_pi_lists_its_signed_in_providers_with_its_default_first(self):
        from river.agents import pi
        text = ("provider model context max-out thinking images\n"
                "amazon-bedrock anthropic.claude-opus-5-5 1M 128K yes yes\n"
                "mistral mistral-small 128K 8K no no\n"
                "openai-codex gpt-6.1-sol 1M 128K yes yes\n")
        with mock.patch.object(pi, "_default_provider", lambda: "openai-codex"):
            rows = pi.parse_models(text)
        self.assertEqual([(r["name"], r["pick"]) for r in rows],
                         [("openai-codex/gpt-6.1-sol", True), ("amazon-bedrock/anthropic.claude-opus-5-5", False),
                          ("mistral/mistral-small", False)])
        self.assertEqual(rows[2]["efforts"], [])
        self.assertEqual(agents.get("pi").model_efforts("x", rows[0]["efforts"]), list(agents.MAXPM_EFFORTS))

    def test_codex_shows_its_cache_with_its_names_and_leaves_hidden_models_out(self):
        import json
        from pathlib import Path
        Path(self.tmp.name, "models_cache.json").write_text(json.dumps({"models": [
            {"slug": "gpt-6.2-sol", "display_name": "GPT-6.2-Sol", "visibility": "list",
             "supported_reasoning_levels": [{"effort": e} for e in ("low", "high")]},
            {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1-Sol", "visibility": "list",
             "supported_reasoning_levels": [{"effort": "medium"}]},
            {"slug": "gpt-reserve", "display_name": "GPT-Reserve", "visibility": "hide"}]}), encoding="utf-8")
        mock.patch.dict(os.environ, {"CODEX_HOME": self.tmp.name}).start()
        ms = {m["name"]: m for m in core.platform_models(self.c, "codex")}
        self.assertNotIn("gpt-reserve", ms)
        self.assertEqual((ms["gpt-6.2-sol"]["label"], ms["gpt-6.2-sol"]["efforts"]), ("GPT-6.2-Sol", ["low", "high"]))
        self.assertEqual((ms["sol"]["label"], ms["sol"]["efforts"]), ("GPT-6.1-Sol", ["medium"]))  # the ladder name
        self.assertIn("astra", ms)  # a ladder name the cache does not list stays

    def test_goose_names_its_configured_model_when_its_cache_is_empty(self):
        from pathlib import Path
        from river.agents import goose
        cfg = Path(self.tmp.name, "config.yaml")
        cfg.write_text("GOOSE_MODE: auto\nactive_provider: chatgpt_codex\nproviders:\n  anthropic:\n"
                       "    model: claude-opus-5-5\n  chatgpt_codex:\n    model: gpt-5.6-terra\n    configured: true\n"
                       "GOOSE_THINKING_EFFORT: medium\n", encoding="utf-8")
        self.assertEqual(goose.configured_models(cfg), ["chatgpt_codex/gpt-5.6-terra", "anthropic/claude-opus-5-5"])

    def test_the_default_model_s_efforts_are_those_of_the_agent_s_models(self):
        self.assertEqual(core._efforts_of([{"efforts": ["low"]}, {"efforts": ["medium", "xhigh"]}],
                                          list(agents.MAXPM_EFFORTS)), ["low", "medium", "xhigh"])
        self.assertEqual(core._efforts_of([{"efforts": ["low"]}, {"name": "x"}], ["low", "max"]), ["low", "max"])
        agy = next(o for o in core.launch_options(self.c) if o["platform"] == "antigravity")
        self.assertEqual(agy["efforts"], ["low", "medium", "high", "xhigh"])  # no agy model takes max


READERS = {"chat": "chat_settings", "screen": "screen_settings", "config": "config_settings"}

# (agent, setting) -> the fix item of a setting that the Settings dialog cannot read yet when the command line names
# none. The test fails when one of them has a source now.
SETTINGS_EXCEPTIONS = {}


def settings_problems(a):
    """The settings of a running session that the Settings dialog cannot read without a command line that names them
    (#6280), as sentences: no source in SETTINGS_READ, or a source whose reader the adapter lacks. A toggle with a
    flag needs none."""
    reads, out = a.settings_read(), []
    for s in a.session_specs():
        if s.get("flag"):
            continue
        src = reads.get(s["key"], ())
        if not src:
            out.append((s["key"], f"{s['key']}: no source in SETTINGS_READ (chat, screen or config)"))
        for x in src:
            if x not in READERS or not a.has(READERS[x]):
                out.append((s["key"], f"{s['key']}: SETTINGS_READ names {x!r}, and the adapter has no "
                                      f"{READERS.get(x, 'such reader')}"))
    return out


class SettingsRead(unittest.TestCase):
    """#6280 (mark 2026-11-20: 'we need that to work for all agent types'): a running session's Settings dialog shows
    the values that session runs with, never 'not known', also when it started with none named."""

    def test_every_setting_of_every_agent_has_a_source_besides_the_command_line(self):
        for platform, a in platforms():
            for key, problem in settings_problems(a):
                with self.subTest(agent=a.id, setting=key):
                    if (a.id, key) in SETTINGS_EXCEPTIONS:
                        continue
                    self.fail(f"{a.label}: {problem}")
        for (aid, key), item in SETTINGS_EXCEPTIONS.items():
            with self.subTest(exception=(aid, key)):
                self.assertIn(key, [k for k, _ in settings_problems(agents.get(aid))],
                              f"{aid} {key} has a source now: take it out of SETTINGS_EXCEPTIONS ({item})")

    def test_the_dialog_shows_the_agent_s_own_config_last(self):
        from river import sessions
        c = core.connect()
        self.addCleanup(c.close)
        core.register(c, "cx1")
        c.execute("UPDATE agents SET agent_type='codex', pid_cmd='codex --add-dir /q go' WHERE name='cx1'")
        with mock.patch.object(agents.get("codex").module, "config_settings",
                               lambda: {"model": "gpt-6.1-sol", "effort": "medium"}):
            got = {s["key"]: s for s in sessions.session_settings(c, "cx1")["settings"]}
        # gpt-6.1-sol is the id of the ladder name sol: the dialog names the model as its list does.
        self.assertEqual((got["model"]["value"], got["model"]["source"]), ("sol", "config"))
        self.assertEqual((got["effort"]["value"], got["effort"]["source"]), ("medium", "config"))

    def test_each_config_reader(self):
        from pathlib import Path
        from river.agents import codex, cursor, hermes, pi
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        home = Path(d.name)
        (home / "config.toml").write_text('model = "gpt-6.1-terra"\nmodel_reasoning_effort = "medium"\n'
                                          '[profiles.x]\nmodel = "other"\n', encoding="utf-8")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            self.assertEqual(codex.config_settings(), {"model": "gpt-6.1-terra", "effort": "medium", "sandbox": None,
                                                       "approval": None})
        (home / ".pi" / "agent").mkdir(parents=True)
        (home / ".pi" / "agent" / "settings.json").write_text(
            '{"defaultProvider": "openai-codex", "defaultModel": "gpt-6.1-sol"}', encoding="utf-8")
        (home / ".cursor").mkdir()
        (home / ".cursor" / "cli-config.json").write_text(
            '{"model": {"modelId": "gpt-5.6-sol-high"}, "sandbox": {"mode": "disabled"}}', encoding="utf-8")
        with mock.patch.object(Path, "home", lambda: home):
            self.assertEqual(pi.config_settings(), {"model": "openai-codex/gpt-6.1-sol", "effort": None})
            self.assertEqual(cursor.config_settings(), {"model": "gpt-5.6-sol", "effort": "high", "sandbox": "disabled",
                                                        "mode": "agent"})
        (home / "config.yaml").write_text("agent:\n  reasoning_effort: \"medium\"\n", encoding="utf-8")
        self.assertEqual(hermes.config_settings({"HERMES_HOME": str(home)}), {"effort": "medium"})

    def test_aider_and_pi_logs_name_the_model(self):
        from river.agents import aider, pi
        self.assertEqual(aider.chat_settings(["Model: anthropic/claude-sonnet-4-5 with diff edit format, reasoning high"]),
                         {"edit_format": "diff", "model": "anthropic/claude-sonnet-4-5", "effort": "high"})
        self.assertEqual(pi.chat_settings(['{"type":"model_change","provider":"openai-codex","modelId":"gpt-6.1-sol"}',
                                           '{"type":"thinking_level_change","thinkingLevel":"medium"}']),
                         {"model": "openai-codex/gpt-6.1-sol", "effort": "medium"})


class AgentDefaults(Db):
    """#6264 (mark 2026-11-19: 'set neon medium as our default agent for now'): each agent has a default model and
    effort, <prefix>model and <prefix>effort; a start that names none takes them, and the dialog preselects them."""

    def setUp(self):
        super().setUp()
        core.config_set(self.c, "launch_agents", "Antigravity CLI=@antigravity; Claude Code=@claude-code", actor="mark")

    def test_the_settings_are_checked_against_the_agent_s_own_list(self):
        core.config_set(self.c, "agy_model", "gemini-4-neon", actor="mark")
        core.config_set(self.c, "agy_effort", "medium", actor="mark")
        with self.assertRaisesRegex(core.RiverError, "Antigravity CLI has no model 'opus'"):
            core.config_set(self.c, "agy_model", "opus", actor="mark")
        with self.assertRaisesRegex(core.RiverError, "gemini-4-neon takes effort low, medium, high, xhigh, not max"):
            core.config_set(self.c, "agy_effort", "max", actor="mark")
        self.assertEqual(core.setting(self.c, "claude_model"), "")  # every agent has the setting, empty by default

    def test_a_start_that_names_none_takes_them_and_a_named_one_wins(self):
        core.config_set(self.c, "agy_model", "gemini-4-neon", actor="mark")
        core.config_set(self.c, "agy_effort", "medium", actor="mark")
        r = core._launch_agent_cmd(self.c, None, "Antigravity CLI")
        self.assertEqual((r["model"], r["effort"]), ("gemini-4-neon", "medium"))
        self.assertIn("--model gemini-4-neon --effort medium", r["command"])
        r = core._launch_agent_cmd(self.c, None, "Antigravity CLI", "gemini-4-carbon", "low")
        self.assertEqual((r["model"], r["effort"]), ("gemini-4-carbon", "low"))
        # A default effort that the chosen model does not take goes; the start does not fail for it.
        r = core._launch_agent_cmd(self.c, None, "Antigravity CLI", "gemini-pro-orchestrator")
        self.assertEqual(r["effort"], None)
        self.assertNotIn("--effort", r["command"])
        r = core._launch_agent_cmd(self.c, None, "Antigravity CLI", "gpt-oss-120b")  # takes only medium
        self.assertEqual(r["effort"], "medium")

    def test_the_dialog_gets_them(self):
        core.config_set(self.c, "agy_model", "gemini-4-neon", actor="mark")
        core.config_set(self.c, "agy_effort", "medium", actor="mark")
        agy = next(o for o in core.launch_options(self.c) if o["platform"] == "antigravity")
        self.assertEqual((agy["default_model"], agy["default_effort"]), ("gemini-4-neon", "medium"))
        cc = next(o for o in core.launch_options(self.c) if o["platform"] == "claude-code")
        self.assertEqual((cc["default_model"], cc["default_effort"]), (None, None))


if __name__ == "__main__":
    unittest.main()
