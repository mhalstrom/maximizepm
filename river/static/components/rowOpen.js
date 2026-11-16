// One look for every row that a click opens (#6016, mark 2026-11-15: 'It needs to be more obvious that you can click
// on the items and the action items table'). The row gets the class clickrow (app.css: a pointer cursor, a hover
// background, its title in the link color) and ends with OPEN_MARK, which shows without hover, also on a phone.
// A table with onRow gets the same through makeTable (table.js): the class tbl-click and openColumn as its last column.
export const OPEN_MARK = `<span class="row-open" aria-hidden="true">›</span>`;

// The last column of a table whose rows open (Tabulator): never folded under its row on a phone (responsive 0).
export const openColumn = { title: "", field: "_open", formatter: () => OPEN_MARK, width: 36, minWidth: 36,
  headerSort: false, resizable: false, hozAlign: "right", responsive: 0, cssClass: "row-open-cell",
  frozen: true };  // at the right edge of the table, also when its columns are wider than the screen
