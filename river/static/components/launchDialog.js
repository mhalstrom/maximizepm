// The launch dialog: every button that starts an agent opens it first, to pick the agent, the model, the
// effort, the agent's launch options (a profile: Remote Control, permission or sandbox mode), the work
// (Start only), and a new tab, a new window, or tmux. The server builds the command (a profile) or fills {model} and
// {effort} in a custom one, and sets MAXPM_MODEL. The last choices stay in this browser; options that
// the person does not change are left to the server, so a project's own setting still applies.
import { $, esc, store } from "../lib.js";
import { makeDialog } from "./dialog.js";

// Whether a model may take an item with limits lo/hi (comma lists), as core.model_check: a limit compares
// only models of the same family, and a model outside the ladder is not limited.
export function modelAllowed(ladder, m, lo, hi) {
  const fam = Object.keys(ladder || {}).find(f => ladder[f].includes(m));
  if (!fam) return true;
  const pos = ladder[fam].indexOf(m);
  const same = (lim) => String(lim || "").split(",").map(x => x.trim()).find(x => ladder[fam].includes(x));
  const a = same(lo), b = same(hi);
  return !(a && pos < ladder[fam].indexOf(a)) && !(b && pos > ladder[fam].indexOf(b));
}

// The ready items an agent can start on, most important first (S.items is in queue order).
export function startable(S) {
  return (S.items || []).filter(i => i.ready && i.doer !== "human" && !["deploy", "review", "monitor"].includes(i.kind)
    && !i.reserved_for && !i.project_archived);
}

// A model as the ladder names it: a CLI id from the model_ids settings (gpt-6-astra) becomes its name (astra).
export function ladderName(ids, m) {
  for (const byName of Object.values(ids || {}))
    for (const [name, id] of Object.entries(byName)) if (id.toLowerCase() === String(m || "").toLowerCase()) return name;
  return m;
}

// The agent to preselect for an item: the first of the item's agent type, else of its model's family.
export function pickAgent(opts, it, ladder, ids) {
  if (!it) return null;
  if (it.agent) { const o = opts.find(o => o.agent_type === it.agent); if (o) return o.label; }
  const m = ladderName(ids, it.model);
  const fam = m && Object.keys(ladder || {}).find(f => ladder[f].includes(m));
  const o = fam && opts.find(o => o.family === fam);
  return o ? o.label : null;
}

// The model to preselect for an agent option on an item: the item's recommendation, else the weakest its
// min limit allows, else its max limit (not the bottom of the ladder), else the last choice here, else the
// agent's own default (""). Only limits that name a model of this agent's family count.
export function pickModel(opt, it, ladder, last, ids) {
  const ok = (m) => opt.models.some(x => x.name === m) && (!it || modelAllowed(ladder, m, it.min_model, it.max_model));
  const rec = it && ladderName(ids, it.model);
  if (rec && ok(rec)) return rec;
  const mine = (lim) => String(lim || "").split(",").map(x => x.trim()).find(x => opt.models.some(m => m.name === x));
  if (it && mine(it.min_model)) {
    const first = opt.models.find(x => ok(x.name));
    if (first) return first.name;
  } else if (it && ok(mine(it.max_model))) return mine(it.max_model);
  return last && ok(last) ? last : "";
}

// The effort to preselect: the item's recommendation when the agent's CLI takes it ("max" becomes the
// CLI's highest level), else the last choice here, else the agent's default ("").
export function pickEffort(opt, it, last) {
  const e = it && it.effort;
  if (e && opt.efforts.includes(e)) return e;
  if (e === "max" && opt.efforts.length) return opt.efforts[opt.efforts.length - 1];
  return last && opt.efforts.includes(last) ? last : "";
}

let dlg = null, resolve = null, ctx = null, S = null;

function el() {
  let d = $("#launchDlg");
  if (d) return d;
  d = document.createElement("div");
  d.id = "launchDlg"; d.className = "hidden";
  d.setAttribute("role", "dialog"); d.setAttribute("aria-modal", "true"); d.setAttribute("aria-labelledby", "launchTitle");
  d.innerHTML = '<div class="box"></div>';
  document.body.appendChild(d);
  dlg = makeDialog(d, { onClose: () => finish(null) });
  d.addEventListener("click", (e) => {
    if (e.target === d) return;  // the overlay: makeDialog closes it (#5027)
    if (e.target.closest("[data-launch-cancel]")) return dlg.close();
    if (e.target.closest("[data-launch-go]")) {
      const v = values();
      store("river.launch.agent", v.agent);
      const fam = famOf(v.agent);
      store("river.launch.model." + fam, v.model);
      store("river.launch.effort." + fam, v.effort);
      store("river.launch.in", v.launch_in);
      finish(v);
      dlg.close();
    }
  });
  d.addEventListener("change", (e) => {
    if (e.target.id === "lAgent") draw(values());
    else if (e.target.id === "lWork") draw({ ...values(), agent: null });  // the new work's agent type picks again
    else if (e.target.dataset && e.target.dataset.lopt) e.target.dataset.changed = "1";
  });
  return d;
}

