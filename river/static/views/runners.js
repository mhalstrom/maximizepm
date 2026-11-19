// Deploy > Runners (#6151; canvas deploy-tab-mockup pictures 3 and 14): the shared list of places where an agent works
// on a pipeline's task with no person, no terminal and no prompt. Each runner: what it is (its SSH target, system,
// Tailscale address), its agent CLIs, when it answered last, its load, and the pipelines that use it ("Used by" opens
// that pipeline's Triggers tab, where it ticks its runners). Below: the agents that run or ran on runners, with Watch
// (what the agent printed, in words) and Stop. A forge's own CI (#6152, GitHub Actions) is a row too: its agent job
// starts in the forge when a stage's workflow fails, and its runs show with the others. Data: api/deploy's runners (river/runners.py listing); the log from GET
// api/runner-log/<run>; actions runner_add, runner_check, runner_stop (a person or the manager).
import { esc, getJson, ago, act, toast } from "../lib.js";
import { button, iconButton } from "../components/button.js";
import { chip } from "../components/chip.js";
import { empty } from "../components/empty.js";
import { makeDialog } from "../components/dialog.js";
import { field, input, actions } from "../components/form.js";

const STATE = { queued: ["c-waiting", "waits"], running: ["c-prog", "runs"], ended: ["c-done", "ended"],
  failed: ["c-warn", "failed"], stopped: ["c-waiting", "stopped"] };
let dlg = null, box = null, watching = null, timer = null;

function whatItIs(r) {
  const how = r.tailnet ? `over Tailscale (${esc(r.tailnet)})` : "over SSH";
  return `${esc(r.ssh)} · ${esc(r.system || "system not known")} · ${how}${r.version ? ` · ${esc(r.version)}` : ""}`;
}

function runnerRow(r, clis) {
  const agents = Object.entries(r.clis).map(([k, v]) => `${esc((clis[k] || {}).label || k)}${k === r.cli ? " ✓" : ""} <span class="muted t-s">${esc(v)}</span>`).join("<br>")
    || `<span class="c-warn">no agent CLI</span>`;
  const used = r.used_by.map(p => button(esc(p), { cls: "plain", data: { "dp-triggers": p }, title: "Open its triggers" })).join(" ")
    || `<span class="muted">none</span>`;
  const state = r.state === "bad" ? `<span class="c-warn">✕ ${esc(r.error || "does not answer")}</span>`
    : `<span class="dot active"></span> ${r.last_seen ? esc(ago(r.last_seen)) : "never"}`;
  // #6153: the sets that every run here gets (the model key), by name
  const keys = (r.keys || []).map(k => `${k.set} (${k.secrets.join(", ") || "empty"})`).join("; ");
  return `<tr><td><b>${esc(r.name)}</b></td><td class="t-s" data-label="What it is">${whatItIs(r)}
    <br>${keys ? `🔒 ${esc(keys)}` : `<span class="muted">no keys of its own</span>`}</td>
    <td class="t-s" data-label="Agents">${agents}</td><td class="t-s" data-label="Last seen">${state}</td>
    <td class="t-s" data-label="Load">${r.busy} of ${r.slots}${r.waiting ? `, ${r.waiting} wait` : ""}<br><span class="muted">at most ${r.max_minutes}m, $${r.max_cost}</span></td>
    <td data-label="Used by">${used}</td><td>${button("Check", { data: { "rn-check": r.name }, title: "Ask the server again" })}</td></tr>`;
}

// A forge's own CI (#6152): GitHub starts the agent in its job when a stage's workflow fails; no slots, no Check.
function forgeRow(f) {
  const used = f.used_by.map(p => button(esc(p), { cls: "plain", data: { "dp-triggers": p }, title: "Open its triggers" })).join(" ")
    || `<span class="muted">no rule yet: maxpm runner ${esc(f.name)} &lt;pipeline&gt;</span>`;
  return `<tr><td><b>${esc(f.label)}</b></td><td class="t-s" data-label="What it is">${esc(f.what)}</td>
    <td class="t-s" data-label="Agents">Claude Code, Codex</td>
    <td class="t-s" data-label="Last seen">${f.last_seen ? `<span class="dot active"></span> ${esc(ago(f.last_seen))}` : "never"}</td>
    <td class="t-s" data-label="Load">starts per failure${f.busy || f.waiting ? `<br>${f.busy} run, ${f.waiting} wait` : ""}</td>
    <td data-label="Used by">${used}</td><td></td></tr>`;
}

