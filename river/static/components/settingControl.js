// Settings controls (#6865): single-choice selects, usage-list checkboxes, and structured row editors
// (agent_order, agent_rules, launch_agents) built from the server's own choice lists (S.settings.choices).
// Shared by Settings > Agents (views/settings.js) and the Settings dialog (components/listSettings.js),
// for both global values and per-project / per-agent / per-item overrides.
import { esc, act, toast } from "../lib.js";
import { button, iconButton } from "./button.js";
import { chip } from "./chip.js";
import { modelChoices } from "./modelPick.js";
import { effortsFor } from "./launchDialog.js";
import { usageListRowsHtml, wireUsageListSettings } from "./agentUsage.js";

const ONOFF = new Set(["on", "off"]);
const CUSTOM = "__custom__";

// Parse a scope string ('global', 'project:foo', 'agent:bar', 'item:42') into config_set/config_unset args.
export function scopeArgs(scope) {
  if (!scope || scope === "global") return {};
  const i = scope.indexOf(":");
  if (i < 0) return {};
  const kind = scope.slice(0, i), name = scope.slice(i + 1);
  return { [kind]: kind === "item" ? +name : name };
}

function choicesOf(state) {
  return (state && state.settings && state.settings.choices) || {};
}

function launchOpt(state, platform) {
  if (!platform) return null;
  const opts = (state && state.launch_options) || [];
  const p = platform.toLowerCase();
  return opts.find(o => (o.platform || "").toLowerCase() === p)
    || opts.find(o => (o.agent_type || "").toLowerCase() === p)
    || opts.find(o => (o.label || "").toLowerCase() === p) || null;
}

// Effective default_agent for a scope (project override, else global override, else first launch option).
function chosenAgent(state, scope) {
  const ov = (state && state.settings && state.settings.overrides) || [];
  if (scope && scope !== "global") {
    const hit = ov.find(x => x.key === "default_agent" && x.scope === scope);
    if (hit && hit.value) return hit.value;
  }
  const glob = ov.find(x => x.key === "default_agent" && x.scope === "global");
  if (glob && glob.value) return glob.value;
  const first = ((state && state.launch_options) || [])[0];
  return (first && (first.platform || first.agent_type)) || "";
}

function modelsForPlatform(state, platform, current, fallback) {
  const opt = launchOpt(state, platform);
  let vals = opt && opt.models && opt.models.length
    ? modelChoices(opt.models, current || "").shown.map(m => m.name)
    : [...(fallback || [])];
  if (current && !vals.includes(current)) vals = [current, ...vals];
  return vals;
}

function effortsForPlatform(state, platform, model, current, fallback) {
  const opt = launchOpt(state, platform);
  const raw = opt ? effortsFor(opt, model || opt.default_model) : [];
  let vals = raw.length ? [...raw] : [...(fallback || [])];
  if (current && !vals.includes(current)) vals = [current, ...vals];
  return vals;
}

// Normalize a setting key's control spec from S.settings.choices + defaults.
export function settingSpec(key, state, scope = "global", current = "") {
  const ch = choicesOf(state);
  const def = state && state.settings && state.settings.defaults ? String(state.settings.defaults[key] ?? "") : "";
  const raw = (ch.keys && ch.keys[key]) || (key.startsWith("agent_for.")
    ? { kind: "agent", values: ch.agent_types || [], allow_empty: true } : null);
  if (!raw) {
    if (ONOFF.has(def)) return { kind: "select", values: ["on", "off"], allowEmpty: false };
    return { kind: "text" };
  }
  if (raw.kind === "agent") {
    let vals = [...(raw.values || ch.agent_types || [])];
    if (current && !vals.includes(current)) vals = [current, ...vals];
    return { kind: "select", values: vals, allowEmpty: raw.allow_empty !== false };
  }
  if (raw.kind === "model") {
    const plat = chosenAgent(state, scope);
    const vals = modelsForPlatform(state, plat, current, raw.values || ch.ladder_models || []);
    return { kind: "select", values: vals, allowEmpty: raw.allow_empty !== false };
  }
  if (raw.kind === "agent_model") {
    const vals = modelsForPlatform(state, raw.platform, current, raw.values || []);
    return { kind: "select", values: vals, allowEmpty: raw.allow_empty !== false };
  }
  if (raw.kind === "agent_effort") {
    const vals = effortsForPlatform(state, raw.platform, "", current, raw.values || ch.efforts || []);
    return { kind: "select", values: vals, allowEmpty: raw.allow_empty !== false };
  }
  if (raw.kind === "select") {
    let vals = [...(raw.values || [])];
    if (current && !vals.includes(current)) vals = [current, ...vals];
    return { kind: "select", values: vals, allowEmpty: !!raw.allow_empty };
  }
  return { kind: raw.kind };
}

