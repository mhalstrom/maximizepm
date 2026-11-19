// Add a project folder: what `maxpm init` does in a folder, from the page (server folder_add). The same form
// in the setup guide and on the Projects tab. In the desktop app, "Choose folder…" opens the system folder
// picker (window.riverDesktop, from the app's preload script); in a browser, people type or paste the path.
import { esc, act, toast } from "../lib.js";
import { button } from "./button.js";

export function folderForm() {
  const pick = window.riverDesktop && window.riverDesktop.pickFolder;
  return `<div class="folderForm">
    <div class="ff-row">
      ${pick ? `<button class="btn" data-ff-pick="1">Choose folder…</button>` : ""}
      <input data-ff="path" placeholder="${pick ? "or paste the folder's path" : "the folder's full path, for example ~/Projects/shop"}" spellcheck="false">
    </div>
    <div class="ff-row">
      <input data-ff="name" placeholder="project name (default: the folder's name)" spellcheck="false">
      <input data-ff="description" placeholder="what it is, in a few words (helps agents)">
      <button class="btn primary" data-ff-go="1">Add folder</button>
    </div>
    <div class="ff-out sub"></div>
  </div>`;
}

// The name river gives a folder when none is typed (as maxpm init does).
function defaultName(path) {
  const base = path.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || "";
  return base.toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "") || "project";
}

// Once per page. onAdded(result) runs after a folder is added (for example to refresh the setup guide).
export function wireFolderForms(onAdded) {
  const hint = (form, p) => { form.querySelector('[data-ff="name"]').placeholder = `project name (default: ${p ? defaultName(p) : "the folder's name"})`; };
  const setPath = (form, p) => { form.querySelector('[data-ff="path"]').value = p; hint(form, p); };
  // Typing only updates the name hint; it never rewrites what the person types.
  document.addEventListener("input", (e) => {
    if (!e.target.dataset || e.target.dataset.ff !== "path") return;
    const form = e.target.closest(".folderForm");
    hint(form, e.target.value.trim());
    // A new path: the question whether to create the old one no longer applies.
    const out = form.querySelector(".ff-out");
    if (out.querySelector("[data-ff-create]")) out.innerHTML = "";
  });
  // Enter in a field of the form does what Add folder does.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.isComposing || !e.target.dataset || !e.target.dataset.ff) return;
    const go = e.target.closest(".folderForm").querySelector("[data-ff-go]");
    if (go) { e.preventDefault(); go.click(); }
  });
  document.addEventListener("click", async (e) => {
    const form = e.target.closest(".folderForm"); if (!form) return;
    if (e.target.closest("[data-ff-pick]")) {
      const p = await window.riverDesktop.pickFolder();
      if (p) setPath(form, p);
      return;
    }
    const out = form.querySelector(".ff-out");
    if (e.target.closest("[data-ff-cancel]")) { out.innerHTML = ""; return; }
    const go = e.target.closest("[data-ff-go], [data-ff-move], [data-ff-create]"); if (!go || go.disabled) return;
    const v = (k) => form.querySelector(`[data-ff="${k}"]`).value.trim();
    const move = go.dataset.ffMove === undefined ? undefined : go.dataset.ffMove === "1";
    const create = go.dataset.ffCreate !== undefined || undefined;
    go.disabled = true;
    try {
      const r = await act("folder_add", { path: v("path"), name: v("name") || undefined, description: v("description"), move, create });
      // A folder that does not exist (#6236): ask before MaximizePM makes it; Create sends the same fields again.
      if (r.missing) {
        out.innerHTML = `<span class="ff-ask">${esc(r.path)} does not exist. Create it?</span>
          ${button("Create", { kind: "primary", data: { "ff-create": "1" } })}${button("Cancel", { data: { "ff-cancel": "1" } })}`;
        const yes = out.querySelector("[data-ff-create]"); if (yes) yes.focus();
        return;
      }
      // An old CLAUDE.md holds rules of this folder: ask once whether to move them to AGENTS.md, which every agent reads.
      const old = r.layout === "claude_only" || r.layout === "both";
      out.innerHTML = old
        ? `Added <b>${esc(r.project)}</b>. This folder's CLAUDE.md has rules that Codex and other agents do not read.
           <button class="btn" data-ff-move="1">Share them with every agent</button><button class="btn" data-ff-move="0">Keep CLAUDE.md as it is</button>`
        : `Added <b>${esc(r.project)}</b> (${esc(r.path)}). Agents started there now use the queue.`;
      // Keep the fields while the CLAUDE.md choice is open: its buttons send the same folder again.
      if (!(old && move === undefined)) {
        for (const k of ["path", "name", "description"]) form.querySelector(`[data-ff="${k}"]`).value = "";
        if (move !== undefined) out.innerHTML = `Added <b>${esc(r.project)}</b> (${esc(r.path)}). Agents started there now use the queue.`;
      }
      toast(`Added the folder for ${r.project}`);
      if (onAdded) onAdded(r);
    } catch (err) { /* act shows the error */ } finally { go.disabled = false; }
  });
}
