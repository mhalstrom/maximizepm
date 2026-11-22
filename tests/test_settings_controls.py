"""Settings controls (#6865): server choice lists, validation, and page selects / row editors."""
import tempfile
import unittest
from pathlib import Path

from river import core
from tests import test_page, test_view_sessions


class SettingsChoicesTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.conn = core.connect(Path(self.dir.name) / "q.db")

    def tearDown(self):
        self.conn.close()
        self.dir.cleanup()

    def test_choices_match_server_validation_lists(self):
        ch = core.config_list(self.conn)["choices"]
        self.assertEqual(ch["agent_types"], core.agent_types())
        self.assertEqual(ch["efforts"], ["low", "medium", "high", "xhigh", "max"])
        keys = ch["keys"]
        for k in ("default_agent", "agent_for.worker", "agent_for.manager",
                  "agent_for.reviewer", "agent_for.deployer", "agent_for.expert"):
            self.assertEqual(keys[k]["kind"], "agent")
            self.assertEqual(keys[k]["values"], core.agent_types())
            self.assertTrue(keys[k]["allow_empty"])
        self.assertEqual(keys["default_effort"]["kind"], "select")
        self.assertEqual(keys["default_effort"]["values"], ch["efforts"])
        self.assertTrue(keys["default_effort"]["allow_empty"])
        for k in ("default_min_model", "default_max_model"):
            self.assertEqual(keys[k]["kind"], "select")
            self.assertEqual(keys[k]["values"], ch["ladder_models"])
            self.assertTrue(keys[k]["allow_empty"])
        self.assertEqual(keys["default_model"]["kind"], "model")
        self.assertTrue(keys["default_model"]["allow_empty"])
        self.assertEqual(keys["usage_list_hide"]["kind"], "usage_list")
        for k in ("agent_order", "agent_rules", "launch_agents"):
            self.assertEqual(keys[k]["kind"], k)

    def test_server_still_refuses_unknown_values(self):
        bad = [
            ("default_effort", "bogus"),
            ("default_agent", "no-such-agent"),
            ("agent_for.worker", "no-such-agent"),
            ("default_min_model", "no-such-model"),
            ("default_max_model", "no-such-model"),
            ("agent_order", "no-such-agent max 2"),
            ("agent_rules", "no-such-agent: *.css"),
            ("launch_agents", "Bad=@no-such-agent"),
            ("usage_list_hide", "no-such-agent"),
        ]
        for k, v in bad:
            with self.subTest(key=k, value=v):
                with self.assertRaises(core.RiverError):
                    core.config_set(self.conn, k, v)


