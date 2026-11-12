// A session as a chat (#4604): the person's turns on the right in a bubble, both sides' text as markdown, each
// tool call folded to one line that opens on a click (its input, and its result when it comes), and small lines
// for events. The transcript lines come from GET /api/session (river/sessions.py).
// Markdown: marked, then DOMPurify, because a turn's text can hold any HTML. Every tool text goes in as text, never
// as HTML. A #<number> that names a queue item links to its drawer (linkRefs, #4609).
import { marked } from "../vendor/marked/marked.esm.js";
import DOMPurify from "../vendor/dompurify/purify.es.mjs";
import { copyText, toast } from "../lib.js";
import { button } from "./button.js";

marked.setOptions({ gfm: true, breaks: false });
const PURIFY = { FORBID_TAGS: ["img", "style", "form", "input", "button", "textarea", "select", "iframe", "video", "audio", "svg", "math"],
                 FORBID_ATTR: ["style"] };
// A file:// link (#4689): DOMPurify drops its href; its path stays as data-file, and linkPaths makes it a link
// that opens in the side panel.
DOMPurify.addHook("uponSanitizeAttribute", (n, data) => {
  if (data.attrName === "href" && /^file:/i.test(data.attrValue || "")) {
    try { n.setAttribute("data-file", decodeURIComponent(new URL(data.attrValue).pathname)); } catch (e) { /* no path */ }
  }
});
DOMPurify.addHook("afterSanitizeAttributes", (n) => {
  if (n.tagName === "A") { n.setAttribute("target", "_blank"); n.setAttribute("rel", "noopener noreferrer"); }
});

export function markdown(text) {
  return DOMPurify.sanitize(marked.parse(String(text || "")), PURIFY);
}

// Markdown as plain text: the left bar's last line shows no markdown marks.
export function plainText(text) {
  const d = document.createElement("div");
  d.innerHTML = markdown(text);  // sanitized by DOMPurify; only its text is used
  return d.textContent.replace(/\s+/g, " ").trim();
}

// Each #<number> that names a queue item (refs: {id: {title, status, assignee}}, from GET /api/session) becomes a
// link that opens the item in the drawer (#4609), with a pop-up on hover and focus (refPopup, #4611). Text in code,
// a link or a button stays as it is.
const REF = /(^|[^\w&])#(\d{1,7})\b/g, HAS_REF = /(^|[^\w&])#\d{1,7}\b/;
// A number after one of these words is no item (#4618): Claude Code's "[Image #1]", "step #2", "option #3".
const NOT_ITEM = /\b(image|step|option|line|no\.?|number|round|attempt|part|phase|page|slide|pr|pull request|issue|comment|row|column|tab|panel|card)\s*\[?$/i;
export function linkRefs(root, refs) {
  if (!refs || !Object.keys(refs).length) return;
  const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => n.parentElement.closest("code, pre, a, button, .link") ? NodeFilter.FILTER_REJECT
      : HAS_REF.test(n.data) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP });
  const nodes = [];
  for (let n; (n = walk.nextNode());) nodes.push(n);
  for (const n of nodes) {
    const frag = document.createDocumentFragment();
    let at = 0;
    n.data.replace(REF, (m, pre, id, off) => {
      if (!(id in refs) || NOT_ITEM.test(n.data.slice(Math.max(0, off - 24), off + pre.length))) return m;
      frag.append(n.data.slice(at, off + pre.length));
      const a = el("span", "link ref", "#" + id);
      a.dataset.open = id; a.dataset.ref = id; a.tabIndex = 0;
      a.setAttribute("aria-label", `#${id} ${refs[id].title}`);
      frag.append(a);
      at = off + m.length;
      return m;
    });
    if (!at) continue;
    frag.append(n.data.slice(at));
    n.replaceWith(frag);
  }
}

