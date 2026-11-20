"""Phone pairing in maxpm serve (#6471, plan: canvas phone-app-plan):
- 10-minute pairing code in the setup guide's Your phone QR
- POST /api/pair stores the device in the devices table (name, platform, push_token, key, last_seen)
- Settings > Notifications lists paired phones and Remove deletes the row (desktop and 390 px phone width, p57).
"""
import io
import json
import os
import tempfile
import unittest

from river import core, server, tailnet
from tests.test_tailnet import Fake, NAME, use
from tests.test_view_sessions import Base, drive_page, DRIVE_HEAD


class DbCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        os.environ["MAXPM_DB"] = os.path.join(self.dir.name, "t.db")
        self.addCleanup(os.environ.pop, "MAXPM_DB", None)
        self.c = core.connect()
        self.addCleanup(self.c.close)
        server.clear_pair_codes()
        self.addCleanup(server.clear_pair_codes)


class DevicesCore(DbCase):
    KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="

    def test_pair_list_touch_dead_and_remove(self):
        d = core.device_pair(self.c, "Mark's iPhone", "ios", "apns-tok-1", self.KEY)
        self.assertEqual(d["name"], "Mark's iPhone")
        self.assertEqual(d["platform"], "ios")
        self.assertNotIn("key", d)
        self.assertNotIn("push_token", d)
        self.assertIsNone(d["dead_at"])

        pub = core.device_list(self.c)
        self.assertEqual(len(pub), 1)
        self.assertNotIn("key", pub[0])
        self.assertNotIn("push_token", pub[0])

        sec = core.device_list(self.c, include_secrets=True)
        self.assertEqual(sec[0]["push_token"], "apns-tok-1")
        self.assertEqual(sec[0]["key"], self.KEY)

        # State exposes public device info only.
        st = core.state(self.c)
        self.assertEqual(len(st["devices"]), 1)
        self.assertNotIn("key", st["devices"][0])

        # Mark dead, then re-pairing same (platform, push_token) updates row and clears dead_at.
        core.device_mark_dead(self.c, d["id"])
        self.assertIsNotNone(core.device_list(self.c)[0]["dead_at"])
        new_key = "ZmVkY2JhOTg3NjU0MzIxMGZlZGNiYTk4NzY1NDMyMTA="
        d2 = core.device_pair(self.c, "", "ios", "apns-tok-1", new_key)
        self.assertEqual(d2["id"], d["id"])
        self.assertEqual(d2["name"], "iPhone")
        self.assertIsNone(d2["dead_at"])
        self.assertEqual(core.device_list(self.c, include_secrets=True)[0]["key"], new_key)

        # Touch updates last_seen and clears dead_at.
        core.device_mark_dead(self.c, d["id"])
        core.device_touch(self.c, d["id"])
        self.assertIsNone(core.device_list(self.c)[0]["dead_at"])

        # Remove deletes the row.
        res = core.device_remove(self.c, d["id"])
        self.assertTrue(res["removed"])
        self.assertEqual(core.device_list(self.c), [])
        with self.assertRaisesRegex(core.RiverError, "no paired device"):
            core.device_remove(self.c, d["id"])

    def test_validation_rejects_bad_inputs(self):
        with self.assertRaisesRegex(core.RiverError, "platform"):
            core.device_pair(self.c, "x", "windows", "tok", self.KEY)
        with self.assertRaisesRegex(core.RiverError, "push_token"):
            core.device_pair(self.c, "x", "android", "", self.KEY)
        with self.assertRaisesRegex(core.RiverError, "key"):
            core.device_pair(self.c, "x", "android", "tok", "short")
        with self.assertRaisesRegex(core.RiverError, "key"):
            core.device_pair(self.c, "x", "android", "tok", "has spaces in the key string")

    def test_migrate_creates_devices_table_on_older_db(self):
        self.c.execute("DROP TABLE devices")
        self.c.commit()
        core._migrate(self.c)
        self.c.commit()
        cols = {r["name"] for r in self.c.execute("PRAGMA table_info(devices)")}
        self.assertTrue({"id", "name", "platform", "push_token", "key", "paired_at", "last_seen", "dead_at"} <= cols)


