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
        mock.patch.dict(antigravity._models, {"at": 0.0, "list": None, "reader": None}).start()

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
        mock.patch.object(antigravity, "MODELS_RUNNER", lambda: "Fetching available models...\n"
                          "gemini-5-neon-low\tGemini 5 Neon (Low)\ngemini-5-neon-max\tGemini 5 Neon (Max)\n"
                          "gemini-4-neon-high\tGemini 4 Neon (High)\n").start()
        antigravity.read_models()  # starts the read; until it ends the list is MODELS_SEEN
        antigravity._models["reader"] and antigravity._models["reader"].join()
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


if __name__ == "__main__":
    unittest.main()
