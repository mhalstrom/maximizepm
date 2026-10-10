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

## Measurement after (#1313)

The manager that has both changes started on 2026-10-08 at 04:33 UTC (setting
`manager_autocompact` 200k). The numbers are its first 15.5 hours, to 20:00
UTC: 1,171 requests, more than each day before. Command:

    python3 current_project/research/manager_cost.py 2026-10-08T04:33:10+00:00 2026-10-08T20:00:00+00:00

| Day (UTC) | Manager eq | All agents eq | Share | Requests | eq per request | Mean context | Hours with requests | eq per hour |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 2026-10-08 | 19.6M | 295.7M | 7% | 1,171 | 16.7k | 128k | 16 | 1.22M |

The old manager ran until 04:22 UTC on 2026-10-08. From 2026-10-07 19:45 UTC
it had M1 (the refusal is in the command) but not M2 (it kept its old
compaction point). That gives three periods:

| | Before (2026-10-06 to 2026-10-07 19:45) | M1 only (to 2026-10-08 04:33) | M1 and M2 (to 2026-10-08 20:00) |
|---|---:|---:|---:|
| Manager eq | 47.1M | 30.5M | 19.6M |
| Share of all agents eq | 9% | 12% | 7% |
| Hours with requests | 21 | 10 | 16 |
| Requests | 1,162 | 725 | 1,171 |
| eq per request | 40.5k | 42.1k | 16.7k |
| Mean context | 278k and 425k | 382k and 418k | 128k |
| eq per hour | 2.24M | 3.05M | 1.22M |
| Requests per hour | 55 | 73 | 73 |
| Turns that a message starts | 84 | 38 | 56 |
| Turns that the end of a background command starts | 149 | 112 | 220 |
| Turns per hour | 11 | 15 | 17 |
| eq per turn | 202k | 203k | 71k |
| `maxpm manage --watch` started | 149 | 132 | 224 |
| `maxpm inbox --wait` started | 96 | 0 | 0 |
| Compactions | 2 (at 667k to 682k) | 1 (at 667k) | 18 (at 167k to 169k) |

The before column is the two rows of "Measurement before", measured again
(the 2026-10-07 row is now 35.0M, 757 requests, 46.2k, 425k: a few requests
more than at the first measurement).

### Against the expected effect

- Mean context: 128k. Expected: about 130k. Correct.
- eq per request: 16.7k, 41% of before (40.5k) and 48% of the #1100 baseline
  (34.8k). Expected: about a third. The difference: Claude Code starts a
  compaction at about 167k, not at 200k, so a compaction comes each 65
  requests, not each 150, and each one writes the cache again.
- eq per hour: 1.22M, 54% of before and 40% of the M1-only period. The
  proposal estimated 28% (178M to 50M). The manager made more requests for
  each hour than before (73, before 55), so the cost for each hour fell less
  than the cost for each request.
- M1, one watcher: correct. The manager started no second wait for messages
  (`inbox --wait`: 96 before, 0 after).
- M1, fewer wakes: not seen. A background command woke the manager 14 times
  for each hour (220 in 16 hours), against 7 before and 11 in the M1-only
  period. These days had more worker sessions and more releases, so the
  periods do not compare one to one, but the settle time of 2m did not bring
  the number down. A wake now costs about 71k eq, before about 200k: the
  saving comes from the smaller context.

### Does the compacted manager forget too much?

The transcripts cannot answer this. They show 18 compactions in 15.5 hours,
one each 52 minutes on average (the shortest time between two: 15 minutes),
and each one cuts the context from about 167k to about 21k. The person who
works with the manager decides. If it forgets too much, there are two steps:
a higher point (`maxpm config set manager_autocompact 300k`: fewer
compactions, and a higher cost for each request), or M3 (an explicit
handover).

### Script change (#1313)

`manager_cost.py` now gives a session to the agent that used it first. A
manager also runs commands with the names of other agents, and the script
then counted the manager's session for one of those. It also prints a second
table: what started each turn, the watch commands, and the compactions.

