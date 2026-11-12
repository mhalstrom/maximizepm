// The Releases view of Plan (the Targets tab's until #5102 step 6): each deploy target of the scope's projects, what it
// ships, Deploy now, its hooks and its deploys. Only what a person acts on shows; the rest is a hover or folded (#5082).
import { $, esc, toast, act, ago, left, clip, shortTitle } from "../lib.js";
import { ownerChip } from "../components/chip.js";
import { nyCard } from "../components/card.js";
import { button } from "../components/button.js";
import { agentStart } from "../components/agentStart.js";
import { chooseLaunch, launchArgs } from "../components/launchDialog.js";
import { hooksHtml } from "../components/targetHooks.js";
import { S, scope } from "../page.js";

// One short line of how a target releases (#5003, #5082): the gate, the cadence, what the deploy session watches.
// Each word carries its full text as a hover; only a gate that is not set shows how to set it.
function releaseLine(t) {
  const r = t.release || {}, a = r.approval || {}, n = esc(t.name), bits = [];
  bits.push(a.approval === "person" ? `<span title="A person approves each release">gate: person</span>`
    : a.approval === "review" ? `<span title="Ships when its review passes (set on ${esc(a.project)})">gate: review</span>`
    : `<span style="color:var(--warn)" title="Set it: maxpm config release_approval">gate: not set</span>`);
  if (r.cadence) bits.push(`<span title="${esc(r.cadence_reason || "At most one release in this time")}">every ${esc(r.cadence)}</span>`);
  if (t.monitor) bits.push(`<span title="After a deploy, watch: ${esc(t.monitor)}">watch</span>`);
  if (r.waits && r.text) bits.push(`<span title="${esc(r.text)}">${r.until ? "next in " + left(r.until) : "after #" + r.running}</span>`);
  return `<div class="muted t-s">${bits.join(" · ")}</div>`;
}

// The items of a release: the id, a short title, and the state only when it is not done.
const shipList = (s) => s.map(x => `<span class="link" data-open="${x.id}" title="${esc(x.title)}">#${x.id}</span> ${esc(shortTitle(x))}${x.status === "done" ? "" : ` <span class="muted">(${esc(x.status.replace("_", " "))})</span>`}`).join("; ");

function pendingHtml(d) {
  const state = d.ready ? "ready" : d.status === "open" ? (d.cut_at ? "cut " + ago(d.cut_at) : "collecting")
    : esc(d.status.replace("_", " ")) + (d.assignee ? " · " + esc(d.assignee) : "");
  const review = d.review ? ` · review <span class="link" data-open="${d.review.id}">#${d.review.id}</span>`
    + (d.review.status === "open" ? "" : ` (${esc(d.review.status.replace("_", " "))}${d.review.assignee ? " · " + esc(d.review.assignee) : ""})`) : "";
  return `<div class="st"><span class="link" data-open="${d.id}">#${d.id}</span> ${state}${review}${d.ships.length ? ": " + shipList(d.ships) : ""}</div>`;
}

// The deploys that are done, folded: the summary says when the last one was.
function historyHtml(t) {
  const H = t.history || [];
  if (!H.length) return "";
  const rows = H.map(h => `<div class="st t-s"${h.output ? ` title="${esc(h.output)}"` : ""}><span class="link" data-open="${h.id}">#${h.id}</span> ${ago(h.closed_at)}${h.done_by ? " · " + esc(h.done_by) : ""}${h.ships.length ? ": " + h.ships.map(x => `<span class="link" data-open="${x.id}" title="${esc(x.title)}">#${x.id}</span>`).join(" ") : ""}</div>`).join("");
  return `<details data-keep="deploys-${esc(t.name)}"><summary>Deploys · last ${ago(H[0].closed_at)}</summary>${rows}</details>`;
}

function actionsHtml(t) {
  if (t.release && t.release.waits) return `<div class="actions"><input id="relwhy-${esc(t.name)}" placeholder="Why now?" style="flex:1;min-width:var(--w-input-m)"> ${button("Release now", { data: { relnow: t.name }, title: "Start this release now" })}</div>`;
  if (!t.pending.some(d => d.status === "open" && d.ships.length)) return "";
  return `<div class="actions">${agentStart(S.launch_agents, [
    { label: "Deploy now", cls: "btn primary", attrs: `data-deploy="${esc(t.name)}"`, title: "Start the deploy now" },
    { label: "Review and deploy", attrs: `data-deploy="${esc(t.name)}" data-review="1"`, title: "Review first, then deploy" }])}</div>`;
}

// The projects of a target: the names when they are few, else their count with the names as a hover.
const projectsHtml = (P) => !P.length ? "" : `<span class="muted t-s" title="${esc(P.join(", "))}">${P.length > 3 ? P.length + " projects" : P.map(esc).join(", ")}</span>`;

export function renderTargets() {
  const T = (S.targets || []).filter(t => !scope.project || (t.project_names || []).includes(scope.project));
  const box = $("#targets");
  if (box.contains(document.activeElement) && document.activeElement.tagName === "INPUT") return;  // do not wipe a reason they type
  const open = new Set([...box.querySelectorAll("details[open]")].map(d => d.dataset.keep));  // a fold stays open over a refresh
  box.innerHTML = T.map(t => nyCard(`<b${t.description ? ` title="${esc(t.description)}"` : ""}>${esc(t.name)}</b>
        ${t.owner && (S.sessions || []).includes(t.owner) ? `<span class="link t-s" data-session-go="${esc(t.owner)}" title="Open the session of the owner, ${esc(t.owner)}">${ownerChip(t.owner)}</span>` : ownerChip(t.owner)}${t.owner && t.owner_expires_at ? `<span class="muted t-s">${left(t.owner_expires_at)}</span>` : ""}
        ${projectsHtml(t.project_names)}`, `${releaseLine(t)}
      ${t.pending.map(pendingHtml).join("")}
      ${actionsHtml(t)}
      ${hooksHtml(t, S.hook_events)}
      ${historyHtml(t)}`)).join("") || `<div class="muted" title='maxpm target add &lt;name&gt;'>No deploy targets${scope.project ? " for " + esc(scope.project) : ""}.</div>`;
  box.querySelectorAll("details[data-keep]").forEach(d => { if (open.has(d.dataset.keep)) d.open = true; });
}

// A click on this view's controls; true when it was one (the frame then stops).
export function onClick(t) {
  if (t.dataset.relnow) return run(async () => {
    const name = t.dataset.relnow, why = document.getElementById("relwhy-" + name), reason = why ? why.value.trim() : "";
    if (!reason) return toast("Say why this release cannot wait", true);
    why.blur();
    return act("release_now", { target: name, reason }).then(() => toast(`The release of ${name} can start now`)).catch(() => {});
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
