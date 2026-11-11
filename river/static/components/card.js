// Cards: goal cards, project cards, and the .ny cards of targets, blocked items, takeovers and the inbox.
import { esc, left } from "../lib.js";
import { chip, ownerChip } from "./chip.js";
import { bar, pct } from "./bar.js";
import { button } from "./button.js";

// A .ny card: a head line, then the body. cls adds classes (for example "msg read").
export function nyCard(head, body, cls = "") {
  return `<div class="ny${cls ? " " + cls : ""}">
      <div class="ny-h">${head}</div>
      ${body}</div>`;
}

// A goal: owner, progress bar, counts, and its actions; the outcome and the result are its hover (#5082). selected: the goal filter picks it; me: who acts.
export function goalCard(g, { selected, me }) {
  const total = g.items_open.length + g.items_done.length, done = pct(g.items_done.length, total);
  const open = g.status === "open";
  const tip = (g.outcome ? g.outcome : "No outcome written.") + (g.done_when ? "\nDone when: " + g.done_when : "") + (g.result ? "\nResult: " + g.result : "");
  const acts = [
    `<span class="link" data-goal-act="up" data-g="${esc(g.name)}" title="more important">↑</span>`,
    `<span class="link" data-goal-act="down" data-g="${esc(g.name)}" title="less important">↓</span>`,
    open && me && g.owner !== me && !g.shared ? `<span class="link" data-goal-act="own" data-g="${esc(g.name)}">own</span>` : "",
    open && me && g.owner === me ? `<span class="link" data-goal-act="release" data-g="${esc(g.name)}">release</span>` : "",
    `<span class="link" data-goal-act="edit" data-g="${esc(g.name)}">edit</span>`,
    open ? (g.shared ? `<span class="link" data-goal-act="unshare" data-g="${esc(g.name)}" title="One agent can own this goal again">unshare</span>`
      : `<span class="link" data-goal-act="share" data-g="${esc(g.name)}" title="No owner: maxpm go gives this goal to no agent, and its items stay open to every agent">share</span>`) : "",
    open ? `<span class="link" data-goal-act="done" data-g="${esc(g.name)}">complete</span>` : `<span class="link" data-goal-act="reopen" data-g="${esc(g.name)}">reopen</span>`,
  ].filter(Boolean).join("");
  return `<div class="goal${selected ? " on" : ""}${open ? "" : " complete"}" data-goal="${esc(g.name)}" title="${esc(tip)}">
    <div class="gh"><b>${esc(g.name)}</b>${open ? ownerChip(g.owner, g.shared) : chip("c-done", "complete", null, ' style="text-decoration:none"')}${open && g.owner && g.owner_expires_at ? `<span class="muted" style="font-size:12px" title="Its agent items are reserved for the owner until then; each maxpm command of the owner renews it">${left(g.owner_expires_at)} left</span>` : ""}</div>
    ${bar(done)}
    <div class="go">${g.items_done.length}/${total} done${g.items_open.length ? " · " + g.items_open.length + " open" : ""}</div>
    <div class="ga">${acts}</div></div>`;
}

// A project: fold button, rank, name, and rank arrows, then the caller's meta, counts, and body.
export function projCard(p, closed, inner) {
  return `<div class="proj${closed ? " closed" : ""}" id="proj-${esc(p.name)}">
      <div class="proj-h">
        <button class="fold" data-fold="${esc(p.name)}" title="${closed ? "Show" : "Hide"} this project's goals and items" aria-expanded="${!closed}">${closed ? "▸" : "▾"}</button>
        <span class="rk" title="Project rank: 1 is the most important">${p.rank}</span><b>${esc(p.name)}</b>
        <span class="spacer"></span>
        <button class="iconbtn" data-rank="${esc(p.name)}" data-to="${p.rank - 1}" title="more important">↑</button>
        <button class="iconbtn" data-rank="${esc(p.name)}" data-to="${p.rank + 1}" title="less important">↓</button>
        ${button("Archive", { data: { "archive-project": p.name }, title: "Archive this project: its items leave the queue and the lists; nothing is deleted (#5197)" })}</div>
      ${inner}
    </div>`;
}
