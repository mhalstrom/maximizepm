"""Tests for the 'app' notice channel in river/notify.py (#6469)."""
import base64
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.parse
from unittest import mock

from river import core, notify
from tests.test_tailnet import Fake, NAME, use


class ChaCha20Poly1305Vectors(unittest.TestCase):
    """Verify our stdlib-only RFC 8439 ChaCha20-Poly1305 implementation against the RFC and Node.js."""

    def test_rfc8439_section_2_8_2_vector(self):
        # Official RFC 8439 Section 2.8.2 test vector.
        key = bytes.fromhex(
            "808182838485868788898a8b8c8d8e8f"
            "909192939495969798999a9b9c9d9e9f"
        )
        nonce = bytes.fromhex("070000004041424344454647")
        aad = bytes.fromhex("50515253c0c1c2c3c4c5c6c7")
        plaintext = (
            b"Ladies and Gentlemen of the class of '99: "
            b"If I could offer you only one tip for the future, sunscreen would be it."
        )
        want_ct = bytes.fromhex(
            "d31a8d34648e60db7b86afbc53ef7ec2"
            "a4aded51296e08fea9e2b5a736ee62d6"
            "3dbea45e8ca9671282fafb69da92728b"
            "1a71de0a9e060b2905d6a5b67ecd3b36"
            "92ddbd7f2d778b8c9803aee328091b58"
            "fab324e4fad675945585808b4831d7bc"
            "3ff4def08e4b7a9de576d26586cec64b"
            "6116"
        )
        want_tag = bytes.fromhex("1ae10b594f09e26a7e902ecbd0600691")

        ct, tag = notify.chacha20_poly1305_encrypt(key, nonce, plaintext, aad=aad)
        self.assertEqual(ct, want_ct)
        self.assertEqual(tag, want_tag)

        got = notify.chacha20_poly1305_decrypt(key, nonce, ct, tag, aad=aad)
        self.assertEqual(got, plaintext)

        # Tampering with ciphertext, tag, or AAD fails authentication.
        with self.assertRaises(ValueError):
            notify.chacha20_poly1305_decrypt(key, nonce, bytes([ct[0] ^ 1]) + ct[1:], tag, aad=aad)
        with self.assertRaises(ValueError):
            notify.chacha20_poly1305_decrypt(key, nonce, ct, bytes([tag[0] ^ 1]) + tag[1:], aad=aad)
        with self.assertRaises(ValueError):
            notify.chacha20_poly1305_decrypt(key, nonce, ct, tag, aad=aad + b"\x00")

    def test_encrypt_decrypt_notice_roundtrip_and_limits(self):
        key_std = base64.b64encode(bytes(range(32))).decode("ascii")
        key_url = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")

        ct1 = notify.encrypt_notice(key_std, "MaximizePM: review", "Ready to check — café", "https://example.ts.net/sessions/a")
        ct2 = notify.encrypt_notice(key_url, "MaximizePM: review", "Ready to check — café", "https://example.ts.net/sessions/a")
        # Random nonces produce distinct ciphertexts.
        self.assertNotEqual(ct1, ct2)
        self.assertEqual(notify.decrypt_notice(key_url, ct1), {
            "title": "MaximizePM: review",
            "body": "Ready to check — café",
            "url": "https://example.ts.net/sessions/a",
        })
        self.assertEqual(notify.decrypt_notice(key_std, ct2)["title"], "MaximizePM: review")

        # Oversized body is truncated so ciphertext stays under the push Worker's 3500-byte limit.
        huge = "x" * 10000
        ct_big = notify.encrypt_notice(key_std, "Title", huge, "https://example.ts.net/")
        self.assertLessEqual(len(ct_big.encode("utf-8")), 3500)
        dec = notify.decrypt_notice(key_std, ct_big)
        self.assertEqual(dec["title"], "Title")
        self.assertTrue(dec["body"].endswith("…"))

        # Wrong key size is rejected.
        with self.assertRaises(ValueError):
            notify.encrypt_notice(base64.b64encode(b"short").decode("ascii"), "t", "b", "")

    def test_node_crypto_can_decrypt_our_envelope(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        key_b64 = base64.b64encode(os.urandom(32)).decode("ascii")
        ct_b64 = notify.encrypt_notice(key_b64, "Hello phone", "Approve #12", "https://mac.ts.net/sessions/w")
        js = (
            "const crypto = require('crypto');"
            "const key = Buffer.from(process.argv[1], 'base64');"
            "const raw = Buffer.from(process.argv[2], 'base64');"
            "if (raw[0] !== 1) throw new Error('bad version');"
            "const nonce = raw.subarray(1, 13);"
            "const ct = raw.subarray(13, raw.length - 16);"
            "const tag = raw.subarray(raw.length - 16);"
            "const d = crypto.createDecipheriv('chacha20-poly1305', key, nonce, { authTagLength: 16 });"
            "d.setAAD(raw.subarray(0, 1));"
            "d.setAuthTag(tag);"
            "const out = Buffer.concat([d.update(ct), d.final()]).toString('utf8');"
            "process.stdout.write(out);"
        )
        r = subprocess.run([node, "-e", js, key_b64, ct_b64], capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {
            "title": "Hello phone", "body": "Approve #12", "url": "https://mac.ts.net/sessions/w"
        })


class _FakeResp:
    def __init__(self, status, body):
        self.status = status
        self._raw = json.dumps(body).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakePushWorker:
    """In-process mock of push-worker/src/index.js POST /push via urllib.request.urlopen."""

    def __init__(self, respond):
        self.requests = []
        self.respond = respond
        self.url = "https://push.test.invalid"
        self._patch = mock.patch("urllib.request.urlopen", side_effect=self._urlopen)
        self._patch.start()

    def _urlopen(self, req, timeout=None):
        parsed = urllib.parse.urlsplit(req.full_url)
        body = json.loads((req.data or b"{}").decode("utf-8"))
        self.requests.append((parsed.path, body))
        code, resp = self.respond(parsed.path, body)
        if code >= 400:
            raw = json.dumps(resp).encode("utf-8")
            raise urllib.error.HTTPError(req.full_url, code, "error", {}, io.BytesIO(raw))
        return _FakeResp(code, resp)

    def close(self):
        self._patch.stop()


class AppChannelTests(unittest.TestCase):
    KEY1 = base64.b64encode(b"1" * 32).decode("ascii")
    KEY2 = base64.b64encode(b"2" * 32).decode("ascii")

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        os.environ["MAXPM_DB"] = os.path.join(self.dir.name, "t.db")
        self.addCleanup(os.environ.pop, "MAXPM_DB", None)
        self.c = core.connect()
        self.addCleanup(self.c.close)

    def _start_worker(self, respond):
        w = FakePushWorker(respond)
        self.addCleanup(w.close)
        core.config_set(self.c, "notify_app_url", w.url)
        return w

    def test_notify_app_url_validation_and_pair_enables_app_channel(self):
        with self.assertRaisesRegex(core.RiverError, "notify_app_url"):
            core.config_set(self.c, "notify_app_url", "not-a-url")
        core.config_set(self.c, "notify_app_url", "https://push.example.com")
        self.assertEqual(core.setting(self.c, "notify_app_url"), "https://push.example.com")

        self.assertEqual(core._channels(core.setting(self.c, "notify_channels")), [])
        core.device_pair(self.c, "Mark's iPhone", "ios", "apns-1", self.KEY1)
        self.assertIn("app", core._channels(core.setting(self.c, "notify_channels")))

    def test_sends_encrypted_notices_to_each_paired_device_and_rewrites_local_url(self):
        use(self, Fake(serving=True))
        w = self._start_worker(lambda path, body: (200, {"ok": True, "platform": body["platform"], "provider_status": 200}))
        d1 = core.device_pair(self.c, "iPhone", "ios", "apns-tok-1", self.KEY1)
        d2 = core.device_pair(self.c, "Pixel", "android", "fcm-tok-2", self.KEY2)

        res = notify.test(self.c, "app")
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(w.requests), 2)

        path1, req1 = w.requests[0]
        path2, req2 = w.requests[1]
        self.assertEqual(path1, "/push")
        self.assertEqual(req1["platform"], "ios")
        self.assertEqual(req1["token"], "apns-tok-1")
        self.assertEqual(path2, "/push")
        self.assertEqual(req2["platform"], "android")
        self.assertEqual(req2["token"], "fcm-tok-2")

        # Each device decrypts with its own key, and not the other device's key.
        dec1 = notify.decrypt_notice(self.KEY1, req1["ciphertext"])
        dec2 = notify.decrypt_notice(self.KEY2, req2["ciphertext"])
        self.assertEqual(dec1["title"], "MaximizePM: test")
        self.assertEqual(dec2["title"], "MaximizePM: test")
        self.assertTrue(dec1["url"].startswith(f"https://{NAME}/"), dec1["url"])
        with self.assertRaises(ValueError):
            notify.decrypt_notice(self.KEY2, req1["ciphertext"])

        # Neither device is marked dead.
        devs = {d["id"]: d for d in core.device_list(self.c)}
        self.assertIsNone(devs[d1["id"]]["dead_at"])
        self.assertIsNone(devs[d2["id"]]["dead_at"])

    def test_dead_token_410_marks_device_dead_and_skips_it_next_time(self):
        def respond(path, body):
            if body["token"] == "dead-tok":
                return 410, {"ok": False, "platform": body["platform"], "provider_status": 410,
                             "dead_token": True, "reason": "Unregistered"}
            return 200, {"ok": True, "platform": body["platform"], "provider_status": 200}

        w = self._start_worker(respond)
        d_ok = core.device_pair(self.c, "iPhone", "ios", "good-tok", self.KEY1)
        d_dead = core.device_pair(self.c, "Old Pixel", "android", "dead-tok", self.KEY2)

        # First send: both attempted; good succeeds, dead is marked dead, batch succeeds (no duplicate to good).
        res = notify.test(self.c, "app")
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(w.requests), 2)
        devs = {d["id"]: d for d in core.device_list(self.c)}
        self.assertIsNone(devs[d_ok["id"]]["dead_at"])
        self.assertIsNotNone(devs[d_dead["id"]]["dead_at"])

        # Second send: dead device is skipped; only healthy device receives a POST.
        w.requests.clear()
        res2 = notify.test(self.c, "app")
        self.assertTrue(res2["ok"], res2)
        self.assertEqual(len(w.requests), 1)
        self.assertEqual(w.requests[0][1]["token"], "good-tok")

    def test_all_dead_or_no_devices_reports_error(self):
        w = self._start_worker(lambda path, body: (410, {"ok": False, "dead_token": True, "reason": "Unregistered"}))
        # No devices paired yet -> RiverError.
        with self.assertRaisesRegex(core.RiverError, "no active paired phone"):
            notify.test(self.c, "app")

        d = core.device_pair(self.c, "iPhone", "ios", "apns-dead", self.KEY1)
        res = notify.test(self.c, "app")
        self.assertFalse(res["ok"])
        self.assertIn("expired push tokens", res["error"])
        self.assertIsNotNone(core.device_list(self.c)[0]["dead_at"])
        self.assertEqual(len(w.requests), 1)

        # Subsequent call sees no active devices and does not hit the Worker again.
        with self.assertRaisesRegex(core.RiverError, "no active paired phone"):
            notify.test(self.c, "app")
        self.assertEqual(len(w.requests), 1)
        self.assertEqual(core.device_list(self.c)[0]["id"], d["id"])

    def test_transient_worker_error_does_not_mark_dead_and_retries_in_outbox(self):
        status = {"code": 502}
        w = self._start_worker(lambda path, body: (
            status["code"],
            {"ok": True} if status["code"] == 200 else {"ok": False, "dead_token": False, "reason": "provider_unreachable"},
        ))
        core.register(self.c, "mark", human=True)
        core.project_add(self.c, "p1")
        core.device_pair(self.c, "iPhone", "ios", "apns-1", self.KEY1)
        core.item_add(self.c, "p1", "Approve the release", doer="human")

        # First notify.run fails with 502 -> outbox row stays failed (retryable), device NOT dead.
        r1 = notify.run(self.c, now_=True)
        self.assertEqual(len(r1), 1)
        self.assertFalse(r1[0]["sent"])
        self.assertIn("502", r1[0]["error"])
        self.assertIsNone(core.device_list(self.c)[0]["dead_at"])

        # Worker recovers -> next notify.run delivers and marks outbox row sent.
        status["code"] = 200
        r2 = notify.run(self.c, now_=True)
        self.assertEqual(len(r2), 1)
        self.assertTrue(r2[0]["sent"], r2)
        self.assertEqual(len(w.requests), 2)
        dec = notify.decrypt_notice(self.KEY1, w.requests[1][1]["ciphertext"])
        self.assertIn("Approve the release", dec["body"])


if __name__ == "__main__":
    unittest.main()
