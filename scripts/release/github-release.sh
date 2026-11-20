#!/usr/bin/env bash

# The release of maximizepm-github: the deploy hook of the target (#6185, mark 2026-11-19: 'the deployment processes
# need to run independently without an agent'). maxpm serve runs it in the main tree when the deploy item is ready,
# also after Release now on the Deploy tab; no session types a release command. The manager sets it once:
#
#   maxpm target hook maximizepm-github deploy 'bash scripts/release/github-release.sh'
#   maxpm config set release_approval draft --project deploy-maximizepm-github
#
#   github-release.sh             a minor version: 1.3.0 -> 1.4.0 (a release
#                                 of a week of work always holds features)
#   github-release.sh --patch     a patch version: 1.3.0 -> 1.3.1
#   github-release.sh --dry-run   the checks and the plan; changes nothing
#                                 (DRY_RUN=1 does the same)
#
# A release is one line of stages, and the tag is the last step (#6213, mark 2026-11-19: 'we actually want the tag to
# be a final step with a manual approval'; 'this should not be a branching deploy workflow'):
#   deploy    this script without MAXPM_APPROVED: it prepares the release, a draft, and exits 76 (HOOK_YES)
#   approval  MaximizePM adds 'Approve release maximizepm-github: ...' for the person, with the last lines of this
#             script (the draft's link, its commit and files); the person looks at the draft and approves it
#   publish   this script again, with MAXPM_APPROVED (the id of the person's yes): the tag, then the draft is published
#   watch     the target's checks, maximizepm-github/downloads with them
#
# The steps, each between '::step start' and '::step end' lines, so the pipeline of the target shows them with their
# times (#6126). Before the yes:
#   check     the main tree is on main with no change to a tracked file; the release pin (MAXPM_REV; with none, HEAD of
#             main) is an ancestor of main; origin/main is an ancestor of main (no force push)
#   version   the new version, from the version of pyproject.toml: when origin has its tag, one minor (or patch) more;
#             when origin does not, an earlier run raised it and stopped before its tag: that version goes again with
#             no second raise
#   commit    'Release vX.Y.Z (deploy #<id>)' in a detached worktree, on top of main as it is at this step: the new
#             version in pyproject.toml, river/__init__.py, desktop/package.json and its lock. Then main of the main
#             tree moves forward to it (merge --ff-only, the one git change the main tree takes). Sessions merge into
#             main all day (#6492: deploy #6211 failed when main moved during its 3 minutes of tests), so the commit
#             comes before the tests: the window is the second between the commit and the fast-forward, and when
#             main moved in it, the step makes the commit again on the new main (at most 5 tries). Merges after it
#             go on top of the release commit and wait for the next release
#   test      python3 -m unittest discover -s tests -t . in that worktree, at the release commit, with no MAXPM_
#             variable of the hook and without the browser tests, as the desktop-release workflow runs them (mark
#             2026-11-12). The tests run the tree that goes out: the pin and the commits after it on main. When they
#             fail, nothing is pushed; main keeps the version commit, and the next run releases that version with no
#             second raise
#   push      git push origin <commit>:refs/heads/main (no tag)
#   build     starts the workflow desktop-release for the commit (gh workflow run, with tag and sha); each of its jobs
#             is a step build-<job> (checks, mac, mac-intel, publish) with its link (scripts/release/downloads.py
#             watch; RELEASE_WAIT seconds, default 60 minutes, within deploy_timeout 2h). The run makes a DRAFT GitHub
#             release: nothing is public. A failed job stops the release with the run's link, except the job checks
#             (the unit tests on GitHub): it reports only, as a note on its step and a fix item in RELEASE_PROJECT
#             (default maximizepm), and the test step above is the gate (#6499, mark 2026-11-20)
#   draft     the draft of vX.Y.Z is made from the commit and has both .dmg files, the code archive and manifest.json
#   notes     gh release edit of the draft with the notes from maxpm target report <deploy id>
#             (scripts/release/release_notes.py: the report's summary, or before it is written the 'changed' line of
#             each done task of project maximizepm; then the Download part of desktop/release-notes.md)
# Then it prints what the person approves and exits 76. After the yes:
#   check     as above, and the draft's commit is on origin/main
#   version   the version of main; its draft names the commit that the person approved
#   tag       git push of the annotated tag vX.Y.Z at that commit
#   publish   the notes again from the report (its summary may be newer than the draft), then gh release edit vX.Y.Z
#             --draft=false: the release and its downloads are public
#   published the GitHub release vX.Y.Z is published and has every download of this version
#
# It fails closed: the first step that fails prints its reason and the script exits 1, so MaximizePM opens the fix
# item of the deploy for an agent, and the release starts again without the yes. It never force-pushes, never moves a
# tag, never changes a published release, and changes no file of the main tree except by the fast-forward. When the
# tag is out and its release is still a draft (the publish failed), a new run asks for the yes on that draft again.
#
# For the tests (tests/test_github_release.py): GH (the gh command), MAXPM_BIN (the maxpm command), RELEASE_TEST_CMD
# (the test command), RELEASE_WAIT and RELEASE_POLL (seconds).
#
# The body is functions that bash reads whole before main runs, and the last line exits: the fast-forward of main can
# change this file.

