// Needs you in the You view of Sessions (#4656, #5102): decisions and steps that wait for a person, with browser
// notifications.
import { $, esc, store, actor, toast, act, ago, clip, copyText } from "../lib.js";
import { prioNumChip, projectChip } from "../components/chip.js";
import { agentStart } from "../components/agentStart.js";
import { chooseLaunch, launchArgs } from "../components/launchDialog.js";
import { S, nav } from "../page.js";
import { linkArtifacts } from "../components/artLinks.js";
import { renderYou } from "./you.js";
import { button } from "../components/button.js";
import { secretFillHtml, wireSecretFill } from "../components/secretFill.js";
import { openCard, openItemCard, openCardQueue, isOpen as waitOpen } from "../components/waitDialog.js";
import { OPEN_MARK } from "../components/rowOpen.js";
import { fromEvent, typeChip } from "../components/askCard.js";
import { openSignin } from "./signinAsk.js";

// The sign-in request that an alert names: "... (request r12) ..." (vault.request).
export const signinRequest = (body) => { const m = /\(request r(\d+)\)/.exec(body || ""); return m ? +m[1] : 0; };

// #5866: an alert that links a session's Browser at a sign-in step (signin.py: a stopped fill, an expired sign-in, a
// 2-step code) gets the masked field: the person fills the password or code from here, on a phone too.
const browserOf = (body) => { const m = /\/sessions\/([^/\s?#]+)\/browser\b/.exec(body || ""); return m ? decodeURIComponent(m[1]) : ""; };

// ---- Needs you: decisions and steps waiting for a person, with browser notifications.
export let NY = [], nySig = "";
const nyOpen = new Set();
export function humanActor() { const a = S && S.agents.find(x => x.name === actor()); return a && a.kind === "human" ? a.name : null; }

export async function pollNeedsYou() {
  const h = humanActor();
  const r = await fetch("api/needs-you" + (h ? "?human=" + encodeURIComponent(h) : "")); if (!r.ok) return;
  NY = (await r.json()).events;
  renderNeedsYou(); notifyNew(); autoCards();
}

// #6017 (mark 2026-11-15: 'This question should pop up as a modal, with all of the information that is relevant'): a
// question or a person item that reaches the person opens its card by itself, once, over any tab, as Allow sign-ins
// does. This browser keeps the ids it showed; the first time it only records what is open, so old cards do not all
// open. It waits while another dialog is open, the page is hidden, or the person types in a box. Several new ones:
// one card with "1 of N" and Next (waitDialog.js). The bell (#6001) is for the phone and Mac notices; this opens on
// the page also with the bell off.
const AUTO_KEEP = 500;
export function newCards(list, seen) {
  const cards = list.filter(e => e.kind === "item" || e.message_kind === "question");
  if (!Array.isArray(seen)) return { show: [], seen: cards.map(e => e.id).slice(-AUTO_KEEP) };
  const s = new Set(seen), show = cards.filter(e => !s.has(e.id));
  return { show, seen: show.length ? [...seen, ...show.map(e => e.id)].slice(-AUTO_KEEP) : seen };
}
function autoCards() {
  const h = humanActor(); if (!h) return;
  const key = "river.card.auto." + h;
  let seen = null;
  try { seen = JSON.parse(store(key) || "null"); } catch (e) { seen = null; }
  const got = newCards(NY, seen);
  if (!got.show.length) { if (!Array.isArray(seen)) store(key, JSON.stringify(got.seen)); return; }
  const typing = document.activeElement && document.activeElement.matches("input, textarea, [contenteditable]")
    && (document.activeElement.value || document.activeElement.textContent || "").trim();
  if (document.hidden || typing || waitOpen() || document.querySelector(".dlg:not(.hidden)")) return;  // later
  store(key, JSON.stringify(got.seen));
  openCardQueue(got.show);
}

// An alert river sent for an agent that waits on a prompt in its terminal (core.PROMPT_NOTE), or for a manager that
// runs no watch (core.WATCH_NOTE): its Terminal answers it.
export const promptAlert = e => e.message_kind === "alert" && ["waits on a prompt in its terminal", "runs no watch"].some(w => (e.body || "").startsWith(w));

// The true number of open Needs you events for the person (#5114): from the page's state, which holds every open one.
// The list of api/needs-you holds at most 100 (the most important); before the state comes, its length.
export function needsCount() {
  const all = S && S.needs_you; if (!all) return NY.length;
  const h = humanActor();
  return all.filter(e => !h || !e.human || e.human === h).length;
}

// The filter above the list (#6007, card-queue-mockup v15): All and each type that a line has (decide, look,
// approve, do, read, #5998), with its count, and a menu of the projects. It shows only when the lines differ in type or
// project; this browser keeps the choice. A choice that matches no line any more shows them all.
const TYPE_ORDER = ["decide", "look", "approve", "do", "read"];
const nyType = () => store("river.ny.type") || "", nyProject = () => store("river.ny.project") || "";
export function nyFilterHtml(list, type, project) {
  const types = TYPE_ORDER.filter(t => list.some(e => e.type === t));
  const projects = [...new Set(list.map(e => e.project).filter(Boolean))].sort();
  if (types.length < 2 && projects.length < 2) return "";
  const b = (t, label, n) => button(`${label} <span class="muted">${n}</span>`, { cls: "ny-f", data: { nyf: t },
    attrs: `aria-pressed="${type === t}"` });
  const inP = list.filter(e => !project || e.project === project);  // the counts of the chosen project
  return (types.length > 1 ? b("", "All", inP.length) + types.map(t => b(t, t[0].toUpperCase() + t.slice(1),
      inP.filter(e => e.type === t).length)).join("") : "")
    + (projects.length > 1 ? `<select class="ny-fp" aria-label="Project"><option value="">All projects</option>${projects.map(p =>
      `<option value="${esc(p)}"${p === project ? " selected" : ""}>${esc(p)}</option>`).join("")}</select>` : "");
}
export function nyFiltered(list, type, project) {
  const out = list.filter(e => (!type || e.type === type) && (!project || e.project === project));
  return out.length ? out : list;
}

export function renderNeedsYou() {
  const total = needsCount();
  document.title = total ? `(${total}) MaximizePM` : "MaximizePM";
  $("#notifyOn").classList.toggle("hidden", !("Notification" in window) || Notification.permission !== "default");
  $("#needsYouPanel").classList.toggle("hidden", !NY.length);
  $("#nothingForYou").classList.toggle("hidden", !!NY.length || !!(S && (S.takeovers || []).length));
  renderYou();
  // A kept choice that no line has now counts as All.
  const type = NY.some(e => e.type === nyType()) ? nyType() : "", project = NY.some(e => e.project === nyProject()) ? nyProject() : "";
  const sig = JSON.stringify([NY.map(e => [e.id, e.summary, e.item_status]), [...nyOpen], total, type, project]);
  if (sig === nySig) return;
  nySig = sig;
  $("#nyFilter").innerHTML = nyFilterHtml(NY, type, project);
  $("#needsYou").innerHTML = nyFiltered(NY, type, project).map(e => {
    const isItem = e.kind === "item";
    const detail = isItem ? (e.item_context || e.item_notes) : e.body;
    const buttons = isItem
      ? button("Open", { data: { open: e.item_id } }) + button("Claim", { data: { ny: "claim", id: e.item_id } })
        + button("Copy prompt", { data: { "copy-prompt": e.item_id }, title: "An agent prompt that helps you do it" })
        + agentStart(S && S.launch_agents, { label: "Open agent", attrs: `data-agent-help="${e.item_id}"`, title: "Open an agent with that prompt" })
        + button("Done", { kind: "primary", data: { ny: "done", id: e.item_id } })
      : (e.message_kind === "question"
          ? button("Answer", { kind: "primary", data: { ny: "answer", msg: e.message_id } })
          : (promptAlert(e) ? button("Open session", { kind: "primary", data: { "session-go": e.from_agent }, title: (e.body || "").startsWith("runs no watch") ? "Open the manager's session in the Sessions tab: tell it to start its watch again" : "Open the session in the Sessions tab: the ! dialog shows the prompt, and you answer it there" }) : "")
            + button("Mark read", { data: { ny: "read", msg: e.message_id } }))
        + (e.item_id ? button(`Open #${e.item_id}`, { data: { open: e.item_id } }) : "");
    // One line each; a click opens the details and the buttons (design: the list fits one screen).
    const title = isItem ? `#${e.item_id} ${e.item_title}` : `${e.message_kind} from ${e.from_agent === "maxpm" ? "MaximizePM" : e.from_agent}: ${e.body || ""}`;
    const open = nyOpen.has(e.id);
    const from = !isItem && e.from_agent ? ` data-agent="${esc(e.from_agent)}"` : "";
    const br = !isItem && e.message_kind === "alert" ? browserOf(e.body) : "";
    const fill = br ? `<div class="ny-sf" data-session="${esc(br)}">${secretFillHtml(null)}</div>` : "";
    return `<div class="nyl clickrow" data-nyt="${e.id}" title="${esc(clip(detail || title, 300))}"><span class="t">${esc(title)}</span>
        <span class="chips">${typeChip(e.type)}${e.priority != null ? prioNumChip(e.priority, (e.priority_from ? `priority from #${e.priority_from}` : "own priority") + (e.unblocks_count ? `; unblocks ${e.unblocks_count}` : "")) : ""}${e.project ? projectChip(e.project) : ""}</span>${OPEN_MARK}</div>
      ${open ? `<div class="nyx"${from}><div class="muted t-s">${ago(e.opened_at)}${e.project ? " · " + esc(e.project) : ""}</div>${detail ? `<div class="ny-c">${esc(clip(detail, 800))}</div>` : ""}${fill}<div class="actions">${buttons}</div></div>` : ""}`;
  }).join("") + (total > NY.length ? `<div class="muted t-s ny-more">The ${NY.length} most important of ${total}. The rest come here as you finish these; <code>maxpm needs-you</code> lists them all.</div>` : "");
  // Files and local pages in the details open in the side panel of the session that sent them (#4689).
  document.querySelectorAll("#needsYou .nyx .ny-c").forEach(x => linkArtifacts(x, x.closest(".nyx").dataset.agent || null));
  document.querySelectorAll("#needsYou .ny-sf").forEach(x => wireSecretFill(x.querySelector(".sf"), () => ({ session: x.dataset.session })));
}

$("#nyFilter").addEventListener("click", (e) => {
  const b = e.target.closest("[data-nyf]"); if (!b) return;
  store("river.ny.type", b.dataset.nyf); renderNeedsYou();
});
$("#nyFilter").addEventListener("change", (e) => {
  if (e.target.matches(".ny-fp")) { store("river.ny.project", e.target.value); renderNeedsYou(); }
});

// One browser notification per event, once: seen ids live in localStorage. The first
// load only records what is already open, so opening the page does not fire a burst.
function notifyNew() {
  let seen = null;
  try { seen = JSON.parse(store("river.ny.seen") || "null"); } catch (e) { seen = null; }
  const first = !Array.isArray(seen);
  const known = new Set(first ? [] : seen);
  const fresh = NY.filter(e => !known.has(e.id));
  if (!first && "Notification" in window && Notification.permission === "granted") {
    for (const e of fresh.slice(0, 5)) {
      const n = new Notification("MaximizePM: needs you", { body: e.summary, tag: "maxpm-" + e.id });
      // A prompt that waits: its session on the Sessions tab, where the ! dialog answers it (#4698).
      n.onclick = () => { window.focus(); if (promptAlert(e)) nav.openSession(e.from_agent); else if (e.item_id) nav.openDrawer(e.item_id); n.close(); };
    }
  }
  store("river.ny.seen", JSON.stringify([...known, ...fresh.map(e => e.id)].slice(-500)));
}

async function copyPrompt(url) {
  const h = humanActor(); const r = await fetch(url + (h ? "?person=" + encodeURIComponent(h) : ""));
  const j = await r.json(); if (!r.ok) return toast(j.error || "failed", true);
  toast(await copyText(j.prompt) ? "Prompt copied: paste it into a new agent session" : "Could not copy; your browser blocked it", !1);
}

async function needsYouAction(t) {
  const what = t.dataset.ny, id = +t.dataset.id, msg = +t.dataset.msg;
  try {
    if (what === "claim") { if (!actor()) return toast("Choose your name in 'You are' first", true); await act("claim", { id }); }
    if (what === "done") { const out = prompt("What did you decide or do? (saved as the item's output)"); if (out === null) return; await act("done", { id, output: out || undefined }); }
    if (what === "answer") {
      if (!actor()) return toast("Choose your name in 'You are' first", true);
      const body = prompt("Your answer"); if (!body) return; await act("answer", { msg, body });
    }
    if (what === "read") await act("message_read", { msg });
  } catch (e) { /* toast shown */ }
}

// A click on this panel's controls; true when it was one (the frame then stops).
export function onClick(t) {
  const nyt = t.closest("[data-nyt]"); if (nyt && !t.closest("button") && !t.closest(".nyx")) return run(async () => {
    const id = +nyt.dataset.nyt, e = NY.find(x => x.id === id);
    // #6002: a question or a person item opens its card; a sign-in request opens its permission dialog; another
    // alert opens in the list, as before.
    if (e && e.kind === "item") return openItemCard(e.item_id);  // the whole item: its tries too (#5996)
    if (e && e.message_kind === "question") return openCard(fromEvent(e));
    if (e && e.message_kind === "alert" && e.item_id && /^Read the report/.test(e.body || "")) {  // its Read card (#6003)
      act("message_read", { msg: e.message_id }).catch(() => {});
      return import("../components/reportDialog.js").then(m => m.openReport(e.item_id));
    }
    const req = e && e.message_kind === "alert" && signinRequest(e.body);
    if (req && (S.signin_asks || []).includes(req)) return openSignin(req);
    nyOpen.has(id) ? nyOpen.delete(id) : nyOpen.add(id); return renderNeedsYou();
  });
  if (t.dataset.ny) return run(() => needsYouAction(t));
  if (t.dataset.copyPrompt) return run(() => copyPrompt(`api/item/${t.dataset.copyPrompt}/prompt`));
  if (t.id === "copyAll") return run(() => copyPrompt("api/prompt-all"));
  if (t.id === "agentAll" && myAssistant()) return run(() => nav.openSession(myAssistant().name));  // one per person (#5110)
  if (t.id === "agentAll" || t.dataset.agentHelp) return run(async () => {
    if (t.dataset.busy) return; t.dataset.busy = "1"; setTimeout(() => delete t.dataset.busy, 4000);
    const hid = +t.dataset.agentHelp, hit = hid ? S.items.find(i => i.id === hid) : null;
    const ch = await chooseLaunch(S, { title: "Open an agent", go: "Open agent", item: hit,
      what: t.id === "agentAll" ? "go through everything that waits on you, one at a time, with you" : `#${hid} ${hit ? hit.title : ""}, with you` });
    if (!ch) return;
    const opts = { person: actor() || undefined, ...launchArgs(ch) };
    try {
      const r = t.id === "agentAll" ? await act("open_needs_you", opts) : await act("open_agent_on", { ...opts, id: +t.dataset.agentHelp });
      toast(r.existing ? `Your assistant ${r.agent} runs already: its chat opens` :
        `Started ${r.agent} in ${r.project}` + (t.id === "agentAll" ? ", to go through what needs you" : `, to do #${t.dataset.agentHelp} with you`));
      if (r.session_name) nav.openSession(r.session_name);  // its chat opens on the Sessions tab at once (#5103)
    } catch (e) { /* toast shown */ }
    return;
  });
  if (t.id === "notifyOn") return run(async () => { await Notification.requestPermission(); return renderNeedsYou(); });
  return false;
}
const run = (f) => { f(); return true; };
// The person's assistant that runs now (#5110): the session that goes through this list with them. One per person, so
// Go through with an agent opens it again in place of a second one.
export function myAssistant() {
  const me = actor();
  return ((S && S.agents) || []).find(a => a.role === "assistant" && !["gone", "stopped"].includes(a.state)
    && (!me || a.assistant_for === me)) || null;
}
