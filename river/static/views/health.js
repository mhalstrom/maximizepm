// The Health view of Plan (#5356, decision 2 of the canvas does-maxpm-work): GET api/health, the numbers of maxpm
// health. Tiles with the headline (person minutes per done item, a proxy) and the measures beside it, each with the
// value of the time before; a small bar graph per measure by day (UTC), the change log as dashed lines; the goal
// comparison as a table. One series per graph: one color, no legend; each day's bar has its value as a hover.
import { $, esc } from "../lib.js";
import { scope, planShown } from "../page.js";

let sig = "", fetched = 0, key = "";
const num = (v, d = 1) => v == null ? "–" : (+v).toFixed(d);
const pc = (v, d = 1) => v == null ? "–" : (v * 100).toFixed(d) + "%";
const kt = (v) => v == null ? "–" : v >= 1e6 ? (v / 1e6).toFixed(1) + "M" : v >= 1e3 ? Math.round(v / 1e3) + "k" : Math.round(v) + "";

function tile(label, val, before, main) {
  return `<div class="h-tile${main ? " h-main" : ""}"><span class="h-val">${esc(val)}</span><span class="h-label">${esc(label)}</span>`
    + `<span class="h-before">before ${esc(before)}</span></div>`;
}

// One bar per day; the hit area of a day is its whole column, so a short bar is easy to hover.
function graph(title, days, field, fmt, changes) {
  const W = 100 * days.length, H = 100;
  const vals = days.map(d => d[field]);
  const max = Math.max(0, ...vals.filter(v => v != null));
  const last = [...vals].reverse().find(v => v != null);
  const bars = days.map((d, i) => {
    const v = d[field], h = v == null || !max ? 0 : Math.max(2, v / max * (H - 4));
    const tip = `${d.day}: ${v == null ? "no data" : fmt(v)}`;
    return `<g><rect class="h-hit" x="${i * 100}" y="0" width="100" height="${H}"><title>${esc(tip)}</title></rect>`
      + (h ? `<rect class="h-bar" x="${i * 100 + 12}" y="${H - h}" width="76" height="${h}" rx="6"><title>${esc(tip)}</title></rect>` : "") + `</g>`;
  }).join("");
  const t0 = Date.parse(days[0].day + "T00:00:00Z");
  const marks = changes.map(c => {
    const x = (Date.parse(c.at) - t0) / 864e5 * 100;
    return x >= 0 && x <= W ? `<line class="h-change" x1="${x}" x2="${x}" y1="0" y2="${H}"><title>${esc(c.at.slice(0, 10) + " " + c.name)}</title></line>` : "";
  }).join("");
  return `<div class="h-graph"><h3 title="${esc(title)}">${esc(title)}</h3>`
    + `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${esc(title)} by day, highest ${esc(fmt(max))}">`
    + `${bars}<line class="h-base" x1="0" x2="${W}" y1="${H}" y2="${H}"/>${marks}</svg>`
    + `<div class="h-axis"><span>${esc(days[0].day.slice(5))}</span><span>max ${esc(fmt(max))}${last == null ? "" : esc(" · last " + fmt(last))}</span><span>${esc(days[days.length - 1].day.slice(5))}</span></div></div>`;
}

function goalTable(g) {
  if (!g || !g.projects.length) return `<div class="muted">No project has done items with a goal and without one in this time.</div>`;
  const row = (label, x) => `<tr${x.small ? ` class="h-small" title="Fewer than ${g.min_group} items: too few to read"` : ""}><td>${esc(label)}</td><td>${x.done}</td>`
    + `<td>${num(x.median_minutes, 0)}</td><td>${pc(x.rework_rate)}</td><td>${num(x.person_load, 0)}</td><td>${kt(x.median_eq)}</td></tr>`;
  const rows = [row("All: goal", g.total.goal), row("All: no goal", g.total.none)];
  for (const p of g.projects) rows.push(`<tr class="h-proj"><td colspan="6">${esc(p.project)}</td></tr>`, row("goal", p.goal), row("no goal", p.none));
  return `<div class="h-wrap"><table class="h-table"><thead><tr><th>Group</th><th>Done</th><th>Min claim to done</th><th>Rework</th>`
    + `<th>Person load</th><th>Cost (eq)</th></tr></thead><tbody>${rows.join("")}</tbody></table></div>`
    + `<div class="muted t-s">Agent task items only, in the projects that have both. In "All" each project weighs as its smaller group. A tag counts as it is now. Person load: needs-you rows per 100 items.</div>`;
}

