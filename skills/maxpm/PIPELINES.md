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
- **Tier**: how close a stage is to the users: `alpha`, `beta`, `prod`, or a
  short name of the pipeline's own (`--tier`; `''` clears it). Several stages
  can share a tier, for example two prod regions. The stage card shows it as a
  badge, and a trigger rule can name a tier ("any prod stage failed"). A
  deploy target's default pipeline has its deploy and watch stages in prod.
- **Start**: how a stage starts when its transition lets a version in
  (`--start auto|manual`). `auto` (the default): it runs at once. `manual`:
  the version waits in the stage until someone presses Run on its card, or
  runs `maxpm pipeline run-stage <name> <stage>`. The transition decides
  whether a version may enter; the start decides whether the stage then runs
  by itself. A run again or a move back starts at once: a person sent it.
  Run takes a person, the manager, or an operator of the pipeline.
- **Branches and joins**: a stage follows the stage before it, or the stages
  that `--after` names (`maxpm pipeline stage <name> prod-b --after staging`;
  earlier stages only; `''` clears it). Two stages after one stage are
  branches: each gets the version, with its own transitions, stages and
  versions (one prod region with one server, another with three). A stage
  after two or more stages is a join (`--after watch-a,watch-b`): its
  transition lets a version in only when that version passed every branch,
  and the arrow names the branch it still waits for. Keep the stages of a
  branch together in the order, and put a branch's first stage `--after` the
  stage where it splits. The page draws each branch as a row of its own. A
  move back acts on one branch; `move-back --every-branch` also moves back
  the stage at the same place of each other branch where that version
  passed.
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
  pipeline of its own, computed from its releases: holding, build (the
  release-cut hook), review, approval, deploy, watch. It needs no setup.
- **Holding**: the first stage of every pipeline. It holds the commits that
  wait for the next release (section "Holding and the release").

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
   maxpm pipeline stage <name> beta --kind deploy --env beta --tier beta --run "make deploy-beta"
   maxpm pipeline stage <name> prod --kind deploy --env prod --tier prod --run "make deploy" --mode "yes mark"
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
  roll back is done on Pulumi's page (the plugin pulumi, below).
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

**Add a token.** A person adds a token on Deploy > Accounts (Add a token, or
Add it and Replace on a missing or refused one): forge, server (a server of
their own), the token, and its name. MaximizePM asks the forge who the token
is before it saves anything: a refused token is not saved, and the form says
the forge's words. A token that works is saved as a hidden secret (no command
prints it; with no name: `<forge>-<account>`), and the form shows its
account, end date and the pipelines that use it. Make the token for me adds
a task for a session (and starts it): it opens the forge's new-token page in
the project's browser, the person signs in and presses Generate there, and
the session saves the token unseen. When you work on such a task, follow its
context: `maxpm browser open <page>`, fill in the name, the end date, the
repositories and the permissions, ask the person to press Generate (`maxpm
ask`), save it with `maxpm secret put <name> --from-browser "<field>"`,
check it with `maxpm pipeline accounts --check --secret <name>`, and close
the task. Only a person adds or makes a token; an agent with no task for it
asks the manager.

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
connects through a plugin, or sends its events with no plugin (next
part). `maxpm pipeline plugins` lists the installed
ones, with the actions each one does. Connect a pipeline to one:

```
maxpm pipeline forge <name> <plugin> --set <key>=<value> --secret <its secret>=<maxpm secret>
maxpm pipeline plugin <plugin>        # its settings, its secrets, and a check that it answers
```

- `--set` gives a setting that the manifest names (`--set url=https://...`);
  each pipeline has its own settings.
- `--secret` maps a secret that the manifest names to a maxpm secret
  (`--secret token=jenkins-ci`). A plugin with one secret takes the maxpm
  secret alone (`--secret jenkins-ci`). A person saves the value first:
  `maxpm secret put jenkins-ci`.
- Only a person or the manager connects a pipeline: an agent asks the
  manager.

Each stage then names the work in that system with `--workflow`. When a
stage's gate opens, MaximizePM asks the plugin to start that work. The page
shows an action that the plugin does not do (run again, roll back, stop) as
"not available for this source", and the command for it is refused: do that
step in the system itself.

MaximizePM ships four plugins; every forge above is one of them. The contract
in "Write a plugin" is enough to write one; their code shows it at work.
`tests/plugins/shipyard/` is a plugin that an agent wrote from this guide and
the jsonl example only, with its test against recorded answers (#6165):

- `github` (`river/pipeline/plugins/github/`): the GitHub, Gitea and
  Forgejo source above, the full example. `maxpm pipeline forge <name>
  github|gitea|forgejo` uses it.
- `gitlab` (`river/pipeline/plugins/gitlab/`): GitLab CI, the example for a
  system where one pipeline holds every stage of a commit. `maxpm pipeline
  forge <name> gitlab` uses it.
- `pulumi` (`river/pipeline/plugins/pulumi/`): Pulumi Delivery, the example
  for a system with no list of runs and with gates of its own. `maxpm
  pipeline forge <name> pulumi` uses it.
- `jsonl` (`river/pipeline/plugins/jsonl/`): the short example. It follows a
  file of JSON lines that another program writes, one line for each change of
  a run, and it writes start and stop requests to a second file.

### The github plugin

- **What it reads**, once a minute: the newest 10 runs of each stage's
  workflow, their jobs (the stage's steps), the JUnit artifacts of a finished
  run (named `junit*`, or the stage's `--junit` name), and, on GitHub, each
  environment's deployments with a success status. Once an hour, on GitHub,
  the names of the secrets of each deploy stage's environment (`secrets` in
  its answer: names only).
- **What it does**: start, run again and roll back are `workflow_dispatch` of
  the stage's workflow on the branch (`--ref`, `main` when not set) with the
  inputs `maxpm_run` and `maxpm_rev` (the older commit of a roll back); stop
  cancels the run.
- **The agent job**: while MaximizePM waits for a job of `agent.yml` (setting
  `agent`), it reads that job's runs and its artifact `maxpm-agent` (see
  'Agents in GitHub Actions').
- **What it cannot do, and why**: a run that a push or a tag starts carries no
  MaximizePM id, so the plugin links it to the version that waits at that
  stage (the same commit). Gitea and Forgejo have no deployments API: a deploy
  stage that passes records its version. Some Gitea and Forgejo versions have
  no jobs, artifacts or cancel call: the read goes on without them, and stop
  says to stop the run on the forge's page.
- **Its token**: a fine-grained token with Actions read and write,
  Deployments read and Contents read on the repository, and Environments read
  for the secret names (without it the stage cards show none) (on Gitea and Forgejo
  a token with the repository scope), saved as a maxpm secret.
- **Its test**: the plugin's `handle(request, fetch)` takes the HTTP function,
  so a test passes recorded API answers instead of the network
  (`tests/test_pipeline.py`, classes GitHub and GiteaForgejo). Test your own
  plugin the same way.

### The gitlab plugin

- **What it reads**, once a minute: the newest 10 pipelines of the project.
  For each MaximizePM stage (`--workflow` names its GitLab stage), the jobs of
  that GitLab stage are one stage run, with the key `gl-<pipeline>-<stage>`: a
  failed job that may not fail fails it, a manual job that waits keeps it
  waiting. A finished pipeline's test report gives the tests, each suite to
  the stage of the job that made it. Each environment's last successful
  deployment gives the version that runs there.
- **What it does**: start plays the stage's manual jobs (`when: manual` in
  `.gitlab-ci.yml`) in the pipeline of the version's commit; run again and
  roll back retry the stage's jobs in that commit's pipeline (GitLab's own
  roll back of an environment); stop cancels the stage's running jobs.
