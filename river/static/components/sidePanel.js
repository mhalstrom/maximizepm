// The side panel of the Sessions tab (#4619): what a session points to, beside its chat. A local page (localhost,
// 127.0.0.1) in a frame; a local file (image, PDF, HTML, Markdown, text) that maxpm serve gives read only
// (GET /api/file, with the page's token, only under the project folders, their worktrees, and uploads); an outside
// page in a frame when the site allows it (GET /api/frame-check), else its title and Open in a new tab. Tabs for
// several, an address bar, Reload, Open in a new tab, Close, and a handle to resize it (the width stays).
// Each session has its own tabs (#4632), kept in this browser across reloads; each tab shows what it holds (file,
// image, page, site, tool). A tool (#4631) opens as a tab too: openTool(key, title, render); the first tool is the
// browser of the session's project (components/browserTool.js).
// A canvas (#4768, a page that a session keeps in maxpm: maxpm canvas put) opens as a tab "canvas:<id>": its newest
// version in a frame of its own origin (or drawn, for Markdown), a version menu, and a reload in place, at the same
// scroll position, when the session puts a new version. A new canvas of the shown session opens here by itself.
// Under the page, its comments (#4769, components/canvasComments.js): Comment picks a part of the page, Comment on
// the page takes all of it; a Markdown canvas also has Edit, which saves the person's change as a new version.
// Two lists come first and stay (#4685): Items (what this session claimed, finished, released or gave away) and
// Artifacts (what it wrote or named). The chat stays in sight beside them: an item opens in a tab of its own here
// (components/itemView.js), an artifact too. The X of the last tab shows the Artifacts list (#4646); the panel's
// own X closes it. Find (#4877): a box over the tab marks each place of the words in the canvas, item, list or file it
// shows (in an HTML canvas through its helper script), with "n of m", Enter and Shift+Enter, and Esc.
import { $, esc, ago, clip, store, toast, act, actor, pageToken, copyText, targetKey } from "../lib.js";
import { canvasMarkdown, drawDiagrams } from "./canvasMd.js";
import { chip } from "./chip.js";
import { itemViewHtml } from "./itemView.js";
import { linkArtifacts, linkTarget } from "./artLinks.js";
import { showRows, wireRows } from "../views/artifacts.js";
import { makeComments, pathIn } from "./canvasComments.js";
import { decorateDoc, updateAnswers } from "./canvasDoc.js";
import { findMark, findClear, findGo } from "./findText.js";
import { queueHtml } from "./agentActions.js";
import { button, iconButton } from "./button.js";
import { S } from "../page.js";

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
// The session whose tabs the panel holds (the browser tool opens for it, #4631).
export function panelSession() { return session; }
// The targets of this session's tabs, to find an artifact by its id.
export function panelTargets() { return tabs.map(t => t.target); }
// The tab of a thing in a list of tabs: one tab for each thing, whatever address named it (#5000, lib.targetKey).
const tabOf = (list, target) => { const k = targetKey(target); return list.find(x => targetKey(x.target) === k); };
// A new tab keeps the address it was opened with, but a claude.ai artifact gets the one form of its address.
const firstTarget = (target) => /^https?:\/\/(?:www\.)?claude\.ai\//i.test(target) ? targetKey(target) : target;

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
  if (keep.length || isList(on)) all[session] = { targets: keep.map(t => t.target), on: isList(on) ? "list:" + on : (tabs.find(t => t.id === on) || {}).target || "",
    by: Object.fromEntries(keep.filter(t => t.by).map(t => [t.target, t.by])) };
  else delete all[session];
  store(TABS, JSON.stringify(all));
}

function renderTabs() {
  // The two lists first, without an X (#4685); then a tab for each thing opened.
  $("#spTabs").innerHTML = Object.entries(LISTS).map(([k, title]) => `<span class="sp-tab sp-tab-list${on === k ? " on" : ""}">`
    + `<button type="button" class="sp-tab-l" role="tab" aria-selected="${on === k}" data-sp-list="${k}">${title}</button></span>`).join("")
    + tabs.map(t => { const k = kindOf(t.target);
    const tip = itemOf(t.target) ? t.title || label(t.target) : t.target;  // who opened it last, on the label (#5000)
    return `<span class="sp-tab${t.id === on ? " on" : ""}" title="${esc(tip)}">`
    + `<button type="button" class="sp-tab-l" role="tab" aria-selected="${t.id === on}" data-sp-tab="${t.id}" title="${esc(tip + (t.by ? `\nopened by ${t.by}` : ""))}"${t.by ? ` data-by="${esc(t.by)}"` : ""}><span class="sp-k sp-k-${k}" aria-label="${k}">${KIND_MARK[k]}</span> ${esc(t.title || label(t.target))}</button>`
    + `<button type="button" class="sp-x" data-close="${t.id}" title="Close this tab" aria-label="Close ${esc(t.title || label(t.target))}">✕</button></span>`; }).join("");
  markPair();
  save();
}
// Items, Artifacts and Browser in the session head (#5115): the one whose list or browser the open panel shows is on
// (Browser: a tab of a project's browser, #5476).
function markPair() {
  const open = !panel().classList.contains("hidden");
  const cur = (tabs.find(t => t.id === on) || {}).target || "";
  for (const b of document.querySelectorAll(".sess-lists [data-sp-open], .sess-lists [data-sp-browser]")) {
    const yes = open && ("spBrowser" in b.dataset ? cur.startsWith("tool:browser:") : on === b.dataset.spOpen);
    b.classList.toggle("on", yes); b.setAttribute("aria-pressed", String(yes));
  }
}

