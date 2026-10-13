// The item drawer: the panel on the right with one item's details, actions, and history.
import { esc, ago, left, clip } from "../lib.js";
import { chip, statusChip, prioChip, doerChip, modelChip, agentChip } from "./chip.js";
import { input, textarea, select, field, two, renderKeepingEdits } from "./form.js";
import { agentStart } from "./agentStart.js";

// The drawer element. show() redraws it and keeps what the person typed (see renderKeepingEdits);
// a different key (another item) or fresh starts clean. While a field in it has focus, show() waits.
export function makeDrawer(el) {
  const api = {
    key: null,
    isOpen: () => el.classList.contains("open"),
    show(key, html, fresh = false) {
      const same = api.key === key;
      const focused = el.contains(document.activeElement) && ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName);
      if (same && focused && !fresh) return;
      api.key = key; el.dataset.id = key;
      renderKeepingEdits(el, html, fresh || !same);
      el.classList.add("open");
    },
    close() { el.classList.remove("open"); api.key = null; },
  };
  return api;
}

// A message on one line: kind, from, to, when, then the body.
export function msgLine(m) {
  return `<div class="ev">${chip("c-p", esc(m.kind))} <b>${esc(m.from_agent)}</b> → ${esc(m.to_agent || "next holder")} · ${ago(m.created_at)}${m.state !== "open" && m.state !== "read" ? " · " + esc(m.state) : ""}<div class="body" style="white-space:pre-wrap">${esc(clip(m.body, 600))}</div></div>`;
}

// The tree of open work an item waits on, as text.
function treeText(n, prefix = "", last = true, root = true) {
  const who = n.assignee ? `, ${n.assignee}${n.lease_seconds_left != null ? ", " + Math.floor(n.lease_seconds_left / 60) + "m left" : ""}` : "";
  const st = (n.ready ? "ready" : n.status.replace("_", " ")) + (n.blocked_reason ? ", " + clip(n.blocked_text || "blocked: " + n.blocked_reason, 60) : "");
  let out = (root ? "" : prefix + (last ? "└─ " : "├─ ")) + `#${n.id} ${root ? n.title : clip(n.title, 60)}  (${st}${who})\n`;
  n.children.forEach((c, i) => { out += treeText(c, prefix + (root ? "" : last ? "   " : "│  "), i === n.children.length - 1, false); });
  return out;
}

// Offer help to whoever holds a piece of the blocker tree.
function offerHelpHtml(it, me) {
  const held = [];
  (function walk(n, root) { if (!root && n.assignee && n.assignee !== me && ["in_progress", "held"].includes(n.status)) held.push(n); n.children.forEach(c => walk(c, false)); })(it.tree, true);
  if (!held.length || ["done", "dropped"].includes(it.status)) return "";
  return `<div style="margin-top:6px">${held.map(n => `<div class="actions"><span class="muted">#${n.id} held by ${esc(n.assignee)}</span>
      <input id="offer-${n.id}" placeholder="I am blocked on this; I can take …" style="flex:1"><button class="btn" data-offer="${n.id}">Offer help</button></div>`).join("")}</div>`;
}

// Open an agent session on this item (server open_agent_on): with the person on a person's item,
// on what blocks an item that waits, or Dispatch on a ready item.
function agentButton(it, S) {
  if (["done", "dropped", "in_progress"].includes(it.status) || it.backlog) return "";
  const [label, title] = it.doer === "human"
    ? ["Work on it with an agent", "Open an agent in the project folder that helps you do this item together"]
    : !it.ready ? ["Open an agent to unblock it", "Open an agent in the project folder that first takes what this item waits on"]
    : it.status === "open" && !it.reserved_for ? ["Dispatch an agent", "Give it to a session waiting in its project, or start a new agent for it"]
    : [];
  return label ? agentStart(S && S.launch_agents, { label, title, attrs: 'data-do="agent"' }) : "";
}

// A model field: the item's own value; a default from settings shows as the placeholder.
function modelInput(it, f, id, hint) {
  const own = it[f + "_from"] === "item" ? it[f] : "";
  const dflt = !own && it[f] ? `${it[f]} (${it[f + "_from"]} default)` : "";
  return input({ id, placeholder: dflt || hint, value: own || "" });
}

