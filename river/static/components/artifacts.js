// The Artifacts tab (#4620): what the sessions made or named, newest first (GET /api/artifacts): files they wrote or
// named that the side panel can show, local pages, and claude.ai artifact links. A filter by session and by item.
// Open shows it in the side panel beside its session's chat; Open full screen gives it a browser tab of its own.
import { $, esc, ago, clip } from "../lib.js";
import { chip } from "./chip.js";
import { openFullScreen } from "./sidePanel.js";

const KIND = { file: "file", page: "local page", artifact: "claude.ai artifact" };
let list = [];

function name(a) {
  if (a.kind === "file") return a.target.split("/").pop();
  return a.target.replace(/^https?:\/\//, "");
}

function render() {
  const s = $("#artSession").value, item = $("#artItem").value.replace(/\D/g, "");
  const rows = list.filter(a => (!s || a.session === s) && (!item || String(a.item) === item));
  $("#artList").innerHTML = rows.map((a, i) => `<div class="art-row">
      <div class="art-main"><b title="${esc(a.target)}">${esc(clip(name(a), 80))}</b>
        <div class="sub">${chip("c-p", esc(KIND[a.kind] || a.kind))} ${a.how === "wrote" ? "written by" : "named by"} ${esc(a.session)}
          ${a.item ? ` · <span class="link" data-open="${a.item}">#${a.item}</span>` : ""}${a.at ? ` · ${ago(a.at)}` : ""}
          <span class="muted art-path">${esc(clip(a.target, 120))}</span></div></div>
      <div class="art-btns">${a.kind === "artifact"
        ? `<a class="btn" href="${esc(a.target)}" target="_blank" rel="noopener noreferrer" title="claude.ai does not let another page show it in a frame">Open on claude.ai</a>`
        : `<button class="btn" type="button" data-art-open="${i}">Open</button>`}
        <button class="btn" type="button" data-art-full="${i}">Open full screen</button></div>
    </div>`).join("") || `<div class="muted">${list.length ? "Nothing matches the filter." : "The sessions made or named nothing the side panel can show yet."}</div>`;
  $("#artList").dataset.rows = JSON.stringify(rows.map(a => [a.target, a.session, a.item]));
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
  $("#artList").addEventListener("click", (e) => {
    const rows = JSON.parse($("#artList").dataset.rows || "[]");
    const o = e.target.closest("[data-art-open]"), f = e.target.closest("[data-art-full]");
    if (o) { const [target, session] = rows[+o.dataset.artOpen]; openBeside(session, target); }
    if (f) { const [target, session, item] = rows[+f.dataset.artFull]; openFullScreen(target, session, item || ""); }
  });
  setInterval(refresh, 10000);
  return { refresh };
}
