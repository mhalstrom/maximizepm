// The text of the dependency graph for Mermaid, and which items a large queue draws.
// No DOM here, so a test can run it (tests/test_view_graph.py gives it a queue of 700 items).

// The limits the page gives Mermaid. Mermaid's own are 50,000 characters and 500 edges: a queue of
// about 350 items passes the first, and Mermaid then draws "Maximum text size in diagram exceeded".
export const GRAPH_LIMITS = { maxTextSize: 1000000, maxEdges: 10000 };
// The layout takes about 2s for 700 items, 10s for 2,000 and 60s for 4,000 (ELK, measured in Chrome).
// Above this count the graph leaves out the finished items that are not next to an open item.
export const GRAPH_MAX = 1500;

const finished = (i) => ["done", "dropped"].includes(i.status);

// Which items to draw. items: all that may be drawn (with or without the finished ones).
// base: null for all of them, else the items of the chosen project or goal; their neighbors are drawn too.
// Returns { show: a Set of ids, total: the count before the limit, hidden: finished items left out }.
export function graphItems(items, base, max = GRAPH_MAX) {
  const ids = new Set(items.map(i => i.id));
  const show = base ? new Set(base.flatMap(i => [i.id, ...i.waits_on, ...i.unblocks]).filter(x => ids.has(x))) : ids;
  if (show.size <= max) return { show, total: show.size, hidden: 0 };
  const open = new Set(items.filter(i => show.has(i.id) && !finished(i)).map(i => i.id));
  const keep = new Set(items.filter(i => open.has(i.id) || (show.has(i.id) && [...i.waits_on, ...i.unblocks].some(x => open.has(x)))).map(i => i.id));
  return { show: keep, total: show.size, hidden: show.size - keep.size };
}

const lab = (s) => { const t = s.replace(/["<>#|{}\[\]]/g, " "); if (t.length <= 60) return t; const cut = t.slice(0, 60); return cut.slice(0, cut.lastIndexOf(" ") > 30 ? cut.lastIndexOf(" ") : 60) + "…"; };

// The flowchart: one subgraph per project, an arrow from each prerequisite, a class for the color,
// and a click that opens the item. Returns { text, nodes, edges }.
export function graphText(projects, items, show) {
  const shown = items.filter(i => show.has(i.id));
  let g = "flowchart LR\n", edges = 0;
  for (const p of projects) {
    const mine = shown.filter(i => i.project === p.name);
    if (!mine.length) continue;
    g += `subgraph P${p.id}["${lab(p.name)}"]\n`;
    for (const i of mine) g += `  N${i.id}["#${i.id} ${lab(i.title)}"]\n`;
    g += "end\n";
  }
  for (const i of shown) for (const b of i.waits_on) if (show.has(b)) { g += `N${b} --> N${i.id}\n`; edges++; }
  g += "classDef ready fill:#dcf5e3,stroke:#1a7f37,color:#0b3d1b\nclassDef prog fill:#fff3c4,stroke:#9a6700,color:#3d2a00\nclassDef wait fill:#eceff2,stroke:#8c959f,color:#24292f\nclassDef done fill:#f0f1f3,stroke:#c4c9cf,color:#8c959f\nclassDef human stroke:#8250df,stroke-width:3px\n";
  for (const i of shown) {
    const cls = finished(i) ? "done" : ["in_progress", "held"].includes(i.status) ? "prog" : i.ready ? "ready" : "wait";
    g += `class N${i.id} ${cls}\n`;
    if (i.doer === "human") g += `class N${i.id} human\n`;
    g += `click N${i.id} call riverOpen(${i.id})\n`;
  }
  return { text: g, nodes: shown.length, edges };
}
