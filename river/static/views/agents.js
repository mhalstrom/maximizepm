// Agents on releases (#6149; canvas release-tracking task D, canvas deploy-tab-mockup pictures 1, 2 and 4; mark
// 2026-11-18: "a standardized view for human monitoring of agents working on deployment tasks", "I want left to right
// view"). Deploy > Agents shows each pipeline that has work now, left to right: under each stage its faults and the
// agents that work on them, each agent as a card (state, present step, where it runs and with which keys, what
// started it, its proof, its cost; Watch, Open the task, Stop; Yes and No for its open question: a question comes
// here as a card, never as a prompt in a terminal). The stage cards (views/pipeline.js) and the rows of Deploy >
// Pipelines show the same as small marks (stageMarks). The data is each stage's work in GET api/deploy and
// api/pipeline/<name> (river/pipeline/links.py stage_work).
import { $, esc, act, toast } from "../lib.js";
import { button } from "../components/button.js";
import { empty } from "../components/empty.js";
import { nav } from "../page.js";
import { askReason } from "./pipeline.js";

const ST_WORD = { run: "running", ok: "passed", human: "human", wait: "queued" };  // the pl-st colors of a state
const NO_WORK = { faults: [], agents: [] };
const short = (rev) => rev && /^[0-9a-f]{12,}$/.test(rev) ? rev.slice(0, 7) : rev || "";

// The small marks of a stage: ⚠ each open fault, then 🤖 each agent that works there (colored by its state).
export function stageMarks(work, cls = "") {
  const w = work || NO_WORK;
  const faults = w.faults.filter(f => !["done", "dropped"].includes(f.status));
  return faults.map(f => `<span class="ag-mark ag-fault-mark ${cls}" title="Fault #${f.id}: ${esc(f.title)}">⚠ #${f.id}</span>`).join("")
    + w.agents.filter(a => a.st !== "ok").map(a => `<span class="ag-mark ag-${esc(a.st)} ${cls}" title="${esc(a.name)}: ${esc(a.state)}">🤖 ${esc(a.name)}</span>`).join("");
}

const hasWork = (p) => p.stages.some(s => s.work && (s.work.faults.length || s.work.agents.length));

function faultCard(f) {
  const closed = ["done", "dropped"].includes(f.status);
  return `<div class="ag-fault${closed ? " ag-closed" : ""}">
    <div><b>⚠ fault #${f.id}</b>${closed ? ` <span class="t-s">${esc(f.status)}</span>` : ""}</div>
    <div class="t-s">${esc(f.title)}${f.rev ? ` · <span class="pl-ver">${esc(short(f.rev))}</span>` : ""}</div>
    ${f.nobody && !closed ? `<div class="t-s">Nobody works on it yet.</div>` : ""}
    <div>${button("Open", { cls: "plain", data: { "ag-item": f.id }, title: "Open the fault task" })}</div></div>`;
}

function agentCard(a) {
  const done = a.st === "ok", gone = ["gone", "stopped"].includes(a.live);
  const q = a.question;
  return `<div class="ag-card">
    <div class="ag-head"><b>🤖 ${esc(a.name)}</b>${a.cost != null ? `<span class="muted t-s">$${a.cost.toFixed(2)}</span>` : ""}</div>
    <span class="pl-st ${ST_WORD[a.st] || "none"}">● ${esc(a.state)}</span>
    ${a.step && !done ? `<div class="t-s">${esc(a.step)}</div>` : ""}
    <div class="muted t-s">task #${a.item}: ${esc(a.title)}</div>
    <div class="muted t-s">runs on ${esc(a.where)}${a.how ? " · " + esc(a.how) : ""}</div>
    <div class="muted t-s">keys: ${a.keys.length ? a.keys.map(esc).join(", ") : "no secret set granted"}</div>
    <div class="muted t-s">started by ${esc(a.started_by)}</div>
    ${a.proof ? `<div class="t-s">proof: ${esc(a.proof)}</div>` : ""}
    ${q ? `<div class="ag-q"><div class="t-s">asks: ${esc(q.body)}</div>
      <div class="ag-acts">${button("Yes", { kind: "primary", data: { "ag-answer": q.id, "ag-word": "yes" }, title: "Answer yes" })}
        ${button("No", { kind: "warn", data: { "ag-answer": q.id, "ag-word": "no" }, title: "Answer no" })}</div></div>` : ""}
    <div class="ag-acts">${gone ? "" : button("Watch", { data: { "ag-watch": a.name }, title: "Open its session" })}
      ${button("Task", { cls: "plain", data: { "ag-item": a.item }, title: "Task" })}
      ${done || gone ? "" : button("Stop", { kind: "warn", data: { "ag-stop": a.name }, title: "Stop" })}</div></div>`;
}

