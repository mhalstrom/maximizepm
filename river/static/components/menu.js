// Menus (#5044): a button (⋯, or the name of the tab on a phone) that shows a group of controls and hides it again.
// While it is open, its host has a class (the CSS of the view shows the group by that class) and the button has
// aria-expanded="true". Esc closes it, and so does a choice in it: a click on a button or a link inside one of the
// closeOn elements, and so does a click outside the outside element (a card under its button) and the button. Menus of the same group are open one at a time (the header of a phone: ⋯ and the sheet of tabs).
// The page's menus: the header ⋯ and the tab sheet (app.js, phone.css .hdr-menu), the session head ⋯ (sessions.js,
// #5115: a card under the ⋯, sessions.css .sess-menu).
const groups = new Map();

export function makeMenu(btn, host, { cls, group = null, onChange = () => {}, closeOn = [], outside = null }) {
  const isOpen = () => host.classList.contains(cls);
  const menu = { isOpen, set };
  function set(open) {
    open = !!open;
    if (open && group) for (const other of groups.get(group)) if (other !== menu && other.isOpen()) other.set(false);
    host.classList.toggle(cls, open);
    btn.setAttribute("aria-expanded", String(open));
    onChange(open);
  }
  if (group) { if (!groups.has(group)) groups.set(group, []); groups.get(group).push(menu); }
  btn.addEventListener("click", () => set(!isOpen()));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && isOpen()) set(false); });
  if (outside) document.addEventListener("pointerdown", (e) => {
    if (isOpen() && !outside.contains(e.target) && !btn.contains(e.target)) set(false);
  });
  for (const el of closeOn) el.addEventListener("click", (e) => { if (e.target.closest("button, a")) setTimeout(() => set(false)); });
  return menu;
}
