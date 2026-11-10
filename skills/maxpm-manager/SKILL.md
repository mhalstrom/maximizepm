---
name: maxpm-manager
description: Run the MaximizePM manager session (the `maxpm manage` command): plan with the user, launch agents, fill agent queues, stop stuck agents and agents that wait too long, change launch settings, and give release targets to another agent. Use when the user says "manage" in a project that uses MaximizePM, or asks you to keep the other agents working together.
---

# MaximizePM: the manager

The manager is an optional chat session, in any agent CLI, that the user starts
to keep the work moving. It plans with the user and helps the other agents work
together. It takes no items itself: claims refuse for a manager.

## Start

Run `maxpm manage` and follow the briefing. It names you (pass `--as <name>` on
every later command) and lists what needs attention. One manager is active at a
time: a second `maxpm manage` names the active one and refuses. To replace it,
`maxpm manage --takeover "<why>"`; the old manager is told.

## The loop

1. Read NEEDS ATTENTION in the briefing and act on each line (below).
2. Start `maxpm --as <you> manage --watch` as a background command (Claude
   Code: `run_in_background` with a time limit above `manage_every`, 30m; in a
   foreground shell with a 10-minute limit, add `--step 9m`). It is your only
   watcher. It exits when a new finding needs you (an agent is stuck or gone,
   an agent waits longer than `wait_too_long`, a project has ready agent work
   and no agent, a target owner is away or gone, a question for the user), and
   when a message or question comes to you: it prints the message and marks it
   read. After the first new finding it waits `manage_settle` (2m) more and
   returns every finding that came meanwhile, so a group of findings wakes you
   once. A finding it reported before does not wake it again. With nothing new
   it exits after `manage_every` with one line. Each message has a level,
   and each level a wait before it wakes you: high after `manage_wait_high`
   (10m: the default, also an alert and a question), low after
   `manage_wait_low` (30m: a message sent with `--level low`). A ship
   request, a review that passed, and a deploy that is done wake you at once
   (a notice of level now): the release waits on you. A blocked message wakes you at once: the sender set
   `--blocked` because its item cannot move until your answer, or MaximizePM
   saw that the sender stands still (it runs `maxpm inbox --wait`, or it
   released or blocked the item). `maxpm inbox` shows "blocked" on such a
   message: answer it first. A person's message always wakes you at once. The findings "an agent waits" and "an item is
   ready for a person" wait `manage_wait_low`; another finding waits
   `manage_settle`. Each exit of the watch brings every message and finding
   that waits, so they do not wake you again. Send your own notes that need
   no action with `--level low`. When the briefing says MaximizePM delivers
   messages into your session itself (`native_message`), only a blocked
   message and a person's message come that way, and they do not wake the
   watch. Run no second background command: each wake reads your
   whole context again, so a second watcher doubles the cost. MaximizePM refuses
   `maxpm inbox --wait` from the active manager.
3. Act on what is new. Then start `manage --watch` again. With no watch,
   nothing wakes you at your prompt: a blocked message waits, and its sender
   stands still. When a command of yours ends with `no watch runs`, start the
   watch before you end your turn. After 5 minutes with no watch and a
   message that is due, `maxpm serve` alerts each person.

Stop when the user tells you to, and tell the user what you did.

## What to do for each finding

- **STUCK agent** (its process ended, or it is away or gone while it holds
  items or has queued items): `maxpm stop <agent> --reason "..."`. If it does
  not end and its items block others, ask the user first; only with their yes,
  `maxpm stop <agent> --kill --reason "..."` (uncommitted work in its folder is
  lost).
- **LEASE RAN OUT** on an item: its session stopped mid-item. Queue it for an
  agent (`maxpm queue add <agent> <id>`), or launch one for it
  (`maxpm launch --item <id>`). The taker checks what is done first.
- **WAITS TOO LONG**: give it work from its area (`maxpm queue add <agent> <id>`),
  or stop it (`maxpm stop <agent> --reason "no work"`) so it does not hold a slot.
- **NO AGENT in a project** with ready work: `maxpm launch --project <name>`
  (`--dry-run` first when unsure). Pick the model and effort from the items
  (`--model`, `--effort`); the items' limits apply. **NO CODEX AGENT** (or
  another type): ready work there needs that agent type; run the launch line
  it prints (`maxpm launch --item <id> --agent Codex`).
