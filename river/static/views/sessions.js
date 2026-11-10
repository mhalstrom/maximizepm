// The Sessions tab: a list of the running agent sessions (Claude Code and Codex) in a left bar, one row each with
// its agent, item and state, read again every few seconds while the tab shows; and the selected session on the
// right: its transcript, with new lines added as the session writes them (#4597, in place of the cards of #4592).
// The view shows the session as a chat (components/chat.js, #4604). A person types in the session through Open
// (its claude.ai/code link), or, for a session in a tmux pane, the composer below the chat (#4596): the text goes
// to the pane, then Enter. MaximizePM types nothing by itself.
// Server: GET /api/sessions, GET /api/session?agent=<name>&cursor=<byte>, and the op session_send (with the
// page's token: typeAction).
import { $, esc, ago, clip, store, toast, typeAction } from "../lib.js";
import { chip } from "../components/chip.js";
import { appendEntries, agePending, wireCopy, plainText } from "../components/chat.js";
import { openTerminal } from "../components/terminalDialog.js";
import { openWaiting } from "../components/waitDialog.js";
import { openSettings } from "../components/settingsDialog.js";
import { makeDictation } from "../components/dictate.js";
import { viewOf } from "../components/viewSettings.js";
import { openInPanel, openList, wirePanel, panelFor } from "../components/sidePanel.js";
import { checkPaths } from "../components/artLinks.js";

const STATE_CHIP = { working: "c-in_progress", prompt: "c-p0", answer: "c-human", idle: "c-waiting", stopping: "c-p",
                     starting: "c-waiting", ended: "c-blocked", expert: "c-p" };
const PICKED = "river.session";  // the session the person chose last

function stateChip(s) {
  return chip(STATE_CHIP[s.state] || "c-p", esc(s.state_label), s.state === "prompt"
    ? "A permission prompt waits in its terminal or session: answer it there" : null);
}