## What wakes the manager (#1582)

The measurement after M1 and M2 found 14 wakes for each hour, at about 71k eq
for each wake. This section counts their causes, with the script
`manager_wakes.py` (in this folder), for the same 15.4 hours:

    python3 current_project/research/manager_wakes.py 2026-10-08T04:33:10+00:00 2026-10-08T20:00:00+00:00

Background commands that ended: 181 watches (`maxpm manage --watch`) and 39
other commands (release steps that the manager ran as a target owner).

| The watch returned for | Wakes |
|---|---:|
| Messages | 124 |
| A finding | 55 |
| The time limit (`manage_every`, 30m) | 2 |

| Messages in the wakes | Messages | Wakes with only this one message |
|---|---:|---:|
| Note from an agent | 98 | 70 |
| Ship request notice (the manager owns a target) | 48 | 18 |
| Alert from an agent | 20 | 1 |
| Question from an agent | 14 | 14 |
| Other notice | 1 | 1 |

| Findings in the wakes | Wakes |
|---|---:|
| waiting (an agent waits longer than `wait_too_long`) | 31 |
| human (an item is ready for a person) | 16 |
| question | 5 |
| uncovered | 2 |
| stuck | 1 |

- Messages cause 69% of the wakes. A message returns the watch at once: the
  settle time of M1 (`manage_settle`, 2m) applies only to findings.
