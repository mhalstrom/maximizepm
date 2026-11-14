// The Credentials panel of Settings (#5829, design: canvas credential-vault). It shows each credential by its name,
// never its value: kind, site and account, when asked (#5842), level, sets, last use. Each set shows what it holds and its grants. The
// person adds a sign-in (the password field goes to the Keychain through maxpm serve and never comes back to the page),
// changes a level, makes a set, grants a set and revokes a grant. GET /api/credentials reads; op "secret" changes.
import { $, esc, toast, act, ago, getJson } from "../lib.js";
import { chip } from "../components/chip.js";
import { button, iconButton } from "../components/button.js";
import { empty } from "../components/empty.js";
import { openBrowser } from "../components/browserTool.js";
import { S, nav } from "../page.js";

let C = null, readAt = 0, reading = false;
const AGAIN = 20000;  // ms: the panel reads the credentials again at most this often while Settings shows

// Read the credentials when Settings shows its Credentials tab (#5848): at once with force (after a change), else at
// most once in AGAIN.
export async function renderCredentials(force) {
  const tab = $("#tab-settings");
  if (!tab || tab.classList.contains("hidden") || $("#credPanel").classList.contains("hidden") || reading
      || (!force && C && Date.now() - readAt < AGAIN)) return;
  reading = true;
  try { C = await getJson("api/credentials"); readAt = Date.now(); }
  catch (e) { $("#credList").innerHTML = empty(`The credentials could not be read: ${esc(e.message)}`); return; }
  finally { reading = false; }
  draw();
}

const KIND = { signin: "sign-in", token: "token", cookies: "saved sign-in" };
const put = (el, html) => { if (el.dataset.sig !== html) { el.innerHTML = html; el.dataset.sig = html; } };

// #5849: the credential that the person changes now: {name, kind}, and for a saved sign-in the browser it opened.
let editing = null;

function draw() {
  const rows = C.credentials || [];
  drawRequests();
  // A password typed in the edit box stays: the list is not drawn again while it holds text.
  const typed = $("#credNewPw");
  if (typed && typed.value) return;
  put($("#credList"), rows.map(credRow).join("")
    || empty("No credentials yet. Add a sign-in below, or run maxpm secret put &lt;name&gt; in a terminal."));
  put($("#credSets"), (C.sets || []).map(setRow).join("") || empty("No sets yet. A set holds credentials that you grant together."));
  put($("#credForms"), forms());
}

// #5833 (mark 2026-11-14): a request at the person is a checklist of credentials by name, the manager's picks ticked,
// with its task and reason; the rest of the credentials under "More credentials", unticked. OK grants exactly the
// ticked ones (op secret, action approve, only). A request still at the manager shows as one line.
// A box that the person changed keeps its ticks while the panel reads again: drawn once per request and state.
function drawRequests() {
  const host = $("#credRequests");
  const reqs = (C.requests || []).filter(r => r.state === "asked" || r.state === "open");
  const sig = reqs.map(r => `${r.id}:${r.state}:${(r.offered || []).join(",")}:${(r.ticked || []).join(",")}`).join("|");
  if (host.dataset.sig === sig) return;
  host.dataset.sig = sig;
  host.innerHTML = reqs.map(reqBox).join("");
}

function pick(id, name, on) {
  return `<label class="cred-check"><input type="checkbox" data-req-pick="${id}" value="${esc(name)}"${on ? " checked" : ""}> ${esc(name)}</label>`;
}

