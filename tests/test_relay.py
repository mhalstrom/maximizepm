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
        self.addCleanup(self.tmp.cleanup)
        old = os.environ.get("MAXPM_DB")
        os.environ["MAXPM_DB"] = os.path.join(self.tmp.name, "maxpm.db")
        self.addCleanup(lambda: os.environ.__setitem__("MAXPM_DB", old) if old else os.environ.pop("MAXPM_DB", None))
        os.environ.pop("MAXPM_RELAY_URL", None)
        relay.STATE.clear()
        relay.STATE["state"] = "off"
        self.addCleanup(setattr, relay, "URLOPEN", None)
        mcp.HTTP_SESSIONS.clear()


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
            "/river/device/token": [(200, {"river_token": "t", "account": "Alice"})]}, [])
        from unittest import mock
        out = io.StringIO()
        with mock.patch.object(relay.time, "sleep"), contextlib.redirect_stdout(out):
            cli.run(["connect", "--relay", "https://r.example", "--no-browser"])
        text = out.getvalue()
        self.assertIn("BCDF-GHJK", text)
        self.assertIn("https://r.example/river/link?code=BCDF-GHJK", text)
        self.assertIn("https://r.example/app/", text)
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

        def end():  # before the temporary folder goes: the link writes relay-state.json as it stops
            self.stop.set()
            thread.join(5)
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
        self.assertEqual(hello, {"t": "hello", "v": 1, "river": relay.__version__, "host": "mac", "replace": False})
        side.send_json({"t": "welcome", "v": 1, "account": "Alice", "limits": {}})
        self.until(lambda: relay.STATE.get("state") == "connected", "connected")
        # No maxpm serve port in this test: the answer says so (PageRelay checks a real one).
        side.send_json({"t": "req", "id": "q1", "kind": "http", "method": "GET", "path": "/", "headers": {},
                        "body_b64": ""})
        res = side.recv_json()
        self.assertEqual((res["t"], res["id"], res["status"]), ("res", "q1", 502))
        side.send_json({"t": "req", "id": "q2", "method": "POST", "body": {"jsonrpc": "2.0", "id": 1}})
        self.assertEqual(side.recv_json()["status"], 400, "only page requests come over the socket")

    def test_pings_and_reconnects_after_the_socket_drops(self):
        relay.PING_EVERY, old = 0.1, relay.PING_EVERY
        self.addCleanup(setattr, relay, "PING_EVERY", old)
        self.start()
        side, _ = self.hello()
        side.send_json({"t": "welcome", "v": 1, "account": "Alice", "limits": {}})
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
        again.send_json({"t": "welcome", "v": 1, "account": "Alice", "limits": {}})
        self.until(lambda: relay.STATE.get("state") == "connected", "connected again")

    def test_stops_trying_when_the_relay_revokes_the_token(self):
        self.start()
        side, _ = self.hello()
        side.send_json({"t": "welcome", "v": 1, "account": "Alice", "limits": {}})
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
        side.send_json({"t": "welcome", "v": 1, "account": "Alice", "limits": {}})
        self.until(lambda: relay.STATE.get("state") == "connected", "connected")
        self.assertNotIn("replace", relay.load(), "replace is for one connection")

    def test_off_ends_the_connection(self):
        self.start()
        side, _ = self.hello()
        side.send_json({"t": "welcome", "v": 1, "account": "Alice", "limits": {}})
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
                 "headers": {"content-type": "application/json", "origin": "https://evil.example"},
                 "body_b64": base64.b64encode(json.dumps(body).encode()).decode() if body is not None else ""}
        res = relay.handle_page(frame)
        self.assertEqual(res["id"], "p1")
        return res["status"], res["headers"], base64.b64decode(res["body_b64"])

    def test_the_same_page_and_api(self):
        status, headers, html = self.page("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["content-type"])
        self.assertIn(b'id="relayBtn"', html)
        self.assertNotIn(b'"/api/', html)
        status, _, app_js = self.page("GET", "/app.js")
        self.assertEqual(status, 200)
        self.assertNotIn(b'fetch("/api/', app_js, "relative paths, so the page works under /app/ on the relay")
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

    def test_bad_frames_and_no_serve(self):
        self.assertEqual(self.page("GET", "api/state")[0], 400)
        self.assertEqual(self.page("DELETE", "/")[0], 400)
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
