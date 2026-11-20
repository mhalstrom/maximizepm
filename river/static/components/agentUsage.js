// The usage of one agent type (#6387, mark 2026-11-20: 'when we click on a specific agent type we should be able to
// get the detailed usage information'). limitRowHtml(r) is a row of the usage list at the foot of the Sessions list;
// a click on it opens openAgentUsage(agent_type): one status line per model group (#6414: at limit until when), every
// usage window of the last reading (a window read before a newer stop dimmed), the sessions of the agent that run now
// with their models (only when one runs), Details (#6427, closed: where and when each reading and limit stop came
// from, with the agent's own words), and Read now (op usage_read), which reads the agent's usage at once.
// Server: GET api/agent-usage?agent=<id> (server.agent_usage, core.agent_usage).
import { esc, getJson, act, ago } from "../lib.js";
import { makeDialog } from "./dialog.js";
import { button } from "./button.js";
import { bar, usedText } from "./bar.js";
import { OPEN_MARK } from "./rowOpen.js";
import { nav } from "../page.js";

// A weekday names a time within six days; further away, or in the past, the date too ('Fri, Nov 27, 09:04 AM': a
// weekly reset is seven days on).
const at = (iso) => {
  const d = new Date(iso), ahead = d - Date.now();
  return d.toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit",
    ...(ahead > 6 * 864e5 || ahead < -6 * 864e5 ? { month: "short", day: "numeric" } : {}) });
};
const resets = (x) => x.resets_at ? `resets ${at(x.resets_at)}` : (x.resets_text ? `resets ${x.resets_text}` : "");
// r.remaining_pct is the least part left of its windows: the most used one.
const pct = (rem) => usedText(100 - rem);
// 'Claude and GPT models' -> 'Claude and GPT': the row is short.
const groupShort = (name) => name.replace(/\s+models$/i, "");

// What the row says: not known; the whole agent at its limit; each model group (agy); or the most percent used.
// With model groups (agy), each group says its own state and reset, also when every group is at its limit: one reset
// for the whole agent would be the reset of one group only (#6414).
function summary(r) {
  if (!r.known) return { html: `<span class="muted">usage not known</span>`, note: "" };
  const groups = r.groups || [];
  if (r.limited && !groups.length) return { html: `<span class="sl-lim-warn">At limit</span>`, note: resets(r) };
  if (groups.length) {
    const html = groups.map(g => g.full
      ? `<span class="sl-lim-warn">${esc(groupShort(g.name))} at limit${g.resets_at ? ` until ${esc(at(g.resets_at))}` : ""}</span>`
      : `<span>${esc(groupShort(g.name))} ${g.remaining_pct != null ? pct(g.remaining_pct) : "no limit reached"}</span>`).join("");
    return { html, note: "" };
  }
  return { html: r.remaining_pct != null ? `<span>${pct(r.remaining_pct)}</span>` : `<span>no limit reached</span>`,
           note: resets(r) };
}

// r: a row of the list (sessions.js): an agent's usage, or a provider entry of agent_order (#6361: r.prov, with its
// place, its sessions against its limit, and the usage of its own model in r). base: the agent's own usage row. An
// entry with a model of its own shows that model's usage; any other row shows each model group of the agent.
export function limitRowHtml(r, base) {
  const p = r.prov;
  const s = p && p.model ? summary({ ...r, groups: [] }) : summary(base || r);
  const read = r.updated_at && !r.limited ? `read ${at(r.updated_at)}` : "";
  const note = [s.note, read].filter(Boolean).join(" · ");
  const slots = !p ? "" : p.max != null
    ? `<span class="${p.full ? "sl-lim-warn" : ""}">${p.used}/${p.max} sessions</span>`
    : `<span class="muted">${p.used} session${p.used === 1 ? "" : "s"}</span>`;
  return `<div class="sl-lim-row clickrow" data-agent-usage="${esc(r.agent_type)}" tabindex="0" role="button"
    aria-label="Usage of ${esc(r.label)}"><span class="sl-lim-text">${p ? `<span class="muted t-s" title="Launch order">${p.pos}</span>` : ""}<span
    class="sl-lim-name title">${esc(r.label)}</span>${slots}${s.html}${
    note ? `<span class="muted t-s">${esc(note)}</span>` : ""}</span>${OPEN_MARK}</div>`;
}

