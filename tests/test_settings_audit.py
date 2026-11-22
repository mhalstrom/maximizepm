"""Settings audit guards (#6866): every DEFAULT_SETTINGS key has a code reader, removed keys in DB are ignored cleanly,
and the page shows only NORMAL_SETTINGS on normal tabs with the rest in a closed, searchable Advanced section."""
import ast
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from river import core
from tests import test_page, test_view_sessions

ROOT = Path(__file__).resolve().parent.parent
SKIP_FUNCS = {
    "config_set", "config_unset", "config_list", "setting_choices", "setting_default",
    "agent_setting_defaults", "mask", "_platform_setting", "_check_option", "_agent_option",
}


def find_setting_readers(keys):
    """Return {key: [file::func, ...]} of real code readers for each setting key outside defaults/labels/validators."""
    readers = {k: [] for k in keys}
    for plat, pl in core.launch_platforms().items():
        for opt in pl["options"]:
            k = pl["prefix"] + opt
            if k in readers:
                readers[k].append(f"river/core.py::profile_options ({plat})")
    for r in core.AGENT_ROLES:
        k = f"agent_for.{r}"
        if k in readers:
            readers[k].append("river/core.py::_effective_models")
    for f in core.MODEL_FIELDS:
        k = f"default_{f}"
        if k in readers:
            readers[k].append("river/core.py::_effective_models")

    for path in sorted((ROOT / "river").rglob("*.py")):
        rel = str(path.relative_to(ROOT))
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        fmap = sorted(
            [(n.lineno, getattr(n, "end_lineno", n.lineno), n.name)
             for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))],
            key=lambda x: x[1] - x[0],
        )

        def enc(ln):
            for a, b, nm in fmap:
                if a <= ln <= b:
                    return nm
            return "<module>"

        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                fn = (n.func.attr if isinstance(n.func, ast.Attribute)
                      else (n.func.id if isinstance(n.func, ast.Name) else ""))
                if fn in ("setting", "setting_of", "_dur", "_setting", "parse_duration",
                          "execute", "_lock_run", "_item_loops", "count_window"):
                    for arg in list(n.args) + [kw.value for kw in n.keywords]:
                        for sub in ast.walk(arg):
                            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                                for k in keys:
                                    if sub.value == k or (fn == "execute" and f"'{k}'" in sub.value):
                                        en = enc(n.lineno)
                                        if not (rel == "river/core.py" and en in SKIP_FUNCS):
                                            readers[k].append(f"{rel}::{en}")
        for line_no, line in enumerate(src.splitlines(), 1):
            en = enc(line_no)
            if rel == "river/core.py" and (en in SKIP_FUNCS or line_no < 760):
                continue
            if rel == "river/space.py" and "space_" in line:
                for k in keys:
                    if f'"{k}"' in line:
                        readers[k].append(f"{rel}::{en}")
            if rel == "river/core.py" and en == "_lease_room" and "max_leases" in line:
                for k in ("max_leases", "goal_max_leases"):
                    if k in readers:
                        readers[k].append(f"{rel}::{en}")
            if "notify_batch_window" in line or "question_nudge_after" in line:
                for k in ("notify_batch_window", "question_nudge_after"):
                    if f'"{k}"' in line and k in readers:
                        readers[k].append(f"{rel}::{en}")
    return readers


class SettingsAuditGuardTest(unittest.TestCase):
    def test_every_default_setting_has_a_code_reader(self):
        keys = list(core.DEFAULT_SETTINGS.keys())
        readers = find_setting_readers(keys)
        unread = [k for k in keys if not readers[k]]
        self.assertEqual(unread, [], f"DEFAULT_SETTINGS keys with no code reader: {unread}")

    def test_normal_settings_short_and_valid(self):
        self.assertLessEqual(len(core.NORMAL_SETTINGS), 15)
        for k in core.NORMAL_SETTINGS:
            self.assertIn(k, core.DEFAULT_SETTINGS)
        with tempfile.TemporaryDirectory() as d:
            conn = core.connect(Path(d) / "q.db")
            try:
                self.assertEqual(core.config_list(conn)["normal"], list(core.NORMAL_SETTINGS))
            finally:
                conn.close()

    def test_removed_setting_in_db_ignored_with_log_and_refused_by_cli(self):
        with tempfile.TemporaryDirectory() as d:
            conn = core.connect(Path(d) / "q.db")
            try:
                with core.tx(conn):
                    conn.execute("INSERT INTO settings(scope,key,value) VALUES ('global','obsolete_test_key','1')")
                core._IGNORED_UNKNOWN_SETTINGS.discard("obsolete_test_key")
                buf = io.StringIO()
                with redirect_stderr(buf):
                    listed = core.config_list(conn)
                self.assertNotIn("obsolete_test_key", [o["key"] for o in listed["overrides"]])
                self.assertIn("obsolete_test_key", buf.getvalue())
                # Second call logs nothing more (one log line per removed key).
                buf2 = io.StringIO()
                with redirect_stderr(buf2):
                    core.config_list(conn)
                self.assertEqual(buf2.getvalue(), "")
                with self.assertRaises(core.RiverError):
                    core.config_set(conn, "obsolete_test_key", "2")
            finally:
                conn.close()


