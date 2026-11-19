#!/usr/bin/env python3
"""The downloads of a MaximizePM release on GitHub (#6186): the build of the tag, and what people can download.

    downloads.py watch --tag v1.4.0 --sha <commit> [--wait 3600] [--poll 30]
    downloads.py assets --tag v1.4.0
    downloads.py check

watch: finds the run of the workflow desktop-release for the tag (its head commit is <commit>), waits until it ends,
and prints a '::step' line for each of its jobs (checks, mac, mac-intel, publish) as it starts and ends, with the
job's link, so the pipeline of maximizepm-github shows them in its downloads branch (scripts/release/github-release.sh
prints the step downloads/tag before, and downloads/published after). Exit 0 when the run passed; 1 when it failed,
was cancelled, or did not end within --wait seconds, with the run's link.

assets: the GitHub release of the tag has both .dmg files, the code archive and manifest.json, and manifest.json
names this version and this code archive (what the app's Update reads, river/release.py). Exit 0, or 1 with what
is missing.

check: the same for the latest GitHub release, as a check of the target's watch (a 'plugin' check, maxpm guide ops):
one status line, exit 0 OK, 2 CRITICAL (a download is missing or manifest.json names another version), 3 UNKNOWN
(gh could not ask GitHub).

gh runs as it is signed in on this computer (no token in a file or a variable, policy g6); GH names another gh (the
tests' fake). Standard library only.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

WORKFLOW = "desktop-release.yml"
KINDS = {"checks": "test", "publish": "deploy"}  # every other job builds


class GhError(Exception):
    pass


def gh(*args):
    """The output of one gh command; GhError when it fails."""
    try:
        r = subprocess.run([os.environ.get("GH") or "gh", *args], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GhError(f"gh {' '.join(args[:2])}: {e}") from e
    if r.returncode != 0:
        raise GhError(f"gh {' '.join(args[:2])}: {(r.stderr or r.stdout).strip()[-300:]}")
    return r.stdout


def gh_json(*args):
    try:
        return json.loads(gh(*args) or "null")
    except ValueError as e:
        raise GhError(f"gh {' '.join(args[:2])} gave no JSON") from e


def expected(version):
    """The files that a release of version must have."""
    return [f"MaximizePM-{version}-arm64.dmg", f"MaximizePM-{version}-x64.dmg", f"MaximizePM-{version}-code.tar.gz",
            "manifest.json"]


def missing(tag):
    """What the GitHub release of tag lacks, as a list of plain reasons (empty: all there)."""
    version = tag[1:] if tag.startswith("v") else tag
    rel = gh_json("release", "view", tag, "--json", "tagName,assets,url")
    names = {a.get("name") for a in rel.get("assets") or []}
    out = [f"no {n}" for n in expected(version) if n not in names]
    if "manifest.json" in names:
        try:
            m = json.loads(gh("release", "download", tag, "-p", "manifest.json", "-O", "-"))
        except ValueError:
            m = None
        if not isinstance(m, dict):
            out.append("manifest.json is not JSON")
        else:
            if m.get("version") != version:
                out.append(f"manifest.json names version {m.get('version')!r}, not {version}")
            code = (m.get("code") or {}).get("name")
            if code != f"MaximizePM-{version}-code.tar.gz":
                out.append(f"manifest.json names the code archive {code!r}")
    return out, rel.get("url") or ""


def _name(job):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", job.get("name") or "job").strip("-") or "job"


def _state(conclusion):
    return {"success": "passed", "skipped": "skipped", "neutral": "passed"}.get(conclusion or "", "failed")


def watch(tag, sha, wait, poll, out=sys.stdout):
    """Wait for the desktop-release run of tag at sha and print its jobs as steps. Returns (ok, text)."""
    t0 = time.monotonic()
    started, ended, run, last_error = set(), set(), None, None

    def left():
        return wait - (time.monotonic() - t0)

    while True:
        try:
            if run is None:
                runs = gh_json("run", "list", "--workflow", WORKFLOW, "--branch", tag, "--limit", "20", "--json",
                               "databaseId,headSha,status,conclusion,url,createdAt") or []
                mine = [r for r in runs if r.get("headSha") == sha]
                if mine:
                    run = max(mine, key=lambda r: (r.get("createdAt") or "", r.get("databaseId") or 0))
                    print(f"downloads: the build of {tag} is {run.get('url')}", file=out, flush=True)
            if run is not None:
                v = gh_json("run", "view", str(run["databaseId"]), "--json", "status,conclusion,url,jobs")
                for j in v.get("jobs") or []:
                    name = f"downloads/build-{_name(j)}"
                    kind = KINDS.get(_name(j), "build")
                    if j.get("status") in ("in_progress", "completed") and name not in started:
                        started.add(name)
                        at = f" at={j['startedAt']}" if j.get("startedAt") and not j["startedAt"].startswith("0001") else ""
                        print(f"::step start {name} kind={kind}{at}", file=out, flush=True)
                    if j.get("status") == "completed" and name not in ended:
                        ended.add(name)
                        at = f" at={j['completedAt']}" if j.get("completedAt") else ""
                        print(f"::step end {name} {_state(j.get('conclusion'))}{at} {j.get('url') or ''}".rstrip(),
                              file=out, flush=True)
                if v.get("status") == "completed":
                    url = v.get("url") or run.get("url")
                    if v.get("conclusion") == "success":
                        return True, f"the build of {tag} passed: {url}"
                    bad = [j.get("name") for j in v.get("jobs") or [] if j.get("status") == "completed"
                           and _state(j.get("conclusion")) == "failed"]
                    return False, (f"the build of {tag} ended {v.get('conclusion') or 'with no result'}"
                                   + (f" (failed: {', '.join(bad)})" if bad else "") + f": {url}")
            last_error = None
        except GhError as e:  # GitHub did not answer this time: ask again until the time is up
            last_error = str(e)
        if left() <= 0:
            where = f": {run.get('url')}" if run else (f" ({last_error})" if last_error else "")
            what = "did not end" if run else f"did not start (no run of {WORKFLOW} for {tag} at {sha[:12]})"
            return False, f"the build of {tag} {what} within {wait}s{where}"
        time.sleep(max(0, min(poll, left())))


def main(argv=None):
    p = argparse.ArgumentParser(description="the downloads of a MaximizePM release on GitHub")
    sub = p.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("watch", help="wait for the build of a tag and print its jobs as steps")
    w.add_argument("--tag", required=True)
    w.add_argument("--sha", required=True)
    w.add_argument("--wait", type=float, default=3600)
    w.add_argument("--poll", type=float, default=30)
    a = sub.add_parser("assets", help="the release of a tag has every download")
    a.add_argument("--tag", required=True)
    sub.add_parser("check", help="the latest release has every download (a plugin check)")
    args = p.parse_args(argv)
    if args.cmd == "watch":
        ok, text = watch(args.tag, args.sha, args.wait, args.poll)
        print(f"downloads: {text}")
        return 0 if ok else 1
    if args.cmd == "assets":
        try:
            gone, url = missing(args.tag)
        except GhError as e:
            print(f"downloads: could not read the release {args.tag}: {e}")
            return 1
        if gone:
            print(f"downloads: the release {args.tag} is not complete: {'; '.join(gone)}: {url}")
            return 1
        print(f"downloads: the release {args.tag} has both .dmg files, the code archive and manifest.json: {url}")
        return 0
    try:
        tag = (gh_json("release", "view", "--json", "tagName") or {}).get("tagName")
        if not tag:
            print("UNKNOWN - GitHub names no latest release")
            return 3
        gone, url = missing(tag)
    except GhError as e:
        print(f"UNKNOWN - could not ask GitHub: {e}")
        return 3
    if gone:
        print(f"CRITICAL - the latest release {tag} lacks downloads: {'; '.join(gone)} | {url}")
        return 2
    print(f"OK - the latest release {tag} has both .dmg files, the code archive and manifest.json of {tag[1:]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
