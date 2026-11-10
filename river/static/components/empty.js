// Empty states (#5028): the one line a list shows when it has nothing ("No open items."). html is already escaped;
// inline: a span, for a place inside a line or a card.
export function empty(html, { inline = false } = {}) {
  return inline ? `<span class="muted">${html}</span>` : `<div class="muted">${html}</div>`;
}
