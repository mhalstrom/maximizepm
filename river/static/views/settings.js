// The Settings tab (#4656): every key, its default and its overrides, and the form that sets one; Policies (#5308):
// the rules sessions follow, and Add policy; and People (#5102): the people of this queue and Register a person.
// #5848 (mark 2026-11-14): a strip of tabs; each shows only its own group. The settings keys go into groups by
// GROUPS (the first group whose test takes a key; Advanced takes the rest), so a new key always shows somewhere.
import { $, esc, toast, act, ago, store, hooks } from "../lib.js";
import { chip } from "../components/chip.js";
import { button, iconButton } from "../components/button.js";
import { makeTable } from "../components/table.js";
import { S } from "../page.js";
import { renderCredentials } from "./credentials.js";

const AGENT_CLIS = /^(aider|amp|claude|codex|copilot|cursor|gemini|goose|grok|hermes|opencode|pi)_/;
const has = (...keys) => (k) => keys.includes(k);
// [tab key, tab name, one line for its heading, test of a settings key (none: not a group of keys)]
export const GROUPS = [
  ["credentials", "Credentials", ""],
  ["agents", "Agents", "Which agents the Start button opens, their models, and the options of each agent program.",
    (k) => AGENT_CLIS.test(k) || k.startsWith("agent_for.") || k.startsWith("default_") && k !== "default_prerequisite_mode"
      || has("launch_agents", "agent_rules", "effort_levels", "model_ladder", "model_prices", "prompt_pattern",
             "prompt_wait")(k)],
  ["sessions", "Sessions", "How long sessions hold work, wait and stay idle; the manager, experts and questions.",
    (k) => /^(lease_|hold_|idle_|wait_|goal_|base_|revive_|expert_|manage_|manager_|question_|web_terminal)/.test(k)
      || has("max_leases", "max_sessions", "busy_max", "gone_after", "away_after", "owner_ttl", "reserve_ttl",
             "auto_continue", "fresh_sessions", "native_message", "human_wait_max", "tmux_adopt", "shells_every")(k)],
  ["work", "Work", "Reviews, releases, deploys, and how items are added, checked and cleaned up.",
    (k) => /^(review|release_|similar_|list_cleanup_)/.test(k)
      || has("auto_review", "deploy_timeout", "result_look", "tracker", "push_guard", "due_warn_before",
             "replan_threshold", "keep_prereq_limit", "default_prerequisite_mode", "stale_after", "unpushed_after",
             "conflict_max_lines", "hook_timeout")(k)],
  ["monitoring", "Monitoring", "The checks that watch your projects, and what a fault does.",
    (k) => k.startsWith("ops_") || has("health_person_minutes", "activity_silence")(k)],
  ["browser", "Browser", "The browser of each session, and the canvases it shows.",
    (k) => /^(browser_|canvas_)/.test(k)],
  ["notifications", "Notifications", "Where MaximizePM sends what needs you, and how often.",
    (k) => /^(notify_|ntfy_)/.test(k) || k === "timezone"],
  ["policies", "Policies", ""],
  ["people", "People", ""],
  ["advanced", "Advanced", "maxpm serve, storage limits and upkeep.", () => true],
];
const KEY_GROUPS = GROUPS.filter(g => g[3]);
export const groupOf = (key) => KEY_GROUPS.find(g => g[3](key))[0];

let current = store("river.settingsTab");
if (!GROUPS.some(g => g[0] === current)) current = "credentials";  // Credentials first (#5848)

// The strip and the pane of the chosen tab.
function renderTabs() {
  const html = GROUPS.map(([k, name]) => button(esc(name), { cls: "set-tab", pressed: current === k, data: { "set-tab": k },
    attrs: `role="tab" aria-selected="${current === k}"` })).join("");
  const el = $("#setTabs"); if (el.dataset.sig !== html) { el.innerHTML = html; el.dataset.sig = html; }
  const keys = KEY_GROUPS.some(g => g[0] === current);
  document.querySelectorAll("#tab-settings .set-pane").forEach(p =>
    p.classList.toggle("hidden", p.dataset.setPane !== (keys ? "keys" : current)));
}

// Settings: one row per key of the chosen group; an override chip's × removes that override.
let settingsTable = null, tableGroup = null;
export function renderSettings() {
  renderTabs();
  const g = KEY_GROUPS.find(x => x[0] === current);
  if (!g) return;
  $("#setGroupName").textContent = g[1];
  $("#setGroupNote").textContent = g[2];
  $("#setHelpAgents").classList.toggle("hidden", current !== "agents");
  $("#setSetupRow").classList.toggle("hidden", current !== "agents");
  const d = S.settings.defaults, o = S.settings.overrides;
  const keys = Object.keys(d).filter(k => groupOf(k) === current).sort();
  settingsTable ||= makeTable($("#settingsTable"), { key: "settings", index: "key", sort: [{ column: "key", dir: "asc" }],
    placeholder: "No settings.",
    columns: [
      { title: "Setting", field: "key", width: 200 },
      { title: "Default", field: "default", width: 240, cssClass: "muted wrap" },
      { title: "Set to", field: "overrides", cssClass: "wrap", html: (r) => r.ov.map(x => chip("c-p", `${esc(x.scope)} = ${esc(x.value)} <span class="link" data-unset="${esc(r.key)}" data-scope="${esc(x.scope)}">×</span>`)).join(" ") },
    ] });
  const now = S.settings.now || {};  // a size limit shows the size now (#5711)
  settingsTable.set(keys.map(k => {
    const ov = o.filter(x => x.key === k);
    return { key: k, default: String(d[k]) + (now[k] ? ` · ${now[k]}` : ""), overrides: ov.map(x => `${x.scope}=${x.value}`).join(" "), ov };
  }));
  // The form's list holds the keys of this group; the key chosen before stays when it is in the group.
  const sel = $("#setKey"), sig = keys.join("|");
  if (sel.dataset.sig !== sig || tableGroup !== current) {
    const keep = sel.value;
    sel.innerHTML = keys.map(k => `<option>${esc(k)}</option>`).join("");
    if (keys.includes(keep)) sel.value = keep;
    sel.dataset.sig = sig;
    tableGroup = current;
  }
}