function arrowChip(s) {
  const t = s.transition || {};
  const word = t.state && t.state !== "open" ? t.state_text || t.state : t.text || "automatic";
  return `<div class="ag-arrow"><span class="t-s">${t.state && t.state !== "open" ? "⛔" : "→"} ${esc(word)}</span></div>`;
}

function laneHtml(p) {
  return `<div class="ag-lane"><div class="ag-lhead">${button(esc(p.name), { cls: "plain", data: { "dp-open": p.name }, title: "Open" })}
      ${p.default ? "" : `<span class="muted t-s">${esc(p.source.label)}</span>`}</div>
    <div class="ag-row">${p.stages.map((s, i) => {
      const w = s.work || NO_WORK;
      return (i ? arrowChip(s) : "") + `<div class="ag-col">
        <div class="ag-stage"><b class="t-m">${esc(s.name)}</b>
          <span class="pl-st ${esc(s.state)}">● ${esc(s.state === "none" ? "no run" : s.state)}${s.version ? " · " + esc(short(s.version)) : ""}</span></div>
        ${w.faults.map(faultCard).join("")}${w.agents.map(agentCard).join("")}</div>`;
    }).join("")}</div></div>`;
}

// Deploy > Agents, from the pipelines of api/deploy.
export function agentsHtml(data) {
  const pipes = (data && data.pipelines) || [];
  const busy = pipes.filter(hasWork);
  const agents = busy.flatMap(p => p.stages.flatMap(s => (s.work || NO_WORK).agents));
  const now = agents.filter(a => a.st !== "ok"), asks = agents.filter(a => a.question);
  const idle = pipes.filter(p => !hasWork(p)).map(p => p.name);
  if (!busy.length) {
    return empty("No agents on releases.");
  }
  return `<div class="ag-top"><b>Release agents</b><span class="muted t-s">${now.length} active${asks.length ? ` · ${asks.length} waiting` : ""}</span></div>
    ${busy.map(laneHtml).join("")}
    ${idle.length ? `<div class="muted t-s">Idle: ${idle.map(esc).join(", ")}</div>` : ""}`;
}

$("#tab-deploy").addEventListener("click", (e) => {
  const t = e.target.closest("[data-ag-item],[data-ag-watch],[data-ag-stop],[data-ag-answer]");
  if (!t) return;
  const d = t.dataset;
  if (d.agItem) return nav.openDrawer(Number(d.agItem));
  if (d.agWatch) return nav.openSession(d.agWatch);
  if (d.agAnswer) {
    return act("answer", { msg: Number(d.agAnswer), body: d.agWord }).then(() => toast(`Answered ${d.agWord}`)).catch(() => {});
  }
  askReason(`Stop ${d.agStop}`, "The session gets a stop request: it saves its work and hands its task back.", "Stop it",
    async (why) => { try { await act("stop_agent", { agent: d.agStop, reason: why }); toast(`${d.agStop} was asked to stop`); } catch (err) { /* act showed it */ } });
});
