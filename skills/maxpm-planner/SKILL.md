---
name: maxpm-planner
description: Plan work with the user into MaximizePM queue items with dependencies and priorities (the `maxpm` command). Use when the user says "plan" in a project that uses MaximizePM, when you plan a project into tasks, load a checklist into the queue, re-rank projects, or fix priorities and dependencies.
---

# MaximizePM: planning work into the queue

## The planning conversation

When the user says "plan", run `maxpm plan` in the project folder. It names
you, makes you a planner (you change the plan; claims refuse), and prints the
overview and the open questions. Then talk with the user before you add
anything. (To also run the other agents, launch them, fill their queues, and
stop stuck ones, the user starts a manager session instead: `maxpm manage`,
rules in `maxpm guide manager`.)

1. **Ask for the outcome first.** "What must be true when this is done, and by
   when?" One or two sentences. Do not add items until you have it. Put the
   date on the outcome item: `--due 2026-10-15` (end of that day) or
   `--due "fri 17:00 America/New_York"`. Its prerequisites show the date too.
   A due date does not change the order; MaximizePM warns each person when it is
   `due_warn_before` (3d) away and again when it passes.
2. **Ask only what changes the plan.** Scope, deadline, who does which steps
   (agents or the user), what already exists, and what must not change. Take
   the open questions from the briefing that bear on this outcome; skip the
   rest.
3. **Make each outcome a goal when an agent should own it.** A goal is an
   outcome with a done-when test (`maxpm goal add`, below). Its owner plans
   and adjusts the items, so the items you add under it are first steps, not
   a full plan. Small or one-off work needs no goal.
4. **Split the outcome into items you can check.** Each item is one result that
   one agent or person finishes, with a `--check` command or a plain test ("the
   page loads at /pricing"). A step only the user can do (accounts, payments,
   legal, a decision) is its own `--doer human` item. For a person who is not
   in MaximizePM (a client, a colleague), still use `--doer human`, and say in
   `--context` who does it and that the user records the result with `maxpm done`.
5. **Show the plan before you write it.** A short numbered list with the
   waits-on links. Change it until the user agrees, then add it.
6. **Set priority on the outcome only** (`maxpm prio <id> 0`). Its prerequisites
   inherit it. A new project gets the lowest rank, so equal priorities in older
   projects go first. When the new outcome must go before them, ask the user,
   then run `maxpm project rank <name> 1`.
7. **Make a new project** only for a separate area with its own folder or
   goal (`maxpm project add <name> --path <dir> --description "..."`).
   Each project has the setting `result_look`: how a finished result (a
   page, a text, a design) gets the user's look. `push_first`: the worker
   pushes, closes the item, and adds an item for the look. `ask_first`: the
   worker shows the result and waits for the user's yes before the push.
   No project gets its value in silence. One fact decides what you
   recommend: does something stand between a push and the users? A deploy
   target with a review or a release step: recommend `push_first`. No target,
   or a push to main that is live at once (a site, a public repository):
   recommend `ask_first`. Tell the user the recommendation and its reason,
   and store the value the user confirms: `--result-look ask_first|push_first`
   on `maxpm project add`, or later
   `maxpm config set result_look ask_first|push_first --project <name>`.
   A project where nobody chose has `ask_first`. The reply of
   `maxpm project add` and `maxpm project show <name>` print the value in
   force and the recommendation.
   Otherwise add to the existing project. When `maxpm plan` says the folder
   has no project, the projects it lists are other work: leave them alone
   unless the user names them.
8. **Report progress** from the queue, not from memory: `maxpm status`,
   `maxpm log --since 7d`, `maxpm blockers <id>`.

End by saying what is ready now, what waits on the user, and that `maxpm go`
in an agent session starts the work.

## Projects

```
maxpm project add <name> [--rank N] --description "..."   # lower-case name
maxpm project describe <name> "..."
maxpm project show <name>
maxpm project rank <name> <N>                          # 1 = most important overall
maxpm project rename <name> <new>                      # all of it follows; the old name stops at once
maxpm project list
maxpm target add <name> --description "how it deploys"  # where projects ship to
maxpm project target <name> <target>                   # each project has at most one
maxpm target show <target>                             # its projects and owner
maxpm target rename <target> <new>                     # with its deploy project deploy-<target>
maxpm target own <target>                              # one owner per target runs its deploys
maxpm target monitor <target> "<what to watch, for how long>"   # a session follows each deploy
maxpm target cadence <target> 2h|1d|1w|1mo|off         # shortest time between releases; ship requests collect
maxpm target cut <target> --rev <commit>               # fix the release's items now; later ships join the next one
maxpm target give <target> --to <agent>                # or: maxpm target release <target>
```

