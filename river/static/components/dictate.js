// Dictation in every text box (#4626, #5107): the microphone turns speech into text at the cursor of the box; the
// person reads it and sends or saves it like typed text.
// Two ways, the first that this computer has:
//   "mac": the page records the microphone and sends the audio to maxpm serve (also through the relay from a phone);
//          on a Mac with macOS 26, serve gives it to maxpm-dictate, which uses Apple's SpeechAnalyzer on the device.
//          The words show in the box while the person speaks, and the final text replaces them.
//   "browser": the browser's own SpeechRecognition (Chrome sends the audio to Google; Safari to Apple or the device).
// With neither, no microphone shows.
// The message box of a chat has a microphone of its own (composer.js, makeDictation). Every other box that takes
// text gets one floating microphone at its right edge while it has the focus (installMics): no view adds it, and it
// moves nothing (a fixed overlay). A box inside data-mic="own" or data-mic="off" gets none.
import { MIC_SVG } from "./composer.js";

const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
let getState = () => null;

// The way to dictate on this page now: "mac", "browser", or null.
export function engine() {
  const st = getState() || {};
  if (st.dictation === "mac" && navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.AudioWorkletNode) return "mac";
  return SR ? "browser" : null;
}

async function post(op, args) {
  const r = await fetch("api/action", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ op, args }) });
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || "failed");
  return j.result;
}

// The AudioWorklet that hands each block of microphone samples to the page.
const TAP = `class Tap extends AudioWorkletProcessor {
  process(inputs) { const ch = inputs[0] && inputs[0][0]; if (ch) this.port.postMessage(ch.slice(0)); return true; }
}
registerProcessor("maxpm-tap", Tap);`;