// The bar of a tab's address, Find, Reload, New tab and Full screen: not over a tool (a project's browser has its own
// bar, and tool:browser:<project> is no address to show, #5477).
function frameBar(yes) {
  $("#spForm").classList.toggle("hidden", !yes);
  if (!yes && findOpen()) closeFind();
}

async function show(t) {
  on = t.id;
  renderTabs();
  tell();
  $("#spAddr").value = itemOf(t.target) ? "#" + itemOf(t.target) : t.target;
  frameBar(kindOf(t.target) !== "tool");
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
      d.innerHTML = /\.md$/i.test(t.target) ? canvasMarkdown(text) : `<pre>${esc(text)}</pre>`;  // DOMPurify
      box.append(d); drawDiagrams(d);
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
// newest); t.shown: the newest version the tab knows; t.scroll: where the person reads. A new version, an answer, or a
// comment later changes the drawn tab in place (updateCanvas, #4844): show() draws it whole only when it opens.
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
  t.kind = meta.kind;
  if (t.pin && !meta.versions.some(x => x.version === t.pin)) t.pin = null;
  t.shown = meta.version;
  const src = `canvas/${id}/${t.pin || "latest"}?${sig}`;
  let text = null;
  if (meta.kind === "md") {  // the text first, so the tab never stands empty while it loads
    const r = await fetch(src);
    text = r.ok ? await r.text() : null;
    if (on !== t.id) return;
  }
  box.replaceChildren();
  const bar = document.createElement("div"); bar.className = "sp-cbar";
  t.bar = bar; t.barSig = null;
  box.append(bar);
  drawBar(t, meta);
  const side = document.createElement("div"); side.className = "sp-cside";
  t.side = side;
  t.comments = makeComments(side, { id, ref: "c" + id, reload: () => loadComments(t), show: (path) => showPart(t, path) });
  t.comments.render(meta);
  t.frame = null; t.md = null; t.next = null;
  if (meta.kind === "md") {
    const d = document.createElement("div"); d.className = "sp-md md sp-canvas";
    if (text === null) d.textContent = "MaximizePM cannot show this canvas.";
    else { const doc = drawDoc(t, text, meta); d.append(...doc.root.childNodes); t.groups = doc.groups; t.text = text; }
    t.md = d;
    drawDiagrams(d);  // ```mermaid blocks as pictures (#4983)
    // Comment mode on a drawn Markdown canvas: this page finds the part itself (the helper script does it in a frame).
    d.addEventListener("mouseover", (e) => { if (t.commenting) markPart(d, e.target === d ? null : e.target); });
    d.addEventListener("click", (e) => {
      if (!t.commenting) return;
      e.preventDefault(); e.stopPropagation();
      const el = e.target === d ? null : e.target.closest(".doc-edit, .doc-answer") ? e.target.closest(".doc-decision, h2, h3, h4, h5, h6") : e.target;
      markPart(d, null); setCommenting(t, false);
      // The part's own words, without the doc's controls (an edit link, a decision's answer bar).
      const words = el ? el.cloneNode(true) : null;
      if (words) words.querySelectorAll(".doc-edit, .doc-answer").forEach(x => x.remove());
      t.comments.pick(el ? { path: pathIn(el, d), text: (words.textContent || "").replace(/\s+/g, " ").trim().slice(0, 200),
                              quote: String(getSelection() || "").trim().slice(0, 500) } : null);
      side.scrollIntoView({ block: "nearest" });
    }, true);
    box.append(d, side);
    box.scrollTop = (t.scroll && t.scroll.y) || 0;
    box.onscroll = () => { if (on === t.id) t.scroll = { x: 0, y: box.scrollTop }; };
    return;
  }
  box.onscroll = null;
  t.frame = canvasFrame(meta.title || meta.name, src);
  box.append(t.frame, side);
}