// detailsOpen: the Details toggle stays as the person left it while the dialog shows a new reading.
let dlg = null, el = null, agent = null, timer = null, tried = false, detailsOpen = false;
function build() {
  el = document.createElement("div");
  el.id = "agentUsageDlg"; el.className = "hidden";
  el.setAttribute("role", "dialog"); el.setAttribute("aria-modal", "true"); el.setAttribute("aria-labelledby", "agentUsageTitle");
  el.innerHTML = `<div class="box"><h2 id="agentUsageTitle">Usage</h2><div class="au-body"></div>
    <div class="actions">${button("Read now", { data: { "au-read": "1" }, title: "Read its usage now" })}${
    button("Close", { data: { "au-close": "1" } })}</div></div>`;
  document.body.appendChild(el);
  dlg = makeDialog(el, { onClose: () => { clearTimeout(timer); agent = null; } });
  el.addEventListener("click", async (e) => {
    if (e.target.closest("[data-au-close]")) { dlg.close(); return; }
    const s = e.target.closest("[data-au-session]");
    if (s) { dlg.close(); return nav.openSession(s.dataset.auSession); }
    if (e.target.closest("[data-au-read]") && agent) readNow();
  });
}

function windowRow(w) {
  const old = w.older ? ` au-old` : "";
  // #6427 (mark 2026-11-20: 'too much unneeded text'): the dimming says it was read before the limit stop; the
  // reading time is in the tooltip.
  const tip = w.older && w.read_at ? ` title="Read ${esc(at(w.read_at))}"` : "";
  if (w.used_percent == null && w.remaining_percent == null) {
    return `<div class="usage-row${old}"${tip}><span class="muted">${esc(w.name)}</span><span>${esc(w.text || "")}</span></div>`;
  }
  // As the agent's own usage screen shows it (mark 2026-11-20: 'it should show the actual usage'): the part used, a bar
  // of it, and the reset in this computer's time. A window read before a newer limit stop of its group is dimmed: the
  // stop says what holds now (#6414).
  const used = Math.max(0, Math.min(100, w.used_percent != null ? w.used_percent : 100 - w.remaining_percent));
  const when = w.reset_since ? "started again since the reading" : resets(w);
  const warn = used >= 100 && !w.older;
  return `<div class="usage-limit${warn ? " full" : ""}${old}"${tip}><div class="usage-row"><span class="muted">${esc(w.name)}</span>
    <span${warn ? ` class="sl-lim-warn"` : ""}>${usedText(used)}${used >= 100 ? " (at limit)" : ""}${
    when ? `, ${esc(when)}` : ""}</span></div>${bar(used)}</div>`;
}

// Who saw the limit, and when: 'a session (maximizepm-6564) stopped at its limit at Fri 10:46 AM'.
const stopWho = (s) => s.source === "limit_stop" || !s.source
  ? `a session${s.session ? ` (${s.session})` : ""} stopped at its limit${s.at ? ` at ${at(s.at)}` : ""}`
  : `seen in ${s.source === "screen" ? "the screen of a session" : "a session's chat log"}${s.at ? ` at ${at(s.at)}` : ""}`;

// One line per model group (one for an agent whose windows count every model): its state and until when (#6414). The
// session stop that limits it and the agent's own words are in Details (#6427).
function lineState(l, cap = false) {
  const until = l.resets_at ? ` until ${at(l.resets_at)}` : (l.resets_text ? ` until ${l.resets_text}` : "");
  const text = l.limited ? `at limit${until}` : `can launch${l.remaining_pct != null ? ` · ${pct(l.remaining_pct)}` : ""}`;
  const shown = cap ? text[0].toUpperCase() + text.slice(1) : text;
  return `<span${l.limited ? ` class="sl-lim-warn"` : ""}>${esc(shown)}</span>`;
}
const statusLine = (l) => `<div class="usage-row"><span>${esc(l.name)}</span>${lineState(l)}</div>`;

// The header sums the lines up, and says nothing that they do not. With one line (an agent whose windows count every
// model) the header is that line: the same state twice is text the person does not need (#6427).
function header(lines) {
  if (!lines.length) return `<span class="muted">usage not known</span>`;
  if (lines.length === 1) return lineState(lines[0], true);
  const lim = lines.filter(l => l.limited);
  if (!lim.length) return `<span>Can launch</span>`;
  if (lim.length === lines.length) return `<span class="sl-lim-warn">Every model group at limit</span>`;
  return `<span class="sl-lim-warn">${esc(lim.map(l => l.name).join(", "))} at limit</span>`;
}