A target has at most one owner. Ownership lasts `owner_ttl` (default 8h) and
every command by the owner renews it; when it expires the target is free and
the old owner gets a notice. `maxpm target own` names the current owner when
it refuses. When that owner is away or gone (`away_after`, default 1h),
`maxpm target own <target> --takeover "<why>"` moves the target at once and
tells the old owner why. A person can give any target:
`maxpm target give <target> --to <agent>`.

`maxpm ship <id>` (or `maxpm done <id> --ship`) puts an item in its target's
open deploy item, in the project `deploy-<target>`. One open deploy item per
target collects requests from every project on it; once the owner claims it,
the next request starts a new one. The deploy item waits on what it ships and
takes the best priority among them.

Write the description for an agent that must decide whether it fits: what the
project covers, where it lives (repository, directories), and what knowledge
helps. Agents read `maxpm project list` to pick an area.

### Cleanup

`maxpm plan` lists items that may be done or stale (from `maxpm cleanup`).
Check each one, or ask the user about a person's item, and record the result
with `maxpm check <id> done|partial|open --note "..."`. `stale_after` (14d)
sets when an untaken ready item counts as stale.

### Outside trackers

When the user says the project uses an issue tracker (Jira, GitHub Issues,
Linear...), record it once, in words an agent can act on:

```
maxpm project tracker <name> "github owner/repo via gh"
maxpm project tracker <name> "jira PROJ via the Jira MCP server"
```

`maxpm plan` then names the tracker. Import its open issues before you plan
new work:

1. Read the open issues with the tool the tracker line names (gh, a Jira or
   Linear MCP server, a command the user set up).
2. Skip each issue MaximizePM has already: `maxpm list --ref <tracker>:<key>`.
   MaximizePM also refuses a second open item with the same link in a project.
3. Add the others with the link and what the issue says:
   `maxpm add "<title>" --ref github:owner/repo#12 --context "..."`, or
   `--ref jira:PROJ-123 --ref-url <link>` (GitHub links get a URL by themselves).
4. Keep the tracker's priority (`-p 0..4`) and order, and record what must
   come first (`maxpm dep <id> --on <id>`).

Ask the user before you import a large backlog or issues assigned to other
people. MaximizePM has no tracker code: you read and write the tracker with your
own tools.

## Goals

A goal is an outcome in a project with a test for "done". One agent owns a
goal at a time; it creates and takes the items that reach it and tags them.
Goals are optional: an item can have no goal, one, or several. Goal progress
counts only the items tagged with it.

Write the outcome as what is true at the end, and done-when as a test someone
can check ("a real card payment succeeds"). Rank a project's goals so free
owners take the most important first. A business goal and the technical goals
under it can share items: tag an item with both.

An owner reserves the goal's agent items, so one agent does them in sequence.
When several agents must work on a goal at the same time, make it shared
(`--shared`): it has no owner, `maxpm go` gives it to no agent, and its items
stay open to every agent. A person or a manager sets and clears this.

```
maxpm goal add <project> <name> --outcome "..." --done-when "..." [--rank N] [--shared]
maxpm goal list [--project P] [--all]    # open goals in order, with owner and progress
maxpm goal show <name>                    # the goal and its items
maxpm goal rank <name> <N>                # 1 = first among the project's goals
maxpm goal edit <name> [--outcome] [--done-when] [--rename]
maxpm goal edit <name> --shared           # no owner: its items stay open to every agent; --owned undoes it
maxpm goal own <name> / maxpm goal release <name> / maxpm goal give <name> --to <agent>
maxpm goal done <name> --result "<one line>" [--drop-open]   # refused while its items are open
maxpm goal reopen <name>
maxpm add "<title>" --goal <name>         # repeatable; default: the goal you own in that project
maxpm add "<title>" --no-goal             # no tag, even when you own a goal
maxpm edit <id> --goal <name> / --untag <name>
maxpm list --goal <name>
```

## Items