- Most message wakes carry one note. A note is a status line ("#12 is on
  main"); the manager guide says that only an alert means "read now".
- Half of the finding wakes are `waiting` and a quarter are `human`. An agent
  that waits ends by itself after `wait_max`, and a person gets a
  notification for a ready item. The manager seldom has to act on these at
  once.
- The time between two wakes is short: the median is 3.2 minutes, 72 of 180
  are under 2 minutes, and 152 are under 10 minutes.

### Proposal M4, second version (#1586): a level for each event, a wait for each level

The first version had one wait (`manage_quiet`) for all less urgent events:
86 wakes at 10m, 70 at 15m, 55 at 30m (181 now). The person who approves
asked for more: priorities. He named three forms and picked none.

All three forms give each event that goes to the manager (a message or a
finding) a level, and give each level a wait before it wakes the manager.
They differ in where the level lives and in who can set it. In each form, a
wake brings every event that waits, so no second wake follows.

Three levels are enough for the events of 2026-10-08:

| Level | Events (default, from the kind) | Highest level in a wake, of 181 wakes |
|---|---|---:|
| urgent | An alert, a question, a stop request; the findings stuck, lost, target, unconnected, uncovered, question | 40 |
| normal | A note; a notice other than a ship request | 91 |
| low | A ship request notice; the findings waiting (an agent waits) and human (an item is ready for a person) | 48 |

Two wakes came from the time limit of the watch.

#### Form 1: two inboxes, high and low

- What it is: each message is in the high inbox or in the low inbox. High
  wakes the manager at once. Low waits (he said about 30 minutes).
- In the code: a column on `messages` for the inbox, a wait setting for the
  low inbox, the wait in `manage_watch`, and `maxpm inbox` shows high first.
  Findings need the same split.
- For a sender: the kind decides (alert and question: high; note and notice:
  low). To put a note in the high inbox, the sender adds a flag or sends an
  alert.
- Wakes: 55 with 30m (70% fewer). All notes then wait up to 30 minutes.
- Limit: two levels cannot tell a note with finished work from a ship
  request notice. Both wait the same time.

#### Form 2: channels, the watch decides by kind

- What it is: messages do not change. A setting says, for each kind of
  message and finding, how long it waits before it wakes the manager.
- In the code: the smallest change. One setting (for example
  `manage_wake = "alert 0m, question 0m, note 10m, notice 30m, waiting 30m,
  human 30m"`) and the wait in `manage_watch`. No change of the database.
- For a sender: nothing changes. A sender cannot raise one message, except
  that it sends an alert in place of a note.
- Wakes: as in the table below, by the waits.
- Limit: the level is not on the message, so `maxpm inbox` and the page cannot
  show it, and a notice kind that must wake at once needs its own rule.

#### Form 3: one inbox, a level on each message

- What it is: each message has a level (urgent, normal, low; in his words:
  major warning, warning, info). The level comes from the kind by default, as
  in the table above. Each level has a wait.
- In the code: a column `level` on `messages` (empty: from the kind), set in
  `_send`; a flag `--level urgent|normal|low` on `maxpm send`, `note`,
  `alert`, and `ask`; two settings, `manage_wait_normal` and
  `manage_wait_low`; the wait in `manage_watch` (findings get their level from
  the kind); `maxpm inbox` shows the level. With `native_message`, a message
  to the manager goes into its session only when its wait ends.
- For a sender: no new habit, because the default comes from the kind. A
  sender can raise or lower one message: `maxpm note manager "<text>" --level
  urgent`, or `--level low` for a line that needs no action.
- Wakes: as in the table below.

#### Wake counts for 2026-10-08

Same method as the first version (each of the 181 wakes as one event at its
time, with the levels of the table above; the script prints this table):

| Wait of the normal level | Wait of the low level | Wakes | Fewer | Less manager cost (71k eq for each wake, 19.6M in the period) |
|---|---|---:|---:|---:|
| 0m (now) | 0m (now) | 181 | | |
| 5m | 30m | 93 | 49% | 6.2M, 32% |
| 10m | 30m | 79 | 56% | 7.2M, 37% |
| 15m | 30m | 67 | 63% | 8.1M, 41% |
| 30m | 30m (form 1, two inboxes) | 55 | 70% | 8.9M, 46% |
| 10m | 60m | 79 | 56% | 7.2M, 37% |

- The wait of the normal level decides the result: 91 wakes had a note as
  their most urgent event.
- A wait of the low level above 30m adds nothing. A low event seldom waits
  that long, because a normal or an urgent wake brings it sooner.

#### Recommendation

Form 3, with the waits urgent 0m, normal 10m, low 30m: 79 wakes in place of
181.

- It contains the two other forms. The default from the kind is form 2 (a
  sender needs no new habit). A level with a wait is an inbox of form 1; with
  normal at 30m it gives the same 55 wakes.
- Only form 3 lets a sender raise or lower one message, and lets the inbox
  and the page show the level.
- 10m for a note: the manager gets a worker's note with finished work in
  time for its next step, and a note seldom wakes it alone. The waits are
  settings, so 15m or 30m is a change of one line later.

Not in this proposal: the 39 wakes from other background commands. They are
release steps, and the release cadence of a target (#1571) reduces them.
Nothing is built before the answer (#1584); #1583 builds the form chosen.

### Built (#1583)

The answer to #1584 was form A with the waits 0m, 10m, 30m.

- `messages.level`: empty means the level of the kind (an alert, a question,
  an answer, an offer: urgent; a note, a notice: normal). The ship request
  notice has the level low. `--level urgent|normal|low` on `maxpm send`,
  `note`, `alert`, and `ask` sets another level, and `maxpm inbox` shows a
  level that the sender set.
- Settings `manage_wait_normal` (10m) and `manage_wait_low` (30m). The wait of
  a message counts from the time it was sent, so a watch that starts again
  does not start the wait again.
- `maxpm manage --watch`: an urgent message returns it at once, a normal or a
  low one when its wait ends. A new finding `waiting` or `human` waits
  `manage_wait_low`; another new finding waits `manage_settle` as before.
  Each return brings every message and finding that waits.
- With `native_message`, only an urgent message goes into the manager's
  session; the watch brings the others.

Expected for a day like 2026-10-08: about 79 wakes in place of 181.

Measured after one day with the new code (#1598): the day 2026-10-09 UTC,
against the 15.4 hours of 2026-10-08 before the change. The watch ran the two
levels of #1608 for the whole day. The section "Measurement after (#1598)"
below has the details.

| | Before (2026-10-08, 04:33 to 20:00 UTC) | After (2026-10-09 UTC) | Change |
|---|---:|---:|---:|
| Hours | 15.4 | 24.0 | |
| Messages that the manager read | 236 | 166 | |
| Messages for each hour | 15.3 | 6.9 | 55% fewer |
| Wakes (turns that the end of a watch started) | 181 | 58 | |
| Wakes for each hour | 11.7 | 2.4 | 79% fewer |
| Wakes for each 100 messages | 77 | 35 | 54% fewer |
| The watch returned for messages | 124 | 20 | |
| The watch returned for a finding | 55 | 29 | |
| The watch returned for the time limit | 2 | 8 | |
| Time between two wakes, median | 3.2m | 27.5m | |
| Wakes less than 10 minutes after the wake before | 152 of 180 | 1 of 57 | |
| Turns that the end of a background command started, for each hour | 14 | 3.8 | 72% fewer |
| eq of the turns that a watch started | 11.3M | 5.5M | |
| eq for each wake | 63k | 95k | 51% more |
| Manager eq for each hour | 1.22M | 1.12M | 8% less |

The expected 79 wakes are 56% fewer than 181. The day after had less than
half the messages for each hour, so the count for each message is the fair
one: 54% fewer wakes. The effect is as expected.

### Changed (#1608): two levels and the flag blocked

After one afternoon with the three levels, the person who approves chose a
simpler form: "low priority, high priority, and then a blocked flag". The
research of #1597 (`worker-stops.md`, section 6) gave the counts: after 78 of
128 alerts and questions to the manager, the sender went on with its item, so
an alert or a question by itself is no reason to wake the manager at once.

- Levels: `high` (the default, also for an alert and a question) and `low`
  (`--level low`, and the ship request notice). The names `urgent` and
  `normal` are gone; `--level urgent` is refused with the text "leave it out
  and add --blocked".
- `--blocked` on `maxpm alert`, `ask`, `note`, and `send`: the sender's item
  cannot move until the answer. `messages.blocked` holds who said so:
  `sender` (the flag), `waits` (the sender ran `maxpm inbox --wait` within
  three minutes after its alert or question), or `item` (the sender released
  or blocked the item within three minutes before or after it). These two
  signs showed 39 of the 50 past messages whose item stood still. `maxpm
  wait` is not a sign: a worker also runs it after an item that is done.
- Waits: a blocked message and a person's message wake the watch at once;
  high after `manage_wait_high` (10m; the setting `manage_wait_normal` was
  renamed); low after `manage_wait_low` (30m).
- No question back to the sender. `maxpm alert` and `maxpm ask` to the
  manager print how long the message waits and when to add `--blocked`.
- `maxpm inbox` and the watch show "blocked", and the watch prints a blocked
  message first. With `native_message`, only a blocked message and a
  person's message go into the manager's session.
- A stored level `urgent` became the flag, and `normal` became the default,
  one time (`_migrate_levels`).

For the measurement of #1598: count the wakes whose first message is blocked,
and compare the senders' flags with what the senders did
(`worker_stops.py`, section 5).

### Measurement after (#1598)

The period is the day 2026-10-09 UTC, for the manager that started on
2026-10-08 at 04:33 UTC (the same session as in "Measurement after (#1313)").
Its watch ran the code of #1608: two levels and the flag blocked. The three
levels of #1583 ran for about 40 minutes only, so no day measures them. The waits
were the defaults: `manage_wait_high` 10m, `manage_wait_low` 30m,
`manage_settle` 2m, `manage_every` 30m. Commands:

    python3 current_project/research/manager_wakes.py 2026-10-09T00:00:00+00:00 2026-10-10T00:00:00+00:00
    python3 current_project/research/manager_cost.py 2026-10-09T00:00:00+00:00 2026-10-10T00:00:00+00:00
    python3 current_project/research/worker_stops.py 2026-10-09T00:00:00+00:00 2026-10-10T00:00:00+00:00

#### The wakes

Background commands that ended and started a turn: 58 watches and 33 other
commands. 8 more watches ended while the manager was in a turn (before: 37),
so the watch returned 66 times (before: 218).

| The watch returned for | Wakes |
|---|---:|
| A finding | 29 |
| Messages | 20 |
| The time limit (`manage_every`, 30m) | 8 |
| Not read (two commands ended at the same time) | 1 |

| Findings in the wakes | Wakes |
|---|---:|
| human (an item is ready for a person) | 20 |
| waiting (an agent waits longer than `wait_too_long`) | 13 |
| uncovered | 3 |
| question | 1 |

| Messages in the wakes | Messages | Wakes with only this one message |
|---|---:|---:|
| Ship request notice | 44 | 1 |
| Note from an agent | 43 | 3 |
| Other notice | 15 | 0 |
| Alert from an agent | 10 | 0 |
| Question from an agent | 6 | 1 |

- A wake now brings 2.0 messages on average (118 in 58 wakes). Before, it
  brought 1.0 (181 in 181 wakes), and 70 wakes brought one note only.
- The manager wakes at a steady pace of about 30 minutes: the wait of the
  low level and the time limit of the watch are both 30m. In the quiet hours
  of the day, 8 wakes brought nothing new.

#### The cost

`manager_cost.py` now prints the eq of the turns by what started them. "For
each hour" divides by the hours with requests (16 before, 24 after).

| Part of the manager cost | Before: eq | Before: for each hour | After: eq | After: for each hour |
|---|---:|---:|---:|---:|
| Turns that the end of a watch started | 11.3M | 0.71M | 5.5M | 0.23M |
| Turns that a message started (the person, or another session) | 5.2M | 0.33M | 9.8M | 0.41M |
| Turns that the end of another background command started | 2.5M | 0.16M | 3.4M | 0.14M |
| Subagents of the manager | 0.5M | 0.03M | 8.2M | 0.34M |
| All | 19.6M | 1.22M | 26.8M | 1.12M |

- The watch part fell from 0.71M to 0.23M eq for each hour, 68% less. It is
  now 21% of the manager cost (before: 58%).
- For each message that the manager read, the watch part fell from 48k to 33k
  eq, 31% less. For a day like 2026-10-08 that is about 3.5M, 18% of the
  19.6M. The proposal estimated 7.2M, 37%.
- The difference: the estimate gave each wake the same cost (71k eq). A wake
  that brings two messages costs more: 95k eq and 5.3 requests (before: 63k
  and 3.8). The work for each message stays; the wait saves the reads of the
  context between the messages.
- The manager cost for each hour fell only 8%, because two other parts grew.
  The manager used subagents (8.2M, before 0.5M), and the turns that a
  message started cost more (0.41M for each hour, before 0.33M). The waits do
  not change these parts.
- The other numbers of the day: 1,487 requests, 18.0k eq for each request,
  mean context 122k, 28 compactions (at 166k to 207k), and no second wait for
  messages.

#### How long the messages waited

From the queue: the time from the send of a message to a manager until a
watch brought it (`manager_wakes.py` prints the table).

| Message to the manager | Messages | Median wait | Longest wait | Read in the first minute |
|---|---:|---:|---:|---:|
| Ship request notice (level low) | 64 | 9.7m | 28.7m | 6 |
| Note | 42 | 9.7m | 10.0m | 4 |
| Other notice | 19 | 7.1m | 10.0m | 0 |
| Alert, not blocked | 16 | 6.7m | 10.0m | 0 |
| Note, level low (the sender set it) | 14 | 8.2m | 21.1m | 1 |
| Question, not blocked | 3 | 0.2m | 10.0m | 2 |
| Blocked: the flag of the sender | 4 | 0.0m | 0.1m | 4 |
| Blocked: the sign item | 2 | 0.0m | 0.0m | 2 |
| Blocked: the sign waits | 2 | 6.6m | 11.9m | 0 |

- Before the change, 223 of 236 messages were read in the first minute, and
  the longest wait was 5.7 minutes.
- A note waited 9.7 minutes in the median and never more than 10.0. A note is
  most often the event whose wait ends first, so it waits almost the full
  `manage_wait_high`.
- A message of the low level seldom waits 30 minutes: the median is 8 to 10
  minutes, because a wake for a high message brings it too.
- 9 more notices went to a manager that had ended before that day. Nobody
  read them; they are not in the table.

#### Blocked messages, and what the senders did (#1608)

Workers sent the manager 27 alerts and questions. MaximizePM wrote 10 of the
alerts itself ("a review was added before your deploy"); the 17 others came
from a worker's own command. What each sender did after the send, from
`worker_stops.py`, section 5, and from the senders' transcripts:

| After the send | Messages | Blocked by the flag | Blocked by a sign | Not blocked |
|---|---:|---:|---:|---:|
| Waited at once (`maxpm inbox --wait` or `maxpm wait` in the foreground) | 6 | 2 | 2 (item) | 2 |
| Worked 35 to 70 seconds more, then waited (`maxpm inbox --wait` in the foreground) | 2 | 0 | 2 (waits) | 0 |
| Continued with its item | 7 | 2 | 0 | 5 |
| Ended its turn until its own subagents reported | 2 | 0 | 0 | 2 |

- MaximizePM saw each of the 8 senders that waited for the answer: 2 set the
  flag, a sign marked 4, and a wake for another event brought the last 2 in
  the first 15 seconds.
- The sign waits was correct both times. `worker_stops.py` counts these two
  senders as "continued", because it reads only the next tool call.
- 8 messages were blocked. Six were read in at most 3 seconds, one 1.2
  minutes after the send, when its sign came, and one after 11.9 minutes (see
  below). Five of the 8 started a turn of the manager (5 of the 58 wakes);
  three came while the manager was in a turn.
- Two of the four flags came from a sender that continued with its item.
  Those are two wakes that could have waited.
- The 10 alerts that MaximizePM wrote waited 2 to 10 minutes. Nothing stood
  still for them: `maxpm done` on the deploy item is refused while its review
  is open.

#### Did the manager miss something through a wait?

No. The manager read each message to it, and no wait of a level held a
message longer than its setting. The person's messages to the manager on
that day (the first line of each) name no late or missed event. Two messages
waited 10 minutes or more:

1. A blocked question waited 11.9 minutes, and its sender stood in
   `maxpm inbox --wait` for 10 minutes. The cause is not a wait of a level:
   no watch ran for 16 minutes. The manager ended a turn and did not start
   `manage --watch` again, until the end of another background command woke
   it. This is the only time of the day with no watch for more than 2
   minutes. A wait setting does not change it; a new item covers it (found
   during #1598).
2. One question without the flag waited the full 10.0 minutes. Its sender
   continued with its item and read its inbox six times in that time. The
   wait did what its setting says; the flag `--blocked` is the tool for a
   sender that needs the answer sooner.

So the waits stay: `manage_wait_high` 10m and `manage_wait_low` 30m. Shorter
waits repair neither case, and they cost a wake for each note. Longer waits
save little: the watch part is 0.23M of the 1.12M eq for each hour. The
larger parts are now the turns that the person starts and the subagents of
the manager.

Not measured: how long a finding waited. The transcripts show when the watch
returned for a finding, not when the finding began. A `waiting` or `human`
finding waits at most `manage_wait_low` by the code.

### Script changes (#1598)

- `manager_wakes.py` counts each end of a watch, also one that came while the
  manager was in a turn, and prints the wait of each message to a manager by
  kind, level, and blocked mark (from the queue).
- `manager_cost.py` prints the eq of the manager's turns by what started
  them, and the eq of its subagents.
