// The Items view of Plan (#4656; Plan since #5102 step 4): a card for each project of the scope with its goals and
// items, and the blocked items. The forms that add an item, a goal or a project are a dialog (plan.js openAdd).
import { $, esc, store, actor, toast, act, ago, left, clip, shortTitle } from "../lib.js";
import { doerChip, projectChip, countChip } from "../components/chip.js";
import { nyCard, goalCard, projCard } from "../components/card.js";
import { itemTableRows, itemTableColumns } from "../components/itemRow.js";
import { makeTable } from "../components/table.js";
import { button } from "../components/button.js";
import { S, goalSel, inGoal, scope } from "../page.js";
import { openAdd } from "./plan.js";
import { nav } from "../page.js";

// A project's notes show their first sentence (#5082: only what a person acts on). The rest was a hover only, which
// a phone cannot show (#5401): when the notes are longer, "more" shows them in full in place, and "less" folds them.
const notesOpen = new Set();
function noteLine(p) {
  const first = clip(p.notes.split(/(?<=\.)\s|\n/)[0], 90), longer = first !== p.notes.trim(), open = longer && notesOpen.has(p.name);
  const text = open ? `<span class="muted pnotes">${esc(p.notes.trim())}</span>` : `<span class="muted" title="${esc(p.notes)}">${esc(first)}</span>`;
  return text + (longer ? ` <span class="link" data-pnotes="${esc(p.name)}">${open ? "less" : "more"}</span>` : "")
    + ` <span class="link" data-describe="${esc(p.name)}">edit</span>`;
}

// Projects folded closed on the Projects tab, remembered in this browser.
function projClosed() { try { return JSON.parse(store("river.projClosed") || "[]"); } catch (e) { return []; } }
export function renderProjects() {
  const showDone = $("#showDone").checked, showBacklog = $("#showBacklog").checked;
  const G = goalSel(), goals = S.goals || [];
  const html = S.projects.filter(p => !scope.project || p.name === scope.project).map(p => {
    const pg = goals.filter(g => g.project === p.name && (g.status === "open" || showDone || g.name === G));
    const all = S.items.filter(i => i.project === p.name && inGoal(i));
    if (G && !all.length && !pg.some(g => g.name === G)) return "";
    const open = all.filter(i => !["done", "dropped"].includes(i.status) && !i.backlog);
    const closed = all.filter(i => ["done", "dropped"].includes(i.status));
    const backlog = all.filter(i => i.backlog);
    // The state has done items only while "done" is on (#4709): else closed_counts counts them, for all goals.
    const closedN = showDone ? closed.length : !G ? (S.closed_counts || {})[p.name] || 0 : null;
    const ready = open.filter(i => i.ready).length, prog = open.filter(i => ["in_progress", "held"].includes(i.status)).length;
    const closedSet = projClosed(), isClosed = closedSet.includes(p.name);
    const who = [...new Set(open.filter(i => i.assignee && ["in_progress", "held"].includes(i.status)).map(i => i.assignee))];
    const home = p.path ? p.path.replace(/^\/(Users|home)\/[^/]+/, "~") : "";
    // The folder, the target and the tracker are a hover on the counts (#5082): nobody acts on them here.
    const meta = [home ? "folder " + home : "no folder", p.target ? "deploys to " + p.target : "", p.tracker ? "tracker " + p.tracker : ""].filter(Boolean);
    const me = actor();
    // One + goal in one place: the goal box, under the goal cards, also when the project has none yet (#5423).
    return projCard(p, isClosed, `<div class="pcounts" title="${esc(meta.join(" · ") + (closedN ? ` · ${closedN} done` : "") + (backlog.length ? ` · ${backlog.length} in backlog` : ""))}">${countChip(ready, "ready", "c-ready")}${countChip(prog, "in progress", "c-in_progress")}${who.length ? `<span class="muted t-s">${who.map(esc).join(", ")}</span>` : ""}</div>
      <div class="pbody">
        <div class="pdesc t-s">${p.notes ? noteLine(p) : `<span class="link" data-describe="${esc(p.name)}">+ description</span>`}</div>
        <div class="goals">${pg.map(g => goalCard(g, { selected: G === g.name, me })).join("")}<div class="gc-add">${button("+ goal", { cls: "plain", data: { addgoal: p.name } })}</div></div>
        <div class="psec">${open.length} open${showDone && closed.length ? `, ${closed.length} done` : ""}${showBacklog && backlog.length ? `, ${backlog.length} in backlog` : ""}</div>
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
  $("#blockedList").innerHTML = B.map(i => nyCard(`<b class="link" data-open="${i.id}" title="${esc(i.title)}">#${i.id} ${esc(shortTitle(i))}</b> ${projectChip(i.project)}${i.doer === "human" ? doerChip(i) : ""}`, `<div class="ny-c t-s" title="${esc(i.blocked_reason)}">${esc(clip(i.blocked_reason, 90))}</div>
      <div class="muted t-s"${i.blocked_set_by ? ` title="set by ${esc(i.blocked_set_by)}"` : ""}>${i.blocked_at ? ago(i.blocked_at) : ""}${i.blocked_until ? `${i.blocked_at ? " · " : ""}<span title="${esc(i.blocked_until_text)}; then ready by itself">${left(i.blocked_until)} left</span>` : ""}</div>`)).join("");
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
  if (t.dataset.addgoal !== undefined) return run(async () => {
    openAdd("goal", { project: t.dataset.addgoal, parent: t.dataset.parent || "" });
  });
  if (t.dataset.goalAct) return run(() => goalAction(t.dataset.goalAct, t.dataset.g));
  if (t.dataset.pnotes) return run(() => {
    const n = t.dataset.pnotes; if (!notesOpen.delete(n)) notesOpen.add(n); return renderProjects();
  });
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
  const res = await act("goal_add", { project: $("#goalProject").value, name, outcome: $("#goalOutcome").value.trim(), done_when: $("#goalDoneWhen").value.trim(),
    parent: $("#goalParent").value || undefined }).catch(() => null);
  if (res) { $("#goalName").value = ""; $("#goalOutcome").value = ""; $("#goalDoneWhen").value = ""; $("#goalParent").value = ""; toast(`Added goal ${res.name}`); }
});
$("#showDone").addEventListener("change", renderProjects);
$("#showBacklog").addEventListener("change", renderProjects);
$("#addGo").addEventListener("click", async () => {
  const title = $("#addTitle").value.trim(); if (!title) return toast("Title is empty", true);
  const ids = (el) => $(el).value.split(/[\s,]+/).filter(Boolean).map(Number);
  const res = await act("item_add", { project: $("#addProject").value, title, priority: +$("#addPrio").value, doer: $("#addDoer").value, tier: $("#addTier").value || undefined, after: ids("#addAfter"), feeds: ids("#addFeeds"), notes: $("#addNotes").value, goals: $("#addGoal").value ? [$("#addGoal").value] : [] }).catch(() => null);
  if (res) { $("#addTitle").value = ""; $("#addAfter").value = ""; $("#addFeeds").value = ""; $("#addNotes").value = ""; toast(`Added #${res.id}`); }
});
$("#projGo").addEventListener("click", async () => { const n = $("#projName").value.trim(); if (n) { await act("project_add", { name: n }).catch(() => {}); $("#projName").value = ""; } });