$("#setTabs").addEventListener("click", (e) => {
  const t = e.target.closest("[data-set-tab]");
  if (!t || t.dataset.setTab === current) return;
  current = t.dataset.setTab;
  store("river.settingsTab", current);
  renderSettings();
  if (current === "credentials") renderCredentials(true);
});

// Policies: each rule with its kind and scope, source and reason. Global ones ship with MaximizePM and change only by
// a release; a user one has ✕, which retires it (deletes it: no tombstone).
export function renderPolicies() {
  const P = S.policies || [];
  const html = P.map(p => `<div class="policy"><div class="policy-body">${chip(p.kind === "global" ? "c-human" : "c-p", esc(p.pid))} ${chip("", esc(p.scope))} ${esc(p.text)}`
    + `<div class="muted t-s">${esc(p.reason)} · ${esc(p.source)}${p.adds_to ? " · adds to " + esc(p.adds_to) : ""}${p.items.length ? " · " + p.items.map(i => "#" + i.id).join(" ") : ""}`
    + `${p.kind === "global" ? " · ships with MaximizePM" : ""}</div></div>`
    + (p.kind === "user" ? iconButton("✕", `Retire ${p.pid}`, { data: { "retire-policy": p.id } }) : "") + `</div>`).join("")
    || `<div class="muted">No policies yet. A policy is a rule for the sessions: maxpm policy add "&lt;rule&gt;" --reason "&lt;why&gt;".</div>`;
  const el = $("#policies"); if (el.dataset.sig !== html) { el.innerHTML = html; el.dataset.sig = html; }
  const opts = (sel, list, first) => {
    const el = $(sel), sig = list.join("|"); if (el.dataset.sig === sig) return;
    const keep = el.value; el.innerHTML = `<option value="">${first}</option>` + list.map(x => `<option>${esc(x)}</option>`).join("");
    el.value = keep; el.dataset.sig = sig;
  };
  opts("#polProject", (S.projects || []).map(p => p.name), "every project");
  opts("#polRole", S.policy_roles || [], "every role");
  opts("#polAddsTo", P.filter(p => p.kind === "global").map(p => p.pid), "no global policy");
}
$("#polGo").addEventListener("click", async () => {
  const text = $("#polText").value.trim(), reason = $("#polReason").value.trim();
  if (!text || !reason) return toast("Type the rule and its reason", true);
  try {
    const p = await act("policy_add", { text, reason, source: $("#polSource").value.trim(), project: $("#polProject").value,
                                        role: $("#polRole").value, adds_to: $("#polAddsTo").value });
    $("#polText").value = ""; $("#polReason").value = ""; $("#polSource").value = "";
    toast(`Added ${p.pid}: the briefings of ${p.scope === "queue" ? "every session" : p.scope} show it`);
  } catch (e) { /* act showed the error */ }
});

// People: each person with their note and when they were last seen.
export function renderPeople() {
  const P = (S.agents || []).filter(a => a.kind === "human");
  const html = P.map(a => `<div class="person"><b>${esc(a.name)}</b>${a.note ? ` <span class="muted">${esc(a.note)}</span>` : ""}<span class="muted t-s"> · seen ${ago(a.last_seen)}</span></div>`).join("")
    || `<div class="muted">No person is registered yet.</div>`;
  const el = $("#people"); if (el.dataset.sig !== html) { el.innerHTML = html; el.dataset.sig = html; }
}
$("#regGo").addEventListener("click", async () => {
  const n = $("#regName").value.trim(); if (!n) return toast("Type the name first", true);
  try { await act("register", { name: n, human: true, note: $("#regNote").value.trim() }); } catch (e) { return; }
  store("river.actor", n); $("#regName").value = ""; $("#regNote").value = ""; await hooks.refresh(); $("#actor").value = n;
  toast(`${n} is registered, and is You are on this page`);
});

// A click on this view's controls; true when it was one (the frame then stops).
export function onClick(t) {
  if (t.dataset.retirePolicy) return run(async () => {
    const p = (S.policies || []).find(x => x.kind === "user" && x.id === +t.dataset.retirePolicy);
    if (p && !confirm(`Retire ${p.pid}? It is deleted, and no briefing shows it again.\n${p.text}`)) return;
    return act("policy_retire", { id: +t.dataset.retirePolicy }).then(() => toast("Retired")).catch(() => {});
  });
  if (t.dataset.unset) return run(async () => {
    const sc = t.dataset.scope, [kind, name] = sc.includes(":") ? [sc.split(":")[0], sc.slice(sc.indexOf(":") + 1)] : ["global", null];
    const args = { key: t.dataset.unset }; if (kind !== "global") args[kind] = kind === "item" ? +name : name;
    return act("config_unset", args).catch(() => {});
  });
  return false;
}
const run = (f) => { f(); return true; };

$("#setGo").addEventListener("click", async () => {
  const scope = $("#setScope").value, nm = $("#setScopeName").value.trim();
  const args = { key: $("#setKey").value, value: $("#setValue").value.trim() };
  if (scope !== "global") { if (!nm) return toast("Give the project, agent, or item", true); args[scope] = scope === "item" ? +nm : nm; }
  await act("config_set", args).then(() => toast("Saved")).catch(() => {});
});


