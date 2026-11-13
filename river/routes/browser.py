"""The browser of a project in the side panel (#4631; river/browser.py): its frames for the panel (a long poll, with
the page's token), and one page action, op "browser", for the panel and for maxpm browser alike. The person's mouse
and keys come through op "browser_input", which needs the page's token (server.TYPING_OPS). A session's Playwright MCP
reaches the browser through its own CDP address, GET /cdp/<agent> (#5405)."""

import hmac
import shlex
import threading
import time

from .. import annotate, browser, core, server  # noqa: F401  (server: read at call time, so tests that replace its globals work)
from ..core import RiverError

# Requests to show a browser in a session's side panel (maxpm browser open): the state carries them, as with
# canvases (server.CANVAS_OPENS). In memory only, for BROWSER_OPEN_KEEP seconds.
BROWSER_OPENS = []
BROWSER_OPEN_KEEP = 120
_open_lock = threading.Lock()
_open_seq = [0]


def browser_opens():
    with _open_lock:
        cut = time.time() - BROWSER_OPEN_KEEP
        BROWSER_OPENS[:] = [x for x in BROWSER_OPENS if x["at"] >= cut][-50:]
        return [{k: v for k, v in x.items() if k != "at"} for x in BROWSER_OPENS]


def _ask_open(project, session, by):
    with _open_lock:
        _open_seq[0] += 1
        BROWSER_OPENS.append({"seq": _open_seq[0], "project": project, "session": session, "by": by, "at": time.time()})


# The sessions whose side panel was asked to show the browser at their first input (#5616): (project, session).
SHOWN = set()


def first_input(project, session):
    """A session's first click or key in a project's browser opens the browser in its side panel, once; the person
    closes it and it stays closed."""
    if session and (project, session) not in SHOWN:
        SHOWN.add((project, session))
        _ask_open(project, session, session)


def frame(h, path):
    """GET /api/browser/frame?project=<name>&after=<seq>&token=<page token>: the newest frame after that seq, or the
    status alone after a few seconds with nothing new. With bin=1 (#5637) the answer is binary (browser.frame_bytes),
    and the pane may keep several such polls open at once (browser.frame, many)."""
    from urllib.parse import parse_qs, urlsplit
    q = {k: v[0] for k, v in parse_qs(urlsplit(h.path).query).items()}
    if not hmac.compare_digest(q.get("token", ""), server.PAGE_TOKEN):
        return h._send(403, {"error": "this needs the page's token: reload the page"})
    project, binary = q.get("project", ""), q.get("bin") == "1"
    b = browser.find(project, q.get("session"))  # the browser of the panel's session (#5636)
    if b is None:
        out = {"project": project, "running": False, "seq": 0, "tabs": [], "controller": None}
    else:
        try:
            out = browser.frame(b, int(q.get("after") or 0), min(float(q.get("wait") or 8), 20), q.get("pane") or None,
                                many=binary)
        except (RiverError, ValueError) as e:
            return h._send(409, {"error": str(e)})
    if binary:
        return h._send(200, browser.frame_bytes(out), "application/octet-stream")
    return h._send(200, out)


def _person(conn, who):
    if not who:
        return True  # the page with no "You are" chosen: a person at this computer
    r = conn.execute("SELECT kind FROM agents WHERE name=?", (who,)).fetchone()
    return bool(r and r["kind"] == "human")


# action -> whether it changes the page (the controller rule holds for it)
CHANGES = {"open", "click", "type", "fill", "select", "check", "uncheck", "key", "back", "forward", "reload", "eval",
           "tab", "new_tab", "close_tab", "dialog", "upload", "hover", "scroll", "drag", "size"}
# a session's first one shows its panel
INPUTS = {"click", "type", "fill", "select", "check", "uncheck", "key", "upload", "hover", "scroll", "drag"}
# What a batch may hold (#5663): the actions whose answer is text.
BATCH = (CHANGES - {"tab", "new_tab", "close_tab"}) | {"snapshot", "text", "status", "outline", "extract"}
# #5680: the actions that join a session's walk, and the arguments a step keeps
RECORD = {"open", "click", "type", "fill", "select", "check", "uncheck", "key", "dialog", "upload", "back", "forward",
          "reload", "extract", "hover", "scroll", "drag"}
