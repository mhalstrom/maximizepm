// The tabs Stages and Versions of a pipeline's page in Deploy (#6116 in Plan, moved to Deploy by #6136; canvas
// pipeline-mockup v2 of #6110, mark 2026-11-18: "kind of like code pipelines"). One pipeline at a time, the one that
// views/deploy.js names (showPipeline): its stages left to right (they get narrow to fit the width, then the row
// scrolls with a faded edge), the gates between them, the detail of the chosen stage (its steps on a time line, its
// last lines, its tests failed first with the prompt that analyzes the failures), where each version runs, and the
// versions in each stage. A deploy target with no pipeline of its own shows as its default pipeline, computed from
// its releases. Reads: GET api/pipeline/<name>, api/pipeline-run/<id> (river/routes/pipeline.py); the actions (hold,
// approve, roll back, run again) are #6117.
import { $, esc, getJson, ago, copyText, toast, act } from "../lib.js";
import { button, iconButton } from "../components/button.js";
import { chip } from "../components/chip.js";
import { empty } from "../components/empty.js";
import { makeDialog } from "../components/dialog.js";
import { field, textarea, actions } from "../components/form.js";

const V = { shown: false, name: null, data: null, run: null, stage: null, tab: "stages", filter: "failed" };
let sig = "", runSig = "";
const WORD = { passed: "passed", running: "running", failed: "failed", queued: "waiting", skipped: "skipped",
  cancelled: "cancelled", stopped: "stopped", error: "error", none: "no run yet" };
const GATE = { auto: "→", manager: "✋", person: "✋" };

const st = (s, word) => `<span class="pl-st ${esc(s || "none")}">● ${esc(word || WORD[s] || s || "no run yet")}</span>`;
const short = (rev) => rev && /^[0-9a-f]{12,}$/.test(rev) ? rev.slice(0, 7) : rev || "";
const secs = (n) => n == null ? "" : n < 90 ? `${Math.round(n)}s` : n < 5400 ? `${Math.round(n / 60)}m` : `${Math.round(n / 3600)}h`;

// The pipeline and tab that the Deploy tab shows (views/deploy.js): another pipeline starts with no chosen stage.
export function showPipeline(name, tab) {
  if (name !== V.name) { V.stage = null; V.run = null; V.version = null; V.data = null; sig = ""; runSig = ""; }
  V.name = name; V.tab = tab; V.shown = !!name && ["stages", "versions"].includes(tab);
}
export function hidePipeline() { V.shown = false; }

export async function renderPipeline(force) {
  if (!V.shown) return;
  const name = V.name;
  let D;
  try { D = await getJson("api/pipeline/" + encodeURIComponent(name)); }
  catch (e) {
    if (V.name !== name) return;
    $("#plNote").textContent = ""; sig = "";
    $("#plBody").innerHTML = empty(`No pipeline or deploy target ${esc(name)}: ${esc(e.message)}`);
    return;
  }
  if (V.name !== name || !V.shown) return;  // another pipeline or tab while the read ran
  V.data = D;
  if (!V.data.stages.some(s => s.name === V.stage)) V.stage = (V.data.stages.find(s => s.last_run && s.last_run.state === "failed")
    || V.data.stages.find(s => s.last_run) || V.data.stages[0] || {}).name || null;
  const s = JSON.stringify([V.name, V.data, V.stage, V.tab, V.filter]);
  if (s === sig && !force) return refreshRun(force);
  sig = s;
  draw();
  refreshRun(true);
}

function draw() {
  const D = V.data;
  const envs = D.envs.length;
  $("#plNote").innerHTML = `${D.stages.length} stage${D.stages.length === 1 ? "" : "s"} · ${envs} environment${envs === 1 ? "" : "s"}`
    + (D.default ? ` · the releases of deploy target ${esc(D.name)}, as a pipeline` : "")  // the page's head names the project
    + (D.default ? "" : " " + (D.held_reason ? button("End the hold", { data: { "pl-unhold": "" }, title: "Stages start again" })
      : button("Hold all stages", { kind: "warn", data: { "pl-hold": "" }, title: "Nothing new starts" })))
    + (D.held_reason ? `<div class="pl-banner">Every stage is held by ${esc(D.held_by || "a person")}: ${esc(D.held_reason)}. Work that runs goes on; nothing new starts.</div>` : "");
  $("#plBody").innerHTML = V.tab === "versions" ? versionsView(D) : stagesView(D);
  if (V.tab === "stages") fit();
}

