// An agent's terminal on the page: what its tmux pane shows (agents started with launch_in tmux), read
// again twice a second while the dialog is open, and the keys a person types there go to the pane. So a
// prompt that waits in the agent's terminal can be read and answered from the page.
// The dialog has its own size (app.css): the pane's columns and rows never change it. A pane wider or taller
// than the screen scrolls inside it, and the view stays at the end of the pane's text, where the prompt is.
// Server: GET /api/terminal?agent=<name>, and the op terminal_keys; both answer this computer only.
import { $, esc, actor, toast } from "../lib.js";
import { makeDialog } from "./dialog.js";

const FG = "#d4d4d4", BG = "#1e1e1e";
const BASE = ["#000000", "#cd3131", "#0dbc79", "#e5e510", "#2472c8", "#bc3fbc", "#11a8cd", "#e5e5e5",
              "#666666", "#f14c4c", "#23d18b", "#f5f543", "#3b8eea", "#d670d6", "#29b8db", "#ffffff"];

// One of the terminal's 256 colours: the 16 named ones, a 6x6x6 cube, then 24 greys.
function color256(n) {
  if (n < 16) return BASE[n];
  if (n > 231) { const v = 8 + (n - 232) * 10; return `rgb(${v},${v},${v})`; }
  const c = n - 16, step = (x) => (x ? 55 + x * 40 : 0);
  return `rgb(${step(Math.floor(c / 36))},${step(Math.floor(c / 6) % 6)},${step(c % 6)})`;
}

const SEQ = /\x1b\[([0-9;:]*)m|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;:?<=>]*[ -\/]*[@-~]|\x1b[()][0-9A-Za-z]|\x1b./g;

// Text with terminal colour sequences (tmux capture-pane -e) as HTML. Sequences that are not colours go.
// Safe to put in the page: every piece of text goes through esc, and a style holds only a colour made here.
export function ansiToHtml(text) {
  let s = { fg: null, bg: null, bold: false, dim: false, italic: false, under: false, inverse: false, strike: false };
  let out = "", at = 0;
  const flush = (chunk) => {
    if (!chunk) return;
    const fg = s.inverse ? (s.bg || BG) : s.fg, bg = s.inverse ? (s.fg || FG) : s.bg;
    const css = [fg && `color:${fg}`, bg && `background:${bg}`, s.bold && "font-weight:bold", s.dim && "opacity:.6",
                 s.italic && "font-style:italic", (s.under || s.strike) && `text-decoration:${[s.under && "underline", s.strike && "line-through"].filter(Boolean).join(" ")}`]
      .filter(Boolean).join(";");
    out += css ? `<span style="${css}">${esc(chunk)}</span>` : esc(chunk);
  };
  const re = new RegExp(SEQ);
  for (let m; (m = re.exec(text));) {
    flush(text.slice(at, m.index));
    at = re.lastIndex;
    if (m[1] === undefined) continue;
    const p = m[1].split(/[;:]/).map(x => (x === "" ? 0 : +x));
    for (let i = 0; i < p.length; i++) {
      const c = p[i];
      if (c === 0) s = { fg: null, bg: null, bold: false, dim: false, italic: false, under: false, inverse: false, strike: false };
      else if (c === 1) s.bold = true; else if (c === 2) s.dim = true; else if (c === 3) s.italic = true;
      else if (c === 4) s.under = true; else if (c === 7) s.inverse = true; else if (c === 9) s.strike = true;
      else if (c === 22) s.bold = s.dim = false; else if (c === 23) s.italic = false; else if (c === 24) s.under = false;
      else if (c === 27) s.inverse = false; else if (c === 29) s.strike = false;
      else if (c >= 30 && c <= 37) s.fg = BASE[c - 30]; else if (c >= 90 && c <= 97) s.fg = BASE[c - 82];
      else if (c >= 40 && c <= 47) s.bg = BASE[c - 40]; else if (c >= 100 && c <= 107) s.bg = BASE[c - 92];
      else if (c === 39) s.fg = null; else if (c === 49) s.bg = null;
      else if (c === 38 || c === 48) {
        let col = null;
        if (p[i + 1] === 5) { col = color256(p[i + 2] || 0); i += 2; }
        else if (p[i + 1] === 2) { const v = p.slice(i + 2).slice(-3); col = `rgb(${v[0] || 0},${v[1] || 0},${v[2] || 0})`; i = p.length; }
        if (c === 38) s.fg = col; else s.bg = col;
      }
    }
  }
  flush(text.slice(at));
  return out;
}

// The pane's rows to the last one with text, or to the cursor's row when that is further down. A pane taller
// than its text ends in empty rows; without them the end of the view is the prompt.
export function usedRows(text, cursorRow = 0) {
  const rows = String(text).split("\n");
  let n = rows.length;
  while (n > cursorRow + 1 && !rows[n - 1].replace(SEQ, "").trim()) n--;
  return rows.slice(0, n).join("\n");
}

// A key press as what the server sends to tmux: {key: <tmux name>}, {text: <the character>}, or null
// (the browser keeps it: Command shortcuts, a modifier alone).
const NAMED = { Enter: "Enter", Escape: "Escape", Backspace: "BSpace", Delete: "DC", ArrowUp: "Up", ArrowDown: "Down",
                ArrowLeft: "Left", ArrowRight: "Right", Home: "Home", End: "End", PageUp: "PPage", PageDown: "NPage" };