export const isWideKind = (kind) => kind === "agent_order" || kind === "agent_rules"
  || kind === "launch_agents" || kind === "usage_list";

// --- Parsers & formatters (preserve exact stored formats of river/core.py) ---

const ORDER_KEYS = new Set(["model", "effort", "max"]);

export function parseAgentOrder(value, state) {
  const out = [];
  for (const raw of String(value || "").split(/\s*(?:,|->|;|\n)\s*/)) {
    const s = raw.trim();
    if (!s) continue;
    const words = s.replace(/\s*=\s*/g, " ").split(/\s+/);
    let i = words.findIndex(w => ORDER_KEYS.has(w.toLowerCase()));
    if (i < 0) i = words.length;
    let agent = words.slice(0, i).join(" ");
    const opt = launchOpt(state, agent) || launchOpt(state, agent.replace(/\s+/g, "-"));
    if (opt && (opt.platform || opt.agent_type)) agent = opt.platform || opt.agent_type;
    const row = { agent, model: "", effort: "", max: "" };
    for (let j = i; j + 1 < words.length; j += 2) {
      const k = words[j].toLowerCase();
      if (ORDER_KEYS.has(k)) row[k] = words[j + 1];
    }
    out.push(row);
  }
  return out;
}

export function formatAgentOrder(rows) {
  return rows.filter(r => r.agent).map(r => {
    const parts = [r.agent];
    if (r.model) parts.push("model", r.model);
    if (r.effort) parts.push("effort", r.effort);
    if (r.max !== "" && r.max != null) parts.push("max", String(r.max));
    return parts.join(" ");
  }).join(", ");
}

export function parseAgentRules(value) {
  const out = [];
  for (const raw of String(value || "").split(";")) {
    const s = raw.trim();
    if (!s) continue;
    const i = s.indexOf(":");
    if (i < 0) continue;
    out.push({ agent: s.slice(0, i).trim(), patterns: s.slice(i + 1).trim() });
  }
  return out;
}

export function formatAgentRules(rows) {
  return rows.filter(r => r.agent && r.patterns.trim())
    .map(r => `${r.agent}: ${r.patterns.trim()}`).join("; ");
}

export function parseLaunchAgents(value, agentTypes) {
  const known = new Set(agentTypes || []);
  const out = [];
  for (const raw of String(value || "").split(";")) {
    const s = raw.trim();
    if (!s) continue;
    const i = s.indexOf("=");
    if (i < 0) continue;
    const label = s.slice(0, i).trim(), cmd = s.slice(i + 1).trim();
    if (cmd.startsWith("@")) {
      const rest = cmd.slice(1).trim();
      const sp = rest.search(/\s/);
      const plat = sp < 0 ? rest : rest.slice(0, sp);
      const extra = sp < 0 ? "" : rest.slice(sp + 1).trim();
      if (known.has(plat)) { out.push({ label, agent: plat, extra }); continue; }
    }
    out.push({ label, agent: CUSTOM, extra: cmd });
  }
  return out;
}

export function formatLaunchAgents(rows) {
  return rows.map(r => {
    const label = (r.label || "").trim();
    const cmd = r.agent === CUSTOM
      ? (r.extra || "").trim()
      : `@${r.agent}${(r.extra || "").trim() ? " " + r.extra.trim() : ""}`;
    return label && cmd ? `${label}=${cmd}` : "";
  }).filter(Boolean).join("; ");
}

// --- Rendering ---

