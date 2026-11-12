// The browser of a project in the side panel (#4631, design #4641): a tool tab of the panel (openTool). maxpm serve
// runs a headless Google Chrome for each project (river/browser.py); this tab shows its page as frames (a long poll
// of GET api/browser/frame), and sends the person's clicks, wheel, keys and pasted text (op browser_input, with the
// page's token). Back, Forward, Reload, an address field, the tabs of the browser, and who controls it: nobody, the
// person (Take control, Give back), or a session (maxpm browser control) and the subagent it names. While a session
// controls it, the person's input waits until Take control. "Open as a window" shows the same browser as a normal
// window, for a site that refuses a headless browser (a sign-in); "Back to the panel" ends that.
// #5616: over the page it draws the pointer of the session that drives (it glides to each point, a ring marks each
// press, a tag names the session) and the keys it types (dots in a password field); serve records them. "Hand to
// <session>" gives the browser to the session of this panel and puts the page's address in its chat.
import { esc, toast, actor, pageToken, typeAction } from "../lib.js";
import { button } from "./button.js";
import { openTool, panelSession } from "./sidePanel.js";

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function call(action, args = {}) {
  const r = await fetch("api/action", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ op: "browser", args: { action, ...args }, actor: actor() }) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { toast(j.error || "failed", true); throw new Error(j.error || "failed"); }
  return j.result;
}

// The browser of the session's project: the tab "Browser · <project>" in the panel.
export async function openBrowser(session) {
  const info = await call("status", session ? { agent: session } : {});
  openTool("browser:" + info.project, "Browser · " + info.project, (box) => render(box, info.project));
}

// maxpm browser open (#4631): a session asks the page to show its browser. The request comes in the state; the tab
// opens when the panel shows that session.
let seen = null;
export function browserOpens(list) {
  list = list || [];
  const top = list.reduce((m, x) => Math.max(m, x.seq), 0);
  if (seen === null || top < seen) { seen = top; return; }
  for (const x of list.filter(x => x.seq > seen))
    if (x.session && x.session === panelSession())
      openTool("browser:" + x.project, "Browser · " + x.project, (box) => render(box, x.project));
  seen = top;
}

const MODS = new Set(["Shift", "Control", "Alt", "Meta", "CapsLock", "Fn"]);
const KEYS = new Set(["Enter", "Backspace", "Tab", "Escape", "Delete", "ArrowLeft", "ArrowUp", "ArrowRight", "ArrowDown",
  "Home", "End", "PageUp", "PageDown"]);
const mods = (e) => ({ alt: e.altKey, ctrl: e.ctrlKey, meta: e.metaKey, shift: e.shiftKey });

