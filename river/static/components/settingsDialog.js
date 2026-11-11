// The Settings dialog of a session (#4625): its chat provider's settings (the server's table for each provider),
// the value it runs with now and where that comes from (its transcript, its command line, or the default). A
// setting its provider changes in a running session (Claude Code /model, /effort) has "Change now": the command
// goes into its tmux pane while it is quiet. Every change can also go with "Restart with these settings": the
// session ends and the same chat resumes with them, with the same name, items and context; "Also make these the
// default" changes the maxpm config value too.
// Server: GET /api/session-settings, the ops session_setting and session_restart (with the page token).
import { $, esc, toast, typeAction } from "../lib.js";
import { makeDialog } from "./dialog.js";
import { viewOf, setView } from "./viewSettings.js";
import { nameChoices, OTHER } from "./modelPick.js";

const dlg = makeDialog($("#setDlg"));
let cur = null, onDone = null;
const SRC = { session: "from the session", "command line": "from its command line", default: "the default",
  // Sent from this page with no turn after it yet (#5288): the session names it at its next turn.
  "changed now": "changed from this page; the session names it at its next turn" };

function field(s, i) {
  const val = s.value == null ? "" : s.value;
  // A long model list (OpenCode: about 500) shows the adapter's own models and the current one; "Other model…" opens
  // a field that finds the rest by typing, as the launch dialog does (#5242).
  const { shown, all } = s.kind === "choice" ? nameChoices(s.choices, s.picks, val) : { shown: [], all: [] };
  const input = s.kind === "choice"
    ? `<select data-i="${i}" aria-label="${esc(s.label)}">${s.choices.includes(val) ? "" : `<option value="">${val ? esc(val) : "not known"}</option>`}${shown.map(c => `<option value="${esc(c)}"${c === val ? " selected" : ""}${s.notes && s.notes[c] ? ` title="${esc(s.notes[c])}"` : ""}>${esc(c)}${s.notes && s.notes[c] && s.notes[c].split(" · ")[0] !== c ? ` (${esc(s.notes[c].split(" · ")[0])})` : ""}</option>`).join("")}${
      all.length ? `<option value="${OTHER}">Other model…</option>` : ""}</select>${all.length ? `
      <input data-other="${i}" class="hidden" list="setAll${i}" autocomplete="off" spellcheck="false"
        placeholder="Type to find one of ${all.length} models" aria-label="Other ${esc(s.label.toLowerCase())}">
      <datalist id="setAll${i}">${all.map(c => `<option value="${esc(c)}"></option>`).join("")}</datalist>` : ""}`
    : `<input data-i="${i}" value="${esc(val)}" aria-label="${esc(s.label)}" spellcheck="false">`;
  const how = s.live ? `<button class="btn" type="button" data-live="${i}" title="Type the change into its terminal now">Change now</button>`
    : `<span class="muted" title="Its chat provider changes this only when the session starts">restart</span>`;
  // A setting that neither Change now nor a restart can change (OpenCode's model, #5285): its value only; the note
  // says how a person changes it. It sends no change.
  if (s.fixed) return `<div class="set-row"><label>${esc(s.label)}</label><span>${val ? esc(val) : "not known"}</span>
    <span class="muted" title="A restart keeps this value">fixed</span>
    <div class="set-sub muted">${val ? esc(SRC[s.source] || s.source || "") : "MaximizePM cannot tell"}${s.note ? ` · ${esc(s.note)}` : ""}</div></div>`;
  return `<div class="set-row"><label>${esc(s.label)}</label>${input}${how}
    <div class="set-sub muted">${val ? esc(SRC[s.source] || s.source || "") : "MaximizePM cannot tell"}${s.reported && s.reported !== val ? ` (it reports ${esc(s.reported)})` : ""}${s.default_key ? ` · default ${esc(s.default_key)} = ${esc(s.default_value || "(the CLI's own)")}` : ""}${s.note ? ` · ${esc(s.note)}` : ""}</div></div>`;
}

