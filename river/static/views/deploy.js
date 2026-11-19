// The Deploy tab (#6136; canvas release-tracking Decision 6, mark 2026-11-18: "in the top we have sessions, Plan,
// Deploy, Terminal tabs"; canvas deploy-tab-mockup pictures 1, 4 and 13). Every pipeline, and every deploy target as
// its default pipeline, flows left to right (mark: "I want left to right view").
//   Parts of Deploy (buttons at the top): Pipelines (one row for each pipeline: its stages as bars, the gates between
//   them, what runs where, where its runs come from; above them what waits for the person's yes and the tokens that
//   fail), Agents, Runners, Where each version runs, Accounts, Audit log.
//   A pipeline's own page: Stages and Versions (views/pipeline.js, with the release card of each target it names,
//   views/targets.js), Triggers, Emergency and Audit log (mark 2026-11-18: emergency actions and triggers are on the
//   pipeline's own page).
// The red number on the tab counts what waits on the person here: a version at a person's gate, a release that waits
// for the person's yes, and each token that is refused or not saved. Reads: GET api/deploy (each pipeline in one
// read, river/routes/pipeline.py) and api/pipeline-audit. The address: /deploy, /deploy/<part>,
// /deploy/pipeline/<name>[/<tab>] (app.js writes it from D).
import { $, esc, getJson, ago, act, toast } from "../lib.js";
import { button } from "../components/button.js";
import { chip } from "../components/chip.js";
import { empty } from "../components/empty.js";
import { tab, nav } from "../page.js";
import { showPipeline, hidePipeline, renderPipeline, askReason } from "./pipeline.js";
import { showTargets } from "./targets.js";

export const PARTS = [["pipelines", "Pipelines"], ["agents", "Agents"], ["runners", "Runners"],
  ["where", "Where each version runs"], ["accounts", "Accounts"], ["audit", "Audit log"]];
export const PAGE_TABS = [["stages", "Stages"], ["versions", "Versions"], ["triggers", "⚡ Triggers"],
  ["emergency", "⚠ Emergency"], ["audit", "Audit log"]];

// What Deploy shows: a part, or the page of one pipeline and its tab. The address sets it (fromUrl), and a click.
export const D = { part: "pipelines", pipeline: null, tab: "stages" };
let data = null, readAt = 0, sig = "", pageSig = "", auditLines = null, auditOf = undefined;
const BADGE_EVERY = 30000;  // while Deploy is not on screen, the red number reads again at most this often

const short = (rev) => rev && /^[0-9a-f]{12,}$/.test(rev) ? rev.slice(0, 7) : rev || "";
const pipe = (name) => data && data.pipelines.find(p => p.name === name);
const plural = (n, one, many = one + "s") => `${n} ${n === 1 ? one : many}`;

// ---- the address

// The part of the address after /deploy: [] or [<part>] or ["pipeline", <name>, <tab>].
export function fromUrl(parts) {
  if (parts[0] === "pipeline" && parts[1]) {
    return { part: null, pipeline: parts[1], tab: PAGE_TABS.some(([k]) => k === parts[2]) ? parts[2] : "stages" };
  }
  return { part: PARTS.some(([k]) => k === parts[0]) ? parts[0] : "pipelines", pipeline: null, tab: "stages" };
}
export function setDeploy(r) { Object.assign(D, r); }
export function urlPath() {
  if (D.pipeline) return "/pipeline/" + encodeURIComponent(D.pipeline) + (D.tab === "stages" ? "" : "/" + D.tab);
  return D.part && D.part !== "pipelines" ? "/" + D.part : "";
}
function go(r) { setDeploy(r); nav.deployGo(); }

// The page of the pipeline that releases a target: the pipeline whose environment names it, else its default.
export function openTarget(name) {
  const p = data && data.pipelines.find(x => (x.targets || []).includes(name) && !x.default);
  go({ part: null, pipeline: p ? p.name : name, tab: "stages" });
}

// ---- reading and drawing

export async function renderDeploy(force) {
  const on = tab === "deploy";
  if (!on) { hidePipeline(); if (!force && Date.now() - readAt < BADGE_EVERY) return; }
  try { data = await getJson("api/deploy"); readAt = Date.now(); }
  catch (e) { if (on) $("#dpList").innerHTML = empty(esc(e.message)); return; }
  drawBadge();
  if (tab !== "deploy") return;
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
  b.title = n ? `${n} wait${n === 1 ? "s" : ""} for you` : "";
}

