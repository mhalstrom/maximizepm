// Deploy › Accounts (#6147): one row for each forge token that a pipeline names. MaximizePM asks the forge who the
// token is, when it runs out and which repositories it reaches (river/pipeline/accounts.py, once an hour); a refused
// or missing token says so here, on its pipelines' heads and as "no news" on their stages. The token never shows.
// Add a token (#6148; canvas deploy-tab-mockup picture 8): the form checks the token with its forge and shows who it is
// before it saves it, so a wrong token never goes into the list; or a session makes the token in the project's
// browser ('Make the token for me'): the person signs in and presses Generate there, and the session saves it unseen.
import { esc, ago, act, toast } from "../lib.js";
import { button, iconButton } from "../components/button.js";
import { chip } from "../components/chip.js";
import { empty } from "../components/empty.js";
import { makeDialog } from "../components/dialog.js";
import { field, input, select, actions } from "../components/form.js";

const FORGES = [["github", "GitHub"], ["gitlab", "GitLab"], ["gitea", "Gitea"], ["forgejo", "Forgejo"],
  ["pulumi", "Pulumi Cloud"]];
const NEEDS = {
  github: "A fine-grained token for the repositories of the pipelines: Actions read and write (to start a workflow after a transition), Deployments read, Contents read, Metadata read.",
  gitlab: "A personal or project access token with the scope api. A server of your own: its address.",
  gitea: "A token with repository read and write, and the address of the server.",
  forgejo: "A token with repository read and write, and the address of the server (Codeberg: https://codeberg.org).",
  pulumi: "A personal or organization access token of Pulumi Cloud.",
};

const STATE = {
  ok: ["c-ready", "works"], new: ["c-waiting", "not checked yet"], error: ["c-waiting", "check failed"],
  refused: ["c-warn", "refused"], missing: ["c-warn", "not saved"],
};

// When a token runs out, in words: "in 52 days (2027-01-10)", "ran out 2026-11-17", or "no end date".
export function runsOut(a) {
  if (!a.expires_at) return a.state === "ok" ? "no end date" : "";
  const day = a.expires_at.slice(0, 10);
  if (a.days_left < 0) return `ran out ${day}`;
  return `in ${Math.floor(a.days_left)} day${Math.floor(a.days_left) === 1 ? "" : "s"} (${day})`;
}

function row(a) {
  const [cls, word] = STATE[a.state] || STATE.new;
  const repos = Object.entries(a.repos || {});
  const soon = a.days_left != null && a.days_left <= 7;
  const btn = ["refused", "missing"].includes(a.state)
    ? button(a.state === "refused" ? "Replace" : "Add it", { kind: "primary", data: { "ac-add": a.forge, "ac-name": a.secret || "",
      "ac-url": a.host && !["github.com", "gitlab.com", "api.pulumi.com"].includes(a.host) ? "https://" + a.host : "" }, title: "Save a new token" })
    : button("Check now", { data: { "dp-check": a.secret || "" }, title: "Check the token" });
  return `<tr>
    <td><b>${esc(a.label)}</b> · ${esc(a.owner || "?")}<div class="muted t-s">${esc(a.host)}${a.kind ? " · " + esc(a.kind) : ""}</div></td>
    <td class="mono" data-label="Token">${esc(a.secret || "none named")}</td>
    <td>${chip(cls, word)}${a.state === "ok" && a.read_at ? `<div class="muted t-s">read ${esc(ago(a.read_at))}</div>` : ""}
      ${a.error ? `<div class="t-s c-warn">${esc(a.error)}</div>` : ""}</td>
    <td class="${soon ? "c-warn" : ""}" data-label="Runs out">${esc(runsOut(a)) || `<span class="muted">-</span>`}</td>
    <td class="t-s" data-label="Reaches">${repos.length ? repos.map(([r, ok]) => ok ? esc(r) : `<span class="c-warn">${esc(r)} (not reached)</span>`).join("<br>")
      : `<span class="muted">-</span>`}${a.scopes ? `<div class="muted">${esc(a.scopes)}</div>` : ""}</td>
    <td data-label="Used by">${a.used_by.map(n => button(esc(n), { cls: "plain", data: { "dp-open": n }, title: "Open" })).join(" ")}</td>
    <td>${btn}</td></tr>`;
}