// The drawer for one item (it from /api/item/<id>). S: the page state; me: who acts.
export function itemDrawerHtml(it, { S, me }) {
  const closed = ["done", "dropped"].includes(it.status);
  return `
    <div style="display:flex;align-items:center;gap:8px"><span class="muted">#${it.id} · ${esc(it.project)}</span><span class="spacer" style="flex:1"></span><button class="btn" id="dClose">Close</button></div>
    <h3>${esc(it.title)}</h3>
    <div class="chips" style="justify-content:flex-start">${prioChip(it)}${doerChip(it)}${agentChip(it)}${modelChip(it)}${statusChip(it)}</div>
    <div class="actions" style="margin-top:6px;align-items:center"><span class="muted" style="font-size:12px">Goals:</span>
      ${(it.goals || []).map(g => chip("c-goal", `${esc(g)} <span class="link" data-untag="${esc(g)}" title="remove this goal tag">×</span>`)).join("") || '<span class="muted" style="font-size:12px">none</span>'}
      ${(S.goals || []).some(g => g.status === "open" && !(it.goals || []).includes(g.name)) ? `<select id="dGoal">${(S.goals || []).filter(g => g.status === "open" && !(it.goals || []).includes(g.name)).sort((a, b) => (b.project === it.project) - (a.project === it.project)).map(g => `<option value="${esc(g.name)}">${esc(g.name)}${g.project !== it.project ? " (" + esc(g.project) + ")" : ""}</option>`).join("")}</select><button class="btn" data-do="tag">Add goal</button>` : ""}</div>
    ${(it.refs || []).length ? `<div class="actions" style="margin-top:6px;align-items:center"><span class="muted" style="font-size:12px">Tracker:</span>
      ${it.refs.map(r => /^https?:\/\//.test(r.url || "") ? `<a class="chip c-p" href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.ref)} ↗</a>` : chip("c-p", esc(r.ref))).map((h, i) => h + (it.refs[i].synced_at ? '<span class="muted" style="font-size:12px">updated</span>' : ["done", "dropped"].includes(it.status) ? chip("c-human", "tracker not updated") : "")).join("")}</div>` : ""}
    ${it.backlog ? `<div class="meta" style="margin-top:6px"><b>In the backlog</b> since ${ago(it.backlog_at)}: ${esc(it.backlog_note || "")}. Nobody is offered it until it is activated.</div>` : ""}
    <div class="meta" style="margin-top:6px">${esc(it.reason)}${it.lease_expires_at ? " · lease " + left(it.lease_expires_at) + " left" : ""}</div>
    <div class="actions">
      ${it.backlog ? `<button class="btn primary" data-do="activate" title="Take it out of the backlog: open again, offered to agents and people when ready">Activate</button>` : ""}
      ${it.status === "open" && !it.backlog ? `<button class="btn primary" data-do="claim">Claim</button>` : ""}
      ${["in_progress", "held"].includes(it.status) ? `<button class="btn primary" data-do="done">Done</button><button class="btn" data-do="release">Release</button>` : ""}
      ${it.status === "open" ? `<button class="btn" data-do="done">Mark done</button>` : ""}
      ${it.status === "open" && !it.backlog && ["work", "fixes"].includes(it.kind) ? `<button class="btn" data-do="backlog" title="Keep it, but offer it to nobody until you activate it">To backlog</button>` : ""}
      ${!closed ? `<button class="btn" data-do="drop">Drop</button>` : `<button class="btn" data-do="reopen">Reopen</button>`}
      <button class="btn" data-do="save">Save details</button>
      ${agentButton(it, S)}
    </div>
    <div class="sec"><h4>Details</h4><div class="form">
      ${input({ id: "dTitle", value: it.title })}
      ${textarea({ id: "dNotes", rows: 3, placeholder: "Notes" }, it.notes)}
      ${field("Context", textarea({ id: "dContext", rows: 2, placeholder: "what a new agent must know to start" }, it.context))}
      ${field("Touches", input({ id: "dTouches", placeholder: "files it changes, comma separated", value: (it.touches || []).join(", ") }))}
      ${field("Check", input({ id: "dCheck", placeholder: "command that shows it works", value: it.check }))}
      ${field("Due", input({ id: "dDue", placeholder: "2026-10-15, fri 17:00, or none", value: "" }), it.due ? ` <span class="muted">(${esc(it.due_text)})</span>` : it.due_from ? ` <span class="muted">(from #${it.due_from}: ${esc(it.due_text)})</span>` : "")}
      ${two(field("Model", modelInput(it, "model", "dModel", "recommended, e.g. opus")),
        field("Effort", modelInput(it, "effort", "dEffort", (S.effort_levels || []).join(", ") || "low … max")))}
      ${two(field("Min model", modelInput(it, "min_model", "dMinModel", "weakest allowed, e.g. opus")),
        field("Max model", modelInput(it, "max_model", "dMaxModel", "strongest allowed, e.g. sonnet")))}
      ${field("Agent type", modelInput(it, "agent", "dAgent", "codex or claude-code; empty: any agent"))}
      ${two(field("Priority (own)", select({ id: "dPrio" }, [0, 1, 2, 3, 4], it.priority)),
        field("Who can do it", select({ id: "dDoer" }, [["any", "anyone"], ["ai", "agent"], ["human", "human"]], it.doer)))}
      ${two(field("Project", select({ id: "dProject" }, S.projects.map(p => p.name), it.project)),
        field("Order in project", `<span class="actions"><button class="btn" data-do="up">Up</button><button class="btn" data-do="down">Down</button></span>`))}
    </div></div>
    ${it.output ? `<div class="sec"><h4>Output</h4><div>${esc(it.output)}</div></div>` : ""}
    <div class="sec"><h4>Waits on</h4>
      ${it.waits_on_detail.map(w => `<div><span class="link" data-open="${w.id}">#${w.id} ${esc(w.title)}</span> <span class="muted">(${w.status})</span> <span class="link" data-undep="${w.id}">remove</span></div>`).join("") || '<div class="muted">nothing</div>'}
      <div class="actions" style="margin-top:6px"><input id="dDep" placeholder="ids, e.g. 4, 9" style="width:140px"><button class="btn" data-do="dep">Add</button></div>
    </div>
    <div class="sec"><h4>Blocker tree</h4><div class="tree">${esc(treeText(it.tree))}</div>${offerHelpHtml(it, me)}</div>
    <div class="sec"><h4>Messages</h4>
      ${(it.messages || []).map(m => msgLine(m)).join("") || '<div class="muted">none</div>'}
      <div class="actions" style="margin-top:6px"><select id="dMsgKind"><option value="note">note</option><option value="question">question</option><option value="alert">alert</option></select>
        <input id="dMsgBody" placeholder="${it.assignee ? "to " + esc(it.assignee) : "to whoever takes it next"}" style="flex:1"><button class="btn" data-do="msg">Send</button></div>
    </div>
    <div class="sec"><h4>Unblocks</h4>${it.unblocks_detail.map(w => `<div><span class="link" data-open="${w.id}">#${w.id} ${esc(w.title)}</span> <span class="muted">(${w.status})</span></div>`).join("") || '<div class="muted">nothing</div>'}</div>
    ${it.status === "open" && !it.backlog ? `<div class="sec"><h4>Push to agent</h4>
      ${it.reserved_until ? `<div>Pushed to <b>${esc(it.reserved_for)}</b> by ${esc(it.reserved_by || "?")} · ${left(it.reserved_until)} left · <span class="link" data-unpush="${it.id}">cancel</span></div>`
        : `<div class="actions"><select id="dPushTo">${S.agents.filter(a => a.name !== me).map(a => `<option>${esc(a.name)}</option>`).join("")}</select>
           <input id="dPushNote" placeholder="note (optional)" style="flex:1"><button class="btn" data-do="push">Push</button></div>
           <div class="muted" style="font-size:12px;margin-top:4px">Or drag the item row onto an agent. It is reserved for them for a while; their next maxpm go takes it first.</div>`}
    </div>` : ""}
    ${it.replan ? `<div class="sec"><h4>Replan</h4><div>${it.late_prereqs} prerequisites were added while it was claimed, so it is bigger than planned. Split or re-scope it, then <span class="link" data-do="replanned">clear the mark</span>.</div></div>` : ""}
    <div class="sec"><h4>Outside blocker</h4>
      ${it.blocked_reason ? `<div>${esc(it.blocked_text)} <span class="link" data-do="unblock">clear</span></div>${it.blocked_at ? `<div class="muted" style="font-size:12px">since ${ago(it.blocked_at)}${it.blocked_until ? ", ready by itself in " + left(it.blocked_until) : ""}</div>` : ""}`
        : `<div class="actions"><input id="dBlock" placeholder="waiting on …" style="flex:1"><input id="dUntil" placeholder="until (optional): 2h, mon 07:00" style="width:170px"><button class="btn" data-do="block">Set</button></div>`}
    </div>
    <div class="sec"><h4>History</h4>${it.events.map(e => `<div class="ev">${ago(e.at)} · <b>${esc(e.actor)}</b> ${esc(e.change)}</div>`).join("")}</div>`;
}
