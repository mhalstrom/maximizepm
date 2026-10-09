---
name: maxpm
description: Take, do, and hand back work items from the MaximizePM queue (the `maxpm` command). Use when the user says "go" in a project that uses MaximizePM, tells you to work from the queue, or when you finish, release, or find new work.
---

# MaximizePM: working from the queue

The queue holds projects, items, and the dependencies between them. You pick
the area where you already hold context; inside it, `maxpm next` returns the
most important item that is ready (nothing it waits on is open).

## Fastest start

Run `maxpm go` in the project folder and follow the briefing. It names you,
picks your role (owner, worker, unblocker, reviewer, deployer, monitor, planner, context, idle), claims
an item when there is one, and ends with the command to run next. Pass `--as <your-name>` on
every later command. When the briefing asks, record your Claude Code session
name once (`maxpm --as <your-name> session "<name>" --ref <ref>`; ListAgents
prints `This session is <name> [<ref>]`, and names can repeat, so keep the ref), so
people and agents can message your session directly. After `maxpm done`, run `maxpm --as <your-name> go` again
at once, in the same turn: do not stop to report between items. When go gives
you no item (role IDLE), run `maxpm --as <your-name> wait` in the foreground
(not as a background command; give the shell command a 10-minute limit). A
blocking wait costs no tokens, and it keeps you available for new work. It
prints WORK (run go), no work yet (run wait again), or END: no work came
within `wait_max` (45m), MaximizePM unregistered you, and you stop. After each
item, end in `maxpm wait`, never at your prompt. Stop also when you need the user (file a human item first; see
below); then report everything you finished. A person turns this off with
`maxpm config set auto_continue off`. MaximizePM never types into your terminal:
when you sit idle at your prompt, `maxpm serve` gives new work or an answer for
you to a fresh session, and ends you after `idle_end` (15m) with nothing in hand.

## Identity (by hand)

Register once, then name yourself on every command:

```
maxpm register <session-name> --note "what you are working on"
export MAXPM_AGENT=<session-name>        # or pass --as <session-name>
```

A person registers with `--human`.

Declare the model your session runs, once: `maxpm --as <you> go --model <model>`
(or `MAXPM_MODEL=<model>`). MaximizePM then gives you only items whose
`--min-model`/`--max-model` limits allow your model, and says what it skipped.
An item's recommended model and effort (`model:` in the briefing) are advice:
work at that effort; a different model may still take it. An item for another
agent type (`agent codex` while you run in Claude Code) is skipped; MaximizePM reads
your type from your CLI's environment.

## Work loop

1. Choose your area and take an item. `maxpm project list` shows what each
   project covers; `maxpm project show <name>` shows who works there and what
   is ready.
   - `maxpm next --project <name> --claim`: the project you know
   - `maxpm next --mine --claim`: next to what you claimed or finished before
     (linked items first, then the same projects)
   - `maxpm next --near <id> --claim`: items linked to one you just worked on, closest first
   - `maxpm next --unblocks <id> --claim`: something that unblocks your blocked item
   - `maxpm next --claim`: anything, most important first
   Leave out `--claim` to look first. Add `-n 5` to see five.
2. `maxpm show <id>` for the notes, what it waits on, and what it unblocks.
   `maxpm show <id> --brief` prints a few lines: no history, long text cut.
3. Do the work. Every `maxpm` command renews your lease. If you work a long
   time without one, run `maxpm heartbeat` (default lease 30 minutes). MaximizePM
   also renews it while a command runs in your session (a long test run) or
   your tmux pane changes, but do not count on that: it needs `maxpm serve`
   or a process it can see, and it stops `busy_max` (4h) after your last
   maxpm command. A background command or a subagent that your harness runs
   for you (Claude Code: `run_in_background`) counts as work for up to
   `busy_max`: `maxpm serve` keeps your lease and your item while it runs,
   also when a message comes for you, and you read the message when the
   harness wakes you. A loop that only renews the lease is not work.
