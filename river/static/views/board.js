// The Board tab (#4656): the status strip, Next up, Work, the manager, agents and people, Recent, and taken over
// items. Its Needs you and Inbox panels have files of their own (board-needs-you.js, board-inbox.js).
import { $, esc, store, actor, toast, act, ago, left, clip, hooks } from "../lib.js";
import { chip, personChip, projectChip } from "../components/chip.js";
import { nyCard } from "../components/card.js";
import { itemRow, itemOrder } from "../components/itemRow.js";
import { makeTable } from "../components/table.js";
import { agentStart } from "../components/agentStart.js";
import { chooseLaunch, launchArgs } from "../components/launchDialog.js";
import { managerHtml, queueHtml, agentButtons } from "../components/agentActions.js";
import { S, tab, nav, inGoal } from "../page.js";
import { NY } from "./board-needs-you.js";

export function renderNext() {
  const area = $("#area").value;
  if (area === "__mine") { $("#next").innerHTML = `<div class="muted">Use <b>Claim next</b>: the server picks the ready item closest to what "${esc(actor() || "(choose your name in You are)")}" claimed or finished before.</div>`; return; }
  const ready = S.items.filter(i => i.ready && (!area || i.project === area) && inGoal(i));
  $("#next").innerHTML = ready.slice(0, 5).map(i => itemRow(i, i.reason)).join("") || `<div class="muted">Nothing is ready${area ? " in " + esc(area) : ""}.</div>`;
}

