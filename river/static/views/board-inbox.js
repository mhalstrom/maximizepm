// The Board's Inbox panel (#4656): messages for the person in "You are", with the answer each kind needs, and the
// form that sends one.
import { $, esc, actor, toast, act, ago, clip, fillSelect } from "../lib.js";
import { chip } from "../components/chip.js";
import { makeTable } from "../components/table.js";
import { msgLine } from "../components/drawer.js";
import { S } from "../page.js";
import { button } from "../components/button.js";

// ---- Inbox: messages for the person in "You are", with the answer each kind needs.
// inboxGen goes up after an action or a change of person, so every inbox row redraws (clears sent text).
let IB = [], inboxGen = 0;

export async function pollInbox() {
  const me = actor();
  $("#inboxPanel").classList.toggle("hidden", !me);
  if (!me) return;
  const r = await fetch(`api/inbox?agent=${encodeURIComponent(me)}${$("#inboxAll").checked ? "&all=1" : ""}`); if (!r.ok) return;
  IB = (await r.json()).messages;
  renderInbox();
}

// The inbox as a table. The Message column holds the whole message with its answer, reply and decline
// controls; on a narrow panel From, Kind, Item, To, Sent and State fold into a detail row.
let inboxTable = null;
// The messages that wait on the person: unread, or an open question or offer (the More line of the Board, #5083).
export function inboxOpenCount() { return IB.filter(m => !inboxDone(m)).length; }
function inboxDone(m) { return !(m.unread || (m.state === "open" && ["question", "offer"].includes(m.kind))); }
function inboxCell(m) {
  const me = actor();
  const open = m.state === "open", it = m.item_id ? S.items.find(i => i.id === m.item_id) : null;
  let acts = "";
  if (m.kind === "question" && open)
    acts = `<input id="ans-${m.id}" placeholder="your answer">` + button("Answer", { kind: "primary", data: { ib: "answer", msg: m.id } });
  else if (m.kind === "offer" && open) {
    const mine = it && it.assignee === me;
    acts = (mine ? button(`Give #${m.item_id} to ${esc(m.from_agent)}`, { kind: "primary", data: { ib: "give", msg: m.id, id: m.item_id, to: m.from_agent } })
        + `\n        <input id="split-${m.id}" placeholder="smaller pieces, separated by ;">` + button("Split", { data: { ib: "split", msg: m.id, id: m.item_id } }) : "")
      + `<input id="dec-${m.id}" placeholder="why not (optional)">` + button("Decline", { data: { ib: "decline", msg: m.id } });
  } else {
    const pushed = m.kind === "alert" && it && it.status === "open" && it.reserved_for === me && it.reserved_until;
    if (pushed) acts += button(`Accept #${it.id}`, { kind: "primary", data: { ib: "accept", id: it.id } })
      + button(`Decline #${it.id}`, { data: { ib: "decline-push", id: it.id } });
    else if (m.kind === "alert" && open) acts += `<input id="dec-${m.id}" placeholder="why not (optional)">` + button("Decline", { data: { ib: "decline", msg: m.id } });
    acts += `<input id="rep-${m.id}" placeholder="reply">` + button("Reply", { data: { ib: "reply", msg: m.id } });
    if (m.unread) acts += button("Mark read", { data: { ib: "read", msg: m.id } });
  }
  return `<div class="ny-h">${chip(m.kind === "alert" ? "c-p0" : "c-p", esc(m.kind))}<b>${esc(m.from_agent)}</b>
      ${m.item_id ? `<span class="link" data-open="${m.item_id}">#${m.item_id} ${esc(clip(m.item_title || "", 60))}</span>` : ""}
      <span class="muted t-s">${ago(m.created_at)} · #${m.id}${m.state !== "open" && m.state !== "read" ? " · " + esc(m.state) : ""}</span>
      <span class="link" data-thread="${m.id}">thread</span></div>
    <div class="body">${esc(m.body)}</div>
    <div class="actions">${acts}</div>
    <div class="thread hidden" id="thr-${m.id}"></div>`;
}
function renderInbox() {
  const me = actor();
  $("#inboxTitle").textContent = `Inbox for ${me}` + (IB.length ? ` (${IB.filter(m => m.unread || (["question", "offer"].includes(m.kind) && m.state === "open")).length})` : "");
  // Who a message can go to: people, and agents whose session has not ended (or that still hold or own something);
  // not every agent that ever registered (727 on a long-used queue, #4729).
  const reachable = a => a.kind === "human" || !["stopped", "gone"].includes(a.state) || a.holds.length || (a.owns || []).length;
  fillSelect("#sendTo", S.agents.filter(reachable).map(a => a.name).filter(n => n !== me), false);
  const ib = $("#inbox");
  if (ib.contains(document.activeElement) && document.activeElement.tagName === "INPUT") return;  // do not wipe what they type
  // maxHeight: the table scrolls inside itself, so in the one-column layout (narrow screens) the inbox no longer
  // makes the Board as long as all its messages (#4721); the column heads stay in sight.
  inboxTable ||= makeTable(ib, { key: "inbox", sort: [{ column: "created_at", dir: "desc" }], maxHeight: "70vh",
    placeholder: "No messages.", responsiveLayout: "collapse", responsiveLayoutCollapseStartOpen: false,
    rowFormatter: (row) => { row.getElement().classList.add("msg"); row.getElement().classList.toggle("read", inboxDone(row.getData().m)); },
    columns: [
      { formatter: "responsiveCollapse", width: 28, minWidth: 28, headerSort: false, filter: false, resizable: false },
      { title: "Message", field: "body", minWidth: 320, responsive: 0, cssClass: "wrap", html: (r) => inboxCell(r.m) },
      { title: "From", field: "from", width: 120, filter: "select", responsive: 1 },
      { title: "Kind", field: "kind", width: 90, filter: "select", responsive: 1 },
      { title: "Item", field: "item", width: 70, responsive: 2, html: (r) => r.m.item_id ? `<span class="link" data-open="${r.m.item_id}">#${r.m.item_id}</span>` : "" },
      { title: "To", field: "to", width: 110, filter: "select", responsive: 3 },
      { title: "Sent", field: "created_at", width: 84, filter: false, responsive: 2, html: (r) => ago(r.created_at) },
      { title: "State", field: "state", width: 90, filter: "select", responsive: 2 },
    ] });
  // A row changes only when its message, or the pushed item it offers, changes.
  inboxTable.set(IB.map(m => {
    const it = m.item_id ? S.items.find(i => i.id === m.item_id) : null;
    return { id: m.id, body: m.body, from: m.from_agent, kind: m.kind, item: m.item_id ? "#" + m.item_id : "", to: m.to_agent || "next holder",
      created_at: m.created_at, state: m.unread ? "unread" : m.state, m, gen: inboxGen,
      it: it && [it.status, it.assignee, it.reserved_for, it.reserved_until] };
  }));
}

