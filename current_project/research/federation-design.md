# Shared projects and task pools between nodes, and team features (design, #4905)

Status: design of 2026-11-09 for mark's review. Nothing is built. The
decisions at the end are in the queue as a person's item; the build items
come after the answers.

## What mark asked

- "we also need to think about features to add team features to maxpm"
- "i am thinking about federate feature for maximizepm"
- "i mean users can allow their nodes to share projects"
- "we can make projects that are federated and task pools for those
  federated projects so if a user has completed their local tasks they can
  select from the shared pool"

The task pool is the center of this design. Sharing a project to read it or
comment on it is a smaller part of the same model.

## Words in this design

- **Node**: one MaximizePM: one queue file (`~/.maximizepm/maxpm.db`) and the
  `maxpm serve` that runs beside it, on one computer. Today each person has
  one node, and a node knows nothing of other nodes.
- **Shared project**: a project that its node shares with other nodes.
- **Home node**: the node where the shared project lives. Its queue file is
  the true copy of the project.
- **Member node**: a node that the home node lets in. It keeps a copy of the
  project and sends each change to the home node.
- **Pool**: the ready items of a shared project that the home node offers to
  its member nodes.
- **Taker**: the member node (and its agent) that holds a pool item.

## The loop in eight steps

1. Ann's node shares project `shop` with Bob's node, with a pool. Ann's
   repository host (GitHub) gives Bob read access and the right to open pull
   requests: Ann does that on GitHub, not in MaximizePM.
2. Bob clones the repository once and links the folder:
   `maxpm project path shop ~/src/shop`.
3. Bob's agents finish Bob's own work. Bob's `maxpm go` then offers a pool
   item of `shop`, for example `#123@ann`.
4. Bob's agent claims it. Ann's node records the claim and the lease; the
   item leaves the pool.
5. Bob's agent works in its own worktree of Bob's clone, on a branch
   `pool/123-<short title>`, with Bob's model subscription.
6. Bob's agent pushes the branch and opens a pull request to Ann's
   repository, then runs `maxpm done 123@ann --pr <url> --output "..."`.
7. Ann's node adds a review item "Review the pull request of #123 from bob".
   Ann (or an agent of Ann) reviews it, merges, and closes the review item.
   The pool item #123 is then done, and Bob's node gets the credit.
8. When the review asks for changes, #123 goes back to Bob's node first (it
   has the context), and to the pool after a day.

Code never crosses MaximizePM. Git and the repository host carry the code.
MaximizePM carries queue data only: items, claims, messages. No file, no
terminal, no command.

## 1. The task pool

### Which items are in the pool

- Each item of a shared project is **pool** or **local**. A local item never
  leaves the home node's agents and people.
- The project has a default for new items: `maxpm config set pool_default
  pool|local --project shop` (default: local). The home node marks items one
  by one: `maxpm add "..." --pool`, `maxpm edit 123 --pool` or `--local`.
- Never in a pool, whatever the flag: deploy, review, and monitor items;
  items of a person (`--doer human`); items with a `blocked` reason; items
  that touch paths in the project's setting `pool_never_touches` (a pattern,
  for example `deploy/|\.env|secrets`).
- A pool item is in the pool only while it is ready (nothing it waits on is
  open) and nobody holds it. The pool is the "ready" list of the project,
  filtered by the pool flag.
- The home node can limit a pool item to some members:
  `maxpm edit 123 --pool-for bob,carol`.

### How a member node takes from the pool

- A node setting `pool_take` with three values:
  - `off` (the default): the node's agents never take pool items. A person
    can still take one by hand from the Pool view.
  - `ask`: when an agent has no local work, `maxpm serve` tells the node's
    person once "ann's pool has 3 ready items" (needs-you and a
    notification). The person picks one in the Pool view, and MaximizePM
    starts a session for it, as Dispatch does today.
  - `auto`: when an agent has no local work, `maxpm go` gives it a pool
    item.