STEP_KEYS = ("action", "url", "selector", "x", "y", "text", "values", "files", "key", "accept", "rows", "fields",
             "limit", "within", "wait", "button", "count", "mods", "to", "amount")
# the actions on one element: a name becomes its ref (#5667); the page's elements are read before them (#5673)
ON_ELEMENT = ("click", "type", "fill", "select", "check", "uncheck", "upload", "hover", "scroll", "drag")
WALK_KEEP = 200  # the steps of a walk that a browser remembers


def batch(conn, a, who):
    """Several actions in one request (maxpm browser batch): each one as act() runs it, in order; the first that fails
    ends the batch, and the answer says which one and why."""
    steps = a.get("steps")
    if not isinstance(steps, list) or not steps:
        raise RiverError("a batch needs steps: one action on each line")
    for s in steps:
        name = (s or {}).get("action") if isinstance(s, dict) else None
        if name not in BATCH:
            raise RiverError(f"a batch takes {', '.join(sorted(BATCH))}; not {name}")
    done = []
    for s in steps:
        step = {k: v for k, v in a.items() if k in ("project", "agent", "cwd", "session")}
        step.update(s, _guard=True)
        try:
            done.append({"step": s, "result": act(conn, step, who)})
        except RiverError as e:
            done.append({"step": s, "error": str(e), "code": getattr(e, "code", "error")})
            break
    return {"steps": done, "ok": "error" not in done[-1], "left": len(steps) - len(done)}


RUN = BATCH | {"upload"}


def run(conn, a, who):
    """#5680: a walk as one call (maxpm browser run): the steps in order, each as act() runs it, the controller
    checked before each one (when the person takes control, the run stops before its next step: not_controller).
    The result is the data of the last extract. A failed step ends it: its number, its code, and the page's outline."""
    steps = a.get("steps")
    if not isinstance(steps, list) or not steps:
        raise RiverError("a run needs steps: one action on each line")
    for s in steps:
        name = (s or {}).get("action") if isinstance(s, dict) else None
        if name not in RUN:
            raise RiverError(f"a run takes {', '.join(sorted(RUN))}; not {name}")
    result, res = None, {}
    for i, s in enumerate(steps, 1):
        step = {k: v for k, v in a.items() if k in ("project", "agent", "cwd", "session")}
        step.update(s, _guard=True, _run=True)
        try:
            res = act(conn, step, who)
        except RiverError as e:
            b = _browser_of(conn, step, who)
            try:
                outline = browser.outline(b)["lines"] if b.running() and not b.dialog else []
            except RiverError:
                outline = []
            line = " ".join(str(x) for x in [s["action"], s.get("selector") or s.get("url") or s.get("key") or ""] if x)
            return {**(b.info() if b.running() else {}), "ok": False, "done": i - 1, "result": result,
                    "failed": {"step": i, "line": line, "code": getattr(e, "code", None) or "error",
                               "message": str(e), "candidates": getattr(e, "candidates", None) or []},
                    "outline": outline}
        if s["action"] == "extract":
            result = res.get("data")
    return {**res, "ok": True, "done": len(steps), "result": result}


def _browser_of(conn, a, who):
    person = _person(conn, who)
    project = browser.project_of(conn, a.get("project"), a.get("agent") or (None if person else who), a.get("cwd"))
    return browser.get(project, (a.get("session") or a.get("agent") or None) if person else who, conn)


def call(conn, a, who):
    """#5678: op browser_call, the HTTP door: one action as act() runs it, always in the reply shape (browser.shape);
    a refusal is a reply with ok false and its error code, not an HTTP error."""
    action = a.get("action") or "status"
    try:
        return browser.shape(action, act(conn, a, who))
    except RiverError as e:
        return browser.shape(action, error=e)


