// Archive a project (#5197): the confirm dialog behind Archive on a project of Plan's Items view. It says what
// archiving does before the person does it: the project's open items leave the queue (no session is offered them)
// and the lists of the page; its goals, done items and settings stay with it; nothing is deleted. Archived projects
// at the foot of Plan's left bar lists it, with Restore (op project_restore), which brings it back with its items.
import { esc, act, toast } from "../lib.js";
import { makeDialog } from "./dialog.js";
import { button } from "./button.js";

let dlg = null, box = null, done = null;
function finish(v) { const r = done; done = null; if (r) r(v); }
function el() {
  if (dlg) return;
  const d = document.createElement("div");
  d.id = "archDlg"; d.className = "hidden";
  d.innerHTML = '<div class="box arch-box" role="dialog" aria-modal="true" aria-labelledby="archTitle"></div>';
  document.body.appendChild(d);
  box = d.firstElementChild;
  dlg = makeDialog(d, { onClose: () => finish(false), draft: false });
  d.addEventListener("click", (e) => {
    if (e.target === d) return;  // the overlay: makeDialog closes it
    if (e.target.closest("[data-arch-cancel]")) return dlg.close();
    if (e.target.closest("[data-arch-go]")) { finish(true); dlg.close(); }
  });
}

const n = (k, one, many) => `${k} ${k === 1 ? one : many}`;

// The text and buttons of the dialog. items: the project's items in the state (done ones may be missing).
export function archiveHtml(name, items) {
  const open = items.filter(i => !["done", "dropped"].includes(i.status));
  const held = open.filter(i => ["in_progress", "held"].includes(i.status));
  const who = [...new Set(held.map(i => i.assignee).filter(Boolean))];
  return `<h2 id="archTitle">Archive ${esc(name)}?</h2>
    <p>${open.length ? `Its ${n(open.length, "open item leaves", "open items leave")} the queue: no session is offered ${open.length === 1 ? "it" : "them"},
      and the Plan tab and the lists no longer show ${open.length === 1 ? "it" : "them"}.` : "It has no open items."}
      The project leaves the projects list, and the other projects move up one rank.</p>
    ${held.length ? `<p>${n(held.length, "item is", "items are")} in progress${who.length ? ` (${esc(who.join(", "))})` : ""}: ${held.length === 1 ? "its session keeps it" : "their sessions keep them"}
      until ${held.length === 1 ? "it finishes or releases it" : "they finish or release them"}.</p>` : ""}
    <p><b>Nothing is deleted.</b> Its items, goals, history and settings stay. Archived projects, at the foot of the projects
      list, shows it; Restore brings it back with its open items.</p>
    <div class="actions">${button("Cancel", { data: { "arch-cancel": "1" } })}${button(`Archive ${esc(name)}`, { kind: "warn", data: { "arch-go": "1" } })}</div>`;
}

// Ask; resolves to true when the person confirmed.
export function confirmArchive(name, items) {
  el();
  box.innerHTML = archiveHtml(name, items);
  if (done) finish(false);
  dlg.open();
  box.querySelector("[data-arch-cancel]").focus();
  return new Promise(r => { done = r; });
}

// Ask, then archive. Resolves to true when the project was archived.
export async function archiveProject(S, name) {
  if (!await confirmArchive(name, S.items.filter(i => i.project === name))) return false;
  try { await act("project_archive", { name }); } catch (_) { return false; }
  toast(`Archived ${name}. Archived projects, at the foot of the projects list, can restore it`);
  return true;
}

// Bring an archived project back. Resolves to true when it is back.
export async function restoreProject(name) {
  let r;
  try { r = await act("project_restore", { name }); } catch (_) { return false; }
  toast(`${name} is back, at rank ${r.rank}, with ${n(r.open_items, "open item", "open items")}`);
  return true;
}
