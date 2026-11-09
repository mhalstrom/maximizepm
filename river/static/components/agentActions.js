// The manager section, each agent's queue, and the Stop dialog (a page dialog, not a browser alert).
// Actions go through the server ops start_manager, queue_add, queue_remove, stop_agent, kill_agent; Open session
// shows the agent's session on the Sessions tab (#4687).
import { $, esc, ago } from "../lib.js";
import { chip } from "./chip.js";
import { makeDialog } from "./dialog.js";

// The Manager section above Agents: none running (Start manager), or the one running with its last actions.
export function managerHtml(S) {
  const m = S.manager;
  if (!m) {
    return `<div class="st">No manager is running. A manager plans with you, starts agents, fills their queues, and stops stuck ones.</div>
      <div class="actions" style="margin-top:6px"><button class="btn primary" data-start-manager="1" title="Opens the launch dialog; the agent starts with maxpm manage">▶ Start manager</button></div>`;
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
  return hasSession(S, name) ? `<button class="btn" data-session-go="${esc(name)}" title="Open the session of ${esc(name)} in the Sessions tab: its chat, its item, and what waits for you there">Open session</button>` : "";
}

// The buttons on an agent row: Open session, and Stop for a session that is not stopped.
export function agentButtons(a, S) {
  if (a.kind === "human") return "";
  const b = sessionButton(S, a.name) + (a.state !== "stopped" ? `<button class="btn" data-stop-agent="${esc(a.name)}">Stop</button>` : "");
  return b ? `<div class="actions" style="margin-top:4px">${b}</div>` : "";
}

let dlg = null, done = null;
function el() {
  let d = $("#stopDlg");
  if (d) return d;
  d = document.createElement("div");
  d.id = "stopDlg"; d.className = "hidden";
  d.setAttribute("role", "dialog"); d.setAttribute("aria-modal", "true");
  d.innerHTML = '<div class="box"></div>';
  document.body.appendChild(d);
  dlg = makeDialog(d, { onClose: () => finish(null) });
  d.addEventListener("click", (e) => {
    const q = (s) => d.querySelector(s);
    if (e.target === d || e.target.closest("[data-stop-cancel]")) return dlg.close();
    const reason = q("#stopReason").value.trim();
    if (e.target.closest("[data-stop-go]")) { finish({ reason, kill: false }); return dlg.close(); }
    if (e.target.closest("[data-kill-ask]")) { q("#killConfirm").classList.remove("hidden"); return; }
    if (e.target.closest("[data-kill-go]")) { finish({ reason, kill: true }); return dlg.close(); }
  });
  return d;
}
function finish(v) { const r = done; done = null; if (r) r(v); }

// Ask for the reason; resolves to {reason, kill} or null. Kill shows only for a session with a PID on this
// host, and needs a second confirm.
export function chooseStop(a) {
  el();
  $("#stopDlg .box").innerHTML = `
    <h2 style="margin:0">Stop ${esc(a.name)}</h2>
    <div class="muted" style="font-size:13px;margin:6px 0">A stop is a request: the agent commits finished work, releases its item, and ends. A waiting agent ends at once.</div>
    <div class="form"><label>Why (optional; the agent sees it)<input id="stopReason" placeholder="the plan changed"></label></div>
    <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:12px">
      <button class="btn" data-stop-cancel="1">Cancel</button>
      ${a.can_kill ? `<button class="btn" data-kill-ask="1" title="Emergency only">Kill process…</button>` : ""}
      <button class="btn primary" data-stop-go="1">Ask it to stop</button>
    </div>
    ${a.can_kill ? `<div id="killConfirm" class="hidden" style="margin-top:10px;padding:8px;border:1px solid var(--line);border-radius:8px">
      <b>Kill PID ${esc(a.pid)} now?</b> Emergency only: uncommitted work in its folder is lost. MaximizePM releases its items, goals, and targets.
      <div style="display:flex;justify-content:flex-end;margin-top:6px"><button class="btn primary" data-kill-go="1">Kill ${esc(a.name)}</button></div></div>` : ""}`;
  if (done) finish(null);
  dlg.open();
  $("#stopReason").focus();
  return new Promise(r => { done = r; });
}
