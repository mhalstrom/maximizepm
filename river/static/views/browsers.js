// The Browsers view (#5879, mark 2026-11-14: 'a view in sessions that you can see all the active browser and click into
// the session associated with each'). One tile for each browser that runs now: its newest frame as a small picture,
// the page's site and title, the session and project, who controls it, and its last change. A tile opens its session
// with the Browser in the side panel; a project's own browser (page walks, no session) opens the project in Plan.
// Cheap by design: the list (GET api/browsers, read from memory) every LIST_MS while the view shows; a tile's picture
// (GET api/browser/thumb, the frame as serve has it) only when its frame changed, the tile is on screen, and THUMB_MS
// passed since its last picture. A tile off screen asks nothing until it scrolls into view. Neither read counts as a
// use of the browser: a list that someone watches keeps no browser from browser_idle_end and sets no pace.
import { $, esc, ago, pageToken } from "../lib.js";
import { button } from "../components/button.js";
import { empty } from "../components/empty.js";
import { openBrowser } from "../components/browserTool.js";
import { S, nav } from "../page.js";

const LIST_MS = 3000, THUMB_MS = 5000;
let wired = false, timer = null, token = null, list = [], seen = null, reading = false;
const pics = new Map();     // key -> {url: an object URL of the JPEG, seq, at: when it was read, busy}
const onScreen = new Set(); // the keys of the tiles in view

const keyOf = (b) => JSON.stringify([b.project, b.session || ""]);
const shown = () => !$("#sessBrowsers").classList.contains("hidden") && !document.hidden;
const nameOf = (agent) => ((S && S.session_names) || {})[agent] || agent;
function site(url) {
  try { const u = new URL(url); return /^https?:$/.test(u.protocol) ? u.host : url; } catch (e) { return url || ""; }
}

export function showBrowsers() {
  const box = $("#brwView");
  if (!wired) { wire(box); wired = true; }
  if (!box.innerHTML) box.innerHTML = empty("Reading the browsers…");
  tick();
}

async function withToken(path) {
  token = token || await pageToken();
  let r = await fetch(path + "&token=" + encodeURIComponent(token));
  if (r.status === 403) { token = await pageToken(true); r = await fetch(path + "&token=" + encodeURIComponent(token)); }
  return r;
}

async function tick() {
  if (reading) return;  // one read at a time: its end sets the next timer
  clearTimeout(timer); timer = null;
  if (!shown()) return;  // the view or the page is hidden: showBrowsers or visibilitychange starts it again
  reading = true;
  try {
    const r = await withToken("api/browsers?v=1");
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || "MaximizePM does not answer");
    list = (await r.json()).browsers || [];
    draw($("#brwView"));
    // The rows of the Browsers tab of the session list (#5883).
    document.dispatchEvent(new CustomEvent("maxpm-browsers", { detail: { rows: list.map(b => ({ key: keyOf(b),
      who: b.session ? nameOf(b.session) : "No session", site: site(b.url) || "New tab", project: b.project })) } }));
  } catch (e) {
    $("#brwView").innerHTML = empty(`Could not read the browsers (${esc(e.message)}); the page asks again.`);
    $("#brwView").dataset.sig = "";
  } finally { reading = false; }
  if (shown()) timer = setTimeout(tick, LIST_MS);
}

function ctlText(c) {
  if (!c || !c.who) return "Nobody controls it";
  return c.kind === "person" ? `${esc(c.who)} controls it` : `${esc(nameOf(c.who))} drives it`;
}

// #5885: the page's size: a fixed one by its name, else Fit (the size of the panel that shows it)
const SIZE_NAMES = { phone: "Phone", tablet: "Tablet", laptop: "Laptop", desktop: "Desktop" };
function sizeText(b) {
  if (b.window) return "in a window";
  const f = b.forced;
  if (f) return `${SIZE_NAMES[f.name] || "Custom"} ${f.w} × ${f.h}`;
  return "Fit" + (b.w && b.h ? ` ${b.w} × ${b.h}` : "");
}

