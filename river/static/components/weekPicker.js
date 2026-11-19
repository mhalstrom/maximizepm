// A week calendar for a time window (#6205, mark 2026-11-19 on the Window dialog of a transition: 'probably a better
// way to do this is with a calander'): seven day columns and 24 hour rows; a click or a drag over the cells marks the
// hours when versions may pass, or clears them. The calendar writes the window text that the server reads
// (pipeline/transitions.parse_window: 'mon-thu 09:00-17:00 America/Los_Angeles', ';' between windows) into a text
// field, and a change of that text draws the calendar again. A text that whole hours cannot show (09:30) stays as it
// is, with a note. The time zone comes from the text, else from this browser.
import { esc } from "../lib.js";
import { button } from "./button.js";

const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const SETS = { weekdays: [0, 1, 2, 3, 4], weekends: [5, 6], daily: [0, 1, 2, 3, 4, 5, 6] };
const hh = (h) => String(h).padStart(2, "0") + ":00";

export function browserZone() {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; } catch (e) { return "UTC"; }
}

// The cells ("d:h") and the zone of a window text; null when the text has minutes or words the calendar cannot show.
export function gridFromWindow(text) {
  const cells = new Set();
  let zone = null;
  for (const part of String(text || "").split(";").map(p => p.trim()).filter(Boolean)) {
    const words = part.replace(/,/g, " ").split(/\s+/);
    if (/\/|^(utc|gmt)$/i.test(words[words.length - 1])) zone = words.pop();
    const spans = words.filter(w => /^\d{1,2}:\d{2}-\d{1,2}:\d{2}$/.test(w));
    if (spans.length !== 1) return null;
    const [a, b] = spans[0].split("-").map(x => x.split(":").map(Number));
    if (a[1] || b[1]) return null;  // minutes: the text stays
    const days = new Set();
    for (const w of words.filter(w => w !== spans[0]).map(w => w.toLowerCase())) {
      if (SETS[w]) SETS[w].forEach(d => days.add(d));
      else if (/^[a-z]{3}-[a-z]{3}$/.test(w) && DAYS.includes(w.slice(0, 3)) && DAYS.includes(w.slice(4))) {
        const x = DAYS.indexOf(w.slice(0, 3)), y = DAYS.indexOf(w.slice(4));
        for (let i = 0; i <= (y - x + 7) % 7; i++) days.add((x + i) % 7);
      } else if (DAYS.includes(w.slice(0, 3))) days.add(DAYS.indexOf(w.slice(0, 3)));
      else return null;
    }
    const end = b[0] === 0 || b[0] === 24 ? 24 : b[0];
    for (const d of days.size ? days : SETS.daily) {
      if (end > a[0]) for (let h = a[0]; h < end; h++) cells.add(`${d}:${h}`);
      else {  // over midnight: the rest of the day, then the next morning
        for (let h = a[0]; h < 24; h++) cells.add(`${d}:${h}`);
        for (let h = 0; h < end; h++) cells.add(`${(d + 1) % 7}:${h}`);
      }
    }
  }
  return { cells, zone };
}

// The window text of the marked cells: each day's runs of hours, the days with the same runs together.
export function windowFromGrid(cells, zone) {
  const byRun = new Map();
  for (let d = 0; d < 7; d++) {
    let h = 0;
    while (h < 24) {
      if (!cells.has(`${d}:${h}`)) { h++; continue; }
      const a = h;
      while (h < 24 && cells.has(`${d}:${h}`)) h++;
      const key = `${hh(a)}-${hh(h === 24 ? 0 : h)}`;
      if (!byRun.has(key)) byRun.set(key, []);
      byRun.get(key).push(d);
    }
  }
  const dayText = (ds) => {
    if (ds.length === 7) return "daily";
    const runs = [];
    for (const d of ds) { const r = runs[runs.length - 1]; if (r && r[1] === d - 1) r[1] = d; else runs.push([d, d]); }
    return runs.map(([a, b]) => a === b ? DAYS[a] : b === a + 1 ? `${DAYS[a]},${DAYS[b]}` : `${DAYS[a]}-${DAYS[b]}`).join(",");
  };
  return [...byRun].map(([span, ds]) => `${dayText(ds)} ${span}${zone ? " " + zone : ""}`).join("; ");
}

