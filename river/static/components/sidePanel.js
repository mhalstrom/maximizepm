// The side panel of the Sessions tab (#4619): what a session points to, beside its chat. A local page (localhost,
// 127.0.0.1) in a frame; a local file (image, PDF, HTML, Markdown, text) that maxpm serve gives read only
// (GET /api/file, with the page's token, only under the project folders, their worktrees, and uploads); an outside
// page in a frame when the site allows it (GET /api/frame-check), else its title and Open in a new tab. Tabs for
// several, an address bar, Reload, Open in a new tab, Close, and a handle to resize it (the width stays).
// Each session has its own tabs (#4632), kept in this browser across reloads; each tab shows what it holds (file,
// image, page, site, tool). A tool (#4631) opens as a tab too: openTool(key, title, render).
// The X of the last tab keeps the panel open, with the list of the session's artifacts to open (#4646); the panel's
// own X closes it.
import { $, esc, store, toast, pageToken } from "../lib.js";
import { markdown } from "./chat.js";
import { showRows, wireRows } from "./artifacts.js";

const WIDTH = "river.sidepanel.width", TABS = "river.sidepanel.tabs";
const LOCAL = /^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(:\d+)?(\/|$)/i;
const IMAGE = /\.(png|jpe?g|gif|webp|svg)$/i, MD = /\.(md|txt)$/i;

let tabs = [], on = null, n = 0, session = "", listed = false;
const bySession = {};  // name -> {tabs, on, listed}
const tools = {};      // key -> render(box): the tool tabs of #4631
const panel = () => $("#sessPanel");

function isFile(target) { return /^(?:\/|~[/\\]|[a-z]:[/\\]|\\\\)/i.test(target); }
function label(target) {
  if (isFile(target)) return target.split(/[\\/]/).pop();
  try { const u = new URL(target); return u.host + (u.pathname.length > 1 ? u.pathname : ""); } catch (e) { return target; }
}
async function fileUrl(path) {
  return `api/file?path=${encodeURIComponent(path)}&token=${encodeURIComponent(await pageToken())}`;
}

function kindOf(target) {
  if (target.startsWith("tool:")) return "tool";
  if (isFile(target)) return IMAGE.test(target) ? "image" : "file";
  return LOCAL.test(target) ? "page" : "site";
}
const KIND_MARK = { image: "▣", file: "▤", page: "◧", site: "◎", tool: "⚙" };

// The tabs of each session stay in this browser (targets only; a tool tab comes back when its tool opens again).
function save() {
  if (!session) return;
  bySession[session] = { tabs, on, listed };
  let all = {};
  try { all = JSON.parse(store(TABS) || "{}") || {}; } catch (e) { all = {}; }
  const keep = tabs.filter(t => kindOf(t.target) !== "tool");
  if (keep.length) all[session] = { targets: keep.map(t => t.target), on: (tabs.find(t => t.id === on) || {}).target || "" };
  else delete all[session];
  store(TABS, JSON.stringify(all));
}

function renderTabs() {
  $("#spTabs").innerHTML = tabs.map(t => { const k = kindOf(t.target);
    return `<span class="sp-tab${t.id === on ? " on" : ""}" title="${esc(t.target)}">`
    + `<button type="button" class="sp-tab-l" role="tab" aria-selected="${t.id === on}" data-tab="${t.id}"><span class="sp-k sp-k-${k}" aria-label="${k}">${KIND_MARK[k]}</span> ${esc(t.title || label(t.target))}</button>`
    + `<button type="button" class="sp-x" data-close="${t.id}" title="Close this tab" aria-label="Close ${esc(t.title || label(t.target))}">✕</button></span>`; }).join("");
  save();
}

