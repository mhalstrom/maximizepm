// A task in the work of its session (#5572, design #5560, mark 2026-11-12: 'understand the task in the context of what
// the session has done and will do', 'there should be a button to Go to Session'). A #id in a chat and the task button
// of a session's head open it: a large view over the chat. Left, the line of the session that owns the task (it holds
// it, did it, or added it): Done, Now, Next, the clicked task marked. Right, the task: what it is, why it exists, what
// changed, where it goes. A goal task has the goal's head on top. Go to Session picks that session on the Sessions
// tab; Open the full task opens the item view. On a phone (390 px) the line comes first, the earlier tasks fold, and
// the task is a sheet at the bottom with Go to Session.
// Data: GET /api/item/<id>/context (core.work_line). Other sessions are named by what they do, never by name alone.
import { esc, getJson, shortTitle, toast } from "../lib.js";
import { makeDialog } from "./dialog.js";
import { button, iconButton } from "./button.js";
import { bar, pct } from "./bar.js";

let dlg = null, root = null, box = null, hooks = {}, shown = null;

function make() {
  if (dlg) return;
  root = document.createElement("div");
  root.id = "taskDlg"; root.className = "hidden";
  root.innerHTML = '<div class="box tc-box" aria-label="A task in the work of its session"></div>';
  document.body.appendChild(root);
  box = root.firstElementChild;
  dlg = makeDialog(root, { draft: false, onClose: () => { shown = null; } });
  root.addEventListener("click", (e) => {
    if (e.target === root) return;  // the overlay: makeDialog closes it
    if (e.target.closest("[data-tc-close]")) return dlg.close();
    const go = e.target.closest("[data-tc-go]");
    if (go) { dlg.close(); return hooks.onGo && hooks.onGo(go.dataset.tcGo); }
    const full = e.target.closest("[data-tc-full]");
    if (full) { dlg.close(); return hooks.onFull && hooks.onFull(+full.dataset.tcFull); }
    if (e.target.closest("[data-tc-unfold]")) { box.querySelector(".tc-line").classList.add("unfold"); return; }
    const row = e.target.closest("[data-tc-task]");
    if (row && +row.dataset.tcTask !== shown) openTask(+row.dataset.tcTask);  // the view moves to that task
  });
}

// Open the view for task id. onGo(agent): go to that session; onFull(id): the full task (the item view).
export async function openTask(id, opts) {
  make();
  if (opts) hooks = opts;
  const keep = shown != null && dlg.isOpen() ? (box.querySelector(".tc-line") || {}).scrollTop || 0 : 0;
  let w;
  try { w = await getJson(`api/item/${id}/context`); } catch (e) { toast(`Task #${id}: ${e.message}`, true); return; }
  shown = id;
  box.innerHTML = viewHtml(w);
  dlg.open();
  const line = box.querySelector(".tc-line");
  if (line) {
    line.scrollTop = keep;
    const on = line.querySelector(".tc-ev.on");
    if (on && !keep) on.scrollIntoView({ block: "nearest" });
  }
  const first = [...box.querySelectorAll("[data-tc-go], [data-tc-close]")].find(b => b.offsetParent);
  if (first) first.focus({ preventScroll: true });
}

export function closeTask() { if (dlg) dlg.close(); }

// ---- words and times

const today = (d) => d.toDateString() === new Date().toDateString();
export function when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return today(d) ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : d.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}
export function length(a, b) {
  if (!a || !b) return "";
  const m = Math.round((Date.parse(b) - Date.parse(a)) / 60000);
  if (m < 1) return "under a minute";
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60), r = m % 60;
  return r ? `${h} h ${r} min` : `${h} h`;
}
const STATE = { active: "running", away: "quiet for a while", gone: "ended", ended: "ended", stopped: "stopped" };
const short = (t) => esc(shortTitle(t));

// A session by what it does: the short title of its task, else what it said it does, else its role; a person by name.
function who(w, name) {
  if (!name) return "";
  const p = (w.people || {})[name] || { name };
  if (p.kind === "human") return esc(name);
  if (p.doing) return `the session on ${esc(p.doing)}`;
  if (p.role === "manager") return "the manager";
  return p.state === "ended" || p.state === "gone" ? "a session that has ended" : "another session";
}

// What the head calls the owner's session.
function ownerWords(w) {
  const p = (w.people || {})[w.owner] || {};
  if (p.role === "manager") return "the manager";
  const now = (w.now || [])[0], last = (w.done || []).slice(-1)[0];
  if (p.doing) return esc(p.doing);
  if (now) return short(now);
  if (last) return `${short(last)}, and more`;
  return short(w.item);
}

