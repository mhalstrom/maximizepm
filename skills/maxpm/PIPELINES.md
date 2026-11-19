# MaximizePM pipelines: following each release left to right

A pipeline is the release process of one project: its stages in order, from
the first build to production. MaximizePM keeps the order, the transitions
between the stages and the record of each run. The project's own commands, or its
forge's workflows, do the work. The person follows every release on the
Deploy tab of the page.

## The parts

- **Pipeline**: one release process, with a name, and the project it releases.
- **Stage**: one step of the pipeline, in its place (1 is the first). Its
  kind: `build`, `test`, `review`, `approve`, `deploy`, `check` or `other`.
  It does its work by a command in the project folder (`--run`) or by a
  workflow on a forge (`--workflow`), never both.
- **Environment**: a place that runs the code (dev, beta, prod, one server).
  A deploy stage names the environment it deploys to. When the stage passes,
  that version runs there.
- **Transition**: the arrow into a stage from the stage before it. The tests
  and checks run inside the stages; a transition only decides when a version
  may enter. Its controls are in the section "Transitions" below.
- **Version**: one commit that goes through the stages. A short and a full
  commit id are one version.
- **Stage run**: one run of a stage for one version: waiting at its
  transition, running, passed, failed, stopped. Its steps, its test results and its last
  lines belong to it.
- **Source**: where the runs come from. With no forge, `maxpm serve` runs
  each stage's command on this computer. With a forge (GitHub, GitLab, Gitea,
  Forgejo, Pulumi Delivery), `maxpm serve` reads the forge's runs once a
  minute with a token, and starts a stage's workflow when its transition lets
  the version in.
- **Default pipeline**: each deploy target that no pipeline names shows as a
  pipeline of its own, computed from its releases: cut, build (the
  release-cut hook), review, approval, deploy, watch. It needs no setup.

## Set up a pipeline for a project

Only a person or the manager defines a pipeline. A worker asks the manager.

1. Add the pipeline and its environments:
   ```
   maxpm pipeline add <name> --project <project>
   maxpm pipeline env <name> beta
   maxpm pipeline env <name> prod [--target <deploy target>]
   ```
2. Add the stages in order. Each one names its kind and its work:
   ```
   maxpm pipeline stage <name> build --kind build --run "make build"
   maxpm pipeline stage <name> test --kind test --run "make test" --junit report.xml
   maxpm pipeline stage <name> beta --kind deploy --env beta --run "make deploy-beta"
   maxpm pipeline stage <name> prod --kind deploy --env prod --run "make deploy" --mode "yes mark"
   ```
   `--pos <n>` moves a stage; `--remove` removes it (its runs stay). A stage
   with nothing to change is refused, so a typo does not look like a change.
3. Set the transition into each stage that needs more than `auto` (see
   "Transitions"): `maxpm pipeline transition <name> prod --window "mon-thu
   09:00-17:00 America/Los_Angeles" --fault-rule on --reason "<why>"`.
4. Look at it: `maxpm pipeline show <name>`, and `maxpm pipeline history
   <name>` for each change with who made it. Keep it in git with `maxpm
   pipeline export <name> --file <path>` (JSON).
