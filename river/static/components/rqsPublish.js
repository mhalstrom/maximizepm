// Share artifact (#5789, the label: mark 2026-11-13): the button of a canvas bar and the line under the bar.
// The button publishes the version on screen as a private realquicksite page and opens it in a new tab. The tab opens
// in the click itself, so no pop-up blocker stops it; it waits on maxpm serve (/api/rqs/wait/<id>) and goes to the
// page when the publish is done. The first time, the same tab goes through the connect step of realquicksite.
// One press (#6011, design-doc-mockup v7): a page that is shared already gets the version on screen at the same link,
// with no question first; the press is the person's yes (core.canvas_publish_send: a session never sends a new version
// of a shared page). The line under the bar says when the shared page is older than the version on screen.
import { esc, act, toast } from "../lib.js";
import { button } from "./button.js";

export function publishButton() {
  return button("Share artifact", { data: { "cv-rqs": "" }, title: "Publish on realquicksite" });
}

const day = (iso) => new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
const short = (url) => url.replace(/^https?:\/\//, "").replace(/\/$/, "");

// The page of the canvas under the bar: the link, private or shared, the end date, publishing, the error, and the
// problems of realquicksite's check. "" when the canvas has no page.
export function pageLine(p, version) {
  if (!p) return "";
  const parts = [];
  if (p.state === "publishing") parts.push("<span>Publishing on realquicksite…</span>");
  if (p.url && !p.ended) {
    parts.push(`<a class="link" href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">${esc(short(p.url))}</a>`,
               `<span>${p.access === "link" ? "shared: anyone with the link" : "private"}</span>`,
               `<span>${version && p.version !== version ? `the shared page shows v${esc(p.version)}; this is v${esc(version)}: Share artifact publishes it` : `v${esc(p.version)}`}</span>`);
    if (p.expires_at) parts.push(`<span>ends ${esc(day(p.expires_at))}</span>`);
  } else if (p.url && p.ended) parts.push(`<span>The page ended${p.expires_at ? " on " + esc(day(p.expires_at)) : ""}.</span>`);
  if (p.error) parts.push(`<span class="cv-rqs-err">${p.state === "failed" ? "Not published: " : ""}${esc(p.error)}</span>`);
  const probs = p.problems || [];
  if (probs.length) parts.push(`<span class="cv-rqs-err">${probs.length} problem${probs.length === 1 ? "" : "s"} found</span>`);
  return `<div class="cv-rqs-line">${parts.join("")}</div>` + (probs.length
    ? `<ul class="cv-rqs-probs">${probs.slice(0, 8).map(x => `<li>${esc(x.viewport || "")}: ${esc(x.message || "")}</li>`).join("")}</ul>` : "");
}

// The click on the button. id: the canvas; meta: its meta (with page); version: the version on screen.
export async function openOnRqs(id, meta, version) {
  const p = meta.page;
  if (p && p.state === "done" && p.url && !p.ended && p.version === version) {
    window.open(p.url, "_blank", "noopener");
    return;
  }
  const confirm = !!(p && p.url && !p.ended && p.access === "link");  // the press is the yes (#6011)
  const w = window.open("", "_blank");
  let r;
  try { r = await act("canvas_publish", { name: String(id), version, confirm }); } catch (e) { if (w) w.close(); return; }
  const to = new URL(r.open || r.url || r.wait, location.href).href;
  if (w) { w.opener = null; w.location.href = to; return; }
  if (r.connect) toast("Allow pop-ups for this page, then click Share artifact again", true);
  else toast(r.open ? "The page is on realquicksite: the link is under the bar" : "Publishing on realquicksite…");
}