function drawParts() {
  const html = PARTS.map(([k, name]) => button(esc(name), { cls: "plan-vbtn", pressed: !D.pipeline && D.part === k,
    data: { "dp-part": k }, attrs: `role="tab" aria-selected="${!D.pipeline && D.part === k}"` })).join("");
  const el = $("#dpParts"); if (el.dataset.sig !== html) { el.innerHTML = html; el.dataset.sig = html; }
}

// The state of a stage as the list draws it: a person's gate that holds a version is the person's color.
function barState(p, s) {
  if (s.state === "queued" && p.waits.some(w => w.stage === s.name)) return "human";
  return s.state || "none";
}
// The mark of a gate between two stages: › automatic, ⏱ after a time, ✋ a yes, ⛔ held.
function gateMark(s) {
  if (s.held) return ["⛔", "warn", `Held: ${s.held}`];
  if (s.gate === "person" || s.gate === "manager") return ["✋", "human", `Waits for a yes: ${s.gate_text}`];
  if (s.gate_text && s.gate_text !== "auto") return ["⏱", "", s.gate_text];
  return ["›", "", "Goes on by itself"];
}
function sourceChip(p) {
  const s = p.source;
  if (p.default) return "";  // the head says "deploy target" already
  if (s.state === "local") return `<span class="dp-src muted t-s">${esc(s.label)}</span>`;
  const icon = { ok: "●", new: "○", bad: "✕", missing: "○" }[s.state];
  const word = s.state === "bad" ? " · token refused" : s.state === "missing" ? " · token not saved" : "";
  return `<span class="dp-src t-s ${s.state === "bad" || s.state === "missing" ? "dp-bad" : ""}" title="${esc(s.secret ? "Token " + s.secret : "No token named")}">${icon}
    ${esc(s.label)}${s.repo ? " · " + esc(s.repo) : ""}${word}</span>`;
}

function stripHtml(p) {
  return p.stages.map((s, i) => {
    const [m, cls, title] = gateMark(s);
    return (i ? `<span class="dp-arrow ${cls ? "dp-" + cls : ""}" title="${esc(title)}">${m}</span>` : "")
      + `<span class="dp-mini" title="${esc(s.name)}: ${esc(s.state)}${s.version ? " · " + esc(short(s.version)) : ""}">
        <span class="dp-bar ${esc(barState(p, s))}"></span><span class="dp-mn">${esc(s.name)}</span>${s.fault ? `<span class="dp-fault">⚠ #${s.fault}</span>` : ""}</span>`;
  }).join("") || `<span class="muted t-s">no stages yet</span>`;
}
function liveHtml(p) {
  const live = p.envs.filter(e => e.rev);
  return live.length ? live.map(e => `<span class="dp-env"><b>${esc(e.name)}</b> <span class="pl-ver">${esc(short(e.rev))}</span>
    ${e.since ? `<span class="muted">since ${esc(ago(e.since))}</span>` : ""}</span>`).join("") : `<span class="muted">runs nowhere yet</span>`;
}

