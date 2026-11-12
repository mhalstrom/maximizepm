// Forms: field builders for forms made in code (the item drawer), and a redraw that keeps
// what the person typed. The forms in index.html use the same classes: .form, .two, label, .actions.
import { esc } from "../lib.js";

// Attributes in the order given; true writes the name alone; null, undefined and false are left out.
function attrs(a) {
  return Object.entries(a).filter(([, v]) => v != null && v !== false).map(([k, v]) => v === true ? " " + k : ` ${k}="${esc(v)}"`).join("");
}
export function input(a) { return `<input${attrs(a)}>`; }
export function textarea(a, value) { return `<textarea${attrs(a)}>${esc(value)}</textarea>`; }
// options: [value, label] pairs, or plain values; the option equal to selected is chosen.
export function select(a, options, selected) {
  return `<select${attrs(a)}>${options.map(o => {
    const [v, label, withValue] = Array.isArray(o) ? [o[0], o[1], true] : [o, o, false];
    return `<option${withValue ? ` value="${esc(v)}"` : ""} ${v === selected ? "selected" : ""}>${esc(label)}</option>`;
  }).join("")}</select>`;
}
// A label above its control; hint goes after the label text, on its line: the label is a grid, so the text and the
// hint are one span, not two rows (#5332).
export function field(label, control, hint = "") { return `<label>${hint ? `<span>${label}${hint}</span>` : label}${control}</label>`; }
export function two(a, b) { return `<div class="two">${a}${b}</div>`; }
export function actions(inner, style) { return `<div class="actions"${style ? ` style="${style}"` : ""}>${inner}</div>`; }

// Whether the person changed a field since it was drawn.
function edited(f) {
  if (f.type === "checkbox" || f.type === "radio") return f.checked !== f.defaultChecked;
  if (f.tagName === "SELECT") return [...f.options].some(o => o.selected !== o.defaultSelected);
  return f.value !== f.defaultValue;
}

// Replace el's content with html. Fields (by id) the person changed keep their values, and the
// focused field keeps its focus and cursor, so a refresh never wipes what someone is typing.
// fresh: drop the edits (after the person saved or sent them).
export function renderKeepingEdits(el, html, fresh = false) {
  const fields = fresh ? [] : [...el.querySelectorAll("input[id], textarea[id], select[id]")];
  const kept = fields.filter(edited).map(f => [f.id, f.type === "checkbox" || f.type === "radio" ? f.checked : f.value]);
  const a = document.activeElement;
  const focus = !fresh && el.contains(a) && a.id ? { id: a.id, start: a.selectionStart, end: a.selectionEnd } : null;
  const top = el.scrollTop;
  el.innerHTML = html;
  const find = (id) => el.querySelector("#" + CSS.escape(id));
  for (const [id, v] of kept) {
    const f = find(id); if (!f) continue;
    if (typeof v === "boolean") f.checked = v; else f.value = v;
  }
  if (focus) {
    const f = find(focus.id);
    if (f) { f.focus({ preventScroll: true }); try { if (focus.start != null) f.setSelectionRange(focus.start, focus.end); } catch (e) { /* not a text field */ } }
  }
  el.scrollTop = top;
}
