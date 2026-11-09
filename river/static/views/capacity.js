// The Capacity tab (#4656): open session slots, sessions with nothing ready, the open items by step.
import { $, esc } from "../lib.js";
import { capacityBars } from "../components/bar.js";
import { S } from "../page.js";

export function renderCapacity() {
  const c = S.capacity;
  const layers = capacityBars(c.layers);
  $("#capacity").innerHTML = `<h2>Parallel work</h2>
    <div class="stats">
      <div class="stat ${c.spare_slots ? "good" : ""}"${c.spare_slots ? ` data-launch="1" style="cursor:pointer" title="Click to start an agent session in a new terminal window"` : ""}><div class="n">${c.spare_slots}</div><div class="l">open slots: more agent sessions you can start now${c.spare_slots ? " (click to start one)" : ""}</div></div>
      <div class="stat ${c.excess_sessions ? "bad" : ""}"><div class="n">${c.excess_sessions}</div><div class="l">sessions with nothing ready for them</div></div>
      <div class="stat"><div class="n">${c.agents_active}</div><div class="l">active agent sessions (${c.agents_busy} busy)</div></div>
      <div class="stat"><div class="n">${c.in_progress.length}</div><div class="l">items in progress</div></div>
      <div class="stat hum"><div class="n">${c.ready_for_humans.length}</div><div class="l">ready items that wait on a human</div></div>
    </div>
    <ul class="advice">${c.advice.map(a => `<li class="${a.kind}">${esc(a.text)}</li>`).join("")}</ul>
    <div class="layers">${layers || '<span class="muted">No open items.</span>'}</div>
    <div class="legend"><span><span class="sw" style="background:var(--accent)"></span>open items for agents</span><span><span class="sw" style="background:var(--human)"></span>for humans</span><span>by step: 1 = can start now, 2 = after step 1, …  Peak agent width ${c.peak_width}.</span></div>`;
}
