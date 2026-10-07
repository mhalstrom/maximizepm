# Manager session cost (proposal, #1100)

Status: approved 2026-10-07 (order M2, M1, then measure; compaction at 200k; M3
only if compaction loses too much). M2 and M1 are built (#1312).

## The problem

The cost report of #1096 (see `goal-context-sessions.md`) found that manager
sessions with no item held used 31% of all tokens of agent sessions: 178M of
580M input-token equivalents (eq: input + 0.1 x cache read + 2.0 x 1-hour
cache write), at 34.8k eq for each request.

## Measurements (manager sessions, 2026-09-30 to 2026-10-06)

| Session | Requests | eq | Largest context | Mean context | Cost above 150k | Compactions |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 2,033 | 78.9M | 667k | 371k | 97% | 2 |
| 2 | 1,429 | 55.3M | 667k | 367k | 98% | 2 |
| 3 | 663 | 25.8M | 647k | 372k | 96% | 0 |
| 4 | 193 | 3.6M | 226k | 167k | 72% | 0 |
| 5 | 174 | 3.0M | 252k | 148k | 59% | 0 |
| All | 4,492 | 166.6M | | | | |

Subagents of these sessions add 11M more.

The three long sessions ran near 370k context on average. Claude Code
compacted them only near 667k.

### What starts a manager's turns

Each turn starts with a message from the person, or with the end of a
background command. The share of the cost of the turns that each one starts:

| Turn starts with | Share of cost, by session |
|---|---|
| A message from the person | 24% to 34% |
| The end of a background command | 64% to 76% |

The background commands that woke the managers, over all five sessions:

| Background command | Wakes | eq |
|---|---:|---:|
| `maxpm manage --watch` (a finding, the time limit, or a restart) | about 360 | 48M |
| A separate wait for messages to the manager (`maxpm inbox --wait` and the like) | 417 | 56M |
| Other | few | 5M |

- Each wake costs about 130k eq: the manager reads its whole context several
  times while it acts and starts the watch again.
- The manager skill says that `manage --watch` is the only watcher, and that it
  also prints messages to the manager. The managers ran a second wait for
  messages anyway. So many events woke them twice.

## Proposal

### M1. One watcher, and fewer wakes

- `maxpm inbox --wait` from the active manager refuses, and names
  `manage --watch`, which already returns for messages.
- `manage --watch` waits a short settle time (setting `manage_settle`, for
  example 2m) after the first new finding, and returns all findings at once.
  Findings often come in groups (an agent ends, then its lease runs out).
- The manager skill says again: one background command, `manage --watch`.

Expected effect: the message waits (56M, a third of the manager cost) mostly
stop. Fewer wakes for each group of findings.

### M2. A smaller context: compact at 200k

- The manager starts with `claude --autocompact 200000` (Claude Code 2.1.292
  takes 100k to 1M). A setting `manager_claude_args` holds it, apart from
  the workers' `claude_args`.
- The session stays the same: same name, same Remote Control link, same chat
  for the person. Claude Code writes the summary itself.
- MaximizePM holds the queue state, so the manager loses little: the next
  `maxpm manage` briefing shows it again.

Expected effect: the mean context falls from about 370k to about 130k (from
about 50k after a compaction up to 200k). A request then reads about a third
as much. Each compaction costs one large request, about every 150 requests.

Estimate for M1 and M2 together: the manager cost falls from 178M to about
50M in the same time, about 22% of all agent tokens.

### M3 (only if M2 loses too much): an explicit handover

If the person finds that a compacted manager forgets too much:

- At a context limit, `manage --watch` tells the manager to write a handoff
  (`maxpm manage handoff "<text>"`: open threads with the person, decisions
  waiting, what it watches) and to run `maxpm manage --handover`.
- `maxpm serve` starts a fresh manager (`start_manager`) with the handoff in
  its first briefing. The old session ends.
- Cost for the person: a new session, so a new chat and a new Remote Control
  link. That is why M2 comes first.
- MaximizePM needs the manager's context size. The transcript gives it, but
  only when MaximizePM knows the Claude session id (also needed by D6 of
  `goal-context-sessions.md`).

## Order of work

1. M2: a setting and the launch flag (small).
2. M1: the refusal, the settle time, and the skill text.
3. Measure again with the #1096 method after a day.
4. M3 only if the person asks for it.

## Questions for the person who approves

1. Is the order M2, M1, then measure, correct?
2. Is 200k the right compaction point for the manager (sessions near 500k
   still work well, but cost about 2.5 times as much for each request)?
3. Is M3 needed now, or only if M2 loses context?

## Built (#1312)

- M2: setting `manager_autocompact` (default 200k; auto: Claude Code's own
  point). Start manager adds `--autocompact 200000` to a Claude Code profile's
  command, unless `claude_args` already has an `--autocompact`. It applies to a
  manager started after the change: the manager that runs now keeps its old
  point until it ends.
- M1: `maxpm inbox --wait` refuses for the active manager and names
  `manage --watch`. `manage --watch` waits `manage_settle` (default 2m) after
  the first new finding and returns all new findings at once; a finding that
  goes away in that time wakes nothing; a message or a stop returns at once.
  The manager skill and the manage briefing say: one background command.

## Measurement before (#1312)

`agent_sessions` (the session ids of each agent) starts on 2026-10-06, so the
script `manager_cost.py` (in this folder) measures from then. It reads the
queue read-only and every transcript of an agent session. A session is a
manager's when its agent's name starts with `manager-`. "Hours with requests"
counts the clock hours in which the manager made at least one request.

Before M1 and M2 (old manager setup), to 2026-10-07 19:45 UTC:

| Day (UTC) | Manager eq | All agents eq | Share | Requests | eq per request | Mean context | Hours with requests | eq per hour |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 2026-10-06 | 12.1M | 127.3M | 10% | 405 | 29.9k | 278k | 7 | 1.73M |
| 2026-10-07 | 34.8M | 381.5M | 9% | 750 | 46.4k | 427k | 14 | 2.48M |

The #1100 baseline above (2026-09-30 to 2026-10-06, other session mapping):
34.8k eq for each request, mean context near 370k, 31% of all agent eq.

The share depends on how much the workers run on that day, so compare eq per
request, mean context, and eq per hour first.

## Measurement after

Run, after one day with a manager that started after both changes:

    python3 current_project/research/manager_cost.py <manager start, UTC> <one day later>

Expected (from the proposal): mean context near 130k, eq per request near a
third of before, and fewer wakes.
