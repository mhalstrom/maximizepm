import { $, esc, store, actor, toast, act, ago, left, clip, copyText, fillSelect, hooks } from "./lib.js";
import { chip, prioNumChip, doerChip, personChip, projectChip, ownerChip, countChip } from "./components/chip.js";
import { bar, pct, capacityBars } from "./components/bar.js";
import { nyCard, goalCard, projCard } from "./components/card.js";
import { itemRow, itemOrder, itemTableRows, itemTableColumns } from "./components/itemRow.js";
import { makeTable } from "./components/table.js";
import { makeDrawer, itemDrawerHtml, msgLine } from "./components/drawer.js";
import { makeDialog } from "./components/dialog.js";
import { agentStart } from "./components/agentStart.js";
import { chooseLaunch, launchArgs } from "./components/launchDialog.js";
import { managerHtml, queueHtml, agentButtons, chooseStop } from "./components/agentActions.js";
import { openTerminal } from "./components/terminalDialog.js";
import { makePanZoom } from "./components/panZoom.js";
import { graphItems, graphText, GRAPH_LIMITS, GRAPH_MAX } from "./components/graphText.js";
import { folderForm, wireFolderForms } from "./components/folderForm.js";
import { hooksHtml } from "./components/targetHooks.js";
hooks.refresh = refresh;
let S = null, openItem = null, tab = "board", graphSig = "";
const drawer = makeDrawer($("#drawer"));

let actorPicked = false;

// Goal filter: "" all items, "__none" items without a goal, else one goal's items.
function goalSel() { return $("#goalFilter").value; }
function inGoal(it) { const g = goalSel(); return !g || (g === "__none" ? !(it.goals || []).length : (it.goals || []).includes(g)); }


function renderCapacity() {
  const c = S.capacity;
  const layers = capacityBars(c.layers);
  $("#capacity").innerHTML = `<h2>Parallel work</h2>
    <div class="stats">
      <div class="stat ${c.spare_slots ? "good" : ""}"${c.spare_slots ? ` data-launch="1" style="cursor:pointer" title="Click to start an agent session in a new terminal window"` : ""}><div class="n">${c.spare_slots}</div><div class="l">open slots: more agent sessions you can start now${c.spare_slots ? " (click to start one)" : ""}</div></div>
      <div class="stat ${c.excess_sessions ? "bad" : ""}"><div class="n">${c.excess_sessions}</div><div class="l">sessions with nothing ready for them</div></div>
      <div class="stat"><div class="n">${c.agents_active}</div><div class="l">active agent sessions (${c.agents_busy} busy)</div></div>
      <div class="stat"><div class="n">${c.in_progress.length}</div><div class="l">items in progress</div></div>
      <div class="stat hum"><div class="n">${c.ready_for_humans.length}</div><div class="l">ready items that wait on a human</div></div>
    </div>
    <ul class="advice">${c.advice.map(a => `<li class="${a.kind}">${esc(a.text)}</li>`).join("")}</ul>
    <div class="layers">${layers || '<span class="muted">No open items.</span>'}</div>
    <div class="legend"><span><span class="sw" style="background:var(--accent)"></span>open items for agents</span><span><span class="sw" style="background:var(--human)"></span>for humans</span><span>by step: 1 = can start now, 2 = after step 1, …  Peak agent width ${c.peak_width}.</span></div>`;
}

function renderNext() {
  const area = $("#area").value;
  if (area === "__mine") { $("#next").innerHTML = `<div class="muted">Use <b>Claim next</b>: the server picks the ready item closest to what "${esc(actor() || "(choose your name in You are)")}" claimed or finished before.</div>`; return; }
  const ready = S.items.filter(i => i.ready && (!area || i.project === area) && inGoal(i));
  $("#next").innerHTML = ready.slice(0, 5).map(i => itemRow(i, i.reason)).join("") || `<div class="muted">Nothing is ready${area ? " in " + esc(area) : ""}.</div>`;
}

// Projects folded closed on the Projects tab, remembered in this browser.
function projClosed() { try { return JSON.parse(store("river.projClosed") || "[]"); } catch (e) { return []; } }
function renderProjects() {
  const showDone = $("#showDone").checked;
  const G = goalSel(), goals = S.goals || [];
  const html = S.projects.map(p => {
    const pg = goals.filter(g => g.project === p.name && (g.status === "open" || showDone || g.name === G));
    const all = S.items.filter(i => i.project === p.name && inGoal(i));
    if (G && G !== "__none" && !all.length && !pg.some(g => g.name === G)) return "";
    const open = all.filter(i => !["done", "dropped"].includes(i.status));
    const closed = all.filter(i => ["done", "dropped"].includes(i.status));
    const ready = open.filter(i => i.ready).length, prog = open.filter(i => ["in_progress", "held"].includes(i.status)).length;
    const closedSet = projClosed(), isClosed = closedSet.includes(p.name);
    const who = [...new Set(open.filter(i => i.assignee && ["in_progress", "held"].includes(i.status)).map(i => i.assignee))];
    const home = p.path ? p.path.replace(/^\/(Users|home)\/[^/]+/, "~") : "";
    const meta = [home ? `folder <code>${esc(home)}</code>` : "no folder",
      p.target ? `deploys to <b>${esc(p.target)}</b>` : "", p.tracker ? `tracker ${esc(p.tracker)}` : ""].filter(Boolean);
    const me = actor();
    return projCard(p, isClosed, `<div class="pmeta">${meta.map(m => `<span>${m}</span>`).join("")}</div>
      <div class="pcounts">${countChip(ready, "ready", "c-ready")}${countChip(prog, "in progress", "c-in_progress")}${countChip(open.length, "open", "c-p")}${countChip(closed.length, "done", "c-p")}${pg.length ? countChip(pg.length, pg.length === 1 ? "goal" : "goals", "c-p") : ""}</div>
      <div class="pbody">
        <div class="pdesc">${p.notes ? esc(p.notes) : '<span class="muted">No description.</span>'} <span class="link" data-describe="${esc(p.name)}">edit</span></div>
        <div class="pwho">${who.length ? "Working here now: " + who.map(n => `<b>${esc(n)}</b>`).join(", ") : "Nobody works here now."}</div>
        <div class="psec">Goals</div>
        <div class="goals">${pg.map(g => goalCard(g, { selected: G === g.name, me })).join("")}<span class="link" style="font-size:12px;align-self:center" data-addgoal="${esc(p.name)}">${pg.length ? "+ goal" : "No goals yet. + Add a goal"}</span></div>
        <div class="psec">Open items (${open.length})${showDone && closed.length ? `, ${closed.length} done` : ""}</div>
        <div class="ptable" data-ptable="${esc(p.name)}"></div>
      </div>`);
  }).join("");
  // The cards redraw only when they change; each project's item table lives on between redraws.
  const box = $("#projects"), full = html || `<div class="muted">No projects yet.</div>`;
  if (box.dataset.sig !== full) {
    box.innerHTML = full; box.dataset.sig = full;
    box.querySelectorAll("[data-ptable]").forEach(ph => {
      const name = ph.dataset.ptable, known = projTables.get(name);
      if (known) ph.replaceWith(known.el);
      else projTables.set(name, { el: ph, table: makeTable(ph, { key: "proj:" + name, sort: [{ column: "order", dir: "asc" }],
        columns: itemTableColumns((it) => ["done", "dropped"].includes(it.status) ? it.output : it.notes),
        placeholder: "No open items.", onRow: (r) => openDrawer(r.id) }) });
    });
  }
  for (const [name, t] of projTables) {
    if (!box.contains(t.el)) { t.table.tabulator.destroy(); projTables.delete(name); continue; }
    t.table.set(itemTableRows(S.items.filter(i => i.project === name && inGoal(i) && (showDone || !["done", "dropped"].includes(i.status)))));
  }
}
const projTables = new Map();

