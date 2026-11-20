// The Deploy tab (#6136; canvas release-tracking Decision 6, mark 2026-11-18: "in the top we have sessions, Plan,
// Deploy, Terminal tabs"; canvas deploy-tab-mockup pictures 1, 4 and 13). Every pipeline, and every deploy target as
// its default pipeline, flows left to right (mark: "I want left to right view").
//   Parts of Deploy (buttons at the top): Pipelines (one row for each pipeline: its stages as bars, the transitions between
//   them, what runs where, where its runs come from; above them what waits for the person's yes and the tokens that
//   fail), Agents, Runners, Where each version runs, Accounts, Audit log.
//   A pipeline's own page: Stages and Versions (views/pipeline.js, with the release card of each target it names,
//   views/targets.js), Triggers (views/triggers.js, #6150), Emergency and Audit log (mark 2026-11-18: emergency
//   actions and triggers are on the pipeline's own page).
// The red number on the tab counts what waits on the person here: a version that waits for a person's yes, a release that waits
// for the person's yes, and each token that is refused or not saved. Reads: GET api/deploy (each pipeline in one
// read, river/routes/pipeline.py) and api/pipeline-audit (views/audit.js). The address: /deploy, /deploy/<part>,
// /deploy/pipeline/<name>[/<tab>] (app.js writes it from D).
import { $, esc, getJson, ago, left, act, toast } from "../lib.js";
import { button } from "../components/button.js";
import { chip } from "../components/chip.js";
import { empty } from "../components/empty.js";
import { tab, nav } from "../page.js";
import { showPipeline, hidePipeline, renderPipeline, releaseButton } from "./pipeline.js";
import { showTargets } from "./targets.js";
import { drawAudit } from "./audit.js";
import { accountsHtml as accountsView } from "./accounts.js";
import { agentsHtml, stageMarks } from "./agents.js";
import { drawTriggers, defaultTriggersHtml } from "./triggers.js";
import { drawEmergency } from "./emergency.js";
import { definitionHead, openVersions } from "./defversions.js";
import { runnersHtml, runnerClick } from "./runners.js";

export const PARTS = [["pipelines", "Releases"], ["agents", "Agents"], ["runners", "Runners"],
  ["where", "Live"], ["accounts", "Accounts"], ["audit", "Audit"]];
export const PAGE_TABS = [["stages", "Stages"], ["versions", "Versions"], ["triggers", "Triggers"],
  ["emergency", "Emergency"], ["audit", "Audit"]];
// A deploy target's page (#6486): its first screen is the release (views/release.js); More opens these, and the tab
// row shows only while one of them is open.
export const TARGET_TABS = [["stages", "Release"], ["versions", "History"], ["controls", "Controls"], ["audit", "Audit"]];

// What Deploy shows: a part, or the page of one pipeline and its tab. The address sets it (fromUrl), and a click.
export const D = { part: "pipelines", pipeline: null, tab: "stages" };
let data = null, readAt = 0, sig = "", pageSig = "";
const BADGE_EVERY = 30000;  // while Deploy is not on screen, the red number reads again at most this often

const short = (rev) => rev && /^[0-9a-f]{12,}$/.test(rev) ? rev.slice(0, 7) : rev || "";
const pipe = (name) => data && data.pipelines.find(p => p.name === name);
const plural = (n, one, many = one + "s") => `${n} ${n === 1 ? one : many}`;

// ---- the address

// The part of the address after /deploy: [] or [<part>] or ["pipeline", <name>, <tab>].
export function fromUrl(parts) {
  if (parts[0] === "pipeline" && parts[1]) {
    return { part: null, pipeline: parts[1], tab: [...PAGE_TABS, ...TARGET_TABS].some(([k]) => k === parts[2]) ? parts[2] : "stages" };
  }
  return { part: PARTS.some(([k]) => k === parts[0]) ? parts[0] : "pipelines", pipeline: null, tab: "stages" };
}
export function setDeploy(r) { Object.assign(D, r); }
export function urlPath() {
  if (D.pipeline) return "/pipeline/" + encodeURIComponent(D.pipeline) + (D.tab === "stages" ? "" : "/" + D.tab);
  return D.part && D.part !== "pipelines" ? "/" + D.part : "";
}
function go(r) { setDeploy(r); nav.deployGo(); }