async function show(t) {
  on = t.id; listed = false;
  renderTabs();
  $("#spAddr").value = t.target;
  const box = $("#spBody");
  box.replaceChildren();
  const note = (html) => { const d = document.createElement("div"); d.className = "sp-note"; d.innerHTML = html; box.append(d); };
  const frame = (src) => { const f = document.createElement("iframe"); f.src = src; f.className = "sp-frame";
    f.setAttribute("sandbox", "allow-scripts allow-forms allow-popups allow-same-origin"); box.append(f); };
  if (kindOf(t.target) === "tool") {
    const render = tools[t.target];
    return render ? render(box) : note(`<div class="muted">This tool is not open now.</div>`);
  }
  if (isFile(t.target)) {
    const src = await fileUrl(t.target);
    if (IMAGE.test(t.target)) {
      const img = document.createElement("img"); img.src = src; img.alt = t.target; img.className = "sp-img";
      img.onerror = () => { box.replaceChildren(); note(`MaximizePM cannot show <code>${esc(t.target)}</code>: it is outside the project folders, or gone.`); };
      box.append(img);
    } else if (MD.test(t.target)) {
      const r = await fetch(src);
      if (!r.ok) return note(esc((await r.json().catch(() => ({}))).error || "MaximizePM cannot show this file"));
      const d = document.createElement("div"); d.className = "sp-md md";
      const text = await r.text();
      d.innerHTML = /\.md$/i.test(t.target) ? markdown(text) : `<pre>${esc(text)}</pre>`;  // markdown(): DOMPurify
      box.append(d);
    } else {
      frame(src);  // a PDF or an HTML file: the server sends it in a sandbox of its own
    }
    return;
  }
  if (LOCAL.test(t.target)) return frame(t.target);
  note(`<div class="muted">Asking whether ${esc(label(t.target))} lets this page show it…</div>`);
  const r = await fetch(`api/frame-check?url=${encodeURIComponent(t.target)}&token=${encodeURIComponent(await pageToken())}`);
  const j = r.ok ? await r.json() : { frame: false };
  if (on !== t.id) return;
  if (j.title) { t.title = j.title; renderTabs(); }
  box.replaceChildren();
  if (j.frame) return frame(t.target);
  note(`<div class="sp-card"><b>${esc(j.title || label(t.target))}</b><div class="muted">This site does not let another page show it in a frame.</div>
    <a class="btn primary" href="${esc(t.target)}" target="_blank" rel="noopener noreferrer">Open in a new tab</a></div>`);
}

// Full screen (#4620): the target alone in a browser tab of its own, at an address with a key that maxpm serve
// signs for it. The tab opens at once (a later window.open is blocked), then goes to that address.
export async function openFullScreen(target, session = "", item = "") {
  const w = window.open("", "_blank");
  try {
    const q = new URLSearchParams({ target, session, item: item || "", token: await pageToken() });
    const r = await fetch("api/view-key?" + q);
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "failed");
    if (w) { w.opener = null; w.location = j.url; } else window.open(j.url, "_blank", "noopener");
  } catch (e) { if (w) w.close(); toast(e.message, true); }
}

// No tab open (#4646): the artifacts of this session (GET /api/artifacts?agent=), as in its Artifacts view (#4630).
// Open gives one a tab; Open full screen a browser tab of its own.
async function showList() {
  on = null; listed = true;
  renderTabs();
  $("#spAddr").value = "";
  const box = $("#spBody"), name = session;
  box.innerHTML = `<div class="sp-note sp-list"><b>Artifacts of ${esc(name || "this session")}</b>
    <div class="muted">Open one in a tab, or type an address above.</div><div class="sp-arts"><div class="muted sl-none">Reading the transcript…</div></div></div>`;
  const list = box.querySelector(".sp-arts");
  wireRows(list, (_s, target) => openInPanel(target));
  let rows = [];
  try {
    const r = await fetch("api/artifacts" + (name ? "?agent=" + encodeURIComponent(name) : ""));
    rows = r.ok ? ((await r.json()).artifacts || []) : [];
  } catch (e) { /* an empty list says so */ }
  if (!listed || name !== session || !box.contains(list)) return;  // a tab or another session came first
  showRows(list, rows, "This session wrote or named nothing the side panel can show.");
}

function reveal(yes) {
  panel().classList.toggle("hidden", !yes);
  $("#sessLayout").classList.toggle("with-panel", yes);
  if (!yes) $("#spBody").replaceChildren();
}

