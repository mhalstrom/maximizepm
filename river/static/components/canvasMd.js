// A Markdown canvas with pictures and diagrams (#4983). The text comes from a session, so it is drawn as the chat
// draws Markdown (marked, then DOMPurify), with two more things:
// - pictures: an <img> whose address is a data: picture (maxpm canvas put puts each local picture inline). Any
//   other address is dropped, so a canvas loads nothing from the network here;
// - diagrams: each ```mermaid block is drawn by a mermaid of its own (securityLevel strict, apart from the Graph
//   tab's), and shown as a picture (<img> of the SVG), so the diagram's text can run nothing in this page.
import { marked } from "../vendor/marked/marked.esm.js";
import DOMPurify from "../vendor/dompurify/purify.es.mjs";

const PURIFY = { FORBID_TAGS: ["style", "form", "input", "button", "textarea", "select", "iframe", "video", "audio", "svg", "math"],
                 FORBID_ATTR: ["style"] };
let pictures = false;  // set only while canvasMarkdown sanitizes: the chat keeps its own rules
DOMPurify.addHook("uponSanitizeAttribute", (n, data) => {
  if (pictures && n.tagName === "IMG" && data.attrName === "src" && !/^data:image\/(png|jpe?g|gif|webp|svg\+xml);/i.test(data.attrValue || "")) data.keepAttr = false;
});

export function canvasMarkdown(text) {
  pictures = true;
  try { return DOMPurify.sanitize(marked.parse(String(text || "")), PURIFY); } finally { pictures = false; }
}

const MERMAID = "https://cdn.jsdelivr.net/npm/mermaid@11.17.2/dist/mermaid.esm.min.mjs";
let mermaidOwn = null;
function dark() {
  const t = document.documentElement.dataset.theme;
  return t === "dark" || (t !== "light" && matchMedia("(prefers-color-scheme: dark)").matches);
}
async function ownMermaid() {
  if (!mermaidOwn) mermaidOwn = import(MERMAID).then(m => m.default).catch((e) => { mermaidOwn = null; throw e; });
  const m = await mermaidOwn;
  m.initialize({ startOnLoad: false, securityLevel: "strict", theme: dark() ? "dark" : "default", flowchart: { useMaxWidth: true } });
  return m;
}

let seq = 0;
// Draws each ```mermaid block under root that is not drawn yet. A block that mermaid cannot read stays as text, with
// the reason under it.
export async function drawDiagrams(root) {
  const blocks = [...root.querySelectorAll("pre > code.language-mermaid")].filter(c => !c.closest(".cv-diagram"));
  if (!blocks.length) return;
  let m;
  try { m = await ownMermaid(); } catch (e) { return; }  // no network: the blocks stay as text
  for (const code of blocks) {
    const pre = code.parentElement; if (!pre.parentNode) continue;
    try {
      const { svg } = await m.render("cvmd" + (++seq), code.textContent);
      const fig = document.createElement("figure"); fig.className = "cv-diagram";
      const img = document.createElement("img"); img.alt = "Diagram"; img.title = code.textContent.slice(0, 300);
      img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
      fig.append(img); pre.replaceWith(fig);
    } catch (e) {
      const why = document.createElement("div"); why.className = "muted";
      why.textContent = "This diagram cannot be drawn: " + String(e && e.message || e).split("\n")[0].slice(0, 200);
      pre.after(why);
      document.querySelectorAll("[id^=dcvmd]").forEach(x => x.remove());  // what mermaid left after an error
    }
  }
}