// The bar of a canvas tab: drawn again only when what it shows changed, so an open version menu stays open.
function drawBar(t, meta) {
  const bar = t.bar, v = t.pin || meta.version;
  const title = meta.title || meta.name;
  if (t.title !== title) { t.title = title; renderTabs(); }
  const sigNow = JSON.stringify([meta.versions, v, t.pin, meta.item_id, meta.item_title, meta.kind, Math.floor(Date.now() / 60000)]);
  if (sigNow === t.barSig || bar.contains(document.activeElement)) return;
  t.barSig = sigNow;
  bar.innerHTML = `<select class="sp-cver" aria-label="Version of the canvas">${[...meta.versions].reverse().map(x =>
      `<option value="${x.version}"${x.version === v ? " selected" : ""}>v${x.version}${x.version === meta.version ? " (newest)" : ""} · ${esc(x.author || "")} · ${ago(x.created_at)}${x.note ? " · " + esc(clip(x.note, 40)) : ""}</option>`).join("")}</select>
    <span class="muted cv-st">${t.pin ? `<span class="cv-st-of">version ${t.pin} of ${meta.version} · </span><span class="link" data-cv-newest>show the newest</span>` : "shows each new version"}</span>
    ${meta.item_id ? `<span class="link cv-item" data-open="${meta.item_id}" title="${esc(meta.item_title || "")}">#${meta.item_id}</span>` : ""}
    <span class="cv-sp"></span>${meta.kind === "svg" ? "" : `<button class="btn" type="button" data-cv-mode aria-pressed="${!!t.commenting}" title="Then click a part of the page, or select text, to comment on it">Comment</button>`}
    <span class="cv-menu"><button class="btn" type="button" data-cv-page>Comment on the page</button>
    ${meta.kind === "md" && !t.pin ? `<button class="btn" type="button" data-cv-edit title="Change the text; it becomes a new version, and the session gets a note">Edit</button>` : ""}</span>
    ${iconButton("⋯", "More: comment on the page, edit", { cls: "cv-more", data: { "cv-more": "" } })}`;
  bar.querySelector("select").onchange = (e) => { const n = +e.target.value; t.pin = n === meta.version ? null : n; t.scroll = null; show(t); };
  const newest = bar.querySelector("[data-cv-newest]");
  if (newest) newest.onclick = () => { t.pin = null; show(t); };
  const mode = bar.querySelector("[data-cv-mode]");
  if (mode) mode.onclick = () => setCommenting(t, !t.commenting);
  bar.querySelector("[data-cv-page]").onclick = () => { setCommenting(t, false); t.comments.compose(null); t.side.scrollIntoView({ block: "nearest" }); };
  const edit = bar.querySelector("[data-cv-edit]");
  if (edit) edit.onclick = () => editCanvas(t, $("#spBody"), `canvas/${canvasOf(t.target)}/latest?${t.sig}`, t.side);
  // #5361: on a phone the bar is one row; Comment on the page and Edit sit under its ⋯. A choice closes the menu, and
  // so do Esc and a click outside the bar (barMenu, once for the page: the bar is drawn again often).
  const more = bar.querySelector("[data-cv-more]");
  more.setAttribute("aria-expanded", String(bar.classList.contains("more")));
  more.onclick = () => barMenu(bar, !bar.classList.contains("more"));
  bar.querySelector(".cv-menu").addEventListener("click", (e) => { if (e.target.closest("button")) barMenu(bar, false); });
}

function barMenu(bar, open) {
  bar.classList.toggle("more", open);
  const b = bar.querySelector("[data-cv-more]"); if (b) b.setAttribute("aria-expanded", String(open));
}
document.addEventListener("keydown", (e) => { if (e.key === "Escape") document.querySelectorAll(".sp-cbar.more").forEach(b => barMenu(b, false)); });
document.addEventListener("pointerdown", (e) => {
  for (const b of document.querySelectorAll(".sp-cbar.more")) if (!b.contains(e.target)) barMenu(b, false);
});

function canvasFrame(title, src) {
  const f = document.createElement("iframe");
  f.className = "sp-frame sp-canvas"; f.title = title;
  f.setAttribute("sandbox", "allow-scripts");  // and the server's CSP sandbox: an origin of its own
  f.src = src;
  return f;
}

// The text of a Markdown canvas, drawn and made a doc, in a new element: { root, groups }. groups: one per section
// (a heading and what follows it, up to the next heading; the first holds what comes before any heading), each with
// its key (the drawn HTML before the doc's controls) and its top-level elements after them.
function drawDoc(t, text, meta) {
  const root = document.createElement("div");
  root.innerHTML = canvasMarkdown(text);  // DOMPurify; data: pictures only (#4983)
  const of = new WeakMap(), keys = [];
  for (const el of root.children) {
    if (/^H[1-6]$/.test(el.tagName) || !keys.length) keys.push("");
    keys[keys.length - 1] += el.outerHTML;
    of.set(el, keys.length - 1);
  }
  // A doc (#4708): pending sections, decision cards answered in place, Edit of a section (newest version only).
  decorateDoc(root, text, meta, { ref: "c" + canvasOf(t.target), live: !t.pin, done: () => updateCanvas(t), current: () => t.text });
  const groups = keys.length ? keys.map(key => ({ key, els: [] })) : [{ key: "", els: [] }];
  for (const el of root.children) {  // a decision card holds its first part: the card is in that part's section
    const g = of.has(el) ? of.get(el) : of.get(el.firstElementChild);
    groups[g === undefined ? groups.length - 1 : g].els.push(el);
  }
  return { root, groups };
}

// A new version of a drawn doc (#4844): the sections whose text is the same keep their elements (with the words a
// person types there, an open section edit, and the place on screen); only the others are drawn again.
function patchDoc(t, box, text, meta) {
  const d = t.md, fresh = drawDoc(t, text, meta), old = t.groups || [];
  const used = new Set(), groups = [];
  let from = 0;
  for (const g of fresh.groups) {
    let k = -1;
    for (let x = from; x < old.length; x++) if (!used.has(x) && old[x].key === g.key) { k = x; break; }
    if (k >= 0) { used.add(k); from = k + 1; groups.push(old[k]); } else groups.push(g);
  }
  const want = groups.flatMap(g => g.els), keep = new Set(want);
  // What the person typed in a decision's box, and where they read: the first element on screen that stays.
  const typed = {};
  for (const inp of d.querySelectorAll(".doc-decision [data-doc-other]")) if (inp.value) typed[inp.closest(".doc-decision").dataset.decision] = inp.value;
  const focus = document.activeElement && d.contains(document.activeElement) && document.activeElement.matches("[data-doc-other]")
    ? document.activeElement.closest(".doc-decision").dataset.decision : null;
  const top = box.getBoundingClientRect().top;
  const anchor = [...d.children].find(el => keep.has(el) && el.getBoundingClientRect().bottom > top);
  const at = anchor ? anchor.getBoundingClientRect().top : 0;
  for (const el of [...d.childNodes]) if (!keep.has(el)) el.remove();
  let cur = d.firstElementChild;
  for (const el of want) { if (el === cur) { cur = cur.nextElementSibling; continue; } d.insertBefore(el, cur); }
  if (anchor && anchor.isConnected) box.scrollTop += anchor.getBoundingClientRect().top - at;
  for (const [n, v] of Object.entries(typed)) {
    const inp = d.querySelector(`.doc-decision[data-decision="${n}"] [data-doc-other]`);
    if (inp && !inp.value) inp.value = v;
  }
  if (focus && !d.contains(document.activeElement)) {
    const inp = d.querySelector(`.doc-decision[data-decision="${focus}"] [data-doc-other]`);
    if (inp) { inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); }
  }
  t.groups = groups; t.text = text;
  drawDiagrams(d);
  updateAnswers(d, meta);  // the sections that stayed show the newest answers too
}

// A new version of an HTML canvas (#4844): it loads in a second frame out of sight, scrolls to the person's place,
// and then takes the old frame's place; the old one shows until then. A frame never moves in the page: that loads it
// again.
function swapFrame(t, src) {
  const old = t.frame; if (!old || !old.isConnected) return;
  if (t.next) t.next.remove();
  const r = old.getBoundingClientRect();
  const f = canvasFrame(old.title, src);
  f.style.cssText = `position:fixed;left:0;top:0;width:${Math.max(r.width, 300)}px;height:${Math.max(r.height, 300)}px;visibility:hidden;pointer-events:none`;
  t.next = f;
  let done = false;
  f.swap = () => {
    if (done || t.next !== f) return;
    done = true; t.next = null;
    if (!old.isConnected) { f.remove(); return; }
    f.style.cssText = ""; old.remove(); t.frame = f;
    if (t.commenting && f.contentWindow) f.contentWindow.postMessage({ maxpmCanvas: "mode", on: true }, "*");
  };
  f.onload = () => setTimeout(f.swap, 1500);  // a page with no helper script (an SVG) says no "ready"
  old.after(f);
}

// The tab on screen again, in place: the bar, the comments, the answers, and a new version (#4844).
async function updateCanvas(t) {
  if (t.updating) { t.again = true; return; }
  t.updating = true;
  try {
    const id = canvasOf(t.target), box = $("#spBody");
    if (on !== t.id) return;
    if (!t.bar || !box.contains(t.bar)) return t.shown ? show(t) : undefined;  // it showed an error: try it whole
    const sig = await canvasSig(t);
    const r = await fetch(`canvas/${id}/meta?${sig}`), meta = await r.json();
    if (!r.ok || on !== t.id || !box.contains(t.bar)) return;
    drawBar(t, meta);
    t.comments.render(meta);
    if (t.frame && t.frame.contentWindow) postAnswers(t.frame.contentWindow, meta);
    const changed = !t.pin && meta.version !== t.shown;
    if (t.md && t.md.isConnected) {
      // A person's open Edit of the whole text, or of a section, waits: the new version comes after Save or Cancel.
      if (changed && !t.md.querySelector(".doc-sec-edit")) {
        const rt = await fetch(`canvas/${id}/latest?${sig}`);
        if (!rt.ok || on !== t.id || !t.md.isConnected) return;
        patchDoc(t, box, await rt.text(), meta);
        t.shown = meta.version;
      } else updateAnswers(t.md, meta);
    } else if (changed && t.frame) {
      swapFrame(t, `canvas/${id}/latest?${sig}`);
      t.shown = meta.version;
    }
  } catch (e) { /* the next state asks again */ } finally {
    t.updating = false;
    if (t.again) { t.again = false; updateCanvas(t); }
  }
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
  const dec = /^decision:(\d+)$/.exec(path || "");  // an answer to a decision of a doc (#4708): its card
  if (dec) path = "#decision-" + dec[1];
  if (t.frame && t.frame.contentWindow) return t.frame.contentWindow.postMessage({ maxpmCanvas: "show", path }, "*");
  let el = null;
  try { el = t.md && path ? t.md.querySelector(path) : null; } catch (e) { el = null; }
  if (!el) return toast("That part is not in this version of the page", true);
  el.scrollIntoView({ block: "center", behavior: "smooth" });
  el.classList.add("maxpm-flash"); setTimeout(() => el.classList.remove("maxpm-flash"), 1600);
}
// The comments again (after an action, and with each state of the page), without a reload of the page; and the
// answers of a doc's decisions (#4844).
async function loadComments(t) {
  if (!t.comments || !t.sig) return;
  try {
    const r = await fetch(`canvas/${canvasOf(t.target)}/meta?${t.sig}`);
    if (!r.ok || on !== t.id) return;
    const meta = await r.json();
    t.comments.render(meta);
    if (t.md && t.md.isConnected && !t.pin) updateAnswers(t.md, meta);
    if (t.frame && t.frame.contentWindow) postAnswers(t.frame.contentWindow, meta);
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

// The answers to the decision cards of a kit page (#4983), for its script: {n: {author, text, state}}.
function postAnswers(win, meta, extra) {
  const answers = {};
  for (const c of meta.comments || []) {
    const n = (/^decision:(\d+)$/.exec((c.anchor && c.anchor.path) || "") || [])[1];
    if (n && (!answers[n] || c.id > answers[n].id)) answers[n] = c;
  }
  const out = {};
  for (const [n, c] of Object.entries(answers)) out[n] = { author: c.author, state: c.state, text: c.body.replace(/^Answer to decision \d+ of \d+ \([^)]*\):\s*/, "") };
  win.postMessage(Object.assign({ maxpmCanvas: "answers", answers: out }, extra || {}), "*");
}
async function canvasMeta(t) {
  const r = await fetch(`canvas/${canvasOf(t.target)}/meta?${t.sig || await canvasSig(t)}`);
  if (!r.ok) throw new Error("no canvas");
  return r.json();
}
// A kit decision card's answer (#4983). The page is a stranger's: only an answer that the person just clicked or
// typed counts (the click in the frame activates this page too), and it goes the way of a doc's answer.
async function frameAnswer(t, win, d) {
  const n = +d.decision, text = String(d.text || "").slice(0, 2000).trim();
  const fail = (error) => win.postMessage({ maxpmCanvas: "answers", decision: n, error, answers: {} }, "*");
  if (!Number.isInteger(n) || !text) return fail("Pick an option or write your answer.");
  if (navigator.userActivation && !navigator.userActivation.isActive) return fail("Click the answer again.");
  if (!actor()) { toast("Choose your name in 'You are' first", true); return fail("Choose your name in 'You are' first."); }
  try {
    await act("canvas_answer", { name: "c" + canvasOf(t.target), decision: n, text });
    toast(`Answered decision ${n}: it went to the session`);
    postAnswers(win, await canvasMeta(t), { sent: n });
  } catch (err) { fail(String(err && err.message || "The answer did not go.")); }
}

// The helper script in an HTML canvas says where the person scrolls, and asks for it after a new version loads.
addEventListener("message", (e) => {
  const d = e.data || {}, t = tabs.find(x => x.id === on);
  if (t && t.next && e.source === t.next.contentWindow && d.maxpmCanvas === "ready") {  // a new version, out of sight
    if (t.scroll) e.source.postMessage({ maxpmCanvas: "scrollTo", x: t.scroll.x, y: t.scroll.y }, "*");
    findReady(e.source);
    return setTimeout(t.next.swap, 60);
  }
  if (!t || !t.frame || e.source !== t.frame.contentWindow || typeof d.maxpmCanvas !== "string") return;
  if (d.maxpmCanvas === "scroll") t.scroll = { x: +d.x || 0, y: +d.y || 0 };
  if (d.maxpmCanvas === "ready" && t.scroll) e.source.postMessage({ maxpmCanvas: "scrollTo", x: t.scroll.x, y: t.scroll.y }, "*");
  if (d.maxpmCanvas === "ready" && t.commenting) e.source.postMessage({ maxpmCanvas: "mode", on: true }, "*");
  if (d.maxpmCanvas === "ready") findReady(e.source);
  if (d.maxpmCanvas === "found" && findOpen() && String(d.q || "") === find.q) findCount(+d.n || 0, +d.i || 0);
  if (d.maxpmCanvas === "find-open") openFind(typeof d.text === "string" ? d.text.slice(0, 200) : "");
  if (d.maxpmCanvas === "escape") setCommenting(t, false);
  if (d.maxpmCanvas === "answers?" && !t.pin) canvasMeta(t).then(m => postAnswers(e.source, m)).catch(() => {});
  if (d.maxpmCanvas === "answer" && !t.pin) frameAnswer(t, e.source, d);
  if (d.maxpmCanvas === "shown" && !d.found) toast("That part is not in this version of the page", true);
  if (d.maxpmCanvas === "pick" && t.comments) {
    // What the frame says is data from a page a session wrote: strings only, cut short; the box shows it escaped.
    const str = (v, n) => String(v || "").slice(0, n);
    setCommenting(t, false);
    t.comments.pick({ path: str(d.path, 300), text: str(d.text, 200), quote: str(d.quote, 500) });
    const side = $("#spBody .sp-cside"); if (side) side.scrollIntoView({ block: "nearest" });
  }
});

// Find in the tab (#4877). Where it looks: the drawn text of a Markdown canvas; an HTML canvas through its helper
// script (its own origin: this page cannot read it); else the tab's body (an item, a list, a Markdown file, a tool).
// A page in a frame (a local page, a site, a PDF) and a picture are out of reach: the box says so.
const find = { q: "", i: 0, marks: [], root: null };
const FIND_SKIP = ".doc-edit, .sp-find";
const findOpen = () => !$("#spFind").classList.contains("hidden");
function findScope() {
  const box = $("#spBody"), t = tabs.find(x => x.id === on);
  if (t && canvasOf(t.target)) {
    if (t.md && t.md.isConnected) return { root: t.md };
    if (t.frame && t.frame.isConnected) return t.kind === "html" ? { frame: t.frame } : { none: "Find cannot read a picture" };
  }
  if (box.querySelector("iframe, img.sp-img")) return { none: "Find cannot read this page: click in it and use the browser's find" };
  return { root: box };
}
function findCount(n, i, none) {
  const el = $("#spFindN");
  el.textContent = none || (!find.q ? "" : n ? `${i + 1} of ${n}` : "No match");
  el.title = none || "";
  el.classList.toggle("sp-find-none", !!none);
}
// Mark the words again. step: 0 marks them anew (the first match, or with again the same one, without a scroll: the
// tab changed under the person); 1 and -1 go to the next and the previous match.
function runFind(step = 0, again = false) {
  const s = findScope();
  if (find.root && find.root !== s.root) { findClear(find.root); find.marks = []; find.root = null; findObserved(); }
  if (s.none) { find.marks = []; findObserved(); return findCount(0, 0, s.none); }
  if (s.frame) {
    if (s.frame.contentWindow) s.frame.contentWindow.postMessage({ maxpmCanvas: "find", q: find.q, step, again }, "*");
    if (!find.q) findCount(0, 0);
    return;
  }
  if (step === 0 || !find.marks.length || !find.marks[0].isConnected) {
    const keep = again ? find.i : 0;
    find.marks = findMark(s.root, find.q, FIND_SKIP); find.root = s.root;
    find.i = findGo(find.marks, keep, !again);
  } else find.i = findGo(find.marks, find.i + step, true);
  findObserved();
  findCount(find.marks.length, find.i);
}
// The marks change the tab's elements: those changes are not news for the watch below.
let findWatch = null, findLater = 0;
function findObserved() { if (findWatch) findWatch.takeRecords(); }
function openFind(text = "") {
  const box = $("#spFind"), inp = $("#spFindQ");
  if (panel().classList.contains("hidden")) return;
  box.classList.remove("hidden");
  $("#spFindBtn").setAttribute("aria-expanded", "true");
  if (text && text.length <= 200 && !/\n/.test(text)) inp.value = text;
  inp.focus(); inp.select();
  if (inp.value !== find.q || text) { find.q = inp.value; runFind(0); } else runFind(0, true);
}
function closeFind() {
  $("#spFind").classList.add("hidden");
  $("#spFindBtn").setAttribute("aria-expanded", "false");
  const q = find.q; find.q = "";
  runFind(0, true);
  find.q = q;  // the words stay in the box for the next time
  findCount(0, 0);
}
// A new frame of an HTML canvas (a new version, another tab) is ready: it finds too, and marks the words when the box
// is open.
function findReady(win) {
  win.postMessage({ maxpmCanvas: "finder" }, "*");
  if (findOpen() && find.q) win.postMessage({ maxpmCanvas: "find", q: find.q, step: 0, again: true }, "*");
}
function wireFind() {
  const inp = $("#spFindQ");
  let typing = 0;
  inp.addEventListener("input", () => { clearTimeout(typing); typing = setTimeout(() => { find.q = inp.value; runFind(0); }, 150); });
  inp.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); return closeFind(); }
    if (e.key !== "Enter") return;
    e.preventDefault();
    clearTimeout(typing);
    if (inp.value !== find.q) { find.q = inp.value; return runFind(0); }
    runFind(e.shiftKey ? -1 : 1);
  });
  $("#spFindNext").onclick = () => runFind(1);
  $("#spFindPrev").onclick = () => runFind(-1);
  $("#spFindX").onclick = closeFind;
  $("#spFindBtn").onclick = () => findOpen() ? closeFind() : openFind();
  // Cmd+F or Ctrl+F while the panel has the focus, or the person clicked in it last: this box, not the browser's.
  let inPanel = false;
  document.addEventListener("pointerdown", (e) => { inPanel = panel().contains(e.target); }, true);
  document.addEventListener("keydown", (e) => {
    if (!(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey || (e.key || "").toLowerCase() !== "f") return;
    if (panel().classList.contains("hidden") || !panel().offsetParent) return;
    const a = document.activeElement;
    if (!(a && a !== document.body ? panel().contains(a) : inPanel)) return;
    e.preventDefault();
    const sel = String(getSelection() || "").trim();
    openFind(sel && panel().contains(getSelection().anchorNode) ? sel : "");
  });
  // The tab changed (another tab, a new version, a list read again): the marks follow, at the same match.
  findWatch = new MutationObserver(() => {
    if (!findOpen() || !find.q) return;
    clearTimeout(findLater); findLater = setTimeout(() => runFind(0, true), 200);
  });
  findWatch.observe($("#spBody"), { childList: true, subtree: true, characterData: true });
}

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
    if (!session || c.agent !== session || tabOf(tabs, `canvas:${c.id}`)) continue;
    const t = { id: ++n, target: `canvas:${c.id}`, title: c.title || c.name, by: c.agent };
    tabs.push(t);
    if (on == null || isList(on)) { reveal(true); show(t); } else renderTabs();
  }
  const t = tabs.find(x => x.id === on);
  if (!t || !canvasOf(t.target) || t.pin || panel().classList.contains("hidden")) return;
  const h = canvasHeads.get(canvasOf(t.target));
  if (h && t.shown && h.version !== t.shown) return updateCanvas(t);  // in place, never a new draw (#4844)
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
    if (name === session) openInPanel(target, x.by || name);
    else addTab(name, target, x.by || name);
    if (!visible && name) offerCanvas(x, name, openSession);
  }
  openSeen = top;
}

// A tab in the panel of a session that the panel does not show now: it shows when the person picks that session.
function addTab(name, target, by) {
  const mine = bySession[name];
  if (mine) {
    let t = tabOf(mine.tabs, target);
    if (!t) { t = { id: ++n, target: firstTarget(target), title: "" }; mine.tabs.push(t); }
    if (by) t.by = by;
    mine.on = t.id;
    return;
  }
  let all = {};
  try { all = JSON.parse(store(TABS) || "{}") || {}; } catch (e) { all = {}; }
  const s = all[name] || { targets: [] };
  const have = (s.targets || []).find(x => targetKey(x) === targetKey(target)) || firstTarget(target);
  s.targets = (s.targets || []).filter(x => targetKey(x) !== targetKey(target)).concat(have);
  if (by) s.by = { ...(s.by || {}), [have]: by };
  s.on = have;
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
  if (!quiet || on !== kind) { on = kind; renderTabs(); tell(); $("#spAddr").value = ""; frameBar(true); }
  const box = $("#spBody"), name = session;
  let list = box.querySelector(`.sp-list[data-list="${kind}"] .sp-rows`);
  if (!quiet || !list) {
    box.innerHTML = `<div class="sp-note sp-list" data-list="${kind}"><b>${LISTS[kind]} of ${esc(name || "this session")}</b>
      <div class="muted">${kind === "items" ? "The items this session claimed, finished, released or gave away. Open one in a tab." : "The files, local pages and claude.ai artifacts this session wrote or named. Open one in a tab, or type an address above."}</div>
      ${kind === "items" ? addForm() : ""}
      <div class="sp-rows${kind === "artifacts" ? " sp-arts" : ""}"><div class="muted sl-none">${kind === "items" ? "Reading the history…" : "Reading the transcript…"}</div></div></div>`;
    list = box.querySelector(".sp-rows");
    if (kind === "items") wireAdd(box.querySelector("[data-sp-add]"), name);
    if (kind === "artifacts") wireRows(list, (s, target) => openInPanel(target, s));
  }
  let rows = [];
  try {
    const r = await fetch((kind === "items" ? "api/session-items" : "api/artifacts") + (name ? "?agent=" + encodeURIComponent(name) : ""));
    rows = r.ok ? ((await r.json())[kind] || []) : [];
  } catch (e) { /* an empty list says so */ }
  if (on !== kind || name !== session || !box.contains(list)) return;  // a tab or another session came first
  if (kind === "artifacts") return showRows(list, rows, "This session wrote or named nothing the side panel can show.");
  lastItems[name] = rows;
  const html = sessionQueue(name) + rows.map(x => `<div class="si-row">
      <span class="link" data-open="${x.id}">#${x.id}</span>
      <span class="si-t link" data-open="${x.id}">${esc(x.title)}</span>
      <span class="si-m">${chip("c-" + esc(x.status), esc(x.status.replace("_", " ")))} <span class="muted">${esc((x.did || []).join(", "))} · ${ago(x.at)}</span></span>
    </div>`).join("") || `<div class="muted sl-none">This session took no item yet.</div>`;
  if (list._html !== html) { list.innerHTML = html; list._html = html; }  // the same rows are not drawn again (#4844)
}
// The session's own queue (#5102 step 2, from the Board's Agents): above its items, each entry with × to take it out.
function sessionQueue(name) {
  const q = ((S && S.queues) || {})[name];
  return q && q.length ? `<div class="sp-queue">${queueHtml(q, name)}</div>` : "";
}
// + item (#5102 step 2): a new item in the session's project, found during the session's item, in that item's goals.
// The session's item: the one it holds, else the last one in its list.
const lastItems = {};
function addForm() {
  return `<form class="sp-add" data-sp-add autocomplete="off"><input placeholder="Title of a new item" aria-label="The title of a new item">${button("+ item", { type: "submit", title: "Add the item in this session's project, found during its item" })}</form>`;
}
function wireAdd(form, name) {
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = form.querySelector("input"), title = input.value.trim();
    if (!title) return input.focus();
    const a = ((S && S.agents) || []).find(x => x.name === name) || { holds: [] };
    const id = (a.holds[0] || {}).id || ((lastItems[name] || [])[0] || {}).id;
    const it = id ? S.items.find(i => i.id === id) : null;
    const project = it ? it.project : (a.waiting_in || "").split(",").filter(Boolean)[0];
    if (!project) return toast("This session has no item and no project yet: add the item on the Plan tab", true);
    try {
      const r = await act("item_add", { project, title, found_during: it ? it.id : undefined, goals: it && (it.goals || []).length ? it.goals : undefined });
      input.value = ""; toast(`Added #${r.id}${it ? `, found during #${it.id}` : ""}`);
    } catch (err) { /* act showed it */ }
  });
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
  markPair();
}