function selectHtml(key, scope, label, spec, curVal, defVal, extraAttrs = "") {
  const scAttr = scope && scope !== "global" ? ` data-set-scope="${esc(scope)}"` : "";
  const opts = [];
  if (spec.allowEmpty) {
    const emptyLabel = defVal ? `default (${defVal})` : "not set";
    opts.push(`<option value=""${!curVal ? " selected" : ""}>${esc(emptyLabel)}</option>`);
  }
  const active = spec.allowEmpty ? curVal : (curVal || defVal);
  for (const v of spec.values) {
    const suffix = !spec.allowEmpty && v === defVal ? " (default)" : "";
    opts.push(`<option value="${esc(v)}"${active === v ? " selected" : ""}>${esc(v + suffix)}</option>`);
  }
  return `<select data-set-input="${esc(key)}"${scAttr} aria-label="${esc(label)}"${extraAttrs}>${opts.join("")}</select>`;
}

function agentSelectHtml(agents, current, extra = []) {
  const vals = [...agents];
  if (current && current !== CUSTOM && !vals.includes(current)) vals.unshift(current);
  return `<select data-ed-field="agent" aria-label="Agent">${
    vals.map(a => `<option value="${esc(a)}"${a === current ? " selected" : ""}>${esc(a)}</option>`).join("")
  }${extra.map(([v, txt]) => `<option value="${esc(v)}"${v === current ? " selected" : ""}>${esc(txt)}</option>`).join("")}</select>`;
}

function rowActions(i, n) {
  return `<span class="se-acts">${
    iconButton("↑", "Move up", { data: { "ed-up": i }, attrs: i === 0 ? "disabled" : "" })
  }${iconButton("↓", "Move down", { data: { "ed-down": i }, attrs: i === n - 1 ? "disabled" : "" })
  }${iconButton("✕", "Remove row", { data: { "ed-del": i } })}</span>`;
}

function orderEditorHtml(rows, state) {
  const ch = choicesOf(state), agents = ch.agent_types || [];
  const body = rows.length ? rows.map((r, i) => {
    const models = modelsForPlatform(state, r.agent, r.model, ch.ladder_models || []);
    const efforts = effortsForPlatform(state, r.agent, r.model, r.effort, ch.efforts || []);
    return `<div class="se-row se-order" data-ed-row="${i}">
      <label class="se-f">Agent${agentSelectHtml(agents, r.agent)}</label>
      <label class="se-f">Model<select data-ed-field="model" aria-label="Model">
        <option value=""${!r.model ? " selected" : ""}>its default</option>
        ${models.map(m => `<option value="${esc(m)}"${m === r.model ? " selected" : ""}>${esc(m)}</option>`).join("")}
      </select></label>
      <label class="se-f">Effort<select data-ed-field="effort" aria-label="Effort">
        <option value=""${!r.effort ? " selected" : ""}>its default</option>
        ${efforts.map(e => `<option value="${esc(e)}"${e === r.effort ? " selected" : ""}>${esc(e)}</option>`).join("")}
      </select></label>
      <label class="se-f se-num">Max<input type="number" min="0" max="9999" inputmode="numeric" data-ed-field="max" value="${esc(r.max)}" placeholder="no limit" aria-label="Max sessions"></label>
      ${rowActions(i, rows.length)}
    </div>`;
  }).join("") : `<div class="muted t-s se-empty">No entries yet.</div>`;
  return `${body}<div class="se-add">${button("Add row", { data: { "ed-add": "1" } })}</div>`;
}

function rulesEditorHtml(rows, state) {
  const agents = choicesOf(state).agent_types || [];
  const body = rows.length ? rows.map((r, i) => `<div class="se-row se-rules" data-ed-row="${i}">
      <label class="se-f">Agent${agentSelectHtml(agents, r.agent)}</label>
      <label class="se-f se-grow">Patterns<input data-ed-field="patterns" value="${esc(r.patterns)}" placeholder="*.css, image, logo" aria-label="Patterns"></label>
      ${rowActions(i, rows.length)}
    </div>`).join("") : `<div class="muted t-s se-empty">No rules yet.</div>`;
  return `${body}<div class="se-add">${button("Add row", { data: { "ed-add": "1" } })}</div>`;
}

