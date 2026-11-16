// The pause button of the top bar (#5962, policies p79 and p136): every working session commits, releases its task
// and ends, and no new session starts until a time or Resume. Settings stay as they are. While paused the button
// shows a play sign in the warn color, and a click resumes. maxpm pause and maxpm resume do the same.
// Server: the ops pause and resume; the state comes from the page state (S.pause).
import { act, toast } from "../lib.js";
import { makeDialog } from "./dialog.js";
import { button, iconButton } from "./button.js";

const PAUSE = '<path d="M9 5v14M15 5v14"/>';
const PLAY = '<path d="M8 5l11 7-11 7z"/>';

function svg(paused) {
  return `<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2"
    stroke-linecap="round" stroke-linejoin="round">${paused ? PLAY : PAUSE}</svg>`;
}

// Draw the button for the pause in force (S.pause, or null); it changes the button only when the state changed.
export function drawPause(btn, pause) {
  const on = !!pause;
  if (btn.dataset.paused === String(on)) return;
  btn.dataset.paused = String(on);
  btn.innerHTML = svg(on);
  btn.setAttribute("aria-pressed", String(on));
  btn.setAttribute("aria-label", on ? "Resume sessions" : "Pause sessions");
  btn.classList.toggle("paused", on);
  btn.title = on ? "Resume sessions" : "Pause sessions";
}

let dlg = null, box = null;
function dialog() {
  if (dlg) return;
  const d = document.createElement("div");
  d.id = "pauseDlg"; d.className = "hidden";
  d.innerHTML = '<div class="box"></div>';
  document.body.appendChild(d);
  box = d.firstElementChild;
  dlg = makeDialog(d);
  d.addEventListener("click", async (e) => {
    if (e.target.closest("[data-pause-cancel]")) return dlg.close();
    if (!e.target.closest("[data-pause-go]")) return;
    const v = (s) => (box.querySelector(s) || {}).value?.trim() || "";
    try {
      const r = await act("pause", { until: v("[data-pause-until]") || undefined, reason: v("[data-pause-reason]") || undefined,
        restart: !!(box.querySelector("[data-pause-restart]") || {}).checked });
      dlg.close();
      toast(`Paused: ${(r.stopped || []).length} session(s) asked to stop` + (r.clear ? "; nothing is held" : ""));
    } catch (_) { /* act showed the error */ }
  });
}

function dialogHtml() {
  return `<div class="dlg-head"><h2>Pause all sessions</h2>${iconButton("✕", "Close", { data: { "pause-cancel": "1" } })}</div>
    <p>Each working session commits its finished work, releases its task with a note, and ends. No new session starts
      until the time below, or until you resume. The manager and experts keep running. Settings stay as they are.</p>
    <div class="form">
      <label>Until (optional)<input data-pause-until placeholder="tomorrow 8:00"></label>
      <label>Why (optional; each session sees it)<input data-pause-reason placeholder="the night"></label>
    </div>
    <label class="end-check"><input type="checkbox" data-pause-restart> Restart MaximizePM once nothing is held</label>
    <div class="actions">${button("Cancel", { data: { "pause-cancel": "1" } })}${button("Pause", { kind: "primary", data: { "pause-go": "1" } })}</div>`;
}

// current(): the pause in force now (S.pause), or null.
export function wirePause(btn, current) {
  btn.addEventListener("click", async () => {
    const p = current();
    if (p) {
      try {
        await act("resume", {});
        drawPause(btn, null);
        toast("Resumed: new sessions start again");
      } catch (_) { /* act showed the error */ }
      return;
    }
    dialog();
    box.innerHTML = dialogHtml();
    dlg.open();
    box.querySelector("[data-pause-until]").focus();
  });
}