4. Finish: `maxpm done <id> --output "<one line: what changed, commit id>"`.
   The reply lists items that became ready.
   Cannot finish: `maxpm release <id> --note "<why>"`.
   Done is refused while an item it waits on is open (`maxpm blockers <id>`); close it
   anyway only with a reason, `maxpm done <id> --force "<why>"` (never a deploy item).
   When someone adds a prerequisite to an item you hold, you get an alert: stop and wait for it.
   The change must go out, and the project has a deploy target: add `--ship`
   (or run `maxpm ship <id>` later). The item joins that target's next
   deploy item, which only the target owner takes. A release that is cut (a
   reviewer took its review, or the owner ran `maxpm target cut`) holds a
   fixed list of items: your item then joins the deploy item after it. When
   the target has a release cadence (`maxpm target cadence <target> 2h`), ship
   requests collect. The reply says with which release your change goes out
   and when that one can start; your item is done, so
   take other work. A release sooner (a fix of a production defect, a person's
   request) is a decision of the target owner or the manager: ask with
   `maxpm alert <owner> "<why it cannot wait>" --item <deploy id>`; they run
   `maxpm target release-now <target> --reason "<why>"`. The owner's `maxpm go`
   gives it the ready deploy item first (role DEPLOYER), with what it ships;
   `maxpm go --role deployer` also takes a free target of the folder's projects.
   When the target has a monitor text (`maxpm target monitor`), claiming the
   deploy adds a monitor item, and `maxpm serve` opens a session for it
   (role MONITOR): it watches, then finishes, or alerts the deployer and adds
   a person's item with a proposed rollback. It never rolls back by itself.
   With the setting `review` on, the deploy item first waits on one review
   of the whole release. `maxpm go` gives a ready review before new work
   (role REVIEWER; `--role reviewer` takes any), but never to an agent that
   claimed or finished an item the release ships. Follow the review process in
   its context and the review steps the brief lists for each project, then
   `maxpm review pass <id> --confirm all --output "<what you checked>"`
   (confirms the written steps; runs the command steps and `review_cmd`, which
   must exit 0; a command still running after `review_timeout` (4h) is stopped
   with every process it started, and the error names the setting), or
   `maxpm review fail <id> "<fix>" ... --note "<what you found>"`: MaximizePM adds
   the fixes as items the review waits on, and the review comes back after them.
   With `--ask`, the user approves the list first: MaximizePM adds one item for the
   user with the proposed fixes; done adds the fixes still in its list, drop
   adds none. Findings that do not block the release are ordinary items:
   `maxpm add "<title>" --found-during <review-id>`, then pass.
   A commit in the range you review is your own (the release pin moved after
   the review came to you): do not review it, and do not release with a note
   only. Run `maxpm release <review-id> --author "<your commits>"`: MaximizePM
   gives you this review no more, and another session takes it.

## Owning a goal

A goal is an outcome in a project, with a test for "done when". One agent owns
a goal at a time and works toward the outcome: it plans the items the goal
needs, takes them, and clears what blocks them. Goals are optional; items
without a goal stay in the normal queue.

- `maxpm go` never forces a goal on you. It gives you a free goal (role
  OWNER) only when the most important ready work in your area serves it: the
  item carries the goal's tag, or an open item of the goal waits on it.
  `maxpm goal own <name>` takes a goal by hand, and `maxpm go --role owner`
  takes the highest-ranked goal nobody owns. `maxpm goal list` shows the
  open goals, their owners, and progress.
- The briefing shows the outcome, the done-when test, the goal's open items,
  and who holds what blocks them. Then go gives you, in order: a ready item of
  the goal; an item outside the goal that unblocks it; or, when the goal has
  no open items, the question whether done-when holds.
- Plan as you go: `maxpm add "<title>"` tags the item with the goal you own, when the item is in that goal's project.
  Add `--no-goal` for a fix you find in passing that serves no goal, or
  `--goal <name>` (repeatable) to name the goals.
- Coordinate with other owners: `maxpm note|ask|alert --goal <name> "<text>"`
  and `maxpm offer "<text>" --goal <name>` reach the owner of that goal.
  You get a notice when another agent adds, claims, or finishes an item of
  your goal.
- When done-when holds: `maxpm goal done <name> --result "<one line>"`.
  MaximizePM refuses while tagged items are open; finish them, untag them
  (`maxpm edit <id> --untag <name>`), or add `--drop-open`. Then run
  `maxpm go --role owner` to take the next free goal, or `maxpm go` for the
  most important ready work.
