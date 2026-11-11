// The Overview view of Plan (#5102 step 5, design #4657): Next up with Claim next (the Board's), the open work of
// each project or, with one project in the scope, of each of its goals (the Board's Work), Capacity with Start an
// agent (the Capacity tab's numbers and advice), and the items blocked outside the queue (from Projects). Every part
// shows the scope of Plan's left bar. The step bars of Capacity are above the graph (renderSteps).
import { $, esc, store, actor, toast, act, ago, left, clip, shortTitle } from "../lib.js";
import { doerChip, projectChip } from "../components/chip.js";
import { nyCard } from "../components/card.js";
import { itemRow, itemOrder } from "../components/itemRow.js";
import { agentStart } from "../components/agentStart.js";
import { capacityBars } from "../components/bar.js";
import { chooseLaunch, launchArgs } from "../components/launchDialog.js";
import { S, nav, scope, inScope } from "../page.js";

const put = (el, h) => { if (el.dataset.sig !== h) { el.innerHTML = h; el.dataset.sig = h; } };
const open = (i) => !["done", "dropped"].includes(i.status) && !i.backlog;

export function renderNext() {
  const ready = S.items.filter(i => i.ready && inScope(i));
  put($("#next"), ready.slice(0, 5).map(i => itemRow(i, i.reason, { hover: true })).join("")
    || `<div class="muted">Nothing is ready${scope.goal ? " in goal " + esc(scope.goal) : scope.project ? " in " + esc(scope.project) : ""}.</div>`);
}

// Work: one row per project (all projects), or per goal of the chosen project, with a row for the items of no goal.
// A click opens its open items, ready first.
function workOpen() { try { return new Set(JSON.parse(store("river.work.open") || "[]")); } catch (e) { return new Set(); } }
export function renderWork() {
  const opened = workOpen();
  let groups;
  if (!scope.project) {
    $("#workTitle").textContent = "Work by project";
    groups = S.projects.map(p => ({ key: "p:" + p.name, label: p.name, items: S.items.filter(i => i.project === p.name && open(i)) }));
  } else {
    $("#workTitle").textContent = `Work in ${scope.project} by goal`;
    const mine = S.items.filter(i => i.project === scope.project && open(i));
    const goals = (S.goals || []).filter(g => g.project === scope.project && g.status === "open").sort((a, b) => a.rank - b.rank)
      .filter(g => !scope.goal || g.name === scope.goal);
    groups = goals.map(g => ({ key: "g:" + g.name, label: g.name, items: mine.filter(i => (i.goals || []).includes(g.name)) }));
    if (!scope.goal) groups.push({ key: "n:" + scope.project, label: "no goal", items: mine.filter(i => !(i.goals || []).length) });
  }
  // Each count is one phrase that never breaks; a wrapped line breaks only between them (#5375).
  const counts = parts => parts.filter(Boolean).map(p => `<span class="cnt">${esc(p)}</span>`).join(" · ");
  put($("#work"), groups.filter(g => g.items.length).map(g => {
    const items = g.items, ready = items.filter(i => i.ready).length, run = items.filter(i => ["in_progress", "held"].includes(i.status)).length;
    const hum = items.filter(i => i.ready && i.doer === "human").length, blocked = items.filter(i => i.blocked_reason).length;
    const isOpen = opened.has(g.key), shown = items.slice().sort(itemOrder).slice(0, 8);
    return `<div class="wp"><div class="wp-h" data-wp="${esc(g.key)}"><span class="tw">${isOpen ? "▾" : "▸"}</span><b>${esc(g.label)}</b>
        <span class="counts">${counts([`${items.length} open`, `${ready} ready`, run && `${run} running`, hum && `${hum} for a person`, blocked && `${blocked} blocked outside`])}</span></div>
      ${isOpen ? `<div class="wp-items">${shown.map(i => itemRow(i)).join("")}${items.length > shown.length ? `<div class="muted wp-more">${items.length - shown.length} more: <span class="link" data-plan-view="items">Items</span></div>` : ""}</div>` : ""}</div>`;
  }).join("") || `<div class="muted">No open items.</div>`);
}

