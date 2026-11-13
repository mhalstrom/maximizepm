// Annotate in the Browser pane (#5655): the person marks up the page of the session's browser and sends it to the
// session. browserTool.js calls makeAnnotate; while the mode is on, the page gets no input, the frame stops (a PNG
// screenshot of the tab, one pixel for each CSS pixel, is the background), and an SVG layer over it takes the
// drawing. Tools: box (drag; serve names the smallest element that holds it and the elements inside it), pen, arrow,
// text note (a click; serve names the element there), element (a click selects the element there: its selector,
// text and box), eraser, undo, a color. Box, text and element are numbered notes, each with a line of text. Send
// stores the set (river/annotate.py) and puts one message in the session's chat: the picture with the drawings, the
// page's address, the numbered notes with their elements; Copy puts the same text on the clipboard. Esc or Done
// ends the mode; the drawings stay on the message, not on the page. Touch: pen and box work with a finger.
// The list under the tools shows the sets sent before for this session: each note's state (fixed, not fixed, open),
// the session's line, and Reopen; the notes of a set of the page that shows have their marks on the page too.
import { esc, toast } from "../lib.js";
import { button } from "./button.js";

const NS = "http://www.w3.org/2000/svg";
const TOOLS = [["box", "▭", "Box"], ["pen", "✎", "Pen"], ["arrow", "↗", "Arrow"], ["text", "T", "Text note"],
  ["element", "⌖", "Select element"], ["erase", "⌫", "Eraser"]];
const COLORS = ["--ann-1", "--ann-2", "--ann-3", "--ann-4"];
const NOTED = new Set(["box", "text", "element"]);
const DRAG_MIN = 6;  // CSS pixels: a press that moves less is a click (annotatetofix's DRAG_MIN)
const STATE = { open: ["○", "open"], fixed: ["✓", "fixed"], not_fixed: ["✗", "not fixed"] };

const color = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "red";

