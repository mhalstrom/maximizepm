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
export function ago(iso) {
  const s = Math.round((Date.now() - Date.parse(iso)) / 1000);
  if (s < 60) return s + "s ago"; if (s < 3600) return Math.round(s / 60) + "m ago";
  if (s < 86400) return Math.round(s / 3600) + "h ago"; return Math.round(s / 86400) + "d ago";
}
export function left(iso) { const m = Math.round((Date.parse(iso) - Date.now()) / 60000); return m >= 60 * 24 ? Math.round(m / 1440) + "d" : m >= 60 ? Math.round(m / 60) + "h" : m + "m"; }
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
