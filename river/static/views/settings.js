// The Settings tab (#4656): every key, its default and its overrides, and the form that sets one; Policies (#5308):
// the rules sessions follow, and Add policy; and People (#5102): the people of this queue and Register a person.
// #5848 (mark 2026-11-14): a strip of tabs; each shows only its own group. The settings keys go into groups by
// GROUPS (the first group whose test takes a key; Advanced takes the rest), so a new key always shows somewhere.
import { $, esc, toast, act, ago, store, hooks } from "../lib.js";
import { chip } from "../components/chip.js";
import { button, iconButton } from "../components/button.js";
import { S } from "../page.js";
import { renderCredentials } from "./credentials.js";
import { labelOf, programOf } from "./settingLabels.js";

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
             "conflict_max_lines", "hook_timeout", "cost_limit")(k)],
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

// Settings (#5850, the manager's review of #5848): one row per key of the chosen group: its plain label with the key
// small under it, its value for every project in a field (empty: the default, which the field shows greyed), and the
// values for one project, agent or item as chips whose × removes them. Enter or leaving the field saves it. On/off
// keys have a select. The Agents tab puts each agent program's options under its own heading.
const ONOFF = new Set(["on", "off"]);
let tableGroup = null;
function settingRow(k, d, ov, now) {
  const glob = ov.find(x => x.scope === "global"), rest = ov.filter(x => x.scope !== "global");
  const def = String(d[k]);
  const ctl = ONOFF.has(def)
    ? `<select data-set-input="${esc(k)}" aria-label="${esc(labelOf(k))}">${["on", "off"].map(v =>
        `<option value="${v}"${(glob ? glob.value : def) === v ? " selected" : ""}>${v}${v === def ? " (default)" : ""}</option>`).join("")}</select>`
    : `<input data-set-input="${esc(k)}" value="${esc(glob ? glob.value : "")}" placeholder="${esc(def || "empty")}" aria-label="${esc(labelOf(k))}">`;
  return `<div class="set-row">
    <div class="set-label">${esc(labelOf(k))}<div class="muted t-s set-key">${esc(k)}${now[k] ? ` · now ${esc(now[k])}` : ""}</div></div>
    <div class="set-ctl">${ctl}${glob ? iconButton("↺", `Back to the default for ${k}`, { data: { unset: k, scope: "global" } }) : ""}</div>
    ${rest.length ? `<div class="set-ov">${rest.map(x => chip("c-p", `${esc(x.scope)} = ${esc(x.value)} <span class="link" data-unset="${esc(k)}" data-scope="${esc(x.scope)}">×</span>`)).join(" ")}</div>` : ""}
  </div>`;
}

export function renderSettings() {
  renderTabs();
  const g = KEY_GROUPS.find(x => x[0] === current);
  if (!g) return;
  $("#setGroupName").textContent = g[1];
  $("#setGroupNote").textContent = g[2];
  $("#setHelpAgents").classList.toggle("hidden", current !== "agents");
  $("#setSetupRow").classList.toggle("hidden", current !== "agents");
  const d = S.settings.defaults, o = S.settings.overrides, now = S.settings.now || {};  // now: a size limit's size (#5711)
  const keys = Object.keys(d).filter(k => groupOf(k) === current)
    .sort((a, b) => (programOf(a) || "").localeCompare(programOf(b) || "") || labelOf(a).localeCompare(labelOf(b)));
  let html = "", head = undefined;
  for (const k of keys) {
    const p = current === "agents" ? programOf(k) : null;
    if (current === "agents" && p !== head) { html += `<div class="cred-sub">${esc(p || "Every agent")}</div>`; head = p; }
    html += settingRow(k, d, o.filter(x => x.key === k), now);
  }
  // A field the person types in keeps its text: the rows are not drawn again while one has focus.
  const rows = $("#setRows");
  if (!rows.contains(document.activeElement) && rows.dataset.sig !== html) { rows.innerHTML = html; rows.dataset.sig = html; }
  // The form for one project, agent or item lists the keys of this group; the key chosen before stays when it is in it.
  const sel = $("#setKey"), sig = keys.join("|");
  if (sel.dataset.sig !== sig || tableGroup !== current) {
    const keep = sel.value;
    sel.innerHTML = keys.map(k => `<option value="${esc(k)}">${esc(labelOf(k))}</option>`).join("");
    if (keys.includes(keep)) sel.value = keep;
    sel.dataset.sig = sig;
    tableGroup = current;
  }
}

// A row's field: Enter or leaving it saves the value for every project; an empty field goes back to the default.
async function saveRow(el) {
  const key = el.dataset.setInput, v = el.value.trim();
  const glob = S.settings.overrides.find(x => x.key === key && x.scope === "global");
  if (v === (glob ? glob.value : "")) return;
  if (el.tagName === "SELECT" && !glob && v === String(S.settings.defaults[key])) return;
  try {
    if (v) await act("config_set", { key, value: v });
    else await act("config_unset", { key });
    toast(v ? `${labelOf(key)}: ${v}` : `${labelOf(key)}: back to the default`);
  } catch (e) { el.value = glob ? glob.value : ""; }  // act showed the error
}
$("#setRows").addEventListener("change", (e) => { if (e.target.dataset.setInput) saveRow(e.target); });
$("#setRows").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.dataset.setInput) { e.preventDefault(); e.target.blur(); }
});

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


