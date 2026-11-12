"""The browser of a project (#4631, design #4641): a Google Chrome that maxpm serve starts for each project, which
the person sees and uses in the side panel of the Sessions tab, and which a session drives with maxpm browser.

- One browser for each project (decision 2), with its own profile under <queue folder>/browsers/<project>/, never
  the person's own Chrome profile. Sign-ins stay in that profile; only the person signs in, in the panel.
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
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from . import core
from .core import RiverError

MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
START_WAIT = 20      # seconds for Chrome to write its DevToolsActivePort
CALL_WAIT = 20       # seconds for one CDP answer
LOAD_WAIT = 15       # seconds that open() waits for the page's load
VIEW = (1280, 860)   # the size of the page before the panel says its own


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


def browser_dir(project):
    """The folder of a project's browser: its profile, its downloads, and state.json. Beside the queue's database,
    so a test queue in a temporary folder has its browsers there too."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", project).strip("._") or "project"
    return core.db_path().parent / "browsers" / safe


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
    def __init__(self, project):
        self.project = project
        self.dir = browser_dir(project)
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
        self.attached = None # the debugging port of a Chrome that serve did not start (browser_source cdp:<port>)
        self.mine = set()    # attached: the tabs serve opened in it, and the tabs that they opened
        self.listeners = {}  # CDP session -> fn(method, params): the events of a tab that a page walk opened
        self.state = self._load()

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
        if window:
            raise RiverError(f"the browser of {self.project} is a Chrome that runs already (browser_source cdp:{port}): "
                             f"it has its own window, so it does not open as one")
        with self.lock:
            if self.ws is not None and self.attached == port:
                return
            if self.ws is not None:
                self._stop_chrome()
            try:
                self._version(port)
            except (OSError, ValueError):
                raise RiverError(f"no Chrome answers on 127.0.0.1:{port} (browser_source cdp:{port} of {self.project}): "
                                 f"start that browser first, as the project's own steps say")
            self.attached = self.port = port
            self._connect()

    def _new_target(self, url="about:blank"):
        """A new tab; attached, it is one of serve's own tabs."""
        t = self.call("Target.createTarget", {"url": url})["targetId"]
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
            raise RiverError("no Google Chrome on this computer: install it, or set MAXPM_CHROME to the path of Chrome or "
                             "Chromium, then run the command again")
        profile = self.dir / "profile"
        (self.dir / "downloads").mkdir(parents=True, exist_ok=True)
        profile.mkdir(parents=True, exist_ok=True)
        try:
            (profile / "DevToolsActivePort").unlink()
        except OSError:
            pass
        w, h = self.size or VIEW
        args = [chrome, f"--user-data-dir={profile}", "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
                "--no-first-run", "--no-default-browser-check", "--disable-default-apps", "--disable-sync",
                f"--proxy-server=http://127.0.0.1:{self.proxy.port}", "--proxy-bypass-list=<-loopback>",
                f"--window-size={w},{h}", "about:blank"]
        if not self.state.get("window"):
            args[1:1] = ["--headless=new", "--hide-scrollbars", "--mute-audio"]
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
        self._attach(pages[0]["targetId"] if pages else self._new_target())

    def _attach(self, target):
        old = self.session
        self.session = self.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        self.target = target
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
        self.call("Page.startScreencast", {"format": "jpeg", "quality": 70, "everyNthFrame": 1}, session=s)
        try:
            self.call("Target.activateTarget", {"targetId": target})
        except RiverError:
            pass

    def _fit(self):
        """The page takes the size of the panel (headless only; a window has its own size)."""
        if not self.size or self.state.get("window") or not self.session:
            return
        w, h = self.size
        self.call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": False},
                  session=self.session)

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
                self.proc.kill()
                self.proc.wait(5)
            return
        end = time.time() + 5
        while time.time() < end and core.pid_alive(pid):
            time.sleep(0.1)

    def _stop_chrome(self):
        ws, self.ws = self.ws, None
        if self.attached:  # a Chrome that serve did not start: only leave it
            if ws:
                ws.close()
            self.attached = None
            self.session = self.target = None
            self.targets, self.mine = {}, set()
            return
        if ws:
            try:
                ws.send(json.dumps({"id": 0, "method": "Browser.close"}))
            except OSError:
                pass
            ws.close()
        pid = self.state.get("pid")
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
            self.session = self.target = None
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
            if session == self.session:
                m = p.get("metadata") or {}
                with self.cond:
                    self.seq += 1
                    self.frame = {"seq": self.seq, "data": p["data"], "w": m.get("deviceWidth"), "h": m.get("deviceHeight")}
                    self.cond.notify_all()
            self.send("Page.screencastFrameAck", {"sessionId": p["sessionId"]}, session=session)
        elif method in ("Target.targetCreated", "Target.targetInfoChanged"):
            t = p["targetInfo"]
            if t["type"] != "page":
                return
            if self.attached:  # attached: only serve's own tabs and the tabs they open
                if t["targetId"] not in self.mine and t.get("openerId") not in self.mine:
                    return
                self.mine.add(t["targetId"])
            new = t["targetId"] not in self.targets
            self.targets[t["targetId"]] = {"url": t["url"], "title": t["title"], "opener": t.get("openerId")}
            if new and t.get("openerId") and t["openerId"] == self.target:  # a page opened a new tab: show it
                threading.Thread(target=self._switch_quiet, args=(t["targetId"],), daemon=True).start()
            with self.cond:
                self.seq += 1
                self.cond.notify_all()
        elif method == "Target.targetDestroyed":
            self.targets.pop(p["targetId"], None)
            self.mine.discard(p["targetId"])
            if p["targetId"] == self.target:
                self.session = self.target = None
                threading.Thread(target=self._switch_quiet, args=(None,), daemon=True).start()
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
        w = self.wait[i] = [threading.Event(), None]
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
        if "error" in w[1]:
            raise RiverError(f"the browser refused {method}: {w[1]['error'].get('message')}")
        return w[1].get("result") or {}

    def page(self, method, params=None, timeout=CALL_WAIT):
        """A call to the tab that the panel shows."""
        self.start()
        if not self.session:
            self._switch_quiet(None)
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
        return {"project": self.project, "running": self.running(), "window": bool(self.state.get("window")),
                "source": f"cdp:{self.attached}" if self.attached else None,
                "url": cur.get("url", ""), "title": cur.get("title", ""), "controller": c or None,
                "tabs": [{"id": k, "url": v["url"], "title": v["title"], "on": k == self.target}
                         for k, v in self.targets.items()]}

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
            except RiverError:
                pass
            time.sleep(0.2)
        try:  # the title and address as they are now (the target events can come a moment later)
            t = self.call("Target.getTargetInfo", {"targetId": self.target})["targetInfo"]
            self.targets[self.target] = {"url": t["url"], "title": t["title"], "opener": t.get("openerId")}
        except (RiverError, KeyError, TypeError):
            pass

    def resize(self, w, h):
        w, h = max(200, min(int(w), 3000)), max(150, min(int(h), 3000))
        self.size = (w, h)
        if self.running():
            self._fit()


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
        url = ("http://" if re.match(r"^(localhost|127\.|\[::1\])", url) else "https://") + url
    scheme = urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise RiverError(f"the browser opens http and https addresses only, not {scheme}:")
    return url


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
    return (f"ws://127.0.0.1:{core.setting(conn, 'serve_port')}/cdp/{quote(agent, safe='')}"
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
            may_change(conn, b, agent, False)
        except RiverError as e:
            return str(e)
    return None


def cdp_session(conn, b, agent, rfile, sock, upstream):
    """Relay one session's CDP connection until either side closes it. rfile and sock: the session's WebSocket (the
    server side: frames out are not masked); upstream: a WS to Chrome of this connection only, so the ids and events
    of two sessions never mix. A refused message gets a CDP error from serve and never reaches Chrome."""
    wlock, worlds = threading.Lock(), set()

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


def get(project):
    with _lock:
        b = BROWSERS.get(project)
        if b is None:
            b = BROWSERS[project] = Browser(project)
        return b


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


def _lock_run(conn, b, key, who):
    """Run the project's browser_lock or browser_unlock for who: (ok, output). No command set: (True, None)."""
    r = conn.execute("SELECT id, path FROM projects WHERE name=?", (b.project,)).fetchone()
    cmd = (core.setting(conn, key, project_id=r["id"] if r else None) or "").strip()
    if not cmd:
        return True, None
    cmd = cmd.replace("{who}", shlex.quote(who))
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


def may_change(conn, b, who, person):
    """RiverError when who may not change the page now (navigate, click, type, run a script)."""
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
        raise RiverError(f"the person ({c['who']}) controls the browser of {b.project}; wait, and read "
                         f"maxpm browser status until they give it back")
    if c["who"] != who:
        raise RiverError(f"the browser of {b.project} is controlled by {c['who']}; maxpm browser status")
    _renew(conn, b, c)


def control(conn, b, who, sub=None):
    """The session takes control (sub: the label of the subagent it gives the browser to)."""
    if not who:
        raise RiverError("name the session: maxpm --as <you> browser control")
    c = b.state.get("controller")
    if c and not controller_gone(conn, c) and c["who"] != who:
        if c["kind"] == "person":
            raise RiverError(f"the person ({c['who']}) controls the browser of {b.project}; wait, and read maxpm browser "
                             f"status until they give it back")
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
        b.state["before"] = c
    b.state["controller"] = {"who": who, "kind": "person", "sub": None, "since": core.iso(core.now())}
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


def _notify(b):
    with b.cond:
        b.seq += 1
        b.cond.notify_all()


# ---- the panel's frames and input

def frame(b, after, timeout=8):
    """The newest frame after seq `after` (a long poll); with no new frame within timeout, the status only."""
    end = time.time() + timeout
    with b.cond:
        while b.seq <= after and time.time() < end:
            b.cond.wait(max(0.05, end - time.time()))
        f = b.frame
        seq = b.seq
    out = b.info()
    out["seq"] = seq
    if f and (not after or f["seq"] > after):
        out["frame"] = {"data": f["data"], "w": f["w"], "h": f["h"]}
    return out


def person_input(b, ev):
    """The person's mouse, wheel and keys from the panel, in the page's CSS pixels."""
    kind = ev.get("type")
    if kind == "mouse":
        action = ev.get("action")
        t = {"down": "mousePressed", "up": "mouseReleased", "move": "mouseMoved", "wheel": "mouseWheel"}.get(action)
        if not t:
            raise RiverError(f"unknown mouse action {action!r}")
        p = {"type": t, "x": float(ev.get("x", 0)), "y": float(ev.get("y", 0)), "modifiers": modifiers(ev.get("mods"))}
        if t in ("mousePressed", "mouseReleased"):
            p.update(button=ev.get("button") or "left", clickCount=int(ev.get("clicks") or 1))
        if t == "mouseWheel":
            p.update(deltaX=float(ev.get("dx", 0)), deltaY=float(ev.get("dy", 0)))
        b.page("Input.dispatchMouseEvent", p)
    elif kind == "key":
        for p in key_events(ev.get("key") or "", ev.get("mods")):
            b.page("Input.dispatchKeyEvent", p)
    elif kind == "text":
        b.page("Input.insertText", {"text": str(ev.get("text") or "")})
    else:
        raise RiverError(f"unknown input {kind!r}")
    return {"ok": True}


# ---- what a session does (maxpm browser ...)

def click(b, selector=None, x=None, y=None):
    if selector:
        pos = b.evaluate("(() => { const e = document.querySelector(%s); if (!e) return null; "
                         "e.scrollIntoView({block: 'center', inline: 'center'}); const r = e.getBoundingClientRect(); "
                         "return [r.left + r.width / 2, r.top + r.height / 2]; })()" % json.dumps(selector))
        if not pos:
            raise RiverError(f"no element matches {selector!r} in {b.info()['url']}")
        x, y = pos
    if x is None or y is None:
        raise RiverError("name what to click: --selector '<css>', or x and y")
    for t in ("mouseMoved", "mousePressed", "mouseReleased"):
        p = {"type": t, "x": float(x), "y": float(y)}
        if t != "mouseMoved":
            p.update(button="left", clickCount=1)
        b.page("Input.dispatchMouseEvent", p)
    time.sleep(0.3)
    b.settle(5)
    return {**b.info(), "clicked": selector or [x, y]}


def type_text(b, text, selector=None):
    if selector:
        click(b, selector)
    b.page("Input.insertText", {"text": text})
    return {**b.info(), "typed": len(text)}


def press(b, key):
    for p in key_events(key):
        b.page("Input.dispatchKeyEvent", p)
    time.sleep(0.2)
    b.settle(5)
    return b.info()


def history(b, step):
    h = b.page("Page.getNavigationHistory")
    i = h["currentIndex"] + step
    if 0 <= i < len(h["entries"]):
        b.page("Page.navigateToHistoryEntry", {"entryId": h["entries"][i]["id"]})
        time.sleep(0.2)
        b.settle()
    return b.info()


def reload(b):
    b.page("Page.reload")
    time.sleep(0.2)
    b.settle()
    return b.info()


def screenshot(b, full=False):
    p = {"format": "png"}
    if full:
        p["captureBeyondViewport"] = True
        m = b.page("Page.getLayoutMetrics")
        size = m.get("cssContentSize") or m.get("contentSize")
        p["clip"] = {"x": 0, "y": 0, "width": size["width"], "height": size["height"], "scale": 1}
    return {**b.info(), "png": b.page("Page.captureScreenshot", p, timeout=60)["data"]}


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

