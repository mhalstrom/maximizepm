// The permission modal for sign-ins and credentials (#5840, #6905). A session asks for the person's sign-ins of some
// sites (maxpm secret request --signin <sites> --from <session>) or for credentials (maxpm secret request <names>,
// a refused pipe, or the manager's propose); when the request is at the person (state='asked'), the page shows it here
// over whatever tab is open: who asks, for which task, why, each site or credential ticked, and 'Do not ask again'
// (#5842). Allow saves/creates the set and grants the ticked ones (op secret, action approve, only, always); Deny
// refuses. Esc or a click outside closes it for this page load; the request still waits in Settings, Credentials.
import { $, esc, toast, act, getJson } from "../lib.js";
import { button, iconButton } from "../components/button.js";
import { makeDialog } from "../components/dialog.js";

const host = $("#signinDlg");
const dlg = makeDialog(host, { draft: false, onClose: () => { if (shown) later.add(shown); shown = null; shownSignin = false; } });
const later = new Set();  // requests that the person closed without an answer, on this page load
let shown = null, shownSignin = false, reading = false;

// Its line in Needs you (#6002): the person closed the modal before, and opens it again from the list.
export async function openSignin(id) {
  later.delete(id);
  if (shown === id) return;
  await signinAsks([id]);
}

// Each refresh: the first request at the person that this page has not shown or closed opens the modal.
export async function signinAsks(ids) {
  const next = (ids || []).find(id => !later.has(id));
  if (shown && !(ids || []).includes(shown)) { shown = null; shownSignin = false; dlg.close(); }  // answered elsewhere
  if (next === undefined || shown !== null || reading) return;
  reading = true;
  let data, r;
  try {
    data = await getJson("api/credentials");
    r = (data.requests || []).find(x => x.id === next);
  }
  catch (e) { return; }
  finally { reading = false; }
  if (!r) return;
  shown = r.id;
  shownSignin = (r.sites || []).length > 0;
  draw(r, data.credentials || []);
  dlg.open();
  const ok = host.querySelector("[data-signin-ok]");
  if (ok) ok.focus();
}

function where(r) {
  const [project, session] = (r.source || "").split("/");
  return session ? `the browser of ${esc(session)} (${esc(project)})` : `the browser of project ${esc(project)}`;
}

function draw(r, allCreds) {
  const task = r.task ? `${esc(r.covers)}: ${esc(r.task)}` : esc(r.covers);
  if ((r.sites || []).length > 0) {
    const acc = r.accounts || {};
    // #5842: beside each tick, 'Do not ask again for this site': Allow saves that site's sign-in as 'Allow without
    // asking', so the manager grants the next request for it with no modal (Settings, Credentials changes it back).
    const rows = (r.sites || []).map(s => `<div class="signin-site"><label class="cred-check"><input type="checkbox" data-signin-site value="${esc(s)}"`
      + `${(r.ticked || []).includes(s) ? " checked" : ""}> <b>${esc(s)}</b>`
      + `${(acc[s] || []).length ? ` <span class="muted t-s">${acc[s].map(esc).join(", ")}</span>` : ""}</label>`
      + `<label class="cred-check t-s"><input type="checkbox" data-signin-always value="${esc(s)}"> Do not ask again for this site</label></div>`).join("");
    host.querySelector(".box").innerHTML = `
      <div class="signin-head"><h2 id="signinTitle">Allow sign-ins?</h2>${iconButton("✕", "Close", { data: { "signin-close": 1 } })}</div>
      <div class="cred-req">
        <div><b>${esc(r.asked_by)}</b> asks to use your sign-ins for ${task}</div>
        ${r.reason ? `<div class="muted t-s">Reason: ${esc(r.reason)}</div>` : ""}
        <div class="muted t-s">From ${where(r)}. Its browser opens these sites signed in as you, without a password.</div>
        <div class="cred-picks">${rows}</div>
        <div class="actions">${button("Allow", { kind: "primary", data: { "signin-ok": r.id } })}
          ${button("Deny", { data: { "signin-deny": r.id } })}</div>
      </div>`;
    return;
  }
  const offered = r.offered || [], ticked = new Set(r.ticked || []);
  const more = (allCreds || []).map(c => c.name).filter(n => !offered.includes(n));
  const rows = offered.map(n => `<div class="signin-site"><label class="cred-check"><input type="checkbox" data-signin-site value="${esc(n)}"`
    + `${ticked.has(n) ? " checked" : ""}> <b>${esc(n)}</b></label>`
    + `<label class="cred-check t-s"><input type="checkbox" data-signin-always value="${esc(n)}"> Do not ask again</label></div>`).join("");
  const moreRows = more.map(n => `<label class="cred-check"><input type="checkbox" data-signin-site value="${esc(n)}"> ${esc(n)}</label>`).join("");
  const lead = r.proposed_by
    ? `<b>${esc(r.proposed_by)}</b> asks you to allow credentials for ${esc(r.asked_by)} (${task})`
    : `<b>${esc(r.asked_by)}</b> asks to use credentials for ${task}`;
  host.querySelector(".box").innerHTML = `
    <div class="signin-head"><h2 id="signinTitle">Allow credentials?</h2>${iconButton("✕", "Close", { data: { "signin-close": 1 } })}</div>
    <div class="cred-req">
      <div>${lead}</div>
      ${r.reason ? `<div class="muted t-s">Reason: ${esc(r.reason)}</div>` : ""}
      <div class="muted t-s">Allow puts the ticked credentials into a set and grants it to ${esc(r.covers)}.</div>
      <div class="cred-picks">${rows}</div>
      ${more.length ? `<details class="t-s"><summary>More credentials</summary><div class="cred-picks">${moreRows}</div></details>` : ""}
      <div class="actions">${button("Allow", { kind: "primary", data: { "signin-ok": r.id } })}
        ${button("Deny", { data: { "signin-deny": r.id } })}</div>
    </div>`;
}

host.addEventListener("click", async (e) => {
  const t = e.target.closest("button");
  if (!t) return;
  if (t.dataset.signinClose) return dlg.close();
  const id = +(t.dataset.signinOk || t.dataset.signinDeny || 0);
  if (!id) return;
  const only = [...host.querySelectorAll("input[data-signin-site]:checked")].map(i => i.value);
  if (t.dataset.signinOk && !only.length) return toast(shownSignin ? "Tick the sites to allow, or press Deny" : "Tick the credentials to allow, or press Deny", true);
  const always = [...host.querySelectorAll("input[data-signin-always]:checked")].map(i => i.value).filter(s => only.includes(s));
  try {
    if (t.dataset.signinOk) {
      await act("secret", { action: "approve", request: id, only, always });
      toast(`Allowed ${only.join(", ")}`);
    } else {
      await act("secret", { action: "refuse", request: id, note: shownSignin ? "denied in the sign-in modal" : "denied in the credentials dialog" });
      toast("Denied");
    }
  } catch (err) { return; }  // act showed the error; the modal stays for another try
  shown = null;
  shownSignin = false;
  later.add(id);
  dlg.close();
});
