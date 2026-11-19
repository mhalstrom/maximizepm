// The tabs Stages and Versions of a pipeline's page in Deploy (#6116 in Plan, moved to Deploy by #6136; canvas
// pipeline-mockup v2 of #6110, mark 2026-11-18: "kind of like code pipelines"). One pipeline at a time, the one that
// views/deploy.js names (showPipeline): its stages left to right (they get narrow to fit the width, then the row
// scrolls with a faded edge), the transitions between them, the detail of the chosen stage (its steps on a time line, its
// last lines, its tests failed first with the prompt that analyzes the failures), where each version runs, and the
// versions in each stage. A deploy target with no pipeline of its own shows as its default pipeline, computed from
// its releases. Reads: GET api/pipeline/<name>, api/pipeline-run/<id> (river/routes/pipeline.py); the actions (hold,
// approve, roll back, run again) are #6117. The arrow between two stages is a transition (#6139; canvas
// deploy-tab-mockup picture 4): it shows its mode, its state, who set it, until when and the waiting version, and a
// click opens its controls (Approve, Block until, Delay, Disable, Let one version pass, Freeze, the mode and window).
import { $, esc, getJson, ago, left, copyText, toast, act } from "../lib.js";
import { button, iconButton } from "../components/button.js";
import { chip } from "../components/chip.js";
import { OPEN_MARK } from "../components/rowOpen.js";
import { empty } from "../components/empty.js";
import { makeDialog } from "../components/dialog.js";
import { field, input, select, textarea, actions } from "../components/form.js";
import { runsOut } from "./accounts.js";
import { stageMarks } from "./agents.js";
import { holdingCardHtml, holdingDetail } from "./holding.js";
import { deployButtons } from "./targets.js";

const V = { shown: false, name: null, data: null, run: null, stage: null, arrow: null, tab: "stages", filter: "failed" };
let sig = "", runSig = "";
const WORD = { passed: "passed", running: "running", failed: "failed", queued: "waiting", skipped: "skipped",
  cancelled: "cancelled", stopped: "stopped", error: "error", none: "no run yet" };
// The mark of a transition: its mode when no control holds it, else the control (mark, word, chip color).
const MODE_MARK = { auto: ["→", "auto", ""], yes: ["✋", "yes", "c-human"], delay: ["⏱", "delay", ""],
  release: ["📦", "release", "c-human"], forge: ["⎇", "forge", ""] };
const STATE_MARK = { disabled: ["⏸", "disabled", "c-waiting"], blocked: ["⛔", "blocked", "c-warn"],
  fault: ["⛔", "fault", "c-warn"], frozen: ["❄", "frozen", "c-warn"], window: ["🕒", "closed", "c-human"],
  delayed: ["⏱", "delayed", "c-waiting"] };
const NEEDS_YES = ["yes", "release", "delay"];

const st = (s, word) => `<span class="pl-st ${esc(s || "none")}">● ${esc(word || WORD[s] || s || "no run yet")}</span>`;
// The end of a run: a watch that failed before its window closed has its end ahead (#6181: "ended -68398s ago").
const endText = (iso) => Date.parse(iso) > Date.now() ? `ends in ${left(iso)}` : ago(iso);
const short = (rev) => rev && /^[0-9a-f]{12,}$/.test(rev) ? rev.slice(0, 7) : rev || "";
const secs = (n) => n == null ? "" : n < 90 ? `${Math.round(n)}s` : n < 5400 ? `${Math.round(n / 60)}m` : `${Math.round(n / 3600)}h`;

// The pipeline and tab that the Deploy tab shows (views/deploy.js): another pipeline starts with no chosen stage.
export function showPipeline(name, tab) {
  if (name !== V.name) { V.stage = null; V.arrow = null; V.run = null; V.version = null; V.data = null; sig = ""; runSig = ""; }
  V.name = name; V.tab = tab; V.shown = !!name && ["stages", "versions"].includes(tab);
}
export function hidePipeline() { V.shown = false; }
// Choose a stage of pipeline name before the Deploy tab shows it (#6189: after Release now, its deploy stage).
export function pickStage(name, stage) {
  if (name !== V.name) showPipeline(name, "stages");
  V.stage = stage; V.arrow = null; V.run = null; sig = ""; runSig = "";
}

export async function renderPipeline(force) {
  if (!V.shown) return;
  const name = V.name;
  let D;
  try { D = await getJson("api/pipeline/" + encodeURIComponent(name)); }
  catch (e) {
    if (V.name !== name) return;
    $("#plNote").textContent = ""; sig = "";
    $("#plBody").innerHTML = empty(`No pipeline or deploy target ${esc(name)}: ${esc(e.message)}`);
    return;
  }
  if (V.name !== name || !V.shown) return;  // another pipeline or tab while the read ran
  V.data = D;
  if (!V.data.stages.some(s => s.name === V.stage)) V.stage = (V.data.stages.find(s => s.last_run && s.last_run.state === "failed")
    || V.data.stages.find(s => s.last_run && !s.holding) || V.data.stages[0] || {}).name || null;
  if (!V.data.stages.some(s => s.name === V.arrow)) V.arrow = null;
  const s = JSON.stringify([V.name, V.data, V.stage, V.arrow, V.tab, V.filter]);
  if (s === sig && !force) return refreshRun(force);
  sig = s;
  draw();
  refreshRun(true);
}

// The release of a deploy target in one click (#6189, mark 2026-11-19: 'I cannot figure out how to deploy
// maximizepm-github'): Approve release when its release waits for the person's yes (never a way round it), else
// Release now when collected tasks wait for the cadence or for the release that runs (its dialog: targets.js).
export function releaseButton(D) {
  if (!D.default || !D.controls) return "";
  // D is the pipeline page's data (its approval stage's last run) or a row of the Deploy list (its waits).
  const appr = (D.stages.find(s => s.name === "approval") || {}).last_run;
  const item = D.waits ? (D.waits[0] || {}).item : appr && appr.state === "queued" ? appr.item : null;
  const dep = ((D.stages.find(s => s.name === "deploy") || {}).last_run || {}).item;
  if (item) return button(`Approve release${dep ? " #" + dep : ""}`, { kind: "primary", data: { "dp-card": item }, title: "Approve or decline" });
  const r = D.release;
  return r ? button(`Release ${r.tasks} task${r.tasks === 1 ? "" : "s"} now`, { kind: "primary", data: { relnow: D.name, relshow: "1" },
    title: "Start the release" }) : "";
}

// Every release control of the page sits here, above the stages (#6189; manager-526c relaying mark 2026-11-19): Approve
// or Release now (releaseButton), the release from holding (the reviewed commits, or all), and for a target with
// collected tasks and nothing that waits, Deploy now (targets.js). The holding detail and the target card keep none.
function headControls(D) {
  const first = releaseButton(D), h = (D.stages.find(s => s.holding) || {}).holding, r = h && h.release;
  const plural = (n, one) => `${n} ${one}${n === 1 ? "" : "s"}`;
  const hold = r && r.can && h.commits ? [
    r.reviewed ? button(`Release the ${plural(r.reviewed, "reviewed commit")}`, { kind: first ? "" : "primary",
      data: { "hd-release": "reviewed", "hd-pipe": D.name, sha: r.upto, n: r.reviewed }, title: "Release the reviewed ones" }) : "",
    button(`Release all ${h.commits}`, { data: { "hd-release": "all", "hd-pipe": D.name, sha: r.last, n: h.commits }, title: "Every commit in holding" })].join("") : "";
  return first + hold + (D.default && !first && !hold ? deployButtons(D.name) : "");
}

// The next step of a deploy target, right under the graph (#6189, mark 2026-11-19: 'its still not obvious how to do
// the release promotion'): one highlighted line that says what waits and for whom, with its button: Approve release
// #N when the release waits for the person's yes, else Release N tasks now and Release via agent, else the release
// from holding or Deploy now (headControls). Nothing when no step waits. The same buttons are in the chip's dialog.
// Where the next step waits on the graph, for its highlight (pl-waits): the approval card while it waits for the
// person's yes, else the chip after holding ("→<stage>") while collected tasks wait for the release; else null.
function nextAt(D) {
  if (!D || !D.default) return null;
  const appr = (D.stages.find(s => s.name === "approval") || {}).last_run;
  if (appr && appr.state === "queued" && appr.item) return "approval";
  const i = D.stages.findIndex(s => s.holding);
  return D.release && i >= 0 && D.stages[i + 1] ? "→" + D.stages[i + 1].name : null;
}

