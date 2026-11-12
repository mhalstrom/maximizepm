// The Artifacts tab (#4620): what the sessions made or named, newest first (GET /api/artifacts): the canvases first
// (#4768: pages a session keeps in maxpm), then files they wrote or
// named that the side panel can show, local pages, and claude.ai artifact links. A filter by session and by item.
// Open shows it in the side panel beside its session's chat; Open full screen gives it a browser tab of its own.
import { $, esc, ago, clip } from "../lib.js";
import { chip } from "../components/chip.js";
import { openFullScreen } from "../components/sidePanel.js";
import { button } from "../components/button.js";
import { empty as emptyState } from "../components/empty.js";

const KIND = { canvas: "canvas", file: "file", page: "local page", artifact: "claude.ai artifact" };
let list = [];

function name(a) {
  if (a.kind === "canvas") return a.title || a.name;
  if (a.kind === "file") return a.target.split(/[\\/]/).pop();
  return a.target.replace(/^https?:\/\//, "");
}

// The rows of a list of artifacts, in the Artifacts tab and in a session's view (#4630): one copy of the row code.
// The list keeps its rows (el._rows) for wireRows.
export function showRows(el, rows, empty) {
  el._rows = rows;
  const html = rows.map((a, i) => `<div class="art-row">
      <div class="art-main"><b title="${esc(a.target)}">${esc(clip(name(a), 80))}</b>
        <div class="sub">${chip("c-p", esc(KIND[a.kind] || a.kind))} <span class="art-meta">${a.how === "wrote" ? "written by" : "named by"} ${esc(a.session)}${(a.sessions || []).length > 1 ? `, also ${esc(a.sessions.slice(1).join(", "))}` : ""}
          ${a.item ? ` · <span class="link" data-open="${a.item}">#${a.item}</span>` : ""}${a.at ? ` · ${ago(a.at)}` : ""}${a.kind === "canvas" ? ` · v${a.version}${a.open_comments ? ` · ${a.open_comments} open comment${a.open_comments === 1 ? "" : "s"}` : ""}` : ""}</span>
          <span class="muted art-path">${esc(clip(a.kind === "canvas" ? `${a.project}/${a.name}` : a.target, 120))}</span></div></div>
      <div class="art-btns">${a.kind === "artifact"
        ? `<a class="btn" href="${esc(a.target)}" target="_blank" rel="noopener noreferrer" title="claude.ai blocks showing it in a frame">Open on claude.ai</a>`
        : button("Open", { data: { "art-open": i } })}
        ${button("Open full screen", { data: { "art-full": i } })}</div>
    </div>`).join("") || emptyState(esc(empty));
  // #4844: the lists read again every 10 seconds; the same rows are not drawn again (a hover, a focus, and a
  // selection stay, and nothing flashes).
  if (el._html !== html) { el.innerHTML = html; el._html = html; }
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
  // One row for each thing (#5000), with each session that named it; with a session filter, Open shows it beside that one.
  const mine = (a) => !s || a.session === s ? a : { ...a, how: "named", session: s, sessions: [s, ...a.sessions.filter(x => x !== s)] };
  showRows($("#artList"), list.filter(a => (!s || (a.sessions || [a.session]).includes(s)) && (!item || String(a.item) === item)).map(mine),
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
      const names = [...new Set(list.flatMap(a => a.sessions || [a.session]))].sort();
      const opts = `<option value="">All sessions</option>` + names.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
      if (sel._html !== opts && document.activeElement !== sel) {  // an open menu stays open (#4844)
        sel.innerHTML = opts; sel._html = opts;
        if (names.includes(cur)) sel.value = cur;
      }
      render();
    } catch (e) {
      $("#artList").innerHTML = `<div class="muted">Could not read the artifacts: ${esc(e.message)}</div>`; $("#artList")._html = null;
    } finally { busy = false; }
  }
  $("#artSession").addEventListener("change", render);
  $("#artItem").addEventListener("input", render);
  wireRows($("#artList"), openBeside);
  setInterval(refresh, 10000);
  return { refresh };
}
