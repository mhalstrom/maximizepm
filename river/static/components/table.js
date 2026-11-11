// Tables: one Tabulator table (vendor/tabulator, loaded by index.html as the global Tabulator)
// from columns and rows. Every column sorts on a header click and filters in the header;
// the sort and the filters are remembered per table in this browser.
import { store } from "../lib.js";

// Above this many changed rows, set() replaces the data in one step.
const BULK = 50;

function saved(key) { try { return JSON.parse(store("river.table." + key) || "{}"); } catch (e) { return {}; } }

// A column is a Tabulator column definition plus:
//   filter: "text" (default: a text box, matches part of the value), "select" (a list of the
//           column's values), or false;
//   html:   (row) => HTML for the cell, for example a chip from chip.js; the column still sorts
//           and filters on its field.
function column({ filter = "text", html, ...c }) {
  const col = { headerSort: true, ...c };
  if (html) col.formatter = (cell) => html(cell.getData());
  if (filter === "select") Object.assign(col, { headerFilter: "list", headerFilterFunc: "=",
    headerFilterParams: { valuesLookup: "active", clearable: true, sort: "asc" },
    headerFilterPlaceholder: "filter" });  // the same word in every filter field, a list or a text box (#5328)
  else if (filter === "text") Object.assign(col, { headerFilter: "input", headerFilterPlaceholder: "filter" });
  return col;
}

// A cell value with no text, or only a muted placeholder ('anyone'), says nothing under a collapsed row.
function hasValue(v) {
  if (v instanceof Node) return !!v.textContent.trim();
  if (v == null || v === false) return false;
  const t = document.createElement("template");
  t.innerHTML = String(v);
  t.content.querySelectorAll(".muted").forEach((m) => m.remove());
  return !!t.content.textContent.trim();
}

// The columns that do not fit on a phone, under their row: a label and its value, only for the columns with a value.
function collapsed(cols) {
  const rows = cols.filter((c) => hasValue(c.value));
  if (!rows.length) return "";
  const table = document.createElement("table");
  for (const c of rows) {
    const tr = table.insertRow(), th = tr.insertCell(), td = tr.insertCell(), b = document.createElement("strong");
    b.textContent = c.title;
    th.append(b);
    if (c.value instanceof Node) td.append(c.value); else td.innerHTML = c.value;
  }
  return table;
}

// makeTable(el, { key, columns, index, sort, onRow, placeholder, height, ...options }) returns { set(rows), tabulator };
// options go to Tabulator as they are (for example groupBy and groupHeader).
//   key:    names the table for the remembered sort and filters;
//   index:  the field that names a row (default "id"): set() updates rows in place by it;
//   sort:   the first sort, as [{ column: field, dir: "asc" | "desc" }], until the person picks one;
//   onRow:  (row) => ... when someone clicks a row outside its links, buttons and inputs.
// Call set(rows) on every refresh: the same rows do nothing, and changed rows update in place,
// so the sort, the filters, the scroll position and any selection stay.
export function makeTable(el, { key, columns, index = "id", sort = [], onRow, placeholder = "Nothing here.", height, ...options } = {}) {
  const keep = saved(key);
  const t = new window.Tabulator(el, {
    index, height, placeholder, data: [], layout: "fitColumns",
    // A phone (#4885): the columns that do not fit go under their row, open, so the table is no wider than the screen.
    // Under the row it lists only the columns that have a value (#5085): no 'Due', 'Waits on' or 'Goals' with nothing.
    ...(window.innerWidth < 760 ? { responsiveLayout: "collapse", responsiveLayoutCollapseStartOpen: true,
                                    responsiveLayoutCollapseFormatter: collapsed } : {}),
    // Without a fixed height the table grows with its rows: draw them all (no virtual scrolling).
    renderVertical: height ? "virtual" : "basic", ...options,
    columns: columns.map(column),
    initialSort: keep.sort || sort,
    initialHeaderFilter: keep.filter || [],
  });
  const built = new Promise((r) => t.on("tableBuilt", r));
  const remember = () => store("river.table." + key, JSON.stringify({
    sort: t.getSorters().map((s) => ({ column: s.field, dir: s.dir })),
    filter: t.getHeaderFilters().map((f) => ({ field: f.field, value: f.value })),
  }));
  built.then(() => { t.on("dataSorted", remember); t.on("dataFiltered", remember); });
  // A table built while hidden (another tab, a folded card) has no height: redraw it when it shows.
  let shown = el.offsetWidth > 0;
  new ResizeObserver(() => {
    const now = el.offsetWidth > 0;
    if (now && !shown) built.then(() => t.redraw(true));
    shown = now;
  }).observe(el);
  if (onRow) t.on("rowClick", (e, row) => { if (!e.target.closest("a, button, input, select, textarea, .link")) onRow(row.getData()); });

  let last = null, loaded = false, seen = new Map();
  async function set(rows) {
    const sig = JSON.stringify(rows);
    if (sig === last) return;
    last = sig;
    await built;
    const now = new Map(rows.map((r) => [r[index], JSON.stringify(r)]));
    const before = seen; seen = now;
    if (!loaded) { loaded = true; return t.setData(rows); }
    const gone = [...before.keys()].filter((id) => !now.has(id));
    const changed = rows.filter((r) => before.get(r[index]) !== now.get(r[index]));
    // Many rows in or out (a box that adds or removes the done items, #4773): one replace, which keeps the sort, the
    // filters and the scroll. Row by row, each change lays out the whole table again: thousands of rows took minutes.
    if (gone.length + changed.length > BULK) return t.replaceData(rows);
    if (gone.length) t.deleteRow(gone);
    await t.updateOrAddData(changed);
    // A cell's formatter may read fields that have no column (the item behind a chip): redraw the whole row.
    for (const r of changed) { const row = t.getRow(r[index]); if (row) row.reformat(); }
    // Changed and new rows take their place in the current sort and filters.
    t.setSort(t.getSorters().map((s) => ({ column: s.field, dir: s.dir })));
    t.refreshFilter();
  }
  return { set, tabulator: t };
}
