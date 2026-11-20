// The links between an artifact and its tasks, both ways (#6208, mark 2026-11-19: 'when we get a task that is linked
// to an artifact review we need to link back to the task from the artifact in maxpm'). core.artifact_tasks gives the
// tasks and questions of an artifact (the canvas bar, the Artifacts lists); core.artifacts_of gives the artifacts of
// a task or a question (the item view, the drawer, the Waits for you cards).
// tasksHtml(tasks): "Made for #12 …" or "Reviewed in #12 …" with the state; a click on #12 opens the item (data-open).
// artifactsHtml(links): each artifact as a link that opens it in the side panel (data-url, data-file:
// artLinks.wireArtLinks), or a claude.ai artifact in a new tab.
import { esc, clip } from "../lib.js";

function taskHtml(t) {
  const st = `<span class="muted t-s">(${esc(t.state)})</span>`;
  if (t.message) {
    const q = `${esc(t.message_kind || "message")} #${t.message}${t.to ? ` to ${esc(t.to)}` : ""}`;
    return `<span class="art-task">Asked in ${q}${t.item
      ? ` about <span class="link" data-open="${t.item}" title="${esc(t.title || "")}">#${t.item}</span>` : ""} ${st}</span>`;
  }
  if (!t.item) return "";
  const word = t.how === "made for" ? "Made for" : "Reviewed in";
  return `<span class="art-task">${word} <span class="link" data-open="${t.item}" title="${esc(t.title || "")}">#${t.item} ${esc(clip(t.short || t.title || "", 60))}</span> ${st}</span>`;
}

// The tasks of one artifact, one line. Empty text when it has none.
export function tasksHtml(tasks) {
  const xs = (tasks || []).map(taskHtml).filter(Boolean);
  return xs.length ? `<div class="art-tasks t-s">${xs.join(" · ")}</div>` : "";
}

function artifactHtml(a) {
  const label = esc(clip(a.label || a.target, 70));
  if (a.kind === "artifact") return `<a href="${esc(a.target)}" target="_blank" rel="noopener noreferrer" title="Opens claude.ai">${label}</a>`;
  const what = a.kind === "canvas" ? `canvas ${esc(a.canvas ? a.canvas.name : "")}${a.canvas && a.canvas.version ? " v" + a.canvas.version : ""}`
    : a.kind === "file" ? "file" : "page";
  const attr = a.kind === "file" ? `data-file="${esc(a.target)}"` : `data-url="${esc(a.target)}"`;
  return `<span class="link" ${attr} tabindex="0" title="Open in panel: ${esc(a.target)}">${label}</span> <span class="muted">${what}</span>`;
}

// The artifacts of one task or question: a section of the item view and the drawer (h4), or a line on a card.
export function artifactsHtml(links, { line = false } = {}) {
  const xs = links || [];
  if (!xs.length) return "";
  if (line) return `<div class="art-links t-s"><span class="muted">About:</span> ${xs.map(artifactHtml).join(" · ")}</div>`;
  return `<div class="sec"><h4>Artifacts</h4>${xs.map(a => `<div>${artifactHtml(a)}</div>`).join("")}</div>`;
}
