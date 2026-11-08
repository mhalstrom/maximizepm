// The Artifacts tab (#4620): what the sessions made or named, newest first (GET /api/artifacts): files they wrote or
// named that the side panel can show, local pages, and claude.ai artifact links. A filter by session and by item.
// Open shows it in the side panel beside its session's chat; Open full screen gives it a browser tab of its own.
import { $, esc, ago, clip } from "../lib.js";
import { chip } from "../components/chip.js";
import { openFullScreen } from "../components/sidePanel.js";

const KIND = { file: "file", page: "local page", artifact: "claude.ai artifact" };
let list = [];

function name(a) {
  if (a.kind === "file") return a.target.split(/[\\/]/).pop();
  return a.target.replace(/^https?:\/\//, "");
}

// The rows of a list of artifacts, in the Artifacts tab and in a session's view (#4630): one copy of the row code.
// The list keeps its rows (el._rows) for wireRows.
export function showRows(el, rows, empty) {
  el._rows = rows;
  el.innerHTML = rows.map((a, i) => `<div class="art-row">
      <div class="art-main"><b title="${esc(a.target)}">${esc(clip(name(a), 80))}</b>
        <div class="sub">${chip("c-p", esc(KIND[a.kind] || a.kind))} ${a.how === "wrote" ? "written by" : "named by"} ${esc(a.session)}
          ${a.item ? ` · <span class="link" data-open="${a.item}">#${a.item}</span>` : ""}${a.at ? ` · ${ago(a.at)}` : ""}
          <span class="muted art-path">${esc(clip(a.target, 120))}</span></div></div>
      <div class="art-btns">${a.kind === "artifact"
        ? `<a class="btn" href="${esc(a.target)}" target="_blank" rel="noopener noreferrer" title="claude.ai does not let another page show it in a frame">Open on claude.ai</a>`
        : `<button class="btn" type="button" data-art-open="${i}">Open</button>`}
        <button class="btn" type="button" data-art-full="${i}">Open full screen</button></div>
    </div>`).join("") || `<div class="muted">${esc(empty)}</div>`;
}

// Open: openBeside(session, target); Open full screen: a browser tab of its own.
export function wireRows(el, openBeside) {
  el.addEventListener("click", (e) => {
    const o = e.target.closest("[data-art-open]"), f = e.target.closest("[data-art-full]");
    if (o && (el._rows || [])[+o.dataset.artOpen]) { const r = el._rows[+o.dataset.artOpen]; openBeside(r.session, r.target); }
    if (f && (el._rows || [])[+f.dataset.artFull]) { const r = el._rows[+f.dataset.artFull]; openFullScreen(r.target, r.session, r.item || ""); }
  });
}

function render() {
  const s = $("#artSession").value, item = $("#artItem").value.replace(/\D/g, "");
  showRows($("#artList"), list.filter(a => (!s || a.session === s) && (!item || String(a.item) === item)),
           list.length ? "Nothing matches the filter." : "The sessions made or named nothing the side panel can show yet.");
}

export function makeArtifacts({ isShown, openBeside }) {
  let busy = false;
  async function refresh() {
    if (busy || document.hidden || !isShown()) return;
    busy = true;
    try {
      const r = await fetch("api/artifacts");
      const j = await r.json();
      list = j.artifacts || [];
      const sel = $("#artSession"), cur = sel.value;
      const names = [...new Set(list.map(a => a.session))].sort();
      sel.innerHTML = `<option value="">All sessions</option>` + names.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
      if (names.includes(cur)) sel.value = cur;
      render();
    } catch (e) {
      $("#artList").innerHTML = `<div class="muted">Could not read the artifacts: ${esc(e.message)}</div>`;
    } finally { busy = false; }
  }
  $("#artSession").addEventListener("change", render);
  $("#artItem").addEventListener("input", render);
  wireRows($("#artList"), openBeside);
  setInterval(refresh, 10000);
  return { refresh };
}
