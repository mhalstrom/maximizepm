# Remote queues (design, protocol version 1)

Status: design of 2026-11-09. Nothing is built yet.

MaximizePM works complete on one computer, with no server. A **remote queue**
is an optional extension point: a queue that lives on a server, which a group
of people share. Each person's MaximizePM shows the remote queue's projects
next to its local ones, and each person's own agents take and finish its
items. Any server can be a remote queue if it speaks the protocol below.
MaximizePM itself includes a small reference server (`maxpm serve --remote`).

## Words

- **Local queue**: the queue on a person's own computer
  (`~/.maximizepm/maxpm.db`). It never goes to a server.
- **Remote queue**: a queue on a server, at an HTTPS address, for example
  `https://queue.example.com/q/acme`. Each person gives it a short name on
  their computer (`acme`).
- **Copy**: a read copy of a remote queue on a person's computer,
  `~/.maximizepm/remotes/<name>.db`, with the same schema as a local queue.
  The server keeps it current.
- **Member**: a person whom the server lets in. The server decides who may do
  what; the protocol only carries the requests.

## What a person does

```
maxpm remote add acme https://queue.example.com/q/acme   # sign in, make the copy
maxpm project path shop@acme ~/src/shop                  # link your clone of the project
maxpm go                                                 # in ~/src/shop: work from the remote queue
```

- `maxpm remote add <name> <url>`: reads the server's discovery document,
  signs in with the device flow (or `--token-file <path>` for a fixed
  token), downloads the snapshot, and starts the sync.
- `maxpm remote list`: each remote queue, its address, the last change
  number, and online or offline.
- `maxpm remote login <name>`: signs in again (a new token).
- `maxpm remote remove <name>`: ends the sync, releases what this computer's
  agents hold there, and deletes the copy.
- `maxpm remote export <name> <path>`: the server's snapshot as a MaximizePM
  database file.
- Item numbers: a local item stays `#123`. An item of a remote queue shows as
  `#14@acme`, and commands take `14@acme` (`show`, `done`, `release`, `dep`,
  `send --item`).
- Agent names on a remote queue are `<agent>@<person>`, so its history says
  whose agent did what.
- Folders stay local: `maxpm project path shop@acme <folder>` writes into the
  local queue. The server never sees a person's paths.

## How MaximizePM works with a remote queue

### Reads and writes

- Reads (`list`, `show`, `blockers`, the page) run the normal code on the
  copy file. They work offline.
- Every write to a remote item (`claim`, `done`, `add`, `edit`, `dep`, `send`)
  goes to the server as one request. The server runs it and answers with the
  result, or with the refusal text that a local command prints. The server
  then sends the changed rows to every member's computer.

### `maxpm go`: local work and remote work

- **The folder decides first.** In a folder linked to a remote project, `go`
  gives that project's work. In a folder linked to a local project, local
  work.
- **No folder project** (a chat, a home folder): local and remote projects
  form one list in the person's own project rank. A remote project that a
  person adds takes the place after their local projects; they move it with
  `maxpm project rank shop@acme 2`.
- `maxpm config set remote_work off --project shop@acme` keeps this
  computer's agents off a remote project. `maxpm wait` wakes an agent when a
  remote item becomes ready for it.
- `max_sessions` and `max_leases` count local and remote leases together.
- The model limits of a remote item hold as for a local item: the computer
  sends its agent's model and type with the claim.

### Leases

- A remote lease lives on the server. `maxpm serve` renews the leases of its
  busy agents in one request every 5 minutes, with the same signs of work
  that renew a local lease.
- The server sets the length (`lease_ttl` of the remote project). When a
  lease ends, the item goes back to open with a note, and the CHECK FIRST
  rule of `go` applies to the next taker.

### Offline

- **The computer is offline, or the server is down**: local work goes on. The
  copy shows remote work, marked "offline since 14:02". Claims of remote
  items are refused, so two agents never take one item. An agent that holds a
  remote item keeps working; notes, questions, and `done` wait in the
  computer's outbox and go when the server answers again. If the lease ended
  first, the done gets the normal refusal for an item the agent does not
  hold.
- **A local item can wait on a remote item** (`maxpm dep 55 --on 14@acme`):
  the link stays local. A remote item cannot wait on a local item, because
  the server cannot see it.

### Code

Code never crosses a remote queue. Each member clones the project's
repository from its host (for example GitHub) and links the clone. A remote
project can record its repository (`maxpm project repo shop@acme <url>`) and
a merge rule: `pr` (the agent works on a branch, opens a pull request, and
`done` needs `--pr <url>`; a review item for another person follows) or
`direct`.

