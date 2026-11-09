# Shared projects between nodes, and team features (design, #4905)

Status: design of 2026-11-09 for mark's review. Nothing is built. The
decisions at the end are in the queue as a person's item; the build items
come after the answers.

## What mark asked

- "we also need to think about features to add team features to maxpm"
- "i am thinking about federate feature for maximizepm"
- "i mean users can allow their nodes to share projects"

## Words in this design

- **Node**: one MaximizePM: one queue file (`~/.maximizepm/maxpm.db`) and the
  `maxpm serve` that runs beside it, on one computer. Today each person has
  one node, and a node knows nothing of other nodes.
- **Share**: one project that one node lets one other node see and work on.
- **Home node**: the node where the shared project lives. Its queue file is
  the true copy of the project.
- **Guest node**: a node that the home node lets in. It keeps a copy of the
  project and sends each change to the home node.
- **Peer**: the other node of a share, seen from one node.

## The idea in five lines

1. A node owner invites another node to one project, with a role.
2. The other node's owner accepts. From then on, the project's items,
   dependencies, claims, and messages show on both nodes. Nothing else of
   either node crosses.
3. The home node decides every change. A guest sends a request; the home
   node runs it through the same code as a local command and sends the
   result to every guest.
4. The relay carries the traffic over the WebSocket that each `maxpm serve`
   holds already. No node opens a port, and the relay keeps nothing that
   passes.
5. Code never crosses. Each node works in its own copy of the repository
   (its own git clone). A share moves queue data only: no file, no terminal,
   no command.

## 1. Who allows what

### The invite

- The home node's owner runs `maxpm share invite <project> --role <role>`
  (or clicks Share on the project's card on the page). MaximizePM prints a
  code, for example `share-7k3q-9xwm-...`.
- The owner sends the code to the other person outside MaximizePM: chat,
  mail, in person. The code holds a random secret. The relay sees only a
  hash of it (below, "Security").
- The other node's owner runs `maxpm share accept <code>` (or pastes it on
  the page). Their node shows what the share gives: the project's name, the
  home node's name, the role. They confirm.
- A code works once and ends after 7 days. The home owner can cancel an
  unused code.
- Each side gives the other node a name of its own choice (like a name in an
  address book): `ann`, `acme`, `laptop`. There is no global list of names.

### Roles for one share

| Role | The guest's people and agents can |
|---|---|
| **read** | See the items, their state, notes, output, dependencies, and who holds what. |
| **comment** | Read, and send notes and questions about items to the home node's people and agents. |
| **full** | Comment, and add items, edit them, claim them, finish them, add dependencies inside the project, ship to the home node's release. |

- The home owner changes the role at any time
  (`maxpm share role <share> full`). The change takes effect on the next
  request.
- The guest owner also decides for their side: whether their agents may take
  items of the share (`maxpm share agents <share> on|off`; off by default).
  A person on the guest node can work on items with agents off.
- Settings that are only for a person (hooks, launch commands, review
  steps that run, deploy targets) never change through a share, whatever
  the role.

### Revoke

Either side can end a share at any time (`maxpm share end <share>`).

- **The home node keeps** everything: every item, also the ones that the
  guest added, with the names of the guest's people and agents in the
  history (`builder-3@bob`).
- **Items that a guest agent holds** go back to open on the home node, with
  a note "the share with bob ended". The guest's agents get a notice and
  stop work on them.
- **The guest node deletes** its copy of the project by default. It keeps
  its own messages and a one-line record of the share. Option: the home
  owner allows "guest keeps a read-only copy" at invite time, for a client
  who must keep a record.
- **What MaximizePM cannot promise**: the home node cannot force a delete on
  a computer that it does not control. A person could copy the data before
  the end. The design says so on the invite page.

## 2. Data model

### One home, one writer

- Every shared project has exactly one home node. Only the home node writes
  the true copy. This avoids merges: two nodes never both decide.
- A guest sends a **request**: "claim 123", "add item X", "done 123 with
  output Y". The home node runs it with the same core function as a local
  command (`claim`, `item_add`, `done`), with the actor `<agent>@<guest>`.
  The same rules apply: dependencies, model limits, max leases, the close
  check, review rules.
- The home node answers the request (the result, or the refusal text that a
  local command prints), then sends each changed row to every guest of the
  project.
- What crosses is the **row after the change** (the whole item, the
  dependency, the message), with a number that grows for each change in the
  share. A guest that misses changes asks for everything after the last
  number it has. Rows are easy to apply twice without harm.

### Where a guest keeps the copy

- New tables on the guest: `peers`, `shares`, `remote_items`,
  `remote_deps`, `remote_goals`. The guest's existing queries (`next`,
  `claim`, `annotate`, `sync_needs_you`) never read them by accident. Each
  place that must show shared items reads them on purpose.
