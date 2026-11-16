// The bell of the top bar (#6001, mark 2026-11-15: 'give us a button to toggle notifications on and off'): all
// notices on or off, in one click. Off: no notice goes to the phone (ntfy) or the Mac, for any notice; Needs you and
// the sessions' messages work as before. The state comes from the page state (S.notifications, the setting
// notifications); a click sets it with the op config_set. maxpm notify on|off does the same.
import { act, toast } from "../lib.js";

const BELL = '<path d="M6 16v-5a6 6 0 0 1 12 0v5l2 2H4z"/><path d="M10 20a2 2 0 0 0 4 0"/>';
const SLASH = '<path d="M3 3l18 18"/>';

export function bellSvg(on) {
  return `<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2"
    stroke-linecap="round" stroke-linejoin="round">${BELL}${on ? "" : SLASH}</svg>`;
}

export function bellTitle(on) {
  return on ? "Notifications on: the phone and the Mac get notices. Click to turn them off"
    : "Notifications off: no notice goes to the phone or the Mac. Click to turn them on";
}

// Draw the button for the setting's value ("on" or "off"); it changes the button only when the state changed.
export function drawBell(btn, value) {
  const on = value !== "off";
  if (btn.dataset.on === String(on)) return;
  btn.dataset.on = String(on);
  btn.innerHTML = bellSvg(on);
  btn.setAttribute("aria-pressed", String(on));
  btn.setAttribute("aria-label", on ? "Notifications on" : "Notifications off");
  btn.classList.toggle("bell-off", !on);
  btn.title = bellTitle(on);
}

// current(): the setting's value now ("on" or "off").
export function wireBell(btn, current) {
  btn.addEventListener("click", async () => {
    const next = current() === "off" ? "on" : "off";
    try {
      await act("config_set", { key: "notifications", value: next });
      drawBell(btn, next);
      toast(next === "off" ? "Notifications off: no phone or Mac notice. Needs you still shows everything"
        : "Notifications on: the phone and the Mac get notices again");
    } catch (e) { /* act showed the error */ }
  });
}
