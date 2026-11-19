"""scripts/release/github-release.sh, the deploy hook of maximizepm-github (#6185), and release_notes.py.

The script runs for real against a repository with a bare 'origin' in a temporary folder, a fake gh (the
GitHub release) and a fake maxpm (the release report). The test command of the release is a shell command
the test picks, so the suite does not run itself."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "release", "github-release.sh")
sys.path.insert(0, os.path.join(ROOT, "scripts", "release"))
import release_notes  # noqa: E402

REPORT = {
    "release": {"id": 6047, "target": "maximizepm-github"},
    "summary": {"text": "68 tasks in 3 groups (MaximizePM wrote this)", "by": "maxpm"},
    "groups": [
        {"key": "goal:deploy-tab", "name": "deploy-tab", "items": [2, 3]},
        {"key": "project:maximizepm", "name": "maximizepm", "items": [4, 5, 6]},
        {"key": "goal:pipelines", "name": "pipelines", "items": [1]},
    ],
    "tasks": {
        "1": {"id": 1, "status": "done", "kind": "work", "project": "maximizepm", "changed": "Pipelines show steps."},
        "2": {"id": 2, "status": "done", "kind": "work", "project": "maximizepm", "changed": "A new Deploy tab."},
        "3": {"id": 3, "status": "open", "kind": "work", "project": "maximizepm", "changed": "Not done yet."},
        "4": {"id": 4, "status": "done", "kind": "work", "project": "maximizepm", "changed": "Reads ~/notes/x.md."},
        "5": {"id": 5, "status": "done", "kind": "work", "project": "elsewhere", "changed": "Another project."},
        "6": {"id": 6, "status": "done", "kind": "work", "project": "maximizepm", "changed": "A fix of  serve."},
    },
}
NOTES_MD = "New: old text of an earlier version.\n\n## Download\n\n- `MaximizePM-{VERSION}-arm64.dmg` of {TAG}\n"

# A fake gh for the release script and downloads.py: the run of the workflow desktop-release for a tag (its head
# commit read with git from the folder it runs in), its four jobs, and the GitHub release with its files.
FAKE_GH = textwrap.dedent('''\
    import json, os, subprocess, sys
    home = os.environ["FAKE_GH"]
    a = sys.argv[1:]
    with open(os.path.join(home, "gh.log"), "a") as f:
        f.write(" ".join(a) + "\\n")
    st = json.load(open(os.path.join(home, "gh.json")))
    def flag(name):
        return a[a.index(name) + 1] if name in a else None
    def sha(tag):
        r = subprocess.run(["git", "rev-parse", "--verify", "--quiet", tag + "^{commit}"], capture_output=True, text=True)
        return r.stdout.strip()
    url = "https://github.example/actions/runs/7"
    if a[:2] == ["run", "list"]:
        head = sha(flag("--branch"))
        out = [] if st["run"] == "none" or not head else [
            {"databaseId": 7, "headSha": head, "status": "completed", "conclusion": st["run"], "url": url,
             "createdAt": "2026-11-19T10:00:00Z"}]
        print(json.dumps(out))
    elif a[:2] == ["run", "view"]:
        bad = st["run"] == "failure"
        jobs = [("checks", "success"), ("mac", "failure" if bad else "success"),
                ("mac-intel", "skipped" if bad else "success"), ("publish", "skipped" if bad else "success")]
        print(json.dumps({"status": "completed", "conclusion": st["run"], "url": url, "jobs": [
            {"name": n, "status": "completed", "conclusion": c, "startedAt": "2026-11-19T10:01:00Z",
             "completedAt": "2026-11-19T10:09:00Z", "url": url + "/job/" + n} for n, c in jobs]}))
    elif a[:2] == ["release", "view"]:
        tag = a[2] if len(a) > 2 and not a[2].startswith("-") else st.get("latest")
        if not tag or st["run"] != "success":
            sys.exit("release not found")
        v = tag[1:]
        names = [n for n in ("manifest.json", "MaximizePM-%s-arm64.dmg" % v, "MaximizePM-%s-x64.dmg" % v,
                             "MaximizePM-%s-code.tar.gz" % v) if n not in st.get("missing", [])]
        print(json.dumps({"tagName": tag, "url": "https://github.example/releases/" + tag,
                          "assets": [{"name": n} for n in names]}))
    elif a[:2] == ["release", "download"]:
        v = st.get("manifest") or a[2][1:]
        print(json.dumps({"version": v, "code": {"name": "MaximizePM-%s-code.tar.gz" % v}}))
    elif a[:2] == ["release", "edit"]:
        open(os.path.join(home, "notes.md"), "w").write(open(flag("--notes-file")).read())
    else:
        sys.exit("fake gh: no answer for " + " ".join(a))
    ''')


class Notes(unittest.TestCase):
    def test_groups_tasks_and_download(self):
        out = release_notes.build(REPORT, "1.4.0", NOTES_MD)
        self.assertTrue(out.startswith("MaximizePM 1.4.0 ships 4 tasks. The main changes:\n"))
        # Goals in report order, the tasks of no goal last; one bullet for each done task of maximizepm.
        self.assertLess(out.index("## Deploy tab"), out.index("## Pipelines"))
        self.assertLess(out.index("## Pipelines"), out.index("## Other changes"))
        self.assertIn("- A new Deploy tab.", out)
        self.assertIn("- A fix of serve.", out)
        self.assertNotIn("Not done yet", out)
        self.assertNotIn("Another project", out)
        self.assertNotIn("~/notes", out)  # a home folder stays out of the public notes
        self.assertNotIn("MaximizePM wrote this", out)  # MaximizePM's own summary only counts the tasks
        self.assertNotIn("old text", out)
        self.assertIn("## Download", out)
        self.assertIn("MaximizePM-1.4.0-arm64.dmg` of v1.4.0", out)

    def test_a_written_summary_comes_first(self):
        report = dict(REPORT, summary={"text": "This release adds the Deploy tab.", "by": "manager-1"})
        out = release_notes.build(report, "1.4.0", "")
        self.assertIn("The main changes:\n\nThis release adds the Deploy tab.\n\n## Deploy tab", out)
        self.assertNotIn("## Download", out)


def _write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    if mode:
        os.chmod(path, os.stat(path).st_mode | mode)


@unittest.skipUnless(shutil.which("bash") and shutil.which("git"), "needs bash and git")
class Script(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("MAXPM_", "GIT_"))}
        self.env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
                         "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull,
                         "GIT_CONFIG_NOSYSTEM": "1", "TMPDIR": self.tmp, "RELEASE_POLL": "0", "RELEASE_WAIT": "0",
                         "RELEASE_TEST_CMD": "test -f pyproject.toml", "MAXPM_DEPLOY": "6047"})
        self.origin = os.path.join(self.tmp, "origin.git")
        self.repo = os.path.join(self.tmp, "repo")
        self.git("init", "--quiet", "--bare", "-b", "main", self.origin, cwd=self.tmp)
        self.git("init", "--quiet", "-b", "main", self.repo, cwd=self.tmp)
        self.git("remote", "add", "origin", self.origin)
        self.versions("1.3.0")
        _write(os.path.join(self.repo, "desktop", "release-notes.md"), NOTES_MD)
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "first")
        self.git("tag", "-a", "v1.3.0", "-m", "MaximizePM v1.3.0")
        self.git("push", "--quiet", "origin", "main", "refs/tags/v1.3.0")
        _write(os.path.join(self.repo, "feature.txt"), "new\n")
        self.git("add", "feature.txt")
        self.git("commit", "--quiet", "-m", "a feature (#1)")
        # The fake gh (FAKE_GH) answers from gh.json; 'release edit' keeps the notes in notes.md.
        bin_ = os.path.join(self.tmp, "bin")
        self.gh_log = os.path.join(self.tmp, "gh.log")
        _write(os.path.join(bin_, "gh"), f"#!{sys.executable}\n" + FAKE_GH, stat.S_IXUSR)
        _write(os.path.join(bin_, "maxpm"), f"#!/bin/sh\ncat {self.tmp}/report.json\n", stat.S_IXUSR)
        _write(os.path.join(self.tmp, "report.json"), json.dumps(REPORT))
        self.github()
        self.env.update({"GH": os.path.join(bin_, "gh"), "MAXPM_BIN": os.path.join(bin_, "maxpm"), "FAKE_GH": self.tmp})

    def github(self, **state):
        """What the fake GitHub answers: run success|failure|none, missing (asset names), manifest (a version)."""
        _write(os.path.join(self.tmp, "gh.json"), json.dumps(dict({"run": "success"}, **state)))

    def git(self, *args, cwd=None):
        r = subprocess.run(["git", *args], cwd=cwd or self.repo, env=getattr(self, "env", None) or None,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def versions(self, v, lock=None):
        _write(os.path.join(self.repo, "pyproject.toml"), f'[project]\nname = "maximizepm"\nversion = "{v}"\n')
        _write(os.path.join(self.repo, "river", "__init__.py"), f'__version__ = "{v}"\n')
        _write(os.path.join(self.repo, "desktop", "package.json"),
               json.dumps({"name": "maximizepm", "version": v, "dependencies": {"x": {"version": "9.9.9"}}}, indent=2))
        _write(os.path.join(self.repo, "desktop", "package-lock.json"), json.dumps(
            {"name": "maximizepm", "version": lock or v, "lockfileVersion": 3,
             "packages": {"": {"name": "maximizepm", "version": lock or v}, "node_modules/x": {"version": "9.9.9"}}},
            indent=2))

    def run_script(self, *args, **env):
        e = dict(self.env, **env)
        return subprocess.run(["bash", SCRIPT, *args], cwd=self.repo, env=e, capture_output=True, text=True,
                              timeout=120)

    def origin_ref(self, ref):
        r = subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref], cwd=self.origin, env=self.env,
                           capture_output=True, text=True)
        return r.stdout.strip() or None

    def test_a_release_raises_the_version_pushes_main_and_the_tag_and_writes_the_notes(self):
        pin = self.git("rev-parse", "HEAD")
        r = self.run_script(MAXPM_REV=pin)
        self.assertEqual(r.returncode, 0, r.stdout)
        head = self.git("rev-parse", "HEAD")
        self.assertEqual(self.git("log", "-1", "--format=%s"), "Release v1.4.0 (deploy #6047)")
        self.assertEqual(self.git("rev-parse", "HEAD^"), pin)
        self.assertEqual(self.origin_ref("refs/heads/main"), head)
        self.assertEqual(self.origin_ref("refs/tags/v1.4.0^{commit}"), head)
        self.assertEqual(self.git("cat-file", "-t", "v1.4.0"), "tag")
        for path, line in (("pyproject.toml", 'version = "1.4.0"'), ("river/__init__.py", '__version__ = "1.4.0"')):
            with open(os.path.join(self.repo, path), encoding="utf-8") as f:
                self.assertIn(line, f.read())
        with open(os.path.join(self.repo, "desktop", "package-lock.json"), encoding="utf-8") as f:
            lock = json.load(f)
        self.assertEqual((lock["version"], lock["packages"][""]["version"], lock["packages"]["node_modules/x"]["version"]),
                         ("1.4.0", "1.4.0", "9.9.9"))
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(len(self.git("worktree", "list").splitlines()), 1)  # its worktree is gone
        with open(os.path.join(self.tmp, "notes.md"), encoding="utf-8") as f:
            notes = f.read()
        self.assertTrue(notes.startswith("MaximizePM 1.4.0 ships 4 tasks."), notes)
        self.assertIn("MaximizePM-1.4.0-arm64.dmg", notes)
        steps = ("check", "version", "test", "commit", "push", "downloads/tag", "downloads/build-checks",
                 "downloads/build-mac", "downloads/build-mac-intel", "downloads/build-publish", "downloads/published",
                 "notes")
        for name in steps:
            self.assertIn(f"::step start {name} ", r.stdout)
            self.assertIn(f"::step end {name} passed ", r.stdout)
        starts = [line.split()[2] for line in r.stdout.splitlines() if line.startswith("::step start ")]
        self.assertEqual(starts, list(steps))
        # Each job of the build names its link, which the pipeline shows on the step.
        self.assertIn("::step end downloads/build-mac passed at=2026-11-19T10:09:00Z "
                      "https://github.example/actions/runs/7/job/mac", r.stdout)
        self.assertTrue(r.stdout.rstrip().endswith(f"github-release: released v1.4.0 at {head}"), r.stdout)

    def test_a_failed_build_stops_the_release_with_the_runs_link_and_keeps_the_tag(self):
        self.github(run="failure")
        r = self.run_script()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("::step end downloads/build-mac failed ", r.stdout)
        self.assertIn("::step end downloads/build-publish skipped ", r.stdout)
        self.assertIn("the build of v1.4.0 ended failure (failed: mac): https://github.example/actions/runs/7", r.stdout)
        self.assertNotIn("::step start notes", r.stdout)
        self.assertEqual(self.origin_ref("refs/tags/v1.4.0^{commit}"), self.git("rev-parse", "HEAD"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "notes.md")))

    def test_a_missing_download_or_a_wrong_manifest_stops_the_release(self):
        for state, words in ((dict(missing=["MaximizePM-1.4.0-x64.dmg"]), "no MaximizePM-1.4.0-x64.dmg"),
                             (dict(manifest="1.3.0"), "manifest.json names version '1.3.0', not 1.4.0")):
            with self.subTest(words):
                self.github(**state)
                r = self.run_script()
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn("::step end downloads/published failed ", r.stdout)
                self.assertIn(words, r.stdout)
                self.assertNotIn("::step start notes", r.stdout)
        # Then GitHub has every file: a new run of the hook checks them again and writes the notes, with no new tag.
        self.github()
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIsNone(self.origin_ref("refs/tags/v1.5.0"))

    def test_the_watch_check_of_the_latest_release(self):
        script = os.path.join(ROOT, "scripts", "release", "downloads.py")
        cases = ((dict(latest="v1.3.0"), 0, "OK - the latest release v1.3.0 has both .dmg files"),
                 (dict(latest="v1.3.0", missing=["manifest.json"]), 2, "CRITICAL - the latest release v1.3.0 lacks "
                                                                       "downloads: no manifest.json"),
                 (dict(latest="v1.3.0", manifest="1.2.1"), 2, "names version '1.2.1'"),
                 (dict(run="failure"), 3, "UNKNOWN - could not ask GitHub"))
        for state, code, words in cases:
            with self.subTest(words):
                self.github(**state)
                r = subprocess.run([sys.executable, script, "check"], cwd=self.repo, env=self.env, capture_output=True,
                                   text=True, timeout=60)
                self.assertEqual(r.returncode, code, r.stdout + r.stderr)
                self.assertIn(words, r.stdout.splitlines()[0])

    def test_patch(self):
        r = self.run_script("--patch")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIsNotNone(self.origin_ref("refs/tags/v1.3.1"))

    def test_a_failed_test_stops_before_any_commit_or_push(self):
        before = self.git("rev-parse", "HEAD")
        r = self.run_script(RELEASE_TEST_CMD="echo 'FAILED (failures=1)'; false")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("::step end test failed", r.stdout)
        self.assertIn("the tests failed", r.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertIsNone(self.origin_ref("refs/tags/v1.4.0"))
        self.assertNotEqual(self.origin_ref("refs/heads/main"), before)
        self.assertEqual(self.git("tag", "-l", "v1.4.0"), "")
        self.assertEqual(len(self.git("worktree", "list").splitlines()), 1)

    def test_the_tests_run_without_the_hooks_maxpm_variables(self):
        out = os.path.join(self.tmp, "env.txt")
        r = self.run_script(RELEASE_TEST_CMD=f"env > {out}", MAXPM_AGENT="manager-1", MAXPM_EVENT="deploy")
        self.assertEqual(r.returncode, 0, r.stdout)
        with open(out, encoding="utf-8") as f:
            seen = dict(x.split("=", 1) for x in f.read().splitlines() if "=" in x)
        self.assertNotIn("MAXPM_AGENT", seen)
        self.assertNotIn("MAXPM_EVENT", seen)
        self.assertTrue(seen["MAXPM_DB"].startswith(self.tmp))
        self.assertEqual(seen["MAXPM_NO_BROWSER"], "1")

    def test_refusals_change_nothing(self):
        other = self.git("rev-parse", "HEAD")
        cases = {
            "a pin off main": dict(MAXPM_REV=self.git("commit-tree", "HEAD^{tree}", "-m", "elsewhere")),
            "an unknown pin": dict(MAXPM_REV="0" * 40),
        }
        for why, env in cases.items():
            with self.subTest(why):
                r = self.run_script(**env)
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn("::step end check failed", r.stdout)
                self.assertEqual(self.git("rev-parse", "HEAD"), other)
                self.assertIsNone(self.origin_ref("refs/tags/v1.4.0"))
        with open(os.path.join(self.repo, "feature.txt"), "w", encoding="utf-8") as f:
            f.write("changed\n")
        r = self.run_script()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("uncommitted changes", r.stdout)
        self.assertIsNone(self.origin_ref("refs/tags/v1.4.0"))

    def test_origin_ahead_is_never_overwritten(self):
        clone = os.path.join(self.tmp, "clone")
        self.git("clone", "--quiet", self.origin, clone, cwd=self.tmp)
        _write(os.path.join(clone, "other.txt"), "x\n")
        self.git("add", "other.txt", cwd=clone)
        self.git("commit", "--quiet", "-m", "pushed elsewhere", cwd=clone)
        self.git("push", "--quiet", "origin", "main", cwd=clone)
        theirs = self.origin_ref("refs/heads/main")
        r = self.run_script()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("never force-pushes", r.stdout)
        self.assertEqual(self.origin_ref("refs/heads/main"), theirs)

    def test_a_tag_on_origin_is_never_moved(self):
        clone = os.path.join(self.tmp, "clone")
        self.git("clone", "--quiet", self.origin, clone, cwd=self.tmp)
        self.git("tag", "-a", "v1.4.0", "-m", "elsewhere", cwd=clone)
        self.git("push", "--quiet", "origin", "refs/tags/v1.4.0", cwd=clone)
        theirs = self.origin_ref("refs/tags/v1.4.0")
        r = self.run_script()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("never moves a tag", r.stdout)
        self.assertEqual(self.origin_ref("refs/tags/v1.4.0"), theirs)

    def test_a_raised_version_that_did_not_go_out_goes_out_with_no_second_raise(self):
        self.versions("1.4.0")
        self.git("commit", "--quiet", "-am", "Release v1.4.0 (deploy #6040)")
        raised = self.git("rev-parse", "HEAD")
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), raised)
        self.assertEqual(self.origin_ref("refs/tags/v1.4.0^{commit}"), raised)
        self.assertIsNone(self.origin_ref("refs/tags/v1.5.0"))

    def test_after_the_push_a_new_run_only_waits_and_writes_the_notes(self):
        self.github(run="none")  # the build of the tag has not started within the wait
        r = self.run_script()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("the build of v1.4.0 did not start (no run of desktop-release.yml for v1.4.0", r.stdout)
        head = self.git("rev-parse", "HEAD")
        self.assertEqual(self.origin_ref("refs/tags/v1.4.0^{commit}"), head)
        self.github()
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn("::step start test", r.stdout)
        self.assertNotIn("::step start push", r.stdout)
        self.assertNotIn("::step start downloads/tag", r.stdout)
        self.assertIn("::step end downloads/build-publish passed", r.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertIsNone(self.origin_ref("refs/tags/v1.5.0"))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "notes.md")))

    def test_versions_that_disagree_are_refused(self):
        self.versions("1.3.0", lock="1.2.9")
        self.git("commit", "--quiet", "-am", "a lock of another version")
        r = self.run_script()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("the version files disagree", r.stdout)

    def test_dry_run_changes_nothing(self):
        before = self.git("rev-parse", "HEAD")
        r = self.run_script("--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("dry run", r.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertIsNone(self.origin_ref("refs/tags/v1.4.0"))
        self.assertFalse(os.path.exists(self.gh_log))


if __name__ == "__main__":
    unittest.main()
