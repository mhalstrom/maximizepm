// The page's own settings of a session's view (#4635), kept for each session in this browser: not settings of
// its chat provider. showTools: the tool call lines in the chat. The default hides them: the person reads the chat
// to follow and answer the agent, and each run of tool calls stays one click away as a marker ("12 tool calls").
import { store } from "../lib.js";

const KEY = "river.sessions.view";
const DEFAULTS = { showTools: false };

function all() {
  try { return JSON.parse(store(KEY) || "{}") || {}; } catch (e) { return {}; }
}

export function viewOf(agent) {
  return { ...DEFAULTS, ...(all()[agent] || {}) };
}

export function setView(agent, key, value) {
  const a = all();
  a[agent] = { ...(a[agent] || {}), [key]: value };
  store(KEY, JSON.stringify(a));
  document.dispatchEvent(new CustomEvent("maxpm-view", { detail: { agent } }));
}
