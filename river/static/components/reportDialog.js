// The review report of a release (#6003, summaries-mockup v7): what a cut release ships, for the person to read before
// the deploy. Left: the scope line, the summary (what changes for a user by area, the risks and what to check; the
// summary task writes it at the cut, MaximizePM's plain text until then), the numbers, and below them the task list,
// closed, by the areas of the summary, risks first in red; a group opens to its tasks. Right: the task you pick, with what changed
// for a person, its proof (commits, check), who, when and cost, its decisions (questions and their answers), and its
// Code tab: the Changes view of the task (changesView.js), where a comment on a line goes to the agent. On a phone the
// levels are one screen each: a tap on a task shows it, Back goes up.
// Opens from Plan > Releases (data-report), the drawer of a deploy or review item, and its Read card. Data: GET
// api/report/<id>; Discuss sends the manager a note (op send) and opens its chat.
import { esc, ago, clip, getJson, toast, act } from "../lib.js";
import { button } from "./button.js";
import { chip } from "./chip.js";
import { makeDialog, wideButton } from "./dialog.js";
import { renderChanges } from "./changesView.js";

let dlg = null, el = null, data = null, picked = null, tab = "task";
// chat.js markdown (marked, then DOMPurify), imported when the dialog first opens: DOMPurify needs a page.
let md = null;
const asText = (text) => `<p>${esc(text)}</p>`;
const usd = (n) => n == null ? "" : "$" + (n >= 100 ? Math.round(n).toLocaleString() : n.toFixed(2));

export function numbersHtml(n) {
  const bits = [[n.done, "done"], [n.dropped, "dropped"], [n.open, "open"], [n.faults_open, "open faults", n.faults_open ? "c-p0" : ""],
    [n.fixes, "review fixes"], [n.decisions, "decided by you"]];
  return `<div class="rep-nums">${bits.filter(([v, label]) => v || label === "done").map(([v, label, cls]) =>
    chip(cls || "c-p", `<b>${v}</b> ${label}`)).join("")}${n.usd != null ? chip("c-p", `<b>${usd(n.usd)}</b> cost`) : ""}${n.files != null
    ? chip("c-p", `<b>${n.files}</b> file${n.files === 1 ? "" : "s"} <b>+${n.added}</b> <b>−${n.removed}</b>`) : ""}</div>`;
}

const taskLine = (t) => `<div class="rep-t${picked === t.id ? " on" : ""}" data-rep-task="${t.id}" role="button" tabindex="0">
  <span class="muted t-s">#${t.id}</span> ${esc(t.short || clip(t.title, 90))}${t.status === "done" ? "" : ` <span class="muted">(${esc(t.status.replace("_", " "))})</span>`}
  ${t.risk ? chip("c-p0", esc(t.risk)) : ""}${t.changed ? `<div class="muted t-s">${esc(t.changed)}</div>` : ""}</div>`;

export function leftHtml(r, render = md || asText) {
  const rel = r.release, rv = rel.review;
  const state = rel.status !== "open" ? rel.status.replace("_", " ") : rel.cut_at ? `cut ${ago(rel.cut_at)}` : "collecting";
  const scope = `<div class="rep-scope">${chip("c-goal", "Release")} <b>#${rel.id}</b> of ${esc(rel.target || "")}
    <span class="muted t-s">${esc(state)}${rel.cut_rev ? " · " + esc(rel.cut_rev) : ""}${rv ? ` · review <span class="link" data-open="${rv.id}">#${rv.id}</span> ${esc(rv.status.replace("_", " "))}` : ""}</span></div>`;
  // The summary leads (#6493): what changes for a user by area, the risks and what to check, written in Markdown by
  // the session of the summary task at the cut. Each #<id> of a task opens it on the right.
  const s = r.summary, written = s.by !== "maxpm";
  const sum = `<div class="rep-sum">${written ? linkTasks(render(s.text), r.tasks) : asText(s.text)}
    <div class="muted t-s">${written ? `The summary of ${esc(s.by)}${s.at ? ", " + esc(ago(s.at)) : ""}`
      : "Written by MaximizePM from its records."}</div></div>`;
  const groups = r.groups.map(g => `<details class="rep-g${g.risk ? " risk" : ""}"${g.risk || r.groups.length <= 3 ? " open" : ""}>
    <summary><b>${esc(g.name)}</b> <span class="muted t-s">${g.items.length} task${g.items.length === 1 ? "" : "s"}</span>
      ${g.line && !g.risk ? `<div class="muted t-s">${esc(g.line)}</div>` : ""}</summary>
    ${g.items.map(i => taskLine(r.tasks[i])).join("")}</details>`).join("");
  // The task list comes below the summary, closed while a written summary leads.
  const n = Object.keys(r.tasks).length;
  const list = groups ? `<details class="rep-tasks"${written ? "" : " open"}><summary><b>The tasks</b>
      <span class="muted t-s">${n} task${n === 1 ? "" : "s"}, by area</span></summary>${groups}</details>` : "";
  // The code of the whole release (#6038): one line that opens it on the right, and the commits that name no task.
  const c = r.code;
  const code = c ? `<div class="rep-t${picked === "code" ? " on" : ""}" data-rep-code="1" role="button" tabindex="0">
      <b>The code of the whole release</b><div class="muted t-s">${c.commits} commit${c.commits === 1 ? "" : "s"}, ${c.files} file${c.files === 1 ? "" : "s"}, from ${esc(c.base_label)} to ${esc(c.head_label)}</div></div>` : "";
  const untasked = c && c.untasked.length ? `<details class="rep-g risk"><summary><b>Commits with no task</b>
      <span class="muted t-s">${c.untasked.length}</span><div class="muted t-s">No queue item names them: read them in the code</div></summary>
      ${c.untasked.map(u => `<div class="rep-t"><code>${esc(u.sha)}</code> ${esc(u.subject)}</div>`).join("")}</details>` : "";
  return scope + sum + numbersHtml(r.numbers) + code + untasked + (list || `<div class="muted">This release ships no task yet.</div>`);
}

