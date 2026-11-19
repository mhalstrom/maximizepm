#!/usr/bin/env bash

# The release of maximizepm-github in one command: the deploy hook of the
# target (#6185, mark 2026-11-19: 'the deployment processes need to run
# independently without an agent'). maxpm serve runs it in the main tree when
# the deploy item is ready, also after Release now on the Deploy tab; no
# session types a release command. The manager sets it once:
#
#   maxpm target hook maximizepm-github deploy 'bash scripts/release/github-release.sh'
#
#   github-release.sh             a minor version: 1.3.0 -> 1.4.0 (a release
#                                 of a week of work always holds features)
#   github-release.sh --patch     a patch version: 1.3.0 -> 1.3.1
#   github-release.sh --dry-run   the checks and the plan; changes nothing
#                                 (DRY_RUN=1 does the same)
#
# The steps, each between '::step start' and '::step end' lines, so the
# pipeline of the target shows them with their times (#6126):
#   check     the main tree is on main with no change to a tracked file; the
#             release pin (MAXPM_REV; with none, HEAD of main) is an ancestor
#             of main; origin/main is an ancestor of main (no force push)
#   version   the new version, from the version of pyproject.toml: when
#             origin has its tag, one minor (or patch) more; when origin does
#             not, an earlier run raised it and failed later: that version
#             goes out with no second raise
#   test      a detached worktree at main with the new version in
#             pyproject.toml, river/__init__.py, desktop/package.json and its
#             lock; python3 -m unittest discover -s tests -t . runs there,
#             with no MAXPM_ variable of the hook and without the browser
#             tests, as the desktop-release workflow runs them (mark
#             2026-11-12). The tests run the tree that goes out: the pin and
#             the commits after it on main
#   commit    'Release vX.Y.Z (deploy #<id>)' on that worktree, then main of
#             the main tree forward to it (merge --ff-only, the one git
#             change the main tree takes)
#   push      git push origin <commit>:refs/heads/main
# The downloads branch (#6186): the steps named downloads/<step> show in the
# stage 'downloads' of the target's pipeline, beside the watch of main:
#   downloads/tag        git push of the annotated tag vX.Y.Z, which starts
#                        the workflow desktop-release
#   downloads/build-<job>  each job of that run (checks, mac, mac-intel,
#                        publish), with its link (scripts/release/downloads.py
#                        watch; RELEASE_WAIT seconds, default 60 minutes,
#                        within deploy_timeout 2h). A failed job stops the
#                        release with the run's link
#   downloads/published  the GitHub release has both .dmg files, the code
#                        archive and manifest.json of this version
# Then the last step of the release:
#   notes     gh release edit with the notes from maxpm target report
#             <deploy id> (scripts/release/release_notes.py: the 'changed'
#             line of each done task of project maximizepm, by goal, and the
#             Download part of desktop/release-notes.md)
#
# It fails closed: the first step that fails prints its reason and the
# script exits 1, so MaximizePM opens the fix item of the deploy for an agent.
# It never force-pushes, never moves a tag, and changes no file of the main
# tree except by the fast-forward. When origin has the tag at HEAD of main
# already (the push went out and a later step failed), a new run only waits
# for the build, checks the downloads and writes the notes.
#
# For the tests (tests/test_github_release.py): GH (the gh command),
# MAXPM_BIN (the maxpm command), RELEASE_TEST_CMD (the test command),
# RELEASE_WAIT and RELEASE_POLL (seconds).
#
# The body is one function that bash reads whole before it runs: the
# fast-forward of main can change this file.

