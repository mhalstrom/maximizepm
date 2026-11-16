// What waits for the person in one session (#4607): the prompt its tmux pane shows (a permission prompt, a
// choice), and its open maxpm questions to a person. One block for each: a prompt with its options to pick and
// Other with a text box (sent by the CLI's own way to tell the agent what to do instead); a question with a box
// for the answer, which goes into the chat and closes the question (#4614). Dismiss closes the dialog and shows
// the chat.
// Server: the op prompt_answer (with the page's token: typeAction) and the op answer.
// #6002: it is also the one card dialog. A question block is the card (askCard.js); a person item gets the same card;
// a line of Needs you or a person task in Plan > Items opens the dialog with that one card (openCard).
import { $, esc, act, toast, typeAction, actor, getJson } from "../lib.js";
import { makeDialog } from "./dialog.js";
import { linkArtifacts } from "./artLinks.js";
import { openTerminal } from "./terminalDialog.js";
import { cardHtml, sideHtml, loadSide, fromWaiting, fromItem, fromEvent, typeChip, discussText, loadDiffs, loadTryFrames } from "./askCard.js";
import { openFullScreen } from "./sidePanel.js";
import { S, nav } from "../page.js";
import { openOnRqs } from "./rqsPublish.js";
import { button } from "./button.js";

let queue = [], qAt = 0;  // the new cards that open by themselves, and the one shown (#6017)
const dlg = makeDialog($("#waitDlg"), { onClose: () => { queue = []; } });  // a close ends the queue of new cards (#6017)
let agent = null, pane = false, onSent = null;
const cards = new Map();  // "kind:id" -> the card, for its side column and Discuss

function promptBlock(w, n) {
  if (w.elsewhere) {
    return `<div class="wblock"><div class="wkind">Prompt</div><div class="wq">${esc(w.question)}</div>
      <div class="muted">This session runs in no tmux pane here, so the page cannot answer it: use Open session, or its terminal.</div></div>`;
  }
  // The CLI's own option to type what to do instead (w.tell) is the Other choice with the text box: no second
  // Other under it (#5253).
  const own = Number.isInteger(w.tell) && w.tell < w.options.length;
  // A question that takes only one of its options (a trust question: its Esc ends the CLI) has no Other (#5904).
  const free = !w.fixed;
  const opts = w.options.map((o, i) => `<label class="wopt"><input type="radio" name="pick${n}" value="${own && i === w.tell ? "other" : i}"${i === w.selected ? " checked" : ""}> <span>${esc(o)}</span></label>`).join("");
  return `<form class="wblock" data-kind="prompt" data-sig="${esc(w.sig)}"${own ? ` data-tell="${w.tell}"` : ""}>
    <div class="wkind">${typeChip("approve")} Prompt in its terminal</div>
    <div class="wq">${esc(w.question || w.tool || "The terminal waits for an answer")}</div>
    ${w.command ? `<div class="wcmd">${esc(w.command)}</div>` : ""}
    ${w.why ? `<div class="muted t-s">${esc(w.why)}</div>` : ""}
    <details class="wlines"${w.options.length ? "" : " open"}><summary>What the terminal shows</summary><pre>${esc((w.lines || []).join("\n"))}</pre></details>
    ${w.options.length ? opts : `<div class="muted">MaximizePM cannot read its choices (a slider or a list with no
      numbers): <span class="link" data-term="${esc(agent)}" tabindex="0" role="button">answer it in the terminal</span>.</div>`}
    ${own || !free ? "" : `<label class="wopt"><input type="radio" name="pick${n}" value="other"${w.options.length ? "" : " checked"}> <span>Other: tell it what to do instead</span></label>`}
    ${free ? `<textarea class="wother" placeholder="What the agent does instead" aria-label="What the agent does instead"></textarea>` : ""}
    ${keyButtons(w)}
  </form>`;
}

// A menu whose key line names more keys than Enter (#5244: Claude Code's /model, 'Enter to set as default · s to use
// this session only'): one send button for each, which chooses the picked option with that key. The first key that is
// not Enter is the main one: Enter there saves the choice as the default of every new session.
function keyButtons(w) {
  const keys = w.keys || [];
  if (!keys.length) return `<button class="btn primary wgo" type="submit">Send answer</button>`;
  const main = keys.findIndex(k => k.key !== "Enter");
  return `<div class="wkeys">${keys.map((k, i) => `<button class="btn${i === main ? " primary" : ""} wgo" type="submit"
    data-key="${esc(k.key)}" title="Chooses the picked option with the key ${esc(k.key)}">${esc(k.label)} (${esc(k.key)})</button>`).join("")}</div>`;
}

// A question: the answer goes into the session's chat (its pane) as the person's turn, and closes the question in
// the queue with the same text (#4614). With no pane, only the queue gets it.
function questionBlock(w) {
  const c = fromWaiting(w, agent);
  cards.set(`question:${c.id}`, c);
  return cardHtml(c, { pane });
}

