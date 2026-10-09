"""MaximizePM core: storage, graph ordering, claims, registry, capacity.

Every public function takes an open connection from `connect()` and returns
plain dicts and lists, so the CLI and the web server share one code path.
"""

from __future__ import annotations

import contextvars
import json
import os
import re
import shutil
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# One database per user, in the home folder (sandboxed agents must be allowed to write there).
# The product: an instructions file that names it has its block.
PRODUCT = "MaximizePM"
COMMAND = "maxpm"
HOME_DB = Path("~/.maximizepm/maxpm.db")

OPEN_STATES = ("open", "in_progress", "held")
CLOSED_STATES = ("done", "dropped")
DOERS = ("any", "ai", "human")
# blocks: wait for it. feeds: wait for it, then read its output. conflicts: no
# order, but never in progress at the same time (both edit the same files).
DEP_KINDS = ("blocks", "feeds", "conflicts")

# Messages. An alert asks for attention now, a question waits for an answer,
# a note is for information, a notice comes from river itself, and an offer
# (help on a blocked item) is accepted or declined.
MESSAGE_KINDS = ("alert", "question", "answer", "note", "notice", "offer")
SEND_KINDS = ("alert", "question", "note")
MESSAGE_STATES = ("open", "accepted", "declined", "answered", "read")
# The level of a message says how soon it wakes the manager's watch (maxpm manage --watch, #1583, #1608): high
# after manage_wait_high, low after manage_wait_low. A message is high unless the sender says low (--level) or
# MaximizePM writes a low one (the ship request notice). A blocked message wakes the watch at once: the sender
# says so (--blocked: its item cannot move until the answer), or MaximizePM sees it (BLOCKED_SIGNS). A person's
# message always wakes it at once. On 2026-10-06 to 08, the sender went on with its item after 78 of 128 alerts
# and questions to the manager (current_project/research/worker-stops.md, #1597).
MESSAGE_LEVELS = ("high", "low")
DEFAULT_LEVEL = "high"
# What messages.blocked holds: who said that the message's item stands still. sender: the flag --blocked. waits:
# the sender then ran maxpm inbox --wait. item: the sender released or blocked the item near the send.
BLOCKED_SIGNS = ("sender", "waits", "item")
# MaximizePM marks an alert or a question to a manager as blocked when its sign comes this long before or after
# the send (#1597: one of the two signs showed 39 of the 50 messages whose item stood still).
BLOCKED_WINDOW = timedelta(minutes=3)
# New findings of these kinds wait manage_wait_low: an agent that waits ends by itself after wait_max, and a
# person gets a notification for an item that is ready for them.
LOW_FINDINGS = ("waiting", "human")

DEFAULT_SETTINGS = {
    "lease_ttl": "30m",
    "hold_ttl": "2h",
    # A goal owner's claim on its goal: how long the ownership lasts without a river command of the owner
    # (each command renews it), and how long its leases on the goal's items last. maxpm goal own --lease 6h
    # picks another length for one ownership. While a goal has an owner, its agent items are reserved for it.
    "goal_lease": "4h",
    # How long an agent may hold an item that waits on a person's item (added with --keep). Then river
    # releases the item (it still waits on the person), the agent takes other work, and the person is reminded.
    "human_wait_max": "30m",
    "owner_ttl": "8h",
    "reserve_ttl": "2h",
    "keep_prereq_limit": "3",
    "replan_threshold": "3",
    "default_prerequisite_mode": "release",
    "max_leases": "1",
    # Items that serve the agent's own outcome count apart, against goal_max_leases: items of a goal it owns,
    # items such an item waits on, and the deploy items of a target it owns. So an owner can take an urgent
    # prerequisite, or run its deploy, while it holds other work.
    "goal_max_leases": "3",
    "away_after": "1h",
    "gone_after": "24h",
    "question_nudge_after": "30m",
    "serve_port": "8765",
    "notify_channels": "",
    "notify_interval": "30s",
    "notify_batch_window": "60s",
    "ntfy_url": "https://ntfy.sh",
    # Where a phone notification opens when river knows no session link for it (a local page cannot open there).
    "ntfy_click": "https://claude.ai/code",
    # The ntfy priority of each notification: min, low, default, high, or urgent. Below high, the ntfy app on
    # Android shows no pop-over banner (the message goes to the notification drawer only), so high is the default.
    "ntfy_priority": "high",
    "ntfy_topic": "",
    "ntfy_token": "",
    "email_to": "",
    "email_from": "",
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_user": "",
    "email_batch_window": "10m",
    "timezone": "",
    "auto_continue": "on",
    # The look of a person at a finished result (a page, a text, a design), for each project (#1668).
    # push_first: the worker pushes, closes the item, and adds a person's item for the look. ask_first: the worker
    # commits, asks in the queue (maxpm ask), and waits for the yes before the push. Under both, public text, a
    # release to production, a step that deletes data or costs money, and an item that says so are asked first.
    # No project gets push_first in silence (#1682): the setup recommends a value (look_setup) and the person
    # confirms it; a project where nobody chose has ask_first.
    "result_look": "ask_first",
    "due_warn_before": "3d",
    # Agents the page can start, as "Label=command" entries separated by ";". The first is the default.
    # "@claude-code" or "@codex" is a launch profile: river builds the command from the platform's options
    # (the settings claude_* and codex_*, below; "@codex sandbox=read-only" sets one for that entry alone).
    # Any other command is custom: {model} and {effort} take the launch dialog's choices, and with no
    # choice the flag before them drops out.
    "launch_agents": "Claude Code=@claude-code",
    # Where a new session opens: a Terminal tab or window (macOS; Windows always opens a console window), or
    # tmux: a pane of the tmux session "maxpm" on any system, and `maxpm view` shows them all side by side.
    # auto (the default): tmux when tmux is installed, else tab. Every start uses it: maxpm launch, the page's
    # Start and Dispatch, and the fresh sessions of maxpm serve.
    "launch_in": "auto",
    # How river reaches a running session through its own platform, so a working agent sees a queue
    # instruction, a stop, or a message at once: "Label=ENV_VAR: command" entries separated by ";". maxpm go
    # records the platform whose ENV_VAR is set in the session's environment, and its value as the address;
    # delivery runs the command with {address} and {message} filled in. The command "uds {address} {message}"
    # is built in: river writes the message as one line to that Unix socket. Claude Code's session inbox
    # (CLAUDE_CODE_MESSAGING_SOCKET, code.claude.com/docs/en/cross-session-messaging) takes such a connection,
    # but its message format is not documented, and a probe did not see the line arrive, so Claude Code is
    # not in the default: add "Claude Code=CLAUDE_CODE_MESSAGING_SOCKET: uds {address} {message}" to try it.
    # A platform without an entry has no native path: the message waits in the queue and the inbox for the
    # agent's next river command.
    "native_message": "Codex=CODEX_THREAD_ID: codex queue --thread {address} --message {message}",
    "setup_done": "off",
    # Review before release: with review on, each deploy item waits on a review item that waits on
    # everything the release ships. review_prompt tells the reviewer what to do (your review process);
    # review_cmd, when set, must exit 0 before maxpm review pass accepts the review.
    # Each project can also keep an ordered list of review steps (maxpm review step add): the review
    # of a release follows the steps of every project it ships, next to review_prompt and review_cmd.
    # Set them globally or on the deploy project (deploy-<target>).
    # The outside tracker a project uses, in words an agent can act on: which tracker, where, and with
    # what tool, e.g. "github owner/shop via gh" or "jira PROJ via the Jira MCP server". Set it per
    # project (maxpm project tracker). River has no tracker API code: agents use their own tools.
    "tracker": "",
    # A session with no work runs maxpm wait in the foreground (a blocking command costs no tokens), never idle at
    # its prompt: it takes new work when it comes, and ends after wait_max without any (0: end at once). One
    # maxpm wait call returns after wait_step, below a shell time limit.
    "wait_max": "45m",
    "wait_step": "9m",
    # The manager (maxpm manage): an agent that waits for work longer than wait_too_long is a finding, and
    # maxpm manage --watch returns on a new finding, or after manage_every with nothing new (it runs as a
    # background command, so the manager's session wakes only then).
    "wait_too_long": "20m",
    # A session the page (or maxpm launch) started that runs no river command within connect_within is a
    # finding (not connected): its agent did not start, or waits on a prompt in its terminal.
    "connect_within": "5m",
    # An agent in a tmux pane that waits on a prompt in its terminal (a permission prompt, a trust question):
    # maxpm serve reads each agent's pane every notify_interval. When one of the last lines on the screen
    # matches prompt_pattern (a regular expression; empty: no watch) and those lines stay the same for
    # prompt_wait, each person gets an alert, with the agent's Terminal on the page. The words are those of
    # Claude Code and Codex; add the words of another CLI or a new version with | (maxpm config set).
    "prompt_pattern": r"Do you (want to|trust)|Would you like to|[Ee]nter to confirm|[❯›]\s*\d+\.\s",
    "prompt_wait": "20s",
    # An agent that ended its turn at its CLI's prompt runs no river command. maxpm serve never types into its
    # pane (a stale session continues a large context); it starts fresh sessions instead. An agent is idle at its
    # prompt when its tmux pane shows the agent CLI with the same screen and no prompt (prompt_pattern) for
    # idle_after, and it ran no river command for idle_after. An agent with an open question to a person (maxpm ask)
    # about an item it holds, or the deploy of a target it owns, is not idle: it waits on that person.
    # fresh_sessions on: work or an answer for an idle agent (a push, an item in its queue, a message, a
    # prerequisite done) goes to a new session: river takes the agent's items back, puts the news in their notes,
    # starts one session for each ready item (the item's model and effort), and ends the idle agent. A person who
    # finishes an item that an agent's item waited on, or answers the question of an agent that ended, also
    # starts a fresh session for that item. off: the news waits in the queue.
    # idle_end: an agent idle at its prompt with nothing in hand ends after this long without a river command
    # (river unregisters it and closes the tmux pane river started for it). 0s: never.
    "idle_after": "2m",
    "fresh_sessions": "on",
    "idle_end": "15m",
    # maxpm serve closes the tmux panes of sessions that are done every tidy_every, as maxpm view --tidy does; a pane
    # it closes by the queue's word (the CLI is still open) must show the same screen for idle_after first. It also
    # ends the tmux servers the tests left behind with their socket gone (maxpm view --orphans). 0s: never.
    "tidy_every": "20m",
    # maxpm serve starts again by itself when the river code on disk is newer than the code it runs (a commit, a
    # pull), so no person restarts it: it looks every notify_interval, and starts again when the files stayed the
    # same for one pass, git shows no change in the code folder that is not committed (an agent in the middle of an
    # edit), and the new code loads. maxpm serve --restart does it at once. off: only a person restarts it.
    "serve_reload": "on",
    # A lease does not run out while its agent is busy. An agent is busy when a command runs in its session (a
    # process below the agent CLI that started after the agent's last river command: a test run, a build), when its
    # tmux pane changes, when its pane shows a prompt for a person, or when its Claude Code transcript has a
    # background command or subagent that has not ended (for busy_max after its start; such a session also keeps
    # its item when news comes for it, #1611). maxpm serve looks every notify_interval and
    # renews the leases of a busy agent; a river command that finds a lease past its time renews it when a command
    # runs in the holder's session. busy_max: how long after an agent's last river command these signs still
    # count (a server that an agent left running would hold an item for ever). 0s: only river commands renew.
    "busy_max": "4h",
    "manage_every": "30m",
    # maxpm manage --watch waits this long after the first new finding, and then returns all new findings at
    # once: they often come in groups (an agent ends, then its lease runs out), and each wake makes the manager
    # read its whole context again (#1312). A finding that goes away in this time wakes nothing. Messages and a
    # stop request still return at once. 0s: return at the first finding.
    "manage_settle": "2m",
    # How long a message to the manager of the level high or low waits before it wakes maxpm manage --watch
    # (#1583, #1608). A message is high (also an alert and a question) unless the sender gives --level low; a
    # ship request notice is low. A blocked message (--blocked, or MaximizePM sees that its item stands still)
    # and a person's message wake the watch at once. The new findings "an agent waits" and "an item is ready
    # for a person" wait manage_wait_low; another finding waits manage_settle. A wake brings every message that
    # waits. On 2026-10-08, 141 of 181 wakes of the manager were for events that could wait. 0s: at once.
    "manage_wait_high": "10m",
    "manage_wait_low": "30m",
    # The manager session's context: Claude Code compacts it at this size (claude --autocompact; 100k to 1M,
    # for example 200k). The session stays the same, with the same name and Remote Control link, and the next
    # maxpm manage briefing shows the queue again. A manager reads its whole context on each wake, so a small
    # context costs less (#1312). auto: Claude Code's own point. A Claude Code launch profile only; an
    # --autocompact in claude_args wins.
    "manager_autocompact": "200k",
    # maxpm cleanup lists a ready item that nobody claimed for this long.
    "stale_after": "14d",
    "review": "off",
    "review_prompt": "",
    "review_cmd": "",
    # maxpm review pass stops a review command or a run step that takes longer than review_timeout, with every
    # process it started (0s: no limit). The reviewer's leases stay renewed while the command runs. Set it on the
    # deploy project (deploy-<target>) when one target's gate is slow.
    "review_timeout": "4h",
    # A hook (maxpm target hook) that runs longer than hook_timeout is stopped, with every process it started
    # (0s: no limit). Set it on the deploy project (deploy-<target>) when the hooks of one target are slow.
    "hook_timeout": "10m",
    # A shell command, run in the release review's folder, that prints the messages of the commits a release ships,
    # for example git log --format=%B <last release>..<pinned commit>. Its environment has MAXPM_LAST_RELEASE (the
    # output of the target's last done deploy item), MAXPM_TARGET, and MAXPM_REVIEW. Every #<id> it prints is an
    # item of the release: whoever claimed or finished it does not review the release (release_authors), and a done
    # one of a project with this target joins the release review and deploy item (maxpm serve). Empty: only the
    # items sent with maxpm ship count.
    "release_commits": "",
    # Releases move with no manager (maxpm serve, every notify_interval). auto_review on: a ready release review
    # that nobody holds or has reserved goes to a session that waits for work in a project the release ships and
    # worked on nothing in it (release_authors), else to a new session. What serve does for a deploy item is each
    # target's deployer mode (maxpm target deployer). max_sessions: serve starts no session by itself for a release
    # while this many agent sessions are live (0: no limit); a person's Start and maxpm launch do not count it.
    "auto_review": "on",
    "max_sessions": "0",
    # Models, weakest to strongest, one list per family ("family: a, b, c", families separated by ";").
    # There is no order across families: a limit compares only models of the same family.
    "model_ladder": "claude: haiku, sonnet, opus, fable; openai: luna, terra, sol, astra",
    "effort_levels": "low, medium, high, xhigh, max",
    # What an item gets when it names none itself. Set them per project (--project) or per item kind
    # (--kind deploy); for example monitors: default_model sonnet, default_effort low, default_max_model sonnet.
    # A recommendation never blocks; min/max limits keep a session whose model is outside them off the item.
    # Goal context sessions (#1104): goal_context on lets maxpm serve start a context session for a goal with
    # two or more ready agent items and no warm base; a worker of the goal starts as a fork of a base that a
    # session used less than base_warm ago (the prompt cache keeps it an hour); a base reads up to base_max tokens.
    "goal_context": "off",
    "base_warm": "50m",
    "base_max": "80k",
    "default_model": "",
    "default_effort": "",
    "default_min_model": "",
    "default_max_model": "",
    # The agent type an item needs, when it names none (maxpm add --agent): a launch platform (codex,
    # claude-code). agent_rules picks one from the item first: "codex: *.css, *.svg, image, logo" gives
    # Codex the items that touch a .css or .svg file or have the word image or logo in the title.
    # default_agent is the fallback; set it per project (--project) or kind (--kind).
    "agent_rules": "",
    "default_agent": "",
}

# Where a new session can open (the setting launch_in).
LAUNCH_INS = ("tab", "window", "tmux")

# Agent platforms river can start from a launch profile ("Label=@claude-code" in launch_agents). Each option
# maps to the CLI's own flags, as `claude --help` and `codex --help` name them. Codex has no Remote Control
# flag for one session (codex remote-control runs a daemon), so it has no remote_control option. Each option
# is also a setting, <prefix><option>, so a project can differ (most specific wins).
LAUNCH_PLATFORMS = {
    "claude-code": {"label": "Claude Code", "exe": "claude", "family": "claude", "prefix": "claude_", "options": {
        "remote_control": {"kind": "toggle", "default": "on",
                           "text": "Remote Control: the session gets a web link that notifications and Open chat use"},
        "permission_mode": {"kind": "choice", "default": "",
                            "choices": ["acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"],
                            "text": "permission mode (--permission-mode); empty: Claude Code's own setting"},
        "model_ids": {"kind": "text", "default": "",
                      "text": "the id Claude Code gets for a model_ladder name (name=id, ...); a name with no "
                              "entry goes as it is: claude --model takes haiku, sonnet, opus, and fable"},
        "args": {"kind": "text", "default": "", "text": "more arguments, put before the prompt"},
        "skill_prompt": {"kind": "toggle", "default": "on",
                         "text": "the maxpm skill in the system prompt (--append-system-prompt-file, with the file "
                                 "args names, if any): each session reads it from the prompt cache, and does not "
                                 "load it again"},
        "prompt": {"kind": "text", "default": "go", "text": "the first prompt"},
    }},
    "codex": {"label": "Codex", "exe": "codex", "family": "openai", "prefix": "codex_", "options": {
        "sandbox": {"kind": "choice", "default": "", "choices": ["read-only", "workspace-write", "danger-full-access"],
                    "text": "sandbox (--sandbox); empty: Codex's own setting. MaximizePM adds --add-dir for the queue folder"},
        "approval": {"kind": "choice", "default": "", "choices": ["on-request", "never"],
                     "text": "when Codex asks before it runs a command (--ask-for-approval); empty: Codex's own setting"},
        # Codex takes full ids, and they change with each OpenAI release: update this list then.
        "model_ids": {"kind": "text", "default": "luna=gpt-6-luna, terra=gpt-5.6-terra, sol=gpt-6.1-sol, astra=gpt-6-astra",
                      "text": "the id Codex gets (codex -m) for a model_ladder name (name=id, ...); "
                              "a name with no entry goes as it is"},
        "args": {"kind": "text", "default": "", "text": "more arguments, put before the prompt"},
        "prompt": {"kind": "text", "default": "run maxpm go in this folder and follow the briefing",
                   "text": "the first prompt (Codex does not read CLAUDE.md)"},
    }},
}
for _pl in LAUNCH_PLATFORMS.values():
    for _o, _spec in _pl["options"].items():
        DEFAULT_SETTINGS[_pl["prefix"] + _o] = _spec["default"]

SCHEMA = """
-- One-time data changes that already ran (key: the change).
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS targets (
  id                INTEGER PRIMARY KEY,
  name              TEXT NOT NULL UNIQUE,
  description       TEXT NOT NULL DEFAULT '',
  monitor           TEXT NOT NULL DEFAULT '',  -- what a session watches after each deploy (maxpm target monitor)
  deployer          TEXT NOT NULL DEFAULT 'launch',  -- what maxpm serve starts for its deploys (maxpm target deployer)
  cadence           TEXT NOT NULL DEFAULT '',  -- how often it releases: 2h, 1d, 1w, 1mo; '' is off (maxpm target cadence)
  owner             TEXT,
  owner_expires_at  TEXT,
  created_at        TEXT NOT NULL
);

-- A command that MaximizePM runs when an event of a target occurs (maxpm target hook): one for each event.
CREATE TABLE IF NOT EXISTS hooks (
  id          INTEGER PRIMARY KEY,
  target_id   INTEGER NOT NULL REFERENCES targets(id),
  event       TEXT NOT NULL,             -- one of HOOK_EVENTS
  command     TEXT NOT NULL,             -- a shell command
  set_by      TEXT,
  set_at      TEXT NOT NULL,
  UNIQUE (target_id, event)
);

-- One row for each event that had a hook: queued in the transaction of the event, run after it (run_hooks).
CREATE TABLE IF NOT EXISTS hook_runs (
  id          INTEGER PRIMARY KEY,
  target      TEXT NOT NULL,
  event       TEXT NOT NULL,
  command     TEXT NOT NULL,             -- the hook as it was when the event occurred
  item_id     INTEGER,                   -- the deploy or the review item of the event (MAXPM_ITEM)
  deploy_id   INTEGER,                   -- the deploy item of the release: its history gets the result
  rev         TEXT,                      -- the revision that the cut of the release pinned (MAXPM_REV)
  actor       TEXT,
  queued_at   TEXT NOT NULL,
  queued_by   TEXT,                      -- the process and thread of the command that caused the event
  started_at  TEXT,
  ended_at    TEXT,
  exit_code   INTEGER,
  timed_out   INTEGER NOT NULL DEFAULT 0,
  seconds     REAL,
  output      TEXT                       -- the last lines
);

CREATE TABLE IF NOT EXISTS projects (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  rank        INTEGER NOT NULL,
  notes       TEXT NOT NULL DEFAULT '',
  path        TEXT,
  target      TEXT,
  archived    INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
  id                INTEGER PRIMARY KEY,
  project_id        INTEGER NOT NULL REFERENCES projects(id),
  title             TEXT NOT NULL,
  notes             TEXT NOT NULL DEFAULT '',
  priority          INTEGER NOT NULL DEFAULT 2 CHECK (priority BETWEEN 0 AND 4),
  rank              REAL NOT NULL,
  doer              TEXT NOT NULL DEFAULT 'any' CHECK (doer IN ('any','ai','human')),
  status            TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open','in_progress','held','done','dropped')),
  blocked_reason    TEXT,
  blocked_at        TEXT,
  blocked_until     TEXT,
  blocked_set_by    TEXT,
  assignee          TEXT,
  claimed_at        TEXT,
  lease_expires_at  TEXT,
  output            TEXT NOT NULL DEFAULT '',
  context           TEXT NOT NULL DEFAULT '',
  touches           TEXT NOT NULL DEFAULT '',
  "check"           TEXT NOT NULL DEFAULT '',
  kind              TEXT NOT NULL DEFAULT 'work',
  target            TEXT,
  reserved_for      TEXT,
  reserved_until    TEXT,
  reserved_by       TEXT,
  hold_expires_at   TEXT,
  held_at           TEXT,
  replan            INTEGER NOT NULL DEFAULT 0,
  late_prereqs      INTEGER NOT NULL DEFAULT 0,
  due               TEXT,
  due_warned        INTEGER NOT NULL DEFAULT 0,
  found_during      INTEGER REFERENCES items(id),
  takeover_by       TEXT,
  takeover_kind     TEXT,
  takeover_note     TEXT,
  takeover_at       TEXT,
  takeover_seen     INTEGER NOT NULL DEFAULT 0,
  needs_check       INTEGER NOT NULL DEFAULT 0,
  fresh_start       TEXT,                     -- maxpm serve starts a fresh session for it (an answer came)
  cut_at            TEXT,                     -- a deploy item: when its release was cut (its list of items is fixed)
  cut_rev           TEXT,                     -- a deploy item: the revision its cut pinned (maxpm target cut --rev)
  model             TEXT,                     -- recommended model (NULL: the default_model setting)
  effort            TEXT,                     -- recommended effort level (NULL: default_effort)
  min_model         TEXT,                     -- hard limits, one model per family, comma list
  max_model         TEXT,
  agent             TEXT,                     -- the agent type that takes it: a launch platform (codex, claude-code)
  created_at        TEXT NOT NULL,
  closed_at         TEXT
);

-- A goal is an outcome in a project with a "done when" test. One agent owns it at a time
-- and creates and takes the items that reach it; items carry goal tags (item_goals).
-- A shared goal has no owner, ever: its items stay open to every agent.
CREATE TABLE IF NOT EXISTS goals (
  id                INTEGER PRIMARY KEY,
  project_id        INTEGER NOT NULL REFERENCES projects(id),
  name              TEXT NOT NULL UNIQUE,
  outcome           TEXT NOT NULL DEFAULT '',
  done_when         TEXT NOT NULL DEFAULT '',
  rank              REAL NOT NULL,
  status            TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','complete')),
  owner             TEXT,
  owner_expires_at  TEXT,
  owner_lease       TEXT,                     -- maxpm goal own --lease; NULL: the goal_lease setting
  shared            INTEGER NOT NULL DEFAULT 0, -- maxpm goal edit --shared: no agent owns it, nothing is reserved
  result            TEXT NOT NULL DEFAULT '',
  created_at        TEXT NOT NULL,
  completed_at      TEXT
);

-- A goal's handoff (#1103): what the goal is, the decisions so far, the files that matter, and what is left.
-- The owner keeps it current; every version stays.
CREATE TABLE IF NOT EXISTS goal_handoffs (
  id          INTEGER PRIMARY KEY,
  goal_id     INTEGER NOT NULL REFERENCES goals(id),
  version     INTEGER NOT NULL,
  text        TEXT NOT NULL,
  by_agent    TEXT,
  created_at  TEXT NOT NULL,
  UNIQUE (goal_id, version)
);

-- A goal's base (#1104): a context session that read the goal's handoff, items and files and then ended.
-- Workers of the goal start as forks of it (claude --resume <session> --fork-session) while it is warm.
CREATE TABLE IF NOT EXISTS goal_bases (
  goal_id     INTEGER PRIMARY KEY REFERENCES goals(id),
  session_id  TEXT NOT NULL,
  model       TEXT,
  path        TEXT NOT NULL,
  tokens      INTEGER,
  by_agent    TEXT,
  ready_at    TEXT NOT NULL,
  used_at     TEXT NOT NULL,
  forks       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS item_goals (
  item_id  INTEGER NOT NULL REFERENCES items(id),
  goal_id  INTEGER NOT NULL REFERENCES goals(id),
  PRIMARY KEY (item_id, goal_id)
);

-- Links to issues in outside trackers (Jira, GitHub Issues, Linear...). MaximizePM stores the link only;
-- agents read and update the tracker with their own tools.
CREATE TABLE IF NOT EXISTS item_refs (
  item_id     INTEGER NOT NULL REFERENCES items(id),
  ref         TEXT NOT NULL,
  url         TEXT,
  created_at  TEXT NOT NULL,
  synced_at   TEXT,
  PRIMARY KEY (item_id, ref)
);

-- An agent's own queue: items it takes before the project queue, and instructions it reads first.
-- Ordered by pos; no expiry and no accept (unlike a push). An item is in at most one queue.
CREATE TABLE IF NOT EXISTS queue_entries (
  id            INTEGER PRIMARY KEY,
  agent         TEXT NOT NULL,
  pos           REAL NOT NULL,
  item_id       INTEGER REFERENCES items(id),
  body          TEXT,                         -- an instruction entry: the text
  kind          TEXT NOT NULL DEFAULT 'item' CHECK (kind IN ('item','message','stop')),
  added_by      TEXT,
  created_at    TEXT NOT NULL,
  delivered_at  TEXT,
  native_status TEXT                          -- native delivery: sent, failed: <why>, or no native channel
);
CREATE UNIQUE INDEX IF NOT EXISTS queue_item ON queue_entries(item_id) WHERE item_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS deps (
  item_id     INTEGER NOT NULL REFERENCES items(id),
  blocked_by  INTEGER NOT NULL REFERENCES items(id),
  kind        TEXT NOT NULL DEFAULT 'blocks',
  auto        INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (item_id, blocked_by),
  CHECK (item_id <> blocked_by)
);

CREATE TABLE IF NOT EXISTS events (
  id       INTEGER PRIMARY KEY,
  item_id  INTEGER REFERENCES items(id),
  at       TEXT NOT NULL,
  actor    TEXT NOT NULL,
  change   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
  name           TEXT PRIMARY KEY,
  kind           TEXT NOT NULL DEFAULT 'ai' CHECK (kind IN ('ai','human')),
  note           TEXT NOT NULL DEFAULT '',
  role           TEXT,
  session        TEXT,
  session_ref    TEXT,
  session_url    TEXT,                       -- web link to the agent's session (Claude Code Remote Control)
  model          TEXT,                       -- the model the session runs (MAXPM_MODEL, maxpm go --model)
  agent_type     TEXT,                       -- the agent CLI it runs in (codex, claude-code), from its environment
  via            TEXT,                       -- 'http': a chat over the local /mcp of maxpm serve
  manage_seen    TEXT,                       -- the manager's findings it has seen (maxpm manage --watch)
  busy_at        TEXT,                       -- when maxpm serve last saw the session busy without a maxpm command (keep_busy)
  pid            INTEGER,                    -- the agent CLI process that runs maxpm, its host, and its command line
  host           TEXT,
  pid_cmd        TEXT,
  platform       TEXT,                       -- the agent CLI (a native_message label) and the session's address in it
  native_address TEXT,
  stop_at        TEXT,                       -- maxpm stop: when, who, and why; the session ends after it
  stop_by        TEXT,
  stop_reason    TEXT,
  registered_at  TEXT NOT NULL,
  last_seen      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  scope  TEXT NOT NULL,
  key    TEXT NOT NULL,
  value  TEXT NOT NULL,
  PRIMARY KEY (scope, key)
);

CREATE TABLE IF NOT EXISTS messages (
  id          INTEGER PRIMARY KEY,
  kind        TEXT NOT NULL
              CHECK (kind IN ('alert','question','answer','note','notice','offer')),
  from_agent  TEXT NOT NULL,
  to_agent    TEXT,
  item_id     INTEGER REFERENCES items(id),
  reply_to    INTEGER REFERENCES messages(id),
  thread_id   INTEGER NOT NULL,
  body        TEXT NOT NULL,
  state       TEXT NOT NULL DEFAULT 'open'
              CHECK (state IN ('open','accepted','declined','answered','read')),
  created_at  TEXT NOT NULL,
  read_at     TEXT,
  closed_at   TEXT,
  nudged_at   TEXT,
  native_status TEXT,
  level       TEXT,  -- low when the sender or MaximizePM said so; NULL: high (DEFAULT_LEVEL)
  blocked     TEXT   -- who said that its item stands still until the answer (BLOCKED_SIGNS); NULL: nobody
);

-- Something needs a person: a human item became ready, or a question or alert went to a human.
CREATE TABLE IF NOT EXISTS needs_you (
  id            INTEGER PRIMARY KEY,
  kind          TEXT NOT NULL CHECK (kind IN ('item','message')),
  item_id       INTEGER REFERENCES items(id),
  message_id    INTEGER REFERENCES messages(id),
  human         TEXT,                       -- NULL: any person
  summary       TEXT NOT NULL,
  opened_at     TEXT NOT NULL,
  closed_at     TEXT,
  close_reason  TEXT
);

-- One row per (event, channel): each event notifies once per channel; a failed send retries.
-- A project's release review, step by step: 'do' is a written instruction the reviewer confirms,
-- 'run' a command that must exit 0 in the project folder. pos orders the steps (1 first).
CREATE TABLE IF NOT EXISTS review_steps (
  id          INTEGER PRIMARY KEY,
  project_id  INTEGER NOT NULL REFERENCES projects(id),
  pos         INTEGER NOT NULL,
  kind        TEXT NOT NULL CHECK (kind IN ('do','run')),
  text        TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
  id          INTEGER PRIMARY KEY,
  event_id    INTEGER NOT NULL REFERENCES needs_you(id),
  channel     TEXT NOT NULL,
  state       TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','sent','failed')),
  attempts    INTEGER NOT NULL DEFAULT 0,
  last_error  TEXT,
  created_at  TEXT NOT NULL,
  sent_at     TEXT,
  UNIQUE (event_id, channel)
);

-- Who held which item when (#1102): the trigger below keeps it for every path that changes an item's
-- status or assignee, so the token usage of a session can be charged to the item its agent held.
CREATE TABLE IF NOT EXISTS holds (
  id          INTEGER PRIMARY KEY,
  item_id     INTEGER NOT NULL,
  agent       TEXT NOT NULL,
  started_at  TEXT NOT NULL,
  ended_at    TEXT
);

CREATE TRIGGER IF NOT EXISTS items_holds AFTER UPDATE OF status, assignee ON items
WHEN (OLD.status IN ('in_progress','held')) <> (NEW.status IN ('in_progress','held'))
     OR OLD.assignee IS NOT NEW.assignee
BEGIN
  UPDATE holds SET ended_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
   WHERE item_id = OLD.id AND ended_at IS NULL
     AND (NEW.status NOT IN ('in_progress','held') OR agent IS NOT NEW.assignee);
  INSERT INTO holds (item_id, agent, started_at)
  SELECT NEW.id, NEW.assignee, strftime('%Y-%m-%dT%H:%M:%SZ','now')
   WHERE NEW.status IN ('in_progress','held') AND NEW.assignee IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM holds WHERE item_id = NEW.id AND agent = NEW.assignee AND ended_at IS NULL);
END;

-- The Claude Code sessions (CLAUDE_CODE_SESSION_ID: the transcript's file name) each agent ran commands in.
CREATE TABLE IF NOT EXISTS agent_sessions (
  agent       TEXT NOT NULL,
  session_id  TEXT NOT NULL,
  first_seen  TEXT NOT NULL,
  PRIMARY KEY (agent, session_id)
);

-- The token usage of each session charged to an item, read from its transcript when the item is done.
-- started: fresh, or fork (the transcript starts with lines copied from another session).
CREATE TABLE IF NOT EXISTS item_usage (
  item_id       INTEGER NOT NULL,
  session_id    TEXT NOT NULL,
  agent         TEXT NOT NULL,
  started       TEXT NOT NULL,
  turns         INTEGER NOT NULL,
  input         INTEGER NOT NULL,
  cache_read    INTEGER NOT NULL,
  write_1h      INTEGER NOT NULL,
  write_5m      INTEGER NOT NULL,
  output        INTEGER NOT NULL,
  peak_context  INTEGER NOT NULL,
  measured_at   TEXT NOT NULL,
  PRIMARY KEY (item_id, session_id)
);

CREATE INDEX IF NOT EXISTS holds_item ON holds(item_id);
CREATE INDEX IF NOT EXISTS holds_agent ON holds(agent);
CREATE INDEX IF NOT EXISTS items_status ON items(status);
CREATE INDEX IF NOT EXISTS deps_blocked_by ON deps(blocked_by);
CREATE INDEX IF NOT EXISTS events_item ON events(item_id);
CREATE INDEX IF NOT EXISTS messages_to ON messages(to_agent, read_at);
CREATE INDEX IF NOT EXISTS messages_item ON messages(item_id);
CREATE INDEX IF NOT EXISTS messages_thread ON messages(thread_id);
"""


class RiverError(Exception):
    """A refusal. The message names the rule and, where possible, the next command."""


class RiverLocked(RiverError):
    """Other commands held the write lock of the queue for longer than a command waits (tx)."""


def names_product(text):
    """True when the text names the product."""
    return PRODUCT in text


# ---------------------------------------------------------------- time


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


_DURATION = re.compile(r"^\s*(\d+)\s*([smhd])\s*$")


def parse_duration(s: str) -> timedelta:
    m = _DURATION.match(s)
    if not m:
        raise RiverError(f"bad duration {s!r}: use a number and s, m, h, or d (for example 30m, 2h, 7d)")
    n, unit = int(m.group(1)), m.group(2)
    return {"s": timedelta(seconds=n), "m": timedelta(minutes=n),
            "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_CLOCK = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$")


def local_zone(name: str = ""):
    """The zone for times a person types and reads: the timezone setting, else the machine's own."""
    if name:
        # UTC needs no time zone database; Windows has none unless the tzdata package is installed.
        if name.upper() in ("UTC", "Z", "GMT", "ETC/UTC"):
            return timezone.utc
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            if not available_timezones():
                raise RiverError(f"time zone {name!r}: this computer has no time zone database (Windows has none "
                                 f"by default). Install it: python -m pip install tzdata; or use UTC")
            raise RiverError(f"unknown time zone {name!r}: use a name such as America/New_York or UTC")
    return datetime.now().astimezone().tzinfo


def parse_when(s: str, zone: str = "", start: datetime | None = None) -> datetime:
    """A future moment from what a person types, in UTC.

    Accepts a duration ('2h', '3d'), an ISO time ('2026-09-28T07:00', with Z or an
    offset, else in the zone), or '[day] [time] [zone]' where day is today,
    tomorrow, a weekday (mon .. sun, the next one), or YYYY-MM-DD, time is 7,
    07:00, or 7am, and zone is a name such as America/New_York. A weekday or a
    time alone means the next such moment; a day alone means its midnight."""
    start = start or now()
    text = s.strip()
    if _DURATION.match(text):
        return start + parse_duration(text)
    words = text.split()
    if words and "/" in words[-1] or (words and words[-1].upper() in ("UTC", "Z")):
        zone, words = ("UTC" if words[-1].upper() in ("UTC", "Z") else words[-1]), words[:-1]
    tz = local_zone(zone)
    if len(words) == 1 and "T" in words[0]:
        try:
            t = datetime.fromisoformat(words[0].replace("Z", "+00:00"))
        except ValueError:
            raise RiverError(f"bad time {s!r}: use for example 2026-09-28T07:00")
        t = t if t.tzinfo else t.replace(tzinfo=tz)
        return t.astimezone(timezone.utc).replace(microsecond=0)
    here = start.astimezone(tz)
    day = weekday = clock = None
    for w in words:
        lw = w.lower()
        if lw in ("today", "tomorrow"):
            day = here.date() + timedelta(days=1 if lw == "tomorrow" else 0)
        elif lw[:3] in WEEKDAYS and lw in (WEEKDAYS[WEEKDAYS.index(lw[:3])], _DAY_NAMES[WEEKDAYS.index(lw[:3])]):
            weekday = WEEKDAYS.index(lw[:3])
        elif re.match(r"^\d{4}-\d{2}-\d{2}$", lw):
            day = datetime.strptime(lw, "%Y-%m-%d").date()
        elif _CLOCK.match(lw):
            m = _CLOCK.match(lw)
            h, mi = int(m.group(1)), int(m.group(2) or 0)
            if m.group(3):
                h = h % 12 + (12 if m.group(3) == "pm" else 0)
            if h > 23 or mi > 59:
                raise RiverError(f"bad time of day {w!r}")
            clock = (h, mi)
        else:
            raise RiverError(f"bad time {s!r}: use a duration (2h), an ISO time (2026-09-28T07:00), "
                             f"or a day and time such as 'mon 07:00 America/New_York'")
    if day is None and weekday is None and clock is None:
        raise RiverError(f"bad time {s!r}: give a day, a time of day, or both")
    h, mi = clock or (0, 0)

    def at(d):
        return datetime(d.year, d.month, d.day, h, mi, tzinfo=tz)

    if day is not None:
        t = at(day)
    elif weekday is not None:
        t = at(here.date() + timedelta(days=(weekday - here.weekday()) % 7))
        if t <= here:
            t += timedelta(days=7)
    else:
        t = at(here.date())
        if t <= here:
            t = at(here.date() + timedelta(days=1))
    return t.astimezone(timezone.utc)


def parse_due(s: str | None, zone: str = "") -> str | None:
    """A due date as stored (UTC ISO), or None for 'none' / ''. A date alone means the end of that day."""
    if s is None or s.strip().lower() in ("", "none"):
        return None
    words = s.split()
    if words and re.match(r"^\d{4}-\d{2}-\d{2}$", words[0]) and not any(_CLOCK.match(w.lower()) for w in words[1:]):
        words.insert(1, "23:59")  # "due 2026-10-15" means by the end of that day
    return iso(parse_when(" ".join(words), zone))


def show_time(iso_s: str | None, zone: str = "") -> str:
    """'Mon 7:00 EDT' for a stored UTC time, in the zone; the date too when it is more than six days away."""
    if not iso_s:
        return ""
    tz = local_zone(zone)
    t = parse_iso(iso_s).astimezone(tz)
    day = t.strftime("%a") if abs((t - now().astimezone(tz)).days) < 6 else t.strftime("%a %Y-%m-%d")
    return f"{day} {t.hour}:{t.minute:02d} {t.strftime('%Z')}".strip()


# ---------------------------------------------------------------- connection

def db_path() -> Path:
    """MAXPM_DB, else ~/.maximizepm/maxpm.db."""
    if os.environ.get("MAXPM_DB"):
        return Path(os.environ["MAXPM_DB"]).expanduser()
    return HOME_DB.expanduser()


def queue_note():
    """When MAXPM_DB points away from the main queue: one line that says which queue this is; else ""."""
    if not os.environ.get("MAXPM_DB"):
        return ""
    p, home = db_path().resolve(), HOME_DB.expanduser().resolve()
    return "" if p == home else f"QUEUE: {p} (set by MAXPM_DB), not the main queue {home}"


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path) if path else db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    _migrate_launch_agents(conn)
    _migrate_levels(conn)
    return conn


def _migrate(conn):
    if "parent_id" not in {r["name"] for r in conn.execute("PRAGMA table_info(goals)")}:
        conn.execute("ALTER TABLE goals ADD COLUMN parent_id INTEGER REFERENCES goals(id)")
    if "no_goal_context" not in {r["name"] for r in conn.execute("PRAGMA table_info(items)")}:
        conn.execute("ALTER TABLE items ADD COLUMN no_goal_context INTEGER NOT NULL DEFAULT 0")
    # Items held before the holds table existed: their hold starts at the claim.
    conn.execute("INSERT INTO holds (item_id, agent, started_at) SELECT i.id, i.assignee, COALESCE(i.claimed_at, ?) "
                 "FROM items i WHERE i.status IN ('in_progress','held') AND i.assignee IS NOT NULL AND NOT EXISTS "
                 "(SELECT 1 FROM holds h WHERE h.item_id=i.id AND h.agent=i.assignee AND h.ended_at IS NULL)",
                 (iso(now()),))
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(projects)")}
    if "path" not in cols:
        conn.execute("ALTER TABLE projects ADD COLUMN path TEXT")
    if "target" not in cols:
        conn.execute("ALTER TABLE projects ADD COLUMN target TEXT")
    if "nudged_at" not in {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}:
        conn.execute("ALTER TABLE messages ADD COLUMN nudged_at TEXT")
    acols = {r["name"] for r in conn.execute("PRAGMA table_info(agents)")}
    if "role" not in acols:
        conn.execute("ALTER TABLE agents ADD COLUMN role TEXT")
    if "session" not in acols:
        conn.execute("ALTER TABLE agents ADD COLUMN session TEXT")
    if "session_ref" not in acols:
        conn.execute("ALTER TABLE agents ADD COLUMN session_ref TEXT")
    if "waiting_since" not in acols:
        conn.execute("ALTER TABLE agents ADD COLUMN waiting_since TEXT")
    if "waiting_in" not in acols:
        conn.execute("ALTER TABLE agents ADD COLUMN waiting_in TEXT")
    if "session_url" not in acols:
        conn.execute("ALTER TABLE agents ADD COLUMN session_url TEXT")
    if "via" not in acols:
        conn.execute("ALTER TABLE agents ADD COLUMN via TEXT")
    if "model" not in acols:
        conn.execute("ALTER TABLE agents ADD COLUMN model TEXT")
    if "agent_type" not in acols:
        conn.execute("ALTER TABLE agents ADD COLUMN agent_type TEXT")
    for col in ("stop_at", "stop_by", "stop_reason", "platform", "native_address", "host", "pid_cmd", "manage_seen", "busy_at"):
        if col not in acols:
            conn.execute(f"ALTER TABLE agents ADD COLUMN {col} TEXT")
    if "pid" not in acols:
        conn.execute("ALTER TABLE agents ADD COLUMN pid INTEGER")
    if "auto" not in {r["name"] for r in conn.execute("PRAGMA table_info(deps)")}:
        conn.execute("ALTER TABLE deps ADD COLUMN auto INTEGER NOT NULL DEFAULT 0")
    icols = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
    for col in ("context", "touches", "check"):
        if col not in icols:
            conn.execute(f"ALTER TABLE items ADD COLUMN \"{col}\" TEXT NOT NULL DEFAULT ''")
    if "kind" not in icols:
        conn.execute("ALTER TABLE items ADD COLUMN kind TEXT NOT NULL DEFAULT 'work'")
    if "target" not in icols:
        conn.execute("ALTER TABLE items ADD COLUMN target TEXT")
    if "reserved_until" not in icols:
        conn.execute("ALTER TABLE items ADD COLUMN reserved_until TEXT")
        conn.execute("ALTER TABLE items ADD COLUMN reserved_by TEXT")
    if "takeover_by" not in icols:
        for col, typ in (("takeover_by", "TEXT"), ("takeover_kind", "TEXT"), ("takeover_note", "TEXT"),
                         ("takeover_at", "TEXT"), ("takeover_seen", "INTEGER NOT NULL DEFAULT 0")):
            conn.execute(f"ALTER TABLE items ADD COLUMN {col} {typ}")
    if "found_during" not in icols:
        conn.execute("ALTER TABLE items ADD COLUMN found_during INTEGER REFERENCES items(id)")
    if "due" not in icols:
        conn.execute("ALTER TABLE items ADD COLUMN due TEXT")
        conn.execute("ALTER TABLE items ADD COLUMN due_warned INTEGER NOT NULL DEFAULT 0")
    if "cut_at" not in icols:
        conn.execute("ALTER TABLE items ADD COLUMN cut_at TEXT")
    if "cut_rev" not in icols:
        conn.execute("ALTER TABLE items ADD COLUMN cut_rev TEXT")
    if "late_prereqs" not in icols:
        conn.execute("ALTER TABLE items ADD COLUMN late_prereqs INTEGER NOT NULL DEFAULT 0")
    for col in ("blocked_at", "blocked_until", "blocked_set_by"):
        if col not in icols:
            conn.execute(f"ALTER TABLE items ADD COLUMN {col} TEXT")
    for col in ("model", "effort", "min_model", "max_model", "agent"):
        if col not in icols:
            conn.execute(f"ALTER TABLE items ADD COLUMN {col} TEXT")
    if "needs_check" not in icols:
        conn.execute("ALTER TABLE items ADD COLUMN needs_check INTEGER NOT NULL DEFAULT 0")
    if "native_status" not in {r["name"] for r in conn.execute("PRAGMA table_info(queue_entries)")}:
        conn.execute("ALTER TABLE queue_entries ADD COLUMN native_status TEXT")
    if "native_status" not in {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}:
        conn.execute("ALTER TABLE messages ADD COLUMN native_status TEXT")
    if "level" not in {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}:
        conn.execute("ALTER TABLE messages ADD COLUMN level TEXT")
    if "blocked" not in {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}:
        conn.execute("ALTER TABLE messages ADD COLUMN blocked TEXT")
    if "fresh_start" not in icols:
        conn.execute("ALTER TABLE items ADD COLUMN fresh_start TEXT")
        conn.execute("DELETE FROM settings WHERE key='wake_after'")  # the pane wake of #729 is gone (#758)
    if "synced_at" not in {r["name"] for r in conn.execute("PRAGMA table_info(item_refs)")}:
        conn.execute("ALTER TABLE item_refs ADD COLUMN synced_at TEXT")
    if "reserved_for" not in icols:
        conn.execute("ALTER TABLE items ADD COLUMN reserved_for TEXT")
        conn.execute("ALTER TABLE items ADD COLUMN hold_expires_at TEXT")
        conn.execute("ALTER TABLE items ADD COLUMN replan INTEGER NOT NULL DEFAULT 0")
    if "monitor" not in {r["name"] for r in conn.execute("PRAGMA table_info(targets)")}:
        conn.execute("ALTER TABLE targets ADD COLUMN monitor TEXT NOT NULL DEFAULT ''")
    if "deployer" not in {r["name"] for r in conn.execute("PRAGMA table_info(targets)")}:
        conn.execute("ALTER TABLE targets ADD COLUMN deployer TEXT NOT NULL DEFAULT 'launch'")
    if "cadence" not in {r["name"] for r in conn.execute("PRAGMA table_info(targets)")}:
        conn.execute("ALTER TABLE targets ADD COLUMN cadence TEXT NOT NULL DEFAULT ''")
    if "owner_lease" not in {r["name"] for r in conn.execute("PRAGMA table_info(goals)")}:
        conn.execute("ALTER TABLE goals ADD COLUMN owner_lease TEXT")
    if "shared" not in {r["name"] for r in conn.execute("PRAGMA table_info(goals)")}:
        conn.execute("ALTER TABLE goals ADD COLUMN shared INTEGER NOT NULL DEFAULT 0")
    if "held_at" not in {r["name"] for r in conn.execute("PRAGMA table_info(items)")}:
        conn.execute("ALTER TABLE items ADD COLUMN held_at TEXT")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='aliases'").fetchone():
        conn.execute("DROP TABLE aliases")  # the old names of renamed projects and targets are gone (#810)


LOCK_TRIES = 3  # a write waits busy_timeout (10s) this many times for the lock of the queue


class tx:
    """BEGIN IMMEDIATE ... COMMIT, so two writers never interleave a claim. When other commands hold the write
    lock longer than busy_timeout, it tries again (nothing of this transaction ran yet), `tries` times in all;
    then it raises RiverLocked, a refusal with the next step, not a traceback (#1615). A command that polls
    passes tries=1 and skips that one write."""

    def __init__(self, conn: sqlite3.Connection, tries=None, claims=False):
        self.conn = conn
        self.tries = tries or LOCK_TRIES
        self.claims = claims  # the transaction claims an item (still_worked asks for the process list)
        self.procs = None

    def __enter__(self):
        # The process list that a sweep or a claim of this transaction reads (busy_now) is taken here, before
        # the lock: no other command waits for the lock while ps runs (#1617).
        self.procs = _PROCS.set(_processes_for_lock(self.conn, self.claims))
        for _ in range(self.tries):
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                return self.conn
            except sqlite3.OperationalError as e:
                if "database is locked" not in str(e):
                    _PROCS.reset(self.procs)
                    raise
        _PROCS.reset(self.procs)
        _log_locked(self.conn, self.tries)
        raise RiverLocked("the queue is busy: other maxpm commands held its database for longer than this "
                          "command waits; nothing changed. Run the command again")

    def __exit__(self, exc_type, exc, tb):
        _PROCS.reset(self.procs)
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


# The text of one ps call for the transaction that runs now (tx), or None: busy_now then asks ps itself.
_PROCS: contextvars.ContextVar[str | None] = contextvars.ContextVar("maxpm_procs", default=None)


def _processes_for_lock(conn, claims=False):
    """The process list for the transaction that starts now, when it will ask which sessions run a command: a
    lease ran out (the sweep keeps the item of a busy agent), or, in a transaction that claims, an item is open
    whose lease ran out and whose agent may still work on it (still_worked). None when nothing asks, or when
    the queue cannot say yet (a database of an older version, before its columns are added)."""
    try:
        if not (conn.execute("SELECT 1 FROM items WHERE status='in_progress' AND lease_expires_at < "
                             "strftime('%Y-%m-%dT%H:%M:%SZ','now') LIMIT 1").fetchone()
                or (claims and _lease_lost(conn))):
            return None
    except sqlite3.Error:
        return None
    return _process_list() or ""


def _log_locked(conn, tries):
    """One line in locked.log beside the queue for each write that gave up, so a person can count how often the
    lock is too busy (#1617). Never an error of its own."""
    try:
        path = conn.execute("PRAGMA database_list").fetchone()[2]
        if path:
            with open(Path(path).parent / "locked.log", "a") as f:
                f.write(f"{iso(now())} tries={tries} pid={os.getpid()} {' '.join(sys.argv[1:4])}\n")
    except Exception:
        pass


# How the running command reached MaximizePM, when not directly: "relay" while maxpm serve answers an action
# from the page shown through the relay (relay.handle_page). _event then records the actor as <name>@relay.
EVENT_VIA: contextvars.ContextVar[str | None] = contextvars.ContextVar("maxpm_event_via", default=None)
# 'http' while mcp.Server runs a command for a chat over the local /mcp of maxpm serve. The environment and the
# parent processes are maxpm serve's, not the chat's, so go records no agent type, model, process, or native
# address from them, and records via='http'.
HTTP_CHAT: contextvars.ContextVar[str | None] = contextvars.ContextVar("maxpm_http_chat", default=None)


def set_via(conn, name, via):
    """Record how a chat session reaches MaximizePM (HTTP_CHAT); the page and maxpm who show it."""
    with tx(conn):
        conn.execute("UPDATE agents SET via=? WHERE name=? AND via IS NOT ?", (via, name, via))


def _event(conn, item_id, actor, change):
    if actor and actor != "maxpm" and conn.execute(
            "SELECT 1 FROM agents WHERE name=? AND role='manager'", (actor,)).fetchone():
        change += f" (by manager {actor})"
    via = EVENT_VIA.get()
    if via and actor and not actor.endswith(f"@{via}"):
        actor = f"{actor}@{via}"  # a command that came through the relay (mcp.Server.via): the history says so
    conn.execute("INSERT INTO events(item_id, at, actor, change) VALUES (?,?,?,?)",
                 (item_id, iso(now()), actor or "?", change))


# ---------------------------------------------------------------- settings

def setting(conn, key: str, item_id: int | None = None, agent: str | None = None,
            project_id: int | None = None, kind: str | None = None) -> str:
    """Most specific value wins: item, agent, item kind, project, global, built-in."""
    if key not in DEFAULT_SETTINGS:
        raise RiverError(f"unknown setting {key!r}; known: {', '.join(sorted(DEFAULT_SETTINGS))}")
    if item_id is not None and (project_id is None or kind is None):
        r = conn.execute("SELECT project_id, kind FROM items WHERE id=?", (item_id,)).fetchone()
        if r:
            project_id = r["project_id"] if project_id is None else project_id
            kind = r["kind"] if kind is None else kind
    scopes = []
    if item_id is not None:
        scopes.append(f"item:{item_id}")
    if agent:
        scopes.append(f"agent:{agent}")
    if kind:
        scopes.append(f"kind:{kind}")
    if project_id is not None:
        r = conn.execute("SELECT name FROM projects WHERE id=?", (project_id,)).fetchone()
        if r:
            scopes.append(f"project:{r['name']}")
    scopes.append("global")
    for sc in scopes:
        r = conn.execute("SELECT value FROM settings WHERE scope=? AND key=?", (sc, key)).fetchone()
        if r:
            return r["value"]
    return DEFAULT_SETTINGS[key]


def _scope(conn, project=None, item=None, agent=None, kind=None) -> str:
    given = [x for x in (project, item, agent, kind) if x is not None]
    if len(given) > 1:
        raise RiverError("give at most one of --project, --item, --agent, --kind")
    if kind is not None:
        if not re.match(r"^[a-z][a-z0-9_-]*$", kind):
            raise RiverError("--kind is an item kind, for example work, deploy, review")
        return f"kind:{kind}"
    if project is not None:
        return f"project:{_project(conn, project)['name']}"
    if item is not None:
        _item(conn, item)
        return f"item:{item}"
    if agent is not None:
        return f"agent:{agent}"
    return "global"


def config_set(conn, key, value, project=None, item=None, agent=None, actor=None, kind=None):
    if key not in DEFAULT_SETTINGS:
        raise RiverError(f"unknown setting {key!r}; known: {', '.join(sorted(DEFAULT_SETTINGS))}")
    if key.endswith(("_ttl", "_after", "_before", "_interval", "_window")) or key in (
            "wait_max", "wait_step", "human_wait_max", "goal_lease", "wait_too_long", "manage_every", "manage_settle", "manage_wait_high", "manage_wait_low", "connect_within",
            "prompt_wait", "idle_end", "tidy_every", "busy_max", "review_timeout", "hook_timeout"):
        parse_duration(value)
    elif key == "prompt_pattern":
        try:
            re.compile(value)
        except re.error as e:
            raise RiverError(f"prompt_pattern is a regular expression, and this one has an error: {e}")
    elif key in ("keep_prereq_limit", "replan_threshold", "max_leases", "goal_max_leases", "serve_port", "smtp_port",
                 "max_sessions"):
        if not value.isdigit():
            raise RiverError(f"{key} takes a whole number")
    elif key == "launch_agents":
        parse_launch_agents(value)
    elif _platform_setting(key):
        _check_option(*_platform_setting(key), value)
    elif key == "model_ladder":
        parse_ladder(value)
    elif key == "agent_rules":
        parse_agent_rules(value)
    elif key == "default_agent" and value:
        _check_agent_type(conn, value)
    elif key == "native_message":
        parse_native(value)
    elif key == "effort_levels":
        if not _levels(value):
            raise RiverError("effort_levels is a comma list, lowest first: low, medium, high, xhigh, max")
    elif key == "default_effort" and value:
        _check_effort(conn, value)
    elif key == "default_model" and value:
        _check_model_name(value)
    elif key in ("default_min_model", "default_max_model") and value:
        _limit_list(parse_ladder(setting(conn, "model_ladder")), value, key)
    elif key in ("review", "auto_review") and value not in ("on", "off"):
        raise RiverError(f"{key} is on or off")
    elif key == "setup_done" and value not in ("on", "off"):
        raise RiverError("setup_done is on or off")
    elif key == "launch_in" and value not in ("auto", *LAUNCH_INS):
        raise RiverError("launch_in is auto, tab, window, or tmux")
    elif key == "auto_continue" and value not in ("on", "off"):
        raise RiverError("auto_continue is on or off")
    elif key == "result_look" and value not in ("ask_first", "push_first"):
        raise RiverError("result_look is ask_first or push_first")
    elif key in ("fresh_sessions", "serve_reload") and value not in ("on", "off"):
        raise RiverError(f"{key} is on or off")
    elif key == "manager_autocompact":
        autocompact_tokens(value)
    elif key == "default_prerequisite_mode" and value not in ("keep", "release"):
        raise RiverError("default_prerequisite_mode is keep or release")
    elif key in ("email_to", "email_from") and value and not all(
            re.match(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$", x.strip()) for x in value.split(",")):
        raise RiverError(f"{key} is an email address" + (" (a comma list is fine)" if key == "email_to" else ""))
    elif key == "ntfy_click" and value and not re.match(r"^https?://[^\s/]+", value):
        raise RiverError("ntfy_click is a web link, for example https://claude.ai/code")
    elif key == "ntfy_priority" and value not in NTFY_PRIORITIES:
        raise RiverError("ntfy_priority is one of: " + ", ".join(NTFY_PRIORITIES)
                         + " (high and urgent show a banner on the phone)")
    elif key == "ntfy_url" and not re.match(r"^https?://[^\s/]+", value):
        raise RiverError("ntfy_url is the server address, for example https://ntfy.sh")
    elif key == "ntfy_topic" and value and not re.match(r"^[A-Za-z0-9_-]{1,64}$", value):
        raise RiverError("ntfy_topic uses letters, digits, '-' and '_' (up to 64); maxpm notify setup ntfy makes one")
    elif key == "notify_channels" and not all(re.match(r"^[a-z0-9_-]+$", c) for c in _channels(value)):
        raise RiverError("notify_channels is a comma list of channel names, for example: mac,ntfy (empty sends nothing)")
    sc = _scope(conn, project, item, agent, kind)
    with tx(conn):
        conn.execute("INSERT INTO settings(scope,key,value) VALUES (?,?,?) "
                     "ON CONFLICT(scope,key) DO UPDATE SET value=excluded.value", (sc, key, value))
        _event(conn, None, actor, f"setting {sc} {key}={mask(key, value)}")
    return {"scope": sc, "key": key, "value": mask(key, value)}


def config_unset(conn, key, project=None, item=None, agent=None, actor=None, kind=None):
    sc = _scope(conn, project, item, agent, kind)
    with tx(conn):
        conn.execute("DELETE FROM settings WHERE scope=? AND key=?", (sc, key))
        _event(conn, None, actor, f"setting {sc} {key} unset")
    return {"scope": sc, "key": key}


# Anyone who knows these can read or send the notifications; config output shows only their end.
NTFY_PRIORITIES = ("min", "low", "default", "high", "urgent")
SECRET_SETTINGS = ("ntfy_topic", "ntfy_token")


def mask(key, value):
    if key in SECRET_SETTINGS and value:
        return "…" + value[-4:] if len(value) > 8 else "…"
    return value


def config_list(conn):
    rows = [dict(r) for r in conn.execute("SELECT scope,key,value FROM settings ORDER BY scope,key")]
    for r in rows:
        r["value"] = mask(r["key"], r["value"])
    return {"defaults": DEFAULT_SETTINGS, "overrides": rows}


# ---------------------------------------------------------------- models

MODEL_FIELDS = ("model", "effort", "min_model", "max_model", "agent")


def parse_ladder(value):
    """model_ladder: {family: [models weakest first]}. "claude: sonnet, opus; openai: luna, sol"."""
    fams, seen = {}, set()
    for n, part in enumerate(x for x in value.split(";") if x.strip()):
        name, _, models = part.rpartition(":")
        name = name.strip().lower() or f"family{n + 1}"
        ms = _levels(models)
        if not ms:
            raise RiverError(f"model_ladder: family {name} lists no models; write "
                             f"\"claude: haiku, sonnet, opus, fable; openai: luna, terra, sol, astra\"")
        for m in ms:
            if m in seen:
                raise RiverError(f"model_ladder: {m} is in more than one place")
            seen.add(m)
        fams[name] = ms
    return fams


def _agent_platform(value):
    """A launch platform from an agent type: 'codex', 'claude-code' (or 'claude'); None when it is none of them."""
    v = (value or "").strip().lower().replace(" ", "-")
    v = {"claude": "claude-code"}.get(v, v)
    return v if v in LAUNCH_PLATFORMS else None


def _check_agent_type(conn, value):
    """An item's agent type: a platform, or a launch_agents label that runs one (Codex, Claude Code)."""
    p = _agent_platform(value)
    if p is None:
        for label, cmd in parse_launch_agents(setting(conn, "launch_agents")):
            if label.lower() == value.strip().lower():
                prof = parse_profile(cmd)
                p = prof[0] if prof else _exe_platform(cmd)
    if p is None:
        raise RiverError(f"agent {value!r}: an agent type is one of {', '.join(LAUNCH_PLATFORMS)} "
                         f"(or a launch_agents label that runs one)")
    return p


def _exe_platform(cmd):
    """The platform a custom launch_agents command runs, by its program: claude or codex."""
    exe = Path(cmd.split()[0]).name.lower() if cmd.split() else ""
    return {"claude": "claude-code", "codex": "codex"}.get(exe)


def parse_agent_rules(value):
    """agent_rules: [(platform, [patterns])]. "codex: *.css, *.svg, image; claude-code: docs/*".
    A pattern with '*', '?', '/' or '.' matches a file the item touches; any other is a word in its title."""
    out = []
    for part in (x.strip() for x in (value or "").split(";")):
        if not part:
            continue
        name, sep, pats = part.partition(":")
        p = _agent_platform(name)
        if not sep or p is None:
            raise RiverError(f"agent_rules: {part!r} needs the form <agent type>: pattern, pattern (agent type: "
                             f"{', '.join(LAUNCH_PLATFORMS)}), for example \"codex: *.css, *.svg, image, logo\"")
        ps = [x.strip() for x in pats.split(",") if x.strip()]
        if not ps:
            raise RiverError(f"agent_rules: {name.strip()} lists no patterns")
        out.append((p, ps))
    return out


def _rule_agent(rules, it):
    """The agent type the first matching agent_rules entry gives the item, or None."""
    import fnmatch
    files = [f for f in (it["touches"] or "").split(",") if f.strip()]
    title = (it["title"] or "").lower()
    for platform, pats in rules:
        for pat in pats:
            if re.search(r"[*?/.]", pat):
                if any(fnmatch.fnmatch(f.strip(), pat) or fnmatch.fnmatch(Path(f.strip()).name, pat) for f in files):
                    return platform
            elif re.search(r"\b" + re.escape(pat.lower()) + r"\b", title):
                return platform
    return None


def _levels(value):
    return [x.strip().lower() for x in (value or "").split(",") if x.strip()]


def _family(ladder, model):
    """(family, position) of a model in the ladder, or (None, None)."""
    m = (model or "").strip().lower()
    for fam, ms in ladder.items():
        if m in ms:
            return fam, ms.index(m)
    return None, None


def _check_model_name(value):
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", value):
        raise RiverError(f"model {value!r}: a model name uses letters, digits, '.', '_', '-'")
    return value.lower()


def _check_effort(conn, value, levels=None):
    levels = levels or _levels(setting(conn, "effort_levels"))
    if value.lower() not in levels:
        raise RiverError(f"effort is one of {', '.join(levels)} (the effort_levels setting)")
    return value.lower()


def _limit_list(ladder, value, what="limit"):
    """A min/max limit: one model per family, each in the ladder. Returns the normal form "a, b"."""
    out, fams = [], set()
    for m in _levels(value):
        fam, _ = _family(ladder, m)
        if fam is None:
            known = "; ".join(f"{f}: {', '.join(ms)}" for f, ms in ladder.items())
            raise RiverError(f"{what}: {m} is not in the model_ladder setting ({known}); "
                             f"a limit needs an order. Add it: maxpm config set model_ladder \"...\"")
        if fam in fams:
            raise RiverError(f"{what}: give at most one model per family ({fam} twice)")
        fams.add(fam)
        out.append(m)
    return ", ".join(out)


def _check_limits(ladder, lo, hi):
    """min <= max inside each family that both name."""
    for a in _levels(lo):
        fa, pa = _family(ladder, a)
        for b in _levels(hi):
            fb, pb = _family(ladder, b)
            if fa == fb and pa > pb:
                raise RiverError(f"min model {a} is stronger than max model {b} ({fa}: {', '.join(ladder[fa])})")


def model_check(ladder, model, lo, hi):
    """Whether a session running `model` may take an item with limits lo/hi (comma lists).

    Returns (allowed, reason). A limit compares only models of the session's family; a limit that names
    no model of that family, or a model the ladder does not know, does not apply, and the reason says so."""
    if not (lo or hi):
        return True, ""
    if not model:
        return True, ""
    fam, pos = _family(ladder, model)
    if fam is None:
        return True, (f"limits (min {lo or '-'}, max {hi or '-'}) not checked: model {model} is not in "
                      f"model_ladder, so it has no order")
    notes = []
    for label, lim in (("min", lo), ("max", hi)):
        if not lim:
            continue
        same = [m for m in _levels(lim) if _family(ladder, m)[0] == fam]
        if not same:
            notes.append(f"{label} {lim} is another family than {model} ({fam}): no order across families, "
                         f"so the limit does not apply")
            continue
        _, p = _family(ladder, same[0])
        if label == "min" and pos < p:
            return False, f"needs at least {same[0]}; {model} is weaker ({fam}: {', '.join(ladder[fam])})"
        if label == "max" and pos > p:
            return False, f"allows at most {same[0]}; {model} is stronger ({fam}: {', '.join(ladder[fam])})"
    return True, "; ".join(notes)


def _model_defaults(conn):
    """The default_* settings by scope, read once for annotate."""
    rows = {}
    for r in conn.execute("SELECT scope, key, value FROM settings WHERE key IN "
                          "('default_model','default_effort','default_min_model','default_max_model','default_agent')"):
        rows.setdefault(r["scope"], {})[r["key"]] = r["value"]
    try:
        rows["_rules"] = parse_agent_rules(setting(conn, "agent_rules"))
    except RiverError:
        rows["_rules"] = []
    return rows


# Built-in defaults per item kind, below every setting: a monitor watches, so a weak model is enough.
KIND_MODELS = {"monitor": {"model": "sonnet", "effort": "low", "max_model": "sonnet"}}


def _item_models(it, project, defaults, ladder_text=DEFAULT_SETTINGS["model_ladder"]):
    """Effective model, effort, and limits of an item: its own, else the most specific default_* setting,
    else the built-in default of its kind (KIND_MODELS)."""
    scopes = [f"item:{it['id']}", f"kind:{it['kind']}", f"project:{project}", "global"]
    out = {}
    for f in MODEL_FIELDS:
        own = it[f] if f in it.keys() else None
        if own:
            out[f], out[f + "_from"] = own, "item"
            continue
        out[f], out[f + "_from"] = None, None
        if f == "agent" and defaults.get("_rules"):
            got = _rule_agent(defaults["_rules"], it)
            if got:
                out[f], out[f + "_from"] = got, "agent_rules"
                continue
        for sc in scopes:
            v = defaults.get(sc, {}).get("default_" + f)
            if v is not None:
                if v:
                    out[f], out[f + "_from"] = v, sc
                break
        else:
            v = KIND_MODELS.get(it["kind"], {}).get(f)
            if v:
                out[f], out[f + "_from"] = v, f"kind:{it['kind']}"
    # A default limit that contradicts the item's own limit gives way to it.
    lo, hi = out["min_model"], out["max_model"]
    if lo and hi and (out["min_model_from"] == "item") != (out["max_model_from"] == "item"):
        try:
            _check_limits(parse_ladder(ladder_text), lo, hi)
        except RiverError:
            f = "max_model" if out["min_model_from"] == "item" else "min_model"
            out[f], out[f + "_from"] = None, None
    return out


def agent_model(conn, name):
    r = conn.execute("SELECT model FROM agents WHERE name=?", (name,)).fetchone() if name else None
    return r["model"] if r else None


def agent_type_from_env(env):
    """The agent CLI a command runs in, from its environment: MAXPM_AGENT_TYPE (to set it by hand), else
    the CLI's own variables (Codex: CODEX_THREAD_ID; Claude Code: CLAUDECODE). Codex first: a Codex started
    from a Claude Code shell keeps CLAUDECODE."""
    t = _agent_platform(env.get("MAXPM_AGENT_TYPE"))
    if t:
        return t
    if env.get("CODEX_THREAD_ID") or env.get("CODEX_SANDBOX"):
        return "codex"
    if env.get("CLAUDECODE"):
        return "claude-code"
    return None


def set_agent_type(conn, name, agent_type):
    with tx(conn):
        if agent_type and conn.execute("UPDATE agents SET agent_type=? WHERE name=? AND agent_type IS NOT ?",
                                       (agent_type, name, agent_type)).rowcount:
            _event(conn, None, name, f"agent type {agent_type}")


def session_type(conn, name):
    """The agent type of a session: recorded from its environment, else the platform of its model's family."""
    r = conn.execute("SELECT agent_type, model FROM agents WHERE name=?", (name,)).fetchone() if name else None
    if not r:
        return None
    if r["agent_type"]:
        return r["agent_type"]
    fam, _ = _family(parse_ladder(setting(conn, "model_ladder")), r["model"])
    return next((p for p, pl in LAUNCH_PLATFORMS.items() if pl["family"] == fam), None) if fam else None


def agent_type_check(session, item_agent):
    """(allowed, reason): a session of one agent type does not take an item meant for another."""
    if not item_agent or not session or session == item_agent:
        return True, ""
    return False, f"is for {item_agent}; this session runs {session}"


def set_agent_model(conn, name, model):
    """Record the model a session runs; go, next, and claim keep it off items whose limits exclude it."""
    model = _check_model_name(model) if model else None
    with tx(conn):
        if conn.execute("UPDATE agents SET model=? WHERE name=? AND model IS NOT ?", (model, name, model)).rowcount:
            _event(conn, None, name, f"model {model or 'unset'}")
    return model


# ---------------------------------------------------------------- projects

def _new_name(conn, kind, row, new):
    """Check the new name of a project or target (inside a tx): refused when the name is in use."""
    if not re.match(r"^[a-z0-9][a-z0-9._-]*$", new or ""):
        raise RiverError(f"{kind} names use lower-case letters, digits, '.', '_', '-'")
    if new == row["name"]:
        raise RiverError(f"{kind} {new} has that name already")
    if conn.execute(f"SELECT 1 FROM {kind}s WHERE name=?", (new,)).fetchone():
        raise RiverError(f"{kind} {new!r} exists; a rename needs a name that is free")


def _project(conn, name):
    r = conn.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()
    if not r:
        names = [x["name"] for x in conn.execute("SELECT name FROM projects WHERE archived=0 ORDER BY rank")]
        raise RiverError(f"no project {name!r}; projects: {', '.join(names) or '(none; maxpm project add <name>)'}")
    return r


def look_setup(conn, name):
    """The setting result_look as the setup of a project shows it (#1682): the value in force, if someone chose
    it for this project, and the value the setup recommends. The recommendation comes from one fact: does
    something stand between a push and the users? A deploy target gives a release step, so push_first; with no
    target a push can be live at once, so ask_first."""
    p = _project(conn, name)
    chosen = conn.execute("SELECT 1 FROM settings WHERE scope=? AND key='result_look'",
                          (f"project:{p['name']}",)).fetchone() is not None
    if p["target"]:
        rec, why = "push_first", (f"it ships to the deploy target {p['target']}, so a release step stands between a "
                                  f"push and the users")
    else:
        rec, why = "ask_first", "it has no deploy target, so a push can be live at once"
    return {"value": setting(conn, "result_look", project_id=p["id"]), "chosen": chosen, "recommended": rec, "why": why}


def project_add(conn, name, rank=None, notes="", actor=None, path=None, target=None, result_look=None):
    if not re.match(r"^[a-z0-9][a-z0-9._-]*$", name):
        raise RiverError("project names use lower-case letters, digits, '.', '_', '-'")
    if result_look is not None and result_look not in ("ask_first", "push_first"):
        raise RiverError("result_look is ask_first or push_first")
    with tx(conn):
        if conn.execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone():
            raise RiverError(f"project {name!r} exists")
        top = conn.execute("SELECT COALESCE(MAX(rank),0) m FROM projects").fetchone()["m"]
        conn.execute("INSERT INTO projects(name,rank,notes,created_at) VALUES (?,?,?,?)",
                     (name, top + 1, notes, iso(now())))
        _event(conn, None, actor, f"project {name} added")
    if rank is not None:
        project_rank(conn, name, rank, actor)
    if path:
        project_path(conn, name, path, actor)
    if target:
        project_target(conn, name, target, actor)
    if result_look:
        config_set(conn, "result_look", result_look, project=name, actor=actor)
    # The agent that sets the project up sees the rule for a look at a finished result, and chooses with the person.
    return {**dict(_project(conn, name)), "look": look_setup(conn, name)}


def project_rank(conn, name, rank, actor=None):
    """Move a project to position `rank` (1 = most important) and renumber the rest."""
    with tx(conn):
        name = _project(conn, name)["name"]
        names = [r["name"] for r in conn.execute(
            "SELECT name FROM projects WHERE archived=0 ORDER BY rank, id") if r["name"] != name]
        rank = max(1, min(int(rank), len(names) + 1))
        names.insert(rank - 1, name)
        for i, n in enumerate(names, 1):
            conn.execute("UPDATE projects SET rank=? WHERE name=?", (i, n))
        _event(conn, None, actor, f"project {name} rank {rank}")
    return project_list(conn)


def project_archive(conn, name, actor=None):
    with tx(conn):
        name = _project(conn, name)["name"]
        conn.execute("UPDATE projects SET archived=1, rank=100000 WHERE name=?", (name,))
        _event(conn, None, actor, f"project {name} archived")
    return project_list(conn)


def project_path(conn, name, path, actor=None, move=False):
    """Link a project to a folder, so `maxpm go` run inside that folder finds it. A project linked to another
    folder that still exists moves only with move=True: agents in the old folder would stop finding it."""
    full = str(Path(path).expanduser().resolve()) if path else None
    with tx(conn):
        p = _project(conn, name)
        name, old = p["name"], p["path"]
        if old and full and old != full and Path(old).is_dir() and not move:
            raise RiverError(f"project {name} is linked to {old}; linking it to {full} moves it, and agents in "
                             f"{old} stop finding it. Only the user decides that: maxpm project path {name} "
                             f"{full} --move. A new project for this folder instead: maxpm init")
        conn.execute("UPDATE projects SET path=? WHERE name=?", (full, name))
        _event(conn, None, actor, f"project {name} path {full or 'cleared'}")
    return dict(_project(conn, name))


def project_target(conn, name, target, actor=None):
    """Put a project in the deploy target it ships to; no target clears it."""
    with tx(conn):
        name = _project(conn, name)["name"]
        if target is not None:
            target = _target(conn, target)["name"]
        conn.execute("UPDATE projects SET target=? WHERE name=?", (target, name))
        _event(conn, None, actor, f"project {name} target {target or 'cleared'}")
    return dict(_project(conn, name))


def projects_for_dir(conn, cwd):
    """Projects whose folder contains cwd; the deepest folder wins, ties keep every match."""
    here = Path(cwd).expanduser().resolve()
    best, depth = [], -1
    for r in conn.execute("SELECT name, path FROM projects WHERE archived=0 AND path IS NOT NULL ORDER BY rank"):
        root = Path(r["path"])
        if here == root or root in here.parents:
            d = len(root.parts)
            if d > depth:
                best, depth = [r["name"]], d
            elif d == depth:
                best.append(r["name"])
    return best


def project_describe(conn, name, text, actor=None):
    """The project description tells an agent what the project covers and what context helps."""
    with tx(conn):
        name = _project(conn, name)["name"]
        conn.execute("UPDATE projects SET notes=? WHERE name=?", (text, name))
        _event(conn, None, actor, f"project {name} description changed")
    return project_show(conn, name)


def project_show(conn, name):
    p = dict(_project(conn, name))
    name = p["name"]
    ann = annotate(conn)
    mine = [a for a in ann.values() if a["project"] == name]
    ready = sorted((a for a in mine if a["ready"]), key=lambda a: a["sort_key"])
    holders = sorted({a["assignee"] for a in mine if a["status"] in ("in_progress", "held") and a["assignee"]})
    recent = sorted({r["actor"] for r in conn.execute(
        "SELECT DISTINCT e.actor FROM events e JOIN items i ON i.id=e.item_id "
        "WHERE i.project_id=? AND (e.change LIKE 'claimed%' OR e.change LIKE 'done%') "
        "ORDER BY e.id DESC LIMIT 20", (p["id"],))})
    p.update(
        description=p.pop("notes"),
        counts={s: sum(1 for a in mine if a["status"] == s) for s in ("open", "in_progress", "held", "done", "dropped")},
        ready=[{k: v for k, v in a.items() if k != "sort_key"} for a in ready[:5]],
        ready_count=len(ready),
        working_now=holders,
        worked_recently=recent,
        goals=goal_list(conn, name, include_complete=False),
        tracker=setting(conn, "tracker", project_id=p["id"]),
        result_look=setting(conn, "result_look", project_id=p["id"]),
        look=look_setup(conn, name),
    )
    return p


def project_list(conn):
    rows = [dict(r) for r in conn.execute(
        "SELECT p.*, (SELECT COUNT(*) FROM items i WHERE i.project_id=p.id AND i.status IN ('open','in_progress','held')) open_items "
        "FROM projects p WHERE archived=0 ORDER BY rank, id")]
    for r in rows:
        r["tracker"] = setting(conn, "tracker", project_id=r["id"])
    return rows


def project_tracker(conn, name, text=None, actor=None):
    """Show, set, or (with none) clear the outside tracker a project uses."""
    p = _project(conn, name)
    name = p["name"]
    if text is not None:
        text = text.strip()
        if text.lower() in ("", "none"):
            if conn.execute("SELECT 1 FROM settings WHERE scope=? AND key='tracker'", (f"project:{name}",)).fetchone():
                config_unset(conn, "tracker", project=name, actor=actor)
        else:
            config_set(conn, "tracker", text, project=name, actor=actor)
    return {"name": p["name"], "tracker": setting(conn, "tracker", project_id=p["id"])}


def _names(conn, project):
    """A project name or a comma list as the projects' names now."""
    return [_project(conn, n.strip())["name"] for n in str(project).split(",") if n.strip()]


def _rename_project(conn, p, new, actor):
    """Inside a tx: project row p gets the name new. Items, goals, review steps, the folder, the rank and the
    target hold the project's id, so they follow; the settings of the project and the projects that waiting
    agents named hold its name, so they change here. The old name stops at once."""
    old = p["name"]
    conn.execute("UPDATE projects SET name=? WHERE id=?", (new, p["id"]))
    moved = conn.execute("UPDATE OR REPLACE settings SET scope=? WHERE scope=?",
                         (f"project:{new}", f"project:{old}")).rowcount
    waiting = 0
    for a in conn.execute("SELECT name, waiting_in FROM agents WHERE waiting_in IS NOT NULL").fetchall():
        names = a["waiting_in"].split(",")
        if old in names:
            conn.execute("UPDATE agents SET waiting_in=? WHERE name=?",
                         (",".join(new if n == old else n for n in names), a["name"]))
            waiting += 1
    _event(conn, None, actor, f"project {old} renamed to {new}")
    return {"name": new, "was": old, "settings": moved, "waiting_agents": waiting,
            "items": conn.execute("SELECT COUNT(*) n FROM items WHERE project_id=?", (p["id"],)).fetchone()["n"],
            "goals": conn.execute("SELECT COUNT(*) n FROM goals WHERE project_id=?", (p["id"],)).fetchone()["n"]}


def project_rename(conn, old, new, actor=None):
    """Give a project a new name, in one transaction. Refused when the new name is in use. Everything of
    the project follows: items, goals, review steps, folder, rank, target, tracker and settings. The old
    name stops at once. Agents keep their names; a new agent gets the new name as its prefix. History
    keeps the old texts."""
    with tx(conn):
        p = _project(conn, old)
        _new_name(conn, "project", p, new)
        t = p["name"][len("deploy-"):] if p["name"].startswith("deploy-") else None
        if t and conn.execute("SELECT 1 FROM targets WHERE name=?", (t,)).fetchone():
            raise RiverError(f"project {p['name']} holds the deploy items of target {t} and takes its name from "
                             f"it; rename the target: maxpm target rename {t} <new>")
        if new.startswith("deploy-") and conn.execute("SELECT 1 FROM targets WHERE name=?", (new[7:],)).fetchone():
            raise RiverError(f"{new} is the name for the deploy items of target {new[7:]}; pick another name")
        return _rename_project(conn, p, new, actor)


def target_rename(conn, old, new, actor=None):
    """Give a deploy target a new name, in one transaction: its projects, its deploy, review and monitor
    items (open ones also get the new name in their title), and its deploy project (deploy-<target>)
    follow. Refused when the new name is in use. The old names stop at once."""
    with tx(conn):
        t = _target(conn, old)
        old = t["name"]
        _new_name(conn, "target", t, new)
        dp = conn.execute("SELECT * FROM projects WHERE name=?", (f"deploy-{old}",)).fetchone()
        if dp is not None:
            _new_name(conn, "project", dp, f"deploy-{new}")
        conn.execute("UPDATE targets SET name=? WHERE id=?", (new, t["id"]))
        projects = conn.execute("UPDATE projects SET target=? WHERE target=?", (new, old)).rowcount
        items = conn.execute("UPDATE items SET target=? WHERE target=?", (new, old)).rowcount
        conn.execute("UPDATE hook_runs SET target=? WHERE target=?", (new, old))
        for kind, was, now_ in (("deploy", f"Deploy {old}", f"Deploy {new}"),
                                ("review", f"Review release {old}", f"Review release {new}")):
            conn.execute(f"UPDATE items SET title=? WHERE kind=? AND target=? AND title=? AND status IN {OPEN_STATES}",
                         (now_, kind, new, was))
        conn.execute("UPDATE items SET title=? || found_during WHERE kind='monitor' AND target=? "
                     f"AND title=? || found_during AND status IN {OPEN_STATES}",
                     (f"Monitor the {new} deploy #", new, f"Monitor the {old} deploy #"))
        res = {"name": new, "was": old, "projects": projects, "items": items,
               "deploy_project": None}
        if dp is not None:
            res["deploy_project"] = _rename_project(conn, dp, f"deploy-{new}", actor)["name"]
            was = f"Deploys to target {old},"
            if dp["notes"].startswith(was):
                conn.execute("UPDATE projects SET notes=? WHERE id=?",
                             (f"Deploys to target {new}," + dp["notes"][len(was):], dp["id"]))
        _event(conn, None, actor, f"target {old} renamed to {new}")
    return res


# ---------------------------------------------------------------- goals

def _goal(conn, name):
    r = conn.execute("SELECT * FROM goals WHERE name=?", (name,)).fetchone()
    if not r:
        known = [x["name"] for x in conn.execute("SELECT name FROM goals WHERE status='open' ORDER BY rank")]
        raise RiverError(f"no goal {name!r}" + (f"; open goals: {', '.join(known)}" if known
                                                else "; add one: maxpm goal add <project> <name> --outcome \"...\""))
    return r


def _tag(conn, item_id, goal, actor):
    g = _goal(conn, goal)
    if conn.execute("INSERT OR IGNORE INTO item_goals(item_id, goal_id) VALUES (?,?)", (item_id, g["id"])).rowcount:
        _event(conn, item_id, actor, f"tagged goal {goal}")
        if g["owner"] and actor and g["owner"] != actor:
            it = _item(conn, item_id)
            _send(conn, "notice", "maxpm", f"{actor} added #{item_id} {it['title']} to your goal {goal}",
                  to=g["owner"], item_id=item_id)


def _goal_notice(conn, item_id, actor, verb, detail=None):
    """Tell each goal's owner when someone else claims or finishes an item tagged with it."""
    for g in conn.execute("SELECT g.name, g.owner FROM item_goals ig JOIN goals g ON g.id=ig.goal_id "
                          "WHERE ig.item_id=? AND g.owner IS NOT NULL AND g.status='open'", (item_id,)).fetchall():
        if actor and g["owner"] != actor:
            it = _item(conn, item_id)
            _send(conn, "notice", "maxpm", f"{actor} {verb} #{item_id} {it['title']} (your goal {g['name']})"
                  + (f": {detail}" if detail else ""),
                  to=g["owner"], item_id=item_id)


def _goal_view(conn, g, ann=None):
    ann = ann if ann is not None else annotate(conn)
    d = dict(g)
    d["project"] = _project_name(conn, g["project_id"])
    ids = [r["item_id"] for r in conn.execute("SELECT item_id FROM item_goals WHERE goal_id=?", (g["id"],))]
    items = [ann[i] for i in ids if i in ann]
    d["items_open"] = sorted(a["id"] for a in items if a["status"] in OPEN_STATES)
    d["items_done"] = sorted(a["id"] for a in items if a["status"] == "done")
    d["items_dropped"] = sorted(a["id"] for a in items if a["status"] == "dropped")
    h = _handoff(conn, g["id"])
    d["handoff"] = {k: h[k] for k in ("version", "by_agent", "created_at")} if h else None
    d["parent"] = _goal_name(conn, g["parent_id"]) if g["parent_id"] else None
    d["subgoals"] = [r["name"] for r in conn.execute("SELECT name FROM goals WHERE parent_id=? ORDER BY rank, id",
                                                     (g["id"],))]
    due = _handoff_due(conn, g)
    d["handoff_due"] = due
    return d


HANDOFF_MAX = 64_000  # characters: a handoff is read at the start of every session of the goal


def _goal_name(conn, goal_id):
    return conn.execute("SELECT name FROM goals WHERE id=?", (goal_id,)).fetchone()["name"]


def parent_handoff(conn, g):
    """The parent goal's latest handoff, with its goal name, for a sub-goal; else None."""
    if not g["parent_id"]:
        return None
    h = _handoff(conn, g["parent_id"])
    return dict(h, goal=_goal_name(conn, g["parent_id"])) if h else None


def _handoff(conn, goal_id, version=None):
    """The latest handoff of a goal (or one version), or None."""
    r = conn.execute("SELECT * FROM goal_handoffs WHERE goal_id=? " + ("AND version=? " if version else "")
                     + "ORDER BY version DESC LIMIT 1", (goal_id, version) if version else (goal_id,)).fetchone()
    return dict(r) if r else None


def _handoff_due(conn, g):
    """The owner's last finished item of the goal when it is newer than the goal's handoff (or the goal has
    none): {"id", "title", "closed_at"}; else None. Only an agent owner keeps a handoff."""
    if not g["owner"] or not conn.execute("SELECT 1 FROM agents WHERE name=? AND kind='ai'", (g["owner"],)).fetchone():
        return None
    h = _handoff(conn, g["id"])
    for r in conn.execute("SELECT i.id, i.title, i.closed_at FROM items i JOIN item_goals ig ON ig.item_id=i.id "
                          "WHERE ig.goal_id=? AND i.status='done' AND i.closed_at IS NOT NULL "
                          "ORDER BY i.closed_at DESC, i.id DESC", (g["id"],)):
        if h and r["closed_at"] <= h["created_at"]:
            return None
        if _done_by(conn, r["id"]) == g["owner"]:
            return dict(r)
    return None


def goal_handoff(conn, name, text=None, actor=None, version=None):
    """Read a goal's handoff (the latest, or one version), or store a new version of it (text). The owner
    writes it; when nobody owns the goal (or it is shared), any agent; a person or the manager always."""
    g = _goal(conn, name)
    if text is not None:
        text = text.strip()
        if not text:
            raise RiverError("the handoff is empty: say what the goal is, the decisions so far, the files that "
                             "matter, and what is left")
        if len(text) > HANDOFF_MAX:
            raise RiverError(f"the handoff has {len(text)} characters; keep it under {HANDOFF_MAX}: every session "
                             f"of the goal reads it. Point to files for the details")
        who = conn.execute("SELECT kind, role FROM agents WHERE name=?", (actor,)).fetchone() if actor else None
        if g["owner"] and actor != g["owner"] and not (who and (who["kind"] == "human" or who["role"] == "manager")):
            raise RiverError(f"goal {name} is owned by {g['owner']}, who keeps its handoff. Send what you know: "
                             f"maxpm note \"...\" --goal {name}")
        with tx(conn):
            n = conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM goal_handoffs WHERE goal_id=?",
                             (g["id"],)).fetchone()[0]
            conn.execute("INSERT INTO goal_handoffs (goal_id, version, text, by_agent, created_at) VALUES (?,?,?,?,?)",
                         (g["id"], n, text, actor, iso(now())))
            _event(conn, None, actor, f"handoff v{n} for goal {name} ({len(text)} characters)")
    h = _handoff(conn, g["id"], version)
    if version and not h:
        raise RiverError(f"goal {name} has no handoff version {version}: maxpm goal handoff {name} --versions")
    versions = [dict(r) for r in conn.execute(
        "SELECT version, by_agent, created_at, LENGTH(text) chars FROM goal_handoffs WHERE goal_id=? ORDER BY version",
        (g["id"],))]
    return {"goal": name, "handoff": h, "versions": versions, "due": _handoff_due(conn, _goal(conn, name))}


def _handoff_refusal(conn, g, verb, no_handoff, actor):
    """goal release and goal give refuse while the owner's last finished item of the goal is newer than its
    handoff, unless the owner gives a reason (no_handoff), which goes in the history. Inside a tx."""
    due = _handoff_due(conn, g)
    if not due:
        return
    if no_handoff and no_handoff.strip():
        _event(conn, None, actor, f"{verb} goal {g['name']} without a current handoff: {no_handoff.strip()}")
        return
    h = _handoff(conn, g["id"])
    raise RiverError(f"refused: the handoff of goal {g['name']} " + (f"(v{h['version']}, {h['created_at']}) is older than"
                     if h else "is missing; you finished") + f" #{due['id']} {due['title']} ({due['closed_at']}). "
                     f"Write it first, for the next owner: maxpm goal handoff {g['name']} --file <path> (what the goal "
                     f"is, the decisions so far, the files that matter, what is left). Or give the reason: "
                     f"maxpm goal {verb} {g['name']} --no-handoff \"<why>\"")


def goal_add(conn, project, name, outcome="", done_when="", actor=None, rank=None, shared=False, parent=None):
    """A goal in a project. parent: a sub-goal of that goal (one level, same project): the context of a
    sub-goal's sessions is the parent's handoff plus its own, so each owner's context stays small."""
    if not re.match(r"^[a-z0-9][a-z0-9._-]{0,63}$", name):
        raise RiverError("goal names use lower-case letters, digits, '.', '_', '-' (up to 64), like project names")
    with tx(conn):
        p = _project(conn, project)
        if conn.execute("SELECT 1 FROM goals WHERE name=?", (name,)).fetchone():
            raise RiverError(f"goal {name} already exists: maxpm goal show {name}")
        up = _goal(conn, parent) if parent else None
        if up is not None:
            if up["parent_id"]:
                raise RiverError(f"goal {parent} is a sub-goal itself; sub-goals have one level. Use its parent: "
                                 f"--parent {_goal_name(conn, up['parent_id'])}")
            if up["project_id"] != p["id"]:
                raise RiverError(f"goal {parent} is in project {_project_name(conn, up['project_id'])}; a sub-goal is "
                                 f"in its parent's project")
            if up["status"] != "open":
                raise RiverError(f"goal {parent} is complete; reopen it first: maxpm goal reopen {parent}")
        top = conn.execute("SELECT COALESCE(MAX(rank),0) m FROM goals WHERE project_id=?", (p["id"],)).fetchone()["m"]
        conn.execute("INSERT INTO goals(project_id,name,outcome,done_when,rank,created_at,shared,parent_id) "
                     "VALUES (?,?,?,?,?,?,?,?)", (p["id"], name, outcome or "", done_when or "", top + 1, iso(now()),
                                                  1 if shared else 0, up["id"] if up is not None else None))
        _event(conn, None, actor, f"goal {name} added to {p['name']}" + (" (shared: no owner)" if shared else "")
               + (f" as a sub-goal of {parent}" if parent else ""))
    if rank is not None:
        goal_rank(conn, name, rank, actor)
    return goal_show(conn, name)


def goal_list(conn, project=None, include_complete=False):
    """Goals in order: project rank, then goal rank. Each with its owner and tagged-item progress."""
    ann = annotate(conn)
    project = _project(conn, project)["name"] if project else None
    sql = ("SELECT g.* FROM goals g JOIN projects p ON p.id=g.project_id WHERE p.archived=0"
           + ("" if include_complete else " AND g.status='open'") + (" AND p.name=?" if project else "")
           + " ORDER BY g.status='complete', p.rank, g.rank, g.id")
    return [_goal_view(conn, g, ann) for g in conn.execute(sql, (project,) if project else ())]


def goal_show(conn, name):
    g = _goal(conn, name)
    ann = annotate(conn)
    d = _goal_view(conn, g, ann)
    h = _handoff(conn, g["id"])
    d["handoff_text"] = h["text"] if h else None
    d["parent_handoff"] = parent_handoff(conn, g)
    d["items"] = [{k: v for k, v in ann[i].items() if k != "sort_key"}
                  for i in sorted(d["items_open"] + d["items_done"] + d["items_dropped"],
                                  key=lambda i: (ann[i]["status"] in CLOSED_STATES, ann[i]["sort_key"]))]
    return d


def goal_rank(conn, name, rank, actor=None):
    """Put a goal at position `rank` (1 = first) among its project's goals."""
    with tx(conn):
        g = _goal(conn, name)
        names = [r["name"] for r in conn.execute("SELECT name FROM goals WHERE project_id=? AND name<>? "
                                                  "ORDER BY rank, id", (g["project_id"], name))]
        names.insert(max(0, min(int(rank) - 1, len(names))), name)
        for i, n in enumerate(names, 1):
            conn.execute("UPDATE goals SET rank=? WHERE name=?", (i, n))
        _event(conn, None, actor, f"goal {name} ranked {rank}")
    return goal_show(conn, name)


def goal_edit(conn, name, outcome=None, done_when=None, new_name=None, actor=None, shared=None):
    """Change a goal. shared=True makes it a goal with no owner: maxpm go never gives it to an agent, nobody
    can own it, and its items stay open to every agent; shared=False lets one agent own it again. A person
    or a manager decides that, and the goal's owner can hand its goal to everyone."""
    with tx(conn):
        g = _goal(conn, name)
        if shared is not None and bool(shared) != bool(g["shared"]):
            who = conn.execute("SELECT kind, role FROM agents WHERE name=?", (actor,)).fetchone() if actor else None
            if who and who["kind"] == "ai" and who["role"] != "manager" and not (shared and g["owner"] == actor):
                raise RiverError(f"refused: a person or a manager decides if goal {name} is shared (no owner)"
                                 + ("" if shared else "; while it is shared, take its items with maxpm go"))
            if shared:
                conn.execute("UPDATE goals SET shared=1, owner=NULL, owner_expires_at=NULL, owner_lease=NULL WHERE id=?",
                             (g["id"],))
                _event(conn, None, actor, f"goal {name} is shared: no owner, its items are open to every agent"
                       + (f" (was owned by {g['owner']})" if g["owner"] else ""))
                if g["owner"] and g["owner"] != actor:
                    _send(conn, "notice", "maxpm", f"{actor or 'someone'} made goal {name} a shared goal: you do not "
                          f"own it now, and its items are open to every agent. Keep the items you hold; maxpm go "
                          f"gives you its other items like any work.", to=g["owner"])
            else:
                conn.execute("UPDATE goals SET shared=0 WHERE id=?", (g["id"],))
                _event(conn, None, actor, f"goal {name} is not shared any more: one agent can own it")
        if outcome is not None and outcome != g["outcome"]:
            conn.execute("UPDATE goals SET outcome=? WHERE id=?", (outcome, g["id"]))
            _event(conn, None, actor, f"goal {name}: outcome changed")
        if done_when is not None and done_when != g["done_when"]:
            conn.execute("UPDATE goals SET done_when=? WHERE id=?", (done_when, g["id"]))
            _event(conn, None, actor, f"goal {name}: done-when changed")
        if new_name and new_name != name:
            if not re.match(r"^[a-z0-9][a-z0-9._-]{0,63}$", new_name):
                raise RiverError("goal names use lower-case letters, digits, '.', '_', '-' (up to 64)")
            if conn.execute("SELECT 1 FROM goals WHERE name=?", (new_name,)).fetchone():
                raise RiverError(f"goal {new_name} already exists")
            conn.execute("UPDATE goals SET name=? WHERE id=?", (new_name, g["id"]))
            _event(conn, None, actor, f"goal {name} renamed to {new_name}")
            name = new_name
    return goal_show(conn, name)


def _goal_lease(conn, g, actor):
    """How long a goal owner's claim lasts: the length it chose (goal own --lease), else goal_lease."""
    return parse_duration(g["owner_lease"] or setting(conn, "goal_lease", agent=actor))


def _lease_for(conn, item_id, actor):
    """An item lease: goal_lease (or the owner's --lease) when the actor owns a goal the item serves, else lease_ttl."""
    g = conn.execute("SELECT g.* FROM item_goals ig JOIN goals g ON g.id=ig.goal_id WHERE ig.item_id=? AND g.owner=? "
                     "AND g.status='open' ORDER BY g.rank LIMIT 1", (item_id, actor)).fetchone() if actor else None
    if g is not None:
        return _goal_lease(conn, g, actor)
    return parse_duration(setting(conn, "lease_ttl", item_id=item_id, agent=actor))


def goal_own(conn, name, actor=None, lease=None):
    """Own a goal: create and take the items that reach it. One owner at a time. Its agent items are
    reserved for the owner, and its leases last goal_lease (or --lease); the ownership frees after that
    long without a river command of the owner."""
    if lease is not None:
        parse_duration(lease)
    if not actor:
        raise RiverError("owning a goal needs an agent name: set MAXPM_AGENT or pass --as <name>")
    with tx(conn):
        _sweep(conn)
        g = _goal(conn, name)
        if g["status"] != "open":
            raise RiverError(f"goal {name} is complete; reopen it first: maxpm goal reopen {name}")
        if g["shared"]:
            raise RiverError(f"goal {name} is shared: nobody owns it, and its items are open to every agent. "
                             f"Take its items with maxpm go. A person or a manager changes that: "
                             f"maxpm goal edit {name} --owned")
        if g["owner"] and g["owner"] != actor:
            raise RiverError(f"goal {name} is owned by {g['owner']}; ask them (maxpm send question --to {g['owner']} ...), "
                             f"or take another: maxpm goal list")
        conn.execute("UPDATE goals SET owner=?, owner_lease=? WHERE id=?",
                     (actor, lease if lease is not None else (g["owner_lease"] if g["owner"] == actor else None), g["id"]))
        ttl = _goal_lease(conn, _goal(conn, name), actor)
        conn.execute("UPDATE goals SET owner_expires_at=? WHERE id=?", (iso(now() + ttl), g["id"]))
        if g["owner"] != actor:
            _event(conn, None, actor, f"owns goal {name} (its agent items are reserved for {actor}; lease {_short(ttl)})")
    return goal_show(conn, name)


def goal_release(conn, name, actor=None, no_handoff=None):
    with tx(conn):
        g = _goal(conn, name)
        if g["owner"] != actor:
            raise RiverError(f"goal {name} is " + (f"owned by {g['owner']}" if g["owner"] else "not owned"))
        _handoff_refusal(conn, g, "release", no_handoff, actor)
        conn.execute("UPDATE goals SET owner=NULL, owner_expires_at=NULL, owner_lease=NULL WHERE id=?", (g["id"],))
        _event(conn, None, actor, f"released goal {name}; its items are open to every agent")
    return goal_show(conn, name)


def _release_goals(conn, agent, why):
    """An agent that ended or is gone owns no goal: its goals are free, and their items open to every agent.
    Inside a tx."""
    for g in conn.execute("SELECT name FROM goals WHERE owner=?", (agent,)).fetchall():
        conn.execute("UPDATE goals SET owner=NULL, owner_expires_at=NULL, owner_lease=NULL WHERE name=?", (g["name"],))
        _event(conn, None, "maxpm", f"goal {g['name']} released: {why}")


def goal_give(conn, name, to, actor=None, no_handoff=None):
    """Hand a goal you own to another agent; they get a notice."""
    with tx(conn):
        g = _goal(conn, name)
        if g["owner"] != actor:
            raise RiverError(f"goal {name} is " + (f"owned by {g['owner']}" if g["owner"] else "not owned")
                             + "; only its owner gives it")
        rec = _agent(conn, to)
        if to == actor:
            raise RiverError("you own it already")
        _handoff_refusal(conn, g, "give", no_handoff, actor)
        ttl = parse_duration(setting(conn, "goal_lease", agent=to))
        conn.execute("UPDATE goals SET owner=?, owner_expires_at=?, owner_lease=NULL WHERE id=?",
                     (rec["name"], iso(now() + ttl), g["id"]))
        _event(conn, None, actor, f"gave goal {name} to {to}")
        _send(conn, "notice", actor, f"{actor} gave you goal {name}: {g['outcome']} (maxpm goal show {name})", to=to)
    return goal_show(conn, name)


def goal_owner(conn, name):
    """The owner of a goal, for messages sent --goal <name>."""
    g = _goal(conn, name)
    if not g["owner"]:
        raise RiverError((f"goal {name} is shared: it has no owner" if g["shared"] else f"nobody owns goal {name}")
                         + f"; message an item holder instead (maxpm goal show {name})")
    return g["owner"]


def goal_done(conn, name, result, actor=None, drop_open=False):
    """Declare a goal complete with a one-line result. Refused while tagged items are open, unless
    drop_open, which drops them with the result as the note."""
    if not (result or "").strip():
        raise RiverError("say what the goal achieved: maxpm goal done <name> --result \"<one line>\"")
    g = _goal(conn, name)
    if g["status"] == "complete":
        raise RiverError(f"goal {name} is already complete")
    if g["owner"] and actor and g["owner"] != actor:
        raise RiverError(f"goal {name} is owned by {g['owner']}; the owner declares it complete")
    subs = [r["name"] for r in conn.execute("SELECT name FROM goals WHERE parent_id=? AND status='open'", (g["id"],))]
    if subs:
        raise RiverError(f"goal {name} has open sub-goals: {', '.join(subs)}. Complete them first "
                         f"(maxpm goal done <sub-goal> --result \"...\")")
    still = _goal_view(conn, g)["items_open"]
    if still and not drop_open:
        raise RiverError(f"goal {name} has open items: {', '.join('#' + str(i) for i in still)}. Finish them, untag them "
                         f"(maxpm edit <id> --untag {name}), or drop them: maxpm goal done {name} --result \"...\" --drop-open")
    for i in still:
        drop(conn, i, actor, note=f"goal {name} completed without it: {result}")
    with tx(conn):
        conn.execute("UPDATE goals SET status='complete', result=?, completed_at=?, owner=NULL, owner_expires_at=NULL "
                     "WHERE id=?", (result.strip(), iso(now()), g["id"]))
        _event(conn, None, actor, f"goal {name} complete: {result.strip()}")
    return goal_show(conn, name)


def goal_reopen(conn, name, actor=None):
    with tx(conn):
        g = _goal(conn, name)
        if g["status"] == "open":
            raise RiverError(f"goal {name} is open already")
        conn.execute("UPDATE goals SET status='open', completed_at=NULL WHERE id=?", (g["id"],))
        _event(conn, None, actor, f"goal {name} reopened")
    return goal_show(conn, name)


# ---------------------------------------------------------------- deploy targets

def _target(conn, name):
    r = conn.execute("SELECT * FROM targets WHERE name=?", (name,)).fetchone()
    if not r:
        names = [x["name"] for x in conn.execute("SELECT name FROM targets ORDER BY name")]
        raise RiverError(f"no target {name!r}; targets: {', '.join(names) or '(none; maxpm target add <name> --description ...)'}")
    return r


def target_add(conn, name, description="", actor=None):
    """A deploy target is where projects ship to (a server, an app store, a package index)."""
    if not re.match(r"^[a-z0-9][a-z0-9._-]*$", name):
        raise RiverError("target names use lower-case letters, digits, '.', '_', '-'")
    with tx(conn):
        if conn.execute("SELECT 1 FROM targets WHERE name=?", (name,)).fetchone():
            raise RiverError(f"target {name!r} exists")
        conn.execute("INSERT INTO targets(name,description,created_at) VALUES (?,?,?)", (name, description, iso(now())))
        _event(conn, None, actor, f"target {name} added")
    return target_show(conn, name)


def target_describe(conn, name, text, actor=None):
    with tx(conn):
        name = _target(conn, name)["name"]
        conn.execute("UPDATE targets SET description=? WHERE name=?", (text, name))
        _event(conn, None, actor, f"target {name} description changed")
    return target_show(conn, name)


def target_monitor(conn, name, text, actor=None):
    """What to watch after each deploy of the target: health links, logs, error rates, and for how long.
    With it set, claiming a deploy item adds a monitor item, and maxpm serve opens a session for it."""
    with tx(conn):
        name = _target(conn, name)["name"]
        conn.execute("UPDATE targets SET monitor=? WHERE name=?", (text.strip(), name))
        _event(conn, None, actor, f"target {name} monitor " + ("changed" if text.strip() else "removed"))
    return target_show(conn, name)


# The events that run a hook (maxpm target hook), each with when it occurs. A later event (an item is claimed,
# an item is done, a goal is done) is one entry here and one _hook call in the transaction where it occurs.
HOOK_EVENTS = {
    "release-cut": "the release is cut: maxpm target cut, or a reviewer (the deployer, with no review) took it",
    "review-passed": "the review of the release is done (maxpm review pass)",
    "review-failed": "maxpm review fail sent the release back with fixes",
    "deployed": "the deploy item of the release is done",
}
HOOK_ORPHAN_AFTER = 120  # seconds after which maxpm serve runs a queued hook whose command did not (it ended early)
HOOK_TAIL = 15  # the lines of a hook's output that the command prints and the run keeps
HOOK_RUNNER = None  # a test hook: (command, folder, limit, renew, env) -> a result like _run_command's


def _hook_event(event):
    if event not in HOOK_EVENTS:
        raise RiverError(f"no hook event {event!r}; the events: {', '.join(HOOK_EVENTS)}")
    return event


def hook_token():
    """Names the process and thread that run now: a command runs the hooks of the events that it caused."""
    import threading
    return f"{this_host()}:{os.getpid()}:{threading.get_ident()}"


def target_hooks(conn, name):
    """The hooks of a target in the order of HOOK_EVENTS, each with its last run (None: it never ran)."""
    t = _target(conn, name)
    rows = {r["event"]: dict(r) for r in conn.execute(
        "SELECT event, command, set_by, set_at FROM hooks WHERE target_id=?", (t["id"],))}
    out = []
    for event in HOOK_EVENTS:
        if event not in rows:
            continue
        last = conn.execute("SELECT queued_at, started_at, ended_at, exit_code, timed_out, seconds, deploy_id, item_id "
                            "FROM hook_runs WHERE target=? AND event=? ORDER BY id DESC LIMIT 1", (t["name"], event)).fetchone()
        out.append(dict(rows[event], last=dict(last) if last else None))
    return out


def target_hook(conn, name, event=None, command=None, clear=False, actor=None):
    """maxpm target hook: set, remove, or list the hooks of a target. A hook is a shell command that MaximizePM
    runs when an event of the target occurs (HOOK_EVENTS), one for each event: a script can call more. The
    command that causes the event runs it (run_hooks). A hook never undoes and never refuses its event."""
    changed = None
    with tx(conn):
        t = _target(conn, name)
        name = t["name"]
        if event is None:
            if clear or command is not None:
                raise RiverError(f"name the event: maxpm target hook {name} <{'|'.join(HOOK_EVENTS)}> \"<command>\"")
        else:
            _hook_event(event)
            has = conn.execute("SELECT command FROM hooks WHERE target_id=? AND event=?", (t["id"], event)).fetchone()
            if clear:
                if command is not None:
                    raise RiverError(f"give a command or --clear, not both: maxpm target hook {name} {event} --clear")
                if has is None:
                    raise RiverError(f"target {name} has no {event} hook; its hooks: maxpm target hook {name}")
                conn.execute("DELETE FROM hooks WHERE target_id=? AND event=?", (t["id"], event))
                _event(conn, None, actor, f"target {name} hook {event} removed (was: {has['command']})")
                changed = "removed"
            elif command is not None:
                command = command.strip()
                if not command:
                    raise RiverError(f"the command is empty; to remove the hook: maxpm target hook {name} {event} --clear")
                conn.execute("INSERT INTO hooks(target_id,event,command,set_by,set_at) VALUES (?,?,?,?,?) "
                             "ON CONFLICT(target_id,event) DO UPDATE SET command=excluded.command, "
                             "set_by=excluded.set_by, set_at=excluded.set_at", (t["id"], event, command, actor, iso(now())))
                _event(conn, None, actor, f"target {name} hook {event} {'changed' if has else 'set'}: {command}")
                changed = "changed" if has else "set"
    folder = _hook_folder(conn, name)
    return {"target": name, "event": event, "changed": changed, "hooks": target_hooks(conn, name),
            "events": dict(HOOK_EVENTS), "folder": folder,
            "timeout": setting(conn, "hook_timeout", project_id=_deploy_project_id(conn, name))}


def _deploy_project_id(conn, target):
    r = conn.execute("SELECT id FROM projects WHERE name=?", (f"deploy-{target}",)).fetchone()
    return r["id"] if r else None


def _hook_folder(conn, target, deploy_id=None):
    """Where a hook of a target runs: the folder of its deploy project (deploy-<target>), else of the first
    project of the target that has one, else of a project the release ships. None when none has one."""
    names = [f"deploy-{target}"] + [r["name"] for r in conn.execute(
        "SELECT name FROM projects WHERE target=? AND archived=0 ORDER BY rank, id", (target,))]
    if deploy_id:
        names += [r["name"] for r in conn.execute(
            "SELECT p.name FROM deps d JOIN items i ON i.id=d.blocked_by JOIN projects p ON p.id=i.project_id "
            "WHERE d.item_id=? ORDER BY p.rank, p.id", (deploy_id,))]
    for n in names:
        r = conn.execute("SELECT path FROM projects WHERE name=?", (n,)).fetchone()
        if r and r["path"]:
            return r["path"]
    return None


def _hook(conn, event, target, item_id, actor, deploy_id=None):
    """An event of a target occurred: when the target has a hook for it, queue one run. Inside the tx of the
    event, so an event that did not occur queues nothing. The command that caused the event runs the hook after
    that transaction (run_hooks): a hook can take minutes, and no command waits for the lock of the queue."""
    _hook_event(event)
    h = conn.execute("SELECT h.command FROM hooks h JOIN targets t ON t.id=h.target_id WHERE t.name=? AND h.event=?",
                     (target, event)).fetchone() if target else None
    if h is None:
        return
    deploy_id = deploy_id or item_id
    rev = conn.execute("SELECT cut_rev FROM items WHERE id=?", (deploy_id,)).fetchone()
    conn.execute("INSERT INTO hook_runs(target,event,command,item_id,deploy_id,rev,actor,queued_at,queued_by) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", (target, event, h["command"], item_id, deploy_id,
                                                rev["cut_rev"] if rev else None, actor, iso(now()), hook_token()))


def _review_deploy(conn, review_id):
    """The deploy item that waits on a release review, or None."""
    r = conn.execute("SELECT i.id FROM deps d JOIN items i ON i.id=d.item_id WHERE d.blocked_by=? AND i.kind='deploy' "
                     "ORDER BY i.id LIMIT 1", (review_id,)).fetchone()
    return r["id"] if r else None


def run_hooks(conn, cwd=None, token=None, orphans=False, sandbox=False):
    """Run the hooks that wait, one after the other, and return their results: those of the events that this
    command caused (token: another thread's, for maxpm serve); with orphans, those that waited
    HOOK_ORPHAN_AFTER because their command ended before it ran them. Each one runs once: the run is taken in a
    transaction first. cwd: where a hook runs when no project of the target has a folder. sandbox: this command
    runs in the sandbox of an agent session, so the hook has the limits of that sandbox; a failure says so."""
    token = token or hook_token()
    old = iso(now() - timedelta(seconds=HOOK_ORPHAN_AFTER))
    q = ("SELECT * FROM hook_runs WHERE started_at IS NULL AND (queued_by=?" + (" OR queued_at<?)" if orphans else ")")
         + " ORDER BY id LIMIT 1")
    args = (token, old) if orphans else (token,)
    out = []
    while conn.execute(q, args).fetchone() is not None:  # most commands queue none: no write, no lock
        with tx(conn):
            run = conn.execute(q, args).fetchone()
            if run is None:
                break
            conn.execute("UPDATE hook_runs SET started_at=? WHERE id=?", (iso(now()), run["id"]))
        out.append(_run_hook(conn, dict(run), cwd, sandbox))
    return out


# What a failure of a hook says when the command that ran it was in the sandbox of an agent session (#1826).
HOOK_SANDBOX = ("The command that caused the event ran in the sandbox of an agent session, so the hook had the limits "
                "of that sandbox (the folders it can write, the network). When that is the cause, the script is not at "
                "fault: a person or a session with the sandbox off runs the hook's command by hand")


def _run_hook(conn, run, cwd=None, sandbox=False):
    """Run one hook and record the result: the run, one history line on the deploy item, and, when it failed or
    reached hook_timeout, an alert to the owner of the target (with no owner: to the manager). Never an error:
    the event stands."""
    import time
    target, event, cmd, actor = run["target"], run["event"], run["command"], run["actor"]
    limit_text = setting(conn, "hook_timeout", item_id=run["deploy_id"])
    limit = parse_duration(limit_text).total_seconds()
    folder = _hook_folder(conn, target, run["deploy_id"])
    where = os.path.expanduser(folder) if folder else cwd
    env = {**os.environ, "MAXPM_EVENT": event, "MAXPM_TARGET": target, "MAXPM_REV": run["rev"] or "",
           "MAXPM_ITEM": str(run["item_id"] or ""), "MAXPM_DEPLOY": str(run["deploy_id"] or ""),
           "MAXPM_AGENT": actor or os.environ.get("MAXPM_AGENT", "")}

    def renew():  # a long hook keeps the leases of the agent whose command runs it
        if actor:
            with tx(conn):
                _touch_agent(conn, actor)

    t0 = time.monotonic()
    code, timed_out, text = None, False, ""
    try:
        if where and not os.path.isdir(where):
            raise OSError(f"the folder {where} does not exist")
        r = (HOOK_RUNNER or _run_command)(cmd, where, limit, renew, env)
        code, timed_out = r.returncode, bool(getattr(r, "timed_out", False))
        text = ((r.stdout or "") + (r.stderr or "")).strip()
    except OSError as e:
        text = f"the command did not start: {e}"
    secs = time.monotonic() - t0
    tail = "\n".join(text.splitlines()[-HOOK_TAIL:])
    took = f"{secs:.0f}s" if secs < 90 else _short(timedelta(seconds=secs))
    ok = code == 0 and not timed_out
    result = (f"stopped at hook_timeout ({limit_text}) after {took}" if timed_out
              else f"exit {code}, {took}" if code is not None else "not started")
    out = {"id": run["id"], "target": target, "event": event, "command": cmd, "item": run["item_id"],
           "deploy": run["deploy_id"], "rev": run["rev"], "folder": where, "exit_code": code, "timed_out": timed_out,
           "seconds": round(secs, 1), "ok": ok, "result": result, "output": tail, "alerted": None,
           "sandbox": bool(sandbox)}
    with tx(conn):
        conn.execute("UPDATE hook_runs SET ended_at=?, exit_code=?, timed_out=?, seconds=?, output=? WHERE id=?",
                     (iso(now()), code, int(timed_out), round(secs, 1), tail, run["id"]))
        if run["deploy_id"] and conn.execute("SELECT 1 FROM items WHERE id=?", (run["deploy_id"],)).fetchone():
            _event(conn, run["deploy_id"], actor or "maxpm", f"hook {event}: {result}"
                   + (" (in the sandbox of an agent session)" if sandbox and not ok else "") + f": {cmd}")
        if not ok:
            tg = conn.execute("SELECT owner FROM targets WHERE name=?", (target,)).fetchone()
            to = (tg["owner"] if tg and tg["owner"] else None) or active_manager(conn)
            if to and to != actor:
                _send(conn, "alert", "maxpm", f"hook {event} of target {target} failed ({result}) for "
                      f"#{run['item_id']}: {cmd}" + (f"\n{tail[-1500:]}" if tail else "")
                      + f"\nThe event stands: a hook never undoes it. Find the cause; when the release needs the "
                      f"command, run it by hand" + (f" in {where}" if where else "") + "."
                      + (f" {HOOK_SANDBOX}." if sandbox else ""), to=to, item_id=run["deploy_id"])
                out["alerted"] = to
    return out


# What maxpm serve starts for a target's deploys (maxpm target deployer):
# launch: when the deploy item is ready, alert the target owner; when there is no owner, or the owner cannot take
#   it (gone, stopped, never connected, or idle at its prompt), start a deployer session and give it the target.
# standing: as launch, and the deployer session starts as soon as the release has a deploy item, so it owns the
#   target and waits while the review runs, and deploys the moment the review passes.
# off: serve starts nothing; the owner or a person runs the deploy.
DEPLOYER_MODES = ("launch", "standing", "off")


def target_deployer(conn, name, mode, actor=None):
    if mode not in DEPLOYER_MODES:
        raise RiverError("the deployer mode is launch, standing, or off")
    with tx(conn):
        name = _target(conn, name)["name"]
        conn.execute("UPDATE targets SET deployer=? WHERE name=?", (mode, name))
        _event(conn, None, actor, f"target {name} deployer {mode}")
    return target_show(conn, name)


# The release cadence of a target (maxpm target cadence, #1571): how often it releases. Ship requests collect on
# the open deploy item, and the first step of the release (its review; the deploy item itself when there is no
# review) waits until the last release plus the cadence. The wait is an outside blocker with a time, set by
# CADENCE_BY, so every reader of the queue (claims, maxpm go, maxpm serve, the page) holds the release the same way.
CADENCE_BY = "maxpm cadence"
_CADENCE = re.compile(r"^(\d+)\s*(mo|[smhdw])$")
_CADENCE_UNITS = {"s": timedelta(seconds=1), "m": timedelta(minutes=1), "h": timedelta(hours=1),
                  "d": timedelta(days=1), "w": timedelta(days=7), "mo": timedelta(days=30)}


def parse_cadence(text):
    """A release cadence as (the stored text, a timedelta); ("", None) for off. 1w is 7 days and 1mo is 30 days."""
    text = (text or "").strip().lower()
    if text in ("", "off", "none", "0"):
        return "", None
    m = _CADENCE.match(text)
    if not m or not int(m.group(1)):
        raise RiverError(f"bad cadence {text!r}: use a number and m, h, d, w, or mo (for example 1h, 2h, 1d, 1w, 1mo), "
                         f"or off")
    return f"{int(m.group(1))}{m.group(2)}", int(m.group(1)) * _CADENCE_UNITS[m.group(2)]


def target_cadence(conn, name, value, actor=None):
    """How often the target releases (2h, 1d, 1w, 1mo; off: each release starts at once). It applies from now
    on, also to the release that collects ship requests now, unless that one has started."""
    text, _ = parse_cadence(value)
    with tx(conn):
        name = _target(conn, name)["name"]
        conn.execute("UPDATE targets SET cadence=? WHERE name=?", (text, name))
        _event(conn, None, actor, f"target {name} release cadence {text or 'off'}")
        _cadence_sync(conn)
    return target_show(conn, name)


def _span(td):
    """A time left for a person: 45m, 3h20m, and 2d04h from two days on."""
    h = max(0, int(td.total_seconds() // 3600))
    return f"{h // 24}d{h % 24:02d}h" if h >= 48 else _short(td)


def release_plan(conn, name):
    """When the next release of a target can start and what waits for it: for maxpm ship, maxpm target show, the
    deployer briefing, and the page.

    A release has a cut (#1619): from then on its deploy item holds a fixed list of items, and a later ship
    request joins the next deploy item. The cut comes when a reviewer takes the review of the release, when the
    deployer takes the deploy item, or with maxpm target cut. `current` is the deploy item that is cut and not
    done. `deploy` is the open deploy item that collects ship requests, and `start` the first step of its
    release: its open review, else the deploy item. That release `waits` while `current` is not done, and, with
    a cadence, until the last cut plus the cadence; `early` is the reason of a maxpm target release-now."""
    t = _target(conn, name)
    name = t["name"]
    text, cad = parse_cadence(t["cadence"])
    zone = setting(conn, "timezone")
    cur = conn.execute(f"SELECT id, cut_at, status, claimed_at FROM items WHERE kind='deploy' AND target=? AND status "
                       f"IN {OPEN_STATES} AND (cut_at IS NOT NULL OR status<>'open') ORDER BY id LIMIT 1", (name,)).fetchone()
    dep = conn.execute("SELECT id FROM items WHERE kind='deploy' AND target=? AND status='open' AND cut_at IS NULL "
                       "ORDER BY id LIMIT 1", (name,)).fetchone()
    # The last cut; for a release from before the cut existed, its end.
    last = conn.execute("SELECT id, COALESCE(cut_at, closed_at) at, cut_at FROM items WHERE kind='deploy' AND target=? "
                        "AND (cut_at IS NOT NULL OR (status='done' AND closed_at IS NOT NULL)) "
                        "ORDER BY COALESCE(cut_at, closed_at) DESC, id DESC LIMIT 1", (name,)).fetchone()
    out = {"target": name, "cadence": text, "deploy": dep["id"] if dep else None, "start": None, "early": None,
           "ships": [], "waits": False, "running": cur["id"] if cur else None,
           "current": {"id": cur["id"], "status": cur["status"], "cut_at": cur["cut_at"] or cur["claimed_at"],
                       "text": show_time(cur["cut_at"] or cur["claimed_at"], zone)} if cur else None,
           "last": {"id": last["id"], "at": last["at"], "text": show_time(last["at"], zone),
                    "cut": bool(last["cut_at"])} if last else None,
           "next_at": iso(parse_iso(last["at"]) + cad) if cad and last else None}
    later = out["next_at"] is not None and out["next_at"] > iso(now())  # the cadence does not permit it yet
    out["until"] = out["next_at"] if later else None
    if dep:
        revs = conn.execute("SELECT i.id, i.status FROM items i JOIN deps d ON d.blocked_by=i.id WHERE d.item_id=? "
                            "AND i.kind='review' ORDER BY i.id", (dep["id"],)).fetchall()
        out["start"] = next((r["id"] for r in revs if r["status"] == "open"), dep["id"])
        early = conn.execute("SELECT change FROM events WHERE item_id=? AND change LIKE 'release now%' "
                             "ORDER BY id DESC LIMIT 1", (dep["id"],)).fetchone()
        out["early"] = early["change"].partition(": ")[2] if early else None
        out["ships"] = [dict(r) for r in conn.execute(
            "SELECT i.id, i.title, i.status FROM deps d JOIN items i ON i.id=d.blocked_by WHERE d.item_id=? "
            "AND i.kind NOT IN ('review','deploy') ORDER BY i.id", (dep["id"],))]
        out["waits"] = not out["early"] and (cur is not None or later)
    when = show_time(out["next_at"], zone) if out["next_at"] else ""
    sooner = f"maxpm target release-now {name} --reason \"<why>\""
    lead = f"release cadence {text}: " if cad else ""
    out["text"] = out["why"] = out["reason"] = ""  # for a person; for the history; the text of the blocker
    if dep and out["early"]:
        out["text"] = f"{lead}this release of {name} goes out sooner ({out['early']})"
    elif cur is not None:
        out["text"] = (f"{lead}release #{cur['id']} of {name} runs now (cut {out['current']['text']}); the next release "
                       f"starts after it is done" + (f", and not before {when}" if later else ""))
        out["why"] = (f"release order of target {name}: release #{cur['id']} (cut {out['current']['text']}) is not "
                      f"done, and the next release starts after it"
                      + (f", not before {when} (release cadence {text})" if later else ""))
    elif later:
        out["text"] = (f"{lead}the next release of {name} can start {when} "
                       f"(in {_span(parse_iso(out['next_at']) - now())})")
        out["why"] = (f"release cadence {text} of target {name}: the last release "
                      f"{'was cut' if out['last']['cut'] else 'ended'} {out['last']['text']} (deploy #{last['id']})")
    elif cad:
        out["text"] = f"{lead}the next release of {name} can start now"
    if out["why"] and out["waits"]:
        out["reason"] = (f"{out['why']}. The target owner, the manager, or a person can release sooner, with a "
                         f"reason: {sooner}")
    return out


def _release_cut(conn, dep_id, actor, why, rev=None):
    """Cut the release of a deploy item: its list of items is fixed from now on, and a later ship request joins
    the next deploy item (ship). Inside a tx. True when this call made the cut."""
    dep = _item(conn, dep_id)
    if dep["kind"] != "deploy" or dep["cut_at"]:
        return False
    conn.execute("UPDATE items SET cut_at=?, cut_rev=? WHERE id=?", (iso(now()), rev, dep_id))
    n = conn.execute("SELECT COUNT(*) FROM deps d JOIN items i ON i.id=d.blocked_by WHERE d.item_id=? "
                     "AND i.kind NOT IN ('review','deploy')", (dep_id,)).fetchone()[0]
    _event(conn, dep_id, actor, f"release cut with {n} item{'' if n == 1 else 's'} ({why})" + (f": {rev}" if rev else "")
           + "; a later ship request joins the next deploy item")
    if rev:
        _add_note(conn, dep_id, f"[maxpm] release cut at {iso(now())}: {rev}")
    _hook(conn, "release-cut", dep["target"], dep_id, actor)
    return True


def _cut_on_claim(conn, it, actor):
    """A reviewer took the review of a release, or the deployer its deploy item: that is the cut. Inside a tx."""
    if it["kind"] == "deploy":
        _release_cut(conn, it["id"], actor, f"{actor} took the deploy item")
    elif it["kind"] == "review":
        for r in conn.execute("SELECT i.id FROM deps d JOIN items i ON i.id=d.item_id WHERE d.blocked_by=? "
                              f"AND i.kind='deploy' AND i.status IN {OPEN_STATES} ORDER BY i.id", (it["id"],)).fetchall():
            _release_cut(conn, r["id"], actor, f"{actor} took review #{it['id']}")
    _cadence_sync(conn)


def target_cut(conn, name, rev=None, actor=None):
    """Cut the collected release of a target now (maxpm target cut): the target owner, the manager, or a person
    fixes its list of items before the review starts, for example when it pins the commit that the release
    builds from (rev: that commit, or any word for it; it goes into the history and the notes of the deploy
    item). Refused while the release waits (an earlier release is not done, or the cadence): release-now first."""
    with tx(conn):
        _sweep(conn)
        tg = _target(conn, name)
        name = tg["name"]
        plan = release_plan(conn, name)
        who = conn.execute("SELECT kind, role FROM agents WHERE name=?", (actor,)).fetchone() if actor else None
        if actor and actor != tg["owner"] and not (who and (who["kind"] == "human" or who["role"] == "manager")):
            raise RiverError(f"refused: the target owner ({tg['owner'] or 'nobody'}), the manager, or a person cuts a "
                             f"release of {name}" + ("" if tg["owner"] else f"; take the target first: maxpm target own {name}"))
        if plan["deploy"] is None or not plan["ships"]:
            raise RiverError(f"nothing is collected for {name}: ask for items to go out first "
                             f"(maxpm ship <id>, or done --ship)"
                             + (f". Release #{plan['running']} is cut already (maxpm show {plan['running']})"
                                if plan["running"] else ""))
        if plan["waits"]:
            raise RiverError(f"{plan['text']}. To cut it sooner, give the reason first: "
                             f"maxpm target release-now {name} --reason \"<why>\"")
        _release_cut(conn, plan["deploy"], actor, f"maxpm target cut by {actor or 'a person'}", (rev or "").strip() or None)
        _cadence_sync(conn)
    return dict(target_show(conn, name), cut=item_show(conn, plan["deploy"]), items=plan["ships"])


def _cadence_sync(conn):
    """Make the waits of the releases match the targets, their releases, and the time. Inside a tx; every command
    runs it (_sweep). The first step of a release that waits (release_plan: an earlier release of the target is cut
    and not done, or the cadence does not permit it yet) gets an outside blocker, until the time when it has one;
    a wait that no longer applies is cleared. A blocker that a person or an agent set on the same item stays."""
    want = {}
    for t in conn.execute("SELECT name FROM targets WHERE cadence<>'' OR name IN (SELECT target FROM items WHERE "
                          f"kind='deploy' AND status IN {OPEN_STATES})").fetchall():
        p = release_plan(conn, t["name"])
        if p["waits"]:
            want[p["start"]] = (p["until"], p["reason"], p["why"])
    t = iso(now())
    for r in conn.execute("SELECT id, blocked_until FROM items WHERE blocked_set_by=?", (CADENCE_BY,)).fetchall():
        if r["id"] not in want:
            conn.execute("UPDATE items SET blocked_reason=NULL, blocked_until=NULL, blocked_at=NULL, blocked_set_by=NULL "
                         "WHERE id=?", (r["id"],))
            _event(conn, r["id"], "maxpm", "release cadence: the wait ended; the release can start"
                   if r["blocked_until"] and r["blocked_until"] <= t else "release order: this item waits no more")
    zone = setting(conn, "timezone")
    for item_id, (until, reason, why) in want.items():
        it = _item(conn, item_id)
        mine = it["blocked_set_by"] == CADENCE_BY
        if (it["blocked_reason"] and not mine) or (mine and it["blocked_until"] == until and it["blocked_reason"] == reason):
            continue
        conn.execute("UPDATE items SET blocked_reason=?, blocked_until=?, blocked_set_by=?, "
                     "blocked_at=COALESCE(blocked_at, ?) WHERE id=?", (reason, until, CADENCE_BY, t, item_id))
        if not mine or it["blocked_until"] != until:
            _event(conn, item_id, "maxpm", why + (f"; this release waits until {show_time(until, zone)}" if until else ""))


def cadence_holds(conn, dep_id):
    """The item of this deploy item's release that waits for the cadence (its review, or itself), or None."""
    r = conn.execute("SELECT id FROM items WHERE blocked_set_by=? AND status='open' AND (id=? OR id IN "
                     "(SELECT blocked_by FROM deps WHERE item_id=?)) ORDER BY id LIMIT 1",
                     (CADENCE_BY, dep_id, dep_id)).fetchone()
    return r["id"] if r else None


def release_now(conn, target, reason, actor=None):
    """Start the collected release sooner than the cadence permits (maxpm target release-now): for a fix of a
    production defect, or when a person asks. The target owner, the manager, or a person decides it, by itself:
    no approval is necessary, but a worker cannot, or each one would send its own change out. The reason goes in
    the history of the deploy item, and the target owner is told. It counts for this release only; the cadence
    then counts from the end of this release."""
    reason = (reason or "").strip()
    with tx(conn):
        _sweep(conn)
        tg = _target(conn, target)
        name = tg["name"]
        plan = release_plan(conn, name)
        if not plan["cadence"] and not plan["waits"]:
            raise RiverError(f"target {name} has no release cadence and no release that runs, so its next release "
                             f"starts at once; set a cadence: maxpm target cadence {name} 2h")
        if plan["deploy"] is None or not plan["ships"]:
            raise RiverError(f"nothing is collected for {name}: ask for items to go out first "
                             f"(maxpm ship <id>, or done --ship)")
        if not plan["waits"]:
            raise RiverError(f"{plan['text']}; nothing waits for the cadence. See: maxpm show {plan['start']}")
        who = conn.execute("SELECT kind, role FROM agents WHERE name=?", (actor,)).fetchone() if actor else None
        if actor and actor != tg["owner"] and not (who and (who["kind"] == "human" or who["role"] == "manager")):
            boss = active_manager(conn)
            ask = tg["owner"] or boss
            raise RiverError(
                f"refused: a release of {name} sooner than its cadence or its release order permits is a decision of the "
                f"target owner ({tg['owner'] or 'nobody'}), the manager" + (f" ({boss})" if boss else "")
                + ", or a person. " + (f"Ask: maxpm alert {ask} \"<why this release cannot wait>\" --item {plan['deploy']}"
                                       if ask else f"Take the target first: maxpm target own {name}"))
        if not reason:
            raise RiverError(f"say why this release cannot wait ({plan['text']}): "
                             f"maxpm target release-now {name} --reason \"<why>\"")
        due = "; ".join(x for x in (
            f"release #{plan['running']} is not done" if plan["running"] else "",
            f"the cadence {plan['cadence']} permits the next release {show_time(plan['until'], setting(conn, 'timezone'))}"
            if plan["until"] else "") if x)
        # "release now" at the start marks this deploy item for release_plan: it waits no more.
        _event(conn, plan["deploy"], actor, f"release now ({due}): {reason}")
        conn.execute("UPDATE items SET blocked_reason=NULL, blocked_until=NULL, blocked_at=NULL, blocked_set_by=NULL "
                     "WHERE id=? AND blocked_set_by=?", (plan["start"], CADENCE_BY))
        if plan["start"] != plan["deploy"]:
            _event(conn, plan["start"], actor, f"release cadence: released sooner: {reason}")
        if tg["owner"] and tg["owner"] != actor:
            _send(conn, "notice", actor or "maxpm", f"release now: deploy #{plan['deploy']} for {name} goes out "
                  f"sooner ({due}): {reason}", to=tg["owner"], item_id=plan["deploy"])
    return dict(release_plan(conn, name), item=item_show(conn, plan["start"]))


def owner_can_deploy(conn, owner):
    """Whether a target owner takes a ready deploy item when it gets an alert: (True, None), or (False, why).
    It can when it waits for work (maxpm wait wakes on the alert), holds an item (its next command shows the
    alert), is a person or the manager, ran a maxpm command within idle_after, or was just started and has
    connect_within to connect. It cannot when it is gone or stopped, never connected, or holds nothing and ran
    no command for idle_after: then it sits at its prompt, and river never types into it."""
    a = conn.execute("SELECT * FROM agents WHERE name=?", (owner,)).fetchone()
    if a is None:
        return False, f"{owner} is not registered"
    if a["kind"] == "human" or a["role"] == "manager" or owner in waits_on_person(conn, {owner}):
        return True, None  # one that waits on a person's answer (a production approval) keeps the target
    state = _agent_state(conn, a)
    if state in ("gone", "stopped"):
        return False, f"{owner} is {state}"
    quiet = now() - parse_iso(a["last_seen"])
    if a["last_seen"] == a["registered_at"]:
        if quiet < parse_duration(setting(conn, "connect_within")):
            return True, None
        return False, f"{owner} never connected"
    if a["role"] == "waiting" or quiet < parse_duration(setting(conn, "idle_after")):
        return True, None
    if conn.execute("SELECT 1 FROM items WHERE assignee=? AND status IN ('in_progress','held')", (owner,)).fetchone():
        return True, None
    return False, f"{owner} holds nothing and ran no maxpm command for {_short(quiet)}: idle at its prompt"


def waits_on_person(conn, names=None):
    """{agent: question id} for the agents (among names; else every AI agent) that wait at their prompt for a
    person's answer: an open question (maxpm ask) from the agent to a person, about an item it holds or the open
    deploy item of a target it owns. The person may answer in the agent's terminal (an approval the auto mode
    classifier refused to the agent), so such an agent is not idle: maxpm serve keeps its leases and its target,
    takes nothing back, starts no other session for its work, and alerts the manager (tell_manager_waits). Not an
    agent that is stopped, or whose process on this computer ended."""
    out = {}
    for r in conn.execute(
            "SELECT m.id, m.from_agent, a.pid, a.host, a.stop_at FROM messages m "
            "JOIN agents a ON a.name=m.from_agent AND a.kind='ai' JOIN agents p ON p.name=m.to_agent AND p.kind='human' "
            "JOIN items i ON i.id=m.item_id WHERE m.kind='question' AND m.state='open' "
            "AND ((i.assignee=m.from_agent AND i.status IN ('in_progress','held')) OR (i.kind='deploy' "
            "AND i.status IN ('open','in_progress','held') AND i.target IN (SELECT name FROM targets WHERE owner=m.from_agent))) "
            "ORDER BY m.id").fetchall():
        if (names is not None and r["from_agent"] not in names) or r["stop_at"] \
                or (r["pid"] and r["host"] == this_host() and not pid_alive(r["pid"])):
            continue
        out.setdefault(r["from_agent"], r["id"])
    return out


def tell_manager_waits(conn, waiting):
    """maxpm serve: for each agent that waits on a person's answer (waits_on_person) and ran no river command for
    idle_after, the active manager gets one alert for that question. Returns the agents it told about."""
    boss = active_manager(conn)
    after = parse_duration(setting(conn, "idle_after"))
    told = []
    if not boss:
        return told  # the person has the question (needs you); nobody else to tell
    with tx(conn):
        for agent, qid in sorted(waiting.items()):
            a = conn.execute("SELECT last_seen FROM agents WHERE name=?", (agent,)).fetchone()
            if not a or now() - parse_iso(a["last_seen"]) < after or conn.execute(
                    "SELECT 1 FROM messages WHERE reply_to=? AND from_agent='maxpm' AND kind='alert'", (qid,)).fetchone():
                continue
            q = _message(conn, qid)
            _send(conn, "alert", "maxpm", f"{agent} waits at its prompt for {q['to_agent']}'s answer to question #{qid} "
                  f"(#{q['item_id']}): {q['body'][:200]}. maxpm keeps its lease and its target and starts no other "
                  f"session for its work. {q['to_agent']} answers in its terminal, or: maxpm answer {qid} \"...\"",
                  to=boss, item_id=q["item_id"], reply_to=qid)
            told.append(agent)
    return told


def target_handoff(conn, name, to, why):
    """maxpm serve gives a target to the deployer session it starts; the old owner, if any, is told. Inside a tx."""
    t = _target(conn, name)
    conn.execute("UPDATE targets SET owner=?, owner_expires_at=? WHERE name=?",
                 (to, iso(now() + parse_duration(setting(conn, "owner_ttl", agent=to))), t["name"]))
    _event(conn, None, "maxpm", f"target {t['name']} given to {to}" + (f" (was {t['owner']})" if t["owner"] else "")
           + f": {why}")
    if t["owner"] and t["owner"] != to:
        _send(conn, "notice", "maxpm", f"maxpm serve gave target {t['name']} to {to}: {why}. You no longer run its "
              f"deploys; a person can give it back: maxpm target give {t['name']} --to {t['owner']}", to=t["owner"])


def live_sessions(conn):
    """How many agent sessions are live now (not gone, not stopped), for max_sessions."""
    return sum(1 for r in conn.execute("SELECT * FROM agents WHERE kind='ai'")
               if _agent_state(conn, r) not in ("gone", "stopped"))


def _add_monitor(conn, dep, actor):
    """When a deploy starts: a monitor item for this release, if the target has a monitor text and none exists."""
    tg = conn.execute("SELECT * FROM targets WHERE name=?", (dep["target"],)).fetchone()
    if not tg or not tg["monitor"] or conn.execute(
            "SELECT 1 FROM items WHERE kind='monitor' AND found_during=?", (dep["id"],)).fetchone():
        return None
    ships = conn.execute("SELECT i.id, i.title FROM deps d JOIN items i ON i.id=d.blocked_by WHERE d.item_id=? "
                         "AND i.kind NOT IN ('review') ORDER BY i.id", (dep["id"],)).fetchall()
    context = (tg["monitor"] + "\nRelease: deploy #" + str(dep["id"]) + " of " + tg["name"]
               + (" ships " + "; ".join(f"#{r['id']} {r['title']}" for r in ships) if ships else ""))
    top = conn.execute("SELECT COALESCE(MAX(rank),0) m FROM items WHERE project_id=?", (dep["project_id"],)).fetchone()["m"]
    cur = conn.execute(
        "INSERT INTO items(project_id,title,notes,priority,rank,doer,context,kind,target,found_during,created_at) "
        "VALUES (?,?,?,?,?,'ai',?,'monitor',?,?,?)",
        (dep["project_id"], f"Monitor the {tg['name']} deploy #{dep['id']}",
         "Follow the deploy as the target's monitor text says; done when all is well, else alert and propose a rollback.",
         dep["priority"], top + 1, context, tg["name"], dep["id"], iso(now())))
    _event(conn, cur.lastrowid, actor, f"monitor for deploy #{dep['id']} ({tg['name']}) added")
    _event(conn, dep["id"], actor, f"found work: #{cur.lastrowid} monitor")
    return cur.lastrowid


def pending_monitors(conn):
    """Monitor items that no session holds and for which river opened no session yet."""
    return [dict(r) for r in conn.execute(
        "SELECT i.id, i.target, i.found_during FROM items i WHERE i.kind='monitor' AND i.status='open' "
        "AND i.assignee IS NULL AND i.reserved_for IS NULL AND NOT EXISTS (SELECT 1 FROM events e "
        "WHERE e.item_id=i.id AND e.change LIKE 'monitor session opened%') ORDER BY i.id")]


def target_own(conn, name, actor=None, takeover=None):
    """Become the one owner of a target. Refused while another agent owns it, unless that owner is
    away or gone and `takeover` says why: then the target moves now and the old owner is told."""
    if not actor:
        raise RiverError("owning a target needs an agent name: set MAXPM_AGENT or pass --as <name>")
    reason = (takeover or "").strip()
    with tx(conn):
        _sweep(conn)
        _agent(conn, actor)
        t = _target(conn, name)
        name = t["name"]
        prior = t["owner"] if t["owner"] and t["owner"] != actor else None
        if prior:
            owner = conn.execute("SELECT * FROM agents WHERE name=?", (prior,)).fetchone()
            state = _agent_state(conn, owner) if owner else "gone"
            left = parse_iso(t["owner_expires_at"]) - now()
            if state == "active" or takeover is None:
                hint = (f"; {prior} is {state} (last seen {owner['last_seen'] if owner else 'never'}): "
                        f"take it over with maxpm target own {name} --takeover \"<why>\""
                        if state != "active" else "")
                raise RiverError(f"refused: target {name} is owned by {prior} "
                                 f"(until {t['owner_expires_at']}, {_short(left)} left unless they renew). "
                                 f"Ask them: maxpm send question --to {prior} \"...\", "
                                 f"or they can hand it over: maxpm target give {name} --to {actor}{hint}")
            if not reason:
                raise RiverError(f"say why you take target {name} from {prior}: "
                                 f"maxpm target own {name} --takeover \"<why>\"")
        ttl = parse_duration(setting(conn, "owner_ttl", agent=actor))
        cur = conn.execute("UPDATE targets SET owner=?, owner_expires_at=? WHERE name=? AND (owner IS NULL OR owner=?)",
                           (actor, iso(now() + ttl), name, prior or actor))
        if cur.rowcount != 1:
            raise RiverError(f"target {name} changed owner while you asked; see: maxpm target show {name}")
        if prior:
            _event(conn, None, actor, f"target {name} taken over from {prior} ({state}) by {actor}: {reason}")
            _send(conn, "notice", actor, f"{actor} took over target {name} while you were {state}: {reason}. "
                  f"You no longer run its deploys; ask {actor} or a person to give it back: "
                  f"maxpm target give {name} --to {prior}", to=prior)
        elif t["owner"] != actor:
            _event(conn, None, actor, f"target {name} owned by {actor}")
    return target_show(conn, name)


def target_release(conn, name, actor=None):
    with tx(conn):
        t = _target(conn, name)
        name = t["name"]
        if t["owner"] != actor:
            raise RiverError(f"target {name} is owned by {t['owner'] or 'nobody'}, not {actor}")
        conn.execute("UPDATE targets SET owner=NULL, owner_expires_at=NULL WHERE name=?", (name,))
        _event(conn, None, actor, f"target {name} released")
    return target_show(conn, name)


def target_give(conn, name, to, actor=None):
    """Hand a target to another agent. The owner can give it, and so can a person, who decides for the
    agents: the old owner is then told."""
    with tx(conn):
        _sweep(conn)
        t = _target(conn, name)
        name = t["name"]
        giver = conn.execute("SELECT kind FROM agents WHERE name=?", (actor,)).fetchone() if actor else None
        role = conn.execute("SELECT role FROM agents WHERE name=?", (actor,)).fetchone() if actor else None
        by_person = bool(giver) and (giver["kind"] == "human" or (role and role["role"] == "manager")) \
            and t["owner"] != actor
        if t["owner"] != actor and not by_person:
            raise RiverError(f"only the owner, a person, or the manager can give target {name}; "
                             f"it is owned by {t['owner'] or 'nobody'}"
                             + ("" if t["owner"] else f" (take it: maxpm target own {name})"))
        _agent(conn, to)
        ttl = parse_duration(setting(conn, "owner_ttl", agent=to))
        conn.execute("UPDATE targets SET owner=?, owner_expires_at=? WHERE name=?", (to, iso(now() + ttl), name))
        _event(conn, None, actor, f"target {name} given to {to}"
               + (f" by {actor} (was {t['owner']})" if by_person and t["owner"] else ""))
        _send(conn, "notice", actor, f"{actor} gave you target {name}: you now run its deploys. "
              f"See: maxpm target show {name}", to=to)
        if by_person and t["owner"] and t["owner"] != to:
            _send(conn, "notice", actor, f"{actor} gave target {name} to {to}; you no longer run its deploys",
                  to=t["owner"])
    return target_show(conn, name)


def _short(td):
    m = max(0, int(td.total_seconds() // 60))
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def target_list(conn):
    return [dict(r) for r in conn.execute(
        "SELECT t.*, (SELECT COUNT(*) FROM projects p WHERE p.target=t.name AND p.archived=0) projects "
        "FROM targets t ORDER BY t.name")]


def target_show(conn, name):
    t = dict(_target(conn, name))
    name = t["name"]
    t["projects"] = [dict(r) for r in conn.execute(
        "SELECT p.name, p.rank, p.notes, (SELECT COUNT(*) FROM items i WHERE i.project_id=p.id "
        "AND i.status IN ('open','in_progress','held')) open_items "
        "FROM projects p WHERE p.target=? AND p.archived=0 ORDER BY p.rank, p.id", (name,))]
    t["release"] = release_plan(conn, name)
    t["hooks"] = target_hooks(conn, name)
    return t


def targets_view(conn, ann=None):
    """Each target with its owner, projects, open deploy items (with what they ship), and last finished deploy."""
    ann = ann if ann is not None else annotate(conn)
    ships = lambda a: [{"id": b, "title": ann[b]["title"], "status": ann[b]["status"], "project": ann[b]["project"]}
                       for b in a["waits_on"] if b in ann and ann[b]["kind"] != "review"]
    review = lambda a: next(({"id": b, "status": ann[b]["status"], "assignee": ann[b]["assignee"]}
                             for b in a["waits_on"] if b in ann and ann[b]["kind"] == "review"
                             and ann[b]["status"] in OPEN_STATES), None)
    monitors = {}
    for a in ann.values():
        if a["kind"] == "monitor" and a["found_during"]:
            monitors.setdefault(a["found_during"], []).append(
                {"id": a["id"], "status": a["status"], "assignee": a["assignee"], "output": a["output"]})
    out = []
    for t in target_list(conn):
        deploys = [a for a in ann.values() if a["kind"] == "deploy" and a["target"] == t["name"]]
        pending = sorted((a for a in deploys if a["status"] in OPEN_STATES), key=lambda a: a["id"])
        done = sorted((a for a in deploys if a["status"] == "done"), key=lambda a: a["closed_at"] or "")
        last = done[-1] if done else None
        out.append(dict(t,
            project_names=[r["name"] for r in conn.execute(
                "SELECT name FROM projects WHERE target=? AND archived=0 AND name<>? ORDER BY rank, id",
                (t["name"], f"deploy-{t['name']}"))],
            pending=[{"id": a["id"], "title": a["title"], "status": a["status"], "assignee": a["assignee"],
                      "ready": a["ready"], "ships": ships(a), "review": review(a), "cut_at": a["cut_at"],
                      "monitors": monitors.get(a["id"], [])} for a in pending],
            release=release_plan(conn, t["name"]), hooks=target_hooks(conn, t["name"]),
            last_deploy=({"id": last["id"], "title": last["title"], "closed_at": last["closed_at"],
                          "output": last["output"], "ships": ships(last)} if last else None),
            history=[{"id": a["id"], "title": a["title"], "closed_at": a["closed_at"], "output": a["output"],
                      "ships": ships(a), "done_by": _done_by(conn, a["id"]), "monitors": monitors.get(a["id"], [])}
                     for a in reversed(done[-10:])]))
    return out


def _done_by(conn, item_id):
    r = conn.execute("SELECT actor FROM events WHERE item_id=? AND change LIKE 'done%' ORDER BY id DESC LIMIT 1",
                     (item_id,)).fetchone()
    return r["actor"] if r else None


def deploy_now(conn, target, review=False, actor=None):
    """Deploy now (the page's Targets tab): the target's open deploy item should go out now.

    Refuses when it collects nothing. With review, the release first waits on one review of everything
    it ships, also when the review setting is off. Returns what to start (the review or the deploy item),
    whether it is ready, and what it still waits on; a ready deploy item alerts the target owner."""
    with tx(conn):
        _sweep(conn)
        tg = _target(conn, target)
        dep = conn.execute(f"SELECT * FROM items WHERE kind='deploy' AND target=? AND status IN {OPEN_STATES} "
                           "ORDER BY id LIMIT 1", (tg["name"],)).fetchone()
        if dep is not None and dep["status"] != "open":
            raise RiverError(f"deploy #{dep['id']} for {tg['name']} is already {dep['status'].replace('_', ' ')}"
                             + (f" by {dep['assignee']}" if dep["assignee"] else ""))
        shipped = [r["blocked_by"] for r in conn.execute(
            "SELECT d.blocked_by FROM deps d JOIN items i ON i.id=d.blocked_by WHERE d.item_id=? AND i.kind<>'review'",
            (dep["id"],))] if dep is not None else []
        if not shipped:
            raise RiverError(f"nothing is collected for {tg['name']}: ask for items to go out first "
                             f"(maxpm ship <id>, or done --ship)")
        plan = release_plan(conn, tg["name"])
        if not dep["cut_at"] and plan["waits"]:  # a release that is cut waits on nothing but its own steps
            raise RiverError(f"{plan['text']}. A release sooner needs a reason: "
                             f"maxpm target release-now {tg['name']} --reason \"<why>\"")
        rv = _release_review(conn, dep, tg, actor, force=True) if review else None
        _event(conn, dep["id"], actor, "deploy now" + (" after a review" if review else ""))
    ann = annotate(conn)
    start = ann[rv["id"] if rv is not None else dep["id"]]
    out = {"target": tg["name"], "owner": tg["owner"], "deploy": {"id": dep["id"], "title": dep["title"]},
           "review": {"id": rv["id"], "title": rv["title"]} if rv is not None else None,
           "start": {"id": start["id"], "title": start["title"], "kind": start["kind"]}, "ready": start["ready"],
           "waits_on": [{"id": b, "title": ann[b]["title"], "status": ann[b]["status"]}
                        for b in start["waits_on"] if ann[b]["status"] in OPEN_STATES]}
    if start["ready"] and start["kind"] == "deploy" and tg["owner"]:
        with tx(conn):
            _send(conn, "alert", actor or "maxpm", f"deploy now: #{dep['id']} {dep['title']} is ready; "
                  f"maxpm go gives it to you", to=tg["owner"], item_id=dep["id"])
        out["alerted"] = tg["owner"]
    return out


# ---------------------------------------------------------------- items

def _item(conn, item_id):
    r = conn.execute("SELECT * FROM items WHERE id=?", (int(item_id),)).fetchone()
    if not r:
        raise RiverError(f"no item {item_id}")
    return r


def _touches(value):
    """Files an item changes, as one path per line; accepts a list or a comma or newline separated string."""
    if value is None:
        return None
    parts = value if isinstance(value, (list, tuple)) else re.split(r"[,\n]", value)
    seen = []
    for x in (str(v).strip() for v in parts):
        if x and x not in seen:
            seen.append(x)
    return "\n".join(seen)


def touches_list(text):
    return [x for x in (text or "").split("\n") if x]


def item_add(conn, project, title, priority=2, notes="", doer="any", after=(), actor=None,
             context="", touches=None, check="", blocks=None, mode=None, found_during=None, feeds=(), due=None,
             goals=None, refs=None, models=None):
    """Add an item. `after`: items it waits on. `feeds`: items it waits on and whose output it reads."""
    if doer not in DOERS:
        raise RiverError(f"doer is one of {', '.join(DOERS)}")
    if not (0 <= int(priority) <= 4):
        raise RiverError("priority is 0 (highest) to 4 (lowest)")
    with tx(conn):
        if found_during is not None:
            _item(conn, found_during)
        p = _project(conn, project)
        top = conn.execute("SELECT COALESCE(MAX(rank),0) m FROM items WHERE project_id=?", (p["id"],)).fetchone()["m"]
        cur = conn.execute(
            'INSERT INTO items(project_id,title,notes,priority,rank,doer,context,touches,"check",created_at) '
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (p["id"], title, notes, int(priority), top + 1, doer, context or "", _touches(touches) or "",
             check or "", iso(now())))
        iid = cur.lastrowid
        _event(conn, iid, actor, f"added to {p['name']} at P{priority}")
        # Goal tags: the ones named, else the goal the actor owns in this item's project (if any).
        # goals=[] means no goal.
        names = list(goals) if goals is not None else [r["name"] for r in conn.execute(
            "SELECT name FROM goals WHERE owner=? AND status='open' AND project_id=? ORDER BY rank, id LIMIT 1",
            (actor, p["id"]))] if actor else []
        for g in names:
            _tag(conn, iid, g, actor)
        if refs:
            _add_refs(conn, iid, refs, actor)
        if models:
            _set_models(conn, conn.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone(), models, actor)
        if due:
            t = parse_due(due, setting(conn, "timezone"))
            conn.execute("UPDATE items SET due=? WHERE id=?", (t, iid))
            _event(conn, iid, actor, f"due {show_time(t, setting(conn, 'timezone'))}")
        if found_during is not None:
            conn.execute("UPDATE items SET found_during=? WHERE id=?", (int(found_during), iid))
            _event(conn, iid, actor, f"found during #{found_during}")
            _event(conn, int(found_during), actor, f"found work: #{iid} {title}")
        for b in after:
            _dep_add(conn, iid, int(b), actor)
        for b in feeds or ():
            _dep_add(conn, iid, int(b), actor, "feeds")
        _sync_conflicts(conn, iid, actor)
        if blocks is not None:
            _dep_add(conn, int(blocks), iid, actor)
            _prereq_mode(conn, int(blocks), [iid], actor, mode)
    a = item_show(conn, iid)
    if blocks is not None and doer == "human":
        parent = _item(conn, blocks)
        if parent["status"] == "held" and parent["assignee"] == actor:
            a["holder_wait"] = {"item": parent["id"], "until": parent["hold_expires_at"],
                                "max": setting(conn, "human_wait_max", item_id=parent["id"], agent=actor)}
    return a


def _set_models(conn, it, models, actor):
    """Set an item's model, effort, min_model, max_model from a dict; "" or "none" clears one (the default applies)."""
    ladder = parse_ladder(setting(conn, "model_ladder"))
    new = {f: it[f] for f in MODEL_FIELDS}
    for f, v in models.items():
        if f not in MODEL_FIELDS:
            raise RiverError(f"unknown model field {f}")
        if v is None:
            continue
        v = v.strip()
        if v.lower() in ("", "none"):
            new[f] = None
        elif f == "model":
            new[f] = ladder_name(conn, _check_model_name(v))
        elif f == "effort":
            new[f] = _check_effort(conn, v)
        elif f == "agent":
            new[f] = _check_agent_type(conn, v)
        else:
            new[f] = _limit_list(ladder, v, f.replace("_", " "))
    _check_limits(ladder, new["min_model"], new["max_model"])
    if new["agent"]:
        # Codex runs OpenAI models and Claude Code runs Claude models: the item's own model and limits must fit.
        fam = LAUNCH_PLATFORMS[new["agent"]]["family"]
        for f in ("model", "min_model", "max_model"):
            for m in _levels(new[f]):
                mf, _ = _family(ladder, m)
                if mf and mf != fam and (f == "model" or len(_levels(new[f])) == 1):
                    raise RiverError(f"#{it['id']} is for {new['agent']}, which runs {fam} models "
                                     f"({', '.join(ladder.get(fam, []))}); its {f.replace('_', ' ')} {m} is {mf}")
    for f in MODEL_FIELDS:
        if new[f] != it[f]:
            conn.execute(f"UPDATE items SET {f}=? WHERE id=?", (new[f], it["id"]))
            _event(conn, it["id"], actor, f"{f.replace('_', ' ')} {new[f] or 'unset'}")


REF_RE = re.compile(r"^[a-z][a-z0-9_.-]*:\S+$")


def ref_url(ref):
    """The web link a ref implies without settings: GitHub issues only."""
    m = re.match(r"^github:([\w.-]+/[\w.-]+)#(\d+)$", ref)
    return f"https://github.com/{m.group(1)}/issues/{m.group(2)}" if m else None


def parse_refs(refs, urls=()):
    """Pair --ref values with --ref-url values by position; check the form <tracker>:<key>."""
    refs, urls = [r.strip() for r in refs or ()], [u.strip() for u in urls or ()]
    if len(urls) > len(refs):
        raise RiverError("more --ref-url than --ref: give each URL after the ref it belongs to")
    out = []
    for i, r in enumerate(refs):
        if not REF_RE.match(r):
            raise RiverError(f"ref {r!r} is not <tracker>:<key>, for example jira:PROJ-123, github:owner/repo#12, "
                             f"linear:ENG-42")
        u = urls[i] if i < len(urls) and urls[i] else ref_url(r)
        if u and not re.match(r"^https?://\S+$", u):
            raise RiverError(f"ref URL {u!r} is not a web link")
        out.append((r, u))
    return out


def _add_refs(conn, item_id, pairs, actor):
    """Link refs to an item. Refused when an open item in the same project already has the ref."""
    it = _item(conn, item_id)
    for ref, url in pairs:
        other = conn.execute(f"SELECT i.id, i.title FROM item_refs r JOIN items i ON i.id=r.item_id WHERE r.ref=? "
                             f"AND i.id<>? AND i.project_id=? AND i.status IN {OPEN_STATES}",
                             (ref, it["id"], it["project_id"])).fetchone()
        if other:
            raise RiverError(f"{ref} is already linked to #{other['id']} {other['title']}, which is open in the "
                             f"same project: maxpm show {other['id']}")
        old = conn.execute("SELECT url FROM item_refs WHERE item_id=? AND ref=?", (it["id"], ref)).fetchone()
        if old is None:
            conn.execute("INSERT INTO item_refs(item_id, ref, url, created_at) VALUES (?,?,?,?)",
                         (it["id"], ref, url, iso(now())))
            _event(conn, it["id"], actor, f"linked {ref}")
        elif url and url != old["url"]:
            conn.execute("UPDATE item_refs SET url=? WHERE item_id=? AND ref=?", (url, it["id"], ref))
            _event(conn, it["id"], actor, f"link {ref} URL changed")


def project_for_add(conn, cwd, related=None):
    """The project a new item goes to when none is named: the related item's, else the folder's."""
    if related is not None:
        return _project_name(conn, _item(conn, related)["project_id"])
    names = projects_for_dir(conn, cwd)
    if len(names) == 1:
        return names[0]
    if names:
        raise RiverError(f"this folder belongs to {', '.join(names)}; name one: maxpm add <project> \"<title>\"")
    raise RiverError("no project named and this folder is not linked to one; "
                     "use: maxpm add <project> \"<title>\" (or --blocks/--found-during <id>)")


def _project_name(conn, pid):
    return conn.execute("SELECT name FROM projects WHERE id=?", (pid,)).fetchone()["name"]


_PLAN_BULLET = re.compile(r"^(?:[-*+]|\d+[.)])\s+")
_PLAN_BOX = re.compile(r"^\[( |x|X)\]\s+")
_PLAN_PRIO = re.compile(r"^[Pp]([0-4])\b[:\s]*")
_PLAN_DOER = re.compile(r"\s*\((human|ai|agent|anyone|any)\)\s*$", re.I)


def parse_plan(text, priority=2, doer="any"):
    """Lines of a plan file as items, read as an outline: one item per line, and a line waits on the
    lines indented under it (its steps come first; `parent` is the line it is a step of). Markdown bullets, numbers, and '[ ]' boxes are dropped; a '[x]' line
    is skipped (already done), and so are blank lines, '#' headings, and '>' quotes. A leading 'P0'..'P4'
    sets the priority; a trailing '(human)', '(ai)', or '(anyone)' sets who can do it."""
    rows, stack = [], []  # stack: (indent, row index)
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.expandtabs(4)
        body = line.strip()
        if not body or body.startswith(("#", ">")):
            continue
        indent = len(line) - len(line.lstrip())
        body = _PLAN_BULLET.sub("", body)
        box = _PLAN_BOX.match(body)
        if box:
            body = body[box.end():]
        m = _PLAN_PRIO.match(body)
        prio = int(m.group(1)) if m else priority
        body = body[m.end():] if m else body
        d = _PLAN_DOER.search(body)
        who = doer if not d else {"agent": "ai", "anyone": "any"}.get(d.group(1).lower(), d.group(1).lower())
        body = body[:d.start()] if d else body
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1] if stack else None
        if box and box.group(1) in "xX":
            continue  # done already; its children wait on nothing from it
        if not body.strip():
            raise RiverError(f"plan line {n} has no title: {raw.strip()!r}")
        rows.append({"line": n, "title": body.strip(), "priority": prio, "doer": who, "parent": parent})
        stack.append((indent, len(rows) - 1))
    if not rows:
        raise RiverError("the plan has no items: one item per line; indent a line to make it wait on the line above")
    return rows


def add_plan(conn, project, text, actor=None, priority=2, doer="any", dry_run=False):
    """Add every item of a plan file (see parse_plan) to one project; each line waits on its steps."""
    rows = parse_plan(text, priority, doer)
    project = _project(conn, project)["name"]
    if dry_run:
        return {"project": project, "dry_run": True, "items": rows}
    ids = [item_add(conn, project, r["title"], r["priority"], "", r["doer"], (), actor)["id"] for r in rows]
    for r, i in zip(rows, ids):
        r["id"] = i
        if r["parent"] is not None:
            dep_add(conn, ids[r["parent"]], [i], actor)
    return {"project": project, "dry_run": False, "items": rows}


def item_edit(conn, item_id, title=None, notes=None, doer=None, project=None, actor=None,
              context=None, touches=None, check=None, due=None, goals=None, untag=None, refs=None, unref=None,
              models=None):
    with tx(conn):
        it = _item(conn, item_id)
        if models:
            _set_models(conn, it, models, actor)
        if refs:
            _add_refs(conn, it["id"], refs, actor)
        for r in unref or ():
            if not conn.execute("DELETE FROM item_refs WHERE item_id=? AND ref=?", (it["id"], r.strip())).rowcount:
                raise RiverError(f"#{it['id']} has no link {r}")
            _event(conn, it["id"], actor, f"unlinked {r.strip()}")
        for g in goals or ():
            _tag(conn, it["id"], g, actor)
        for g in untag or ():
            gid = _goal(conn, g)["id"]
            if conn.execute("DELETE FROM item_goals WHERE item_id=? AND goal_id=?", (it["id"], gid)).rowcount:
                _event(conn, it["id"], actor, f"untagged goal {g}")
        if due is not None:
            zone = setting(conn, "timezone")
            t = parse_due(due, zone)
            if t != it["due"]:
                conn.execute("UPDATE items SET due=?, due_warned=0 WHERE id=?", (t, it["id"]))
                _event(conn, it["id"], actor, f"due {show_time(t, zone)}" if t else "due date removed")
        if title is not None and title != it["title"]:
            conn.execute("UPDATE items SET title=? WHERE id=?", (title, it["id"]))
            _event(conn, it["id"], actor, "title changed")
        if notes is not None and notes != it["notes"]:
            conn.execute("UPDATE items SET notes=? WHERE id=?", (notes, it["id"]))
            _event(conn, it["id"], actor, "notes changed")
        if doer is not None and doer != it["doer"]:
            if doer not in DOERS:
                raise RiverError(f"doer is one of {', '.join(DOERS)}")
            conn.execute("UPDATE items SET doer=? WHERE id=?", (doer, it["id"]))
            _event(conn, it["id"], actor, f"doer {it['doer']} -> {doer}")
        if project is not None and _project(conn, project)["id"] != it["project_id"]:
            p = _project(conn, project)
            conn.execute("UPDATE items SET project_id=? WHERE id=?", (p["id"], it["id"]))
            _event(conn, it["id"], actor, f"moved to project {p['name']}")
        for col, val in (("context", context), ("touches", _touches(touches)), ("check", check)):
            if val is not None and val != it[col]:
                conn.execute(f'UPDATE items SET "{col}"=? WHERE id=?', (val, it["id"]))
                _event(conn, it["id"], actor, f"{col} changed")
                if col == "touches":
                    _sync_conflicts(conn, it["id"], actor)
    return item_show(conn, item_id)


def item_prio(conn, item_id, priority, actor=None):
    if not (0 <= int(priority) <= 4):
        raise RiverError("priority is 0 (highest) to 4 (lowest)")
    with tx(conn):
        it = _item(conn, item_id)
        conn.execute("UPDATE items SET priority=? WHERE id=?", (int(priority), it["id"]))
        _event(conn, it["id"], actor, f"priority P{it['priority']} -> P{priority}")
    return item_show(conn, item_id)


def item_move(conn, item_id, before=None, after=None, actor=None):
    """Change the manual order inside a project: place the item just before or after another."""
    if (before is None) == (after is None):
        raise RiverError("give exactly one of --before or --after")
    with tx(conn):
        it = _item(conn, item_id)
        ref = _item(conn, before if before is not None else after)
        if ref["project_id"] != it["project_id"]:
            raise RiverError("manual order applies inside one project; the two items are in different projects")
        ranks = [r["rank"] for r in conn.execute(
            "SELECT rank FROM items WHERE project_id=? AND id<>? ORDER BY rank", (it["project_id"], it["id"]))]
        i = ranks.index(ref["rank"])
        if before is not None:
            lo = ranks[i - 1] if i > 0 else ref["rank"] - 1
            new = (lo + ref["rank"]) / 2
        else:
            hi = ranks[i + 1] if i + 1 < len(ranks) else ref["rank"] + 1
            new = (ref["rank"] + hi) / 2
        conn.execute("UPDATE items SET rank=? WHERE id=?", (new, it["id"]))
        _event(conn, it["id"], actor, f"moved {'before' if before is not None else 'after'} {ref['id']}")
    return item_show(conn, item_id)


def _reachable(conn, start, edges_sql):
    seen, stack = set(), [start]
    while stack:
        x = stack.pop()
        for r in conn.execute(edges_sql, (x,)):
            y = r[0]
            if y not in seen:
                seen.add(y)
                stack.append(y)
    return seen


def _goal_work(conn, item_id, actor):
    """Whether an item serves the actor's own outcome: it carries a goal the actor owns, an open item of such
    a goal waits on it, or it deploys a target the actor owns. Such items count toward goal_max_leases."""
    if not actor:
        return False
    it = _item(conn, item_id)
    if it["kind"] == "deploy" and conn.execute("SELECT 1 FROM targets WHERE name=? AND owner=?",
                                               (it["target"], actor)).fetchone():
        return True
    tagged = {r[0] for r in conn.execute(
        "SELECT ig.item_id FROM item_goals ig JOIN goals g ON g.id=ig.goal_id WHERE g.owner=? AND g.status='open' "
        "AND g.owner_expires_at >= ?", (actor, iso(now())))}
    if not tagged:
        return False
    if it["id"] in tagged:
        return True
    return bool(tagged & _reachable(conn, it["id"], "SELECT d.item_id FROM deps d JOIN items i ON i.id=d.item_id "
                                    "WHERE d.blocked_by=? AND d.kind<>'conflicts' AND i.status IN ('open','in_progress','held')"))


def _lease_room(conn, item_id, actor, states=("in_progress", "held")):
    """(held, limit, key): the items the actor holds that count against the same limit as this item."""
    goal = _goal_work(conn, item_id, actor)
    key = "goal_max_leases" if goal else "max_leases"
    rows = conn.execute(f"SELECT id FROM items WHERE assignee=? AND status IN ({','.join('?' * len(states))})",
                        (actor, *states)).fetchall()
    held = sum(1 for r in rows if _goal_work(conn, r["id"], actor) == goal)
    return held, int(setting(conn, key, agent=actor)), key


def _link(conn, a, b):
    """Any dependency row between two items, in either direction."""
    return conn.execute("SELECT * FROM deps WHERE (item_id=? AND blocked_by=?) OR (item_id=? AND blocked_by=?)",
                        (a, b, b, a)).fetchone()


def _dep_add(conn, item_id, blocked_by, actor, kind="blocks", auto=False, alert=True):
    if kind not in DEP_KINDS:
        raise RiverError(f"dependency kind is one of {', '.join(DEP_KINDS)}")
    _item(conn, item_id)
    _item(conn, blocked_by)
    if item_id == blocked_by:
        raise RiverError("an item cannot wait on itself")
    if kind == "conflicts":
        # Symmetric and without order. An order between the two already keeps them apart.
        lo, hi = sorted((item_id, blocked_by))
        link = _link(conn, lo, hi)
        if link and link["kind"] != "conflicts":
            if auto:
                return
            raise RiverError(f"#{link['item_id']} already waits on #{link['blocked_by']} ({link['kind']}), "
                             f"so they never run at the same time; no conflict link needed")
        if link:
            return
        conn.execute("INSERT INTO deps(item_id,blocked_by,kind,auto) VALUES (?,?,?,?)", (lo, hi, kind, int(auto)))
        _event(conn, lo, actor, f"conflicts with {hi}" + (" (touches overlap)" if auto else ""))
        _event(conn, hi, actor, f"conflicts with {lo}" + (" (touches overlap)" if auto else ""))
        return
    # Adding item -> blocked_by makes a cycle when item is already a prerequisite of blocked_by.
    upstream_of_blocker = _reachable(conn, blocked_by, "SELECT blocked_by FROM deps WHERE item_id=? AND kind<>'conflicts'")
    if item_id in upstream_of_blocker:
        raise RiverError(f"refused: item {blocked_by} already waits on item {item_id} (directly or through other items); "
                         f"this dependency would make a loop")
    # An order replaces a conflict link between the same two items.
    conn.execute("DELETE FROM deps WHERE kind='conflicts' AND ((item_id=? AND blocked_by=?) OR (item_id=? AND blocked_by=?))",
                 (item_id, blocked_by, blocked_by, item_id))
    conn.execute("INSERT INTO deps(item_id,blocked_by,kind) VALUES (?,?,?) "
                 "ON CONFLICT(item_id,blocked_by) DO UPDATE SET kind=excluded.kind, auto=0", (item_id, blocked_by, kind))
    _event(conn, item_id, actor, f"waits on {blocked_by}" + (" (feeds)" if kind == "feeds" else ""))
    if alert:
        _alert_new_prereq(conn, item_id, blocked_by, actor)


def _alert_new_prereq(conn, item_id, blocked_by, actor):
    """An open prerequisite added to an item someone holds, or to a deploy item, is news that must stop
    them: alert the holder and the deploy target's owner (not whoever added it)."""
    it, b = _item(conn, item_id), _item(conn, blocked_by)
    if it["status"] not in OPEN_STATES or b["status"] not in OPEN_STATES:
        return
    to = []
    if it["assignee"] and it["status"] in ("in_progress", "held"):
        to.append(it["assignee"])
    if it["kind"] == "deploy" and it["target"]:
        tg = conn.execute("SELECT owner FROM targets WHERE name=?", (it["target"],)).fetchone()
        if tg and tg["owner"]:
            to.append(tg["owner"])
    for who in dict.fromkeys(to):
        if who == actor:
            continue
        what = "deploy" if it["kind"] == "deploy" else "item"
        _send(conn, "alert", actor or "maxpm",
              f"#{b['id']} {b['title']} was added before your {what} #{it['id']} {it['title']}. "
              f"Stop and wait for it: maxpm done {it['id']} is refused while it is open (maxpm blockers {it['id']}).",
              to=who, item_id=it["id"])


def dep_add(conn, item_id, on, actor=None, kind="blocks", mode=None):
    """Link items. With mode keep or release, also hold or release the parent (design 7.3)."""
    with tx(conn):
        for b in on:
            _dep_add(conn, int(item_id), int(b), actor, kind)
        if mode and kind != "conflicts":
            _prereq_mode(conn, int(item_id), [int(b) for b in on], actor, mode)
    return item_show(conn, item_id)


def dep_remove(conn, item_id, on, actor=None):
    with tx(conn):
        for b in on:
            i, b = int(item_id), int(b)
            conn.execute("DELETE FROM deps WHERE (item_id=? AND blocked_by=?) "
                         "OR (kind='conflicts' AND item_id=? AND blocked_by=?)", (i, b, b, i))
            _event(conn, i, actor, f"no longer linked to {b}")
    return item_show(conn, item_id)


def _norm_path(p):
    """One form for comparing paths: forward slashes (Windows gives backslashes), and no case on Windows."""
    p = p.replace("\\", "/")
    return p.lower() if os.name == "nt" else p


def _paths_overlap(a, b):
    """Same file, or one path is a directory that holds the other."""
    a, b = _norm_path(a).rstrip("/"), _norm_path(b).rstrip("/")
    return a == b or b.startswith(a + "/") or a.startswith(b + "/")


def _touch_keys(touches, project_id, root):
    """Touches as comparable keys: a full path when the project has a folder (or the touch is absolute),
    else the relative path scoped to its project, so two repositories' 'public/' never meet."""
    r = os.path.realpath(os.path.expanduser(root)) if root else None
    keys = []
    for t in touches_list(touches):
        p = os.path.expanduser(t)
        if os.path.isabs(p):
            keys.append((None, os.path.realpath(p)))
        elif r:
            keys.append((None, os.path.normpath(os.path.join(r, p))))
        else:
            keys.append((project_id, os.path.normpath(p)))
    return keys


def _keys_overlap(a, b):
    return a[0] == b[0] and _paths_overlap(a[1], b[1])


def _sync_conflicts(conn, item_id, actor):
    """Keep automatic conflict links equal to the open items whose touches overlap this item's."""
    it = _item(conn, item_id)
    root = lambda pid: conn.execute("SELECT path FROM projects WHERE id=?", (pid,)).fetchone()["path"]
    mine = _touch_keys(it["touches"], it["project_id"], root(it["project_id"]))
    want = set()
    if it["status"] in OPEN_STATES and mine:
        for r in conn.execute(f"SELECT i.id, i.touches, i.project_id, p.path FROM items i JOIN projects p "
                              f"ON p.id=i.project_id WHERE i.id<>? AND i.status IN {OPEN_STATES} AND i.touches<>''",
                              (it["id"],)):
            theirs = _touch_keys(r["touches"], r["project_id"], r["path"])
            if any(_keys_overlap(x, y) for x in mine for y in theirs):
                want.add(r["id"])
    have = {r["item_id"] if r["blocked_by"] == it["id"] else r["blocked_by"] for r in conn.execute(
        "SELECT item_id, blocked_by FROM deps WHERE kind='conflicts' AND auto=1 AND (item_id=? OR blocked_by=?)",
        (it["id"], it["id"]))}
    for other in have - want:
        lo, hi = sorted((it["id"], other))
        conn.execute("DELETE FROM deps WHERE item_id=? AND blocked_by=? AND kind='conflicts'", (lo, hi))
        _event(conn, it["id"], actor, f"no longer conflicts with {other} (touches no longer overlap)")
    for other in sorted(want - have):
        _dep_add(conn, it["id"], other, actor, "conflicts", auto=True)


def _open_prereqs(conn, item_id):
    return [r["id"] for r in conn.execute(
        "SELECT i.id FROM deps d JOIN items i ON i.id=d.blocked_by "
        f"WHERE d.item_id=? AND d.kind<>'conflicts' AND i.status IN {OPEN_STATES}", (item_id,))]


def _human_prereqs(conn, item_id):
    """Open items for a person that this item waits on."""
    return [r["id"] for r in conn.execute(
        "SELECT i.id FROM deps d JOIN items i ON i.id=d.blocked_by "
        f"WHERE d.item_id=? AND d.kind<>'conflicts' AND i.doer='human' AND i.status IN {OPEN_STATES} ORDER BY i.id",
        (item_id,))]


def _hold_until(conn, item_id, actor, t):
    """When a hold ends: hold_ttl from t, but while a person's item is open before the held item, at most
    human_wait_max after the agent began to wait on it (the later of the hold and that item)."""
    until = t + parse_duration(setting(conn, "hold_ttl", item_id=item_id, agent=actor))
    human = _human_prereqs(conn, item_id)
    if human:
        held_at = conn.execute("SELECT held_at FROM items WHERE id=?", (item_id,)).fetchone()["held_at"]
        made = min(conn.execute(f"SELECT created_at FROM items WHERE id IN ({','.join('?' * len(human))})",
                                human).fetchall(), key=lambda r: r["created_at"])["created_at"]
        start = max(parse_iso(held_at) if held_at else t, parse_iso(made))
        until = min(until, start + parse_duration(setting(conn, "human_wait_max", item_id=item_id, agent=actor)))
    return until


def _hold(conn, parent, actor, reserve):
    t = now()
    conn.execute("UPDATE items SET held_at=CASE WHEN status='held' AND assignee=? AND held_at IS NOT NULL "
                 "THEN held_at ELSE ? END, status='held', assignee=?, lease_expires_at=NULL WHERE id=?",
                 (actor, iso(t), actor, parent))
    conn.execute("UPDATE items SET hold_expires_at=? WHERE id=?", (iso(_hold_until(conn, parent, actor, t)), parent))
    for r in reserve:
        conn.execute("UPDATE items SET reserved_for=? WHERE id=? AND status='open' AND reserved_for IS NULL", (actor, r))
    _event(conn, parent, actor, f"held by {actor} while it does " + ", ".join(f"#{r}" for r in reserve) if reserve
           else f"held by {actor} until its prerequisites are done")


def _unhold(conn, parent, actor, why):
    """Turn a hold into a release: the parent is open to everyone and its reservations end."""
    it = _item(conn, parent)
    conn.execute("UPDATE items SET status='open', assignee=NULL, claimed_at=NULL, lease_expires_at=NULL, "
                 "hold_expires_at=NULL WHERE id=?", (parent,))
    if it["assignee"]:
        # A pushed item stays reserved for the agent that claimed it; given back, it is nobody's.
        conn.execute("UPDATE items SET reserved_for=NULL, reserved_until=NULL, reserved_by=NULL "
                     "WHERE id=? AND reserved_for=?", (parent, it["assignee"]))
        for r in _open_prereqs(conn, parent):
            conn.execute("UPDATE items SET reserved_for=NULL WHERE id=? AND reserved_for=?", (r, it["assignee"]))
    _event(conn, parent, actor, why)
    if actor and actor == it["assignee"]:  # what it just told the manager about this item cannot wait (#1608)
        _raise_blocked(conn, actor, "item", parent)


def _count_late(conn, parent, n, actor):
    """Count prerequisites added to a claimed item; at replan_threshold, mark it replan.

    Work found after an agent starts means the item was bigger than planned, so
    a planner looks at it again (maxpm plan lists it; maxpm replanned clears it)."""
    conn.execute("UPDATE items SET late_prereqs=late_prereqs+? WHERE id=?", (n, parent))
    it = _item(conn, parent)
    limit = int(setting(conn, "replan_threshold", item_id=parent, agent=actor))
    if it["late_prereqs"] >= limit and not it["replan"]:
        conn.execute("UPDATE items SET replan=1 WHERE id=?", (parent,))
        _event(conn, parent, "maxpm", f"marked replan: {it['late_prereqs']} prerequisites added while it was "
               f"claimed (replan_threshold {limit})")


def replanned(conn, item_id, note=None, actor=None):
    """Clear the replan mark after a planner looked at the item again; the count starts over."""
    with tx(conn):
        it = _item(conn, item_id)
        conn.execute("UPDATE items SET replan=0, late_prereqs=0 WHERE id=?", (it["id"],))
        _event(conn, it["id"], actor, "replanned" + (f": {note}" if note else ""))
    return item_show(conn, item_id)


def _prereq_mode(conn, parent, new, actor, mode):
    """After prerequisites land on a parent: keep it (hold) or release it, per design 7.3."""
    p = _item(conn, parent)
    if p["status"] in ("in_progress", "held"):
        _count_late(conn, parent, len(new), actor)
    if p["status"] not in ("in_progress", "held") or p["assignee"] != actor:
        if mode == "keep":
            raise RiverError(f"--keep needs you to hold #{parent}; it is {p['status']}"
                             + (f" (held by {p['assignee']})" if p["assignee"] else ""))
        return
    mode = mode or setting(conn, "default_prerequisite_mode", item_id=parent, agent=actor)
    if mode == "keep":
        limit = int(setting(conn, "keep_prereq_limit", item_id=parent, agent=actor))
        n = len(_open_prereqs(conn, parent))
        if n > limit:
            conn.execute("UPDATE items SET replan=1 WHERE id=?", (parent,))
            _unhold(conn, parent, actor, f"released, not kept: {n} open prerequisites is more than "
                    f"keep_prereq_limit {limit}; marked replan")
            return
        _hold(conn, parent, actor, new)
    else:
        _unhold(conn, parent, actor, "released: new prerequisites " + ", ".join(f"#{r}" for r in new))


def keep(conn, item_id, actor=None):
    """Turn a release back into a hold: own the parent again and reserve its free open prerequisites."""
    if not actor:
        raise RiverError("keeping needs an agent name: set MAXPM_AGENT or pass --as <name>")
    with tx(conn):
        _sweep(conn)
        p = _item(conn, item_id)
        if p["status"] == "held" and p["assignee"] == actor:
            return item_show(conn, item_id)
        if p["status"] not in ("open", "in_progress") or (p["assignee"] and p["assignee"] != actor):
            raise RiverError(f"#{item_id} is {p['status']}" + (f" by {p['assignee']}" if p["assignee"] else "")
                             + "; only an open item, or one you hold, can be kept")
        prereqs = _open_prereqs(conn, item_id)
        if not prereqs:
            raise RiverError(f"#{item_id} waits on nothing open; claim it instead: maxpm claim {item_id}")
        taken = [r for r in conn.execute(
            f"SELECT id, assignee, reserved_for FROM items WHERE id IN ({','.join('?' * len(prereqs))})", prereqs)
            if (r["assignee"] and r["assignee"] != actor) or (r["reserved_for"] and r["reserved_for"] != actor)]
        if taken:
            raise RiverError("refused: " + ", ".join(f"#{r['id']} is taken by {r['assignee'] or r['reserved_for']}"
                                                    for r in taken) + f"; #{item_id} stays open")
        limit = int(setting(conn, "keep_prereq_limit", item_id=item_id, agent=actor))
        if len(prereqs) > limit:
            raise RiverError(f"refused: #{item_id} has {len(prereqs)} open prerequisites, more than keep_prereq_limit "
                             f"{limit}. Plan it instead: split the work, or let other agents take the prerequisites")
        _agent(conn, actor)
        _hold(conn, item_id, actor, prereqs)
    return item_show(conn, item_id)


def push(conn, item_id, to, note=None, actor=None):
    """Reserve an open item for one agent and alert it. It expires after reserve_ttl if nobody answers."""
    with tx(conn):
        _sweep(conn)
        it = _item(conn, item_id)
        _agent(conn, to)
        if it["status"] != "open":
            raise RiverError(f"#{item_id} is {it['status']}" + (f" by {it['assignee']}" if it["assignee"] else "")
                             + "; only an open item can be pushed")
        if it["reserved_for"] and it["reserved_for"] not in (to, actor):  # its own reservation, the actor hands on
            raise RiverError(f"#{item_id} is already reserved for {it['reserved_for']}"
                             + (" (pushed)" if it["reserved_until"] else "") + f"; {_unreserve_hint(item_id)}")
        q = conn.execute("SELECT agent FROM queue_entries WHERE item_id=?", (it["id"],)).fetchone()
        if q and q["agent"] != to:  # a push would hold it against the queue, and nobody could claim it
            raise RiverError(f"#{item_id} is in the queue of {q['agent']}; remove it there first: "
                             f"maxpm queue remove {q['agent']} {item_id}")
        if author_refusal(conn, it, to):
            raise RiverError(f"refused: {to} worked on what release review #{item_id} covers; push it to an agent "
                             f"that did not, or let maxpm serve start one")
        ttl = parse_duration(setting(conn, "reserve_ttl", item_id=it["id"], agent=to))
        until = now() + ttl
        conn.execute("UPDATE items SET reserved_for=?, reserved_until=?, reserved_by=? WHERE id=?",
                     (to, iso(until), actor, it["id"]))
        _event(conn, it["id"], actor, f"pushed to {to}" + (f": {note}" if note else ""))
        _send(conn, "alert", actor or "maxpm",
              f"{actor or 'someone'} pushed #{it['id']} {it['title']} to you" + (f": {note}" if note else "") +
              f". Take it: maxpm accept {it['id']}   or: maxpm decline {it['id']} --note \"why\"   "
              f"(reserved for you for {_short(ttl)})", to=to, item_id=it["id"])
    return item_show(conn, item_id)


def accept(conn, item_id, actor=None):
    """Take an item pushed to you."""
    it = _item(conn, item_id)
    if it["reserved_for"] != actor or not it["reserved_until"]:
        raise RiverError(f"#{item_id} is not pushed to {actor}" +
                         (f" (reserved for {it['reserved_for']})" if it["reserved_for"] else "") +
                         f"; claim it instead: maxpm claim {item_id}")
    return claim(conn, item_id, actor)


def decline(conn, item_id, note=None, actor=None):
    """Hand a pushed item back: it is open to everyone again and the pusher hears why."""
    with tx(conn):
        it = _item(conn, item_id)
        if it["reserved_for"] != actor or not it["reserved_until"]:
            raise RiverError(f"#{item_id} is not pushed to {actor}")
        conn.execute("UPDATE items SET reserved_for=NULL, reserved_until=NULL, reserved_by=NULL WHERE id=?", (it["id"],))
        conn.execute("UPDATE messages SET state='declined', read_at=COALESCE(read_at, ?) "
                     "WHERE kind='alert' AND item_id=? AND to_agent=? AND state='open'", (iso(now()), it["id"], actor))
        _event(conn, it["id"], actor, "declined the push" + (f": {note}" if note else ""))
        if it["reserved_by"] and it["reserved_by"] != actor:
            _send(conn, "notice", actor, f"{actor} declined #{it['id']} {it['title']}" + (f": {note}" if note else "")
                  + "; it is open to every agent again", to=it["reserved_by"], item_id=it["id"])
    return item_show(conn, item_id)


def offer(conn, body, item, to=None, actor=None, goal=None):
    """Offer help to the agent that holds an item you are blocked on (design 7.5 step 4)."""
    if not actor:
        raise RiverError("offering needs an agent name: set MAXPM_AGENT or pass --as <name>")
    if goal is not None and to is None:
        to = goal_owner(conn, goal)
    with tx(conn):
        it = _item(conn, item)
        to = to or it["assignee"] or it["reserved_for"]
        if not to:
            raise RiverError(f"nobody holds #{item}; take it yourself: maxpm claim {item} (or maxpm next --unblocks {item} --claim)")
        if to == actor:
            raise RiverError("you hold it yourself")
        _agent(conn, to)
        mid = _send(conn, "offer", actor, body.strip(), to=to, item_id=it["id"])
        _event(conn, it["id"], actor, f"offer #{mid} to {to}")
        conn.execute("UPDATE messages SET body=body || ? WHERE id=?", (
            f"\n(answer: maxpm give <id> --to {actor}   or: maxpm split {it['id']} \"<smaller piece>\" ...   "
            f"or: maxpm decline {mid} --message --note \"why\")", mid))
    return message_show(conn, mid)


def _accept_offers(conn, holder, helper, t):
    conn.execute("UPDATE messages SET state='accepted', read_at=COALESCE(read_at, ?), closed_at=? "
                 "WHERE kind='offer' AND state='open' AND to_agent=? AND from_agent=?", (t, t, holder, helper))


def _goal_holder(conn, item_id):
    """Who owns an open goal of an open agent item right now (its items are reserved for them), or None."""
    it = _item(conn, item_id)
    if it["status"] != "open" or it["doer"] == "human":
        return None
    g = conn.execute("SELECT g.owner FROM item_goals ig JOIN goals g ON g.id=ig.goal_id WHERE ig.item_id=? "
                     "AND g.status='open' AND g.owner IS NOT NULL AND g.owner_expires_at >= ? ORDER BY g.rank LIMIT 1",
                     (item_id, iso(now()))).fetchone()
    return g["owner"] if g else None


def give(conn, item_id, to, actor=None):
    """Hand an item you hold (or that is reserved for you) to another agent; the lease moves with it."""
    with tx(conn):
        _sweep(conn)
        it = _item(conn, item_id)
        rec = _agent(conn, to)
        if to == actor:
            raise RiverError("you already have it")
        t = now()
        if it["assignee"] == actor and it["status"] in ("in_progress", "held"):
            if rec["role"] == "planner":
                raise RiverError(f"{to} is a planner session and takes no work")
            n, limit, key = _lease_room(conn, it["id"], to, ("in_progress",))
            if it["status"] == "in_progress" and n >= limit:
                raise RiverError(f"refused: {to} already holds {n} item(s) ({key} {limit}); they can release one first")
            ttl = _lease_for(conn, it["id"], to)
            if it["status"] == "in_progress":
                conn.execute("UPDATE items SET assignee=?, claimed_at=?, lease_expires_at=? WHERE id=?",
                             (to, iso(t), iso(t + ttl), it["id"]))
            else:
                conn.execute("UPDATE items SET assignee=?, hold_expires_at=? WHERE id=?",
                             (to, iso(_hold_until(conn, it["id"], to, t)), it["id"]))
                for r in _open_prereqs(conn, it["id"]):
                    conn.execute("UPDATE items SET reserved_for=? WHERE id=? AND reserved_for=?", (to, r, actor))
        elif it["reserved_for"] == actor and it["status"] == "open":
            conn.execute("UPDATE items SET reserved_for=? WHERE id=?", (to, it["id"]))
        elif it["status"] == "open" and not it["reserved_for"] and _goal_holder(conn, it["id"]) == actor:
            # A goal owner hands on one of the goal's items: it is reserved for the other agent like a push,
            # and comes back to the goal when they do not take it within reserve_ttl.
            ttl = parse_duration(setting(conn, "reserve_ttl", item_id=it["id"], agent=to))
            conn.execute("UPDATE items SET reserved_for=?, reserved_until=?, reserved_by=? WHERE id=?",
                         (to, iso(t + ttl), actor, it["id"]))
        else:
            raise RiverError(f"#{item_id} is not yours to give (" + (f"{it['status']} by {it['assignee']}" if it["assignee"]
                             else f"reserved for {it['reserved_for']}" if it["reserved_for"] else it["status"]) + ")")
        _event(conn, it["id"], actor, f"given to {to}")
        _accept_offers(conn, actor, to, iso(t))
        _send(conn, "notice", actor, f"{actor} gave you #{it['id']} {it['title']}; it is yours now "
              f"(maxpm show {it['id']})", to=to, item_id=it["id"])
    return item_show(conn, item_id)


def split(conn, item_id, titles, actor=None, doer="any"):
    """Add smaller prerequisites anyone can take; if you hold the item it waits for them, still yours."""
    if not titles:
        raise RiverError("give at least one title for a smaller piece")
    with tx(conn):
        it = _item(conn, item_id)
        pname = _project_name(conn, it["project_id"])
    new = [item_add(conn, pname, t, it["priority"], "", doer, (), actor, found_during=None)["id"] for t in titles]
    with tx(conn):
        for n in new:
            _dep_add(conn, it["id"], n, actor)
            _event(conn, n, actor, f"split from #{it['id']}")
        cur = _item(conn, item_id)
        if cur["assignee"] == actor and cur["status"] in ("in_progress", "held"):
            _hold(conn, it["id"], actor, [])
        conn.execute("UPDATE messages SET state='accepted', closed_at=? WHERE kind='offer' AND state='open' "
                     "AND to_agent=? AND item_id=?", (iso(now()), actor, it["id"]))
    res = item_show(conn, item_id)
    res["split_into"] = new
    return res


def accept_message(conn, msg_id, actor=None):
    """Say yes to an alert (design 7.4): claim its item now, or, while you hold other work, keep it
    reserved for you until after that (reserve_ttl). The sender hears which."""
    with tx(conn):
        m = _message(conn, msg_id)
        if m["kind"] != "alert":
            raise RiverError(f"message {msg_id} is {'an' if m['kind'][0] in 'aeiou' else 'a'} {m['kind']}; only alerts are "
                             f"accepted by message id (an offer: maxpm give <item> --to <agent>, or maxpm split)")
        if m["to_agent"] != actor:
            raise RiverError(f"message {msg_id} is for {m['to_agent']}, not {actor}")
        if m["state"] not in ("open", "read"):  # reading an alert does not answer it
            raise RiverError(f"message {msg_id} is already {m['state']}")
        t = iso(now())
        conn.execute("UPDATE messages SET state='accepted', read_at=COALESCE(read_at, ?), closed_at=? WHERE id=?",
                     (t, t, m["id"]))
        if m["item_id"] is None:
            _send(conn, "note", actor, f"yes to your alert #{m['id']}", to=m["from_agent"], reply_to=m["id"])
            return message_show(conn, msg_id)
        it = _item(conn, m["item_id"])
        busy = conn.execute("SELECT id FROM items WHERE assignee=? AND status IN ('in_progress','held')",
                            (actor,)).fetchall()
        ann = annotate(conn)
        now_ok = it["status"] == "open" and not busy and ann[it["id"]]["ready"] and not (
            it["reserved_for"] and it["reserved_for"] != actor)
        if not now_ok:
            if it["status"] != "open" or (it["reserved_for"] and it["reserved_for"] != actor):
                raise RiverError(f"#{it['id']} is {it['status']}" + (f" by {it['assignee']}" if it["assignee"] else "")
                                 + (f", reserved for {it['reserved_for']}" if it["reserved_for"] else "")
                                 + f"; decline instead: maxpm decline {msg_id} --message --note \"why\"")
            ttl = parse_duration(setting(conn, "reserve_ttl", item_id=it["id"], agent=actor))
            conn.execute("UPDATE items SET reserved_for=?, reserved_until=?, reserved_by=? WHERE id=?",
                         (actor, iso(now() + ttl), m["from_agent"], it["id"]))
            _event(conn, it["id"], actor, f"accepted alert #{m['id']}; kept for after "
                   + (", ".join(f"#{r['id']}" for r in busy) or "its prerequisites"))
            _send(conn, "note", actor, f"yes to your alert #{m['id']}: I take #{it['id']} after "
                  + (", ".join(f"#{r['id']}" for r in busy) or "what it waits on"),
                  to=m["from_agent"], item_id=it["id"], reply_to=m["id"])
            return message_show(conn, msg_id)
        _send(conn, "note", actor, f"yes to your alert #{m['id']}: I am taking #{it['id']} now",
              to=m["from_agent"], item_id=it["id"], reply_to=m["id"])
    claim(conn, it["id"], actor)
    return message_show(conn, msg_id)


def _holder_of(conn, item_id):
    it = _item(conn, item_id)
    if it["status"] not in ("in_progress", "held") or not it["assignee"]:
        raise RiverError(f"nobody holds #{item_id} ({it['status']}); name the agent, or use --item {item_id} "
                         f"so the next holder gets it")
    return it["assignee"]


def message(conn, kind, body, to=None, holder_of=None, item=None, file=None, cwd=None, actor=None, goal=None,
            level=None, blocked=False):
    """The shortcuts maxpm alert / ask / note (design 7.4): to an agent, to the holder of an item, or
    (questions) to every agent whose held items touch a file. Returns the messages sent."""
    if sum(x is not None for x in (to, holder_of, file, goal)) > 1:
        raise RiverError("give one of: an agent name, --holder-of <id>, --goal <name>, or --file <path>")
    if goal is not None:
        to = goal_owner(conn, goal)
    if file is not None:
        if kind != "question":
            raise RiverError("--file is for questions: maxpm ask --file <path> \"...\"")
        targets = [a["name"] for a in who(conn, file=file, cwd=cwd) if a["name"] != actor]
        if not targets:
            raise RiverError(f"no agent holds an item that touches {file} (maxpm who --file {file})")
        return [send(conn, kind, body, t, item, None, actor, level, blocked) for t in targets]
    if holder_of is not None:
        to = _holder_of(conn, holder_of)
        if item is None:
            item = holder_of
    if to is None and item is None:
        raise RiverError("say who gets it: an agent name, --holder-of <id>, or --item <id> (its holder)")
    return [send(conn, kind, body, to, item, None, actor, level, blocked)]


def decline_message(conn, msg_id, note=None, actor=None):
    """Say no to an offer or alert; the sender hears why."""
    with tx(conn):
        m = _message(conn, msg_id)
        if m["kind"] not in ("offer", "alert"):
            raise RiverError(f"message {msg_id} is {'an' if m['kind'][0] in 'aeiou' else 'a'} {m['kind']}; only offers and alerts are declined")
        if m["to_agent"] != actor:
            raise RiverError(f"message {msg_id} is for {m['to_agent']}, not {actor}")
        if m["state"] not in ("open", "read"):  # reading an alert does not answer it
            raise RiverError(f"message {msg_id} is already {m['state']}")
        t = iso(now())
        conn.execute("UPDATE messages SET state='declined', read_at=COALESCE(read_at, ?), closed_at=? WHERE id=?", (t, t, m["id"]))
        _send(conn, "note", actor, f"no to your {m['kind']} #{m['id']}" + (f": {note}" if note else ""),
              to=m["from_agent"], item_id=m["item_id"], reply_to=m["id"])
    return message_show(conn, msg_id)


def _free_pushes(conn, agent, why):
    """Take back every open push to an agent that will not take it (stopped, ended, never connected):
    the items are open to every agent again, and the project counts as without an agent. Inside a tx."""
    rows = conn.execute("SELECT id FROM items WHERE reserved_for=? AND reserved_until IS NOT NULL AND status='open'",
                        (agent,)).fetchall()
    for r in rows:
        conn.execute("UPDATE items SET reserved_for=NULL, reserved_until=NULL, reserved_by=NULL WHERE id=?", (r["id"],))
        conn.execute("UPDATE messages SET state='declined', read_at=COALESCE(read_at, ?) "
                     "WHERE kind='alert' AND item_id=? AND to_agent=? AND state='open'", (iso(now()), r["id"], agent))
        _event(conn, r["id"], "maxpm", f"push to {agent} taken back: {why}; open to everyone")
    return [r["id"] for r in rows]


def _free_reservations(conn, agent, why):
    """End every reservation of an agent that takes no work any more (stopped, gone, unregistered): its
    pushes, and the items reserved for it without a time limit (a prerequisite of an item it held, one
    given to it, or a pushed item it claimed and lost). Inside a tx."""
    ids = _free_pushes(conn, agent, why)
    for r in conn.execute("SELECT id FROM items WHERE reserved_for=? AND status='open'", (agent,)).fetchall():
        conn.execute("UPDATE items SET reserved_for=NULL, reserved_until=NULL, reserved_by=NULL WHERE id=?", (r["id"],))
        _event(conn, r["id"], "maxpm", f"reservation for {agent} ended: {why}; open to everyone")
        ids.append(r["id"])
    return ids


def _unreserve_hint(item_id):
    return f"that agent, a person, or a manager ends the reservation: maxpm edit {item_id} --unreserve"


def cancel_push(conn, item_id, actor=None):
    """Take a push back before it is answered; the agent it went to hears about it. A person or a manager
    also ends a reservation that is not a push (an item reserved for an agent with no time limit)."""
    with tx(conn):
        it = _item(conn, item_id)
        if it["status"] == "open" and it["reserved_for"] and not it["reserved_until"]:
            if actor != it["reserved_for"] and may_change_queue(conn, actor, it["reserved_for"]):
                raise RiverError(f"refused: #{item_id} is reserved for {it['reserved_for']}; that agent, a person, "
                                 f"or a manager ends the reservation")
        elif not it["reserved_until"] or it["status"] != "open":
            raise RiverError(f"#{item_id} has no open push or reservation")
        to = it["reserved_for"]
        conn.execute("UPDATE items SET reserved_for=NULL, reserved_until=NULL, reserved_by=NULL WHERE id=?", (it["id"],))
        conn.execute("UPDATE messages SET state='declined', read_at=COALESCE(read_at, ?) "
                     "WHERE kind='alert' AND item_id=? AND to_agent=? AND state='open'", (iso(now()), it["id"], to))
        what = "push" if it["reserved_until"] else "reservation"
        _event(conn, it["id"], actor, f"{what} {'to' if it['reserved_until'] else 'for'} {to} cancelled")
        if to != actor:
            _send(conn, "notice", actor or "maxpm", f"the {what} of #{it['id']} {it['title']} "
                  f"{'to' if it['reserved_until'] else 'for'} you was cancelled", to=to, item_id=it["id"])
    return item_show(conn, item_id)


def _resume_holds(conn, closed_id):
    """A prerequisite closed: every held parent with nothing left open goes back to its holder, in progress."""
    back = []
    for r in conn.execute("SELECT i.id, i.assignee FROM deps d JOIN items i ON i.id=d.item_id "
                          "WHERE d.blocked_by=? AND d.kind<>'conflicts' AND i.status='held'", (closed_id,)).fetchall():
        if not _open_prereqs(conn, r["id"]):
            ttl = _lease_for(conn, r["id"], r["assignee"])
            conn.execute("UPDATE items SET status='in_progress', hold_expires_at=NULL, claimed_at=?, lease_expires_at=? "
                         "WHERE id=?", (iso(now()), iso(now() + ttl), r["id"]))
            _event(conn, r["id"], "maxpm", f"prerequisites done; back in progress for {r['assignee']}")
            back.append(r["id"])
    return back


def block(conn, item_id, reason=None, actor=None, until=None):
    """Record a blocker that is not an item in the queue ("waiting on Stripe review").

    With `until` (see parse_when) the blocker ends by itself at that time: the
    sweep clears it, and the item is ready again."""
    if not reason and not until:
        raise RiverError("say what the item waits on: --reason \"<what>\", --until <time>, or both")
    with tx(conn):
        it = _item(conn, item_id)
        zone = setting(conn, "timezone")
        t = iso(parse_when(until, zone)) if until else None
        if t and t <= iso(now()):
            raise RiverError(f"{until!r} is not in the future ({show_time(t, zone)})")
        reason = reason or "waiting for a set time"
        conn.execute("UPDATE items SET blocked_reason=?, blocked_until=?, blocked_set_by=?, "
                     "blocked_at=COALESCE(CASE WHEN blocked_reason IS NOT NULL THEN blocked_at END, ?) WHERE id=?",
                     (reason, t, actor, iso(now()), it["id"]))
        _event(conn, it["id"], actor, f"blocked: {reason}" + (f" (until {show_time(t, zone)})" if t else ""))
        _raise_blocked(conn, actor, "item", it["id"])  # what it just told the manager about this item (#1608)
    return item_show(conn, item_id)


def unblock(conn, item_id, actor=None):
    with tx(conn):
        it = _item(conn, item_id)
        if it["blocked_set_by"] == CADENCE_BY:
            raise RiverError(f"#{it['id']} waits for the release cadence or the release order of target "
                             f"{it['target']}; a release sooner needs a reason: "
                             f"maxpm target release-now {it['target']} --reason \"<why>\"")
        conn.execute("UPDATE items SET blocked_reason=NULL, blocked_until=NULL, blocked_at=NULL, blocked_set_by=NULL "
                     "WHERE id=?", (it["id"],))
        _event(conn, it["id"], actor, "outside blocker cleared")
    return item_show(conn, item_id)


# ---------------------------------------------------------------- graph

def _load_graph(conn):
    # One view for the three reads. As separate statements, another session's new item and its links could
    # come between them: a link then named an item this read did not have, and the command stopped (KeyError).
    # A plain BEGIN holds one view of the database (WAL) and keeps no writer out; inside the caller's
    # transaction the view is one already.
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN")
    try:
        projects = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM projects")}
        items = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM items")}
        deps = conn.execute("SELECT item_id, blocked_by, kind FROM deps").fetchall()
    finally:
        if own:
            conn.execute("COMMIT")
    waits_on = {i: [] for i in items}     # item -> prerequisites
    waited_by = {i: [] for i in items}    # item -> dependents
    conflicts = {i: [] for i in items}   # item -> items it must not run beside
    feeds = {i: [] for i in items}       # item -> prerequisites whose output it reads
    for r in deps:
        if r["kind"] == "conflicts":
            conflicts[r["item_id"]].append(r["blocked_by"])
            conflicts[r["blocked_by"]].append(r["item_id"])
            continue
        waits_on[r["item_id"]].append(r["blocked_by"])
        waited_by[r["blocked_by"]].append(r["item_id"])
        if r["kind"] == "feeds":
            feeds[r["item_id"]].append(r["blocked_by"])
    return projects, items, waits_on, waited_by, conflicts, feeds


def annotate(conn):
    """Compute readiness, effective priority, unblock counts, and sort keys for every item."""
    projects, items, waits_on, waited_by, conflicts, feeds = _load_graph(conn)
    is_open = {i: items[i]["status"] in OPEN_STATES for i in items}
    zone = setting(conn, "timezone")
    tags: dict[int, list] = {}
    for r in conn.execute("SELECT ig.item_id, g.name FROM item_goals ig JOIN goals g ON g.id=ig.goal_id "
                          "ORDER BY g.rank, g.id"):
        tags.setdefault(r["item_id"], []).append(r["name"])
    # A goal with an owner reserves its agent items for the owner (a person's items stay theirs).
    goal_owners = {r["name"]: r["owner"] for r in conn.execute(
        "SELECT name, owner FROM goals WHERE status='open' AND owner IS NOT NULL AND owner_expires_at >= ?",
        (iso(now()),))}
    queued = {r["item_id"]: r["agent"] for r in conn.execute(
        "SELECT item_id, agent FROM queue_entries WHERE item_id IS NOT NULL")}
    refs: dict[int, list] = {}
    for r in conn.execute("SELECT item_id, ref, url, synced_at FROM item_refs ORDER BY created_at, ref"):
        refs.setdefault(r["item_id"], []).append({"ref": r["ref"], "url": r["url"], "synced_at": r["synced_at"]})
    now_s = iso(now())
    soon_s = iso(now() + parse_duration(setting(conn, "due_warn_before")))
    model_defaults = _model_defaults(conn)
    ladder_text = setting(conn, "model_ladder")

    # Open dependents, transitive, of each open item.
    memo: dict[int, frozenset] = {}

    def dependents(i, trail=()):
        if i in memo:
            return memo[i]
        acc = set()
        for d in waited_by[i]:
            if is_open[d] and d not in trail:
                acc.add(d)
                acc |= dependents(d, trail + (i,))
        memo[i] = frozenset(acc)
        return memo[i]

    # Depth: 0 when no open prerequisite, else 1 + max depth of open prerequisites.
    depth_memo: dict[int, int] = {}

    def depth(i, trail=()):
        if i in depth_memo:
            return depth_memo[i]
        ds = [depth(b, trail + (i,)) + 1 for b in waits_on[i] if is_open[b] and b not in trail]
        depth_memo[i] = max(ds) if ds else 0
        return depth_memo[i]

    out = {}
    for i, it in items.items():
        p = projects[it["project_id"]]
        open_blockers = [b for b in waits_on[i] if is_open[b]]
        deps_i = dependents(i) if is_open[i] else frozenset()
        eff = it["priority"]
        source = None
        for d in deps_i:
            if items[d]["priority"] < eff:
                eff, source = items[d]["priority"], d
        busy = [c for c in conflicts[i] if items[c]["status"] in ("in_progress", "held")]
        ready = it["status"] == "open" and not open_blockers and not it["blocked_reason"] and not busy
        a = dict(it)
        a.update(
            touches=touches_list(it["touches"]),
            project=p["name"],
            project_rank=p["rank"],
            project_archived=bool(p["archived"]),
            waits_on=sorted(waits_on[i]),
            open_blockers=sorted(open_blockers),
            unblocks=sorted(waited_by[i]),
            fed_by=sorted(feeds[i]),
            conflicts=sorted(c for c in conflicts[i] if is_open[c]),
            busy_conflicts=sorted(busy),
            conflict_holders=sorted({items[c]["assignee"] or "" for c in busy}),
            unblocks_count=len(deps_i),
            effective_priority=eff,
            priority_from=source,
            ready=ready,
            depth=depth(i) if is_open[i] else None,
        )
        due, due_from = it["due"], None
        for d in deps_i:
            if items[d]["due"] and (due is None or items[d]["due"] < due):
                due, due_from = items[d]["due"], d
        a["effective_due"], a["due_from"] = (due, due_from) if is_open[i] else (it["due"], None)
        a.update(_item_models(it, p["name"], model_defaults, ladder_text))
        a["goals"] = tags.get(i, [])
        a["goal_reserved"] = None
        a["queued_for"] = queued.get(i)
        if a["queued_for"] and it["status"] == "open":
            a["reserved_for"] = a["queued_for"]  # other agents skip a queued item
        if not a["reserved_for"] and it["doer"] != "human" and it["status"] == "open":
            g = next((g for g in a["goals"] if g in goal_owners), None)
            if g:
                a["reserved_for"], a["goal_reserved"] = goal_owners[g], g
        a["refs"] = refs.get(i, [])
        a["due_text"] = show_time(a["effective_due"], zone)
        a["due_state"] = (None if not is_open[i] or not a["effective_due"]
                          else "overdue" if a["effective_due"] <= now_s
                          else "soon" if a["effective_due"] <= soon_s else None)
        a["blocked_until_text"] = show_time(it["blocked_until"], zone)
        a["blocked_text"] = ("" if not it["blocked_reason"] else "blocked" + (
            f" until {a['blocked_until_text']}" if it["blocked_until"] else "") + f": {it['blocked_reason']}")
        a["reason"] = _reason(a)
        a["sort_key"] = (eff, p["rank"], -len(deps_i), it["rank"], it["created_at"], i)
        out[i] = a
    return out


def _reason(a):
    parts = [f"P{a['effective_priority']}"]
    if a["priority_from"] is not None:
        parts[0] += f" inherited from #{a['priority_from']} (own P{a['priority']})"
    parts.append(f"project {a['project']} (rank {a['project_rank']})")
    if a["unblocks_count"]:
        n = a["unblocks_count"]
        parts.append(f"unblocks {n} item{'s' if n != 1 else ''}")
    return ", ".join(parts)


def _prereq_closure(ann, root):
    seen, stack = set(), [root]
    while stack:
        x = stack.pop()
        for b in ann[x]["waits_on"]:
            if b not in seen and ann[b]["status"] in OPEN_STATES:
                seen.add(b)
                stack.append(b)
    return seen


def _graph_distances(ann, starts):
    """Distance from the start items over dependency links in both directions."""
    dist = {s: 0 for s in starts}
    frontier = list(starts)
    while frontier:
        nxt = []
        for x in frontier:
            for y in ann[x]["waits_on"] + ann[x]["unblocks"]:
                if y not in dist:
                    dist[y] = dist[x] + 1
                    nxt.append(y)
        frontier = nxt
    return dist


def history(conn, actor, limit=20):
    """Items the agent claimed or finished, most recent first."""
    seen, out = set(), []
    for r in conn.execute(
            "SELECT item_id FROM events WHERE actor=? AND item_id IS NOT NULL "
            "AND (change LIKE 'claimed%' OR change LIKE 'done%') ORDER BY id DESC", (actor,)):
        if r["item_id"] not in seen:
            seen.add(r["item_id"])
            out.append(r["item_id"])
            if len(out) >= limit:
                break
    return out


def ready_for(a, actor):
    """Whether an agent can take an item now: it is ready, or only conflicts with items the agent holds
    itself keep it back. A conflict keeps two agents out of the same files; one agent edits them in turn."""
    return a["ready"] or bool(actor and a["status"] == "open" and not a["open_blockers"] and not a["blocked_reason"]
                              and a["conflict_holders"] == [actor])


def ready_list(conn, project=None, unblocks=None, doer_for=None, ann=None, near=None, mine=None, actor=None):
    """Ready items drawn from the area the agent chooses. With `actor`: also the items that only conflict
    with what that agent holds itself (ready_for).

    The agent picks the area where it holds context: one or more projects
    (`project`, a name or comma list), the prerequisites of an item or project
    (`unblocks`), or the items linked to items it knows (`near`, closest
    first). Inside the area the graph order applies. With no area, the pool
    is every project.
    """
    ann = ann or annotate(conn)
    pool = [a for a in ann.values() if ready_for(a, actor) and not a["project_archived"]]
    if project:
        names = _names(conn, project)
        pool = [a for a in pool if a["project"] in names]
    if unblocks is not None:
        if str(unblocks).isdigit():
            _item(conn, unblocks)
            roots = [int(unblocks)]
        else:
            unblocks = _project(conn, unblocks)["name"]
            roots = [a["id"] for a in ann.values() if a["project"] == unblocks and a["status"] in OPEN_STATES]
        closure = set()
        for r in roots:
            closure |= _prereq_closure(ann, r)
        pool = [a for a in pool if a["id"] in closure]
    if doer_for is not None:
        pool = [a for a in pool if a["doer"] in ("any", doer_for)]
    if mine:
        hist = [i for i in history(conn, mine) if i in ann]
        if not hist:
            raise RiverError(f"{mine} has no claimed or finished items yet, so there is no history to work near. "
                             f"Pick an area instead: maxpm project list, then maxpm next --project <name>")
        dist = _graph_distances(ann, hist)
        hist_projects = {ann[i]["project"] for i in hist}
        far = 10 ** 6
        pool = [a for a in pool if a["id"] in dist or a["project"] in hist_projects]
        for a in pool:
            a["distance"] = dist.get(a["id"])
            if a["id"] not in dist:
                a["same_project"] = True
        pool.sort(key=lambda a: (dist.get(a["id"], far), a["sort_key"]))
        return pool
    if near:
        starts = [int(x) for x in (near if isinstance(near, (list, tuple)) else str(near).split(","))]
        for x in starts:
            _item(conn, x)
        dist = _graph_distances(ann, starts)
        pool = [a for a in pool if a["id"] in dist]
        for a in pool:
            a["distance"] = dist[a["id"]]
        pool.sort(key=lambda a: (dist[a["id"]], a["sort_key"]))
        return pool
    pool.sort(key=lambda a: a["sort_key"])
    return pool


# ---------------------------------------------------------------- agent queues

def may_change_queue(conn, actor, agent):
    """Who may change an agent's queue: a person, or a manager session. Returns None or why not."""
    if not actor:
        return "name yourself: --as <name>"
    r = conn.execute("SELECT kind, role FROM agents WHERE name=?", (actor,)).fetchone()
    if r and (r["kind"] == "human" or r["role"] == "manager"):
        return None
    return (f"{actor} is an agent; a person or a manager changes queues. An agent lists its own queue "
            f"(maxpm queue list) and removes its own instruction entries after it acts on them")


def _queue_rows(conn, agent):
    return conn.execute("SELECT * FROM queue_entries WHERE agent=? ORDER BY kind='item', pos, id", (agent,)).fetchall()


def _queue_pos(conn, agent, first=False, before=None, after=None):
    rows = conn.execute("SELECT item_id, pos FROM queue_entries WHERE agent=? ORDER BY pos", (agent,)).fetchall()
    if not rows:
        return 1.0
    if first:
        return rows[0]["pos"] - 1
    ref = before if before is not None else after
    if ref is None:
        return rows[-1]["pos"] + 1
    k = next((n for n, r in enumerate(rows) if r["item_id"] == int(ref)), None)
    if k is None:
        raise RiverError(f"#{ref} is not in the queue of {agent}")
    if before is not None:
        return (rows[k - 1]["pos"] + rows[k]["pos"]) / 2 if k else rows[k]["pos"] - 1
    return (rows[k]["pos"] + rows[k + 1]["pos"]) / 2 if k + 1 < len(rows) else rows[k]["pos"] + 1


def queue_add(conn, agent, item=None, message=None, first=False, before=None, actor=None, kind=None):
    """Put an item (at the end, first, or before another) or an instruction into an agent's queue."""
    why = may_change_queue(conn, actor, agent)
    if why:
        raise RiverError(f"refused: {why}")
    if (item is None) == (message is None):
        raise RiverError("give an item id or --message \"...\"")
    with tx(conn):
        ag = _agent(conn, agent)
        if ag["kind"] != "ai":
            raise RiverError(f"{agent} is a person; a queue is for an agent session")
        t = iso(now())
        if message is not None:
            k = kind or "message"
            eid = conn.execute("INSERT INTO queue_entries(agent,pos,body,kind,added_by,created_at) VALUES (?,?,?,?,?,?)",
                               (agent, _queue_pos(conn, agent, first=True) if k == "stop" else _queue_pos(conn, agent),
                                message, k, actor, t)).lastrowid
            _event(conn, None, actor, f"queue {agent}: {k} added")
            _send(conn, "notice", actor or "maxpm", f"new {'stop request' if k == 'stop' else 'instruction'} in your "
                  f"queue: {message}", to=agent)
    if message is not None:
        _deliver_entry(conn, eid)
        return queue_list(conn, agent)
    with tx(conn):
        _sweep(conn)  # a push that ran out ends here
        it = _item(conn, item)
        if it["status"] in CLOSED_STATES:
            raise RiverError(f"#{it['id']} is {it['status']}")
        if ag["kind"] != "ai":
            raise RiverError(f"{agent} is a person; a queue is for an agent session")
        if it["doer"] == "human":
            raise RiverError(f"#{it['id']} is for a person")
        if author_refusal(conn, it, agent):
            raise RiverError(f"refused: {agent} worked on what release review #{it['id']} covers; queue it for an "
                             f"agent that did not")
        q = conn.execute("SELECT agent FROM queue_entries WHERE item_id=?", (it["id"],)).fetchone()
        if q:
            raise RiverError(f"#{it['id']} is already in the queue of {q['agent']}" + (
                "" if q["agent"] == agent else f"; remove it there first: maxpm queue remove {q['agent']} {it['id']}"))
        if it["status"] != "open" and it["assignee"] != agent:
            raise RiverError(f"#{it['id']} is {it['status']} by {it['assignee']}")
        to = it["reserved_for"] if it["status"] == "open" and it["reserved_for"] != agent else None
        if to and not it["reserved_until"]:
            raise RiverError(f"#{it['id']} is reserved for {to}; {_unreserve_hint(it['id'])}")
        if to:
            # A queue is a person's or a manager's choice: it ends a push to another agent, which hears it.
            # Else that agent's push and this agent's queue both hold the item, and neither can claim it.
            conn.execute("UPDATE items SET reserved_for=NULL, reserved_until=NULL, reserved_by=NULL WHERE id=?",
                         (it["id"],))
            conn.execute("UPDATE messages SET state='declined', read_at=COALESCE(read_at, ?) "
                         "WHERE kind='alert' AND item_id=? AND to_agent=? AND state='open'", (t, it["id"], to))
            _event(conn, it["id"], actor, f"push to {to} ended: queued for {agent}")
            _send(conn, "notice", actor or "maxpm", f"the push of #{it['id']} {it['title']} to you ended: it is in "
                  f"the queue of {agent} now", to=to, item_id=it["id"])
        conn.execute("INSERT INTO queue_entries(agent,pos,item_id,kind,added_by,created_at) VALUES (?,?,?,'item',?,?)",
                     (agent, _queue_pos(conn, agent, first, before), it["id"], actor, t))
        _event(conn, it["id"], actor, f"queued for {agent}")
        _send(conn, "notice", actor or "maxpm", f"#{it['id']} {it['title']} is in your queue now; maxpm go takes it "
              f"when it is ready", to=agent, item_id=it["id"])
    return queue_list(conn, agent)


def queue_list(conn, agent, ann=None):
    ann = ann or annotate(conn)
    out = []
    for r in _queue_rows(conn, agent):
        e = {"entry": r["id"], "kind": r["kind"], "added_by": r["added_by"], "created_at": r["created_at"],
             "delivered_at": r["delivered_at"], "native_status": r["native_status"]}
        if r["item_id"] is not None and r["item_id"] in ann:
            a = ann[r["item_id"]]
            e.update(item=a["id"], title=a["title"], project=a["project"], ready=a["ready"], status=a["status"],
                     open_blockers=a["open_blockers"])
        else:
            e["body"] = r["body"]
        out.append(e)
    return {"agent": agent, "entries": out}


def _queue_entry(conn, agent, ref):
    """An entry of the agent's queue by item id (12) or entry id (e5)."""
    ref = str(ref).strip().lstrip("#")
    if ref[:1] in ("e", "E") and ref[1:].isdigit():
        r = conn.execute("SELECT * FROM queue_entries WHERE agent=? AND id=?", (agent, int(ref[1:]))).fetchone()
    elif ref.isdigit():
        r = conn.execute("SELECT * FROM queue_entries WHERE agent=? AND item_id=?", (agent, int(ref))).fetchone()
    else:
        raise RiverError("name an item id (12) or an entry (e5; maxpm queue list shows them)")
    if not r:
        raise RiverError(f"{ref} is not in the queue of {agent} (maxpm queue list {agent})")
    return r


def queue_remove(conn, agent, ref, actor=None):
    with tx(conn):
        r = _queue_entry(conn, agent, ref)
        own_note = actor == agent and r["kind"] == "message"
        why = None if own_note else may_change_queue(conn, actor, agent)
        if why:
            raise RiverError(f"refused: {why}")
        conn.execute("DELETE FROM queue_entries WHERE id=?", (r["id"],))
        if r["kind"] == "stop":
            conn.execute("UPDATE agents SET stop_at=NULL, stop_by=NULL, stop_reason=NULL WHERE name=?", (agent,))
            _event(conn, None, actor, f"stop of {agent} withdrawn")
        _event(conn, r["item_id"], actor, f"removed from the queue of {agent}" if r["item_id"] else
               f"queue {agent}: {r['kind']} e{r['id']} removed")
    return queue_list(conn, agent)


def queue_move(conn, agent, item, before=None, after=None, actor=None):
    why = may_change_queue(conn, actor, agent)
    if why:
        raise RiverError(f"refused: {why}")
    if (before is None) == (after is None):
        raise RiverError("give --before <id> or --after <id>")
    with tx(conn):
        r = _queue_entry(conn, agent, item)
        if r["item_id"] is None:
            raise RiverError("instructions come first, in the order they were added; move items only")
        conn.execute("DELETE FROM queue_entries WHERE id=?", (r["id"],))
        pos = _queue_pos(conn, agent, before=before, after=after)
        conn.execute("INSERT INTO queue_entries(id,agent,pos,item_id,kind,added_by,created_at) VALUES (?,?,?,?,'item',?,?)",
                     (r["id"], agent, pos, r["item_id"], r["added_by"], r["created_at"]))
        _event(conn, r["item_id"], actor, f"moved in the queue of {agent}")
    return queue_list(conn, agent)


def queue_instructions(conn, agent, mark=True):
    """The agent's instruction entries, in order; marks them delivered."""
    rows = [dict(r) for r in conn.execute("SELECT * FROM queue_entries WHERE agent=? AND kind<>'item' "
                                          "ORDER BY kind<>'stop', pos, id", (agent,))]
    if mark and rows:
        with tx(conn):
            conn.execute("UPDATE queue_entries SET delivered_at=? WHERE agent=? AND kind<>'item' AND delivered_at IS NULL",
                         (iso(now()), agent))
    return rows


def _queue_ready(conn, agent, ann=None):
    """The agent's queued items that are ready now, in queue order."""
    ann = ann or annotate(conn)
    return [ann[r["item_id"]] for r in conn.execute(
        "SELECT item_id FROM queue_entries WHERE agent=? AND item_id IS NOT NULL ORDER BY pos", (agent,))
        if r["item_id"] in ann and ann[r["item_id"]]["ready"]]


def _drop_queue(conn, agent, why, items_only=False):
    """The agent is gone or stopped: its queued items go back to the main queue; an active manager hears it.
    items_only keeps the instructions (a stop entry stays, so a person can withdraw the stop)."""
    rows = conn.execute("SELECT item_id FROM queue_entries WHERE agent=? AND item_id IS NOT NULL", (agent,)).fetchall()
    conn.execute("DELETE FROM queue_entries WHERE agent=?" + (" AND item_id IS NOT NULL" if items_only else ""), (agent,))
    for r in rows:
        _event(conn, r["item_id"], "maxpm", f"back to the main queue ({agent} {why})")
    if rows:
        for m in conn.execute("SELECT * FROM agents WHERE role='manager' AND name<>?", (agent,)).fetchall():
            if _agent_state(conn, m) == "active":
                _send(conn, "notice", "maxpm", f"{agent} {why}; its queued items went back to the main queue: "
                      + ", ".join(f"#{r['item_id']}" for r in rows), to=m["name"])
    return [r["item_id"] for r in rows]


# ---------------------------------------------------------------- agent processes

SHELLS = {"sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh", "env", "timeout", "cmd", "powershell", "pwsh",
          "sandbox-exec", "script"}  # Windows names without .exe


def this_host():
    import socket
    return socket.gethostname()


def pid_alive(pid):
    if os.name == "nt":
        return _win_running(pid) is not None  # os.kill(pid, 0) is no check there: 0 is the Ctrl-C event
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def proc_info(pid):
    """(parent pid, program name, command line) of a process, from ps (on Windows, from the system's process
    list); None when it does not run or no ps."""
    import subprocess
    if os.name == "nt":
        return _win_proc_info(int(pid))
    try:
        out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        args = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    line = out.stdout.strip()
    if out.returncode or not line:
        return None
    ppid, _, comm = line.partition(" ")
    return int(ppid), os.path.basename(comm.strip()), args.stdout.strip()


_WIN = None


def _win_api():
    """Windows: kernel32 and ntdll with the types of the calls below, and the two structures they fill."""
    global _WIN
    if _WIN is None:
        import ctypes
        from ctypes import wintypes as w
        k32, ntdll = ctypes.WinDLL("kernel32", use_last_error=True), ctypes.WinDLL("ntdll")

        class Entry(ctypes.Structure):  # PROCESSENTRY32W
            _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
                        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD),
                        ("th32ParentProcessID", w.DWORD), ("pcPriClassBase", w.LONG), ("dwFlags", w.DWORD),
                        ("szExeFile", w.WCHAR * 260)]

        class UnicodeString(ctypes.Structure):
            _fields_ = [("Length", w.USHORT), ("MaximumLength", w.USHORT), ("Buffer", ctypes.c_void_p)]
        for fn, res, args in (
                (k32.OpenProcess, w.HANDLE, (w.DWORD, w.BOOL, w.DWORD)),
                (k32.CloseHandle, w.BOOL, (w.HANDLE,)),
                (k32.WaitForSingleObject, w.DWORD, (w.HANDLE, w.DWORD)),
                (k32.GetProcessTimes, w.BOOL, (w.HANDLE,) + (ctypes.POINTER(w.FILETIME),) * 4),
                (k32.CreateToolhelp32Snapshot, w.HANDLE, (w.DWORD, w.DWORD)),
                (k32.Process32FirstW, w.BOOL, (w.HANDLE, ctypes.POINTER(Entry))),
                (k32.Process32NextW, w.BOOL, (w.HANDLE, ctypes.POINTER(Entry))),
                (ntdll.NtQueryInformationProcess, ctypes.c_long,
                 (w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.ULONG, ctypes.POINTER(w.ULONG)))):
            fn.restype, fn.argtypes = res, args
        _WIN = (ctypes, k32, ntdll, Entry, UnicodeString)
    return _WIN


def _win_running(pid):
    """Windows: None when the process does not run; else (start time, command line), each None when Windows
    does not let this user read it."""
    ctypes, k32, ntdll, _, UnicodeString = _win_api()
    from ctypes import wintypes as w
    h = k32.OpenProcess(0x00100000 | 0x1000, False, int(pid))  # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return (None, None) if ctypes.get_last_error() == 5 else None  # 5: access denied, so it exists
    try:
        if k32.WaitForSingleObject(h, 0) != 0x102:  # not WAIT_TIMEOUT: it has ended
            return None
        times = [w.FILETIME() for _ in range(4)]
        created = None
        if k32.GetProcessTimes(h, *[ctypes.byref(t) for t in times]):
            created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        cmd, size = None, w.ULONG(0)
        ntdll.NtQueryInformationProcess(h, 60, None, 0, ctypes.byref(size))  # 60: ProcessCommandLineInformation
        if size.value:
            buf = ctypes.create_string_buffer(size.value)
            if ntdll.NtQueryInformationProcess(h, 60, buf, size.value, ctypes.byref(size)) == 0:
                us = UnicodeString.from_buffer(buf)
                cmd = ctypes.wstring_at(us.Buffer, us.Length // 2) if us.Buffer else ""
        return created, cmd
    finally:
        k32.CloseHandle(h)


def _win_proc_info(pid):
    """proc_info on Windows: the parent and the program name from a snapshot of the process list, the
    command line from the process itself (the program name when Windows does not give it)."""
    run = _win_running(pid)
    if run is None:
        return None
    ctypes, k32, _, Entry, _ = _win_api()
    snap = k32.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == ctypes.c_void_p(-1).value:
        return None
    found, e = None, Entry()
    e.dwSize = ctypes.sizeof(Entry)
    try:
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok and found is None:
            if e.th32ProcessID == pid:
                found = (e.th32ParentProcessID, e.szExeFile)
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    if found is None:
        return None
    ppid, name = found
    # Windows keeps the parent's PID after the parent ends and can give that PID to a new process: a
    # "parent" that started after its child is another program.
    parent = _win_running(ppid) if ppid else None
    if parent is None or (parent[0] and run[0] and parent[0] > run[0]):
        ppid = 0
    return ppid, name, (run[1] or "").strip() or name


# Windows: programs that hold terminals and the desktop. One of them above the shells means a person ran
# river by hand; it is not an agent, and maxpm stop --kill must not end it.
WIN_HOSTS = {"explorer", "windowsterminal", "openconsole", "conhost", "svchost", "services", "wininit", "winlogon",
             "sihost", "csrss"}


def _between(comm, args):
    """True for a process between the agent CLI and this river command: a shell, and on Windows also the
    river launcher and a Python that runs river (river.exe and a venv's python.exe start the real one)."""
    name = comm.lower().lstrip("-")
    if os.name == "nt":
        name = name[:-4] if name.endswith(".exe") else name
        runs_maxpm = re.search(r'(^|[\\/\s"])maxpm(\.exe|-script\.py)?["\s]|\s-m river\s', args.lower() + " ")
        if name == COMMAND or re.fullmatch(r"py|python[\d.]*w?", name) and runs_maxpm:
            return True
    return name in SHELLS


def agent_process(start=None):
    """The agent CLI process that runs this river command: the first ancestor above the shell(s) that is not
    a shell (SHELLS). Returns (pid, command line), or (None, None) when it finds none (no ps, or on Windows a
    terminal that a person works in)."""
    pid = start or os.getppid()
    for _ in range(12):
        info = proc_info(pid)
        if info is None:
            return None, None
        ppid, comm, args = info
        if not _between(comm, args):
            if os.name == "nt" and re.sub(r"\.exe$", "", comm.lower()) in WIN_HOSTS:
                return None, None
            return pid, args
        if ppid <= 1:
            return None, None
        pid = ppid
    return None, None


def set_process(conn, name, pid, cmd, host=None):
    with tx(conn):
        conn.execute("UPDATE agents SET pid=?, pid_cmd=?, host=? WHERE name=?", (pid, cmd, host or this_host(), name))


def kill_agent(conn, agent, reason, actor=None, grace=5.0, sleep=None):
    """Emergency only (the normal path is maxpm stop): end the agent's process on this host, then release
    everything it held. Checks that the PID still runs the recorded command, so a reused PID is not killed."""
    import signal
    import subprocess
    import time
    sleep = sleep or time.sleep
    if not reason or not reason.strip():
        raise RiverError("say why: maxpm stop <agent> --kill --reason \"...\"")
    why = may_change_queue(conn, actor, agent)
    if why:
        raise RiverError(f"refused: a person or a manager kills an agent ({why})")
    ag = _agent(conn, agent)
    if not ag["pid"]:
        raise RiverError(f"{agent} has no recorded process (maxpm go records it); ask it to stop instead: "
                         f"maxpm stop {agent} --reason \"...\"")
    if ag["host"] != this_host():
        raise RiverError(f"{agent} runs on {ag['host']}, not on this host ({this_host()}); kill it there")
    killed = False
    if pid_alive(ag["pid"]):
        info = proc_info(ag["pid"])
        if info is not None and ag["pid_cmd"] and info[2] != ag["pid_cmd"]:
            raise RiverError(f"refused: PID {ag['pid']} now runs another command ({info[2][:80]}), not {agent}'s; "
                             f"nothing was killed")
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(ag["pid"]), "/T", "/F"], capture_output=True)
        else:
            os.kill(ag["pid"], signal.SIGTERM)
            waited = 0.0
            while pid_alive(ag["pid"]) and waited < grace:
                sleep(0.2)
                waited += 0.2
                try:
                    os.waitpid(ag["pid"], os.WNOHANG)  # a child of this process (tests): reap it
                except (ChildProcessError, OSError):
                    pass
            if pid_alive(ag["pid"]):
                os.kill(ag["pid"], signal.SIGKILL)
        killed = True
    with tx(conn):
        held = [dict(r) for r in conn.execute(
            "SELECT id, title FROM items WHERE assignee=? AND status IN ('in_progress','held')", (agent,))]
        for h in held:
            conn.execute("UPDATE items SET status='open', assignee=NULL, claimed_at=NULL, lease_expires_at=NULL, "
                         "hold_expires_at=NULL, needs_check=1 WHERE id=?", (h["id"],))
            _event(conn, h["id"], actor, f"released: {agent} killed by {actor}: {reason.strip()}")
            for d in conn.execute("SELECT DISTINCT i.id, i.assignee FROM deps x JOIN items i ON i.id=x.item_id "
                                  "WHERE x.blocked_by=? AND i.assignee IS NOT NULL AND i.assignee<>? "
                                  "AND i.status IN ('in_progress','held')", (h["id"], agent)).fetchall():
                _send(conn, "notice", "maxpm", f"#{h['id']} {h['title']}, which your #{d['id']} waits on, is open again: "
                      f"its agent {agent} was killed ({reason.strip()})", to=d["assignee"], item_id=d["id"])
        conn.execute("UPDATE items SET reserved_for=NULL, reserved_until=NULL, reserved_by=NULL "
                     "WHERE reserved_for=? AND status='open'", (agent,))
        _release_goals(conn, agent, f"{agent} killed")
        for t in conn.execute("SELECT name FROM targets WHERE owner=?", (agent,)).fetchall():
            conn.execute("UPDATE targets SET owner=NULL, owner_expires_at=NULL WHERE name=?", (t["name"],))
            _event(conn, None, "maxpm", f"target {t['name']} released: {agent} killed")
        queued = _drop_queue(conn, agent, "was killed")
        conn.execute("UPDATE agents SET stop_at=?, stop_by=?, stop_reason=?, role='stopped', note='killed' WHERE name=?",
                     (iso(now()), actor, "killed: " + reason.strip(), agent))
        _event(conn, None, actor, f"killed {agent} (by {actor}: {reason.strip()})")
    return {"agent": agent, "pid": ag["pid"], "killed": killed, "released": held, "queued_back": queued,
            "warning": "Uncommitted work in the agent's folder is not saved: look at git status there."}


# ---------------------------------------------------------------- native delivery

NATIVE_RUNNER = None  # tests set a fake: f(args) -> (returncode, output)


def parse_native(value):
    """native_message: [(label, env_var, command template)]."""
    out = []
    for part in (value or "").split(";"):
        if not part.strip():
            continue
        label, sep, rest = part.partition("=")
        var, sep2, cmd = rest.partition(":")
        if not sep or not sep2 or not label.strip() or not re.match(r"^[A-Z][A-Z0-9_]*$", var.strip()) \
                or "{address}" not in cmd or "{message}" not in cmd:
            raise RiverError(f"native_message entry {part.strip()!r} needs the form Label=ENV_VAR: command with "
                             f"{{address}} and {{message}}, for example "
                             f"'Codex=CODEX_THREAD_ID: codex queue --thread {{address}} --message {{message}}'")
        out.append((label.strip(), var.strip(), cmd.strip()))
    return out


def native_from_env(conn, env):
    """(platform, address) of a session from its environment, or (None, None)."""
    for label, var, _ in parse_native(setting(conn, "native_message")):
        if env.get(var):
            return label, env[var]
    return None, None


def set_native(conn, name, platform, address):
    with tx(conn):
        if conn.execute("UPDATE agents SET platform=?, native_address=? WHERE name=? AND "
                        "(platform IS NOT ? OR native_address IS NOT ?)",
                        (platform, address, name, platform, address)).rowcount:
            _event(conn, None, name, f"native channel {platform or 'none'}")


def deliver_native(conn, agent, text):
    """Send text into the agent's running session through its platform. Returns the status to record:
    'sent', 'failed: <why>', or 'no native channel' (the queue and inbox still hold it)."""
    import shlex
    import subprocess
    r = conn.execute("SELECT platform, native_address FROM agents WHERE name=?", (agent,)).fetchone()
    if not r or not r["platform"] or not r["native_address"]:
        return "no native channel"
    entry = next((e for e in parse_native(setting(conn, "native_message")) if e[0] == r["platform"]), None)
    if entry is None:
        return f"no native channel ({r['platform']} has no native_message entry)"
    text = " ".join(text.split())  # one line: the socket reads a message per line
    args = [a.replace("{address}", r["native_address"]).replace("{message}", text) for a in shlex.split(entry[2])]
    try:
        if NATIVE_RUNNER is not None:
            code, out = NATIVE_RUNNER(args)
        elif args[0] == "uds":
            code, out = _uds_send(args[1], args[2])
        else:
            p = subprocess.run(args, capture_output=True, text=True, timeout=15)
            code, out = p.returncode, (p.stderr or p.stdout)
    except (OSError, subprocess.SubprocessError) as e:
        return f"failed: {e}"
    return "sent" if code == 0 else f"failed: exit {code}: {(out or '').strip()[:200]}"


def _uds_send(path, text, timeout=5):
    """Write one line to a Unix socket (a Claude Code session's inbox). Returns (code, error text)."""
    import socket
    if not hasattr(socket, "AF_UNIX"):
        return 1, "no Unix sockets on this system"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sk:
            sk.settimeout(timeout)
            sk.connect(path)
            sk.sendall((text + "\n").encode())
    except OSError as e:
        return 1, f"{path}: {e.strerror or e}"
    return 0, ""


def _native_text(kind, sender, body):
    return f"[maxpm {kind} from {sender}] {body} (maxpm --as <you> inbox; maxpm go shows your queue)"


def _deliver_entry(conn, entry_id):
    r = conn.execute("SELECT * FROM queue_entries WHERE id=?", (entry_id,)).fetchone()
    st = deliver_native(conn, r["agent"], _native_text("stop request" if r["kind"] == "stop" else "instruction",
                                                       r["added_by"] or "maxpm", r["body"]))
    with tx(conn):
        conn.execute("UPDATE queue_entries SET native_status=? WHERE id=?", (st, entry_id))
    return st


def stop_agent(conn, agent, reason, actor=None):
    """maxpm stop: a request, not a kill. A stop entry goes to the front of the agent's queue; a waiting agent
    ends at once (maxpm wait returns STOP), a working one on its next river command, after it commits and
    releases its item. A person or a manager may stop any agent; an agent may stop only itself."""
    if not reason or not reason.strip():
        raise RiverError("say why: maxpm stop <agent> --reason \"...\"")
    if actor != agent:
        why = may_change_queue(conn, actor, agent)
        if why:
            raise RiverError(f"refused: an agent may not stop another; a person or a manager stops agents ({why})")
    with tx(conn):
        ag = _agent(conn, agent)
        if ag["kind"] != "ai":
            raise RiverError(f"{agent} is a person")
        if ag["stop_at"]:
            raise RiverError(f"{agent} is already asked to stop (by {ag['stop_by']}: {ag['stop_reason']})")
        t = iso(now())
        conn.execute("UPDATE agents SET stop_at=?, stop_by=?, stop_reason=? WHERE name=?", (t, actor, reason.strip(), agent))
        eid = conn.execute("INSERT INTO queue_entries(agent,pos,body,kind,added_by,created_at) VALUES (?,?,?,'stop',?,?)",
                           (agent, _queue_pos(conn, agent, first=True), reason.strip(), actor, t)).lastrowid
        _event(conn, None, actor, f"stopped {agent} (by {actor}: {reason.strip()})")
        _free_reservations(conn, agent, f"{agent} was stopped")
        holds = [dict(r) for r in conn.execute(
            "SELECT id, title, status FROM items WHERE assignee=? AND status IN ('in_progress','held') ORDER BY id", (agent,))]
        for h in holds:
            _event(conn, h["id"], actor, f"stop requested for {agent}: {reason.strip()}")
        _send(conn, "alert", actor or "maxpm", f"STOP requested: {reason.strip()}. Commit finished work, release or "
              f"hand back your item with a note, then end this session.", to=agent)
    waiting = ag["role"] == "waiting" and not holds
    native = None if waiting else _deliver_entry(conn, eid)
    return {"agent": agent, "reason": reason.strip(), "waiting": waiting, "holds": holds, "native": native,
            "ends": "now: maxpm wait returns STOP within seconds" if waiting
            else "after its next maxpm command, once it commits and releases its item"}


def stop_request(conn, name):
    """The stop asked for this agent, or None."""
    r = conn.execute("SELECT stop_at, stop_by, stop_reason FROM agents WHERE name=?", (name,)).fetchone() if name else None
    return dict(r) if r and r["stop_at"] else None


def _finish_stop(conn, agent):
    """The stopped agent holds nothing now: release its goals and targets, give its queued items back."""
    with tx(conn):
        if conn.execute("SELECT 1 FROM items WHERE assignee=? AND status IN ('in_progress','held')", (agent,)).fetchone():
            return False
        _free_reservations(conn, agent, f"{agent} stopped")
        _release_goals(conn, agent, f"{agent} stopped")
        for t in conn.execute("SELECT name FROM targets WHERE owner=?", (agent,)).fetchall():
            conn.execute("UPDATE targets SET owner=NULL, owner_expires_at=NULL WHERE name=?", (t["name"],))
            _event(conn, None, "maxpm", f"target {t['name']} released: {agent} stopped")
        _drop_queue(conn, agent, "stopped", items_only=True)
        conn.execute("UPDATE agents SET role='stopped', note='stopped', waiting_since=NULL, waiting_in=NULL "
                     "WHERE name=?", (agent,))
        if not conn.execute("SELECT 1 FROM events WHERE actor='maxpm' AND change=?", (f"{agent} ended (stopped)",)).fetchone():
            _event(conn, None, "maxpm", f"{agent} ended (stopped)")
    return True


# ---------------------------------------------------------------- registry and leases

def _touch_agent(conn, actor):
    """Renew every lease and target ownership the actor holds, and record that it was seen."""
    if not actor:
        return
    t = now()
    conn.execute("UPDATE agents SET last_seen=? WHERE name=?", (iso(t), actor))
    _renew(conn, actor, t)


def _renew(conn, actor, t):
    """Every lease, hold, goal and target the actor has runs from t."""
    for r in conn.execute("SELECT id FROM items WHERE assignee=? AND status='held'", (actor,)).fetchall():
        conn.execute("UPDATE items SET hold_expires_at=? WHERE id=?", (iso(_hold_until(conn, r["id"], actor, t)), r["id"]))
    owner_ttl = parse_duration(setting(conn, "owner_ttl", agent=actor))
    conn.execute("UPDATE targets SET owner_expires_at=? WHERE owner=?", (iso(t + owner_ttl), actor))
    # A goal owner keeps the goal while it runs river commands; it frees after goal_lease without one.
    for g in conn.execute("SELECT * FROM goals WHERE owner=? AND status='open'", (actor,)).fetchall():
        conn.execute("UPDATE goals SET owner_expires_at=? WHERE id=?", (iso(t + _goal_lease(conn, g, actor)), g["id"]))
    for r in conn.execute("SELECT id FROM items WHERE assignee=? AND status='in_progress'", (actor,)).fetchall():
        conn.execute("UPDATE items SET lease_expires_at=? WHERE id=?", (iso(t + _lease_for(conn, r["id"], actor)), r["id"]))


def _sweep(conn):
    t = iso(now())
    expired = conn.execute(
        "SELECT id, assignee FROM items WHERE status='in_progress' AND lease_expires_at < ?", (t,)).fetchall()
    busy = busy_now(conn, {r["assignee"] for r in expired}) if expired else set()
    for r in expired:
        if r["assignee"] in busy:  # a long command (a test run, a build): the agent works, and keeps its item
            conn.execute("UPDATE items SET lease_expires_at=? WHERE id=?",
                         (iso(now() + _lease_for(conn, r["id"], r["assignee"])), r["id"]))
            conn.execute("UPDATE agents SET busy_at=? WHERE name=?", (t, r["assignee"]))
            _event(conn, r["id"], "maxpm", f"lease renewed: a command runs in the session of {r['assignee']}")
            continue
        # The session may have done part or all of the work: the next taker checks first (maxpm check).
        conn.execute("UPDATE items SET status='open', assignee=NULL, claimed_at=NULL, lease_expires_at=NULL, "
                     "needs_check=1 WHERE id=?", (r["id"],))
        conn.execute("UPDATE items SET reserved_for=NULL, reserved_until=NULL, reserved_by=NULL "
                     "WHERE id=? AND reserved_for=?", (r["id"], r["assignee"]))
        _event(conn, r["id"], "maxpm", f"lease expired (was {r['assignee']}); back to open")
        _send(conn, "notice", "maxpm", f"your lease on #{r['id']} expired; the item is open again. "
              f"Claim it again if you still work on it: maxpm claim {r['id']}", to=r["assignee"], item_id=r["id"])
    # A session river started for an item that never ran a river command gives its pushes back.
    late = iso(now() - parse_duration(setting(conn, "connect_within")))
    for r in conn.execute("SELECT DISTINCT a.name FROM agents a JOIN items i ON i.reserved_for=a.name "
                          "WHERE i.reserved_until IS NOT NULL AND i.status='open' AND a.stop_at IS NULL AND "
                          "a.note LIKE ? AND a.last_seen=a.registered_at AND a.registered_at < ?",
                          (STARTED_NOTE + "%", late)).fetchall():
        _free_pushes(conn, r["name"], f"{r['name']} never connected (no maxpm command within connect_within)")
    # A stopped session, or one that is gone, takes no work: nothing stays reserved for it, with or
    # without a time limit (an item it released after the stop, a prerequisite of an item it held).
    for r in conn.execute("SELECT DISTINCT a.* FROM agents a JOIN items i ON i.reserved_for=a.name "
                          "WHERE i.status='open' AND a.kind='ai'").fetchall():
        state = _agent_state(conn, r)
        if state in ("stopped", "gone"):
            _free_reservations(conn, r["name"], f"{r['name']} was stopped" if state == "stopped" else f"{r['name']} is gone")
    # An item reserved for a name that is not registered any more: nobody could claim or give it.
    for r in conn.execute("SELECT DISTINCT reserved_for FROM items WHERE reserved_for IS NOT NULL AND status='open' "
                          "AND reserved_for NOT IN (SELECT name FROM agents)").fetchall():
        _free_reservations(conn, r["reserved_for"], f"{r['reserved_for']} is not registered")
    for r in conn.execute("SELECT id, title, reserved_for, reserved_by FROM items WHERE reserved_until < ? "
                          "AND status='open'", (t,)).fetchall():
        conn.execute("UPDATE items SET reserved_for=NULL, reserved_until=NULL, reserved_by=NULL WHERE id=?", (r["id"],))
        _event(conn, r["id"], "maxpm", f"push to {r['reserved_for']} expired; open to everyone")
        for who in {r["reserved_for"], r["reserved_by"]} - {None}:
            _send(conn, "notice", "maxpm", f"the push of #{r['id']} {r['title']} to {r['reserved_for']} expired "
                  f"without an answer; it is open to every agent again", to=who, item_id=r["id"])
    _cadence_sync(conn)  # the waits of a release cadence end and move here, not in the loop below
    for r in conn.execute("SELECT id, title, blocked_reason, blocked_set_by, doer FROM items WHERE blocked_until <= ? "
                          "AND blocked_reason IS NOT NULL AND COALESCE(blocked_set_by,'')<>?", (t, CADENCE_BY)).fetchall():
        conn.execute("UPDATE items SET blocked_reason=NULL, blocked_until=NULL, blocked_at=NULL, blocked_set_by=NULL "
                     "WHERE id=?", (r["id"],))
        _event(conn, r["id"], "maxpm", f"wait ended ({r['blocked_reason']}); outside blocker cleared")
        # A person's item reaches them through Needs you (sync_needs_you opens it once it is ready);
        # for other items, tell whoever set the wait.
        if r["blocked_set_by"] and r["doer"] != "human":
            _send(conn, "notice", "maxpm", f"the wait on #{r['id']} {r['title']} ended "
                  f"({r['blocked_reason']}); it can start now", to=r["blocked_set_by"], item_id=r["id"])
    _due_warnings(conn, t)
    for r in conn.execute("SELECT DISTINCT a.* FROM agents a JOIN queue_entries q ON q.agent=a.name").fetchall():
        if _agent_state(conn, r) == "gone":
            _drop_queue(conn, r["name"], "is gone")
    for r in conn.execute("SELECT name, owner FROM goals WHERE owner IS NOT NULL AND owner_expires_at < ?",
                          (t,)).fetchall():
        conn.execute("UPDATE goals SET owner=NULL, owner_expires_at=NULL, owner_lease=NULL WHERE name=?", (r["name"],))
        _event(conn, None, "maxpm", f"goal {r['name']} ownership expired (was {r['owner']})")
        _send(conn, "notice", "maxpm", f"your ownership of goal {r['name']} expired (goal_lease without a maxpm "
              f"command); nobody owns it now, and its items are open to every agent. "
              f"Take it again if you still work on it: maxpm goal own {r['name']}", to=r["owner"])
    # An owner whose session is gone reserves nothing: the goal is free at once, not after goal_lease.
    for r in conn.execute("SELECT DISTINCT a.* FROM agents a JOIN goals g ON g.owner=a.name WHERE a.kind='ai'").fetchall():
        if _agent_state(conn, r) == "gone":
            _release_goals(conn, r["name"], f"{r['name']} is gone")
    _question_nudges(conn)
    for r in conn.execute("SELECT id, title, assignee FROM items WHERE status='held' AND hold_expires_at < ?",
                          (t,)).fetchall():
        human = _human_prereqs(conn, r["id"])
        if human:
            _human_wait_ended(conn, r, human)
            continue
        _unhold(conn, r["id"], "maxpm", f"hold expired (was {r['assignee']}); released")
        _send(conn, "notice", "maxpm", f"your hold on #{r['id']} expired, so it is open to every agent now, and its "
              f"prerequisites are no longer reserved for you. Hold it again: maxpm keep {r['id']}",
              to=r["assignee"], item_id=r["id"])
    for r in conn.execute("SELECT name, owner FROM targets WHERE owner IS NOT NULL AND owner_expires_at < ?",
                          (t,)).fetchall():
        conn.execute("UPDATE targets SET owner=NULL, owner_expires_at=NULL WHERE name=?", (r["name"],))
        _event(conn, None, "maxpm", f"target {r['name']} ownership expired (was {r['owner']})")
        _send(conn, "notice", "maxpm", f"your ownership of target {r['name']} expired; nobody owns it now. "
              f"Take it again if you still deploy there: maxpm target own {r['name']}", to=r["owner"])
    return [r["id"] for r in expired]


def _human_wait_ended(conn, r, human):
    """An agent waited human_wait_max on a person's item: release its item (it still waits on the person),
    send the agent to other work, and remind every person of the answer it waits for."""
    wait = setting(conn, "human_wait_max", item_id=r["id"], agent=r["assignee"])
    names = ", ".join(f"#{h} {_item(conn, h)['title']}" for h in human)
    _unhold(conn, r["id"], "maxpm", f"waited {wait} (human_wait_max) on {names}; released (was {r['assignee']})")
    _send(conn, "notice", "maxpm", f"you waited {wait} (human_wait_max) for a person on {names}, so #{r['id']} "
          f"{r['title']} is open again and still waits on it. Take other work now: maxpm go. When the person "
          f"finishes, #{r['id']} is ready for whoever runs go.", to=r["assignee"], item_id=r["id"])
    for h in (x["name"] for x in conn.execute("SELECT name FROM agents WHERE kind='human' ORDER BY name").fetchall()):
        _send(conn, "alert", "maxpm", f"#{r['id']} {r['title']} waits on you: {names}. {r['assignee']} waited "
              f"{wait} and took other work; the item continues when you finish.", to=h, item_id=human[0])


def _due_warnings(conn, t):
    """Warn every person once when an open item's own due date comes close (due_warn_before), and
    once more when it passes. The warning is an alert, so it shows in Needs you and notifies."""
    zone = setting(conn, "timezone")
    humans = [r["name"] for r in conn.execute("SELECT name FROM agents WHERE kind='human' ORDER BY name")]
    for r in conn.execute(f"SELECT id, title, due, due_warned FROM items WHERE due IS NOT NULL "
                          f"AND status IN {OPEN_STATES} AND due_warned < 2").fetchall():
        soon = iso(parse_iso(r["due"]) - parse_duration(setting(conn, "due_warn_before", item_id=r["id"])))
        stage = 2 if r["due"] <= t else 1 if soon <= t else 0
        if stage <= r["due_warned"]:
            continue
        conn.execute("UPDATE items SET due_warned=? WHERE id=?", (stage, r["id"]))
        left_ = _open_prereqs(conn, r["id"])
        text = (f"#{r['id']} {r['title']} " + ("is past its due date" if stage == 2 else "is due soon")
                + f" ({show_time(r['due'], zone)}). "
                + (f"Still open before it: {', '.join('#' + str(x) for x in left_)}." if left_ else "Nothing waits before it."))
        _event(conn, r["id"], "maxpm", "overdue" if stage == 2 else "due soon")
        for h in humans:
            _send(conn, "alert", "maxpm", text, to=h)


def _question_nudges(conn):
    """A question unanswered after question_nudge_after whose receiver is away or gone: tell the asker
    once, so it can ask someone else (design 7.4). The receiver's own count says how long it waits."""
    for r in conn.execute("SELECT m.id, m.from_agent, m.to_agent, m.item_id, m.created_at, a.last_seen "
                          "FROM messages m JOIN agents a ON a.name=m.to_agent WHERE m.kind='question' "
                          "AND m.state='open' AND m.nudged_at IS NULL").fetchall():
        wait = parse_duration(setting(conn, "question_nudge_after", agent=r["to_agent"]))
        if parse_iso(r["created_at"]) + wait > now():
            continue
        state = _agent_state(conn, {"name": r["to_agent"], "last_seen": r["last_seen"]})
        if state == "active":
            continue
        conn.execute("UPDATE messages SET nudged_at=? WHERE id=?", (iso(now()), r["id"]))
        _send(conn, "notice", "maxpm", f"{r['to_agent']} is {state} (last seen {r['last_seen'][:16].replace('T', ' ')} UTC) "
              f"and has not answered your question #{r['id']}. Ask someone else"
              + (f": maxpm ask --holder-of {r['item_id']} \"...\", or maxpm who" if r["item_id"] else ": maxpm who"),
              to=r["from_agent"], item_id=r["item_id"], reply_to=r["id"])
    # Other messages (alerts, notes, offers) that a gone agent never read: tell the sender once.
    for r in conn.execute("SELECT m.id, m.kind, m.from_agent, m.to_agent, m.item_id, a.last_seen FROM messages m "
                          "JOIN agents a ON a.name=m.to_agent WHERE m.kind IN ('alert','note','offer') "
                          "AND m.read_at IS NULL AND m.nudged_at IS NULL AND m.from_agent<>'maxpm'").fetchall():
        if _agent_state(conn, {"name": r["to_agent"], "last_seen": r["last_seen"]}) != "gone":
            continue
        conn.execute("UPDATE messages SET nudged_at=? WHERE id=?", (iso(now()), r["id"]))
        _send(conn, "notice", "maxpm", f"{r['to_agent']} is gone (last seen {r['last_seen'][:16].replace('T', ' ')} UTC) "
              f"and never read your {r['kind']} #{r['id']}. Send it to someone else if it still matters (maxpm who)",
              to=r["from_agent"], item_id=r["item_id"], reply_to=r["id"])


PROC_RUNNER = None  # tests: returns the lines "pid ppid elapsed" of every process, as ps prints them


def _elapsed(text):
    """The seconds of a ps elapsed time: [[dd-]hh:]mm:ss."""
    days, _, rest = text.rpartition("-")
    parts = [int(x) for x in rest.split(":")]
    return int(days or 0) * 86400 + sum(x * 60 ** n for n, x in enumerate(reversed(parts)))


def _process_list():
    """The output of one ps call (pid, parent, and age of every process), or None with no ps (Windows, a sandbox
    that blocks it)."""
    import subprocess
    if PROC_RUNNER is None and os.name == "nt":
        return None
    try:
        return PROC_RUNNER() if PROC_RUNNER else subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,etime="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def busy_now(conn, names=None):
    """The agents (among names; else every AI agent) in whose session a command runs now: the agent CLI process
    runs on this computer, and a process below it started after the agent's last river command (a test run, a
    build, a merge). The servers the CLI started with are older, and so is a maxpm wait, which renews last_seen
    itself. Only within busy_max of the last river command. One ps call; an empty set with no ps (Windows, a
    sandbox that blocks it)."""
    limit = parse_duration(setting(conn, "busy_max"))
    t, host = now(), this_host()
    rows = [r for r in conn.execute("SELECT name, pid, last_seen FROM agents WHERE kind='ai' AND pid IS NOT NULL AND host=?",
                                    (host,)).fetchall()
            if (names is None or r["name"] in names) and t - parse_iso(r["last_seen"]) < limit]
    if not rows:
        return set()
    text = _PROCS.get()  # taken before the write lock of the transaction that runs now (tx)
    if text is None:
        text = _process_list()
    if not text:
        return set()
    below, started = {}, {}
    for line in text.splitlines():
        f = line.split()
        try:
            pid, ppid, age = int(f[0]), int(f[1]), _elapsed(f[2])
        except (IndexError, ValueError):
            continue
        below.setdefault(ppid, []).append(pid)
        started[pid] = t - timedelta(seconds=age)
    out = set()
    for r in rows:
        if r["pid"] not in started:
            continue  # the agent CLI ended
        after = parse_iso(r["last_seen"]) + timedelta(seconds=1)  # ps counts whole seconds
        todo, seen = list(below.get(r["pid"], ())), set()
        while todo:
            pid = todo.pop()
            if pid in seen:
                continue
            seen.add(pid)
            if started[pid] > after:
                out.add(r["name"])
                break
            todo += below.get(pid, ())
    return out


def keep_busy(conn, names, waiting=()):
    """maxpm serve saw these agents busy with no river command (busy_now, or their tmux pane): their leases, holds,
    goals and targets run from now, as after a river command. last_seen stays the time of the last river command.
    busy_max does not limit the agents in waiting (waits_on_person): a person's answer can take a night."""
    limit = parse_duration(setting(conn, "busy_max"))
    t, kept = now(), []
    with tx(conn):
        for r in conn.execute("SELECT name, last_seen FROM agents WHERE kind='ai'").fetchall():
            if r["name"] in waiting or (r["name"] in names and t - parse_iso(r["last_seen"]) < limit):
                conn.execute("UPDATE agents SET busy_at=? WHERE name=?", (iso(t), r["name"]))
                _renew(conn, r["name"], t)
                kept.append(r["name"])
    return kept


def _lease_lost(conn):
    """{item: (agent row, when)} for the open items whose lease ran out and that nobody claimed since, when the
    agent is registered on this computer and is not asked to stop."""
    rows = conn.execute(
        "SELECT i.id, e.at, e.change FROM items i JOIN events e ON e.id=(SELECT MAX(id) FROM events WHERE item_id=i.id "
        "AND (change LIKE 'lease expired (was %' OR change LIKE 'claimed%')) "
        "WHERE i.status='open' AND i.needs_check=1 AND e.change LIKE 'lease expired (was %'").fetchall()
    was = {}
    for r in rows:
        m = re.match(r"lease expired \(was (.+)\); back to open$", r["change"])
        a = m and conn.execute("SELECT name, busy_at FROM agents WHERE name=? AND kind='ai' AND stop_at IS NULL "
                               "AND pid IS NOT NULL AND host=?", (m.group(1), this_host())).fetchone()
        if a:
            was[r["id"]] = (a, r["at"])
    return was


def still_worked(conn):
    """{item: agent} for the open items whose lease ran out while the agent's session still works on them: nobody
    claimed the item since, the agent is registered on this computer and is not asked to stop, and a command runs
    in its session now, or maxpm serve saw it busy after the lease ran out and within the last lease_ttl. No
    second agent takes such an item (two sessions in one folder); the first one claims it again, or a person or
    a manager stops it."""
    was = _lease_lost(conn)
    if not was:
        return {}
    busy, t = busy_now(conn, {a["name"] for a, _ in was.values()}), now()
    out = {}
    for i, (a, at) in was.items():
        recent = a["busy_at"] and a["busy_at"] > at and t - parse_iso(a["busy_at"]) < parse_duration(
            setting(conn, "lease_ttl", item_id=i, agent=a["name"]))
        if a["name"] in busy or recent:
            out[i] = a["name"]
    return out


def activity(conn, actor, tries=None):
    """Run at the start of every command: expire old leases, then renew the actor's own."""
    with tx(conn, tries):
        expired = _sweep(conn)
        _touch_agent(conn, actor)
        sync_needs_you(conn)
    return expired


POLL_SWEEP = timedelta(seconds=30)  # a command that polls runs the whole activity this often
_POLL_SWEPT = {}  # database file -> when a poll of this process last ran the whole activity


def poll_activity(conn, actor):
    """activity for a command that polls every few seconds (maxpm wait, maxpm manage --watch, the state read
    of the page). Each poll only records that the actor is there and renews its leases, in one short
    transaction; the sweep and sync_needs_you run each POLL_SWEEP, because every ordinary command and maxpm
    serve run them too (#1617: ten polls held the write lock a large part of the time). When the queue stays
    locked for one busy_timeout, the poll skips this renewal and the next one tries again: a lock never ends
    a wait (#1615)."""
    key, t = conn.execute("PRAGMA database_list").fetchone()[2], now()
    last = _POLL_SWEPT.get(key)
    try:
        if last is None or not timedelta(0) <= t - last < POLL_SWEEP:
            out = activity(conn, actor, tries=1)
            _POLL_SWEPT[key] = t
            return out
        if actor:
            with tx(conn, 1):
                _touch_agent(conn, actor)
    except RiverLocked:
        pass
    return []


def session_url_from_env(env=None):
    """The web link of the Claude Code session this command runs in, when Remote Control gives it one."""
    sid = (env if env is not None else os.environ).get("CLAUDE_CODE_BRIDGE_SESSION_ID", "").strip()
    return f"https://claude.ai/code/{sid}" if re.match(r"^session_[A-Za-z0-9]+$", sid) else None


def record_session_url(conn, name, url):
    """Keep an agent's session link current, so a notification about its work opens that session."""
    if not (name and url):
        return
    with tx(conn):
        conn.execute("UPDATE agents SET session_url=? WHERE name=? AND kind='ai' AND session_url IS NOT ?",
                     (url, name, url))


def origin_session_url(conn, item_id=None, message_id=None):
    """The session link of the agent that asked (a message) or added the item (its first event), if known."""
    who = None
    if message_id is not None:
        r = conn.execute("SELECT from_agent FROM messages WHERE id=?", (message_id,)).fetchone()
        who = r and r["from_agent"]
    elif item_id is not None:
        r = conn.execute("SELECT actor FROM events WHERE item_id=? ORDER BY id LIMIT 1", (item_id,)).fetchone()
        who = r and r["actor"]
    if not who:
        return None
    r = conn.execute("SELECT session_url FROM agents WHERE name=?", (who,)).fetchone()
    return r and r["session_url"]


# ---------------------------------------------------------------- needs you

NOTIFY_MAX_ATTEMPTS = 5


def _channels(value):
    return [c.strip() for c in (value or "").split(",") if c.strip()]


def _open_event(conn, kind, summary, item_id=None, message_id=None, human=None):
    t = iso(now())
    eid = conn.execute("INSERT INTO needs_you(kind,item_id,message_id,human,summary,opened_at) VALUES (?,?,?,?,?,?)",
                       (kind, item_id, message_id, human, summary, t)).lastrowid
    for ch in _channels(setting(conn, "notify_channels", item_id=item_id, agent=human)):
        conn.execute("INSERT OR IGNORE INTO notifications(event_id,channel,created_at) VALUES (?,?,?)", (eid, ch, t))
    if item_id is not None:
        _event(conn, item_id, "maxpm", f"needs you: {summary}")
    return eid


def sync_needs_you(conn):
    """Open and close needs-you events so they match the queue. Safe to run any number of times.

    Runs inside the caller's transaction. Opens an event for each ready human
    item and each open question or unread alert to a human, and closes events
    whose reason is gone (done, dropped, claimed, blocked again, answered, read).
    """
    humans = {r["name"] for r in conn.execute("SELECT name FROM agents WHERE kind='human'")}
    ann = annotate(conn)
    want_items = {a["id"]: a for a in ann.values()
                  if a["ready"] and a["doer"] == "human" and not a["project_archived"]}
    want_msgs = {}
    if humans:
        for r in conn.execute(
                f"SELECT * FROM messages WHERE kind IN ('question','alert') AND to_agent IN ({','.join('?' * len(humans))}) "
                "AND ((kind='question' AND state='open') OR (kind='alert' AND read_at IS NULL))", sorted(humans)):
            want_msgs[r["id"]] = r
    t = iso(now())
    for ev in conn.execute("SELECT * FROM needs_you WHERE closed_at IS NULL").fetchall():
        if ev["kind"] == "item" and ev["item_id"] not in want_items:
            it = ann.get(ev["item_id"])
            why = (it["status"] if it and it["status"] in CLOSED_STATES
                   else "claimed" if it and it["status"] in ("in_progress", "held")
                   else "no longer ready")
            conn.execute("UPDATE needs_you SET closed_at=?, close_reason=? WHERE id=?", (t, why, ev["id"]))
        elif ev["kind"] == "message" and ev["message_id"] not in want_msgs:
            m = conn.execute("SELECT kind, state FROM messages WHERE id=?", (ev["message_id"],)).fetchone()
            why = m["state"] if m and m["state"] != "open" else "read"
            conn.execute("UPDATE needs_you SET closed_at=?, close_reason=? WHERE id=?", (t, why, ev["id"]))
    have_items = {r[0] for r in conn.execute("SELECT item_id FROM needs_you WHERE kind='item' AND closed_at IS NULL")}
    have_msgs = {r[0] for r in conn.execute("SELECT message_id FROM needs_you WHERE kind='message' AND closed_at IS NULL")}
    for iid in sorted(set(want_items) - have_items):
        a = want_items[iid]
        _open_event(conn, "item", f"#{iid} {a['title']} ({a['project']}) is ready for a person", item_id=iid)
    for mid in sorted(set(want_msgs) - have_msgs):
        m = want_msgs[mid]
        _open_event(conn, "message", f"{m['kind']} from {m['from_agent']}: {m['body'][:120]}",
                    item_id=m["item_id"], message_id=mid, human=m["to_agent"])


def needs_you(conn, human=None, include_closed=False, limit=100):
    """Needs-you events for one person (theirs and the ones for anyone), most important first.

    Open events come before closed ones. Among open events, questions and alerts come
    first, oldest first, because an agent waits on the answer now; then items in
    queue order (effective priority, project rank, what they unblock). Closed
    events follow, newest first. Each item event carries its effective priority,
    where that priority comes from, and how many open items it unblocks."""
    with tx(conn):
        sync_needs_you(conn)
    sql = ("SELECT n.*, i.title item_title, p.name project, m.kind message_kind, m.from_agent, m.body "
           "FROM needs_you n LEFT JOIN items i ON i.id=n.item_id LEFT JOIN projects p ON p.id=i.project_id "
           "LEFT JOIN messages m ON m.id=n.message_id WHERE 1=1")
    args = []
    if not include_closed:
        sql += " AND n.closed_at IS NULL"
    if human is not None:
        sql += " AND (n.human IS NULL OR n.human=?)"
        args.append(human)
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY n.id DESC", args)]
    ann = annotate(conn)
    for r in rows:
        a = ann.get(r["item_id"]) if r["kind"] == "item" else None
        r["priority"] = a["effective_priority"] if a else None
        r["priority_from"] = a["priority_from"] if a else None
        r["unblocks_count"] = a["unblocks_count"] if a else 0
        if r["closed_at"]:
            r["_key"] = (3, -r["id"])
        elif a:
            r["_key"] = (1,) + a["sort_key"]
        elif r["kind"] == "message":
            r["_key"] = (0, r["id"])
        else:
            r["_key"] = (2, -r["id"])  # an item the queue no longer knows
    rows.sort(key=lambda r: r["_key"])
    rows = rows[:limit]
    for r in rows:
        del r["_key"]
        r["notifications"] = [dict(x) for x in conn.execute(
            "SELECT channel, state, attempts, last_error, sent_at FROM notifications WHERE event_id=? ORDER BY channel",
            (r["id"],))]
    return rows


def outbox(conn, channel=None):
    """Notifications still to send: pending, or failed with attempts left. For the dispatcher."""
    sql = ("SELECT o.*, n.summary, n.kind, n.item_id, n.message_id, n.human "
           "FROM notifications o JOIN needs_you n ON n.id=o.event_id "
           "WHERE (o.state='pending' OR (o.state='failed' AND o.attempts < ?)) "
           "AND n.closed_at IS NULL")  # a person already acted: do not notify about it
    args = [NOTIFY_MAX_ATTEMPTS]
    if channel:
        sql += " AND o.channel=?"
        args.append(channel)
    return [dict(r) for r in conn.execute(sql + " ORDER BY o.id", args)]


def outbox_mark(conn, notification_id, ok, error=None):
    """Record one send attempt: sent, or failed with the error (it retries until NOTIFY_MAX_ATTEMPTS)."""
    with tx(conn):
        if not conn.execute("SELECT 1 FROM notifications WHERE id=?", (notification_id,)).fetchone():
            raise RiverError(f"no notification {notification_id}")
        if ok:
            conn.execute("UPDATE notifications SET state='sent', attempts=attempts+1, sent_at=?, last_error=NULL "
                         "WHERE id=?", (iso(now()), notification_id))
        else:
            conn.execute("UPDATE notifications SET state='failed', attempts=attempts+1, last_error=? WHERE id=?",
                         (str(error or "unknown error")[:500], notification_id))
    return dict(conn.execute("SELECT * FROM notifications WHERE id=?", (notification_id,)).fetchone())


def register(conn, name, human=False, note="", session=None):
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", name):
        raise RiverError("agent names use letters, digits, '.', '_', '-' (up to 64)")
    if name.lower() == COMMAND:
        raise RiverError(f"{name} is the name MaximizePM signs its own notices with; pick another name")
    t = iso(now())
    with tx(conn):
        conn.execute(
            "INSERT INTO agents(name,kind,note,registered_at,last_seen) VALUES (?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, note=excluded.note, last_seen=excluded.last_seen",
            (name, "human" if human else "ai", note, t, t))
        _event(conn, None, name, f"registered as {'human' if human else 'ai'}")
    if session:
        set_session(conn, name, session)
    return agent_status(conn, name)


def set_session(conn, name, session, ref=None):
    """Record the Claude Code session an agent runs in, so people and agents can message that session.

    Session names can repeat, so keep the short ref too: ListAgents prints 'name [ref]', and either
    form is accepted here ('name [ref]' in one string, or name plus ref)."""
    # A name can have spaces: river names the sessions it starts '#<id> <title>'.
    m = re.match(r"^([^\[\]\x00-\x1f]+?)(?:\[\s*([0-9A-Za-z]+)\s*\])?$", " ".join((session or "").split()))
    if not m or len(m.group(1).strip()) > 128:
        raise RiverError("a session name is one line of up to 128 characters, optionally with its ref: "
                         "maxpm session \"<name>\" --ref <ref>  (ListAgents prints 'This session is <name> [<ref>]')")
    session, ref = m.group(1).strip(), (ref or m.group(2) or "").strip("[] ") or None
    if ref and not re.match(r"^[0-9A-Za-z]{1,32}$", ref):
        raise RiverError("a session ref is the short code in brackets that ListAgents prints, such as b5e2e0")
    with tx(conn):
        old = _agent(conn, name)
        if (old["session"], old["session_ref"]) != (session, ref):
            conn.execute("UPDATE agents SET session=?, session_ref=? WHERE name=?", (session, ref, name))
            _event(conn, None, name, f"session: {session}" + (f" [{ref}]" if ref else ""))
    return agent_status(conn, name)


def unregister(conn, name, actor=None):
    with tx(conn):
        _agent(conn, name)
        held = conn.execute("SELECT id FROM items WHERE assignee=? AND status IN ('in_progress','held')", (name,)).fetchall()
        if held:
            raise RiverError(f"{name} holds {', '.join('#' + str(r['id']) for r in held)}; release those first")
        owned = [r["name"] for r in conn.execute("SELECT name FROM targets WHERE owner=?", (name,))]
        if owned:
            raise RiverError(f"{name} owns target {', '.join(owned)}; release or give it first "
                             f"(maxpm target release <t>, maxpm target give <t> --to <agent>)")
        _drop_queue(conn, name, "stopped")
        _free_reservations(conn, name, f"{name} ended")
        _release_goals(conn, name, f"{name} ended")
        conn.execute("DELETE FROM agents WHERE name=?", (name,))
        conn.execute("DELETE FROM settings WHERE scope=?", (f"agent:{name}",))
        _event(conn, None, actor or name, f"agent {name} unregistered")
    return {"unregistered": name}


def agent_note(conn, name, note):
    with tx(conn):
        _agent(conn, name)
        conn.execute("UPDATE agents SET note=? WHERE name=?", (note, name))
    return agent_status(conn, name)


def _agent(conn, name):
    r = conn.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone()
    if not r:
        raise RiverError(f"agent {name!r} is not registered; run: maxpm register {name} [--human] [--note ...]")
    return r


def _agent_state(conn, a):
    if "stop_at" in a.keys() and a["stop_at"]:
        return "stopped"
    # Its process on this host has ended: gone at once, not after gone_after.
    if "pid" in a.keys() and a["pid"] and a["host"] == this_host() and not pid_alive(a["pid"]):
        return "gone"
    age = now() - parse_iso(a["last_seen"])
    if age > parse_duration(setting(conn, "gone_after", agent=a["name"])):
        return "gone"
    if age > parse_duration(setting(conn, "away_after", agent=a["name"])):
        return "away"
    return "active"


# The note of a session that river started for an item (Start, Dispatch, maxpm launch).
STARTED_NOTE = "started from the page for"
# The first words of the alert river sends for an agent that waits on a prompt in its terminal
# (server.watch_prompts); the page and the notification link open that agent's Terminal for it.
PROMPT_NOTE = "waits on a prompt in its terminal"


def prompt_alert_agent(conn, message_id):
    """The agent whose terminal prompt this alert is about, or None for any other message."""
    r = conn.execute("SELECT from_agent, body FROM messages WHERE id=? AND kind='alert'", (message_id,)).fetchone() \
        if message_id is not None else None
    return r["from_agent"] if r and r["body"].startswith(PROMPT_NOTE) else None


def not_connected(conn, a):
    """A session river started for an item that has run no river command since, for longer than connect_within."""
    return (a["kind"] == "ai" and (a["note"] or "").startswith(STARTED_NOTE) and a["last_seen"] == a["registered_at"]
            and not a["stop_at"] and now() - parse_iso(a["registered_at"]) > parse_duration(setting(conn, "connect_within")))


def agent_status(conn, name):
    a = dict(_agent(conn, name))
    a["state"] = _agent_state(conn, a)
    a["can_kill"] = bool(a.get("pid")) and a.get("host") == this_host() and a["state"] != "gone"
    a["holds"] = [dict(r) for r in conn.execute(
        "SELECT id, title, status, lease_expires_at, hold_expires_at FROM items WHERE assignee=? "
        "AND status IN ('in_progress','held') ORDER BY id",
        (name,))]
    a["owns"] = [dict(r) for r in conn.execute(
        "SELECT name, owner_expires_at FROM targets WHERE owner=? ORDER BY name", (name,))]
    return a


def _rel_path(path, root, cwd=None):
    """A path as the touches of a project in `root` write it: relative, no './'. A relative path is
    taken from `cwd` when that lies inside the project, else as already relative to the project."""
    p = os.path.expanduser(path)
    r = os.path.realpath(os.path.expanduser(root)) if root else None
    inside = lambda q: q == r or q.startswith(r.rstrip(os.sep) + os.sep)
    if not os.path.isabs(p) and cwd and r and inside(os.path.realpath(os.path.join(cwd, p))):
        p = os.path.join(cwd, p)
    if os.path.isabs(p):
        p = os.path.realpath(p)
        if not r or not inside(p):
            return None
        p = os.path.relpath(p, r)
    p = os.path.normpath(p)
    return "" if p == "." else p


def ended(a):
    """A stopped or gone agent (an agent_status) that holds no item and owns no target: who and status
    leave it out unless asked, because most registered sessions ended long ago."""
    return a["state"] in ("stopped", "gone") and not a["holds"] and not a["owns"]


def who(conn, item=None, project=None, file=None, cwd=None, everyone=True):
    """Every agent and what it holds; or only the holder of an item, the agents in a project, or the
    agents whose held items touch a file or directory (each with the matching `touching` paths).
    everyone=False leaves out the ended agents (stopped or gone, holding and owning nothing)."""
    agents = [agent_status(conn, r["name"]) for r in conn.execute("SELECT name FROM agents ORDER BY name")]
    if not everyone:
        agents = [a for a in agents if not ended(a)]
    if file is not None:
        rows = conn.execute("SELECT i.id, i.title, i.assignee, i.touches, p.path FROM items i "
                            "JOIN projects p ON p.id=i.project_id "
                            "WHERE i.status IN ('in_progress','held') AND i.touches<>''").fetchall()
        by = {}
        for r in rows:
            rel = _rel_path(file, r["path"], cwd)
            if rel is None:
                continue
            hit = [t for t in touches_list(r["touches"]) if rel == "" or _paths_overlap(rel, t)]
            if hit:
                by.setdefault(r["assignee"], []).append({"id": r["id"], "title": r["title"], "paths": hit})
        for a in agents:
            a["touching"] = by.get(a["name"], [])
        return [a for a in agents if a["touching"]]
    if item is not None:
        it = _item(conn, item)
        return [a for a in agents if a["name"] == it["assignee"]]
    if project is not None:
        p = _project(conn, project)
        ids = {r["id"] for r in conn.execute("SELECT id FROM items WHERE project_id=?", (p["id"],))}
        return [a for a in agents if any(h["id"] in ids for h in a["holds"])]
    return agents


def _claim_row(conn, item_id, actor):
    if not actor:
        raise RiverError("claiming needs an agent name: set MAXPM_AGENT or pass --as <name>")
    ag = _agent(conn, actor)
    if ag["stop_at"]:
        raise RiverError(f"refused: {actor} is asked to stop (by {ag['stop_by']}: {ag['stop_reason']}); it takes no "
                         f"new work. Commit finished work, release your item, then end the session")
    it0 = _item(conn, item_id)
    if ag["role"] == "context":
        raise RiverError(f"refused: {actor} is a context session: it reads a goal's handoff, items and files, "
                         f"runs maxpm goal base <goal> --ready, and ends. It claims no item and edits no file")
    if ag["role"] in ("planner", "manager"):
        raise RiverError(f"refused: {actor} is a {ag['role']} session; it changes the plan and does not take work. "
                         f"To work instead, run: maxpm --as {actor} go" + (
                             f". #{item_id} is reserved for you: start a session for it (maxpm launch --item {item_id}), "
                             f"or open it to every agent (maxpm edit {item_id} --unreserve)"
                             if it0["reserved_for"] == actor and it0["status"] == "open" else ""))
    if it0["doer"] == "human" and ag["kind"] == "ai":
        raise RiverError(f"refused: #{item_id} is for a person. If you can do it or work around it, take it over "
                         f"(the user is told, and can undo it): maxpm takeover {item_id} --note \"<how you will do it>\"")
    if it0["reserved_for"] and it0["reserved_for"] != actor:
        raise RiverError(f"refused: #{item_id} is reserved for {it0['reserved_for']}"
                         + ("" if it0["reserved_until"] else ", who holds the item it unblocks or was given it")
                         + f"; {_unreserve_hint(item_id)}")
    q = conn.execute("SELECT agent FROM queue_entries WHERE item_id=?", (item_id,)).fetchone()
    if q and q["agent"] != actor and ag["kind"] == "ai":
        raise RiverError(f"refused: #{item_id} is in the queue of {q['agent']} (maxpm queue list {q['agent']})")
    # A goal's items are its owner's, unless one was pushed or given to this agent.
    if ag["kind"] == "ai" and it0["doer"] != "human" and it0["reserved_for"] != actor:
        g = conn.execute("SELECT g.name, g.owner FROM item_goals ig JOIN goals g ON g.id=ig.goal_id WHERE ig.item_id=? "
                         "AND g.status='open' AND g.owner IS NOT NULL AND g.owner<>? AND g.owner_expires_at >= ? "
                         "ORDER BY g.rank LIMIT 1", (item_id, actor, iso(now()))).fetchone()
        if g:
            raise RiverError(f"refused: #{item_id} is reserved for {g['owner']}, who owns goal {g['name']}. "
                             f"Offer help: maxpm offer \"<what you can take>\" --goal {g['name']}")
    # A hold does not use up a lease when the agent takes one of its own reserved prerequisites.
    held, limit, key = _lease_room(conn, item_id, actor, ("in_progress",) if it0["reserved_for"] == actor
                                   else ("in_progress", "held"))
    if held >= limit:
        what = "items of its own goals and targets" if key == "goal_max_leases" else "item(s)"
        raise RiverError(f"refused: {actor} already holds {held} {what} ({key} {limit}). "
                         f"Finish one (maxpm done <id>), release one (maxpm release <id>), "
                         f"or raise the limit (maxpm config set {key} <n> --agent {actor})")
    # An item pushed to this session (Dispatch, maxpm push): a person chose the session, so its agent type
    # does not matter; model limits still do.
    why = _model_refusal(conn, it0, actor, any_type=it0["reserved_for"] == actor)
    if why:
        raise RiverError(f"refused: #{item_id} {why}. Take other work (maxpm go), or a session with an allowed "
                         f"model or agent type takes it; a planner can change them: maxpm edit {item_id} "
                         f"--min-model/--max-model/--agent")
    it = _item(conn, item_id)
    if it["kind"] == "deploy":
        owner = conn.execute("SELECT owner FROM targets WHERE name=?", (it["target"],)).fetchone()
        if not owner or owner["owner"] != actor:
            raise RiverError(f"refused: #{item_id} deploys to target {it['target']}, and only its owner can take it "
                             f"(owner: {owner['owner'] if owner and owner['owner'] else 'nobody'}). "
                             f"Become the owner first: maxpm target own {it['target']}")
    ttl = _lease_for(conn, item_id, actor)
    t = now()
    cur = conn.execute(
        "UPDATE items SET status='in_progress', assignee=?, claimed_at=?, lease_expires_at=?, fresh_start=NULL "
        "WHERE id=? AND status='open'", (actor, iso(t), iso(t + ttl), item_id))
    if cur.rowcount != 1:
        raise RiverError(f"item {item_id} is no longer open")
    if it0["reserved_until"]:
        conn.execute("UPDATE items SET reserved_until=NULL WHERE id=?", (item_id,))
        conn.execute("UPDATE messages SET state='accepted', read_at=COALESCE(read_at, ?) "
                     "WHERE kind='alert' AND item_id=? AND to_agent=? AND state='open'", (iso(t), item_id, actor))
    _event(conn, item_id, actor, f"claimed (lease {_short(ttl)})")
    if it0["kind"] in ("review", "deploy"):
        _cut_on_claim(conn, it0, actor)  # the release holds a fixed list of items from now on
    if conn.execute("DELETE FROM queue_entries WHERE item_id=?", (item_id,)).rowcount:
        _event(conn, item_id, actor, "left the queue (claimed)")
    _goal_notice(conn, item_id, actor, "claimed")
    if it["kind"] == "deploy":
        _add_monitor(conn, it, actor)
    return ag


def _model_refusal(conn, it, actor, any_type=False):
    """Why the actor's model or agent type may not take this item (its min/max limits, its agent), or None."""
    model, stype = agent_model(conn, actor), None if any_type else session_type(conn, actor)
    if not (model or stype):
        return None
    ladder_text = setting(conn, "model_ladder")
    m = _item_models(it, _project_name(conn, it["project_id"]), _model_defaults(conn), ladder_text)
    ok, why = agent_type_check(stype, m["agent"])
    if ok and model:
        ok, why = model_check(parse_ladder(ladder_text), model, m["min_model"], m["max_model"])
    return None if ok else why


def next_item(conn, project=None, unblocks=None, claim=False, actor=None, limit=1, near=None, mine=False,
              model=None, skipped=None, folderless=False):
    """Show, or claim, the first ready item from the area the agent chose.

    model: the session's model (default: the one the agent recorded). Items whose min/max limits exclude it
    are left out; `skipped`, a list, receives them with the reason. folderless (a chat app session): items
    that need a folder are left out too (needs_folder)."""
    model = model or agent_model(conn, actor)
    ladder = parse_ladder(setting(conn, "model_ladder")) if model else None
    stype = session_type(conn, actor)
    doer_for = None
    if actor:
        r = conn.execute("SELECT kind FROM agents WHERE name=?", (actor,)).fetchone()
        doer_for = r["kind"] if r else None
    if mine and not actor:
        raise RiverError("--mine needs an agent name: set MAXPM_AGENT or pass --as <name>")
    who_mine = actor if mine else None

    def fits(pool):
        if not (model or stype):
            return pool
        out = []
        for a in pool:
            ok, why = agent_type_check(None if actor and a["reserved_for"] == actor else stype, a["agent"])
            if ok and model:
                ok, why = model_check(ladder, model, a["min_model"], a["max_model"])
            if ok:
                a["model_note"] = why
                out.append(a)
            elif skipped is not None and a["id"] not in {x["id"] for x in skipped}:
                skipped.append({"id": a["id"], "title": a["title"], "why": why})
        return out

    def takeable(pool):
        if not actor:
            return fits(pool)
        owned = {r["name"] for r in conn.execute("SELECT name FROM targets WHERE owner=?", (actor,))}
        # An item an answer just came for goes to a fresh session (maxpm serve starts it within a pass or two),
        # not to a session that waits; with no maxpm serve, the next go takes it after FRESH_GRACE.
        fresh = {r["id"] for r in conn.execute("SELECT id FROM items WHERE fresh_start > ?",
                                               (iso(now() - FRESH_GRACE),))}
        pool = [a for a in pool if (a["kind"] != "deploy" or a["target"] in owned)
                and a["reserved_for"] in (None, actor) and a["id"] not in fresh]
        # An item whose lease ran out while its agent's session still works on it: no second agent takes it.
        still = still_worked(conn)
        for a in [a for a in pool if still.get(a["id"], actor) != actor]:
            if skipped is not None and a["id"] not in {x["id"] for x in skipped}:
                skipped.append({"id": a["id"], "title": a["title"], "why": f"{still[a['id']]} still works on it in its session"})
        pool = [a for a in pool if still.get(a["id"], actor) == actor]
        for a in [a for a in pool if author_refusal(conn, a, actor)]:
            if skipped is not None and a["id"] not in {x["id"] for x in skipped}:
                skipped.append({"id": a["id"], "title": a["title"], "why": "you worked on the release it reviews"})
            pool.remove(a)
        # The agent's own queue comes first, in its order and from any project; then items pushed to it.
        # The sort is stable, so graph order holds inside each group.
        qpos = {r["item_id"]: n for n, r in enumerate(conn.execute(
            "SELECT item_id FROM queue_entries WHERE agent=? AND item_id IS NOT NULL ORDER BY pos", (actor,)))}
        have = {a["id"] for a in pool}
        pool = [a for a in _queue_ready(conn, actor) if a["id"] not in have and not author_refusal(conn, a, actor)] + pool
        pool = fits(pool)
        if folderless:
            for a in [a for a in pool if needs_folder(a)]:
                if skipped is not None and a["id"] not in {x["id"] for x in skipped}:
                    skipped.append({"id": a["id"], "title": a["title"], "why": FOLDER_WHY})
            pool = [a for a in pool if not needs_folder(a)]
        return sorted(pool, key=lambda a: (a["id"] not in qpos, qpos.get(a["id"], 0),
                                           not (a["reserved_for"] == actor and a["reserved_until"])))

    if not claim:
        return [{k: v for k, v in a.items() if k != 'sort_key'}
                for a in takeable(ready_list(conn, project, unblocks, doer_for, near=near, mine=who_mine, actor=actor))[:limit]]
    _prime_release_commits(conn)
    with tx(conn, claims=True):
        _sweep(conn)
        pool = takeable(ready_list(conn, project, unblocks, doer_for, near=near, mine=who_mine, actor=actor))
        if not pool:
            return []
        _claim_row(conn, pool[0]["id"], actor)
        return [item_show(conn, pool[0]["id"])]


# A chat app session (Claude desktop through maxpm mcp) has no folder and no shell: it cannot edit a
# repository, run a check, commit, or deploy. An item that names files or a check command needs one.
FOLDER_WHY = "needs a folder: it names files or a check command (a coding agent takes it)"


def needs_folder(a):
    touches = a.get("touches") or []
    touches = touches.split(",") if isinstance(touches, str) else touches
    return any(str(t).strip() for t in touches) or bool((a.get("check") or "").strip()) or a.get("kind") in ("deploy", "monitor")


def claim(conn, item_id, actor=None):
    _prime_release_commits(conn, item_id)
    with tx(conn, claims=True):
        _sweep(conn)
        a = annotate(conn)[_item(conn, item_id)["id"]]
        if not ready_for(a, actor):
            why = (f"waits on {', '.join('#' + str(b) for b in a['open_blockers'])}" if a["open_blockers"]
                   else a["blocked_text"] if a["blocked_reason"]
                   else f"conflicts with {', '.join('#' + str(b) for b in a['busy_conflicts'])}, which is in progress "
                        f"(both edit the same files)" if a["busy_conflicts"] and a["status"] == "open"
                   else f"status is {a['status']}" + (f" (held by {a['assignee']})" if a["assignee"] else ""))
            raise RiverError(f"item {item_id} is not ready: {why}. See: maxpm blockers {item_id}")
        why = author_refusal(conn, a, actor)
        if why:
            raise RiverError(f"refused: {why}. Take other work: maxpm go")
        first = still_worked(conn).get(a["id"], actor)
        if first != actor:
            raise RiverError(f"refused: the lease on #{item_id} ran out, but {first} still works on it in its session "
                             f"(a command runs there). Take other work (maxpm go). A person or a manager ends that "
                             f"session first: maxpm stop {first} --reason \"...\"")
        _claim_row(conn, a["id"], actor)
    return item_show(conn, item_id)


def _close(conn, item_id, status, actor, output=None, note=None, force=None):
    with tx(conn):
        it = _item(conn, item_id)
        if it["status"] in CLOSED_STATES:
            raise RiverError(f"item {item_id} is already {it['status']}")
        # Done means everything it waits on is done: a deploy never goes out past an open review or
        # an unfinished item it ships. Other items can be closed anyway with a reason (--force).
        waits = _open_prereqs(conn, it["id"]) if status == "done" else []
        if waits:
            ids = ", ".join(f"#{x}" for x in waits)
            if it["kind"] == "deploy":
                raise RiverError(f"refused: deploy #{item_id} still waits on {ids} (maxpm blockers {item_id}); "
                                 f"finish or drop those first, then deploy")
            if not (force or "").strip():
                raise RiverError(f"refused: #{item_id} still waits on {ids} (maxpm blockers {item_id}). Finish those "
                                 f"first, or close it anyway with a reason: maxpm done {item_id} --force \"<why>\"")
            _event(conn, it["id"], actor, f"done while {ids} still open: {force.strip()}")
        if it["doer"] == "human" and _is_ai(conn, actor):
            if not (note or "").strip():
                raise RiverError(f"#{item_id} is for a person; to mark it {status} as an agent, say why the user "
                                 f"no longer needs to do it: maxpm {'done' if status == 'done' else 'drop'} {item_id} "
                                 f"--note \"<why>\"   (the user is told, and can undo it)")
            _record_takeover(conn, it, "done" if status == "done" else "dropped", note, actor)
        if it["assignee"] and actor and it["assignee"] != actor:
            raise RiverError(f"item {item_id} is held by {it['assignee']}; ask them, or release it first")
        conn.execute("UPDATE items SET status=?, closed_at=?, lease_expires_at=NULL, hold_expires_at=NULL, "
                     "reserved_for=NULL, needs_check=0, output=COALESCE(?, output) WHERE id=?",
                     (status, iso(now()), output, it["id"]))
        _event(conn, it["id"], actor, status + (f": {output}" if output else ""))
        if status == "done" and it["kind"] == "deploy":
            _hook(conn, "deployed", it["target"], it["id"], actor)
        elif status == "done" and it["kind"] == "review":
            _hook(conn, "review-passed", it["target"], it["id"], actor, _review_deploy(conn, it["id"]))
        _goal_notice(conn, it["id"], actor, "finished" if status == "done" else status, output)
        newly, resumed = [], []
        if status in CLOSED_STATES:
            resumed = _resume_holds(conn, it["id"])
            ann = annotate(conn)
            newly = [d for d in ann[it["id"]]["unblocks"] if ann[d]["ready"]]
            # A person answered: an agent item that waited on this person's item starts in a fresh session.
            if it["doer"] == "human":
                for d in newly:
                    _want_fresh(conn, d, f"[#{it['id']} {it['title']}: {status} by {actor or 'a person'}]"
                                + (f" {output}" if output else "") + (f" ({note})" if note else ""))
            # Tell whoever holds an item that waits on this one (design 7.4: "a prerequisite you waited on is done").
            for d in ann[it["id"]]["unblocks"]:
                a = ann[d]
                if a["assignee"] and a["status"] in ("in_progress", "held") and a["assignee"] != actor:
                    left_ = a["open_blockers"]
                    _send(conn, "notice", "maxpm", f"#{it['id']} {it['title']} is {status}"
                          + (f" (output: {output})" if output else "") + f"; your #{d} waits on it. "
                          + (f"Still open before #{d}: " + ", ".join(f"#{x}" for x in left_) if left_
                             else (f"Nothing is left before #{d}; it is in progress again" if d in resumed
                                   else f"Nothing is left before #{d}")),
                          to=a["assignee"], item_id=d)
    res = item_show(conn, item_id)
    res["now_ready"] = newly
    res["resumed"] = resumed
    return res


def synced(conn, item_id, ref=None, actor=None):
    """Record that the tracker issues of a closed item got the result (comment, close). One ref, or all."""
    with tx(conn):
        it = _item(conn, item_id)
        if it["status"] not in CLOSED_STATES:
            raise RiverError(f"#{item_id} is {it['status']}; the write-back comes after maxpm done {item_id}")
        rows = conn.execute("SELECT ref FROM item_refs WHERE item_id=?" + (" AND ref=?" if ref else ""),
                            (it["id"], ref.strip()) if ref else (it["id"],)).fetchall()
        if not rows:
            raise RiverError(f"#{item_id} has no tracker link" + (f" {ref}" if ref else "")
                             + f"; its links: maxpm show {item_id}")
        for r in rows:
            if conn.execute("UPDATE item_refs SET synced_at=? WHERE item_id=? AND ref=? AND synced_at IS NULL",
                            (iso(now()), it["id"], r["ref"])).rowcount:
                _event(conn, it["id"], actor, f"tracker updated: {r['ref']}")
    return item_show(conn, item_id)


def unsynced(conn, actor=None):
    """Closed items with tracker links not yet updated; with actor, only the items that agent closed."""
    rows = conn.execute(
        "SELECT DISTINCT i.id FROM items i JOIN item_refs r ON r.item_id=i.id WHERE r.synced_at IS NULL "
        f"AND i.status IN {CLOSED_STATES}" + (" AND i.assignee=?" if actor else "") + " ORDER BY i.closed_at",
        (actor,) if actor else ()).fetchall()
    ann = annotate(conn) if rows else {}
    return [{"id": r["id"], "title": ann[r["id"]]["title"], "status": ann[r["id"]]["status"],
             "project": ann[r["id"]]["project"], "output": ann[r["id"]]["output"],
             "tracker": setting(conn, "tracker", item_id=r["id"]),
             "refs": [x for x in ann[r["id"]]["refs"] if not x["synced_at"]]} for r in rows]


def _approved_fixes(conn, it, actor):
    """A person approved the fixes a review proposed (an item of kind fixes is done): add each '- ' line
    of its notes as a fix item that the review it blocks waits on."""
    titles = [l[2:].strip() for l in (it["notes"] or "").splitlines() if l.startswith("- ") and l[2:].strip()]
    reviews = [r["item_id"] for r in conn.execute(
        "SELECT d.item_id FROM deps d JOIN items i ON i.id=d.item_id WHERE d.blocked_by=? AND i.kind='review' "
        f"AND i.status IN {OPEN_STATES}", (it["id"],))]
    if not titles or not reviews:
        return []
    project = _project_name(conn, it["project_id"])
    added = [item_add(conn, project, t, priority=it["priority"], doer="ai", actor=actor,
                      context=f"Approved in #{it['id']} ({it['title']}): {it['context']}") for t in titles]
    for r in reviews:
        dep_add(conn, r, [a["id"] for a in added], actor)
    return [{"id": a["id"], "title": a["title"]} for a in added]


def done(conn, item_id, output=None, actor=None, ship_it=False, note=None, synced_=False, force=None):
    """Close an item as done. Refused while an item it waits on is open; force (a reason) closes a
    non-deploy item anyway and records the reason."""
    res = _close(conn, item_id, "done", actor, output, note, force)
    try:  # the cost of the item, from its sessions' transcripts on this computer; never stops a done
        measure_usage(conn, res["id"])
        res["usage"] = item_usage(conn, res["id"])
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        pass
    if res["kind"] == "fixes":
        res["fixes_added"] = _approved_fixes(conn, _item(conn, item_id), actor)
    if synced_ and res["refs"]:
        res = dict(synced(conn, item_id, actor=actor), now_ready=res["now_ready"], resumed=res["resumed"])
    res["tracker"] = setting(conn, "tracker", item_id=item_id) if res["refs"] else ""
    if ship_it:
        shipped = ship(conn, item_id, actor)
        res["shipped_in"], res["ship_release"] = shipped["id"], shipped["release"]
    return res


def ship(conn, item_id, actor=None):
    """Ask for the item to go out: add it to the deploy item of its target that collects ship requests, or
    start one.

    The deploy item waits on everything it ships, sits in the project
    deploy-<target>, and only the target's owner can take it. A release that is cut (release_plan) takes no
    more ship requests: the item joins the next deploy item, which waits until the cut release is done (#1619).
    Not a fix that a review of the cut release waits on: it goes out with that release.
    """
    with tx(conn):
        it = _item(conn, item_id)
        if it["kind"] == "deploy":
            raise RiverError(f"#{item_id} is itself a deploy item")
        if it["status"] == "dropped":
            raise RiverError(f"#{item_id} is dropped; there is nothing to ship")
        proj = conn.execute("SELECT name, target FROM projects WHERE id=?", (it["project_id"],)).fetchone()
        if not proj["target"]:
            raise RiverError(f"project {proj['name']} has no deploy target; set one: "
                             f"maxpm project target {proj['name']} <target>  (maxpm target list)")
        tg = _target(conn, proj["target"])
        # A fix that a review of a cut release waits on is part of that release.
        dep = conn.execute(
            "SELECT d.* FROM items d JOIN deps dr ON dr.item_id=d.id JOIN items r ON r.id=dr.blocked_by "
            "JOIN deps rf ON rf.item_id=r.id WHERE d.kind='deploy' AND d.target=? AND d.status='open' AND d.cut_at "
            f"IS NOT NULL AND r.kind='review' AND r.status IN {OPEN_STATES} AND rf.blocked_by=? ORDER BY d.id LIMIT 1",
            (tg["name"], it["id"])).fetchone()
        if dep is None:
            dep = conn.execute("SELECT * FROM items WHERE kind='deploy' AND target=? AND status='open' AND cut_at IS NULL "
                               "ORDER BY id LIMIT 1", (tg["name"],)).fetchone()
        if dep is None:
            pname = f"deploy-{tg['name']}"
            if not conn.execute("SELECT 1 FROM projects WHERE name=?", (pname,)).fetchone():
                top = conn.execute("SELECT COALESCE(MAX(rank),0) m FROM projects WHERE archived=0").fetchone()["m"]
                conn.execute("INSERT INTO projects(name,rank,notes,target,created_at) VALUES (?,?,?,?,?)",
                             (pname, top + 1, f"Deploys to target {tg['name']}, made by maxpm ship. "
                              f"Only the target owner takes these items.", tg["name"], iso(now())))
                _event(conn, None, actor, f"project {pname} added for target {tg['name']}")
            pid = conn.execute("SELECT id FROM projects WHERE name=?", (pname,)).fetchone()["id"]
            top = conn.execute("SELECT COALESCE(MAX(rank),0) m FROM items WHERE project_id=?", (pid,)).fetchone()["m"]
            cur = conn.execute(
                "INSERT INTO items(project_id,title,notes,priority,rank,doer,context,kind,target,created_at) "
                "VALUES (?,?,?,?,?,'any',?,'deploy',?,?)",
                (pid, f"Deploy {tg['name']}", "Ship every item this waits on; put the release id in the output.",
                 it["priority"], top + 1, tg["description"], tg["name"], iso(now())))
            dep = _item(conn, cur.lastrowid)
            _event(conn, dep["id"], actor, f"deploy item for target {tg['name']} started")
        rv = _release_review(conn, dep, tg, actor)
        if rv is not None and not conn.execute("SELECT 1 FROM deps WHERE item_id=? AND blocked_by=?",
                                               (rv["id"], it["id"])).fetchone():
            _dep_add(conn, rv["id"], it["id"], actor)
        if not conn.execute("SELECT 1 FROM deps WHERE item_id=? AND blocked_by=?", (dep["id"], it["id"])).fetchone():
            _dep_add(conn, dep["id"], it["id"], actor, alert=False)  # the ship request notice below says it
            if it["priority"] < dep["priority"]:
                conn.execute("UPDATE items SET priority=? WHERE id=?", (it["priority"], dep["id"]))
            _event(conn, it["id"], actor, f"ship requested in #{dep['id']} ({tg['name']})")
            if tg["owner"] and tg["owner"] != actor:
                # low: the owner acts on it only when the release starts (the cadence), not at each request
                _send(conn, "notice", actor or "maxpm", f"ship request: #{it['id']} {it['title']} joins deploy "
                      f"#{dep['id']} for {tg['name']}", to=tg["owner"], item_id=dep["id"], level="low")
        _cadence_sync(conn)  # a new release waits (a cut release, the cadence) from its first ship request
    # release: when this release can start, for the worker that asked; in_cut: the item joined a cut release.
    return dict(item_show(conn, dep["id"]), release=dict(release_plan(conn, tg["name"]), in_cut=bool(dep["cut_at"])))


def _release_review(conn, dep, tg, actor, force=False):
    """With review on, the open review item the deploy item waits on; started when there is none.

    A new review waits on the release's items that no finished review of this deploy covered."""
    if not force and setting(conn, "review", item_id=dep["id"]) != "on":
        return None
    rv = conn.execute("SELECT i.* FROM items i JOIN deps d ON d.blocked_by=i.id WHERE d.item_id=? AND i.kind='review' "
                      f"AND i.status IN {OPEN_STATES} ORDER BY i.id LIMIT 1", (dep["id"],)).fetchone()
    if rv is not None:
        return rv
    top = conn.execute("SELECT COALESCE(MAX(rank),0) m FROM items WHERE project_id=?", (dep["project_id"],)).fetchone()["m"]
    cur = conn.execute(
        "INSERT INTO items(project_id,title,notes,priority,rank,doer,context,\"check\",kind,target,created_at) "
        "VALUES (?,?,?,?,?,'any',?,?,'review',?,?)",
        (dep["project_id"], f"Review release {tg['name']}",
         "Review everything this release ships before it deploys. Pass: maxpm review pass <id>; "
         "problems: maxpm review fail <id> \"<fix>\" ... (fix items the review waits on).",
         dep["priority"], top + 0.5, setting(conn, "review_prompt", item_id=dep["id"]),
         setting(conn, "review_cmd", item_id=dep["id"]), tg["name"], iso(now())))
    rv = _item(conn, cur.lastrowid)
    _event(conn, rv["id"], actor, f"review for deploy #{dep['id']} ({tg['name']}) started")
    # Every shipped item that no finished review of this deploy covered (done ones too: Review and deploy
    # can start after the work is finished).
    for r in conn.execute("SELECT i.id FROM items i JOIN deps d ON d.blocked_by=i.id WHERE d.item_id=? "
                          "AND i.kind<>'review' AND i.status<>'dropped' AND i.id NOT IN ("
                          "  SELECT d2.blocked_by FROM deps d2 JOIN items r ON r.id=d2.item_id JOIN deps d3 ON d3.blocked_by=r.id "
                          "  WHERE d3.item_id=? AND r.kind='review' AND r.status='done')",
                          (dep["id"], dep["id"])).fetchall():
        _dep_add(conn, rv["id"], r["id"], actor)
    _dep_add(conn, dep["id"], rv["id"], actor)
    return rv


def _review_item(conn, item_id, actor):
    it = _item(conn, item_id)
    if it["kind"] != "review":
        raise RiverError(f"#{item_id} is not a review item; reviews come with maxpm ship when the setting review is on")
    if it["status"] != "in_progress" or (actor and it["assignee"] != actor):
        raise RiverError(f"#{item_id} is {it['status']}" + (f" (held by {it['assignee']})" if it["assignee"] else "")
                         + f"; take it first: maxpm claim {item_id}")
    return it


# ---------------------------------------------------------------- review steps

def _review_step(conn, step_id):
    r = conn.execute("SELECT s.*, p.name project FROM review_steps s JOIN projects p ON p.id=s.project_id WHERE s.id=?",
                     (int(step_id),)).fetchone()
    if not r:
        raise RiverError(f"no review step {step_id}; list them: maxpm review step list [<project>]")
    return r


def _renumber_steps(conn, project_id):
    ids = [r["id"] for r in conn.execute("SELECT id FROM review_steps WHERE project_id=? ORDER BY pos, id", (project_id,))]
    for n, sid in enumerate(ids, 1):
        conn.execute("UPDATE review_steps SET pos=? WHERE id=?", (n, sid))


def review_steps(conn, project=None):
    """The review steps of one project, or of every project, in order."""
    project = _project(conn, project)["name"] if project else None
    q = ("SELECT s.id, s.pos, s.kind, s.text, p.name project FROM review_steps s JOIN projects p ON p.id=s.project_id "
         + ("WHERE p.name=? " if project else "WHERE p.archived=0 ") + "ORDER BY p.rank, p.id, s.pos, s.id")
    if project:
        _project(conn, project)
    return [dict(r) for r in conn.execute(q, (project,) if project else ())]


def review_step_add(conn, project, text, run=False, at=None, actor=None):
    """Add a review step to a project: a written instruction, or with run a command that must exit 0."""
    p = _project(conn, project)
    text = (text or "").strip()
    if not text:
        raise RiverError("say what the step is: maxpm review step add <project> \"<instruction>\" (or --run \"<command>\")")
    with tx(conn):
        n = conn.execute("SELECT COUNT(*) FROM review_steps WHERE project_id=?", (p["id"],)).fetchone()[0]
        pos = n + 1 if at is None else max(1, min(int(at), n + 1))
        conn.execute("UPDATE review_steps SET pos=pos+1 WHERE project_id=? AND pos>=?", (p["id"], pos))
        cur = conn.execute("INSERT INTO review_steps(project_id,pos,kind,text,created_at) VALUES (?,?,?,?,?)",
                           (p["id"], pos, "run" if run else "do", text, iso(now())))
        _event(conn, None, actor, f"review step {pos} added to {p['name']}: {text}")
    return dict(_review_step(conn, cur.lastrowid))


def review_step_edit(conn, step_id, text=None, run=None, actor=None):
    """Change a step's text, or make it a command (run=True) or a written instruction (run=False)."""
    st = _review_step(conn, step_id)
    if text is None and run is None:
        raise RiverError(f"say what changes: maxpm review step edit {step_id} --text \"...\" and/or --run / --do")
    if text is not None and not text.strip():
        raise RiverError("the step text cannot be empty; to remove it: maxpm review step rm " + str(step_id))
    with tx(conn):
        conn.execute("UPDATE review_steps SET text=?, kind=? WHERE id=?",
                     (text.strip() if text is not None else st["text"],
                      st["kind"] if run is None else ("run" if run else "do"), st["id"]))
        _event(conn, None, actor, f"review step {st['pos']} of {st['project']} changed")
    return dict(_review_step(conn, st["id"]))


def review_step_remove(conn, step_id, actor=None):
    st = _review_step(conn, step_id)
    with tx(conn):
        conn.execute("DELETE FROM review_steps WHERE id=?", (st["id"],))
        _renumber_steps(conn, st["project_id"])
        _event(conn, None, actor, f"review step {st['pos']} removed from {st['project']}: {st['text']}")
    return review_steps(conn, st["project"])


def review_step_move(conn, step_id, to, actor=None):
    """Move a step to position to (1 is first) within its project."""
    st = _review_step(conn, step_id)
    with tx(conn):
        n = conn.execute("SELECT COUNT(*) FROM review_steps WHERE project_id=?", (st["project_id"],)).fetchone()[0]
        to = max(1, min(int(to), n))
        ids = [r["id"] for r in conn.execute("SELECT id FROM review_steps WHERE project_id=? AND id<>? ORDER BY pos, id",
                                             (st["project_id"], st["id"]))]
        ids.insert(to - 1, st["id"])
        for k, sid in enumerate(ids, 1):
            conn.execute("UPDATE review_steps SET pos=? WHERE id=?", (k, sid))
        _event(conn, None, actor, f"review step of {st['project']} moved from {st['pos']} to {to}")
    return review_steps(conn, st["project"])


def release_review_steps(conn, item_id):
    """The review steps a release review follows: of each project whose items it covers, by project rank."""
    rows = conn.execute("SELECT DISTINCT p.id, p.name, p.path, p.rank FROM deps d JOIN items i ON i.id=d.blocked_by "
                        "JOIN projects p ON p.id=i.project_id WHERE d.item_id=? AND i.kind<>'review' "
                        "ORDER BY p.rank, p.id", (int(item_id),)).fetchall()
    out = []
    for p in rows:
        steps = [dict(r) for r in conn.execute("SELECT id, pos, kind, text FROM review_steps WHERE project_id=? "
                                               "ORDER BY pos, id", (p["id"],))]
        if steps:
            out.append({"project": p["name"], "path": p["path"], "steps": steps})
    return out


def review_pass(conn, item_id, output=None, actor=None, cwd=None, runner=None, confirm=None):
    """Accept a release review.

    Every written step of the projects the release ships must be confirmed (confirm: "all", or the
    step ids), and every command step must exit 0 in its project folder; so must the item's review
    command (in cwd). Each step's result goes into the item's history."""
    it = _review_item(conn, item_id, actor)
    groups = release_review_steps(conn, item_id)
    todo = [(g, s) for g in groups for s in g["steps"] if s["kind"] == "do"]
    if isinstance(confirm, str):
        confirm = [confirm]
    confirm = [str(x).strip() for c in (confirm or []) for x in str(c).split(",") if str(x).strip()]
    if not all(x == "all" or x.isdigit() for x in confirm):
        raise RiverError("--confirm takes all, or the ids of the written steps you did (e.g. --confirm 3,5)")
    everything = "all" in confirm
    missing = [(g, s) for g, s in todo if not everything and str(s["id"]) not in confirm]
    if missing:
        raise RiverError("confirm each written review step after you did it:\n"
                         + "\n".join(f"  step {s['id']} ({g['project']} {s['pos']}): {s['text']}" for g, s in missing)
                         + f"\nThen: maxpm review pass {item_id} --confirm all   (or --confirm "
                         + ",".join(str(s["id"]) for _, s in missing) + ")")
    limit = parse_duration(setting(conn, "review_timeout", item_id=it["id"])).total_seconds()

    def renew():
        with tx(conn):
            _touch_agent(conn, actor or it["assignee"])

    run = runner or (lambda c, d: _run_command(c, d, limit, renew))

    def must_pass(cmd, where, what):
        r = run(cmd, where)
        if getattr(r, "timed_out", False):
            tail = "\n".join(((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-15:])
            proj = conn.execute("SELECT name FROM projects WHERE id=?", (it["project_id"],)).fetchone()["name"]
            raise RiverError(f"{what} did not finish within review_timeout ({setting(conn, 'review_timeout', item_id=it['id'])}); "
                             f"MaximizePM stopped it and the processes it started: {cmd}\n{tail}\n"
                             f"Raise the limit (0s: none): maxpm config set review_timeout 8h --project {proj}\n"
                             f"Then run it again: maxpm review pass {item_id}")
        if r.returncode != 0:
            tail = "\n".join(((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-15:])
            raise RiverError(f"{what} failed (exit {r.returncode}): {cmd}\n{tail}\n"
                             f"Fix it, or send the release back: maxpm review fail {item_id} \"<fix>\"")

    results = []
    for g in groups:
        where = g["path"] if g["path"] and os.path.isdir(g["path"]) else cwd
        for s in g["steps"]:
            if s["kind"] == "run":
                must_pass(s["text"], where, f"review step {s['id']} ({g['project']} {s['pos']})")
                results.append({"id": s["id"], "project": g["project"], "kind": "run", "text": s["text"], "result": "exit 0"})
            else:
                results.append({"id": s["id"], "project": g["project"], "kind": "do", "text": s["text"], "result": "confirmed"})
    cmd = (it["check"] or "").strip()
    ran = None
    if cmd:
        must_pass(cmd, cwd, "review command")
        ran = cmd
    with tx(conn):
        for r in results:
            _event(conn, it["id"], actor, f"review step {r['id']} ({r['project']}) {r['result']}: {r['text']}")
    summary = [f"{len(results)} review step(s) followed"] if results else []
    if ran:
        summary.append(f"{ran} exit 0")
    res = done(conn, item_id, output or "; ".join(["review passed"] + summary), actor)
    res["review_cmd"] = ran
    res["review_steps"] = results
    return res


REVIEW_RENEW_EVERY = 60  # seconds between lease renewals while a review command runs; the tests make it short


def _run_command(cmd, cwd, limit, renew=None, env=None):
    """Run a shell command in its own process group and capture its output.

    renew runs every REVIEW_RENEW_EVERY seconds while the command runs, so a long gate keeps the
    reviewer's leases. After limit seconds (0: none) the whole group stops: SIGTERM, then SIGKILL
    (taskkill /T on Windows), so no build is left running without an owner. The result has
    returncode, stdout, stderr, and timed_out."""
    import subprocess
    import time
    win = os.name == "nt"
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if win else {"start_new_session": True}
    p = subprocess.Popen(cmd, shell=True, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         **kw)
    end = time.monotonic() + limit if limit > 0 else None
    out = err = ""
    while True:
        wait = REVIEW_RENEW_EVERY if end is None else max(0.0, min(REVIEW_RENEW_EVERY, end - time.monotonic()))
        try:
            out, err = p.communicate(timeout=wait)
            return subprocess.CompletedProcess(cmd, p.returncode, out, err)
        except subprocess.TimeoutExpired:
            pass
        if end is not None and time.monotonic() >= end:
            break
        if renew:
            try:
                renew()
            except sqlite3.Error:
                pass  # a busy database: the next renewal tries again
    _stop_group(p)
    try:
        out, err = p.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    r = subprocess.CompletedProcess(cmd, p.returncode, out or "", err or "")
    r.timed_out = True
    return r


def _stop_group(p):
    """Stop a process started by _run_command and every process in its group."""
    import signal
    import subprocess
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(p.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            p.wait(timeout=5)
            if sig == signal.SIGTERM:
                # The shell is gone; the rest of the group may still be running: make sure.
                os.killpg(p.pid, signal.SIGKILL)
            return
        except subprocess.TimeoutExpired:
            continue
        except (ProcessLookupError, PermissionError):
            return


def review_fail(conn, item_id, fixes, note=None, project=None, actor=None, ask=False):
    """Send a release back: add fix items that the review waits on, and release the review.

    The fixes go to the named project, else to the project of the first item the review covers.
    When they are done, the review is ready again for any reviewer. With ask, the release waits on the
    user first: one item for a person lists the proposed fixes; done adds the fixes still in its list
    (the person may edit it), drop adds none. Either way the review comes back afterwards."""
    it = _review_item(conn, item_id, actor)
    fixes = [f.strip() for f in fixes if f and f.strip()]
    if not fixes:
        raise RiverError(f"name at least one fix: maxpm review fail {item_id} \"<what to fix>\" --note \"<why>\"")
    if project is None:
        r = conn.execute("SELECT p.name FROM deps d JOIN items i ON i.id=d.blocked_by JOIN projects p ON p.id=i.project_id "
                         "WHERE d.item_id=? AND i.kind<>'review' ORDER BY i.id LIMIT 1", (it["id"],)).fetchone()
        if r is None:
            raise RiverError("the review covers no items; name the project for the fixes: --project <name>")
        project = r["name"]
    project = _project(conn, project)["name"]
    if ask:
        body = "\n".join(f"- {t}" for t in fixes)
        h = item_add(conn, project, f"Release {it['target']} is blocked: approve {len(fixes)} proposed fix(es)",
                     priority=it["priority"], doer="human", actor=actor, notes=body,
                     context=(f"The review of release {it['target']} (#{it['id']}) found problems that block it"
                              + (f": {note}" if note else "") + ". The proposed fixes are the '- ' lines in the notes. "
                              "Keep, edit, or delete lines, then mark this done: MaximizePM adds each remaining line as a fix "
                              "item the release waits on. Drop it to add no fixes; the review then comes back."))
        conn.execute("UPDATE items SET kind='fixes' WHERE id=?", (h["id"],))
        dep_add(conn, it["id"], [h["id"]], actor, mode="release")
        with tx(conn):
            _hook(conn, "review-failed", it["target"], it["id"], actor, _review_deploy(conn, it["id"]))
        res = item_show(conn, it["id"])
        res["asked"] = {"id": h["id"], "title": h["title"], "fixes": fixes}
        return res
    added = [item_add(conn, project, t, priority=it["priority"], doer="ai", actor=actor,
                      context=f"Found in the review of release {it['target']} (#{it['id']})" + (f": {note}" if note else ""))
             for t in fixes]
    dep_add(conn, it["id"], [a["id"] for a in added], actor, mode="release")
    with tx(conn):
        _hook(conn, "review-failed", it["target"], it["id"], actor, _review_deploy(conn, it["id"]))
    res = item_show(conn, it["id"])
    res["fixes"] = [{"id": a["id"], "title": a["title"], "project": project} for a in added]
    return res


def _review_claim(conn, actor, names, any_project=False, brief=None):
    """Claim a ready release review that covers items of these projects (or any, with any_project)."""
    ann = annotate(conn)
    ready = []
    for a in ann.values():
        if a["kind"] != "review" or not a["ready"] or a["reserved_for"] not in (None, actor):
            continue
        if author_refusal(conn, a, actor):  # the author's go after maxpm ship: serve starts another reviewer
            continue
        if any_project or any(ann[b]["project"] in names for b in a["waits_on"]):
            ready.append(a)
    for a in sorted(ready, key=lambda a: a["sort_key"]):
        try:
            return claim(conn, a["id"], actor)
        except RiverError as e:
            if brief is not None:
                brief["claim_refused"] = str(e)
    return None


CHECK_RESULTS = ("done", "partial", "open")
# The last event that says who holds an item: when it is a lease expiry, a session stopped mid-item.
LAST_HOLD_EVENT = ("SELECT at, change FROM events WHERE item_id=? AND (change LIKE 'claimed%' OR change LIKE "
                   "'lease expired%' OR change LIKE 'released%' OR change LIKE 'checked:%') ORDER BY id DESC LIMIT 1")


def check(conn, item_id, result, note=None, actor=None):
    """Record what a check of a suspect item found: done (close it), partial (note what is left), or open."""
    if result not in CHECK_RESULTS:
        raise RiverError(f"check result is one of {', '.join(CHECK_RESULTS)}")
    it = _item(conn, item_id)
    if it["status"] in CLOSED_STATES:
        raise RiverError(f"#{item_id} is already {it['status']}")
    note = (note or "").strip()
    if result == "done":
        if not note:
            raise RiverError(f"say where the work is (commits, files): maxpm check {item_id} done --note \"...\"")
        return done(conn, item_id, f"found done in a check: {note}", actor, note=note, force=f"found done in a check: {note}")
    if result == "partial" and not note:
        raise RiverError(f"say what is done and what is left: maxpm check {item_id} partial --note \"...\"")
    with tx(conn):
        if note:
            conn.execute("UPDATE items SET notes=? WHERE id=?",
                         ((it["notes"] + "\n" if it["notes"] else "") + f"[checked {result}] {note}", it["id"]))
        conn.execute("UPDATE items SET needs_check=0 WHERE id=?", (it["id"],))
        _event(conn, it["id"], actor, f"checked: {result}" + (f": {note}" if note else ""))
    return item_show(conn, item_id)


def _git_log(path, *args):
    """Commits as (short id, UTC time like river's, subject); empty when the folder is not a git repository."""
    import subprocess
    try:
        r = subprocess.run(["git", "-C", path, "log", "--format=%h%x09%cd%x09%s",
                            "--date=format-local:%Y-%m-%dT%H:%M:%SZ", *args],
                           capture_output=True, text=True, timeout=20, env={**os.environ, "TZ": "UTC"})
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    return [tuple(line.split("\t", 2)) for line in r.stdout.splitlines() if line.count("\t") >= 2]


def cleanup(conn, project=None, git=True):
    """Open items that may be done or stale although the queue says otherwise, with the evidence.

    Suspect: a lease ran out without done; a commit in the project folder names the item (#id);
    a person's item whose touched files changed after it was made; ready and never claimed for
    stale_after; a needs-you notice whose title is out of date. A check (maxpm check) after the
    evidence clears the item."""
    ann = annotate(conn)
    t = now()
    project = _project(conn, project)["name"] if project is not None else None
    items = [a for a in ann.values() if a["status"] == "open" and not a["project_archived"]
             and (project is None or a["project"] == project) and a["kind"] == "work"]
    if project is not None:
        _project(conn, project)
    ids = {a["id"] for a in items}
    last_check = {r["item_id"]: r["at"] for r in conn.execute(
        "SELECT item_id, MAX(at) at FROM events WHERE change LIKE 'checked:%' GROUP BY item_id")}
    out = {}

    def flag(a, kind, evidence):
        e = out.setdefault(a["id"], {"id": a["id"], "title": a["title"], "project": a["project"], "doer": a["doer"],
                                     "reasons": [], "evidence": []})
        if kind not in e["reasons"]:
            e["reasons"].append(kind)
        e["evidence"].append(evidence)

    for a in items:
        since = max(a["created_at"], last_check.get(a["id"], ""))
        r = conn.execute(LAST_HOLD_EVENT, (a["id"],)).fetchone()
        if a["needs_check"] or (r and r["change"].startswith("lease expired")):
            flag(a, "lease expired", f"{r['change']} at {r['at'][:16]}" if r and r["change"].startswith("lease")
                 else "a lease ran out without done")
        if a["ready"] and not a["claimed_at"]:
            ready_since = max([since] + [ann[b]["closed_at"] or "" for b in a["waits_on"]])
            took = conn.execute("SELECT 1 FROM events WHERE item_id=? AND change LIKE 'claimed%' AND at > ?",
                                (a["id"], ready_since)).fetchone()
            stale = parse_duration(setting(conn, "stale_after", item_id=a["id"]))
            if not took and parse_iso(ready_since) < t - stale:
                flag(a, "stale", f"ready since {ready_since[:10]} and nobody took it")
    for r in conn.execute("SELECT n.item_id, n.summary FROM needs_you n WHERE n.closed_at IS NULL AND n.kind='item'"):
        a = ann.get(r["item_id"])
        if a and a["id"] in ids and a["title"] not in r["summary"]:
            flag(a, "old notice", f"the needs-you notice says: {r['summary']}")
    if git:
        by_path: dict[str, list] = {}
        for a in items:
            p = _project(conn, a["project"])
            if p["path"] and Path(p["path"]).is_dir():
                by_path.setdefault(p["path"], []).append(a)
        for path, group in by_path.items():
            oldest = min(a["created_at"] for a in group)
            commits = _git_log(path, f"--since={oldest}", "-n", "2000")
            for a in group:
                since = max(a["created_at"], last_check.get(a["id"], ""))
                pat = re.compile(rf"#{a['id']}(?!\d)")
                hits = [c for c in commits if pat.search(c[2]) and c[1] >= since[:19]]
                for c in hits[:3]:
                    flag(a, "commit names it", f"commit {c[0]} {c[1][:10]}: {c[2][:80]}")
                if a["doer"] == "human" and a["touches"]:
                    changed = _git_log(path, f"--since={since}", "-n", "3", "--", *a["touches"])
                    for c in changed:
                        flag(a, "files changed", f"commit {c[0]} {c[1][:10]} changed its files: {c[2][:80]}")
    return sorted(out.values(), key=lambda e: ann[e["id"]]["sort_key"])


def drop(conn, item_id, actor=None, note=None):
    return _close(conn, item_id, "dropped", actor, note=note)


def _is_ai(conn, actor):
    r = conn.execute("SELECT kind FROM agents WHERE name=?", (actor,)).fetchone() if actor else None
    return bool(r) and r["kind"] == "ai"


_TAKEOVER_WORDS = {"took over": "took over", "done": "marked done", "dropped": "dropped"}


def _record_takeover(conn, it, kind, note, actor):
    """An agent took a person's item off their list: record it, tell the people, and close their open asks."""
    t = iso(now())
    conn.execute("UPDATE items SET takeover_by=?, takeover_kind=?, takeover_note=?, takeover_at=?, takeover_seen=0 "
                 "WHERE id=?", (actor, kind, note, t, it["id"]))
    _event(conn, it["id"], actor, f"{_TAKEOVER_WORDS[kind]} a person's item: {note}")
    humans = [r["name"] for r in conn.execute("SELECT name FROM agents WHERE kind='human' ORDER BY name")]
    for h in humans:
        _send(conn, "notice", actor, f"{actor} {_TAKEOVER_WORDS[kind]} #{it['id']} {it['title']} (it was yours): {note}. "
              f"Undo: maxpm undo-takeover {it['id']}", to=h, item_id=it["id"])
    if humans:
        conn.execute(f"UPDATE messages SET state='read', read_at=COALESCE(read_at, ?) WHERE item_id=? "
                     f"AND kind IN ('question','alert') AND state='open' AND to_agent IN ({','.join('?' * len(humans))})",
                     [t, it["id"]] + humans)


def takeover(conn, item_id, note, actor=None):
    """An agent does a person's item itself: it becomes an agent item, claimed by the actor, and the people are told."""
    if not actor:
        raise RiverError("taking over needs an agent name: set MAXPM_AGENT or pass --as <name>")
    if not (note or "").strip():
        raise RiverError(f"say how you will do it without the user: maxpm takeover {item_id} --note \"...\"")
    with tx(conn):
        _sweep(conn)
        it = _item(conn, item_id)
        if it["doer"] != "human":
            raise RiverError(f"#{item_id} is not a person's item (doer {it['doer']}); claim it: maxpm claim {item_id}")
        a = annotate(conn)[it["id"]]
        if not a["ready"]:
            raise RiverError(f"#{item_id} is not ready ({a['status']}"
                             + (f", waits on {', '.join('#' + str(b) for b in a['open_blockers'])}" if a["open_blockers"] else "")
                             + "); take it over when it is")
        conn.execute("UPDATE items SET doer='ai' WHERE id=?", (it["id"],))
        _record_takeover(conn, it, "took over", note.strip(), actor)
        _claim_row(conn, it["id"], actor)
    return item_show(conn, item_id)


def undo_takeover(conn, item_id, actor=None):
    """Give the item back to the people, open, as it was before an agent took it."""
    with tx(conn):
        it = _item(conn, item_id)
        if not it["takeover_by"]:
            raise RiverError(f"#{item_id} was not taken over by an agent")
        conn.execute("UPDATE items SET doer='human', status='open', assignee=NULL, claimed_at=NULL, lease_expires_at=NULL, "
                     "hold_expires_at=NULL, closed_at=NULL, takeover_by=NULL, takeover_kind=NULL, takeover_note=NULL, "
                     "takeover_at=NULL, takeover_seen=0 WHERE id=?", (it["id"],))
        _event(conn, it["id"], actor, f"undo: back to the people (was {_TAKEOVER_WORDS[it['takeover_kind']]} by {it['takeover_by']})")
        if it["takeover_by"] != actor:
            _send(conn, "notice", actor or "maxpm", f"{actor or 'someone'} gave #{it['id']} {it['title']} back to the people; "
                  "stop work on it; it is no longer yours",
                  to=it["takeover_by"], item_id=it["id"])
    return item_show(conn, item_id)


def takeover_seen(conn, item_id, actor=None):
    with tx(conn):
        _item(conn, item_id)
        conn.execute("UPDATE items SET takeover_seen=1 WHERE id=?", (int(item_id),))
    return {"id": int(item_id), "seen": True}


def takeovers(conn, include_seen=False):
    sql = ("SELECT i.id, i.title, i.status, i.takeover_by, i.takeover_kind, i.takeover_note, i.takeover_at, p.name project "
           "FROM items i JOIN projects p ON p.id=i.project_id WHERE i.takeover_by IS NOT NULL")
    if not include_seen:
        sql += " AND i.takeover_seen=0"
    return [dict(r) for r in conn.execute(sql + " ORDER BY i.takeover_at DESC")]


AUTHOR_RELEASE = "released as an author of the release"  # the event that release_authors reads (#1616)


def _named_not_waited(conn, item_id, note):
    """The open items that a note names as #<id> and that the item does not wait on, itself or through
    another open item."""
    named = {int(n) for n in re.findall(r"#(\d+)", note or "")} - {item_id}
    if not named:
        return []
    waits, stack = set(), [item_id]
    while stack:
        for b in _open_prereqs(conn, stack.pop()):
            if b not in waits:
                waits.add(b)
                stack.append(b)
    return [r["id"] for r in conn.execute(
        f"SELECT id FROM items WHERE id IN ({','.join('?' * len(named))}) AND status IN {OPEN_STATES} ORDER BY id",
        sorted(named)) if r["id"] not in waits]


def release(conn, item_id, note=None, actor=None, author=None):
    """Give a claimed item back. author (a release review only): the reviewer wrote commits of the release and
    says which ones. MaximizePM sees the authors of a release up to the pin of that minute (release_commits);
    the pin can move to a commit that holds the reviewer's own work. The event keeps this review from the
    agent from then on (release_authors); maxpm serve starts another reviewer.
    A note that names an open item the released item does not wait on ("waits on #12", and no link) gets a
    hint with the dep command: without the link go gives the item to the next session, which finds the same
    thing and releases it again (#1613). Only a hint: a note can name an item for another reason."""
    with tx(conn):
        it = _item(conn, item_id)
        if author is not None and it["kind"] != "review":
            raise RiverError(f"--author is for a release review, and #{item_id} is a {it['kind']} item; "
                             f"release it with: maxpm release {item_id} --note \"<why>\"")
        if author is not None and not author.strip():
            raise RiverError(f"say which commits of the release you wrote: maxpm release {item_id} --author "
                             f"\"<commit ids, items>\"")
        if it["status"] not in ("in_progress", "held"):
            raise RiverError(f"item {item_id} is {it['status']}; only a claimed item can be released" + (
                f". It is reserved for {it['reserved_for']}; {_unreserve_hint(item_id)}"
                if it["status"] == "open" and it["reserved_for"] else ""))
        if actor and it["assignee"] != actor:
            raise RiverError(f"item {item_id} is held by {it['assignee']}, not {actor}")
        why = f"{AUTHOR_RELEASE}: {author.strip()}" + (f" ({note})" if note else "") if author is not None else (
            "released" + (f": {note}" if note else ""))
        _unhold(conn, it["id"], it["assignee"] if author is not None else actor, why)
        named = [] if author is not None else _named_not_waited(conn, it["id"], note)
    res = item_show(conn, item_id)
    if named:
        ids = ", ".join(f"#{n}" for n in named)
        res["hint"] = (f"the note names {ids}, but #{item_id} does not wait on {'it' if len(named) == 1 else 'them'}, "
                       f"so go gives #{item_id} out again; add the link: "
                       + " ; ".join(f"maxpm dep {item_id} --on {n}" for n in named))
    return res


def reopen(conn, item_id, actor=None):
    with tx(conn):
        it = _item(conn, item_id)
        if it["status"] not in CLOSED_STATES:
            raise RiverError(f"item {item_id} is {it['status']}, not closed")
        conn.execute("UPDATE items SET status='open', closed_at=NULL, assignee=NULL WHERE id=?", (it["id"],))
        _event(conn, it["id"], actor, "reopened")
    return item_show(conn, item_id)


# ---------------------------------------------------------------- reads

# ---------------------------------------------------------------- token usage (#1102)

# Anthropic bills a cache read at 0.1 of the input price, a 1-hour cache write at 2.0 and a 5-minute write at
# 1.25. "eq" is input-token equivalents on that scale; output tokens are shown apart.
EQ_READ, EQ_WRITE_1H, EQ_WRITE_5M = 0.1, 2.0, 1.25
# A line this much older than the first line of a transcript was copied from the session a fork started from.
FORK_SLACK = 30
_SESSION_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def claude_session_from_env(env=None):
    """The Claude Code session id this command runs in (its transcript is <id>.jsonl), or None."""
    sid = (env if env is not None else os.environ).get("CLAUDE_CODE_SESSION_ID", "").strip()
    return sid if _SESSION_ID_RE.match(sid) else None


def record_claude_session(conn, name, sid):
    """Remember that agent `name` ran a command in Claude Code session `sid` (once per pair)."""
    if not (name and sid) or conn.execute("SELECT 1 FROM agent_sessions WHERE agent=? AND session_id=?",
                                          (name, sid)).fetchone():
        return
    if conn.execute("SELECT 1 FROM agents WHERE name=? AND kind='ai'", (name,)).fetchone():
        with tx(conn):
            conn.execute("INSERT OR IGNORE INTO agent_sessions (agent, session_id, first_seen) VALUES (?,?,?)",
                         (name, sid, iso(now())))


def transcripts_root():
    """Where Claude Code keeps session transcripts: <config dir>/projects/<folder>/<session id>.jsonl."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"


def _ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_transcript(path):
    """(requests, fork) of one transcript file. requests: {message id: (time, usage)}, each API request once
    (Claude Code writes one line per content block, with the same usage). A fork (claude --resume <id>
    --fork-session) starts with lines copied from its base, with the base's older times: they are left out,
    and fork is True. Copied lines come before the session's own first request: a line there is copied when
    it is FORK_SLACK older than the first line, or when the first user message is older than the start line
    before it: claude -p writes a queue line when it starts, an interactive session a file-history snapshot
    (its time inside the snapshot). Not the file's birth time: files are written again."""
    reqs, first, fork, dequeued, leading, users = {}, None, False, None, True, 0
    slack = timedelta(seconds=FORK_SLACK)
    with open(path, errors="replace") as f:
        for line in f:
            if not leading and ('"usage"' not in line or '"assistant"' not in line):
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            snap = d.get("snapshot") if d.get("type") == "file-history-snapshot" else None
            if leading and isinstance(snap, dict) and isinstance(snap.get("timestamp"), str) and users == 0:
                dequeued = _ts(snap["timestamp"]) or dequeued
                continue
            t = _ts(d["timestamp"]) if isinstance(d.get("timestamp"), str) else None
            if t is None:
                continue
            if leading:
                first = first or t
                if d.get("type") == "queue-operation":
                    dequeued = t
                    continue
                users += d.get("type") == "user"
                before_queue = dequeued and t < dequeued - timedelta(seconds=1)
                if t < first - slack or (before_queue and (fork or (users == 1 and d.get("type") == "user"))):
                    fork = True
                    continue
            msg = d.get("message") if isinstance(d.get("message"), dict) else {}
            if d.get("type") == "assistant" and isinstance(msg.get("usage"), dict):
                reqs.setdefault(msg.get("id") or d["timestamp"], (t, msg["usage"]))
                leading = False
    return reqs, fork


# The harness of Claude Code runs background tasks for a session: a command started with run_in_background, a
# command that ran into its time limit and was moved to the background, and a background subagent. The session
# ends its turn, and the harness wakes it when the task ends. To maxpm serve such a session looked idle at its
# prompt: no river command, a quiet screen, and no process newer than its last river command (or no process at
# all for a sandboxed session, which cannot run ps). The transcript has the start of each task (the tool result)
# and its end (a task notification with a status, or TaskStop), so serve reads it there (#1611).
_BG_STARTS = (("command", re.compile(r"Command running in background with ID: (\w+)")),
              ("command", re.compile(r"Command [^\n]{0,200}?moved to the background \(ID: (\w+)\)")),
              ("subagent", re.compile(r"Async agent launched successfully.*?agentId: (\w+)", re.S)))
_BG_NOTE = re.compile(r"<task-notification>(.*?)</task-notification>", re.S)
_BG_RIVER_WAIT = re.compile(r"\bmaxpm\b[^\n;|&]*\s(wait|--wait|--watch)\b")
_BG_CACHE = {}  # transcript path -> (size, mtime, tasks): a session that waits writes nothing


def _result_text(content):
    if isinstance(content, str):
        return content
    return "\n".join(x.get("text", "") for x in content or [] if isinstance(x, dict) and x.get("type") == "text")


def _background_tasks(path):
    """The background tasks of one transcript that have a start and no end: [{id, kind (command or subagent),
    at, what, command}]. A start counts only as the first words of a tool result, so text that quotes one (a
    grep of another transcript) starts nothing."""
    started, ended = {}, set()
    with open(path, errors="replace") as f:
        for line in f:
            if "<task-notification>" in line and "<status>" in line:
                for body in _BG_NOTE.findall(line):
                    m = re.search(r"<task-id>(\w+)</task-id>", body)
                    if m and "<status>" in body:
                        ended.add(m.group(1))
            start = '"tool_result"' in line and ("background" in line or "Async agent launched" in line)
            stop = '"TaskStop"' in line or '"KillShell"' in line
            if not (start or stop):
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            msg = d.get("message") if isinstance(d, dict) and isinstance(d.get("message"), dict) else {}
            parts = msg.get("content") if isinstance(msg.get("content"), list) else []
            t = _ts(d["timestamp"]) if isinstance(d.get("timestamp"), str) else None
            for x in parts:
                if not isinstance(x, dict):
                    continue
                if x.get("type") == "tool_use" and x.get("name") in ("TaskStop", "KillShell"):
                    inp = x.get("input") if isinstance(x.get("input"), dict) else {}
                    ended.add(str(inp.get("task_id") or inp.get("shell_id") or ""))
                elif x.get("type") == "tool_result" and d.get("type") == "user" and t is not None:
                    text = _result_text(x.get("content")).lstrip()
                    for kind, rx in _BG_STARTS:
                        m = rx.match(text)
                        if m:
                            started[m.group(1)] = {"id": m.group(1), "kind": kind, "at": t, "use": x.get("tool_use_id"),
                                                   "what": "", "command": ""}
    tasks = [v for k, v in started.items() if k not in ended]
    uses = {v["use"]: v for v in tasks if v["use"]}
    if uses:  # what each one is: the description and the command of its tool call
        with open(path, errors="replace") as f:
            for line in f:
                if '"tool_use"' not in line or not any(u in line for u in uses):
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                msg = d.get("message") if isinstance(d, dict) and isinstance(d.get("message"), dict) else {}
                for x in msg.get("content") if isinstance(msg.get("content"), list) else []:
                    if isinstance(x, dict) and x.get("type") == "tool_use" and x.get("id") in uses:
                        inp = x.get("input") if isinstance(x.get("input"), dict) else {}
                        v = uses[x["id"]]
                        v["command"] = str(inp.get("command") or "")
                        v["what"] = " ".join(str(inp.get("description") or v["command"]).split())[:80]
    return tasks


def background_work(conn, agent, root=None):
    """The background tasks that run now in the agent's Claude Code session (its last one): a command or a
    subagent that the harness started and that has not ended, at most busy_max old. Such a session is not idle:
    the harness wakes it when the task ends, and it reads its news then. Not a river wait (maxpm wait,
    inbox --wait, manage --watch) and not a loop that only renews the lease: these are waits, not work. An
    empty list with no transcript on this computer (another agent CLI) or with busy_max 0s."""
    limit = parse_duration(setting(conn, "busy_max"))
    r = conn.execute("SELECT session_id FROM agent_sessions WHERE agent=? ORDER BY first_seen DESC, rowid DESC LIMIT 1",
                     (agent,)).fetchone()
    if not r or not limit.total_seconds():
        return []
    out, t = [], now()
    for path in sorted(Path(root or transcripts_root()).glob(f"*/{r['session_id']}.jsonl")):
        try:
            st = path.stat()
            got = _BG_CACHE.get(str(path))
            if not got or got[:2] != (st.st_size, st.st_mtime_ns):
                got = _BG_CACHE[str(path)] = (st.st_size, st.st_mtime_ns, _background_tasks(path))
        except OSError:
            continue
        for task in got[2]:
            cmd = task["command"]
            if t - task["at"] >= limit or _BG_RIVER_WAIT.search(cmd) or ("maxpm" in cmd and "heartbeat" in cmd
                                                                          and "sleep" in cmd):
                continue
            out.append({"id": task["id"], "kind": task["kind"], "what": task["what"], "at": iso(task["at"])})
    return out


def _session_requests(sid, root):
    """All requests of a Claude Code session, its subagents' too, and whether it started as a fork; None
    when no transcript of it is on this computer."""
    mains = sorted(Path(root).glob(f"*/{sid}.jsonl"))
    if not mains:
        return None
    reqs, fork = {}, False
    for p in mains:
        r, f = read_transcript(p)
        reqs.update(r)
        fork = fork or f
        for sub in sorted(p.parent.glob(f"{sid}/subagents/*.jsonl")):
            reqs.update(read_transcript(sub)[0])
    return reqs, fork


def usage_eq(u):
    """Input-token equivalents of a usage row (or a transcript usage dict)."""
    if "cache_read" in u:
        return u["input"] + EQ_READ * u["cache_read"] + EQ_WRITE_1H * u["write_1h"] + EQ_WRITE_5M * u["write_5m"]
    cc = u.get("cache_creation") or {}
    w1, w5 = cc.get("ephemeral_1h_input_tokens"), cc.get("ephemeral_5m_input_tokens")
    if w1 is None and w5 is None:
        w1, w5 = u.get("cache_creation_input_tokens") or 0, 0
    return ((u.get("input_tokens") or 0) + EQ_READ * (u.get("cache_read_input_tokens") or 0)
            + EQ_WRITE_1H * (w1 or 0) + EQ_WRITE_5M * (w5 or 0))


def measure_usage(conn, item_id, root=None):
    """Read the token usage of the sessions that held the item from their Claude Code transcripts, and store
    it (item_usage; a new measurement replaces the old one). A request is charged to the item when the
    session's agent held it at that time; when the agent held several items, to the one it took last.
    Returns the stored rows."""
    root = Path(root) if root else transcripts_root()
    end_now = now()
    holds = lambda agent: [(parse_iso(h["started_at"]), parse_iso(h["ended_at"]) if h["ended_at"] else end_now,
                            h["id"], h["item_id"]) for h in conn.execute(
        "SELECT * FROM holds WHERE agent=? ORDER BY id", (agent,))]
    rows = []
    for (agent,) in conn.execute("SELECT DISTINCT agent FROM holds WHERE item_id=?", (item_id,)).fetchall():
        mine = holds(agent)
        for (sid,) in conn.execute("SELECT session_id FROM agent_sessions WHERE agent=? ORDER BY first_seen",
                                   (agent,)).fetchall():
            got = _session_requests(sid, root)
            if not got:
                continue
            reqs, fork = got
            row = {"item_id": item_id, "session_id": sid, "agent": agent, "started": "fork" if fork else "fresh",
                   "turns": 0, "input": 0, "cache_read": 0, "write_1h": 0, "write_5m": 0, "output": 0,
                   "peak_context": 0}
            for t, u in reqs.values():
                active = [h for h in mine if h[0] <= t < h[1]]
                if not active or max(active, key=lambda h: (h[0], h[2]))[3] != item_id:
                    continue
                cc = u.get("cache_creation") or {}
                w1, w5 = cc.get("ephemeral_1h_input_tokens"), cc.get("ephemeral_5m_input_tokens")
                if w1 is None and w5 is None:
                    w1, w5 = u.get("cache_creation_input_tokens") or 0, 0
                inp, read = u.get("input_tokens") or 0, u.get("cache_read_input_tokens") or 0
                row["turns"] += 1
                row["input"] += inp
                row["cache_read"] += read
                row["write_1h"] += w1 or 0
                row["write_5m"] += w5 or 0
                row["output"] += u.get("output_tokens") or 0
                row["peak_context"] = max(row["peak_context"], inp + read + (w1 or 0) + (w5 or 0))
            if row["turns"]:
                rows.append(row)
    with tx(conn):
        conn.execute("DELETE FROM item_usage WHERE item_id=?", (item_id,))
        for r in rows:
            conn.execute("INSERT INTO item_usage (item_id, session_id, agent, started, turns, input, cache_read, "
                         "write_1h, write_5m, output, peak_context, measured_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (r["item_id"], r["session_id"], r["agent"], r["started"], r["turns"], r["input"],
                          r["cache_read"], r["write_1h"], r["write_5m"], r["output"], r["peak_context"], iso(now())))
    return rows


def item_usage(conn, item_id):
    """The stored usage of one item, summed over its sessions; None when it was not measured."""
    rows = [dict(r) for r in conn.execute("SELECT * FROM item_usage WHERE item_id=? ORDER BY session_id",
                                          (item_id,))]
    if not rows:
        return None
    total = {k: sum(r[k] for r in rows) for k in ("turns", "input", "cache_read", "write_1h", "write_5m", "output")}
    starts = sorted({r["started"] for r in rows})
    return {**total, "eq": round(usage_eq(total)), "sessions": len(rows), "started": "+".join(starts),
            "forks": sum(r["started"] == "fork" for r in rows), "peak_context": max(r["peak_context"] for r in rows),
            "rows": rows}


def usage_report(conn, project=None, since="7d", measure=None, root=None):
    """maxpm usage: the cost of each done item (measured on done), newest first, and the medians by how
    its sessions started (fresh, fork, or both), so forks and fresh starts can be compared. measure: an item
    id to read again from the transcripts first."""
    if measure is not None:
        _item(conn, measure)
        measure_usage(conn, measure, root)
    q = ("SELECT i.id, i.title, i.closed_at, p.name project FROM items i JOIN projects p ON p.id=i.project_id "
         "WHERE i.id IN (SELECT item_id FROM item_usage)")
    args = []
    if project:
        q += " AND p.id=?"
        args.append(_project(conn, project)["id"])
    if since and since != "all":
        q += " AND COALESCE(i.closed_at, '') >= ?"
        args.append(iso(now() - parse_duration(since)))
    items = []
    for r in conn.execute(q + " ORDER BY i.closed_at DESC, i.id DESC", args):
        u = item_usage(conn, r["id"])
        u.pop("rows")
        items.append({**dict(r), **u})
    groups = []
    for g in ("fresh", "fork", "fork+fresh"):
        rr = [x for x in items if x["started"] == g]
        if rr:
            med = lambda k: statistics.median(x[k] for x in rr)
            groups.append({"started": g, "items": len(rr), "median_eq": round(med("eq")),
                           "median_turns": med("turns"), "median_output": round(med("output")),
                           "eq_per_turn": round(sum(x["eq"] for x in rr) / max(1, sum(x["turns"] for x in rr)))})
    return {"items": items, "groups": groups, "since": since or "all"}


# ---------------------------------------------------------------- goal bases (#1104)

def _k_tokens(text):
    """80k -> 80000; 1.5M -> 1500000; a plain number as is."""
    m = re.match(r"^\s*([0-9.]+)\s*([kKmM]?)\s*$", str(text))
    if not m:
        raise RiverError(f"{text!r}: a number of tokens, such as 80k")
    return int(float(m.group(1)) * {"": 1, "k": 1000, "m": 1_000_000}[m.group(2).lower()])


def context_brief(conn, goal):
    """What a context session reads for a goal: the handoff, the open agent items with their notes and the
    files they touch, and the limit base_max."""
    g = _goal(conn, goal)
    ann = annotate(conn)
    items = sorted((ann[r["item_id"]] for r in conn.execute("SELECT item_id FROM item_goals WHERE goal_id=?", (g["id"],))
                    if r["item_id"] in ann), key=lambda a: a["sort_key"])
    live = [a for a in items if a["status"] in OPEN_STATES and a["doer"] != "human" and not a["no_goal_context"]]
    return {"goal": g["name"], "project": _project_name(conn, g["project_id"]), "outcome": g["outcome"],
            "done_when": g["done_when"], "handoff": _handoff(conn, g["id"]), "parent_handoff": parent_handoff(conn, g),
            "items": [{"id": a["id"], "title": a["title"], "ready": a["ready"], "notes": a["notes"],
                       "context": a["context"], "touches": touches_list(a["touches"])} for a in live[:12]],
            "more": max(0, len(live) - 12), "base_max": setting(conn, "base_max", project_id=g["project_id"])}


def _base_view(conn, g):
    r = conn.execute("SELECT * FROM goal_bases WHERE goal_id=?", (g["id"],)).fetchone()
    if not r:
        return None
    warm = parse_duration(setting(conn, "base_warm", project_id=g["project_id"]))
    until = parse_iso(r["used_at"]) + warm
    return {**dict(r), "warm": now() < until, "warm_until": iso(until)}


def goal_base(conn, name, actor=None, ready=False, clear=False, session=None, model=None, cwd=None, root=None):
    """A goal's base: show it; --ready records the context session this command runs in as the base (its
    session id, model, folder, context size); --clear drops it."""
    g = _goal(conn, name)
    warn = None
    if ready:
        if not session:
            raise RiverError("run maxpm goal base --ready inside the Claude Code context session: it records that "
                             "session (CLAUDE_CODE_SESSION_ID) as the base")
        got = _session_requests(session, Path(root) if root else transcripts_root())
        reqs = sorted(got[0].values(), key=lambda r: r[0]) if got else []
        last = reqs[-1][1] if reqs else {}
        tokens = sum(last.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens",
                                                 "cache_creation_input_tokens")) or None
        limit = _k_tokens(setting(conn, "base_max", project_id=g["project_id"]))
        if tokens and tokens > limit:
            warn = (f"the base has {tokens} tokens, above base_max {limit}: each fork reads all of it. Next time "
                    f"read less (the handoff and the files the next items touch)")
        model = model or (agent_model(conn, actor) if actor else None)
        with tx(conn):
            conn.execute("INSERT OR REPLACE INTO goal_bases (goal_id, session_id, model, path, tokens, by_agent, "
                         "ready_at, used_at, forks) VALUES (?,?,?,?,?,?,?,?,0)",
                         (g["id"], session, model, os.path.realpath(cwd or os.getcwd()), tokens, actor,
                          iso(now()), iso(now())))
            _event(conn, None, actor, f"base of goal {name} ready: session {session[:8]}, model {model or '?'}, "
                                      f"{tokens or '?'} tokens")
    elif clear:
        with tx(conn):
            conn.execute("DELETE FROM goal_bases WHERE goal_id=?", (g["id"],))
            _event(conn, None, actor, f"base of goal {name} dropped")
    return {"goal": name, "base": _base_view(conn, g), "warning": warn, "ready": ready, "cleared": clear}


def item_goal_context(conn, item_id, needed, actor=None):
    """maxpm add/edit --no-goal-context (needed False): the item needs no goal context, so a session for it
    starts fresh, never as a fork of its goal's base; --goal-context (True) undoes it."""
    it = _item(conn, item_id)
    with tx(conn):
        conn.execute("UPDATE items SET no_goal_context=? WHERE id=?", (0 if needed else 1, it["id"]))
        _event(conn, it["id"], actor, "needs goal context" if needed else "needs no goal context: starts fresh")
    return item_show(conn, it["id"])


def fork_base(conn, item, path, model, platform):
    """The base a new session for this item starts from (claude --resume <base> --fork-session), or None: a
    Claude Code session, an item of an open goal that needs goal context, a base in the same folder, of the
    same model, that a session used less than base_warm ago."""
    if platform != "claude-code" or not item.get("goals") or item.get("no_goal_context"):
        return None
    for name in item["goals"]:
        g = conn.execute("SELECT * FROM goals WHERE name=? AND status='open'", (name,)).fetchone()
        b = _base_view(conn, g) if g else None
        if not b or not b["warm"] or b["path"] != os.path.realpath(path):
            continue
        want = model or setting(conn, "default_model", project_id=g["project_id"]) or None
        if (b["model"] or None) == want:
            return {"goal": name, "session_id": b["session_id"], "tokens": b["tokens"]}
    return None


def base_used(conn, session_id):
    """A fork started from this base: it read the base from the cache, so the base stays warm longer."""
    with tx(conn):
        conn.execute("UPDATE goal_bases SET used_at=?, forks=forks+1 WHERE session_id=?", (iso(now()), session_id))


CONTEXT_RETRY = timedelta(minutes=30)


def context_wanted(conn):
    """Goals that want a context session now (goal_context on): two or more ready agent items that need goal
    context, no warm base, and no context session at work for it. [(goal, project, path)]"""
    ann = annotate(conn)
    out = []
    for g in conn.execute("SELECT g.*, p.path, p.name project FROM goals g JOIN projects p ON p.id=g.project_id "
                          "WHERE g.status='open' AND p.archived=0 ORDER BY p.rank, g.rank"):
        if setting(conn, "goal_context", project_id=g["project_id"]) != "on" or not g["path"]:
            continue
        ready = [a for a in ann.values() if g["name"] in a["goals"] and a["ready"] and a["doer"] != "human"
                 and not a["no_goal_context"] and a["kind"] not in ("deploy", "review", "monitor")]
        b = _base_view(conn, g)
        if len(ready) < 2 or (b and b["warm"]):
            continue
        busy = [r for r in conn.execute("SELECT * FROM agents WHERE role='context' AND note=?",
                                        (f"role: context for goal {g['name']}",)) if _agent_state(conn, r) == "active"]
        # One start per CONTEXT_RETRY: a session that has not run maxpm go yet, or ended without a base.
        tried = conn.execute("SELECT 1 FROM events WHERE item_id IS NULL AND change LIKE ? AND at >= ?",
                             (f"context session started for goal {g['name']} %", iso(now() - CONTEXT_RETRY))).fetchone()
        if busy or tried:
            continue
        out.append({"goal": g["name"], "project": g["project"], "path": g["path"],
                    "model": next((a["model"] for a in ready if a.get("model")), None)})
    return out


def item_show(conn, item_id, ann=None):
    ann = ann or annotate(conn)
    iid = int(item_id)
    if iid not in ann:
        raise RiverError(f"no item {item_id}")
    a = {k: v for k, v in ann[iid].items() if k != "sort_key"}
    label = lambda x: "ready" if ann[x]["ready"] else ann[x]["status"]
    a["waits_on_detail"] = [{"id": b, "title": ann[b]["title"], "status": label(b),
                             "kind": "feeds" if b in a["fed_by"] else "blocks"} for b in a["waits_on"]]
    a["unblocks_detail"] = [{"id": d, "title": ann[d]["title"], "status": label(d)} for d in a["unblocks"]]
    a["fed_by_detail"] = [{"id": d, "title": ann[d]["title"], "status": ann[d]["status"], "output": ann[d]["output"]}
                          for d in a["fed_by"]]
    a["conflicts_detail"] = [{"id": d, "title": ann[d]["title"], "status": ann[d]["status"],
                              "assignee": ann[d]["assignee"]} for d in a["conflicts"]]
    a["events"] = [dict(r) for r in conn.execute(
        "SELECT at, actor, change FROM events WHERE item_id=? ORDER BY id DESC LIMIT 50", (iid,))]
    if a["kind"] == "review":
        a["reviews"] = [d for d in a["waits_on_detail"]]
        a["review_steps"] = release_review_steps(conn, iid)
    if a["kind"] == "deploy":
        r = conn.execute("SELECT owner FROM targets WHERE name=?", (a["target"],)).fetchone()
        a["target_owner"] = r["owner"] if r else None
        a["ships"] = a["waits_on_detail"]
    a["found_here"] = [{"id": r["id"], "title": ann[r["id"]]["title"], "status": label(r["id"])} for r in conn.execute(
        "SELECT id FROM items WHERE found_during=? ORDER BY id", (iid,))]
    a["message_count"] = conn.execute("SELECT COUNT(*) FROM messages WHERE item_id=?", (iid,)).fetchone()[0]
    a["usage"] = item_usage(conn, iid)
    return a


def item_list(conn, project=None, status=None, include_closed=False, goal=None, ref=None):
    """Items in order. With ref: the items linked to that tracker issue, closed ones too (an import checks it)."""
    ann = annotate(conn)
    rows = [a for a in ann.values() if not a["project_archived"]]
    if ref is not None:
        rows = [a for a in rows if any(r["ref"] == ref.strip() for r in a["refs"])]
        include_closed = True
    if goal is not None:
        _goal(conn, goal)
        rows = [a for a in rows if goal in a["goals"]]
    if project is not None:
        project = _project(conn, project)["name"]
        rows = [a for a in rows if a["project"] == project]
    if status is not None:
        rows = [a for a in rows if a["status"] == status]
    elif not include_closed:
        rows = [a for a in rows if a["status"] in OPEN_STATES]
    rows.sort(key=lambda a: (a["status"] in CLOSED_STATES, not a["ready"], a["sort_key"]))
    return [{k: v for k, v in a.items() if k != "sort_key"} for a in rows]


def blockers(conn, item_id):
    """Tree of open prerequisites with status and holder."""
    ann = annotate(conn)
    root = _item(conn, item_id)["id"]

    def node(i, trail):
        a = ann[i]
        left = None
        if a["lease_expires_at"]:
            left = int((parse_iso(a["lease_expires_at"]) - now()).total_seconds())
        return {
            "id": i, "title": a["title"], "status": a["status"], "assignee": a["assignee"],
            "ready": a["ready"], "blocked_reason": a["blocked_reason"], "blocked_text": a["blocked_text"], "lease_seconds_left": left,
            "busy_conflicts": [{"id": c, "title": ann[c]["title"], "assignee": ann[c]["assignee"]}
                               for c in a["busy_conflicts"]],
            "children": [node(b, trail | {i}) for b in a["waits_on"]
                         if ann[b]["status"] in OPEN_STATES and b not in trail],
        }

    return node(root, frozenset())


# ---------------------------------------------------------------- messages

def _message(conn, msg_id):
    r = conn.execute("SELECT * FROM messages WHERE id=?", (int(msg_id),)).fetchone()
    if not r:
        raise RiverError(f"no message {msg_id}")
    return r


def _send(conn, kind, sender, body, to=None, item_id=None, reply_to=None, level=None, blocked=None):
    """Insert one message inside the caller's transaction and return its id. level: low when it is not
    DEFAULT_LEVEL. blocked: one of BLOCKED_SIGNS when the message's item stands still until the answer."""
    t = iso(now())
    cur = conn.execute(
        "INSERT INTO messages(kind,from_agent,to_agent,item_id,reply_to,thread_id,body,created_at,level,blocked) "
        "VALUES (?,?,?,?,?,0,?,?,?,?)", (kind, sender, to, item_id, reply_to, body, t, level, blocked))
    mid = cur.lastrowid
    thread = _message(conn, reply_to)["thread_id"] if reply_to else mid
    conn.execute("UPDATE messages SET thread_id=? WHERE id=?", (thread, mid))
    return mid


# A message reaches its to_agent. A message with no to_agent but an item
# reaches whoever holds that item when they read, so a question on an
# unclaimed item waits for the next holder.
_TO_ME = ("(m.to_agent=? OR (m.to_agent IS NULL AND m.item_id IN "
          "(SELECT id FROM items WHERE assignee=? AND status IN ('in_progress','held'))))")


def _msg_dict(r):
    m = dict(r)
    m["unread"] = m["read_at"] is None
    m["level_set"] = bool(m.get("level"))  # the sender gave it
    m["level"] = m.get("level") or DEFAULT_LEVEL
    return m


def _is_manager(conn, name):
    return bool(name and conn.execute("SELECT 1 FROM agents WHERE name=? AND role='manager'", (name,)).fetchone())


def _is_person(conn, name):
    return bool(name and conn.execute("SELECT 1 FROM agents WHERE name=? AND kind='human'", (name,)).fetchone())


def _item_stands(conn, sender, item_id):
    """Inside a tx, at the send. True when MaximizePM knows that the item of a message from sender stands
    still: the sender blocked it or released it in the last BLOCKED_WINDOW. With no item on the message: an
    item that the sender released or blocked in that time."""
    since = iso(now() - BLOCKED_WINDOW)
    about = "" if item_id is None else " AND item_id=?"
    args = () if item_id is None else (item_id,)
    if conn.execute(f"SELECT 1 FROM events WHERE actor=? AND at>=? AND change LIKE 'released%'{about} LIMIT 1",
                    (sender, since) + args).fetchone():
        return True
    return bool(conn.execute(
        "SELECT 1 FROM items WHERE blocked_reason IS NOT NULL AND blocked_set_by=? AND status IN ('open','in_progress','held') "
        "AND " + ("blocked_at>=?" if item_id is None else "id=?") + " LIMIT 1",
        (sender, since if item_id is None else item_id)).fetchone())


def _raise_blocked(conn, sender, sign, item_id=None):
    """Inside a tx. A sign came after the send that the sender's item stands still until the manager answers
    (sign waits: it ran maxpm inbox --wait; sign item: it released or blocked item_id). Its alerts and questions
    to a manager of the last BLOCKED_WINDOW that nobody read, about that item or about no item, become blocked:
    the manager's watch returns for them at once (#1608). Returns how many."""
    if not sender or _is_manager(conn, sender):
        return 0
    return conn.execute(
        "UPDATE messages SET blocked=? WHERE from_agent=? AND blocked IS NULL AND read_at IS NULL "
        "AND kind IN ('alert','question') AND created_at>=? AND (? IS NULL OR item_id IS NULL OR item_id=?) "
        "AND to_agent IN (SELECT name FROM agents WHERE role='manager')",
        (sign, sender, iso(now() - BLOCKED_WINDOW), item_id, item_id)).rowcount


def send(conn, kind, body, to=None, item=None, reply_to=None, actor=None, level=None, blocked=False):
    """Send an alert, question, or note to an agent, to the holder of an item, or as a reply. For a message to
    the manager, level (high, low) says how long it waits before it wakes the manager's watch, and blocked says
    that the sender's item cannot move until the answer: the watch returns for it at once."""
    if not actor:
        raise RiverError("sending needs an agent name: set MAXPM_AGENT or pass --as <name>")
    if kind not in SEND_KINDS:
        raise RiverError(f"send kind is one of {', '.join(SEND_KINDS)}; to answer a question: maxpm answer <message-id> \"...\"")
    if level is not None and level not in MESSAGE_LEVELS:
        raise RiverError(f"the level is one of {', '.join(MESSAGE_LEVELS)}; for a message that cannot wait, "
                         f"leave it out and add --blocked")
    if not body or not body.strip():
        raise RiverError("a message needs text")
    with tx(conn):
        if reply_to is not None:
            parent = _message(conn, reply_to)
            if to is None:
                to = parent["from_agent"] if parent["from_agent"] != actor else parent["to_agent"]
            if item is None:
                item = parent["item_id"]
        if item is not None:
            it = _item(conn, item)
            item = it["id"]
            if to is None and it["status"] in ("in_progress", "held"):
                to = it["assignee"]
        if to is None and item is None:
            raise RiverError("say who gets it: --to <agent>, --item <id> (its holder), or --reply <message-id>")
        if to is not None:
            _agent(conn, to)
        manager = _is_manager(conn, to)
        # What MaximizePM knows without the flag: the sender released or blocked the item just before.
        sign = "sender" if blocked else ("item" if manager and kind in ("alert", "question")
                                         and _item_stands(conn, actor, item) else None)
        mid = _send(conn, kind, actor, body.strip(), to=to, item_id=item, reply_to=reply_to,
                    level=level if level != DEFAULT_LEVEL else None, blocked=sign)
        if reply_to is not None:
            # Replying to a message means the sender read it.
            if conn.execute(f"SELECT 1 FROM messages m WHERE m.id=? AND {_TO_ME}", (reply_to, actor, actor)).fetchone():
                _mark_read(conn, [reply_to])
        if item is not None:
            _event(conn, item, actor, f"{kind} #{mid} to {to or 'the next holder'}")
    got = conn.execute("SELECT role FROM agents WHERE name=? AND kind='ai' AND platform IS NOT NULL",
                       (to,)).fetchone() if to is not None else None
    if got:
        # A message into the manager's session wakes it, so only a blocked one or a person's goes there; its
        # watch brings the others when their wait ends (manage_wait_high, manage_wait_low).
        st = (f"waits: the manager's watch brings a {level or DEFAULT_LEVEL} message after its wait"
              if got["role"] == "manager" and not sign and not _is_person(conn, actor)
              else deliver_native(conn, to, _native_text(kind, actor, body.strip())))
        with tx(conn):
            conn.execute("UPDATE messages SET native_status=? WHERE id=?", (st, mid))
    res = message_show(conn, mid)
    if manager and not res["blocked"] and not _is_person(conn, actor):
        # For the sender: how long the manager's watch lets it wait (the command says how to shorten that).
        res["manager_wait"] = setting(conn, "manage_wait_" + res["level"], agent=to)
    return res


def answer(conn, msg_id, body, actor=None):
    """Answer a question: the answer goes to the asker and the question closes."""
    if not actor:
        raise RiverError("answering needs an agent name: set MAXPM_AGENT or pass --as <name>")
    if not body or not body.strip():
        raise RiverError("an answer needs text")
    with tx(conn):
        q = _message(conn, msg_id)
        if q["kind"] != "question":
            raise RiverError(f"message {msg_id} is {'an' if q['kind'][0] in 'aeiou' else 'a'} {q['kind']}, not a question; "
                             f"reply with: maxpm send note --reply {msg_id} \"...\"")
        if q["state"] != "open":
            raise RiverError(f"question {msg_id} is already {q['state']}; see: maxpm thread {msg_id}")
        if q["from_agent"] == actor:
            raise RiverError(f"you asked question {msg_id}; add to it with: maxpm send note --reply {msg_id} \"...\"")
        t = iso(now())
        mid = _send(conn, "answer", actor, body.strip(), to=q["from_agent"], item_id=q["item_id"], reply_to=q["id"])
        conn.execute("UPDATE messages SET state='answered', closed_at=?, read_at=COALESCE(read_at, ?), "
                     "to_agent=COALESCE(to_agent, ?) WHERE id=?", (t, t, actor, q["id"]))
        if q["item_id"] is not None:
            _event(conn, q["item_id"], actor, f"answered question #{q['id']}")
            # The asker ended (it filed its question and stopped): a fresh session takes the item, with the answer.
            if not conn.execute("SELECT 1 FROM agents WHERE name=?", (q["from_agent"],)).fetchone():
                _want_fresh(conn, q["item_id"], f"[question #{q['id']} from {q['from_agent']}] {q['body']}\n"
                            f"[answer from {actor}] {body.strip()}")
    return message_show(conn, mid)


def _mark_read(conn, ids):
    t = iso(now())
    for i in ids:
        # Questions and offers stay open until someone answers, accepts, or declines them.
        conn.execute("UPDATE messages SET read_at=COALESCE(read_at, ?), "
                     "state=CASE WHEN kind IN ('question','offer') THEN state ELSE 'read' END WHERE id=?", (t, i))


def inbox(conn, actor, include_read=False, mark_read=True):
    """Messages for the actor: unread ones, and questions still waiting for an answer."""
    if not actor:
        raise RiverError("the inbox needs an agent name: set MAXPM_AGENT or pass --as <name>")
    _agent(conn, actor)
    where = f"{_TO_ME} AND m.from_agent<>?"
    if not include_read:
        where += " AND (m.read_at IS NULL OR (m.kind IN ('question','offer') AND m.state='open'))"
    with tx(conn):
        rows = [_msg_dict(r) for r in conn.execute(
            f"SELECT m.*, i.title item_title FROM messages m LEFT JOIN items i ON i.id=m.item_id "
            f"WHERE {where} ORDER BY m.id", (actor, actor, actor))]
        if mark_read:
            _mark_read(conn, [m["id"] for m in rows if m["unread"]])
    return rows


INBOX_WAIT = "25m"  # maxpm inbox --wait returns after this with nothing new, below a 30m background-command limit


def inbox_wait(conn, actor, timeout=None, sleep=None, poll=3.0):
    """maxpm inbox --wait: block until a message to the actor is unread (or a stop request comes), at most
    timeout. Returns the new messages (marked read), so a background command alerts its session at once."""
    import time
    sleep = sleep or time.sleep
    if not actor:
        raise RiverError("the inbox needs an agent name: set MAXPM_AGENT or pass --as <name>")
    _agent(conn, actor)
    if active_manager(conn) == actor:  # a second watcher woke the manager twice for each message (#1312)
        raise RiverError(f"{actor} is the active manager, and maxpm manage --watch is its only watcher: it returns "
                         f"for messages too. Run maxpm --as {actor} manage --watch instead of inbox --wait")
    deadline = now() + parse_duration(timeout or INBOX_WAIT)
    raised = False
    while True:
        if not raised:
            # A sender that now waits for messages stands still until the manager answers (#1608). A locked
            # queue never ends a wait (#1615): the next poll tries again.
            try:
                with tx(conn, tries=1):
                    _raise_blocked(conn, actor, "waits")
                raised = True
            except RiverLocked:
                pass
        st = stop_request(conn, actor)
        if st:
            return {"result": "stop", "agent": actor, "stop": st, "messages": []}
        # Unread only: an open question already read stays in the inbox and would wake it at once, every time.
        if unread(conn, actor)["unread"]:
            try:
                rows = inbox(conn, actor)
            except RiverLocked:  # nothing is marked read: the next poll brings the messages
                rows = None
            if rows is not None:
                return {"result": "messages", "agent": actor, "messages": [m for m in rows if m["unread"]],
                        "still_open": sum(1 for m in rows if not m["unread"])}
        if now() >= deadline:
            return {"result": "timeout", "agent": actor, "messages": [], "waited": timeout or INBOX_WAIT}
        sleep(poll)


def has_native(conn, agent):
    """True when river delivers messages into the agent's running session itself (native_message)."""
    r = conn.execute("SELECT platform, native_address FROM agents WHERE name=?", (agent,)).fetchone()
    return bool(r and r["platform"] and r["native_address"]
                and any(e[0] == r["platform"] for e in parse_native(setting(conn, "native_message"))))


def unread(conn, actor):
    """Counts for the line every command prints: unread messages and open questions to the actor."""
    if not actor:
        return {"unread": 0, "alerts": 0, "questions": 0, "questions_waiting": 0, "nudge_after": ""}
    nudge = setting(conn, "question_nudge_after", agent=actor)
    old = iso(now() - parse_duration(nudge))
    r = conn.execute(
        f"SELECT SUM(m.read_at IS NULL) unread, SUM(m.read_at IS NULL AND m.kind='alert') alerts, "
        f"SUM(m.kind='question' AND m.state='open') questions, "
        f"SUM(m.kind='question' AND m.state='open' AND m.created_at <= ?) waiting "
        f"FROM messages m WHERE {_TO_ME} AND m.from_agent<>?",
        (old, actor, actor, actor)).fetchone()
    return {"unread": r["unread"] or 0, "alerts": r["alerts"] or 0, "questions": r["questions"] or 0,
            "questions_waiting": r["waiting"] or 0, "nudge_after": nudge}


def message_show(conn, msg_id):
    r = conn.execute("SELECT m.*, i.title item_title FROM messages m LEFT JOIN items i ON i.id=m.item_id "
                     "WHERE m.id=?", (int(msg_id),)).fetchone()
    if not r:
        raise RiverError(f"no message {msg_id}")
    return _msg_dict(r)


def thread(conn, msg_id, actor=None):
    """A message with everything before and after it in the same conversation."""
    root = _message(conn, msg_id)["thread_id"]
    with tx(conn):
        rows = [_msg_dict(r) for r in conn.execute(
            "SELECT m.*, i.title item_title FROM messages m LEFT JOIN items i ON i.id=m.item_id "
            "WHERE m.thread_id=? ORDER BY m.id", (root,))]
        if actor:
            mine = {r["id"] for r in conn.execute(
                f"SELECT m.id FROM messages m WHERE m.thread_id=? AND m.read_at IS NULL AND m.from_agent<>? AND {_TO_ME}",
                (root, actor, actor, actor))}
            _mark_read(conn, mine)
    return {"thread_id": root, "messages": rows}


def item_messages(conn, item_id):
    _item(conn, item_id)
    return [_msg_dict(r) for r in conn.execute(
        "SELECT m.*, NULL item_title FROM messages m WHERE m.item_id=? ORDER BY m.id", (int(item_id),))]


# ---------------------------------------------------------------- capacity

def capacity(conn, ann=None):
    """How many agent sessions the graph can use now, and whether too many are running.

    Ready items never wait on each other (a ready item has no open prerequisite),
    so every ready item nobody holds is a slot for one session, except that of
    two ready items that conflict (same files) only one can run.
    """
    ann = ann or annotate(conn)
    live = [a for a in ann.values() if not a["project_archived"]]
    ready = [a for a in live if a["ready"]]
    ready_ai = [a for a in ready if a["doer"] in ("any", "ai")]
    runnable, taken = 0, set()
    for a in sorted(ready_ai, key=lambda a: a["sort_key"]):
        if not taken & set(a["conflicts"]):
            runnable += 1
            taken.add(a["id"])
    ready_human = [a for a in ready if a["doer"] == "human"]
    agents = [agent_status(conn, r["name"]) for r in conn.execute("SELECT name FROM agents")]
    # Planner sessions change the plan and take no work, so they are not slots.
    ai_active = [a for a in agents if a["kind"] == "ai" and a["state"] == "active" and a.get("role") != "planner"]
    ai_busy = [a for a in ai_active if a["holds"]]
    ai_idle = [a for a in ai_active if not a["holds"]]
    # Waiting sessions (maxpm wait) take new work by themselves within seconds, and end after wait_max.
    ai_waiting = [a for a in ai_idle if a.get("role") == "waiting"]
    in_progress = [a for a in live if a["status"] in ("in_progress", "held")]

    # Width by depth: how many open items could run at each step if every earlier step finished.
    layers: dict[int, dict] = {}
    for a in live:
        if a["status"] in OPEN_STATES and a["depth"] is not None:
            layer = layers.setdefault(a["depth"], {"depth": a["depth"], "ai": 0, "human": 0})
            layer["human" if a["doer"] == "human" else "ai"] += 1
    layer_list = [layers[k] for k in sorted(layers)]

    spare = runnable - len(ai_idle)
    advice = []
    if spare > 0:
        advice.append({"kind": "launch", "text": f"{spare} ready item(s) for agents can run and have nobody on them: "
                       f"you can start up to {spare} more agent session(s)."})
    stuck_idle = max(-spare - len(ai_waiting), 0)
    if spare < 0 and ai_waiting:
        advice.append({"kind": "waiting", "text": f"{len(ai_waiting)} session(s) wait for work: they take the next "
                       f"ready item by themselves, or one you push to them, and end after "
                       f"{setting(conn, 'wait_max')} without work."})
    if stuck_idle:
        advice.append({"kind": "too_many", "text": f"{len(ai_idle)} active agent session(s) hold nothing, "
                       f"but only {runnable} ready item(s) can run for them now. "
                       f"{stuck_idle} session(s) have no work; stop them or give them other work."})
    if ready_human:
        advice.append({"kind": "human", "text": f"{len(ready_human)} ready item(s) wait on a human."})
    if not ready and any(a["status"] == "open" for a in live) and not in_progress:
        advice.append({"kind": "stuck", "text": "Nothing is ready and nothing is in progress: "
                       "every open item is blocked. Check outside blockers (maxpm list)."})
    return {
        "ready_for_agents": [a["id"] for a in sorted(ready_ai, key=lambda a: a["sort_key"])],
        "ready_for_humans": [a["id"] for a in sorted(ready_human, key=lambda a: a["sort_key"])],
        "in_progress": [a["id"] for a in in_progress],
        "agents_active": len(ai_active),
        "agents_busy": len(ai_busy),
        "agents_idle": [a["name"] for a in ai_idle],
        "agents_waiting": [a["name"] for a in ai_waiting],
        "spare_slots": max(spare, 0),
        "excess_sessions": max(-spare, 0),
        "layers": layer_list,
        "peak_width": max((l["ai"] for l in layer_list), default=0),
        "advice": advice,
    }


def completed(conn, project=None, since="7d"):
    """Done items with their output, who finished them, and when; grouped by day, with progress per project.

    `since` is a duration (7d, 12h) or None for all time. Progress counts every
    item in the project, so a short window still shows how far the project is.
    """
    cutoff = iso(now() - parse_duration(since)) if since else None
    if project is not None:
        project = _project(conn, project)["name"]
    sql = ("SELECT i.id, i.title, i.output, i.closed_at, p.name project, "
           "(SELECT e.actor FROM events e WHERE e.item_id=i.id AND e.change LIKE 'done%' ORDER BY e.id DESC LIMIT 1) by_agent "
           "FROM items i JOIN projects p ON p.id=i.project_id WHERE i.status='done' AND p.archived=0")
    args = []
    if cutoff:
        sql += " AND i.closed_at >= ?"
        args.append(cutoff)
    if project is not None:
        sql += " AND p.name = ?"
        args.append(project)
    items = [dict(r) for r in conn.execute(sql + " ORDER BY i.closed_at DESC, i.id DESC", args)]
    days = {}
    for it in items:
        days.setdefault(it["closed_at"][:10], []).append(it)
    progress = []
    for p in project_list(conn):
        if project is not None and p["name"] != project:
            continue
        counts = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) n FROM items WHERE project_id=? GROUP BY status", (p["id"],))}
        done_n = counts.get("done", 0)
        total = sum(n for st, n in counts.items() if st != "dropped")
        recent = sum(1 for it in items if it["project"] == p["name"])
        if total or recent:
            progress.append({"project": p["name"], "done": done_n, "total": total,
                             "open": total - done_n, "done_in_window": recent})
    return {"since": since, "cutoff": cutoff, "items": items,
            "by_day": [{"day": d, "items": days[d]} for d in sorted(days, reverse=True)],
            "progress": progress}


def status(conn, recent=10):
    """One overview: every project's counts, the latest completions, who is working, and open slots."""
    ann = annotate(conn)
    projects = []
    for p in project_list(conn):
        mine = [a for a in ann.values() if a["project"] == p["name"]]
        projects.append({
            "project": p["name"], "rank": p["rank"], "target": p.get("target"),
            "done": sum(a["status"] == "done" for a in mine),
            "open": sum(a["status"] in OPEN_STATES for a in mine),
            "ready": sum(a["ready"] for a in mine),
            "in_progress": sum(a["status"] in ("in_progress", "held") for a in mine),
            "human_waiting": sum(a["ready"] and a["doer"] == "human" for a in mine),
            "blocked": sum(a["status"] == "open" and not a["ready"] for a in mine),
        })
    agents = [agent_status(conn, r["name"]) for r in conn.execute("SELECT name FROM agents ORDER BY name")]
    cap = capacity(conn, ann)
    return {
        "now": iso(now()),
        "projects": projects,
        "recent": completed(conn, since=None)["items"][:recent],
        "agents": [{"name": a["name"], "kind": a["kind"], "state": a["state"], "note": a["note"],
                    "holds": a["holds"], "owns": [o["name"] for o in a["owns"]]}
                   for a in agents if not ended(a)],
        "ended": sum(1 for a in agents if ended(a)),
        "human_waiting": [{"id": a["id"], "title": a["title"], "project": a["project"]}
                          for a in sorted(ann.values(), key=lambda a: a["sort_key"])
                          if a["ready"] and a["doer"] == "human" and not a["project_archived"]],
        "due": [{"id": a["id"], "title": a["title"], "project": a["project"], "due_text": a["due_text"],
                 "due_state": a["due_state"]} for a in sorted(ann.values(), key=lambda a: a["due"] or "")
                if a["due"] and a["status"] in OPEN_STATES and not a["project_archived"]],
        "unsynced": unsynced(conn),
        "spare_slots": cap["spare_slots"],
        "excess_sessions": cap["excess_sessions"],
        "advice": cap["advice"],
    }


def _person(conn, person=None):
    if person:
        return person
    r = conn.execute("SELECT name FROM agents WHERE kind='human' ORDER BY registered_at, name LIMIT 1").fetchone()
    return r["name"] if r else "<your-name>"


def _item_prompt_section(conn, a, ann, person, n=None):
    p = conn.execute("SELECT name, path, notes FROM projects WHERE name=?", (a["project"],)).fetchone()
    blocks = [ann[d] for d in a["unblocks"] if ann[d]["status"] in OPEN_STATES]
    lines = [f"{'' if n is None else f'{n}. '}Item #{a['id']}: {a['title']}  (project {a['project']}, P{a['effective_priority']})"]
    if p["path"]:
        lines.append(f"   Folder: {p['path']}")
    if blocks:
        lines.append("   It blocks: " + "; ".join(f"#{b['id']} {b['title']} (P{b['effective_priority']})" for b in blocks[:3]))
    for label, text in (("Context", a["context"]), ("Notes", a["notes"])):
        if text.strip():
            lines.append(f"   {label}: {text.strip()}")
    cd = f"cd {p['path']} && " if p["path"] else ""
    lines.append(f"   Read it first: {cd}maxpm --as {person} show {a['id']}")
    lines.append(f"   Record the result: maxpm --as {person} done {a['id']} --output \"<what was decided or done>\"")
    return "\n".join(lines)


# How an agent puts a decision to a person in chat (river item #168: compressed tables and one-line
# recommendations made the person ask for details on every decision).
DECISION_FORMAT = """Put each decision to the person in this form, one decision at a time:

  Decision <n> of <total>: <short name>  (item #<id>)
  What you decide: one sentence, as a question, with the kind of answer (yes or no, pick A, B, or C, a number).
  Why it matters: what it changes, and what waits on it.
  Options: one line or more for each option: what happens if they pick it, what it costs, the risk,
    and whether they can change it later. Use the real names, amounts, and dates; no shorthand.
  My recommendation: the option, and why, in one or two sentences.
  Your answer: the exact words to reply, for example "A", "yes", or "$200 a month".

Then stop and wait for the answer before you show the next decision. Do not squeeze several decisions
into one table. If they ask what something means, explain it, then ask the same decision again."""

PROMPT_STEPS = """How to help:
1. Run the "read it first" command. Read the files or links it names.
2. Explain to {person} in plain words what is needed and why.
""" + "\n".join("   " + line if line else "" for line in DECISION_FORMAT.split("\n")) + """
3. Answer their questions. Help them do it: draft the text, check the setting, walk through the steps.
   Ask before anything that uses their accounts, money, or sends something in their name.
4. When it is decided or done, record it with the "record the result" command, in their words.
   If you can do the step yourself without them, say so; with their yes, take it over instead:
   maxpm register <your-agent-name>, then maxpm --as <your-agent-name> takeover <id> --note "<how>".
If the maxpm command is not found, ask {person} where MaximizePM is installed."""


def prompt_for(conn, item_id, person=None):
    """A paste-ready prompt that lets a fresh agent explain one human item and close it with the person."""
    ann = annotate(conn)
    iid = _item(conn, item_id)["id"]
    person = _person(conn, person)
    a = ann[iid]
    head = (f"You are helping {person} with one step in their work queue (MaximizePM, the `maxpm` command). "
            + ("It is marked for a person to do or decide." if a["doer"] == "human"
               else f"{person} took it to do themselves, with your help."))
    return "\n\n".join([head, _item_prompt_section(conn, a, ann, person), PROMPT_STEPS.format(person=person)])


def prompt_for_all(conn, person=None):
    """The whole needs-you list for a person as one prompt, most important first."""
    ann = annotate(conn)
    person = _person(conn, person)
    items = sorted((a for a in ann.values() if a["ready"] and a["doer"] == "human" and not a["project_archived"]),
                   key=lambda a: a["sort_key"])
    qs = [dict(r) for r in conn.execute(
        "SELECT id, from_agent, body, item_id FROM messages WHERE kind='question' AND state='open' AND to_agent=? ORDER BY id",
        (person,))]
    if not items and not qs:
        return f"Nothing in the MaximizePM queue waits on {person} now."
    parts = [f"You are helping {person} work through everything that waits on them in their work queue "
             f"(MaximizePM, the `maxpm` command): {len(items)} item(s) and {len(qs)} question(s), most important "
             f"first. Take them one at a time: finish or park one before you start the next."]
    parts += [_item_prompt_section(conn, a, ann, person, n) for n, a in enumerate(items, 1)]
    for n, q in enumerate(qs, len(items) + 1):
        parts.append(f"{n}. Question #{q['id']} from {q['from_agent']}" + (f" about #{q['item_id']}" if q["item_id"] else "")
                     + f": {q['body']}\n   Answer it: maxpm --as {person} answer {q['id']} \"<answer>\"")
    parts.append(PROMPT_STEPS.format(person=person))
    return "\n\n".join(parts)


def parse_launch_agents(value):
    """'Claude Code=@claude-code; Grok=grok "run maxpm go"' -> [("Claude Code", "@claude-code"), ...]. An entry
    that starts with @ is a launch profile; its platform and options are checked here."""
    out = []
    for part in (value or "").split(";"):
        if not part.strip():
            continue
        label, sep, cmd = part.partition("=")
        if not sep or not label.strip() or not cmd.strip():
            raise RiverError(f"launch_agents entry {part.strip()!r} needs the form Label=command, for example "
                             f"'Claude Code=@claude-code; Codex=@codex; Grok=grok \"run maxpm go in this folder and follow the briefing\"'")
        parse_profile(cmd.strip())
        out.append((label.strip(), cmd.strip()))
    if not out:
        raise RiverError("launch_agents needs at least one Label=command entry")
    return out


def parse_profile(cmd):
    """'@codex sandbox=read-only' -> ("codex", {"sandbox": "read-only"}); a custom command -> None."""
    import shlex
    if not cmd.startswith("@"):
        return None
    try:
        words = shlex.split(cmd[1:])
    except ValueError as e:
        raise RiverError(f"launch profile {cmd!r}: {e}")
    if not words or words[0] not in LAUNCH_PLATFORMS:
        raise RiverError(f"launch profile {cmd!r}: the platform is one of {', '.join('@' + p for p in LAUNCH_PLATFORMS)}")
    opts = {}
    for w in words[1:]:
        k, sep, v = w.partition("=")
        if not sep:
            raise RiverError(f"launch profile {cmd!r}: {w!r} needs the form option=value")
        _check_option(words[0], k, v)
        opts[k] = v
    return words[0], opts


def _platform_setting(key):
    """(platform, option) when the setting is a launch profile option, e.g. claude_remote_control."""
    for name, pl in LAUNCH_PLATFORMS.items():
        if key.startswith(pl["prefix"]) and key[len(pl["prefix"]):] in pl["options"]:
            return name, key[len(pl["prefix"]):]
    return None


def _check_option(platform, opt, value):
    spec = LAUNCH_PLATFORMS[platform]["options"].get(opt)
    if spec is None:
        raise RiverError(f"{platform} has no option {opt!r}; it has {', '.join(LAUNCH_PLATFORMS[platform]['options'])}")
    if spec["kind"] == "toggle" and value not in ("on", "off"):
        raise RiverError(f"{platform} {opt} is on or off")
    if spec["kind"] == "choice" and value and value not in spec["choices"]:
        raise RiverError(f"{platform} {opt} is one of {', '.join(spec['choices'])} (or empty for the CLI's own setting)")
    if opt == "model_ids":
        parse_model_ids(value, platform)
    if opt == "prompt" and not value.strip():
        raise RiverError(f"{platform} prompt: the session needs a first prompt, for example {spec['default']!r}")
    if ";" in value:
        raise RiverError(f"{platform} {opt}: ';' separates launch_agents entries; leave it out")


def parse_model_ids(value, platform="model_ids"):
    """'luna=gpt-6-luna, sol=gpt-6.1-sol' -> {"luna": "gpt-6-luna", "sol": "gpt-6.1-sol"}."""
    out = {}
    for part in (x.strip() for x in (value or "").split(",")):
        if not part:
            continue
        name, sep, mid = (x.strip() for x in part.partition("="))
        if not sep or not name or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$", mid):
            raise RiverError(f"{platform} model_ids: {part!r} needs the form name=id, for example astra=gpt-6-astra")
        out[name.lower()] = mid
    return out


def model_ids(conn):
    """{platform: {ladder name: CLI id}} from the <prefix>model_ids settings (global)."""
    out = {}
    for p, pl in LAUNCH_PLATFORMS.items():
        try:
            out[p] = parse_model_ids(setting(conn, pl["prefix"] + "model_ids"), p)
        except RiverError:
            out[p] = {}
    return out


def ladder_name(conn, model):
    """A model as river names it: a CLI id from model_ids (gpt-6-astra) becomes its ladder name (astra)."""
    for ids in model_ids(conn).values():
        for name, mid in ids.items():
            if mid.lower() == (model or "").lower():
                return name
    return model


def model_id(conn, platform, model, project_id=None, inline=None):
    """The id a platform's CLI gets for a model_ladder name: its <prefix>model_ids entry, else the name."""
    if not model or platform not in LAUNCH_PLATFORMS:
        return model
    ids = (inline or {}).get("model_ids")
    if ids is None:
        ids = setting(conn, LAUNCH_PLATFORMS[platform]["prefix"] + "model_ids", project_id=project_id)
    return parse_model_ids(ids, platform).get(model.lower(), model)


def codex_models():
    """{id: [effort levels]} from the Codex model cache (~/.codex/models_cache.json), or None without one."""
    import json
    path = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "models_cache.json"
    try:
        data = json.loads(path.read_text())
        return {m["slug"]: [x["effort"] for x in m.get("supported_reasoning_levels") or [] if x.get("effort")]
                for m in data.get("models") or [] if m.get("slug")}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


# The system whose shell gets the commands river builds: its quoting, and how the page opens a session (a
# Terminal tab on macOS, a console window on Windows). Tests set it.
PLATFORM = sys.platform


def tmux_path():
    """The tmux program, or None when it is not installed: on PATH, else where Homebrew or MacPorts put it
    (the desktop app starts maxpm serve from the Dock, with a short PATH)."""
    return shutil.which("tmux") or next((p for p in ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux",
                                                     "/opt/local/bin/tmux") if os.access(p, os.X_OK)), None)


def _shell_quote(s):
    import shlex
    if PLATFORM == "win32":
        return f'"{s}"' if not s or re.search(r'[\s"&|<>^%]', s) else s
    return shlex.quote(s)


def river_dir():
    """The folder of the queue, which an agent's sandbox must let it write (Codex --add-dir)."""
    return str(db_path().expanduser().resolve().parent)


def profile_options(conn, platform, inline=None, project_id=None, chosen=None):
    """A profile's option values: the launch dialog's choice, else the entry's own, else the settings."""
    pl = LAUNCH_PLATFORMS[platform]
    for k, v in (chosen or {}).items():
        spec = pl["options"].get(k)
        if spec is None or spec["kind"] == "text":
            raise RiverError(f"{pl['label']} has no option {k!r} to choose at launch; it has "
                             f"{', '.join(o for o, sp in pl['options'].items() if sp['kind'] != 'text')}")
        _check_option(platform, k, v)
    return {k: (chosen or {}).get(k, (inline or {}).get(k, setting(conn, pl["prefix"] + k, project_id=project_id)))
            for k in pl["options"]}


def _short_title(text, limit=48):
    """A session name: short, and only characters that are safe in a shell word, a console title, and a
    session list. A long one ends at a word."""
    text = " ".join(re.sub(r"[^\w #.:-]+", " ", text).split())
    if len(text) > limit:
        text = text[:limit + 1].rsplit(" ", 1)[0] if " " in text[limit // 2:limit + 1] else text[:limit]
    return text.rstrip()


def session_title(goals, item_id, title):
    """The name of a session river starts for an item: the goal it opens with, else '#<id> <title>'."""
    return _short_title(goals[0] if goals else f"#{item_id} {title}")


def focus_title(conn, focus):
    """The name of a session river starts with MAXPM_FOCUS: 'help #7 <title>', 'deploy web', 'needs you'."""
    kind, _, rest = focus.partition(":")
    ref = rest.split("@")[0]
    if kind == "needs":
        return "needs you"
    if ref.isdigit():
        r = conn.execute("SELECT title FROM items WHERE id=?", (int(ref),)).fetchone()
        return _short_title(f"{kind} #{ref} {r['title'] if r else ''}")
    return _short_title(f"{kind} {ref}")


# The packaged agent guides, as cli.GUIDES finds them: inside the package when installed, else the repository's.
GUIDES = next((p for p in (Path(__file__).resolve().parent / "skills", Path(__file__).resolve().parent.parent / "skills")
               if p.is_dir()), Path(__file__).resolve().parent / "skills")

SKILL_PROMPT_HEAD = ("# The maxpm skill (already loaded)\n\n"
                     "This is the whole maxpm skill. It is already in your context: do not load it with the Skill "
                     "tool.\n\n")


def skill_prompt_args(args):
    """args (the claude_args setting) with one --append-system-prompt-file: a file of the primer that args names
    (if any), then the maxpm skill. The file's name is a hash of its text, so every launch with the same primer
    and skill passes the same bytes, and Claude Code reads that system prompt from the prompt cache. args stays
    as it is when the skill or the primer cannot be read."""
    import hashlib
    import shlex
    try:
        if PLATFORM == "win32":  # a POSIX split drops the backslashes of a Windows path
            words = [w[1:-1] if len(w) > 1 and w[0] == w[-1] == '"' else w
                     for w in shlex.split(args or "", posix=False)]
        else:
            words = shlex.split(args or "")
    except ValueError:
        return args
    rest, primer = [], None
    i = 0
    while i < len(words):
        w = words[i]
        if w == "--append-system-prompt-file" and i + 1 < len(words):
            primer, i = words[i + 1], i + 2
            continue
        if w.startswith("--append-system-prompt-file="):
            primer = w.split("=", 1)[1]
        else:
            rest.append(w)
        i += 1
    try:
        skill = (GUIDES / "maxpm" / "SKILL.md").read_text()
        head = (Path(primer).expanduser().read_text().rstrip() + "\n\n") if primer else ""
    except OSError:
        return args
    if skill.startswith("---"):
        skill = skill.split("---", 2)[2]
    text = head + SKILL_PROMPT_HEAD + skill.strip() + "\n"
    f = Path(river_dir()) / "prompts" / (hashlib.sha256(text.encode()).hexdigest()[:16] + ".md")
    if not f.is_file():
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + f".{os.getpid()}.tmp")
        tmp.write_text(text)
        tmp.replace(f)
    return " ".join([_shell_quote(w) for w in rest] + ["--append-system-prompt-file", _shell_quote(str(f))])


def autocompact_tokens(value):
    """The manager_autocompact setting as a token count (None for auto), checked as claude --autocompact
    checks it: 100k to 1M, written 200k, 200000, 1m, or 200 (thousands)."""
    v = (value or "").strip().lower()
    if v == "auto":
        return None
    m = re.match(r"^(\d+)(k|m)?$", v)
    n = int(m.group(1)) * {"k": 1000, "m": 1000000}.get(m.group(2), 1) if m else 0
    if m and not m.group(2) and n < 1000:
        n *= 1000
    if not 100000 <= n <= 1000000:
        raise RiverError(f"manager_autocompact {value!r}: auto, or 100k to 1M tokens (for example 200k)")
    return n


def build_command(platform, opts, model=None, effort=None, name=None):
    """The command line that starts a platform's CLI with these options. name: the session's name, for a
    CLI that takes one (claude --name, and the Remote Control session name); Codex has no flag for it."""
    q = _shell_quote
    if platform == "claude-code":
        parts = ["claude"] + (["--name", q(name)] if name else [])
        parts += (["--model", model] if model else []) + (["--effort", effort] if effort else [])
        parts += ["--permission-mode", opts["permission_mode"]] if opts.get("permission_mode") else []
        parts += [opts["args"].strip()] if opts.get("args", "").strip() else []
        # The prompt goes before --remote-control: that flag takes an optional session name after it.
        parts.append(q(opts["prompt"]))
        parts += ["--remote-control"] + ([q(name)] if name else []) if opts.get("remote_control") == "on" else []
    elif platform == "codex":
        parts = ["codex"] + (["-m", model] if model else []) + (["-c", f"model_reasoning_effort={effort}"] if effort else [])
        parts += ["--sandbox", opts["sandbox"]] if opts.get("sandbox") else []
        parts += ["--ask-for-approval", opts["approval"]] if opts.get("approval") else []
        parts += ["--add-dir", q(river_dir())]
        parts += [opts["args"].strip()] if opts.get("args", "").strip() else []
        parts.append(q(opts["prompt"]))
    else:
        raise RiverError(f"no command builder for {platform}")
    return " ".join(parts)


# maxpm launch --prompt: a first instruction for one session. It follows the profile's prompt, so the session
# still runs go and registers. The agent CLI gets it as the session's own first prompt.
CUSTOM_PROMPT_MAX = 4000


def custom_prompt(text):
    """The text of maxpm launch --prompt, checked: not empty, at most CUSTOM_PROMPT_MAX characters, and no
    control characters but line breaks (a launch types the command into a shell, where a tab completes and
    Ctrl-C stops). A tab becomes a space. Windows: cmd cannot quote a line break, '"' or '%', so they are refused."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ").strip()
    if not text:
        raise RiverError("--prompt is empty; give the session's first instruction, or leave --prompt out")
    if len(text) > CUSTOM_PROMPT_MAX:
        raise RiverError(f"--prompt has {len(text)} characters; at most {CUSTOM_PROMPT_MAX}. Put the long part in a "
                         f"file or an item's context, and name it in the prompt")
    bad = sorted({c for c in text if (ord(c) < 32 and c != "\n") or ord(c) == 127})
    if bad:
        raise RiverError(f"--prompt has control characters ({', '.join(repr(c) for c in bad)}); leave them out")
    if PLATFORM == "win32" and re.search(r'[\n"%]', text):
        raise RiverError("--prompt on Windows is one line with no '\"' or '%': cmd cannot quote them")
    return text


def join_prompt(first, extra):
    """The profile's first prompt, a blank line, then the custom prompt (Windows: one line, a space between)."""
    return f"{first} {extra}" if PLATFORM == "win32" else f"{first}\n\n{extra}"


def entry_exe(cmd):
    """The program a launch_agents command starts (a profile's CLI, else the first word)."""
    prof = parse_profile(cmd)
    if prof:
        return LAUNCH_PLATFORMS[prof[0]]["exe"]
    return cmd.split()[0] if cmd.split() else ""


def profile_from_command(cmd):
    """An old hand-written command that a profile can build exactly -> (platform, options); else None.
    Unknown flags, a fixed model, or another --add-dir keep the command custom, so nothing is lost."""
    import shlex
    try:
        w = shlex.split(cmd)
    except ValueError:
        return None
    if not w:
        return None
    exe = Path(w[0]).name.lower()
    platform = {"claude": "claude-code", "codex": "codex"}.get(exe)
    if platform is None:
        return None
    opts, prompt, i = {}, None, 1
    if platform == "claude-code":
        opts["remote_control"] = "off"
    choices = LAUNCH_PLATFORMS[platform]["options"]
    while i < len(w):
        t, nxt = w[i], (w[i + 1] if i + 1 < len(w) else None)
        if platform == "claude-code" and t == "--model" and nxt == "{model}" or \
                platform == "claude-code" and t == "--effort" and nxt == "{effort}" or \
                platform == "codex" and t in ("-m", "--model") and nxt == "{model}" or \
                platform == "codex" and t in ("-c", "--config") and nxt == "model_reasoning_effort={effort}":
            i += 2
        elif platform == "claude-code" and t == "--remote-control":
            if nxt is not None and not nxt.startswith("-"):
                return None  # a session name, or a prompt that the flag would take as one
            opts["remote_control"] = "on"
            i += 1
        elif platform == "claude-code" and t == "--permission-mode" and nxt in choices["permission_mode"]["choices"]:
            opts["permission_mode"] = nxt
            i += 2
        elif platform == "codex" and t in ("-s", "--sandbox") and nxt in choices["sandbox"]["choices"]:
            opts["sandbox"] = nxt
            i += 2
        elif platform == "codex" and t in ("-a", "--ask-for-approval") and nxt in choices["approval"]["choices"]:
            opts["approval"] = nxt
            i += 2
        elif platform == "codex" and t == "--add-dir" and nxt is not None and (
                nxt == "{maxpm_dir}" or os.path.realpath(os.path.expanduser(nxt)) == os.path.realpath(river_dir())):
            i += 2
        elif not t.startswith("-") and prompt is None and t.strip() and ";" not in t:
            prompt = t
            i += 1
        else:
            return None
    if prompt is None:
        return None
    opts["prompt"] = prompt
    return platform, opts


def _migrate_levels(conn):
    """The three message levels of #1583 (urgent, normal, low) become two levels and a flag (#1608), one time:
    a level urgent that a sender set becomes the flag blocked, normal becomes high (the default, so nothing is
    stored), and the setting manage_wait_normal becomes manage_wait_high."""
    if conn.execute("SELECT 1 FROM meta WHERE key='message_levels_1608'").fetchone():
        return
    with tx(conn):
        if conn.execute("SELECT 1 FROM meta WHERE key='message_levels_1608'").fetchone():
            return  # another process ran it just now
        conn.execute("INSERT INTO meta(key, value) VALUES ('message_levels_1608', ?)", (iso(now()),))
        conn.execute("UPDATE messages SET blocked='sender', level=NULL WHERE level='urgent'")
        conn.execute("UPDATE messages SET level=NULL WHERE level='normal'")
        conn.execute("UPDATE OR REPLACE settings SET key='manage_wait_high' WHERE key='manage_wait_normal'")


def _migrate_launch_agents(conn):
    """Turn hand-written launch_agents commands that a profile builds exactly into profiles (@claude-code,
    @codex). An option that differs from the built-in default becomes a setting in the same scope, or, for a
    second entry of the same platform there, part of the entry. The event says what changed."""
    if conn.execute("SELECT 1 FROM meta WHERE key='launch_profiles'").fetchone():
        return
    rows = conn.execute("SELECT scope, value FROM settings WHERE key='launch_agents'").fetchall()
    todo = []
    for r in rows:
        try:
            agents = parse_launch_agents(r["value"])
        except RiverError:
            continue
        if any(profile_from_command(c) for _, c in agents):
            todo.append(r["scope"])
    with tx(conn):
        if conn.execute("SELECT 1 FROM meta WHERE key='launch_profiles'").fetchone():
            return  # another process ran it just now
        conn.execute("INSERT INTO meta(key, value) VALUES ('launch_profiles', ?)", (iso(now()),))
        for scope in todo:
            r = conn.execute("SELECT value FROM settings WHERE key='launch_agents' AND scope=?", (scope,)).fetchone()
            if not r:
                continue
            agents, notes, base = parse_launch_agents(r["value"]), [], {}
            out = []
            for label, cmd in agents:
                got = profile_from_command(cmd)
                if not got:
                    out.append((label, cmd))
                    continue
                platform, opts = got
                pl = LAUNCH_PLATFORMS[platform]
                if platform not in base:
                    base[platform] = {}
                    for k in pl["options"]:
                        have = conn.execute("SELECT value FROM settings WHERE scope=? AND key=?",
                                            (scope, pl["prefix"] + k)).fetchone()
                        base[platform][k] = have["value"] if have else pl["options"][k]["default"]
                        if not have and opts.get(k, base[platform][k]) != base[platform][k]:
                            conn.execute("INSERT INTO settings(scope,key,value) VALUES (?,?,?)",
                                         (scope, pl["prefix"] + k, opts[k]))
                            base[platform][k] = opts[k]
                            notes.append(f"{pl['prefix']}{k}={opts[k]}")
                inline = {k: v for k, v in opts.items() if v != base[platform][k]}
                out.append((label, "@" + platform + "".join(f" {k}={_shell_quote(v)}" for k, v in inline.items())))
                notes.insert(0, f"{label}: {cmd} -> {out[-1][1]}")
            new = "; ".join(f"{a}={c}" for a, c in out)
            conn.execute("UPDATE settings SET value=? WHERE key='launch_agents' AND scope=?", (new, scope))
            _event(conn, None, "maxpm", f"launch_agents migrated to launch profiles ({scope}): " + "; ".join(notes))


def launch_migration_note(conn):
    """What the move to launch profiles changed, for maxpm config get launch_agents."""
    r = conn.execute("SELECT at, change FROM events WHERE change LIKE 'launch_agents migrated%' ORDER BY id DESC LIMIT 1"
                     ).fetchone()
    return f"{r['change']} (on {r['at']})" if r else None


def waiting_agent_for(conn, project, item_id=None):
    """An active session that waits for work in this project (maxpm wait), longest waiting first; with
    item_id, only one whose model the item's limits allow, and for a release review, none that worked on
    the release (release_authors)."""
    it = _item(conn, item_id) if item_id is not None else None
    authors = release_authors(conn, it["id"]) if it is not None and it["kind"] == "review" else set()
    for r in conn.execute("SELECT name, waiting_in FROM agents WHERE role='waiting' AND kind='ai' "
                          "ORDER BY waiting_since").fetchall():
        a = agent_status(conn, r["name"])
        if a["state"] == "active" and not a["holds"] and project in (r["waiting_in"] or "").split(","):
            if r["name"] not in authors and (it is None or not _model_refusal(conn, it, r["name"])):
                return r["name"]
    return None


RELEASE_COMMITS_TTL = 120  # seconds a release_commits answer is reused (maxpm serve asks on every pass)
_RELEASE_COMMITS = {}  # (review id, command) -> (monotonic time it runs out, item ids)


def release_commit_items(conn, review):
    """The items that the commits of a release name (setting release_commits): every #<id> the command prints, of
    items that exist and are no review or deploy item. An empty set when the setting is empty or the command fails.
    The covered list of a review holds only what agents sent with maxpm ship; a commit can name more (#1236)."""
    import subprocess
    import time
    cmd = (setting(conn, "release_commits", item_id=review["id"]) or "").strip()
    if not cmd or review["kind"] != "review":
        return set()
    key = (review["id"], cmd)
    hit = _RELEASE_COMMITS.get(key)
    if hit and hit[0] > time.monotonic():
        return set(hit[1])
    review = dict(review)
    if "project" not in review:  # a row of items (_item) has the project id only
        review["project"] = conn.execute("SELECT name FROM projects WHERE id=?", (review["project_id"],)).fetchone()[0]
    folder = _review_folder(conn, review)
    last = conn.execute("SELECT output FROM items WHERE kind='deploy' AND target IS ? AND status='done' "
                        "ORDER BY closed_at DESC, id DESC LIMIT 1", (review["target"],)).fetchone()
    env = {**os.environ, "MAXPM_LAST_RELEASE": (last["output"] or "") if last else "",
           "MAXPM_TARGET": review["target"] or "", "MAXPM_REVIEW": str(review["id"])}
    try:
        r = subprocess.run(cmd, shell=True, cwd=os.path.expanduser(folder["path"]) if folder else None, env=env,
                           capture_output=True, text=True, timeout=60)
        named = {int(x) for x in re.findall(r"#(\d+)\b", r.stdout)} if r.returncode == 0 else set()
    except (OSError, subprocess.SubprocessError):
        named = set()
    ids = set()
    if named:
        marks = ",".join("?" * len(named))
        ids = {row["id"] for row in conn.execute(
            f"SELECT id FROM items WHERE id IN ({marks}) AND kind NOT IN ('review','deploy')", sorted(named))}
    _RELEASE_COMMITS[key] = (time.monotonic() + RELEASE_COMMITS_TTL, frozenset(ids))
    return ids


def _prime_release_commits(conn, item_id=None):
    """Ask release_commits before a claim takes the write lock, for the release reviews that the claim can meet
    (this one; else each open review that waits on nothing open). The answer stays RELEASE_COMMITS_TTL, so
    author_refusal inside the transaction starts no process while other commands wait for the lock (#1617)."""
    sql = ("SELECT i.*, p.name project FROM items i JOIN projects p ON p.id=i.project_id "
           "WHERE i.kind='review' AND i.status='open' AND ")
    if item_id is not None:
        rows = conn.execute(sql + "i.id=?", (item_id,)).fetchall()
    else:
        rows = conn.execute(sql + "NOT EXISTS (SELECT 1 FROM deps d JOIN items b ON b.id=d.blocked_by WHERE "
                            "d.item_id=i.id AND d.kind<>'conflicts' AND b.status NOT IN ('done','dropped'))").fetchall()
    for r in rows:
        release_commit_items(conn, dict(r))


def release_join_commits(conn, review_id, actor="maxpm"):
    """Add the done items that the release's commits name (release_commit_items) to the release review and its deploy
    item, as maxpm ship does, so the review checks them and the release lists them. Returns the ids added."""
    review = _item(conn, review_id)
    ids = release_commit_items(conn, review)
    if not ids:
        return []
    added = []
    with tx(conn):
        deploys = [r["item_id"] for r in conn.execute(
            "SELECT d.item_id FROM deps d JOIN items i ON i.id=d.item_id WHERE d.blocked_by=? AND i.kind='deploy'",
            (review_id,))]
        for item_id in sorted(ids):
            it = _item(conn, item_id)
            if it["status"] != "done":
                continue  # an open item would hold the release back; its authors are kept out all the same
            target = conn.execute("SELECT target FROM projects WHERE id=?", (it["project_id"],)).fetchone()[0]
            if target != review["target"]:
                continue  # a number that names an item of another product (an issue number, say) joins nothing
            new = False
            for owner in [review_id] + deploys:
                if not conn.execute("SELECT 1 FROM deps WHERE item_id=? AND blocked_by=?", (owner, item_id)).fetchone():
                    _dep_add(conn, owner, item_id, actor, alert=False)
                    new = True
            if new:
                _event(conn, item_id, actor, f"joins release review #{review_id}: a commit of the release names it "
                                             f"(release_commits)")
                added.append(item_id)
    return added


def release_authors(conn, review_id):
    """The agents that worked on what a release review covers: whoever claimed or closed an item it ships, or an
    item that a commit of the release names (release_commits), and a reviewer that said it wrote commits of the
    release (maxpm release <review> --author). A session started for the review is none of them."""
    review = _item(conn, review_id)
    # The reviewer's own word: the pin moved after the review went out, and holds its commits now (#1616).
    names = {r["actor"].split("@")[0] for r in conn.execute(  # name@relay: the command came through the relay
        "SELECT DISTINCT actor FROM events WHERE item_id=? AND change LIKE ?", (review_id, AUTHOR_RELEASE + "%"))}
    ids = [r["blocked_by"] for r in conn.execute(
        "SELECT d.blocked_by FROM deps d JOIN items i ON i.id=d.blocked_by WHERE d.item_id=? AND i.kind<>'review'",
        (review_id,))]
    ids = sorted(set(ids) | release_commit_items(conn, review))
    if not ids:
        return names
    marks = ",".join("?" * len(ids))
    names |= {r["assignee"] for r in conn.execute(f"SELECT assignee FROM items WHERE id IN ({marks})", ids) if r["assignee"]}
    names |= {r["actor"] for r in conn.execute(
        f"SELECT DISTINCT actor FROM events WHERE item_id IN ({marks}) AND (change LIKE 'claimed%' OR change LIKE 'done%')",
        ids)}
    return names


def author_refusal(conn, item, agent):
    """Why this agent may not take this release review, or None: an agent session that worked on what the review
    covers (release_authors) does not review its own work. A person may; maxpm serve starts a reviewer that did not."""
    if item["kind"] != "review" or not _is_ai(conn, agent) or agent not in release_authors(conn, item["id"]):
        return None
    return (f"you worked on what release review #{item['id']} covers; a review needs an agent that did not write the "
            f"release (maxpm serve starts one)")


def _review_folder(conn, item):
    """Where a session for a release review opens: the review's project folder, else the folder of the first
    project of its target, else of a project the release ships. None when none has one."""
    p = _project(conn, item["project"])
    if p["path"]:
        return p
    names = [r["name"] for r in conn.execute("SELECT name FROM projects WHERE target=? AND archived=0 ORDER BY rank, id",
                                             (item["target"],))] if item["target"] else []
    names += [r["name"] for r in conn.execute(
        "SELECT p.name FROM deps d JOIN items i ON i.id=d.blocked_by JOIN projects p ON p.id=i.project_id "
        "WHERE d.item_id=? ORDER BY p.rank, p.id", (item["id"],))]
    return next((q for q in (_project(conn, n) for n in names) if q["path"]), None)


def covered_projects(conn, ann=None):
    """Projects that have an agent, with one of its sessions: a session that is not gone and holds an item
    there, waits for work there (maxpm wait), or has an item there pushed to it (a Start just opened it)."""
    return {p: names[0] for p, names in project_sessions(conn, ann).items()}


def project_sessions(conn, ann=None):
    """{project: [the sessions that cover it, as covered_projects counts them]}."""
    ann = ann if ann is not None else annotate(conn)
    live = {r["name"] for r in conn.execute("SELECT * FROM agents WHERE kind='ai'")
            if _agent_state(conn, r) not in ("gone", "stopped") and not not_connected(conn, r)}
    covered = {}
    for a in sorted(ann.values(), key=lambda a: a["id"]):
        who = (a["assignee"] if a["status"] in ("in_progress", "held") else
               a["reserved_for"] if a["status"] == "open" and a["reserved_until"] else None)
        if who in live and who not in covered.get(a["project"], []):
            covered.setdefault(a["project"], []).append(who)
    for r in conn.execute("SELECT name, waiting_in FROM agents WHERE role='waiting' AND kind='ai' ORDER BY name"):
        if r["name"] in live:
            for p in (r["waiting_in"] or "").split(","):
                if p and r["name"] not in covered.get(p, []):
                    covered.setdefault(p, []).append(r["name"])
    return covered


def launch_target(conn, project=None, agent=None, item=None, model=None, effort=None, launch_in=None, spread=False,
                  options=None, actor=None, prompt=None):
    """Where and how a new agent session should start: the folder of the project that holds the most
    important ready item an agent can take (or of the project named, or of the one item named), and the
    command of the chosen launch_agents entry (the first when none is named). Refuses when nothing is ready there.

    spread (Start with no project or item named): first a project with ready agent work and no agent yet,
    the one whose top item is most important; when every such project has an agent, the top item.

    The one item named can be reserved for the actor (given to a manager, who takes no work): the new
    session gets it. prompt: maxpm launch --prompt, after the profile's first prompt."""
    ann = annotate(conn)
    project = _project(conn, project)["name"] if project else None
    own = conn.execute("SELECT 1 FROM items WHERE id=? AND reserved_for=? AND status='open'",
                       (int(item), actor)).fetchone() if item is not None and actor else None
    # A ready release review is work for any agent (role REVIEWER); a deploy item is the target owner's.
    pool = sorted((a for a in ann.values() if a["ready"] and a["doer"] != "human" and a["kind"] not in ("deploy", "monitor")
                   and (not a["reserved_for"] or (own and a["id"] == int(item))) and not a["project_archived"]
                   and (project is None or a["project"] == project)), key=lambda a: a["sort_key"])
    if item is not None:
        top = next((a for a in pool if a["id"] == int(item)), None)
        if top is None:
            a = ann.get(int(item))
            held = a is not None and a["status"] == "open" and _item(conn, item)["reserved_for"]
            raise RiverError(f"#{item} is not ready for an agent" + (
                "" if a is None else f" (status {a['status']}" + (f", reserved for {a['reserved_for']}" if a["reserved_for"] else "")
                + (", for a person" if a["doer"] == "human" else "") + (", waits on open items" if not a["ready"] else "")
                + (", a deploy item: only the owner of its target takes it" if a["kind"] == "deploy" else "") + ")")
                + (f"; {_unreserve_hint(item)}" if held else ""))
    elif not pool:
        raise RiverError("nothing is ready for an agent" + (f" in {project}" if project else "")
                         + "; a new session would have no work")
    else:
        top = pool[0]
    why = None
    if spread and item is None and project is None:
        covered = covered_projects(conn, ann)
        firsts = {}
        for a in pool:
            firsts.setdefault(a["project"], a)
        open_ = [a for p, a in firsts.items() if p not in covered]
        have = [p for p in firsts if p in covered]
        if open_:
            top = open_[0]
            why = f"first agent for project {top['project']}" + (
                f"; {', '.join(have)} already {'has' if len(have) == 1 else 'have'} one" if have else "")
        else:
            why = "every project with ready work has an agent, so the most important ready item"
    p = _project(conn, top["project"])
    if not p["path"] and top["kind"] == "review":
        # A deploy project has no folder: the reviewer starts in a folder of the release.
        p = _review_folder(conn, top) or p
    if not p["path"]:
        raise RiverError(f"project {p['name']} has no folder, so MaximizePM cannot start a session there: "
                         f"maxpm project path {p['name']} <folder>")
    if agent is None and top["agent"]:
        agent = agent_for_type(conn, top["agent"], p["id"])
        if agent is None:
            raise RiverError(f"#{top['id']} is for {top['agent']}, and no launch_agents entry runs it; add one: "
                             f"maxpm config set launch_agents \"...; {LAUNCH_PLATFORMS[top['agent']]['label']}=@{top['agent']}\"")
    if model:
        ok, why = model_check(parse_ladder(setting(conn, "model_ladder")), model, top["min_model"], top["max_model"])
        if not ok:
            raise RiverError(f"#{top['id']} {why}; pick another model")
    name = session_title(top["goals"], top["id"], top["title"])
    cmd = _launch_agent_cmd(conn, p["id"], agent, model, effort, options, name, prompt)
    base = fork_base(conn, top, p["path"], model, cmd["platform"])
    if base:  # a worker of the goal starts from the warm base: it reads the goal's context from the cache
        cmd = _launch_agent_cmd(conn, p["id"], agent, model, effort, options, name, prompt, fork_of=base["session_id"])
        cmd["env"] = {**cmd["env"], "MAXPM_FORK_OF": base["goal"]}  # its go briefing says what it is now
    return {"project": p["name"], "path": p["path"], "item": {"id": top["id"], "title": top["title"], "agent": top["agent"]},
            "ready": len(pool), "why": why, "session_title": name, **cmd, "fork_of": base,
            "launch_in": _launch_in(conn, p["id"], launch_in)}


def agent_for_type(conn, agent_type, project_id=None):
    """The first launch_agents entry that runs this agent type (a profile of it, or a custom command of its CLI)."""
    for label, cmd in parse_launch_agents(setting(conn, "launch_agents", project_id=project_id)):
        prof = parse_profile(cmd)
        if (prof[0] if prof else _exe_platform(cmd)) == agent_type:
            return label
    return None


def _start_next(conn):
    """What Start (work: top item) opens now, and why; None when nothing is ready."""
    try:
        t = launch_target(conn, spread=True)
    except RiverError:
        return None
    return {**t["item"], "project": t["project"], "why": t["why"]}


def _launch_in(conn, project_id, choice=None):
    """Where a new session opens: the choice of one start, else the setting launch_in; auto is tmux when
    tmux is installed, else tab."""
    if choice not in (None, "", *LAUNCH_INS):
        raise RiverError("launch_in is tab, window, or tmux")
    where = choice or setting(conn, "launch_in", project_id=project_id)
    return ("tmux" if tmux_path() else "tab") if where == "auto" else where


def _launch_agent_cmd(conn, project_id, agent, model=None, effort=None, options=None, name=None, prompt=None,
                      fork_of=None, autocompact=None):
    """The chosen launch_agents entry as a command: a profile builds it from its options (options: the
    launch dialog's choices) and gives the session its name, a custom command gets {model} and {effort}
    filled in. The session also gets MAXPM_MODEL. prompt (maxpm launch --prompt) follows the profile's
    first prompt; a custom command has no prompt option to put it in. autocompact (tokens, the manager's
    manager_autocompact): claude --autocompact, unless the profile's args already name one."""
    agents = parse_launch_agents(setting(conn, "launch_agents", project_id=project_id))
    pick = agents[0] if agent is None else next((a for a in agents if a[0] == agent), None)
    if pick is None:
        raise RiverError(f"no agent {agent!r} in launch_agents; known: {', '.join(a for a, _ in agents)}")
    model = _check_model_name(model) if model else None
    if effort and not re.match(r"^[a-z][a-z0-9_-]{0,31}$", effort):
        raise RiverError(f"effort {effort!r}: a level such as low, medium, high")
    prof = parse_profile(pick[1])
    # The CLI gets its own id for the model (codex -m gpt-6-astra); river keeps the ladder name everywhere else.
    platform = prof[0] if prof else {"claude": "claude-code", "codex": "codex"}.get(Path(entry_exe(pick[1])).name.lower())
    mid = model_id(conn, platform, model, project_id, prof[1] if prof else None)
    if effort and platform == "codex":
        _check_codex_effort(mid, effort)
    prompt = custom_prompt(prompt) if prompt is not None else None
    if prof:
        opts = profile_options(conn, prof[0], prof[1], project_id, options)
        if prompt:
            opts = {**opts, "prompt": join_prompt(opts["prompt"], prompt)}
        built = opts
        if prof[0] == "claude-code" and opts.get("skill_prompt") == "on":
            built = {**opts, "args": skill_prompt_args(opts.get("args", ""))}
        if fork_of and prof[0] == "claude-code":  # fork_base: start from a goal's warm base
            built = {**built, "args": f"--resume {_shell_quote(fork_of)} --fork-session " + built.get("args", "")}
        if autocompact and prof[0] == "claude-code" and "--autocompact" not in built.get("args", ""):
            built = {**built, "args": f"--autocompact {autocompact} " + built.get("args", "")}
        else:
            autocompact = None
        cmd = build_command(prof[0], built, mid, effort or None, name)
    elif prompt:
        raise RiverError(f"{pick[0]} is a custom command ({pick[1]}), so it takes no --prompt; give it a profile "
                         f"in launch_agents (@claude-code or @codex) for one")
    elif options:
        raise RiverError(f"{pick[0]} is a custom command ({pick[1]}), so it has no launch options; "
                         f"give it a profile in launch_agents (@claude-code or @codex) for them")
    else:
        opts, cmd, autocompact = {}, fill_launch_command(pick[1], mid, effort or None, name), None
    return {"agent": pick[0], "command": cmd, "platform": prof[0] if prof else None, "options": opts,
            "model": model, "model_id": mid, "effort": effort or None, "custom_prompt": prompt,
            "autocompact": autocompact,
            "env": {"MAXPM_MODEL": model} if model else {}}


def _check_codex_effort(mid, effort):
    """Refuse an effort that Codex does not take: for the model, when the Codex model cache lists it,
    else Codex's levels in PLATFORM_EFFORTS."""
    known = codex_models() or {}
    levels = known.get(mid) if mid else None
    levels = levels or PLATFORM_EFFORTS["openai"]
    if effort not in levels:
        raise RiverError(f"Codex{' ' + mid if mid else ''} takes effort {', '.join(levels)}, not {effort}; pick one of them")


# Which model family an agent CLI runs, by the program that starts it, and the effort levels its CLI takes.
AGENT_FAMILIES = {"claude": "claude", "codex": "openai"}
PLATFORM_EFFORTS = {"claude": ["low", "medium", "high", "xhigh", "max"],
                    "openai": ["low", "medium", "high", "xhigh", "max"]}
# One line per model in the launch dialog: when it fits.
MODEL_NOTES = {
    "haiku": "quick, simple work: lookups, formatting, trivial edits",
    "sonnet": "routine, well specified work: monitors, checks, small fixes",
    "opus": "normal feature work that follows the code already there",
    "fable": "hard design, hard bugs, security, data that is costly to lose",
    "luna": "quick routine edits and checks",
    "terra": "routine work that needs some judgment",
    "sol": "normal feature work that follows the code already there",
    "astra": "the hardest problems: design, hard bugs, security",
}


def fill_launch_command(cmd, model, effort, name=None):
    """Put the model, effort, and session name into a launch command. Without a value the placeholder goes,
    and with it the flag just before it: '--model {model}' and '-c model_reasoning_effort={effort}' drop out
    whole. The name goes in quoted for the shell: write '--name {name}' with no quotes of your own."""
    for key, value in (("model", model), ("effort", effort), ("name", _shell_quote(name) if name else None)):
        ph = "{" + key + "}"
        if value:
            cmd = cmd.replace(ph, value)
            continue

        def drop(m):
            # A word that is a flag itself ('--effort={effort}') goes alone; the flag before it stays.
            return (m.group(1) or "") if m.group(2).startswith("-") else ""
        cmd = re.sub(r"(\s+-[\w-]+)?\s+(\S*" + re.escape(ph) + r"\S*)", drop, " " + cmd)[1:]
    return cmd


def launch_options(conn):
    """What the launch dialog offers for each launch_agents entry: its family, models, and effort levels."""
    ladder = parse_ladder(setting(conn, "model_ladder"))
    levels = _levels(setting(conn, "effort_levels"))
    out = []
    for label, cmd in parse_launch_agents(setting(conn, "launch_agents")):
        prof = parse_profile(cmd)
        fam = LAUNCH_PLATFORMS[prof[0]]["family"] if prof else AGENT_FAMILIES.get(Path(entry_exe(cmd)).name.lower())
        fam = fam if fam in ladder else None
        out.append({"label": label, "family": fam, "platform": prof[0] if prof else None,
                    "agent_type": prof[0] if prof else _exe_platform(cmd),
                    "models": [{"name": m, "note": MODEL_NOTES.get(m, ""), "family": f}
                               for f, ms in ladder.items() if fam in (None, f) for m in ms],
                    "efforts": PLATFORM_EFFORTS.get(fam, levels),
                    "takes_model": bool(prof) or "{model}" in cmd, "takes_effort": bool(prof) or "{effort}" in cmd,
                    # The options the dialog can change for one launch, preset from the entry and the settings.
                    "options": profile_option_list(conn, prof[0], prof[1], kinds=("toggle", "choice")) if prof else []})
    return out


def profile_option_list(conn, platform, inline=None, project_id=None, kinds=("toggle", "choice", "text")):
    """A platform's options with their values, for the page: [{name, setting, kind, choices, value, text}]."""
    pl = LAUNCH_PLATFORMS[platform]
    vals = profile_options(conn, platform, inline, project_id)
    return [{"name": k, "setting": pl["prefix"] + k, "kind": sp["kind"], "choices": sp.get("choices", []),
             "value": vals[k], "text": sp["text"], "fixed": k in (inline or {})}
            for k, sp in pl["options"].items() if sp["kind"] in kinds]


def launch_profiles(conn):
    """Settings > Setup: each platform that a launch_agents entry uses, with all its options."""
    used = []
    for _, cmd in parse_launch_agents(setting(conn, "launch_agents")):
        prof = parse_profile(cmd)
        if prof and prof[0] not in used:
            used.append(prof[0])
    return [{"platform": p, "label": LAUNCH_PLATFORMS[p]["label"], "options": profile_option_list(conn, p)}
            for p in used]


def recent_events(conn, limit=40):
    return [dict(r) for r in conn.execute(
        "SELECT e.at, e.actor, e.change, e.item_id, i.title FROM events e LEFT JOIN items i ON i.id=e.item_id "
        "ORDER BY e.id DESC LIMIT ?", (limit,))]


def state(conn):
    """Everything the web page shows, in one read."""
    ann = annotate(conn)
    items = sorted(ann.values(), key=lambda a: a["sort_key"])
    return {
        "now": iso(now()),
        "projects": project_list(conn),
        "items": [{k: v for k, v in a.items() if k != "sort_key"} for a in items if not a["project_archived"]],
        "agents": [agent_status(conn, r["name"]) for r in conn.execute("SELECT name FROM agents ORDER BY name")],
        "capacity": capacity(conn, ann),
        "targets": targets_view(conn, ann),
        "hook_events": dict(HOOK_EVENTS),  # the Targets tab offers a hook for each event
        "launch_agents": [label for label, _ in parse_launch_agents(setting(conn, "launch_agents"))],
        "model_ladder": parse_ladder(setting(conn, "model_ladder")),
        "model_ids": model_ids(conn),
        "launch_options": launch_options(conn),
        "launch_in": _launch_in(conn, None),
        "tmux": bool(tmux_path()),  # the launch dialog offers tmux only when it is installed
        "start_next": _start_next(conn),
        "queues": {r["agent"]: queue_list(conn, r["agent"], ann)["entries"]
                   for r in conn.execute("SELECT DISTINCT agent FROM queue_entries ORDER BY agent")},
        "manager": manager_view(conn),
        "effort_levels": _levels(setting(conn, "effort_levels")),
        "settings": config_list(conn),
        "events": recent_events(conn),
        "takeovers": takeovers(conn),
        "needs_you": [dict(r) for r in conn.execute(
            "SELECT id, kind, item_id, message_id, human, summary, opened_at FROM needs_you "
            "WHERE closed_at IS NULL ORDER BY id DESC")],
    }


# ---------------------------------------------------------------- go

ROLES = ("deployer", "reviewer", "owner", "worker", "unblocker", "planner", "manager", "context", "idle")


def _owned_goal(conn, actor, names):
    """The first open goal the actor owns, in these projects if any of them have one."""
    rows = conn.execute("SELECT g.* FROM goals g JOIN projects p ON p.id=g.project_id WHERE g.owner=? AND "
                        "g.status='open' ORDER BY p.rank, g.rank, g.id", (actor,)).fetchall()
    here = [g for g in rows if _project_name(conn, g["project_id"]) in names]
    return (here or rows or [None])[0]


def _goal_brief(conn, name, actor):
    """What a goal owner needs in the briefing: the outcome, the test, its items, and who holds its blockers."""
    ann = annotate(conn)
    g = _goal_view(conn, _goal(conn, name), ann)
    open_items = sorted((ann[i] for i in g["items_open"]), key=lambda a: a["sort_key"])
    blockers, seen = [], set()
    for a in open_items:
        for b in [a["id"]] + sorted(_prereq_closure(ann, a["id"])):
            x = ann[b]
            if b in seen or x["status"] not in ("in_progress", "held") or x["assignee"] in (None, actor):
                continue
            seen.add(b)
            other = [n for n in x["goals"] if n != name]
            owner = conn.execute("SELECT owner FROM goals WHERE name=?", (other[0],)).fetchone()["owner"] if other else None
            blockers.append({"id": b, "title": x["title"], "assignee": x["assignee"],
                             "goal": other[0] if other else None, "goal_owner": owner})
    return {"name": g["name"], "project": g["project"], "outcome": g["outcome"], "done_when": g["done_when"],
            "items_open": [{"id": a["id"], "title": a["title"], "status": a["status"], "ready": a["ready"],
                            "assignee": a["assignee"], "doer": a["doer"]} for a in open_items],
            "items_done": len(g["items_done"]), "blockers_held": blockers,
            "lease": _short(_goal_lease(conn, _goal(conn, name), actor)),
            "handoff": goal_handoff(conn, name)["handoff"], "handoff_due": g["handoff_due"],
            "parent_handoff": parent_handoff(conn, _goal(conn, name))}


def go(conn, cwd, actor=None, project=None, role=None, session=None, focus=None, model=None, chat=False,
       agent_type=None):
    """One call for a fresh agent session; see _go. A session that gets a monitor item has role monitor,
    and its brief says which deploy it follows and whom to alert."""
    brief = _go(conn, cwd, actor, project, role, session, focus, model, chat, agent_type)
    it = brief.get("item")
    if it and it["kind"] == "monitor":
        brief["role"] = "monitor"
        brief["monitor"] = _monitor_brief(conn, it)
        _set_role_note(conn, brief["agent"], "monitor", it["id"])
    if it:
        # The rule of the item's project for a look at the finished result: ask first, or push and then ask.
        brief["result_look"] = setting(conn, "result_look", item_id=it["id"])
        # The handoff of each open goal of the item: the goal's context, for a session that starts on it.
        # A sub-goal's context is its parent's handoff, then its own.
        gb = brief.get("goal") or {}  # the owner's briefing shows its goal's handoffs already
        seen = {gb.get("name"), (gb.get("parent_handoff") or {}).get("goal")}
        brief["handoffs"] = []
        for r in conn.execute("SELECT g.* FROM item_goals ig JOIN goals g ON g.id=ig.goal_id WHERE ig.item_id=? "
                              "AND g.status='open' ORDER BY g.rank, g.id", (it["id"],)).fetchall():
            for h in (parent_handoff(conn, r), _handoff(conn, r["id"]) and dict(_handoff(conn, r["id"]), goal=r["name"])):
                if h and h["goal"] not in seen:
                    seen.add(h["goal"])
                    brief["handoffs"].append(h)
        # Items of a shared goal have no owner who plans them: the agent tags the items it adds itself.
        brief["shared_goals"] = [r["name"] for r in conn.execute(
            "SELECT g.name FROM item_goals ig JOIN goals g ON g.id=ig.goal_id WHERE ig.item_id=? AND g.shared=1 "
            "AND g.status='open' ORDER BY g.rank, g.id", (it["id"],))]
    if "messages" in brief:
        # Counted after the claim: it accepts the push alert of the item this briefing gives, so the alert is
        # not unread any more. The count from before sent each started session to an empty inbox (#1605).
        brief["messages"] = unread(conn, brief["agent"])
    return brief


def _monitor_brief(conn, it):
    dep = _item(conn, it["found_during"]) if it["found_during"] else None
    deployer = dep and (dep["assignee"] or _done_by(conn, dep["id"]))
    return {"deploy": {"id": dep["id"], "title": dep["title"], "status": dep["status"]} if dep else None,
            "deployer": deployer, "target": it["target"], "person": _person(conn)}


def _go(conn, cwd, actor=None, project=None, role=None, session=None, focus=None, model=None, chat=False,
        agent_type=None):
    """One call for a fresh agent session: find the project, name the session, pick a role, and brief it.

    chat: a chat app session with no folder (maxpm mcp from Claude desktop). With no project named and no
    project at cwd, its area is every project; it takes only items that need no folder (needs_folder).

    focus (MAXPM_FOCUS, set when the page opens an agent): "help:<id>@<person>" briefs the session to do a
    person's item together with the person (the Copy prompt text); "needs:@<person>" the same for everything
    that waits on the person; "unblock:<id>" takes work that unblocks that item first; "item:<id>" (Start,
    Dispatch) claims that item, or says why not."""
    if role is not None and role not in ROLES:
        raise RiverError(f"role is one of {', '.join(ROLES)}")
    if project:
        names = _names(conn, project)
    else:
        names = projects_for_dir(conn, cwd)
        # A session in a project folder is not a chat, even with MAXPM_CHAT set: the Codex CLI reads the same
        # MCP config as the ChatGPT app, and its sessions run in the project folder.
        chat = chat and not names
        if not names and chat:
            names = [p["name"] for p in project_list(conn)]
            if not names:
                raise RiverError("no projects in this queue yet: plan first (maxpm plan), or add one: maxpm project add <name>")
        if not names:
            listing = "; ".join(f"{p['name']}" + (f" ({p['path']})" if p.get("path") else "") for p in project_list(conn))
            q = queue_note()
            raise RiverError(
                (f"{q}. If this folder's work is in another queue, that is why: start the agent without MAXPM_DB. "
                 if q else "")
                + f"no project is linked to {Path(cwd).resolve()} in this queue. Ask the user which project this "
                f"folder is, then: maxpm go --project <name>. A project without a folder: maxpm project path <name> . "
                f"A new project for this folder: maxpm init. Do not move a project that is linked to another "
                f"folder. Projects: {listing or '(none)'}")
    area = ",".join(names)

    # Identity: reuse a registered name, else make one and register it.
    new_name = False
    if actor:
        if not conn.execute("SELECT 1 FROM agents WHERE name=?", (actor,)).fetchone():
            register(conn, actor, note=f"started with maxpm go in {area}")
            new_name = True
    else:
        import secrets
        actor = f"{'chat' if chat and not project else names[0]}-{secrets.token_hex(2)}"
        register(conn, actor, note=f"started with maxpm go in {area}" + (" (chat, no folder)" if chat else ""))
        new_name = True
    activity(conn, actor)
    if session:
        set_session(conn, actor, session)
    if model:
        set_agent_model(conn, actor, model)
    if agent_type:
        set_agent_type(conn, actor, agent_type)
    with tx(conn):
        conn.execute("UPDATE agents SET role=NULL WHERE name=? AND role='planner'", (actor,))

    ann = annotate(conn)
    descs = {p["name"]: p["notes"] for p in project_list(conn) if p["name"] in names}
    trackers = {p["name"]: p["tracker"] for p in project_list(conn) if p["name"] in names and p["tracker"]}
    in_area = [a for a in ann.values() if a["project"] in names]
    open_in_area = [a for a in in_area if a["status"] in OPEN_STATES]
    human_ready = sorted((a for a in in_area if a["ready"] and a["doer"] == "human"), key=lambda a: a["sort_key"])
    brief = {"agent": actor, "new_name": new_name, "projects": names, "descriptions": descs, "trackers": trackers,
             "human_waiting": [{"id": a["id"], "title": a["title"],
                                "blocks": [{"id": d, "title": ann[d]["title"], "priority": ann[d]["effective_priority"]}
                                           for d in a["unblocks"] if ann[d]["status"] in OPEN_STATES]}
                               for a in human_ready],
             "has_history": bool(history(conn, actor, limit=1)),
             "auto_continue": setting(conn, "auto_continue", agent=actor) == "on",
             "session": _agent(conn, actor)["session"],
             "human_wait_max": setting(conn, "human_wait_max", agent=actor),
             "fresh_sessions": setting(conn, "fresh_sessions", agent=actor) == "on",
             "messages": unread(conn, actor),
             "humans": [r["name"] for r in conn.execute("SELECT name FROM agents WHERE kind='human' ORDER BY name")],
             "chat": chat}

    brief["unsynced"] = unsynced(conn, actor)
    stop = stop_request(conn, actor)
    if stop:
        holds = [item_show(conn, r["id"]) for r in conn.execute(
            "SELECT id FROM items WHERE assignee=? AND status IN ('in_progress','held') ORDER BY id", (actor,))]
        ended = not holds and _finish_stop(conn, actor)
        brief.update(role="stopped", item=None, stop=stop, stop_holds=holds, ended=ended,
                     why=f"stop requested by {stop['stop_by']}: {stop['stop_reason']}")
        return brief
    brief["queue_instructions"] = queue_instructions(conn, actor)
    brief["model"] = agent_model(conn, actor)
    brief["model_skipped"] = skipped = []
    mine_goal = _owned_goal(conn, actor, names)
    if mine_goal is not None:
        brief["goal"] = _goal_brief(conn, mine_goal["name"], actor)

    # Resume: an item already held.
    held = conn.execute("SELECT id FROM items WHERE assignee=? AND status IN ('in_progress','held') "
                        "ORDER BY status='held', id", (actor,)).fetchall()
    if held and role in (None, "worker", "unblocker"):
        parent = _item(conn, held[0]["id"])
        if parent["status"] == "held":
            ann = annotate(conn)
            mine = sorted((ann[b] for b in _open_prereqs(conn, parent["id"])
                           if ready_for(ann[b], actor) and ann[b]["reserved_for"] == actor), key=lambda a: a["sort_key"])
            if mine:
                try:
                    item = claim(conn, mine[0]["id"], actor)
                except RiverError as e:
                    brief["claim_refused"] = str(e)
                else:
                    brief.update(role="worker", item=item,
                                 why=f"you hold #{parent['id']}; #{item['id']} is a prerequisite reserved for you")
                    _set_role_note(conn, actor, "worker", item["id"])
                    return brief
        item = item_show(conn, held[0]["id"])
        if parent["status"] == "held" and _human_prereqs(conn, parent["id"]):
            brief["human_wait"] = {"on": [{"id": h, "title": _item(conn, h)["title"]}
                                          for h in _human_prereqs(conn, parent["id"])],
                                   "until": _item(conn, parent["id"])["hold_expires_at"]}
        r = {"deploy": "deployer", "review": "reviewer"}.get(item["kind"], "worker")
        if r == "deployer":
            brief.update(_deploy_brief(conn, item))
        brief.update(role=r, resumed=True, item=item,
                     why=f"you already hold #{item['id']}; finish or release it first")
        _set_role_note(conn, actor, r, item["id"])
        return brief

    if role in (None, "worker") and not focus:
        # The agent's own queue before the project queue: the first ready item in it.
        for a in _queue_ready(conn, actor):
            if chat and needs_folder(a):
                skipped.append({"id": a["id"], "title": a["title"], "why": FOLDER_WHY})
                continue
            try:
                item = claim(conn, a["id"], actor)
            except RiverError as e:
                brief["claim_refused"] = str(e)
                continue
            r = "reviewer" if item["kind"] == "review" else "worker"
            brief.update(role=r, item=item, why=f"#{item['id']} is the first ready item in your queue")
            _set_role_note(conn, actor, r, item["id"])
            return brief
        brief["queue_waiting"] = [e for e in queue_list(conn, actor)["entries"] if e.get("item")]

    def try_claim(**kw):
        try:
            got = next_item(conn, claim=True, actor=actor, skipped=skipped, folderless=chat, **kw)
        except RiverError as e:
            brief["claim_refused"] = str(e)
            return None
        return got[0] if got else None

    kind, _, fid = (focus or "").partition(":")
    fid, _, person = fid.partition("@")
    # Deploy now on the Targets tab: the session deploys (owning the free target) or reviews the release.
    if role is None and kind in ("deploy", "review"):
        role = "deployer" if kind == "deploy" else "reviewer"
    if role in (None, "context") and kind == "context" and fid:
        brief.update(role="context", item=None, context=context_brief(conn, fid),
                     why=f"maxpm serve started this session to build the base of goal {fid}")
        _set_role_note(conn, actor, "context", None)
        with tx(conn):  # context_wanted reads it: one context session per goal
            conn.execute("UPDATE agents SET note=? WHERE name=?", (f"role: context for goal {fid}", actor))
        return brief
    if role is None and kind == "needs":
        person = _person(conn, person or None)
        brief.update(role="helper", item=None, help_prompt=prompt_for_all(conn, person),
                     why=f"the page opened this session to work through what waits on {person}, with them")
        _set_role_note(conn, actor, "helper", None)
        return brief
    if role is None and fid.isdigit() and int(fid) in annotate(conn):
        f = item_show(conn, int(fid))
        if f["status"] in OPEN_STATES and kind == "help" and (f["doer"] == "human" or (person and f["assignee"] == person)):
            person = _person(conn, person or None)
            brief.update(role="helper", item=None, help_prompt=prompt_for(conn, f["id"], person),
                         why=f"the page opened this session to do #{f['id']} together with {person}")
            _set_role_note(conn, actor, "helper", f["id"])
            return brief
        if f["status"] == "open" and kind == "monitor" and f["kind"] == "monitor":
            try:
                item = claim(conn, f["id"], actor)
            except RiverError as e:
                brief["focus_note"] = f"The page opened this session to monitor #{f['id']}, but: {e}"
            else:
                brief.update(role="monitor", item=item, why=f"MaximizePM opened this session to follow deploy "
                                                            f"#{f['found_during']} of {f['target']}")
                return brief
        # Start or Dispatch opened this session for one item: claim it, or say why not.
        if kind == "item" and f["status"] in OPEN_STATES and f["assignee"] != actor:
            try:
                item = claim(conn, f["id"], actor)
            except RiverError as e:
                brief["focus_note"] = (f"THE PAGE STARTED THIS SESSION FOR #{f['id']} {f['title']}, BUT YOU CANNOT "
                                       f"TAKE IT: {e}. MaximizePM gives you other work instead; tell the user.")
            else:
                r = "reviewer" if item["kind"] == "review" else "worker"
                brief.update(role=r, item=item, why=f"the page started this session for #{f['id']}")
                _set_role_note(conn, actor, r, item["id"])
                return brief
        if f["status"] in OPEN_STATES and kind == "unblock":
            got = try_claim(unblocks=str(f["id"]))
            if got:
                brief.update(role="unblocker", item=got,
                             why=f"the page opened this session to unblock #{f['id']} {f['title']}")
                _set_role_note(conn, actor, "unblocker", got["id"])
                return brief
            brief["focus_note"] = (f"The page opened this session to unblock #{f['id']}, but nothing that blocks it "
                                   f"is ready for an agent now (maxpm blockers {f['id']}).")
    if role in (None, "worker"):
        ann = annotate(conn)
        pushed = sorted((a for a in ann.values() if a["reserved_for"] == actor and a["reserved_until"] and ready_for(a, actor)),
                        key=lambda a: a["sort_key"])
        for a in pushed:
            try:
                item = claim(conn, a["id"], actor)
            except RiverError as e:
                brief["claim_refused"] = str(e)
                continue
            r = "reviewer" if item["kind"] == "review" else "worker"
            brief.update(role=r, item=item, why=f"#{item['id']} was pushed to you by {a['reserved_by'] or 'someone'}")
            _set_role_note(conn, actor, r, item["id"])
            return brief
    if role in (None, "deployer"):
        got = _deploy_claim(conn, actor, names, take_free=(role == "deployer"), brief=brief)
        if got:
            brief.update(_deploy_brief(conn, got))
            brief.update(role="deployer", item=got,
                         why=f"you own target {got['target']} and #{got['id']} is ready to deploy")
            _set_role_note(conn, actor, "deployer", got["id"])
            return brief
        if role == "deployer":
            brief.update(role="idle", item=None, held_by_others=[],
                         why=brief.get("claim_refused") or "no deploy item is ready for the targets you own")
            _set_role_note(conn, actor, "idle", None)
            return brief
    if role in (None, "reviewer"):
        # A release waits on its review: reviews come before new work, so finished work goes out.
        got = _review_claim(conn, actor, names, any_project=(role == "reviewer"), brief=brief)
        if got:
            brief.update(role="reviewer", item=got,
                         why=f"release {got['target']} waits on this review, and everything it ships is done")
            _set_role_note(conn, actor, "reviewer", got["id"])
            return brief
        if role == "reviewer":
            brief.update(role="idle", item=None, held_by_others=[],
                         why=brief.get("claim_refused") or "no release review is ready")
            _set_role_note(conn, actor, "idle", None)
            return brief
    if role in (None, "owner", "worker"):
        g = _owned_goal(conn, actor, names)
        if g is None and role in (None, "owner"):
            # Agents own outcomes: take an open goal nobody owns. --role owner takes the highest-ranked one.
            # Plain go never forces a goal: it takes one only when the best ready work in the area serves it
            # (the item is tagged with the goal, or an open item of the goal waits on it); otherwise the
            # agent works as a worker below. After goal done, the owner continues with go --role owner.
            # A shared goal is never taken: its items are normal work for every agent.
            free = conn.execute(f"SELECT g.name FROM goals g JOIN projects p ON p.id=g.project_id WHERE g.status='open' "
                                f"AND g.owner IS NULL AND g.shared=0 AND p.archived=0 AND p.name IN ({','.join('?' * len(names))}) "
                                f"ORDER BY p.rank, g.rank, g.id", names).fetchall()
            if role is None:
                try:
                    best = next_item(conn, area, None, False, actor, 1)
                except RiverError:
                    best = []
                serves = set()
                if best:
                    ann = annotate(conn)
                    serves = set(best[0].get("goals") or [])
                    for a in ann.values():
                        if a["goals"] and a["status"] in OPEN_STATES and best[0]["id"] in _prereq_closure(ann, a["id"]):
                            serves.update(a["goals"])
                free = [f for f in free if f["name"] in serves]
            for f in free:
                try:
                    goal_own(conn, f["name"], actor)
                except RiverError as e:
                    brief["claim_refused"] = str(e)
                    continue
                g = _goal(conn, f["name"])
                brief["took_goal"] = True
                break
        if g is not None:
            gb = brief["goal"] = _goal_brief(conn, g["name"], actor)
            ann = annotate(conn)
            tagged = sorted((a for a in ann.values() if g["name"] in a["goals"] and ready_for(a, actor) and a["doer"] != "human"
                             and a["kind"] not in ("deploy", "review") and a["reserved_for"] in (None, actor)), key=lambda a: a["sort_key"])
            for a in tagged:  # (1) the next ready item of the goal
                try:
                    item = claim(conn, a["id"], actor)
                except RiverError as e:
                    brief["claim_refused"] = str(e)
                    continue
                brief.update(role="owner", item=item, why=f"#{item['id']} is the next ready item for your goal {g['name']}")
                _set_role_note(conn, actor, "owner", item["id"])
                return brief
            for o in gb["items_open"]:  # (2) work outside the goal that unblocks it
                item = try_claim(unblocks=str(o["id"]))
                if item:
                    brief.update(role="owner", item=item,
                                 why=f"#{item['id']} unblocks #{o['id']} of your goal {g['name']}")
                    _set_role_note(conn, actor, "owner", item["id"])
                    return brief
            if not gb["items_open"]:  # (3) nothing open: judge the outcome
                brief.update(role="owner", item=None, goal_action="judge",
                             why=f"your goal {g['name']} has no open items")
                _set_role_note(conn, actor, "owner", None)
                return brief
            brief["goal_action"] = "wait"  # (4) its items wait on others: take other work meanwhile
        elif role == "owner":
            brief.update(role="idle", item=None, held_by_others=[], why="no goal here is free")
            _set_role_note(conn, actor, "idle", None)
            return brief
    if role in (None, "worker"):
        item = try_claim(project=area)
        if item:
            brief.update(role="worker", item=item, why=f"#{item['id']} is the most important ready item in {area}"
                         + ("; your goal waits on others meanwhile" if brief.get("goal_action") == "wait" else ""))
            _set_role_note(conn, actor, "worker", item["id"])
            return brief
    if role in (None, "unblocker"):
        item = try_claim(unblocks=names[0]) if len(names) == 1 else None
        if item is None and len(names) > 1:
            for n in names:
                item = try_claim(unblocks=n)
                if item:
                    break
        if item:
            brief.update(role="unblocker", item=item,
                         why=f"nothing is ready in {area}; #{item['id']} ({item['project']}) clears the way for it")
            _set_role_note(conn, actor, "unblocker", item["id"])
            return brief

    if brief.get("goal_action") == "wait":
        brief.update(role="owner", item=None,
                     why=f"the open items of your goal {brief['goal']['name']} wait on other sessions or people")
        _set_role_note(conn, actor, "owner", None)
        return brief
    outside = [a for a in open_in_area if a["status"] == "open" and a["blocked_reason"]]
    held_by_others = [a for a in open_in_area if a["status"] in ("in_progress", "held")]
    needs_plan = not open_in_area or (not held_by_others and len(outside) == len([a for a in open_in_area if a["status"] == "open"]))
    if role == "planner" or (role is None and needs_plan):
        brief.update(role="planner", item=None,
                     open_items=[{"id": a["id"], "title": a["title"], "blocked_reason": a["blocked_reason"],
                                  "blocked_text": a["blocked_text"], "open_blockers": a["open_blockers"]} for a in open_in_area],
                     why=("the project has no open items" if not open_in_area
                          else "every open item waits on something outside the queue"))
        _set_role_note(conn, actor, "planner", None)
        return brief

    brief.update(role="idle", item=None,
                 held_by_others=[{"id": a["id"], "title": a["title"], "assignee": a["assignee"]} for a in held_by_others],
                 why=("other sessions hold all the work that can move now" if held_by_others
                      else "nothing here can move now, and nothing it waits on is ready"))
    _set_role_note(conn, actor, "idle", None)
    return brief


def _deploy_claim(conn, actor, names, take_free=False, brief=None):
    """Claim a ready deploy item on a target the actor owns; with take_free, first own a free target of these projects."""
    if take_free:
        targets = [r["target"] for r in conn.execute(
            f"SELECT DISTINCT target FROM projects WHERE target IS NOT NULL AND name IN ({','.join('?' * len(names))})",
            names)] if names else []
        for t in targets:
            try:
                target_own(conn, t, actor)
            except RiverError as e:
                if brief is not None:
                    brief["claim_refused"] = str(e)
    owned = [r["name"] for r in conn.execute("SELECT name FROM targets WHERE owner=? ORDER BY name", (actor,))]
    if not owned:
        return None
    ann = annotate(conn)
    ready = sorted((a for a in ann.values() if a["kind"] == "deploy" and a["target"] in owned and a["ready"]),
                   key=lambda a: a["sort_key"])
    for a in ready:
        try:
            return claim(conn, a["id"], actor)
        except RiverError as e:
            if brief is not None:
                brief["claim_refused"] = str(e)
    return None


def _deploy_brief(conn, item):
    t = dict(_target(conn, item["target"]))
    nxt = conn.execute("SELECT id FROM items WHERE kind='deploy' AND target=? AND status='open' AND id<>? ORDER BY id",
                       (t["name"], item["id"])).fetchall()
    mon = conn.execute("SELECT id, title, status, assignee FROM items WHERE kind='monitor' AND found_during=?",
                       (item["id"],)).fetchone()
    return {"target": {"name": t["name"], "description": t["description"], "owner": t["owner"], "monitor": t["monitor"]},
            "monitor_item": dict(mon) if mon else None, "hooks": target_hooks(conn, t["name"]),
            "ships": item["waits_on_detail"], "release": release_plan(conn, t["name"]),
            "next_deploy": [item_show(conn, r["id"]) for r in nxt]}


def _set_role_note(conn, actor, role, item_id):
    note = f"role: {role}" + (f" on #{item_id}" if item_id else "")
    with tx(conn):
        # A role with work ends a wait; idle keeps the wait clock, so wait_max counts from the first wait.
        keep = role in ("idle", "waiting")
        conn.execute("UPDATE agents SET note=?, role=?, waiting_since=CASE WHEN ? THEN waiting_since END, "
                     "waiting_in=CASE WHEN ? THEN waiting_in END WHERE name=?", (note, role, keep, keep, actor))


def focus_role(focus):
    """The role a session's MAXPM_FOCUS gives each of its maxpm go: deployer (deploy:<target>), reviewer
    (review:<target>), else None (the other focuses act on the first go only)."""
    return {"deploy": "deployer", "review": "reviewer"}.get((focus or "").partition(":")[0])


def _work_for(conn, actor, names, role=None):
    """Why this agent has something to do now, or None: a push to it, ready work it can take, or messages. With
    role (focus_role), only what maxpm go gives that role: a ready deploy item of a target the agent owns or
    can own, or a ready release review; never a push or an item that go refuses to the role."""
    note = conn.execute("SELECT kind, body FROM queue_entries WHERE agent=? AND kind<>'item' AND delivered_at IS NULL "
                        "ORDER BY kind<>'stop', pos LIMIT 1", (actor,)).fetchone()
    if role:
        if note:
            return f"your queue has {'a stop request' if note['kind'] == 'stop' else 'an instruction'}: {note['body']}"
        ann = annotate(conn)
        if role == "deployer":
            targets = {r["name"] for r in conn.execute("SELECT name FROM targets WHERE owner=?", (actor,))} | {
                r["name"] for r in conn.execute(
                    f"SELECT t.name FROM targets t JOIN projects p ON p.target=t.name WHERE t.owner IS NULL "
                    f"AND p.name IN ({','.join('?' * len(names))})", names)}
            got = next((a for a in sorted(ann.values(), key=lambda a: a["sort_key"])
                        if a["kind"] == "deploy" and a["target"] in targets and a["ready"]), None)
        else:
            got = next((a for a in sorted(ann.values(), key=lambda a: a["sort_key"]) if a["kind"] == "review"
                        and a["ready"] and a["reserved_for"] in (None, actor) and not author_refusal(conn, a, actor)), None)
        if got:
            return f"#{got['id']} is ready: {got['title']}"
        u = unread(conn, actor)
        return "you have messages (maxpm inbox)" if u["unread"] or u["questions"] else None
    pushed = conn.execute("SELECT id FROM items WHERE reserved_for=? AND reserved_until IS NOT NULL "
                          "AND status='open'", (actor,)).fetchone()
    if pushed:
        return f"#{pushed['id']} was pushed to you"
    if note:
        return f"your queue has {'a stop request' if note['kind'] == 'stop' else 'an instruction'}: {note['body']}"
    q = _queue_ready(conn, actor)
    if q:
        return f"#{q[0]['id']} in your queue is ready: {q[0]['title']}"
    try:
        nxt = next_item(conn, ",".join(names) if names else None, None, False, actor, 1)
    except RiverError:
        nxt = []
    if nxt:
        return f"#{nxt[0]['id']} is ready: {nxt[0]['title']}"
    u = unread(conn, actor)
    if u["unread"] or u["questions"]:
        return "you have messages (maxpm inbox)"
    return None


def wait(conn, cwd, actor, project=None, step=None, sleep=None, poll=3.0, focus=None):
    """Block until this agent has work (a push, a ready item in its projects, a message), for at most wait_step.
    A session whose focus gives every go a role (focus_role: a deployer, a reviewer) waits only for that work, as
    go would give it (#1012: a deployer woke for a console item, and go answered IDLE); it waits in no project,
    so no push of project work (Dispatch, maxpm serve) goes to it.

    Returns result work (run go), again (run wait again), or end: no work came within wait_max since the
    first wait, so river released the agent's goals and unregistered it, and the session should stop."""
    import time
    sleep = sleep or time.sleep
    if not actor:
        raise RiverError("waiting needs an agent name: pass --as <name>")
    _agent(conn, actor)
    names = _names(conn, project) if project else projects_for_dir(conn, cwd)
    role = focus_role(focus)

    def stopped():
        st = stop_request(conn, actor)
        if st:
            return {"result": "stop", "agent": actor, "stop": st, "ended": _finish_stop(conn, actor)}
    st = stopped()
    if st:
        return st
    with tx(conn):
        held = conn.execute("SELECT id FROM items WHERE assignee=? AND status IN ('in_progress','held')",
                            (actor,)).fetchall()
        if held:
            raise RiverError(f"{actor} holds {', '.join('#' + str(r['id']) for r in held)}; finish or release it "
                             f"before you wait: maxpm --as {actor} go")
        conn.execute("UPDATE agents SET waiting_since=COALESCE(waiting_since, ?), role='waiting', waiting_in=?, "
                     "note=? WHERE name=?", (iso(now()), None if role else ",".join(names),
                                             f"waiting for {role} work (maxpm wait)" if role else "waiting for work (maxpm wait)",
                                             actor))
    since = parse_iso(_agent(conn, actor)["waiting_since"])
    limit = parse_duration(setting(conn, "wait_max", agent=actor))
    step = parse_duration(step or setting(conn, "wait_step", agent=actor))
    deadline = min(now() + step, since + limit)
    while True:
        st = stopped()
        if st:
            return st
        why = _work_for(conn, actor, names, role)
        if why:
            return {"result": "work", "why": why, "agent": actor}
        if now() >= deadline:
            break
        poll_activity(conn, actor)  # a waiting session is active: it takes work within seconds
        sleep(poll)
    if now() < since + limit:
        return {"result": "again", "agent": actor, "left": _short(since + limit - now())}
    with tx(conn):
        _release_goals(conn, actor, f"{actor} ended after waiting")
    try:
        unregister(conn, actor, "maxpm")
    except RiverError as e:  # it owns a deploy target: keep it registered, and say so
        return {"result": "end", "agent": actor, "waited": _short(limit), "kept": str(e)}
    return {"result": "end", "agent": actor, "waited": _short(limit)}


# ---------------------------------------------------------------- fresh sessions

FRESH_GRACE = timedelta(minutes=2)

def _add_note(conn, item_id, text):
    it = _item(conn, item_id)
    conn.execute("UPDATE items SET notes=? WHERE id=?", ((it["notes"] + "\n" if it["notes"] else "") + text, item_id))


def _want_fresh(conn, item_id, note):
    """An answer came for an agent item that an agent worked on before and nobody holds now (the agent filed a
    person's item and ended, or asked a question and ended): put the answer in the item's notes, and mark it
    for maxpm serve, which starts a fresh session for it (fresh_sessions). Inside a tx."""
    it = _item(conn, item_id)
    if (it["status"] != "open" or it["doer"] == "human" or it["kind"] != "work" or it["reserved_for"]
            or not conn.execute("SELECT 1 FROM events WHERE item_id=? AND change LIKE 'claimed%'", (item_id,)).fetchone()):
        return False
    _add_note(conn, item_id, note)
    if setting(conn, "fresh_sessions", item_id=item_id) != "on":
        return False  # the answer is in the notes; whoever runs go next takes the item
    conn.execute("UPDATE items SET fresh_start=? WHERE id=?", (iso(now()), item_id))
    _event(conn, item_id, "maxpm", "an answer came: a fresh session takes it (fresh_sessions)")
    return True


def fresh_items(conn):
    """The items maxpm serve starts a fresh session for now; it clears the mark of an item that is not ready
    for a new session any more (claimed, closed, reserved, waits again)."""
    rows = conn.execute("SELECT id FROM items WHERE fresh_start IS NOT NULL ORDER BY fresh_start, id").fetchall()
    if not rows:
        return []
    ann, out = annotate(conn), []
    with tx(conn):
        for r in rows:
            a = ann.get(r["id"])
            if a and a["status"] == "open" and a["ready"] and not a["reserved_for"]:
                out.append(r["id"])
            conn.execute("UPDATE items SET fresh_start=NULL WHERE id=?", (r["id"],))
    return out


def idle_news(conn, agent, since):
    """What came for an agent after its last river command (since): unread messages to it or to the holder of
    its items, that native_message did not deliver (from a sender other than river, or about an item it holds
    or that is reserved for it), and entries of its queue."""
    msgs = [dict(r) for r in conn.execute(
        f"SELECT m.id, m.kind, m.from_agent, m.item_id, m.body FROM messages m WHERE {_TO_ME} AND m.from_agent<>? "
        "AND m.read_at IS NULL AND m.created_at > ? AND (m.native_status IS NULL OR m.native_status<>'sent') "
        "AND (m.from_agent<>'maxpm' OR m.item_id IN (SELECT id FROM items WHERE assignee=? OR reserved_for=?)) "
        "ORDER BY m.id", (agent, agent, agent, since, agent, agent))]
    entries = [dict(r) for r in conn.execute(
        "SELECT id, kind, item_id, body, added_by FROM queue_entries WHERE agent=? AND delivered_at IS NULL "
        "AND created_at > ? AND (native_status IS NULL OR native_status<>'sent') ORDER BY pos, id", (agent, since))]
    return {"messages": msgs, "entries": entries} if msgs or entries else None


def hand_over(conn, agent, news):
    """An agent idle at its prompt got news (idle_news): take its work back so fresh sessions do it. Its held
    items are released (to check first: its changes may be in the folder), its pushes and reservations end, its
    queued items leave its queue, and the news goes into the notes of each item (a message about an item into
    that item's, other news into all). Returns the ids of the items, in order; maxpm serve starts a session for
    each that is ready."""
    with tx(conn):
        ids = []
        add = lambda i: ids.append(i) if i not in ids else None
        for r in conn.execute("SELECT id FROM items WHERE assignee=? AND status IN ('in_progress','held') ORDER BY id",
                              (agent,)).fetchall():
            _unhold(conn, r["id"], "maxpm", f"released: {agent} stopped at its prompt with news for it; "
                    f"a fresh session takes the item (fresh_sessions)")
            conn.execute("UPDATE items SET needs_check=1 WHERE id=?", (r["id"],))
            _add_note(conn, r["id"], f"[maxpm] {agent} worked on this and stopped at its prompt: its changes may be "
                      f"in the folder, not committed. Check git status and git log first.")
            add(r["id"])
        for e in news["entries"]:
            if e["item_id"] is not None:
                conn.execute("DELETE FROM queue_entries WHERE id=?", (e["id"],))
                _event(conn, e["item_id"], "maxpm", f"left the queue of {agent}: it is idle at its prompt; "
                       f"a fresh session takes the item")
                add(e["item_id"])
        for i in _free_reservations(conn, agent, f"{agent} is idle at its prompt; a fresh session takes it"):
            add(i)
        for m in news["messages"]:
            if m["item_id"] is not None and _item(conn, m["item_id"])["status"] == "open":
                add(m["item_id"])
        ids = [i for i in ids if _item(conn, i)["status"] == "open" and _item(conn, i)["doer"] != "human"]
        if ids:
            gone = {r["id"] for r in conn.execute("SELECT id FROM messages WHERE state='declined' AND to_agent=?", (agent,))}
            for m in news["messages"]:
                if m["id"] in gone:
                    continue  # the alert of a push taken back above: the item itself is the news
                for i in ([m["item_id"]] if m["item_id"] in ids else ids):
                    _add_note(conn, i, f"[{m['kind']} #{m['id']} from {m['from_agent']} to {agent}] {m['body']}")
            _mark_read(conn, [m["id"] for m in news["messages"]])
            for e in news["entries"]:
                if e["item_id"] is None and e["kind"] != "stop":
                    for i in ids:
                        _add_note(conn, i, f"[instruction for {agent} from {e['added_by']}] {e['body']}")
                    conn.execute("DELETE FROM queue_entries WHERE id=?", (e["id"],))
    return ids


def end_idle(conn, agent, why):
    """End an agent that is idle at its prompt and holds nothing: tell the senders of what it never read (a
    message, an instruction in its queue), release its goals, and unregister it. Returns None when it holds an
    item or owns a target (it stays)."""
    with tx(conn):
        if conn.execute("SELECT 1 FROM items WHERE assignee=? AND status IN ('in_progress','held')", (agent,)).fetchone() \
                or conn.execute("SELECT 1 FROM targets WHERE owner=?", (agent,)).fetchone() or waits_on_person(conn, {agent}):
            return None
        unread = conn.execute("SELECT id, kind, from_agent, item_id FROM messages WHERE to_agent=? AND from_agent<>'maxpm' "
                              "AND (read_at IS NULL OR (kind='question' AND state='open'))", (agent,)).fetchall()
        for m in unread:
            _send(conn, "notice", "maxpm", f"{agent} ended ({why}) before it read your {m['kind']} #{m['id']}. Send it "
                  f"to another agent (maxpm who), or add an item for it", to=m["from_agent"], item_id=m["item_id"],
                  reply_to=m["id"])
        for e in conn.execute("SELECT body, added_by FROM queue_entries WHERE agent=? AND item_id IS NULL "
                              "AND kind<>'stop' AND added_by IS NOT NULL", (agent,)).fetchall():
            _send(conn, "notice", "maxpm", f"{agent} ended ({why}) before it took your instruction: {e['body']}. "
                  f"Add an item for it, or queue it for another agent", to=e["added_by"])
        _event(conn, None, "maxpm", f"{agent} ended: {why}")
    unregister(conn, agent, "maxpm")
    return {"ended": agent, "why": why}


def active_manager(conn, but=None):
    """The manager session that is active now, or None."""
    for r in conn.execute("SELECT * FROM agents WHERE role='manager' AND kind='ai' ORDER BY last_seen DESC").fetchall():
        if r["name"] != but and _agent_state(conn, r) == "active":
            return r["name"]
    return None


# The loop of maxpm serve writes this file beside the queue after each pass (#1663): a file, so a pass costs
# no write lock, and the pass that could not open the queue is recorded too.
LOOP_FILE = "serve-loop.json"
# The loop is late when its last complete pass is older than this many notify_interval, and than this time.
LOOP_LATE_PASSES = 10
LOOP_LATE_MIN = timedelta(minutes=5)


def _beside_queue(conn, name):
    """The path of a file beside the queue (conn, or with no connection the queue of db_path)."""
    path = conn.execute("PRAGMA database_list").fetchone()[2] if conn is not None else str(db_path())
    return Path(path).parent / name if path else None


def serve_loop_beat(conn, beat):
    """The loop of maxpm serve ran one pass: write what it says (started, last_pass, last_try, error) to
    serve-loop.json beside the queue, with this process. conn is None when the pass could not open the queue.
    Never an error of its own."""
    try:
        p = _beside_queue(conn, LOOP_FILE)
        if p is None:
            return
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps({**beat, "pid": os.getpid(), "host": this_host()}))
        os.replace(tmp, p)
    except Exception:
        pass


def serve_loop(conn):
    """The loop of maxpm serve (watch, fresh sessions, tidy, notifications, reload) as its last pass left it:
    last_pass (the last complete pass), error (of the last pass, or None), age, and late: no complete pass
    for longer than LOOP_LATE_PASSES passes and LOOP_LATE_MIN. The thread ended, a pass hangs, or every pass
    fails; on 2026-10-08 nothing said so for 3 hours (#1663). None when no maxpm serve runs on this queue."""
    try:
        d = json.loads(_beside_queue(conn, LOOP_FILE).read_text())
        since = parse_iso(d.get("last_pass") or d["started"])
        if d["host"] != this_host() or not pid_alive(d["pid"]):
            return None
    except (AttributeError, OSError, ValueError, TypeError, KeyError):
        return None
    age = now() - since
    late = age > max(parse_duration(setting(conn, "notify_interval")) * LOOP_LATE_PASSES, LOOP_LATE_MIN)
    if late:  # the file of a serve that is gone, and its process id belongs to another program now
        info = proc_info(d["pid"])
        if info and "serve" not in (info[2] or ""):
            return None
    return {"started": d.get("started"), "last_pass": d.get("last_pass"), "last_try": d.get("last_try"),
            "error": d.get("error"), "pid": d["pid"], "age": _short(age), "late": late}


def manager_findings(conn):
    """What a manager acts on: stuck agents, agents that wait too long, projects with ready agent work and no
    agent, targets whose owner is away or gone, a loop of maxpm serve that is late, and what waits on the user."""
    ann = annotate(conn)
    t = now()
    agents = [agent_status(conn, r["name"]) for r in conn.execute("SELECT name FROM agents WHERE kind='ai' ORDER BY name")]
    queued = {r["agent"]: r["n"] for r in conn.execute(
        "SELECT agent, COUNT(*) n FROM queue_entries WHERE item_id IS NOT NULL GROUP BY agent")}
    stuck = []
    for a in agents:
        if a["state"] in ("away", "gone") and (a["holds"] or queued.get(a["name"])):
            dead = bool(a.get("pid")) and a.get("host") == this_host() and not pid_alive(a["pid"])
            stuck.append({"agent": a["name"], "state": a["state"], "why": "its process ended" if dead else
                          f"not seen for {_short(t - parse_iso(a['last_seen']))}",
                          "holds": [h["id"] for h in a["holds"]], "queued": queued.get(a["name"], 0)})
    lost = [{"id": x["id"], "title": x["title"], "project": x["project"]} for x in ann.values()
            if x["needs_check"] and x["status"] == "open" and not x["reserved_until"] and not x["project_archived"]]
    too_long = parse_duration(setting(conn, "wait_too_long"))
    waiting = [{"agent": a["name"], "since": a["waiting_since"], "waited": _short(t - parse_iso(a["waiting_since"])),
                "in": a["waiting_in"]} for a in agents
               if a["role"] == "waiting" and a["waiting_since"] and a["state"] == "active"
               and t - parse_iso(a["waiting_since"]) > too_long]
    unconnected = []
    for r in conn.execute("SELECT * FROM agents WHERE kind='ai' ORDER BY name"):
        if not_connected(conn, r):
            it = next((x for x in ann.values() if x["reserved_for"] == r["name"] and x["status"] == "open"), None)
            unconnected.append({"agent": r["name"], "since": _short(t - parse_iso(r["registered_at"])),
                                "item": {"id": it["id"], "title": it["title"]} if it else None})
    sessions = project_sessions(conn, ann)
    types = {n: session_type(conn, n) for ns in sessions.values() for n in ns}
    pool = sorted((x for x in ann.values() if x["ready"] and x["doer"] != "human" and not x["reserved_for"]
                   and x["kind"] not in ("deploy", "review", "monitor") and not x["project_archived"]),
                  key=lambda x: x["sort_key"])
    # Ready work by project and agent type: work for Codex needs a Codex session, whatever else runs there.
    # A session of no known type covers any work.
    uncovered, seen = [], set()
    for x in pool:
        key = (x["project"], x["agent"])
        have = [n for n in sessions.get(x["project"], []) if not x["agent"] or types[n] in (None, x["agent"])]
        if not have and key not in seen:
            seen.add(key)
            uncovered.append({"project": x["project"], "agent_type": x["agent"],
                              "launch": agent_for_type(conn, x["agent"]) if x["agent"] else None,
                              "top": {"id": x["id"], "title": x["title"]},
                              "ready": sum(1 for y in pool if (y["project"], y["agent"]) == key)})
    states = {a["name"]: a["state"] for a in agents}
    targets = [{"target": r["name"], "owner": r["owner"], "state": states.get(r["owner"], "gone")}
               for r in conn.execute("SELECT name, owner FROM targets WHERE owner IS NOT NULL ORDER BY name")
               if states.get(r["owner"], "gone") in ("away", "gone", "stopped")]
    humans = [r["name"] for r in conn.execute("SELECT name FROM agents WHERE kind='human'")]
    questions = [dict(r) for r in conn.execute(
        f"SELECT id, from_agent, to_agent, body, item_id FROM messages WHERE kind='question' AND state='open' "
        f"AND to_agent IN ({','.join('?' * len(humans))}) ORDER BY id", humans)] if humans else []
    loop = serve_loop(conn)
    return {"stuck": stuck, "lost_leases": lost, "waiting_too_long": waiting, "uncovered": uncovered,
            "not_connected": unconnected, "serve_loop": [loop] if loop and loop["late"] else [],
            "targets": targets, "questions": questions,
            "human_ready": [{"id": x["id"], "title": x["title"]} for x in sorted(
                               (x for x in ann.values() if x["ready"] and x["doer"] == "human" and not x["project_archived"]),
                               key=lambda x: x["sort_key"])]}


def _finding_keys(f):
    return sorted({f"stuck:{x['agent']}" for x in f["stuck"]} | {f"lost:{x['id']}" for x in f["lost_leases"]}
                  | {f"waiting:{x['agent']}" for x in f["waiting_too_long"]}
                  | {f"unconnected:{x['agent']}" for x in f.get("not_connected", [])} | {f"uncovered:{x['project']}" + (f":{x['agent_type']}" if x.get("agent_type") else "") for x in f["uncovered"]}
                  | {f"target:{x['target']}" for x in f["targets"]} | {f"question:{x['id']}" for x in f["questions"]}
                  | {"serve_loop" for x in f.get("serve_loop", [])}
                  | {f"human:{x['id']}" for x in f["human_ready"]})


def manager_view(conn):
    """The page's Manager section: the active manager (or none), its last actions, what it asked people."""
    name = active_manager(conn)
    if name is None:
        return None
    a = agent_status(conn, name)
    humans = [r["name"] for r in conn.execute("SELECT name FROM agents WHERE kind='human'")]
    return {"name": name, "platform": a.get("platform"), "model": a.get("model"), "state": a["state"],
            "session_url": a.get("session_url"), "pid": a.get("pid"), "can_kill": a["can_kill"],
            "actions": [dict(r) for r in conn.execute(
                "SELECT at, change, item_id FROM events WHERE actor=? AND change LIKE '%(by manager %' "
                "ORDER BY id DESC LIMIT 6", (name,))],
            "asked": [dict(r) for r in conn.execute(
                f"SELECT id, body, to_agent, item_id FROM messages WHERE from_agent=? AND kind='question' AND state='open' "
                f"AND to_agent IN ({','.join('?' * len(humans))}) ORDER BY id", (name, *humans))] if humans else []}


def manage(conn, cwd, actor=None, takeover=None):
    """Start or continue the manager session: one at a time. It changes the plan and runs the other agents
    (launch, queues, messages, stop, launch settings, target give) and takes no work itself."""
    new_name = False
    if not actor:
        import secrets
        actor = f"manager-{secrets.token_hex(2)}"
    if not conn.execute("SELECT 1 FROM agents WHERE name=?", (actor,)).fetchone():
        register(conn, actor, note="role: manager")
        new_name = True
    other = active_manager(conn, but=actor)
    if other and not takeover:
        raise RiverError(f"refused: {other} is the active manager; one manager at a time. Message it "
                         f"(maxpm note {other} \"...\"), or take over: maxpm manage --takeover \"<why>\"")
    activity(conn, actor)
    held = conn.execute("SELECT id FROM items WHERE assignee=? AND status IN ('in_progress','held')", (actor,)).fetchall()
    if held:
        raise RiverError(f"{actor} holds {', '.join('#' + str(r['id']) for r in held)}; a manager holds no work. "
                         f"Finish or release it first, or manage from a new session: maxpm manage (without --as)")
    if other:
        with tx(conn):
            conn.execute("UPDATE agents SET role=NULL, note='manager until taken over' WHERE name=?", (other,))
            _send(conn, "alert", actor, f"{actor} took over as manager: {takeover}. You are no longer the manager.",
                  to=other)
            _event(conn, None, actor, f"took over as manager from {other}: {takeover}")
    _set_role_note(conn, actor, "manager", None)
    f = manager_findings(conn)
    import json
    with tx(conn):  # what the manager has seen: manage --watch reports what is new after this
        conn.execute("UPDATE agents SET manage_seen=? WHERE name=?", (json.dumps(_finding_keys(f)), actor))
    return {"agent": actor, "new_name": new_name, "role": "manager", "status": status(conn), "findings": f,
            "took_over": other, "every": setting(conn, "manage_every"), "settle": setting(conn, "manage_settle", agent=actor),
            "wait_high": setting(conn, "manage_wait_high", agent=actor),
            "wait_low": setting(conn, "manage_wait_low", agent=actor),
            "wait_too_long": setting(conn, "wait_too_long"),
            "tidy_every": setting(conn, "tidy_every"),
            "hooks": {t["name"]: h for t in target_list(conn) for h in [target_hooks(conn, t["name"])] if h},
            "native": has_native(conn, actor)}


def manage_watch(conn, actor, step=None, sleep=None, poll=3.0):
    """maxpm manage --watch: block until something new needs the manager (a new finding, an unread message
    or a stop request), at most manage_every (or step). Findings it reported before do not count as new;
    the messages it returns are marked read, as maxpm inbox --wait does, so the manager needs one watcher.
    After the first new finding it waits manage_settle more, so a group of findings wakes the manager once.
    Each message has a level and may be blocked (#1583, #1608): a blocked message (the sender's item cannot
    move until the answer), a person's message, or a stop request returns at once; a high message (the
    default, also an alert and a question) returns manage_wait_high after it was sent, a low one (a ship
    request notice) after manage_wait_low; a new finding of LOW_FINDINGS waits manage_wait_low from when the
    watch saw it. A return for any reason brings every message and finding that waits, so no second wake
    follows. With native_message the platform brings blocked messages and a person's messages into the
    session, and they do not wake it; the watch brings the others. Returns what changed."""
    import json
    import time
    sleep = sleep or time.sleep
    ag = _agent(conn, actor)
    if ag["role"] != "manager":
        raise RiverError(f"{actor} is not the manager; start with: maxpm manage")
    try:
        base = set(json.loads(conn.execute("SELECT manage_seen FROM agents WHERE name=?", (actor,)).fetchone()[0] or "[]"))
    except (TypeError, ValueError):
        base = set()
    deadline = now() + parse_duration(step or setting(conn, "manage_every", agent=actor))
    settle = parse_duration(setting(conn, "manage_settle", agent=actor))
    native = has_native(conn, actor)
    waits = {"high": parse_duration(setting(conn, "manage_wait_high", agent=actor)),
             "low": parse_duration(setting(conn, "manage_wait_low", agent=actor))}
    settled = None  # when the first new finding that is not low has waited manage_settle
    low_due = None  # when the first new low finding has waited manage_wait_low
    while True:
        f = manager_findings(conn)
        keys = set(_finding_keys(f))
        st = stop_request(conn, actor)
        # Unread only: an open question already read would wake it at once, every time. Not what the platform
        # brought into the session already (native_message).
        waiting = [m for m in conn.execute(
            f"SELECT m.level, m.blocked, m.created_at, m.native_status, a.kind sender FROM messages m "
            f"LEFT JOIN agents a ON a.name=m.from_agent WHERE {_TO_ME} "
            f"AND m.from_agent<>? AND m.read_at IS NULL", (actor, actor, actor)).fetchall()
            if not (native and (m["native_status"] is None or m["native_status"] == "sent"))]
        # A blocked message and a person's message do not wait; MaximizePM may mark one blocked after the send.
        mail = any(m["blocked"] or m["sender"] == "human"
                   or parse_iso(m["created_at"]) + waits[m["level"] or DEFAULT_LEVEL] <= now() for m in waiting)
        low = {k for k in keys - base if k.partition(":")[0] in LOW_FINDINGS}
        if not keys - base - low:
            settled = None  # a finding that went away again wakes nothing
        elif settled is None:
            settled = now() + settle
        if not low:
            low_due = None
        elif low_due is None:
            low_due = now() + waits["low"]
        if (settled and now() >= settled) or (low_due and now() >= low_due) or mail or st or now() >= deadline:
            try:
                rows = inbox(conn, actor) if waiting else []  # all that waits comes along
            except RiverLocked:  # nothing is marked read: the next poll returns with the messages
                sleep(poll)
                continue
            mail = any(m["unread"] for m in rows)
            try:
                with tx(conn):
                    conn.execute("UPDATE agents SET manage_seen=? WHERE name=?", (json.dumps(sorted(keys)), actor))
            except RiverLocked:
                pass  # the messages are read already, so return them; the next watch names these findings again
            return {"agent": actor,
                    "result": "stop" if st else "change" if keys - base else "messages" if mail else "tick",
                    "new": sorted(keys - base), "gone": sorted(base - keys), "findings": f, "stop": st,
                    # A blocked message first: its sender stands still.
                    "messages": sorted((m for m in rows if m["unread"]), key=lambda m: (not m["blocked"], m["id"])),
                    "still_open": sum(1 for m in rows if not m["unread"]),
                    "unread": unread(conn, actor)["unread"], "native": native}
        poll_activity(conn, actor)
        sleep(poll)


def plan(conn, cwd, actor=None, project=None):
    """Start or continue a planner session: the overview plus the questions a planner should raise with the user."""
    names = _names(conn, project) if project else projects_for_dir(conn, cwd)
    new_name = False
    if not actor:
        import secrets
        actor = f"{names[0]}-plan-{secrets.token_hex(2)}" if names else f"planner-{secrets.token_hex(2)}"
        new_name = True
    if not conn.execute("SELECT 1 FROM agents WHERE name=?", (actor,)).fetchone():
        register(conn, actor, note="role: planner")
        new_name = True
    activity(conn, actor)
    held = conn.execute("SELECT id FROM items WHERE assignee=? AND status IN ('in_progress','held')", (actor,)).fetchall()
    if held:
        raise RiverError(f"{actor} holds {', '.join('#' + str(r['id']) for r in held)}; a planner holds no work. "
                         f"Finish or release it first, or plan from a new session: maxpm plan (without --as)")
    _set_role_note(conn, actor, "planner", None)

    ann = annotate(conn)
    live = sorted((a for a in ann.values() if not a["project_archived"] and a["status"] in OPEN_STATES),
                  key=lambda a: a["sort_key"])
    focus = [a for a in live if not names or a["project"] in names]
    brief_item = lambda a: {"id": a["id"], "project": a["project"], "title": a["title"]}
    no_notes = [a for a in focus if not a["notes"].strip() and not a["context"].strip() and a["kind"] != "deploy"]
    stuck = [dict(brief_item(a), reason=a["blocked_reason"], blocked_text=a["blocked_text"],
                  blocked_until=a["blocked_until"], holds_up=a["unblocks_count"])
             for a in focus if a["blocked_reason"]]
    stuck.sort(key=lambda x: -x["holds_up"])
    return {
        "agent": actor, "new_name": new_name, "projects": names, "role": "planner",
        "trackers": {n: setting(conn, "tracker", project_id=_project(conn, n)["id"]) for n in names},
        "cwd": cwd, "folder_has_project": bool(names) or project is not None,
        "status": status(conn),
        "questions": {
            "projects_without_description": [p["name"] for p in project_list(conn)
                                             if not p["notes"].strip() and (not names or p["name"] in names)],
            "items_without_notes": [brief_item(a) for a in no_notes],
            "human_waiting": [brief_item(a) for a in focus if a["ready"] and a["doer"] == "human"],
            "stuck": stuck,
            "replan": [dict(brief_item(a), late_prereqs=a["late_prereqs"]) for a in focus if a["replan"]],
            "suspect": [s for s in cleanup(conn) if not names or s["project"] in names],
            "due": [dict(brief_item(a), due_text=a["due_text"], due_state=a["due_state"],
                         open_before=len(a["open_blockers"])) for a in sorted(
                        (a for a in focus if a["due"]), key=lambda a: a["due"])],
        },
    }
