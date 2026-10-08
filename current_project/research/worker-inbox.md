# How workers use their inboxes, and levels for a worker (research, #1596)

Status: research for a decision (#1606). Nothing is built.

The question (2026-10-08, after form A for the manager in #1584): how do the
worker agents use their own inboxes, and does a priority level make sense
there too?

## Method

The script `worker_inbox.py` (in this folder) reads the queue read-only and
the Claude Code transcripts of the worker sessions. A worker is an agent that
is not a manager and not a person. For each message to a worker, the script
finds the first place where the text of the message shows in a transcript of
that worker, and the command that printed it.

    python3 current_project/research/worker_inbox.py 2026-10-06T21:34:32+00:00 2026-10-08T20:30:00+00:00

The period is 47 hours: 1,471 messages to 180 workers. All requests of these
workers in the period cost 779M eq (eq as in `manager-cost.md`). Two limits:

- Most of the messages come from one project with many sessions, a release
  process with a push freeze, and a disk emergency on the first evening.
  Another project gives other shares.
- "What the worker did" has two sources: a count by the script (the next six
  tool calls after the read), and 57 messages that I read by hand with the
  worker's own words after the read.

## What a worker gets

| Kind | From | Messages | In a broadcast | Never read | Median wait before the read | 90% |
|---|---|---:|---:|---:|---:|---:|
| Note | The manager | 615 | 425 | 26 | 0.2m | 9.9m |
| Alert | The manager | 196 | 165 | 7 | 3.3m | 18.0m |
| Note | Another worker | 168 | 17 | 30 | 1.9m | 16.1m |
| Push alert | MaximizePM (`maxpm serve`, the page) | 95 | 0 | 0 | 0.1m | 0.2m |
| Push alert | The manager | 92 | 3 | 0 | 0.1m | 0.2m |
| Stop request alert | The manager | 88 | 31 | 27 | 0.1m | 0.2m |
| Answer | The manager, a worker, a person | 70 | 0 | 9 | 0.6m | 8.0m |
| Notice | MaximizePM, also in the name of an agent | 105 | 7 | 56 | 0.5m to 4.2m | 3.1m to 4.8h |
| Alert | Another worker | 31 | 0 | 2 | 0.1m | 4.5m |
| Question | Another worker, the manager | 8 | 0 | 2 | 7.3m | 44.1m |
| Offer | Another worker | 3 | 0 | 0 | 4.2m | 7.6m |

- A broadcast is the same text from one sender to three or more workers in
  two minutes. There were 88 broadcasts to 648 inboxes, 44% of all messages,
  with a median of seven workers for each. All but 24 of these inboxes got a
  broadcast of the manager: a push freeze starts or ends, builds must stop or
  can start again, a rule for the shared machine.
- A worker got five messages in the median, 18 at the 90% point, and 58 at
  the most.
- The 187 push alerts repeat what the go briefing says ("take #12"). The
  claim in `maxpm go` closes them: 69 never showed in a transcript.
- The 88 stop request alerts repeat the STOP banner that each command prints.
  The banner is what stops the session; 27 alerts were never read.
- Questions between agents go to the manager. A worker got eight.

## How a worker reads a message

| How the worker read it | Messages | Median wait | 90% | Longest |
|---|---:|---:|---:|---:|
| `maxpm inbox`, after the status line of a command showed the count | 857 | 1.0m | 13.4m | 1.6h |
| `maxpm inbox --wait` in the foreground (the worker was blocked in it) | 168 | 0.0m | 0.9m | 13.2m |
| Never read | 154 | | | |
| `maxpm thread` | 88 | 0.2m | 0.6m | 65m |
| Closed by a claim, text never shown | 69 | 0.1m | 0.2m | |
| Marked read, text not in the transcript | 63 | 2.2m | 4.4m | 32m |
| Another command, or `inbox --peek` | 68 | | | |
| Native delivery into the session | 0 | | | |

- The status line is the channel. For 750 messages the script found the
  status line that showed the count: it came 0.5m after the message in the
  median (90%: 12.7m). The worker then ran `maxpm inbox` 0.1m later in the
  median (90%: 0.2m). A worker reads each message as soon as it sees the
  count, whatever the kind.
- "The next command" comes soon most of the time. The time between two maxpm
  commands of a worker: median 0.1m, 90% 3.0m. But 245 of 6,892 gaps are
  longer than 10 minutes and 40 are longer than 30 minutes: a long build or
  test run, or a subagent. In such a gap nothing reaches the worker.
- One read shows one message: 854 of 992 reads. Messages do not collect.
- No worker gets messages by native delivery. The setting `native_message`
  names Codex only, two of 329 agents have a native address, and no message
  of the period has a delivery state.
- A background `maxpm inbox --wait` (what the worker guide names) ran 29
  times. Workers use the foreground form: 377 calls by 54 workers, with a
  shell time limit of about nine minutes, 40.6 hours blocked in total. It is
  their way to wait for one message, almost always "freeze ended". 153 calls
  returned a message and 224 ended at the time limit; the worker then started
  the wait again.
- A worker with no item waits in `maxpm wait`. That command returns for each
  unread message: 154 of its 1,082 results.
- Never read: 156. For 117 the session had ended before the message came or
  made no request after it. For 39 the worker worked on and never ran
  `maxpm inbox`.
- 39 messages were read more than 30 minutes after they came.

## What a worker did after the read

The script counts what the worker ran in its next six tool calls:

| Message | Read in a transcript | Sent a message or changed the queue | Only waited or looked for work | Went on with other tools |
|---|---:|---:|---:|---:|
| Note from the manager, broadcast | 388 | 117 | 62 | 209 |
| Note from the manager, to one worker | 178 | 71 | 16 | 91 |
| Alert from the manager, broadcast | 150 | 72 | 6 | 72 |
| Note from another worker | 136 | 52 | 17 | 67 |
| Push alert | 117 | 31 | 1 | 85 |
| Stop request alert | 61 | 3 | 56 | 2 |
| Answer | 55 | 18 | 4 | 33 |
| Notice | 34 | 12 | 5 | 17 |
| Alert from the manager, to one worker | 27 | 21 | 1 | 5 |
| Alert from another worker | 26 | 21 | 0 | 5 |
| Question | 6 | 6 | 0 | 0 |

The first column of actions is an upper limit (a message that the worker sent
for another reason counts too). "Went on with other tools" does not say if
the message changed the work. The 57 messages read by hand say more:

| Message | Read by hand | Changed what the worker did | Changed nothing |
|---|---:|---:|---:|
| Alert from the manager, to one worker | 9 | 6 | 3 |
| Note from the manager, to one worker | 9 | 4 | 5 |
| Answer | 4 | 3 | 1 |
| Broadcast note of the manager | 14 | 10 | 4 |
| Broadcast alert of the manager | 8 | 5 | 3 |
| Note from another worker | 9 | 2 | 7 |
| Notice | 4 | 1 | 3 |

- An alert or a note to one worker that changed the work carried a decision
  of the person, a new scope, a defect from a review, or a prerequisite. The
  worker changed its edits, released its item, or took the item up again.
- A note to one worker that changed nothing was a thanks, a confirmation, or
  a statement that was out of date at the read.
- A broadcast changes the work only of a worker in the state it names. A
  worker with a commit ready to push waited for "freeze ended" and pushed at
  once. A worker with no item read the same note and waited again.
- A note from another worker is mostly "my item is on main, this changed". In
  seven of nine cases the worker wrote "for information" or "matches what I
  have" and went on.

### Messages that cost a turn and changed nothing

| Case | Times | Cost | Share of the workers' 779M eq |
|---|---:|---:|---:|
| The request after each `maxpm inbox` call (1,221 calls) | 1,221 | 38.5M eq | 4.9% |
| Of these: the inbox was empty | 394 | 10.9M eq | 1.4% |
| `maxpm wait` returned for messages; the worker read them, ran go, and waited again | 122 of 154 | 9.1M eq | 1.2% |
| A foreground `inbox --wait` ended at its time limit; the worker started it again | 224 | 8.7M eq | 1.1% |

- A read is one more request with the whole context. The median context of a
  worker at a read is 260k tokens, and the request costs 28k eq.
- 83 of the messages in the 122 useless wakes are broadcast notes of the
  manager, 15 are notes from another worker. A wake costs 76k eq in the
  median.
- 117 of the 394 empty inbox calls have one cause, a defect (#1605): the go
  briefing says "Messages for you: inbox: 1 unread, 1 alert. Read them before
  you start" for the push alert that its own claim closes. 160 go briefings
  had this line; the inbox was empty after 117 of them.

### Messages that came too late

A broadcast that says "do not push now" or "run no build now" shows at the
worker's next maxpm command. A worker in a long step sees it after the step.

| Broadcast | Inboxes | Median wait before the read | Read after more than 10m | The worker started the named command between the note and its read |
|---|---:|---:|---:|---:|
| A push freeze starts | 190 | 0.8m | 20 | 20 (a push) |
| Builds or tests must stop (disk, load, a gate starts) | 77 | 0.5m | 7 | 12 (a build or a test run) |

- Pushes: a hook refused 14 of the 20. The first freeze had no hook yet, and
  at least three pushes went to main, 0.4, 1.8, and 9.6 minutes after the
  note. One worker then wrote a memory for itself: check the inbox before a
  push. The hook is what holds the freeze. The note gives the reason.
- Builds: the script prints 13; one is a false match. No hook stops a build. On 2026-10-08 a note "gate starts" came at
  20:14:48Z. One worker read it 3.3 minutes later, after three runs of its
  unit tests. A second worker started a test run 0.5 minutes after the note
  and read the note 7.4 minutes after it. In the disk emergency, workers read
  "stop all builds now" up to 12.8 minutes late, with a test run started in
  that time.
- The opposite problem is small: three of 214 start notes were read when the
  end note was there already.
- Old alerts: 10 broadcast alerts and 11 notes from other workers were read
  more than 30 minutes late. In the hand sample the worker called two such
  alerts "old" and did nothing.

## Does a level help a worker?

#1583 put the level on every message (`messages.level`; empty: from the kind;
alert, question, answer, and offer are urgent, note and notice are normal;
`--level` on each send). For the manager each level has a wait. A worker has
no watch, so a level must have another effect. The facts above say what a
level can and cannot do.

1. A level cannot make an urgent message faster. A worker already reads each
   message within a minute of its next command, one message for each read.
   No note stands in front of an alert. The late messages are late because no
   maxpm command runs, and a level does not change that.
2. "Interrupt now" needs delivery into the session, and no worker has it. With
   it, an urgent message would enter at the worker's next tool result (the
   transcripts show that Claude Code adds queued input there). It would reach
   a worker that reads and edits for a long time. It would not stop one long
   command.
3. A rule such as "run nothing now" holds only when a check at the command
   holds it, as the push hook does. A level on the note does not replace that.
4. A level can cut useless reads. Today each message has the same effect: the
   count in the status line, a read at once, and a wake of `maxpm wait`. A
   level can remove that effect for the messages that seldom change the work.

### What each level does for a worker (the form that the facts support)

| Level | Default | Status line of each command | `maxpm wait` (a worker with no item) | Where the worker reads it |
|---|---|---|---|---|
| urgent | Alert, question, answer, offer | Counted, named first | Returns | `maxpm inbox` at once (later: delivery into the session) |
| normal | Note, notice | Counted | Does not return | `maxpm inbox` at the next command of a worker that holds an item; the next go briefing of a worker that waits |
| low | Only with `--level low` | Not counted | Does not return | The next go briefing prints the text; `maxpm inbox` shows it |

- `maxpm inbox --wait` returns for each level: a worker that runs it asked for
  messages ("freeze ended").
- A push and a queue entry wake `maxpm wait` as work, as now.
- Expected effect on the data of this period: the 122 useless wakes of
  waiting workers stop (9.1M eq, 1.2%). The low level saves one request (28k
  eq) for each message that a sender marks; how many depends on the senders.
  The whole effect is 1% to 2% of the workers' cost. That is small against
  the 37% that the waits save for the manager, because a worker is seldom
  woken.

## Options

- A. No level effect for a worker. Repair only the defect of the go briefing
  (#1605). Cost: nothing more. The useless wakes stay.
- B. The table above: `maxpm wait` returns only for an urgent message, and a
  low message is not counted and shows in the next go briefing. A small
  change (`unread`, `wait`, the go briefing, the worker guide). It can change
  back with no loss. Risk: a note to a waiting worker is read at its next
  item; a sender that needs it sooner sends an alert.
- C. B, and delivery of urgent messages into a Claude Code session
  (`native_message`). The only form that interrupts a worker between two
  maxpm commands. A larger change: the channel must be tested with real
  sessions. (#758 removed another wake, the line typed into the terminal
  pane of an idle session, because a wake of an old session costs much.)
- D. A shared state in place of broadcast notes: the manager sets a rule with
  a start and an end ("push freeze", "run no build"), each command shows it in
  the status line while it is on, and a session that starts later sees it
  too. No message, no read, no wake: 44% of the messages of this period. The
  largest change, and its value depends on the release process (the rule of
  2026-10-08 ended the push freeze).

## Decision

Decision 1 of 1: the effect of a message level for a worker  (item #1606)

What you decide: does a worker's message level change what MaximizePM does
with the message, and in which form (A, B, C, or D)?

Why it matters: 4.9% of the workers' cost is the request after each
`maxpm inbox` call, and 122 turns in two days were a waiting worker that read
a note and waited again. A "run nothing now" note came too late in 12 of 77
cases; no level repairs that.

Options:
- A: nothing changes for a worker. No cost, no risk. The 122 useless wakes
  stay. It can change later.
- B: `maxpm wait` returns only for an urgent message; a low message is not
  counted and shows in the next go briefing. A small change, about 1% to 2%
  less worker cost. Risk: a note reaches a waiting worker at its next item.
  It can change back.
- C: B, and urgent messages go into the Claude Code session. It interrupts a
  worker between commands. A larger change with a real risk (a text that
  enters a running session). It can be a setting, off by default.
- D: a shared state in place of broadcast notes. It removes 44% of the
  messages. The largest change; its value depends on how releases run from
  now on. It can come later, after B.

My recommendation: B. It uses the level that #1583 stores, it removes the one
effect that the data show as waste, and it is small. For "run nothing now",
add a check at the command (as the push hook), not a level. Measure again
before C or D.

Your answer: the letter, for example "B".