function runRow(x) {
  const [cls, word] = STATE[x.state] || ["c-waiting", x.state];
  const live = ["queued", "running"].includes(x.state);
  return `<tr><td class="mono">#${x.id} ${chip(cls, word)} ${esc(x.runner)}</td>
    <td data-label="Task">${button("#" + x.item, { cls: "plain", data: { "dp-card": x.item }, title: "Open the task" })}</td>
    <td class="t-s" data-label="Started by">${x.pipeline ? `${esc(x.pipeline)} ${esc(x.rule || "")}` : "by hand"}</td>
    <td class="t-s" data-label="Since" title="${esc(x.started_at || x.queued_at)}">${esc(ago(x.started_at || x.queued_at))}</td>
    <td class="t-s" data-label="Cost">${x.cost != null ? "$" + Number(x.cost).toFixed(2) : "not known"}</td>
    <td class="t-s rn-result"${x.result ? ` data-label="Result"` : ""}>${esc(x.result || "")}</td>
    <td class="rn-acts">${x.started_at ? button("Watch", { data: { "rn-watch": x.id }, title: "What the agent printed" }) : ""}
      ${live ? button("Stop", { kind: "warn", data: { "rn-stop": x.id }, title: "Stop this agent" }) : ""}</td></tr>`;
}

// The Runners part of Deploy, from the data of api/deploy.
export function runnersHtml(data) {
  const R = data.runners || { runners: [], this: { rules: [] }, clis: {} };
  const forges = R.forges || [];
  const rows = R.runners.map(r => runnerRow(r, R.clis)).join("") + forges.map(forgeRow).join("");
  const thisRules = R.this.rules.map(x => `${esc(x.pipeline)} ${esc(x.rule)}`).join(", ");
  const here = `<tr><td><b>This computer</b></td><td class="t-s" data-label="What it is">maxpm serve · a session in a terminal, with the agent CLI you signed in</td>
    <td class="t-s" data-label="Agents">every agent CLI</td><td class="t-s" data-label="Last seen"><span class="dot active"></span> now</td>
    <td class="t-s" data-label="Load">max_sessions</td>
    <td class="t-s" data-label="Used by">${thisRules || `<span class="muted">rules that name no runner</span>`}</td><td></td></tr>`;
  const runs = R.runners.concat(forges).flatMap(r => r.runs).sort((a, b) => b.id - a.id);
  return `<div class="pl-bar"><b>Runners</b><span class="muted t-s">places where an agent runs with no person · shared by the pipelines</span>
      <span class="spacer"></span>${button("Add a runner", { kind: "primary", data: { "rn-add": 1 }, title: "Add a server" })}</div>
    <div class="pl-table"><table class="pl-blocks"><tr><th>Runner</th><th>What it is</th><th>Agents</th><th>Last seen</th><th>Load</th><th>Used by</th><th></th></tr>
      ${rows}${here}</table></div>
    ${R.runners.length || forges.length ? "" : empty("No runner yet. Add a server that you reach with ssh: MaximizePM installs itself there and runs the agents of the trigger rules that name it.")}
    <h3 class="tg-h">Agents on runners</h3>
    ${runs.length ? `<div class="pl-table"><table class="pl-blocks"><tr><th>Run</th><th>Task</th><th>Started by</th><th>Since</th><th>Cost</th><th>Result</th><th></th></tr>
      ${runs.map(runRow).join("")}</table></div>` : `<div class="muted t-m">No agent ran on a runner yet.</div>`}
    <div class="muted t-s">A pipeline ticks the runners it may use on its Triggers tab; a rule there names one. The agent's model key: a credential set granted to the runner, else the runner's own:
      ${Object.values(R.clis).map(c => `${esc(c.label)}: ${esc(c.key)}`).join("; ")}.</div>
    <pre class="rn-cmds mono">maxpm runner add ci-1 --ssh ci@ci-1        # checks the server, installs maxpm there
maxpm runner allow ci-1 &lt;pipeline&gt;          # the pipeline's trigger rules may run their agents there
maxpm secret grant model-keys --runner ci-1  # every run there gets the set (never a person's sign-in)</pre>`;
}

// ---- the dialogs: Add a runner, Watch a run

function dialog() {
  if (dlg) return;
  const el = document.createElement("div");
  el.id = "rnDlg"; el.className = "hidden";
  el.innerHTML = '<div class="box"></div>';
  document.body.appendChild(el);
  box = el.firstElementChild;
  dlg = makeDialog(el, { onClose: () => { watching = null; clearTimeout(timer); } });
}

