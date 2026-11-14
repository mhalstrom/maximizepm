// Start an agent: the button(s) that start one, the same wherever the page starts an agent. A click opens
// the launch dialog (launchDialog.js), which picks the agent, the model, the effort, and tab, window, or tmux.
import { esc } from "../lib.js";

// The setting queue (#5851) as the page reads it: a project's own value, else the global one, else on. off: the
// project uses sessions only, so the page offers New session there and not the queue's Start.
export function queueOn(S, project) {
  const o = (S && S.settings && S.settings.overrides) || [];
  const v = (project && o.find(x => x.key === "queue" && x.scope === "project:" + project))
    || o.find(x => x.key === "queue" && x.scope === "global");
  return !v || v.value !== "off";
}
// Whether any project uses the queue (a queue with no project yet follows the global value).
export function queueAnywhere(S) {
  const ps = ((S && S.projects) || []).filter(p => !p.archived);
  return ps.length ? ps.some(p => queueOn(S, p.name)) : queueOn(S, null);
}
// New session (#5851): an agent in a folder with no item; the click opens the launch dialog with a folder field.
export function newSession(S, primary) {
  return agentStart(S && S.launch_agents, { label: "New session", cls: primary ? "primary" : "", attrs: 'data-new-session="1"',
    title: "Talk with an agent" });
}

// The buttons. Each button: {label, title, attrs, cls}; attrs are raw attributes (data-*), label and title
// are plain text; cls adds classes ("primary") to "btn", which every button has: a "primary" without "btn" kept
// the browser's own border, a dark ring that no other primary button has (#5312).
// agents: the Start button's list (S.launch_agents); with one agent the title names it.
export function agentStart(agents, buttons) {
  const LA = agents || [], who = LA.length === 1 ? ` (${LA[0]})` : "";
  const cls = (c) => [...new Set(["btn", ...(c || "").split(/\s+/).filter(Boolean)])].join(" ");
  const bs = (Array.isArray(buttons) ? buttons : [buttons]).map(b =>
    `<button class="${cls(b.cls)}" ${b.attrs || ""} title="${esc((b.title || "") + who)}">▶ ${esc(b.label)}</button>`);
  return `<span class="agentStart">${bs.join("")}</span>`;
}
