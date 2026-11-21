// What waits for the person in one session (#4607): the prompt its tmux pane shows (a permission prompt, a
// choice), and its open maxpm questions to a person. One block for each: a prompt with its options to pick and
// Other with a text box (sent by the CLI's own way to tell the agent what to do instead); a question with a box
// for the answer, which goes into the chat and closes the question (#4614). Dismiss closes the dialog and shows
// the chat.
// Server: the op prompt_answer (with the page's token: typeAction) and the op answer.
// #6002: it is also the one card dialog. A question block is the card (askCard.js); a person item gets the same card;
// a line of Needs you or a person task in Plan > Items opens the dialog with that one card (openCard).
// #6438 (mark 2026-11-20: 'it also should not take over the screen. when these open for a session they should display
// when we go to that specific session'): what waits in a session shows in that session's view, in the panel #sessAsk
// above its message box (openWaiting with dock), not over the page. Nothing opens by itself over another tab. The one
// list (#waitList) moves between that panel and the dialog's box, so the same handlers answer in both.
import { $, esc, act, toast, typeAction, actor, getJson, hooks } from "../lib.js";
import { makeDialog } from "./dialog.js";
import { linkArtifacts } from "./artLinks.js";
import { openTerminal } from "./terminalDialog.js";
import { cardHtml, sideHtml, loadSide, fromWaiting, fromItem, typeChip, discussText, loadDiffs, loadTryFrames } from "./askCard.js";
import { openFullScreen } from "./sidePanel.js";
import { S, nav } from "../page.js";
import { openOnRqs } from "./rqsPublish.js";
import { button } from "./button.js";

const dlg = makeDialog($("#waitDlg"));
let agent = null, pane = false, onSent = null, onAsk = null;
let docked = false;  // the list is in the session view's panel (#6438), not in the dialog
const dockEl = () => document.getElementById("sessAsk");
// The list goes into the session's panel, or back into the dialog's box under its head.
function place(inPanel) {
  docked = inPanel && !!dockEl();
  if (docked) $("#sessAskList").append($("#waitList"));
  else $("#waitTitle").parentElement.after($("#waitList"));
}
function setTitle(text) { $(docked ? "#sessAskTitle" : "#waitTitle").textContent = text; }
// Hide the session's panel; the session view's line 'Answer it' shows it again.
export function hideDocked() { const d = dockEl(); if (d) d.classList.add("hidden"); }
export function dockedShows() { const d = dockEl(); return !!d && docked && !d.classList.contains("hidden"); }
function closeAll() { if (docked) hideDocked(); else dlg.close(); }
const cards = new Map();  // "kind:id" -> the card, for its side column and Discuss