// A found or added task: its state in words.
function fate(w, f) {
  if (f.status === "done" || f.status === "dropped") {
    const by = f.closed_by, p = (w.people || {})[by] || {};
    const verb = f.status === "done" ? "done" : "dropped";
    if (!by || by === w.owner) return verb;
    return p.kind === "human" ? `${verb} by ${esc(by)}` : `another session ${f.status === "done" ? "did it" : "dropped it"}`;
  }
  if (f.holder) return f.holder === w.owner ? "it works on it" : `${who(w, f.holder)} works on it`;
  return "nobody holds it yet";
}
const pill = (text, cls = "") => `<span class="tc-pill ${cls}">${text}</span>`;
// A task named in a sentence of the line: a button that moves the view to it.
const link = (t) => button(short(t), { kind: "card", cls: "tc-k", data: { "tc-task": t.id }, title: "Show this task" });

// ---- the line

function ev(w, t, { time, dot, sub, pills = "" }) {
  const on = t.id === w.item.id;
  const title = `<span class="tc-tl"><span class="tc-ti">${short(t)}</span>${pills}${on ? pill("you clicked", "tc-you") : ""}</span>`;
  return button(`<span class="tc-t">${esc(time || "")}</span><span class="tc-dot ${dot}"></span>`
    + `<span class="tc-txt">${title}<span class="tc-sub">${sub || ""}<span class="tc-num">${sub ? " · " : ""}#${t.id}</span></span></span>`,
  { kind: "card", cls: "tc-ev" + (on ? " on" : ""), title: on ? "This task" : "Show this task",
    data: { "tc-task": t.id } });
}

function kids(w, list, words) {
  if (!list || !list.length) return "";
  const n = list.length === 1 ? "one more task" : `${list.length} more tasks`;
  return `<div class="tc-kid">${words.replace("{n}", n)}: ${list.map(f =>
    `${link(f)} ${pill(fate(w, f))}`).join(" · ")}</div>`;
}

function lineHtml(w) {
  const done = w.done || [], now = w.now || [], next = w.next || [];
  const at = done.findIndex(t => t.id === w.item.id);
  const fold = at > 1 ? at - 1 : 0;  // on a phone the tasks before the one above the clicked one fold
  const out = [];
  out.push(`<div class="tc-grp">Done${done.length ? ` · ${done.length} task${done.length === 1 ? "" : "s"}` : ""}</div>`);
  if (!done.length) out.push('<div class="tc-none">Nothing done yet in this session</div>');
  if (fold) out.push(button(`▸ ${fold} earlier task${fold === 1 ? "" : "s"}`, { kind: "card", cls: "tc-unfold", data: { "tc-unfold": 1 }, title: "Show earlier tasks" }));
  done.forEach((t, i) => {
    const why = t.id === w.item.id && w.item.found_during && done.some(d => d.id === w.item.found_during)
      ? ` · it found this task during ${short(done.find(d => d.id === w.item.found_during))}` : "";
    const outcome = t.outcome && t.outcome !== "done" ? ` · ${esc(t.outcome)}` : "";
    out.push(`<div class="tc-row${i < fold ? " tc-early" : ""}">` + ev(w, t, { time: when(t.started),
      dot: t.outcome === "done" ? "done" : "wait", sub: esc(length(t.started, t.ended)) + outcome + why })
      + kids(w, t.found, "It found {n}") + "</div>");
  });
  if ((w.added || []).length) out.push(kids(w, w.added, "It also added {n}"));
  out.push('<div class="tc-grp">Now</div>');
  if (!now.length) out.push(`<div class="tc-none">${(w.people[w.owner] || {}).state === "ended" ? "This session has ended" : "It holds no task now"}</div>`);
  for (const t of now) {
    out.push(ev(w, t, { time: when(t.started), dot: "now", pills: pill(t.status === "held" ? "on hold" : "working", "tc-prog"),
      sub: t.blocked_reason ? `waits on: ${esc(t.blocked_reason)}` : "" }));
    if (t.waits_on.length) out.push(`<div class="tc-kid">Waits on ${t.waits_on.map(o =>
      link(o) + (o.holder ? ` (${who(w, o.holder)})` : " (nobody holds it yet)")).join(" · ")}</div>`);
    out.push(kids(w, t.found, "It found {n}"));
  }
  out.push('<div class="tc-grp">Next</div>');
  if (!next.length) out.push('<div class="tc-none">Nothing planned. After this it takes linked work, or it waits</div>');
  for (const t of next) {
    if (t.id == null) { out.push(`<div class="tc-kid tc-instr">${esc(t.reason)}: ${esc(t.body || "")}</div>`); continue; }
    out.push(ev(w, t, { dot: "next", sub: esc(t.reason) + (t.holder ? ` · ${who(w, t.holder)} works on it` : "") }));
  }
  const g = w.goal, listed = new Set([...done, ...now, ...next].map(t => t.id));
  const more = g ? g.open_tasks.filter(t => !listed.has(t.id)) : [];
  if (more.length) {
    out.push(`<div class="tc-grp">Next · the goal's open tasks</div>`);
    for (const t of more.slice(0, 8)) out.push(ev(w, t, { dot: "next", sub: t.holder ? `${who(w, t.holder)} works on it` : "nobody holds it yet" }));
    if (more.length > 8) out.push(`<div class="tc-none">and ${more.length - 8} more</div>`);
  }
  return out.join("");
}

