// The Operations view of Plan (#5438, design #5379): GET api/ops, the checks of the scope's project and of its deploy
// target (core.ops_view). Each check shows its state, its last result and its last runs as a strip; the open faults
// link their items; a proposed check has Accept and Reject (a reason). Run once, Pause and Resume act on a check.
// Add a check asks the plain questions of maxpm ops setup for the four forms, runs the check once in maxpm serve
// (op ops_trial, it keeps nothing), and keeps it when that run passed, or on a second press when it did not.
// Detections: GET api/ops/record (core.ops_detections) with the kind and time filters (#5510).
import { $, esc, toast, act, ago, left, getJson } from "../lib.js";
import { chip } from "../components/chip.js";
import { button } from "../components/button.js";
import { empty } from "../components/empty.js";
import { makeDialog } from "../components/dialog.js";
import { S, scope, planShown } from "../page.js";

let V = null, sig = "";

// The state of a check as a chip: a fault is the warning color, a healthy check the ready color.
const STATE = { fault: "c-p0", failing: "c-waiting", recovered: "c-in_progress", healthy: "c-ready", new: "c-p", paused: "c-p" };
const stateChip = (c) => chip(STATE[c.state] || "c-p", esc(c.state === "paused" && c.paused_until ? "paused · " + left(c.paused_until) : c.state));
const scopeOf = (x) => `${x.scope_kind}:${x.scope}`;

// The last runs, oldest first: one mark per run in the color of its status; its time and result are its hover.
function strip(c) {
  if (!c.runs.length) return `<span class="muted t-s">${c.form === "beat" ? "no beat yet" : "no run yet"}</span>`;
  return `<span class="ops-strip" role="img" aria-label="Last ${c.runs.length} runs">${c.runs.map(r =>
    `<span class="ops-run ops-${esc(r.status || "running")}" title="${esc((r.ended_at ? ago(r.ended_at) : "running") + ": " + (r.summary || r.status || ""))}"></span>`).join("")}</span>`;
}

function lastLine(c) {
  const r = c.last;
  if (!r) return "";
  return `<div class="t-s ops-last"><b>${esc(r.status || "running")}</b> ${esc(r.summary || "")}${r.ended_at ? ` <span class="muted">${ago(r.ended_at)}</span>` : ""}</div>`;
}

const faultLinks = (ids) => ids.map(id => `<span class="link" data-open="${id}">#${id}</span>`).join(" ");

function checkCard(c) {
  const paused = c.state === "paused";
  const pause = paused ? button("Resume", { data: { "ops-resume": c.name, scope: scopeOf(c) }, title: "Run it again from now" })
    : `<select class="ops-until" data-ops-until="${esc(scopeOf(c) + "/" + c.name)}" title="How long to pause">`
      + ["30m", "1h", "2h", "4h", "1d"].map(d => `<option${d === "1h" ? " selected" : ""}>${d}</option>`).join("") + `</select>`
      + button("Pause", { data: { "ops-pause": c.name, scope: scopeOf(c) }, title: "Pause it for planned work" });
  return `<div class="ops-check">
    <div class="ops-head"><b>${esc(c.name)}</b>${stateChip(c)}<span class="muted t-s">${esc(c.scope_kind === "target" ? "target " + c.scope : c.scope)}</span>
      ${c.faults.length ? `<span class="t-s">fault ${faultLinks(c.faults)}</span>` : ""}<span class="spacer"></span>${strip(c)}</div>
    <div class="muted t-s ops-sum">${esc(c.summary)}${c.repair ? " · has a repair" : ""}</div>
    ${lastLine(c)}
    <div class="actions">${button("Run once", { data: { "ops-run": c.name, scope: scopeOf(c) }, title: "Run it now" })}${pause}</div>
  </div>`;
}

function faultRow(f) {
  return `<div class="ops-fault t-m"><span class="link" data-open="${f.id}">#${f.id}</span> ${esc(f.short || f.title)}
    <span class="muted t-s">${esc(f.status.replace("_", " "))}${f.assignee ? " · " + esc(f.assignee) : ""} · ${ago(f.created_at)}</span></div>`;
}

