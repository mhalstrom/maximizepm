"""Annotations of a session's browser (#5655): the person marks up the page in the Browser pane (boxes, pen lines,
arrows, text notes, selected elements) and sends it to the session. Each sent set is stored here (the table
annotations: its page address, the picture with the drawings, and the numbered notes, each with the element it
names), so the session, and a later session on the same item, read it again: maxpm browser annotations, maxpm
browser annotation <id>. The session marks each note fixed or not fixed with one line of what it changed (as the
check mode of the annotatetofix add-on does), and the pane shows each note's state; the person can reopen a note.

The selector code that runs in the page (PAGE_JS) is a copy of annotatetofix's utils/selector.ts and utils/area.ts,
in plain JavaScript: getSelector, elementText, findElement, containerOf and elementsInside. The prompt form
(prompt()) follows its utils/prompt.ts."""

import base64
import json
import re
from pathlib import Path

from . import core
from .core import RiverError

STATES = ("open", "fixed", "not_fixed")
NOTES_MAX = 60       # numbered notes in one set
NOTE_MAX = 2000      # characters of one note's text
FIX_MAX = 300        # characters of the session's line for a fixed or not fixed note
PICTURE_MAX = 12 * 1024 * 1024
SEND_MAX = 3900      # the chat takes 4000 characters (server.session_send)
INTRO = "These are problems that {who} marked on this page. Fix each one in the code."

# The selector code of annotatetofix (utils/selector.ts, utils/area.ts), run in the page by Runtime.evaluate. One
# function: (mode, arg) -> the answer of that mode. pick: the element at a point of the view (CSS pixels); area: the
# smallest element that holds a box, and the main elements inside it; locate: where the elements of earlier notes
# are now. Boxes are in the view's CSS pixels; scroll is the page's scroll, so view + scroll is the place on the page.
PAGE_JS = r"""((mode, arg) => {
  const TEST = ['data-testid', 'data-test-id', 'data-test', 'data-cy', 'data-qa'];
  const SKIP = '__maxpm_cursor';
  const esc = (v) => CSS.escape(v);
  const quote = (v) => '"' + v.replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';
  const generated = (id) => /\d{4,}/.test(id) || id.startsWith(':') || /^(radix|headlessui|mui|ember|react)[-_]/i.test(id);
  const unique = (sel, el) => { try { const f = document.querySelectorAll(sel); return f.length === 1 && f[0] === el; }
    catch (e) { return false; } };
  const text = (el) => {
    let t = '';
    if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) t = el.value || el.placeholder;
    else if (el instanceof HTMLImageElement) t = el.alt;
    else t = (typeof el.innerText === 'string' ? el.innerText : '') || el.textContent || '';
    if (!t.trim()) t = el.getAttribute('aria-label') || el.getAttribute('title') || '';
    return t.replace(/\s+/g, ' ').trim().slice(0, 80);
  };
  const selector = (el) => {
    if (el.id && !generated(el.id) && unique('#' + esc(el.id), el)) return '#' + esc(el.id);
    for (const name of TEST) {
      const v = el.getAttribute(name);
      if (v && unique('[' + name + '=' + quote(v) + ']', el)) return '[' + name + '=' + quote(v) + ']';
    }
    const parts = [];
    let cur = el;
    while (cur && cur !== document.documentElement) {
      if (cur !== el && cur.id && !generated(cur.id) && unique('#' + esc(cur.id), cur)) {
        parts.unshift('#' + esc(cur.id));
        if (unique(parts.join(' > '), el)) return parts.join(' > ');
        parts.shift();
      }
      let part = cur.tagName.toLowerCase();
      const parent = cur.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter((c) => c.tagName === cur.tagName);
        if (same.length > 1) part += ':nth-of-type(' + (same.indexOf(cur) + 1) + ')';
      }
      parts.unshift(part);
      if (unique(parts.join(' > '), el)) return parts.join(' > ');
      cur = parent;
    }
    return parts.join(' > ');
  };
  const ref = (el) => ({ selector: selector(el), tag: el.tagName.toLowerCase(), text: text(el) });
  const rect = (el) => { const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height }; };
  const holds = (o, i) => o.x <= i.x && o.y <= i.y && o.x + o.w >= i.x + i.w && o.y + o.h >= i.y + i.h;
  const overlaps = (a, b) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
  const sized = (b) => b.w > 0 && b.h > 0;
  const skip = (el) => el.id === SKIP || !!el.closest('#' + SKIP);
  const scroll = [scrollX, scrollY];
  const find = (t) => {
    let el = null;
    try { el = document.querySelector(t.selector); } catch (e) { el = null; }
    if (el && (!t.text || text(el) === t.text)) return el;
    if (t.text) { const m = Array.from(document.getElementsByTagName(t.tag || '*')).find((c) => text(c) === t.text);
      if (m) return m; }
    return el;
  };
  if (mode === 'pick') {
    let el = document.elementFromPoint(arg.x, arg.y);
    while (el && skip(el)) el = el.parentElement;
    el = el || document.body || document.documentElement;
    return { ref: ref(el), box: rect(el), scroll };
  }
  if (mode === 'area') {
    const box = arg;
    let best = document.body || document.documentElement, size = Infinity;
    for (const el of Array.from(document.body ? document.body.querySelectorAll('*') : [])) {
      if (skip(el)) continue;
      const r = rect(el);
      if (!sized(r) || !holds(r, box)) continue;
      if (r.w * r.h <= size) { best = el; size = r.w * r.h; }  // a tie: the later one, in document order the descendant
    }
    const inside = [];
    const walk = (parent) => {
      for (const c of Array.from(parent.children)) {
        if (inside.length >= 6) return;
        const r = rect(c);
        if (sized(r) && holds(box, r)) inside.push(c);
        else if (!sized(r) || overlaps(box, r)) walk(c);
      }
    };
    walk(best);
    const list = inside.length ? inside : Array.from(best.children).filter((c) => sized(rect(c)) && overlaps(box, rect(c))).slice(0, 6);
    return { ref: ref(best), box: rect(best), inside: list.map(ref), scroll };
  }
  if (mode === 'locate') return { scroll, at: arg.map((t) => { const el = t && find(t); return el ? rect(el) : null; }) };
  return null;
})"""