// Capacity: the open slots, the sessions with nothing ready, the sessions, items in progress, ready items for people;
// the advice; Start an agent when an agent can take something.
export function renderCapacity() {
  const c = S.capacity; if (!c) return;
  const ready = S.items.filter(i => i.ready && i.doer !== "human" && !["deploy", "review", "monitor"].includes(i.kind)).length;
  put($("#capStart"), ready ? agentStart(S.launch_agents, { label: "Start an agent", cls: "primary", attrs: 'data-launch="1"',
    title: "Open a new session in the folder of the most important ready item and start the chosen agent there" }) : "");
  const idle = (S.agents || []).filter(a => a.kind !== "human" && a.role === "waiting");
  put($("#capacity"), `<div class="stats">
      <div class="stat ${c.spare_slots ? "good" : ""}" title="More agent sessions you can start now (max_sessions less the sessions that run)"><div class="n">${c.spare_slots}</div><div class="l">open slots</div></div>
      <div class="stat ${c.excess_sessions ? "bad" : ""}" title="Agent sessions with nothing ready for them${idle.length ? ": " + idle.map(a => a.name).join(", ") : ""}"><div class="n">${c.excess_sessions}</div><div class="l">idle</div></div>
      <div class="stat" title="Active agent sessions; ${c.agents_busy} of them busy"><div class="n">${c.agents_active}</div><div class="l">sessions</div></div>
      <div class="stat"><div class="n">${c.in_progress.length}</div><div class="l">in progress</div></div>
      <div class="stat hum" title="Ready items that wait on a person"><div class="n">${c.ready_for_humans.length}</div><div class="l">for people</div></div>
    </div>
    ${idle.length ? `<div class="muted t-s ov-idle">Waiting for work: ${idle.map(a => `<span class="link" data-session-go="${esc(a.name)}">${esc((S.session_names || {})[a.name] || a.name)}</span>`).join(", ")}</div>` : ""}
    <ul class="advice">${c.advice.map(a => `<li class="${a.kind}">${esc(a.text)}</li>`).join("")}</ul>`);
}

// The step bars above the graph: the open items by step, agents and people.
export function renderSteps() {
  const c = S.capacity; if (!c) return;
  const layers = capacityBars(c.layers);
  put($("#graphSteps"), `<div class="layers">${layers || '<span class="muted">No open items.</span>'}</div>
    <div class="legend" title="Open items by step: 1 can start now, 2 after step 1, … Peak agent width ${c.peak_width}."><span><span class="sw sw-a"></span>agents</span><span><span class="sw sw-h"></span>people</span></div>`);
}

export function renderBlocked() {
  const B = S.items.filter(i => i.blocked_reason && !["done", "dropped"].includes(i.status) && inScope(i));
  $("#blockedPanel").classList.toggle("hidden", !B.length);
  put($("#blockedList"), B.map(i => nyCard(`<b class="link" data-open="${i.id}" title="${esc(i.title)}">#${i.id} ${esc(shortTitle(i))}</b> ${projectChip(i.project)}${i.doer === "human" ? doerChip(i) : ""}`, `<div class="ny-c t-s" title="${esc(i.blocked_reason)}">${esc(clip(i.blocked_reason, 90))}</div>
      <div class="muted t-s"${i.blocked_set_by ? ` title="set by ${esc(i.blocked_set_by)}"` : ""}>${i.blocked_at ? ago(i.blocked_at) : ""}${i.blocked_until ? `${i.blocked_at ? " · " : ""}<span title="${esc(i.blocked_until_text)}; then ready by itself">${left(i.blocked_until)} left</span>` : ""}</div>`)).join(""));
}

export function renderOverview() { renderNext(); renderWork(); renderCapacity(); renderBlocked(); renderSteps(); }

// A click on this view's controls; true when it was one (the frame then stops).
export function onClick(t) {
  const wp = t.closest("[data-wp]"); if (wp) { const o = workOpen(), n = wp.dataset.wp; o.has(n) ? o.delete(n) : o.add(n); store("river.work.open", JSON.stringify([...o])); renderWork(); return true; }
  return false;
}

$("#claimNext").addEventListener("click", async () => {
  if (!actor()) return toast("Choose your name in 'You are' first", true);
  const res = await act("next_claim", { project: scope.project || undefined }).catch(() => null);
  if (!(res && res.length)) { if (res) toast("Nothing is ready in that project"); return; }
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
