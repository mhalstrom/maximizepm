// The Sessions tab: a list of the running agent sessions (Claude Code and Codex) in a left bar, one row each with
// its agent, item and state, read again every few seconds while the tab shows; and the selected session on the
// right: its transcript, with new lines added as the session writes them (#4597, in place of the cards of #4592).
// The view shows the session as a chat (components/chat.js, #4604). A person types in the session through Open
// (its claude.ai/code link), or, for a session in a tmux pane, the composer below the chat (#4596): the text goes
// to the pane, then Enter. MaximizePM types nothing by itself.
// Server: GET /api/sessions, GET /api/session?agent=<name>&cursor=<byte>, and the op session_send (with the
// page's token: typeAction).
import { $, esc, ago, clip, store, toast, typeAction, act, actor, shortTitle, getJson } from "../lib.js";
import { chip } from "../components/chip.js";
import { appendEntries, prependEntries, agePending, wireCopy, plainText, drawWaiting } from "../components/chat.js";
import { openTerminal } from "../components/terminalDialog.js";
import { openWaiting, closeWaiting } from "../components/waitDialog.js";
import { openSettings } from "../components/settingsDialog.js";
import { makeDictation } from "../components/dictate.js";
import { viewOf } from "../components/viewSettings.js";
import { openInPanel, openList, wirePanel, panelFor } from "../components/sidePanel.js";
import { openTask } from "../components/taskContext.js";
import { openBrowser } from "../components/browserTool.js";
import { checkPaths } from "../components/artLinks.js";
import { cacheBadge, askCold } from "../components/cacheBadge.js";
import { contextMeter, openUsage } from "../components/usagePanel.js";
import { endButton, endSession } from "../components/endSession.js";
import { button, buttonEl } from "../components/button.js";
import { composerHtml, composerState } from "../components/composer.js";
import { makeMenu } from "../components/menu.js";
import { makeSlashList } from "../components/slashList.js";
import { markYou } from "./you.js";
import { S } from "../page.js";
import { renderGive } from "./sessions-work.js";
import { queueAnywhere } from "../components/agentStart.js";
import { loadAgents, agentOf, mainAgent } from "../components/agents.js";
import { statusRowHtml, statusHtml, togglePush } from "./status.js";

const PICKED = "river.session";
const FOLD = "river.sessFold";  // "1": the person folded the list of sessions (#5626)

// The list of sessions folds to a thin strip, so the chat and the side panel get its width (#5626, mark: 'we need to be
// able to collape the running session left panel'). The button at the list's top, or Cmd+\ (Ctrl+\ off a Mac), folds
// it and opens it again; this browser keeps the choice. Under 760 px (phone.css) the tab shows the list or the chat, and
// the strip does not apply (sessions.css sets it only from 760 px).
function wireFold() {
  const lay = $("#sessLayout"), btn = $("#sessFold");
  const key = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent) ? "Cmd+\\" : "Ctrl+\\";
  const set = (folded, keep) => {
    lay.classList.toggle("side-folded", folded);
    btn.textContent = folded ? "›" : "‹";
    btn.title = (folded ? "Show list" : "Hide list") + ` (${key})`;
    btn.setAttribute("aria-label", folded ? "Show the session list" : "Hide the session list");
    btn.setAttribute("aria-expanded", folded ? "false" : "true");
    if (keep) store(FOLD, folded ? "1" : "0");
  };
  set(store(FOLD) === "1", false);
  btn.addEventListener("click", () => set(!lay.classList.contains("side-folded"), true));
  document.addEventListener("keydown", (e) => {
    if (e.key !== "\\" || !(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey) return;
    if ($("#tab-sessions").classList.contains("hidden") || window.innerWidth < 760) return;
    e.preventDefault(); set(!lay.classList.contains("side-folded"), true);
  });
}
// Fill a part of the head only when its HTML changed: a poll (2 s) then replaces no control under the pointer.
const putHtml = (el, h) => { if (el && el.dataset.sig !== h) { el.innerHTML = h; el.dataset.sig = h; } };  // the session the person chose last


