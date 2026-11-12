// The Graph view of Plan (#4656; Plan since #5102 step 4): the dependency graph (Mermaid, ELK) of the scope. The
// small graph of a project card in Plan's left bar is here too (miniGraph).
import { $, esc } from "../lib.js";
import { makePanZoom } from "../components/panZoom.js";
import { graphItems, graphText, GRAPH_LIMITS, GRAPH_MAX } from "../components/graphText.js";
import { S, scope, inGoal, planShown } from "../page.js";

let graphSig = "";
let elkReady = null;
// ELK routes edges around boxes; the default layout can run an edge behind an unrelated box, which
// reads as a dependency that does not exist. If ELK does not load, edges are drawn above the boxes.
function loadElk() {
  if (elkReady === null) elkReady = import("https://cdn.jsdelivr.net/npm/@mermaid-js/layout-elk@0.2.3/dist/mermaid-layout-elk.esm.min.mjs")
    .then(m => { mermaid.registerLayoutLoaders(m.default); return true; }).catch(() => false);
  return elkReady;
}
// A small picture of one project's graph for its sidebar card: items in columns by step
// (1 = nothing inside the project waits first), lines for waits-on, colors as in the full graph.
export function miniGraph(items) {
  const byId = new Map(items.map(i => [i.id, i])), depth = new Map();
  const d = (i, seen = new Set()) => {
    if (depth.has(i.id)) return depth.get(i.id);
    if (seen.has(i.id)) return 0; seen.add(i.id);
    const v = Math.max(-1, ...i.waits_on.filter(x => byId.has(x)).map(x => d(byId.get(x), seen))) + 1;
    depth.set(i.id, v); return v;
  };
  items.forEach(i => d(i));
  const cols = [];
  for (const i of items) (cols[depth.get(i.id)] ||= []).push(i);
  const W = 12, H = 7, GX = 10, GY = 4, pos = new Map();
  cols.forEach((col, x) => col.forEach((i, y) => pos.set(i.id, [6 + x * (W + GX), 5 + y * (H + GY)])));
  const w = 12 + cols.length * (W + GX) - GX, h = 10 + Math.max(...cols.map(c => c.length)) * (H + GY) - GY;
  const fill = (i) => ["done", "dropped"].includes(i.status) ? "var(--line)" : ["in_progress", "held"].includes(i.status) ? "var(--prog)" : i.ready ? "var(--ready)" : "var(--muted)";
  let lines = "", boxes = "";
  for (const i of items) for (const b of i.waits_on) if (pos.has(b)) {
    const [x1, y1] = pos.get(b), [x2, y2] = pos.get(i.id);
    lines += `<line x1="${x1 + W}" y1="${y1 + H / 2}" x2="${x2}" y2="${y2 + H / 2}"/>`;
  }
  for (const i of items) {
    const [x, y] = pos.get(i.id);
    boxes += `<rect x="${x}" y="${y}" width="${W}" height="${H}" rx="2" fill="${fill(i)}"${i.doer === "human" ? ' stroke="var(--human)" stroke-width="1.5"' : ""}/>`;
  }
  return `<svg viewBox="0 0 ${Math.max(w, 150)} ${Math.max(h, 38)}" preserveAspectRatio="xMidYMid meet" aria-hidden="true"><g stroke="var(--muted)" stroke-width=".7" opacity=".6">${lines}</g>${boxes}</svg>`;
}
const graphView = makePanZoom($("#graph"));
export async function renderGraph(force) {
  if (!planShown("graph") || !window.mermaid || !S) return;  // no state yet (the page shows the tab before it, #4888)
  const proj = scope.project, withDone = $("#graphDone").checked;
  $("#graphTitle").textContent = scope.goal ? `${proj} · goal ${scope.goal}` : proj || "All projects";
  const items = S.items.filter(i => (withDone || !["done", "dropped"].includes(i.status)));
  const G = scope.goal;
  const { show, total, hidden } = graphItems(items, proj || G ? items.filter(i => (!proj || i.project === proj) && inGoal(i)) : null);
  // The drawing's own text is the signature: a view that shows again with the same graph keeps its drawing, since
  // Mermaid took about 90 ms at each switch to Graph (#5556). force (a new theme) draws it again.
  const { text, nodes, edges } = graphText(S.projects, items, show);
  const sig = JSON.stringify([proj, G, withDone, text]);
  if (!force && sig === graphSig) return;
  graphSig = sig;
  const note = $("#graphNote");
  note.textContent = hidden ? `${hidden} of ${total} items left out: finished, and next to no open item.` : "";
  note.classList.toggle("hidden", !hidden);
  // Above its limits Mermaid draws its own error picture and reports no error: say it here, with the counts.
  if (text.length > GRAPH_LIMITS.maxTextSize || edges > GRAPH_LIMITS.maxEdges) {
    graphView.show(`<div class="muted">The graph is too large to draw: ${nodes} items and ${edges} links. Choose a project or a goal.</div>`, null);
    return;
  }
  try {
    const elk = await loadElk();
    const { svg, bindFunctions } = await mermaid.render("g" + Date.now(), (elk ? "---\nconfig:\n  layout: elk\n---\n" : "") + text);
    // The same project, goal and done choice keeps the zoom and position across refreshes.
    const stage = graphView.show(svg, JSON.stringify([proj, G, withDone]));
    bindFunctions && bindFunctions(stage);
    stage.querySelectorAll(".edgePaths").forEach(e => e.parentNode.appendChild(e));
  } catch (e) { graphSig = ""; graphView.show(`<div class="muted">Graph failed to draw: ${esc(e.message || e)}</div>`, null); }
}
$("#graphDone").addEventListener("change", () => renderGraph(true));
