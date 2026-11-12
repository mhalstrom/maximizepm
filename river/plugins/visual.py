"""The visual check (#5462, mark 2026-11-12): a page looks as it was approved. Deterministic, no model call.

maxpm ops visual run <name> --url <address> [--widths 1440,390] [--themes light,dark] [--threshold 0.01] [--mask <css>]
opens the address in a headless Google Chrome of its own (a new temporary profile each run) at each width and color
theme, takes a screenshot of each one (a "shot", named <width>-<theme>), and compares it with the approved baseline
of that shot. Chrome itself compares the two pictures on a canvas, so no image library is needed: a pixel differs
when one of its colors differs by more than PIXEL_TOLERANCE, and a shot differs when more than --threshold percent
of its pixels differ (default 0.01%: about 130 pixels of a 1440 x 900 window, as one changed word; two loads of
one page on one computer draw the same pixels, so the threshold is for small noise only). It prints on the Monitoring Plugins API, so a plugin check runs it like any other plugin:

  VISUAL OK|WARNING|CRITICAL|UNKNOWN - <summary> | <shot>=<percent>%;<threshold> ...
  <shot>: OK|WARNING|CRITICAL <what>          (one line for each shot: a subject when the check has --subject on)
  {"subjects": {...}, "shots": {...}, "approve": "maxpm ops visual approve <name>"}

and exits 0 (every shot matches), 1 (a shot has no baseline yet), 2 (a shot differs) or 3 (the page or Chrome
failed). The files are beside the queue, in <queue folder>/visual/<name>/: base-<shot>.png (the approved one),
new-<shot>.png (the last run) and diff-<shot>.png (the new one, faded, with the pixels that differ in red), and
last.json (the last run). A release that changes a page on purpose shows as CRITICAL; a person or the manager looks
at the old, the new and the diff (maxpm ops visual show <name>) and approves the new one: maxpm ops visual approve
<name> [--shot <shot>]. Four shots take some seconds: give the plugin check --timeout 2m. --mask hides the parts that change on each load (a clock, a count) before the screenshot."""

import base64
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from .. import core
from ..core import RiverError

PIXEL_TOLERANCE = 24   # the largest difference of one color (0-255) that still counts as the same pixel
HEIGHT = {"desktop": 900, "phone": 844}  # the height of the window, for a screenshot of the window only
PHONE_BELOW = 760      # a width below it is a phone: a touch screen, as the page's own break (phone.css)
FULL_MAX = 8000        # the longest full page, in CSS pixels
START_WAIT = 20        # seconds for Chrome to open its debugging port
SHOOT = None           # a test hook: (url, shots, opts) -> {shot: png bytes}; raises RiverError when the page fails
COMPARE = None         # a test hook: (base png, new png) -> {"ratio", "diff_png", "size"}
STATUS = {0: "OK", 1: "WARNING", 2: "CRITICAL", 3: "UNKNOWN"}
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def folder(name):
    """The folder of a visual check's pictures, beside the queue's database (a test queue keeps them there too)."""
    if not _NAME.match(name or ""):
        raise RiverError("a visual check name uses lower-case letters, digits, '.', '_', '-'")
    return core.db_path().parent / "visual" / name


def parse_shots(widths, themes):
    """[(shot, width, theme)] of the widths (a comma list of CSS pixels) and the themes (light, dark)."""
    try:
        ws = [int(w) for w in str(widths).split(",") if w.strip()]
    except ValueError:
        raise RiverError(f"bad --widths {widths!r}: CSS pixels, for example 1440,390")
    ts = [t.strip() for t in str(themes).split(",") if t.strip()]
    if not ws or any(w < 200 or w > 4000 for w in ws):
        raise RiverError(f"bad --widths {widths!r}: each one from 200 to 4000 CSS pixels")
    if not ts or any(t not in ("light", "dark") for t in ts):
        raise RiverError(f"bad --themes {themes!r}: light, dark, or both (light,dark)")
    return [(f"{w}-{t}", w, t) for w in ws for t in ts]


# ---------------------------------------------------------------- Chrome

