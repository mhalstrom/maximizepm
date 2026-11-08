// A session as a chat (#4604): the person's turns on the right in a bubble, both sides' text as markdown, each
// tool call folded to one line that opens on a click (its input, and its result when it comes), and small lines
// for events. The transcript lines come from GET /api/session (river/sessions.py).
// Markdown: marked, then DOMPurify, because a turn's text can hold any HTML. Every tool text goes in as text, never
// as HTML. A #<number> that names a queue item links to its drawer (linkRefs, #4609).
import { marked } from "../vendor/marked/marked.esm.js";
import DOMPurify from "../vendor/dompurify/purify.es.mjs";
import { copyText, toast } from "../lib.js";

marked.setOptions({ gfm: true, breaks: false });
const PURIFY = { FORBID_TAGS: ["img", "style", "form", "input", "button", "textarea", "select", "iframe", "video", "audio", "svg", "math"],
                 FORBID_ATTR: ["style"] };
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
      if (!(id in refs)) return m;
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
    if (e.text) {
      const b = el("div", "bubble md");
      b.innerHTML = markdown(e.text);  // sanitized by DOMPurify
      addCopy(b);
      m.append(b);
    }
    if (e.sent) m.append(el("div", "msg-note", "sent to its terminal"));
    return m;
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
  return el("div", "msg-event", e.text);
}

// Add transcript lines to the chat column. A tool result fills the tool call it belongs to (when that call is
// shown); it never shows as a line of its own.
export function appendEntries(col, entries, refs) {
  for (const e of entries) {
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
    col.append(node);
  }
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
