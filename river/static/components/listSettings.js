// The settings of the Sessions list (#5883, mark 2026-11-14: 'maybe we move the settings for all of these into a
// settings modal acessible from that button in the bottom left'): the ⚙ at the foot of the list opens one dialog with
// a tab for each of its parts: Sessions, Agents, Browsers (the browser and canvas keys: canvases are artifacts), and
// Cost. Each row is the row of the Settings tab (views/settings.js settingRow): a value for every project,
// saved on Enter or when the field loses focus. All settings opens the Settings tab on the same group.
import { $, esc, store } from "../lib.js";
import { makeDialog } from "./dialog.js";
import { button, iconButton } from "./button.js";
import { S, nav } from "../page.js";
import { settingRow, groupOf } from "../views/settings.js";
import { labelOf, programOf } from "../views/settingLabels.js";
import { wireSettingControls } from "./settingControl.js";

// [tab key, name, the test of a key, the group of the Settings tab that holds the rest]
const PARTS = [
  ["sessions", "Sessions", (k) => groupOf(k) === "sessions", "sessions"],
  ["agents", "Agents", (k) => groupOf(k) === "agents" && !programOf(k), "agents"],
  ["browsers", "Browsers", (k) => groupOf(k) === "browser", "browser"],
  ["cost", "Cost", (k) => k === "cost_limit", "work"],
];
const KEY = "river.listSettings";
let dlg = null, part = PARTS.some(p => p[0] === store(KEY)) ? store(KEY) : "sessions";

function el() {
  let d = $("#listSetDlg");
  if (d) return d;
  d = document.createElement("div");
  d.id = "listSetDlg"; d.className = "hidden";
  d.setAttribute("role", "dialog"); d.setAttribute("aria-labelledby", "listSetTitle");
  d.innerHTML = `<div class="box">
    <div class="dlg-head"><h2 id="listSetTitle">Settings</h2>${iconButton("✕", "Close", { data: { "ls-close": 1 } })}</div>
    <div class="set-tabs" id="listSetTabs" role="tablist" aria-label="Settings of"></div>
    <div class="ls-rows" id="listSetRows"></div>
    <div class="dlg-foot">${button("All settings", { data: { "ls-all": 1 }, title: "The Settings tab" })}
      ${button("Done", { kind: "primary", data: { "ls-close": 1 } })}</div></div>`;
  document.body.appendChild(d);
  dlg = makeDialog(d, { draft: false });
  d.addEventListener("click", (e) => {
    if (e.target.closest("[data-ls-close]")) return dlg.close();
    if (e.target.closest("[data-unset]")) { setTimeout(() => draw(true), 400); return; }  // ↺ / ✕ (settings.js onClick unsets it)
    const t = e.target.closest("[data-ls-part]");
    if (t) { part = t.dataset.lsPart; store(KEY, part); return draw(true); }
    if (e.target.closest("[data-ls-all]")) {
      store("river.settingsTab", PARTS.find(p => p[0] === part)[3]);
      dlg.close();
      nav.setTab("settings");
    }
  });
  wireSettingControls($("#listSetRows"), () => S, labelOf, () => draw(true));
  return d;
}

function draw(force = false) {
  if (!S || !S.settings) return;
  $("#listSetTabs").innerHTML = PARTS.map(([k, name]) => button(esc(name), { cls: "set-tab", pressed: part === k,
    data: { "ls-part": k }, attrs: `role="tab" aria-selected="${part === k}"` })).join("");
  const d = S.settings.defaults, o = S.settings.overrides, now = S.settings.now || {};
  const test = PARTS.find(p => p[0] === part)[2];
  const rows = Object.keys(d).filter(test).map(k => settingRow(k, d, o.filter(x => x.key === k), now)).join("");
  const box = $("#listSetRows");
  const focused = document.activeElement;
  const typing = !force && box.contains(focused) && (focused.tagName === "INPUT" || focused.tagName === "SELECT");
  if (!typing) {
    box.innerHTML = rows || `<div class="muted">No settings here.</div>`;
  }
}

export function openListSettings(tab) {
  if (tab && PARTS.some(p => p[0] === tab)) { part = tab; store(KEY, part); }
  el();
  draw(true);
  dlg.open();
}
