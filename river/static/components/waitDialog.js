// What waits for the person in one session (#4607): the prompt its tmux pane shows (a permission prompt, a
// choice), and its open maxpm questions to a person. One block for each: a prompt with its options to pick and
// Other with a text box (sent by the CLI's own way to tell the agent what to do instead); a question with a box
// for the answer, which goes into the chat and closes the question (#4614). Dismiss closes the dialog and shows
// the chat.
// Server: the op prompt_answer (with the page's token: typeAction) and the op answer.
import { $, esc, act, toast, typeAction } from "../lib.js";
import { makeDialog } from "./dialog.js";

const dlg = makeDialog($("#waitDlg"));
let agent = null, pane = false, onSent = null;

function promptBlock(w, n) {
  if (w.elsewhere) {
    return `<div class="wblock"><div class="wkind">Prompt</div><div class="wq">${esc(w.question)}</div>
      <div class="muted">This session runs in no tmux pane here, so the page cannot answer it: use Open session, or its terminal.</div></div>`;
  }
  const opts = w.options.map((o, i) => `<label class="wopt"><input type="radio" name="pick${n}" value="${i}"${i === w.selected ? " checked" : ""}> <span>${esc(o)}</span></label>`).join("");
  return `<form class="wblock" data-kind="prompt" data-sig="${esc(w.sig)}">
    <div class="wkind">Prompt in its terminal</div>
    <div class="wq">${esc(w.question || w.tool || "The terminal waits for an answer")}</div>
    ${w.command ? `<div class="wcmd">${esc(w.command)}</div>` : ""}
    ${w.why ? `<div class="muted" style="font-size:12px">${esc(w.why)}</div>` : ""}
    <details class="wlines"><summary>What the terminal shows</summary><pre>${esc((w.lines || []).join("\n"))}</pre></details>
    ${opts}
    <label class="wopt"><input type="radio" name="pick${n}" value="other"${w.options.length ? "" : " checked"}> <span>Other: tell it what to do instead</span></label>
    <textarea class="wother" placeholder="What the agent does instead" aria-label="What the agent does instead"></textarea>
    <button class="btn primary wgo" type="submit">Send answer</button>
  </form>`;
}

// A question: the answer goes into the session's chat (its pane) as the person's turn, and closes the question in
// the queue with the same text (#4614). With no pane, only the queue gets it.
function questionBlock(w) {
  return `<form class="wblock" data-kind="question" data-id="${w.id}">
    <div class="wkind">Question to ${esc(w.to)}${w.item_id ? ` · <span class="link" data-open="${w.item_id}">#${w.item_id}</span>` : ""}</div>
    <div class="wq">${esc(w.body)}</div>
    <textarea class="wanswer" placeholder="Your answer" aria-label="Your answer"></textarea>
    ${pane ? "" : `<div class="muted" style="font-size:12px">This session runs in no tmux pane here, so the answer goes to the queue only; the session reads it with maxpm inbox.</div>`}
    <button class="btn primary wgo" type="submit">${pane ? "Send to the chat" : "Answer"}</button>
  </form>`;
}

// opts.onSent(text): the chat shows the person's turn at once.
export function openWaiting(s, opts = {}) {
  agent = s.name; pane = !!s.terminal; onSent = opts.onSent || null;
  $("#waitTitle").textContent = `Waits for you: ${s.name}`;
  $("#waitList").innerHTML = s.waiting.map((w, n) => w.kind === "question" ? questionBlock(w) : promptBlock(w, n)).join("");
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
  const form = e.target, btn = form.querySelector(".wgo");
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
      if (pick.value === "other" && !other) { toast("Write what the agent does instead", true); return; }
      const r = await typeAction("prompt_answer", { agent, sig: form.dataset.sig, ...(other ? { other } : { pick: +pick.value }) });
      toast(`Sent: ${r.text || r.picked}`);
      if (onSent) onSent(r.text || r.picked);
    }
    done(form);
  } catch (err) { toast(err.message, true); }
  finally { btn.disabled = false; }
});
$("#waitList").addEventListener("click", (e) => { if (e.target.closest("[data-open]")) dlg.close(); });
$("#waitClose").onclick = () => dlg.close();
$("#waitDismiss").onclick = () => dlg.close();
$("#waitDlg").addEventListener("click", (e) => { if (e.target === e.currentTarget) dlg.close(); });
