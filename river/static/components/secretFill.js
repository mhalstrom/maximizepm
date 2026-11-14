// The masked field for a secret that a session's browser waits at (#5866, mark 2026-11-14 on the iPhone: 'i can't
// manage to paste on my iphone', 'i need a way to provide secrets by text'). The Browser pane draws the page as
// frames, so a phone has no field of its own to paste into: this is a native field of the person's device, with
// Fill, and for a password 'Save as credential'. Fill sends the value once (op browser_secret, a typing op) and
// empties the field; serve types it into the page's password or code field and presses Enter. The value goes into
// no chat, message, log or canvas. The Browser pane shows it when the page shows such a field (frame status
// secret_field); a needs-you card that links a session's browser shows it with no field known (kind null: the
// server finds the password or code field when Fill comes).
import { esc, toast, typeAction } from "../lib.js";
import { button } from "./button.js";

const b64 = (text) => btoa(Array.from(new TextEncoder().encode(text), c => String.fromCharCode(c)).join(""));

// f: {kind: 'password' | 'code', site, account, label} or null (not known yet).
export function secretFillHtml(f) {
  const code = f && f.kind === "code", pw = f && f.kind === "password";
  const site = f && f.site ? esc(f.site.replace(/^www\./, "")) : "";
  const head = code ? `The page asks for a code${site ? ` (${site})` : ""}`
    : pw ? `The page asks for a password${f.account ? ` for ${esc(f.account)}` : site ? ` (${site})` : ""}`
    : "Password or code for this browser";
  return `<form class="sf" autocomplete="off" aria-label="${code ? "Code" : "Password"} for the page">
    <div class="sf-h t-s">${head}. Paste or type it here: it goes into the page's field only.</div>
    <div class="sf-row">
      <input class="sf-in" type="password" aria-label="${code ? "Code" : "Password"}"
        autocomplete="${code ? "one-time-code" : "off"}" autocapitalize="off" autocorrect="off" spellcheck="false"
        ${code ? 'inputmode="numeric"' : ""} enterkeyhint="go">
      ${button("Fill", { kind: "primary", cls: "sf-go", type: "submit", title: "Fill the page's field" })}
    </div>
    ${code ? "" : `<label class="sf-save t-s"><input type="checkbox" class="sf-keep"> Save as credential</label>
    <label class="sf-acct t-s hidden">Account<input class="sf-ain" autocapitalize="off" autocorrect="off"
      spellcheck="false" value="${esc((f && f.account) || "")}"></label>`}
  </form>`;
}

// where(): {project, session} of the browser to fill. onDone(result) after a Fill.
export function wireSecretFill(form, where, onDone) {
  const input = form.querySelector(".sf-in"), keep = form.querySelector(".sf-keep"), acct = form.querySelector(".sf-acct");
  if (keep) keep.addEventListener("change", () => acct.classList.toggle("hidden", !keep.checked));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const value = input.value;
    if (!value) return toast("Paste or type the password or code first", true);
    const go = form.querySelector(".sf-go");
    const args = { ...where(), value_b64: b64(value) };
    if (keep && keep.checked) { args.save = true; args.account = form.querySelector(".sf-ain").value.trim(); }
    input.value = "";  // the value leaves the page now; nothing shows it again
    go.disabled = true;
    try {
      const r = await typeAction("browser_secret", args);
      toast(`Filled the ${r.filled}` + (r.save ? `; it is saved as ${r.save.name} when the sign-in works` : ""));
      if (onDone) onDone(r);
    } catch (err) { toast(err.message, true); }
    finally { go.disabled = false; }
  });
}
