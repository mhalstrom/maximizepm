"""Keys for agents with no person (#6153): credential sets granted to a stage, a tier or a runner. A stage's command
gets only its own keys, a trigger rule gives a stage key only to a task of its stages, a run on a runner gets the
runner's keys on stdin, and no key is a person's sign-in."""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from river import core, runners, vault
from river.core import RiverError
from river.pipeline import define, keys, operators, triggers

from tests.test_pipeline import RunnerBase
from tests.test_runners import Base as RunnerServerBase

PROD = b"prod-value-7f3a9c"
BETA = b"beta-value-2b8e41"
MODEL = b"sk-model-5d1c77"


def secrets_in(case):
    """A file store for the test, three secrets and three sets: prod-keys, beta-keys and model-keys."""
    old = vault.BACKEND
    vault.BACKEND = vault.Files()
    case.addCleanup(setattr, vault, "BACKEND", old)
    vault.put(case.c, "prod-ssh", PROD, "mark")
    vault.put(case.c, "beta-tok", BETA, "mark")
    vault.put(case.c, "anthropic-ci", MODEL, "mark", var="ANTHROPIC_API_KEY")
    vault.set_change(case.c, "prod-keys", add=["prod-ssh"], actor="mark")
    vault.set_change(case.c, "beta-keys", add=["beta-tok"], actor="mark")
    vault.set_change(case.c, "model-keys", add=["anthropic-ci"], actor="mark")


