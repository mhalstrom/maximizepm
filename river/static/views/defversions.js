// The versions of a pipeline's definition (#6143; canvas deploy-tab-mockup pictures 4 and 11: "definition v12 ·
// versions" in the head). The head of a pipeline's page names the version, who made it, when and why; Versions opens
// this dialog: each version newest first, what changed from the one before it, and Go back (one more version, the
// same as the one picked; a person or the manager, with a reason). Reads GET api/pipeline-versions; acts with the op
// pipeline_definition_back (river/routes/pipeline.py; the command maxpm pipeline versions).
import { esc, getJson, ago, act, toast } from "../lib.js";
import { button, iconButton } from "../components/button.js";
import { makeDialog } from "../components/dialog.js";
import { askReason } from "./pipeline.js";

let dlg = null, box = null, name = null, open = null, onDone = null;

// The words of the head: "definition v12 · mark, 2h ago: <why>" and the Versions button.
export function definitionHead(p) {
  const d = p && p.definition;
  if (!d) return "";
  return `<span class="muted t-s dv-head" title="${esc(d.why || "")}">definition v${d.n} · ${esc(d.actor || "maxpm")}, ${esc(ago(d.at))}</span>`
    + button("Versions", { cls: "plain", data: { "dv-open": p.name }, title: "Config history" });
}

export async function openVersions(pipeline, done) {
  name = pipeline; open = null; onDone = done;
  if (!dlg) {
    const el = document.createElement("div");
    el.id = "dvDlg"; el.className = "hidden";
    el.innerHTML = '<div class="box"></div>';
    document.body.appendChild(el);
    box = el.firstElementChild;
    dlg = makeDialog(el, { draft: false });
    box.addEventListener("click", onClick);
  }
  await draw();
  dlg.open();
}

async function draw() {
  let d;
  try { d = await getJson("api/pipeline-versions?pipeline=" + encodeURIComponent(name)); }
  catch (e) { return toast(e.message, true); }
  let diff = "";
  if (open) {
    try {
      const c = await getJson(`api/pipeline-versions?pipeline=${encodeURIComponent(name)}&compare=${open - 1},${open}`);
      diff = c.changes.length ? `<ul class="t-s dv-diff">${c.changes.map(x => `<li><b>${esc(x.what)}</b>: ${esc(show(x.a))} → ${esc(show(x.b))}</li>`).join("")}</ul>`
        : `<div class="muted t-s">The same as v${open - 1}.</div>`;
    } catch (e) { diff = `<div class="muted t-s">${esc(e.message)}</div>`; }
  }
  const newest = d.versions.length ? d.versions[0].n : 0;
  box.innerHTML = `<div class="dlg-head"><h2>Config history · ${esc(d.pipeline)}</h2>${iconButton("✕", "Close", { data: { "dv-close": 1 } })}</div>
    <div class="pl-table"><table><tr><th>Version</th><th>When</th><th>Who</th><th>Why</th></tr>
    ${d.versions.map(v => `<tr${v.n === newest ? ' class="live"' : ""}><td class="mono tg-nw">v${v.n}${v.n === newest ? ` <span class="muted t-s">now</span>` : ""}</td>
      <td class="t-s tg-nw" title="${esc(v.at)}">${esc(ago(v.at))}</td><td class="t-s">${esc(v.actor || "maxpm")}</td>
      <td class="t-s dv-why">${esc(v.why || "")}<div class="pl-bar">${v.n > 1 ? button(open === v.n ? "Hide" : "Changes", { data: { "dv-diff": v.n }, title: `What v${v.n} changed` }) : ""}
        ${v.n !== newest ? button("Go back…", { kind: "warn", data: { "dv-back": v.n }, title: `Back to v${v.n}` }) : ""}</div>
        ${open === v.n ? diff : ""}</td></tr>`).join("")}</table></div>`;
}
const show = (v) => v == null ? "none" : typeof v === "string" ? v : JSON.stringify(v);

async function onClick(e) {
  if (e.target.closest("[data-dv-close]")) return dlg.close();
  const d = e.target.closest("[data-dv-diff]");
  if (d) { open = open === +d.dataset.dvDiff ? null : +d.dataset.dvDiff; return draw(); }
  const b = e.target.closest("[data-dv-back]");
  if (b) {
    const n = +b.dataset.dvBack;
    return askReason(`Go back to definition v${n}?`, `The stages, branches, transitions, trigger rules and runners of ${name} become what v${n} was, as one more version.`,
      "Go back", async (reason) => {
        try { await act("pipeline_definition_back", { pipeline: name, n, reason }); } catch (err) { return; }
        toast(`${name}: back to v${n}`);
        await draw();
        if (onDone) onDone();
      });
  }
}