def act(conn, a, who):
    """One browser action. a: action, and project, or agent (its item's project), or cwd (the project of the folder);
    then url, selector, x, y, text, key, js, full, sub, target, w, h, window as the action needs.
    #5674: an action that opens a box of the page (alert, confirm, prompt) replies with the page and the box; a box
    that was open before the action refuses it, and the refusal names the box."""
    at = time.time()
    b, mark = _record_mark(conn, a, who)
    try:
        out = _act(conn, a, who)
    except browser.DialogOpen as e:
        if e.dialog["at"] < at:
            _record(b, mark, error=e)
            raise
        out = {**e.browser.info(), "navigated": False}
    except RiverError as e:
        _record(b, mark, error=e)
        raise
    _record(b, mark, out=out)
    return out


STEP_ACTIONS = CHANGES | {"extract"}  # #5682: the actions that add a step to a record that is on


def _record_mark(conn, a, who):
    """#5682: when a record is on in this browser, the mark of an action that joins it: its line (names, not refs),
    who, and the page's error count before it. Each action of a batch or a run joins on its own."""
    if not browser.RECORDING or (a.get("action") or "status") not in STEP_ACTIONS:
        return None, None
    try:
        b = _browser_of(conn, a, who)
    except RiverError:
        return None, None
    if not b.rec:
        return None, None
    s = {k: v for k, v in a.items() if k in STEP_KEYS + ("size", "js", "target")}
    if b.running() and not b.dialog:
        for k in ("selector", "rows", "to"):
            if s.get(k) and isinstance(s[k], str):
                try:
                    s[k] = browser.step_target(b, s[k])
                except RiverError:
                    pass  # the ref as it was typed
    person = _person(conn, who)
    return b, browser.record_begin(b, browser.step_words(s), (who or "the person") if person else who)


def _record(b, mark, out=None, error=None):
    if mark is not None:
        browser.record_step(b, mark, out, error)


def record_action(conn, a, who, b, project, person):
    """#5682: maxpm browser record start [--name] | stop | status | step <n>. Stop puts the steps as a canvas of the
    project (the record's name), which opens in the session's side panel."""
    do = a.get("record") or "status"
    by = (who or "the person") if person else who
    if do == "start":
        return browser.record_start(b, a.get("name"), by)
    if do == "step":
        return {**b.info(), "step": browser.record_get(b, a.get("n"))}
    if do == "status":
        r = b.rec or b.last_rec
        return {**b.info(), "on": bool(b.rec), "record": browser.record_info(r),
                "steps": [{k: s.get(k) for k in ("n", "line", "ok", "code", "ms", "errors")} for s in (r or {}).get(
                    "steps", [])]}
    if do != "stop":
        raise RiverError(f"maxpm browser record start | stop | status | step <n>; not {do}")
    r = browser.record_stop(b)
    out = {**b.info(), "on": False, "record": browser.record_info(r), "canvas": None}
    if r["steps"]:
        pid = core._project(conn, project)["id"]
        page = browser.record_page(r, int(core.setting(conn, "canvas_max", project_id=pid)))
        c = core.canvas_put(conn, r["name"], page, actor=who or r["by"] or "maxpm", kind="html",
                            title=f"Browser record {r['name']}", project=project, note=f"{len(r['steps'])} steps")
        out["canvas"] = {"name": c["name"], "version": c["shown"]["version"], "bytes": len(page.encode())}
    return out


