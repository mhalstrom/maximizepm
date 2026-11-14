"""Browser sign-in fill (#5826): a granted sign-in fills a session's browser by itself, once, on the real site only."""

import base64
import contextlib
import io
import json
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
        self.signin_confirmed = True  # the owner's own requests were confirmed (#5823)

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


class Base(unittest.TestCase):
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



class SignIn(Base):
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

    def test_no_fill_while_the_owner_is_not_confirmed(self):
        self.grant()
        b = FakeBrowser([page("https://github.com/login", pw=True, user=True)])
        b.signin_confirmed = False  # its last request came under another session's name
        self.assertEqual(signin.check(b, self.c), [])
        self.assertEqual(b.typed, [])

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

    def test_scrub_text_cleans_a_cdp_message_and_reads_the_fields_seldom(self):
        # #5831: Runtime.evaluate('document.querySelector("input[type=password]").value') through a session's CDP
        # address returned the password that serve filled.
        b = FakeBrowser([])
        b.signin_values = {'pa"ss\\wörd'}
        b.field_values = ["typed-by-person"]
        calls = []
        real = b.evaluate
        b.evaluate = lambda expr, timeout=None: (calls.append(expr), real(expr, timeout))[1]
        filled = json.dumps({"id": 7, "result": {"result": {"type": "string", "value": 'pa"ss\\wörd'}}})
        self.assertNotIn("wörd", signin.scrub_text(b, filled))  # the JSON-escaped form
        raw = json.dumps({"id": 8, "result": {"value": 'pa"ss\\wörd'}}, ensure_ascii=False)
        self.assertEqual(json.loads(signin.scrub_text(b, raw))["result"]["value"], "***")  # Chrome's raw UTF-8
        ev = json.dumps({"method": "Network.requestWillBeSent", "params": {"postData": "pw=typed-by-person"}})
        self.assertEqual(json.loads(signin.scrub_text(b, ev))["params"]["postData"], "pw=***")
        self.assertEqual(len(calls), 1)  # one read of the fields for the three messages (LIVE_AGAIN)
        b.field_values = ["typed-later"]
        b.signin_live = (b.signin_live[0] - 0.5, b.signin_live[1])  # half a second later
        self.assertIn("typed-later", signin.scrub_text(b, '{"id":9,"result":"typed-later"}'))  # not read again yet
        self.assertEqual(signin.scrub_text(b, '{"id":10,"result":"typed-later"}', read=True),
                         '{"id":10,"result":"***"}')  # a read of the page: the fields are read again
        self.assertEqual(signin.scrub_text(FakeBrowser([]), '{"id":1,"result":"abc"}'), '{"id":1,"result":"abc"}')

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


class CookieBrowser(FakeBrowser):
    """A browser with a cookie jar: Storage.getCookies, Storage.setCookies, Network.deleteCookies."""

    def __init__(self, jar=(), pages=(), owner="dev"):
        super().__init__(list(pages) or [page("https://github.com/")], owner)
        self.jar = [dict(c) for c in jar]
        self.attached = False

    def call(self, method, params=None, session=None, timeout=None):
        if method == "Storage.getCookies":
            return {"cookies": [dict(c) for c in self.jar]}
        if method == "Storage.setCookies":
            for c in params["cookies"]:
                self.jar = [x for x in self.jar if (x["name"], x["domain"]) != (c["name"], c["domain"])] + [dict(c)]
            return {}
        raise AssertionError(method)

    def page(self, method, params=None, timeout=None):
        if method == "Network.deleteCookies":
            self.jar = [x for x in self.jar if (x["name"], x["domain"]) != (params["name"], params["domain"])]
            return {}
        return super().page(method, params, timeout)


GH = [{"name": "user_session", "value": "s3ss10n", "domain": "github.com", "path": "/", "secure": True},
      {"name": "_gh_sess", "value": "abc", "domain": ".github.com", "path": "/", "secure": True}]
OTHER = [{"name": "sid", "value": "x", "domain": "example.org", "path": "/"}]