function nextBar(D) {
  const b = headControls(D);
  if (!D.default || !b) return "";
  const appr = (D.stages.find(s => s.name === "approval") || {}).last_run || {}, R = D.release;
  const dep = ((D.stages.find(s => s.name === "deploy") || {}).last_run || {});
  const text = appr.state === "queued" && appr.item
    ? `Next: your yes for release${dep.item ? " #" + dep.item : ""}${appr.version ? ` (<span class="pl-ver">${esc(short(appr.version))}</span>)` : ""}. It goes to production after it.`
    : R ? `Next: ${R.tasks} task${R.tasks === 1 ? "" : "s"} wait for the release. Without a click: ${esc(R.text)}.`
    : "Next: the collected tasks can go out.";
  const agent = R ? button("Release via agent…", { data: { relagent: D.name }, title: "An agent releases it" }) : "";
  return `<div class="pl-next"><span class="t-m">${text}</span><span class="spacer"></span>${b}${agent}</div>`;
}

function draw() {
  const D = V.data;
  const envs = D.envs.length;
  // #6183: Hold all stages stands at the end of the bar, not inside the line of text.
  const words = `${D.stages.length} stage${D.stages.length === 1 ? "" : "s"} · ${envs} environment${envs === 1 ? "" : "s"}`
    + (D.default ? ` · the releases of deploy target ${esc(D.name)}, as a pipeline` : "")  // the page's head names the project
    + (D.followed ? ` · followed: its own system runs the flow; its trigger rules and hooks act on its events` : "");  // #6167
  const hold = (D.default && !D.controls) || D.followed ? "" : D.held_reason ? button("End the hold", { data: { "pl-unhold": "" }, title: "Stages start again" })
    : button("Hold all stages", { kind: "warn", data: { "pl-hold": "" }, title: "Nothing new starts" });
  $("#plNote").innerHTML = `<div class="pl-bar"><span>${words}</span><span class="spacer"></span>${hold}</div>`
    + (D.held_reason ? `<div class="pl-banner">Every stage is held by ${esc(D.held_by || "a person")}: ${esc(D.held_reason)}. Work that runs goes on; nothing new starts.</div>` : "");
  $("#plBody").innerHTML = V.tab === "versions" ? versionsView(D) : stagesView(D);
  if (V.tab === "stages") fit();
  drawArrow();  // #6189: an open transition dialog shows the new state
}

// ---- the stages left to right

// The forge token of the pipeline fails (#6147): what the forge said is old, so a stage that the forge runs shows "no
// news" with the time of the last read, never a green that may be stale.
const tokenFails = D => D.account && ["refused", "missing"].includes(D.account.state);

// The tier of a stage as a badge (#6137): prod in the warning color, beta in the person's, alpha in the ready one,
// a tier of the pipeline's own plain. Several stages may share a tier.
const TIER_COLOR = { prod: "c-warn", beta: "c-human", alpha: "c-ready" };
function tierBadge(s) {
  return s.tier ? " " + chip(`${TIER_COLOR[s.tier] || ""} pl-tier`, esc(s.tier.toUpperCase()), `Tier ${s.tier}`) : "";
}

// A stage that starts manually (#6138): its card says so, and the version that its transition let in waits there
// until someone presses Run (a button under the card: the card itself is a button).
function stageCard(s) {
  const card = stageCardOnly(s);
  if (s.start !== "manual") return card;
  const w = s.waits_for_run;
  return `<span class="pl-stack">${card}${w ? button(`▶ Run ${esc(short(w.rev))}`, { kind: "primary", cls: "pl-run",
    data: { "pl-run-stage": s.name, rev: w.rev || "" }, title: `Start ${s.name}` }) : ""}</span>`;
}

function stageCardOnly(s) {
  const D = V.data;
  if (s.holding) {  // the first stage: the commits that wait for the release (#6140, views/holding.js)
    return button(`<span class="pl-in">${holdingCardHtml(s.holding)}</span>`, { kind: "card", cls: "pl-stage pl-hold",
      pressed: s.name === V.stage && !V.arrow, data: { "pl-stage": s.name }, title: "Commits before the release" });
  }
  if (tokenFails(D) && s.workflow && !s.run) {
    const r = s.last_run, read = D.account.read_at;
    return button(`<span class="pl-in"><span class="pl-kind" title="${esc(s.kind)}${s.env ? " · " + esc(s.env) : ""}">${esc(s.kind)}${tierBadge(s)}${s.env ? " · " + esc(s.env) : ""}</span>
      <span class="pl-name">${esc(s.name)}</span><span class="pl-ver">${esc(short(r && r.version))}</span>
      ${st("none", "no news")}<span class="pl-meta pl-more-line">${read ? "last read " + esc(ago(read)) : "not read yet"}</span></span>`,
      { kind: "card", cls: "pl-stage" + (s.env ? " pl-env" : ""), pressed: s.name === V.stage && !V.arrow, data: { "pl-stage": s.name },
        title: "Forge workflow: " + s.workflow });
  }
  const r = s.last_run, live = r && s.env && r.state === "passed";
  const stepDots = r && r.step_list ? r.step_list.map(x => x.state) : r ? Object.entries(r.steps || {}).flatMap(([k, n]) => Array(Math.min(n, 12)).fill(k)) : [];
  const meta = !r ? "" : r.state === "queued" && r.gate_note ? r.gate_note
    : r.ended_at ? `${WORD[r.state] || r.state}${Date.parse(r.ended_at) > Date.now() ? "," : ""} ${endText(r.ended_at)}` : r.started_at ? `since ${ago(r.started_at)}` : "";
  return button(`<span class="pl-in"><span class="pl-kind" title="${esc(s.kind)}${s.env ? " · " + esc(s.env) : ""}">${esc(s.kind)}${tierBadge(s)}${s.env ? " · " + esc(s.env) : ""}</span>
    <span class="pl-name">${esc(s.name)}</span>
    <span class="pl-ver">${esc(short(r && r.version))}</span>
    ${r ? st(r.state, live ? "live" : null) : st("none")}
    <span class="pl-meta pl-more-line">${esc(meta)}</span>
    ${s.start === "manual" ? `<span class="pl-meta">✋ ${s.waits_for_run ? "waits for Run" : "starts manually"}</span>` : ""}
    ${keysLine(s)}
    ${s.override ? `<span class="pl-meta c-warn" title="Skipped: ${esc(s.override.skipped.join(", "))}">⚠ not through the pipeline</span>` : ""}
    ${stepDots.length ? `<span class="pl-dots pl-more-line" aria-hidden="true">${stepDots.map(x => `<span class="pl-dot ${esc(x)}"></span>`).join("")}</span>` : ""}
    ${s.work && (s.work.faults.length || s.work.agents.length) ? `<span class="ag-marks">${stageMarks(s.work)}</span>` : ""}</span>`,
    { kind: "card", cls: "pl-stage" + (s.env ? " pl-env" : "") + (nextAt(V.data) === s.name ? " pl-waits" : ""),
      pressed: s.name === V.stage && !V.arrow, data: { "pl-stage": s.name },
      title: s.run ? "Runs: " + s.run : s.workflow ? "Forge workflow: " + s.workflow : s.name });
}

// The keys a stage may read (#6153), by name only: the secrets of the credential sets granted to the stage or its
// tier, and the names that the deployment system keeps for it (GitHub environment secrets, a Pulumi ESC environment).
const keyNames = s => s.keys ? [...new Set([...s.keys.secrets, ...s.keys.kept.flatMap(x => x.names)])] : [];
function keysLine(s) {
  const names = keyNames(s);
  return names.length ? `<span class="pl-meta pl-more-line">🔒 ${esc(names.join(", "))}</span>` : "";
}
function keysHtml(s) {
  const k = s.keys;
  if (!k) return "";
  const row = (n, where, who) => `<tr><td data-label="Secret">🔒 ${esc(n)}</td><td data-label="Kept in">${where}</td>
    <td data-label="Who uses it">${esc(who)}</td></tr>`;
  const rows = k.detail.flatMap(x => (x.secrets.length ? x.secrets : ["(an empty set)"]).map(n => row(n,
      `MaximizePM <span class="muted t-s">set ${esc(x.set)}</span>`, x.by === "tier" ? `every ${s.tier} stage` : `only the ${s.name} stage`)))
    .concat(k.kept.flatMap(x => x.names.map(n => row(n, `${esc(x.kept_in)} <span class="muted t-s">${x.source === "person"
      ? "named by a person" : "read by " + esc(x.source)}</span>`, `the ${s.name} stage's own job there`))));
  return `<div class="t-m"><b>Secrets this stage may read</b></div>` + (rows.length
    ? `<div class="pl-table"><table class="pl-blocks"><tr><th>Secret</th><th>Kept in</th><th>Who uses it</th></tr>${rows.join("")}</table></div>`
    : `<div class="muted t-s">None: this stage reads no secret. Grant a set: <code>maxpm secret grant &lt;set&gt; --stage ${esc(V.name)}/${esc(s.name)}</code></div>`);
}