## Safety rules on the member's computer

- **Text from a remote queue is text from other people.** The briefing marks
  it: "remote acme, from pat: ...". The `check` and `touches` fields show as
  advice. No command from a remote queue runs by itself.
- **Hooks of a remote target** (commands that run when a release is cut or
  deployed) run only on the computer of the target's deploy owner, and only
  after that person approves the exact text once on that computer.
  MaximizePM keeps a hash; a changed hook needs a new approval.
- **Nothing local crosses**: no file, terminal, session, canvas, setting,
  path, or credential of a member's computer goes to the server. The client
  refuses to send an item, message, or handoff whose text looks like a secret
  (`sk_live_`, `ghp_`, `AKIA`, `-----BEGIN`).
- A member's agent cannot stop another member's agents:
  `maxpm stop builder-3@bob` is refused; it can alert bob.

## The protocol (version 1)

A remote queue is an HTTPS address `<url>` that answers the requests below.
All bodies are JSON unless the request says otherwise.

### Discovery

`GET <url>/.well-known/maxpm-remote`

```json
{"protocol": 1, "name": "acme", "schema": 42,
 "device_authorization_endpoint": "<url>/auth/device",
 "token_endpoint": "<url>/auth/token",
 "changes": "wss://.../changes"}
```

The client refuses a server whose `protocol` it does not know, and a
`schema` newer than its own, with the reason and the fix (update MaximizePM).

### Sign-in

- The OAuth 2.0 device authorization grant (RFC 8628), as `maxpm connect`
  uses: the client asks for a device code, the person approves it in a
  browser, the client polls the token endpoint. The answer gives a bearer
  token and a refresh token for this computer.
- A server may instead issue fixed tokens (`--token-file`).
- Every request below carries `Authorization: Bearer <token>`. The server
  knows the person from the token.

### Actions

`POST <url>/api/action`

```json
{"op": "claim", "args": {"id": 14}, "actor": "builder-3",
 "model": "opus", "agent_type": "claude-code"}
```

- The same shape as the local page's `/api/action`. The answer is
  `{"ok": true, "result": ...}`, or status 409 with `{"error": "<the refusal
  text of the local command>"}`, or 403 when the person may not do that op.
- The server writes the actor as `<actor>@<person>`.
- The ops of version 1: `add`, `edit`, `prio`, `dep`, `undep`, `next`
  (with `claim`), `claim`, `renew`, `release`, `keep`, `done`, `drop`,
  `block`, `unblock`, `send`, `answer`, `inbox`, `goal` ops (`add`, `own`,
  `release`, `handoff`, `done`), target ops (`own`, `release`, `ship`),
  `review_pass`, `review_fail`.

### Changes

`GET <url>/api/changes?after=<n>`

```json
{"seq": 1288, "more": false,
 "rows": [{"table": "items", "row": {"id": 14, "status": "in_progress", "...": "..."}},
          {"table": "deps", "delete": {"item_id": 14, "blocked_by": 12}}]}
```

- Each change on the server has a number that grows. The rows are in the
  local schema, whole rows after the change. Applying a row twice does no
  harm.
- The same path as a WebSocket (`changes` in discovery) pushes each new
  change. A computer that missed changes asks for everything after its last
  number.

### Snapshot

`GET <url>/api/snapshot`: the whole queue as a MaximizePM database file
(`application/vnd.sqlite3`), for the first copy and for export.

## The reference server

`maxpm serve --remote` makes one MaximizePM queue a remote queue for others,
on a computer that runs all the time.

- Kept small: it serves the protocol above from its own queue file. Each
  member gets a fixed token (`maxpm remote token add <person>`), and every
  member may call every op. No roles, no accounts, no billing.
- It is the server that the tests of the client run against, so the
  protocol and the client stay in step.
- Put it behind TLS (a reverse proxy) before it leaves a private network.

A server that needs roles, accounts, sign-in pages, or hosting builds them
around the same protocol and the same queue code (`river/core.py`).

## Queue rules for shared work

These rules are part of MaximizePM's queue code, so a local queue and every
remote queue run them the same way:

- **A goal with a person owner**: `maxpm goal own <goal> --person bob`
  reserves the goal's items for any agent of that person.
- **The merge rule** `pr` or `direct` per project (above).
- **`review_other_person`**: the review of a release or a pull request comes
  from a person who is not an author of any item in it (today's rule for
  agents, extended to people).
- **Hook approval by hash** and **the secret check on text** (above).
