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

// The inside of the holding stage card (views/pipeline.js stageCard).
export function holdingCardHtml(h) {
  const n = h.commits || 0;
  return `<span class="pl-kind">holding</span><span class="pl-name">holding</span>
    <span class="pl-ver">${h.reading ? "reading…" : n ? plural(n, "commit") : h.folder ? "nothing waits" : "no git folder"}</span>
    ${n ? `<span class="pl-st queued">● ${h.reviewed} of ${n} reviewed</span>` : `<span class="pl-st none">● empty</span>`}
    <span class="pl-meta pl-more-line">${n ? `+${h.added} −${h.removed}` : ""}${h.base_label ? ` · since ${esc(h.base_label)}` : ""}</span>`;
}

function table(H) {
  return `<div class="pl-table pl-blocks"><table><tr><th>Commit</th><th>What</th><th>Task</th><th>By</th><th>Review</th><th>Changes</th><th></th></tr>
    ${H.commits.map(c => `<tr><td class="mono">${esc(short(c.sha))}</td><td>${esc(c.subject)}</td>
      <td>${c.tasks.map(t => `<span class="link" data-open="${t.id}" title="${esc(t.title)}">#${t.id}</span>`).join(" ")}</td>
      <td class="t-s">${esc(c.session)}${c.by_session ? "" : ` <span class="muted">(git author)</span>`}</td>
      <td class="t-s"><span class="pl-st ${CLS[c.review.state]}">${MARK[c.review.state]} ${esc(c.review.text)}</span>
        ${c.review.release ? ` <span class="link" data-report="${c.review.release}">report</span>` : ""}</td>
      <td class="mono">+${c.added} −${c.removed}</td>
      <td>${button("Diff", { cls: "plain", data: { "hd-diff": c.sha }, title: "Show this commit" })}</td></tr>`).join("")}
  </table></div>`;
}

function html(H) {
  const n = H.commits.length, c = H.counts, rel = H.release || {};
  if (!H.folder) {
    return `<h3>Holding</h3><div class="muted t-s">The project of ${esc(H.pipeline)} has no git folder, so holding shows no commits. Link one: maxpm project path ${esc(H.project || "<project>")} &lt;folder&gt;</div>`;
  }
  const upto = H.upto ? H.commits.findIndex(x => x.sha === H.upto) + 1 : 0;
  const acts = [
    n ? button(`View all changes (+${H.added} −${H.removed})`, { data: { "hd-all": 1 }, title: "Every commit in holding" }) : "",
    H.report ? `<span class="link t-m" data-report="${H.report}">Open the review report #${H.report}</span>` : "",
    rel.can && upto ? button(`Release the ${plural(upto, "reviewed commit")}`, { kind: "primary", data: { "hd-release": "reviewed" }, title: "Release the reviewed ones" }) : "",
    rel.can && n ? button(`Release all ${n}`, { data: { "hd-release": "all" }, title: "Every commit in holding" }) : "",
  ].filter(Boolean).join("");
  return `<h3>Holding · ${plural(n, "commit")} since ${esc(H.base_label || "the start")}</h3>
    ${n ? `<div class="pl-bar"><span class="pl-st passed">✓ ${c.reviewed} reviewed</span><span class="pl-st running">◐ ${c.in_review} in review</span>
      <span class="pl-st queued">○ ${c.not_reviewed} not reviewed</span></div>` : ""}
    <div class="pl-bar hd-acts">${acts}</div>
    <div class="t-s">The release after holding: ${esc(rel.text || "")}${rel.why ? ` · <span class="muted">${esc(rel.why)}</span>` : ""}${rel.plan ? ` · <span class="muted">${esc(rel.plan)}</span>` : ""}</div>
    ${n ? table(H) : `<div class="muted t-s">No commit waits: every commit up to ${esc(short(H.head))} has left holding.</div>`}
    <div class="muted t-s">A release takes the reviewed commits in order, up to the first one that is not reviewed; Release all takes every one, for a pipeline whose review comes after the release.</div>`;
}

// The detail of the holding stage: what was read last at once, then a new read (views/pipeline.js detail()).
export function holdingDetail(name) {
  setTimeout(() => refreshHolding(name), 0);
  return `<div class="hd" data-hd="${esc(name)}">${cache[name] ? cache[name].html : `<div class="muted t-s">Reading the commits…</div>`}</div>`;
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
  const box = t.closest(".hd"), name = box && box.dataset.hd;
  if (!name) return;
  if (t.dataset.hdDiff) return openHoldingChanges(name, t.dataset.hdDiff);
  if (t.dataset.hdAll) return openHoldingChanges(name, null);
  const H = cache[name] && cache[name].data;
  const all = t.dataset.hdRelease === "all";
  const n = all ? H.commits.length : H.commits.findIndex(x => x.sha === H.upto) + 1;
  const last = all ? H.commits[H.commits.length - 1] : H.commits[n - 1];
  try { await act("pipeline_release", { pipeline: name, all, upto: last ? last.sha : null }); } catch (err) { return; }
  toast(`Released ${plural(n, "commit")} of ${name}, up to ${short(last && last.sha)}`);
  refreshHolding(name);
});
