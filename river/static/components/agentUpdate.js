// The agent CLIs' versions (#6430; mark 2026-11-20: "this should show a dialog that says, X agent is out of date.
// update now button"). maxpm serve reads each agent CLI's installed and newest version when it starts and once a day
// (river/agentupdate.py); GET api/agent-versions gives the rows. When one is out of date, the dialog 'Update <Agent>'
// ('Updates for N agents') opens: a row for each agent with both versions and Update now, Update all for more than
// one, and Later (no dialog for that version for a day). The update runs when no session of the agent runs (the row
// says how many it waits for; new sessions of the agent wait for the update), then a short test request; the row
// shows each step and the end in a few words (#6460), a failed one with its message in Details. Settings > Agents
// lists every agent (versionsHtml).
import { esc, act, ago, toast } from "../lib.js";
import { makeDialog } from "./dialog.js";
import { button, iconButton } from "./button.js";

const SLOW = 5 * 60 * 1000, FAST = 2000;
const HOW = { npm: "npm", brew: "Homebrew", cask: "Homebrew", uv: "uv", pipx: "pipx", self: "its own updater" };
const ACTIVE = ["queued", "updating", "testing"];
let st = null, dlg = null, root = null, shown = [], closedSig = null, timer = null;

const iso = (t) => t ? new Date(t * 1000).toISOString() : null;
const active = (r) => r.update && ACTIVE.includes(r.update.state);
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

// What the row says of its update, in a few words; "" with no update. The long message of the update stays in
// Details (failed rows), the alert and the history (mark 2026-11-20, #6460: 'the wordyness of the text is really bad').
export function stateText(r) {
  const u = r.update;
  if (!u) return "";
  if (u.state === "queued") return u.waits ? `Waits for ${plural(u.waits, "session", "sessions")}` : "Queued";
  if (u.state === "updating") return "Updating…";
  if (u.state === "testing") return "Testing…";
  if (u.state === "done") return changed(u) ? "Updated ✓" : "Already newest";
  if (u.state === "failed") return u.kept ? "Failed, rolled back" : "Failed";
  if (u.state === "cancelled") return "Cancelled";
  return "";
}
const changed = (u) => !!(u.old && u.new && u.old !== u.new);

// The versions beside the name: installed → newest; after an update, the old → the new version.
export function verText(r) {
  const u = r.update || {};
  if (u.state === "done" && changed(u)) return `${esc(u.old)} → ${esc(u.new)}`;
  return `${esc(r.installed || "?")}${r.newer ? ` → ${esc(r.latest)}` : ""}`;
}

// A failed update's message, in a closed Details toggle under its row. The toggles a person opened stay open when
// the dialog or the Settings list draws again (keepOpen).
const opened = new Set();
function detailsHtml(r) {
  const u = r.update || {};
  if (u.state !== "failed" || !u.message) return "";
  const m = u.message[0].toUpperCase() + u.message.slice(1);
  return `<details class="au-why t-s" data-au-more="${esc(r.id)}"${opened.has(r.id) ? " open" : ""}><summary>Details</summary>
    <div class="muted">${esc(m)}</div></details>`;
}
function keepOpen(host) {
  host.addEventListener("toggle", (e) => {
    const id = e.target.dataset && e.target.dataset.auMore;
    if (id) e.target.open ? opened.add(id) : opened.delete(id);
  }, true);
}

// The task that investigates a failed or late update (#6461): ' · task #N', a link that opens it in the drawer.
const taskLink = (u) => u && u.task ? ` · <span class="link" data-open="${+u.task}">task #${+u.task}</span>` : "";

function rowHtml(r, one) {
  const u = r.update || {}, run = active(r);
  const go = u.state === "queued" ? button("Cancel", { data: { "au-cancel": r.id }, title: "Cancel the update" })
    : run || u.state === "done" ? ""
    : button(u.state === "failed" ? "Try again" : "Update now", { kind: one ? "primary" : "", data: { "au-go": r.id } });
  const cls = u.state === "failed" ? " au-fail" : u.state === "done" ? " au-done" : "";
  return `<div class="au-row${cls}">
    <div class="au-name"><b>${esc(r.label)}</b> <span class="muted t-s">${verText(r)}</span></div>
    <div class="au-st t-s" role="status">${esc(stateText(r))}${taskLink(u)}</div>
    <div class="au-acts">${go}</div>${detailsHtml(r)}</div>`;
}

