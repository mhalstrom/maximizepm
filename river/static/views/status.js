// The status inbox (#5852): every running session of every project in one list, in four groups by its state (the
// server's sessions.status_of): Needs you (a prompt, a question, anything in its ! badge), Done (its turn ended after
// you last had its chat open), Working, Idle. A group's Push toggle sets the setting notify_status: maxpm serve then
// sends a push notice on your notification channels when a session enters that state. The rows come from the same
// read of GET /api/sessions as the session list (views/sessions.js), so the two never disagree.
import { esc, ago, clip } from "../lib.js";
import { chip, projectChip } from "../components/chip.js";
import { button } from "../components/button.js";
import { empty } from "../components/empty.js";

export const GROUPS = [
  ["needs_you", "Needs you", "needs you", "A prompt or a question waits for you"],
  ["done", "Done", "done", "Its turn ended since you last looked"],
  ["working", "Working", "working", "It runs a turn or background work"],
  ["idle", "Idle", "idle", "It waits, and you have seen it"],
];
const DOT = { needs_you: "needs", done: "active", working: "active", idle: "away" };

// The sessions of one state, in the order the person wants them: what waits longest first in Needs you, the newest
// first in the other groups.
export function byStatus(list) {
  const out = Object.fromEntries(GROUPS.map(([k]) => [k, []]));
  for (const s of list) (out[s.status] || out.idle).push(s);
  const t = (s) => Date.parse(s.at || "") || 0;
  out.needs_you.sort((a, b) => t(a) - t(b));
  for (const k of ["done", "working", "idle"]) out[k].sort((a, b) => t(b) - t(a));
  return out;
}

// The words of the counts: "1 needs you · 2 done · 3 working".
export function countLine(list) {
  const g = byStatus(list);
  return GROUPS.filter(([k]) => g[k].length).map(([k, , word]) => `${g[k].length} ${k === "needs_you" && g[k].length > 1 ? "need you" : word}`)
    .join(" · ") || "No session runs";
}

// The Status row of the session list: the counts, and a ! badge while a session needs you.
export function statusRowHtml(list, on) {
  const n = byStatus(list).needs_you.length;
  return `<div class="srow srow-status${n ? " needs" : ""}${on ? " on" : ""}" data-status="1">
    <div class="srow-main" tabindex="0" role="button" title="All sessions by state" aria-label="Status of all sessions">
      <div class="srow-h"><b class="srow-t">Status</b><span class="spacer"></span>${n ? `<span class="wbadge" title="${n} session(s) need you">! ${n}</span>` : ""}</div>
      <div class="sub">${esc(countLine(list))}</div></div></div>`;
}

function row(s) {
  const tag = s.role === "manager" ? `<span class="mgr-tag">Manager</span>` : s.role === "expert" ? `<span class="mgr-tag expert-tag">Expert</span>` : "";
  const head = s.role === "manager" && s.title === "Manager" ? "" : s.title || s.name;
  const n = (s.waiting || []).length;
  const html = `<span class="st-h">${tag}${head ? `<b class="st-t">${esc(head)}</b>` : ""}${s.project ? projectChip(s.project) : ""}<span class="spacer"></span>${n ? `<span class="wbadge">! ${n}</span>` : ""}<span class="dot ${DOT[s.status] || "away"}"></span><span class="sago" data-at="${esc(s.at || "")}">${s.at ? ago(s.at) : ""}</span></span>
    <span class="sub st-sub">${esc(clip(s.doing || s.state_label || "", 140))}</span>`;
  return button(html, { kind: "card", cls: "st-row", data: { "status-go": s.name }, title: `${s.name}: ${s.state_label || ""}`,
    label: `Open session ${head || s.name}` });
}

// The four groups. push: the value of notify_status ("off", or a comma list of states).
export function statusHtml(list, push) {
  const g = byStatus(list), on = new Set(String(push || "off").split(",").map(x => x.trim()));
  if (!list.length) return empty("No session runs now. Start one with New session.");
  return GROUPS.map(([k, name, , tip]) => `<section class="st-group" data-group="${k}">
      <h3 class="st-gh"><span title="${esc(tip)}">${esc(name)}</span> ${chip("c-p", String(g[k].length))}<span class="spacer"></span>${
        button("Push", { cls: "st-push", pressed: on.has(k), data: { "status-push": k }, title: "Push notice" })}</h3>
      ${g[k].length ? g[k].map(row).join("") : `<div class="muted t-s st-none">None</div>`}
    </section>`).join("");
}

// The next value of notify_status when the person turns Push of one state on or off.
export function togglePush(push, key) {
  const on = new Set(String(push || "off").split(",").map(x => x.trim()).filter(x => x && x !== "off"));
  if (on.has(key)) on.delete(key); else on.add(key);
  const keep = GROUPS.map(([k]) => k).filter(k => on.has(k));
  return keep.length ? keep.join(",") : "off";
}
