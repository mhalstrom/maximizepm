// Item rows, and the one order items take in a list.
import { esc } from "../lib.js";
import { goalChips, prioChip, dueChip, doerChip, replanChip, statusChip, modelChip, agentChip } from "./chip.js";

// Ready first, then more important, then what unblocks more, then the project's own order.
export function itemOrder(a, b) {
  return (b.ready - a.ready) || (a.effective_priority - b.effective_priority) || (b.unblocks_count - a.unblocks_count) || (a.rank - b.rank);
}

// One item: id, title, an optional line under it (why), and its chips. Open items can be dragged onto an agent.
export function itemRow(it, why) {
  return `<div class="row" data-id="${it.id}"${it.status === "open" ? ' draggable="true"' : ""}>
    <div class="id">#${it.id}</div>
    <div class="t"><div class="title">${esc(it.title)}</div>${it.blocked_reason && it.status === "open" ? `<div class="blk">${esc(it.blocked_text)}</div>` : ""}${why ? `<div class="why">${esc(why)}</div>` : ""}</div>
    <div class="chips">${goalChips(it)}${prioChip(it)}${dueChip(it)}${doerChip(it)}${agentChip(it)}${modelChip(it)}${replanChip(it)}${statusChip(it)}</div></div>`;
}

// Items as table rows (components/table.js): one flat row per item, with plain values to sort and
// filter on, and the item itself (it) for the chips. order: the place in itemOrder, the default sort.
export function itemTableRows(items) {
  return [...items].sort(itemOrder).map((it, order) => ({
    id: it.id, order, title: it.title, it,
    status: it.status === "open" ? (it.backlog ? "backlog" : it.ready ? "ready" : it.blocked_reason ? "blocked" : "waiting") : it.status.replace("_", " "),
    prio: it.effective_priority,
    doer: it.doer === "any" ? "anyone" : it.doer === "human" ? "human" : "agent",
    due: it.effective_due && !["done", "dropped"].includes(it.status) ? it.effective_due : "",
    waits: it.open_blockers.map(b => "#" + b).join(" "),
    goals: (it.goals || []).join(" "),
    model: it.model || "",
  }));
}

// The columns for itemTableRows. why(it): an optional line under the title (notes, output).
export function itemTableColumns(why = () => "") {
  return [
    { title: "", field: "order", visible: false },
    { title: "#", field: "id", width: 60, filter: false },
    { title: "Title", field: "title", minWidth: 200, widthGrow: 3, html: (r) => {
      const it = r.it, w = why(it);
      return `<div class="title">${esc(it.title)}</div>${it.blocked_reason && it.status === "open" ? `<div class="blk">${esc(it.blocked_text)}</div>` : ""}${w ? `<div class="why">${esc(w)}</div>` : ""}`;
    } },
    { title: "Status", field: "status", width: 130, filter: "select", html: (r) => statusChip(r.it) + replanChip(r.it) },
    { title: "P", field: "prio", width: 92, filter: "select", html: (r) => prioChip(r.it) },
    { title: "Who", field: "doer", width: 80, filter: "select", html: (r) => doerChip(r.it) || '<span class="muted">anyone</span>' },
    { title: "Model", field: "model", width: 110, filter: "select", html: (r) => modelChip(r.it) },
    { title: "Due", field: "due", width: 84, filter: false, html: (r) => dueChip(r.it) },
    { title: "Waits on", field: "waits", width: 90, html: (r) => r.it.open_blockers.map(b => `<span class="link" data-open="${b}">#${b}</span>`).join(" ") },
    { title: "Goals", field: "goals", minWidth: 110, widthGrow: 1, cssClass: "wrap", html: (r) => goalChips(r.it) },
  ];
}