```
maxpm add <project> "<title>" [-p 0-4] [--doer any|ai|human] [--after <id> ...] [--feeds <id> ...] [--notes "..."]
          [--context "..."] [--touches <file> ...] [--check "<command>"]
          [--model <m>] [--effort <level>] [--min-model <m>] [--max-model <m>] [--agent codex|claude-code]
maxpm add [project] --from plan.md [--dry-run]   # one item per line of an outline: a line waits on
                                    # the lines indented under it; 'P0' at the start and '(human)' or
                                    # '(ai)' at the end set priority and doer; '[x]' lines are skipped
maxpm dep <id> --on <id> ...        # <id> waits on the others
maxpm dep <id> --on <id> --kind feeds      # waits, then reads their output in its context
                                           # (changes an existing --after link to feeds; show marks it)
maxpm dep <id> --on <id> --kind conflicts  # no order, never in progress together
maxpm undep <id> --on <id> ...
maxpm prio <id> <0-4>               # 0 is most important
maxpm move <id> --before|--after <id>   # manual order inside a project
maxpm edit <id> [--title] [--notes] [--doer] [--project] [--context] [--touches ...] [--check] [--due <date>|none]
          [--model|--effort|--min-model|--max-model|--agent <value>|none]
maxpm blocked <id> --reason "..." [--until "mon 07:00 America/New_York"] / maxpm unblock <id>
maxpm drop <id> / maxpm reopen <id>
maxpm replanned <id> [--note "..."]   # clear the replan mark after you split or re-scope the item
```

## How the order works

An item is ready when it is open, has no outside blocker, everything it
waits on is done or dropped, and no item it conflicts with is in progress.
A conflict keeps two agents apart: the agent that holds the other item can
take the item (it edits the same files in sequence).
MaximizePM adds a conflict by itself when two open items' `--touches` overlap (the
same file, or a directory and a file in it); change the touches and MaximizePM
removes it. Use `feeds` when the later item needs a name, path, or signature
that the earlier item creates: the earlier item's `--output` shows in the
later item's briefing. Ready items sort by:

1. Effective priority: the best priority of the item and of every open item
   that waits on it, directly or indirectly. A P3 task that blocks a P0 task
   is P0.
2. Project rank.
3. How many open items it unblocks.
4. Manual order (`maxpm move`).
5. Age.

So set a high priority only on the outcome you care about (for example, "submit
Stripe activation" P0); its prerequisites inherit it. Do not raise every step.

## Writing good items

- One outcome per item that one agent or person can finish and check.
- Give an agent what it needs to start without searching. `--context`: why the
  item exists, where to look, decisions already made. `--touches`: the files
  it changes. `--check`: the command that shows it works. `maxpm next` and
  `maxpm go` print these three in one block. Use `--notes` for anything else.
- Mark who can do it: `--doer human` for account, legal, and payment steps;
  `--doer ai` for code and text work; `any` otherwise.
- Record every "needs first" as a dependency. The tool refuses loops.

## Model and effort

Each item can recommend a model (`--model`) and an effort level (`--effort`:
one of the `effort_levels` setting, `low, medium, high, xhigh, max`). The
recommendation says which agent to start; it never keeps a session off the item.
Pick the weakest model that does the item well:

- A weaker model (sonnet, luna) and low effort: routine, well specified work.
  Monitors, checks, renames, text changes, a fix whose cause is known.
- A middle model (opus, terra or sol) and medium or high effort: normal
  feature work that follows code already there.
- The strongest model (fable, astra) and high effort or more: design with
  few examples to follow, a hard bug, security, or data that is costly to lose.

Hard limits keep sessions off an item: `--min-model opus` (weaker sessions skip
it) and `--max-model sonnet` (do not spend a strong model on it). Set a limit
only when a wrong model is costly; the recommendation is enough otherwise.
The `model_ladder` setting orders the models, weakest first, one list per
family: `claude: haiku, sonnet, opus, fable; openai: luna, terra, sol, astra`. There
is no order across families, so a limit applies only to sessions of its
family. Name one model per family to limit both: `--min-model opus,sol`.

Defaults come from settings: `default_model`, `default_effort`,
`default_min_model`, `default_max_model`, per project (`--project`) or per
item kind (`--kind deploy`). An item's own value wins. A session declares its
model with `MAXPM_MODEL=<model>` or `maxpm go --model <model>`.

## Agent type

An item can need one agent type: `--agent codex` or `--agent claude-code` (a
launch_agents label such as `Codex` works too). Sessions of another type skip
it, and Start, Dispatch, and `maxpm launch` start that type. Use it when one
agent does the work better, for example Codex for images and CSS. Keep the
item's models in that type's family (Codex: luna, terra, sol, astra; Claude
Code: haiku, sonnet, opus, fable). Set rules once instead of on every item:
`maxpm config set agent_rules "codex: *.css, *.svg, image, logo"` (a pattern
with `*`, `?`, `/` or `.` matches a file in `--touches`; any other is a word in
the title). `default_agent` is the fallback, per project or kind. A person can
still push an item to a session of another type.

## Settings

`maxpm config get` lists every setting. `maxpm config set <key> <value>
[--project P | --agent A | --item N | --kind K]`. The most specific value wins: item,
agent, item kind, project, global, default. For example, a person's lease:
`maxpm config set lease_ttl 7d --agent alex`.
