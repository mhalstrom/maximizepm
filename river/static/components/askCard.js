// The card (#6002, mark 2026-11-16: 'cards are for when the agents are asking for decisions to be made ... the action
// items are basically questions for the user so can be the same model'). One block for a question to a person and for
// a person item, in the dialog Waits for you (waitDialog.js), as card-queue-mockup v15 shows: the type tag (#5998) and
// the level in its head, the question, the options as buttons when the text has the decision form (its "Your answer:"
// line, maxpm guide decisions), the answer box, the earlier decisions (#5955); and on its right the comments (the
// messages on it), its history, Share (#6006: a private page of the card; the votes and comments of the people it
// is shared with come back here), and Discuss with the manager.
import { esc, ago, clip, getJson, pageToken } from "../lib.js";
import { chip, prioNumChip, projectChip } from "./chip.js";
import { button } from "./button.js";
import { earlierHtml } from "./card.js";
import { answerOptions } from "./canvasDoc.js";
import { artifactsHtml } from "./artifactTasks.js";

export const TYPES = ["decide", "look", "approve", "do", "read"];
export const typeChip = (t) => t ? chip("c-type-" + t, t[0].toUpperCase() + t.slice(1), "What you do with it") : "";

// One card from a Needs you event (api/needs-you), a session's waiting question (sessions.py waiting) or an item
// (api/item/<id>): { kind: "question"|"item", id, item_id, title, detail, type, level, blocked, priority, project,
// from, at, note, earlier }.
export function fromEvent(e) {
  return e.kind === "item"
    ? { kind: "item", id: e.item_id, item_id: e.item_id, title: e.item_title || "", detail: e.item_context || e.item_notes || "",
        type: e.type, priority: e.priority, project: e.project, at: e.opened_at, earlier: e.earlier || [],
        exhibits: e.exhibits || [], artifacts: e.artifact_links || [], decision: e.decision || null }
    : { kind: "question", id: e.message_id, item_id: e.item_id, title: e.body || "", type: e.type, level: e.message_level,
        blocked: e.message_blocked, project: e.project, from: e.from_agent, at: e.opened_at, earlier: e.earlier || [],
        exhibits: e.exhibits || [], artifacts: e.artifact_links || [], decision: e.decision || null };
}
export function fromWaiting(w, session) {
  return { kind: "question", id: w.id, item_id: w.item_id, title: w.body || "", type: w.type || "decide", level: w.level,
    blocked: w.blocked, from: w.from || session, at: w.asked_at, note: w.note, earlier: w.earlier || [],
    exhibits: w.exhibits || [], artifacts: w.artifact_links || [], decision: w.decision || null };
}
export function fromItem(it) {
  return { kind: "item", id: it.id, item_id: it.id, title: it.title || "", detail: it.context || it.notes || "",
    type: it.type, priority: it.effective_priority != null ? it.effective_priority : it.priority, project: it.project,
    at: it.created_at, earlier: it.earlier || [], tries: it.tries || null, exhibits: it.exhibits || [],
    artifacts: it.artifact_links || [], release: it.release_yes || null, decision: it.decision || null };
}

// The manager's escalation (#6413, mark 2026-11-20: 'a dialog modal with selectable options with a final text area
// option'): its options (maxpm forward --option) as rows to pick, the recommended one first and marked. The box under
// them takes the person's own answer, or words added to the option picked.
const choices = (c) => (c.decision && c.decision.options) || [];
export function choicesHtml(c) {
  const opts = choices(c);
  if (!opts.length) return "";
  return `<div class="acard-choices" role="radiogroup" aria-label="Options">${opts.map(o =>
    `<label class="wopt acard-choice"><input type="radio" name="choice-${c.kind}-${c.id}" value="${esc(o.label)}">
      <span><b>${esc(o.label)}</b>${o.recommended ? " " + chip("c-ready", "Recommended", "The manager's pick") : ""}
      <span class="acard-cd muted t-s">${esc(o.detail)}</span></span></label>`).join("")}</div>`;
}

