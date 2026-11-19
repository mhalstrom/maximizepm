"""The tables of the pipeline package (#6112). core.SCHEMA ends with this text, so connect() makes them; a new
column of a table that exists goes into core._migrate as a guarded ALTER. Each table names its readers (policy p22):
the pipeline view (#6116), the runner of maxpm serve (#6114), the maxpm pipeline commands (#6113), and the
MaximizePM connection (#6118: fault items, approval cards, a session for failed tests).

The tables hold names of MaximizePM things (a project, a target), never their ids: the package keeps no foreign key
into the rest of the queue."""

SCHEMA = """
-- Pipelines (river/pipeline, #6112). Readers: the view (#6116), the commands and the export (#6113), the runner (#6114).
CREATE TABLE IF NOT EXISTS pipelines (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  project     TEXT,                      -- the MaximizePM project it releases (a name)
  notes       TEXT,
  created_by  TEXT,
  created_at  TEXT NOT NULL,
  forge       TEXT,                      -- where its workflows run: github, or none (#6119)
  repo        TEXT,                      -- owner/name on the forge
  secret      TEXT,                      -- the name of the maxpm secret that holds the forge's API token
  ref         TEXT,                      -- the branch that a started workflow runs on (main when none)
  forge_url   TEXT,                      -- a self-hosted forge's address (GitLab: https://gitlab.com when none, #6121)
  polled_at   TEXT,                      -- the last read of the forge (the adapter reads it every minute)
  poll_error  TEXT,                      -- what the last read said when it failed
  held_reason TEXT,                      -- a hold of every stage: no stage run starts (#6117)
  held_by     TEXT,
  held_at     TEXT,
  plugin_settings TEXT,                  -- a plugin source (#6159): JSON {settings, secrets: {its name: maxpm secret}}
  plugin_state    TEXT,                  -- the JSON state the plugin's last answer kept (a cursor), sent back to it
  stop_runs       INTEGER,               -- 1: an operator stopped the pipeline (#6145): with the hold above, a running
                                         -- stage stops after its present step
  agents_operate  INTEGER,               -- 1: agent sessions on the operator list may press the emergency actions
  runners     TEXT,                      -- JSON list of the runners its trigger rules may use (#6151)
  intake_secret   TEXT,                  -- events in over HTTP (#6163): the maxpm secret whose value a sender shows
  plugin_pin      TEXT,                  -- the kept version of its plugin that it runs (#6166); NULL: the plugin's folder
  permissions     TEXT,                  -- #6154: JSON {allow, ask, never} of its agents' permission rules; NULL: the
                                         -- default (permissions.DEFAULT)
  followed        INTEGER,               -- 1: MaximizePM only follows an outside system's flow (#6167): its stages and
                                         -- versions come from events; no transition, start, move back or override of
                                         -- ours; its trigger rules and hooks still act
  followed_by     TEXT,
  followed_at     TEXT
);

-- The numbered versions of a pipeline's definition (#6143): each change of it makes version n+1 with the whole
-- definition as maxpm pipeline export prints it (less the freezes). Writer: versions.snapshot (define._change,
-- transitions._line); readers: maxpm pipeline versions, the head of the pipeline's page. A line is never changed.
CREATE TABLE IF NOT EXISTS pipeline_def_versions (
  id           INTEGER PRIMARY KEY,
  pipeline_id  INTEGER NOT NULL,
  n            INTEGER NOT NULL,
  at           TEXT NOT NULL,
  actor        TEXT,
  why          TEXT,                     -- the change's line, or 'back to version N: <reason>'
  definition   TEXT NOT NULL,            -- JSON
  UNIQUE (pipeline_id, n)
);

-- The operators of a pipeline (#6145; Decision 7, mark 2026-11-18: "operators can, we audit who does them"): the
-- people, and the agent sessions when pipelines.agents_operate allows, who may press its emergency actions. A pipeline
-- with none: a person or the manager. Readers: operators.check, the Emergency tab.
CREATE TABLE IF NOT EXISTS pipeline_operators (
  id           INTEGER PRIMARY KEY,
  pipeline_id  INTEGER NOT NULL,
  name         TEXT NOT NULL,            -- a person's or an agent session's name in the agents table
  added_by     TEXT,
  added_at     TEXT NOT NULL,
  UNIQUE (pipeline_id, name)
);

-- A place that runs the code: dev, beta, prod, or one server. version_id and since say what runs there now.
-- Readers: the view ('where each version runs'), the runner (a deploy stage names its environment).
CREATE TABLE IF NOT EXISTS pipeline_envs (
  id           INTEGER PRIMARY KEY,
  pipeline_id  INTEGER NOT NULL,
  name         TEXT NOT NULL,
  target       TEXT,                     -- the MaximizePM deploy target of today, when it is one (a name)
  notes        TEXT,
  url          TEXT,
  version_id   INTEGER,                  -- the version that runs there now
  since        TEXT,                     -- since when
  removed_at   TEXT,                     -- an environment.deleted event
  created_at   TEXT NOT NULL,
  UNIQUE (pipeline_id, name)
);

-- The ordered stages of a pipeline. Readers: the view, the runner (the command or workflow, the gate), the export.
CREATE TABLE IF NOT EXISTS pipeline_stages (
  id           INTEGER PRIMARY KEY,
  pipeline_id  INTEGER NOT NULL,
  name         TEXT NOT NULL,
  pos          INTEGER NOT NULL,         -- left to right
  kind         TEXT NOT NULL,            -- build, test, review, approve, deploy, check, other
  run          TEXT,                     -- the project's command that does the work
  workflow     TEXT,                     -- or the forge workflow or job that does it
  env_id       INTEGER,                  -- a deploy stage's environment
  created_by   TEXT,                     -- 'event' when an event named a stage the pipeline did not have
  created_at   TEXT NOT NULL,
  removed_at   TEXT,                     -- a removed stage keeps its runs; adding its name again brings it back
  junit        TEXT,                     -- a test stage: the JUnit XML file its command writes (in the project folder)
  waves        TEXT,                     -- a stage over many servers (#6120): JSON [["web-1"], ["web-2", "web-3"]]
  check_cmd    TEXT,                     -- ... the check of one server after its deploy (MAXPM_HOST)
  rollback_cmd TEXT,                     -- ... puts the version before back on one server (MAXPM_HOST, MAXPM_PREV_REV)
  wave_wait    TEXT,                     -- ... how long the checks of a wave must hold before the next wave
  tier         TEXT,                     -- alpha, beta, prod, or a name the pipeline adds; several stages may share one
                                         -- (#6137). Readers: the stage card, trigger rules (any stage of a tier)
  start        TEXT,                     -- manual: a version that its transition lets in waits for Run (#6138);
                                         -- none: auto, the stage runs by itself
  after        TEXT,                     -- the stages it follows, comma-separated (#6142): two stages after one stage
                                         -- are branches; a stage after two is a join. None: the stage before it
  UNIQUE (pipeline_id, name)
);

-- The transition into each stage (#6139): the arrow from the stage before it, which controls only the flow (the
-- tests and checks run inside the stages). A stage with no row has an automatic arrow with no controls. Each change
-- is a line of pipeline_changes. Readers: the runner (may a waiting version enter: transitions.check), the view and
-- maxpm pipeline show (the arrow: transitions.describe), the export.
CREATE TABLE IF NOT EXISTS pipeline_transitions (
  id           INTEGER PRIMARY KEY,
  pipeline_id  INTEGER NOT NULL,
  stage_id     INTEGER NOT NULL UNIQUE,  -- the stage it lets versions into
  mode         TEXT NOT NULL DEFAULT 'auto',  -- auto, yes, delay, release, forge
  mode_arg     TEXT,                     -- yes: whose (a person's name, or manager; none: any person); delay: the time;
                                         -- release: the cadence (2h), or none
  mode_by      TEXT,
  mode_at      TEXT,
  time_window  TEXT,                     -- when a version may pass: 'mon-thu 09:00-17:00 America/Los_Angeles' (';' between)
  window_by    TEXT,
  window_at    TEXT,
  freezes      TEXT,                     -- JSON [{"from", "to", "reason", "by", "at"}]: no version passes in them
  fault_rule   INTEGER NOT NULL DEFAULT 0,  -- 1: blocked while a fault of the pipeline is open (its own, or ops checks)
  rule_by      TEXT,
  rule_at      TEXT,
  block_reason TEXT,                     -- a block by a person or the manager
  block_by     TEXT,
  block_at     TEXT,
  block_until  TEXT,                     -- ... until this time; none: until someone unblocks it
  block_item   INTEGER,                  -- ... or until this item of the queue is closed
  delay_until  TEXT,                     -- a delay: no version passes before this time
  delay_by     TEXT,
  delay_at     TEXT,
  delay_reason TEXT,
  disabled_reason TEXT,                  -- disabled: no version passes until a person or the manager enables it
  disabled_by  TEXT,
  disabled_at  TEXT
);

-- The '::step' lines of a deploy target's hook run (#6126): its parts with their times, as the hook prints them.
-- hook_run is a row of MaximizePM's hook_runs (an id, as the package keeps no foreign key). Reader: the default
-- pipeline of the target (targets.py: the steps of its build and deploy stages).
CREATE TABLE IF NOT EXISTS hook_steps (
  id           INTEGER PRIMARY KEY,
  hook_run     INTEGER NOT NULL,
  name         TEXT NOT NULL,
  kind         TEXT,
  host         TEXT,
  parallel     INTEGER NOT NULL DEFAULT 0,
  state        TEXT NOT NULL,            -- running, passed, failed, skipped (stopped: the hook ended first, at read)
  started_at   TEXT,
  ended_at     TEXT,
  seconds      REAL,
  note         TEXT
);
CREATE INDEX IF NOT EXISTS hook_steps_run ON hook_steps(hook_run, id);

-- An account of a forge: one token (a maxpm secret) on one forge server, which any number of pipelines name
-- (#6147). What the forge said about it at the last check. Readers: the Accounts view and each pipeline's head
-- (routes/pipeline.py), maxpm pipeline accounts, and accounts.serve_pass (the manager's alerts).
CREATE TABLE IF NOT EXISTS pipeline_accounts (
  id            INTEGER PRIMARY KEY,
  forge         TEXT NOT NULL,           -- github, gitlab, gitea, forgejo, pulumi
  host          TEXT NOT NULL,           -- github.com, gitlab.example.net, api.pulumi.com
  secret        TEXT NOT NULL,           -- the name of the maxpm secret; its value never comes here
  owner         TEXT,                    -- the account the token acts as (the forge's login)
  kind          TEXT,                    -- user, organization, bot, when the forge says
  expires_at    TEXT,                    -- when the token runs out, when the forge says (UTC)
  repos         TEXT,                    -- JSON {repository: true when the token reaches it}
  scopes        TEXT,                    -- what the token may do, when the forge says
  state         TEXT NOT NULL DEFAULT 'new',  -- ok, refused (401/403), error (the check did not get an answer)
  error         TEXT,                    -- the forge's own words at the last failed check
  checked_at    TEXT,
  refused_since TEXT,
  alert_refused TEXT,                    -- when the manager got the alert of this refusal (cleared when it works)
  alert_expiry  TEXT,                    -- the expires_at that the manager got the 7-day alert for
  UNIQUE (forge, host, secret)
);

-- The audit log of a pipeline (#6113, #6144): each change of its definition, each action on it and each change of
-- its flow (a stage's end, a deploy, a fault), with its actor. Readers: maxpm pipeline history and audit, the Deploy
-- tab's Audit log (river/pipeline/audit.py). A line is never changed or deleted (the triggers below): a correction is
-- a new line that names the old one.
CREATE TABLE IF NOT EXISTS pipeline_changes (
  id           INTEGER PRIMARY KEY,
  pipeline_id  INTEGER NOT NULL,
  at           TEXT NOT NULL,
  actor        TEXT,
  change       TEXT NOT NULL,            -- in words: 'stage prod added: kind deploy, env prod, mode yes mark'
  detail       TEXT,                     -- the stage or environment after the change, as JSON
  reason       TEXT,                     -- why, as the actor said it (#6139), or a run's errors
  until_at     TEXT,                     -- a block, delay or freeze: until when
  kind         TEXT,                     -- the actor: person, agent, trigger, hook, forge (none: before #6144)
  action       TEXT,                     -- a short word: define, start, approve, decline, hold, unhold, rerun,
                                         -- rollback, stop, transition, block, unblock, delay, disable, enable,
                                         -- freeze, pass, passed, failed, stopped, deployed, rolled back, fault, fix,
                                         -- correct
  place        TEXT,                     -- what it acts on: 'stage prod', 'the transition test → prod', 'every stage'
  rev          TEXT,                     -- the version
  run_id       INTEGER,                  -- the stage run it started or ended: its state now is the line's result
  result       TEXT,                     -- what came of it, when no run says it
  corrects     INTEGER                   -- a correction: the line it corrects
);
CREATE INDEX IF NOT EXISTS pipeline_changes_pipeline ON pipeline_changes(pipeline_id, id);
CREATE TRIGGER IF NOT EXISTS pipeline_changes_no_update BEFORE UPDATE ON pipeline_changes BEGIN
  SELECT RAISE(ABORT, 'a line of the audit log of a pipeline is never changed: add a correction (maxpm pipeline correct <line> "<text>")');
END;
CREATE TRIGGER IF NOT EXISTS pipeline_changes_no_delete BEFORE DELETE ON pipeline_changes BEGIN
  SELECT RAISE(ABORT, 'a line of the audit log of a pipeline is never deleted: add a correction (maxpm pipeline correct <line> "<text>")');
END;

-- A commit that moves through the stages. Readers: the view (the versions in each stage), the MaximizePM
-- connection (the tasks of a version).
CREATE TABLE IF NOT EXISTS pipeline_versions (
  id           INTEGER PRIMARY KEY,
  pipeline_id  INTEGER NOT NULL,
  rev          TEXT NOT NULL,            -- the commit; a short and a full id of one commit are one version
  ref          TEXT,                     -- a branch or a tag
  title        TEXT,
  created_at   TEXT NOT NULL,
  UNIQUE (pipeline_id, rev)
);

-- One run of a stage for a version (a CDEvents pipelinerun). Readers: the view (each stage's last run, the
-- detail), the runner (what runs now), the MaximizePM connection (a failed run opens a fault item).
CREATE TABLE IF NOT EXISTS stage_runs (
  id           INTEGER PRIMARY KEY,
  pipeline_id  INTEGER NOT NULL,
  stage_id     INTEGER NOT NULL,
  version_id   INTEGER,
  key          TEXT,                     -- the pipelinerun subject id of the event that made it
  state        TEXT NOT NULL,            -- queued, running, passed, failed, cancelled, skipped
  queued_at    TEXT,
  started_at   TEXT,
  ended_at     TEXT,
  url          TEXT,
  host         TEXT,                     -- where it ran
  source       TEXT,                     -- the event source: maxpm, a forge adapter
  errors       TEXT,                     -- the errors of a finished event
  output       TEXT,                     -- the last lines of the stage's output
  gate_note    TEXT,                     -- a queued run: why its gate is closed now (the runner, #6114)
  gate_item    INTEGER,                  -- a person gate: the approval item of MaximizePM that asks for the yes
  approved_by  TEXT,                     -- who gave the yes of the transition's mode (a yes, the release, a delay cut short)
  approved_at  TEXT,
  passed_by    TEXT,                     -- who let this version pass every control of the transition once (#6139)
  passed_at    TEXT,
  runner       TEXT,                     -- <pid>:<token> of the maxpm serve process that runs its command
  purpose      TEXT,                     -- rerun, rollback (a move back), or failed:<run id> (its failed tests again);
                                         -- none: the flow
  sent_by      TEXT,                     -- who sent this version here on purpose (a run again, a move back, #6141)
  reason       TEXT,                     -- ... and why, as they said it
  fault_item   INTEGER,                  -- the fault item of MaximizePM that a failed run opened or joined (#6118)
  outside      TEXT,                     -- an outside system asked at the transition (#6164): its name; it runs the stage
                                         -- itself, so MaximizePM only answers whether the version may enter
  outside_at   TEXT,                     -- ... when it asked last
  outside_said TEXT,                     -- ... the last answer: yes, or no and the kind of control (one audit line each)
  entered_at   TEXT,                     -- a stage that starts manually (#6138): its transition let the version in;
                                         -- the run waits in the stage for Run
  run_by       TEXT,                     -- who pressed Run (maxpm pipeline run-stage)
  run_at       TEXT,
  UNIQUE (pipeline_id, key)
);
CREATE INDEX IF NOT EXISTS stage_runs_state ON stage_runs(state);
CREATE INDEX IF NOT EXISTS stage_runs_stage ON stage_runs(stage_id, id);
CREATE INDEX IF NOT EXISTS stage_runs_version ON stage_runs(version_id);

-- A part of a stage run (a CDEvents taskrun; a '::step' line pair). Readers: the view (steps on a time line).
CREATE TABLE IF NOT EXISTS stage_steps (
  id           INTEGER PRIMARY KEY,
  run_id       INTEGER NOT NULL,
  key          TEXT NOT NULL,            -- the taskrun subject id
  name         TEXT NOT NULL,
  kind         TEXT,                     -- build, test, deploy, check, wait
  host         TEXT,                     -- where=<host>: one step for each server
  parallel     INTEGER NOT NULL DEFAULT 0,
  state        TEXT NOT NULL,            -- running, passed, failed, skipped, stopped (its run ended first)
  started_at   TEXT,
  ended_at     TEXT,
  seconds      REAL,
  note         TEXT,
  output       TEXT,                     -- the last lines printed while it was the newest open step
  UNIQUE (run_id, key)
);

-- A run of a test suite (a CDEvents testsuiterun). Readers: the view (the test view's suites and counts).
CREATE TABLE IF NOT EXISTS test_suites (
  id           INTEGER PRIMARY KEY,
  run_id       INTEGER NOT NULL,
  key          TEXT NOT NULL,
  name         TEXT NOT NULL,
  state        TEXT NOT NULL,            -- running, passed, failed, cancelled
  started_at   TEXT,
  ended_at     TEXT,
  tests        INTEGER NOT NULL DEFAULT 0,
  failed       INTEGER NOT NULL DEFAULT 0,  -- failed and error
  skipped      INTEGER NOT NULL DEFAULT 0,
  seconds      REAL,
  UNIQUE (run_id, key)
);

-- Each test's result in a stage run (a CDEvents testcaserun). Readers: the view (failed first), the MaximizePM
-- connection (the prompt that analyzes the failures).
CREATE TABLE IF NOT EXISTS test_results (
  id           INTEGER PRIMARY KEY,
  run_id       INTEGER NOT NULL,
  suite_id     INTEGER,
  suite        TEXT NOT NULL,            -- the class or suite name
  name         TEXT NOT NULL,
  state        TEXT NOT NULL,            -- passed, failed, error, skipped
  seconds      REAL,
  message      TEXT,
  detail       TEXT,                     -- the failure's text (a stack trace), cut to 4000 characters
  file         TEXT,
  at           TEXT,
  UNIQUE (run_id, suite, name)
);
CREATE INDEX IF NOT EXISTS test_results_state ON test_results(run_id, state);

-- Each version that went to an environment (service deployed, upgraded, rolledback, removed). Readers: the view
-- (the history of an environment), the MaximizePM connection (the watch after a deploy).
CREATE TABLE IF NOT EXISTS pipeline_deploys (
  id           INTEGER PRIMARY KEY,
  env_id       INTEGER NOT NULL,
  version_id   INTEGER,
  run_id       INTEGER,
  kind         TEXT NOT NULL,            -- deployed, upgraded, rolledback, removed
  at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pipeline_deploys_env ON pipeline_deploys(env_id, id);

-- Every event as it came, in the CDEvents form. The records above follow from them. Readers: the view (a run's
-- history), the MaximizePM connection (new events since the last one it read).
CREATE TABLE IF NOT EXISTS pipeline_events (
  id           INTEGER PRIMARY KEY,
  event_id     TEXT NOT NULL,            -- context.id
  source       TEXT NOT NULL,            -- context.source
  type         TEXT NOT NULL,            -- context.type, as dev.cdevents.<subject>.<predicate>.<version>
  at           TEXT NOT NULL,            -- context.timestamp, in UTC
  received_at  TEXT NOT NULL,
  pipeline_id  INTEGER,
  run_id       INTEGER,
  body         TEXT NOT NULL,            -- the whole event as JSON
  UNIQUE (source, event_id)
);
CREATE INDEX IF NOT EXISTS pipeline_events_pipeline ON pipeline_events(pipeline_id, id);
CREATE INDEX IF NOT EXISTS pipeline_events_run ON pipeline_events(run_id, id);

-- The trigger rules of a pipeline (#6150): when a generic event happens (triggers.EVENTS), an agent does what the
-- rule says. Readers: maxpm serve (triggers.serve_pass), maxpm pipeline triggers, the Triggers tab, the export.
CREATE TABLE IF NOT EXISTS pipeline_triggers (
  id           INTEGER PRIMARY KEY,
  pipeline_id  INTEGER NOT NULL,
  event        TEXT NOT NULL,            -- stage-failed, check-failed, transition-opened, run-started, run-ended
  stage        TEXT,                     -- only this stage (a name); none: any stage
  tier         TEXT,                     -- or only the stages of this tier (#6137)
  does         TEXT NOT NULL,            -- what the agent does: the instructions of its task
  command      TEXT,                     -- a hook (#6167): this command runs on the event instead of an agent
  runner       TEXT NOT NULL DEFAULT 'this computer',  -- where the agent runs
  keys         TEXT,                     -- the credential sets its task gets, comma-separated
  keys_by      TEXT,                     -- the person or the manager who named them (vault._may_grant checked)
  enabled      INTEGER NOT NULL DEFAULT 1,
  since        TEXT NOT NULL,            -- it acts on events from this time on (added, or turned on again)
  off_reason   TEXT,
  off_by       TEXT,
  off_at       TEXT,
  created_by   TEXT,
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pipeline_triggers_pipeline ON pipeline_triggers(pipeline_id, id);

-- Each time a rule acted: one row for each (rule, event), so an event starts one agent once. Readers: maxpm serve
-- (what it did already), the Triggers tab (what the rules did lately).
CREATE TABLE IF NOT EXISTS pipeline_trigger_fires (
  id           INTEGER PRIMARY KEY,
  trigger_id   INTEGER NOT NULL,
  pipeline_id  INTEGER NOT NULL,
  key          TEXT NOT NULL,            -- <event>:<stage run id>, or check-failed:<ops_events id>
  event        TEXT NOT NULL,
  at           TEXT NOT NULL,            -- when the event happened
  fired_at     TEXT NOT NULL,
  run_id       INTEGER,
  stage        TEXT,
  rev          TEXT,
  item         INTEGER,                  -- the task of MaximizePM that the agent takes
  result       TEXT,                     -- in words: 'opened fault #12', 'no project to hold the task'
  UNIQUE (trigger_id, key)
);
CREATE INDEX IF NOT EXISTS pipeline_trigger_fires_pipeline ON pipeline_trigger_fires(pipeline_id, id);

-- Runners (#6151, river/runners.py): servers where an agent runs a trigger rule's task with no person, no terminal
-- and no prompt. One list that every pipeline shares; each pipeline ticks the ones it may use (pipelines.runners).
-- Readers: maxpm serve (runners.serve_pass starts and ends the runs), maxpm runner, Deploy > Runners, the Triggers tab
-- (the choices of a rule's runner).
CREATE TABLE IF NOT EXISTS runners (
  name         TEXT PRIMARY KEY,
  ssh          TEXT NOT NULL,            -- the SSH target (Tailscale SSH or a key; never a password prompt)
  system       TEXT,                     -- uname -sm of the server
  version      TEXT,                     -- the maxpm there ('maxpm 1.2.0')
  tailnet      TEXT,                     -- its Tailscale address, when Tailscale runs there
  clis         TEXT,                     -- JSON {"claude": "2.1.3 (Claude Code)", "codex": "codex-cli 0.15"}: found there
  cli          TEXT NOT NULL DEFAULT 'claude',  -- the agent CLI that runs the tasks (runners.CLIS)
  slots        INTEGER NOT NULL DEFAULT 1,      -- runs at the same time
  max_minutes  INTEGER NOT NULL DEFAULT 60,     -- maxpm serve stops a run after this
  max_cost     REAL NOT NULL DEFAULT 5,         -- dollars for one run (Claude Code's --max-budget-usd)
  allow        TEXT,                     -- the tools an agent may use there with no prompt; NULL: runners.ALLOW
  folder       TEXT,                     -- where the projects are cloned there; NULL: runners.FOLDER
  state        TEXT NOT NULL DEFAULT 'new',     -- ok, or bad (error says why)
  error        TEXT,
  checked_at   TEXT,
  added_by     TEXT,
  added_at     TEXT NOT NULL
);

-- Each agent run on a runner: queued when a rule gives it a task, running while its ssh connection lives, then
-- ended, failed or stopped. Readers: maxpm serve, maxpm runner runs and log, Deploy > Runners and Agents, and the
-- command endpoint (POST /api/runner-run: key_hash and agent say which session a command of the runner is).
CREATE TABLE IF NOT EXISTS runner_runs (
  id           INTEGER PRIMARY KEY,
  runner       TEXT NOT NULL,
  item         INTEGER NOT NULL,         -- the task the agent works on
  agent        TEXT NOT NULL,            -- its session record (agents.name)
  pipeline     TEXT,                     -- what started it: the pipeline and its rule
  rule         TEXT,
  rev          TEXT,                     -- the version it starts from (the failed run's), else origin's HEAD
  state        TEXT NOT NULL,            -- queued, running, ended, failed, stopped
  cli          TEXT,
  queued_at    TEXT NOT NULL,
  started_at   TEXT,
  ended_at     TEXT,
  exit_code    INTEGER,
  pid          INTEGER,                  -- the ssh process on this computer
  port         INTEGER,                  -- the port on the runner that leads back to maxpm serve
  key_hash     TEXT,                     -- sha256 of the key that this run's commands carry
  transcript   TEXT,                     -- the file on this computer that holds what the agent printed
  cost         REAL,                     -- dollars, from the transcript (Claude Code's result line)
  result       TEXT,                     -- the agent's last words, or why the run failed
  stopped_by   TEXT,
  stop_reason  TEXT,
  -- #6152: a run in a forge's own CI (runner 'github'): the forge starts the job, MaximizePM reads it in the poll
  forge_key    TEXT,                     -- the failed stage run's key in the forge (gh-run-<id>), which the job names
  job          TEXT,                     -- the forge's run of the agent job (gh-run-<id>)
  url          TEXT,                     -- its page on the forge
  proof        TEXT                      -- JSON: the job's proof (commit, branch, pull request, the test's result)
);
CREATE INDEX IF NOT EXISTS runner_runs_state ON runner_runs(state, runner);
CREATE INDEX IF NOT EXISTS runner_runs_key ON runner_runs(key_hash);
-- #6166: the versions of an installed plugin that MaximizePM keeps (plugins/.versions/<name>/<key> beside the queue)
CREATE TABLE IF NOT EXISTS plugin_versions (
  id           INTEGER PRIMARY KEY,
  name         TEXT NOT NULL,
  key          TEXT NOT NULL,            -- its folder: the version, or <version>+<8 of its digest> when files differ
  version      TEXT NOT NULL,
  digest       TEXT NOT NULL,            -- sha256 of its files (river/pipeline/install.py digest)
  source       TEXT,                     -- the folder or git address it came from (and --ref, --path)
  added_by     TEXT,
  added_at     TEXT NOT NULL,
  UNIQUE (name, key)
);
-- #6166: a person's yes for the exact files of a plugin (its digest), and the one question that asks for it
CREATE TABLE IF NOT EXISTS plugin_trust (
  id           INTEGER PRIMARY KEY,
  name         TEXT NOT NULL,
  digest       TEXT NOT NULL,
  version      TEXT,
  manifest     TEXT,                     -- what the person approved: level, actions, secrets, settings, hosts, run
  item         INTEGER,                  -- the person's item that asks for the yes
  asked_at     TEXT,
  trusted_by   TEXT,                     -- NULL: asked, no yes yet
  trusted_at   TEXT,
  UNIQUE (name, digest)
);

-- Keys for agents with no person (#6153, river/pipeline/keys.py): a credential set (its name in secret_sets) that a
-- person grants to a stage or a tier of a pipeline, or to a runner (the model key). Only those stages read a stage
-- key. Readers: the stage runner (a command's variables), runners.py (a run's variables), triggers (a stage key goes
-- only to a task of its stages), the stage cards, maxpm pipeline keys, maxpm secret grants, the Emergency tab.
CREATE TABLE IF NOT EXISTS pipeline_keys (
  id            INTEGER PRIMARY KEY,
  set_name      TEXT NOT NULL,
  scope         TEXT NOT NULL,           -- stage, tier or runner
  pipeline_id   INTEGER,                 -- the pipeline of a stage or tier; NULL for a runner
  value         TEXT NOT NULL,           -- the stage's name, the tier, or the runner's name
  note          TEXT,
  granted_by    TEXT,
  granted_at    TEXT NOT NULL,
  revoked_by    TEXT,
  revoked_at    TEXT,
  revoke_reason TEXT
);
CREATE INDEX IF NOT EXISTS pipeline_keys_place ON pipeline_keys(pipeline_id, scope, value);
-- A step that a run refused with no prompt (#6154, river/pipeline/permissions.py): the card that asks for it, and the
-- answer. A yes allows that one step (rule) for that task in its next runs. Readers: maxpm serve (permissions.
-- serve_pass: a card done or dropped), runners.py (a run's allowed steps), the Triggers tab, maxpm pipeline permissions.
CREATE TABLE IF NOT EXISTS pipeline_permit_asks (
  id           INTEGER PRIMARY KEY,
  pipeline_id  INTEGER,
  item         INTEGER NOT NULL,         -- the task of the run
  run_id       INTEGER,                  -- the runner run that refused it
  runner       TEXT,
  rule         TEXT NOT NULL,            -- the one step, as a permission rule: Bash(./deploy.sh prod)
  listed       TEXT,                     -- the ask rule that it matched; NULL: on no list
  card         INTEGER,                  -- the person's item that asks
  state        TEXT NOT NULL,            -- asked, yes, no
  asked_at     TEXT NOT NULL,
  decided_by   TEXT,
  decided_at   TEXT
);
CREATE INDEX IF NOT EXISTS pipeline_permit_asks_item ON pipeline_permit_asks(item, state);

-- The names of the keys that the deployment system keeps for a stage itself (GitHub environment secrets, a Pulumi
-- ESC environment), never a value: from a plugin's answer ("secrets") or a person (maxpm pipeline keys --kept-in).
-- Readers: the stage cards and maxpm pipeline keys.
CREATE TABLE IF NOT EXISTS pipeline_kept_keys (
  pipeline_id  INTEGER NOT NULL,
  stage        TEXT NOT NULL,
  kept_in      TEXT NOT NULL,            -- where: 'GitHub environment production'
  names        TEXT NOT NULL,            -- JSON list of the names
  source       TEXT NOT NULL,            -- the plugin's name, or person
  at           TEXT NOT NULL,
  by           TEXT,
  PRIMARY KEY (pipeline_id, stage, kept_in)
);
"""