// ctx: { box, stage, img, view (the pane's v: w, h), project, session, call(action, args), typeAction, title(), url(),
// resume() (show the live frames again) }
export function makeAnnotate(ctx) {
  const { box, stage, img } = ctx;
  const bar = document.createElement("div");
  bar.className = "br-ann hidden";
  bar.innerHTML = `<div class="br-anntools" role="toolbar" aria-label="Annotate">
      ${TOOLS.map(([k, s, l]) => button(s, { kind: "icon", label: l, title: l, cls: "br-tool", data: { ann: "tool", tool: k } })).join("")}
      ${COLORS.map((c, i) => button("", { kind: "icon", label: "Color " + (i + 1), title: "Color", cls: "br-color br-c" + (i + 1), data: { ann: "color", color: c } })).join("")}
      ${button("↶", { kind: "icon", label: "Undo", title: "Undo", data: { ann: "undo" } })}
      <span class="br-sp"></span>
      ${button("Send", { kind: "primary", cls: "br-send", title: "Send to the session", data: { ann: "send" } })}
      ${button("Copy", { title: "Copy the text", data: { ann: "copy" } })}
      ${button("Done", { title: "Leave Annotate", data: { ann: "done" } })}
    </div>
    <form class="br-annnote hidden" autocomplete="off"><span class="br-nn t-s"></span>
      <input class="br-nt" aria-label="Note" placeholder="What is wrong here?" enterkeyhint="done">
      ${button("OK", { title: "Keep the note", attrs: 'type="submit"' })}</form>
    <div class="br-sets t-s"></div>`;
  box.querySelector(".br-ctl").after(bar);
  const layer = document.createElementNS(NS, "svg");
  layer.setAttribute("class", "br-draw hidden");
  stage.appendChild(layer);
  const $ = (s) => bar.querySelector(s);
  const noteForm = $(".br-annnote"), noteInput = $(".br-nt");

  const a = { on: false, tool: "box", color: COLORS[0], shapes: [], drawing: null, bg: null, w: 0, h: 0,
    editing: -1, saved: null, sets: [] };

  // The layer lies over the picture of the page, in its CSS pixels (viewBox), so a stage of any size draws it right.
  function place() {
    const s = stage.getBoundingClientRect(), r = img.getBoundingClientRect();
    Object.assign(layer.style, { left: r.left - s.left + "px", top: r.top - s.top + "px", width: r.width + "px", height: r.height + "px" });
  }
  const ro = new ResizeObserver(() => a.on && place());
  ro.observe(stage);

  const pt = (e) => { const r = layer.getBoundingClientRect();
    return { x: Math.max(0, Math.min(a.w, (e.clientX - r.left) * a.w / r.width)), y: Math.max(0, Math.min(a.h, (e.clientY - r.top) * a.h / r.height)) }; };
  const numbered = () => a.shapes.filter(s => NOTED.has(s.kind));
  const numberOf = (s) => numbered().indexOf(s) + 1;

  // One SVG text of all the shapes, for the layer and for the picture (literal colors: an SVG drawn as a picture
  // reads no CSS of the page). scale: the picture's pixels for each CSS pixel.
  function svg(scale = 1, forPicture = false) {
    const sw = 3, out = [], edge = color("--ptr-edge");
    a.shapes.forEach((s, i) => {
      const c = color(s.color), d = `data-i="${i}"`;
      if (s.kind === "pen" && s.pts.length) out.push(`<polyline ${d} points="${s.pts.map(p => p.x + "," + p.y).join(" ")}" fill="none" stroke="${c}" stroke-width="${sw}" stroke-linecap="round" stroke-linejoin="round"/>`);
      if (s.kind === "arrow") {
        const { x1, y1, x2, y2 } = s, ang = Math.atan2(y2 - y1, x2 - x1), L = 14;
        const h1 = [x2 - L * Math.cos(ang - 0.45), y2 - L * Math.sin(ang - 0.45)], h2 = [x2 - L * Math.cos(ang + 0.45), y2 - L * Math.sin(ang + 0.45)];
        out.push(`<g ${d} stroke="${c}" stroke-width="${sw}" stroke-linecap="round" fill="none"><line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"/><polyline points="${h1} ${x2},${y2} ${h2}"/></g>`);
      }
      if (s.kind === "box" || s.kind === "element") {
        const b = s.box;
        if (b) out.push(`<rect ${d} x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" fill="${c}" fill-opacity="${s.kind === "element" ? 0.12 : 0.04}" stroke="${c}" stroke-width="${sw}"${s.kind === "element" ? ' stroke-dasharray="6 4"' : ""}/>`);
      }
      if (NOTED.has(s.kind)) {
        const n = numberOf(s), p = s.kind === "text" ? s.at : { x: s.box ? s.box.x : s.at.x, y: s.box ? s.box.y : s.at.y };
        const x = Math.max(12, Math.min(a.w - 12, p.x)), y = Math.max(12, Math.min(a.h - 12, p.y));
        out.push(`<g ${d}><circle cx="${x}" cy="${y}" r="11" fill="${c}" stroke="${edge}" stroke-width="2"/><text x="${x}" y="${y + 4.5}" text-anchor="middle" font-family="system-ui, sans-serif" font-size="13" font-weight="700" fill="${edge}">${n}</text>`
          + (s.kind === "text" && s.text ? `<text x="${x + 16}" y="${y + 5}" font-family="system-ui, sans-serif" font-size="14" font-weight="600" fill="${c}" stroke="${edge}" stroke-width="3" paint-order="stroke">${esc(s.text.slice(0, 80))}</text>` : "")
          + "</g>");
      }
    });
    if (!forPicture) for (const m of a.marks || []) out.push(m);
    return `<svg xmlns="${NS}" viewBox="0 0 ${a.w} ${a.h}" width="${Math.round(a.w * scale)}" height="${Math.round(a.h * scale)}">${out.join("")}</svg>`;
  }
  const draw = () => { layer.setAttribute("viewBox", `0 0 ${a.w} ${a.h}`); layer.innerHTML = svg().replace(/^<svg[^>]*>|<\/svg>$/g, ""); };

  // The note of a numbered shape: the field under the tools.
  function editNote(i) {
    a.editing = i;
    const s = a.shapes[i];
    if (!s) { noteForm.classList.add("hidden"); return; }
    $(".br-nn").textContent = numberOf(s) + ".";
    noteInput.value = s.text || "";
    noteForm.classList.remove("hidden");
    noteInput.focus({ preventScroll: true });
  }
  const keepNote = () => { const s = a.shapes[a.editing]; if (s) { s.text = noteInput.value.trim(); a.saved = null; draw(); } };
  noteForm.addEventListener("submit", (e) => { e.preventDefault(); keepNote(); a.editing = -1; noteForm.classList.add("hidden"); });

  function add(s) { keepNote(); a.shapes.push(s); a.saved = null; draw(); if (NOTED.has(s.kind)) editNote(a.shapes.length - 1); }

  // What serve says about a point or a box: the element, its box in the view, the page's scroll.
  async function ask(action, args, s) {
    try {
      const r = await ctx.call(action, args);
      s.ref = r.ref; s.inside = r.inside || [];
      const sx = (r.scroll || [0, 0])[0], sy = (r.scroll || [0, 0])[1];
      if (s.kind === "element" && r.box) s.box = r.box;
      const b = s.box || { ...s.at, w: 0, h: 0 };
      s.page = { x: b.x + sx, y: b.y + sy, w: b.w, h: b.h };
      draw();
    } catch (e) { /* call showed it; the note keeps its place without an element */ }
  }

  layer.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    const p = pt(e);
    if (a.tool === "erase") {
      const i = +((e.target.closest("[data-i]") || {}).dataset || {}).i;
      if (!Number.isNaN(i) && a.shapes[i]) { a.shapes.splice(i, 1); a.saved = null; a.editing = -1; noteForm.classList.add("hidden"); draw(); }
      return;
    }
    if (a.tool === "text" || a.tool === "element") {
      const s = { kind: a.tool, color: a.color, at: { x: p.x, y: p.y, w: 0, h: 0 }, text: "" };
      add(s);
      ask("pick", { x: p.x, y: p.y }, s);
      return;
    }
    try { layer.setPointerCapture(e.pointerId); } catch (err) { /* a pointer that is no longer down */ }
    a.drawing = { kind: a.tool, color: a.color, start: p, pts: [p], x1: p.x, y1: p.y, x2: p.x, y2: p.y };
  });
  layer.addEventListener("pointermove", (e) => {
    const d = a.drawing; if (!d) return;
    const p = pt(e);
    if (d.kind === "pen") d.pts.push(p);
    d.x2 = p.x; d.y2 = p.y;
    if (d.kind === "box") d.box = { x: Math.min(d.x1, p.x), y: Math.min(d.y1, p.y), w: Math.abs(p.x - d.x1), h: Math.abs(p.y - d.y1) };
    const i = a.shapes.indexOf(d);
    if (i < 0) a.shapes.push(d);
    draw();
  });
  const up = () => {
    const d = a.drawing; if (!d) return;
    a.drawing = null;
    const i = a.shapes.indexOf(d);
    if (i >= 0) a.shapes.splice(i, 1);
    const moved = Math.abs(d.x2 - d.x1) >= DRAG_MIN || Math.abs(d.y2 - d.y1) >= DRAG_MIN;
    if (!moved && d.kind !== "pen") return draw();
    const s = { kind: d.kind, color: d.color };
    if (d.kind === "pen") s.pts = d.pts.map(p => ({ x: Math.round(p.x * 10) / 10, y: Math.round(p.y * 10) / 10 }));
    if (d.kind === "arrow") Object.assign(s, { x1: d.x1, y1: d.y1, x2: d.x2, y2: d.y2 });
    if (d.kind === "box") { s.box = d.box; s.at = { x: d.box.x, y: d.box.y, w: 0, h: 0 }; s.text = ""; }
    add(s);
    if (d.kind === "box") ask("area", { box: s.box }, s);
  };
  layer.addEventListener("pointerup", up);
  layer.addEventListener("pointercancel", up);

  function setTool(t) {
    a.tool = t;
    bar.querySelectorAll(".br-tool").forEach(b => { b.classList.toggle("on", b.dataset.tool === t); b.setAttribute("aria-pressed", b.dataset.tool === t ? "true" : "false"); });
    layer.dataset.tool = t;
  }
  function setColor(c) {
    a.color = c;
    bar.querySelectorAll(".br-color").forEach(b => { b.classList.toggle("on", b.dataset.color === c); b.setAttribute("aria-pressed", b.dataset.color === c ? "true" : "false"); });
  }

  // The picture: the background PNG with the shapes drawn over it, at the PNG's size (the page's view).
  async function picture() {
    const bg = new Image();
    bg.src = a.bg || img.src;
    await bg.decode();
    const W = bg.naturalWidth, H = bg.naturalHeight, cv = document.createElement("canvas");
    cv.width = W; cv.height = H;
    const g = cv.getContext("2d");
    g.drawImage(bg, 0, 0);
    const over = new Image();
    over.src = URL.createObjectURL(new Blob([svg(W / a.w, true)], { type: "image/svg+xml" }));
    await over.decode();
    g.drawImage(over, 0, 0, W, H);
    URL.revokeObjectURL(over.src);
    return cv.toDataURL("image/png").split(",")[1];
  }

  async function sendOrCopy(send) {
    keepNote();
    if (!a.shapes.length) return toast("Draw a box, a line or a note first", true);
    const notes = numbered().map(s => ({ kind: s.kind, text: s.text || "", at: s.at, page: s.page, box: s.box, ref: s.ref, inside: s.inside }));
    const drawings = { pen: a.shapes.filter(s => s.kind === "pen").length, arrow: a.shapes.filter(s => s.kind === "arrow").length };
    try {
      const r = await ctx.typeAction("browser_annotate", { project: ctx.project, session: ctx.session, url: ctx.url(),
        title: ctx.title(), w: Math.round(a.w), h: Math.round(a.h), png: await picture(), notes, drawings, send, id: a.saved });
      a.saved = r.id;
      if (send) { toast(`Sent to ${r.told}: a${r.id}, ${notes.length} note${notes.length === 1 ? "" : "s"}`); stop(); }
      else {
        try { await navigator.clipboard.writeText(r.prompt); toast(`Copied a${r.id}: paste it to an agent`); }
        catch (e) { toast("The browser did not allow the clipboard", true); }
      }
    } catch (e) { toast(e.message, true); }
  }

  // The sets sent before (this session's): states, the session's line, Reopen; the marks of this page's sets.
  async function loadSets() {
    try { a.sets = (await ctx.call("annotations", {})).sets || []; } catch (e) { a.sets = []; }
    showSets();
  }
  function showSets() {
    const el = $(".br-sets");
    a.marks = [];
    for (const s of a.sets) for (const x of s.notes) {
      const h = x.here; if (!h) continue;
      const [sym] = STATE[x.state], c = color({ fixed: "--done", not_fixed: "--warn", open: "--accent" }[x.state]);
      a.marks.push(`<g class="br-old"><rect x="${h.x}" y="${h.y}" width="${Math.max(h.w, 0)}" height="${Math.max(h.h, 0)}" fill="none" stroke="${c}" stroke-width="1.5" stroke-dasharray="3 3"/><text x="${h.x + 4}" y="${h.y + 14}" font-family="system-ui, sans-serif" font-size="12" font-weight="700" fill="${c}" stroke="${color("--ptr-edge")}" stroke-width="3" paint-order="stroke">a${s.id}.${x.n} ${sym}</text></g>`);
    }
    draw();
    el.innerHTML = a.sets.length ? a.sets.slice(0, 8).map(s => `<details class="br-set"${s.open ? " open" : ""}><summary>a${s.id} · ${esc(s.title || s.url)} · ${s.notes.length - s.open} of ${s.notes.length} fixed</summary>
      <ol class="br-setn">${s.notes.map(x => { const [sym, word] = STATE[x.state]; return `<li class="br-st-${x.state}"><span class="br-sym" aria-label="${word}">${sym}</span> <span>${esc(x.text || "(no text)")}</span>${
        x.fix ? `<div class="muted">${esc(x.fix.by)}: ${esc(x.fix.note)}</div>` : ""}${
        x.state !== "open" ? button("Reopen", { title: "Open it again", data: { ann: "reopen", id: s.id, n: x.n } }) : ""}</li>`; }).join("")}</ol></details>`).join("")
      : "";
  }

  async function reopen(id, n) {
    try { a.sets = (await ctx.call("annotation_mark", { id, n })).sets || []; showSets(); toast(`a${id} note ${n} is open again`); }
    catch (e) { /* call showed it */ }
  }

  async function start() {
    if (a.on) return;
    a.on = true; a.shapes = []; a.saved = null; a.editing = -1; a.bg = null;
    a.w = ctx.view.w || img.naturalWidth; a.h = ctx.view.h || img.naturalHeight;
    stage.classList.add("br-annon"); bar.classList.remove("hidden"); layer.classList.remove("hidden");
    box.querySelector(".br-annbtn").setAttribute("aria-pressed", "true");
    $(".br-send").classList.toggle("hidden", !ctx.session);
    place(); draw();
    setTool(a.tool); setColor(a.color);
    // The background: a PNG of the tab now, one pixel for each CSS pixel; the frozen frame shows until it comes.
    ctx.call("screenshot", {}).then(r => { if (a.on && r.png) { a.bg = "data:image/png;base64," + r.png; img.src = a.bg; } }).catch(() => {});
    loadSets();
  }
  function stop() {
    if (!a.on) return;
    a.on = false; a.shapes = []; a.drawing = null; a.marks = [];
    stage.classList.remove("br-annon"); bar.classList.add("hidden"); layer.classList.add("hidden"); noteForm.classList.add("hidden");
    box.querySelector(".br-annbtn").setAttribute("aria-pressed", "false");
    layer.innerHTML = "";
    ctx.resume();
  }

  bar.addEventListener("click", (e) => {
    const b = e.target.closest("[data-ann]"); if (!b) return;
    e.preventDefault();
    const w = b.dataset.ann;
    if (w === "tool") setTool(b.dataset.tool);
    else if (w === "color") setColor(b.dataset.color);
    else if (w === "undo") { a.shapes.pop(); a.saved = null; a.editing = -1; noteForm.classList.add("hidden"); draw(); }
    else if (w === "send" || w === "copy") sendOrCopy(w === "send");
    else if (w === "done") stop();
    else if (w === "reopen") reopen(+b.dataset.id, +b.dataset.n);
  });
  box.addEventListener("keydown", (e) => {
    if (!a.on) return;
    if (e.key === "Escape") {
      e.preventDefault(); e.stopPropagation();
      if (document.activeElement === noteInput) { a.editing = -1; noteForm.classList.add("hidden"); noteInput.blur(); }
      else stop();
    } else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "z" && document.activeElement !== noteInput) {
      e.preventDefault(); a.shapes.pop(); a.saved = null; draw();
    }
  });

  return { start, stop, get on() { return a.on; }, toggle: () => a.on ? stop() : start() };
}