// The head of the chosen session (#4608; one row and one ⋯ menu since #5115). The menu holds Open session (its
// claude.ai/code page; else its tmux pane in the terminal view; else off, with why), Give work (sessions-work.js),
// Settings, the links to Plan, and End (#5041) for a session that runs. Each entry is a plain row of the menu.
const MI = "sess-mi";
function headerOpen(s) {
  const set = s.provider ? button("Settings", { cls: MI, data: { settings: s.name }, title: "This session's settings" }) : "";
  return headerOpenBtn(s) + stopRow(s) + pinRow(s) + set + (s.running ? endButton(s, { cls: MI + " sess-mi-end" }) : "");
}
// Stop (#5547, mark 2026-11-12): ends the running turn now, as Esc in its terminal; the session keeps its context and
// item. Only while it runs a turn (or waits at a prompt) in a tmux pane here. The keys are its agent's
// (components/agents.js interrupt); an agent with none says so in the row.
const STOPS = ["working", "prompt", "stopping"];
const stopKeys = (s) => (agentOf(s.provider || s.agent_type) || mainAgent() || {}).interrupt || [];
// After a first Stop of an agent with a second step (Codex: /stop ends its background commands, #5563) the list row
// says stop_again: Stop stays while it waits at its prompt.
const canStop = (s, state) => !!(s.running && s.terminal && (STOPS.includes(state) || s.stop_again));
function stopRow(s) {
  if (!canStop(s, s.state)) return "";
  if (stopKeys(s).length) return button("Stop its turn", { cls: MI, data: { stop: s.name }, title: "End its turn now" });
  const why = `${(agentOf(s.provider || s.agent_type) || {}).label || "Its agent"} has no key that stops a turn`;
  return button(`Stop its turn<span class="muted sess-noopen">${esc(why)}</span>`, { cls: MI, disabled: true, title: why });
}
// Pin and Unpin of a worker in the ⋯ menu (#5400): a touch screen has no hover, so the row's pin does not show there.
function pinRow(s) {
  if (!s.running || ["manager", "expert", "assistant"].includes(s.role)) return "";
  const on = isPinned(s);
  return button(on ? "Unpin" : "Pin to the top", { cls: MI, data: { "pin-to": on ? "off" : "on", "pin-name": s.name },
    title: on ? "Unpin: it goes back to its place" : "Pin to the top of the worker sessions" });
}
// The texts of the web link and of why there is none come from the session's agent (components/agents.js, #5184);
// a session of no known agent takes the main agent's.
function headerOpenBtn(s) {
  const setup = (agentOf(s.provider || s.agent_type) || mainAgent() || {}).setup || {};
  if (/^https:\/\//.test(s.session_url || "")) return `<a class="btn ${MI}" href="${esc(s.session_url)}" target="_blank" rel="noopener" title="${esc(setup.open_title || "Open this session's web page")}">${esc(setup.open_link || "Open session")}</a>`;
  if (s.terminal) return button("Open in terminal", { cls: MI, data: { term: s.name }, title: "Open its tmux pane in the terminal view" });
  const why = setup.no_link || "This session has no web link and runs in no tmux pane here";
  return button(`Open session<span class="muted sess-noopen">${esc(why)}</span>`, { cls: MI, disabled: true, title: why });
}
// The targets a session owns: entries are {name, owner_expires_at} (or a name), shown by name (#4686).
const ownNames = (s) => (s.owns || []).map(o => (o && o.name) || String(o));
// Links to Plan (#5102 step 7, design #4657): the project and goals of the session's item, and a goal or target it
// owns. Rows of the ⋯ menu (#5115).
function planLinks(s) {
  // The session's row names its item's project and goals (#5124); the page's state is the fallback.
  const h = s.holds && s.holds[0];
  const it = h && (h.project ? h : ((S && S.items) || []).find(i => i.id === h.id));
  const link = (view, project, goal, text, title) => button(esc(text), { cls: MI,
    data: { "plan-go": view, project, ...(goal ? { goal } : {}) }, title });
  const out = [];
  if (it) {
    out.push(link("items", it.project, "", "Plan: " + it.project, `The items of ${it.project} in Plan`));
    for (const g of it.goals || []) out.push(link("goals", it.project, g, "Plan: goal " + g, `The goal ${g} in Plan`));
  }
  for (const g of ((S && S.goals) || []).filter(x => x.owner === s.name && x.status === "open"))
    out.push(link("goals", g.project, g.name, "Plan: owns goal " + g.name, `The goal ${g.name} in Plan: this session owns it`));
  for (const tg of ownNames(s)) out.push(link("releases", "", "", "Plan: owns target " + tg, `The releases in Plan: this session owns the target ${tg}`));
  return out.join("");
}
// The item in the head: a link to it in the side panel, only when the session holds one (#5115: no 'No item').
function headerItem(s) {
  return (s.holds || []).map(h => button(
    `#${h.id}<span class="sess-it-t"> ${esc(shortTitle(h))}</span>${h.status && h.status !== "in_progress" ? " " + chip("c-" + h.status, esc(h.status.replace("_", " "))) : ""}`,
    { cls: "sess-itembtn", data: { open: h.id }, title: `#${h.id} ${h.title || ""}` })).join("");
}

// The state of a session in its head: a dot and its word (#5115), the same dot as its row of the list.
function stateDot(s) {
  const tip = s.state === "prompt" ? "A permission prompt waits in its terminal or session: answer it there" : s.state_label;
  return `<span class="sess-stw" title="${esc(tip || "")}"><span class="dot ${DOT[s.state] || "away"}"></span>${esc(s.state_label || "")}</span>`;
}

// The chat provider of a session (#4627): a small label from the server's provider table, only when its agent is
// not the main one (components/agents.js).
const tagged = (p) => p && !(agentOf(p) || {}).main;
function provTag(p) {
  return tagged(p) ? `<span class="prov prov-${esc(p.id)}" title="Runs in ${esc(p.label)}">${esc(p.label)}</span>` : "";
}

// The shell commands that run in a session now, foreground and background (#5830; maxpm serve reads them): a chip
// with their number, and each one's age and command in the hover of the row and of the chip.
export function shellLines(s) {
  return (s.shells || []).map(x => `${ago(x.started).replace(" ago", "")}: ${x.label}`);
}
export function shellChip(s) {
  const n = (s.shells || []).length;
  return n ? chip("c-in_progress shchip", `${n} running`, shellLines(s).join("\n")) : "";
}

// The ! badge: how many things wait for the person in this session (a prompt in its pane, open questions).
function waitBadge(s) {
  const n = (s.waiting || []).length;
  return n ? `<span class="wbadge" title="${n} thing(s) wait for you here">! ${n}</span>` : `<span class="wbadge-slot" aria-hidden="true"></span>`;
}

// The pin of a worker row (#5007): a pinned session stays at the top of the worker sessions. The pin is the
// person's (You are), stored by maxpm; with nobody chosen in You are, any person's pin counts.
const PIN_SVG = `<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path fill="currentColor" d="M9.8 1.2a1 1 0 0 1 1.4 0l3.6 3.6a1 1 0 0 1 0 1.4l-.9.9a1 1 0 0 1-1.1.2l-2.4 2.4.3 2.4a1 1 0 0 1-.3.8l-.7.7a.6.6 0 0 1-.9 0L6.4 11.2 2.7 14.9a.6.6 0 0 1-.9-.9l3.7-3.7-2.4-2.4a.6.6 0 0 1 0-.9l.7-.7a1 1 0 0 1 .8-.3l2.4.3 2.4-2.4a1 1 0 0 1 .2-1.1z"/></svg>`;
export function isPinned(s) {
  const me = actor(), by = s.pinned_by || [];
  return me ? by.includes(me) : by.length > 0;
}
function pinButton(s) {
  // No pin on these rows. The pin and End sit over the end of the second line (#5402), so no slot keeps a place.
  if (["manager", "expert", "assistant"].includes(s.role)) return "";
  const on = isPinned(s);
  return button(PIN_SVG, { kind: "icon", cls: "srow-pin", pressed: on, data: { pin: s.name },
    title: on ? "Unpin: it goes back to its place" : "Pin to the top of the worker sessions", label: `${on ? "Unpin" : "Pin"} ${s.name}` });
}

// One row of the left bar, two lines as in a Messages list (#5086): line 1 is a short name of the work (the session's
// own headline, or the short title of its item), line 2 what it does now in plain words (its 'maxpm now' text, or
// the plain words of its last tool call, or what it waits for). At the right: the ! badge, the state dot and the
// time; the pin and End show on hover (the pin also while it is on). The agent id, the item, the target and the last
// message are the hover of the row and the head of the opened session. A session that waits at a prompt or for an
// answer stands out (class "needs").
const DOT = { working: "active", expert: "active", prompt: "needs", answer: "needs", idle: "away", starting: "away",
              stopping: "gone", ended: "gone", gone: "gone" };
// #5364: the head of the opened session shows the state of its row, so the two never disagree: the list applies
// rules that one session's read does not (no transcript and no process: not connected yet; a pane that shows a shell:
// ended; a question the person answered in the chat). A session that left the list has ended. Before the first read
// of the list, the session's own read (one) says it.
export function headState(row, one, listRead) {
  if (row) return { state: row.state, state_label: row.state_label };
  if (listRead) return { state: "gone", state_label: "ended" };
  return { state: one.state, state_label: one.state_label };
}
function rowHover(s) {
  const last = s.last.length ? s.last[s.last.length - 1] : null;
  const lastText = last ? (last.role === "tool" ? last.text : plainText(last.text)) : "";
  const h = s.holds && s.holds[0];
  return [s.name + (s.provider ? " · " + s.provider.label : ""), s.state_label,
    h ? `#${h.id} ${h.title}` : "", ownNames(s).length ? "Owns target " + ownNames(s).join(", ") : "",
    ...shellLines(s).map(x => "Runs " + x),
    lastText ? "Last: " + clip(lastText, 300) : ""].filter(Boolean).join("\n");
}
// The kind of agent, only when it is not the main one (Claude Code): a small letter mark (Codex: C).
function provIcon(p) {
  return tagged(p) ? `<span class="prov prov-ic prov-${esc(p.id)}" title="Runs in ${esc(p.label)}" aria-label="${esc(p.label)}">${esc(p.label.slice(0, 1))}</span>` : "";
}
export function sessionRow(s) {
  // The assistant (#5110) goes through what waits on the person: its tag names its job, as the manager's does.
  const role = s.role === "manager" ? `<span class="mgr-tag">Manager</span>` : s.role === "expert" ? `<span class="mgr-tag expert-tag">Expert</span>`
    : s.role === "assistant" ? `<span class="mgr-tag expert-tag">Needs you</span>` : "";
  const head = s.role === "expert" ? s.name : s.role === "assistant" || (s.role === "manager" && s.title === "Manager") ? "" : s.title || s.name;
  const line2 = s.role === "expert" && s.topic ? s.topic : s.doing || s.state_label;
  // data-agent: a drop target for an item row (#5102 step 2): the item goes into this session's queue.
  return `<div class="srow${["prompt", "answer"].includes(s.state) || (s.waiting || []).length ? " needs" : ""}${isPinned(s) ? " pinned" : ""}" data-session="${esc(s.name)}" data-agent="${esc(s.name)}" data-state="${esc(s.state)}">
    <div class="srow-main" tabindex="0" role="button" title="${esc(rowHover(s))}" aria-label="Open session ${esc(head || s.name)}">
      <div class="srow-h">${role}${head ? `<b class="srow-t">${esc(head)}</b>` : ""}${provIcon(s.provider)}<span class="spacer"></span>${shellChip(s)}${waitBadge(s)}<span class="dot ${DOT[s.state] || "away"}" title="${esc(s.state_label)}"></span><span class="sago" data-at="${esc(s.at || "")}">${s.at ? ago(s.at) : ""}</span></div>
      <div class="sub">${esc(clip(line2, 120))}</div>
      <span class="srow-acts">${pinButton(s)}${endButton(s, { icon: true, cls: "srow-end" })}</span>
      ${s.role === "expert" && s.cache ? `<div class="sid cache-line">${cacheBadge(s.cache)}</div>` : ""}
    </div>
  </div>`;
}

// The manager's rows come first, apart from the other sessions (#4616); with no manager running, a row that starts
// one (the page's Start manager: the launch dialog, then the op start_manager).
const NO_MANAGER = `<div class="srow mgr-none"><div class="srow-main"><div class="srow-h"><span class="mgr-tag">Manager</span>
  <span class="muted">No manager running</span></div></div>
  <div class="srow-it">${button("Start manager", { kind: "primary", data: { "start-manager": "1" }, title: "Start a manager" })}</div></div>`;

// Replace only the rows whose data changed, so a row keeps its hover and focus between reads.
function renderList(el, list, picked, empty) {
  const rows = [...el.querySelectorAll("[data-session]")];
  if (!list.length) {
    if (el.dataset.empty !== "1") { el.innerHTML = empty; el.dataset.empty = "1"; }
    return;
  }
  delete el.dataset.empty;
  const sigs = list.map(s => JSON.stringify(s));
  if (rows.length !== list.length || rows.some((r, i) => r.dataset.session !== list[i].name)) {
    el.innerHTML = list.map(sessionRow).join("");
    [...el.children].forEach((r, i) => { r.dataset.sig = sigs[i]; });
  } else {
    rows.forEach((r, i) => {
      if (r.dataset.sig === sigs[i]) return;
      const t = document.createElement("template");
      t.innerHTML = sessionRow(list[i]).trim();
      t.content.firstChild.dataset.sig = sigs[i];
      r.replaceWith(t.content.firstChild);
    });
  }
  el.querySelectorAll(".sago").forEach(x => { x.textContent = x.dataset.at ? ago(x.dataset.at) : ""; });
  el.querySelectorAll("[data-session]").forEach(r => r.classList.toggle("on", r.dataset.session === picked));
}

export function makeSessions({ isShown, onPick = () => {}, onPane = () => {} }) {
  let list = [], busy = false, again = false, proposals = [], listRead = false;
  // A pane in place of a session's chat (#5102): "you" (what waits on the person), "status" (every session by its
  // state, #5852) or "arts" (the artifacts of all sessions); null while a session shows.
  let pane = null;
  const PANES = { you: "#sessYou", status: "#sessStatus", arts: "#sessArts" };
  let push = "off";  // notify_status: the states whose Push is on in the Status view (#5852)
  const seenAt = {};  // agent: the last session_seen this page sent for it (ms), so a poll sends it once per turn end
  // The reply sheet of the Status view (#5853): the session it sends to, and its microphone (made with the view).
  let replyTo = null, replyDict = null;
  function closeReply() {
    if (replyDict) replyDict.stop();
    replyTo = null;
    $("#statusReply").classList.add("hidden");
  }
  let agent = null, cursor = null, timer = null, gen = 0, lastCache = null, lastRead = null;
  // The messages that maxpm serve holds for this session until its turn ends (#5587), under the chat.
  let waiting = [];
  const drawPend = (rows) => { waiting = rows; drawWaiting($("#sessPend"), rows); };
  let older = null, readingOlder = false;  // where the earlier lines of the chat start (#5120), and a read of them
  let pinned = false;  // chosen by the person: it stays on screen when it is not running (read only, #4622)
  const side = $("#sessions"), mgr = $("#sessMgr"), bar = side.closest(".sess-side");
  const experts = $("#sessExpertRows"), expertGroup = $("#sessExperts"), assist = $("#sessAssist");
  const body = $("#sessBody"), col = $("#sessCol"), main = $("#sessMain");
  wireCopy(col);
  wirePanel();
  wireFold();
  // The width of the chat's scrollbar when it takes room (Linux, Windows, a Mac with a mouse: 15 px; CI's Chrome showed
  // it, #5584): sessions.css moves it into the pane's side padding, so the chat keeps the width of its foot (#5555).
  new ResizeObserver(() => main.style.setProperty("--sb-w", (body.offsetWidth - body.clientWidth) + "px")).observe(body);
  // The chat stays in sight (#4685): in this tab an item opens over the chat or in the side panel, not in the drawer:
  // a #<id> in the chat and the item of the header open the task view (#5572), the item of a session row and an item
  // of the panel's lists open in the side panel; Items and Artifacts open there as lists.
  $("#tab-sessions").addEventListener("click", (e) => {
    // Also from the ⋯ menu, where a phone has them (#5402): the menu closes.
    const l = e.target.closest("[data-sp-open]"); if (l) { if (l.closest("#sessMenu")) headMore(false); return openList(l.dataset.spOpen); }
    const br = e.target.closest("[data-sp-browser]");
    if (br) { if (br.closest("#sessMenu")) headMore(false); return openBrowser(agent).catch(() => {}); }  // #4631
    if (e.target.closest("#sessYou, #sessArts")) return;  // these views open an item in the drawer, as their tabs did
    const o = e.target.closest("[data-open]");
    if (!o || !/^\d+$/.test(o.dataset.open) || e.metaKey || e.ctrlKey) return;
    e.preventDefault(); e.stopPropagation();
    // A #id in the chat and the task of the head: the task in the work of its session (#5572); the rest (a row's
    // task, the panel's lists) opens in the side panel.
    if (o.closest("#sessCol, #sessItem, #sessMenuItem")) {
      if (o.closest("#sessMenu")) headMore(false);  // the head's task in the ⋯ menu, where a phone has it
      return openTask(+o.dataset.open, { onGo: (name) => pick(name), onFull: (id) => openInPanel("item:" + id) });
    }
    openInPanel("item:" + o.dataset.open);
  });
  // A link or a file path in the chat opens in the side panel; Cmd or Ctrl+click keeps a new browser tab (#4619).
  $("#sessItem").addEventListener("click", (e) => {  // the context meter of the header (#5224)
    if (e.target.closest("[data-usage]") && agent) { e.preventDefault(); openUsage(agent); }
  });
  col.addEventListener("click", (e) => {
    const t = e.target.closest("[data-term]");  // a slash command that shows its view in the terminal (#5220)
    if (t) { e.preventDefault(); openTerminal(t.dataset.term); return; }
    const f = e.target.closest("[data-file]");
    if (f) { e.preventDefault(); openInPanel(f.dataset.file, agent); return; }
    const a = e.target.closest("a[href]");
    if (!a || a.hasAttribute("download") || e.metaKey || e.ctrlKey || e.shiftKey || !/^https?:\/\//i.test(a.getAttribute("href"))) return;
    e.preventDefault(); openInPanel(a.href, agent);
  });
  col.addEventListener("keydown", (e) => {
    const f = e.key === "Enter" && e.target.closest("[data-file]"); if (f) { e.preventDefault(); openInPanel(f.dataset.file); }
    const o = e.key === "Enter" && e.target.closest("span[data-open], span[data-term]"); if (o) { e.preventDefault(); o.click(); }
  });
  const note = (text) => { col.replaceChildren(); const n = document.createElement("div"); n.className = "muted sl-none"; n.textContent = text; col.append(n); };

  // The tab fills the window below the header; the view scrolls inside it, so the text bar stays in sight.
  // On a phone (#4885) the tab shows the list or the chat of one session: the chat fills the screen under the header.
  // The keyboard of a phone (#4902): while a field of the chat has the focus and the keyboard makes the visible part
  // (visualViewport) smaller than the window, the tab is fixed to that visible part: its top (iOS scrolls the page
  // under the keyboard: offsetTop) and its height, so the message box sits right above the keyboard. It follows each
  // resize and scroll of the visible part, and again after the keyboard's animation ends.
  // The keyboard is open when the visible part is shorter than the whole page height (#4910): iOS Safari makes
  // window.innerHeight smaller with the keyboard too, so a test against innerHeight said "no keyboard", the tab stayed
  // in the page, and Safari scrolled its head and most of the chat above the screen. The whole height is the layout
  // viewport (documentElement.clientHeight, which the keyboard does not change), or the tallest visible part seen
  // while no field had the focus, at this width.
  let full = { w: 0, h: 0 };
  function keyboardOpen(lay, vv) {
    const a = document.activeElement, field = !!(a && lay.contains(a) && /^(TEXTAREA|INPUT)$/.test(a.tagName));
    if (!vv) return false;
    if (full.w !== window.innerWidth) full = { w: window.innerWidth, h: 0 };
    if (!field) full.h = Math.max(full.h, vv.height);
    return field && vv.height < Math.max(document.documentElement.clientHeight, full.h) - 80;
  }
  function fit() {
    const lay = $("#sessLayout"), phone = window.innerWidth < 760, vv = window.visualViewport;
    const chat = isShown() && phone && lay.classList.contains("phone-chat");
    const kb = chat && keyboardOpen(lay, vv);
    lay.classList.toggle("phone-kb", kb);
    kbDebug(lay, vv, kb);
    if (kb) { lay.style.top = vv.offsetTop + "px"; lay.style.height = vv.height + "px"; return; }
    lay.style.removeProperty("top");
    if (!isShown() || (phone && !chat)) return lay.style.removeProperty("height");
    const h = phone && vv ? vv.height : window.innerHeight;
    lay.style.height = Math.max(320, h - Math.max(0, lay.getBoundingClientRect().top)) + "px";
  }
  let fitting = 0;
  const fitSoon = () => { cancelAnimationFrame(fitting); fitting = requestAnimationFrame(fit); };
  // The keyboard opens or closes in about 300 ms, and its suggestion bar can come later: fit again until it settles.
  const fitAfter = () => { fitSoon(); for (const ms of [100, 350, 700, 1200]) setTimeout(fit, ms); };
  window.addEventListener("resize", fitSoon);
  window.addEventListener("scroll", () => { if ($("#sessLayout").classList.contains("phone-kb")) fitSoon(); }, { passive: true });
  // ?kbdebug=1 (remembered; ?kbdebug=0 ends it) shows the numbers of the fit above the message box, so a screenshot
  // from a phone says what the page measured (#4910).
  const KBDEBUG = "river.kbdebug";
  { const d = new URLSearchParams(location.search).get("kbdebug"); if (d != null) store(KBDEBUG, d === "1" ? "1" : ""); }
  function kbDebug(lay, vv, kb) {
    if (store(KBDEBUG) !== "1") return;
    let el = $("#kbDebug");
    if (!el) { el = document.createElement("div"); el.id = "kbDebug"; el.className = "muted"; el.style.cssText = "font: var(--fs-xs) ui-monospace, monospace; white-space: pre-wrap";
      $("#sessMain .chat-foot").prepend(el); }
    const r = lay.getBoundingClientRect(), a = document.activeElement;
    el.textContent = `kb ${kb} focus ${a ? a.tagName + (a.id ? "#" + a.id : "") : "-"} inner ${innerWidth}x${innerHeight} client ${document.documentElement.clientHeight}`
      + ` full ${full.h} vv ${vv ? Math.round(vv.height) + " @" + Math.round(vv.offsetTop) : "-"} scrollY ${Math.round(scrollY)} box ${Math.round(r.top)}+${Math.round(r.height)}`;
  }
  if (window.visualViewport) { window.visualViewport.addEventListener("resize", fitSoon); window.visualViewport.addEventListener("scroll", fitSoon); }
  $("#sessLayout").addEventListener("focusin", fitAfter);
  $("#sessLayout").addEventListener("focusout", fitAfter);
  const phoneChat = (yes) => { $("#sessLayout").classList.toggle("phone-chat", !!yes); headMore(false); fit(); };
  $("#sessBack").addEventListener("click", () => { phoneChat(false); window.scrollTo(0, 0); });
  for (const b of ["#youBack", "#statusBack", "#artsBack"]) $(b).addEventListener("click", () => { phoneChat(false); window.scrollTo(0, 0); });
  // The ⋯ of the session head (#4903; at every width since #5115): a card under it at the right with Open session,
  // Give work, Settings, the links to Plan and End (on a phone also the item). A choice, Esc, or a click outside closes it.
  const more = makeMenu($("#sessMore"), main, { cls: "head-more", onChange: () => fit(),
    closeOn: [$("#sessMenu")], outside: $("#sessMenu") });
  const headMore = (open) => more.set(open);

  async function refresh() {
    if (document.hidden || !isShown()) return;
    // A refresh asked for while one reads (an action's, after End) reads again after it: the read in flight
    // began before the action, so it shows the old rows (#5087).
    if (busy) { again = true; return; }
    busy = true; again = false;
    try {
      await loadAgents();  // once: the rows ask it about each session's agent
      const j = await getJson("api/sessions");
      list = j.sessions; listRead = true;
      proposals = j.proposals || [];
      push = j.notify_status || "off";
      forgetDrafts(list);
      draw();
    } catch (e) {
      side.innerHTML = `<div class="load-err" role="alert">Could not read the sessions (${esc(e.message)}); the page asks again.</div>`;
      delete side.dataset.empty;
    } finally { busy = false; if (again) refresh(); }
  }

  // The state in the head of the opened session: its row's (#5364), drawn after each read of the list or of the session.
  function drawState() {
    if (!agent || !lastRead || lastRead.agent !== agent) return;
    const row = list.find(x => x.name === agent), st = headState(row, lastRead, listRead);
    putHtml($("#sessState"), provTag(lastRead.provider) + stateDot(st) + (row ? shellChip(row) : ""));
    const s = { ...(row || {}), provider: lastRead.provider, running: !!row, terminal: lastRead.terminal };
    // Stop and Send now are in the message bar (#5587): the square while the box is empty, the bolt with text.
    $("#sessBar").classList.toggle("can-now", canStop(s, st.state) && stopKeys(s).length > 0);
  }

  // The rows from the list as it is now: refresh after each read, and a pin at once (#5400).
  function draw() {
    // At load: the session chosen last, else the first. A session that ended gives way to the first.
    if (!pane && (!agent || (!pinned && !list.some(s => s.name === agent)))) {
      const want = store(PICKED);
      pick((list.find(s => s.name === want) || list.find(s => s.role === "manager") || list[0] || {}).name || null, false);
    }
    renderList(mgr, list.filter(x => x.role === "manager"), agent, NO_MANAGER);
    // The experts in their own group under the manager (#4955): sessions the person talks with for days.
    const ex = list.filter(x => x.role === "expert");
    expertGroup.classList.toggle("hidden", !ex.length);
    renderList(experts, ex, agent, "");
    // The person's assistant under the You row (#5110), not among the workers.
    renderList(assist, list.filter(x => x.role === "assistant"), agent, "");
    // Pinned workers first, each group in today's order (#5007; sort is stable).
    const workers = list.filter(x => !["manager", "expert", "assistant"].includes(x.role));
    workers.sort((a, b) => isPinned(b) - isPinned(a));
    renderList(side, workers, agent,
      `<div class="muted">No other agent session runs now. Start one with New session${queueAnywhere(S)
        ? ", or Start an agent for the next ready item" : ""}.</div>`);
    drawStatus();
    drawState();
    proposeCard();
    fit();
    autoWait();
  }

  // The Status row of the list and the Status view (#5852), from the same read as the rows. The view draws only while
  // it shows; its HTML goes in only when it changed, so a row keeps its hover and focus.
  function drawStatus() {
    putHtml($("#sessStatusRow"), statusRowHtml(list, pane === "status"));
    if (pane === "status") putHtml($("#statusList"), statusHtml(list, push));
    document.querySelectorAll("#sessStatusRow .sago, #statusList .sago").forEach(x => { x.textContent = x.dataset.at ? ago(x.dataset.at) : ""; });
  }
  // The person has this session's chat open while its turn has ended (#5852): it is seen, and moves from Done to Idle.
  // Once for each turn end (its row's time), and only while the page shows.
  function markSeen(s) {
    if (!s || s.status !== "done" || document.hidden || !isShown()) return;
    const at = Date.parse(s.at || "") || 0;
    if (seenAt[s.name] === at) return;
    seenAt[s.name] = at;
    s.status = "idle";
    drawStatus();
    fetch("api/action", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ op: "session_seen", args: { agent: s.name }, actor: actor() }) }).catch(() => {});
  }
  // Push of one state on or off: the setting notify_status for every project.
  async function setPush(key) {
    const value = togglePush(push, key);
    push = value; drawStatus();
    try {
      await act("config_set", { key: "notify_status", value });
      toast(value === "off" ? "No push notices for session states" : "Push notices for: " + value.replace(/_/g, " ").replace(/,/g, ", "));
    } catch (_) { /* act showed why */ }
    await refresh();
  }

  // Pin or unpin a worker (#5007), from its row or the ⋯ menu (#5400). The rows and the menu change at once, not at
  // the next read of the sessions (about 5 s later); a refused pin reads them again.
  async function setPin(name, on) {
    if (!actor()) return toast("Choose your name in 'You are' first", true);
    const s = list.find(x => x.name === name), me = actor();
    if (s) {
      s.pinned_by = on ? [...new Set([...(s.pinned_by || []), me])] : (s.pinned_by || []).filter(x => x !== me);
      draw();
    }
    try { await act("session_pin", { agent: name, on }); } catch (_) { /* act showed why */ }
    await refresh();
    if (name === agent) poll();  // the ⋯ menu says Pin or Unpin again
  }

  async function poll() {
    const mine = gen;
    clearTimeout(timer);
    if (!agent) return;
    if (document.hidden || !isShown()) { timer = setTimeout(poll, 3000); return; }
    try {
      await loadAgents();  // the head asks it about the session's agent
      const r = await fetch(`api/session?agent=${encodeURIComponent(agent)}${cursor != null ? "&cursor=" + cursor : ""}`);
      const j = await r.json();
      if (mine !== gen) return;
      if (!r.ok) { note(j.error || "failed"); timer = setTimeout(poll, 10000); return; }
      // At the end: the last line shows. The room under a box with more lines (--cmp-over, #5111) does not count.
      const over = parseFloat(getComputedStyle($("#sessMain")).getPropertyValue("--cmp-over")) || 0;
      const first = cursor == null, atEnd = body.scrollHeight - over - body.scrollTop - body.clientHeight < 60;
      if (first) { if (j.entries.length) col.replaceChildren(); else note(j.note || "No lines yet."); }
      else if (j.entries.length) col.querySelector(".sl-none")?.remove();
      if (first) { older = j.older || null; readingOlder = false; }
      if (first && !older && j.entries.length) prependEntries(col, j.entries, j.refs, true);
      else appendEntries(col, j.entries, j.refs);
      checkPaths(col, agent);  // what each path means for this session; a refused one says why (#4689)
      agePending(col);
      drawPend(j.pending || []);
      cursor = j.cursor;
      const s = list.find(x => x.name === agent) || {};
      markSeen(list.find(x => x.name === agent));
      lastRead = { agent, provider: j.provider, state: j.state, state_label: j.state_label, terminal: j.terminal };
      drawState();
      waitLine(j);
      lastCache = j.cache || null;  // the expert's prompt cache (#5023): the header says it, and a send checks it
      const itemHtml = s.name ? headerItem(s) : "";
      putHtml($("#sessItem"), itemHtml + cacheBadge(j.cache) + contextMeter(j.context));
      putHtml($("#sessMenuItem"), itemHtml);
      putHtml($("#sessPlan"), s.name ? planLinks(s) : "");
      $("#sessName").textContent = s.title || "";
      barFor(j.terminal, list.some(x => x.name === agent) ? s : { ended: true });
      dict.showFor(list.some(x => x.name === agent) && !!j.terminal);
      putHtml($("#sessOpen"), headerOpen({ ...s, running: list.some(x => x.name === agent), session_url: j.session_url,
                                               terminal: j.terminal, provider: j.provider,
                                               state: headState(list.find(x => x.name === agent), lastRead, listRead).state }));
      renderGive(agent);
      // The first read shows the end; later new lines are followed. With no new line the chat stays where it is, so
      // a poll while the person types moves nothing (#5111).
      if (first || (atEnd && j.entries.length)) body.scrollTop = body.scrollHeight;
      if (first) moreAbove();
    } catch (e) { /* the next poll tries again */ }
    if (mine === gen && agent) timer = setTimeout(poll, 2000);
  }

  // The You view and the Artifacts view (#5102): in place of a session's chat. Their row of the list shows as chosen;
  // no session polls.
  function markPane() {
    markYou(pane === "you");
    $("#sessArtsRow .srow").classList.toggle("on", pane === "arts");
    if (pane !== "status") closeReply();
    drawStatus();
  }
  // Scrolling up reads the earlier lines (#5120), a page at a time, across compactions and into the transcript of the
  // session it was resumed from; the lines in sight stay where they are.
  function moreAbove() { if (older && !readingOlder && body.scrollTop < body.clientHeight) readOlder(); }
  async function readOlder() {
    const mine = gen, at = older;
    readingOlder = true;
    try {
      const r = await fetch(`api/session?agent=${encodeURIComponent(agent)}&file=${encodeURIComponent(at.file)}`
                            + (at.before != null ? `&before=${at.before}` : ""));
      const j = await r.json();
      if (mine !== gen) return;
      if (!r.ok) { older = null; return; }
      const h = body.scrollHeight, top = body.scrollTop;
      older = j.older || null;
      prependEntries(col, j.entries, j.refs, !older);
      checkPaths(col, agent);
      body.scrollTop = top + body.scrollHeight - h;
    } catch (e) { return; } finally { if (mine === gen) readingOlder = false; }
    if (mine === gen) moreAbove();
  }
  body.addEventListener("scroll", moreAbove, { passive: true });
  function showPane(kind, byPerson = true) {
    if (pane !== kind) {
      if (pane) $(PANES[pane]).classList.add("hidden");
      pane = kind; dict.stop(); agent = null; cursor = null; gen++; clearTimeout(timer); pinned = true;
      main.classList.add("hidden"); $(PANES[kind]).classList.remove("hidden");
      closeWaiting();  // the ! dialog of a session the page showed before (at load, the session chosen last)
      bar.querySelectorAll("[data-session]").forEach(r => r.classList.remove("on"));
      panelFor(null);
    }
    markPane();
    phoneChat(true);
    onPane(kind, byPerson);
  }
  function pick(name, remember = true) {
    if (pane && name) { $(PANES[pane]).classList.add("hidden"); pane = null; markPane(); }
    if (remember && name) store(PICKED, name);
    if (remember || !name) phoneChat(!!name);  // a session the person picks shows its chat (on a phone, #4885)
    if (name === agent) return;
    if (!agent && name) phoneChat(true);  // the first one at load
    if (name !== agent) dict.stop();
    saveDraft();  // the session it leaves keeps its text, cursor and files (#5112)
    col.classList.toggle("hide-tools", !viewOf(name).showTools);  // #4635
    agent = name; cursor = null; gen++; pinned = remember; lastCache = null;
    onPick(name, remember);  // the page's address (#4653); the panel then names its tab
    panelFor(name);  // its own side panel tabs (#4632)
    main.classList.toggle("hidden", !name);
    $("#sessTitle").textContent = name || "";
    $("#sessName").textContent = (list.find(x => x.name === name) || {}).title || "";  // what it works on (#4695)
    for (const id of ["#sessState", "#sessItem", "#sessMenuItem", "#sessOpen", "#sessPlan"]) putHtml($(id), "");
    $("#sessBar").classList.remove("can-now");  // the next read of the session says whether it can stop
    drawPend([]);
    renderGive(name);
    loadPrompts(name);
    loadDraft(name);
    barFor(null);
    note("Reading the transcript…");
    waitLine({});
    proposeCard();
    bar.querySelectorAll("[data-session]").forEach(r => r.classList.toggle("on", r.dataset.session === name));
    poll();
  }

  document.addEventListener("maxpm-view", (e) => { if (e.detail.agent === agent) col.classList.toggle("hide-tools", !viewOf(agent).showTools); });

  // The manager proposes an expert for a long talk (#4990): a card in its chat, above the message box. Continue in
  // an expert starts the expert with the talk so far and opens its chat; Dismiss keeps the talk with the manager.
  const proposeBox = $("#sessPropose");
  function proposeCard() {
    const p = proposals.find(x => x.manager === agent);
    proposeBox.classList.toggle("hidden", !p);
    if (!p) { proposeBox.innerHTML = ""; delete proposeBox.dataset.id; return; }
    if (proposeBox.dataset.id === String(p.id)) return;  // a refresh keeps the card (and a click in progress)
    proposeBox.dataset.id = String(p.id);
    proposeBox.innerHTML = `<div class="muted">${esc(p.manager)} proposes to continue this talk in an expert</div>
      <div class="sp-topic">${esc(p.topic)}</div>
      <div class="sp-meta muted">Expert ${esc(p.name)} · ${esc(p.model)}${p.model_why ? ` (${esc(p.model_why)})` : ""}${p.effort ? " · effort " + esc(p.effort) : ""}</div>
      <div class="sp-acts">${button("Continue in an expert", { kind: "primary", data: { "propose-go": p.id } })}
      ${button("Dismiss", { data: { "propose-no": p.id } })}</div>`;
  }
  proposeBox.addEventListener("click", async (e) => {
    const go = e.target.closest("[data-propose-go]"), no = e.target.closest("[data-propose-no]");
    if (!go && !no) return;
    const btns = proposeBox.querySelectorAll("button");
    btns.forEach(b => (b.disabled = true));
    try {
      if (no) {
        await act("expert_dismiss", { id: no.dataset.proposeNo });
        proposals = proposals.filter(x => String(x.id) !== no.dataset.proposeNo);
        proposeCard();
        return;
      }
      go.textContent = "Starting the expert…";
      const res = await act("expert_accept", { id: go.dataset.proposeGo });
      proposals = proposals.filter(x => String(x.id) !== go.dataset.proposeGo);
      toast(`The expert ${res.session_name} starts on ${res.model}; its chat opens here.`);
      if (res.session_name) pick(res.session_name);
    } catch (err) {  // act showed the error; the card stays, so the person can try again
      btns.forEach(b => (b.disabled = false));
      if (go) go.textContent = "Continue in an expert";
    }
  });

  // A session that waits on the person says so above the composer, with a link to the ! dialog that answers it. A
  // prompt that runs nothing (no tool, no command) is a menu: a slash command's (#5220), not a permission prompt.
  function waitLine(j) {
    const w = $("#sessWait"), all = (list.find(x => x.name === agent) || {}).waiting || [], waits = all.map(x => x.kind);
    const p = all.find(x => x.kind === "prompt"), menu = p && !p.elsewhere && !p.tool && !p.command;
    // A prompt in the terminal is a permission prompt or a question of the agent (Hermes's clarify tool, #5253).
    const what = menu ? "It shows a menu in its terminal."
      : waits.includes("prompt") || j.state === "prompt" ? "It waits for your answer at a prompt in its terminal."
      : waits.includes("question") || j.state === "answer" ? "It waits for your answer to its question." : "";
    w.innerHTML = what ? `${esc(what)} <span class="link" data-wait-open="1" tabindex="0" role="button">${menu ? "Choose" : "Answer it"}</span>` : "";
    w.classList.toggle("hidden", !what);
  }
  // What may open the dialog without a click on 'Answer it': a prompt, or a question younger than question_popup_max
  // (#5001: an old question stays on the card and behind 'Answer it', but does not pop up).
  const popsUp = (s) => (s.waiting || []).filter(w => !w.old);
  const showWaiting = (s) => { for (const w of s.waiting || []) seenWait.add(s.name + "\x1f" + waitKey(w)); openWaiting(s, { onSent: sentTurn }); };
  $("#sessWait").addEventListener("click", (e) => {
    if (!e.target.closest("[data-wait-open]")) return;
    const s = list.find(x => x.name === agent);
    if (s && (s.waiting || []).length) showWaiting(s);
  });
  $("#sessWait").addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.closest("[data-wait-open]")) e.target.click(); });
  // The open session comes to a prompt or asks a question: the ! dialog opens by itself (#4663), once for each
  // prompt (its sig) and question (its id), so Dismiss does not bring the same one back; a new one opens it again.
  // Not while the tab is hidden or another dialog is open: the next refresh after that opens it.
  const seenWait = new Set();
  function waitKey(w) { return w.kind === "question" ? "q" + w.id : "p" + (w.sig || w.question || ""); }
  function autoWait() {
    if (!agent || document.hidden || !isShown()) return;
    const s = list.find(x => x.name === agent);
    if (!s || !popsUp(s).some(w => !seenWait.has(s.name + "\x1f" + waitKey(w)))) return;
    if (document.querySelector('#setup:not(.hidden), [id$="Dlg"]:not(.hidden)')) return;
    showWaiting(s);
  }
  // The composer (components/composer.js, #5078) grows with the text; Enter sends, Shift+Enter starts a new line.
  $("#sessBar").innerHTML = composerHtml("sess", { placeholder: "Message", label: "Message for the session's terminal",
    attachTitle: "Attach files (up to 5, 10 MB each)", sendTitle: "Send the message and files to the session. Shift+Enter starts a new line.",
    stopTitle: "Stop its turn" });
  const text = $("#sessText");
  // The box grows with its text, up to a third of the window, upward over the chat (#5111): the chat keeps its frame
  // and its scroll, so nothing in it moves while the person types. A hidden copy of the box measures the text, so the
  // box never shrinks to one line in place (that made the chat shorter for a moment, and the browser scrolled it).
  // --cmp-over: how far the box reaches over the chat; the chat gets as much room at its end, to scroll to.
  const measure = text.cloneNode(false);
  measure.removeAttribute("id"); measure.tabIndex = -1; measure.setAttribute("aria-hidden", "true");
  measure.classList.add("cmp-measure");
  text.after(measure);
  let oneLine = null;
  // The pill around the box has a least height (a phone's is taller than one line of text): it grows only by what the
  // box adds above that, and the box reaches over the chat by as much.
  const pillHeight = (h) => {
    const cs = getComputedStyle(text.parentElement);
    const edge = ["paddingTop", "paddingBottom", "borderTopWidth", "borderBottomWidth"].reduce((n, k) => n + (parseFloat(cs[k]) || 0), 0);
    return Math.max(parseFloat(cs.minHeight) || 0, h + edge);
  };
  const grow = () => {
    measure.style.width = text.clientWidth + "px";
    measure.value = text.value || "x";
    const h = Math.min(measure.scrollHeight, window.innerHeight / 3);
    if (oneLine == null || !text.value) { measure.value = "x"; oneLine = measure.scrollHeight; }
    text.style.height = h + "px";
    $("#sessMain").style.setProperty("--cmp-over", Math.max(0, pillHeight(h) - pillHeight(oneLine)) + "px");
  };
  // Empty messages send nothing. Files being read reserve a slot, and Send waits until they are ready.
  const FILE_MAX = 10 * 1024 * 1024, FILE_COUNT = 5;
  let files = [], sending = false;
  const empty = () => !text.value.trim() && !files.length;
  const sendState = () => {
    $("#sessSend").disabled = text.disabled || sending || empty() || files.some(f => f.reading);
    composerState($("#sessBar"), !empty());
    $("#sessAttach").disabled = $("#sessPickFiles").disabled = text.disabled || sending;
  };
  // Dictation (#4626): the microphone puts speech into the box as text.
  const dict = makeDictation({ button: $("#sessMic"), input: text, status: $("#sessDictate"), changed: () => { grow(); sendState(); } });
  text.addEventListener("input", () => { grow(); sendState(); saveDraft(); });
  text.addEventListener("blur", saveDraft);  // its cursor
  window.addEventListener("pagehide", saveDraft);
  // The "/" list (#5186): the slash commands of the session's agent; while it is open it takes Up, Down, Tab and Esc.
  const slash = makeSlashList(text, text.parentElement, {
    key: () => agent,
    load: () => agent ? fetch("api/session-commands?agent=" + encodeURIComponent(agent)).then(r => r.ok ? r.json() : null)
      .then(j => (j && j.commands) || []) : Promise.resolve([]),
    onChange: () => { grow(); sendState(); saveDraft(); },
  });
  text.addEventListener("keydown", (e) => {
    if (slash.keydown(e)) { e.preventDefault(); e.stopPropagation(); return; }
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault(); slash.close();
      if (!empty()) $("#sessBar").requestSubmit();
    }
    else if ((e.key === "ArrowUp" || e.key === "ArrowDown") && recall(e.key === "ArrowUp" ? 1 : -1, e)) e.preventDefault();
  });
  // The earlier prompts (#4855), as in a shell: Up brings back the prompts sent to this session from this box,
  // newest first, one at a time; Down goes forward again, and past the newest one the box shows the text the person
  // typed before the first Up (the draft), or nothing. Up acts only with the caret on the box's first visual line and
  // Down only on its last visual line (a wrapped line counts as several), else the caret moves as in any text box. The list comes from the server (every device,
  // and the relay); files attached are not part of it.
  let prompts = [], recalled = -1, draft = "";
  function loadPrompts(name, again = false) {  // again: the box got the focus; a list in use stays as it is
    if (!again) { prompts = []; recalled = -1; draft = ""; }
    if (!name) return;
    fetch("api/session-prompts?agent=" + encodeURIComponent(name)).then(r => r.ok ? r.json() : null).then(j => {
      if (j && name === agent && recalled === -1) prompts = j.prompts || [];
    }).catch(() => {});
  }
  // Is the caret at `at` on the box's first visual line (up) or its last (down)? A long line that wraps is several
  // lines (#5146). The hidden copy measures the text up to the caret, with the rest of the caret's word: that word
  // wraps with the caret, so the copy wraps where the box does.
  const lines = (s) => { measure.style.width = text.clientWidth + "px"; measure.value = s || "x"; return measure.scrollHeight; };
  const onEdge = (up, at) => {
    const v = text.value, head = v.slice(0, at) + v.slice(at).match(/^\S*/)[0];
    return up ? lines(head) <= lines("x") : lines(head) >= lines(v);
  };
  function recall(step, e) {
    if (e.isComposing || e.shiftKey || e.altKey || e.ctrlKey || e.metaKey || text.disabled) return false;
    const v = text.value, at = step > 0 ? text.selectionStart : text.selectionEnd;
    if (!onEdge(step > 0, at)) return false;  // not the edge line: the caret moves as in any text box
    const to = recalled + step;
    if (to < -1 || to >= prompts.length) return false;
    if (recalled === -1) draft = v;
    recalled = to;
    text.value = to === -1 ? draft : prompts[to];
    // Up leaves the caret on the first line, so the next Up goes on: at the end of the first line, or at the start
    // when that line wraps. Down leaves it at the end, on the last line.
    grow();
    let end = step > 0 && text.value.includes("\n") ? text.value.indexOf("\n") : text.value.length;
    if (step > 0 && !onEdge(true, end)) end = 0;
    text.setSelectionRange(end, end);
    sendState(); saveDraft();
    return true;
  }
  const sentPrompt = (to, t) => {
    t = (t || "").trim();
    if (to !== agent) return;
    if (t && prompts[0] !== t) prompts.unshift(t);
    recalled = -1; draft = "";
  };
  text.addEventListener("focus", () => { if (agent && recalled === -1) loadPrompts(agent, true); });
  function renderFiles() {
    const box = $("#sessFiles");
    box.replaceChildren(...files.map((f, i) => {
      const d = document.createElement("div"); d.className = "sess-file";
      if (f.url) {
        const img = document.createElement("img"); img.src = f.url; img.alt = f.name;
        d.append(img);
      }
      const label = document.createElement("span"); label.textContent = f.name; label.title = f.name;
      const x = buttonEl("✕", { kind: "icon", label: `Remove ${f.name}`, title: `Remove ${f.name}`, data: { rm: i },
                                disabled: sending });
      d.append(label, x); return d;
    }));
    box.classList.toggle("hidden", !files.length);
    sendState();
  }
  // Each session's draft (#5112, mark: 'we need to maintain what is in the input for each session when we switch
  // session as well as when we reload'): the text of its box, the cursor, and the files attached but not sent, in
  // this browser (localStorage, by the session's name). A file is saved on the server when it is attached
  // (session_upload), so the draft holds its id, not its bytes. Send empties the draft; a session that ended loses it.
  const DRAFT = "river.draft.";
  function readDraft(name) { try { return JSON.parse(localStorage.getItem(DRAFT + name) || "null"); } catch (e) { return null; } }
  function saveDraft() {
    if (!agent) return;
    const d = { text: text.value, start: text.selectionStart, end: text.selectionEnd,
                files: files.filter(f => f.id).map(f => ({ name: f.name, type: f.type, id: f.id })) };
    try {
      if (!d.text && !d.files.length) localStorage.removeItem(DRAFT + agent);
      else localStorage.setItem(DRAFT + agent, JSON.stringify(d));
    } catch (e) { /* no storage here: the draft lives until the page goes */ }
  }
  function loadDraft(name) {
    files.forEach(f => { if (f.url && f.url.startsWith("blob:")) URL.revokeObjectURL(f.url); });
    const d = (name && readDraft(name)) || {};
    text.value = d.text || "";
    files = (d.files || []).map(f => ({ ...f, reading: false, url: /^image\/(png|jpeg|gif|webp)$/.test(f.type || "")
      ? `api/upload/${encodeURIComponent(name)}/${encodeURIComponent(f.id)}` : null }));
    renderFiles(); grow();
    if (d.start != null) { try { text.setSelectionRange(d.start, d.end ?? d.start); } catch (e) { /* hidden */ } }
  }
  // A saved file taken out of the box goes from the server too.
  const dropSaved = (owner, id) => typeAction("session_upload_drop", { agent: owner, id }).catch(() => {});
  function forgetDrafts(running) {  // the sessions that ended
    try {
      for (const k of Object.keys(localStorage)) {
        if (k.startsWith(DRAFT) && !running.some(s => s.name === k.slice(DRAFT.length))) localStorage.removeItem(k);
      }
    } catch (e) { /* no storage */ }
  }
  function clearFiles() {
    files.forEach(f => { if (f.url && f.url.startsWith("blob:")) URL.revokeObjectURL(f.url); });
    files = []; renderFiles();
  }
  function addFile(file) {
    if (!file || sending) return;
    if (text.disabled) return toast("This session runs outside tmux, so the page cannot send it a file", true);
    if (file.size > FILE_MAX) return toast("A file is at most 10 MB", true);
    if (files.length >= FILE_COUNT) return toast(`At most ${FILE_COUNT} files go with one message`, true);
    const f = { name: file.name || "file", type: file.type, reading: true,
      url: /^image\/(png|jpeg|gif|webp)$/.test(file.type) ? URL.createObjectURL(file) : null };
    files.push(f); renderFiles();
    const r = new FileReader(), owner = agent;
    // Saved on the server at once: the draft keeps its id through a switch and a reload (#5112).
    r.onload = () => {
      if (!files.includes(f)) return;
      typeAction("session_upload", { agent: owner, name: f.name, data: String(r.result).split(",")[1] }).then((u) => {
        if (!files.includes(f)) return dropSaved(owner, u.id);  // taken out while it was saved
        f.id = u.id; f.reading = false; renderFiles(); saveDraft();
      }, (e) => {
        if (!files.includes(f)) return;
        files.splice(files.indexOf(f), 1); if (f.url) URL.revokeObjectURL(f.url);
        renderFiles(); toast(`Could not attach ${f.name}: ${e.message}`, true);
      });
    };
    r.onerror = () => {
      if (!files.includes(f)) return;
      files.splice(files.indexOf(f), 1); if (f.url) URL.revokeObjectURL(f.url);
      renderFiles(); toast(`Could not read ${f.name}`, true);
    };
    r.readAsDataURL(file);
  }
  $("#sessFiles").addEventListener("click", (e) => {
    const b = e.target.closest("[data-rm]"); if (!b || sending) return;
    const [f] = files.splice(+b.dataset.rm, 1); if (f.url && f.url.startsWith("blob:")) URL.revokeObjectURL(f.url);
    if (f.id) dropSaved(agent, f.id);
    renderFiles(); saveDraft(); text.focus();
  });
  $("#sessAttach").addEventListener("click", () => $("#sessPickFiles").click());
  $("#sessPickFiles").addEventListener("change", (e) => { [...e.target.files].forEach(addFile); e.target.value = ""; });
  text.addEventListener("paste", (e) => {
    const incoming = [...(e.clipboardData?.files || [])];
    if (!incoming.length) return;
    e.preventDefault(); incoming.forEach(addFile);
  });
  const foot = $("#sessBar").closest(".chat-foot");
  foot.addEventListener("dragover", (e) => { if (e.dataTransfer?.types.includes("Files")) { e.preventDefault(); foot.classList.add("drop"); } });
  foot.addEventListener("dragleave", () => foot.classList.remove("drop"));
  foot.addEventListener("drop", (e) => {
    if (!e.dataTransfer?.files.length) return;
    e.preventDefault(); foot.classList.remove("drop");
    [...e.dataTransfer.files].forEach(addFile);
  });
  // A turn the person sent from the ! dialog shows in the chat at once.
  function sentTurn(text, track = false) {  // track: a message the transcript shows later (not a key such as a pick)
    col.querySelector(".sl-none")?.remove();
    appendEntries(col, [{ role: "user", sent: true, track: track && !!text, text: text || "(Enter)" }]);
    body.scrollTop = body.scrollHeight;
  }
  // The text bar is always there. With no tmux pane of the session on this computer it is off, and says why.
  function barFor(pane, s = {}) {
    const input = $("#sessText"), why = $("#sessBarWhy");
    const off = !pane;
    input.disabled = off;
    sendState();
    why.classList.toggle("hidden", !off || pane === null);
    const elsewhere = /^https:\/\//.test(s.session_url || "") ? "type in it with Open, or in its terminal" : "type in its terminal";
    if (s.ended) { why.textContent = off ? "This session is not running: its chat shows read only." : ""; return; }
    const ad = s.adopt || { when: "never", why: "" };
    why.textContent = !off ? "" : ad.when === "now" ? `This session runs outside tmux. MaximizePM moves it into a tmux pane now; until then, ${elsewhere}.`
      : ad.when === "later" ? `This session runs outside tmux. ${ad.why} (tmux_adopt). Until then, ${elsewhere}.`
      : `This session runs outside tmux, so the page cannot type into it${ad.why ? ` (${ad.why})` : ""}: ${elsewhere}.`;
  }
  // The text bar: the person's text goes to the session's tmux pane. It shows at once as the person's turn; the
  // transcript has it too when the session reads it as a prompt.
  $("#sessBar").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = $("#sessText"), btn = $("#sessSend"), to = agent;
    // A command whose view the Usage panel gives (#5224: /usage, /context, /cost, /status, /stats with no argument):
    // the panel only reads, so it opens also for a session that the page cannot type into; nothing goes to it.
    if (to && !sending && !files.length && /^\/\S+\s*$/.test(input.value)) {
      const c = await slash.lookup(input.value);
      if (c && c.page === "usage" && !c.args && to === agent) {
        openUsage(to);
        input.value = ""; slash.close(); grow(); sendState(); saveDraft();
        return;
      }
    }
    if (!to || sending || btn.disabled || empty()) return;
    // Enter or the send arrow while the microphone listens (#5118): it stops first, the final text comes in (at
    // most 1.5 s), and then the box goes once; text after it never comes back into the box to be sent again.
    if (dict.listening()) {
      sending = true; renderFiles();
      try { await dict.finish(1500); } finally { sending = false; renderFiles(); }
      if (to !== agent || empty()) return;
    }
    // A cold expert with a large context (#5023): the message would read all of it again at the write price.
    const ex = list.find(x => x.name === to) || {}, cold = ex.role === "expert" ? lastCache || ex.cache : null;
    if (cold && cold.fresh) {
      const how = await askCold(to, cold);
      if (!how) return input.focus();
      if (how === "fresh") {
        try {
          const r = await act("expert_fresh", { name: to });
          toast(`${to} ended; the fresh expert ${r.session_name} starts on ${r.model} and reads the handoff. Your text stays here: send it to ${r.session_name} when it is up.`);
          const typed = input.value;
          await refresh();
          pick(r.session_name);
          input.value = typed;  // the new expert's box gets the text; the person sends it to the new expert
          grow(); sendState(); saveDraft();
        } catch (_) { /* act shows the error */ }
        return;
      }
    }
    // A command that the session's Settings does (#5220, mark 2026-11-11: /model, /effort with no argument): the
    // dialog opens at its row, and nothing goes to the terminal, where its picker would wait for keys.
    const cmd = files.length ? null : await slash.lookup(input.value);
    if (cmd && cmd.setting && !cmd.args && to === agent) {
      openSettings(to, { done: refresh, focus: cmd.setting });
      input.value = ""; slash.close(); grow(); sendState(); saveDraft();
      return;
    }
    sending = true; renderFiles();
    let ok = true;
    try {
      const sent = files, typed = input.value;
      // While the session runs a turn (the Stop square shows), maxpm serve holds the message and types it when the
      // turn has ended; it shows under the chat with its own Send now (#5587).
      const r = await typeAction("session_send", { agent: to, text: typed, hold: $("#sessBar").classList.contains("can-now"),
        ...(sent.length ? { files: sent.map(f => f.id ? { name: f.name, id: f.id } : { name: f.name, data: f.data }) } : {}) });
      if (to === agent && r.waiting) {
        drawPend([...waiting.filter(p => p.id !== r.waiting.id), r.waiting]);
        clearFiles(); sentPrompt(to, typed); body.scrollTop = body.scrollHeight;
        input.value = ""; slash.close(); grow(); saveDraft();
      } else if (to === agent) {
        col.querySelector(".sl-none")?.remove();
        // A slash command to an agent whose chat log does not record one (CHAT['commands'], #5253: Hermes) never
        // shows in the chat: the turn says "sent to its terminal" and does not wait to be taken.
        const ag = agentOf(ex.provider || ex.agent_type);
        const untracked = !!ag && ag.chat_commands === false && /^\//.test(String(r.text || "").trim());
        appendEntries(col, [{ role: "user", sent: true, track: !!r.text && !untracked, text: r.text || (sent.length ? "" : "(Enter)"),
                              command: r.command || null, terminal: to,
                              images: (r.images || []).map(p => p.split(/[\\/]/).slice(-2).join("/")),
                              files: (r.files || []).filter(p => !(r.images || []).includes(p)).map(p => ({
                                path: p.split(/[\\/]/).slice(-2).join("/"), name: p.split(/[\\/]/).pop().replace(/^\d{8}-\d{6}-[a-f0-9]{8}-/, "") })) }]);
        clearFiles();
        sentPrompt(to, typed);
        body.scrollTop = body.scrollHeight;
        input.value = "";
        slash.close();  // a send by the arrow, or a text the list was open for
        grow();
        saveDraft();  // sent: the draft is empty
      }
    } catch (err) { toast(err.message, true); ok = !/tmux pane/.test(err.message); }
    finally { sending = false; renderFiles(); if (!input.disabled) { sendState(); input.focus(); } if (!ok) barFor(false); }
  });
  // End (#5041): the confirm dialog says what happens; then the row shows 'ending' until the session is gone.
  async function endIt(name) {
    const s = list.find(x => x.name === name);
    if (!s) return toast(`${name} is not running`, true);
    const c = await endSession(s);
    if (!c) return;
    toast(c.how === "kill" ? `${name} ended now` : c.how === "expert"
      ? `${name} ended; ${(s.holds || []).length ? (c.done ? "its task closed" : "its task went back to the queue") : "its notes stay"}`
      : `${name} is asked to stop: it ends after it commits and releases its task`);
    await refresh();
    if (name === agent) poll();
  }
  // Stop (#5547): the agent's interrupt keys go to its pane; the head then says it is at its prompt. The square of the
  // message bar waits for the answer, so a second press is a second Stop (Codex: /stop, #5563), not a double click.
  async function stopIt(name) {
    headMore(false);
    let r;
    const sq = $("#sessStop");
    sq.disabled = true;
    try { r = await typeAction("session_interrupt", { agent: name }); } catch (err) { return toast(err.message, true); }
    finally { sq.disabled = false; }
    toast(r && r.command ? `${name}: ${r.command} sent, to ${r.did}`
      : `${name}: its turn stopped${r && r.again ? `. ${r.again}` : ""}`);
    await refresh();
    if (name === agent) poll();
  }
  $("#sessStop").addEventListener("click", () => { if (agent) stopIt(agent); });
  // Send now under a held message (#5587): its turn stops, and that message goes at once; the others keep waiting.
  $("#sessPend").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-now]"); if (!b || !agent) return;
    const name = agent;
    b.disabled = true;
    try { await typeAction("pending_send_now", { agent: name, id: +b.dataset.now }); }
    catch (err) { b.disabled = false; return toast(err.message, true); }
    toast(`${name}: its turn stopped; your message went at once`);
    drawPend(waiting.filter(p => p.id !== +b.dataset.now));
    await refresh();
    if (name === agent) poll();
  });
  $("#sessOpen").addEventListener("click", (e) => {
    const stp = e.target.closest("[data-stop]"); if (stp) return stopIt(stp.dataset.stop);
    const end = e.target.closest("[data-end]"); if (end) return endIt(end.dataset.end);
    const pn = e.target.closest("[data-pin-to]"); if (pn) return setPin(pn.dataset.pinName, pn.dataset.pinTo === "on");
    const b = e.target.closest("[data-term]"); if (b) openTerminal(b.dataset.term);
    const st = e.target.closest("[data-settings]"); if (st) openSettings(st.dataset.settings, { done: refresh });
  });
  bar.addEventListener("click", async (e) => {
    if (e.target.closest("[data-you]")) return showPane("you");
    if (e.target.closest("[data-status]")) return showPane("status");
    if (e.target.closest("[data-arts]")) return showPane("arts");
    const end = e.target.closest("[data-end]");
    if (end) { e.preventDefault(); return endIt(end.dataset.end); }  // End does not open the session
    const pin = e.target.closest("[data-pin]");
    if (pin) {  // the pin does not open the session
      e.preventDefault();
      return setPin(pin.dataset.pin, !pin.classList.contains("on"));
    }
    if (e.target.closest("[data-open], a")) return;  // an item link opens in the side panel
    const r = e.target.closest(".srow-main") && e.target.closest("[data-session]"); if (!r) return;
    pick(r.dataset.session);
    const s = list.find(x => x.name === r.dataset.session);
    if (s && popsUp(s).length) showWaiting(s);  // what waits for the person comes first; Dismiss shows the chat
  });
  bar.addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") && e.target.closest("[data-you], [data-status], [data-arts]")) {
      e.preventDefault();
      return showPane(e.target.closest("[data-you]") ? "you" : e.target.closest("[data-status]") ? "status" : "arts");
    }
    const r = (e.key === "Enter" || e.key === " ") && !e.target.closest("[data-pin], [data-end]") && e.target.closest(".srow-main")
      && e.target.closest("[data-session]");
    if (!r) return;
    e.preventDefault(); pick(r.dataset.session);
    const s = list.find(x => x.name === r.dataset.session);
    if (s && popsUp(s).length) showWaiting(s);
  });
  setInterval(refresh, 3000);
  // The Status view (#5852): a row opens its session, with what waits for the person first, as a row of the list does;
  // Push turns the notices of that state on or off.
  // Under a row (#5853): Answer opens what waits in its dialog; Esc goes to its tmux pane; Reply opens the sheet.
  const rForm = $("#stReplyForm"), byName = (n) => list.find(x => x.name === n);
  rForm.innerHTML = composerHtml("stR", { placeholder: "Message", label: "Message for the session", attachTitle: "Attach files", sendTitle: "Send" });
  const rText = $("#stRText"), rState = () => composerState(rForm, rText.value.trim());
  replyDict = makeDictation({ button: $("#stRMic"), input: rText, status: $("#stReplyDictate"), changed: rState });
  function openReply(name) {
    replyTo = name;
    const s = byName(name) || {};
    $("#stReplyName").textContent = s.title || name;
    $("#statusReply").classList.remove("hidden");
    replyDict.showFor(true);
    rState();
    rText.focus();
  }
  rText.addEventListener("input", rState);
  rText.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); rForm.requestSubmit(); } });
  rForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (replyDict.listening()) await replyDict.finish();
    const to = replyTo, text = rText.value.trim();
    if (!to || !text) return;
    const s = byName(to) || {};
    try {
      // While its turn runs, maxpm serve holds the message and types it when the turn has ended (#5587).
      const r = await typeAction("session_send", { agent: to, text, hold: s.status === "working" });
      toast(r && r.waiting ? `${s.title || to}: it gets it when its turn ends` : `Sent to ${s.title || to}`);
      rText.value = ""; rState(); closeReply();
    } catch (err) { return toast(err.message, true); }
    await refresh();
  });
  $("#stReplyClose").addEventListener("click", closeReply);
  $("#stReplyChat").addEventListener("click", () => { const n = replyTo; closeReply(); if (n) pick(n); });
  async function sendEsc(name) {
    try { await typeAction("terminal_keys", { agent: name, keys: [{ key: "Escape" }] }); } catch (err) { return toast(err.message, true); }
    toast(`Esc sent to ${(byName(name) || {}).title || name}`);
    await refresh();
  }
  $("#statusList").addEventListener("click", (e) => {
    const p = e.target.closest("[data-status-push]"); if (p) return setPush(p.dataset.statusPush);
    const an = e.target.closest("[data-st-answer]");
    if (an) { const s = byName(an.dataset.stAnswer); return s && showWaiting(s); }
    const es = e.target.closest("[data-st-esc]"); if (es) return sendEsc(es.dataset.stEsc);
    const rp = e.target.closest("[data-st-reply]"); if (rp) return openReply(rp.dataset.stReply);
    const r = e.target.closest("[data-status-go]"); if (!r) return;
    pick(r.dataset.statusGo);
    const s = list.find(x => x.name === r.dataset.statusGo);
    if (s && popsUp(s).length) showWaiting(s);
  });
  return { refresh: () => { refresh(); fit(); }, pick, showYou: (byPerson = true) => showPane("you", byPerson),
           showStatus: (byPerson = true) => showPane("status", byPerson),
           showArts: (byPerson = true) => showPane("arts", byPerson), pane: () => pane };
}
