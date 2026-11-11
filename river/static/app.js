import { $, esc, store, actor, toast, act, clip, fillSelect, hooks, artId, ago, left } from "./lib.js";
import { chip, prioNumChip, doerChip, projectChip, personChip } from "./components/chip.js";
import { itemOrder } from "./components/itemRow.js";
import { makeDrawer, itemDrawerHtml } from "./components/drawer.js";
import { makeDialog, ASK_AGAIN_MS } from "./components/dialog.js";
import { agentStart } from "./components/agentStart.js";
import { chooseLaunch, launchArgs } from "./components/launchDialog.js";
import { chooseStop } from "./components/agentActions.js";
import { archiveProject, restoreProject } from "./components/archiveProject.js";
import { openTerminal } from "./components/terminalDialog.js";
import { GRAPH_LIMITS } from "./components/graphText.js";
import { folderForm, wireFolderForms } from "./components/folderForm.js";
import { openInPanel, onPanel, panelTargets, canvasState, canvasOpens } from "./components/sidePanel.js";
import { wireArtLinks } from "./components/artLinks.js";
import { wireItemPop } from "./components/itemPop.js";
import { installMics } from "./components/dictate.js";
import { S, tab, setState, setTabName, nav, goalSel, setPlan, planView, scope, planShown } from "./page.js";
import { makeStateClient } from "./state.js";
// The views (#4656): each tab of the page has a file of its own in views/.
import * as overview from "./views/overview.js";
import * as needsYou from "./views/you-needs.js";
import * as inbox from "./views/you-inbox.js";
import * as youView from "./views/you.js";
import * as sessWork from "./views/sessions-work.js";
import * as projects from "./views/projects.js";
import * as plan from "./views/plan.js";
import * as goals from "./views/goals.js";
import { makeSessions } from "./views/sessions.js";
import { makeArtifacts } from "./views/artifacts.js";
import { makeTerminalTab } from "./views/terminal.js";
import * as targets from "./views/targets.js";
import { renderGraph } from "./views/graph.js";
import { renderLog, renderEvents } from "./views/done.js";
import * as settings from "./views/settings.js";
import { makeMenu } from "./components/menu.js";
hooks.refresh = refresh;
Object.assign(nav, { openDrawer: (id, fresh) => openDrawer(id, fresh), setTab: (n, push) => setTab(n, push), openReady: () => openReady(),
  openPlan: (view) => showPlan(view),
  openSession: async (name) => { await setTab("sessions"); sessionsTab.pick(name); },
  // The You view of Sessions (#5102): what waits on the person; takeovers opens the list of items an agent took over.
  openYou: async (takeovers) => { await setTab("sessions"); sessionsTab.showYou(true); if (takeovers) youView.openTakeovers(); } });
let openItem = null, drawerItem = null;  // drawerItem: the open item as /api/item gave it
const drawer = makeDrawer($("#drawer"));
const header = document.querySelector("header");  // the page's header: on a phone one bar of 48 px (#4903)

let actorPicked = false;




// The loop of maxpm serve is late (S.loop.late): it does no automatic work. One button starts the server again.
function renderLoop() {
  const l = S.loop, b = $("#loopLate");
  b.classList.toggle("hidden", !(l && l.late));
  if (!l || !l.late) return;
  b.textContent = `Automatic work stopped · ${l.age}`;
  b.title = `maxpm serve made no complete pass of its loop for ${l.age}, so it closes no finished session, sends no notification, starts no fresh session and does not reload.`
    + (l.error ? `\nLast error: ${l.error}` : "") + "\nClick to start the server again (maxpm serve --restart).";
}

function renderSelects() {
  const names = S.projects.map(p => p.name);
  fillSelect("#addProject", names, false);
  const a = $("#actor"), cur = a.value || store("river.actor") || "";
  // Only people act from this page; agents act through the maxpm command.
  const people = S.agents.filter(x => x.kind === "human");
  a.innerHTML = `<option value="">(choose your name)</option>` + people.map(x => `<option value="${esc(x.name)}">${esc(x.name)}</option>`).join("");
  if (people.some(x => x.name === cur)) a.value = cur;
  else if (!a.value && people.length === 1 && !actorPicked) { a.value = people[0].name; actorPicked = true; store("river.actor", a.value); }
  fillSelect("#setKey", Object.keys(S.settings.defaults), false);
  fillSelect("#goalProject", names, false);
  const openGoals = (S.goals || []).filter(g => g.status === "open");
  { const el = $("#addGoal"), cur = el.value, proj = $("#addProject").value;
    const list = openGoals.filter(g => g.project === proj).concat(openGoals.filter(g => g.project !== proj));
    el.innerHTML = `<option value="">no goal</option>` + list.map(g => `<option value="${esc(g.name)}">${esc(g.name)}${g.project !== proj ? " (" + esc(g.project) + ")" : ""}</option>`).join("");
    if ([...el.options].some(o => o.value === cur)) el.value = cur; }
}

window.riverOpen = (id) => openDrawer(id);
// Closing takes the item out of the URL: Back when this page added it, else the URL is rewritten.
function closeDrawer() {
  const id = openItem;
  drawer.close(); openItem = null;
  if (urlItem() !== id) return;
  if (history.state && history.state.item === id) history.back();
  else history.replaceState(null, "", pageUrl(null));
}
// Escape closes the drawer at every width (#4716), as it closes the search and the setup guide. Something on top
// of the drawer takes the key first: an open dialog (each closes itself) or the item pop-up. Capture phase, so
// this looks before a dialog's own handler closes the dialog.
window.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || e.defaultPrevented || !drawer.isOpen()) return;
  if (document.querySelector('[role="dialog"]:not(.hidden), #itemPop:not(.hidden)')) return;
  closeDrawer();
}, true);
// A click outside the drawer on empty space closes it too (#5027), as a click outside a dialog does. Not a click on
// a control or a link (it does its own thing, and a click on another item opens that one), not the end of a text
// selection, not while a dialog is open, and not when it opened another item. A field of the item that the person
// changed asks first: the first click says so, a second one within a few seconds closes it.
let drawerDown = null, drawerWarned = 0;
const drawerDraft = () => [...$("#drawer").querySelectorAll("input, textarea, select")].some(f =>
  f.type === "checkbox" || f.type === "radio" ? f.checked !== f.defaultChecked
    : f.tagName === "SELECT" ? f.selectedIndex !== Math.max(0, [...f.options].findIndex(o => o.defaultSelected))
    : f.value !== f.defaultValue);
document.addEventListener("pointerdown", (e) => {
  drawerDown = drawer.isOpen() && !$("#drawer").contains(e.target) ? openItem : null;
}, true);
document.addEventListener("click", (e) => {
  const was = drawerDown; drawerDown = null;
  if (was === null || was !== openItem || !drawer.isOpen() || e.defaultPrevented) return;
  const t = e.target;
  if (!(t instanceof Element) || !t.isConnected || $("#drawer").contains(t)) return;
  if (t.closest('a, button, input, select, textarea, label, summary, option, [data-open], [role="button"], [role="tab"], '
               + '[role="dialog"], [contenteditable], [tabindex], #itemPop, #toast, iframe, .tabulator-row')) return;
  if (String(getSelection() || "").trim() || document.querySelector('[role="dialog"]:not(.hidden)')) return;
  if (drawerDraft() && Date.now() - drawerWarned > ASK_AGAIN_MS) {
    drawerWarned = Date.now();
    return toast("You changed a field of this item: click outside again to close it without the change", true);
  }
  drawerWarned = 0;
  closeDrawer();
});


async function openDrawer(id, fresh = false) {
  openItem = id;
  // A new entry when no item was open (Back closes it); another item replaces the open one.
  if (urlItem() == null) history.pushState({ item: id }, "", pageUrl(id));
  else if (urlItem() !== id) history.replaceState(history.state && history.state.item != null ? { item: id } : null, "", pageUrl(id));
  const r = await fetch(`api/item/${id}`); if (!r.ok) return;
  const data = await r.json();
  // Closed or moved to another item while the fetch ran (a slow refresh, then Escape): do not open it again (#4766).
  if (openItem !== id) return;
  drawerItem = data;
  drawer.show(id, itemDrawerHtml(data, { S, me: actor() }), fresh);
}