# The rule each pipeline starts with (#6150): what MaximizePM did for every pipeline before rules (#6118). The
# words of 'does' are triggers.DEFAULT_DOES; schema.py imports nothing, so the text is here and triggers reads it.
DEFAULT_RULE = ("stage-failed", "Find the cause; say whether it is the code, the stage's command or the environment; "
                                "fix it, then run the stage again.")


def migrate(conn):
    """Add the columns that a queue made by an older version of these tables lacks. core._migrate calls it, inside
    its tx; core.schema_print counts this function, so a change here runs again."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(pipelines)")}
    for c in ("forge", "repo", "secret", "ref", "polled_at", "poll_error", "held_reason", "held_by", "held_at",
              "forge_url", "plugin_settings", "plugin_state", "runners", "intake_secret", "plugin_pin", "permissions"):
        if c not in cols:  # #6119, #6117, #6121, #6159; #6151: runners, a JSON list of the runners it may use; #6163;
            # #6166: plugin_pin, the kept version of its plugin (plugin_versions.key) that it runs; NULL: the plugin's folder
            # #6154: permissions, the JSON list of what its agents may do with no prompt
            conn.execute(f"ALTER TABLE pipelines ADD COLUMN {c} TEXT")
    for c in ("stop_runs", "agents_operate", "followed"):  # #6145, #6167
        if c not in cols:
            conn.execute(f"ALTER TABLE pipelines ADD COLUMN {c} INTEGER")
    for c in ("followed_by", "followed_at"):  # #6167
        if c not in cols:
            conn.execute(f"ALTER TABLE pipelines ADD COLUMN {c} TEXT")
    tcols = {r["name"] for r in conn.execute("PRAGMA table_info(pipeline_triggers)")}
    if tcols and "command" not in tcols:  # #6167: a rule that runs a hook command
        conn.execute("ALTER TABLE pipeline_triggers ADD COLUMN command TEXT")
    stage_cols = {r["name"] for r in conn.execute("PRAGMA table_info(pipeline_stages)")}
    for c in ("removed_at", "junit", "waves", "check_cmd", "rollback_cmd", "wave_wait",
              "tier", "start", "after"):  # #6113, #6114, #6120, #6137, #6138, #6142
        if c not in stage_cols:
            conn.execute(f"ALTER TABLE pipeline_stages ADD COLUMN {c} TEXT")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(stage_runs)")}
    for c, t in (("gate_note", "TEXT"), ("gate_item", "INTEGER"), ("approved_by", "TEXT"), ("approved_at", "TEXT"),
                 ("runner", "TEXT"), ("purpose", "TEXT"), ("fault_item", "INTEGER"), ("passed_by", "TEXT"),
                 ("passed_at", "TEXT"), ("sent_by", "TEXT"), ("reason", "TEXT"),
                 ("outside", "TEXT"), ("outside_at", "TEXT"), ("outside_said", "TEXT"),  # #6114, #6117, #6118, #6139, #6141, #6164
                 ("entered_at", "TEXT"), ("run_by", "TEXT"), ("run_at", "TEXT")):  # #6138
        if c not in cols:
            conn.execute(f"ALTER TABLE stage_runs ADD COLUMN {c} {t}")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(pipeline_changes)")}
    for c, t in (("reason", "TEXT"), ("until_at", "TEXT"),  # #6139
                 ("kind", "TEXT"), ("action", "TEXT"), ("place", "TEXT"), ("rev", "TEXT"), ("run_id", "INTEGER"),
                 ("result", "TEXT"), ("corrects", "INTEGER")):  # #6144
        if c not in cols:
            conn.execute(f"ALTER TABLE pipeline_changes ADD COLUMN {c} {t}")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(runner_runs)")}
    for c in ("forge_key", "job", "url", "proof"):  # #6152: an agent job in a forge's own CI
        if c not in cols:
            conn.execute(f"ALTER TABLE runner_runs ADD COLUMN {c} TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS pipeline_changes_run ON pipeline_changes(run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS pipeline_changes_at ON pipeline_changes(at)")
    _gates_to_transitions(conn, stage_cols)
    # #6150, once: each pipeline made before trigger rules gets the rule that acted for every pipeline until then.
    if not conn.execute("SELECT 1 FROM meta WHERE key='pipeline_default_triggers'").fetchone():
        from datetime import datetime, timezone
        at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for p in conn.execute("SELECT id FROM pipelines WHERE id NOT IN (SELECT pipeline_id FROM pipeline_triggers)"
                              ).fetchall():
            conn.execute("INSERT INTO pipeline_triggers(pipeline_id, event, does, since, created_by, created_at) "
                         "VALUES (?,?,?,?,?,?)", (p["id"], *DEFAULT_RULE, at, "maxpm", at))
        conn.execute("INSERT INTO meta(key, value) VALUES ('pipeline_default_triggers', ?)", (at,))


_OLD_MODES = {"manager": ("yes", "manager"), "forge": ("forge", None)}


def _gates_to_transitions(conn, stage_cols):
    """#6139, once: before the transitions, a stage had a gate (pipeline_stages.gate and gate_rule: auto, auto after
    30m, manager, person [name], forge) and a hold (held_reason, held_by, held_at). Each becomes the transition into
    that stage: auto after 30m a delay of 30m, manager the manager's yes, person a person's yes, a hold a block with no
    end. The old columns stay in a queue made before (SQLite keeps them); no code reads them after this."""
    if conn.execute("SELECT 1 FROM meta WHERE key='pipeline_transitions_6139'").fetchone():
        return
    if "gate" in stage_cols:
        held = "held_reason" in stage_cols
        for s in conn.execute("SELECT * FROM pipeline_stages").fetchall():
            gate, rule = s["gate"] or "auto", s["gate_rule"]
            mode, arg = ("delay", rule) if gate == "auto" and rule else ("yes", rule) if gate == "person" else \
                _OLD_MODES.get(gate, ("auto", None))
            block = s["held_reason"] if held else None
            if mode == "auto" and not block:
                continue
            conn.execute("INSERT OR IGNORE INTO pipeline_transitions(pipeline_id, stage_id, mode, mode_arg, mode_by, "
                         "mode_at, block_reason, block_by, block_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (s["pipeline_id"], s["id"], mode, arg, s["created_by"], s["created_at"], block,
                          s["held_by"] if block else None, s["held_at"] if block else None))
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('pipeline_transitions_6139', datetime('now'))")
