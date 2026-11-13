"""How smooth the Browser pane is (#5637): frames per second while a page moves, and the time from a click or a key
to the first frame after it. #5651: the turn of a scroll (down hard, then up), a hover, and a key while the person
drags, with the pane's way to send input before #5651 (one request at a time) and after (numbered, at once). It runs a maxpm serve (in this process, on a free port, on an empty queue in a
temporary folder) with a real headless Chrome, and a client that asks for frames and sends input as the pane does.

    python3 seed/browser_speed.py                       # this Mac: the pane over 127.0.0.1
    python3 seed/browser_speed.py --rtt 120 --mbps 20   # a phone over the relay: 120 ms there and back, 20 Mbit/s
    python3 seed/browser_speed.py --code <folder>       # the river of another checkout (before and after a change)

--rtt adds half the round trip before each request and half after its answer; --mbps lets the answers through one
link of that speed, with the relay's base64 (4/3 of the bytes; a JSON answer counts gzipped, as the relay sends it).
The client reads frames the way the pane of that code does: one long poll at a time (JSON with base64), or, when
serve answers in binary (#5637), as many polls at once as the pane keeps. It does not decode the JPEG or draw it.
It needs Google Chrome and a local port (outside an agent sandbox)."""

import argparse
import base64
import gzip
import http.client
import http.server
import itertools
import json
import os
import statistics
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--code", default=str(Path(__file__).resolve().parent.parent))
ap.add_argument("--rtt", type=float, default=0.0, help="ms there and back (a phone over the relay)")
ap.add_argument("--mbps", type=float, default=0.0, help="Mbit/s of the answers' link (0: no limit)")
ap.add_argument("--seconds", type=float, default=4.0)
ap.add_argument("--dpr", type=float, default=2.0)
ap.add_argument("--size", default="760x900", help="the pane's CSS px")
opt = ap.parse_args()

tmp = tempfile.mkdtemp(prefix="maxpm-speed-")
os.environ["MAXPM_DB"] = os.path.join(tmp, "q.db")
sys.path.insert(0, opt.code)
from river import browser, core, server  # noqa: E402

# ---- the pages: a long page to scroll, a moving box, and a box that changes its color at a press or a key

LONG = ("<!doctype html><title>long</title><style>body{margin:0;font:18px sans-serif}p{margin:0;padding:12px 20px}"
        "p:nth-child(odd){background:#e8eef8}</style>"
        + "".join(f"<p>Line {i}: some words of a long page that scrolls, {i * 7919 % 10007}</p>" for i in range(3000)))
ANIM = ("<!doctype html><title>anim</title><style>body{margin:0}#b{width:120px;height:120px;background:#c33;"
        "position:absolute;top:200px;animation:m 2s linear infinite alternate}@keyframes m{from{left:0}to{left:600px}}"
        "</style><div id=b></div>")
CLICK = ("<!doctype html><title>click</title><style>body{margin:0;background:#fff}#t{position:absolute;inset:0}"
         "</style><div id=t></div><input id=q style='position:absolute;top:10px;left:10px'><script>"
         "let n=0;const t=document.getElementById('t');"
         "addEventListener('mousedown',e=>{if(e.target.id!=='q'){e.preventDefault();n++;t.style.background=n%2?'#36c':'#fff'}});"
         "</script>")
# A key changes the color; a drag changes nothing (no text to select), so the first frame after a key is the key's.
KEYS = ("<!doctype html><title>keys</title><style>html,body{margin:0;height:100%;background:#fff;user-select:none}"
        "</style><script>let n=0;addEventListener('keydown',()=>{n++;document.body.style.background=n%2?'#3a6':'#fff'});"
        "</script>")
HOVER = ("<!doctype html><title>hover</title><style>body{margin:0}#h{position:absolute;left:200px;top:200px;"
         "width:200px;height:200px;background:#ddd}#h:hover{background:#c63}</style><div id=h></div>")