5. Send a version in: `maxpm pipeline start <name> [--rev <commit>]` (the
   project folder's HEAD when no commit is named). `maxpm serve` runs each
   stage when its transition lets the version in.

## Write a stage's script

A stage's command is a normal script of the project. MaximizePM reads only
its exit code, its output and its test report.

- **Where it runs**: in the project folder, in a process group of its own.
- **What it gets**: `MAXPM_PIPELINE`, `MAXPM_STAGE`, `MAXPM_REV` (the commit),
  `MAXPM_RUN` (the run's id) and `MAXPM_ENV` (the environment of a deploy
  stage). A run of the failed tests also gets `MAXPM_TESTS`: one
  `suite.name` on each line.
- **The result**: exit 0 passes the stage; any other exit fails it. A stage
  that runs longer than `stage_timeout` (2h) is stopped and fails.
- **Its steps**: print a line at the start and the end of each part, so the
  page shows the parts and their times:
  ```
  ::step start <name> [kind=build|test|deploy|check|wait] [where=<host>] [parallel]
  ::step end <name> passed|failed|skipped [where=<host>] [9m02s] [<note>]
  ```
  An end line with no start makes a step from its time taken. An end line
  with no state word means passed. A step is its name plus its host:
  `deploy where=edge-1` and `deploy where=cell-1` are two steps. Every other
  line goes to the run's last lines.
- **Its tests**: a test stage writes a JUnit XML file and names it
  (`--junit report.xml`, a path in the project folder). After the command,
  MaximizePM reads it. A missing file, a file older than the run, or a failed
  test fails the stage, also when the command exits 0. The page lists each
  test, failed first, with a prompt that analyzes the failures.
- **Many servers**: `--waves "web-1; web-2, web-3"` (`;` between waves) runs
  `--run` once for each server with `MAXPM_HOST` set; the command does the
  ssh itself (`ssh "$MAXPM_HOST" ...`). `--check "<cmd>"` checks each server
  after its deploy, and `--wave-wait 10m` is how long a wave's checks must
  hold before the next wave. A failed deploy or check stops the next waves and
  runs `--rollback "<cmd>"` on the servers of that wave (`MAXPM_PREV_REV` is
  the version before).

Test the script by hand in the project folder before you name it in a stage.

## Run the stages on a forge

The work can run on the project's forge instead of this computer:

```
maxpm pipeline forge <name> github --repo <owner/name> --secret <secret name>
maxpm pipeline forge <name> gitlab --repo <group/project> --secret <secret name> [--url <address>]
maxpm pipeline forge <name> gitea|forgejo --repo <owner/name> --secret <secret name> --url <address>
maxpm pipeline forge <name> pulumi --repo <org/project/stack> --secret <secret name>
```

Then each stage names its forge work with `--workflow`: on GitHub, Gitea and
Forgejo the workflow file (`tests.yml`); on GitLab a GitLab stage; on Pulumi
Delivery a Delivery stage. The person saves the token first (`maxpm secret put
<secret name>`); a session never types or prints a token.

- GitHub, Gitea, Forgejo: MaximizePM starts a stage's workflow with
  `workflow_dispatch` and the inputs `maxpm_run` and `maxpm_rev`; the
  workflow names its run `maxpm run <id>` (run-name), so the run links to its
  version. A test job uploads its JUnit file as an artifact named `junit*`.
- GitLab: when the transition lets a version in, the stage plays its manual
  jobs; the pipeline's test report and the environments' deployments are read
  too.
- Pulumi Delivery: the yes of the transition continues the Delivery gate. A
  roll back is done on Pulumi's page.
- A stage that the forge starts by itself (a push, a tag) has the mode
  `forge`: MaximizePM only reads its run.

### Accounts and tokens

Each token is an account: one token on one forge server. Several pipelines
can name the same token, such as `github-mhalstrom` for every pipeline of
that GitHub account. Use one token for each account, and save it once.

`maxpm serve` asks the forge about each token once an hour: the account it
acts as, when it runs out, and which of the pipelines' repositories it
reaches. GitHub sends the end date of a fine-grained token, GitLab of each
access token; Gitea, Forgejo and Pulumi Cloud send none.

```
maxpm pipeline accounts                         # each token: account, state, end date, repositories, pipelines
maxpm pipeline accounts --check [--secret <name>]   # ask the forges now (a person or the manager)
```

The Deploy tab shows the same: Deploy > Accounts lists the tokens, and the
head of each pipeline names its forge, account, token and last read.

When the forge refuses a token (401 or 403), or nobody saved it:

- the pipeline's head turns red, a banner on its page quotes the forge's own
  words, and its forge stages show "no news" instead of a state that may be
  old;
- the Deploy tab counts the token once in its red number;
- the manager gets one alert. It makes a new token and saves it under the
  same name (`maxpm secret put <name>`), and asks the person only for the
  forge's sign-in. The next check finds that it works.

Seven days before a token runs out, the manager gets one alert to make a new
one.

## Connect another system with a plugin

Any other release system (Jenkins, Argo CD, a cron job, a deploy script)
connects through a plugin. `maxpm pipeline plugins` lists the installed
ones, with the actions each one does. Connect a pipeline to one:

```
maxpm pipeline forge <name> <plugin> --set <key>=<value> --secret <its secret>=<maxpm secret>
maxpm pipeline plugin <plugin>        # its settings, its secrets, and a check that it answers
```

Each stage then names the work in that system with `--workflow`. When a
stage's gate opens, MaximizePM asks the plugin to start that work. The page
shows an action that the plugin does not do (run again, roll back, stop) as
"not available for this source", and the command for it is refused: do that
step in the system itself.

MaximizePM ships two plugins. Read their code before you write a plugin:

- `github` (`river/pipeline/plugins/github/`): the GitHub, Gitea and
  Forgejo source above, the full example. `maxpm pipeline forge <name>
  github|gitea|forgejo` uses it.
- `jsonl` (`river/pipeline/plugins/jsonl/`): the short example. It follows a
  file of JSON lines that another program writes, one line for each change of
  a run, and it writes start and stop requests to a second file.

### The github plugin

- **What it reads**, once a minute: the newest 10 runs of each stage's
  workflow, their jobs (the stage's steps), the JUnit artifacts of a finished
  run (named `junit*`, or the stage's `--junit` name), and, on GitHub, each
  environment's deployments with a success status.
- **What it does**: start, run again and roll back are `workflow_dispatch` of
  the stage's workflow on the branch (`--ref`, `main` when not set) with the
  inputs `maxpm_run` and `maxpm_rev` (the older commit of a roll back); stop
  cancels the run.
- **What it cannot do, and why**: a run that a push or a tag starts carries no
  MaximizePM id, so the plugin links it to the version that waits at that
  stage (the same commit). Gitea and Forgejo have no deployments API: a deploy
  stage that passes records its version. Some Gitea and Forgejo versions have
  no jobs, artifacts or cancel call: the read goes on without them, and stop
  says to stop the run on the forge's page.
- **Its token**: a fine-grained token with Actions read and write,
  Deployments read and Contents read on the repository (on Gitea and Forgejo
  a token with the repository scope), saved as a maxpm secret.
- **Its test**: the plugin's `handle(request, fetch)` takes the HTTP function,
  so a test passes recorded API answers instead of the network
  (`tests/test_pipeline.py`, classes GitHub and GiteaForgejo). Test your own
  plugin the same way.

## Write a plugin

A plugin is a folder with two parts: a manifest, `plugin.json`, and a program
in any language. It imports nothing of MaximizePM's. Put the folder in
`plugins/<name>/` beside the queue's database (`maxpm db` shows that folder;
usually `~/.maximizepm/plugins/<name>/`). The folder's name is the plugin's
name.

### The manifest

```json
{
  "contract": 1,
  "name": "jenkins",
  "version": "1.0.0",
  "level": "act",
  "description": "Reads the builds of a Jenkins job; starts and stops them.",
  "run": ["{python}", "plugin.py"],
  "actions": ["start", "stop"],
  "settings": {"url": {"required": true, "help": "https://jenkins.example.com"},
               "job": {"required": true, "help": "the job's name"}},
  "secrets": {"token": {"help": "a Jenkins API token"}},
  "poll_every": 60,
  "timeout": 30
}
```

- `contract`: 1, the version of this contract.
- `name`: lower case letters, digits and `-`; the same as the folder. The
  names of the built-in forges (`github`, `gitlab`, `gitea`, `forgejo`,
  `pulumi`) and `none`, `maxpm`, `here`, `plugin` are taken.
- `level`: `report` (it reads only; no actions), `act` (it reads, and it does
  the actions it lists), or `gate` (as `act`; the outside flow will also ask
  MaximizePM's gates before it moves on; that call is not ready yet, so a gate
  plugin works as `act` now).
- `run`: the command as a list of words, run in the plugin's folder.
  `{python}` is the Python that runs MaximizePM.
- `actions`: some of `start`, `stop`, `rerun`, `rollback`.
- `settings` and `secrets`: each name with its `help`. A setting can be
  `required` and have a `default`. A secret is required unless it says
  `"required": false`.
- `poll_every`: seconds between two polls (60 when not given; at least 10).
- `timeout`: seconds for one call (30 when not given; at most 300).

### The calls

For each call, `maxpm serve` starts the program, writes one JSON object (the
request) on its stdin, and reads one JSON object (the answer) from its
stdout. Every request has `contract` and `call`. The calls:

- `describe`: answer with `name`, `version`, `level` and `actions`. They must
  match the manifest. `maxpm pipeline forge` and `maxpm pipeline plugin` call
  it.
- `poll`: read what changed in the system since the last poll, and answer with
  `events` (a list of CDEvents objects, below) and `state`. It can also answer
  `reports`: the JUnit XML files of finished runs, each as `{"run_key": <the
  system's id of the run>, "xml": <the file's text>}`; MaximizePM reads them
  with its own parser, once for each run, before it hands the version on. And
  `counts`: numbers for the person, such as `{"runs": 10}`.
- `start`, `rerun`, `rollback`: start the work of one stage run. `rerun` runs
  a stage again; `rollback` puts an older version on an environment. The
  answer can have `key` (the system's id of the run), `url` and `note`.
- `stop`: stop the run. The run ends when a later poll reports its end.

The request of `poll` and of each action also has:

- `pipeline`: its `name`, `project`, `stages` (each with `name`, `kind`,
  `pos`, `work` (the stage's `--workflow`), `env`, `transition` (the mode of
  the arrow into it), `junit` (the stage's `--junit` name)) and `envs` (each
  with `name`, `target`, and the `version` that runs there).
- `settings`: the pipeline's settings for the plugin.
- `secrets`: the value of each secret the manifest names.
- `state`: the `state` of the plugin's last answer (null at first). Keep a
  cursor here, such as the last build number read; the plugin keeps no file
  of its own.
- `poll` only: `runs`, the stage runs that wait or run (`id`, `stage`,
  `work`, `version`, `key`, `state`, `purpose`).
- an action only: `run`, the stage run it acts on (the same fields). A
  `rerun` of failed tests also has `tests`: one `suite.name` for each test.

The answer can always have `error` (a message). Exit 0 when the call worked.
On an error, exit 1 or more and say why in `error` or on stderr. Do not print
anything else on stdout.

### The events

A `poll` (and an action) answers with events in the CDEvents form, the same
form that MaximizePM keeps for every source:

```json
{"context": {"version": "0.4.1", "id": "build-482-finished", "source": "jenkins",
             "type": "dev.cdevents.pipelinerun.finished.0.2.0", "timestamp": "2026-11-18T10:04:00Z"},
 "subject": {"id": "build-482", "type": "pipelineRun",
             "content": {"pipelineName": "build", "outcome": "success", "url": "https://..."}},
 "customData": {"maxpm": {"stage": "build", "version": "3f2a9c1", "run": 17}}}
```

- A stage run: `pipelinerun.queued`, `.started`, `.finished` (`outcome`
  success, failure, cancel; `errors` for the reason). The subject id is the
  system's id of the run. A step: `taskrun.started` and `.finished`, with
  `content.taskName`, and `customData.maxpm.run_key` set to the run's subject
  id. A test: `testcaserun.finished` with `content.testCase.name` and
  `content.outcome` (success, failure, error, skip), and
  `customData.maxpm.run_key` and `suite`. A version that now runs in an
  environment: `service.deployed` with `content.environment.id` (the
  environment's name).
- `customData.maxpm.stage` names the stage; `version` names the commit.
- `customData.maxpm.run`: the id of a MaximizePM stage run (from `runs` of
  the request, or the `run` of a start). It links the system's run to the run
  that waits at MaximizePM's gate. A system that starts its runs itself
  (a push, a timer) links them this way.
- Give each event an id that is the same each time you read the same change:
  MaximizePM keeps each event once for each pipeline, so a change read twice
  changes nothing. MaximizePM sets the event's source to the plugin's name.
- A run that ends passed hands its version to the next stage, as a run of a
  forge does.

### What MaximizePM does for you

- The program gets no variable of `maxpm serve`'s environment but `PATH`,
  `HOME`, the language, `TMPDIR`, `TZ` and `MAXPM_PLUGIN_CONTRACT`. It gets
  only the secrets its manifest names, and only the ones the pipeline mapped.
- A call that runs past its `timeout` is stopped with every process it
  started. A crash, a hang or an answer that is not JSON goes on the
  pipeline as its last error (`maxpm pipeline show`, and the Accounts list of
  the Deploy tab); the other pipelines go on.
- A failed `start` fails that stage run with the plugin's message.

### Test a plugin

1. Run each call by hand: `echo '{"contract": 1, "call": "describe"}' |
   python3 plugin.py`, then a `poll` request with your settings.
2. Keep the system's answers in files and test the plugin against them, not
   against the live system.
3. `maxpm pipeline plugin <name>` checks the manifest and `describe`.
4. Connect a test pipeline (`maxpm pipeline forge <test pipeline> <name>
   ...`), and follow it with `maxpm pipeline show <test pipeline>` and on the
   Deploy tab.

## What happens when a stage fails

- A failed run opens a fault task in the pipeline's project, titled
  `pipeline:<pipeline>:<stage> failed for <version>`, with the failed tests
  and the command that runs the stage again. A later failure of the same
  stage joins that task. It is an operational task: a new session takes it.
  The pipeline's trigger rule "any stage fails" does this (next section): with
  that rule off, a failure opens no task.
- `maxpm pipeline run <run> [--prompt]` prints the run, its failed tests and
  the prompt that analyzes them. `maxpm pipeline fix <run> [--start]` adds a
  fix task from that prompt (`--start`: and a session on it now).
- `maxpm pipeline rerun-failed <run>` runs only the failed tests again (a
  person or the manager).

Fix the cause in the code, and let the pipeline run the version again. Do not
deploy by hand around the pipeline.

## Transitions

The arrow into each stage is a transition. It controls only the flow; the
tests and checks run inside the stages. A version that passed the stage before
waits at the transition until its mode lets it in and no control holds it.

The mode: how a version passes when nothing holds it.

- `auto`: at once.
- `yes`, `yes <person>`, `yes manager`: a yes. A person's yes is a task in
  What waits for you, and only a person (the one named) gives it. The
  manager's yes is one alert to the manager; a person may give it too.
- `delay 30m`: after that time, while no check of the project (or of the
  environment's target before it) fails.
- `release`, `release every 2h`: the release. The manager or a person
  releases each version; with a cadence, the next version goes in by itself
  when the last one entered that long ago.
- `forge`: the forge starts the stage (a push, a tag); MaximizePM only reads
  its run.

The controls: each one holds every waiting version until it ends. Each needs a
reason, and each goes in the audit log with who set it and until when.

```
maxpm pipeline transition <name> <stage>                   # show it: mode, controls, state, waiting versions
maxpm pipeline transition <name> <stage> --mode "yes mark" --window "mon-thu 09:00-17:00 America/Los_Angeles" \
  --fault-rule on --reason "<why>"                         # the standing rules; --window none removes it
maxpm pipeline block <name> <stage> --reason "<why>" [--until <time> | --until-done <item>]
maxpm pipeline unblock <name> <stage>
maxpm pipeline delay <name> <stage> <time> --reason "<why>"   # none ends it
maxpm pipeline disable <name> <stage> --reason "<why>"        # until someone enables it
maxpm pipeline enable <name> <stage>
maxpm pipeline freeze <name> [<stage>] --from <time> --to <time> --reason "<why>"   # no stage: every transition
maxpm pipeline freeze <name> [<stage>] --clear
maxpm pipeline approve <name> <stage> [--rev <commit>]     # the yes of the mode
maxpm pipeline pass <name> <stage> [--rev <commit>] --reason "<why>"   # one version past every control, once
```

- **Window**: days (`mon-fri`, `mon,wed`, `weekdays`, `weekends`, `daily`;
  none: every day), a time span (`22:00-04:00` runs over midnight) and a time
  zone (none: the setting `timezone`). `;` between windows.
- **Fault rule**: the transition is blocked while a fault of the pipeline is
  open: a failed stage's fault task, or an open `ops:` fault of its project
  or of its environments' targets. It opens when the fault closes.
- **Block**: until a time (`2h`, `fri 17:00`), until an item closes (a fault,
  a person's task), or until someone unblocks it. A block, a delay and a
  freeze end by themselves at their time.
- **Approve and pass**: approve gives the yes of the mode only; a block or a
  window still holds the approved version. Pass lets one waiting version past
  every control once; a yes that only a named person gives still waits for
  that person.
- Nobody is asked for a yes while the transition is blocked, disabled or
  frozen.

The page shows the same on each arrow: the mode or the control that holds it,
who set it, until when and the version that waits. A click on the arrow opens
its controls and their buttons.

## Trigger rules: what starts an agent

Each pipeline has its own trigger rules. A rule says: when this happens in the
pipeline, an agent does this. A new pipeline starts with one rule, "any stage
fails: find the cause and fix it".

A rule names a generic event, never an event of GitHub, GitLab or Pulumi:
every source reports its runs the same way, so one rule works for a script and
for a forge.

| Event | When it happens |
|---|---|
| `stage-failed` | a stage run failed (a run of only its failed tests again does not count) |
| `check-failed` | a check of an environment's target, or of the pipeline's project, became a fault within a day after a version went to that environment, and before the next one did |
| `transition-opened` | a version went into a stage: its transition let it in |
| `run-started` | a stage run started |
| `run-ended` | a stage run ended: passed, failed or stopped |

Each rule also names the stages it watches (one stage, or any stage), what the
agent does (the instructions of its task), where the agent runs (this
computer: `maxpm serve` starts a new session), and the keys its task gets
(credential sets, granted to that task alone until it closes).

```
maxpm pipeline triggers <name>                           # the rules, and what they did lately
maxpm pipeline trigger <name> add --when <event> [--stage <stage>] --does "<instructions>" [--keys <set>,<set>]
maxpm pipeline trigger <name> edit t<n> [--when ...] [--stage ""] [--does ...] [--keys ""]
maxpm pipeline trigger <name> off t<n> --reason "<why>"  # on t<n>: it acts again on what happens from then
maxpm pipeline trigger <name> remove t<n>
```

What a rule does when it acts:

- `stage-failed`: it opens the stage's fault task, or joins the open one, and
  adds its instructions to the task.
- `check-failed`: it adds its instructions and the version that went out to
  the check's open fault task (`ops:<scope>:<check>`), or adds a task when the
  check has none.
- The other events: it adds a task, `pipeline:<pipeline>:<stage> <event> for
  <version>: <instructions>`, with the run and the commands to read it.

Each task is operational: a new session takes it. An event acts once for each
rule. Each act is a line of the pipeline's audit log, by `trigger t<n>`.

Only a person or the manager changes the rules, on the Triggers tab of the
pipeline's page or with these commands. Keys: only a person names a credential
set, or the manager names one that the person lets the manager grant. When
you work on a task that a rule added, its context names the rule and the
event; do what the instructions say, and close the task as any other.

## Actions on a running pipeline

A person or the manager does these; each one goes in the audit log with who
did it.

```
maxpm pipeline hold <name> --reason "<why>"               # every stage: nothing new starts
maxpm pipeline unhold <name>
maxpm pipeline runs <name> <stage>                       # Versions in <stage>: each run there, who and why
maxpm pipeline rerun <name> <stage> --rev <commit> [--reason "<why>"]   # run one stage again, an older version too
maxpm pipeline move-back <name> <stage> --rev <commit> --reason "<why>" # back to a version that passed there
maxpm pipeline stop <run>                                # the command, the forge run, or the wait at the transition
maxpm pipeline rollback <name> <env> --rev <older commit> --reason "<why>"
```

`maxpm pipeline hold <name> <stage>` blocks the transition into that stage
(the same as `block` with no end); `unhold <name> <stage>` unblocks it. A run
again and a move back pass the transition on purpose.

**The way back.** `maxpm pipeline runs <name> <stage>` lists each version that
ran in a stage, newest first: what it was (deployed or ran by the flow, moved
back, run again, failed tests again), who sent it or said yes, and why; the
version its environment runs now is marked live. A move back takes a version
that passed there before: the stage runs again with it (a deploy stage puts
it on its environment), as one more run in that list, and the transition into
the stage is blocked, so no newer version comes by itself. Unblock it when the
cause is fixed. `rollback <name> <env>` is the same move back for the deploy
stage of an environment, and takes any version (a forge may have deployed it).
On the page, a click on a stage shows "Versions in <stage>" with Move back
here, Run again and Stop on each run.

To find what a version carries: `maxpm pipeline version <name> <commit>`
(the tasks its commits name). To find where a task runs: `maxpm pipeline item
<id>`.

## The audit log

Every change to a pipeline and its flow is one line of its audit log: who did
it (a person, an agent session, a trigger, a hook such as a stage's command,
or a forge), what (a definition change, start, approve, decline, hold, end of
a hold, a change of a transition, block, unblock, delay, disable, enable,
freeze, let one version pass, run again, roll back, stop, a stage that passed
or failed, a deploy, a fault task, a fix task, a token or grant change), on
which stage or transition, the version, why, until when, and the result (the
state of the run that the line started or ended, as it is now).

```
maxpm pipeline audit [<name>] [--who person|agent|trigger|hook|forge|<actor>] [--since today|7d|<date>] [--search "<words>"] [-n 50]
maxpm pipeline correct <line> "<what is wrong and what is right>"
```

- MaximizePM writes each line when the thing happens. You write none by
  hand: run the command or press the button, and its line follows.
- No line is ever changed or deleted, not even by a person. A mistake (a
  wrong reason, the wrong version) gets a correction: a new line that names
  the old one. The actor of that line, a person or the manager may add it.
- The changes of a token that a pipeline names (`--secret`), and the grants
  of the sets that hold it, show in that pipeline's log too.
- Before you report on a release or a failure, read the log of its pipeline:
  it says who approved, held, ran again or rolled back, and when.

## The Deploy tab

The person follows all of this on the page's Deploy tab:

- **Pipelines**: one row for each pipeline and each deploy target, its stages
  left to right, the transitions between them, and what runs where. Above the rows,
  a line for each version that waits for the person's yes (with Approve) and
  for each token that fails.
- **Where each version runs**, **Accounts** (each forge token and its state),
  **Audit log** (every line of every pipeline, with filters by pipeline,
  actor and time, a search, Correct on each line, and Export as a CSV file), and the page
  **Runners**.
- **Agents**: each pipeline that has work now, left to right. Under each stage
  stand its faults and the sessions that work on them: the session on a
  fault task, on a fix task found during it, or on a release's review or
  deploy. Each session's card shows its state, its step (`maxpm now`), where
  it runs, the secret sets granted to it, what started it, its proof when it
  is done, its cost, and Watch, Open the task and Stop. A done one stays a
  day. The stage cards and the Pipelines rows show the same as small marks
  (⚠ a fault, 🤖 a session).
- **A pipeline's own page**: Stages (each stage's runs, steps, tests and the
  failure prompt; a target's release card), Versions, Triggers (its trigger
  rules: add, edit, turn off or on; what they did lately), Emergency (hold
  every stage, stop what runs, put an environment back) and its Audit log.
- The red number on the tab counts what waits on the person there.

When you tell the person about a release, name the pipeline and the stage:
they find it there.

When you work on a fault of a stage, or on its fix: keep `maxpm now` current,
because your card under that stage shows it. Ask the person with
`maxpm ask <person> "<question>" --item <your task>`, never in your terminal
alone: your open question shows on your card with Yes and No, and the answer
comes back to you as an answer to the question.

## Later

Each Deploy tab task adds its part to this guide when it is built: tiers,
manual starts, the holding stage, branches, operators and the override,
and runners that start agents with no person's computer (a rule then names
one of them).
