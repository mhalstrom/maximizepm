# Goal context sessions and forked workers (proposal, #1095)

Status: proposal. Nothing here is built. A person approves it before work starts.

## The problem

A Claude Code session sends its whole context on each turn. The cache makes a
repeated prefix cheap, but a turn still costs about 0.1 of the input price for
each token in the context. A long session therefore costs more per turn as it
grows: in the sessions we looked at, 72% of the usage came from turns above
150k tokens of context.

Sessions near 500k still work well. This proposal is about the cost of each
turn, not a size limit.

Two costs repeat for each new session:

1. The first message. Claude Code writes it to the cache again in each session.
2. The project context. Each new worker reads the same files again, and these
   reads are cache writes too.

The direction: a context session builds the base context of a goal once.
Worker sessions for the goal's items start as forks of it and read that base
from the cache. Standalone items start fresh, with a project primer in the
system prompt.

## Prices used here

Anthropic bills a cache read at 0.1 of the input price and a 1-hour cache write
at 2.0 of it (a 5-minute write is 1.25). The sessions we measured write with the
1-hour TTL (`ephemeral_1h_input_tokens` in the transcript). Below, "eq" means
input-token equivalents: read x 0.1 + write x 2.0.

## Measurements (2026-10-06, Claude Code 2.1.292, `claude -p --model opus`)

All runs used a clean environment (no `MAXPM_*` or `CLAUDE*` variables), the
project folder as the working folder, and `--allowedTools "Read Grep Glob"`.
The numbers are from each request's `usage` in the session transcript.

### 1. Does a fork read the base session's prefix from the cache?

Yes. A new session id does not break the cache.

| Request | Cache read | Cache write |
|---|---:|---:|
| Base, turn 1 (cold) | 0 | 30,732 |
| Base, turn 2 (after it read 500 lines of `core.py` and 300 of `server.py`) | 30,732 | 18,901 |
| Fork 1, first turn (`--resume <base> --fork-session`) | 49,633 | 3,866 |
| Fork 2, first turn | 49,633 | 3,865 |

- The fork reads all 49,633 tokens of the base from the cache.
- The fork writes about 3.9k: the base's last answer, the new prompt, and the
  sandbox text that a resume adds again.
- Fork 2 does not read the 3.9k that fork 1 wrote. Each session's sandbox text
  names a path of its own, so the two tails differ. The cost is small.

The first turn of a fork costs 49.6k x 0.1 + 3.9k x 2 = 12.7k eq. A fresh
session that builds the same context costs about 75k eq: it writes its first
message (16.4k) and the files it reads (18.9k). The saving grows with the
size of the base.

### 2. A fresh session's first turn, with and without the flags and a primer

One-word prompt. Session 1 and session 2 ran in the project folder, session 3
in a git worktree of it. The primer was a 20 KB Markdown file (about 7.6k
tokens).

| Launch | Session 1 read / write | Session 2 read / write | Session 3 (worktree) read / write |
|---|---|---|---|
| plain | 14,242 / 16,446 | 14,242 / 16,444 | 14,242 / 17,128 |
| `--append-system-prompt-file primer` | 12,440 / 25,861 | 21,855 / 16,444 | 21,855 / 17,129 |
| same + `--exclude-dynamic-system-prompt-sections` | 0 / 38,059 | 20,874 / 17,186 | 20,874 / 17,871 |

What this shows:

- In this Claude Code version, the system prompt is already the same for every
  session of a project, also in a worktree. The dynamic parts (working folder,
  environment, git status, CLAUDE.md, the skill list, the sandbox text) go in
  the first user message.
- `--exclude-dynamic-system-prompt-sections` moves only the memory section and
  the session guidance (about 1k tokens) into the first message. It made the
  cached read 981 tokens smaller. It helps only across projects, because the
  memory path differs for each project. Do not use it inside one project.
- A primer in `--append-system-prompt-file` is a cache read for session 2 and
  later: 7.6k more read (21,855 against 14,242) and no more write. The same text
  in CLAUDE.md goes in the first message, and each session writes it again.
  The project primer therefore belongs in the system prompt, not in CLAUDE.md.
- Each fresh session writes its first message again: 16.4k tokens in `-p`
  mode, 18.9k in an interactive session of this project. The first message
  differs for each session (the sandbox text names a path of its own), so no
  launch flag can make it a cache read. A fork pays it once, in the base.

### 3. How long a base stays usable

The cache entry lives one hour after the last request that used it. Each fork
reads the base prefix and renews it. If no session uses the base for an hour,
the next fork writes the whole base again (base size x 2.0 eq). That costs
about the same as a fresh session that reads the same files.

### Not measured

- A fork that starts in another folder (a worktree). The auto mode classifier
  refused the run. Today a worker starts in the project folder and makes its
  worktree later, so a fork can start in the project folder too.
