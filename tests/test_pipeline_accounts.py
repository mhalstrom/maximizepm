"""Accounts of pipelines (#6147): each forge token is an account; what the forge says about it; a refused or missing
token on the pipeline and its stages ('no news'); the manager's alerts at the first refusal and before the end."""

import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from river import cli, core
from river.core import RiverError
from river.pipeline import accounts, define, forge, plugin, runner
from river.routes import pipeline as routes


class FakeForge:
    """Answers by (host, path): (status, headers, JSON). Records each call with its auth header."""

    def __init__(self):
        self.routes, self.calls = {}, []

    def __call__(self, method, url, headers):
        from urllib.parse import urlparse
        u = urlparse(url)
        self.calls.append((u.netloc, u.path, headers.get("Authorization") or headers.get("PRIVATE-TOKEN")))
        status, hdrs, body = self.routes.get((u.netloc, u.path), (404, {}, {"message": "Not Found"}))
        import json
        return status, hdrs, json.dumps(body).encode()


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        env = mock.patch.dict(os.environ, {"MAXPM_DB": os.path.join(self.dir.name, "t.db")})
        env.start()
        self.addCleanup(env.stop)
        self.c = core.connect()
        self.addCleanup(self.c.close)
        self.api = FakeForge()
        old = (accounts.FETCH, forge.token)
        accounts.FETCH = self.api
        forge.token = lambda conn, p: f"value-of-{p['secret']}"
        self.addCleanup(lambda: setattr(accounts, "FETCH", old[0]) or setattr(forge, "token", old[1]))
        core.register(self.c, "mark", human=True)
        core.register(self.c, "boss")
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET role='manager' WHERE name='boss'")
        mgr = mock.patch("river.core.active_manager", lambda conn, but=None: "boss")
        mgr.start()
        self.addCleanup(mgr.stop)
        for name, repo in (("app", "mhalstrom/app"), ("site", "mhalstrom/site")):
            define.pipeline_add(self.c, name)
            define.stage_set(self.c, name, "tests", "test", workflow="tests.yml")
            define.forge_set(self.c, name, "github", repo=repo, secret="github-mhalstrom")
        self.save("github-mhalstrom")
        self.api.routes[("api.github.com", "/user")] = (
            200, {"github-authentication-token-expiration": "2027-01-10 00:00:00 UTC",
                  "x-oauth-scopes": "repo, workflow"}, {"login": "mhalstrom", "type": "User"})
        self.api.routes[("api.github.com", "/repos/mhalstrom/app")] = (200, {}, {"full_name": "mhalstrom/app"})

    def save(self, name):
        with core.tx(self.c):
            self.c.execute("INSERT OR IGNORE INTO secrets(name, created_at) VALUES (?, ?)", (name, core.iso(core.now())))

    def alerts(self):
        return [r["body"] for r in self.c.execute("SELECT body FROM messages WHERE kind='alert' AND to_agent='boss' "
                                                  "ORDER BY id")]

    def age(self, hours):
        """Make the last check of every account older."""
        with core.tx(self.c):
            self.c.execute("UPDATE pipeline_accounts SET checked_at=?",
                           (core.iso(core.now() - core.timedelta(hours=hours)),))


