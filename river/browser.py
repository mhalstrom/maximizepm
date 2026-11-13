"""The browser of a session (#4631, design #4641; #5636): a Google Chrome that maxpm serve starts for each session of a
project, which the person sees and uses in the side panel of that session, and which the session drives with maxpm
browser and its Playwright tools.

- One browser for each session (#5636, mark 2026-11-12: 'I think it should be session specific'; it was one for each
  project, decision 2), with its own profile under <queue folder>/browsers/<project>/sessions/<session>/. Its first
  start copies the sign-ins of the project's profile (<queue folder>/browsers/<project>/profile), never the person's
  own Chrome profile. serve stops it when the session ends, and after browser_idle_end with no use (sweep). The
  project's own browser (no session) stays for the page walk checks. Only the person signs in, in the panel.
- Headless (decision 3): no window and no focus taken. "Open as a window" starts the same profile as a normal
  window, for a site that refuses a headless browser (a Google sign-in); "Back to the panel" goes headless again.
- The debugging port listens on 127.0.0.1 and only serve uses it: the panel and the sessions reach the browser
  through serve, which keeps the controller rule (one controller at a time: the person, a session, or a subagent
  that a session names).
- Network (decision 4): Chrome goes through a small proxy here, which resolves each host and refuses loopback and
  private addresses, except the ones that the project's setting browser_allow names. A page cannot reach serve,
  the test databases, or the Docker API, also not through a DNS name that points there. file:// and chrome://
  pages are blocked; downloads go to <queue folder>/browsers/<project>/downloads.
- The panel shows the page as frames (CDP Page.startScreencast) that it reads with a long poll (frame()), and
  sends the person's mouse and keys (person_input()).
- A session drives the browser with the Playwright MCP tools too (#5405): serve gives each session a CDP address of
  its own, ws://127.0.0.1:<serve port>/cdp/<agent>?project=<name>&token=<t>, and relays the CDP messages to Chrome
  after it checks each one (cdp_refusal): the token, the controller rule, and the sandbox. The setting browser_mcp
  puts a Playwright MCP server with that address into the launch command of a session that maxpm starts.
- A project's setting browser_source cdp:<port> (#5406) attaches to a Chrome that runs already (a shared test
  browser) instead: the panel and the sessions use only the tabs that serve opened in it, serve never closes that
  Chrome, and the network rule above does not hold for it. browser_lock and browser_unlock name the commands that
  take and give back that browser's lock when the controller changes.
- Such a browser runs headless too, so no window shows (#5619). The setting browser_mode names the command that starts
  it again as a window or headless ({mode}); with it, the panel offers Open as a window and Back to the panel for it
  as for its own browser, and serve runs the command under the lock (attached_mode).
- A plain headless Chrome says HeadlessChrome in its user agent and navigator.webdriver true, and Google's search
  answered it with a CAPTCHA at the first search (#5619, Chrome 157). The person drives this browser, so it says what
  the same Chrome with a window says (#5621, decided for mark 2026-11-12): serve's own Chrome gets that user agent
  (--user-agent), --disable-blink-features=AutomationControlled, and a window of a normal size. A normal window kept
  off the screen does not work on macOS: Chrome keeps 40 px of it on the screen, and a minimized or hidden window
  sends no frames. A page that refuses the browser anyway (REFUSED) makes the panel say so and offer Open as a window.

Python's standard library has no WebSocket client, so the CDP connection uses the small one below (RFC 6455)."""

import base64
import hashlib
import hmac
import itertools
import ipaddress
import json
import os
import re
import select
import shlex
import shutil
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote_plus, urldefrag, urlsplit

from . import core
from .core import RiverError

MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
START_WAIT = 20      # seconds for Chrome to write its DevToolsActivePort
CALL_WAIT = 20       # seconds for one CDP answer
LOAD_WAIT = 15       # seconds that open() waits for the page's load
CLOSE_WAIT = 5       # seconds that a close waits for Chrome to end by itself (it writes its cookies), then stops it
VIEW = (1280, 860)   # the size of the page before the panel says its own
# The screen that the page sees, when the panel's screen is smaller than the window (a phone through the relay): a
# headless Chrome says 800x600, smaller than its own window, which no Chrome with a window says (#5625).
SCREEN = (1512, 982) if sys.platform == "darwin" else (1920, 1080)
DSF_MAX = 2.0        # a phone's density of 3 sends 2.25 times the pixels of 2 through the relay, for little more
PANE_GONE = 30.0     # seconds after a panel's last frame poll that it counts as closed
SHARP_SEEN = 8       # frames whose bytes a sharp frame skips: a screenshot paints the page again, twice (#5625)
# The sharp frame waits until the page holds still this long (#5637): while the page moves (a scroll, typing, a video)
# the panel gets the screencast frames only. A screenshot after each frame sent each change twice, the second about
# four times larger, and it took the place of the frames that came after it.
SHARP_QUIET = 0.3
# The JPEG quality of the screencast frames (#5658). At a density above 1 they show only while the page moves (the
# sharp frame takes their place when it holds still), so they go smaller: over a phone link (120 ms, 20 Mbit/s) a
# scrolling text page sent 86 kB frames at quality 70, 16 a second; 70 kB and 19 at 50; 62 kB and 21 at 40
# (seed/browser_speed.py --rtt 120 --mbps 20). At density 1 they are the picture that stays.
STILL_QUALITY = 70
MOVING_QUALITY = 40


def chrome_path():
    """Google Chrome (or Chromium) on this computer: MAXPM_CHROME, else the usual places; None when there is none."""
    env = os.environ.get("MAXPM_CHROME")
    if env:
        return env if Path(env).exists() else None
    if Path(MAC_CHROME).exists():
        return MAC_CHROME
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        p = shutil.which(name)
        if p:
            return p
    return None


CHROME_DOWNLOAD = "https://www.google.com/chrome/"


def no_chrome():
    """Why the project browser cannot start on this computer, or None when Chrome is here (#5624, mark 2026-11-12: 'if
    the user does not have chrome we need to prompt them to download it'). Read at each start, so a Chrome that the
    person installs works at the next open, with no restart of serve."""
    if chrome_path():
        return None
    env = os.environ.get("MAXPM_CHROME")
    where = f" (MAXPM_CHROME names {env}, which is not there)" if env else ""
    return (f"Google Chrome is not on this computer{where}, and the project browser needs it: download it at "
            f"{CHROME_DOWNLOAD}, install it, then open the browser again. A session tells the person and does not "
            f"try again")