- **What it cannot do, and why**: jobs that start by themselves (no `when:
  manual`) run before MaximizePM's transition opens; give that stage's
  transition the mode `forge`, so MaximizePM only reads it. An approval gate
  in GitLab needs a paid plan; MaximizePM's transitions need none.
- **Its token**: a project access token with the `api` scope, saved as a
  maxpm secret (`--url` for your own GitLab server).
- **Its test**: `tests/test_pipeline.py`, class GitLab, against recorded
  answers.

### The pulumi plugin

- **What it reads**, once a minute: Delivery runs by number, from the setting
  `ref` (`--ref`, the first run) on, up to the first number that has no run;
  its state keeps the oldest run that has not ended. Each Delivery stage that
  a MaximizePM stage names (`--workflow`) is one stage run, with the key
  `pd-<run>-<stage>`.
- **What it does**: start begins a Delivery run (for a version that no run
  carries yet), or, when the run is there, continues the stage's Delivery
  gate: each pending manual condition, and an approval condition only after a
  person's yes. When the gate is not open yet, the next poll continues it.
  Run again retries the Delivery run (its failed members, the same commit);
  stop cancels the whole Delivery run.
- **The agent job**: while MaximizePM waits for an agent job, it starts the
  job's deployment, reads its state, and at its end reads the proof from its
  log (see 'Agents in Pulumi Deployments').
- **What it cannot do, and why**: Pulumi documents no list of runs (so it
  reads them by number), no commit or times of a run (so a run's version is
  the one MaximizePM sent, else `release <n>`, else `run <n>`, and a stage
  shows no steps), no roll back of a stage (do it in the stage's Actions menu
  on Pulumi's page), and no way to run a commit named from outside (a start
  runs the head of its source). Its routes are a public preview and can
  change. Pulumi documents no link from a Delivery stage to the ESC
  environment that holds its secrets, so the plugin sends no `secrets`: name
  them by hand (`maxpm pipeline keys <name> <stage> --kept-in "Pulumi ESC
  environment org/project/prod" --names a,b`, section "Keys for stages and
  runners").
- **Its token**: a Pulumi access token of the organization, saved as a maxpm
  secret.
- **Its test**: `tests/test_pipeline.py`, class PulumiDelivery, against
  recorded answers.

## Send events with no plugin

A system that MaximizePM does not ask (Jenkins, Argo CD, Octopus, a cron
job) can tell MaximizePM what it does. It sends the events of the form in
"The events" below, for one pipeline:

```
maxpm pipeline event <pipeline> --file events.json      # or --file - (stdin)
maxpm pipeline report <run> --steps output.txt --junit junit.xml
```

- `event` takes one JSON object, a JSON list, or one object on each line.
  All the events go in, or none (the reply says what is wrong). Give each
  event an id that is the same each time you send the same change: the same
  event sent twice changes nothing.
- `report` adds `::step` lines (as a stage's script prints them, "Write a
  stage's script") and a JUnit file to a stage run (`--steps -` reads stdin).
- A run that an event ends as passed hands its version to the next stage,
  as a run of a forge does. Give the stage's transition the mode `forge`
  when the system starts its runs itself.
- Only a person or the manager runs these commands (a cron job of the person
  runs as the person).

Over HTTP, from a program on this computer or on the person's tailnet:

```
maxpm secret put ci-events          # a long random value, unseen
maxpm pipeline intake <pipeline> --secret ci-events     # --off: no more
curl -X POST http://127.0.0.1:<serve port>/api/pipeline-events \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"pipeline": "<pipeline>", "events": [...]}'
```

- The token is the value of the secret that `intake` names: it reaches that
  pipeline only. A wrong or missing token gets 401; a pipeline with no
  intake secret gets 403.
- `{"run": <id>, "steps": "<lines>", "junit": "<xml>"}` reports steps and
  tests, as `maxpm pipeline report` does.
- `maxpm serve` takes the request only from this computer or from the
  person's tailnet, and never from a page in a browser.
- Each request that changes something is a line of the audit log, with the
  token's secret as its actor.

## Write a plugin

A plugin is a folder with two parts: a manifest, `plugin.json`, and a program
in any language. It imports nothing of MaximizePM's. Put the folder in
`plugins/<name>/` beside the queue's database (`maxpm db` shows that folder;
usually `~/.maximizepm/plugins/<name>/`). The folder's name is the plugin's
name. A plugin that lives in a project's repository: copy its folder into
that plugins folder (`maxpm plugin add <folder>`), or put a link to it there.
`maxpm plugin new <name> [--from jsonl|github|gitlab|pulumi]` writes a
starter there from a shipped plugin. The program writes no file in its
folder but under `var/` (its files there are not part of what a person
approves); keep a cursor in the answer's `state`.

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
  "hosts": ["jenkins.example.com"],
  "poll_every": 60,
  "timeout": 30
}
```

- `contract`: 1, the version of this contract.
- `name`: lower case letters, digits and `-`; the same as the folder. The
  names of the built-in forges (`github`, `gitlab`, `gitea`, `forgejo`,
  `pulumi`) and `none`, `maxpm`, `here`, `plugin` are taken.
- `level`: `report` (it reads only; no actions), `act` (it reads, and it does
  the actions it lists), or `gate` (as `act`; the outside flow also asks
  MaximizePM's transitions before each stage: `maxpm pipeline may`, section
  "Outside flows ask the transitions").
- `run`: the command as a list of words, run in the plugin's folder.
  `{python}` is the Python that runs MaximizePM. A test may load the program
  from another folder: open `plugin.json` by the program's own path, not the
  current folder.
- `actions`: some of `start`, `stop`, `rerun`, `rollback`.
- `settings` and `secrets`: each name with its `help`. A setting can be
  `required` and have a `default` (MaximizePM fills it in when the pipeline is
  connected). A secret is required unless it says `"required": false`.
- `description`: one line for the person. Say there what the plugin cannot
  do, such as "no roll back".
- `hosts`: the hosts it reaches (at most 20), for the person who approves it.
- `poll_every`: seconds between two polls (60 when not given; at least 10).
- `timeout`: seconds for one call (30 when not given; at most 300). Keep the
  plugin's own network time limits below it: a call that MaximizePM stops
  keeps nothing of its answer.

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
  system's id of the run, a string>, "xml": <the file's text>}`; MaximizePM
  reads them with its own parser, once for each run (a second report of the
  same run is skipped). Send a run's report in the same answer as its
  `finished` event: MaximizePM reads the reports first, then hands the version
  on. And `counts`: numbers of this poll for the person, such as
  `{"runs": 10}`.