// ---- the stages left to right

function stageCard(s) {
  const r = s.last_run, live = r && s.env && r.state === "passed";
  const stepDots = r && r.step_list ? r.step_list.map(x => x.state) : r ? Object.entries(r.steps || {}).flatMap(([k, n]) => Array(Math.min(n, 12)).fill(k)) : [];
  const meta = !r ? "" : r.state === "queued" && r.gate_note ? r.gate_note
    : r.ended_at ? `${WORD[r.state] || r.state} ${ago(r.ended_at)}` : r.started_at ? `since ${ago(r.started_at)}` : "";
  return button(`<span class="pl-in"><span class="pl-kind">${esc(s.kind)}${s.env ? " · " + esc(s.env) : ""}</span>
    <span class="pl-name">${esc(s.name)}</span>
    <span class="pl-ver">${esc(short(r && r.version))}</span>
    ${r ? st(r.state, live ? "live" : null) : st("none")}
    <span class="pl-meta pl-more-line">${esc(meta)}</span>
    ${stepDots.length ? `<span class="pl-dots pl-more-line" aria-hidden="true">${stepDots.map(x => `<span class="pl-dot ${esc(x)}"></span>`).join("")}</span>` : ""}</span>`,
    { kind: "card", cls: "pl-stage" + (s.env ? " pl-env" : ""), pressed: s.name === V.stage, data: { "pl-stage": s.name },
      title: s.run ? "Runs: " + s.run : s.workflow ? "Forge workflow: " + s.workflow : s.name });
}

function gateEl(s) {
  const g = s.gate || "auto", rule = s.gate_text || g;
  if (s.held_reason) return `<div class="pl-gate">${chip("c-warn", `⛔<span class="pl-long"> held</span>`, `Held: ${s.held_reason}`)}</div>`;
  const cls = g === "auto" ? "" : "c-human";
  return `<div class="pl-gate">${chip(cls, `${GATE[g] || "→"}<span class="pl-long"> ${esc(rule === "auto" ? "auto" : rule)}</span>`,
    `The gate before ${s.name}: ${rule}`)}</div>`;
}

function stagesView(D) {
  if (!D.stages.length) return empty(`Pipeline ${esc(D.name)} has no stages yet: maxpm pipeline stage ${esc(D.name)} &lt;stage&gt; --kind &lt;kind&gt;`);
  const row = D.stages.map((s, i) => (i ? gateEl(s) : "") + stageCard(s)).join("");
  return `<div class="pl-scroll" id="plScroll"><div class="pl-row" id="plRow">${row}</div></div>
    <div class="pl-card" id="plDetail">${detail()}</div>
    ${envTable(D)}`;
}

// Narrow cards first; when even those do not fit, the row scrolls and its right edge fades while more is there.
function fit() {
  const sc = $("#plScroll"); if (!sc) return;
  sc.classList.remove("pl-compact");
  if (sc.scrollWidth > sc.clientWidth + 1) sc.classList.add("pl-compact");
  fade();
}
function fade() {
  const sc = $("#plScroll"); if (!sc) return;
  sc.classList.toggle("pl-more", sc.scrollLeft + sc.clientWidth < sc.scrollWidth - 1);
}