class StageKeys(RunnerBase):
    def setUp(self):
        super().setUp()
        secrets_in(self)
        define.stage_set(self.c, "app", "prod", tier="prod")
        define.stage_set(self.c, "app", "beta", tier="beta")

    def output(self, stage):
        return self.runs()[stage]["output"] or ""

    def test_each_stage_command_gets_only_its_own_keys_and_the_output_masks_them(self):
        vault.grant(self.c, "beta-keys", "stage", "app/beta", actor="mark")
        vault.grant(self.c, "prod-keys", "tier", "app/prod", actor="mark")
        define.stage_set(self.c, "app", "beta", run='echo "beta=$BETA_TOK prod=${PROD_SSH:-none}"')
        define.stage_set(self.c, "app", "prod", run='echo "prod=$PROD_SSH beta=${BETA_TOK:-none}"')
        self.runner.start(self.c, "app", rev="abc1234")
        self.passes()
        self.runner.approve(self.c, "app", "prod", actor="mark")
        self.passes(2)
        beta, prod = self.output("beta"), self.output("prod")
        self.assertIn("beta=*** prod=none", beta)  # beta reads no prod key
        self.assertIn("prod=*** beta=none", prod)
        self.assertIn("maxpm: the keys of stage prod: prod-ssh as $PROD_SSH", prod)
        for text in (beta, prod, self.output("build")):
            self.assertNotIn(PROD.decode(), text)
            self.assertNotIn(BETA.decode(), text)
        self.assertNotIn("the keys of stage", self.output("build"))  # a stage with no key says nothing
        uses = [(r["name"], r["actor"]) for r in self.c.execute("SELECT name, actor FROM secret_log WHERE action='use'")]
        self.assertEqual(sorted(uses), [("beta-tok", "stage app/beta"), ("prod-ssh", "stage app/prod")])

    def test_who_grants_where_and_never_a_persons_signin(self):
        with self.assertRaises(RiverError):  # an agent that is not the manager
            vault.grant(self.c, "prod-keys", "stage", "app/prod", actor="w1")
        for scope, value in (("stage", "app/nope"), ("stage", "prod"), ("tier", "app/gamma"), ("runner", "ci-9"),
                             ("stage", "nopipe/prod")):
            with self.assertRaises(RiverError, msg=value):
                vault.grant(self.c, "prod-keys", scope, value, actor="mark")
        with self.assertRaises(RiverError):  # a key lasts until a revoke
            vault.grant(self.c, "prod-keys", "stage", "app/prod", until="7d", actor="mark")
        vault.put(self.c, "mark-google", b"pw", "mark")
        with core.tx(self.c):
            self.c.execute("UPDATE secrets SET kind='signin' WHERE name='mark-google'")
        vault.set_change(self.c, "prod-keys", add=["mark-google"], actor="mark")
        with self.assertRaises(RiverError) as e:
            vault.grant(self.c, "prod-keys", "stage", "app/prod", actor="mark")
        self.assertIn("a person's sign-in (mark-google)", str(e.exception))
        vault.set_change(self.c, "prod-keys", remove=["mark-google"], actor="mark")
        g = vault.grant(self.c, "prod-keys", "stage", "app/prod", note="the deploy key", actor="mark")
        self.assertEqual((g["grant"], g["covers"], g["already"]), ("k1", "stage app/prod", False))
        self.assertTrue(vault.grant(self.c, "prod-keys", "stage", "app/prod", actor="mark")["already"])
        listed = {x["grant"]: x for x in vault.grants(self.c)["grants"]}
        self.assertIn("k1", listed)
        self.assertNotIn("k1", {x["grant"] for x in vault.grants(self.c, for_agent="w1")["grants"]})
        line = self.c.execute("SELECT actor, action, place, change FROM pipeline_changes WHERE action='key'").fetchone()
        self.assertEqual((line[0], line[1], line[2]), ("mark", "key", "stage prod"))
        self.assertIn("may read the set prod-keys (prod-ssh)", line[3])
        from river.pipeline import versions  # a key grant is part of the definition: a new version (#6143)
        newest = versions.listing(self.c, "app")
        self.assertIn("k1: stage app/prod may read the set prod-keys", str(newest))
        self.assertEqual(define.export(self.c, "app")["keys"], [{"set": "prod-keys", "stage": "prod"}])
        out = vault.revoke(self.c, "k1", actor="mark")["revoked"]
        self.assertEqual([x["grant"] for x in out], ["k1"])
        self.assertEqual(keys.stage_sets(self.c, 1, "prod", "prod"), [])
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM pipeline_changes WHERE action='revoke'").fetchone()[0], 1)
        # A sign-in added to a granted set later is left out of the command, and the output says so.
        vault.grant(self.c, "prod-keys", "tier", "app/prod", actor="mark")
        with core.tx(self.c):
            self.c.execute("INSERT INTO secret_set_members (set_id, name, added_at) SELECT id, 'mark-google', ? FROM "
                           "secret_sets WHERE name='prod-keys'", (core.iso(core.now()),))
        run = {"stage_id": self.c.execute("SELECT id FROM pipeline_stages WHERE name='prod'").fetchone()[0],
               "pipeline_id": 1, "pipeline": "app", "id": 99, "cmd": "deploy.sh"}
        env, forms, given, left = keys.stage_env(self.c, run)
        self.assertEqual(list(env), ["PROD_SSH"])
        self.assertIn("mark-google: a person's sign-in is never given to stage app/prod", left)

    def test_a_rule_gives_a_stage_key_only_to_a_task_of_its_stages(self):
        vault.grant(self.c, "prod-keys", "tier", "app/prod", actor="mark")
        with self.assertRaises(RiverError) as e:  # a rule of the build stage cannot name a prod key
            triggers.rule_add(self.c, "app", "stage-failed", stage="build", keys="prod-keys", actor="mark")
        self.assertIn("a key of stage prod of app only", str(e.exception))
        triggers.rule_add(self.c, "app", "stage-failed", stage="prod", keys="prod-keys", actor="mark")
        triggers.rule_add(self.c, "app", "stage-failed", tier="prod", keys="prod-keys,beta-keys", actor="mark")
        base = triggers.rules(self.c, "app")["rules"][0]
        triggers.rule_edit(self.c, "app", base["name"], keys="prod-keys,beta-keys", actor="mark")  # any stage
        with self.assertRaises(RiverError):  # the rule of prod cannot move to build with its prod key
            triggers.rule_edit(self.c, "app", "t2", stage="build", actor="mark")
        listed = {r["name"]: r["key_list"] for r in triggers.rules(self.c, "app")["rules"]}
        self.assertEqual(listed[base["name"]], [{"set": "prod-keys", "stages": ["prod"]},
                                                {"set": "beta-keys", "stages": None}])
        define.stage_set(self.c, "app", "build", run="exit 3")
        self.runner.start(self.c, "app", rev="bad0001")
        self.passes(2)
        result = self.c.execute("SELECT result FROM pipeline_trigger_fires").fetchone()[0]
        self.assertIn("; keys beta-keys", result)
        self.assertIn("held back: prod-keys is a key of prod only", result)
        grants = [r[0] for r in self.c.execute("SELECT s.name FROM secret_grants g JOIN secret_sets s ON "
                                               "s.id=g.set_id WHERE g.scope='item'")]
        self.assertEqual(grants, ["beta-keys"])  # the build fault's agent never gets the prod key

    def test_the_view_the_names_a_system_keeps_and_the_emergency_revoke(self):
        vault.grant(self.c, "prod-keys", "tier", "app/prod", actor="mark")
        vault.grant(self.c, "beta-keys", "stage", "app/beta", actor="mark")
        with self.assertRaises(RiverError):  # an agent that is not the manager
            keys.kept(self.c, "app", "prod", "Pulumi ESC environment acme/app/prod", "db-password", actor="w1")
        keys.kept(self.c, "app", "prod", "Pulumi ESC environment acme/app/prod", "db-password, aws-creds",
                  actor="mark")
        p = self.c.execute("SELECT * FROM pipelines WHERE name='app'").fetchone()
        keys.take_plugin(self.c, p, "github", [{"stage": "beta", "kept_in": "GitHub environment beta",
                                               "names": ["DEPLOY_KEY"]},
                                              {"stage": "prod", "kept_in": "Pulumi ESC environment acme/app/prod",
                                               "names": ["other"]}])  # a person's list stays
        with self.assertRaises(RiverError):
            keys.take_plugin(self.c, p, "github", [{"stage": "beta", "names": ["x"]}])
        v = keys.view(self.c, "app")["stages"]
        self.assertEqual(v["prod"]["secrets"], ["prod-ssh"])
        self.assertEqual(v["prod"]["sets"][0]["by"], "tier")
        self.assertEqual(v["prod"]["kept"][0]["names"], ["aws-creds", "db-password"])
        self.assertEqual(v["beta"]["kept"][0]["names"], ["DEPLOY_KEY"])
        self.assertEqual(v["build"], {"tier": None, "sets": [], "secrets": [], "kept": []})
        from river.routes import pipeline as route
        page = {s["name"]: s.get("keys") for s in route.pipeline(self.c, "/api/pipeline/app")["stages"]}
        self.assertEqual(page["prod"]["secrets"], ["prod-ssh"])
        self.assertEqual(page["beta"]["kept"][0]["kept_in"], "GitHub environment beta")
        # The Emergency tab lists the stage key and ends it for this pipeline only.
        define.pipeline_add(self.c, "other", project="app")
        define.stage_set(self.c, "other", "prod", "test", run="true", tier="prod")
        vault.grant(self.c, "prod-keys", "tier", "other/prod", actor="mark")
        listed = {k["key"]: k for k in operators._keys(self.c, p)}
        self.assertIn("stage(s) prod may read", listed["prod-keys"]["what"])
        self.assertEqual(listed["prod-keys"]["others"], ["other"])
        out = operators.revoke(self.c, "app", "prod-keys", "the key leaked", actor="mark")
        self.assertIn("every prod stage of app no longer reads it (k1)", out["done"])
        self.assertEqual(keys.view(self.c, "app")["stages"]["prod"]["sets"], [])
        self.assertEqual(keys.view(self.c, "other")["stages"]["prod"]["secrets"], ["prod-ssh"])

    def test_the_commands(self):
        from river import cli
        out = io.StringIO()

        def run(*words):
            out.truncate(0); out.seek(0)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                    mock.patch.dict(os.environ, {"MAXPM_DB": self.path, "MAXPM_QUIET": "1"}):
                code = cli.run(["--as", "mark", *words])
            return code, out.getvalue()
        code, text = run("secret", "grant", "prod-keys", "--tier", "app/prod")
        self.assertEqual(code, 0, text)
        self.assertIn("granted: k1  set prod-keys to every prod stage of app", text)
        code, text = run("pipeline", "keys", "app", "prod", "--kept-in", "GitHub environment prod", "--names",
                         "DEPLOY_KEY")
        self.assertEqual(code, 0, text)
        code, text = run("pipeline", "keys", "app")
        self.assertEqual(code, 0, text)
        self.assertIn("prod [prod]: prod-keys (prod-ssh; k1, by its tier prod)", text)
        self.assertIn("GitHub environment prod keeps DEPLOY_KEY (a person said so)", text)
        code, text = run("pipeline", "trigger", "app", "edit", "t1", "--keys", "prod-keys")
        code, text = run("pipeline", "triggers", "app")
        self.assertIn("keys prod-keys (prod only)", text)
        code, text = run("secret", "grants")
        self.assertIn("k1  set prod-keys", text)
        code, text = run("secret", "revoke", "k1")
        self.assertEqual(code, 0, text)
        self.assertIn("revoked: k1", text)