// The header of the chosen session (#4608): Open session (its claude.ai/code page; else its tmux pane in the
// terminal view; else off, with why), and the item it works on, which opens in the drawer.
function headerOpen(s) {
  const set = s.provider ? `<button class="btn" type="button" data-settings="${esc(s.name)}" title="The settings this session runs with, and what you can change">Settings</button> ` : "";
  return set + headerOpenBtn(s);
}
function headerOpenBtn(s) {
  if (/^https:\/\//.test(s.session_url || "")) return `<a class="btn" href="${esc(s.session_url)}" target="_blank" rel="noopener" title="Open this session on claude.ai/code">Open session</a>`;
  if (s.terminal) return `<button class="btn" type="button" data-term="${esc(s.name)}" title="Open its tmux pane in the terminal view">Open session</button>`;
  const why = s.agent_type === "codex" ? "A Codex session has no web link, and this one runs in no tmux pane here" : "This session has no Remote Control link and runs in no tmux pane here";
  return `<button class="btn" type="button" disabled title="${esc(why)}">Open session</button><span class="muted sess-noopen">${esc(why)}</span>`;
}
// The targets a session owns: entries are {name, owner_expires_at} (or a name), shown by name (#4686).
const ownNames = (s) => (s.owns || []).map(o => (o && o.name) || String(o));
function headerItem(s) {
  if (s.holds && s.holds.length) return s.holds.map(h => `<button class="btn sess-itembtn" type="button" data-open="${h.id}" title="Open #${h.id} in the side panel: its notes, what it waits on, and what it unblocks">`
    + `#${h.id} ${esc(clip(h.title, 110))}${h.status ? " " + chip("c-" + h.status, esc(h.status.replace("_", " "))) : ""}</button>`).join(" ");
  if (s.owns && s.owns.length) return `<span class="muted">No item · owns target ${ownNames(s).map(esc).join(", ")}</span>`;
  return `<span class="muted">No item</span>`;
}

// The chat provider of a session (#4627): a small label from the server's provider table.
function provTag(p) {
  return p ? `<span class="prov prov-${esc(p.id)}" title="Runs in ${esc(p.label)}">${esc(p.label)}</span>` : "";
}

// The ! badge: how many things wait for the person in this session (a prompt in its pane, open questions).
function waitBadge(s) {
  const n = (s.waiting || []).length;
  return n ? `<span class="wbadge" title="${n} thing(s) wait for you here: click to answer">! ${n}</span>` : "";
}

// One row of the left bar, in two parts (#4609): the left two thirds (name, state, ! badge, last line) opens the
// session on the right; the right third shows the item it works on and opens that item in the drawer. A session
// that waits at a prompt or for an answer stands out (class "needs").
export function sessionRow(s) {
  const last = s.last.length ? s.last[s.last.length - 1] : null;
  const lastText = last ? (last.role === "tool" ? last.text : plainText(last.text)) : "";
  const h = s.holds && s.holds[0];
  const item = h ? `<button class="srow-it" type="button" data-open="${h.id}" title="${esc(`Open item #${h.id} ${h.title}`)}"><b>#${h.id}</b><span>${esc(clip(h.title, 48))}</span></button>`
    : `<div class="srow-it none" title="${s.owns.length ? esc("Owns target " + ownNames(s).join(", ")) : "No item"}"><span class="muted">${s.owns.length ? "No item · " + esc(ownNames(s).join(", ")) : "No item"}</span></div>`;
  return `<div class="srow${["prompt", "answer"].includes(s.state) || (s.waiting || []).length ? " needs" : ""}" data-session="${esc(s.name)}">
    <div class="srow-main" tabindex="0" role="button" title="Open session ${esc(s.name)}">
      <div class="srow-h">${s.role === "manager" ? `<span class="mgr-tag">Manager</span>` : ""}${s.role === "expert" ? `<span class="mgr-tag expert-tag">Expert</span>` : ""}<b title="${esc(s.title || s.name)}">${esc(s.role === "manager" || s.role === "expert" ? s.name : s.title || s.name)}</b>${provTag(s.provider)}<span class="spacer"></span>${waitBadge(s)}${stateChip(s)}</div>
      ${s.role === "manager" ? "" : s.role === "expert"
        ? `<div class="sid expert-topic" title="${esc(`Its topic${s.model ? "; model " + s.model : ""}${s.talked_at ? "; last talk " + ago(s.talked_at) : ""}`)}">${esc(clip(s.topic || "", 120))}${s.model ? ` · ${esc(s.model)}` : ""}</div>`
        : `<div class="sid" title="Its agent id: maxpm --as, messages and commands use it">${esc(s.name)}</div>`}
      <div class="sub">${esc(clip(lastText, 90))}${s.at ? ` · <span class="sago" data-at="${esc(s.at)}">${ago(s.at)}</span>` : ""}</div>
    </div>
    ${item}
  </div>`;
}

// The manager's rows come first, apart from the other sessions (#4616); with no manager running, a row that starts
// one (the page's Start manager: the launch dialog, then the op start_manager).
const NO_MANAGER = `<div class="srow mgr-none"><div class="srow-main"><div class="srow-h"><span class="mgr-tag">Manager</span>
  <span class="muted">No manager running</span></div></div>
  <div class="srow-it"><button class="btn primary" type="button" data-start-manager="1" title="Opens the launch dialog; the agent starts with maxpm manage">Start manager</button></div></div>`;

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
  el.querySelectorAll(".sago").forEach(x => { x.textContent = ago(x.dataset.at); });
  el.querySelectorAll("[data-session]").forEach(r => r.classList.toggle("on", r.dataset.session === picked));
}