// The mark and the words of a transition: [mark, short word, chip color].
function arrowMark(t) {
  if (t.state !== "open") return STATE_MARK[t.state] || ["⛔", t.state, "c-warn"];
  const m = MODE_MARK[t.mode] || MODE_MARK.auto;
  if (t.mode === "yes") return [m[0], t.arg && t.arg !== "manager" ? `${t.arg}'s yes` : t.arg ? "manager" : "a yes", m[2]];
  if (t.mode === "delay") return [m[0], t.arg, m[2]];
  return m;
}
// A transition with nothing to say: automatic, open, no time rule. The first stage's arrow shows only when it has one.
const plainArrow = (t) => t.mode === "auto" && t.state === "open" && !t.window && !t.fault_rule && !(t.freezes || []).length;

function arrowEl(s, i) {
  const t = s.transition;
  if (!t || (!i && plainArrow(t))) return "";
  if (V.data && V.data.followed) return i ? `<div class="pl-gate"></div>` : "";  // #6167: no transition of ours
  const [m, word, cls] = arrowMark(t);
  const w = (t.waiting || [])[0];
  const by = t.state !== "open" ? [t.set_by, t.until ? (t.until.startsWith("#") ? `until ${t.until} closes` : `${left(t.until)} left`) : ""]
    .filter(Boolean).join(" · ") : "";
  // A join (#6142): it lets a version in when it passed every branch; it names the branches a version still waits for.
  const join = (t.from || []).length > 1 ? (t.join ? `⋈ ${short(t.join.rev)} waits for ${t.join.missing.join(", ")}` : "⋈ every branch") : "";
  return `<div class="pl-gate">${button(`${chip(cls, `${esc(m)}<span class="pl-long"> ${esc(word)}</span>`)}
    ${join ? `<span class="pl-anote pl-join">${esc(join)}</span>` : ""}
    ${by ? `<span class="pl-anote pl-long">${esc(by)}</span>` : ""}${w ? `<span class="pl-anote pl-ver pl-long">${esc(short(w.rev))} waits</span>` : ""}`,
    { kind: "card", cls: "pl-arrow" + (nextAt(V.data) === "→" + s.name ? " pl-waits" : ""), pressed: V.arrow === s.name, data: { "pl-arrow": s.name },
      title: `Transition into ${s.name}` })}</div>`;
}

function stagesView(D) {
  if (!D.stages.length) return empty(`Pipeline ${esc(D.name)} has no stages yet: maxpm pipeline stage ${esc(D.name)} &lt;stage&gt; --kind &lt;kind&gt;`);
  const L = layout(D.stages);
  const row = L.branched ? branchedRow(D.stages, L) : D.stages.map((s, i) => arrowEl(s, i) + stageCard(s)).join("");
  return `${tokenBanner(D)}<div class="pl-scroll" id="plScroll"><div class="pl-row${L.branched ? " pl-grid" : ""}" id="plRow">${row}</div></div>
    ${nextBar(D)}
    <div class="pl-card" id="plDetail">${detail()}</div>
    <div id="plStageRuns"></div>
    ${envTable(D)}${sourceCard(D)}`;
}

// Branches and joins (#6142, Decision 9): the place of each stage. Its column is the longest way from the first stage;
// the first stage after a stage keeps that stage's row, each other branch gets a row of its own, and a join sits on
// the row of its first branch. branched: the pipeline splits somewhere (else it is one row, as before).
function layout(stages) {
  const col = {}, row = {}, kept = {}, prevs = {};
  let rows = 0, branched = false;
  stages.forEach((s, i) => {  // in order, so the stages before s have their places
    let p = (s.prev || []).filter(n => n in col);
    if (!p.length && i) p = [stages[i - 1].name];  // after the Holding stage (#6140), which names none
    col[s.name] = p.length ? 1 + Math.max(...p.map(n => col[n])) : 0;
    if (p.length > 1) { row[s.name] = Math.min(...p.map(n => row[n])); branched = true; }
    else if (p.length === 1 && !kept[p[0]]) { row[s.name] = row[p[0]]; kept[p[0]] = true; }
    else { row[s.name] = rows; if (p.length) branched = true; }
    rows = Math.max(rows, row[s.name] + 1);
    prevs[s.name] = p;
  });
  return { col, row, rows, branched, prevs };
}

// The grid of a pipeline with branches: each stage (its arrow and card) in its row and column; before each column a
// thin column with the lines that fan out to the branches and fan in to a join.
function branchedRow(stages, L) {
  const cells = stages.map((s, i) => `<div class="pl-cell" style="grid-row:${L.row[s.name] + 1};grid-column:${2 * L.col[s.name] + 2}">${arrowEl(s, i)}${stageCard(s)}</div>`);
  const edgesOf = {};
  for (const s of stages) for (const p of L.prevs[s.name]) {
    (edgesOf[L.col[s.name]] = edgesOf[L.col[s.name]] || []).push([L.row[p], L.row[s.name]]);
  }
  for (const [c, edges] of Object.entries(edgesOf)) {
    if (edges.every(([a, b]) => a === b)) continue;  // a plain step: the arrow's own line is enough
    const top = Math.min(...edges.flat()), end = Math.max(...edges.flat()), n = end - top + 1;
    const y = r => ((r - top + 0.5) / n * 100).toFixed(1);
    cells.push(`<div class="pl-fan" style="grid-row:${top + 1} / ${end + 2};grid-column:${2 * c + 1}" aria-hidden="true"><svg viewBox="0 0 24 100" preserveAspectRatio="none">${edges.map(([a, b]) => `<path d="M0 ${y(a)} C 12 ${y(a)}, 12 ${y(b)}, 24 ${y(b)}"/>`).join("")}</svg></div>`);
  }
  return cells.join("");
}

// The forge's own words when it refuses the token, or that nobody saved it (#6147).
function tokenBanner(D) {
  if (!tokenFails(D)) return "";
  const a = D.account;
  const why = a.state === "refused" ? `${a.error || a.label + " refuses the token " + a.secret}`
    + (a.refused_since ? ` (since ${ago(a.refused_since)})` : "") : a.error;
  return `<div class="pl-banner">✕ ${esc(why)}. This page shows what ${esc(a.label)} said before then; nothing new starts there until a token works. The manager knows.
    ${a.secret ? `Save a new one: <code>maxpm secret put ${esc(a.secret)}</code>` : ""}</div>`;
}

// Where the pipeline's runs come from: the forge, the account, the token and when it runs out (#6147).
function sourceCard(D) {
  const a = D.account;
  if (!a) return "";
  const state = a.state === "refused" ? `<span class="c-warn">✕ refused</span>` : a.state === "missing" ? `<span class="c-warn">○ not saved</span>`
    : a.read_at ? `● read ${esc(ago(a.read_at))}` : `<span class="muted">not read yet</span>`;
  return `<div class="pl-card"><h3>Where its runs come from</h3><div class="pl-table"><table class="pl-blocks">
    <tr><th>Forge</th><th>Account</th><th>Token</th><th>Last read</th><th>The token runs out</th></tr>
    <tr><td data-label="Forge">${esc(a.label)} <span class="muted t-s">${esc(a.host)}</span></td>
      <td data-label="Account">${esc(a.owner || "?")}${a.kind ? ` <span class="muted t-s">${esc(a.kind)}</span>` : ""}</td>
      <td class="mono" data-label="Token">${esc(a.secret || "none named")}</td><td data-label="Last read">${state}</td>
      <td class="${a.days_left != null && a.days_left <= 7 ? "c-warn" : ""}" data-label="Runs out">${esc(runsOut(a)) || `<span class="muted">-</span>`}</td></tr></table></div>
    <div class="pl-bar">${button("All accounts", { data: { "dp-part": "accounts" }, title: "See the accounts" })}</div></div>`;
}

// Narrow cards first; when even those do not fit, the row scrolls and its right edge fades while more is there.
function fit() {
  const sc = $("#plScroll"); if (!sc) return;
  sc.classList.remove("pl-compact");
  if (sc.scrollWidth > sc.clientWidth + 1) sc.classList.add("pl-compact");
  fade();
}
function fade() {
  const sc = $("#plScroll"); if (!sc) return;
  sc.classList.toggle("pl-more", sc.scrollLeft + sc.clientWidth < sc.scrollWidth - 1);
}

// Whether the pipeline's source can do an action on a stage (#6159): a stage whose work the source does (a workflow
// and no command) has only the actions its plugin or adapter lists; a stage with a command has every action.
function sourceCan(D, s, action) {
  if (!s || !s.workflow || s.run) return true;
  return ((D.source && D.source.actions) || []).includes(action);
}

// An action button, or the same button greyed out when the source does not do that action (its text goes in miss,
// for the line under the bar).
function srcButton(D, s, action, html, opts, miss) {
  if (sourceCan(D, s, action)) return button(html, opts);
  if (miss) miss.push(html);
  return button(html, { ...opts, data: {}, disabled: true, title: "Source cannot do this" });
}

