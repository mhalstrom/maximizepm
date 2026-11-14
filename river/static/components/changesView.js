// The Changes view (#5855, mark's design 2026-11-14): what a session changed in its folder, or what an item or a
// release changed (its commits), as a diff the person reads and answers.
//   renderChanges(box, {session} | {item}): the side panel's fixed tab Changes, and the item dialog (openItemChanges).
// A session's diff is against a base the person picks (GitHub main, Not committed, Since my last review). Side by side
// or one column (a phone: one column). A click on a line, Shift-click for a range, opens a comment box; the comments
// collect, and Send to agent sends them as one message with file and line. Reviewed marks the commit for 'Since my
// last review'; Approve and Request changes answer the project setting review_at (a push or a release waits for
// Approve). Open in editor opens the file in VS Code. No editing here. Data: GET /api/diff; ops review_send, review_mark.
// Classes and data keys start with chg- (cv- is the canvas view's, sessions.css).

import { esc, store, toast, act, getJson } from "../lib.js";
import { button } from "./button.js";
import { chip } from "./chip.js";
import { makeDialog } from "./dialog.js";

const LAYOUT = "river.changes.layout";
const BASES = [["origin", "Against GitHub main"], ["uncommitted", "Not committed"], ["review", "Since my last review"]];
const ctxs = {};  // key -> {base, comments, sel}: kept while the page lives, so a comment survives a refresh of the view
const phone = () => matchMedia("(max-width: 759px)").matches;

function ctxOf(src) {
  const key = src.session ? "s:" + src.session : "i:" + src.item;
  return ctxs[key] || (ctxs[key] = { base: "origin", comments: [], sel: null, data: null, rows: [] });
}

export async function renderChanges(box, src) {
  const ctx = ctxOf(src);
  box.classList.add("chg-host");
  box.innerHTML = `<div class="chg muted t-m">Reading the changes…</div>`;
  const q = src.session ? `session=${encodeURIComponent(src.session)}&base=${ctx.base}` : `item=${src.item}`;
  try { ctx.data = await getJson("api/diff?" + q); }
  catch (e) { box.innerHTML = `<div class="chg muted">${esc(e.message)}</div>`; return; }
  draw(box, src, ctx);
  if (!box._chgWired) { wire(box, src); box._chgWired = true; }
}

// One file's lines as rows: [{t, old, new, text}] for one column, [[left, right]] for side by side.
function pairs(lines) {
  const out = [];
  for (let i = 0; i < lines.length;) {
    if (lines[i][0] === " ") { out.push([lines[i], lines[i]]); i++; continue; }
    const del = [], add = [];
    while (i < lines.length && lines[i][0] === "-") del.push(lines[i++]);
    while (i < lines.length && lines[i][0] === "+") add.push(lines[i++]);
    for (let k = 0; k < Math.max(del.length, add.length); k++) out.push([del[k] || null, add[k] || null]);
  }
  return out;
}