main() {
  set -uo pipefail
  # One stream: maxpm keeps the last lines of a hook's output, and the lines
  # that name the failed step must come before the error of a tool.
  exec 2>&1

  local bump=minor dry_run="" arg
  for arg in "$@"; do
    case "$arg" in
      --patch) bump=patch ;;
      --minor) bump=minor ;;
      --dry-run) dry_run=1 ;;
      *) echo "github-release: unknown argument $arg (use --patch, --minor or --dry-run)"; return 2 ;;
    esac
  done
  [ "${DRY_RUN:-}" = 1 ] && dry_run=1

  local here tree common_dir git_dir
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || return 1
  tree="$(git rev-parse --show-toplevel)" || { echo "github-release: not in a git repository"; return 1; }
  cd "$tree" || return 1
  common_dir="$(git rev-parse --path-format=absolute --git-common-dir)" || return 1
  git_dir="$(git rev-parse --path-format=absolute --git-dir)" || return 1

  local gh="${GH:-gh}" maxpm="${MAXPM_BIN:-$tree/bin/maxpm}"
  local test_cmd="${RELEASE_TEST_CMD:-python3 -m unittest discover -s tests -t .}"
  local wait_s="${RELEASE_WAIT:-3600}" poll="${RELEASE_POLL:-30}"
  local deploy="${MAXPM_DEPLOY:-}" remote=origin
  local step="" scratch="" wt=""

  step_start() {  # step_start <name> <kind>
    step="$1"
    echo "::step start $1 kind=$2 at=$(date -u +%FT%TZ)"
  }
  step_end() {  # step_end passed|failed|skipped
    [ -n "$step" ] && echo "::step end $step $1 at=$(date -u +%FT%TZ)"
    step=""
  }
  cleanup() {
    if [ -n "$wt" ] && [ -d "$wt" ]; then
      git -C "$tree" worktree remove --force "$wt" >/dev/null 2>&1 || true
    fi
    [ -n "$scratch" ] && rm -rf "$scratch"
    git -C "$tree" worktree prune >/dev/null 2>&1 || true
  }
  fail() {  # fail <reason>: the step fails, and so does the release
    echo "github-release: $1"
    step_end failed
    cleanup
    exit 1
  }

  # ---- check
  step_start check check
  [ "$git_dir" = "$common_dir" ] \
    || fail "$tree is a worktree; the release runs only in the main tree (AGENTS.md: the main tree is the live maxpm)"
  [ "$(git symbolic-ref -q HEAD)" = refs/heads/main ] \
    || fail "the main tree is not on the branch main ($(git symbolic-ref -q --short HEAD || echo detached))"
  local unclean
  unclean="$(git status --porcelain --untracked-files=no | head -n 3 | tr '\n' ' ')"
  [ -z "$unclean" ] || fail "the main tree has uncommitted changes (${unclean% }); the release runs only on a clean main"
  git fetch --quiet "$remote" "+refs/heads/main:refs/remotes/$remote/main" \
    || fail "git fetch $remote main failed"
  local head pin
  head="$(git rev-parse HEAD)" || fail "no HEAD"
  pin="${MAXPM_REV:-}"
  if [ -z "$pin" ]; then
    pin="$head"
    echo "github-release: the release has no pin (MAXPM_REV); it releases HEAD of main, $head"
  fi
  pin="$(git rev-parse --verify --quiet "$pin^{commit}")" || fail "the release pin ${MAXPM_REV:-} is not a commit of this repository"
  git merge-base --is-ancestor "$pin" "$head" \
    || fail "the release pin $pin is not an ancestor of main ($head); cut the release again at a commit of main"
  git merge-base --is-ancestor "$remote/main" "$head" \
    || fail "$remote/main ($(git rev-parse --short "$remote/main")) is not an ancestor of main: main and $remote/main went apart; the release never force-pushes"
  local after
  after="$(git rev-list --count "$pin..$head")"
  echo "github-release: pin $pin; main $head ($after commit(s) after the pin go out with it)"
  step_end passed

  # ---- version
  step_start version check
  versions() {  # versions <dir>: the versions of the four files, one line each
    python3 - "$1" <<'PY'
import json, re, sys
d = sys.argv[1]
def grab(path, pattern):
    try:
        m = re.search(pattern, open(f"{d}/{path}", encoding="utf-8").read(), re.M)
    except OSError:
        return "missing"
    return m.group(1) if m else "none"
print(grab("pyproject.toml", r'^version = "([^"]+)"'))
print(grab("river/__init__.py", r'^__version__ = "([^"]+)"'))
for path in ("desktop/package.json", "desktop/package-lock.json"):
    try:
        j = json.load(open(f"{d}/{path}", encoding="utf-8"))
    except (OSError, ValueError):
        print("missing")
        continue
    print(j.get("version", "none"))
    if path.endswith("lock.json"):
        print(((j.get("packages") or {}).get("") or {}).get("version", "none"))
PY
  }
  local vs cur
  vs="$(versions "$tree")" || fail "could not read the version files"
  cur="$(echo "$vs" | head -n 1)"
  [ "$(echo "$vs" | sort -u | wc -l | tr -d ' ')" = 1 ] \
    || fail "the version files disagree: $(echo "$vs" | tr '\n' ' ')(pyproject.toml, river/__init__.py, desktop/package.json, its lock twice)"
  echo "$cur" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' || fail "the version $cur is not X.Y.Z"
  remote_tag() {  # remote_tag <tag>: the commit of the tag on the remote, or nothing
    git ls-remote --tags "$remote" "refs/tags/$1^{}" "refs/tags/$1" | awk '
      $2 ~ /\^\{\}$/ { peeled = $1 } $2 !~ /\^\{\}$/ { plain = $1 }
      END { if (peeled != "") print peeled; else if (plain != "") print plain }'
  }
  local cur_tag new resume=""
  cur_tag="$(remote_tag "v$cur")" || fail "git ls-remote $remote failed"
  if [ -n "$cur_tag" ] && [ "$cur_tag" = "$head" ]; then
    new="$cur"
    resume=1
    echo "github-release: $remote has v$cur at HEAD of main already: only the build, the downloads and the notes are left"
  elif [ -n "$cur_tag" ]; then
    local major minor patch
    IFS=. read -r major minor patch <<<"$cur"
    if [ "$bump" = patch ]; then
      new="$major.$minor.$((patch + 1))"
    else
      new="$major.$((minor + 1)).0"
    fi
    echo "github-release: v$cur is out; this release is v$new ($bump)"
  else
    new="$cur"
    echo "github-release: $remote has no tag v$cur: an earlier run raised the version and stopped; this release is v$new"
  fi
  if [ -z "$resume" ]; then
    [ -z "$(remote_tag "v$new")" ] || fail "$remote has a tag v$new already, at another commit; the release never moves a tag"
  fi
  step_end passed

  if [ -n "$dry_run" ]; then
    echo "github-release: dry run: it would test main with the version $new, commit 'Release v$new (deploy #${deploy:-none})' when the version changes, move main to it, push it to $remote main, push the tag v$new, wait for the build of the downloads, check them and write the release notes"
    return 0
  fi

  local commit="$head"
  if [ -z "$resume" ]; then
    # ---- test
    step_start test test
    scratch="$(mktemp -d "${TMPDIR:-/tmp}/maxpm-release.XXXXXX")" || fail "mktemp failed"
    wt="$scratch/tree"
    git worktree add --quiet --detach "$wt" "$head" || fail "git worktree add $wt at $head failed"
    if [ "$new" != "$cur" ]; then
      python3 - "$wt" "$cur" "$new" <<'PY' || fail "could not write the version $new"
