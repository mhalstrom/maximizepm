// What the frame (app.js) and the views (views/*.js) share (#4656). S is the last /api/state, tab the view on
// screen: live bindings that a view reads and only the frame writes (setState, setTabName). nav holds what the
// frame does for a view (open an item's drawer, show a tab, the Ready dialog); app.js fills it in at load.
import { $ } from "./lib.js";

export let S = null, tab = "board";
export function setState(v) { S = v; }
export function setTabName(v) { tab = v; }
export const nav = { openDrawer: async () => {}, setTab: async () => {}, openReady: () => {}, openSession: async () => {} };

// Goal filter: "" all items, "__none" items without a goal, else one goal's items.
export function goalSel() { return $("#goalFilter").value; }
export function inGoal(it) { const g = goalSel(); return !g || (g === "__none" ? !(it.goals || []).length : (it.goals || []).includes(g)); }