async function inboxAction(t) {
  const what = t.dataset.ib, msg = +t.dataset.msg, id = +t.dataset.id, val = (p) => ($("#" + p + "-" + msg) || {}).value?.trim();
  try {
    if (what === "answer") { const body = val("ans"); if (!body) return toast("Write the answer first", true); await act("answer", { msg, body }); toast("Answered"); }
    if (what === "give") { await act("give", { id, to: t.dataset.to }); toast(`Gave #${id} to ${t.dataset.to}`); }
    if (what === "split") { const titles = (val("split") || "").split(";").map(x => x.trim()).filter(Boolean); if (!titles.length) return toast("Write the smaller pieces, separated by ;", true); await act("split", { id, titles }); toast(`Split #${id} into ${titles.length}`); }
    if (what === "decline") { await act("decline_message", { msg, note: val("dec") || undefined }); toast("Declined"); }
    if (what === "accept") { await act("accept", { id }); toast(`#${id} is yours`); }
    if (what === "decline-push") { await act("decline", { id }); toast(`Handed #${id} back`); }
    if (what === "reply") { const body = val("rep"); if (!body) return toast("Write the reply first", true); await act("send", { kind: "note", body, reply: msg }); toast("Sent"); }
    if (what === "read") await act("message_read", { msg });
  } catch (e) { /* toast shown */ }
  inboxGen++; document.activeElement && document.activeElement.blur(); await pollInbox();
}

async function toggleThread(msg) {
  const box = $("#thr-" + msg); if (!box) return;
  if (!box.classList.contains("hidden")) return box.classList.add("hidden");
  const r = await fetch(`api/thread/${msg}`); if (!r.ok) return;
  box.innerHTML = (await r.json()).messages.map(msgLine).join("");
  box.classList.remove("hidden");
  const row = inboxTable && inboxTable.tabulator.getRow(msg); if (row) row.normalizeHeight();
}

async function sendForm() {
  if (!actor()) return toast("Choose your name in 'You are' first", true);
  const body = $("#sendBody").value.trim(), item = $("#sendItem").value.replace("#", "").trim();
  if (!body) return toast("Write the message first", true);
  const args = { kind: $("#sendKind").value, body };
  if (item) args.item = +item; else args.to = $("#sendTo").value;
  try { await act("send", args); $("#sendBody").value = ""; $("#sendItem").value = ""; toast("Sent"); } catch (e) { /* toast shown */ }
}

// A click on this panel's controls; true when it was one (the frame then stops).
export function onClick(t) {
  if (t.dataset.ib) return run(() => inboxAction(t));
  if (t.dataset.thread) return run(() => toggleThread(+t.dataset.thread));
  if (t.id === "sendGo") return run(() => sendForm());
  return false;
}
const run = (f) => { f(); return true; };
// Another person in "You are": every row redraws, with that person's messages.
export function actorChanged() { inboxGen++; return pollInbox(); }

$("#inboxAll").addEventListener("change", () => { inboxGen++; pollInbox().catch(() => {}); });