async function drawerAction(what) {
  const id = openItem;
  // A done item is in the state only while a view shows done items: else the drawer's own copy.
  const it = S.items.find(i => i.id === id) || (drawerItem && drawerItem.id === id ? drawerItem : null);
  try {
    if (what === "claim") await act("claim", { id });
    if (what === "done") { const out = prompt("Output note (optional)") ?? undefined; await act("done", { id, output: out || undefined }); }
    if (what === "release") await act("release", { id });
    if (what === "drop") await act("drop", { id });
    if (what === "reopen") await act("reopen", { id });
    if (what === "activate") { await act("activate", { id }); toast(`#${id} is open again`); }
    if (what === "backlog") {
      const note = prompt(`Why does nobody take #${id} now? (one line)`); if (!note) return;
      const waits = S.items.filter(i => i.waits_on.includes(id) && !["done", "dropped"].includes(i.status) && !i.backlog);
      const force = waits.length ? (confirm(`${waits.map(i => "#" + i.id).join(", ")} wait on #${id} and cannot be ready while it is in the backlog. Move it anyway?`) ? "moved from the page; the waiting items may wait" : null) : undefined;
      if (force === null) return;
      await act("backlog", { id, note, force }); toast(`#${id} moved to the backlog`);
    }
    if (what === "unblock") await act("unblock", { id });
    if (what === "replanned") await act("replanned", { id });
    if (what === "msg") { const body = $("#dMsgBody").value.trim(); if (!body) return; if (!actor()) return toast("Choose your name in 'You are' first", true); await act("send", { kind: $("#dMsgKind").value, body, item: id }); toast("Sent"); }
    if (what === "agent") {
      const ch = await chooseLaunch(S, { title: "Open an agent", go: "Open agent", what: `#${id} ${it.title}`, item: it.ready ? it : null });
      if (!ch) return;
      const r = await act("open_agent_on", { id, person: actor() || undefined, ...launchArgs(ch) });
      toast(r.pushed_to ? `Gave #${id} to ${r.pushed_to}, which was waiting for work`
        : `Started ${r.agent} in ${r.project}` + (r.focus && r.focus.startsWith("help") ? `, to do #${id} with you` : r.focus ? `, to unblock #${id}` : `, for #${id}`));
    }
    if (what === "push") { const to = $("#dPushTo").value; if (to) { await act("push", { id, to, note: $("#dPushNote").value.trim() || undefined }); toast(`Pushed #${id} to ${to}`); } }
    if (what === "block") { const r = $("#dBlock").value.trim(), u = $("#dUntil").value.trim(); if (r || u) await act("block", { id, reason: r || undefined, until: u || undefined }); }
    if (what === "tag") { const g = $("#dGoal").value; if (g) await act("item_edit", { id, goals: [g] }); }
    if (what === "dep") { const on = $("#dDep").value.split(/[\s,]+/).filter(Boolean).map(Number); if (on.length) await act("dep_add", { id, on }); }
    if (what === "save") {
      await act("item_edit", { id, title: $("#dTitle").value, short: $("#dShort").value.trim(), notes: $("#dNotes").value, context: $("#dContext").value, touches: $("#dTouches").value, check: $("#dCheck").value, due: $("#dDue").value.trim() || undefined, doer: $("#dDoer").value, project: $("#dProject").value !== it.project ? $("#dProject").value : undefined,
        models: Object.fromEntries([["model", "#dModel"], ["effort", "#dEffort"], ["min_model", "#dMinModel"], ["max_model", "#dMaxModel"], ["agent", "#dAgent"]]
          .map(([f, sel]) => [f, $(sel).value.trim()]).filter(([f, v]) => v !== (it[f + "_from"] === "item" ? it[f] || "" : ""))
          .map(([f, v]) => [f, v || "none"])) });
      if (+$("#dPrio").value !== it.priority) await act("prio", { id, priority: +$("#dPrio").value });
      toast("Saved");
    }
    if (what === "up" || what === "down") {
      const sib = S.items.filter(i => i.project === it.project).sort((a, b) => a.rank - b.rank);
      const k = sib.findIndex(i => i.id === id), other = sib[what === "up" ? k - 1 : k + 1];
      if (other) await act("move", what === "up" ? { id, before: other.id } : { id, after: other.id });
    }
    document.activeElement && document.activeElement.blur();
    await openDrawer(id, true);
  } catch (e) { /* toast shown */ }
}


// New page code (#4690, #4850): maxpm serve sends the version of the page's files in each state. When it changes, the
// page shows a bar, "MaximizePM was updated", with Reload, and keeps it until a click: it never loads itself again
// for a new version alone, because the served code moves at each maxpm merge, and a reload loses the place in an open
// artifact or canvas. Only when the server's state has another shape than this code reads (STATE_SHAPE in
// server.py, PAGE_SHAPE here) does the page load itself again, and at most once in 10 minutes; else the bar says
// that the page must load again.
const PAGE_SHAPE = 1;
const SHAPE_RELOAD = "maxpm-shape-reload";
let pageBuild = null;
function busyHere() {
  const a = document.activeElement;
  if (a && ["INPUT", "TEXTAREA"].includes(a.tagName) && a.id !== "actor") return true;
  if (document.querySelector('[role="dialog"]:not(.hidden)') || drawer.isOpen()) return true;
  if ($("#sessText") && $("#sessText").value.trim()) return true;
  return !!($("#sessFiles") && $("#sessFiles").children.length);
}
function codeBar(text) {
  let bar = $("#newCodeBar");
  if (!bar) {
    bar = document.createElement("div"); bar.id = "newCodeBar"; bar.className = "newcode"; bar.setAttribute("role", "status");
    bar.innerHTML = `<span></span> <button class="btn primary" type="button">Reload</button>`;
    bar.querySelector("button").onclick = () => location.reload();
    document.body.append(bar);
  }
  bar.querySelector("span").textContent = text;
}
// Returns true when the page loads itself again (the caller then draws nothing).
function newCode(build, shape) {
  if (shape != null && shape !== PAGE_SHAPE) {
    let last = 0;
    try { last = +sessionStorage.getItem(SHAPE_RELOAD) || 0; } catch (e) { /* no storage: the bar only */ }
    if (!busyHere() && Date.now() - last > 10 * 60 * 1000) {
      try { sessionStorage.setItem(SHAPE_RELOAD, String(Date.now())); } catch (e) { /* the bar stays the guard */ }
      location.reload();
      return true;
    }
    codeBar("MaximizePM was updated, and this page must load again.");
    return false;
  }
  if (!build) return false;
  if (pageBuild == null) { pageBuild = build; return false; }
  if (build !== pageBuild) codeBar("MaximizePM was updated.");
  return false;
}

// The state comes as changes since the page's last version (state.js, #4709). Done and dropped items come only while
// a view shows them: the Projects tab with "done" on, or the Graph with its done box on.
const stateClient = makeStateClient(async (u) => { const r = await fetch(u); if (!r.ok) throw new Error(`state: ${r.status}`); return r.json(); });
let stateClosed = false;
function wantClosed() { return (planShown("items") && $("#showDone").checked) || (planShown("graph") && $("#graphDone").checked); }
for (const box of ["#showDone", "#graphDone"]) $(box).addEventListener("change", () => refresh().catch(() => {}));
// Done items are not always in the state: closed_counts says how many each project has.
function closedTotal() { return Object.values((S && S.closed_counts) || {}).reduce((a, b) => a + b, 0); }
function anyStarted() { return closedTotal() > 0 || S.items.some(i => ["in_progress", "held", "done"].includes(i.status)); }
async function refresh() {
  stateClosed = wantClosed();
  setState(await stateClient.load(stateClosed));
  if (S.dev_build) { if (window._build && window._build !== S.dev_build) return location.reload(); window._build = S.dev_build; }
  if (newCode(S.page_build, S.state_shape)) return;
  renderSelects(); plan.renderPlanViews(); plan.renderPlanSide(); overview.renderOverview(); projects.renderProjects(); renderReady(); renderLoop(); renderEvents(); settings.renderSettings(); settings.renderPeople(); goals.renderGoals(); youView.renderTakeovers(); sessWork.renderStart(); sessWork.renderFoot(); sessWork.renderGive(view.session); targets.renderTargets();
  renderRelay();
  canvasState(S.canvases);  // an open canvas follows its newest version (#4768)
  canvasOpens(S.canvas_opens, (name) => nav.openSession(name));  // maxpm canvas open (#4783)
  renderGraph(false); renderLog(false); needsYou.pollNeedsYou().then(firstYou).catch(() => {}); inbox.pollInbox().catch(() => {});
  $("#stamp").textContent = "updated " + new Date().toLocaleTimeString();
  const boot = $("#bootNote"); if (boot) boot.remove();
  header.classList.toggle("no-actor", !actor());  // a phone shows You are until it is chosen (#4885)
  if (openItem != null && drawer.isOpen()) openDrawer(openItem);
  // The setup guide's last steps follow the queue: when a task is added or an agent picks one up, it checks again.
  const firstRun = JSON.stringify([S.items.length, S.agents.some(a => a.kind !== "human"), anyStarted()]);
  if (setupDialog.isOpen() && firstRun !== setupSig && !$("#setupSteps").contains(document.activeElement)) openSetup();
  setupSig = firstRun;
}