- **Releases move by themselves**: `maxpm serve` gives a ready release review to a
  session that waits in a project of the release and held no task before, else
  starts a reviewer (`auto_review`). A review always starts in a new session. When the deploy item is ready, it alerts the
  target owner once; with no owner, or an owner that cannot take it (gone, never
  connected, or idle at its prompt), it starts a deployer session and gives it the
  target (`maxpm target deployer <target> launch|standing|off`; standing starts the
  deployer while the review runs). No session starts while `max_sessions` agent
  sessions are live (0: no limit). Each start or failure is a `release:` line in the
  item's history. Do not hand releases over yourself unless serve says it cannot.
- **Each project sets its release gate and its cadence from its needs**, never
  as one switch for all projects and never for one night. The gate is the setting
  `release_approval` of the deploy project (`deploy-<target>`) or of a project of
  the target: `review` (a release goes out when its review passes; this turns
  review on for that project) or `person` (maxpm serve adds one item for a person
  to each ready release, "Approve release <target>: ...", and the deploy waits on
  that yes; after a failed deploy it asks again). `maxpm config set
  release_approval review|person --project <name>`; there is no global value, and
  `maxpm target show` says when a target has none. Ask the person which one fits
  when you set up a target, with the project's needs: a service with users can
  ship on review, and a release that only the person may approve takes `person`.
  The cadence: `maxpm target cadence <target> <time>|off --reason "<the project's
  needs>"` (a service with users: short, so a fix is live soon; a public tool
  repository where a push is the release: off). `maxpm target show` and
  `maxpm project show` print both, with the reason.
- **Fewer, larger releases**: give the target a release cadence,
  `maxpm target cadence <target> 2h|6h|1d|1w|1mo|off`: the shortest time between two
  normal releases (2h is at most 12 a day, 6h is four). Ship requests then collect on
  the open deploy item, and its review (the deploy item, with no review) is ready only
  at the cut of the last release plus the cadence; `maxpm target show <target>` says
  when, and what waits. Do not hold a cadence by hand (auto_review off, pinned
  releases). A release that cannot wait (a fix of a production defect, a person asks)
  is your decision or the target owner's, with no approval from a person:
  `maxpm target release-now <target> --reason "<why>"`; the history keeps the reason.
  A worker cannot run it; it asks you or the owner.
- **A release has a cut**: from the cut on, its deploy item holds a fixed list of
  items. A ship request that comes later joins the next deploy item, and that
  release starts when this one is done (and, with a cadence, not before the cut
  plus the cadence). The cut comes by itself when a reviewer takes the review
  (the deployer, with no review). When you pin the commit of a release before
  its review, cut it then: `maxpm target cut <target> --rev <commit>`; the commit
  goes into the history and the notes of the deploy item. A fix that the review of
  the cut release asks for still goes out with that release.
- **A hook runs a project's command on a release event**:
  `maxpm target hook <target> <event> "<command>"` (events: release-cut,
  review-passed, review-failed, deployed, deploy; `--clear` removes one; no event lists
  them; only a person or you set, change, or remove a hook). The maxpm command
  that causes the event runs the hook before it returns (when that command is
  in the sandbox of an agent session, maxpm serve runs it, outside the
  sandbox, and the command prints the result), in the folder of the target's project, with MAXPM_EVENT,
  MAXPM_TARGET, MAXPM_REV (the commit of `maxpm target cut --rev`), MAXPM_ITEM,
  and MAXPM_AGENT set. So a step such as "prepare the build folder at the cut,
  clean it after the deploy" is a hook, not a line in a runbook that you run
  by hand. A hook never undoes and never refuses its event: when it fails or
  runs longer than `hook_timeout` (10m), the target owner (you, with no owner)
  gets an alert with the exit code and the last lines of its output.