// The thing itself (#5956, policy g3): the screenshot of a page the card names, a picture file, or a commit's diff,
// in line. One that serve has not made yet says so; one it could not make says why, with the source.
export function exhibitsHtml(c) {
  const xs = c.exhibits || [];
  if (!xs.length) return "";
  return `<div class="acard-things">${xs.map(x => {
    const src = `<div class="muted t-s">${esc(x.what === "diff" ? "commit " + x.source : x.source)}</div>`;
    if (x.error) return `<div class="acard-thing">${src}<div class="muted t-s">No picture: ${esc(x.error)}</div></div>`;
    if (!x.ready) return `<div class="acard-thing">${src}<div class="muted t-s">MaximizePM makes the ${x.what === "diff" ? "diff" : "screenshot"} now…</div></div>`;
    return x.what === "diff"
      ? `<details class="acard-thing" open><summary>${esc("commit " + x.source)}</summary><pre class="acard-diff" data-exhibit="${x.id}">…</pre></details>`
      : `<div class="acard-thing">${src}<a href="api/exhibit/${x.id}" target="_blank" rel="noopener"><img class="acard-shot" src="api/exhibit/${x.id}" alt="${esc(x.source)}" loading="lazy"></a></div>`;
  }).join("")}</div>`;
}

// The text of each diff on the cards in box (read once per card draw).
export function loadDiffs(box) {
  box.querySelectorAll("pre[data-exhibit]").forEach(async (pre) => {
    try {
      const r = await fetch(`api/exhibit/${pre.dataset.exhibit}`);
      pre.textContent = r.ok ? await r.text() : "The diff is gone.";
    } catch (_) { pre.textContent = "The diff did not load."; }
  });
}

// A try's output as its summary (the first sentence) and the rest, which the card folds under "more" (#6040).
export function trySummary(text) {
  const s = String(text || "").trim();
  const m = /^[\s\S]*?[.!?](?=\s|$)/.exec(s);
  const head = (m ? m[0] : s).trim();
  if (head.length > 280) return { summary: clip(s, 280), rest: s };
  return { summary: head, rest: s.slice(head.length).trim() };
}

// The tries of one task side by side (#5996): each try's result (#6040: its first canvas in a frame, a button that
// opens that canvas in a tab of its own, and the first sentence of its output, the rest under "more") and a button
// that keeps it. A try that is not done yet has no Keep button: the card is ready when every try is done.
export function triesHtml(c) {
  if (!c.tries || !c.tries.length) return "";
  return `<div class="acard-tries">${c.tries.map(t => {
    const cv = (t.canvases || [])[0], done = t.status === "done";
    const { summary, rest } = trySummary(t.output);
    const text = !done ? `Not done yet (${esc(t.status.replace("_", " "))})`
      : summary ? esc(summary) : "No output: open the item";
    const frame = cv ? `<div class="acard-trybox"><iframe class="acard-tryframe" data-try-frame="${cv.id}"
      title="${esc(cv.title || cv.name)}" sandbox="allow-scripts" loading="lazy"></iframe></div>` : "";
    const acts = (cv ? button(`Open try ${t.k}`, { data: { "try-open": cv.id, "try-item": t.id, "try-session": t.assignee || "" },
      title: "Open the canvas" }) : "") + (done ? button(`Keep try ${t.k}`, { kind: "primary", data: { opt: String(t.k) } }) : "");
    return `<div class="acard-try">
    <div class="acard-h"><b>Try ${t.k}</b><span class="muted t-s">#${t.id}${t.assignee ? ` · ${esc(t.assignee)}` : ""}</span></div>
    ${frame}
    <div class="acard-d">${text}</div>
    ${done && rest ? `<details class="acard-more"><summary class="t-s">more</summary><div class="acard-d">${esc(rest)}</div></details>` : ""}
    ${acts ? `<div class="acard-tryacts">${acts}</div>` : ""}</div>`;
  }).join("")}</div>`;
}

// The canvas frames of the tries on the cards in box: each loads with a full-screen key for its canvas, as a canvas
// tab of the side panel does (#4768), never with the page's token.
export function loadTryFrames(box) {
  box.querySelectorAll("iframe[data-try-frame]").forEach(async (f) => {
    try {
      const q = new URLSearchParams({ target: "canvas:" + f.dataset.tryFrame, token: await pageToken() });
      const r = await fetch("api/view-key?" + q), j = await r.json();
      if (!r.ok) throw new Error(j.error || "failed");
      const u = new URLSearchParams(j.url.split("?")[1]);
      f.src = `canvas/${f.dataset.tryFrame}/latest?e=${u.get("e")}&k=${encodeURIComponent(u.get("k"))}`;
    } catch (_) { f.replaceWith(Object.assign(document.createElement("div"), { className: "muted t-s", textContent: "The canvas did not load." })); }
  });
}

