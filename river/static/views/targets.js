// The Releases view of Plan (the Targets tab's until #5102 step 6): one card for each deploy target of the scope's
// projects (#6033, mark 2026-11-16 'this UI is really bad'). The head names the target, its owner and its projects;
// each open release is one state line in plain words, the button Open report (the review report, #6003: the main
// action here), and its tasks as item rows grouped like the report (goal, then project), folded after a few. Release
// now is a plain button that opens a small dialog for the reason; Deploy now, the hooks and the past deploys stay.
import { $, esc, toast, act, ago, left, clip } from "../lib.js";
import { chip, ownerChip } from "../components/chip.js";
import { button, iconButton } from "../components/button.js";
import { itemRow } from "../components/itemRow.js";
import { makeDialog } from "../components/dialog.js";
import { field, textarea, actions } from "../components/form.js";
import { agentStart } from "../components/agentStart.js";
import { chooseLaunch, launchArgs } from "../components/launchDialog.js";
import { hooksHtml } from "../components/targetHooks.js";
import { S, scope } from "../page.js";

const FOLD = 5;                 // the tasks a release shows before Show all
const shown = new Set();        // the releases whose whole list is open (by deploy id), over a refresh
const plural = (n, one, many = one + "s") => `${n} ${n === 1 ? one : many}`;