export function dialogHtml(rows) {
  const left = rows.filter(r => !active(r) && !["done"].includes((r.update || {}).state));
  const one = rows.length === 1;
  // The title counts the agents that are still out of date: after an update its row stays, with how it ended.
  const old = rows.filter(r => r.newer);
  const title = !old.length ? (one ? `${rows[0].label} is up to date` : "Agents are up to date")
    : old.length === 1 ? `Update ${old[0].label}` : `Updates for ${old.length} agents`;
  const done = rows.every(r => r.update && ["done", "failed", "cancelled"].includes(r.update.state));
  return `<div class="dlg-head"><h2 id="auTitle">${esc(title)}</h2>${iconButton("✕", "Close", { data: { "au-close": "1" } })}</div>
    <div class="au-rows">${rows.map(r => rowHtml(r, one)).join("")}</div>
    <div class="actions">${done ? button("Close", { kind: "primary", data: { "au-close": "1" } })
      : button("Later", { data: { "au-later": "1" }, title: "Ask again tomorrow" })
        + (rows.length > 1 && left.length ? button("Update all", { kind: "primary", data: { "au-all": "1" } }) : "")}</div>`;
}

function dialog() {
  if (dlg) return;
  const d = document.createElement("div");
  d.id = "auDlg"; d.className = "hidden";
  d.innerHTML = '<div class="box au-box" role="dialog" aria-modal="true" aria-labelledby="auTitle"></div>';
  document.body.appendChild(d);
  root = d.firstElementChild;
  dlg = makeDialog(d, { draft: false, onClose: () => { closedSig = sigOf(); shown = []; } });
  keepOpen(d);
  d.addEventListener("click", async (e) => {
    if (e.target === d) return;
    if (e.target.closest("[data-au-close]")) return dlg.close();
    if (e.target.closest("[data-open]")) return dlg.close();  // the task opens in the drawer (app.js)
    if (e.target.closest("[data-au-later]")) {
      await run("agent_update_later", { agents: shown });
      return dlg.close();
    }
    const go = e.target.closest("[data-au-go]"), cancel = e.target.closest("[data-au-cancel]");
    if (go) await run("agent_update", { agent: go.dataset.auGo });
    else if (cancel) await run("agent_update_cancel", { agent: cancel.dataset.auCancel });
    else if (e.target.closest("[data-au-all]")) {
      const ids = rowsOf(shown).filter(r => !active(r) && (r.update || {}).state !== "done").map(r => r.id);
      await run("agent_update", { agents: ids });
    }
  });
}

async function run(op, args) {
  try { await act(op, args); } catch (e) { return; }  // act showed the error
  await poll();
}

const rowsOf = (ids) => (st ? st.agents : []).filter(r => ids.includes(r.id));
// The rows the dialog opens with: each agent out of date (not Later), and each whose update runs now (a page that
// opens during an update shows its steps too).
const due = () => (st ? st.agents : []).filter(r => r.dialog || active(r));
const sigOf = () => due().map(r => `${r.id}@${r.latest}`).join(",");

function draw() {
  if (dlg && dlg.isOpen()) root.innerHTML = dialogHtml(rowsOf(shown));
}

export function openAgentUpdates(ids) {
  if (!st) return;
  shown = ids || due().map(r => r.id);
  if (!shown.length) return;
  dialog();
  root.innerHTML = dialogHtml(rowsOf(shown));
  dlg.open();
  const b = root.querySelector(".btn.primary"); if (b) b.focus();
}

