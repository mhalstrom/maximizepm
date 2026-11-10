// The Terminal tab (#4803): plain shells on the computer that runs maxpm serve (GET /api/shell; the ops shell_open,
// shell_keys and shell_close, with the page's token). Open a terminal starts one in the home folder and shows it in
// the Terminal dialog (components/terminalDialog.js), as an agent's terminal; Open shows it again; Close ends it.
// Only a person uses it; through the relay only when the setting web_terminal_relay is on.
import { $, esc, ago, toast, typeAction } from "../lib.js";
import { openTerminal } from "../components/terminalDialog.js";
import { button } from "../components/button.js";
import { empty } from "../components/empty.js";

export function makeTerminalTab({ isShown }) {
  let busy = false;
  async function refresh() {
    if (busy || document.hidden || !isShown()) return;
    busy = true;
    try {
      const r = await fetch("api/shell"), j = await r.json();
      const list = $("#shellList"), state = $("#shellState");
      $("[data-shell-new]").disabled = !r.ok || !j.tmux;
      if (!r.ok) {
        state.textContent = j.via_relay && j.web_terminal
          ? "The Terminal tab is off through the relay: whoever holds your relay sign-in could run any command on your Mac. To allow it, run on the Mac: maxpm config set web_terminal_relay on"
          : (j.error || "The Terminal tab is off") + ". To allow it: maxpm config set web_terminal on";
        list.innerHTML = ""; return;
      }
      state.textContent = j.tmux ? (j.via_relay ? "You use this page through the relay." : "") : "A terminal needs tmux on this computer: brew install tmux";
      $("#shellNote").textContent = j.note || "";
      list.innerHTML = j.shells.map(s => `<div class="sh-row">
          <div class="sh-t"><b>${esc(s.name)}</b> <span class="muted">${esc(s.running)} in <code>${esc(s.path)}</code>${s.by ? ` · opened by ${esc(s.by)}` : ""}${s.started ? ` ${ago(new Date(s.started * 1000).toISOString())}` : ""}</span></div>
          ${button("Open", { kind: "primary", data: { "shell-open": s.pane, "shell-name": s.name } })}
          ${button("Close", { data: { "shell-close": s.pane } })}</div>`).join("")
        || empty("No terminal is open.");
    } catch (e) {
      $("#shellState").textContent = "maxpm serve does not answer";
    } finally { busy = false; }
  }
  $("#tab-terminal").addEventListener("click", async (e) => {
    const t = e.target.closest("button"); if (!t) return;
    try {
      if (t.hasAttribute("data-shell-new")) {
        const r = await typeAction("shell_open", {});
        openTerminal(r.pane, { shell: true, title: r.name });
      }
      if (t.dataset.shellOpen) openTerminal(t.dataset.shellOpen, { shell: true, title: t.dataset.shellName });
      if (t.dataset.shellClose) { await typeAction("shell_close", { pane: t.dataset.shellClose }); toast("Closed the terminal"); }
    } catch (err) { toast(err.message || "failed", true); }
    busy = false; refresh();
  });
  setInterval(refresh, 5000);
  return { refresh };
}
