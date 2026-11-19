# MaximizePM pipelines: following each release left to right

A pipeline is the release process of one project: its stages in order, from
the first build to production. MaximizePM keeps the order, the gates between
the stages and the record of each run. The project's own commands, or its
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
- **Gate**: the rule that lets a version into a stage:
  - `auto`: at once, when the stage before passed;
  - `auto after 30m`: when the stage before passed and no check of the
    project (or of the environment's target) failed for that time;
  - `manager`: the manager says yes (it gets one alert);
  - `person` or `person <name>`: a person says yes (a task in What waits for
    you); only a person's yes opens it;
  - `forge`: a push or a tag starts it on the forge.
- **Version**: one commit that goes through the stages. A short and a full
  commit id are one version.
- **Stage run**: one run of a stage for one version: waiting at its gate,
  running, passed, failed, stopped. Its steps, its test results and its last
  lines belong to it.
- **Source**: where the runs come from. With no forge, `maxpm serve` runs
  each stage's command on this computer. With a forge (GitHub, GitLab, Gitea,
  Forgejo, Pulumi Delivery), `maxpm serve` reads the forge's runs once a
  minute with a token, and starts a gated workflow after its gate opens.
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
   maxpm pipeline stage <name> prod --kind deploy --env prod --run "make deploy" --gate "person mark"
   ```
   `--pos <n>` moves a stage; `--remove` removes it (its runs stay). A stage
   with nothing to change is refused, so a typo does not look like a change.
3. Change a gate later: `maxpm pipeline gate <name> <stage> "auto after 30m"`.
4. Look at it: `maxpm pipeline show <name>`, and `maxpm pipeline history
   <name>` for each change with who made it. Keep it in git with `maxpm
   pipeline export <name> --file <path>` (JSON).
5. Send a version in: `maxpm pipeline start <name> [--rev <commit>]` (the
   project folder's HEAD when no commit is named). `maxpm serve` runs each
   stage when its gate opens.

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

- GitHub, Gitea, Forgejo: MaximizePM starts a gated workflow with
  `workflow_dispatch` and the inputs `maxpm_run` and `maxpm_rev`; the
  workflow names its run `maxpm run <id>` (run-name), so the run links to its
  version. A test job uploads its JUnit file as an artifact named `junit*`.
- GitLab: a gated stage plays its manual jobs; the pipeline's test report and
  the environments' deployments are read too.
- Pulumi Delivery: the gate's yes continues the Delivery gate. A roll back is
  done on Pulumi's page.

When the forge refuses the token, the pipeline shows the error, and the
Deploy tab counts it in its red number.

## What happens when a stage fails

- A failed run opens a fault task in the pipeline's project, titled
  `pipeline:<pipeline>:<stage> failed for <version>`, with the failed tests
  and the command that runs the stage again. A later failure of the same
  stage joins that task. It is an operational task: a new session takes it.
- `maxpm pipeline run <run> [--prompt]` prints the run, its failed tests and
  the prompt that analyzes them. `maxpm pipeline fix <run> [--start]` adds a
  fix task from that prompt (`--start`: and a session on it now).
- `maxpm pipeline rerun-failed <run>` runs only the failed tests again (a
  person or the manager).

Fix the cause in the code, and let the pipeline run the version again. Do not
deploy by hand around the pipeline.

## Actions on a running pipeline

A person or the manager does these; each one goes in the audit log with who
did it.

```
maxpm pipeline approve <name> <stage> [--rev <commit>]   # a yes at a manager gate; a person gate takes a person
maxpm pipeline hold <name> [<stage>] --reason "<why>"    # nothing new enters (no stage: nothing new starts)
maxpm pipeline unhold <name> [<stage>]
maxpm pipeline rerun <name> <stage> --rev <commit>       # run one stage again, an older version too
maxpm pipeline stop <run>                                # the command, the forge run, or the wait at the gate
maxpm pipeline rollback <name> <env> --rev <older commit> --reason "<why>"
```

A roll back runs the deploy stage with the older version and holds that
stage's gate, so no newer version comes by itself. End the hold when the cause
is fixed.

To find what a version carries: `maxpm pipeline version <name> <commit>`
(the tasks its commits name). To find where a task runs: `maxpm pipeline item
<id>`.

## The Deploy tab

The person follows all of this on the page's Deploy tab:

- **Pipelines**: one row for each pipeline and each deploy target, its stages
  left to right, the gates between them, and what runs where. Above the rows,
  a line for each version that waits for the person's yes (with Approve) and
  for each token that fails.
- **Where each version runs**, **Accounts** (each forge token and its state),
  **Audit log** (each change and action, with who did it), and the pages
  **Agents** and **Runners**.
- **A pipeline's own page**: Stages (each stage's runs, steps, tests and the
  failure prompt; a target's release card), Versions, Triggers (what happens
  on a failure or a wait), Emergency (hold every stage, stop what runs, put an
  environment back) and its Audit log.
- The red number on the tab counts what waits on the person there.

When you tell the person about a release, name the pipeline and the stage:
they find it there.

## Later

Each Deploy tab task adds its part to this guide when it is built: tiers,
manual starts, controls on the transitions, the holding stage, branches,
operators and the override, trigger rules, runners that start agents with no
person's computer, and a plugin contract that connects any other release
system.
