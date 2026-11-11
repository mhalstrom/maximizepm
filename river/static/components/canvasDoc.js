// Docs (#4708): a Markdown canvas that a session writes section by section, like a Claude Doc. After the panel draws
// the Markdown (sanitized by DOMPurify), this file adds what a doc needs:
// - a pending section (its text is one line "writing: <intent>") shows as being written;
// - a decision in the decision form (maxpm guide decisions: "Decision <n> of <m>: <name>" ... "Your answer: ...")
//   becomes a card: its options (the quoted words of "Your answer") as buttons, a box for other words, and the
//   answer given so far; an answer goes at once to the session that writes the doc (core.canvas_answer);
// - each section heading has Edit: the section's text in a box; Save makes a new version by the person, and the
//   session gets a note (core.canvas_section).
import { esc, ago, toast, act, actor } from "../lib.js";

const DECISION = /^\W*Decision (\d+) of (\d+):\s*(.+?)\s*$/i;
const HEADING = /^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$/;

// The sections of a Markdown text, as core.md_sections: [{title, level, start, end}] (line numbers).
export function mdSections(text) {
  const lines = String(text).split("\n"), out = [];
  let fence = null;
  lines.forEach((line, i) => {
    const f = /^\s*(```|~~~)/.exec(line);
    if (f) { fence = fence === f[1] ? null : (fence || f[1]); return; }
    const h = fence ? null : HEADING.exec(line);
    if (h) out.push({ title: h[2].trim(), level: h[1].length, start: i, end: lines.length });
  });
  out.forEach((s, k) => { const nxt = out.slice(k + 1).find(x => x.level <= s.level); s.end = nxt ? nxt.start : lines.length; });
  return out;
}

// The options of a decision: the quoted words of its "Your answer" line ("A", “yes”).
export function answerOptions(text) {
  const m = /Your answer:(.*)$/im.exec(text || ""); if (!m) return [];
  return [...m[1].matchAll(/["“]([^"”]{1,60})["”]/g)].map(x => x[1].trim()).filter(Boolean);
}

const isHeading = (el) => /^H[1-6]$/.test(el.tagName);

// #5360: the parts of a decision card read as a form, not as one wall of text. The first line ("Decision 1 of 2:
// <name>") becomes the card's title, and each label of the decision form at the start of a line is bold. The elements
// stay where they are (the side panel finds a card's section by its first element), only their text is wrapped.
const LABEL = /(^|\n)([ \t]*)(What you decide|Why it matters|Options|My recommendation|Your answer):/g;
function markTitle(start) {
  if (isHeading(start)) { start.classList.add("doc-dtitle"); return; }
  const take = [];
  for (const node of [...start.childNodes]) {
    const nl = node.nodeType === 3 ? node.data.indexOf("\n") : -1;
    if (nl < 0) { take.push(node); continue; }
    const rest = node.splitText(nl); rest.data = rest.data.slice(1);  // the title is a block: the break goes
    take.push(node); break;
  }
  const title = document.createElement("span"); title.className = "doc-dtitle";
  start.prepend(title); title.append(...take);
}
function markLabels(part) {
  const walk = document.createTreeWalker(part, NodeFilter.SHOW_TEXT), nodes = [];
  while (walk.nextNode()) if (!walk.currentNode.parentElement.closest("code, pre, .doc-dtitle")) nodes.push(walk.currentNode);
  for (const node of nodes) {
    const block = node.parentElement.closest("p, li, div") || part, r = document.createRange();
    const title = block.querySelector(":scope > .doc-dtitle");  // a line starts after the title
    if (title && title.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING) r.setStartAfter(title);
    else r.setStart(block, 0);
    r.setEnd(node, 0);
    const lineStart = /(^|\n)[ \t]*$/.test(r.toString());
    const frag = document.createDocumentFragment();
    let last = 0, m;
    LABEL.lastIndex = 0;
    while ((m = LABEL.exec(node.data))) {
      if (!m[1] && (m.index !== 0 || !lineStart)) continue;
      const at = m.index + m[1].length + m[2].length;
      frag.append(node.data.slice(last, at));
      const b = document.createElement("span"); b.className = "doc-label"; b.textContent = m[3] + ":";
      frag.append(b); last = at + m[3].length + 1;
    }
    if (!last) continue;
    frag.append(node.data.slice(last));
    node.replaceWith(frag);
  }
}

// The newest answer to each decision: {n: comment}, from the canvas's comments (anchor decision:<n>).
function answersOf(meta) {
  const answers = {};
  for (const c of meta.comments || []) {
    const n = (/^decision:(\d+)$/.exec((c.anchor && c.anchor.path) || "") || [])[1];
    if (n && (!answers[n] || c.id > answers[n].id)) answers[n] = c;
  }
  return answers;
}
const answeredHtml = (a) => !a ? "" : `<b>${esc(a.author)}</b> answered ${ago(a.created_at)}: ${esc(a.body.replace(/^Answer to decision \d+ of \d+ \([^)]*\):\s*/, ""))}
  <span class="muted">(${esc(a.state === "sent" ? "sent to the session" : a.state === "read" ? "the session read it" : a.state)}${(a.replies || []).length ? ", " + a.replies.length + " repl" + (a.replies.length === 1 ? "y" : "ies") : ""})</span>`;

// The answered line of a card's bar for answer a (a comment).
function showAnswer(bar, a) {
  let line = bar.querySelector(".doc-answered");
  const html = answeredHtml(a);
  if (!line) { line = document.createElement("div"); line.className = "doc-answered"; bar.prepend(line); }
  if (line.dataset.sig !== html) { line.innerHTML = html; line.dataset.sig = html; }
}

// #4844: a new answer, or a new state of one, changes only the answered line of each card: the buttons, and the words
// a person types in the box, stay.
export function updateAnswers(root, meta) {
  const answers = answersOf(meta);
  for (const card of root.querySelectorAll(".doc-decision")) {
    const a = answers[card.dataset.decision], bar = card.querySelector(".doc-answer");
    if (!bar) continue;
    if (bar.dataset.sending) continue;  // an answer on its way shows when it came back
    const line = bar.querySelector(".doc-answered");
    if (!a) { if (line) line.remove(); } else showAnswer(bar, a);
    const go = bar.querySelector("[data-doc-answer]");
    if (go) go.textContent = a ? "Answer again" : "Answer";
  }
}

// root: the drawn Markdown. source: its text. meta: the canvas (comments for the answers). opts: { ref, live (the
// newest version: answers and edits allowed), done() after a change, current() (the newest text: a section that a
// new version left as it was keeps its Edit, which must read the text of that version, #4844) }.
export function decorateDoc(root, source, meta, { ref, live, done, current }) {
  // Pending sections.
  for (const p of root.querySelectorAll("p")) {
    const m = /^writing:\s*(.*)$/is.exec(p.textContent.trim());
    if (!m || !p.previousElementSibling || !isHeading(p.previousElementSibling)) continue;
    p.className = "doc-pending"; p.setAttribute("role", "status");
    p.innerHTML = `<span class="doc-dots" aria-hidden="true"></span> Being written: ${esc(m[1])}`;
  }
  // Decision cards.
  const answers = answersOf(meta);
  for (const start of [...root.children]) {
    const m = DECISION.exec(start.textContent.trim().split("\n")[0]);
    if (!m || !start.parentNode) continue;
    const n = +m[1], parts = [start];
    let el = start;
    while (!/Your answer:/i.test(el.textContent) && el.nextElementSibling && !isHeading(el.nextElementSibling)
           && !DECISION.test(el.nextElementSibling.textContent.trim())) { el = el.nextElementSibling; parts.push(el); }
    const card = document.createElement("div");
    card.className = "doc-decision"; card.id = "decision-" + n; card.dataset.decision = n;
    start.before(card); card.append(...parts);
    const opts = answerOptions(parts.map(x => x.textContent).join("\n")), a = answers[n];
    markTitle(start); parts.forEach(markLabels);
    // The options on one row (they wrap), the box and Answer on their own row, which never wraps: on a phone the box
    // keeps its words and Answer stays beside it (#5360).
    const bar = document.createElement("div"); bar.className = "doc-answer";
    bar.innerHTML = (a ? `<div class="doc-answered" data-sig="${esc(answeredHtml(a))}">${answeredHtml(a)}</div>` : "")
      + (live ? (opts.length ? `<div class="cv-acts doc-opts">${opts.map(o => `<button class="btn" type="button" data-doc-opt="${esc(o)}">${esc(o)}</button>`).join("")}</div>` : "")
        + `<div class="cv-acts doc-ans-row"><input data-doc-other placeholder="${opts.length ? "or your own words" : "your answer"}" aria-label="Your answer to decision ${n}">
        <button class="btn primary" type="button" data-doc-answer>${a ? "Answer again" : "Answer"}</button></div>` : "");
    card.append(bar);
    // #4845: one answer per click. While it goes, the card's buttons and box wait and say so; then the answer shows
    // on the card at once, before the page's next state.
    const send = async (text) => {
      if (!actor()) return toast("Choose your name in 'You are' first", true);
      if (!text.trim()) return toast("Pick an option or write your answer", true);
      if (bar.dataset.sending) return;
      const go = bar.querySelector("[data-doc-answer]"), label = go ? go.textContent : "";
      const ctl = [...bar.querySelectorAll("button, input")];
      bar.dataset.sending = "1"; ctl.forEach(x => { x.disabled = true; });
      if (go) go.textContent = "Sending…";
      try {
        const com = await act("canvas_answer", { name: ref, decision: n, text });
        toast(`Answered decision ${n}: it went to the session`);
        const box = bar.querySelector("[data-doc-other]"); if (box) box.value = "";
        showAnswer(bar, com);
        if (go) go.textContent = "Answer again";
        done();
      } catch (e) { if (go) go.textContent = label; /* toast shown */ } finally {
        delete bar.dataset.sending; ctl.forEach(x => { x.disabled = false; });
      }
    };
    bar.addEventListener("click", (e) => {
      const o = e.target.closest("[data-doc-opt]"); if (o) return send(o.dataset.docOpt);
      if (e.target.closest("[data-doc-answer]")) send(bar.querySelector("[data-doc-other]").value);
    });
    bar.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.matches("[data-doc-other]")) { e.preventDefault(); send(e.target.value); } });
  }
  // Edit a section.
  if (!live) return;
  const find = (text, h) => mdSections(text).find(x => x.title.replace(/[*_`]/g, "").trim() === h.dataset.docTitle);
  const heads = [...root.querySelectorAll("h1, h2, h3, h4, h5, h6")];
  heads.forEach((h) => {
    h.dataset.docTitle = h.textContent.trim();
    const first = find(source, h);
    if (!first || first.level === 1) return;
    const b = document.createElement("button"); b.type = "button"; b.className = "doc-edit link"; b.textContent = "edit";
    b.title = `Edit the section ${first.title}`; h.append(" ", b);
    b.onclick = (e) => {
      e.stopPropagation();
      const text = current ? current() : source, s = find(text, h) || first, lines = String(text).split("\n");
      const ed = document.createElement("div"); ed.className = "cv-edit doc-sec-edit";
      ed.innerHTML = `<textarea aria-label="The text of the section ${esc(s.title)}"></textarea>
        <div class="cv-acts"><button class="btn primary" type="button">Save the section</button><button class="btn" type="button">Cancel</button>
        <span class="muted">A new version; the session that writes the doc gets a note.</span></div>`;
      ed.querySelector("textarea").value = lines.slice(s.start + 1, s.end).join("\n").trim();
      // The section's drawn blocks give way to the box until Save or Cancel.
      const drawn = []; let el = h.nextElementSibling;
      while (el && !(isHeading(el) && +el.tagName[1] <= s.level)) { drawn.push(el); el = el.nextElementSibling; }
      drawn.forEach(x => { x.hidden = true; }); h.after(ed); ed.querySelector("textarea").focus();
      const [save, cancel] = ed.querySelectorAll("button");
      cancel.onclick = () => { ed.remove(); drawn.forEach(x => { x.hidden = false; }); };
      save.onclick = async () => {
        if (!actor()) return toast("Choose your name in 'You are' first", true);
        try {
          const r = await act("canvas_section", { name: ref, heading: s.title, text: ed.querySelector("textarea").value });
          toast(r.unchanged ? "No change" : `Saved the section as version ${r.version}`);
          cancel.onclick(); done();
        } catch (err) { /* toast shown */ }
      };
    };
  });
}