- `start`: start the work of one stage run, the `run` of the request (its
  stage's `work`, its `version`: the commit as MaximizePM has it, which a
  person may have typed short). Answer with `key`, the system's id of the new
  run as a string: from then on, events with that subject id are this run's,
  and no `customData.maxpm.run` is needed. `url` and `note` are optional, and
  the answer may also carry events.
- `rerun` (run a stage again) and `rollback` (put an older version on an
  environment): `run` is a new stage run that MaximizePM made for it, with no
  key yet; the earlier run of that stage is in `runs` with its key. Start the
  work again in the system, and answer with the key of the system's new run.
  When the system's retry keeps the same id, answer that id: the acting run
  takes the key, and the earlier run keeps its record under `<key>~<its id>`.
  So a key in `runs` that ends in `~<id>` is an older record: use the part
  before `~` to reach the system. A `rerun` of failed tests also has `tests`;
  a system that cannot run only those runs every test and says so in `note`.
- `stop`: stop the run. Answer `{}` or a `note`; the run ends when a later
  poll reports its end. A run with no key has nothing in the system yet:
  answer an `error` that says so.
- MaximizePM never sends an action that the manifest's `actions` leave out.

The request of `poll` and of each action also has:

- `pipeline`: its `name`, `project`, `stages` (each with `name`, `kind`,
  `pos`, `work` (the stage's `--workflow`: match the system's job to it),
  `env`, `transition` (the mode of the arrow into it), `junit` (the stage's
  `--junit` name)) and `envs` (each with `name`, `target`, and the `version`
  that runs there).
- `settings`: an object of the pipeline's settings, by the manifest's names
  (`{"url": "https://...", "job": "shop"}`), defaults filled in.
- `secrets`: an object of the secret values, by the manifest's names
  (`{"token": "..."}`). A secret that the pipeline did not map is missing.
- `state`: the `state` of the plugin's last answer that had one (null at
  first). Keep a cursor here, such as the last build number read; the plugin
  keeps no file of its own. An answer with no `state` keeps the one before.
- `runs`: the stage runs that wait or run, and the newest 100 others. Each
  has `id` (a number), `stage`, `work`, `version`, `key` (the system's id of
  the run, or null before a start or an event gave one), `state`, `purpose`,
  `url`, `runner` (`forge:<source>` when the plugin was asked to start it),
  `started_at`, `approved_at` and `approved_by_person` (a person, not the
  manager or a rule, let it through its transition).
  - `state`: `queued` (it waits at its transition, or for its system to
    start it), `running`, `passed`, `failed`, `cancelled` or `skipped`.
  - `purpose`: why MaximizePM made it: `rerun`, `failed:<run id>` (the failed
    tests of that run again), `rollback`, or null for a version that moved on
    by itself.
- an action only: `run`, the stage run it acts on (the same fields). A
  `rerun` of failed tests also has `tests`: one `suite.name` for each test.

Any answer can also have `notes`: `[{"run": <id>, "note": <text>}]`, a line
that the page shows on a waiting run, such as what a gate in the system still
waits for.

A `poll` answer can also have `secrets`: `[{"stage": <name>, "kept_in":
<where>, "names": [<name>, ...]}]`, the names of the keys that the system
keeps for a stage itself (an environment's secrets, a vault). Never send a
value: MaximizePM keeps only the names (at most 100 for one stage) and shows
them on the stage card. Each one replaces what the plugin said before for
that stage and place; send `"names": []` when the place is gone. Send them
seldom (the github plugin: once an hour, its state keeps the time).

The answer can always have `error` (a message). Exit 0 when the call worked.
On an error, exit 1 or more and say why in `error` or on stderr. Do not print
anything else on stdout. An answer with `error` fails the call also with exit
0, and MaximizePM keeps nothing of a failed answer (no events, no state): the
next poll asks again from the same state. Name the cause in the error, such
as "Shipyard refused the key (401): save a new one with maxpm secret put
...": the Deploy tab shows it on the pipeline's source.

One answer carries at most 2000 events, 50 reports and 64 KB of state. When
more changed, answer what fits and keep the cursor there: the next poll goes
on from it.

A whole `poll` request, as the program reads it:

```json
{"contract": 1, "call": "poll",
 "pipeline": {"name": "shop", "project": "shop",
              "stages": [{"name": "build", "kind": "build", "pos": 1, "work": "build", "env": null,
                          "transition": "auto", "junit": null},
                         {"name": "deploy", "kind": "deploy", "pos": 2, "work": "deploy", "env": "prod",
                          "transition": "yes", "junit": null}],
              "envs": [{"name": "prod", "target": null, "version": "9b8c7d6"}]},
 "settings": {"url": "https://ci.example.com", "project": "shop"},
 "secrets": {"key": "..."},
 "state": {"cursor": 7},
 "runs": [{"id": 12, "stage": "deploy", "work": "deploy", "version": "3f2a9c1e5b7d...", "key": null,
           "state": "queued", "purpose": null, "url": null, "runner": null, "started_at": null,
           "approved_at": null, "approved_by_person": false}]}
```

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

- Every event: `context.version` `"0.4.1"`, `context.type`
  `dev.cdevents.<subject>.<predicate>.0.2.0`, `context.id`, and
  `context.timestamp` (RFC 3339; when it is missing or null, the time
  MaximizePM reads it). `customData.maxpm` holds MaximizePM's own fields.
- A stage run: subject type `pipelineRun`, `pipelinerun.queued`, `.started`,
  `.finished` (`outcome` success, failure, cancel or skip; `errors` for the
  reason). The subject id is the system's id of the run, a string.
  `content.pipelineName` is the stage's name. A run that the poll sees for the
  first time after its end: send `started` (it gives the start time) and
  `finished`; `finished` alone is accepted too.
- `customData.maxpm.stage` names the stage (else `content.pipelineName`);
  `version` names the commit (it may be null).
- A step: subject type `taskRun`, `taskrun.started` and `.finished`. The
  subject id names the step inside its run (such as `<run id>/<step name>`).
  `content.taskName` is its name; `content.outcome` takes the run's words
  (skip for a step that did not run); the times are the events' timestamps.
  `customData.maxpm.run_key` (or `content.pipelineRun.id`) is the run's
  subject id. `customData.maxpm` may also have `kind` (build, test, deploy,
  check, wait), `where` (a host), `parallel` (true), `seconds` and `note`.
- Tests: answer `reports` (above), and MaximizePM reads the JUnit itself. A
  system with no JUnit sends `testcaserun.finished` events instead, with
  `content.testCase.name`, `content.outcome` (success, failure, error, skip),
  and `customData.maxpm.run_key` and `suite`. Use one way for each run.
  Failed tests show on the run, but the system's outcome decides the run: a
  run that the system calls passed stays passed. A stage whose command runs
  on this computer is different: its failed test fails it.
- A version that now runs in an environment: `service.deployed` with
  `content.environment.id` (the environment's name). A deploy stage that
  passes records its version by itself: send this only for a deploy that no
  stage made.
- Give each event an id that is the same each time you read the same change,
  and different for every other change of the pipeline (any text):
  MaximizePM keeps each event once for each pipeline, so a change read twice
  changes nothing. MaximizePM sets the event's source to the plugin's name.
- A run that ends passed hands its version to the next stage, as a run of a
  forge does.

How MaximizePM finds the run of a `pipelinerun` event, in this order:

1. The run whose key is the event's subject id (the key that a start, a
   rerun or an earlier event gave it).
2. `customData.maxpm.run`, the id (a number) of a MaximizePM stage run from
   `runs` or the action's `run`: that run takes the subject id as its key.
3. Else a new run of the stage. An event that names a stage the pipeline
   lacks adds that stage at the end: skip the system's jobs that no stage's
   `work` names.

MaximizePM does not match a run by its version. A run that the system starts
by itself (a push, a timer) for a version that waits at that stage: find that
run in `runs` (the same stage, `state` `queued`, no `key`, the same commit;
a person may have given a short commit id) and set `customData.maxpm.run`.
Else the version has two runs of that stage. Give such a stage's transition
the mode `forge`, so MaximizePM only reads it.

### What MaximizePM does for you

- The program gets no variable of `maxpm serve`'s environment but `PATH`,
  `HOME`, the language, `TMPDIR`, `TZ` and `MAXPM_PLUGIN_CONTRACT`. It gets
  only the secrets its manifest names, and only the ones the pipeline mapped.
- A call that runs past its `timeout` is stopped with every process it
  started. A crash, a hang or an answer that is not JSON goes on the
  pipeline as its last error (`maxpm pipeline show`, and the Accounts list of
  the Deploy tab); the other pipelines go on.
- A failed `start` (an `error`, or an exit that is not 0) fails that stage
  run with the plugin's message.
- A plugin's secrets are its own: the hourly account check (Accounts and
  tokens, above) covers the shipped forges only. A refused key shows as the
  pipeline's last error, in the words of the plugin's `error`.

