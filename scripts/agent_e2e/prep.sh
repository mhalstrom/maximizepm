#!/bin/bash
# prep.sh <T> <tag> [worktree]: a project e2e-<tag> with a git folder <T>/w-<tag> of its own and one item for it.
# With 'worktree', the project's sessions work in MaximizePM worktrees (workspace worktree, #6339). Prints the item id.
T=$1; tag=$2
Q="$(cd "$(dirname "$0")" && pwd)/q.sh"
mkdir -p "$T/w-$tag"
git -C "$T/w-$tag" init -q
git -C "$T/w-$tag" commit -q --allow-empty -m init
bash "$Q" "$T" project add "e2e-$tag" >/dev/null 2>&1
bash "$Q" "$T" project path "e2e-$tag" "$T/w-$tag" >/dev/null 2>&1
[ "$3" = worktree ] && bash "$Q" "$T" config set workspace worktree --project "e2e-$tag" >/dev/null
bash "$Q" "$T" add "e2e-$tag" "Append the line '$tag ok' to the file ok.txt in the project folder, commit it with git, and close this item" \
  --short "Append a line and commit" --doer ai --new "agent test" 2>&1 | head -1 | sed -E 's/^#([0-9]+).*/\1/'
