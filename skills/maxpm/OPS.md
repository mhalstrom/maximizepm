# MaximizePM operations: watching a project after its release

MaximizePM watches each project with checks, as it watches the work. One
process serves every project: nothing about a project lives in MaximizePM's
code. A project sets its own checks, settings and system description.

## The parts

- **Check**: one thing to watch, of a project or a deploy target. Its form:
  - `url`: an HTTP GET; healthy when the status is in the list (and the body
    holds the text);
  - `beat`: the project's own job runs `maxpm ops beat`; healthy while a beat
    comes within the period;
  - `cmd`: a shell command; healthy on exit 0;
  - `log`: a command that prints the new log lines; healthy while the matching
    lines in the window stay below the limit;
  - `plugin`: an executable on the Monitoring Plugins API (exit 0 OK,
    1 WARNING, 2 CRITICAL, 3 UNKNOWN).

  `maxpm serve` runs each check every N (`--every`) or at its times (`--cron`),
  and after each release of its target at once. `fail_after` failed runs in a
  row make a fault; `recover_after` passes end it. Before a new fault opens an
  item, the check runs once more at once (`ops_confirm`).
- **Severity**: `item` (an item and an alert to the manager), `note` (a note
  to the manager, no item). MaximizePM rings no phone for a fault: an outside
  service that gets the beat of `ops_deadman_url` can.
- **Fault item**: one operational item for each fault (title
  `ops:<scope>:<check>`), with the evidence. Later runs add notes to it; a
  fault that comes back within a day opens the same item again; more than 3
  checks of one scope that fail within 5 minutes share one item.
- **Kind**: each fault gets a kind of problem by eight fixed rules (`maxpm ops
  kinds`): a broken check, noise, planned, a dependency, capacity, a service
  down, expired, configuration, data, a release regression, a code defect, or
  unknown. The kind decides what happens and what never happens, and whether
  the first step is a person's.
- **Investigation**: a fault item starts a new session at once, ahead of other
  work (at most `ops_max_investigations`, 2, at the same time). The session
  finds the cause and proves it.
- **Repair**: a command set in advance on a check (`--repair`), such as a
  restart. The session that holds the fault item may start it at most
  `ops_repair_max` (2) times in `ops_repair_window` (6h); then only a person.
- **System description**: the facts of a project (its code, hosts, logs,
  dependencies, release and rollback, what is normal, known messages) that the
  kinds and the evidence read. It lives in MaximizePM, never in a repository.
- **Noise rule**: a check, one subject of it, or a pattern that is not a
  problem until the rule ends. A match opens no item and starts no agent.
- **Profile**: every check of a project or a target and its `ops_*` settings
  as one JSON file. Keep it in the project's repository; apply it on another
  computer.

## Who may do what

- Any session adds a `url` or a `beat` check, a noise rule (it ends within
  `ops_noise_max`, 30d; the manager gets a note), and a version of a system
  description (it waits for the accept of a person or the manager).
- A `cmd`, `log` or `plugin` check and a `--repair` need a person or the
  manager, because `maxpm serve` runs them outside the sandbox. A repair on a deploy target needs a person. An agent
  proposes them: `maxpm ops propose ...`; the manager runs `maxpm ops accept`
  or `reject`.
- No session types a production command. A rollback or a change of production
  data is the person's step.

## When you hold a fault item

The item's context holds the kind, the evidence, the evidence pack, and the
steps. In order:

1. An unknown kind: read the evidence, give the kind, and stop:
   `maxpm ops kind <id> <kind> --why "<what shows it>" --message "<the text in the output>"`.
   The message becomes a known message, so the next fault of this kind sorts
   itself.
2. Find the cause. Read the check's runs (`maxpm ops history <scope> <check>`),
   the logs that the system description names, and the recent releases.
3. State the cause with a proof that MaximizePM runs:
   `maxpm ops cause <id> --cause "<what went wrong>" --proof "<check or command>" --action "<what fixes it>"`.
   Fix items of the fault wait until a proof passes.
4. Act: a repair (`maxpm ops repair <scope> <check> --reason "<why it helps>"`),
   a fix item through normal review and release, or a person's step.
5. Close the item with the cause: `maxpm done <id> --output "<cause>"`. When
   the check fails again after the action, the item opens again.

- Not a problem (a bot, a closed connection): `maxpm ops noise add <scope> <check> [--pattern <regex>] --reason "<why nobody acts>"`, then close the item.
- A fact you lacked: `maxpm ops system edit <project> --set <part.field>="..." --why "<the fact, and this fault>"`.
- A recovery adds a note and leaves the item open: you close it.
- While you hold a fault item, MaximizePM refuses ship, target cut, target
  release-now, deploy, and a hook change: the fix goes out as its own item.
- After `ops_fix_max` (2) released fixes did not clear a fault, no more agents
  start for it; the manager decides.

## Setting up checks

- `maxpm ops setup <project>`: a guided start. It asks what to watch, runs each
  new check once before it keeps it, sends a test notice, and sets the outside
  switch.
- `maxpm ops add <project|target> <name> url --url <address> [--severity note]`
- `maxpm ops add <scope> <name> beat --period 1d`; the job then runs `maxpm ops beat`.
- `maxpm ops add <scope> <name> plugin --command "maxpm ops logs --file <path>"`: new log lines against a pattern.
- `maxpm ops probe <name>`: MaximizePM's own checks (idle work, stuck sessions,
  loops, serve, the relay, notices, page speed).
- `maxpm ops deps <project> --add`: a check of each dependency's status page.
- Look: `maxpm ops list`, `show`, `history`, `record` (every detection).
- Change: `maxpm ops edit`, `pause --until <time>` (planned work), `run` (now).
- `maxpm ops profile export <scope>` and `maxpm ops profile apply <file> --yes`.

## Who gets told

- The manager gets an alert for each new fault item and each fault that comes
  back.
- The watchdog (`maxpm serve --install-watchdog`) runs the checks marked
  `--outside on` every minute, also when serve is down. It repairs; the fault
  item stays for the manager.
- `ops_deadman_url`: serve sends a beat to an outside heartbeat service, which
  tells the person when this computer or serve is gone.

## Writing a check script

A `plugin` check is a script that the project owns. MaximizePM keeps the
schedule, the window, the counts and the history; the script only says how
things are now.

- Print one status line first (`OK - ...`, `WARNING - ...`, `CRITICAL - ...`)
  and exit 0, 1 or 2. Exit 3 (UNKNOWN) only when the script could not look:
  that is a broken check, not a fault of the project.
- CRITICAL only for what needs an agent now. Something to look at later is a
  WARNING.
- Report groups, not lines: one line for each kind of error with its count
  (`<group>: CRITICAL 42 timeouts to the payment API`), and the check runs
  with `--subject on`, so each group has its own fault and item.
- Compare with what is normal: a rate against the usual rate of the hour, not
  a fixed count (a `log` check with `--errors ours --normal <lines an hour>`).
- Errors that others cause (4xx answers, bots, scanners) are not ours by
  default. They are ours when our own app sends them, or when the rate for
  real users jumps after a release. Count different sources, not lines
  (`--errors outside`).
- At most 5 sample lines, in one JSON object of evidence after the status
  lines. No secrets and no personal data in them.
- Keep no state and no timing in the script: no files with old counts, no
  sleep. To read only new log lines, use `maxpm ops logs`, which keeps the
  read position. Skip the lines that match `$MAXPM_NOISE` (one pattern a line).