class PairingHttp(DbCase):
    TS = {"Host": NAME, "Tailscale-User-Login": "mark@example.com", "X-Forwarded-For": "100.64.0.2",
          "X-Forwarded-Proto": "https"}
    KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="

    def call(self, method, path, headers, body=None):
        raw = json.dumps(body).encode() if body is not None else b""
        h = server.Handler.__new__(server.Handler)
        h.path, h.client_address, h.rfile, out = path, ("127.0.0.1", 5555), io.BytesIO(raw), {}
        h.headers = {"Content-Length": str(len(raw)), **headers}
        h._send = lambda code, b, ctype=None: out.update(code=code, body=b)
        getattr(h, "do_" + method)()
        return out["code"], out["body"]

    def test_pair_code_ttl_and_single_use(self):
        code, exp = server.issue_pair_code(now_mono=1000.0)
        self.assertTrue(code and exp)
        # Expired at +600s.
        self.assertFalse(server.consume_pair_code(code, now_mono=1600.0))
        # Fresh code succeeds once, then fails on reuse.
        code2, _ = server.issue_pair_code(now_mono=2000.0)
        self.assertTrue(server.consume_pair_code(code2, now_mono=2599.0))
        self.assertFalse(server.consume_pair_code(code2, now_mono=2599.5))

    def test_setup_status_embeds_pair_code_in_qr_and_post_pair_stores_device(self):
        use(self, Fake(serving=True))
        st = server.setup_status(self.c)["tailnet"]
        code = st["pair_code"]
        self.assertTrue(code)
        self.assertEqual(st["pair_url"], f"https://{NAME}/?pair={code}")
        self.assertIn(f"?pair={code}", st["qr"])

        # Bad payload does not burn the code.
        rc, body = self.call("POST", "/api/pair", self.TS,
                             {"code": code, "platform": "web", "push_token": "t1", "key": self.KEY})
        self.assertEqual(rc, 400, body)

        # Valid request over Tailscale pairs the device.
        rc, body = self.call("POST", "/api/pair", self.TS,
                             {"code": code, "platform": "ios", "push_token": "apns-1", "key": self.KEY,
                              "name": "Mark's iPhone"})
        self.assertEqual(rc, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["device"]["name"], "Mark's iPhone")
        self.assertNotIn("key", body["device"])

        # Reusing the same code is refused.
        rc, _ = self.call("POST", "/api/pair", self.TS,
                          {"code": code, "platform": "ios", "push_token": "apns-2", "key": self.KEY})
        self.assertEqual(rc, 403)

        # Disallowed Tailscale login is refused before pairing.
        code2, _ = server.issue_pair_code()
        rc, _ = self.call("POST", "/api/pair", {**self.TS, "Tailscale-User-Login": "eve@example.com"},
                          {"code": code2, "platform": "android", "push_token": "fcm-1", "key": self.KEY})
        self.assertEqual(rc, 403)
        self.assertEqual(len(core.device_list(self.c)), 1)

    def test_device_remove_action_requires_person(self):
        core.register(self.c, "mark", human=True)
        core.register(self.c, "worker-1")
        d = core.device_pair(self.c, "Pixel", "android", "fcm-1", self.KEY)

        # An agent session (no Origin, non-human) is refused.
        rc, body = self.call("POST", "/api/action", {"Host": "127.0.0.1:8765"},
                             {"op": "device_remove", "actor": "worker-1", "args": {"id": d["id"]}})
        self.assertEqual(rc, 409, body)
        self.assertIn("refused", body["error"])
        self.assertEqual(len(core.device_list(self.c)), 1)

        # The page (person) removes it.
        rc, body = self.call("POST", "/api/action",
                             {"Host": "127.0.0.1:8765", "Origin": "http://127.0.0.1:8765"},
                             {"op": "device_remove", "actor": "mark", "args": {"id": d["id"]}})
        self.assertEqual(rc, 200, body)
        self.assertEqual(core.device_list(self.c), [])


class PairingPage(Base):
    """Setup guide QR with 10-minute pairing code, and Settings > Notifications paired devices + Remove,
    at desktop (1500x900) and phone (390x844) widths (p57)."""

    KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="

    DRIVER = DRIVE_HEAD + r"""
try {
  // 1. Setup guide > Your phone shows QR with pairing code note and New code button.
  await until(() => !q("#setup").classList.contains("hidden") && q("#setupSteps details.more"), "the setup guide");
  q("#setupSteps details.more").open = true;
  await until(() => q("#setupSteps .su-qr svg") && q("#setupSteps .su-pair"), "QR and pairing note");
  const qrLabel = q("#setupSteps .su-qr svg").getAttribute("aria-label") || "";
  const pairText = q("#setupSteps .su-pair").textContent.trim();
  const setupWide = document.documentElement.scrollWidth <= innerWidth;
  // Close setup guide.
  q("#setupClose").click();
  await until(() => q("#setup").classList.contains("hidden"), "setup closed");

  // 2. Open Settings > Notifications, verify paired devices, click Remove.
  q("#hdrMore").click();
  await until(() => q("#settingsBtn"), "settings menu item");
  q("#settingsBtn").click();
  await until(() => !q("#tab-settings").classList.contains("hidden"), "settings tab");
  const notifTab = q('[data-set-tab="notifications"]');
  check(notifTab, "Notifications tab");
  notifTab.click();
  await until(() => q("#setDevices .dev-row"), "paired device row");
  const before = [...document.querySelectorAll("#setDevices .dev-row")].map(r => r.textContent.trim());
  const settingsWide = document.documentElement.scrollWidth <= innerWidth;
  q('#setDevices [data-device-remove]').click();
  await until(() => document.querySelectorAll("#setDevices .dev-row").length === 1, "one device left after Remove");
  const after = [...document.querySelectorAll("#setDevices .dev-row")].map(r => r.textContent.trim());
  post({ qrLabel, pairText, setupWide, settingsWide, before, after });
} catch (e) { post({ error: String(e.stack || e) }); }
"""

    def run_at(self, size):
        use(self, Fake(serving=True))
        c = core.connect()
        try:
            d1 = core.device_pair(c, "Mark's iPhone", "ios", "apns-1", self.KEY)
            d2 = core.device_pair(c, "Pixel 9", "android", "fcm-1", self.KEY)
            core.device_mark_dead(c, d2["id"])
        finally:
            c.close()
        got = drive_page(self, self.DRIVER, size=size)
        self.assertIn("?pair=", got["qrLabel"])
        self.assertIn("10 minutes", got["pairText"])
        self.assertTrue(got["setupWide"], got)
        self.assertTrue(got["settingsWide"], got)
        self.assertEqual(len(got["before"]), 2)
        self.assertIn("Mark's iPhone", got["before"][0])
        self.assertIn("iPhone", got["before"][0])
        self.assertIn("Pixel 9", got["before"][1])
        self.assertIn("push token expired", got["before"][1])
        self.assertEqual(len(got["after"]), 1)
        self.assertIn("Pixel 9", got["after"][0])
        c = core.connect()
        try:
            self.assertEqual([d["id"] for d in core.device_list(c)], [d2["id"]])
        finally:
            c.close()

    def test_desktop(self):
        self.run_at("1500,900")

    def test_phone(self):
        self.run_at("390,844")


if __name__ == "__main__":
    unittest.main()