class SettingsVisibilityBrowser(test_view_sessions.TmuxBase):
    drive = test_page.PageBrowser.drive

    def test_normal_tabs_show_only_normal_keys_and_advanced_shows_rest(self):
        norm = list(core.NORMAL_SETTINGS)
        driver = r'''
const errors = window.__pageErrors;
const NORMAL = new Set(EXPECTED_NORMAL);
try {
  await until(() => window.__appReady && q("#sessYouRow .srow") && q("#sessSettings"), "page ready");
  q("#sessSettings").click();
  await until(() => !q("#listSetDlg").classList.contains("hidden") && q('#listSetTabs [data-ls-part="agents"]'), "dialog");

  // 1. Settings dialog: every tab shows ONLY NORMAL keys; Advanced is closed by default
  const dlgParts = [...document.querySelectorAll('#listSetTabs [data-ls-part]')].map(b => b.dataset.lsPart);
  const seenDlg = new Set();
  for (const p of dlgParts) {
    q(`#listSetTabs [data-ls-part="${p}"]`).click();
    await sleep(100);
    for (const r of document.querySelectorAll('#listSetRows [data-set-row]')) {
      const k = r.dataset.setRow;
      check(NORMAL.has(k), `dialog ${p}: non-NORMAL key ${k} shown on normal tab`);
      seenDlg.add(k);
    }
  }
  for (const k of NORMAL) check(seenDlg.has(k), `dialog missing NORMAL key ${k}`);
  check(!q("#listSetAdv").open, "dialog Advanced is closed by default");
  q("#listSetAdv > summary").click();
  await until(() => q("#listSetAdv").open && q("#listSetAdvRows [data-set-row]"), "dialog Advanced opens");
  q("#listSetAdvSearch").value = "lease_ttl";
  q("#listSetAdvSearch").dispatchEvent(new Event("input", { bubbles: true }));
  await until(() => q('#listSetAdvRows [data-set-row="lease_ttl"]')
    && document.querySelectorAll('#listSetAdvRows [data-set-row]').length === 1, "dialog Advanced search filters to lease_ttl");

  // 2. Settings page: every normal tab shows ONLY NORMAL keys; Advanced is closed by default and searchable
  q('#listSetDlg [data-ls-close]').click();
  await until(() => q("#listSetDlg").classList.contains("hidden"), "dialog closed");
  q("#hdrMore").click(); q("#settingsBtn").click();
  await until(() => q('#setTabs [data-set-tab="agents"]'), "settings page");
  const pageTabs = [...document.querySelectorAll('#setTabs [data-set-tab]')].map(b => b.dataset.setTab)
    .filter(k => !["credentials", "policies", "people"].includes(k));
  const seenPage = new Set();
  for (const t of pageTabs) {
    q(`#setTabs [data-set-tab="${t}"]`).click();
    await sleep(100);
    for (const r of document.querySelectorAll('#setRows [data-set-row]')) {
      const k = r.dataset.setRow;
      check(NORMAL.has(k), `page ${t}: non-NORMAL key ${k} shown on normal tab`);
      seenPage.add(k);
    }
  }
  for (const k of NORMAL) check(seenPage.has(k), `page missing NORMAL key ${k}`);
  check(!q("#setAdvanced").open, "page Advanced is closed by default");
  q("#setAdvanced > summary").click();
  await until(() => q("#setAdvanced").open && q("#setAdvRows [data-set-row]"), "page Advanced opens");
  q("#setAdvSearch").value = "ops_storm_count";
  q("#setAdvSearch").dispatchEvent(new Event("input", { bubbles: true }));
  await until(() => q('#setAdvRows [data-set-row="ops_storm_count"]')
    && document.querySelectorAll('#setAdvRows [data-set-row]').length === 1, "page Advanced search filters to ops_storm_count");

  post({ ok: true, errors });
} catch (e) { post({ error: String(e.stack || e) + " errors: " + errors.join(" | ") }); }
'''.replace("EXPECTED_NORMAL", repr(norm))
        got = self.drive(driver)
        self.assertEqual(got.get("error"), None, got)
        self.assertTrue(got.get("ok"), got)
        self.assertEqual(got.get("errors"), [])


if __name__ == "__main__":
    unittest.main()