function launchEditorHtml(rows, state) {
  const agents = choicesOf(state).agent_types || [];
  const body = rows.length ? rows.map((r, i) => `<div class="se-row se-launch" data-ed-row="${i}">
      <label class="se-f">Label<input data-ed-field="label" value="${esc(r.label)}" placeholder="Claude Code" aria-label="Label"></label>
      <label class="se-f">Agent${agentSelectHtml(agents, r.agent, [[CUSTOM, "Custom command"]])}</label>
      <label class="se-f se-grow">${r.agent === CUSTOM ? "Command" : "Options"}<input data-ed-field="extra" value="${esc(r.extra)}" placeholder="${r.agent === CUSTOM ? 'grok "run maxpm go"' : "optional"}" aria-label="${r.agent === CUSTOM ? "Command" : "Command options"}"></label>
      ${rowActions(i, rows.length)}
    </div>`).join("") : `<div class="muted t-s se-empty">No entries yet.</div>`;
  return `${body}<div class="se-add">${button("Add row", { data: { "ed-add": "1" } })}</div>`;
}

export function structuredEditorHtml(key, value, state, scope = "global", opts = {}) {
  const kind = settingSpec(key, state, scope, value).kind;
  const agents = choicesOf(state).agent_types || [];
  let inner = "";
  if (kind === "agent_order") inner = orderEditorHtml(parseAgentOrder(value, state), state);
  else if (kind === "agent_rules") inner = rulesEditorHtml(parseAgentRules(value), state);
  else if (kind === "launch_agents") inner = launchEditorHtml(parseLaunchAgents(value, agents), state);
  const scAttr = scope && scope !== "global" ? ` data-set-scope="${esc(scope)}"` : "";
  const formAttr = opts.form ? ' data-ed-form="1"' : "";
  return `<div class="se-box" data-set-editor="${esc(key)}" data-ed-kind="${esc(kind)}"${scAttr}${formAttr}>${inner}</div>`;
}

// One setting's control for a given scope (global or override), or for the scoped-add form (opts.id / opts.form).
export function settingControlHtml(key, value, defVal, label, state, scope = "global", opts = {}) {
  const spec = settingSpec(key, state, scope, value);
  if (spec.kind === "select") {
    const idAttr = opts.id ? ` id="${esc(opts.id)}"` : "";
    return selectHtml(key, scope, label, spec, value, defVal, idAttr);
  }
  if (spec.kind === "usage_list") {
    return `<div class="se-usage" data-set-usage="${esc(key)}">${usageListRowsHtml(state)}</div>`;
  }
  if (spec.kind === "agent_order" || spec.kind === "agent_rules" || spec.kind === "launch_agents") {
    const eff = value || (kindUsesDefault(spec.kind) ? defVal : "");
    return structuredEditorHtml(key, eff, state, scope, opts);
  }
  const scAttr = scope && scope !== "global" ? ` data-set-scope="${esc(scope)}"` : "";
  const idAttr = opts.id ? ` id="${esc(opts.id)}"` : "";
  return `<input${idAttr} data-set-input="${esc(key)}"${scAttr} value="${esc(value)}" placeholder="${esc(defVal || "empty")}" aria-label="${esc(label)}">`;
}

const kindUsesDefault = (kind) => kind === "launch_agents";

// Render a full setting row: label, control (select / checkboxes / row editor / input), reset button, and overrides.
export function settingRowHtml(k, d, ov, now, state, label, opts = {}) {
  const glob = ov.find(x => x.scope === "global"), rest = ov.filter(x => x.scope !== "global");
  const def = String(d[k] ?? "");
  const val = glob ? glob.value : "";
  const spec = settingSpec(k, state, "global", val);
  const wide = isWideKind(spec.kind);
  const ctl = settingControlHtml(k, val, def, label, state, "global");
  const raw = opts.rawText && (spec.kind === "agent_order" || spec.kind === "agent_rules" || spec.kind === "launch_agents")
    ? `<details class="set-raw t-s"><summary class="muted">Raw text</summary><input data-set-input="${esc(k)}" value="${esc(val)}" placeholder="${esc(def || "empty")}" aria-label="${esc(label)}"></details>`
    : "";
  const reset = glob && spec.kind !== "usage_list"
    ? iconButton("↺", "Reset to default", { data: { unset: k, scope: "global" } }) : "";
  const ovHtml = rest.length ? `<div class="set-ov">${rest.map(x => overrideHtml(k, x, def, label, state)).join("")}</div>` : "";
  return `<div class="set-row${wide ? " set-row-wide" : ""}" data-set-row="${esc(k)}">
    <div class="set-label">${esc(label)}<div class="muted t-s set-key">${esc(k)}${now[k] ? ` · now ${esc(now[k])}` : ""}</div></div>
    <div class="set-ctl">${ctl}${reset}</div>
    ${raw}${ovHtml}
  </div>`;
}

