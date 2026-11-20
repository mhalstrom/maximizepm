// The first screen of a deploy target's page in Deploy (#6486, mark 2026-11-20: 'the deploy process here ... look
// like garbage and makes no sense', 'I can't figure out how to use it'). It answers, from the top: what the next
// release ships and when it goes out by itself (Release now); how the release that runs, or the last one, goes, step
// by step as the deploy hook prints them ('::step' lines), with its GitHub release, or the step that failed and its
// fix task; and the GitHub tests of the branch that goes out. The graph of stages and transitions, the history and
// the audit log are behind More (views/deploy.js tabs Controls, History, Audit). Words: the table of canvas
// deploy-text-audit that mark approved (#6503): a label is one to three words, a state is a word and a time.
// Reads: D from GET api/pipeline/<target> (river/pipeline/targets.py default_pipeline: latest, checks, release) and
// the target's release plan in S.targets (release: ships, until, early, running).
import { esc, ago } from "../lib.js";
import { button } from "../components/button.js";
import { S } from "../page.js";

const plural = (n, one) => `${n} ${one}${n === 1 ? "" : "s"}`;
const secs = (n) => n == null ? "" : n < 90 ? `${Math.round(n)}s` : n < 5400 ? `${Math.round(n / 60)}m` : `${Math.round(n / 3600)}h`;
const clock = (n) => {
  const s = Math.max(0, Math.floor(n)), pad2 = (v) => String(v).padStart(2, "0");
  return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m ${pad2(s % 60)}s` : `${Math.floor(s / 3600)}h ${pad2(Math.floor(s / 60) % 60)}m`;
};
// A time that runs: pipeline.js tick() counts each [data-pl-tick] in #plBody every second.
const runFor = (iso) => `<span data-pl-tick="${esc(iso)}">${clock((Date.now() - Date.parse(iso)) / 1000)}</span>`;
// The browser's time zone and a 24-hour clock (#6206).
const when = (iso) => new Date(iso).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const st = (s, word) => `<span class="pl-st ${esc(s || "none")}">● ${esc(word || s || "")}</span>`;
const link = (data, text) => `<span class="link" ${data}>${esc(text)}</span>`;
const task = (id, text) => link(`data-open="${id}"`, text || `#${id}`);
const report = (id) => link(`data-report="${id}"`, "Report");
const github = (url) => url ? `<a class="link" href="${esc(url)}" target="_blank" rel="noopener">GitHub</a>` : "";
const label = (name) => { const s = String(name || "").replace(/[-_]/g, " "); return s.charAt(0).toUpperCase() + s.slice(1); };

export const targetOf = (D) => (S.targets || []).find(t => t.name === D.name) || null;

// The steps of a release in their order: the deploy hook's, the person's yes, the publish run's, then the watch. A
// step that ran again (the hook runs again after a fix) shows once, as its newest run.
function steps(L) {
  const runs = L.runs || {}, out = [];
  const add = (list) => { for (const x of list || []) { const i = out.findIndex(y => y.name === x.name); if (i >= 0) out.splice(i, 1); out.push(x); } };
  add((runs.build || {}).step_list);
  add((runs.deploy || {}).step_list);
  const a = runs.approval;
  if (a) out.push({ name: "approve", state: a.state, started_at: a.started_at, ended_at: a.ended_at, item: a.item, approve: a.state === "queued" });
  add((runs.publish || {}).step_list);
  const w = runs.watch;
  if (w && w.state !== "skipped") out.push({ name: "checks", state: w.state, started_at: w.started_at, ended_at: w.ended_at,
    note: w.errors, watch: true });
  return out;
}

// The state of the release as a word and a time, and the step where it stopped.
function stateOf(L, list) {
  const failed = list.find(x => x.state === "failed" && !x.watch), running = list.find(x => x.state === "running");
  const yes = list.find(x => x.approve);
  if (L.status === "done") return { s: "passed", word: `passed ${ago(L.closed_at)}` };
  if (yes) return { s: "queued", word: "waiting for approval", yes };
  if (running) return { s: "running", word: label(running.name), running };
  if (failed) return { s: "failed", word: `failed at ${label(failed.name)}`, failed };
  const dep = (L.runs || {}).deploy || {};
  if (dep.state === "failed") return { s: "failed", word: "failed" };
  return { s: "queued", word: "starting" };
}

function stepRow(x) {
  const t = x.state === "running" && x.started_at ? runFor(x.started_at)
    : x.seconds != null ? esc(secs(x.seconds)) : x.ended_at && !x.watch ? esc(ago(x.ended_at)) : "";
  const note = x.state === "failed" && x.note ? `<div class="rl-note t-s">${esc(x.note)}</div>` : "";
  return `<li class="rl-step ${esc(x.state)}"><span class="pl-dot ${esc(x.state)}" aria-hidden="true"></span>
    <span class="rl-sname">${esc(label(x.name))}</span> <span class="pl-st ${esc(x.state)}">${esc(x.state === "queued" ? "waiting" : x.state)}</span>
    <span class="muted t-s">${t}</span>${note}</li>`;
}

// The release that runs now, or the last one (D.latest).
function lastCard(D) {
  const L = D.latest;
  if (!L) return `<section class="pl-card rl-card"><h3>Last release</h3><div class="muted t-m">No releases yet.</div></section>`;
  const list = steps(L), S0 = stateOf(L, list);
  const title = S0.s === "running" ? "Releasing" : L.status === "done" ? "Last release" : "This release";
  const links = [github(L.url), report(L.deploy), L.fix ? task(L.fix.id, `Fix #${L.fix.id}`) : ""].filter(Boolean).join(" · ");
  const yes = S0.yes ? button("Approve", { kind: "primary", data: { "pl-yes": S0.yes.item, dep: L.deploy, ver: L.tag || "" }, title: "Approve" })
    + button("Details", { data: { "dp-card": S0.yes.item }, title: "Details" }) : "";
  return `<section class="pl-card rl-card" id="rlLast"><div class="rl-head"><h3>${title}${L.tag ? ` <span class="pl-ver">${esc(L.tag)}</span>` : ""}</h3>
      ${st(S0.s, S0.word)}<span class="spacer"></span>${yes}</div>
    <div class="t-s rl-links">${links}</div>
    ${list.length ? `<ol class="rl-steps">${list.map(stepRow).join("")}</ol>` : ""}</section>`;
}

// What the next release ships, when it goes out by itself, and Release now.
function nextCard(D) {
  const T = targetOf(D), P = (T && T.release) || {}, R = D.release, L = D.latest;
  const n = R ? R.tasks : (P.ships || []).length, dep = R ? R.deploy : P.deploy;
  if (!n || !dep) return `<section class="pl-card rl-card" id="rlNext"><div class="rl-head"><h3>Next release</h3></div>
    <div class="muted t-m">No tasks queued.</div></section>`;
  const waitsFix = L && L.deploy === dep && L.fix;
  const due = waitsFix ? `waits for ${task(L.fix.id, "fix #" + L.fix.id)}`
    : R && R.until ? `next by itself ${esc(when(R.until))}`
    : R && R.running ? `after ${task(R.running, "#" + R.running)}`
    : P.early || !P.cadence ? "starting" : "";
  const go = R ? button("Release now", { kind: "primary", data: { relnow: D.name }, title: "Release now" }) : "";
  return `<section class="pl-card rl-card rl-next" id="rlNext"><div class="rl-head"><h3>Next release</h3><span class="spacer"></span>${go}</div>
    <div class="t-m">${plural(n, "task")} · ${report(dep)}${due ? ` · ${due}` : ""}</div></section>`;
}

// The GitHub tests of the branch that goes out (D.checks): its last run, red with its fix task when it failed.
function testsCard(D) {
  const C = D.checks || [];
  if (!C.length) return "";
  const rows = C.map(c => {
    const r = c.last_run, name = `${c.name === "tests" ? "Tests" : label(c.name)} (${c.branch || "main"})`;
    const state = c.error && !r ? st("failed", "Refresh failed") : !r ? st("none", "No run")
      : st(r.state, `${r.state === "queued" ? "waiting" : r.state} ${r.state === "running" ? "" : ago(r.ended_at || r.started_at || r.queued_at)}`.trim());
    const fix = r && r.state === "failed" && r.fault_item ? task(r.fault_item, `Fix #${r.fault_item}`) : "";
    const bad = r && r.tests ? (r.tests.failed || 0) + (r.tests.error || 0) : 0;
    return `<li class="rl-step"><span class="rl-sname">${esc(name)}</span>${state}
      ${bad ? `<span class="t-s c-warn">${plural(bad, "test")} failed</span>` : ""}
      <span class="t-s rl-links">${[fix, github(r && r.url)].filter(Boolean).join(" · ")}</span></li>`;
  }).join("");
  return `<section class="pl-card rl-card" id="rlTests"><h3>Tests</h3><ul class="rl-steps">${rows}</ul></section>`;
}

// More: the rest of a target's page, one quiet row.
function moreRow(D) {
  const b = (text, data) => button(text, { cls: "plain", data, title: text });
  return `<div class="pl-opts t-s rl-more"><span class="muted">More</span>${b("History", { "dp-tab": "versions" })}${b("Controls", { "dp-tab": "controls" })}
    ${b("Audit", { "dp-tab": "audit" })}${D.release ? b("Release with agent", { relagent: D.name }) : ""}</div>`;
}

export function releasePage(D) {
  return `<div class="rl">${nextCard(D)}${lastCard(D)}${testsCard(D)}${moreRow(D)}</div>`;
}
