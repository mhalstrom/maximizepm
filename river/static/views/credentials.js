// The Credentials panel of Settings (#5829, design: canvas credential-vault). It shows each credential by its name,
// never its value: kind, site and account, level, sets, last use. Each set shows what it holds and its grants. The
// person adds a sign-in (the password field goes to the Keychain through maxpm serve and never comes back to the page),
// changes a level, makes a set, grants a set and revokes a grant. GET /api/credentials reads; op "secret" changes.
import { $, esc, toast, act, ago, getJson } from "../lib.js";
import { chip } from "../components/chip.js";
import { button, iconButton } from "../components/button.js";
import { empty } from "../components/empty.js";
import { S } from "../page.js";

let C = null, readAt = 0, reading = false;
const AGAIN = 20000;  // ms: the panel reads the credentials again at most this often while Settings shows

// Read the credentials when Settings shows: at once with force (after a change), else at most once in AGAIN.
export async function renderCredentials(force) {
  const tab = $("#tab-settings");
  if (!tab || tab.classList.contains("hidden") || reading || (!force && C && Date.now() - readAt < AGAIN)) return;
  reading = true;
  try { C = await getJson("api/credentials"); readAt = Date.now(); }
  catch (e) { $("#credList").innerHTML = empty(`The credentials could not be read: ${esc(e.message)}`); return; }
  finally { reading = false; }
  draw();
}

const KIND = { signin: "sign-in", token: "token", cookies: "saved sign-in" };
const put = (el, html) => { if (el.dataset.sig !== html) { el.innerHTML = html; el.dataset.sig = html; } };

function draw() {
  const rows = C.credentials || [];
  put($("#credList"), rows.map(credRow).join("")
    || empty("No credentials yet. Add a sign-in below, or run maxpm secret put &lt;name&gt; in a terminal."));
  put($("#credSets"), (C.sets || []).map(setRow).join("") || empty("No sets yet. A set holds credentials that you grant together."));
  put($("#credForms"), forms());
}

function credRow(r) {
  const where = r.kind === "signin" ? [r.site, r.account].filter(Boolean).map(esc).join(" · ") : r.var ? esc(r.var) : "";
  const used = r.used_at ? `used ${ago(r.used_at)}${r.used_by ? " by " + esc(r.used_by) : ""}` : "never used";
  const levels = (C.levels || []).map(l => `<option${l === r.level ? " selected" : ""}>${esc(l)}</option>`).join("");
  return `<div class="cred">
    <div class="cred-main"><b>${esc(r.name)}</b> ${chip("c-p", esc(KIND[r.kind] || r.kind || "token"))}
      ${(r.sets || []).map(s => chip("c-goal", esc(s), "In this set")).join(" ")}
      ${r.fail_why ? chip("c-human", "sign-in failed", "Last sign-in failed") : ""}
      <div class="muted t-s">${where ? where + " · " : ""}${used}${r.fail_why ? " · " + esc(r.fail_why) : ""}</div></div>
    <label class="cred-level t-s">Level <select data-cred-level="${esc(r.name)}" title="Who may use it">${levels}</select></label>
  </div>`;
}

function setRow(s) {
  const grants = (C.grants || []).filter(g => g.set === s.name);
  const scopes = (C.scopes || []).map(x => `<option>${esc(x)}</option>`).join("");
  return `<div class="cred-set">
    <div><b>${esc(s.name)}</b>${s.manager_may_grant ? " " + chip("c-p", "manager may grant") : ""}
      <span class="muted t-s">${s.secrets.length ? s.secrets.map(esc).join(", ") : "holds nothing yet"}</span></div>
    <div class="cred-grants">${grants.map(g => `<span class="cred-grant">${chip("c-ready", esc(g.covers || `${g.scope} ${g.value}`))}`
      + `${g.until ? `<span class="muted t-s"> until ${esc(g.until.slice(0, 10))}</span>` : ""}`
      + iconButton("✕", `Revoke ${g.grant}`, { data: { credRevoke: g.id } }) + `</span>`).join("")
      || `<span class="muted t-s">No grant: no session can use it.</span>`}</div>
    <div class="cred-grantform" data-set="${esc(s.name)}">
      <select class="cred-scope" title="Grant to what">${scopes}</select>
      <input class="cred-value" placeholder="project, goal, kind, #task or agent" list="credChoices-${esc(s.name)}">
      <datalist id="credChoices-${esc(s.name)}">${choices(C.scopes ? C.scopes[0] : "project")}</datalist>
      <input class="cred-until" placeholder="until (optional: 7d, 2026-12-01)">
      ${button("Grant", { data: { credGrant: s.name } })}
    </div>
  </div>`;
}

