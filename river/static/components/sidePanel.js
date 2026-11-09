// The side panel of the Sessions tab (#4619): what a session points to, beside its chat. A local page (localhost,
// 127.0.0.1) in a frame; a local file (image, PDF, HTML, Markdown, text) that maxpm serve gives read only
// (GET /api/file, with the page's token, only under the project folders, their worktrees, and uploads); an outside
// page in a frame when the site allows it (GET /api/frame-check), else its title and Open in a new tab. Tabs for
// several, an address bar, Reload, Open in a new tab, Close, and a handle to resize it (the width stays).
// Each session has its own tabs (#4632), kept in this browser across reloads; each tab shows what it holds (file,
// image, page, site, tool). A tool (#4631) opens as a tab too: openTool(key, title, render).
// A canvas (#4768, a page that a session keeps in maxpm: maxpm canvas put) opens as a tab "canvas:<id>": its newest
// version in a frame of its own origin (or drawn, for Markdown), a version menu, and a reload in place, at the same
// scroll position, when the session puts a new version. A new canvas of the shown session opens here by itself.
// Under the page, its comments (#4769, components/canvasComments.js): Comment picks a part of the page, Comment on
// the page takes all of it; a Markdown canvas also has Edit, which saves the person's change as a new version.
// Two lists come first and stay (#4685): Items (what this session claimed, finished, released or gave away) and
// Artifacts (what it wrote or named). The chat stays in sight beside them: an item opens in a tab of its own here
// (components/itemView.js), an artifact too. The X of the last tab shows the Artifacts list (#4646); the panel's
// own X closes it.
import { $, esc, ago, clip, store, toast, act, pageToken, copyText } from "../lib.js";
import { markdown } from "./chat.js";
import { chip } from "./chip.js";
import { itemViewHtml } from "./itemView.js";
import { linkArtifacts, linkTarget } from "./artLinks.js";
import { showRows, wireRows } from "../views/artifacts.js";
import { makeComments, pathIn } from "./canvasComments.js";

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
const canvasOf = (target) => +((/^canvas:c?(\d+)$/.exec(target) || [])[1] || 0) || null;
// The canvases of the queue (S.canvases: id, name, title, version, agent), from the page's state (canvasState).
let canvasHeads = new Map(), knownCanvases = null;
function label(target) {
  if (itemOf(target)) return "#" + itemOf(target);
  if (canvasOf(target)) { const h = canvasHeads.get(canvasOf(target)); return h ? h.title || h.name : target; }
  if (isFile(target)) return target.split(/[\\/]/).pop();
  try { const u = new URL(target); return u.host + (u.pathname.length > 1 ? u.pathname : ""); } catch (e) { return target; }
}
async function fileUrl(path) {
  return `api/file?path=${encodeURIComponent(path)}&token=${encodeURIComponent(await pageToken())}`;
}

function kindOf(target) {
  if (target.startsWith("tool:")) return "tool";
  if (canvasOf(target)) return "canvas";
  if (itemOf(target)) return "item";
  if (isFile(target)) return IMAGE.test(target) ? "image" : "file";
  return LOCAL.test(target) ? "page" : "site";
}
const KIND_MARK = { image: "▣", file: "▤", page: "◧", site: "◎", tool: "⚙", item: "◇", canvas: "✎" };

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
  if (canvasOf(t.target)) return showCanvas(t, box, note);
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

// A canvas's frame loads with a full-screen key for "canvas:<id>", never with the page's token: the canvas can read
// its own address (#4768). The key lasts hours; the tab keeps it and asks again before it ends.
async function canvasSig(t) {
  if (t.sig && t.sigEnd > Date.now() / 1000 + 600) return t.sig;
  const q = new URLSearchParams({ target: t.target, session, token: await pageToken() });
  const r = await fetch("api/view-key?" + q), j = await r.json();
  if (!r.ok) throw new Error(j.error || "MaximizePM cannot show this canvas");
  const u = new URLSearchParams(j.url.split("?")[1]);
  t.sig = `e=${u.get("e")}&k=${encodeURIComponent(u.get("k"))}`; t.sigEnd = +u.get("e");
  return t.sig;
}