function tile(b) {
  const k = keyOf(b);
  const who = b.session ? `<b>${esc(nameOf(b.session))}</b>` : `<b class="muted">No session</b>`;
  const html = `<span class="brw-pic" data-brw-pic="${esc(k)}"><img alt="" hidden></span>
    <span class="brw-info">
      <span class="brw-site">${esc(site(b.url) || "New tab")}${b.tabs > 1 ? ` <span class="muted t-s">+${b.tabs - 1} tab${b.tabs > 2 ? "s" : ""}</span>` : ""}</span>
      ${b.title ? `<span class="brw-title muted t-s">${esc(b.title)}</span>` : ""}
      <span class="brw-who t-s">${who} <span class="muted">· ${esc(b.project)}</span></span>
      <span class="brw-ctl muted t-s">${ctlText(b.controller)} · ${esc(sizeText(b))}<span class="spacer"></span><span class="sago" data-at="${esc(b.last || "")}"></span></span>
    </span>`;
  return button(html, { kind: "card", cls: "brw-tile" + (b.controller && b.controller.kind === "person" ? " brw-person" : ""),
    title: b.session ? `Open ${b.session} and its browser` : `Open the project ${b.project}`, data: { "brw-key": k } });
}

function draw(box) {
  const html = list.length ? `<div class="brw-grid">${list.map(tile).join("")}</div>`
    : empty("No browser runs now. A session's browser starts with its first browser step, and stops when nobody uses it.");
  if (box.dataset.sig !== html) {
    box.innerHTML = html; box.dataset.sig = html;
    watch(box);
  }
  const keys = new Set(list.map(keyOf));
  for (const [k, p] of pics) if (!keys.has(k)) { if (p.url) URL.revokeObjectURL(p.url); pics.delete(k); }
  for (const b of list) place(box, keyOf(b));
  box.querySelectorAll(".sago").forEach(x => { x.textContent = x.dataset.at ? ago(x.dataset.at) : ""; });
  for (const b of list) if (onScreen.has(keyOf(b))) picture(b);
}

// Which tiles are on screen: only those read their pictures.
function watch(box) {
  if (seen) seen.disconnect();
  onScreen.clear();
  seen = new IntersectionObserver((entries) => {
    for (const e of entries) {
      const k = e.target.dataset.brwPic;
      if (e.isIntersecting) { onScreen.add(k); const b = list.find(x => keyOf(x) === k); if (b) picture(b); }
      else onScreen.delete(k);
    }
  });
  box.querySelectorAll("[data-brw-pic]").forEach(el => seen.observe(el));
}

function place(box, k) {
  const p = pics.get(k);
  const el = [...box.querySelectorAll("[data-brw-pic]")].find(x => x.dataset.brwPic === k);
  const img = el && el.querySelector("img");
  if (!img || !p || !p.url) return;
  if (img.src !== p.url) img.src = p.url;
  img.hidden = false;
}

async function picture(b) {
  const k = keyOf(b), p = pics.get(k) || {};
  pics.set(k, p);
  if (p.busy || !b.seq || p.seq === b.seq || Date.now() - (p.at || 0) < THUMB_MS) return;
  p.busy = true;
  try {
    const r = await withToken(`api/browser/thumb?project=${encodeURIComponent(b.project)}&session=${encodeURIComponent(b.session || "")}`);
    if (r.status === 200) {
      const blob = await r.blob();
      if (p.url) URL.revokeObjectURL(p.url);
      p.url = URL.createObjectURL(blob);
      p.seq = +(r.headers.get("X-Frame-Seq") || b.seq);
    }
    p.at = Date.now();
  } catch (e) { p.at = Date.now(); /* the next tick tries again */ } finally { p.busy = false; }
  if (pics.get(k) === p) place($("#brwView"), k);
}

// A tile, or a row of the Browsers tab of the list (#5883): its session with the Browser beside the chat; a project's
// own browser (no session) opens the project in Plan.
export async function openBrowserRow(key) {
  const b = list.find(x => keyOf(x) === key);
  if (!b) return;
  if (!b.session) return nav.openProject(b.project);
  await nav.openSession(b.session);
  openBrowser(b.session, null, b.project).catch(() => {});
}

function wire(box) {
  box.addEventListener("click", (e) => {
    const t = e.target.closest("[data-brw-key]");
    if (t) openBrowserRow(t.dataset.brwKey);
  });
  document.addEventListener("visibilitychange", () => { if (shown() && !timer && !reading) tick(); });
}