// An agent session not seen for away_after, holding and owning nothing, has most likely stopped: hide it.
// People always show, and so does any agent that still holds an item, owns a goal or target, or has a push waiting.
function agentShown(a) {
  if (a.kind === "human" || a.state === "active" || a.holds.length || (a.owns || []).length) return true;
  if ((S.goals || []).some(g => g.owner === a.name && g.status === "open")) return true;
  return S.items.some(i => i.status === "open" && i.reserved_until && i.reserved_for === a.name);
}
// A waiting session (maxpm wait) takes a pushed item within seconds: offer the ready items of its projects.
function giveWork(a) {
  const where = (a.waiting_in || "").split(",").filter(Boolean);
  const ready = S.items.filter(i => i.ready && i.doer !== "human" && i.kind === "work" && !i.reserved_for
    && (!where.length || where.includes(i.project)));
  const since = a.waiting_since ? ` since ${ago(a.waiting_since)}` : "";
  if (!ready.length) return `<div class="st">${chip("c-ready", "waiting" + since)} no ready item in ${esc(where.join(", ") || "its projects")}</div>`;
  return `<div class="st" style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">${chip("c-ready", "waiting" + since)}
    <select data-give-item="${esc(a.name)}" style="max-width:220px">${ready.map(i => `<option value="${i.id}"${giveChoice[a.name] == i.id ? " selected" : ""}>#${i.id} ${esc(clip(i.title, 50))}</option>`).join("")}</select>
    <button class="btn" data-give="${esc(a.name)}">Give work</button></div>`;
}

const giveChoice = {};
// Agents and people as a table. Name and Holds always show; on a narrow panel the other columns
// fold into a detail row (the ▸ at the start of the row). A row is a drop target for an item row.
let agentsTable = null;
// The loop of maxpm serve is late (S.loop.late): it does no automatic work. One button starts the server again.
function renderLoop() {
  const l = S.loop, b = $("#loopLate");
  b.classList.toggle("hidden", !(l && l.late));
  if (!l || !l.late) return;
  b.textContent = `Automatic work stopped · ${l.age}`;
  b.title = `maxpm serve made no complete pass of its loop for ${l.age}, so it closes no finished session, sends no notification, starts no fresh session and does not reload.`
    + (l.error ? `\nLast error: ${l.error}` : "") + "\nClick to start the server again (maxpm serve --restart).";
}
function renderAgents() {
  renderLoop();
  // The tmux panes of sessions that are done (S.tmux_done): one button closes them, as maxpm view --tidy does.
  const done = S.tmux_done || [];
  $("#tidyPanes").classList.toggle("hidden", !done.length);
  $("#tidyPanes").textContent = `Close ${done.length} finished`;
  $("#tidyPanes").title = "Close the tmux panes of the sessions that are done (maxpm view --tidy). A session that holds work, or shows a prompt, stays.\n"
    + done.map(p => `${p.name}: ${p.why}`).join("\n");
  // The panel redraws every few seconds: leave it alone while someone picks from a Give work menu.
  if (document.activeElement && document.activeElement.matches("#agents select")) return;
  const hidden = S.agents.filter(a => !agentShown(a)), showIdle = $("#showIdle").checked;
  $("#showIdleWrap").classList.toggle("hidden", !hidden.length);
  $("#showIdleText").textContent = `show ${hidden.length} stopped`;
  agentsTable ||= makeTable($("#agents"), { key: "agents", index: "name", placeholder: "No agents registered.",
    responsiveLayout: "collapse", responsiveLayoutCollapseStartOpen: false,
    rowFormatter: (row) => { row.getElement().dataset.agent = row.getData().name; },
    columns: [
      { formatter: "responsiveCollapse", width: 28, minWidth: 28, headerSort: false, filter: false, resizable: false },
      { title: "Name", field: "name", minWidth: 120, widthGrow: 1, responsive: 0, cssClass: "wrap",
        html: (r) => `<span class="dot ${r.state}"></span><span class="nm">${esc(r.name)}</span>${r.a.note ? `<div class="st">${esc(r.a.note)}</div>` : ""}` },
      { title: "Holds", field: "holds", minWidth: 140, widthGrow: 1, responsive: 0, cssClass: "wrap", html: (r) => agentHolds(r.a) },
      { title: "Status", field: "state", width: 90, filter: "select", responsive: 1 },
      { title: "Kind", field: "kind", width: 80, filter: "select", responsive: 2, html: (r) => personChip(r.a.kind) },
      { title: "Role", field: "role", width: 90, filter: "select", responsive: 2 },
      { title: "Seen", field: "last_seen", width: 84, filter: false, responsive: 3, html: (r) => ago(r.last_seen) },
      { title: "Session", field: "session", width: 150, responsive: 4, cssClass: "wrap", html: (r) => agentSession(r.a) },
    ] });
  agentsTable.set(S.agents.filter(a => showIdle || agentShown(a)).map(a => ({
    name: a.name, state: a.state, kind: a.kind === "human" ? "human" : "agent", role: a.role || "", last_seen: a.last_seen,
    session: [a.session, a.session_ref].filter(Boolean).join(" "), a,
    holds: a.holds.map(h => `#${h.id} ${h.title}`).join(" "),
    // what the Holds cell shows changes with time, so it is part of the row
    view: agentHolds(a),
  })));
}
function renderManager() {
  const h = managerHtml(S);
  if ($("#manager").dataset.sig !== h) { $("#manager").innerHTML = h; $("#manager").dataset.sig = h; }
}
// What an agent holds, owns, was pushed, or (while it waits) can be given; its queue and its buttons.
function agentHolds(a) {
  return `${a.role === "waiting" && !a.holds.length ? giveWork(a) : ""}
      ${(S.goals || []).filter(g => g.owner === a.name && g.status === "open").map(g => `<div class="st">owns goal <span class="link" data-goal="${esc(g.name)}">${esc(g.name)}</span>${g.owner_expires_at ? " · " + left(g.owner_expires_at) + " left" : ""}</div>`).join("")}
      ${a.holds.map(h => `<div class="st">holds <span class="link" data-open="${h.id}">#${h.id} ${esc(h.title)}</span>${h.lease_expires_at ? " · " + left(h.lease_expires_at) + " left" : ""}</div>`).join("") || '<div class="st">holds nothing</div>'}
      ${S.items.filter(i => i.status === "open" && i.reserved_until && i.reserved_for === a.name).map(i => `<div class="st">pushed <span class="link" data-open="${i.id}">#${i.id} ${esc(i.title)}</span> · ${left(i.reserved_until)} left · <span class="link" data-unpush="${i.id}">cancel</span></div>`).join("")}
      ${queueHtml((S.queues || {})[a.name], a.name)}${agentButtons(a, S)}`;
}
// A chat over the local /mcp of maxpm serve (agents.via).
const VIA_LABEL = { "http": "chat on this computer" };
function agentSession(a) {
  return `${a.via ? `<span class="chip" title="a chat session that reaches MaximizePM over MCP (${esc(a.via)})">${esc(VIA_LABEL[a.via] || a.via)}</span> ` : ""}${a.model ? `<span class="chip c-p" title="the model this session runs (MAXPM_MODEL)">${esc(a.model)}</span> ` : ""}${a.session ? `<span title="Claude Code session">${esc(a.session)}${a.session_ref ? " [" + esc(a.session_ref) + "]" : ""}</span>` : ""}${a.session_url ? ` <a class="link" href="${esc(a.session_url)}" target="_blank" rel="noopener">open</a>` : ""}`;
}

// The monitor sessions that followed a deploy (maxpm target monitor).
function monitorLines(d) {
  return (d.monitors || []).map(m => `<div class="muted" style="font-size:12px">monitor <span class="link" data-open="${m.id}">#${m.id}</span> (${esc(m.status.replace("_", " "))}${m.assignee ? " · " + esc(m.assignee) : ""})${m.output ? ": " + esc(clip(m.output, 200)) : ""}</div>`).join("");
}