PAGES = {"/long": LONG, "/anim": ANIM, "/click": CLICK, "/keys": KEYS, "/hover": HOVER}


class Pages(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path, "").encode()
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class Serve(server._Server):
    def service_actions(self):
        pass  # no notify loop: this measures the page's own routes


pages = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Pages)
threading.Thread(target=pages.serve_forever, daemon=True).start()
srv = Serve(("127.0.0.1", 0), server.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT, WEB = srv.server_address[1], pages.server_address[1]
conn = core.connect()
core.project_add(conn, "speed")
core.config_set(conn, "browser_allow", f"127.0.0.1:{WEB}", project="speed")

# ---- the link: the round trip and the speed of a phone over the relay

link = threading.Lock()


def through(n_bytes, ctype):
    """The answer's time on the link: base64 in the relay's frames; JSON gzipped first."""
    if opt.mbps <= 0:
        return
    with link:
        time.sleep(n_bytes * 4 / 3 * 8 / (opt.mbps * 1e6))


def request(method, path, body=None, headers=None):
    if opt.rtt:
        time.sleep(opt.rtt / 2000)
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=60)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    ctype = r.getheader("Content-Type") or ""
    c.close()
    wire = len(gzip.compress(data, 1)) if "json" in ctype else len(data)
    through(wire, ctype)
    if opt.rtt:
        time.sleep(opt.rtt / 2000)
    return r.status, ctype, data, wire


TOKEN = json.loads(request("GET", "/api/token")[2])["token"]


def act(op, args):
    st, _, data, _ = request("POST", "/api/action", json.dumps({"op": op, "args": args, "actor": ""}).encode(),
                             {"Content-Type": "application/json", "X-Maxpm-Token": TOKEN})
    if st != 200:
        raise SystemExit(f"{op} {args.get('action', '')}: {st} {data[:300]!r}")
    return json.loads(data)["result"]


# ---- the client: frames as the pane asks for them

PANE = "speedpane"
W, H = (int(x) for x in opt.size.split("x"))


def jpeg_size(d):
    """(width, height) of a JPEG, from its frame header."""
    i = 2
    while i + 9 < len(d):
        m, n = d[i + 1], struct.unpack(">H", d[i + 2:i + 4])[0]
        if m in (0xC0, 0xC1, 0xC2):
            return struct.unpack(">HH", d[i + 5:i + 9])[::-1]
        i += 2 + n
    return None