// A path to a file the side panel can show becomes a link that opens it there (#4619): a full or ~ path, a file://
// link, and (#4689) a relative path or a bare file name, which the server looks for in the session's folder and
// among its files (components/artLinks.js checks each one; a name it finds nothing for goes back to plain text).
// A local page address in plain text (http://localhost, 127.0.0.1) links too. Text in a link, a button or a code
// block stays as it is (inPre: a code block links too, for the text of a terminal); a path in inline code links.
const EXT = "(?:png|jpe?g|gif|webp|svg|pdf|html?|md|txt)";
const END = "(?=$|[\\s)`'\",:;!?\\]]|\\.(?:\\s|$))";
const FPATH = new RegExp(`(^|[\\s(\`'"=\\[])(?:file://(?:localhost)?)?((?:[a-zA-Z]:[/\\\\]|\\\\\\\\|~[/\\\\]|\\/)(?:[\\w.@~+-]+[/\\\\])*[\\w@~+-][\\w.@~+-]*\\.${EXT})${END}`, "g");
const RPATH = new RegExp(`(^|[\\s(\`'"=\\[])((?:\\.{1,2}/)?(?:[\\w.@~+-]+/)*[\\w@~+-][\\w.@~+-]*\\.${EXT})${END}`, "g");
const LURL = /(^|[\s(`'"=[])(https?:\/\/(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?(?:\/[^\s)\]>'"`]*)?)/g;
function linkText(root, test, rx, make, inPre) {
  const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => n.parentElement.closest(inPre ? "a, button, .link, .fpath-no" : "pre, a, button, .link, .fpath-no")
      ? NodeFilter.FILTER_REJECT : test.test(n.data) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP });
  const nodes = [];
  for (let n; (n = walk.nextNode());) nodes.push(n);
  for (const n of nodes) {
    const frag = document.createDocumentFragment();
    let at = 0;
    n.data.replace(rx, (m, pre, what, off) => {
      const text = m.slice(pre.length).replace(/[.,;:]+$/, "");  // a sentence's end is no part of a path
      frag.append(n.data.slice(at, off + pre.length));
      frag.append(make(what.replace(/[.,;:]+$/, ""), text));
      at = off + pre.length + text.length;
      return m;
    });
    if (!at) continue;
    frag.append(n.data.slice(at));
    n.replaceWith(frag);
  }
}
function fileLink(path, text, rel) {
  const a = el("span", "link fpath", text);
  a.dataset.file = path; a.title = "Open in the side panel"; a.tabIndex = 0;
  if (rel) a.dataset.rel = "1";
  return a;
}
export function linkPaths(root, { inPre = false } = {}) {
  root.querySelectorAll("a[data-file]").forEach((a) => a.replaceWith(fileLink(a.dataset.file, a.textContent)));
  linkText(root, /[~\/\\][^\s]*\.\w{2,4}/, FPATH, (path, text) => fileLink(path, text), inPre);
  linkText(root, /\.\w{2,4}\b/, RPATH, (path, text) => fileLink(path, text, true), inPre);
  linkText(root, /https?:\/\/(localhost|127\.0\.0\.1|\[::1\])/, LURL, (url) => {
    const a = el("span", "link fpath", url);
    a.dataset.url = url; a.title = "Open in the side panel"; a.tabIndex = 0;
    return a;
  }, inPre);
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

// A Copy button on each code block of a reply.
function addCopy(root) {
  root.querySelectorAll("pre").forEach((pre) => {
    const b = el("button", "copy", "Copy");
    b.type = "button"; b.title = "Copy the code";
    pre.append(b);
  });
}

function entryNode(e) {
  if (e.role === "user") {
    const m = el("div", "msg msg-user" + (e.sent ? " sent" : ""));
    const srcs = [...(e.localImages || []), ...(e.images || []).map(p => "api/upload/" + p.split("/").map(encodeURIComponent).join("/"))];
    if (srcs.length) {
      const g = el("div", "msg-imgs");
      for (const src of srcs) {
        const img = el("img"); img.src = src; img.alt = "Image sent to the session"; img.loading = "lazy";
        g.append(img);
      }
      m.append(g);
    } else if (e.image_count) {
      m.append(el("div", "msg-note", `${e.image_count} image${e.image_count > 1 ? "s" : ""} attached`));
    }
    if (e.files?.length) {
      const g = el("div", "msg-files");
      for (const f of e.files) {
        const a = el("a", "", f.name);
        a.href = "api/upload/" + f.path.split("/").map(encodeURIComponent).join("/");
        a.download = f.name;
        g.append(a);
      }
      m.append(g);
    }
    if (e.text) {
      const b = el("div", "bubble md");
      b.innerHTML = markdown(e.text);  // sanitized by DOMPurify
      addCopy(b);
      m.append(b);
    }
    if (e.sent) m.append(el("div", "msg-note", "sent to its terminal"));
    if (e.sent && e.command) commandNote(m, e.command, e.terminal);
    if (e.sent && e.track && norm(e.text)) {
      m.dataset.pending = norm(e.text); m.dataset.sentAt = Date.now();
      if (e.command) m.dataset.command = "1";  // no chat log line may come for it: the note says what it did
    }
    return m;
  }
  // What a slash command printed (#5220: Claude Code keeps it in its chat log), folded under one line.
  if (e.role === "output") {
    const d = el("details", "cmd-out");
    d.append(el("summary", "", "Output"));
    d.append(el("pre", "", e.text));
    return d;
  }
  if (e.role === "assistant") {
    const m = el("div", "msg msg-agent md");
    m.innerHTML = markdown(e.text);  // sanitized by DOMPurify
    addCopy(m);
    return m;
  }
  if (e.role === "tool") {
    const d = el("details", "tool");
    if (e.id) d.dataset.tool = e.id;
    const s = el("summary");
    const colon = e.text.indexOf(": ");
    s.append(el("span", "tname", colon > 0 ? e.text.slice(0, colon) : e.text));
    if (colon > 0) s.append(el("span", "twhat", e.text.slice(colon + 2)));
    d.append(s);
    const body = el("div", "tool-body");
    body.append(el("pre", "tool-in", e.detail || e.text));
    const out = el("pre", "tool-out");
    out.hidden = true;
    body.append(out);
    d.append(body);
    return d;
  }
  // A compaction or a resume (#5120): one thin line across the chat with what happened and when; the start of the
  // session the same, with no time. The summary of a compaction is folded under it, closed.
  if (e.role === "divider") {
    const d = el("div", "chat-div", e.at ? `${e.text} · ${when(e.at)}` : e.text);
    d.setAttribute("role", "separator");
    if (e.kind) d.dataset.kind = e.kind;
    return d;
  }
  if (e.role === "summary") {
    const d = el("details", "chat-summary");
    d.append(el("summary", "", "Summary"));
    const b = el("div", "md");
    b.innerHTML = markdown(e.text);  // sanitized by DOMPurify
    d.append(b);
    return d;
  }
  return el("div", "msg-event", e.text);
}
const when = (at) => new Date(at).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });

