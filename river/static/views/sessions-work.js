// The work controls of the Sessions tab (#5102 step 2, design #4657), moved from the Board's strip and Agents:
// Start an agent and the open session slots at the top of the list (where the new session shows), Give work in the
// ⋯ menu of the head of a session that waits for work (#5115), and at the foot of the list the ended sessions (read only) and Close N
// finished (the tmux panes of sessions that are done). Drawn from the page's state (S) on each refresh.
import { $, esc, act, toast, ago, clip, shortTitle } from "../lib.js";
import { chip } from "../components/chip.js";
import { agentStart } from "../components/agentStart.js";
import { button } from "../components/button.js";
import { S } from "../page.js";

const put = (el, h) => { if (el && el.dataset.sig !== h) { el.innerHTML = h; el.dataset.sig = h; } };

// Start an agent, with the open slots (max_sessions less the sessions that run) and the ready items for agents.
export function renderStart() {
  if (!S) return;
  const ready = S.items.filter(i => i.ready && i.doer !== "human" && !["deploy", "review", "monitor"].includes(i.kind)).length;
  const slots = S.capacity ? S.capacity.spare_slots : 0;
  const line = `<span class="muted t-s" title="Open session slots: max_sessions less the sessions that run. Ready: items an agent can take now.">${slots} open slot${slots === 1 ? "" : "s"} · ${ready} ready</span>`;
  put($("#sessStart"), line + (ready ? agentStart(S.launch_agents, { label: "Start an agent", cls: "primary", attrs: 'data-launch="1"',
    title: "Open a new session in the folder of the most important ready item and start the chosen agent there" }) : ""));
}

// The ended sessions (stopped or gone agents with no running session), newest first; a click shows the chat read only.
let showEnded = false;
function ended() {
  const running = new Set(S.sessions || []);
  return (S.agents || []).filter(a => a.kind !== "human" && ["stopped", "gone"].includes(a.state) && !running.has(a.name))
    .sort((a, b) => String(b.last_seen || "").localeCompare(String(a.last_seen || ""))).slice(0, 30);
}
export function renderFoot() {
  if (!S) return;
  const E = ended(), done = S.tmux_done || [];
  put($("#sessFoot"), (E.length ? button(showEnded ? `Hide ended` : `Show ${E.length} ended`, { cls: "plain", pressed: showEnded,
      data: { "sess-ended": 1 }, title: "The sessions that stopped: their chats, read only" }) : "")
    + (done.length ? button(`Close ${done.length} finished`, { data: { "sess-tidy": 1 },
      title: "Close the tmux panes of the sessions that are done (maxpm view --tidy). A session that holds work, or shows a prompt, stays.\n"
        + done.map(p => `${p.name}: ${p.why}`).join("\n") }) : ""));
  const box = $("#sessEnded");
  box.classList.toggle("hidden", !showEnded || !E.length);
  if (!showEnded) return put(box, "");
  const on = document.querySelector("#sessEnded .srow.on");
  put(box, E.map(a => {
    const head = (S.session_names || {})[a.name] || a.name;
    return `<div class="srow ended" data-session="${esc(a.name)}" data-state="ended">
      <div class="srow-main" tabindex="0" role="button" title="${esc(a.name)}: ended ${esc(ago(a.last_seen))} ago" aria-label="Open the ended session ${esc(head)}">
        <div class="srow-h"><b class="srow-t">${esc(head)}</b><span class="spacer"></span><span class="dot gone"></span><span class="sago">${ago(a.last_seen)}</span></div>
        <div class="sub">${esc(clip(a.note || "ended", 120))}</div></div></div>`;
  }).join(""));
  if (on) { const r = box.querySelector(`[data-session="${CSS.escape(on.dataset.session)}"]`); if (r) r.classList.add("on"); }
}

// Give work (#5102 step 2; in the head's ⋯ menu since #5115): a session that waits for work (maxpm wait) takes a pushed item within seconds. The ready
// items of its projects, in a menu; the menu is not drawn again while the person picks from it.
const giveChoice = {};
export function renderGive(name) {
  const el = $("#sessGive"); if (!el || !S) return;
  if (el.contains(document.activeElement)) return;
  const a = (S.agents || []).find(x => x.name === name);
  if (!a || a.role !== "waiting" || a.holds.length) return put(el, "");
  const where = (a.waiting_in || "").split(",").filter(Boolean);
  const ready = S.items.filter(i => i.ready && i.doer !== "human" && i.kind === "work" && !i.reserved_for
    && (!where.length || where.includes(i.project)));
  const since = a.waiting_since ? ` since ${ago(a.waiting_since)}` : "";
  if (!ready.length) return put(el, `${chip("c-ready", "waiting" + since)} <span class="muted t-s">no ready item in ${esc(where.join(", ") || "its projects")}</span>`);
  put(el, `${chip("c-ready", "waiting" + since)}
    <select class="give-sel" data-give-item="${esc(a.name)}" aria-label="The item to give">${ready.map(i => `<option value="${i.id}"${giveChoice[a.name] == i.id ? " selected" : ""}>#${i.id} ${esc(shortTitle(i))}</option>`).join("")}</select>
    ${button("Give work", { data: { give: a.name }, title: "Push the chosen item to this session: it takes it at once" })}`);
}
document.addEventListener("change", (e) => { const s = e.target.closest && e.target.closest("#sessGive [data-give-item]"); if (s) giveChoice[s.dataset.giveItem] = s.value; });

// A click on these controls; true when it was one (the frame then stops).
export function onClick(t) {
  if (t.closest("[data-sess-ended]")) { showEnded = !showEnded; renderFoot(); return true; }
  if (t.closest("[data-sess-tidy]")) {
    act("tmux_tidy", {}).then(r => toast(r.closed.length ? `Closed ${r.closed.length} finished: ${r.closed.map(p => p.name).join(", ")}` : "No finished session to close")).catch(() => {});
    return true;
  }
  const gw = t.closest("#sessGive [data-give]");
  if (gw) {
    const to = gw.dataset.give, sel = document.querySelector(`#sessGive [data-give-item="${CSS.escape(to)}"]`);
    if (sel && sel.value) act("push", { id: +sel.value, to, note: "from the page: you were waiting for work" })
      .then(() => toast(`Gave #${sel.value} to ${to}`)).catch(() => {});
    return true;
  }
  return false;
}
