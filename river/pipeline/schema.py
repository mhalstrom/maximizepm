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
  held_at     TEXT
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

-- Each change of a pipeline's definition, with its actor (#6113). Readers: maxpm pipeline history, the view.
CREATE TABLE IF NOT EXISTS pipeline_changes (
  id           INTEGER PRIMARY KEY,
  pipeline_id  INTEGER NOT NULL,
  at           TEXT NOT NULL,
  actor        TEXT,
  change       TEXT NOT NULL,            -- in words: 'stage prod added: kind deploy, env prod, mode yes mark'
  detail       TEXT,                     -- the stage or environment after the change, as JSON
  reason       TEXT,                     -- why, as the actor said it (#6139)
  until_at     TEXT                      -- a block, delay or freeze: until when
);
CREATE INDEX IF NOT EXISTS pipeline_changes_pipeline ON pipeline_changes(pipeline_id, id);

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
  purpose      TEXT,                     -- rerun, rollback, or failed:<run id> (its failed tests again); none: the flow
  fault_item   INTEGER,                  -- the fault item of MaximizePM that a failed run opened or joined (#6118)
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
"""


def migrate(conn):
    """Add the columns that a queue made by an older version of these tables lacks. core._migrate calls it, inside
    its tx; core.schema_print counts this function, so a change here runs again."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(pipelines)")}
    for c in ("forge", "repo", "secret", "ref", "polled_at", "poll_error", "held_reason", "held_by", "held_at",
              "forge_url"):
        if c not in cols:  # #6119, #6117, #6121
            conn.execute(f"ALTER TABLE pipelines ADD COLUMN {c} TEXT")
    stage_cols = {r["name"] for r in conn.execute("PRAGMA table_info(pipeline_stages)")}
    for c in ("removed_at", "junit", "waves", "check_cmd", "rollback_cmd", "wave_wait"):  # #6113, #6114, #6120
        if c not in stage_cols:
            conn.execute(f"ALTER TABLE pipeline_stages ADD COLUMN {c} TEXT")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(stage_runs)")}
    for c, t in (("gate_note", "TEXT"), ("gate_item", "INTEGER"), ("approved_by", "TEXT"), ("approved_at", "TEXT"),
                 ("runner", "TEXT"), ("purpose", "TEXT"), ("fault_item", "INTEGER"), ("passed_by", "TEXT"),
                 ("passed_at", "TEXT")):  # #6114, #6117, #6118, #6139
        if c not in cols:
            conn.execute(f"ALTER TABLE stage_runs ADD COLUMN {c} {t}")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(pipeline_changes)")}
    for c in ("reason", "until_at"):  # #6139
        if c not in cols:
            conn.execute(f"ALTER TABLE pipeline_changes ADD COLUMN {c} TEXT")
    _gates_to_transitions(conn, stage_cols)


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