def page_call(b, mode, arg):
    """Run PAGE_JS in the tab that shows (browser.Browser b)."""
    return b.evaluate(f"{PAGE_JS}({json.dumps(mode)}, {json.dumps(arg)})", 10)


def pictures_dir():
    return Path(core.river_dir()) / "annotations"


def _num(v, name):
    try:
        return float(v)
    except (TypeError, ValueError):
        raise RiverError(f"a note's {name} must be a number")


def _box(v):
    if not isinstance(v, dict):
        return None
    return {k: round(_num(v.get(k, 0), k), 1) for k in ("x", "y", "w", "h")}


def _ref(v):
    if not isinstance(v, dict) or not isinstance(v.get("selector"), str):
        return None
    return {"selector": v["selector"][:500], "tag": str(v.get("tag") or "")[:40], "text": str(v.get("text") or "")[:80]}


def clean_notes(notes):
    """The numbered notes the pane sends, as they are stored: kind element (a click on an element), box (a dragged
    area) or text (a note at a point); its text; where it is (at: the view, page: the page, box); the element (ref),
    and for a box the main elements inside it."""
    if not isinstance(notes, list) or len(notes) > NOTES_MAX:
        raise RiverError(f"a set has at most {NOTES_MAX} notes")
    out = []
    for i, x in enumerate(notes, 1):
        if not isinstance(x, dict) or x.get("kind") not in ("element", "box", "text"):
            raise RiverError("a note is an element, a box, or a text")
        text = x.get("text") if isinstance(x.get("text"), str) else ""
        n = {"n": i, "kind": x["kind"], "text": text.strip()[:NOTE_MAX], "state": "open",
             "at": _box(x.get("at")), "page": _box(x.get("page")), "box": _box(x.get("box")), "ref": _ref(x.get("ref")),
             "inside": [r for r in map(_ref, x["inside"][:6]) if r] if isinstance(x.get("inside"), list) else []}
        out.append(n)
    return out


def _png(data):
    if not isinstance(data, str):
        raise RiverError("the picture comes as base64 PNG")
    if data.startswith("data:"):
        data = data.split(",", 1)[-1]
    try:
        raw = base64.b64decode(data, validate=True)
    except ValueError:
        raise RiverError("the picture comes as base64 PNG")
    if not raw.startswith(b"\x89PNG\r\n\x1a\n") or len(raw) > PICTURE_MAX:
        raise RiverError(f"the picture is a PNG of at most {PICTURE_MAX // (1024 * 1024)} MB")
    return raw


def _held(conn, agent):
    r = agent and conn.execute("SELECT id FROM items WHERE assignee=? AND status IN ('in_progress','held') "
                               "ORDER BY claimed_at DESC, id DESC LIMIT 1", (agent,)).fetchone()
    return r["id"] if r else None


