"""Dependency releases (#6603): the last release plus only a dependency update, started on its own, on every kind of
pipeline (a deploy target in mode deploy or push, a pipeline of its own), by hand or on a schedule."""

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

from river import core
from river.core import RiverError
from river.pipeline import define, deps, store


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
        deps.SYNC["on"] = True
        self.addCleanup(deps.SYNC.update, on=False)
        core.register(self.c, "mark", human=True)
        core.register(self.c, "w1")
        self.folder = os.path.join(self.dir.name, "app")
        os.makedirs(self.folder)
        self.git("init", "-q", "-b", "main")
        self.write("Cargo.lock", "serde 1.0\n")
        self.write("src.txt", "fn main\n")
        self.base = self.commit("Start", "Cargo.lock", "src.txt")

    def git(self, *a):
        return subprocess.run(["git", "-C", self.folder, *a], capture_output=True, text=True, check=True).stdout.strip()

    def write(self, name, text):
        path = os.path.join(self.folder, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)

    def commit(self, msg, *names):
        self.git("add", *names)
        self.git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def cli(self, *words, who="mark"):
        from river import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.run(["--as", who, *words])
        return code, out.getvalue()

    def released(self, target, project):
        """One code release of target went out at self.base: the person's done on its deploy item."""
        t = core.item_add(self.c, project, "The first change", actor="mark")["id"]
        core.claim(self.c, t, "w1")
        core.done(self.c, t, output="done", actor="w1", ship_it=True, force="test")
        dep = core.release_plan(self.c, target)["deploy"]
        core.target_cut(self.c, target, rev=self.base, actor="mark")
        core.target_own(self.c, target, "mark")
        core.claim(self.c, dep, "mark")
        core.done(self.c, dep, output="out", actor="mark", force="test")
        return dep


