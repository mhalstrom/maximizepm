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

    def test_the_drawer_names_any_agent_as_an_agent_type(self):
        text = (STATIC / "components" / "drawer.js").read_text(encoding="utf-8")
        self.assertNotIn("codex or claude-code;", text)
        self.assertIn("opencode", text)


if __name__ == "__main__":
    unittest.main()