export function openInPanel(target) {
  target = String(target || "").trim();
  if (!target) return;
  if (!isFile(target) && !/^https?:\/\//i.test(target)) return toast("The side panel opens a web address (http, https) or a full file path", true);
  reveal(true);
  let t = tabs.find(x => x.target === target);  // the same thing again: its tab
  if (!t) { t = { id: ++n, target, title: "" }; tabs.push(t); }
  show(t);
}

// A tool in a tab (#4631): render(box) fills the tab's body each time the tab shows.
export function openTool(key, title, render) {
  const target = "tool:" + key;
  tools[target] = render;
  reveal(true);
  let t = tabs.find(x => x.target === target);
  if (!t) { t = { id: ++n, target, title }; tabs.push(t); }
  show(t);
}

// The chosen session changed: keep the tabs of the one before, and show its own (from this browser, after a reload).
export function panelFor(name) {
  if (name === session) return;
  if (session) bySession[session] = { tabs, on };
  session = name || "";
  let mine = bySession[session];
  if (!mine) {
    let all = {};
    try { all = JSON.parse(store(TABS) || "{}") || {}; } catch (e) { all = {}; }
    const s = all[session] || { targets: [] };
    const list = (s.targets || []).filter(x => typeof x === "string").map(target => ({ id: ++n, target, title: "" }));
    mine = { tabs: list, on: (list.find(t => t.target === s.on) || list[list.length - 1] || {}).id ?? null };
  }
  tabs = mine.tabs; on = mine.on; listed = !!mine.listed;
  if (!tabs.length) {
    if (listed) { reveal(true); return showList(); }
    $("#spTabs").innerHTML = ""; return reveal(false);
  }
  reveal(true);
  show(tabs.find(t => t.id === on) || tabs[tabs.length - 1]);
}

// The X of a tab. After the last one the panel stays, with the session's artifacts (#4646).
function close(id) {
  tabs = tabs.filter(t => t.id !== id);
  if (!tabs.length) return showList();
  if (on === id) show(tabs[tabs.length - 1]); else renderTabs();
}

// The panel's own X: no tabs, and no panel.
function closePanel() {
  tabs = []; on = null; listed = false;
  renderTabs();
  reveal(false);
}

export function wirePanel() {
  const w = +store(WIDTH);
  if (w >= 280) document.documentElement.style.setProperty("--sp-w", w + "px");
  $("#spTabs").addEventListener("click", (e) => {
    const x = e.target.closest("[data-close]"); if (x) return close(+x.dataset.close);
    const t = e.target.closest("[data-tab]"); if (t) show(tabs.find(z => z.id === +t.dataset.tab));
  });
  $("#spForm").addEventListener("submit", (e) => { e.preventDefault(); openInPanel($("#spAddr").value); });
  $("#spReload").onclick = () => { const t = tabs.find(z => z.id === on); if (t) show(t); else if (listed) showList(); };
  $("#spNew").onclick = async () => {
    const t = tabs.find(z => z.id === on); if (!t) return toast("Open an artifact in a tab first", true);
    window.open(isFile(t.target) ? await fileUrl(t.target) : t.target, "_blank", "noopener");
  };
  $("#spClose").onclick = closePanel;
  $("#spFull").onclick = () => { const t = tabs.find(z => z.id === on);
    if (t && kindOf(t.target) === "tool") return toast("A tool shows in the side panel only", true);
    if (t) openFullScreen(t.target, session); else toast("Open an artifact in a tab first", true); };
  // Drag the left edge to resize; the width stays for the next visit.
  const grip = $("#spGrip");
  grip.addEventListener("pointerdown", (e) => {
    e.preventDefault(); grip.setPointerCapture(e.pointerId);
    const right = panel().getBoundingClientRect().right;
    const move = (ev) => document.documentElement.style.setProperty("--sp-w", Math.max(280, Math.min(window.innerWidth * 0.6, right - ev.clientX)) + "px");
    const up = () => { grip.removeEventListener("pointermove", move); grip.removeEventListener("pointerup", up);
      store(WIDTH, String(Math.round(panel().getBoundingClientRect().width))); };
    grip.addEventListener("pointermove", move); grip.addEventListener("pointerup", up);
  });
}
