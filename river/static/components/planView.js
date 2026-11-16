// The plan of a design document (#6014, design-doc-mockup v7 part 2), under the outline row of the side panel: its
// plan versions (only the versions that changed its tasks, their parts, or a done-when line; core.plan_versions), why
// each one came (the version's note), and Compare of two versions: the tasks added, removed and moved to another part,
// each with its state now. A task opens in the drawer (data-open). Data: GET api/plan/<canvas id>.

const STATE = { done: "done", dropped: "dropped", in_progress: "in progress", held: "held", open: "open" };

const taskRow = (t, mark, esc) => `<div class="pl-t ${mark}"><span class="pl-m" aria-hidden="true">${
  mark === "added" ? "+" : mark === "removed" ? "−" : mark === "changed" ? "~" : "·"}</span>
  <span class="link" data-open="${t.id}">#${t.id}</span> ${esc(t.title || "(no such item)")}
  <span class="muted t-s">${esc(STATE[t.status] || t.status || "")}${t.part ? " · " + esc(t.part) : ""}</span></div>`;

// data: plan_versions; a, b: the plan versions to compare (b the newer); esc: the page's escape.
export function planHtml(data, a, b, esc) {
  const P = (data && data.plans) || [];
  if (!P.length) return `<div class="muted t-s">No plan yet: its Plan part is still being written.</div>`;
  const vb = P.find(p => p.plan_version === b) || P[P.length - 1];
  const va = P.find(p => p.plan_version === a && a < vb.plan_version) || P[Math.max(0, P.indexOf(vb) - 1)];
  const opt = (sel) => P.map(p => `<option value="${p.plan_version}"${p.plan_version === sel ? " selected" : ""}>v${p.plan_version}</option>`).join("");
  const head = `<div class="pl-head"><b>Plan</b> <span class="muted t-s">${P.length} version${P.length === 1 ? "" : "s"}</span>
    ${P.length > 1 ? `<span class="pl-cmp">Compare <select data-pl="a" aria-label="The older version">${opt(va.plan_version)}</select>
      with <select data-pl="b" aria-label="The newer version">${opt(vb.plan_version)}</select></span>` : ""}</div>`;
  const why = (p) => `<div class="muted t-s">v${p.plan_version}: canvas version ${p.version}, ${esc(p.author || "")}${p.note ? " · why: " + esc(p.note) : ""}</div>`;
  if (va === vb) {
    return head + why(vb) + (vb.tasks.length ? vb.tasks.map(t => taskRow(t, "", esc)).join("") : `<div class="muted t-s">No task named yet.</div>`);
  }
  // Compare: the change from va to vb, from the steps between them.
  const steps = P.filter(p => p.plan_version > va.plan_version && p.plan_version <= vb.plan_version);
  const added = new Set(), removed = new Set(), changed = new Set();
  for (const s of steps) {
    s.added.forEach(i => { if (removed.has(i)) removed.delete(i); else added.add(i); });
    s.removed.forEach(i => { if (added.has(i)) added.delete(i); else removed.add(i); });
    (s.changed || []).forEach(i => { if (!added.has(i)) changed.add(i); });
  }
  const now = new Map(vb.tasks.map(t => [t.id, t]));
  const old = new Map(P.flatMap(p => p.tasks).map(t => [t.id, t]));
  const rows = [...[...added].map(i => [now.get(i), "added"]), ...[...removed].map(i => [old.get(i) || { id: i }, "removed"]),
    ...[...changed].map(i => [now.get(i), "changed"]), ...vb.tasks.filter(t => !added.has(t.id) && !changed.has(t.id)).map(t => [t, ""])];
  return head + steps.map(why).join("") + (rows.length ? rows.map(([t, m]) => taskRow(t, m, esc)).join("") : `<div class="muted t-s">No task named.</div>`);
}
