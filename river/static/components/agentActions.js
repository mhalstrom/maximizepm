// The manager section and each agent's queue. Actions go through the server ops start_manager, queue_add and
// queue_remove; Open session shows the agent's session on the Sessions tab (#4687). A stop or an end of a session is
// the End dialog (endSession.js, #5041); the Stop dialog that was here had no button left to open it (#5403).
import { esc, ago } from "../lib.js";
import { chip } from "./chip.js";

// The Manager section above Agents: none running (Start manager), or the one running with its last actions.
export function managerHtml(S) {
  const m = S.manager;
  if (!m) {
    return `<div class="st">No manager is running.</div>
      <div class="actions" style="margin-top:6px"><button class="btn primary" data-start-manager="1" title="Start a manager">▶ Start manager</button></div>`;
  }
  return `<div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap"><span class="dot ${esc(m.state)}"></span><b>${esc(m.name)}</b>
      ${m.platform ? chip("c-p", esc(m.platform)) : ""}${m.model ? chip("c-p", esc(m.model)) : ""}${chip(m.state === "active" ? "c-ready" : "c-waiting", esc(m.state))}
      <span class="spacer" style="flex:1"></span>${sessionButton(S, m.name)}</div>
    ${m.no_watch ? `<div class="st" style="color:var(--warn)">runs no watch (maxpm manage --watch) for ${esc(m.no_watch)}: nothing wakes it at its prompt. Tell it in its terminal to start the watch again.</div>` : ""}
    ${(m.asked || []).map(q => `<div class="st">asks you: ${esc(q.body)}${q.item_id ? ` <span class="link" data-open="${q.item_id}">#${q.item_id}</span>` : ""}</div>`).join("")}
    ${(m.actions || []).map(e => `<div class="ev">${ago(e.at)} · ${esc(e.change.replace(/ \(by manager [^)]*\)$/, ""))}</div>`).join("")
      || '<div class="st muted">No actions yet.</div>'}`;
}

// An agent's queue in its Agents row: items and instructions, with the native delivery status.
export function queueHtml(entries, agent) {
  if (!entries || !entries.length) return "";
  return `<div class="st"><b>queue</b></div>` + entries.map(e => e.item
    ? `<div class="st">· <span class="link" data-open="${e.item}">#${e.item} ${esc(e.title)}</span> <span class="muted">(${e.ready ? "ready" : esc(e.status === "open" ? "not ready" : e.status.replace("_", " "))})</span> <span class="link" data-qremove="${esc(agent)}" data-ref="${e.item}" title="Take it out of the queue">×</span></div>`
    : `<div class="st">· ${e.kind === "stop" ? "<b>stop</b>" : "note"}: ${esc(e.body)}${e.native_status ? ` <span class="muted">[${esc(e.native_status)}]</span>` : ""} <span class="link" data-qremove="${esc(agent)}" data-ref="e${e.entry}" title="${e.kind === "stop" ? "Withdraw the stop" : "Remove"}">×</span></div>`).join("");
}

// Open session: the agent's session on the Sessions tab (#4687), for an agent whose session that tab shows
// (S.sessions, from /api/state). A stopped agent, or one whose session ended, gets none.
export function hasSession(S, name) {
  return ((S && S.sessions) || []).includes(name);
}
export function sessionButton(S, name) {
  return hasSession(S, name) ? `<button class="btn" data-session-go="${esc(name)}" title="Open the session of ${esc(name)}">Open session</button>` : "";
}