export function agentUsageHtml(u) {
  const parts = [];
  const lines = u.lines || [];
  parts.push(`<div class="au-sum">${header(lines)}</div>`);
  if (lines.length > 1) parts.push(`<div class="au-lines">${lines.map(statusLine).join("")}</div>`);
  const wins = u.usage_windows || [];
  const rd = u.read;
  parts.push(`<h3>Usage windows${rd ? ` <span class="muted t-s">read ${esc(at(rd.at))}</span>` : ""}</h3>` + (wins.length
    ? wins.map(windowRow).join("")
    : `<p class="muted t-m">${u.limited ? "This limit came with no windows: Read now shows them." : "No reading of its windows yet."}</p>`));
  // A reading that runs or failed is a state the person acts on (Read now): it stays in view.
  const probe = u.reading ? `<div class="t-m">Reading now…</div>`
    : u.probe_error ? `<div class="sl-lim-warn t-m">The last reading failed: ${esc(u.probe_error)}</div>` : "";
  if (probe) parts.push(probe);
  const sess = u.sessions || [];
  if (sess.length) parts.push(`<h3>Sessions now</h3>` + sess.map(x => `<div class="usage-row"><span class="link" data-au-session="${esc(x.name)}"
      title="Open its chat">${esc(x.name)}</span><span class="muted">${esc(x.model || "model not known")}${x.items.length
      ? ` · ${x.items.map(i => `#${i.id}`).join(", ")}` : ""}</span></div>`).join(""));
  // Details (#6427): where each fact came from, for a diagnosis: the reading of the windows, each limit stop with the
  // agent's own words. Closed by default.
  const reads = [];
  if (rd) reads.push(`<div class="t-m">Usage windows: ${esc(rd.source_text || rd.source || "")}, ${esc(at(rd.at))} (${esc(ago(rd.at))})</div>`);
  const seen = new Set();  // a stop of the whole agent is on each group's line: name it once
  for (const l of lines) {
    const key = l.stop && `${l.stop.group}|${l.stop.at}`;
    if (!key || !l.stop.at || seen.has(key)) continue;
    seen.add(key);
    reads.push(`<div class="t-m">Limit stop${l.stop.group ? ` of ${esc(l.stop.group)}` : ""}: ${esc(stopWho(l.stop))} (${esc(ago(l.stop.at))})</div>${
      l.stop.message ? `<div class="muted t-s au-details">${esc(l.stop.message)}</div>` : ""}`);
  }
  if (reads.length) parts.push(`<details class="au-more"${detailsOpen ? " open" : ""}><summary>Details</summary>${reads.join("")}</details>`);
  return parts.join("");
}

async function load() {
  clearTimeout(timer);
  const mine = agent;
  if (!mine) return;
  const body = el.querySelector(".au-body");
  let u;
  try {
    u = await getJson(`api/agent-usage?agent=${encodeURIComponent(mine)}`);
  } catch (e) {
    if (agent === mine) body.innerHTML = `<p class="muted">${esc(e.message)}</p>`;
    return;
  }
  if (agent !== mine) return;
  el.querySelector("#agentUsageTitle").textContent = `Usage: ${u.label}`;
  const more = body.querySelector(".au-more");
  if (more) detailsOpen = more.open;
  body.innerHTML = agentUsageHtml(u);
  const b = el.querySelector("[data-au-read]");
  b.classList.toggle("hidden", !u.can_read);
  b.disabled = !!u.reading;
  // No reading of its windows yet: read it now, once per opening, instead of 'not known' (mark 2026-11-20).
  if (!u.reading && !tried && u.can_read && !u.probe_error && !(u.windows || []).some(w => w.used_percent != null)) {
    tried = true;
    return readNow();
  }
  if (u.reading) timer = setTimeout(load, 2000);  // the probe takes about 5 s: show its reading when it ends
}

async function readNow() {
  const b = el.querySelector("[data-au-read]");
  b.disabled = true;
  try { await act("usage_read", { agent }); } catch (err) { b.disabled = false; return; }
  return load();
}

export function openAgentUsage(agentType) {
  if (!dlg) build();
  agent = agentType; tried = false; detailsOpen = false;
  el.querySelector("#agentUsageTitle").textContent = "Usage";
  el.querySelector(".au-body").innerHTML = `<p class="muted">Reading…</p>`;
  dlg.open();
  load();
}