### Test a plugin

1. Shape the program so a test needs no network: a function `handle(request,
   fetch)` that returns the answer, where `fetch(method, url, headers, body)`
   returns `(status, bytes)`, and a `main()` that reads stdin, calls
   `handle` with the real HTTP function, and prints the answer (exit 1 with
   `{"error": ...}` on a failure).
2. Keep the system's answers in files and test `handle` against them, not
   against the live system: a first poll, a second poll with the `state` it
   returned (nothing new), each action, and a refused key.
   `tests/plugins/shipyard/test_shipyard.py` is such a test.
3. Run each call by hand: `echo '{"contract": 1, "call": "describe"}' |
   python3 plugin.py`, then a `poll` request (the whole request above).
4. `maxpm pipeline plugin <name>` checks the manifest and `describe`, after
   a person's yes for its files (next part). It finds plugins only in the
   plugins folder beside the queue's database: try
   it on a test queue (`MAXPM_DB=<new folder>/x.db`, with the plugin in
   `<new folder>/plugins/<name>/`). A session that `maxpm serve` started
   carries its token (`MAXPM_TOKEN`), which a test queue does not know:
   unset it for the test queue.
5. A person or the manager connects a test pipeline (`maxpm pipeline forge
   <test pipeline> <name> ...`); follow it with `maxpm pipeline show <test
   pipeline>` and on the Deploy tab. Test with recorded answers, not with a
   person's real secret.

### Install, versions and a person's yes

```
maxpm plugin add <folder or git address> [--ref <tag>] [--path <subfolder>]   # a person or the manager
maxpm plugin list                       # each plugin: version, the yes, kept versions, pipelines
maxpm plugin show <name>                # what a person approves, kept versions, pins
maxpm plugin trust <name> [--version <v>]   # only a person
maxpm plugin pin <pipeline> <version|folder>  # a person or the manager
```

- `add` copies the plugin into the plugins folder beside the queue and keeps
  a copy of each version (`plugins/.versions/`). A version number that comes
  again with other files is kept as `<version>+<8 of its digest>`. It
  refuses to replace a folder whose files no kept version has (a plugin
  someone writes there) unless `--replace`. Nothing goes into an agent CLI's
  home.
- A plugin runs only after a person approved its exact files: their sha256,
  without `var/`. `trust` and `show` print what the person approves: the
  level, the actions, the secrets it gets, the settings, the hosts it
  reaches and its command. After any change of its files it waits for a new
  yes: its pipelines get no news, the read says why, and a person's item
  ("Approve plugin <name> <version> for pipeline <p>") asks once for those
  files. Done by a person, that item is the yes. The shipped plugins need
  no yes.
- `maxpm pipeline forge <p> <plugin>` pins the version whose files the
  plugin's folder holds then. A later `add` does not move it: `pin` does, and
  the pinned version runs only with its own yes. A pipeline with no pin
  (`folder`) runs the folder as it is, which is how you test a plugin that
  you write.
- An agent may write a plugin (`new`) and test it on a test pipeline after
  the person's yes. It never approves one, and it asks the manager to
  install one.

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

## Outside flows ask the transitions

A deploy that another system runs (Jenkins, Argo CD, Octopus, a cron job, a
workflow on a runner of this computer) asks before each stage whether the
version may enter it. The transition into that stage answers, with all its
controls and its mode, as for a stage that MaximizePM runs. The stage is a
stage of the pipeline with no `--run`: the outside system runs it.

```
maxpm pipeline may <pipeline> <stage> --rev <version> [--wait 30m] [--from jenkins]
curl -fsS "http://127.0.0.1:<serve_port>/api/pipeline-gate?pipeline=<p>&stage=<s>&rev=<v>&wait=10m&from=jenkins"
```

- The command exits 0 for yes and 1 for no, and prints why. The HTTP answer
  is 200 `{"may": true, ...}` or 409 `{"may": false, "why", "until"}`, so
  `curl --fail` exits with the answer. `until` says when a block, a freeze, the
  window or a delay ends.
- `--wait` asks again every 5 seconds until a yes or the end of the wait (at
  most 1h; the HTTP request at most 10m).
- The first question queues the version at the stage, marked with the asker:
  the arrow lists it ("asked by jenkins"), a person's yes makes the approval
  card once, and the manager gets the alert of a release once. `maxpm serve`
  never starts that stage.
- A yes means the version entered the stage. The outside system reports its
  end with an event of that run (`customData.maxpm.run`: the run id is in
  the answer; section "Send events with no plugin"), or the version's
  question at a later stage ends it as passed. The next stage then waits for
  the system's question there; `maxpm serve` does not run it. A no that a
  person declined stays no.
- Each change of the answer is one line of the audit log, by the asker (the
  filter `forge` shows the systems); a poll that gets the same no again adds
  no line.