class _Tab:
    """One page of the Chrome of this run, over its own CDP WebSocket: call() waits for the answer of its id, and
    keeps the events that came before it for wait_event()."""

    def __init__(self, ws_url):
        from ..browser import WS
        self.ws, self.n, self.events = WS(ws_url), 0, []

    def _recv(self, until):
        left = until - time.monotonic()
        if left <= 0:
            raise RiverError("Chrome did not answer in time")
        self.ws.sock.settimeout(left)
        try:
            return json.loads(self.ws.recv())
        except socket.timeout:
            raise RiverError("Chrome did not answer in time")

    def call(self, method, params=None, timeout=30):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params or {}}))
        until = time.monotonic() + timeout
        while True:
            m = self._recv(until)
            if m.get("id") == self.n:
                if "error" in m:
                    raise RiverError(f"Chrome refused {method}: {m['error'].get('message')}")
                return m.get("result") or {}
            if "method" in m:
                self.events.append(m)

    def wait_event(self, name, timeout):
        until = time.monotonic() + timeout
        while True:
            for i, m in enumerate(self.events):
                if m["method"] == name:
                    return self.events.pop(i)
            m = self._recv(until)
            if "method" in m:
                self.events.append(m)

    def close(self):
        self.ws.close()


class _Chrome:
    """A headless Chrome with a new temporary profile, for one run; close() ends it and removes the profile."""

    def __init__(self):
        from ..browser import chrome_path
        chrome = chrome_path()
        if not chrome:
            raise RiverError("no Google Chrome on this computer: install it, or set MAXPM_CHROME to Chrome or Chromium")
        self.dir = Path(tempfile.mkdtemp(prefix="maxpm-visual-"))
        args = [chrome, "--headless=new", "--hide-scrollbars", "--mute-audio", f"--user-data-dir={self.dir}",
                "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1", "--no-first-run",
                "--no-default-browser-check", "--disable-default-apps", "--disable-sync", "--force-color-profile=srgb",
                "--font-render-hinting=none", "about:blank"]
        # Chrome stays in the process group of this command: when maxpm serve stops a run at the check's timeout,
        # it stops the group, and Chrome with it.
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        end, port = time.monotonic() + START_WAIT, None
        while time.monotonic() < end and port is None:
            try:
                port = int((self.dir / "DevToolsActivePort").read_text(encoding="utf-8").split()[0])
            except (OSError, ValueError, IndexError):
                if self.proc.poll() is not None:
                    self.close()
                    raise RiverError(f"Google Chrome ended at its start (exit {self.proc.returncode})")
                time.sleep(0.1)
        if port is None:
            self.close()
            raise RiverError(f"Google Chrome did not open its debugging port within {START_WAIT}s")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(50):
            with opener.open(f"http://127.0.0.1:{port}/json/list", timeout=5) as r:
                pages = [t for t in json.loads(r.read()) if t.get("type") == "page"]
            if pages:
                break
            time.sleep(0.1)
        else:
            self.close()
            raise RiverError("Google Chrome opened no page")
        self.tab = _Tab(pages[0]["webSocketDebuggerUrl"])

    def close(self):
        if getattr(self, "tab", None):
            self.tab.close()
        try:  # its helper processes end with it
            self.proc.terminate()
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(5)
        except OSError:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)


# The style that makes two loads of one page draw the same: no animation, no blinking caret, and the --mask parts
# hidden (they keep their place, so nothing beside them moves).
_STILL = ("*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important}")


def shoot(url, shots, opts):
    """{shot: png bytes} of each (shot, width, theme). opts: wait (seconds after the load), full (the whole page),
    masks (CSS selectors to hide), timeout (seconds for a load)."""
    if SHOOT:
        return SHOOT(url, shots, opts)
    c = _Chrome()
    try:
        t, out = c.tab, {}
        t.call("Page.enable")
        css = _STILL + "".join(f"{m}{{visibility:hidden!important}}" for m in opts.get("masks") or [])
        for shot, width, theme in shots:
            phone = width < PHONE_BELOW
            height = HEIGHT["phone" if phone else "desktop"]
            t.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1,
                                                          "mobile": phone})
            t.call("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-color-scheme", "value": theme},
                                                               {"name": "prefers-reduced-motion", "value": "reduce"}]})
            t.events.clear()
            nav = t.call("Page.navigate", {"url": url})
            if nav.get("errorText"):
                raise RiverError(f"the page did not open at {width} px: {nav['errorText']}")
            t.wait_event("Page.loadEventFired", opts.get("timeout", 30))
            t.call("Runtime.evaluate", {"expression": "(() => { const s = document.createElement('style'); "
                                                      f"s.textContent = {json.dumps(css)}; "
                                                      "document.documentElement.append(s); })()"})
            time.sleep(opts.get("wait", 1.0))
            params = {"format": "png"}
            if opts.get("full"):
                size = t.call("Page.getLayoutMetrics").get("cssContentSize") or {}
                params.update(captureBeyondViewport=True, clip={"x": 0, "y": 0, "width": width, "scale": 1,
                                                                "height": min(FULL_MAX, max(height, int(size.get("height") or height)))})
            out[shot] = base64.b64decode(t.call("Page.captureScreenshot", params, timeout=60)["data"])
        return out
    finally:
        c.close()


