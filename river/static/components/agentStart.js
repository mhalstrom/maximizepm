// Start an agent: the button(s) that start one, the same wherever the page starts an agent. A click opens
// the launch dialog (launchDialog.js), which picks the agent, the model, the effort, and tab, window, or tmux.
import { esc } from "../lib.js";

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