function envTable(D) {
  if (!D.envs.length) return "";
  const rows = D.envs.map(e => `<tr class="${e.rev ? "live" : ""}"><td>${esc(e.name)}${e.target && e.target !== e.name ? ` <span class="muted t-s">target ${esc(e.target)}</span>` : ""}</td>
    <td class="mono">${esc(short(e.rev)) || `<span class="muted">none yet</span>`}</td><td>${e.since ? esc(ago(e.since)) : ""}</td>
    <td class="t-s muted">${(e.history || []).slice(1, 4).filter(h => h.rev && h.rev !== e.rev).map(h => esc(short(h.rev))
      + (h.kind !== "deployed" ? " (" + esc(h.kind) + ")" : "") + (D.default ? "" : " " + button("Put back", { data: { "pl-rollback": e.name, rev: h.rev },
        title: `Roll back to ${short(h.rev)}` }))).join(" ")}</td></tr>`).join("");
  return `<div class="pl-card"><h3>Where each version runs</h3><div class="pl-table"><table>
    <tr><th>Environment</th><th>Version</th><th>Since</th><th>Before</th></tr>${rows}</table></div></div>`;
}

// ---- the detail of the chosen stage

function chosen() { return V.data && V.data.stages.find(s => s.name === V.stage); }

async function refreshRun(force) {
  const s = chosen(), r = s && s.last_run;
  if (!r || r.id == null) { V.run = r || null; if (force && $("#plDetail")) $("#plDetail").innerHTML = detail(); return; }
  let R;
  try { R = await getJson("api/pipeline-run/" + r.id); } catch (e) { return; }
  const s2 = JSON.stringify([R, V.filter]);
  if (s2 === runSig && !force) return;
  runSig = s2; V.run = R;
  if ($("#plDetail")) $("#plDetail").innerHTML = detail();
}

function gantt(steps) {
  const t = (x) => Date.parse(x);
  const timed = steps.filter(x => x.started_at || x.ended_at);
  if (!timed.length) return "";
  const t0 = Math.min(...timed.map(x => t(x.started_at || x.ended_at)));
  const t1 = Math.max(...timed.map(x => x.ended_at ? t(x.ended_at) : Date.now()), t0 + 1000);
  const pct = (v) => ((v - t0) / (t1 - t0) * 100).toFixed(1);
  return `<div class="pl-gantt">${timed.map(x => {
    const a = t(x.started_at || x.ended_at), b = x.ended_at ? t(x.ended_at) : Date.now();
    const w = Math.max(1.5, (b - a) / (t1 - t0) * 100).toFixed(1);
    return `<div>${esc(x.name)}${x.host ? ` <span class="muted">${esc(x.host)}</span>` : ""} <span class="muted">${esc(secs(x.seconds ?? (b - a) / 1000))}</span></div>
      <div class="pl-track" title="${esc(x.name)}: ${esc(WORD[x.state] || x.state)}${x.note ? " · " + esc(x.note) : ""}"><div class="pl-seg ${esc(x.state)}" style="left:${pct(a)}%;width:${w}%"></div></div>`;
  }).join("")}</div>`;
}

function detail() {
  const s = chosen(); if (!s) return "";
  const r = V.run && (V.run.id == null || (s.last_run && V.run.id === s.last_run.id)) ? V.run : s.last_run;
  const head = `<h3>${esc(s.name)}${r && r.version ? ` · <span class="pl-ver">${esc(short(r.version))}</span>` : ""}</h3>`;
  if (!r) return head + `<div class="muted t-s">No run of this stage yet.${s.run ? " It runs: " + esc(s.run) : ""}</div>`;
  const when = r.started_at ? `started ${esc(ago(r.started_at))}` : r.queued_at ? `waiting since ${esc(ago(r.queued_at))}` : "";
  const item = (r.item ? ` · <span class="link" data-open="${r.item}">#${r.item}</span>` : "")
    + (r.fault_item ? ` · fault <span class="link" data-open="${r.fault_item}">#${r.fault_item}</span>` : "");
  return head + `<div class="pl-bar">${st(r.state)}<span class="muted t-s">${when}${r.ended_at ? ` · ended ${esc(ago(r.ended_at))}` : ""}${item}</span></div>`
    + (s.held_reason ? `<div class="pl-banner">Held by ${esc(s.held_by || "a person")}: ${esc(s.held_reason)}</div>` : "")
    + (r.gate_note && r.state === "queued" ? `<div class="t-s">Gate: ${esc(r.gate_note)}</div>` : "")
    + stageActions(s, r)
    + (r.errors ? `<div class="t-s c-warn">${esc(r.errors)}</div>` : "")
    + wavesHtml(s, r)
    + gantt(r.step_list || [])
    + (r.output ? `<pre class="pl-pre">${esc(r.output)}</pre>` : "")
    + (r.test_list && r.test_list.length ? tests(r) : "");
}

