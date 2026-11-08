// The Sessions tab: a list of the running agent sessions (Claude Code and Codex) in a left bar, one row each with
// its agent, item and state, read again every few seconds while the tab shows; and the selected session on the
// right: its transcript, with new lines added as the session writes them (#4597, in place of the cards of #4592).
// A person types in the session through Open (its claude.ai/code link), or, for a session in a tmux pane, the
// text bar below the view (#4596): the text goes to the pane, then Enter. MaximizePM types nothing by itself.
// Server: GET /api/sessions, GET /api/session?agent=<name>&cursor=<byte>, and the op session_send (with the
// page's token: typeAction).
import { $, esc, ago, clip, store, toast, typeAction } from "../lib.js";
import { chip } from "./chip.js";

const STATE_CHIP = { working: "c-in_progress", prompt: "c-p0", answer: "c-human", idle: "c-waiting", stopping: "c-p" };
const ROLE_LABEL = { user: "person", assistant: "agent", tool: "tool", event: "event" };
const PICKED = "river.session";  // the session the person chose last

function stateChip(s) {
  return chip(STATE_CHIP[s.state] || "c-p", esc(s.state_label), s.state === "prompt"
    ? "A permission prompt waits in its terminal or session: answer it there" : null);
}

function lineHtml(e, full) {
  const text = full ? e.text : clip(e.text.split("\n")[0], 220);
  return `<div class="sl sl-${e.role}${e.sent ? " sl-sent" : ""}"><span class="sl-who">${e.sent ? "sent" : ROLE_LABEL[e.role] || esc(e.role)}</span>`
    + `<span class="sl-text">${esc(text)}</span></div>`;
}

function openButton(s) {
  if (/^https:\/\//.test(s.session_url || "")) return `<a class="btn" href="${esc(s.session_url)}" target="_blank" rel="noopener" title="Open this session on claude.ai/code to type in it">Open</a>`;
  return `<span class="muted sl-noopen" title="${s.agent_type === "codex" ? "Codex sessions have no web link: type in its terminal" : "This session has no Remote Control link: type in its terminal"}">no web link</span>`;
}

function itemHtml(s, n) {
  return s.holds.length ? s.holds.map(h => `<span class="link" data-open="${h.id}">#${h.id} ${esc(clip(h.title, n))}</span>`).join(" ")
    : s.owns.length ? `owns target ${s.owns.map(esc).join(", ")}` : `<span class="muted">${esc(s.role || "no item")}</span>`;
}

// One row of the left bar. A session that waits at a prompt or for an answer stands out (class "needs").
export function sessionRow(s) {
  const last = s.last.length ? s.last[s.last.length - 1] : null;
  return `<div class="srow${["prompt", "answer"].includes(s.state) ? " needs" : ""}" data-session="${esc(s.name)}" tabindex="0" role="button" title="Show this session">
    <div class="srow-h"><b>${esc(s.name)}</b><span class="spacer"></span>${stateChip(s)}</div>
    <div class="srow-item">${itemHtml(s, 70)}</div>
    <div class="sub">${last ? esc(clip(last.text.split("\n")[0], 80)) : ""}${s.at ? ` · <span class="sago" data-at="${esc(s.at)}">${ago(s.at)}</span>` : ""}</div>
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
  const side = $("#sessions"), body = $("#sessBody"), main = $("#sessMain");

  // The tab fills the window below the header; the view scrolls inside it, so the text bar stays in sight.
  function fit() {
    const lay = $("#sessLayout");
    if (!isShown() || window.innerWidth < 760) return lay.style.removeProperty("height");
    lay.style.height = Math.max(320, window.innerHeight - lay.getBoundingClientRect().top - 16) + "px";
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
      if (!r.ok) { body.innerHTML = `<div class="muted">${esc(j.error || "failed")}</div>`; timer = setTimeout(poll, 10000); return; }
      const first = cursor == null, atEnd = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
      if (first) body.innerHTML = j.entries.length ? "" : `<div class="muted sl-none">${esc(j.note || "No lines yet.")}</div>`;
      else if (j.entries.length) body.querySelector(".sl-none")?.remove();
      body.insertAdjacentHTML("beforeend", j.entries.map(e => lineHtml(e, true)).join(""));
      cursor = j.cursor;
      const s = list.find(x => x.name === agent) || {};
      $("#sessState").innerHTML = stateChip(j);
      $("#sessItem").innerHTML = s.holds ? itemHtml(s, 120) : "";
      barFor(j.terminal, s);
      $("#sessOpen").innerHTML = openButton({ ...s, session_url: j.session_url });
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
    body.innerHTML = `<div class="muted sl-none">Reading the transcript…</div>`;
    side.querySelectorAll("[data-session]").forEach(r => r.classList.toggle("on", r.dataset.session === name));
    poll();
  }

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
        body.querySelector(".sl-none")?.remove();
        body.insertAdjacentHTML("beforeend", lineHtml({ role: "user", sent: true, text: r.text || "(Enter)" }, true));
        body.scrollTop = body.scrollHeight;
        input.value = "";
      }
    } catch (err) { toast(err.message, true); ok = !/tmux pane/.test(err.message); }
    finally { if (!input.disabled) { btn.disabled = false; input.focus(); } if (!ok) barFor(false); }
  });
  side.addEventListener("click", (e) => {
    if (e.target.closest("[data-open], a")) return;  // an item link opens the drawer
    const r = e.target.closest("[data-session]"); if (r) pick(r.dataset.session);
  });
  side.addEventListener("keydown", (e) => {
    const r = (e.key === "Enter" || e.key === " ") && e.target.closest("[data-session]");
    if (r) { e.preventDefault(); pick(r.dataset.session); }
  });
  setInterval(refresh, 3000);
  return { refresh: () => { refresh(); fit(); }, pick };
}