function renderTargets() {
  const T = S.targets || [];
  const box = $("#targets");
  if (box.contains(document.activeElement) && document.activeElement.tagName === "INPUT") return;  // do not wipe a reason they type
  const shipList = (s) => s.map(x => `<span class="link" data-open="${x.id}">#${x.id}</span> ${esc(clip(x.title, 50))} <span class="muted">(${esc(x.status.replace("_", " "))})</span>`).join("; ") || '<span class="muted">nothing yet</span>';
  $("#targets").innerHTML = T.map(t => nyCard(`<b>${esc(t.name)}</b>
        ${ownerChip(t.owner)}${t.owner ? `<span class="muted" style="font-size:12px">${t.owner_expires_at ? left(t.owner_expires_at) + " left" : ""}</span>` : ""}
        <span class="muted" style="font-size:12px">${t.project_names.length ? "projects: " + t.project_names.map(esc).join(", ") : "no projects"}</span>`, `${t.description ? `<div class="ny-c">${esc(clip(t.description, 300))}</div>` : ""}
      <div class="muted" style="font-size:12px">${t.monitor ? "monitor after each deploy: " + esc(clip(t.monitor, 200)) : `no monitor (a session follows each deploy when you set one: maxpm target monitor ${esc(t.name)} "&lt;what to watch, for how long&gt;")`}</div>
      ${hooksHtml(t, S.hook_events)}
      ${t.pending.map(d => `<div class="st" style="margin-top:4px"><span class="link" data-open="${d.id}">#${d.id}</span> ${d.ready ? "ready to deploy" : d.status === "open" ? (d.cut_at ? "cut " + ago(d.cut_at) : "collecting") : esc(d.status.replace("_", " ")) + (d.assignee ? " · " + esc(d.assignee) : "")}: ${shipList(d.ships)}${d.review ? `<div class="muted" style="font-size:12px">first a review: <span class="link" data-open="${d.review.id}">#${d.review.id}</span> (${esc(d.review.status.replace("_", " "))}${d.review.assignee ? " · " + esc(d.review.assignee) : ""})</div>` : ""}${monitorLines(d)}</div>`).join("") || '<div class="st muted">No pending ship requests.</div>'}
      ${t.release && t.release.text ? `<div class="muted" style="font-size:12px">${esc(t.release.text)}${t.release.last ? ` · last release ${ago(t.release.last.at)}` : ""}</div>` : ""}
      <div class="actions" style="margin-top:6px">${t.release && t.release.waits
        ? `<input id="relwhy-${esc(t.name)}" placeholder="why this release cannot wait for the cadence" style="flex:1;min-width:180px"> <button class="btn" data-relnow="${esc(t.name)}" title="Start this release sooner than the cadence permits; the reason goes in the history of the deploy item">Release now</button>`
        : t.pending.some(d => d.status === "open" && d.ships.length)
        ? agentStart(S.launch_agents, [
            { label: "Deploy now", cls: "btn primary", attrs: `data-deploy="${esc(t.name)}"`, title: "Start the deploy now: the owner gets an alert, or the chosen agent opens to take it" },
            { label: "Review and deploy", attrs: `data-deploy="${esc(t.name)}" data-review="1"`, title: "First one review of everything it ships (review steps per project), then the deploy" }])
        : '<span class="muted" style="font-size:12px">Nothing to deploy: ship items first (maxpm ship &lt;id&gt;, or done --ship).</span>'}</div>
      <div class="st" style="margin-top:6px"><b>Deploys</b></div>
      ${(t.history || []).map(h => `<div class="st" style="margin-top:4px"><span class="link" data-open="${h.id}">#${h.id}</span> ${ago(h.closed_at)}${h.done_by ? " by " + esc(h.done_by) : ""}${h.output ? ": " + esc(clip(h.output, 200)) : ""}<div class="muted" style="font-size:12px">shipped ${shipList(h.ships)}</div>${monitorLines(h)}</div>`).join("") || '<div class="st muted">Never deployed.</div>'}`)).join("") || `<div class="muted">No deploy targets. Add one: maxpm target add &lt;name&gt; --description "how it deploys"</div>`;
}

function renderBlocked() {
  const B = S.items.filter(i => i.blocked_reason && !["done", "dropped"].includes(i.status));
  $("#blockedPanel").classList.toggle("hidden", !B.length);
  $("#blockedList").innerHTML = B.map(i => nyCard(`<b class="link" data-open="${i.id}">#${i.id} ${esc(i.title)}</b> ${projectChip(i.project)}${doerChip(i)}`, `<div class="ny-c">${esc(i.blocked_reason)}</div>
      <div class="muted" style="font-size:12px">${i.blocked_at ? "since " + ago(i.blocked_at) : "since: not recorded"} · ${i.blocked_until ? `until ${esc(i.blocked_until_text)} (${left(i.blocked_until)} left), then ready by itself` : "until someone clears it"}${i.blocked_set_by ? " · set by " + esc(i.blocked_set_by) : ""}</div>`)).join("");
}

let takeoversOpen = false;
function renderTakeovers() {
  const T = S.takeovers || [];
  $("#takeoverPanel").classList.toggle("hidden", !T.length);
  const words = { "took over": "took over", "done": "marked done", "dropped": "dropped" };
  const head = `<div class="nyl" data-takeovers-toggle="1"><span class="t">${takeoversOpen ? "▾" : "▸"} ${T.length} item${T.length === 1 ? "" : "s"} an agent took off your list: review or undo</span><span class="chips"><button class="btn" data-takeovers-okall="1" title="Accept all of them">OK all</button></span></div>`;
  if (!takeoversOpen) { $("#takeovers").innerHTML = head; return; }
  $("#takeovers").innerHTML = head + T.map(t => nyCard(`<b>#${t.id} ${esc(t.title)}</b> ${projectChip(t.project)}<span class="muted" style="font-size:12px">${ago(t.takeover_at)}</span>`, `<div class="ny-c"><b>${esc(t.takeover_by)}</b> ${words[t.takeover_kind] || t.takeover_kind} it: ${esc(t.takeover_note || "")}</div>
      <div class="actions"><button class="btn" data-open="${t.id}">Open</button><button class="btn" data-undo-takeover="${t.id}">Undo: give it back to me</button><button class="btn primary" data-takeover-ok="${t.id}">OK</button></div>`)).join("");
}

// The status strip: counts you click to go where they are.
function renderStrip() {
  if (!S) return;
  const ready = S.items.filter(i => i.ready && i.doer !== "human" && !["deploy", "review", "monitor"].includes(i.kind)).length;
  const running = S.items.filter(i => ["in_progress", "held"].includes(i.status)).length;
  const blocked = S.items.filter(i => i.blocked_reason && !["done", "dropped"].includes(i.status)).length;
  const T = (S.takeovers || []).length, slots = S.capacity ? S.capacity.spare_slots : 0;
  const b = (n, label, go, cls) => `<button data-strip="${go}" class="${n ? cls : ""}"><b>${n}</b>${label}</button>`;
  const html = b(NY.length, "need you", "needs", "hum") + (T ? b(T, "taken off your list", "takeovers", "hum") : "")
    + b(ready, "ready for agents", "ready", "good") + b(running, "in progress", "work", "")
    + b(blocked, "blocked outside", "blocked", "bad") + b(slots, "open agent slots", "capacity", "good")
    + (ready ? agentStart(S.launch_agents, { label: "Start an agent", cls: "primary", attrs: 'data-launch="1"',
        title: "Open a new terminal tab (a console window on Windows) in the folder of the most important ready item and start the chosen agent there" }) : "");
  if ($("#strip").dataset.sig !== html) { $("#strip").innerHTML = html; $("#strip").dataset.sig = html; }
  // The other fixed places that start an agent: Needs you > Open agent, and Claim next with an agent.
  const put = (el, h) => { if (el.dataset.sig !== h) { el.innerHTML = h; el.dataset.sig = h; } };
  put($("#agentAllSlot"), agentStart(S.launch_agents, { label: "Open agent", attrs: 'id="agentAll"', title: "Open an agent session that walks you through all of these, with the same prompt" }));
}

// Work column: one row per project; a click opens its open items (ready first).
function workOpen() { try { return new Set(JSON.parse(store("river.work.open") || "[]")); } catch (e) { return new Set(); } }
function renderWork() {
  const open = workOpen();
  $("#work").innerHTML = S.projects.map(p => {
    const items = S.items.filter(i => i.project === p.name && !["done", "dropped"].includes(i.status));
    if (!items.length) return "";
    const ready = items.filter(i => i.ready).length, run = items.filter(i => ["in_progress", "held"].includes(i.status)).length;
    const hum = items.filter(i => i.ready && i.doer === "human").length;
    const isOpen = open.has(p.name), shown = items.sort(itemOrder).slice(0, 8);
    return `<div class="wp"><div class="wp-h" data-wp="${esc(p.name)}"><span class="tw">${isOpen ? "▾" : "▸"}</span><b>${esc(p.name)}</b>
        <span class="counts">${items.length} open · ${ready} ready${run ? ` · ${run} running` : ""}${hum ? ` · ${hum} for you` : ""}</span></div>
      ${isOpen ? `<div class="wp-items">${shown.map(i => itemRow(i)).join("")}${items.length > shown.length ? `<div class="muted" style="font-size:12px;padding:4px 6px">${items.length - shown.length} more: <span class="link" data-tab="projects">Projects tab</span></div>` : ""}</div>` : ""}</div>`;
  }).join("") || `<div class="muted">No open items.</div>`;
}

function renderEvents() {
  $("#events").innerHTML = S.events.slice(0, 15).map(e => `<div class="ev">${ago(e.at)} · <b>${esc(e.actor)}</b> ${esc(e.change)}${e.item_id ? ` <span class="link" data-open="${e.item_id}">#${e.item_id}</span>` : ""}</div>`).join("");
}

function renderSelects() {
  const names = S.projects.map(p => p.name);
  { const el = $("#area"), cur = el.value; el.innerHTML = `<option value="">All projects</option><option value="__mine">Near my earlier work</option>` + names.map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join(""); if ([...el.options].some(o => o.value === cur)) el.value = cur; } fillSelect("#logProject", names, true); fillSelect("#addProject", names, false);
  const a = $("#actor"), cur = a.value || store("river.actor") || "";
  // Only people act from this page; agents act through the maxpm command.
  const people = S.agents.filter(x => x.kind === "human");
  a.innerHTML = `<option value="">(choose your name)</option>` + people.map(x => `<option value="${esc(x.name)}">${esc(x.name)}</option>`).join("");
  if (people.some(x => x.name === cur)) a.value = cur;
  else if (!a.value && people.length === 1 && !actorPicked) { a.value = people[0].name; actorPicked = true; store("river.actor", a.value); }
  fillSelect("#setKey", Object.keys(S.settings.defaults), false);
  fillSelect("#goalProject", names, false);
  const openGoals = (S.goals || []).filter(g => g.status === "open");
  { const el = $("#goalFilter"), cur = el.value || store("river.goal") || "";
    const extra = cur && cur !== "__none" && !openGoals.some(g => g.name === cur) && (S.goals || []).some(g => g.name === cur) ? [cur] : [];
    el.innerHTML = `<option value="">All goals</option><option value="__none">No goal</option>` + openGoals.map(g => g.name).concat(extra).map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
    el.value = [...el.options].some(o => o.value === cur) ? cur : ""; }
  { const el = $("#addGoal"), cur = el.value, proj = $("#addProject").value;
    const list = openGoals.filter(g => g.project === proj).concat(openGoals.filter(g => g.project !== proj));
    el.innerHTML = `<option value="">no goal</option>` + list.map(g => `<option value="${esc(g.name)}">${esc(g.name)}${g.project !== proj ? " (" + esc(g.project) + ")" : ""}</option>`).join("");
    if ([...el.options].some(o => o.value === cur)) el.value = cur; }
}

// Settings: one row per key; an override chip's × removes that override.
let settingsTable = null;
function renderSettings() {
  const d = S.settings.defaults, o = S.settings.overrides;
  settingsTable ||= makeTable($("#settingsTable"), { key: "settings", index: "key", sort: [{ column: "key", dir: "asc" }],
    placeholder: "No settings.",
    columns: [
      { title: "Key", field: "key", width: 200 },
      { title: "Default", field: "default", width: 240, cssClass: "muted wrap" },
      { title: "Overrides", field: "overrides", cssClass: "wrap", html: (r) => r.ov.map(x => chip("c-p", `${esc(x.scope)} = ${esc(x.value)} <span class="link" data-unset="${esc(r.key)}" data-scope="${esc(x.scope)}">×</span>`)).join(" ") },
    ] });
  settingsTable.set(Object.keys(d).map(k => {
    const ov = o.filter(x => x.key === k);
    return { key: k, default: String(d[k]), overrides: ov.map(x => `${x.scope}=${x.value}`).join(" "), ov };
  }));
}

let logSig = "", logTable = null;
const logTime = (iso) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const logDay = (d) => new Date(d + "T12:00:00Z").toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
// The Done log: one row per finished item, grouped by day; every column sorts and filters.
function makeLogTable() {
  return makeTable($("#log"), {
    key: "done", sort: [{ column: "closed_at", dir: "desc" }], placeholder: "Nothing done in this period.",
    onRow: (r) => openDrawer(r.id),
    groupBy: "day", groupHeader: (day, count) => `<b>${esc(logDay(day))}</b> <span class="muted">${count} done</span>`,
    columns: [
      { title: "#", field: "id", width: 64, filter: false },
      { title: "Title", field: "title", minWidth: 180, cssClass: "wrap" },
      { title: "Project", field: "project", width: 150, filter: "select", html: (r) => projectChip(r.project) },
      { title: "Output", field: "output", minWidth: 180, cssClass: "wrap muted", html: (r) => `<div class="clamp" title="${esc(r.output)}">${esc(r.output)}</div>` },
      { title: "By", field: "by", width: 130, filter: "select" },
      { title: "Finished", field: "closed_at", width: 96, filter: false, html: (r) => logTime(r.closed_at) },
    ],
  });
}
async function renderLog(force) {
  if (tab !== "done") return;
  const q = new URLSearchParams({ since: $("#logSince").value });
  if ($("#logProject").value) q.set("project", $("#logProject").value);
  const r = await fetch("api/log?" + q); if (!r.ok) return;
  const L = await r.json(), sig = JSON.stringify(L);
  if (!force && sig === logSig) return;
  logSig = sig;
  logTable ||= makeLogTable();
  logTable.set(L.by_day.flatMap(d => d.items.map(it => ({ id: it.id, title: it.title, project: it.project, output: it.output || "",
    by: it.by_agent && it.by_agent !== "?" ? it.by_agent : "", closed_at: it.closed_at, day: d.day }))));
  const win = L.since ? $("#logSince").selectedOptions[0].textContent : null;
  $("#progress").innerHTML = L.progress.map(p => {
    const done = pct(p.done, p.total);
    return `<div class="prog"><div class="prog-h"><b>${esc(p.project)}</b><span class="muted">${p.done}/${p.total} · ${done}%</span></div>
      ${bar(done)}
      ${win ? `<div class="muted" style="font-size:12px;margin-top:2px">+${p.done_in_window} in the ${esc(win)}</div>` : ""}</div>`;
  }).join("") || `<div class="muted">No items.</div>`;
}

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
async function renderGraph(force) {
  if (tab !== "graph" || !window.mermaid) return;
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
window.riverOpen = (id) => openDrawer(id);
// Closing takes the item out of the URL: Back when this page added it, else the URL is rewritten.
function closeDrawer() {
  const id = openItem;
  drawer.close(); openItem = null;
  if (hashItem() !== id) return;
  if (history.state && history.state.item === id) history.back();
  else history.replaceState(null, "", pageHash(tab, null));
}


async function openDrawer(id, fresh = false) {
  openItem = id;
  // A new entry when no item was open (Back closes it); another item replaces the open one.
  if (hashItem() == null) history.pushState({ item: id }, "", pageHash(tab, id));
  else if (hashItem() !== id) history.replaceState(history.state && history.state.item != null ? { item: id } : null, "", pageHash(tab, id));
  const r = await fetch(`api/item/${id}`); if (!r.ok) return;
  drawer.show(id, itemDrawerHtml(await r.json(), { S, me: actor() }), fresh);
}

async function drawerAction(what) {
  const id = openItem;
  const it = S.items.find(i => i.id === id);
  try {
    if (what === "claim") await act("claim", { id });
    if (what === "done") { const out = prompt("Output note (optional)") ?? undefined; await act("done", { id, output: out || undefined }); }
    if (what === "release") await act("release", { id });
    if (what === "drop") await act("drop", { id });
    if (what === "reopen") await act("reopen", { id });
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
      await act("item_edit", { id, title: $("#dTitle").value, notes: $("#dNotes").value, context: $("#dContext").value, touches: $("#dTouches").value, check: $("#dCheck").value, due: $("#dDue").value.trim() || undefined, doer: $("#dDoer").value, project: $("#dProject").value !== it.project ? $("#dProject").value : undefined,
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

// ---- Inbox: messages for the person in "You are", with the answer each kind needs.
// inboxGen goes up after an action or a change of person, so every inbox row redraws (clears sent text).
let IB = [], inboxGen = 0;

async function pollInbox() {
  const me = actor();
  $("#inboxPanel").classList.toggle("hidden", !me);
  if (!me) return;
  const r = await fetch(`api/inbox?agent=${encodeURIComponent(me)}${$("#inboxAll").checked ? "&all=1" : ""}`); if (!r.ok) return;
  IB = (await r.json()).messages;
  renderInbox();
}

// The inbox as a table. The Message column holds the whole message with its answer, reply and decline
// controls; on a narrow panel From, Kind, Item, To, Sent and State fold into a detail row.
let inboxTable = null;
function inboxDone(m) { return !(m.unread || (m.state === "open" && ["question", "offer"].includes(m.kind))); }
function inboxCell(m) {
  const me = actor();
  const open = m.state === "open", it = m.item_id ? S.items.find(i => i.id === m.item_id) : null;
  let acts = "";
  if (m.kind === "question" && open)
    acts = `<input id="ans-${m.id}" placeholder="your answer"><button class="btn primary" data-ib="answer" data-msg="${m.id}">Answer</button>`;
  else if (m.kind === "offer" && open) {
    const mine = it && it.assignee === me;
    acts = (mine ? `<button class="btn primary" data-ib="give" data-msg="${m.id}" data-id="${m.item_id}" data-to="${esc(m.from_agent)}">Give #${m.item_id} to ${esc(m.from_agent)}</button>
        <input id="split-${m.id}" placeholder="smaller pieces, separated by ;"><button class="btn" data-ib="split" data-msg="${m.id}" data-id="${m.item_id}">Split</button>` : "")
      + `<input id="dec-${m.id}" placeholder="why not (optional)"><button class="btn" data-ib="decline" data-msg="${m.id}">Decline</button>`;
  } else {
    const pushed = m.kind === "alert" && it && it.status === "open" && it.reserved_for === me && it.reserved_until;
    if (pushed) acts += `<button class="btn primary" data-ib="accept" data-id="${it.id}">Accept #${it.id}</button><button class="btn" data-ib="decline-push" data-id="${it.id}">Decline #${it.id}</button>`;
    else if (m.kind === "alert" && open) acts += `<input id="dec-${m.id}" placeholder="why not (optional)"><button class="btn" data-ib="decline" data-msg="${m.id}">Decline</button>`;
    acts += `<input id="rep-${m.id}" placeholder="reply"><button class="btn" data-ib="reply" data-msg="${m.id}">Reply</button>`;
    if (m.unread) acts += `<button class="btn" data-ib="read" data-msg="${m.id}">Mark read</button>`;
  }
  return `<div class="ny-h">${chip(m.kind === "alert" ? "c-p0" : "c-p", esc(m.kind))}<b>${esc(m.from_agent)}</b>
      ${m.item_id ? `<span class="link" data-open="${m.item_id}">#${m.item_id} ${esc(clip(m.item_title || "", 60))}</span>` : ""}
      <span class="muted" style="font-size:12px">${ago(m.created_at)} · #${m.id}${m.state !== "open" && m.state !== "read" ? " · " + esc(m.state) : ""}</span>
      <span class="link" data-thread="${m.id}">thread</span></div>
    <div class="body">${esc(m.body)}</div>
    <div class="actions">${acts}</div>
    <div class="thread hidden" id="thr-${m.id}"></div>`;
}
function renderInbox() {
  const me = actor();
  $("#inboxTitle").textContent = `Inbox for ${me}` + (IB.length ? ` (${IB.filter(m => m.unread || (["question", "offer"].includes(m.kind) && m.state === "open")).length})` : "");
  fillSelect("#sendTo", S.agents.map(a => a.name).filter(n => n !== me), false);
  const ib = $("#inbox");
  if (ib.contains(document.activeElement) && document.activeElement.tagName === "INPUT") return;  // do not wipe what they type
  inboxTable ||= makeTable(ib, { key: "inbox", sort: [{ column: "created_at", dir: "desc" }],
    placeholder: "No messages.", responsiveLayout: "collapse", responsiveLayoutCollapseStartOpen: false,
    rowFormatter: (row) => { row.getElement().classList.add("msg"); row.getElement().classList.toggle("read", inboxDone(row.getData().m)); },
    columns: [
      { formatter: "responsiveCollapse", width: 28, minWidth: 28, headerSort: false, filter: false, resizable: false },
      { title: "Message", field: "body", minWidth: 320, responsive: 0, cssClass: "wrap", html: (r) => inboxCell(r.m) },
      { title: "From", field: "from", width: 120, filter: "select", responsive: 1 },
      { title: "Kind", field: "kind", width: 90, filter: "select", responsive: 1 },
      { title: "Item", field: "item", width: 70, responsive: 2, html: (r) => r.m.item_id ? `<span class="link" data-open="${r.m.item_id}">#${r.m.item_id}</span>` : "" },
      { title: "To", field: "to", width: 110, filter: "select", responsive: 3 },
      { title: "Sent", field: "created_at", width: 84, filter: false, responsive: 2, html: (r) => ago(r.created_at) },
      { title: "State", field: "state", width: 90, filter: "select", responsive: 2 },
    ] });
  // A row changes only when its message, or the pushed item it offers, changes.
  inboxTable.set(IB.map(m => {
    const it = m.item_id ? S.items.find(i => i.id === m.item_id) : null;
    return { id: m.id, body: m.body, from: m.from_agent, kind: m.kind, item: m.item_id ? "#" + m.item_id : "", to: m.to_agent || "next holder",
      created_at: m.created_at, state: m.unread ? "unread" : m.state, m, gen: inboxGen,
      it: it && [it.status, it.assignee, it.reserved_for, it.reserved_until] };
  }));
}