// data: GET api/deploy (its accounts, and its pipelines for the ones that need no token).
export function accountsHtml(data) {
  const accts = data.accounts || [];
  const here = data.pipelines.filter(p => !p.default && p.source.state === "local").map(p => p.name);
  const plugins = data.pipelines.filter(p => !p.default && p.source.plugin).map(p => p.name);
  const add = button("Add token", { kind: "primary", data: { "ac-add": "github" }, title: "Add token" });
  const table = accts.length ? `<div class="pl-card"><div class="pl-bar"><h3>Accounts</h3>${add}</div>
    <div class="pl-table"><table class="pl-blocks">
      <tr><th>Account</th><th>Token</th><th>State</th><th>Expires</th><th>Repos</th><th>Used by</th><th></th></tr>
      ${accts.map(row).join("")}</table></div></div>`
    : `<div class="pl-card"><div class="pl-bar"><h3>Accounts</h3>${add}</div>
      <div class="muted t-s">No tokens saved.</div></div>`;
  const local = here.length ? `<div class="pl-card"><h3>This computer</h3>
    <div class="t-m">${here.map(n => button(esc(n), { cls: "plain", data: { "dp-open": n }, title: "Open" })).join(" ")}</div></div>` : "";
  const plug = plugins.length ? `<div class="pl-card"><h3>Plugins</h3>
    <div class="t-m">${plugins.map(n => button(esc(n), { cls: "plain", data: { "dp-open": n }, title: "Open" })).join(" ")}</div></div>` : "";
  const targets = data.pipelines.filter(p => p.default).map(p => p.name);
  const tgt = targets.length ? `<div class="pl-card"><h3>Deploy targets</h3>
    <div class="t-m">${targets.map(n => button(esc(n), { cls: "plain", data: { "dp-open": n }, title: "Open" })).join(" ")}</div></div>` : "";
  return table + local + plug + tgt || empty("No releases yet.");
}

// ---- Add a token (#6148)

let dlg = null, box = null;

function formHtml(forge, name, url) {
  return `<div class="dlg-head"><h2>Add token</h2>${iconButton("✕", "Close", { data: { "ac-close": 1 } })}</div>
    <div class="form">
      ${field("Git host", select({ id: "acForge" }, FORGES, forge))}
      ${field("Server", input({ id: "acUrl", value: url || "", placeholder: "https://<host>, for a server of your own" }))}
      ${field("Token", input({ id: "acToken", type: "password", autocomplete: "off", spellcheck: "false", placeholder: "paste the token" }))}
      ${field("Name", input({ id: "acName", value: name || "", placeholder: "<host>-<account>, filled in after the check" }))}
      <div class="muted t-s" id="acNeeds">${esc(NEEDS[forge] || "")}</div>
    </div>
    ${actions(button("Cancel", { data: { "ac-close": 1 } }) + button("Check and save", { kind: "primary", data: { "ac-save": 1 }, title: "Check, then save" }))}
    <div class="t-m" id="acResult"></div>
    <div class="ac-or muted t-s">or</div>
    <div>${button("Create in browser", { data: { "ac-make": 1 }, title: "Create in browser" })}</div>`;
}

const val = (id) => (box.querySelector(id).value || "").trim();

function openTokenDialog(forge, name, url) {
  if (!dlg) {
    const d = document.createElement("div");
    d.id = "acTokenDlg"; d.className = "hidden";
    d.innerHTML = '<div class="box"></div>';
    document.body.appendChild(d);
    box = d.firstElementChild;
    dlg = makeDialog(d, { draft: false });
    box.addEventListener("change", (e) => {
      if (e.target.id === "acForge") box.querySelector("#acNeeds").textContent = NEEDS[e.target.value] || "";
    });
    box.addEventListener("click", onClick);
  }
  box.innerHTML = formHtml(forge || "github", name, url);
  dlg.open();
  box.querySelector("#acToken").focus();
}

async function onClick(e) {
  if (e.target.closest("[data-ac-close]")) return dlg.close();
  const args = { forge: val("#acForge"), url: val("#acUrl") || null, name: val("#acName") || null };
  if (e.target.closest("[data-ac-save]")) {
    const token = box.querySelector("#acToken").value.trim();
    if (!token) return toast("Paste the token first", true);
    let r;
    try { r = await act("pipeline_token_add", { ...args, token }); } catch (err) { return; }
    box.querySelector("#acToken").value = "";  // the value leaves the page once, to the forge and the vault
    box.querySelector("#acName").value = r.secret;
    const end = r.expires_at ? `runs out ${r.expires_at.slice(0, 10)}` : "no end date";
    box.querySelector("#acResult").innerHTML = `<span class="c-ready">●</span> ${esc(r.label)} accepts it: ${esc(r.kind || "account")}
      <b>${esc(r.owner || "?")}</b>, ${esc(end)}${r.scopes ? `, scopes ${esc(r.scopes)}` : ""}. ${r.replaced ? "Replaced" : "Saved as"}
      <span class="mono">${esc(r.secret)}</span>${r.used_by.length ? `, used by ${r.used_by.map(esc).join(", ")}` : ""}.`;
    return;
  }
  if (e.target.closest("[data-ac-make]")) {
    if (!args.name) return toast("Name the token first", true);
    let r;
    try { r = await act("pipeline_token_make", args); } catch (err) { return; }
    dlg.close();
    toast(`Task #${r.item}: a session makes the token${r.started ? " now" : ""}. Sign in when its browser asks.`);
  }
}

document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-ac-add]");
  if (t) openTokenDialog(t.dataset.acAdd, t.dataset.acName, t.dataset.acUrl);
});