// The CLI's own question dialog (Claude Code's AskUserQuestion, #6234), read from its pane: one block for the
// question that the terminal shows now, with its tabs (each question, then Submit), its options and their
// descriptions, the CLI's own text option ('Type something'), and 'Chat about this'. A question with boxes takes its
// ticks, then Next; every other question takes one option. The answer goes in as the CLI's keys, and the terminal
// shows the next question: the page reads it again at once, and this dialog opens with it.
function askBlock(w, n) {
  const a = w.ask, opts = a.options.map((o, i) => ({ ...o, i }));
  const plain = opts.filter(o => !o.text && !o.chat), box = opts.find(o => o.text), chat = opts.find(o => o.chat);
  const multi = a.multi, last = a.tabs[a.tabs.length - 1].submit && a.current === a.tabs.length - 2;
  const tabs = a.tabs.length > 1 ? `<div class="wtabs" role="group" aria-label="Questions">${a.tabs.map((t, i) =>
    button(`${t.submit ? "✔" : t.done ? "☒" : "☐"} ${esc(t.header)}`, { cls: "wtab", pressed: i === a.current,
      disabled: a.current == null || i === a.current, data: { wtab: i }, title: t.submit ? "Review and send" : "Show this question" })).join("")}</div>` : "";
  const desc = (o) => o.detail ? `<div class="muted t-s">${esc(o.detail)}</div>` : "";
  const mark = (o) => o.chosen ? ` <span class="muted t-s">(your answer)</span>` : "";
  const sel = (opts.find(o => o.chosen) || { i: w.selected }).i;  // the answer given before, else the marked option
  const input = (o, val) => multi
    ? `<input type="checkbox" value="${val}"${o.checked ? " checked" : ""}>`
    : `<input type="radio" name="pick${n}" value="${val}"${o.i === sel ? " checked" : ""}>`;
  const list = plain.map(o => `<label class="wopt">${input(o, o.i)} <span>${esc(o.label)}${mark(o)}${desc(o)}</span></label>`).join("");
  const own = box ? `<label class="wopt">${input(box, "text")} <span>Your own answer</span></label>
    <textarea class="wother" placeholder="Your answer" aria-label="Your own answer">${esc(box.typed || "")}</textarea>` : "";
  const talk = chat ? (multi ? button(esc(chat.label), { cls: "wchat", data: { wchat: chat.i }, title: "Decline and talk instead" })
    : `<label class="wopt"><input type="radio" name="pick${n}" value="${chat.i}"${chat.i === sel ? " checked" : ""}> <span>${esc(chat.label)}<div class="muted t-s">The agent gets no answer and asks what to clarify</div></span></label>`) : "";
  const review = (a.review || []).length ? `<ul class="wreview">${a.review.map(r =>
    `<li><span>${esc(r.q)}</span> <b>${esc(r.a || "no answer")}</b></li>`).join("")}</ul>` : "";
  const preview = (a.preview || []).length ? `<div class="muted t-s">The terminal shows the preview of the marked option:</div><pre class="wcmd wpreview">${esc(a.preview.join("\n"))}</pre>` : "";
  const go = !multi || a.tabs.length < 2 ? "Send answer" : last ? "Next: review" : "Next question";
  return `<form class="wblock" data-kind="ask" data-sig="${esc(w.sig)}" data-multi="${multi ? 1 : ""}">
    <div class="wkind">${typeChip("decide")} Question in its terminal</div>
    ${tabs}
    <div class="wq">${esc(a.question || "The terminal asks a question")}</div>
    ${review}${a.warn ? `<div class="muted t-s">${esc(a.warn)}</div>` : ""}
    ${list}${own}${preview}${talk}
    <details class="wlines"><summary>What the terminal shows</summary><pre>${esc((w.lines || []).join("\n"))}</pre></details>
    ${button(go, { kind: "primary", cls: "wgo", type: "submit" })}
  </form>`;
}

// The keys went in: read the pane again when the CLI has drawn the next question, and open it (opts.onAsk of
// openWaiting: sessions.js), also a question that the person saw before.
function askAgain() { const f = onAsk; setTimeout(() => f ? f() : hooks.refresh(), 300); }