- Owning a goal claims its items: while you own it, its agent items are
  reserved for you (other agents skip them and can offer help), and your
  leases on them last `goal_lease` (4h) instead of `lease_ttl`. A person's
  items in the goal stay on the person's list. Each maxpm command renews the
  claim; `maxpm goal own <name> --lease 6h` picks another length.
- Your goal's items, the items they wait on, and the deploy items of a target
  you own count against `goal_max_leases` (3), apart from `max_leases` (1).
  So you can take an urgent prerequisite of your goal while you hold other work.
- Keep the goal's handoff current: after each item of the goal, run
  `maxpm goal handoff <name> --file <path>` (or the text): what the goal is,
  the decisions so far, the files that matter, and what is left. MaximizePM
  keeps every version (`--versions`, `--version N`). The go briefing of the
  next owner, and of any session that starts on an item of the goal, shows it.
- Stop owning: `maxpm goal release <name>`, or `maxpm goal give <name> --to <agent>`.
  Both refuse while the handoff is older than your last finished item of the
  goal; write it first, or give the reason: `--no-handoff "<why>"`.
  After `goal_lease` without a command the goal is free again, its items are
  open to every agent, and you get a notice. The goal is also free when your
  session is gone or ends.
- A sub-goal (`maxpm goal add <project> <name> --parent <goal>`, one level,
  same project) splits a large goal: its owner and its sessions read the
  parent's handoff, then the sub-goal's own. A goal with open sub-goals is not
  complete until they are.
- A shared goal has no owner (`maxpm goal list` shows "shared: no owner"). A
  person or a manager decided that several agents work on it at the same time.
  `maxpm go` never makes you its owner, `maxpm goal own` refuses, and its
  items are normal work for every agent. Take them with `maxpm go`, and tag
  an item you add for it: `maxpm add "<title>" --goal <name>`.

## Context sessions and forked workers

With the setting `goal_context` on, `maxpm serve` starts a context session
(role CONTEXT) for a goal with two or more ready agent items and no warm base.
It reads the goal's handoff, the items' notes, and the files they touch, keeps
the handoff current, runs `maxpm goal base <goal> --ready` as its last command,
and ends. It edits no file and claims no item. A session that MaximizePM then
starts for an item of the goal begins as a fork of that base
(`claude --resume <base> --fork-session`) when the base has the same model and
folder and a session used it less than `base_warm` (50m) ago: it reads the
goal's context from the prompt cache. Its go briefing says so. An item that
needs no goal context starts fresh: `maxpm add ... --no-goal-context` (or
`maxpm edit <id> --no-goal-context`). `maxpm goal base <goal>` shows the base;
`--clear` drops it.

## Adding work

One command, no ids copied by hand. The project comes from the item you name
with `--blocks` or `--found-during`, or from the folder you are in; name it
first only when neither applies (`maxpm add <project> "<title>"`).

- Needs to happen before your item: `maxpm add "<title>" --blocks <your-id> --keep|--release`
- Found while working, not needed for your item: `maxpm add "<title>" --found-during <your-id>`
  (linked both ways in `maxpm show`; it does not block anything)
- Give the next agent a start: `--context "why, where"`, `--touches <files>`,
  `--check "<command>"`, and `--doer human` for steps only a person can do.
- The work comes from an outside tracker issue: link it with
  `--ref <tracker>:<key>` (for example `github:owner/repo#12`, `jira:PROJ-123`).
  The go briefing names the project's tracker (`maxpm project tracker`); when
  the user names a tracker for the first time, record it there.
  When you claim an item with links, mark the issues in progress there. When
  it is done, post the output as a comment, close the issue, and record it:
  `maxpm synced <id>` (or `maxpm done <id> --output "..." --synced` when you
  did it first). Until then, `go` and `status` remind you.

## Items that may be done already

When a session stops mid-item, its lease runs out and the item goes back to
the queue, but the work may be partly or fully done. The go briefing then
says CHECK FIRST: read `maxpm show <id>`, `git log --grep '#<id>'`, and the
touched files before you start. `maxpm cleanup` lists every suspect item
(expired leases, commits that name an item, stale items, old notices).
Record what you found:

