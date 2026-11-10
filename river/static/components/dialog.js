// Dialogs: a modal overlay (an element with a .box inside, hidden by the class "hidden"). Every dialog of the page
// closes the same three ways (#5027): its own close button, Escape, and a click or tap on the overlay outside the
// box. A press that starts in the box and ends outside (a text selection) does not close it. A dialog where the
// person typed text asks first: the first Escape or outside click shows a line in the box, and a second one within
// a few seconds closes it (draft: false for a dialog whose text is not worth keeping, such as a search). Focus goes
// back to where it was before it opened.

export const ASK_AGAIN_MS = 6000;

// A line in the box that says what the next Escape or outside click does; it goes away when the dialog closes.
export function draftNote(box, text) {
  if (!box) return;
  let n = box.querySelector(":scope > .dlg-draft");
  if (!n) { n = document.createElement("div"); n.className = "dlg-draft"; n.setAttribute("role", "status"); box.append(n); }
  n.textContent = text;
}

export function makeDialog(el, { onClose, draft = true } = {}) {
  el.classList.add("dlg");  // the one look of a dialog (app.css .dlg): overlay, box, width --dlg-w (#5028)
  let before = null, typed = new Set(), warned = 0, down = false;
  const isOpen = () => !el.classList.contains("hidden");
  const box = () => el.querySelector(".box") || el.firstElementChild;
  const clearNote = () => el.querySelectorAll(".dlg-draft").forEach(n => n.remove());
  function open() {
    if (!isOpen()) { before = document.activeElement; typed = new Set(); warned = 0; clearNote(); }
    el.classList.remove("hidden");
  }
  function close() {
    if (!isOpen()) return;
    el.classList.add("hidden");
    clearNote(); typed = new Set(); warned = 0;
    if (before && before.isConnected && before.focus) before.focus({ preventScroll: true });
    before = null;
    if (onClose) onClose();
  }
  // Text the person typed since the dialog opened, still in a field of it.
  const hasDraft = () => draft && [...typed].some(f => f.isConnected && el.contains(f)
    && String(f.isContentEditable ? f.textContent : f.value || "").trim());
  // Escape or an outside click: close, unless typed text would go; then ask once.
  function requestClose() {
    if (!isOpen()) return false;
    if (hasDraft() && Date.now() - warned > ASK_AGAIN_MS) {
      warned = Date.now();
      draftNote(box(), "You typed text here. Press Esc or click outside again to close without it.");
      return false;
    }
    close();
    return true;
  }
  el.addEventListener("input", (e) => {
    if (e.target.matches && e.target.matches("input:not([type=checkbox]):not([type=radio]), textarea, [contenteditable]")) typed.add(e.target);
  });
  el.addEventListener("pointerdown", (e) => { down = e.target === el; });
  el.addEventListener("click", (e) => { if (e.target === el && down) requestClose(); down = false; });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && isOpen()) requestClose(); });
  return { open, close, isOpen, requestClose };
}