// The calendar's HTML for a window text; wire it with wireWeekPicker after it is in the page.
export function weekPickerHtml() {
  const head = `<span></span>${DAYS.map(d => `<span class="wk-day">${d}</span>`).join("")}`;
  const rows = Array.from({ length: 24 }, (_, h) => `<span class="wk-hour">${h % 3 ? "" : hh(h)}</span>`
    + DAYS.map((_, d) => `<span class="wk-cell" data-wk="${d}:${h}" title="${DAYS[d]} ${hh(h)}"></span>`).join("")).join("");
  return `<div class="wk"><div class="wk-grid" role="grid" aria-label="Open hours">${head}${rows}</div>
    <div class="wk-foot t-s"><label class="wk-zl">Time zone <input class="wk-zone" list="wkZones" aria-label="Time zone"
      autocomplete="off" spellcheck="false"><datalist id="wkZones"></datalist></label>
      <span class="wk-note muted"></span><span class="spacer"></span>
      ${button("Weekdays 9-17", { cls: "plain", data: { "wk-preset": "work" }, title: "Office hours" })}
      ${button("Any time", { cls: "plain", data: { "wk-preset": "none" }, title: "No window" })}</div></div>`;
}

// Connects the calendar in root to the text field: marks follow the text, and the text follows the marks.
export function wireWeekPicker(root, field) {
  const box = root.querySelector(".wk");
  if (!box || !field) return;
  let cells = new Set(), zone = browserZone(), paint = null;
  const note = box.querySelector(".wk-note"), sel = box.querySelector(".wk-zone");
  // The time zone selector (#6206, mark 2026-11-19: 'localize the timezone by default ... and have a selector'): a
  // field that suggests every zone this browser knows as the person types, this browser's zone first; a known zone
  // writes itself into the window text.
  const zones = (() => { try { return Intl.supportedValuesOf("timeZone"); } catch (e) { return []; } })();
  const known = new Set([browserZone(), "UTC", ...zones]);
  box.querySelector("#wkZones").innerHTML = [...known].map(z => `<option value="${esc(z)}"></option>`).join("");
  const fill = () => { sel.value = zone; };
  const draw = () => {
    box.querySelectorAll("[data-wk]").forEach(c => c.classList.toggle("on", cells.has(c.dataset.wk)));
    if (sel.value !== zone) fill();
    note.textContent = cells.size ? "" : "No hour marked: versions pass at any time.";
  };
  const fromText = () => {
    const g = gridFromWindow(field.value);
    if (!g) { note.textContent = "This window has minutes: change it in the text."; return; }
    cells = g.cells; if (g.zone) zone = g.zone;
    draw();
  };
  sel.addEventListener("change", () => {
    if (!known.has(sel.value.trim())) { sel.value = zone; note.textContent = "Pick a zone from the list."; return; }
    zone = sel.value.trim(); toText();
  });
  const toText = () => { field.value = windowFromGrid(cells, zone); draw(); };
  const mark = (c) => { if (!c || !paint) return; paint === "on" ? cells.add(c.dataset.wk) : cells.delete(c.dataset.wk); toText(); };
  box.addEventListener("pointerdown", (e) => {
    const c = e.target.closest("[data-wk]");
    if (!c) return;
    e.preventDefault();
    paint = cells.has(c.dataset.wk) ? "off" : "on";
    mark(c);
  });
  // A finger keeps its pointer on the first cell: the cell under the point is the one it is over.
  box.addEventListener("pointermove", (e) => {
    if (!paint) return;
    const el = document.elementFromPoint(e.clientX, e.clientY), c = el && el.closest("[data-wk]");
    if (c && box.contains(c) && (paint === "on") !== cells.has(c.dataset.wk)) mark(c);
  });
  window.addEventListener("pointerup", () => { paint = null; });
  box.addEventListener("click", (e) => {
    const p = e.target.closest("[data-wk-preset]");
    if (!p) return;
    cells = new Set();
    if (p.dataset.wkPreset === "work") for (const d of SETS.weekdays) for (let h = 9; h < 17; h++) cells.add(`${d}:${h}`);
    toText();
  });
  field.addEventListener("input", fromText);
  fill();
  fromText();
}