document.addEventListener("click", async (e) => {
  const t = e.target;
  // A view's own controls first (#4656), then what every view shares: agents, the drawer, item links.
  for (const v of [needsYou, youView, sessWork, overview, inbox, projects, targets, settings]) if (v.onClick(t)) return;
  if (t.dataset.startManager) {
    const ch = await chooseLaunch(S, { title: "Start the manager", go: "Start manager",
      what: "Manager: plans with you, starts agents, fills their queues, and stops stuck ones (maxpm manage)" });
    if (!ch) return;
    try { const r = await act("start_manager", launchArgs(ch)); toast(`Started ${r.agent} as ${r.session_name}, the manager`); } catch (e) { /* toast shown */ }
    return; }
  if (t.dataset.terminal) return openTerminal(t.dataset.terminal);
  if (t.dataset.stopAgent) {
    if (!actor()) return toast("Choose your name in 'You are' first", true);
    const a = S.agents.find(x => x.name === t.dataset.stopAgent); if (!a) return;
    const ch = await chooseStop(a); if (!ch) return;
    try { const r = await act(ch.kill ? "kill_agent" : "stop_agent", { agent: a.name, reason: ch.reason });
      toast(ch.kill ? `Killed ${a.name}; released ${r.released.length} item(s). Check its folder for uncommitted work.` : `Asked ${a.name} to stop: it ends ${r.ends}`);
    } catch (e) { /* toast shown */ }
    return; }
  if (t.dataset.qremove) { await act("queue_remove", { agent: t.dataset.qremove, ref: t.dataset.ref }).catch(() => {}); return; }
  const la = t.closest("[data-launch]"); if (la) {
    if (la.dataset.busy) return; la.dataset.busy = "1"; setTimeout(() => delete la.dataset.busy, 4000);
    const ch = await chooseLaunch(S, { title: "Start an agent", go: "Start", pickWork: true });
    if (!ch) { delete la.dataset.busy; return; }  // closed with no start: the next click opens it again at once
    try { const w = ch.work || "";
      const r = w.startsWith("i:") ? await act("dispatch_item", { id: +w.slice(2), ...launchArgs(ch) })
        : await act("launch_agent", { project: w.startsWith("p:") ? w.slice(2) : undefined, ...launchArgs(ch) });
      toast(r.pushed_to ? `Gave #${r.item.id} ${clip(r.item.title, 60)} to ${r.pushed_to}, which was waiting for work`
                        : `Started ${r.agent} in ${r.project}, for #${r.item.id} ${clip(r.item.title, 60)}` + (r.why ? ` (${r.why})` : "")); } catch (e) { /* toast shown */ }
    return; }
  if (t.dataset.untag) { await act("item_edit", { id: openItem, untag: [t.dataset.untag] }).catch(() => {}); return openDrawer(openItem, true); }
  // A link to Plan from Sessions or the drawer (#5102 step 7): that scope, that view.
  const pg = t.closest("[data-plan-go]");
  if (pg) { if (drawer.isOpen()) closeDrawer(); setPlan(null, { project: pg.dataset.project || "", goal: pg.dataset.goal || "" });
    store("river.scope", JSON.stringify(scope)); return showPlan(pg.dataset.planGo); }
  // Plan (#5102 step 4): a view button, a project or a goal of the left bar (the scope), + project.
  const pv = t.closest("[data-plan-view]"); if (pv) return showPlan(pv.dataset.planView);
  const sg = t.closest("[data-scope-goal]"); if (sg) return setScope(sg.dataset.scopeProject, scope.goal === sg.dataset.scopeGoal ? "" : sg.dataset.scopeGoal);
  const sp = t.closest("[data-scope-project]"); if (sp) return setScope(sp.dataset.scopeProject, "");
  if (t.closest("[data-plan-addproject]")) return plan.openAdd("project");
  // Archive a project (Items view), the Archived projects list of the left bar, and Restore (#5197).
  const ap = t.closest("[data-archive-project]");
  if (ap) { const name = ap.dataset.archiveProject; if (await archiveProject(S, name) && scope.project === name) setScope("", ""); return; }
  if (t.closest("[data-plan-archived]")) return plan.toggleArchived();
  const rp = t.closest("[data-restore-project]"); if (rp) { await restoreProject(rp.dataset.restoreProject); return; }
  // A goal card of the Items view: that goal is the scope (a second click: its project).
  const card = t.closest("[data-goal]"); if (card) { const g = (S.goals || []).find(x => x.name === card.dataset.goal);
    if (g) setScope(g.project, scope.goal === g.name ? "" : g.name); return; }
  // A session link (the drawer's Open session, a name in its history, the Board's agents and manager card, #4687):
  // the Sessions tab on that session (#4615).
  const sgo = t.closest("[data-session-go]");
  if (sgo) { closeDrawer(); await setTab("sessions"); sessionsTab.pick(sgo.dataset.sessionGo); return; }
  const open = t.closest("[data-open]"); if (open) return openDrawer(+open.dataset.open);
  const row = t.closest(".row"); if (row) return openDrawer(+row.dataset.id);
  if (t.id === "dClose") return closeDrawer();
  if (t.dataset.do) return drawerAction(t.dataset.do);
  if (t.dataset.offer) { const n = +t.dataset.offer, body = $("#offer-" + n).value.trim(); if (!body) return toast("Say what you can take", true);
    return act("offer", { item: n, body }).then(() => toast(`Offer sent to the holder of #${n}`)).catch(() => {}); }
  if (t.dataset.undep) { await act("dep_remove", { id: openItem, on: [+t.dataset.undep] }).catch(() => {}); return openDrawer(openItem, true); }
  if (t.dataset.tab && TABS.includes(t.dataset.tab)) return setTab(t.dataset.tab);  // only a tab of the page (#4652)
});

$("#actor").addEventListener("change", () => { store("river.actor", $("#actor").value); inbox.actorChanged().catch(() => {}); });
// Plan's scope and view (#5102 step 4): every view draws again; the address names them (a new entry, so Back returns).
function planDraw() {
  plan.renderPlanViews(); plan.renderPlanSide(); overview.renderOverview(); projects.renderProjects(); goals.renderGoals(); targets.renderTargets();
  renderGraph(true); renderLog(true); renderEvents();
  if (S && wantClosed() !== stateClosed) refresh().catch(() => {});
}
function setScope(project, goal) {
  setPlan(null, { project, goal });
  store("river.scope", JSON.stringify(scope));
  if (tab === "plan") syncUrl(true);
  planDraw();
}
async function showPlan(view, push = true) {
  setPlan(view);
  if (tab !== "plan") await setTab("plan", push);
  else if (push) syncUrl(true);
  planDraw();
}
try { const sc = JSON.parse(store("river.scope") || "null"); if (sc) setPlan(null, sc); } catch (e) { /* none kept */ }
$("#addProject").addEventListener("change", renderSelects);

// Theme: System follows the computer's setting (also when it changes); Light and Dark set data-theme on
// <html>, saved in this browser. Mermaid draws the graph in the chosen theme.
const darkQuery = matchMedia("(prefers-color-scheme: dark)");
function themeChoice() { const t = store("river.theme"); return t === "light" || t === "dark" ? t : "system"; }
function applyTheme() {
  const t = themeChoice(), root = document.documentElement;
  if (t === "system") delete root.dataset.theme; else root.dataset.theme = t;
  const dark = t === "dark" || (t === "system" && darkQuery.matches);
  if (window.mermaid) mermaid.initialize({ startOnLoad: false, securityLevel: "loose", theme: dark ? "dark" : "default", flowchart: { useMaxWidth: true }, ...GRAPH_LIMITS });
  if (S) renderGraph(true);
}
$("#theme").value = themeChoice();
$("#theme").addEventListener("change", (e) => { store("river.theme", e.target.value === "system" ? "" : e.target.value); applyTheme(); });
darkQuery.addEventListener("change", () => { if (themeChoice() === "system") applyTheme(); });
applyTheme();
// Drag an item row onto an agent to push it there.
document.addEventListener("dragstart", (e) => { const r = e.target.closest && e.target.closest(".row[draggable]"); if (r) e.dataTransfer.setData("text/river-item", r.dataset.id); });
document.addEventListener("dragover", (e) => { const a = e.target.closest && e.target.closest("[data-agent]"); if (a && e.dataTransfer.types.includes("text/river-item")) { e.preventDefault(); a.classList.add("drop"); } });
document.addEventListener("dragleave", (e) => { const a = e.target.closest && e.target.closest("[data-agent]"); if (a) a.classList.remove("drop"); });
document.addEventListener("drop", async (e) => {
  const a = e.target.closest && e.target.closest("[data-agent]"); if (!a) return;
  e.preventDefault(); a.classList.remove("drop");
  const id = +e.dataTransfer.getData("text/river-item"); if (!id) return;
  await act("queue_add", { agent: a.dataset.agent, id }).then(() => toast(`#${id} is in the queue of ${a.dataset.agent}`)).catch(() => {});
});

