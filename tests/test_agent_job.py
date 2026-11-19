"""The agent job in a forge's own CI (#6152): maxpm runner github writes .github/workflows/agent.yml and moves the
pipeline's stage-failed rules there; GitHub starts the job when a stage's workflow fails, and the github plugin's poll
brings its state, proof and transcript back. GitHub is the fake API of tests/test_pipeline.py."""

import contextlib
import io
import json
import os
import subprocess
import zipfile

from river import cli, core, runners
from river.core import RiverError
from river.pipeline import agent_job, define, triggers

from tests.test_pipeline import Base, FakeGitHub, gh_run, plugin_hooks

TRANSCRIPT = "\n".join([
    json.dumps({"type": "system", "subtype": "init", "model": "claude-opus-5-5", "cwd": "/work"}),
    json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "The test reads a removed file."}]}}),
    json.dumps({"type": "result", "total_cost_usd": 1.1, "result": "Cause: a removed fixture. Restored it; 54 tests pass."}),
]) + "\n"


def artifact(proof, transcript=TRANSCRIPT):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("proof.json", json.dumps(proof))
        z.writestr("transcript.jsonl", transcript)
    return buf.getvalue()


class AgentJob(Base):
    def setUp(self):
        super().setUp()
        from river.pipeline import runner
        runner.SYNC["on"] = True
        self.addCleanup(runner.SYNC.update, on=False)
        self.api = FakeGitHub()
        self.gh = plugin_hooks(self, self.api)
        core.project_add(self.c, "maximizepm")
        core.register(self.c, "mark", human=True)
        core.register(self.c, "w1")
        define.pipeline_add(self.c, "mpm", project="maximizepm")
        define.stage_set(self.c, "mpm", "tests", "test", workflow="tests.yml")
        define.stage_set(self.c, "mpm", "check", "test", run="true")  # a stage of this computer
        define.forge_set(self.c, "mpm", "github", repo="o/r", secret="gh-token")
        self.p = lambda: self.c.execute("SELECT * FROM pipelines WHERE name='mpm'").fetchone()
        self.tests, self.agents = [], []
        self.api.routes[("GET", "/repos/o/r/actions/workflows/tests.yml/runs")] = \
            lambda path, body: (200, {"workflow_runs": self.tests})
        self.api.routes[("GET", "/repos/o/r/actions/workflows/agent.yml/runs")] = \
            lambda path, body: (200, {"workflow_runs": self.agents})
        self.api.routes[("GET", "/repos/o/r/deployments")] = (200, [])
        self.api.routes[("GET", "/repos/o/r/actions/runs/")] = (200, {"jobs": [], "artifacts": []})  # any other run
        self.file = os.path.join(self.dir.name, "agent.yml")

    def setup_job(self, **kw):
        return agent_job.setup(self.c, "mpm", file=self.file, actor="mark", cwd=self.dir.name, **kw)

    def fail_tests(self):
        """The workflow tests.yml fails on GitHub for commit bad...; the poll reads it and the rule acts."""
        self.tests.append(gh_run(101, "b" * 40, conclusion="failure", at=core.iso(core.now())))
        self.gh.poll(self.c, self.p())
        triggers.serve_pass(self.c)
        return self.c.execute("SELECT * FROM runner_runs").fetchone()

    def agent_run(self, status="in_progress", conclusion=None):
        r = gh_run(555, "b" * 40, status=status, conclusion=conclusion, title="maxpm agent 101", at=core.iso(core.now()))
        self.agents[:] = [r]
        return r

    def test_setup_writes_the_job_and_moves_the_stage_failed_rule_to_github(self):
        res = self.setup_job(test="python3 -m unittest", max_minutes=20)
        with open(self.file) as f:
            text = f.read()
        self.assertEqual(res["workflows"], [".github/workflows/tests.yml"])  # no checkout here: the path is its name
        for part in ("run-name: maxpm agent ${{ github.event.workflow_run.id }}", "workflow_run:",
                     "github.event.workflow_run.head_branch == 'main'", "persist-credentials: false",
                     "timeout-minutes: 20", "ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}",
                     "'Bash(git commit:*)' 'Bash(python3:*)'", "name: maxpm-agent", "--permission-mode dontAsk"):
            self.assertIn(part, text)
        self.assertNotIn("git push", text.split("- name: Push the fix")[0])  # the agent's steps hold no push
        rule = triggers.rules(self.c, "mpm")["rules"][0]
        self.assertEqual(rule["runner"], "github")
        self.assertIn("now runs its agent in GitHub Actions", res["rules"][0])
        self.assertIn("github", runners.choices(self.c, self.p()))
        hist = [x["change"] for x in self.c.execute("SELECT change FROM pipeline_changes ORDER BY id")]
        self.assertTrue(any("agent job written" in w for w in hist))
        shown = agent_job.setup(self.c, "mpm", show=True, actor="mark", cli="codex")
        self.assertIn("codex exec --json", shown["text"])
        self.assertIn("CODEX_API_KEY: ${{ secrets.CODEX_API_KEY }}", shown["text"])
        self.assertIsNone(shown["file"])

    def test_setup_refuses_a_worker_another_source_and_another_checkout(self):
        with self.assertRaises(RiverError):
            agent_job.setup(self.c, "mpm", file=self.file, actor="w1")
        define.pipeline_add(self.c, "plain", project="maximizepm")
        with self.assertRaisesRegex(RiverError, "does not read GitHub"):
            agent_job.setup(self.c, "plain", file=self.file, actor="mark")
        repo = os.path.join(self.dir.name, "other")
        subprocess.run(["git", "init", "-q", repo], check=True)
        subprocess.run(["git", "-C", repo, "remote", "add", "origin", "git@github.com:else/where.git"], check=True)
        with self.assertRaisesRegex(RiverError, "not of o/r"):
            agent_job.setup(self.c, "mpm", actor="mark", cwd=repo)
        subprocess.run(["git", "-C", repo, "remote", "set-url", "origin", "https://github.com/O/r.git"], check=True)
        os.makedirs(os.path.join(repo, ".github", "workflows"))
        with open(os.path.join(repo, ".github", "workflows", "tests.yml"), "w") as f:
            f.write("name: tests\non: push\n")
        res = agent_job.setup(self.c, "mpm", actor="mark", cwd=repo)
        self.assertEqual(res["file"], os.path.join(os.path.realpath(repo), ".github", "workflows", "agent.yml"))
        self.assertEqual(res["workflows"], ["tests"])  # workflow_run names a workflow by its name
        with self.assertRaisesRegex(RiverError, "agent job"):
            runners.add(self.c, "github", "ci@x", actor="mark")

    def test_a_failure_in_github_goes_to_the_job_and_its_proof_comes_back(self):
        self.setup_job()
        run = self.fail_tests()
        self.assertEqual((run["runner"], run["state"], run["forge_key"], run["agent"]),
                         ("github", "queued", "gh-run-101", f"github-r{run['id']}"))
        item = core._item(self.c, run["item"])
        self.assertEqual(item["reserved_for"], run["agent"])  # no session here takes the fault
        fire = self.c.execute("SELECT result FROM pipeline_trigger_fires").fetchone()["result"]
        self.assertIn("the job maxpm-agent in GitHub Actions works on it", fire)
        self.assertEqual(runners.serve_pass(self.c)["started"], [])  # serve starts nothing over SSH for it
        # GitHub starts the job: the poll asks about the open run and marks it running.
        self.agent_run()
        self.gh.poll(self.c, self.p())
        req_agents = [c for c in self.api.calls if "agent.yml" in c[1]]
        self.assertTrue(req_agents)
        run = self.c.execute("SELECT * FROM runner_runs").fetchone()
        self.assertEqual((run["state"], run["job"], run["url"]),
                         ("running", "gh-run-555", "https://github.com/o/r/actions/runs/555"))
        # The job ends: its proof and transcript come back, the task goes back to the queue with the proof.
        self.agent_run("completed", "success")
        self.api.routes[("GET", "/repos/o/r/actions/runs/555/artifacts")] = (200, {"artifacts": [
            {"name": "maxpm-agent", "expired": False, "archive_download_url": "https://api.github.com/download/9"}]})
        self.api.routes[("GET", "/download/9")] = (200, artifact({
            "rev": "b" * 40, "commit": "7e1a2b0" + "0" * 33, "branch": "maxpm/fix-101",
            "pr": "https://github.com/o/r/pull/12", "test": {"command": "python3 -m unittest", "exit": 0,
                                                             "last": "OK"}, "agent_exit": 0}))
        self.gh.poll(self.c, self.p())
        run = self.c.execute("SELECT * FROM runner_runs").fetchone()
        self.assertEqual((run["state"], run["cost"]), ("ended", 1.1))
        self.assertIn("proof ready: commit 7e1a2b000000 on branch maxpm/fix-101, pull request "
                      "https://github.com/o/r/pull/12; python3 -m unittest: passed (OK)", run["result"])
        self.assertIn("Restored it", run["result"])
        self.assertEqual(json.loads(run["proof"])["branch"], "maxpm/fix-101")
        item = core._item(self.c, run["item"])
        self.assertIsNone(item["reserved_for"])
        self.assertEqual(item["status"], "open")
        notes = [m["body"] for m in self.c.execute("SELECT body FROM messages WHERE item_id=?", (item["id"],))]
        self.assertTrue(any("pull request https://github.com/o/r/pull/12" in b and "merge it" in b for b in notes))
        log = runners.log(self.c, run["id"])
        self.assertIn(("say", "The test reads a removed file."), [(x["kind"], x["text"]) for x in log["lines"]])
        forge = runners.listing(self.c)["forges"][0]
        self.assertEqual((forge["name"], forge["used_by"], forge["runs"][0]["id"]), ("github", ["mpm"], run["id"]))
        n = len(self.api.calls)
        self.gh.poll(self.c, self.p())  # no open job: the poll reads agent.yml no more
        self.assertFalse([c for c in self.api.calls[n:] if "agent.yml" in c[1]])

    def test_no_job_on_github_yet_says_so_and_the_task_comes_back_after_a_while(self):
        self.setup_job()
        run = self.fail_tests()
        self.api.routes[("GET", "/repos/o/r/actions/workflows/agent.yml/runs")] = (404, {"message": "Not Found"})
        self.gh.poll(self.c, self.p())
        self.assertIsNone(self.p()["poll_error"])
        run = self.c.execute("SELECT * FROM runner_runs").fetchone()
        self.assertEqual(run["state"], "queued")
        self.assertIn("has no workflow agent.yml in o/r yet", run["result"])
        with core.tx(self.c):
            self.c.execute("UPDATE runner_runs SET queued_at=?", (core.iso(core.now() - core.timedelta(hours=1)),))
        runners.serve_pass(self.c)
        run = self.c.execute("SELECT * FROM runner_runs").fetchone()
        self.assertEqual(run["state"], "failed")
        self.assertIn("showed no job maxpm-agent for the failed run within 30m", run["result"])
        self.assertIsNone(core._item(self.c, run["item"])["reserved_for"])

    def test_a_rule_on_github_for_a_stage_of_this_computer_leaves_the_task_here(self):
        self.setup_job()
        triggers.rule_edit(self.c, "mpm", "t1", stage="check", actor="mark")
        from river.pipeline import store
        run = store.run_start(self.c, "mpm", "check", version="c" * 40)  # a run of this computer, not of GitHub
        store.run_finish(self.c, run, "failed", errors="exit 3")
        triggers.serve_pass(self.c)
        fire = self.c.execute("SELECT * FROM pipeline_trigger_fires").fetchone()
        self.assertIsNotNone(fire)
        self.assertIn("did not run in GitHub Actions; a session here takes", fire["result"])
        self.assertIsNone(self.c.execute("SELECT * FROM runner_runs").fetchone())

    def test_stop_cancels_the_job_on_github(self):
        self.setup_job()
        self.fail_tests()
        self.agent_run()
        self.gh.poll(self.c, self.p())
        self.api.routes[("POST", "/repos/o/r/actions/runs/555/cancel")] = (202, {})
        run = self.c.execute("SELECT * FROM runner_runs").fetchone()
        res = runners.stop(self.c, run["id"], "mark", "a person looks at it")
        self.assertEqual(res["state"], "stopped")
        self.assertIn(("POST", "/repos/o/r/actions/runs/555/cancel", None), self.api.calls)
        run = self.c.execute("SELECT * FROM runner_runs").fetchone()
        self.assertEqual((run["state"], run["stopped_by"]), ("stopped", "mark"))

    def test_the_command_line(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(["--as", "mark", "runner", "github", "mpm", "--file", self.file, "--test", "make test"])
        text = out.getvalue()
        self.assertIn(f"wrote {self.file}", text)
        self.assertIn("repository secret ANTHROPIC_API_KEY", text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(["--as", "mark", "runner", "github", "mpm", "--print"])
        self.assertTrue(out.getvalue().startswith("# The MaximizePM agent job of pipeline mpm"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(["--as", "mark", "runner", "list"])
        self.assertIn("GitHub Actions (github): the job maxpm-agent", out.getvalue())