// A time a person reads: "Tue 21:43" within a week, else "Nov 30 21:43".
function when(iso) {
  const d = new Date(iso), soon = d - Date.now() < 6 * 864e5;
  return d.toLocaleString([], soon ? { weekday: "short", hour: "2-digit", minute: "2-digit" }
    : { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

// The state of one release in plain words: what it is now, and when it can go out.
function stateLine(t, d) {
  const r = t.release || {}, n = d.ships.length, tasks = plural(n, "task");
  const rv = d.review, rvWho = rv && rv.assignee ? " by " + esc(rv.assignee) : "";
  if (d.status === "in_progress") return `<b>Going out now</b>: ${tasks}${d.assignee ? ` · ${esc(d.assignee)} deploys` : ""}`;
  if (d.status === "held") return `<b>Deploy paused</b>: ${tasks}${d.assignee ? ` · held by ${esc(d.assignee)}` : ""}`;
  if (d.ready) return `<b>Ready to go out</b>: ${tasks}`;
  if (d.cut_at) return `<b>Release cut ${ago(d.cut_at)}</b>: ${tasks}` + (rv ? (rv.status === "open" ? " · waits for its review"
    : ` · in review${rvWho}`) : "");
  const lead = `<b>Next release</b>: ${tasks}`;
  if (!n) return `<b>Collecting</b>: no task yet`;
  if (r.deploy === d.id && r.early) return `${lead} · goes out early (${esc(r.early)})`;
  if (r.deploy === d.id && r.waits && r.until) return `${lead} · can go out ${esc(when(r.until))} <span class="muted">(in ${left(r.until)})</span>`;
  if (r.deploy === d.id && r.waits && r.running) return `${lead} · goes out after release <span class="link" data-open="${r.running}">#${r.running}</span>`;
  return `${lead} · collecting`;
}

// The tasks of a release as item rows, grouped like its report: each goal, then each project. Not-done tasks say so.
function taskRows(d) {
  const groups = new Map();
  for (const x of d.ships) {
    const key = x.goals && x.goals.length ? "g:" + x.goals[0] : "p:" + x.project;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(x);
  }
  const keys = [...groups.keys()].sort((a, b) => (a[0] === "g" ? 0 : 1) - (b[0] === "g" ? 0 : 1));  // goals first
  const all = shown.has(d.id) || d.ships.length <= FOLD + 1;
  let left_ = all ? Infinity : FOLD, out = "";
  for (const key of keys) {
    if (left_ <= 0) break;
    const xs = groups.get(key), take = xs.slice(0, left_); left_ -= take.length;
    out += `<div class="rel-g"><b>${esc(key.slice(2))}</b><span class="muted t-s">${key[0] === "g" ? "goal" : "project"} · ${plural(xs.length, "task")}</span></div>`
      + take.map(x => itemRow(x, "", { chips: x.status === "done" ? "" : chip(x.status === "open" ? "c-waiting" : "c-" + x.status,
        esc(x.status === "open" ? "not done" : x.status.replace("_", " "))) })).join("");
  }
  const more = d.ships.length > FOLD + 1 ? button(all ? "Show fewer" : `Show all ${d.ships.length}`, { data: { relall: d.id } }) : "";
  return `<div class="rel-tasks">${out}</div>${more ? `<div class="rel-more">${more}</div>` : ""}`;
}

// One open release of a target: the state line with Open report, then its tasks.
function releaseHtml(t, d, first) {
  const report = d.ships.length ? button("Open report", { kind: first ? "primary" : "", data: { report: d.id },
    title: "What this release ships" }) : "";
  return `<section class="rel-rel">
      <div class="rel-state"><div class="rel-line t-m">${stateLine(t, d)}
        <div class="muted t-s">Release <span class="link" data-open="${d.id}">#${d.id}</span>${d.review ? ` · review <span class="link" data-open="${d.review.id}">#${d.review.id}</span>` : ""}</div></div>
        ${report}</div>
      ${d.ships.length ? taskRows(d) : ""}</section>`;
}

// A cadence as words: "1w" is "a week", "2h" is "every 2 hours"; another form stays as it is.
function cadenceWords(c) {
  const m = /^(\d+)\s*([mhdw])$/.exec(String(c).trim()), unit = m && { m: "minute", h: "hour", d: "day", w: "week" }[m[2]];
  if (!m) return "every " + c;
  return +m[1] === 1 ? "a " + unit : `every ${m[1]} ${unit}s`;
}

// How the target releases, only what a person needs: who approves, the cadence. A gate that is not set says nothing.
function rulesLine(t) {
  const r = t.release || {}, a = r.approval || {}, bits = [];  // [text, hover]
  if (a.approval === "person") bits.push([a.review ? "goes out after its review and your sign-off"  // #6039
    : "you approve each release before it goes out"]);
  else if (a.approval === "review") bits.push(["goes out when its review passes"]);
  if (r.cadence) bits.push(["at most one release " + cadenceWords(r.cadence), r.cadence_reason]);
  if (t.monitor) bits.push(["watched after each deploy", t.monitor]);
  if (!bits.length) return "";
  bits[0][0] = bits[0][0].charAt(0).toUpperCase() + bits[0][0].slice(1);
  return `<div class="muted t-s">${bits.map(([s, tip]) => tip ? `<span title="${esc(tip)}">${esc(s)}</span>` : esc(s)).join(" · ")}</div>`;
}

// The buttons under the releases: Deploy now when nothing holds the release back; Release now when the cadence or a
// running release holds it (a person gives the reason in its dialog).
function actionsHtml(t) {
  if (t.release && t.release.waits && t.release.deploy) return `<div class="rel-acts">${button("Release now…", { data: { relnow: t.name }, title: "Release sooner" })}</div>`;
  if (!t.pending.some(d => d.status === "open" && d.ships.length)) return "";
  return `<div class="rel-acts">${agentStart(S.launch_agents, [
    { label: "Deploy now", attrs: `data-deploy="${esc(t.name)}"`, title: "Start the deploy now" },
    { label: "Review and deploy", attrs: `data-deploy="${esc(t.name)}" data-review="1"`, title: "Review first, then deploy" }])}</div>`;
}

// The deploys that are done, folded: one short line each, with its report.
function historyHtml(t) {
  const H = t.history || [];
  if (!H.length) return "";
  const rows = H.map(h => `<div class="rel-past t-s"${h.output ? ` title="${esc(h.output)}"` : ""}><span class="link" data-open="${h.id}">#${h.id}</span>
    <span class="muted">${ago(h.closed_at)}${h.done_by ? " · " + esc(h.done_by) : ""} · ${plural(h.ships.length, "task")}</span>
    ${h.ships.length ? `<span class="link" data-report="${h.id}">Report</span>` : ""}</div>`).join("");
  return `<details data-keep="deploys-${esc(t.name)}"><summary>Past deploys · last ${ago(H[0].closed_at)}</summary>${rows}</details>`;
}

function cardHtml(t) {
  const owner = t.owner && (S.sessions || []).includes(t.owner)
    ? `<span class="link" data-session-go="${esc(t.owner)}" title="Open the owner's session">${ownerChip(t.owner)}</span>` : ownerChip(t.owner);
  const P = t.project_names || [];
  const projects = !P.length ? "" : `<span class="muted t-s" title="${esc(P.join(", "))}">${P.length > 3 ? P.length + " projects" : P.map(esc).join(", ")}</span>`;
  const rels = t.pending.length ? t.pending.map((d, i) => releaseHtml(t, d, i === 0)).join("")
    : `<div class="muted t-m">No open release. A task joins the next one with <code>maxpm done &lt;id&gt; --ship</code>.</div>`;
  return `<article class="rel">
      <div class="rel-h"><h3${t.description ? ` title="${esc(t.description)}"` : ""}>${esc(t.name)}</h3>${owner}${projects}</div>
      ${rulesLine(t)}
      ${rels}
      ${actionsHtml(t)}
      <div class="rel-fold">${hooksHtml(t, S.hook_events)}${historyHtml(t)}</div></article>`;
}

export function renderTargets() {
  const T = (S.targets || []).filter(t => !scope.project || (t.project_names || []).includes(scope.project));
  const box = $("#targets");
  const open = new Set([...box.querySelectorAll("details[open]")].map(d => d.dataset.keep));  // a fold stays open over a refresh
  box.innerHTML = T.map(cardHtml).join("") || `<div class="muted" title='maxpm target add &lt;name&gt;'>No deploy targets${scope.project ? " for " + esc(scope.project) : ""}.</div>`;
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
  const t = (S.targets || []).find(x => x.name === name) || {}, r = t.release || {};
  relBox.innerHTML = `<div class="dlg-head"><h2>Release ${esc(name)} now?</h2>${iconButton("✕", "Close", { data: { relclose: "1" } })}</div>
    <p class="t-m">${r.until ? `The next release can go out ${esc(when(r.until))} (in ${left(r.until)}).`
      : r.running ? `Release #${r.running} runs now; the next one starts after it.` : "This release waits."}
      A release sooner needs a reason; it goes in the release's history.</p>
    <div class="form">${field("Why can it not wait?", textarea({ id: "relWhy", rows: 3, placeholder: "A fix for a defect in production" }, ""))}</div>
    ${actions(button("Cancel", { data: { relclose: "1" } }) + button("Release now", { kind: "primary", data: { relgo: "1" } }))}`;
  relDlg.open();
  relBox.querySelector("#relWhy").focus();
}

// A click on this view's controls; true when it was one (the frame then stops).
export function onClick(t) {
  const el = (k) => t.closest && t.closest(`[data-${k}]`);
  const all = el("relall");
  if (all) { const id = +all.dataset.relall; shown.has(id) ? shown.delete(id) : shown.add(id); renderTargets(); return true; }
  const now = el("relnow");
  if (now) { openReleaseNow(now.dataset.relnow); return true; }
  if (el("relclose")) { relDlg.close(); return true; }
  if (el("relgo")) return run(async () => {
    const reason = ($("#relWhy").value || "").trim();
    if (!reason) return toast("Say why this release cannot wait", true);
    try { await act("release_now", { target: relName, reason }); } catch (e) { return; }
    relDlg.close();
    toast(`The release of ${relName} can start now`);
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