// The line under a bar of actions that names the greyed ones and the source.
function missLine(D, miss) {
  if (!miss.length) return "";
  const src = D.source || {};
  return `<div class="muted t-s">${miss.join(", ")}: not available for this source${src.name ? ` (${src.kind === "plugin" || src.kind === "missing" ? "plugin " : ""}${esc(src.name)})` : ""}</div>`;
}

function envTable(D) {
  if (!D.envs.length) return "";
  const deployer = e => D.stages.find(s => s.kind === "deploy" && s.env === e.name);
  // #6187: on a phone each environment is a block with each cell under its name (pl-blocks), not four columns that
  // broke 'Environment' and 'Since' inside the word at 390 px; the versions before are a list with separators.
  const rows = D.envs.map(e => `<tr class="${e.rev ? "live" : ""}"><td data-label="Environment">${esc(e.name)}${e.target && e.target !== e.name ? ` <span class="muted t-s">target ${esc(e.target)}</span>` : ""}</td>
    <td data-label="Version">${e.rev ? `<span class="mono">${esc(short(e.rev))}</span>` : `<span class="muted">none yet</span>`}</td><td class="pl-nowrap" data-label="Since">${e.since ? esc(ago(e.since)) : ""}</td>
    <td class="t-s muted" data-label="Before">${(e.history || []).slice(1, 4).filter(h => h.rev && h.rev !== e.rev).map(h => esc(short(h.rev))
      + (h.kind !== "deployed" ? " (" + esc(h.kind) + ")" : "") + (D.default ? "" : " " + srcButton(D, deployer(e), "rollback", "Put back", { data: { "pl-rollback": e.name, rev: h.rev },
        title: `Roll back to ${short(h.rev)}` }))).join(" · ")}</td></tr>`).join("");
  return `<div class="pl-card"><h3>Where each version runs</h3><div class="pl-table"><table class="pl-blocks">
    <tr><th>Environment</th><th>Version</th><th>Since</th><th>Before</th></tr>${rows}</table></div></div>`;
}

// ---- the detail of the chosen stage

function chosen() { return V.data && V.data.stages.find(s => s.name === V.stage); }

// Versions in <stage> (#6141): each run of the chosen stage, from GET api/pipeline-stage; the box under the detail.
let srSig = "";
async function refreshStageRuns(force) {
  const s = chosen(), box = $("#plStageRuns");
  if (!box) return;
  if (!s || (V.data.default && !V.data.controls) || V.arrow || s.holding) { box.innerHTML = ""; srSig = ""; return; }
  let SR;
  try { SR = await getJson(`api/pipeline-stage?pipeline=${encodeURIComponent(V.name)}&stage=${encodeURIComponent(s.name)}`); }
  catch (e) { return; }
  const sg = JSON.stringify(SR);
  if (sg === srSig && !force && box.innerHTML) return;
  srSig = sg;
  box.innerHTML = stageRunsHtml(SR);
}

const RUNS_SHOWN = 5;  // the newest rows of Versions in <stage> before Show all (#6182)