def _act(conn, a, who):
    action = a.get("action") or "status"
    if action == "batch":
        return batch(conn, a, who)
    if action == "run":
        return run(conn, a, who)
    person = _person(conn, who)
    project = browser.project_of(conn, a.get("project"), a.get("agent") or (None if person else who), a.get("cwd"))
    # Whose browser (#5636): a session's own (maxpm browser, its Playwright); the person's panel names the session it
    # shows; a page walk uses the project's own browser, so a check never starts a Chrome for a session.
    owner = None if action == "walk" else (a.get("session") or a.get("agent") or None) if person else who
    b = browser.get(project, owner, conn)
    if action == "steps":  # #5680: this browser's walk so far
        if a.get("clear"):
            b.walk = []
        steps = list(getattr(b, "walk", []))
        return {**b.info(), "steps": steps, "lines": [shlex.join(browser.step_words(s)) for s in steps]}
    if action == "record":  # #5682: a step record of the actions in this browser
        return record_action(conn, a, who, b, project, person)
    if action == "size":  # #5676: the page's size by command (the controller rule holds above)
        return browser.set_size(b, a.get("size") or "", (who or "the person") if person else who, a.get("dpr"))
    if action == "errors":  # #5675: the shown tab's script errors and failed requests
        out = {**b.info(), "errors": list(b.errors), "lines": [browser.error_line(e) for e in b.errors]}
        if a.get("clear"):
            b.errors = []
        return out
    # #5680: a step of a batch or a run checks the controller before it runs, reads too: the person's Take control
    # stops the sequence before its next step
    guard = a.get("_guard") and not person and action not in CHANGES
    if (action in CHANGES and not (person and action == "dialog")) or guard:  # the person answers a box at any time
        try:
            browser.may_change(conn, b, who, person)
        except browser.BrowserError:
            raise
        except RiverError as e:  # #5678: its code
            raise browser.BrowserError(str(e), "not_controller")
        if person:
            b.person_at = time.time()
        if not person and action in INPUTS:
            first_input(project, who)
    if person and a.get("pane") and action in CHANGES:
        b.drive(a.get("pane"))  # the person uses the browser in this panel: its size is the page's size (#5625)
    tab = a.get("tab")
    if tab and action not in ("tab", "new_tab", "close_tab", "suggest", "walk", "start") and b.running() \
            and tab != b.target:  # #5678: a call names its tab, and depends on no tab that shows by chance
        browser.may_change(conn, b, who, person)
        browser.switch_tab(b, tab)
    if action == "status":
        if getattr(b, "stale", False) and b.running() and not b.dialog:
            b.here()  # #5717: the page's script changed the address since the last read of its title
        return b.info()
    if action == "suggest":  # the address bar, as the person types (#5635): from memory, no Chrome call
        return {"items": browser.suggest(b, a.get("text") or "")}
    if action == "walk":  # a page walk check (#5516): its own background tab, so it changes nothing the person sees
        pages = a.get("pages") or []
        if not isinstance(pages, list) or not pages:
            raise RiverError("a page walk needs pages: a list of addresses")
        return browser.walk(b, [browser.norm_url(u) for u in pages], float(a.get("settle") or 3),
                            float(a.get("timeout") or 60))
    if action == "start":  # the panel shows the browser: start it, and say so
        why = None if b._attached_now() else browser.no_chrome()
        if why:  # the panel asks the person to download Chrome (#5624)
            return {**b.info(), "no_chrome": why, "download": browser.CHROME_DOWNLOAD}
        b.start()
        return b.info()
    wait = a.get("wait")
    if wait and wait not in browser.WAITS:
        raise RiverError(f"--wait is {', '.join(browser.WAITS)}, not {wait}")
    sess = None if person else who

    def page_text(a):  # #5672: the text with its limits, and where the rest is
        every = bool(a.get("all"))
        t = browser.page_text(b, a.get("section"), a.get("grep"), a.get("from") or 0,
                              10 ** 9 if every else int(a.get("max") or browser.TEXT_MAX), every)
        return {k: t.get(k) for k in ("text", "rest", "next", "section", "matches")}

    pre = None  # #5673: the page's elements before a session's action, for the reply that says what changed
    b.said = []  # #5668: what the action did on its own (a ref found again), for its reply
    # #5680: a session's action joins its walk once it succeeds, with the role and name of a ref (refs end with
    # the page); maxpm browser steps prints the walk for run. A run's own steps do not join it.
    rec = None
    if not person and action in RECORD and not a.get("_run"):
        rec = {k: v for k, v in a.items() if k in STEP_KEYS}
        for k in ("selector", "rows", "to"):
            if rec.get(k) and isinstance(rec[k], str):
                rec[k] = browser.step_target(b, rec[k])

    def record():
        if rec is not None:
            b.walk = (getattr(b, "walk", []) + [rec])[-WALK_KEEP:]

    def then(out):  # --snapshot: the snapshot of the page that the action left (#5663)
        record()
        if b.said:
            out["notes"] = b.said
        if pre is not None and not out.get("dialog"):  # a new page: its outline; else what changed on this one
            if out.get("navigated"):
                out["outline"] = browser.outline(b)["lines"]
            else:
                out["changes"] = browser.changes(b, pre)
        if a.get("snapshot"):
            out["snapshot"] = browser.snapshot(b, interactive=True)["snapshot"]
        return out
    if action == "snapshot":
        return browser.snapshot(b, bool(a.get("interactive")), a.get("scope") or None, bool(a.get("delta")),
                                int(a.get("max") or browser.SNAP_MAX), view=not a.get("all"),
                                region=a.get("region") or None, find=a.get("find") or None)
    if action == "open":
        out = b.open(a.get("url") or "")
        if wait == "idle":
            browser.net_idle(b)
        if not person and a.get("show", True):
            _ask_open(project, who, who)
        if a.get("outline", not person) and not out.get("dialog"):  # #5670: the map of the page; the pane needs none
            out["outline"] = browser.outline(b)["lines"]
        if a.get("read") and not out.get("dialog"):  # #5672: open --text, the text in the same command
            out.update(page_text(a))
        return then(out)
    if action == "outline":
        return {**b.info(), "outline": browser.outline(b, int(a.get("heads") or browser.OUTLINE_HEADS))["lines"]}
    if action == "extract":  # #5677: rows with named fields, as JSON
        r = browser.extract(b, a.get("rows") or None, a.get("fields") or [], int(a.get("limit") or browser.EXTRACT_MAX))
        record()
        return {**b.info(), "data": r["rows"], "count": r["count"]}
    if action in ON_ELEMENT and a.get("selector"):
        # #5667: a name (or role and name, --in its row) becomes the ref of the one element so named
        a = {**a, "selector": browser.resolve_target(b, a["selector"], a.get("within"), action)}
    if action == "drag" and isinstance(a.get("to"), str):
        a = {**a, "to": browser.resolve_target(b, a["to"], None, "drag")}
    if action in ON_ELEMENT + ("key", "dialog") and not person and a.get("changes", True):
        pre = browser.view_keys(b) if not b.dialog else None
    if action == "upload":  # #5679
        return then(browser.upload(b, conn, a.get("selector") or "", a.get("files") or [], who=sess, wait=wait))
    x, y, mods = a.get("x"), a.get("y"), a.get("mods") or ()
    x, y = (None if x is None else float(x)), (None if y is None else float(y))
    if action == "click":
        return then(browser.click(b, a.get("selector"), x, y, who=sess, wait=wait, button=a.get("button") or "left",
                                  count=int(a.get("count") or 1), mods=mods))
    if action == "hover":  # #5669
        return then(browser.hover(b, a.get("selector"), x, y, who=sess, wait=wait, mods=mods))
    if action == "scroll":
        return then(browser.scroll(b, a.get("selector"), a.get("amount"), who=sess, wait=wait, x=x, y=y))
    if action == "drag":
        src = a.get("selector") or ([x, y] if x is not None and y is not None else None)
        if not src or not a.get("to"):
            raise RiverError("a drag needs where it starts and where it ends: maxpm browser drag <from> <to>")
        return then(browser.drag(b, src, a["to"], who=sess, wait=wait, mods=mods))
    if action == "type":
        return then(browser.type_text(b, a.get("text") or "", a.get("selector"), who=sess, wait=wait))
    if action == "fill":
        return then(browser.fill(b, a.get("selector") or "", a.get("text") or "", who=sess, wait=wait))
    if action == "select":
        return then(browser.select_options(b,a.get("selector") or "", a.get("values") or [], who=sess, wait=wait))
    if action in ("check", "uncheck"):
        return then(browser.check(b, a.get("selector") or "", action == "check", who=sess, wait=wait))
    if action == "key":
        return then(browser.press(b, a.get("key") or "", who=sess, wait=wait))
    if action in ("back", "forward"):
        return then(browser.history(b, -1 if action == "back" else 1, wait=wait))
    if action == "reload":
        return then(browser.reload(b, wait=wait))
    if action == "dialog":
        text = a.get("text")
        return then(browser.answer_dialog(b, a.get("accept", True), None if text is None else str(text), who=sess,
                                          wait=wait))
    if action == "eval":
        return {**b.info(), "value": b.evaluate(a.get("js") or "")}
    if action == "html":  # #5671: --scope (a ref or a CSS selector) gives one part of the page, the fallback of a name
        sc = browser.resolve_target(b, a.get("scope"), verb="html --scope") if a.get("scope") else None
        html = browser.on_element(b, sc, "(el) => el.outerHTML") if sc else b.evaluate("document.documentElement.outerHTML")
        return {**b.info(), "html": html, "bytes": len((html or "").encode())}
    if action == "text":
        return {**b.info(), **page_text(a)}
    if action == "screenshot":
        return browser.screenshot(b, bool(a.get("full")), bool(a.get("cursor")))
    if action == "tab":
        return browser.switch_tab(b, a.get("target") or "")
    if action == "new_tab":
        return browser.new_tab(b, a.get("url") or "about:blank")
    if action == "close_tab":
        return browser.close_tab(b, a.get("target"))
    if action == "control":
        if person:
            out = browser.take(conn, b, who)
            b.drive(a.get("pane"))
            return out
        return browser.control(conn, b, who, a.get("sub"), bool(a.get("take")))
    if action == "give":
        if person:
            raise RiverError("a person gives the browser back with Give back (action give_back)")
        if not (a.get("sub") or "").strip():
            raise RiverError("name the subagent: maxpm browser give <label>")
        return browser.control(conn, b, who, a.get("sub"))
    if action == "release":
        return browser.release(conn, b, who)
    if action == "give_back":
        return browser.give_back(conn, b, who)
    if action == "resize":
        b.resize(a.get("w") or 0, a.get("h") or 0, a.get("dpr") or 1, a.get("screen"), a.get("pane"))
        return b.info()
    if action == "window":  # decision 3: a normal window for a site that refuses a headless browser, and back
        url = b.info()["url"] if b.running() else ""
        window = bool(a.get("window", True))
        if window and not person:  # #5650: only when the person asked in the session's chat
            browser.may_open_window(conn, b, who)
        if b._attached_now():  # a Chrome that serve did not start: its browser_mode, under the lock (#5619)
            browser.attached_mode(conn, b, who, window)
        else:
            b.start(window=window)
        browser.mark_window(b, who, person, window)  # the panel says who opened the window, and when (#5650)
        if url and url != "about:blank":
            b.open(url)
        return b.info()
    if action == "close":
        if not person:
            c = b.state.get("controller")
            if c and c["who"] != who and not browser.controller_gone(conn, c):
                raise RiverError(f"the browser of {project} is controlled by {c['who']}; maxpm browser status")
        b.close()
        return b.info()
    if action == "save_signins":  # Save sign-ins for the project (#5633): the person's choice, never a session's
        if not person:
            raise RiverError("only a person saves a browser's sign-ins for the project: the Browser pane has the "
                             "button Save sign-ins for the project")
        return browser.save_signins(b)
    if action == "freeze":  # Annotate's background (#5660): a PNG of the tab and the size of its view in CSS pixels,
        # read together: a frame that the pane shows can be older than the pane's last resize
        view = b.evaluate("[innerWidth, innerHeight]", 5)
        return {**browser.screenshot(b), "view": view}
    if action in ("pick", "area"):  # Annotate (#5655): the element at a point, or what a dragged box holds
        if not b.running():
            raise RiverError("the browser is not running: open a page first")
        arg = {"x": float(a.get("x") or 0), "y": float(a.get("y") or 0)} if action == "pick" else \
            {k: float((a.get("box") or {}).get(k) or 0) for k in ("x", "y", "w", "h")}
        return annotate.page_call(b, action, arg) or {}
    if action == "annotations":  # the pane: this session's marked-up pages, and where their notes are on this page now
        return {"sets": annotations_here(b, annotate.sets(conn, agent=owner, project=project, limit=20))}
    if action == "annotation_mark":  # the person reopens a note (#5655); a session marks with maxpm browser annotation
        if not person:
            raise RiverError("a session marks a note with maxpm browser annotation <id> fix <n> --note \"...\"")
        annotate.mark(conn, a.get("id"), a.get("n"), "open", a.get("note"), who or "the person")
        return {"sets": annotations_here(b, annotate.sets(conn, agent=owner, project=project, limit=20))}
    raise RiverError(f"unknown browser action {action!r}")