- The guest links its own folder to the shared project
  (`maxpm project path`), as today. The path never crosses.

### Item numbers

- A local item stays `#123`. Nothing changes for projects that are not
  shared.
- An item of another node shows as `#123@ann`: the number on its home node,
  then the name that this node gave the peer. Commands take `123@ann`:
  `maxpm show 123@ann`, `maxpm claim 123@ann`.
- `@` is safe in bash and zsh. (`ann#123` is not: zsh with extended glob
  reads `#` as a pattern.)
- On the home node, the same item is plain `#123`. Its history names the
  guest's actors as `builder-3@bob`.
- An item that a guest adds to a shared project gets its number from the
  home node.

### Dependencies across nodes

- Inside one shared project: as today. `maxpm dep 123@ann --on 130@ann`
  goes to the home node as a request.
- My local item waits on an item of a shared project:
  `maxpm dep 55 --on 123@ann`. My node stores the link and reads the state
  of 123 from its copy. My item becomes ready when the copy says done.
  Nothing about my item 55 crosses.
- An item of a shared project waits on my local item: refused. The home
  node cannot see my local item, so it cannot tell when it is done. The
  refusal says the fix: move the item into the shared project, or add an
  item there.
- Between two shared projects with different home nodes (I am guest of
  ann's project and host of my own shared project, and ann is my guest
  too): allowed, because each home node can see the other one's copy.

### Claims and leases

- A guest agent claims `123@ann`: the request goes to the home node, which
  runs `_claim_row` with the actor `builder-3@bob`. The lease lives on the
  home node.
- The guest node renews the leases of its agents in one request every 5
  minutes (from `maxpm serve`), while those agents run commands or are busy
  on the guest (the same signs that renew a local lease today).
- `max_leases` and `goal_max_leases` of an agent count on its own node, with
  its remote leases. The home node also counts them for the project's own
  rules.
- The model limits of an item (`--min-model`) come from the home node. The
  guest sends the model of its agent with the claim.
- A guest's `maxpm go` offers ready items of shared projects linked to its
  folder, sorted with local items by the same order (priority, project rank,
  unblocks). Only with role full and agents on.

### When the home node is offline

- A guest still shows the copy, marked "ann is offline since 14:02".
- Claims, adds, edits, and done are refused while the home node is offline:
  "ann's node is offline; the change waits for it" is worse than a clear no,
  because two guests could both "claim" offline.
- Notes and questions wait in the guest's outbox and go when the home node
  is back. They change no state, so the order does not matter.
- A guest agent that holds a lease keeps working. The home node keeps the
  lease until it ends (lease_ttl, 30 minutes, or goal_lease). When the link
  comes back within that time, the renew continues. When the lease ended
  meanwhile, the home node released the item. The agent's `done` then gets
  the normal answer for an item it does not hold, and `maxpm check` finds
  its commits.
- **When a guest is offline**, nothing waits on it. The home node sends its
  changes when the guest comes back.

### Two edits of one item

- The home node applies requests one at a time, in order. There is no merge.
- An edit request names the change number of the item it saw. When the same
  field changed since then, the home node refuses: "notes of #123 changed by
  mark at 14:05 since you read it; read it again". Claims already work this
  way (a second claim is refused).
- An item's status uses the existing rules: a done of an item that someone
  else holds is refused, as today.

## 3. Messages and people across nodes

- Names across nodes: `<name>@<peer>`. `maxpm ask mark@ann "..."`,
  `maxpm alert builder-3@bob "..." --item 123@ann`. A message about an item
  of a shared project goes to the home node; a message to a name on another
  node goes to that node.
- `--holder-of 123@ann` reaches whoever holds the item, on whichever node.
- A message lands in the messages table of the node where its receiver
  lives, with `from_agent` like `builder-3@bob`. Answers go back the same
  way. Threads keep one id per node and a link between the two.
- **Each person's needs-you list stays on their own node**, and their node
  notifies them through their own channels (phone, mail, Mac). A person's
  item in a shared project names the person: `--doer human --for bob@bob`.
  Without `--for`, a person's item goes only to the home node's people.
- The page's "You are" list shows only the people of this node. The
  people of a peer show in the share's panel, with their node.
- The manager of a node sees the shared projects that its node takes part
  in, and the guest agents that hold items there. It manages only its own
  node's agents: `maxpm stop builder-3@bob` is refused; the manager can
  alert bob's manager.

## 4. Releases and deploy targets

- A deploy target stays with its node. The deploy item, hooks, review, and
  credentials never cross.
- A guest's `done 123@ann --ship` asks the home node to ship the item. It
  joins the next deploy item of the project's target on the home node, with
  the same rules (cut, cadence, release-now by the owner or manager).
- The guest's code must reach the home node's repository first: a push to a
  branch, a pull request, a commit on a shared remote. That is git's job,
  not MaximizePM's. The item's output names the commit or the pull request.
  A review step of the home node checks that it is merged.
