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
  created_at  TEXT NOT NULL
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
  gate         TEXT NOT NULL DEFAULT 'auto',  -- who lets a version in: auto, manager, person
  gate_rule    TEXT,                     -- more of the rule, in words ('after beta checks pass for 30m')
  created_by   TEXT,                     -- 'event' when an event named a stage the pipeline did not have
  created_at   TEXT NOT NULL,
  UNIQUE (pipeline_id, name)
);

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
  UNIQUE (pipeline_id, key)
);
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
"""
