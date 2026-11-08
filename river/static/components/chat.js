// A session as a chat (#4604): the person's turns on the right in a bubble, the agent's replies as markdown, each
// tool call folded to one line that opens on a click (its input, and its result when it comes), and small lines
// for events. The transcript lines come from GET /api/session (river/sessions.py).
// Markdown: marked, then DOMPurify, because an agent's text can hold any HTML. The person's text and every tool
// text go in as text, never as HTML.
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
    m.append(el("div", "bubble", e.text));
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
export function appendEntries(col, entries) {
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
    col.append(entryNode(e));
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
