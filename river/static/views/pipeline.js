// The tabs Stages and Versions of a pipeline's page in Deploy (#6116 in Plan, moved to Deploy by #6136; canvas
// pipeline-mockup v2 of #6110, mark 2026-11-18: "kind of like code pipelines"). One pipeline at a time, the one that
// views/deploy.js names (showPipeline): its stages left to right (they get narrow to fit the width, then the row
// scrolls with a faded edge), the transitions between them, the detail of the chosen stage (its steps on a time line, its
// last lines, its tests failed first with the prompt that analyzes the failures), where each version runs, and the
// versions in each stage. A deploy target with no pipeline of its own shows as its default pipeline, computed from
// its releases. Reads: GET api/pipeline/<name>, api/pipeline-run/<id> (river/routes/pipeline.py); the actions (hold,
// approve, roll back, run again) are #6117. The arrow between two stages is a transition (#6139; canvas
// deploy-tab-mockup picture 4): it shows its mode, its state, who set it, until when and the waiting version, and a
// click opens its controls (Approve, Block until, Delay, Disable, Let one version pass, Freeze, the mode and window).
import { $, esc, getJson, ago, left, copyText, toast, act } from "../lib.js";
import { button, iconButton } from "../components/button.js";
import { chip } from "../components/chip.js";
import { empty } from "../components/empty.js";
import { makeDialog } from "../components/dialog.js";
import { field, input, select, textarea, actions } from "../components/form.js";

const V = { shown: false, name: null, data: null, run: null, stage: null, arrow: null, tab: "stages", filter: "failed" };
let sig = "", runSig = "";
const WORD = { passed: "passed", running: "running", failed: "failed", queued: "waiting", skipped: "skipped",
  cancelled: "cancelled", stopped: "stopped", error: "error", none: "no run yet" };
// The mark of a transition: its mode when no control holds it, else the control (mark, word, chip color).
const MODE_MARK = { auto: ["→", "auto", ""], yes: ["✋", "yes", "c-human"], delay: ["⏱", "delay", ""],
  release: ["📦", "release", "c-human"], forge: ["⎇", "forge", ""] };
const STATE_MARK = { disabled: ["⏸", "disabled", "c-waiting"], blocked: ["⛔", "blocked", "c-warn"],
  fault: ["⛔", "fault", "c-warn"], frozen: ["❄", "frozen", "c-warn"], window: ["🕒", "closed", "c-human"],
  delayed: ["⏱", "delayed", "c-waiting"] };
const NEEDS_YES = ["yes", "release", "delay"];

const st = (s, word) => `<span class="pl-st ${esc(s || "none")}">● ${esc(word || WORD[s] || s || "no run yet")}</span>`;
const short = (rev) => rev && /^[0-9a-f]{12,}$/.test(rev) ? rev.slice(0, 7) : rev || "";
const secs = (n) => n == null ? "" : n < 90 ? `${Math.round(n)}s` : n < 5400 ? `${Math.round(n / 60)}m` : `${Math.round(n / 3600)}h`;

// The pipeline and tab that the Deploy tab shows (views/deploy.js): another pipeline starts with no chosen stage.
export function showPipeline(name, tab) {
  if (name !== V.name) { V.stage = null; V.arrow = null; V.run = null; V.version = null; V.data = null; sig = ""; runSig = ""; }
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
  if (!V.data.stages.some(s => s.name === V.arrow)) V.arrow = null;
  const s = JSON.stringify([V.name, V.data, V.stage, V.arrow, V.tab, V.filter]);
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
    { kind: "card", cls: "pl-stage" + (s.env ? " pl-env" : ""), pressed: s.name === V.stage && !V.arrow, data: { "pl-stage": s.name },
      title: s.run ? "Runs: " + s.run : s.workflow ? "Forge workflow: " + s.workflow : s.name });
}

// The mark and the words of a transition: [mark, short word, chip color].
function arrowMark(t) {
  if (t.state !== "open") return STATE_MARK[t.state] || ["⛔", t.state, "c-warn"];
  const m = MODE_MARK[t.mode] || MODE_MARK.auto;
  if (t.mode === "yes") return [m[0], t.arg && t.arg !== "manager" ? `${t.arg}'s yes` : t.arg ? "manager" : "a yes", m[2]];
  if (t.mode === "delay") return [m[0], t.arg, m[2]];
  return m;
}
// A transition with nothing to say: automatic, open, no time rule. The first stage's arrow shows only when it has one.
const plainArrow = (t) => t.mode === "auto" && t.state === "open" && !t.window && !t.fault_rule && !(t.freezes || []).length;