function overrideHtml(k, x, def, label, state) {
  const spec = settingSpec(k, state, x.scope, x.value);
  if (spec.kind === "select") {
    const ctl = selectHtml(k, x.scope, `${x.scope} ${label}`, spec, x.value, def);
    return `<div class="set-ov-row" data-ov-key="${esc(k)}" data-ov-scope="${esc(x.scope)}">${chip("c-p", esc(x.scope))}${ctl}${iconButton("✕", "Remove override", { data: { unset: k, scope: x.scope } })}</div>`;
  }
  if (spec.kind === "agent_order" || spec.kind === "agent_rules" || spec.kind === "launch_agents") {
    return `<div class="set-ov-row set-ov-block" data-ov-key="${esc(k)}" data-ov-scope="${esc(x.scope)}">
      <div class="set-ov-head">${chip("c-p", esc(x.scope))}${iconButton("✕", "Remove override", { data: { unset: k, scope: x.scope } })}</div>
      ${structuredEditorHtml(k, x.value, state, x.scope)}
    </div>`;
  }
  return chip("c-p", `${esc(x.scope)} = ${esc(x.value)} <span class="link" data-unset="${esc(k)}" data-scope="${esc(x.scope)}">×</span>`);
}

// --- Saving & reading ---

export function readEditorRows(box) {
  const kind = box.dataset.edKind;
  return [...box.querySelectorAll("[data-ed-row]")].map(r => {
    const get = (f) => { const el = r.querySelector(`[data-ed-field="${f}"]`); return el ? el.value : ""; };
    if (kind === "agent_order") return { agent: get("agent"), model: get("model"), effort: get("effort"), max: get("max").trim() };
    if (kind === "agent_rules") return { agent: get("agent"), patterns: get("patterns") };
    return { label: get("label"), agent: get("agent"), extra: get("extra") };
  });
}

export function formatEditorValue(box) {
  const kind = box.dataset.edKind, rows = readEditorRows(box);
  if (kind === "agent_order") return formatAgentOrder(rows);
  if (kind === "agent_rules") return formatAgentRules(rows);
  return formatLaunchAgents(rows);
}

export async function saveSettingValue(key, value, scope, state, label) {
  const sc = scope || "global";
  const ov = (state && state.settings && state.settings.overrides) || [];
  const cur = ov.find(x => x.key === key && x.scope === sc);
  if (value === (cur ? cur.value : "")) return false;
  const args = { key, ...scopeArgs(sc) };
  if (value) await act("config_set", { ...args, value });
  else await act("config_unset", args);
  const where = sc === "global" ? "" : ` (${sc})`;
  toast(value ? `${label}${where}: ${value}` : `${label}${where}: back to the default`);
  return true;
}

function nextOrderRow(rows, state) {
  const ch = choicesOf(state), agents = ch.agent_types || [];
  for (const a of agents) {
    if (!rows.some(r => r.agent === a && !r.model)) return { agent: a, model: "", effort: "", max: "" };
  }
  for (const a of agents) {
    for (const m of modelsForPlatform(state, a, "", ch.ladder_models || [])) {
      if (!rows.some(r => r.agent === a && r.model === m)) return { agent: a, model: m, effort: "", max: "" };
    }
  }
  return { agent: agents[0] || "", model: "", effort: "", max: "" };
}

function rerenderEditor(box, rows, state) {
  const kind = box.dataset.edKind;
  if (kind === "agent_order") box.innerHTML = orderEditorHtml(rows, state);
  else if (kind === "agent_rules") box.innerHTML = rulesEditorHtml(rows, state);
  else if (kind === "launch_agents") box.innerHTML = launchEditorHtml(rows, state);
}

