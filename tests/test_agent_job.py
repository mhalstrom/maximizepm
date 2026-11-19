"""The agent job in a forge's own CI (#6152): maxpm runner github writes .github/workflows/agent.yml and moves the
pipeline's stage-failed rules there; GitHub starts the job when a stage's workflow fails, and the github plugin's poll
brings its state, proof and transcript back. GitHub is the fake API of tests/test_pipeline.py."""

import base64
import contextlib
import io
import json
import os
import subprocess
import zipfile

from river import cli, core, runners
from river.core import RiverError
from river.pipeline import agent_job, define, triggers

from tests.test_pipeline import Base, FakeGitHub, FakeGitLab, FakePulumi, gh_run, gl_job, pd_run, plugin_hooks

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


class AgentJobGitLab(Base):
    """The same in GitLab CI (#6172): maxpm runner gitlab writes the job maxpm-agent (stage .post, when: on_failure)
    into .gitlab-ci.yml; the gitlab plugin finds it in the failed run's GitLab pipeline and reads its artifact."""

    P = "/projects/grp%2Fshop"
    CI = "stages: [test, deploy]\nunit:\n  stage: test\n  script: [make test]\n"

    def setUp(self):
        super().setUp()
        from river.pipeline import runner
        runner.SYNC["on"] = True
        self.addCleanup(runner.SYNC.update, on=False)
        self.api = FakeGitLab()
        self.gl = plugin_hooks(self, self.api)
        core.project_add(self.c, "shop")
        core.register(self.c, "mark", human=True)
        core.register(self.c, "w1")
        define.pipeline_add(self.c, "shop", project="shop")
        define.stage_set(self.c, "shop", "test", "test", workflow="test", gate="forge")
        define.stage_set(self.c, "shop", "check", "test", run="true")  # a stage of this computer
        define.forge_set(self.c, "shop", "gitlab", repo="grp/shop", secret="gl-token")
        self.p = lambda: self.c.execute("SELECT * FROM pipelines WHERE name='shop'").fetchone()
        self.pls, self.jobs = [], {}
        self.api.routes[("GET", f"{self.P}/pipelines?per_page")] = lambda path, body: (200, self.pls)
        self.api.routes[("GET", f"{self.P}/environments")] = (200, [])
        self.file = os.path.join(self.dir.name, ".gitlab-ci.yml")
        with open(self.file, "w") as f:
            f.write(self.CI)

    def setup_job(self, **kw):
        return agent_job.setup(self.c, "shop", file=self.file, actor="mark", cwd=self.dir.name, forge="gitlab", **kw)

    def pipeline(self, n, sha, jobs, status="failed", ref="main"):
        self.pls.insert(0, {"id": n, "sha": sha, "ref": ref, "status": status, "web_url": f"p/{n}"})
        self.jobs[n] = jobs
        self.api.routes[("GET", f"{self.P}/pipelines/{n}/jobs")] = lambda path, body: (200, self.jobs[n])
        self.api.routes[("GET", f"{self.P}/pipelines/{n}/test_report")] = (200, {"test_suites": []})

    def fail_tests(self, ref="main"):
        """The GitLab stage test fails for commit bbb...; the poll reads it and the rule acts."""
        self.pipeline(501, "b" * 40, [self.job(1, "unit", "test", "failed"),
                                      self.job(9, "maxpm-agent", ".post", "created")], ref=ref)
        self.gl.poll(self.c, self.p())
        triggers.serve_pass(self.c)
        return self.c.execute("SELECT * FROM runner_runs").fetchone()

    def job(self, n, name, stage, status):
        return gl_job(n, name, stage, status, at=core.iso(core.now()))  # a rule acts on what happens after it

    def agent(self, status):
        self.jobs[501][1] = self.job(9, "maxpm-agent", ".post", status)

    def test_setup_writes_the_job_into_the_file_and_moves_the_rule(self):
        res = self.setup_job(test="python3 -m unittest", max_minutes=20)
        with open(self.file) as f:
            text = f.read()
        self.assertTrue(text.startswith(self.CI + "\n# >>> maxpm-agent"))  # the person's file stays as it was
        self.assertEqual(res["workflows"], ["test"])
        for part in ("maxpm-agent:\n  stage: .post\n", "when: on_failure", "image: node:20", "timeout: 20 minutes",
                     '$CI_COMMIT_BRANCH == \\"main\\" && $CI_PROJECT_PATH == \\"grp/shop\\"',
                     'MAXPM_STAGES: "test"', "env -i HOME=", 'ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" claude -p',
                     "'Bash(git commit:*)' 'Bash(python3:*)'", "--permission-mode dontAsk",
                     "core.hooksPath=/dev/null push -o merge_request.create", "paths: [maxpm-agent/]",
                     "MAXPM_GITLAB_TOKEN"):
            self.assertIn(part, text)
        self.assertNotIn("${{", text)
        rule = triggers.rules(self.c, "shop")["rules"][0]
        self.assertEqual(rule["runner"], "gitlab")
        self.assertIn("now runs its agent in GitLab CI", res["rules"][0])
        self.assertIn("gitlab", runners.choices(self.c, self.p()))
        # Again, with another key: the job is replaced, not added twice.
        self.setup_job(secret="MY_KEY", image="python:3.12")
        with open(self.file) as f:
            text = f.read()
        self.assertEqual(text.count("# >>> maxpm-agent"), 1)
        self.assertIn('ANTHROPIC_API_KEY="$MY_KEY"', text)
        self.assertIn("image: python:3.12", text)
        self.assertTrue(text.startswith(self.CI))
        shown = agent_job.setup(self.c, "shop", show=True, actor="mark", cli="codex", forge="gitlab")
        self.assertIn('CODEX_API_KEY="$CODEX_API_KEY" codex exec --json', shown["text"])
        with self.assertRaisesRegex(RiverError, "does not read GitHub.*maxpm runner gitlab shop"):
            agent_job.setup(self.c, "shop", file=self.file, actor="mark")
        with self.assertRaisesRegex(RiverError, "MAXPM_GITLAB_TOKEN"):
            self.setup_job(secret="MAXPM_GITLAB_TOKEN")

    def test_setup_refuses_a_job_of_the_same_name_and_another_checkout(self):
        with open(self.file, "a") as f:
            f.write("maxpm-agent:\n  script: [echo mine]\n")
        with self.assertRaisesRegex(RiverError, "a job maxpm-agent of its own"):
            self.setup_job()
        repo = os.path.join(self.dir.name, "shop")
        subprocess.run(["git", "init", "-q", repo], check=True)
        subprocess.run(["git", "-C", repo, "remote", "add", "origin", "git@gitlab.com:grp/else.git"], check=True)
        with self.assertRaisesRegex(RiverError, "not of grp/shop"):
            agent_job.setup(self.c, "shop", actor="mark", cwd=repo, forge="gitlab")
        subprocess.run(["git", "-C", repo, "remote", "set-url", "origin", "git@gitlab.com:grp/shop.git"], check=True)
        with self.assertRaisesRegex(RiverError, "has no .gitlab-ci.yml"):
            agent_job.setup(self.c, "shop", actor="mark", cwd=repo, forge="gitlab")
        with open(os.path.join(repo, ".gitlab-ci.yml"), "w") as f:
            f.write(self.CI)
        res = agent_job.setup(self.c, "shop", actor="mark", cwd=repo, forge="gitlab")
        self.assertEqual(res["file"], os.path.join(os.path.realpath(repo), ".gitlab-ci.yml"))
        self.assertTrue(agent_job.same_repo("https://gitlab.com/grp/shop.git", "grp/shop", "gitlab.com"))
        self.assertTrue(agent_job.same_repo("ssh://git@git.example.com:2222/grp/sub/shop.git", "grp/sub/shop",
                                            "git.example.com"))
        self.assertFalse(agent_job.same_repo("https://github.com/grp/shop.git", "grp/shop", "gitlab.com"))

    def test_a_failure_in_gitlab_goes_to_the_job_and_its_proof_comes_back(self):
        self.setup_job()
        run = self.fail_tests()
        self.assertEqual((run["runner"], run["state"], run["forge_key"], run["agent"]),
                         ("gitlab", "queued", "gl-501-test", f"gitlab-r{run['id']}"))
        self.assertEqual(core._item(self.c, run["item"])["reserved_for"], run["agent"])
        fire = self.c.execute("SELECT result FROM pipeline_trigger_fires").fetchone()["result"]
        self.assertIn("the job maxpm-agent in GitLab CI works on it", fire)
        # GitLab starts the job in the same pipeline.
        self.agent("running")
        self.gl.poll(self.c, self.p())
        run = self.c.execute("SELECT * FROM runner_runs").fetchone()
        self.assertEqual((run["state"], run["job"], run["url"]), ("running", "gl-job-9", "u/9"))
        # The job ends: the plugin reads the two files of its artifact by path.
        self.agent("success")
        art = f"{self.P}/jobs/9/artifacts/maxpm-agent/"
        self.api.routes[("GET", art + "proof.json")] = (200, json.dumps({
            "rev": "b" * 40, "commit": "7e1a2b0" + "0" * 33, "branch": "maxpm/fix-501",
            "pr": "https://gitlab.com/grp/shop/-/merge_requests/3",
            "test": {"command": "make test", "exit": 0, "last": "OK"}, "agent_exit": 0}).encode())
        self.api.routes[("GET", art + "transcript.jsonl")] = (200, TRANSCRIPT.encode())
        self.gl.poll(self.c, self.p())
        run = self.c.execute("SELECT * FROM runner_runs").fetchone()
        self.assertEqual((run["state"], run["cost"]), ("ended", 1.1))
        self.assertIn("proof ready: commit 7e1a2b000000 on branch maxpm/fix-501, merge request "
                      "https://gitlab.com/grp/shop/-/merge_requests/3; make test: passed (OK)", run["result"])
        item = core._item(self.c, run["item"])
        self.assertIsNone(item["reserved_for"])
        notes = [m["body"] for m in self.c.execute("SELECT body FROM messages WHERE item_id=?", (item["id"],))]
        self.assertTrue(any("read the merge request" in b for b in notes))
        forge = [f for f in runners.listing(self.c)["forges"] if f["name"] == "gitlab"][0]
        self.assertEqual((forge["label"], forge["used_by"]), ("GitLab CI", ["shop"]))
        self.assertIn(".gitlab-ci.yml", forge["what"])
        n = len(self.api.calls)
        self.gl.poll(self.c, self.p())  # no open job: no read of the agent's files
        self.assertFalse([c for c in self.api.calls[n:] if "artifacts" in c[1]])

    def test_a_pipeline_that_ended_with_no_job_gives_the_task_back_at_once(self):
        self.setup_job()
        run = self.fail_tests()
        self.jobs[501] = [self.job(1, "unit", "test", "failed")]  # the commit's file had no job maxpm-agent
        self.gl.poll(self.c, self.p())
        run = self.c.execute("SELECT * FROM runner_runs").fetchone()
        self.assertEqual(run["state"], "failed")
        self.assertIn("has no job maxpm-agent", run["result"])
        self.assertIsNone(core._item(self.c, run["item"])["reserved_for"])

    def test_a_failure_on_another_branch_stays_here(self):
        self.setup_job()
        self.assertIsNone(self.fail_tests(ref="feature"))
        fire = self.c.execute("SELECT result FROM pipeline_trigger_fires").fetchone()["result"]
        self.assertIn("it failed on feature, and the job maxpm-agent runs only on main", fire)

    def test_stop_cancels_the_job_on_gitlab(self):
        self.setup_job()
        self.fail_tests()
        self.agent("running")
        self.gl.poll(self.c, self.p())
        self.api.routes[("POST", f"{self.P}/jobs/9/cancel")] = (201, {})
        run = self.c.execute("SELECT * FROM runner_runs").fetchone()
        self.assertEqual(runners.stop(self.c, run["id"], "mark", "a person looks at it")["state"], "stopped")
        self.assertIn(("POST", f"{self.P}/jobs/9/cancel", None), self.api.calls)

    def test_the_command_line(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(["--as", "mark", "runner", "gitlab", "shop", "--file", self.file, "--test", "make test"])
        text = out.getvalue()
        self.assertIn(f"into {self.file}", text)
        self.assertIn("MAXPM_GITLAB_TOKEN", text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(["--as", "mark", "runner", "gitlab", "shop", "--print"])
        self.assertTrue(out.getvalue().startswith("# >>> maxpm-agent: the MaximizePM agent job of pipeline shop"))
        with self.assertRaisesRegex(RiverError, "only a person or the manager"):
            cli.run(["--as", "w1", "runner", "gitlab", "shop", "--print"])


def _exe(folder, name, text):
    path = os.path.join(folder, name)
    with open(path, "w") as f:
        f.write("#!/bin/sh\n" + text)
    os.chmod(path, 0o755)


class PulumiAgentJob(Base):
    """The agent job in Pulumi Deployments (#6173): maxpm runner pulumi keeps the job's settings and moves the
    stage-failed rules; when a Delivery stage fails, the pulumi plugin's poll starts a deployment of the agent stack,
    reads its state, and at its end reads the proof, the transcript and the patch from its log. Pulumi is the fake API
    of tests/test_pipeline.py."""

    DEPLOYS = "/api/stacks/acme/shop-delivery/main/deployments"

    def setUp(self):
        super().setUp()
        from river.pipeline import runner
        runner.SYNC["on"] = True
        self.addCleanup(runner.SYNC.update, on=False)
        self.api = FakePulumi()
        self.pd = plugin_hooks(self, self.api)
        core.project_add(self.c, "shop")
        core.register(self.c, "mark", human=True)
        core.register(self.c, "w1")
        define.pipeline_add(self.c, "shop", project="shop")
        define.stage_set(self.c, "shop", "test", "test", workflow="checks")
        define.forge_set(self.c, "shop", "pulumi", repo="acme/shop-delivery/main", secret="pulumi-token")
        self.p = lambda: self.c.execute("SELECT * FROM pipelines WHERE name='shop'").fetchone()

    def run_row(self):
        return self.c.execute("SELECT * FROM runner_runs").fetchone()

    def fail_checks(self):
        """Stage checks of Delivery run 1 fails; the poll reads it and the rule acts."""
        self.api.routes[("GET", "/runs/1")] = (200, pd_run(1, "failed", [("checks", "failed", None)]))
        self.pd.poll(self.c, self.p())
        triggers.serve_pass(self.c)
        return self.run_row()

    def job_output(self, conf):
        """Run the job's own script in a git checkout, with fake npm, curl, timeout and claude (the agent commits a
        fix), and return what it prints: the deployment's log."""
        work = os.path.join(self.dir.name, "work")
        bin_ = os.path.join(self.dir.name, "bin")
        out = os.path.join(self.dir.name, "out")
        os.makedirs(bin_)
        subprocess.run(["git", "init", "-q", work], check=True)
        for args in (["config", "user.name", "t"], ["config", "user.email", "t@x"], ["commit", "-q", "--allow-empty",
                                                                                     "-m", "start"]):
            subprocess.run(["git", "-C", work, *args], check=True)
        _exe(bin_, "npm", "exit 0\n")
        _exe(bin_, "curl", "echo '{\"summary\": {\"number\": 1}}'\n")
        _exe(bin_, "timeout", 'shift\nexec "$@"\n')
        _exe(bin_, "claude", "test -z \"$MAXPM_GIT_TOKEN\" || exit 9\necho fixed > fix.txt\ngit add fix.txt\n"
                             "git commit -qm 'Restore the fixture'\n" + "".join(
                                 f"echo '{line}'\n" for line in TRANSCRIPT.splitlines()))
        script = agent_job.pulumi_script(conf).replace(agent_job.OUT, out)
        env = {**os.environ, "PATH": bin_ + os.pathsep + os.environ["PATH"], "MAXPM_PROMPT": "fix it",
               "MAXPM_TEST": "true", "MAXPM_GIT_TOKEN": "", "MAXPM_BRANCH": "maxpm/fix-pd-1-checks",
               "MAXPM_RUN_URL": "https://api.pulumi.com/x"}
        r = subprocess.run(["bash", "-c", script], cwd=work, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.splitlines()

    def test_setup_keeps_the_settings_and_moves_the_stage_failed_rule(self):
        with self.assertRaises(RiverError):
            agent_job.setup_pulumi(self.c, "shop", env="shop/agents", actor="w1")
        with self.assertRaisesRegex(RiverError, "--env names the Pulumi ESC environment"):
            agent_job.setup_pulumi(self.c, "shop", actor="mark")
        with self.assertRaisesRegex(RiverError, "does not read Pulumi Delivery"):
            define.pipeline_add(self.c, "plain", project="shop")
            agent_job.setup_pulumi(self.c, "plain", env="shop/agents", actor="mark")
        with self.assertRaisesRegex(RiverError, "maxpm runner pulumi shop"):
            agent_job.setup(self.c, "shop", actor="mark", show=True)
        res = agent_job.setup_pulumi(self.c, "shop", env="shop/agents", test="make test", actor="mark")
        self.assertEqual((res["stack"], res["branch"], res["stages"]), ("acme/shop-delivery/main", "main", ["checks"]))
        self.assertIn("now runs its agent in Pulumi Deployments", res["rules"][0])
        self.assertEqual(triggers.rules(self.c, "shop")["rules"][0]["runner"], "pulumi")
        self.assertEqual(agent_job.conf(self.p())["env"], "shop/agents")
        self.assertIn("agent job in Pulumi Deployments set", define.history(self.c, "shop", 1)[0]["change"])
        shown = agent_job.setup_pulumi(self.c, "shop", show=True, actor="mark", test="make test")
        self.assertIn("env -u MAXPM_GIT_TOKEN -u PULUMI_ACCESS_TOKEN timeout 30m claude -p", shown["script"])
        self.assertIn("'Bash(make:*)'", shown["script"])
        self.assertIn("pulumi", runners.choices(self.c, self.p()))

    def test_a_failed_delivery_stage_starts_a_deployment_and_its_proof_comes_back(self):
        agent_job.setup_pulumi(self.c, "shop", env="shop/agents", test="true", actor="mark")
        run = self.fail_checks()
        self.assertEqual((run["runner"], run["state"], run["forge_key"], run["agent"]),
                         ("pulumi", "queued", "pd-1-checks", f"pulumi-r{run['id']}"))
        self.assertEqual(core._item(self.c, run["item"])["reserved_for"], run["agent"])
        # The next poll starts the deployment with the request that MaximizePM made.
        self.api.routes[("POST", self.DEPLOYS)] = (202, {"id": "dep-1", "version": 3,
                                                         "consoleUrl": "https://app.pulumi.com/acme/dep-1"})
        self.pd.poll(self.c, self.p())
        body = next(c[2] for c in self.api.calls if c[:2] == ("POST", self.DEPLOYS))
        self.assertEqual((body["operation"], body["inheritSettings"]), ("preview", True))
        pre = body["operationContext"]["preRunCommands"]
        self.assertEqual(pre[1], "pulumi env run shop/agents -- bash /tmp/maxpm-agent.sh")
        script = base64.b64decode(pre[0].split()[1]).decode()
        conf = agent_job.conf(self.p())
        self.assertEqual(script, agent_job.pulumi_script(conf))
        env = body["operationContext"]["environmentVariables"]
        self.assertEqual(env["MAXPM_RUN_URL"],
                         "https://api.pulumi.com/api/preview/stacks/acme/shop-delivery/main/delivery/runs/1")
        self.assertEqual(env["MAXPM_BRANCH"], "maxpm/fix-pd-1-checks")
        self.assertIn("Stage checks of Pulumi Delivery run 1", env["MAXPM_PROMPT"])
        run = self.run_row()
        self.assertEqual((run["state"], run["job"], run["url"]),
                         ("queued", "pdd-dep-1", "https://app.pulumi.com/acme/dep-1"))
        # It runs, then ends: the plugin reads the log that the job's own script prints.
        self.api.routes[("GET", self.DEPLOYS + "/dep-1")] = (200, {"status": "running", "created": core.iso(core.now())})
        self.pd.poll(self.c, self.p())
        self.assertEqual(self.run_row()["state"], "running")
        self.assertEqual(len([c for c in self.api.calls if c[:2] == ("POST", self.DEPLOYS)]), 1)
        lines = self.job_output(conf)
        self.assertTrue(any(x.startswith("::maxpm-patch ") for x in lines))
        half = len(lines) // 2
        self.api.routes[("GET", self.DEPLOYS + "/dep-1")] = (200, {"status": "succeeded"})
        self.api.routes[("GET", self.DEPLOYS + "/dep-1/logs")] = lambda path, body: (200, (
            {"lines": [{"line": x} for x in lines[half:]]} if "continuationToken=t%202" in path else
            {"lines": [{"header": "Pre-run", "line": x} for x in lines[:half]], "nextToken": "t 2"}))
        self.pd.poll(self.c, self.p())
        run = self.run_row()
        self.assertEqual((run["state"], run["cost"]), ("ended", 1.1))
        proof = json.loads(run["proof"])
        self.assertEqual(proof["test"], {"command": "true", "exit": 0, "last": None})
        with open(proof["patch"]) as f:
            self.assertIn("Restore the fixture", f.read())
        self.assertIn("proof ready: commit", run["result"])
        item = core._item(self.c, run["item"])
        self.assertEqual((item["status"], item["reserved_for"]), ("open", None))
        notes = [m["body"] for m in self.c.execute("SELECT body FROM messages WHERE item_id=?", (item["id"],))]
        self.assertTrue(any(f"git am {proof['patch']}" in b for b in notes))
        self.assertIn(("say", "The test reads a removed file."),
                      [(x["kind"], x["text"]) for x in runners.log(self.c, run["id"])["lines"]])

    def test_no_settings_leaves_the_task_here_and_stop_cancels_the_deployment(self):
        triggers.rule_edit(self.c, "shop", "t1", runner="pulumi", actor="mark")
        self.fail_checks()
        fire = self.c.execute("SELECT result FROM pipeline_trigger_fires").fetchone()["result"]
        self.assertIn("no agent job is set for Pulumi Deployments", fire)
        self.assertIsNone(self.run_row())
        agent_job.setup_pulumi(self.c, "shop", env="shop/agents", actor="mark")
        self.api.routes[("GET", "/runs/2")] = (200, pd_run(2, "failed", [("checks", "failed", None)]))
        self.pd.poll(self.c, self.p())
        triggers.serve_pass(self.c)
        run = self.run_row()
        self.assertEqual(run["forge_key"], "pd-2-checks")
        self.api.routes[("POST", self.DEPLOYS)] = (403, {"message": "no deployment settings"})
        self.pd.poll(self.c, self.p())
        self.assertIn("Pulumi answered 403", self.run_row()["result"])  # a note on the run; the poll goes on
        self.assertIsNone(self.p()["poll_error"])
        self.api.routes[("POST", self.DEPLOYS)] = (202, {"id": "dep-2", "consoleUrl": "u", "version": 1})
        self.pd.poll(self.c, self.p())
        self.api.routes[("POST", self.DEPLOYS + "/dep-2/cancel")] = (200, {})
        res = runners.stop(self.c, run["id"], "mark", "a person looks at it")
        self.assertEqual(res["state"], "stopped")
        self.assertIn(("POST", self.DEPLOYS + "/dep-2/cancel", {}), self.api.calls)
        with core.tx(self.c):
            self.c.execute("UPDATE runner_runs SET state='queued', job=NULL, queued_at=?",
                           (core.iso(core.now() - core.timedelta(hours=1)),))
        runners.serve_pass(self.c)
        self.assertIn("no deployment of the agent job started in Pulumi within 30m", self.run_row()["result"])

    def test_the_command_line(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(["--as", "mark", "runner", "pulumi", "shop", "--env", "shop/agents"])
        self.assertIn("ESC environment shop/agents", out.getvalue())
        self.assertIn("MAXPM_GIT_TOKEN", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(["--as", "mark", "runner", "list"])
        self.assertIn("Pulumi Deployments (pulumi): a deployment of the agent stack", out.getvalue())