function reqBox(r) {
  const task = r.task ? `${esc(r.covers)}: ${esc(r.task)}` : esc(r.covers);
  if (r.state === "open") {
    return `<div class="cred-req is-open t-s"><b>${esc(r.asked_by)}</b> asks the manager for ${esc(r.set_name)} (${task})`
      + `${r.reason ? `: ${esc(r.reason)}` : ""}</div>`;
  }
  const offered = r.offered || [], ticked = new Set(r.ticked || []);
  // #5840: a request for sign-ins offers its sites only; OK saves their cookies from the browser and grants them
  const signins = (r.sites || []).length > 0;
  const more = signins ? [] : (C.credentials || []).map(c => c.name).filter(n => !offered.includes(n));
  return `<div class="cred-req" data-req="${r.id}">
    <div>${signins ? `<b>${esc(r.asked_by)}</b> asks to use your sign-ins of these sites for ${task}`
      : `<b>${esc(r.proposed_by || "MaximizePM")}</b> asks you to allow credentials for ${esc(r.asked_by)} (${task})`}</div>
    ${r.reason ? `<div class="muted t-s">Reason: ${esc(r.reason)}</div>` : ""}
    <div class="cred-picks">${offered.map(n => pick(r.id, n, ticked.has(n))).join("")}</div>
    ${more.length ? `<details class="t-s"><summary>More credentials</summary><div class="cred-picks">`
      + more.map(n => pick(r.id, n, false)).join("") + `</div></details>` : ""}
    <div class="actions">${button("OK", { kind: "primary", data: { "req-ok": r.id } })}
      ${button("Refuse", { data: { "req-refuse": r.id } })}</div>
  </div>`;
}

function credRow(r) {
  const where = r.kind === "signin" ? [r.site, r.account].filter(Boolean).map(esc).join(" · ") : r.var ? esc(r.var) : "";
  const used = r.used_at ? `used ${ago(r.used_at)}${r.used_by ? " by " + esc(r.used_by) : ""}` : "never used";
  const levels = (C.levels || []).map(l => `<option${l === r.level ? " selected" : ""}>${esc(l)}</option>`).join("");
  const asks = ASKS.map(([v, text]) => `<option value="${v}"${v === (r.ask || "ask") ? " selected" : ""}>${text}</option>`).join("");
  return `<div class="cred">
    <div class="cred-main"><b>${esc(r.name)}</b> ${chip("c-p", esc(KIND[r.kind] || r.kind || "token"))}
      ${(r.sets || []).map(s => chip("c-goal", esc(s), "In this set")).join(" ")}
      ${r.fail_why ? chip("c-human", "sign-in failed", "Last sign-in failed") : ""}
      <div class="muted t-s">${where ? where + " · " : ""}${used}${r.fail_why ? " · " + esc(r.fail_why) : ""}</div></div>
    <div class="cred-sel">
      <label class="cred-level t-s">When asked <select data-cred-ask="${esc(r.name)}" title="When a session asks">${asks}</select></label>
      <label class="cred-level t-s">Level <select data-cred-level="${esc(r.name)}" title="Who may use it">${levels}</select></label>
      ${r.kind === "signin" ? button("Change password", { data: { "cred-pw": r.name } })
        : r.kind === "cookies" ? button("Sign in again", { data: { "cred-renew": r.name } }) : ""}
    </div>
    ${editing && editing.name === r.name ? editBox(r) : ""}
  </div>`;
}

