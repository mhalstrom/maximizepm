// The Graph tab (#4656): the dependency graph (Mermaid, ELK) and a card for each project.
import { $, esc, left } from "../lib.js";
import { makePanZoom } from "../components/panZoom.js";
import { graphItems, graphText, GRAPH_LIMITS, GRAPH_MAX } from "../components/graphText.js";
import { S, tab, goalSel, inGoal } from "../page.js";

let graphSig = "";
let graphDefaulted = false, elkReady = null, graphProj = "", sideSig = "", sideShown = null;
// ELK routes edges around boxes; the default layout can run an edge behind an unrelated box, which
// reads as a dependency that does not exist. If ELK does not load, edges are drawn above the boxes.
function loadElk() {
  if (elkReady === null) elkReady = import("https://cdn.jsdelivr.net/npm/@mermaid-js/layout-elk@0.2.3/dist/mermaid-layout-elk.esm.min.mjs")
    .then(m => { mermaid.registerLayoutLoaders(m.default); return true; }).catch(() => false);
  return elkReady;
}
// A small picture of one project's graph for its sidebar card: items in columns by step
// (1 = nothing inside the project waits first), lines for waits-on, colors as in the full graph.
function miniGraph(items) {
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
function renderGraphSide(withDone) {
  const live = (i) => withDone || !["done", "dropped"].includes(i.status);
  const per = S.projects.map(p => ({ p, items: S.items.filter(i => i.project === p.name && live(i) && inGoal(i)) }));
  const sig = JSON.stringify([graphProj, withDone, per.map(x => [x.p.name, x.items.map(i => [i.id, i.status, i.ready, i.waits_on])])]);
  if (sig === sideSig) return;
  sideSig = sig;
  const card = (name, label, items, extra) => {
    const ready = items.filter(i => i.ready).length;
    return `<button class="gcard${graphProj === name ? " on" : ""}${items.length ? "" : " empty"}" data-gproj="${esc(name)}" aria-pressed="${graphProj === name}">
      <b>${esc(label)}</b><span class="gc-n">${items.length} ${withDone ? "items" : "open"} · ${ready} ready</span>${extra}</button>`;
  };
  $("#graphSide").innerHTML = `<h2>Projects</h2>` + card("", "All projects", per.flatMap(x => x.items), "")
    + per.map(({ p, items }) => card(p.name, p.name, items, items.length ? miniGraph(items) : `<span class="gc-none">No ${withDone ? "" : "open "}items</span>`)).join("");
  const on = $("#graphSide .gcard.on"), side = $("#graphSide");
  if (on && sideShown !== graphProj) side.scrollTo({ left: on.offsetLeft - side.offsetLeft - 10, top: side.scrollHeight > side.clientHeight ? on.offsetTop - side.offsetTop - 40 : 0 });
  sideShown = graphProj;
}
const graphView = makePanZoom($("#graph"));
export async function renderGraph(force) {
  if (tab !== "graph" || !window.mermaid || !S) return;  // no state yet (the page shows the tab before it, #4888)
  // A big queue draws as an unreadable wall: the first time, show only the project of the top ready item.
  if (!graphDefaulted) {
    graphDefaulted = true;
    const open = S.items.filter(i => !["done", "dropped"].includes(i.status));
    const top = S.items.find(i => i.ready) || open[0];
    if (open.length > 40 && top && !graphProj) graphProj = top.project;
  }
  if (graphProj && !S.projects.some(p => p.name === graphProj)) graphProj = "";
  const proj = graphProj, withDone = $("#graphDone").checked;
  renderGraphSide(withDone);
  $("#graphTitle").textContent = proj ? "Dependency graph: " + proj : "Dependency graph: all projects";
  const items = S.items.filter(i => (withDone || !["done", "dropped"].includes(i.status)));
  const G = goalSel();
  const { show, total, hidden } = graphItems(items, proj || G ? items.filter(i => (!proj || i.project === proj) && inGoal(i)) : null);
  const sig = JSON.stringify([proj, G, withDone, items.filter(i => show.has(i.id)).map(i => [i.id, i.status, i.ready, i.waits_on])]);
  if (!force && sig === graphSig) return;
  graphSig = sig;
  const note = $("#graphNote");
  note.textContent = hidden ? `The graph has ${total} items, more than the ${GRAPH_MAX} it draws at one time: ${hidden} finished items that are not next to an open item are left out.` : "";
  note.classList.toggle("hidden", !hidden);
  const { text, nodes, edges } = graphText(S.projects, items, show);
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
  } catch (e) { graphView.show(`<div class="muted">Graph failed to draw: ${esc(e.message || e)}</div>`, null); }
}
$("#graphSide").addEventListener("click", (e) => {
  const c = e.target.closest("[data-gproj]"); if (!c) return;
  graphProj = c.dataset.gproj; renderGraph(true);
});
$("#graphDone").addEventListener("change", () => renderGraph(true));
