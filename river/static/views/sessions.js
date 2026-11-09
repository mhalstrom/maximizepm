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
import { showRows, wireRows } from "./artifacts.js";
import { makeDictation } from "../components/dictate.js";
import { viewOf } from "../components/viewSettings.js";
import { openInPanel, wirePanel, panelFor } from "../components/sidePanel.js";

const STATE_CHIP = { working: "c-in_progress", prompt: "c-p0", answer: "c-human", idle: "c-waiting", stopping: "c-p",
                     starting: "c-waiting", ended: "c-blocked" };
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
function headerItem(s) {
  if (s.holds && s.holds.length) return s.holds.map(h => `<button class="btn sess-itembtn" type="button" data-open="${h.id}" title="Open #${h.id} in the drawer: its notes, what it waits on, and what it unblocks">`
    + `#${h.id} ${esc(clip(h.title, 110))}${h.status ? " " + chip("c-" + h.status, esc(h.status.replace("_", " "))) : ""}</button>`).join(" ");
  if (s.owns && s.owns.length) return `<span class="muted">No item · owns target ${s.owns.map(esc).join(", ")}</span>`;
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
    : `<div class="srow-it none" title="${s.owns.length ? esc("Owns target " + s.owns.join(", ")) : "No item"}"><span class="muted">${s.owns.length ? "No item · " + esc(s.owns.join(", ")) : "No item"}</span></div>`;
  return `<div class="srow${["prompt", "answer"].includes(s.state) || (s.waiting || []).length ? " needs" : ""}" data-session="${esc(s.name)}">
    <div class="srow-main" tabindex="0" role="button" title="Open session ${esc(s.name)}">
      <div class="srow-h">${s.role === "manager" ? `<span class="mgr-tag">Manager</span>` : ""}<b>${esc(s.name)}</b>${provTag(s.provider)}<span class="spacer"></span>${waitBadge(s)}${stateChip(s)}</div>
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
  const body = $("#sessBody"), col = $("#sessCol"), main = $("#sessMain");
  wireCopy(col);
  wirePanel();
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
  function fit() {
    const lay = $("#sessLayout");
    if (!isShown() || window.innerWidth < 760) return lay.style.removeProperty("height");
    lay.style.height = Math.max(320, window.innerHeight - lay.getBoundingClientRect().top) + "px";
  }
  window.addEventListener("resize", fit);

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
      renderList(side, list.filter(x => x.role !== "manager"), agent,
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
      agePending(col);
      cursor = j.cursor;
      const s = list.find(x => x.name === agent) || {};
      $("#sessState").innerHTML = provTag(j.provider) + stateChip(j);
      waitLine(j);
      $("#sessItem").innerHTML = s.name ? headerItem(s) : "";
      barFor(j.terminal, list.some(x => x.name === agent) ? s : { ended: true });
      dict.showFor(list.some(x => x.name === agent) ? (s || {}).provider || j.provider : null, !!j.terminal);
      $("#sessOpen").innerHTML = headerOpen({ ...s, session_url: j.session_url, terminal: j.terminal });
      if (atEnd || first) body.scrollTop = body.scrollHeight;  // the first read shows the end; later, follow it
    } catch (e) { /* the next poll tries again */ }
    if (mine === gen && agent) timer = setTimeout(poll, 2000);
  }

  function pick(name, remember = true) {
    if (remember && name) store(PICKED, name);
    if (name === agent) return;
    if (name !== agent) dict.stop();
    col.classList.toggle("hide-tools", !viewOf(name).showTools);  // #4635
    agent = name; cursor = null; gen++; pinned = remember;
    onPick(name, remember);  // the page's address (#4653); the panel then names its tab
    panelFor(name);  // its own side panel tabs (#4632)
    main.classList.toggle("hidden", !name);
    $("#sessTitle").textContent = name || "";
    $("#sessState").innerHTML = ""; $("#sessItem").innerHTML = ""; $("#sessOpen").innerHTML = "";
    $("#sessText").value = "";
    clearFiles();
    barFor(null);
    note("Reading the transcript…");
    if (sview !== "chat") setView(sview);
    waitLine({});
    bar.querySelectorAll("[data-session]").forEach(r => r.classList.toggle("on", r.dataset.session === name));
    poll();
  }

  // Chat | Items (#4623): the items this session claimed, finished, released or gave away, newest first.
  let sview = "chat";
  async function loadItems() {
    if (sview !== "items" || !agent) return;
    const name = agent;
    const r = await fetch("api/session-items?agent=" + encodeURIComponent(name));
    const j = r.ok ? await r.json() : { items: [] };
    if (name !== agent || sview !== "items") return;
    $("#sessItemList").innerHTML = j.items.map(x => `<div class="si-row">
        <span class="link" data-open="${x.id}">#${x.id}</span>
        <span class="si-t">${esc(x.title)}</span>
        <span class="si-m">${chip("c-" + esc(x.status), esc(x.status.replace("_", " ")))} <span class="muted">${esc(x.did.join(", "))} · ${ago(x.at)}</span></span>
      </div>`).join("") || `<div class="muted sl-none">This session took no item yet.</div>`;
  }
  // Artifacts (#4630): what this session wrote or named, with the rows of the Artifacts tab; also when it ended.
  async function loadArts() {
    if (sview !== "artifacts" || !agent) return;
    const name = agent;
    const r = await fetch("api/artifacts?agent=" + encodeURIComponent(name));
    const j = r.ok ? await r.json() : { artifacts: [] };
    if (name !== agent || sview !== "artifacts") return;
    showRows($("#sessArtList"), j.artifacts || [], "This session wrote or named nothing the side panel can show.");
  }
  wireRows($("#sessArtList"), (_session, target) => openInPanel(target));
  function setView(v) {
    sview = v;
    $("#sessArts").classList.toggle("hidden", v !== "artifacts");
    if (v === "artifacts") { $("#sessArtList").innerHTML = `<div class="muted sl-none">Reading the transcript…</div>`; loadArts(); }
    document.querySelectorAll("[data-sview]").forEach(b => { b.classList.toggle("on", b.dataset.sview === v); b.setAttribute("aria-selected", String(b.dataset.sview === v)); });
    $("#sessBody").classList.toggle("hidden", v !== "chat");
    $("#sessItems").classList.toggle("hidden", v !== "items");
    if (v === "items") { $("#sessItemList").innerHTML = `<div class="muted sl-none">Reading the history…</div>`; loadItems(); }
  }
  document.querySelector(".sess-views").addEventListener("click", (e) => { const b = e.target.closest("[data-sview]"); if (b) setView(b.dataset.sview); });
  setInterval(() => { if (!document.hidden && isShown()) { loadItems(); loadArts(); } }, 10000);
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
  });
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
      const sent = files;
      const r = await typeAction("session_send", { agent: to, text: input.value,
        ...(sent.length ? { files: sent.map(f => ({ name: f.name, data: f.data })) } : {}) });
      if (to === agent) {
        col.querySelector(".sl-none")?.remove();
        appendEntries(col, [{ role: "user", sent: true, track: !!r.text, text: r.text || (sent.length ? "" : "(Enter)"),
                              images: (r.images || []).map(p => p.split(/[\\/]/).slice(-2).join("/")),
                              files: (r.files || []).filter(p => !(r.images || []).includes(p)).map(p => ({
                                path: p.split(/[\\/]/).slice(-2).join("/"), name: p.split(/[\\/]/).pop().replace(/^\d{8}-\d{6}-[a-f0-9]{8}-/, "") })) }]);
        clearFiles();
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
    if (e.target.closest("[data-open], a")) return;  // an item link opens the drawer
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
