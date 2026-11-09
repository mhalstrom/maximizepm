// Find text in a part of a page (#4877): mark each place where the words show, and go from one to the next.
// This file runs in two places: the side panel imports it (components/sidePanel.js), and maxpm serve puts it inline
// in the helper script of each HTML canvas (routes/canvas.py, which takes out the word "export"), because the
// canvas runs in an origin of its own that the panel cannot read. So it is plain ES5, with no import.
// A match is found inside one text node: words split by a tag (a bold word in a sentence) are found apart.

var FIND_SKIP = /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|TEXTAREA|SELECT|OPTION)$/;

// Take the marks away and join the text again.
export function findClear(root) {
  if (!root) return;
  var marks = root.querySelectorAll("mark.maxpm-find"), parents = [];
  for (var k = 0; k < marks.length; k++) {
    var m = marks[k], p = m.parentNode;
    if (!p) continue;
    p.replaceChild(m.ownerDocument.createTextNode(m.textContent), m);
    if (parents.indexOf(p) < 0) parents.push(p);
  }
  for (var j = 0; j < parents.length; j++) parents[j].normalize();
}

// Mark each place of q (not case sensitive) in the text of root that a person sees, and return the marks in page
// order. skip: a CSS selector of parts to leave out (controls such as an edit link).
export function findMark(root, q, skip) {
  findClear(root);
  q = String(q || "").toLowerCase();
  if (!root || !q) return [];
  var doc = root.ownerDocument || document, nodes = [], out = [], n;
  var walk = doc.createTreeWalker(root, 4 /* NodeFilter.SHOW_TEXT */, null);
  while ((n = walk.nextNode())) {
    var p = n.parentNode;
    if (!p || FIND_SKIP.test(p.nodeName) || n.nodeValue.toLowerCase().indexOf(q) < 0) continue;
    if (skip && p.closest && p.closest(skip)) continue;
    if (p.getClientRects && !p.getClientRects().length) continue;  // hidden: display none, a closed details
    nodes.push(n);
  }
  for (var k = 0; k < nodes.length; k++) {
    var node = nodes[k], text = node.nodeValue, low = text.toLowerCase(), at = 0, i;
    var frag = doc.createDocumentFragment();
    while ((i = low.indexOf(q, at)) >= 0) {
      if (i > at) frag.appendChild(doc.createTextNode(text.slice(at, i)));
      var m = doc.createElement("mark");
      m.className = "maxpm-find";
      m.textContent = text.slice(i, i + q.length);
      frag.appendChild(m);
      out.push(m);
      at = i + q.length;
    }
    if (at < text.length) frag.appendChild(doc.createTextNode(text.slice(at)));
    node.parentNode.replaceChild(frag, node);
  }
  return out;
}

// Make mark i the current one (it counts round: -1 is the last), scroll to it when scroll is true, and return i.
export function findGo(marks, i, scroll) {
  var n = marks.length;
  if (!n) return 0;
  i = ((i % n) + n) % n;
  for (var k = 0; k < n; k++) marks[k].classList.toggle("maxpm-find-on", k === i);
  if (scroll) marks[i].scrollIntoView({ block: "center", inline: "nearest" });
  return i;
}