function b64(int16) {
  const bytes = new Uint8Array(int16.buffer, int16.byteOffset, int16.byteLength);
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

// Record and send to maxpm serve. update({text, partial, note, error, done}) gets each answer.
async function macSession(lang, update) {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
  const ctx = new AudioContext();
  const url = URL.createObjectURL(new Blob([TAP], { type: "text/javascript" }));
  try { await ctx.audioWorklet.addModule(url); } finally { URL.revokeObjectURL(url); }
  const src = ctx.createMediaStreamSource(stream);
  const tap = new AudioWorkletNode(ctx, "maxpm-tap");
  const mute = ctx.createGain(); mute.gain.value = 0;  // a node must lead to the speakers to run; at no volume
  src.connect(tap); tap.connect(mute); mute.connect(ctx.destination);
  // 16 kHz, 16 bit: the average of each group of input samples (a simple low pass before the step down).
  const ratio = ctx.sampleRate / 16000;
  let pending = [], acc = 0, n = 0, t = 0;
  tap.port.onmessage = (e) => {
    for (const x of e.data) {
      acc += x; n++; t++;
      if (t >= ratio) { const v = Math.max(-1, Math.min(1, acc / n)); pending.push(v < 0 ? v * 0x8000 : v * 0x7fff); acc = 0; n = 0; t -= ratio; }
    }
  };
  let id, busy = null;
  try { id = (await post("dictate_start", { lang })).id; } catch (e) { stream.getTracks().forEach(x => x.stop()); ctx.close(); throw e; }
  const flush = () => {
    if (busy || !pending.length) return busy;
    const chunk = Int16Array.from(pending); pending = [];
    busy = post("dictate_audio", { id, pcm: b64(chunk) }).then(update, (e) => update({ error: e.message, done: true }))
      .finally(() => { busy = null; });
    return busy;
  };
  const timer = setInterval(flush, 250);
  return {
    async stop() {
      clearInterval(timer);
      stream.getTracks().forEach(x => x.stop());
      try { src.disconnect(); tap.disconnect(); await ctx.close(); } catch (e) { /* closed already */ }
      if (busy) await busy;
      if (pending.length) await flush();
      try { update({ ...(await post("dictate_stop", { id })), done: true }); } catch (e) { update({ error: e.message, done: true }); }
    },
  };
}

// The browser's own speech recognition, with the same update().
function browserSession(lang, update) {
  const rec = new SR();
  let alive = true, done = "";
  rec.lang = lang; rec.continuous = true; rec.interimResults = true;
  rec.onresult = (e) => {
    let fin = "", now = "";
    for (let i = 0; i < e.results.length; i++) (e.results[i].isFinal ? (fin += e.results[i][0].transcript) : (now += e.results[i][0].transcript));
    done = fin.trim();
    update({ text: done, partial: now.trim() });
  };
  rec.onerror = (e) => {
    if (e.error === "no-speech") return;
    alive = false;
    update({ text: done, error: e.error === "not-allowed" || e.error === "service-not-allowed"
      ? "The browser does not let this page use the microphone: allow it in the site settings."
      : e.error === "network" ? "The browser's speech service did not answer." : `Dictation stopped (${e.error}).`, done: true });
  };
  rec.onend = () => { if (alive) { try { rec.start(); } catch (e) { alive = false; } } };  // a pause ends it in some browsers
  rec.start();
  return { async stop() { alive = false; try { rec.stop(); } catch (e) { /* ended */ } update({ text: done, partial: "", done: true }); } };
}

// Dictate into one box: the spoken text goes in at the cursor (in place of a selection), the words not final yet
// after it, and the final text replaces them. say(text): a line for the person (listening, an error, a download).
// stop(): the microphone stops, and the final text still comes into the box. finish(limit): the box is sent now
// (#5118): the microphone stops, the final text comes in for at most limit ms, and after that nothing more does,
// so a box that the send emptied stays empty.
export function dictateInto(input, { changed = () => {}, say = () => {}, onEnd = () => {} } = {}) {
  const how = engine();
  if (!how) return null;
  const v = input.value, a = input.selectionStart ?? v.length, b = input.selectionEnd ?? v.length;
  const before = v.slice(0, a), after = v.slice(b);
  const join = (x, y) => !x || !y ? x + y : /\s$/.test(x) || /^\s/.test(y) ? x + y : x + " " + y;
  let last = { text: "", partial: "" }, ended = false, dropped = false;
  const show = (r) => {
    if (!r || dropped) return;
    last = { text: r.text ?? last.text, partial: r.done ? "" : (r.partial ?? last.partial) };
    const spoken = [last.text, last.partial].filter(Boolean).join(" ");
    input.value = join(join(before, spoken), after);
    const at = join(before, spoken).length;
    try { input.setSelectionRange(at, at); } catch (e) { /* not a text field */ }
    changed();
    if (r.error) say(r.error);
    else if (r.note) say(r.note);
    else if (!r.done) say(last.partial ? "… " + last.partial : "Listening. Click the microphone again to stop.");
    if (r.done && !ended) { ended = true; if (!r.error) say(""); onEnd(); }
  };
  const lang = navigator.language || "en-US";
  const run = how === "mac" ? macSession(lang, show) : Promise.resolve(browserSession(lang, show));
  say("Listening. Click the microphone again to stop.");
  run.catch((e) => {
    say(/Permission|NotAllowed/i.test(e.name + e.message) ? "The browser does not let this page use the microphone: allow it in the site settings."
      : `Dictation did not start: ${e.message}`);
    ended = true; onEnd();
  });
  // A recording that starts after a stop (the microphone's permission came late) stops when it starts.
  const stop = async () => { const s = await run.catch(() => null); if (s) await s.stop(); else if (!ended) { ended = true; onEnd(); } };
  return {
    stop,
    async finish(limit = 1500) {
      if (!(limit > 0)) dropped = true;  // the box goes now: not one more word comes into it
      await Promise.race([stop().catch(() => {}), new Promise(r => setTimeout(r, limit))]);
      dropped = true;
      if (!ended) { ended = true; onEnd(); }
      say("");
    },
  };
}

// The microphone of the message box of a chat (composer.js).
export function makeDictation({ button, input, status, changed }) {
  let cur = null;
  const say = (t) => { status.textContent = t; status.classList.toggle("hidden", !t); };
  const off = () => { cur = null; button.classList.remove("on"); button.setAttribute("aria-pressed", "false"); };
  function stop() { if (cur) { const c = cur; off(); c.stop(); } }
  // Enter or the send arrow while it listens (#5118): one send of the final text, and nothing comes back after it.
  async function finish(limit) { if (cur) { const c = cur; off(); await c.finish(limit); } }
  button.addEventListener("click", () => {
    if (cur) return stop();
    if (input.disabled) return;
    cur = dictateInto(input, { changed, say, onEnd: off });
    if (cur) { button.classList.add("on"); button.setAttribute("aria-pressed", "true"); }
  });
  return {
    stop, finish,
    listening: () => !!cur,
    // Whether the box takes text now; the microphone shows wherever this page can dictate.
    showFor(enabled) {
      const how = engine();
      button.classList.toggle("hidden", !how);
      button.disabled = !enabled;
      button.title = how ? "Dictate" : "";  // one word (#5481)
      if (how) button.setAttribute("aria-label", "Dictate");
      if (!enabled || !how) stop();
    },
  };
}

// A box that takes text and has no microphone of its own.
const TEXT_TYPES = new Set(["text", "search", "url", ""]);
function dictable(el) {
  if (!el || el.disabled || el.readOnly) return false;
  const own = el.closest("[data-mic]");
  if (own && ["own", "off"].includes(own.dataset.mic)) return false;
  return el.tagName === "TEXTAREA" || (el.tagName === "INPUT" && TEXT_TYPES.has((el.getAttribute("type") || "").toLowerCase()));
}

// One floating microphone for every other box (call once). state(): the page's state, which names the engine.
export function installMics(state) {
  getState = state;
  const btn = document.createElement("button");
  btn.type = "button"; btn.className = "mic-float hidden"; btn.setAttribute("aria-pressed", "false");
  btn.setAttribute("aria-label", "Dictate"); btn.innerHTML = MIC_SVG;
  const note = document.createElement("div");
  note.className = "mic-note hidden"; note.setAttribute("aria-live", "polite");
  document.body.append(btn, note);
  let field = null, cur = null;
  const off = () => { cur = null; btn.classList.remove("on"); btn.setAttribute("aria-pressed", "false"); };
  function place() {
    if (!field || !field.isConnected) { btn.classList.add("hidden"); note.classList.add("hidden"); return; }
    const r = field.getBoundingClientRect();
    const tall = field.tagName === "TEXTAREA" && r.height > 48;
    // A tall box that the person can resize has its grip in the bottom right corner: the mic stands left of it (#5315).
    const grip = tall && getComputedStyle(field).resize !== "none" ? 16 : 0;
    btn.style.left = `${Math.round(r.right - 28 - grip)}px`;
    btn.style.top = `${Math.round(tall ? r.bottom - 28 : r.top + (r.height - 24) / 2)}px`;
    note.style.left = `${Math.round(Math.max(8, r.right - 280))}px`;
    note.style.top = `${Math.round(r.bottom + 4)}px`;
  }
  const say = (t) => { note.textContent = t; note.classList.toggle("hidden", !t); place(); };
  function show(el) {
    field = el;
    btn.title = "Dictate";
    btn.classList.remove("hidden");
    place();
  }
  function hide() { if (cur) { const c = cur; off(); c.stop(); } field = null; btn.classList.add("hidden"); say(""); }
  document.addEventListener("focusin", (e) => {
    if (e.target === btn) return;
    if (dictable(e.target) && engine()) { if (field !== e.target) hide(); show(e.target); } else hide();
  });
  document.addEventListener("focusout", (e) => {
    if (e.target !== field) return;
    setTimeout(() => { if (document.activeElement !== field && document.activeElement !== btn) hide(); }, 0);
  });
  // The box is sent while it listens (#5118): its form submits, or Enter in a line box, or Cmd/Ctrl+Enter. What is in
  // the box goes; the microphone stops, and no text comes into the box after it (it would send the same words again).
  const sent = (e) => {
    if (!cur || !field || (e.type === "submit" ? !e.target.contains(field) : e.target !== field)) return;
    if (e.type === "keydown" && !(e.key === "Enter" && !e.shiftKey && !e.isComposing
        && (field.tagName === "INPUT" || e.metaKey || e.ctrlKey))) return;
    const c = cur; off(); c.finish(0);
  };
  document.addEventListener("submit", sent, true);
  document.addEventListener("keydown", sent, true);
  btn.addEventListener("pointerdown", (e) => e.preventDefault());  // the box keeps the focus and its cursor
  btn.addEventListener("click", () => {
    if (!field) return;
    if (cur) { const c = cur; off(); c.stop(); return; }
    const f = field;
    cur = dictateInto(f, { changed: () => f.dispatchEvent(new Event("input", { bubbles: true })), say, onEnd: off });
    if (cur) { btn.classList.add("on"); btn.setAttribute("aria-pressed", "true"); }
    f.focus();
  });
  window.addEventListener("scroll", place, true);
  window.addEventListener("resize", place);
}
