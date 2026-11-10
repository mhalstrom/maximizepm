// The Capacity tab (#4656): open session slots, sessions with nothing ready, the open items by step.
import { $, esc } from "../lib.js";
import { capacityBars } from "../components/bar.js";
import { S } from "../page.js";

export function renderCapacity() {
  const c = S.capacity;
  const layers = capacityBars(c.layers);
  $("#capacity").innerHTML = `<div class="stats">
      <div class="stat ${c.spare_slots ? "good" : ""}"${c.spare_slots ? ` data-launch="1" style="cursor:pointer"` : ""} title="More agent sessions you can start now${c.spare_slots ? "; click to start one in a new terminal window" : ""}"><div class="n">${c.spare_slots}</div><div class="l">open slots</div></div>
      <div class="stat ${c.excess_sessions ? "bad" : ""}" title="Agent sessions with nothing ready for them"><div class="n">${c.excess_sessions}</div><div class="l">idle</div></div>
      <div class="stat" title="Active agent sessions; ${c.agents_busy} of them busy"><div class="n">${c.agents_active}</div><div class="l">sessions</div></div>
      <div class="stat"><div class="n">${c.in_progress.length}</div><div class="l">in progress</div></div>
      <div class="stat hum" title="Ready items that wait on a person"><div class="n">${c.ready_for_humans.length}</div><div class="l">for people</div></div>
    </div>
    <ul class="advice">${c.advice.map(a => `<li class="${a.kind}">${esc(a.text)}</li>`).join("")}</ul>
    <div class="layers">${layers || '<span class="muted">No open items.</span>'}</div>
    <div class="legend" title="Open items by step: 1 can start now, 2 after step 1, … Peak agent width ${c.peak_width}."><span><span class="sw" style="background:var(--accent)"></span>agents</span><span><span class="sw" style="background:var(--human)"></span>people</span></div>`;
}
