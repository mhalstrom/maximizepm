// The side panel of the Sessions tab (#4619): what a session points to, beside its chat. A local page (localhost,
// 127.0.0.1) in a frame; a local file (image, PDF, HTML, Markdown, text) that maxpm serve gives read only
// (GET /api/file, with the page's token, only under the project folders, their worktrees, and uploads); an outside
// page in a frame when the site allows it (GET /api/frame-check), else its title and Open in a new tab. Tabs for
// several, an address bar, Reload, Open in a new tab, Close, and a handle to resize it (the width stays).
// Each session has its own tabs (#4632), kept in this browser across reloads; each tab shows what it holds (file,
// image, page, site, tool). A tool (#4631) opens as a tab too: openTool(key, title, render).
// Two lists come first and stay (#4685): Items (what this session claimed, finished, released or gave away) and
// Artifacts (what it wrote or named). The chat stays in sight beside them: an item opens in a tab of its own here
// (components/itemView.js), an artifact too. The X of the last tab shows the Artifacts list (#4646); the panel's
// own X closes it.
import { $, esc, ago, store, toast, pageToken, copyText } from "../lib.js";
import { markdown } from "./chat.js";
import { chip } from "./chip.js";
import { itemViewHtml } from "./itemView.js";
import { linkArtifacts, linkTarget } from "./artLinks.js";
import { showRows, wireRows } from "../views/artifacts.js";

const WIDTH = "river.sidepanel.width", TABS = "river.sidepanel.tabs";
const LOCAL = /^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(:\d+)?(\/|$)/i;
// The page itself runs away from the computer of maxpm serve: through the relay, or from another computer (#4711).
export function awayFromHome(origin = location.origin) { return !LOCAL.test(origin + "/"); }
const IMAGE = /\.(png|jpe?g|gif|webp|svg)$/i, MD = /\.(md|txt)$/i;

// on: the id of the tab that shows, or "items" / "artifacts" for a list (#4685).
let tabs = [], on = null, n = 0, session = "";
const bySession = {};  // name -> {tabs, on}
const LISTS = { items: "Items", artifacts: "Artifacts" };
const isList = (x) => x === "items" || x === "artifacts";
const tools = {};      // key -> render(box): the tool tabs of #4631
const panel = () => $("#sessPanel");
// What the panel shows changed (#4653): fn(session, target or null). The page puts it in its address.
let told = () => {};
export function onPanel(fn) { told = fn; }
const tell = () => told(session, (tabs.find(t => t.id === on) || {}).target || null);
// The targets of this session's tabs, to find an artifact by its id.
export function panelTargets() { return tabs.map(t => t.target); }

function isFile(target) { return /^(?:\/|~[/\\]|[a-z]:[/\\]|\\\\)/i.test(target); }
const itemOf = (target) => (/^item:(\d+)$/.exec(target) || [])[1];
function label(target) {
  if (itemOf(target)) return "#" + itemOf(target);
  if (isFile(target)) return target.split(/[\\/]/).pop();
  try { const u = new URL(target); return u.host + (u.pathname.length > 1 ? u.pathname : ""); } catch (e) { return target; }
}
async function fileUrl(path) {
  return `api/file?path=${encodeURIComponent(path)}&token=${encodeURIComponent(await pageToken())}`;
}

function kindOf(target) {
  if (target.startsWith("tool:")) return "tool";
  if (itemOf(target)) return "item";
  if (isFile(target)) return IMAGE.test(target) ? "image" : "file";
  return LOCAL.test(target) ? "page" : "site";
}
const KIND_MARK = { image: "▣", file: "▤", page: "◧", site: "◎", tool: "⚙", item: "◇" };

// The tabs of each session stay in this browser (targets only; a tool tab comes back when its tool opens again).
function save() {
  if (!session) return;
  bySession[session] = { tabs, on };
  let all = {};
  try { all = JSON.parse(store(TABS) || "{}") || {}; } catch (e) { all = {}; }
  const keep = tabs.filter(t => kindOf(t.target) !== "tool");
  if (keep.length || isList(on)) all[session] = { targets: keep.map(t => t.target), on: isList(on) ? "list:" + on : (tabs.find(t => t.id === on) || {}).target || "" };
  else delete all[session];
  store(TABS, JSON.stringify(all));
}