- A stage that MaximizePM runs (one with `--run`) refuses the question.

When you connect such a system, add the question as a step before each stage
of its own flow, and name the system with `--from`.

## Followed pipelines

A pipeline that an outside system runs, where MaximizePM controls nothing of
its flow, is followed (mark 2026-11-18: "also we need to be able to just
follow along with outside systems").

```
maxpm pipeline follow <name> [--source <plugin>] [--project <project>]
maxpm pipeline follow <name> --off                     # MaximizePM runs its flow again
```

- Its runs come from its source: a plugin (`--source`; its settings with
  `maxpm pipeline forge <name> <plugin> --set ...`) or events in over HTTP. An
  event that names a stage the pipeline lacks adds the stage, so a followed
  pipeline needs no setup of stages.
- None of our flow acts on it: no transition, start, run again, Run, hold,
  move back, stop or override. Each refuses and says the pipeline is
  followed. MaximizePM hands no version on: the outside system reports each
  stage itself. The page marks it "followed · read only" and draws plain
  arrows.
- What still acts on its events: its trigger rules (`--does`: an agent works
  on the failure where we can act: our code, a pull request, a fix task, a
  message to its owner) and its hooks (`--command`, below). On the Emergency
  tab, Stop its agents and Revoke its keys still apply: they act on our side.

**Hooks.** A trigger rule with `--command "<command>"` runs that command on
its event instead of starting an agent, on any pipeline:

```
maxpm pipeline trigger <name> add --when stage-failed --command "./notify-owner.sh"
```

It runs in the pipeline's project folder, with `MAXPM_PIPELINE`,
`MAXPM_STAGE`, `MAXPM_REV`, `MAXPM_EVENT` and `MAXPM_RUN`; `maxpm serve` runs
it in the background, stops it after 10 minutes, and writes its exit code and
last line to the rule's list and to the audit log. A hook gets no keys. A
person or the manager adds it, as any rule.

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
computer: `maxpm serve` starts a new session; or a runner that the pipeline
ticks: the next section), and the keys its task gets (credential sets,
granted to that task alone until it closes). A set that is a key of some
stages (section "Keys for stages and runners") goes only to a task of those
stages: a rule of one stage cannot name a key of another stage, and a rule of
any stage holds it back for the others (its result says "held back").

```
maxpm pipeline triggers <name>                           # the rules, and what they did lately
maxpm pipeline trigger <name> add --when <event> [--stage <stage>] --does "<instructions>" [--runs-on <runner>] [--keys <set>,<set>]
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

## Holding and the release

Holding is the first stage of every pipeline. MaximizePM computes it from the
project's git folder (`maxpm project path`): the commits on the main branch
since the last version that left holding (for a pipeline: the newest version
that reached its first stage; for a deploy target: the pin of its last done
release, else the last version tag). It lists them oldest first, each with:

- its tasks (the `#<id>` in its message) and the session that wrote it (its
  Claude-Session line; else git's author);
- its changes (added and removed lines; Diff shows the commit);
- its review: passed in, or in, the review of a cut release that covers it;
  approved, or changes requested, in the Changes view of its task; else not
  reviewed.

```
maxpm pipeline holding <name>                 # the commits, their review, and what the release takes
maxpm pipeline release <name>                 # the reviewed commits, oldest first, up to the first one not reviewed
maxpm pipeline release <name> --all           # every commit (a review stage after the release checks them)
maxpm pipeline release <name> --rev <commit>  # up to that commit
```

The release is the arrow after holding; only a person or the manager makes
it. For a pipeline, the version (the last commit it takes) starts at the first
stage. For a deploy target, its collected release is cut, pinned at that
commit, as `maxpm target cut <target> --rev <commit>` does; the target's
cadence and its earlier release still decide when it may go. On the page the
word is release, never cut. Name your task in each commit message
(`(#<id>)`), so holding links the commit to it.

## Runners: agents with no person

A runner is a server where a rule's agent works with no person, no terminal
and no permission prompt. The list is shared by every pipeline; each pipeline
ticks the runners its rules may use, and a rule names one with `--runs-on`.
A rule that names no runner runs its agent on this computer, as before.

```
maxpm runner add ci-1 --ssh ci@ci-1      # check the server, install this computer's maxpm there, find its agent CLIs
maxpm runner allow ci-1 <pipeline>       # the pipeline's rules may use ci-1 (deny takes it away)
maxpm pipeline trigger <pipeline> edit t<n> --runs-on ci-1
maxpm runner list                        # each runner: what uses it, its load, its last runs
maxpm runner set ci-1 --cli codex --slots 2 --max-minutes 90 --max-cost 10
maxpm runner runs [ci-1]                 # the agent runs, newest first
maxpm runner log <run>                   # what the agent printed, in words
maxpm runner stop <run> --reason "<why>"
```

What the server needs:

- SSH from this computer with no prompt: Tailscale SSH (`tailscale set --ssh`
  on the server) or your key. `maxpm runner add` refuses a server that asks.
- Linux or macOS, git, and access to the project's origin (the runner clones
  the project from the origin of its folder here).
- The agent CLI, signed in with a key of its own. Each CLI runs with no
  terminal:

| CLI | How it runs there | Its model key on the runner | The cost limit |
|---|---|---|---|
| Claude Code (`--cli claude`, the default) | `claude -p` with `--output-format stream-json`, `--permission-mode dontAsk` and `--allowedTools`: a tool outside the list is refused, never asked | a secret with the variable `ANTHROPIC_API_KEY` in a set granted to the runner (next section), else `ANTHROPIC_API_KEY` in its environment | `--max-budget-usd`, the runner's max cost (5 dollars when not set) |
| Codex (`--cli codex`) | `codex exec --json` in its workspace-write sandbox, with network on, so maxpm reaches the queue | a secret with the variable `CODEX_API_KEY` in a set granted to the runner, else `codex login` there or `CODEX_API_KEY` | none in the CLI: the time limit stops it |

`maxpm serve` stops each run after the runner's max minutes (60 when not set).
What a Claude Code agent may do there with no prompt is its pipeline's list
(section "What agents may do with no prompt"). A pipeline with no list of
its own uses the runner's (`maxpm runner set <name> --allow "<tools>"`), else
maxpm, git, and reading and changing files.

What a run does:

1. A rule acts and names a runner: its task is reserved for a new session
   `<runner>-r<run>`, so no session here takes it.