- A guest may review a release of the home node when the home node allows it
  (a share setting, off by default). The rule that an author may not review
  holds across nodes: an agent of bob that finished an item in the release
  cannot review it.
- The review's `run` steps and `review_cmd` run only on the home node. A
  guest reviewer confirms written steps only.

## 5. Security

### What the relay sees

- The relay already passes each tunnel's pages in memory and writes none of
  them. Shares use the same rule: the relay forwards a frame from one
  account's River object to the other's, and writes nothing that passes.
- What the relay keeps for shares in D1: the share's id, the two accounts,
  the hash of the invite code, times, the state (active, ended). No project
  name, no item, no node name.
- When a peer is offline, the relay drops the frame and says so. The sender
  keeps the change in its outbox and sends it again later. The relay never
  holds a queue of content.

### End to end

- **Integrity, from the first slice**: the invite code's secret never
  reaches the relay. Both nodes keep it, and each frame carries an HMAC-SHA256
  of its content with a key from that secret (Python's `hmac`, standard
  library). The relay cannot forge or change a request, even though it
  carries it.
- **Secrecy, later**: the Python standard library has no AES and no
  public-key encryption, and MaximizePM uses the standard library only. Three
  ways:
  - A. A stream cipher built from `hashlib.shake_256` and a keyed
    `blake2b` MAC, from the invite secret. Standard library only, but it is
    our own construction, and it needs an outside review before anyone
    depends on it.
  - B. The optional `cryptography` package for nodes that install it, and
    TLS-level secrecy only for the rest.
  - C. TLS inside the WebSocket: `ssl.MemoryBIO` runs a TLS session over any
    channel, with certificates that the two nodes pin at invite time. It
    needs a key pair per node, and the standard library cannot make one
    (macOS has the `openssl` command; Windows does not have it).
  Until then, the relay sees shared items in memory the same way it sees
  the pages of a tunnel today. The invite page says so.

### What another node may do on my node

- Read and change items of the projects that I shared, within the role.
- Send messages to my people and agents.
- Nothing else: no file, no folder, no terminal, no session, no canvas
  outside the shared project's items, no setting, no other project.
- **No command from another node runs on my node.** Some item fields are
  commands or paths: `check`, `touches`, review `run` steps, hooks, launch
  profiles. A guest's copy shows them as text and never runs them. An item
  that a guest adds keeps its `check` as a suggestion; the home node runs a
  `check` command only after a home person confirms it once.
- **Text from another node is text from a stranger to an agent.** An item's
  notes and context are instructions that agents read. The go briefing of a
  guest agent marks text from the home node: "from ann's node: ...". Each
  guest agent works in its own folder with its own permissions; the share
  does not widen them.

### Audit

- Every request from a peer writes an event on the receiving node, as every
  change does today, with the actor `<name>@<peer>`.
- `maxpm share log <share>` lists the requests of the share: what, who,
  when, accepted or refused.
- The relay's audit keeps only "share created", "share ended", with the
  account id and time, as it does for its other events.

## 6. Plans and price

Today the relay sells one plan per account: $7.99 a month or $79 a year, with
a 7-day trial. The plan opens the tunnel.

- Each node needs a relay account to take part in a share: the relay must
  know which account sends a frame.
- Options for who pays:
  - A. **The home node pays; guests share for free.** A guest account with no
    plan can accept shares, but has no tunnel. A client or contractor joins
    without a card. The home node's plan includes a number of guest nodes
    (for example three), and more needs the team plan.
  - B. Every node in a share needs its own plan.
  - C. Sharing is only in a team plan, priced per person.
- The cost to us is small: a share sends a few frames for each change, far
  less than a tunnel page. Durable Objects that sleep cost nothing.

## 7. Team features in the same frame

A team is a set of nodes that belong to one company and share many projects.
The share model above carries it; a team adds these parts:

- **Members**: a team on the relay (a company account with members). An admin
  invites people by their sign-in (GitHub or Google). Each member's node joins
  the team once.
- **Shares by default**: a team project is shared with every member node,
  with the role that the admin sets per member (read, comment, full). No
  invite per project.
- **An always-on home node**: a project's home node must be online for
  claims. A laptop is not always on. For a team, the home of team projects
  should be a node that runs all the time: a small server or a Mac mini that
  runs `maxpm serve` (an install guide and a `maxpm serve --headless`
  service). A home hosted by us is a later choice: it would put the team's
  data on our servers, against the relay's rule.
- **Roles**: owner (billing, members), admin (projects, roles, targets),
  member (full on team projects), viewer (read and comment).
- **Reviews by another person**: a project setting `review_other_person`:
  the release review must come from an agent or person of a different person
  than the authors of the release. This extends the existing rule (an agent
  that worked on the release does not review it) from agents to people.
