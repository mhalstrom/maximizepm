// Buttons (#5028): the one way a view or a part makes a button, so each button of the page looks and acts the same.
// kind: "" (the plain .btn), "primary", "warn", "icon" (.iconbtn: a symbol such as ✕ ↑ ⋯, with a label for
// screen readers and a tooltip), or "card" (.cardbtn: a whole card that is one button, such as a project card of the
// Graph tab; the card's own class gives its look, #5046). cls adds layout classes of the view (never colors or sizes). data: {key: value}
// becomes data-key="value". pressed: a toggle (aria-pressed and the class "on"). html is already escaped; title,
// label and data values are plain text.
import { esc } from "../lib.js";

function classes(kind, cls, pressed) {
  const base = kind === "icon" ? "iconbtn" : kind === "card" ? "cardbtn" : "btn" + (kind ? " " + kind : "");
  return base + (cls ? " " + cls : "") + (pressed ? " on" : "");
}

export function button(html, { kind = "", cls = "", title, label, id, data = {}, attrs = "", type = "button",
                               disabled = false, pressed } = {}) {
  const a = [`class="${classes(kind, cls, pressed)}"`, `type="${type}"`];
  if (id) a.push(`id="${esc(id)}"`);
  if (title != null) a.push(`title="${esc(title)}"`);
  if (label != null) a.push(`aria-label="${esc(label)}"`);
  if (pressed != null) a.push(`aria-pressed="${pressed ? "true" : "false"}"`);
  for (const [k, v] of Object.entries(data)) a.push(`data-${k}="${esc(v)}"`);
  if (disabled) a.push("disabled");
  return `<button ${a.join(" ")}${attrs ? " " + attrs : ""}>${html}</button>`;
}

// A symbol button (✕, ↑, ↓, ⋯): label says what it does, for the tooltip and screen readers.
export function iconButton(symbol, label, opts = {}) {
  return button(esc(symbol), { ...opts, kind: "icon", title: opts.title ?? label, label });
}

// The same as a DOM element, for code that builds elements; text is plain text.
export function buttonEl(text, { kind = "", cls = "", title, label, data = {}, type = "button", disabled = false,
                                 pressed, onClick } = {}) {
  const b = document.createElement("button");
  b.type = type; b.className = classes(kind, cls, pressed); b.textContent = text;
  if (title != null) b.title = title;
  if (label != null) b.setAttribute("aria-label", label);
  if (pressed != null) b.setAttribute("aria-pressed", pressed ? "true" : "false");
  for (const [k, v] of Object.entries(data)) b.setAttribute("data-" + k, v);
  if (disabled) b.disabled = true;
  if (onClick) b.addEventListener("click", onClick);
  return b;
}