// ---- the task and the goal

function taskHtml(w, go) {
  const t = w.item, done = w.done || [];
  const state = t.status === "done" ? `done ${when(t.closed_at)}` : t.status === "dropped" ? `dropped ${when(t.closed_at)}`
    : t.status === "in_progress" ? "in progress" : t.status === "held" ? "on hold" : t.nobody_holds ? "nobody holds it yet" : "open";
  const sec = (h, body) => body ? `<div class="tc-sec"><h4>${h}</h4>${body}</div>` : "";
  const from = t.found_during && (done.find(d => d.id === t.found_during) || (w.now || []).find(d => d.id === t.found_during));
  const why = from ? `This session found it while it worked on ${short(from)}.`
    : t.found_during ? `A session found it while it worked on task #${t.found_during}.`
    : t.context ? esc(t.context.length > 400 ? t.context.slice(0, 399).trimEnd() + "…" : t.context) : "";
  const changed = t.status === "done" || t.status === "dropped" ? esc(t.changed || shortTitle(t)) : "";
  const ships = (t.ships || []).map(s => `In the release of ${esc(s.target || "its target")} (task #${s.id}): `
    + (s.status === "done" ? `out ${esc(when(s.closed_at))}` : s.status === "dropped" ? "that release was dropped" : "not out yet")).join("<br>");
  return `<div class="tc-meta t-s muted">Task #${t.id} · ${esc(t.project || "")} · ${esc(state)}</div>
    <h3 class="tc-h">${short(t)}</h3>
    ${sec("What it is", esc(t.title))}
    ${sec("Why it exists", why)}
    ${sec("What changed", changed)}
    ${sec("Where it goes", ships)}
    <div class="tc-acts">${go}${button("Open the full task", { data: { "tc-full": t.id }, title: "Open full task" })}</div>`;
}

function goalHtml(g) {
  if (!g) return "";
  const total = g.done + g.open;
  return `<div class="tc-goal">
    <div class="t-s muted">Goal${g.parent ? ` · part of ${esc(g.parent.outcome || g.parent.name)}` : ""}</div>
    <div class="tc-go">${esc(g.outcome || g.name)}</div>
    ${g.done_when ? `<div class="t-s muted">Done when: ${esc(g.done_when)}</div>` : ""}
    ${bar(pct(g.done, total))}
    <div class="t-s muted">${g.done} task${g.done === 1 ? "" : "s"} done · ${g.open} open · ${g.sessions} session${g.sessions === 1 ? "" : "s"} worked on it${g.handoff_at ? ` · last handoff ${esc(when(g.handoff_at))}` : ""}</div>
  </div>`;
}

export function viewHtml(w) {
  const person = w.owner && ((w.people || {})[w.owner] || {}).kind === "human";
  const line = w.owner && !person && w.done;
  const p = (w.people || {})[w.owner] || {};
  const go = line ? button("Go to Session", { kind: "primary", cls: "tc-gobtn", data: { "tc-go": w.owner }, title: "Open its chat" }) : "";
  const head = line ? `Session: ${ownerWords(w)}` : person ? `A task of ${esc(w.owner)}` : "A task nobody worked on yet";
  const count = line ? (w.done.length + (w.now || []).length) : 0;
  const meta = line ? [w.since ? `started ${when(w.since)}` : "", `${count} task${count === 1 ? "" : "s"}`, STATE[p.state] || ""]
    .filter(Boolean).join(" · ") : "";
  return `<div class="dlg-head tc-head"><h2>${head}</h2><span class="t-s muted tc-hm">${esc(meta)}</span>
      <span class="tc-sp"></span>${go}${iconButton("✕", "Close", { cls: "tc-x", data: { "tc-close": 1 } })}</div>
    ${goalHtml(w.goal)}
    <div class="tc-body${line ? "" : " tc-only"}">
      ${line ? `<div class="tc-line">${lineHtml(w)}</div>` : ""}
      <div class="tc-det">${taskHtml(w, go)}</div>
    </div>`;
}
