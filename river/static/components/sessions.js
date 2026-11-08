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
import { appendEntries, wireCopy, plainText } from "./chat.js";
import { openTerminal } from "./terminalDialog.js";
import { openWaiting } from "./waitDialog.js";

const STATE_CHIP = { working: "c-in_progress", prompt: "c-p0", answer: "c-human", idle: "c-waiting", stopping: "c-p" };
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
      <div class="srow-h"><b>${esc(s.name)}</b><span class="spacer"></span>${waitBadge(s)}${stateChip(s)}</div>
      <div class="sub">${esc(clip(lastText, 90))}${s.at ? ` · <span class="sago" data-at="${esc(s.at)}">${ago(s.at)}</span>` : ""}</div>
    </div>
    ${item}
  </div>`;
}

// Replace only the rows whose data changed, so a row keeps its hover and focus between reads.
function renderList(el, list, picked) {
  const rows = [...el.querySelectorAll("[data-session]")];
  if (!list.length) {
    el.innerHTML = `<div class="muted">No agent session runs now. Start one from the Board, or run <code>claude</code> and <code>maxpm go</code> in a project folder.</div>`;
    return;
  }
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
  const side = $("#sessions"), body = $("#sessBody"), col = $("#sessCol"), main = $("#sessMain");
  wireCopy(col);
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
      if (!list.some(s => s.name === agent)) {
        const want = store(PICKED);
        pick((list.find(s => s.name === want) || list[0] || {}).name || null, false);
      }
      renderList(side, list, agent);
      fit();
    } catch (e) {
      side.innerHTML = `<div class="muted">Could not read the sessions: ${esc(e.message)}</div>`;
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
      cursor = j.cursor;
      const s = list.find(x => x.name === agent) || {};
      $("#sessState").innerHTML = stateChip(j);
      waitLine(j);
      $("#sessItem").innerHTML = s.name ? headerItem(s) : "";
      barFor(j.terminal, s);
      $("#sessOpen").innerHTML = headerOpen({ ...s, session_url: j.session_url, terminal: j.terminal });
      if (atEnd || first) body.scrollTop = body.scrollHeight;  // the first read shows the end; later, follow it
    } catch (e) { /* the next poll tries again */ }
    if (mine === gen && agent) timer = setTimeout(poll, 2000);
  }

  function pick(name, remember = true) {
    if (remember && name) store(PICKED, name);
    if (name === agent) return;
    agent = name; cursor = null; gen++;
    main.classList.toggle("hidden", !name);
    $("#sessTitle").textContent = name || "";
    $("#sessState").innerHTML = ""; $("#sessItem").innerHTML = ""; $("#sessOpen").innerHTML = "";
    $("#sessText").value = "";
    barFor(null);
    note("Reading the transcript…");
    waitLine({});
    side.querySelectorAll("[data-session]").forEach(r => r.classList.toggle("on", r.dataset.session === name));
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
  text.addEventListener("input", grow);
  text.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#sessBar").requestSubmit(); }
  });
  // The text bar is always there. With no tmux pane of the session on this computer it is off, and says why.
  function barFor(pane, s = {}) {
    const input = $("#sessText"), btn = $("#sessBar button"), why = $("#sessBarWhy");
    const off = !pane;
    input.disabled = btn.disabled = off;
    why.classList.toggle("hidden", !off || pane === null);
    const elsewhere = /^https:\/\//.test(s.session_url || "") ? "type in it with Open, or in its terminal" : "type in its terminal";
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
    if (!to || btn.disabled) return;
    btn.disabled = true;
    let ok = true;
    try {
      const r = await typeAction("session_send", { agent: to, text: input.value });
      if (to === agent) {
        col.querySelector(".sl-none")?.remove();
        appendEntries(col, [{ role: "user", sent: true, text: r.text || "(Enter)" }]);
        body.scrollTop = body.scrollHeight;
        input.value = "";
        grow();
      }
    } catch (err) { toast(err.message, true); ok = !/tmux pane/.test(err.message); }
    finally { if (!input.disabled) { btn.disabled = false; input.focus(); } if (!ok) barFor(false); }
  });
  $("#sessOpen").addEventListener("click", (e) => {
    const b = e.target.closest("[data-term]"); if (b) openTerminal(b.dataset.term);
  });
  side.addEventListener("click", (e) => {
    if (e.target.closest("[data-open], a")) return;  // an item link opens the drawer
    const r = e.target.closest(".srow-main") && e.target.closest("[data-session]"); if (!r) return;
    pick(r.dataset.session);
    const s = list.find(x => x.name === r.dataset.session);
    if (s && (s.waiting || []).length) openWaiting(s);  // what waits for the person comes first; Dismiss shows the chat
  });
  side.addEventListener("keydown", (e) => {
    const r = (e.key === "Enter" || e.key === " ") && e.target.closest(".srow-main") && e.target.closest("[data-session]");
    if (!r) return;
    e.preventDefault(); pick(r.dataset.session);
    const s = list.find(x => x.name === r.dataset.session);
    if (s && (s.waiting || []).length) openWaiting(s);
  });
  setInterval(refresh, 3000);
  return { refresh: () => { refresh(); fit(); }, pick };
}