- An interactive fork with `--name` and `--remote-control`. All runs used `-p`.
- A fork with a different `--effort` from the base.

### The facts from the item

In another project's last 10 sessions, the first turn read about 29.8k from the
cache and wrote about 25k. The sessions peaked at 112k to 308k tokens over 55
to 309 turns.

## Cost of each item, measured (#1096)

From the transcripts of 148 agent sessions, 2026-09-27 to 2026-10-06. Each
request goes to the item its agent held at that time (from the claim and done
events). The groups are by session: the first item of a session, or a later
item that is related to an earlier one (a link or a shared `touches` path),
or unrelated to them.

| Group | Items | Median eq | Median turns | Median mean context | eq per turn |
|---|---:|---:|---:|---:|---:|
| First item of a session | 91 | 732k | 49 | 107k | 20.6k |
| Later item, related | 51 | 341k | 16 | 211k | 25.9k |
| Later item, unrelated | 16 | 758k | 26 | 226k | 30.6k |
| Later item, no `touches` to tell | 57 | 426k | 22 | 189k | 24.2k |

- Of all 580M eq, items used 53%, manager sessions with no item 31% (34.8k
  per turn), and agents with no item 16%.
- Turns above 150k context used 77% of the cost.
- The first turn after a wait (`maxpm wait` or `inbox --wait`) used about 7%.
- A later unrelated item costs as much as a fresh start in half the turns: it
  pays for context it does not use. A related one costs half.

## What costs the most

Each turn costs about (context x 0.1) + (new tokens x 2.0). For example:

- A session that runs 300 turns up to 300k has a mean context near 150k:
  about 15k eq for each turn.
- A worker that starts at 60k and ends at 120k after 60 turns has a mean
  context near 90k: about 9k eq for each turn.

The largest saving is short sessions: one item, or a few, for each session.
Without a base, each short session pays the start again. The fork makes a
short session with full goal context cheap to start. The base must stay lean,
because each fork reads all of it on every turn.

## Design

### D1. A handoff for each goal

- `maxpm goal handoff <goal> --file <path>` (or text) stores the goal's
  handoff: what the goal is, the decisions so far, the files that matter, and
  what is left. MaximizePM keeps the versions.
- `maxpm goal show` and the go briefing for an item of the goal show it.
- The owner keeps it current. `goal release` and `goal give` refuse when the
  handoff is older than the owner's last finished item of the goal, unless the
  owner gives `--no-handoff "<why>"`.

### D2. Sub-goals

- `maxpm goal add <name> --parent <goal>`. One level only.
- The context of a sub-goal is the parent's handoff plus its own handoff. That
  keeps each owner's context bounded.

### D3. The context session (role CONTEXT)

- When a goal has two or more ready agent items and no warm base for the
  model they need, `maxpm serve` starts a context session for the goal.
- Its briefing: read the handoff (and the parent's), the items' notes, and the
  files the items name with `--touches`. Update the handoff. Then run
  `maxpm goal base <goal> --ready`, which records the session id, the model,
  the context size, and the time. Then the session ends.
- A context session never edits files and never claims an item.
- A setting `base_max` (for example 80k tokens) limits the base. Above it, the
  base reads less.

### D4. Forked workers

- When serve or Dispatch starts a session for an item of a goal, and the goal
  has a base with the same model that a session used less than `base_warm`
  (50 minutes) ago, the command is:

  ```
  claude --resume <base> --fork-session --name <name> --model <model> "<prompt for the item>" --remote-control <name>
  ```

- Each fork renews the base's last-use time.
- Else serve starts a fresh session, as today.
- An item flag `--no-goal-context` marks a goal item that needs no goal
  context. It starts fresh.
- The goal owner is a normal session. It keeps the handoff current. It is not
  the base, because its context grows while it works.

### D5. Standalone items: a project primer

- A fresh session gets the project primer in its system prompt:
  `--append-system-prompt-file <primer>`. The existing setting `claude_args`
  can do this today for each project, with no code change.
- The primer lives in `private/primers/<project>.md`, at most about 8k tokens.
  A planner keeps it current.
- No `--exclude-dynamic-system-prompt-sections` (see measurement 2).

### D6. Measure the result

- When an item is done, MaximizePM reads the usage of the sessions that worked
  on it from their transcripts. It stores the cache read, cache write, and
  output tokens, and how each session started (fork or fresh).
- `maxpm` then shows the cost of each item, so we can compare forks with
  fresh starts.

## Order of work

1. D5: settings only.
2. D6: the measurement, so the next steps show their effect.
3. D1: the handoff.
4. D3 and D4: the context session and the forked workers.
5. D2: sub-goals.

## Questions for the person who approves

1. Is the order of work above correct?
2. Where does the handoff live: in the MaximizePM database, or as a file in
   the project folder?
3. Is the base a separate context session (this proposal), or the goal
   owner's own session?
