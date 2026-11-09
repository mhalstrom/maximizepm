// The comments of a canvas (#4769), under the page in its side panel tab (mark's answer 4 of #4650: on a part of the
// page or on the whole page, with a signal to the session).
// - Comment: a mode in which a click picks a part of the page (the helper script in the frame says its CSS path, text
//   and selected text; a Markdown canvas is drawn here, so this file finds the part itself). Comment on the page: the
//   whole page. A box opens; its text stays in this browser while the person writes (a reload keeps it).
// - Save as draft, or Send now. Send all drafts sends every draft of the person as one message to the session that
//   wrote the canvas; when that session has ended, a fresh session takes the canvas's item (core.canvas_send).
// - The threads: each comment with its part (Show scrolls there), its state (draft, sent, read, answered,
//   resolved), its replies, and Reply and Resolve.
import { esc, ago, clip, store, toast, act, actor } from "../lib.js";

const KEEP = "river.canvas.draft.";

// The state a person reads: a sent comment with a reply from someone else is answered.
export function stateOf(c) {
  if (c.state === "resolved" || c.state === "draft") return c.state;
  return (c.replies || []).some(r => r.author !== c.author) ? "answered" : c.state;
}

// A short CSS path of el inside root (a Markdown canvas drawn in the panel); the same form as the helper script's.
export function pathIn(el, root) {
  const parts = [];
  while (el && el !== root && el.nodeType === 1 && parts.length < 8) {
    if (el.id) { parts.unshift("#" + CSS.escape(el.id)); break; }
    let i = 1, s = el;
    while ((s = s.previousElementSibling)) if (s.tagName === el.tagName) i++;
    parts.unshift(el.tagName.toLowerCase() + ":nth-of-type(" + i + ")");
    el = el.parentElement;
  }
  return parts.join(" > ");
}

const partText = (a) => !a ? "the whole page"
  : a.quote ? `“${clip(a.quote, 80)}”` : a.text ? `“${clip(a.text, 80)}”` : a.path || "a part";

