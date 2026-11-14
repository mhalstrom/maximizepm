"""The page over Tailscale (#5863, #5853): maxpm serve takes the requests that tailscale serve forwards from the
person's own tailnet logins and refuses the rest; the setup guide's Your phone and maxpm phone show the address and
its QR code. The Tailscale CLI is a fake here (tailnet.RUNNER)."""
import contextlib
import io
import json
import os
import tempfile
import unittest

from river import core, mcp, server, tailnet
from tests.test_view_sessions import Base, drive_page, DRIVE_HEAD

NAME = "mac.tail1a2b.ts.net"
STATUS = {"BackendState": "Running", "Self": {"DNSName": NAME + ".", "UserID": 7},
          "User": {"7": {"LoginName": "mark@example.com", "DisplayName": "Mark"}}}


class Fake:
    """The Tailscale CLI: tailscale status --json, serve status --json, serve --bg <port>."""

    def __init__(self, status=STATUS, serving=False):
        self.status, self.serving, self.calls = status, serving, []

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["status", "--json"]:
            return (0, json.dumps(self.status)) if self.status is not None else (1, "")
        if args[:3] == ["serve", "status", "--json"]:
            return 0, json.dumps({"Web": {f"{NAME}:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8765"}}}}}
                                 if self.serving else {})
        if args[:2] == ["serve", "--bg"]:
            self.serving = True
            return 0, ""
        return 1, ""


def use(test, fake):
    old = tailnet.RUNNER
    tailnet.RUNNER = fake
    tailnet.forget()
    test.addCleanup(lambda: (setattr(tailnet, "RUNNER", old), tailnet.forget()))
    return fake