class SavedSignIns(Base):
    """#5827: the cookies of one site in a set, loaded by grant, saved back after a sign-in, removed on revoke."""

    def save(self, cookies=GH):
        return vault.cookies_save(self.c, "gh-cookies", "github.com", cookies, "mark", "gh-set")

    def names(self, b):
        return sorted(c["name"] for c in b.jar)

    def test_a_grant_loads_the_sign_in_and_a_revoke_takes_it_out(self):
        self.save()
        b = CookieBrowser(OTHER)
        self.assertEqual(signin.enforce_cookies(b, self.c), {"loaded": [], "removed": ["gh-cookies"]})  # no grant
        self.assertEqual(self.names(b), ["sid"])
        g = vault.grant(self.c, "gh-set", "project", "site", actor="mark")
        self.assertEqual(signin.enforce_cookies(b, self.c)["loaded"], ["gh-cookies"])
        self.assertEqual(self.names(b), ["_gh_sess", "sid", "user_session"])
        self.assertIn("browser sign-in loaded (2 cookies of github.com)", vault.log(self.c, "gh-cookies")["log"][0]["detail"])
        self.assertEqual(signin.enforce_cookies(b, self.c), {"loaded": [], "removed": []})  # once
        vault.revoke(self.c, g["grant"], actor="mark")
        self.assertEqual(signin.enforce_cookies(b, self.c)["removed"], ["gh-cookies"])
        self.assertEqual(self.names(b), ["sid"])  # the other site's cookies stay

    def test_a_new_chrome_loads_the_sign_in_again(self):
        """#5840: realquicksite-c711 closed and opened its browser; the new Chrome had no Gmail cookies, and the load
        skipped them because the old Chrome's 'loaded' mark stayed. start() forgets the marks of the last Chrome."""
        from river import browser
        b = browser.Browser("site", "dev")
        b.cookie_loaded, b.cookie_checked = {"gh-cookies"}, True  # what the last Chrome loaded
        seen = []
        with mock.patch.object(browser, "setting_of", lambda *a, **k: None), \
                mock.patch.object(browser, "Proxy", lambda *a, **k: object()), \
                mock.patch.object(browser.Browser, "_attach_old", lambda self: True), \
                mock.patch.object(browser.Browser, "_connect", lambda self: setattr(self, "ws", object())), \
                mock.patch.object(signin, "enforce_cookies",
                                  lambda bb, conn=None: seen.append((bb.__dict__.get("cookie_loaded"),
                                                                     bb.__dict__.get("cookie_checked")))):
            b.start()
        self.assertEqual(seen, [(None, None)])

    def test_the_project_profile_copy_brings_no_sign_in_of_a_set_without_a_grant(self):
        self.save()
        b = CookieBrowser(GH + OTHER)  # its first start copied the project's profile
        signin.enforce_cookies(b, self.c)
        self.assertEqual(self.names(b), ["sid"])
        from river import browser
        self.assertEqual([c["name"] for c in browser._skip(GH + OTHER, ["github.com"])], ["sid"])

    def test_an_unconfirmed_owner_gets_no_sign_in(self):
        self.save()
        vault.grant(self.c, "gh-set", "project", "site", actor="mark")
        b = CookieBrowser()
        b.signin_confirmed = False
        self.assertEqual(signin.enforce_cookies(b, self.c)["loaded"], [])
        self.assertEqual(b.jar, [])
        # #5845: the log says why, once for this Chrome, so a session that finds no sign-in can read it
        signin.enforce_cookies(b, self.c)
        why = [x for x in vault.log(self.c, "gh-cookies")["log"] if x["action"] == "refused"]
        self.assertEqual(len(why), 1)
        self.assertIn("could not confirm that the browser's last command came from dev", why[0]["detail"])

    def test_a_new_sign_in_is_saved_back(self):
        self.save([{"name": "user_session", "value": "old", "domain": "github.com", "path": "/"}])
        self.grant()
        b = CookieBrowser(GH, [page("https://github.com/login", pw=True, user=True), page("https://github.com/")])
        self.assertEqual(signin.check(b, self.c), ["password"])
        got = vault.unpack_cookies(vault.Files().get(self.c, "gh-cookies"))
        self.assertEqual(sorted(c["value"] for c in got), ["abc", "s3ss10n"])
        self.assertEqual(vault.log(self.c, "gh-cookies")["log"][0]["actor"], "maxpm")

    def test_cookies_go_to_no_command_and_only_a_person_saves_them(self):
        self.save()
        self.grant()
        with self.assertRaises(RiverError) as e:
            vault.get(self.c, "gh-cookies", "dev", "pipe: cat", "pipe", ["cat"])
        self.assertIn("no command gets them", str(e.exception))
        with self.assertRaises(RiverError):
            vault.cookies_save(self.c, "x-cookies", "github.com", GH, "dev")
        packed = vault.pack_cookies([{"name": "n", "value": "v\n"}])
        self.assertEqual(vault.unpack_cookies(packed), [{"name": "n", "value": "v\n"}])


