// The Operations tab (#6629; canvas operations-tab, layout mark 2026-11-21: "projects, then areas, then triggers;
// playbooks at the project level, each showing the triggers that use it"). Each trigger shows its condition (time,
// state, event), how its events arrive (poll, push: its address and a button that makes a new secret and shows it
// once), its playbook, and its last event with the item it opened. A trigger opens a dialog with its playbook and its
// events. Add and edit forms for areas, triggers and playbooks (ops ops_area_*, ops_trigger_*, ops_playbook_*,
// river/routes/operations.py). A check is a state trigger: it is added in Plan > Operations (Add a check), and its
// area, playbook and priority change here. Reads GET api/operations (all of it, in one read) and
// GET api/operations/trigger (one trigger's events).
import { $, esc, getJson, ago, left, act, toast, store, copyText } from "../lib.js";
import { button, iconButton } from "../components/button.js";
import { chip } from "../components/chip.js";
import { empty } from "../components/empty.js";
import { makeDialog } from "../components/dialog.js";
import { field, input, select, textarea, two, actions } from "../components/form.js";
import { OPEN_MARK } from "../components/rowOpen.js";
import { tab } from "../page.js";

let O = null, sig = "", dlg = null, box = null, form = null;
const PROJECT_KEY = "river.opsProject";

const COND = { time: "time", state: "state", event: "event" };
const STATE = { fault: "c-p0", failing: "c-waiting", recovered: "c-in_progress", healthy: "c-ready", new: "c-p", paused: "c-p" };
const keyOf = (t) => `${t.scope_kind}:${t.scope}`;
const projectOf = (name) => O && O.projects.find(p => p.name === name);
const plural = (n, one, many = one + "s") => `${n} ${n === 1 ? one : many}`;
// An event's link comes from outside (untrusted data): only a web address becomes a link.
const safeLink = (u) => typeof u === "string" && /^https?:\/\//i.test(u);

// ---- reading and drawing

export async function renderOperations(force) {
  if (tab !== "operations") return;
  let d;
  try { d = await getJson("api/operations"); }
  catch (e) { $("#opBody").innerHTML = empty(esc(e.message)); return; }
  O = d;
  drawProjects();
  const s = JSON.stringify([d, $("#opProject").value, Math.floor(Date.now() / 60000)]);  // "3m ago" changes each minute
  if (s === sig && !force) return;
  sig = s;
  $("#opBody").innerHTML = bodyHtml(d);
}

// The project choice of the bar: all projects, or one; this browser keeps it.
function drawProjects() {
  const sel = $("#opProject"), want = sel.value || store(PROJECT_KEY) || "";
  const opts = [["", "All projects"], ...O.projects.map(p => [p.name, p.name])];
  const html = opts.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join("");
  if (sel.dataset.sig !== html) { sel.innerHTML = html; sel.dataset.sig = html; }
  sel.value = opts.some(([v]) => v === want) ? want : "";
}

// A project that has operations: an area with a trigger, an area a person added, or a playbook of its own.
const used = (p) => p.areas.some(a => a.triggers.length || a.created_by) || p.playbooks.some(b => !b.is_default);

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