// A target's release page.
export function openTarget(name) {
  go({ part: null, pipeline: name, tab: "stages" });
}

// ---- reading and drawing

export async function renderDeploy(force) {
  const on = tab === "deploy";
  if (!on) { hidePipeline(); if (!force && Date.now() - readAt < BADGE_EVERY) return; }
  try { data = await getJson("api/deploy"); readAt = Date.now(); }
  catch (e) { if (on) $("#dpList").innerHTML = empty(esc(e.message)); return; }
  drawBadge();
  if (tab !== "deploy") return;
  const alias = data.aliases?.[D.pipeline];
  if (typeof alias === "string") {
    D.pipeline = alias;
    nav.deployReplaceUrl();
  }
  drawParts();
  for (const id of ["list", "pipeline", "part"]) $("#deploy-" + id).classList.toggle("hidden", id !== view());
  if (D.pipeline) return drawPage(force);
  showTargets([]); hidePipeline();
  if (D.part === "pipelines") return drawList(force);
  return drawPart(force);
}
const view = () => D.pipeline ? "pipeline" : D.part === "pipelines" ? "list" : "part";

function drawBadge() {
  const b = $("#deployBadge"), n = data ? data.count : 0;
  b.textContent = n ? String(n) : "";
  b.classList.toggle("hidden", !n);
  b.title = n ? `${n} waiting` : "";
}

function drawParts() {
  const html = PARTS.map(([k, name]) => button(esc(name), { cls: "plan-vbtn", pressed: !D.pipeline && D.part === k,
    data: { "dp-part": k }, attrs: `role="tab" aria-selected="${!D.pipeline && D.part === k}"` })).join("");
  const el = $("#dpParts"); if (el.dataset.sig !== html) { el.innerHTML = html; el.dataset.sig = html; strip(el); }
}

// On a phone the part buttons and a pipeline's tabs are one row that scrolls sideways (deploy.css, #6188): the chosen
// one comes into view, and the edge fades while more buttons are past it.
function strip(el) {
  const on = el.querySelector(".on");
  if (on && el.scrollWidth > el.clientWidth) {
    const r = on.getBoundingClientRect(), b = el.getBoundingClientRect();
    const pad = parseFloat(getComputedStyle(el).getPropertyValue("--fade")) || 0;  // clear of the faded edge
    if (r.left < b.left + pad) el.scrollLeft -= b.left + pad - r.left;
    else if (r.right > b.right - pad) el.scrollLeft += r.right - b.right + pad;
  }
  fadeStrip(el);
}
function fadeStrip(el) {
  el.classList.toggle("dp-more", el.scrollLeft + el.clientWidth < el.scrollWidth - 1);
  el.classList.toggle("dp-before", el.scrollLeft > 1);
}
for (const id of ["#dpParts", "#plTabs"]) $(id).addEventListener("scroll", () => fadeStrip($(id)), { passive: true });
window.addEventListener("resize", () => { if (tab === "deploy") ["#dpParts", "#plTabs"].forEach(id => fadeStrip($(id))); });

// The state of a stage as the list draws it: a version that waits for a person's yes is the person's color.
function barState(p, s) {
  if (s.no_news) return "none";  // its forge refuses the token: its last state is old (#6147)
  if (s.state === "queued" && p.waits.some(w => w.stage === s.name)) return "human";
  return s.state || "none";
}
// The mark of the transition between two stages (#6139): › automatic, ✋ a yes, ⏱ a delay, 📦 the release; when a
// control holds it: ⛔ blocked (or a fault), ⏸ disabled, ❄ frozen, 🕒 outside its window, ⏱ delayed.
const HOLD_MARK = { blocked: ["⛔", "warn"], fault: ["⛔", "warn"], disabled: ["⏸", ""], frozen: ["❄", "warn"],
  window: ["🕒", "human"], delayed: ["⏱", ""] };