function renderTabs() {
  // The two lists first, without an X (#4685); then a tab for each thing opened.
  $("#spTabs").innerHTML = Object.entries(LISTS).map(([k, title]) => `<span class="sp-tab sp-tab-list${on === k ? " on" : ""}">`
    + `<button type="button" class="sp-tab-l" role="tab" aria-selected="${on === k}" data-sp-list="${k}">${title}</button></span>`).join("")
    + tabs.map(t => { const k = kindOf(t.target);
    return `<span class="sp-tab${t.id === on ? " on" : ""}" title="${esc(itemOf(t.target) ? t.title || label(t.target) : t.target)}">`
    + `<button type="button" class="sp-tab-l" role="tab" aria-selected="${t.id === on}" data-sp-tab="${t.id}"><span class="sp-k sp-k-${k}" aria-label="${k}">${KIND_MARK[k]}</span> ${esc(t.title || label(t.target))}</button>`
    + `<button type="button" class="sp-x" data-close="${t.id}" title="Close this tab" aria-label="Close ${esc(t.title || label(t.target))}">✕</button></span>`; }).join("");
  save();
}

async function show(t) {
  on = t.id;
  renderTabs();
  tell();
  $("#spAddr").value = itemOf(t.target) ? "#" + itemOf(t.target) : t.target;
  const box = $("#spBody");
  box.replaceChildren();
  const note = (html) => { const d = document.createElement("div"); d.className = "sp-note"; d.innerHTML = html; box.append(d); };
  const frame = (src) => { const f = document.createElement("iframe"); f.src = src; f.className = "sp-frame";
    f.setAttribute("sandbox", "allow-scripts allow-forms allow-popups allow-same-origin"); box.append(f); };
  if (kindOf(t.target) === "tool") {
    const render = tools[t.target];
    return render ? render(box) : note(`<div class="muted">This tool is not open now.</div>`);
  }
  if (itemOf(t.target)) {
    note(`<div class="muted">Reading #${itemOf(t.target)}…</div>`);
    const r = await fetch("api/item/" + itemOf(t.target)).catch(() => null);
    if (on !== t.id) return;
    box.replaceChildren();
    if (!r || !r.ok) return note(`<div class="muted">There is no item #${itemOf(t.target)}.</div>`);
    const it = await r.json();
    if (t.title !== `#${it.id} ${it.title}`) { t.title = `#${it.id} ${it.title}`; renderTabs(); }
    box.innerHTML = itemViewHtml(it);
    linkArtifacts(box.querySelector(".sp-item"), session);  // its files and pages open in a tab here (#4689)
    return;
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
  if (LOCAL.test(t.target)) {
    if (!awayFromHome()) return frame(t.target);
    // Seen from another computer (the relay website, #4711), the address would open that computer, not the one
    // that runs the page: say so, with the address to copy, in place of an empty or wrong frame.
    note(`<div class="sp-card"><b>Open this on your Mac</b><div class="muted"><code>${esc(t.target)}</code> is a page on the
      computer that runs MaximizePM, so this browser cannot show it. Open it in a browser there.</div>
      <button class="btn primary" type="button" data-sp-copy>Copy the address</button></div>`);
    box.querySelector("[data-sp-copy]").addEventListener("click", async (e) => {
      if (await copyText(t.target)) { e.target.textContent = "Copied"; setTimeout(() => { e.target.textContent = "Copy the address"; }, 1500); }
      else toast("Could not copy", true);
    });
    return;
  }
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

// The two lists of this session (#4685): Items (GET /api/session-items?agent=) and Artifacts (GET /api/artifacts?agent=,
// the rows of the Artifacts tab). A click on an item or an artifact opens it in a tab here. After the last tab
// closes, the Artifacts list shows (#4646). quiet: read again in place, with no "Reading" line (every 10 seconds).
async function showList(kind = "artifacts", quiet = false) {
  if (!quiet || on !== kind) { on = kind; renderTabs(); tell(); $("#spAddr").value = ""; }
  const box = $("#spBody"), name = session;
  let list = box.querySelector(`.sp-list[data-list="${kind}"] .sp-rows`);
  if (!quiet || !list) {
    box.innerHTML = `<div class="sp-note sp-list" data-list="${kind}"><b>${LISTS[kind]} of ${esc(name || "this session")}</b>
      <div class="muted">${kind === "items" ? "The items this session claimed, finished, released or gave away. Open one in a tab." : "The files, local pages and claude.ai artifacts this session wrote or named. Open one in a tab, or type an address above."}</div>
      <div class="sp-rows${kind === "artifacts" ? " sp-arts" : ""}"><div class="muted sl-none">${kind === "items" ? "Reading the history…" : "Reading the transcript…"}</div></div></div>`;
    list = box.querySelector(".sp-rows");
    if (kind === "artifacts") wireRows(list, (_s, target) => openInPanel(target));
  }
  let rows = [];
  try {
    const r = await fetch((kind === "items" ? "api/session-items" : "api/artifacts") + (name ? "?agent=" + encodeURIComponent(name) : ""));
    rows = r.ok ? ((await r.json())[kind] || []) : [];
  } catch (e) { /* an empty list says so */ }
  if (on !== kind || name !== session || !box.contains(list)) return;  // a tab or another session came first
  if (kind === "artifacts") return showRows(list, rows, "This session wrote or named nothing the side panel can show.");
  list.innerHTML = rows.map(x => `<div class="si-row">
      <span class="link" data-open="${x.id}">#${x.id}</span>
      <span class="si-t link" data-open="${x.id}">${esc(x.title)}</span>
      <span class="si-m">${chip("c-" + esc(x.status), esc(x.status.replace("_", " ")))} <span class="muted">${esc((x.did || []).join(", "))} · ${ago(x.at)}</span></span>
    </div>`).join("") || `<div class="muted sl-none">This session took no item yet.</div>`;
}
// A list of the session, from the session's header (Items, Artifacts): the panel opens on it.
export function openList(kind) {
  if (!isList(kind)) return;
  reveal(true);
  showList(kind);
}

function reveal(yes) {
  panel().classList.toggle("hidden", !yes);
  $("#sessLayout").classList.toggle("with-panel", yes);
  if (!yes) $("#spBody").replaceChildren();
}

export function openInPanel(target) {
  target = String(target || "").trim().replace(/^#(\d+)$/, "item:$1");  // #12 in the address bar: the item
  if (!target) return;
  if (!isFile(target) && !itemOf(target) && !/^https?:\/\//i.test(target)) return toast("The side panel opens a web address (http, https), a full file path, or an item (#12)", true);
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
    const lst = /^list:(items|artifacts)$/.exec(s.on || "");
    mine = { tabs: list, on: lst ? lst[1] : (list.find(t => t.target === s.on) || list[list.length - 1] || {}).id ?? null };
  }
  tabs = mine.tabs; on = mine.on;
  if (isList(on)) { reveal(true); return showList(on); }
  if (!tabs.length) { on = null; $("#spTabs").innerHTML = ""; reveal(false); return tell(); }
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
  tabs = []; on = null;
  renderTabs();
  reveal(false);
  tell();
}

export function wirePanel() {
  const w = +store(WIDTH);
  if (w >= 280) document.documentElement.style.setProperty("--sp-w", w + "px");
  $("#spTabs").addEventListener("click", (e) => {
    const x = e.target.closest("[data-close]"); if (x) return close(+x.dataset.close);
    const l = e.target.closest("[data-sp-list]"); if (l) return showList(l.dataset.spList);
    // data-sp-tab, not data-tab: the page's own click handler reads data-tab as a tab of the page (#4652).
    const t = e.target.closest("[data-sp-tab]"); if (t) show(tabs.find(z => z.id === +t.dataset.spTab));
  });
  $("#spForm").addEventListener("submit", (e) => { e.preventDefault(); openInPanel($("#spAddr").value); });
  $("#spReload").onclick = () => { const t = tabs.find(z => z.id === on); if (t) show(t); else if (isList(on)) showList(on); };
  $("#spNew").onclick = async () => {
    const t = tabs.find(z => z.id === on); if (!t) return toast("Open an artifact in a tab first", true);
    if (itemOf(t.target)) return window.open("?item=" + itemOf(t.target), "_blank", "noopener");  // the page, with its drawer
    window.open(isFile(t.target) ? await fileUrl(t.target) : t.target, "_blank", "noopener");
  };
  // In the panel, an item link opens the item in a tab here, not in the drawer; Edit in the drawer opens the drawer.
  $("#spBody").addEventListener("click", (e) => {
    const d = e.target.closest("[data-drawer-open]");
    if (d) { e.stopPropagation(); return window.riverOpen && window.riverOpen(+d.dataset.drawerOpen); }
    const t = linkTarget(e.target);
    if (t && !e.metaKey && !e.ctrlKey) { e.preventDefault(); openInPanel(t); }
  });
  // The lists follow what the session does while they show.
  setInterval(() => { if (isList(on) && !document.hidden && !panel().classList.contains("hidden") && panel().offsetParent) showList(on, true); }, 10000);
  $("#spClose").onclick = closePanel;
  $("#spFull").onclick = () => { const t = tabs.find(z => z.id === on);
    if (t && kindOf(t.target) === "tool") return toast("A tool shows in the side panel only", true);
    if (t && itemOf(t.target)) return window.open("?item=" + itemOf(t.target), "_blank", "noopener");
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
