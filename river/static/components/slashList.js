// The "/" list of a message box (#5186, docs/agent-adapters.md part 10): when the text starts with "/" and the caret
// is still in the command's name, a list of the matching slash commands of the session's agent opens above the box.
// Tab completes the chosen command ("/name " and the rest of the text stays); Up and Down move in the list while it
// is open (the view's own Up and Down, the recall of earlier prompts, act only while it is closed); Esc closes it
// until the text changes again; a click on a row completes it. Enter is not taken: the box sends its text as before,
// except after Up or Down chose a row, when Enter completes that row first and then sends.
//
// makeSlashList(input, host, { load, onChange }): input is the textarea; the list goes into host (a box with
// position: relative, above which it opens). load() returns a Promise of [{name, description, hint}]; key() names
// the session: when it changes, the next "/" loads that session's commands. onChange() runs after a
// completion changed the text (the view resizes the box and saves the draft). keydown(e) returns true when the list
// took the key; the view then does nothing more with it.
import { esc } from "../lib.js";

const MAX_ROWS = 50;
let seq = 0;

export function makeSlashList(input, host, { load, key = () => "", onChange = () => {} }) {
  const id = "slash" + (++seq);
  const box = document.createElement("div");
  box.className = "slash-list hidden";
  box.id = id;
  box.setAttribute("role", "listbox");
  box.setAttribute("aria-label", "Slash commands");
  host.append(box);
  input.setAttribute("aria-controls", id);
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-expanded", "false");

  let commands = null, loading = null, gen = 0, forKey;
  let shown = [], at = 0, moved = false, closedFor = null;

  // The command name being typed: "/" at the very start, then no white space up to the caret. null otherwise.
  function query() {
    const v = input.value, caret = input.selectionStart;
    if (!v.startsWith("/") || input.selectionEnd !== caret) return null;
    const word = v.slice(1).match(/^\S*/)[0];
    if (caret < 1 || caret > 1 + word.length) return null;
    return word;
  }
  function matches(q) {
    const l = q.toLowerCase(), head = [], inside = [];
    for (const c of commands || []) {
      const n = c.name.toLowerCase();
      if (n.startsWith(l)) head.push(c);
      else if (l && n.includes(l)) inside.push(c);
    }
    return head.concat(inside).slice(0, MAX_ROWS);
  }
  const isOpen = () => !box.classList.contains("hidden");
  function close() {
    box.classList.add("hidden");
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    shown = []; moved = false;
  }
  function draw() {
    box.innerHTML = shown.map((c, i) =>
      `<div class="slash-row${i === at ? " on" : ""}" role="option" id="${id}-${i}" aria-selected="${i === at}" data-i="${i}">` +
      `<span class="slash-name">/${esc(c.name)}${c.hint ? ` <span class="slash-hint">${esc(c.hint)}</span>` : ""}</span>` +
      `<span class="slash-desc">${esc(c.description || "")}</span></div>`).join("");
    input.setAttribute("aria-activedescendant", `${id}-${at}`);
    const row = box.children[at];
    if (row) row.scrollIntoView({ block: "nearest" });
  }
  // Reads the text and the caret again: opens, filters or closes the list.
  function update() {
    const q = query();
    if (q === null || input.disabled) { closedFor = null; return close(); }
    if (closedFor !== null && closedFor === input.value) return close();  // Esc closed it for this very text
    closedFor = null;
    if (key() !== forKey) reset();
    if (commands === null) {
      if (!loading) {
        const mine = gen;
        loading = Promise.resolve(load()).catch(() => []).then(got => {
          if (mine !== gen) return;
          commands = Array.isArray(got) ? got : [];
          loading = null;
          update();
        });
      }
      return;
    }
    const before = shown[at] && shown[at].name;
    shown = matches(q);
    if (!shown.length) return close();
    const keep = shown.findIndex(c => c.name === before);
    at = moved && keep >= 0 ? keep : 0;
    if (keep < 0) moved = false;
    box.classList.remove("hidden");
    input.setAttribute("aria-expanded", "true");
    draw();
  }
  function reset() { gen++; forKey = key(); commands = null; loading = null; closedFor = null; close(); }
  function complete(i) {
    const c = shown[i];
    if (!c) return;
    const v = input.value, word = v.slice(1).match(/^\S*/)[0];
    let rest = v.slice(1 + word.length);
    if (!rest.startsWith(" ")) rest = " " + rest;
    input.value = "/" + c.name + rest;
    const caret = 2 + c.name.length;
    input.setSelectionRange(caret, caret);
    close();
    onChange();
  }
  function keydown(e) {
    if (!isOpen() || e.isComposing) return false;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return false;
      at = (at + (e.key === "ArrowDown" ? 1 : -1) + shown.length) % shown.length;
      moved = true;
      draw();
      return true;
    }
    if (e.key === "Tab" && !e.shiftKey && !e.altKey && !e.ctrlKey && !e.metaKey) { complete(at); return true; }
    if (e.key === "Escape") { closedFor = input.value; close(); return true; }
    if (e.key === "Enter" && !e.shiftKey && moved) { complete(at); return false; }  // the view sends the completed text
    return false;
  }
  // A click on a row completes it; the box keeps the focus (mousedown would take it away).
  box.addEventListener("mousedown", (e) => e.preventDefault());
  box.addEventListener("click", (e) => {
    const row = e.target.closest(".slash-row");
    if (row) { complete(Number(row.dataset.i)); input.focus(); }
  });
  input.addEventListener("input", update);
  input.addEventListener("click", update);
  input.addEventListener("keyup", (e) => { if (e.key === "ArrowLeft" || e.key === "ArrowRight" || e.key === "Home" || e.key === "End") update(); });
  input.addEventListener("blur", close);
  return {
    keydown, close, update, isOpen, reset,
  };
}