class DeployTarget(Base):
    """A deploy target in mode deploy (toolscaled-prod, maximizepm-github): a release item pinned at the update."""

    def setUp(self):
        super().setUp()
        core.target_add(self.c, "shop", "", None)
        core.project_add(self.c, "web", path=self.folder, target="shop")
        self.first = self.released("shop", "web")
        # main moves on with code that no release took yet
        self.write("src.txt", "fn new_code\n")
        self.main = self.commit("New code", "src.txt")

    def test_off_until_a_person_or_the_manager_turns_it_on(self):
        with self.assertRaisesRegex(RiverError, "is off"):
            deps.start(self.c, "shop", actor="mark")
        with self.assertRaisesRegex(RiverError, "only a person or the manager"):
            deps.configure(self.c, "shop", ["cargo update"], True, actor="w1")
        with self.assertRaisesRegex(RiverError, "name the update commands first"):
            deps.configure(self.c, "shop", None, True, actor="mark")
        deps.configure(self.c, "shop", ["echo 'serde 1.1' > Cargo.lock"], True, actor="mark")
        with self.assertRaisesRegex(RiverError, "target owner starts"):
            deps.start(self.c, "shop", actor="w1")  # a worker that does not own the target

    def test_the_update_goes_out_alone_through_the_usual_release(self):
        deps.configure(self.c, "shop", ["echo 'serde 1.1' > Cargo.lock"], True, actor="mark")
        out = deps.start(self.c, "shop", actor="mark")
        dep = core.item_show(self.c, out["deploy"])
        self.assertEqual((out["base"], dep["kind"], dep["blocked_set_by"]), (self.base, "deploy", deps.DEPS_BY))
        self.assertEqual(core.release_plan(self.c, "shop")["running"], dep["id"])  # no code release is cut meanwhile
        with self.assertRaisesRegex(RiverError, "runs now"):
            deps.start(self.c, "shop", actor="mark")
        deps.serve_pass(self.c)
        run = deps.show(self.c, "shop")["runs"][0]
        self.assertEqual((run["state"], run["files"]), ("passed", ["Cargo.lock"]), run)
        rev = run["rev"]
        self.assertEqual(self.git("rev-parse", f"{rev}^"), self.base)  # the last release, plus the update
        self.assertEqual(self.git("diff", "--name-only", self.base, rev), "Cargo.lock")  # no code of main
        self.assertEqual(self.git("rev-parse", f"maxpm/deps/shop-{run['id']}"), rev)
        dep = core.item_show(self.c, out["deploy"])
        self.assertEqual((dep["cut_rev"], dep["blocked_set_by"]), (rev, None))
        self.assertEqual(deps.kind_of_deploy(self.c, dep["id"]), "deps")  # MAXPM_RELEASE_KIND of its hooks
        self.assertEqual(deps.base_of_deploy(self.c, dep["id"]), self.base)
        self.assertEqual(deps.kind_of_deploy(self.c, self.first), "code")
        ships = core.release_plan(self.c, "shop")  # the update is the release's one task, done
        change = self.c.execute("SELECT i.* FROM deps d JOIN items i ON i.id=d.blocked_by WHERE d.item_id=? AND "
                                "i.kind='work'", (dep["id"],)).fetchone()
        self.assertEqual((change["status"], change["touches"]), ("done", "Cargo.lock"))
        self.assertIsNotNone(ships)
        # the task that brings it into main, and the next code release waits on it
        merge = core.item_show(self.c, run["item_id"])
        self.assertIn(f"git cherry-pick {rev[:12]}", merge["context"])
        self.assertIn("do not merge a lock file's text by hand", merge["context"])
        t = core.item_add(self.c, "web", "Another change", actor="mark")["id"]
        core.claim(self.c, t, "w1")
        core.done(self.c, t, output="done", actor="w1", ship_it=True, force="test")
        nxt = core.release_plan(self.c, "shop")["deploy"]
        self.assertNotEqual(nxt, dep["id"])  # a ship request joins the next release item, not the dependency one
        deps.serve_pass(self.c)
        self.assertTrue(self.c.execute("SELECT 1 FROM deps WHERE item_id=? AND blocked_by=?",
                                       (nxt, merge["id"])).fetchone())
        # the page and maxpm pipeline show say the kind of each version
        from river.pipeline import targets
        kinds = {r["deploy"]: r["kind"] for r in targets.default_pipeline(self.c, "shop")["releases"]}
        self.assertEqual((kinds[dep["id"]], kinds[self.first]), ("deps", "code"))
        code, text = self.cli("pipeline", "show", "shop")
        self.assertEqual(code, 0, text)
        self.assertIn(f"release #{dep['id']}  dependency release", text)
        self.assertIn(f"release #{self.first}  code release", text)

    def test_the_same_review_and_yes_as_a_code_release(self):
        core.config_set(self.c, "review", "on", project="deploy-shop")  # where a release reads it (its deploy item)
        core.config_set(self.c, "release_approval", "person", project="web")
        deps.configure(self.c, "shop", ["echo 'serde 1.1' > Cargo.lock"], True, actor="mark")
        out = deps.start(self.c, "shop", actor="mark")
        self.assertIsNotNone(core.approval_refusal(self.c, core._item(self.c, out["deploy"])))  # it waits for a yes
        deps.serve_pass(self.c)
        rv = self.c.execute("SELECT i.* FROM deps d JOIN items i ON i.id=d.blocked_by WHERE d.item_id=? AND "
                            "i.kind='review'", (out["deploy"],)).fetchone()
        self.assertIsNotNone(rv)
        self.assertEqual(rv["status"], "open")
        self.assertNotIn(out["deploy"], core.release_yes_pass(self.c))  # not ready: its review runs first
        core.claim(self.c, rv["id"], "mark")
        core.done(self.c, rv["id"], output="checked", actor="mark", force="test")
        yes = core.release_yes_pass(self.c).get(out["deploy"])
        self.assertIsNotNone(yes)
        self.assertIn("Dependency update of shop", core.item_show(self.c, yes)["context"])

    def test_a_command_that_changes_code_fails_and_adds_a_fix_task(self):
        deps.configure(self.c, "shop", ["echo x >> src.txt; echo 'serde 1.1' > Cargo.lock"], True, actor="mark")
        out = deps.start(self.c, "shop", actor="mark")
        deps.serve_pass(self.c)
        run = deps.show(self.c, "shop")["runs"][0]
        self.assertEqual(run["state"], "failed")
        self.assertIn("src.txt, which are no dependency files", run["errors"])
        self.assertEqual(core.item_show(self.c, out["deploy"])["status"], "dropped")
        fix = core.item_show(self.c, run["fix_id"])
        self.assertIn(f"--kind deps --rev <commit>", fix["context"])

    def test_a_failed_command_and_no_change(self):
        deps.configure(self.c, "shop", ["exit 3"], True, actor="mark")
        deps.start(self.c, "shop", actor="mark")
        deps.serve_pass(self.c)
        self.assertIn("exited 3", deps.show(self.c, "shop")["runs"][0]["errors"])
        deps.configure(self.c, "shop", ["true"], True, actor="mark")
        out = deps.start(self.c, "shop", actor="mark")
        deps.serve_pass(self.c)
        self.assertEqual(deps.show(self.c, "shop")["runs"][0]["state"], "unchanged")
        self.assertEqual(core.item_show(self.c, out["deploy"])["status"], "dropped")
        self.assertIsNone(core.release_plan(self.c, "shop")["running"])

    def test_a_commit_made_by_hand(self):
        deps.configure(self.c, "shop", ["true"], True, actor="mark")
        self.git("checkout", "-q", "-b", "fix", self.base)
        self.write("Cargo.lock", "serde 1.2\n")
        hand = self.commit("Update serde", "Cargo.lock")
        self.write("src.txt", "more code\n")
        bad = self.commit("Code too", "src.txt")
        self.git("checkout", "-q", "main")
        with self.assertRaisesRegex(RiverError, "src.txt, which are no dependency files"):
            deps.start(self.c, "shop", rev=bad, actor="mark")
        with self.assertRaisesRegex(RiverError, "src.txt, which are no dependency files"):
            deps.start(self.c, "shop", rev=self.main, actor="mark")  # main holds code that no release took
        out = deps.start(self.c, "shop", rev=hand, actor="mark")
        self.assertEqual(core.item_show(self.c, out["deploy"])["cut_rev"], hand)

    def test_the_start_command_and_the_page_action(self):
        code, text = self.cli("pipeline", "deps", "shop", "--cmd", "echo 'serde 1.1' > Cargo.lock", "--on")
        self.assertEqual(code, 0, text)
        self.assertIn("dependency release on", text)
        code, text = self.cli("pipeline", "start", "shop", "--kind", "deps")
        self.assertEqual(code, 0, text)
        self.assertIn("dependency release u", text)
        from river.routes.pipeline import OPS
        with self.assertRaisesRegex(RiverError, "runs now"):
            OPS["pipeline_start_kind"](self.c, {"pipeline": "shop", "kind": "deps"}, "mark")
        self.assertTrue(deps.head(self.c, "shop")["on"])