- **A target with a deploy plan releases by a deploy hook** (mark: "the deploy
  should be a hook"): `maxpm target hook <target> deploy "<release command>"`,
  and `maxpm config set review on --project deploy-<target>`. No session types
  the release command, and you do not run it: when the deploy item is ready
  (its review passed), maxpm serve runs the hook (at most `deploy_timeout`, 2h),
  and no session takes the deploy item. Exit 0 closes the deploy item. A failure
  keeps it open, and the release starts again from its beginning: a fix item
  with the output, a new review of the fix, then the hook again. Nobody runs the
  command again by hand or repairs production by hand. A target where a push
  to main is the release needs no deploy hook. `maxpm target deployer <t> off`
  stops the hook too.
- **A release review nobody takes** (serve does not run, auto_review is off, or a start failed):
  `maxpm launch --item <review id>`. The new session reviews (role REVIEWER) in a
  folder of the release; a waiting session that worked on the release does not get it.
- **Experts** (role expert, `maxpm expert list`): sessions that the user talks
  with for hours or days. They take no queue work, wait at their prompt, and are
  never stuck, idle, or waiting too long: leave them alone. Each has a task of
  tier expert (`maxpm add ... --tier expert`); a ready one needs a new session:
  `maxpm launch --item <id>`, or `maxpm expert start <name> --item <id>`, never a
  push to a worker. When the user wants one on a new topic, or a talk with you
  becomes a long talk (see "Hand a long talk to an expert"),
  `maxpm expert start <name> --topic "..." [--project <p>] [--model <m>]`; only
  the user ends one (`maxpm expert end <name> [--done]`), or `expert_idle_end` does.
- **A request for an expert** (an `EXPERT REQUEST` alert from `maxpm expert
  request`, or a session or the user asks for "an expert", "a Fable expert"):
  start an expert, never a worker. Run the line the alert names,
  `maxpm expert start <name> --item <id>` (or `--topic "..." --model <m>` when
  no task exists). A plain `maxpm add` and `maxpm launch --item` of a task
  without `--tier expert` starts a worker (#5036); a session started so is made
  an expert in its chat: `maxpm --as <name> expert adopt`.
- **NOT CONNECTED**: a session MaximizePM started ran no maxpm command. Tell the
  user (its terminal may wait on a prompt; for an agent in tmux the user reads
  and answers it with the agent's Terminal button on the page, and only a
  person does that), stop it, and launch again. MaximizePM
  takes its push back after `connect_within`. A stop ends every reservation
  of the agent at once, and MaximizePM does the same for an agent that is gone.
- **A prompt alert to you** ("waits on a prompt in its terminal: <agent> in
  tmux <window>, with no question river can read"): a person gets each prompt
  alert that names a question and its keys; one without a question comes to
  you. Read the screen (`maxpm view`, or the Terminal button). Send the person
  an alert with the question and the keys when only a person can answer it;
  else stop the agent and launch again.
- **An activity alert to you** ("<agent> stands still: ..."): the Claude Code
  hooks of a session that MaximizePM started say that it holds an item and
  waits on a permission prompt, ended its turn at its prompt, or reported
  nothing for `activity_silence` (10m). A turn end while its own background
  command, background subagent, or wakeup runs is no alert: that work wakes
  it, and its next turn end counts. The alert names the item and the
  command it waits on. Look at its terminal or session. Send a prompt that
  only a person can answer to the person; else stop the agent and launch
  again. Agents reach you with `maxpm alert manager "..."` (also ask, note).
- **SERVE LOOP LATE**: `maxpm serve` made no complete pass of its loop for
  5 minutes or more, so it closes no finished session, sends no notification,
  starts no fresh session, and does not reload. Run `maxpm serve --restart`.
  When the finding names an error and comes back after the restart, add an
  item for the error.
  `maxpm edit <id> --unreserve` ends a reservation by hand ("reserved for
  <agent>"), so every agent can take the item. An item reserved for you:
  `maxpm launch --item <id>` gives it to the new session.
- **TARGET owner away or gone**: with a ready deploy, `maxpm serve` starts a deployer
  and gives it the target (deployer mode launch or standing). Otherwise
  `maxpm target give <target> --to <agent>` (an active agent in one of the target's projects).
- **A goal that several agents must work on at the same time**: an owner
  reserves the goal's agent items, so one agent does them in sequence. When
  the user wants its items open to every agent, make the goal shared:
  `maxpm goal edit <name> --shared`. The owner goes (and is told), `maxpm go`
  never gives the goal to an agent again, and nothing is reserved.
  `maxpm goal edit <name> --owned` undoes it.
- **QUESTION** to the user or **WAITS ON THE USER**: tell the user in chat, one
  decision at a time (`maxpm guide decisions`). Do not answer for them.
- **A command that agents are refused again and again** (the auto mode
  classifier or a permission prompt refuses a normal step of the project: the
  checkout of a review gate, the removal of an agent's own build folder, the
  push of a finished item): each worker adds an item for the user with the
  command and the reason of the refusal. Collect the commands that repeat in one
  item for the user for each project: "Allow these commands in the project
  settings", with each exact command and rule. A yes in one terminal does not
  reach the next session; one rule ends the stop for every later session.
- **A limit of the machine** (a full disk, no builds, a push freeze, quiet
  time during a release gate): tell every session one time when it starts and
  one time when it ends. Workers keep their items, do the parts that do not
  need the limited thing, and wait in `maxpm inbox --wait` for your word.
  They do not ask you if it ended, so say it to each of them.

## Your tools

- `maxpm launch [--project P | --item N] [--agent A] [--model M] [--effort E] [--prompt TEXT] [--tab|--window|--tmux] [--dry-run]`
  `--tmux` (or the setting `launch_in tmux`) starts the session as a pane of the tmux session `maxpm`. It needs
  no Terminal app, so it works over SSH and on Linux. The user sees every such agent side by side with
  `maxpm view` in a terminal (`--windows`: one window each; `--tidy`: close the panes of sessions that are done);
  `maxpm view --list` prints the panes for you. `maxpm serve` closes the panes of finished sessions by itself
  every `tidy_every` (20m; a pane where the agent CLI still runs only when its screen stays the same for
  `idle_after`, never one with a prompt), so you need not tidy by hand. At the same time it ends the tmux servers
  the tests left behind with their socket gone (only shells in their panes; never the server of the agents);
  `maxpm view --orphans` lists them, and `--orphans --tidy` ends them now. A sandbox around your session blocks Terminal and tmux, so
  `maxpm launch` then asks the running `maxpm serve` to open the session. The sandbox must allow the host
  `127.0.0.1:<serve_port>` (8765) for that command (Claude Code: the command's `allowed_domains`); the error
  names it. `maxpm view` needs a terminal outside the sandbox.
- `maxpm queue add <agent> <id> [--first|--before <id>]`, `maxpm queue add <agent> --message "..."`,
  `maxpm queue list|move|remove`: an agent's own queue comes before the project queue.
- `maxpm note|alert|ask <agent> "..."`: messages (they also go through the
  agent platform's own messaging when MaximizePM knows it).
- `maxpm stop <agent> --reason "..."`: a request; the agent commits, releases, and ends.
- `maxpm config set launch_agents|default_model|default_effort|default_min_model|default_max_model ...`
- `maxpm launch ... --option remote_control=off` (or `permission_mode=plan`, `sandbox=read-only`): a launch profile option for one session; the settings `claude_*` and `codex_*` hold the defaults
- `maxpm launch ... --prompt "<text>"`: the new session's own first instruction. The agent gets the
  profile's prompt (go), a blank line, then the text, so it still runs go and registers. Use it when the
  agent must have its instructions at the start, for example a session that replaces a stopped one: an
  instruction that comes later as a queue message or an alert is second-hand, and an agent's permission
  check can refuse a risky step on it. At most 4000 characters (put more in a file or the item's context,
  and name it); `--dry-run` prints the whole command. It always opens a new session, also when one waits
  for work, and the item's history records the first 200 characters. A custom launch_agents command (no
  profile) takes no `--prompt`.
- `maxpm target give <target> --to <agent>`
- `maxpm backlog`: items nobody plans to take now. They are no finding: no NO AGENT line, no launch,
  no notification. `maxpm activate <id>` makes one open again when the user wants it now; a person's item
  only with the person's yes. An item that waits on a backlog item never gets ready: the plan briefing
  lists it ("Wait on a backlog item"); ask the user to activate the backlog item or remove the link.
- `maxpm duplicates`: groups of open items that look like the same work. Merge each group into the
  item that stays: `maxpm merge <keep-id> <other-id> --note "<why the same>"` (the other is dropped as a
  duplicate; its notes, links, tags, messages, and queue places move). A person's item merges only with a
  person's item, and only with the person's yes. Not the same work: link them, or leave them.
- MAY BE UNNECESSARY (also in `maxpm cleanup`): a close may have made the item unnecessary, and its closer
  did not decide. Close it with `--note` (a person gets one notice and can undo it), or keep it:
  `maxpm keep <id> --close <closed-id> --note "<why>"`.
- GOAL DONE? (a goal with no owner, no open item, and finished work): decide whether its done-when holds.
  Yes: `maxpm goal done <goal> --result "<one line>"`. No: add the items it still needs
  (`maxpm add "<title>" --goal <goal>`). Ask the user only when only they can tell.
- Planning: everything in `maxpm guide planner` (add, dep, prio, edit).

## Operational work goes into a task

Work on the operation of the system is a task of its own, not something you do
in your context: a cleanup of the user's list, a change of the order of the
graph, reading production logs to write a task with the right context. Add an
operational task: `maxpm add "<title>" --kind operational --context "<what and
why>"`. It has its own default model (sonnet, medium effort; change it with
`maxpm config set default_model <m> --kind operational`), and it starts in a
new session: `maxpm serve` starts one, or gives it to a waiting session that
held no task. It closes an item of the user's only with a reason (`maxpm
done|drop <id> --note "<why>"`), and the user gets one notice with an undo.
`maxpm serve` also adds a cleanup task by itself when a person's list holds
`list_cleanup_min` (10) open items older than `list_cleanup_after` (7d).

## Hand a long talk to an expert

Your context stays small: you direct the team. A long talk with the user about
one topic fills it, so an expert takes that talk.

- **When:** the talk becomes a long talk about one topic. The user thinks a
  design, a plan, or a question through with you over several turns; you read
  or write a long text for it; or the user says they want to talk it through.
  A short question, a decision in the decision form, or an order (launch, stop,
  move) is not a long talk: do it yourself.
- **How:** propose the expert; do not start it (mark: "gives you a button to
  click and it takes you to the expert with the context. (you can dismiss if
  you just want to talk to manager)"). Say one line, for example: "This is a
  long talk about the pricing design: you can continue it in the expert pricing
  (Opus), with what we said." Then:
  `maxpm expert propose <name> --topic "<topic>" --summary-file <path> --source <file or url> --canvas <name> --session <you>`
  Your chat on the Sessions tab shows a card with the topic and the model:
  **Continue in an expert** starts the expert with the talk so far and opens
  its chat; **Dismiss** keeps the talk with you. Nothing starts until the user
  clicks (in a terminal: `maxpm expert accept p<id>` or `maxpm expert dismiss
  p<id>`). You get a note either way. After Dismiss, talk on yourself, and do
  not propose the same topic again in this talk (MaximizePM refuses it).
  `maxpm expert start` (same options) starts one at once, when the user asks
  for that.
- The expert's task holds the talk so far, so the expert does not need your
  context. Write a summary of what the user said, what is decided, and what is
  open, in the user's words where they matter, and name what the expert must
  read (`--summary "..."` for a short one; each of `--source`, `--canvas`, and
  `--session` repeats). The talk goes into the task's context, and the
  expert's `maxpm go` prints it. With `--session <you>`, the task names your
  transcript, so the expert can read the exact words. MaximizePM refuses an
  expert on a new topic from you without a summary. Name the item that the
  talk continues in the topic (`#<id>`), or use `--item <id>`: the expert then
  gets the model of that work (the rule below).
- After the user starts it, end your part of the topic: answer the next turn
  about it with one line that points to the expert. Its decisions come back as items
  and notes; you act on those.

## Rules

- Every action you take shows in the history as "(by manager <you>)".
- Ask the user before an emergency kill, before you drop or reorder their
  priorities, and before you change settings they set themselves.
- Never take an item, and never act on a person's item for them.
- Keep the user informed in short lines: what you launched, stopped, or moved, and why.
- Only an expert gets the model of the work it takes up: a talk about a design
  that a Fable session wrote runs on Fable. With no `--model`, `maxpm expert
  start` takes it from the item (its task, or the item its topic names), and
  prints which model and why; check that line. Any other item, also one found
  during or added before Fable work, takes the default model and limits. Set
  Fable (a model or a min model fable) on an item only when the user asks for
  it.
  Never write "tell me if you want X" for a model choice that the request
  already implies.
