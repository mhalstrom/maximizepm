// The release card of a deploy target (#6033, mark 2026-11-16 'this UI is really bad'): the Targets tab until #5102
// step 6, then Plan > Releases, and since #6136 the Stages tab of the pipeline in Deploy that names the target (its
// default pipeline, or a pipeline whose environment names it). The head names the target, its owner and its projects;
// each open release is one state line in plain words, the button Open report (the review report, #6003: the main
// action here; its tasks are in the report, not repeated on the card, #6182). Release
// now is a plain button that opens a small dialog for the reason; Deploy now, the hooks and the past deploys stay.
import { $, esc, toast, act, ago, clip, actor } from "../lib.js";
import { openTarget } from "./deploy.js";
import { pickStage } from "./pipeline.js";
import { ownerChip } from "../components/chip.js";
import { button, iconButton } from "../components/button.js";
import { makeDialog } from "../components/dialog.js";
import { field, textarea, actions } from "../components/form.js";
import { agentStart } from "../components/agentStart.js";
import { chooseLaunch, launchArgs } from "../components/launchDialog.js";
import { hooksHtml } from "../components/targetHooks.js";
import { S } from "../page.js";

const plural = (n, one, many = one + "s") => `${n} ${n === 1 ? one : many}`;

// A time a person reads: "Tue 21:43" within a week, else "Nov 30 21:43".
function when(iso) {
  const d = new Date(iso), soon = d - Date.now() < 6 * 864e5;
  return d.toLocaleString([], soon ? { weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }  // #6206
    : { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
}

// The state of one release in plain words: what it is now, and when it can go out (#6486: canvas deploy-text-audit).
function stateLine(t, d) {
  const r = t.release || {}, n = d.ships.length, tasks = plural(n, "task");
  const rv = d.review, rvWho = rv && rv.assignee ? " · " + esc(rv.assignee) : "";
  if (d.status === "in_progress") return `<b>Releasing</b>: ${tasks}${d.assignee ? ` · ${esc(d.assignee)}` : ""}`;
  if (d.status === "held") return `<b>Paused</b>: ${tasks}${d.assignee ? ` · ${esc(d.assignee)}` : ""}`;
  if (d.ready) return `<b>Ready</b>: ${tasks}`;
  if (d.cut_at) return `<b>Cut ${ago(d.cut_at)}</b>: ${tasks}` + (rv ? (rv.status === "open" ? " · review waiting"
    : ` · in review${rvWho}`) : "");
  const lead = `<b>Next release</b>: ${tasks}`;
  if (!n) return `<b>No tasks yet</b>`;
  if (r.deploy === d.id && r.early) return `${lead} · Early (${esc(r.early)})`;
  if (r.deploy === d.id && r.waits && r.until) return `${lead} · next by itself ${esc(when(r.until))}`;
  if (r.deploy === d.id && r.waits && r.running) return `${lead} · After <span class="link" data-open="${r.running}">#${r.running}</span>`;
  return lead;
}

// One open release of a target: the state line with Report. Its tasks are in the report only (#6182, mark
// 2026-11-19: the card under the pipeline's tables listed the same tasks as Open report).
function releaseHtml(t, d, first) {
  const report = d.ships.length ? button("Report", { kind: first ? "primary" : "", data: { report: d.id },
    title: "Report" }) : "";
  return `<section class="rel-rel">
      <div class="rel-state"><div class="rel-line t-m">${stateLine(t, d)}
        <div class="muted t-s"><span class="link" data-open="${d.id}">#${d.id}</span>${d.review ? ` · review <span class="link" data-open="${d.review.id}">#${d.review.id}</span>` : ""}</div></div>
        ${report}</div></section>`;
}

// A cadence as one word: "1w" is "Weekly", "1d" "Daily", "2h" "Every 2h"; another form stays as it is.
function cadenceWords(c) {
  const m = /^(\d+)\s*([mhdw])$/.exec(String(c).trim());
  if (!m) return "Every " + c;
  return +m[1] === 1 && { d: "Daily", w: "Weekly", h: "Hourly" }[m[2]] || `Every ${m[1]}${m[2]}`;
}

// How the target releases, only what a person needs: who approves, the cadence. A gate that is not set says nothing.
function rulesLine(t) {
  const r = t.release || {}, a = r.approval || {}, bits = [];  // [text, hover]
  if (a.approval === "person") bits.push([a.review ? "Review + approval" : "Approval required"]);  // #6039
  else if (a.approval === "review") bits.push(["Review required"]);
  if (r.cadence) bits.push([cadenceWords(r.cadence), r.cadence_reason]);
  if (t.monitor) bits.push(["Watched", t.monitor]);
  if (!bits.length) return "";
  return `<div class="muted t-s">${bits.map(([s, tip]) => tip ? `<span title="${esc(tip)}">${esc(s)}</span>` : esc(s)).join(" · ")}</div>`;
}

// Deploy now when nothing holds the collected release back. Since #6189 it sits with every other release control at
// the top of the pipeline's page (views/pipeline.js headControls), not on this card; Release now has its own button
// there.
export function deployButtons(name) {
  const t = (S.targets || []).find(x => x.name === name);
  if (!t || (t.release && t.release.waits) || !(t.pending || []).some(d => d.status === "open" && d.ships.length)) return "";
  return `<span class="rel-acts">${agentStart(S.launch_agents, [
    { label: "Deploy now", attrs: `data-deploy="${esc(t.name)}"`, title: "Start the deploy now" },
    { label: "Review and deploy", attrs: `data-deploy="${esc(t.name)}" data-review="1"`, title: "Review first, then deploy" }])}</span>`;
}

// The deploys that are done, folded: one short line each, with its report.
function historyHtml(t) {
  const H = t.history || [];
  if (!H.length) return "";
  const rows = H.map(h => `<div class="rel-past t-s"${h.output ? ` title="${esc(h.output)}"` : ""}><span class="link" data-open="${h.id}">#${h.id}</span>
    <span class="muted">${ago(h.closed_at)} · ${plural(h.ships.length, "task")}</span>
    ${h.ships.length ? `<span class="link" data-report="${h.id}">Report</span>` : ""}</div>`).join("");
  return `<details data-keep="deploys-${esc(t.name)}"><summary>Past releases</summary>${rows}</details>`;
}

function cardHtml(t) {
  const owner = !t.owner ? "" : (S.sessions || []).includes(t.owner)
    ? `<span class="link" data-session-go="${esc(t.owner)}" title="Session">${ownerChip(t.owner)}</span>` : ownerChip(t.owner);
  const rels = t.pending.length ? t.pending.map((d, i) => releaseHtml(t, d, i === 0)).join("")
    : `<div class="muted t-m">No queued tasks.</div>`;
  return `<article class="rel">
      <div class="rel-h"><h3>${esc(t.name)}</h3>${owner}</div>
      ${rulesLine(t)}
      ${rels}
      <div class="rel-fold">${hooksHtml(t, S.hook_events)}${historyHtml(t)}</div></article>`;
}

// The targets whose cards show: those of the pipeline on screen (views/deploy.js sets them; none: no card).
let shownTargets = [];
export function showTargets(names) { shownTargets = names || []; renderTargets(); }

export function renderTargets() {
  const box = $("#targets");
  if (!S || !box) return;
  const T = (S.targets || []).filter(t => shownTargets.includes(t.name));
  const open = new Set([...box.querySelectorAll("details[open]")].map(d => d.dataset.keep));  // a fold stays open over a refresh
  const html = T.map(cardHtml).join("");
  if (box.dataset.sig === html) return;  // a redraw every 3 seconds would close a menu or lose a hover
  box.innerHTML = html; box.dataset.sig = html;
  box.querySelectorAll("details[data-keep]").forEach(d => { if (open.has(d.dataset.keep)) d.open = true; });
}

// Release now: a small dialog that asks why this release cannot wait (maxpm target release-now).
let relDlg = null, relBox = null, relName = "";
function openReleaseNow(name) {
  if (!relDlg) {
    const d = document.createElement("div");
    d.id = "relNowDlg"; d.className = "hidden";
    d.innerHTML = '<div class="box"></div>';
    document.body.appendChild(d);
    relBox = d.firstElementChild;
    relDlg = makeDialog(d);
  }
  relName = name;
  const t = (S.targets || []).find(x => x.name === name) || {}, r = t.release || {}, n = (r.ships || []).length;
  // #6189: what goes out (its tasks, with the report) and a reason already written, so one click and Release now start
  // it. #6486: the words of canvas deploy-text-audit.
  relBox.innerHTML = `<div class="dlg-head"><h2>Release ${esc(name)} now</h2>${iconButton("✕", "Close", { data: { relclose: "1" } })}</div>
    <p class="t-m">${n ? plural(n, "task") : "The queued tasks"}${r.deploy ? ` · <span class="link" data-report="${r.deploy}">Report</span>` : ""}
      ${r.until ? ` · Scheduled ${esc(when(r.until))}` : r.running ? ` · after #${r.running}` : ""}</p>
    <div class="form">${field("Reason", textarea({ id: "relWhy", rows: 2 }, `Manual release by ${actor() || "a person"}`))}</div>
    ${actions(button("Cancel", { data: { relclose: "1" } }) + button("Release now", { kind: "primary", data: { relgo: "1" } }))}`;
  relDlg.open();
  relBox.querySelector('[data-relgo]').focus();
}

// A click on this view's controls; true when it was one (the frame then stops).
export function onClick(t) {
  const el = (k) => t.closest && t.closest(`[data-${k}]`);
  const now = el("relnow");
  if (now) { openReleaseNow(now.dataset.relnow); return true; }
  if (el("relclose")) { relDlg.close(); return true; }
  if (el("relgo")) return run(async () => {
    const reason = ($("#relWhy").value || "").trim();
    if (!reason) return toast("Add a reason", true);
    try { await act("release_now", { target: relName, reason }); } catch (e) { return; }
    relDlg.close();
    toast("Release started");
    // #6189, #6486: show the release as it runs: the target's page, whose release card shows each step.
    pickStage(relName, "deploy"); openTarget(relName);
  });
  if (t.dataset.hook) return run(async () => {
    const name = t.dataset.target, event = t.dataset.hook;
    const has = ((S.targets.find(x => x.name === name) || {}).hooks || []).find(h => h.event === event);
    if (t.dataset.clear) {
      if (!confirm(`Remove the ${event} hook of ${name}?` + (has ? "\n" + has.command : ""))) return;
      return act("target_hook", { target: name, event, clear: true }).then(() => toast(`Hook ${event} of ${name} removed`)).catch(() => {});
    }
    const command = prompt(`The shell command that MaximizePM runs for ${name} when ${(S.hook_events || {})[event] || "the event " + event + " occurs"}`, has ? has.command : "");
    if (command === null || (has && command.trim() === has.command)) return;
    if (!command.trim()) return has ? toast(`The command is empty. To take the hook away, use remove`, true) : undefined;
    return act("target_hook", { target: name, event, command })
      .then(r => toast(`Hook ${event} of ${name} ${r.changed}: it runs in ${r.folder || "the folder of the command that causes the event"}, for at most ${r.timeout}`)).catch(() => {});
  });
  // Release via agent (#6189, mark 2026-11-19: 'one option should for sure be "Release Via Agent" which has an agent
  // handle it'): the chosen agent and model in a new session take the release; a release that waits for the cadence
  // starts now first (release_now, with the reason), as Release now does. A release that waits for the person's yes
  // shows Approve instead of this button (pipeline.js arrowNow).
  if (t.dataset.relagent) return run(async () => {
    // #6194: op release_agent (server.release_with_agent): with a deploy hook, the release starts and a new session
    // watches it and fixes what fails (the hook runs the release); with none, Deploy now.
    const name = t.dataset.relagent;
    const ch = await chooseLaunch(S, { title: "Release with an agent", go: "Start the agent",
      what: `release ${name}: a new agent session starts, reads the release report, follows the release's steps, and on a failure fixes the cause so the release runs again` });
    if (!ch) return;
    try {
      const r = await act("release_agent", { target: name, ...launchArgs(ch) });
      toast(r.task ? `Started ${r.agent || "an agent"} on #${r.task} to release ${name}` : r.alerted ? `Alerted the owner ${r.alerted}`
        : `Started ${r.agent || "an agent"} to release ${name}`);
      pickStage(name, "deploy"); openTarget(name);
    } catch (e) { /* toast shown */ }
  });
  if (t.dataset.deploy) return run(async () => {
    if (t.dataset.busy) return; t.dataset.busy = "1"; setTimeout(() => delete t.dataset.busy, 4000);
    const ch = await chooseLaunch(S, { title: t.dataset.review ? "Review and deploy" : "Deploy now", go: "Start",
      what: `${t.dataset.review ? "review and deploy" : "deploy"} ${t.dataset.deploy} (when the target has an owner, MaximizePM alerts the owner instead)` });
    if (!ch) return;
    try {
      const r = await act("deploy_now", { target: t.dataset.deploy, review: !!t.dataset.review, ...launchArgs(ch) });
      const what = `#${r.start.id} ${clip(r.start.title, 50)}`;
      toast(!r.ready ? `${what} waits on ${r.waits_on.map(w => "#" + w.id).join(", ")}; it starts when they are done`
        : r.alerted ? `${what} is ready; alerted the owner ${r.alerted}`
        : `Started ${r.agent} in ${r.project} to ${r.start.kind === "review" ? "review" : "deploy"} ${r.target}`);
    } catch (e) { /* toast shown */ }
    return;
  });
  return false;
}
const run = (f) => { f(); return true; };