function stageRunsHtml(SR) {
  if (!SR.runs.length) return "";
  const liveRev = (SR.runs.find(r => r.live) || {}).rev;
  const D = V.data, s = D.stages.find(x => x.name === SR.stage), miss = [], ours = !D.followed;  // #6167
  // #6176: a deploy target's rows are its releases: a move back and a run again of deploy make a deploy item (the
  // person's yes and the deploy hook still release it); a run again of review or build acts on a release not out yet.
  const tg = !!SR.target, busy = (r) => ["running", "queued"].includes(r.state);
  const back = (r) => ours && r.state === "passed" && r.rev && r.rev !== liveRev && (!tg || (SR.stage === "deploy" && r.pinned));
  const again = (r) => ours && r.rev && (!tg || (r.pinned && (SR.stage === "deploy" ? !busy(r)
    : ["review", "build"].includes(SR.stage) && !r.released)));
  // #6182 (mark 2026-11-19, the review and approval tables of toolscaled-prod): the newest RUNS_SHOWN rows, then Show
  // all; the state in its own column (no 'ran' on every row), How long it took, the task and the buttons each under a
  // heading, a release with no pin says so instead of 'deploy #N', and the columns that every row leaves empty go.
  const all = V.allRuns === SR.stage, list = all ? SR.runs : SR.runs.slice(0, RUNS_SHOWN);
  const plain = (w) => !w || w === "ran" || w === "deployed";
  const cells = list.map(r => {
    const b = [];
    if (back(r))
      b.push(srcButton(D, s, "rollback", "Move back here", { kind: "warn", data: { "pl-moveback": SR.stage, rev: r.rev }, title: `Back to ${short(r.rev)}` }, miss));
    if (back(r) && (SR.branches || []).length)  // #6142: the same place of every branch
      b.push(srcButton(D, s, "rollback", "Every branch", { kind: "warn", data: { "pl-moveback": SR.stage, rev: r.rev, branches: SR.branches.join(", ") }, title: "Move every branch back" }, miss));
    if (ours && r.run != null && busy(r)) b.push(srcButton(D, s, "stop", "Stop", { kind: "warn", data: { "pl-stop": r.run }, title: "Stop this run" }, miss));
    else if (again(r)) b.push(srcButton(D, s, "rerun", "Run again", { data: { "pl-rerun": SR.stage, rev: r.rev }, title: "Run this stage again" }, miss));
    const when = r.started_at || r.queued_at;
    const why = (r.by ? esc(r.by) + (r.why ? ": " : "") : "") + esc(r.why || "") + (r.errors ? `${r.by || r.why ? " · " : ""}<span class="c-warn">${esc(r.errors)}</span>` : "");
    return { r, b, why, when, task: tg && r.item ? `<span class="link" data-open="${r.item}">#${r.item}</span>` : "",
      ver: tg && !r.pinned ? `<span class="muted">not pinned</span>` : esc(short(r.rev)) };
  });
  const hasWhy = cells.some(c => c.why), hasTask = cells.some(c => c.task), hasActs = cells.some(c => c.b.length);
  const rows = cells.map(({ r, b, why, when, task, ver }) => `<tr class="${r.live ? "live" : ""}"><td class="t-s pl-nowrap">${when ? esc(ago(when)) : ""}</td>
      <td class="mono">${ver}${r.live ? ` <span class="muted t-s">live</span>` : ""}</td>
      <td class="pl-nowrap">${st(r.state)}${plain(r.what) ? "" : ` <span class="muted t-s">${esc(r.what)}</span>`}</td>
      <td class="t-s muted">${r.seconds != null ? esc(secs(r.seconds)) : ""}</td>
      ${hasWhy ? `<td class="t-s pl-wide">${why}</td>` : ""}${hasTask ? `<td class="t-s">${task}</td>` : ""}
      ${hasActs ? `<td class="pl-wide"><span class="pl-bar">${b.join("")}</span></td>` : ""}</tr>`).join("");
  const more = SR.runs.length > RUNS_SHOWN ? `<div class="t-s">${button(all ? "Show the newest " + RUNS_SHOWN : `Show all ${SR.runs.length}`,
    { data: { "pl-allruns": SR.stage }, title: all ? "Fewer rows" : "Every run" })}</div>` : "";
  return `<div class="pl-card"><h3>Versions in ${esc(SR.stage)}</h3>
    <div class="pl-table"><table class="pl-runs"><tr class="pl-th"><th>When</th><th>Version</th><th>State</th><th>Took</th>
      ${hasWhy ? "<th>Why, or who</th>" : ""}${hasTask ? "<th>Task</th>" : ""}${hasActs ? "<th>Actions</th>" : ""}</tr>${rows}</table></div>
    ${more}
    ${missLine(D, [...new Set(miss)])}
    <div class="muted t-s">${tg ? `Each row is a release of ${esc(SR.target)}. A move back or a run again of deploy adds a deploy item pinned at that version: it waits for the person's yes and goes out through the deploy hook, like every release. A move back also blocks the transition into deploy until someone unblocks it.`
      : ours ? `A move back is one more run here, and it blocks the transition into ${esc(SR.stage)} until someone unblocks it.`
      : "Followed: these runs come from its own system; a move back or a run again happens there."}</div></div>`;
}

async function refreshRun(force) {
  refreshStageRuns(force);
  const s = chosen(), r = s && s.last_run;
  if (!r || r.id == null) { V.run = r || null; if (force && $("#plDetail")) $("#plDetail").innerHTML = detail(); return; }
  let R;
  try { R = await getJson("api/pipeline-run/" + r.id); } catch (e) { return; }
  const s2 = JSON.stringify([R, V.filter]);
  if (s2 === runSig && !force) return;
  runSig = s2; V.run = R;
  if ($("#plDetail")) $("#plDetail").innerHTML = detail();
}

function gantt(steps) {
  const t = (x) => Date.parse(x);
  const timed = steps.filter(x => x.started_at || x.ended_at);
  if (!timed.length) return "";
  const t0 = Math.min(...timed.map(x => t(x.started_at || x.ended_at)));
  const t1 = Math.max(...timed.map(x => x.ended_at ? t(x.ended_at) : Date.now()), t0 + 1000);
  const pct = (v) => ((v - t0) / (t1 - t0) * 100).toFixed(1);
  return `<div class="pl-gantt">${timed.map(x => {
    const a = t(x.started_at || x.ended_at), b = x.ended_at ? t(x.ended_at) : Date.now();
    const w = Math.max(1.5, (b - a) / (t1 - t0) * 100).toFixed(1);
    return `<div>${esc(x.name)}${x.host ? ` <span class="muted">${esc(x.host)}</span>` : ""} <span class="muted">${esc(secs(x.seconds ?? (b - a) / 1000))}</span></div>
      <div class="pl-track" title="${esc(x.name)}: ${esc(WORD[x.state] || x.state)}${x.note ? " · " + esc(x.note) : ""}"><div class="pl-seg ${esc(x.state)}" style="left:${pct(a)}%;width:${w}%"></div></div>`;
  }).join("")}</div>`;
}

// The watch of a release (#6181): one row for each check of the target, its name on one line, its last result in the
// watch, its runs and failed runs, and the newest failed runs with their summary. A click on the row opens every run
// of the check in the watch (GET api/pipeline-check-runs); a second click closes it. Before, each run was a row of the
// time line: a check every few minutes for 24 hours made a page over 30000 px tall.
const WV = { key: null, runs: null };
const watchKey = (r, x) => `${x.check}|${r.started_at}|${r.watch_end}`;

function watchHtml(r) {
  const rows = (r.step_list || []).map(x => {
    const k = watchKey(r, x), open = WV.key === k, fails = x.fails || [];
    const failText = fails.map(f => `<div><span class="mono">${esc(when(f.at))}</span> ${esc(f.summary || "")}</div>`).join("")
      + (x.failed > fails.length ? `<div class="muted">and ${x.failed - fails.length} more</div>` : "");
    const now = x.now && x.now !== x.state ? ` <span class="muted">now ${esc(WORD[x.now] || x.now)}</span>` : "";
    return `<tr class="clickrow" data-pl-check="${esc(k)}" aria-expanded="${open}"><td class="pl-cname"><span class="title">${esc(x.name)}</span></td>
      <td>${st(x.state)}${now}</td><td class="pl-n">${x.runs} run${x.runs === 1 ? "" : "s"}</td>
      <td class="pl-n">${x.failed ? `<span class="c-warn">${x.failed} failed</span>` : "none failed"}</td>
      <td class="t-s pl-wide">${failText}</td><td class="pl-open">${OPEN_MARK}</td></tr>`
      + (open ? `<tr class="pl-allruns"><td colspan="6">${allRuns()}</td></tr>` : "");
  }).join("");
  return `<div class="pl-table"><table class="pl-checks"><tr class="pl-th"><th>Check</th><th>Last result</th><th>Runs</th>
    <th>Failed</th><th>Failed runs, newest first</th><th></th></tr>${rows}</table></div>`;
}

function allRuns() {
  if (!WV.runs) return `<div class="muted t-s">Reading the runs…</div>`;
  if (WV.runs.error) return `<div class="c-warn t-s">${esc(WV.runs.error)}</div>`;
  if (!WV.runs.runs.length) return `<div class="muted t-s">No run in the watch.</div>`;
  return `<div class="pl-runlist t-s">${WV.runs.runs.map(x => `<div><span class="mono">${esc(when(x.at))}</span> ${st(x.state)}
    ${x.summary ? `<span class="muted">${esc(x.summary)}</span>` : ""}</div>`).join("")}</div>`;
}

async function toggleCheck(k) {
  if (WV.key === k) { WV.key = null; WV.runs = null; }
  else {
    WV.key = k; WV.runs = null;
    const [check, from, to] = k.split("|");
    getJson(`api/pipeline-check-runs?check=${encodeURIComponent(check)}&from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`)
      .then(R => { if (WV.key === k) WV.runs = R; }, e => { if (WV.key === k) WV.runs = { error: String(e.message || e) }; })
      .then(() => { if ($("#plDetail")) $("#plDetail").innerHTML = detail(); });
  }
  if ($("#plDetail")) $("#plDetail").innerHTML = detail();
}

// ---- the detail of the chosen transition: each control with who set it and until when, and its buttons

const when = (iso) => iso ? new Date(iso).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "";
const setBy = (who, at) => esc(who || "a person") + (at ? " · " + esc(ago(at)) : "");

// A transition chip opens its dialog (#6189, mark 2026-11-19 through manager-526c: "the release control is ON THE
// GRAPH ... A click on a transition chip opens a modal"): what it does now (for the release after holding: the tasks
// and commits that go out, up to which version, the deploy hook that runs, and the buttons Release now, Release the
// reviewed commits, Release all, Approve, Deploy now), then what it can do (its controls, arrowDetail). It is drawn
// again with each read of the pipeline while it is open. It lives in #dpStages, so this view's clicks reach it.
let arrowDlg = null, arrowBox = null, arrowName = null;
function openArrow(name) {
  if (!arrowDlg) {
    const d = document.createElement("div");
    d.id = "plArrowDlg"; d.className = "hidden";
    d.innerHTML = '<div class="box"></div>';
    $("#dpStages").appendChild(d);
    arrowBox = d.firstElementChild;
    arrowDlg = makeDialog(d, { onClose: () => { arrowName = null; } });
  }
  arrowName = name;
  drawArrow();
  arrowDlg.open();
}

function drawArrow() {
  if (!arrowName || !arrowBox || !V.data) return;
  const D = V.data, s = D.stages.find(x => x.name === arrowName);
  if (!s || !s.transition) return arrowDlg.close();
  arrowBox.innerHTML = `<div class="dlg-head"><h2>Into ${esc(s.name)}</h2>${iconButton("✕", "Close", { data: { "pl-arrowclose": "1" } })}</div>`
    + arrowNow(D, s) + arrowDetail(s);
}

// What the transition does now, with the buttons that act on it.
function arrowNow(D, s) {
  const i = D.stages.indexOf(s), afterHolding = i > 0 && D.stages[i - 1].holding;
  if (!afterHolding) {
    const yes = s.name === "approval" ? releaseButton(D) : "";
    return yes ? `<div class="pl-bar">${yes}</div>` : "";
  }
  const h = D.stages[i - 1].holding || {}, r = h.release || {}, R = D.release, hook = (D.stages.find(x => x.name === "deploy") || {}).run;
  const lines = [];
  if (R) lines.push(`${R.tasks} task${R.tasks === 1 ? "" : "s"} wait for this release${R.deploy ? ` (<span class="link" data-open="${R.deploy}">#${R.deploy}</span>, <span class="link" data-report="${R.deploy}">its report</span>)` : ""}. Without a click: ${esc(R.text)}.`);
  if (h.commits) lines.push(`${h.commits} commit${h.commits === 1 ? "" : "s"} in holding, ${h.reviewed || 0} reviewed${r.last ? `; Release all goes up to <span class="pl-ver">${esc(short(r.last))}</span>` : ""}.`);
  if (hook) lines.push(`Then the deploy hook runs: <code>${esc(hook)}</code>`);
  if (!lines.length) lines.push("Nothing waits for a release now.");
  // Release via agent beside the manual release (mark 2026-11-19), when a release can go and no yes waits.
  const waitsYes = releaseButton(D).includes("dp-card"), agent = D.default && !waitsYes && (R || h.commits)
    ? button("Release via agent…", { data: { relagent: D.name }, title: "An agent releases it" }) : "";
  const b = headControls(D) + agent;
  return `<h3>What it does now</h3>${lines.map(x => `<div class="t-m">${x}</div>`).join("")}${b ? `<div class="pl-bar">${b}</div>` : ""}<h3>What it can do</h3>`;
}

function arrowDetail(s) {
  const D = V.data, t = s.transition, i = D.stages.indexOf(s);
  const from = (t.from || []).length ? t.from.join(" + ") : t.from ? "start" : i > 0 ? D.stages[i - 1].name : "start";  // #6142
  const [m, word, cls] = arrowMark(t);
  const row = (what, now, by) => `<tr><td><b>${esc(what)}</b></td><td>${now}</td><td class="t-s muted">${by}</td></tr>`;
  const rows = [row("Mode", esc(t.text), t.mode_by ? setBy(t.mode_by, t.mode_at) : t.fixed || D.default ? "the deploy target" : "the start: automatic")];
  if (t.window) rows.push(row("Time window", `${esc(t.window.text)} ${st(t.window.open ? "passed" : "queued", t.window.open ? "open now" : "closed now")}`,
    setBy(t.window.by, t.window.at) + (t.window.change ? ` · ${t.window.open ? "closes" : "opens"} ${esc(when(t.window.change))}` : "")));
  for (const f of t.freezes || []) rows.push(row("Freeze", `${esc(when(f.from))} to ${esc(when(f.to))}: ${esc(f.reason)}${f.now ? " " + st("failed", "now") : ""}`, setBy(f.by)));
  if (t.fault_rule) rows.push(row("Fault rule", t.fault ? `blocked while <span class="link" data-open="${t.fault.id}">#${t.fault.id}</span> is open`
    : "blocked while a fault of the pipeline is open; none is open", setBy(t.rule_by)));
  if (t.block) rows.push(row("Block", `<span class="c-warn">${esc(t.block.reason)}</span>`, setBy(t.block.by, t.block.at) + " · until "
    + (t.block.until ? esc(when(t.block.until)) : t.block.item ? `<span class="link" data-open="${t.block.item}">#${t.block.item}</span> closes` : "someone unblocks it")));
  if (t.delay) rows.push(row("Delay", esc(t.delay.reason), setBy(t.delay.by) + " · until " + esc(when(t.delay.until))));
  if (t.disabled) rows.push(row("Disabled", esc(t.disabled.reason), setBy(t.disabled.by, t.disabled.at) + " · until someone enables it"));
  const waits = (t.waiting || []).map(w => `<div class="t-m"><span class="pl-ver">${esc(short(w.rev))}</span> waits since ${esc(ago(w.since))}`
    + (w.outside ? ` · asked by ${esc(w.outside)}${w.outside_at ? ", last " + esc(ago(w.outside_at)) : ""}` : "")  // #6164: an outside flow
    + (w.approved_by ? ` · yes from ${esc(w.approved_by)}` : "") + (w.passed_by ? ` · let pass by ${esc(w.passed_by)}` : "")
    + (w.note ? `<div class="muted t-s">${esc(w.note)}</div>` : "") + `</div>`).join("");
  return `<h3>The transition ${esc(from)} → ${esc(s.name)} ${chip(cls, `${esc(m)} ${esc(word)}`)}</h3>
    <div class="t-m">${t.state === "open" ? "Open: " + esc(t.text) : esc(t.state_text)}</div>
    ${waits || (D.default ? "" : `<div class="muted t-s">No version waits here now.</div>`)}
    <div class="pl-table"><table class="pl-ctl"><tr><th>Control</th><th>Now</th><th>Set by, until</th></tr>${rows.join("")}</table></div>
    ${arrowActions(s, t)}
    <div class="muted t-s">The tests and checks run inside the stages; a transition only lets versions in. Each change is a line in the audit log with who made it, why and until when.</div>`;
}

