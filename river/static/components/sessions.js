// The Sessions tab: a list of the running agent sessions (Claude Code and Codex) in a left bar, one row each with
// its agent, item and state, read again every few seconds while the tab shows; and the selected session on the
// right: its transcript, with new lines added as the session writes them (#4597, in place of the cards of #4592).
// The view shows the session as a chat (components/chat.js, #4604). A person types in the session through Open
// (its claude.ai/code link), or, for a session in a tmux pane, the composer below the chat (#4596): the text goes
// to the pane, then Enter. MaximizePM types nothing by itself.
// Server: GET /api/sessions, GET /api/session?agent=<name>&cursor=<byte>, and the op session_send (with the
// page's token: typeAction).
import { $, esc, ago, clip, store, toast, typeAction } from "../lib.js";
import { chip } from "./chip.js";
import { appendEntries, agePending, wireCopy, plainText } from "./chat.js";
import { openTerminal } from "./terminalDialog.js";
import { openWaiting } from "./waitDialog.js";
import { openInPanel, wirePanel } from "./sidePanel.js";

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
      <div class="srow-h">${s.role === "manager" ? `<span class="mgr-tag">Manager</span>` : ""}<b>${esc(s.name)}</b><span class="spacer"></span>${waitBadge(s)}${stateChip(s)}</div>
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