function gateMark(s) {
  const t = s.transition || { mode: "auto", state: "open", text: "automatic" };
  if (t.state !== "open") return [...(HOLD_MARK[t.state] || ["⛔", "warn"]), t.state_text];
  if (t.mode === "yes" || t.mode === "release") return [t.mode === "yes" ? "✋" : "📦", "human", `Waits for ${t.text}`];
  if (t.mode === "delay") return ["⏱", "", t.text];
  return ["›", "", "Goes on by itself"];
}
function sourceChip(p) {
  // A followed pipeline (#6167): its outside system runs the flow; we read it.
  return (p.followed ? chip("c-waiting", "Read only", "Read only") : "") + sourceOnly(p);
}
// #6486: the Git host only, and a word when its token fails; the account and token are on Deploy > Accounts.
function sourceOnly(p) {
  const s = p.source;
  if (p.default) return "";
  if (s.state === "local") return p.followed ? "" : `<span class="dp-src muted t-s">${esc(s.label)}</span>`;
  const word = s.state === "bad" ? (s.plugin ? " · refresh failed" : " · token refused") : s.state === "missing" ? " · token missing" : "";
  return button(`${esc(s.label)}${word}`, { cls: "plain dp-src t-s"
    + (s.state === "bad" || s.state === "missing" ? " dp-bad" : ""), data: { "dp-part": "accounts" }, title: "Accounts" });
}

function stripHtml(p) {
  return p.stages.map((s, i) => {
    const [m, cls, title] = gateMark(s);
    return (i ? `<span class="dp-arrow ${cls ? "dp-" + cls : ""}" title="${esc(title)}">${m}</span>` : "")
      + `<span class="dp-mini" title="${esc(s.name)}${s.tier ? " (" + esc(s.tier) + ")" : ""}: ${s.no_news ? "no news" : esc(s.state)}${s.version ? " · " + esc(short(s.version)) : ""}">
        <span class="dp-bar ${esc(barState(p, s))}"></span><span class="dp-mn">${esc(s.name)}</span>${stageMarks(s.work)}</span>`;
  }).join("") || `<span class="muted t-s">No stages</span>`;
}
// #6183: a deploy target's one environment has the pipeline's own name: the row names it once, so the version and its
// time keep their room.
function liveHtml(p) {
  const live = p.envs.filter(e => e.rev);
  return live.length ? live.map(e => `<span class="dp-env">${e.name === p.name ? "" : `<b>${esc(e.name)}</b> `}<span class="pl-ver">${esc(short(e.rev))}</span>
    ${e.since ? `<span class="muted">${esc(ago(e.since))}</span>` : ""}</span>`).join("") : `<span class="muted">Not live</span>`;
}