function promptBlock(w, n) {
  if (w.ask) return askBlock(w, n);
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
    ${free ? `<textarea class="wother" placeholder="What the agent does instead" aria-label="Your other instruction"></textarea>` : ""}
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
    data-key="${esc(k.key)}" title="Send with ${esc(k.key)}">${esc(k.label)} (${esc(k.key)})</button>`).join("")}</div>`;
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
export function closeWaiting() { hideDocked(); dlg.close(); }

// opts.dock: in the session view's panel (#6438), not as a dialog; opts.focus: false when it shows without a click,
// so it takes no keys from the message box.
export function openWaiting(s, opts = {}) {
  if (opts.dock) dlg.close();
  place(!!opts.dock);
  agent = s.name; pane = !!s.terminal; onSent = opts.onSent || null; onAsk = opts.onAsk || null;
  setTitle(docked ? "Waits for you" : `Waits for you: ${s.name}`);
  // With no pane, one note under the heading for every question, not the same note under each one (#5359).
  const note = !pane && s.waiting.some(w => w.kind === "question")
    ? `<div class="muted t-s wnote">This session runs in no tmux pane here, so an answer goes to the queue only; the session reads it with maxpm inbox.</div>`
    : "";
  cards.clear();
  // opts.extra: the cards of the decisions the session put to the person as the person (#6436, decision.by). Their
  // answer goes to the queue only, which tells the session; nothing is typed into its chat (data-nopane).
  const extra = (opts.extra || []).map(c => { cards.set(`${c.kind}:${c.id}`, c); return cardHtml(c); }).join("");
  $("#waitList").innerHTML = note + s.waiting.map((w, n) => w.kind === "question" ? questionBlock(w) : promptBlock(w, n)).join("") + extra;
  for (const c of opts.extra || []) {
    const f = $("#waitList").querySelector(`form[data-kind="${c.kind}"][data-id="${c.id}"]`);
    if (f) f.dataset.nopane = "1";
  }
  // Files and local pages in the question, the command and the terminal's text open in the side panel (#4689).
  $("#waitList").dataset.agent = s.name;
  $("#waitList").querySelectorAll(".wq, .wcmd, .wlines pre").forEach(x => linkArtifacts(x, s.name, { inPre: true }));
  shown(opts.focus !== false);
}

// One card, from a line of Needs you or a person task in Plan > Items (askCard.js fromEvent, fromItem). A question's
// answer goes to the queue; the session reads it as it does today. Only a click opens it.
export function openCard(c) {
  hideDocked(); place(false);
  agent = c.kind === "question" ? c.from : null; pane = false; onSent = null; onAsk = null;
  setTitle(c.kind === "item" ? "Waits for you" : `Waits for you: ${c.from === "maxpm" ? "MaximizePM" : c.from}`);
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

function shown(focus = true) {
  $("#waitDlg").classList.toggle("has-cards", cards.size > 0);
  fillSides();
  loadDiffs($("#waitList"));  // a commit's diff on a card (#5956)
  loadTryFrames($("#waitList"));  // the canvas of each try (#6040)
  if (docked) dockEl().classList.remove("hidden");
  else dlg.open();
  const first = focus && $("#waitList").querySelector(".acard-choice input, input:checked, textarea");  // the first option (#6413)
  if (first) first.focus({ preventScroll: docked });
}

function done(form) {
  form.remove();
  if ($("#waitList").querySelector(".wblock")) return;
  closeAll();
}

$("#waitList").addEventListener("input", (e) => {  // a word in Other picks Other
  if (!e.target.matches(".wother")) return;
  const other = e.target.closest("form").querySelector('input[value="other"], input[value="text"]');
  if (other && e.target.value.trim()) other.checked = true;
});
$("#waitList").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target, btn = e.submitter && e.submitter.matches(".wgo") ? e.submitter : form.querySelector(".wgo");
  if (btn.disabled) return;
  btn.disabled = true;
  try {
    // The option picked from the manager's escalation (#6413): it goes as a field, the box's words with it.
    const picked = form.querySelector(".acard-choice input:checked");
    const choice = picked ? picked.value : undefined;
    if (form.dataset.kind === "question") {
      const body = form.querySelector(".wanswer").value.trim();
      if (!body && !choice) { toast(form.querySelector(".acard-choice") ? "Pick an option or write your answer" : "Write the answer first", true); return; }
      const said = choice ? (body ? `${choice}: ${body}` : choice) : body;
      let typed = false;
      if (pane && !form.dataset.nopane) {
        try { const r = await typeAction("session_send", { agent, text: said }); typed = true; if (onSent) onSent(r.text, true); }
        catch (err) { toast(`Not typed into the session (${err.message}); the queue still gets the answer`, true); }
      }
      await act("answer", { msg: +form.dataset.id, body, choice });
      toast(typed ? "Sent to the chat, and the question is answered" : "Answered in the queue");
    } else if (form.dataset.kind === "item") {  // a person item: done, with what the person decided or did
      if (!actor()) { toast("Choose your name in 'You are' first", true); return; }
      const out = form.querySelector(".wanswer").value.trim();
      if (!out && !choice && form.querySelector(".acard-choice")) { toast("Pick an option or write your answer", true); return; }
      const r = await act("done", { id: +form.dataset.id, output: out || undefined, choice });
      if (r && r.kept_open) toast(`#${form.dataset.id} postponed (kept open)` + (choice ? `: ${choice}` : ""));
      else toast(`#${form.dataset.id} is done` + (choice ? `: ${choice}` : ""));
    } else if (form.dataset.kind === "ask") {  // the CLI's question dialog (#6234)
      const text = (form.querySelector(".wother") || {}).value || "";
      const own = form.querySelector('input[value="text"]');
      const args = { agent, sig: form.dataset.sig };
      if (form.dataset.multi) {
        args.picks = [...form.querySelectorAll('input[type=checkbox]:checked')].filter(x => x.value !== "text").map(x => +x.value);
        if (own && own.checked && text.trim()) args.other = text.trim();
      } else {
        const pick = form.querySelector("input[type=radio]:checked");
        if (!pick) { toast("Pick an option", true); return; }
        if (pick.value === "text") {
          if (!text.trim()) { toast("Write your answer first", true); return; }
          args.other = text.trim();
        } else args.pick = +pick.value;
      }
      const r = await typeAction("prompt_answer", args);
      toast(`Sent: ${r.picked}`);
      askAgain();
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
// The keys of a card (#6413): the arrows move between its options (the browser's own radio group), Enter on an
// option sends, and Cmd or Ctrl+Enter sends from the box. Esc closes the dialog (dialog.js).
$("#waitList").addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || e.isComposing) return;
  const onPick = e.target.matches(".acard-choice input");
  if (!onPick && !(e.target.matches(".wanswer") && (e.metaKey || e.ctrlKey))) return;
  const form = e.target.closest("form"), go = form && form.querySelector(".wgo");
  if (!go) return;
  e.preventDefault();
  if (onPick) e.target.checked = true;
  form.requestSubmit(go);
});
$("#waitList").addEventListener("click", async (e) => {
  if (e.target.closest("[data-wcancel]")) { closeAll(); return; }
  const move = e.target.closest("[data-wtab], [data-wchat]");  // another question of the dialog, or Chat about this
  if (move) {
    const form = move.closest("form"), d = move.dataset;
    try {
      const r = await typeAction("prompt_answer", { agent, sig: form.dataset.sig,
        ...(d.wtab != null ? { tab: +d.wtab } : { pick: +d.wchat }) });
      if (d.wchat != null) toast(`Sent: ${r.picked}`);
      done(form);
      askAgain();
    } catch (err) { toast(err.message, true); }
    return;
  }
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
  const later = e.target.closest("[data-wlater]");
  if (later) {  // postpone a person item: keep it open and blocked (#6526)
    if (!actor()) { toast("Choose your name in 'You are' first", true); return; }
    const form = later.closest("form"), out = form.querySelector(".wanswer").value.trim();
    try {
      await act("item_reply", { id: +form.dataset.id, body: out || undefined, postpone: true });
      toast(`#${form.dataset.id} postponed (kept open)`);
      done(form);
    } catch (err) { /* act showed it */ }
    return;
  }
  const reply = e.target.closest("[data-wreply]");
  if (reply) {  // reply on a person item without closing it (#6526)
    if (!actor()) { toast("Choose your name in 'You are' first", true); return; }
    const form = reply.closest("form"), out = form.querySelector(".wanswer").value.trim();
    const picked = form.querySelector(".acard-choice input:checked");
    const choice = picked ? picked.value : undefined;
    if (!out && !choice) { toast("Write your reply first", true); form.querySelector(".wanswer").focus(); return; }
    try {
      await act("item_reply", { id: +form.dataset.id, body: out || undefined, choice, postpone: false });
      toast(`Reply saved on #${form.dataset.id} (kept open)`);
      done(form);
    } catch (err) { /* act showed it */ }
    return;
  }
  if (e.target.closest("[data-open]") && !docked) dlg.close();  // the session's panel stays under the drawer
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
      closeAll();
      nav.openSession(mgr.name);
    } catch (err) { /* act showed the error */ }
    return;
  }
  const t = e.target.closest("[data-term]");  // a prompt with no choices the page can read (#5220)
  if (t) { if (!docked) dlg.close(); openTerminal(t.dataset.term); }
});
$("#waitClose").onclick = () => dlg.close();
$("#waitDismiss").onclick = () => dlg.close();
if (dockEl()) $("#sessAskClose").onclick = hideDocked;  // the session's panel (#6438, views/sessions.html)
