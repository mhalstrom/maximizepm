// Links to a session's artifacts everywhere on the page (#4689): in the chat, the ! dialog (its question, command,
// and "What the terminal shows"), the Needs you list, and an item in the side panel. linkPaths (chat.js) marks
// each file path and local page address; checkPaths asks the server (GET /api/file-check) what each path means
// for that session: a full path the side panel shows (the link opens it there), a path it refuses (no link; the
// reason shows on hover), or a relative name it finds nothing for (plain text again). A click outside the
// Sessions tab's own chat goes to the Sessions tab, picks the session, and opens the file or page in its side
// panel (window.riverOpenInPanel, app.js).
import { pageToken } from "../lib.js";
import { linkPaths } from "./chat.js";

export async function checkPaths(root, agent) {
  const spans = [...root.querySelectorAll("[data-file]:not([data-checked])")];
  if (!spans.length) return;
  spans.forEach(s => { s.dataset.checked = "1"; });
  const paths = [...new Set(spans.map(s => s.dataset.file))];
  const res = {};
  const token = await pageToken().catch(() => "");
  for (let i = 0; i < paths.length; i += 40) {
    const q = new URLSearchParams({ token });
    if (agent) q.set("agent", agent);
    paths.slice(i, i + 40).forEach(p => q.append("path", p));
    try { const r = await fetch("api/file-check?" + q); if (r.ok) Object.assign(res, (await r.json()).paths || {}); } catch (e) { /* the links stay */ }
  }
  for (const s of spans) {
    const x = res[s.dataset.file];
    if (!x) continue;  // no answer: the link stays as it was
    if (x.ok) { s.dataset.file = x.target; s.title = "Open in the side panel: " + x.target; continue; }
    if (s.dataset.rel) { s.replaceWith(document.createTextNode(s.textContent)); continue; }
    s.classList.remove("link"); s.classList.add("fpath-no"); s.removeAttribute("tabindex");
    s.removeAttribute("data-file");
    s.title = "MaximizePM cannot show this file: " + x.why;
  }
}

// Mark the paths and local pages in root, then check them for agent's session.
export function linkArtifacts(root, agent, opts) {
  if (!root) return Promise.resolve();
  linkPaths(root, opts);
  return checkPaths(root, agent);
}

// The target of a link that opens in the side panel, or null.
export function linkTarget(t) {
  const f = t.closest && t.closest("[data-file], [data-url]");
  return f ? (f.dataset.file || f.dataset.url) : null;
}

// One click handler for the links outside the Sessions tab's chat (that one opens them itself, and so does the side
// panel). The session is the closest data-agent; with none, the Sessions tab's current one.
export function wireArtLinks() {
  const go = (e, el) => {
    const target = linkTarget(el);
    if (!target || e.defaultPrevented) return;
    e.preventDefault(); e.stopPropagation();
    const agent = (el.closest("[data-agent]") || {}).dataset?.agent || null;
    const dlg = el.closest('[role="dialog"]:not(.hidden)');
    if (dlg) dlg.classList.add("hidden");  // the panel shows behind it
    if (window.riverOpenInPanel) window.riverOpenInPanel(agent, target);
  };
  document.addEventListener("click", (e) => { if (!e.metaKey && !e.ctrlKey) go(e, e.target); });
  document.addEventListener("keydown", (e) => { if (e.key === "Enter" && linkTarget(e.target)) go(e, e.target); });
}