- A node setting `pool_max` (default 2): the most pool items that the node
  holds at the same time, over all pools.
- **Local work first.** `maxpm go` gives a pool item only when no local item
  is ready for the agent (the role would be IDLE). `maxpm wait` wakes the
  agent when a pool item becomes ready and `pool_take` is `auto`.
- Among pools: the project rank that the member node gives each shared
  project, then the order inside the pool (priority, unblocks, rank, as the
  home node computes it).
- The Pool view on the page: every pool item of every shared project, with
  the home node, priority, model limits, and a Take button (with the agent
  or a new session). `maxpm pool` prints the same list.

### Claims and leases across nodes

- A member agent claims `123@ann`: the request goes to the home node, which
  runs the same claim code as a local claim, with the actor `builder-3@bob`.
  The lease lives on the home node.
- The member node renews the leases of its agents in one request every 5
  minutes, while those agents run commands or are busy (the same signs that
  renew a local lease today).
- A pool lease has its own length, `pool_lease` (default 2h), set by the home
  node per project: long enough for real work, short enough that a dead node
  does not hold an item for a day.
- **When the lease ends, the item goes back to the pool**, with a note "bob's
  lease ended". When Bob's agent comes back later, its `done` gets the
  normal answer for an item it does not hold, and `maxpm check` finds its
  branch.
- The model limits of an item (`--min-model`) come from the home node. The
  member node sends its agent's model with the claim. A pool item that asks
  for opus is not offered to a sonnet session.

### The code: clone, branch, pull request

- The home node records where the code is: `maxpm project repo shop
  https://github.com/ann/shop`. Members see it.
- The home owner gives access on the repository host: a collaborator with
  read and pull request rights, or a fork for a public repository.
  MaximizePM does not manage repository access: the host already does it
  well, and it keeps the code out of the relay.
- A member links its own clone: `maxpm project path shop ~/src/shop`.
  `maxpm go` refuses a pool item when the project has no folder on this
  node, with the command to fix it ("clone https://github.com/ann/shop, then
  run maxpm project path shop <folder>").
- The briefing of a pool item tells the agent: work in a new worktree, on
  the branch `pool/<id>-<words>`, from the home repository's main branch;
  push the branch (or to a fork); open a pull request; never push to main.
- `maxpm done 123@ann` needs `--pr <url>`: the home node refuses a done of a
  pool item without a pull request link.

### Who reviews: the home node

- The member's done does not close the pool item. The home node:
  - sets #123 to "in review" (held, with the pull request link and the
    taker),
  - adds a local review item #124 "Review the pull request of #123 from
    bob" (kind review, never in a pool), which #123 waits on.
- A home agent or person takes #124: reads the pull request, runs the
  project's review steps on the home node, merges.
  - Pass: `maxpm done 124` closes #124, and #123 becomes done with the
    taker's output. The taker's node gets a notice and the credit.
  - Changes needed: `maxpm review fail 124 --note "<what to change>"`. #123
    goes back to the same member node, reserved for it for 24 hours (setting
    `pool_rework_ttl`), with the note. After that it goes back to the pool,
    with the pull request link, so another taker can continue the branch.
  - Rejected: `maxpm drop 124 --note "<why>"`. The reviewer then chooses:
    #123 goes back to the pool for a new start, or #123 is dropped too.
- The author rule holds across nodes: no agent that worked on #123 reviews
  #124.
- Shipping: after the merge, the home node ships #123 with its own deploy
  target, as it ships local work. A member never deploys.

### Whose tokens pay

- The taker's own model subscription pays for the work on a pool item. The
  home node pays for the review.
- `maxpm usage` on the taker's node counts the tokens of the pool item as it
  counts local items. The taker can send the count with its done
  (`pool_report_usage on`, off by default), so the home node sees what a
  pool item cost.

### Credit

