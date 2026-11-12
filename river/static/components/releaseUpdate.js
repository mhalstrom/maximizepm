// A new MaximizePM release (#5601): the bar at the foot of the page, "A new version of MaximizePM is available", and
// its dialog with what is new and one button. The server keeps the last check of GitHub (river/release.py) and sends
// it in GET /api/update as release; how says what the button does: code (the app downloads the new code, checks it
// and starts again), shell (the release needs the new app: it downloads the .dmg and opens it), git (the git Update
// of a clone), other (the person updates it the way they installed it). Later hides the bar for a day.
import { esc, store, toast } from "../lib.js";
import { makeDialog } from "./dialog.js";
import { button, iconButton } from "./button.js";
import { markdown } from "./chat.js";

const LATER = "maxpm-release-later";
const DAY = 24 * 60 * 60 * 1000;
let rel = null, opts = {}, dlg = null, root = null, bar = null;

function later() {
  try { const v = JSON.parse(store(LATER) || "null"); return v && v.version === rel.version && Date.now() - v.at < DAY; }
  catch (e) { return false; }
}

// What the button does, in the person's words.
export function howText(r) {
  if (r.how === "code") return "Update downloads the new version, checks it, and starts MaximizePM again. Your queue and your sessions stay as they are.";
  if (r.how === "shell") return `This version needs the new app. Download saves ${r.dmg} in Downloads and opens it: drag MaximizePM to Applications to replace the old app, then open it again.`;
  if (r.how === "git") return "Update brings the new code into this folder (git) and starts MaximizePM again. Your queue and your sessions stay as they are.";
  return "Update it the way you installed it. The release page has the files and the steps.";
}

export function releaseHtml(r) {
  const go = r.how === "code" || r.how === "git" ? button("Update and restart", { kind: "primary", data: { "rel-go": "update" } })
    : r.how === "shell" ? button("Download the new app", { kind: "primary", data: { "rel-go": "download" } })
    : `<a class="btn primary" href="${esc(r.url)}" target="_blank" rel="noopener">Open the release page</a>`;
  return `<div class="dlg-head"><h2 id="relTitle">MaximizePM ${esc(r.version)} is available</h2>${iconButton("✕", "Close", { data: { "rel-close": "1" } })}</div>
    <p class="t-m muted">You have ${esc(r.current)}. <a href="${esc(r.url)}" target="_blank" rel="noopener">Release page</a></p>
    <div class="rel-notes">${r.notes ? markdown(r.notes) : "<p>No notes for this release.</p>"}</div>
    <p class="t-m">${esc(howText(r))}</p>
    <div class="actions">${button("Later", { data: { "rel-later": "1" } })}${go}</div>`;
}

function dialog() {
  if (dlg) return;
  const d = document.createElement("div");
  d.id = "relDlg"; d.className = "hidden";
  d.innerHTML = '<div class="box rel-box" role="dialog" aria-modal="true" aria-labelledby="relTitle"></div>';
  document.body.appendChild(d);
  root = d.firstElementChild;
  dlg = makeDialog(d, { draft: false });
  d.addEventListener("click", (e) => {
    if (e.target === d) return;  // the overlay: makeDialog closes it
    if (e.target.closest("[data-rel-close]")) return dlg.close();
    if (e.target.closest("[data-rel-later]")) { hideForADay(); return dlg.close(); }
    const b = e.target.closest("[data-rel-go]");
    if (b && !b.disabled) run(b);
  });
}

function hideForADay() {
  store(LATER, JSON.stringify({ version: rel.version, at: Date.now() }));
  draw();
}

async function run(b) {
  const what = b.dataset.relGo;
  b.disabled = true; b.textContent = what === "download" ? "Downloading…" : "Updating…";
  try {
    const boot = opts.boot ? await opts.boot() : null;
    const r = await fetch("api/action", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ op: what === "download" ? "release_download" : "release_update", args: {} }) });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "the update failed");
    const res = j.result;
    if (what === "download") {
      dlg.close();
      return toast(`Saved ${res.path}. Drag MaximizePM to Applications, then open it again.`);
    }
    if (res.updated === false) { dlg.close(); return toast("MaximizePM is up to date"); }
    toast(res.to ? `Updated to ${res.to}. Starting again…` : "Updated. Starting again…");
    if (opts.waitForRestart) await opts.waitForRestart(boot);
  } catch (e) {
    toast(e.message, true);
    b.disabled = false; b.textContent = what === "download" ? "Download the new app" : "Update and restart";
  }
}

export function openRelease() {
  if (!rel) return;
  dialog();
  root.innerHTML = releaseHtml(rel);
  dlg.open();
  const go = root.querySelector("[data-rel-go], .btn.primary"); if (go) go.focus();
}

function draw() {
  const show = rel && rel.newer && !later();
  if (!show) { if (bar) bar.remove(); bar = null; return; }
  if (!bar) {
    bar = document.createElement("div"); bar.id = "relBar"; bar.className = "relbar"; bar.setAttribute("role", "status");
    bar.addEventListener("click", (e) => {
      if (e.target.closest("[data-rel-open]")) return openRelease();
      if (e.target.closest("[data-rel-later]")) return hideForADay();
    });
    document.body.append(bar);
  }
  bar.innerHTML = `<span>A new version of MaximizePM is available: ${esc(rel.version)}</span>
    ${button("What's new", { kind: "primary", data: { "rel-open": "1" } })}${iconButton("✕", "Later", { data: { "rel-later": "1" } })}`;
}

// The release part of GET /api/update; o: { boot: () => the server's boot id, waitForRestart(boot) }.
export function showRelease(r, o = {}) {
  rel = r && r.newer ? r : null; opts = o;
  draw();
  return !!rel;
}
