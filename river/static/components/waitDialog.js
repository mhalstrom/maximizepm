// What waits for the person in one session (#4607): the prompt its tmux pane shows (a permission prompt, a
// choice), and its open maxpm questions to a person. One block for each: a prompt with its options to pick and
// Other with a text box (sent by the CLI's own way to tell the agent what to do instead); a question with a box
// for the answer, which goes into the chat and closes the question (#4614). Dismiss closes the dialog and shows
// the chat.
// Server: the op prompt_answer (with the page's token: typeAction) and the op answer.
import { $, esc, act, toast, typeAction } from "../lib.js";
import { makeDialog } from "./dialog.js";
import { linkArtifacts } from "./artLinks.js";
import { openTerminal } from "./terminalDialog.js";

const dlg = makeDialog($("#waitDlg"));
let agent = null, pane = false, onSent = null;

function promptBlock(w, n) {
  if (w.elsewhere) {
    return `<div class="wblock"><div class="wkind">Prompt</div><div class="wq">${esc(w.question)}</div>
      <div class="muted">This session runs in no tmux pane here, so the page cannot answer it: use Open session, or its terminal.</div></div>`;
  }
  // The CLI's own option to type what to do instead (w.tell) is the Other choice with the text box: no second
  // Other under it (#5253).
  const own = Number.isInteger(w.tell) && w.tell < w.options.length;
  const opts = w.options.map((o, i) => `<label class="wopt"><input type="radio" name="pick${n}" value="${own && i === w.tell ? "other" : i}"${i === w.selected ? " checked" : ""}> <span>${esc(o)}</span></label>`).join("");
  return `<form class="wblock" data-kind="prompt" data-sig="${esc(w.sig)}"${own ? ` data-tell="${w.tell}"` : ""}>
    <div class="wkind">Prompt in its terminal</div>
    <div class="wq">${esc(w.question || w.tool || "The terminal waits for an answer")}</div>
    ${w.command ? `<div class="wcmd">${esc(w.command)}</div>` : ""}
    ${w.why ? `<div class="muted t-s">${esc(w.why)}</div>` : ""}
    <details class="wlines"${w.options.length ? "" : " open"}><summary>What the terminal shows</summary><pre>${esc((w.lines || []).join("\n"))}</pre></details>
    ${w.options.length ? opts : `<div class="muted">MaximizePM cannot read its choices (a slider or a list with no
      numbers): <span class="link" data-term="${esc(agent)}" tabindex="0" role="button">answer it in the terminal</span>.</div>`}
    ${own ? "" : `<label class="wopt"><input type="radio" name="pick${n}" value="other"${w.options.length ? "" : " checked"}> <span>Other: tell it what to do instead</span></label>`}
    <textarea class="wother" placeholder="What the agent does instead" aria-label="What the agent does instead"></textarea>
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
  return `<form class="wblock" data-kind="question" data-id="${w.id}">
    <div class="wkind">Question to ${esc(w.to)}${w.item_id ? ` · <span class="link" data-open="${w.item_id}">#${w.item_id}</span>` : ""}</div>
    <div class="wq">${esc(w.body)}</div>
    ${w.note ? `<div class="muted">The manager: ${esc(w.note)}</div>` : ""}
    <textarea class="wanswer" placeholder="Your answer" aria-label="Your answer"></textarea>
    <button class="btn primary wgo" type="submit">${pane ? "Send to the chat" : "Answer"}</button>
  </form>`;
}

// opts.onSent(text): the chat shows the person's turn at once.
// Close it: the page left the session (the You view of the first page, #5102), and it opens again only for something new.
export function closeWaiting() { dlg.close(); }

export function openWaiting(s, opts = {}) {
  agent = s.name; pane = !!s.terminal; onSent = opts.onSent || null;
  $("#waitTitle").textContent = `Waits for you: ${s.name}`;
  // With no pane, one note under the heading for every question, not the same note under each one (#5359).
  const note = !pane && s.waiting.some(w => w.kind === "question")
    ? `<div class="muted t-s wnote">This session runs in no tmux pane here, so an answer goes to the queue only; the session reads it with maxpm inbox.</div>`
    : "";
  $("#waitList").innerHTML = note + s.waiting.map((w, n) => w.kind === "question" ? questionBlock(w) : promptBlock(w, n)).join("");
  // Files and local pages in the question, the command and the terminal's text open in the side panel (#4689).
  $("#waitList").dataset.agent = s.name;
  $("#waitList").querySelectorAll(".wq, .wcmd, .wlines pre").forEach(x => linkArtifacts(x, s.name, { inPre: true }));
  dlg.open();
  const first = $("#waitList").querySelector("input:checked, textarea");
  if (first) first.focus();
}

function done(form) {
  form.remove();
  if (!$("#waitList").querySelector(".wblock")) dlg.close();
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
$("#waitList").addEventListener("click", (e) => {
  if (e.target.closest("[data-open]")) dlg.close();
  const t = e.target.closest("[data-term]");  // a prompt with no choices the page can read (#5220)
  if (t) { dlg.close(); openTerminal(t.dataset.term); }
});
$("#waitClose").onclick = () => dlg.close();
$("#waitDismiss").onclick = () => dlg.close();