export function makeSessions({ isShown }) {
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
    if (!a || e.metaKey || e.ctrlKey || e.shiftKey || !/^https?:\/\//i.test(a.getAttribute("href"))) return;
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
      $("#sessState").innerHTML = stateChip(j);
      waitLine(j);
      $("#sessItem").innerHTML = s.name ? headerItem(s) : "";
      barFor(j.terminal, list.some(x => x.name === agent) ? s : { ended: true });
      $("#sessOpen").innerHTML = headerOpen({ ...s, session_url: j.session_url, terminal: j.terminal });
      if (atEnd || first) body.scrollTop = body.scrollHeight;  // the first read shows the end; later, follow it
    } catch (e) { /* the next poll tries again */ }
    if (mine === gen && agent) timer = setTimeout(poll, 2000);
  }

  function pick(name, remember = true) {
    if (remember && name) store(PICKED, name);
    if (name === agent) return;
    agent = name; cursor = null; gen++; pinned = remember;
    main.classList.toggle("hidden", !name);
    $("#sessTitle").textContent = name || "";
    $("#sessState").innerHTML = ""; $("#sessItem").innerHTML = ""; $("#sessOpen").innerHTML = "";
    $("#sessText").value = "";
    clearImgs();
    barFor(null);
    note("Reading the transcript…");
    waitLine({});
    bar.querySelectorAll("[data-session]").forEach(r => r.classList.toggle("on", r.dataset.session === name));
    poll();
  }

  // A session that waits on the person says so above the composer.
  function waitLine(j) {
    const w = $("#sessWait");
    w.textContent = j.state === "prompt" ? "It waits at a permission prompt in its terminal: type the answer below (for example 1), then Send."
      : j.state === "answer" ? "It waits for your answer to its question: type it below, then Send." : "";
    w.classList.toggle("hidden", !w.textContent);
  }
  // The composer grows with the text; Enter sends, Shift+Enter starts a new line.
  const text = $("#sessText");
  const grow = () => { text.style.height = "auto"; text.style.height = Math.min(text.scrollHeight, window.innerHeight / 3) + "px"; };
  // An empty message box (no text, no image) sends nothing (#4617): Send is off, and Enter does nothing.
  const empty = () => !text.value.trim() && !imgs.length;
  const sendState = () => { $("#sessBar button").disabled = text.disabled || empty(); };
  text.addEventListener("input", () => { grow(); sendState(); });
  text.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); if (!empty()) $("#sessBar").requestSubmit(); }
  });
  // Images pasted or dropped into the message box go with the next message (#4610): a thumbnail with Remove each.
  const IMG_MAX = 10 * 1024 * 1024, IMG_COUNT = 5;
  let imgs = [];
  function renderImgs() {
    const box = $("#sessImgs");
    box.replaceChildren(...imgs.map((im, i) => {
      const d = document.createElement("div"); d.className = "sess-img";
      const img = document.createElement("img"); img.src = im.url; img.alt = "Image to send";
      const x = document.createElement("button"); x.type = "button"; x.className = "iconbtn"; x.textContent = "✕";
      x.title = "Remove this image"; x.setAttribute("aria-label", "Remove this image"); x.dataset.rm = i;
      d.append(img, x); return d;
    }));
    box.classList.toggle("hidden", !imgs.length);
    sendState();
  }
  function clearImgs(revoke = true) {
    if (revoke) imgs.forEach(im => URL.revokeObjectURL(im.url));
    imgs = []; renderImgs();
  }
  function addImage(file) {
    if (!file || !/^image\/(png|jpeg|gif|webp)$/.test(file.type)) return toast("Only PNG, JPEG, GIF and WebP images go to a session", true);
    if (file.size > IMG_MAX) return toast("An image is at most 10 MB", true);
    if (imgs.length >= IMG_COUNT) return toast(`At most ${IMG_COUNT} images go with one message`, true);
    if (text.disabled) return toast("This session runs outside tmux, so the page cannot send it an image", true);
    const r = new FileReader();
    r.onload = () => { imgs.push({ type: file.type, data: String(r.result).split(",")[1], url: URL.createObjectURL(file) }); renderImgs(); };
    r.readAsDataURL(file);
  }
  $("#sessImgs").addEventListener("click", (e) => {
    const b = e.target.closest("[data-rm]"); if (!b) return;
    const [im] = imgs.splice(+b.dataset.rm, 1); URL.revokeObjectURL(im.url); renderImgs(); text.focus();
  });
  text.addEventListener("paste", (e) => {
    const files = [...(e.clipboardData?.items || [])].filter(i => i.kind === "file" && i.type.startsWith("image/")).map(i => i.getAsFile());
    if (!files.length) return;
    e.preventDefault();
    files.forEach(addImage);
  });
  const foot = $("#sessBar").closest(".chat-foot");
  foot.addEventListener("dragover", (e) => { if (e.dataTransfer?.types.includes("Files")) { e.preventDefault(); foot.classList.add("drop"); } });
  foot.addEventListener("dragleave", () => foot.classList.remove("drop"));
  foot.addEventListener("drop", (e) => {
    if (!e.dataTransfer?.files.length) return;
    e.preventDefault(); foot.classList.remove("drop");
    [...e.dataTransfer.files].forEach(addImage);
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
    const input = $("#sessText"), btn = $("#sessBar button"), to = agent;
    if (!to || btn.disabled || empty()) return;
    btn.disabled = true;
    let ok = true;
    try {
      const sent = imgs;
      const r = await typeAction("session_send", { agent: to, text: input.value,
        ...(sent.length ? { images: sent.map(im => ({ type: im.type, data: im.data })) } : {}) });
      if (to === agent) {
        col.querySelector(".sl-none")?.remove();
        appendEntries(col, [{ role: "user", sent: true, track: !!r.text, text: r.text || (sent.length ? "" : "(Enter)"),
                              localImages: sent.map(im => im.url) }]);
        clearImgs(false);  // the chat shows them now
        body.scrollTop = body.scrollHeight;
        input.value = "";
        grow();
      }
    } catch (err) { toast(err.message, true); ok = !/tmux pane/.test(err.message); }
    finally { if (!input.disabled) { sendState(); input.focus(); } if (!ok) barFor(false); }
  });
  $("#sessOpen").addEventListener("click", (e) => {
    const b = e.target.closest("[data-term]"); if (b) openTerminal(b.dataset.term);
  });
  bar.addEventListener("click", (e) => {
    if (e.target.closest("[data-open], a")) return;  // an item link opens the drawer
    const r = e.target.closest(".srow-main") && e.target.closest("[data-session]"); if (!r) return;
    pick(r.dataset.session);
    const s = list.find(x => x.name === r.dataset.session);
    if (s && (s.waiting || []).length) openWaiting(s, { onSent: sentTurn });  // what waits for the person comes first; Dismiss shows the chat
  });
  bar.addEventListener("keydown", (e) => {
    const r = (e.key === "Enter" || e.key === " ") && e.target.closest(".srow-main") && e.target.closest("[data-session]");
    if (!r) return;
    e.preventDefault(); pick(r.dataset.session);
    const s = list.find(x => x.name === r.dataset.session);
    if (s && (s.waiting || []).length) openWaiting(s, { onSent: sentTurn });
  });
  setInterval(refresh, 3000);
  return { refresh: () => { refresh(); fit(); }, pick };
}