function openAdd(refresh) {
  dialog();
  box.innerHTML = `<div class="dlg-head"><h2>Add a runner</h2>${iconButton("✕", "Close", { data: { "rn-close": 1 } })}</div>
    <div class="form">
      ${field("Name", input({ id: "rnName", placeholder: "ci-1" }))}
      ${field("SSH target", input({ id: "rnSsh", placeholder: "ci@ci-1" }), ` <span class="muted t-s">Tailscale SSH or your key; no password prompt</span>`)}
    </div>
    <div class="muted t-s">MaximizePM checks the server, installs this computer's maxpm there, and finds its agent CLIs. This takes up to a few minutes.</div>
    ${actions(button("Cancel", { data: { "rn-close": 1 } }) + button("Add", { kind: "primary", data: { "rn-save": 1 } }))}`;
  box.onclick = async (e) => {
    if (e.target.closest("[data-rn-close]")) return dlg.close();
    if (!e.target.closest("[data-rn-save]")) return;
    const name = box.querySelector("#rnName").value.trim(), ssh = box.querySelector("#rnSsh").value.trim();
    if (!name || !ssh) return toast("Give a name and an SSH target", true);
    toast(`Setting up ${name}…`);
    try { await act("runner_add", { name, ssh }); } catch (err) { return; }
    toast(`Runner ${name} added`);
    dlg.close();
    refresh();
  };
  dlg.open();
  box.querySelector("#rnName").focus();
}

function logHtml(d) {
  const r = d.run, [cls, word] = STATE[r.state] || ["c-waiting", r.state];
  const lines = d.lines.map(x => `<div class="rn-line rn-${esc(x.kind)}"><span class="muted">${esc(x.kind)}</span> ${esc(x.text)}</div>`).join("")
    || `<div class="muted">Nothing printed yet.</div>`;
  return `<div class="dlg-head"><h2>Run #${r.id} on ${esc(r.runner)} · task #${r.item}</h2>${iconButton("✕", "Close", { data: { "rn-close": 1 } })}</div>
    <div class="t-m">${chip(cls, word)} ${esc(r.agent)}${r.pipeline ? ` · ${esc(r.pipeline)} ${esc(r.rule || "")}` : ""}${r.cost != null ? ` · $${Number(r.cost).toFixed(2)}` : ""}</div>
    <div class="rn-log mono">${lines}</div>
    ${r.result ? `<div class="t-m"><b>Result</b> ${esc(r.result)}</div>` : ""}`;
}

async function watch(id) {
  dialog();
  watching = id;
  const draw = async () => {
    let d;
    try { d = await getJson("api/runner-log/" + id); } catch (e) { box.innerHTML = empty(esc(e.message)); return; }
    if (watching !== id) return;
    const log = box.querySelector(".rn-log"), end = !log || log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
    box.innerHTML = logHtml(d);
    const now = box.querySelector(".rn-log");
    if (now && end) now.scrollTop = now.scrollHeight;
    if (["queued", "running"].includes(d.run.state)) timer = setTimeout(draw, 3000);
  };
  box.onclick = (e) => { if (e.target.closest("[data-rn-close]")) dlg.close(); };
  box.innerHTML = `<div class="muted">Reading…</div>`;
  dlg.open();
  draw();
}

// A click in Deploy on a [data-rn-*] control; refresh draws Deploy again after a change.
export function runnerClick(t, refresh) {
  const d = t.dataset;
  if (d.rnAdd) return openAdd(refresh);
  if (d.rnWatch) return watch(+d.rnWatch);
  if (d.rnCheck) return act("runner_check", { name: d.rnCheck }).then(() => { toast(`Checked ${d.rnCheck}`); refresh(); }, () => {});
  if (d.rnStop) return act("runner_stop", { run: +d.rnStop, reason: "stopped on the page" })
    .then(() => { toast(`Run #${d.rnStop} stops`); refresh(); }, () => {});
}

// The ticks on a pipeline's Triggers tab: which runners its rules may use.
export function ticksHtml(d) {
  const all = d.all_runners || [];
  if (!all.length) return `<div class="muted t-m">No runner yet: Deploy › Runners adds one. Until then the agents run on this computer.</div>`;
  return `<div class="pl-table"><table><tr><th>Uses</th><th>Runner</th><th>What it is</th></tr>
    ${all.map(r => `<tr><td>${button(r.ticked ? "✓ Uses" : "Use", { pressed: r.ticked, data: { "rn-tick": r.name, on: r.ticked ? "" : "1" },
      title: r.ticked ? "Stop using it" : "Let its rules use it" })}</td><td><b>${esc(r.name)}</b></td>
      <td class="t-s">${esc(r.ssh)} · ${esc(r.cli)}${r.state === "bad" ? ` · <span class="c-warn">does not answer</span>` : ""}</td></tr>`).join("")}</table></div>`;
}

export async function tick(pipeline, name, on, refresh) {
  try { await act("runner_allow", { pipeline, name, on }); } catch (e) { return; }
  toast(on ? `${pipeline} may use ${name}` : `${pipeline} no longer uses ${name}`);
  refresh();
}