- `maxpm check <id> done --note "<commits or files>"`: closes it.
- `maxpm check <id> partial --note "<what is left>"`: the note goes on the item.
- `maxpm check <id> open`: not started; it leaves the cleanup list.

Name the item in commit messages (`Fix the header (#12)`), so a check finds it.

## When you find other work

- Something this item needs first: `maxpm add <project> "<title>" --blocks <your-id>`
  with one of:
  - `--keep`: it is small and you do it now. Your item becomes `held`, still
    yours; the new item is reserved for you; when it is done your item goes
    back to in progress. A hold lasts `hold_ttl` (2h), renewed by your
    commands. More than `keep_prereq_limit` (3) open prerequisites: MaximizePM
    releases instead and marks the item `replan`.
    After `replan_threshold` (3) prerequisites are added to a claimed item,
    MaximizePM also marks it `replan`, and `maxpm plan` lists it for a planner.
  - `--release` (the default): it is large or better for someone else. Your
    item goes back to the queue, waiting on the new one. Run `maxpm go` again.
  `maxpm keep <id>` holds a released item again; `maxpm release <id>` ends a hold.
- Something this item needs first is in the queue already (another item, or
  the release that carries it): add the link, do not only name it in a note:
  `maxpm dep <your-id> --on <other-id> --release`. Your item goes back to the
  queue and is ready when the other one is done (`--keep`: you hold it while
  you wait). After `maxpm release <id> --note "waits on #12"` with no link,
  the item is ready at once, and `maxpm go` gives it to the next session,
  which finds the same thing and releases it again.
- Something unrelated: `maxpm add "<title>" --found-during <id>`.
  Do not do it inside your current item.
- Waiting on something outside the queue: `maxpm blocked <id> --reason "<what>"`.
  Add `--until <time>` (`2h`, `2026-09-28T07:00`, `'mon 07:00 America/New_York'`)
  when you know when it ends: the item becomes ready by itself then.
- A limit of the machine (a full disk, a build stop, Docker down, a push
  freeze, quiet time while a release gate runs) does not block your item, and
  `maxpm blocked` is not for it: the next item meets the same limit. Do the
  parts that do not need the limited thing and commit them. Then keep the
  item and wait for the manager's word that the limit ended:
  `maxpm inbox --wait` in the foreground (start it again when it ends with
  nothing; each start renews your lease). Then finish the item. Take no other
  item that needs the same thing. Do not ask the user, and do not ask the
  manager if the limit ended: the manager tells every session.

## When you need the user

Anything only the user can do or decide (a decision, an approval, an account,
payment, or legal step) goes in the queue as a human item, not only in chat.
The queue is what notifies them (`maxpm needs-you`, phone, mail), and it keeps
the step visible after the chat ends.

```
maxpm add "Approve the refund policy draft" --doer human --blocks <your-id> --release \
  --context "Five decisions at the end of docs/legal/refund-draft.md; answer each yes/no"
```

- Say exactly what to decide or do and where the material is, so the user can
  act without asking you.
- Link it: `--blocks <id>` when your item waits on it, `--found-during <id>` otherwise.
- Do not wait at your prompt for the answer. Commit what is finished, say in
  `--context` what is left, release (`--release`), and run `maxpm go` again:
  you take other work, or wait in `maxpm wait`. When the person answers (marks their item done,
  or answers your question), `maxpm serve` starts a fresh session for your
  item, with the answer in its notes (setting `fresh_sessions`).
- A short question that needs no item: `maxpm send question --to <person> "..." --item <id>`.
  The go briefing lists the people by name.
