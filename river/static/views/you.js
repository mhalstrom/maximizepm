// The You view of the Sessions tab (#5102 step 1, design #4657): what waits on the person in "You are". The You row
// at the top of the session list shows a ! badge with the count of what needs the person now (Needs you and the
// questions that wait on an answer, #5114) and opens this view: the items an agent took over (Open, Undo, OK), Needs you (you-needs.js) and the
// inbox (you-inbox.js). These were the parts of the Board about the person; the plan's parts go to Plan.
import { $, esc, act, toast, ago, shortTitle } from "../lib.js";
import { projectChip } from "../components/chip.js";
import { nyCard } from "../components/card.js";
import { agentStart } from "../components/agentStart.js";
import { button } from "../components/button.js";
import { S } from "../page.js";
import { NY, myAssistant, needsCount } from "./you-needs.js";
import { inboxOpenCount, inboxQuestions } from "./you-inbox.js";

// What needs the person now (#5114, mark: the badge said '! 900'): the true count of Needs you (items for a person,
// questions and alerts) and the questions in the inbox that wait on an answer and are not in Needs you. The items an
// agent took off the list and the unread notes wait on nothing: the line under it counts them, the badge does not.
export function youCount() {
  const inNy = new Set(NY.map(e => e.message_id).filter(Boolean));
  return needsCount() + inboxQuestions().filter(m => !inNy.has(m.id)).length;
}

// The You row of the session list, and the head of the You view. It draws again on each state and each poll of Needs
// you and the inbox, and replaces its HTML only when it changed, so a row keeps its hover and focus.
export function renderYou() {
  const row = $("#sessYouRow"); if (!row) return;
  const n = youCount(), N = needsCount(), msgs = inboxOpenCount(), T = ((S && S.takeovers) || []).length;
  const line = [N ? `${N} need${N === 1 ? "s" : ""} you` : "", T ? `${T} taken off your list` : "",
                msgs ? `${msgs} message${msgs === 1 ? "" : "s"}` : ""].filter(Boolean).join(" · ") || "Nothing waits on you";
  const on = row.dataset.on === "1";
  const html = `<div class="srow srow-you${n ? " needs" : ""}${on ? " on" : ""}" data-you="1">
    <div class="srow-main" tabindex="0" role="button" title="What waits on you: Needs you, items an agent took off your list, and your inbox" aria-label="You: what waits on you">
      <div class="srow-h"><b class="srow-t">You</b><span class="spacer"></span>${n ? `<span class="wbadge" title="${n} thing(s) need you now: Needs you and the questions that wait on your answer">! ${n}</span>` : ""}</div>
      <div class="sub">${esc(line)}</div></div></div>`;
  if (row.dataset.sig !== html) { row.innerHTML = html; row.dataset.sig = html; }
  // The agent that goes through the list with the person (#5103): next to Copy prompt, in the head of the view.
  // While your assistant runs (#5110), the same place opens its chat.
  const mine = myAssistant(), slot = $("#agentAllSlot");
  const b = mine ? agentStart(null, { label: "Open your assistant", cls: "primary", attrs: 'id="agentAll"',
      title: `${mine.name} goes through what waits on you, with you: open its chat` })
    : NY.length ? agentStart(S && S.launch_agents, { label: "Go through with an agent", cls: "primary",
      attrs: 'id="agentAll"', title: "Start your assistant: an agent session that goes through what waits on you, one at a time, with you; its chat opens here" }) : "";
  if (slot.dataset.sig !== b) { slot.innerHTML = b; slot.dataset.sig = b; }
}
// The You row shows as chosen while the You view shows.
export function markYou(on) {
  const row = $("#sessYouRow"); row.dataset.on = on ? "1" : "";
  const r = row.querySelector(".srow"); if (r) r.classList.toggle("on", !!on);
}

let takeoversOpen = false;
export function renderTakeovers() {
  const T = S.takeovers || [];
  $("#takeoverPanel").classList.toggle("hidden", !T.length);
  $("#nothingForYou").classList.toggle("hidden", !!NY.length || !!T.length);
  renderYou();
  const words = { "took over": "took over", "done": "marked done", "dropped": "dropped" };
  const head = `<div class="nyl" data-takeovers-toggle="1"><span class="t">${takeoversOpen ? "▾" : "▸"} ${T.length} item${T.length === 1 ? "" : "s"} an agent took off your list: review or undo</span><span class="chips">${button("OK all", { data: { "takeovers-okall": 1 }, title: "Accept all of them" })}</span></div>`;
  if (!takeoversOpen) { $("#takeovers").innerHTML = head; return; }
  $("#takeovers").innerHTML = head + T.map(t => nyCard(`<b title="${esc(t.title)}">#${t.id} ${esc(shortTitle(t))}</b> ${projectChip(t.project)}<span class="muted t-s">${ago(t.takeover_at)}</span>`, `<div class="ny-c"><b>${esc(t.takeover_by)}</b> ${words[t.takeover_kind] || t.takeover_kind} it: ${esc(t.takeover_note || "")}</div>
      <div class="actions">${button("Open", { data: { open: t.id } })}${button("Undo: give it back to me", { data: { "undo-takeover": t.id } })}${button("OK", { kind: "primary", data: { "takeover-ok": t.id } })}</div>`)).join("");
}
// Open the list of taken over items.
export function openTakeovers() { takeoversOpen = true; renderTakeovers(); }

// A click on this view's controls; true when it was one (the frame then stops).
export function onClick(t) {
  if (t.dataset.takeoversOkall) return run(async () => { for (const x of S.takeovers || []) await act("takeover_seen", { id: x.id }).catch(() => {}); });
  if (t.closest("[data-takeovers-toggle]") && !t.closest("button")) return run(async () => { takeoversOpen = !takeoversOpen; return renderTakeovers(); });
  if (t.dataset.undoTakeover) return run(async () => { await act("undo_takeover", { id: +t.dataset.undoTakeover }).then(() => toast("Back on your list")).catch(() => {}); });
  if (t.dataset.takeoverOk) return run(async () => { await act("takeover_seen", { id: +t.dataset.takeoverOk }).catch(() => {}); });
  return false;
}
const run = (f) => { f(); return true; };
