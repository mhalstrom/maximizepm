// The list under a field that suggests as the person types (#5635: the address bar of the Browser pane). It opens
// under the field with the rows that load(text) gives; Down and Up move in it, Enter on a row that Down or Up chose
// picks that row (Enter with no row chosen is the field's own Enter: the form submits the typed text), Esc closes it
// until the text changes, and a click picks a row. An answer that comes after a newer key is dropped.
//
// makeSuggest(input, host, { load, pick, label }): host is a box with position: relative that holds the field; the list
// goes into it. load(text) returns a Promise of [{html, value}] (html is escaped already). pick(value) runs for the
// chosen row. Returns { close, isOpen }.
let seq = 0;

export function makeSuggest(input, host, { load, pick, label = "Suggestions" }) {
  const id = "sugg" + (++seq);
  const box = document.createElement("div");
  box.className = "sugg-list hidden";
  box.id = id;
  box.setAttribute("role", "listbox");
  box.setAttribute("aria-label", label);
  host.append(box);
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-controls", id);
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-expanded", "false");

  let rows = [], at = -1, asked = 0, closedFor = null;
  const isOpen = () => !box.classList.contains("hidden");
  function close() {
    box.classList.add("hidden");
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    rows = []; at = -1;
  }
  function draw() {
    box.innerHTML = rows.map((r, i) =>
      `<div class="sugg-row${i === at ? " on" : ""}" role="option" id="${id}-${i}" aria-selected="${i === at}" data-i="${i}">${r.html}</div>`).join("");
    if (at >= 0) { input.setAttribute("aria-activedescendant", `${id}-${at}`); box.children[at].scrollIntoView({ block: "nearest" }); }
    else input.removeAttribute("aria-activedescendant");
  }
  async function update() {
    const text = input.value;
    if (!text.trim() || document.activeElement !== input) { closedFor = null; return close(); }
    if (closedFor === text) return;
    closedFor = null;
    const mine = ++asked;
    let got = [];
    try { got = await load(text); } catch (e) { got = []; }
    if (mine !== asked || input.value !== text || document.activeElement !== input) return;
    rows = Array.isArray(got) ? got : [];
    at = -1;
    if (!rows.length) return close();
    box.classList.remove("hidden");
    input.setAttribute("aria-expanded", "true");
    draw();
  }
  function choose(i) {
    const r = rows[i];
    close();
    if (r) pick(r.value);
  }
  input.addEventListener("keydown", (e) => {
    if (e.isComposing || e.altKey || e.ctrlKey || e.metaKey) return;
    if (!isOpen()) {
      if (e.key === "ArrowDown" && input.value.trim()) { e.preventDefault(); closedFor = null; update(); }
      return;
    }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const n = rows.length;
      at = e.key === "ArrowDown" ? (at + 1) % n : (at <= 0 ? n - 1 : at - 1);
      draw();
    } else if (e.key === "Escape") {
      e.preventDefault(); e.stopPropagation();
      closedFor = input.value; close();
    } else if (e.key === "Enter" && at >= 0) {
      e.preventDefault(); choose(at);
    } else if (e.key === "Enter") close();
  });
  input.addEventListener("input", update);
  input.addEventListener("blur", close);
  // A press on a row keeps the focus in the field (a blur would close the list before the click).
  box.addEventListener("mousedown", (e) => e.preventDefault());
  box.addEventListener("click", (e) => { const row = e.target.closest(".sugg-row"); if (row) choose(Number(row.dataset.i)); });
  return { close, isOpen };
}