class Frames:
    def __init__(self):
        self.lock = threading.Lock()
        self.seq = 0
        self.got = []  # (time received, seq, bytes on the link, sharp)
        self.stop = False
        self.binary = None
        self.window = 1

    def poll_once(self):
        t = time.time()
        st, ctype, data, wire = request(
            "GET", f"/api/browser/frame?project=speed&after={self.seq}&pane={PANE}&token={TOKEN}&bin=1")
        if st != 200:
            time.sleep(0.5)
            return
        now = time.time()
        if ctype.startswith("application/octet-stream"):
            self.binary = True
            n = struct.unpack(">I", data[:4])[0]
            head, jpeg = json.loads(data[4:4 + n]), data[4 + n:]
            # The pane's rule (browserTool.js): one poll for each 30 ms of the link's round trip, at most four.
            self.trips = (getattr(self, "trips", []) + [(now - t) * 1000 - head.get("waited", 0)])[-20:]
            self.window = max(1, min(4, -(-int(min(self.trips)) // 30)))
        else:
            self.binary = False
            head = json.loads(data)
            jpeg = (head.get("frame") or {}).get("data")
            jpeg = base64.b64decode(jpeg) if jpeg else None
        with self.lock:
            if jpeg and head.get("seq", 0) > self.seq:
                size = jpeg_size(jpeg)  # a sharp frame has more pixels than the pane's CSS px
                self.got.append((now, head["seq"], wire, bool(size and size[0] > W)))
            self.seq = max(self.seq, head.get("seq", 0))

    def run(self):
        def one(i):
            while not self.stop:
                if i >= self.window:  # the window became smaller
                    time.sleep(0.05)
                    continue
                self.poll_once()
        for i in range(4):
            threading.Thread(target=one, args=(i,), daemon=True).start()

    def since(self, t0, t1=None):
        with self.lock:
            return [g for g in self.got if g[0] >= t0 and (t1 is None or g[0] <= t1)]


BATCHES = itertools.count(1)


class Sender:
    """The pane's input requests. at_once False: one request at a time, the input that comes meanwhile in the next one
    (the pane before #5651). True: each batch at once with its number, at most four on the way (browserTool.js)."""

    def __init__(self, at_once):
        self.at_once, self.pending, self.flying = at_once, [], 0
        self.lock = threading.Lock()

    def send(self, ev):
        with self.lock:
            last = self.pending[-1] if self.pending else {}
            act_, prev = ev.get("action"), last.get("action")
            if self.at_once and act_ == prev == "move":  # the pane's rules (#5651): a move replaces the one before
                self.pending[-1] = ev
            elif self.at_once and act_ == prev == "wheel":  # a wheel step adds up; a step the other way replaces
                back = ev["dy"] * last["dy"] < 0 or ev["dx"] * last["dx"] < 0
                self.pending[-1] = ev if back else {**ev, "dx": last["dx"] + ev["dx"], "dy": last["dy"] + ev["dy"]}
            else:
                self.pending.append(ev)
        self.flush()

    def flush(self):
        with self.lock:
            while self.pending and self.flying < (4 if self.at_once else 1):
                evs, self.pending = self.pending, []
                self.flying += 1
                args = {"project": "speed", "pane": PANE, "evs": evs}
                if self.at_once:
                    args["n"] = next(BATCHES)  # one count for the pane, as the pane has: serve drops an old number
                threading.Thread(target=self._go, args=(args,), daemon=True).start()

    def _go(self, args):
        try:
            act("browser_input", args)
        finally:
            with self.lock:
                self.flying -= 1
            self.flush()


def wait_quiet(fr, quiet=0.8, most=6):
    """Until no frame came for `quiet` seconds (the sharp frame of the last change is in)."""
    end = time.time() + most
    while time.time() < end:
        last = fr.got[-1][0] if fr.got else 0
        if time.time() - last > quiet:
            return
        time.sleep(0.05)


def main():
    act("browser", {"action": "start", "project": "speed"})
    act("browser", {"action": "resize", "project": "speed", "pane": PANE, "w": W, "h": H, "dpr": opt.dpr,
                    "screen": [1512, 982]})
    fr = Frames()
    first = request("GET", f"/api/browser/frame?project=speed&after=0&pane={PANE}&token={TOKEN}&bin=1&wait=1")
    binary = first[1].startswith("application/octet-stream")
    fr.run()
    out = {"code": opt.code, "rtt_ms": opt.rtt, "mbps": opt.mbps, "pane": f"{W}x{H}@{opt.dpr}",
           "transport": "binary" if binary else "json"}

    # 1. A long page scrolled by the wheel, 60 wheel events a second, sent as the pane of that code sends them.
    new = hasattr(browser, "Inputs")  # the code has #5651: numbered batches at once
    out["pane_rule"] = "at once (#5651)" if new else "one request at a time"
    act("browser", {"action": "open", "project": "speed", "pane": PANE, "url": f"http://127.0.0.1:{WEB}/long"})
    wait_quiet(fr)
    t0 = time.time()
    s = Sender(new)
    while time.time() < t0 + opt.seconds:
        s.send({"type": "mouse", "action": "wheel", "x": 300, "y": 300, "dx": 0, "dy": 40})
        time.sleep(1 / 60)
    t1 = time.time()
    got = fr.since(t0, t1)
    time.sleep(0.5)
    out["scroll"] = {"fps": round(len(got) / (t1 - t0), 1), "frames": len(got),
                     "sharp_frames": sum(1 for g in got if g[3]),
                     "kb_per_s": round(sum(g[2] for g in got) / 1024 / (t1 - t0)),
                     "kb_per_frame": round(sum(g[2] for g in got) / 1024 / max(1, len(got)), 1)}

    # 1b. A turn (#5651, mark: 'if I scroll down and then scroll up it will continue scrolling down'): the wheel hard
    # down for a second, then up. turn_ms: from the first step up until the page scrolls up (its scrollY, read in
    # serve's Chrome); frame_ms: until the first frame after that turn reaches the client.
    b = browser.find("speed", None)
    turns = []
    for _ in range(3):
        act("browser", {"action": "open", "project": "speed", "pane": PANE, "url": f"http://127.0.0.1:{WEB}/long"})
        wait_quiet(fr)
        s = Sender(new)
        end = time.time() + 1
        while time.time() < end:
            s.send({"type": "mouse", "action": "wheel", "x": 300, "y": 300, "dx": 0, "dy": 120})
            time.sleep(1 / 60)
        top = b.evaluate("scrollY")
        t = time.time()
        turned = None
        while time.time() < t + 3:
            if turned is None:
                s.send({"type": "mouse", "action": "wheel", "x": 300, "y": 300, "dx": 0, "dy": -120})
            y = b.evaluate("scrollY")
            if turned is None and y < top:
                turned = time.time()
            top = max(top, y)
            if turned and time.time() > turned + 0.3:
                break
            time.sleep(1 / 60)
        if turned:
            after = [g[0] for g in fr.since(turned)]
            turns.append(((turned - t) * 1000, ((after[0] if after else turned) - t) * 1000))
        time.sleep(0.5)
    if turns:
        out["scroll_turn_ms"] = {"turn": sorted(round(x[0]) for x in turns),
                                 "frame": sorted(round(x[1]) for x in turns)}

    # 1c. A hover (#5651, mark: 'I am also not seeing hover effects'): a move with no button onto a box whose :hover
    # changes its color; the time to the first frame after it. The wheel before left the pointer in the box: the
    # first move goes out of it.
    act("browser", {"action": "open", "project": "speed", "pane": PANE, "url": f"http://127.0.0.1:{WEB}/hover"})
    wait_quiet(fr)
    hov = []
    for i in range(6):
        wait_quiet(fr, 0.6)
        with fr.lock:
            before = fr.seq
        t = time.time()
        threading.Thread(target=act, daemon=True, args=("browser_input", {"project": "speed", "pane": PANE, "evs": [
            {"type": "mouse", "action": "move", "x": 700 if i % 2 == 0 else 300, "y": 300}]})).start()  # out, in, ...
        while time.time() < t + 3:
            with fr.lock:
                g = [g for g in fr.got if g[1] > before]
            if g:
                hov.append(round((g[0][0] - t) * 1000))
                break
            time.sleep(0.002)
    out["hover_to_frame_ms"] = {"ms": sorted(hov), "of": 6}  # fewer than 6: a hover that showed no change

    # 2. A page that moves by itself (a box that slides: a video, an animation).
    act("browser", {"action": "open", "project": "speed", "pane": PANE, "url": f"http://127.0.0.1:{WEB}/anim"})
    time.sleep(1)
    t0 = time.time()
    time.sleep(opt.seconds)
    t1 = time.time()
    got = fr.since(t0, t1)
    out["animation"] = {"fps": round(len(got) / (t1 - t0), 1), "sharp_frames": sum(1 for g in got if g[3]),
                        "kb_per_s": round(sum(g[2] for g in got) / 1024 / (t1 - t0)),
                        "kb_per_frame": round(sum(g[2] for g in got) / 1024 / max(1, len(got)), 1)}

    # 3. A click, a key and a wheel step: the time from the input request's start to the first frame after it.
    lat = {"click": [], "key": [], "wheel": []}

    def first_frame(kind, evs):
        wait_quiet(fr, 0.6)
        with fr.lock:
            before = fr.seq
        t = time.time()
        threading.Thread(target=act, args=("browser_input", {"project": "speed", "pane": PANE, "evs": evs}),
                         daemon=True).start()
        end = t + 3
        while time.time() < end:
            with fr.lock:
                new = [g for g in fr.got if g[1] > before]
            if new:
                lat[kind].append((new[0][0] - t) * 1000)
                return
            time.sleep(0.002)

    act("browser", {"action": "open", "project": "speed", "pane": PANE, "url": f"http://127.0.0.1:{WEB}/long"})
    wait_quiet(fr)
    for i in range(8):
        first_frame("wheel", [{"type": "mouse", "action": "wheel", "x": 300, "y": 300, "dx": 0, "dy": 120}])
    act("browser", {"action": "open", "project": "speed", "pane": PANE, "url": f"http://127.0.0.1:{WEB}/click"})
    wait_quiet(fr)
    press = [{"type": "mouse", "action": a, "x": 30, "y": 20, "button": "left", "clicks": 1} for a in ("down", "up")]
    act("browser_input", {"project": "speed", "pane": PANE, "evs": press})  # the focus in the text field
    for i in range(8):
        first_frame("click", [{"type": "mouse", "action": a, "x": 400, "y": 400, "button": "left", "clicks": 1}
                              for a in ("down", "up")])
        first_frame("key", [{"type": "key", "key": "abcdefgh"[i]}])
    for k, v in lat.items():
        if v:
            v = sorted(v)
            out[f"{k}_to_frame_ms"] = {"median": round(statistics.median(v)), "worst": round(v[-1]), "n": len(v)}

    # 4. A key while the person drags (#5651): the moves go once a screen frame, and the key's time to its frame is
    # measured with the pane's two ways to send: one request at a time (before #5651: the key waits for the answer
    # of the moves before it) and at once with numbers, at most four on the way (after).
    act("browser", {"action": "open", "project": "speed", "pane": PANE, "url": f"http://127.0.0.1:{WEB}/keys"})
    wait_quiet(fr)

    def during_drag(at_once):
        s = Sender(at_once)
        out_ms = []
        for i in range(6):
            wait_quiet(fr, 0.6)
            s.send({"type": "mouse", "action": "down", "x": 400, "y": 400, "button": "left", "clicks": 1})
            stop = threading.Event()

            def drag():
                x = 400
                while not stop.is_set():
                    x = 400 + (x - 399) % 200
                    s.send({"type": "mouse", "action": "move", "x": x, "y": 420, "button": "left"})
                    time.sleep(1 / 60)
            threading.Thread(target=drag, daemon=True).start()
            time.sleep(0.4)
            with fr.lock:
                before = fr.seq
                drag_frames.append(len([g for g in fr.got if g[0] > time.time() - 0.3]))  # should be 0
            t = time.time()
            s.send({"type": "key", "key": "klmnop"[i]})
            end = t + 3
            while time.time() < end:
                with fr.lock:
                    new = [g for g in fr.got if g[1] > before]
                if new:
                    out_ms.append((new[0][0] - t) * 1000)
                    break
                time.sleep(0.002)
            stop.set()
            s.send({"type": "mouse", "action": "up", "x": 400, "y": 420, "button": "left", "clicks": 1})
        v = sorted(out_ms)
        return {"median": round(statistics.median(v)), "worst": round(v[-1]), "n": len(v)} if v else None

    drag_frames = []
    out["key_in_drag_ms"] = {"one_at_a_time": during_drag(False), "at_once": during_drag(True),
                             "frames_of_the_drag": sum(drag_frames)}

    # 5. A page that holds still sends nothing: no sharp frame again and again.
    wait_quiet(fr)
    t0 = time.time()
    time.sleep(2)
    out["frames_in_2s_still"] = len(fr.since(t0))
    out["polls_at_once"] = fr.window
    fr.stop = True
    print(json.dumps(out, indent=1))


try:
    main()
finally:
    b = browser.BROWSERS.get("speed")
    if b:
        b.close()
    srv.shutdown()
    pages.shutdown()
