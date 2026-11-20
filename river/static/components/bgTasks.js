// Background tasks of a session (#6454, mark 2026-11-20: 'how can we see background tasks that run for sessions?'):
// the commands and subagents that its agent CLI runs in the background, which maxpm serve reads from its chat log
// (core.record_tasks). tasks is null when the session's CLI has no reader of them (Codex): then no badge, not 0. A
// river wait (maxpm wait, the manager's watch) counts for nothing: it shows in the list, dim, as a wait (#6021: a
// count on every waiting row said nothing).
// taskBadge(tasks): the count on a session row, in the shape of the ! badge (.tbadge, sessions.css); taskListHtml:
// the list in the head of the opened session (its clock button, sessions.html #sessTasks).
import { esc, ago } from "../lib.js";
import { chip } from "./chip.js";

export const CLOCK_SVG = `<svg class="tclock" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6.25" fill="none" stroke="currentColor" stroke-width="1.75"/><path d="M8 4.5V8l2.5 1.6" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round"/></svg>`;

// The tasks that run now and are work (no river wait).
export const runningTasks = (tasks) => (tasks || []).filter(t => !t.end && !t.wait);

export function taskWords(n) { return `${n} background task${n === 1 ? "" : "s"}`; }

export function taskBadge(tasks) {
  const n = runningTasks(tasks).length;
  return n ? `<span class="tbadge" title="${taskWords(n)}" aria-label="${taskWords(n)}">${CLOCK_SVG}${n}</span>` : "";
}

const hm = (iso) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
function lasted(a, b) {
  const m = Math.round((Date.parse(b) - Date.parse(a)) / 60000);
  return m < 1 ? "under 1m" : m < 60 ? m + "m" : Math.floor(m / 60) + "h " + (m % 60) + "m";
}

function taskRow(t) {
  const kind = t.wait ? chip("c-waiting", "wait") : chip(t.kind === "subagent" ? "c-goal" : "c-in_progress", esc(t.kind));
  const when = t.end ? `${hm(t.at)} to ${hm(t.end)} · ${lasted(t.at, t.end)} · ${t.status || "ended"}`
    : `since ${hm(t.at)} · ${ago(t.at).replace(" ago", "")}`;
  return `<li class="bgt${t.end ? " bgt-end" : ""}${t.wait ? " bgt-wait" : ""}" title="${esc(t.command || t.what)}">
    ${kind}<span class="bgt-w">${esc(t.what || t.command || t.id)}</span><span class="bgt-t">${esc(when)}</span></li>`;
}

export function taskListHtml(tasks) {
  const run = (tasks || []).filter(t => !t.end), done = (tasks || []).filter(t => t.end);
  const n = runningTasks(tasks).length;
  return `<div class="bgt-h">${n ? `${taskWords(n)} run now` : "No background task runs now"}</div>`
    + (run.length ? `<ul class="bgt-list">${run.map(taskRow).join("")}</ul>` : "")
    + (done.length ? `<div class="bgt-h bgt-sub">Ended</div><ul class="bgt-list">${done.map(taskRow).join("")}</ul>` : "");
}