function bannersHtml() {
  const out = [];
  for (const p of data.pipelines) for (const w of p.waits) {
    const what = `${esc(p.name)}: ${w.rev ? `<span class="pl-ver">${esc(short(w.rev))}</span> ` : ""}waits for your yes at ${esc(w.stage)}`;
    const btns = (p.default ? (w.item ? button("Open", { kind: "primary", data: { "dp-card": w.item }, title: "Approve or decline" }) : "")
      : button("Approve", { kind: "primary", data: { "dp-approve": p.name, stage: w.stage, rev: w.rev || "" }, title: "Let this version in" }))
      + button("Pipeline", { data: { "dp-open": p.name }, title: "Open the pipeline" });
    out.push(`<div class="dp-banner dp-human"><span>✋ ${what}</span><span class="spacer"></span>${btns}</div>`);
  }
  const seen = new Set();
  for (const p of data.pipelines) {
    const s = p.source;
    if (!["bad", "missing"].includes(s.state) || seen.has(s.forge + "|" + s.secret)) continue;
    seen.add(s.forge + "|" + s.secret);
    const users = data.pipelines.filter(x => x.source.forge === s.forge && x.source.secret === s.secret).map(x => x.name);
    const what = s.state === "bad" ? `${esc(s.label)} refuses the token ${esc(s.secret)}` : s.secret ? `The token ${esc(s.secret)} is not saved`
      : `${esc(p.name)} names no token for ${esc(s.label)}`;
    out.push(`<div class="dp-banner dp-warn"><span>✕ ${what}: ${esc(users.join(", "))} ${users.length === 1 ? "shows" : "show"} old data.</span>
      <span class="spacer"></span>${button("Accounts", { data: { "dp-part": "accounts" }, title: "See the accounts" })}</div>`);
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
      <span class="dp-head"><b class="dp-pname">${esc(p.name)}</b>${p.default ? `<span class="muted t-s">deploy target</span>` : ""}${sourceChip(p)}
        ${p.held ? `<span class="t-s c-warn">⛔ held</span>` : ""}</span>
      <span class="dp-strip">${stripHtml(p)}</span>
      <span class="dp-live t-s">${liveHtml(p)}</span></span>`,
    { kind: "card", cls: "dp-pipe", data: { "dp-open": p.name }, title: `Open ${p.name}` })).join("");
  const body = rows || empty(`No pipeline yet. Ask the manager for one ("build, test, then beta, then prod after my yes"),
    or give a project a deploy target.`);
  $("#dpBanners").innerHTML = html;
  $("#dpList").innerHTML = body;
}

// ---- the parts of Deploy other than Pipelines

function drawPart(force) {
  if (D.part === "audit") { sig = ""; return drawAudit(null, $("#dpPart"), force); }
  const s = dataSig(D.part);
  if (s === sig && !force) return;
  sig = s;
  const html = D.part === "where" ? whereHtml() : D.part === "accounts" ? accountsHtml() : D.part === "runners" ? runnersHtml()
    : agentsHtml();
  $("#dpPart").dataset.of = "";
  $("#dpPart").innerHTML = html;
}

function whereHtml() {
  const rows = data.pipelines.flatMap(p => p.envs.map(e => `<tr class="${e.rev ? "live" : ""}">
    <td>${button(esc(p.name), { cls: "plain", data: { "dp-open": p.name }, title: "Open the pipeline" })}</td>
    <td>${esc(e.name)}${e.target && e.target !== e.name ? ` <span class="muted t-s">target ${esc(e.target)}</span>` : ""}</td>
    <td class="mono">${esc(short(e.rev)) || `<span class="muted">none yet</span>`}</td><td>${e.since ? esc(ago(e.since)) : ""}</td>
    <td class="t-s muted">${(e.history || []).filter(h => h.rev && h.rev !== e.rev).slice(0, 3).map(h => esc(short(h.rev))).join(", ")}</td></tr>`));
  if (!rows.length) return empty("No environment yet: a pipeline's deploy stage names one.");
  return `<div class="pl-table"><table><tr><th>Pipeline</th><th>Environment</th><th>Version</th><th>Since</th><th>Before</th></tr>${rows.join("")}</table></div>`;
}

function accountsHtml() {
  const groups = new Map();
  for (const p of data.pipelines) {
    const s = p.source, key = s.state === "local" ? s.label : `${s.forge}|${s.url || ""}|${s.secret || ""}`;
    if (!groups.has(key)) groups.set(key, { s, pipes: [] });
    groups.get(key).pipes.push(p);
  }
  const card = ({ s, pipes }) => {
    const names = pipes.map(p => button(esc(p.name), { cls: "plain", data: { "dp-open": p.name }, title: "Open the pipeline" })).join(" ");
    if (s.state === "local") return `<div class="pl-card"><h3>${s.label === "this computer" ? "This computer" : "Deploy targets"}</h3>
      <div class="t-m muted">${s.label === "this computer" ? "maxpm serve runs these stages here: no token." : "MaximizePM's own releases: no token."}</div>
      <div class="t-m">${names}</div></div>`;
    const state = { ok: chip("c-ready", "works"), new: chip("c-waiting", "not read yet"), bad: chip("c-warn", "refused"),
      missing: chip("c-warn", "not saved") }[s.state];
    const repos = [...new Set(pipes.map(p => p.source.repo).filter(Boolean))];
    return `<div class="pl-card"><h3>${esc(s.label)}${s.url ? ` <span class="muted t-s">${esc(s.url)}</span>` : ""} ${state}</h3>
      <div class="t-m">Token <span class="pl-ver">${esc(s.secret || "none named")}</span>${s.read_at ? ` · read ${esc(ago(s.read_at))}` : ""}</div>
      ${s.error ? `<div class="t-s c-warn">${esc(s.error)}</div>` : ""}
      ${s.state === "missing" && s.secret ? `<div class="t-s muted">Save it in a terminal: <code>maxpm secret put ${esc(s.secret)}</code></div>` : ""}
      ${repos.length ? `<div class="t-s muted">${esc(repos.join(", "))}</div>` : ""}
      <div class="t-m">${names}</div></div>`;
  };
  return groups.size ? `<div class="dp-cards">${[...groups.values()].map(card).join("")}</div>` : empty("No pipeline yet.");
}

function runnersHtml() {
  const local = data.pipelines.filter(p => !p.default && p.source.state === "local");
  const running = data.pipelines.flatMap(p => p.stages.filter(s => s.state === "running").map(s => `${p.name} › ${s.name}`));
  return `<div class="dp-cards"><div class="pl-card"><h3>This computer ${chip("c-ready", "maxpm serve")}</h3>
    <div class="t-m">Runs the stages of the pipelines with no forge${local.length ? ": " + local.map(p => esc(p.name)).join(", ") : ""}.</div>
    <div class="t-m">${running.length ? "Runs now: " + running.map(esc).join(", ") : `<span class="muted">Runs no stage now.</span>`}</div></div></div>
    <div class="muted t-s">A forge runs the stages of its own pipelines on its own runners.</div>`;
}

function agentsHtml() {
  return empty("No agent works on a release here yet. Each agent that works on a stage or a fault of a pipeline shows here, with its step.");
}

// The audit log of every pipeline (pipeline: null) or of one: who changed what, newest first. A search field filters it.
async function drawAudit(name, box, force) {
  if (!box.querySelector(".dp-audit") || box.dataset.of !== String(name)) {
    box.dataset.of = String(name);
    box.innerHTML = `<div class="dp-audit"><div class="pl-bar"><input type="search" class="dp-q" placeholder="Search the log"
      aria-label="Search the log"><span class="muted t-s dp-an"></span></div><div class="dp-at"></div></div>`;
    auditLines = null;
  }
  let A;
  try { A = await getJson("api/pipeline-audit" + (name ? "?pipeline=" + encodeURIComponent(name) : "")); }
  catch (e) { box.querySelector(".dp-at").innerHTML = empty(esc(e.message)); return; }
  if (box.dataset.of !== String(name)) return;
  const s = JSON.stringify(A.lines);
  if (s === auditLines && auditOf === name && !force) return;
  auditLines = s; auditOf = name;
  box._lines = A.lines;
  filterAudit(box);
}
function filterAudit(box) {
  const q = (box.querySelector(".dp-q").value || "").trim().toLowerCase(), L = box._lines || [];
  const shown = q ? L.filter(l => [l.actor, l.change, l.pipeline].join(" ").toLowerCase().includes(q)) : L;
  const one = box.dataset.of !== "null";
  box.querySelector(".dp-an").textContent = L.length ? `${shown.length} of ${plural(L.length, "line")}` : "";
  box.querySelector(".dp-at").innerHTML = !L.length ? empty("Nothing changed yet: each change of a pipeline and each action on it (hold, approve, roll back, run again, stop) goes here with who did it.")
    : `<div class="pl-table"><table><tr><th>When</th><th>Who</th>${one ? "" : "<th>Pipeline</th>"}<th>What</th></tr>
      ${shown.map(l => `<tr><td class="mono" title="${esc(l.at)}">${esc(ago(l.at))}</td><td>${esc(l.actor || "")}</td>
        ${one ? "" : `<td>${button(esc(l.pipeline), { cls: "plain", data: { "dp-open": l.pipeline }, title: "Open the pipeline" })}</td>`}
        <td class="t-m">${esc(l.change)}</td></tr>`).join("")}</table></div>`;
}

// ---- a pipeline's own page

function drawPage(force) {
  const p = pipe(D.pipeline);
  const head = `${button("Pipelines ›", { cls: "plain", data: { "dp-part": "pipelines" }, title: "All pipelines" })}
    <b class="dp-pname">${esc(D.pipeline)}</b>${p ? (p.default ? `<span class="muted t-s">deploy target</span>` : "")
      + (p.project ? `<span class="muted t-s">project ${esc(p.project)}</span>` : "") + sourceChip(p) : ""}`;
  if ($("#dpHead").dataset.sig !== head) { $("#dpHead").innerHTML = head; $("#dpHead").dataset.sig = head; }
  const tabs = PAGE_TABS.map(([k, n]) => button(n, { cls: "plan-vbtn" + (k === "emergency" ? " dp-em" : ""), pressed: D.tab === k,
    data: { "dp-tab": k }, attrs: `role="tab" aria-selected="${D.tab === k}"` })).join("");
  if ($("#plTabs").dataset.sig !== tabs) { $("#plTabs").innerHTML = tabs; $("#plTabs").dataset.sig = tabs; }
  const stages = ["stages", "versions"].includes(D.tab);
  $("#dpStages").classList.toggle("hidden", !stages);
  $("#dpPage").classList.toggle("hidden", stages);
  showPipeline(D.pipeline, D.tab);
  showTargets(D.tab === "stages" && p ? p.targets : []);
  if (stages) { pageSig = ""; return renderPipeline(force); }
  if (D.tab === "audit") {
    if (p && p.default) return setPage(empty("A deploy target keeps no audit log here: each release's history is on its items (Past deploys on the Stages tab)."), force);
    return drawAudit(D.pipeline, $("#dpPage"), force);
  }
  setPage(!p ? empty(`No pipeline or deploy target ${esc(D.pipeline)}.`) : D.tab === "triggers" ? triggersHtml(p) : emergencyHtml(p), force);
}
function setPage(html, force) {
  const box = $("#dpPage");
  if (html === pageSig && !force && !box.querySelector(".dp-audit")) return;
  pageSig = html; box.dataset.of = ""; box.innerHTML = html;
}

// What starts work when something happens in this pipeline: today the same rules for every pipeline (#6114, #6118).
function triggersHtml(p) {
  const rule = (when, then) => `<div class="dp-rule"><div class="t-m"><b>When</b> ${when}</div><div class="t-m"><b>Then</b> ${then}</div></div>`;
  const rules = p.default ? [
    rule("a release waits for a person's yes", "MaximizePM asks the person (a task in What waits for you)."),
    rule("a check fails while the watch of a release runs", "its check opens an operational task with the evidence, and a new session takes it."),
  ] : [
    rule("a stage fails", p.project ? `MaximizePM opens a fault task in ${esc(p.project)} with the failure and the command that runs the stage again; a later failure of the same stage joins that task.`
      : "nothing: the pipeline names no project to hold a fault task."),
    rule("a version waits at a person's gate", "MaximizePM asks that person for a yes (a task in What waits for you)."),
    rule("a version waits at the manager's gate", "MaximizePM alerts the manager once."),
  ];
  return `<div class="dp-cards">${rules.join("")}</div><div class="muted t-s">These rules are the same for every pipeline now.</div>`;
}

// The emergency actions of this pipeline (#6117 actions; the operators of Decision 7 come later): each asks why and
// goes in the audit log with who pressed it.
function emergencyHtml(p) {
  if (p.default) return empty(`${esc(p.name)} is a deploy target: its release card on the Stages tab has Release now and Deploy now. Hold, stop and put back work on a pipeline of its own.`);
  const stop = p.held
    ? `<div class="pl-banner">⛔ Every stage is held${p.held_by ? " by " + esc(p.held_by) : ""}: ${esc(p.held)}</div>${button("End the hold", { kind: "primary", data: { "dp-unhold": p.name }, title: "Stages start again" })}`
    : button("Hold every stage", { kind: "warn", data: { "dp-hold": p.name }, title: "Nothing new starts" });
  const runs = p.stages.filter(s => ["running", "queued"].includes(s.state) && s.run != null);
  const envs = p.envs.map(e => {
    const before = (e.history || []).filter(h => h.rev && h.rev !== e.rev).slice(0, 3);
    return `<div class="dp-envrow"><span><b>${esc(e.name)}</b> <span class="pl-ver">${esc(short(e.rev)) || "none yet"}</span></span>
      ${before.map(h => button(`Put back ${esc(short(h.rev))}`, { kind: "warn", data: { "dp-rollback": e.name, rev: h.rev }, title: `Roll back to ${short(h.rev)}` })).join("")
        || `<span class="muted t-s">no earlier version</span>`}</div>`;
  }).join("");
  return `<div class="dp-cards">
    <div class="pl-card dp-em-card"><h3>Stop this pipeline</h3><div class="t-m">Nothing new starts in any stage until you end the hold. Work that runs goes on to its end.</div><div class="pl-bar">${stop}</div></div>
    <div class="pl-card dp-em-card"><h3>Stop what runs now</h3>${runs.length ? runs.map(s => `<div class="dp-envrow"><span><b>${esc(s.name)}</b>
      <span class="pl-ver">${esc(short(s.version))}</span> <span class="muted t-s">${esc(s.state === "queued" ? "waits" : "runs")}</span></span>
      ${button("Stop", { kind: "warn", data: { "dp-stop": s.run }, title: "Stop this run" })}</div>`).join("") : `<div class="muted t-m">Nothing runs now.</div>`}</div>
    <div class="pl-card dp-em-card"><h3>Put an environment back</h3>${envs || `<div class="muted t-m">This pipeline has no environment.</div>`}
      <div class="muted t-s">The deploy stage runs with that version, and its gate holds after it, so no newer version comes by itself.</div></div>
  </div><div class="muted t-s">A person or the manager may press these. A hold and a put back ask why; the audit log names who pressed each one.</div>`;
}

// ---- clicks

async function pipeAct(op, args, done) {
  try { await act(op, args); } catch (e) { return; }
  if (done) toast(done);
  renderDeploy(true);
}

$("#tab-deploy").addEventListener("input", (e) => {
  const box = e.target.closest(".dp-audit");
  if (box && e.target.classList.contains("dp-q")) filterAudit(box.parentElement);
});
$("#tab-deploy").addEventListener("click", (e) => {
  const t = e.target.closest("[data-dp-part],[data-dp-open],[data-dp-tab],[data-dp-approve],[data-dp-card],[data-dp-hold],[data-dp-unhold],[data-dp-stop],[data-dp-rollback]");
  if (!t) return;
  const d = t.dataset;
  if (d.dpPart) return go({ part: d.dpPart, pipeline: null, tab: "stages" });
  if (d.dpOpen) return go({ part: null, pipeline: d.dpOpen, tab: "stages" });
  if (d.dpTab) return go({ part: null, pipeline: D.pipeline, tab: d.dpTab });
  if (d.dpCard) return import("../components/waitDialog.js").then(m => m.openItemCard(+d.dpCard));
  if (d.dpApprove) return pipeAct("pipeline_approve", { pipeline: d.dpApprove, stage: d.stage, rev: d.rev || null }, `${d.dpApprove}: ${d.stage} approved`);
  if (d.dpHold) return askReason("Hold every stage?", "Nothing new starts in any stage. Work that runs goes on to its end.", "Hold",
    (reason) => pipeAct("pipeline_hold", { pipeline: d.dpHold, stage: null, reason }, `${d.dpHold} is held`));
  if (d.dpUnhold) return pipeAct("pipeline_unhold", { pipeline: d.dpUnhold, stage: null }, `${d.dpUnhold} goes on`);
  if (d.dpStop) return pipeAct("pipeline_stop", { pipeline: D.pipeline, run: +d.dpStop }, "Stopped");
  if (d.dpRollback) return askReason(`Put ${short(d.rev)} back on ${d.dpRollback}?`,
    "Its deploy stage runs with this version, and that gate holds until you end the hold, so no newer version comes by itself.",
    "Put back", (reason) => pipeAct("pipeline_rollback", { pipeline: D.pipeline, env: d.dpRollback, rev: d.rev, reason }, `${d.dpRollback} goes back to ${short(d.rev)}`));
});
