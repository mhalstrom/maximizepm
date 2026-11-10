// The prompt cache of an expert (#5023): a person talks with an expert for hours, and after a pause its next message
// reads the whole context again at the cache write price. The badge on its row and in its chat header says warm (with
// the minutes left) or cold, and what the next message costs; c is sessions.cache_state from the server. Before a
// message to a cold expert with a large context (c.fresh: expert_fresh_min tokens or more), askCold offers a fresh
// expert that reads only the old one's handoff (op expert_fresh), or Send anyway.
import { esc } from "../lib.js";
import { makeDialog } from "./dialog.js";

const k = (n) => n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1000 ? Math.round(n / 1000) + "k" : String(n);
export const cost = (c) => c.next_usd != null ? "$" + (c.next_usd < 0.01 ? c.next_usd.toFixed(3) : c.next_usd.toFixed(2))
  : k(c.next_eq) + " tokens";

export function cacheBadge(c) {
  if (!c) return "";
  const warm = c.state === "warm", min = Math.max(1, Math.round(c.left / 60));
  const tip = (warm ? `Its context is in the prompt cache for ${min} more minute${min === 1 ? "" : "s"} (${c.ttl} cache)`
    : `Its context left the prompt cache at ${new Date(c.until).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}: the next message reads all of it again at the write price`)
    + `. Context ${k(c.context)} tokens on ${c.model || "its model"}; the next message costs about ${cost(c)}`
    + (c.next_usd != null ? ` (${k(c.next_eq)} input-token equivalents)` : "") + ".";
  return `<span class="cache-b ${warm ? "warm" : "cold"}${c.fresh ? " big" : ""}" title="${esc(tip)}">${warm ? `cache ${min}m` : "cache cold"} · ${esc(cost(c))} next</span>`;
}

let dlg = null, answer = null;
function build() {
  const el = document.createElement("div");
  el.id = "coldDlg"; el.className = "hidden";
  el.setAttribute("role", "dialog"); el.setAttribute("aria-modal", "true"); el.setAttribute("aria-labelledby", "coldTitle");
  el.innerHTML = `<div class="box"><h2 id="coldTitle">This expert's context left the cache</h2><p class="cold-why"></p>
    <div class="cold-btns"><button class="btn" type="button" data-cold="cancel">Cancel</button>
    <button class="btn" type="button" data-cold="send">Send anyway</button>
    <button class="btn primary" type="button" data-cold="fresh">Start a fresh expert from the handoff</button></div></div>`;
  document.body.appendChild(el);
  dlg = makeDialog(el, { onClose: () => { const a = answer; answer = null; if (a) a(null); } });
  el.addEventListener("click", (e) => {
    const b = e.target.closest("[data-cold]");
    if (!b && e.target !== el) return;
    const a = answer; answer = null;
    dlg.close();
    if (a) a(b && b.dataset.cold !== "cancel" ? b.dataset.cold : null);
  });
}

// "fresh", "send", or null (cancel).
export function askCold(name, c) {
  if (!dlg) build();
  const el = document.getElementById("coldDlg");
  el.querySelector(".cold-why").textContent = `${name} holds ${k(c.context)} tokens of context, and its prompt cache `
    + `ended. Your message makes it read all of them again: about ${cost(c)}. A fresh expert on the same model and task `
    + `reads only ${name}'s handoff (its notes) and costs far less; ${name} ends, and its task goes to the new one.`;
  return new Promise((resolve) => { answer = resolve; dlg.open(); el.querySelector('[data-cold="fresh"]').focus(); });
}