async function inboxAction(t) {
  const what = t.dataset.ib, msg = +t.dataset.msg, id = +t.dataset.id, val = (p) => ($("#" + p + "-" + msg) || {}).value?.trim();
  try {
    if (what === "answer") { const body = val("ans"); if (!body) return toast("Write the answer first", true); await act("answer", { msg, body }); toast("Answered"); }
    if (what === "give") { await act("give", { id, to: t.dataset.to }); toast(`Gave #${id} to ${t.dataset.to}`); }
    if (what === "split") { const titles = (val("split") || "").split(";").map(x => x.trim()).filter(Boolean); if (!titles.length) return toast("Write the smaller pieces, separated by ;", true); await act("split", { id, titles }); toast(`Split #${id} into ${titles.length}`); }
    if (what === "decline") { await act("decline_message", { msg, note: val("dec") || undefined }); toast("Declined"); }
    if (what === "accept") { await act("accept", { id }); toast(`#${id} is yours`); }
    if (what === "decline-push") { await act("decline", { id }); toast(`Handed #${id} back`); }
    if (what === "reply") { const body = val("rep"); if (!body) return toast("Write the reply first", true); await act("send", { kind: "note", body, reply: msg }); toast("Sent"); }
    if (what === "read") await act("message_read", { msg });
  } catch (e) { /* toast shown */ }
  inboxGen++; document.activeElement && document.activeElement.blur(); await pollInbox();
}

