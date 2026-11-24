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

# A fake gh for the release script and downloads.py (#6213): 'workflow run' starts the build of a commit, which makes a
# draft release of the tag (gh.json "releases": {tag: {"draft", "sha"}}) when the run succeeds; its four jobs; the
# release's files; 'release edit' keeps the notes in notes.md and publishes a draft (--draft=false).
FAKE_GH = textwrap.dedent('''\
    import datetime, json, os, sys
    home = os.environ["FAKE_GH"]
    a = sys.argv[1:]
    with open(os.path.join(home, "gh.log"), "a") as f:
        f.write(" ".join(a) + "\\n")
    path = os.path.join(home, "gh.json")
    st = json.load(open(path))
    rels = st.setdefault("releases", {})
    def save():
        json.dump(st, open(path, "w"))
    def flag(name):
        return a[a.index(name) + 1] if name in a else None
    url = "https://github.example/actions/runs/7"
    if a[:2] == ["workflow", "run"]:
        f = dict(x.split("=", 1) for x in [a[i + 1] for i, y in enumerate(a) if y == "-f"])
        st["built"] = {"tag": f["tag"], "sha": f["sha"],
                       "at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        if st["run"] == "success":
            old = rels.get(f["tag"])
            if old and not old["draft"]:
                sys.exit("published already")
            rels[f["tag"]] = {"draft": True, "sha": f["sha"]}
        save()
    elif a[:2] == ["run", "list"]:
        b = st.get("built")
        out = [] if st["run"] == "none" or not b or b["sha"] != flag("--commit") else [
            {"databaseId": 7, "headSha": b["sha"], "status": "completed",
             "conclusion": st.get("checks") or st["run"], "url": url, "createdAt": b["at"]}]
        print(json.dumps(out))
    elif a[:2] == ["run", "view"]:
        bad = st["run"] == "failure"
        jobs = [("checks", st.get("checks") or "success"), ("mac", "failure" if bad else "success"),
                ("mac-intel", "skipped" if bad else "success"), ("publish", "skipped" if bad else "success")]
        print(json.dumps({"status": "completed", "conclusion": st.get("checks") or st["run"], "url": url, "jobs": [
            {"name": n, "status": "completed", "conclusion": c, "startedAt": "2026-11-19T10:01:00Z",
             "completedAt": "2026-11-19T10:09:00Z", "url": url + "/job/" + n} for n, c in jobs]}))
    elif a[:2] == ["release", "view"]:
        tag = a[2] if len(a) > 2 and not a[2].startswith("-") else st.get("latest")
        if st.get("down"):
            sys.exit("HTTP 502")
        rel = rels.get(tag) or ({"draft": False, "sha": "0" * 40} if tag and tag == st.get("latest") else None)
        if not rel:
            sys.exit("release not found")
        v = tag[1:]
        names = [n for n in ("manifest.json", "MaximizePM-%s-arm64.dmg" % v, "MaximizePM-%s-x64.dmg" % v,
                             "MaximizePM-%s-code.tar.gz" % v) if n not in st.get("missing", [])]
        print(json.dumps({"tagName": tag, "url": "https://github.example/releases/" + tag, "isDraft": rel["draft"],
                          "targetCommitish": rel["sha"], "assets": [{"name": n} for n in names]}))
    elif a[:2] == ["release", "download"]:
        v = st.get("manifest") or a[2][1:]
        print(json.dumps({"version": v, "code": {"name": "MaximizePM-%s-code.tar.gz" % v}}))
    elif a[:2] == ["release", "create"]:
        tag = a[2]
        old = rels.get(tag)
        if old and not old["draft"]:
            sys.exit("published already")
        rels[tag] = {"draft": "--draft" in a, "sha": flag("--target")}
        save()
    elif a[:2] == ["release", "upload"]:
        tag = a[2]
        if tag not in rels:
            sys.exit("release not found")
    elif a[:2] == ["release", "edit"]:
        if "--target" in a:
            rels[a[2]]["sha"] = flag("--target")
            save()
        if "--notes-file" in a:
            open(os.path.join(home, "notes.md"), "w").write(open(flag("--notes-file")).read())
        if "--draft=false" in a:
            if st.get("publish_fails"):
                sys.exit("HTTP 500")
            rels[a[2]]["draft"] = False
            save()
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

    def test_a_written_summary_is_the_notes(self):
        # #6493: the notes are the report's summary, so the two never differ; no task list, no queue ids.
        text = ("## What changes for you\n\n### Deploy page\n- A new Deploy tab (#2, #6).\n- Steps show (#1).\n\n"
                "## Risks and what to check\n- Restart maxpm serve; see ~/notes/x.md (#4)\n- None else, see #6.\n\n"
                "## Under the hood\n3 internal fixes.\n")
        report = dict(REPORT, summary={"text": text, "by": "maximizepm-ab12"})
        out = release_notes.build(report, "1.4.0", NOTES_MD)
        self.assertTrue(out.startswith("MaximizePM 1.4.0 ships 4 tasks.\n\n## What changes for you\n\n### Deploy page\n"
                                       "- A new Deploy tab.\n- Steps show.\n\n## Risks and what to check\n"
                                       "- None else, see.\n\n## Under the hood\n3 internal fixes.\n\n## Download"), out)
        self.assertNotIn("Pipelines show steps", out)  # no line of each task
        self.assertNotIn("#", "".join(ln for ln in out.splitlines() if not ln.startswith("#")))


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
        self.build_log = os.path.join(self.tmp, "build.log")
        _write(os.path.join(bin_, "gh"), f"#!{sys.executable}\n" + FAKE_GH, stat.S_IXUSR)
        _write(os.path.join(bin_, "maxpm"), f'#!/bin/sh\nprintf "%s|" "$@" >> {self.tmp}/maxpm.log\n'
               f"echo >> {self.tmp}/maxpm.log\ncat {self.tmp}/report.json\n", stat.S_IXUSR)
        _write(os.path.join(bin_, "release-local.sh"), textwrap.dedent(f'''\
            #!/usr/bin/env bash
            set -euo pipefail
            echo "$*" >> "{self.build_log}"
            tag="" sha=""
            while [ $# -gt 0 ]; do
              case "$1" in
                --tag) tag="$2"; shift 2 ;;
                --sha) sha="$2"; shift 2 ;;
                *) shift ;;
              esac
            done
            run_state="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("run", "success"))' "{self.tmp}/gh.json")"
            if [ "$run_state" != "success" ]; then
              echo "fake release-local: build failed ($run_state)" >&2
              exit 1
            fi
            "$GH" release create "$tag" --draft --target "$sha" --title "MaximizePM $tag"
            '''), stat.S_IXUSR)
        _write(os.path.join(self.tmp, "report.json"), json.dumps(REPORT))
        self.github()
        self.env.update({
            "GH": os.path.join(bin_, "gh"),
            "MAXPM_BIN": os.path.join(bin_, "maxpm"),
            "RELEASE_BUILD_SCRIPT": os.path.join(bin_, "release-local.sh"),
            "FAKE_GH": self.tmp,
        })

    def github(self, **state):
        """What the fake GitHub answers: run success|failure|none, missing (asset names), manifest (a version),
        publish_fails, down. Its releases and its last build stay."""
        path = os.path.join(self.tmp, "gh.json")
        old = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                old = json.load(f)
        keep = {k: old[k] for k in ("releases", "built") if k in old}
        _write(path, json.dumps(dict({"run": "success"}, **keep, **state)))

    def releases(self):
        with open(os.path.join(self.tmp, "gh.json"), encoding="utf-8") as f:
            return json.load(f).get("releases", {})

    def steps(self, out):
        return [line.split()[2] for line in out.splitlines() if line.startswith("::step start ")]

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

    def test_a_release_prepares_a_draft_asks_for_the_yes_then_tags_and_publishes_it(self):
        """#6213: before the yes the hook raises the version, pushes main, builds the downloads as a draft and writes
        its notes, then exits 76 with what the person approves; nothing is tagged or public. After the yes
        (MAXPM_APPROVED) it pushes the tag at the approved commit and publishes the draft."""
        pin = self.git("rev-parse", "HEAD")
        r = self.run_script(MAXPM_REV=pin)
        self.assertEqual(r.returncode, 76, r.stdout)
        head = self.git("rev-parse", "HEAD")
        self.assertEqual(self.git("log", "-1", "--format=%s"), "Release v1.4.0 (deploy #6047)")
        self.assertEqual(self.git("rev-parse", "HEAD^"), pin)
        self.assertEqual(self.origin_ref("refs/heads/main"), head)
        self.assertIsNone(self.origin_ref("refs/tags/v1.4.0"))  # the tag comes after the yes
        self.assertEqual(self.releases(), {"v1.4.0": {"draft": True, "sha": head}})
        with open(self.build_log, encoding="utf-8") as f:
            self.assertIn(f"--tag v1.4.0 --sha {head}", f.read())
        with open(self.gh_log, encoding="utf-8") as f:
            self.assertNotIn("workflow run", f.read())
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
        steps = ["check", "version", "commit", "test", "push", "build", "draft", "notes"]
        for name in steps:
            self.assertIn(f"::step end {name} passed ", r.stdout)
        self.assertEqual(self.steps(r.stdout), steps)
        # The last lines: what the person approves (they go on the person's item).
        tail = r.stdout.rstrip().splitlines()[-6:]
        self.assertEqual(tail[0], "github-release: v1.4.0 is prepared and waits for your yes. Nothing is public yet.")
        self.assertIn("  the draft: https://github.example/releases/v1.4.0", tail)
        self.assertIn(f"  its commit: {head} (on origin main)", tail)
        # A commit on main while the person looks: the yes publishes the commit that was approved.
        _write(os.path.join(self.repo, "later.txt"), "x\n")
        self.git("add", "later.txt")
        self.git("commit", "--quiet", "-m", "later (#2)")
        r = self.run_script(MAXPM_REV=pin, MAXPM_APPROVED="17")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.steps(r.stdout), ["check", "version", "tag", "publish", "published"])
        self.assertIn("the person approved the draft (yes #17)", r.stdout)
        self.assertEqual(self.origin_ref("refs/tags/v1.4.0^{commit}"), head)
        self.assertEqual(self.git("cat-file", "-t", "v1.4.0"), "tag")
        self.assertEqual(self.releases(), {"v1.4.0": {"draft": False, "sha": head}})
        self.assertEqual(self.origin_ref("refs/heads/main"), head)  # the later commit waits for the next release
        self.assertTrue(r.stdout.rstrip().endswith(f"github-release: released v1.4.0 at {head}"), r.stdout)
        # A second run after the yes (a cut run) only checks the published downloads.
        r = self.run_script(MAXPM_APPROVED="17")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.steps(r.stdout), ["check", "version", "published"])

    def test_a_yes_with_no_draft_publishes_nothing(self):
        r = self.run_script(MAXPM_APPROVED="17")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("GitHub has no draft of v1.3.0", r.stdout)
        self.assertIsNone(self.origin_ref("refs/tags/v1.4.0"))

    def test_a_failed_build_stops_the_release_and_makes_no_tag(self):
        self.github(run="failure")
        r = self.run_script()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("::step end build failed ", r.stdout)
        self.assertIn("the draft build of v1.4.0 failed", r.stdout)
        self.assertIn("nothing is public", r.stdout)
        self.assertNotIn("::step start notes", r.stdout)
        self.assertIsNone(self.origin_ref("refs/tags/v1.4.0"))
        self.assertEqual(self.releases(), {})
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "notes.md")))

    def test_downloads_watch_treats_failed_github_test_job_as_report_only(self):
        # #6499: downloads.py watch (still kept for manual desktop-release.yml runs) treats a failed checks job as
        # report-only and writes it to --noted.
        script = os.path.join(ROOT, "scripts", "release", "downloads.py")
        sha = self.git("rev-parse", "HEAD")
        self.github(run="success", checks="failure", built={"tag": "v1.4.0", "sha": sha, "at": "2026-11-19T10:00:00Z"})
        noted = os.path.join(self.tmp, "noted")
        r = subprocess.run([sys.executable, script, "watch", "--tag", "v1.4.0", "--sha", sha,
                            "--wait", "0", "--poll", "0", "--noted", noted],
                           cwd=self.repo, env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("::step end build-checks failed at=2026-11-19T10:09:00Z "
                      "https://github.example/actions/runs/7/job/checks report only: the release goes on", r.stdout)
        self.assertIn("::step end build-publish passed", r.stdout)
        with open(noted, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "checks https://github.example/actions/runs/7/job/checks")

    def test_a_missing_download_or_a_wrong_manifest_stops_the_release(self):
        for state, words in ((dict(missing=["MaximizePM-1.4.0-x64.dmg"]), "no MaximizePM-1.4.0-x64.dmg"),
                             (dict(manifest="1.3.0"), "manifest.json names version '1.3.0', not 1.4.0")):
            with self.subTest(words):
                self.github(**state)
                r = self.run_script()
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn("::step end draft failed ", r.stdout)
                self.assertIn(words, r.stdout)
                self.assertNotIn("::step start notes", r.stdout)
        # Then GitHub has every file: a new run builds the draft of the same version again and asks for the yes.
        self.github()
        r = self.run_script()
        self.assertEqual(r.returncode, 76, r.stdout)
        self.assertIn("v1.4.0 is prepared", r.stdout)
        self.assertIsNone(self.origin_ref("refs/tags/v1.4.0"))

    def test_a_tag_whose_publish_failed_asks_for_the_yes_on_its_draft_again(self):
        self.assertEqual(self.run_script().returncode, 76)
        head = self.git("rev-parse", "HEAD")
        self.github(publish_fails=True)
        r = self.run_script(MAXPM_APPROVED="17")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("::step end publish failed ", r.stdout)
        self.assertEqual(self.origin_ref("refs/tags/v1.4.0^{commit}"), head)
        # The release starts again without the yes: no build, no new version; the yes on the same draft.
        self.github()
        r = self.run_script()
        self.assertEqual(r.returncode, 76, r.stdout)
        self.assertEqual(self.steps(r.stdout), ["check", "version", "draft"])
        self.assertIn("its release is still a draft", r.stdout)
        r = self.run_script(MAXPM_APPROVED="18")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("has the tag v1.4.0 at", r.stdout)
        self.assertEqual(self.releases()["v1.4.0"], {"draft": False, "sha": head})

    def test_the_watch_check_of_the_latest_release(self):
        script = os.path.join(ROOT, "scripts", "release", "downloads.py")
        cases = ((dict(latest="v1.3.0"), 0, "OK - the latest release v1.3.0 has both .dmg files"),
                 (dict(latest="v1.3.0", missing=["manifest.json"]), 2, "CRITICAL - the latest release v1.3.0 lacks "
                                                                       "downloads: no manifest.json"),
                 (dict(latest="v1.3.0", manifest="1.2.1"), 2, "names version '1.2.1'"),
                 (dict(down=True), 3, "UNKNOWN - could not ask GitHub"))
        for state, code, words in cases:
            with self.subTest(words):
                self.github(**state)
                r = subprocess.run([sys.executable, script, "check"], cwd=self.repo, env=self.env, capture_output=True,
                                   text=True, timeout=60)
                self.assertEqual(r.returncode, code, r.stdout + r.stderr)
                self.assertIn(words, r.stdout.splitlines()[0])

    def test_patch(self):
        r = self.run_script("--patch")
        self.assertEqual(r.returncode, 76, r.stdout)
        self.assertIn("v1.3.1", self.releases())

    def test_a_failed_test_stops_before_any_push_and_the_next_run_keeps_the_version(self):
        """#6492: the version commit comes before the tests. When they fail, nothing goes to origin; main keeps the
        commit, and the next run releases that version with no second raise."""
        before = self.git("rev-parse", "HEAD")
        r = self.run_script(RELEASE_TEST_CMD="echo 'FAILED (failures=1)'; false")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("::step end test failed", r.stdout)
        self.assertIn("the tests failed", r.stdout)
        self.assertIn("nothing was pushed", r.stdout)
        raised = self.git("rev-parse", "HEAD")
        self.assertEqual(self.git("rev-parse", "HEAD^"), before)
        self.assertEqual(self.git("log", "-1", "--format=%s"), "Release v1.4.0 (deploy #6047)")
        self.assertIsNone(self.origin_ref("refs/tags/v1.4.0"))
        self.assertNotIn(self.origin_ref("refs/heads/main"), (before, raised))
        self.assertEqual(self.git("tag", "-l", "v1.4.0"), "")
        self.assertEqual(len(self.git("worktree", "list").splitlines()), 1)
        self.assertEqual(self.releases(), {})
        r = self.run_script()
        self.assertEqual(r.returncode, 76, r.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), raised)
        self.assertEqual(self.origin_ref("refs/heads/main"), raised)

    def test_main_that_moves_during_the_commit_gets_the_release_commit_on_top(self):
        """#6492: deploy #6211 failed when a session merged into main while the release ran. A merge between the
        version commit and the fast-forward makes the release commit again on the new main; the tests then run the
        merged commit too, and it goes out."""
        hook = os.path.join(self.repo, ".git", "hooks", "post-commit")
        mark = os.path.join(self.tmp, "merged-once")
        # The first commit of the release (in its worktree) makes another session's merge into main.
        _write(hook, "#!/bin/sh\n"
                     f"[ -e '{mark}' ] && exit 0\n"
                     f"touch '{mark}'\n"
                     "unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_PREFIX\n"
                     f"cd '{self.repo}' && echo late > late.txt && git add late.txt"
                     " && git commit --quiet -m 'merged while the release ran'\n", stat.S_IXUSR)
        r = self.run_script(RELEASE_TEST_CMD="test -f late.txt && grep -q 1.4.0 pyproject.toml")
        self.assertEqual(r.returncode, 76, r.stdout)
        self.assertIn("main moved while the release committed", r.stdout)
        self.assertIn("1 commit(s) came into main after the check", r.stdout)
        head = self.git("rev-parse", "HEAD")
        self.assertEqual(self.git("log", "-1", "--format=%s"), "Release v1.4.0 (deploy #6047)")
        self.assertEqual(self.git("log", "-1", "--format=%s", "HEAD^"), "merged while the release ran")
        self.assertEqual(self.origin_ref("refs/heads/main"), head)
        self.assertEqual(self.releases(), {"v1.4.0": {"draft": True, "sha": head}})
        self.assertEqual(self.steps(r.stdout)[:4], ["check", "version", "commit", "test"])

    def test_the_tests_run_without_the_hooks_maxpm_variables(self):
        out = os.path.join(self.tmp, "env.txt")
        r = self.run_script(RELEASE_TEST_CMD=f"env > {out}", MAXPM_AGENT="manager-1", MAXPM_EVENT="deploy")
        self.assertEqual(r.returncode, 76, r.stdout)
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
        self.assertEqual(r.returncode, 76, r.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), raised)
        self.assertEqual(self.releases(), {"v1.4.0": {"draft": True, "sha": raised}})
        self.assertIsNone(self.origin_ref("refs/tags/v1.4.0"))

    def test_a_failed_build_retries_with_no_second_raise(self):
        self.github(run="none")  # the draft build fails
        r = self.run_script()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("the draft build of v1.4.0 failed", r.stdout)
        head = self.git("rev-parse", "HEAD")
        self.assertEqual(self.origin_ref("refs/heads/main"), head)
        self.github()
        r = self.run_script()
        self.assertEqual(r.returncode, 76, r.stdout)
        self.assertIn("::step end build passed", r.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)  # the same version, no second commit
        self.assertEqual(self.releases(), {"v1.4.0": {"draft": True, "sha": head}})
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
        log = ""
        if os.path.exists(self.gh_log):
            with open(self.gh_log, encoding="utf-8") as f:
                log = f.read()
        self.assertNotIn("workflow run", log)  # it may read GitHub, never change it
        self.assertNotIn("release edit", log)

    def _make_deps_commit(self, base, files, msg="deps update"):
        """Make a commit on top of base in a detached worktree, leaving main untouched."""
        wt = os.path.join(self.tmp, f"deps-wt-{len(os.listdir(self.tmp))}")
        self.git("worktree", "add", "--quiet", "--detach", wt, base)
        try:
            for rel, content in files.items():
                _write(os.path.join(wt, rel), content)
            self.git("add", "-A", cwd=wt)
            self.git("commit", "--quiet", "--allow-empty", "-m", msg, cwd=wt)
            return self.git("rev-parse", "HEAD", cwd=wt)
        finally:
            self.git("worktree", "remove", "--force", wt)

    def test_dependency_release_builds_tags_and_publishes_without_touching_main(self):
        """#6623: MAXPM_RELEASE_KIND=deps accepts a pin on top of MAXPM_DEPS_BASE (not main), makes the version commit
        on top of it, tests that tree without unreleased main commits, pushes a temporary branch (not main), builds
        the draft, and after MAXPM_APPROVED pushes the tag, publishes the draft and cleans up the temporary branch."""
        base = self.git("rev-parse", "v1.3.0^{commit}")
        main_before = self.git("rev-parse", "HEAD")
        origin_main_before = self.origin_ref("refs/heads/main")
        lock = json.dumps({
            "name": "maximizepm", "version": "1.3.0", "lockfileVersion": 3,
            "packages": {"": {"name": "maximizepm", "version": "1.3.0"}, "node_modules/x": {"version": "9.9.10"}},
        }, indent=2)
        deps_pin = self._make_deps_commit(base, {"desktop/package-lock.json": lock})
        self.git("branch", "maxpm/deps/maximizepm-github-1", deps_pin)

        # Before the yes: version 1.3.1 (patch by default), tests see 9.9.10 and NOT feature.txt from main.
        r = self.run_script(
            MAXPM_RELEASE_KIND="deps", MAXPM_DEPS_BASE=base, MAXPM_REV=deps_pin,
            RELEASE_TEST_CMD="! test -f feature.txt && grep -q 9.9.10 desktop/package-lock.json && grep -q 1.3.1 pyproject.toml",
        )
        self.assertEqual(r.returncode, 76, r.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), main_before)
        self.assertEqual(self.origin_ref("refs/heads/main"), origin_main_before)
        build_ref = f"maxpm/deps/v1.3.1-{deps_pin[:8]}"
        commit = self.origin_ref(f"refs/heads/{build_ref}")
        self.assertIsNotNone(commit)
        self.assertEqual(self.git("rev-parse", f"{commit}^"), deps_pin)
        self.assertIsNone(self.origin_ref("refs/tags/v1.3.1"))
        self.assertEqual(self.releases(), {"v1.3.1": {"draft": True, "sha": commit}})
        with open(self.build_log, encoding="utf-8") as f:
            self.assertIn(f"--tag v1.3.1 --sha {commit}", f.read())
        with open(self.gh_log, encoding="utf-8") as f:
            self.assertNotIn("workflow run", f.read())
        self.assertIn("not on main", r.stdout)
        self.assertEqual(len(self.git("worktree", "list").splitlines()), 1)

        # After the yes: tags v1.3.1 at commit, publishes draft, deletes temp branch, main still untouched.
        r = self.run_script(MAXPM_RELEASE_KIND="deps", MAXPM_DEPS_BASE=base, MAXPM_REV=deps_pin, MAXPM_APPROVED="17")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.steps(r.stdout), ["check", "version", "tag", "publish", "published"])
        self.assertEqual(self.origin_ref("refs/tags/v1.3.1^{commit}"), commit)
        self.assertEqual(self.releases(), {"v1.3.1": {"draft": False, "sha": commit}})
        self.assertIsNone(self.origin_ref(f"refs/heads/{build_ref}"))
        self.assertEqual(self.git("rev-parse", "HEAD"), main_before)
        self.assertEqual(self.origin_ref("refs/heads/main"), origin_main_before)

        # A second run after the yes only checks the published downloads.
        r = self.run_script(MAXPM_RELEASE_KIND="deps", MAXPM_DEPS_BASE=base, MAXPM_REV=deps_pin, MAXPM_APPROVED="17")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.steps(r.stdout), ["check", "version", "published"])

        # A later code release on main still bumps 1.3.0 -> 1.4.0 with no tag collision.
        r = self.run_script(MAXPM_REV=main_before)
        self.assertEqual(r.returncode, 76, r.stdout)
        self.assertIn("v1.4.0", self.releases())

    def test_dependency_release_refusals(self):
        base = self.git("rev-parse", "v1.3.0^{commit}")
        main_head = self.git("rev-parse", "HEAD")
        origin_main = self.origin_ref("refs/heads/main")
        lock = json.dumps({
            "name": "maximizepm", "version": "1.3.0", "lockfileVersion": 3,
            "packages": {"": {"name": "maximizepm", "version": "1.3.0"}, "node_modules/x": {"version": "9.9.10"}},
        }, indent=2)
        good_pin = self._make_deps_commit(base, {"desktop/package-lock.json": lock})
        two_commits = self._make_deps_commit(good_pin, {"desktop/package-lock.json": lock.replace("9.9.10", "9.9.11")})
        non_dep = self._make_deps_commit(base, {"desktop/package-lock.json": lock, "river/core.py": "# code\n"})
        empty_commit = self._make_deps_commit(base, {})

        cases = [
            ("missing MAXPM_REV", dict(MAXPM_RELEASE_KIND="deps", MAXPM_DEPS_BASE=base), "needs MAXPM_REV"),
            ("missing MAXPM_DEPS_BASE", dict(MAXPM_RELEASE_KIND="deps", MAXPM_REV=good_pin), "needs MAXPM_DEPS_BASE"),
            ("wrong parent", dict(MAXPM_RELEASE_KIND="deps", MAXPM_DEPS_BASE=base, MAXPM_REV=two_commits), "as its parent"),
            ("non-dependency file", dict(MAXPM_RELEASE_KIND="deps", MAXPM_DEPS_BASE=base, MAXPM_REV=non_dep), "non-dependency file: river/core.py"),
            ("no files changed", dict(MAXPM_RELEASE_KIND="deps", MAXPM_DEPS_BASE=base, MAXPM_REV=empty_commit), "changes no files"),
            ("unknown kind", dict(MAXPM_RELEASE_KIND="bogus"), "unknown MAXPM_RELEASE_KIND"),
        ]
        for label, env, words in cases:
            with self.subTest(label):
                r = self.run_script(**env)
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn("::step end check failed", r.stdout)
                self.assertIn(words, r.stdout)
                self.assertEqual(self.git("rev-parse", "HEAD"), main_head)
                self.assertEqual(self.origin_ref("refs/heads/main"), origin_main)
                self.assertIsNone(self.origin_ref("refs/tags/v1.3.1"))

    def test_dependency_release_retry_after_failed_test_and_build(self):
        base = self.git("rev-parse", "v1.3.0^{commit}")
        origin_main = self.origin_ref("refs/heads/main")
        lock = json.dumps({
            "name": "maximizepm", "version": "1.3.0", "lockfileVersion": 3,
            "packages": {"": {"name": "maximizepm", "version": "1.3.0"}, "node_modules/x": {"version": "9.9.10"}},
        }, indent=2)
        deps_pin = self._make_deps_commit(base, {"desktop/package-lock.json": lock})
        build_ref = f"maxpm/deps/v1.3.1-{deps_pin[:8]}"

        # Failed test: nothing pushed at all.
        r = self.run_script(MAXPM_RELEASE_KIND="deps", MAXPM_DEPS_BASE=base, MAXPM_REV=deps_pin,
                            RELEASE_TEST_CMD="false")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("::step end test failed", r.stdout)
        self.assertIsNone(self.origin_ref(f"refs/heads/{build_ref}"))
        self.assertEqual(self.origin_ref("refs/heads/main"), origin_main)

        # Failed build: temp branch was pushed; retry reuses the same commit without force-pushing.
        self.github(run="failure")
        r = self.run_script(MAXPM_RELEASE_KIND="deps", MAXPM_DEPS_BASE=base, MAXPM_REV=deps_pin)
        self.assertEqual(r.returncode, 1, r.stdout)
        first_commit = self.origin_ref(f"refs/heads/{build_ref}")
        self.assertIsNotNone(first_commit)

        self.github()
        r = self.run_script(MAXPM_RELEASE_KIND="deps", MAXPM_DEPS_BASE=base, MAXPM_REV=deps_pin)
        self.assertEqual(r.returncode, 76, r.stdout)
        self.assertIn("reusing dependency release commit", r.stdout)
        self.assertEqual(self.origin_ref(f"refs/heads/{build_ref}"), first_commit)
        self.assertEqual(self.releases(), {"v1.3.1": {"draft": True, "sha": first_commit}})

    def test_dependency_release_dry_run(self):
        base = self.git("rev-parse", "v1.3.0^{commit}")
        main_head = self.git("rev-parse", "HEAD")
        lock = json.dumps({
            "name": "maximizepm", "version": "1.3.0", "lockfileVersion": 3,
            "packages": {"": {"name": "maximizepm", "version": "1.3.0"}, "node_modules/x": {"version": "9.9.10"}},
        }, indent=2)
        deps_pin = self._make_deps_commit(base, {"desktop/package-lock.json": lock})
        r = self.run_script("--dry-run", MAXPM_RELEASE_KIND="deps", MAXPM_DEPS_BASE=base, MAXPM_REV=deps_pin)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("dry run", r.stdout)
        self.assertIn("not main", r.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), main_head)
        self.assertIsNone(self.origin_ref(f"refs/heads/maxpm/deps/v1.3.1-{deps_pin[:8]}"))


class DownloadsKeychainHelper(unittest.TestCase):
    def test_missing_checks_keychain_helper_only_when_manifest_names_it(self):
        import downloads
        orig_gh_json, orig_gh = downloads.gh_json, downloads.gh
        try:
            assets = [
                {"name": "MaximizePM-1.6.0-arm64.dmg"},
                {"name": "MaximizePM-1.6.0-x64.dmg"},
                {"name": "MaximizePM-1.6.0-code.tar.gz"},
                {"name": "manifest.json"},
            ]
            downloads.gh_json = lambda *a: {"tagName": "v1.6.0", "url": "https://example/v1.6.0",
                                            "isDraft": False, "targetCommitish": "0" * 40, "assets": list(assets)}
            # Older release manifest without keychain_helper still passes.
            downloads.gh = lambda *a: json.dumps({"version": "1.6.0",
                                                  "code": {"name": "MaximizePM-1.6.0-code.tar.gz"}})
            gone, _ = downloads.missing("v1.6.0")
            self.assertEqual(gone, [])

            # New release manifest naming keychain_helper fails when asset is missing and passes when attached.
            kh_name = "MaximizePM-1.6.0-keychain-helper-macos.tar.gz"
            downloads.gh = lambda *a: json.dumps({
                "version": "1.6.0",
                "code": {"name": "MaximizePM-1.6.0-code.tar.gz"},
                "keychain_helper": {"name": kh_name, "identifier": "com.maximizepm.keychain-helper"},
            })
            gone, _ = downloads.missing("v1.6.0")
            self.assertEqual(gone, [f"no {kh_name}"])

            assets.append({"name": kh_name})
            gone, _ = downloads.missing("v1.6.0")
            self.assertEqual(gone, [])
        finally:
            downloads.gh_json, downloads.gh = orig_gh_json, orig_gh


if __name__ == "__main__":
    unittest.main()
