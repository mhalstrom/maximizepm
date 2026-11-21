// The Operations tab (#6629, #6672; canvas operations-tab, layout mark 2026-11-21: "projects, then areas, then
// triggers; playbooks at the project level, each showing the triggers that use it"). Each project also shows its open
// faults and proposed checks; the detections record sits below with kind and time filters. Each trigger shows its
// condition (time, state, event), how its events arrive (poll, push: its address and a button that makes a new secret
// and shows it once), its playbook, its last event with the item it opened, and for state triggers its run strip. A
// trigger opens a dialog with its playbook, its events, and Run now / Pause / Resume. Add and edit forms for areas,
// triggers (including state checks with Try once via ops_trial and ops_add) and playbooks (river/routes/operations.py).
// Reads GET api/operations and GET api/ops/record in parallel, and GET api/operations/trigger for one trigger's dialog.
import { $, esc, getJson, ago, left, act, toast, store, copyText } from "../lib.js";
import { button, iconButton } from "../components/button.js";
import { chip } from "../components/chip.js";
import { empty } from "../components/empty.js";
import { makeDialog } from "../components/dialog.js";
import { field, input, select, textarea, two, actions } from "../components/form.js";
import { OPEN_MARK } from "../components/rowOpen.js";
import { tab, nav } from "../page.js";

let O = null, sig = "", dlg = null, box = null, form = null, anyway = false;
const PROJECT_KEY = "river.opsProject";

const COND = { time: "time", state: "state", event: "event" };
const STATE = { fault: "c-p0", failing: "c-waiting", recovered: "c-in_progress", healthy: "c-ready", new: "c-p", paused: "c-p" };
const QUIET = new Set(["noise", "planned"]);
const keyOf = (t) => `${t.scope_kind}:${t.scope}`;
const projectOf = (name) => O && O.projects.find(p => p.name === name);
const plural = (n, one, many = one + "s") => `${n} ${n === 1 ? one : many}`;
// An event's link comes from outside (untrusted data): only a web address becomes a link.
const safeLink = (u) => typeof u === "string" && /^https?:\/\//i.test(u);

export function openOperations(project) {
  const name = project || "";
  store(PROJECT_KEY, name);
  const sel = $("#opProject");
  if (sel && [...sel.options].some(o => o.value === name)) sel.value = name;
  return nav.setTab("operations");
}

// ---- reading and drawing

export async function renderOperations(force) {
  if (tab !== "operations") return;
  const wantProj = O ? $("#opProject").value : (store(PROJECT_KEY) || "");
  const rq = new URLSearchParams();
  if (wantProj) rq.set("project", wantProj);
  if ($("#opRecKind").value) rq.set("kind", $("#opRecKind").value);
  if ($("#opRecSince").value) rq.set("since", $("#opRecSince").value);
  let d, R;
  try {
    [d, R] = await Promise.all([
      getJson("api/operations"),
      getJson("api/ops/record?" + rq).catch(() => wantProj ? getJson("api/ops/record") : { rows: [], kinds: {}, all_kinds: [] }),
    ]);
  } catch (e) { $("#opBody").innerHTML = empty(esc(e.message)); return; }
  O = d;
  drawProjects();
  if (!force && $("#tab-operations").contains(document.activeElement) && document.activeElement.tagName === "INPUT") return;
  const curProj = $("#opProject").value;
  const s = JSON.stringify([d, R, curProj, Math.floor(Date.now() / 60000)]);  // "3m ago" changes each minute
  if (s === sig && !force) return;
  sig = s;
  $("#opBody").innerHTML = bodyHtml(d);
  drawRecord(R, !curProj);
}

// The project choice of the bar: all projects, or one; this browser keeps it.
function drawProjects() {
  const sel = $("#opProject"), want = sel.value || store(PROJECT_KEY) || "";
  const opts = [["", "All projects"], ...O.projects.map(p => [p.name, p.name])];
  const html = opts.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join("");
  if (sel.dataset.sig !== html) { sel.innerHTML = html; sel.dataset.sig = html; }
  sel.value = opts.some(([v]) => v === want) ? want : "";
}

// A project that has operations: an area with a trigger, a custom area or playbook, an open fault, or a proposed check.
const used = (p) => p.areas.some(a => a.triggers.length || a.created_by) || p.playbooks.some(b => !b.is_default)
  || (p.faults && p.faults.length) || (p.proposals && p.proposals.length);

function bodyHtml(d) {
  const only = $("#opProject").value;
  const shown = d.projects.filter(p => only ? p.name === only : used(p));
  const rest = only ? [] : d.projects.filter(p => !used(p)).map(p => p.name);
  const parts = shown.map(projectHtml);
  if (d.unplaced.length) parts.push(`<div class="panel"><h2>Triggers with no area</h2>
    <div class="op-list">${d.unplaced.map(triggerRow).join("")}</div></div>`);
  if (!parts.length) parts.push(empty("No triggers yet. A trigger starts an agent when a time comes, a check changes "
    + "state, or an outside system reports an event."));
  if (rest.length) parts.push(`<div class="muted t-s op-none">No operations yet: ${rest.map(esc).join(", ")}.</div>`);
  return parts.join("");
}

