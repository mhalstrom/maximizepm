// An item in a tab of the Sessions side panel (#4685): what the drawer shows, to read beside the chat. The chat stays
// in sight; a #<id> in it, an item of the session's Items list, or the item a session holds opens here. Links to
// other items open in the panel too. "Edit in the drawer" opens the drawer for its actions and fields.
// Server: GET /api/item/<id> (the drawer's data).
import { esc, ago, left } from "../lib.js";
import { statusChip, prioChip, doerChip, modelChip, agentChip, tierChip } from "./chip.js";
import { msgLine } from "./drawer.js";

const links = (list) => list.map(w => `<div><span class="link" data-open="${w.id}">#${w.id} ${esc(w.title)}</span> <span class="muted">(${esc(w.status)})</span></div>`).join("")
  || '<div class="muted">nothing</div>';
const text = (title, value) => value ? `<div class="sec"><h4>${title}</h4><div class="spi-text">${esc(value)}</div></div>` : "";

export function itemViewHtml(it) {
  const holder = ["in_progress", "held"].includes(it.status) && it.assignee;
  return `<div class="sp-item">
    <div class="spi-top"><span class="muted">#${it.id} · ${esc(it.project)}</span><span class="spacer"></span>
      <button class="btn" type="button" data-drawer-open="${it.id}" title="Open #${it.id} in the drawer: claim, done, release, edit its fields">Edit in the drawer</button></div>
    <h3>${esc(it.title)}</h3>
    <div class="chips" style="justify-content:flex-start">${prioChip(it)}${doerChip(it)}${agentChip(it)}${tierChip(it)}${modelChip(it)}${statusChip(it)}</div>
    ${holder ? `<div class="meta">Held by <b>${esc(holder)}</b>${it.lease_expires_at ? " · lease " + left(it.lease_expires_at) + " left" : ""}</div>` : ""}
    <div class="meta">${esc(it.reason || "")}</div>
    ${text("Notes", it.notes)}${text("Context", it.context)}${text("Output", it.output)}
    ${(it.touches || []).length ? text("Touches", it.touches.join(", ")) : ""}${text("Check", it.check)}
    ${it.blocked_reason ? text("Outside blocker", it.blocked_text) : ""}
    <div class="sec"><h4>Waits on</h4>${links(it.waits_on_detail || [])}</div>
    <div class="sec"><h4>Unblocks</h4>${links(it.unblocks_detail || [])}</div>
    <div class="sec"><h4>Messages</h4>${(it.messages || []).map(m => msgLine(m)).join("") || '<div class="muted">none</div>'}</div>
    <div class="sec"><h4>History</h4>${(it.events || []).slice(0, 30).map(e => `<div class="ev">${ago(e.at)} · <b>${esc(e.actor)}</b> ${esc(e.change)}</div>`).join("")}</div>
  </div>`;
}