function arrowEl(s, i) {
  const t = s.transition;
  if (!t || (!i && plainArrow(t))) return "";
  const [m, word, cls] = arrowMark(t);
  const w = (t.waiting || [])[0];
  const by = t.state !== "open" ? [t.set_by, t.until ? (t.until.startsWith("#") ? `until ${t.until} closes` : `${left(t.until)} left`) : ""]
    .filter(Boolean).join(" · ") : "";
  return `<div class="pl-gate">${button(`${chip(cls, `${esc(m)}<span class="pl-long"> ${esc(word)}</span>`)}
    ${by ? `<span class="pl-anote pl-long">${esc(by)}</span>` : ""}${w ? `<span class="pl-anote pl-ver pl-long">${esc(short(w.rev))} waits</span>` : ""}`,
    { kind: "card", cls: "pl-arrow", pressed: V.arrow === s.name, data: { "pl-arrow": s.name },
      title: `Transition into ${s.name}` })}</div>`;
}

function stagesView(D) {
  if (!D.stages.length) return empty(`Pipeline ${esc(D.name)} has no stages yet: maxpm pipeline stage ${esc(D.name)} &lt;stage&gt; --kind &lt;kind&gt;`);
  const row = D.stages.map((s, i) => arrowEl(s, i) + stageCard(s)).join("");
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

// ---- the detail of the chosen transition: each control with who set it and until when, and its buttons

const when = (iso) => iso ? new Date(iso).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "";
const setBy = (who, at) => esc(who || "a person") + (at ? " · " + esc(ago(at)) : "");

function arrowDetail(s) {
  const D = V.data, t = s.transition, i = D.stages.indexOf(s), from = i > 0 ? D.stages[i - 1].name : "start";
  const [m, word, cls] = arrowMark(t);
  const row = (what, now, by) => `<tr><td><b>${esc(what)}</b></td><td>${now}</td><td class="t-s muted">${by}</td></tr>`;
  const rows = [row("Mode", esc(t.text), t.mode_by ? setBy(t.mode_by, t.mode_at) : t.fixed ? "the deploy target" : "the start: automatic")];
  if (t.window) rows.push(row("Time window", `${esc(t.window.text)} ${st(t.window.open ? "passed" : "queued", t.window.open ? "open now" : "closed now")}`,
    setBy(t.window.by, t.window.at) + (t.window.change ? ` · ${t.window.open ? "closes" : "opens"} ${esc(when(t.window.change))}` : "")));
  for (const f of t.freezes || []) rows.push(row("Freeze", `${esc(when(f.from))} to ${esc(when(f.to))}: ${esc(f.reason)}${f.now ? " " + st("failed", "now") : ""}`, setBy(f.by)));
  if (t.fault_rule) rows.push(row("Fault rule", t.fault ? `blocked while <span class="link" data-open="${t.fault.id}">#${t.fault.id}</span> is open`
    : "blocked while a fault of the pipeline is open; none is open", setBy(t.rule_by)));
  if (t.block) rows.push(row("Block", `<span class="c-warn">${esc(t.block.reason)}</span>`, setBy(t.block.by, t.block.at) + " · until "
    + (t.block.until ? esc(when(t.block.until)) : t.block.item ? `<span class="link" data-open="${t.block.item}">#${t.block.item}</span> closes` : "someone unblocks it")));
  if (t.delay) rows.push(row("Delay", esc(t.delay.reason), setBy(t.delay.by) + " · until " + esc(when(t.delay.until))));
  if (t.disabled) rows.push(row("Disabled", esc(t.disabled.reason), setBy(t.disabled.by, t.disabled.at) + " · until someone enables it"));
  const waits = (t.waiting || []).map(w => `<div class="t-m"><span class="pl-ver">${esc(short(w.rev))}</span> waits since ${esc(ago(w.since))}`
    + (w.approved_by ? ` · yes from ${esc(w.approved_by)}` : "") + (w.passed_by ? ` · let pass by ${esc(w.passed_by)}` : "")
    + (w.note ? `<div class="muted t-s">${esc(w.note)}</div>` : "") + `</div>`).join("");
  return `<h3>The transition ${esc(from)} → ${esc(s.name)} ${chip(cls, `${esc(m)} ${esc(word)}`)}</h3>
    <div class="t-m">${t.state === "open" ? "Open: " + esc(t.text) : esc(t.state_text)}</div>
    ${waits || `<div class="muted t-s">No version waits here now.</div>`}
    <div class="pl-table"><table class="pl-ctl"><tr><th>Control</th><th>Now</th><th>Set by, until</th></tr>${rows.join("")}</table></div>
    ${arrowActions(s, t)}
    <div class="muted t-s">The tests and checks run inside the stages; a transition only lets versions in. Each change is a line in the audit log with who made it, why and until when.</div>`;
}

function arrowActions(s, t) {
  if (V.data.default || t.fixed) return `<div class="muted t-s">A deploy target's own steps decide this. Give it a pipeline of its own to control its transitions.</div>`;
  const w = (t.waiting || [])[0], b = [], n = s.name;
  if (w && NEEDS_YES.includes(t.mode) && !w.approved_by)
    b.push(button(`Approve ${esc(short(w.rev))}`, { kind: "primary", data: { "pl-approve": n, rev: w.rev }, title: "Give its yes" }));
  b.push(t.block ? button("Unblock", { data: { "pl-unblock": n }, title: "End the block" })
    : button("Block until…", { kind: "warn", data: { "pl-block": n }, title: "Stop versions here" }));
  b.push(t.delay ? button("End the delay", { data: { "pl-undelay": n }, title: "End the delay" })
    : button("Delay…", { data: { "pl-delay": n }, title: "Hold for a time" }));
  b.push(t.disabled ? button("Enable", { data: { "pl-enable": n }, title: "Let versions pass" })
    : button("Disable…", { data: { "pl-disable": n }, title: "Until enabled" }));
  if (w && t.state !== "open") b.push(button(`Let ${esc(short(w.rev))} pass once…`, { data: { "pl-pass": n, rev: w.rev }, title: "Past every control" }));
  b.push(button("Freeze…", { data: { "pl-freeze": n }, title: "Stop for a period" }));
  if ((t.freezes || []).length) b.push(button("Remove the freezes", { data: { "pl-unfreeze": n }, title: "Remove the freezes" }));
  b.push(button("Change the mode or the window…", { data: { "pl-change": n }, title: "The standing rules" }));
  return `<div class="pl-bar">${b.join("")}</div>`;
}

function detail() {
  const a = V.arrow && V.data && V.data.stages.find(s => s.name === V.arrow);
  if (a) return arrowDetail(a);
  const s = chosen(); if (!s) return "";
  const r = V.run && (V.run.id == null || (s.last_run && V.run.id === s.last_run.id)) ? V.run : s.last_run;
  const head = `<h3>${esc(s.name)}${r && r.version ? ` · <span class="pl-ver">${esc(short(r.version))}</span>` : ""}</h3>`;
  if (!r) return head + `<div class="muted t-s">No run of this stage yet.${s.run ? " It runs: " + esc(s.run) : ""}</div>`;
  const when = r.started_at ? `started ${esc(ago(r.started_at))}` : r.queued_at ? `waiting since ${esc(ago(r.queued_at))}` : "";
  const item = (r.item ? ` · <span class="link" data-open="${r.item}">#${r.item}</span>` : "")
    + (r.fault_item ? ` · fault <span class="link" data-open="${r.fault_item}">#${r.fault_item}</span>` : "");
  return head + `<div class="pl-bar">${st(r.state)}<span class="muted t-s">${when}${r.ended_at ? ` · ended ${esc(ago(r.ended_at))}` : ""}${item}</span></div>`
    + (r.gate_note && r.state === "queued" ? `<div class="t-s">At the transition: ${esc(r.gate_note)}</div>` : "")
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
// its own actions on Releases. The controls before the stage are on its transition (#6139).
function stageActions(s, r) {
  if (V.data.default) return "";
  const rev = r && r.version, b = [];
  if (s.transition) b.push(button("The transition before it", { data: { "pl-arrow": s.name }, title: "Its controls" }));
  if (r && ["running", "queued"].includes(r.state) && r.id != null) b.push(button("Stop", { kind: "warn", data: { "pl-stop": r.id }, title: "Stop this run" }));
  if (r && rev && !["running", "queued"].includes(r.state)) b.push(button("Run again", { data: { "pl-rerun": s.name, rev }, title: "Run this stage again" }));
  const bad = r && r.tests ? (r.tests.failed || 0) + (r.tests.error || 0) : 0;
  if (bad && r.id != null && !["running", "queued"].includes(r.state))
    b.push(button(`Run the ${bad} failed again`, { data: { "pl-refail": r.id }, title: "Only the failed tests" }));
  return `<div class="pl-bar">${b.join("")}</div>`;
}

// The reason of a hold or a roll back: a small dialog (components/dialog.js). The Emergency tab asks with it too.
export function askReason(title, text, go, then) {
  askForm(title, text, go, [], (v) => then(v.why));
}

// The same dialog with fields above the reason: fields [{id, label, value, hint, options}] (options: a select).
// needWhy false: the reason may stay empty. then(values) gets {why, <id>: value}.
let askDlg = null, askBox = null;
export function askForm(title, text, go, fields, then, needWhy = true) {
  if (!askDlg) {
    const d = document.createElement("div");
    d.id = "plAskDlg"; d.className = "hidden";
    d.innerHTML = '<div class="box"></div>';
    document.body.appendChild(d);
    askBox = d.firstElementChild;
    askDlg = makeDialog(d);
  }
  const ctl = (f) => f.options ? select({ id: "plF-" + f.id }, f.options, f.value)
    : input({ id: "plF-" + f.id, value: f.value || "", placeholder: f.hint || null });
  askBox.innerHTML = `<div class="dlg-head"><h2>${esc(title)}</h2>${iconButton("✕", "Close", { data: { "pl-askclose": "1" } })}</div>
    <p class="t-m">${esc(text)}</p>
    <div class="form">${fields.map(f => field(esc(f.label), ctl(f))).join("")}${field(needWhy ? "Why?" : "Why? (optional)", textarea({ id: "plWhy", rows: 3 }, ""))}</div>
    ${actions(button("Cancel", { data: { "pl-askclose": "1" } }) + button(go, { kind: "primary", data: { "pl-askgo": "1" } }))}`;
  askDlg.open();
  askBox.querySelector(fields.length ? "#plF-" + fields[0].id : "#plWhy").focus();
  askBox.onclick = async (e) => {
    if (e.target.closest("[data-pl-askclose]")) return askDlg.close();
    if (!e.target.closest("[data-pl-askgo]")) return;
    const v = { why: (askBox.querySelector("#plWhy").value || "").trim() };
    for (const f of fields) v[f.id] = (askBox.querySelector("#plF-" + f.id).value || "").trim();
    if (needWhy && !v.why) return toast("Say why", true);
    askDlg.close();
    await then(v);
  };
}

// The buttons of a transition (#6139): each one asks why (the audit log keeps it) and what it needs.
function arrowAct(d) {
  const n = d.plBlock || d.plUnblock || d.plDelay || d.plUndelay || d.plDisable || d.plEnable || d.plPass || d.plFreeze
    || d.plUnfreeze || d.plChange;
  const s = V.data.stages.find(x => x.name === n), t = s && s.transition;
  if (!t) return;
  const into = `the transition into ${n}`;
  if (d.plBlock) return askForm(`Block ${into}?`, "No version passes until the time you give, until a task closes, or until someone unblocks it. A version you let pass on purpose still does.",
    "Block", [{ id: "until", label: "Until", hint: "2h, fri 17:00, #<task>, or empty" }],
    (v) => doAct("pipeline_block", { stage: n, reason: v.why, ...(v.until.startsWith("#") ? { until_done: +v.until.slice(1) } : v.until ? { until: v.until } : {}) }));
  if (d.plUnblock) return doAct("pipeline_unblock", { stage: n });
  if (d.plDelay) return askForm(`Delay ${into}?`, "No version passes before this time. Then the transition works as before.", "Delay",
    [{ id: "until", label: "Until", hint: "2h, mon 09:00" }], (v) => v.until ? doAct("pipeline_delay", { stage: n, until: v.until, reason: v.why }) : toast("Say until when", true));
  if (d.plUndelay) return doAct("pipeline_delay", { stage: n, until: "none" });
  if (d.plDisable) return askForm(`Disable ${into}?`, "No version passes until a person or the manager enables it.", "Disable", [],
    (v) => doAct("pipeline_disable", { stage: n, reason: v.why }));
  if (d.plEnable) return doAct("pipeline_enable", { stage: n });
  if (d.plPass) return askForm(`Let ${short(d.rev)} pass once?`, "This one version passes every control of the transition now: the block, the fault rule, a freeze, the window, a delay. A yes that only a named person gives still waits for that person.",
    "Let it pass", [], (v) => doAct("pipeline_pass", { stage: n, rev: d.rev, reason: v.why }));
  if (d.plFreeze) return askForm(`Freeze ${into}?`, "No version passes in this period. To freeze every transition of the pipeline, run maxpm pipeline freeze with no stage.",
    "Freeze", [{ id: "from", label: "From (empty: now)", hint: "wed 18:00, 2026-11-26T00:00" }, { id: "to", label: "To", hint: "mon 08:00, 3d" }],
    (v) => v.to ? doAct("pipeline_freeze", { stage: n, from: v.from || null, to: v.to, reason: v.why }) : toast("Say until when", true));
  if (d.plUnfreeze) return doAct("pipeline_freeze", { stage: n, clear: true });
  return askForm(`Change ${into}`, "The mode is how a version passes when nothing holds it. The window says when versions may pass (for example mon-thu 09:00-17:00 America/Los_Angeles); empty: at any time.",
    "Save", [{ id: "mode", label: "Mode", value: t.words, hint: "auto, yes mark, delay 10m, release" },
      { id: "window", label: "Time window", value: t.window ? t.window.text : "", hint: "mon-thu 09:00-17:00" },
      { id: "fault", label: "Fault rule", options: [["on", "Block it"], ["off", "Let versions pass"]], value: t.fault_rule ? "on" : "off" }],
    (v) => {
      const args = { stage: n, reason: v.why || null };
      if (v.mode && v.mode !== t.words) args.mode = v.mode;
      if (v.window !== (t.window ? t.window.text : "")) args.window = v.window || "none";
      if ((v.fault === "on") !== !!t.fault_rule) args.fault_rule = v.fault === "on";
      if (Object.keys(args).length === 2) return toast("Nothing changed");
      return doAct("pipeline_transition", args);
    }, false);
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
  const tr = e.target.closest("[data-pl-block],[data-pl-unblock],[data-pl-delay],[data-pl-undelay],[data-pl-disable],[data-pl-enable],[data-pl-pass],[data-pl-freeze],[data-pl-unfreeze],[data-pl-change]");
  if (tr) return arrowAct(tr.dataset);
  const a = e.target.closest("[data-pl-hold],[data-pl-unhold],[data-pl-approve],[data-pl-stop],[data-pl-rerun],[data-pl-refail],[data-pl-rollback]");
  if (a) {
    const d = a.dataset;
    if (d.plHold !== undefined) return askReason("Hold every stage?",
      "Nothing new starts in any stage. Work that runs goes on to its end.", "Hold",
      (reason) => doAct("pipeline_hold", { stage: null, reason }));
    if (d.plUnhold !== undefined) return doAct("pipeline_unhold", { stage: d.plUnhold || null });
    if (d.plApprove) return doAct("pipeline_approve", { stage: d.plApprove, rev: d.rev });
    if (d.plStop) return doAct("pipeline_stop", { run: +d.plStop });
    if (d.plRerun) return doAct("pipeline_rerun", { stage: d.plRerun, rev: d.rev });
    if (d.plRefail) return doAct("pipeline_rerun_failed", { run: +d.plRefail });
    if (d.plRollback) return askReason(`Put ${short(d.rev)} back on ${d.plRollback}?`,
      "Its deploy stage runs with this version, and the transition into it is blocked until you unblock it, so no newer version comes by itself.",
      "Roll back", (reason) => doAct("pipeline_rollback", { env: d.plRollback, rev: d.rev, reason }));
  }
  const t = e.target.closest("[data-pl-stage],[data-pl-arrow],[data-pl-filter],[data-pl-copy]");
  if (!t) return;
  if (t.dataset.plArrow) { V.arrow = t.dataset.plArrow; sig = ""; draw(); return; }
  if (t.dataset.plStage) { V.stage = t.dataset.plStage; V.arrow = null; V.run = null; runSig = ""; draw(); return refreshRun(true); }
  if (t.dataset.plFilter) { V.filter = t.dataset.plFilter; runSig = ""; return refreshRun(true); }
  if (t.dataset.plCopy && V.run && V.run.prompt) copyText(V.run.prompt).then(() => toast("Copied the prompt"));
});
$("#dpStages").addEventListener("scroll", (e) => { if (e.target.id === "plScroll") fade(); }, true);
window.addEventListener("resize", () => { if (V.shown && V.tab === "stages") fit(); });
