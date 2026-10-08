# What stops a worker (research, #1597)

Status: research of 2026-10-08. Nothing is built. The decisions at the end are
in the queue as item #1599.

## The question

The person asked: "we need to understand what is considered blocking or what
do the agents consider to be blocking for themselves." This text lists each
time a worker stopped or paused an item it held, sorts the stops by cause, and
compares them with what the `maxpm` skill and the go briefing tell a worker.

A second question came while the work ran (note #4958): before MaximizePM
wakes the manager for an urgent message, should it ask the sender whether the
sender is blocked? Section 6 gives the counts, the definition of "blocked",
and what MaximizePM can see by itself, for build item #1608.

## Method

The script `worker_stops.py` (in this folder) reads four sources, read-only:

    python3 current_project/research/worker_stops.py 2026-10-06T00:00:00+00:00 2026-10-08T20:15:00+00:00

Add `--detail` for each stop with its text.

1. The queue (`holds`, `events`): how each hold of an item by a worker ended.
   The cause of a release or a block comes from its text, by word rules (in
   the script). I read all 168 stops by hand to check the rules.
2. The transcripts of 176 worker sessions: each turn that ended while the
   session held an item, when the session then stood 5 minutes or more.
3. The questions and alerts of workers (`messages`).
4. The refusals of the Claude Code auto mode classifier in the transcripts.

Limits. The `holds` table starts on 2026-10-06, so the window is 68 hours.
"Stood still" is the time from the stop to the next claim of the item (or its
close). "Waited on it" counts the open items that had the item as a direct
prerequisite. A release review waits on each item of its release, so a release
waits on almost each item that has a pushed commit; the tables count work
items and releases apart. A worker is each agent that is not a manager.

## Result in short

- 576 holds by 176 sessions on 422 items. 388 ended in done (67%). 168 ended
  in a stop (29%). 9 were dropped, 11 were still held.
- The largest group is correct and cheap for the worker: 74 stops for a
  prerequisite that the worker added (`--blocks`). 39 of them wait on a
  person's item: 33 decisions, 3 steps, and 3 times the person's decision on
  the disk. The worker did not stand: it released and took other work. The
  item stood still for the person's answer time: median 3.9 hours from the add
  to the close.
- The second group has no rule in the skill: limits of the machine (a full
  disk, a build stop, Docker down, a push freeze). 30 stops in the queue, 11
  stands at the prompt, and 10 questions "is the freeze over?" (7 of them in
  13 minutes).
  Each worker chose its own way.
- 26 times a worker stood at its prompt with an item for something that only
  another party could give (13.2 hours in sum). The causes: the disk (10), a
  refused command (8), a yes before a push (2), others (6). One skill rule
  tells the worker to stand there.
- A question to a manager is not a cost: 58 questions, median answer 20
  seconds, and no asker stood idle. A question to a person while the asker
  kept the item cost more: 19 questions, the item stayed held 16 minutes in
  the median, 7 hours at the longest.
- A file conflict (`--touches`) stopped no worker by itself. The queue makes
  the conflict links before the claim (514 links in the window). One release
  note names a file in edit by other items as a second cause.

## 1. Stops, by the causes that the item named

| Cause | Stops | The item stood still (median; longest) | What waited on it | Was the stop necessary? |
|---|---:|---|---|---|
| **A prerequisite the worker added** (`--blocks`; 71 with `--release`, 3 with `--keep`) | 74 | | | 3 of the 74 are the person's decision on the disk; they are in the machine rows. |
| A decision of a person (a proposal first, then the item for the person) | 33 | 3.9 h; 38 h | a work item in 7 stops, a release in 5 | Yes for most. Of 30 decision items read by hand, the person changed the proposal or chose another option in at least 5 (#1119, #1132, #1235, #1273, #1311), and 4 were dropped. A build of the recommended option before the answer loses work in about one of three. |
| A step only a person can do (sign in a browser, connect a workspace, paste a prompt in a chat app) | 3 | 4.6 h; 6.5 h | a work item in 1 stop | Yes. The worker cannot do the step. |
| Work for another agent (a split, or a defect found first) | 11 | 143 m; 8.4 h | a release in 7 | Yes. This is the rule as written. |
| A deploy (the item is a check on production) | 5 on 2 items | 114 m; 10.9 h | a work item in 3 | Yes, but the same two items were claimed and released 5 times: see "claimed too early". |
| The release review failed: the fix list waits for a person (`review fail --ask`) | 15 on 7 reviews | 16 m; 9 h | a release in 14 | The review process. The setting decides it, not the worker. |
| The release review failed: fix items for agents | 4 | 16 m; 90 m | a release in 4 | Yes. |
| **`maxpm blocked` with a reason** | 17 | | | 16 of the 17 are machine limits (15) or the push freeze (1); they are in the next rows. |
| **A limit of the machine** | 30 | | | |
| Disk space, a build stop by the manager, Docker down | 24 on 21 items, 9 sessions | 62 m; 6.1 h | a work item in 5, a release in 11 | The stop of the build: yes. The release of the item: no for 11 of the 24, where the code was done and committed and only the build, the test, and the push were left. In the other 13 the worker had not started. One session blocked six items in 13 minutes with "code done, not built", then refused a seventh. |
| A push freeze for a release review (the work is done, the push waits) | 6 on 4 items | 53 m; 82 m | a work item in 1 | The wait for the push: yes. The release of the item: no. A fresh session had to take the item only to push one commit. |
| **A stop of all sessions** (a restart of tmux or of the computer, on the person's word) | 15 on 12 items | 25 m; 60 m | a release in 8 | Yes. The STOP rule worked: each worker committed, wrote a handoff, and released. |
| **A stand at the prompt, until the hold ended by itself** (`maxpm serve` took the item back, or the lease ran out) | 25 | seconds: a fresh session took it | a release in 15 | See section 2 for the cause of each stand. 4 of the 25 sessions waited for their own background command or subagent and were not idle. |
| **A refused command**, released with a note | 2 | 4.9 h; 7.1 h | a work item in 1 | Section 4. |
| **A missing tool or sign-in** (a browser that is not signed in; a subagent with no browser tools) | 4 | 3.9 h; 15.1 h | a work item in 2 | Yes, but two items went to a second session that stopped for the same cause. The open person's item for the sign-in was not a prerequisite of them. |
| **An alert or note read as a stop** | 1 | 86 m | none | No. The worker had pushed before it read the manager's hold, and it released the finished item to ask. |
| **Other** | 20 | | | |
| The reviewer wrote commits of the release (the go rule did not catch it) | 8 on 3 reviews, 5 sessions | seconds | a release in 8 | The stop is right. The claim is wrong: each cost a session start. |
| The pinned revision of the review was old | 1 | 2 m | a release | Yes. |
| Claimed too early: the item was ready in the queue, but it needs another item or a release that is not live | 5 | 114 m; 24 h | a work item in 1 | The stop is right. The release note names the other item, but no link is added, so go gives the item out again. Three items (#1302, #1405, #1482) were claimed and released by two or three sessions. |
| Split into pieces; the parent is released | 2 | 149 m; 4.9 h | a work item in 2 | Yes. |
| A handoff note with no cause; one release after `human_wait_max` | 4 | | | |
| **A question to another agent** | 0 stops | | | 5 questions, answered in 16 m (median). No asker stood idle. |
| **A file conflict** (`--touches`) | 0 stops | | | The queue holds such items back before the claim. |
| **A gate or a quiet-machine note** | 0 stops in the queue | | | One stand of 14 m ("no browser test until the gate ended"), and waits in background loops. The notes exist: 265 messages of managers name a gate, 80 name quiet time. |

## 2. Stands at the prompt with an item in hand

The queue does not show these. The session ended its turn, kept the item, and
did nothing for 5 minutes or more.

| What the session's last text said | Stands | Sessions | At the prompt (median; longest; sum) | What waited |
|---|---:|---:|---|---|
| The disk was full: it asked the person to free space | 10 | 10 | 11 m; 4.4 h; 6.4 h | a release in 9 |
| A command was refused: it asked the person for a yes or for a permission rule | 8 | 8 | 27 m; 75 m; 4.2 h | a work item in 3, a release in 3 |
| A yes before a push ("I wait for your 'push'") | 2 | 1 | 56 m and 34 m | 3 work items, 2 release items |
| A yes before a release to production | 1 | 1 | 6 m | a work item |
| A decision asked in chat | 1 | 1 | 5 m | a release |
| The push freeze | 1 | 1 | 9 m | none |
| A report with no request | 3 | 3 | 7 m; 30 m | |
| **Sum** | **26** | | **13.2 h** | |
| Not a stop: it waited for its own background command or subagent | 41 | 23 | 8 m; 49 m; 7.8 h | |

Facts behind the rows:

- The 10 disk stands came at one time (2026-10-07 05:13 UTC). The disk filled,
  each command failed, and nine sessions told the person to free space and
  ended their turns. The classifier had refused the removal of their own build
  folder (8 refusals with the reason "Irreversible Local Destruction").
  `maxpm serve` took eight of the items back 11 minutes later.
- Each of the 8 refused-command stands followed the skill rule: the worker
  asked in the queue (`maxpm ask`) and stood. 6 of the 8 texts told the person
  that a permission rule removes the stop.
- The same refusal came back in the next session. Three items had the same
  command refused in two or three sessions in sequence (#1084, #1446, and the
  deploy items #1036 and #1143). The person's yes in one terminal does not
  reach the fresh session.
- The yes before a push. One session built #1449 by 06:19 UTC and asked the
  person to look before the push (the person had asked to see the page). The
  yes came at 12:51: 6.5 hours. Nine work items waited on #1449 at that time.
  The same session held #1451 from the question #4885 at 19:22 to "ok looks
  fine" at 20:07: 45 minutes. In the first wait the session did other work; in
  both it kept the item.

## 3. Questions

| To | Questions | Answer (median; longest) | The asker held the item | The item stayed held (median; longest; sum) | No tool call until the answer |
|---|---:|---|---:|---|---:|
| A person | 30 (4 open) | 8 m; 11.9 h | 19 | 16 m; 7.1 h; 18.7 h | 6 (19 m; 46 m) |
| A manager | 58 (1 open) | 20 s; 21 h | 45 | 20 s; 38 m; 3.1 h | 0 |
| Another agent | 5 (1 open) | 16 m; 44 m | 3 | 24 m; 44 m; 1.4 h | 0 |

A person's item that a worker added as a prerequisite closed after 3.9 hours
in the median (33 items; longest 8.1 hours). A question to the person in the
queue got its answer in 8 minutes in the median. The short question is fast,
but the asker keeps the item for that time.

## 4. Refused commands

44 calls refused by the auto mode classifier in 26 worker sessions, in 28
turns. 37 came while the session held an item. Reasons: Modify Shared
Resources 11, no reason given 8, Irreversible Local Destruction 8, Auto-Mode
Bypass 4, Instruction Poisoning 4, and 9 calls with seven other reasons.

| What the worker did after the refusal | Turns |
|---|---:|
| Ended the turn and stood at the prompt with the item, 5 minutes or more | 10 |
| Ended the turn (a stand under 5 minutes, or no item held) | 6 |
| Released or blocked the item in the next 20 minutes | 5 |
| Continued in the same turn (found another way, or did the other parts) | 7 |

Most refused commands are steps that the project's own process asks for: the
detached checkout of the review gate in the main tree, a release step, the
push of a finished text, the removal of the worker's own build folder, a
migration that the person had approved. They repeat for each session.

## 5. The skill text against the facts

What the skill and the go briefing tell a worker to treat as blocking:

| Rule in the text | Used | Fits the facts? |
|---|---:|---|
| "It needs something first": `add --blocks`, with `--keep` (small, you do it now) or `--release` | 74 | Yes. `--keep` is rare (3). |
| "When you need the user": a human item, release, take other work; "do not wait at your prompt" | 39 | Yes. No worker stood for these. The cost is the person's answer time. |
| "Only you can do the step once the person says yes (a deploy, a command the auto mode classifier refused to you), so you must wait at your prompt: ask in the queue first" | 8 stands, and 2 more by its pattern | **This rule makes workers stand.** Its reason ("only you can do the step") is seldom true: another session can do the step. Workers also stretch it to cases it does not name: a look at a result before the push (2 stands), a full disk (10 stands). |
| "Waiting on something outside the queue": `maxpm blocked --reason`, release, run go again | 17 | **It fits a blocker of one item, not a limit of the machine.** 16 of the 17 uses were machine limits. After the release, go gives the worker a new item that meets the same limit. |
| "An alert means stop and read now" | | Managers sent workers 482 alerts and 667 notes in 68 hours. The text does not say what a note on the machine's state means for the item. |
| "When someone adds a prerequisite to an item you hold, you get an alert: stop and wait for it" | 1 | Yes. |
| STOP REQUESTED: commit, done or release with a note, run go | 15 | Yes. |
| "Blocked on another agent's item": `blockers`, `next --unblocks`, `offer` | 0 stops | Not needed in the window. |
| `maxpm who --file` before you edit a shared file | 0 stops | The text does not say what to do when a holder exists. No worker stopped for it. |

Places where workers stop and no rule exists:

1. **A limit of the machine** (disk, build stop, Docker, push freeze, quiet
   time during a gate). The manager sends a note or an alert to each session.
   Workers then did four different things: blocked and released item after
   item; waited in a background loop with the item; asked the manager if the
   limit had ended (10 questions; 7 came in 13 minutes on 2026-10-08, each
   from a session with a finished commit); or stood at the prompt and asked the person.
2. **A look at a finished result before the push.** The worker keeps the item
   and waits for one word. No rule says who may ask for this, or that the
   worker can push first when the change is easy to undo.
3. **An item that go gave too early.** The worker releases with a note. No
   rule tells it to add the missing link (`maxpm dep`), so the item comes back.
4. **A release review of the worker's own commits.** The text says go never
   gives such a review; go gave it 8 times.
5. **A wait for the session's own background command.** The worker ends its
   turn and the harness wakes it. To `maxpm serve` it looks idle: serve took
   4 such items back when news came for the session.

Places where a rule makes them stop for nothing:

1. The stand at the prompt for a refused command, when the command is a
   normal step of the project. One permission rule in the project's settings
   removes each of these for all later sessions.
2. `maxpm blocked` and release for a machine limit, when the code is done.
   The item goes back to the queue with a local commit in a worktree, and a
   fresh session must read it all to run the test and push.

## 6. Alerts and questions to the manager: did the sender stop?

The question of note #4958: before an urgent message wakes the manager,
should MaximizePM ask the sender if it is blocked? The person then chose the
form (note #4976): two levels, high and low, and a flag `--blocked` on the
send command, with no question back to the sender. Build item #1608 waits on
this section for three things: the counts, the definition of "blocked", and
the cases that MaximizePM can set by itself. The counts show no defect in
that form.

### The counts

Workers sent managers 164 urgent messages in the window (106 alerts and 58
questions, 2.4 for each hour). What the sender did after the send, read from
its transcript:

| After the send | Alerts | Questions | Sum |
|---|---:|---:|---:|
| Continued with another tool call | 60 | 29 | 89 |
| Waited: in `maxpm inbox --wait` (in the foreground) | 9 | 11 | 20 |
| Waited: in `maxpm wait` (most had released the item) | 6 | 2 | 8 |
| Waited: in a sleep or a loop of its own | 2 | 7 | 9 |
| Waited: ended its turn | 2 | 0 | 2 |
| Sent by MaximizePM itself (a worker's `done --ship` added a review before a deploy that the manager holds) | 21 | 0 | 21 |
| No transcript line for the send | 6 | 9 | 15 |

- Of the 128 messages that a worker sent and that the transcript shows, the
  sender **continued in 89 (70%)** and **stood still in 39 (30%)**.
- Of the 39 that stood still, 25 kept the item in hand. 14 had released or
  blocked the item at the send and waited for other work.
- 11 of the 89 that continued had also released or blocked the item at the
  send: the sender took other work, but the item of the message stood still.
- So **an item stood still until the answer in 50 of the 128 (39%)**, and
  in 78 (61%) the sender went on with the same item.
- The manager's reply came fast in each group: 18 to 30 seconds (median) for
  a question, 1 to 5 minutes for an alert.
- 21 of the 106 alerts had no sender that could be blocked: MaximizePM wrote
  them, in the name of the worker whose command caused them.

### The definition of "blocked" for the send command

> Set `--blocked` when the item of your message cannot move until the answer:
> you have no other part of it to do, and nobody else can do the next step.
> Leave it out when you continue with the item and the answer only changes
> what you do later.

A worker can apply this at the call. It matches what the 128 senders did: a
sender that had another part to do continued (78), and a sender whose item
could not move waited or gave the item back (50).

### What MaximizePM can set by itself

| Sign that MaximizePM has | Blocked messages that show it (of 50) | Wrong for |
|---|---:|---|
| The sender runs `maxpm inbox --wait` or `maxpm wait` as its next command, in the foreground | 28 | A sender that runs `inbox --wait` in the background and works on. None did so directly after a send in the window. |
| The sender's item is released or blocked within three minutes of the send | 25 | None seen: the item stood still in each. |
| One of the two signs above | 42 (84%) | |
| The sender stands at its prompt | 2 at the send; `maxpm serve` sees a stand only some minutes later | A sender that waits for its own background command (41 such stands in section 2). |
| No sign: the sender waits in a sleep or a loop of its own, with the item in hand | 6 | These need the flag from the sender. |

Notes for the build (#1608):

1. The two signs that the item names ("the sender waits at its prompt, its
   item is held or blocked on this answer") find fewer cases than the wait
   commands do. Add the sign "the sender's next command is `maxpm inbox
   --wait` or `maxpm wait`": it shows 28 of the 50.
2. The signs come after the send, some of them minutes after. So MaximizePM
   must be able to raise a message to blocked while it waits for its level's
   time, not only at the call.
3. An alert that MaximizePM writes itself has no sender to set the flag (21
   of 106). Its level must come from the event. "A review was added before
   your deploy" stops nothing at once: `maxpm done` on the deploy is refused
   while the review is open.
4. A question back would cost about 58k eq for each message (two requests of
   a worker at 29k eq in the mean), against about 71k eq for the wake of the
   manager that it saves in six of ten cases. The flag costs nothing. This
   agrees with the choice.

## Decisions

Item #1599 holds these for the person. Nothing is built before the answers.

**Decision 1 of 3: a look at a finished result, before or after the push** (item #1599)

- **What you decide:** When a worker has finished a change and wants you to
  look at it, does it push first? Answer with a letter.
- **Why it matters:** One session kept #1449 for 6.5 hours and #1451 for 45
  minutes for the word "push". Nine work items waited on #1449. In the window,
  19 questions to you came from a worker that kept its item: 18.7 hours of
  held items in sum.
- **Options:**
  - A. As today. The worker asks, keeps the item, and waits. Cost: the item
    and all that waits on it stand still for your answer time. No risk.
  - B. Push, then ask. The worker pushes, closes the item, and adds an item
    for you: "Look at <result>". Your changes become new items. Four cases
    stay "ask first": public text (README, site), a release to production, a
    step that deletes data or costs money, and an item that says "ask before
    the push" in words. Cost: a change you do not like is on main until the
    new item fixes it. You can change the rule back at any time: it is text.
  - C. As B, but only when the item says "push, then show". Cost: the planner
    must write it on each item; without it, option A applies.
- **My recommendation:** B. A push is easy to undo before a release, and the
  release review still stands between main and production.
- **Your answer:** "D1 A", "D1 B", or "D1 C".

**Decision 2 of 3: a refused command** (item #1599)

- **What you decide:** What does a worker do when the auto mode classifier or
  a permission prompt refuses a command that its item needs? Answer with a
  letter.
- **Why it matters:** 28 turns had a refusal. In 10 the worker stood at its
  prompt with the item (median 27 minutes, longest 75). The same command was
  refused again in the next session for three items.
- **Options:**
  - A. As today. The worker asks you in the queue and stands at its prompt
    until your yes. Cost: the item stands for your answer time; each new
    session meets the same refusal.
  - B. The worker stands only for a step that must not run without your yes
    each time (a release to production, a deletion of data). For each other
    refused command it finishes the parts it can, adds an item for you with
    the exact command and the permission rule that allows it, releases, and
    takes other work. The manager collects the commands that repeat (the
    review gate's checkout, the removal of a worker's own build folder, the
    push of a finished item) in one item for each project: "allow these
    commands in the project settings". Cost: you add each rule one time. Risk:
    a rule allows the command for each later session. You can remove a rule
    at any time.
- **My recommendation:** B. Most refused commands are normal steps of the
  project, and a rule ends the stop for all sessions, not for one.
- **Your answer:** "D2 A" or "D2 B".

**Decision 3 of 3: limits of the machine** (item #1599)

- **What you decide:** How does a worker learn that a limit is in force (a
  full disk, no builds, a push freeze, quiet time during a gate), and what
  does it do then? Answer with a letter.
- **Why it matters:** 30 stops, 11 stands at the prompt, and 10 questions to
  the manager came from these limits in 68 hours. Managers sent 457 messages
  that name a freeze and 257 that name the disk. One session blocked six
  items in 13 minutes.
- **Options:**
  - A. As today. The manager sends notes, and each worker decides.
  - B. A text rule in the skill and the go briefing: a limit of the machine
    does not block the item. Do the parts that do not need the limited thing,
    commit them, keep the item, and wait in `maxpm wait` until the manager
    says the limit ended. Take no new item that needs the same thing. Do not
    ask the person, and do not ask the manager if it ended. Cost: one text
    change. Risk: a worker holds an item through a long limit; the lease rules
    still apply.
  - C. B now, and a proposal for a state in the queue: "limit in force", set
    by the manager or by you, with a reason and an end. The go briefing shows
    it, go gives out only items that do not need the limited thing, and
    `maxpm wait` returns when it ends. It replaces the notes. Cost: a build
    item after you approve the proposal.
- **My recommendation:** C. The text rule stops the different reactions now.
  The state removes the messages, which are also a large part of what wakes
  the manager.
- **Your answer:** "D3 A", "D3 B", or "D3 C".

## Findings that need no decision (added as items)

- #1609: a worker that releases an item because it waits on another item
  adds the link. Today it writes a note, and go gives the item out again (5
  stops; three items claimed and released by two or three sessions).
- #1610: go and `maxpm serve` gave a release review to a session that wrote
  commits of the release (8 releases on 3 reviews).
- #1611: `maxpm serve` took the item of a session that waited for its own
  background command or subagent (4 cases).