class QR(unittest.TestCase):
    def test_the_smallest_version_that_holds_the_text(self):
        self.assertEqual(len(tailnet.qr_matrix("hi")), 21)  # version 1
        self.assertEqual(len(tailnet.qr_matrix(f"https://{NAME}/")), 29)  # version 3
        m = tailnet.qr_matrix("https://example.com/" + "x" * 120)  # version 8: several blocks
        self.assertEqual(len(m), 49)
        with self.assertRaisesRegex(ValueError, "too long"):
            tailnet.qr_matrix("x" * 400)

    def test_the_finder_patterns_and_the_dark_module(self):
        m = tailnet.qr_matrix(f"https://{NAME}/")
        n = len(m)
        for x0, y0 in ((0, 0), (n - 7, 0), (0, n - 7)):
            self.assertTrue(all(m[y0][x0 + i] and m[y0 + 6][x0 + i] for i in range(7)))
            self.assertTrue(all(m[y0 + 2 + i][x0 + 2 + j] for i in range(3) for j in range(3)))
            self.assertFalse(m[y0 + 1][x0 + 1])
        self.assertTrue(m[n - 8][8])

    def test_svg_and_text(self):
        svg = tailnet.qr_svg('a"<b>')
        self.assertTrue(svg.startswith("<svg") and 'aria-label="QR code of a&quot;&lt;b&gt;"' in svg)
        lines = tailnet.qr_text("hi").splitlines()
        self.assertEqual(len(lines), (2 + 21 + 3) // 2)  # a quiet zone of two rows above, three below, two per line
        self.assertEqual({len(x) for x in lines}, {25})

    def test_chrome_reads_each_code_back(self):
        # The codes scan: Chrome's BarcodeDetector decodes them (a QR of a person's phone camera reads the same).
        import subprocess
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from tests.test_server import _chrome
        if not _chrome():
            self.skipTest("no Chrome on this computer")
        texts = [f"https://{NAME}/", "hi", "https://example.com/" + "x" * 120, "MaximizePM ✓ ünïcode"]
        page = ("""<!doctype html><meta charset="utf-8"><script>(async () => { const out = [];
  try { const d = new BarcodeDetector({ formats: ["qr_code"] });
    for (const s of %s) { const img = new Image(); img.src = URL.createObjectURL(new Blob([s], { type: "image/svg+xml" }));
      await img.decode(); const c = document.createElement("canvas"); c.width = img.naturalWidth; c.height = img.naturalHeight;
      c.getContext("2d").drawImage(img, 0, 0); out.push((await d.detect(c)).map(x => x.rawValue)); } }
  catch (e) { out.push("ERR " + e); }
  await fetch("/r", { method: "POST", body: JSON.stringify(out) }); })();</script>"""
                % json.dumps([tailnet.qr_svg(t, 8) for t in texts])).encode()
        got, done = {}, threading.Event()

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(page)

            def do_POST(self):
                got["r"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.send_response(204)
                self.end_headers()
                done.set()
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        except PermissionError:
            self.skipTest("no local port here")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        with tempfile.TemporaryDirectory() as d:
            p = subprocess.Popen([_chrome(), "--headless=new", "--disable-gpu", "--no-first-run", f"--user-data-dir={d}/b",
                                  f"http://127.0.0.1:{srv.server_address[1]}/"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                done.wait(40)
            finally:
                p.kill()
                p.wait()
                srv.shutdown()
                srv.server_close()
        if not got or any(isinstance(x, str) and x.startswith("ERR") for x in got["r"]):
            self.skipTest(f"no BarcodeDetector in this Chrome: {got}")
        self.assertEqual(got["r"], [[t] for t in texts])


class DbCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        os.environ["MAXPM_DB"] = os.path.join(self.dir.name, "t.db")
        self.addCleanup(os.environ.pop, "MAXPM_DB", None)
        self.c = core.connect()
        self.addCleanup(self.c.close)


class Status(DbCase):
    def test_read_self_and_the_steps_of_status(self):
        f = use(self, Fake())
        self.assertEqual(tailnet.read_self(), {"name": NAME, "login": "mark@example.com", "running": True})
        st = tailnet.status(self.c)
        self.assertEqual((st["installed"], st["running"], st["serving"], st["url"], st["logins"]),
                         (True, True, False, f"https://{NAME}/", ["mark@example.com"]))
        self.assertEqual(st["command"], f"tailscale serve --bg {core.serve_port_now(self.c)}")
        tailnet.serve_on(self.c, "mark")
        self.assertIn(["serve", "--bg", str(core.serve_port_now(self.c))], f.calls)
        self.assertTrue(tailnet.status(self.c)["serving"])
        use(self, Fake(status=None))
        self.assertIsNone(tailnet.read_self())

    def test_maxpm_phone_prints_the_next_step_or_the_address_and_code(self):
        from river import cli
        f = use(self, Fake())

        def run(*words):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                cli.run(["-q", "phone", *words])
            return out.getvalue()
        self.assertIn(f"tailscale serve --bg {core.serve_port_now(self.c)}", run())
        got = run("--on")
        self.assertTrue(f.serving)
        self.assertIn(f"https://{NAME}/", got)
        self.assertIn("█", got)
        self.assertIn("mark@example.com", got)

    def test_tailnet_logins_is_a_comma_list(self):
        core.config_set(self.c, "tailnet_logins", "mark@example.com, mark@github")
        with self.assertRaisesRegex(core.RiverError, "tailnet_logins is a comma list"):
            core.config_set(self.c, "tailnet_logins", "two words")


class Refusal(DbCase):
    TS = {"Host": NAME, "Tailscale-User-Login": "mark@example.com", "X-Forwarded-For": "100.64.0.2",
          "X-Forwarded-Proto": "https"}

    def why(self, headers, what="maxpm serve", ip="127.0.0.1"):
        return mcp.local_refusal(ip, headers, what)

    def test_a_request_of_an_allowed_login_at_this_computers_name_passes(self):
        use(self, Fake())
        self.assertIsNone(self.why(self.TS))
        self.assertIsNone(self.why({**self.TS, "Origin": f"https://{NAME}"}))
        self.assertIsNone(self.why({"Host": "127.0.0.1:8765", "X-Forwarded-Host": NAME,
                                    "Tailscale-User-Login": "MARK@example.com"}))

    def test_everything_else_is_refused(self):
        use(self, Fake())
        for headers, what, ip, why in (
                ({**self.TS, "Tailscale-User-Login": "eve@example.com"}, "maxpm serve", "127.0.0.1", "may not use"),
                ({**self.TS, "Tailscale-User-Login": ""}, "maxpm serve", "127.0.0.1", "may not use"),
                ({**self.TS, "Host": "evil.example"}, "maxpm serve", "127.0.0.1", f"only at {NAME}"),
                ({**self.TS, "Host": "127.0.0.1:8765"}, "maxpm serve", "127.0.0.1", f"only at {NAME}"),
                ({**self.TS, "X-Forwarded-Host": "evil.example"}, "maxpm serve", "127.0.0.1", f"only at {NAME}"),
                ({**self.TS, "Cf-Ray": "x"}, "maxpm serve", "127.0.0.1", "another proxy"),
                ({**self.TS, "Origin": "https://evil.example"}, "maxpm serve", "127.0.0.1", "cross-origin"),
                (self.TS, "/mcp", "127.0.0.1", "only on this computer"),
                (self.TS, "maxpm serve", "100.64.0.2", "only this computer"),
                # No login: Funnel (public) and a tagged device send none, so it is any proxy's request.
                ({k: v for k, v in self.TS.items() if k != "Tailscale-User-Login"}, "maxpm serve", "127.0.0.1",
                 "tunnel or proxy")):
            got = self.why(headers, what, ip)
            self.assertIsNotNone(got, headers)
            self.assertIn(why, got, headers)
        use(self, Fake(status=None))
        self.assertIn("not known", self.why(self.TS))

    def test_tailnet_logins_names_who_else_may_use_it(self):
        use(self, Fake())
        core.config_set(self.c, "tailnet_logins", "sam@example.com")
        tailnet.forget()
        self.assertIsNone(self.why({**self.TS, "Tailscale-User-Login": "sam@example.com"}))
        self.assertIn("may not use", self.why(self.TS))  # the owner is no longer on the list

    def test_the_page_and_its_actions_answer_over_tailscale(self):
        use(self, Fake())
        core.register(self.c, "mark", human=True)
        core.register(self.c, "ag")

        def call(method, path, headers, body=None):
            raw = json.dumps(body).encode() if body is not None else b""
            h = server.Handler.__new__(server.Handler)
            h.path, h.client_address, h.rfile, out = path, ("127.0.0.1", 5555), io.BytesIO(raw), {}
            h.headers = {"Content-Length": str(len(raw)), **headers}
            h._send = lambda code, body, ctype=None: out.update(code=code, body=body)
            getattr(h, "do_" + method)()
            return out["code"], out["body"]
        note = {"op": "send", "actor": "mark", "args": {"kind": "note", "body": "hello", "to": "ag"}}
        self.assertEqual(call("GET", "/api/state", self.TS)[0], 200)
        self.assertEqual(call("POST", "/api/action", {**self.TS, "Origin": f"https://{NAME}"}, note)[0], 200)
        code, body = call("POST", "/api/action", {**self.TS, "Tailscale-User-Login": "eve@example.com",
                                                   "Origin": f"https://{NAME}"}, note)
        self.assertEqual(code, 403)
        self.assertEqual(len(core.inbox(self.c, "ag", True, mark_read=False)), 1)


class SetupStep(Base):
    """Your phone in the setup guide, at desktop and phone width (p57): the address and a QR code that fits."""

    DRIVER = DRIVE_HEAD + r"""
try {
  await until(() => !q("#setup").classList.contains("hidden") && q("#setupSteps details.more"), "the setup guide");
  q("#setupSteps details.more").open = true;
  const step = [...document.querySelectorAll("#setupSteps .step")].find(s => /Your phone/.test(s.textContent));
  check(step, "the step Your phone");
  const on = step.querySelector('[data-su="tailnet"]');
  check(on, "Turn it on");
  on.click();
  await until(() => q("#setupSteps .su-qr svg"), "the QR code after Turn it on");
  const svg = q("#setupSteps .su-qr svg").getBoundingClientRect(), box = q("#setup .box").getBoundingClientRect();
  post({ link: q("#setupSteps .su-qr").closest(".step").querySelector("a").textContent, w: svg.width, right: svg.right,
         boxRight: box.right, wide: document.documentElement.scrollWidth <= innerWidth });
} catch (e) { post({ error: String(e.stack || e) }); }
"""

    def run_at(self, size):
        f = use(self, Fake())
        got = drive_page(self, self.DRIVER, size=size)
        self.assertTrue(f.serving)
        self.assertEqual(got["link"], f"https://{NAME}/")
        self.assertGreater(got["w"], 100)
        self.assertLessEqual(got["right"], got["boxRight"] + 1, got)
        self.assertTrue(got["wide"])

    def test_desktop(self):
        self.run_at("1500,900")

    def test_phone(self):
        self.run_at("390,844")


if __name__ == "__main__":
    unittest.main()