function fileHtml(f, fi, root, ctx, split) {
  const rows = ctx.rows;
  const on = (r) => ctx.sel && ctx.sel.fi === fi && r >= ctx.sel.from && r <= ctx.sel.to ? " chg-on" : "";
  const kind = (l) => l[0] === "+" ? "chg-add" : l[0] === "-" ? "chg-del" : "";
  const cell = (l, side) => {
    if (!l) return `<td class="chg-n"></td><td class="chg-c chg-none"></td>`;
    const n = side === "old" ? l[1] : l[2];
    const r = rows.push({ fi, file: f.path, n, side, text: l[3] }) - 1;
    const cls = kind(l) + on(r);
    return `<td class="chg-n ${cls}" data-r="${r}">${n ?? ""}</td><td class="chg-c ${cls}" data-r="${r}">${esc(l[3])}</td>`;
  };
  const body = [];
  for (const h of f.hunks) {
    body.push(`<tr class="chg-hunk"><td colspan="${split ? 4 : 3}">${esc(h.head)}</td></tr>`);
    if (split) for (const [a, b] of pairs(h.lines)) {
      const startR = rows.length;
      body.push(`<tr>${cell(a, "old")}${cell(b, "new")}</tr>`);
      body.push(commentsAt(ctx, fi, startR, rows.length, split));
    } else for (const l of h.lines) {
      const side = l[0] === "-" ? "old" : "new";
      const n = side === "old" ? l[1] : l[2];
      const r = rows.push({ fi, file: f.path, n, side, text: l[3] }) - 1;
      const cls = kind(l) + on(r);
      body.push(`<tr><td class="chg-n ${cls}" data-r="${r}">${l[1] ?? ""}</td><td class="chg-n ${cls}" data-r="${r}">${l[2] ?? ""}</td>`
        + `<td class="chg-c ${cls}" data-r="${r}"><span class="chg-sign">${l[0] === " " ? " " : l[0]}</span>${esc(l[3])}</td></tr>`);
      body.push(commentsAt(ctx, fi, r, r + 1, split));
    }
  }
  const status = { added: "new", deleted: "deleted", renamed: "renamed" }[f.status];
  const editor = root && f.status !== "deleted" ? `<a class="link chg-editor" href="vscode://file/${esc(encodeURI(root + "/" + f.path))}" title="Open this file in VS Code">Open in editor</a>` : "";
  return `<section class="chg-file" id="chg-f-${fi}">
    <div class="chg-fhead"><span class="chg-path">${esc(f.path)}</span>${status ? chip("c-p", status) : ""}${f.untracked ? chip("c-p", "not in git") : ""}
      <span class="chg-plus">+${f.add}</span><span class="chg-minus">−${f.del}</span><span class="spacer"></span>${editor}</div>
    ${f.binary ? `<div class="muted t-m chg-note">A binary file.</div>` : ""}
    ${f.hunks.length ? `<div class="chg-scroll"><table class="chg-t${split ? " chg-split" : ""}">${split
      ? `<colgroup><col class="chg-cn"><col><col class="chg-cn"><col></colgroup>` : ""}${body.join("")}</table></div>` : ""}
    ${f.cut ? `<div class="muted t-m chg-note">The rest of this file is not shown: the diff is too long. ${editor}</div>` : ""}
  </section>`;
}

// The comments whose last line is one of the rows [from, to), and the open comment box when the selection ends there.
function commentsAt(ctx, fi, from, to, split) {
  const span = split ? 4 : 3;
  let html = "";
  ctx.comments.forEach((c, i) => {
    if (c.fi === fi && c.r >= from && c.r < to) html += `<tr class="chg-crow"><td colspan="${span}"><div class="chg-comment">
      <div class="t-s muted chg-where">${esc(c.file)}:${c.line}${c.end && c.end !== c.line ? "–" + c.end : ""}</div><div>${esc(c.text)}</div>
      ${button("Remove", { cls: "link", data: { "chg-del": i } })}</div></td></tr>`;
  });
  if (ctx.sel && ctx.sel.fi === fi && ctx.sel.to >= from && ctx.sel.to < to)
    html += `<tr class="chg-crow"><td colspan="${span}"><form class="chg-form">
      <textarea class="chg-text" rows="3" placeholder="A comment on this line for the agent"></textarea>
      <div class="actions">${button("Add comment", { kind: "primary", type: "submit" })}${button("Cancel", { data: { "chg-cancel": 1 } })}</div>
    </form></td></tr>`;
  return html;
}

