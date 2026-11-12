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
// Folded (#5082): the summary names the hooks, and a hook that failed shows there in the warn color.
export function hooksHtml(t, events) {
  const hooks = t.hooks || [], name = esc(t.name);
  const link = (event, label, clear) => `<span class="link" data-hook="${esc(event)}" data-target="${name}"${clear ? ' data-clear="1"' : ""} title="${esc(clear ? "Remove this hook" : label === "change" ? "Change this hook" : "Add a hook on " + event)}">${esc(label)}</span>`;
  const runs = hooks.map(h => hookRun(h.last));
  const lines = hooks.map((h, i) => {
    const r = runs[i];
    const ran = (r.item ? `<span class="link" data-open="${r.item}">#${r.item}</span> ` : "") + esc(r.text);
    return `<div class="st hook t-s"><b>${esc(h.event)}</b> <code title="${esc(h.command)}">${esc(clip(h.command, 60))}</code>
        <span class="${r.bad ? "" : "muted"}"${r.bad ? ' style="color:var(--warn)"' : ""}>${ran}</span>
        ${link(h.event, "change")} · ${link(h.event, "remove", true)}</div>`;
  }).join("");
  const free = Object.keys(events || {}).filter(e => !hooks.some(h => h.event === e));
  const bad = hooks.filter((h, i) => runs[i].bad).map(h => esc(h.event));
  const head = hooks.length ? `Hooks: ${hooks.map(h => esc(h.event)).join(", ")}` : "Hooks: none";
  return `<details data-keep="hooks-${name}" title="Commands that run on release events"><summary>${head}${bad.length ? ` · <span style="color:var(--warn)">${bad.join(", ")} failed</span>` : ""}</summary>
      ${lines}${free.length ? `<div class="muted t-s hook-add">add: ${free.map(e => link(e, e)).join(" · ")}</div>` : ""}</details>`;
}