def user_agent(chrome):
    """The user agent of this Chrome with a window, for a headless one: Google's sign-in refuses HeadlessChrome. With
    the flag, navigator.userAgentData says Google Chrome too (Chrome 157, 2026-11-12). None when the version is not
    known."""
    try:
        out = subprocess.run([chrome, "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"(\d+)\.\d+", out)
    if not m:
        return None
    plat = {"darwin": "Macintosh; Intel Mac OS X 10_15_7", "win32": "Windows NT 10.0; Win64; x64"}.get(
        sys.platform, "X11; Linux x86_64")
    return f"Mozilla/5.0 ({plat}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{m.group(1)}.0.0.0 Safari/537.36"


# The pages of a site that refuses a browser with no window (#5619): the panel says so and offers Open as a window.
REFUSED = [re.compile(r"^https://accounts\.google\.com/(?:v3/)?signin/rejected")]

MODE_RUNNER = None   # tests: port -> True (the Chrome on it has a window), False (headless), None (not known)


def has_window(port):
    """Whether the Chrome that listens on 127.0.0.1:<port> has a window, from its arguments (its user agent can hide
    HeadlessChrome, also in /json/version). None when it is not known."""
    if MODE_RUNNER:
        return MODE_RUNNER(port)
    try:
        pids = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], capture_output=True, text=True,
                              timeout=5).stdout.split()
        if not pids:
            return None
        args = subprocess.run(["ps", "-o", "args=", "-p", pids[0]], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return "--headless" not in args if args.strip() else None


def browser_dir(project):
    """The folder of a project's browser: its profile, its downloads, and state.json. Beside the queue's database,
    so a test queue in a temporary folder has its browsers there too."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", project).strip("._") or "project"
    return core.db_path().parent / "browsers" / safe


def session_dir(project, owner):
    """The folder of one session's browser of a project (#5636): its own profile, downloads and state.json."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", owner).strip("._") or "session"
    return browser_dir(project) / "sessions" / safe


# What a session's profile copies from the project's profile at its first start (#5636): the sign-ins (cookies, the
# pages' local storage, saved passwords) and Local State, which holds the key of the saved passwords on Linux and
# Windows. No caches, no history. Each SQLite file goes with its journal and WAL.
PROFILE_COPY = ("Local State", "Default/Cookies", "Default/Network/Cookies", "Default/Login Data",
                "Default/Local Storage")


def copy_profile(src, dst):
    """Copy the sign-ins of the project's profile into a new session profile. A project with no profile yet gives an
    empty one. Chrome may run on src: each file is copied as it is now (Chrome writes a cookie at once)."""
    import shutil
    dst.mkdir(parents=True, exist_ok=True)
    for rel in PROFILE_COPY:
        for name in (rel, rel + "-journal", rel + "-wal"):
            s, d = src / name, dst / name
            try:
                if s.is_dir():
                    shutil.copytree(s, d, dirs_exist_ok=True, ignore=shutil.ignore_patterns("LOCK"))
                elif s.is_file():
                    d.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(s, d)
            except OSError:
                pass  # a file Chrome replaced while we read it: the session signs in again there


# ---------------------------------------------------------------- WebSocket (RFC 6455), the client side

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_accept(key):
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def _mask(data, key):
    if not data:
        return data
    n = len(data)
    return (int.from_bytes(data, "big") ^ int.from_bytes((key * (n // 4 + 1))[:n], "big")).to_bytes(n, "big")


def ws_frame(op, data, mask=True):
    """One final frame. A client masks what it sends; a server does not."""
    n = len(data)
    head = bytes([0x80 | op])
    bit = 0x80 if mask else 0
    if n < 126:
        head += bytes([bit | n])
    elif n < 65536:
        head += bytes([bit | 126]) + struct.pack(">H", n)
    else:
        head += bytes([bit | 127]) + struct.pack(">Q", n)
    if mask:
        key = os.urandom(4)
        return head + key + _mask(data, key)
    return head + data


def _exact(rfile, n):
    out = rfile.read(n)
    if out is None or len(out) < n:
        raise EOFError("the WebSocket closed")
    return out


def ws_read(rfile):
    """(opcode, payload) of one frame from a buffered file of a socket."""
    b1, b2 = _exact(rfile, 2)
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack(">H", _exact(rfile, 2))[0]
    elif n == 127:
        n = struct.unpack(">Q", _exact(rfile, 8))[0]
    key = _exact(rfile, 4) if b2 & 0x80 else None
    data = _exact(rfile, n) if n else b""
    if key:
        data = _mask(data, key)
    return b1 & 0x0F, bool(b1 & 0x80), data


class WS:
    """A WebSocket client for one ws:// address on this computer: send(text), recv() -> text."""

    def __init__(self, url, timeout=10):
        u = urlsplit(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + ("?" + u.query if u.query else "")
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{u.port or 80}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        self.rfile = self.sock.makefile("rb")
        head = b""
        while not head.endswith(b"\r\n\r\n"):
            c = self.rfile.read(1)
            if not c:
                raise OSError("the WebSocket server closed during the handshake")
            head += c
            if len(head) > 65536:
                raise OSError("the WebSocket handshake is too long")
        lines = head.decode("latin-1").split("\r\n")
        if " 101 " not in lines[0] + " ":
            raise OSError(f"no WebSocket upgrade: {lines[0]}")
        got = {k.strip().lower(): v.strip() for k, _, v in (x.partition(":") for x in lines[1:] if x)}
        if got.get("sec-websocket-accept") != ws_accept(key):
            raise OSError("the WebSocket handshake has a wrong accept key")
        self.sock.settimeout(None)
        self.wlock = threading.Lock()

    def send(self, text):
        with self.wlock:
            self.sock.sendall(ws_frame(0x1, text.encode()))

    def recv(self):
        parts = []
        while True:
            op, fin, data = ws_read(self.rfile)
            if op == 0x9:  # ping
                with self.wlock:
                    self.sock.sendall(ws_frame(0xA, data))
                continue
            if op == 0xA:
                continue
            if op == 0x8:
                raise EOFError("the WebSocket closed")
            parts.append(data)
            if fin:
                return b"".join(parts).decode("utf-8", "replace")

    def close(self):
        try:
            with self.wlock:
                self.sock.sendall(ws_frame(0x8, b""))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------- the network rule (decision 4)

def parse_allow(text):
    """The entries of browser_allow: host or host:port, split by spaces or commas, in lower case."""
    return [x.strip().lower() for x in re.split(r"[\s,]+", text or "") if x.strip()]


def _private(ip):
    a = ipaddress.ip_address(ip)
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_private or a.is_loopback or a.is_link_local or a.is_reserved or a.is_multicast or a.is_unspecified


def allowed(host, port, allow):
    """Whether browser_allow names this host (or host:port)."""
    host = (host or "").lower().strip("[]")
    return host in allow or f"{host}:{port}" in allow


def resolve(host, port, allow):
    """The addresses to connect to for host:port, or RiverError when it is a loopback or private address that
    browser_allow does not name. The proxy connects to the address it checked, so a DNS name that changes its
    answer between the check and the connection reaches nothing new."""
    host = (host or "").strip("[]")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise RiverError(f"the browser cannot find the host {host}: {e}")
    ips = [i[4][0] for i in infos]
    if not allowed(host, port, allow):
        bad = [ip for ip in ips if _private(ip)]
        if bad:
            raise RiverError(f"the browser does not open {host}:{port}: it is a loopback or private address ({bad[0]}); "
                             f"a person allows it for the project with maxpm config set browser_allow "
                             f"\"{host}:{port}\" --project <name>")
    return list(dict.fromkeys(ips))


def connect_any(ips, port, timeout=15):
    """A connection to the first of the checked addresses that answers (localhost: ::1, then 127.0.0.1)."""
    err = None
    for ip in ips:
        try:
            return socket.create_connection((ip, port), timeout)
        except OSError as e:
            err = e
    raise err or OSError("no address")


class _ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        sock = self.request
        sock.settimeout(30)
        head = b""
        try:
            while b"\r\n\r\n" not in head:
                c = sock.recv(65536)
                if not c:
                    return
                head += c
                if len(head) > 262144:
                    return
        except OSError:
            return
        line, _, rest = head.partition(b"\r\n")
        try:
            method, target, version = line.decode("latin-1").split(" ", 2)
        except ValueError:
            return
        if method.upper() == "CONNECT":
            host, _, port = target.rpartition(":")
            port = int(port or 443)
            upstream_first = b""
        else:
            u = urlsplit(target)
            if u.scheme != "http" or not u.hostname:
                return self._refuse(sock, "the proxy of the MaximizePM browser takes http and CONNECT only")
            host, port = u.hostname, u.port or 80
            path = (u.path or "/") + ("?" + u.query if u.query else "")
            hdrs, _, body = rest.partition(b"\r\n\r\n")
            keep = [h for h in hdrs.split(b"\r\n") if h and not h.lower().startswith((b"proxy-", b"connection:", b"keep-alive:"))]
            upstream_first = (f"{method} {path} {version}\r\n".encode() + b"\r\n".join(keep)
                              + b"\r\nConnection: close\r\n\r\n" + body)
        try:
            ips = resolve(host, port, self.server.allow())
        except RiverError as e:
            return self._refuse(sock, str(e))
        try:
            up = connect_any(ips, port)
        except OSError as e:
            return self._refuse(sock, f"the browser could not reach {host}:{port}: {e}", 502)
        try:
            if upstream_first:
                up.sendall(upstream_first)
            else:
                sock.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                extra = rest.partition(b"\r\n\r\n")[2]
                if extra:
                    up.sendall(extra)
            _pipe(sock, up)
        finally:
            up.close()

    def _refuse(self, sock, why, code=403):
        body = (f"<!doctype html><title>Blocked</title><p style='font:14px system-ui;margin:24px'>"
                f"MaximizePM: {why}</p>").encode()
        try:
            sock.sendall(f"HTTP/1.1 {code} Blocked\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: "
                         f"{len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        except OSError:
            pass


def _pipe(a, b):
    a.settimeout(None)
    b.settimeout(None)
    socks = [a, b]
    while True:
        try:
            ready, _, _ = select.select(socks, [], [], 300)
        except (OSError, ValueError):
            return
        if not ready:
            return
        for s in ready:
            try:
                data = s.recv(65536)
            except OSError:
                return
            if not data:
                return
            try:
                (b if s is a else a).sendall(data)
            except OSError:
                return


class Proxy(socketserver.ThreadingTCPServer):
    """The proxy of one project's browser, on 127.0.0.1. allow() reads browser_allow at each connection, so a
    changed setting holds at once."""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, project, port=0):
        self.project = project
        super().__init__(("127.0.0.1", port), _ProxyHandler)
        self.port = self.server_address[1]
        threading.Thread(target=self.serve_forever, daemon=True, name=f"browser-proxy-{project}").start()

    def allow(self):
        conn = core.connect()
        try:
            pid = conn.execute("SELECT id FROM projects WHERE name=?", (self.project,)).fetchone()
            return parse_allow(core.setting(conn, "browser_allow", project_id=pid[0] if pid else None))
        finally:
            conn.close()


# ---------------------------------------------------------------- keys of the person (the page's KeyboardEvent)

# key -> (windowsVirtualKeyCode, code, text on keyDown)
SPECIAL = {
    "Enter": (13, "Enter", "\r"), "Backspace": (8, "Backspace", ""), "Tab": (9, "Tab", ""), "Escape": (27, "Escape", ""),
    "Delete": (46, "Delete", ""), "ArrowLeft": (37, "ArrowLeft", ""), "ArrowUp": (38, "ArrowUp", ""),
    "ArrowRight": (39, "ArrowRight", ""), "ArrowDown": (40, "ArrowDown", ""), "Home": (36, "Home", ""),
    "End": (35, "End", ""), "PageUp": (33, "PageUp", ""), "PageDown": (34, "PageDown", ""), " ": (32, "Space", " "),
}


def modifiers(m):
    """CDP's bit mask from the page's names: alt 1, ctrl 2, meta 4, shift 8."""
    m = m or {}
    return (1 if m.get("alt") else 0) | (2 if m.get("ctrl") else 0) | (4 if m.get("meta") else 0) | (8 if m.get("shift") else 0)


def key_events(key, mods=None):
    """The CDP Input.dispatchKeyEvent calls for one key press (down and up)."""
    m = modifiers(mods)
    if key in SPECIAL:
        vk, code, text = SPECIAL[key]
        down = {"type": "keyDown" if text else "rawKeyDown", "key": key, "code": code, "windowsVirtualKeyCode": vk,
                "nativeVirtualKeyCode": vk, "modifiers": m}
        if text:
            down["text"] = text
        return [down, {"type": "keyUp", "key": key, "code": code, "windowsVirtualKeyCode": vk,
                       "nativeVirtualKeyCode": vk, "modifiers": m}]
    if len(key) == 1:
        vk = ord(key.upper()) if key.isalnum() else 0
        down = {"type": "keyDown", "key": key, "modifiers": m, "windowsVirtualKeyCode": vk}
        if not m & 6:  # with Ctrl or Cmd it is a shortcut, not text
            down["text"] = key
        return [down, {"type": "keyUp", "key": key, "modifiers": m, "windowsVirtualKeyCode": vk}]
    raise RiverError(f"unknown key {key!r}: name one of {', '.join(sorted(k for k in SPECIAL if k != ' '))}, or one character")


# ---------------------------------------------------------------- one project's browser

class Browser:
    def __init__(self, project, owner=None):
        self.project = project
        self.owner = owner   # the session whose browser this is (#5636); None: the project's own (page walks)
        self.dir = session_dir(project, owner) if owner else browser_dir(project)
        self.used = time.time()  # the last frame poll or command: browser_idle_end counts from it (sweep)
        self.lock = threading.RLock()
        self.cond = threading.Condition()
        self.ws = None
        self.proc = None
        self.proxy = None
        self.port = None
        self.ids = itertools.count(1)
        self.wait = {}       # id -> [Event, message]
        self.session = None  # the CDP session of the tab the panel shows
        self.target = None
        self.targets = {}    # targetId -> {url, title, opener}
        self.frame = None    # {seq, data, w, h}
        self.seq = 0
        self.size = None     # (w, h) of the panel, CSS px
        self.dsf = 1         # the pixel density of the panel's screen (#5625)
        self.screen = None   # (w, h) of the panel's screen, CSS px
        self.sharp = threading.Event()  # a sharp frame is due: _sharpen takes a screenshot of the page
        self.sharp_due = 0.0  # when: SHARP_QUIET after the last new frame
        self.sent = {}       # pane id -> the seq that its last answered poll had (#5637: several polls at once)
        self.sharp_thread = None
        self.seen = []       # the bytes (a hash) of the last screencast frames, for the sharp frames
        self.look = None     # the hash of the screencast frame that shows now, or that the sharp frame shows (#5651)
        self.panes = {}      # the open panels: pane id -> {size, dsf, screen, at}
        self.driver = None   # the panel where a person last clicked, typed or took control: it sets the size
        self.pane_lock = threading.Lock()
        self.attached = None # the debugging port of a Chrome that serve did not start (browser_source cdp:<port>)
        self.mine = set()    # attached: the tabs serve opened in it, and the tabs that they opened
        self.context = None  # attached, a session's: its browser context in that Chrome (#5634)
        self.attached_window = None  # attached: whether that Chrome has a window (None: not known)
        self.modes = False   # attached: the project has browser_mode, so the panel offers a window and back
        self.listeners = {}  # CDP session -> fn(method, params): the events of a tab that a page walk opened
        self.pointer = None  # the driving session's mouse (#5616): {x, y, who, target, n, press, px, py}
        self.typed = None    # what it types now: {who, target, text, secret, n, at, done}
        self.watched = 0.0   # when the panel last asked for a frame: someone watches (browser_pace watch)
        self.watching = 0    # frame polls that wait now
        self.person_at = 0.0 # the person's last input in the panel (a click, a key, an address, Take control; #5629)
        self.inputs = Inputs(self)  # the person's input, to Chrome in order without a wait in the request (#5651)
        self.refit_ws = None  # the CDP connection of the Chrome whose density was set again after a load (#5651)
        self.cast_quality = None  # the JPEG quality of the running screencast (#5658)
        self.active_at = time.time()  # the last change of the page: a new frame, a tab, an address (#5650: a window
                                      # with none for browser_window_idle goes back into the panel)
        self.visits = {}     # url -> {url, title, n, at}: the pages its tabs showed, for the address bar (#5635)
        self.history = {"rows": None, "mtime": None, "at": 0.0, "reading": False}  # Chrome's History in memory
        self.icons = {"rows": None, "mtime": None, "at": 0.0, "reading": False}  # Chrome's Favicons (#5632)
        # #5663: what an action of a session waits for. navs: the main frame of the shown tab started to load a new
        # document; stops: it stopped loading (loaded, failed, or cancelled); nav: set at each start; requests: the
        # shown tab's requests in flight (requestId -> when it started), for --wait idle.
        self.navs = 0
        self.stops = 0
        self.asks = self.asks_end = 0  # #5674: next pages that the page asked for, and those that started or stayed
        self.within = 0  # #5717: address changes in the same document (pushState, replaceState, a #part)
        self.stale = False  # #5717: such a change came after the last read of the page's title (here)
        self.nav = threading.Event()
        self.requests = {}
        self.net_at = 0.0
        self.snap = None     # the snapshot's world in the shown tab: {target, session, ctx}; refs live in it (#5663)
        self.refs = 0        # the last ref number given: refs never repeat in one browser, so an old ref fails
        self.opened = 0      # tabs that a page of the shown tab opened
        self.dialog = None   # the alert, confirm or prompt box of the shown tab that waits for an answer (#5674)
        self.state = self._load()
        if owner:  # the sweep of a later serve finds a Chrome of this session that still runs (#5636)
            self.state.update(project=project, owner=owner)

    # state.json: the controller, the window mode, Chrome's pid and the proxy port (to attach after a restart)
    def _load(self):
        try:
            return json.loads((self.dir / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / "state.json.tmp"
        tmp.write_text(json.dumps(self.state), encoding="utf-8")
        tmp.replace(self.dir / "state.json")

    # ---- start, attach, stop

    def running(self):
        return self.ws is not None

    def start(self, window=None):
        """Start Chrome (or attach to the one that this project's browser left running before serve restarted). With
        browser_source cdp:<port>, attach to that Chrome instead (#5406)."""
        port = setting_of(self.project, "browser_source", core.browser_cdp_port)
        if port:
            return self._start_attached(port, window)
        with self.lock:
            if self.attached:  # the setting changed: leave the other Chrome alone
                self._stop_chrome()
            if self.ws is not None and window in (None, bool(self.state.get("window"))):
                return
            if window is not None and window != bool(self.state.get("window")):
                self._stop_chrome()
                self.state["window"] = window
            if self.proxy is None:
                try:
                    self.proxy = Proxy(self.project, self.state.get("proxy_port") or 0)
                except OSError:
                    self.proxy = Proxy(self.project)
            if not self._attach_old():
                self._launch()
            self._connect()

    def _start_attached(self, port, window):
        """Attach to that Chrome. A change of its mode (window) goes through attached_mode, which holds the lock."""
        with self.lock:
            on = self.ws is not None and self.attached == port
            if window is not None and window != bool(self.attached_window if on else has_window(port)):
                raise RiverError(f"the browser of {self.project} is a Chrome that runs already (browser_source "
                                 f"cdp:{port}); it opens as a window only through the setting browser_mode")
            if not on:
                if self.ws is not None:
                    self._stop_chrome()
                try:
                    self._version(port)
                except (OSError, ValueError):
                    raise RiverError(f"no Chrome answers on 127.0.0.1:{port} (browser_source cdp:{port} of "
                                     f"{self.project}): start that browser first, as the project's own steps say")
                self.attached = self.port = port
                self.attached_window = has_window(port)
                self.modes = bool((setting_of(self.project, "browser_mode") or "").strip())
                self._connect()

    def _windowed(self):
        return bool(self.attached_window) if self.attached else bool(self.state.get("window"))

    def _new_target(self, url="about:blank"):
        """A new tab; attached, it is one of serve's own tabs (in the session's own context, #5634)."""
        t = self.call("Target.createTarget", {"url": url, **({"browserContextId": self.context} if self.context else {})}
                      )["targetId"]
        if self.attached:
            self.mine.add(t)
            self.targets.setdefault(t, {"url": url, "title": "", "opener": None})
            self.state["tab"] = t
            self._save()
        return t

    def _active_port(self):
        try:
            return int((self.dir / "profile" / "DevToolsActivePort").read_text(encoding="utf-8").split()[0])
        except (OSError, ValueError, IndexError):
            return None

    def _version(self, port):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=3) as r:
            return json.loads(r.read())

    def _attach_old(self):
        """The Chrome of this profile still runs (serve restarted): use it when its proxy is still ours."""
        port = self._active_port()
        if not port or not self.state.get("pid") or not core.pid_alive(self.state["pid"]):
            return False
        try:
            self._version(port)
        except (OSError, ValueError):
            return False
        if self.state.get("proxy_port") != self.proxy.port:
            self._kill(self.state["pid"])  # its proxy is gone: start it again with the new one
            return False
        self.port = port
        return True

    def _launch(self):
        chrome = chrome_path()
        if not chrome:
            raise RiverError(no_chrome())
        profile = self.dir / "profile"
        (self.dir / "downloads").mkdir(parents=True, exist_ok=True)
        if self.owner and not profile.exists():  # a session's first start: the project's sign-ins (#5636)
            copy_profile(browser_dir(self.project) / "profile", profile)
        profile.mkdir(parents=True, exist_ok=True)
        try:
            (profile / "DevToolsActivePort").unlink()
        except OSError:
            pass
        # The window has a normal size; the page takes the size of the panel by emulation (_fit). A window of the panel's
        # size (454x635) was one of the traits that made Google's search answer with a CAPTCHA (#5619). With the
        # debugging port, Chrome says navigator.webdriver true; the person drives this browser, so it says false.
        w, h = VIEW
        args = [chrome, f"--user-data-dir={profile}", "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
                "--no-first-run", "--no-default-browser-check", "--disable-default-apps", "--disable-sync",
                "--disable-blink-features=AutomationControlled",
                f"--proxy-server=http://127.0.0.1:{self.proxy.port}", "--proxy-bypass-list=<-loopback>",
                f"--window-size={w},{h}", "about:blank"]
        if not self.state.get("window"):
            args[1:1] = ["--headless=new", "--hide-scrollbars", "--mute-audio"]
            ua = user_agent(chrome)
            if ua:
                args.insert(1, f"--user-agent={ua}")
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True)
        end = time.time() + START_WAIT
        while time.time() < end:
            port = self._active_port()
            if port:
                try:
                    self._version(port)
                    self.port = port
                    break
                except (OSError, ValueError):
                    pass
            if self.proc.poll() is not None:
                raise RiverError(f"Google Chrome ended at its start (exit {self.proc.returncode}); is another Chrome using "
                                 f"the profile {profile}?")
            time.sleep(0.1)
        else:
            self._kill(self.proc.pid)
            raise RiverError(f"Google Chrome did not open its debugging port within {START_WAIT}s")
        self.state.update(pid=self.proc.pid, proxy_port=self.proxy.port)
        self._save()

    def _connect(self):
        url = self._version(self.port)["webSocketDebuggerUrl"]
        self.ws = WS(url)
        threading.Thread(target=self._reader, args=(self.ws,), daemon=True, name=f"browser-{self.project}").start()
        self.call("Target.setDiscoverTargets", {"discover": True})
        pages = [t for t in self.call("Target.getTargets")["targetInfos"] if t["type"] == "page"]
        if self.attached:  # only serve's own tab: the other tabs are other sessions' and the person's (#5406)
            self.targets = {}
            if self.owner:  # a session: a browser context of its own, with the sign-ins of the default one (#5634)
                self.context = self.call("Target.createBrowserContext", {"disposeOnDetach": True})["browserContextId"]
                cookies = cookie_params(self.call("Storage.getCookies").get("cookies") or [])
                if cookies:
                    self.call("Storage.setCookies", {"cookies": cookies, "browserContextId": self.context})
                self.state.pop("tab", None)  # the tab of an earlier connection went with its context
            own = [t for t in pages if t["targetId"] == self.state.get("tab")]
            if own:
                t = own[0]
                self.mine = {t["targetId"]}
                self.targets[t["targetId"]] = {"url": t["url"], "title": t["title"], "opener": None}
                return self._attach(t["targetId"])
            self.mine = set()
            return self._attach(self._new_target())
        try:
            self.call("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(self.dir / "downloads")})
        except RiverError:
            pass
        for t in pages:
            self.targets[t["targetId"]] = {"url": t["url"], "title": t["title"], "opener": t.get("openerId")}
            note_visit(self, t["url"], t["title"])
        self._attach(pages[0]["targetId"] if pages else self._new_target())

    def _attach(self, target):
        old = self.session
        self.session = self.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        self.target = target
        self.dialog = None  # a box of the tab shown before waits in that tab
        self.asks_end = self.asks
        self.requests = {}  # the requests of the tab shown before never finish for this one (--wait idle)
        self.stops = self.navs  # nor does a load of that tab (loading)
        if old:
            try:
                self.call("Target.detachFromTarget", {"sessionId": old})
            except RiverError:
                pass
        s = self.session
        self.call("Page.enable", session=s)
        self.call("Network.enable", session=s)
        self.call("Network.setBlockedURLs", {"urls": ["file://*", "chrome://*", "chrome-extension://*"]}, session=s)
        self._fit()
        self.cast_quality = self._cast_quality()
        self.call("Page.startScreencast", {"format": "jpeg", "quality": self.cast_quality, "everyNthFrame": 1}, session=s)
        try:
            self.call("Target.activateTarget", {"targetId": target})
        except RiverError:
            pass

    def _fit(self):
        """The page takes the size of the panel (headless only; a window has its own size), at the pixel density of the
        panel's screen. Emulation and not Browser.setWindowBounds (#5625): the window keeps its normal size (VIEW), and
        a window of the panel's size was one of the traits that made Google answer with a CAPTCHA (#5619). The screen
        is the panel's screen, or a normal one when that is smaller than the window."""
        if not self.size or self._windowed() or not self.session:
            return
        w, h = self.size
        sw, sh = self.screen or (0, 0)
        if sw < VIEW[0] or sh < VIEW[1]:
            sw, sh = SCREEN
        self.call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": self.dsf,
                  "mobile": False, "screenWidth": sw, "screenHeight": sh}, session=self.session)
        q = self._cast_quality()
        if self.cast_quality not in (None, q):  # the density crossed 1: the frames' quality follows
            self.cast_quality = q
            self.call("Page.stopScreencast", session=self.session)
            self.call("Page.startScreencast", {"format": "jpeg", "quality": q, "everyNthFrame": 1}, session=self.session)
        self._want_sharp(now=True)  # a page that does not paint again after the change sends no frame of its new size

    def _cast_quality(self):
        """Smaller screencast frames when a sharp frame follows them (#5658)."""
        return MOVING_QUALITY if self.dsf > 1 and self.size and not self._windowed() else STILL_QUALITY

    def _refit(self):
        try:
            with self.lock:
                self._fit()
        except (RiverError, OSError):
            self.refit_ws = None  # the next load tries again

    def _want_sharp(self, now=False):
        self.sharp_due = time.time() + (0 if now else SHARP_QUIET)
        if self.sharp_thread is None:
            self.sharp_thread = threading.Thread(target=self._sharpen, daemon=True, name=f"browser-sharp-{self.project}")
            self.sharp_thread.start()
        self.sharp.set()

    def _sharpen(self):
        """The frames at the panel's pixel density (#5625). A headless Chrome sends its screencast frames at one pixel
        for each CSS pixel, whatever the emulated density, and with --force-device-scale-factor its screencast stops
        when the emulation changes the density (Chrome 157). So when the page holds still for SHARP_QUIET after a new
        frame, a screenshot of the page follows (#5637), at most one at a time; the next one waits for the next change."""
        while True:
            self.sharp.wait()
            while self.sharp_due > time.time():  # each new frame moves the time on: the page still moves
                time.sleep(max(0.01, self.sharp_due - time.time()))
            self.sharp.clear()  # the frames of the wait ask for this one screenshot; a frame after now asks again
            s, size, due = self.session, self.size, self.sharp_due
            if due > time.time():  # a frame came just now
                self.sharp.set()
                continue
            if not s or not size or self._windowed():
                continue
            try:
                data = self.call("Page.captureScreenshot", {"format": "jpeg", "quality": 70}, session=s, timeout=10)["data"]
            except (RiverError, KeyError):
                continue
            if s != self.session or size != self.size or self.sharp_due != due:
                continue  # another tab or size, or a new frame came while it ran: the next quiet time asks again
            with self.cond:
                self.seq += 1
                self.frame = {"seq": self.seq, "data": data, "w": size[0], "h": size[1], "sharp": True}
                self.cond.notify_all()

    def _kill(self, pid):
        try:
            os.killpg(pid, signal.SIGTERM)
        except (OSError, AttributeError):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        if self.proc is not None and self.proc.pid == pid:  # our child: wait reaps it
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (OSError, AttributeError):
                    self.proc.kill()
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    # A slow GitHub macOS runner took longer to end a killed Chrome (#5705): the close goes on, and
                    # Python reaps the process when it ends.
                    pass
            return
        end = time.time() + 5
        while time.time() < end and core.pid_alive(pid):
            time.sleep(0.1)

    def _stop_chrome(self):
        if self.ws is not None and not self.attached:
            try:  # wait for its answer: a socket closed at once made Chrome miss the command (#5633)
                self.call("Browser.close", timeout=2)
            except RiverError:
                pass
        ws, self.ws = self.ws, None
        if self.attached:  # a Chrome that serve did not start: only leave it (a session's context goes with the socket)
            if ws:
                ws.close()
            self.attached = self.attached_window = self.context = None
            self.session = self.target = None
            self.targets, self.mine = {}, set()
            return
        if ws:
            ws.close()
        pid = self.state.get("pid")
        if ws and pid:  # Chrome writes its cookies to disk as it closes: give it that time (#5633), then stop it
            end = time.time() + CLOSE_WAIT
            while time.time() < end and (self.proc.poll() is None if self.proc is not None and self.proc.pid == pid
                                         else core.pid_alive(pid)):
                time.sleep(0.05)
        if pid and (core.pid_alive(pid) or (self.proc is not None and self.proc.pid == pid)):
            self._kill(pid)
        self.proc = None
        self.session = self.target = None
        self.targets = {}
        self.state.pop("pid", None)
        self._save()

    def close(self):
        with self.lock:
            self._stop_chrome()
            if self.proxy:
                self.proxy.shutdown()
                self.proxy.server_close()
                self.proxy = None
        with self.cond:
            self.frame = None
            self.seq += 1
            self.cond.notify_all()

    # ---- CDP

    def _reader(self, ws):
        while True:
            try:
                msg = json.loads(ws.recv())
            except (EOFError, OSError, ValueError):
                break
            if "id" in msg:
                w = self.wait.get(msg["id"])
                if w:
                    w[1] = msg
                    w[0].set()
                continue
            try:
                self._event(msg.get("method"), msg.get("params") or {}, msg.get("sessionId"))
            except Exception:  # an event must never stop the reader
                pass
        if self.ws is ws:  # Chrome ended (a person closed the window): the next action starts it again
            self.ws = None
            self.session = self.target = self.dialog = None
            if self.state.get("window") and not self.attached:  # #5650: headless again, not a new window
                self.state["window"] = False
                self.state.pop("window_by", None)
                self.state["window_end"] = {"why": "the window was closed", "at": core.iso(core.now())}
                try:
                    self._save()
                except OSError:
                    pass
            with self.cond:
                self.seq += 1
                self.cond.notify_all()
        for w in list(self.wait.values()):
            w[0].set()

    def _event(self, method, p, session):
        fn = self.listeners.get(session) if session else None
        if fn:
            fn(method, p)
            return
        if method == "Page.screencastFrame":
            self.send("Page.screencastFrameAck", {"sessionId": p["sessionId"]}, session=session)
            if session == self.session:
                m = p.get("metadata") or {}
                if self.dsf > 1 and self.size and not self._windowed() and self.watched > time.time() - WATCH_GAP:
                    # Someone watches at a density above 1: a screenshot gives the sharp frame once the page holds
                    # still. The screenshot paints the page again, which sends frames that it saw already: those
                    # ask for none, and the sharp frame stays. A new frame shows at once (#5637).
                    # #5651: the look that shows now (the sharp frame's) comes again: the screenshot's echo. Another
                    # look seen before shows only after the person's input (a hover on, off, and on again); with no
                    # input it is a caret that blinks.
                    h = hashlib.sha1(p["data"].encode()).digest()
                    sharp = self.frame and self.frame.get("sharp")
                    if sharp and (h == self.look or (h in self.seen and time.time() - self.person_at > INPUT_SHOWS)):
                        return
                    self.look = h
                    if h not in self.seen:
                        self.seen = (self.seen + [h])[-SHARP_SEEN:]
                    self._want_sharp()
                self.active_at = time.time()
                with self.cond:
                    self.seq += 1
                    self.frame = {"seq": self.seq, "data": p["data"], "w": m.get("deviceWidth"), "h": m.get("deviceHeight")}
                    self.cond.notify_all()
        elif session == self.session and method in NAV_EVENTS:
            track_nav(self, method, p)
        elif session == self.session and method in ("Page.javascriptDialogOpening", "Page.javascriptDialogClosed"):
            track_dialog(self, method, p, session)
        elif method == "Page.loadEventFired" and session == self.session and self.refit_ws is not self.ws:
            # #5651: a Chrome that took the panel's density before its first page load put each hover (a move with
            # no button held) at x / density: at density 2 a move to (300, 300) reached the page at (150, 150), so
            # no hover showed under the person's pointer. The same density set again after a load ends it for this
            # Chrome, also for other sites and new tabs (set twice at once, or a second later, it does not).
            self.refit_ws = self.ws
            threading.Thread(target=self._refit, daemon=True, name=f"browser-refit-{self.project}").start()
        elif method in ("Target.targetCreated", "Target.targetInfoChanged"):
            t = p["targetInfo"]
            if t["type"] != "page":
                return
            if self.attached:  # attached: only serve's own tabs and the tabs they open
                if t["targetId"] not in self.mine and t.get("openerId") not in self.mine:
                    return
                self.mine.add(t["targetId"])
            new = t["targetId"] not in self.targets
            self.active_at = time.time()
            self.targets[t["targetId"]] = {"url": t["url"], "title": t["title"], "opener": t.get("openerId")}
            note_visit(self, t["url"], t["title"])
            if new and t.get("openerId") and t["openerId"] == self.target:  # a page opened a new tab: show it
                self.opened += 1  # an action that led to it waits for it (after)
                self.nav.set()
                threading.Thread(target=self._switch_quiet, args=(t["targetId"],), daemon=True).start()
            with self.cond:
                self.seq += 1
                self.cond.notify_all()
        elif method == "Target.targetDestroyed":
            gone = self.targets.pop(p["targetId"], None) or {}
            self.mine.discard(p["targetId"])
            if p["targetId"] == self.target:
                self.session = self.target = self.dialog = None
                # A popup that closes (an OAuth sign-in) shows the page that opened it again (#5619).
                back = gone.get("opener") if gone.get("opener") in self.targets else None
                threading.Thread(target=self._switch_quiet, args=(back,), daemon=True).start()
            with self.cond:
                self.seq += 1
                self.cond.notify_all()

    def _switch_quiet(self, target):
        try:
            with self.lock:
                if target is None:
                    left = list(self.targets)
                    target = left[-1] if left else self._new_target()
                self._attach(target)
        except (RiverError, OSError):
            pass

    def send(self, method, params=None, session=None):
        """A CDP call whose answer nobody waits for."""
        ws = self.ws
        if ws is None:
            return
        msg = {"id": next(self.ids), "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        try:
            ws.send(json.dumps(msg))
        except OSError:
            pass

    def call(self, method, params=None, session=None, timeout=CALL_WAIT):
        ws = self.ws
        if ws is None:
            raise RiverError("the browser is not running: open a page with maxpm browser open <address>")
        i = next(self.ids)
        msg = {"id": i, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        w = self.wait[i] = [threading.Event(), None, session]  # a box that opens in session ends the wait (#5674)
        try:
            ws.send(json.dumps(msg))
            if not w[0].wait(timeout):
                raise RiverError(f"the browser did not answer {method} within {timeout}s")
        except OSError as e:
            raise RiverError(f"the browser connection broke: {e}")
        finally:
            self.wait.pop(i, None)
        if w[1] is None:
            raise RiverError("the browser closed")
        if "dialog" in w[1]:
            raise DialogOpen(self, w[1]["dialog"])
        if "error" in w[1]:
            raise RiverError(f"the browser refused {method}: {w[1]['error'].get('message')}")
        return w[1].get("result") or {}

    def page(self, method, params=None, timeout=CALL_WAIT):
        """A call to the tab that the panel shows."""
        self.start()
        if not self.session:
            self._switch_quiet(None)
        if self.dialog and method != "Page.handleJavaScriptDialog":  # the page waits for the box: the call would too
            raise DialogOpen(self, self.dialog)
        return self.call(method, params, session=self.session, timeout=timeout)

    def evaluate(self, expr, timeout=CALL_WAIT):
        r = self.page("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True,
                                           "userGesture": True}, timeout)
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            raise RiverError("the script failed in the page: "
                             + ((d.get("exception") or {}).get("description") or d.get("text") or "an error"))
        return (r.get("result") or {}).get("value")

    # ---- what the panel and the sessions do

    def info(self):
        cur = self.targets.get(self.target) or {}
        c = self.state.get("controller") or {}
        before = self.state.get("before") or {}
        url = cur.get("url", "")
        return {"project": self.project, "owner": self.owner, "running": self.running(), "window": self._windowed(),
                "source": f"cdp:{self.attached}" if self.attached else None,
                "modes": not self.attached or self.modes,  # the panel offers Open as a window and Back to the panel
                "refused": any(r.match(url) for r in REFUSED),  # the site refuses a browser with no window
                "url": cur.get("url", ""), "title": cur.get("title", ""), "controller": c or None,
                "before": before.get("who"),
                # #5650: who opened it as a window and when; why it went back into the panel by itself
                "window_by": self.state.get("window_by") if self._windowed() else None,
                "window_idle": setting_of(self.project, "browser_window_idle") if self._windowed() else None,
                "window_end": self.state.get("window_end"),
                "size": list(self.size) if self.size else None,  # the panel's size that serve has (#5625)
                "dialog": dialog_info(self.dialog),  # a box of the page that waits for an answer (#5674)
                # The tab strip (#5632): each tab with its icon (from Chrome's Favicons, a data address) and the
                # session whose pointer is on it.
                "tabs": [{"id": k, "url": v["url"], "title": v["title"], "on": k == self.target,
                          "icon": tab_icon(icons, v["url"]), "agent": p["who"] if p and p.get("target") == k else None}
                         for icons, p in ((tab_icons(self), self.pointer),) for k, v in self.targets.items()]}

    def open(self, url):
        url = norm_url(url)
        u = urlsplit(url)
        if u.scheme in ("http", "https") and not self._attached_now():
            resolve(u.hostname, u.port or (443 if u.scheme == "https" else 80), self.proxy_allow())
        self.start()
        r = self.page("Page.navigate", {"url": url})
        if r.get("errorText"):
            raise RiverError(f"the browser could not open {url}: {r['errorText']}")
        self.settle()
        return self.info()

    def _attached_now(self):
        """Whether this browser is (or will be, at its start) a Chrome that serve did not start."""
        return bool(self.attached or setting_of(self.project, "browser_source", core.browser_cdp_port))

    def proxy_allow(self):
        if self.proxy is None:
            self.start()
        return self.proxy.allow()

    def settle(self, timeout=LOAD_WAIT):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if self.evaluate("document.readyState", 5) == "complete":
                    break
            except DialogOpen:  # the page waits for a box (#5674): it loads no further until someone answers it
                break
            except RiverError:
                pass
            time.sleep(0.2)
        try:  # the title and address as they are now (the target events can come a moment later)
            t = self.call("Target.getTargetInfo", {"targetId": self.target})["targetInfo"]
            self.targets[self.target] = {"url": t["url"], "title": t["title"], "opener": t.get("openerId")}
        except (RiverError, KeyError, TypeError):
            pass
        self.here()

    def here(self):
        """The shown tab's address and title as the page knows them now. #5663: right after a load, the target's info
        can still hold the page before. #5717: a page's script that changes the address (a client route, pushState)
        changes no title in Chrome's target info, so the replies named the page before."""
        try:
            url, title = self.evaluate("[location.href, document.title]", 5)
            if self.target in self.targets:
                self.targets[self.target].update(url=url, title=title)
            self.stale = False
        except (RiverError, TypeError, ValueError):
            pass

    def resize(self, w, h, dpr=1, screen=None, pane=None):
        """A panel's size in CSS px, the pixel density of its screen, and its screen (w, h). Which panel sets the
        page's size (mark 2026-11-12, #5625): the panel where a person last clicked, typed or took control (the same
        person can show the browser on the Mac and on the phone); while a session drives, that panel's size stays;
        with no such panel open, the largest open panel. The other panels show the frame scaled. With no pane id (an
        older page), the size holds at once."""
        w, h = max(200, min(int(w), 3000)), max(150, min(int(h), 3000))
        try:
            dpr = round(max(1.0, min(float(dpr or 1), DSF_MAX)), 2)
        except (TypeError, ValueError):
            dpr = 1
        try:
            screen = (max(0, min(int(screen[0]), 10000)), max(0, min(int(screen[1]), 10000))) if screen else None
        except (TypeError, ValueError, IndexError):
            screen = None
        if pane:
            with self.pane_lock:
                self.panes[str(pane)[:40]] = {"size": (w, h), "dsf": dpr, "screen": screen, "at": time.time()}
            return self._choose()
        self.size, self.dsf, self.screen = (w, h), dpr, screen
        if self.running():
            self._fit()

    def pane_seen(self, pane):
        """A panel polls for frames: it is open. True when serve knows its size."""
        with self.pane_lock:
            p = self.panes.get(pane)
            if p:
                p["at"] = time.time()
        self._choose()
        return p is not None

    def drive(self, pane):
        """A person clicked, typed or took control in this panel: its size is the page's size from now on."""
        if pane and pane != self.driver:
            self.driver = str(pane)[:40]
            self._choose()

    def _choose(self):
        now = time.time()
        with self.pane_lock:
            for k in [k for k, p in self.panes.items() if now - p["at"] > PANE_GONE]:
                del self.panes[k]
            p = self.panes.get(self.driver) or max(self.panes.values(), default=None,
                                                   key=lambda p: p["size"][0] * p["size"][1])
            if not p or (p["size"], p["dsf"], p["screen"]) == (self.size, self.dsf, self.screen):
                return
            self.size, self.dsf, self.screen = p["size"], p["dsf"], p["screen"]
        if self.running():
            self._fit()


SEARCH_URL = "https://www.google.com/search?q="


def norm_url(url):
    url = (url or "").strip()
    if not url:
        raise RiverError("name an address: maxpm browser open https://example.com")
    if url == "about:blank":
        return url
    m = re.match(r"^([a-z][a-z0-9+.-]*):(?!\d)", url, re.I)  # a scheme (localhost:3000 is a host and a port)
    if m and m.group(1).lower() not in ("http", "https"):
        raise RiverError(f"the browser opens http and https addresses only, not {m.group(1).lower()}:")
    if not m:
        # Words are a search, as in Chrome's address bar (#5635): a space, or one word with no dot that is not a host
        # and port (localhost, my-box:8080, an IPv6 address in brackets).
        if re.search(r"\s", url) or not re.match(r"^(localhost|\[[0-9a-f:.]+\]|[^/\s:?#]+\.[^/\s:?#]+|[^/\s:?#]+:\d+)"
                                                r"([/:?#]|$)", url, re.I):
            return SEARCH_URL + quote_plus(url)
        url = ("http://" if re.match(r"^(localhost|127\.|\[::1\])", url) else "https://") + url
    scheme = urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise RiverError(f"the browser opens http and https addresses only, not {scheme}:")
    return url


# ---------------------------------------------------------------- what the address bar suggests (#5635)

HISTORY_ROWS = 3000   # the newest addresses of Chrome's History that serve keeps in memory
HISTORY_AGAIN = 20.0  # seconds before serve reads a History file that changed again
SUGGEST_MAX = 8       # rows that the address bar shows
_SKIP = re.compile(r"^https://(www\.)?google\.[a-z.]+/(search|url|sorry)\b|^https://accounts\.google\.com/")


def _history_file(b):
    """Chrome's History of the browser's own profile; None for a Chrome that serve did not start (it has its own)."""
    return None if b.attached else b.dir / "profile" / "Default" / "History"


def read_history(path):
    """[{url, title, n, at}] of a Chrome History file, newest first. Chrome holds the file open and locked: serve reads a
    copy (with its journal), in a folder of its own."""
    rows = _read_copy(path, "SELECT url, title, visit_count + 2 * typed_count, last_visit_time FROM urls WHERE hidden = 0 "
                            "ORDER BY last_visit_time DESC LIMIT ?", (HISTORY_ROWS,))
    # last_visit_time: microseconds since 1601-01-01; the same order as Unix time
    return [{"url": u, "title": ti or "", "n": n or 1, "at": (at or 0) / 1e6 - 11644473600} for u, ti, n, at in rows]


def _read_copy(path, sql, args=()):
    """The rows of one query on a copy of a Chrome database (with its journal), in a folder of its own: Chrome holds
    the file open and locked."""
    import sqlite3
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        copy = Path(d) / Path(path).name
        shutil.copyfile(path, copy)
        for extra in ("-journal", "-wal"):
            if Path(str(path) + extra).exists():
                shutil.copyfile(str(path) + extra, str(copy) + extra)
        con = sqlite3.connect(copy)
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()


ICON_PX = 32  # the icon width the tab strip draws at (16 CSS px on a 2x screen)


def read_icons(path):
    """{"pages": {page url: data address}, "origins": {origin: data address}} of a Chrome Favicons file (#5632): for
    each page the bitmap nearest ICON_PX wide. A tab whose own address has no icon yet takes its site's."""
    best = {}
    for url, width, data in _read_copy(path, "SELECT m.page_url, b.width, b.image_data FROM icon_mapping m "
                                             "JOIN favicon_bitmaps b ON b.icon_id = m.icon_id WHERE b.image_data IS NOT NULL"):
        if url not in best or abs((width or 0) - ICON_PX) < abs(best[url][0] - ICON_PX):
            best[url] = (width or 0, data)
    pages = {u: "data:image/png;base64," + base64.b64encode(d).decode() for u, (_, d) in best.items()}
    origins = {}
    for u, icon in pages.items():
        origins.setdefault(_origin(u), icon)
    return {"pages": pages, "origins": origins}


def _origin(url):
    u = urlsplit(url)
    return f"{u.scheme}://{u.netloc}"


def tab_icons(b):
    """The icons of b's profile in memory (read as _history reads History); None for a Chrome serve did not start."""
    return _profile_cached(b, b.icons, None if b.attached else b.dir / "profile" / "Default" / "Favicons", read_icons)


def tab_icon(icons, url):
    """The data address of a tab's icon, or None (the strip draws a plain mark)."""
    if not icons or not url.startswith(("http://", "https://")):
        return None
    return icons["pages"].get(url) or icons["origins"].get(_origin(url))


def _history(b):
    """The History rows of b in memory (see _profile_cached)."""
    return _profile_cached(b, b.history, _history_file(b), read_history)


def _profile_cached(b, h, path, reader):
    """What reader(path) gives for a file of b's profile, from memory. A changed file is read again at most each
    HISTORY_AGAIN seconds, in a thread: the address bar and the tab strip answer from memory (p147, under 100 ms);
    only the first read waits."""
    try:
        mtime = path.stat().st_mtime if path else None
    except OSError:
        mtime = None
    if mtime is None or mtime == h["mtime"] or h["reading"]:
        return h["rows"]

    def load():
        try:
            rows = reader(path)
            h.update(rows=rows, mtime=mtime, at=time.time())
        except Exception:  # a copy that Chrome was writing: the next suggestion reads it again
            h["mtime"] = None
        finally:
            h["reading"] = False

    if h["at"] and time.time() - h["at"] < HISTORY_AGAIN:
        return h["rows"]
    h["reading"] = True
    if h["rows"] is None:
        load()
    else:
        threading.Thread(target=load, daemon=True).start()
    return h["rows"]


def note_visit(b, url, title):
    """A page that the browser showed (its tab's address changed): the address bar suggests it at once, before Chrome
    writes its History, and for a Chrome that serve did not start, whose History serve does not read."""
    if not url.startswith(("http://", "https://")) or _SKIP.match(url):
        return
    v = b.visits.get(url)
    if v is None:
        if len(b.visits) >= HISTORY_ROWS:
            b.visits.pop(next(iter(b.visits)))
        b.visits[url] = {"url": url, "title": title, "n": 1, "at": time.time()}
    else:
        v["at"] = time.time()
        if title:
            v["title"] = title


def _bare(url):
    return re.sub(r"^https?://(www\.)?", "", url, flags=re.I)


def suggest(b, text, n=SUGGEST_MAX):
    """[{url, title}]: the addresses this browser visited that hold every word of text (in the address or the title).
    An address that starts with the text comes first (as Chrome's bar completes it), then the most visited, then the
    newest. One address once; Google's search and sign-in pages are left out."""
    words = text.lower().split()
    if not words:
        return []
    rows = {}
    for r in (_history(b) or []) + list(b.visits.values()):
        u = r["url"]
        if _SKIP.match(u) or not u.startswith(("http://", "https://")):
            continue
        hay = (u + " " + r["title"]).lower()
        if not all(w in hay for w in words):
            continue
        old = rows.get(u)
        if old is None or r["at"] > old["at"]:
            rows[u] = {**r, "n": max(r["n"], old["n"] if old else 0), "title": r["title"] or (old or {}).get("title", "")}
    head = text.strip().lower()
    order = sorted(rows.values(), key=lambda r: (not _bare(r["url"]).lower().startswith(_bare(head)),
                                                 not r["url"].lower().startswith(head), -r["n"], -r["at"]))
    return [{"url": r["url"], "title": r["title"]} for r in order[:n]]


# ---------------------------------------------------------------- the CDP address of a session (#5405)

def _cdp_key():
    """The secret of the sessions' CDP tokens: a file beside the queue (mode 0600), made at its first use, so a token
    in a session's MCP config stays good when serve starts again."""
    p = core.db_path().parent / "browsers" / "cdp.key"
    try:
        key = p.read_bytes()
        if key:
            return key
    except OSError:
        pass
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"cdp.key.{os.getpid()}.{threading.get_ident()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(os.urandom(32))
    try:
        os.link(tmp, p)  # only the first process makes it; another one reads that one
    except FileExistsError:
        pass
    finally:
        tmp.unlink()
    return p.read_bytes()


def cdp_token(project, agent):
    """The token of one session's CDP address for one project's browser."""
    return hmac.new(_cdp_key(), f"{project}\0{agent}".encode(), hashlib.sha256).hexdigest()[:40]


def cdp_url(conn, project, agent):
    from urllib.parse import quote
    return (f"ws://127.0.0.1:{core.serve_port_now(conn)}/cdp/{quote(agent, safe='')}"
            f"?project={quote(project, safe='')}&token={cdp_token(project, agent)}")


MCP_NAME = "maxpm-browser"  # its tools: mcp__maxpm-browser__browser_navigate, ...; a playwright server of the person stays
MCP_DEFAULT = "npx -y @playwright/mcp@latest"


def mcp_command(value):
    """The words that start the Playwright MCP server for the setting browser_mcp (off, on, or a command), or None."""
    v = (value or "").strip()
    if v in ("", "off"):
        return None
    try:
        words = shlex.split(MCP_DEFAULT if v == "on" else v)
    except ValueError as e:
        raise RiverError(f"browser_mcp: {e}; give off, on, or a command such as {MCP_DEFAULT}")
    if not words:
        raise RiverError("browser_mcp takes off, on, or the command that starts Playwright MCP (" + MCP_DEFAULT + ")")
    return words


def mcp_config_file(conn, project_id, agent):
    """With browser_mcp on for the project: write the session's MCP config (a Playwright MCP server with the session's
    CDP address) beside the project's browser, mode 0600 because it holds the token, and return its path; else None."""
    words = mcp_command(core.setting(conn, "browser_mcp", project_id=project_id))
    if not words:
        return None
    project = conn.execute("SELECT name FROM projects WHERE id=?", (project_id,)).fetchone()["name"]
    d = browser_dir(project) / "mcp"
    d.mkdir(parents=True, exist_ok=True)
    path = d / (re.sub(r"[^A-Za-z0-9._-]+", "_", agent).strip("._") + ".json")
    cfg = {"mcpServers": {MCP_NAME: {"command": words[0],
                                     "args": words[1:] + ["--cdp-endpoint", cdp_url(conn, project, agent)]}}}
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    tmp.replace(path)
    return str(path)


# What a session may never send through its address: it would close the shared browser, open a port of this computer
# to it, or lift the block of file:// and chrome:// pages.
CDP_NEVER = {"Browser.close", "Browser.crash", "Browser.crashGpuProcess", "Target.setRemoteLocations",
             "Target.exposeDevToolsProtocol", "Network.setBlockedURLs"}
# What changes the page (navigation, input, scripts, tabs): the controller rule holds for it, as for the actions of
# maxpm browser (routes.CHANGES). Reads (a screenshot, the DOM, the accessibility tree, a script of an isolated world:
# CDP_SCRIPTS) pass while another controls.
CDP_CHANGES = {"Page.navigate", "Page.navigateToHistoryEntry", "Page.reload", "Page.close", "Page.stopLoading",
               "Page.setDocumentContent", "Page.bringToFront", "Runtime.evaluate", "Runtime.callFunctionOn",
               "Runtime.compileScript", "Runtime.runScript", "Target.createTarget", "Target.closeTarget",
               "Target.activateTarget", "DOM.setAttributeValue", "DOM.setAttributesAsText", "DOM.setOuterHTML",
               "DOM.setNodeValue", "DOM.setNodeName", "DOM.removeNode", "DOM.removeAttribute", "DOM.moveTo",
               "DOM.setFileInputFiles", "DOM.focus"}
CDP_SCHEMES = ("http", "https", "about", "data", "blob")
# The script calls of CDP_CHANGES. In an isolated world they are reads (#5460): Playwright (its world
# __playwright_utility_world__) and Puppeteer run every read there (the accessibility snapshot, text, locators), and a
# refusal makes Playwright wait 30 s, then fail. In the page's own world (a session's page.evaluate) they wait for
# control. The rule is for sessions that work together, not a wall: a script of an isolated world can change the DOM.
CDP_SCRIPTS = {"Runtime.evaluate", "Runtime.callFunctionOn", "Runtime.compileScript", "Runtime.runScript"}


def _context_of(p):
    """The execution context id that a script call names: contextId, executionContextId, or the context part of an
    objectId (V8 writes '<isolate>.<context>.<n>', older Chrome {"injectedScriptId": <context>, ...}), or 'u:' and a
    uniqueContextId; None: the page's own world."""
    for k in ("contextId", "executionContextId"):
        if isinstance(p.get(k), int):
            return p[k]
    if isinstance(p.get("uniqueContextId"), str):
        return "u:" + p["uniqueContextId"]
    oid = p.get("objectId")
    if not isinstance(oid, str):
        return None
    parts = oid.split(".")
    if len(parts) == 3 and parts[1].lstrip("-").isdigit():
        return int(parts[1])
    try:
        j = json.loads(oid)
    except ValueError:
        return None
    return j.get("injectedScriptId") if isinstance(j, dict) and isinstance(j.get("injectedScriptId"), int) else None


def track_worlds(worlds, msg):
    """Keep worlds (a set of (sessionId, context id)) up to date with one message from Chrome: an isolated world
    comes with Runtime.executionContextCreated and goes with Destroyed or Cleared."""
    method, p, sid = msg.get("method"), msg.get("params") or {}, msg.get("sessionId")
    if method == "Runtime.executionContextCreated":
        c = p.get("context") or {}
        if (c.get("auxData") or {}).get("type") == "isolated" and isinstance(c.get("id"), int):
            worlds.add((sid, c["id"]))
            if c.get("uniqueId"):
                worlds.add((sid, "u:" + str(c["uniqueId"])))
    elif method == "Runtime.executionContextDestroyed":
        worlds.discard((sid, p.get("executionContextId")))
        if p.get("executionContextUniqueId"):
            worlds.discard((sid, "u:" + str(p["executionContextUniqueId"])))
    elif method == "Runtime.executionContextsCleared":
        for w in [w for w in worlds if w[0] == sid]:
            worlds.discard(w)


def _upload_ok(conn, b, f):
    """A file for an upload: in the project's folder or in the browser's downloads, as the side panel's rule."""
    try:
        real = Path(f).expanduser().resolve()
    except (OSError, RuntimeError, TypeError):
        return False
    r = conn.execute("SELECT path FROM projects WHERE name=?", (b.project,)).fetchone()
    roots = [b.dir / "downloads"] + ([Path(r["path"]).expanduser()] if r and r["path"] else [])
    return any(real == root.resolve() or root.resolve() in real.parents for root in roots)


def cdp_refusal(conn, b, agent, msg, worlds=()):
    """Why one CDP message of a session must not reach Chrome, or None. It may change msg: a download path becomes
    the project's downloads folder. worlds: the isolated worlds of this connection (track_worlds), where a script
    call is a read."""
    method = msg.get("method") if isinstance(msg.get("method"), str) else ""
    p = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    if method in CDP_NEVER or method.startswith("Tethering."):
        return f"{method} is not allowed through a session's CDP address"
    if method in ("Page.navigate", "Target.createTarget"):
        scheme = urlsplit(str(p.get("url") or "about:blank")).scheme.lower()
        if scheme not in CDP_SCHEMES:
            return f"the browser opens http and https addresses only, not {scheme}:"
    if method in ("Browser.setDownloadBehavior", "Page.setDownloadBehavior") and "downloadPath" in p:
        p["downloadPath"] = str(b.dir / "downloads")
    if method == "DOM.setFileInputFiles":
        bad = [f for f in p.get("files") or [] if not _upload_ok(conn, b, f)]
        if bad:
            return f"an upload takes a file from the project folder or the browser's downloads only, not {bad[0]}"
    if method in CDP_SCRIPTS and (msg.get("sessionId"), _context_of(p)) in worlds:
        return None
    if method in CDP_CHANGES or method.startswith("Input."):
        try:
            may_change(conn, b, agent, False, take=True)
        except RiverError as e:
            return str(e)
    return None


def track_targets(tabs, msg):
    """Keep tabs (CDP sessionId -> targetId of this connection) up to date with one message from Chrome."""
    method, p = msg.get("method"), msg.get("params") or {}
    if method == "Target.attachedToTarget" and p.get("sessionId"):
        tabs[p["sessionId"]] = (p.get("targetInfo") or {}).get("targetId")
    elif method == "Target.detachedFromTarget":
        tabs.pop(p.get("sessionId"), None)


def relay_input(b, agent, msg, tabs):
    """Record one Input.* message of a session's CDP connection for the panel (#5616): the pointer of a mouse event,
    the text of a key or of insertText. Keys of a tab that the panel does not show are not kept."""
    method, p = msg.get("method"), msg.get("params") or {}
    target = tabs.get(msg.get("sessionId"))
    if not target:
        return
    who = driver(b, agent)
    if method == "Input.dispatchMouseEvent" and p.get("type") in ("mouseMoved", "mousePressed", "mouseReleased"):
        x, y = p.get("x"), p.get("y")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            note_pointer(b, who, x, y, press=p["type"] == "mousePressed", target=target)
        return
    if target != b.target:
        return
    hide = lambda: secret(b)  # noqa: E731
    if method == "Input.insertText" and isinstance(p.get("text"), str):
        # A fill can focus another field with no press (a script's focus()): look at the field each time.
        note_keys(b, who, p["text"], secret=secret(b))
    elif method == "Input.dispatchKeyEvent" and p.get("type") in ("keyDown", "rawKeyDown", "char"):
        key, text = p.get("key") or "", p.get("text") or ""
        if key in KEY_SIGNS and p["type"] != "char":
            note_keys(b, who, key=key, hidden=hide)
        elif text and text not in "\r\n" and p["type"] in ("keyDown", "char") and not (p.get("modifiers", 0) & 6):
            # The keys of one text come close together; after a pause the focus may be in another field.
            t = b.typed
            again = not t or time.time() - t["at"] > KEY_PAUSE
            note_keys(b, who, text, secret=secret(b) if again else None, hidden=hide)


def cdp_session(conn, b, agent, rfile, sock, upstream, on_input=None):
    """Relay one session's CDP connection until either side closes it. rfile and sock: the session's WebSocket (the
    server side: frames out are not masked); upstream: a WS to Chrome of this connection only, so the ids and events
    of two sessions never mix. A refused message gets a CDP error from serve and never reaches Chrome.
    on_input: called at each input that passes (the page shows the browser in the session's side panel). The
    session's input goes to the panel too (relay_input), and with the setting browser_shot_cursor on, a screenshot of
    the shown tab has the pointer drawn in (#5616)."""
    wlock, worlds, tabs, shots = threading.Lock(), set(), {}, set()

    def out(op, data):
        with wlock:
            sock.sendall(ws_frame(op, data, mask=False))

    def pump():
        try:
            while True:
                text = upstream.recv()
                if '"Runtime.executionContext' in text:  # the isolated worlds, where scripts are reads (#5460)
                    try:
                        track_worlds(worlds, json.loads(text))
                    except (ValueError, AttributeError):
                        pass
                elif '"Target.attachedToTarget"' in text or '"Target.detachedFromTarget"' in text:
                    try:
                        track_targets(tabs, json.loads(text))
                    except (ValueError, AttributeError):
                        pass
                elif shots and text.startswith('{"id":'):
                    try:
                        i = json.loads(text).get("id")
                    except ValueError:
                        i = None
                    if i in shots:
                        shots.discard(i)
                        cursor_off(b)
                out(0x1, text.encode())
        except (EOFError, OSError):
            pass
        for step in (lambda: out(0x8, b""), lambda: sock.shutdown(socket.SHUT_RDWR)):
            try:
                step()
            except OSError:
                pass

    t = threading.Thread(target=pump, daemon=True, name=f"cdp-{agent}")
    t.start()
    parts = []
    try:
        while True:
            op, fin, data = ws_read(rfile)
            if op == 0x8:
                break
            if op == 0x9:
                out(0xA, data)
                continue
            if op == 0xA:
                continue
            parts.append(data)
            if not fin:
                continue
            text, parts = b"".join(parts).decode("utf-8", "replace"), []
            try:
                msg = json.loads(text)
            except ValueError:
                msg = None
            why = cdp_refusal(conn, b, agent, msg, worlds) if isinstance(msg, dict) else "a CDP message is one JSON object"
            if why:
                reply = {"id": msg.get("id") if isinstance(msg, dict) else None,
                         "error": {"code": -32000, "message": f"maxpm: {why}"}}
                if isinstance(msg, dict) and msg.get("sessionId"):
                    reply["sessionId"] = msg["sessionId"]
                out(0x1, json.dumps(reply).encode())
                continue
            method = msg.get("method") or ""
            if method.startswith("Input.") or method == "Page.captureScreenshot":
                try:
                    if method.startswith("Input."):
                        relay_input(b, agent, msg, tabs)
                        if on_input:
                            on_input()
                    elif (tabs.get(msg.get("sessionId")) == b.target and msg.get("id") is not None
                          and setting_of(b.project, "browser_shot_cursor") == "on" and cursor_on(b)):
                        shots.add(msg["id"])
                except Exception:  # the panel's view never stops the session's input
                    pass
            upstream.send(json.dumps(msg))
    except (EOFError, OSError):
        pass
    finally:
        upstream.close()
        t.join(5)


# ---------------------------------------------------------------- the browsers of this serve

BROWSERS = {}
_lock = threading.Lock()


def setting_of(project, key, parse=None):
    """A project's setting, read at once (a changed setting holds at the next action)."""
    conn = core.connect()
    try:
        r = conn.execute("SELECT id FROM projects WHERE name=?", (project,)).fetchone()
        v = core.setting(conn, key, project_id=r[0] if r else None)
    finally:
        conn.close()
    return parse(v) if parse else v


def get(project, owner=None, conn=None):
    """The browser of one session in a project (#5636), or the project's own with no session (page walks). In a Chrome
    that serve did not start (browser_source), a session's browser is a browser context of its own there (#5634).
    Each call counts as a use for browser_idle_end."""
    with _lock:
        b = BROWSERS.get((project, owner))
        if b is None:
            b = BROWSERS[(project, owner)] = Browser(project, owner)
        b.used = time.time()
        return b


def find(project, owner=None):
    """The browser that a panel shows, if serve has it: the session's, else (an attached Chrome) the project's."""
    b = BROWSERS.get((project, owner or None))
    if b is None and owner:
        b = BROWSERS.get((project, None))
        b = b if b is not None and b.attached else None
    if b is not None:
        b.used = time.time()
    return b


def sweep(conn, at=None):
    """A pass of maxpm serve (#5636): stop each session's browser whose session is gone (unregistered), or that nobody
    used for browser_idle_end. A stopped browser starts again at its next use. Returns the (project, session) pairs
    that it stopped."""
    at = at or time.time()
    out = []
    for project, owner, why in window_sweep(conn, at):
        print(f"browser {project}/{owner or '-'}: back in the panel ({why})", flush=True)
    for (project, owner), b in list(BROWSERS.items()):
        if not owner:
            continue
        gone = not conn.execute("SELECT 1 FROM agents WHERE name=?", (owner,)).fetchone()
        r = conn.execute("SELECT id FROM projects WHERE name=?", (project,)).fetchone()
        idle = core.parse_duration(core.setting(conn, "browser_idle_end", project_id=r[0] if r else None)).total_seconds()
        if not gone and (b.watching or at - b.used < idle):
            continue
        if b.running() or b.proxy is not None:
            b.close()
            out.append((project, owner))
        if gone:
            with _lock:
                BROWSERS.pop((project, owner), None)
    # A Chrome that an earlier serve started for a session, and that nobody used since this serve started.
    for f in core.db_path().parent.glob("browsers/*/sessions/*/state.json"):
        try:
            st = json.loads(f.read_text(encoding="utf-8"))
            idle_for = at - f.stat().st_mtime
        except (OSError, ValueError):
            continue
        project, owner, pid = st.get("project"), st.get("owner"), st.get("pid")
        if not (project and owner and pid) or (project, owner) in BROWSERS or not core.pid_alive(pid):
            continue
        gone = not conn.execute("SELECT 1 FROM agents WHERE name=?", (owner,)).fetchone()
        r = conn.execute("SELECT id FROM projects WHERE name=?", (project,)).fetchone()
        idle = core.parse_duration(core.setting(conn, "browser_idle_end", project_id=r[0] if r else None)).total_seconds()
        if gone or idle_for >= idle:
            Browser(project, owner).close()
            out.append((project, owner))
    return out


def project_of(conn, project=None, agent=None, cwd=None):
    """The project whose browser a command means: named; else the project of the item the agent holds, or of its
    last item; else the project of the folder."""
    if project:
        return core._project(conn, project)["name"]
    if agent:
        # A project with a folder first (#5478): a deploy-* project has none, and a session whose last item was a
        # deploy check works in the folder of a real project.
        for folder in ("AND p.path IS NOT NULL", ""):
            r = conn.execute(f"""SELECT p.name FROM items i JOIN projects p ON p.id=i.project_id
                                 WHERE i.assignee=? AND i.status IN ('in_progress','held') {folder}
                                 ORDER BY i.claimed_at DESC LIMIT 1""", (agent,)).fetchone()
            if not r:
                r = conn.execute(f"""SELECT p.name FROM events e JOIN items i ON i.id=e.item_id
                                     JOIN projects p ON p.id=i.project_id WHERE e.actor=? {folder}
                                     ORDER BY e.id DESC LIMIT 1""", (agent,)).fetchone()
            if r:
                return r[0]
    if cwd:
        found = core.projects_for_dir(conn, cwd)
        if len(found) == 1:
            return found[0]
    names = [r[0] for r in conn.execute("SELECT name FROM projects WHERE archived=0")]
    if len(names) == 1:
        return names[0]
    raise RiverError("name the project of the browser: --project <name>"
                     + (" (this session holds no item of a project)" if agent else ""))


# ---- the lock of a shared browser (#5406): browser_lock and browser_unlock, run when the controller changes

LOCK_RENEW = 600     # seconds between two runs of browser_lock while one holder keeps control
LOCK_WAIT = 60       # seconds that one lock command may take
LOCK_RUNNER = None   # tests: (command, folder) -> (exit code, output)


def _lock_run(conn, b, key, who, mode=None):
    """Run the project's browser_lock, browser_unlock or browser_mode for who: (ok, output). No command set: (True,
    None)."""
    r = conn.execute("SELECT id, path FROM projects WHERE name=?", (b.project,)).fetchone()
    cmd = (core.setting(conn, key, project_id=r["id"] if r else None) or "").strip()
    if not cmd:
        return True, None
    cmd = cmd.replace("{who}", shlex.quote(who))
    if mode:
        cmd = cmd.replace("{mode}", mode)
    cwd = os.path.expanduser(r["path"]) if r and r["path"] else None
    if LOCK_RUNNER:
        code, out = LOCK_RUNNER(cmd, cwd)
        return code == 0, out or ""
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=LOCK_WAIT)
    except subprocess.TimeoutExpired:
        return False, f"{key} did not end within {LOCK_WAIT}s"
    return p.returncode == 0, (p.stdout + p.stderr).strip() or f"exit {p.returncode}"


def _take_lock(conn, b, who):
    ok, out = _lock_run(conn, b, "browser_lock", who)
    if not ok:
        raise RiverError(f"the browser lock of {b.project} refused {who}: {out}. Wait until the lock is free, then "
                         f"take control again")
    if out is not None:  # a lock command ran: renew it while who acts
        b.state["locked"] = {"who": who, "at": time.time()}


def _give_lock(conn, b, who):
    _lock_run(conn, b, "browser_unlock", who)
    if (b.state.get("locked") or {}).get("who") == who:
        b.state.pop("locked", None)


def _renew(conn, b, c):
    """The controller keeps its lock while it acts: browser_lock again after LOCK_RENEW."""
    lk = b.state.get("locked")
    if c and lk and lk.get("who") == c["who"] and time.time() - lk.get("at", 0) > LOCK_RENEW:
        _take_lock(conn, b, c["who"])
        b._save()


# ---- the controller rule: one controller at a time

def controller_gone(conn, c):
    """A session that controls the browser and is gone (unregistered, or its process ended) holds it no more."""
    if not c or c.get("kind") != "session":
        return False
    return not conn.execute("SELECT 1 FROM agents WHERE name=?", (c["who"],)).fetchone()


def _ago(sec):
    sec = int(max(0, sec))
    return f"{sec}s" if sec < 120 else f"{sec // 60}m"


def why_take(conn, b, who):
    """(True, why) when the session who may take the browser from the person now (#5629): the person's newest
    message in its chat is newer than their last input in the browser, or they have not used the browser for
    browser_take_after. Else (False, why not)."""
    c = b.state.get("controller") or {}
    since = core.parse_iso(c["since"]).timestamp() if c.get("since") else 0
    last = max(b.person_at, since)
    r = conn.execute("SELECT person_said_at FROM agents WHERE name=?", (who,)).fetchone()
    said = core.parse_iso(r["person_said_at"]).timestamp() if r and r["person_said_at"] else 0
    if said > last:  # the queue keeps whole seconds: within one second the person's input wins
        return True, "you asked in its chat"
    after = core.parse_duration(setting_of(b.project, "browser_take_after")).total_seconds()
    if time.time() - last >= after:
        return True, f"you had not used it for {_ago(time.time() - last)}"
    return False, (f"the person ({c.get('who')}) controls the browser of {b.project} and used it {_ago(time.time() - last)} "
                   f"ago, with no message in your chat since; ask them there, or wait until they have not used it for "
                   f"{_ago(after)}")


def take_from_person(conn, b, who, sub=None):
    """The session takes the browser from the person (#5629), when why_take allows it; RiverError with the reason
    when not. The panel says who took it and why; Take control gives it back to the person."""
    ok, why = why_take(conn, b, who)
    if not ok:
        raise RiverError(why)
    c = b.state["controller"]
    _give_lock(conn, b, c["who"])
    try:
        _take_lock(conn, b, who)
    except RiverError:
        _lock_run(conn, b, "browser_lock", c["who"])
        raise
    b.state.pop("before", None)
    b.state["controller"] = {"who": who, "kind": "session", "sub": (sub or "").strip() or None,
                             "since": core.iso(core.now()), "took": why, "from": c["who"]}
    b._save()
    item = conn.execute("SELECT id FROM items WHERE assignee=? AND status IN ('in_progress','held') ORDER BY claimed_at "
                        "DESC LIMIT 1", (who,)).fetchone()
    with core.tx(conn):
        core._event(conn, item["id"] if item else None, who,
                    f"took the browser of {b.project} from {c['who']} ({why})")
    _notify(b)
    return b.info()


def may_change(conn, b, who, person, take=False):
    """RiverError when who may not change the page now (navigate, click, type, run a script). take: a session's
    input through its Playwright tools takes the browser from the person when why_take allows it (#5629)."""
    c = b.state.get("controller")
    if controller_gone(conn, c):
        b.state.pop("controller", None)
        _give_lock(conn, b, c["who"])
        b._save()
        c = None
    if not c:
        return
    if person:
        if c["kind"] == "person":
            return _renew(conn, b, c)
        raise RiverError(f"{c['who']} controls the browser of {b.project}"
                         f"{' (subagent ' + c['sub'] + ')' if c.get('sub') else ''}: press Take control first")
    if c["kind"] == "person":
        if take:
            take_from_person(conn, b, who)
            return
        raise RiverError(f"the person ({c['who']}) controls the browser of {b.project}: maxpm browser control --take "
                         f"takes it when they asked you in your chat or stopped using it; else wait")
    if c["who"] != who:
        raise RiverError(f"the browser of {b.project} is controlled by {c['who']}; maxpm browser status")
    _renew(conn, b, c)


def control(conn, b, who, sub=None, take=False):
    """The session takes control (sub: the label of the subagent it gives the browser to). take: also from the
    person, when they asked in the session's chat or stopped using the browser (#5629)."""
    if not who:
        raise RiverError("name the session: maxpm --as <you> browser control")
    c = b.state.get("controller")
    if c and not controller_gone(conn, c) and c["who"] != who:
        if c["kind"] == "person":
            if take:
                return take_from_person(conn, b, who, sub)
            raise RiverError(f"the person ({c['who']}) controls the browser of {b.project}: maxpm browser control "
                             f"--take takes it when they asked you in your chat or stopped using it; else wait")
        raise RiverError(f"the browser of {b.project} is controlled by {c['who']}; maxpm browser status")
    if c and c["who"] != who:  # a gone session's lock goes first
        _give_lock(conn, b, c["who"])
    _take_lock(conn, b, who)
    b.state["controller"] = {"who": who, "kind": "session", "sub": (sub or "").strip() or None, "since": core.iso(core.now())}
    b._save()
    _notify(b)
    return b.info()


def release(conn, b, who):
    c = b.state.get("controller")
    if c and c["who"] != who and not controller_gone(conn, c):
        raise RiverError(f"the browser of {b.project} is controlled by {c['who']}, not by {who}")
    b.state.pop("controller", None)
    if c:
        _give_lock(conn, b, c["who"])
    b._save()
    _notify(b)
    return b.info()


def take(conn, b, who):
    """The person takes control in the panel; Give back returns it to the session that held it."""
    c = b.state.get("controller")
    who = who or "person"
    if c and c["who"] != who:  # the session's lock goes to the person; when the lock refuses, the session keeps it
        _give_lock(conn, b, c["who"])
        try:
            _take_lock(conn, b, who)
        except RiverError:
            _lock_run(conn, b, "browser_lock", c["who"])
            raise
    else:
        _take_lock(conn, b, who)
    if c and c["kind"] == "session":
        b.state["before"] = {k: v for k, v in c.items() if k not in ("took", "from")}
    b.state["controller"] = {"who": who, "kind": "person", "sub": None, "since": core.iso(core.now())}
    b.person_at = time.time()
    b._save()
    _notify(b)
    return b.info()


def give_back(conn, b, who):
    before = b.state.pop("before", None)
    c = b.state.pop("controller", None)
    if c:
        _give_lock(conn, b, c["who"])
    if before and not controller_gone(conn, before):
        try:
            _take_lock(conn, b, before["who"])
        except RiverError:
            b._save()
            _notify(b)
            raise
        b.state["controller"] = before
    b._save()
    _notify(b)
    return b.info()


def hand(conn, b, who, to):
    """The person hands the browser to a session (Hand to in the panel, #5616): that session controls it now."""
    c = b.state.get("controller")
    if c and c["kind"] == "person" and c["who"] != (who or "person"):
        raise RiverError(f"{c['who']} controls the browser of {b.project}")
    if c:
        _give_lock(conn, b, c["who"])
    b.state.pop("before", None)
    b.state.pop("controller", None)
    try:
        _take_lock(conn, b, to)
    except RiverError:
        b._save()
        _notify(b)
        raise
    b.state["controller"] = {"who": to, "kind": "session", "sub": None, "since": core.iso(core.now())}
    b._save()
    _notify(b)
    return b.info()


def attached_mode(conn, b, who, window):
    """Open as a window, or Back to the panel, for an attached Chrome (browser_source cdp, #5619): browser_mode starts
    that Chrome again in the mode, with the same profile, while who holds its lock, because the restart closes every
    tab in it (the walks of other sessions too). A lock that another holder keeps refuses it."""
    who = who or "person"
    if window == bool(b.attached_window) and b.running():
        return b.info()
    lk = b.state.get("locked")
    if lk and lk.get("who") != who:
        raise RiverError(f"{lk['who']} holds the lock of the browser of {b.project}, and a new mode starts that Chrome "
                         f"again: press Take control first")
    mine = not lk
    if mine:
        _take_lock(conn, b, who)
    try:
        with b.lock:
            b._stop_chrome()
            ok, out = _lock_run(conn, b, "browser_mode", who, "window" if window else "headless")
            if out is None:
                raise RiverError(f"the browser of {b.project} is a Chrome that runs already (browser_source): set the "
                                 f"command that starts it again as a window or headless, maxpm config set browser_mode "
                                 f"'bash scripts/test-browser.sh {{mode}}' --project {b.project}")
            if not ok:
                raise RiverError(f"browser_mode of {b.project} failed: {out}")
            b.start()
    finally:
        if mine:
            _give_lock(conn, b, who)
            b._save()
    _notify(b)
    return b.info()


def _notify(b):
    with b.cond:
        b.seq += 1
        b.cond.notify_all()


# ---- a window ends by itself (#5650): a browser shown as a window goes back into the panel, headless

def mark_window(b, who, person, window):
    """Record who opened the browser as a window and when (the panel shows it), or that it went back."""
    if window:
        b.state["window_by"] = {"who": who or "the person", "person": bool(person), "at": core.iso(core.now())}
        b.state.pop("window_end", None)
    else:
        b.state.pop("window_by", None)
    b._save()


def may_open_window(conn, b, who):
    """A session opens the browser as a window only when the person asked for it in its chat within
    browser_window_idle; RiverError says why not."""
    idle = core.parse_duration(setting_of(b.project, "browser_window_idle")).total_seconds()
    r = conn.execute("SELECT person_said_at FROM agents WHERE name=?", (who,)).fetchone()
    said = core.parse_iso(r["person_said_at"]).timestamp() if r and r["person_said_at"] else 0
    if time.time() - said > idle:
        raise RiverError(f"a session opens the browser of {b.project} as a window only when the person asked for it in "
                         f"its chat (in the last {_ago(idle)}): ask them there; the person presses Open as a window "
                         f"in the panel")


def end_window(conn, b, why):
    """Back into the panel, headless: serve's own Chrome starts again with no window, an attached one through
    browser_mode (under its lock)."""
    if b.attached:
        attached_mode(conn, b, "maxpm", False)
    else:
        b.start(window=False)
    b.state.pop("window_by", None)
    b.state["window_end"] = {"why": why, "at": core.iso(core.now())}
    b._save()
    _notify(b)


def window_sweep(conn, at=None):
    """A pass of maxpm serve: each browser shown as a window with no input and no change of its page for
    browser_window_idle, and each attached Chrome whose window was closed, goes back into the panel. Returns the
    (project, owner, why) of each."""
    at = at or time.time()
    out = []
    for (project, owner), b in list(BROWSERS.items()):
        why = None
        if b.attached and b.attached_window and not b.running():
            why = "the window was closed"
        elif b.running() and b._windowed():
            by = (b.state.get("window_by") or {}).get("at")
            last = max(b.active_at, b.person_at, core.parse_iso(by).timestamp() if by else 0)
            idle = core.parse_duration(setting_of(project, "browser_window_idle")).total_seconds()
            if at - last >= idle:
                why = f"no input in the window for {_ago(at - last)}"
        if not why:
            continue
        try:
            end_window(conn, b, why)
            out.append((project, owner, why))
        except RiverError:
            pass  # another holder keeps the lock of a shared Chrome: the next pass tries again
    return out


# ---- the panel's frames and input

def frame(b, after, timeout=8, pane=None, many=False):
    """The newest frame after seq `after` (a long poll); with no new frame within timeout, the status only. pane: the
    panel that asks; pane_known says whether serve has its size (after a restart of serve it has none).
    many (#5637): the panel keeps several polls open at once (a phone over the relay, where each poll costs a round
    trip). Each answer then moves the pane's cursor, so the next poll that waits gets the next change, never one that
    another poll took; at most as many frames as polls are on the way, each the newest when it left. A poll that
    waits the whole timeout gets the frame after `after` itself, in case an answer was lost."""
    known = b.pane_seen(pane) if pane else None
    start = time.time()
    end = start + timeout
    again = not b.watching and start - b.watched > WATCH_GAP
    b.watched = start
    if again and b.dsf > 1 and b.frame and not b.frame.get("sharp"):
        b._want_sharp()  # the panel watches again: the frame that came while nobody watched is not sharp
    key = str(pane)[:40] if many and pane else None
    with b.cond:
        b.watching += 1
        try:
            while True:  # the cursor again at each wake: another poll of the pane may have taken the change
                mark = max(after, b.sent.get(key, 0)) if key else after
                if b.seq > mark or time.time() >= end:
                    break
                b.cond.wait(max(0.05, end - time.time()))
        finally:
            b.watching -= 1
        f = b.frame
        seq = b.seq
        if key:
            if seq <= mark:
                mark = after  # nothing new for the whole poll: the panel's own seq says what it has
            if len(b.sent) > 50:
                b.sent.clear()
            b.sent[key] = max(b.sent.get(key, 0), seq)
    b.watched = time.time()
    out = b.info()
    out["seq"] = seq
    out["waited"] = round((time.time() - start) * 1000)  # ms in serve: the panel tells the link's time from it
    if pane:
        out["pane_known"] = known
    if f and (not mark or f["seq"] > mark):
        out["frame"] = {"data": f["data"], "w": f["w"], "h": f["h"], "sharp": bool(f.get("sharp")), "seq": f["seq"]}
    out.update(drawn(b))
    return out


def frame_bytes(out):
    """frame() as one binary answer (#5637): 4 bytes of the length of the JSON head (big-endian), the head, then the
    JPEG as it is. As base64 in JSON the frame had a third more bytes, serve gzipped it again for each answer, and the
    page decoded it again."""
    f = out.pop("frame", None)
    jpeg = b""
    if f:
        jpeg = base64.b64decode(f.pop("data"))
        out["frame"] = f
    head = json.dumps(out, default=str).encode()
    return struct.pack(">I", len(head)) + head + jpeg


BUTTONS = {"left": 1, "right": 2, "middle": 4}  # CDP's buttons: the bit of each button that is held


def input_calls(ev):
    """The CDP calls of one input of the person (mouse, wheel, a key, a pasted text), in the page's CSS pixels:
    [(method, params)]. RiverError for an input it does not know."""
    kind = ev.get("type")
    if kind == "mouse":
        action = ev.get("action")
        t = {"down": "mousePressed", "up": "mouseReleased", "move": "mouseMoved", "wheel": "mouseWheel"}.get(action)
        if not t:
            raise RiverError(f"unknown mouse action {action!r}")
        p = {"type": t, "x": float(ev.get("x", 0)), "y": float(ev.get("y", 0)), "modifiers": modifiers(ev.get("mods"))}
        if t in ("mousePressed", "mouseReleased"):
            p.update(button=ev.get("button") or "left", clickCount=int(ev.get("clicks") or 1))
        if t == "mouseMoved" and ev.get("button") in BUTTONS:  # a drag: the page sees the button held
            p.update(button=ev["button"], buttons=BUTTONS[ev["button"]])
        if t == "mouseWheel":
            p.update(deltaX=float(ev.get("dx", 0)), deltaY=float(ev.get("dy", 0)))
        return [("Input.dispatchMouseEvent", p)]
    if kind == "key":
        return [("Input.dispatchKeyEvent", p) for p in key_events(ev.get("key") or "", ev.get("mods"))]
    if kind == "text":
        return [("Input.insertText", {"text": str(ev.get("text") or "")})]
    raise RiverError(f"unknown input {kind!r}")


def person_input(b, ev):
    """One input of the person, sent to the page now; the call returns after Chrome took it."""
    for method, params in input_calls(ev):
        b.page(method, params)
    return {"ok": True}


INPUT_GAP = 0.3     # seconds that a pane's batch waits for the batches before it (they took another path) at most
INPUT_SHOWS = 1.5   # seconds after the person's input in which a frame of a look seen before still shows
INPUT_IDLE = 30.0   # seconds with no input after which the thread that sends it to Chrome ends


class Inputs:
    """The person's input of one browser, sent to Chrome in the order the person made it (#5651). The panel sends each
    batch at once, with its number (n, counted by the pane), and does not wait for the answer of the one before: a
    queue of requests behind the slow answers of a phone's link was the lag. Requests can come out of order (each
    has a thread of serve, and the relay serves several at once), so a batch waits until the batches before it came,
    at most INPUT_GAP. The request answers at once; one thread sends the calls to Chrome and waits for each answer,
    so Chrome takes them in order. When Chrome is slower than the input, the calls that wait join: of several
    moves in a row only the newest goes, and wheel steps in a row add up. A press, a release or a key never joins."""

    def __init__(self, b):
        self.b = b
        self.cond = threading.Condition()
        self.calls = []      # (method, params) to send, in order
        self.next = {}       # pane -> the number of its next batch
        self.held = {}       # pane -> {n: (calls, when it came)}: batches that came before the ones before them
        self.thread = None
        self.busy = False    # a call is with Chrome now
        self.error = None    # the last refusal of Chrome: the next answer to the panel says it

    def put(self, pane, n, calls):
        """Take a batch of the pane (n: its number; None: no number, it goes in the order it came)."""
        with self.cond:
            if n is None:
                self.calls.extend(calls)
            else:
                pane = str(pane or "")[:40]
                if len(self.next) > 50:
                    self.next.clear()
                # A pane counts from 1; a high first number is a pane that a serve before this one knew.
                want = self.next.setdefault(pane, 1 if n <= 8 else n)
                if n < want:
                    return  # an old number: a pane that started again counts from 1 (#5651)
                self.held.setdefault(pane, {})[n] = (calls, time.monotonic())
                self._release(pane)
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, daemon=True, name=f"maxpm-input-{self.b.project}")
                self.thread.start()
            self.cond.notify_all()

    def _release(self, pane, late=False):
        """Inside cond: move the pane's batches that are next in turn to the calls; late: also go past a missing
        batch that did not come in INPUT_GAP."""
        held = self.held.get(pane)
        while held:
            want = self.next[pane]
            if want not in held:
                first = min(held)
                if not (late and time.monotonic() - held[first][1] >= INPUT_GAP):
                    return
                want = first  # the batch before it is lost (a request that failed): go on without it
            self.calls.extend(held.pop(want)[0])
            self.next[pane] = want + 1
        self.held.pop(pane, None)

    def _take(self):
        """Inside cond: the next call, joined with the moves and wheel steps after it."""
        method, params = self.calls.pop(0)
        if method == "Input.dispatchMouseEvent" and params["type"] in ("mouseMoved", "mouseWheel"):
            while self.calls and self.calls[0][0] == method and self.calls[0][1]["type"] == params["type"] \
                    and self.calls[0][1]["modifiers"] == params["modifiers"]:
                nxt = self.calls.pop(0)[1]
                # Wheel steps the same way add up; a step the other way replaces them: the page turns at once.
                if params["type"] == "mouseWheel" and not (params["deltaY"] * nxt["deltaY"] < 0
                                                           or params["deltaX"] * nxt["deltaX"] < 0):
                    nxt = {**nxt, "deltaX": params["deltaX"] + nxt["deltaX"], "deltaY": params["deltaY"] + nxt["deltaY"]}
                params = nxt
        return method, params

    def _run(self):
        idle = time.monotonic()
        while True:
            with self.cond:
                self.busy = False
                self.cond.notify_all()
                while not self.calls:
                    for pane in list(self.held):
                        self._release(pane, late=True)
                    if self.calls:
                        break
                    if not self.held and time.monotonic() - idle > INPUT_IDLE:
                        self.thread = None
                        return
                    self.cond.wait(INPUT_GAP / 3 if self.held else 1.0)
                method, params = self._take()
                self.busy = True
            try:
                if self.b.running():  # a browser that closed meanwhile: page() would start it again
                    self.b.page(method, params)
            except DialogOpen:  # the pane shows the box and its buttons (#5674): no error under it
                pass
            except RiverError as e:
                self.error = str(e)
            except Exception as e:  # the thread must not end with calls in the queue
                self.error = f"the browser did not take the input: {e}"
            idle = time.monotonic()

    def settle(self, timeout=5.0):
        """Wait until every call that came went to Chrome (tests, and the speed script)."""
        end = time.monotonic() + timeout
        with self.cond:
            while (self.calls or self.held or self.busy) and time.monotonic() < end:
                self.cond.wait(0.05)
            return not (self.calls or self.held or self.busy)

    def refusal(self):
        """The last refusal of Chrome since the last answer, once."""
        e, self.error = self.error, None
        return e


# ---- what the panel draws over the page (#5616): the driving session's pointer and the keys it types

GLIDE_TIME = 0.5    # seconds that a click of maxpm browser moves the pointer before it presses, while someone watches
GLIDE_STEPS = 14
KEY_TIME = 0.06     # seconds between two keys of maxpm browser type, while someone watches
TYPE_MAX = 8.0      # seconds that one maxpm browser type takes at most: a long text goes faster
TYPED_GAP = 4.0     # seconds after a key that the panel still shows the text
TYPED_KEEP = 48     # the characters of the text that the panel shows
KEY_PAUSE = 0.3     # seconds between two keys of the relay after which the focused field is looked at again
WATCH_GAP = 4.0     # seconds after the panel's last frame poll that someone still watches
# What the panel shows for a key that types no text.
KEY_SIGNS = {"Enter": "⏎", "Tab": "⇥", "Backspace": "⌫", "Delete": "⌦", "Escape": "⎋", "ArrowLeft": "←",
             "ArrowRight": "→", "ArrowUp": "↑", "ArrowDown": "↓", "Home": "⇱", "End": "⇲", "PageUp": "⇞",
             "PageDown": "⇟"}
# Is the focused field secret (a password or a one-time code)? Through open shadow roots and same-origin frames.
SECRET_JS = ("(() => { let e = document.activeElement; try { for (let i = 0; i < 20 && e; i++) { "
             "const n = (e.shadowRoot && e.shadowRoot.activeElement) || (e.contentDocument && e.contentDocument.activeElement); "
             "if (!n) break; e = n; } } catch (x) {} if (!e) return false; "
             "const t = (e.getAttribute && (e.getAttribute('type') || '')).toLowerCase(), "
             "a = (e.getAttribute && (e.getAttribute('autocomplete') || '')).toLowerCase(); "
             "return t === 'password' || /password|one-time-code/.test(a) || "
             "(e.style && /disc|circle|square/.test(getComputedStyle(e).webkitTextSecurity || '')); })()")


def driver(b, who):
    """The name that the panel shows beside the pointer: the session, and the subagent it named."""
    c = b.state.get("controller") or {}
    return f"{who} · {c['sub']}" if who and c.get("who") == who and c.get("sub") else (who or "a session")


def pace(b):
    """True: move and type at a person's pace (the setting browser_pace: watch, while the panel shows the browser;
    person, always; fast, never)."""
    v = setting_of(b.project, "browser_pace")
    if v == "person":
        return True
    if v == "fast":
        return False
    return b.watching > 0 or time.time() - b.watched < WATCH_GAP


def secret(b):
    """Whether the focused field of the shown tab hides its text; True when the page does not answer."""
    try:
        return b.evaluate(SECRET_JS, 5) is not False
    except RiverError:
        return True


def note_pointer(b, who, x, y, press=False, target=None):
    p = b.pointer or {}
    n = {"x": round(float(x), 1), "y": round(float(y), 1), "who": who, "target": target or b.target,
         "n": p.get("n", 0) + 1, "press": p.get("press", 0), "px": p.get("px"), "py": p.get("py")}
    if press:
        n.update(press=n["press"] + 1, px=n["x"], py=n["y"])
        if b.typed:  # a press can move the focus to another field: the next key looks at its field again
            b.typed["done"] = True
    b.pointer = n
    _notify(b)


def note_keys(b, who, text="", key=None, hidden=None, target=None, secret=None):
    """One key or a text of the driving session. hidden: a function that says whether the field is secret; it runs
    when a new run of keys starts (after a gap, a press, Enter or Tab). secret: known already (maxpm browser type
    looked); a run of another kind starts a new one. A secret field's text is never kept: the panel shows dots."""
    now, target = time.time(), target or b.target
    t = b.typed
    if (not t or t["who"] != who or t["target"] != target or t.get("done") or now - t["at"] > TYPED_GAP
            or (secret is not None and t["secret"] != bool(secret))):
        hide = bool(secret) if secret is not None else bool(hidden and hidden())
        t = {"who": who, "target": target, "text": "", "secret": hide, "n": (t or {}).get("n", 0)}
    if key and key in KEY_SIGNS:
        add = KEY_SIGNS[key]
        t["done"] = key in ("Enter", "Tab")  # the focus may move: the next key looks at its field again
    else:
        add = "•" * len(text) if t["secret"] else text.replace("\r", "").replace("\n", "⏎")
    t.update(text=(t["text"] + add)[-TYPED_KEEP:], at=now, n=t["n"] + 1)
    b.typed = t
    _notify(b)


def drawn(b):
    """What the panel draws over the frame: the pointer and the keys of the shown tab."""
    out = {}
    p = b.pointer
    if p and p["target"] == b.target:
        out["pointer"] = {k: v for k, v in p.items() if k != "target"}
    t = b.typed
    if t and t["target"] == b.target:
        out["typed"] = {"who": t["who"], "text": t["text"], "secret": t["secret"], "n": t["n"],
                        "age": round(time.time() - t["at"], 2)}
    return out


def glide(b, who, x, y):
    """Move the pointer to (x, y): along a short curve in steps while someone watches, so hover menus open on the
    way and the panel shows the move; else at once."""
    p = b.pointer if b.pointer and b.pointer["target"] == b.target else None
    if pace(b):
        if p:
            x0, y0 = p["x"], p["y"]
        else:  # the first move comes from up and to the left
            x0, y0 = max(0.0, x - 160), max(0.0, y - 90)
        dx, dy = x - x0, y - y0
        dist = (dx * dx + dy * dy) ** 0.5
        if dist > 4:
            bend = min(80.0, dist * 0.15)  # the control point stands beside the middle of the line
            cx, cy = x0 + dx / 2 - dy / dist * bend, y0 + dy / 2 + dx / dist * bend
            for i in range(1, GLIDE_STEPS):
                u = i / GLIDE_STEPS
                u = u * u * (3 - 2 * u)  # slow at the start and at the end
                px = (1 - u) ** 2 * x0 + 2 * (1 - u) * u * cx + u * u * x
                py = (1 - u) ** 2 * y0 + 2 * (1 - u) * u * cy + u * u * y
                b.page("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": px, "y": py})
                note_pointer(b, who, px, py)
                time.sleep(GLIDE_TIME / GLIDE_STEPS)
    b.page("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": float(x), "y": float(y)})
    note_pointer(b, who, x, y)


def char_events(ch):
    """The CDP key events that type one character, as a person's key press."""
    if ch in "\r\n":
        return key_events("Enter")
    if ch == "\t":
        return key_events("Tab")
    vk = ord(ch.upper()) if ch.isascii() and ch.isalnum() else 0
    return [{"type": "keyDown", "key": ch, "text": ch, "unmodifiedText": ch, "windowsVirtualKeyCode": vk},
            {"type": "keyUp", "key": ch, "windowsVirtualKeyCode": vk}]


# ---- what an action waits for (#5663): the next page that it led to, or nothing

NAV_EVENTS = {"Page.frameStartedLoading", "Page.frameStoppedLoading", "Page.frameNavigated",
              "Page.frameRequestedNavigation", "Page.frameClearedScheduledNavigation", "Page.navigatedWithinDocument",
              "Network.requestWillBeSent", "Network.loadingFinished", "Network.loadingFailed"}
NAV_GRACE = 0.02    # seconds in which a navigation that an action caused has started (a link, a form, Enter):
                    # Chrome said so within 2 ms on a local page; a later start is caught by loading() below
LOADING_WAIT = 5.0  # seconds that a snapshot or a ref waits for the page that loads now
NAV_START = 3.0     # --wait load: seconds in which the next page must start to load
                    # #5717: also what a click on a link to another address of the same site waits for the page's
                    # script to change the address (a client route: The Infatuation's did after 1.2 s)
ROUTE_TITLE = 1.0   # #5717: seconds that a client route waits for its new title (the page's script sets it later)
IDLE_QUIET = 0.5    # --wait idle: seconds with no request in flight
IDLE_WAIT = 10.0    # --wait idle: seconds that it waits at most
IDLE_OLD = 10.0     # a request open longer than this (a long poll, a stream) does not count
WAITS = ("auto", "load", "idle", "none")


def _pause(sec):
    threading.Event().wait(sec)  # not time.sleep: the tests replace it to count a person's pace


def track_nav(b, method, p):
    """The shown tab's loads and requests, from its CDP events (Browser._event)."""
    if method.startswith("Network."):
        rid = p.get("requestId")
        if method == "Network.requestWillBeSent":
            if p.get("type") not in ("EventSource", "WebSocket"):
                b.requests[rid] = time.time()
        else:
            b.requests.pop(rid, None)
        b.net_at = time.time()
        return
    if p.get("frameId") != b.target and not (method == "Page.frameNavigated" and not p["frame"].get("parentId")):
        return  # an inner frame
    if method == "Page.frameRequestedNavigation":  # #5674: the page asked for the next page; it starts to load later
        if p.get("disposition", "currentTab") == "currentTab":  # (400 ms later after a box that asks to leave)
            b.asks += 1
            b.nav.set()
        return
    if method in ("Page.frameClearedScheduledNavigation", "Page.navigatedWithinDocument"):
        b.asks_end = b.asks  # it started, or it stays in this document (a link to #part)
        if method == "Page.navigatedWithinDocument":  # #5717: the page's script changed the address
            b.within += 1
            b.stale = True
            if b.target in b.targets and p.get("url"):
                b.targets[b.target]["url"] = p["url"]
    elif method == "Page.frameStartedLoading":
        b.navs += 1
        b.nav.set()
    elif method == "Page.frameStoppedLoading":
        b.stops = b.navs
    else:  # a new document: the refs of the snapshot went with the old one
        b.snap = None
        b.requests.clear()
    with b.cond:
        b.cond.notify_all()


def loading(b):
    """Wait (LOADING_WAIT at most) while the shown tab loads a new page: a snapshot or a ref then reads the new page,
    also when the action before returned before the load started."""
    if b.navs != b.stops:
        with b.cond:
            b.cond.wait_for(lambda: b.stops == b.navs or b.dialog, LOADING_WAIT)


# ---- a box of the page (#5674): alert, confirm, prompt. While one shows, the page's script stops at it, and so does
# every CDP call that needs the page (a script, a click, a screenshot): each would wait CALL_WAIT and fail with no
# cause. So a call that waits ends when a box opens, a later call fails at once and names the box, and the session
# answers it with maxpm browser dialog accept|dismiss (the person with the buttons of the pane). A box that asks
# whether to leave the page (beforeunload) is accepted at once: the action that leaves the page asked for it.

DIALOG_CLOSE = 1.0  # seconds that an answer waits for Chrome's word that the box closed


class DialogOpen(RiverError):
    """A box of the page waits for an answer."""

    def __init__(self, b, dialog):
        self.browser, self.dialog = b, dialog
        super().__init__(dialog_text(dialog))


def dialog_info(d):
    """A box as the replies and the pane show it."""
    return {"type": d["type"], "message": d["message"], "default": d.get("default", ""), "url": d.get("url", "")} \
        if d else None


def dialog_text(d):
    """The refusal of a command while the box shows: the box, its text, and how to answer it."""
    accept = 'maxpm browser dialog accept "<text>"' if d["type"] == "prompt" else "maxpm browser dialog accept"
    return f'the page shows a {d["type"]} box {json.dumps(d["message"], ensure_ascii=False)}: {accept}, or ' \
           f"maxpm browser dialog dismiss"


def track_dialog(b, method, p, session):
    """Page.javascriptDialogOpening and Closed of the shown tab (Browser._event)."""
    if method == "Page.javascriptDialogOpening":
        if p.get("type") == "beforeunload":
            b.send("Page.handleJavaScriptDialog", {"accept": True}, session=session)
            return
        d = b.dialog = {"type": p.get("type") or "alert", "message": p.get("message") or "",
                        "default": p.get("defaultPrompt") or "", "url": p.get("url") or "", "session": session,
                        "at": time.time()}
        for w in list(b.wait.values()):  # the calls that wait for this page: they end now and say why
            if w[2] == session and w[1] is None:
                w[1] = {"dialog": d}
                w[0].set()
        b.nav.set()
    else:
        b.dialog = None
    with b.cond:
        b.seq += 1  # the panel's poll answers at once with the box, or without it
        b.cond.notify_all()


def answer_dialog(b, accept=True, text=None, who=None, wait=None):
    """Accept or dismiss the box of the page; text: the answer of a prompt box. Then the page goes on, and the reply
    is the page as the action that follows the answer left it (a new page, or a next box)."""
    d = b.dialog
    if not d:
        raise RiverError("the page shows no alert, confirm or prompt box")
    if text is not None and d["type"] != "prompt":
        raise RiverError(f"a {d['type']} box takes no text: maxpm browser dialog accept")
    mark = nav_mark(b)
    p = {"accept": bool(accept)}
    if text is not None:
        p["promptText"] = text
    try:
        b.call("Page.handleJavaScriptDialog", p, session=d["session"])
    except RiverError as e:
        if "no dialog" not in str(e).lower():
            raise  # else someone else answered it a moment before (the person in the pane)
    with b.cond:  # Chrome answers before the page goes on (18 ms on a local page); the page goes on at the close
        b.cond.wait_for(lambda: b.dialog is not d, DIALOG_CLOSE)
    if b.dialog is d:
        b.dialog = None
    driver(b, who)
    return done(b, wait, mark, answered={"type": d["type"], "message": d["message"], "accept": bool(accept)})


def nav_mark(b):
    """Before an action: what after() compares with."""
    b.nav.clear()
    return {"navs": b.navs, "opened": b.opened, "target": b.target, "asks": b.asks, "within": b.within,
            "title": (b.targets.get(b.target) or {}).get("title"), "url": (b.targets.get(b.target) or {}).get("url")}


def route_title(b, mark):
    """After the page's script went to another address (#5717): wait ROUTE_TITLE at most for its new title, then
    read the address and title from the page."""
    end = time.time() + (ROUTE_TITLE if mark.get("link") else 0)
    while time.time() < end:
        try:
            if b.evaluate("document.title", 1) != mark.get("title"):
                break
        except RiverError:
            break
        _pause(0.05)
    b.here()


def net_idle(b):
    end = time.time() + IDLE_WAIT
    while time.time() < end:
        now = time.time()
        if not any(now - t < IDLE_OLD for t in list(b.requests.values())) and now - b.net_at >= IDLE_QUIET:
            return True
        _pause(0.05)
    return False


def after(b, wait, mark):
    """Wait as --wait says after an action (auto: for the next page when the action led to one, else not at all;
    load: for the next page, which must start within NAV_START; idle: also until no request is in flight; none:
    not at all). True when a new page or a new tab came, or the page's script went to another address (#5717)."""
    wait = wait or "auto"
    if wait == "none":
        return False
    moved = lambda: b.navs != mark["navs"] or b.opened != mark["opened"]  # noqa: E731
    routed = lambda: b.within != mark.get("within", b.within)  # noqa: E731
    if not moved() and not b.dialog:
        b.nav.wait(NAV_START if wait == "load" else NAV_GRACE)
    if not moved() and b.asks != mark["asks"] and b.asks_end != b.asks and not b.dialog:
        with b.cond:  # the page asked for the next page, which has not started to load yet
            b.cond.wait_for(lambda: moved() or b.asks_end == b.asks or b.dialog, NAV_START)
    if not moved() and not routed() and mark.get("link") and not b.dialog:
        with b.cond:  # #5717: a link to another address of the site, which the page's script may follow later
            b.cond.wait_for(lambda: moved() or routed() or b.dialog, NAV_START)
    if not moved():
        if routed() and not b.dialog:
            route_title(b, mark)
            return urldefrag(b.info()["url"])[0] != urldefrag(mark.get("url") or "")[0]  # a #part is no new page
        if wait == "idle" and not b.dialog:
            net_idle(b)
        return False
    end = time.time() + LOAD_WAIT
    if b.opened != mark["opened"]:  # a new tab: serve shows it once it is attached (_switch_quiet)
        while time.time() < end and b.target == mark["target"]:
            _pause(0.05)
    else:  # a box of the next page stops its load (#5674): the reply names the box
        with b.cond:
            b.cond.wait_for(lambda: b.stops == b.navs or b.target != mark["target"] or b.dialog,
                            max(0.0, end - time.time()))
    b.settle(max(0.5, end - time.time()))
    new = True
    if routed() and not b.dialog:  # Chrome also says 'loading' for a client route; its title comes after (#5717)
        route_title(b, mark)
        new = b.navs - mark["navs"] > b.within - mark["within"] or b.opened != mark["opened"] or \
            urldefrag(b.info()["url"])[0] != urldefrag(mark.get("url") or "")[0]  # a #part is no new page
    if wait == "idle":
        net_idle(b)
    return new


# ---- the snapshot (#5663): the interactive elements of the page, each with a ref (@e1, ...) that later actions name

SNAP_MAX = 400      # lines that one snapshot prints at most; --scope shows a part of a larger page
SNAP_WORLD = "maxpm"
# It runs in a world of its own (Page.createIsolatedWorld): the page's scripts do not see the refs, and a new
# document ends the world, so a ref of an earlier page fails instead of naming another element. A ref stays with its
# element while the element stays: a later snapshot of the same page gives it the same ref.
# #5671: by default it lists what is in the view (o.view), with a last line that counts the rest; o.region lists one
# region of the page (nav, main, a label or a heading), o.find the elements whose name or context holds the words.
# Repeated rows (siblings of the same structure, as the stories of Hacker News) print one line each, and a name that
# says too little (empty, one sign, or the same as another's) gets its row, card or column, then a test id or class.
SNAP_JS = r"""(function (scope, all, start, delta, max, o) {
  o = o || {};
  const R = window.__maxpm || (window.__maxpm = {ref: new WeakMap(), els: new Map(), last: null});
  const REGION = 'header,nav,main,footer,aside,dialog,[role=banner],[role=navigation],[role=main],[role=contentinfo],' +
    '[role=complementary],[role=dialog],[role=alertdialog],[role=search],[role=menu],[role=form]';
  const KIND = {HEADER: 'header', NAV: 'nav', MAIN: 'main', FOOTER: 'footer', ASIDE: 'aside', DIALOG: 'dialog',
    banner: 'header', navigation: 'nav', main: 'main', contentinfo: 'footer', complementary: 'aside', dialog: 'dialog',
    alertdialog: 'dialog', search: 'search', menu: 'menu', form: 'form'};
  const kindOf = r => KIND[(r.getAttribute('role') || '').trim()] || KIND[r.tagName] || 'region';
  const regionName = r => { const l = (r.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
    return kindOf(r) + (l ? ' ' + JSON.stringify(l.slice(0, 30)) : ''); };
  let root = document.documentElement, scoped = false;
  if (scope) {
    try { root = document.querySelector(scope); } catch (e) { return {error: 'not a CSS selector: ' + scope}; }
    if (!root) return {error: 'no element matches ' + scope};
    scoped = true;
  }
  const regions = () => Array.from(document.querySelectorAll(REGION)).filter(r => r.getClientRects().length);
  if (o.region) {
    const want = o.region.toLowerCase().trim(), rs = regions();
    const head = r => { const h = r.querySelector('h1,h2,h3,h4,h5,h6'); return h ? h.textContent.toLowerCase() : ''; };
    root = rs.find(r => kindOf(r) === want) || rs.find(r => regionName(r).toLowerCase().includes(want))
      || rs.find(r => head(r).includes(want));
    if (!root) return {error: 'no region ' + JSON.stringify(o.region) + ' on the page; it has ' +
      (Array.from(new Set(rs.map(regionName))).slice(0, 12).join(', ') || 'none: use --all or --scope <css>')};
    scoped = true;
  }
  const SEL = 'a[href],button,input,select,textarea,summary,[contenteditable=""],[contenteditable=true],[onclick],' +
    '[tabindex]:not([tabindex="-1"]),[role=button],[role=link],[role=checkbox],[role=radio],[role=switch],[role=tab],' +
    '[role=menuitem],[role=menuitemcheckbox],[role=menuitemradio],[role=option],[role=combobox],[role=textbox],' +
    '[role=searchbox],[role=slider],[role=spinbutton],[role=treeitem]';
  const INPUT = {checkbox: 'checkbox', radio: 'radio', submit: 'button', button: 'button', reset: 'button',
    image: 'button', range: 'slider', number: 'spinbutton', search: 'searchbox', file: 'file'};
  const cut = (s, n) => { s = (s || '').replace(/\s+/g, ' ').trim(); return s.length > n ? s.slice(0, n - 1) + '…' : s; };
  const text = e => e.childElementCount > 30 ? e.textContent : e.innerText;
  const shown = e => {
    if (!e.getClientRects().length) return false;
    const v = getComputedStyle(e).visibility; return v !== 'hidden' && v !== 'collapse';
  };
  const role = e => {
    const r = (e.getAttribute('role') || '').trim().split(/\s+/)[0]; if (r) return r;
    const t = e.tagName;
    if (t === 'A') return 'link';
    if (t === 'BUTTON' || t === 'SUMMARY') return 'button';
    if (t === 'INPUT') return INPUT[(e.type || 'text').toLowerCase()] || 'textbox';
    if (t === 'SELECT') return e.multiple ? 'listbox' : 'combobox';
    if (t === 'TEXTAREA' || e.isContentEditable) return 'textbox';
    return 'clickable';
  };
  const name = e => {
    let s = e.getAttribute('aria-label');
    if (s && s.trim()) return s;
    const by = e.getAttribute('aria-labelledby');
    if (by) { s = by.split(/\s+/).map(i => { const n = document.getElementById(i); return n ? text(n) : ''; }).join(' ');
      if (s.trim()) return s; }
    if (e.labels && e.labels.length) { s = Array.from(e.labels).map(text).join(' '); if (s.trim()) return s; }
    const t = e.tagName, ty = (e.type || '').toLowerCase();
    if (t === 'INPUT' && ['submit', 'button', 'reset'].includes(ty)) return e.value || ty;
    if (t === 'INPUT' && ty === 'image') return e.alt || 'image';
    if (t === 'INPUT' || t === 'TEXTAREA' || t === 'SELECT') return e.placeholder || e.title || e.name || '';
    s = text(e);
    if (!s || !s.trim()) {
      const i = e.querySelector('img[alt],[aria-label],[title],svg title');
      s = i ? (i.tagName.toLowerCase() === 'title' ? i.textContent
        : i.getAttribute('alt') || i.getAttribute('aria-label') || i.getAttribute('title')) : '';
    }
    return s || e.title || '';
  };
  const extra = (e, r) => {
    let s = '';
    const t = e.tagName, ty = (e.type || '').toLowerCase();
    if (t === 'SELECT') s += ' value=' + JSON.stringify(cut(Array.from(e.selectedOptions).map(o => o.label).join(', '), 40));
    else if ((t === 'INPUT' && !['checkbox', 'radio', 'submit', 'button', 'reset', 'image'].includes(ty)) || t === 'TEXTAREA') {
      if (e.value) s += ty === 'password' ? ' value=•••' : ' value=' + JSON.stringify(cut(e.value, 40));
    }
    if (e.checked || e.getAttribute('aria-checked') === 'true') s += ' checked';
    const x = e.getAttribute('aria-expanded'); if (x === 'true') s += ' expanded'; else if (x === 'false') s += ' collapsed';
    if (e.getAttribute('aria-selected') === 'true') s += ' selected';
    if (e.disabled || e.getAttribute('aria-disabled') === 'true') s += ' disabled';
    if (r === 'link' && !name(e).trim()) s += ' href=' + JSON.stringify(cut(e.getAttribute('href'), 50));
    return s;
  };
  // The context of an element whose name says too little: the first heading or text of its row, card or list item
  // (not its own), or the header of its table column; else a test id or a class (icon-trash).
  const CONT = 'li,tr,article,section,fieldset,dialog,dd,[role=row],[role=listitem],[role=dialog],[role=article],' +
    '[role=gridcell],[role=group]';
  const firstText = (c, skip) => {
    const h = c.querySelector('h1,h2,h3,h4,h5,h6,legend,caption,[role=heading]');
    if (h && !h.contains(skip) && !skip.contains(h) && text(h).trim()) return cut(text(h), 40);
    const w = document.createTreeWalker(c, NodeFilter.SHOW_TEXT);
    for (let t = w.nextNode(); t; t = w.nextNode()) {
      if (skip.contains(t)) continue;
      const s = t.textContent.replace(/\s+/g, ' ').trim();
      if (/[\p{L}\p{N}]{2,}/u.test(s) && t.parentElement && t.parentElement.getClientRects().length) return cut(s, 40);
    }
    return '';
  };
  const context = (e, own) => {
    for (let c = e.parentElement, i = 0; c && c !== document.body && i < 8; c = c.parentElement, i++) {
      if (!c.matches(CONT)) continue;
      const t = firstText(c, e);
      if (t && t !== own) return t;
    }
    return '';
  };
  const column = e => {
    const td = e.closest('td'), tb = td && td.closest('table');
    const hr = tb && (tb.querySelector('thead tr') || tb.rows[0]), th = hr && hr.cells[td.cellIndex];
    return th && th.tagName === 'TH' && !th.contains(e) ? cut(text(th), 30) : '';
  };
  const hint = e => {
    const t = e.getAttribute('data-testid') || e.getAttribute('data-test') || e.getAttribute('data-qa');
    if (t) return ' testid=' + cut(t, 30);
    const c = e.querySelector('[class]'), cls = Array.from(e.classList).concat(c ? Array.from(c.classList) : []);
    const k = cls.find(x => /icon|ico-|fa-|close|delete|trash|edit|menu|search|arrow|vote|like|share|more/i.test(x)) || cls[0];
    return k ? ' class=' + cut(k, 30) : '';
  };
  const weak = s => !/[\p{L}\p{N}]{2,}/u.test(s);
  // Rows: the children of one parent whose structure (tag and classes) repeats, in chunks of 1 to 3 children (a story
  // of Hacker News is three rows of a table), at least 3 times.
  const sig = e => e.tagName + '.' + Array.from(e.classList).sort().join('.');
  const groups = new Map();
  let gid = 0;
  const groupOf = p => {
    if (groups.has(p)) return groups.get(p);
    let best = null;
    const ch = p.children;
    if (ch.length >= 3 && ch.length <= 3000) {
      const s = Array.from(ch, sig);
      for (let per = 1; per <= 3; per++) {
        for (let off = 0; off < per; off++) {
          let runAt = off, run = 0, prev = null;
          for (let i = off; i + per <= s.length; i += per) {
            const k = s.slice(i, i + per).join('|');
            if (per > 1 && s.slice(i, i + per).every(x => x === s[i])) { prev = null; run = 0; continue; }
            if (k === prev) run++; else { prev = k; run = 1; runAt = i; }
            if (run >= 3 && (!best || run * per > best.n * best.per || (run * per === best.n * best.per && per < best.per)))
              best = {at: runAt, per, n: run};
          }
        }
      }
    }
    if (best) {
      best.id = ++gid;
      best.parent = p;
      best.idx = new Map();
      for (let i = best.at; i < best.at + best.n * best.per; i++) best.idx.set(ch[i], Math.floor((i - best.at) / best.per));
    }
    groups.set(p, best);
    return best;
  };
  const chain = e => {  // the groups that hold e, nearest first, with its row in each
    const out = [];
    for (let a = e, i = 0; a && a.parentElement && i < 12; a = a.parentElement, i++) {
      const g = groupOf(a.parentElement);
      if (g && g.idx.has(a)) out.push({g, row: g.idx.get(a)});
    }
    return out;
  };
  const inView = e => {
    const r = e.getBoundingClientRect();
    return r.width + r.height > 0 && r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
  };
  // #5670: the outline, the map from which the agent picks a region or a section: each region with its counts of
  // links, buttons, fields and repeated rows, and the headings h1 to h3 under it (OUTLINE_HEADS at most).
  if (o.outline) {
    const FIELD = new Set(['textbox', 'searchbox', 'combobox', 'listbox', 'checkbox', 'radio', 'switch', 'slider',
      'spinbutton', 'file']);
    const regs = new Map(), heads = [], per = new Map(), els = [];
    const at = e => { const r = e.closest(REGION); if (!regs.has(r)) regs.set(r, {link: 0, button: 0, field: 0, rows: 0, heads: []}); return regs.get(r); };
    const look = node => {
      const w = document.createTreeWalker(node, NodeFilter.SHOW_ELEMENT);
      for (let e = w.currentNode; e; e = w.nextNode()) {
        if (e.nodeType !== 1) continue;
        if (/^H[1-3]$/.test(e.tagName)) { if (shown(e) && text(e).trim()) { const h = [+e.tagName[1], cut(text(e), 50)]; heads.push(h); at(e).heads.push(h); } }
        else if (e.matches(SEL) && !(e.tagName === 'INPUT' && e.type === 'hidden') && e.getClientRects().length) {
          const r = role(e), c = at(e);
          c[r === 'link' ? 'link' : FIELD.has(r) ? 'field' : 'button']++;
          const ch = chain(e);
          els.push(ch);
          ch.forEach(({g, row}) => { const k = g.id + ':' + row; per.set(k, (per.get(k) || 0) + 1); });
        }
        if (e.shadowRoot) look(e.shadowRoot);
      }
    };
    look(document.documentElement);
    const fill = new Map();  // group id -> [rows with elements, elements]
    per.forEach((v, k) => { const id = k.split(':')[0], a = fill.get(id) || [0, 0]; a[0]++; a[1] += v; fill.set(id, a); });
    const counted = new Set();
    els.forEach(ch => {
      const c = ch.find(({g}) => { const a = fill.get(String(g.id)); return a && a[0] >= 2 && a[1] / a[0] >= 2; });
      if (c && !counted.has(c.g)) { counted.add(c.g); at(c.g.parent).rows += c.g.n; }
    });
    // The headings to print: h1 and h2 first, then h3, in page order, OUTLINE_HEADS at most.
    let keep = new Set(heads.filter(h => h[0] < 3).slice(0, o.heads));
    heads.filter(h => h[0] === 3).slice(0, Math.max(0, o.heads - keep.size)).forEach(h => keep.add(h));
    const lines = [], plural = (n, w) => n + ' ' + w + (n === 1 ? '' : 's');
    regs.forEach((c, r) => {
      const nums = [c.link && plural(c.link, 'link'), c.button && plural(c.button, 'button'),
        c.field && plural(c.field, 'field'), c.rows && plural(c.rows, 'row')].filter(Boolean);
      const hs = c.heads.filter(h => keep.has(h));
      if (!nums.length && !hs.length) return;
      lines.push('[' + (r ? regionName(r) : 'page') + '] ' + (nums.join(', ') || 'no elements'));
      hs.forEach(h => lines.push('  ' + '#'.repeat(h[0]) + ' ' + h[1]));
    });
    if (heads.length > keep.size) lines.push('(' + (heads.length - keep.size) + ' more headings: maxpm browser snapshot --all)');
    return {lines, url: location.href, title: document.title};
  }
  // #5667: the elements of the page with one name (and role), in a row, card or region whose text holds o.target.within;
  // each match gets a ref. A name that ends with … (as a snapshot cuts it) matches the names that start so.
  if (o.target) {
    const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase(), t = o.target;
    const want = norm(t.name), pre = want.endsWith('…') ? want.slice(0, -1) : null, inn = t.within ? norm(t.within) : null;
    const rowText = e => {  // the first text of the nearest repeated row that has text besides e (cells of a row: the row)
      for (const {g, row} of chain(e)) {
        for (const k of [...g.idx].filter(([, r]) => r === row).map(([k]) => k)) { const s = firstText(k, e); if (s) return s; }
      }
      return '';
    };
    const inside = e => {
      if (!inn) return true;
      for (const {g, row} of chain(e))
        if (norm([...g.idx].filter(([, r]) => r === row).map(([k]) => k.innerText).join(' ')).includes(inn)) return true;
      for (let c = e.parentElement; c && c !== document.documentElement; c = c.parentElement) {
        if (c.matches(CONT) && norm(firstText(c, e)).includes(inn)) return true;
        if (c.matches(REGION) && (norm(regionName(c)).includes(inn) || norm(firstText(c, e)).includes(inn))) return true;
      }
      return false;
    };
    const hits = [], names = new Map();
    const look = node => {
      const w = document.createTreeWalker(node, NodeFilter.SHOW_ELEMENT);
      for (let e = w.currentNode; e; e = w.nextNode()) {
        if (e.nodeType === 1 && e.matches(SEL) && !(e.tagName === 'INPUT' && e.type === 'hidden') && shown(e)) {
          const r = role(e), full = name(e), nm = norm(full);
          if ((nm === want || (pre && nm.startsWith(pre))) && (!t.role || r === t.role)) { if (inside(e)) hits.push(e); }
          else if (nm && names.size < 3000) names.set(r + ' ' + JSON.stringify(cut(full, 60)), nm);
        }
        if (e.nodeType === 1 && e.shadowRoot) look(e.shadowRoot);
      }
    };
    look(document.documentElement);
    let n = start;
    const out = {next: n, hits: hits.slice(0, 12).map(e => {
      let ref = R.ref.get(e);
      if (!ref || !R.els.has(ref)) { ref = 'e' + (++n); R.ref.set(e, ref); }
      R.els.set(ref, new WeakRef(e));
      const nm = cut(name(e), 60), c = rowText(e) || context(e, nm) || column(e);
      return {ref, line: '@' + ref + ' ' + role(e) + ' ' + JSON.stringify(nm) + extra(e, role(e)) +
        (c ? ' (in ' + JSON.stringify(c) + ')' : '') + (inView(e) ? '' : ' (out of the window)')};
    }), count: hits.length};
    out.next = n;
    if (!hits.length) {  // the nearest names: they hold the name, the name holds them, or they share words with it
      const ws = want.split(' ').filter(x => x.length > 1), score = nm =>
        (nm.includes(want) ? 3 : 0) + (nm.length > 2 && want.includes(nm) ? 2 : 0) + ws.filter(x => nm.includes(x)).length;
      out.near = [...names].map(([k, nm]) => [score(nm), k]).filter(([s]) => s > 0).sort((a, b) => b[0] - a[0])
        .slice(0, 5).map(([, k]) => k);
    }
    return out;
  }
  const view = o.view && !scoped && !o.find && !delta;
  const words = o.find ? [o.find.toLowerCase().replace(/\s+/g, ' ').trim()] : null;  // the words as one phrase
  const items = [], now = {};
  let n = start, more = 0, outside = 0, frames = 0;
  const visit = node => {
    const w = document.createTreeWalker(node, NodeFilter.SHOW_ELEMENT);
    for (let e = w.currentNode; e; e = w.nextNode()) {
      if (e.nodeType !== 1) continue;  // a shadow root
      const head = all && !words && /^H[1-6]$/.test(e.tagName);
      if (e.tagName === 'IFRAME' && shown(e)) frames++;
      else if ((head || e.matches(SEL)) && !(e.tagName === 'INPUT' && e.type === 'hidden')) {
        if (view && !inView(e)) { if (e.getClientRects().length) outside++; }
        else if (!shown(e)) { /* hidden */ }
        else if (head) items.push({head: '#'.repeat(+e.tagName[1]) + ' ' + cut(text(e), 80), e});
        else add(e);
      }
      if (e.shadowRoot) visit(e.shadowRoot);
    }
  };
  const add = e => {
    const r = role(e), nm = cut(name(e), 60);
    if (words) {
      const hay = (r + ' ' + nm + ' ' + context(e, nm) + ' ' + column(e)).toLowerCase();
      if (!words.every(x => hay.includes(x))) return;
    }
    if (items.length >= max) { more++; return; }
    let ref = R.ref.get(e);
    if (!ref || !R.els.has(ref)) { ref = 'e' + (++n); R.ref.set(e, ref); }
    R.els.set(ref, new WeakRef(e));
    const x = extra(e, r);
    now[ref] = '@' + ref + ' ' + r + ' ' + JSON.stringify(nm) + x;
    items.push({e, ref, r, nm, x});
  };
  visit(root);
  for (const [k, w] of R.els) { const e = w.deref(); if (!e || !e.isConnected) R.els.delete(k); }
  const out = {lines: [], more, outside, frames, next: n, url: location.href, title: document.title};
  const last = R.last && R.last.scope === scope ? R.last.lines : null;
  R.last = {scope, lines: now};
  if (delta && last) {  // one line for each element that is new or changed, not folded
    out.delta = true;
    out.lines = items.filter(i => i.head || last[i.ref] !== now[i.ref]).map(i => i.head || now[i.ref]);
    out.removed = Object.keys(last).filter(k => !(k in now)).map(k => '@' + k);
    return out;
  }
  if (outside) out.regions = Array.from(new Set(regions().map(kindOf))).slice(0, 8);
  // Each element goes to its nearest group whose rows hold two or more listed elements on average (a list of single
  // links stays one line each).
  if (!words) {
    const per = new Map(), chains = items.map(i => i.ref ? chain(i.e) : []);
    chains.forEach(c => c.forEach(({g, row}) => { const k = g.id + ':' + row; per.set(k, (per.get(k) || 0) + 1); }));
    const rows = new Map();
    per.forEach((v, k) => { const id = k.split(':')[0]; const a = rows.get(id) || [0, 0]; a[0]++; a[1] += v; rows.set(id, a); });
    items.forEach((i, x) => {
      const c = chains[x].find(({g}) => { const a = rows.get(String(g.id)); return a && a[0] >= 2 && a[1] / a[0] >= 2; });
      if (c) { i.g = c.g; i.row = c.row; }
    });
    items.forEach((i, x) => {  // a heading inside a row joins the row
      if (i.head) { const c = chain(i.e).find(({g}) => items.some(j => j.g === g)); if (c) { i.g = c.g; i.row = c.row; } }
    });
  }
  const label = i => '@' + i.ref + (i.r === 'link' ? '' : ' ' + i.r) + ' ' + JSON.stringify(i.nm) + i.x;
  const solo = items.filter(i => i.ref && !i.g), seen = new Map();
  solo.forEach(i => { const k = i.r + i.nm; seen.set(k, (seen.get(k) || 0) + 1); });
  const plain = i => {
    let s = '@' + i.ref + ' ' + i.r + ' ' + JSON.stringify(i.nm) + i.x;
    const low = i.nm.toLowerCase();  // find: an element that matched by its row or column shows that row or column
    if (weak(i.nm) || seen.get(i.r + i.nm) > 1 || (words && !words.every(w => low.includes(w)))) {
      const c = context(i.e, i.nm), col = column(i.e);
      if (c) s += ' (in ' + JSON.stringify(c) + ')';
      if (col) s += ' (column ' + JSON.stringify(col) + ')';
      if (!c && !col && weak(i.nm)) s += hint(i.e);
    }
    return s;
  };
  let region = null;
  const at = e => { const r = e.closest(REGION); if (r !== region) { region = r; if (r && !scoped) out.lines.push('[' + regionName(r) + ']'); } };
  for (let x = 0; x < items.length;) {
    const i = items[x];
    at(i.e);
    if (!i.g) { out.lines.push(i.head || plain(i)); x++; continue; }
    let y = x;  // the block: the items of this group that follow each other
    while (y < items.length && items[y].g === i.g) y++;
    const block = items.slice(x, y), byRow = new Map();
    block.forEach(j => { if (!byRow.has(j.row)) byRow.set(j.row, []); byRow.get(j.row).push(j); });
    const keys = it => it.filter(j => j.ref).map(j => j.r + ' ' + j.nm);
    let same = null;  // the names that every row has: printed once, in the head
    if (byRow.size >= 2) byRow.forEach(r => { const k = new Set(keys(r)); same = same ? new Set([...same].filter(s => k.has(s))) : k; });
    same = same ? [...same] : [];
    const first = [...byRow.values()][0], tell = s => {  // a weak name in the head: its role and test id or class
      const j = first.find(j => j.ref && j.r + ' ' + j.nm === s);
      return weak(j.nm) ? (j.r + hint(j.e)).trim() : JSON.stringify(j.nm);
    };
    out.lines.push(byRow.size + (byRow.size < i.g.n ? ' of ' + i.g.n : '') + ' rows' +
      (same.length ? ' (each ends with ' + same.map(tell).join(', ') + ')' : '') + ':');
    byRow.forEach((r, k) => {
      const ends = same.map(s => r.find(j => j.ref && j.r + ' ' + j.nm === s)).filter(Boolean);
      const mid = r.filter(j => !ends.includes(j)).map(j => j.head ? JSON.stringify(j.head.replace(/^#+ /, '')) : label(j));
      out.lines.push('  ' + (k + 1) + '. ' + mid.join(' ') + (ends.length ? ' · ' + ends.map(j => '@' + j.ref).join(' ') : ''));
    });
    x = y;
  }
  return out;
})"""


def world(b, create=True):
    """The execution context of the snapshot's world in the shown tab; None when it has none (and not create)."""
    s = b.snap
    if s and b.session and s["session"] == b.session:
        return s["ctx"]
    if not create:
        return None
    b.page("Page.getFrameTree")  # starts the browser and attaches the shown tab, when needed
    ctx = b.page("Page.createIsolatedWorld", {"frameId": b.target, "worldName": SNAP_WORLD})["executionContextId"]
    b.snap = {"session": b.session, "target": b.target, "ctx": ctx}
    return ctx


NO_WORLD = object()


def in_world(b, expr, create=True):
    """Run expr in the snapshot's world; NO_WORLD when the page has none (a new page since the snapshot)."""
    for again in (False, True):
        ctx = world(b, create=create)
        if ctx is None:
            return NO_WORLD
        try:
            r = b.page("Runtime.evaluate", {"expression": expr, "contextId": ctx, "returnByValue": True,
                                            "awaitPromise": True, "userGesture": True})
        except RiverError as e:
            if "context" not in str(e).lower() or again:
                raise
            b.snap = None  # the page went on to a new document
            if not create:
                return NO_WORLD
            continue
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            raise RiverError("the script failed in the page: "
                             + ((d.get("exception") or {}).get("description") or d.get("text") or "an error"))
        return (r.get("result") or {}).get("value")
    return NO_WORLD


def snapshot(b, interactive=False, scope=None, delta=False, limit=SNAP_MAX, view=True, region=None, find=None):
    """The page's interactive elements as short lines, each with a ref; without interactive, its headings too.
    delta: only the lines that are new or changed since the last snapshot of the same page and scope, and the refs
    that went. #5671: view (the default) lists what shows in the window and counts the rest; region: one region of
    the page; find: the elements whose name or context holds the words (as one phrase), anywhere on the page."""
    loading(b)
    opts = {"view": bool(view), "region": region or None, "find": find or None}
    r = in_world(b, "(%s)(%s, %s, %d, %s, %d, %s)" % (SNAP_JS, json.dumps(scope), json.dumps(not interactive), b.refs,
                                                       json.dumps(bool(delta)), int(limit), json.dumps(opts)))
    if r is NO_WORLD or not isinstance(r, dict):
        raise RiverError("the page did not answer the snapshot: run maxpm browser snapshot again")
    if r.get("error"):
        raise RiverError(r["error"])
    b.refs = max(b.refs, r.pop("next"))
    b.here()  # #5717: the header names the page as it is now, also after its script changed the address
    return {**b.info(), "snapshot": r}


def as_ref(target):
    """'@e3' or 'e3' -> 'e3'; None for a CSS selector."""
    m = re.fullmatch(r"@?(e\d+)", (target or "").strip())
    return m.group(1) if m else None


ROLES = {"link", "button", "textbox", "searchbox", "checkbox", "radio", "switch", "combobox", "listbox", "slider",
         "spinbutton", "tab", "menuitem", "menuitemcheckbox", "menuitemradio", "option", "treeitem", "file", "clickable"}


OUTLINE_HEADS = 12  # headings that an outline prints at most


def outline(b, heads=OUTLINE_HEADS):
    """#5670: the page's regions with their counts of links, buttons, fields and repeated rows, and its headings h1 to
    h3: about 0.5 KB, from which the agent picks a region (snapshot --region) or a section."""
    loading(b)
    r = in_world(b, "(%s)(null, false, %d, false, 0, %s)" % (SNAP_JS, b.refs, json.dumps({"outline": True,
                                                                                           "heads": int(heads)})))
    if r is NO_WORLD or not isinstance(r, dict):
        raise RiverError("the page did not answer the outline: run maxpm browser outline again")
    return r


TEXT_MAX = 3800  # bytes of text that maxpm browser text prints by default: with its last line, under 4 KB
# #5672: the text of the main content (main, role=main, article; else the body) as a person reads it, without the
# menus, side bars and footers inside it; or one section by its heading (to the next heading of its level or
# higher); or the lines that hold a word, with one line around each; from a character on, up to max bytes.
TEXT_JS = r"""(function (o) {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const OUT = 'nav,footer,aside,[role=navigation],[role=contentinfo],[role=complementary],[role=search]';
  const main = o.all ? document.body : document.querySelector('main,[role=main]') || document.querySelector('article')
    || document.body;
  if (!main) return {text: '', total: 0, rest: 0, next: null, heads: []};
  let text = main.innerText || '';
  if (!o.all) for (const x of main.querySelectorAll(OUT)) {
    const up = x.parentElement && x.parentElement.closest(OUT);
    if (up && main.contains(up)) continue;  // its text went with the part around it
    const t = x.innerText; if (t && t.trim()) text = text.replace(t, '');
  }
  text = text.replace(/\n{3,}/g, '\n\n').trim();
  const heads = Array.from(main.querySelectorAll('h1,h2,h3,h4,h5,h6')).filter(h => h.innerText.trim())
    .map(h => [+h.tagName[1], h.innerText.trim().split('\n')[0]]);
  const out = {heads: heads.slice(0, 40).map(h => h[1])};
  if (o.section) {
    const want = norm(o.section), lines = text.split('\n');
    let i = heads.findIndex(h => norm(h[1]) === want);
    if (i < 0) i = heads.findIndex(h => norm(h[1]).startsWith(want));
    if (i < 0) return {error: 'no heading ' + JSON.stringify(o.section) + '; the page has ' +
      heads.slice(0, 15).map(h => JSON.stringify(h[1])).join(', ')};
    const lvl = heads[i][0], stop = heads.slice(i + 1).filter(h => h[0] <= lvl).map(h => norm(h[1]));
    const at = lines.findIndex(l => norm(l) === norm(heads[i][1]));
    if (at < 0) return {error: 'the heading ' + JSON.stringify(heads[i][1]) + ' is not in the text of the page'};
    let end = lines.length;
    for (let k = at + 1; k < lines.length; k++) if (stop.includes(norm(lines[k]))) { end = k; break; }
    text = lines.slice(at, end).join('\n').trim();
    out.section = heads[i][1];
  }
  if (o.grep) {
    const w = norm(o.grep), lines = text.split('\n').filter(l => l.trim()), keep = new Set();
    lines.forEach((l, k) => { if (norm(l).includes(w)) [k - 1, k, k + 1].forEach(j => j >= 0 && j < lines.length && keep.add(j)); });
    const ks = [...keep].sort((a, b) => a - b), parts = [];
    ks.forEach((k, n) => { if (n && k !== ks[n - 1] + 1) parts.push('--'); parts.push(lines[k]); });
    text = parts.join('\n');
    out.matches = lines.filter(l => norm(l).includes(w)).length;
  }
  const enc = new TextEncoder(), from = Math.max(0, o.from || 0);
  const shown = text.slice(from);
  let cutAt = shown.length;
  if (enc.encode(shown).length > o.max) {  // cut at the end of a line within max bytes
    let lo = 0, hi = shown.length;
    while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (enc.encode(shown.slice(0, mid)).length <= o.max) lo = mid; else hi = mid - 1; }
    const nl = shown.lastIndexOf('\n', lo);
    cutAt = nl > lo / 2 ? nl : lo;
  }
  out.text = shown.slice(0, cutAt).trimEnd();
  out.total = text.length;
  out.next = from + cutAt < text.length ? from + cutAt : null;
  out.rest = out.next === null ? 0 : enc.encode(text.slice(out.next)).length;
  return out;
})"""


def page_text(b, section=None, grep=None, start=0, limit=TEXT_MAX, everything=False):
    """#5672: the page's text with limits (TEXT_JS); rest: the bytes after what it gives, next: where --from goes on."""
    loading(b)
    r = in_world(b, "(%s)(%s)" % (TEXT_JS, json.dumps({"section": section or None, "grep": grep or None,
                                                       "from": int(start or 0), "max": int(limit or TEXT_MAX),
                                                       "all": bool(everything)})))
    if r is NO_WORLD or not isinstance(r, dict):
        raise RiverError("the page did not answer: run maxpm browser text again")
    if r.get("error"):
        raise RiverError(r["error"])
    return r


def resolve_target(b, target, within=None, verb="click"):
    """#5667: the element that an action names, as a ref (@e3) or a CSS selector (css:<selector>). target: a ref, a
    name as a snapshot prints it ("Comments"), a role and a name (link "Comments"), or css:<selector>; within: the
    text of its row, card or region. One element with that name: its ref. Several: an error that lists them, and
    nothing happens. None: a CSS selector that matches (as before names), else an error with the nearest names."""
    t = (target or "").strip()
    if not t or as_ref(t) or t.startswith("css:"):
        if within and t:
            raise RiverError("--in takes a name, not a ref or a CSS selector: maxpm browser click \"<name>\" --in \"<row>\"")
        return t or None
    m = re.fullmatch(r'([a-z]+)\s+"(.*)"', t)
    role, name = (m.group(1), m.group(2)) if m and m.group(1) in ROLES else (None, t)
    loading(b)
    r = in_world(b, "(%s)(null, false, %d, false, 0, %s)" % (SNAP_JS, b.refs, json.dumps(
        {"target": {"name": name, "role": role, "within": within}})))
    if r is NO_WORLD or not isinstance(r, dict):
        raise RiverError("the page did not answer: run the command again")
    b.refs = max(b.refs, r.get("next") or 0)
    what = (f'{role} "{name}"' if role else f'"{name}"') + (f' in "{within}"' if within else "")
    if r["count"] == 1:
        return "@" + r["hits"][0]["ref"]
    if r["count"] > 1:
        more = f"\n  ... {r['count'] - len(r['hits'])} more" if r["count"] > len(r["hits"]) else ""
        raise RiverError(f"{r['count']} elements are {what}; nothing was done. Name one: maxpm browser {verb} @e<n>, "
                         f"or add --in \"<the text of its row or card>\":\n  " + "\n  ".join(h["line"] for h in r["hits"])
                         + more)
    if not role and not within:
        try:  # a CSS selector, as maxpm browser took before names (#5663)
            if b.evaluate("(() => { try { return !!document.querySelector(%s); } catch (e) { return false; } })()"
                          % json.dumps(t)):
                return "css:" + t
        except DialogOpen:
            raise
        except RiverError:
            pass
    near = ("; the nearest: " + ", ".join(r.get("near") or [])) if r.get("near") else ""
    raise RiverError(f"no element is {what}{near}. Else: maxpm browser html --scope <ref or css> for that part of the "
                     f"page, then --selector '<css>' or a point (x y)")


def on_element(b, target, fn, *args):
    """fn(el, ...args) in the page, for the element that target names: a ref of a snapshot (@e3, in the snapshot's
    world) or a CSS selector (the first element that matches, in the page's world; css:<selector> too)."""
    rest = "".join(", " + json.dumps(x) for x in args)
    if target and target.startswith("css:"):
        target = target[4:]
    ref = as_ref(target)
    loading(b)
    if ref:
        find ="((w) => w && w.deref())(window.__maxpm && window.__maxpm.els.get(%s))" % json.dumps(ref)
    else:
        find = "document.querySelector(%s)" % json.dumps(target)
    expr = "((el) => el && el.isConnected ? {v: (%s)(el%s)} : {gone: true})(%s)" % (fn, rest, find)
    r = in_world(b, expr, create=False) if ref else b.evaluate(expr)
    if r is NO_WORLD:
        raise RiverError(f"@{ref} is from an earlier page: run maxpm browser snapshot")
    if not r or r.get("gone"):
        if ref:
            raise RiverError(f"@{ref} is no longer on the page: run maxpm browser snapshot")
        raise RiverError(f"no element matches {target!r} in {b.info()['url']}")
    return r.get("v")


# The point to click, and (#5717) the address of the element's link when it is another address of the same site in the
# same tab: the page's script may follow it as a client route, and the click then waits for that.
POINT_JS = ("(el) => { el.scrollIntoView({block: 'center', inline: 'center'}); const r = el.getBoundingClientRect(); "
            "const a = el.closest('a[href]'); let link = null; "
            "if (a && (!a.target || a.target === '_self')) { const u = new URL(a.href, location.href), "
            "h = new URL(location.href); u.hash = h.hash = ''; "
            "if (u.origin === h.origin && u.href !== h.href) link = u.href; } "
            "return [r.left + r.width / 2, r.top + r.height / 2, link]; }")
# Focus a field and select what it holds, so the text that comes next replaces it. What kind of field it is: text
# (Input.insertText types it), value (a date, a color: the value is set), select, or no:<what> (fill refuses).
FOCUS_JS = r"""(el) => { el.scrollIntoView({block: 'center', inline: 'center'}); el.focus();
  const t = el.tagName, ty = (el.type || '').toLowerCase();
  if (t === 'SELECT') return 'select';
  if (el.isContentEditable) { const r = document.createRange(); r.selectNodeContents(el);
    const s = getSelection(); s.removeAllRanges(); s.addRange(r); return 'text'; }
  if (t === 'TEXTAREA') { el.select(); return 'text'; }
  if (t !== 'INPUT') return 'no:' + (el.getAttribute('role') || t.toLowerCase());
  if (['checkbox', 'radio', 'submit', 'button', 'reset', 'image', 'file'].includes(ty)) return 'no:' + ty;
  if (['date', 'time', 'datetime-local', 'month', 'week', 'color', 'range'].includes(ty)) return 'value';
  el.select(); return 'text'; }"""
# The value of a field set at once, with the events a page listens to; the input's own setter, so a framework that
# watches the value (React) sees the change.
SET_JS = r"""(el, v) => { const p = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  if (el.isContentEditable) el.textContent = v; else Object.getOwnPropertyDescriptor(p, 'value').set.call(el, v);
  el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true}));
  return true; }"""
SELECT_JS = r"""(el, vals) => { if (el.tagName !== 'SELECT') return {err: 'not a select (a ' + el.tagName.toLowerCase() +
    '): click it, then click the option'};
  const opts = Array.from(el.options), pick = [], low = s => s.trim().toLowerCase();
  for (const v of vals) {
    const o = opts.find(o => o.value === v) || opts.find(o => low(o.label) === low(v));
    if (!o) return {err: 'no option ' + JSON.stringify(v) + '; it has ' + opts.slice(0, 15).map(o => JSON.stringify(o.label.trim())).join(', ')};
    pick.push(o);
  }
  if (pick.length > 1 && !el.multiple) return {err: 'this select takes one option'};
  el.focus(); for (const o of opts) o.selected = pick.includes(o);
  el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true}));
  return {picked: pick.map(o => o.label.trim())}; }"""
CHECKED_JS = ("(el) => el.type === 'checkbox' || el.type === 'radio' ? el.checked : "
              "el.hasAttribute('aria-checked') ? el.getAttribute('aria-checked') === 'true' : null")


def point_of(b, target):
    pos = on_element(b, target, POINT_JS)
    if not pos:
        raise RiverError(f"{target} has no place on the page to click")
    return pos


# ---- what a session does (maxpm browser ...)

def click(b, selector=None, x=None, y=None, who=None, wait=None):
    """Click an element (a ref of a snapshot, or a CSS selector) or a point. While someone watches, the pointer glides
    there first; then the action waits as after() says."""
    link = None
    if selector:
        x, y, *rest = point_of(b, selector)
        link = rest[0] if rest else None
    if x is None or y is None:
        raise RiverError("name what to click: maxpm browser click @e3 (a ref of maxpm browser snapshot), "
                         "--selector '<css>', or x and y")
    who = driver(b, who)
    glide(b, who, x, y)
    mark = {**nav_mark(b), "link": link}
    for t in ("mousePressed", "mouseReleased"):
        b.page("Input.dispatchMouseEvent", {"type": t, "x": float(x), "y": float(y), "button": "left", "clickCount": 1})
        if t == "mousePressed":
            note_pointer(b, who, x, y, press=True)
    return done(b, wait, mark, clicked=selector or [x, y])


def done(b, wait, mark, **out):
    """The answer of an action: the page as it is after the wait. #5717: with no new page, the address and title
    are read from the page (its script may have changed them), not from Chrome's target info."""
    moved = after(b, wait, mark)
    if not moved and not b.dialog:
        b.here()
    return {**b.info(), **out, "navigated": moved}


def type_text(b, text, selector=None, who=None, wait=None):
    """Type text where the focus is (after a click on selector). While someone watches, key by key at a pace a
    person can follow (TYPE_MAX at most), and the panel shows the keys; else all at once."""
    if selector:
        click(b, selector, who=who, wait="none")
    who = driver(b, who)
    hide = secret(b)
    mark = nav_mark(b)
    if not pace(b) or not text:
        b.page("Input.insertText", {"text": text})
        note_keys(b, who, text, secret=hide)
        return done(b, wait, mark, typed=len(text))
    gap = min(KEY_TIME, TYPE_MAX / len(text))
    for ch in text:
        for p in char_events(ch):
            b.page("Input.dispatchKeyEvent", p)
        if ch in "\r\n\t":
            note_keys(b, who, key="Tab" if ch == "\t" else "Enter", secret=hide)
        else:
            note_keys(b, who, ch, secret=hide)
        time.sleep(gap)
    return done(b, wait, mark, typed=len(text))


def fill(b, target, text, who=None, wait=None):
    """Set a field's text at once (#5663): its old text goes. Not key by key, also while someone watches: the panel
    shows the text (dots for a secret field)."""
    kind = on_element(b, target, FOCUS_JS)
    if kind == "select":
        return select_options(b, target, [text], who=who, wait=wait)
    if str(kind).startswith("no:"):
        raise RiverError(f"{target} is a {kind[3:]}, not a text field: "
                         + ("maxpm browser check " + target if kind[3:] in ("checkbox", "radio") else
                            "maxpm browser click " + target))
    hide = secret(b)
    mark = nav_mark(b)
    if kind == "value" or not text:
        on_element(b, target, SET_JS, text)
    else:
        b.page("Input.insertText", {"text": text})
    note_keys(b, driver(b, who), text, secret=hide)
    return done(b, wait, mark, filled=len(text))


def select_options(b, target, values, who=None, wait=None):
    """Pick the options of a <select> by value or label."""
    mark = nav_mark(b)
    r = on_element(b, target, SELECT_JS, list(values))
    if r.get("err"):
        raise RiverError(f"{target}: {r['err']}")
    note_keys(b, driver(b, who), ", ".join(r["picked"]))
    return done(b, wait, mark, picked=r["picked"])


def check(b, target, on=True, who=None, wait=None):
    """Make a checkbox, a radio button or a switch checked (on) or not: a click when it is not so already."""
    now = on_element(b, target, CHECKED_JS)
    if now is None:
        raise RiverError(f"{target} is not a checkbox, a radio button or a switch: maxpm browser click {target}")
    out = {**b.info(), "navigated": False}
    if now != on:
        out = click(b, target, who=who, wait=wait)
        now = on_element(b, target, CHECKED_JS) if not out.get("navigated") else on
        if now != on:
            raise RiverError(f"the click on {target} did not change it: the page keeps it "
                             + ("checked" if now else "not checked"))
    return {**out, "checked": on}


def press(b, key, who=None, wait=None):
    mark = nav_mark(b)
    for p in key_events(key):
        b.page("Input.dispatchKeyEvent", p)
    if len(key) == 1 or key in KEY_SIGNS:
        note_keys(b, driver(b, who), key if len(key) == 1 else "", key=key, secret=secret(b) if len(key) == 1 else None,
                  hidden=lambda: secret(b))
    return done(b, wait, mark)


def history(b, step, wait=None):
    h = b.page("Page.getNavigationHistory")
    i = h["currentIndex"] + step
    if not 0 <= i < len(h["entries"]):
        return {**b.info(), "navigated": False}
    mark = nav_mark(b)
    b.page("Page.navigateToHistoryEntry", {"entryId": h["entries"][i]["id"]})
    return done(b, wait, mark)


def reload(b, wait=None):
    mark = nav_mark(b)
    b.page("Page.reload")
    return done(b, wait or "load", mark)


# The pointer and the last click ring of a screenshot with --cursor (#5616): an element in a closed shadow root at
# the page's coordinates, which the screenshot draws at the page's own size and device scale; it goes right after.
CURSOR_ID = "__maxpm_cursor"
CURSOR_JS = """((x, y, px, py) => {
  const old = document.getElementById(%(id)s); if (old) old.remove();
  const h = document.createElement('div'); h.id = %(id)s; h.setAttribute('aria-hidden', 'true');
  h.style.cssText = 'position:absolute;left:0;top:0;width:0;height:0;overflow:visible;z-index:2147483647;pointer-events:none';
  const s = h.attachShadow({mode: 'closed'}), sx = scrollX, sy = scrollY;
  const ring = px === null ? '' : `<div style="position:absolute;left:${px + sx - 14}px;top:${py + sy - 14}px;width:24px;height:24px;border:2px solid #e8590c;border-radius:50%%;background:rgba(232,89,12,.18)"></div>`;
  s.innerHTML = ring + `<svg style="position:absolute;left:${x + sx - 1}px;top:${y + sy - 1}px" width="20" height="28" viewBox="0 0 20 28"><path d="M1 1 L1 22 L6.5 17 L10 26 L13.5 24.5 L10 16 L17.5 16 Z" fill="#111" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>`;
  document.documentElement.appendChild(h);
  setTimeout(() => h.remove(), 15000);
})""" % {"id": json.dumps(CURSOR_ID)}
CURSOR_OFF = "(() => { const e = document.getElementById(%s); if (e) e.remove(); })()" % json.dumps(CURSOR_ID)


def cursor_on(b):
    """Draw the pointer into the shown tab for a screenshot; False when no session pointed in it."""
    p = b.pointer
    if not p or p["target"] != b.target:
        return False
    args = [p["x"], p["y"], p.get("px"), p.get("py")]
    b.evaluate(CURSOR_JS + "(" + ",".join(json.dumps(a) for a in args) + ")")
    return True


def cursor_off(b):
    try:
        b.evaluate(CURSOR_OFF, 5)
    except RiverError:
        pass  # it goes by itself after 15 s


def screenshot(b, full=False, cursor=False):
    """A PNG of the shown tab; cursor: with the driving session's pointer and its last click ring."""
    p = {"format": "png"}
    scale = 1 / b.dsf if b.dsf > 1 and b.size and not b._windowed() else 1  # one pixel for each CSS pixel (#5625)
    if full:
        p["captureBeyondViewport"] = True
        m = b.page("Page.getLayoutMetrics")
        size = m.get("cssContentSize") or m.get("contentSize")
        p["clip"] = {"x": 0, "y": 0, "width": size["width"], "height": size["height"], "scale": scale}
    elif scale != 1:
        m = b.page("Page.getLayoutMetrics")["cssVisualViewport"]
        p["clip"] = {"x": m["pageX"], "y": m["pageY"], "width": m["clientWidth"], "height": m["clientHeight"],
                     "scale": scale}
    drew = cursor and cursor_on(b)
    try:
        return {**b.info(), "png": b.page("Page.captureScreenshot", p, timeout=60)["data"], "cursor": bool(drew)}
    finally:
        if drew:
            cursor_off(b)


def switch_tab(b, target):
    if target not in b.targets:
        raise RiverError(f"no tab {target} in the browser of {b.project}: maxpm browser status lists them")
    with b.lock:
        b._attach(target)
    _notify(b)
    return b.info()


def new_tab(b, url="about:blank"):
    url = norm_url(url)
    b.start()
    t = b._new_target()
    with b.lock:
        b._attach(t)
    if url != "about:blank":
        return b.open(url)
    return b.info()


def close_tab(b, target=None):
    target = target or b.target
    if target not in b.targets:
        raise RiverError(f"no tab {target} in the browser of {b.project}")
    b.call("Target.closeTarget", {"targetId": target})
    time.sleep(0.3)
    return b.info()


# The fields of a cookie that Storage.setCookies takes (CDP CookieParam); getCookies gives more (size, session).
COOKIE_PARAMS = ("name", "value", "domain", "path", "secure", "httpOnly", "sameSite", "expires", "priority",
                 "sameParty", "sourceScheme", "sourcePort", "partitionKey")


def cookie_params(cookies):
    """Cookies of Storage.getCookies as Storage.setCookies takes them; a session cookie stays one."""
    keep = []
    for c in cookies:
        p = {k: c[k] for k in COOKIE_PARAMS if k in c}
        if c.get("session") or (p.get("expires") or 0) <= 0:
            p.pop("expires", None)  # a cookie for this run of Chrome only: it stays one
        keep.append(p)
    return keep


def save_signins(b):
    """Save sign-ins for the project (#5633): the cookies of a session's browser go into the project's profile, so
    the sessions that start later begin signed in. Chrome writes both profiles itself (Storage.getCookies from the
    session's Chrome, Storage.setCookies into a Chrome on the project's profile); a project Chrome that this started
    closes again, and writes its cookies to disk as it closes. Local storage stays in the session's profile.
    In a Chrome that serve did not start (#5634), the cookies of the session's context go into that Chrome's default
    context, whose profile is the shared one."""
    if not b.owner or (b.attached and not b.context):
        raise RiverError("only the browser of a session saves sign-ins for its project")
    if not b.running():
        raise RiverError("the browser is not running: open a page first")
    if b.attached:
        keep = cookie_params(b.call("Storage.getCookies", {"browserContextId": b.context}).get("cookies") or [])
        if keep:
            b.call("Storage.setCookies", {"cookies": keep})
        return {**b.info(), "saved": len(keep)}
    keep = cookie_params(b.call("Storage.getCookies").get("cookies") or [])
    project = get(b.project)
    started = not project.running()
    try:
        project.start()
        if keep:
            project.call("Storage.setCookies", {"cookies": keep})
    finally:
        if started:
            project.close()
    return {**b.info(), "saved": len(keep)}


# ---- the page walk (#5516): a check opens each page of a list in a tab of its own and records what went wrong

WALK_FRAMES = 12  # the stack frames kept of one error


def _frames(trace):
    """The frames of a CDP stack trace: [{function, url, line, column}], 0-based as CDP gives them."""
    return [{"function": f.get("functionName") or "", "url": f.get("url") or "", "line": f.get("lineNumber", 0),
             "column": f.get("columnNumber", 0)} for f in ((trace or {}).get("callFrames") or [])[:WALK_FRAMES]]


def _arg_text(a):
    if "value" in a:
        return a["value"] if isinstance(a["value"], str) else json.dumps(a["value"])
    return a.get("description") or a.get("type") or ""


def walk(b, pages, settle=3.0, timeout=60.0):
    """Open each address of pages in a background tab of the project's browser b (not the tab the person sees) and
    record, for each page: the uncaught exceptions, console.error calls and error log entries with their stack
    frames (minified as the browser ran them), the requests that failed (an HTTP status of 400 or more, or no
    answer), and the SHA-256 of each script that a frame names, so the caller can check that a source map is the
    build of that script. settle: seconds after the load event, for the errors that come late. Pages that do not fit
    in timeout seconds are marked skipped. The tab closes at the end."""
    b.start()
    end = time.monotonic() + timeout
    target = b.call("Target.createTarget", {"url": "about:blank", "background": True})["targetId"]
    if b.attached:
        b.mine.add(target)
    session = b.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
    loaded, now = threading.Event(), {"page": None, "requests": {}, "scripts": {}}

    def on(method, p):
        page = now["page"]
        if method == "Page.loadEventFired":
            loaded.set()
        elif method == "Page.javascriptDialogOpening":  # a box would stop the page and the walk (#5674)
            b.send("Page.handleJavaScriptDialog", {"accept": p.get("type") == "beforeunload"}, session=session)
        elif method == "Debugger.scriptParsed" and p.get("url"):
            now["scripts"][p["url"]] = p["scriptId"]
        elif page is None:
            return
        elif method == "Runtime.exceptionThrown":
            d = p.get("exceptionDetails") or {}
            text = ((d.get("exception") or {}).get("description") or d.get("text") or "an exception").strip()
            frames = _frames(d.get("stackTrace")) or ([{"function": "", "url": d["url"], "line": d.get("lineNumber", 0),
                                                        "column": d.get("columnNumber", 0)}] if d.get("url") else [])
            page["errors"].append({"kind": "exception", "message": text.splitlines()[0][:500], "frames": frames})
        elif method == "Runtime.consoleAPICalled" and p.get("type") in ("error", "assert"):
            text = " ".join(_arg_text(a) for a in p.get("args") or []).strip() or p["type"]
            page["errors"].append({"kind": "console", "message": text.splitlines()[0][:500],
                                   "frames": _frames(p.get("stackTrace"))})
        elif method == "Log.entryAdded":
            e = p.get("entry") or {}
            if e.get("level") == "error" and e.get("source") not in ("network", "console-api"):
                page["errors"].append({"kind": "log", "message": (e.get("text") or "").splitlines()[0][:500],
                                       "frames": _frames(e.get("stackTrace")) or (
                                           [{"function": "", "url": e["url"], "line": e.get("lineNumber", 0), "column": 0}]
                                           if e.get("url") else [])})
        elif method == "Network.requestWillBeSent":
            r = p.get("request") or {}
            now["requests"][p["requestId"]] = {"url": r.get("url", ""), "method": r.get("method", "GET"),
                                               "type": p.get("type") or ""}
        elif method == "Network.responseReceived":
            r = p.get("response") or {}
            if (r.get("status") or 0) >= 400:
                q = now["requests"].get(p["requestId"]) or {"url": r.get("url", ""), "method": "GET"}
                page["requests"].append({**q, "type": p.get("type") or q.get("type", ""), "status": r["status"]})
        elif method == "Network.loadingFailed" and not p.get("canceled"):
            q = now["requests"].get(p["requestId"]) or {"url": "", "method": "GET"}
            page["requests"].append({**q, "type": p.get("type") or q.get("type", ""),
                                     "error": p.get("blockedReason") or p.get("errorText") or "failed"})

    b.listeners[session] = on
    out = []
    try:
        for m in ("Page.enable", "Runtime.enable", "Log.enable", "Network.enable", "Debugger.enable"):
            b.call(m, session=session)
        b.call("Debugger.setSkipAllPauses", {"skip": True}, session=session)  # a debugger statement must not stop it
        for url in pages:
            left = end - time.monotonic()
            if left <= 1:
                out.append({"url": url, "skipped": True})
                continue
            page = {"url": url, "errors": [], "requests": [], "scripts": {}, "load": None}
            now.update(page=page, requests={}, scripts={})
            loaded.clear()
            try:
                r = b.call("Page.navigate", {"url": url}, session=session, timeout=min(CALL_WAIT, left))
                if r.get("errorText"):
                    page["load"] = r["errorText"]
                elif not loaded.wait(min(LOAD_WAIT, max(0.0, end - time.monotonic()))):
                    page["load"] = f"no load event within {min(LOAD_WAIT, int(left))}s"
                time.sleep(max(0.0, min(settle, end - time.monotonic())))
            except RiverError as e:
                page["load"] = str(e)
            now["page"] = None
            for u in dict.fromkeys(f["url"] for e in page["errors"] for f in e["frames"] if f["url"]):
                sid = now["scripts"].get(u)
                if not sid:
                    continue
                try:
                    src = b.call("Debugger.getScriptSource", {"scriptId": sid}, session=session)["scriptSource"]
                    page["scripts"][u] = hashlib.sha256(src.encode()).hexdigest()
                except (RiverError, KeyError):
                    pass
            out.append(page)
    finally:
        b.listeners.pop(session, None)
        try:
            b.call("Target.closeTarget", {"targetId": target}, timeout=5)
        except RiverError:
            pass
    return {"project": b.project, "pages": out}