// Wire change, keydown, click, and usage-list checkboxes for any container that renders settingRowHtml.
export function wireSettingControls(host, getState, labelFor, onChanged) {
  if (!host || host.dataset.scWired) return;
  host.dataset.scWired = "1";
  wireUsageListSettings(host, onChanged);

  const commitEditor = async (box, rows) => {
    const state = getState();
    if (box.dataset.edForm) { rerenderEditor(box, rows, state); return; }
    const key = box.dataset.setEditor, scope = box.dataset.setScope || "global", kind = box.dataset.edKind;
    let value = "";
    if (kind === "agent_order") value = formatAgentOrder(rows);
    else if (kind === "agent_rules") value = formatAgentRules(rows);
    else value = formatLaunchAgents(rows);
    try {
      await saveSettingValue(key, value, scope, state, labelFor(key));
    } catch (e) { /* act showed the error */ }
    if (onChanged) onChanged();
  };

  host.addEventListener("change", async (e) => {
    const t = e.target;
    if (t.dataset.setInput) {
      const state = getState(), key = t.dataset.setInput, scope = t.dataset.setScope || "global", v = t.value.trim();
      const ov = (state && state.settings && state.settings.overrides) || [];
      const cur = ov.find(x => x.key === key && x.scope === scope);
      if (t.tagName === "SELECT" && !cur && scope === "global" && v === String((state.settings.defaults || {})[key] ?? "")) return;
      try {
        await saveSettingValue(key, v, scope, state, labelFor(key));
      } catch (err) { t.value = cur ? cur.value : ""; }
      if (onChanged) onChanged();
      return;
    }
    if (t.dataset.edField) {
      const box = t.closest("[data-set-editor]");
      if (!box) return;
      const state = getState(), kind = box.dataset.edKind;
      const rows = readEditorRows(box);
      const rowEl = t.closest("[data-ed-row]");
      const i = rowEl ? +rowEl.dataset.edRow : -1;
      if (kind === "agent_order") {
        if (t.dataset.edField === "max" && t.value.trim() && !/^\d{1,4}$/.test(t.value.trim())) {
          toast("Max sessions: a whole number, or empty for no limit", true); return;
        }
        if (t.dataset.edField === "agent" && i >= 0) {
          rows[i].model = ""; rows[i].effort = "";
        }
      } else if (kind === "launch_agents" && t.dataset.edField === "agent" && i >= 0) {
        if (!rows[i].label.trim() && rows[i].agent !== CUSTOM) {
          const opt = launchOpt(state, rows[i].agent);
          rows[i].label = (opt && opt.label) || rows[i].agent;
        }
      }
      if (kind === "agent_rules" && i >= 0 && !rows[i].patterns.trim()) return;
      await commitEditor(box, rows);
    }
  });

  host.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.target.dataset.setInput || e.target.dataset.edField) && e.target.tagName === "INPUT") {
      e.preventDefault(); e.target.blur();
    }
  });

  host.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-ed-add],[data-ed-del],[data-ed-up],[data-ed-down]");
    if (!btn) return;
    const box = btn.closest("[data-set-editor]");
    if (!box) return;
    const state = getState(), kind = box.dataset.edKind;
    const agents = choicesOf(state).agent_types || [];
    const rows = readEditorRows(box);
    if (btn.dataset.edAdd) {
      if (kind === "agent_order") {
        const next = nextOrderRow(rows, state);
        rows.push(next);
        await commitEditor(box, rows);
      } else if (kind === "agent_rules") {
        rows.push({ agent: agents[0] || "", patterns: "" });
        rerenderEditor(box, rows, state);
        const last = box.querySelector(`[data-ed-row="${rows.length - 1}"] [data-ed-field="patterns"]`);
        if (last) last.focus();
      } else if (kind === "launch_agents") {
        const a = agents.find(x => !rows.some(r => r.agent === x)) || agents[0] || "";
        const opt = launchOpt(state, a);
        rows.push({ label: (opt && opt.label) || a, agent: a, extra: "" });
        await commitEditor(box, rows);
      }
      return;
    }
    const i = +(btn.dataset.edDel ?? btn.dataset.edUp ?? btn.dataset.edDown);
    if (btn.dataset.edDel !== undefined) {
      rows.splice(i, 1);
      await commitEditor(box, rows);
      return;
    }
    const up = btn.dataset.edUp !== undefined, j = up ? i - 1 : i + 1;
    if (j < 0 || j >= rows.length) return;
    [rows[i], rows[j]] = [rows[j], rows[i]];
    await commitEditor(box, rows);
  });
}
