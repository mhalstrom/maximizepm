// The Health section of the Cost page (#5356 made it a view of Plan; #6217, mark 2026-11-19: 'this page should more be
// in the costs page that you get to in the bottom left. it also needs to be reworded and should not be so wordy').
// GET api/health, the numbers of maxpm health for the Cost page's period, against the time before: six tiles with
// short labels, a small bar graph per measure by day (UTC), and goal against no goal as a table. How each number is
// measured is in the hover of its label, never a paragraph. One series per graph: one color, no legend; each day's bar
// has its value as a hover. views/cost.js calls loadHealth after it draws.
import { esc } from "../lib.js";

// The Cost page's period as a time of maxpm health: today is the last day.
const SINCE = { today: "1d", "7d": "7d", "30d": "30d", all: "all" };
let cache = { key: "", at: 0, data: null };

const num = (v, d = 1) => v == null ? "–" : (+v).toFixed(d);
const pc = (v, d = 1) => v == null ? "–" : (v * 100).toFixed(d) + "%";
const kt = (v) => v == null ? "–" : v >= 1e6 ? (v / 1e6).toFixed(1) + "M" : v >= 1e3 ? Math.round(v / 1e3) + "k" : Math.round(v) + "";

function tile(label, how, val, before, main) {
  return `<div class="h-tile${main ? " h-main" : ""}" title="${esc(how)}"><span class="h-val">${esc(val)}</span>`
    + `<span class="h-label">${esc(label)}</span>${before == null ? "" : `<span class="h-before">was ${esc(before)}</span>`}</div>`;
}

// One bar per day; the hit area of a day is its whole column, so a short bar is easy to hover.
function graph(title, days, field, fmt) {
  const W = 100 * days.length, H = 100;
  const vals = days.map(d => d[field]);
  const max = Math.max(0, ...vals.filter(v => v != null));
  const bars = days.map((d, i) => {
    const v = d[field], h = v == null || !max ? 0 : Math.max(2, v / max * (H - 4));
    const tip = `${d.day}: ${v == null ? "no data" : fmt(v)}`;
    return `<g><rect class="h-hit" x="${i * 100}" y="0" width="100" height="${H}"><title>${esc(tip)}</title></rect>`
      + (h ? `<rect class="h-bar" x="${i * 100 + 12}" y="${H - h}" width="76" height="${h}" rx="6"><title>${esc(tip)}</title></rect>` : "") + `</g>`;
  }).join("");
  return `<div class="h-graph"><h4 title="${esc(title)}">${esc(title)}</h4>`
    + `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${esc(title)} daily, peak ${esc(fmt(max))}">`
    + `${bars}<line class="h-base" x1="0" x2="${W}" y1="${H}" y2="${H}"/></svg>`
    + `<div class="h-axis"><span>${esc(days[0].day.slice(5))}</span><span>max ${esc(fmt(max))}</span><span>${esc(days[days.length - 1].day.slice(5))}</span></div></div>`;
}

function goalTable(g) {
  if (!g || !g.projects.length) return `<div class="muted t-m">No project has tasks done with a goal and without one.</div>`;
  const row = (label, x) => `<tr${x.small ? ` class="h-small" title="Under ${g.min_group} tasks"` : ""}><td>${esc(label)}</td><td>${x.done}</td>`
    + `<td>${num(x.median_minutes, 0)}</td><td>${pc(x.rework_rate)}</td><td>${num(x.person_load, 0)}</td><td>${kt(x.median_eq)}</td></tr>`;
  const rows = [row("All: goal", g.total.goal), row("All: no goal", g.total.none)];
  for (const p of g.projects) rows.push(`<tr class="h-proj"><td colspan="6">${esc(p.project)}</td></tr>`, row("goal", p.goal), row("no goal", p.none));
  const th = (t, how) => `<th title="${esc(how)}">${t}</th>`;
  return `<div class="h-wrap"><table class="h-table"><thead><tr>`
    + th("Group", "Agent tasks of the projects that have both. In All, each project weighs as its smaller group; a goal tag counts as it is now")
    + th("Done", "Tasks done") + th("Minutes", "Median minutes from claim to done") + th("Redone", "Reopens, lost claims and review fixes per task done")
    + th("Your load", "Needs-you rows per 100 tasks") + th("Cost", "Median input-token equivalents per task done")
    + `</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
}

export function healthHtml(H, period) {
  const n = H.now, b = period === "all" ? null : H.before;
  const idle = (x) => x.sessions && x.sessions.total_secs ? x.sessions.idle : null;
  // A value with its unit, or "–" alone; the time before only when it has a value.
  const unit = (v, f, u) => v == null ? "–" : f(v) + u;
  const was = (pick, f) => b && pick(b) != null ? f(pick(b)) : null;
  const minutes = (v) => unit(v, x => num(x, 2), " min"), hours = (v) => unit(v, num, "h");
  const tiles = [
    tile("Your time per task", `Minutes of your time per task done: ${H.person_minutes} min for each needs-you row (health_person_minutes)`,
         minutes(n.person_minutes_per_item), was(x => x.person_minutes_per_item, minutes), true),
    tile("Tasks done a day", `${n.done} tasks done`, num(n.per_day), was(x => x.per_day, num)),
    tile("Redone", `${n.reopens} reopens, ${n.lost_claims} lost claims and ${n.review_fixes} review fixes in ${n.done} tasks done`,
         pc(n.rework_rate), was(x => x.rework_rate, pc)),
    tile("Wait for you", "Mean wait of a needs-you row", hours(n.wait_mean_h), was(x => x.wait_mean_h, hours)),
    tile("Idle sessions", "Share of session time waiting for work", pc(idle(n), 0), was(idle, v => pc(v, 0))),
    tile("Cost per task", `Median input-token equivalents per task done; ${n.cost_measured} of ${n.done} measured`,
         kt(n.cost_median_eq), was(x => x.cost_median_eq, kt)),
  ].join("");
  const graphs = H.days && H.days.length > 1 ? `<div class="h-graphs">${[
    graph("Your time per task", H.days, "person_minutes_per_item", v => num(v, 2)),
    graph("Tasks done", H.days, "done", v => num(v, 0)),
    graph("Redone", H.days, "rework_rate", v => pc(v)),
    graph("Idle sessions", H.days, "idle", v => pc(v, 0)),
    graph("Cost per task", H.days, "median_eq", kt),
  ].join("")}</div>` : "";
  return `<div class="h-tiles">${tiles}</div>${graphs}<h4 class="h-sub">Goal against no goal</h4>${goalTable(H.by_goal)}`;
}

// Fill box with the Health section for the Cost page's period. A read is kept a minute: a 30-day read takes a second.
export async function loadHealth(box, period) {
  if (!box) return;
  const since = SINCE[period] || "7d";
  box.dataset.since = since;  // a read of an older period that ends later draws nothing
  if (cache.key === since && Date.now() - cache.at < 60e3 && cache.data) { box.innerHTML = healthHtml(cache.data, period); return; }
  if (cache.key !== since) box.innerHTML = `<div class="muted t-m">Reading…</div>`;
  try {
    const r = await fetch("api/health?" + new URLSearchParams({ since }));
    if (!r.ok) throw new Error((await r.json()).error || "failed");
    const data = await r.json();
    cache = { key: since, at: Date.now(), data };
    if (box.isConnected && box.dataset.since === since) box.innerHTML = healthHtml(data, period);
  } catch (e) {
    if (box.isConnected && box.dataset.since === since) box.innerHTML = `<div class="muted t-m">Health could not be read: ${esc(e.message)}</div>`;
  }
}
