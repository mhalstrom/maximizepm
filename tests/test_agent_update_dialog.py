"""The texts of the agent update dialog and of Settings > Agents (river/static/components/agentUpdate.js, #6460: a few
words per row, the long message only in a failed row's Details), run in node."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "river" / "static"


def row(label="Grok CLI", update=None, installed="1.0.0", latest="1.1.0", newer=True):
    return {"id": label.split()[0].lower(), "label": label, "installed": installed, "latest": latest, "newer": newer,
            "path": "/x", "how": "npm", "can_update": True, "auto": False, "update": update}


def run(expr, **data):
    js = (f"globalThis.document = {{ addEventListener() {{}} }};"  # the module listens for visibilitychange
          f" const m = await import('./components/agentUpdate.js'); const D = {json.dumps(data)};"
          f" console.log(JSON.stringify({expr}));")
    out = subprocess.run(["node", "--input-type=module", "-e", js], cwd=STATIC, capture_output=True, text=True,
                         timeout=30, check=True)
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which("node"), "node is not on this machine")
class AgentUpdateTexts(unittest.TestCase):
    def state(self, update, **kw):
        return run("m.stateText(D.r)", r=row(update=update, **kw))

    def test_each_state_is_a_few_words(self):
        self.assertEqual(self.state({"state": "queued"}), "Queued")
        self.assertEqual(self.state({"state": "queued", "waits": 2}), "Waits for 2 sessions")
        self.assertEqual(self.state({"state": "queued", "waits": 1}), "Waits for 1 session")
        self.assertEqual(self.state({"state": "updating"}), "Updating…")
        self.assertEqual(self.state({"state": "testing", "new": "1.1.0"}), "Testing…")
        self.assertEqual(self.state({"state": "done", "old": "1.0.0", "new": "1.1.0", "message": "updated 1.0.0 → 1.1.0; "
                                     "the test answered OK"}), "Updated ✓")
        self.assertEqual(self.state({"state": "done", "old": "1.1.0", "new": "1.1.0"}), "Already newest")
        self.assertEqual(self.state({"state": "failed", "kept": True, "message": "the test after the update failed"}),
                         "Failed, rolled back")
        self.assertEqual(self.state({"state": "failed", "message": "MaximizePM does not know how Grok CLI was installed"}),
                         "Failed")
        self.assertEqual(self.state({"state": "cancelled", "message": "cancelled by mark"}), "Cancelled")
        self.assertEqual(self.state(None), "")

    def test_the_title_and_no_paragraph(self):
        two = run("m.dialogHtml(D.rows)", rows=[row(), row("Codex")])
        self.assertIn(">Updates for 2 agents</h2>", two)
        self.assertNotIn("<p", two)
        self.assertIn(">Update Grok CLI</h2>", run("m.dialogHtml(D.rows)", rows=[row()]))
        self.assertIn(">Agents are up to date</h2>", run("m.dialogHtml(D.rows)", rows=[
            row(installed="1.1.0", newer=False, update={"state": "done", "old": "1.0.0", "new": "1.1.0"}),
            row("Codex", installed="2.0.0", latest="2.0.0", newer=False, update={"state": "cancelled"})]))

    def test_a_done_row_shows_both_versions_once(self):
        html = run("m.dialogHtml(D.rows)", rows=[row(installed="1.1.0", newer=False, update={
            "state": "done", "old": "1.0.0", "new": "1.1.0", "message": "updated 1.0.0 → 1.1.0; the test answered OK"})])
        self.assertIn("1.0.0 → 1.1.0</span>", html)
        self.assertEqual(html.count("1.0.0"), 1)
        self.assertNotIn("test answered", html)

    def test_a_failed_row_has_its_message_in_a_closed_details(self):
        r = row(update={"state": "failed", "message": "the update failed (exit 1); 1.0.0 still runs", "kept": True})
        for html in (run("m.dialogHtml(D.rows)", rows=[r]), run("m.versionsHtml(D.s)", s={"agents": [r]})):
            self.assertIn("Failed, rolled back", html)
            self.assertIn('<details class="au-why t-s" data-au-more="grok"><summary>Details</summary>', html)
            self.assertIn("The update failed (exit 1); 1.0.0 still runs", html)
        self.assertNotIn("<details", run("m.dialogHtml(D.rows)", rows=[row(update={"state": "queued"})]))


if __name__ == "__main__":
    unittest.main()