function proposalCard(p) {
  const key = `${p.id}`;
  return `<div class="ops-check">
    <div class="ops-head"><b>${esc(p.name)}</b>${chip("c-human", p.changes ? "change" : "new")}<span class="muted t-s">${esc(p.scope_kind === "target" ? "target " + p.scope : p.scope)}</span></div>
    <div class="t-s">${esc(p.why)}</div>
    <div class="muted t-s ops-sum">${esc(p.summary)}${p.changes ? " · changes " + esc(p.changes.join(", ")) : ""}${p.runs_command ? " · runs a command outside the sandbox" : ""}${p.repair ? " · repair: " + esc(p.repair) : ""}</div>
    <div class="muted t-s">by ${esc(p.proposed_by || "?")} ${ago(p.proposed_at)}${p.item_id ? ` · <span class="link" data-open="${p.item_id}">#${p.item_id}</span>` : ""}</div>
    <div class="actions"><input id="opsrej-${key}" class="ops-why" placeholder="Why not? (for Reject)">
      ${button("Reject", { data: { "ops-reject": p.name, scope: scopeOf(p), pid: key }, title: "Do not keep it" })}
      ${button("Accept", { kind: "primary", data: { "ops-accept": p.name, scope: scopeOf(p) }, title: "Keep it as a check" })}</div>
  </div>`;
}

// One detection of the record (#5510): its kind, the check (and subject), how often and when, and its item.
const QUIET = new Set(["noise", "planned"]);
function detectionRow(r) {
  const seen = `${esc(r.status)} · ${r.count}× · ${ago(r.last_seen)}${r.count > 1 ? " · first " + ago(r.first_seen) : ""}`;
  return `<div class="ops-det">
    <div class="ops-head">${chip(QUIET.has(r.kind) ? "c-p" : "c-waiting", esc(r.kind || "?"))}<b>${esc(r.check_name)}${r.subject ? ":" + esc(r.subject) : ""}</b>
      <span class="muted t-s">${seen}</span><span class="spacer"></span>
      ${r.item_id ? `<span class="link t-s" data-open="${r.item_id}">#${r.item_id}</span>` : `<span class="muted t-s">no item</span>`}</div>
    <div class="muted t-s ops-sum">${esc(r.summary || "")}</div>
  </div>`;
}

function drawRecord(R) {
  const sel = $("#opsRecKind"), was = sel.value;
  sel.innerHTML = `<option value="">All kinds</option>` + R.all_kinds.map(k =>
    `<option value="${esc(k)}">${esc(k)}${R.kinds[k] ? ` (${R.kinds[k]})` : ""}</option>`).join("");
  sel.value = was;
  $("#opsRecord").innerHTML = R.rows.map(detectionRow).join("") || empty("No detections in this time.");
}

export async function renderOps(force) {
  if (!planShown("ops")) return;
  const q = new URLSearchParams(); if (scope.project) q.set("project", scope.project);
  const rq = new URLSearchParams(q);
  if ($("#opsRecKind").value) rq.set("kind", $("#opsRecKind").value);
  if ($("#opsRecSince").value) rq.set("since", $("#opsRecSince").value);
  let R;
  try { [V, R] = await Promise.all([getJson("api/ops?" + q), getJson("api/ops/record?" + rq)]); }
  catch (e) { $("#opsNote").textContent = e.message; return; }
  const box = $("#plan-ops");
  if (!force && box.contains(document.activeElement) && document.activeElement.tagName === "INPUT") return;  // keep a reason they type
  const s = JSON.stringify([V, R, scope.project]);
  if (s === sig && !force) return;
  sig = s;
  const C = V.checks, where = scope.project ? `${esc(scope.project)}${V.scopes.length > 1 ? " and its target " + esc(V.scopes[1].name) : ""}` : "all projects";
  const bad = C.filter(c => ["fault", "failing"].includes(c.state)).length;
  $("#opsNote").innerHTML = C.length ? `${C.length} check${C.length === 1 ? "" : "s"} of ${where}; ${bad ? `<b>${bad} failing</b>` : "none failing"}. maxpm serve runs them.`
    : `No checks of ${where} yet. Add one here, or in a terminal: maxpm ops setup${scope.project ? " " + esc(scope.project) : ""}.`;
  $("#opsFaultsBox").classList.toggle("hidden", !V.faults.length);
  $("#opsFaults").innerHTML = V.faults.map(faultRow).join("");
  $("#opsPropBox").classList.toggle("hidden", !V.proposals.length);
  $("#opsProposals").innerHTML = V.proposals.map(proposalCard).join("");
  $("#opsChecks").innerHTML = C.map(checkCard).join("") || empty("No checks.");
  drawRecord(R);
}
$("#opsRecKind").addEventListener("change", () => renderOps(true));
$("#opsRecSince").addEventListener("change", () => renderOps(true));

// Add a check: the scopes, the four forms and their plain questions come from api/ops.
const dlg = makeDialog($("#opsDlg"));
let anyway = false;
function scopesForAdd() {
  if (scope.project) return V.scopes;
  return (S.projects || []).map(p => ({ kind: "project", name: p.name })).concat((S.targets || []).map(t => ({ kind: "target", name: t.name })));
}
function drawFields() {
  const f = V.forms.find(x => x.form === $("#opsKind").value);
  $("#opsFields").innerHTML = f.questions.map(q => `<label>${esc(q.ask.replace(/\s*\(Enter:[^)]*\)/, ""))}<input data-ops-field="${esc(q.field)}" value="${esc(q.default || "")}"></label>`).join("");
  $("#opsName").placeholder = f.form === "beat" ? "the job's name (needed)" : "from the address or command";
  $("#opsEvery").placeholder = f.form === "beat" ? "how often to look: 5m" : "5m";
  resetTrial();
}
function resetTrial() { anyway = false; $("#opsTrial").textContent = ""; $("#opsSave").textContent = "Add check"; }
function openAdd() {
  if (!V) return;
  $("#opsScope").innerHTML = scopesForAdd().map(x => `<option value="${esc(x.kind + ":" + x.name)}">${esc((x.kind === "target" ? "target " : "") + x.name)}</option>`).join("");
  $("#opsKind").innerHTML = V.forms.map(f => `<option value="${esc(f.form)}">${esc(f.label)}</option>`).join("");
  for (const id of ["#opsName", "#opsEvery", "#opsRunbook"]) $(id).value = "";
  $("#opsSeverity").value = "item";
  drawFields(); dlg.open(); $("#opsScope").focus();
}
function formArgs() {
  const fields = {};
  document.querySelectorAll("#opsFields [data-ops-field]").forEach(i => { if (i.value.trim()) fields[i.dataset.opsField] = i.value.trim(); });
  const every = $("#opsEvery").value.trim(), runbook = $("#opsRunbook").value.trim();
  if (every) fields.every = every;
  if (runbook) fields.runbook = runbook;
  fields.severity = $("#opsSeverity").value;
  return { scope: $("#opsScope").value, form: $("#opsKind").value, name: $("#opsName").value.trim() || undefined, fields };
}
// One run in maxpm serve; it keeps nothing. Returns true when it passed (a beat has no run: it waits for the job).
async function trial() {
  const a = formArgs();
  if (a.form === "beat") return true;
  $("#opsTrial").textContent = "Running it once …";
  try {
    const r = await act("ops_trial", a);
    const res = r.result || {};
    const ok = res.status === "ok";
    $("#opsTrial").textContent = `${ok ? "Passed" : "Did not pass"}: ${res.status || "?"}${res.summary ? " · " + res.summary : ""}`;
    return ok;
  } catch (e) { $("#opsTrial").textContent = e.message; return false; }
}
$("#opsAdd").onclick = openAdd;
$("#opsClose").onclick = () => dlg.close();
$("#opsKind").addEventListener("change", drawFields);
$("#opsFields").addEventListener("input", resetTrial);
$("#opsTry").onclick = () => { trial(); };
$("#opsSave").onclick = async () => {
  if (!anyway && !(await trial())) { anyway = true; $("#opsSave").textContent = "Add anyway"; return; }
  try {
    const r = await act("ops_add", formArgs());
    dlg.close(); toast(`Check ${r.check.scope}/${r.check.name} added: maxpm serve runs it from now on`); renderOps(true);
  } catch (e) { /* toast shown */ }
};

// A click on this view's controls; true when it was one.
export function onClick(t) {
  const d = t.dataset;
  if (d.opsRun) return run(() => act("ops_run", { scope: d.scope, name: d.opsRun })
    .then(() => { toast(`${d.opsRun} runs now`); setTimeout(() => renderOps(true), 3000); }).catch(() => {}));
  if (d.opsPause) return run(() => {
    const sel = document.querySelector(`[data-ops-until="${CSS.escape(d.scope + "/" + d.opsPause)}"]`);
    return act("ops_pause", { scope: d.scope, name: d.opsPause, until: sel ? sel.value : "1h" })
      .then(() => { toast(`${d.opsPause} paused`); renderOps(true); }).catch(() => {});
  });
  if (d.opsResume) return run(() => act("ops_pause", { scope: d.scope, name: d.opsResume, resume: true })
    .then(() => { toast(`${d.opsResume} runs again`); renderOps(true); }).catch(() => {}));
  if (d.opsAccept) return run(() => act("ops_accept", { scope: d.scope, name: d.opsAccept })
    .then(() => { toast(`${d.opsAccept} is a check now`); renderOps(true); }).catch(() => {}));
  if (d.opsReject) return run(() => {
    const why = document.getElementById("opsrej-" + d.pid), note = why ? why.value.trim() : "";
    if (!note) return toast("Say why not, in the box beside Reject", true);
    return act("ops_reject", { scope: d.scope, name: d.opsReject, note }).then(() => { toast(`${d.opsReject} rejected`); renderOps(true); }).catch(() => {});
  });
  return false;
}
const run = (f) => { f(); return true; };