const TABS = ["sessions", "plan", "settings", "terminal"];
// The page's address (#4653), so each view can be linked, reloaded, and reached with Back:
//   /  (Sessions: the You view when something waits on you, else the session chosen last, #5102)   /sessions
//   /sessions/<session>   /sessions/<session>/artifacts/<artifact id>
//   /you (the You view of Sessions, #5102)   /artifacts (the Artifacts view of Sessions, #5102)
//   /sessions/<session>/items/<id> (an item in the side panel, #4685)
//   /artifacts/<artifact id> (opens it beside its session)   /plan/<view>?project=&goal=   /settings
// plus ?as=<person>, ?item=<id> while an item's drawer is open, and ?terminal=<agent> (once: it opens that agent's
// Terminal, then leaves the address). The artifact id is artId(target). The paths
// are relative to ROOT, the page's own folder (maxpm serve sends a <base>): / here and on the relay's tunnel.
const ROOT = new URL("./", document.baseURI).pathname;
const view = { session: null, art: null, pane: null };  // the session the Sessions tab shows, the tab of its side panel, a pane ("you", "arts")
let asParam = new URLSearchParams(location.search).get("as");
let quiet = 0;  // while the page follows an address, its own changes do not write one
function readUrl() {
  const rel = location.pathname.startsWith(ROOT) ? location.pathname.slice(ROOT.length) : "";
  const parts = rel.split("/").filter(Boolean).map(x => { try { return decodeURIComponent(x); } catch (e) { return x; } });
  const q = new URLSearchParams(location.search), it = q.get("item");
  const pane = parts[0] === "you" ? "you" : parts[0] === "artifacts" ? "arts" : null;
  const r = { tab: TABS.includes(parts[0]) ? parts[0] : "sessions", session: null, art: null, root: !parts.length,
              pane, as: q.get("as"), terminal: q.get("terminal"), item: /^\d+$/.test(it || "") ? +it : null };
  if (r.tab === "sessions" && !pane) { r.session = parts[1] || null; if (parts[2] === "artifacts") r.art = parts[3] || null;
    if (parts[2] === "items" && /^\d+$/.test(parts[3] || "")) r.art = "item:" + parts[3]; }
  if (pane === "arts") r.art = parts[1] || null;
  // Plan (#5102): /plan/<view>?project=<p>&goal=<g>. The old paths (/board, /projects, ...) answer "not found".
  if (r.tab === "plan") r.plan = { view: plan.PLAN_VIEWS.some(([k]) => k === parts[1]) ? parts[1] : "overview",
                                   project: q.get("project") || "", goal: q.get("goal") || "" };
  return r;
}
function urlItem() { return readUrl().item; }
function pageUrl(item = drawer.isOpen() ? openItem : null, tabName = tab) {
  const pane = tabName === "sessions" ? { you: "you", arts: "artifacts" }[view.pane] : null;
  let p = pane || (tabName === "sessions" && !view.session ? "" : tabName);
  if (tabName === "sessions" && view.session && !pane) {
    p += "/" + encodeURIComponent(view.session);
    if (view.art) p += view.art.startsWith("item:") ? "/items/" + view.art.slice(5) : "/artifacts/" + view.art;
  }
  const q = new URLSearchParams();
  if (tabName === "plan") { p += "/" + planView; if (scope.project) q.set("project", scope.project); if (scope.goal) q.set("goal", scope.goal); }
  if (asParam) q.set("as", asParam);
  if (item != null) q.set("item", item);
  return ROOT + p + (q.toString() ? "?" + q : "");
}
// Write the address of what the page shows: a new history entry (Back returns) or in place of the current one.
function syncUrl(push) {
  if (quiet) return;
  const url = pageUrl();
  if (url === location.pathname + location.search) return;
  if (push) history.pushState(null, "", url);
  else history.replaceState(history.state, "", url);
}
function setTab(name, push = true) {
  if (name !== "sessions") firstPage = false;
  if (push && name !== tab) history.pushState(null, "", pageUrl(drawer.isOpen() ? openItem : null, name));
  setTabName(name); document.querySelectorAll("nav button").forEach(b => b.classList.toggle("on", b.dataset.tab === tab));
  // A phone's bar (#4903): the name of the tab; Settings is no tab of the bar but opens from the ⋯ (#5116).
  $("#tabName").textContent = (document.querySelector("nav button.on") || {}).textContent || (tab === "settings" ? "Settings" : "");
  $("#settingsBtn").classList.toggle("on", tab === "settings");
  if (header.classList.contains("tabs-open") || header.classList.contains("more")) headerOpen(null);
  const on = document.querySelector("nav button.on"); if (on && on.parentElement.scrollWidth > on.parentElement.clientWidth) on.scrollIntoView({ block: "nearest", inline: "nearest" });
  for (const s of TABS) $("#tab-" + s).classList.toggle("hidden", s !== tab);
  plan.renderPlanViews(); plan.renderPlanSide();
  renderLog(true);
  if (tab === "sessions") sessionsTab.refresh();
  if (tab === "terminal") terminalTab.refresh();
  if (S && wantClosed() !== stateClosed) refresh().catch(() => {});  // this view shows done items, or no more
  return renderGraph(true);
}
// A session the person picks is a new entry; one the tab picks by itself replaces the address.
const sessionsTab = makeSessions({ isShown: () => tab === "sessions",
  onPick: (name, byPerson) => { if (byPerson) firstPage = false;
    view.session = name; view.art = null; view.pane = null; if (tab === "sessions") syncUrl(byPerson); },
  onPane: (kind, byPerson) => { view.pane = kind; view.session = null; view.art = null; if (kind === "arts") artifactsTab.refresh();
    if (tab === "sessions") syncUrl(byPerson); } });
// Another tab of the side panel, or none: the same entry, with the artifact's id.
onPanel((session, target) => {
  if (session !== view.session) return;
  view.art = !target ? null : /^item:\d+$/.test(target) ? target : artId(target);
  if (tab === "sessions") syncUrl(false);
});
wireItemPop();  // the pop-up of every item on the page (#4622)
installMics(() => S);  // a microphone in every box that takes text (#5107)
// Open in the Artifacts tab: the thing in the side panel, beside the chat of the session that made it.
const artifactsTab = makeArtifacts({ isShown: () => tab === "sessions" && view.pane === "arts",
  openBeside: async (session, target) => {
    quiet++;
    try { await setTab("sessions", false); if (session) sessionsTab.pick(session); openInPanel(target, session); } finally { quiet--; }
    syncUrl(true);
  } });
