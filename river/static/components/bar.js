// Bars: goal and project progress, and the capacity columns.

export function pct(done, total) { return total ? Math.round(100 * done / total) : 0; }
export function bar(percent) { return `<div class="bar"><div style="width:${percent}%"></div></div>`; }
// The usage of an agent in every place says the part used, as the providers' own usage pages and the bars count it
// (#6518, mark 2026-11-20: '% left' in the list and '% used' in the dialog read as two numbers).
export function usedText(used) { return `${Math.round(Math.max(0, Math.min(100, used)))}% used`; }

// One column per step of the open work: its height is the step's item count, split agents / humans.
export function capacityBars(layers) {
  const maxL = Math.max(1, ...layers.map(l => l.ai + l.human));
  return layers.map(l => `<div class="layer" style="height:${Math.round((l.ai + l.human) / maxL * 100)}%" title="${l.ai} agent, ${l.human} human">
      <div class="a" style="flex:${l.ai}"></div><div class="h" style="flex:${l.human}"></div><div class="cap">${l.depth + 1}</div></div>`).join("");
}