def save(conn, project, agent, author, url, title, w, h, png, notes, drawings=None, aid=None):
    """Store a set (or, with aid, store it again: the person changed the drawing after Copy and then sent it). The
    picture goes to <queue folder>/annotations/a<id>.png. Returns the set as show() gives it."""
    raw = _png(png)
    notes = clean_notes(notes)
    drawings = {k: int(v) for k, v in (drawings or {}).items() if k in ("pen", "arrow") and isinstance(v, (int, float))}
    p = conn.execute("SELECT id FROM projects WHERE name=?", (project,)).fetchone()
    if not p:
        raise RiverError(f"no project {project}")
    at = core.iso(core.now())
    with core.tx(conn):
        if aid:
            old = conn.execute("SELECT id FROM annotations WHERE id=? AND project_id=?", (aid, p["id"])).fetchone()
            if not old:
                raise RiverError(f"no annotation set a{aid} in {project}")
            conn.execute("UPDATE annotations SET url=?, title=?, width=?, height=?, notes=?, drawings=?, updated_at=? "
                         "WHERE id=?", (url, title, int(w or 0), int(h or 0), json.dumps(notes), json.dumps(drawings),
                                        at, aid))
        else:
            item = _held(conn, agent)
            aid = conn.execute(
                "INSERT INTO annotations (project_id, agent, item_id, author, url, title, width, height, notes, drawings, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (p["id"], agent or None, item, author or None, url or "", title or "", int(w or 0), int(h or 0),
                 json.dumps(notes), json.dumps(drawings), at, at)).lastrowid
            if item:
                core._event(conn, item, author or "maxpm",
                            f"annotations a{aid}: {len(notes)} note(s) on {url} for {agent}")
        f = pictures_dir() / f"a{aid}.png"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(raw)
        conn.execute("UPDATE annotations SET picture=? WHERE id=?", (str(f), aid))
    return show(conn, aid)


def mark_sent(conn, aid):
    with core.tx(conn):
        conn.execute("UPDATE annotations SET sent_at=? WHERE id=?", (core.iso(core.now()), aid))


def _row(conn, r):
    notes = json.loads(r["notes"] or "[]")
    p = conn.execute("SELECT name FROM projects WHERE id=?", (r["project_id"],)).fetchone()
    return {"id": r["id"], "project": p["name"] if p else "", "agent": r["agent"], "item_id": r["item_id"],
            "author": r["author"], "url": r["url"], "title": r["title"], "w": r["width"], "h": r["height"],
            "picture": r["picture"], "notes": notes, "drawings": json.loads(r["drawings"] or "{}"),
            "open": sum(1 for n in notes if n["state"] != "fixed"), "created_at": r["created_at"],
            "updated_at": r["updated_at"], "sent_at": r["sent_at"]}


def _id(aid):
    try:
        return int(str(aid).lstrip("a"))
    except ValueError:
        raise RiverError(f"no annotation set {aid}: maxpm browser annotations lists them")


def show(conn, aid):
    r = conn.execute("SELECT * FROM annotations WHERE id=?", (_id(aid),)).fetchone()
    if not r:
        raise RiverError(f"no annotation set a{aid}: maxpm browser annotations lists them")
    return _row(conn, r)


def sets(conn, agent=None, project=None, item_id=None, url=None, open_only=False, limit=30):
    """The sets of a session, or of its item (a new session on the item sees them), or of a project; newest first."""
    q, args = "SELECT * FROM annotations WHERE 1=1", []
    if agent or item_id:
        q += " AND (" + " OR ".join((["agent=?"] if agent else []) + (["item_id=?"] if item_id else [])) + ")"
        args += ([agent] if agent else []) + ([item_id] if item_id else [])
    if project:
        q += " AND project_id=(SELECT id FROM projects WHERE name=?)"
        args.append(project)
    if url:
        q += " AND url=?"
        args.append(url)
    out = [_row(conn, r) for r in conn.execute(q + " ORDER BY id DESC LIMIT ?", (*args, int(limit)))]
    return [s for s in out if s["open"]] if open_only else out