export function keyOf(e) {
  if (e.metaKey) return null;
  if (e.key === "Tab") return { key: e.shiftKey ? "BTab" : "Tab" };
  if (NAMED[e.key]) return { key: NAMED[e.key] };
  if (e.ctrlKey && !e.altKey && /^[a-z]$/i.test(e.key)) return { key: "C-" + e.key.toLowerCase() };
  if (!e.ctrlKey && e.key.length === 1) return { text: e.key };
  return null;
}

let dlg = null, agent = null, timer = null, shown = null, toEnd = true, pending = [], sending = false;

function el() {
  let d = $("#termDlg");
  if (d) return d;
  d = document.createElement("div");
  d.id = "termDlg"; d.className = "hidden";
  d.setAttribute("role", "dialog"); d.setAttribute("aria-modal", "true"); d.setAttribute("aria-labelledby", "termTitle");
  d.innerHTML = `<div class="box">
    <div style="display:flex;justify-content:space-between;align-items:baseline;gap:8px">
      <h2 id="termTitle" style="margin:0"></h2><button class="iconbtn" data-term-close="1" title="Close">✕</button></div>
    <div class="muted" id="termNote" style="font-size:12px;margin:4px 0 8px"></div>
    <pre class="term" id="termScreen" tabindex="0" aria-label="The agent's terminal. Keys you type here go to the agent."></pre>
    <div class="actions" style="margin-top:8px;align-items:center">
      ${[["Enter", "Enter"], ["Esc", "Escape"], ["↑", "Up"], ["↓", "Down"], ["Tab", "Tab"], ["Shift+Tab", "BTab"], ["Ctrl-C", "C-c"]]
        .map(([label, key]) => `<button class="btn" data-term-key="${key}">${label}</button>`).join("")}
      <span class="muted" style="font-size:12px">Click the terminal, then type: your keys go to the agent, Esc too. Close with ✕.</span>
    </div></div>`;
  document.body.appendChild(d);
  dlg = makeDialog(d, { onClose: () => { agent = null; clearTimeout(timer); } });
  d.addEventListener("click", (e) => {
    if (e.target === d || e.target.closest("[data-term-close]")) return dlg.close();
    const k = e.target.closest("[data-term-key]");
    if (k) { send({ key: k.dataset.termKey }); $("#termScreen").focus(); }
  });
  const scr = d.querySelector("#termScreen");
  scr.addEventListener("keydown", (e) => {
    const k = keyOf(e);
    if (!k) return;
    e.preventDefault(); e.stopPropagation();  // Escape goes to the agent; it does not close the dialog
    send(k);
  });
  scr.addEventListener("paste", (e) => {
    const text = (e.clipboardData || window.clipboardData).getData("text");
    e.preventDefault();
    for (let i = 0; i < text.length; i += 4000) send({ text: text.slice(i, i + 4000) });
  });
  return d;
}

// Keys go in the order typed: one request at a time, with the characters typed meanwhile joined.
async function send(k) {
  const last = pending[pending.length - 1];
  if (k.text && last && last.text && last.text.length + k.text.length <= 4000) last.text += k.text; else pending.push({ ...k });
  const scr = $("#termScreen");
  scr.scrollTop = scr.scrollHeight;  // a key goes to the prompt: show it again after the person scrolled away
  if (sending) return;
  sending = true;
  try {
    while (pending.length && agent) {
      const keys = pending.splice(0, pending.length);
      const r = await fetch("api/action", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ op: "terminal_keys", args: { agent, keys }, actor: actor() }) });
      if (!r.ok) { pending = []; toast(((await r.json().catch(() => ({}))).error) || "the keys did not go", true); }
    }
  } finally { sending = false; }
  clearTimeout(timer); tick();
}

// Draw one answer of /api/terminal. The view follows the end of the text while the person is there; after
// the person scrolled up it stays there. Sideways it stays where it is.
export function showScreen(j) {
  $("#termNote").textContent = `${j.name || j.pane} · ${j.width}×${j.height}` + (j.ended ? " · the agent has ended: a shell runs here" : "");
  if (j.text === shown) return;
  const scr = $("#termScreen");
  if (String(window.getSelection()) && scr.contains(window.getSelection().anchorNode)) { shown = null; return; }  // the person selects text to copy: draw again after that
  shown = j.text;
  const atEnd = toEnd || scr.scrollHeight - scr.scrollTop - scr.clientHeight < 4;
  scr.innerHTML = ansiToHtml(usedRows(j.text, (j.cursor || [])[1]));
  if (atEnd) scr.scrollTop = scr.scrollHeight;
  toEnd = false;
}

async function tick() {
  if (!agent) return;
  const who = agent;
  let wait = document.hidden ? 3000 : 500;
  try {
    const r = await fetch("api/terminal?agent=" + encodeURIComponent(who));
    const j = await r.json();
    if (who !== agent) return;
    if (!r.ok) { $("#termNote").textContent = j.error || "no terminal"; wait = 3000; }
    else showScreen(j);
  } catch (e) { $("#termNote").textContent = "maxpm serve does not answer"; wait = 3000; }
  if (who === agent) timer = setTimeout(tick, wait);
}

// Open the terminal of an agent that runs in a tmux pane (S.terminals lists them).
export function openTerminal(name) {
  el();
  agent = name; shown = null; toEnd = true; pending = [];
  $("#termTitle").textContent = `Terminal: ${name}`;
  $("#termNote").textContent = "\u00a0";  // keeps its line, so the screen has its size before the first answer
  $("#termScreen").innerHTML = "";
  dlg.open();
  $("#termScreen").focus();
  clearTimeout(timer); tick();
}
