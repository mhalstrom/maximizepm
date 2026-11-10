// The Plan tab (#5102 step 4, design #4657, mark's answers in #4658): the project management area. The left bar holds
// All projects, then a card for each project in project rank (its open and ready counts and a small picture of its
// graph), and under the chosen project its open goals in goal rank (owner and progress; a sub-goal under its parent).
// A click on a project or a goal sets the scope of every view (page.js scope); the address names it
// (/plan/<view>?project=<p>&goal=<g>). The views are buttons at the top; Add item opens a dialog whose project and
// goal come from the scope. The views: Overview (overview.js), Items (projects.js), Goals (goals.js), Graph (graph.js),
// Releases (targets.js), Done (done.js).
import { $, esc } from "../lib.js";
import { button } from "../components/button.js";
import { bar, pct } from "../components/bar.js";
import { makeDialog } from "../components/dialog.js";
import { S, scope, planView } from "../page.js";
import { miniGraph } from "./graph.js";

// The views of Plan, in the order of their buttons.
export const PLAN_VIEWS = [["overview", "Overview"], ["items", "Items"], ["goals", "Goals"], ["graph", "Graph"], ["releases", "Releases"], ["done", "Done"]];

export function renderPlanViews() {
  const html = PLAN_VIEWS.map(([k, name]) => button(esc(name), { cls: "plan-vbtn", pressed: planView === k, data: { "plan-view": k },
    attrs: `role="tab" aria-selected="${planView === k}"` })).join("");
  const el = $("#planViews"); if (el.dataset.sig !== html) { el.innerHTML = html; el.dataset.sig = html; }
  for (const [k] of PLAN_VIEWS) $("#plan-" + k).classList.toggle("hidden", k !== planView);
  $("#planScope").textContent = scope.goal ? `goal ${scope.goal}` : scope.project ? scope.project : "all projects";
}

// The left bar: All projects, the project cards, and the goals of the chosen project.
let sideSig = "";
export function renderPlanSide() {
  if (!S) return;
  const live = (i) => !["done", "dropped"].includes(i.status) && !i.backlog;
  const per = S.projects.map(p => ({ p, items: S.items.filter(i => i.project === p.name && live(i)) }));
  const goals = (S.goals || []).filter(g => g.status === "open" && g.project === scope.project).sort((a, b) => a.rank - b.rank);
  const sig = JSON.stringify([scope, per.map(x => [x.p.name, x.items.map(i => [i.id, i.status, i.ready, i.waits_on])]),
    goals.map(g => [g.name, g.owner, g.shared, g.parent, g.items_open.length, g.items_done.length])]);
  if (sig === sideSig) return;
  sideSig = sig;
  const card = (name, label, items, extra) => {
    const ready = items.filter(i => i.ready).length;
    return button(`<b>${esc(label)}</b><span class="gc-n">${items.length} open · ${ready} ready</span>${extra}`,
      { kind: "card", cls: "gcard" + (items.length ? "" : " empty"), data: { "scope-project": name }, pressed: scope.project === name && !scope.goal });
  };
  // A goal: its name, its owner (or shared), and a bar of done items. A sub-goal sits under its parent.
  const top = goals.filter(g => !g.parent || !goals.some(x => x.name === g.parent));
  const goalRow = (g, sub) => {
    const total = g.items_open.length + g.items_done.length;
    return button(`<b>${esc(g.name)}</b><span class="gc-n">${g.shared ? "shared" : g.owner ? esc(g.owner) : "no owner"} · ${g.items_done.length}/${total}</span>${bar(pct(g.items_done.length, total))}`,
      { kind: "card", cls: "gl-goal" + (sub ? " sub" : ""), data: { "scope-goal": g.name, "scope-project": g.project }, pressed: scope.goal === g.name,
        title: (g.outcome || "No outcome written.") + (g.done_when ? "\nDone when: " + g.done_when : "") });
  };
  const goalList = goals.length ? `<div class="gl" aria-label="Goals of ${esc(scope.project)}">${top.map(g => goalRow(g, false)
    + goals.filter(x => x.parent === g.name).map(x => goalRow(x, true)).join("")).join("")}</div>` : "";
  $("#planSide").innerHTML = card("", "All projects", per.flatMap(x => x.items), "")
    + per.map(({ p, items }) => card(p.name, p.name, items, items.length ? miniGraph(items) : `<span class="gc-none">No open items</span>`)
      + (p.name === scope.project ? goalList : "")).join("")
    + `<div class="plan-foot">${button("+ project", { cls: "plain", data: { "plan-addproject": 1 }, title: "Add a project: with a folder, or without one" })}</div>`;
  const on = $("#planSide .gcard.on, #planSide .gl-goal.on"), side = $("#planSide");
  if (on && side.scrollWidth > side.clientWidth) side.scrollTo({ left: on.offsetLeft - side.offsetLeft - 10 });
}

// Add item, Add a goal, Add a project: one dialog; the project and the goal come from the scope.
const addDialog = makeDialog($("#addDlg"));
export function openAdd(part, { project, parent } = {}) {
  if (scope.project && [...$("#addProject").options].some(o => o.value === scope.project)) {
    $("#addProject").value = scope.project; $("#addProject").dispatchEvent(new Event("change"));
  }
  if (scope.goal && [...$("#addGoal").options].some(o => o.value === scope.goal)) $("#addGoal").value = scope.goal;
  if (project || scope.project) $("#goalProject").value = project || scope.project;
  fillParents(); $("#goalParent").value = parent || "";
  $("#addGoalPart").open = part === "goal"; $("#addProjPart").open = part === "project";
  addDialog.open();
  (part === "goal" ? $("#goalName") : part === "project" ? $("#addProjPart input") : $("#addTitle")).focus();
}
// The parent of a new goal: an open goal of the chosen project that is no sub-goal itself (one level).
function fillParents() {
  const proj = $("#goalProject").value, el = $("#goalParent");
  const list = (S.goals || []).filter(g => g.status === "open" && g.project === proj && !g.parent);
  el.innerHTML = `<option value="">none: a goal of its own</option>` + list.map(g => `<option value="${esc(g.name)}">${esc(g.name)}</option>`).join("");
}
$("#goalProject").addEventListener("change", fillParents);
$("#addClose").onclick = () => addDialog.close();
$("#planAdd").onclick = () => openAdd("item");
