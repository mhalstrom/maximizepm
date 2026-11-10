"""Every modal of the page closes on a click outside it and on Escape (#5027): the dialogs through one component
(components/dialog.js makeDialog), and the item drawer. A press that starts inside and ends outside does not close
it; typed text asks once first."""
import unittest

from river import core


class Modals(unittest.TestCase):
    def setUp(self):
        from tests import test_view_sessions as tvs
        self.tvs = tvs
        self.base = tvs.Base("run")
        self.base.setUp()
        self.addCleanup(self.base.tearDown)

    def test_dialogs_and_the_drawer_close_outside_and_on_escape(self):
        item = self.base.it
        driver = self.tvs.DRIVE_HEAD + r'''
try {
  await until(() => /updated/.test(q("#stamp").textContent), "the page");
  await sleep(300);
  const out = {};
  const press = (down, up) => {
    down.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
    up.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  };
  const esc = () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  const shown = (el) => !el.classList.contains("hidden");
  // Each dialog of the page: an outside click closes it, and Escape closes it.
  out.dialogs = {};
  for (const id of ["setup", "readyDlg", "waitDlg", "setDlg", "searchDlg"]) {
    const d = q("#" + id);
    d.classList.remove("hidden");
    press(d, d);
    const outside = !shown(d);
    d.classList.remove("hidden");
    esc();
    out.dialogs[id] = [outside, !shown(d)];
    d.classList.add("hidden");
  }
  // The shared component: a press that starts in the box and ends outside keeps it; typed text asks once.
  const { makeDialog } = await import("/components/dialog.js");
  const d = document.createElement("div");
  d.className = "hidden"; d.setAttribute("role", "dialog");
  d.innerHTML = '<div class="box"><textarea></textarea></div>';
  document.body.append(d);
  const dlg = makeDialog(d);
  dlg.open();
  press(d.querySelector("textarea"), d);
  out.selection = shown(d);
  const ta = d.querySelector("textarea");
  ta.value = "a draft"; ta.dispatchEvent(new Event("input", { bubbles: true }));
  press(d, d);
  out.asked = [shown(d), (d.querySelector(".dlg-draft") || {}).textContent || ""];
  esc();
  out.second = shown(d);
  dlg.open();
  out.noteGone = !d.querySelector(".dlg-draft");
  press(d, d);
  out.emptyCloses = !shown(d);
  d.remove();
  // The item drawer: a click on empty space outside closes it; a click on a control does not.
  window.riverOpen(''' + str(item) + r''');
  await until(() => q("#drawer").classList.contains("open"), "the drawer");
  await sleep(300);
  const btn = [...document.querySelectorAll("button")].find(b => b.offsetParent && !q("#drawer").contains(b));
  if (btn) { btn.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true })); }
  out.controlKeeps = q("#drawer").classList.contains("open");
  const empty = q("#stamp");
  out.why = { draft: [...q("#drawer").querySelectorAll("input, textarea, select")].filter(f => f.tagName === "SELECT"
      ? f.selectedIndex !== Math.max(0, [...f.options].findIndex(o => o.defaultSelected)) : f.value !== f.defaultValue).map(f => f.outerHTML.slice(0, 80)),
    in: empty.closest('a, button, input, select, textarea, label, summary, option, [data-open], [role="button"], [role="tab"], [role="dialog"], [contenteditable], [tabindex], #itemPop, #toast, iframe, .tabulator-row') ? "control" : "" };
  press(empty, empty);
  out.drawerCloses = !q("#drawer").classList.contains("open");
  // A field of the item that the person changed: the first outside click asks, the second closes it.
  window.riverOpen(''' + str(item) + r''');
  await until(() => q("#drawer").classList.contains("open") && q("#drawer textarea"), "the drawer again");
  await sleep(300);
  q("#drawer textarea").value = "a change";
  press(empty, empty);
  out.draftKeeps = q("#drawer").classList.contains("open");
  press(empty, empty);
  out.draftCloses = !q("#drawer").classList.contains("open");
  post(out);
} catch (e) { post({ error: String(e.stack || e) }); }
'''
        got = self.tvs.drive_page(self.base, driver)
        self.assertNotIn("error", got, got)
        for id_, (outside, escape) in got["dialogs"].items():
            self.assertTrue(outside, f"{id_} closes on a click outside")
            self.assertTrue(escape, f"{id_} closes on Escape")
        self.assertTrue(got["selection"], "a press that starts in the box and ends outside keeps it open")
        self.assertTrue(got["asked"][0], "typed text: the first outside click asks")
        self.assertIn("You typed text here", got["asked"][1])
        self.assertFalse(got["second"], "the second one closes it")
        self.assertTrue(got["noteGone"])
        self.assertTrue(got["emptyCloses"])
        self.assertTrue(got["controlKeeps"])
        self.assertTrue(got["drawerCloses"], got.get("why"))
        self.assertTrue(got["draftKeeps"], "a changed field of the item: the first outside click asks")
        self.assertTrue(got["draftCloses"])


if __name__ == "__main__":
    unittest.main()
