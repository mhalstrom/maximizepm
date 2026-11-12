# The parts of the page

Read this before you change the page. A view (`views/*`) builds its elements from these parts and
colors and sizes from the tokens of `app.css`. It makes no button, dialog, color or px size of its
own. `tests/test_ui_parts.py` checks this. Its allow list holds what the views had when the guard
came (#5028). The list only goes down: when you move a view to the parts, set the lower number.

## Which part for what

| Element | Part | Notes |
|---|---|---|
| Button | `button.js`: `button(html, {kind, cls, title, data, pressed})`, `buttonEl(text, ...)` | kind `""`, `primary`, `warn`; CSS `.btn`, `.primary`, `.warn` |
| Card that is a button | `button.js`: `button(html, {kind: "card", cls})` | CSS `.cardbtn` takes the button look away; the card class (`cls`) draws it (Graph `.gcard`) |
| Icon button (✕ ↑ ↓ ⋯) | `button.js`: `iconButton(symbol, label, ...)`, or `kind: "icon"` | CSS `.iconbtn`; a label for screen readers |
| Toggle (pin, filter) | `button(..., {pressed})` | `aria-pressed` and the class `on` |
| Chip, badge | `chip.js`: `chip`, `statusChip`, `prioChip`, `dueChip`, `doerChip`, ... | colors from `.c-*` classes |
| Status of a session | `.dot.active|away|gone` (app.css); `cacheBadge.js` for an expert's cache | |
| Page tabs | the header `nav` of `index.html` (app.js) | one per area (Sessions, Plan, Terminal); Settings opens from the header ⋯ (`#settingsBtn`, #5116) |
| List row of an item | `itemRow.js` | the one order of items too |
| Card | `card.js`: `nyCard`, `goalCard`, project cards | |
| Progress | `bar.js` | |
| Dialog | `dialog.js`: `makeDialog(el, {onClose, draft})` | adds the class `dlg`: overlay, box, Escape, outside click, focus back (#5027). Set only `--dlg-w`, `--dlg-z` |
| Sheet (phone) | the same dialog: under 760 px `.dlg` is the full width from the top (phone.css) | |
| Drawer | `drawer.js` (the item drawer) | |
| Side panel | `sidePanel.js` (Sessions tab), `itemView.js` for an item in it | |
| A task in the work of its session | `taskContext.js`: `openTask(id, {onGo, onFull})` | a large dialog over the chat from GET /api/item/<id>/context: the session's Done, Now, Next, the task, the goal head; on a phone the task is a sheet at the bottom (#5572). A #id in a chat and the head's task open it |
| Browser of a project | `browserTool.js`: `openBrowser(session, url)`, `browserOpens(list)` | the fixed tab Browser of the side panel (#4631, #5628), where every outside address of the panel opens: frames of serve's Chrome, the person's input, who controls it, the driving session's pointer and keys, Hand to (#5616) |
| Menu (⋯) | `menu.js`: `makeMenu(btn, host, {cls, group, onChange, closeOn, outside})` | a class on the host while it is open, `aria-expanded`, Esc and a choice close it, and a click outside `outside`; one menu of a group at a time. The header ⋯ and the tab sheet (app.js, phone.css `.hdr-menu`), the session head ⋯ (sessions.js; a card `.sess-menu` with rows `.sess-mi`) |
| Archive a project | `archiveProject.js`: `archiveProject(S, name)`, `restoreProject(name)` | the confirm dialog says what archiving does; Restore is in Plan's left bar, Archived projects (#5197) |
| End a session | `endSession.js`: `endButton(s, {icon})`, `endSession(s)` | the confirm dialog: stop request, expert end, End now (#5041) |
| A new release | `releaseUpdate.js`: `showRelease(release, {boot, waitForRestart})`, `openRelease()` | the bar at the foot, "A new version of MaximizePM is available", and its dialog with the notes and one button (#5601); from GET /api/update |
| Toast | `lib.js`: `toast(text, err)` | |
| Form field | `form.js`; `folderForm.js` for a project folder | classes `.form`, `.two`, `label`, `.actions` |
| Empty list | `empty.js`: `empty(html, {inline})` | "No open items." |
| Table | `table.js` (Tabulator) | |
| Message box of a chat | `composer.js`: `composerHtml(prefix, {...})`, `composerState(form, hasText)` | round + to attach, one pill field, the mic while empty, a round send arrow with text; line icons `PLUS_SVG`, `MIC_SVG`, `SEND_SVG` (#5078); `stopTitle` adds Stop, a square (`STOP_SVG`) in the arrow's place while the form has `can-now` and the field is empty (#5587) |
| Chat | `chat.js` | `drawWaiting(host, rows)`: the messages held until the session's turn ends, each with Send now (`data-now`, #5587) |
| List that suggests under a field | `suggestList.js`: `makeSuggest(input, host, {load, pick, label})` | rows from `load(text)` as the person types; Down, Up, Enter on a chosen row, Esc, a click; the Browser pane's address bar (#5635) |
| "/" list of a message box | `slashList.js`: `makeSlashList(input, host, {load, key, onChange})` | the slash commands of the session's agent above the box; Tab completes, Up and Down move, Esc closes; `keydown(e)` says whether it took a key (#5186) |
| Model choice of the launch dialog | `modelPick.js`: `modelChoices(models, current)`, `OTHER` | a long list (OpenCode) shows the adapter's models (`pick`) and the current one; "Other model…" finds the rest by typing (#5238) |
| What an agent supports, its texts | `agents.js`: `loadAgents()`, `agentOf(provider or name)`, `mainAgent()`, `setupOf(p)` | from GET /api/agents (#5184); a view tests no agent name |

## A hover moves nothing

A control that a hover shows (the pin and End of a session row, Copy on a code block) has its place in the row
always: the hover changes only `opacity` or `visibility`, and colors. A `:hover` rule (also `:not(:hover)`) never
sets `display`, a size, a margin or padding, a border width, a font size or weight, or a position: the box beside it
would move (#5106). `tests/test_ui_parts.py` refuses such a rule in any style sheet of the page, and
`PageBrowser.test_a_hover_moves_nothing_on_any_tab_*` (tests/test_page.py) hovers every row, button and chip of
each tab at 1240 and 390 px and fails when another box moves.

## Tokens (app.css `:root`)

- Colors: `--bg --panel --ink --muted --line --accent --accent-ink`, the states `--ready --prog --wait
  --done --warn --human` and their `-bg`, `--bubble`. Each has a dark value; never write a hex
  color in a view. `--scrim` (behind a dialog), `--shadow-dlg`, `--shadow-pop` (a bar over the page), `--frame-bg` (a
  page in a frame), `--find`, `--find-on`, `--find-ink` (the marks of Find), `--flash`, and `--ptr`, `--ptr-edge`, `--ptr-mark` (the pointer of a session in a browser) are the same in both.
- Space: `--sp-0` 2 px, `--sp-1` 4, `--sp-2` 6, `--sp-3` 8, `--sp-4` 10, `--sp-5` 12, `--sp-6` 16,
  `--sp-7` 24.
- Text: `--fs-xs` 11 px, `--fs-s` 12, `--fs-m` 13, `--fs-base` 14, `--fs-l` 17, `--fs-xl` 18, `--fs-2xl` 24 (a big number). For a
  line in a view: the classes `t-s` (12 px) and `t-m` (13 px), not a `style` attribute.
- Corners: `--r-xs` 4 px, `--r-s` 6 px, `--r-m` 8, `--r-l` 10, `--r-xl` 12, `--r-pill`, `--r-bubble` 18 (a chat bubble); a line: `--hair` (1 px),
  `--hair-2` (2 px).
- Widths of a field or a list: `--w-xs` 80 px, `--w-s` 120, `--w-m` 160, `--w-l` 220;
  `--board-top` (150 px): the header and the strip above a Board column.
- A size between two tokens: `calc()` of tokens, for example `calc(var(--sp-2) + var(--hair))` for 7 px.
- `--tap`: 44 px, the least size of a control on a phone.
- Widths and heights of view parts that no space token gives: `--w-input-s` 90 px, `--w-input-m` 180, `--w-form`
  640, `--w-col` 200, `--w-tool` 32, `--w-side` 260, `--w-card` 180, `--w-card-phone` 148, `--h-spark` 56, `--h-bars`
  70, `--w-bar` 60, `--h-box-min` 320, `--h-above-box` 210, `--fade` 40; dialogs `--w-dlg-s` 520 and `--w-dlg-m` 620
  (for `--dlg-w`); the Sessions tab `--w-sess-min` 280 and `--w-sess-max` 360 (the list), `--w-panel` 480 (the side
  panel), `--w-tab` 200, `--w-file` 240, `--w-label` 170, `--w-field-s` 100 and `--w-field-m` 140 (a field in a bar),
  `--h-pre` 300 (a tall box of text or a page), `--h-text-min` 60. A value between two space tokens is a
  `calc()` of tokens (14 px: `calc(var(--sp-5) + var(--sp-0))`); add a named token only for a size of a part.

A media query cannot read a token: write its width as a number. The page uses 600, 640, 800 and
1000 px (app.css), and 759 (a phone) and 1023 px (phone.css).

## Add a part

A part is a file here with a comment at its top that says what it is for. It takes plain data and
returns HTML text (escaped with `esc`) or an element. It uses tokens, no literal sizes or colors.
Add its row to the table above. Then move each view that drew the element itself to the part.