class SigninRequests(Base):
    """#5840: a session asks for the person's sign-ins of sites from another session's browser; the person gets it at
    once (the page's modal), and Allow saves the ticked sites' cookies and grants them to the task in one step."""

    def setUp(self):
        super().setUp()
        from river import browser
        core.register(self.c, "boss")
        self.addCleanup(lambda saved=dict(browser.BROWSERS): (browser.BROWSERS.clear(), browser.BROWSERS.update(saved)))
        src = CookieBrowser(GH + OTHER, owner="boss")
        src.proxy = None
        browser.BROWSERS[("site", "boss")] = src

    def ask(self, sites=("github.com", "example.org")):
        return vault.signin_request(self.c, list(sites), "site/boss", "check the repo page", "dev")

    def test_allow_saves_the_ticked_sites_and_grants_them_to_the_task(self):
        from river.routes import secrets as routes_secrets
        r = self.ask()
        self.assertEqual((r["state"], r["sites"], r["ticked"], r["to"], r["scope"], r["value"]),
                         ("asked", ["github.com", "example.org"], ["github.com", "example.org"], "mark", "item",
                          str(self.item)))
        body = self.c.execute("SELECT body FROM messages WHERE to_agent='mark' ORDER BY id DESC").fetchone()[0]
        self.assertIn("asks for your sign-ins of github.com, example.org from the browser of boss (site)", body)
        # The page's state lists it, so the modal opens over any tab
        self.assertEqual([x[0] for x in self.c.execute(
            "SELECT id FROM secret_requests WHERE state='asked' AND sites IS NOT NULL")], [r["id"]])
        with self.assertRaises(RiverError):  # only a person allows
            routes_secrets.act(self.c, {"action": "approve", "request": r["id"], "only": ["github.com"]}, "dev")
        out = routes_secrets.act(self.c, {"action": "approve", "request": r["id"], "only": ["github.com"]}, "mark")
        self.assertEqual((out["state"], out["saved"]), ("granted", [f"r{r['id']}-github.com"]))
        b = CookieBrowser(owner="dev")
        self.assertEqual(signin.enforce_cookies(b, self.c)["loaded"], [f"r{r['id']}-github.com"])
        self.assertEqual(sorted(c["name"] for c in b.jar), ["_gh_sess", "user_session"])  # not example.org's
        other = CookieBrowser(owner="boss")  # the grant covers the task, not every session
        self.assertEqual(signin.enforce_cookies(other, self.c)["loaded"], [])

    def test_a_site_with_no_sign_in_there_refuses_the_whole_allow_and_deny_refuses(self):
        from river.routes import secrets as routes_secrets
        r = self.ask(("github.com", "gitlab.com"))
        with self.assertRaises(RiverError) as e:
            routes_secrets.act(self.c, {"action": "approve", "request": r["id"]}, "mark")
        self.assertIn("has no sign-in of gitlab.com", str(e.exception))
        self.assertEqual([s["name"] for s in vault.listing(self.c)["secrets"]], ["gh"])  # nothing was saved
        with self.assertRaises(RiverError):
            routes_secrets.act(self.c, {"action": "approve", "request": r["id"], "only": ["evil.com"]}, "mark")
        self.assertEqual(routes_secrets.act(self.c, {"action": "refuse", "request": r["id"]}, "mark")["state"],
                         "refused")

    def test_do_not_ask_again_lets_the_manager_grant_the_next_request(self):
        # #5842: 'Do not ask again for this site' in the modal makes the saved sign-in 'Allow without asking'
        from river.routes import secrets as routes_secrets
        r = self.ask()
        routes_secrets.act(self.c, {"action": "approve", "request": r["id"], "only": ["github.com", "example.org"],
                                    "always": ["github.com"]}, "mark")
        asks = {x["name"]: x["ask"] for x in vault.listing(self.c)["secrets"]}
        self.assertEqual((asks[f"r{r['id']}-github.com"], asks[f"r{r['id']}-example.org"]), ("allow", "ask"))
        core.register(self.c, "manager-1")
        self.c.execute("UPDATE agents SET role='manager' WHERE name='manager-1'")
        with mock.patch.object(core, "active_manager", lambda conn, but=None: "manager-1"):
            mixed = self.ask()  # example.org still asks: the whole request goes to the person, as before
            self.assertEqual((mixed["state"], mixed["to"]), ("asked", "mark"))
            routes_secrets.act(self.c, {"action": "refuse", "request": mixed["id"]}, "mark")
            r2 = self.ask(("github.com",))
        self.assertEqual((r2["state"], r2["to"]), ("open", "manager-1"))
        body = self.c.execute("SELECT body FROM messages WHERE to_agent='manager-1' ORDER BY id DESC").fetchone()[0]
        self.assertIn(f"maxpm secret approve r{r2['id']}", body)
        self.assertEqual([x[0] for x in self.c.execute(  # no modal for the person
            "SELECT id FROM secret_requests WHERE state='asked' AND sites IS NOT NULL")], [])
        with self.assertRaises(RiverError):
            routes_secrets.act(self.c, {"action": "approve", "request": r2["id"]}, "dev")
        out = routes_secrets.act(self.c, {"action": "approve", "request": r2["id"]}, "manager-1")
        self.assertEqual((out["state"], out["chosen"], out["saved"]), ("granted", [f"r{r['id']}-github.com"], []))
        vault.set_ask(self.c, f"r{r['id']}-github.com", "ask", actor="mark")  # mark changes it back in the panel
        r3 = self.ask(("github.com",))
        self.assertEqual((r3["state"], r3["to"]), ("asked", "mark"))

    def test_the_panel_read_names_the_accounts_of_each_site(self):
        from river import browser
        from river.routes import secrets as routes_secrets
        browser.account_change(self.c, "github.com", "mark-gh", actor="mark")
        self.ask()
        reqs = vault.requests(self.c)["requests"]
        routes_secrets.signin_accounts(self.c, reqs)
        self.assertEqual(reqs[0]["accounts"], {"github.com": ["mark-gh"], "example.org": []})

    def test_the_cli_asks_with_signin_and_from(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(["--as", "dev", "secret", "request", "--signin", "github.com", "--from", "boss",
                     "--from-project", "site", "--reason", "check the repo page"])
        self.assertIn("r1 asked: dev asks for the person's sign-ins of github.com from the browser of site/boss",
                      out.getvalue())
        self.assertNotIn("None", out.getvalue())
        self.assertIn(f"maxpm blocked {self.item} --reason \"waits for request r1\"", out.getvalue())
        self.assertEqual(vault.requests(self.c)["requests"][0]["source"], "site/boss")

    def test_allow_ends_the_block_that_names_the_request(self):
        """#5840: the asking session released its task, blocked on the request; Allow makes the task ready."""
        from river.routes import secrets as routes_secrets
        r = self.ask()
        core.block(self.c, self.item, "waits for request r1 (sign-ins)", actor="dev")
        core.release(self.c, self.item, "dev")
        routes_secrets.act(self.c, {"action": "approve", "request": r["id"]}, "mark")
        self.assertIsNone(self.c.execute("SELECT blocked_reason FROM items WHERE id=?", (self.item,)).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