export async function renderHealth(force) {
  if (!planShown("health")) return;
  const since = $("#healthSince").value, k = since + "|" + (scope.project || "");
  // The numbers change slowly and a 30-day read takes a second: read again after a minute, or when the scope changes.
  if (!force && k === key && Date.now() - fetched < 60e3) return;
  const q = new URLSearchParams({ since });
  if (scope.project) q.set("project", scope.project);
  key = k; fetched = Date.now();
  const r = await fetch("api/health?" + q); if (!r.ok) return;
  const H = await r.json(), s = JSON.stringify(H);
  if (s === sig) return;
  sig = s;
  const n = H.now, b = H.before, idle = (x) => x.sessions && x.sessions.total_secs ? x.sessions.idle : null;
  $("#healthTiles").innerHTML = [
    tile("person minutes per done item", num(n.person_minutes_per_item, 2), num(b.person_minutes_per_item, 2), true),
    tile("items done per day", num(n.per_day), num(b.per_day)),
    tile("rework rate", pc(n.rework_rate), pc(b.rework_rate)),
    tile("mean wait of a needs-you row", num(n.wait_mean_h) + "h", num(b.wait_mean_h) + "h"),
    tile("session time waiting for work", pc(idle(n), 0), pc(idle(b), 0)),
    tile("median cost per done item (eq)", kt(n.cost_median_eq), kt(b.cost_median_eq)),
  ].join("");
  $("#healthNote").textContent = `Person minutes are a proxy: ${H.person_minutes} minutes for each needs-you row (setting health_person_minutes). `
    + `Rework: ${n.reopens} reopens, ${n.lost_claims} lost claims, ${n.review_fixes} review fix items in ${n.done} done items. `
    + `Cost: ${n.cost_measured} of ${n.done} items measured.`;
  const ch = H.changes || [];
  $("#healthGraphs").innerHTML = [
    graph("Person min per item", H.days, "person_minutes_per_item", v => num(v, 2), ch),
    graph("Items done", H.days, "done", v => num(v, 0), ch),
    graph("Rework rate", H.days, "rework_rate", v => pc(v), ch),
    graph("Waiting for work", H.days, "idle", v => pc(v, 0), ch),
    graph("Median cost (eq)", H.days, "median_eq", kt, ch),
  ].join("");
  $("#healthChangesBox").classList.toggle("hidden", !ch.length);
  $("#healthChanges").innerHTML = ch.map(c => {
    const x = c.before || {}, y = c.after || {};
    return `<div class="h-change-row"><b>${esc(c.at.slice(0, 10))} ${esc(c.name)}</b>: person min/item ${num(x.person_minutes_per_item, 2)} → ${num(y.person_minutes_per_item, 2)}, `
      + `done/day ${num(x.per_day)} → ${num(y.per_day)}, rework ${pc(x.rework_rate)} → ${pc(y.rework_rate)}</div>`;
  }).join("") + (ch.length ? `<div class="muted t-s">Weak proof: other things change at the same time. A person or the manager adds a change: maxpm change add "&lt;name&gt;" --at &lt;date&gt;</div>` : "");
  $("#healthGoal").innerHTML = goalTable(H.by_goal);
}
$("#healthSince").addEventListener("change", () => renderHealth(true));
