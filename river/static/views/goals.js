// The Goals view of Plan (#5102 step 6, design #4657, mark's answer 6A): one card for each goal of the scope, larger
// than the goal chips of Items. The outcome, the done-when test, the owner (a link to the owner's session) with the time
// left on its claim, shared or not, the progress, the sessions that hold an item of the goal now, the latest handoff
// (folded closed; it loads when opened, with its earlier versions), its sub-goals and + sub-goal, and the actions
// (↑ ↓, own, release, edit, share, complete, reopen: projects.js goalAction). + goal at the end.
import { $, esc, actor, ago, left } from "../lib.js";
import { projectChip } from "../components/chip.js";
import { bar, pct } from "../components/bar.js";
import { button } from "../components/button.js";
import { S, scope } from "../page.js";

const sessionLink = (name) => `<span class="link" data-session-go="${esc(name)}" title="Open the session of ${esc(name)}: ${esc((S.session_names || {})[name] || "")}">${esc(name)}</span>`;
const hasSession = (name) => (S.sessions || []).includes(name);
const who = (name) => hasSession(name) ? sessionLink(name) : esc(name);

function acts(g) {
  const me = actor(), open = g.status === "open", a = (what, text, title) => `<span class="link" data-goal-act="${what}" data-g="${esc(g.name)}"${title ? ` title="${esc(title)}"` : ""}>${text}</span>`;
  return [a("up", "↑", "more important"), a("down", "↓", "less important"),
    open && me && g.owner !== me && !g.shared ? a("own", "own") : "", open && me && g.owner === me ? a("release", "release") : "",
    a("edit", "edit"),
    open ? (g.shared ? a("unshare", "unshare", "One agent can own this goal again") : a("share", "share", "No owner: maxpm go gives this goal to no agent, and its items stay open to every agent")) : "",
    open ? a("done", "complete") : a("reopen", "reopen")].filter(Boolean).join(" ");
}

function card(g, sub) {
  const total = g.items_open.length + g.items_done.length;
  const holders = [...new Set(S.items.filter(i => (i.goals || []).includes(g.name) && ["in_progress", "held"].includes(i.status) && i.assignee).map(i => i.assignee))];
  const owner = g.shared ? "shared: no owner" : g.owner ? `owner ${who(g.owner)}${g.owner_expires_at ? ` <span class="muted">· ${left(g.owner_expires_at)} left</span>` : ""}` : "no owner";
  const h = g.handoff;
  const subs = (S.goals || []).filter(x => x.parent === g.name);
  return `<div class="goalcard${sub ? " sub" : ""}${g.status === "open" ? "" : " complete"}" data-goalcard="${esc(g.name)}">
    <div class="gc-h"><b>${esc(g.name)}</b>${sub ? "" : projectChip(g.project)}${g.status === "open" ? "" : `<span class="muted t-s">complete</span>`}<span class="spacer"></span><span class="t-s">${acts(g)}</span></div>
    <div class="gc-o">${g.outcome ? esc(g.outcome) : `<span class="muted">No outcome written.</span>`}</div>
    <div class="gc-dw"><span class="muted">Done when:</span> ${g.done_when ? esc(g.done_when) : `<span class="muted">not written</span>`}</div>
    ${g.result ? `<div class="gc-dw"><span class="muted">Result:</span> ${esc(g.result)}</div>` : ""}
    <div class="gc-meta t-s">${owner} · ${g.items_done.length}/${total} done${g.items_open.length ? ` · ${g.items_open.length} open` : ""}${holders.length ? ` · sessions on it: ${holders.map(who).join(", ")}` : ""}</div>
    ${bar(pct(g.items_done.length, total))}
    ${h ? `<details class="gc-handoff" data-handoff="${esc(g.name)}"><summary>Handoff v${h.version} · ${esc(h.by_agent || "")} · ${ago(h.created_at)}${g.handoff_due ? ` <span class="warn-t" title="Its owner finished #${g.handoff_due.id} after this handoff">older than the last item</span>` : ""}</summary><div class="gc-ht muted">Reading…</div></details>`
      : `<div class="muted t-s">No handoff yet: the owner writes it with maxpm goal handoff ${esc(g.name)}.</div>`}
    ${sub ? "" : `<div class="gc-subs">${subs.map(x => card(x, true)).join("")}${g.status === "open" ? button("+ sub-goal", { cls: "plain", data: { addgoal: g.project, parent: g.name } }) : ""}</div>`}
  </div>`;
}

// The goals of the scope: one goal (and its sub-goals), the goals of one project, or all; open first, in goal rank.
export function renderGoals() {
  if (!S) return;
  const showDone = $("#goalsDone").checked;
  let G = (S.goals || []).filter(g => (showDone || g.status === "open") && (!scope.project || g.project === scope.project));
  if (scope.goal) G = G.filter(g => g.name === scope.goal || g.parent === scope.goal);
  const top = G.filter(g => !g.parent || !G.some(x => x.name === g.parent))
    .sort((a, b) => (a.status !== "open") - (b.status !== "open") || a.project.localeCompare(b.project) || a.rank - b.rank);
  const html = top.map(g => card(g, false)).join("")
    + `<div class="gc-add">${button("+ goal", { cls: "plain", data: { addgoal: scope.project || "" } })}</div>`;
  const box = $("#goalCards");
  if (box.dataset.sig === html) return;
  const open = new Set([...box.querySelectorAll("details[open][data-handoff]")].map(d => d.dataset.handoff));  // a fold stays open
  box.innerHTML = html; box.dataset.sig = html;
  box.querySelectorAll("details[data-handoff]").forEach(d => { if (open.has(d.dataset.handoff)) { d.open = true; loadHandoff(d); } });
}

// The handoff's text when its fold opens, with links to the earlier versions.
async function loadHandoff(d, version) {
  const r = await fetch("api/goal-handoff?" + new URLSearchParams({ name: d.dataset.handoff, ...(version ? { version } : {}) })).catch(() => null);
  const j = r && r.ok ? await r.json() : null, box = d.querySelector(".gc-ht");
  if (!j || !j.handoff) { box.textContent = "Could not read the handoff."; return; }
  box.classList.remove("muted");
  box.innerHTML = `<pre class="gc-text">${esc(j.handoff.text)}</pre>` + (j.versions.length > 1 ? `<div class="t-s muted">Versions: ${j.versions.map(v =>
    `<span class="link${v.version === j.handoff.version ? " on" : ""}" data-hver="${v.version}" title="${esc((v.by_agent || "") + " · " + ago(v.created_at))}">v${v.version}</span>`).join(" ")}</div>` : "");
}
$("#goalCards").addEventListener("toggle", (e) => { const d = e.target; if (d.matches && d.matches("details[data-handoff]") && d.open) loadHandoff(d); }, true);
$("#goalCards").addEventListener("click", (e) => { const v = e.target.closest("[data-hver]"); if (v) loadHandoff(v.closest("details"), v.dataset.hver); });
$("#goalsDone").addEventListener("change", renderGoals);
