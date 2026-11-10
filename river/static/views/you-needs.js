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

// ---- Needs you: decisions and steps waiting for a person, with browser notifications.
export let NY = [], nySig = "";
const nyOpen = new Set();
export function humanActor() { const a = S && S.agents.find(x => x.name === actor()); return a && a.kind === "human" ? a.name : null; }

export async function pollNeedsYou() {
  const h = humanActor();
  const r = await fetch("api/needs-you" + (h ? "?human=" + encodeURIComponent(h) : "")); if (!r.ok) return;
  NY = (await r.json()).events;
  renderNeedsYou(); notifyNew();
}

// An alert river sent for an agent that waits on a prompt in its terminal (core.PROMPT_NOTE), or for a manager that
// runs no watch (core.WATCH_NOTE): its Terminal answers it.
export const promptAlert = e => e.message_kind === "alert" && ["waits on a prompt in its terminal", "runs no watch"].some(w => (e.body || "").startsWith(w));

export function renderNeedsYou() {
  document.title = NY.length ? `(${NY.length}) MaximizePM` : "MaximizePM";
  $("#notifyOn").classList.toggle("hidden", !("Notification" in window) || Notification.permission !== "default");
  $("#needsYouPanel").classList.toggle("hidden", !NY.length);
  $("#nothingForYou").classList.toggle("hidden", !!NY.length || !!(S && (S.takeovers || []).length));
  renderYou();
  const sig = JSON.stringify([NY.map(e => [e.id, e.summary, e.item_status]), [...nyOpen]]);
  if (sig === nySig) return;
  nySig = sig;
  $("#needsYou").innerHTML = NY.map(e => {
    const isItem = e.kind === "item";
    const detail = isItem ? (e.item_context || e.item_notes) : e.body;
    const buttons = isItem
      ? button("Open", { data: { open: e.item_id } }) + button("Claim", { data: { ny: "claim", id: e.item_id } })
        + button("Copy prompt", { data: { "copy-prompt": e.item_id }, title: "A prompt for an agent that explains this and helps you do it" })
        + agentStart(S && S.launch_agents, { label: "Open agent", attrs: `data-agent-help="${e.item_id}"`, title: "Open an agent session with that prompt, in the project folder" })
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
    return `<div class="nyl" data-nyt="${e.id}" title="${esc(clip(detail || title, 300))}"><span class="t">${esc(title)}</span>
        <span class="chips">${e.priority != null ? prioNumChip(e.priority, (e.priority_from ? `priority from #${e.priority_from}` : "own priority") + (e.unblocks_count ? `; unblocks ${e.unblocks_count}` : "")) : ""}${e.project ? projectChip(e.project) : ""}</span></div>
      ${open ? `<div class="nyx"${from}><div class="muted t-s">${ago(e.opened_at)}${e.project ? " · " + esc(e.project) : ""}</div>${detail ? `<div class="ny-c">${esc(clip(detail, 800))}</div>` : ""}<div class="actions">${buttons}</div></div>` : ""}`;
  }).join("");
  // Files and local pages in the details open in the side panel of the session that sent them (#4689).
  document.querySelectorAll("#needsYou .nyx .ny-c").forEach(x => linkArtifacts(x, x.closest(".nyx").dataset.agent || null));
}

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
  const nyt = t.closest("[data-nyt]"); if (nyt && !t.closest("button")) return run(async () => { const id = +nyt.dataset.nyt; nyOpen.has(id) ? nyOpen.delete(id) : nyOpen.add(id); return renderNeedsYou(); });
  if (t.dataset.ny) return run(() => needsYouAction(t));
  if (t.dataset.copyPrompt) return run(() => copyPrompt(`api/item/${t.dataset.copyPrompt}/prompt`));
  if (t.id === "copyAll") return run(() => copyPrompt("api/prompt-all"));
  if (t.id === "agentAll" || t.dataset.agentHelp) return run(async () => {
    if (t.dataset.busy) return; t.dataset.busy = "1"; setTimeout(() => delete t.dataset.busy, 4000);
    const hid = +t.dataset.agentHelp, hit = hid ? S.items.find(i => i.id === hid) : null;
    const ch = await chooseLaunch(S, { title: "Open an agent", go: "Open agent", item: hit,
      what: t.id === "agentAll" ? "go through everything that waits on you, one at a time, with you" : `#${hid} ${hit ? hit.title : ""}, with you` });
    if (!ch) return;
    const opts = { person: actor() || undefined, ...launchArgs(ch) };
    try {
      const r = t.id === "agentAll" ? await act("open_needs_you", opts) : await act("open_agent_on", { ...opts, id: +t.dataset.agentHelp });
      toast(`Started ${r.agent} in ${r.project}` + (t.id === "agentAll" ? ", to go through what needs you" : `, to do #${t.dataset.agentHelp} with you`));
      if (r.session_name) nav.openSession(r.session_name);  // its chat opens on the Sessions tab at once (#5103)
    } catch (e) { /* toast shown */ }
    return;
  });
  if (t.id === "notifyOn") return run(async () => { await Notification.requestPermission(); return renderNeedsYou(); });
  return false;
}
const run = (f) => { f(); return true; };
