// The audit log of pipelines (#6144; canvas release-tracking task M, canvas deploy-tab-mockup picture 12; mark
// 2026-11-18: "we need an audit log and an emergency actions tab in the deploy tab"). One list of every change to a
// pipeline and its flow: when, who (a person, an agent session, a trigger, a hook, a forge), what, on which pipeline
// and stage or transition, the version, why, and the result. Deploy > Audit log shows every pipeline; a pipeline's
// own tab Audit log shows its lines only. Filters: pipeline, actor, time, and a search, read by the server
// (GET api/pipeline-audit, river/pipeline/audit.py), so a filter finds lines older than the newest 500.
// No line can be changed or deleted: Correct adds a new line that names the old one.
import { $, esc, getJson, ago, act, toast } from "../lib.js";
import { button } from "../components/button.js";
import { empty } from "../components/empty.js";
import { askReason } from "./pipeline.js";

const WHO = [["", "Every actor"], ["person", "People"], ["agent", "Agents"], ["trigger,hook", "Triggers and hooks"],
  ["forge", "Forges"]];
const SINCE = [["today", "Today"], ["7d", "7 days"], ["30d", "30 days"], ["all", "All"]];
const KIND_MARK = { person: "👤", agent: "🤖", trigger: "⚡", hook: "⚙", forge: "⚙" };
const EXPORT_MAX = 10000;
let timer = null;

const options = (list, on) => list.map(([v, t]) => `<option value="${esc(v)}"${v === on ? " selected" : ""}>${esc(t)}</option>`).join("");
const short = (rev) => rev && /^[0-9a-f]{12,}$/.test(rev) ? rev.slice(0, 7) : rev || "";

// The filters of the log in box, as the query of api/pipeline-audit.
function query(box, limit) {
  const q = new URLSearchParams();
  const one = box.dataset.of !== "null";
  const pipe = one ? box.dataset.of : box.querySelector(".au-pipe").value;
  if (pipe) q.set("pipeline", pipe);
  for (const [k, cls] of [["who", ".au-who"], ["since", ".au-since"], ["q", ".au-q"]]) {
    const v = (box.querySelector(cls).value || "").trim();
    if (v) q.set(k, v);
  }
  if (limit) q.set("limit", String(limit));
  return q.toString();
}

// Draw the log of every pipeline (name: null) or of one into box. A redraw keeps the filters and draws again only
// when the lines changed.
export async function drawAudit(name, box, force) {
  if (!box.querySelector(".dp-audit") || box.dataset.of !== String(name)) {
    box.dataset.of = String(name);
    box.innerHTML = `<div class="dp-audit"><div class="pl-bar">
      ${name ? "" : `<select class="au-pipe" aria-label="Pipeline"><option value="">All pipelines</option></select>`}
      <select class="au-who" aria-label="Who">${options(WHO, "")}</select>
      <select class="au-since" aria-label="Since">${options(SINCE, "30d")}</select>
      <input type="search" class="dp-q au-q" placeholder="Search: a version, a reason" aria-label="Search the log">
      <span class="muted t-s dp-an"></span><span class="au-grow"></span>
      ${button("Export as a file", { data: { "au-export": "1" }, title: "Save as CSV" })}</div>
      <div class="muted t-s au-note">Every change to ${name ? "this pipeline" : "a pipeline"} and its flow. No line can be changed or deleted; Correct adds a line.</div>
      <div class="dp-at"></div></div>`;
    box._sig = null;
  }
  const q = query(box);
  let A;
  try { A = await getJson("api/pipeline-audit" + (q ? "?" + q : "")); }
  catch (e) { box.querySelector(".dp-at").innerHTML = empty(esc(e.message)); return; }
  if (box.dataset.of !== String(name) || query(box) !== q) return;  // another pipeline or filter came meanwhile
  const sel = box.querySelector(".au-pipe");
  if (sel) {
    const names = (A.pipelines || []).join("\n");
    if (sel.dataset.names !== names) {
      const on = sel.value;
      sel.innerHTML = options([["", "All pipelines"], ...(A.pipelines || []).map(n => [n, n])], on);
      sel.dataset.names = names;
    }
  }
  // The page's refresh asks again every few seconds (with force): draw only when the lines, the filters or the minute
  // (the 'ago' times) changed, so a button under the pointer stays in its place.
  const s = q + "\n" + Math.floor(Date.now() / 60000) + "\n" + JSON.stringify(A);
  if (s === box._sig) return;
  box._sig = s;
  drawLines(box, A, name);
}

