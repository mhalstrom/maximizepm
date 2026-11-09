# Teams on maximizepm.com (design, #4915)

Status: design of 2026-11-09 for mark's review. Nothing is built. This design
replaces the federation design (#4905, `federation-design.md`), which stays as
the earlier version. The decisions at the end are in the queue as a person's
item; the build items come after the answers.

## What mark asked

- "we want the product to be like teams at maximizepm.com. Users can create a
  team and a team is essentially just another DB that allows planning a
  project and tracking tasks and doing all that stuff. Once you have relay
  you can create a team for some amount, and you can join a team."
- "a couple other roles so that there's project monitoring for PMs, ways for
  people to understand the tasks of the projects. Essentially everybody has
  their own local agents and a place for collaborative work to be done."
- "a shared project that lives in the cloud that users can take goals,
  shared deployments, other roles like pm which maybe is just a mcp in claude
  to make tasks."
- Decision A (mark): the team's data lives on our servers, in our Cloudflare
  account beside the relay. Code never: code goes through the team's
  repository host. The relay tunnel keeps its rule (nothing that passes
  through it is stored); only a team, which a person creates on purpose, is
  stored.

## What changes from the federation design

| Federation (#4905) | Teams (this design) |
|---|---|
| A shared project lives on one person's computer (the home node). | A team project lives in the cloud, in the team's queue. Nobody's laptop must be on. |
| Claims fail while the home node is offline. | Claims work whenever the member is online. |
| A pool of items that a home node offers. | No pool: the team queue is the pool. Every ready agent item of a team project is open to the members' agents. |
| Peers name each other (`ann`, `bob`). | Members of a team, with roles, signed in with the relay account. |
| The relay passes frames and stores none. | The relay tunnel still stores nothing. The team service stores the team's queue, on purpose. |

What stays from the federation design: one writer decides every change (now
the team's queue in the cloud); claims and leases across computers; code
through the repository host (clone, branch, pull request); review by another
person; the list of what an agent of another person never gets; item numbers
`#123@<name>`; local items never leave the computer.

## Words in this design

- **Team**: a group of people, with one hosted queue. It has a short name
  (`acme`), unique on maximizepm.com.
- **Team queue**: the team's projects, items, dependencies, goals, release
  targets, messages, and history. It lives in one Durable Object of the team
  (section 9).
- **Local queue**: the queue on a person's own computer
  (`~/.maximizepm/maxpm.db`), as today. It never goes to the cloud.
- **Team copy**: a read copy of the team queue on a member's computer, kept
  current by the team service. It shows team work offline.
- **Team service**: the code at `teams.maximizepm.com` that holds the team
  queues, the team pages, and the PM's MCP server.

## The loop in eight steps

1. Ann has a relay plan. On `teams.maximizepm.com` she creates the team
   `acme` and the project `shop`, with its repository
   `https://github.com/acme/shop`.
2. Ann invites Bob as a member and Pat as a PM. Each opens the invite link
   and signs in with GitHub or Google (a relay account, no relay plan needed).
3. Bob runs `maxpm team join acme` once on his computer, clones the
   repository, and links it: `maxpm project path shop@acme ~/src/shop`.
4. Pat, in Claude with the MaximizePM connector, says "add the checkout
   retry as a P1 item of shop, after #12". Claude calls the team's MCP
   server; item `#14@acme` is in the team queue.
5. Bob's agent runs `maxpm go` in `~/src/shop`. Its computer asks the team
   service, which claims `#14@acme` for `builder-3@bob` with a lease.
6. Bob's agent works in its own worktree, pushes a branch, opens a pull
   request, and runs `maxpm done 14@acme --pr <url> --output "..."`.
7. The project's merge rule says "pull request", so the team queue adds a
   review item for another member. Ann's agent reviews and merges. `#14@acme`
   is done.
8. Pat sees it on the team page and through the connector: the goal's
   progress, who did it, and the release it waits for.

Code never crosses MaximizePM. MaximizePM carries queue data only.

## 1. The team and the local queue

### One hosted queue per team

- A team queue has the same model as a local queue: projects, items,
  dependencies, goals, release targets, review steps, messages, events,
  agents, needs-you. It runs the same rules: readiness, priority order,
  claims and leases, model limits, holds, goal ownership, the close check.
- The team service runs the same Python code as local MaximizePM (`core.py`)
  on the team's SQLite database (section 9). So a rule changes in one place,
  and a team behaves like a local queue in every detail.
- What a team queue does not have: anything that runs on a computer. No
  terminals, no tmux, no launch of sessions, no hooks that run there, no
  canvases with files, no settings for a computer (`native_message`,
  `web_terminal`). Those stay on each member's computer.

### How a member's computer works with the team

- `maxpm team join <team>` (once; the river token of `maxpm connect`
  identifies the person) makes a team copy:
  `~/.maximizepm/teams/<team>.db`, a file with the same schema as a local
  queue. Reads (`list`, `show`, `blockers`, the page) run the normal code on
  that file.
- Every write to a team item (`claim`, `done`, `add`, `edit`, `dep`, `send`)
  goes to the team service as one request, the same shape as the page's
  `POST /api/action {op, args, actor}`. The team service runs it and answers
  with the result or the refusal text that a local command prints.
- The team service sends every changed row to each member's computer that is
  online, over one WebSocket per team that `maxpm serve` keeps open (the
  relay's socket code, river/relay.py, and the Hibernation API on the
  service side). Each change has a number; a computer that missed changes
  asks for everything after its last number. Applying a row twice does no
  harm.
- Item numbers: a local item stays `#123`. A team item shows as `#14@acme`,
  and commands take `14@acme`. Agent names on the team are
  `<agent>@<person>` (`builder-3@bob`), so the history says whose agent did
  what.
- Folders stay local: `maxpm project path shop@acme ~/src/shop` writes into
  the local queue. The team never sees a member's paths.

### `maxpm go`: local work and team work

- **The folder decides first.** In a folder linked to a team project, `go`
  gives that team project's work (with goals, reviews, deploys of that team
  project as today). In a folder linked to a local project, local work.
- **No folder project** (a chat, a home folder): local and team projects
  form one list, in the person's own project rank. A team project that a
  person joins takes the place after their local projects; they move it with
  `maxpm project rank shop@acme 2`.
- A person can turn team work off for their agents, per team:
  `maxpm config set team_work off --project shop@acme` (on by default for a
  member). `maxpm wait` wakes an agent when a team item becomes ready for it.
- The limits stay per person: `max_sessions` and `max_leases` count local and
  team leases together. The team can also set a limit per member
  (`team_member_max`, default 3 held items), so one member's agents do not
  take the whole queue.
- The model limits of a team item hold as today: the computer sends its
  agent's model and type with the claim.

### Leases across computers

- A team lease lives in the team queue. The member's `maxpm serve` renews the
  leases of its busy agents in one request every 5 minutes, with the same
  signs of work that renew a local lease today.
- The length is the team's `lease_ttl` (default 2h for team projects, against
  30 minutes locally): long enough for real work, short enough that a dead
  laptop does not hold an item for a day.
- When a lease ends, the item goes back to open with a note, and the normal
  CHECK FIRST rule of `go` applies to the next taker (the branch may hold the
  work).

### Offline

- **The member is offline**: local work goes on. The team copy shows team
  work, marked "offline since 14:02". Claims of team items are refused (two
  agents must never take one item). An agent that holds a team item keeps
  working: notes, questions, and `done` wait in the computer's outbox and go
  when it is back. If the lease ended first, the done gets the normal refusal
  for an item the agent does not hold; the branch and the pull request stay.
- **The team service is down** (a Cloudflare outage): the same as offline,
  for every member. Nothing local depends on it.
- **A local item can wait on a team item** (`maxpm dep 55 --on 14@acme`): the
  link and the state stay local. A team item cannot wait on a local item:
  the team cannot see it. The refusal says the fix: add the item to the team
  project.

## 2. Members and roles

### Roles

| Role | Can | Seat |
|---|---|---|
| **owner** | Everything a PM and a member can; members, roles, billing, team settings, export, delete the team. A team has one or more owners. | paid |
| **member** | Take items and goals with local agents; add and edit items; message; review; own a release target when an owner allows it. | paid |
| **PM** | Plan: add, edit, rank, and drop items; dependencies; goals and their rank; ask and answer; read all progress. Takes no items and owns no goals. Works through the page and the MCP server; needs no local install. | paid (decision 6) |
| **viewer** | Read the team page and progress; comment on items. | free |

- A person has one role per team, and can be in several teams.
- Settings that run commands (target hooks, review `run` steps) change only
  by an owner (section 3).

### Invites

- An owner or a PM invites from the team page or with
  `maxpm team invite acme --role member`. The invite is a link with a random
  secret, for one person, that ends after 7 days.
- The person opens the link, signs in with GitHub or Google (a relay
  account; made at this step when they have none), sees the team, the role,
  and who invited them, and accepts.
- A member then runs `maxpm team join acme` on each computer that works for
  the team. A PM or a viewer needs nothing on a computer.

### Removal

An owner removes a person on the members page, or the person leaves.

- **Claims**: every item that the person's agents hold goes back to open at
  once, with a note "bob left the team; check the branch". Reservations and
  pushes to the person end.
- **Goals and targets**: the person's goals and release targets become free;
  the handoff stays with the goal. An owner gets a needs-you entry for each
  target with no owner.
- **Access**: the person's sessions and the team's tokens for their
  computers stop at once. The team service tells their computers to delete
  the team copy; a computer that is offline deletes it when it next connects.
  The team cannot force a delete on a computer that it does not control; the
  invite page says so.
- **History**: the team keeps everything the person did, with their actor
  names. Items that the person added stay.

## 3. Goals and shared deployments

### Goals

- Goals work as today, in the team queue: a member's agent owns a goal
  (`maxpm go --role owner`, or the owner briefing), its items are reserved
  for that agent, and its handoff is kept with every version.
- A team goal can also have a **person owner**: `maxpm goal own <goal>@acme
  --person bob`. Then the goal's items are reserved for Bob's agents, any of
  them, and the goal's handoff is Bob's to keep. This fits a team: a person
  answers for an outcome, and their agents come and go.
- A PM adds goals, ranks them, and gives them to a person; a PM never owns
  one.
- Shared goals (no owner, open to every member's agents) work as today.

### Release targets of a team

- A team project has its release target in the team queue: the deploy item,
  the release cut, the review, the cadence, `release-now`.
- **The deploy owner** is a member, set by an owner
  (`maxpm target own <target>@acme --person ann`), with a second member as
  backup. The deploy item goes only to the deploy owner's agents.
- **Where the deploy runs**: on the deploy owner's computer. The deploy
  credentials (cloud keys, tokens) stay there; the team never stores them.
- **The review** of a release: on by default for a team target, by a member
  who is not an author of any item in the release (today's author rule,
  extended from agents to people: `review_other_person`, on by default in a
  team).
- **Hooks**: the commands of a target hook are text in the team queue, set by
  an owner only. They run only on the deploy owner's computer, and only after
  the deploy owner approves that exact text once on that computer (MaximizePM
  keeps a hash). A changed hook needs a new approval. So no team member can
  make a command run on another member's computer.
- **Monitor items** go to the deploy owner's agents first, then to any member
  after 15 minutes.

### Code: the repository host

- The team project records the repository: `maxpm project repo shop@acme
  https://github.com/acme/shop`. Access is on the host, not in MaximizePM.
- A project setting `merge`: `pr` (default) or `direct`.
  - `pr`: the briefing tells the agent to work on a branch
    `<id>-<words>` in its own worktree, push it, and open a pull request.
    `done` needs `--pr <url>`. The team queue adds a review item "Review the
    pull request of #14 from bob" for another member; pass merges and closes
    #14; fail sends #14 back to the same person's agents for 24 hours, then to
    open. This is the review loop of the federation design.
  - `direct`: the team trusts its members to push to main, as a person does
    alone today. The release review still checks the whole release.

## 4. The PM role through MCP

- A remote MCP server at `https://teams.maximizepm.com/mcp` (the Streamable
  HTTP transport). Claude (a custom connector) and ChatGPT (a connector in
  developer mode or an app) connect to it by its address.
- Sign-in: OAuth 2.1 with dynamic client registration, which both clients
  need. The OAuth screen is the relay's sign-in (GitHub or Google). After
  sign-in, the person picks the teams that this connector may use. The token
  carries the person, the teams, and the person's role in each.
- Tools (one call each; the answer is the same text as the CLI):
  - `teams`: the person's teams and roles.
  - `progress`: per goal and project: done, open, in progress, blocked, what
    waits on a person, the next release.
  - `list`, `show`, `search`, `blockers`: read items.
  - `add`: an item with title, notes, project, priority, goal, doer, after
    (dependencies), due.
  - `edit`, `prio`, `rank`, `dep`, `drop`: change items.
  - `goal`: add, edit, rank, give to a person, show.
  - `ask`, `answer`, `note`, `inbox`: messages with members.
  - `needs_you`: what waits on this person.
  - `plan`: the planner briefing, with a plan as text in, items out
    (`add_plan`).
- The role decides the tools: a viewer gets the read tools only; a PM and an
  owner get all of them; no tool claims an item or runs a command anywhere.
- Members' agents use their local `maxpm` (CLI or the local MCP server), not
  this server: their work needs the local folder and the local sessions.

## 5. Monitoring pages for PMs

`https://teams.maximizepm.com/<team>`, signed in with the relay account.

- **Board**: projects and goals with progress (done, open, in progress,
  blocked), the next release of each target, and the counts per person.
- **Items**: the list with filters (project, goal, person, state, priority),
  and the item drawer with notes, history, messages, and the pull request.
- **People**: each member, their agents that hold team items now, what each
  holds, since when, and what each finished in the last 7 days.
- **Waits on a person**: the needs-you items and open questions of every
  member, with their age.
- **Releases**: per target: the open deploy item, what it ships, the review
  state, the last deploys.
- **Graph**: the dependency graph of a project or a goal.
- The pages reuse the local page's parts (`static/components`: card, chip,
  drawer, table, graphText, panZoom) with a team mode: the team service
  answers `/api/state` with `core.state` of the team queue, in the same
  shape. Parts that need a computer (Sessions, terminals, Launch) do not
  show.
- The local page of a member shows team projects next to local ones, from the
  team copy, marked with the team's name.

## 6. Accounts and price

- **Accounts**: the relay account (GitHub or Google sign-in) is the account
  for teams too. The relay asks for no email address; a team asks for one
  only when a person turns on mail notifications.
- **To create a team**: the creator needs an active relay plan ($7.99 a month
  or $79 a year), as mark said.
- **A member, PM, or viewer** needs only a relay account, not a relay plan.
  `maxpm team join` uses the river token of `maxpm connect`, which works
  without a plan (the plan opens the tunnel only).
- **Price of a team**, options:
  - A. Per seat: $6 per member, PM, or owner seat a month ($60 a year);
    viewers free. Stripe bills one subscription with a quantity; a new seat
    is prorated.
  - B. Per team: $20 a month for up to 5 seats, $40 up to 15.
  - C. Both: $10 a month per team plus $4 a seat.
- **Trial**: 14 days for a new team, up to 5 seats, with a card at checkout
  (as the relay's trial). Promotion codes work as on the relay.
- **Free tier**: none at the start; promotion codes cover friends and early
  teams. A free team of two seats can come later (decision 8).
- **Stripe**: a second product beside the relay plan, with Managed Payments
  as on the relay. The owner who pays is the team's billing owner; another
  owner can take it over.
- **A lapsed team plan**: while Stripe retries (`past_due`), everything
  works. When it ends, the team is read-only for 30 days (pages, MCP read
  tools, export, the team copies), then deleted, with mails to the owners at
  the start and 7 days before.
- **Our cost**: one Durable Object per team with its SQLite storage, and a
  request per command. Small against $6 a seat. The model tokens are each
  member's own.

## 7. Security

### What we store

- The team queue only: projects, items (title, notes, context, output,
  check, touches), dependencies, goals and handoffs, targets and hooks (as
  text), messages, events, agent names and models, and needs-you.
- Never: code, files, diffs, terminals, screenshots, canvases, prompts or
  transcripts of sessions, local paths, credentials, settings of a computer.
- The team service refuses an item, message, or handoff whose text looks like
  a secret (a check by pattern: `sk_live_`, `ghp_`, `AKIA`, `-----BEGIN`),
  with the reason.
- The relay tunnel keeps its rule; the team service is a separate Worker
  (section 9), so the rule "the relay stores nothing that passes" stays true
  in its code and its tests.

### Encryption, backup, deletion, export

- **At rest**: Cloudflare encrypts Durable Object and D1 storage at rest. In
  transit: TLS. Not end to end: the team service must read the items to run
  the rules, the pages, and the MCP server (decision 10).
- **Backup**: the Durable Object's point-in-time recovery (30 days) for the
  team queue. The team directory (teams, members, invites, billing) is in D1,
  with D1 Time Travel. The relay's rule "rebuild from Stripe" covers the
  accounts; it cannot cover queue data.
- **Export**: an owner downloads the team queue at any time as a MaximizePM
  database file (`acme.db`, the local schema) or as JSON. `maxpm team export
  acme` does the same. A team can leave us and run its queue on a local
  computer.
- **Deletion**: an owner deletes the team; it is read-only for 7 days (undo
  and export), then the service deletes the Durable Object's storage and the
  team's D1 rows. The recovery copy is gone after 30 more days. The privacy
  page says so (a later item, with mark's yes, as Decision A said).

### Audit log

- Every change in the team queue is an event, as today, with the actor
  (`builder-3@bob`, `pat@mcp`, `ann@page`).
- A team audit for owners: invites, joins, role changes, removals, MCP
  connectors that signed in, exports, hook changes and approvals, billing
  changes, deletion. Kept for one year; owners read it on the team page.

### What an agent of one member can and cannot do

- **Can**: what its person's role allows, through its person's computer:
  claim and finish items, add items, message, own goals, review, and deploy
  for a target its person owns.
- **Cannot**: see another member's local queue, files, sessions, or settings;
  run a command on another member's computer (hooks run only on the deploy
  owner's computer, after that person's approval); change roles, billing, or
  hooks; take items reserved for another person's goal; review its own
  person's work when `review_other_person` is on; stop another member's
  agents (`maxpm stop builder-3@bob` is refused; it can alert bob).
- **Text from the team is text from other people.** The briefing marks it:
  "team acme, from pat: ...". The `check` and `touches` fields of a team item
  show as advice; no command from the team runs by itself.
- A member's computer authenticates with a team token that the join gives,
  per computer, which an owner or the member can revoke on the team page.

## 8. Slices

### Slice 1 (one week): the whole loop

Create a team, invite one member, a PM adds an item through MCP, the
member's local agent takes it and finishes it, the PM sees it done.

- **Day 1 to 2, the check that decides the stack (decision 1)**: `core.py`
  in a Python Durable Object (Cloudflare Python Workers, Pyodide), on the
  Durable Object's SQLite through a small adapter that gives `core` the
  `sqlite3` calls it uses (execute, rows by name, `with tx`). Pass: the
  tests of Ordering, Claims, Goals, and Messages run against the adapter,
  and a claim answers in under 200 ms after a cold start. If it fails, the
  fallback (decision 1) makes slice 1 two weeks.
- **Team service** (a new Worker, `maxpm-teams`, at teams.maximizepm.com):
  sign-in through the relay account (the relay's D1, read only, for users);
  D1 tables `teams`, `members`, `invites`, `team_tokens`, `team_audit`;
  create a team (behind a flag, no billing in slice 1); invite and accept;
  `POST /t/<team>/api/action` with a team token; the WebSocket of changes.
- **The Team Durable Object** (Python): one per team, runs `core` on its
  SQLite, with the ops that a team needs (add, edit, claim through `next`,
  renew, done with `--pr`, show, list, state, send, answer).
- **MCP**: `/mcp` with OAuth (dynamic client registration) and the tools
  `teams`, `progress`, `list`, `show`, `add`.
- **River (local)**: `maxpm team join|list|leave`; the team copy file and its
  sync; `14@acme` in `show`, `done`, and `release`; `go` gives a team item in
  a folder linked to a team project; lease renewal from `maxpm serve`; the
  outbox.
- **Page**: a read-only team Board (projects, items, who holds what) at
  `teams.maximizepm.com/<team>`, with the local page's parts.
- **Tests**: the Team object's tests with the core tests on the adapter; a
  local test with a fake team service that runs the loop (join, a PM add,
  go, done, the PM's progress); the Worker's tests (no team data in the
  relay's D1 or the River object).

### Later slices

2. **Goals and people**: person owners of goals, the owner briefing for team
   goals, `ask|alert|note <name>@<person>`, needs-you of team items on each
   member's local list and notifications.
3. **Pull request review**: `merge pr|direct`, the review item, rework back
   to the taker, `review_other_person`.
4. **Shared deployments**: team targets, deploy owner and backup, release
   review, hooks with approval on the deploy owner's computer, monitors.
5. **PM pages and MCP, the rest**: People, Waits on a person, Releases,
   Graph; all MCP tools; the page's Add and Edit.
6. **Billing**: the Stripe product, seats, trial, lapsed team, the team's
   pages in the account site.
7. **Owners' tools**: removal with its rules, export and import, deletion
   with the 7 days, the audit page, the secret check on text.
8. **Mail notifications** for PMs and viewers who have no local MaximizePM.

## 9. How the team service is built

- **A new Worker** `maxpm-teams` beside the relay Worker, in the same
  Cloudflare account, on `teams.maximizepm.com`. It reads the relay's D1 for
  accounts and sign-in sessions (one sign-in for both), and has its own D1
  tables for teams. The relay Worker keeps its rule and its tests unchanged.
- **One Durable Object per team** (`Team`), with SQLite storage: one writer
  per team, so two claims never interleave (the same reason as
  `BEGIN IMMEDIATE` locally); up to 10 GB per team; point-in-time recovery.
  A Durable Object per team, not one D1 for all teams: one team's load never
  slows another, and deletion is one call.
- **The same Python code**: the Team object runs `river/core.py` (standard
  library only, so it loads in Pyodide) on an adapter over the object's SQL
  storage. The team service and local MaximizePM then share one rule set,
  and the team's `/api/state` has the page's shape. The front of the Worker
  (sign-in, OAuth, MCP transport, routing) is TypeScript, as the relay is,
  and calls the Team object.
- **Fallback, if Python in a Durable Object fails the check**:
  - B. Cloudflare Containers: one small container per team that runs
    `maxpm serve` on a database file, with the file copied to R2 after each
    change. Same code; slower start, more to run.
  - C. A TypeScript port of the parts of `core` that a team needs (claims,
    readiness, goals, messages). Fast, but two copies of every rule.

## Decisions for mark

1. **Where the team queue's code runs.** A: `core.py` in a Python Durable
   Object per team (one rule set); B: a container per team; C: a TypeScript
   port. Recommendation: A, with a two-day check first; B if it fails.
2. **One Durable Object per team, D1 for the directory** (teams, members,
   invites, billing). Recommendation: yes.
3. **A separate Worker at teams.maximizepm.com**, so the relay's rule and
   tests stay as they are. Recommendation: yes, one sign-in for both.
4. **`maxpm go` order**: the folder's project first; else one list in the
   person's project rank, new team projects after local ones.
   Recommendation: yes.
5. **The team lease**: 2 hours, renewed every 5 minutes by `maxpm serve`
   while the agent works; `team_member_max` 3 held items per person.
   Recommendation: yes.
6. **Roles**: owner, member, PM, viewer; PM and member are paid seats, viewer
   free. Recommendation: yes.
7. **Price**: A, $6 a seat a month ($60 a year), viewers free; B, $20 or $40
   per team; C, $10 a team plus $4 a seat. Recommendation: A.
8. **Trial and free tier**: a 14-day trial with a card; no free tier at the
   start, promotion codes instead. Recommendation: yes.
9. **A member needs only a relay account, not a relay plan.** The creator
   needs a relay plan. Recommendation: yes.
10. **Encryption**: at rest and in transit, not end to end (the service must
    read items for the rules, pages, and MCP). Recommendation: yes, said
    plainly on the privacy page.
11. **Merge rule per project**: `pr` (default, review by another member) or
    `direct`. Recommendation: `pr` as the default.
12. **Hooks of a team target** run only on the deploy owner's computer,
    after that person approves the exact text once. Recommendation: yes.
13. **Goals with a person owner** (any of that person's agents) besides an
    agent owner. Recommendation: yes, in slice 2.
14. **A lapsed or deleted team**: read-only 30 days after a plan ends, 7
    days after a delete, then gone (the recovery copy 30 days later).
    Recommendation: yes.
15. **Slice 1** as in section 8: the whole loop in one week with MCP add and
    a read-only page, no billing (behind a flag). Recommendation: yes.
16. **The federation design (#4905)**: no build; its pool and peer parts
    stay as a record. Recommendation: yes.