class PushTarget(Base):
    """A deploy target in mode push (maximizepm-com and the sites): one task tests the update and pushes it."""

    def test_the_update_is_a_task_that_pushes_it(self):
        core.target_add(self.c, "site", "", None)
        core.project_add(self.c, "www", path=self.folder, target="site")
        core.target_mode(self.c, "site", "push", actor="mark")
        deps.configure(self.c, "site", ["echo 'serde 1.1' > Cargo.lock"], True, actor="mark")
        out = deps.start(self.c, "site", actor="mark")
        self.assertEqual((out["mode"], out["deploy"], out["base_label"]), ("push", None, "main"))
        deps.serve_pass(self.c)
        run = deps.show(self.c, "site")["runs"][0]
        self.assertEqual(run["state"], "passed")
        task = core.item_show(self.c, run["item_id"])
        self.assertTrue(task["title"].startswith("Push the dependency update"))
        self.assertIn("(no --ship)", task["context"])
        with self.assertRaisesRegex(RiverError, "not pushed yet"):
            deps.start(self.c, "site", actor="mark")


class OwnPipeline(Base):
    """A pipeline of its own: the update goes into its first stage as a version of kind deps."""

    def test_the_update_is_a_version(self):
        from river.pipeline import runner
        runner.SYNC["on"] = True
        self.addCleanup(runner.SYNC.update, on=False)
        core.project_add(self.c, "app", path=self.folder)
        define.pipeline_add(self.c, "app", project="app")
        define.stage_set(self.c, "app", "build", "build", run="true")
        runner.start(self.c, "app", rev=self.base, actor="mark")
        runner.serve_pass(self.c)
        deps.configure(self.c, "app", ["echo 'serde 1.1' > Cargo.lock"], True, actor="mark")
        out = deps.start(self.c, "app", actor="mark")
        self.assertEqual((out["base"], out["mode"]), (self.base, "own"))
        deps.serve_pass(self.c)
        run = deps.show(self.c, "app")["runs"][0]
        self.assertEqual(run["state"], "passed", run)
        kinds = {v["rev"]: v["kind"] for v in store.versions(self.c, "app")}
        self.assertEqual((kinds[run["rev"]], kinds[self.base]), ("deps", "code"))
        self.assertIsNotNone(run["item_id"])  # into main


class Scheduled(Base):
    """maxpm schedule add --pipeline <p> --kind deps: a weekly dependency release."""

    def test_a_schedule_starts_the_release(self):
        core.target_add(self.c, "shop", "", None)
        core.project_add(self.c, "web", path=self.folder, target="shop")
        self.released("shop", "web")
        deps.configure(self.c, "shop", ["echo 'serde 1.1' > Cargo.lock"], True, actor="mark")
        with self.assertRaisesRegex(RiverError, "only a person or the manager"):
            core.schedule_add(self.c, "web", "Weekly dependencies", cron="0 6 * * 1", pipeline="shop",
                              release_kind="deps", actor="w1")
        s = core.schedule_add(self.c, "web", "Weekly dependencies", cron="0 6 * * 1", pipeline="shop",
                              release_kind="deps", actor="mark")["schedule"]
        out = core.schedule_due(self.c, at=core.parse_iso(s["next_due"]) + timedelta(minutes=1))
        self.assertEqual(len(out), 1, out)
        self.assertEqual(out[0]["release"]["kind"], "deps")
        self.assertIsNone(out[0]["item"])
        code, text = self.cli("schedule", "list")
        self.assertIn("starts a deps release of pipeline shop", text)


if __name__ == "__main__":
    unittest.main()