// #5849 (mark 2026-11-14: 'How do we handle it when those credentials need to be updated?'). Change password: the new
// one replaces the old in place (name, sets, level, 'When asked' and grants stay) and a stopped fill may run again.
// Sign in again: the site opens in a session's browser; the person signs in there; Save the new sign-in replaces the
// saved one in place.
function editBox(r) {
  if (r.kind === "signin") {
    return `<div class="cred-edit form">
      <label>New password for ${esc(r.account || r.name)}<input id="credNewPw" type="password" autocomplete="new-password"></label>
      <div class="actions">${button("Save password", { kind: "primary", data: { "cred-pw-save": r.name } })}
        ${button("Cancel", { data: { "cred-edit-cancel": 1 } })}</div></div>`;
  }
  const list = C.browsers || [];
  if (!list.length) {
    return `<div class="cred-edit muted t-s">No session browser runs now. Start a session (or open its Browser), then
      press Sign in again. ${button("Cancel", { data: { "cred-edit-cancel": 1 } })}</div>`;
  }
  const pick = editing.session || (list.find(b => b.session === r.used_by) || list[0]).session;
  const opts = list.map(b => `<option value="${esc(b.session)}|${esc(b.project)}"${b.session === pick ? " selected" : ""}>`
    + `${esc(b.session)} · ${esc(b.project)}</option>`).join("");
  return `<div class="cred-edit form">
    <label>Sign in to ${esc(r.site)} in the browser of<select id="credRenewBrowser">${opts}</select></label>
    <div class="muted t-s">${editing.opened ? `Sign in to ${esc(r.site)} in that session's Browser, then press Save the new sign-in.`
      : "Open the sign-in page: it shows in that session's Browser on the Sessions tab."}</div>
    <div class="actions">${button("Open the sign-in page", { kind: editing.opened ? "" : "primary", data: { "cred-renew-open": r.name } })}
      ${button("Save the new sign-in", { kind: editing.opened ? "primary" : "", data: { "cred-renew-save": r.name } })}
      ${button("Cancel", { data: { "cred-edit-cancel": 1 } })}</div></div>`;
}

async function editClick(t) {
  const row = (name) => (C.credentials || []).find(c => c.name === name);
  if (t.dataset.credPw || t.dataset.credRenew) {
    const name = t.dataset.credPw || t.dataset.credRenew;
    editing = editing && editing.name === name ? null : { name };
    return draw(), true;
  }
  if (t.dataset.credEditCancel) { editing = null; const f = $("#credNewPw"); if (f) f.value = ""; return draw(), true; }
  if (t.dataset.credPwSave) {
    const f = $("#credNewPw");
    if (!f || !f.value) return toast("Type the new password", true), true;
    const value_b64 = b64(f.value);
    f.value = "";  // the password leaves the page now; nothing shows it again
    try { await act("secret", { action: "signin", name: t.dataset.credPwSave, value_b64 }); }
    catch (err) { return true; }
    toast(`Saved the new password of ${t.dataset.credPwSave}`);
    editing = null;
    renderCredentials(true);
    return true;
  }
  if (t.dataset.credRenewOpen || t.dataset.credRenewSave) {
    const name = t.dataset.credRenewOpen || t.dataset.credRenewSave, r = row(name);
    const [session, project] = ($("#credRenewBrowser") || {}).value ? $("#credRenewBrowser").value.split("|") : [];
    if (!r || !session) return toast("Choose the browser", true), true;
    editing = { ...editing, session };
    if (t.dataset.credRenewOpen) {
      try {
        await nav.openSession(session);
        await openBrowser(session, `https://${r.site}/`);
      } catch (err) { return toast(`The browser did not open: ${err.message}`, true), true; }
      editing = { name, session, opened: true };
      toast(`Sign in to ${r.site} in the Browser of ${session}, then come back to Settings and press Save the new sign-in`);
      return true;
    }
    try {
      const out = await act("secret", { action: "cookies", name, site: r.site, session, agent: session, project });
      toast(`Saved the new sign-in ${name}: ${out.cookies} cookies of ${out.site}`);
    } catch (err) { return true; }
    editing = null;
    renderCredentials(true);
    return true;
  }
  return false;
}

// #5842 (mark 2026-11-14): one plain setting per credential. Ask me: the person answers each request for it (the
// checklist or the sign-in modal). Allow without asking: the manager grants it with no question.
const ASKS = [["ask", "Ask me"], ["allow", "Allow without asking"]];

function setRow(s) {
  const grants = (C.grants || []).filter(g => g.set === s.name);
  const scopes = (C.scopes || []).map(x => `<option>${esc(x)}</option>`).join("");
  return `<div class="cred-set">
    <div><b>${esc(s.name)}</b>
      <span class="muted t-s">${s.secrets.length ? s.secrets.map(esc).join(", ") : "holds nothing yet"}</span></div>
    <div class="cred-grants">${grants.map(g => `<span class="cred-grant">${chip("c-ready", esc(g.covers || `${g.scope} ${g.value}`))}`
      + `${g.until ? `<span class="muted t-s"> until ${esc(g.until.slice(0, 10))}</span>` : ""}`
      // A data key is kebab-case: HTML lowers an attribute's name, so data-credRevoke would read as dataset.credrevoke.
      + iconButton("✕", `Revoke ${g.grant}`, { data: { "cred-revoke": g.id } }) + `</span>`).join("")
      || `<span class="muted t-s">No grant: no session can use it.</span>`}</div>
    <div class="cred-grantform" data-set="${esc(s.name)}">
      <select class="cred-scope" title="Grant to what">${scopes}</select>
      <input class="cred-value" placeholder="project, goal, kind, #task or agent" list="credChoices-${esc(s.name)}">
      <datalist id="credChoices-${esc(s.name)}">${choices(C.scopes ? C.scopes[0] : "project")}</datalist>
      <input class="cred-until" placeholder="until (optional: 7d, 2026-12-01)">
      ${button("Grant", { data: { "cred-grant": s.name } })}
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
  ${browserForm()}
  <div class="cred-sub">New set</div>
  <div class="form cred-form">
    <label>Name<input id="setName" placeholder="stripe-live" autocomplete="off"></label>
    <div class="actions">${button("Make set", { id: "credMakeSet" })}</div>
  </div>`;
}