export async function poll() {
  clearTimeout(timer);
  try {
    const r = await fetch("api/agent-versions");
    if (r.ok) st = await r.json();
  } catch (e) { /* the next poll tries again */ }
  if (st) {
    if (dlg && dlg.isOpen()) draw();
    else if (!document.querySelector(".dlg:not(.hidden)")) {
      const sig = sigOf();
      if (sig && sig !== closedSig) openAgentUpdates();
    }
    for (const f of listeners) f(st);
  }
  const busy = st && st.agents.some(active) || (st && st.checking);
  timer = setTimeout(() => { if (!document.hidden || busy) poll(); else poll.later = true; }, busy ? FAST : SLOW);
}
document.addEventListener("visibilitychange", () => { if (!document.hidden && poll.later) { poll.later = false; poll(); } });

const listeners = new Set();
export function onVersions(f) { listeners.add(f); if (st) f(st); return () => listeners.delete(f); }
export function versionsNow() { return st; }

// Settings > Agents: every installed agent with its versions, the last check and update, Update now, and whether
// serve updates it by itself (the setting agent_auto_update).
export function versionsHtml(s) {
  if (!s) return `<div class="cred-sub">Agent versions</div><div class="muted">Reading…</div>`;
  const rows = s.agents.filter(r => r.path || r.installed);
  const head = `<div class="cred-sub">Agent versions</div>
    <div class="muted t-s set-note">${s.check === "off" ? "Daily check off · " : ""}${s.checked_at ? `Checked ${esc(ago(iso(s.checked_at)))}` : "Not checked yet"}${s.checking ? " · checking…" : ""}${s.error ? ` · ${esc(s.error)}` : ""}
      ${button("Check now", { cls: "plain", data: { "au-check": "1" } })}</div>`;
  if (!rows.length) return head + `<div class="muted">No agents installed</div>`;
  return head + `<div class="au-rows au-list">${rows.map(r => {
    const u = r.update || {}, run = active(r);
    const ver = u.state === "done" && changed(u) ? verText(r)
      : `${esc(r.installed || "?")}${r.newer ? ` → <b>${esc(r.latest)}</b>` : r.latest ? " · newest" : ""}`;
    const go = u.state === "queued" ? button("Cancel", { data: { "au-cancel": r.id }, title: "Cancel the update" })
      : run ? "" : r.can_update ? button("Update now", { kind: r.newer ? "primary" : "", data: { "au-go": r.id } }) : "";
    const auto = r.can_update ? button("Update by itself", { pressed: r.auto, data: { "au-auto": r.id }, title: "Automatic updates" }) : "";
    const lines = [r.updated_at ? `updated ${esc(ago(iso(r.updated_at)))}` : "", r.how ? `by ${esc(HOW[r.how] || r.how)}` : "",
      r.error ? esc(r.error) : ""].filter(Boolean).join(" · ");
    return `<div class="au-row${u.state === "failed" ? " au-fail" : ""}">
      <div class="au-name"><b>${esc(r.label)}</b> <span class="t-s">${ver}</span></div>
      <div class="au-st t-s muted">${esc(stateText(r)) || lines}${taskLink(u)}</div>
      <div class="au-acts">${go}${auto}</div>${detailsHtml(r)}</div>`;
  }).join("")}</div>`;
}

// The clicks of the Settings list; redraw() draws it again.
export function wireVersions(host, redraw) {
  if (host.dataset.auWired) return;
  host.dataset.auWired = "1";
  keepOpen(host);
  host.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-au-go],[data-au-cancel],[data-au-auto],[data-au-check]");
    if (!b || b.disabled) return;
    b.disabled = true;
    try {
      if (b.dataset.auGo) { await act("agent_update", { agent: b.dataset.auGo }); toast("Update queued"); }
      else if (b.dataset.auCancel) await act("agent_update_cancel", { agent: b.dataset.auCancel });
      else if (b.dataset.auAuto) await act("agent_update_auto", { agent: b.dataset.auAuto, on: b.getAttribute("aria-pressed") !== "true" });
      else { await act("agent_update_check", {}); toast("Checking versions"); }
    } catch (err) { /* act showed it */ }
    await poll();
    redraw();
  });
}