function finish(v) { const r = resolve; resolve = null; if (r) r(v); }
function famOf(agent) { const o = (S.launch_options || []).find(x => x.label === agent); return (o && o.family) || "any"; }

function values() {
  const d = $("#launchDlg"), q = (s) => d.querySelector(s);
  return { agent: q("#lAgent") ? q("#lAgent").value : (S.launch_options[0] || {}).label,
           model: q("#lModel") ? q("#lModel").value : "", effort: q("#lEffort") ? q("#lEffort").value : "",
           launch_in: (d.querySelector('input[name="lIn"]:checked') || {}).value || "tab",
           work: q("#lWork") ? q("#lWork").value : "",
           options: Object.fromEntries([...d.querySelectorAll("[data-lopt][data-changed]")].map(x =>
             [x.dataset.lopt, x.type === "checkbox" ? (x.checked ? "on" : "off") : x.value])) };
}

// The item the work choice points at (its recommendation and limits pick the model).
function workItem(work) {
  if (!ctx.pickWork) return ctx.item || null;
  const rows = startable(S);
  if (work.startsWith("i:")) return rows.find(i => i.id === +work.slice(2)) || null;
  if (work.startsWith("p:")) return rows.find(i => i.project === work.slice(2)) || null;
  const nx = S.start_next;  // the server's choice: first a project with ready work and no agent yet
  return (nx && rows.find(i => i.id === nx.id)) || rows[0] || null;
}

