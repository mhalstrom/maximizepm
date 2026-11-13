"""The relay connection (river/relay.py): maxpm connect, the WebSocket client, and the socket in maxpm serve.

A small WebSocket server in this file plays the relay (relay/ of the site repository has the real one).
"""

import base64
import contextlib
import hashlib
import io
import json
import os
import queue
import re
import socket
import stat
import struct
import tempfile
import threading
import time
import unittest
from unittest import mock

from river import cli, core, mcp, relay
from river.core import RiverError


class FakeSide:
    """The relay's end of one socket: unmasks client frames, sends unmasked frames."""

    def __init__(self, conn, headers):
        self.conn, self.headers, self.buf = conn, headers, b""
        self.pings = 0
        conn.settimeout(5)

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.conn.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def frame(self):
        b0, b1 = self._read(2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack("!H", self._read(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self._read(8))[0]
        assert b1 & 0x80, "a client frame must be masked"
        mask = self._read(4)
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(self._read(n)))
        return b0 & 0x0F, data

    def send(self, opcode, payload=b""):
        n = len(payload)
        head = bytes([0x80 | opcode]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack("!H", n)
                                         if n < 65536 else bytes([127]) + struct.pack("!Q", n))
        self.conn.sendall(head + payload)

    def send_json(self, obj):
        self.send(0x1, json.dumps(obj).encode())

    def recv_json(self):
        while True:
            op, data = self.frame()
            if op == 0x9:
                self.pings += 1
                self.send(0xA, data)
                continue
            if op == 0x8:
                raise ConnectionError("client closed")
            return json.loads(data)


class FakeRelay:
    """Accepts WebSocket upgrades on 127.0.0.1; status other than 101 refuses them."""

    def __init__(self, status=101):
        self.status = status
        self.sock = socket.socket()
        try:
            self.sock.bind(("127.0.0.1", 0))
        except PermissionError:
            self.sock.close()
            raise unittest.SkipTest("no local port here (a sandbox)")
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.sides = queue.Queue()
        self.conns = []
        threading.Thread(target=self._accept, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(conn)
            data = b""
            while b"\r\n\r\n" not in data:
                data += conn.recv(4096)
            lines = data.split(b"\r\n\r\n")[0].decode().split("\r\n")
            headers = {k.strip().lower(): v.strip() for k, v in (x.split(":", 1) for x in lines[1:])}
            if self.status != 101:
                conn.sendall(f"HTTP/1.1 {self.status} No\r\nContent-Length: 0\r\n\r\n".encode())
                conn.close()
                continue
            accept = base64.b64encode(hashlib.sha1((headers["sec-websocket-key"] + relay.GUID).encode()).digest())
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                         b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
            self.sides.put(FakeSide(conn, headers))

    def side(self, timeout=5):
        return self.sides.get(timeout=timeout)

    def close(self):
        self.sock.close()
        for conn in self.conns:
            conn.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.clean_tmp)
        old = os.environ.get("MAXPM_DB")
        os.environ["MAXPM_DB"] = os.path.join(self.tmp.name, "maxpm.db")
        self.addCleanup(lambda: os.environ.__setitem__("MAXPM_DB", old) if old else os.environ.pop("MAXPM_DB", None))
        self.addCleanup(self.join_connect)  # runs before MAXPM_DB goes back (cleanups run last first)
        os.environ.pop("MAXPM_RELAY_URL", None)
        relay.STATE.clear()
        relay.STATE["state"] = "off"
        self.addCleanup(setattr, relay, "URLOPEN", None)
        mcp.HTTP_SESSIONS.clear()


    def clean_tmp(self):
        """A thread of the page server (they are not joined) may still close its queue when the test ends; Windows
        refuses to delete an open file (WinError 32), so the delete tries again for up to five seconds (#5096)."""
        for _ in range(50):
            try:
                return self.tmp.cleanup()
            except OSError:
                time.sleep(0.1)
        self.tmp.cleanup()

    def join_connect(self):
        """The page's Connect finishes in a thread (maxpm-relay-connect) that writes relay-state.json at its end: it
        ends before MAXPM_DB goes back, else its write finds the real ~/.maximizepm (#4906)."""
        for t in threading.enumerate():
            if t.name == "maxpm-relay-connect":
                cancel = relay.PENDING.get("cancel")
                if cancel:
                    cancel.set()
                t.join(10)
                self.assertFalse(t.is_alive(), "the page's Connect thread still runs after its test")


class FakeResponse(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(json.dumps(body).encode())
        self.status = status


def relay_answers(answers, calls):
    """A URLOPEN that answers each POST from a list keyed by path: (status, body), in order."""
    import urllib.error

    def opener(req, timeout):
        path = "/" + req.full_url.split("/", 3)[3]
        calls.append((path, json.loads(req.data or b"{}"), req.get_header("Authorization")))
        status, body = answers[path].pop(0)
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "x", {}, io.BytesIO(json.dumps(body).encode()))
        return FakeResponse(status, body)
    return opener


DEVICE = {"device_code": "dc", "user_code": "BCDF-GHJK", "verification_uri": "https://r.example/river/link",
          "verification_uri_complete": "https://r.example/river/link?code=BCDF-GHJK", "expires_in": 600, "interval": 5}


class Connect(Base):
    def test_the_device_flow_keeps_the_token_in_a_private_file(self):
        calls = []
        relay.URLOPEN = relay_answers({
            "/river/device": [(200, DEVICE)],
            "/river/device/token": [(400, {"error": "authorization_pending"}), (400, {"error": "slow_down"}),
                                    (200, {"river_token": "secret-token", "account": "Alice"})],
        }, calls)
        waits = []
        dev = relay.start_device("https://r.example", host="studio")
        self.assertEqual(calls[0][1]["host"], "studio")
        self.assertEqual(calls[0][1]["version"], relay.__version__)
        cfg = relay.finish_device(dev, sleep=waits.append)
        self.assertEqual(waits, [5, 5, 10], "slow_down adds 5 seconds")
        self.assertEqual(cfg["token"], "secret-token")
        self.assertEqual(relay.load()["account"], "Alice")
        self.assertEqual(relay.config_path(), os.path.join(self.tmp.name, "relay.json"))
        self.assertEqual(stat.S_IMODE(os.stat(relay.config_path()).st_mode), 0o600)
        conn = core.connect()
        self.addCleanup(conn.close)
        self.assertNotIn("secret-token", json.dumps(core.config_list(conn)), "never in the settings")

    def test_a_cancelled_or_expired_code_says_so(self):
        relay.URLOPEN = relay_answers({"/river/device/token": [(400, {"error": "access_denied"}),
                                                               (400, {"error": "expired_token"})]}, [])
        dev = {**DEVICE, "url": "https://r.example", "host": "h"}
        with self.assertRaisesRegex(RiverError, "cancelled"):
            relay.finish_device(dev, sleep=lambda s: None)
        with self.assertRaisesRegex(RiverError, "expired"):
            relay.finish_device(dev, sleep=lambda s: None)
        self.assertIsNone(relay.load())

    def test_off_revokes_the_token_and_deletes_the_file(self):
        relay.save({"url": "https://r.example", "token": "tok", "account": "Alice", "host": "h"})
        calls = []
        relay.URLOPEN = relay_answers({"/river/revoke": [(200, {"revoked": True})]}, calls)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(["connect", "--off"])
        self.assertEqual(calls[0][2], "Bearer tok")
        self.assertIn("Disconnected", out.getvalue())
        self.assertIsNone(relay.load())
        with contextlib.redirect_stdout(io.StringIO()) as again:
            cli.run(["connect", "--off"])
        self.assertIn("not connected", again.getvalue())

    def test_connect_prints_the_code_and_the_page_url(self):
        relay.URLOPEN = relay_answers({
            "/river/device": [(200, DEVICE)],
            "/river/device/token": [(200, {"river_token": "t", "account": "Alice",
                                           "tunnel": "https://alice-ab3cd9.r.example"})]}, [])
        from unittest import mock
        out = io.StringIO()
        with mock.patch.object(relay.time, "sleep"), contextlib.redirect_stdout(out):
            cli.run(["connect", "--relay", "https://r.example", "--no-browser"])
        text = out.getvalue()
        self.assertIn("BCDF-GHJK", text)
        self.assertIn("https://r.example/river/link?code=BCDF-GHJK", text)
        self.assertIn("https://alice-ab3cd9.r.example", text, "the tunnel the relay named")
        self.assertEqual(relay.load()["tunnel"], "https://alice-ab3cd9.r.example")
        self.assertEqual(relay.load()["url"], "https://r.example")

    def test_status_says_when_serve_does_not_run(self):
        self.assertEqual(cli.relay_line(relay.saved_status()), "Relay: not connected (maxpm connect)")
        relay.save({"url": "https://r.example", "token": "t", "account": "Alice", "host": "h"})
        self.assertIn("maxpm serve is not running", cli.relay_line(relay.saved_status()))
        relay._set_state(state="connected", account="Alice")
        self.assertEqual(cli.relay_line(relay.saved_status()), "Relay: connected as Alice (https://r.example)")


class WebSocketClient(Base):
    def test_handshake_frames_ping_and_close(self):
        fake = FakeRelay()
        self.addCleanup(fake.close)
        ws = relay.WebSocket.open(f"ws://127.0.0.1:{fake.port}/river/socket", {"Authorization": "Bearer x"})
        side = fake.side()
        self.assertEqual(side.headers["authorization"], "Bearer x")
        for size in (5, 300, 70000):  # the three length forms
            ws.send_text("é" * size)
            self.assertEqual(side.frame()[1].decode(), "é" * size)
            side.send(0x1, ("ü" * size).encode())
            self.assertEqual(ws.recv(), ("text", "ü" * size))
        side.send(0x9, b"hi")  # a ping from the relay gets a pong
        side.send(0x1, b'"after"')
        self.assertEqual(ws.recv(), ("text", '"after"'))
        self.assertEqual(side.frame(), (0xA, b"hi"))
        side.send(0x1, b"")  # an empty text frame
        self.assertEqual(ws.recv(), ("text", ""))
        side.conn.sendall(bytes([0x01, 3]) + b"abc" + bytes([0x80, 3]) + b"def")  # a fragmented message
        self.assertEqual(ws.recv(), ("text", "abcdef"))
        side.send(0x8, struct.pack("!H", 4000) + b"bye")
        self.assertEqual(ws.recv(), ("close", (4000, "bye")))
        ws.close()

    def test_a_refused_upgrade_names_the_status(self):
        fake = FakeRelay(status=401)
        self.addCleanup(fake.close)
        with self.assertRaises(relay.Refused) as e:
            relay.WebSocket.open(f"ws://127.0.0.1:{fake.port}/river/socket", {})
        self.assertEqual(e.exception.status, 401)

    def test_a_timeout_keeps_a_partial_frame(self):
        fake = FakeRelay()
        self.addCleanup(fake.close)
        ws = relay.WebSocket.open(f"ws://127.0.0.1:{fake.port}/river/socket", {})
        side = fake.side()
        ws.sock.settimeout(0.2)
        side.conn.sendall(bytes([0x81, 5]) + b"he")
        with self.assertRaises(socket.timeout):
            ws.recv()
        side.conn.sendall(b"llo")
        self.assertEqual(ws.recv(), ("text", "hello"))
        ws.close()


class LinkTest(Base):
    def setUp(self):
        super().setUp()
        self.fake = FakeRelay()
        self.addCleanup(self.fake.close)
        self.stop = threading.Event()
        self.conn = core.connect()
        self.addCleanup(self.conn.close)

    def start(self, **cfg):
        relay.save({"url": self.fake.url, "token": "tok", "account": "Alice", "host": "mac", **cfg})
        self.link = relay.Link(self.stop)
        thread = threading.Thread(target=self.link.run, daemon=True)
        thread.start()

        def end():
            # Before MAXPM_DB and the temporary folder go: the link writes relay-state.json as it stops, and a late
            # write found the real ~/.maximizepm (#4906). A link that waits for the relay's welcome (up to 15 s)
            # does not see the stop: closing the fake relay's connections ends that wait at once.
            self.stop.set()
            for conn in self.fake.conns:
                conn.close()
            thread.join(10)
            self.assertFalse(thread.is_alive(), "the relay link still runs after its test")
        self.addCleanup(end)

    def hello(self):
        side = self.fake.side()
        self.assertEqual(side.headers["authorization"], "Bearer tok")
        hello = side.recv_json()
        return side, hello

    def until(self, check, what):
        for _ in range(300):
            if check():
                return
            time.sleep(0.01)
        self.fail(f"waited for {what}: {relay.STATE}")

    def test_says_hello_and_answers_page_requests(self):
        self.start()
        side, hello = self.hello()
        self.assertEqual(hello, {"t": "hello", "v": 3, "river": relay.__version__, "host": "mac", "replace": False,
                                 "ping": True})
        side.send_json({"t": "welcome", "v": 3, "account": "Alice", "limits": {}})
        self.until(lambda: relay.STATE.get("state") == "connected", "connected")
        # No maxpm serve port in this test: the answer says so (PageRelay checks a real one).
        side.send_json({"t": "req", "id": "q1", "kind": "http", "method": "GET", "path": "/", "headers": {},
                        "body_b64": ""})
        res = side.recv_json()
        self.assertEqual((res["t"], res["id"], res["status"]), ("res", "q1", 502))
        side.send_json({"t": "req", "id": "q2", "method": "POST", "body": {"jsonrpc": "2.0", "id": 1}})
        self.assertEqual(side.recv_json()["status"], 400, "only page requests come over the socket")

    def test_hello_names_no_alert_topic(self):
        """#4870: the relay sends no notification and keeps no alert topic, so hello carries none, also when the
        person has an ntfy topic; notifications are a job of maxpm on the computer."""
        core.config_set(self.conn, "ntfy_topic", "maxpm-alice-7f3k2q9x")
        self.start()
        _, hello = self.hello()
        self.assertNotIn("alert", hello)
        self.assertNotIn("relay_offline_alert", core.DEFAULT_SETTINGS)

    def test_the_plan_from_the_relay_shows_in_the_status(self):
        """#4759: welcome and plan frames carry the relay plan; maxpm connect --status says when it ended or a
        payment failed, and a relay with no payments (no plan field) is 'none'."""
        from river import cli
        self.start()
        side, _ = self.hello()
        side.send_json({"t": "welcome", "v": 3, "account": "GitHub account", "plan": "active", "limits": {}})
        self.until(lambda: relay.STATE.get("plan") == "active", "the plan from welcome")
        side.send_json({"t": "plan", "plan": "past_due"})
        self.until(lambda: relay.STATE.get("plan") == "past_due", "the plan frame")
        st = {**relay.status(), "state": "connected"}
        self.assertIn("relay payment failed: update your card at", cli.relay_line(st))
        side.send_json({"t": "plan", "plan": "ended"})
        self.until(lambda: relay.STATE.get("plan") == "ended", "plan ended")
        st = {**relay.status(), "state": "connected"}
        self.assertIn(f"relay plan ended: renew it at {st['url']}/account", cli.relay_line(st))
        side.send_json({"t": "plan", "plan": None})
        self.until(lambda: relay.STATE.get("plan") == "none", "no plan")
        self.assertEqual(relay.plan_note({**relay.status(), "state": "connected"}), "")

    def test_bodies_come_and_go_in_parts(self):
        """A request in parts reaches maxpm serve whole; the answer goes back in parts of the welcome's size, as
        river reads it; a new address from the relay is kept."""
        got, gzips = [], []

        def echo(frame, body, send, part, gzip=False):
            got.append((frame["headers"], body))
            gzips.append(gzip)
            data = body[::-1]
            chunks = [data[at:at + part] for at in range(0, len(data), part)]
            for i, c in enumerate(chunks):
                more = i < len(chunks) - 1
                send({"t": "res", "id": frame["id"], "status": 200, "headers": [], "body_b64": base64.b64encode(c).decode(),
                      "more": more} if i == 0 else
                     {"t": "part", "id": frame["id"], "i": i, "body_b64": base64.b64encode(c).decode(), "more": more})
        with mock.patch.object(relay, "serve_page", echo), mock.patch.object(relay, "MAX_REQUEST", 4096):
            self.start()
            side, _ = self.hello()
            side.send_json({"t": "welcome", "v": 3, "account": "Alice", "tunnel": "https://alice-ab3cd9.relay.example",
                            "limits": {"part": 1024}})
            self.until(lambda: relay.STATE.get("state") == "connected", "connected")
            self.assertEqual(relay.load()["tunnel"], "https://alice-ab3cd9.relay.example")
            self.assertEqual(relay.site_url(), "https://alice-ab3cd9.relay.example")
            body = bytes(range(256)) * 10  # 2560 bytes, not UTF-8
            chunks = [body[:1024], body[1024:2048], body[2048:]]
            side.send_json({"t": "req", "id": "q1", "kind": "http", "method": "POST", "path": "/api/action",
                            "headers": [["X-Maxpm-Token", "t"]], "body_b64": "", "more": True})
            for i, c in enumerate(chunks, 1):
                side.send_json({"t": "part", "id": "q1", "i": i, "body_b64": base64.b64encode(c).decode(),
                                "more": i < 3})
            res = side.recv_json()
            self.assertEqual((res["t"], res["id"], res["status"], res["more"]), ("res", "q1", 200, True))
            back = [base64.b64decode(res["body_b64"])]
            for i in (1, 2):
                part = side.recv_json()
                self.assertEqual((part["t"], part["id"], part["i"], part["more"]), ("part", "q1", i, i < 2))
                back.append(base64.b64decode(part["body_b64"]))
            self.assertEqual(got, [([["X-Maxpm-Token", "t"]], body)])
            self.assertEqual(gzips, [False], "a welcome with no limits.gzip: river sends answers as they are (#4895)")
            self.assertEqual(b"".join(back), body[::-1])

            # A part out of order, and a request over MAX_REQUEST: refused, and nothing reaches maxpm serve.
            side.send_json({"t": "req", "id": "q2", "kind": "http", "method": "POST", "path": "/", "headers": [],
                            "body_b64": "", "more": True})
            side.send_json({"t": "part", "id": "q2", "i": 2, "body_b64": "", "more": False})
            self.assertEqual(side.recv_json()["status"], 400)
            side.send_json({"t": "req", "id": "q3", "kind": "http", "method": "POST", "path": "/", "headers": [],
                            "body_b64": base64.b64encode(b"x" * 3000).decode(), "more": True})
            side.send_json({"t": "part", "id": "q3", "i": 1, "body_b64": base64.b64encode(b"x" * 3000).decode(),
                            "more": False})
            self.assertEqual(side.recv_json()["status"], 413)
            self.assertEqual(len(got), 1)

            # cancel: the request is forgotten.
            side.send_json({"t": "req", "id": "q4", "kind": "http", "method": "POST", "path": "/", "headers": [],
                            "body_b64": "", "more": True})
            side.send_json({"t": "cancel", "id": "q4"})
            side.send_json({"t": "part", "id": "q4", "i": 1, "body_b64": "", "more": False})
            side.send_json({"t": "tunnel", "tunnel": "https://home-zz2345.relay.example"})
            self.until(lambda: (relay.load() or {}).get("tunnel") == "https://home-zz2345.relay.example", "new address")
            self.assertEqual(len(got), 1)

    def test_keeps_the_computer_awake_while_connected(self):
        """#4754: caffeinate -i -w <serve's pid> while relay.json exists; it ends with the connection, and the
        setting relay_keep_awake off keeps it from starting."""
        started = []

        class Proc:
            def __init__(self, argv):
                self.argv, self.ended = argv, False
                started.append(self)

            def poll(self):
                return 0 if self.ended else None

            def terminate(self):
                self.ended = True
        relay.AWAKE_RUNNER = Proc
        self.addCleanup(setattr, relay, "AWAKE_RUNNER", None)
        self.start()
        side, _ = self.hello()
        side.send_json({"t": "welcome", "v": 3, "account": "Alice", "limits": {}})
        self.until(lambda: relay.STATE.get("state") == "connected", "connected")
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0].argv, ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])
        self.assertTrue(relay.STATE.get("awake"))
        os.remove(relay.config_path())
        self.until(lambda: started[0].ended, "caffeinate ends when the computer is disconnected")
        core.config_set(self.conn, "relay_keep_awake", "off")
        with self.assertRaises(RiverError):
            core.config_set(self.conn, "relay_keep_awake", "maybe")
        relay.save({"url": self.fake.url, "token": "tok", "account": "Alice", "host": "mac"})
        side = self.fake.side(timeout=15)  # with no relay.json the link looks again every 5 s
        side.recv_json()
        side.send_json({"t": "welcome", "v": 3, "account": "Alice", "limits": {}})
        self.until(lambda: relay.STATE.get("state") == "connected", "connected again")
        self.assertEqual(len(started), 1, "relay_keep_awake off: the computer sleeps as usual")

    def test_a_relay_that_stops_answering_is_dialed_again(self):
        """#4754: a connection that looks open but carries nothing (a network change, a sleeping router) ends
        after SILENT_LIMIT, and river dials again."""
        for name, value in (("PING_EVERY", 0.1), ("SILENT_LIMIT", 0.6)):
            self.addCleanup(setattr, relay, name, getattr(relay, name))
            setattr(relay, name, value)
        self.start()
        side, _ = self.hello()
        side.send_json({"t": "welcome", "v": 3, "account": "Alice", "limits": {}})
        self.until(lambda: relay.STATE.get("state") == "connected", "connected")
        # The relay side reads nothing more and answers no ping.
        self.until(lambda: relay.STATE.get("state") == "waiting", "the silent connection ended")
        self.assertIn("no answer from the relay", relay.STATE.get("error", ""))
        again, hello = self.hello()
        self.assertEqual(hello["t"], "hello")

    def test_text_pings_when_the_welcome_asks_for_them(self):
        """#4860: Cloudflare answers WebSocket pings at its edge, so the River object finds a silent computer by a
        text ping that it answers by itself. River sends it at the welcome's limits.ping, and none without it."""
        for name, value in (("APP_PING_MIN", 0.05), ("PING_EVERY", 60)):
            self.addCleanup(setattr, relay, name, getattr(relay, name))
            setattr(relay, name, value)
        self.start()
        side, hello = self.hello()
        self.assertIs(hello["ping"], True)
        side.send_json({"t": "welcome", "v": 3, "account": "Alice", "limits": {"ping": 0.1}})
        side.conn.settimeout(2)
        texts = []
        while len(texts) < 2:
            op, data = side.frame()
            if op == 0x1:
                texts.append(data)
                side.send_json({"t": "pong"})  # the River object's answer: river takes it as a frame heard
        self.assertEqual(texts, [b'{"t":"ping"}'] * 2, "exactly the bytes that the relay answers by itself")
        self.assertEqual(relay.STATE.get("state"), "connected")
        # A welcome that names no ping gets none: the relay decides the interval (#4881).
        side.conn.close()
        again, _ = self.hello()
        again.send_json({"t": "welcome", "v": 3, "account": "Alice", "limits": {}})
        self.until(lambda: relay.STATE.get("state") == "connected", "connected again")
        again.conn.settimeout(0.5)
        with self.assertRaises(socket.timeout):
            again.frame()
        self.assertIsNone(self.link.app_ping)

    def test_another_protocol_says_to_update(self):
        self.start()
        side, _ = self.hello()
        side.send_json({"t": "bye", "reason": "protocol 4 needed: update MaximizePM"})
        self.until(lambda: "protocol 4 needed" in (relay.STATE.get("error") or ""), "the error")
        self.assertIn("this MaximizePM speaks protocol 3", relay.STATE["error"])

    def test_pings_and_reconnects_after_the_socket_drops(self):
        relay.PING_EVERY, old = 0.1, relay.PING_EVERY
        self.addCleanup(setattr, relay, "PING_EVERY", old)
        self.start()
        side, _ = self.hello()
        side.send_json({"t": "welcome", "v": 3, "account": "Alice", "limits": {}})
        side.conn.settimeout(2)
        pings = 0
        while pings < 2:  # river pings every PING_EVERY
            op, data = side.frame()
            if op == 0x9:
                pings += 1
                side.send(0xA, data)
        side.conn.close()
        self.until(lambda: relay.STATE.get("state") == "waiting", "waiting")
        again, hello = self.hello()  # the backoff starts at about a second
        self.assertEqual(hello["t"], "hello")
        again.send_json({"t": "welcome", "v": 3, "account": "Alice", "limits": {}})
        self.until(lambda: relay.STATE.get("state") == "connected", "connected again")

    def test_stops_trying_when_the_relay_revokes_the_token(self):
        self.start()
        side, _ = self.hello()
        side.send_json({"t": "welcome", "v": 3, "account": "Alice", "limits": {}})
        side.send_json({"t": "bye", "reason": "revoked"})
        self.until(lambda: relay.STATE.get("state") == "error", "error")
        self.assertIn("run maxpm connect", relay.STATE["error"])
        time.sleep(1.5)
        self.assertTrue(self.fake.sides.empty(), "no new connection with a revoked token")

    def test_a_401_waits_for_a_new_connect(self):
        self.fake.status = 401
        self.start()
        self.until(lambda: relay.STATE.get("state") == "error", "error")
        self.assertIn("refused this computer's token", relay.STATE["error"])

    def test_replace_goes_once_and_another_computer_is_named(self):
        self.start(replace=True)
        side, hello = self.hello()
        self.assertTrue(hello["replace"])
        side.send_json({"t": "welcome", "v": 3, "account": "Alice", "limits": {}})
        self.until(lambda: relay.STATE.get("state") == "connected", "connected")
        self.assertNotIn("replace", relay.load(), "replace is for one connection")

    def test_off_ends_the_connection(self):
        self.start()
        side, _ = self.hello()
        side.send_json({"t": "welcome", "v": 3, "account": "Alice", "limits": {}})
        self.until(lambda: relay.STATE.get("state") == "connected", "connected")
        os.remove(relay.config_path())
        self.until(lambda: relay.STATE.get("state") == "off", "off")