// by: the session (or person) that opened it; the tab's tooltip names the last one.
export function openInPanel(target, by) {
  target = String(target || "").trim().replace(/^#(\d+)$/, "item:$1");  // #12 in the address bar: the item
  if (!target) return;
  if (!isFile(target) && !itemOf(target) && !canvasOf(target) && !/^https?:\/\//i.test(target)) return toast("The side panel opens a web address (http, https), a full file path, an item (#12), or a canvas (canvas:3)", true);
  reveal(true);
  // The same thing again, from any link, list, or session: its tab shows and reads it again (#5000).
  let t = tabOf(tabs, target);
  if (!t) { t = { id: ++n, target: firstTarget(target), title: "" }; tabs.push(t); }
  if (by) t.by = by;
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
    const list = [];  // the tabs kept before #5000 may hold one thing twice: one tab for it
    for (const target of (s.targets || []).filter(x => typeof x === "string"))
      if (!tabOf(list, target)) list.push({ id: ++n, target, title: "", by: (s.by || {})[target] });
    const lst = /^list:(items|artifacts)$/.exec(s.on || "");
    mine = { tabs: list, on: lst ? lst[1] : ((s.on && tabOf(list, s.on)) || list[list.length - 1] || {}).id ?? null };
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
  wireFind();
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
