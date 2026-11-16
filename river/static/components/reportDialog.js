// The review report of a release (#6003, summaries-mockup v7): what a cut release ships, for the person to read before
// the deploy. Left: the scope line, the summary paragraph (the manager's, or MaximizePM's plain one until then), the
// numbers, and the groups, risks first in red; a group opens to its tasks. Right: the task you pick, with what changed
// for a person, its proof (commits, check), who, when and cost, its decisions (questions and their answers), and its
// Code tab: the Changes view of the task (changesView.js), where a comment on a line goes to the agent. On a phone the
// levels are one screen each: a tap on a task shows it, Back goes up.
// Opens from Plan > Releases (data-report), the drawer of a deploy or review item, and its Read card. Data: GET
// api/report/<id>; Discuss sends the manager a note (op send) and opens its chat.
import { esc, ago, clip, getJson, toast, act } from "../lib.js";
import { button } from "./button.js";
import { chip } from "./chip.js";
import { makeDialog } from "./dialog.js";
import { renderChanges } from "./changesView.js";

let dlg = null, el = null, data = null, picked = null, tab = "task";
const usd = (n) => n == null ? "" : "$" + (n >= 100 ? Math.round(n).toLocaleString() : n.toFixed(2));

export function numbersHtml(n) {
  const bits = [[n.done, "done"], [n.dropped, "dropped"], [n.open, "open"], [n.faults_open, "open faults", n.faults_open ? "c-p0" : ""],
    [n.fixes, "review fixes"], [n.decisions, "decided by you"]];
  return `<div class="rep-nums">${bits.filter(([v, label]) => v || label === "done").map(([v, label, cls]) =>
    chip(cls || "c-p", `<b>${v}</b> ${label}`)).join("")}${n.usd != null ? chip("c-p", `<b>${usd(n.usd)}</b> cost`) : ""}</div>`;
}

const taskLine = (t) => `<div class="rep-t${picked === t.id ? " on" : ""}" data-rep-task="${t.id}" role="button" tabindex="0">
  <span class="muted t-s">#${t.id}</span> ${esc(t.short || clip(t.title, 90))}${t.status === "done" ? "" : ` <span class="muted">(${esc(t.status.replace("_", " "))})</span>`}
  ${t.risk ? chip("c-p0", esc(t.risk)) : ""}${t.changed ? `<div class="muted t-s">${esc(t.changed)}</div>` : ""}</div>`;

export function leftHtml(r) {
  const rel = r.release, rv = rel.review;
  const state = rel.status !== "open" ? rel.status.replace("_", " ") : rel.cut_at ? `cut ${ago(rel.cut_at)}` : "collecting";
  const scope = `<div class="rep-scope">${chip("c-goal", "Release")} <b>#${rel.id}</b> of ${esc(rel.target || "")}
    <span class="muted t-s">${esc(state)}${rel.cut_rev ? " · " + esc(rel.cut_rev) : ""}${rv ? ` · review <span class="link" data-open="${rv.id}">#${rv.id}</span> ${esc(rv.status.replace("_", " "))}` : ""}</span></div>`;
  const sum = `<div class="rep-sum"><p>${esc(r.summary.text)}</p><div class="muted t-s">${r.summary.by === "maxpm"
    ? "Written by MaximizePM from its records; the manager writes the summary when it reads the release."
    : `The summary of ${esc(r.summary.by)}${r.summary.at ? ", " + esc(ago(r.summary.at)) : ""}`}</div></div>`;
  const groups = r.groups.map(g => `<details class="rep-g${g.risk ? " risk" : ""}"${g.risk || r.groups.length <= 3 ? " open" : ""}>
    <summary><b>${esc(g.name)}</b> <span class="muted t-s">${g.items.length} task${g.items.length === 1 ? "" : "s"}</span>
      ${g.line && !g.risk ? `<div class="muted t-s">${esc(g.line)}</div>` : ""}</summary>
    ${g.items.map(i => taskLine(r.tasks[i])).join("")}</details>`).join("");
  return scope + sum + numbersHtml(r.numbers) + (groups || `<div class="muted">This release ships no task yet.</div>`);
}