main() {
  set -uo pipefail
  # One stream: maxpm keeps the last lines of a hook's output, and the lines that name the failed step must come
  # before the error of a tool.
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
  local deploy="${MAXPM_DEPLOY:-}" approved="${MAXPM_APPROVED:-}" remote=origin
  local step="" scratch="" wt="" HOOK_YES=76
  local project="${RELEASE_PROJECT:-maximizepm}"

  step_start() {  # step_start <name> <kind>
    step="$1"
    echo "::step start $1 kind=$2 at=$(date -u +%FT%TZ)"
  }
  step_end() {  # step_end passed|failed|skipped
    [ -n "$step" ] && echo "::step end $step $1 at=$(date -u +%FT%TZ)"
    step=""
  }
  fix_item() {  # fix_item <title> <context>: a failure that does not stop the release becomes a fix item (#6499)
    local found=()
    [ -n "$deploy" ] && found=(--found-during "$deploy")
    "$maxpm" -q add "$project" "$1" ${found[@]+"${found[@]}"} --context "$2" \
      || echo "github-release: could not add the fix item '$1'; the release goes on"
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
  release_state() {  # release_state <tag>: sets state (draft|published|none), draft_sha and url of its GitHub release
    local line
    line="$(python3 "$here/downloads.py" state --tag "$1")" || return 1
    read -r state draft_sha url <<<"$line"
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
  [ -z "$approved" ] || echo "github-release: the person approved the draft (yes #$approved): this run tags and publishes it"
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
  local cur_tag new commit="$head" state="" draft_sha="" url="" tagged=""
  cur_tag="$(remote_tag "v$cur")" || fail "git ls-remote $remote failed"
  if [ -n "$approved" ]; then
    # After the yes: the version of main, made from the commit that the person approved (its draft names it).
    new="$cur"
    release_state "v$new" || fail "could not read the GitHub release v$new"
    [ "$state" != none ] \
      || fail "GitHub has no draft of v$new, so there is nothing approved to publish; the release starts again"
    commit="$draft_sha"
    git cat-file -e "$commit^{commit}" 2>/dev/null \
      || fail "the draft of v$new is made from $commit, which is not a commit of this repository"
    git merge-base --is-ancestor "$commit" "$remote/main" \
      || fail "the draft of v$new is made from $commit, which is not on $remote/main"
    [ -z "$cur_tag" ] || [ "$cur_tag" = "$commit" ] \
      || fail "$remote has a tag v$new at $cur_tag, not at the approved $commit; the release never moves a tag"
    [ -n "$cur_tag" ] && tagged=1
    echo "github-release: v$new, approved at $commit ($state: $url)"
  elif [ -n "$cur_tag" ]; then
    release_state "v$cur" || fail "could not read the GitHub release v$cur"
    if [ "$state" = draft ]; then
      # The tag went out and the publish did not: the person approves that draft again; nothing is built again.
      new="$cur"
      commit="$cur_tag"
      tagged=1
      echo "github-release: $remote has the tag v$cur and its release is still a draft ($url): it asks for the yes on it again"
    else
      local major minor patch
      IFS=. read -r major minor patch <<<"$cur"
      if [ "$bump" = patch ]; then
        new="$major.$minor.$((patch + 1))"
      else
        new="$major.$((minor + 1)).0"
      fi
      echo "github-release: v$cur is out; this release is v$new ($bump)"
    fi
  else
    new="$cur"
    echo "github-release: $remote has no tag v$cur: an earlier run raised the version and stopped before its tag; this release is v$new"
  fi
  if [ -z "$approved" ] && [ -z "$tagged" ]; then
    [ -z "$(remote_tag "v$new")" ] || fail "$remote has a tag v$new already, at another commit; the release never moves a tag"
  fi
  step_end passed

  if [ -n "$dry_run" ]; then
    if [ -n "$approved" ]; then
      echo "github-release: dry run: it would push the tag v$new at $commit and publish the draft of v$new"
    else
      echo "github-release: dry run: it would commit 'Release v$new (deploy #${deploy:-none})' on main when the version changes, move main to it, test it, push it to $remote main, build the downloads as a draft of v$new, write its notes and ask for the person's yes; the tag comes after the yes"
    fi
    return 0
  fi

  [ -z "$approved" ] || publish
  [ -n "$tagged" ] || prepare
  ask
}

# ---- before the yes: test, commit, push main, build the draft, write its notes
prepare() {
  bump() {  # bump: the new version in the four files of the worktree
    python3 - "$wt" "$cur" "$new" <<'PY'
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
  }

  # ---- commit: on top of main as it is now; the tests come after it (#6492)
  step_start commit build
  scratch="$(mktemp -d "${TMPDIR:-/tmp}/maxpm-release.XXXXXX")" || fail "mktemp failed"
  wt="$scratch/tree"
  git worktree add --quiet --detach "$wt" "$head" || fail "git worktree add $wt at $head failed"
  if [ "$new" != "$cur" ]; then
    local try base made=""
    for try in 1 2 3 4 5; do
      base="$(git rev-parse --verify --quiet refs/heads/main)" || fail "the branch main is gone"
      git merge-base --is-ancestor "$head" "$base" \
        || fail "main went back from $head to $base while the release ran; the release never force-moves main"
      git -C "$wt" checkout --quiet --detach "$base" || fail "git checkout of main $base in $wt failed"
      bump || fail "could not write the version $new on main $base (a commit after the check changed a version file?)"
      git -C "$wt" commit --quiet -m "Release v$new (deploy #${deploy:-none})" -- \
        pyproject.toml river/__init__.py desktop/package.json desktop/package-lock.json \
        || fail "git commit of the version $new failed"
      commit="$(git -C "$wt" rev-parse HEAD)" || fail "no commit"
      if git merge --quiet --ff-only "$commit"; then
        made=1
        break
      fi
      echo "github-release: main moved while the release committed (merge --ff-only $commit refused, try $try of 5); it commits again on the new main"
      sleep "$try"
    done
    [ -n "$made" ] || fail "main moved at each of 5 tries to move it to the release commit; the next run starts again"
    [ "$base" = "$head" ] \
      || echo "github-release: $(git rev-list --count "$head..$base") commit(s) came into main after the check; they go out too, and the tests run them"
  fi
  echo "github-release: main is at $commit (v$new)"
  step_end passed

  # ---- test: the commit that goes out
  step_start test test
  echo "github-release: $test_cmd (in $wt, at $commit, version $new)"
  (
    local v
    for v in $(compgen -e); do
      case "$v" in MAXPM_*) unset "$v" ;; esac
    done
    export MAXPM_DB="$scratch/test-queue.db" MAXPM_NO_BROWSER=1
    cd "$wt" && eval "$test_cmd"
  ) || fail "the tests failed at $commit (v$new); nothing was pushed. Fix them on main: the next run releases v$new with no second raise"
  step_end passed

  # ---- push: main only; the tag comes after the person's yes
  step_start push deploy
  git push --quiet "$remote" "$commit:refs/heads/main" || fail "git push $remote main failed (never forced)"
  echo "github-release: pushed main $commit to $remote"
  step_end passed

  # ---- build: the workflow desktop-release builds the downloads of the commit into a draft release
  step_start build build
  local since
  since="$(date -u -v-2M +%FT%TZ 2>/dev/null || date -u -d '2 minutes ago' +%FT%TZ)"
  "$gh" workflow run desktop-release.yml --ref main -f "tag=v$new" -f "sha=$commit" >/dev/null \
    || fail "gh workflow run desktop-release.yml for v$new at $commit failed"
  echo "github-release: started the draft build of v$new at $commit"
  step_end passed
  python3 "$here/downloads.py" watch --tag "v$new" --sha "$commit" --since "$since" --wait "$wait_s" --poll "$poll" \
    --noted "$scratch/noted" \
    || fail "the draft build of v$new failed; nothing is public. Fix the build: the next run of this hook builds again"
  local job job_url
  while read -r job job_url; do  # a GitHub job that reports only failed: a fix item, and the release goes on (#6499)
    fix_item "Fix the GitHub job $job that failed in the build of v$new" \
      "The job $job of the workflow desktop-release failed at $commit: $job_url. It reports only, so the release went on (mark 2026-11-20)."
  done < <(cat "$scratch/noted" 2>/dev/null)

  # ---- draft
  step_start draft check
  python3 "$here/downloads.py" assets --tag "v$new" --draft --sha "$commit" || fail "the draft of v$new is not complete"
  step_end passed

  # ---- notes, on the draft: the person reads them before the yes
  step_start notes deploy
  if [ -z "$deploy" ]; then
    echo "github-release: no deploy item (MAXPM_DEPLOY): the notes stay those of desktop/release-notes.md"
    step_end skipped
  else
    notes
    step_end passed
  fi
}

# ---- the notes of the release from the report of the deploy item: its summary (#6493), the same text the person read
notes() {
  [ -n "$scratch" ] || scratch="$(mktemp -d "${TMPDIR:-/tmp}/maxpm-release.XXXXXX")" || fail "mktemp failed"
  "$maxpm" --json target report "$deploy" >"$scratch/report.json" 2>"$scratch/report.err" \
    || fail "maxpm target report $deploy failed: $(tail -n 3 "$scratch/report.err")"
  git show "$commit:desktop/release-notes.md" >"$scratch/release-notes.md" 2>/dev/null || : >"$scratch/release-notes.md"
  python3 "$here/release_notes.py" "$scratch/report.json" "$new" "$scratch/release-notes.md" >"$scratch/notes.md" \
    || fail "release_notes.py could not write the notes"
  "$gh" release edit "v$new" --notes-file "$scratch/notes.md" >/dev/null || fail "gh release edit v$new failed"
  echo "github-release: the notes of v$new come from the report of deploy #$deploy ($(wc -l <"$scratch/notes.md" | tr -d ' ') lines)"
}

# ---- the end before the yes: what the person approves (the last lines go on their item), then exit HOOK_YES
ask() {
  if [ -n "$tagged" ]; then
    step_start draft check
    python3 "$here/downloads.py" assets --tag "v$new" --draft --sha "$commit" || fail "the draft of v$new is not complete"
    step_end passed
  fi
  release_state "v$new" || fail "could not read the draft of v$new"
  cleanup
  echo "github-release: v$new is prepared and waits for your yes. Nothing is public yet."
  echo "  the draft: $url"
  echo "  its commit: $commit (on $remote main)"
  echo "  its files: MaximizePM-$new-arm64.dmg, MaximizePM-$new-x64.dmg, MaximizePM-$new-code.tar.gz, manifest.json"
  echo "  its notes: on the draft's page"
  echo "  after the yes: the tag v$new at $commit, then the draft is published"
  exit "$HOOK_YES"
}

# ---- after the yes: the tag, then the draft is published
publish() {
  if [ "$state" != published ]; then
    step_start tag deploy
    if [ -n "$tagged" ]; then
      echo "github-release: $remote has the tag v$new at $commit already"
    else
      local tag_at
      if tag_at="$(git rev-parse --verify --quiet "refs/tags/v$new^{commit}")"; then
        [ "$tag_at" = "$commit" ] || fail "the local tag v$new is at $tag_at, not at $commit; the release never moves a tag"
      else
        git tag -a "v$new" -m "MaximizePM v$new" "$commit" || fail "git tag v$new failed"
      fi
      git push --quiet "$remote" "refs/tags/v$new" || fail "git push of the tag v$new failed"
      echo "github-release: pushed the tag v$new at $commit to $remote"
    fi
    step_end passed

    step_start publish deploy
    # The summary of the report may have changed since the draft (an agent wrote it, the manager edited it): the
    # published notes are the report's text of now (#6493).
    [ -z "$deploy" ] || notes
    "$gh" release edit "v$new" --draft=false >/dev/null || fail "gh release edit v$new --draft=false failed; the tag stays"
    echo "github-release: published the release v$new: $url"
    step_end passed
  else
    echo "github-release: the release v$new is published already ($url): only the check of its downloads is left"
  fi

  step_start published check
  python3 "$here/downloads.py" assets --tag "v$new" --published || fail "the published release v$new lacks downloads"
  step_end passed

  cleanup
  echo "github-release: released v$new at $commit"
  exit 0
}

main "$@"; exit