// A stage over many servers (#6120): one card for each wave, each server with the state of its deploy, its check
// and, after a failed wave, its roll back (the steps of the run with where=<host>).
function wavesHtml(s, r) {
  let waves;
  try { waves = s.waves ? JSON.parse(s.waves) : null; } catch (e) { waves = null; }
  if (!waves || !waves.length) return "";
  const of = (host, name) => (r.step_list || []).filter(x => x.host === host && x.name === name).pop();
  const cell = (x, word) => x ? st(x.state, `${word} ${WORD[x.state] || x.state}`) : "";
  const card = (w, n) => `<div class="pl-wave"><b class="t-m">Wave ${n + 1}</b>${w.map(h => {
    const d = of(h, "deploy"), c = of(h, "check"), b = of(h, "rollback");
    return `<div class="pl-host"><span class="pl-ver">${esc(h)}</span><span class="pl-bar">${d ? cell(d, "deploy") + cell(c, "check")
      + cell(b, "roll back") : st("queued", "not started")}</span></div>`;
  }).join("")}</div>`;
  return `<div class="pl-waves">${waves.map(card).join("")}</div>`;
}

// The actions on a stage (#6117): only a person or the manager acts; a default pipeline (a target's releases) has
// its own actions on Releases.
function stageActions(s, r) {
  if (V.data.default) return "";
  const rev = r && r.version, b = [];
  b.push(s.held_reason ? button("End the hold", { data: { "pl-unhold": s.name }, title: "New versions enter" })
    : button("Hold here", { kind: "warn", data: { "pl-hold": s.name }, title: "No new version enters" }));
  if (r && r.state === "queued" && ["manager", "person"].includes(s.gate) && rev)
    b.push(button(`Approve ${esc(short(rev))}`, { kind: "primary", data: { "pl-approve": s.name, rev }, title: "Let this version in" }));
  if (r && ["running", "queued"].includes(r.state) && r.id != null) b.push(button("Stop", { kind: "warn", data: { "pl-stop": r.id }, title: "Stop this run" }));
  if (r && rev && !["running", "queued"].includes(r.state)) b.push(button("Run again", { data: { "pl-rerun": s.name, rev }, title: "Run this stage again" }));
  const bad = r && r.tests ? (r.tests.failed || 0) + (r.tests.error || 0) : 0;
  if (bad && r.id != null && !["running", "queued"].includes(r.state))
    b.push(button(`Run the ${bad} failed again`, { data: { "pl-refail": r.id }, title: "Only the failed tests" }));
  return `<div class="pl-bar">${b.join("")}</div>`;
}

// The reason of a hold or a roll back: a small dialog (components/dialog.js). The Emergency tab asks with it too.
let askDlg = null, askBox = null, askThen = null;
export function askReason(title, text, go, then) {
  if (!askDlg) {
    const d = document.createElement("div");
    d.id = "plAskDlg"; d.className = "hidden";
    d.innerHTML = '<div class="box"></div>';
    document.body.appendChild(d);
    askBox = d.firstElementChild;
    askDlg = makeDialog(d);
  }
  askThen = then;
  askBox.innerHTML = `<div class="dlg-head"><h2>${esc(title)}</h2>${iconButton("✕", "Close", { data: { "pl-askclose": "1" } })}</div>
    <p class="t-m">${esc(text)}</p>
    <div class="form">${field("Why?", textarea({ id: "plWhy", rows: 3 }, ""))}</div>
    ${actions(button("Cancel", { data: { "pl-askclose": "1" } }) + button(go, { kind: "primary", data: { "pl-askgo": "1" } }))}`;
  askDlg.open();
  askBox.querySelector("#plWhy").focus();
  askBox.onclick = async (e) => {
    if (e.target.closest("[data-pl-askclose]")) return askDlg.close();
    if (!e.target.closest("[data-pl-askgo]")) return;
    const why = (askBox.querySelector("#plWhy").value || "").trim();
    if (!why) return toast("Say why", true);
    askDlg.close();
    await askThen(why);
  };
}