export function taskHtml(t) {
  if (!t) return `<div class="muted rep-empty">Pick a task on the left: what changed, its proof and its decisions.</div>`;
  const proof = [...t.commits.map(c => `<code>${esc(c)}</code>`), t.check ? `check: <code>${esc(t.check)}</code>` : ""].filter(Boolean);
  const dec = t.decisions.length ? t.decisions.map(d => `<div class="rep-dec"><span class="muted t-s">${esc(d.kind)} · ${esc(d.from)}${d.to ? " to " + esc(d.to) : ""} · ${esc(ago(d.at))}</span>
      <div>${esc(clip(d.body, 600))}</div>${d.answer ? `<div class="rep-ans"><b>${esc(d.answer.from)}:</b> ${esc(clip(d.answer.body, 600))}</div>`
      : d.kind === "question" ? `<div class="muted t-s">No answer.</div>` : ""}</div>`).join("")
    : `<div class="muted t-s">No questions or notes on this task.</div>`;
  return `<div class="rep-tabs" role="tablist">${button("Task", { cls: tab === "task" ? "on" : "", data: { "rep-tab": "task" }, attrs: `role="tab" aria-selected="${tab === "task"}"` })}${button("Code", { cls: tab === "code" ? "on" : "", data: { "rep-tab": "code" }, attrs: `role="tab" aria-selected="${tab === "code"}"` })}</div>
    <div class="rep-pane" ${tab === "task" ? "" : "hidden"}>
      <h3>#${t.id} ${esc(t.title)}</h3>
      ${t.risk ? `<div>${chip("c-p0", esc(t.risk))}</div>` : ""}
      <div class="rep-line">${t.changed ? esc(t.changed) : `<span class="muted">No line for a person (done --changed).</span>`}</div>
      <h4>Proof</h4><div class="t-s">${proof.join(" · ") || `<span class="muted">No commit named in its output.</span>`}</div>
      ${t.output ? `<div class="muted t-s rep-out">${esc(clip(t.output, 800))}</div>` : ""}
      <div class="muted t-s">${t.done_by ? esc(t.done_by) : ""}${t.closed_at ? " · " + esc(ago(t.closed_at)) : ""}${t.usd != null ? " · " + usd(t.usd) : ""}${t.goals.length ? " · goal " + t.goals.map(esc).join(", ") : ""}</div>
      <h4>Decisions</h4>${dec}
      <div class="rep-acts">${button("Open the item", { data: { open: t.id } })}</div>
    </div>
    <div class="rep-pane rep-code" ${tab === "code" ? "" : "hidden"}></div>`;
}

function draw() {
  const r = data, rel = r.release;
  el.querySelector("h2").textContent = `Release #${rel.id} of ${rel.target || "its target"}`;
  el.querySelector(".rep-l").innerHTML = leftHtml(r);
  const right = el.querySelector(".rep-r");
  right.innerHTML = `<div class="rep-back">${button("‹ Back", { data: { "rep-back": 1 } })}</div>` + taskHtml(picked ? r.tasks[picked] : null);
  el.querySelector(".rep-grid").classList.toggle("picked", !!picked);
  if (picked && tab === "code") renderChanges(right.querySelector(".rep-code"), { item: picked });
}

export async function openReport(id) {
  if (!el) {
    el = document.createElement("div");
    el.id = "reportDlg";
    el.className = "hidden";
    el.innerHTML = `<div class="box"><div class="dlg-head"><h2>Release</h2><span class="spacer"></span>
      ${button("Discuss with the manager", { data: { "rep-discuss": 1 }, title: "Send it to the manager's chat" })}
      ${button("✕", { kind: "icon", label: "Close", data: { "rep-close": 1 } })}</div>
      <div class="rep-grid"><div class="rep-l"></div><div class="rep-r"></div></div></div>`;
    document.body.append(el);
    dlg = makeDialog(el, { draft: false });
    el.addEventListener("click", onClick);
    el.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.dataset.repTask) onClick(e); });
  }
  picked = null; tab = "task";
  el.querySelector(".rep-l").innerHTML = `<div class="muted">Reading the release…</div>`;
  el.querySelector(".rep-r").innerHTML = "";
  dlg.open();
  try { data = await getJson(`api/report/${id}`); }
  catch (e) { el.querySelector(".rep-l").innerHTML = `<div class="muted">${esc(e.message)}</div>`; return; }
  const first = data.groups.length ? data.groups[0].items[0] : null;
  picked = matchMedia("(max-width: 759px)").matches ? null : first;  // a phone starts at the summary
  draw();
}

async function onClick(e) {
  const t = e.target;
  if (t.closest("[data-rep-close]")) return dlg.close();
  if (t.closest("[data-open]")) return dlg.close();  // the item opens in the drawer
  const task = t.closest("[data-rep-task]");
  if (task) { picked = +task.dataset.repTask; tab = "task"; return draw(); }
  const tb = t.closest("[data-rep-tab]");
  if (tb) { tab = tb.dataset.repTab; return draw(); }
  if (t.closest("[data-rep-back]")) { picked = null; return draw(); }
  if (t.closest("[data-rep-discuss]") && data) {
    const { S, nav } = await import("../page.js");
    const mgr = ((S && S.agents) || []).find(a => a.role === "manager" && !["gone", "stopped"].includes(a.state));
    if (!mgr) return toast("No manager runs now", true);
    try {
      await act("send", { kind: "note", to: mgr.name, item: data.release.id,
        body: `The person wants to talk about the report of release #${data.release.id} of ${data.release.target}`
          + (picked ? `, task #${picked}` : "") + `: maxpm target report ${data.release.id}` });
      dlg.close();
      nav.openSession(mgr.name);
    } catch (err) { /* act showed it */ }
  }
}
