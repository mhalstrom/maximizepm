## Work queue

This project uses MaximizePM (`maxpm`) to track work and who is doing it.
When the user says "go" (or asks you to take work from the queue), run
`maxpm go` in this folder and follow the briefing it prints, to its end.
When the user says "plan", run `maxpm plan` and follow its briefing.

## Working on MaximizePM itself

- Run the tests before each commit: `python3 -m unittest discover -s tests -t .`
- Commit with explicit file paths, in your own worktree (below).
- The repository is public. Keep names, paths, and plans from other projects
  out of it; local queue data lives in `data/` and `private/`, which git ignores.
- When you finish a queue item, put the commit id in `maxpm done <id> --output`.
- Before you change the page (`river/static`), read `river/static/components/README.md`: which
  part for which element, and the tokens. `tests/test_ui_parts.py` refuses a raw button, a dialog
  of its own, or a literal color or px size in a view.
- After a commit that changes `river/*.py`, `maxpm serve` starts again by itself
  within a minute or two and runs the new code. To have it at once, run
  `maxpm serve --restart`: it waits until the page answers. Never add an item
  for a person to restart `maxpm serve`.

## The main tree is the live maxpm

`~/Projects/biggestriver` is the live maxpm: the `maxpm` command and `maxpm serve`
run the code in that folder. One conflict marker there stops maxpm for every session.

- Never run `git stash`, `merge`, `rebase` or `checkout` in the main tree, and never
  edit a file there.
- Work, rebase on `main`, and test in your own worktree:
  `git worktree add -b <branch> ../biggestriver-<branch> main`.
- Move `main` forward only with `git -C ~/Projects/biggestriver merge --ff-only <branch>`.
  When `--ff-only` refuses, rebase again in your worktree and test again. Never resolve
  a conflict in the main tree.
- Release: commit in your worktree, then `merge --ff-only` into the local `main`. Do not
  push, and do not use `--ship`. Then run `maxpm done <id>` and
  `maxpm alert <manager> "<branch> is on main" --item <id> --blocked`.