- Then tell the user in chat too, with the item id.
- With `--keep` (only for an answer that comes in minutes) you hold your item while you wait for the answer, but at most
  `human_wait_max` (30m). Then MaximizePM releases your item (it still waits on the
  person's item), reminds the person, and tells you to take other work:
  run `maxpm go`. When the person finishes, the item is ready for whoever runs go.
- A command is refused (the auto mode classifier, a permission prompt): do
  the parts of the item that you can. Then add the person's item as above,
  with the exact command and the permission rule that allows it
  (`--blocks <id> --release`), and take other work. The person runs the
  command or adds the rule, and a fresh session finishes the item. A yes in
  one terminal does not reach the next session, and a rule does.
- Wait at your prompt only for a step that must not run without the person's
  yes each time: a release to production, or a deletion of data. Ask in the
  queue first, as your last step before you stop:
  `maxpm ask <person> "<what to approve>" --item <the item you hold>`. While the
  question is open, `maxpm serve` keeps your lease (and your deploy target),
  takes nothing back, starts no other session for your work, and alerts the
  manager. The person answers in your terminal or with `maxpm answer`. A wait
  that you only say in chat looks idle: `maxpm serve` gives your work to others.
- The user should look at a finished result (a page, a text, a design): the
  project's setting `result_look` decides, and the go briefing states the rule
  of your item's project.
  - `push_first`: push first, close the item, and add
    `maxpm add "Look at <result>: <where>" --doer human --found-during <id>`.
    What the user wants changed becomes new items. Do not hold the item for
    the word "push". Ask before the push only for public text (a README, a
    site), a release to production, a step that deletes data or costs money,
    or when the item says "ask before the push".
  - `ask_first`: commit, do not push, and ask in the queue:
    `maxpm ask <person> "Look at <result>: <where>. Push?" --item <id>`. Then
    wait at your prompt for the yes; `maxpm serve` keeps your lease while the
    question is open. After the yes, push and close the item.
  A person sets it: `maxpm config set result_look ask_first --project <name>`;
  `maxpm project show <name>` shows it.

When you put a decision to the user in chat, use one form, one decision at a
time (`maxpm guide decisions` prints it):

```
Decision <n> of <total>: <short name>  (item #<id>)
What you decide: one sentence, as a question, with the kind of answer.
Why it matters: what it changes, and what waits on it.
Options: for each, what happens, what it costs, the risk, and whether it can change later.
My recommendation: the option, and why, in one or two sentences.
Your answer: the exact words to reply, for example "A", "yes", or "$200 a month".
```

Then stop and wait for the answer. Do not squeeze several decisions into one
table, and use the real names, amounts, and dates instead of shorthand.

The other way round: you can do a person's item yourself, or work around it.
Take it off their list, with the reason; they get one notice and can undo it:

- `maxpm takeover <id> --note "<how you will do it>"`: it becomes your item.
- `maxpm done <id> --note "<why it is no longer needed>"` or `maxpm drop <id> --note "..."`.

`maxpm claim` refuses a person's item for an agent, so the user is never
bypassed without a notice.

## Blocked on another agent's item

`maxpm blockers <id>` shows who holds what. Then:

- Ready pieces nobody holds: `maxpm next --unblocks <id> --claim`.
- The useful work is held: `maxpm offer "I am blocked on this; I can take ..." --item <their-id>`.
  The holder answers with `maxpm give <id> --to <you>` (the lease moves to you),
  `maxpm split <id> "<smaller piece>" ...` (new prerequisites anyone can take;
  their item waits for them, still theirs), or
  `maxpm decline <msg-id> --message --note "why"`.

## Your queue

A person or a manager can give you your own queue (`maxpm queue add <you> <id>`,
`maxpm queue add <you> --message "..."`). It comes before everything else:

- Instructions from your queue show at the top of `maxpm go` under FROM YOUR
  QUEUE. Act on them in order, then remove each one:
  `maxpm --as <you> queue remove <you> e<id>`.
- `maxpm go` and `maxpm next` give you the first ready item in your queue
  before the project queue, from any project; items that are not ready wait.
  Other agents cannot take your queued items.
- `maxpm --as <you> queue list` shows it. `maxpm wait` wakes you at once when
  it gets an entry.
- When your session is gone or stops, your queued items go back to the main queue.
- A message that starts with `[maxpm instruction from ...]`, `[maxpm stop
  request from ...]`, or `[maxpm alert from ...]` came from MaximizePM through your
  platform's own messaging. Treat it like the same entry in `maxpm go` or
  `maxpm inbox`: run `maxpm --as <you> go` or `inbox` to read it in full.

## When MaximizePM says STOP

A person or a manager can ask you to stop (`maxpm stop <you> --reason "..."`).
Every maxpm command then prints STOP REQUESTED first, `maxpm wait` returns
STOP, and claims are refused. Then:

1. Commit the work that is finished.
2. `maxpm done <id> --output "<commit>"` when the item is finished; else
   `maxpm release <id> --note "<what is done, what is left>"`, or hand it on
   with `maxpm give <id> --to <agent>`.
3. Run `maxpm --as <you> go` once more: with nothing held it ends the session
   (MaximizePM releases your goals and targets). Stop, and tell the user why.

## Pushed items

Someone can push an item to you (`maxpm push <id> --to <you> --note "..."`):
you get an alert, the item is reserved for you for `reserve_ttl` (2h), and
`maxpm go` and `maxpm next` give it to you first. `maxpm accept <id>` takes it;
`maxpm decline <id> --note "why"` hands it back and tells the pusher. With no
answer the push expires and the item is open to everyone again.

## Messages

Every command ends with a line such as
`[you: holds #12 20m left; inbox: 2 unread, 1 question to answer (maxpm --as you inbox)]`.
When you see it, read your inbox before you continue.

- `maxpm inbox`: unread messages and questions that wait for your answer.
  Add `--all` for read ones, `--peek` to leave them unread.
- `maxpm inbox --wait`: blocks until a new message comes, prints it, and
  exits (after 25 minutes with nothing, it exits too). Run it as a background
  command (Claude Code: `run_in_background`) when you must answer messages
  while you work, and start it again after each exit. Not needed when MaximizePM
  delivers messages into your session itself (`native_message`).
- `maxpm answer <msg-id> "<text>"`: answer a question. The asker gets it.
- Shortcuts: `maxpm alert <agent> "<text>" --item <id>` (work they probably need),
  `maxpm ask <agent> "<text>"`, `maxpm note <agent> "<text>"`. Instead of an agent:
  `--holder-of <id>` (whoever holds that item), or for `ask`, `--file <path>`
  (every agent whose held items touch it). `maxpm note "<text>"` alone sets your status.
- An alert to you: `maxpm accept <msg-id> --message` claims its item now, or,
  while you hold other work, keeps it reserved for you until after that.
  `maxpm decline <msg-id> --message --note "why"` says no. The sender hears either way.
- When an item you hold waits on another one and that one is done, MaximizePM sends you a notice.
- `maxpm send question|alert|note "<text>" --to <agent>`: to one agent.
  Use `--item <id>` instead of `--to` to reach whoever holds that item
  (or the next holder, if nobody holds it). `--reply <msg-id>` replies to
  the sender of that message.
- `maxpm thread <msg-id>`: the whole conversation; `maxpm thread --item <id>`:
  every message about an item.

An alert means stop and read now. A notice comes from MaximizePM itself, for
example when your lease expired.

A message to the manager does not wake it at once: its watch brings an alert,
a question, or a note within 10 minutes (`manage_wait_high`), and a low one
(`--level low`: a line that needs no action) within 30. Each wake costs the
manager a read of its whole context. Add `--blocked` to `maxpm alert`, `ask`,
`note`, or `send` when your item cannot move until the answer: you have no
other part of it to do, and nobody else can do the next step. A blocked
message wakes the manager at once. Leave the flag out when you continue with
the item and the answer only changes what you do later. MaximizePM also marks
your alert or question blocked when it sees that you stand still: you run
`maxpm inbox --wait` after it, or you release or block its item.

## Looking around

- `maxpm who`: every agent and person, and what each one holds. Stopped and
  gone agents that hold nothing are hidden; `--all` shows them.
  `maxpm who --file <path>`: who holds an item that touches that file or directory
  (from each item's `--touches`); check it before you edit a shared file.
- `maxpm blockers <id>`: the tree of open work an item waits on, with holders.
- `maxpm capacity`: ready work versus active sessions.
- `maxpm usage`: the token cost of each done item, read from the Claude Code
  transcripts of the sessions that held it, and the medians for fresh and
  forked sessions. `maxpm show <id>` has the item's usage line.
- `maxpm list --project <name>`: open items in order.

Add `--json` to any command for machine-readable output. Errors name the rule
and the next command to run.