- **Per-person notifications**: each person's own node notifies them, from
  their own needs-you list, with their own channels. This works today; the
  team adds `--for <person>` on items and questions so that the right person
  gets them.
- **Who works on what**: the page's Board and `maxpm who` show the agents of
  the peers that hold items of shared projects, by node.
- **Price**: per member and month (option C above), with the personal plan
  as the base.

## 8. Slices

### Slice 1 (about one week): share one project, read and comment

Between two relay accounts (two people, or one person with two accounts:
the relay allows one connected computer per account today).

- Relay: a `shares` table in D1; `POST /river/share/invite` and
  `/river/share/accept` (with the river token); a `fed` frame that the River
  object forwards to the River object of the other account of an active
  share; a `peer` frame for online and offline. Tests: no content of a `fed`
  frame in D1, in Durable Object storage, or in a log line (the same test as
  for the tunnel).
- River: `maxpm share invite|accept|list|end|log`; the tables `peers`,
  `shares`, `remote_items`, `remote_deps`; the home node sends the whole
  project at accept, then each changed row; the HMAC on each frame; the
  outbox for notes.
- Roles read and comment only. `maxpm show 123@ann`, `maxpm list --project
  <shared>`, and `maxpm send note|question --item 123@ann` to the home
  node's holder or people.
- The page: the shared project on Board and Plan with the peer's name and its
  online state, and a Share dialog on the project card.
- Tests: two queue files in one test, joined by a fake relay pipe; the relay
  Worker's own tests.

### Later slices

2. **Full role**: add, edit, claim, done, dependencies inside the project,
   leases and renew, the guest's `maxpm go`, agents on or off, refusals while
   the home node is offline, edit conflicts.
3. **People across nodes**: `ask|alert|note <name>@<peer>`, `--for` on
   person's items, needs-you and notifications on the person's own node,
   threads across nodes.
4. **Releases**: `--ship` from a guest, guest reviewers, the cross-node
   author rule.
5. **Secrecy end to end** (the choice in decision 6).
6. **More than one computer per account**: the relay allows several rivers
   for one account, each a node with its own name, so that a person shares
   between their laptop and desktop with one sign-in.
7. **Teams**: the team account on the relay, members, team projects shared by
   default, roles, the always-on home node guide, `review_other_person`, the
   per-member price.

## Decisions for mark

1. **One home node per shared project, and only the home node decides
   changes.** Guests send requests; the home node runs them. The other way
   is that every node writes and the nodes merge, which needs conflict rules
   for every field and lets two nodes give one item to two agents.
   Recommendation: yes, one home node.

2. **What happens on a guest while the home node is offline.** A: read-only,
   with notes and questions sent later (recommended: no double claims). B:
   guests also queue claims and edits, and the home node applies or refuses
   them when it comes back.
   Recommendation: A.

3. **Roles for a share.** Three roles: read, comment, full; and on the guest
   side, agents on or off (off by default).
   Recommendation: yes, these three plus the agent switch.

4. **What the guest keeps after the share ends.** A: the guest deletes its
   copy (default), and the home owner can allow a read-only copy at invite
   time. B: the guest always keeps a read-only copy. C: the guest always
   deletes it.
   Recommendation: A.

5. **Item numbers of another node.** `#123@ann`, with `ann` the name that
   this node gave the peer.
   Recommendation: yes; no global names on the relay.

6. **Secrecy end to end.** Slice 1 has integrity (the relay cannot change a
   request) but the relay sees the content in memory, as it sees tunnel
   pages today. Later: A, our own stream cipher from the standard library
   with an outside review; B, the optional `cryptography` package; C, TLS
   inside the WebSocket with pinned certificates.
   Recommendation: ship slice 1 with integrity only and say so on the invite
   page; decide between A and B before slice 5. B is safer, but it breaks the
   "standard library only" rule for the nodes that use it.

7. **Who pays for sharing.** A: the home node's plan pays and includes three
   guest nodes; guests join free with no tunnel. B: every node needs a plan.
   C: sharing only in a team plan, per person.
   Recommendation: A for personal plans, C for teams. A free guest is how a
   client or contractor tries MaximizePM.

8. **The price of a team plan per member and month.** For example the
   personal price ($7.99), or a higher price for the admin and review
   features.
   Recommendation: decide at slice 7; it does not change slices 1 to 6.

9. **A home node that we host.** For teams whose home node is a laptop. It
   would keep the team's data on our servers, against the relay's rule
   today.
   Recommendation: no for now. Write a guide for an always-on Mac or server
   that runs `maxpm serve`, and look again when teams ask for it.

10. **Slice 1 as above**: read and comment, two relay accounts, through the
    relay, about one week.
    Recommendation: yes. Then build slices 2 and 3 before releases and teams,
    because full work on another node's items is what makes sharing useful.
