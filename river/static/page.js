// What the frame (app.js) and the views (views/*.js) share (#4656). S is the last /api/state, tab the view on
// screen: live bindings that a view reads and only the frame writes (setState, setTabName). nav holds what the
// frame does for a view (open an item's drawer, show a tab, the Ready dialog); app.js fills it in at load.
import { $ } from "./lib.js";

export let S = null, tab = "sessions";  // Sessions is the first page (#5102 step 5)
export function setState(v) { S = v; }
export function setTabName(v) { tab = v; }
export const nav = { openDrawer: async () => {}, setTab: async () => {}, openReady: () => {}, openSession: async () => {},
  openYou: async () => {}, openPlan: async () => {} };

// Plan (#5102 step 4): the view on screen (overview, items, graph, done; more come), and the scope that the left bar sets for
// every view: all projects (""), one project, or one goal of a project. Only the frame writes them (setPlan).
export let planView = "overview";
export const scope = { project: "", goal: "" };
export function setPlan(view, sc) {
  if (view) planView = view;
  if (sc) { scope.project = sc.project || ""; scope.goal = sc.goal || ""; }
}
// The view of Plan that shows now, or null.
export function planShown(v) { return tab === "plan" && planView === v; }
// The goal of the scope ("" for none), and whether an item is in it.
export function goalSel() { return scope.goal; }
export function inGoal(it) { const g = scope.goal; return !g || (it.goals || []).includes(g); }
// An item in the scope: its project, and its goal when one is chosen.
export function inScope(it) { return (!scope.project || it.project === scope.project) && inGoal(it); }
