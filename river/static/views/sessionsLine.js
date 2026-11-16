// The Sessions line of the session list (#5886: the heading over the tabs Agents, Artifacts, Browsers) and the state
// dot of a session row. The line counts the rows by their dots, so the line and the rows never disagree (#6021).
// No page imports: tests/test_sessions_line.py runs it in node.
import { esc } from "../lib.js";

const DOT = { working: "active", expert: "active", prompt: "needs", answer: "needs", idle: "away", starting: "away",
              stopping: "gone", ended: "gone", gone: "gone" };
// The label of a session whose turn ended while background work runs that wakes it: 'running: <what>' (#6021).
export const RUNNING = "running: ";

// The dot of a row and of the head of the opened session: needs (a prompt, a question to the person, anything in its !
// badge), active (a turn runs, an expert talks, or its background work runs), away (it waits), gone (it ends).
export function dotOf(s) {
  if (["prompt", "answer"].includes(s.state) || (s.waiting || []).length) return "needs";
  if (s.state === "idle" && String(s.state_label || "").startsWith(RUNNING)) return "active";
  return DOT[s.state] || "away";
}

// The words of the counts: "1 needs you · 3 working · 2 waiting".
export function countLine(list) {
  const n = { needs: 0, active: 0, away: 0 };
  for (const s of list) { const d = dotOf(s); if (d in n) n[d]++; }
  return [n.needs ? `${n.needs} need${n.needs === 1 ? "s" : ""} you` : "", n.active ? `${n.active} working` : "",
          n.away ? `${n.away} waiting` : ""].filter(Boolean).join(" · ") || "No session runs";
}

// The line. read: the page has read the sessions once. Before that the list is empty, and "No session runs" was false:
// a page that loaded hidden (a background tab, a browser that an agent drives) reads them only when it shows, and kept
// saying it while five sessions ran (#5999).
export const READING = "Reading the sessions…";
export function sessionsLine(list, browsers = 0, read = true) {
  const line = (read ? countLine(list) : READING) + (browsers ? ` · ${browsers} browser${browsers === 1 ? "" : "s"}` : "");
  return `<div class="srow srow-status"><div class="srow-main">
      <div class="srow-h"><b class="srow-t">Sessions</b></div>
      <div class="sub">${esc(line)}</div></div></div>`;
}
