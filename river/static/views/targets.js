// The Targets tab (#4656): each deploy target, what it ships, its hooks, Deploy now and its deploys.
import { $, esc, toast, act, ago, left, clip } from "../lib.js";
import { ownerChip } from "../components/chip.js";
import { nyCard } from "../components/card.js";
import { button } from "../components/button.js";
import { agentStart } from "../components/agentStart.js";
import { chooseLaunch, launchArgs } from "../components/launchDialog.js";
import { hooksHtml } from "../components/targetHooks.js";
import { S } from "../page.js";

// The release gate and the cadence of a target, each with why (#5003): set per project from its needs.
function releaseGate(t) {
  const r = t.release || {}, a = r.approval || {}, n = esc(t.name);
  const gate = a.approval === "person" ? `a person's yes for each release (set on ${esc(a.project)})`
    : a.approval === "review" ? `ships when its review passes (set on ${esc(a.project)})`
    : `<span style="color:var(--warn)">not set</span>: maxpm config set release_approval review|person --project &lt;a project of ${n}&gt;`;
  const cad = r.cadence ? `every ${esc(r.cadence)} at most` + (r.cadence_reason ? `: ${esc(clip(r.cadence_reason, 160))}` : ` (no reason given: maxpm target cadence ${n} ${esc(r.cadence)} --reason "…")`)
    : "off: each release starts when the one before it is done" + (r.cadence_reason ? `: ${esc(clip(r.cadence_reason, 160))}` : "");
  return `<div class="muted t-s">release gate: ${gate}<br>cadence: ${cad}</div>`;
}

export function renderTargets() {
  const T = S.targets || [];
  const box = $("#targets");
  if (box.contains(document.activeElement) && document.activeElement.tagName === "INPUT") return;  // do not wipe a reason they type
  const shipList = (s) => s.map(x => `<span class="link" data-open="${x.id}">#${x.id}</span> ${esc(clip(x.title, 50))} <span class="muted">(${esc(x.status.replace("_", " "))})</span>`).join("; ") || '<span class="muted">nothing yet</span>';
  $("#targets").innerHTML = T.map(t => nyCard(`<b>${esc(t.name)}</b>
        ${ownerChip(t.owner)}${t.owner ? `<span class="muted t-s">${t.owner_expires_at ? left(t.owner_expires_at) + " left" : ""}</span>` : ""}
        <span class="muted t-s">${t.project_names.length ? "projects: " + t.project_names.map(esc).join(", ") : "no projects"}</span>`, `${t.description ? `<div class="ny-c">${esc(clip(t.description, 300))}</div>` : ""}
      <div class="muted t-s">${t.monitor ? "after each deploy, the deploy session watches: " + esc(clip(t.monitor, 200)) : `nothing to watch after a deploy (the deploy session watches what you set: maxpm target monitor ${esc(t.name)} "&lt;what to watch, for how long&gt;")`}</div>
      ${releaseGate(t)}
      ${hooksHtml(t, S.hook_events)}
      ${t.pending.map(d => `<div class="st" style="margin-top:var(--sp-1)"><span class="link" data-open="${d.id}">#${d.id}</span> ${d.ready ? "ready to deploy" : d.status === "open" ? (d.cut_at ? "cut " + ago(d.cut_at) : "collecting") : esc(d.status.replace("_", " ")) + (d.assignee ? " · " + esc(d.assignee) : "")}: ${shipList(d.ships)}${d.review ? `<div class="muted t-s">first a review: <span class="link" data-open="${d.review.id}">#${d.review.id}</span> (${esc(d.review.status.replace("_", " "))}${d.review.assignee ? " · " + esc(d.review.assignee) : ""})</div>` : ""}</div>`).join("") || '<div class="st muted">No pending ship requests.</div>'}
      ${t.release && t.release.text ? `<div class="muted t-s">${esc(t.release.text)}${t.release.last ? ` · last release ${ago(t.release.last.at)}` : ""}</div>` : ""}
      <div class="actions" style="margin-top:var(--sp-2)">${t.release && t.release.waits
        ? `<input id="relwhy-${esc(t.name)}" placeholder="why this release cannot wait for the cadence" style="flex:1;min-width:var(--w-input-m)"> ${button("Release now", { data: { relnow: t.name }, title: "Start this release sooner than the cadence permits; the reason goes in the history of the deploy item" })}`
        : t.pending.some(d => d.status === "open" && d.ships.length)
        ? agentStart(S.launch_agents, [
            { label: "Deploy now", cls: "btn primary", attrs: `data-deploy="${esc(t.name)}"`, title: "Start the deploy now: the owner gets an alert, or the chosen agent opens to take it" },
            { label: "Review and deploy", attrs: `data-deploy="${esc(t.name)}" data-review="1"`, title: "First one review of everything it ships (review steps per project), then the deploy" }])
        : '<span class="muted t-s">Nothing to deploy: ship items first (maxpm ship &lt;id&gt;, or done --ship).</span>'}</div>
      <div class="st" style="margin-top:var(--sp-2)"><b>Deploys</b></div>
      ${(t.history || []).map(h => `<div class="st" style="margin-top:var(--sp-1)"><span class="link" data-open="${h.id}">#${h.id}</span> ${ago(h.closed_at)}${h.done_by ? " by " + esc(h.done_by) : ""}${h.output ? ": " + esc(clip(h.output, 200)) : ""}<div class="muted t-s">shipped ${shipList(h.ships)}</div></div>`).join("") || '<div class="st muted">Never deployed.</div>'}`)).join("") || `<div class="muted">No deploy targets. Add one: maxpm target add &lt;name&gt; --description "how it deploys"</div>`;
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
