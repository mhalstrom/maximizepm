// The Projects tab (#4656): a card for each project with its goals and items, blocked items, and the forms
// that add an item, a goal or a project.
import { $, esc, store, actor, toast, act, ago, left } from "../lib.js";
import { doerChip, projectChip, countChip } from "../components/chip.js";
import { nyCard, goalCard, projCard } from "../components/card.js";
import { itemTableRows, itemTableColumns } from "../components/itemRow.js";
import { makeTable } from "../components/table.js";
import { S, tab, goalSel, inGoal, nav } from "../page.js";

// Projects folded closed on the Projects tab, remembered in this browser.
function projClosed() { try { return JSON.parse(store("river.projClosed") || "[]"); } catch (e) { return []; } }
export function renderProjects() {
  const showDone = $("#showDone").checked, showBacklog = $("#showBacklog").checked;
  const G = goalSel(), goals = S.goals || [];
  const html = S.projects.map(p => {
    const pg = goals.filter(g => g.project === p.name && (g.status === "open" || showDone || g.name === G));
    const all = S.items.filter(i => i.project === p.name && inGoal(i));
    if (G && G !== "__none" && !all.length && !pg.some(g => g.name === G)) return "";
    const open = all.filter(i => !["done", "dropped"].includes(i.status) && !i.backlog);
    const closed = all.filter(i => ["done", "dropped"].includes(i.status));
    const backlog = all.filter(i => i.backlog);
    const ready = open.filter(i => i.ready).length, prog = open.filter(i => ["in_progress", "held"].includes(i.status)).length;
    const closedSet = projClosed(), isClosed = closedSet.includes(p.name);
    const who = [...new Set(open.filter(i => i.assignee && ["in_progress", "held"].includes(i.status)).map(i => i.assignee))];
    const home = p.path ? p.path.replace(/^\/(Users|home)\/[^/]+/, "~") : "";
    const meta = [home ? `folder <code>${esc(home)}</code>` : "no folder",
      p.target ? `deploys to <b>${esc(p.target)}</b>` : "", p.tracker ? `tracker ${esc(p.tracker)}` : ""].filter(Boolean);
    const me = actor();
    return projCard(p, isClosed, `<div class="pmeta">${meta.map(m => `<span>${m}</span>`).join("")}</div>
      <div class="pcounts">${countChip(ready, "ready", "c-ready")}${countChip(prog, "in progress", "c-in_progress")}${countChip(open.length, "open", "c-p")}${countChip(closed.length, "done", "c-p")}${backlog.length ? countChip(backlog.length, "in backlog", "c-p") : ""}${pg.length ? countChip(pg.length, pg.length === 1 ? "goal" : "goals", "c-p") : ""}</div>
      <div class="pbody">
        <div class="pdesc">${p.notes ? esc(p.notes) : '<span class="muted">No description.</span>'} <span class="link" data-describe="${esc(p.name)}">edit</span></div>
        <div class="pwho">${who.length ? "Working here now: " + who.map(n => `<b>${esc(n)}</b>`).join(", ") : "Nobody works here now."}</div>
        <div class="psec">Goals</div>
        <div class="goals">${pg.map(g => goalCard(g, { selected: G === g.name, me })).join("")}<span class="link" style="font-size:12px;align-self:center" data-addgoal="${esc(p.name)}">${pg.length ? "+ goal" : "No goals yet. + Add a goal"}</span></div>
        <div class="psec">Open items (${open.length})${showDone && closed.length ? `, ${closed.length} done` : ""}${showBacklog && backlog.length ? `, ${backlog.length} in backlog` : ""}</div>
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
        placeholder: "No open items.", onRow: (r) => nav.openDrawer(r.id) }) });
    });
  }
  for (const [name, t] of projTables) {
    if (!box.contains(t.el)) { t.table.tabulator.destroy(); projTables.delete(name); continue; }
    t.table.set(itemTableRows(S.items.filter(i => i.project === name && inGoal(i) && (showDone || !["done", "dropped"].includes(i.status))
      && (showBacklog || !i.backlog))));
  }
}
const projTables = new Map();

export function renderBlocked() {
  const B = S.items.filter(i => i.blocked_reason && !["done", "dropped"].includes(i.status));
  $("#blockedPanel").classList.toggle("hidden", !B.length);
  $("#blockedList").innerHTML = B.map(i => nyCard(`<b class="link" data-open="${i.id}">#${i.id} ${esc(i.title)}</b> ${projectChip(i.project)}${doerChip(i)}`, `<div class="ny-c">${esc(i.blocked_reason)}</div>
      <div class="muted" style="font-size:12px">${i.blocked_at ? "since " + ago(i.blocked_at) : "since: not recorded"} · ${i.blocked_until ? `until ${esc(i.blocked_until_text)} (${left(i.blocked_until)} left), then ready by itself` : "until someone clears it"}${i.blocked_set_by ? " · set by " + esc(i.blocked_set_by) : ""}</div>`)).join("");
}

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

// A click on this view's controls; true when it was one (the frame then stops).
export function onClick(t) {
  if (t.dataset.addgoal) return run(async () => {
    await nav.setTab("projects"); const d = $("#goalName").closest("details"); d.open = true; $("#goalProject").value = t.dataset.addgoal; d.scrollIntoView({ block: "center" }); $("#goalName").focus();
  });
  if (t.dataset.goalAct) return run(() => goalAction(t.dataset.goalAct, t.dataset.g));
  if (t.dataset.describe) return run(async () => {
    const p = S.projects.find(x => x.name === t.dataset.describe); const text = prompt(`What does "${p.name}" cover, and what context helps an agent work on it?`, p.notes || ""); if (text !== null) await act("project_describe", { name: p.name, text }).catch(() => {});
  });
  if (t.dataset.rank) return run(async () => {
    const to = +t.dataset.to; if (to >= 1) await act("project_rank", { name: t.dataset.rank, rank: to }).catch(() => {});
  });
  if (t.dataset.fold) return run(async () => {
    const c = projClosed(), n = t.dataset.fold; store("river.projClosed", JSON.stringify(c.includes(n) ? c.filter(x => x !== n) : c.concat(n))); return renderProjects();
  });
  return false;
}
const run = (f) => { f(); return true; };

$("#goalGo").addEventListener("click", async () => {
  const name = $("#goalName").value.trim(); if (!name) return toast("Give the goal a name", true);
  const res = await act("goal_add", { project: $("#goalProject").value, name, outcome: $("#goalOutcome").value.trim(), done_when: $("#goalDoneWhen").value.trim() }).catch(() => null);
  if (res) { $("#goalName").value = ""; $("#goalOutcome").value = ""; $("#goalDoneWhen").value = ""; toast(`Added goal ${res.name}`); }
});
$("#showDone").addEventListener("change", renderProjects);
$("#showBacklog").addEventListener("change", renderProjects);
$("#addGo").addEventListener("click", async () => {
  const title = $("#addTitle").value.trim(); if (!title) return toast("Title is empty", true);
  const ids = (el) => $(el).value.split(/[\s,]+/).filter(Boolean).map(Number);
  const res = await act("item_add", { project: $("#addProject").value, title, priority: +$("#addPrio").value, doer: $("#addDoer").value, after: ids("#addAfter"), feeds: ids("#addFeeds"), notes: $("#addNotes").value, goals: $("#addGoal").value ? [$("#addGoal").value] : [] }).catch(() => null);
  if (res) { $("#addTitle").value = ""; $("#addAfter").value = ""; $("#addFeeds").value = ""; $("#addNotes").value = ""; toast(`Added #${res.id}`); }
});
$("#projGo").addEventListener("click", async () => { const n = $("#projName").value.trim(); if (n) { await act("project_add", { name: n }).catch(() => {}); $("#projName").value = ""; } });