class GitHubSecretNames(unittest.TestCase):
    def test_the_github_plugin_sends_each_deploy_environments_secret_names_once_an_hour(self):
        import importlib.util
        import json
        spec = importlib.util.spec_from_file_location(
            "gh_plugin_6153", Path(__file__).resolve().parent.parent / "river/pipeline/plugins/github/plugin.py")
        gh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gh)
        asked = []

        def fetch(method, url, headers, body):
            asked.append(url)
            if url.endswith("/environments/prod/secrets?per_page=100"):
                return 200, json.dumps({"total_count": 2, "secrets": [{"name": "DEPLOY_KEY"}, {"name": "DB_URL"}]}).encode()
            if "/environments/" in url:
                return 403, b'{"message": "Resource not accessible by integration"}'
            return 200, b"[]"
        req = {"call": "poll", "settings": {"forge": "github", "repo": "o/r"}, "secrets": {"token": "t"},
               "pipeline": {"stages": [{"name": "deploy", "env": "prod"}, {"name": "beta", "env": "beta"},
                                       {"name": "build"}], "envs": []}, "state": None}
        out = gh.handle(req, fetch)
        self.assertEqual(out["secrets"], [{"stage": "deploy", "kept_in": "GitHub environment prod",
                                           "names": ["DEPLOY_KEY", "DB_URL"]}])  # beta's 403: none, the read goes on
        again = gh.handle(dict(req, state=out["state"]), fetch)
        self.assertNotIn("secrets", again)  # not again within the hour
        self.assertFalse(any("/environments/" in u for u in asked[3:]))