function faultRow(f) {
  return `<div class="op-fault t-m"><span class="link" data-open="${f.id}">#${f.id}</span> ${esc(f.short || f.title)}
    <span class="muted t-s">${esc(f.status.replace("_", " "))}${f.assignee ? " · " + esc(f.assignee) : ""} · ${ago(f.created_at)}</span></div>`;
}

function proposalCard(pr) {
  const key = `${pr.id}`;
  return `<div class="op-prop-card">
    <div class="op-head"><b>${esc(pr.name)}</b>${chip("c-human", pr.changes ? "change" : "new")}<span class="muted t-s">${esc(pr.scope_kind === "target" ? "target " + pr.scope : pr.scope)}</span></div>
    <div class="t-s op-line">${esc(pr.why)}</div>
    <div class="muted t-s op-line">${esc(pr.summary)}${pr.changes ? " · changes " + esc(pr.changes.join(", ")) : ""}${pr.runs_command ? " · runs a command outside the sandbox" : ""}${pr.repair ? " · repair: " + esc(pr.repair) : ""}</div>
    <div class="muted t-s op-line">by ${esc(pr.proposed_by || "?")} ${ago(pr.proposed_at)}${pr.item_id ? ` · <span class="link" data-open="${pr.item_id}">#${pr.item_id}</span>` : ""}</div>
    <div class="actions op-prop-act"><input id="oprej-${key}" class="op-why" placeholder="Why not? (for Reject)">
      ${button("Reject", { data: { "op-check-reject": pr.name, scope: keyOf(pr), pid: key }, title: "Do not keep it" })}
      ${button("Accept", { kind: "primary", data: { "op-check-accept": pr.name, scope: keyOf(pr) }, title: "Keep as a check" })}</div>
  </div>`;
}

function projectHtml(p) {
  const faults = p.faults && p.faults.length
    ? `<h3 class="op-h op-h-warn">Open faults</h3><div class="op-list">${p.faults.map(faultRow).join("")}</div>` : "";
  const props = p.proposals && p.proposals.length
    ? `<h3 class="op-h">Proposed checks</h3><div class="op-list">${p.proposals.map(proposalCard).join("")}</div>` : "";
  const areas = p.areas.length ? p.areas.map(a => areaHtml(p, a)).join("")
    : `<div class="muted t-m">No areas yet.</div>`;
  const books = p.playbooks.length ? p.playbooks.map(b => bookRow(p, b)).join("") : `<div class="muted t-m">No playbooks yet.</div>`;
  return `<div class="panel op-project"><h2>${esc(p.name)}</h2>
    ${faults}${props}${areas}
    <h3 class="op-h">Playbooks</h3><div class="op-list">${books}</div></div>`;
}

function areaHtml(p, a) {
  const rows = a.triggers.length ? a.triggers.map(triggerRow).join("") : `<div class="muted t-m">No triggers.</div>`;
  return `<div class="op-area">
    <div class="op-head"><b>${esc(a.name)}</b>${a.target ? chip("c-p", "target " + esc(a.target)) : ""}
      ${a.place ? `<span class="muted t-s mono">${esc(a.place)}</span>` : ""}<span class="spacer"></span>
      ${button("Edit", { data: { "op-area-edit": a.name, project: p.name }, title: "Edit area" })}</div>
    ${a.description ? `<div class="muted t-s op-desc">${esc(a.description)}</div>` : ""}
    <div class="op-list">${rows}</div></div>`;
}

// The last runs of a check, oldest first: one mark per run in the color of its status.
function strip(c) {
  const runs = c.runs || [];
  if (!runs.length) return `<span class="muted t-s">${c.form === "beat" ? "no beat yet" : "no run yet"}</span>`;
  return `<span class="op-strip" role="img" aria-label="Last ${runs.length} runs">${runs.map(r =>
    `<span class="op-run op-${esc(r.status || "running")}" title="${esc((r.ended_at ? ago(r.ended_at) : "running") + ": " + (r.summary || r.status || ""))}"></span>`).join("")}</span>`;
}

// When the trigger acts: its time, its routine and how often, or the address that the outside system posts to.
function whenText(t) {
  const often = t.cron ? `cron ${t.cron}` : `every ${t.every}`;
  if (t.condition === "time") return esc(often) + (t.title ? ` · “${esc(t.title)}”` : "");
  if (t.arrival === "push") return t.push && t.push.url ? `posts to <span class="mono">${esc(t.push.url)}</span>` : "posts to it";
  return (t.routine ? `${esc(t.form && t.form !== "cmd" ? t.form + " " : "")}<span class="mono">${esc(t.routine)}</span> · ` : "") + esc(often);
}

function itemLink(ev) {
  if (!ev || !ev.item_id) return "";
  const st = ev.item ? ev.item.status : "";
  return ` · <span class="link" data-open="${ev.item_id}">#${ev.item_id}</span>${st ? ` <span class="muted">${esc(st.replace("_", " "))}</span>` : ""}`;
}

function lastText(t) {
  const ev = t.last;
  if (!ev) return `<span class="muted">no event yet</span>`;
  const more = ev.repeats ? ` <span class="muted">(${plural(ev.repeats + 1, "time")})</span>` : "";
  return `${esc(ev.title)}${more} <span class="muted">${esc(ago(ev.at))}</span>${itemLink(ev)}`;
}