# Chrome compares the two pictures: each pixel whose largest color difference is above the tolerance counts, and
# the diff picture is the new one, faded, with those pixels in red. Pictures of two sizes differ in every pixel
# outside the part they share.
_COMPARE_JS = """(async (a, b, tol) => {
  const load = (src) => new Promise((ok, no) => { const i = new Image(); i.onload = () => ok(i); i.onerror = () => no(new Error('not a picture')); i.src = src; });
  const [x, y] = await Promise.all([load(a), load(b)]);
  const w = Math.max(x.width, y.width), h = Math.max(x.height, y.height);
  const data = (img) => { const c = document.createElement('canvas'); c.width = w; c.height = h; const g = c.getContext('2d'); g.drawImage(img, 0, 0); return g.getImageData(0, 0, w, h); };
  const p = data(x).data, q = data(y), r = q.data;
  let changed = 0;
  for (let k = 0; k < r.length; k += 4) {
    const px = (k / 4) % w, py = Math.floor(k / 4 / w);
    const out = px >= x.width || py >= x.height || px >= y.width || py >= y.height;
    const d = out ? 255 : Math.max(Math.abs(p[k] - r[k]), Math.abs(p[k + 1] - r[k + 1]), Math.abs(p[k + 2] - r[k + 2]), Math.abs(p[k + 3] - r[k + 3]));
    if (d > tol) { changed++; r[k] = 230; r[k + 1] = 0; r[k + 2] = 0; r[k + 3] = 255; }
    else { r[k] = 255 - (255 - r[k]) / 4; r[k + 1] = 255 - (255 - r[k + 1]) / 4; r[k + 2] = 255 - (255 - r[k + 2]) / 4; r[k + 3] = 255; }
  }
  const c = document.createElement('canvas'); c.width = w; c.height = h; c.getContext('2d').putImageData(q, 0, 0);
  return { changed, total: w * h, size: [x.width, x.height, y.width, y.height], diff: c.toDataURL('image/png').split(',')[1] };
})"""


def compare(pairs):
    """{shot: {"ratio", "diff_png", "size"}} of {shot: (base png, new png)}, in one Chrome."""
    if COMPARE:
        return {s: COMPARE(a, b) for s, (a, b) in pairs.items()}
    if not pairs:
        return {}
    c = _Chrome()
    try:
        out = {}
        for shot, (a, b) in pairs.items():
            uri = lambda png: "data:image/png;base64," + base64.b64encode(png).decode()
            res = c.tab.call("Runtime.evaluate", {"expression": f"{_COMPARE_JS}({json.dumps(uri(a))}, "
                                                                f"{json.dumps(uri(b))}, {PIXEL_TOLERANCE})",
                                                  "awaitPromise": True, "returnByValue": True}, timeout=120)
            if res.get("exceptionDetails"):
                raise RiverError(f"the comparison of {shot} failed: {res['exceptionDetails'].get('text')}")
            v = res["result"]["value"]
            out[shot] = {"ratio": v["changed"] / v["total"] if v["total"] else 1.0,
                         "diff_png": base64.b64decode(v["diff"]), "size": v["size"]}
        return out
    finally:
        c.close()


# ---------------------------------------------------------------- run, show, approve

