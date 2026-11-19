"""The controls of a deploy target's pipeline (#6176): transitions, operators, emergency actions, override, move back,
triggers and the audit log on a target with no pipeline of its own, while every release still goes through the
target's own path (its deploy item, the person's approval item, its deploy hook)."""

import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from river import core
from river.core import RiverError
from river.pipeline import audit, define, operators, runner, store, target_flow, targets, transitions, triggers


class TargetControls(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "t.db")
        env = mock.patch.dict(os.environ, {"MAXPM_DB": self.path, "MAXPM_QUIET": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.c = core.connect(self.path)
        self.addCleanup(self.c.close)
        core.register(self.c, "mark", human=True)
        core.target_add(self.c, "web-prod", "deploys web", None)
        core.project_add(self.c, "web", target="web-prod")
        core.config_set(self.c, "release_approval", "person", project="web")

    def release(self, rev, title, done=False):
        i = core.item_add(self.c, "web", title, actor="mark")["id"]
        core.done(self.c, i, output="done", actor="mark", ship_it=True)
        dep = self.c.execute("SELECT MAX(id) FROM items WHERE kind='deploy'").fetchone()[0]
        core.target_cut(self.c, "web-prod", rev=rev, actor=None)
        if done:
            with core.tx(self.c):
                self.c.execute("UPDATE items SET status='done', closed_at=? WHERE id=?", (core.iso(core.now()), dep))
        return dep

    def item(self, i):
        return self.c.execute("SELECT * FROM items WHERE id=?", (i,)).fetchone()

    def sweep(self):
        with core.tx(self.c):
            core._sweep(self.c)

    def stage(self, name):
        return next(s for s in targets.default_pipeline(self.c, "web-prod")["stages"] if s["name"] == name)

    def test_the_target_gets_a_row_for_its_controls_at_its_first_use(self):
        self.assertIsNone(target_flow.row(self.c, "web-prod"))
        arrow = self.stage("deploy")["transition"]
        self.assertFalse(arrow["fixed"])  # the page offers its controls before the row exists
        self.assertTrue(self.stage("watch")["transition"]["fixed"])
        p = store._pipeline(self.c, "web-prod")
        self.assertEqual(p["target"], "web-prod")
        self.assertEqual([s["name"] for s in target_flow._live(self.c, p["id"])], ["approval", "deploy", "watch"])
        # It lists once: as the target's default pipeline, never as a pipeline of its own.
        self.assertEqual([x["name"] for x in store.pipelines(self.c)], [])
        self.assertEqual([x["name"] for x in targets.default_list(self.c)], ["web-prod"])
        # A stage the target gains later (a review) joins the row, in the target's order.
        core.config_set(self.c, "review", "on", project="web")
        store._pipeline(self.c, "web-prod")
        self.assertEqual([s["name"] for s in target_flow._live(self.c, p["id"])],
                         ["review", "approval", "deploy", "watch"])

    def test_a_block_before_production_holds_the_deploy_item_and_its_hook(self):
        core.target_hook(self.c, "web-prod", "deploy", "true", actor="mark")
        dep = self.release("aaaaaaa", "A change")
        transitions.block(self.c, "web-prod", "deploy", "the database moves tonight", actor="mark")
        self.sweep()
        it = self.item(dep)
        self.assertEqual(it["blocked_set_by"], target_flow.HOLD_BY)
        self.assertIn("the transition into deploy of pipeline web-prod is blocked by mark", it["blocked_reason"])
        self.assertIsNone(core.deploy_hook_start(self.c, dep))  # the deploy hook does not start
        self.assertEqual(core.release_yes_pass(self.c), {})  # and nobody is asked for the yes yet
        arrow = self.stage("deploy")["transition"]
        self.assertEqual(arrow["state"], "blocked")
        self.assertEqual([h["id"] for h in arrow["holds"]], [dep])
        transitions.unblock(self.c, "web-prod", "deploy", actor="mark")
        self.sweep()
        self.assertIsNone(self.item(dep)["blocked_reason"])
        self.assertIn(dep, core.release_yes_pass(self.c))  # the person's yes comes now
        lines = [l["action"] for l in audit.lines(self.c, "web-prod")["lines"]]
        self.assertIn("block", lines)
        self.assertIn("unblock", lines)

    def test_a_blocker_of_someone_else_stays(self):
        dep = self.release("aaaaaaa", "A change")
        core.block(self.c, dep, "waiting on the vendor", actor="mark")
        transitions.disable(self.c, "web-prod", "deploy", "not now", actor="mark")
        self.sweep()
        self.assertEqual(self.item(dep)["blocked_reason"], "waiting on the vendor")
        transitions.enable(self.c, "web-prod", "deploy", actor="mark")
        self.sweep()
        self.assertEqual(self.item(dep)["blocked_reason"], "waiting on the vendor")

    def test_a_freeze_of_the_release_holds_the_cut_and_the_collected_release(self):
        transitions.freeze(self.c, "web-prod", None, None, "2d", "the holidays", actor="mark")
        i = core.item_add(self.c, "web", "A change", actor="mark")["id"]
        core.done(self.c, i, output="done", actor="mark", ship_it=True)
        dep = self.c.execute("SELECT MAX(id) FROM items WHERE kind='deploy'").fetchone()[0]
        with self.assertRaisesRegex(RiverError, "frozen until .* the holidays.*the release waits"):
            core.target_cut(self.c, "web-prod", rev="aaaaaaa", actor="mark")
        self.sweep()
        self.assertEqual(self.item(dep)["blocked_set_by"], target_flow.HOLD_BY)
        self.assertIsNone(self.item(dep)["cut_at"])
        # The watch has no controls; the freeze of every transition leaves it out.
        self.assertEqual(self.stage("watch")["transition"]["freezes"], [])
        with self.assertRaisesRegex(RiverError, "transition into watch .* has no controls"):
            transitions.block(self.c, "web-prod", "watch", "x", actor="mark")

    def test_the_modes_come_from_the_target_and_its_stages_from_its_releases(self):
        with self.assertRaisesRegex(RiverError, "comes from the target"):
            transitions.change(self.c, "web-prod", "deploy", mode="yes mark", actor="mark")
        transitions.change(self.c, "web-prod", "deploy", window="mon-fri 09:00-17:00 UTC", actor="mark")
        self.assertEqual(self.stage("deploy")["transition"]["window"]["text"], "mon-fri 09:00-17:00 UTC")
        with self.assertRaisesRegex(RiverError, "is a deploy target"):
            define.stage_set(self.c, "web-prod", "smoke", "test", run="make smoke", actor="mark")
        with self.assertRaisesRegex(RiverError, "takes the person's yes on its own item"):
            runner.approve(self.c, "web-prod", "deploy", actor="mark")

    def test_run_again_of_deploy_makes_a_deploy_item_that_waits_on_the_person_s_yes(self):
        old = self.release("aaaaaaa", "First change", done=True)
        new = self.release("bbbbbbb", "Second change")
        transitions.block(self.c, "web-prod", "deploy", "hold production", actor="mark")
        out = runner.run_again(self.c, "web-prod", "deploy", "aaaaaaa", actor="mark", reason="the newer one broke")
        dep = self.item(out["item"])
        self.assertEqual((dep["kind"], dep["target"], dep["cut_rev"], dep["status"]),
                         ("deploy", "web-prod", "aaaaaaa", "open"))
        self.assertTrue(dep["cut_at"])
        self.assertEqual(out["waits"], [new])  # the open release waits until it is out
        self.sweep()
        self.assertIsNone(self.item(out["item"])["blocked_reason"])  # sent on purpose: it passes the block
        self.assertEqual(self.item(new)["blocked_set_by"], target_flow.HOLD_BY)
        self.assertEqual(core.approval_refusal(self.c, self.item(out["item"]))[:20], "target web-prod take")
        asked = core.release_yes_pass(self.c)
        self.assertIn(out["item"], asked)  # the person's approval item, as for every release
        # The same version again reuses that deploy item.
        again = runner.run_again(self.c, "web-prod", "deploy", "aaaaaaa", actor="mark")
        self.assertEqual((again["item"], again["reused"]), (out["item"], True))
        rows = store.stage_versions(self.c, "web-prod", "deploy")["runs"]
        self.assertEqual(rows[0]["deploy"], out["item"])
        self.assertEqual((rows[0]["what"], rows[0]["by"], rows[0]["why"]), ("run again", "mark", "the newer one broke"))
        self.assertTrue(next(r for r in rows if r["deploy"] == old)["live"])
        line = audit.lines(self.c, "web-prod", search="rerun")["lines"][0]
        self.assertEqual((line["action"], line["rev"]), ("rerun", "aaaaaaa"))
        with self.assertRaisesRegex(RiverError, "takes no run again"):
            runner.run_again(self.c, "web-prod", "watch", "aaaaaaa", actor="mark")

    def test_live_is_the_release_in_production_in_every_stage(self):
        """#6182: the review table marked 9783eb1 live (the newest passed review) while production ran daa69ba. Live
        is the newest done deploy in every stage; a plain release has no 'ran' and no 'the release of deploy #N'."""
        out = self.release("aaaaaaa", "First change", done=True)
        self.release("bbbbbbb", "Second change")  # cut, not out: its holding passed, its deploy waits
        for stage in ("holding", "deploy"):
            rows = store.stage_versions(self.c, "web-prod", stage)["runs"]
            self.assertEqual([(r["rev"], r["live"]) for r in rows], [("bbbbbbb", False), ("aaaaaaa", True)], stage)
            self.assertEqual({(r["what"], r["why"]) for r in rows}, {(None, None)}, stage)
        self.assertEqual(self.c.execute("SELECT cut_rev FROM items WHERE id=?", (out,)).fetchone()[0], "aaaaaaa")

    def test_a_worker_may_not_and_an_operator_agent_may_only_when_agents_may(self):
        self.release("aaaaaaa", "First change", done=True)
        self.release("bbbbbbb", "Second change", done=True)
        core.register(self.c, "worker-1")
        with self.assertRaisesRegex(RiverError, "person or the manager"):
            runner.run_again(self.c, "web-prod", "deploy", "aaaaaaa", actor="worker-1")
        with self.assertRaisesRegex(RiverError, "person or the manager"):
            operators.stop(self.c, "web-prod", "fire", actor="worker-1")
        operators.change(self.c, "web-prod", add=["mark", "worker-1"], actor="mark")
        with self.assertRaisesRegex(RiverError, "lets no agent operate"):
            operators.stop(self.c, "web-prod", "fire", actor="worker-1")
        operators.change(self.c, "web-prod", agents=True, actor="mark")
        operators.stop(self.c, "web-prod", "the site is down", actor="worker-1")
        self.assertEqual(targets.default_pipeline(self.c, "web-prod")["held_reason"], "the site is down")
        out = operators.move_back(self.c, "web-prod", "deploy", "bbbbbbb broke checkout", actor="worker-1")
        self.assertEqual((out["version"], out["previous"]), ("aaaaaaa", "bbbbbbb"))
        self.sweep()
        # A stop of the whole pipeline holds even the version sent on purpose, until an operator resumes it.
        self.assertEqual(self.item(out["item"])["blocked_set_by"], target_flow.HOLD_BY)
        operators.resume(self.c, "web-prod", actor="worker-1")
        self.sweep()
        self.assertIsNone(self.item(out["item"])["blocked_reason"])
        # The move back blocked the transition into deploy: the next release waits until someone unblocks it.
        self.assertEqual(self.stage("deploy")["transition"]["state"], "blocked")
        kinds = {(l["action"], l["actor"]) for l in audit.lines(self.c, "web-prod")["lines"]}
        self.assertTrue({("emergency-stop", "worker-1"), ("resume", "worker-1"), ("rollback", "worker-1")} <= kinds)

    def test_override_sends_a_version_to_deploy_and_the_whole_run_cuts_a_release(self):
        self.release("aaaaaaa", "First change", done=True)
        plan = operators.plan(self.c, "web-prod")
        self.assertEqual([s["name"] for s in plan["override"]["stages"]], ["deploy"])
        self.assertEqual(plan["override"]["versions"][0]["rev"], "aaaaaaa")
        out = operators.override(self.c, "web-prod", "deploy", "ccccccc", "hotfix for the outage", actor="mark")
        self.assertEqual(self.item(out["item"])["cut_rev"], "ccccccc")
        self.assertIn("approval", out["skipped"])  # its own approval item still comes
        self.assertEqual(audit.lines(self.c, "web-prod", search="override")["lines"][0]["action"], "override")
        with self.assertRaisesRegex(RiverError, "nothing is collected"):
            operators.whole(self.c, "web-prod", "ccccccc", actor="mark")

    def test_a_trigger_rule_acts_on_a_failed_deploy_of_the_target(self):
        dep = self.release("aaaaaaa", "A change")
        triggers.rule_add(self.c, "web-prod", "stage-failed", "Find why the deploy failed", stage="deploy",
                          actor="mark")
        t = core.iso(core.now())
        self.c.execute("INSERT INTO hook_runs(target, event, command, deploy_id, rev, queued_at, started_at, ended_at, "
                       "exit_code, seconds, output) VALUES ('web-prod','deploy','make it',?,?,?,?,?,2,5,'boom')",
                       (dep, "aaaaaaa", t, t, t))
        fires = triggers.serve_pass(self.c)
        self.assertEqual(len(fires), 1)
        task = self.item(fires[0]["item"])
        self.assertEqual(task["kind"], "operational")
        self.assertIn("pipeline:web-prod:deploy", task["title"])
        self.assertEqual(triggers.serve_pass(self.c), [])  # once

    def test_the_cli_shows_the_target_with_its_controls(self):
        from river import cli
        transitions.block(self.c, "web-prod", "deploy", "wait for the vendor", actor="mark")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            cli.run(["--as", "mark", "pipeline", "list"])
            cli.run(["--as", "mark", "pipeline", "show", "web-prod"])
        text = out.getvalue()
        self.assertEqual(text.count("web-prod  (a deploy target: its default pipeline)"), 1)
        self.assertIn("wait for the vendor", text)

    def test_serve_releases_the_version_sent_first_through_the_deploy_hook(self):
        import sys
        import time
        from river import server
        core.config_unset(self.c, "release_approval", project="web")
        core.target_hook(self.c, "web-prod", "deploy", f'"{sys.executable}" -c "print(\'released\')"', actor="mark")
        self.release("aaaaaaa", "First change", done=True)
        new = self.release("bbbbbbb", "Second change")
        sent = runner.run_again(self.c, "web-prod", "deploy", "aaaaaaa", actor="mark", reason="back to the good one")
        r = server.auto_release(self.c, runner=[].append)
        self.assertEqual(list(r["deploying"]), [sent["item"]])  # the hook, not a command of ours, releases it
        for _ in range(600):
            if core._item(self.c, sent["item"])["status"] == "done" and not server.HOOKS["running"]:
                break
            time.sleep(0.05)
        self.assertEqual(core._item(self.c, sent["item"])["status"], "done")
        run = self.c.execute("SELECT rev FROM hook_runs WHERE deploy_id=? AND event='deploy'", (sent["item"],)).fetchone()
        self.assertEqual(run["rev"], "aaaaaaa")  # MAXPM_REV of the deploy hook
        self.assertEqual(self.item(new)["status"], "open")  # the open release goes next

    def test_the_deploy_tab_reads_its_stop(self):
        from river.routes import pipeline as routes
        operators.stop(self.c, "web-prod", "incident", actor="mark")
        d = next(p for p in routes.deploy(self.c, "/api/deploy")["pipelines"] if p["name"] == "web-prod")
        self.assertEqual((d["held"], d["controls"]), ("incident", True))
        one = routes.pipeline(self.c, "/api/pipeline/web-prod")
        self.assertTrue(one["default"])
        self.assertEqual(routes.stage_runs(self.c, "/api/pipeline-stage?pipeline=web-prod&stage=deploy")["runs"], [])


if __name__ == "__main__":
    unittest.main()