function triggerRow(t) {
  const st = t.condition === "state" && t.state ? chip(STATE[t.state] || "c-p", esc(t.state === "paused" && t.paused_until
    ? "paused · " + left(t.paused_until) : t.state)) : "";
  const prio = t.priority != null ? chip(t.priority === 0 ? "c-p0" : "c-p", "P" + t.priority, "Priority") : "";
  const secret = t.arrival === "push" ? button("New secret", { data: { "op-secret": t.name, scope: keyOf(t) }, title: "Show a new secret" }) : "";
  const runs = t.condition === "state" && t.runs && t.runs.length ? `<span class="spacer"></span>${strip(t)}` : "";
  return `<div class="op-row clickrow" role="button" tabindex="0" data-op-trigger="${esc(t.name)}" data-scope="${esc(keyOf(t))}">
    <div class="op-main">
      <div class="op-head"><span class="title">${esc(t.name)}</span>${chip("c-p", COND[t.condition] || esc(t.condition), "Condition")}
        ${chip("c-p", esc(t.arrival), "Arrival")}${st}${prio}${runs}</div>
      <div class="t-s op-line"><span class="muted">When</span> ${whenText(t)}</div>
      <div class="t-s op-line"><span class="muted">Playbook</span> ${esc(t.playbook || "default")}
        · <span class="muted">Last</span> ${lastText(t)}</div>
      ${secret ? `<div class="op-line">${secret}</div>` : ""}
    </div>${OPEN_MARK}</div>`;
}

function bookRow(p, b) {
  const uses = b.triggers.length ? b.triggers.map(x => esc(x.name)).join(", ") : `<span class="muted">no trigger</span>`;
  const props = b.proposals.length ? chip("c-human", plural(b.proposals.length, "proposal"), "Changes an agent proposed") : "";
  return `<div class="op-row clickrow" role="button" tabindex="0" data-op-book="${esc(b.name)}" data-project="${esc(p.name)}">
    <div class="op-main">
      <div class="op-head"><span class="title">${esc(b.name)}</span>${b.is_default ? chip("c-p", "default", "For triggers that name none") : ""}${props}</div>
      <div class="t-s op-line"><span class="muted">Used by</span> ${uses}</div>
    </div>${OPEN_MARK}</div>`;
}

