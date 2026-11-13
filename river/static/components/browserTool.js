// The browser of a project in the side panel (#4631, design #4641): a tool tab of the panel (openTool). maxpm serve
// runs a headless Google Chrome for each session of a project (river/browser.py, #5636); this tab shows the page of
// the Chrome of the panel's session as frames (long polls of GET api/browser/frame, binary: #5637), and sends the
// person's clicks, wheel, keys and pasted text (op browser_input, with the page's token). Back, Forward, Reload, an address field, the tabs of the browser, and who controls it: nobody, the
// person (Take control, Give back), or a session (maxpm browser control) and the subagent it names. While a session
// controls it, the person's input waits until Take control. "Open as a window" shows the same browser as a normal
// window, for a site that refuses a headless browser (a sign-in); "Back to the panel" ends that.
// #5616: over the page it draws the pointer of the session that drives (it glides to each point, a ring marks each
// press, a tag names the session) and the keys it types (dots in a password field); serve records them. "Hand to
// <session>" gives the browser to the session of this panel and puts the page's address in its chat.
// #5635: the address field suggests as the person types: the addresses this browser visited (serve keeps them in
// memory: Chrome's History and the pages its tabs showed), and a last row that searches Google for the words.
import { esc, toast, actor, pageToken, typeAction } from "../lib.js";
import { button } from "./button.js";
import { makeSuggest } from "./suggestList.js";
import { openTool, panelSession } from "./sidePanel.js";

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function callOp(action, args = {}) {
  const r = await fetch("api/action", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ op: "browser", args: { action, ...args }, actor: actor() }) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { toast(j.error || "failed", true); throw new Error(j.error || "failed"); }
  return j.result;
}

// Each session has a browser of its own (#5636): the tab key names the session too, because the panel keeps one set
// of tabs for each session, and its browser is that session's Chrome.
const showTool = (project, session) => openTool("browser:" + project + ":" + (session || ""), "Browser · " + project,
  (box) => render(box, project, session));

// The browser of the session's project: the tab "Browser · <project>" in the panel; with url, it opens that address
// (an outside address of the side panel, #5628).
export async function openBrowser(session, url) {
  session = session || panelSession();
  const info = await callOp("status", session ? { agent: session, session } : {});
  showTool(info.project, session);
  if (url) await callOp("open", { project: info.project, session, url });
}

// maxpm browser open (#4631): a session asks the page to show its browser. The request comes in the state; the tab
// opens when the panel shows that session.
let seen = null;
export function browserOpens(list) {
  list = list || [];
  const top = list.reduce((m, x) => Math.max(m, x.seq), 0);
  if (seen === null || top < seen) { seen = top; return; }
  for (const x of list.filter(x => x.seq > seen))
    if (x.session && x.session === panelSession()) showTool(x.project, x.session);
  seen = top;
}

const MODS = new Set(["Shift", "Control", "Alt", "Meta", "CapsLock", "Fn"]);
const KEYS = new Set(["Enter", "Backspace", "Tab", "Escape", "Delete", "ArrowLeft", "ArrowUp", "ArrowRight", "ArrowDown",
  "Home", "End", "PageUp", "PageDown"]);
const mods = (e) => ({ alt: e.altKey, ctrl: e.ctrlKey, meta: e.metaKey, shift: e.shiftKey });

