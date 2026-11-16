// The outline of a design document (#6013, design-doc-mockup v7): its parts (the level-2 headings, as maxpm canvas
// design writes them: Brief, Discovery, Decisions, Plan, Risks, #5989) with the state of each: being written (its
// text is still "writing: ..."), with open decisions, or written. The side panel shows them as a row at the top of the
// document, and Full screen (/view) as a column at its side; a click goes to the part. A canvas with fewer than three
// parts has no outline.

const slug = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 40) || "part";

// [{id, title, state, open}] of the drawn document under root; each part's heading gets an id when it has none.
export function docParts(root) {
  const heads = [...root.querySelectorAll(":scope > h2")];
  if (heads.length < 3) return [];
  const used = new Set();
  return heads.map((h, i) => {
    const words = h.cloneNode(true); words.querySelectorAll(".doc-edit").forEach(x => x.remove());  // not its Edit link
    const title = words.textContent.trim();
    if (!h.id) { let id = "part-" + slug(title); while (used.has(id) || document.getElementById(id)) id += "-" + (i + 1); h.id = id; }
    used.add(h.id);
    let open = 0, writing = false;
    for (let el = h.nextElementSibling; el && el.tagName !== "H2"; el = el.nextElementSibling) {
      if (el.classList.contains("doc-pending") || /^writing:\s/i.test(el.textContent.trim())) writing = true;
      if (el.classList.contains("doc-decision") && !el.querySelector(".doc-answered")) open++;
    }
    return { id: h.id, title, state: writing ? "writing" : open ? "open" : "written", open };
  });
}

const stateText = (p) => p.state === "writing" ? "being written" : p.state === "open" ? `${p.open} open` : "written";

// The row (or, with column: true, the column) of the parts. Plain text only: esc comes from the caller's page.
export function outlineHtml(parts, esc, { column = false } = {}) {
  if (!parts.length) return "";
  return `<nav class="doc-outline${column ? " col" : ""}" aria-label="Document parts">${parts.map(p =>
    `<a class="doc-part ${p.state}" href="#${esc(p.id)}" data-doc-part="${esc(p.id)}" title="${esc(stateText(p))}">`
    + `<span class="doc-dot" aria-hidden="true"></span>${esc(p.title)}<span class="doc-ps">${esc(stateText(p))}</span></a>`).join("")}</nav>`;
}