function draw(prev) {
  const opts = S.launch_options || [];
  const work = prev ? prev.work : (ctx.work || "");
  const it = workItem(work);
  // An item preselects the first agent of its agent type, else of its model's family; a choice here wins.
  const typed = pickAgent(opts, it, S.model_ladder, S.model_ids);
  const agent = prev && prev.agent && opts.some(o => o.label === prev.agent) ? prev.agent
    : typed ? typed
    : opts.some(o => o.label === store("river.launch.agent")) ? store("river.launch.agent") : (opts[0] || {}).label;
  const opt = opts.find(o => o.label === agent) || { models: [], efforts: [] };
  const fam = opt.family || "any";
  const ladder = S.model_ladder || {};
  const models = opt.models.filter(m => !it || modelAllowed(ladder, m.name, it.min_model, it.max_model));
  const model = pickModel(opt, it, ladder, store("river.launch.model." + fam), S.model_ids);
  const effort = pickEffort(opt, it, store("river.launch.effort." + fam));
  const where = (prev && prev.launch_in) || store("river.launch.in") || S.launch_in || "tab";
  const rows = startable(S), projects = [...new Set(rows.map(i => i.project))];
  const limits = it && (it.min_model || it.max_model) ? `#${it.id} allows ${[it.min_model && "at least " + it.min_model, it.max_model && "at most " + it.max_model].filter(Boolean).join(" and ")}.` : "";
  const other = it && it.agent && opt.agent_type && opt.agent_type !== it.agent
    ? `#${it.id} is for ${it.agent}; ${agent} runs ${opt.agent_type}, and you start it anyway.` : "";
  const rec = it && (it.model || it.effort) ? `#${it.id} recommends ${[it.model, it.effort && it.effort + " effort"].filter(Boolean).join(", ")}.` : "";
  const box = $("#launchDlg .box");
  box.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:baseline;gap:8px">
      <h2 id="launchTitle" style="margin:0">${esc(ctx.title || "Start an agent")}</h2>
      <button class="iconbtn" data-launch-cancel="1" title="Close">✕</button>
    </div>
    <div class="form" style="margin-top:10px">
      ${ctx.pickWork ? `<label>Work<select id="lWork">
          <option value="">${S.start_next ? `Next: #${S.start_next.id} ${esc(S.start_next.title)} (${esc(S.start_next.why || S.start_next.project)})` : "Nothing is ready"}</option>
          ${projects.length > 1 ? `<optgroup label="Project (its most important ready item)">${projects.map(p => `<option value="p:${esc(p)}" ${work === "p:" + p ? "selected" : ""}>${esc(p)}</option>`).join("")}</optgroup>` : ""}
          <optgroup label="Item">${rows.slice(0, 30).map(i => `<option value="i:${i.id}" ${work === "i:" + i.id ? "selected" : ""}>#${i.id} ${esc(i.title)}</option>`).join("")}</optgroup>
        </select></label>` : `<div><span class="muted">Work:</span> ${esc(ctx.what || "")}</div>`}
      <label>Agent<select id="lAgent">${opts.map(o => `<option ${o.label === agent ? "selected" : ""}>${esc(o.label)}</option>`).join("")}</select></label>
      <div class="two">
        <label>Model<select id="lModel"><option value="">the agent's default</option>${models.map(m =>
          `<option value="${esc(m.name)}" ${m.name === model ? "selected" : ""}>${esc(m.name)}${m.note ? " · " + esc(m.note) : ""}</option>`).join("")}</select></label>
        <label>Effort<select id="lEffort"><option value="">the agent's default</option>${opt.efforts.map(e =>
          `<option ${e === effort ? "selected" : ""}>${esc(e)}</option>`).join("")}</select></label>
      </div>
      ${optionFields(opt)}
      <div class="muted" style="font-size:12px">${esc([other, rec, limits].filter(Boolean).join(" "))}
        ${!opt.takes_model || !opt.takes_effort ? ` The ${esc(agent)} command has no ${[!opt.takes_model && "{model}", !opt.takes_effort && "{effort}"].filter(Boolean).join(" or ")} placeholder, so the agent starts with its own ${!opt.takes_model ? "model" : "effort"} (MaximizePM still records the model you pick). Add it in Settings: launch_agents, or use a profile (@claude-code, @codex).` : ""}</div>
      <div class="actions" style="align-items:center"><span class="muted" style="font-size:12px">Open in:</span>
        <label style="display:flex;gap:4px;align-items:center"><input type="radio" name="lIn" value="tab" ${where === "tab" ? "checked" : ""}>a new tab</label>
        <label style="display:flex;gap:4px;align-items:center"><input type="radio" name="lIn" value="window" ${where === "window" ? "checked" : ""}>a new window</label>
        ${S.tmux || where === "tmux" ? `<label style="display:flex;gap:4px;align-items:center" title="A pane of the tmux session maxpm. Run maxpm view in a terminal to see every agent side by side."><input type="radio" name="lIn" value="tmux" ${where === "tmux" ? "checked" : ""}>tmux</label>` : ""}</div>
    </div>
    <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:12px">
      <button class="btn" data-launch-cancel="1">Cancel</button>
      <button class="btn primary" data-launch-go="1">▶ ${esc(ctx.go || "Start")}</button>
    </div>`;
}

// A profile's options for this one launch: a toggle, or a choice with the CLI's own default first.
// Preset from the settings; only the ones the person changes go to the server.
export function optionFields(opt) {
  const os = (opt && opt.options) || [];
  if (!os.length) return "";
  const name = (o) => o.name.replace(/_/g, " ").replace(/^./, c => c.toUpperCase());
  return `<div class="actions" style="align-items:center;flex-wrap:wrap;gap:10px">${os.map(o => o.kind === "toggle"
    ? `<label style="display:flex;gap:4px;align-items:center" title="${esc(o.text)}"><input type="checkbox" data-lopt="${esc(o.name)}" ${o.value === "on" ? "checked" : ""}>${esc(name(o))}</label>`
    : `<label style="display:flex;gap:4px;align-items:center" title="${esc(o.text)}">${esc(name(o))}<select data-lopt="${esc(o.name)}">
        <option value="" ${!o.value ? "selected" : ""}>the agent's own</option>${o.choices.map(c => `<option ${c === o.value ? "selected" : ""}>${esc(c)}</option>`).join("")}</select></label>`).join("")}</div>`;
}

// Open the dialog. c: {title, go (button label), what (the work, as text), item (limits and recommendation),
// pickWork (Start: choose the work)}. Resolves to {agent, model, effort, launch_in, work} or null (cancel).
export function chooseLaunch(state, c) {
  S = state; ctx = c || {};
  el();
  if (resolve) finish(null);
  draw(null);
  dlg.open();
  const first = $("#launchDlg [data-launch-go]"); if (first) first.focus();
  return new Promise(r => { resolve = r; });
}

// The choices as action arguments (empty ones left out, so the server uses its defaults).
export function launchArgs(v) {
  const out = { agent: v.agent };
  for (const k of ["model", "effort", "launch_in"]) if (v[k]) out[k] = v[k];
  if (v.options && Object.keys(v.options).length) out.options = v.options;
  return out;
}
