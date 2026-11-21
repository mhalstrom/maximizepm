// The Triggers tab of a pipeline's own page (#6150; canvas deploy-tab-mockup picture 3; mark 2026-11-18: "same with
// triggers", each pipeline has its own rules, and "the generic componenets are what the triggers are hooked to").
// Each rule: when (a stage, any stage, or any stage of a tier, fails; a check fails after a deploy; a transition
// opens; a run starts or ends), what the agent does, where it runs, which keys its task gets; Edit, Turn off or on,
// Remove (in the edit dialog). Below them what the rules did lately. A person or the manager changes them (op
// pipeline_trigger, river/routes/pipeline.py; the command maxpm pipeline trigger). Reads GET
// api/pipeline-triggers/<name>. A deploy target (a default pipeline) keeps its fixed rules: defaultTriggersHtml.
import { $, esc, getJson, ago, act, toast } from "../lib.js";
import { button, iconButton } from "../components/button.js";
import { chip } from "../components/chip.js";
import { empty } from "../components/empty.js";
import { makeDialog } from "../components/dialog.js";
import { field, input, select, textarea, actions } from "../components/form.js";
import { askReason } from "./pipeline.js";
import { ticksHtml, tick } from "./runners.js";

let T = null, sig = "", dlg = null, dlgBox = null, editing = null;

// Draw the rules of the pipeline name into host (the #dpPage of deploy.js); force: draw even when nothing changed.
export async function drawTriggers(name, host, force) {
  const of = "triggers:" + name;
  if (host.dataset.of !== of) { host.dataset.of = of; host.innerHTML = `<div class="tg-view"></div>`; sig = ""; }
  let d;
  try { d = await getJson("api/pipeline-triggers/" + encodeURIComponent(name)); }
  catch (e) { host.querySelector(".tg-view").innerHTML = empty(esc(e.message)); return; }
  if (host.dataset.of !== of) return;
  T = d;
  const s = JSON.stringify([d, Math.floor(Date.now() / 60000)]);  // "3m ago" changes each minute
  if (s === sig && !force) return;
  sig = s;
  host.querySelector(".tg-view").innerHTML = viewHtml(d);
}

// A key as the rule gives it (#6153): a stage key goes only to a task of the stages that may read it; a runner's set
// (the model key) goes to every run there.
const keyWord = k => k.runner ? `${k.set} (runner ${k.runner})`
  : k.stages ? `${k.set} (${k.stages.join(", ") || "no live stage"} only)` : k.set;

function ruleHtml(r) {
  const list = r.key_list || r.keys.map(set => ({ set }));
  const keys = list.length ? `🔒 keys ${esc(list.map(keyWord).join(", "))}` : "no keys";
  return `<div class="dp-rule${r.enabled ? "" : " tg-off"}">
    <div class="tg-head"><b>${esc(r.when)}</b><span class="muted t-s">${esc(r.name)}</span>${r.enabled ? "" : chip("c-waiting", "off")}</div>
    <div class="t-m"><b>The agent</b> ${esc(r.does)}</div>
    <div class="t-s muted">Runs on ${esc(r.runner)} · ${keys}</div>
    ${r.enabled ? "" : `<div class="t-s c-warn">Off${r.off_by ? " by " + esc(r.off_by) : ""}${r.off_at ? " " + esc(ago(r.off_at)) : ""}: ${esc(r.off_reason || "")}</div>`}
    <div class="pl-bar">${button("Edit", { data: { "tg-edit": r.name }, title: "Change this rule" })}
      ${r.enabled ? button("Turn off", { kind: "warn", data: { "tg-off": r.name }, title: "Start no agent" })
        : button("Turn on", { data: { "tg-on": r.name }, title: "Act again from now" })}</div>
  </div>`;
}

function firedHtml(d) {
  if (!d.fired.length) return `<div class="muted t-m">No runs yet.</div>`;
  return `<div class="pl-table"><table><tr><th>When</th><th class="tg-nw">Rule</th><th>What happened</th><th>What it did</th></tr>
    ${d.fired.map(f => `<tr><td class="mono tg-nw" title="${esc(f.fired_at)}">${esc(ago(f.fired_at))}</td><td>${esc(f.rule)}</td>
      <td class="t-m">${esc(d.events[f.event] || f.event)}: ${esc(f.stage || "")}${f.rev ? ` <span class="pl-ver">${esc(f.rev.slice(0, 7))}</span>` : ""}</td>
      <td class="t-m">${esc(f.result || "acting")}</td></tr>`).join("")}</table></div>`;
}