function arrowActions(s, t) {
  if (t.fixed || (V.data.default && !V.data.controls))
    return `<div class="muted t-s">${t.fixed_why ? esc(t.fixed_why[0].toUpperCase() + t.fixed_why.slice(1)) + "." : "A deploy target's own steps decide this."}</div>`;
  const w = (t.waiting || [])[0], b = [], n = s.name;
  if (w && NEEDS_YES.includes(t.mode) && !w.approved_by)
    b.push(button(`Approve ${esc(short(w.rev))}`, { kind: "primary", data: { "pl-approve": n, rev: w.rev }, title: "Give its yes" }));
  b.push(t.block ? button("Unblock", { data: { "pl-unblock": n }, title: "End the block" })
    : button("Block until…", { kind: "warn", data: { "pl-block": n }, title: "Stop versions here" }));
  b.push(t.delay ? button("End the delay", { data: { "pl-undelay": n }, title: "End the delay" })
    : button("Delay…", { data: { "pl-delay": n }, title: "Hold for a time" }));
  b.push(t.disabled ? button("Enable", { data: { "pl-enable": n }, title: "Let versions pass" })
    : button("Disable…", { data: { "pl-disable": n }, title: "Until enabled" }));
  if (w && t.state !== "open") b.push(button(`Let ${esc(short(w.rev))} pass once…`, { data: { "pl-pass": n, rev: w.rev }, title: "Past every control" }));
  b.push(button("Freeze…", { data: { "pl-freeze": n }, title: "Stop for a period" }));
  if ((t.freezes || []).length) b.push(button("Remove the freezes", { data: { "pl-unfreeze": n }, title: "Remove the freezes" }));
  b.push(button(V.data.default ? "Change the window…" : "Change the mode or the window…", { data: { "pl-change": n }, title: "The standing rules" }));
  // #6176: a deploy target's arrow holds its release items, not versions: it names them.
  const holds = (t.holds || []).map(h => `<span class="link" data-open="${h.id}">#${h.id}</span> ${esc(h.kind)}`).join(", ");
  return `<div class="pl-bar">${b.join("")}</div>${holds ? `<div class="t-s">It holds ${holds} now.</div>` : ""}`
    + (V.data.default ? `<div class="muted t-s">The mode comes from the deploy target (its release cadence, the person's yes). A closed transition holds the release item after it; the person's approval item and the deploy hook still release it.</div>` : "");
}

function detail() {
  const a = V.arrow && V.data && V.data.stages.find(s => s.name === V.arrow);
  if (a) return arrowDetail(a);
  const s = chosen(); if (!s) return "";
  if (s.holding) return holdingDetail(V.name);
  const r = V.run && (V.run.id == null || (s.last_run && V.run.id === s.last_run.id)) ? V.run : s.last_run;
  const head = `<h3>${esc(s.name)}${r && r.version ? ` · <span class="pl-ver">${esc(short(r.version))}</span>` : ""}</h3>`;
  if (!r) return head + `<div class="muted t-s">No run of this stage yet.${s.run ? " It runs: " + esc(s.run) : ""}</div>` + keysHtml(s);
  const when = r.started_at ? `started ${esc(ago(r.started_at))}` : r.queued_at ? `waiting since ${esc(ago(r.queued_at))}` : "";
  const item = (r.item ? ` · <span class="link" data-open="${r.item}">#${r.item}</span>` : "")
    + (r.fault_item ? ` · fault <span class="link" data-open="${r.fault_item}">#${r.fault_item}</span>` : "");
  return head + `<div class="pl-bar">${st(r.state)}<span class="muted t-s">${when}${r.ended_at ? ` · ${Date.parse(r.ended_at) > Date.now() ? "" : "ended "}${esc(endText(r.ended_at))}` : ""}${item}</span></div>`
    + (r.gate_note && r.state === "queued" ? `<div class="t-s">At the transition: ${esc(r.gate_note)}</div>` : "")
    + stageActions(s, r)
    + (r.errors ? `<div class="t-s c-warn">${esc(r.errors)}</div>` : "")
    + wavesHtml(s, r)
    + (r.watch_end ? watchHtml(r) : gantt(r.step_list || []))
    + (r.output ? `<pre class="pl-pre">${esc(r.output)}</pre>` : "")
    + (r.test_list && r.test_list.length ? tests(r) : "")
    + keysHtml(s);
}

// A stage over many servers (#6120): one card for each wave, each server with the state of its deploy, its check
// and, after a failed wave, its roll back (the steps of the run with where=<host>).
function wavesHtml(s, r) {
  let waves;
  try { waves = s.waves ? JSON.parse(s.waves) : null; } catch (e) { waves = null; }
  if (!waves || !waves.length) return "";
  const of = (host, name) => (r.step_list || []).filter(x => x.host === host && x.name === name).pop();
  const cell = (x, word) => x ? st(x.state, `${word} ${WORD[x.state] || x.state}`) : "";
  const card = (w, n) => `<div class="pl-wave"><b class="t-m">Wave ${n + 1}</b>${w.map(h => {
    const d = of(h, "deploy"), c = of(h, "check"), b = of(h, "rollback");
    return `<div class="pl-host"><span class="pl-ver">${esc(h)}</span><span class="pl-bar">${d ? cell(d, "deploy") + cell(c, "check")
      + cell(b, "roll back") : st("queued", "not started")}</span></div>`;
  }).join("")}</div>`;
  return `<div class="pl-waves">${waves.map(card).join("")}</div>`;
}

