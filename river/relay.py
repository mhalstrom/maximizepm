"""The MaximizePM relay: Claude and ChatGPT connectors reach this computer's MaximizePM through it.

`maxpm connect` signs this computer in to the relay once (a device flow: the person approves a code in the
browser) and keeps the river token in relay.json next to the database (mode 0600, never in the settings
table). While that file exists, `maxpm serve` keeps one outbound WebSocket to the relay open, and serves each
request that comes over it with mcp.http_post and mcp.http_delete: the same code as the local /mcp. Events
that a relay session writes carry the actor <agent>@relay. Standard library only.

The relay also shows the person this computer's MaximizePM page (https://relay.maximizepm.com/app/): each
page request comes over the same socket (req kind "http"), and river passes it to its own maxpm serve on
127.0.0.1, with a header that marks it as relayed, so the page and its actions are the same as here.

Protocol 1 (frames are JSON text):
  river -> relay  hello {v, river, host, tools, instructions, replace}
  relay -> river  welcome {v, account, limits}   bye {reason}
  relay -> river  req {id, session, client, method, body}            (MCP)   -> res {id, status, session, body}
  relay -> river  req {id, kind: "http", method, path, headers, body_b64}  -> res {id, status, headers, body_b64}
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import os
import random
import secrets
import socket
import ssl
import struct
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from . import __version__, core
from .core import RiverError

DEFAULT_URL = "https://relay.maximizepm.com"
PROTOCOL = 1
PING_EVERY = 30  # seconds between WebSocket pings; the relay closes a socket silent for 90 s
SILENT_LIMIT = 90  # no frame and no pong from the relay for this long: the connection is dead
BACKOFF_MAX = 60
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

MAX_PAGE = 4 * 1024 * 1024  # the largest page answer river sends (the relay takes 8 MB frames)
PAGE_REQUEST_HEADERS = ("content-type", "accept", "if-none-match", "if-modified-since")
PAGE_ANSWER_HEADERS = ("content-type", "cache-control", "location", "content-disposition", "etag", "last-modified")
# The header that marks a page request as relayed. Only this process knows the value, so no other program on
# this computer can make maxpm serve record its actions as <name>@relay.
PAGE_SECRET = secrets.token_urlsafe(24)
PAGE_PORT = {"port": None}  # maxpm serve's port, for the relayed page requests

# Test hooks: tests replace these.
OPEN_SOCKET = None  # (url, headers) -> WebSocket
URLOPEN = None  # (request, timeout) -> response


# ---------------------------------------------------------------- files

def config_path() -> str:
    """relay.json lives next to the database, so a test database (MAXPM_DB) has its own."""
    return os.path.join(os.path.dirname(os.path.abspath(str(core.db_path()))), "relay.json")


def state_path() -> str:
    return os.path.join(os.path.dirname(config_path()), "relay-state.json")


def load() -> dict | None:
    try:
        with open(config_path()) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return None
    return cfg if isinstance(cfg, dict) and cfg.get("token") else None


def _write_private(path: str, data: dict) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def save(cfg: dict) -> None:
    os.makedirs(os.path.dirname(config_path()), exist_ok=True)
    _write_private(config_path(), cfg)


def relay_url(cfg: dict | None = None) -> str:
    return (os.environ.get("MAXPM_RELAY_URL") or (cfg or {}).get("url") or DEFAULT_URL).rstrip("/")


def host_label() -> str:
    return (socket.gethostname() or "computer").split(".")[0][:100]


# ---------------------------------------------------------------- sign-in (device flow)

def _post(url: str, data: dict, token: str | None = None, timeout: float = 15) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=json.dumps(data).encode(), method="POST",
                                 headers={"content-type": "application/json", "user-agent": f"maxpm/{__version__}"})
    if token:
        req.add_header("authorization", f"Bearer {token}")
    opener = URLOPEN or (lambda r, t: urllib.request.urlopen(r, timeout=t))
    try:
        with opener(req, timeout) as res:
            return res.status, json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
        except ValueError:
            body = {}
        finally:
            e.close()
        return e.code, body
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise RiverError(f"cannot reach the relay at {url.split('/river')[0]}: {getattr(e, 'reason', e)}")


def start_device(url: str | None = None, host: str | None = None) -> dict:
    """Step 1: the relay gives a code for the person to approve. Returns the relay's answer and the url."""
    url = (url or relay_url(load())).rstrip("/")
    status, body = _post(f"{url}/river/device", {"host": host or host_label(), "version": __version__})
    if status != 200 or not body.get("device_code"):
        raise RiverError(f"the relay did not start the sign-in (HTTP {status}): {body.get('message', body)}")
    return {**body, "url": url, "host": host or host_label()}


