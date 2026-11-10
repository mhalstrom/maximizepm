## Work queue

This project uses MaximizePM (`maxpm`) to track work and who is doing it.
When the user says "go" (or asks you to take work from the queue), run
`maxpm go` in this folder and follow the briefing it prints, to its end.

## Working on MaximizePM itself

- Run the tests before each commit: `python3 -m unittest discover -s tests -t .`
- Commit with explicit file paths, then push to `main` right away. The public
  repository must always match the latest finished item.
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