function bannersHtml() {
  const out = [];
  for (const p of data.pipelines) for (const w of p.waits) {
    const what = `${esc(p.name)}: approve ${w.rev ? `<span class="pl-ver">${esc(short(w.rev))}</span>` : ""}`;
    const btns = (p.default ? (w.item ? button("Approve", { kind: "primary", data: { "dp-card": w.item }, title: "Approve" }) : "")
      : button("Approve", { kind: "primary", data: { "dp-approve": p.name, stage: w.stage, rev: w.rev || "" }, title: "Approve" }))
      + button("Open", { data: { "dp-open": p.name }, title: "Open" });
    out.push(`<div class="dp-banner dp-human"><span>${what}</span><span class="spacer"></span>${btns}</div>`);
  }
  // #6189: a deploy target whose collected tasks wait (for its cadence, or for the release that runs): Release now.
  for (const p of data.pipelines) if (p.release) {
    const r = p.release, n = `${r.tasks} task${r.tasks === 1 ? "" : "s"}`;
    out.push(`<div class="dp-banner"><span>${esc(p.name)}: ${n}${r.until ? ` · next in ${esc(left(r.until))}`
      : r.running ? ` · after #${r.running}` : ""}</span><span class="spacer"></span>${releaseButton(p)}
      ${button("Open", { data: { "dp-open": p.name }, title: "Open" })}</div>`);
  }
  const seen = new Set();
  for (const p of data.pipelines) {
    const s = p.source;
    if (!["bad", "missing"].includes(s.state) || seen.has(s.forge + "|" + s.secret)) continue;
    seen.add(s.forge + "|" + s.secret);
    const users = data.pipelines.filter(x => x.source.forge === s.forge && x.source.secret === s.secret).map(x => x.name);
    const what = `${esc(s.label)} token ${s.state === "bad" ? "refused" : "missing"}`;
    out.push(`<div class="dp-banner dp-warn"><span>${what}: ${esc(users.join(", "))}</span>
      <span class="spacer"></span>${button("Accounts", { data: { "dp-part": "accounts" }, title: "Accounts" })}</div>`);
  }
  return out.join("");
}

// A redraw replaces the rows (a hover and a click in progress are lost): only when the data changes, or once a
// minute for the times ("since 3m ago").
const dataSig = (...extra) => JSON.stringify([data, Math.floor(Date.now() / 60000), ...extra]);

function drawList(force) {
  const s = dataSig("list");
  if (s === sig && !force) return;
  sig = s;
  const html = bannersHtml(), rows = data.pipelines.map(p => button(`<span class="dp-row">
      <span class="dp-head"><b class="dp-pname">${esc(p.name)}</b>${sourceChip(p)}
        ${p.held ? `<span class="t-s c-warn">Held</span>` : ""}</span>
      <span class="dp-strip">${stripHtml(p)}</span>
      <span class="dp-live t-s">${liveHtml(p)}</span></span>`,
    { kind: "card", cls: "dp-pipe", data: { "dp-open": p.name }, title: "Open" })).join("");
  const body = rows || empty("No releases yet.");
  $("#dpBanners").innerHTML = html;
  $("#dpList").innerHTML = body;
}

// ---- the parts of Deploy other than Pipelines

function drawPart(force) {
  if (D.part === "audit") { sig = ""; return drawAudit(null, $("#dpPart"), force); }
  const s = dataSig(D.part);
  // Agents (#6149): drawn again only when its data changed, so a button under the pointer stays in its place.
  if (s === sig && (!force || D.part === "agents")) return;
  sig = s;
  const html = D.part === "where" ? whereHtml() : D.part === "accounts" ? accountsHtml() : D.part === "runners" ? runnersHtml(data)
    : agentsHtml(data);
  $("#dpPart").dataset.of = "";
  $("#dpPart").innerHTML = html;
}

// #6183: no row tint (every row with a version had the person's color), and on a phone each row is a block with its
// cells under their names (pl-blocks), not five columns that broke each word into letters.
function whereHtml() {
  const rows = data.pipelines.flatMap(p => p.envs.map(e => {
    const before = (e.history || []).filter(h => h.rev && h.rev !== e.rev).slice(0, 3).map(h => esc(short(h.rev))).join(", ");
    return `<tr>
    <td>${button(esc(p.name), { cls: "plain", data: { "dp-open": p.name }, title: "Open" })}</td>
    <td data-label="Environment">${esc(e.name)}</td>
    <td class="mono" data-label="Version">${esc(short(e.rev)) || `<span class="muted">—</span>`}</td>
    <td data-label="Since">${e.since ? esc(ago(e.since)) : `<span class="muted">-</span>`}</td>
    <td class="t-s muted"${before ? ` data-label="Before"` : ""}>${before}</td></tr>`;
  }));
  if (!rows.length) return empty("Nothing live yet.");
  return `<div class="pl-table"><table class="pl-blocks"><tr><th>Release</th><th>Environment</th><th>Version</th><th>Since</th><th>Before</th></tr>${rows.join("")}</table></div>`;
}

// The accounts: views/accounts.js (#6147).
function accountsHtml() { return `<div class="dp-cards">${accountsView(data)}</div>`; }


// ---- a pipeline's own page

function drawPage(force) {
  const p = pipe(D.pipeline), target = !!(p && p.default);
  // #6486: the head names the release once; a target's account, project and definition are not its first screen.
  const head = `${button("Releases ›", { cls: "plain", data: { "dp-part": "pipelines" }, title: "Releases" })}
    <b class="dp-pname">${esc(D.pipeline)}</b>${p && !target ? sourceChip(p) + definitionHead(p) : ""}`;
  if ($("#dpHead").dataset.sig !== head) { $("#dpHead").innerHTML = head; $("#dpHead").dataset.sig = head; }
  const tabs = target && D.tab === "stages" ? "" : (target ? TARGET_TABS : PAGE_TABS).map(([k, n]) => button(n, {
    cls: "plan-vbtn" + (k === "emergency" ? " dp-em" : ""), pressed: D.tab === k,
    data: { "dp-tab": k }, attrs: `role="tab" aria-selected="${D.tab === k}"` })).join("");
  if ($("#plTabs").dataset.sig !== tabs) { $("#plTabs").innerHTML = tabs; $("#plTabs").dataset.sig = tabs; strip($("#plTabs")); }
  $("#plTabs").classList.toggle("hidden", !tabs);
  const stages = ["stages", "versions", "controls"].includes(D.tab);
  $("#dpStages").classList.toggle("hidden", !stages);
  $("#dpPage").classList.toggle("hidden", stages);
  showPipeline(D.pipeline, D.tab);
  // The card of each target the pipeline names: a target's hooks and past releases are under Controls (#6486).
  showTargets(p && (target ? D.tab === "controls" : D.tab === "stages") ? p.targets : []);
  if (stages) { pageSig = ""; return renderPipeline(force); }
  // #6176: a deploy target's pipeline has its controls too (fixed: an older server sends no controls)
  const fixed = p && p.default && !p.controls;
  if (D.tab === "audit") {
    if (fixed) return setPage(empty("No audit entries."), force);
    return drawAudit(D.pipeline, $("#dpPage"), force);
  }
  if (D.tab === "triggers" && p && !fixed) { pageSig = ""; return drawTriggers(D.pipeline, $("#dpPage"), force); }
  if (D.tab === "emergency" && p && !fixed) { pageSig = ""; return drawEmergency(D.pipeline, $("#dpPage"), force); }
  setPage(!p ? empty(`No release ${esc(D.pipeline)}.`) : D.tab === "triggers" ? defaultTriggersHtml(p) : emergencyHtml(p), force);
}
function setPage(html, force) {
  const box = $("#dpPage");
  if (html === pageSig && !force && !box.querySelector(".dp-audit, .tg-view, .em-view")) return;
  pageSig = html; box.dataset.of = ""; box.innerHTML = html;
}

// A deploy target's Emergency tab of an older server: its release page has the actions. A pipeline's own:
// views/emergency.js (#6145).
function emergencyHtml(p) {
  return empty(`${esc(p.name)}: use Release now or Hold.`);
}

// ---- clicks

async function pipeAct(op, args, done) {
  try { await act(op, args); } catch (e) { return; }
  if (done) toast(done);
  renderDeploy(true);
}

$("#tab-deploy").addEventListener("click", (e) => {
  const r = e.target.closest("[data-rn-add],[data-rn-watch],[data-rn-check],[data-rn-stop]");
  if (r) return runnerClick(r, () => renderDeploy(true));  // Deploy > Runners (#6151)
  const dv = e.target.closest("[data-dv-open]");  // the versions of the definition (#6143)
  if (dv) return openVersions(dv.dataset.dvOpen, () => renderDeploy(true));
  const t = e.target.closest("[data-dp-part],[data-dp-open],[data-dp-tab],[data-dp-approve],[data-dp-card],[data-dp-check],[data-dp-triggers]");
  if (!t) return;
  const d = t.dataset;
  if (d.dpTriggers) return go({ part: null, pipeline: d.dpTriggers, tab: "triggers" });
  if (d.dpCheck !== undefined) return pipeAct("pipeline_account_check", { secret: d.dpCheck || null }, "Checked with the Git host");
  if (d.dpPart) return go({ part: d.dpPart, pipeline: null, tab: "stages" });
  if (d.dpOpen) return go({ part: null, pipeline: d.dpOpen, tab: "stages" });
  if (d.dpTab) return go({ part: null, pipeline: D.pipeline, tab: d.dpTab });
  if (d.dpCard) return import("../components/waitDialog.js").then(m => m.openItemCard(+d.dpCard));
  if (d.dpApprove) return pipeAct("pipeline_approve", { pipeline: d.dpApprove, stage: d.stage, rev: d.rev || null }, `${d.dpApprove}: ${d.stage} approved`);
});
