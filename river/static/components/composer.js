// The message box of a chat (#5078, mark: 'the bottom where we write messages ... looks terrible'), in the form of
// iMessage: a round + that attaches files on the left, then one pill field. The pill holds the text, the microphone
// while the field is empty, and a round send arrow when it has text (or files). Enter sends, as before.
// The icons are line drawings (stroke currentColor, round caps), like END_SVG in endSession.js: no emoji.
// The form keeps the ids that the view's code reads (<prefix>Text, Attach, Mic, Send, PickFiles); the view toggles
// the class has-text on the form (composerState) when the field has text or files.
const svg = (d, w = 1.6) => `<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="${w}" stroke-linecap="round" stroke-linejoin="round" d="${d}"/></svg>`;
export const PLUS_SVG = svg("M8 3.2v9.6M3.2 8h9.6", 1.7);
export const MIC_SVG = svg("M8 1.8a2.4 2.4 0 0 1 2.4 2.4v3.2a2.4 2.4 0 0 1-4.8 0V4.2A2.4 2.4 0 0 1 8 1.8zM3.8 7.4a4.2 4.2 0 0 0 8.4 0M8 11.6v2.6M5.9 14.2h4.2");
export const SEND_SVG = svg("M8 12.8V3.6M4.2 7.4L8 3.6l3.8 3.8", 2);

export function composerHtml(prefix, { placeholder, label, attachTitle, sendTitle }) {
  return `<input id="${prefix}PickFiles" type="file" multiple hidden>` +
    `<button id="${prefix}Attach" class="cmp-plus" type="button" title="${attachTitle}" aria-label="Attach files">${PLUS_SVG}</button>` +
    `<div class="cmp-pill" data-mic="own"><textarea id="${prefix}Text" rows="1" placeholder="${placeholder}" aria-label="${label}"></textarea>` +
    `<button id="${prefix}Mic" class="cmp-mic hidden" type="button" aria-pressed="false" aria-label="Dictate">${MIC_SVG}</button>` +
    `<button id="${prefix}Send" class="cmp-send" type="submit" title="${sendTitle}" aria-label="Send">${SEND_SVG}</button></div>`;
}

// The field has text or files: the send arrow takes the microphone's place (a running dictation keeps its button).
export function composerState(form, hasText) {
  form.classList.toggle("has-text", !!hasText);
}