// box: the element that holds the threads. ref: "c<id>". reload(): fetch the canvas's meta again and render.
// show(path): scroll the page to a part. Returns { render(meta), pick(anchor), compose(anchor) }.
export function makeComments(box, { id, ref, reload, show }) {
  let meta = null, sig = "";
  const kept = () => { try { return JSON.parse(store(KEEP + id) || "null"); } catch (e) { return null; } };
  const keep = (v) => store(KEEP + id, v ? JSON.stringify(v) : "");

  function compose(anchor) {
    const old = kept();
    keep({ anchor: anchor || null, text: old && JSON.stringify(old.anchor) === JSON.stringify(anchor || null) ? old.text : "" });
    sig = ""; draw();
    const ta = box.querySelector(".cv-compose textarea"); if (ta) ta.focus();
  }

  function draw() {
    if (!meta) return;
    const me = actor();
    const list = (meta.comments || []).filter(c => c.state !== "draft" || c.author === me);
    const drafts = list.filter(c => c.state === "draft");
    const open = list.filter(c => ["sent", "read"].includes(c.state));
    const k = kept();
    const s = JSON.stringify([list, k, me]);
    if (s === sig) return;
    // Not while the person types in a reply: the next poll draws it.
    if (box.contains(document.activeElement) && document.activeElement.matches("input, textarea") && sig) return;
    sig = s;
    // #4844: a reply that the person began to another comment stays when the list draws again.
    const replies = {};
    for (const inp of box.querySelectorAll("[data-cv-reply-text]")) if (inp.value) replies[inp.dataset.cvReplyText] = inp.value;
    box.innerHTML = `<div class="cv-head"><b>Comments</b> <span class="muted">${open.length} open${drafts.length ? ` · ${drafts.length} draft${drafts.length === 1 ? "" : "s"}` : ""}</span>
        <span class="cv-sp"></span>${drafts.length ? `<button class="btn primary" type="button" data-cv-sendall>Send ${drafts.length === 1 ? "the draft" : `all ${drafts.length} drafts`} to the session</button>` : ""}</div>
      ${k ? `<div class="cv-compose"><div class="muted">On ${esc(partText(k.anchor))}${k.anchor && k.anchor.path ? ` <code>${esc(clip(k.anchor.path, 60))}</code>` : ""}</div>
        <textarea rows="3" placeholder="Your comment: what to change, and why" aria-label="Your comment">${esc(k.text || "")}</textarea>
        <div class="cv-acts"><button class="btn primary" type="button" data-cv-now>Send now</button>
          <button class="btn" type="button" data-cv-draft>Save as draft</button>
          <button class="btn" type="button" data-cv-cancel>Cancel</button></div></div>` : ""}
      ${list.map(c => { const st = stateOf(c);
        return `<div class="cv-c cv-${st}" data-cid="${c.id}">
          <div class="cv-h"><span class="chip cv-st">${st}</span> <b>c${c.id}</b> <span class="muted">${esc(c.author || "")} · ${ago(c.created_at)} · v${c.version}</span>
            ${c.anchor && c.anchor.path ? `<span class="link" data-cv-show="${esc(c.anchor.path)}" title="Show this part of the page">on ${esc(partText(c.anchor))}</span>` : `<span class="muted">on ${esc(partText(c.anchor))}</span>`}</div>
          <div class="cv-b">${esc(c.body)}</div>
          ${(c.replies || []).map(r => `<div class="cv-r"><b>${esc(r.author || "")}</b> <span class="muted">${ago(r.created_at)}</span>: ${esc(r.body)}</div>`).join("")}
          ${c.state === "resolved" ? `<div class="muted cv-r">resolved by ${esc(c.resolved_by || "")}${c.resolve_note ? ": " + esc(c.resolve_note) : ""}</div>` : ""}
          <div class="cv-acts">${c.state === "draft"
            ? `<button class="btn primary" type="button" data-cv-send="${c.id}">Send</button><button class="btn" type="button" data-cv-discard="${c.id}">Discard</button>`
            : c.state === "resolved" ? "" : `<input data-cv-reply-text="${c.id}" placeholder="Reply" aria-label="Reply to c${c.id}">
              <button class="btn" type="button" data-cv-reply="${c.id}">Reply</button><button class="btn" type="button" data-cv-resolve="${c.id}">Resolve</button>`}</div>
        </div>`; }).join("") || (k ? "" : `<div class="muted cv-none">No comments yet. Comment picks a part of the page; Comment on the page takes the whole page.</div>`)}`;
    for (const [cid, v] of Object.entries(replies)) {
      const inp = box.querySelector(`[data-cv-reply-text="${cid}"]`); if (inp) inp.value = v;
    }
  }

  // #4845: one action at a time. While it goes, the buttons wait and the clicked one says so; a second click does
  // nothing (a comment came 2 to 6 times when nothing showed that the first click went).
  let busy = false;
  async function run(op, args, done, btn) {
    if (!actor()) return toast("Choose your name in 'You are' first", true);
    if (busy) return;
    busy = true;
    const ctl = [...box.querySelectorAll("button")], label = btn ? btn.textContent : "";
    ctl.forEach(x => { x.disabled = true; });
    if (btn) btn.textContent = "Sending…";
    try { const r = await act(op, args); if (done) done(r); } catch (e) { return; /* toast shown */ } finally {
      busy = false;
      ctl.forEach(x => { x.disabled = false; });
      if (btn && btn.isConnected) btn.textContent = label;
    }
    sig = ""; await reload();
  }

  box.addEventListener("input", (e) => {
    if (e.target.matches(".cv-compose textarea")) { const k = kept(); if (k) keep({ ...k, text: e.target.value }); }
  });
  box.addEventListener("keydown", (e) => {
    const r = e.target.closest && e.target.closest("[data-cv-reply-text]");
    if (r && e.key === "Enter") { e.preventDefault(); box.querySelector(`[data-cv-reply="${r.dataset.cvReplyText}"]`).click(); }
  });
  box.addEventListener("click", (e) => {
    const t = e.target.closest("button, [data-cv-show]"); if (!t) return;
    if (busy && t.matches("button")) return;
    const k = kept();
    if (t.dataset.cvShow) return show(t.dataset.cvShow);
    if (t.hasAttribute("data-cv-cancel")) { keep(null); sig = ""; return draw(); }
    if (t.hasAttribute("data-cv-now") || t.hasAttribute("data-cv-draft")) {
      const text = (box.querySelector(".cv-compose textarea").value || "").trim();
      if (!text) return toast("Write the comment first", true);
      const now = t.hasAttribute("data-cv-now");
      return run("canvas_comment", { name: ref, text, anchor: k && k.anchor, version: meta.version, send: now },
        (r) => { keep(null); toast(r.again ? `c${r.id} went already` : now ? `Sent c${r.id} to the session` : `Saved c${r.id} as a draft`); }, t);
    }
    if (t.hasAttribute("data-cv-sendall")) return run("canvas_send", { name: ref }, (r) => toast(`Sent ${r.sent.length} comment(s) to ${r.to}`), t);
    if (t.dataset.cvSend) return run("canvas_send", { name: ref, comments: [+t.dataset.cvSend] }, (r) => toast(`Sent to ${r.to}`), t);
    if (t.dataset.cvDiscard) return run("canvas_discard", { comment: +t.dataset.cvDiscard }, null, t);
    if (t.dataset.cvResolve) return run("canvas_resolve", { comment: +t.dataset.cvResolve, note: "resolved in the page" }, null, t);
    if (t.dataset.cvReply) {
      const inp = box.querySelector(`[data-cv-reply-text="${t.dataset.cvReply}"]`), text = (inp && inp.value || "").trim();
      if (!text) return toast("Write the reply first", true);
      return run("canvas_reply", { comment: +t.dataset.cvReply, text }, () => { if (inp) inp.value = ""; }, t);
    }
  });

  return {
    render(m) { meta = m; draw(); },
    pick(anchor) { compose(anchor); },
    compose,
  };
}
