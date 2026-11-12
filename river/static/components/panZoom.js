// Pan and zoom for a drawing (an SVG) inside a fixed box: the wheel or a pinch zooms at the pointer,
// a drag or a two-finger scroll moves it, and the buttons zoom, fit it in the box, or show it at full size.
// A click that did not drag still reaches the drawing, so its boxes open items as before.
// el: the box. Returns { show(svgHtml, key) } — the same key keeps the view, a new key fits the drawing.

const MIN = 0.1, MAX = 4;

export function makePanZoom(el) {
  el.classList.add("pz");
  el.innerHTML = `<div class="pz-stage"></div>
    <div class="pz-tools" role="toolbar" aria-label="Zoom">
      <button class="btn" data-pz="in" title="Zoom in (or the wheel, or pinch)" aria-label="Zoom in">+</button>
      <button class="btn" data-pz="out" title="Zoom out" aria-label="Zoom out">−</button>
      <button class="btn" data-pz="fit" title="Fit the whole graph in the box">Fit</button>
      <button class="btn" data-pz="one" title="Full size">1:1</button>
    </div>`;
  const stage = el.querySelector(".pz-stage");
  let v = { x: 0, y: 0, k: 1 }, key = null, size = { w: 1, h: 1 };
  const apply = () => { stage.style.transform = `translate(${v.x}px, ${v.y}px) scale(${v.k})`; };
  // The scale at which the whole drawing shows. A drawing of many hundred boxes needs less than MIN,
  // so the lower limit of the zoom follows it.
  const PAD = 16;
  // The top of the drawing when it starts or fits: below the zoom buttons, so they cover no part of it (#5331).
  const tools = el.querySelector(".pz-tools");
  const top = () => Math.max(PAD, tools.offsetTop + tools.offsetHeight + PAD / 2);
  const fitK = () => {
    const W = el.clientWidth - 2 * PAD, H = el.clientHeight - top() - PAD;
    return W > 0 && H > 0 ? Math.min(1, W / size.w, H / size.h) : MIN;  // a hidden box has no size
  };
  const clampK = (k) => Math.min(MAX, Math.max(Math.min(MIN, fitK()), k));
  // Zoom by factor f, keeping the point (px, py) of the box where it is.
  function zoomAt(f, px, py) {
    const k = clampK(v.k * f);
    v = { x: px - (px - v.x) * k / v.k, y: py - (py - v.y) * k / v.k, k };
    apply();
  }
  function fit() {
    const W = el.clientWidth, H = el.clientHeight, k = fitK();
    v = { k, x: (W - size.w * k) / 2, y: top() + Math.max(0, (H - top() - PAD - size.h * k) / 2) };
    apply();
  }
  // A new drawing shows whole (Fit). In a narrow box (a phone, #4890) Fit can make the text too small to read: there
  // it starts at a readable size (READ), at the top, from the left when it is wider than the box; Fit shows it all.
  const READ = 0.8, NARROW = 600;
  function start() {
    const W = el.clientWidth;
    if (W >= NARROW || fitK() >= READ) return fit();
    const k = clampK(READ);
    v = { k, x: size.w * k <= W - 2 * PAD ? (W - size.w * k) / 2 : PAD, y: top() };
    apply();
    // The corner of a large drawing can be empty (the layout puts no box there): start at the group or box nearest
    // to the top left of what is drawn, so the first view shows a part of the drawing (#5331). The topmost one alone
    // can be far to the right of the first column. Mermaid names them .cluster and .node.
    const rs = [...stage.querySelectorAll(".cluster, .node")].map((n) => n.getBoundingClientRect()).filter((r) => r.width);
    if (!rs.length) return;
    const minL = Math.min(...rs.map((r) => r.left)), minT = Math.min(...rs.map((r) => r.top));
    const first = rs.reduce((a, r) => (r.left - minL) + (r.top - minT) < (a.left - minL) + (a.top - minT) ? r : a);
    const s = stage.getBoundingClientRect();
    if (size.w * k > W - 2 * PAD) v.x = PAD - (first.left - s.left);
    v.y = top() - (first.top - s.top);
    apply();
  }

  el.addEventListener("wheel", (e) => {
    e.preventDefault();
    const r = el.getBoundingClientRect();
    // A pinch on a trackpad comes as a wheel event with ctrlKey; a mouse wheel moves in steps of lines.
    if (e.ctrlKey || e.deltaMode === 1 || (Math.abs(e.deltaY) >= 50 && !e.deltaX)) zoomAt(Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.002)), e.clientX - r.left, e.clientY - r.top);
    else { v.x -= e.deltaX; v.y -= e.deltaY; apply(); }
  }, { passive: false });

  let drag = null, moved = false;
  el.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || e.target.closest(".pz-tools")) return;
    drag = { x: e.clientX, y: e.clientY, vx: v.x, vy: v.y, id: e.pointerId }; moved = false;
  });
  el.addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (!moved && Math.hypot(dx, dy) < 4) return;
    if (!moved) { moved = true; el.setPointerCapture(e.pointerId); el.classList.add("dragging"); }
    v.x = drag.vx + dx; v.y = drag.vy + dy; apply();
  });
  const end = () => { drag = null; el.classList.remove("dragging"); };
  el.addEventListener("pointerup", end);
  el.addEventListener("pointercancel", end);
  // After a drag, the click that follows must not open the box under the pointer.
  el.addEventListener("click", (e) => { if (moved) { e.stopPropagation(); e.preventDefault(); moved = false; } }, true);

  el.addEventListener("click", (e) => {
    const b = e.target.closest("[data-pz]"); if (!b) return;
    const W = el.clientWidth / 2, H = el.clientHeight / 2;
    if (b.dataset.pz === "in") zoomAt(1.25, W, H);
    if (b.dataset.pz === "out") zoomAt(0.8, W, H);
    if (b.dataset.pz === "fit") fit();
    if (b.dataset.pz === "one") zoomAt(1 / v.k, W, H);
  });
  el.tabIndex = 0;
  el.addEventListener("keydown", (e) => {
    const W = el.clientWidth / 2, H = el.clientHeight / 2, step = 60;
    if (e.key === "+" || e.key === "=") zoomAt(1.25, W, H);
    else if (e.key === "-") zoomAt(0.8, W, H);
    else if (e.key === "0") fit();
    else if (e.key.startsWith("Arrow")) { v.x += { ArrowLeft: step, ArrowRight: -step }[e.key] || 0; v.y += { ArrowUp: step, ArrowDown: -step }[e.key] || 0; apply(); }
    else return;
    e.preventDefault();
  });

  return {
    // Put the drawing in the box. The svg keeps its own size (not the box width), so zoom is the only scale.
    show(html, newKey) {
      stage.innerHTML = html;
      const svg = stage.querySelector("svg");
      if (svg) {
        const vb = svg.viewBox && svg.viewBox.baseVal;
        const w = vb && vb.width ? vb.width : svg.getBBox().width, h = vb && vb.height ? vb.height : svg.getBBox().height;
        svg.style.maxWidth = "none"; svg.setAttribute("width", w); svg.setAttribute("height", h);
        size = { w, h };
      }
      if (newKey !== key) { key = newKey; start(); }
      return stage;
    },
    fit,
  };
}