// An agent session not seen for away_after, holding and owning nothing, has most likely stopped: hide it.
// People always show, and so does any agent that still holds an item, owns a goal or target, or has a push waiting.
function agentShown(a) {
  if (a.kind === "human" || a.state === "active" || a.holds.length || (a.owns || []).length) return true;
  // Its tmux pane is open (S.terminals): a session that never ran a maxpm command may wait there on a prompt,
  // and its Terminal button is the only way to answer it (#2713).
  if ((S.terminals || {})[a.name]) return true;
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
$("#agents").addEventListener("change", (e) => { const s = e.target.closest("[data-give-item]"); if (s) giveChoice[s.dataset.giveItem] = s.value; });
// Agents and people as a table. Name and Holds always show; on a narrow panel the other columns
// fold into a detail row (the ▸ at the start of the row). A row is a drop target for an item row.
let agentsTable = null;
export function renderAgents() {
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
export function renderManager() {
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

let takeoversOpen = false;
export function renderTakeovers() {
  const T = S.takeovers || [];
  $("#takeoverPanel").classList.toggle("hidden", !T.length);
  const words = { "took over": "took over", "done": "marked done", "dropped": "dropped" };
  const head = `<div class="nyl" data-takeovers-toggle="1"><span class="t">${takeoversOpen ? "▾" : "▸"} ${T.length} item${T.length === 1 ? "" : "s"} an agent took off your list: review or undo</span><span class="chips"><button class="btn" data-takeovers-okall="1" title="Accept all of them">OK all</button></span></div>`;
  if (!takeoversOpen) { $("#takeovers").innerHTML = head; return; }
  $("#takeovers").innerHTML = head + T.map(t => nyCard(`<b>#${t.id} ${esc(t.title)}</b> ${projectChip(t.project)}<span class="muted" style="font-size:12px">${ago(t.takeover_at)}</span>`, `<div class="ny-c"><b>${esc(t.takeover_by)}</b> ${words[t.takeover_kind] || t.takeover_kind} it: ${esc(t.takeover_note || "")}</div>
      <div class="actions"><button class="btn" data-open="${t.id}">Open</button><button class="btn" data-undo-takeover="${t.id}">Undo: give it back to me</button><button class="btn primary" data-takeover-ok="${t.id}">OK</button></div>`)).join("");
}

// The status strip: counts you click to go where they are.
export function renderStrip() {
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
export function renderWork() {
  const open = workOpen();
  $("#work").innerHTML = S.projects.map(p => {
    const items = S.items.filter(i => i.project === p.name && !["done", "dropped"].includes(i.status) && !i.backlog);
    if (!items.length) return "";
    const ready = items.filter(i => i.ready).length, run = items.filter(i => ["in_progress", "held"].includes(i.status)).length;
    const hum = items.filter(i => i.ready && i.doer === "human").length;
    const isOpen = open.has(p.name), shown = items.sort(itemOrder).slice(0, 8);
    return `<div class="wp"><div class="wp-h" data-wp="${esc(p.name)}"><span class="tw">${isOpen ? "▾" : "▸"}</span><b>${esc(p.name)}</b>
        <span class="counts">${items.length} open · ${ready} ready${run ? ` · ${run} running` : ""}${hum ? ` · ${hum} for you` : ""}</span></div>
      ${isOpen ? `<div class="wp-items">${shown.map(i => itemRow(i)).join("")}${items.length > shown.length ? `<div class="muted" style="font-size:12px;padding:4px 6px">${items.length - shown.length} more: <span class="link" data-tab="projects">Projects tab</span></div>` : ""}</div>` : ""}</div>`;
  }).join("") || `<div class="muted">No open items.</div>`;
}

export function renderEvents() {
  $("#events").innerHTML = S.events.slice(0, 15).map(e => `<div class="ev">${ago(e.at)} · <b>${esc(e.actor)}</b> ${esc(e.change)}${e.item_id ? ` <span class="link" data-open="${e.item_id}">#${e.item_id}</span>` : ""}</div>`).join("");
}

// A click on this view's controls; true when it was one (the frame then stops).
export function onClick(t) {
  if (t.dataset.takeoversOkall) return run(async () => { for (const x of S.takeovers || []) await act("takeover_seen", { id: x.id }).catch(() => {}); });
  if (t.closest("[data-takeovers-toggle]") && !t.closest("button")) return run(async () => { takeoversOpen = !takeoversOpen; return renderTakeovers(); });
  const wp = t.closest("[data-wp]"); if (wp) return run(async () => { const o = workOpen(), n = wp.dataset.wp; o.has(n) ? o.delete(n) : o.add(n); store("river.work.open", JSON.stringify([...o])); return renderWork(); });
  const gw = t.closest("[data-give]"); if (gw) return run(async () => {
    const to = gw.dataset.give, sel = document.querySelector(`[data-give-item="${CSS.escape(to)}"]`);
    if (sel && sel.value) await act("push", { id: +sel.value, to, note: "from the page: you were waiting for work" })
      .then(() => toast(`Gave #${sel.value} to ${to}`)).catch(() => {});
  });
  const sb = t.closest("[data-strip]"); if (sb) return run(async () => { const go = sb.dataset.strip;
    if (go === "blocked") { await nav.setTab("projects"); return $("#blockedPanel").scrollIntoView({ block: "start" }); }
    if (go === "capacity") return nav.setTab("capacity");
    if (go === "ready") return nav.openReady();
    await nav.setTab("board");
    if (go === "takeovers") { takeoversOpen = true; renderTakeovers(); }
    return $(go === "work" ? "#colWork" : "#colNeeds").scrollIntoView({ block: "start" });
  });
  if (t.dataset.undoTakeover) return run(async () => { await act("undo_takeover", { id: +t.dataset.undoTakeover }).then(() => toast("Back on your list")).catch(() => {}); });
  if (t.dataset.takeoverOk) return run(async () => { await act("takeover_seen", { id: +t.dataset.takeoverOk }).catch(() => {}); });
  if (t.dataset.unpush) return run(async () => { await act("push_cancel", { id: +t.dataset.unpush }).then(() => toast("Push cancelled")).catch(() => {}); });
  return false;
}
const run = (f) => { f(); return true; };

$("#area").addEventListener("change", renderNext);
$("#showIdle").addEventListener("change", renderAgents);
$("#tidyPanes").addEventListener("click", async () => {
  const r = await act("tmux_tidy", {}).catch(() => null);
  if (r) toast(r.closed.length ? `Closed ${r.closed.length} finished: ${r.closed.map(p => p.name).join(", ")}` : "No finished session to close");
});
$("#claimNext").addEventListener("click", async () => {
  if (!actor()) return toast("Choose your name in 'You are' first", true);
  const area = $("#area").value;
  const res = await act("next_claim", area === "__mine" ? { mine: true } : { project: area || undefined }).catch(() => null);
  if (!(res && res.length)) { if (res) toast("Nothing is ready in that area"); return; }
  const id = res[0].id;
  nav.openDrawer(id);
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
$("#regGo").addEventListener("click", async () => {
  const n = $("#regName").value.trim(); if (!n) return;
  await act("register", { name: n, human: $("#regHuman").checked, note: $("#regNote").value }).catch(() => {});
  store("river.actor", n); $("#regName").value = ""; await hooks.refresh(); $("#actor").value = n;
});