function drawLines(box, A, name) {
  const L = A.lines || [], one = !!name;
  const filtered = query(box).replace(/(^|&)pipeline=[^&]*/, "") !== "";
  box.querySelector(".dp-an").textContent = L.length ? `${L.length}${A.more ? "+" : ""} line${L.length === 1 ? "" : "s"}` : "";
  if (!L.length) {
    box.querySelector(".dp-at").innerHTML = empty(filtered ? "No line fits these filters."
      : "Nothing changed yet: each change of a pipeline, each action on it, each stage's end and each deploy goes here with who did it.");
    return;
  }
  // #6183: a column that no line of the list fills (On, Version, Why, Result) is left out: four empty columns took the
  // room of Did, which then wrapped into a narrow column.
  const has = { place: L.some(l => l.place), rev: L.some(l => l.rev), why: L.some(l => l.reason || l.until), result: L.some(l => l.result) };
  box.querySelector(".dp-at").innerHTML = `<div class="pl-table"><table class="au-table"><tr><th>When</th><th>Who</th>
    <th>Did</th>${one ? "" : "<th>Pipeline</th>"}${has.place ? "<th>On</th>" : ""}${has.rev ? "<th>Version</th>" : ""}${has.why ? "<th>Why</th>" : ""}${has.result ? "<th>Result</th>" : ""}<th></th></tr>
    ${L.map(l => `<tr class="${l.corrected_by ? "au-fixed" : ""}">
      <td class="mono" title="${esc(l.at)}">${esc(ago(l.at))}</td>
      <td><span aria-hidden="true">${KIND_MARK[l.kind] || ""}</span> ${esc(l.actor || "")}<div class="muted t-s">${esc(l.kind || "")}</div></td>
      <td><b>${esc(l.action || "")}</b><div class="t-s">${esc(l.change)}</div>
        ${l.corrected_by ? `<div class="au-corr t-s">corrected by line ${l.corrected_by.map(esc).join(", ")}</div>` : ""}</td>
      ${one ? "" : `<td>${button(esc(l.pipeline), { cls: "plain", data: { "dp-open": l.pipeline.split(", ")[0] }, title: "Open the pipeline" })}</td>`}
      ${has.place ? `<td class="t-s">${esc(l.place || "")}</td>` : ""}${has.rev ? `<td class="mono" title="${esc(l.rev || "")}">${esc(short(l.rev))}</td>` : ""}
      ${has.why ? `<td class="t-s">${esc(l.reason || "")}${l.until ? `<div class="muted">until <span title="${esc(l.until)}">${esc(new Date(l.until).toLocaleString([], { hourCycle: "h23" }))}</span></div>` : ""}</td>` : ""}
      ${has.result ? `<td class="t-s">${esc(l.result || "")}</td>` : ""}
      <td>${typeof l.id === "number" ? button("Correct", { cls: "plain", data: { "au-correct": l.id }, title: "Add a correction" }) : ""}</td>
    </tr>`).join("")}</table></div>${A.more ? `<div class="muted t-s">The newest ${L.length} lines. Narrow the time or search to find older ones; Export saves up to ${EXPORT_MAX}.</div>` : ""}`;
}

// The lines that the filters find, as a CSV file (up to EXPORT_MAX).
async function exportCsv(box) {
  let A;
  try { A = await getJson("api/pipeline-audit?" + query(box, EXPORT_MAX)); }
  catch (e) { return toast(e.message, true); }
  const cols = ["id", "at", "pipeline", "actor", "kind", "action", "change", "place", "rev", "reason", "until", "result",
    "corrects"];
  const cell = (v) => { const t = v === null || v === undefined ? "" : String(v); return /[",\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t; };
  const text = [cols.join(","), ...A.lines.map(l => cols.map(c => cell(l[c])).join(","))].join("\n") + "\n";
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: "text/csv" }));
  a.download = `audit-${box.dataset.of !== "null" ? box.dataset.of : "pipelines"}-${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  toast(`Saved ${A.lines.length} line${A.lines.length === 1 ? "" : "s"}` + (A.more ? ` (the newest ${EXPORT_MAX})` : ""));
}

const host = $("#tab-deploy");
host.addEventListener("input", (e) => {
  const box = e.target.closest(".dp-audit");
  if (!box || !e.target.classList.contains("au-q")) return;
  clearTimeout(timer);
  timer = setTimeout(() => drawAudit(box.parentElement.dataset.of === "null" ? null : box.parentElement.dataset.of, box.parentElement, true), 250);
});
host.addEventListener("change", (e) => {
  const box = e.target.closest(".dp-audit");
  if (!box || !e.target.matches("select")) return;
  const of = box.parentElement.dataset.of;
  drawAudit(of === "null" ? null : of, box.parentElement, true);
});
host.addEventListener("click", (e) => {
  const t = e.target.closest("[data-au-export],[data-au-correct]");
  if (!t) return;
  const holder = t.closest(".dp-audit").parentElement, of = holder.dataset.of;
  if (t.dataset.auExport) return exportCsv(holder);
  const line = Number(t.dataset.auCorrect);
  askReason(`Correct line ${line}`, "The line stays as it is. A new line says what is wrong in it and what is right, with your name.",
    "Add the correction", async (text) => {
      try { await act("pipeline_correct", { line, text }); } catch (err) { return; }
      toast(`Line ${line} has a correction`);
      drawAudit(of === "null" ? null : of, holder, true);
    });
});