// A canvas tab: a bar with the version menu, then the version. t.pin: a version the person chose (null follows the
// newest); t.shown: the newest version the tab knows; t.scroll: where the person reads.
async function showCanvas(t, box, note) {
  const id = canvasOf(t.target);
  let sig, meta;
  try {
    sig = await canvasSig(t);
    const r = await fetch(`canvas/${id}/meta?${sig}`); meta = await r.json();
    if (!r.ok) throw new Error(meta.error || "gone");
  } catch (e) {
    if (on === t.id) { box.replaceChildren(); note(`<div class="muted">MaximizePM cannot show this canvas: ${esc(e.message)}</div>`); }
    return;
  }
  if (on !== t.id) return;
  const title = meta.title || meta.name;
  if (t.title !== title) { t.title = title; renderTabs(); }
  if (t.pin && !meta.versions.some(x => x.version === t.pin)) t.pin = null;
  t.shown = meta.version;
  const v = t.pin || meta.version;
  box.replaceChildren();
  const bar = document.createElement("div"); bar.className = "sp-cbar";
  bar.innerHTML = `<select class="sp-cver" aria-label="Version of the canvas">${[...meta.versions].reverse().map(x =>
      `<option value="${x.version}"${x.version === v ? " selected" : ""}>v${x.version}${x.version === meta.version ? " (newest)" : ""} · ${esc(x.author || "")} · ${ago(x.created_at)}${x.note ? " · " + esc(clip(x.note, 40)) : ""}</option>`).join("")}</select>
    <span class="muted">${t.pin ? `version ${t.pin} of ${meta.version} · <span class="link" data-cv-newest>show the newest</span>` : "shows each new version"}</span>
    ${meta.item_id ? `<span class="link" data-open="${meta.item_id}" title="${esc(meta.item_title || "")}">#${meta.item_id}</span>` : ""}
    <span class="cv-sp"></span>${meta.kind === "svg" ? "" : `<button class="btn" type="button" data-cv-mode aria-pressed="${!!t.commenting}" title="Then click a part of the page, or select text, to comment on it">Comment</button>`}
    <button class="btn" type="button" data-cv-page>Comment on the page</button>
    ${meta.kind === "md" && !t.pin ? `<button class="btn" type="button" data-cv-edit title="Change the text; it becomes a new version, and the session gets a note">Edit</button>` : ""}`;
  box.append(bar);
  bar.querySelector("select").onchange = (e) => { const n = +e.target.value; t.pin = n === meta.version ? null : n; t.scroll = null; show(t); };
  const newest = bar.querySelector("[data-cv-newest]");
  if (newest) newest.onclick = () => { t.pin = null; show(t); };
  const src = `canvas/${id}/${t.pin || "latest"}?${sig}`;
  const side = document.createElement("div"); side.className = "sp-cside";
  t.comments = makeComments(side, { id, ref: "c" + id, reload: () => loadComments(t), show: (path) => showPart(t, path) });
  t.comments.render(meta);
  const mode = bar.querySelector("[data-cv-mode]");
  if (mode) mode.onclick = () => setCommenting(t, !t.commenting);
  bar.querySelector("[data-cv-page]").onclick = () => { setCommenting(t, false); t.comments.compose(null); side.scrollIntoView({ block: "nearest" }); };
  const edit = bar.querySelector("[data-cv-edit]");
  if (edit) edit.onclick = () => editCanvas(t, box, src, side);
  t.frame = null; t.md = null;
  if (meta.kind === "md") {
    const r = await fetch(src);
    if (on !== t.id) return;
    const d = document.createElement("div"); d.className = "sp-md md sp-canvas";
    d.innerHTML = r.ok ? markdown(await r.text()) : "MaximizePM cannot show this canvas.";  // markdown(): DOMPurify
    t.md = d;
    // Comment mode on a drawn Markdown canvas: this page finds the part itself (the helper script does it in a frame).
    d.addEventListener("mouseover", (e) => { if (t.commenting) markPart(d, e.target === d ? null : e.target); });
    d.addEventListener("click", (e) => {
      if (!t.commenting) return;
      e.preventDefault(); e.stopPropagation();
      const el = e.target === d ? null : e.target;
      markPart(d, null); setCommenting(t, false);
      t.comments.pick(el ? { path: pathIn(el, d), text: (el.innerText || "").replace(/\s+/g, " ").trim().slice(0, 200),
                              quote: String(getSelection() || "").trim().slice(0, 500) } : null);
      side.scrollIntoView({ block: "nearest" });
    }, true);
    box.append(d, side);
    box.scrollTop = (t.scroll && t.scroll.y) || 0;
    box.onscroll = () => { if (on === t.id) t.scroll = { x: 0, y: box.scrollTop }; };
    return;
  }
  box.onscroll = null;
  const f = document.createElement("iframe");
  f.className = "sp-frame sp-canvas"; f.title = title;
  f.setAttribute("sandbox", "allow-scripts");  // and the server's CSP sandbox: an origin of its own
  f.src = src;
  t.frame = f;
  box.append(f, side);
}

// Comment mode: the button shows it; the frame's helper script (or this page, for Markdown) picks the part.
function setCommenting(t, yes) {
  t.commenting = !!yes;
  const b = $("#spBody [data-cv-mode]"); if (b && on === t.id) b.setAttribute("aria-pressed", String(t.commenting));
  $("#spBody").classList.toggle("cv-picking", t.commenting && on === t.id);
  if (t.frame && t.frame.contentWindow) t.frame.contentWindow.postMessage({ maxpmCanvas: "mode", on: t.commenting }, "*");
  if (!t.commenting && t.md) markPart(t.md, null);
}
function markPart(root, el) {
  root.querySelectorAll(".maxpm-hi").forEach(x => x.classList.remove("maxpm-hi"));
  if (el) el.classList.add("maxpm-hi");
}
// Show: scroll the page to the part of a comment, and mark it for a moment.
function showPart(t, path) {
  if (t.frame && t.frame.contentWindow) return t.frame.contentWindow.postMessage({ maxpmCanvas: "show", path }, "*");
  let el = null;
  try { el = t.md && path ? t.md.querySelector(path) : null; } catch (e) { el = null; }
  if (!el) return toast("That part is not in this version of the page", true);
  el.scrollIntoView({ block: "center", behavior: "smooth" });
  el.classList.add("maxpm-flash"); setTimeout(() => el.classList.remove("maxpm-flash"), 1600);
}
// The comments again (after an action, and with each state of the page), without a reload of the page.
async function loadComments(t) {
  if (!t.comments || !t.sig) return;
  try {
    const r = await fetch(`canvas/${canvasOf(t.target)}/meta?${t.sig}`);
    if (r.ok && on === t.id) t.comments.render(await r.json());
  } catch (e) { /* the next state asks again */ }
}
// Edit a Markdown canvas: its text in a box; Save makes a new version by the person, and the session gets a note.
async function editCanvas(t, box, src, side) {
  const r = await fetch(src); if (!r.ok || on !== t.id) return;
  const text = await r.text();
  const ed = document.createElement("div"); ed.className = "cv-edit";
  ed.innerHTML = `<textarea aria-label="The text of the canvas (Markdown)"></textarea>
    <div class="cv-acts"><button class="btn primary" type="button" data-cv-save>Save as a new version</button>
      <button class="btn" type="button" data-cv-stop>Cancel</button><span class="muted">The session that wrote it gets a note.</span></div>`;
  ed.querySelector("textarea").value = text;
  (t.md || box.querySelector(".sp-canvas")).replaceWith(ed);
  ed.querySelector("textarea").focus();
  ed.querySelector("[data-cv-stop]").onclick = () => show(t);
  ed.querySelector("[data-cv-save]").onclick = async () => {
    try {
      const res = await act("canvas_edit", { name: "c" + canvasOf(t.target), text: ed.querySelector("textarea").value });
      toast(res.unchanged ? "No change" : `Saved as version ${res.version}`);
      t.pin = null; show(t);
    } catch (e) { /* toast shown */ }
  };
}

// The helper script in an HTML canvas says where the person scrolls, and asks for it after a new version loads.
addEventListener("message", (e) => {
  const d = e.data || {}, t = tabs.find(x => x.id === on);
  if (!t || !t.frame || e.source !== t.frame.contentWindow || typeof d.maxpmCanvas !== "string") return;
  if (d.maxpmCanvas === "scroll") t.scroll = { x: +d.x || 0, y: +d.y || 0 };
  if (d.maxpmCanvas === "ready" && t.scroll) e.source.postMessage({ maxpmCanvas: "scrollTo", x: t.scroll.x, y: t.scroll.y }, "*");
  if (d.maxpmCanvas === "ready" && t.commenting) e.source.postMessage({ maxpmCanvas: "mode", on: true }, "*");
  if (d.maxpmCanvas === "escape") setCommenting(t, false);
  if (d.maxpmCanvas === "shown" && !d.found) toast("That part is not in this version of the page", true);
  if (d.maxpmCanvas === "pick" && t.comments) {
    // What the frame says is data from a page a session wrote: strings only, cut short; the box shows it escaped.
    const str = (v, n) => String(v || "").slice(0, n);
    setCommenting(t, false);
    t.comments.pick({ path: str(d.path, 300), text: str(d.text, 200), quote: str(d.quote, 500) });
    const side = $("#spBody .sp-cside"); if (side) side.scrollIntoView({ block: "nearest" });
  }
});

// The page's state came (app.js, every three seconds): the canvas tab on screen loads a new version in place, and a
// canvas that the shown session makes opens in a tab. It shows at once only when the panel shows no tab or a list,
// so it never takes a page away from the person while they read it.
export function canvasState(heads) {
  const list = heads || [];
  canvasHeads = new Map(list.map(c => [c.id, c]));
  if (knownCanvases === null) knownCanvases = new Set(list.map(c => c.id));  // the ones from before the page loaded
  for (const c of list) {
    if (knownCanvases.has(c.id)) continue;
    knownCanvases.add(c.id);
    if (!session || c.agent !== session || tabs.some(x => x.target === `canvas:${c.id}`)) continue;
    const t = { id: ++n, target: `canvas:${c.id}`, title: c.title || c.name };
    tabs.push(t);
    if (on == null || isList(on)) { reveal(true); show(t); } else renderTabs();
  }
  const t = tabs.find(x => x.id === on);
  if (!t || !canvasOf(t.target) || t.pin || panel().classList.contains("hidden")) return;
  const h = canvasHeads.get(canvasOf(t.target));
  if (h && t.shown && h.version !== t.shown) return show(t);
  loadComments(t);
}

// maxpm canvas open (#4783): a session asks the page to put a canvas in front of the person. The requests come in
// the state (seq, canvas, title, session); the page counts only the ones that came after it loaded. The canvas
// opens as a tab in the side panel of the session named: at once when the Sessions tab shows that session, else
// the tab waits in that session's panel and a bar offers Show (openSession: the Sessions tab on that session).
let openSeen = null;
export function canvasOpens(list, openSession) {
  list = list || [];
  const top = list.reduce((m, x) => Math.max(m, x.seq), 0);
  if (openSeen === null || top < openSeen) { openSeen = top; return; }  // the first state, or a restarted server
  for (const x of list.filter(x => x.seq > openSeen)) {
    const target = `canvas:${x.canvas}`, name = x.session || session;
    const visible = !$("#tab-sessions").classList.contains("hidden") && name === session;
    if (name === session) openInPanel(target);
    else addTab(name, target);
    if (!visible && name) offerCanvas(x, name, openSession);
  }
  openSeen = top;
}

// A tab in the panel of a session that the panel does not show now: it shows when the person picks that session.
function addTab(name, target) {
  const mine = bySession[name];
  if (mine) {
    let t = mine.tabs.find(x => x.target === target);
    if (!t) { t = { id: ++n, target, title: "" }; mine.tabs.push(t); }
    mine.on = t.id;
    return;
  }
  let all = {};
  try { all = JSON.parse(store(TABS) || "{}") || {}; } catch (e) { all = {}; }
  const s = all[name] || { targets: [] };
  s.targets = (s.targets || []).filter(x => x !== target).concat(target);
  s.on = target;
  all[name] = s;
  store(TABS, JSON.stringify(all));
}

function offerCanvas(x, name, openSession) {
  let bar = $("#cvOffer");
  if (!bar) {
    bar = document.createElement("div");
    bar.id = "cvOffer"; bar.className = "cv-offer"; bar.setAttribute("role", "status");
    bar.innerHTML = `<span class="cv-offer-t"></span><button type="button" class="cv-offer-show">Show</button>`
      + `<button type="button" class="plain cv-offer-x" aria-label="Dismiss">×</button>`;
    document.body.appendChild(bar);
    bar.querySelector(".cv-offer-x").onclick = () => bar.classList.add("hidden");
  }
  bar.querySelector(".cv-offer-t").textContent = `${x.by || name} opened the canvas "${x.title}" in the side panel of ${name}.`;
  bar.querySelector(".cv-offer-show").onclick = () => { bar.classList.add("hidden"); openSession(name); };
  bar.classList.remove("hidden");
  clearTimeout(bar._h); bar._h = setTimeout(() => bar.classList.add("hidden"), 30000);
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
  if (!isFile(target) && !itemOf(target) && !canvasOf(target) && !/^https?:\/\//i.test(target)) return toast("The side panel opens a web address (http, https), a full file path, an item (#12), or a canvas (canvas:3)", true);
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
    if (canvasOf(t.target)) return openFullScreen(t.target, session);  // its own page, with a key (#4768)
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