def finish_device(dev: dict, replace=False, sleep=None, clock=None, cancel=None) -> dict:
    """Step 2: poll until the person approves or cancels, then keep the river token in relay.json."""
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    interval = max(1, int(dev.get("interval") or 5))
    ends = clock() + int(dev.get("expires_in") or 600)
    while clock() < ends:
        sleep(interval)
        if cancel is not None and cancel.is_set():
            raise RiverError("the connection was cancelled")
        status, body = _post(f"{dev['url']}/river/device/token", {"device_code": dev["device_code"]})
        if status == 200 and body.get("river_token"):
            cfg = {"url": dev["url"], "token": body["river_token"], "account": body.get("account"),
                   "host": dev["host"], "connected_at": core.iso(core.now())}
            if replace:
                cfg["replace"] = True
            save(cfg)
            return cfg
        error = body.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error == "access_denied":
            raise RiverError("the connection was cancelled on the relay page")
        if error == "expired_token":
            break
        raise RiverError(f"the relay refused the sign-in (HTTP {status}): {error or body}")
    raise RiverError("the code expired before it was approved; run maxpm connect again")


def disconnect() -> dict:
    """maxpm connect --off: revoke the river token at the relay and delete relay.json."""
    cfg = load()
    if not cfg:
        return {"was_connected": False}
    revoked, error = False, None
    try:
        status, _ = _post(f"{relay_url(cfg)}/river/revoke", {}, token=cfg["token"])
        revoked = status in (200, 401)  # 401: the relay already forgot it
    except RiverError as e:
        error = str(e)
    try:
        os.remove(config_path())
    except FileNotFoundError:
        pass
    return {"was_connected": True, "revoked": revoked, "error": error, "account": cfg.get("account")}


# ---------------------------------------------------------------- WebSocket client (RFC 6455)

class WebSocketError(Exception):
    pass


class Refused(WebSocketError):
    """The relay answered the upgrade with an HTTP status, not 101."""

    def __init__(self, status: int):
        super().__init__(f"the relay answered HTTP {status}")
        self.status = status


