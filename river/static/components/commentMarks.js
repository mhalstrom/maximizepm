// The marks of canvas comments in the text (#6134, mark 2026-11-18: 'comments should have an indication in line which
// you can click on to view in line'). Each comment on a part of the page gets a highlight of its quote (or of the
// part, when it has no quote) and a small comment sign after it; a click on either opens the comment's thread beside
// the part (components/canvasComments.js draws the thread, components/sidePanel.js places it).
// This file runs in two places, as findText.js does: the side panel imports it for a Markdown canvas, and maxpm serve
// puts it inline in the helper script of each HTML canvas (routes/canvas.py takes out the word "export"). So it is
// plain ES5, with no import.
// A quote is found by its letters only (spaces and line ends do not count): a selection across two paragraphs says
// "Title Para" where the text nodes say "Title" and "Para".

var CMT_SKIP = ".doc-edit, .doc-answer, .maxpm-cmt-pin, script, style, noscript, template, textarea, select";
var CMT_VOID = /^(IMG|HR|BR|INPUT|VIDEO|AUDIO|CANVAS|IFRAME|SVG)$/i;
var CMT_BLOCKS = /^(UL|OL|DL|TABLE|THEAD|TBODY|TFOOT|TR)$/;
var CMT_SIGN = '<svg viewBox="0 0 16 16" width="1em" height="1em" aria-hidden="true" focusable="false">'
  + '<path fill="currentColor" d="M2 2.5A1.5 1.5 0 0 1 3.5 1h9A1.5 1.5 0 0 1 14 2.5v7a1.5 1.5 0 0 1-1.5 1.5H7.2L4 14v-3h-.5'
  + 'A1.5 1.5 0 0 1 2 9.5z"/></svg>';

// Take the marks and signs away and join the text again (a find mark inside stays).
export function marksClear(root) {
  if (!root) return;
  var pins = root.querySelectorAll(".maxpm-cmt-pin");
  for (var i = 0; i < pins.length; i++) if (pins[i].parentNode) pins[i].parentNode.removeChild(pins[i]);
  var parts = root.querySelectorAll(".maxpm-cmt-part");
  for (var j = 0; j < parts.length; j++) parts[j].classList.remove("maxpm-cmt-part", "maxpm-cmt-done", "maxpm-cmt-draft", "maxpm-cmt-on");
  var marks = root.querySelectorAll("mark.maxpm-cmt"), parents = [];
  for (var k = marks.length - 1; k >= 0; k--) {
    var m = marks[k], p = m.parentNode;
    if (!p) continue;
    while (m.firstChild) p.insertBefore(m.firstChild, m);
    p.removeChild(m);
    if (parents.indexOf(p) < 0) parents.push(p);
  }
  for (var n = 0; n < parents.length; n++) parents[n].normalize();
}

// The element of the page under a click or a hover: never a mark or a sign of a comment (a new comment's path names
// the page's own element, so that it still holds when the marks are drawn again).
export function markOwner(el) {
  while (el && el.closest && el.closest("mark.maxpm-cmt, .maxpm-cmt-pin")) el = el.closest("mark.maxpm-cmt, .maxpm-cmt-pin").parentElement;
  return el;
}

// The comment that a click opens: the id of the mark or sign under it, or 0. A link inside a mark still opens the link.
export function markAt(el) {
  if (!el || !el.closest) return 0;
  var pin = el.closest(".maxpm-cmt-pin");
  if (pin) return +pin.getAttribute("data-maxpm-cmt") || 0;
  if (el.closest("a[href], button, input, textarea, select, summary")) return 0;
  var m = el.closest("mark.maxpm-cmt");
  return m ? +m.getAttribute("data-maxpm-cmt") || 0 : 0;
}

// Wrap the letters of quote inside el in marks; returns the marks (none when el does not hold the quote).
function cmtWrap(el, quote, cls, id) {
  var doc = el.ownerDocument || document, walk = doc.createTreeWalker(el, 4 /* NodeFilter.SHOW_TEXT */, null);
  var nodes = [], s = "", at = [], n, k, c;
  while ((n = walk.nextNode())) {
    var p = n.parentNode;
    if (!p || (p.closest && p.closest(CMT_SKIP))) continue;
    for (k = 0; k < n.nodeValue.length; k++) {
      c = n.nodeValue.charAt(k);
      if (/\s/.test(c)) continue;
      s += c; at.push([nodes.length, k]);
    }
    nodes.push(n);
  }
  var q = String(quote || "").replace(/\s+/g, "");
  if (!q) return [];
  var i = s.indexOf(q);
  if (i < 0) i = s.toLowerCase().indexOf(q.toLowerCase());
  if (i < 0) return [];
  var first = at[i], last = at[i + q.length - 1], out = [];
  for (var x = last[0]; x >= first[0]; x--) {
    var node = nodes[x], a = x === first[0] ? first[1] : 0, b = x === last[0] ? last[1] + 1 : node.nodeValue.length;
    if (!node.nodeValue.slice(a, b).trim()) continue;
    if (b < node.nodeValue.length) node.splitText(b);
    var mid = a > 0 ? node.splitText(a) : node;
    var m = doc.createElement("mark");
    m.className = cls; m.setAttribute("data-maxpm-cmt", String(id));
    mid.parentNode.replaceChild(m, mid);
    m.appendChild(mid);
    out.unshift(m);
  }
  return out;
}