- The home node counts, for each member node: pool items done (merged), in
  review, sent back for changes, dropped, and leases that ended.
- All members of the pool see the counts in the Pool view and in
  `maxpm pool stats`. This is a record, not a payment. Money for pool work
  is a later decision (decision 9).

### Trust

- The home node chooses who is in the pool: all members of the shared
  project, or a list (`maxpm share pool shop --members bob,carol`).
- The home node marks items pool or local, and limits single items to some
  members (`--pool-for`).
- The home node can pause the pool (`maxpm share pool shop --pause`) or take
  one member out of it, at any time. Items that the member holds then go
  back to the pool when their leases end, or at once with `--now`.
- The counts above help a home owner decide whom to trust with more.

### What a taker's agent never gets

- No secret of the home node: no setting, no environment variable, no
  credential, no `relay.json`. An item's text must not hold secrets; the
  pool refuses an item whose fields look like a key or a token (a new check
  by pattern, for example `sk_live_`, `ghp_`, `-----BEGIN`).
- No deploy: deploy, review, and monitor items never enter a pool, and a
  member cannot ship.
- No access to the home node's computer: no terminal, no session, no file,
  no canvas, no tmux pane, no hook, no setting.
- No command from the home node runs on the member node by itself: the
  `check` and `touches` fields of a pool item show as text. The member's
  agent reads them as advice and works with the permissions of the member's
  own session.
- The other way, too: text from the home node is text from another person
  to the member's agent. The briefing marks it: "from ann's node: ...".

## 2. Who allows what

### The invite

- The home node's owner runs `maxpm share invite <project> --role <role>`
  (or clicks Share on the project card). MaximizePM prints a code.
- The owner sends the code outside MaximizePM (chat, mail). The code holds a
  random secret. The relay sees only a hash of it.
- The member node's owner runs `maxpm share accept <code>` (or pastes it on
  the page), sees the project, the home node, and the role, and confirms.
- A code works once and ends after 7 days.
- Each side gives the other node a name of its own choice, like a name in an
  address book: `ann`, `acme`, `laptop`. There is no global list of names.

### Roles in one shared project

| Role | The member's people and agents can |
|---|---|
| **read** | See the items, their state, notes, output, dependencies, and who holds what. |
| **pool** | Read; take pool items, work them, and hand them back with a pull request; comment on items. |
| **full** | Pool; also add items, edit them, take local-only items, add dependencies inside the project. |

- The home owner changes the role at any time; it takes effect on the next
  request.
- Settings that are only for the home node (hooks, launch commands, review
  steps that run, deploy targets) never change through a share, whatever
  the role.

### End of a share

Either side can end it (`maxpm share end <share>`).

- **The home node keeps** everything, also the items that the member added,
  with the member's actors in the history (`builder-3@bob`).
- **Items that a member agent holds** go back to the pool (or to open for a
  local item), with a note. The member's agents get a notice and stop.
- **Open pull requests** stay on the repository host. Their review items
  stay on the home node; the reviewer decides.
- **The member node deletes** its copy of the project by default. The home
  owner can allow "member keeps a read-only copy" at invite time.
- The home node cannot force a delete on a computer that it does not
  control. The invite page says so.

## 3. Data model

### One home, one writer

- Every shared project has exactly one home node, and only the home node
  writes the true copy. Two nodes never both decide, so no merge rules are
  needed, and one item never goes to two agents.
- A member sends a **request**: "claim 123", "renew", "done 123 with pull
  request X". The home node runs it with the same core function as a local
  command, with the actor `<agent>@<member>`. All rules apply: dependencies,
  model limits, leases, the close check.
- The home node answers (the result, or the refusal text that a local
  command prints), then sends each changed row to every member.
- What crosses is the **row after the change** (the whole item, a
  dependency, a message), with a number that grows for each change in the
  share. A member that missed changes asks for everything after its last
  number. Applying a row twice does no harm.

