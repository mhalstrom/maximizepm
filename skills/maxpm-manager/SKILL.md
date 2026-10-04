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
2. Start `maxpm --as <you> inbox --wait` as a background command (Claude
   Code: `run_in_background`, which wakes the session when it exits; Codex:
   its background shell). It exits as soon as a message or question comes to
   you, prints it, and marks it read; after 25 minutes with nothing it exits
   too. Each time it exits, act on the messages and start it again. Skip it
   when the briefing says MaximizePM delivers messages into your session itself
   (`native_message`).
3. Start `maxpm --as <you> manage --watch` as a background command too (Claude
   Code: `run_in_background` with a time limit above `manage_every`, 30m; in a
   foreground shell with a 10-minute limit, add `--step 9m`). It exits when a
   new finding needs you (an agent is stuck or gone, an agent waits longer than
   `wait_too_long`, a project has ready agent work and no agent, a target owner
   is away or gone, a question for the user). A finding it reported before
   does not wake it again. With nothing new it exits after `manage_every` with
   one line. Messages to you do not wake it: the inbox poller brings them.
4. Act on what is new. Then start `manage --watch` again.

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
- **A release review nobody takes** (its authors are busy, or an author released it):
  `maxpm launch --item <review id>`. The new session reviews (role REVIEWER) in a
  folder of the release; a waiting session that worked on the release does not get it.
- **NOT CONNECTED**: a session MaximizePM started ran no maxpm command. Tell the
  user (its terminal may wait on a prompt; for an agent in tmux the user reads
  and answers it with the agent's Terminal button on the page, and only a
  person does that), stop it, and launch again. MaximizePM
  takes its push back after `connect_within`. A stop ends every reservation
  of the agent at once, and MaximizePM does the same for an agent that is gone.
  `maxpm edit <id> --unreserve` ends a reservation by hand ("reserved for
  <agent>"), so every agent can take the item. An item reserved for you:
  `maxpm launch --item <id>` gives it to the new session.
- **TARGET owner away or gone**: `maxpm target give <target> --to <agent>`
  (an active agent in one of the target's projects).
- **A goal that several agents must work on at the same time**: an owner
  reserves the goal's agent items, so one agent does them in sequence. When
  the user wants its items open to every agent, make the goal shared:
  `maxpm goal edit <name> --shared`. The owner goes (and is told), `maxpm go`
  never gives the goal to an agent again, and nothing is reserved.
  `maxpm goal edit <name> --owned` undoes it.
- **QUESTION** to the user or **WAITS ON THE USER**: tell the user in chat, one
  decision at a time (`maxpm guide decisions`). Do not answer for them.

## Your tools

- `maxpm launch [--project P | --item N] [--agent A] [--model M] [--effort E] [--prompt TEXT] [--tab|--window|--tmux] [--dry-run]`
  `--tmux` (or the setting `launch_in tmux`) starts the session as a pane of the tmux session `maxpm`. It needs
  no Terminal app, so it works over SSH and on Linux. The user sees every such agent side by side with
  `maxpm view` in a terminal (`--windows`: one window each; `--tidy`: close the panes of sessions that are done);
  `maxpm view --list` prints the panes for you. `maxpm serve` closes the panes of finished sessions by itself
  every `tidy_every` (20m; a pane where the agent CLI still runs only when its screen stays the same for
  `idle_after`, never one with a prompt), so you need not tidy by hand. A sandbox around your session blocks Terminal and tmux, so
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
- Planning: everything in `maxpm guide planner` (add, dep, prio, edit).

## Rules

- Every action you take shows in the history as "(by manager <you>)".
- Ask the user before an emergency kill, before you drop or reorder their
  priorities, and before you change settings they set themselves.
- Never take an item, and never act on a person's item for them.
- Keep the user informed in short lines: what you launched, stopped, or moved, and why.
