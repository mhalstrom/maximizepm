"""The page's own time in headless Chrome, on a copy of a queue (#5556): a click to the next drawn frame.

    python3 scripts/pagetime.py [--from ~/.maximizepm/maxpm.db] [--width 1280] [--runs 5]

It copies the queue, serves the page from this folder on a free local port, opens it in headless Chrome, and prints
the median of each step: the state read, a refresh, an action (its POST alone, and with the refresh after it), each
tab, each Plan view, the Items view after a change that came while it was hidden, and the first open of Items and
Done. 'long' lists the frames above 50 ms that Chrome reports for the step (long-animation-frame). A step above
100 ms is marked OVER (mark 2026-11-12: every page action answers in under 100 ms). Check a phone too: --width 390
(below 500 px the page loads in an iframe of that width, since headless Chrome makes no narrower window).
It needs a local port, so it does not run in an agent's sandbox; it changes only the copy.
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
LIMIT = 100

DRIVE = r"""
const post = (o) => fetch("/__result", { method: "POST", body: JSON.stringify(o) });
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const frame = () => new Promise(r => requestAnimationFrame(() => setTimeout(r, 0)));
const med = (a) => { const b = [...a].sort((x, y) => x - y); return Math.round(b[b.length >> 1]); };
const q = (s) => document.querySelector(s);
const tab = (t) => q(`nav button[data-tab="${t}"]`) || q(`[data-tab="${t}"]`), view = (v) => q(`[data-plan-view="${v}"]`);
const RUNS = __RUNS__, ITEM = __ITEM__;
let loaf = [];
new PerformanceObserver(l => { for (const e of l.getEntries()) loaf.push(Math.round(e.duration)); }).observe({ type: "long-animation-frame" });
const out = {};
// One step: prepare, then time act() to the next frame; the frames above 50 ms that come within 300 ms after it.
async function step(name, prepare, act, runs = RUNS) {
  const v = [], long = [];
  for (let k = 0; k < runs; k++) {
    await prepare(); await frame(); await sleep(300);
    loaf = []; const t = performance.now(); await act(); await frame(); v.push(performance.now() - t);
    await sleep(300); long.push(...loaf);
  }
  out[name] = { ms: med(v), long };
}
try {
  for (let k = 0; k < 600 && q("#bootNote"); k++) await sleep(100);
  await sleep(1000);
  const lib = await import("/lib.js");
  const edit = (what) => lib.act("item_edit", { id: ITEM, context: what + " " + Math.random() });
  await step("first open: Items", () => { tab("plan").click(); view("overview").click(); }, () => view("items").click(), 1);
  await step("first open: Done", () => view("overview").click(), () => view("done").click(), 1);
  await step("state read", () => {}, async () => (await fetch("api/state")).json());
  await step("refresh, nothing new", () => {}, () => lib.hooks.refresh());
  await step("action: POST only", () => {}, () => fetch("api/action", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ op: "item_edit", args: { id: ITEM, context: "x" + Math.random() }, actor: lib.actor() }) }));
  await step("action with its refresh", () => {}, () => edit("speed"));
  for (const t of ["sessions", "plan", "settings", "terminal"])
    await step("tab: " + t, () => tab(t === "sessions" ? "plan" : "sessions").click(), () => tab(t).click());
  tab("plan").click();
  for (const v of ["overview", "items", "goals", "graph", "releases", "ops", "done", "health"])
    if (view(v)) await step("view: " + v, () => view(v === "overview" ? "items" : "overview").click(), () => view(v).click());
  await step("Items after a hidden change", async () => { view("overview").click(); await edit("hidden"); }, () => view("items").click());
  await post(out);
} catch (e) { await post({ error: String(e && e.stack || e) }); }
"""


def chrome():
    for c in (os.environ.get("MAXPM_CHROME"), "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("chromium-browser")):
        if c and os.path.exists(c):
            return c
    sys.exit("no Chrome on this computer: MAXPM_CHROME names one")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--from", dest="src", default=os.path.expanduser("~/.maximizepm/maxpm.db"), help="the queue to copy")
    ap.add_argument("--width", type=int, default=1280, help="the window width (a phone: 390)")
    ap.add_argument("--runs", type=int, default=5)
    a = ap.parse_args()
    d = tempfile.mkdtemp()
    db = os.path.join(d, "q.db")
    src, dst = sqlite3.connect(a.src), sqlite3.connect(db)
    src.backup(dst)
    src.close(), dst.close()
    os.environ["MAXPM_DB"] = db
    from river import core, server
    conn = core.connect(db)
    item = next(i for i in core.item_list(conn) if i["status"] == "open")["id"]
    person = conn.execute("SELECT name FROM agents WHERE kind='human' ORDER BY name LIMIT 1").fetchone()
    conn.close()
    drive = DRIVE.replace("__RUNS__", str(a.runs)).replace("__ITEM__", str(item)).encode()
    page = server.page_html("/").replace(b"</body>", b'<script type="module" src="/__drive.js"></script></body>')
    got, result = threading.Event(), {}
    page_url = "/" + (f"?as={person[0]}" if person else "")

    class H(server.Handler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            p = self.path.split("?")[0]
            if p == "/":
                return self._send(200, page, "text/html; charset=utf-8")
            if p == "/__frame":  # a phone: headless Chrome makes no window narrower than 500 px (#5415)
                return self._send(200, (f'<!doctype html><meta charset="utf-8"><style>html,body{{margin:0}}iframe{{border:0;'
                                        f'display:block}}</style><iframe src="{page_url}" width="{a.width}" height="844">'
                                        f'</iframe>').encode(), "text/html; charset=utf-8")
            if p == "/__drive.js":
                return self._send(200, drive, "text/javascript; charset=utf-8")
            return super().do_GET()

        def do_POST(self):
            if self.path == "/__result":
                result.update(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                got.set()
                return self._send(204, b"")
            return super().do_POST()

    httpd = server._Server(("127.0.0.1", 0), H)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    framed = a.width < 500
    url = base + "/__frame" if framed else base + page_url
    p = subprocess.Popen([chrome(), "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                          "--disable-extensions", f"--window-size={800 if framed else a.width},950",
                          f"--user-data-dir={os.path.join(d, 'p')}", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ok = got.wait(300)
    finally:
        p.kill()
        p.wait()
        httpd.shutdown()
        shutil.rmtree(d, ignore_errors=True)
    if not ok or "error" in result:
        sys.exit(f"the page did not finish: {result.get('error', 'no answer in 300 s')}")
    print(f"width {a.width} px" + (" (in an iframe)" if framed else "") + f", median of {a.runs} runs (ms; long: frames above 50 ms)")
    for name, r in result.items():
        print(f"  {name:32} {r['ms']:>5}" + ("  OVER" if r["ms"] > LIMIT else "") + (f"  long {r['long']}" if r["long"] else ""))


if __name__ == "__main__":
    main()