// The actions on a stage (#6117): only a person or the manager acts; a default pipeline (a target's releases) has
// its own actions on Releases. The controls before the stage are on its transition (#6139).
function stageActions(s, r) {
  if (V.data.default) return targetActions(s, r);
  if (V.data.followed) return `<div class="muted t-s">Followed: its own system runs this stage. A trigger rule acts on its events (Triggers).</div>`;  // #6167
  const rev = r && r.version, b = [];
  if (s.transition) b.push(button("The transition before it", { data: { "pl-arrow": s.name }, title: "Its controls" }));
  const D = V.data, miss = [], ours = r && r.state === "queued" && !r.runner && (!r.source || r.source === "maxpm");
  if (r && ["running", "queued"].includes(r.state) && r.id != null)
    b.push(srcButton(D, ours ? null : s, "stop", "Stop", { kind: "warn", data: { "pl-stop": r.id }, title: "Stop this run" }, miss));
  if (r && rev && !["running", "queued"].includes(r.state)) b.push(srcButton(D, s, "rerun", "Run again", { data: { "pl-rerun": s.name, rev }, title: "Run this stage again" }, miss));
  // The override (#6146): a version came here past the stages before; run them for it, or send another one here.
  if (s.override) b.push(button(`Run the whole pipeline for ${esc(short(s.override.rev))}`, { kind: "primary", data: { "pl-whole": s.override.rev }, title: "Run the skipped stages" }));
  if ((s.prev || []).length || D.stages.indexOf(s) > 0)
    b.push(button("Send a version here directly…", { kind: "warn", data: { "dp-tab": "emergency", "em-preset": s.name }, title: "Override" }));
  const bad = r && r.tests ? (r.tests.failed || 0) + (r.tests.error || 0) : 0;
  if (bad && r.id != null && !["running", "queued"].includes(r.state))
    b.push(srcButton(D, s, "rerun", `Run the ${bad} failed again`, { data: { "pl-refail": r.id }, title: "Only the failed tests" }, miss));
  return `<div class="pl-bar">${b.join("")}</div>${missLine(D, miss)}`;
}

// The actions on a stage of a deploy target (#6176): its transition, and for deploy a version sent there directly (a
// deploy item that still waits for the person's yes); Versions in <stage> under it has Run again and Move back here.
function targetActions(s, r) {
  if (!V.data.controls) return "";
  const b = [];
  if (s.transition && !s.transition.fixed) b.push(button("The transition before it", { data: { "pl-arrow": s.name }, title: "Its controls" }));
  if (s.name === "deploy") b.push(button("Send a version here directly…", { kind: "warn", data: { "dp-tab": "emergency", "em-preset": s.name }, title: "Override" }));
  return b.length ? `<div class="pl-bar">${b.join("")}</div>` : "";
}

// The reason of a hold or a roll back: a small dialog (components/dialog.js). The Emergency tab asks with it too.
export function askReason(title, text, go, then) {
  askForm(title, text, go, [], (v) => then(v.why));
}

// The same dialog with fields above the reason: fields [{id, label, value, hint, options}] (options: a select).
// needWhy false: the reason may stay empty. then(values) gets {why, <id>: value}.
let askDlg = null, askBox = null;
export function askForm(title, text, go, fields, then, needWhy = true) {
  if (!askDlg) {
    const d = document.createElement("div");
    d.id = "plAskDlg"; d.className = "hidden";
    d.innerHTML = '<div class="box"></div>';
    document.body.appendChild(d);
    askBox = d.firstElementChild;
    askDlg = makeDialog(d);
  }
  const ctl = (f) => f.options ? select({ id: "plF-" + f.id }, f.options, f.value)
    : input({ id: "plF-" + f.id, value: f.value || "", placeholder: f.hint || null });
  askBox.innerHTML = `<div class="dlg-head"><h2>${esc(title)}</h2>${iconButton("✕", "Close", { data: { "pl-askclose": "1" } })}</div>
    <p class="t-m">${esc(text)}</p>
    <div class="form">${fields.map(f => field(esc(f.label), ctl(f))).join("")}${field(needWhy ? "Why?" : "Why? (optional)", textarea({ id: "plWhy", rows: 3 }, ""))}</div>
    ${actions(button("Cancel", { data: { "pl-askclose": "1" } }) + button(go, { kind: "primary", data: { "pl-askgo": "1" } }))}`;
  askDlg.open();
  askBox.querySelector(fields.length ? "#plF-" + fields[0].id : "#plWhy").focus();
  askBox.onclick = async (e) => {
    if (e.target.closest("[data-pl-askclose]")) return askDlg.close();
    if (!e.target.closest("[data-pl-askgo]")) return;
    const v = { why: (askBox.querySelector("#plWhy").value || "").trim() };
    for (const f of fields) v[f.id] = (askBox.querySelector("#plF-" + f.id).value || "").trim();
    if (needWhy && !v.why) return toast("Say why", true);
    askDlg.close();
    await then(v);
  };
}

// The buttons of a transition (#6139): each one asks why (the audit log keeps it) and what it needs.
function arrowAct(d) {
  const n = d.plBlock || d.plUnblock || d.plDelay || d.plUndelay || d.plDisable || d.plEnable || d.plPass || d.plFreeze
    || d.plUnfreeze || d.plChange;
  const s = V.data.stages.find(x => x.name === n), t = s && s.transition;
  if (!t) return;
  const into = `the transition into ${n}`;
  if (d.plBlock) return askForm(`Block ${into}?`, "No version passes until the time you give, until a task closes, or until someone unblocks it. A version you let pass on purpose still does.",
    "Block", [{ id: "until", label: "Until", hint: "2h, fri 17:00, #<task>, or empty" }],
    (v) => doAct("pipeline_block", { stage: n, reason: v.why, ...(v.until.startsWith("#") ? { until_done: +v.until.slice(1) } : v.until ? { until: v.until } : {}) }));
  if (d.plUnblock) return doAct("pipeline_unblock", { stage: n });
  if (d.plDelay) return askForm(`Delay ${into}?`, "No version passes before this time. Then the transition works as before.", "Delay",
    [{ id: "until", label: "Until", hint: "2h, mon 09:00" }], (v) => v.until ? doAct("pipeline_delay", { stage: n, until: v.until, reason: v.why }) : toast("Say until when", true));
  if (d.plUndelay) return doAct("pipeline_delay", { stage: n, until: "none" });
  if (d.plDisable) return askForm(`Disable ${into}?`, "No version passes until a person or the manager enables it.", "Disable", [],
    (v) => doAct("pipeline_disable", { stage: n, reason: v.why }));
  if (d.plEnable) return doAct("pipeline_enable", { stage: n });
  if (d.plPass) return askForm(`Let ${short(d.rev)} pass once?`, "This one version passes every control of the transition now: the block, the fault rule, a freeze, the window, a delay. A yes that only a named person gives still waits for that person.",
    "Let it pass", [], (v) => doAct("pipeline_pass", { stage: n, rev: d.rev, reason: v.why }));
  if (d.plFreeze) return askForm(`Freeze ${into}?`, "No version passes in this period. To freeze every transition of the pipeline, run maxpm pipeline freeze with no stage.",
    "Freeze", [{ id: "from", label: "From (empty: now)", hint: "wed 18:00, 2026-11-26T00:00" }, { id: "to", label: "To", hint: "mon 08:00, 3d" }],
    (v) => v.to ? doAct("pipeline_freeze", { stage: n, from: v.from || null, to: v.to, reason: v.why }) : toast("Say until when", true));
  if (d.plUnfreeze) return doAct("pipeline_freeze", { stage: n, clear: true });
  const target = V.data.default;  // #6176: a deploy target's mode comes from the target
  return askForm(`Change ${into}`, (target ? "" : "The mode is how a version passes when nothing holds it. ") + "The window says when versions may pass (for example mon-thu 09:00-17:00 America/Los_Angeles); empty: at any time.",
    "Save", [...(target ? [] : [{ id: "mode", label: "Mode", value: t.words, hint: "auto, yes mark, delay 10m, release" }]),
      { id: "window", label: "Time window", value: t.window ? t.window.text : "", hint: "mon-thu 09:00-17:00" },
      { id: "fault", label: "Fault rule", options: [["on", "Block it"], ["off", "Let versions pass"]], value: t.fault_rule ? "on" : "off" }],
    (v) => {
      const args = { stage: n, reason: v.why || null };
      if (v.mode && v.mode !== t.words) args.mode = v.mode;
      if (v.window !== (t.window ? t.window.text : "")) args.window = v.window || "none";
      if ((v.fault === "on") !== !!t.fault_rule) args.fault_rule = v.fault === "on";
      if (Object.keys(args).length === 2) return toast("Nothing changed");
      return doAct("pipeline_transition", args);
    }, false);
}

async function doAct(op, args) {
  try { await act(op, { pipeline: V.name, ...args }); } catch (e) { return; }
  runSig = ""; sig = ""; srSig = "";
  renderPipeline(true);
}

// ---- the test view: each test's result, failed first, and the prompt that analyzes the failures

