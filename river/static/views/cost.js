// The Cost view (#5857, mark 2026-11-14, research theme 4: parallel agents use the plan limits fast, and most tools show
// no cost). A period (Today, 7 days, 30 days, All); the total in dollars, tokens and model calls; the plan windows an
// agent writes to its log (Codex), or where to look (Claude Code: /usage in its terminal); each cost limit the person
// set, with its use (none by default); and one table at a time: Sessions, Items or Projects, the most expensive first,
// each with its share of the total and a CSV file. A session opens its chat; an item opens in the drawer.
// Server: GET api/cost?period=<p> (river/cost.py); api/cost?period=<p>&csv=<table> for the file.
import { $, esc, store, getJson } from "../lib.js";
import { button } from "../components/button.js";
import { bar } from "../components/bar.js";
import { empty } from "../components/empty.js";
import { nav } from "../page.js";

const PERIOD = "river.cost.period", TABLE = "river.cost.table";
const PERIODS = [["today", "Today"], ["7d", "7 days"], ["30d", "30 days"], ["all", "All"]];
const TABLES = [["sessions", "Sessions"], ["items", "Items"], ["projects", "Projects"]];
let period = store(PERIOD) || "today", table = store(TABLE) || "sessions", data = null, wired = false, reading = 0;

const usd = (n) => n == null ? "–" : n >= 100 ? "$" + Math.round(n).toLocaleString() : "$" + n.toFixed(2);
const tok = (n) => n >= 1e9 ? (n / 1e9).toFixed(1) + "B" : n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? Math.round(n / 1e3) + "k" : String(n);
const when = (iso) => { try { return new Date(iso).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" }); } catch (e) { return iso; } };

export async function showCost() {
  const box = $("#costView");
  if (!wired) { wire(box); wired = true; }
  const mine = ++reading;
  if (!data || data.period !== period) box.innerHTML = head() + `<div class="muted t-m cost-note">Reading the chat logs of the sessions… A long period takes a few seconds the first time.</div>`;
  try {
    const got = await getJson("api/cost?period=" + period);
    if (mine !== reading) return;
    data = got;
  } catch (e) {
    if (mine === reading) box.innerHTML = head() + `<div class="muted cost-note">${esc(e.message)}</div>`;
    return;
  }
  draw(box);
  const sub = document.querySelector("#sessCostRow .sub");
  if (sub && data.period === "today") sub.textContent = `Today ${usd(data.total.usd)}`;
}

function head() {
  return `<div class="cost-bar" role="group" aria-label="Period">${PERIODS.map(([k, t]) =>
    button(t, { cls: "cost-tab", pressed: period === k, data: { "cost-period": k } })).join("")}</div>`;
}

function plans(p) {
  const rows = (p.windows || []).map(w => w.used_percent != null
    ? `<div class="cost-meter"><div class="cost-mh"><span>${esc(w.agent)}: ${esc(w.name)}</span><span class="spacer"></span><b>${Math.round(w.used_percent)}%</b></div>
       ${bar(Math.min(100, w.used_percent))}${w.resets_at ? `<div class="muted t-s">Starts again ${esc(when(w.resets_at))}</div>` : ""}</div>`
    : `<div class="t-m">${esc(w.agent)}: ${esc(w.name)} ${esc(w.text || "")}</div>`).join("");
  const silent = (p.silent || []).length
    ? `<div class="muted t-m">${p.silent.map(esc).join(", ")} ${p.silent.length === 1 ? "writes" : "write"} no plan use to the log: type /usage in its terminal to see it.</div>` : "";
  return rows || silent ? `<section class="cost-sec"><h3 class="cost-h">Plan</h3>${rows}${silent}</section>` : "";
}

function limits(list) {
  if (!list.length) return `<div class="muted t-m cost-note">No cost limit. To get an alert at 80% and 100% of an amount, set cost_limit in Settings, Work (for example $40/day).</div>`;
  return `<section class="cost-sec"><h3 class="cost-h">Limits</h3>${list.map(l => {
    const used = l.kind === "usd" ? usd(l.used) : tok(l.used);
    return `<div class="cost-meter"><div class="cost-mh"><span>${l.project ? esc(l.project) : "All projects"}: ${used} of ${esc(l.text)}</span><span class="spacer"></span>
      <b class="${(l.percent || 0) >= 100 ? "cost-over" : ""}">${Math.round(l.percent || 0)}%</b></div>${bar(Math.min(100, l.percent || 0))}</div>`;
  }).join("")}<div class="muted t-s">An alert comes at 80% and at 100%. Nothing stops by itself.</div></section>`;
}

function rowsHtml(d) {
  const rows = d[table] || [];
  if (!rows.length) return empty(`No model calls ${period === "today" ? "today" : "in this period"}.`);
  const total = d.total.usd || 0;
  const name = (r) => table === "sessions"
    ? `<span class="link" data-cost-session="${esc(r.agent)}" title="Open its chat">${esc(r.agent)}</span>${r.state && !["active", "away"].includes(r.state) ? ` <span class="muted t-s">${esc(r.state)}</span>` : ""}`
    : table === "items" ? `<span class="link" data-cost-item="${r.id}" title="${esc(r.title)}">#${r.id}</span> ${esc(r.title)} <span class="muted t-s">${esc(r.project || "")}</span>`
    : esc(r.project || "no project");
  const extra = (r) => table === "sessions" ? `<td class="cost-model muted t-s">${esc((r.model || "").replace(/^claude-/, ""))}</td>` : "";
  return `<div class="cost-scroll"><table class="cost-t"><thead><tr><th>${TABLES.find(t => t[0] === table)[1].replace(/s$/, "")}</th>${table === "sessions" ? `<th class="cost-model">Model</th>` : ""}
    <th class="num">Calls</th><th class="num">Tokens</th><th class="num">Cost</th><th class="cost-share">Share</th></tr></thead><tbody>${rows.map(r =>
    `<tr><td class="cost-name">${name(r)}</td>${extra(r)}<td class="num">${r.calls.toLocaleString()}</td><td class="num" title="${tok(r.eq)} input-token equivalents">${tok(r.tokens)}</td>
      <td class="num"><b>${usd(r.usd)}</b>${r.unpriced ? `<span class="muted t-s" title="${r.unpriced} calls with no price"> +?</span>` : ""}</td>
      <td class="cost-share">${bar(total ? Math.round(100 * (r.usd || 0) / total) : 0)}</td></tr>`).join("")}</tbody></table></div>`;
}

function draw(box) {
  const d = data, t = d.total;
  box.innerHTML = head() + `
    <div class="cost-sum"><span class="cost-big">${usd(t.usd)}</span>
      <span class="muted t-m">${tok(t.tokens)} tokens · ${tok(t.eq)} input-token equivalents · ${t.calls.toLocaleString()} model calls</span></div>
    ${d.output_priced ? "" : `<div class="muted t-s">A model's price has no output price (model_prices): its output is not in the dollars.</div>`}
    ${t.unpriced ? `<div class="muted t-s">${t.unpriced.toLocaleString()} model calls of a model with no price: their tokens count, not their cost.</div>` : ""}
    ${plans(d.plans || {})}
    ${limits(d.limits || [])}
    <section class="cost-sec"><div class="cost-th"><span class="cost-bar" role="group" aria-label="Table">${TABLES.map(([k, l]) =>
      button(l, { cls: "cost-tab", pressed: table === k, data: { "cost-table": k } })).join("")}</span><span class="spacer"></span>
      <a class="link t-m" href="api/cost?period=${encodeURIComponent(period)}&csv=${table}" download title="This table as a CSV file">CSV</a></div>
      ${rowsHtml(d)}</section>
    <div class="muted t-s cost-note">From the chat logs of the sessions on this computer, priced by model_prices (Settings, Agents). A model call counts for the item its session held then.</div>`;
}

function wire(box) {
  box.addEventListener("click", (e) => {
    const p = e.target.closest("[data-cost-period]");
    if (p) { period = p.dataset.costPeriod; store(PERIOD, period); return showCost(); }
    const t = e.target.closest("[data-cost-table]");
    if (t) { table = t.dataset.costTable; store(TABLE, table); return draw(box); }
    const s = e.target.closest("[data-cost-session]");
    if (s) return nav.openSession(s.dataset.costSession);
    const i = e.target.closest("[data-cost-item]");
    if (i) return nav.openDrawer(+i.dataset.costItem);
  });
}