def run(name, url, widths="1440,390", themes="light,dark", threshold=0.01, wait=1.0, full=False, masks=(),
        timeout=30):
    """One run: the screenshots, the comparison with the baselines, the files. Returns the result that lines()
    prints: {"status": 0..3, "summary", "shots": {shot: {...}}, "perf", "name"}."""
    d = folder(name)
    shots = parse_shots(widths, themes)
    if not re.match(r"^(https?|data|file):", url or ""):
        raise RiverError("--url needs an http:// or https:// address")
    if threshold < 0 or threshold > 100:
        raise RiverError("--threshold is a percent of the pixels, from 0 to 100")
    d.mkdir(parents=True, exist_ok=True)
    try:
        pics = shoot(url, shots, {"wait": wait, "full": full, "masks": list(masks), "timeout": timeout})
    except (RiverError, OSError, EOFError) as e:
        return {"name": name, "status": 3, "summary": f"{url} could not be shot: {e}", "shots": {}, "perf": ""}
    pairs, res = {}, {}
    for shot, _, _ in shots:
        (d / f"new-{shot}.png").write_bytes(pics[shot])
        base = d / f"base-{shot}.png"
        res[shot] = {"baseline": str(base) if base.exists() else None, "new": str(d / f"new-{shot}.png"),
                     "diff_image": None, "diff": None}
        if base.exists():
            pairs[shot] = (base.read_bytes(), pics[shot])
    try:
        cmp = compare(pairs)
    except (RiverError, OSError, EOFError) as e:
        return {"name": name, "status": 3, "summary": f"the comparison failed: {e}", "shots": res, "perf": ""}
    for shot, c in cmp.items():
        (d / f"diff-{shot}.png").write_bytes(c["diff_png"])
        res[shot].update(diff=round(c["ratio"] * 100, 3), diff_image=str(d / f"diff-{shot}.png"),
                         size=c["size"])
    for shot, r in res.items():
        r["status"] = (1 if r["diff"] is None else 2 if r["diff"] > threshold else 0)
    differ = [s for s, r in res.items() if r["status"] == 2]
    new = [s for s, r in res.items() if r["status"] == 1]
    status = 2 if differ else 1 if new else 0
    if differ:
        summary = (f"{len(differ)} of {len(res)} shots differ from the approved ones: "
                   + ", ".join(f"{s} {res[s]['diff']:.2f}%" for s in differ) + f" (threshold {threshold}%)")
    elif new:
        summary = f"no approved picture of {', '.join(new)}: look at the new ones, then maxpm ops visual approve {name}"
    else:
        summary = f"{len(res)} shots match the approved ones (threshold {threshold}%)"
    perf = " ".join(f"{s}={r['diff']}%;{threshold}" for s, r in res.items() if r["diff"] is not None)
    out = {"name": name, "url": url, "status": status, "summary": summary, "shots": res, "perf": perf,
           "threshold": threshold, "at": core.iso(core.now())}
    (d / "last.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


def lines(res):
    """The output of a run on the Monitoring Plugins API (module docstring)."""
    head = f"VISUAL {STATUS[res['status']]} - {res['summary']}" + (f" | {res['perf']}" if res.get("perf") else "")
    out, subjects = [head], {}
    for shot, r in res["shots"].items():
        st = r.get("status", 3)
        text = ("no approved picture yet" if st == 1 else f"{r['diff']:.2f}% of the pixels differ" if r.get("diff") is not None
                else "not compared")
        out.append(f"{shot}: {STATUS[st]} {text}")
        subjects[shot] = text if st else "matches"
    if res["shots"]:
        out.append(json.dumps({"subjects": {s: t for s, t in subjects.items() if res["shots"][s].get("status")},
                               "shots": res["shots"], "approve": f"maxpm ops visual approve {res['name']}"}))
    return out


def show(name):
    """The last run of a visual check, with the three pictures of each shot (maxpm ops visual show)."""
    d = folder(name)
    try:
        last = json.loads((d / "last.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RiverError(f"the visual check {name!r} has not run yet: maxpm ops visual run {name} --url <address>")
    return last


def approve(conn, name, shots=None, actor=None, note=None):
    """The new pictures become the approved ones: of every shot of the last run, or of the shots named. Only a person
    or the manager approves: a new baseline decides how the page should look."""
    if actor and not core._is_person(conn, actor) and not core._is_manager(conn, actor):
        raise RiverError(f"{actor} is an agent; only a person or the manager approves how a page looks. Ask the "
                         f"manager: maxpm --as {actor} ask manager \"approve the new pictures of {name}: maxpm ops "
                         f"visual show {name}\"")
    last = show(name)
    d = folder(name)
    want = list(last["shots"]) if not shots else shots
    unknown = [s for s in want if s not in last["shots"]]
    if unknown:
        raise RiverError(f"no shot {', '.join(unknown)} in the last run of {name}; its shots: {', '.join(last['shots'])}")
    done = []
    for s in want:
        new = d / f"new-{s}.png"
        if not new.exists():
            raise RiverError(f"no new picture of {s}: run the check again (maxpm ops visual run {name} ...)")
        shutil.copyfile(new, d / f"base-{s}.png")
        done.append(f"{s} (was {'new' if last['shots'][s].get('diff') is None else str(last['shots'][s]['diff']) + '%'})")
    with core.tx(conn):
        core._event(conn, None, actor, f"ops visual {name}: approved the pictures of {', '.join(done)}"
                    + (f" ({note.strip()})" if (note or "").strip() else ""))
    return {"name": name, "approved": want, "folder": str(d)}
