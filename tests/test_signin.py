"""Browser sign-in fill (#5826): a granted sign-in fills a session's browser by itself, once, on the real site only."""

import base64
import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

from river import cli, core, signin, vault
from river.core import RiverError

PNG = base64.b64encode(b"\x89PNG fake").decode()


def page(url, pw=False, otp=False, user=False, user_value="", robot=False, said=""):
    return {"url": url, "pw": pw, "otp": otp, "user": user, "user_value": user_value, "robot": robot, "said": said}


class FakeBrowser:
    """The pages of a sign-in, one after the other: Enter goes to the next."""

    def __init__(self, pages, owner="dev"):
        self.pages, self.i = pages, 0
        self.owner, self.project, self.state = owner, "site", {}
        self.typed, self.shots, self.field_values = [], 0, []

    def running(self):
        return True

    def evaluate(self, expr, timeout=None):
        if expr == signin.FIND_JS:
            return self.pages[min(self.i, len(self.pages) - 1)]
        if expr == signin.VALUES_JS:
            return list(self.field_values)
        if expr.startswith(signin.FOCUS_JS.split("%s")[0]):
            return True
        raise AssertionError(expr)

    def page(self, method, params=None, timeout=None):
        if method == "Input.insertText":
            self.typed.append(params["text"])
        elif method == "Input.dispatchKeyEvent" and params.get("key") == "Enter" and params["type"] != "keyUp":
            self.i += 1
        elif method == "Page.captureScreenshot":
            self.shots += 1
            return {"data": PNG}
        return {}