function projectHtml(p) {
  const areas = p.areas.length ? p.areas.map(a => areaHtml(p, a)).join("")
    : `<div class="muted t-m">No areas yet.</div>`;
  const books = p.playbooks.length ? p.playbooks.map(b => bookRow(p, b)).join("") : `<div class="muted t-m">No playbooks yet.</div>`;
  return `<div class="panel op-project"><h2>${esc(p.name)}</h2>
    ${areas}
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
  return `<div class="op-row clickrow" role="button" tabindex="0" data-op-trigger="${esc(t.name)}" data-scope="${esc(keyOf(t))}">
    <div class="op-main">
      <div class="op-head"><span class="title">${esc(t.name)}</span>${chip("c-p", COND[t.condition] || esc(t.condition), "Condition")}
        ${chip("c-p", esc(t.arrival), "Arrival")}${st}${prio}</div>
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
  const rows = [
    ["Condition", esc(O && O.conditions[t.condition] || t.condition)],
    ["Arrival", esc(O && O.arrivals[t.arrival] || t.arrival)],
    t.condition === "time" ? ["Time", esc(often) + (t.title ? ` · “${esc(t.title)}”` : "")] : null,
    t.arrival === "poll" && t.condition !== "time" ? ["Routine", `${t.routine ? `<span class="mono">${esc(t.routine)}</span> · ` : ""}${esc(often)}`] : null,
    ...push,
    ["Area", esc(t.area || "")],
    ["Priority", t.priority != null ? "P" + t.priority : `P${t.priority_used} <span class="muted">by its condition</span>`],
    ["Escalate after", t.escalate_after ? esc(t.escalate_after) : `<span class="muted">off</span>`],
    t.condition === "state" ? ["State", esc(t.state)] : null,
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
  show(`${esc(t.name)} <span class="muted t-s">${esc(t.scope)}</span>`,
    kv(rows) + `<h3 class="op-h">Events</h3>${evs}`,
    run + button("Edit", { data: { "op-trigger-edit": t.name, scope: keyOf(t) } }) + button("Close", { data: { "op-close": 1 } }));
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
  const pname = t ? t.project : formProject(), p = projectOf(pname);
  const cond = t ? t.condition : "event", arr = t ? t.arrival : "poll";
  const head = t ? "" : field("Project", projectSelect("opProj", pname)) + field("Name", input({ id: "opName", placeholder: "new-tickets" }));
  const kinds = t ? "" : two(field("When it acts", select({ id: "opCond" }, [["event", "an outside system reports an event"],
      ["time", "a time comes"], ["state", "a check changes state"]], cond)),
    field("How events arrive", select({ id: "opArr" }, [["poll", "MaximizePM runs a routine"], ["push", "the system posts to it"]], arr)));
  show(t ? `Edit trigger · ${esc(t.name)}` : "Add trigger",
    `<div class="form">${head}${kinds}<div id="opCondFields"></div>
      ${two(field("Area", areaChoices(p, t ? t.area : undefined)), field("Playbook", bookChoices(p, t ? t.playbook : undefined)))}
      ${two(field("Priority", select({ id: "opPrio" }, PRIO, t && t.priority != null ? String(t.priority) : "")),
        field("Escalate after", input({ id: "opEsc", value: t ? t.escalate_after || "" : "", placeholder: "off, or 2h" })))}</div>`,
    button("Cancel", { data: { "op-close": 1 } }) + button(t ? "Save" : "Add", { kind: "primary", data: { "op-save": 1 } }));
  condFields();
}

// The fields of the trigger form that its condition and arrival need.
function condFields() {
  const t = form.edit, host = box.querySelector("#opCondFields");
  const cond = t ? t.condition : val("opCond"), arrSel = box.querySelector("#opArr");
  if (arrSel) { arrSel.disabled = cond !== "event"; if (cond !== "event") arrSel.value = "poll"; }
  const arr = t ? t.arrival : val("opArr");
  const save = box.querySelector("[data-op-save]");
  if (save) save.disabled = cond === "state" && !t;
  const often = (x) => two(field("Cron", input({ id: "opCron", value: x ? x.cron || "" : "", placeholder: "0 9 * * mon" })),
    field("Or every", input({ id: "opEvery", value: x ? x.every || "" : "", placeholder: "5m" })));
  let h = "";
  if (cond === "state") h = t ? `<p class="muted t-s op-form-note">A check: change its routine in Plan › Operations.</p>`
    : `<p class="t-m op-form-note">A check is a state trigger. Add it in <a href="plan/ops">Plan › Operations</a> with Add a check; then set its area and playbook here.</p>`;
  else if (cond === "time") h = field("Title of its event", input({ id: "opTitle", value: t ? t.title || "" : "", placeholder: "Weekly dependency update" })) + often(t);
  else if (arr === "poll") h = field("Routine", input({ id: "opCmd", value: t ? t.routine || "" : "", placeholder: "a command that prints one JSON event a line" }))
    + often(t) + field("Time limit", input({ id: "opTimeout", value: t ? t.timeout || "" : "", placeholder: "30s" }));
  else if (!t) h = `<p class="muted t-s op-form-note">MaximizePM gives the trigger its address and a secret when you add it.</p>`;
  host.innerHTML = h;
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
    toast(`${run.dataset.opRun} runs now`); return;
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
  if (e.target.id === "opProj" && form.kind === "trigger") {
    const p = projectOf(e.target.value);
    box.querySelector("#opArea").outerHTML = areaChoices(p);
    box.querySelector("#opBook").outerHTML = bookChoices(p);
  }
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