// Each #<id> in the summary's HTML (sanitized) that names a task of the release becomes a link that shows the task.
export function linkTasks(html, tasks) {
  return html.replace(/(^|[^\w&#"/])#(\d{1,7})\b/g, (m, pre, id) => tasks[id]
    ? `${pre}<span class="link" data-rep-task="${id}" role="button" tabindex="0">#${id}</span>` : m);
}

// The right side for the code of the whole release: its files with their tasks, and the Changes view of the range.
export function codeHtml(c, tasks) {
  const tabs = `<div class="rep-tabs" role="tablist">${button("Files", { cls: tab !== "changes" ? "on" : "", data: { "rep-tab": "files" }, attrs: `role="tab" aria-selected="${tab !== "changes"}"` })}${button("Changes", { cls: tab === "changes" ? "on" : "", data: { "rep-tab": "changes" }, attrs: `role="tab" aria-selected="${tab === "changes"}"` })}</div>`;
  const ref = (n) => `<span class="link" ${tasks[n] ? `data-rep-task="${n}"` : `data-open="${n}"`}>#${n}</span>`;
  const files = c.file_list.map(f => `<div class="rep-file"><span class="rep-path">${esc(f.path)}</span>
      <span class="muted t-s">+${f.added} −${f.removed}</span>
      <span class="t-s">${f.tasks.length ? f.tasks.map(ref).join(" ") : `<span class="muted">no task</span>`}</span></div>`).join("");
  return tabs + `<div class="rep-pane" ${tab !== "changes" ? "" : "hidden"}>
      <h3>The whole release</h3>
      <div class="muted t-s">${esc(c.base_label)}${c.base_label === c.base ? "" : ` (${esc(c.base.slice(0, 10))})`} to ${esc(c.head_label)}: ${c.commits} commits, ${c.files} files, +${c.added} −${c.removed}</div>
      <div class="rep-files">${files || `<div class="muted">No file changed.</div>`}</div></div>
    <div class="rep-pane rep-code" ${tab === "changes" ? "" : "hidden"}></div>`;
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
  right.innerHTML = `<div class="rep-back">${button("‹ Back", { data: { "rep-back": 1 } })}</div>`
    + (picked === "code" && r.code ? codeHtml(r.code, r.tasks) : taskHtml(picked ? r.tasks[picked] : null));
  // While a diff shows, the task list takes less of the width, so the code lines fit (#6069).
  const diff = picked === "code" ? tab === "changes" : !!picked && tab === "code";
  el.querySelector(".rep-grid").classList.toggle("picked", !!picked);
  el.querySelector(".rep-grid").classList.toggle("diff", diff);
  if (picked === "code" && tab === "changes") renderChanges(right.querySelector(".rep-code"), { item: rel.id, range: true });
  else if (picked && picked !== "code" && tab === "code") renderChanges(right.querySelector(".rep-code"), { item: picked });
}

export async function openReport(id) {
  if (!el) {
    el = document.createElement("div");
    el.id = "reportDlg";
    el.className = "hidden";
    el.innerHTML = `<div class="box"><div class="dlg-head"><h2>Release</h2><span class="spacer"></span>
      ${button("Discuss with the manager", { data: { "rep-discuss": 1 }, title: "Send to the manager" })}
      ${wideButton()}${button("✕", { kind: "icon", label: "Close", data: { "rep-close": 1 } })}</div>
      <div class="rep-grid"><div class="rep-l"></div><div class="rep-r"></div></div></div>`;
    document.body.append(el);
    dlg = makeDialog(el, { draft: false, wide: "reportWide" });
    el.addEventListener("click", onClick);
    el.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.target.dataset.repTask || e.target.dataset.repCode)) onClick(e); });
  }
  picked = null; tab = "task";
  el.querySelector(".rep-l").innerHTML = `<div class="muted">Reading the release…</div>`;
  el.querySelector(".rep-r").innerHTML = "";
  dlg.open();
  try {
    if (!md) md = (await import("./chat.js")).markdown;
    data = await getJson(`api/report/${id}`);
  }
  catch (e) { el.querySelector(".rep-l").innerHTML = `<div class="muted">${esc(e.message)}</div>`; return; }
  // A phone starts at the summary; a desktop shows the code of the whole release beside it (#6493), or the first task.
  const first = data.code ? "code" : data.groups.length ? data.groups[0].items[0] : null;
  picked = matchMedia("(max-width: 759px)").matches ? null : first;
  tab = first === "code" ? "files" : "task";
  draw();
}

async function onClick(e) {
  const t = e.target;
  if (t.closest("[data-rep-close]")) return dlg.close();
  if (t.closest("[data-open]")) return dlg.close();  // the item opens in the drawer
  const task = t.closest("[data-rep-task]");
  if (task) { picked = +task.dataset.repTask; tab = "task"; return draw(); }
  if (t.closest("[data-rep-code]")) { picked = "code"; tab = "files"; return draw(); }
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