async function doAct(op, args) {
  try { await act(op, { pipeline: V.name, ...args }); } catch (e) { return; }
  runSig = ""; sig = "";
  renderPipeline(true);
}

// ---- the test view: each test's result, failed first, and the prompt that analyzes the failures

function tests(r) {
  const all = r.test_list, bad = all.filter(t => t.state === "failed" || t.state === "error");
  const skipped = all.filter(t => t.state === "skipped").length;
  const shown = V.filter === "failed" && bad.length ? bad : all;
  const suites = [...new Set(all.map(t => t.suite))];
  return `<div class="pl-bar">${st(bad.length ? "failed" : "passed", `${bad.length} failed`)}${st("passed", `${all.length - bad.length - skipped} passed`)}${st("skipped", `${skipped} skipped`)}
      <span class="muted t-s">from the test report (JUnit XML)</span></div>
    ${suites.slice(0, 20).map(su => `<div class="pl-suite"><span class="pl-sname">${esc(su)}</span><span class="pl-dots">${all.filter(t => t.suite === su).map(t =>
      `<span class="pl-dot ${esc(t.state)}" title="${esc(t.name)}: ${esc(t.state)}"></span>`).join("")}</span></div>`).join("")}
    <div class="pl-bar">${button(`Failed (${bad.length})`, { pressed: V.filter === "failed", data: { "pl-filter": "failed" } })}${button(`All ${all.length}`,
      { pressed: V.filter === "all", data: { "pl-filter": "all" } })}</div>
    <div class="pl-table"><table class="pl-tests"><tr class="pl-th"><th>Test</th><th>Result</th><th>Time</th><th>Message</th></tr>
      ${shown.slice(0, 500).map(t => `<tr><td class="mono">${esc(t.suite)}.${esc(t.name)}</td><td>${st(t.state)}</td><td>${esc(secs(t.seconds))}</td>
        <td class="t-s">${esc(t.message || "")}</td></tr>`).join("")}</table></div>
    ${r.prompt ? `<div class="pl-card"><h3>Prompt to analyze the failures</h3><pre class="pl-pre">${esc(r.prompt)}</pre>
      <div class="pl-bar">${button("Start a session", { kind: "primary", data: { "pl-fix": r.id, start: 1 }, title: "Fix it now" })}${button("Add a fix task",
        { data: { "pl-fix": r.id }, title: "Queue a fix" })}${button("Copy", { data: { "pl-copy": 1 }, title: "Copy the prompt" })}</div></div>` : ""}`;
}

// ---- the versions in each stage

function versionsView(D) {
  if (!D.versions.length) return empty("No version went through this pipeline yet.");
  const names = D.stages.map(s => s.name);
  const live = new Set(D.envs.map(e => e.rev).filter(Boolean));
  const cell = (c) => c ? st(c.state) : `<span class="muted">·</span>`;
  return `<div class="muted t-s">Each row is one version, newest on top. Read across to see how far it went.</div>
    <div class="pl-table"><table><tr><th>Version</th>${names.map(n => `<th>${esc(n)}</th>`).join("")}</tr>
    ${D.versions.map(v => `<tr class="${live.has(v.rev) ? "live" : ""}"><td class="mono">${v.deploy
        ? `<span class="link" data-report="${v.deploy}" title="The release report">${esc(short(v.rev))}</span>`
        : button(esc(short(v.rev)), { cls: "plain", data: { "pl-version": v.rev }, title: "Its tasks", pressed: V.version === v.rev })}${v.title ? ` <span class="muted t-s">${esc(v.title)}</span>` : ""}</td>
      ${names.map(n => `<td>${cell(v.stages[n])}</td>`).join("")}</tr>`).join("")}</table></div>
    <div id="plTasks"></div>`;
}