def annotations_here(b, sets):
    """The sets for the pane; each note of a set on the page that shows now gets 'here': its place in the view (the
    element of an element note where the page has it now, else its place on the page less the scroll)."""
    url = b.info().get("url") if b.running() else None
    on = [s for s in sets if url and s["url"] == url]
    if on:
        try:
            got = annotate.page_call(b, "locate", [x.get("ref") if x["kind"] == "element" else None
                                                   for s in on for x in s["notes"]]) or {}
        except RiverError:
            got = {}
        sx, sy = (got.get("scroll") or [0, 0])[:2]
        at = iter(got.get("at") or [])
        for s in on:
            for x in s["notes"]:
                el, p = next(at, None), x.get("page") or x.get("at")
                if x["kind"] == "element" and el:
                    x["here"] = {"x": el["x"], "y": el["y"], "w": el["w"], "h": el["h"]}
                elif p:
                    x["here"] = {"x": p["x"] - sx, "y": p["y"] - sy, "w": p.get("w", 0), "h": p.get("h", 0)}
    return sets


def annotate_send(conn, a, who):
    """Send or Copy of Annotate (#5655): store the set (the picture with the drawings, the notes), and with send, put
    one message in the session's chat: the picture attached, the page's address, and the numbered notes with their
    elements. A typing op: it types into the session's chat, so it needs the page's token."""
    if not _person(conn, who):
        raise RiverError("only a person sends a marked-up page; a session reads it with maxpm browser annotation <id>")
    project, session = a.get("project") or "", (a.get("session") or "").strip()
    send = bool(a.get("send"))
    if send and not session:
        raise RiverError("this browser has no session to send to: Copy puts the text on the clipboard")
    s = annotate.save(conn, project, session or None, who or None, a.get("url") or "", a.get("title") or "",
                      a.get("w"), a.get("h"), a.get("png"), a.get("notes") or [], a.get("drawings"), a.get("id"))
    out = {**s, "prompt": annotate.prompt(s, who), "sent": False}
    if send:
        text = annotate.prompt(s, who, limit=annotate.SEND_MAX, attached=True)
        try:
            server.session_send(conn, session, text, who=who, hold=True,
                                files=[{"name": f"annotations-a{s['id']}.png", "data": a.get("png")}])
        except RiverError as e:
            raise RiverError(f"stored as a{s['id']}, but the chat of {session} did not get it: {e}. "
                             f"The session reads it with maxpm browser annotation {s['id']}")
        annotate.mark_sent(conn, s["id"])
        out.update(sent=True, told=session)
    return out


