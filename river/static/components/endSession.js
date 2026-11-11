// End a session (#5041): the one confirm dialog behind End on a row of the Sessions tab and in its chat header. It
// says what happens before the person ends it:
// - a worker or the manager: a stop request (op stop_agent). It commits its finished work, releases its task with a
//   note, and ends; a session that waits for work ends at once. The optional reason goes to the session.
// - an expert: maxpm expert end (op expert_end). It ends now; its notes stay, and its task goes back to the queue,
//   or closes when the person says the talk is done.
// - End now (op kill_agent): only for a session that does not answer, with a process on this computer, or (a session
//   that ran no maxpm command, #5278) a program in its own tmux pane. It ends at once, and MaximizePM releases its
//   task, goals and targets. It needs a second confirm.
// The row then shows 'ending' until the session is gone; then it leaves the list.
import { esc, act } from "../lib.js";
import { makeDialog } from "./dialog.js";
import { button, iconButton } from "./button.js";

// The symbol of End on a row: a power sign.
export const END_SVG = `<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" d="M8 1.8v5.4M4.6 3.9a5.2 5.2 0 1 0 6.8 0"/></svg>`;

// The End button of a row (data-end) and of the chat header (data-end, with its text).
export function endButton(s, { icon = false, cls = "" } = {}) {
  const ending = s.state === "stopping";
  const title = ending ? `${s.name} is ending: it was asked to stop. End it now if it does not answer`
    : s.role === "expert" ? `End the expert ${s.name}: its notes stay`
    : s.role === "assistant" ? `End your assistant ${s.name}: what you decided with it stays in the queue`
    : `End ${s.name}: it commits finished work, releases its task, and ends`;
  return icon ? button(END_SVG, { kind: "icon", cls, title, label: `End ${s.name}`, data: { end: s.name } })
    : button(ending ? "Ending…" : "End", { cls, title, data: { end: s.name } });
}

let dlg = null, done = null, box = null;
function finish(v) { const r = done; done = null; if (r) r(v); }
function el() {
  if (dlg) return;
  const d = document.createElement("div");
  d.id = "endDlg"; d.className = "hidden";
  d.innerHTML = '<div class="box end-box"></div>';
  document.body.appendChild(d);
  box = d.firstElementChild;
  dlg = makeDialog(d, { onClose: () => finish(null) });
  d.addEventListener("click", (e) => {
    if (e.target === d) return;  // the overlay: makeDialog closes it
    const q = (s) => box.querySelector(s);
    const reason = (q("[data-end-reason]") || {}).value?.trim() || "";
    const closeTask = !!(q("[data-end-done]") || {}).checked;
    if (e.target.closest("[data-end-cancel], [data-end-close]")) return dlg.close();
    if (e.target.closest("[data-end-go]")) { finish({ how: e.target.closest("[data-end-go]").dataset.endGo, reason, done: closeTask }); return dlg.close(); }
    if (e.target.closest("[data-end-now-ask]")) { q(".end-now").classList.remove("hidden"); q(".end-now button").focus(); }
  });
  d.addEventListener("change", (e) => {
    if (!e.target.matches("[data-end-done]")) return;
    box.querySelectorAll("[data-task-fate]").forEach(x => { x.textContent = fate(e.target.checked); });
  });
}
const fate = (closes) => closes ? "closes: the talk is done" : "goes back to the queue; maxpm expert start takes it again with its chat";
const taskList = (s) => (s.holds || []).map(h => `#${h.id} ${esc(h.title)}`).join(", ");

// The text and buttons of the dialog for one session (a card of GET /api/sessions).
export function endHtml(s) {
  const name = esc(s.name), held = (s.holds || []).length;
  const reason = `<div class="form"><label>Why (optional; ${s.role === "expert" ? "the history keeps it" : "the session sees it"})<input data-end-reason placeholder="the plan changed"></label></div>`;
  const now = s.can_kill ? `<div class="end-now hidden"><b>End ${name} now?</b> ${s.pid ? `Its process (PID ${esc(s.pid)})` : "The program in its tmux pane"} ends at once. Use this only
      when the session does not answer. Work that it did not commit stays in its folder, and nothing commits it. MaximizePM releases its
      task, goals and targets.<div class="actions">${button(`End ${name} now`, { kind: "warn", data: { "end-go": "kill" } })}</div></div>` : "";
  const nowBtn = s.can_kill ? button("End now…", { title: "Only for a session that does not answer", data: { "end-now-ask": "1" } }) : "";
  let what, go;
  if (s.state === "stopping") {
    what = `<p>${name} was asked to stop${s.stop_by ? ` by ${esc(s.stop_by)}` : ""}${s.stop_reason ? `: ${esc(s.stop_reason)}` : ""}. It ends after
      its next maxpm command, when it has committed and released its task.</p>`
      + (s.can_kill ? "<p>If it does not answer, end it now.</p>"
        : `<p class="muted">It has no process on this computer that MaximizePM can end: if it does not answer, close it in its terminal.</p>`);
    go = "";
  } else if (s.role === "expert") {
    what = `<p>The expert ends now. Its notes stay, and <code>maxpm expert start ${name} --resume</code> starts it again with its chat.</p>`
      + (held ? `<p>Its task ${taskList(s)} <span data-task-fate>${fate(false)}</span>.</p>
          <label class="end-check"><input type="checkbox" data-end-done> The talk is done: close its task</label>` : "<p>It holds no task.</p>");
    go = button("End expert", { kind: "primary", data: { "end-go": "expert" } });
  } else {
    what = `<p>MaximizePM asks ${name} to stop. It commits its finished work, ${held ? `releases ${taskList(s)} with a note,` : "releases what it holds,"}
      and ends. A session that waits for work ends at once.</p>`;
    go = button("End session", { kind: "primary", data: { "end-go": "stop" } });
  }
  // The head of the other dialogs: the heading and a ✕ that closes it like Cancel (#5363, as #5334).
  return `<div class="dlg-head"><h2>End ${esc(s.title && s.title !== s.name ? `${s.title} (${s.name})` : s.name)}</h2>${iconButton("✕", "Close", { data: { "end-close": "1" } })}</div>${what}
    ${s.state === "stopping" ? "" : reason}
    <div class="actions">${button("Cancel", { data: { "end-cancel": "1" } })}${nowBtn}${go}</div>${now}`;
}

// Ask; resolves to {how: "stop"|"expert"|"kill", reason, done} or null.
export function chooseEnd(s) {
  el();
  box.innerHTML = endHtml(s);
  if (done) finish(null);
  dlg.open();
  (box.querySelector("[data-end-reason]") || box.querySelector("[data-end-go], [data-end-now-ask]") || box).focus?.();
  return new Promise(r => { done = r; });
}

// The op of each choice, with its arguments.
export function endOp(s, c) {
  if (c.how === "expert") return ["expert_end", { name: s.name, reason: c.reason || undefined, done: c.done }];
  return [c.how === "kill" ? "kill_agent" : "stop_agent", { agent: s.name, reason: c.reason }];
}

// Ask, then end it. Resolves to the choice when the op ran, else null (cancelled, or act showed the error).
export async function endSession(s) {
  const c = await chooseEnd(s);
  if (!c) return null;
  const [op, args] = endOp(s, c);
  try { await act(op, args); } catch (_) { return null; }
  return c;
}