// What the pipeline's agents may do with no permission prompt (#6154): at once, ask first (a card), never; and the
// steps that runs asked for lately, each with its card.
const ASK_STATE = { asked: ["c-human", "waiting"], yes: ["c-ready", "yes"], no: ["c-waiting", "no"] };
function permissionsHtml(d) {
  const P = d.permissions;
  if (!P) return "";
  const col = (xs) => xs.length ? xs.map(x => `<div class="mono t-s">${esc(x)}</div>`).join("") : `<span class="muted t-s">nothing</span>`;
  const asks = (P.asks || []).map(a => {
    const [cls, word] = ASK_STATE[a.state] || ["c-waiting", a.state];
    return `<tr><td class="mono t-s" data-label="Step">${esc(a.rule)}</td>
      <td data-label="Task">${button("#" + a.item, { cls: "plain", data: { "dp-card": a.item }, title: "Task" })}</td>
      <td data-label="Card">${a.card ? button("#" + a.card, { cls: "plain", data: { "dp-card": a.card }, title: "Details" }) : ""}</td>
      <td data-label="Answer">${chip(cls, word)}${a.decided_by ? ` <span class="muted t-s">${esc(a.decided_by)}</span>` : ""}</td></tr>`;
  }).join("");
  return `<div class="pl-table"><table class="pl-blocks"><tr><th>Allow</th><th>Ask</th><th>Deny</th></tr>
      <tr><td data-label="Allow">${col(P.allow)}</td><td data-label="Ask">${col(P.ask)}</td><td data-label="Deny">${col(P.never)}</td></tr></table></div>
    <div class="pl-bar">${button("Edit", { data: { "tg-perms": 1 }, title: "Edit permissions" })}
      ${P.default ? `<span class="muted t-s">default</span>` : ""}</div>
    ${asks ? `<div class="pl-table"><table class="pl-blocks"><tr><th>Asked for</th><th>Task</th><th>Card</th><th>Answer</th></tr>${asks}</table></div>` : ""}`;
}

// The dialog of the list: one rule a line in each part (op pipeline_trigger, action permit).
function openPerms() {
  if (!T || !T.permissions) return;
  const P = T.permissions;
  ensureDialog();
  const area = (id, xs, ph) => textarea({ id, rows: 4, placeholder: ph }, xs.join("\n"));
  dlgBox.innerHTML = `<div class="dlg-head"><h2>Permissions · ${esc(T.pipeline)}</h2>
      ${iconButton("✕", "Close", { data: { "tg-close": 1 } })}</div>
    <div class="form">
      ${field("Allow", area("tgAllow", P.allow, "Read\nBash(npm test:*)"))}
      ${field("Ask", area("tgAsk", P.ask, "Bash(./deploy.sh:*)"))}
      ${field("Deny", area("tgNever", P.never, "Bash(rm:*)"))}
    </div>
    <div class="muted t-s">One rule per line.</div>
    ${actions(button("Cancel", { data: { "tg-close": 1 } }) + button("Save", { kind: "primary", data: { "tg-perms-save": 1 } }))}`;
  dlg.open();
  dlgBox.querySelector("#tgAllow").focus();
}

function viewHtml(d) {
  const rules = d.rules.map(ruleHtml).join("") || empty("No rules yet.");
  return `<div class="dp-cards">${rules}</div>
    <div class="pl-bar">${button("Add rule", { kind: "primary", data: { "tg-add": 1 }, title: "Add rule" })}</div>
    <h3 class="tg-h">Runners</h3>${ticksHtml(d)}
    <h3 class="tg-h">Permissions</h3>${permissionsHtml(d)}
    <h3 class="tg-h">Recent runs</h3>${firedHtml(d)}`;
}

// The fixed rules of a deploy target: it has no pipeline of its own, so nothing here can change them.
export function defaultTriggersHtml(p) {
  const rule = (when, then) => `<div class="dp-rule"><div class="t-m"><b>When</b> ${when}</div><div class="t-m"><b>Then</b> ${then}</div></div>`;
  return `<div class="dp-cards">${rule("a release awaits approval", "Ask for approval.")}
    ${rule("a post-deploy check fails", "Open a fault task and start a session.")}</div>
    <div class="muted t-s">Built-in rules.</div>`;
}

// ---- the dialog that adds or changes a rule

function stageChoices(d) {
  const out = [["", "Any stage"], ...d.stages.map(s => ["stage:" + s, "Stage " + s])];
  for (const t of d.tiers || []) out.push(["tier:" + t, `Any ${t} stage`]);
  return out;
}

function ensureDialog() {
  if (!dlg) {
    const el = document.createElement("div");
    el.id = "tgDlg"; el.className = "hidden";
    el.innerHTML = '<div class="box"></div>';
    document.body.appendChild(el);
    dlgBox = el.firstElementChild;
    dlg = makeDialog(el);
    dlgBox.addEventListener("click", onDialogClick);
  }
}