// The tasks of a version (#6118): the items its commits name since the version before; a click opens one.
async function drawTasks(rev) {
  const box = $("#plTasks"); if (!box) return;
  if (!rev) { box.innerHTML = ""; return; }
  let T;
  try { T = await getJson(`api/pipeline-version?pipeline=${encodeURIComponent(V.name)}&rev=${encodeURIComponent(rev)}`); }
  catch (e) { box.innerHTML = `<div class="muted t-s">${esc(e.message)}</div>`; return; }
  box.innerHTML = `<div class="pl-card"><h3>Tasks of ${esc(short(rev))}${T.base ? ` <span class="muted t-s">since ${esc(short(T.base))}</span>` : ""}</h3>
    ${T.tasks.length ? T.tasks.map(t => `<div class="t-m"><span class="link" data-open="${t.id}">#${t.id}</span> ${esc(t.title)}
      <span class="muted t-s">${esc(t.status)}</span></div>`).join("") : `<div class="muted t-s">No commit of this version names a task.</div>`}</div>`;
}

// ---- clicks

$("#dpStages").addEventListener("click", (e) => {
  const vb = e.target.closest("[data-pl-version]");
  if (vb) { V.version = V.version === vb.dataset.plVersion ? null : vb.dataset.plVersion; sig = ""; renderPipeline(true).then(() => drawTasks(V.version)); return; }
  const fx = e.target.closest("[data-pl-fix]");
  if (fx) return (async () => {
    let r;
    try { r = await act("pipeline_fix", { run: +fx.dataset.plFix, start: !!fx.dataset.start }); } catch (err) { return; }
    toast(fx.dataset.start ? `Fix task #${r.item} added; a session starts on it` : `Fix task #${r.item} added`);
  })();
  const a = e.target.closest("[data-pl-hold],[data-pl-unhold],[data-pl-approve],[data-pl-stop],[data-pl-rerun],[data-pl-refail],[data-pl-rollback]");
  if (a) {
    const d = a.dataset;
    if (d.plHold !== undefined) return askReason(d.plHold ? `Hold before ${d.plHold}?` : "Hold every stage?",
      d.plHold ? "No new version enters this stage until you end the hold. A version you send on purpose still does."
        : "Nothing new starts in any stage. Work that runs goes on to its end.", "Hold",
      (reason) => doAct("pipeline_hold", { stage: d.plHold || null, reason }));
    if (d.plUnhold !== undefined) return doAct("pipeline_unhold", { stage: d.plUnhold || null });
    if (d.plApprove) return doAct("pipeline_approve", { stage: d.plApprove, rev: d.rev });
    if (d.plStop) return doAct("pipeline_stop", { run: +d.plStop });
    if (d.plRerun) return doAct("pipeline_rerun", { stage: d.plRerun, rev: d.rev });
    if (d.plRefail) return doAct("pipeline_rerun_failed", { run: +d.plRefail });
    if (d.plRollback) return askReason(`Put ${short(d.rev)} back on ${d.plRollback}?`,
      "Its deploy stage runs with this version, and that gate holds until you end the hold, so no newer version comes by itself.",
      "Roll back", (reason) => doAct("pipeline_rollback", { env: d.plRollback, rev: d.rev, reason }));
  }
  const t = e.target.closest("[data-pl-stage],[data-pl-filter],[data-pl-copy]");
  if (!t) return;
  if (t.dataset.plStage) { V.stage = t.dataset.plStage; V.run = null; runSig = ""; draw(); return refreshRun(true); }
  if (t.dataset.plFilter) { V.filter = t.dataset.plFilter; runSig = ""; return refreshRun(true); }
  if (t.dataset.plCopy && V.run && V.run.prompt) copyText(V.run.prompt).then(() => toast("Copied the prompt"));
});
$("#dpStages").addEventListener("scroll", (e) => { if (e.target.id === "plScroll") fade(); }, true);
window.addEventListener("resize", () => { if (V.shown && V.tab === "stages") fit(); });