export function makeSessions({ isShown, onPick = () => {} }) {
  let list = [], busy = false;
  let agent = null, cursor = null, timer = null, gen = 0;
  let pinned = false;  // chosen by the person: it stays on screen when it is not running (read only, #4622)
  const side = $("#sessions"), mgr = $("#sessMgr"), bar = side.closest(".sess-side");
  const experts = $("#sessExpertRows"), expertGroup = $("#sessExperts");
  const body = $("#sessBody"), col = $("#sessCol"), main = $("#sessMain");
  wireCopy(col);
  wirePanel();
  // The chat stays in sight (#4685): in this tab an item opens in the side panel, not in the drawer (a #<id> in the
  // chat, the item of a session row or of the header, an item of the panel's lists), and Items and Artifacts open
  // there as lists.
  $("#tab-sessions").addEventListener("click", (e) => {
    const l = e.target.closest("[data-sp-open]"); if (l) return openList(l.dataset.spOpen);
    const o = e.target.closest("[data-open]");
    if (!o || !/^\d+$/.test(o.dataset.open) || e.metaKey || e.ctrlKey) return;
    e.preventDefault(); e.stopPropagation();
    openInPanel("item:" + o.dataset.open);
  });
  // A link or a file path in the chat opens in the side panel; Cmd or Ctrl+click keeps a new browser tab (#4619).
  col.addEventListener("click", (e) => {
    const f = e.target.closest("[data-file]");
    if (f) { e.preventDefault(); openInPanel(f.dataset.file); return; }
    const a = e.target.closest("a[href]");
    if (!a || a.hasAttribute("download") || e.metaKey || e.ctrlKey || e.shiftKey || !/^https?:\/\//i.test(a.getAttribute("href"))) return;
    e.preventDefault(); openInPanel(a.href);
  });
  col.addEventListener("keydown", (e) => {
    const f = e.key === "Enter" && e.target.closest("[data-file]"); if (f) { e.preventDefault(); openInPanel(f.dataset.file); }
    const o = e.key === "Enter" && e.target.closest("span[data-open]"); if (o) { e.preventDefault(); o.click(); }
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
    if (!el) { el = document.createElement("div"); el.id = "kbDebug"; el.className = "muted"; el.style.cssText = "font: 11px ui-monospace, monospace; white-space: pre-wrap";
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
  // The ⋯ of the session head on a phone (#4903): it shows the item line, Settings, Open session, Items and Artifacts
  // under the one line of the head; a tap on one of them closes it again.
  function headMore(open) {
    $("#sessMain").classList.toggle("head-more", open);
    $("#sessMore").setAttribute("aria-expanded", String(open));
    fit();
  }
  $("#sessMore").addEventListener("click", () => headMore(!$("#sessMain").classList.contains("head-more")));
  for (const sel of ["#sessOpen", ".sess-item"]) $("#sessMain").querySelector(sel).addEventListener("click", (e) => {
    if (e.target.closest("button, a")) setTimeout(() => headMore(false));
  });

  async function refresh() {
    if (busy || document.hidden || !isShown()) return;
    busy = true;
    try {
      const r = await fetch("api/sessions");
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || "failed");
      list = j.sessions;
      // At load: the session chosen last, else the first. A session that ended gives way to the first.
      if (!agent || (!pinned && !list.some(s => s.name === agent))) {
        const want = store(PICKED);
        pick((list.find(s => s.name === want) || list.find(s => s.role === "manager") || list[0] || {}).name || null, false);
      }
      renderList(mgr, list.filter(x => x.role === "manager"), agent, NO_MANAGER);
      // The experts in their own group under the manager (#4955): sessions the person talks with for days.
      const ex = list.filter(x => x.role === "expert");
      expertGroup.classList.toggle("hidden", !ex.length);
      renderList(experts, ex, agent, "");
      renderList(side, list.filter(x => x.role !== "manager" && x.role !== "expert"), agent,
        `<div class="muted">No other agent session runs now. Start one from the Board, or run <code>claude</code> and <code>maxpm go</code> in a project folder.</div>`);
      fit();
      autoWait();
    } catch (e) {
      side.innerHTML = `<div class="muted">Could not read the sessions: ${esc(e.message)}</div>`;
      delete side.dataset.empty;
    } finally { busy = false; }
  }

  async function poll() {
    const mine = gen;
    clearTimeout(timer);
    if (!agent) return;
    if (document.hidden || !isShown()) { timer = setTimeout(poll, 3000); return; }
    try {
      const r = await fetch(`api/session?agent=${encodeURIComponent(agent)}${cursor != null ? "&cursor=" + cursor : ""}`);
      const j = await r.json();
      if (mine !== gen) return;
      if (!r.ok) { note(j.error || "failed"); timer = setTimeout(poll, 10000); return; }
      const first = cursor == null, atEnd = body.scrollHeight - body.scrollTop - body.clientHeight < 60;
      if (first) { if (j.entries.length) col.replaceChildren(); else note(j.note || "No lines yet."); }
      else if (j.entries.length) col.querySelector(".sl-none")?.remove();
      appendEntries(col, j.entries, j.refs);
      checkPaths(col, agent);  // what each path means for this session; a refused one says why (#4689)
      agePending(col);
      cursor = j.cursor;
      const s = list.find(x => x.name === agent) || {};
      $("#sessState").innerHTML = provTag(j.provider) + stateChip(j);
      waitLine(j);
      $("#sessItem").innerHTML = s.name ? headerItem(s) : "";
      $("#sessName").textContent = s.title || "";
      barFor(j.terminal, list.some(x => x.name === agent) ? s : { ended: true });
      dict.showFor(list.some(x => x.name === agent) ? (s || {}).provider || j.provider : null, !!j.terminal);
      $("#sessOpen").innerHTML = headerOpen({ ...s, session_url: j.session_url, terminal: j.terminal });
      if (atEnd || first) body.scrollTop = body.scrollHeight;  // the first read shows the end; later, follow it
    } catch (e) { /* the next poll tries again */ }
    if (mine === gen && agent) timer = setTimeout(poll, 2000);
  }

  function pick(name, remember = true) {
    if (remember && name) store(PICKED, name);
    if (remember || !name) phoneChat(!!name);  // a session the person picks shows its chat (on a phone, #4885)
    if (name === agent) return;
    if (!agent && name) phoneChat(true);  // the first one at load
    if (name !== agent) dict.stop();
    col.classList.toggle("hide-tools", !viewOf(name).showTools);  // #4635
    agent = name; cursor = null; gen++; pinned = remember;
    onPick(name, remember);  // the page's address (#4653); the panel then names its tab
    panelFor(name);  // its own side panel tabs (#4632)
    main.classList.toggle("hidden", !name);
    $("#sessTitle").textContent = name || "";
    $("#sessName").textContent = (list.find(x => x.name === name) || {}).title || "";  // what it works on (#4695)
    $("#sessState").innerHTML = ""; $("#sessItem").innerHTML = ""; $("#sessOpen").innerHTML = "";
    $("#sessText").value = "";
    loadPrompts(name);
    clearFiles();
    barFor(null);
    note("Reading the transcript…");
    waitLine({});
    bar.querySelectorAll("[data-session]").forEach(r => r.classList.toggle("on", r.dataset.session === name));
    poll();
  }

  document.addEventListener("maxpm-view", (e) => { if (e.detail.agent === agent) col.classList.toggle("hide-tools", !viewOf(agent).showTools); });

  // A session that waits on the person says so above the composer, with a link to the ! dialog that answers it.
  function waitLine(j) {
    const w = $("#sessWait"), waits = ((list.find(x => x.name === agent) || {}).waiting || []).map(x => x.kind);
    const what = waits.includes("prompt") || j.state === "prompt" ? "It waits at a permission prompt in its terminal."
      : waits.includes("question") || j.state === "answer" ? "It waits for your answer to its question." : "";
    w.innerHTML = what ? `${esc(what)} <span class="link" data-wait-open="1" tabindex="0" role="button">Answer it</span>` : "";
    w.classList.toggle("hidden", !what);
  }
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
    if (!s || !(s.waiting || []).some(w => !seenWait.has(s.name + "\x1f" + waitKey(w)))) return;
    if (document.querySelector('#setup:not(.hidden), [id$="Dlg"]:not(.hidden)')) return;
    showWaiting(s);
  }
  // The composer grows with the text; Enter sends, Shift+Enter starts a new line.
  const text = $("#sessText");
  const grow = () => { text.style.height = "auto"; text.style.height = Math.min(text.scrollHeight, window.innerHeight / 3) + "px"; };
  // Empty messages send nothing. Files being read reserve a slot, and Send waits until they are ready.
  const FILE_MAX = 10 * 1024 * 1024, FILE_COUNT = 5;
  let files = [], sending = false;
  const empty = () => !text.value.trim() && !files.length;
  const sendState = () => {
    $("#sessSend").disabled = text.disabled || sending || empty() || files.some(f => f.reading);
    $("#sessAttach").disabled = $("#sessPickFiles").disabled = text.disabled || sending;
  };
  // Dictation (#4626): the microphone puts speech into the box as text.
  const dict = makeDictation({ button: $("#sessMic"), input: text, status: $("#sessDictate"), changed: () => { grow(); sendState(); } });
  text.addEventListener("input", () => { grow(); sendState(); });
  text.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); if (!empty()) $("#sessBar").requestSubmit(); }
    else if ((e.key === "ArrowUp" || e.key === "ArrowDown") && recall(e.key === "ArrowUp" ? 1 : -1, e)) e.preventDefault();
  });
  // The earlier prompts (#4855), as in a shell: Up brings back the prompts sent to this session from this box,
  // newest first, one at a time; Down goes forward again, and past the newest one the box shows the text the person
  // typed before the first Up (the draft), or nothing. Up acts only with the caret on the box's first line and Down
  // only on its last line, else the caret moves as in any text box. The list comes from the server (every device,
  // and the relay); files attached are not part of it.
  let prompts = [], recalled = -1, draft = "";
  function loadPrompts(name, again = false) {  // again: the box got the focus; a list in use stays as it is
    if (!again) { prompts = []; recalled = -1; draft = ""; }
    if (!name) return;
    fetch("api/session-prompts?agent=" + encodeURIComponent(name)).then(r => r.ok ? r.json() : null).then(j => {
      if (j && name === agent && recalled === -1) prompts = j.prompts || [];
    }).catch(() => {});
  }
  function recall(step, e) {
    if (e.isComposing || e.shiftKey || e.altKey || e.ctrlKey || e.metaKey || text.disabled) return false;
    const v = text.value, at = step > 0 ? text.selectionStart : text.selectionEnd;
    if (step > 0 ? v.slice(0, at).includes("\n") : v.slice(at).includes("\n")) return false;  // not the edge line
    const to = recalled + step;
    if (to < -1 || to >= prompts.length) return false;
    if (recalled === -1) draft = v;
    recalled = to;
    text.value = to === -1 ? draft : prompts[to];
    // Up leaves the caret on the first line, so the next Up goes on; Down leaves it on the last line.
    const end = step > 0 && text.value.includes("\n") ? text.value.indexOf("\n") : text.value.length;
    text.setSelectionRange(end, end);
    grow(); sendState();
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
      const x = document.createElement("button"); x.type = "button"; x.className = "iconbtn"; x.textContent = "✕";
      x.title = `Remove ${f.name}`; x.setAttribute("aria-label", x.title); x.dataset.rm = i; x.disabled = sending;
      d.append(label, x); return d;
    }));
    box.classList.toggle("hidden", !files.length);
    sendState();
  }
  function clearFiles() {
    files.forEach(f => { if (f.url) URL.revokeObjectURL(f.url); });
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
    const r = new FileReader();
    r.onload = () => { if (!files.includes(f)) return; f.data = String(r.result).split(",")[1]; f.reading = false; renderFiles(); };
    r.onerror = () => {
      if (!files.includes(f)) return;
      files.splice(files.indexOf(f), 1); if (f.url) URL.revokeObjectURL(f.url);
      renderFiles(); toast(`Could not read ${f.name}`, true);
    };
    r.readAsDataURL(file);
  }
  $("#sessFiles").addEventListener("click", (e) => {
    const b = e.target.closest("[data-rm]"); if (!b || sending) return;
    const [f] = files.splice(+b.dataset.rm, 1); if (f.url) URL.revokeObjectURL(f.url); renderFiles(); text.focus();
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
    if (!to || btn.disabled || empty()) return;
    sending = true; renderFiles();
    let ok = true;
    try {
      const sent = files, typed = input.value;
      const r = await typeAction("session_send", { agent: to, text: typed,
        ...(sent.length ? { files: sent.map(f => ({ name: f.name, data: f.data })) } : {}) });
      if (to === agent) {
        col.querySelector(".sl-none")?.remove();
        appendEntries(col, [{ role: "user", sent: true, track: !!r.text, text: r.text || (sent.length ? "" : "(Enter)"),
                              images: (r.images || []).map(p => p.split(/[\\/]/).slice(-2).join("/")),
                              files: (r.files || []).filter(p => !(r.images || []).includes(p)).map(p => ({
                                path: p.split(/[\\/]/).slice(-2).join("/"), name: p.split(/[\\/]/).pop().replace(/^\d{8}-\d{6}-[a-f0-9]{8}-/, "") })) }]);
        clearFiles();
        sentPrompt(to, typed);
        body.scrollTop = body.scrollHeight;
        input.value = "";
        grow();
      }
    } catch (err) { toast(err.message, true); ok = !/tmux pane/.test(err.message); }
    finally { sending = false; renderFiles(); if (!input.disabled) { sendState(); input.focus(); } if (!ok) barFor(false); }
  });
  $("#sessOpen").addEventListener("click", (e) => {
    const b = e.target.closest("[data-term]"); if (b) openTerminal(b.dataset.term);
    const st = e.target.closest("[data-settings]"); if (st) openSettings(st.dataset.settings, { done: refresh });
  });
  bar.addEventListener("click", (e) => {
    if (e.target.closest("[data-open], a")) return;  // an item link opens in the side panel
    const r = e.target.closest(".srow-main") && e.target.closest("[data-session]"); if (!r) return;
    pick(r.dataset.session);
    const s = list.find(x => x.name === r.dataset.session);
    if (s && (s.waiting || []).length) showWaiting(s);  // what waits for the person comes first; Dismiss shows the chat
  });
  bar.addEventListener("keydown", (e) => {
    const r = (e.key === "Enter" || e.key === " ") && e.target.closest(".srow-main") && e.target.closest("[data-session]");
    if (!r) return;
    e.preventDefault(); pick(r.dataset.session);
    const s = list.find(x => x.name === r.dataset.session);
    if (s && (s.waiting || []).length) showWaiting(s);
  });
  setInterval(refresh, 3000);
  return { refresh: () => { refresh(); fit(); }, pick };
}