// The comments and history of each card in the list, read after it shows.
function fillSides() {
  $("#waitList").querySelectorAll("[data-side]").forEach(async (box) => {
    const c = cards.get(box.dataset.side);
    if (!c) return;
    try { box.innerHTML = sideHtml(await loadSide(c)); }
    catch (e) { box.innerHTML = sideHtml({ comments: [], history: [] }); }
  });
}

// opts.onSent(text): the chat shows the person's turn at once.
// Close it: the page left the session (the You view of the first page, #5102), and it opens again only for something new.
export function closeWaiting() { dlg.close(); }

export function openWaiting(s, opts = {}) {
  queue = [];
  agent = s.name; pane = !!s.terminal; onSent = opts.onSent || null;
  $("#waitTitle").textContent = `Waits for you: ${s.name}`;
  // With no pane, one note under the heading for every question, not the same note under each one (#5359).
  const note = !pane && s.waiting.some(w => w.kind === "question")
    ? `<div class="muted t-s wnote">This session runs in no tmux pane here, so an answer goes to the queue only; the session reads it with maxpm inbox.</div>`
    : "";
  cards.clear();
  $("#waitList").innerHTML = note + s.waiting.map((w, n) => w.kind === "question" ? questionBlock(w) : promptBlock(w, n)).join("");
  // Files and local pages in the question, the command and the terminal's text open in the side panel (#4689).
  $("#waitList").dataset.agent = s.name;
  $("#waitList").querySelectorAll(".wq, .wcmd, .wlines pre").forEach(x => linkArtifacts(x, s.name, { inPre: true }));
  shown();
}

// One card, from a line of Needs you or a person task in Plan > Items (askCard.js fromEvent, fromItem). A question's
// answer goes to the queue; the session reads it as it does today.
export function openCard(c) { queue = []; drawCard(c); }  // a card the person opened: no queue of new cards
function drawCard(c) {
  agent = c.kind === "question" ? c.from : null; pane = false; onSent = null;
  $("#waitTitle").textContent = c.kind === "item" ? "Waits for you" : `Waits for you: ${c.from === "maxpm" ? "MaximizePM" : c.from}`;
  cards.clear();
  cards.set(`${c.kind}:${c.id}`, c);
  $("#waitList").innerHTML = cardHtml(c);
  $("#waitList").dataset.agent = c.from || "";
  $("#waitList").querySelectorAll(".wq, .acard-d").forEach(x => linkArtifacts(x, c.from || null, { inPre: true }));
  shown();
}

// A person item by its id (Plan > Items): its card, from GET api/item/<id>.
export async function openItemCard(id) {
  try { openCard(fromItem(await getJson(`api/item/${id}`))); }
  catch (e) { toast(e.message, true); }
}

// New cards that open by themselves (#6017, you-needs.js autoCards): one card at a time, with "1 of N" and Next;
// after an answer or a Done the next one shows. Each entry: a Needs you event. Close ends the queue; the cards stay in
// Needs you.
export function isOpen() { return !$("#waitDlg").classList.contains("hidden"); }
export async function openCardQueue(events) {
  queue = events.slice(); qAt = 0;
  return showQueued();
}
async function showQueued() {
  while (qAt < queue.length) {
    const e = queue[qAt];
    try {
      const c = e.kind === "item" ? fromItem(await getJson(`api/item/${e.item_id}`)) : fromEvent(e);
      drawCard(c);
      if (queue.length > 1) {
        const line = document.createElement("div");
        line.className = "wqueue muted t-s";
        line.innerHTML = `New for you: ${qAt + 1} of ${queue.length}` + (qAt + 1 < queue.length
          ? button("Next", { data: { wnext: 1 }, title: "The next new card" }) : "");
        $("#waitList").prepend(line);
      }
      return true;
    } catch (err) { qAt++; }  // gone meanwhile: the next one
  }
  queue = []; return false;
}

function shown() {
  $("#waitDlg").classList.toggle("has-cards", cards.size > 0);
  fillSides();
  loadDiffs($("#waitList"));  // a commit's diff on a card (#5956)
  loadTryFrames($("#waitList"));  // the canvas of each try (#6040)
  dlg.open();
  const first = $("#waitList").querySelector("input:checked, textarea");
  if (first) first.focus();
}

function done(form) {
  form.remove();
  if ($("#waitList").querySelector(".wblock")) return;
  if (queue.length && qAt + 1 < queue.length) { qAt++; return showQueued(); }  // the next new card (#6017)
  queue = [];
  dlg.close();
}