class Sessions(Base):
    def chat_go(self):
        init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).encode()
        _, _, h = mcp.http_post(init)
        call = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "go", "arguments": {}}})
        _, reply, _ = mcp.http_post(call.encode(), h["Mcp-Session-Id"])
        text = reply["result"]["content"][0]["text"]
        return re.search(r"You are MaximizePM agent (\S+?)\.", text).group(1)

    def test_a_chat_takes_nothing_from_the_environment_of_serve(self):
        from unittest import mock
        conn = core.connect()
        self.addCleanup(conn.close)
        core.project_add(conn, "web", None, "", "mark")
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1", "MAXPM_MODEL": "opus", "MAXPM_FOCUS": "manager"}):
            name = self.chat_go()
        row = dict(conn.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone())
        self.assertEqual(row["via"], "http")
        self.assertIsNone(row["agent_type"], "not maxpm serve's Claude Code")
        self.assertIsNone(row["model"])
        self.assertIsNone(row["pid"])
        self.assertIsNone(row["platform"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(["who"])
        self.assertIn("chat over MCP: http", out.getvalue())

    def test_events_say_relay_only_while_a_relayed_action_runs(self):
        conn = core.connect()
        self.addCleanup(conn.close)
        token = core.EVENT_VIA.set("relay")
        try:
            core.project_add(conn, "a", None, "", "bob")
        finally:
            core.EVENT_VIA.reset(token)
        core.project_add(conn, "b", None, "", "bob")
        self.assertEqual([r[0] for r in conn.execute("SELECT actor FROM events ORDER BY rowid")], ["bob@relay", "bob"])


class PageRelay(Base):
    """Page requests that come over the relay socket go to this computer's maxpm serve."""

    def setUp(self):
        super().setUp()
        from river import server
        try:
            self.httpd = server._Server(("127.0.0.1", 0), server.Handler)
        except PermissionError:
            self.skipTest("no local port here (a sandbox)")
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        relay.PAGE_PORT["port"] = self.httpd.server_port
        self.addCleanup(relay.PAGE_PORT.__setitem__, "port", None)

    def page(self, method, path, body=None):
        frame = {"t": "req", "id": "p1", "kind": "http", "method": method, "path": path,
                 "headers": [["Content-Type", "application/json"]],
                 "body_b64": base64.b64encode(json.dumps(body).encode()).decode() if body is not None else ""}
        res = relay.handle_page(frame, base64.b64decode(frame["body_b64"]))
        self.assertEqual(res["id"], "p1")
        return res["status"], {k.lower(): v for k, v in res["headers"]}, res["body"]

    def test_the_same_page_and_api(self):
        status, headers, html = self.page("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["content-type"])
        self.assertIn(b'id="relayBtn"', html)
        self.assertNotIn(b'"/api/', html)
        status, _, app_js = self.page("GET", "/app.js")
        self.assertEqual(status, 200)
        self.assertNotIn(b'fetch("/api/', app_js, "relative paths, so the page works at any depth")
        status, _, state = self.page("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertIn("relay", json.loads(state))

    def test_actions_through_the_relay_are_marked(self):
        status, _, out = self.page("POST", "/api/action", {"op": "project_add", "args": {"name": "web"}, "actor": "mark"})
        self.assertEqual(status, 200, out)
        conn = core.connect()
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT actor FROM events").fetchone()[0], "mark@relay")
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{self.httpd.server_port}/api/action", method="POST",
                                     data=json.dumps({"op": "project_add", "args": {"name": "app"}, "actor": "mark"}).encode(),
                                     headers={"content-type": "application/json", "X-Maxpm-Relay": "guessed"})
        urllib.request.urlopen(req).close()
        self.assertEqual(conn.execute("SELECT actor FROM events ORDER BY rowid DESC").fetchone()[0], "mark",
                         "a wrong secret marks nothing")

    def test_the_page_token_passes(self):
        """Typing into a session through the relay: the page's token header reaches maxpm serve (#4710)."""
        def action(op, token=None):
            frame = {"t": "req", "id": "p1", "kind": "http", "method": "POST", "path": "/api/action",
                     "headers": [["Content-Type", "application/json"], *([["X-Maxpm-Token", token]] if token else [])]}
            res = relay.handle_page(frame, json.dumps({"op": op, "args": {"agent": "nobody", "text": "hi"},
                                                       "actor": "mark"}).encode())
            return res["status"], json.loads(res["body"])
        status, out = action("session_send")
        self.assertEqual(status, 403)
        self.assertIn("token", out["error"])
        token = json.loads(self.page("GET", "/api/token")[2])["token"]
        status, out = action("session_send", token)
        self.assertEqual(status, 409, out)
        self.assertIn("does not run in a tmux pane", out["error"], "past the token check")

    def test_the_whole_site_passes_and_the_answer_streams_in_parts(self):
        """#4733: every header passes; the tunnel's origin is this computer's for maxpm serve, another origin
        stays (and serve refuses it); the answer comes in frames of the part size as river reads it."""
        relay.TUNNEL["url"] = "https://alice-ab3cd9.relay.example"
        self.addCleanup(relay.TUNNEL.__setitem__, "url", None)

        def post(origin):
            frame = {"t": "req", "id": "p1", "kind": "http", "method": "POST", "path": "/api/action",
                     "headers": [["Content-Type", "application/json"], ["Origin", origin], ["Host", "evil"],
                                 ["Connection", "close"], ["X-Maxpm-Relay", "guess"]]}
            return relay.handle_page(frame, json.dumps({"op": "project_add", "args": {"name": "web"},
                                                        "actor": "mark"}).encode())["status"]
        self.assertEqual(post("https://alice-ab3cd9.relay.example"), 200, "the tunnel's own page may act")
        self.assertEqual(post("https://evil.example"), 403, "another site may not")
        conn = core.connect()
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT actor FROM events").fetchone()[0], "mark@relay")

        frames = []
        relay.serve_page({"t": "req", "id": "p2", "kind": "http", "method": "GET", "path": "/app.js",
                          "headers": [["Accept", "*/*"]]}, b"", frames.append, part=4096)
        whole = b"".join(base64.b64decode(f["body_b64"]) for f in frames)
        self.assertGreater(len(frames), 2)
        self.assertEqual([f["t"] for f in frames], ["res"] + ["part"] * (len(frames) - 1))
        self.assertEqual([f["more"] for f in frames], [True] * (len(frames) - 1) + [False])
        self.assertTrue(all(len(base64.b64decode(f["body_b64"])) <= 4096 for f in frames))
        self.assertEqual(whole, self.page("GET", "/app.js")[2])
        self.assertIn(["Content-Length", str(len(whole))], frames[0]["headers"])

        local = f"http://127.0.0.1:{relay.PAGE_PORT['port']}"
        self.assertEqual(relay.answer_headers([("Location", local + "/sessions"), ("Transfer-Encoding", "chunked"),
                                               ("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")], local),
                         [["Location", "https://alice-ab3cd9.relay.example/sessions"], ["Set-Cookie", "a=1"],
                          ["Set-Cookie", "b=2"]])

    def test_a_text_answer_goes_gzipped_when_the_welcome_and_the_browser_take_it(self):
        """#4895: the first state crosses the river-relay socket gzipped; the relay passes it as it is."""
        import gzip as gz
        # One time for the whole test: /api/state carries the time, and each answer must equal the first one
        # (#5087: the second ticked over between two answers on CI).
        fixed = core.now()
        clock = mock.patch.object(core, "now", lambda: fixed)
        clock.start()
        self.addCleanup(clock.stop)
        plain = self.page("GET", "/api/state")[2]

        def frames(path, gzip=True, accept="gzip, deflate, br", method="GET", part=4096):
            out = []
            relay.serve_page({"t": "req", "id": "g1", "kind": "http", "method": method, "path": path,
                              "headers": [["Accept-Encoding", accept]] if accept else []}, b"", out.append, part, gzip)
            heads = {k.lower(): v for k, v in out[0]["headers"]}
            return out, heads, b"".join(base64.b64decode(f["body_b64"]) for f in out)
        out, heads, body = frames("/api/state")
        self.assertEqual((heads.get("content-encoding"), heads.get("vary"), heads.get("content-length")),
                         ("gzip", "Accept-Encoding", None))
        self.assertEqual(gz.decompress(body), plain)
        self.assertLess(len(body), len(plain) * 0.6)
        self.assertEqual([f["more"] for f in out], [True] * (len(out) - 1) + [False])
        self.assertTrue(all(len(base64.b64decode(f["body_b64"])) <= 4096 for f in out))
        html = frames("/", part=1024 * 1024)
        self.assertEqual((html[1].get("content-encoding"), len(html[0])), ("gzip", 1), "one frame when it fits")
        self.assertIn(b'id="relayBtn"', gz.decompress(html[2]))
        # As it is: no gzip in the welcome, a browser that does not take it, HEAD, or a short answer.
        for kw in ({"gzip": False}, {"accept": None}, {"accept": "gzip;q=0, identity"}, {"method": "HEAD"}):
            o, h, b = frames("/api/state", **kw)
            self.assertNotIn("content-encoding", h, kw)
            self.assertEqual(b, b"" if kw.get("method") == "HEAD" else plain, kw)
        token = frames("/api/token")
        self.assertNotIn("content-encoding", token[1], "under GZIP_MIN")
        self.assertTrue(relay.accepts_gzip([["accept-encoding", "br;q=1, *;q=0.5"]]))
        self.assertFalse(relay.accepts_gzip([["Accept-Encoding", "br, deflate"]]))

    def test_bad_frames_and_no_serve(self):
        self.assertEqual(self.page("GET", "api/state")[0], 400)
        self.assertEqual(self.page("get", "/")[0], 400)
        relay.PAGE_PORT["port"] = None
        self.assertEqual(self.page("GET", "/")[0], 502)


class PageConnect(Base):
    def test_the_page_starts_the_flow_and_serve_finishes_it(self):
        from river import server
        relay.URLOPEN = relay_answers({
            "/river/device": [(200, {**DEVICE, "interval": 1})],
            "/river/device/token": [(200, {"river_token": "t", "account": "Alice"})],
            "/river/revoke": [(200, {"revoked": True})]}, [])
        old = relay.DEFAULT_URL
        relay.DEFAULT_URL = "https://r.example"
        self.addCleanup(setattr, relay, "DEFAULT_URL", old)
        conn = core.connect()
        self.addCleanup(conn.close)
        got = server.OPS["relay_connect"](conn, {}, "mark")
        self.assertEqual(got, {"code": "BCDF-GHJK", "link": DEVICE["verification_uri_complete"]})
        self.assertEqual(relay.status()["pending"]["code"], "BCDF-GHJK")
        for _ in range(500):  # the background poll waits the interval first
            if relay.load():
                break
            time.sleep(0.01)
        self.assertEqual(relay.load()["account"], "Alice")
        self.assertNotIn("pending", relay.status())
        self.assertEqual(server.OPS["relay_disconnect"](conn, {}, "mark")["was_connected"], True)


if __name__ == "__main__":
    unittest.main()


class LoginAgent(Base):
    """#4754: maxpm serve --install-login and --remove-login (a launchd agent)."""

    def setUp(self):
        super().setUp()
        from river import login
        self.login = login
        self.calls = []
        self.loaded = False

        def launchctl(argv):
            self.calls.append(argv[1])
            if argv[1] == "print":
                return (0 if self.loaded else 113), ""
            return 0, ""
        login.LAUNCHCTL = launchctl
        self.addCleanup(setattr, login, "LAUNCHCTL", None)
        home = os.path.join(self.tmp.name, "home")
        patch = mock.patch.dict(os.environ, {"HOME": home})
        patch.start()
        self.addCleanup(patch.stop)
        self.conn = core.connect()
        self.addCleanup(self.conn.close)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.free = s.getsockname()[1]
        s.close()
        core.config_set(self.conn, "serve_port", str(self.free))

    def test_install_writes_the_agent_and_starts_it_when_no_serve_runs(self):
        import plistlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(["serve", "--install-login"])
        path = self.login.plist_path()
        self.assertTrue(str(path).startswith(os.path.join(self.tmp.name, "home")))
        spec = plistlib.loads(path.read_bytes())
        self.assertEqual(spec["Label"], "com.maximizepm.serve")
        self.assertEqual(spec["ProgramArguments"][2:], ["serve", "--port", str(self.free)])
        self.assertTrue(spec["RunAtLoad"])
        self.assertEqual(spec["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(spec["EnvironmentVariables"]["MAXPM_DB"], os.environ["MAXPM_DB"])
        self.assertIn("bootstrap", self.calls)
        self.assertIn("it runs now", out.getvalue())

        # Loaded already (it may run this very serve): the file changes, launchd is left alone.
        self.calls.clear()
        self.loaded = True
        with contextlib.redirect_stdout(io.StringIO()) as again:
            cli.run(["serve", "--install-login"])
        self.assertNotIn("bootstrap", self.calls)
        self.assertNotIn("bootout", self.calls)
        self.assertIn("next login", again.getvalue())

        with contextlib.redirect_stdout(io.StringIO()) as gone:
            cli.run(["serve", "--remove-login"])
        self.assertFalse(path.exists())
        self.assertIn("bootout", self.calls)
        self.assertIn("no longer starts at login", gone.getvalue())

    def test_install_watchdog_writes_a_job_every_minute_and_loads_it_again(self):
        # #5441: maxpm ops check --outside from launchd, also when no maxpm serve runs.
        import plistlib
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.run(["serve", "--install-watchdog"])
        path = self.login.plist_path(self.login.WATCHDOG_LABEL)
        spec = plistlib.loads(path.read_bytes())
        self.assertEqual(spec["Label"], "com.maximizepm.watchdog")
        self.assertEqual(spec["ProgramArguments"][2:], ["ops", "check", "--outside"])
        self.assertEqual((spec["StartInterval"], spec["RunAtLoad"]), (60, True))
        self.assertNotIn("KeepAlive", spec)
        self.assertEqual(spec["EnvironmentVariables"]["MAXPM_DB"], os.environ["MAXPM_DB"])
        self.assertTrue(spec["StandardOutPath"].endswith("watchdog.log"))
        self.assertEqual(self.calls, ["print", "bootstrap"])
        self.assertIn("No check is marked outside yet", out.getvalue())
        # Loaded already: it holds no state, so it is loaded again with the new file.
        self.calls.clear()
        self.loaded = True
        self.login.install_watchdog(self.conn)
        self.assertEqual(self.calls, ["print", "bootout", "bootstrap"])
        with contextlib.redirect_stdout(io.StringIO()) as gone:
            cli.run(["serve", "--remove-watchdog"])
        self.assertFalse(path.exists())
        self.assertIn("the watchdog no longer runs", gone.getvalue())

    def test_kick_loads_the_agent_or_restarts_it_and_needs_one(self):
        with self.assertRaisesRegex(core.RiverError, "install one first"):
            self.login.kick()
        with contextlib.redirect_stdout(io.StringIO()):
            cli.run(["serve", "--install-login"])
        self.calls.clear()
        self.assertEqual(self.login.kick()["how"], "loaded")  # the file, not loaded: launchd loads it
        self.assertEqual(self.calls, ["print", "bootstrap"])
        self.calls.clear()
        self.loaded = True
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.run(["serve", "--kick"])
        self.assertEqual(self.calls, ["print", "kickstart"])
        self.assertIn("stopped and started again", out.getvalue())

    def test_a_serve_that_runs_already_is_not_started_twice(self):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", self.free))
        except OSError:
            self.skipTest("the port was taken")
        s.listen()
        self.addCleanup(s.close)
        res = self.login.install(self.conn)
        self.assertTrue(res["already_running"])
        self.assertFalse(res["started_now"])
        self.assertNotIn("bootstrap", self.calls)

    # #5649: the desktop app installs and keeps its own watchdog.

    def app(self):
        """The variables of the desktop app: its bundle and its folder, in the test's folder."""
        bundle, appdir = os.path.join(self.tmp.name, "bundle"), os.path.join(self.tmp.name, "appdir")
        patch = mock.patch.dict(os.environ, {"MAXPM_DESKTOP": "1", "MAXPM_APP_BUNDLE": bundle, "MAXPM_APP_DIR": appdir,
                                             "MAXPM_APP_SHELL": "abc"})
        patch.start()
        self.addCleanup(patch.stop)
        return bundle, appdir

    def marker(self, appdir, **kw):
        os.makedirs(appdir, exist_ok=True)
        with open(os.path.join(appdir, "open"), "w") as f:
            json.dump({"app": os.getpid(), "serve": None, "db": os.environ["MAXPM_DB"], **kw}, f)

    def test_the_app_installs_the_watchdog_with_its_launcher_which_follows_each_update(self):
        import plistlib
        import subprocess
        import sys
        bundle, appdir = self.app()
        res = self.login.app_start(self.conn)
        self.assertEqual((res["watchdog"], res["background"]), ("installed", "off"))
        self.assertEqual(self.calls, ["print", "bootstrap"])
        spec = plistlib.loads(self.login.plist_path(self.login.WATCHDOG_LABEL).read_bytes())
        launcher = os.path.join(appdir, "bin", "maxpm")
        self.assertEqual(spec["ProgramArguments"], [launcher, "ops", "check", "--outside"])
        env = spec["EnvironmentVariables"]
        self.assertEqual((env["MAXPM_DESKTOP"], env["MAXPM_APP_BUNDLE"], env["MAXPM_APP_DIR"], env["MAXPM_APP_SHELL"],
                          env["PYTHONDONTWRITEBYTECODE"]), ("1", bundle, appdir, "abc", "1"))
        self.assertTrue(env["PATH"].startswith(os.path.join(appdir, "bin") + os.pathsep))  # the checks' maxpm
        # The launcher runs the code version that current names (an update moves it), else the app's own copy.
        new = os.path.join(appdir, "versions", "9.9.9")
        for root in (bundle, new):
            os.makedirs(os.path.join(root, "bin"), exist_ok=True)
            with open(os.path.join(root, "bin", "maxpm"), "w") as f:
                f.write("import sys; print(__file__, sys.argv[1:])\n")
        run = lambda: subprocess.run([launcher, "ops"], capture_output=True, text=True, timeout=20).stdout  # noqa: E731
        self.assertEqual(run().strip(), f"{bundle}/bin/maxpm ['ops']")
        with open(os.path.join(appdir, "current"), "w") as f:
            f.write(new + "\n")
        self.assertEqual(run().strip(), f"{new}/bin/maxpm ['ops']")
        # The next start with the same job loaded changes nothing.
        self.calls.clear()
        self.loaded = True
        self.assertEqual(self.login.app_start(self.conn)["watchdog"], "current")
        self.assertEqual(self.calls, ["print"])
        with open(launcher, encoding="utf-8") as f:
            self.assertIn(sys.executable, f.read())  # the app's Python

    def test_the_app_leaves_a_login_serve_of_another_install_and_its_watchdog_alone(self):
        with contextlib.redirect_stdout(io.StringIO()):
            cli.run(["serve", "--install-login"])  # a git clone's login serve, from a terminal
        self.app()
        self.calls.clear()
        res = self.login.app_start(self.conn)
        self.assertEqual((res["watchdog"], res["background"]), ("left", "other"))
        self.assertIn("serve --port", res["by"])
        self.assertFalse(self.login.plist_path(self.login.WATCHDOG_LABEL).exists())
        self.assertEqual(self.calls, [])

    def test_keep_running_in_the_background_is_a_login_agent_that_runs_the_apps_launcher(self):
        import plistlib
        _, appdir = self.app()
        with contextlib.redirect_stdout(io.StringIO()):
            cli.run(["serve", "--install-login"])
        spec = plistlib.loads(self.login.plist_path().read_bytes())
        self.assertEqual(spec["ProgramArguments"], [os.path.join(appdir, "bin", "maxpm"), "serve", "--port", str(self.free)])
        res = self.login.app_start(self.conn)
        self.assertEqual((res["watchdog"], res["background"]), ("installed", "on"))
        self.assertTrue(self.login.serve_wanted())

    def test_serve_is_wanted_while_a_login_agent_runs_it_or_the_app_is_open(self):
        import subprocess
        import sys
        _, appdir = self.app()
        self.assertFalse(self.login.serve_wanted())  # the app was quit: serve stopped on purpose
        self.marker(appdir)
        self.assertTrue(self.login.serve_wanted())
        self.marker(appdir, db=os.path.join(self.tmp.name, "other.db"))
        self.assertFalse(self.login.serve_wanted())  # the app is open for another queue
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        self.marker(appdir, app=gone.pid)
        self.assertFalse(self.login.serve_wanted())  # the app crashed
        with contextlib.redirect_stdout(io.StringIO()):
            cli.run(["serve", "--install-login"])
        self.assertTrue(self.login.serve_wanted())

    def test_kick_with_the_app_open_stops_the_serve_that_the_app_started(self):
        import subprocess
        import sys
        _, appdir = self.app()
        self.marker(appdir)
        with self.assertRaisesRegex(core.RiverError, "install one first"):
            self.login.kick()  # the app runs no serve of its own (a terminal's serve is never stopped)
        serve = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(serve.kill)
        self.marker(appdir, serve=serve.pid)
        with mock.patch.object(self.login, "KILL_WAIT", 0.5), contextlib.redirect_stdout(io.StringIO()) as out:
            cli.run(["serve", "--kick"])
        self.assertIn("the desktop app starts it again", out.getvalue())
        self.assertIsNotNone(serve.wait(timeout=5))


class LoginSystemd(Base):
    """#5520: the same on Linux, as systemd user units."""

    def setUp(self):
        super().setUp()
        from river import login
        self.login = login
        self.calls = []

        def systemctl(argv):
            self.assertEqual(argv[:2], ["systemctl", "--user"])
            self.calls.append(" ".join(argv[2:]))
            return 0, ""
        login.SYSTEMCTL = systemctl
        self.addCleanup(setattr, login, "SYSTEMCTL", None)
        self.units = os.path.join(self.tmp.name, "config")
        patch = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.units})
        patch.start()
        self.addCleanup(patch.stop)
        self.conn = core.connect()
        self.addCleanup(self.conn.close)
        core.config_set(self.conn, "serve_port", "1")  # nothing answers on port 1

    def unit(self, name):
        with open(os.path.join(self.units, "systemd", "user", name), encoding="utf-8") as f:
            return f.read()

    def test_login_is_a_user_service_that_restarts_after_a_crash(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.run(["serve", "--install-login"])
        text = self.unit("maxpm-serve.service")
        self.assertIn(" serve --port 1\n", text)
        self.assertIn("Restart=on-failure\n", text)
        self.assertIn(f"Environment=MAXPM_DB={os.environ['MAXPM_DB']}\n", text)
        self.assertIn("WantedBy=default.target", text)
        self.assertEqual(self.calls, ["daemon-reload", "enable --now maxpm-serve.service"])
        self.assertIn("loginctl enable-linger", out.getvalue())
        self.calls.clear()
        self.assertEqual(self.login.kick()["how"], "restarted")
        self.assertEqual(self.calls, ["restart maxpm-serve.service"])
        self.calls.clear()
        self.assertTrue(self.login.remove()["removed"])
        self.assertEqual(self.calls, ["disable --now maxpm-serve.service", "daemon-reload"])
        self.assertFalse(os.path.exists(os.path.join(self.units, "systemd", "user", "maxpm-serve.service")))
        with self.assertRaisesRegex(core.RiverError, "install one first"):
            self.login.kick()

    def test_the_watchdog_is_a_timer_every_minute(self):
        res = self.login.install_watchdog(self.conn)
        self.assertEqual(res["system"], "systemd")
        service, timer = self.unit("maxpm-watchdog.service"), self.unit("maxpm-watchdog.timer")
        self.assertIn("Type=oneshot\n", service)
        self.assertIn(" ops check --outside\n", service)
        self.assertIn("OnUnitActiveSec=60s\n", timer)
        self.assertEqual(self.calls, ["daemon-reload", "enable --now maxpm-watchdog.timer",
                                      "restart maxpm-watchdog.timer"])
        self.calls.clear()
        with contextlib.redirect_stdout(io.StringIO()) as gone:
            cli.run(["serve", "--remove-watchdog"])
        self.assertIn("the watchdog no longer runs", gone.getvalue())
        self.assertEqual(self.calls, ["disable --now maxpm-watchdog.timer", "daemon-reload"])
        self.assertFalse(os.path.exists(os.path.join(self.units, "systemd", "user", "maxpm-watchdog.service")))

    def test_a_word_with_a_space_is_quoted_for_systemd(self):
        self.assertEqual(self.login._quote("/a b/python3"), '"/a b/python3"')
        self.assertEqual(self.login._quote("x$y%z"), '"x$$y%%z"')
        self.assertEqual(self.login._quote("/usr/bin/python3"), "/usr/bin/python3")