// The page's view of this session (#4635): changes at once, in this browser only; no restart.
function pageSection(agent) {
  const v = viewOf(agent);
  return `<div class="muted set-head">This page</div>
    <div class="set-row"><label for="setTools">Show tool calls</label>
      <select id="setTools" data-view="showTools" data-agent="${esc(agent)}"><option value="on"${v.showTools ? " selected" : ""}>on</option><option value="off"${v.showTools ? "" : " selected"}>off: a marker such as "12 tool calls" opens them</option></select>
      <span class="muted">now</span>
      <div class="set-sub muted">The tool call lines (Bash, Write, ...) between the messages of its chat; kept for this session in this browser</div></div>`;
}

// The value of row i: its select or field, or the typed model of "Other model…" (#5242).
function valueOf(i) {
  const el = document.querySelector(`#setList [data-i="${i}"]`);
  if (!el || el.value !== OTHER) return el ? el.value : "";
  const o = document.querySelector(`#setList [data-other="${i}"]`);
  return o ? o.value.trim() : "";
}

function changes() {
  const out = {};
  document.querySelectorAll("#setList [data-i]").forEach(el => {
    const s = cur.settings[+el.dataset.i], v = valueOf(el.dataset.i);
    if (v && v !== (s.value || "")) out[s.key] = v;
  });
  return out;
}

function sync() {
  const n = Object.keys(changes()).length;
  $("#setRestart").disabled = !n;
  $("#setRestart").textContent = n ? `Restart with ${n === 1 ? "this setting" : `these ${n} settings`}` : "Restart with these settings";
}

// focus: the key of a setting whose field gets the focus (#5220: /model and /effort from the message box).
export async function openSettings(agent, { done, focus } = {}) {
  onDone = done;
  $("#setTitle").textContent = `Settings of ${agent}`;
  $("#setList").innerHTML = `<div class="muted">Reading the session…</div>`;
  $("#setDefault").checked = false;
  dlg.open();
  try {
    const r = await fetch("api/session-settings?agent=" + encodeURIComponent(agent));
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "failed");
    cur = j;
    $("#setList").innerHTML = pageSection(agent) + `<div class="muted set-head">${esc(j.provider.label)}${j.transcript ? "" : " · no transcript here: the values come from its command line"}</div>`
      + j.settings.map(field).join("");
    sync();
    const at = focus ? j.settings.findIndex(x => x.key === focus) : -1;
    const el = at >= 0 && document.querySelector(`#setList [data-i="${at}"]`);
    if (el) { el.closest(".set-row").classList.add("on"); el.focus(); el.scrollIntoView({ block: "nearest" }); }
  } catch (e) { $("#setList").innerHTML = pageSection(agent) + `<div class="muted">${esc(e.message)}</div>`; cur = null; }
}

$("#setList").addEventListener("input", sync);
$("#setList").addEventListener("change", (e) => {
  const v = e.target.closest("[data-view]");
  if (v) return setView(v.dataset.agent, v.dataset.view, v.value === "on");
  if (e.target.dataset.i != null) {  // "Other model…" shows its field
    const o = document.querySelector(`#setList [data-other="${e.target.dataset.i}"]`);
    if (o) { o.classList.toggle("hidden", e.target.value !== OTHER); if (e.target.value === OTHER) o.focus(); }
  }
  sync();
});
$("#setList").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-live]"); if (!b || !cur) return;
  const s = cur.settings[+b.dataset.live], v = valueOf(b.dataset.live);
  if (!v) return toast(`${s.label}: pick or type a value first`, true);
  b.disabled = true;
  try {
    await typeAction("session_setting", { agent: cur.agent, key: s.key, value: v });
    toast(`${s.label}: ${v} typed into its terminal`);
    openSettings(cur.agent, { done: onDone });
  } catch (err) { toast(err.message, true); } finally { b.disabled = false; }
});
$("#setRestart").addEventListener("click", async () => {
  const ch = changes(); if (!cur || !Object.keys(ch).length) return;
  $("#setRestart").disabled = true;
  try {
    const r = await typeAction("session_restart", { agent: cur.agent, changes: ch, make_default: $("#setDefault").checked });
    toast(`${cur.agent} restarts with ${r.changed.join("; ")}${r.defaults.length ? ` (also the default: ${r.defaults.join(", ")})` : ""}`);
    dlg.close();
    if (onDone) onDone();
  } catch (err) { toast(err.message, true); sync(); }
});
$("#setClose").addEventListener("click", () => dlg.close());