// The level in the head: an item's priority; a question's level (low, or high when nobody said low), and that its
// session stands still until the answer.
function levelChips(c) {
  if (c.kind === "item") return c.priority != null ? prioNumChip(c.priority) : "";
  return (c.level === "low" ? chip("c-p", "Low") : chip("c-p0", "High"))
    + (c.blocked ? chip("c-blocked", "its session waits", "Its session waits for the answer") : "");
}

// The options of the decision form, as buttons: a click puts the option in the answer box and sends it.
export function optionsHtml(c) {
  const opts = answerOptions(c.title + "\n" + (c.detail || ""));
  return opts.length ? `<div class="acard-opts">${opts.map(o => button(esc(o), { cls: "acard-opt", data: { opt: o },
    title: `Answer: ${o}` })).join("")}</div>` : "";
}

// A person's sign-off on a release (#6039): the review it follows, and its report. Only the person approves it.
export function releaseHtml(r) {
  const rv = r.review;
  return `<div class="acard-rel">
    <div><b>Sign-off of release #${r.deploy}</b> of ${esc(r.target || "its target")}: only you can approve it.</div>
    ${rv ? `<div class="t-s">Its review #${rv.id} ${rv.status === "done" ? "passed" : esc(rv.status.replace("_", " "))}${rv.done_by ? ` (${esc(rv.done_by)})` : ""}${rv.output ? `: ${esc(clip(rv.output, 300))}` : ""}</div>`
      : `<div class="muted t-s">This release has no review.</div>`}
    <div>${button("Read the review report", { data: { report: r.deploy } })}</div></div>`;
}

// The card's form. opts.pane: the asking session has a tmux pane here, so an answer also goes into its chat.
export function cardHtml(c, { pane = false } = {}) {
  const who = c.kind === "item" ? `#${c.id}` : `question #${c.id}${c.from ? ` from ${esc(c.from === "maxpm" ? "MaximizePM" : c.from)}` : ""}`;
  const head = `<div class="acard-h">${typeChip(c.type)}${levelChips(c)}${c.project ? projectChip(c.project) : ""}
    <span class="muted t-s">${who}</span><span class="spacer"></span>${c.at ? `<span class="muted t-s">${esc(ago(c.at))}</span>` : ""}</div>`;
  const item = c.kind === "item", rel = item && c.release, picks = choices(c).length > 0;
  // The manager's short question leads; the asker's own words and the item's text follow under it.
  const ask = c.decision && c.decision.question;
  const title = ask || c.title, detail = ask ? [c.title, c.detail].filter(Boolean).join("\n\n") : c.detail;
  const box = picks
    ? `<textarea class="wanswer" placeholder="Other / your words (alone, or added to the option you pick)" aria-label="Other / your words"></textarea>`
    : rel
    ? `<textarea class="wanswer" placeholder="What must change (for Request changes), or a note on your yes" aria-label="What must change"></textarea>`
    : item
    ? `<textarea class="wanswer" placeholder="What you decided or did (saved on the item)" aria-label="Your decision or action"></textarea>`
    : `<textarea class="wanswer" placeholder="Your answer" aria-label="Your answer"></textarea>`;
  const acts = (c.item_id ? button("Open the item", { data: { open: c.item_id } }) : "")
    + (rel ? button("Request changes", { data: { wchanges: 1 }, title: "The review runs again" }) : "")
    + (c.decision ? button("Cancel", { data: { wcancel: 1 }, title: "Close without an answer" }) : "")
    + button(rel ? "Approve" : c.decision ? "Submit" : item ? "Done" : pane ? "Send to the chat" : "Answer",
      { kind: "primary", cls: "wgo", type: "submit" });
  return `<form class="wblock acard" data-kind="${item ? "item" : "question"}" data-id="${c.id}">
    <div class="acard-main">${head}
      <div class="wq">${esc(title)}</div>
      ${detail ? `<div class="acard-d">${esc(clip(detail, 1200))}</div>` : ""}
      ${rel ? releaseHtml(rel) : ""}
      ${artifactsHtml(c.artifacts, { line: true })}
      ${exhibitsHtml(c)}
      ${c.note ? `<div class="muted">The manager: ${esc(c.note)}</div>` : ""}
      ${c.tries && c.tries.length ? triesHtml(c) : picks ? choicesHtml(c) : optionsHtml(c)}
      ${earlierHtml(c.earlier)}
      ${box}
      <div class="acard-acts">${acts}</div></div>
    <aside class="acard-s" data-side="${c.kind}:${c.id}">${sideHtml(null)}</aside>
  </form>`;
}

