// The Emergency tab of a pipeline's own page (#6145; canvas deploy-tab-mockup picture 13; mark 2026-11-18: "the
// emergency actions are per pipeline", "operators can, we audit who does them", and whether agents may be operators
// is "a pipeline by pipeline decision"). At the top the pipeline's operators and the setting 'agents may be
// operators' (a person changes them); then the actions: Stop this pipeline, Stop its agents, Move back to the last
// good version, Revoke its keys. Each shows what it acts on first, needs a reason, and writes a line to the audit
// log with the operator's name. Reads GET api/pipeline-emergency?pipeline=<name>; acts with the ops
// pipeline_emergency and pipeline_operators (river/routes/pipeline.py). The override is #6146.
import { $, esc, getJson, ago, act, toast } from "../lib.js";
import { button, iconButton } from "../components/button.js";
import { chip } from "../components/chip.js";
import { empty } from "../components/empty.js";
import { field, input, select, textarea, renderKeepingEdits } from "../components/form.js";

// [key, name, what it does, tooltip (1-4 words, p146)]
const ACTS = [
  ["stop", "Stop this pipeline", "Hold every stage. Nothing new starts, and a running stage stops after its present step.", "Stop the pipeline"],
  ["agents", "Stop its agents", "Ask each agent session that works on it now to stop. Its task goes back to the queue with a note.", "Stop its agents"],
  ["moveback", "Move back to the last good version", "Pick a stage: it runs the last version that passed there before, and the transition into it is blocked after it.", "Move a stage back"],
  ["revoke", "Revoke its keys", "Take a token or a credential set away from this pipeline, for one that leaked.", "Revoke a key"],
];
let E = null, sig = "", pick = "stop";

// Draw the Emergency tab of pipeline name into host (the #dpPage of deploy.js); force: draw even when nothing changed.
export async function drawEmergency(name, host, force) {
  const of = "emergency:" + name;
  if (host.dataset.of !== of) { host.dataset.of = of; host.innerHTML = `<div class="em-view"></div>`; sig = ""; pick = "stop"; }
  let d;
  try { d = await getJson("api/pipeline-emergency?pipeline=" + encodeURIComponent(name)); }
  catch (e) { host.querySelector(".em-view").innerHTML = empty(esc(e.message)); return; }
  if (host.dataset.of !== of) return;
  E = d;
  const s = JSON.stringify([d, pick]);
  if (s === sig && !force) return;
  sig = s;
  renderKeepingEdits(host.querySelector(".em-view"), viewHtml(d), force);
}

function operatorsHtml(o) {
  const names = o.operators.map(x => `<span class="em-op">${chip(x.may_act ? "" : "c-waiting", `${x.kind === "agent" ? "🤖" : "👤"} ${esc(x.name)}`,
    x.may_act ? "May act" : "Agents may not act")}${iconButton("✕", `Remove ${x.name}`, { data: { "em-remove": x.name } })}</span>`).join("");
  return `<div class="pl-card"><h3>Operators</h3>
    <div class="pl-bar">${names || `<span class="muted t-m">None named: a person or the manager may press these.</span>`}</div>
    <div class="pl-bar">${input({ id: "emAddOp", placeholder: "a person or a session", "aria-label": "Name of an operator" })}
      ${button("Add an operator", { data: { "em-add": 1 }, title: "Add an operator" })}
      ${button(o.agents ? "Agents may be operators" : "Agents may not be operators", { pressed: o.agents, data: { "em-agents": 1 }, title: "Agents as operators" })}</div>
    <div class="muted t-s">Only a person changes this list and the setting. The audit log names the operator of each action.</div></div>`;
}

function actsOn(d) {
  if (pick === "stop") return d.stop;
  if (pick === "agents") return d.agents.length ? d.agents.map(a => `${a.name} on #${a.item} (${a.title}) under ${a.stage}: ${a.state}`)
    : ["No agent works on this pipeline now"];
  if (pick === "moveback") return d.moveback.filter(m => m.good).map(m => `${m.stage}: ${short(m.now) || "none"} → ${short(m.good)}`
    + (m.good_at ? ` (passed there ${ago(m.good_at)})` : "")).concat(["The transition into that stage is blocked after it, until a person unblocks it"]);
  return d.keys.length ? d.keys.map(k => `${k.key}: ${k.what}` + (k.others.length ? ` ⚠ ${k.others.join(", ")} use${k.others.length === 1 ? "s" : ""} it too and keep${k.others.length === 1 ? "s" : ""} it` : ""))
    .concat(["The secret itself stays: a person replaces it (maxpm secret put <name>) or deletes it"]) : ["This pipeline names no key"];
}
const short = (rev) => rev && /^[0-9a-f]{12,}$/.test(rev) ? rev.slice(0, 7) : rev || "";