class SignIn(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "t.db")
        env = mock.patch.dict(os.environ, {"MAXPM_DB": self.path})
        env.start()
        self.addCleanup(env.stop)
        self.c = core.connect(self.path)
        self.addCleanup(self.c.close)
        old = vault.BACKEND
        vault.BACKEND = vault.Files()
        self.addCleanup(setattr, vault, "BACKEND", old)
        for k, v in (("POLL", 0), ("SUBMIT_WAIT", 0.01)):
            p = mock.patch.object(signin, k, v)
            p.start()
            self.addCleanup(p.stop)
        core.project_add(self.c, "site")
        core.register(self.c, "mark", human=True)
        core.register(self.c, "dev")
        vault.signin_save(self.c, "gh", "https://www.github.com/login", "mark-gh", b"hunter2-pw")
        vault.set_change(self.c, "gh-set", add=["gh"], actor="mark")
        iid = core.item_add(self.c, "site", "release notes", actor="t")["id"]
        core.claim(self.c, iid, "dev")
        self.item = iid

    def grant(self):
        vault.grant(self.c, "gh-set", "project", "site", actor="mark")

    def test_account_then_password_on_the_real_site(self):
        self.grant()
        b = FakeBrowser([page("https://github.com/login", user=True), page("https://github.com/session", pw=True),
                         page("https://github.com/")])
        self.assertEqual(signin.check(b, self.c), ["account", "password"])
        self.assertEqual(b.typed, ["mark-gh", "hunter2-pw"])
        self.assertIn("browser sign-in: password on github.com", vault.log(self.c, "gh")["log"][0]["detail"])
        self.assertEqual(signin.note(self.c, "dev", "github.com").split(":")[0],
                         "MaximizePM signs this browser in as mark-gh on github.com by itself (sign-in gh)")

    def test_no_grant_and_look_alike_and_plain_http_get_nothing(self):
        b = FakeBrowser([page("https://github.com/login", pw=True, user=True)])
        self.assertEqual(signin.check(b, self.c), [])
        self.grant()
        for url in ("https://github.com.evil.example/login", "http://github.com/login", "https://notgithub.com/login"):
            b = FakeBrowser([page(url, pw=True, user=True)])
            self.assertEqual(signin.check(b, self.c), [], url)
            self.assertEqual(b.typed, [])
        b = FakeBrowser([page("https://github.com/login", pw=True, user=True)], owner=None)
        signin.soon(b, "https://github.com/login")  # the project's own browser (page walks): no fill
        self.assertEqual(b.typed, [])

    def test_a_refused_password_is_one_try_and_one_alert_with_a_screenshot(self):
        self.grant()
        bad = page("https://github.com/session", pw=True, said="Incorrect username or password.")
        b = FakeBrowser([page("https://github.com/login", pw=True, user=True), bad])
        res = signin.check(b, self.c)
        self.assertEqual(b.typed, ["mark-gh", "hunter2-pw"])  # once
        self.assertEqual(b.shots, 1)
        r = self.c.execute("SELECT fail_why FROM secrets WHERE name='gh'").fetchone()
        self.assertEqual(r["fail_why"], "Incorrect username or password.")
        msg = self.c.execute("SELECT * FROM messages WHERE kind='alert' ORDER BY id DESC").fetchone()
        self.assertEqual(msg["to_agent"], "mark")  # no manager is active
        self.assertIn("tries no more", msg["body"])
        self.assertIn("--reset", msg["body"])
        shots = os.listdir(os.path.join(self.dir.name, "signin"))
        self.assertEqual(len(shots), 1)
        b2 = FakeBrowser([page("https://github.com/login", pw=True, user=True)])
        self.assertEqual(signin.check(b2, self.c), [])  # no fill until a person resets it
        self.assertEqual(b2.typed, [])
        with self.assertRaises(RiverError):
            vault.signin_save(self.c, "gh", reset=True, actor="dev")
        vault.signin_save(self.c, "gh", reset=True, actor="mark")
        self.assertEqual(signin.check(FakeBrowser([page("https://github.com/login", pw=True), page("https://github.com/")]),
                                      self.c), ["password"])

    def test_robot_check_types_nothing(self):
        self.grant()
        b = FakeBrowser([page("https://github.com/login", pw=True, robot=True)])
        self.assertEqual(signin.check(b, self.c)["failed"], "the page shows a robot check")
        self.assertEqual(b.typed, [])

    def test_two_step_code_by_the_agents_or_by_the_person(self):
        self.grant()
        b = FakeBrowser([page("https://github.com/sessions/two-factor", otp=True)])
        res = signin.check(b, self.c)
        self.assertIn("the person passes that step", res["failed"])
        self.assertIsNone(self.c.execute("SELECT fail_at FROM secrets WHERE name='gh'").fetchone()[0])  # no failure
        self.assertEqual(b.typed, [])
        self.assertEqual(signin.check(b, self.c), {"waits": res["failed"]})  # one alert, not one per page load
        key = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
        vault.signin_save(self.c, "gh", totp=key, two_step="agents", actor="mark")
        b = FakeBrowser([page("https://github.com/sessions/two-factor", otp=True), page("https://github.com/")])
        with mock.patch.object(vault, "totp_code", lambda k: "287082"):
            self.assertEqual(signin.check(b, self.c), ["code"])
        self.assertEqual(b.typed, ["287082"])

    def test_totp_code_of_rfc_6238(self):
        key = vault.totp_key(b"GEZD GNBV GY3T QOJQ GEZD GNBV GY3T QOJQ")
        self.assertEqual(vault.totp_code(key, at=59, digits=8), "94287082")
        self.assertEqual(vault.totp_code(key, at=1111111109, digits=8), "07081804")
        self.assertEqual(vault.totp_key(b"otpauth://totp/x?secret=GEZDGNBV&issuer=x"), b"GEZDGNBV")
        with self.assertRaises(RiverError):
            vault.totp_key(b"not a key!")

    def test_scrub_takes_filled_and_field_values_out(self):
        b = FakeBrowser([])
        b.signin_values = {"hunter2-pw"}
        b.field_values = ["typed-by-person"]
        out = signin.scrub(b, {"value": "a hunter2-pw b", "rows": [{"x": "typed-by-person"}], "n": 3})
        self.assertEqual(out, {"value": "a *** b", "rows": [{"x": "***"}], "n": 3})

    def test_cli_saves_a_signin_with_a_key(self):
        out, err = io.StringIO(), io.StringIO()

        class Stdin:
            buffer = io.BytesIO(b"pw-123456\nGEZDGNBVGY3TQOJQ\n")

            def isatty(self):
                return False
        with mock.patch.object(sys, "stdin", Stdin()), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.run(["-q", "--as", "mark", "secret", "signin", "google-mark", "--site", "google.com",
                          "--account", "me@example.com", "--totp", "--two-step", "agents"])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("me@example.com on google.com", out.getvalue())
        self.assertIn("the agents pass a 2-step check", out.getvalue())
        self.assertEqual(vault.Files().get(self.c, "google-mark"), b"pw-123456")
        self.assertEqual(vault.Files().get(self.c, "google-mark#totp"), b"GEZDGNBVGY3TQOJQ")
        self.assertEqual([r["name"] for r in vault.signins_for(self.c, "accounts.google.com")], ["google-mark"])
        vault.delete(self.c, "google-mark")
        self.assertIsNone(vault.Files().get(self.c, "google-mark#totp"))


if __name__ == "__main__":
    unittest.main()