function openRule(r) {
  if (!T) return;
  editing = r;
  ensureDialog();
  const where = r ? (r.stage ? "stage:" + r.stage : r.tier ? "tier:" + r.tier : "") : "";
  dlgBox.innerHTML = `<div class="dlg-head"><h2>${r ? `Rule ${esc(r.name)}` : "A new rule"} · ${esc(T.pipeline)}</h2>
      ${iconButton("✕", "Close", { data: { "tg-close": 1 } })}</div>
    <div class="form">
      ${field("When", select({ id: "tgEvent" }, Object.entries(T.events).map(([k, t]) => [k, t[0].toUpperCase() + t.slice(1)]),
        r ? r.event : "stage-failed"))}
      ${field("Which stage", select({ id: "tgWhere" }, stageChoices(T), where))}
      ${field("What the agent does", textarea({ id: "tgDoes", rows: 4, placeholder: "For a failure, when empty: find the cause and fix it" }, r ? r.does : ""))}
      ${field("Runs on", select({ id: "tgRunner" }, T.runners, r ? r.runner : T.runners[0]))}
      ${field("Keys its task gets", input({ id: "tgKeys", value: r ? r.keys.join(", ") : "", placeholder: "credential sets, comma-separated" }))}
    </div>
    ${actions((r ? button("Remove the rule", { kind: "warn", data: { "tg-remove": r.name }, title: "Remove this rule" }) : "")
      + button("Cancel", { data: { "tg-close": 1 } }) + button(r ? "Save" : "Add", { kind: "primary", data: { "tg-save": 1 } }))}`;
  dlg.open();
  dlgBox.querySelector("#tgEvent").focus();
}

async function onDialogClick(e) {
  if (e.target.closest("[data-tg-close]")) return dlg.close();
  if (e.target.closest("[data-tg-perms-save]")) {
    const lines = (id) => dlgBox.querySelector("#" + id).value.split("\n").map(x => x.trim()).filter(Boolean);
    const ok = await change({ action: "permit", allow: lines("tgAllow"), ask: lines("tgAsk"), never: lines("tgNever") }, "List saved");
    if (ok) dlg.close();
    return;
  }
  const rm = e.target.closest("[data-tg-remove]");
  if (rm) {
    dlg.close();
    return change({ action: "remove", rule: rm.dataset.tgRemove }, `Rule ${rm.dataset.tgRemove} removed`);
  }
  if (!e.target.closest("[data-tg-save]")) return;
  const v = (id) => dlgBox.querySelector("#" + id).value.trim();
  const where = v("tgWhere");
  const args = { event: v("tgEvent"), does: v("tgDoes"), runner: v("tgRunner"), keys: v("tgKeys"),
    stage: where.startsWith("stage:") ? where.slice(6) : "", tier: where.startsWith("tier:") ? where.slice(5) : "" };
  if (!editing && !["stage-failed", "check-failed"].includes(args.event) && !args.does) return toast("Say what the agent does", true);
  if (!editing) { if (!args.stage) delete args.stage; if (!args.tier) delete args.tier; }
  const ok = await change(editing ? { action: "edit", rule: editing.name, ...args } : { action: "add", ...args },
    editing ? `Rule ${editing.name} saved` : "Rule added");
  if (ok) dlg.close();
}

async function change(args, done) {
  try { await act("pipeline_trigger", { pipeline: T.pipeline, ...args }); } catch (e) { return false; }
  toast(done);
  const host = $("#dpPage");
  if (host && T) drawTriggers(T.pipeline, host, true);
  return true;
}

$("#tab-deploy").addEventListener("click", (e) => {
  const t = e.target.closest("[data-tg-add],[data-tg-edit],[data-tg-off],[data-tg-on],[data-rn-tick],[data-tg-perms]");
  if (!t || !T) return;
  const d = t.dataset, rule = (name) => T.rules.find(r => r.name === name);
  if (d.tgPerms) return openPerms();
  if (d.rnTick) return tick(T.pipeline, d.rnTick, !!d.on, () => { const host = $("#dpPage"); if (host) drawTriggers(T.pipeline, host, true); });
  if (d.tgAdd) return openRule(null);
  if (d.tgEdit) return openRule(rule(d.tgEdit));
  if (d.tgOn) return change({ action: "on", rule: d.tgOn }, `Rule ${d.tgOn} is on`);
  if (d.tgOff) return askReason(`Turn off rule ${d.tgOff}?`, "It starts no agent until you turn it on again.", "Turn off",
    (reason) => change({ action: "off", rule: d.tgOff, reason }, `Rule ${d.tgOff} is off`));
});
