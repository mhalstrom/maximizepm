// Holding, the first stage of every pipeline (#6140; canvas release-tracking task S, canvas deploy-tab-mockup picture
// 4; mark 2026-11-18: "a holding stage at the beginning that shows the commits, releases/ review reports so we can
// see whats been reviewed, see the code changes", and "cut is not the right thing to call it. its more like its the
// actual release": the page says release, never cut). It lists the commits since the last version that left it, the
// oldest first, each with its tasks, the session that wrote it, its review and its changes: View all changes, Diff
// for one commit, Open the review report, and the release after it (the reviewed commits up to the first one not
// reviewed, or all of them). Data: GET api/pipeline-holding/<name> (river/pipeline/targets.py holding); the release is
// the op pipeline_release.
import { esc, getJson, act, toast } from "../lib.js";
import { button } from "../components/button.js";
import { openHoldingChanges } from "../components/changesView.js";

const MARK = { ok: "✓", run: "◐", bad: "✕", wait: "○" };
const CLS = { ok: "passed", run: "running", bad: "failed", wait: "queued" };
const cache = {};  // pipeline name -> {sig, html}: the detail draws at once, then reads again
const short = (sha) => (sha || "").slice(0, 8);
const plural = (n, one) => `${n} ${one}${n === 1 ? "" : "s"}`;

// A count of lines in a card: 33912 is 34k (#6209).

// The inside of the holding stage card (views/pipeline.js stageCard).
export function holdingCardHtml(h) {
  const n = h.commits || 0;
  return `<span class="pl-kind">holding</span><span class="pl-name">Next release</span>
    <span class="pl-ver pl-words">${h.reading ? "Loading…" : n ? plural(n, "commit") : h.folder ? "Nothing queued" : "No repo"}</span>
    ${n && h.review_enabled ? `<span class="pl-st queued">● ${h.reviewed} of ${n} reviewed</span>` : ""}`;
}

function table(H) {
  return `<div class="pl-table pl-blocks"><table><tr><th>Commit</th><th>Message</th><th>Task</th><th>Author</th>${H.review_enabled ? "<th>Review</th>" : ""}<th>Lines</th><th></th></tr>
    ${H.commits.map(c => `<tr><td class="mono">${esc(short(c.sha))}</td><td>${esc(c.subject)}</td>
      <td>${c.tasks.map(t => `<span class="link" data-open="${t.id}" title="${esc(t.title)}">#${t.id}</span>`).join(" ")}</td>
      <td class="t-s">${esc(c.session)}</td>
      ${c.review ? `<td class="t-s"><span class="pl-st ${CLS[c.review.state]}">${MARK[c.review.state]} ${esc(c.review.text)}</span>
        ${c.review.release ? ` <span class="link" data-report="${c.review.release}">Report</span>` : ""}</td>` : ""}
      <td class="mono">+${c.added} −${c.removed}</td>
      <td>${button("Diff", { cls: "plain", data: { "hd-diff": c.sha }, title: "Diff" })}</td></tr>`).join("")}
  </table></div>`;
}

function checksHtml(checks) {
  return (checks || []).map(({ branch, last_run: run, error }) => {
    const state = run?.state || "none";
    return `<div class="pl-bar"><span>Tests (${esc(branch)})</span>
      <span class="pl-st ${esc(state)}">● ${esc(error ? "Refresh failed" : run?.state || "No run")}</span>
      ${run ? `<span class="mono">${esc(short(run.version))}</span>` : ""}
      ${run?.url ? button("GitHub", { cls: "plain", data: { url: run.url }, title: "GitHub" }) : ""}</div>`;
  }).join("");
}

function html(H) {
  const n = H.commits.length, c = H.counts;
  if (!H.folder) return `<h3>Next release</h3>${checksHtml(H.checks)}<div class="muted t-s">No git folder set.</div>`;
  // The release buttons are at the top of the pipeline's page with every release control (#6189).
  const acts = [
    n ? button(`Diff (+${H.added} −${H.removed})`, { data: { "hd-all": 1 }, title: "Diff" }) : "",
    H.report ? `<span class="link t-m" data-report="${H.report}">Report</span>` : "",
  ].filter(Boolean).join("");
  return `<h3>Next release · ${plural(n, "commit")}</h3>
    ${checksHtml(H.checks)}
    ${n && H.review_enabled ? `<div class="pl-bar"><span class="pl-st passed">✓ ${c.reviewed} reviewed</span><span class="pl-st running">◐ ${c.in_review} in review</span>
      <span class="pl-st queued">○ ${c.not_reviewed} not reviewed</span></div>` : ""}
    <div class="pl-bar hd-acts">${acts}</div>
    ${n ? table(H) : `<div class="muted t-s">Up to date.</div>`}`;
}

// The detail of the holding stage: what was read last at once, then a new read (views/pipeline.js detail()).
export function holdingDetail(name) {
  setTimeout(() => refreshHolding(name), 0);
  return `<div class="hd" data-hd="${esc(name)}">${cache[name] ? cache[name].html : `<div class="muted t-s">Loading…</div>`}</div>`;
}

async function refreshHolding(name) {
  let H;
  try { H = await getJson("api/pipeline-holding/" + encodeURIComponent(name)); }
  catch (e) { H = null; cache[name] = { sig: "", html: `<div class="muted t-s">${esc(e.message)}</div>` }; }
  if (H) {
    const sig = JSON.stringify(H);
    if (cache[name] && cache[name].sig === sig && document.querySelector(`.hd[data-hd="${CSS.escape(name)}"] h3`)) return;
    cache[name] = { sig, html: html(H), data: H };
  }
  const box = document.querySelector(`.hd[data-hd="${CSS.escape(name)}"]`);
  if (box) box.innerHTML = cache[name].html;
}

document.addEventListener("click", async (e) => {
  const t = e.target.closest("[data-hd-diff],[data-hd-all],[data-hd-release]");
  if (!t) return;
  const box = t.closest(".hd"), name = (box && box.dataset.hd) || t.dataset.hdPipe;
  if (!name) return;
  if (t.dataset.hdDiff) return openHoldingChanges(name, t.dataset.hdDiff);
  if (t.dataset.hdAll) return openHoldingChanges(name, null);
  // A release button at the top of the pipeline's page (#6189) names the pipeline, the last commit and the count.
  const all = t.dataset.hdRelease === "all", n = +t.dataset.n || 0, sha = t.dataset.sha || null;
  try { await act("pipeline_release", { pipeline: name, all, upto: sha }); } catch (err) { return; }
  toast(`Released ${plural(n, "commit")} of ${name}, up to ${short(sha)}`);
  refreshHolding(name);
});
