# A limit in force as a state of the queue (proposal, #1659)

Status: proposal of 2026-10-08. Nothing is built. The decision at the end is
in the queue as item #1662; the build item #1661 waits on it.

It follows decision D3 C of `worker-stops.md` (#1599): a text rule now
(#1658, done), and this proposal.

## The problem

A limit of the machine is a fact that holds for every session at the same
time: no push to main while a release is checked, no build while the disk is
almost full, no load while a release gate measures times. Today the manager
writes that fact to each session, one time when it starts and one time when
it ends.

Facts of 2026-10-08 (00:00 to 20:15 UTC):

- The manager sent workers 611 notes and alerts. 473 of them (77%) name a
  push freeze, a quiet machine, or the end of a gate. They went to 51
  sessions.
- 25 releases went out. Each had a push freeze of 11 to 40 minutes, with a
  note to each of 4 to 13 sessions at the start and at the end. From 13:24
  the freeze also asked for a quiet machine while the gate ran.
- A disk limit ("no new build under 20 GB free") ran from 15:24 to 17:10.

Facts of the three days of `worker-stops.md`:

- 30 stops, 11 stands at the prompt, and 10 questions "is the freeze over?"
  came from such limits.
- A note comes too late or is not read in time. One worker's builds ran in
  two quiet times before it read the notes. One push met a new freeze that
  started 30 seconds after the last one ended. A session that starts during
  a limit gets no note at all: the note went out before it existed.
- The rule of #1658 tells a worker to keep its item and wait in
  `maxpm inbox --wait` for the manager's word. So the manager must still
  write to each session, and each note is a request at the manager's context
  size.

## The proposal

A limit is one row in the queue: a name (a short word such as `push`,
`build`, `quiet`), a reason, a scope (all projects, the projects of one
deploy target, or one project), who set it, since when, and an optional end
time. MaximizePM shows it to each session in scope. Nobody writes notes.

### Who sets a limit, and how it ends

1. **By hand**, a person or the manager:

       maxpm limit set push "review of release #1519" --target web --until 40m
       maxpm limit end push --note "release #1519 is live at <commit>: rebase, then push"
       maxpm limit                      # the limits in force

   With `--until` the limit ends by itself at that time. A worker cannot set
   or end a limit.

2. **From a release** (forms B and C). A deploy target gets a setting:

       maxpm target freeze web push          # or: push,quiet   or: off

   MaximizePM then sets `push` for the target's projects at the cut of a
   release (#1619: a reviewer takes the review, or the deployer takes the
   deploy item) and ends it when the deploy item is done or dropped. It sets
   `quiet` while `maxpm review pass` runs the review command of the release:
   MaximizePM starts and ends that command itself. This is the interval that
   the manager announces by hand today ("push freeze for release #N" to
   "freeze ended, release #N is live").

3. **From a measure** (form C). A setting `disk_min` (for example `20G`):
   `maxpm serve` reads the free disk space in each pass and sets `build`
   below it. It ends the limit at 5 GB above `disk_min`, so the limit does
   not go on and off.

### What a session sees

- The go briefing and `maxpm status` start with the limits in the session's
  scope:

      LIMIT IN FORCE: push, since 13:24Z (review of release #1519). Commit in your
      worktree; do not push to main. Wait for its end: maxpm --as <you> limit wait push

- The line at the end of each command names them: `[you: holds #12; limit:
  push since 13:24Z]`.
- `maxpm limit wait [<name>]` blocks until the limit ends. It works while the
  session holds an item (`maxpm wait` refuses then), and it renews the lease.
  It prints the end text: `ENDED: push (release #1519 is live at <commit>:
  rebase, then push)`. It replaces `maxpm inbox --wait` in the rule of #1658.
- `maxpm limit check <name>` exits 0 when the limit is not in force, and 1
  with the reason when it is. A project's pre-push hook or build script calls
  it in one line. The hook and the queue then have one source, and a
  project's own freeze file is not needed.

### Which items go gives out

No item says what it needs. A planner would have to write it on each item,
and most items have parts that do not need the limited thing: of 24 stops
for the disk or a build stop, 11 had the code done and committed.

A limit can be set with `--no-new-work`. Then go gives no new item in its
scope (role IDLE, with the reason), `maxpm wait` returns when it ends, and
`maxpm serve` starts no new session there. It is off for `push` and `quiet`.
It is on for the disk limit of form C: 13 of the 24 stops were items that the
worker could not start.

### What replaces the manager's notes

- At the start: MaximizePM writes one notice to each session in scope that
  holds an item, as the manager does now. A session in the middle of a build
  runs no maxpm command, so only a message reaches it. The manager writes
  nothing.
- At the end: no message. A session in `maxpm limit wait` returns, and each
  other session sees that the line at the end of its commands has no limit.
- A session that starts during a limit reads it in its go briefing.
- The end of a limit does not wake the manager.

For a day such as 2026-10-08 this takes away the manager's part of about 470
messages (two commands for a release in form A, none in form B) and half of
the messages to workers.

### How it fits what exists

- **The release cadence (#1571) and the cut (#1619):** the cut is already
  the time when the content of a release is fixed. The limit `push` uses it
  as its start. A cadence makes fewer releases, so fewer limits.
  `maxpm target release-now` does not change.
- **The STOP rule:** a restart of tmux or of the computer stays a stop
  request to each session. A limit never ends a session.
- **The rule of #1658:** its text stays ("a limit does not block your item"),
  with `maxpm limit wait` in place of `maxpm inbox --wait`, and without "the
  manager tells every session".
- **Messages to the manager (#1608):** a worker that waits in
  `maxpm limit wait` is not blocked on the manager, so its earlier messages
  keep their level.

## Decision

**Decision 1 of 1: the form of the limit state** (item #1662)

- **What you decide:** How much of the proposal is built first? Answer with a
  letter.
- **Why it matters:** The manager's notes on limits were 77% of its messages
  to workers on 2026-10-08, and workers stopped or stood still 41 times in
  three days because each one read those notes in its own way.
- **Options:**
  - A. By hand only: `maxpm limit set`, `end`, `wait`, `check`, the lines in
    the briefing and at the end of each command, and the start notice from
    MaximizePM. The manager runs two commands for each release. Cost: one
    build item, smaller than #1608. Risk: the manager forgets `limit end`;
    `--until` covers that.
  - B. A, and limits from a release: `maxpm target freeze <target>
    push|push,quiet`. The manager runs nothing for a release. Cost: one more
    part in the same item, on top of the cut of #1619. Risk: a project that
    wants pushes during a release leaves the setting off.
  - C. B, and the disk limit from `maxpm serve` (`disk_min`), with no new
    work while it is in force. Cost: one more setting and one more pass of
    serve. Risk: a wrong threshold stops new work on the computer; the
    setting is off until you set it.
  - D. Nothing more. The text rule of #1658 stays, with the manager's notes.
- **My recommendation:** B. Releases caused 25 of the 26 limits of
  2026-10-08, and MaximizePM already knows when each one starts and ends.
  The disk limit can come later with the same parts.
- **Your answer:** "A", "B", "C", or "D".

In each form, a project that has a pre-push hook needs one line in it
(`maxpm limit check push`). That is a step in the project, not in MaximizePM.
