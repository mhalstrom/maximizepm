// The context meter and the Usage panel of a session (#5224, mark 2026-11-11: 'about usage, context etc'): what an
// agent's /usage, /context, /cost, /status and /stats show, as a page form for every agent, from data its adapter
// already reads. contextMeter(c) is the small meter in the session's header (c: the context of GET api/session: used,
// window, model); a click on it, or one of those commands typed with no argument, opens openUsage(agent): the context
// against the model's window, the session's tokens and model calls, and the plan limits where the agent writes them
// to its log (Codex). Where it shows them only on its screen, the panel says so and offers its terminal.
// Server: GET api/session-usage?agent=<name> (sessions.session_usage).
import { esc } from "../lib.js";
import { makeDialog } from "./dialog.js";
import { button } from "./button.js";
import { bar } from "./bar.js";
import { openTerminal } from "./terminalDialog.js";

const k = (n) => n == null ? "–" : n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1000 ? Math.round(n / 1000) + "k" : String(n);
const pctOf = (c) => c && c.window ? Math.min(100, Math.round(100 * c.used / c.window)) : null;

export function contextMeter(c) {
  if (!c || c.used == null) return "";
  const p = pctOf(c);
  const tip = `Context of its last model call: ${k(c.used)} tokens` + (c.window ? ` of ${k(c.window)} (${p}%)` : "")
    + ` on ${c.model || "its model"}. Click for its usage.`;
  return button(`ctx ${p != null ? p + "%" : k(c.used)}`, { cls: `ctx-b${p != null && p >= 80 ? " full" : ""}`,
    data: { usage: "1" }, title: tip });
}

let dlg = null, el = null, agent = null;
function build() {
  el = document.createElement("div");
  el.id = "usageDlg"; el.className = "hidden";
  el.setAttribute("role", "dialog"); el.setAttribute("aria-modal", "true"); el.setAttribute("aria-labelledby", "usageTitle");
  el.innerHTML = `<div class="box"><h2 id="usageTitle">Usage</h2><div class="usage-body"></div>
    <div class="actions">${button("Close", { data: { "usage-close": "1" } })}</div></div>`;
  document.body.appendChild(el);
  dlg = makeDialog(el);
  el.addEventListener("click", (e) => {
    if (e.target.closest("[data-usage-close]")) { dlg.close(); return; }
    const t = e.target.closest("[data-usage-term]");
    if (t) { dlg.close(); openTerminal(agent); }
  });
}

function row(label, value, tip = "") {
  return `<div class="usage-row"${tip ? ` title="${esc(tip)}"` : ""}><span class="muted">${esc(label)}</span><span>${esc(value)}</span></div>`;
}

function limitRow(l) {
  if (l.used_percent == null) return row(l.name, l.text || "");
  const at = l.resets_at ? new Date(l.resets_at).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" }) : "";
  return `<div class="usage-limit"><div class="usage-row"><span class="muted">${esc(l.name)}</span>
    <span>${Math.round(l.used_percent)}% used${at ? ` · resets ${esc(at)}` : ""}</span></div>${bar(Math.min(100, l.used_percent))}</div>`;
}

export function usageHtml(u) {
  const ctx = u.context != null ? { used: u.context, window: u.window, model: u.model } : null;
  const p = pctOf(ctx);
  const head = ctx ? `<div class="usage-ctx">${row("Context", `${k(ctx.used)}${ctx.window ? ` of ${k(ctx.window)} · ${p}%` : " tokens"}`,
      "The tokens of its last model call: the context the next call reads again")}${ctx.window ? bar(p) : ""}</div>`
    : `<p class="muted">No model call yet.</p>`;
  const tokens = u.measured ? [
    row("Model", u.model || "–"),
    u.calls != null ? row("Model calls", String(u.calls)) : "",
    row("Input", k(u.input), "Tokens read at the input price: not from the prompt cache"),
    row("Cache reads", k(u.cache_read)),
    row("Cache writes", k(u.cache_write)),
    row("Output", k(u.output)),
    row("Input-token equivalents", k(u.eq), "input + 0.1 x cache read + the cache writes at their price, as maxpm usage counts them"),
  ].join("") : `<p class="muted">Its log gives no token counts.</p>`;
  const limits = u.limits && u.limits.length ? u.limits.map(limitRow).join("")
    : `<p class="muted">${esc(u.limits_note || "No plan limits.")}</p>
       ${u.limits == null ? button("Open its terminal", { data: { "usage-term": "1" } }) : ""}`;
  return `${head}<h3>This session</h3>${tokens}<h3>Plan limits</h3>${limits}`;
}

export async function openUsage(name) {
  if (!dlg) build();
  agent = name;
  el.querySelector("#usageTitle").textContent = `Usage: ${name}`;
  const body = el.querySelector(".usage-body");
  body.innerHTML = `<p class="muted">Reading its logs…</p>`;
  dlg.open();
  try {
    const r = await fetch(`api/session-usage?agent=${encodeURIComponent(name)}`);
    const u = await r.json();
    body.innerHTML = r.ok ? usageHtml(u) : `<p class="muted">${esc(u.error || "No usage to show.")}</p>`;
  } catch (e) {
    body.innerHTML = `<p class="muted">${esc(String(e))}</p>`;
  }
}