// The Terminal tab (#4803): plain shells on this computer.
const terminalTab = makeTerminalTab({ isShown: () => tab === "terminal" });
// Setup guide: an overlay that shows until the user dismisses it for good (setting setup_done).
let setupSeen = false;
// Ready for agents: every ready agent item, most important first, each with Dispatch (one session for one item).
const readyDialog = makeDialog($("#readyDlg"));
function openReady() { readyDialog.open(); renderReady(); $("#readyClose").focus(); }
function renderReady() {
  if (!S || !readyDialog.isOpen()) return;
  const rows = S.items.filter(i => i.ready && i.doer !== "human" && !["deploy", "review", "monitor"].includes(i.kind)).sort(itemOrder);
  $("#readyList").innerHTML = rows.map(i => `<div class="rrow"><div>
      <div class="t"><span class="link" data-open="${i.id}">#${i.id} ${esc(i.title)}</span></div>
      <div class="sub">${projectChip(i.project)}${prioNumChip(i.effective_priority ?? i.priority, "priority")}${doerChip(i)}
        ${i.reserved_for ? `<span>reserved for ${esc(i.reserved_for)}${i.reserved_until ? ", " + left(i.reserved_until) + " left" : ""}</span>` : ""}</div></div>
      ${i.reserved_for ? "" : agentStart(S.launch_agents, { label: "Dispatch", attrs: `data-dispatch="${i.id}"`,
        title: `Give #${i.id} to a session that waits in ${i.project}, or start the chosen agent in its folder for this item` })}
    </div>`).join("") || `<div class="muted">Nothing is ready for an agent now.</div>`;
}
$("#readyClose").onclick = () => readyDialog.close();
$("#readyDlg").addEventListener("click", async (e) => {
  if (e.target === e.currentTarget) return;  // the overlay: makeDialog closes it (#5027)
  if (e.target.closest("[data-open]")) return readyDialog.close();  // the item opens in the drawer
  const b = e.target.closest("[data-dispatch]"); if (!b || b.disabled) return;
  const id = +b.dataset.dispatch, it = S.items.find(i => i.id === id);
  const ch = await chooseLaunch(S, { title: "Dispatch", go: "Dispatch", what: `#${id} ${it ? it.title : ""}`, item: it });
  if (!ch) return;
  b.disabled = true;
  try {
    const r = await act("dispatch_item", { id, ...launchArgs(ch) });
    toast(r.pushed_to ? `Gave #${r.item.id} ${clip(r.item.title, 60)} to ${r.pushed_to}, which was waiting for work`
                      : `Started ${r.agent} as ${r.session_name} in ${r.project}, for #${r.item.id} ${clip(r.item.title, 60)}`);
  } catch (err) { b.disabled = false; /* toast shown */ }
});

// Search: the header field opens a dialog that lists the items, open, done, and dropped, that hold every
// word (core.search, as maxpm search), newest first; a click opens the item in the drawer (#2968).
const searchDialog = makeDialog($("#searchDlg"), { draft: false });  // a search is not worth asking about
let searchSeq = 0, searchTimer = null;
function openSearch(text) {
  if (S) fillSelect("#searchProject", S.projects.map(p => p.name), true);
  if (text != null) $("#searchText").value = text;
  searchDialog.open();
  const el = $("#searchText"); el.focus(); el.setSelectionRange(el.value.length, el.value.length);
  runSearch();
}
function runSearch(delay = 0) {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(doSearch, delay);
}
function searchRow(m) {
  const st = m.status === "open" || m.status === "backlog" ? chip("c-p", m.status) : chip("c-" + m.status, m.status.replace("_", " "));
  const when = m.closed_at ? `${m.status === "dropped" ? "dropped" : "done"} ${ago(m.closed_at)}` : `added ${ago(m.created_at)}`;
  return `<div class="qrow" data-open="${m.id}"><div class="t"><span class="link">#${m.id} ${esc(m.title)}</span></div>
    <div class="sub">${projectChip(m.project)}${st}${m.doer === "human" ? personChip("human") : ""}
      ${m.assignee && !m.closed_at ? `<span>${esc(m.assignee)}</span>` : ""}<span>${when}</span></div>
    ${m.output ? `<div class="out">${esc(clip(m.output, 220))}</div>` : ""}</div>`;
}
async function doSearch() {
  const text = $("#searchText").value.trim(), seq = ++searchSeq;
  if (!text) { $("#searchInfo").textContent = ""; $("#searchList").innerHTML = ""; return; }
  const q = new URLSearchParams({ q: text, status: $("#searchStatus").value, n: 100 });
  if ($("#searchProject").value) q.set("project", $("#searchProject").value);
  if ($("#searchSince").value) q.set("since", $("#searchSince").value);
  const r = await fetch("api/search?" + q).catch(() => null);
  if (seq !== searchSeq || !r) return;
  const res = await r.json();
  if (!r.ok) { $("#searchInfo").textContent = res.error; $("#searchList").innerHTML = ""; return; }
  if (res.items.length) {
    $("#searchInfo").textContent = res.total > res.items.length ? `${res.total} items; the newest ${res.items.length} shown.`
      : `${res.total} item${res.total === 1 ? "" : "s"}, newest first.`;
    $("#searchList").innerHTML = res.items.map(searchRow).join("");
  } else {
    $("#searchInfo").textContent = res.closest.length ? "No item holds every word. The closest:" : "No item holds every word.";
    $("#searchList").innerHTML = res.closest.map(searchRow).join("");
  }
}
$("#search").addEventListener("input", () => { const v = $("#search").value; $("#search").value = ""; openSearch(v); });
$("#search").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); openSearch(); } });
$("#searchText").addEventListener("input", () => runSearch(200));
for (const id of ["#searchStatus", "#searchProject", "#searchSince"]) $(id).addEventListener("change", () => runSearch());
$("#searchClose").onclick = () => searchDialog.close();
$("#searchDlg").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) return;  // the overlay: makeDialog closes it (#5027)
  if (e.target.closest("[data-open]")) return searchDialog.close();  // the item opens in the drawer
});
// "/" anywhere outside a field opens the search, as on many sites.
document.addEventListener("keydown", (e) => {
  if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey || searchDialog.isOpen()) return;
  const t = e.target; if (t.closest && t.closest("input, textarea, select, [contenteditable]")) return;
  e.preventDefault(); openSearch();
});