class SettingsControlsBrowser(test_view_sessions.TmuxBase):
    drive = test_page.PageBrowser.drive

    def test_settings_render_selects_and_row_editors_and_save_exact_format(self):
        core.config_set(self.c, "default_agent", "claude-code", project="site")
        core.config_set(self.c, "agent_order", "claude-code model opus", project="site")
        core.config_set(self.c, "agent_order", "claude-code model opus max 2")
        ch = core.config_list(self.c)["choices"]

        driver = r'''
const errors = window.__pageErrors;
try {
  await until(() => window.__appReady && q("#sessYouRow .srow") && q("#sessSettings"), "page ready");
  q("#actor").value = "mark"; q("#actor").dispatchEvent(new Event("change"));
  // 1. Open Settings dialog (Agents tab)
  q("#sessSettings").click();
  await until(() => !q("#listSetDlg").classList.contains("hidden") && q('#listSetTabs [data-ls-part="agents"]'), "settings dialog");
  q('#listSetTabs [data-ls-part="agents"]').click();
  await until(() => q('#listSetRows [data-set-input="default_effort"]'), "dialog agents tab");

  const dlgNormalSelKeys = [
    "default_model", "default_effort", "default_max_model", "default_agent", "agent_for.expert"
  ];
  for (const k of dlgNormalSelKeys) {
    const el = q(`#listSetRows select[data-set-input="${k}"]`);
    check(el, `dialog: ${k} is a select`);
    check(el.options[0].value === "", `dialog: ${k} has empty first option`);
  }
  const dlgEff = [...q('#listSetRows select[data-set-input="default_effort"]').options].map(o => o.value).filter(Boolean);
  check(JSON.stringify(dlgEff) === JSON.stringify(EXPECTED_EFFORTS), "dialog effort options match server: " + dlgEff);
  const dlgAg = [...q('#listSetRows select[data-set-input="default_agent"]').options].map(o => o.value).filter(Boolean);
  check(JSON.stringify(dlgAg) === JSON.stringify(EXPECTED_AGENTS), "dialog agent options match server: " + dlgAg);

  // usage_list_hide has checkboxes, no free text input, and only one row
  check(document.querySelectorAll('#listSetRows [data-set-row="usage_list_hide"]').length === 1, "one usage_list_hide row");
  check(!q('#listSetRows input[data-set-input="usage_list_hide"]'), "usage_list_hide is not a text input");
  check(q('#listSetRows [data-set-usage="usage_list_hide"]'), "usage_list_hide renders checkboxes container");

  // agent_order renders row editor on normal Agents tab; agent_rules & launch_agents render row editors in Advanced
  check(q('#listSetRows [data-set-editor="agent_order"]'), "dialog: agent_order renders row editor");

  // Open Advanced in dialog to verify remaining selects & row editors
  q("#listSetAdv > summary").click();
  await until(() => q("#listSetAdv").open && q('#listSetAdvRows select[data-set-input="default_min_model"]'), "dialog advanced");
  for (const k of ["default_min_model", "agent_for.worker", "agent_for.manager", "agent_for.reviewer", "agent_for.deployer"]) {
    const el = q(`#listSetAdvRows select[data-set-input="${k}"]`);
    check(el && el.options[0].value === "", `dialog advanced: ${k} is a select`);
  }
  for (const k of ["agent_rules", "launch_agents"]) {
    check(q(`#listSetAdvRows [data-set-editor="${k}"]`), `dialog advanced: ${k} renders row editor`);
  }

  // Project overrides use the same controls
  const ovAg = q('#listSetRows select[data-set-input="default_agent"][data-set-scope="project:site"]');
  check(ovAg && ovAg.value === "claude-code", "project:site default_agent override is a select");
  const ovOrd = q('#listSetRows [data-set-editor="agent_order"][data-set-scope="project:site"]');
  check(ovOrd && ovOrd.querySelector('[data-ed-row="0"]'), "project:site agent_order override is a row editor");

  // Save default_effort, default_agent, and project:site default_agent via selects
  const effEl = q('#listSetRows select[data-set-input="default_effort"]');
  effEl.value = "high"; effEl.dispatchEvent(new Event("change", { bubbles: true }));
  await sleep(300);

  const agEl = q('#listSetRows select[data-set-input="default_agent"]');
  agEl.value = "codex"; agEl.dispatchEvent(new Event("change", { bubbles: true }));
  await sleep(300);

  const ovAg2 = q('#listSetRows select[data-set-input="default_agent"][data-set-scope="project:site"]');
  ovAg2.value = "codex"; ovAg2.dispatchEvent(new Event("change", { bubbles: true }));
  await sleep(300);

  // Edit agent_order max sessions in the row editor
  const maxEl = q('#listSetRows [data-set-editor="agent_order"]:not([data-set-scope]) [data-ed-row="0"] [data-ed-field="max"]');
  check(maxEl, "agent_order max input");
  maxEl.value = "4"; maxEl.dispatchEvent(new Event("change", { bubbles: true }));
  await sleep(300);

  // Add an agent_rules row via the row editor in Advanced
  q('#listSetAdvRows [data-set-editor="agent_rules"] [data-ed-add]').click();
  await until(() => q('#listSetAdvRows [data-set-editor="agent_rules"] [data-ed-row="0"] [data-ed-field="patterns"]'), "agent_rules row added");
  const pat = q('#listSetAdvRows [data-set-editor="agent_rules"] [data-ed-row="0"] [data-ed-field="patterns"]');
  pat.value = "*.css, logo"; pat.dispatchEvent(new Event("change", { bubbles: true }));
  await sleep(300);

  // Close dialog and also verify Settings > Agents tab
  q('#listSetDlg [data-ls-close]').click();
  await until(() => q("#listSetDlg").classList.contains("hidden"), "dialog closed");
  q("#hdrMore").click(); q("#settingsBtn").click();
  await until(() => q('#setTabs [data-set-tab="agents"]'), "settings tab");
  q('#setTabs [data-set-tab="agents"]').click();
  await until(() => q('#setRows select[data-set-input="default_effort"]'), "settings > agents selects");
  check(q('#setRows select[data-set-input="default_effort"]').value === "high", "Settings > Agents shows saved default_effort");
  check(q('#setRows select[data-set-input="default_agent"]').value === "codex", "Settings > Agents shows saved default_agent");
  check(q('#setRows [data-set-editor="agent_order"]'), "Settings > Agents shows agent_order row editor");
  q("#setAdvanced > summary").click();
  await until(() => q('#setAdvRows [data-set-editor="agent_rules"]'), "Settings Advanced shows agent_rules row editor");
  check(q('#setAdvRows [data-set-editor="launch_agents"]'), "Settings Advanced shows launch_agents row editor");

  post({ ok: true, errors });
} catch (e) { post({ error: String(e.stack || e) + " errors: " + errors.join(" | ") }); }
'''
        driver = (driver
                  .replace("EXPECTED_EFFORTS", repr(ch["efforts"]))
                  .replace("EXPECTED_AGENTS", repr(ch["agent_types"])))
        got = self.drive(driver)
        self.assertEqual(got.get("error"), None, got)
        self.assertTrue(got.get("ok"), got)
        self.assertEqual(got.get("errors"), [])
        self.assertEqual(core.setting(self.c, "default_effort"), "high")
        self.assertEqual(core.setting(self.c, "default_agent"), "codex")
        pid = self.c.execute("SELECT id FROM projects WHERE name='site'").fetchone()["id"]
        self.assertEqual(core.setting(self.c, "default_agent", project_id=pid), "codex")
        self.assertEqual(core.setting(self.c, "agent_order"), "claude-code model opus max 4")
        self.assertIn("*.css, logo", core.setting(self.c, "agent_rules"))


if __name__ == "__main__":
    unittest.main()