function render(box, project, session) {
  session = session || "";
  const call = (action, args = {}) => callOp(action, { session, ...args });  // this panel's session's browser
  box.innerHTML = `<div class="br">
    <div class="br-strip" role="tablist" aria-label="Tabs"></div>
    <form class="br-bar" autocomplete="off">
      ${button("◀", { kind: "icon", label: "Back", title: "Back", data: { br: "back" } })}
      ${button("▶", { kind: "icon", label: "Forward", title: "Forward", data: { br: "forward" } })}
      ${button("↻", { kind: "icon", label: "Reload", title: "Load the page again", data: { br: "reload" } })}
      <input class="br-addr" placeholder="Address or search" aria-label="Address or search"
        autocapitalize="off" spellcheck="false" enterkeyhint="go">
    </form>
    <div class="br-ctl"><span class="br-who muted t-s"></span>
      <span class="br-refused hidden t-s">This site refuses a browser with no window: open it as a window.</span><span class="br-sp"></span>
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
  const pane = Math.random().toString(36).slice(2, 12);  // this panel, for the size rule (#5625)
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
      try { await typeAction("browser_input", { project, session, pane, evs }); } catch (e) { toast(e.message, true); }
    }
    busy = false;
  }

  // Who controls it: the line, the buttons, and whether the person's input goes through. Drawn again only when the
  // status changed (#5637): most poll answers bring a frame and the same status.
  let shownKey = "";
  function show(j) {
    const { seq, waited, frame, pointer, typed: t, pane_known, ...status } = j;
    const key = JSON.stringify(status);
    if (key === shownKey) return draw(j);
    shownKey = key;
    v.running = !!j.running; v.window = !!j.window;
    // No Chrome on this computer (#5624): the answer of start says so; it holds until a start runs Chrome.
    if ("no_chrome" in j || j.running) v.noChrome = j.running ? "" : j.no_chrome || "";
    const c = j.controller, me = actor();
    v.mine = !c || c.kind === "person";
    $$(".br-who").textContent = !j.running ? "The browser is not running."
      : !c ? "Nobody controls it: you can use it. A session takes it with maxpm browser control."
      : c.kind === "person" ? (c.who === me || !me ? "You control it." : `${c.who} controls it.`)
      : c.took ? `${c.who} took control (${c.took}). Take control to get it back.`  // #5629
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
    win.classList.toggle("hidden", !j.modes || !!v.noChrome);  // a Chrome that runs already (browser_source) needs browser_mode (#5619)
    win.classList.toggle("primary", !!j.refused && !j.window);
    $$(".br-refused").classList.toggle("hidden", !(j.refused && !j.window));
    stage.classList.toggle("br-locked", !v.mine);
    if (document.activeElement !== addr) addr.value = j.url && j.url !== "about:blank" ? j.url : "";
    strip(j.tabs || []);
    msg.classList.toggle("hidden", j.running && !!v.w);
    if (!j.running) msg.innerHTML = v.noChrome
      ? `The Browser needs Google Chrome, and this computer does not have it. Download it, install it, then press Check again.
         <div class="br-dl"><a class="btn primary" href="${esc(j.download || "https://www.google.com/chrome/")}" target="_blank" rel="noopener">Download Google Chrome</a>
         ${button("Check again", { data: { br: "start" } })}</div>`
      : `The browser of ${esc(project)} is not running. ${button("Start", { kind: "primary", data: { br: "start" } })}`;
    draw(j);
  }

  // A frame (#5637): the JPEG bytes of the binary answer, shown from a blob address. A poll that was slower than
  // another brings an older frame: it is dropped. The addresses that no longer show are freed once the new one loads.
  let shown = 0, urls = [];
  function paint(f, jpeg) {
    if (f.seq && f.seq <= shown) return;
    shown = f.seq || shown;
    const u = URL.createObjectURL(new Blob([jpeg], { type: "image/jpeg" }));
    urls.push(u);
    img.src = u;
    v.w = f.w || v.w; v.h = f.h || v.h;
    msg.classList.add("hidden");
  }
  img.addEventListener("load", () => { const keep = img.src; urls = urls.filter(u => u === keep || URL.revokeObjectURL(u)); });

  // The tab strip (#5632), like Chrome's: each tab with its icon and title (a click shows it), x closes it, + opens a
  // new one; the session whose pointer is on a tab is named on it. Drawn again only when a tab changed.
  let stripKey = "";
  function strip(tabs) {
    const key = JSON.stringify(tabs.map(t => [t.id, t.title, t.url, t.on, !!t.icon, t.agent]));
    if (key === stripKey) return;
    stripKey = key;
    const el = $$(".br-strip");
    el.innerHTML = tabs.map(t => `<div class="br-tab${t.on ? " on" : ""}">${button(
        (t.icon ? `<img class="br-ico" src="${esc(t.icon)}" alt="">` : `<span class="br-ico br-ico0"></span>`)
        + `<span class="br-tabt">${esc(t.title || (t.url !== "about:blank" && t.url) || "New tab")}</span>`
        + (t.agent ? `<span class="br-ta t-s">${esc(t.agent)}</span>` : ""),
        { cls: "br-go", data: { br: "tab", target: t.id }, attrs: `role="tab" aria-selected="${t.on ? "true" : "false"}"` })}${
      button("×", { kind: "icon", label: "Close tab", title: "Close tab", data: { br: "close_tab", target: t.id } })}</div>`).join("")
      + button("+", { kind: "icon", label: "New tab", title: "New tab", data: { br: "new_tab" } });
    const on = el.querySelector(".br-tab.on");
    if (on) on.scrollIntoView({ block: "nearest", inline: "nearest" });
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

  // The page takes the size of the stage, at the pixel density of this screen (#5625; headless only), once the size
  // holds for 150 ms (a drag of the divider). Each panel says its size; serve uses the size of the panel where the
  // person last clicked, typed or took control (else the largest), and the other panels show the frame scaled.
  let sized = "", fitting = null;
  const fitNow = () => {
    fitting = null;
    const r = stage.getBoundingClientRect(), dpr = Math.min(2, window.devicePixelRatio || 1);
    const w = Math.floor(r.width), h = Math.floor(r.height), key = `${w}x${h}@${dpr}`;
    if (r.width < 50 || key === sized || !v.running) return;
    sized = key;
    call("resize", { project, pane, w, h, dpr, screen: [window.screen.width, window.screen.height] }).catch(() => { sized = ""; });
  };
  const fit = (again) => { if (fitting && !again) return; clearTimeout(fitting); fitting = setTimeout(fitNow, 150); };
  const ro = new ResizeObserver(() => fit(true)); ro.observe(stage);

  // The frames (#5637): binary answers (bin=1: the length of a JSON head, the head, the JPEG). Over a long link (a
  // phone through the relay) each poll costs a round trip, so the pane keeps one poll open for each 30 ms of it, at
  // most four; serve gives each one the next change, so at most that many frames are on the way, never a backlog.
  let slots = 1, trips = [];
  async function poll(i) {
    while (box.isConnected) {
      if (document.hidden || i >= slots) { await sleep(i ? 250 : 1000); continue; }
      try {
        token = token || await pageToken();
        const t = performance.now();
        const r = await fetch(`api/browser/frame?project=${encodeURIComponent(project)}&session=${encodeURIComponent(session)}&after=${v.seq}&pane=${pane}&bin=1&token=${encodeURIComponent(token)}`);
        if (r.status === 403) { token = await pageToken(true); continue; }
        if (!r.ok) { await sleep(2000); continue; }
        const buf = await r.arrayBuffer();
        if (!box.isConnected) break;
        const n = new DataView(buf).getUint32(0), j = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 4, n)));
        trips = trips.concat(performance.now() - t - (j.waited || 0)).slice(-20);
        slots = Math.max(1, Math.min(4, Math.ceil(Math.min(...trips) / 30)));
        const was = v.running;
        v.seq = Math.max(v.seq, j.seq || 0);
        if (j.frame && buf.byteLength > 4 + n) paint(j.frame, new Uint8Array(buf, 4 + n));
        show(j);
        // serve started again (it does not know this panel), or the window went to a screen of another density.
        if ((!was && v.running) || (v.running && !j.window && j.pane_known === false)) sized = "";
        fit();
        if (!j.running) await sleep(2000);
      } catch (e) { await sleep(2000); }
    }
  }

  async function loop() {
    try { show(await call("start", { project })); } catch (e) { show({ running: false }); }
    fit();
    await Promise.all([0, 1, 2, 3].map(poll));
    ro.disconnect();
  }

  // The point in the page's CSS pixels.
  const at = (e) => { const r = img.getBoundingClientRect();
    return { x: (e.clientX - r.left) * (v.w || r.width) / r.width, y: (e.clientY - r.top) * (v.h || r.height) / r.height }; };
  // A drag's moves and the wheel go once for each frame of the screen (#5637); a move that waits goes before the up.
  let down = false, move = null, moving = 0, wheel = null;
  const sendMove = () => { cancelAnimationFrame(moving); moving = 0; if (move) { const m = move; move = null; send(m); } };
  img.addEventListener("pointerdown", (e) => {
    e.preventDefault(); keys.focus({ preventScroll: true });
    if (!v.mine) return toast("A session controls the browser: press Take control first", true);
    down = true; img.setPointerCapture(e.pointerId);
    send({ type: "mouse", action: "down", ...at(e), button: ["left", "middle", "right"][e.button] || "left", clicks: e.detail || 1, mods: mods(e) });
  });
  img.addEventListener("pointerup", (e) => {
    if (!down) return; down = false;
    sendMove();
    send({ type: "mouse", action: "up", ...at(e), button: ["left", "middle", "right"][e.button] || "left", clicks: e.detail || 1, mods: mods(e) });
  });
  img.addEventListener("pointermove", (e) => {
    if (!down || !v.mine) return;
    move = { type: "mouse", action: "move", ...at(e), mods: mods(e) };
    if (!moving) moving = requestAnimationFrame(sendMove);
  });
  img.addEventListener("contextmenu", (e) => e.preventDefault());
  stage.addEventListener("wheel", (e) => {
    e.preventDefault(); if (!v.mine) return;
    if (wheel) { wheel.dx += e.deltaX; wheel.dy += e.deltaY; return; }
    wheel = { type: "mouse", action: "wheel", ...at(e), dx: e.deltaX, dy: e.deltaY };
    requestAnimationFrame(() => { const w = wheel; wheel = null; send(w); });
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
        const r = await typeAction("browser_hand", { project, session, to: b.dataset.to });
        toast(`${r.told} has the browser`); return show(r);
      }
      if (what === "window" || what === "headless") { msg.classList.remove("hidden"); msg.textContent = what === "window" ? "Opening a window…" : "Back to the panel…"; }
      const args = { project, pane };
      if (what === "window" || what === "headless") args.window = what === "window";
      if (b.dataset.target) args.target = b.dataset.target;  // a tab of the strip (#5632)
      show(await call(what === "headless" ? "window" : what, args));
      if (what === "start") { sized = ""; fit(); }
      if (what === "new_tab") addr.focus();  // a new tab: type its address
    } catch (err) { if (what === "hand") toast(err.message, true); }
  });
  const go = async (url) => {
    addr.blur();
    try { show(await call("open", { project, pane, url })); } catch (err) { /* call showed it */ }
  };
  $$(".br-bar").addEventListener("submit", (e) => { e.preventDefault(); const url = addr.value.trim(); if (url) go(url); });
  // The rows: each address with its title, then "Search Google for <words>" (Enter on words does the same: serve
  // makes words a search). Not through call(): a failed suggestion shows no toast.
  makeSuggest(addr, $$(".br-bar"), {
    label: "Addresses and a search",
    load: async (text) => {
      const r = await fetch("api/action", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ op: "browser", args: { action: "suggest", project, session, text }, actor: actor() }) });
      const items = r.ok ? ((await r.json()).result || {}).items || [] : [];
      return items.map(x => ({ value: x.url,
          html: `<span class="br-st">${esc(x.title || x.url)}</span><span class="br-su">${esc(x.url)}</span>` }))
        .concat([{ value: "https://www.google.com/search?q=" + encodeURIComponent(text.trim()),
          html: `<span class="br-st">Search Google for “${esc(text.trim())}”</span>` }]);
    },
    pick: (url) => { addr.value = url; go(url); },
  });
  loop();
}