function draw(box, src, ctx) {
  const d = ctx.data, split = !phone() && (store(LAYOUT) || "split") === "split";
  const keep = box.scrollTop;
  ctx.rows = [];
  const files = src.session ? d.files || [] : (d.commits || []).flatMap(c => c.files);
  const add = files.reduce((s, f) => s + f.add, 0), del = files.reduce((s, f) => s + f.del, 0);
  const root = src.session ? d.root : null;
  let fi = 0;
  const fileBlocks = src.session
    ? files.map(f => fileHtml(f, fi++, root, ctx, split)).join("")
    : (d.commits || []).map(c => `<div class="chg-commit"><span class="chg-sha">${esc(c.sha.slice(0, 8))}</span> ${esc(c.subject)}</div>`
        + c.files.map(f => fileHtml(f, fi++, c.root, ctx, split)).join("")).join("");
  const list = files.map((f, i) => `<a class="chg-li" href="#chg-f-${i}" data-goto="${i}"><span class="chg-path">${esc(f.path)}</span>`
    + `<span class="chg-plus">+${f.add}</span><span class="chg-minus">−${f.del}</span></a>`).join("");
  const state = d.state === "approved" ? chip("c-ready", "approved") : d.state === "changes" ? chip("c-waiting", "changes requested") : "";
  const gate = { push: "This project waits for Approve before a push.", release: "This project waits for Approve before a release." }[d.review_at] || "";
  const head = src.session
    ? `<div class="chg-bar" role="group" aria-label="Compare against">${BASES.map(([k, t]) =>
        button(t, { cls: "chg-base", pressed: ctx.base === k, data: { "chg-base": k } })).join("")}</div>
       <div class="t-m muted">Against ${esc(d.against || "")}. ${d.commits_ahead != null ? `${d.commits_ahead} commit${d.commits_ahead === 1 ? "" : "s"} not on GitHub.` : ""}</div>
       ${(d.shared || []).length ? `<div class="t-m chg-shared">This folder is shared with ${d.shared.map(esc).join(", ")}: the diff holds every session's changes here. The workspace setting gives each session its own folder and its own diff (#5856).</div>` : ""}`
    : `<div class="t-m"><b>#${d.item}</b> ${esc(d.title)}</div>
       <div class="t-m muted">${d.kind === "deploy" ? `What release #${d.item} ships: the commits of ${d.items.length} item${d.items.length === 1 ? "" : "s"}.` : "The commits whose message names #" + d.item + ", also after the push."}</div>
       ${d.waits ? `<div class="t-m chg-shared">This release waits for your Approve.</div>` : ""}`;
  const n = ctx.comments.length;
  const actions = `<div class="chg-acts">
    ${n ? button(`Send to agent (${n})`, { kind: "primary", data: { "chg-send": 1 }, title: "One message, with file and line" }) : ""}
    ${button("Request changes", { data: { "chg-mark": "changes" }, title: "Send the comments; the push stays held" })}
    ${button("Approve", { data: { "chg-mark": "approved" }, title: gate ? "The push or release waits for this" : "Mark these changes approved" })}
    ${src.session ? button("Reviewed", { data: { "chg-mark": "reviewed" }, title: "Start 'Since my last review' here" }) : ""}
    <span class="spacer"></span>
    ${phone() ? "" : `<span class="chg-bar" role="group" aria-label="Layout">${button("Side by side", { cls: "chg-lay", pressed: split, data: { "chg-lay": "split" } })}${button("One column", { cls: "chg-lay", pressed: !split, data: { "chg-lay": "unified" } })}</span>`}
  </div>`;
  box.innerHTML = `<div class="chg">${head}
    <div class="chg-sum t-m">${files.length} file${files.length === 1 ? "" : "s"} <span class="chg-plus">+${add}</span> <span class="chg-minus">−${del}</span> ${state} ${gate ? `<span class="muted">${esc(gate)}</span>` : ""}</div>
    ${actions}
    ${files.length ? `<div class="chg-list" role="list" aria-label="Changed files">${list}</div>` : `<div class="muted chg-note">No changes ${src.session ? "against " + esc(d.against || "this base") : "named #" + d.item + " in a commit yet"}.</div>`}
    ${d.cut ? `<div class="t-m muted chg-note">The diff is long: some lines are not shown.</div>` : ""}
    ${d.untracked_more ? `<div class="t-m muted chg-note">And ${d.untracked_more.count} more new files that git does not know: ${
      d.untracked_more.folders.map(([f, k]) => `<span class="chg-path">${esc(f)}</span> ${k}`).join(", ")}. Add them to .gitignore if they are not work.</div>` : ""}
    ${fileBlocks}</div>`;
  box.scrollTop = keep;
  const ta = box.querySelector(".chg-text");
  if (ta) ta.focus({ preventScroll: true });
}

function wire(box, src) {
  const ctx = ctxOf(src);
  const redraw = () => draw(box, src, ctx);
  box.addEventListener("click", async (e) => {
    const t = e.target;
    const go = t.closest("[data-goto]");
    if (go) { e.preventDefault(); const f = box.querySelector("#chg-f-" + go.dataset.goto); if (f) f.scrollIntoView({ block: "start" }); return; }
    const b = t.closest("[data-chg-base]");
    if (b) { ctx.base = b.dataset.chgBase; ctx.sel = null; return renderChanges(box, src); }
    const l = t.closest("[data-chg-lay]");
    if (l) { store(LAYOUT, l.dataset.chgLay); ctx.sel = null; return redraw(); }
    if (t.closest("[data-chg-cancel]")) { ctx.sel = null; return redraw(); }
    const del = t.closest("[data-chg-del]");
    if (del) { ctx.comments.splice(+del.dataset.chgDel, 1); return redraw(); }
    if (t.closest("[data-chg-send]")) return send(box, src, ctx, false);
    const m = t.closest("[data-chg-mark]");
    if (m) {
      const kind = m.dataset.chgMark;
      if (kind === "changes") return send(box, src, ctx, true);
      try {
        await act("review_mark", { kind, session: src.session, item: src.item, sha: src.session ? ctx.data.head : null });
        toast(kind === "approved" ? "Approved" : "Marked as reviewed");
        return renderChanges(box, src);
      } catch (err) { return; }
    }
    const cell = t.closest("[data-r]");
    if (cell && !window.getSelection().toString()) {
      const r = +cell.dataset.r, row = ctx.rows[r];
      if (!row || row.n == null) return;
      if (e.shiftKey && ctx.sel && ctx.sel.fi === row.fi) ctx.sel = { fi: row.fi, from: Math.min(ctx.sel.from, r), to: Math.max(ctx.sel.to, r) };
      else ctx.sel = { fi: row.fi, from: r, to: r };
      redraw();
    }
  });
  box.addEventListener("submit", (e) => {
    if (!e.target.closest(".chg-form")) return;
    e.preventDefault();
    const text = box.querySelector(".chg-text").value.trim();
    if (!text) return toast("Write the comment first", true);
    const a = ctx.rows[ctx.sel.from], z = ctx.rows[ctx.sel.to];
    const quote = ctx.rows.slice(ctx.sel.from, ctx.sel.to + 1).map(x => x.text).join(" ").trim();
    ctx.comments.push({ fi: a.fi, r: ctx.sel.to, file: a.file, line: a.n, end: z.n, side: a.side, quote, text });
    ctx.sel = null;
    redraw();
  });
  box.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && e.target.closest(".chg-text")) e.target.form.requestSubmit();
    if (e.key === "Escape" && e.target.closest(".chg-text")) { e.stopPropagation(); ctx.sel = null; redraw(); }
  });
}

async function send(box, src, ctx, request) {
  if (!ctx.comments.length) return toast(request ? "Add a comment first: click a line, then say what to change" : "No comments yet", true);
  try {
    await act("review_send", { comments: ctx.comments.map(({ file, line, end, quote, text, side }) =>
      ({ file, line: side === "old" ? `${line} (old)` : line, end, quote, text })), session: src.session, item: src.item,
      request, sha: src.session ? ctx.data.head : null });
  } catch (e) { return; }
  toast(request ? "Changes requested: the comments went to the agent" : "Sent to the agent");
  ctx.comments = [];
  return renderChanges(box, src);
}

// An item's or a release's changes in a large dialog (the item drawer's Changes button).
let dlg = null;
export function openItemChanges(id) {
  let el = document.getElementById("changesDlg");
  if (!el) {
    el = document.createElement("div");
    el.id = "changesDlg";
    el.className = "hidden";
    el.innerHTML = `<div class="box"><div class="dlg-head"><h2>Changes</h2>${button("✕", { kind: "icon", label: "Close", data: { "chg-close": 1 } })}</div><div class="chg-dbody"></div></div>`;
    document.body.append(el);
    dlg = makeDialog(el);
    el.addEventListener("click", (e) => { if (e.target.closest("[data-chg-close]")) dlg.close(); });
  }
  dlg.open();
  const body = el.querySelector(".chg-dbody");
  const fresh = body.cloneNode(false);  // a new body: its listeners are for this item only
  body.replaceWith(fresh);
  return renderChanges(fresh, { item: id });
}
