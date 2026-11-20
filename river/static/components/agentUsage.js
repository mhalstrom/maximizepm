// The usage of one agent type (#6387, mark 2026-11-20: 'when we click on a specific agent type we should be able to
// get the detailed usage information'). limitRowHtml(r) is a row of the usage list at the foot of the Sessions list;
// a click on it opens openAgentUsage(agent_type): what can still launch (each model group of agy), every usage window
// with its percent left and reset time, the reason of a limit, where and when the reading came from, the sessions of
// the agent that run now with their models, and Read now (op usage_read), which reads the agent's usage at once.
// Server: GET api/agent-usage?agent=<id> (server.agent_usage, core.agent_usage).
import { esc, getJson, act, ago } from "../lib.js";
import { makeDialog } from "./dialog.js";
import { button } from "./button.js";
import { bar } from "./bar.js";
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
const pct = (n) => `${Math.round(n)}% left`;
// 'Claude and GPT models' -> 'Claude and GPT': the row is short.
const groupShort = (name) => name.replace(/\s+models$/i, "");

// What the row says: not known; the whole agent at its limit; each model group (agy); or the least percent left.
function summary(r) {
  if (!r.known) return { html: `<span class="muted">usage not known</span>`, note: "" };
  if (r.limited) return { html: `<span class="sl-lim-warn">At limit</span>`, note: resets(r) };
  const groups = r.groups || [];
  if (groups.length) {
    const html = groups.map(g => g.full
      ? `<span class="sl-lim-warn">${esc(groupShort(g.name))} at limit</span>`
      : `<span>${esc(groupShort(g.name))} ${g.remaining_pct != null ? pct(g.remaining_pct) : "no limit reached"}</span>`).join("");
    const full = groups.find(g => g.full);
    return { html, note: full ? resets(full) : "" };
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

let dlg = null, el = null, agent = null, timer = null, tried = false;
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
  if (w.used_percent == null && w.remaining_percent == null) {
    return `<div class="usage-row"><span class="muted">${esc(w.name)}</span><span>${esc(w.text || "")}</span></div>`;
  }
  // As the agent's own usage screen shows it (mark 2026-11-20: 'it should show the actual usage'): the part used, a bar
  // of it, and the reset in this computer's time.
  const used = Math.max(0, Math.min(100, w.used_percent != null ? w.used_percent : 100 - w.remaining_percent));
  const when = w.reset_since ? "started again since the reading" : resets(w);
  const stop = w.session ? `limit stop of session ${w.session}${w.at ? ` at ${at(w.at)}` : ""}` : "";
  return `<div class="usage-limit${used >= 100 ? " full" : ""}"><div class="usage-row"><span class="muted">${esc(w.name)}</span>
    <span${used >= 100 ? ` class="sl-lim-warn"` : ""}>${Math.round(used)}% used${used >= 100 ? " (at limit)" : ""}${
    when ? `, ${esc(when)}` : ""}</span></div>${bar(used)}${stop ? `<div class="muted t-s">${esc(stop)}</div>` : ""}</div>`;
}

export function agentUsageHtml(u) {
  const parts = [];
  const s = summary(u);
  parts.push(`<div class="au-sum">${s.html}${s.note ? `<span class="muted t-s">${esc(s.note)}</span>` : ""}</div>`);
  if (u.reason) parts.push(`<div class="t-m">Why: ${esc(u.reason)}</div>`);
  const groups = u.groups || [];
  if (groups.length) {
    parts.push(`<h3>What can launch</h3>` + groups.map(g => `<div class="usage-row"><span class="muted">${esc(g.name)}</span>${g.full
      ? `<span class="sl-lim-warn">no launch${resets(g) ? ` · ${esc(resets(g))}` : ""}</span>`
      : `<span>launches${g.remaining_pct != null ? ` · ${Math.round(100 - g.remaining_pct)}% used` : ""}</span>`}</div>`).join(""));
  }
  const wins = u.windows || [];
  parts.push(`<h3>Usage windows</h3>` + (wins.length ? wins.map(windowRow).join("")
    : `<p class="muted t-m">${u.limited ? "This limit came with no windows: Read now shows them." : "No reading of its windows yet."}</p>`));
  const read = u.updated_at ? `${esc(u.source_text || u.source || "")}, ${esc(at(u.updated_at))} (${esc(ago(u.updated_at))})`
    : "No reading yet.";
  const probe = u.reading ? `<div class="t-m">Reading now…</div>`
    : u.probe_error ? `<div class="sl-lim-warn t-m">The last reading failed: ${esc(u.probe_error)}</div>` : "";
  parts.push(`<h3>Reading</h3><div class="t-m">${read}</div>${probe}`);
  const sess = u.sessions || [];
  parts.push(`<h3>Sessions now</h3>` + (sess.length ? sess.map(x => `<div class="usage-row"><span class="link" data-au-session="${esc(x.name)}"
      title="Open its chat">${esc(x.name)}</span><span class="muted">${esc(x.model || "model not known")}${x.items.length
      ? ` · ${x.items.map(i => `#${i.id}`).join(", ")}` : ""}</span></div>`).join("")
    : `<p class="muted t-m">No session of it runs now.</p>`));
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
  agent = agentType; tried = false;
  el.querySelector("#agentUsageTitle").textContent = "Usage";
  el.querySelector(".au-body").innerHTML = `<p class="muted">Reading…</p>`;
  dlg.open();
  load();
}
