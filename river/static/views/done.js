// The Done view of Plan (#4656; Plan since #5102 step 4): what was finished in the scope's project, grouped by day,
// and the progress of each project.
import { $, esc, shortTitle } from "../lib.js";
import { projectChip } from "../components/chip.js";
import { bar, pct } from "../components/bar.js";
import { makeTable } from "../components/table.js";
import { nav, scope, planShown } from "../page.js";

let logSig = "", logTable = null;
const logTime = (iso) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const logDay = (d) => new Date(d + "T12:00:00Z").toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
// The Done log: one row per finished item, grouped by day; every column sorts and filters. The output of an item is
// the hover of its title, and the drawer shows it (#5082).
function makeLogTable() {
  return makeTable($("#log"), {
    key: "done", sort: [{ column: "closed_at", dir: "desc" }], placeholder: "Nothing done in this period.",
    onRow: (r) => nav.openDrawer(r.id),
    groupBy: "day", groupHeader: (day, count) => `<b>${esc(logDay(day))}</b> <span class="muted">${count} done</span>`,
    columns: [
      { title: "#", field: "id", width: 64, filter: false },
      { title: "Title", field: "title", minWidth: 180, cssClass: "wrap", html: (r) => `<span title="${esc(r.title + (r.output ? "\n\n" + r.output : ""))}">${esc(shortTitle(r))}</span>` },
      { title: "Project", field: "project", width: 150, filter: "select", html: (r) => projectChip(r.project) },
      { title: "By", field: "by", width: 130, filter: "select" },
      { title: "Finished", field: "closed_at", width: 96, filter: false, html: (r) => logTime(r.closed_at) },
    ],
  });
}
export async function renderLog(force) {
  if (!planShown("done")) return;
  const q = new URLSearchParams({ since: $("#logSince").value });
  if (scope.project) q.set("project", scope.project);
  const r = await fetch("api/log?" + q); if (!r.ok) return;
  const L = await r.json(), sig = JSON.stringify(L);
  if (!force && sig === logSig) return;
  logSig = sig;
  logTable ||= makeLogTable();
  logTable.set(L.by_day.flatMap(d => d.items.map(it => ({ id: it.id, title: it.title, short: it.short, project: it.project, output: it.output || "",
    by: it.by_agent && it.by_agent !== "?" ? it.by_agent : "", closed_at: it.closed_at, day: d.day }))));
  const win = L.since ? $("#logSince").selectedOptions[0].textContent : null;
  $("#progress").innerHTML = L.progress.map(p => {
    const done = pct(p.done, p.total);
    return `<div class="prog"><div class="prog-h"><b>${esc(p.project)}</b><span class="muted"${win ? ` title="${p.done_in_window} done in the ${esc(win)}"` : ""}>${win && p.done_in_window ? `+${p.done_in_window} · ` : ""}${p.done}/${p.total}</span></div>
      ${bar(done)}</div>`;
  }).join("") || `<div class="muted">No items.</div>`;
}
$("#logSince").addEventListener("change", () => renderLog(true));
