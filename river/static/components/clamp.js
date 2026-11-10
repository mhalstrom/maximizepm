// Long text on a phone (#4889): a project's notes and the help above the Settings table show their first lines, with
// a More button that shows the rest. The clamp is CSS of phone widths only (app.css, .clamp); on a wider screen the
// text shows in full and More stays hidden. A text that fits in its lines gets no More (class "short").
//
// clampHtml(id, inner, after) gives the markup: the text, then More, then `after` (for example an "edit" link) on
// the same line. The views redraw their markup on each change of the state, so the open ones are kept here by id.
const opened = new Set();

export function clampHtml(id, inner, after = "") {
  const open = opened.has(id);
  return `<span class="clamp${open ? " open" : ""}" data-clamp="${id.replace(/"/g, "&quot;")}"><span class="clamp-text">${inner}</span>` +
    moreButton(open) + `${after}</span>`;
}

function moreButton(open) {
  return `<button type="button" class="clamp-more" aria-expanded="${open}">${open ? "Less" : "More"}</button>`;
}

// A text measures 0 while its tab is hidden, so each one is measured again when its size changes: when its tab
// shows, the screen turns, or the text changes.
const watch = typeof ResizeObserver === "function" ? new ResizeObserver((entries) => {
  for (const e of entries) {
    if (!e.target.isConnected) { watch.unobserve(e.target); continue; }
    measure(e.target);
  }
}) : null;

function measure(text) {
  const box = text.parentElement;
  if (!box || box.classList.contains("open") || !text.clientHeight) return;
  box.classList.toggle("short", text.scrollHeight <= text.clientHeight + 1);
}

/** Measures the clamped texts under root and watches them; call it after the view drew its markup. */
// A clamp written in a view's HTML (the Settings help) has no More of its own: it gets one here, so a view makes no
// button (#5046).
export function fitClamps(root) {
  for (const box of root.querySelectorAll(".clamp")) {
    if (!box.querySelector(":scope > .clamp-more")) box.querySelector(".clamp-text")?.insertAdjacentHTML("afterend", moreButton(box.classList.contains("open")));
  }
  for (const t of root.querySelectorAll(".clamp-text")) { measure(t); if (watch) watch.observe(t); }
}

document.addEventListener("click", (e) => {
  const b = e.target.closest && e.target.closest(".clamp-more");
  if (!b) return;
  const box = b.closest(".clamp"), open = box.classList.toggle("open");
  if (open) opened.add(box.dataset.clamp); else opened.delete(box.dataset.clamp);
  b.textContent = open ? "Less" : "More";
  b.setAttribute("aria-expanded", String(open));
});
