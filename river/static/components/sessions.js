// The Sessions tab: one card per running agent session (Claude Code and Codex), with its agent, item, state and
// its last lines, read again every few seconds while the tab shows. A click on a card opens the large view: the
// session's transcript, with new lines added as the session writes them. Read only: a person types in the session
// itself, through Open (its claude.ai/code link) or its terminal. MaximizePM never types into a session (#4592).
// Server: GET /api/sessions, and GET /api/session?agent=<name>&cursor=<byte> for the large view.
import { $, esc, ago, clip } from "../lib.js";
import { chip } from "./chip.js";
import { makeDialog } from "./dialog.js";

const STATE_CHIP = { working: "c-in_progress", prompt: "c-p0", answer: "c-human", idle: "c-waiting", stopping: "c-p" };
const ROLE_LABEL = { user: "person", assistant: "agent", tool: "tool", event: "event" };

function stateChip(s) {
  return chip(STATE_CHIP[s.state] || "c-p", esc(s.state_label), s.state === "prompt"
    ? "A permission prompt waits in its terminal or session: answer it there" : null);
}

function lineHtml(e, full) {
  const text = full ? e.text : clip(e.text.split("\n")[0], 220);
  return `<div class="sl sl-${e.role}"><span class="sl-who">${ROLE_LABEL[e.role] || esc(e.role)}</span>`
    + `<span class="sl-text">${esc(text)}</span></div>`;
}

function openButton(s) {
  if (/^https:\/\//.test(s.session_url || "")) return `<a class="btn" href="${esc(s.session_url)}" target="_blank" rel="noopener" title="Open this session on claude.ai/code to type in it">Open</a>`;
  return `<span class="muted sl-noopen" title="${s.agent_type === "codex" ? "Codex sessions have no web link: type in its terminal" : "This session has no Remote Control link: type in its terminal"}">no web link</span>`;
}

export function sessionCard(s) {
  const item = s.holds.length ? s.holds.map(h => `<span class="link" data-open="${h.id}">#${h.id} ${esc(clip(h.title, 90))}</span>`).join(" ")
    : s.owns.length ? `owns target ${s.owns.map(esc).join(", ")}` : `<span class="muted">${esc(s.role || "no item")}</span>`;
  const meta = [s.agent_type, s.model, s.also.length ? "also: " + s.also.join(", ") : ""].filter(Boolean).map(esc).join(" · ");
  const lines = s.last.length ? s.last.map(e => lineHtml(e, false)).join("")
    : `<div class="muted sl-none">${s.transcript ? "No lines yet." : "No transcript of this session on this computer."}</div>`;
  return `<div class="scard" data-session="${esc(s.name)}" tabindex="0" title="Click to see the whole session">
    <div class="scard-h"><b>${esc(s.name)}</b>${stateChip(s)}<span class="spacer"></span>${openButton(s)}</div>
    <div class="scard-item">${item}</div>
    <div class="sub">${meta}${s.at ? ` · last line <span class="sago" data-at="${esc(s.at)}">${ago(s.at)}</span>` : ""}</div>
    <div class="scard-lines">${lines}</div>
  </div>`;
}

// Replace only the cards whose data changed, so a card keeps its hover, focus and selected text between reads.
function renderGrid(grid, list) {
  const cards = [...grid.querySelectorAll("[data-session]")];
  if (!list.length) {
    grid.innerHTML = `<div class="muted">No agent session runs now. Start one from the Board, or run <code>claude</code> and <code>maxpm go</code> in a project folder.</div>`;
    return;
  }
  const sigs = list.map(s => JSON.stringify(s));
  if (cards.length !== list.length || cards.some((c, i) => c.dataset.session !== list[i].name)) {
    grid.innerHTML = list.map(sessionCard).join("");
    [...grid.children].forEach((c, i) => { c.dataset.sig = sigs[i]; });
    return;
  }
  cards.forEach((c, i) => {
    if (c.dataset.sig === sigs[i]) return;
    const t = document.createElement("template");
    t.innerHTML = sessionCard(list[i]).trim();
    t.content.firstChild.dataset.sig = sigs[i];
    c.replaceWith(t.content.firstChild);
  });
  grid.querySelectorAll(".sago").forEach(x => { x.textContent = ago(x.dataset.at); });
}

export function makeSessions({ isShown }) {
  let list = [], busy = false;
  const grid = $("#sessions");
  const render = () => renderGrid(grid, list);
  async function refresh() {
    if (busy || document.hidden || !isShown()) return;
    busy = true;
    try {
      const r = await fetch("api/sessions");
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || "failed");
      list = j.sessions;
      render();
    } catch (e) {
      grid.innerHTML = `<div class="muted">Could not read the sessions: ${esc(e.message)}</div>`;
    } finally { busy = false; }
  }

  // The large view of one session.
  const dlg = makeDialog($("#sessDlg"), { onClose: () => { agent = null; clearTimeout(timer); } });
  const body = $("#sessBody");
  let agent = null, cursor = null, timer = null, gen = 0;
  async function poll() {
    const mine = gen;
    clearTimeout(timer);
    if (!agent) return;
    try {
      const r = await fetch(`api/session?agent=${encodeURIComponent(agent)}${cursor != null ? "&cursor=" + cursor : ""}`);
      const j = await r.json();
      if (mine !== gen) return;
      if (!r.ok) { body.innerHTML = `<div class="muted">${esc(j.error || "failed")}</div>`; return; }
      const first = cursor == null, atEnd = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
      if (first) body.innerHTML = j.entries.length ? "" : `<div class="muted sl-none">No lines yet.</div>`;
      else if (j.entries.length) body.querySelector(".sl-none")?.remove();
      body.insertAdjacentHTML("beforeend", j.entries.map(e => lineHtml(e, true)).join(""));
      cursor = j.cursor;
      $("#sessState").innerHTML = stateChip(j);
      $("#sessOpen").innerHTML = openButton({ ...(list.find(s => s.name === agent) || {}), session_url: j.session_url });
      if (atEnd || first) body.scrollTop = body.scrollHeight;  // the first read shows the end; later, follow it
    } catch (e) { /* the next poll tries again */ }
    if (mine === gen && agent && !document.hidden) timer = setTimeout(poll, 2000);
    else if (mine === gen && agent) timer = setTimeout(poll, 10000);
  }
  function open(name) {
    agent = name; cursor = null; gen++;
    $("#sessTitle").textContent = name;
    $("#sessState").innerHTML = ""; $("#sessOpen").innerHTML = "";
    body.innerHTML = `<div class="muted sl-none">Reading the transcript…</div>`;
    dlg.open(); $("#sessClose").focus();
    poll();
  }
  $("#sessClose").onclick = () => dlg.close();
  $("#sessDlg").addEventListener("click", (e) => {
    if (e.target === e.currentTarget) return dlg.close();
    if (e.target.closest("[data-open]")) dlg.close();  // the item opens in the drawer
  });
  grid.addEventListener("click", (e) => {
    if (e.target.closest("[data-open], a")) return;
    const c = e.target.closest("[data-session]"); if (c) open(c.dataset.session);
  });
  grid.addEventListener("keydown", (e) => {
    const c = e.key === "Enter" && e.target.closest("[data-session]"); if (c) open(c.dataset.session);
  });
  setInterval(refresh, 3000);
  return { refresh, open };
}