async function toggleThread(msg) {
  const box = $("#thr-" + msg); if (!box) return;
  if (!box.classList.contains("hidden")) return box.classList.add("hidden");
  const r = await fetch(`api/thread/${msg}`); if (!r.ok) return;
  box.innerHTML = (await r.json()).messages.map(msgLine).join("");
  box.classList.remove("hidden");
  const row = inboxTable && inboxTable.tabulator.getRow(msg); if (row) row.normalizeHeight();
}

// ---- Needs you: decisions and steps waiting for a person, with browser notifications.
let NY = [], nySig = "";
const nyOpen = new Set();
function humanActor() { const a = S && S.agents.find(x => x.name === actor()); return a && a.kind === "human" ? a.name : null; }

async function pollNeedsYou() {
  const h = humanActor();
  const r = await fetch("api/needs-you" + (h ? "?human=" + encodeURIComponent(h) : "")); if (!r.ok) return;
  NY = (await r.json()).events;
  renderNeedsYou(); notifyNew();
}

// An alert river sent for an agent that waits on a prompt in its terminal (core.PROMPT_NOTE), or for a manager that
// runs no watch (core.WATCH_NOTE): its Terminal answers it.
const promptAlert = e => e.message_kind === "alert" && ["waits on a prompt in its terminal", "runs no watch"].some(w => (e.body || "").startsWith(w));

