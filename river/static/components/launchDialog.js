// The launch dialog: every button that starts an agent opens it first, to pick the agent, the model, the
// effort, the work (Start only), and under More the agent's launch options (a profile: Remote Control, permission
// or sandbox mode). Every agent starts in a pane of the tmux session maxpm (#5105; Windows: a console window), so
// there is no choice of where. The server builds the command (a profile) or fills {model} and {effort} in a custom
// one, and sets MAXPM_MODEL. The last choices stay in this browser; options that the person does not change are left
// to the server, so a project's own setting still applies.
import { $, esc, store } from "../lib.js";
import { makeDialog } from "./dialog.js";
import { button, iconButton } from "./button.js";
import { field, two, select } from "./form.js";

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
  const workSel = prev ? prev.work : (ctx.work || "");
  const it = workItem(workSel);
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
  const rows = startable(S), projects = [...new Set(rows.map(i => i.project))];
  const limits = it && (it.min_model || it.max_model) ? `#${it.id} allows ${[it.min_model && "at least " + it.min_model, it.max_model && "at most " + it.max_model].filter(Boolean).join(" and ")}.` : "";
  const other = it && it.agent && opt.agent_type && opt.agent_type !== it.agent
    ? `#${it.id} is for ${it.agent}; ${agent} runs ${opt.agent_type}, and you start it anyway.` : "";
  const rec = it && (it.model || it.effort) ? `#${it.id} recommends ${[it.model, it.effort && it.effort + " effort"].filter(Boolean).join(", ")}.` : "";
  const noPlace = !opt.takes_model || !opt.takes_effort
    ? `The ${agent} command has no ${[!opt.takes_model && "{model}", !opt.takes_effort && "{effort}"].filter(Boolean).join(" or ")} placeholder, so the agent starts with its own ${!opt.takes_model ? "model" : "effort"} (MaximizePM still records the model you pick). Add it in Settings: launch_agents, or use a profile (@claude-code, @codex).` : "";
  const note = [other, rec, limits, noPlace].filter(Boolean).join(" ");
  const work = ctx.pickWork ? field("Work", `<select id="lWork">
          <option value="">${S.start_next ? `Next: #${S.start_next.id} ${esc(S.start_next.title)} (${esc(S.start_next.why || S.start_next.project)})` : "Nothing is ready"}</option>
          ${projects.length > 1 ? `<optgroup label="Project (its most important ready item)">${projects.map(p => `<option value="p:${esc(p)}" ${workSel === "p:" + p ? "selected" : ""}>${esc(p)}</option>`).join("")}</optgroup>` : ""}
          <optgroup label="Item">${rows.slice(0, 30).map(i => `<option value="i:${i.id}" ${workSel === "i:" + i.id ? "selected" : ""}>#${i.id} ${esc(i.title)}</option>`).join("")}</optgroup>
        </select>`) : `<div class="ld-work"><span class="muted">Work:</span> ${esc(ctx.what || "")}</div>`;
  // The model's name only: the ladder's note on each model is its tooltip.
  const modelSel = `<select id="lModel"><option value="">the agent's default</option>${models.map(m =>
    `<option value="${esc(m.name)}" ${m.name === model ? "selected" : ""}${m.note ? ` title="${esc(m.note)}"` : ""}>${esc(m.name)}</option>`).join("")}</select>`;
  const more = optionFields(opt);
  const box = $("#launchDlg .box");
  box.innerHTML = `
    <div class="dlg-head">
      <h2 id="launchTitle">${esc(ctx.title || "Start an agent")}</h2>
      ${iconButton("✕", "Close", { data: { "launch-cancel": "1" } })}
    </div>
    <div class="form ld-form">
      ${work}
      ${field("Agent", select({ id: "lAgent" }, opts.map(o => o.label), agent))}
      ${two(field("Model", modelSel), field("Effort", select({ id: "lEffort" }, [["", "the agent's default"], ...opt.efforts.map(e => [e, e])], effort)))}
      ${note ? `<div class="muted t-s">${esc(note)}</div>` : ""}
      ${S.tmux === false ? `<div class="ld-warn t-s">Every agent starts in tmux, and tmux is not installed on this computer: ${esc(S.tmux_install || "brew install tmux")}</div>` : ""}
      ${more ? `<details class="ld-more"><summary>More</summary>${more}</details>` : ""}
    </div>
    <div class="dlg-foot">
      ${button("Cancel", { data: { "launch-cancel": "1" } })}
      ${button("▶ " + esc(ctx.go || "Start"), { kind: "primary", data: { "launch-go": "1" } })}
    </div>`;
}

// A profile's options for this one launch: a toggle, or a choice with the CLI's own default first.
// Preset from the settings; only the ones the person changes go to the server.
export function optionFields(opt) {
  const os = (opt && opt.options) || [];
  if (!os.length) return "";
  const name = (o) => o.name.replace(/_/g, " ").replace(/^./, c => c.toUpperCase());
  return `<div class="ld-opts">${os.map(o => o.kind === "toggle"
    ? `<label class="ld-opt" title="${esc(o.text)}"><input type="checkbox" data-lopt="${esc(o.name)}" ${o.value === "on" ? "checked" : ""}>${esc(name(o))}</label>`
    : `<label class="ld-opt" title="${esc(o.text)}">${esc(name(o))}<select data-lopt="${esc(o.name)}">
        <option value="" ${!o.value ? "selected" : ""}>the agent's own</option>${o.choices.map(c => `<option ${c === o.value ? "selected" : ""}>${esc(c)}</option>`).join("")}</select></label>`).join("")}</div>`;
}

// Open the dialog. c: {title, go (button label), what (the work, as text), item (limits and recommendation),
// pickWork (Start: choose the work)}. Resolves to {agent, model, effort, work, options} or null (cancel).
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
  for (const k of ["model", "effort"]) if (v[k]) out[k] = v[k];
  if (v.options && Object.keys(v.options).length) out.options = v.options;
  return out;
}