function tests(r) {
  const all = r.test_list, bad = all.filter(t => t.state === "failed" || t.state === "error");
  const skipped = all.filter(t => t.state === "skipped").length;
  const shown = V.filter === "failed" && bad.length ? bad : all;
  const suites = [...new Set(all.map(t => t.suite))];
  return `<div class="pl-bar">${st(bad.length ? "failed" : "passed", `${bad.length} failed`)}${st("passed", `${all.length - bad.length - skipped} passed`)}${st("skipped", `${skipped} skipped`)}
      <span class="muted t-s">from the test report (JUnit XML)</span></div>
    ${suites.slice(0, 20).map(su => `<div class="pl-suite"><span class="pl-sname">${esc(su)}</span><span class="pl-dots">${all.filter(t => t.suite === su).map(t =>
      `<span class="pl-dot ${esc(t.state)}" title="${esc(t.name)}: ${esc(t.state)}"></span>`).join("")}</span></div>`).join("")}
    <div class="pl-bar">${button(`Failed (${bad.length})`, { pressed: V.filter === "failed", data: { "pl-filter": "failed" } })}${button(`All ${all.length}`,
      { pressed: V.filter === "all", data: { "pl-filter": "all" } })}</div>
    <div class="pl-table"><table class="pl-tests"><tr class="pl-th"><th>Test</th><th>Result</th><th>Time</th><th>Message</th></tr>
      ${shown.slice(0, 500).map(t => `<tr><td class="mono">${esc(t.suite)}.${esc(t.name)}</td><td>${st(t.state)}</td><td>${esc(secs(t.seconds))}</td>
        <td class="t-s">${esc(t.message || "")}</td></tr>`).join("")}</table></div>
    ${r.prompt ? `<div class="pl-card"><h3>Prompt to analyze the failures</h3><pre class="pl-pre">${esc(r.prompt)}</pre>
      <div class="pl-bar">${button("Start a session", { kind: "primary", data: { "pl-fix": r.id, start: 1 }, title: "Fix it now" })}${button("Add a fix task",
        { data: { "pl-fix": r.id }, title: "Queue a fix" })}${button("Copy", { data: { "pl-copy": 1 }, title: "Copy the prompt" })}</div></div>` : ""}`;
}

// ---- the versions in each stage

function versionsView(D) {
  if (!D.versions.length) return empty("No version went through this pipeline yet.");
  const names = D.stages.map(s => s.name);
  const live = new Set(D.envs.map(e => e.rev).filter(Boolean));
  const cell = (c) => c ? st(c.state) : `<span class="muted">·</span>`;
  return `<div class="muted t-s">Each row is one version, newest on top. Read across to see how far it went.</div>
    <div class="pl-table"><table class="pl-vtable"><tr><th>Version</th>${names.map(n => `<th>${esc(n)}</th>`).join("")}</tr>
    ${D.versions.map(v => `<tr class="${live.has(v.rev) ? "live" : ""}"><td class="mono">${v.deploy
        ? `<span class="link" data-report="${v.deploy}" title="The release report">${esc(short(v.rev))}</span>`
        : button(esc(short(v.rev)), { cls: "plain", data: { "pl-version": v.rev }, title: "Its tasks", pressed: V.version === v.rev })}${v.title ? ` <span class="muted t-s">${esc(v.title)}</span>` : ""}</td>
      ${names.map(n => `<td>${cell(v.stages[n])}</td>`).join("")}</tr>`).join("")}</table></div>
    <div id="plTasks"></div>`;
}

// The tasks of a version (#6118): the items its commits name since the version before; a click opens one.
async function drawTasks(rev) {
  const box = $("#plTasks"); if (!box) return;
  if (!rev) { box.innerHTML = ""; return; }
  let T;
  try { T = await getJson(`api/pipeline-version?pipeline=${encodeURIComponent(V.name)}&rev=${encodeURIComponent(rev)}`); }
  catch (e) { box.innerHTML = `<div class="muted t-s">${esc(e.message)}</div>`; return; }
  box.innerHTML = `<div class="pl-card"><h3>Tasks of ${esc(short(rev))}${T.base ? ` <span class="muted t-s">since ${esc(short(T.base))}</span>` : ""}</h3>
    ${T.tasks.length ? T.tasks.map(t => `<div class="t-m"><span class="link" data-open="${t.id}">#${t.id}</span> ${esc(t.title)}
      <span class="muted t-s">${esc(t.status)}</span></div>`).join("") : `<div class="muted t-s">No commit of this version names a task.</div>`}</div>`;
}

// ---- clicks

$("#dpStages").addEventListener("click", (e) => {
  // #6189: a button in the transition's dialog closes it first (its action may open a dialog of its own).
  if (arrowDlg && e.target.closest("#plArrowDlg") && e.target.closest("button")) arrowDlg.close();
  const vb = e.target.closest("[data-pl-version]");
  if (vb) { V.version = V.version === vb.dataset.plVersion ? null : vb.dataset.plVersion; sig = ""; renderPipeline(true).then(() => drawTasks(V.version)); return; }
  const fx = e.target.closest("[data-pl-fix]");
  if (fx) return (async () => {
    let r;
    try { r = await act("pipeline_fix", { run: +fx.dataset.plFix, start: !!fx.dataset.start }); } catch (err) { return; }
    toast(fx.dataset.start ? `Fix task #${r.item} added; a session starts on it` : `Fix task #${r.item} added`);
  })();
  const tr = e.target.closest("[data-pl-block],[data-pl-unblock],[data-pl-delay],[data-pl-undelay],[data-pl-disable],[data-pl-enable],[data-pl-pass],[data-pl-freeze],[data-pl-unfreeze],[data-pl-change]");
  if (tr) return arrowAct(tr.dataset);
  const whole = e.target.closest("[data-pl-whole]");  // #6146: the stages an override skipped
  if (whole) return doAct("pipeline_emergency", { action: "whole", rev: whole.dataset.plWhole });
  const a = e.target.closest("[data-pl-hold],[data-pl-unhold],[data-pl-approve],[data-pl-stop],[data-pl-rerun],[data-pl-refail],[data-pl-rollback],[data-pl-moveback]");
  if (a) {
    const d = a.dataset;
    if (d.plMoveback && d.branches) return askReason(`Move every branch back to ${short(d.rev)}?`,
      `${d.plMoveback} and ${d.branches} run again with this version where it passed, and the transition into each is blocked until someone unblocks it. A branch where it never passed stays as it is.`,
      "Move back every branch", (reason) => doAct("pipeline_move_back", { stage: d.plMoveback, rev: d.rev, reason, every_branch: true }));
    if (d.plMoveback) return askReason(`Move ${d.plMoveback} back to ${short(d.rev)}?`,
      `The stage runs again with this version, as one more run, and the transition into ${d.plMoveback} is blocked until someone unblocks it, so no newer version comes by itself.`,
      "Move back", (reason) => doAct("pipeline_move_back", { stage: d.plMoveback, rev: d.rev, reason }));
    if (d.plHold !== undefined) return askReason("Hold every stage?",
      "Nothing new starts in any stage. Work that runs goes on to its end.", "Hold",
      (reason) => doAct("pipeline_hold", { stage: null, reason }));
    if (d.plUnhold !== undefined) return doAct("pipeline_unhold", { stage: d.plUnhold || null });
    if (d.plApprove) return doAct("pipeline_approve", { stage: d.plApprove, rev: d.rev });
    if (d.plStop) return doAct("pipeline_stop", { run: +d.plStop });
    if (d.plRerun) return doAct("pipeline_rerun", { stage: d.plRerun, rev: d.rev });
    if (d.plRefail) return doAct("pipeline_rerun_failed", { run: +d.plRefail });
    if (d.plRollback) return askReason(`Put ${short(d.rev)} back on ${d.plRollback}?`,
      "Its deploy stage runs with this version, and the transition into it is blocked until you unblock it, so no newer version comes by itself.",
      "Roll back", (reason) => doAct("pipeline_rollback", { env: d.plRollback, rev: d.rev, reason }));
  }
  const run = e.target.closest("[data-pl-run-stage]");  // Run: a stage that starts manually (#6138)
  if (run) return doAct("pipeline_run_stage", { stage: run.dataset.plRunStage, rev: run.dataset.rev || null });
  const t = e.target.closest("[data-pl-stage],[data-pl-arrow],[data-pl-filter],[data-pl-copy],[data-pl-check],[data-pl-allruns]");
  if (!t) return;
  if (t.dataset.plAllruns) { V.allRuns = V.allRuns === t.dataset.plAllruns ? null : t.dataset.plAllruns; return refreshStageRuns(true); }  // #6182
  if (t.dataset.plCheck) return toggleCheck(t.dataset.plCheck);  // #6181: every run of the check in the watch
  if (t.dataset.plArrow) return openArrow(t.dataset.plArrow);  // #6189: a dialog, not the panel under the graph
  if (t.dataset.plStage) { V.stage = t.dataset.plStage; V.arrow = null; V.run = null; runSig = ""; draw(); return refreshRun(true); }
  if (t.dataset.plFilter) { V.filter = t.dataset.plFilter; runSig = ""; return refreshRun(true); }
  if (t.dataset.plCopy && V.run && V.run.prompt) copyText(V.run.prompt).then(() => toast("Copied the prompt"));
});
$("#dpStages").addEventListener("scroll", (e) => { if (e.target.id === "plScroll") fade(); }, true);
window.addEventListener("resize", () => { if (V.shown && V.tab === "stages") fit(); });
