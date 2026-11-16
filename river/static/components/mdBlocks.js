// The blocks of a Markdown text, for Edit in place of a document (#6004, design-doc-mockup v7): a paragraph, a list, a
// table, a quote (lines up to a blank line), a heading (one line), or a fenced code block (to its closing fence, blank
// lines and all). Each block knows its lines, so the page edits one block and puts the whole text back as it was
// around it.

// [{start, end, src}]: start and end are line numbers (end: the first line after the block).
export function mdBlocks(text) {
  const lines = String(text || "").split("\n"), out = [];
  let i = 0;
  while (i < lines.length) {
    if (!lines[i].trim()) { i++; continue; }
    const start = i, fence = /^\s*(```|~~~)/.exec(lines[i]);
    if (fence) {
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(fence[1])) i++;
      i = Math.min(i + 1, lines.length);
    } else if (/^#{1,6}\s/.test(lines[i])) {
      i++;
    } else {
      while (i < lines.length && lines[i].trim() && !/^#{1,6}\s/.test(lines[i]) && !/^\s*(```|~~~)/.test(lines[i])) i++;
    }
    out.push({ start, end: i, src: lines.slice(start, i).join("\n") });
  }
  return out;
}

// The text with one block replaced by src (an empty src takes the block out, with its blank line).
export function spliceBlock(text, block, src) {
  const lines = String(text || "").split("\n");
  const add = String(src || "").replace(/\s+$/, "");
  if (!add) {
    const end = block.end < lines.length && !lines[block.end].trim() ? block.end + 1 : block.end;
    lines.splice(block.start, end - block.start);
  } else {
    lines.splice(block.start, block.end - block.start, ...add.split("\n"));
  }
  return lines.join("\n");
}