// Add transcript lines to the chat column. A tool result fills the tool call it belongs to (when that call is
// shown); it never shows as a line of its own.
// The page shows a message it sent at once (pending); when the transcript holds the same turn, that turn takes its
// place, so the message shows once (#4618). Claude Code puts "[Image #1]" before the text of a turn with an image.
const norm = (t) => String(t || "").replace(/\[Image #\d+\]/g, "").replace(/\s+/g, " ").trim();
function takePending(col, e) {
  if (e.role !== "user" || e.sent) return;
  const got = norm(e.text);
  const m = [...col.querySelectorAll(".msg-user[data-pending]")].find(x => x.dataset.pending === got);
  if (m) m.remove();
}
// What a slash command sent from the message box did (#5220): the server looked at the session's pane after its
// Enter (session_send's command: {name, run, taken}). taken: "menu" (the ! dialog answers it), "waiting" (it is still
// in the agent's input box), or "ran". A command whose run is "terminal" shows its view only in the terminal: the
// note links to it (data-term; the view opens the terminal).
const RAN = { menu: "opened a menu: choose in the dialog", waiting: "still in its input box: not taken", ran: "ran" };
function commandNote(m, c, agent) {
  const note = m.querySelector(".msg-note");
  note.textContent = c.taken === "waiting" ? `sent to its terminal; ${RAN.waiting}`
    : c.run === "terminal" ? `/${c.name} shows its view in the terminal. `
    : `sent to its terminal; /${c.name} ${RAN[c.taken] || RAN.ran}`;
  if (c.run === "terminal" && c.taken !== "waiting" && agent) {
    const a = el("span", "link", "Open the terminal");
    a.dataset.term = agent; a.tabIndex = 0; a.setAttribute("role", "button");
    note.append(a);
  }
}

// A sent message that the transcript has not shown after PENDING_MAX says so; not a slash command (#5220), whose note
// says what it did.
const PENDING_MAX = 30000;
// Messages that maxpm serve holds while the session runs a turn (#5587, mark 2026-11-12: 'a little small button under
// the messages that are waiting'): each as a muted bubble, with 'Waits for the turn · Send now' under it. Send now
// (data-now: the message's id) stops the turn and sends that message at once; the others keep waiting. They come
// from the server (GET /api/session: pending), so a reload shows them again.
export function drawWaiting(host, rows) {
  host.replaceChildren(...rows.map((p) => {
    const files = (p.files || []).map(f => ({ path: f.split(/[\\/]/).slice(-2).join("/"),
                                              name: f.split(/[\\/]/).pop().replace(/^\d{8}-\d{6}-[a-f0-9]{8}-/, "") }));
    const m = entryNode({ role: "user", text: p.text, files });
    m.classList.add("waiting");
    m.dataset.wait = p.id;
    const n = el("div", "msg-note");
    n.innerHTML = p.state === "sending" ? "goes to its terminal now"
      : "Waits for the turn · " + button("Send now", { cls: "msg-now", data: { now: p.id }, title: "Stop and send this" });
    m.append(n);
    return m;
  }));
}

export function agePending(col) {
  for (const m of col.querySelectorAll(".msg-user[data-pending]:not([data-command])")) {
    const note = m.querySelector(".msg-note");
    if (note && Date.now() - +m.dataset.sentAt > PENDING_MAX) note.textContent = "sent to its terminal; not taken yet";
  }
}

export function appendEntries(col, entries, refs) {
  for (const e of entries) {
    takePending(col, e);
    if (e.role === "result") {
      const d = e.for && col.querySelector(`details.tool[data-tool="${CSS.escape(e.for)}"]`);
      if (!d) continue;
      const out = d.querySelector(".tool-out");
      out.textContent = e.text || "(no output)";
      out.hidden = false;
      d.classList.toggle("err", !!e.error);
      continue;
    }
    const node = entryNode(e);
    linkRefs(node, refs);
    linkPaths(node);
    if (e.role === "tool" || e.role === "event") toolGroup(col).append(node), countGroup(col.lastElementChild);
    else col.append(node);
  }
}

// Earlier lines at the top of the chat (#5120: scrolling up): drawn as appendEntries draws them, then put before the
// first line. With none left before them (start), the chat begins with 'Start of the session'.
export function prependEntries(col, entries, refs, start = false) {
  const tmp = document.createElement("div");
  if (start) appendEntries(tmp, [{ role: "divider", kind: "start", text: "Start of the session" }]);
  appendEntries(tmp, entries, refs);
  col.prepend(...tmp.childNodes);
}

// Each run of tool calls (and "a background task ended" lines) sits in one group (#4635). With the chat's tool
// calls hidden (class hide-tools), the group shows only its marker ("12 tool calls"), which opens it.
function toolGroup(col) {
  const last = col.lastElementChild;
  if (last && last.classList.contains("tool-group")) return last;
  const g = el("div", "tool-group");
  const b = el("button", "tg-sum");
  b.type = "button";
  b.setAttribute("aria-expanded", "false");
  b.onclick = () => { const o = g.classList.toggle("open"); b.setAttribute("aria-expanded", String(o)); countGroup(g); };
  g.append(b);
  col.append(g);
  return g;
}
function countGroup(g) {
  const n = g.querySelectorAll(":scope > details.tool").length, ev = g.querySelectorAll(":scope > .msg-event").length;
  const parts = [n ? `${n} tool call${n > 1 ? "s" : ""}` : "", ev ? `${ev} background task${ev > 1 ? "s" : ""} ended` : ""].filter(Boolean);
  g.querySelector(".tg-sum").textContent = `${g.classList.contains("open") ? "▾" : "▸"} ${parts.join(", ")}`;
}

export function wireCopy(col) {
  col.addEventListener("click", async (ev) => {
    const b = ev.target.closest("button.copy"); if (!b) return;
    const pre = b.closest("pre"), code = pre.querySelector("code");
    const text = code ? code.textContent : [...pre.childNodes].filter(n => n !== b).map(n => n.textContent).join("");
    if (await copyText(text)) { b.textContent = "Copied"; setTimeout(() => { b.textContent = "Copy"; }, 1500); }
    else toast("Could not copy", true);
  });
}