// The right column: the comments (messages on the item or in the question's thread), the history, and Discuss.
// rows: null while it loads.
export function sideHtml(rows) {
  const comments = rows == null ? `<div class="muted t-s">Reading…</div>`
    : rows.comments.length ? rows.comments.map(m => `<div class="acard-cm"><b>${esc(m.from_agent === "maxpm" ? "MaximizePM" : m.from_agent)}</b>
        <span class="muted t-s">${esc(m.kind)} · ${esc(ago(m.created_at))}</span><div class="t-s">${esc(clip(m.body, 400))}</div></div>`).join("")
    : `<div class="muted t-s">No comments yet.</div>`;
  const history = rows == null ? "" : rows.history.length
    ? `<ul class="acard-hist">${rows.history.map(h => `<li><span class="muted">${esc(ago(h.at))}</span> ${esc(clip(h.change, 160))}</li>`).join("")}</ul>`
    : `<div class="muted t-s">Nothing yet.</div>`;
  const sh = rows && rows.shared, page = sh && sh.page;
  const shared = page && page.url && !page.ended
    ? `<div class="t-s"><a class="link" href="${esc(page.url)}" target="_blank" rel="noopener noreferrer">The shared page</a>
        <span class="muted">${page.access === "link" ? "anyone with the link" : "private"}</span></div>` : "";
  return `<h4>Comments</h4>${comments}<h4>History</h4>${history}${shared ? `<h4>Shared</h4>${shared}` : ""}
    <div class="acard-disc">${button(page && page.url && !page.ended ? "Share again" : "Share", { data: { share: 1 },
      title: "Share for advice" })}${button("Discuss with the manager", { data: { discuss: 1 },
      title: "Send to the manager" })}</div>`;
}

// The votes and comments that came back from the card's shared page (#6006), in the form of the comments above.
export function sharedComments(sh) {
  const out = [];
  for (const c of (sh && sh.comments) || []) {
    for (const x of [c, ...(c.replies || [])]) {
      out.push({ from_agent: x.author || "a reader", kind: "on the shared page", body: x.body, created_at: x.created_at });
    }
  }
  return out;
}

// The comments and the history of a card, from the server. A question: its thread; an item: its messages and events.
export async function loadSide(c) {
  const shared = await getJson(`api/card-share/${c.kind}/${c.id}`).catch(() => null);
  const back = sharedComments(shared);
  if (c.kind === "question") {
    const t = await getJson(`api/thread/${c.id}`);
    const all = t.messages || [];
    return { shared, comments: [...all.filter(m => m.id !== c.id), ...back],
      history: all.filter(m => m.id === c.id).map(m => ({ at: m.created_at, change: `asked by ${m.from_agent}`
        + (m.delivered_at ? `; shown ${ago(m.delivered_at)}` : "") + (m.read_at ? `; read ${ago(m.read_at)}` : "") })) };
  }
  const j = await getJson(`api/item/${c.id}`);  // item_show: its events newest first
  return { shared, comments: [...(j.messages || []), ...back], history: (j.events || []).slice(0, 8) };
}

// The text that Discuss with the manager sends: the card whole, so the manager reads it in its chat.
export function discussText(c) {
  return `The person wants to talk about ${c.kind === "item" ? `item #${c.id}` : `question #${c.id}` + (c.from ? ` from ${c.from}` : "")}`
    + `${c.type ? ` (${c.type})` : ""}: ${clip(c.title, 600)}`;
}