class Accounts(Base):
    def test_one_token_of_two_pipelines_is_one_account_with_what_github_says(self):
        res = accounts.serve_pass(self.c)
        self.assertEqual(res["checked"], ["github-mhalstrom"])
        [a] = accounts.listing(self.c)
        self.assertEqual((a["owner"], a["kind"], a["state"], a["secret"]), ("mhalstrom", "user", "ok", "github-mhalstrom"))
        self.assertEqual(a["expires_at"], "2027-01-10T00:00:00Z")
        self.assertEqual(a["repos"], {"mhalstrom/app": True, "mhalstrom/site": False})
        self.assertEqual(a["scopes"], "repo, workflow")
        self.assertEqual(a["used_by"], ["app", "site"])
        self.assertIn("Bearer value-of-github-mhalstrom", self.api.calls[0][2])
        # Within the hour no new check; after it, one.
        n = len(self.api.calls)
        accounts.serve_pass(self.c)
        self.assertEqual(len(self.api.calls), n)
        self.age(2)
        accounts.serve_pass(self.c)
        self.assertGreater(len(self.api.calls), n)
        self.assertEqual(self.alerts(), [])

    def test_a_refused_token_alerts_the_manager_once_and_shows_on_the_pipeline(self):
        self.api.routes[("api.github.com", "/user")] = (401, {}, {"message": "Bad credentials"})
        accounts.serve_pass(self.c)
        self.age(2)
        accounts.serve_pass(self.c)
        [a] = accounts.listing(self.c)
        self.assertEqual((a["state"], a["error"]), ("refused", "GitHub answered 401: Bad credentials"))
        self.assertTrue(a["refused_since"])
        [alert] = self.alerts()
        self.assertIn("GitHub refuses the token github-mhalstrom (GitHub answered 401: Bad credentials). The "
                      "pipelines app, site show old data", alert)
        self.assertIn("maxpm secret put github-mhalstrom", alert)
        d = routes.deploy(self.c, "/api/deploy")
        app = next(p for p in d["pipelines"] if p["name"] == "app")
        self.assertEqual(app["source"]["state"], "bad")
        self.assertEqual(app["source"]["error"], "GitHub answered 401: Bad credentials")
        self.assertTrue(app["stages"][0]["no_news"])
        self.assertEqual(d["count"], 1)  # one token fails, whatever the number of its pipelines
        page = routes.pipeline(self.c, "/api/pipeline/app")
        self.assertEqual(page["account"]["state"], "refused")
        # A new token works: the refusal and its alert end, and a later refusal alerts again.
        self.api.routes[("api.github.com", "/user")] = (200, {}, {"login": "mhalstrom", "type": "User"})
        self.age(2)
        accounts.serve_pass(self.c)
        self.assertEqual(accounts.listing(self.c)[0]["state"], "ok")
        self.assertFalse(routes.deploy(self.c, "/api/deploy")["pipelines"][0]["stages"][0]["no_news"])
        self.api.routes[("api.github.com", "/user")] = (401, {}, {"message": "Bad credentials"})
        self.age(2)
        accounts.serve_pass(self.c)
        self.assertEqual(len(self.alerts()), 2)

    def test_a_refused_read_checks_the_account_at_once(self):
        accounts.serve_pass(self.c)
        self.api.routes[("api.github.com", "/user")] = (401, {}, {"message": "Bad credentials"})
        self.age(0.1)  # 6 minutes: past RECHECK_MIN, before CHECK_EVERY
        accounts.serve_pass(self.c)
        self.assertEqual(accounts.listing(self.c)[0]["state"], "ok")  # no refused read yet: no early check
        with core.tx(self.c):
            self.c.execute("UPDATE pipelines SET poll_error='GitHub answered 401 to GET /repos/x: Bad credentials' "
                           "WHERE name='app'")
        accounts.serve_pass(self.c)
        self.assertEqual(accounts.listing(self.c)[0]["error"], "GitHub answered 401: Bad credentials")

    def test_seven_days_before_the_end_the_manager_gets_one_alert(self):
        soon = core.now() + core.timedelta(days=4, hours=12)
        self.api.routes[("api.github.com", "/user")] = (
            200, {"github-authentication-token-expiration": soon.strftime("%Y-%m-%d %H:%M:%S UTC")},
            {"login": "mhalstrom", "type": "User"})
        accounts.serve_pass(self.c)
        self.age(2)
        accounts.serve_pass(self.c)
        [alert] = self.alerts()
        self.assertIn("The GitHub token github-mhalstrom of mhalstrom runs out in 4 day(s)", alert)
        self.assertEqual(int(accounts.listing(self.c)[0]["days_left"]), 4)

    def test_a_token_that_nobody_saved(self):
        define.forge_set(self.c, "site", "github", repo="mhalstrom/site", secret="github-other")
        accounts.serve_pass(self.c)
        other = next(a for a in accounts.listing(self.c) if a["secret"] == "github-other")
        self.assertEqual(other["state"], "missing")
        self.assertIn("site names the token github-other, but nobody saved it yet", other["error"])
        self.assertNotIn("Bearer value-of-github-other", [c[2] for c in self.api.calls])
        self.assertTrue(next(p for p in routes.deploy(self.c, "/api/deploy")["pipelines"]
                             if p["name"] == "site")["stages"][0]["no_news"])

    def test_gitlab_says_the_end_date_and_scopes_of_its_token(self):
        define.pipeline_add(self.c, "docs")
        define.forge_set(self.c, "docs", "gitlab", repo="group/docs", secret="gitlab-self", url="https://git.example.net")
        self.save("gitlab-self")
        self.api.routes[("git.example.net", "/api/v4/personal_access_tokens/self")] = (
            200, {}, {"expires_at": "2027-02-01", "scopes": ["api", "read_repository"]})
        self.api.routes[("git.example.net", "/api/v4/user")] = (200, {}, {"username": "docs-bot", "bot": True})
        self.api.routes[("git.example.net", "/api/v4/projects/group%2Fdocs")] = (200, {}, {"id": 4})
        accounts.serve_pass(self.c)
        a = next(a for a in accounts.listing(self.c) if a["forge"] == "gitlab")
        self.assertEqual((a["owner"], a["kind"], a["host"]), ("docs-bot", "bot", "git.example.net"))
        self.assertEqual((a["expires_at"], a["scopes"], a["repos"]), ("2027-02-01T00:00:00Z", "api, read_repository",
                                                                      {"group/docs": True}))
        self.assertIn(("git.example.net", "/api/v4/user", "value-of-gitlab-self"), self.api.calls)

    def test_no_answer_is_an_error_not_a_refusal(self):
        self.api.routes[("api.github.com", "/user")] = (502, {}, {"message": "Bad gateway"})
        accounts.serve_pass(self.c)
        a = accounts.listing(self.c)[0]
        self.assertEqual(a["state"], "error")
        self.assertIn("GitHub answered 502 to /user: Bad gateway", a["error"])
        self.assertEqual(self.alerts(), [])

    def test_the_serve_pass_of_the_runner_checks_them(self):
        with mock.patch.object(plugin, "poll_due", lambda conn: {}):  # no read of the runs on the real GitHub
            runner.serve_pass(self.c)
        self.assertEqual(accounts.listing(self.c)[0]["owner"], "mhalstrom")


class Commands(Base):
    def test_maxpm_pipeline_accounts_and_the_check_now(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(["-q", "--as", "mark", "pipeline", "accounts", "--check"])
            cli.run(["-q", "pipeline", "accounts"])
        text = out.getvalue()
        self.assertIn("GitHub github.com: token github-mhalstrom, account mhalstrom (user): works; runs out 2027-01-10",
                      text)
        self.assertIn("reaches: mhalstrom/app, mhalstrom/site (NOT reached)", text)
        self.assertIn("used by: app, site", text)
        with self.assertRaises(RiverError):
            accounts.check_now(self.c, actor="w1") if core.register(self.c, "w1") else None
        res = routes.OPS["pipeline_account_check"](self.c, {"secret": "github-mhalstrom"}, "mark")
        self.assertEqual(res[0]["state"], "ok")


if __name__ == "__main__":
    unittest.main()