// #5840: the sign-in that the person made in a session's Browser pane (its cookies of one site) goes into a set from
// here, as maxpm secret cookies does in a terminal. The list holds the session browsers that serve runs now. A set
// name that does not exist yet makes the set.
function browserForm() {
  const list = C.browsers || [];
  if (!list.length) return `<div class="cred-sub">Save a browser's sign-in</div>`
    + `<div class="muted t-s">No session browser runs now. Sign in to the site in a session's Browser pane first.</div>`;
  const opts = list.map(b => `<option value="${esc(b.session)}|${esc(b.project)}">${esc(b.session)} · ${esc(b.project)}</option>`).join("");
  const sets = (C.sets || []).map(s => `<option value="${esc(s.name)}">`).join("");
  return `<div class="cred-sub">Save a browser's sign-in</div>
  <div class="form cred-form" id="credCookies">
    <div class="two"><label>Browser of<select id="ckBrowser">${opts}</select></label>
      <label>Site<input id="ckSite" placeholder="google.com" autocomplete="off"></label></div>
    <div class="two"><label>Name<input id="ckName" placeholder="gmail-mark" autocomplete="off"></label>
      <label>Set<input id="ckSet" placeholder="a set, new or old" list="ckSets" autocomplete="off"></label></div>
    <datalist id="ckSets">${sets}</datalist>
    <div class="actions">${button("Save browser sign-in", { kind: "primary", id: "ckSave" })}</div>
  </div>`;
}

async function saveCookies() {
  const name = $("#ckName").value.trim(), site = $("#ckSite").value.trim(), set = $("#ckSet").value.trim();
  const [session, project] = $("#ckBrowser").value.split("|");
  if (!name || !site) return toast("Fill in the site and the name", true);
  try {
    const r = await act("secret", { action: "cookies", name, site, session, agent: session, project, set: set || null });
    toast(`Saved ${name}: ${r.cookies} cookies of ${r.site}` + (set ? ` in ${set}` : ""));
  } catch (err) { return; }
  ["#ckName", "#ckSite", "#ckSet"].forEach(s => { $(s).value = ""; });
  renderCredentials(true);
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
  if (t.dataset.credAsk) {
    const text = (ASKS.find(([v]) => v === t.value) || [])[1];
    try { await act("secret", { action: "asking", name: t.dataset.credAsk, ask: t.value }); toast(`${t.dataset.credAsk}: ${text}`); }
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
  if (await editClick(t)) return;  // #5849: Change password, Sign in again
  if (t.dataset.reqOk || t.dataset.reqRefuse) {  // #5833: the person's answer to a checklist
    const id = +(t.dataset.reqOk || t.dataset.reqRefuse);
    const only = [...panel.querySelectorAll(`input[data-req-pick="${id}"]:checked`)].map(i => i.value);
    if (t.dataset.reqOk && !only.length) return toast("Tick the credentials to allow, or press Refuse", true);
    try {
      if (t.dataset.reqOk) {
        await act("secret", { action: "approve", request: id, only });
        toast(`Allowed ${only.join(", ")}`);
      } else {
        await act("secret", { action: "refuse", request: id });
        toast("Refused");
      }
    } catch (err) { return; }
    return renderCredentials(true);
  }
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
  if (t.id === "ckSave") return saveCookies();
  if (t.id === "credMakeSet") {
    const name = $("#setName").value.trim();
    if (!name) return toast("Type the set's name", true);
    try { await act("secret", { action: "set", name }); toast(`Made the set ${name}`); }
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