function cmtPin(doc, c, cls) {
  var b = doc.createElement("button");
  b.type = "button"; b.className = "maxpm-cmt-pin" + cls; b.setAttribute("data-maxpm-cmt", String(c.id));
  b.setAttribute("aria-label", "Comment c" + c.id); b.title = "Comment c" + c.id;
  b.innerHTML = CMT_SIGN;
  return b;
}

// Draw the marks of list ([{id, path, quote, done, draft}], the comments of the page) in root; open: the id of the
// thread that shows. A comment whose part or quote this version does not hold gets no mark (the list under the page
// still has it). Returns the number of comments marked.
export function marksDraw(root, list, open) {
  marksClear(root);
  if (!root || !list) return 0;
  var doc = root.ownerDocument || document, count = 0;
  for (var i = 0; i < list.length; i++) {
    var c = list[i], el = null;
    if (!c || !c.path) continue;
    try { el = root.querySelector(c.path); } catch (e) { el = null; }
    if (el && el.closest && el.closest(".maxpm-cmt-pin")) el = null;
    var cls = (c.done ? " maxpm-cmt-done" : "") + (c.draft ? " maxpm-cmt-draft" : "") + (+c.id === +open ? " maxpm-cmt-on" : "");
    var marks = [];
    if (c.quote) {
      marks = el ? cmtWrap(el, c.quote, "maxpm-cmt" + cls, c.id) : [];
      if (!marks.length) marks = cmtWrap(root, c.quote, "maxpm-cmt" + cls, c.id);  // the part moved in a new version
      if (!marks.length) continue;
      var lastMark = marks[marks.length - 1];
      lastMark.parentNode.insertBefore(cmtPin(doc, c, cls), lastMark.nextSibling);
    } else {
      if (!el || el === root || el === doc.body || el === doc.documentElement) continue;
      var names = ("maxpm-cmt-part" + cls).split(" ");
      for (var w = 0; w < names.length; w++) if (names[w]) el.classList.add(names[w]);
      var pin = cmtPin(doc, c, cls);
      if (CMT_VOID.test(el.tagName)) el.parentNode.insertBefore(pin, el.nextSibling);
      else if (CMT_BLOCKS.test(el.tagName)) el.parentNode.insertBefore(pin, el);
      else el.appendChild(pin);
    }
    count++;
  }
  return count;
}

// The thread of id shows (0: none): its marks stand out.
export function marksOpen(root, id) {
  if (!root) return;
  var all = root.querySelectorAll("[data-maxpm-cmt], .maxpm-cmt-part");
  for (var i = 0; i < all.length; i++) all[i].classList.remove("maxpm-cmt-on");
  if (!id) return;
  var on = root.querySelectorAll('[data-maxpm-cmt="' + (+id) + '"]');
  for (var j = 0; j < on.length; j++) {
    on[j].classList.add("maxpm-cmt-on");
    if (on[j].classList.contains("maxpm-cmt-pin") && on[j].parentNode && on[j].parentNode.classList
        && on[j].parentNode.classList.contains("maxpm-cmt-part")) on[j].parentNode.classList.add("maxpm-cmt-on");
  }
}

// Where the thread of id goes: the box of its marks and sign, in the coordinates of the window of root (null when
// this version has no mark of it).
export function markRect(root, id) {
  if (!root) return null;
  var els = root.querySelectorAll('[data-maxpm-cmt="' + (+id) + '"]'), top = Infinity, bottom = -Infinity, left = Infinity;
  for (var i = 0; i < els.length; i++) {
    var r = els[i].getBoundingClientRect();
    if (!r.width && !r.height) continue;
    if (r.top < top) top = r.top; if (r.bottom > bottom) bottom = r.bottom; if (r.left < left) left = r.left;
  }
  return top === Infinity ? null : { top: top, bottom: bottom, left: left };
}