2. When the runner has a free slot, `maxpm serve` opens one SSH connection
   with a reverse tunnel: a port on the runner's 127.0.0.1 leads back to
   `maxpm serve` here. The script goes on the connection's input, so no key
   stands in a command line. It makes a worktree of the project at the failed
   version (else at the origin's head) and starts the agent CLI there.
3. The agent runs `maxpm go`, `maxpm now`, `maxpm done` as any session does.
   maxpm on the runner sends each command through the tunnel, with the run's
   key and the session's token; serve runs it here, on this queue. Nothing
   listens on the tailnet for it, and the runner has no queue of its own: a
   command that only runs on the queue's computer (`maxpm wait`, `review pass`)
   is refused there.
4. The agent's output comes back on the connection: the run's transcript.
   When the CLI exits, the run keeps its cost and its last words; a task that
   is still open goes back to the queue, and the manager gets an alert.

When you are the agent on a runner: nobody answers at a prompt. Ask in the
queue (`maxpm ask`, or a person item that blocks your task), then release the
task and stop. Push your branch to origin, because the queue's computer does
not see your folder, and name the commit in `maxpm done <id> --commit <sha>`.

## Keys for stages and runners

An agent with no person has no browser sign-in, no Keychain prompt and no
terminal. Its keys are maxpm secrets in credential sets, granted to the place
where it works. Only a prod stage reads a prod key.

```
maxpm secret set prod-deploy --add prod-ssh-key --add prod-db-url   # a set of tokens and keys
maxpm secret grant prod-deploy --stage shop/deploy            # this stage may read it
maxpm secret grant prod-deploy --tier shop/prod               # or every prod stage of shop
maxpm secret level anthropic-ci --var ANTHROPIC_API_KEY       # the variable a CLI reads
maxpm secret set model-keys --add anthropic-ci
maxpm secret grant model-keys --runner ci-1                   # every agent run on ci-1 gets it
maxpm pipeline keys shop                                      # each stage's keys, by name
maxpm secret grants                                           # every grant: g<n> for sessions, k<n> for places
maxpm secret revoke k<n>                                      # end one
```

The rules:

- **A stage's command** on this computer gets the secrets of the sets granted
  to its stage and to its tier, as variables, and no other secret. A
  secret's variable is its own (`maxpm secret level <name> --var X`), else
  its name in capitals with `-` and `.` as `_` (`prod-ssh-key` is
  `$PROD_SSH_KEY`). The stage's output keeps a line that names what it got
  (`maxpm: the keys of stage deploy: prod-ssh-key as $PROD_SSH_KEY`), and
  MaximizePM masks each value in the output (`***`). A locked secret goes
  only to a command that its level names (`maxpm secret level <name>
  --command scripts/deploy.sh`).
- **A run on a runner** gets the runner's sets (the model key) and the sets
  that its rule granted to its task, as variables in the script on the
  connection's input: never on a command line. The transcript masks them.
  The task's history names what the run got.
- **A trigger rule's keys**: a set that is a key of some stages goes only to
  a task of those stages (the section on trigger rules).
- **Never a person's sign-in**: a set that holds a saved sign-in or browser
  cookies is refused for a stage, a tier or a runner. Such a secret added to
  the set later is left out, and the output says so.
- **Keys that the system keeps itself** (GitHub environment secrets, a Pulumi
  ESC environment) show by name only. The github plugin reads the names of
  each deploy stage's environment secrets; for another place, name them:
  `maxpm pipeline keys <name> <stage> --kept-in "<where>" --names a,b`
  (`--names ""` forgets that place). MaximizePM never reads their values.

The stage card shows its keys after a 🔒, and its detail has the table
"Secrets this stage may read": each secret, where it is kept, and who uses
it. A rule's card shows each key with the stages that may read it ("prod-deploy
(deploy only)"), and the runner's own. Deploy > Runners shows each runner's
keys.

Only a person grants a set, or the manager a set whose credentials the person
allows without asking. Each grant and revoke of a stage or tier key is a line
of the pipeline's audit log. The Emergency tab's Revoke its keys also ends the
pipeline's stage and tier keys of that set.

## Agents in GitHub Actions

When a stage runs a GitHub workflow, its agent can run in GitHub's own
runner: no server of yours, and no person's computer. GitHub cannot reach
this computer, so the job does not use the queue while it runs. GitHub starts
it, and the pipeline's read of GitHub (the github plugin, once a minute)
brings its state, its proof and its transcript back.

```
maxpm runner github <pipeline> --test "<the command that proves a fix>"
maxpm runner github <pipeline> --print         # show the file; write and change nothing
maxpm runner runs github                       # the jobs, newest first
maxpm runner log <run>                         # what the agent printed, in words
maxpm runner stop <run> --reason "<why>"       # cancels the job on GitHub
```

`maxpm runner github` (a person or the manager) does two things:

- It writes `.github/workflows/agent.yml` into the checkout you run it in (it
  must be a checkout of the pipeline's repository; `--file <path>` writes
  elsewhere). The job `maxpm-agent` starts on `workflow_run` when one of the
  pipeline's stage workflows fails on the branch the pipeline follows
  (`--ref`), for a commit of the repository itself, never a fork's.
- It moves the pipeline's stage-failed rules from this computer to `github`
  (or adds one): `--runs-on github` on a rule does the same by hand.

Then a person does two things on GitHub, once:

1. Set the model key as a repository secret: `ANTHROPIC_API_KEY` for Claude
   Code (the default), `CODEX_API_KEY` with `--cli codex`; `--secret <NAME>`
   names another. MaximizePM never reads or searches for the key.
2. Allow GitHub Actions to create pull requests (Settings > Actions > General
   > Workflow permissions). Then commit the file to the default branch:
   GitHub runs a `workflow_run` job only from there.

What the job does, in order:

1. Checks out the failed commit with no saved token, and saves the failed
   log (`gh run view --log-failed`) in `/tmp/maxpm-agent/failed.log`.
2. Runs the agent with no terminal and no prompt: Claude Code in print mode
   with `--permission-mode dontAsk`, `--max-budget-usd` (`--max-cost`, 5
   dollars) and the tools of `--allow` (default: read and change files, `git`
   status, diff, log, show, add and commit, and the first word of `--test`);
   or `codex exec --json` in its workspace-write sandbox. The agent commits
   its fix. It cannot push: its step has no token.
3. When the agent committed: runs `--test`, pushes the commit to the branch
   `maxpm/fix-<failed run id>`, and opens a pull request into the branch that
   failed.
4. Uploads the artifact `maxpm-agent`: `proof.json` (`commit`, `branch`, `pr`,
   `test` with its command, exit code and last line, `agent_exit`, `error`),
   the transcript and the logs. GitHub stops the job after `--max-minutes`
   (30).

What MaximizePM does with it:

- The rule opens the fault task, as for any failed stage, and reserves it for
  the record `github-r<run>`: no session here takes it while the job works.
- Each read of GitHub asks for the job whose run name is `maxpm agent <failed
  run id>`; the run shows running, then ended or failed, under Deploy >
  Runners with its cost and the agent's last words.
- At the end, the task goes back to the queue with the proof in a note, and
  the manager gets an alert. The session that takes it reads the pull
  request, checks that its tests pass, merges it, and closes the task.
- No job within 30 minutes (the file is not on the default branch, or the
  read of GitHub failed): the run fails with the reason, and a session here
  takes the task. A rule on `github` for a stage that runs here, or for
  another event than stage-failed, gives its task to a session here.

Note: GitHub does not run your workflows on a pull request that the job's own
token opened. The proof is the job's own `--test` run; the session that
merges runs the tests again.

## Agents in GitLab CI

The same for a pipeline that reads GitLab: its agent runs in GitLab's own
runner, and the gitlab plugin's read (once a minute) brings its state, proof
and transcript back.

```
maxpm runner gitlab <pipeline> --test "<the command that proves a fix>"
maxpm runner gitlab <pipeline> --print         # show the job; write and change nothing
maxpm runner runs gitlab                       # the jobs, newest first
maxpm runner stop <run> --reason "<why>"       # cancels the job on GitLab
```

`maxpm runner gitlab` (a person or the manager) does two things:

- It writes the job `maxpm-agent` at the end of `.gitlab-ci.yml` in the
  checkout you run it in (a checkout of the pipeline's project; `--file
  <path>` writes elsewhere), between two marked lines: running it again
  replaces the job and keeps the rest of the file. The job is in GitLab's last
  stage `.post` with `when: on_failure`, so GitLab starts it in the pipeline
  of the failed commit, after a job failed, on the branch the pipeline
  follows, in the project itself (never a fork). `--image` names its image
  (default `node:20`: it needs npm, git and python3).
- It moves the pipeline's stage-failed rules to `gitlab`, as for GitHub.

Then a person sets two masked CI/CD variables (Settings > CI/CD > Variables),
once, and commits `.gitlab-ci.yml` to the branch the pipeline follows:

1. The model key: `ANTHROPIC_API_KEY` (or `CODEX_API_KEY` with `--cli codex`;
   `--secret <NAME>` names another).
2. `MAXPM_GITLAB_TOKEN`: a project access token, role Developer, scopes `api`
   and `write_repository`. The job reads the failed jobs' logs with it, pushes
   the fix and opens the merge request. With none, the agent gets no log, and
   the push uses the job's own token (only when the project allows a push
   with it).

A protected variable reaches only a protected branch. MaximizePM never reads
or searches for either value.

What the job does, in order:

1. Saves the logs of the failed jobs of the pipeline's stages in
   `maxpm-agent/failed.log`. A failure only in a stage that no MaximizePM stage
   names starts no agent.
2. Runs the agent as the GitHub job does, with a clean environment: GitLab
   gives every CI/CD variable to every job, so the agent and the `--test` run
   get only the model key, `HOME`, `PATH` and `CI`.
3. When the agent committed: runs `--test`, pushes the commit to
   `maxpm/fix-<pipeline id>` with no hook of the checkout, and opens a merge
   request into the failed branch (git push options).
4. Keeps the artifact `maxpm-agent/` for 14 days: `proof.json` (the same
   fields; `pr` is the merge request) and the transcript.

What MaximizePM does with it is the same as for GitHub, with the record
`gitlab-r<run>`. The plugin finds the job `maxpm-agent` among the jobs of the
failed run's GitLab pipeline. When that pipeline ended with no such job (the
commit's file has none), the run ends at once and a session here takes the
task.

## Agents in Pulumi Deployments

When a stage is a Pulumi Delivery stage, its agent can run in a Pulumi
Deployment, the hosted runner of Pulumi Cloud. Delivery runs nothing when a
stage fails, and it gives no log of a job by its API. So MaximizePM starts
the job itself: the pulumi plugin's next read (once a minute) creates the
deployment, follows it, and reads the proof from its log at its end.

```
maxpm runner pulumi <pipeline> --env <project/env> --test "<the command that proves a fix>"
maxpm runner pulumi <pipeline> --print         # show the settings and the job's script; change nothing
maxpm runner runs pulumi                       # the jobs, newest first
maxpm runner log <run>                         # what the agent printed, in words
maxpm runner stop <run> --reason "<why>"       # cancels the deployment
```

`maxpm runner pulumi` (a person or the manager) keeps the job's settings on
the pipeline, and moves its stage-failed rules from this computer to
`pulumi` (or adds one). `--stack` names the stack whose deployments run the
job (default: the pipeline stack), `--branch` the branch that the job checks
out and a fix goes into (default `main`).

Then a person does two things on Pulumi's page, once:

1. Puts the model key in the ESC environment of `--env`, under
   `environmentVariables`: `ANTHROPIC_API_KEY` for Claude Code (the default),
   `CODEX_API_KEY` with `--cli codex`. Also `MAXPM_GIT_TOKEN`, a token that
   may push branches, when the fix should come back as a branch (on GitHub
   also with a pull request); without it, it comes back as a patch.
   MaximizePM never reads or searches for these keys.
2. Sets the deployment settings of the stack (Settings > Deploy): the source
   repository and the branch.

What the deployment does: a preview of the stack, whose pre-run commands
write the job's script and run it inside `pulumi env run <the environment>`.
The script, in order:

1. Saves what Pulumi shows of the failed Delivery run (its stages, their
   status and gates) in `/tmp/maxpm-agent/failed-run.json`.
2. Runs the agent with no terminal and no prompt, as the GitHub job does
   (`--allow`, `--max-cost`, `--max-minutes`), with neither the git token nor
   the Pulumi token in its environment: it can commit, and it cannot push.
3. When the agent committed: runs `--test`, then pushes the commit to the
   branch `maxpm/fix-pd-<run>-<stage>` and opens a pull request on GitHub,
   or, with no git token, makes a patch.
4. Prints its proof, the end of the transcript and the patch as lines that
   start with `::maxpm-` in the deployment's log, and ends with 0: the proof,
   not the deployment's status, says what the agent did.

What MaximizePM does with it is the same as for GitHub: the fault task is
reserved for the record `pulumi-r<run>`, the run shows under Deploy >
Runners, and at its end the task goes back to the queue with the proof in a
note. A patch is kept beside the queue: the session that takes the task
applies it (`git am <file>`), runs the tests, and commits it. No deployment
within 30 minutes (Pulumi refused it: the reason is on the run), or no
settings for the job: a session here takes the task.

## What agents may do with no prompt

An agent on a runner has nobody at a terminal to answer a permission prompt.
Each pipeline has one list in three parts: what its agents do at once, what
they ask first (a card, never a prompt), and what they never do. Each entry
is a permission rule as Claude Code writes it: a tool (`Read`, `Edit`,
`Write`, `Glob`, `Grep`, `WebFetch`), or a tool with what it acts on:
`Bash(git push:*)` is each command that starts with `git push`,
`Bash(make deploy)` only that command.

```
maxpm pipeline permissions shop                          # the list, and the steps that runs asked for lately
maxpm pipeline permit shop --allow "Bash(npm test:*)"    # at once
maxpm pipeline permit shop --ask "Bash(./deploy.sh:*)"   # a card first
maxpm pipeline permit shop --never "Bash(rm:*)"          # never
maxpm pipeline permit shop --remove "Bash(rm:*)"         # off its list
maxpm pipeline permit shop --default                     # back to the default list
```

A rule is on one list only: naming it again moves it. `Bash(maxpm:*)` stays
allowed, because the agent reports through maxpm. A pipeline with no list
has the default one: maxpm, git, and reading and changing files.

How a run uses it (Claude Code): the list becomes the run's own flags,
`--allowedTools` (at once) and `--disallowedTools` (ask first and never),
with `--permission-mode dontAsk`. MaximizePM writes nothing into the CLI's
home. A step outside the list is refused at once, never asked, and the
run's prompt names the three parts. After the run, MaximizePM reads the
refused steps from Claude Code's result line:

- a step of never: one line of the audit log, nothing more;
- a step of ask first, or a step on no list: a card (a person's item that
  the task waits on; the manager sees it first, as every person item). A yes
  (the card done) runs the task again on the same runner with that one step
  allowed for that task only. A no (the card dropped) ends there.

Codex has no list of tools: its run gets the list in its prompt only, and
keeps to it by itself.

When you are the agent on a runner and a step is refused: do the parts you
can without it, release the task with a note that names the step, and stop.
The card comes by itself; do not ask again in a prompt.

The Triggers tab shows the list (Edit the list changes it) and the steps
that runs asked for, each with its card and answer. Only a person or the
manager changes the list. Each change is a line of the audit log and a
version of the definition; going back to a version brings its list back.
The agent job in GitHub Actions or GitLab CI (the sections above) is not a
run of this list: its tools are the `--allow` of `maxpm runner github` or
`maxpm runner gitlab`.

## Actions on a running pipeline

A person or the manager does these; each one goes in the audit log with who
did it.

```
maxpm pipeline hold <name> --reason "<why>"               # every stage: nothing new starts
maxpm pipeline unhold <name>
maxpm pipeline run-stage <name> <stage> [--rev <commit>]  # Run: a stage that starts manually
maxpm pipeline runs <name> <stage>                       # Versions in <stage>: each run there, who and why
maxpm pipeline rerun <name> <stage> --rev <commit> [--reason "<why>"]   # run one stage again, an older version too
maxpm pipeline move-back <name> <stage> --rev <commit> --reason "<why>" # back to a version that passed there
maxpm pipeline move-back <name> <stage> --rev <commit> --reason "<why>" --every-branch  # and each other branch
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

## Versions of the definition

Each change of a pipeline's definition (its environments, stages, commands
and workflows, branches, the mode, window and fault rule of a transition, its
trigger rules with their runners and keys, its runners, its source) makes a
numbered version: the whole definition, who changed it, when, and the change's
own line as the why. A block, a delay, a hold or a freeze is an action of a
moment and makes none. The head of the pipeline's page says "definition v12 ·
who, when"; Versions there lists them, shows what each one changed, and goes
back to one.

```
maxpm pipeline versions <name>                        # newest first: vN, when, who, why
maxpm pipeline versions <name> --show 7               # version 7 as JSON (the form of maxpm pipeline export)
maxpm pipeline versions <name> --compare 7 12         # what changed from 7 to 12
maxpm pipeline versions <name> --back 7 --reason "<why>"
```

`--back` makes one more version, the same as version 7 (version 7 itself never
changes): its environments, stages with their order and branches, the
standing rules of each transition, its trigger rules and its runners. The
source (forge or plugin, its token), the freezes and the keys of its stages
stay as they are now (a key grant or revoke makes a version too, but going
back grants nothing). A person or the manager goes back.

## Emergency actions and operators

Each pipeline has its own emergency actions, on the Emergency tab of its page
and in `maxpm pipeline emergency`. They act on that pipeline only: the other
pipelines go on.

```
maxpm pipeline emergency <name>                                  # what each action acts on now
maxpm pipeline emergency <name> stop --reason "<why>"            # Stop this pipeline
maxpm pipeline emergency <name> resume
maxpm pipeline emergency <name> agents --reason "<why>"          # Stop its agents
maxpm pipeline emergency <name> moveback --stage <stage> --reason "<why>"
maxpm pipeline emergency <name> revoke --key <token or set> --reason "<why>"
maxpm pipeline emergency <name> override --stage <stage> --rev <commit> --reason "<why>"
maxpm pipeline emergency <name> whole --rev <commit>                # Run the whole pipeline for <commit>
maxpm pipeline operators <name> [--add <name>] [--remove <name>] [--agents on|off]
```

- **Stop this pipeline**: every stage is held, so nothing new starts, and a
  running stage stops after its present step (a stage over many servers
  starts no next server). A banner shows the stop until an operator resumes
  it. A plain `hold` lets running work end; a stop does not.
- **Stop its agents**: each agent session that works on the pipeline now (on
  a fault of its stages, or a fix task of one) gets a stop request
  (`maxpm stop`): it commits, hands its task back with a note, and ends.
- **Move back to the last good version**: a stage runs again with the newest
  version that passed there before the one it has, and the transition into
  it is blocked (the same as `move-back`, with the version picked for you).
- **Revoke its keys**: the pipeline stops using a token (the forge or plugin
  token it reads with) or a credential set (its trigger rules give it no more,
  and the grants their tasks hold end). The secret itself stays: a person
  replaces it (`maxpm secret put <name>`) or deletes it. When another pipeline
  uses the same token, the plan and the reply name it, and it keeps the token.

- **Send a version straight to a stage** (the override): a hotfix, or a
  version that passed elsewhere, goes to a later stage now, past the stages
  and transitions between. The plan shows what it skips and what runs in the
  stage: the stage's own steps and checks always run, and the flow goes on
  after it. The skipped stages do not run by themselves; the stage shows
  "not through the pipeline" until they passed for that version. Run them
  later with Run the whole pipeline for <version> (on the stage, or
  `whole`): the version starts at the first stage, and the run ends at the
  stage where the override put it. A stage's own Send a version here
  directly… opens this action with that stage.

Each action needs a reason, and its audit line names who pressed it.

**Operators.** Each pipeline has a list of operators: the people, and agent
sessions, who may press its emergency actions. Only a person changes the list
and the setting "agents may be operators" (a decision for each pipeline,
mark 2026-11-18). An agent on the list acts only while that setting is on. A
pipeline with no operators named: a person or the manager. You are an agent
and not an operator: ask the person with `maxpm ask`, and name the action and
its reason.

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
  actor and time, a search, Correct on each line, and Export as a CSV file), and
  **Runners** (each runner, what it is, its agent CLIs, its load, the
  pipelines that use it; Add a runner and Check; below them the agents on
  runners with Watch, their transcript in words, and Stop).
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
  rules: add, edit, turn off or on; the runners it may use; what they did
  lately), Emergency (its operators; stop the pipeline, stop its agents, move
  a stage back, send a version straight to a stage, revoke a key, each with
  what it acts on first) and its Audit log.
- The red number on the tab counts what waits on the person there.

When you tell the person about a release, name the pipeline and the stage:
they find it there.

When you work on a fault of a stage, or on its fix: keep `maxpm now` current,
because your card under that stage shows it. Ask the person with
`maxpm ask <person> "<question>" --item <your task>`, never in your terminal
alone: your open question shows on your card with Yes and No, and the answer
comes back to you as an answer to the question.

## Later

Each Deploy tab task adds its part to this guide when it is built: the agent
job in GitLab CI and Pulumi Delivery.