function renderNeedsYou() {
  document.title = NY.length ? `(${NY.length}) MaximizePM` : "MaximizePM";
  $("#notifyOn").classList.toggle("hidden", !("Notification" in window) || Notification.permission !== "default");
  $("#needsYouPanel").classList.toggle("hidden", !NY.length);
  $("#nothingForYou").classList.toggle("hidden", !!NY.length || !!(S && (S.takeovers || []).length));
  renderStrip();
  const sig = JSON.stringify([NY.map(e => [e.id, e.summary, e.item_status]), [...nyOpen]]);
  if (sig === nySig) return;
  nySig = sig;
  $("#needsYou").innerHTML = NY.map(e => {
    const isItem = e.kind === "item";
    const detail = isItem ? (e.item_context || e.item_notes) : e.body;
    const buttons = isItem
      ? `<button class="btn" data-open="${e.item_id}">Open</button><button class="btn" data-ny="claim" data-id="${e.item_id}">Claim</button><button class="btn" data-copy-prompt="${e.item_id}" title="A prompt for an agent that explains this and helps you do it">Copy prompt</button>${agentStart(S && S.launch_agents, { label: "Open agent", attrs: `data-agent-help="${e.item_id}"`, title: "Open an agent session with that prompt, in the project folder" })}<button class="btn primary" data-ny="done" data-id="${e.item_id}">Done</button>`
      : (e.message_kind === "question"
          ? `<button class="btn primary" data-ny="answer" data-msg="${e.message_id}">Answer</button>`
          : (promptAlert(e) ? `<button class="btn primary" data-terminal="${esc(e.from_agent)}" title="${(e.body || "").startsWith("runs no watch") ? "Tell the manager to start its watch again" : "Read the prompt and answer it"}">Terminal</button>` : "")
            + `<button class="btn" data-ny="read" data-msg="${e.message_id}">Mark read</button>`)
        + (e.item_id ? `<button class="btn" data-open="${e.item_id}">Open #${e.item_id}</button>` : "");
    // One line each; a click opens the details and the buttons (design: the Board fits one screen).
    const title = isItem ? `#${e.item_id} ${e.item_title}` : `${e.message_kind} from ${e.from_agent}: ${e.body || ""}`;
    const open = nyOpen.has(e.id);
    return `<div class="nyl" data-nyt="${e.id}" title="${esc(clip(detail || title, 300))}"><span class="t">${esc(title)}</span>
        <span class="chips">${e.priority != null ? prioNumChip(e.priority, (e.priority_from ? `priority from #${e.priority_from}` : "own priority") + (e.unblocks_count ? `; unblocks ${e.unblocks_count}` : "")) : ""}${e.project ? projectChip(e.project) : ""}</span></div>
      ${open ? `<div class="nyx"><div class="muted" style="font-size:12px">${ago(e.opened_at)}${e.project ? " · " + esc(e.project) : ""}</div>${detail ? `<div class="ny-c">${esc(clip(detail, 800))}</div>` : ""}<div class="actions">${buttons}</div></div>` : ""}`;
  }).join("");
}

// One browser notification per event, once: seen ids live in localStorage. The first
// load only records what is already open, so opening the page does not fire a burst.
function notifyNew() {
  let seen = null;
  try { seen = JSON.parse(store("river.ny.seen") || "null"); } catch (e) { seen = null; }
  const first = !Array.isArray(seen);
  const known = new Set(first ? [] : seen);
  const fresh = NY.filter(e => !known.has(e.id));
  if (!first && "Notification" in window && Notification.permission === "granted") {
    for (const e of fresh.slice(0, 5)) {
      const n = new Notification("MaximizePM: needs you", { body: e.summary, tag: "maxpm-" + e.id });
      n.onclick = () => { window.focus(); if (promptAlert(e)) openTerminal(e.from_agent); else if (e.item_id) openDrawer(e.item_id); n.close(); };
    }
  }
  store("river.ny.seen", JSON.stringify([...known, ...fresh.map(e => e.id)].slice(-500)));
}

async function copyPrompt(url) {
  const h = humanActor(); const r = await fetch(url + (h ? "?person=" + encodeURIComponent(h) : ""));
  const j = await r.json(); if (!r.ok) return toast(j.error || "failed", true);
  toast(await copyText(j.prompt) ? "Prompt copied: paste it into a new agent session" : "Could not copy; your browser blocked it", !1);
}

async function needsYouAction(t) {
  const what = t.dataset.ny, id = +t.dataset.id, msg = +t.dataset.msg;
  try {
    if (what === "claim") { if (!actor()) return toast("Choose your name in 'You are' first", true); await act("claim", { id }); }
    if (what === "done") { const out = prompt("What did you decide or do? (saved as the item's output)"); if (out === null) return; await act("done", { id, output: out || undefined }); }
    if (what === "answer") {
      if (!actor()) return toast("Choose your name in 'You are' first", true);
      const body = prompt("Your answer"); if (!body) return; await act("answer", { msg, body });
    }
    if (what === "read") await act("message_read", { msg });
  } catch (e) { /* toast shown */ }
}

async function refresh() {
  const r = await fetch("api/state"); S = await r.json();
  if (S.dev_build) { if (window._build && window._build !== S.dev_build) return location.reload(); window._build = S.dev_build; }
  renderSelects(); renderCapacity(); renderNext(); renderProjects(); renderWork(); renderStrip(); renderReady(); renderAgents(); renderManager(); renderEvents(); renderSettings(); renderTakeovers(); renderBlocked(); renderTargets();
  renderRelay();
  renderGraph(false); renderLog(false); pollNeedsYou().catch(() => {}); pollInbox().catch(() => {});
  $("#stamp").textContent = "updated " + new Date().toLocaleTimeString();
  if (openItem != null && drawer.isOpen()) openDrawer(openItem);
  // The setup guide's last steps follow the queue: when a task is added or an agent picks one up, it checks again.
  const firstRun = JSON.stringify([S.items.length, S.agents.some(a => a.kind !== "human"), S.items.some(i => ["in_progress", "held", "done"].includes(i.status))]);
  if (setupDialog.isOpen() && firstRun !== setupSig && !$("#setupSteps").contains(document.activeElement)) openSetup();
  setupSig = firstRun;
}

document.addEventListener("click", async (e) => {
  const t = e.target;
  const nyt = t.closest("[data-nyt]"); if (nyt && !t.closest("button")) { const id = +nyt.dataset.nyt; nyOpen.has(id) ? nyOpen.delete(id) : nyOpen.add(id); return renderNeedsYou(); }
  if (t.dataset.takeoversOkall) { for (const x of S.takeovers || []) await act("takeover_seen", { id: x.id }).catch(() => {}); return; }
  if (t.closest("[data-takeovers-toggle]") && !t.closest("button")) { takeoversOpen = !takeoversOpen; return renderTakeovers(); }
  const wp = t.closest("[data-wp]"); if (wp) { const o = workOpen(), n = wp.dataset.wp; o.has(n) ? o.delete(n) : o.add(n); store("river.work.open", JSON.stringify([...o])); return renderWork(); }
  const gw = t.closest("[data-give]"); if (gw) {
    const to = gw.dataset.give, sel = document.querySelector(`[data-give-item="${CSS.escape(to)}"]`);
    if (sel && sel.value) await act("push", { id: +sel.value, to, note: "from the page: you were waiting for work" })
      .then(() => toast(`Gave #${sel.value} to ${to}`)).catch(() => {});
    return; }
  if (t.dataset.startManager) {
    const ch = await chooseLaunch(S, { title: "Start the manager", go: "Start manager",
      what: "Manager: plans with you, starts agents, fills their queues, and stops stuck ones (maxpm manage)" });
    if (!ch) return;
    try { const r = await act("start_manager", launchArgs(ch)); toast(`Started ${r.agent} as ${r.session_name}, the manager`); } catch (e) { /* toast shown */ }
    return; }
  if (t.dataset.chat) {
    try { const r = await act("open_chat", { agent: t.dataset.chat });
      if (r.url) window.open(r.url, "_blank", "noopener"); else if (r.focused) toast(`Opened the terminal tab of ${r.agent}`);
      else if (r.tmux_pane) openTerminal(r.agent); else toast(r.hint, true);
    } catch (e) { /* toast shown */ }
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
  const sb = t.closest("[data-strip]"); if (sb) { const go = sb.dataset.strip;
    if (go === "blocked") { await setTab("projects"); return $("#blockedPanel").scrollIntoView({ block: "start" }); }
    if (go === "capacity") return setTab("capacity");
    if (go === "ready") return openReady();
    await setTab("board");
    if (go === "takeovers") { takeoversOpen = true; renderTakeovers(); }
    return $(go === "work" ? "#colWork" : "#colNeeds").scrollIntoView({ block: "start" }); }
  if (t.dataset.addgoal) { await setTab("projects"); const d = $("#goalName").closest("details"); d.open = true; $("#goalProject").value = t.dataset.addgoal; d.scrollIntoView({ block: "center" }); $("#goalName").focus(); return; }
  if (t.dataset.goalAct) return goalAction(t.dataset.goalAct, t.dataset.g);
  if (t.dataset.untag) { await act("item_edit", { id: openItem, untag: [t.dataset.untag] }).catch(() => {}); return openDrawer(openItem, true); }
  const card = t.closest("[data-goal]"); if (card) { const g = card.dataset.goal; setGoal(goalSel() === g ? "" : g); return; }
  const open = t.closest("[data-open]"); if (open) return openDrawer(+open.dataset.open);
  const row = t.closest(".row"); if (row) return openDrawer(+row.dataset.id);
  if (t.id === "dClose") return closeDrawer();
  if (t.dataset.do) return drawerAction(t.dataset.do);
  if (t.dataset.ny) return needsYouAction(t);
  if (t.dataset.ib) return inboxAction(t);
  if (t.dataset.thread) return toggleThread(+t.dataset.thread);
  if (t.id === "sendGo") return sendForm();
  if (t.dataset.offer) { const n = +t.dataset.offer, body = $("#offer-" + n).value.trim(); if (!body) return toast("Say what you can take", true);
    return act("offer", { item: n, body }).then(() => toast(`Offer sent to the holder of #${n}`)).catch(() => {}); }
  if (t.dataset.copyPrompt) return copyPrompt(`api/item/${t.dataset.copyPrompt}/prompt`);
  if (t.id === "copyAll") return copyPrompt("api/prompt-all");
  if (t.dataset.relnow) {
    const name = t.dataset.relnow, why = document.getElementById("relwhy-" + name), reason = why ? why.value.trim() : "";
    if (!reason) return toast("Say why this release cannot wait", true);
    why.blur();
    return act("release_now", { target: name, reason }).then(() => toast(`The release of ${name} can start now`)).catch(() => {});
  }
  if (t.dataset.hook) {
    const name = t.dataset.target, event = t.dataset.hook;
    const has = ((S.targets.find(x => x.name === name) || {}).hooks || []).find(h => h.event === event);
    if (t.dataset.clear) {
      if (!confirm(`Remove the ${event} hook of ${name}?` + (has ? "\n" + has.command : ""))) return;
      return act("target_hook", { target: name, event, clear: true }).then(() => toast(`Hook ${event} of ${name} removed`)).catch(() => {});
    }
    const command = prompt(`The shell command that MaximizePM runs for ${name} when ${(S.hook_events || {})[event] || "the event " + event + " occurs"}`, has ? has.command : "");
    if (command === null || (has && command.trim() === has.command)) return;
    if (!command.trim()) return has ? toast(`The command is empty. To take the hook away, use remove`, true) : undefined;
    return act("target_hook", { target: name, event, command })
      .then(r => toast(`Hook ${event} of ${name} ${r.changed}: it runs in ${r.folder || "the folder of the command that causes the event"}, for at most ${r.timeout}`)).catch(() => {});
  }
  if (t.dataset.deploy) {
    if (t.dataset.busy) return; t.dataset.busy = "1"; setTimeout(() => delete t.dataset.busy, 4000);
    const ch = await chooseLaunch(S, { title: t.dataset.review ? "Review and deploy" : "Deploy now", go: "Start",
      what: `${t.dataset.review ? "review and deploy" : "deploy"} ${t.dataset.deploy} (when the target has an owner, MaximizePM alerts the owner instead)` });
    if (!ch) return;
    try {
      const r = await act("deploy_now", { target: t.dataset.deploy, review: !!t.dataset.review, ...launchArgs(ch) });
      const what = `#${r.start.id} ${clip(r.start.title, 50)}`;
      toast(!r.ready ? `${what} waits on ${r.waits_on.map(w => "#" + w.id).join(", ")}; it starts when they are done`
        : r.alerted ? `${what} is ready; alerted the owner ${r.alerted}`
        : `Started ${r.agent} in ${r.project} to ${r.start.kind === "review" ? "review" : "deploy"} ${r.target}`);
    } catch (e) { /* toast shown */ }
    return;
  }
  if (t.id === "agentAll" || t.dataset.agentHelp) {
    if (t.dataset.busy) return; t.dataset.busy = "1"; setTimeout(() => delete t.dataset.busy, 4000);
    const hid = +t.dataset.agentHelp, hit = hid ? S.items.find(i => i.id === hid) : null;
    const ch = await chooseLaunch(S, { title: "Open an agent", go: "Open agent", item: hit,
      what: t.id === "agentAll" ? "go through everything that waits on you, with you" : `#${hid} ${hit ? hit.title : ""}, with you` });
    if (!ch) return;
    const opts = { person: actor() || undefined, ...launchArgs(ch) };
    try {
      const r = t.id === "agentAll" ? await act("open_needs_you", opts) : await act("open_agent_on", { ...opts, id: +t.dataset.agentHelp });
      toast(`Started ${r.agent} in ${r.project}` + (t.id === "agentAll" ? ", to go through what needs you" : `, to do #${t.dataset.agentHelp} with you`));
    } catch (e) { /* toast shown */ }
    return;
  }
  if (t.id === "notifyOn") { await Notification.requestPermission(); return renderNeedsYou(); }
  if (t.dataset.undoTakeover) { await act("undo_takeover", { id: +t.dataset.undoTakeover }).then(() => toast("Back on your list")).catch(() => {}); return; }
  if (t.dataset.takeoverOk) { await act("takeover_seen", { id: +t.dataset.takeoverOk }).catch(() => {}); return; }
  if (t.dataset.unpush) { await act("push_cancel", { id: +t.dataset.unpush }).then(() => toast("Push cancelled")).catch(() => {}); return; }
  if (t.dataset.undep) { await act("dep_remove", { id: openItem, on: [+t.dataset.undep] }).catch(() => {}); return openDrawer(openItem, true); }
  if (t.dataset.describe) { const p = S.projects.find(x => x.name === t.dataset.describe); const text = prompt(`What does "${p.name}" cover, and what context helps an agent work on it?`, p.notes || ""); if (text !== null) await act("project_describe", { name: p.name, text }).catch(() => {}); return; }
  if (t.dataset.rank) { const to = +t.dataset.to; if (to >= 1) await act("project_rank", { name: t.dataset.rank, rank: to }).catch(() => {}); return; }
  if (t.dataset.unset) { const sc = t.dataset.scope, [kind, name] = sc.includes(":") ? [sc.split(":")[0], sc.slice(sc.indexOf(":") + 1)] : ["global", null];
    const args = { key: t.dataset.unset }; if (kind !== "global") args[kind] = kind === "item" ? +name : name;
    return act("config_unset", args).catch(() => {}); }
  if (t.dataset.tab) return setTab(t.dataset.tab);
  if (t.dataset.fold) { const c = projClosed(), n = t.dataset.fold; store("river.projClosed", JSON.stringify(c.includes(n) ? c.filter(x => x !== n) : c.concat(n))); return renderProjects(); }
});

$("#actor").addEventListener("change", () => { store("river.actor", $("#actor").value); inboxGen++; pollInbox().catch(() => {}); });
$("#inboxAll").addEventListener("change", () => { inboxGen++; pollInbox().catch(() => {}); });
async function sendForm() {
  if (!actor()) return toast("Choose your name in 'You are' first", true);
  const body = $("#sendBody").value.trim(), item = $("#sendItem").value.replace("#", "").trim();
  if (!body) return toast("Write the message first", true);
  const args = { kind: $("#sendKind").value, body };
  if (item) args.item = +item; else args.to = $("#sendTo").value;
  try { await act("send", args); $("#sendBody").value = ""; $("#sendItem").value = ""; toast("Sent"); } catch (e) { /* toast shown */ }
}
function setGoal(g) { $("#goalFilter").value = g; store("river.goal", g); renderNext(); renderProjects(); renderGraph(true); }
async function goalAction(what, name) {
  const g = (S.goals || []).find(x => x.name === name); if (!g) return;
  try {
    if (what === "up" || what === "down") {
      const sib = S.goals.filter(x => x.project === g.project).sort((a, b) => a.rank - b.rank);
      const to = sib.findIndex(x => x.name === name) + 1 + (what === "up" ? -1 : 1);
      if (to >= 1 && to <= sib.length) await act("goal_rank", { name, rank: to });
    }
    if (what === "own" || what === "release") {
      if (!actor()) return toast("Choose your name in 'You are' first", true);
      await act("goal_" + what, { name }); toast(what === "own" ? `You own ${name}` : `Released ${name}`);
    }
    if (what === "edit") {
      const outcome = prompt(`Outcome of "${name}": what is true when it is reached?`, g.outcome || ""); if (outcome === null) return;
      const done_when = prompt("Done when: the test the owner checks", g.done_when || ""); if (done_when === null) return;
      await act("goal_edit", { name, outcome, done_when });
    }
    if (what === "done") {
      const result = prompt(`What did "${name}" achieve? (one line)` + (g.items_open.length ? `\n${g.items_open.length} tagged items are still open; they will be dropped.` : "")); if (!result) return;
      if (g.items_open.length && !confirm(`Drop ${g.items_open.length} open items (${g.items_open.map(i => "#" + i).join(", ")}) and complete the goal?`)) return;
      await act("goal_done", { name, result, drop_open: g.items_open.length > 0 }); toast(`${name} complete`);
    }
    if (what === "share" || what === "unshare") {
      if (what === "share" && g.owner && !confirm(`${g.owner} owns "${name}" now. Make it a goal with no owner? Its items open to every agent.`)) return;
      await act("goal_edit", { name, shared: what === "share" });
      toast(what === "share" ? `${name} has no owner: its items are open to every agent` : `One agent can own ${name} again`);
    }
    if (what === "reopen") await act("goal_reopen", { name });
  } catch (e) { /* toast shown */ }
}
$("#goalFilter").addEventListener("change", () => setGoal($("#goalFilter").value));
$("#addProject").addEventListener("change", renderSelects);
$("#goalGo").addEventListener("click", async () => {
  const name = $("#goalName").value.trim(); if (!name) return toast("Give the goal a name", true);
  const res = await act("goal_add", { project: $("#goalProject").value, name, outcome: $("#goalOutcome").value.trim(), done_when: $("#goalDoneWhen").value.trim() }).catch(() => null);
  if (res) { $("#goalName").value = ""; $("#goalOutcome").value = ""; $("#goalDoneWhen").value = ""; toast(`Added goal ${res.name}`); }
});
$("#area").addEventListener("change", renderNext);
$("#showDone").addEventListener("change", renderProjects);
$("#showIdle").addEventListener("change", renderAgents);
$("#tidyPanes").addEventListener("click", async () => {
  const r = await act("tmux_tidy", {}).catch(() => null);
  if (r) toast(r.closed.length ? `Closed ${r.closed.length} finished: ${r.closed.map(p => p.name).join(", ")}` : "No finished session to close");
});
$("#graphSide").addEventListener("click", (e) => {
  const c = e.target.closest("[data-gproj]"); if (!c) return;
  graphProj = c.dataset.gproj; renderGraph(true);
});
$("#graphDone").addEventListener("change", () => renderGraph(true));
$("#logProject").addEventListener("change", () => renderLog(true));
$("#logSince").addEventListener("change", () => renderLog(true));
$("#claimNext").addEventListener("click", async () => {
  if (!actor()) return toast("Choose your name in 'You are' first", true);
  const area = $("#area").value;
  const res = await act("next_claim", area === "__mine" ? { mine: true } : { project: area || undefined }).catch(() => null);
  if (!(res && res.length)) { if (res) toast("Nothing is ready in that area"); return; }
  const id = res[0].id;
  openDrawer(id);
  const person = (S.agents || []).some(a => a.name === actor() && a.kind === "human");
  if (!person || !$("#claimAgent").checked) return toast(`Claimed #${id}`);
  const it = S.items.find(i => i.id === id);
  const ch = await chooseLaunch(S, { title: "Claim next with an agent", go: "Open agent", what: `#${id} ${it ? it.title : ""}, with you`, item: it });
  if (!ch) return toast(`Claimed #${id}`);
  try {
    const r = await act("open_agent_on", { id, person: actor(), ...launchArgs(ch) });
    toast(`Claimed #${id}; started ${r.agent} in ${r.project} to do it with you`);
  } catch (e) { toast(`Claimed #${id}; no agent opened`, true); }
});
try { if (store("river.claim.agent") === "off") $("#claimAgent").checked = false; } catch (e) { /* no storage */ }
$("#claimAgent").addEventListener("change", (e) => store("river.claim.agent", e.target.checked ? "on" : "off"));
$("#addGo").addEventListener("click", async () => {
  const title = $("#addTitle").value.trim(); if (!title) return toast("Title is empty", true);
  const ids = (el) => $(el).value.split(/[\s,]+/).filter(Boolean).map(Number);
  const res = await act("item_add", { project: $("#addProject").value, title, priority: +$("#addPrio").value, doer: $("#addDoer").value, after: ids("#addAfter"), feeds: ids("#addFeeds"), notes: $("#addNotes").value, goals: $("#addGoal").value ? [$("#addGoal").value] : [] }).catch(() => null);
  if (res) { $("#addTitle").value = ""; $("#addAfter").value = ""; $("#addFeeds").value = ""; $("#addNotes").value = ""; toast(`Added #${res.id}`); }
});
$("#projGo").addEventListener("click", async () => { const n = $("#projName").value.trim(); if (n) { await act("project_add", { name: n }).catch(() => {}); $("#projName").value = ""; } });
$("#regGo").addEventListener("click", async () => {
  const n = $("#regName").value.trim(); if (!n) return;
  await act("register", { name: n, human: $("#regHuman").checked, note: $("#regNote").value }).catch(() => {});
  store("river.actor", n); $("#regName").value = ""; await refresh(); $("#actor").value = n;
});
$("#setGo").addEventListener("click", async () => {
  const scope = $("#setScope").value, nm = $("#setScopeName").value.trim();
  const args = { key: $("#setKey").value, value: $("#setValue").value.trim() };
  if (scope !== "global") { if (!nm) return toast("Give the project, agent, or item", true); args[scope] = scope === "item" ? +nm : nm; }
  await act("config_set", args).then(() => toast("Saved")).catch(() => {});
});

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

const TABS = ["board", "projects", "targets", "capacity", "graph", "done", "settings"];
// The page's URL: #as-<person>, #tab-<name> (the Board has none), and #item-<id> while an item's drawer
// is open, so a tab or an open item can be linked, reloaded, and reached with Back.
function pageHash(tabName = tab, item = drawer.isOpen() ? openItem : null) {
  const keep = location.hash.slice(1).split("&").filter(x => /^as-/.test(x));
  if (tabName !== "board") keep.push("tab-" + tabName);
  if (item != null) keep.push("item-" + item);
  return keep.length ? "#" + keep.join("&") : location.pathname + location.search;
}
function hashItem() { const m = location.hash.match(/(?:^#|&)item-(\d+)/); return m ? +m[1] : null; }
function setTab(name, push = true) {
  if (push && name !== tab) history.pushState(null, "", pageHash(name));
  tab = name; document.querySelectorAll("nav button").forEach(b => b.classList.toggle("on", b.dataset.tab === tab));
  for (const s of TABS) $("#tab-" + s).classList.toggle("hidden", s !== tab);
  renderLog(true);
  return renderGraph(true);
}
// Links into the page: #item-4, #tab-graph, #as-alex, or several joined with & (#as-alex&item-4);
// #terminal-<agent> (a notification for an agent that waits on a prompt) opens that agent's Terminal.
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
  if (e.target === e.currentTarget) return readyDialog.close();
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
  const step = (ok, title, sub, fix) => {
    n++; const here = !ok && next; if (here) next = false;
    return `<div class="step${here ? " next" : ""}"><div class="mark ${ok ? "ok" : "todo"}">${ok ? "✓" : n}</div>
      <div><b>${title}</b>${sub ? `<div class="sub">${sub}</div>` : ""}${!ok && fix ? `<div class="fix">${fix}</div>` : ""}</div></div>`;
  };
  const extra = (ok, title, sub, fix) => `<div class="step"><div class="mark ${ok ? "ok" : "opt"}">${ok ? "✓" : "·"}</div>
    <div><b>${title}</b>${sub ? `<div class="sub">${sub}</div>` : ""}${!ok && fix ? `<div class="fix">${fix}</div>` : ""}</div></div>`;
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
    st.folders.length ? st.folders.map(f => `<div>${esc(f.path)} <span class="muted">(${f.projects.map(esc).join(", ")})</span>`
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
  out.push(step(ready.length > 0, "Choose an agent",
    ready.length ? "The Start button opens " + ready.map(esc).join(" or ")
        + (st.launch_in === "tmux" ? " in a tmux pane (<code>maxpm view</code> in a terminal shows every agent side by side)."
          : st.launch_in === "tab" ? " in a Terminal tab (with tmux installed, in a tmux pane: one terminal shows every agent)." : ".")
        + (addable.length ? ` Also on this Mac: <span class="fix" style="display:inline-flex">${addBtns}</span>` : "")
      : none ? `No AI coding agent is installed on this Mac yet. Install one, then check again:
          <a href="https://docs.claude.com/en/docs/claude-code/setup" target="_blank" rel="noopener">Claude Code</a> or
          <a href="https://github.com/openai/codex" target="_blank" rel="noopener">Codex</a>.`
      : "Found on this Mac: " + addable.map(a => esc(a.label)).join(", ") + ". Add one to the Start button.",
    (none ? `<button class="btn" data-su="recheck">Check again</button>` : "") + addBtns));
  // How river starts each profile agent (Claude Code, Codex): its options, as global settings.
  const profs = st.launch_profiles || [];
  const howStart = profs.length && extra(true, "How agents start",
    profs.map(p => `<div style="margin:4px 0"><b>${esc(p.label)}</b> ${p.options.map(o => {
      const name = o.name.replace(/_/g, " ");
      return o.kind === "toggle"
        ? `<label style="display:inline-flex;gap:4px;align-items:center;margin-right:10px" title="${esc(o.text)}"><input type="checkbox" data-su-opt="${esc(o.setting)}" ${o.value === "on" ? "checked" : ""}>${esc(name)}</label>`
        : o.kind === "choice"
        ? `<label style="display:inline-flex;gap:4px;align-items:center;margin-right:10px" title="${esc(o.text)}">${esc(name)}<select data-su-opt="${esc(o.setting)}"><option value="">the agent's own</option>${o.choices.map(c => `<option ${c === o.value ? "selected" : ""}>${esc(c)}</option>`).join("")}</select></label>`
        : `<label style="display:inline-flex;gap:4px;align-items:center;margin-right:10px" title="${esc(o.text)}">${esc(name)}<input data-su-opt="${esc(o.setting)}" value="${esc(o.value)}" style="width:${o.name === "model_ids" ? 380 : o.name === "prompt" ? 260 : 140}px"></label>`;
    }).join("")}</div>`).join("")
      + `<div class="muted" style="font-size:12px">For every project; set one project apart in Settings (for example claude_remote_control with the project scope). The launch dialog can change the toggles for one start.</div>`, "");
  // A first task, then an agent that takes it.
  const tasks = (S && S.items || []).filter(i => !["deploy", "review", "monitor"].includes(i.kind));
  const folderProjects = (S && S.projects || []).filter(p => p.path);
  out.push(step(tasks.length > 0, "Add a first task",
    tasks.length ? `${tasks.length} task${tasks.length === 1 ? "" : "s"} on the list.`
      : "Something small an agent can do in your project, so you can see how it goes.",
    folderProjects.length ? `<select id="suTaskProject">${folderProjects.map(p => `<option>${esc(p.name)}</option>`).join("")}</select>
      <input id="suTaskTitle" placeholder="for example: add a README that says what this project does" style="flex:1 1 280px">
      <button class="btn primary" data-su="task">Add task</button>` : `<span class="muted">Add a project folder first.</span>`));
  const started = (S && S.agents || []).some(a => a.kind !== "human") || tasks.some(i => ["in_progress", "held", "done"].includes(i.status));
  out.push(step(started, "Start an agent",
    started ? "An agent has picked up work. Watch it on the Board; finished tasks show on Done."
      : "An agent opens in a new Terminal window, takes your task, and gets to work. You'll see it on the Board.",
    tasks.length && ready.length ? agentStart(S.launch_agents, { label: "Start an agent", cls: "btn primary", attrs: 'data-launch="1"',
        title: "Open a new Terminal window in the project folder and start the chosen agent there" })
      : `<span class="muted">Needs an agent and a task first.</span>`));
  // Extras: nice to have, not needed for a first result.
  const more = howStart ? [howStart] : [];
  const missing = Object.entries(st.skills).filter(([, v]) => v !== "installed").map(([k]) => k);
  if (st.claude_home) more.push(extra(missing.length === 0, "Claude Code skills",
    missing.length ? "Teach Claude Code MaximizePM's full workflow (missing: " + missing.join(", ") + ")." : "maxpm and maxpm-planner are installed.",
    `<button class="btn" data-su="skills">Install skills</button>`));
  more.push(extra(st.ntfy_ready && st.notify_channels.includes("ntfy"), "Phone notifications",
    st.ntfy_ready ? "Channels: " + (st.notify_channels.map(esc).join(", ") || "none") : "Get a push on your phone when something needs you (ntfy).",
    `<button class="btn" data-su="ntfy">Turn on ntfy</button><span id="suNtfy" class="sub"></span>`));
  const noTracker = folderProjects.filter(p => !p.tracker);
  if (folderProjects.length) more.push(extra(noTracker.length === 0, "Issue tracker",
    folderProjects.map(p => `<div>${esc(p.name)}: ${p.tracker ? esc(p.tracker) : "none"}</div>`).join("")
      + "If you use GitHub Issues, Jira, or Linear, agents can bring issues in and close them when they're done. Say which tracker, where, and with what tool.",
    noTracker.map(p => `<span style="display:flex;gap:4px;align-items:center">${esc(p.name)}
      <input data-tracker-for="${esc(p.name)}" placeholder="github owner/repo via gh" style="width:220px">
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
      const title = $("#suTaskTitle").value.trim(); if (!title) return toast("Write the task first", true);
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
  const x = e.target.closest("[data-su-opt]"); if (!x) return;
  const value = x.type === "checkbox" ? (x.checked ? "on" : "off") : x.value.trim();
  try { await act("config_set", { key: x.dataset.suOpt, value }); toast(`${x.dataset.suOpt} = ${value || "(the agent's own)"}`); }
  catch (err) { /* act() showed the error */ }
  await openSetup();
});
$("#agents").addEventListener("change", (e) => { const s = e.target.closest("[data-give-item]"); if (s) giveChoice[s.dataset.giveItem] = s.value; });
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

async function openFromHash() {
  let tabIn = null, itemIn = false;
  for (const part of location.hash.slice(1).split("&")) {
    const m = part.match(/^(item|tab|as|terminal)-(.+)$/); if (!m) continue;
    const v = decodeURIComponent(m[2]);
    if (m[1] === "terminal") openTerminal(v);
    if (m[1] === "as" && [...$("#actor").options].some(o => o.value === v)) { $("#actor").value = v; store("river.actor", v); inboxGen++; await pollInbox().catch(() => {}); }
    if (m[1] === "tab" && TABS.includes(v)) tabIn = v;
    if (m[1] === "item") { itemIn = true; await openDrawer(+v); }
  }
  // No item in the URL (Back after opening one): the drawer closes.
  if (!itemIn && drawer.isOpen()) { drawer.close(); openItem = null; }
  // An item link alone keeps the tab; a link with no tab and no item is the Board.
  if (!tabIn && !itemIn) tabIn = "board";
  if (tabIn && tabIn !== tab) await setTab(tabIn, false);
}
window.addEventListener("hashchange", openFromHash);
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
    b.textContent = r.state === "connected" ? "Relay · on" : "Relay · off";
    b.title = (r.state === "connected" ? `Connected${who} to ${r.url}` : `Not connected${who}: ${r.error || r.state}`)
      + `\nFrom anywhere: ${r.url}/app/\nClick to disconnect this computer.`;
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
checkUpdate().catch(() => {}); setInterval(() => { if (!document.hidden) checkUpdate().catch(() => {}); }, 30 * 60 * 1000);
// A stale server is cheap to spot (no git fetch), so look for it more often than for new commits.
setInterval(() => {
  if (document.hidden || updateMode !== "update") return;  // hidden, or already asking for a restart
  fetch("api/update?fetch=0").then(r => r.ok ? r.json() : null).then(u => { if (u && u.stale) checkUpdate(); }).catch(() => {});
}, 60 * 1000);
refresh().then(openFromHash).then(maybeSetup); setInterval(() => { if (!document.hidden) refresh().catch(() => {}); }, 3000);
// A hidden tab stops the full refresh but keeps asking what needs a person, so notifications still arrive.
setInterval(() => { if (document.hidden) pollNeedsYou().catch(() => {}); }, 15000);

