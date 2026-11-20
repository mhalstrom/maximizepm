"""The launch dialog's texts for every registered agent (#5237): river/static/components/launchDialog.js."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "river" / "static"


def note(agent, opt):
    """placeholderNote(agent, opt) of the launch dialog, run in node."""
    js = ("const m = await import('./components/launchDialog.js');"
          f"console.log(JSON.stringify(m.placeholderNote({json.dumps(agent)}, {json.dumps(opt)})));")
    out = subprocess.run(["node", "--input-type=module", "-e", js], cwd=STATIC, capture_output=True, text=True,
                         timeout=30, check=True)
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which("node"), "node is not on this machine")
class PlaceholderNote(unittest.TestCase):
    def test_a_profile_has_no_note_also_when_its_agent_has_no_effort(self):
        # OpenCode=@opencode: the adapter builds the command, and OpenCode has no effort at all.
        self.assertEqual(note("OpenCode", {"platform": "opencode", "takes_model": True, "takes_effort": False,
                                           "efforts": []}), "")
        self.assertEqual(note("Codex", {"platform": "codex", "takes_model": True, "takes_effort": True,
                                        "efforts": ["low", "high"]}), "")

    def test_a_custom_command_names_only_the_placeholders_it_can_miss(self):
        self.assertEqual(note("Mine", {"platform": None, "takes_model": True, "takes_effort": False, "efforts": []}), "")
        got = note("Mine", {"platform": None, "takes_model": True, "takes_effort": False, "efforts": ["low", "high"]})
        self.assertIn("The Mine command has no {effort} placeholder, so the agent starts with its own effort", got)
        got = note("Mine", {"platform": None, "takes_model": False, "takes_effort": True, "efforts": ["low"]})
        self.assertIn("has no {model} placeholder, so the agent starts with its own model", got)
        self.assertNotIn("@codex", got)  # a profile is @ and the agent's name, for every registered agent

    def test_the_folder_picked_for_a_project_with_none_goes_to_the_server(self):
        # #5894: the dialog's Folder for <project> field; with none typed, nothing goes (the project has a folder).
        js = ("const m = await import('./components/launchDialog.js');"
              "console.log(JSON.stringify([m.launchArgs({agent: 'A', newFolder: '~/Projects/web'}),"
              " m.launchArgs({agent: 'A', newFolder: ''})]));")
        out = subprocess.run(["node", "--input-type=module", "-e", js], cwd=STATIC, capture_output=True, text=True,
                             timeout=30, check=True)
        self.assertEqual(json.loads(out.stdout), [{"agent": "A", "folder": "~/Projects/web"}, {"agent": "A"}])

    def test_the_effort_list_follows_the_model_and_the_weakest_allowed_model_is_preselected(self):
        # #6256: a model with its own levels (Antigravity) offers them; one with none offers no effort; a model with
        # no levels of its own offers the agent's. The list is strongest first, so a min limit picks from the bottom.
        opt = {"efforts": ["low", "medium", "high", "xhigh", "max"], "models": [
            {"name": "gemini-4-carbon", "efforts": ["low", "medium", "high"]},
            {"name": "gemini-pro-orchestrator", "efforts": []}, {"name": "other"}]}
        claude = {"efforts": ["low", "high"], "models": [{"name": n} for n in ("fable", "opus", "sonnet", "haiku")]}
        ladder = {"claude": ["haiku", "sonnet", "opus", "fable"]}
        js = ("const m = await import('./components/launchDialog.js');"
              f"const o = {json.dumps(opt)}, c = {json.dumps(claude)}, l = {json.dumps(ladder)};"
              "console.log(JSON.stringify([m.effortsFor(o, 'gemini-4-carbon'), m.effortsFor(o, 'gemini-pro-orchestrator'),"
              " m.effortsFor(o, 'other'), m.effortsFor(o, ''), m.pickEffort(o, {effort: 'max'}, '', 'gemini-4-carbon'),"
              " m.pickEffort(o, null, 'xhigh', 'gemini-4-carbon'), m.pickModel(c, {min_model: 'sonnet'}, l, '', {})]));")
        out = subprocess.run(["node", "--input-type=module", "-e", js], cwd=STATIC, capture_output=True, text=True,
                             timeout=30, check=True)
        self.assertEqual(json.loads(out.stdout), [["low", "medium", "high"], [], opt["efforts"], opt["efforts"],
                                                  "high", "", "sonnet"])

    def test_the_agent_s_default_model_and_effort_are_preselected(self):
        # #6264: agy_model gemini-4-neon, agy_effort medium; an item's own recommendation still wins.
        opt = {"efforts": ["low", "medium", "high"], "default_model": "gemini-4-neon", "default_effort": "medium",
               "models": [{"name": "gemini-4-neon", "efforts": ["low", "medium", "high", "xhigh"]},
                          {"name": "gemini-4-carbon", "efforts": ["low", "medium", "high"]}]}
        js = ("const m = await import('./components/launchDialog.js');"
              f"const o = {json.dumps(opt)};"
              "console.log(JSON.stringify([m.pickModel(o, null, {}, 'gemini-4-carbon', {}),"
              " m.pickEffort(o, null, 'low', 'gemini-4-neon'), m.pickEffort(o, {effort: 'high'}, '', 'gemini-4-neon'),"
              " m.pickModel(o, {model: 'gemini-4-carbon'}, {}, '', {})]));")
        out = subprocess.run(["node", "--input-type=module", "-e", js], cwd=STATIC, capture_output=True, text=True,
                             timeout=30, check=True)
        self.assertEqual(json.loads(out.stdout), ["gemini-4-neon", "medium", "high", "gemini-4-carbon"])

    def test_an_item_s_model_words_show_only_for_an_agent_that_has_the_model(self):
        # #6257: with Antigravity CLI chosen, '#6250 recommends opus. #6250 allows at most opus.' said nothing true.
        agy = {"efforts": ["low", "high"], "models": [{"name": "gemini-4-neon", "label": "Gemini 4 Neon",
                                                       "efforts": ["low", "high"]}]}
        claude = {"efforts": ["low", "max"], "models": [{"name": "opus"}, {"name": "sonnet"}]}
        it = {"id": 6250, "model": "opus", "effort": "high", "max_model": "opus"}
        js = ("const m = await import('./components/launchDialog.js');"
              f"const it = {json.dumps(it)};"
              f"console.log(JSON.stringify([m.itemNotes({json.dumps(agy)}, it, {{}}), m.itemNotes({json.dumps(claude)}, it, {{}})]));")
        out = subprocess.run(["node", "--input-type=module", "-e", js], cwd=STATIC, capture_output=True, text=True,
                             timeout=30, check=True)
        self.assertEqual(json.loads(out.stdout), [
            {"rec": "#6250 recommends high effort.", "limits": ""},
            {"rec": "#6250 recommends opus.", "limits": "#6250 allows at most opus."}])

    def test_the_drawer_names_any_agent_as_an_agent_type(self):
        text = (STATIC / "components" / "drawer.js").read_text(encoding="utf-8")
        self.assertNotIn("codex or claude-code;", text)
        self.assertIn("opencode", text)


if __name__ == "__main__":
    unittest.main()
