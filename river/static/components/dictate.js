// Dictation in the message box (#4626). Claude Code and Codex record the microphone of the computer they run on
// and take no audio from outside, so the page turns speech into text with the browser's own speech recognition:
// the text goes into the box, and the person reads it and sends it like a typed message. No account and no key;
// the browser decides where the audio goes (Chrome sends it to Google; Safari to Apple, or on the device).
// The button shows only for a session whose provider takes text this way (provider.voice "dictate"), in a
// browser that has speech recognition.
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;

export function makeDictation({ button, input, status, changed }) {
  let rec = null, base = "";
  const say = (t) => { status.textContent = t; status.classList.toggle("hidden", !t); };
  function stop() {
    if (rec) { const r = rec; rec = null; try { r.stop(); } catch (e) { /* it ended already */ } }
    button.classList.remove("on"); button.setAttribute("aria-pressed", "false");
    say("");
  }
  function start() {
    rec = new SR();
    rec.lang = navigator.language || "en-US";
    rec.continuous = true;
    rec.interimResults = true;
    base = input.value.replace(/\s+$/, "");
    rec.onresult = (e) => {
      let done = "", now = "";
      for (let i = 0; i < e.results.length; i++) (e.results[i].isFinal ? (done += e.results[i][0].transcript) : (now += e.results[i][0].transcript));
      input.value = [base, done.trim()].filter(Boolean).join(base && done.trim() ? " " : "");
      say(now ? "… " + now : "Listening. Click the microphone again to stop; then Send.");
      changed();
    };
    rec.onerror = (e) => {
      const why = e.error === "not-allowed" || e.error === "service-not-allowed" ? "The browser does not let this page use the microphone: allow it in the site settings."
        : e.error === "no-speech" ? "" : e.error === "network" ? "The browser's speech service did not answer." : `Dictation stopped (${e.error}).`;
      stop();
      if (why) say(why);
    };
    rec.onend = () => { if (rec) { try { rec.start(); } catch (e) { stop(); } } };  // a pause ends it in some browsers
    rec.start();
    button.classList.add("on"); button.setAttribute("aria-pressed", "true");
    say("Listening. Click the microphone again to stop; then Send.");
  }
  button.addEventListener("click", () => { if (rec) stop(); else if (!input.disabled) { try { start(); } catch (e) { stop(); say(`Dictation did not start: ${e.message}`); } } });
  return {
    stop,
    // The provider of the chosen session, and whether its box takes text now.
    showFor(provider, enabled) {
      const yes = !!(SR && provider && provider.voice === "dictate");
      button.classList.toggle("hidden", !yes);
      button.disabled = !enabled;
      button.title = !yes ? "" : `Dictate: speech becomes text in the box, and you send it. ${provider.label} has a voice mode of its own: ${provider.voice_cli}.`;
      if (!enabled || !yes) stop();
    },
  };
}