function render(box, project) {
  box.innerHTML = `<div class="br">
    <form class="br-bar" autocomplete="off">
      ${button("◀", { kind: "icon", label: "Back", title: "Back", data: { br: "back" } })}
      ${button("▶", { kind: "icon", label: "Forward", title: "Forward", data: { br: "forward" } })}
      ${button("↻", { kind: "icon", label: "Reload", title: "Load the page again", data: { br: "reload" } })}
      <input class="br-addr" placeholder="Address" aria-label="The address of the browser's page">
      <select class="br-tabs hidden" aria-label="The tabs of the browser"></select>
    </form>
    <div class="br-ctl"><span class="br-who muted t-s"></span><span class="br-sp"></span>
      ${button("Take control", { cls: "br-take", title: "Take control of the browser", data: { br: "control" } })}
      ${button("Give back", { cls: "br-back hidden", title: "Hand the browser on", data: { br: "give_back" } })}
      ${button("Open as a window", { cls: "br-win", title: "Show it as a normal window", data: { br: "window" } })}
      ${button("Close", { kind: "warn", title: "Close it; the sign-ins stay", data: { br: "close" } })}
    </div>
    <div class="br-stage" tabindex="-1">
      <img class="br-img" alt="The page of the browser" draggable="false">
      <div class="br-over" aria-hidden="true">
        <div class="br-ring"></div>
        <div class="br-ptr hidden"><svg class="br-arrow" viewBox="0 0 20 28"><path d="M1 1 L1 22 L6.5 17 L10 26 L13.5 24.5 L10 16 L17.5 16 Z"/></svg><span class="br-tag"></span></div>
        <div class="br-typed hidden"><span class="br-tw"></span> <span class="br-tt"><bdi></bdi></span></div>
      </div>
      <textarea class="br-keys" aria-label="Keys for the browser" autocapitalize="off" autocomplete="off" spellcheck="false"></textarea>
      <div class="br-msg muted t-m">Starting the browser of ${esc(project)}…</div>
    </div>
  </div>`;
  const $$ = (s) => box.querySelector(s);
  const stage = $$(".br-stage"), img = $$(".br-img"), keys = $$(".br-keys"), msg = $$(".br-msg"), addr = $$(".br-addr");
  const v = { seq: 0, w: 0, h: 0, mine: true, running: false, window: false };
  const ptr = $$(".br-ptr"), ring = $$(".br-ring"), typed = $$(".br-typed");
  // One request at a time; what the person types or clicks while it runs goes in the next one, in order (#5479: one
  // request for each key took about 0.7 s a key when serve was busy).
  let pending = [], busy = false, token = null;
  const send = (ev) => { if (!v.mine) return toast("A session controls the browser: press Take control first", true);
    pending.push(ev); if (!busy) flush(); };
  async function flush() {
    busy = true;
    while (pending.length) {
      const evs = pending; pending = [];
      try { await typeAction("browser_input", { project, evs }); } catch (e) { toast(e.message, true); }
    }
    busy = false;
  }

  // Who controls it: the line, the buttons, and whether the person's input goes through.
  function show(j) {
    v.running = !!j.running; v.window = !!j.window;
    const c = j.controller, me = actor();
    v.mine = !c || c.kind === "person";
    $$(".br-who").textContent = !j.running ? "The browser is not running."
      : !c ? "Nobody controls it: you can use it. A session takes it with maxpm browser control."
      : c.kind === "person" ? (c.who === me || !me ? "You control it." : `${c.who} controls it.`)
      : `${c.who}${c.sub ? " (subagent " + c.sub + ")" : ""} controls it. Take control to use it yourself.`;
    $$(".br-take").classList.toggle("hidden", !c || c.kind === "person");
    const back = $$(".br-back"), to = handTo(j);
    back.classList.toggle("hidden", !(c && c.kind === "person"));
    back.textContent = to ? "Hand to " + to : "Give back";
    back.dataset.br = to ? "hand" : "give_back";
    back.dataset.to = to || "";
    const win = $$(".br-win");
    win.textContent = j.window ? "Back to the panel" : "Open as a window";
    win.dataset.br = j.window ? "headless" : "window";
    win.classList.toggle("hidden", !!j.source);  // a Chrome that runs already (browser_source) has its own window
    stage.classList.toggle("br-locked", !v.mine);
    if (document.activeElement !== addr) addr.value = j.url && j.url !== "about:blank" ? j.url : "";
    const sel = $$(".br-tabs"), tabs = j.tabs || [];
    sel.classList.toggle("hidden", tabs.length < 2);
    sel.innerHTML = tabs.map(t => `<option value="${esc(t.id)}"${t.on ? " selected" : ""}>${esc((t.title || t.url || "").slice(0, 60))}</option>`).join("");
    msg.classList.toggle("hidden", j.running && !!v.w);
    if (!j.running) msg.innerHTML = `The browser of ${esc(project)} is not running. ${button("Start", { kind: "primary", data: { br: "start" } })}`;
    if (j.frame) {
      img.src = "data:image/jpeg;base64," + j.frame.data;
      v.w = j.frame.w || img.naturalWidth; v.h = j.frame.h || img.naturalHeight;
      msg.classList.add("hidden");
    }
    draw(j);
  }

  // The session of this panel, else the one that held the browser before the person took it.
  const handTo = (j) => panelSession() || j.before || "";

  // The driving session's pointer and keys over the frame (#5616), in the stage's pixels.
  let last = null, presses = null, hideTyped = null;
  const spot = (x, y) => { const s = stage.getBoundingClientRect(), r = img.getBoundingClientRect();
    return v.w && r.width ? [r.left - s.left + x * r.width / v.w, r.top - s.top + y * r.height / v.h] : null; };
  function draw(j) {
    const p = j.pointer, c = j.controller, xy = p && spot(p.x, p.y);
    if (!xy || (c && c.kind === "person")) { ptr.classList.add("hidden"); last = null; }
    else {
      // A long jump glides longer; the steps of a click of maxpm browser come close together and follow at once.
      const d = last ? Math.hypot(xy[0] - last[0], xy[1] - last[1]) : 0;
      ptr.style.transitionDuration = last ? Math.round(Math.min(450, Math.max(60, d * 1.2))) + "ms" : "0ms";
      ptr.style.transform = `translate(${xy[0]}px, ${xy[1]}px)`;
      ptr.querySelector(".br-tag").textContent = p.who || "";
      ptr.classList.remove("hidden"); last = xy;
      if (presses !== null && p.press > presses && p.px != null) {
        const r = spot(p.px, p.py);
        ring.style.translate = `${r[0]}px ${r[1]}px`;  // not transform: the pulse's scale would scale the move too
        ring.classList.remove("br-pulse"); void ring.offsetWidth; ring.classList.add("br-pulse");
      }
      presses = p.press;
    }
    const t = j.typed;
    clearTimeout(hideTyped);
    if (t && t.age < 4 && t.text) {
      typed.querySelector(".br-tw").textContent = t.who + (t.secret ? " types a secret:" : " types:");
      typed.querySelector(".br-tt bdi").textContent = t.text;
      typed.classList.remove("hidden");
      hideTyped = setTimeout(() => typed.classList.add("hidden"), (4 - t.age) * 1000);
    } else typed.classList.add("hidden");
  }

  // The page takes the size of the stage (headless only).
  let sized = "";
  const fit = () => {
    const r = stage.getBoundingClientRect(), key = Math.round(r.width) + "x" + Math.round(r.height);
    if (r.width < 50 || key === sized || !v.running) return;
    sized = key; call("resize", { project, w: Math.round(r.width), h: Math.round(r.height) }).catch(() => {});
  };
  const ro = new ResizeObserver(() => fit()); ro.observe(stage);

  async function loop() {
    try { show(await call("start", { project })); } catch (e) { show({ running: false }); }
    fit();
    while (box.isConnected) {
      if (document.hidden) { await sleep(1000); continue; }
      try {
        token = token || await pageToken();
        const r = await fetch(`api/browser/frame?project=${encodeURIComponent(project)}&after=${v.seq}&token=${encodeURIComponent(token)}`);
        if (r.status === 403) { token = await pageToken(true); continue; }
        const j = await r.json();
        if (!box.isConnected) break;
        if (j.error) { await sleep(2000); continue; }
        const was = v.running;
        v.seq = j.seq || 0; show(j);
        if (!was && v.running) { sized = ""; fit(); }
        if (!j.running) await sleep(2000);
      } catch (e) { await sleep(2000); }
    }
    ro.disconnect();
  }

  // The point in the page's CSS pixels.
  const at = (e) => { const r = img.getBoundingClientRect();
    return { x: (e.clientX - r.left) * (v.w || r.width) / r.width, y: (e.clientY - r.top) * (v.h || r.height) / r.height }; };
  let down = false, lastMove = 0, wheel = null;
  img.addEventListener("pointerdown", (e) => {
    e.preventDefault(); keys.focus({ preventScroll: true });
    if (!v.mine) return toast("A session controls the browser: press Take control first", true);
    down = true; img.setPointerCapture(e.pointerId);
    send({ type: "mouse", action: "down", ...at(e), button: ["left", "middle", "right"][e.button] || "left", clicks: e.detail || 1, mods: mods(e) });
  });
  img.addEventListener("pointerup", (e) => {
    if (!down) return; down = false;
    send({ type: "mouse", action: "up", ...at(e), button: ["left", "middle", "right"][e.button] || "left", clicks: e.detail || 1, mods: mods(e) });
  });
  img.addEventListener("pointermove", (e) => {
    if (!down || !v.mine || Date.now() - lastMove < 50) return;
    lastMove = Date.now(); send({ type: "mouse", action: "move", ...at(e), mods: mods(e) });
  });
  img.addEventListener("contextmenu", (e) => e.preventDefault());
  stage.addEventListener("wheel", (e) => {
    e.preventDefault(); if (!v.mine) return;
    if (wheel) { wheel.dx += e.deltaX; wheel.dy += e.deltaY; return; }
    wheel = { type: "mouse", action: "wheel", ...at(e), dx: e.deltaX, dy: e.deltaY };
    setTimeout(() => { const w = wheel; wheel = null; send(w); }, 60);
  }, { passive: false });
  keys.addEventListener("keydown", (e) => {
    if (MODS.has(e.key)) return;
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "v") return;  // the paste event sends the text
    e.preventDefault();
    if (KEYS.has(e.key) || e.key.length === 1) send({ type: "key", key: e.key, mods: mods(e) });
  });
  keys.addEventListener("paste", (e) => { e.preventDefault(); const t = e.clipboardData.getData("text"); if (t) send({ type: "text", text: t }); });

  box.querySelector(".br").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-br]"); if (!b) return;
    e.preventDefault();
    const what = b.dataset.br;
    try {
      if (what === "hand") {  // #5616: the session controls it now, and its chat has the page's address
        const r = await typeAction("browser_hand", { project, to: b.dataset.to });
        toast(`${r.told} has the browser`); return show(r);
      }
      if (what === "window" || what === "headless") { msg.classList.remove("hidden"); msg.textContent = what === "window" ? "Opening a window…" : "Back to the panel…"; }
      const args = { project };
      if (what === "window" || what === "headless") args.window = what === "window";
      show(await call(what === "headless" ? "window" : what, args));
      if (what === "start") { sized = ""; fit(); }
    } catch (err) { if (what === "hand") toast(err.message, true); }
  });
  $$(".br-bar").addEventListener("submit", async (e) => {
    e.preventDefault(); const url = addr.value.trim(); if (!url) return;
    addr.blur();
    try { show(await call("open", { project, url })); } catch (err) { /* call showed it */ }
  });
  $$(".br-tabs").addEventListener("change", async (e) => {
    try { show(await call("tab", { project, target: e.target.value })); } catch (err) { /* call showed it */ }
  });
  loop();
}
