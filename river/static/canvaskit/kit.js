/* The maxpm canvas kit (#4983): the script that maxpm puts into an HTML canvas with <meta name="maxpm-kit">. It
   draws the parts that need code: task graphs from queue item ids (their states come from maxpm each time the page
   loads, in <script id="maxpm-items">), graphs of your own nodes, item chips, bar charts, timeline dates, and decision
   cards whose answer goes to the session that wrote the page (through the MaximizePM side panel). It runs in the
   canvas's sandbox: it reads nothing from the network. */
(function () {
  "use strict";
  var ITEMS = {};
  try { var el = document.getElementById("maxpm-items"); if (el) ITEMS = JSON.parse(el.textContent || "{}"); } catch (e) { ITEMS = {}; }
  var P = window.parent && window.parent !== window ? window.parent : null;
  function h(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function ids(s) { return String(s || "").split(/[\s,]+/).map(function (x) { return x.replace(/^#/, ""); }).filter(function (x) { return /^\d+$/.test(x); }); }

  // An item's state as a label and a tone.
  function state(it) {
    if (!it) return { label: "not found", tone: "bad" };
    var who = it.doer === "human" ? " (person)" : "";
    if (it.status === "done") return { label: "done", tone: "good" };
    if (it.status === "dropped") return { label: "dropped", tone: "idle" };
    if (it.status === "in_progress") return { label: "in progress", tone: "info", who: it.assignee };
    if (it.status === "held") return { label: "held", tone: "info", who: it.assignee };
    if (it.blocked) return { label: "blocked" + who, tone: "bad" };
    if (it.backlog) return { label: "backlog", tone: "idle" };
    if (it.waits) return { label: "waits" + who, tone: "warn" };
    return { label: "ready" + who, tone: it.doer === "human" ? "warn" : "idle" };
  }
  function chip(st) { var c = h("span", "kit-chip", st.label); c.dataset.tone = st.tone; return c; }

  // <span data-item="12"></span>: "#12 title" and its state; with your own words inside, only the state is added.
  document.querySelectorAll("[data-item]").forEach(function (e) {
    if (e.classList.contains("kit-node")) return;
    var id = ids(e.getAttribute("data-item"))[0], it = ITEMS[id];
    e.classList.add("kit-item");
    if (!e.textContent.trim()) e.textContent = "#" + id + (it ? " " + it.title : "");
    e.appendChild(chip(state(it)));
  });

  // Graphs. Queue items: <div class="kit-graph" data-items="12 13 14"></div> (an arrow for each 'waits on' between
  // them). Your own: <div class="kit-graph"><div class="kit-node" id="a">A</div><div class="kit-node" id="b"
  // data-after="a">B</div></div>. Each node goes in the row after its last prerequisite.
  function itemNode(id) {
    var it = ITEMS[id], st = state(it), n = h("div", "kit-node");
    n.dataset.key = id; n.dataset.tone = st.tone; n.dataset.after = it ? (it.deps || []).join(" ") : "";
    n.appendChild(h("div", "kit-node-id", "#" + id));
    n.appendChild(h("div", "kit-node-title", it ? it.title : "not in the queue"));
    n.appendChild(chip(st));
    if (st.who) n.appendChild(h("span", "kit-node-who", st.who));
    if (it) n.title = "#" + id + " " + it.title;
    return n;
  }
  // What a node waits on, in words: a phone shows this line instead of the arrows.
  function afterLine(n, keys, names) {
    if (!keys.length) return;
    n.appendChild(h("div", "kit-node-after", "waits on " + keys.map(function (k) { return names[k] || k; }).join(", ")));
  }
  function layout(g) {
    var nodes = [];
    ids(g.getAttribute("data-items")).forEach(function (id) { nodes.push(itemNode(id)); });
    g.querySelectorAll(":scope > .kit-node").forEach(function (n) {
      n.dataset.key = n.id || n.dataset.key || ("n" + nodes.length); nodes.push(n);
    });
    if (!nodes.length) return;
    var by = {}, order = {};
    nodes.forEach(function (n, i) { by[n.dataset.key] = n; order[n.dataset.key] = i; });
    var pre = {};
    nodes.forEach(function (n) { pre[n.dataset.key] = ids(n.dataset.after).concat(String(n.dataset.after || "").split(/[\s,]+/))
      .filter(function (k, i, a) { return k && by[k] && a.indexOf(k) === i && k !== n.dataset.key; }); });
    var depth = {}, seen = {};
    function d(k) {
      if (depth[k] != null) return depth[k];
      if (seen[k]) return 0;  // a cycle: no endless loop
      seen[k] = true;
      var v = 0; pre[k].forEach(function (p) { v = Math.max(v, d(p) + 1); });
      return (depth[k] = v);
    }
    var names = {};  // each node's own words, before any "waits on" line is added
    nodes.forEach(function (n) { names[n.dataset.key] = n.querySelector(".kit-node-title") ? "#" + n.dataset.key : n.textContent.trim().slice(0, 40); });
    nodes.forEach(function (n) { afterLine(n, pre[n.dataset.key], names); });
    var rows = [];
    nodes.forEach(function (n) { var k = n.dataset.key, v = d(k); (rows[v] = rows[v] || []).push(k); });
    // Each row in the order of the rows above it (the mean place of each node's prerequisites), so arrows cross less.
    var place = {};
    rows.forEach(function (row, r) {
      if (!row) return;
      if (r) row.sort(function (a, b) {
        function m(k) { var p = pre[k].filter(function (x) { return place[x] != null; });
          return p.length ? p.reduce(function (s, x) { return s + place[x]; }, 0) / p.length : order[k]; }
        return m(a) - m(b) || order[a] - order[b];
      });
      row.forEach(function (k, i) { place[k] = i / Math.max(row.length - 1, 1); });
    });
    var keep = Array.prototype.filter.call(g.childNodes, function (c) { return !(c.classList && c.classList.contains("kit-node")); });
    g.textContent = "";
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "kit-edges"); svg.setAttribute("aria-hidden", "true");
    g.appendChild(svg);
    var step = 0;
    rows.forEach(function (row) {
      if (!row) return;
      var r = h("div", "kit-layer");
      r.appendChild(h("div", "kit-layer-label", ++step === 1 ? "Step 1: can start first" : "Step " + step));
      row.forEach(function (k) { r.appendChild(by[k]); }); g.appendChild(r);
    });
    keep.forEach(function (c) { if (c.nodeType === 1 || c.textContent.trim()) g.appendChild(c); });
    function edges() {
      var box = g.getBoundingClientRect(), out = [];
      // Arrows only when each row fits on one line; else (a phone, a wide row) steps and words.
      var widest = Math.max.apply(null, rows.map(function (r) { return r ? r.length : 0; }));
      g.classList.toggle("kit-narrow", g.clientWidth < Math.max(560, widest * 146 + 24));
      if (g.classList.contains("kit-narrow")) { svg.innerHTML = ""; return; }
      svg.setAttribute("width", g.scrollWidth); svg.setAttribute("height", g.scrollHeight);
      nodes.forEach(function (n) {
        var b = n.getBoundingClientRect(), tx = b.left + b.width / 2 - box.left, ty = b.top - box.top;
        pre[n.dataset.key].forEach(function (p) {
          var a = by[p].getBoundingClientRect(), sx = a.left + a.width / 2 - box.left, sy = a.bottom - box.top;
          var m = Math.max(12, (ty - sy) / 2);
          out.push("M" + sx + "," + sy + " C" + sx + "," + (sy + m) + " " + tx + "," + (ty - m) + " " + tx + "," + (ty - 2)
                   + " M" + (tx - 4) + "," + (ty - 8) + " L" + tx + "," + (ty - 2) + " L" + (tx + 4) + "," + (ty - 8));
        });
      });
      svg.innerHTML = out.map(function (dd) { return '<path d="' + dd + '"/>'; }).join("");
    }
    edges();
    if (window.ResizeObserver) new ResizeObserver(edges).observe(g); else addEventListener("resize", edges);
  }
  document.querySelectorAll(".kit-graph").forEach(layout);

  // Bar charts: <div class="kit-bars" data-unit="tasks"><div data-value="2320">Worker</div>...</div>.
  document.querySelectorAll(".kit-bars").forEach(function (b) {
    var rows = Array.prototype.filter.call(b.children, function (c) { return c.hasAttribute("data-value"); });
    var max = +b.getAttribute("data-max") || Math.max.apply(null, rows.map(function (c) { return +c.getAttribute("data-value") || 0; }).concat([1]));
    var unit = b.getAttribute("data-unit") || "";
    rows.forEach(function (c) {
      var v = +c.getAttribute("data-value") || 0, row = h("div", "kit-bar"), label = h("span", "kit-bar-label");
      label.innerHTML = c.innerHTML; label.title = c.textContent.trim();
      var track = h("span", "kit-bar-track"), fill = h("span", "kit-bar-fill");
      fill.style.width = Math.max(0, Math.min(100, v / max * 100)) + "%"; track.appendChild(fill);
      row.appendChild(label);
      row.appendChild(h("span", "kit-bar-value", c.getAttribute("data-label") || (v.toLocaleString() + (unit ? " " + unit : ""))));
      row.appendChild(track);
      if (c.dataset.tone) row.dataset.tone = c.dataset.tone;
      b.replaceChild(row, c);
    });
  });

  // Timeline dates.
  document.querySelectorAll(".kit-timeline > li[data-when]").forEach(function (li) {
    li.insertBefore(h("div", "kit-when", li.getAttribute("data-when")), li.firstChild);
  });

  // Decision cards: <section class="kit-decision" data-decision="1" data-options="A|B|C"> with a heading
  // "Decision 1 of 2: <name>" and the decision form. An answer goes to the session that wrote the page.
  var cards = {};
  document.querySelectorAll(".kit-decision[data-decision]").forEach(function (card) {
    var n = String(+card.getAttribute("data-decision")); if (n === "NaN") return;
    card.id = card.id || "decision-" + n;
    var opts = String(card.getAttribute("data-options") || "").split("|").map(function (x) { return x.trim(); }).filter(Boolean);
    var bar = h("div", "kit-answer"), line = h("div", "kit-answered"), note = h("div", "kit-note");
    line.hidden = true; note.hidden = true; bar.appendChild(line);
    var input = h("input"); input.placeholder = opts.length ? "or your own words" : "your answer";
    input.setAttribute("aria-label", "Your answer to decision " + n);
    var go = h("button", "kit-primary", "Answer"); go.type = "button";
    var timer = null;
    function send(text) {
      text = String(text || "").trim();
      if (!text) { note.hidden = false; note.textContent = "Pick an option or write your answer."; return; }
      if (!P) { note.hidden = false; note.textContent = "Open this page in MaximizePM to answer."; return; }
      go.textContent = "Sending…"; note.hidden = true;
      P.postMessage({ maxpmCanvas: "answer", decision: +n, text: text.slice(0, 2000) }, "*");
      clearTimeout(timer);
      timer = setTimeout(function () {  // no answer from a page that sends answers: full screen, or another app
        go.textContent = "Answer"; note.hidden = false;
        note.textContent = "This view cannot send the answer: open the canvas in the MaximizePM side panel.";
      }, 4000);
    }
    opts.forEach(function (o) { var b = h("button", "", o); b.type = "button"; b.onclick = function () { send(o); }; bar.appendChild(b); });
    bar.appendChild(input); bar.appendChild(go); bar.appendChild(note);
    go.onclick = function () { send(input.value); };
    input.onkeydown = function (e) { if (e.key === "Enter") { e.preventDefault(); send(input.value); } };
    card.appendChild(bar);
    cards[n] = { line: line, go: go, note: note, input: input, done: function () { clearTimeout(timer); } };
  });
  if (P && Object.keys(cards).length) {
    addEventListener("message", function (e) {
      var d = e.data || {};
      if (e.source !== P || d.maxpmCanvas !== "answers") return;
      if (d.error && cards[d.decision]) {
        var c0 = cards[d.decision]; c0.done(); c0.go.textContent = "Answer"; c0.note.hidden = false; c0.note.textContent = String(d.error).slice(0, 300);
      }
      Object.keys(cards).forEach(function (n) {
        var a = (d.answers || {})[n], c = cards[n];
        if (d.sent === +n) { c.done(); c.input.value = ""; }
        if (!a) return;
        c.done(); c.line.hidden = false;
        c.line.textContent = String(a.author || "") + " answered: " + String(a.text || "").slice(0, 500)
          + (a.state ? " (" + (a.state === "read" ? "the session read it" : a.state === "sent" ? "sent to the session" : a.state) + ")" : "");
        c.go.textContent = "Answer again";
      });
    });
    P.postMessage({ maxpmCanvas: "answers?" }, "*");
  }
})();