def person_input(conn, a, who):
    """The person's mouse and keys in the panel (needs the page's token)."""
    b = browser.find(a.get("project") or "", a.get("session"))
    if b is None or not b.running():
        raise RiverError("the browser is not running: open a page first")
    browser.may_change(conn, b, who, True)
    evs = [ev or {} for ev in a.get("evs") or [a.get("ev") or {}]]  # what came while the batches before were on their way (#5479)
    calls = []
    for ev in evs:
        calls += browser.input_calls(ev)
    # #5651: nobody controls the browser, and the person clicks, scrolls or types in it: they control it from now on
    # (a session's maxpm browser waits then), with no Take control first. A hover alone takes nothing.
    if not b.state.get("controller") and any(ev.get("action") != "move" for ev in evs):
        browser.take(conn, b, who)
    b.person_at = time.time()
    b.drive(a.get("pane"))
    # #5651: the request answers at once; serve sends the calls to Chrome in the pane's order (browser.Inputs).
    n = a.get("n")
    b.inputs.put(a.get("pane"), int(n) if isinstance(n, (int, float)) and n >= 1 else None, calls)
    return {"ok": True, "error": b.inputs.refusal()}


HAND_TEXT = "The browser is at {title} ({url}). Take it from here."


def hand(conn, a, who):
    """Hand to <session> in the panel (#5616): the person gives the browser to the session whose side panel shows it,
    and its chat gets one message with the page, so it goes on from there (the same tab, the person's sign-in). A
    typing op: it types into the session's chat, so it needs the page's token."""
    project, to = a.get("project") or "", (a.get("to") or "").strip()
    if not to:
        raise RiverError("name the session to hand the browser to")
    r = conn.execute("SELECT kind FROM agents WHERE name=?", (to,)).fetchone()
    if not r or r["kind"] != "ai":
        raise RiverError(f"no session {to}: pick a session that runs now")
    if not _person(conn, who):
        raise RiverError("only a person hands the browser on; a session takes it with maxpm browser control")
    b = browser.find(project, a.get("session") or to)
    if b is None or not b.running():
        raise RiverError("the browser is not running: open a page first")
    server._typing_pane(conn, to, who)  # the chat can take the message: else the control stays with the person
    info = browser.hand(conn, b, who, to)
    text = HAND_TEXT.format(title=info.get("title") or "a page with no title", url=info.get("url") or "about:blank")
    try:
        server.session_send(conn, to, text, who=who, hold=True)
    except RiverError as e:
        raise RiverError(f"{to} controls the browser now, but its chat did not get the message: {e}")
    SHOWN.add((project, to))
    return {**info, "told": to, "text": text}