def mark(conn, aid, n, state, note, actor):
    """A note's state: fixed or not_fixed, with the session's line of what it changed (or why not); open again (the
    person reopens it, with what is still wrong)."""
    state = {"fix": "fixed", "not-fixed": "not_fixed", "reopen": "open"}.get(state, state)
    if state not in STATES:
        raise RiverError("a note is fixed, not-fixed, or open again (reopen)")
    note = (note or "").strip()
    if state != "open" and not note:
        raise RiverError("say in one line what you changed: --note \"<what changed>\"" if state == "fixed"
                         else "say in one line why it is not fixed: --note \"<why>\"")
    s = show(conn, aid)
    try:
        x = s["notes"][int(n) - 1]
    except (ValueError, IndexError, TypeError):
        raise RiverError(f"a{s['id']} has notes 1 to {len(s['notes'])}")
    at = core.iso(core.now())
    x.setdefault("history", []).append({"state": state, "note": note[:FIX_MAX], "by": actor, "at": at})
    x["state"] = state
    if state == "open":
        if note:
            x["again"] = note[:NOTE_MAX]  # what is still wrong, in the person's words
        x.pop("fix", None)
    else:
        x["fix"] = {"note": note[:FIX_MAX], "by": actor, "at": at}
    with core.tx(conn):
        conn.execute("UPDATE annotations SET notes=?, updated_at=? WHERE id=?", (json.dumps(s["notes"]), at, s["id"]))
        if s["item_id"]:
            core._event(conn, s["item_id"], actor, f"annotation a{s['id']} note {x['n']}: "
                        + {"fixed": "fixed", "not_fixed": "not fixed", "open": "open again"}[state]
                        + (f": {note[:FIX_MAX]}" if note else ""))
    return show(conn, s["id"])


def _code(v):
    fence = "`" * (max([len(r) for r in re.findall(r"`+", v)] or [0]) + 1)
    pad = " " if v.startswith("`") or v.endswith("`") else ""
    return f"{fence}{pad}{v}{pad}{fence}"


def describe(ref):
    """An element as the agent finds it: tag, selector and the start of its text (annotatetofix's form)."""
    return f"{_code('<' + ref['tag'] + '>')} found by {_code(ref['selector'])}" + (
        f', text "{ref["text"]}"' if ref.get("text") else ", no text")


def note_line(x):
    """One note: where it is, the element, and the problem."""
    r = x.get("ref")
    if x["kind"] == "box" and x.get("box"):
        b, p = x["box"], x.get("page") or x["box"]
        where = f"Area {round(b['w'])} × {round(b['h'])} px at x {round(p['x'])}, y {round(p['y'])} of the page"
        if r:
            where += f"; inside {describe(r)}"
        if x.get("inside"):
            where += "; in the area: " + "; ".join(map(describe, x["inside"]))
    elif x["kind"] == "element" and r:
        where = f"Element {describe(r)}"
    else:
        p = x.get("page") or x.get("at") or {"x": 0, "y": 0}
        where = f"At x {round(p['x'])}, y {round(p['y'])} of the page" + (f", on {describe(r)}" if r else "")
    text = (x.get("text") or "(no text: see the picture)").replace("\n", "\n   ")
    if x.get("again"):
        text += f"\n   Still wrong: {x['again']}"
    return f"{where}\n   Problem: {text}"


def _drawn(d):
    parts = [f"{d[k]} {name}{'s' if d[k] != 1 else ''}" for k, name in (("pen", "pen line"), ("arrow", "arrow"))
             if d.get(k)]
    return ", ".join(parts)


def prompt(s, who=None, limit=None, attached=False):
    """The text for the session: the page, the picture, and one numbered entry for each note that is not fixed (the
    numbers are the ones in the picture). limit: cut the list to fit, and say how to read the rest."""
    who = who or s.get("author") or "the person"
    head = [f"[maxpm annotations a{s['id']} from {who}] Page: {s['url']}" + (f" ({s['title']})" if s.get("title") else ""),
            f"Picture{' (attached)' if attached else ''}: {s['picture']} ({s['w']} × {s['h']} px, the page's view)",
            INTRO.format(who=who)]
    body = [f"{x['n']}. {note_line(x)}" for x in s["notes"] if x["state"] != "fixed"]
    if s.get("drawings") and _drawn(s["drawings"]):
        body.append(f"Drawings in the picture: {_drawn(s['drawings'])}.")
    tail = [f"After each fix: maxpm browser annotation {s['id']} fix <n> --note \"<what you changed>\" "
            f"(or not-fixed <n> --note \"<why>\")."]
    if not s["notes"]:
        body.insert(0, "No numbered notes: the drawings in the picture show what to change.")
    text = "\n".join(head + body + tail)
    if limit and len(text) > limit:
        keep, left = [], limit - len("\n".join(head + tail)) - 120
        for line in body:
            if len(line) + 1 > left:
                break
            keep.append(line)
            left -= len(line) + 1
        more = len(body) - len(keep)
        keep.append(f"… and {more} more: maxpm browser annotation {s['id']}")
        text = "\n".join(head + keep + tail)
    return text


def brief(conn, agent, item_id):
    """The sets with open notes of a session or of its item, for the go briefing."""
    return [{"id": s["id"], "url": s["url"], "open": s["open"], "notes": len(s["notes"])}
            for s in sets(conn, agent=agent, item_id=item_id, open_only=True, limit=10)]
