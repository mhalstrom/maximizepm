# The parts of the page

Read this before you change the page. A view (`views/*`) builds its elements from these parts and
colors and sizes from the tokens of `app.css`. It makes no button, dialog, color or px size of its
own. `tests/test_ui_parts.py` checks this. Its allow list holds what the views had when the guard
came (#5028). The list only goes down: when you move a view to the parts, set the lower number.

## Which part for what

| Element | Part | Notes |
|---|---|---|
| Button | `button.js`: `button(html, {kind, cls, title, data, pressed})`, `buttonEl(text, ...)` | kind `""`, `primary`, `warn`; CSS `.btn`, `.primary`, `.warn` |
| Icon button (✕ ↑ ↓ ⋯) | `button.js`: `iconButton(symbol, label, ...)`, or `kind: "icon"` | CSS `.iconbtn`; a label for screen readers |
| Toggle (pin, filter) | `button(..., {pressed})` | `aria-pressed` and the class `on` |
| Chip, badge | `chip.js`: `chip`, `statusChip`, `prioChip`, `dueChip`, `doerChip`, ... | colors from `.c-*` classes |
| Status of a session | `.dot.active|away|gone` (app.css); `cacheBadge.js` for an expert's cache | |
| Page tabs | the header `nav` of `index.html` (app.js) | one per view |
| List row of an item | `itemRow.js` | the one order of items too |
| Card | `card.js`: `nyCard`, `goalCard`, project cards | |
| Progress | `bar.js` | |
| Dialog | `dialog.js`: `makeDialog(el, {onClose, draft})` | adds the class `dlg`: overlay, box, Escape, outside click, focus back (#5027). Set only `--dlg-w`, `--dlg-z` |
| Sheet (phone) | the same dialog: under 760 px `.dlg` is the full width from the top (phone.css) | |
| Drawer | `drawer.js` (the item drawer) | |
| Side panel | `sidePanel.js` (Sessions tab), `itemView.js` for an item in it | |
| Menu | none yet: the phone header menu (phone.css `.hdr-menu`) and the session head ⋯ (`sessions.js` `headMore`) | add `menu.js` before a third menu |
| Toast | `lib.js`: `toast(text, err)` | |
| Form field | `form.js`; `folderForm.js` for a project folder | classes `.form`, `.two`, `label`, `.actions` |
| Long text on a phone | `clamp.js` | |
| Empty list | `empty.js`: `empty(html, {inline})` | "No open items." |
| Table | `table.js` (Tabulator) | |
| Chat | `chat.js` | |

## Tokens (app.css `:root`)

- Colors: `--bg --panel --ink --muted --line --accent --accent-ink`, the states `--ready --prog --wait
  --done --warn --human` and their `-bg`, `--bubble`. Each has a dark value; never write a hex
  color in a view. `--scrim` (behind a dialog) and `--shadow-dlg` are the same in both.
- Space: `--sp-0` 2 px, `--sp-1` 4, `--sp-2` 6, `--sp-3` 8, `--sp-4` 10, `--sp-5` 12, `--sp-6` 16,
  `--sp-7` 24.
- Text: `--fs-xs` 11 px, `--fs-s` 12, `--fs-m` 13, `--fs-base` 14, `--fs-l` 17.
- Corners: `--r-s` 6 px, `--r-m` 8, `--r-l` 10, `--r-xl` 12, `--r-pill`; a line: `--hair` (1 px).
- `--tap`: 44 px, the least size of a control on a phone.

A media query cannot read a token: write its width as a number. The page uses 600, 640, 800 and
1000 px (app.css), and 759 (a phone) and 1023 px (phone.css).

## Add a part

A part is a file here with a comment at its top that says what it is for. It takes plain data and
returns HTML text (escaped with `esc`) or an element. It uses tokens, no literal sizes or colors.
Add its row to the table above. Then move each view that drew the element itself to the part.
