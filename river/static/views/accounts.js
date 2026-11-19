// Deploy › Accounts (#6147): one row for each forge token that a pipeline names. MaximizePM asks the forge who the
// token is, when it runs out and which repositories it reaches (river/pipeline/accounts.py, once an hour); a refused
// or missing token says so here, on its pipelines' heads and as "no news" on their stages. The token never shows.
import { esc, ago } from "../lib.js";
import { button } from "../components/button.js";
import { chip } from "../components/chip.js";
import { empty } from "../components/empty.js";

const STATE = {
  ok: ["c-ready", "works"], new: ["c-waiting", "not checked yet"], error: ["c-waiting", "check failed"],
  refused: ["c-warn", "refused"], missing: ["c-warn", "not saved"],
};

// When a token runs out, in words: "in 52 days (2027-01-10)", "ran out 2026-11-17", or "no end date".
export function runsOut(a) {
  if (!a.expires_at) return a.state === "ok" ? "no end date" : "";
  const day = a.expires_at.slice(0, 10);
  if (a.days_left < 0) return `ran out ${day}`;
  return `in ${Math.floor(a.days_left)} day${Math.floor(a.days_left) === 1 ? "" : "s"} (${day})`;
}

function row(a) {
  const [cls, word] = STATE[a.state] || STATE.new;
  const repos = Object.entries(a.repos || {});
  const soon = a.days_left != null && a.days_left <= 7;
  const btn = ["refused", "missing"].includes(a.state)
    ? `<code class="t-s">maxpm secret put ${esc(a.secret || "<name>")}</code>`
    : button("Check now", { data: { "dp-check": a.secret || "" }, title: "Ask the forge now" });
  return `<tr>
    <td><b>${esc(a.label)}</b> · ${esc(a.owner || "?")}<div class="muted t-s">${esc(a.host)}${a.kind ? " · " + esc(a.kind) : ""}</div></td>
    <td class="mono" data-label="Token">${esc(a.secret || "none named")}</td>
    <td>${chip(cls, word)}${a.state === "ok" && a.read_at ? `<div class="muted t-s">read ${esc(ago(a.read_at))}</div>` : ""}
      ${a.error ? `<div class="t-s c-warn">${esc(a.error)}</div>` : ""}</td>
    <td class="${soon ? "c-warn" : ""}" data-label="Runs out">${esc(runsOut(a)) || `<span class="muted">-</span>`}</td>
    <td class="t-s" data-label="Reaches">${repos.length ? repos.map(([r, ok]) => ok ? esc(r) : `<span class="c-warn">${esc(r)} (not reached)</span>`).join("<br>")
      : `<span class="muted">-</span>`}${a.scopes ? `<div class="muted">${esc(a.scopes)}</div>` : ""}</td>
    <td data-label="Used by">${a.used_by.map(n => button(esc(n), { cls: "plain", data: { "dp-open": n }, title: "Open the pipeline" })).join(" ")}</td>
    <td>${btn}</td></tr>`;
}

// data: GET api/deploy (its accounts, and its pipelines for the ones that need no token).
export function accountsHtml(data) {
  const accts = data.accounts || [];
  const here = data.pipelines.filter(p => !p.default && p.source.state === "local").map(p => p.name);
  const plugins = data.pipelines.filter(p => !p.default && p.source.plugin).map(p => p.name);
  const table = accts.length ? `<div class="pl-card"><h3>Accounts and tokens</h3>
    <div class="muted t-s">One token for each account on a forge; many pipelines can use one token. MaximizePM asks the forge about each token once an hour.</div>
    <div class="pl-table"><table class="pl-blocks">
      <tr><th>Account</th><th>Token</th><th>State</th><th>Runs out</th><th>Reaches</th><th>Used by</th><th></th></tr>
      ${accts.map(row).join("")}</table></div>
    <div class="muted t-s">A token is a secret of MaximizePM. Nobody sees its value, not here and not in a terminal.</div></div>` : "";
  const local = here.length ? `<div class="pl-card"><h3>This computer</h3><div class="t-m muted">maxpm serve runs these stages here: no token.</div>
    <div class="t-m">${here.map(n => button(esc(n), { cls: "plain", data: { "dp-open": n }, title: "Open the pipeline" })).join(" ")}</div></div>` : "";
  const plug = plugins.length ? `<div class="pl-card"><h3>Plugins</h3><div class="t-m muted">A plugin reads its own system with the secrets its pipeline maps to it.</div>
    <div class="t-m">${plugins.map(n => button(esc(n), { cls: "plain", data: { "dp-open": n }, title: "Open the pipeline" })).join(" ")}</div></div>` : "";
  const targets = data.pipelines.filter(p => p.default).map(p => p.name);
  const tgt = targets.length ? `<div class="pl-card"><h3>Deploy targets</h3><div class="t-m muted">MaximizePM's own releases: no token.</div>
    <div class="t-m">${targets.map(n => button(esc(n), { cls: "plain", data: { "dp-open": n }, title: "Open the pipeline" })).join(" ")}</div></div>` : "";
  return table + local + plug + tgt || empty("No pipeline yet.");
}