// One detection of the record (#5510): its kind, the check (and subject), how often and when, and its item.
function detectionRow(r, showScope) {
  const seen = `${esc(r.status)} · ${r.count}× · ${ago(r.last_seen)}${r.count > 1 ? " · first " + ago(r.first_seen) : ""}`;
  const sc = showScope ? `<span class="muted t-s">${esc(r.scope_kind === "target" ? "target " + r.scope : r.scope)}</span>` : "";
  return `<div class="op-det">
    <div class="op-head">${chip(QUIET.has(r.kind) ? "c-p" : "c-waiting", esc(r.kind || "?"))}<b>${esc(r.check_name)}${r.subject ? ":" + esc(r.subject) : ""}</b>${sc}
      <span class="muted t-s">${seen}</span><span class="spacer"></span>
      ${r.item_id ? `<span class="link t-s" data-open="${r.item_id}">#${r.item_id}</span>` : `<span class="muted t-s">no item</span>`}</div>
    <div class="muted t-s op-line">${esc(r.summary || "")}</div>
  </div>`;
}

function drawRecord(R, showScope) {
  const sel = $("#opRecKind"), was = sel.value;
  sel.innerHTML = `<option value="">All kinds</option>` + (R.all_kinds || []).map(k =>
    `<option value="${esc(k)}">${esc(k)}${R.kinds && R.kinds[k] ? ` (${R.kinds[k]})` : ""}</option>`).join("");
  sel.value = was;
  $("#opRecord").innerHTML = (R.rows || []).map(r => detectionRow(r, showScope)).join("") || empty("No detections in this time.");
}

// ---- the dialog (one for every form; its content changes)

function ensureDialog() {
  if (dlg) return;
  const el = document.createElement("div");
  el.id = "opDlg"; el.className = "hidden";
  el.innerHTML = '<div class="box op-box"></div>';
  document.body.appendChild(el);
  box = el.firstElementChild;
  dlg = makeDialog(el);
  box.addEventListener("click", onDialogClick);
  box.addEventListener("change", onDialogChange);
  box.addEventListener("input", onDialogInput);
}

function show(title, body, foot) {
  ensureDialog();
  box.innerHTML = `<div class="dlg-head"><h2>${title}</h2>${iconButton("✕", "Close", { data: { "op-close": 1 } })}</div>
    ${body}${foot ? actions(foot) : ""}`;
  dlg.open();
}

const val = (id) => { const f = box.querySelector("#" + id); return f ? f.value.trim() : undefined; };
const kv = (rows) => `<dl class="op-kv">${rows.filter(Boolean).map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>`;

// ---- a trigger: its details, its playbook and its events

async function openTrigger(scope, name) {
  let t;
  try { t = await getJson(`api/operations/trigger?scope=${encodeURIComponent(scope)}&name=${encodeURIComponent(name)}`); }
  catch (e) { return toast(e.message, true); }
  const b = t.playbook_used || {};
  const push = t.push ? [["Address", `<span class="mono">${esc(t.push.url || t.push.path || "")}</span> `
      + button("Copy", { data: { "op-copy": t.push.url || "" }, title: "Copy address" })],
    ["Secret", `${esc(t.push.secret_prefix || "")}… ` + button("New secret", { data: { "op-secret": t.name, scope: keyOf(t) }, title: "Show a new secret" })
      + (t.push.bad_secrets ? ` <span class="c-warn t-s">${plural(t.push.bad_secrets, "refused post")}</span>` : "")]] : [];
  const often = t.cron ? `cron ${t.cron}` : `every ${t.every}`;
  const stateLabel = t.state === "paused" && t.paused_until ? `paused · ${left(t.paused_until)}` : t.state;
  const rows = [
    ["Condition", esc(O && O.conditions[t.condition] || t.condition)],
    ["Arrival", esc(O && O.arrivals[t.arrival] || t.arrival)],
    t.condition === "time" ? ["Time", esc(often) + (t.title ? ` · “${esc(t.title)}”` : "")] : null,
    t.arrival === "poll" && t.condition !== "time" ? ["Routine", `${t.routine ? `<span class="mono">${esc(t.routine)}</span> · ` : ""}${esc(often)}`] : null,
    ...push,
    ["Area", esc(t.area || "")],
    ["Priority", t.priority != null ? "P" + t.priority : `P${t.priority_used} <span class="muted">by its condition</span>`],
    ["Escalate after", t.escalate_after ? esc(t.escalate_after) : `<span class="muted">off</span>`],
    t.condition === "state" ? ["State", esc(stateLabel)] : null,
    t.condition === "state" ? ["Recent runs", strip(t)] : null,
    ["Playbook", `<span class="link" data-op-book="${esc(b.name || "")}" data-project="${esc(t.project || "")}">${esc(b.name || "")}</span>`],
    ["Knowledge", `<div class="op-text t-s">${esc(b.knowledge || "")}</div>`],
    ["Done means", `<div class="op-text t-s">${esc(b.done_means || "")}</div>`],
    ["Tools", `<div class="op-text t-s">${esc(b.tools || "")}</div>`],
    ["Limits", `<div class="op-text t-s">${esc(b.limits || "")}</div>`],
  ];
  const evs = t.event_rows.length ? `<table class="op-events t-s"><tr><th>When</th><th>Event</th><th>Item</th></tr>
    ${t.event_rows.map(e => `<tr><td title="${esc(e.at)}">${esc(ago(e.at))}</td>
      <td>${esc(e.title)}${e.repeats ? ` <span class="muted">(${plural(e.repeats + 1, "time")})</span>` : ""}
        ${e.event_id ? `<div class="muted mono">${esc(e.event_id)}</div>` : ""}
        ${safeLink(e.link) ? `<div><a href="${esc(e.link)}" target="_blank" rel="noopener noreferrer">${esc(e.link)}</a></div>` : ""}</td>
      <td>${e.item_id ? `<span class="link" data-open="${e.item_id}">#${e.item_id}</span>${e.item ? ` <span class="muted">${esc(e.item.status.replace("_", " "))}</span>` : ""}` : `<span class="muted">none</span>`}</td></tr>`).join("")}</table>`
    : `<div class="muted t-m">No events yet.</div>`;
  const run = t.arrival === "poll" ? button("Run now", { data: { "op-run": t.name, scope: keyOf(t) }, title: "Run once now" }) : "";
  const pause = t.condition === "state" ? (t.state === "paused"
    ? button("Resume", { data: { "op-resume": t.name, scope: keyOf(t) }, title: "Resume from now" })
    : select({ id: "opPauseUntil", class: "op-until", title: "Pause duration" }, ["30m", "1h", "2h", "4h", "1d"], "1h")
      + button("Pause", { data: { "op-pause": t.name, scope: keyOf(t) }, title: "Pause for planned work" })) : "";
  show(`${esc(t.name)} <span class="muted t-s">${esc(t.scope)}</span>`,
    kv(rows) + `<h3 class="op-h">Events</h3>${evs}`,
    run + pause + button("Edit", { data: { "op-trigger-edit": t.name, scope: keyOf(t) } }) + button("Close", { data: { "op-close": 1 } }));
}

// A new secret for a push trigger: the old one stops at once, and the new one shows only now (it is kept hashed).
function askSecret(scope, name) {
  show(`New secret · ${esc(name)}`,
    `<p class="t-m op-form-note">The old secret stops working at once. Give the new one to the outside system that posts to this trigger.</p>`,
    button("Cancel", { data: { "op-close": 1 } }) + button("Make new secret", { kind: "warn", data: { "op-rotate": name, scope } }));
}

function showSecret(name, r) {
  const url = r.trigger && r.trigger.push ? r.trigger.push.url : "";
  show(`Secret · ${esc(name)}`,
    `<p class="t-m op-form-note">Copy it now: MaximizePM shows it only once.</p>
     <code class="op-secret">${esc(r.secret)}</code>${button("Copy secret", { data: { "op-copy": r.secret } })}
     ${url ? `<p class="t-m">The outside system posts each event as JSON (an <span class="mono">id</span> and a
       <span class="mono">title</span>) to</p><code class="op-secret">${esc(url)}</code>${button("Copy address", { data: { "op-copy": url } })}` : ""}`,
    button("Done", { kind: "primary", data: { "op-close": 1 } }));
}

// ---- forms

function projectSelect(id, current) {
  return select({ id }, O.projects.map(p => p.name), current);
}
const formProject = () => $("#opProject").value || (O.projects.find(used) || O.projects[0] || {}).name || "";

function areaChoices(p, current) {
  const names = (p ? p.areas : []).map(a => [a.name, a.target ? `${a.name} (target ${a.target})` : a.name]);
  if (!names.some(([n]) => n === O.defaults.general)) names.unshift(["", `${O.defaults.general} (the project's own)`]);
  return select({ id: "opArea" }, names, current ?? (names[0] || [""])[0]);
}
function bookChoices(p, current) {
  const books = p ? p.playbooks : [];
  const def = books.find(b => b.is_default);
  return select({ id: "opBook" }, books.length ? books.map(b => [b.name, b.is_default ? `${b.name} (default)` : b.name]) : [["", "default"]],
    current ?? (def ? def.name : ""));
}
const PRIO = [["", "By its condition"], ...[0, 1, 2, 3, 4].map(n => [String(n), "P" + n])];

function openTriggerForm(t) {
  form = { kind: "trigger", edit: t || null };
  anyway = false;
  const pname = t ? t.project : formProject(), p = projectOf(pname);
  const cond = t ? t.condition : "event", arr = t ? t.arrival : "poll";
  const head = t ? "" : field("Project", projectSelect("opProj", pname)) + field("Name", input({ id: "opName", placeholder: "new-tickets" }));
  const kinds = t ? "" : two(field("When it acts", select({ id: "opCond" }, [["event", "an outside system reports an event"],
      ["time", "a time comes"], ["state", "a check changes state"]], cond)),
    field("How events arrive", select({ id: "opArr" }, [["poll", "MaximizePM runs a routine"], ["push", "the system posts to it"]], arr)));
  const tryBtn = t ? "" : button("Try once", { cls: "hidden", data: { "op-try": 1 }, title: "Run it once" });
  show(t ? `Edit trigger · ${esc(t.name)}` : "Add trigger",
    `<div class="form">${head}${kinds}<div id="opCondFields"></div>
      ${two(field("Area", areaChoices(p, t ? t.area : undefined)), field("Playbook", bookChoices(p, t ? t.playbook : undefined)))}
      ${two(field("Priority", select({ id: "opPrio" }, PRIO, t && t.priority != null ? String(t.priority) : "")),
        field("Escalate after", input({ id: "opEsc", value: t ? t.escalate_after || "" : "", placeholder: "off, or 2h" })))}</div>`,
    button("Cancel", { data: { "op-close": 1 } }) + tryBtn + button(t ? "Save" : "Add", { kind: "primary", data: { "op-save": 1 } }));
  condFields();
}

function resetTrial() {
  anyway = false;
  const tr = box && box.querySelector("#opTrial"); if (tr) tr.textContent = "";
  const sb = box && box.querySelector("[data-op-save]"); if (sb && form && !form.edit) sb.textContent = "Add";
}

function drawCheckFields() {
  const forms = (O && O.forms) || [], kind = val("opCheckForm");
  const f = forms.find(x => x.form === kind) || forms[0];
  if (!f) return;
  const host = box.querySelector("#opCheckFields");
  if (host) host.innerHTML = f.questions.map(q => field(esc(q.ask.replace(/\s*\(Enter:[^)]*\)/, "")),
    input({ "data-op-field": q.field, value: q.default || "" }))).join("");
  const nm = box.querySelector("#opName");
  if (nm) nm.placeholder = f.form === "beat" ? "the job's name (needed)" : "from the address or command";
  const ev = box.querySelector("#opEvery");
  if (ev) ev.placeholder = f.form === "beat" ? "how often to look: 5m" : "5m";
  resetTrial();
}

// The fields of the trigger form that its condition and arrival need.
function condFields() {
  const t = form.edit, host = box.querySelector("#opCondFields");
  const cond = t ? t.condition : val("opCond"), arrSel = box.querySelector("#opArr");
  if (arrSel) { arrSel.disabled = cond !== "event"; if (cond !== "event") arrSel.value = "poll"; }
  const arr = t ? t.arrival : val("opArr");
  const tryBtn = box.querySelector("[data-op-try]");
  if (tryBtn) tryBtn.classList.toggle("hidden", !(cond === "state" && !t));
  resetTrial();
  const often = (x) => two(field("Cron", input({ id: "opCron", value: x ? x.cron || "" : "", placeholder: "0 9 * * mon" })),
    field("Or every", input({ id: "opEvery", value: x ? x.every || "" : "", placeholder: "5m" })));
  let h = "";
  if (cond === "state") {
    if (t) h = `<p class="muted t-s op-form-note">A check: change its area, playbook, priority and escalation here.</p>`;
    else {
      const forms = (O && O.forms) || [];
      h = field("What to watch", select({ id: "opCheckForm" }, forms.map(f => [f.form, f.label]), (forms[0] || {}).form || "url"))
        + `<div id="opCheckFields"></div>`
        + two(field("How often", input({ id: "opEvery", placeholder: "5m" })),
          field("On a fault", select({ id: "opSeverity" }, [["item", "an item for an agent, and the manager hears"],
            ["note", "a line on this page only"]], "item")))
        + `<div id="opTrial" class="muted t-s" role="status"></div>`;
    }
  } else {
    const nm = box.querySelector("#opName"); if (nm) nm.placeholder = "new-tickets";
    if (cond === "time") h = field("Title of its event", input({ id: "opTitle", value: t ? t.title || "" : "", placeholder: "Weekly dependency update" })) + often(t);
    else if (arr === "poll") h = field("Routine", input({ id: "opCmd", value: t ? t.routine || "" : "", placeholder: "a command that prints one JSON event a line" }))
      + often(t) + field("Time limit", input({ id: "opTimeout", value: t ? t.timeout || "" : "", placeholder: "30s" }));
    else if (!t) h = `<p class="muted t-s op-form-note">MaximizePM gives the trigger its address and a secret when you add it.</p>`;
  }
  host.innerHTML = h;
  if (cond === "state" && !t) drawCheckFields();
}

function stateFormArgs() {
  const pname = val("opProj"), p = projectOf(pname), aname = val("opArea");
  const a = p && p.areas.find(x => x.name === aname);
  const scope = a && a.target ? `target:${a.target}` : `project:${pname}`;
  const fields = {};
  box.querySelectorAll("#opCheckFields [data-op-field]").forEach(i => { if (i.value.trim()) fields[i.dataset.opField] = i.value.trim(); });
  const every = val("opEvery");
  if (every) fields.every = every;
  fields.severity = val("opSeverity") || "item";
  return { scope, form: val("opCheckForm"), name: val("opName") || undefined, fields,
    area: aname || undefined, playbook: val("opBook") || undefined, priority: val("opPrio"), escalate_after: val("opEsc") };
}

// One run of a state trigger in maxpm serve; it keeps nothing. Returns true when it passed (a beat has no run).
async function trialState() {
  const a = stateFormArgs();
  if (a.form === "beat") return true;
  const out = box.querySelector("#opTrial");
  if (out) out.textContent = "Running it once …";
  try {
    const r = await act("ops_trial", { scope: a.scope, form: a.form, name: a.name, fields: a.fields });
    const res = r.result || {};
    const ok = res.status === "ok";
    if (out) out.textContent = `${ok ? "Passed" : "Did not pass"}: ${res.status || "?"}${res.summary ? " · " + res.summary : ""}`;
    return ok;
  } catch (e) { if (out) out.textContent = e.message; return false; }
}

function openAreaForm(project, a) {
  form = { kind: "area", edit: a || null, project };
  show(a ? `Edit area · ${esc(a.name)}` : "Add area",
    `<div class="form">${a ? field("Name", input({ id: "opName", value: a.name }))
        : field("Project", projectSelect("opProj", project)) + field("Name", input({ id: "opName", placeholder: "prod-eu" }))}
      ${field("Description", textarea({ id: "opDesc", rows: 2, placeholder: "What runs here" }, a ? a.description || "" : ""))}
      ${field("Folder or host", input({ id: "opPlace", value: a ? a.place || "" : "", placeholder: "~/work/app or db.example.com" }))}
      ${a ? (a.target ? `<p class="muted t-s">Deploy target ${esc(a.target)}.</p>` : "")
        : field("Deploy target", select({ id: "opTarget" }, [["", "none"], ...O.targets.map(n => [n, n])], ""))}</div>`,
    button("Cancel", { data: { "op-close": 1 } }) + button(a ? "Save" : "Add", { kind: "primary", data: { "op-save": 1 } }));
}

const BOOK_FIELDS = [["knowledge", "Knowledge", "What the agent must know: where to look, what is normal", 4],
  ["done_means", "Done means", "When the work of an event is finished", 2],
  ["tools", "Tools", "", 2], ["limits", "Limits", "", 2]];

function openBookForm(project, b) {
  form = { kind: "book", edit: b || null, project };
  const d = O.defaults;
  const ph = { tools: d.tools, limits: d.limits };
  const props = b && b.proposals.length ? `<h3 class="op-h">Proposed changes</h3>${b.proposals.map(x => `<div class="op-prop t-s">
      <div><b>${esc(x.ref)}</b> ${esc(x.by || "")} <span class="muted">${esc(ago(x.at))}</span>${x.item_id ? ` · <span class="link" data-open="${x.item_id}">#${x.item_id}</span>` : ""}</div>
      <div>${esc(x.why || "")}</div>
      ${x.knowledge != null ? `<div class="muted">Knowledge</div><div class="op-text">${esc(x.knowledge)}</div>` : ""}
      ${x.done_means != null ? `<div class="muted">Done means</div><div class="op-text">${esc(x.done_means)}</div>` : ""}
      <div class="pl-bar">${button("Accept", { data: { "op-accept": x.ref } })}${button("Reject", { kind: "warn", data: { "op-reject": x.ref } })}</div></div>`).join("")}` : "";
  const uses = b ? `<p class="t-s op-form-note"><span class="muted">Used by</span> ${b.triggers.length ? b.triggers.map(x => esc(x.name)).join(", ") : "no trigger"}</p>` : "";
  show(b ? `Playbook · ${esc(b.name)}` : "Add playbook",
    `${uses}<div class="form">${b ? field("Name", input({ id: "opName", value: b.name }))
        : field("Project", projectSelect("opProj", project)) + field("Name", input({ id: "opName", placeholder: "tickets" }))}
      ${BOOK_FIELDS.map(([k, l, p, rows]) => field(l, textarea({ id: "opB-" + k, rows, placeholder: p || ph[k] }, b ? b[k] || "" : ""))).join("")}
      ${two(field("Model", input({ id: "opB-model", value: b ? b.model || "" : "", placeholder: "the queue's setting" })),
        field("Effort", input({ id: "opB-effort", value: b ? b.effort || "" : "", placeholder: "the queue's setting" })))}
      ${two(field("Where it runs", input({ id: "opB-workdir", value: b ? b.workdir || "" : "", placeholder: "the area's folder" })),
        field("Repair command", input({ id: "opB-repair", value: b ? b.repair || "" : "", placeholder: "none" })))}
      <p class="muted t-s">Only a person sets the tools, the limits and the repair command.</p></div>${props}`,
    button("Cancel", { data: { "op-close": 1 } }) + button(b ? "Save" : "Add", { kind: "primary", data: { "op-save": 1 } }));
}

const BOOK_KEYS = ["knowledge", "done_means", "tools", "limits", "model", "effort", "workdir", "repair"];

async function save() {
  const f = form, name = val("opName");
  if (f.kind === "trigger" && !f.edit && val("opCond") === "state") {
    if (!anyway && !(await trialState())) {
      anyway = true;
      const sb = box.querySelector("[data-op-save]"); if (sb) sb.textContent = "Add anyway";
      return;
    }
    let r;
    try { r = await act("ops_add", stateFormArgs()); } catch (e) { return; }
    dlg.close();
    toast(`Check ${r.check.scope}/${r.check.name} added: maxpm serve runs it from now on`);
    return renderOperations(true);
  }
  let op, args, done;
  if (f.kind === "trigger") {
    const t = f.edit;
    args = { area: val("opArea"), playbook: val("opBook"), priority: val("opPrio"), escalate_after: val("opEsc") };
    for (const [k, id] of [["title", "opTitle"], ["command", "opCmd"], ["cron", "opCron"], ["every", "opEvery"], ["timeout", "opTimeout"]]) {
      const v = val(id); if (v !== undefined) args[k] = v;
    }
    if (t) { op = "ops_trigger_edit"; Object.assign(args, { scope: keyOf(t), name: t.name }); done = `Trigger ${t.name} saved`; }
    else {
      if (!name) return toast("Give the trigger a name", true);
      op = "ops_trigger_add"; Object.assign(args, { project: val("opProj"), name, condition: val("opCond"), arrival: val("opArr") });
      if (!args.area) delete args.area;
      done = `Trigger ${name} added`;
    }
  } else if (f.kind === "area") {
    const a = f.edit;
    args = { description: val("opDesc"), place: val("opPlace") };
    if (a) { op = "ops_area_edit"; Object.assign(args, { project: f.project, name: a.name, new_name: name }); done = `Area ${name} saved`; }
    else {
      if (!name) return toast("Give the area a name", true);
      op = "ops_area_add"; Object.assign(args, { project: val("opProj"), name, target: val("opTarget") }); done = `Area ${name} added`;
    }
  } else {
    const b = f.edit;
    args = {};
    for (const k of BOOK_KEYS) {
      const v = val("opB-" + k);
      if (!b || v !== (b[k] || "")) args[k] = v;  // an edit sends only what changed (only a person sets tools and limits)
    }
    if (b) {
      if (!Object.keys(args).length && name === b.name) { dlg.close(); return; }
      op = "ops_playbook_edit"; Object.assign(args, { project: f.project, name: b.name, new_name: name }); done = `Playbook ${name} saved`;
    } else {
      if (!name) return toast("Give the playbook a name", true);
      op = "ops_playbook_add"; Object.assign(args, { project: val("opProj"), name }); done = `Playbook ${name} added`;
    }
  }
  let r;
  try { r = await act(op, args); } catch (e) { return; }
  toast(r && r.changed === "proposed" ? "Change proposed: the manager decides" : r && r.changed === "nothing" ? "Nothing changed" : done);
  if (r && r.secret) showSecret(name, r); else dlg.close();
  renderOperations(true);
}

// ---- clicks

async function onDialogClick(e) {
  const t = e.target;
  if (t.closest("[data-op-close]")) return dlg.close();
  if (t.closest("[data-open]")) return dlg.close();  // the item opens in the drawer (app.js)
  const c = t.closest("[data-op-copy]");
  if (c) { await copyText(c.dataset.opCopy); return toast("Copied"); }
  if (t.closest("[data-op-try]")) return trialState();
  if (t.closest("[data-op-save]")) return save();
  const b = t.closest("[data-op-book]");
  if (b) return openBook(b.dataset.project, b.dataset.opBook);
  const s = t.closest("[data-op-secret]");
  if (s) return askSecret(s.dataset.scope, s.dataset.opSecret);
  const rot = t.closest("[data-op-rotate]");
  if (rot) {
    let r; try { r = await act("ops_trigger_rotate", { scope: rot.dataset.scope, name: rot.dataset.opRotate }); } catch (err) { return; }
    showSecret(rot.dataset.opRotate, r); return renderOperations(true);
  }
  const run = t.closest("[data-op-run]");
  if (run) {
    try { await act("ops_trigger_run", { scope: run.dataset.scope, name: run.dataset.opRun }); } catch (err) { return; }
    toast(`${run.dataset.opRun} runs now`); setTimeout(() => renderOperations(true), 3000); return;
  }
  const pa = t.closest("[data-op-pause]");
  if (pa) {
    try { await act("ops_pause", { scope: pa.dataset.scope, name: pa.dataset.opPause, until: val("opPauseUntil") || "1h" }); } catch (err) { return; }
    toast(`${pa.dataset.opPause} paused`); openTrigger(pa.dataset.scope, pa.dataset.opPause); return renderOperations(true);
  }
  const re = t.closest("[data-op-resume]");
  if (re) {
    try { await act("ops_pause", { scope: re.dataset.scope, name: re.dataset.opResume, resume: true }); } catch (err) { return; }
    toast(`${re.dataset.opResume} runs again`); openTrigger(re.dataset.scope, re.dataset.opResume); return renderOperations(true);
  }
  const ed = t.closest("[data-op-trigger-edit]");
  if (ed) return openTriggerForm(findTrigger(ed.dataset.scope, ed.dataset.opTriggerEdit));
  const acc = t.closest("[data-op-accept]"), rej = t.closest("[data-op-reject]");
  if (acc || rej) {
    const ref = (acc || rej).dataset[acc ? "opAccept" : "opReject"];
    const note = rej ? prompt_note(ref) : null;
    if (rej && !note) return;
    try { await act("ops_playbook_decide", { ref, accept: !!acc, note }); } catch (err) { return; }
    toast(`${ref} ${acc ? "accepted" : "rejected"}`); dlg.close(); renderOperations(true);
  }
}

// The reason of a rejected proposal: a field in the dialog, read when Reject is pressed the second time.
function prompt_note(ref) {
  let f = box.querySelector("#opRejectWhy");
  if (!f || f.dataset.ref !== ref) {
    box.querySelectorAll(".op-reject-why").forEach(x => x.remove());
    const at = box.querySelector(`[data-op-reject="${CSS.escape(ref)}"]`).closest(".op-prop");
    at.insertAdjacentHTML("beforeend", `<div class="form op-reject-why">${field("Why not", input({ id: "opRejectWhy", "data-ref": ref }))}</div>`);
    box.querySelector("#opRejectWhy").focus();
    toast("Say why, then press Reject again");
    return null;
  }
  return f.value.trim() || (toast("Say why", true), null);
}

function onDialogChange(e) {
  if (!form) return;
  if (e.target.id === "opCond" || e.target.id === "opArr") return condFields();
  if (e.target.id === "opCheckForm") return drawCheckFields();
  if (e.target.id === "opProj" && form.kind === "trigger") {
    const p = projectOf(e.target.value);
    box.querySelector("#opArea").outerHTML = areaChoices(p);
    box.querySelector("#opBook").outerHTML = bookChoices(p);
    resetTrial();
  }
  if (e.target.id === "opArea" && form.kind === "trigger") resetTrial();
}

function onDialogInput(e) {
  if (form && form.kind === "trigger" && !form.edit && e.target.closest("#opCheckFields, #opName, #opEvery")) resetTrial();
}

function findTrigger(scope, name) {
  for (const p of O ? O.projects : []) for (const a of p.areas) for (const t of a.triggers)
    if (keyOf(t) === scope && t.name === name) return t;
  return null;
}
function openBook(project, name) {
  const p = projectOf(project), b = p && p.playbooks.find(x => x.name === name);
  if (b) openBookForm(project, b);
}

$("#tab-operations").addEventListener("click", (e) => {
  const t = e.target;
  if (!O || t.closest("[data-open]") || t.closest("a")) return;
  const ca = t.closest("[data-op-check-accept]");
  if (ca) return act("ops_accept", { scope: ca.dataset.scope, name: ca.dataset.opCheckAccept })
    .then(() => { toast(`${ca.dataset.opCheckAccept} is a check now`); renderOperations(true); }).catch(() => {});
  const cr = t.closest("[data-op-check-reject]");
  if (cr) {
    const why = document.getElementById("oprej-" + cr.dataset.pid), note = why ? why.value.trim() : "";
    if (!note) return toast("Say why not, in the box beside Reject", true);
    return act("ops_reject", { scope: cr.dataset.scope, name: cr.dataset.opCheckReject, note })
      .then(() => { toast(`${cr.dataset.opCheckReject} rejected`); renderOperations(true); }).catch(() => {});
  }
  const s = t.closest("[data-op-secret]");
  if (s) return askSecret(s.dataset.scope, s.dataset.opSecret);
  const ae = t.closest("[data-op-area-edit]");
  if (ae) { const p = projectOf(ae.dataset.project); return openAreaForm(ae.dataset.project, p && p.areas.find(a => a.name === ae.dataset.opAreaEdit)); }
  if (t.closest("#opAddArea")) return openAreaForm(formProject(), null);
  if (t.closest("#opAddBook")) return openBookForm(formProject(), null);
  if (t.closest("#opAddTrigger")) return openTriggerForm(null);
  const tr = t.closest("[data-op-trigger]");
  if (tr) return openTrigger(tr.dataset.scope, tr.dataset.opTrigger);
  const b = t.closest("[data-op-book]");
  if (b) return openBook(b.dataset.project, b.dataset.opBook);
});
$("#tab-operations").addEventListener("keydown", (e) => {
  if ((e.key === "Enter" || e.key === " ") && e.target.matches(".op-row")) { e.preventDefault(); e.target.click(); }
});
$("#opProject").addEventListener("change", () => { store(PROJECT_KEY, $("#opProject").value); renderOperations(true); });
$("#opRecKind").addEventListener("change", () => renderOperations(true));
$("#opRecSince").addEventListener("change", () => renderOperations(true));