class RunnerKeys(RunnerServerBase):
    def setUp(self):
        super().setUp()
        secrets_in(self)

    def test_a_run_gets_the_runners_model_key_and_its_tasks_keys_on_stdin(self):
        self.add()
        vault.grant(self.c, "model-keys", "runner", "ci-1", actor="mark")
        vault.grant(self.c, "beta-keys", "stage", "app/build", actor="mark")
        rule = triggers.rules(self.c, "app")["rules"][0]
        triggers.rule_edit(self.c, "app", rule["name"], keys="beta-keys", actor="mark")
        run = self.fault_on_runner()
        runners.serve_pass(self.c)
        s = self.spawned[0]
        self.assertIn("ANTHROPIC_API_KEY=" + MODEL.decode(), s["script"])
        self.assertIn("BETA_TOK=" + BETA.decode(), s["script"])
        self.assertNotIn(PROD.decode(), s["script"])
        self.assertNotIn(MODEL.decode(), " ".join(s["argv"]))  # never on a command line
        hist = core.item_show(self.c, run["item"])["events"]
        self.assertTrue(any("keys anthropic-ci as $ANTHROPIC_API_KEY, beta-tok as $BETA_TOK" in h["change"]
                            for h in hist), hist)
        listed = runners.listing(self.c)["runners"][0]["keys"]
        self.assertEqual([(k["set"], k["secrets"]) for k in listed], [("model-keys", ["anthropic-ci"])])
        rk = triggers.rules(self.c, "app")["rules"][0]["key_list"]
        self.assertIn({"set": "model-keys", "stages": None, "runner": "ci-1"}, rk)

    def test_a_rule_on_a_runner_refuses_a_set_with_a_persons_signin(self):
        self.add()
        runners.allow_pipeline(self.c, "app", "ci-1", actor="mark")
        with core.tx(self.c):
            self.c.execute("UPDATE secrets SET kind='cookies' WHERE name='beta-tok'")
        with self.assertRaises(RiverError):
            triggers.rule_add(self.c, "app", "stage-failed", runner="ci-1", keys="beta-keys", actor="mark")
        with self.assertRaises(RiverError):
            vault.grant(self.c, "beta-keys", "runner", "ci-1", actor="mark")

    def test_the_transcript_masks_the_keys(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "run1.log"
        argv = [sys.executable, "-c", "import sys; print('script: ' + sys.stdin.read().strip()); "
                                      f"print('the key is {MODEL.decode()} ok')"]
        proc = runners._spawn(argv, "hello", path, vault.mask_forms(MODEL))
        self.assertEqual(proc.wait(timeout=30), 0)
        text = path.read_text()
        self.assertIn("script: hello", text)  # the script reaches the command on stdin
        self.assertIn("the key is *** ok", text)
        self.assertNotIn(MODEL.decode(), text)
        self.assertFalse(Path(str(path) + ".forms").exists())  # the masking process deleted its forms file


if __name__ == "__main__":
    unittest.main()
