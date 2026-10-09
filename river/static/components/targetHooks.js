// The hooks of a deploy target on the Targets tab (maxpm target hook): each hook with the result of its last
// run, and the events that have no hook yet. A click goes through the server op target_hook (app.js).
import { esc, ago, clip } from "../lib.js";

// The last run of a hook (core.target_hooks), in the words of maxpm target show. item: the deploy or review
// item of the event. bad: the hook failed; its event stands, because a hook never undoes it.
export function hookRun(last) {
  if (!last) return { text: "never ran", item: null, bad: false };
  if (!last.ended_at) return { text: last.started_at ? "runs now" : "waits to run", item: last.item_id, bad: false };
  const result = last.timed_out ? "stopped at hook_timeout" : last.exit_code == null ? "not started" : "exit " + last.exit_code;
  return { text: `${result}, ${ago(last.ended_at)}`, item: last.item_id, bad: !!last.timed_out || last.exit_code !== 0 };
}

// t: a target of the state (name, hooks). events: {event: when it occurs} (state.hook_events), in their order.
export function hooksHtml(t, events) {
  const hooks = t.hooks || [], name = esc(t.name);
  const link = (event, label, clear) => `<span class="link" data-hook="${esc(event)}" data-target="${name}"${clear ? ' data-clear="1"' : ""} title="${esc(clear ? "Remove this hook" : (events || {})[event] ? "MaximizePM runs the command when " + events[event] : "")}">${esc(label)}</span>`;
  const lines = hooks.map(h => {
    const r = hookRun(h.last);
    const ran = (r.item ? `last run for <span class="link" data-open="${r.item}">#${r.item}</span>: ` : "") + esc(r.text);
    return `<div class="st" style="margin-top:4px"><b>${esc(h.event)}</b>: <code style="font-size:12px" title="${esc(h.command)}">${esc(clip(h.command, 200))}</code>
        <span class="${r.bad ? "" : "muted"}" style="font-size:12px${r.bad ? ";color:var(--warn)" : ""}">(${ran})</span>
        <span style="font-size:12px">${link(h.event, "change")} · ${link(h.event, "remove", true)}</span></div>`;
  }).join("");
  const free = Object.keys(events || {}).filter(e => !hooks.some(h => h.event === e));
  return `<div class="st" style="margin-top:6px"><b>Hooks</b> <span class="muted" style="font-size:12px">${hooks.length ? "MaximizePM runs each command when its event occurs; a hook that fails does not undo the event" : "none: a hook is a command that MaximizePM runs when a release event occurs"}</span></div>
      ${lines}${free.length ? `<div class="muted" style="font-size:12px;margin-top:4px">add a hook for: ${free.map(e => link(e, e)).join(" · ")}</div>` : ""}`;
}