### Where a member keeps the copy

- New tables on the member: `peers`, `shares`, `remote_items`,
  `remote_deps`. The member's existing queries (`next`, `claim`, `annotate`)
  never read them by accident; the places that must show shared items read
  them on purpose (go's pool step, the Pool view, `show 123@ann`).

### Item numbers

- A local item stays `#123`. Nothing changes for projects that are not
  shared.
- An item of another node shows as `#123@ann`: its number on its home node,
  then the name that this node gave the peer. Commands take `123@ann`.
- `@` is safe in bash and zsh. (`ann#123` is not: zsh with extended glob
  reads `#` as a pattern.)
- An item that a member adds gets its number from the home node.

### Dependencies across nodes

- Inside one shared project: as today, as a request to the home node.
- My local item waits on an item of a shared project
  (`maxpm dep 55 --on 123@ann`): my node stores the link and reads the state
  of 123 from its copy. Nothing about my item crosses.
- An item of a shared project waits on my local item: refused. The home
  node cannot see my item. The refusal says the fix: add the item to the
  shared project.

### When the home node is offline

- A member still shows the copy, marked "ann is offline since 14:02". The
  pool shows nothing new.
- Claims, adds, edits, and done are refused while the home node is offline,
  so that two members never take the same item.
- Notes and questions wait in the member's outbox and go when the home node
  is back.
- A member agent that holds a pool item keeps working: the work is in its
  own clone, and the pull request goes to the repository host, which is
  online. Its `done` waits in the outbox; when the home node comes back
  before the lease ends, the done counts. Else the home node already put the
  item back in the pool, and the done gets the normal refusal; the pull
  request is still there for the next taker.
- **When a member is offline**, nothing waits on it. The home node sends its
  changes when the member comes back.

### Two edits of one item

- The home node applies requests one at a time. An edit request names the
  change number of the item it saw. When the same field changed since then,
  the home node refuses: "notes of #123 changed by mark at 14:05 since you
  read it; read it again".

## 4. Messages and people across nodes

- Names across nodes: `<name>@<peer>`. `maxpm ask mark@ann "..."`,
  `maxpm note --holder-of 123@ann "..."`. A message lands in the messages
  table of the node where its receiver lives, with `from_agent` like
  `builder-3@bob`.
- A question from a taker's agent about a pool item goes to the home node's
  people by default (the project's owner).
- **Each person's needs-you list stays on their own node**, and their own
  node notifies them with their own channels. A person's item in a shared
  project can name a member's person: `--doer human --for bob@bob`.
- The manager of a node sees the pool items that its agents hold. It manages
  only its own node's agents: `maxpm stop builder-3@bob` is refused; it can
  alert bob's manager.

## 5. Releases and deploy targets

- A deploy target stays with its node. Deploy items, hooks, and credentials
  never cross.
- A pool item ships from the home node after the merge, with the home
  node's target, like local work.
- A member may review a release of the home node only when the home node
  allows it (off by default); the review's `run` steps run only on the home
  node.

## 6. Security

### What the relay sees

- The relay passes each tunnel's pages in memory today and writes none of
  them. Shares use the same rule: the relay forwards a frame from one
  account's River object to the other's and writes nothing that passes.
- D1 keeps for shares: the share's id, the two accounts, the hash of the
  invite code, times, and the state. No project name, no item, no node name.
- When a peer is offline, the relay drops the frame and says so. The sender
  keeps it in its outbox and sends it again later. The relay holds no queue
  of content.
- Code does not pass the relay at all: it goes through the repository host.

### End to end

- **Integrity, from the first slice**: the invite secret never reaches the
  relay. Both nodes keep it, and each frame carries an HMAC-SHA256 of its
  content with a key from that secret (Python's `hmac`). The relay cannot
  forge a claim or a done.
- **Secrecy, later**: the Python standard library has no AES and no
  public-key encryption, and MaximizePM uses the standard library only.
  Options:
  - A. A stream cipher from `hashlib.shake_256` and a keyed `blake2b` MAC,
    from the invite secret. Standard library only, but our own
    construction, which needs an outside review.
  - B. The optional `cryptography` package for nodes that install it.
  - C. TLS inside the WebSocket (`ssl.MemoryBIO`) with certificates that the
    nodes pin at invite time. It needs a key pair per node, which the
    standard library cannot make.
  Until then the relay sees items in memory, as it sees tunnel pages today.
  The invite page says so.

### Audit

- Every request from a peer writes an event on the receiving node, with the
  actor `<name>@<peer>`.
- `maxpm share log <share>` lists the requests: what, who, when, accepted
  or refused.
- The relay's audit keeps "share created" and "share ended" only.

## 7. Plans and price

Today the relay sells one plan per account: $7.99 a month or $79 a year, with
a 7-day trial. The plan opens the tunnel.

- Each node needs a relay account to take part: the relay must know which
  account sends a frame.
- Options for who pays:
  - A. **The home node pays; members join free.** A member account with no
    plan can accept shares and take pool items, but has no tunnel. The home
    plan includes a number of member nodes (for example three); more needs
    the team plan.
  - B. Every node in a share needs its own plan.
  - C. Sharing only in a team plan, priced per person.
- The cost to us is small: a share sends a few frames per change. The model
  tokens are each taker's own.

## 8. Team features in the same frame

A team is a set of nodes of one company that share many projects. The pool
is how a team spreads work: each member's agents finish their own work, then
take from the team's pools.

- **Members**: a team account on the relay. An admin invites people by their
  sign-in (GitHub or Google). Each member's node joins once.
- **Shares by default**: team projects are shared with every member node,
  with the role that the admin sets (read, pool, full). No invite per
  project.
- **An always-on home node**: claims need the home node online. For a team,
  the home of team projects should be a computer that runs all the time (a
  small server or a Mac mini with `maxpm serve` as a service). A home hosted
  by us would put the team's queue on our servers, against the relay's rule
  today.
- **Roles on the team**: owner (billing, members), admin (projects, roles,
  pools, targets), member, viewer.
- **Reviews by another person**: a project setting `review_other_person`:
  the review of a pool item or a release comes from a different person's
  node than the author's. It extends today's rule (an agent that worked on a
  release does not review it) from agents to people.
- **Per-person notifications**: each person's own node notifies them from
  their own needs-you list. This works today; `--for <person>` sends items
  and questions to the right person.
- **Who works on what**: the Board and `maxpm who` show the peers' agents
  that hold items of shared projects, by node. The pool counts show what
  each node finished.

## 9. Slices

### Slice 1 (about one week): the pool loop between two nodes

The loop of mark's request: node A shares a project with a pool; node B
finishes its local work, takes a pool item, works it in its clone, opens a
pull request; node A reviews and closes the item. Between two relay
accounts (two people, or one person with two accounts: the relay allows one
connected computer per account today).

- Relay: a `shares` table in D1; `POST /river/share/invite` and
  `/river/share/accept` with the river token; a `fed` frame that the River
  object forwards to the other account of an active share; a `peer` frame for
  online and offline. Tests: no content of a `fed` frame in D1, in Durable
  Object storage, or in a log line (as the tunnel's test does).
- River, both sides: `maxpm share invite|accept|list|end`; the tables
  `peers`, `shares`, `remote_items`, `remote_deps`; the whole project at
  accept, then each changed row; the HMAC; the outbox.
- River, home side: the pool flag (`--pool`, `pool_default`), the pool rules
  (ready, not held, never deploy/review/monitor or a person's item),
  `maxpm project repo`, claim and renew requests with `pool_lease`, done
  with `--pr` that adds the review item, review pass and fail back to the
  taker.
- River, member side: `pool_take` (off, auto; `ask` in slice 2) and
  `pool_max`; go's pool step after local work; the pool item's briefing
  (worktree, branch, pull request, never main); `maxpm pool`;
  `maxpm show 123@ann`.
- The page: the shared project on Board with the peer and its online state,
  and a Pool list with Take. The Share dialog can wait for slice 2; the
  commands do it.
- Tests: two queue files in one test, joined by a fake relay pipe, run the
  whole loop; the relay Worker's own tests.

### Later slices

2. **Pool, the rest**: `pool_take ask` with the notification, the Pool view
   with filters, `--pool-for`, `pool_never_touches`, pool pause and member
   removal, credit counts and `maxpm pool stats`, the Share dialog.
3. **Comments and people across nodes**: `ask|alert|note <name>@<peer>`,
   `--for` on a person's item, threads across nodes.
4. **Full role**: members add and edit items, take local-only items, edit
   conflicts.
5. **Secrecy end to end** (decision 7).
6. **More than one computer per account**: the relay allows several rivers
   for one account, so one person shares between a laptop and a desktop with
   one sign-in.
7. **Teams**: the team account, members, team projects shared by default,
   roles, the always-on home node guide, `review_other_person`, the price
   per member.

## Decisions for mark

1. **One home node per shared project, and only it decides changes.**
   Members send requests; the home node runs them. The other way: every node
   writes and the nodes merge, with conflict rules for every field and the
   risk that two nodes give one item to two agents.
   Recommendation: one home node.

2. **Code goes through the repository host, not MaximizePM.** The home owner
   gives access on GitHub (collaborator or fork); members clone, push a
   branch, and open a pull request; never a push to main. The other way:
   MaximizePM carries patches through the relay, which puts code on the
   relay and builds a second git.
   Recommendation: the repository host.

3. **A pool item is done only after the home node's review.** The taker's
   done adds a review item on the home node; changes go back to the same
   taker for 24 hours, then to the pool.
   Recommendation: yes, and 24 hours for the rework reservation.

4. **The default of `pool_take` on a member node.** A: off (a person turns it
   on). B: ask. C: auto. And `pool_max` 2.
   Recommendation: A, off. A node owner turns on `auto` when they trust the
   pool; until then, a person takes items by hand.

5. **The length of a pool lease.** 2 hours by default (`pool_lease`, per
   project), against 30 minutes for a local lease.
   Recommendation: 2 hours, renewed by the member node every 5 minutes while
   its agent works.

6. **The roles: read, pool, full.** Pool takes pool items and comments; full
   also adds and edits items and takes local-only items.
   Recommendation: these three.

7. **Secrecy end to end.** Slice 1 has integrity (the relay cannot change a
   claim or a done) but the relay sees items in memory, as it sees tunnel
   pages today; code never passes the relay. Later: A, our own stream cipher
   from the standard library with an outside review; B, the optional
   `cryptography` package; C, TLS inside the WebSocket.
   Recommendation: slice 1 with integrity only, said on the invite page;
   decide A or B before slice 5.

8. **Who pays for sharing.** A: the home node's plan pays and includes three
   member nodes; members join free, with no tunnel. B: every node needs a
   plan. C: sharing only in a team plan, per person.
   Recommendation: A for personal plans, C for teams. A free member is how a
   contractor or a friend tries MaximizePM.

9. **Money for pool work.** Credit in slice 1 is a count only. Later the home
   node could pay a taker per item (through Stripe Connect), or members
   could trade credit.
   Recommendation: counts only, until people use pools and ask for more.

10. **A home node that we host.** For teams whose home node is a laptop. It
    keeps the team's queue on our servers.
    Recommendation: no for now; write a guide for an always-on Mac or server
    with `maxpm serve`, and look again when teams ask.

11. **Slice 1 as above**: the pool loop between two relay accounts, about one
    week, with `pool_take` off and auto (ask in slice 2).
    Recommendation: yes.
