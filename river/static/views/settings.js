// The Settings tab (#4656): every key, its default and its overrides, and the form that sets one.
import { $, esc, toast, act } from "../lib.js";
import { chip } from "../components/chip.js";
import { makeTable } from "../components/table.js";
import { fitClamps } from "../components/clamp.js";
import { S } from "../page.js";

// Settings: one row per key; an override chip's × removes that override.
let settingsTable = null;
export function renderSettings() {
  const d = S.settings.defaults, o = S.settings.overrides;
  settingsTable ||= makeTable($("#settingsTable"), { key: "settings", index: "key", sort: [{ column: "key", dir: "asc" }],
    placeholder: "No settings.",
    columns: [
      { title: "Key", field: "key", width: 200 },
      { title: "Default", field: "default", width: 240, cssClass: "muted wrap" },
      { title: "Overrides", field: "overrides", cssClass: "wrap", html: (r) => r.ov.map(x => chip("c-p", `${esc(x.scope)} = ${esc(x.value)} <span class="link" data-unset="${esc(r.key)}" data-scope="${esc(x.scope)}">×</span>`)).join(" ") },
    ] });
  settingsTable.set(Object.keys(d).map(k => {
    const ov = o.filter(x => x.key === k);
    return { key: k, default: String(d[k]), overrides: ov.map(x => `${x.scope}=${x.value}`).join(" "), ov };
  }));
}

// A click on this view's controls; true when it was one (the frame then stops).
export function onClick(t) {
  if (t.dataset.unset) return run(async () => {
    const sc = t.dataset.scope, [kind, name] = sc.includes(":") ? [sc.split(":")[0], sc.slice(sc.indexOf(":") + 1)] : ["global", null];
    const args = { key: t.dataset.unset }; if (kind !== "global") args[kind] = kind === "item" ? +name : name;
    return act("config_unset", args).catch(() => {});
  });
  return false;
}
const run = (f) => { f(); return true; };

$("#setGo").addEventListener("click", async () => {
  const scope = $("#setScope").value, nm = $("#setScopeName").value.trim();
  const args = { key: $("#setKey").value, value: $("#setValue").value.trim() };
  if (scope !== "global") { if (!nm) return toast("Give the project, agent, or item", true); args[scope] = scope === "item" ? +nm : nm; }
  await act("config_set", args).then(() => toast("Saved")).catch(() => {});
});

// The help above the table folds to four lines on a phone (#4889).
fitClamps($("#tab-settings"));
