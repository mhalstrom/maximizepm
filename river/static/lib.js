// Small helpers the page shares: DOM lookup, escaping, storage, toasts, and server actions.
// app.js sets hooks.refresh, so act() reloads the state after each action.
export const hooks = { refresh: async () => {} };

export const $ = (s) => document.querySelector(s);
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
export function store(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } }
export function actor() { return $("#actor").value || null; }
export function toast(msg, err) {
  const t = $("#toast"); t.textContent = msg; t.className = err ? "err" : ""; t.style.display = "block";
  clearTimeout(t._h); t._h = setTimeout(() => (t.style.display = "none"), err ? 6000 : 2500);
}
export async function act(op, args) {
  const r = await fetch("api/action", { method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ op, args, actor: actor() }) });
  const j = await r.json();
  if (!r.ok) { toast(j.error || "failed", true); throw new Error(j.error); }
  await hooks.refresh();
  return j.result;
}
// GET a JSON answer. An error says what a person can read (#5362): the server's own error, "the server answered 500"
// for a body that is no JSON, or "MaximizePM does not answer" when no answer came; never a parser's text.
export async function getJson(url) {
  let r;
  try { r = await fetch(url); } catch (e) { throw new Error("MaximizePM does not answer"); }
  let j = null;
  try { j = await r.json(); } catch (e) { /* not JSON: the status says it */ }
  if (!r.ok || j === null) throw new Error((j && j.error) || `the server answered ${r.status}`);
  return j;
}
export function ago(iso) {
  const s = Math.round((Date.now() - Date.parse(iso)) / 1000);
  if (s < 60) return s + "s ago"; if (s < 3600) return Math.round(s / 60) + "m ago";
  if (s < 86400) return Math.round(s / 3600) + "h ago"; return Math.round(s / 86400) + "d ago";
}
export function left(iso) { const m = Math.round((Date.parse(iso) - Date.now()) / 60000); return m >= 60 * 24 ? Math.round(m / 1440) + "d" : m >= 60 ? Math.round(m / 60) + "h" : m + "m"; }
// The short title of an item (#5086): its own short title, else the first words of its title. The same rule as
// core.short_title: no leading #<id> and no (...), cut at the first ':', ';' or dash that leaves three words or more,
// at most 7 words. The pages show it; the full title is the hover and the item page.
export function shortTitle(it) {
  if (!it) return "";
  if (it.short) return it.short;
  let t = String(it.title || "").replace(/^#\d+\s+/, "").replace(/\s*\([^()]*\)/g, "").split(/\s+/).filter(Boolean).join(" ");
  const m = t.match(/[:;]|\s[-\u2013\u2014]\s/);
  if (m && t.slice(0, m.index).split(" ").length >= 3) t = t.slice(0, m.index).replace(/[ ,.]+$/, "");
  const w = t.split(" ");
  if (w.length > 7) t = w.slice(0, 7).join(" ").replace(/[ ,.:;]+$/, "") + "\u2026";
  return t.length <= 60 ? t : t.slice(0, 59).trimEnd() + "\u2026";
}
export function clip(s, n) { s = String(s || "").trim(); return s.length > n ? s.slice(0, n) + "…" : s; }
export async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch (e) { /* fall back below */ }
  const ta = document.createElement("textarea"); ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta); ta.select();
  let ok = false; try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
  ta.remove(); return ok;
}
export function fillSelect(sel, values, keepFirst) {
  const el = $(sel), cur = el.value;
  const first = keepFirst ? el.options[0].outerHTML : "";
  el.innerHTML = first + values.map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
  if ([...el.options].some(o => o.value === cur)) el.value = cur;
}
// An action that types into an agent's terminal (terminal_keys, session_send): it sends the page's token, which
// maxpm serve makes at each start (GET api/token). After a restart of the server the old token is refused: read
// the new one and send once more.
let typingToken = null;
// The page's token, read once (and again after a restart of the server: fresh = true).
export async function pageToken(fresh = false) {
  if (!typingToken || fresh) typingToken = (await (await fetch("api/token")).json()).token;
  return typingToken;
}
export async function typeAction(op, args) {
  for (let i = 0; i < 2; i++) {
    if (!typingToken) typingToken = (await (await fetch("api/token")).json()).token;
    const r = await fetch("api/action", { method: "POST",
      headers: { "Content-Type": "application/json", "X-Maxpm-Token": typingToken },
      body: JSON.stringify({ op, args, actor: actor() }) });
    const j = await r.json().catch(() => ({}));
    if (r.status === 403 && i === 0 && /token/.test(j.error || "")) { typingToken = null; continue; }
    if (!r.ok) throw new Error(j.error || "failed");
    return j.result;
  }
}

// One key for each thing the side panel shows (#5000): the same thing under two addresses is one tab and one row.
// A claude.ai artifact is https://claude.ai/artifact/<id> (or code/artifact/<uuid>), without what follows its id (a
// query, a hash, a ')' or '.' that a chat link took along), whatever the scheme or the case of the host; a canvas
// canvas:<n> (canvas:c3 too); an item item:<n> (#12 too); a file path with / for \ and no / at its end; another web
// address as the browser writes it (its host in lower case, a / for an empty path; its hash stays: a local page can
// show other views by it).
// sessions.artifact_key does the same for the Artifacts list.
export function targetKey(target) {
  const s = String(target || "").trim();
  const art = /^https?:\/\/(?:www\.)?claude\.ai\/((?:code\/)?artifact\/)([\w-]+)/i.exec(s);
  if (art) return "https://claude.ai/" + art[1].toLowerCase() + art[2];
  const cv = /^canvas:c?(\d+)$/i.exec(s); if (cv) return "canvas:" + +cv[1];
  const it = /^(?:item:|#)(\d+)$/.exec(s); if (it) return "item:" + +it[1];
  if (/^(?:\/|~[/\\]|[a-z]:[/\\]|\\\\)/i.test(s)) return s.replace(/\\/g, "/").replace(/(.)\/+$/, "$1");
  if (/^https?:\/\//i.test(s)) { try { return new URL(s).href; } catch (e) { return s; } }
  return s;
}

// The id of an artifact in the page's address (#4653): a short hash of its target (a file path or a web address),
// the same in every browser, so /sessions/<session>/artifacts/<id> names one thing. 53 bits (cyrb53), in base 36.
export function artId(target) {
  let h1 = 0xdeadbeef, h2 = 0x41c6ce57;
  for (const ch of String(target)) {
    const c = ch.codePointAt(0);
    h1 = Math.imul(h1 ^ c, 2654435761); h2 = Math.imul(h2 ^ c, 1597334677);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return (4294967296 * (2097151 & h2) + (h1 >>> 0)).toString(36);
}