function actionHtml(d) {
  const [, title, , tip] = ACTS.find(a => a[0] === pick);
  const backs = d.moveback.filter(m => m.good), can = pick === "moveback" ? backs.length : pick === "revoke" ? d.keys.length
    : pick === "agents" ? d.agents.length : !d.stopped;
  const extra = pick === "moveback" && backs.length ? field("Stage", select({ id: "emStage" }, backs.map(m => [m.stage, `${m.stage}: back to ${short(m.good)}`])))
    : pick === "revoke" && d.keys.length ? field("Token or key", select({ id: "emKey" }, d.keys.map(k => [k.key, k.key + (k.others.length ? " ⚠ shared" : "")]))) : "";
  return `<div class="pl-card dp-em-card"><h3>${esc(d.pipeline)}: ${esc(title)}</h3>
    <div class="t-s muted">It acts on:</div><ul class="t-m em-list">${actsOn(d).map(x => `<li>${esc(x)}</li>`).join("")}</ul>
    ${can ? `<div class="form">${extra}${field("Why? (required)", textarea({ id: "emWhy", rows: 2, placeholder: "what happened" }, ""))}</div>
      <div class="pl-bar">${button(esc(goWord(d)), { kind: "warn", data: { "em-go": pick }, title: tip })}</div>`
      : `<div class="muted t-m">${pick === "stop" ? "The pipeline is stopped: resume it from the banner." : "Nothing to act on now."}</div>`}</div>`;
}
function goWord(d) {
  return { stop: `Stop ${d.pipeline} now`, agents: "Stop these agents now", moveback: "Move back now", revoke: "Revoke now" }[pick];
}

function viewHtml(d) {
  const st = d.stopped;
  const banner = st ? `<div class="pl-banner">⛔ ${esc(d.pipeline)} is ${st.stop_runs ? "stopped" : "held"} by ${esc(st.by || "a person")}${st.at ? " " + esc(ago(st.at)) : ""}: ${esc(st.reason)}.
    ${st.stop_runs ? "Nothing new starts; a running stage stops after its present step." : "Nothing new starts."}
    ${button(`Resume ${esc(d.pipeline)}`, { kind: "primary", data: { "em-resume": 1 }, title: "Stages start again" })}</div>` : "";
  const picks = ACTS.map(([k, t, text, tip]) => button(`<b>${esc(t)}</b><span class="t-s muted">${esc(text)}</span>`,
    { kind: "card", cls: "pl-card em-pick", pressed: pick === k, data: { "em-pick": k }, title: tip })).join("");
  return `${banner}${operatorsHtml(d.operators)}<div class="em-picks">${picks}</div>${actionHtml(d)}
    <div class="muted t-s">The emergency actions take ${esc(d.operators.rule)}. The override (send a version straight to a stage) comes with its own task.</div>`;
}

async function emAct(op, args, done) {
  try { await act(op, { pipeline: E.pipeline, ...args }); } catch (e) { return; }
  if (done) toast(done);
  sig = "";
  const host = $("#dpPage");
  if (host && E) drawEmergency(E.pipeline, host, true);
}

$("#tab-deploy").addEventListener("click", (e) => {
  const t = e.target.closest("[data-em-pick],[data-em-go],[data-em-resume],[data-em-agents],[data-em-add],[data-em-remove]");
  if (!t || !E) return;
  const d = t.dataset;
  if (d.emPick) { pick = d.emPick; sig = ""; return drawEmergency(E.pipeline, $("#dpPage"), true); }
  if (d.emResume) return emAct("pipeline_emergency", { action: "resume" }, `${E.pipeline} goes on`);
  if (d.emAgents) return emAct("pipeline_operators", { agents: !E.operators.agents });
  if (d.emRemove) return emAct("pipeline_operators", { remove: [d.emRemove] });
  if (d.emAdd) {
    const name = ($("#emAddOp").value || "").trim();
    if (!name) return toast("Name a person or a session", true);
    return emAct("pipeline_operators", { add: [name] }, `${name} is an operator`);
  }
  if (d.emGo) {
    const why = ($("#emWhy").value || "").trim();
    if (!why) return toast("Say why", true);
    const args = { action: d.emGo, reason: why };
    if (d.emGo === "moveback") args.stage = $("#emStage").value;
    if (d.emGo === "revoke") args.key = $("#emKey").value;
    return emAct("pipeline_emergency", args, { stop: "Stopped", agents: "Asked to stop", moveback: "Moving back", revoke: "Revoked" }[d.emGo]);
  }
});