$("#waitList").addEventListener("input", (e) => {  // a word in Other picks Other
  if (!e.target.matches(".wother")) return;
  const other = e.target.closest("form").querySelector('input[value="other"]');
  if (other && e.target.value.trim()) other.checked = true;
});
$("#waitList").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target, btn = e.submitter && e.submitter.matches(".wgo") ? e.submitter : form.querySelector(".wgo");
  if (btn.disabled) return;
  btn.disabled = true;
  try {
    if (form.dataset.kind === "question") {
      const body = form.querySelector(".wanswer").value.trim();
      if (!body) { toast("Write the answer first", true); return; }
      let typed = false;
      if (pane) {
        try { const r = await typeAction("session_send", { agent, text: body }); typed = true; if (onSent) onSent(r.text, true); }
        catch (err) { toast(`Not typed into the session (${err.message}); the queue still gets the answer`, true); }
      }
      await act("answer", { msg: +form.dataset.id, body });
      toast(typed ? "Sent to the chat, and the question is answered" : "Answered in the queue");
    } else if (form.dataset.kind === "item") {  // a person item: done, with what the person decided or did
      if (!actor()) { toast("Choose your name in 'You are' first", true); return; }
      const out = form.querySelector(".wanswer").value.trim();
      await act("done", { id: +form.dataset.id, output: out || undefined });
      toast(`#${form.dataset.id} is done`);
    } else {
      const pick = form.querySelector("input[type=radio]:checked");
      if (!pick) { toast("Pick an option", true); return; }
      const other = pick.value === "other" ? form.querySelector(".wother").value.trim() : "";
      // The CLI's own Other with no text: pick it, and the CLI asks for the text in its terminal.
      const own = pick.value === "other" && !other && form.dataset.tell != null;
      if (pick.value === "other" && !other && !own) { toast("Write what the agent does instead", true); return; }
      const r = await typeAction("prompt_answer", { agent, sig: form.dataset.sig,
        ...(other ? { other } : { pick: own ? +form.dataset.tell : +pick.value, ...(btn.dataset.key ? { key: btn.dataset.key } : {}) }) });
      toast(`Sent: ${r.text || r.picked}`);
      if (onSent) onSent(r.text || r.picked);
    }
    done(form);
  } catch (err) { toast(err.message, true); }
  finally { btn.disabled = false; }
});
$("#waitList").addEventListener("click", async (e) => {
  if (e.target.closest("[data-wnext]")) { qAt++; return showQueued(); }
  const ch = e.target.closest("[data-wchanges]");
  if (ch) {  // a release sign-off: the person says no; the review runs again after a fix with these words (#6039)
    const form = ch.closest("form"), note = form.querySelector(".wanswer").value.trim();
    if (!note) { toast("Write what must change", true); form.querySelector(".wanswer").focus(); return; }
    try {
      const r = await act("release_changes", { item: +form.dataset.id, note });
      toast(`Changes asked: fix #${r.fix}; the review runs again`);
      done(form);
    } catch (err) { /* act showed it */ }
    return;
  }
  if (e.target.closest("[data-open]")) dlg.close();
  const tryOpen = e.target.closest("[data-try-open]");  // a try's canvas in a tab of its own: the card stays (#6040)
  if (tryOpen) {
    const d = tryOpen.dataset;
    return openFullScreen("canvas:" + d.tryOpen, d.trySession || "", d.tryItem || "");
  }
  const opt = e.target.closest("[data-opt]");  // an option of the decision form: it is the answer
  if (opt) {
    const form = opt.closest("form");
    form.querySelector(".wanswer").value = opt.dataset.opt;
    form.requestSubmit(form.querySelector(".wgo"));
    return;
  }
  if (e.target.closest("[data-share]")) {  // a private page of the card, as Share artifact of a canvas (#6006)
    const form = e.target.closest("form"), c = form && cards.get(`${form.dataset.kind}:${form.dataset.id}`);
    if (!c) return;
    try {
      const r = await act("card_share", { kind: c.kind, id: c.id });
      await openOnRqs(r.canvas, { page: r.page }, r.version);
      fillSides();
    } catch (err) { /* act showed the error */ }
    return;
  }
  if (e.target.closest("[data-discuss]")) {  // the card goes into the manager's chat, and its chat opens
    const form = e.target.closest("form"), c = form && cards.get(`${form.dataset.kind}:${form.dataset.id}`);
    const mgr = ((S && S.agents) || []).find(a => a.role === "manager" && !["gone", "stopped"].includes(a.state));
    if (!c || !mgr) { toast("No manager runs now", true); return; }
    try {
      await act("send", { kind: "note", body: discussText(c), to: mgr.name, item: c.item_id || undefined });
      dlg.close();
      nav.openSession(mgr.name);
    } catch (err) { /* act showed the error */ }
    return;
  }
  const t = e.target.closest("[data-term]");  // a prompt with no choices the page can read (#5220)
  if (t) { dlg.close(); openTerminal(t.dataset.term); }
});
$("#waitClose").onclick = () => dlg.close();
$("#waitDismiss").onclick = () => dlg.close();