import json, re, sys
d, old, new = sys.argv[1:4]
def sub(path, pattern, repl, count=1):
    p = f"{d}/{path}"
    text = open(p, encoding="utf-8").read()
    out, n = re.subn(pattern, repl, text, count=count, flags=re.M)
    if n != count:
        sys.exit(f"{path}: found {n} of {count} version lines")
    open(p, "w", encoding="utf-8").write(out)
sub("pyproject.toml", r'^version = "' + re.escape(old) + '"', f'version = "{new}"')
sub("river/__init__.py", r'^__version__ = "' + re.escape(old) + '"', f'__version__ = "{new}"')
sub("desktop/package.json", r'"version": "' + re.escape(old) + '"', f'"version": "{new}"')
sub("desktop/package-lock.json", r'"version": "' + re.escape(old) + '"', f'"version": "{new}"', count=2)
lock = json.load(open(f"{d}/desktop/package-lock.json", encoding="utf-8"))
if lock["version"] != new or lock["packages"][""]["version"] != new:
    sys.exit("desktop/package-lock.json: the two version lines are not those of the package")
PY
    fi
    echo "github-release: $test_cmd (in $wt, version $new)"
    (
      local v
      for v in $(compgen -e); do
        case "$v" in MAXPM_*) unset "$v" ;; esac
      done
      export MAXPM_DB="$scratch/test-queue.db" MAXPM_NO_BROWSER=1
      cd "$wt" && eval "$test_cmd"
    ) || fail "the tests failed at main $head with the version $new; nothing was committed or pushed"
    step_end passed

    # ---- commit
    step_start commit build
    if [ "$new" != "$cur" ]; then
      git -C "$wt" commit --quiet -m "Release v$new (deploy #${deploy:-none})" -- \
        pyproject.toml river/__init__.py desktop/package.json desktop/package-lock.json \
        || fail "git commit of the version $new failed"
      commit="$(git -C "$wt" rev-parse HEAD)" || fail "no commit"
      git merge --quiet --ff-only "$commit" \
        || fail "main moved while the release ran (merge --ff-only $commit refused); the next run starts again"
    fi
    echo "github-release: main is at $commit (v$new)"
    step_end passed

    # ---- push
    step_start push deploy
    local tag_at
    if tag_at="$(git rev-parse --verify --quiet "refs/tags/v$new^{commit}")"; then
      [ "$tag_at" = "$commit" ] || fail "the local tag v$new is at $tag_at, not at $commit; the release never moves a tag"
    else
      git tag -a "v$new" -m "MaximizePM v$new" "$commit" || fail "git tag v$new failed"
    fi
    git push --quiet "$remote" "$commit:refs/heads/main" || fail "git push $remote main failed (never forced)"
    echo "github-release: pushed main $commit to $remote"
    step_end passed

    # ---- the downloads branch (#6186): the tag starts the build of the apps
    step_start downloads/tag deploy
    git push --quiet "$remote" "refs/tags/v$new" || fail "git push of the tag v$new failed"
    echo "github-release: pushed the tag v$new to $remote"
    step_end passed
  fi

  # ---- downloads: the workflow desktop-release builds the apps and makes the GitHub release of the tag; each of
  # its jobs is a step downloads/build-<job>. A failed build stops the release with the run's link.
  python3 "$here/downloads.py" watch --tag "v$new" --sha "$commit" --wait "$wait_s" --poll "$poll" \
    || fail "the tag v$new is pushed and stays; fix the build, then a new run of this hook (after the fix, a new version) releases again"
  step_start downloads/published check
  python3 "$here/downloads.py" assets --tag "v$new" || fail "the GitHub release v$new lacks downloads"
  step_end passed

  # ---- notes
  step_start notes deploy
  if [ -z "$deploy" ]; then
    echo "github-release: no deploy item (MAXPM_DEPLOY): the notes stay those of desktop/release-notes.md"
    step_end skipped
  else
    [ -n "$scratch" ] || scratch="$(mktemp -d "${TMPDIR:-/tmp}/maxpm-release.XXXXXX")" || fail "mktemp failed"
    "$maxpm" --json target report "$deploy" >"$scratch/report.json" 2>"$scratch/report.err" \
      || fail "maxpm target report $deploy failed: $(tail -n 3 "$scratch/report.err")"
    git show "v$new:desktop/release-notes.md" >"$scratch/release-notes.md" 2>/dev/null || : >"$scratch/release-notes.md"
    python3 "$here/release_notes.py" "$scratch/report.json" "$new" "$scratch/release-notes.md" >"$scratch/notes.md" \
      || fail "release_notes.py could not write the notes"
    "$gh" release edit "v$new" --notes-file "$scratch/notes.md" >/dev/null || fail "gh release edit v$new failed"
    echo "github-release: the notes of v$new come from the report of deploy #$deploy ($(wc -l <"$scratch/notes.md" | tr -d ' ') lines)"
    step_end passed
  fi

  cleanup
  echo "github-release: released v$new at $commit"
  return 0
}

main "$@"