class WebSocket:
    """A small client: text frames, client masking, ping, pong, close; no extensions."""

    def __init__(self, sock, buf=b""):
        self.sock = sock
        self.buf = buf
        self.lock = threading.Lock()
        self.last_heard = time.monotonic()
        self._parts: list[bytes] = []

    @classmethod
    def open(cls, url: str, headers: dict, timeout: float = 15, context: ssl.SSLContext | None = None):
        u = urlsplit(url)
        secure = u.scheme == "wss"
        port = u.port or (443 if secure else 80)
        sock = socket.create_connection((u.hostname, port), timeout=timeout)
        try:
            if secure:
                sock = (context or ssl.create_default_context()).wrap_socket(sock, server_hostname=u.hostname)
            key = base64.b64encode(os.urandom(16)).decode()
            lines = [f"GET {u.path or '/'}{'?' + u.query if u.query else ''} HTTP/1.1",
                     f"Host: {u.hostname}{'' if u.port is None else f':{u.port}'}",
                     "Upgrade: websocket", "Connection: Upgrade",
                     f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
            lines += [f"{k}: {v}" for k, v in headers.items()]
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    raise WebSocketError("the relay closed the connection during the upgrade")
                data += chunk
                if len(data) > 65536:
                    raise WebSocketError("the upgrade answer is too long")
            head, rest = data.split(b"\r\n\r\n", 1)
            status_line, *header_lines = head.decode("latin-1").split("\r\n")
            parts = status_line.split(" ", 2)
            status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            if status != 101:
                raise Refused(status)
            got = {k.strip().lower(): v.strip() for k, v in (h.split(":", 1) for h in header_lines if ":" in h)}
            want = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
            if got.get("sec-websocket-accept") != want:
                raise WebSocketError("the relay's upgrade answer has a wrong Sec-WebSocket-Accept")
            return cls(sock, rest)
        except BaseException:
            sock.close()
            raise

    def _send(self, opcode: int, payload: bytes = b"") -> None:
        head = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack("!Q", n)
        mask = os.urandom(4)
        with self.lock:
            self.sock.sendall(head + mask + _mask(payload, mask))

    def send_text(self, text: str) -> None:
        self._send(0x1, text.encode())

    def ping(self) -> None:
        self._send(0x9, b"maxpm")

    def close(self, code: int = 1000, reason: str = "") -> None:
        try:
            self._send(0x8, struct.pack("!H", code) + reason.encode()[:120])
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def _frame(self):
        """One whole frame from the buffer, or None. A partial frame stays in the buffer."""
        b = self.buf
        if len(b) < 2:
            return None
        fin, opcode, masked, n = b[0] & 0x80, b[0] & 0x0F, b[1] & 0x80, b[1] & 0x7F
        at = 2
        if n == 126:
            if len(b) < 4:
                return None
            n, at = struct.unpack("!H", b[2:4])[0], 4
        elif n == 127:
            if len(b) < 10:
                return None
            n, at = struct.unpack("!Q", b[2:10])[0], 10
        mask = b""
        if masked:
            if len(b) < at + 4:
                return None
            mask, at = b[at:at + 4], at + 4
        if len(b) < at + n:
            return None
        payload = b[at:at + n]
        self.buf = b[at + n:]
        if mask:
            payload = _mask(payload, mask)
        return bool(fin), opcode, payload

    def recv(self):
        """The next message: ("text", str) or ("close", (code, reason)). socket.timeout leaves the buffer whole."""
        while True:
            frame = self._frame()
            if frame is None:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise WebSocketError("the relay closed the connection")
                self.buf += chunk
                self.last_heard = time.monotonic()
                continue
            fin, opcode, payload = frame
            if opcode == 0x9:  # ping
                self._send(0xA, payload)
            elif opcode == 0xA:  # pong
                pass
            elif opcode == 0x8:
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1005
                return "close", (code, payload[2:].decode("utf-8", "replace"))
            elif opcode in (0x1, 0x2, 0x0):
                self._parts.append(payload)
                if fin:
                    data, self._parts = b"".join(self._parts), []
                    return "text", data.decode("utf-8", "replace")
            else:
                raise WebSocketError(f"unknown frame opcode {opcode}")


def _mask(payload: bytes, mask: bytes) -> bytes:
    n = len(payload)
    key = int.from_bytes((mask * (n // 4 + 1))[:n], "big")
    return (int.from_bytes(payload, "big") ^ key).to_bytes(n, "big") if n else b""


# ---------------------------------------------------------------- the connection (maxpm serve)

class Bye(Exception):
    pass


STATE: dict = {"state": "off"}
_STATE_LOCK = threading.Lock()


def _set_state(**fields) -> None:
    """Changes the fields given (None removes one) and writes relay-state.json when something changed."""
    with _STATE_LOCK:
        new = {k: v for k, v in {**STATE, **fields}.items() if v is not None and k != "at"}
        if new == {k: v for k, v in STATE.items() if k != "at"}:
            return
        new["at"] = core.iso(core.now())
        STATE.clear()
        STATE.update(new)
        try:
            _write_private(state_path(), {**STATE, "pid": os.getpid()})
        except OSError:
            pass


def status() -> dict:
    """The relay connection as maxpm serve sees it now (for the page)."""
    with _STATE_LOCK:
        st = dict(STATE)
    cfg = load()
    st["configured"] = bool(cfg)
    if cfg:
        st.setdefault("account", cfg.get("account"))
        st.setdefault("host", cfg.get("host"))
        st.setdefault("url", relay_url(cfg))
    return st


def saved_status() -> dict:
    """The relay connection as the last maxpm serve wrote it (for maxpm status and maxpm connect --status)."""
    cfg = load()
    out = {"configured": bool(cfg), "account": (cfg or {}).get("account"), "url": relay_url(cfg),
           "host": (cfg or {}).get("host"), "state": "off"}
    try:
        with open(state_path()) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    if cfg and st:
        alive = core.pid_alive(st.get("pid")) if st.get("pid") else False
        out["state"] = st.get("state", "off") if alive else "serve not running"
        out["error"] = st.get("error") if alive else None
        out["since"] = st.get("at")
    elif cfg:
        out["state"] = "serve not running"
    return out


class Link:
    """Keeps the socket to the relay open while relay.json exists. One per maxpm serve."""

    def __init__(self, stop: threading.Event, sleep=None):
        self.stop = stop
        self.wait = sleep or stop.wait
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="maxpm-relay-call")
        self.refused_token = None  # a token the relay refused: wait for a new maxpm connect

    def run(self) -> None:
        _set_state(state="starting")  # relay-state.json now names this maxpm serve (its pid)
        backoff = 1.0
        while not self.stop.is_set():
            pause = None  # seconds to wait before the next try; None: the backoff
            cfg = load()
            if not cfg:
                _set_state(state="off", error=None)
                self.wait(5)
                continue
            if cfg["token"] == self.refused_token:
                self.wait(5)
                continue
            try:
                self.session(cfg)
                backoff = 1.0
            except Refused as e:
                if e.status == 401:
                    self.refused_token = cfg["token"]
                    _set_state(state="error", error="the relay refused this computer's token (disconnected on the "
                                                    "account page?); run maxpm connect")
                    continue
                _set_state(state="waiting", error=str(e))
            except Bye as e:
                reason = str(e)
                if reason in ("revoked", "account deleted"):
                    self.refused_token = cfg["token"]
                    _set_state(state="error", error=f"the relay ended the connection: {reason}; run maxpm connect")
                    continue
                _set_state(state="waiting", error=f"the relay ended the connection: {reason}")
                if reason.startswith("already connected"):
                    pause = BACKOFF_MAX  # another computer holds the account; look again each minute
                elif reason.startswith("protocol"):
                    pause = 10 * BACKOFF_MAX  # this MaximizePM is too old for the relay: update it
            except (OSError, WebSocketError, ValueError) as e:
                _set_state(state="waiting", error=str(e) or e.__class__.__name__)
            if self.stop.is_set():
                break
            self._pause(pause if pause is not None else backoff * (0.5 + random.random() / 2))
            backoff = min(backoff * 2, BACKOFF_MAX)
        _set_state(state="off")

    def _pause(self, seconds: float) -> None:
        """Waits, but returns at once when the computer wakes from sleep (the wall clock jumps ahead)."""
        wall, mono = time.time(), time.monotonic()
        end = mono + seconds
        while not self.stop.is_set() and time.monotonic() < end:
            self.wait(min(1.0, end - time.monotonic()))
            if (time.time() - wall) - (time.monotonic() - mono) > 5:
                return

    def session(self, cfg: dict) -> None:
        base = relay_url(cfg)
        ws_url = ("wss://" if base.startswith("https://") else "ws://") + base.split("://", 1)[1] + "/river/socket"
        _set_state(state="connecting", url=base, host=cfg.get("host"), account=cfg.get("account"))
        opener = OPEN_SOCKET or WebSocket.open
        ws = opener(ws_url, {"Authorization": f"Bearer {cfg['token']}", "User-Agent": f"maxpm/{__version__}"})
        try:
            from . import mcp
            ws.send_text(json.dumps({"t": "hello", "v": PROTOCOL, "river": __version__, "host": cfg.get("host"),
                                     "tools": mcp.TOOLS, "instructions": mcp.CHAT_INSTRUCTIONS,
                                     "replace": bool(cfg.get("replace"))}))
            ws.sock.settimeout(15)
            kind, data = ws.recv()
            if kind == "close":
                raise WebSocketError(f"the relay closed the connection ({data[0]})")
            frame = json.loads(data)
            if frame.get("t") == "bye":
                raise Bye(frame.get("reason") or "no reason")
            if frame.get("t") != "welcome":
                raise WebSocketError(f"the relay sent {frame.get('t')!r} before welcome")
            if cfg.get("replace"):
                cfg = {k: v for k, v in cfg.items() if k != "replace"}  # replace is for one connection
                save(cfg)
            _set_state(state="connected", account=frame.get("account") or cfg.get("account"), error=None)
            self.loop(ws, cfg)
        finally:
            ws.close()

    def loop(self, ws: WebSocket, cfg: dict) -> None:
        ws.sock.settimeout(1.0)
        last_ping = time.monotonic()
        wall, mono = time.time(), time.monotonic()
        while not self.stop.is_set():
            now_wall, now_mono = time.time(), time.monotonic()
            if (now_wall - wall) - (now_mono - mono) > 30:
                raise WebSocketError("the computer slept; connecting again")
            wall, mono = now_wall, now_mono
            current = load()
            if not current or current["token"] != cfg["token"]:
                return  # maxpm connect --off, or a new maxpm connect: start over
            if now_mono - last_ping >= PING_EVERY:
                ws.ping()
                last_ping = now_mono
            if now_mono - ws.last_heard > SILENT_LIMIT:
                raise WebSocketError("no answer from the relay for 90 s")
            try:
                kind, data = ws.recv()
            except socket.timeout:
                continue
            if kind == "close":
                raise WebSocketError(f"the relay closed the connection ({data[0]} {data[1]})".strip())
            frame = json.loads(data)
            if frame.get("t") == "req":
                self.pool.submit(self.serve_request, ws, frame)
            elif frame.get("t") == "bye":
                raise Bye(frame.get("reason") or "no reason")

    def serve_request(self, ws: WebSocket, frame: dict) -> None:
        answer = handle(frame)
        try:
            ws.send_text(json.dumps(answer))
        except OSError:
            pass  # the socket is gone; the relay answers the app for it


def page_request(headers) -> bool:
    """Whether a request to maxpm serve came from the relay thread (handle_page)."""
    value = headers.get("X-Maxpm-Relay") or ""
    return bool(value) and hmac.compare_digest(value, PAGE_SECRET)


def handle_page(frame: dict) -> dict:
    """A page request from the relay -> its answer, from this computer's maxpm serve."""
    rid = frame.get("id")

    def fail(status, text):
        return {"t": "res", "id": rid, "status": status, "headers": {"content-type": "text/plain; charset=utf-8"},
                "body_b64": base64.b64encode(text.encode()).decode()}

    path, method = frame.get("path") or "/", frame.get("method") or "GET"
    if not path.startswith("/") or method not in ("GET", "HEAD", "POST"):
        return fail(400, "bad page request")
    if not PAGE_PORT["port"]:
        return fail(502, "maxpm serve has no port")
    headers = {k: v for k, v in (frame.get("headers") or {}).items() if k.lower() in PAGE_REQUEST_HEADERS}
    headers["X-Maxpm-Relay"] = PAGE_SECRET
    body = base64.b64decode(frame.get("body_b64") or "")
    conn = http.client.HTTPConnection("127.0.0.1", PAGE_PORT["port"], timeout=55)
    try:
        conn.request(method, path, body=body or None, headers=headers)
        res = conn.getresponse()
        data = res.read(MAX_PAGE + 1)
        if len(data) > MAX_PAGE:
            return fail(502, "the answer is larger than the relay takes")
        return {"t": "res", "id": rid, "status": res.status,
                "headers": {k.lower(): v for k, v in res.getheaders() if k.lower() in PAGE_ANSWER_HEADERS},
                "body_b64": base64.b64encode(data).decode()}
    except OSError as e:
        return fail(502, f"maxpm serve did not answer: {e}")
    finally:
        conn.close()


def handle(frame: dict) -> dict:
    """One req frame -> its res frame: a page request, or MCP with the code of the local /mcp."""
    from . import mcp
    if frame.get("kind") == "http":
        return handle_page(frame)
    rid, session = frame.get("id"), frame.get("session")
    try:
        if frame.get("method") == "DELETE":
            gone = mcp.http_delete(session, via="relay")
            return {"t": "res", "id": rid, "status": 204 if gone else 404, "session": None, "body": None}
        status, reply, headers = mcp.http_post(json.dumps(frame.get("body")).encode(), session, via="relay")
        return {"t": "res", "id": rid, "status": status, "session": headers.get("Mcp-Session-Id"), "body": reply}
    except Exception as e:  # never leave the relay without an answer
        body = frame.get("body") if isinstance(frame.get("body"), dict) else {}
        return {"t": "res", "id": rid, "status": 500, "session": None,
                "body": {"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32603, "message": f"maxpm: {e}"}}}


def start(stop: threading.Event, port: int | None = None) -> threading.Thread:
    PAGE_PORT["port"] = port
    t = threading.Thread(target=Link(stop).run, daemon=True, name="maxpm-relay")
    t.start()
    return t


# ---------------------------------------------------------------- connect from the page

PENDING: dict = {}


def connect_from_page(replace=False) -> dict:
    """The page's Connect button: start the device flow, finish it in the background, return the code."""
    dev = start_device()
    cancel = threading.Event()
    old = PENDING.get("cancel")
    if old:
        old.set()
    PENDING.clear()
    PENDING.update({"cancel": cancel})
    _set_state(pending={"code": dev["user_code"], "link": dev["verification_uri_complete"]})

    def finish():
        try:
            cfg = finish_device(dev, replace=replace, sleep=cancel.wait, cancel=cancel)
            _set_state(pending=None, account=cfg.get("account"), error=None)
        except RiverError as e:
            _set_state(pending=None, error=str(e))

    threading.Thread(target=finish, daemon=True, name="maxpm-relay-connect").start()
    return {"code": dev["user_code"], "link": dev["verification_uri_complete"]}