// The names a grant can take for one scope, from the page's state.
function choices(scope) {
  const names = scope === "project" ? (S.projects || []).map(p => p.name)
    : scope === "goal" ? (S.goals || []).filter(g => g.status === "open").map(g => g.name)
    : scope === "kind" ? (C.kinds || [])
    : scope === "agent" ? (S.agents || []).filter(a => a.kind !== "human").map(a => a.name)
    : [];
  return names.map(n => `<option value="${esc(n)}">`).join("");
}

function forms() {
  const sets = (C.sets || []).map(s => `<option>${esc(s.name)}</option>`).join("");
  return `<div class="cred-sub">Add a sign-in</div>
  <div class="form cred-form" id="credSignin">
    <div class="two"><label>Name<input id="credName" placeholder="google-mark" autocomplete="off"></label>
      <label>Site<input id="credSite" placeholder="google.com" autocomplete="off"></label></div>
    <div class="two"><label>Account<input id="credAccount" placeholder="me@gmail.com" autocomplete="off"></label>
      <label>Password<input id="credPassword" type="password" autocomplete="new-password"></label></div>
    <label>Add to set<select id="credSet"><option value="">no set</option>${sets}</select></label>
    <div class="actions">${button("Save sign-in", { kind: "primary", id: "credSave" })}</div>
  </div>
  <div class="cred-sub">New set</div>
  <div class="form cred-form">
    <label>Name<input id="setName" placeholder="stripe-live" autocomplete="off"></label>
    <label class="cred-check"><input type="checkbox" id="setManager"> The manager may grant it</label>
    <div class="actions">${button("Make set", { id: "credMakeSet" })}</div>
  </div>`;
}

// UTF-8 text as base64, as op "secret" takes a value.
const b64 = (text) => btoa(Array.from(new TextEncoder().encode(text), c => String.fromCharCode(c)).join(""));

const panel = $("#credPanel");
panel.addEventListener("change", async (e) => {
  const t = e.target;
  if (t.dataset.credLevel) {
    try { await act("secret", { action: "level", name: t.dataset.credLevel, level: t.value }); toast(`${t.dataset.credLevel} is ${t.value} now`); }
    catch (err) { /* act showed the error */ }
    return renderCredentials(true);
  }
  if (t.classList.contains("cred-scope")) {  // the names that the chosen scope can take
    const f = t.closest(".cred-grantform");
    f.querySelector("datalist").innerHTML = choices(t.value);
    f.querySelector(".cred-value").value = "";
  }
});
panel.addEventListener("click", async (e) => {
  const t = e.target.closest("button");
  if (!t) return;
  if (t.dataset.credRevoke) {
    if (!confirm("Revoke this grant? Sessions that it covers lose the set at their next use.")) return;
    try { await act("secret", { action: "revoke", grant: +t.dataset.credRevoke }); toast("Revoked"); } catch (err) { /* shown */ }
    return renderCredentials(true);
  }
  if (t.dataset.credGrant) {
    const f = t.closest(".cred-grantform"), value = f.querySelector(".cred-value").value.trim();
    if (!value) return toast("Name what the set is for", true);
    const until = f.querySelector(".cred-until").value.trim();
    try {
      const g = await act("secret", { action: "grant", name: t.dataset.credGrant, scope: f.querySelector(".cred-scope").value,
                                      value, until: until || null });
      toast(g.already ? `${g.grant} holds already` : `Granted: ${g.grant}`);
    } catch (err) { return; }
    return renderCredentials(true);
  }
  if (t.id === "credSave") return saveSignin();
  if (t.id === "credMakeSet") {
    const name = $("#setName").value.trim();
    if (!name) return toast("Type the set's name", true);
    try { await act("secret", { action: "set", name, manager_may_grant: $("#setManager").checked }); toast(`Made the set ${name}`); }
    catch (err) { return; }
    return renderCredentials(true);
  }
});

async function saveSignin() {
  const name = $("#credName").value.trim(), site = $("#credSite").value.trim(), account = $("#credAccount").value.trim();
  const pw = $("#credPassword"), set = $("#credSet").value;
  if (!name || !site || !account || !pw.value) return toast("Fill in the name, site, account and password", true);
  const value_b64 = b64(pw.value);
  pw.value = "";  // the password leaves the page now; nothing shows it again
  try {
    await act("secret", { action: "signin", name, site, account, value_b64 });
    if (set) await act("secret", { action: "set", name: set, add: [name] });
  } catch (err) { return; }
  ["#credName", "#credSite", "#credAccount"].forEach(s => { $(s).value = ""; });
  toast(`Saved the sign-in ${name}` + (set ? ` in ${set}` : ""));
  renderCredentials(true);
}