def cdp(h, path):
    """GET /cdp/<agent>?project=<name>&token=<t>, a WebSocket: one session's CDP connection to its project's browser
    (#5405). serve opens a CDP connection to Chrome for it and relays each message after browser.cdp_refusal. The
    token comes from the session's MCP config (browser.mcp_config_file); the real debugging port never leaves serve."""
    from urllib.parse import parse_qs, unquote, urlsplit
    u = urlsplit(h.path)
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    agent, project = unquote(u.path[len("/cdp/"):]), q.get("project", "")
    if not agent or not project or not hmac.compare_digest(q.get("token", ""), browser.cdp_token(project, agent)):
        return h._send(403, {"error": "this CDP address needs the token from the session's MCP config "
                                      "(setting browser_mcp; maxpm starts the session)"})
    key = h.headers.get("Sec-WebSocket-Key")
    if (h.headers.get("Upgrade") or "").lower() != "websocket" or not key:
        return h._send(400, {"error": "a CDP address takes a WebSocket (Playwright: --cdp-endpoint)"})
    conn = core.connect()
    try:
        if not conn.execute("SELECT 1 FROM projects WHERE name=?", (project,)).fetchone():
            return h._send(404, {"error": f"no project {project}"})
        b = browser.get(project, agent, conn)  # the session's own browser (#5636)
        if b._attached_now():
            return h._send(409, {"error": f"the browser of {project} is a Chrome that serve did not start "
                                          f"(browser_source): sessions use maxpm browser for it, not a CDP address"})
        try:
            b.start()
            upstream = browser.WS(b._version(b.port)["webSocketDebuggerUrl"])
        except (RiverError, OSError, ValueError, KeyError) as e:
            return h._send(409, {"error": f"the browser of {project} did not start: {e}"})
        # RFC 6455 wants an HTTP/1.1 status line; the handler writes HTTP/1.0 by default, which Playwright accepts
        # but a strict client refuses (agent-browser: "HTTP version must be 1.1 or higher", #5657).
        h.protocol_version = "HTTP/1.1"
        h.send_response_only(101, "Switching Protocols")
        h.send_header("Upgrade", "websocket")
        h.send_header("Connection", "Upgrade")
        h.send_header("Sec-WebSocket-Accept", browser.ws_accept(key))
        h.end_headers()
        h.wfile.flush()
        h.close_connection = True
        browser.cdp_session(conn, b, agent, h.rfile, h.connection, upstream, on_input=lambda: first_input(project, agent))
    finally:
        conn.close()


GET = [
    (lambda path: path == "/api/browser/frame", frame),
    (lambda path: path.startswith("/cdp/"), cdp),
]

OPS = {
    "browser": act,
    "browser_call": call,  # #5678: the same action, in the one reply shape
    "browser_input": person_input,
    "browser_hand": hand,
    "browser_annotate": annotate_send,
}
