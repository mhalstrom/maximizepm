// The item pop-up (#4611, #4622): on every item on the page (a #<id> link, a Board row, an item button), hover or
// keyboard focus shows the item's title and state, Open item (the drawer), and Open session: the session that holds
// it, else the last session that held it, running or ended (an ended session shows its chat read only).
// Server: GET /api/item-pop/<id>. The page's own click handler acts on data-open and data-session-go.
import { esc } from "../lib.js";
import { chip } from "./chip.js";

const cache = new Map();  // id -> {at, data}: a pop-up of the same item within 10 seconds asks again for nothing
let pop = null, popFor = null, popId = null, showTimer = null, hideTimer = null;
const idOf = (t) => t && (t.dataset.open || t.dataset.id);

async function info(id) {
  const c = cache.get(id);
  if (c && Date.now() - c.at < 10000) return c.data;
  const r = await fetch("api/item-pop/" + id);
  if (!r.ok) return null;
  const data = await r.json();
  cache.set(id, { at: Date.now(), data });
  return data;
}

function itemOf(el) {
  const t = el && el.closest && el.closest("[data-open], .row[data-id]");
  if (!t || (pop && pop.contains(t))) return null;
  const id = t.dataset.open || t.dataset.id;
  return /^\d+$/.test(id || "") ? t : null;
}

function render(d) {
  const s = d.session;
  const go = !s ? `<span class="muted">No session${d.goal ? ` · goal ${esc(d.goal)}` : " worked on it"}</span>`
    : s.running ? `<button class="btn" type="button" data-session-go="${esc(s.name)}" title="Open the session of ${esc(s.name)} in the Sessions tab">Open session</button>`
    : s.viewable ? `<button class="btn" type="button" data-session-go="${esc(s.name)}" title="The session of ${esc(s.name)} has ended: its chat shows read only">Open session (ended)</button>`
    : `<span class="muted">${esc(s.name)} has ended; its transcript is not on this computer</span>`;
  // Why this session (#4628): it holds the item, owns its goal, works on its goal, or finished it.
  const who = s ? `${esc(s.name)} ${esc(s.why || "")}${s.running ? "" : " (ended)"}` : "";
  pop.innerHTML = `<div class="refpop-t"><b>#${d.id}</b> ${esc(d.title)}</div>
    <div class="refpop-s">${chip("c-" + esc(d.status), esc(d.status.replace("_", " ")))} <span class="muted">${who}</span></div>
    <div class="refpop-b"><button class="btn" type="button" data-open="${d.id}">Open item</button>${go}</div>`;
}

// Beside its item. The Board draws its rows again every few seconds: the pop-up then finds the new element of the
// same item near where it was, and stays.
function place() {
  if (!popFor) return;
  if (!popFor.isConnected) {
    const old = popFor._rect;
    const near = [...document.querySelectorAll(`[data-open="${popId}"], .row[data-id="${popId}"]`)]
      .filter(x => !pop.contains(x)).find(x => { const r = x.getBoundingClientRect(); return !old || Math.abs(r.top - old.top) < 40; });
    if (!near) return;
    popFor = near;
  }
  const b = popFor.getBoundingClientRect();
  popFor._rect = b;
  if (b.bottom < 0 || b.top > window.innerHeight || (!b.width && !b.height)) return hide(true);
  const w = pop.offsetWidth, h = pop.offsetHeight;
  pop.style.left = Math.max(8, Math.min(b.left, window.innerWidth - w - 8)) + "px";
  pop.style.top = (b.bottom + h + 8 > window.innerHeight ? Math.max(8, b.top - h - 6) : b.bottom + 6) + "px";
}

function hide(now) {
  clearTimeout(hideTimer); clearTimeout(showTimer);
  const go = () => { pop.classList.add("hidden"); popFor = null; popId = null; };
  if (now) go(); else hideTimer = setTimeout(go, 250);
}

function showFor(t, delay) {
  clearTimeout(showTimer); clearTimeout(hideTimer);
  showTimer = setTimeout(async () => {
    const d = await info(idOf(t)).catch(() => null);
    if (!d) return;
    popFor = t; popId = String(d.id);
    render(d);
    pop.classList.remove("hidden");
    place();
  }, delay);
}

export function wireItemPop() {
  pop = document.createElement("div");
  pop.className = "refpop hidden"; pop.setAttribute("role", "tooltip"); pop.id = "itemPop";
  document.body.append(pop);
  document.addEventListener("mouseover", (e) => {
    if (pop.contains(e.target)) { clearTimeout(hideTimer); return; }
    const t = itemOf(e.target);
    if (t && idOf(t) === popId) { popFor = t; clearTimeout(hideTimer); return; }  // the same item, drawn again
    if (t) showFor(t, 400);
  });
  document.addEventListener("mouseout", (e) => {
    const t = itemOf(e.target);
    if (!t) { if (pop.contains(e.target) && !pop.contains(e.relatedTarget)) hide(); else clearTimeout(showTimer); return; }
    if (t.contains(e.relatedTarget) || pop.contains(e.relatedTarget)) return;
    clearTimeout(showTimer);
    if (idOf(t) === popId) hide();
  });
  document.addEventListener("focusin", (e) => {
    if (pop.contains(e.target)) { clearTimeout(hideTimer); return; }
    const t = itemOf(e.target);
    if (t && t === e.target) showFor(t, 0); else if (popFor) hide();
  });
  document.addEventListener("scroll", place, true);
  window.addEventListener("resize", place);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && popFor) hide(true); });
  pop.addEventListener("click", (e) => { if (e.target.closest("[data-open], [data-session-go]")) hide(true); });
}