$("#projFolderForm").innerHTML = folderForm();
wireFolderForms(() => { if (setupDialog.isOpen()) openSetup(); });
async function openSetup() {
  const r = await fetch("api/setup"); if (!r.ok) return;
  renderSetup(await r.json()); setupDialog.open();
}
const setupDialog = makeDialog($("#setup"));
let setupSig = "";
function closeSetup() { setupDialog.close(); }
function renderSetup(st) {
  // The first run is a short path in order (you, river for agents, a folder, an agent, a first task, Start);
  // each step checks itself. The first step not done yet is marked as the next one. Extras come after, folded.
  let n = 0, next = true;
  // A step that is done shows only its title: its line is the title's hover, unless the line holds a control (#5082).
  const head = (ok, title, sub) => {
    const tip = ok && sub && !/<(button|details|select|input|a)\b/.test(sub);
    return [`<b${tip ? ` title="${esc(sub.replace(/<[^>]+>/g, ""))}"` : ""}>${title}</b>`, tip ? "" : sub];
  };
  const step = (ok, title, sub, fix) => {
    n++; const here = !ok && next; if (here) next = false;
    const [b, line] = head(ok, title, sub);
    return `<div class="step${here ? " next" : ""}"><div class="mark ${ok ? "ok" : "todo"}">${ok ? "✓" : n}</div>
      <div>${b}${line ? `<div class="sub">${line}</div>` : ""}${!ok && fix ? `<div class="fix">${fix}</div>` : ""}</div></div>`;
  };
  const extra = (ok, title, sub, fix) => { const [b, line] = head(ok, title, sub); return `<div class="step"><div class="mark ${ok ? "ok" : "opt"}">${ok ? "✓" : "·"}</div>
    <div>${b}${line ? `<div class="sub">${line}</div>` : ""}${!ok && fix ? `<div class="fix">${fix}</div>` : ""}</div></div>`; };
  const out = [];
  out.push(step(st.people.length > 0, "Tell MaximizePM your name",
    st.people.length ? "You're here as " + st.people.map(esc).join(", ") + ". Pick your name at the top right if it isn't chosen."
      : "So MaximizePM can show you what needs you, and let you know.",
    `<input id="suName" placeholder="your name" style="width:160px"><button class="btn primary" data-su="register">Save</button>`));
  // Agents run `maxpm go`: a new terminal must find the maxpm command (the app writes a small launcher).
  const rc = st.maxpm_cmd || {};
  if (!rc.unsupported) out.push(step(rc.ok, "Let agents use MaximizePM",
    rc.ok ? `Agents can run the maxpm command${rc.shell_path ? ` (${esc(rc.shell_path)})` : ""}.`
      : rc.in_app_image ? "First drag MaximizePM to your Applications folder, then open it from there."
      : rc.launcher === "old" ? "The maxpm command still points at an older copy of MaximizePM. Update it to this one."
      : "The agents you start talk to MaximizePM with a small command, maxpm. One click adds it to this Mac.",
    rc.in_app_image ? "" : `<button class="btn primary" data-su="maxpmcmd">${rc.launcher === "old" ? "Update" : "Add"} the maxpm command</button>`));
  // One instructions file for every agent: AGENTS.md holds the rules, CLAUDE.md imports it (@AGENTS.md).
  const blocks = st.folders.filter(f => f.exists && (f.claude_md !== "current" || f.agents_md !== "current"));
  const layouts = { claude_only: "its CLAUDE.md has rules that Codex and other agents don't read",
    both: "CLAUDE.md and AGENTS.md have different rules; each agent reads only one" };
  out.push(step(st.folders.length > 0 && blocks.length === 0, "Add a project folder",
    st.folders.length ? st.folders.map(f => `<div title="${esc(f.path)}">${esc(f.path.replace(/^\/(Users|home)\/[^/]+/, "~"))} <span class="muted">(${f.projects.map(esc).join(", ")})</span>`
      + (!f.exists ? " · folder not found" : layouts[f.layout] ? " · " + esc(layouts[f.layout]) : "")
      + (f.layout === "claude_only" ? ` <button class="btn" data-su="block" data-move="1" data-path="${esc(f.path)}"
          title="The text of CLAUDE.md goes to AGENTS.md; CLAUDE.md becomes the one line @AGENTS.md">Share them with every agent</button>` : "")
      + `</div>`).join("")
      + `<details style="margin-top:6px"><summary>Add another folder</summary>${folderForm()}</details>`
      : "The folder of something you're building. Agents you start work in it." + folderForm(),
    blocks.map(f => `<button class="btn" data-su="block" data-path="${esc(f.path)}">Update ${esc(f.path.split("/").pop())}</button>`).join("")));
  // An agent the Start button can open: installed here, and on the Start button's list.
  const ready = (st.start_agents || []).filter(a => a.found).map(a => a.label);
  const addable = st.agent_clis.filter(a => a.found && !a.added);
  const none = !ready.length && !addable.length;
  const addBtns = addable.map(a => `<button class="btn${ready.length ? "" : " primary"}" data-su="agent" data-label="${esc(a.label)}" title="${esc(a.command)}">Add ${esc(a.label)}</button>`).join("");
  const opens = "The Start button opens " + ready.map(esc).join(" or ")
    + (st.tmux ? " in a tmux pane (maxpm view in a terminal shows every agent side by side)."
      : ` in a tmux pane, and tmux is not installed: ${esc(st.tmux_install || "brew install tmux")}.`);
  out.push(step(ready.length > 0, ready.length ? `<span title="${opens}">Choose an agent</span>` : "Choose an agent",
    ready.length ? (addable.length ? `<span class="fix" style="display:inline-flex">${addBtns}</span>` : "")
      : none ? `No AI coding agent is installed on this Mac yet. Install one, then check again:
          <a href="https://docs.claude.com/en/docs/claude-code/setup" target="_blank" rel="noopener">Claude Code</a> or
          <a href="https://github.com/openai/codex" target="_blank" rel="noopener">Codex</a>.`
      : "Found on this Mac: " + addable.map(a => esc(a.label)).join(", ") + ". Add one to the Start button.",
    (none ? `<button class="btn" data-su="recheck">Check again</button>` : "") + addBtns));
  // How river starts each profile agent (Claude Code, Codex): its options, as global settings.
  const profs = st.launch_profiles || [];
  const howStart = profs.length && extra(true, `<span title="For every project; set one project apart in Settings (for example claude_remote_control with the project scope). The launch dialog can change the toggles for one start.">How agents start</span>`,
    profs.map(p => `<div style="margin:4px 0"><b>${esc(p.label)}</b> ${p.options.map(o => {
      const name = o.label || o.name.replace(/_/g, " ");
      return o.kind === "toggle"
        ? `<label class="su-opt" title="${esc(o.text)}"><input type="checkbox" data-su-opt="${esc(o.setting)}" ${o.value === "on" ? "checked" : ""}>${esc(name)}</label>`
        : o.kind === "choice"
        ? `<label class="su-opt" title="${esc(o.text)}">${esc(name)}<select data-su-opt="${esc(o.setting)}"><option value="">the agent's own</option>${o.choices.map(c => `<option ${c === o.value ? "selected" : ""}>${esc(c)}</option>`).join("")}</select></label>`
        : `<label class="su-opt" title="${esc(o.text)}">${esc(name)}<input data-su-opt="${esc(o.setting)}" value="${esc(o.value)}" class="su-w${o.name === "model_ids" ? "l" : o.name === "prompt" ? "m" : "s"}"></label>`;
    }).join("")}</div>`).join("")
, "");
  // A first item, then an agent that takes it. The page says "item" everywhere, not "task" (#4705).
  const tasks = (S && S.items || []).filter(i => !["deploy", "review", "monitor"].includes(i.kind));
  const closedN = closedTotal();
  const folderProjects = (S && S.projects || []).filter(p => p.path);
  out.push(step(tasks.length + closedN > 0, "Add a first item",
    tasks.length || closedN ? `${tasks.length} item${tasks.length === 1 ? "" : "s"} on the list${closedN ? `, ${closedN} done` : ""}.`
      : "Something small an agent can do in your project, so you can see how it goes.",
    folderProjects.length ? `<select id="suTaskProject">${folderProjects.map(p => `<option>${esc(p.name)}</option>`).join("")}</select>
      <input id="suTaskTitle" placeholder="for example: add a README that says what this project does" style="flex:1 1 280px">
      <button class="btn primary" data-su="task">Add item</button>` : `<span class="muted">Add a project folder first.</span>`));
  const started = (S && S.agents || []).some(a => a.kind !== "human") || closedN > 0 || tasks.some(i => ["in_progress", "held", "done"].includes(i.status));
  out.push(step(started, "Start an agent",
    started ? "An agent has picked up work. Watch it on Sessions; finished items show in Plan > Done."
      : "An agent opens in a new Terminal window, takes your item, and gets to work. You'll see it on Sessions.",
    tasks.length && ready.length ? agentStart(S.launch_agents, { label: "Start an agent", cls: "btn primary", attrs: 'data-launch="1"',
        title: "Open a new Terminal window in the project folder and start the chosen agent there" })
      : `<span class="muted">Needs an agent and an item first.</span>`));
  // Extras: nice to have, not needed for a first result.
  const more = howStart ? [howStart] : [];
  const missing = Object.entries(st.skills).filter(([, v]) => v !== "installed").map(([k]) => k);
  if (st.claude_home) more.push(extra(missing.length === 0, "Claude Code skills",
    missing.length ? "Teach Claude Code MaximizePM's full workflow (missing: " + missing.join(", ") + ")." : "maxpm and maxpm-manager are installed.",
    `<button class="btn" data-su="skills">Install skills</button>`));
  more.push(extra(st.ntfy_ready && st.notify_channels.includes("ntfy"), "Phone notifications",
    st.ntfy_ready ? "Channels: " + (st.notify_channels.map(esc).join(", ") || "none") : "Get a push on your phone when something needs you (ntfy).",
    `<button class="btn" data-su="ntfy">Turn on ntfy</button><span id="suNtfy" class="sub"></span>`));
  // Where a tap on a notification opens (notify_open, #4673): the person's own choice, else everyone's.
  const me = $("#actor").value, open = (me && (st.notify_open_people || {})[me]) || st.notify_open;
  more.push(extra(true, "Where a notification opens",
    `A tap on a notification opens <select id="suOpen" title="notify_open${me ? " for " + esc(me) : ""}">
      <option value="maxpm" ${open === "maxpm" ? "selected" : ""}>the MaximizePM page</option>
      <option value="agent" ${open === "agent" ? "selected" : ""}>the agent's own app (claude.ai/code, chatgpt.com/codex)</option></select>
      <div class="muted" style="font-size:12px">${esc(st.notify_phone || "")}</div>`, ""));
  const noTracker = folderProjects.filter(p => !p.tracker);
  if (folderProjects.length) more.push(extra(noTracker.length === 0, "Issue tracker",
    folderProjects.map(p => `<div>${esc(p.name)}: ${p.tracker ? esc(p.tracker) : "none"}</div>`).join("")
      + "If you use GitHub Issues, Jira, or Linear, agents can bring issues in and close them when they're done. Say which tracker, where, and with what tool.",
    noTracker.map(p => `<span class="su-track">${esc(p.name)}
      <input data-tracker-for="${esc(p.name)}" placeholder="github owner/repo via gh">
      <button class="btn" data-su="tracker" data-project="${esc(p.name)}">Save</button></span>`).join("")));
  $("#setupSteps").innerHTML = out.join("") + `<details class="more"><summary>More (optional)</summary>${more.join("")}</details>`;
}
$("#setupSteps").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-su]"); if (!b) return;
  const k = b.dataset.su;
  try {
    if (k === "register") {
      const n = $("#suName").value.trim(); if (!n) return toast("Type your name first", true);
      await act("register", { name: n, human: true, note: "" });
      $("#actor").value = n; store("river.actor", n);
    } else if (k === "block") await act("setup_block", { path: b.dataset.path,
      move: b.dataset.move === undefined ? null : b.dataset.move === "1" });
    else if (k === "tracker") {
      const v = document.querySelector(`[data-tracker-for="${CSS.escape(b.dataset.project)}"]`).value.trim();
      if (!v) return toast("Say which tracker, for example: github owner/repo via gh", true);
      await act("config_set", { key: "tracker", value: v, project: b.dataset.project });
    }
    else if (k === "skills") await act("setup_skills", {});
    else if (k === "task") {
      const title = $("#suTaskTitle").value.trim(); if (!title) return toast("Write the item first", true);
      await act("item_add", { project: $("#suTaskProject").value, title, doer: "ai" });
    }
    else if (k === "recheck") { /* openSetup below checks again */ }
    else if (k === "maxpmcmd") { const r = await act("setup_maxpm_cmd", {}); toast(r.note || "Added the maxpm command. Agents in new terminals can use it now."); }
    else if (k === "agent") await act("setup_agent_add", { label: b.dataset.label });
    else if (k === "ntfy") {
      const r = await act("setup_ntfy", {});
      await openSetup();
      $("#suNtfy").textContent = (r && r.subscribe || []).join(" ");
      return;
    }
    await openSetup();
  } catch (err) { /* act() showed the error */ }
});
// Enter in a field of the guide does what the button next to it does (the name, the first task, a tracker).
// The fields of the folder form have their own (folderForm.js), and an option saves when it changes.
$("#setupSteps").addEventListener("keydown", (e) => {
  const f = e.target;
  if (e.key !== "Enter" || e.isComposing || f.tagName !== "INPUT") return;
  const b = f.id === "suName" ? '[data-su="register"]' : f.id === "suTaskTitle" ? '[data-su="task"]'
    : f.dataset.trackerFor !== undefined ? `[data-su="tracker"][data-project="${CSS.escape(f.dataset.trackerFor)}"]` : null;
  const btn = b && $("#setupSteps").querySelector(b);
  if (btn) { e.preventDefault(); btn.click(); }
});
$("#setupSteps").addEventListener("change", async (e) => {
  if (e.target.id === "suOpen") {  // for the person in "You are", else for everyone
    const me = $("#actor").value, value = e.target.value;
    try { await act("config_set", { key: "notify_open", value, agent: me || null }); toast(`A tap on a notification opens ${value === "maxpm" ? "the MaximizePM page" : "the agent's own app"}`); }
    catch (err) { /* act() showed the error */ }
    return openSetup();
  }
  const x = e.target.closest("[data-su-opt]"); if (!x) return;
  const value = x.type === "checkbox" ? (x.checked ? "on" : "off") : x.value.trim();
  try { await act("config_set", { key: x.dataset.suOpt, value }); toast(`${x.dataset.suOpt} = ${value || "(the agent's own)"}`); }
  catch (err) { /* act() showed the error */ }
  await openSetup();
});
$("#setupClose").onclick = closeSetup;
$("#setupLater").onclick = closeSetup;
$("#setupDone").onclick = async () => { await act("config_set", { key: "setup_done", value: "on" }).catch(() => {}); closeSetup(); };
$("#openSetup").onclick = openSetup;
function maybeSetup() {
  if (setupSeen || !S) return;
  setupSeen = true;
  const done = S.settings.overrides.some(x => x.key === "setup_done" && x.scope === "global" && x.value === "on");
  if (!done) openSetup();
}

// The first page shows the You view when something waits on the person (#5102): at load, and at a later read of
// Needs you while the person has chosen nothing yet (the list can come a moment after the page).
let firstPage = false;
function firstYou() {
  if (!firstPage || tab !== "sessions" || sessionsTab.pane()) return;
  if (youView.youCount()) { firstPage = false; sessionsTab.showYou(false); }
}

// Show what the address names: at load, and after Back or Forward.
async function openFromUrl() {
  const r = readUrl();
  quiet++;
  try {
    asParam = r.as;
    if (r.as && r.as !== $("#actor").value && [...$("#actor").options].some(o => o.value === r.as)) {
      $("#actor").value = r.as; store("river.actor", r.as); await inbox.actorChanged().catch(() => {});
    }
    if (r.terminal) openTerminal(r.terminal);
    if (r.pane === "arts" && r.art) return await openArtifact(null, r.art);
    if (r.plan) { setPlan(r.plan.view, r.plan); planDraw(); }
    if (r.tab !== tab) await setTab(r.tab, false);
    if (r.pane === "you") sessionsTab.showYou(false);
    if (r.pane === "arts") sessionsTab.showArts(false);
    // The first page (#5102 step 5, design #4657): the You view when something waits on the person.
    if (r.root && !r.item) { firstPage = true; await needsYou.pollNeedsYou().catch(() => {}); firstYou(); }
    if (r.tab === "sessions" && r.session) {
      sessionsTab.pick(r.session);
      if (r.art && r.art !== view.art) await openArtifact(r.session, r.art);
    }
    // No item in the address (Back after opening one): the drawer closes.
    if (r.item != null) { if (r.item !== openItem || !drawer.isOpen()) await openDrawer(r.item); }
    else if (drawer.isOpen()) { drawer.close(); openItem = null; }
  } finally {
    quiet--;
    syncUrl(false);  // the address of what shows now: a gone artifact or an unknown path is written plainly
  }
}
// An artifact by its id: from the session's panel tabs, else from what the session (or any session) made.
async function openArtifact(session, id) {
  if (id.startsWith("item:")) {  // an item in the side panel (#4685)
    if (tab !== "sessions") await setTab("sessions", false);
    if (session) sessionsTab.pick(session);
    return openInPanel(id);
  }
  let target = session ? panelTargets().find(t => artId(t) === id) : null;
  if (!target) {
    const r = await fetch("api/artifacts" + (session ? "?agent=" + encodeURIComponent(session) : "")).catch(() => null);
    const hit = ((r && r.ok ? await r.json() : {}).artifacts || []).find(a => artId(a.target) === id);
    if (hit) { target = hit.target; session = session || hit.session; }
  }
  if (!target) { if (!session) { await setTab("sessions", false); sessionsTab.showArts(false); } return toast("That artifact is not in the list any more", true); }
  if (tab !== "sessions") await setTab("sessions", false);
  if (session) sessionsTab.pick(session);
  openInPanel(target, session);
}
window.addEventListener("popstate", openFromUrl);
// A link to a session's file or page anywhere on the page (#4689): the Sessions tab, that session, its side panel.
window.riverOpenInPanel = async (session, target) => {
  if (tab !== "sessions") await setTab("sessions");
  if (session) sessionsTab.pick(session);
  openInPanel(target, session);
};
wireArtLinks();
// Relay button: connects this MaximizePM to the relay (maxpm connect), so its page opens from any browser.
// A click starts the sign-in: the relay page opens with a code to approve, and maxpm serve finishes it.
let relayArmed = 0;  // a click on a connected relay asks again before it disconnects
function renderRelay() {
  const r = S.relay, b = $("#relayBtn");
  if (!r) return b.classList.add("hidden");
  b.classList.remove("hidden");
  const armed = Date.now() - relayArmed < 4000;
  b.classList.toggle("primary", !!r.pending || armed);
  if (r.pending) {
    b.textContent = `Relay code ${r.pending.code}`;
    b.title = "Approve this code on the relay page. Click to open the page again.";
  } else if (!r.configured) {
    b.textContent = "Connect relay";
    b.title = "Open this MaximizePM's page from any browser, through the relay"
      + (r.error ? `\nLast try: ${r.error}` : "");
  } else if (armed) {
    b.textContent = "Disconnect?";
    b.title = "Click again to disconnect this computer from the relay";
  } else {
    const who = r.account ? ` as ${r.account}` : "";
    // A lapsed relay plan (#4759): ended carries nothing until it is renewed; past_due still works.
    const ended = r.state === "connected" && r.plan === "ended", failed = r.state === "connected" && r.plan === "past_due";
    b.textContent = ended ? "Relay · plan ended" : r.state === "connected" ? "Relay · on" : "Relay · off";
    b.classList.toggle("warn", ended || failed);
    b.title = (r.state === "connected" ? `Connected${who} to ${r.url}` : `Not connected${who}: ${r.error || r.state}`)
      + (ended ? `\nYour relay plan ended: renew it at ${r.url}/account. Until then the relay carries nothing.` : "")
      + (failed ? `\nYour relay payment failed: update your card at ${r.url}/account.` : "")
      + `\nFrom anywhere: ${r.tunnel || r.url}\nClick to disconnect this computer.`;
  }
}
$("#relayBtn").onclick = async () => {
  const r = S.relay || {};
  if (r.pending) return void window.open(r.pending.link, "_blank", "noopener");
  if (!r.configured) {
    try {
      const got = await act("relay_connect", {});
      window.open(got.link, "_blank", "noopener");
      toast(`Approve code ${got.code} on the relay page`);
    } catch (e) { /* toast shown */ }
    return;
  }
  if (Date.now() - relayArmed > 4000) { relayArmed = Date.now(); renderRelay(); setTimeout(renderRelay, 4100); return; }
  relayArmed = 0;
  try {
    const res = await act("relay_disconnect", {});
    toast(res.revoked ? "Disconnected from the relay" : `Disconnected here; the relay did not confirm (${res.error})`, !res.revoked);
  } catch (e) { /* toast shown */ }
};

// Update button: shows how many new commits the river clone is missing; a click pulls them and restarts the server.
// It also says when the server itself is out of date: code changed on disk after it started (mode "restart"),
// or the server is too old to know the update route (mode "old": only a manual restart helps).
const RESTART_BY_HAND = "This page is newer than its server. Stop maxpm serve (Ctrl-C in its terminal) and start it again.";
let updateMode = "update";
async function checkUpdate() {
  const r = await fetch("api/update"), b = $("#updateBtn");
  const u = r.ok ? await r.json() : {};
  if (!r.ok) {
    updateMode = "old";
    b.classList.remove("hidden"); b.classList.add("primary");
    b.textContent = "Restart needed"; b.title = RESTART_BY_HAND;
    return;
  }
  if (!u.git && !u.stale) return b.classList.add("hidden");
  b.classList.remove("hidden");
  updateMode = u.behind ? "update" : u.stale ? "restart" : "update";
  b.classList.toggle("primary", u.behind > 0 || !!u.stale);
  b.textContent = u.behind ? `Update · ${u.behind} new` : u.stale ? "Restart · new code" : "Update";
  b.title = u.behind ? "New in MaximizePM:\n" + u.commits.join("\n")
      + (u.ahead ? `\n\nThis clone also has ${u.ahead} commit(s) of its own, so it cannot fast-forward: push or rebase them first.` : "")
    : u.stale ? "MaximizePM's code changed since this server started. The server starts again by itself within a minute of the commit; maxpm serve --restart does it now."
    : u.fetch_error ? "Could not check for updates: " + u.fetch_error
    : `Up to date (${u.head})` + (u.ahead ? `; this clone has ${u.ahead} commit(s) not on ${u.upstream} yet` : "");
}
async function waitForRestart(boot) {
  for (let i = 0; i < 60; i++) {  // wait for the restarted server, then load the new page
    await new Promise(res => setTimeout(res, 500));
    try { const s = await (await fetch("api/update?fetch=0")).json(); if (s.boot !== boot) return location.reload(); } catch (e) {}
  }
  toast("The server did not come back; run maxpm serve again", true);
}
$("#updateBtn").onclick = async () => {
  if (updateMode === "old") return toast(RESTART_BY_HAND, true);
  const b = $("#updateBtn"); b.disabled = true; b.textContent = updateMode === "restart" ? "Restarting…" : "Updating…";
  try {
    const boot = (await (await fetch("api/update?fetch=0")).json()).boot;
    const r = await fetch("api/action", { method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ op: updateMode, args: {}, actor: actor() }) });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || updateMode + " failed");
    if (updateMode === "restart") { toast("Restarting the server…"); return await waitForRestart(boot); }
    const u = j.result;
    if (!u.updated) { toast(`Already up to date (${u.head})`); return; }
    toast(`Updated ${u.from} → ${u.head}. Restarting…`);
    await waitForRestart(boot);
  } catch (e) { toast(e.message, true); }
  finally { b.disabled = false; checkUpdate().catch(() => {}); }
};
$("#loopLate").onclick = () => {
  if (S.desktop) return toast("Quit the app and open it again: that starts its automatic work again", true);
  if (updateMode !== "old") updateMode = "restart";
  $("#updateBtn").onclick();
};
// The header on a phone and a tablet. The ⋯ (#4885) shows the controls that do not fit: on a phone (#4903) as a menu
// under the bar of 48 px. On a phone the name of the tab opens the sheet of all tabs, and the magnifier opens the
// search. One of the two is open at a time; a tap outside them (on the scrim), Esc, or a tab closes them.
const hdrChange = (open) => {
  if (open) header.classList.remove("hdr-hide");
  $("#hdrScrim").classList.toggle("hidden", !header.classList.contains("more") && !header.classList.contains("tabs-open"));
};
const hdrMenu = makeMenu($("#hdrMore"), header, { cls: "more", group: "header", onChange: hdrChange });
const tabSheet = makeMenu($("#tabBtn"), header, { cls: "tabs-open", group: "header", onChange: hdrChange });
function headerOpen(which) { hdrMenu.set(which === "more"); tabSheet.set(which === "tabs-open"); }
$("#hdrScrim").onclick = () => headerOpen(null);
$("#searchBtn").onclick = () => { headerOpen(null); openSearch(); };
// The bar of a phone hides while the person scrolls down and comes back when they scroll up a little (24 px) or come
// near the top: a transform, so nothing under it moves. Never while the sheet, the menu, or a field of it is open.
{
  const phone = window.matchMedia("(max-width: 759px)");
  let lastY = window.scrollY, upFrom = null, ticking = false;
  const step = () => {
    ticking = false;
    const y = Math.max(0, window.scrollY), dy = y - lastY; lastY = y;
    if (!phone.matches || header.classList.contains("more") || header.classList.contains("tabs-open") || header.contains(document.activeElement)
        || y < header.offsetHeight) { upFrom = null; return header.classList.remove("hdr-hide"); }
    if (dy > 0) { upFrom = null; if (y > header.offsetHeight) header.classList.add("hdr-hide"); }
    else if (dy < 0) { if (upFrom == null) upFrom = y - dy; if (upFrom - y >= 24) header.classList.remove("hdr-hide"); }
  };
  window.addEventListener("scroll", () => { if (!ticking) { ticking = true; requestAnimationFrame(step); } }, { passive: true });
  phone.addEventListener("change", step);
  header.addEventListener("focusin", () => header.classList.remove("hdr-hide"));
}
checkUpdate().catch(() => {}); setInterval(() => { if (!document.hidden) checkUpdate().catch(() => {}); }, 30 * 60 * 1000);
// A stale server is cheap to spot (no git fetch), so look for it more often than for new commits.
setInterval(() => {
  if (document.hidden || updateMode !== "update") return;  // hidden, or already asking for a restart
  fetch("api/update?fetch=0").then(r => r.ok ? r.json() : null).then(u => { if (u && u.stale) checkUpdate(); }).catch(() => {});
}, 60 * 1000);
// The tab in the address shows at once, before the state comes (#4888): Sessions, Artifacts, Done and Terminal read
// their own data, and the others draw when the state is there. A first state that fails says so in the loading line.
{
  const r = readUrl();
  firstPage = r.root && r.item == null;  // before the session list writes the address of the session it shows
  {
    try { if (r.plan) setPlan(r.plan.view, r.plan); setTab(r.tab, false); if (r.tab === "sessions" && r.session) sessionsTab.pick(r.session, false);
      if (r.pane === "you") sessionsTab.showYou(false); if (r.pane === "arts" && !r.art) sessionsTab.showArts(false); } catch (e) { /* the state draws it */ }
  }
}
refresh().then(openFromUrl).then(maybeSetup).catch((e) => {
  const boot = $("#bootNote"); if (boot) boot.textContent = `Could not load the queue (${e.message}); the page asks again every 3 seconds.`;
});
setInterval(() => { if (!document.hidden) refresh().catch(() => {}); }, 3000);
// A hidden tab stops the full refresh but keeps asking what needs a person, so notifications still arrive.
setInterval(() => { if (document.hidden) needsYou.pollNeedsYou().catch(() => {}); }, 15000);

