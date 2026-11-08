// The side panel of the Sessions tab (#4619): what a session points to, beside its chat. A local page (localhost,
// 127.0.0.1) in a frame; a local file (image, PDF, HTML, Markdown, text) that maxpm serve gives read only
// (GET /api/file, with the page's token, only under the project folders, their worktrees, and uploads); an outside
// page in a frame when the site allows it (GET /api/frame-check), else its title and Open in a new tab. Tabs for
// several, an address bar, Reload, Open in a new tab, Close, and a handle to resize it (the width stays).
import { $, esc, store, toast, pageToken } from "../lib.js";
import { markdown } from "./chat.js";

const WIDTH = "river.sidepanel.width";
const LOCAL = /^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(:\d+)?(\/|$)/i;
const IMAGE = /\.(png|jpe?g|gif|webp|svg)$/i, MD = /\.(md|txt)$/i;

let tabs = [], on = null, n = 0;
const panel = () => $("#sessPanel");

function isFile(target) { return target.startsWith("/") || target.startsWith("~/"); }
function label(target) {
  if (isFile(target)) return target.split("/").pop();
  try { const u = new URL(target); return u.host + (u.pathname.length > 1 ? u.pathname : ""); } catch (e) { return target; }
}
async function fileUrl(path) {
  return `api/file?path=${encodeURIComponent(path)}&token=${encodeURIComponent(await pageToken())}`;
}

function renderTabs() {
  $("#spTabs").innerHTML = tabs.map(t => `<span class="sp-tab${t.id === on ? " on" : ""}" data-tab="${t.id}" title="${esc(t.target)}">`
    + `<span class="sp-tab-l">${esc(t.title || label(t.target))}</span><button type="button" class="sp-x" data-close="${t.id}" title="Close this tab" aria-label="Close this tab">✕</button></span>`).join("");
}

async function show(t) {
  on = t.id;
  renderTabs();
  $("#spAddr").value = t.target;
  const box = $("#spBody");
  box.replaceChildren();
  const note = (html) => { const d = document.createElement("div"); d.className = "sp-note"; d.innerHTML = html; box.append(d); };
  const frame = (src) => { const f = document.createElement("iframe"); f.src = src; f.className = "sp-frame";
    f.setAttribute("sandbox", "allow-scripts allow-forms allow-popups allow-same-origin"); box.append(f); };
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

export function openInPanel(target) {
  target = String(target || "").trim();
  if (!target) return;
  if (!isFile(target) && !/^https?:\/\//i.test(target)) return toast("The side panel opens a web address (http, https) or a full file path", true);
  panel().classList.remove("hidden");
  $("#sessLayout").classList.add("with-panel");
  let t = tabs.find(x => x.target === target);
  if (!t) { t = { id: ++n, target, title: "" }; tabs.push(t); }
  show(t);
}

function close(id) {
  tabs = tabs.filter(t => t.id !== id);
  if (!tabs.length) { on = null; panel().classList.add("hidden"); $("#sessLayout").classList.remove("with-panel"); $("#spBody").replaceChildren(); return; }
  if (on === id) show(tabs[tabs.length - 1]); else renderTabs();
}

export function wirePanel() {
  const w = +store(WIDTH);
  if (w >= 280) document.documentElement.style.setProperty("--sp-w", w + "px");
  $("#spTabs").addEventListener("click", (e) => {
    const x = e.target.closest("[data-close]"); if (x) return close(+x.dataset.close);
    const t = e.target.closest("[data-tab]"); if (t) show(tabs.find(z => z.id === +t.dataset.tab));
  });
  $("#spForm").addEventListener("submit", (e) => { e.preventDefault(); openInPanel($("#spAddr").value); });
  $("#spReload").onclick = () => { const t = tabs.find(z => z.id === on); if (t) show(t); };
  $("#spNew").onclick = async () => {
    const t = tabs.find(z => z.id === on); if (!t) return;
    window.open(isFile(t.target) ? await fileUrl(t.target) : t.target, "_blank", "noopener");
  };
  $("#spClose").onclick = () => { tabs = []; close(-1); };
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
