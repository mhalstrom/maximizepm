"""The texts of the agent update dialog and of Settings > Agents (river/static/components/agentUpdate.js, #6460: a few
words per row, the long message only in a failed row's Details), run in node."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from river import agentupdate as au
from tests import test_page, test_view_sessions

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

    def test_an_old_done_update_yields_to_a_newer_version(self):
        # After an earlier update to g00d70a, a newer version gbb6f68 arrived (#6899): the row must offer Update now
        # and Later for installed → latest, not 'Updated ✓' with only Close.
        r = row("Amp", installed="0.0.1795204834-g00d70a", latest="0.0.1795363233-gbb6f68", newer=True, update={
            "state": "done", "old": "0.0.1795190440-gf3619d", "new": "0.0.1795204834-g00d70a",
            "message": "updated 0.0.1795190440-gf3619d → 0.0.1795204834-g00d70a; the test answered OK"})
        self.assertEqual(run("m.stateText(D.r)", r=r), "")
        self.assertEqual(run("m.verText(D.r)", r=r), "0.0.1795204834-g00d70a → 0.0.1795363233-gbb6f68")
        dlg = run("m.dialogHtml(D.rows)", rows=[r])
        self.assertIn(">Update Amp</h2>", dlg)
        self.assertIn("0.0.1795204834-g00d70a → 0.0.1795363233-gbb6f68", dlg)
        self.assertIn('data-au-go="amp">Update now</button>', dlg)
        self.assertIn('data-au-later="1"', dlg)
        self.assertNotIn("Updated ✓", dlg)
        self.assertNotIn("gf3619d", dlg)
        self.assertNotIn(">Close</button>", dlg)
        lst = run("m.versionsHtml(D.s)", s={"agents": [r]})
        self.assertIn("0.0.1795204834-g00d70a → <b>0.0.1795363233-gbb6f68</b>", lst)
        self.assertNotIn("Updated ✓", lst)


class AgentUpdateDialogBrowser(test_view_sessions.TmuxBase):
    drive = test_page.PageBrowser.drive

    def _seed_amp(self):
        au.change(self.c, lambda d: d["agents"].update(amp={
            "installed": "0.0.1795204834-g00d70a",
            "latest": "0.0.1795363233-gbb6f68",
            "how": "npm",
            "package": "@sourcegraph/amp",
            "path": "/usr/local/bin/amp",
            "checked_at": 1795363300.0,
            "update": {
                "state": "done",
                "old": "0.0.1795190440-gf3619d",
                "new": "0.0.1795204834-g00d70a",
                "message": "updated 0.0.1795190440-gf3619d → 0.0.1795204834-g00d70a; the test answered OK",
            },
        }))

    def _run_dialog_close_test(self, size=None):
        self._seed_amp()
        driver = r'''
const errors = window.__pageErrors;
try {
  await until(() => window.__appReady, "page ready");
  if (!sessionStorage.getItem("au_reloaded")) {
    await until(() => q("#auDlg") && !q("#auDlg").classList.contains("hidden") && q("#auTitle"), "update dialog opens");
    const dlg = q("#auDlg");
    check(q("#auTitle").textContent.trim() === "Update Amp", "title: " + q("#auTitle").textContent);
    const txt = dlg.textContent;
    check(txt.includes("0.0.1795204834-g00d70a → 0.0.1795363233-gbb6f68"), "shows installed → latest: " + txt);
    check(!txt.includes("Updated ✓"), "no stale Updated ✓: " + txt);
    check(!txt.includes("gf3619d"), "no old version: " + txt);
    const go = q('#auDlg [data-au-go="amp"]');
    check(go && go.textContent.trim() === "Update now", "Update now button");
    const later = q('#auDlg [data-au-later]');
    check(later && later.textContent.trim() === "Later", "Later button");
    check(document.documentElement.scrollWidth <= window.innerWidth + 1, "no horizontal overflow");
    q('#auDlg [data-au-close]').click();
    await until(() => q("#auDlg").classList.contains("hidden"), "dialog closes on X");
    let hidden = false;
    for (let i = 0; i < 40 && !hidden; i++) {
      const r = await fetch("api/agent-versions").then(x => x.json());
      const a = r.agents.find(x => x.id === "amp");
      if (a && a.later && !a.dialog) hidden = true;
      else await sleep(50);
    }
    check(hidden, "X persisted later on server");
    sessionStorage.setItem("au_reloaded", "1");
    location.reload();
  } else {
    await sleep(400);
    const r = await fetch("api/agent-versions").then(x => x.json());
    const a = r.agents.find(x => x.id === "amp");
    check(a && a.later && !a.dialog, "still hidden after reload");
    check(!q("#auDlg") || q("#auDlg").classList.contains("hidden"), "dialog stays closed after reload");
    check(!errors.length, "page errors: " + errors.join("; "));
    post({ ok: true });
  }
} catch (e) {
  post({ error: String(e && e.stack || e) });
}
'''
        res = self.drive(driver, size=size)
        self.assertTrue(res.get("ok"))
        row = next(r for r in au.status(self.c)["agents"] if r["id"] == "amp")
        self.assertTrue(row["later"])
        self.assertFalse(row["dialog"])

    def test_old_done_update_opens_with_update_now_and_x_persists_across_reload(self):
        self._run_dialog_close_test(size="1240,820")

    def test_old_done_update_dialog_on_phone_width(self):
        self._run_dialog_close_test(size="390,844")


if __name__ == "__main__":
    unittest.main()
